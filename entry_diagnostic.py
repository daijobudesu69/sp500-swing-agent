"""
entry_diagnostic.py — Shadow log diagnostic for SP500 Swing Agent.

Reads shadow_log.csv and produces a review table covering:
  - EMA9 limit fill rate (filled vs unfilled)
  - Entry improvement: EMA9 entry vs close entry in R terms
  - MAE distribution for closed trades
  - F6/F7 soft warning vs outcome correlation
  - Win rate by fill status

USAGE:
  python entry_diagnostic.py                    # full report (all closed trades)
  python entry_diagnostic.py --min-signals 10   # only run if 10+ closed trades
  python entry_diagnostic.py --show-unfilled    # show only unfilled EMA9 rows
  python entry_diagnostic.py --csv other.csv    # specify alternate CSV path

MANUAL FIELDS REQUIRED (fill in shadow_log.csv after each trade closes):
  _shadow_ema9_filled   True / False
  _shadow_outcome       SL / TP1 / TP2 / FLOAT
  _shadow_r_realized    float (e.g. -1.0, 3.0, 0.53)
  _shadow_hold_days     int

Phase 2.3 — June 2026
"""
import argparse
import csv
import os
import sys
from typing import Any, Dict, List, Optional


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_CSV_PATH = "shadow_log.csv"
EMA9_FILL_GAP_THRESHOLD = 1.5      # % — fills likely if gap <= this
F6_ATR_THRESHOLD = 1.5             # extension threshold in ATR units
F7_RS_THRESHOLD = -3.5             # RS gap threshold in %
DIVIDER = "─" * 72


# ============================================================
# CSV READER
# ============================================================

def load_shadow_log(csv_path: str) -> List[Dict[str, Any]]:
    """
    Load shadow_log.csv into a list of dicts.
    Coerces numeric fields. Leaves blanks as empty string.
    Returns empty list if file not found.
    """
    if not os.path.isfile(csv_path):
        print(f"[ERROR] File not found: {csv_path}")
        print("  Run the daily pipeline at least once to generate shadow_log.csv")
        return []

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(_coerce_row(row))

    return rows


def _coerce_row(row: Dict[str, str]) -> Dict[str, Any]:
    """Coerce numeric string fields to float/int/bool where possible."""
    float_fields = [
        "_shadow_close_price", "_shadow_ema9_price", "_shadow_ema9_gap_pct",
        "_shadow_ema9_ext_atr", "_shadow_atr", "_shadow_atr_pct",
        "_shadow_rs_gap_vs_spy", "_shadow_spy_10d_return", "_shadow_stock_10d_return",
        "_shadow_conviction", "_shadow_haiku_score", "_shadow_adx",
        "_shadow_rsi", "_shadow_volume_ratio", "_shadow_entry_close",
        "_shadow_entry_ema9", "_shadow_stop", "_shadow_tp1", "_shadow_tp2",
        "_shadow_rr", "_shadow_r_realized",
    ]
    int_fields = ["_shadow_confluence_count", "_shadow_hold_days"]
    bool_fields = ["_shadow_ema9_filled"]

    out = dict(row)

    for f in float_fields:
        val = out.get(f, "").strip()
        try:
            out[f] = float(val) if val != "" else None
        except ValueError:
            out[f] = None

    for f in int_fields:
        val = out.get(f, "").strip()
        try:
            out[f] = int(val) if val != "" else None
        except ValueError:
            out[f] = None

    for f in bool_fields:
        val = out.get(f, "").strip().lower()
        if val == "true":
            out[f] = True
        elif val == "false":
            out[f] = False
        else:
            out[f] = None  # blank = not yet filled in

    return out


# ============================================================
# FILTERS
# ============================================================

def get_closed(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows where _shadow_outcome is SL / TP1 / TP2 (not FLOAT or blank)."""
    return [
        r for r in rows
        if r.get("_shadow_outcome", "").strip().upper() in ("SL", "TP1", "TP2")
    ]


def get_filled(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows where _shadow_ema9_filled = True."""
    return [r for r in rows if r.get("_shadow_ema9_filled") is True]


def get_unfilled(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows where _shadow_ema9_filled = False."""
    return [r for r in rows if r.get("_shadow_ema9_filled") is False]


def get_with_outcome(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows where _shadow_r_realized is filled in (any outcome including FLOAT)."""
    return [r for r in rows if r.get("_shadow_r_realized") is not None]


# ============================================================
# ANALYSIS FUNCTIONS
# ============================================================

def analyse_fill_rate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    EMA9 fill rate analysis.
    Splits by filled/unfilled, computes avg gap and fill rate.
    """
    total = len(rows)
    filled = get_filled(rows)
    unfilled = get_unfilled(rows)
    pending = [r for r in rows if r.get("_shadow_ema9_filled") is None]

    gaps = [r["_shadow_ema9_gap_pct"] for r in rows if r.get("_shadow_ema9_gap_pct") is not None]
    filled_gaps = [r["_shadow_ema9_gap_pct"] for r in filled if r.get("_shadow_ema9_gap_pct") is not None]
    unfilled_gaps = [r["_shadow_ema9_gap_pct"] for r in unfilled if r.get("_shadow_ema9_gap_pct") is not None]

    return {
        "total": total,
        "filled_n": len(filled),
        "unfilled_n": len(unfilled),
        "pending_n": len(pending),
        "fill_rate_pct": (len(filled) / (len(filled) + len(unfilled)) * 100)
                          if (filled or unfilled) else None,
        "avg_gap_all": (sum(gaps) / len(gaps)) if gaps else None,
        "avg_gap_filled": (sum(filled_gaps) / len(filled_gaps)) if filled_gaps else None,
        "avg_gap_unfilled": (sum(unfilled_gaps) / len(unfilled_gaps)) if unfilled_gaps else None,
    }


def analyse_entry_improvement(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compare EMA9 entry vs close entry.
    Entry improvement = (close - EMA9) / stop_distance
    Positive = EMA9 starts with a better price in R terms.
    Only computed for rows where both prices are available.
    """
    improvements = []
    for r in rows:
        close = r.get("_shadow_close_price")
        ema9 = r.get("_shadow_entry_ema9")
        stop = r.get("_shadow_stop")
        if close is None or ema9 is None or stop is None:
            continue
        if ema9 <= 0 or stop <= 0:
            continue
        stop_dist_ema9 = ema9 - stop
        if stop_dist_ema9 <= 0:
            continue
        improvement_r = (close - ema9) / stop_dist_ema9
        improvements.append(improvement_r)

    return {
        "n": len(improvements),
        "avg_improvement_r": (sum(improvements) / len(improvements)) if improvements else None,
        "median_improvement_r": _median(improvements),
        "pct_improved": (sum(1 for x in improvements if x > 0) / len(improvements) * 100)
                         if improvements else None,
    }


def analyse_outcomes(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Win rate and expectancy on closed trades."""
    if not closed:
        return {"n": 0}

    wins = [r for r in closed if r.get("_shadow_outcome", "").upper() in ("TP1", "TP2")]
    losses = [r for r in closed if r.get("_shadow_outcome", "").upper() == "SL"]

    r_values = [r["_shadow_r_realized"] for r in closed if r.get("_shadow_r_realized") is not None]
    expectancy = (sum(r_values) / len(r_values)) if r_values else None

    return {
        "n": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else None,
        "expectancy_r": expectancy,
        "net_r": sum(r_values) if r_values else None,
    }


def analyse_f6_f7_vs_outcome(rows_with_outcome: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Split outcomes by F6 (EMA9 extension) and F7 (RS gap) breach status.
    Helps validate whether soft warnings have predictive value.
    """
    f6_breached = [r for r in rows_with_outcome
                   if r.get("_shadow_ema9_ext_atr") is not None
                   and r["_shadow_ema9_ext_atr"] > F6_ATR_THRESHOLD]
    f6_ok = [r for r in rows_with_outcome
             if r.get("_shadow_ema9_ext_atr") is not None
             and r["_shadow_ema9_ext_atr"] <= F6_ATR_THRESHOLD]

    f7_breached = [r for r in rows_with_outcome
                   if r.get("_shadow_rs_gap_vs_spy") is not None
                   and r["_shadow_rs_gap_vs_spy"] < F7_RS_THRESHOLD]
    f7_ok = [r for r in rows_with_outcome
             if r.get("_shadow_rs_gap_vs_spy") is not None
             and r["_shadow_rs_gap_vs_spy"] >= F7_RS_THRESHOLD]

    return {
        "f6_breached": _outcome_summary(f6_breached),
        "f6_ok": _outcome_summary(f6_ok),
        "f7_breached": _outcome_summary(f7_breached),
        "f7_ok": _outcome_summary(f7_ok),
    }


def _outcome_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact win rate + avg R for a subset of rows."""
    if not rows:
        return {"n": 0, "win_rate_pct": None, "avg_r": None}
    closed = get_closed(rows)
    wins = [r for r in closed if r.get("_shadow_outcome", "").upper() in ("TP1", "TP2")]
    r_vals = [r["_shadow_r_realized"] for r in rows if r.get("_shadow_r_realized") is not None]
    return {
        "n": len(rows),
        "closed_n": len(closed),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else None,
        "avg_r": (sum(r_vals) / len(r_vals)) if r_vals else None,
    }


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 != 0 else (s[mid - 1] + s[mid]) / 2


# ============================================================
# PRINT HELPERS
# ============================================================

def _pct(val: Optional[float], decimals: int = 1) -> str:
    return f"{val:.{decimals}f}%" if val is not None else "N/A"


def _r(val: Optional[float]) -> str:
    return f"{val:+.2f}R" if val is not None else "N/A"


def _float(val: Optional[float], decimals: int = 2) -> str:
    return f"{val:.{decimals}f}" if val is not None else "N/A"


def _n(val: Optional[int]) -> str:
    return str(val) if val is not None else "0"


# ============================================================
# REPORT SECTIONS
# ============================================================

def print_signal_table(rows: List[Dict[str, Any]]) -> None:
    """Print all rows as a compact review table."""
    print(f"\n{'DATE':<12} {'TICKER':<7} {'CLOSE':>8} {'EMA9':>8} "
          f"{'GAP%':>6} {'EXT':>6} {'RS':>7} "
          f"{'FILL':>5} {'OUTCOME':<7} {'R':>6}")
    print(DIVIDER)

    for r in rows:
        date = r.get("_shadow_date", "")[:10]
        ticker = r.get("_shadow_ticker", "")
        close = r.get("_shadow_close_price")
        ema9 = r.get("_shadow_entry_ema9")
        gap = r.get("_shadow_ema9_gap_pct")
        ext = r.get("_shadow_ema9_ext_atr")
        rs = r.get("_shadow_rs_gap_vs_spy")
        filled = r.get("_shadow_ema9_filled")
        outcome = r.get("_shadow_outcome", "").strip() or "—"
        r_val = r.get("_shadow_r_realized")

        fill_str = "Y" if filled is True else ("N" if filled is False else "?")
        close_str = f"${close:.2f}" if close is not None else "N/A"
        ema9_str = f"${ema9:.2f}" if ema9 is not None else "N/A"
        gap_str = f"{gap:.2f}%" if gap is not None else "N/A"
        ext_str = f"{ext:.2f}×" if ext is not None else "N/A"
        rs_str = f"{rs:+.1f}%" if rs is not None else "N/A"
        r_str = f"{r_val:+.2f}R" if r_val is not None else "—"

        # Flag soft warning breaches
        ext_flag = " ⚠" if (ext is not None and ext > F6_ATR_THRESHOLD) else "  "
        rs_flag = " ⚠" if (rs is not None and rs < F7_RS_THRESHOLD) else "  "

        print(f"{date:<12} {ticker:<7} {close_str:>8} {ema9_str:>8} "
              f"{gap_str:>6} {ext_str+ext_flag:>8} {rs_str+rs_flag:>9} "
              f"{fill_str:>5} {outcome:<7} {r_str:>6}")


def print_fill_rate_section(analysis: Dict[str, Any]) -> None:
    print(f"\n{'EMA9 FILL RATE ANALYSIS':}")
    print(DIVIDER)
    total = analysis["total"]
    filled = analysis["filled_n"]
    unfilled = analysis["unfilled_n"]
    pending = analysis["pending_n"]
    rate = analysis["fill_rate_pct"]

    print(f"  Total signals logged : {total}")
    print(f"  Filled (limit hit)   : {filled}")
    print(f"  Unfilled (no fill)   : {unfilled}")
    print(f"  Pending (not updated): {pending}")
    print(f"  Fill rate            : {_pct(rate)}")
    print()
    print(f"  Avg EMA9 gap (all)      : {_float(analysis['avg_gap_all'])}%")
    print(f"  Avg EMA9 gap (filled)   : {_float(analysis['avg_gap_filled'])}%")
    print(f"  Avg EMA9 gap (unfilled) : {_float(analysis['avg_gap_unfilled'])}%")
    print(f"  Fill threshold (ref)    : ≤ {EMA9_FILL_GAP_THRESHOLD}%")


def print_entry_improvement_section(analysis: Dict[str, Any]) -> None:
    print(f"\n{'ENTRY IMPROVEMENT: EMA9 vs CLOSE':}")
    print(DIVIDER)
    n = analysis["n"]
    if n == 0:
        print("  No data yet — need close price + EMA9 price + stop in log")
        return
    print(f"  Signals analysed           : {n}")
    print(f"  Avg improvement (EMA9-close): {_r(analysis['avg_improvement_r'])}")
    print(f"  Median improvement          : {_r(analysis['median_improvement_r'])}")
    print(f"  % signals where EMA9 better : {_pct(analysis['pct_improved'])}")
    print()
    print("  Interpretation: positive R = EMA9 entry starts closer to TP direction")
    print("  e.g. +0.3R means EMA9 entry is 0.3R better than buying at close")


def print_outcomes_section(analysis: Dict[str, Any]) -> None:
    print(f"\n{'CLOSED TRADE OUTCOMES':}")
    print(DIVIDER)
    n = analysis.get("n", 0)
    if n == 0:
        print("  No closed trades yet (SL/TP1/TP2) — fill _shadow_outcome in CSV")
        return
    print(f"  Closed trades : {n}")
    print(f"  Wins (TP1/TP2): {analysis['wins']}")
    print(f"  Losses (SL)   : {analysis['losses']}")
    print(f"  Win rate      : {_pct(analysis['win_rate_pct'])}")
    print(f"  Expectancy    : {_r(analysis['expectancy_r'])} per trade")
    print(f"  Net R         : {_r(analysis['net_r'])}")
    print()
    print(f"  Breakeven WR  : 25.0% (for +3R/−1R profile)")


def print_soft_warning_section(analysis: Dict[str, Any]) -> None:
    print(f"\n{'SOFT WARNING PREDICTIVE VALUE (F6 / F7)':}")
    print(DIVIDER)

    def _row(label: str, s: Dict[str, Any]) -> None:
        n = s.get("n", 0)
        closed = s.get("closed_n", 0)
        wr = _pct(s.get("win_rate_pct"))
        avg = _r(s.get("avg_r"))
        print(f"  {label:<35} n={n:<4} closed={closed:<4} WR={wr:<8} avgR={avg}")

    print(f"  {'Group':<35} {'n':<6} {'closed':<10} {'WR':<9} {'avgR'}")
    print(f"  {'-'*65}")
    _row(f"F6 ext > {F6_ATR_THRESHOLD}×ATR  (breached)", analysis["f6_breached"])
    _row(f"F6 ext ≤ {F6_ATR_THRESHOLD}×ATR  (ok)",      analysis["f6_ok"])
    print()
    _row(f"F7 RS gap < {F7_RS_THRESHOLD}%  (breached)", analysis["f7_breached"])
    _row(f"F7 RS gap ≥ {F7_RS_THRESHOLD}%  (ok)",      analysis["f7_ok"])
    print()
    print("  Interpretation: if breached WR << ok WR → thresholds are valid")
    print("  If no difference → consider loosening thresholds in config.py")


def print_data_quality_note(rows: List[Dict[str, Any]]) -> None:
    """Warn if too many manual fields are still blank."""
    total = len(rows)
    if total == 0:
        return
    blank_outcome = sum(1 for r in rows if not r.get("_shadow_outcome", "").strip())
    blank_filled = sum(1 for r in rows if r.get("_shadow_ema9_filled") is None)

    if blank_outcome > 0 or blank_filled > 0:
        print(f"\n  ⚠  DATA QUALITY: {blank_outcome}/{total} rows missing outcome, "
              f"{blank_filled}/{total} missing ema9_filled")
        print("     Fill these in shadow_log.csv after each trade closes.")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SP500 Swing Agent — shadow log diagnostic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python entry_diagnostic.py
  python entry_diagnostic.py --min-signals 10
  python entry_diagnostic.py --show-unfilled
  python entry_diagnostic.py --csv path/to/shadow_log.csv
        """,
    )
    parser.add_argument(
        "--csv",
        default=DEFAULT_CSV_PATH,
        help=f"Path to shadow_log.csv (default: {DEFAULT_CSV_PATH})",
    )
    parser.add_argument(
        "--min-signals",
        type=int,
        default=0,
        metavar="N",
        help="Only run analysis if N or more closed trades exist",
    )
    parser.add_argument(
        "--show-unfilled",
        action="store_true",
        help="Show only rows where EMA9 limit did not fill",
    )
    args = parser.parse_args()

    # ── Load ──────────────────────────────────────────────────
    rows = load_shadow_log(args.csv)
    if not rows:
        sys.exit(1)

    # ── Apply --show-unfilled filter ──────────────────────────
    display_rows = get_unfilled(rows) if args.show_unfilled else rows

    closed = get_closed(rows)
    rows_with_outcome = get_with_outcome(rows)

    # ── --min-signals gate ────────────────────────────────────
    if args.min_signals > 0 and len(closed) < args.min_signals:
        print(f"\n[GATE] Only {len(closed)} closed trades in log. "
              f"Need {args.min_signals} to run analysis.")
        print(f"       {len(rows)} total signals logged, "
              f"{len(rows) - len(closed)} still open/pending.")
        sys.exit(0)

    # ── Header ────────────────────────────────────────────────
    print(f"\n{'='*72}")
    mode = " (UNFILLED ONLY)" if args.show_unfilled else ""
    print(f"  SP500 SWING AGENT — SHADOW LOG DIAGNOSTIC{mode}")
    print(f"  Source: {args.csv}")
    print(f"  Total rows: {len(rows)}  |  Closed: {len(closed)}  |  "
          f"Displaying: {len(display_rows)}")
    print(f"{'='*72}")

    # ── Signal table ──────────────────────────────────────────
    if display_rows:
        print_signal_table(display_rows)
    else:
        label = "unfilled signals" if args.show_unfilled else "signals"
        print(f"\n  No {label} found in log.")

    # ── Analysis sections (only if not filtered to unfilled) ──
    if not args.show_unfilled:
        fill_analysis = analyse_fill_rate(rows)
        entry_analysis = analyse_entry_improvement(rows)
        outcome_analysis = analyse_outcomes(closed)
        soft_analysis = analyse_f6_f7_vs_outcome(rows_with_outcome)

        print_fill_rate_section(fill_analysis)
        print_entry_improvement_section(entry_analysis)
        print_outcomes_section(outcome_analysis)
        print_soft_warning_section(soft_analysis)
        print_data_quality_note(rows)

    print(f"\n{DIVIDER}\n")


if __name__ == "__main__":
    main()
