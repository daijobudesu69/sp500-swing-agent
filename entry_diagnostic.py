"""
entry_diagnostic.py — Shadow log diagnostic for SP500 Swing Agent.

Reads shadow_log.csv and produces a review table covering:
  - EMA9 limit fill rate (filled vs unfilled)
  - Entry improvement: EMA9 entry vs close entry in R terms
  - MAE distribution for closed trades
  - F6/F7 soft warning vs outcome correlation
  - F7 lookback comparison: 10d vs 60d vs 126d RS gap predictive power
  - Win rate by fill status

USAGE:
  python entry_diagnostic.py                    # full report (all closed trades)
  python entry_diagnostic.py --min-signals 10   # only run if 10+ closed trades
  python entry_diagnostic.py --show-unfilled    # show only unfilled EMA9 rows
  python entry_diagnostic.py --csv other.csv    # specify alternate CSV path

MANUAL FIELDS REQUIRED (fill in shadow_log.csv after each trade closes):
  _shadow_ema9_filled   True / False
  _shadow_outcome       SL / TP1_TRAIL / TP2 / INVALIDATED_LOSS /
                         INVALIDATED_PROFIT / FLOAT / TIME_TP / TIME_SL
  _shadow_r_realized    float (e.g. -1.0, 3.0, 0.53, 2.67) — see the
                         "R Calculator" sheet tab for the formula per category
  _shadow_hold_days     int

Phase 2.3 — June 2026
Phase 2.3 — F7 LOOKBACK REVIEW REWRITE — June 2026
====================================================
CHANGED:
  - Outcome categories updated from the old 4-state set (SL/TP1/TP2/FLOAT)
    to the 6-state set actually used in the shadow log going forward:
      SL                  -> loss, closed
      TP1_TRAIL           -> win,  closed (TP1 hit, 50% locked +2R, rest trailed)
      TP2                 -> win,  closed (full target)
      INVALIDATED_LOSS     -> loss, closed (discretionary exit, exit < entry)
      INVALIDATED_PROFIT   -> win,  closed (discretionary exit, exit > entry)
      FLOAT               -> still open, NOT closed
    The old script only recognised SL/TP1/TP2/FLOAT, which silently
    excluded TP1_TRAIL and both INVALIDATED_* rows from every closed-trade
    calculation (win rate, expectancy, F6/F7 breach analysis) even though
    they represent real, closed, R-valued trades. This was a correctness
    bug, not just a missing feature — fixed here.
  - _shadow_rs_60d_gap and _shadow_rs_126d_gap added to the numeric
    coercion list. These existed in the CSV/Sheet schema since the F7
    review but were never read by this script — the entire point of
    collecting them (comparing 10d vs 60d vs 126d predictive power) was
    not actually achievable until this fix.
  - NEW section: print_rs_lookback_comparison() — splits closed trades by
    each lookback's breach status and reports win rate / avg R per
    lookback so the three windows can be compared side by side at the
    next filter review.

TIME-STOP OUTCOME CATEGORIES ADDED — July 2026
================================================
CHANGED:
  - Two new outcome categories added: TIME_TP and TIME_SL. Both apply
    when a trade is held to the max-hold limit (10 trading days, ~14
    calendar days) WITHOUT TP1 ever being tagged, and is closed manually
    at that point rather than via SL or TP1_TRAIL:
      TIME_TP  -> win,  closed (still in profit when max hold reached)
      TIME_SL  -> loss, closed (still in loss/breakeven when max hold reached)
    These sit alongside, not instead of, the existing 6 categories. A
    trade that DID tag TP1 before the hold limit is always TP1_TRAIL (or
    TP2), never TIME_TP/TIME_SL, regardless of how long it was ultimately
    held — the time-stop categories only apply to trades that never
    reached TP1.
    Outcome category count: 8 (was 6). WIN_OUTCOMES/LOSS_OUTCOMES,
    analyse_outcome_breakdown(), and the wins/losses labels in
    print_outcomes_section() were all updated accordingly.

HEADER NORMALIZATION FIX — June 2026:
=======================================
The Google Sheet (and any CSV exported from it via File > Download) uses
column headers WITHOUT the leading underscore (e.g. "shadow_date" instead
of "_shadow_date") — Google Sheets / Sheet column display strips it.
shadow_log.csv written directly by main.py's _append_shadow_log() DOES
use the underscore-prefixed form, since that's the actual dict key used
internally.

Rather than support two parallel naming schemes throughout this entire
file (error-prone, easy to miss one spot), load_shadow_log() now
normalizes every row's keys to the underscore-prefixed form immediately
on read, via _normalize_fieldnames(). Every other function in this file
is unchanged and only ever sees the canonical "_shadow_*" key names,
regardless of which naming convention the source CSV used.

LEGACY ROW-ARTIFACT SKIP — June 21 2026:
=======================================
Two bugs in sheets_logger.py (now fixed at the source, see that file's
docstring) could leave stale artifacts baked into a Google Sheet export
taken BEFORE that fix:
  1. Duplicate-header bug: a second, code-inserted header row landed on
     top of the user's original hand-curated header row. In a CSV
     export, csv.DictReader treats whichever row is physically first as
     the field names — the OTHER header row then gets read back as if
     it were a real trade row, with literal text like "shadow_ticker"
     sitting in the ticker column instead of an actual ticker symbol.
  2. Row-gap bug: append_rows() could land the next day's rows below a
     blank gap row, which reads back as a row of all-empty strings.

Neither bug can recur going forward — sheets_logger.py writes are now
position-explicit and never touch an existing header — but a CSV
exported before that fix can still contain these two artifact types.
load_shadow_log() now detects and skips both via _is_blank_row() and
_is_header_artifact_row(), printing a one-line summary of how many rows
were skipped and why. This is cleanup for stale exports only, not a
defense against anything the current sheets_logger.py can still produce.

PHASE 2.6 — BATCH 4 COLUMNS + NEW ANALYSES (August 2026):
==========================================================
Batch 4 is the first real-money batch. Five columns were added to the
shadow log, and this script would have silently ignored every one of
them — the same failure mode that made rs_60d_gap/rs_126d_gap
uncollectable for a whole phase. All five are now read.

ADDED TO _CANONICAL_FIELDS AND THE COERCION LISTS:
  _shadow_risk_usd              float — dollar risk at entry
  _shadow_entry_type            str   — "strict" | "fallback"
  _shadow_force_closed          bool  — closed by batch cutoff, not by rule
  _shadow_discretionary_action  str   — TAKEN | VETOED | MODIFIED
  _shadow_veto_reason           str   — free text
  _shadow_spy_vs_sma20          float — regime context, LOG ONLY
  _shadow_vix_level             float — regime context, LOG ONLY
  _shadow_pct_above_50dma       float — regime context, LOG ONLY

ALSO READ (manual / formula columns, written by hand in the Sheet and
never by main.py — so they appear in a Sheet export but not in a CSV
written directly by the pipeline):
  _shadow_mfe_price, _shadow_mfe_r, _shadow_days_to_mfe,
  _shadow_fill_date, _shadow_exit_date

NEW SECTION — MFE GIVE-BACK QUADRANTS (the most important one):
  Batch 3 could not answer the single question that decides what to fix
  next: did losing and TIME_TP trades never go anywhere, or did they go
  up and then hand it all back? Those two diagnoses call for OPPOSITE
  fixes — one is a selection problem (change what you buy), the other is
  an exit problem (change when you sell) — and without MFE there is no
  way to tell them apart. Phase 2.1 could tell (12 of 13 losers never
  reached +1R); Batch 3 could not, because the column did not exist.

  give_back = mfe_r - r_realized, then split by days_to_mfe:
    mfe >= 2.0R                -> TP1 was TAGGED. If the outcome is not
                                  TP1_TRAIL/TP2, that is an EXECUTION bug,
                                  not a strategy result.
    1.0-2.0R, peak early (<=5) -> rose, peaked, gave it back -> exit is
                                  too late
    1.0-2.0R, peak late  (>=8) -> still rising when the hold cut it off ->
                                  hold is too short
    < 1.0R                     -> never went anywhere -> SELECTION problem;
                                  changing the hold period cannot help

  Quadrant boundaries (2.0R, day 5) are drawn from just two annotated
  Batch 3 cases (NTAP mfe 2.00 @ day 5 -> 0.73R; URI 1.63 @ day 5 ->
  0.66R). They are provisional starting lines, not established thresholds.

NEW SECTION — ENTRY TYPE (strict vs fallback):
  The out-of-sample test of ENTRY2_ATR_TOLERANCE = 0.15, which was fitted
  in-sample on 5 Batch 3 near-misses and catches 2 of them. Reports win
  rate and avg R for each entry type. Do NOT retune the tolerance from
  this batch's own numbers — that is the in-sample fit that a
  Benjamini-Hochberg pass on Batch 3 already showed 0/9 hypotheses
  surviving. Treat any signal here as a hypothesis for the NEXT batch.

NEW SECTION — DISCRETIONARY ATTRIBUTION:
  Batch 4 is manually filtered, so "how did the system do" and "how did
  the operator do" are two different questions and mixing them makes both
  unanswerable. TAKEN rows measure the executed strategy; VETOED rows,
  scored against what actually happened, measure whether the operator's
  judgement adds anything. Consistently vetoing losers would be evidence
  worth trying to encode as a filter; vetoing winners is worth knowing
  even sooner.

NEW SECTION — DOLLAR RECONCILIATION:
  With floating 2% risk, R is no longer convertible to dollars by a single
  multiplier. Sums r_realized * risk_usd per row to reconstruct realised
  P&L and reconcile it against ACCOUNT_START_USD, so a mismatch against
  the broker balance surfaces here rather than being discovered later.
  Also counts closed rows MISSING risk_usd — those are invisible to the
  equity calculation in sheets_logger.read_equity(), and therefore
  silently mis-size every subsequent trade.

NEW FLAG — --exclude-vetoed:
  Restricts every analysis to discretionary_action == TAKEN.

FORCE-CLOSED HANDLING:
  Rows with force_closed TRUE were ended by an administrative batch cutoff
  rather than by their own stop/target/time rule. They are still real
  closed trades and stay in the totals, but they are counted and reported
  separately — a batch ending mid-trade truncates outcomes in a direction
  that has nothing to do with the strategy.
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
F7_RS_THRESHOLD = -3.5             # RS gap threshold in % (10d, current hard config value)
DIVIDER = "─" * 72

# Outcome category classification (Phase 2.3 F7 review — 6 states,
# extended July 2026 with TIME_TP/TIME_SL — 8 states total)
WIN_OUTCOMES = {"TP1_TRAIL", "TP2", "INVALIDATED_PROFIT", "TIME_TP"}
LOSS_OUTCOMES = {"SL", "INVALIDATED_LOSS", "TIME_SL"}
CLOSED_OUTCOMES = WIN_OUTCOMES | LOSS_OUTCOMES   # everything except FLOAT / blank
OPEN_OUTCOMES = {"FLOAT"}

# Phase 2.6 — discretionary attribution (Batch 4 is manually filtered)
TAKEN_ACTIONS = {"TAKEN", "MODIFIED"}   # MODIFIED = taken with an adjustment
VETOED_ACTIONS = {"VETOED"}

# Phase 2.6 — MFE give-back quadrant boundaries.
# PROVISIONAL. Derived from exactly two annotated Batch 3 cases
# (NTAP mfe 2.00R @ day 5 -> 0.73R; URI 1.63R @ day 5 -> 0.66R).
# Two observations do not establish a threshold — these are starting
# lines for reading the distribution, and should be redrawn once a real
# distribution of mfe_r / days_to_mfe exists.
MFE_TP1_TAGGED_R = 2.0      # at/above this, TP1 was reachable
MFE_WENT_NOWHERE_R = 1.0    # below this, the trade never moved
MFE_PEAK_EARLY_DAY = 5      # peak on/before this day = early
MFE_PEAK_LATE_DAY = 8       # peak on/after this day = still rising at cutoff

# Canonical field list (the underscore-prefixed form every function in
# this file expects). Used by _normalize_fieldnames() to map a header
# WITHOUT the leading underscore back to its canonical name.
_CANONICAL_FIELDS = [
    "_shadow_date", "_shadow_ticker", "_shadow_close_price", "_shadow_ema9_price",
    "_shadow_ema9_gap_pct", "_shadow_ema9_ext_atr", "_shadow_atr", "_shadow_atr_pct",
    "_shadow_rs_gap_vs_spy", "_shadow_spy_10d_return", "_shadow_stock_10d_return",
    "_shadow_rs_60d_gap", "_shadow_rs_126d_gap", "_shadow_conviction",
    "_shadow_confluence_count", "_shadow_haiku_score", "_shadow_adx", "_shadow_rsi",
    "_shadow_volume_ratio", "_shadow_entry_close", "_shadow_entry_ema9",
    "_shadow_stop", "_shadow_tp1", "_shadow_tp2", "_shadow_rr",
    "_shadow_ema9_filled", "_shadow_outcome", "_shadow_r_realized", "_shadow_hold_days",
    # ---- Phase 2.6: written by main.py ----
    "_shadow_risk_usd", "_shadow_entry_type", "_shadow_force_closed",
    "_shadow_discretionary_action", "_shadow_veto_reason",
    "_shadow_spy_vs_sma20", "_shadow_vix_level", "_shadow_pct_above_50dma",
    # ---- Phase 2.6: manual / formula columns, Sheet-only ----
    # Never written by main.py, so absent from a pipeline-written CSV but
    # present in a Sheet export. Listed here so normalisation maps them
    # from the no-underscore Sheet form, and so _is_header_artifact_row()
    # recognises them if one ever lands in a data-row position.
    "_shadow_fill_date", "_shadow_exit_date",
    "_shadow_mfe_price", "_shadow_mfe_r", "_shadow_days_to_mfe",
]
# Map "shadow_date" -> "_shadow_date", etc. — built once at import time.
_NO_UNDERSCORE_TO_CANONICAL = {f.lstrip("_"): f for f in _CANONICAL_FIELDS}

# PHASE 2.6 — third naming convention: BARE names.
# The manual / formula columns are added to the Sheet by hand and carry no
# prefix at all — the header cell literally reads "mfe_r", "days_to_mfe",
# "fill_date". They are not "shadow_mfe_r", so neither map above matches
# them, and every one was being read as an unknown pass-through column:
# the MFE section reported "no mfe_r data" on a sheet that had it.
#
# Restricted to the manual columns ON PURPOSE. Applying bare mapping to
# every canonical field would make a plain "date" or "stop" column collide
# with "_shadow_date" / "_shadow_stop". Narrow map, no collisions.
_MANUAL_BARE_FIELDS = [
    "_shadow_fill_date", "_shadow_exit_date",
    "_shadow_mfe_price", "_shadow_mfe_r", "_shadow_days_to_mfe",
]
_BARE_TO_CANONICAL = {f.replace("_shadow_", ""): f for f in _MANUAL_BARE_FIELDS}


# ============================================================
# CSV READER
# ============================================================

def _normalize_fieldnames(row: Dict[str, str]) -> Dict[str, str]:
    """
    Normalize a single row's keys to the canonical "_shadow_*" form,
    regardless of whether the source CSV used the underscore-prefixed
    form (main.py's direct CSV write) or the no-underscore form
    (Google Sheets column headers / a CSV exported from the Sheet).

    Any key not recognised in either form is passed through unchanged
    (e.g. extra columns someone added manually, like a "Remarks" field
    from the R Calculator tab if it ever ends up in the same export).
    """
    out = {}
    for key, value in row.items():
        if key in _CANONICAL_FIELDS:
            out[key] = value                              # already canonical
        elif key in _NO_UNDERSCORE_TO_CANONICAL:
            out[_NO_UNDERSCORE_TO_CANONICAL[key]] = value  # strip-form -> canonical
        elif str(key).strip().lower() in _BARE_TO_CANONICAL:
            # Phase 2.6: bare manual column ("mfe_r" -> "_shadow_mfe_r").
            # Guarded so an explicit canonical value already present is
            # never overwritten by a bare duplicate.
            canon = _BARE_TO_CANONICAL[str(key).strip().lower()]
            if canon not in out or str(out.get(canon, "")).strip() == "":
                out[canon] = value
        else:
            out[key] = value                               # unknown column, pass through
    return out


def _is_blank_row(row: Dict[str, str]) -> bool:
    """
    True if every value in the row is empty/whitespace. Catches the
    row-gap artifact from the (now-fixed) sheets_logger.py append_rows()
    bug — a gap row reads back as all-empty-string values.

    Handles csv.DictReader's restkey/restval edge cases: if a row has
    more columns than the header, the extras land under a `None` key as
    a list; if it has fewer, missing fields come back as `None` rather
    than "". Both are treated as blank/empty here rather than failing
    the check, since a truly blank source row can produce either shape
    depending on exact comma count.
    """
    for key, value in row.items():
        if key is None:
            # Extra fields beyond the header — csv.DictReader's restkey.
            if isinstance(value, list):
                if any(str(v).strip() != "" for v in value):
                    return False
            elif str(value).strip() != "":
                return False
            continue
        if value is None:
            continue  # missing field (restval) — counts as blank
        if str(value).strip() != "":
            return False
    return True


def _is_header_artifact_row(row: Dict[str, str]) -> bool:
    """
    True if this row's values look like header text rather than real
    trade data — the signature of the (now-fixed) duplicate-header bug,
    where one header row got read back as a data row by csv.DictReader.

    Heuristic: check the ticker field specifically. A real ticker is a
    short alphanumeric string (e.g. "BNY", "UNH"); a header-artifact row
    will instead contain something like "shadow_ticker" or
    "_shadow_ticker" in that same column. This is checked against both
    naming conventions since the artifact row could be either the old
    hand-curated header or the canonical HEADER list, depending on which
    one ended up in the data-row position for a given export.
    """
    ticker_val = str(row.get("_shadow_ticker", "")).strip().lower()
    if not ticker_val:
        return False
    header_like = {f.lower() for f in _CANONICAL_FIELDS} | {
        f.lstrip("_").lower() for f in _CANONICAL_FIELDS
    }
    return ticker_val in header_like


def load_shadow_log(csv_path: str) -> List[Dict[str, Any]]:
    """
    Load shadow_log.csv into a list of dicts.
    Coerces numeric fields. Leaves blanks as empty string.
    Returns empty list if file not found.

    Accepts either header convention transparently (see
    _normalize_fieldnames() docstring above) — every row is normalized
    to "_shadow_*" keys before any further processing happens.
    """
    if not os.path.isfile(csv_path):
        print(f"[ERROR] File not found: {csv_path}")
        print("  Run the daily pipeline at least once to generate shadow_log.csv")
        print("  (or export the Google Sheet 'shadow_log' tab via File > Download > CSV)")
        return []

    rows = []
    skipped_blank = 0
    skipped_header_artifact = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            if _is_blank_row(raw_row):
                skipped_blank += 1
                continue
            normalized = _normalize_fieldnames(raw_row)
            if _is_header_artifact_row(normalized):
                skipped_header_artifact += 1
                continue
            rows.append(_coerce_row(normalized))

    if skipped_blank or skipped_header_artifact:
        print(f"[INFO] Skipped {skipped_blank} blank row(s) and "
              f"{skipped_header_artifact} header-artifact row(s) — "
              f"legacy cleanup from a since-fixed sheets_logger.py bug. "
              f"See module docstring for detail.")

    return rows


def _coerce_row(row: Dict[str, str]) -> Dict[str, Any]:
    """Coerce numeric string fields to float/int/bool where possible."""
    float_fields = [
        "_shadow_close_price", "_shadow_ema9_price", "_shadow_ema9_gap_pct",
        "_shadow_ema9_ext_atr", "_shadow_atr", "_shadow_atr_pct",
        "_shadow_rs_gap_vs_spy", "_shadow_spy_10d_return", "_shadow_stock_10d_return",
        "_shadow_rs_60d_gap", "_shadow_rs_126d_gap",   # ADDED — F7 lookback review
        "_shadow_conviction", "_shadow_haiku_score", "_shadow_adx",
        "_shadow_rsi", "_shadow_volume_ratio", "_shadow_entry_close",
        "_shadow_entry_ema9", "_shadow_stop", "_shadow_tp1", "_shadow_tp2",
        "_shadow_rr", "_shadow_r_realized",
        # ---- Phase 2.6 ----
        # Without these the columns are read as raw strings and every
        # numeric comparison against them silently fails — the exact
        # failure that made rs_60d/rs_126d unusable for a whole phase.
        "_shadow_risk_usd",
        "_shadow_spy_vs_sma20", "_shadow_vix_level", "_shadow_pct_above_50dma",
        "_shadow_mfe_price", "_shadow_mfe_r",
    ]
    int_fields = ["_shadow_confluence_count", "_shadow_hold_days",
                  "_shadow_days_to_mfe"]          # Phase 2.6
    bool_fields = ["_shadow_ema9_filled",
                   "_shadow_force_closed"]        # Phase 2.6

    out = dict(row)

    for f in float_fields:
        val = str(out.get(f, "")).strip()
        try:
            out[f] = float(val) if val != "" else None
        except ValueError:
            out[f] = None

    for f in int_fields:
        val = str(out.get(f, "")).strip()
        try:
            out[f] = int(val) if val != "" else None
        except ValueError:
            out[f] = None

    for f in bool_fields:
        val = str(out.get(f, "")).strip().lower()
        if val == "true":
            out[f] = True
        elif val == "false":
            out[f] = False
        else:
            out[f] = None  # blank = not yet filled in

    return out


# ============================================================
# OUTCOME CLASSIFICATION HELPERS
# ============================================================

def _outcome(row: Dict[str, Any]) -> str:
    """Normalised outcome string, uppercased, stripped. '' if blank."""
    return str(row.get("_shadow_outcome", "")).strip().upper()


def is_closed(row: Dict[str, Any]) -> bool:
    return _outcome(row) in CLOSED_OUTCOMES


def is_win(row: Dict[str, Any]) -> bool:
    return _outcome(row) in WIN_OUTCOMES


def is_loss(row: Dict[str, Any]) -> bool:
    return _outcome(row) in LOSS_OUTCOMES


# ============================================================
# FILTERS
# ============================================================

def get_closed(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Rows whose outcome is one of the 8 known closed categories:
    SL, TP1_TRAIL, TP2, INVALIDATED_LOSS, INVALIDATED_PROFIT,
    TIME_TP, TIME_SL.
    Excludes FLOAT and blank/unrecognised outcomes.
    """
    return [r for r in rows if is_closed(r)]


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
    """
    Win rate and expectancy on closed trades.
    A trade counts as a win if its outcome is in WIN_OUTCOMES
    (TP1_TRAIL, TP2, INVALIDATED_PROFIT, TIME_TP) and a loss if in
    LOSS_OUTCOMES (SL, INVALIDATED_LOSS, TIME_SL). FLOAT is excluded
    upstream by get_closed().
    """
    if not closed:
        return {"n": 0}

    wins = [r for r in closed if is_win(r)]
    losses = [r for r in closed if is_loss(r)]

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


def analyse_outcome_breakdown(closed: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Per-category breakdown across all 8 outcome states (closed only).
    Useful to see e.g. how much of the win bucket is TP1_TRAIL vs TP2
    vs INVALIDATED_PROFIT vs TIME_TP, rather than just a flat win/loss
    number.
    """
    categories = [
        "SL", "TP1_TRAIL", "TP2", "INVALIDATED_LOSS", "INVALIDATED_PROFIT",
        "TIME_TP", "TIME_SL",
    ]
    out = {}
    for cat in categories:
        cat_rows = [r for r in closed if _outcome(r) == cat]
        r_vals = [r["_shadow_r_realized"] for r in cat_rows if r.get("_shadow_r_realized") is not None]
        out[cat] = {
            "n": len(cat_rows),
            "avg_r": (sum(r_vals) / len(r_vals)) if r_vals else None,
        }
    return out


def analyse_f6_f7_vs_outcome(rows_with_outcome: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Split outcomes by F6 (EMA9 extension) and F7 10d (RS gap) breach status.
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


def analyse_rs_lookback_comparison(rows_with_outcome: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    NEW (F7 lookback review) — compares predictive power of the 10d
    (current F7 gate), 60d, and 126d RS gaps against closed-trade outcomes.

    No formal hard threshold exists yet for the 60d/126d windows (they
    were added as shadow-only context, not gates), so this uses a simple
    sign split — RS gap negative (underperforming SPY) vs non-negative —
    as a first-pass comparison. This is intentionally simple: the goal
    at this stage is just to see whether ANY of the three windows shows
    a meaningfully different win rate between its two groups. Whichever
    window shows the clearest separation is the one worth formalising
    into a real threshold at the next filter review.
    """
    def _split_by_sign(field: str) -> Dict[str, Any]:
        neg = [r for r in rows_with_outcome
               if r.get(field) is not None and r[field] < 0]
        nonneg = [r for r in rows_with_outcome
                  if r.get(field) is not None and r[field] >= 0]
        return {
            "negative": _outcome_summary(neg),
            "non_negative": _outcome_summary(nonneg),
        }

    return {
        "rs_10d": _split_by_sign("_shadow_rs_gap_vs_spy"),
        "rs_60d": _split_by_sign("_shadow_rs_60d_gap"),
        "rs_126d": _split_by_sign("_shadow_rs_126d_gap"),
    }


def _outcome_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact win rate + avg R for a subset of rows."""
    if not rows:
        return {"n": 0, "closed_n": 0, "win_rate_pct": None, "avg_r": None}
    closed = get_closed(rows)
    wins = [r for r in closed if is_win(r)]
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
          f"{'GAP%':>6} {'EXT':>6} {'RS10':>7} "
          f"{'FILL':>5} {'OUTCOME':<18} {'R':>6}")
    print(DIVIDER)

    for r in rows:
        date = str(r.get("_shadow_date", ""))[:10]
        ticker = r.get("_shadow_ticker", "")
        close = r.get("_shadow_close_price")
        ema9 = r.get("_shadow_entry_ema9")
        gap = r.get("_shadow_ema9_gap_pct")
        ext = r.get("_shadow_ema9_ext_atr")
        rs = r.get("_shadow_rs_gap_vs_spy")
        filled = r.get("_shadow_ema9_filled")
        outcome = _outcome(r) or "—"
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
              f"{fill_str:>5} {outcome:<18} {r_str:>6}")


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


def print_outcomes_section(analysis: Dict[str, Any], breakdown: Dict[str, Dict[str, Any]]) -> None:
    print(f"\n{'CLOSED TRADE OUTCOMES':}")
    print(DIVIDER)
    n = analysis.get("n", 0)
    if n == 0:
        print("  No closed trades yet — fill _shadow_outcome in CSV/Sheet")
        print("  Valid values: SL, TP1_TRAIL, TP2, INVALIDATED_LOSS,")
        print("                INVALIDATED_PROFIT, FLOAT, TIME_TP, TIME_SL")
        return
    print(f"  Closed trades : {n}")
    print(f"  Wins          : {analysis['wins']}  (TP1_TRAIL + TP2 + INVALIDATED_PROFIT + TIME_TP)")
    print(f"  Losses        : {analysis['losses']}  (SL + INVALIDATED_LOSS + TIME_SL)")
    print(f"  Win rate      : {_pct(analysis['win_rate_pct'])}")
    print(f"  Expectancy    : {_r(analysis['expectancy_r'])} per trade")
    print(f"  Net R         : {_r(analysis['net_r'])}")
    print()
    print(f"  Breakeven WR  : 25.0% (for +3R/−1R profile)")
    print()
    print("  Breakdown by category:")
    for cat, s in breakdown.items():
        n_cat = s["n"]
        avg_r = _r(s["avg_r"])
        print(f"    {cat:<20} n={n_cat:<4} avgR={avg_r}")


def print_soft_warning_section(analysis: Dict[str, Any]) -> None:
    print(f"\n{'SOFT WARNING PREDICTIVE VALUE (F6 / F7 10d)':}")
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
    _row(f"F7 10d RS gap < {F7_RS_THRESHOLD}%  (breached)", analysis["f7_breached"])
    _row(f"F7 10d RS gap ≥ {F7_RS_THRESHOLD}%  (ok)",      analysis["f7_ok"])
    print()
    print("  Interpretation: if breached WR << ok WR → thresholds are valid")
    print("  If no difference → consider loosening thresholds in config.py")


def print_rs_lookback_comparison(analysis: Dict[str, Any]) -> None:
    """
    NEW — the actual point of the F7 lookback review: compare 10d vs 60d
    vs 126d RS gap as predictors of trade outcome, side by side.
    """
    print(f"\n{'F7 LOOKBACK COMPARISON: 10d vs 60d vs 126d RS GAP':}")
    print(DIVIDER)
    print("  Jegadeesh (1990): 10d sits in the short-term REVERSAL zone")
    print("  Jegadeesh & Titman (1993): 60d/126d sit in the MOMENTUM zone (skip 21d)")
    print()

    def _row(label: str, s: Dict[str, Any]) -> None:
        n = s.get("n", 0)
        closed = s.get("closed_n", 0)
        wr = _pct(s.get("win_rate_pct"))
        avg = _r(s.get("avg_r"))
        print(f"  {label:<30} n={n:<4} closed={closed:<4} WR={wr:<8} avgR={avg}")

    print(f"  {'Group':<30} {'n':<6} {'closed':<10} {'WR':<9} {'avgR'}")
    print(f"  {'-'*60}")

    for window_key, window_label in [
        ("rs_10d", "RS 10d (current F7 gate)"),
        ("rs_60d", "RS 60d (momentum)"),
        ("rs_126d", "RS 126d (momentum, robust)"),
    ]:
        w = analysis.get(window_key, {})
        print(f"  -- {window_label} --")
        _row("  gap < 0 (underperforming)", w.get("negative", {}))
        _row("  gap ≥ 0 (outperforming)", w.get("non_negative", {}))
        print()

    print("  Interpretation: look for the window where the two groups'")
    print("  win rates diverge the MOST. If 60d or 126d separates win/loss")
    print("  better than 10d, that is evidence F7 should be re-pointed to")
    print("  a longer lookback. If 10d already separates cleanly while")
    print("  60d/126d don't, the current 10d gate may be fine as-is.")
    print("  NOTE: minimum ~10 observations per group before drawing any")
    print("  conclusion — early Batch 3 data will likely be too thin here.")


def print_data_quality_note(rows: List[Dict[str, Any]]) -> None:
    """Warn if too many manual fields are still blank, or outcome values
    don't match one of the 8 recognised categories."""
    total = len(rows)
    if total == 0:
        return

    valid_categories = CLOSED_OUTCOMES | OPEN_OUTCOMES
    blank_outcome = 0
    unrecognised_outcome = []
    blank_filled = sum(1 for r in rows if r.get("_shadow_ema9_filled") is None)

    for r in rows:
        o = _outcome(r)
        if not o:
            blank_outcome += 1
        elif o not in valid_categories:
            unrecognised_outcome.append((r.get("_shadow_ticker", "?"), o))

    if blank_outcome > 0 or blank_filled > 0 or unrecognised_outcome:
        print(f"\n  ⚠  DATA QUALITY: {blank_outcome}/{total} rows missing outcome, "
              f"{blank_filled}/{total} missing ema9_filled")
        if unrecognised_outcome:
            print(f"     {len(unrecognised_outcome)} row(s) have an outcome value "
                  f"that doesn't match any known category:")
            for ticker, val in unrecognised_outcome[:10]:
                print(f"       {ticker}: '{val}'")
            print(f"     Valid values: {', '.join(sorted(valid_categories))}")
        print("     Fill these in shadow_log.csv / the Sheet after each trade closes.")


# ============================================================
# MAIN
# ============================================================

# ============================================================
# PHASE 2.6 — ANALYSES
# ============================================================

def _action(row: Dict[str, Any]) -> str:
    return str(row.get("_shadow_discretionary_action", "") or "").strip().upper()


def _entry_type(row: Dict[str, Any]) -> str:
    return str(row.get("_shadow_entry_type", "") or "").strip().lower()


def get_taken(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Rows the operator actually executed.

    A BLANK action counts as TAKEN. Rationale: blank is the default state
    of a fresh row, and treating it as "not taken" would drop most of the
    batch on the first day someone forgets to annotate. A veto is a
    deliberate act and must be recorded deliberately.
    """
    return [r for r in rows if _action(r) not in VETOED_ACTIONS]


def analyse_entry_type(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Strict (EMA9 limit filled) vs fallback (two-stage near-miss executed at
    the next open). This is the out-of-sample test of
    ENTRY2_ATR_TOLERANCE = 0.15.

    Batch 3 motivation: unfilled EMA9 orders went on to reach TP1 at a
    significantly higher rate than filled ones (Fisher exact p ~ 0.007 on
    8/8). The limit was filling on weakness and missing the strongest
    continuations. If that holds, fallback entries should show a BETTER
    win rate than strict ones despite the worse entry price.

    Do NOT retune the tolerance from these numbers — same batch, same fit.
    """
    out = {"strict": {"n": 0, "wins": 0, "r_sum": 0.0},
           "fallback": {"n": 0, "wins": 0, "r_sum": 0.0},
           "unknown": {"n": 0, "wins": 0, "r_sum": 0.0}}
    for row in closed:
        et = _entry_type(row)
        bucket = out.get(et, out["unknown"])
        r = row.get("_shadow_r_realized")
        bucket["n"] += 1
        if is_win(row):
            bucket["wins"] += 1
        if r is not None:
            bucket["r_sum"] += float(r)
    for k, b in out.items():
        b["win_rate"] = (b["wins"] / b["n"] * 100) if b["n"] else None
        b["avg_r"] = (b["r_sum"] / b["n"]) if b["n"] else None
    return out


def analyse_mfe_giveback(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The section Batch 3 could not produce, and the one that decides what
    gets fixed next.

    give_back = mfe_r - r_realized. Split by how far the trade actually
    travelled and when it peaked:

      tp1_tagged     mfe >= 2.0R. TP1 was reachable. If the outcome is not
                     TP1_TRAIL/TP2, this is an EXECUTION failure — the
                     order was not placed or did not trigger — not a
                     strategy result. NTAP in Batch 3 (mfe exactly 2.00R,
                     outcome TIME_TP) is the known example.
      peaked_early   1.0-2.0R, peak on/before day 5. Rose, then handed it
                     back. The exit is too late.
      peaked_late    1.0-2.0R, peak on/after day 8. Still rising when the
                     hold limit cut it off. The hold is too short.
      went_nowhere   mfe < 1.0R. Never moved. This is a SELECTION problem,
                     and no change to hold period or trailing rule can fix
                     it. In Phase 2.1 this described 12 of 13 losers.

    If went_nowhere dominates, do not touch the exit logic — the answer is
    upstream in what gets bought.
    """
    buckets = {"tp1_tagged": [], "peaked_early": [], "peaked_late": [],
               "peaked_mid": [], "went_nowhere": []}
    missing = 0
    for row in closed:
        mfe = row.get("_shadow_mfe_r")
        if mfe is None:
            missing += 1
            continue
        day = row.get("_shadow_days_to_mfe")
        r = row.get("_shadow_r_realized")
        entry = {
            "ticker": row.get("_shadow_ticker", "?"),
            "outcome": _outcome(row),
            "mfe_r": float(mfe),
            "r": float(r) if r is not None else None,
            "day": day,
            "give_back": (float(mfe) - float(r)) if r is not None else None,
        }
        if float(mfe) >= MFE_TP1_TAGGED_R:
            buckets["tp1_tagged"].append(entry)
        elif float(mfe) < MFE_WENT_NOWHERE_R:
            buckets["went_nowhere"].append(entry)
        elif day is not None and day <= MFE_PEAK_EARLY_DAY:
            buckets["peaked_early"].append(entry)
        elif day is not None and day >= MFE_PEAK_LATE_DAY:
            buckets["peaked_late"].append(entry)
        else:
            buckets["peaked_mid"].append(entry)

    # Execution bug detector: TP1 was reachable but the outcome says it
    # was never tagged. This is the one finding here that is actionable
    # immediately, without waiting for a larger sample.
    exec_bugs = [e for e in buckets["tp1_tagged"]
                 if e["outcome"] not in {"TP1_TRAIL", "TP2"}]
    return {"buckets": buckets, "missing_mfe": missing, "exec_bugs": exec_bugs,
            "n_with_mfe": sum(len(v) for v in buckets.values())}


def analyse_discretionary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Score the operator's manual filtering against what actually happened.

    Batch 4 is manually filtered, so a single blended number cannot
    distinguish "the system picked well" from "the operator removed the
    bad ones". Splitting them makes both answerable:

      TAKEN  -> the strategy as actually executed
      VETOED -> the operator's judgement, scored against the outcome the
                trade would have had

    A vetoed row only carries information if its outcome was recorded
    ANYWAY (i.e. tracked on paper after being skipped). Vetoed rows with
    no outcome are counted but cannot be scored — and are worth chasing,
    because they are precisely where the operator's edge would show up.
    """
    taken, vetoed, vetoed_scored = [], [], []
    for row in rows:
        act = _action(row)
        if act in VETOED_ACTIONS:
            vetoed.append(row)
            if is_closed(row):
                vetoed_scored.append(row)
        else:
            taken.append(row)

    def _summ(rs):
        cl = [r for r in rs if is_closed(r)]
        wins = [r for r in cl if is_win(r)]
        rvals = [float(r["_shadow_r_realized"]) for r in cl
                 if r.get("_shadow_r_realized") is not None]
        return {
            "n": len(rs), "closed": len(cl), "wins": len(wins),
            "win_rate": (len(wins) / len(cl) * 100) if cl else None,
            "avg_r": (sum(rvals) / len(rvals)) if rvals else None,
            "total_r": sum(rvals) if rvals else 0.0,
        }

    reasons: Dict[str, int] = {}
    for row in vetoed:
        reason = str(row.get("_shadow_veto_reason", "") or "").strip() or "(no reason given)"
        reasons[reason] = reasons.get(reason, 0) + 1

    return {"taken": _summ(taken), "vetoed": _summ(vetoed),
            "vetoed_unscored": len(vetoed) - len(vetoed_scored),
            "reasons": reasons}


def analyse_dollars(closed: List[Dict[str, Any]], start_equity: float = 100.0) -> Dict[str, Any]:
    """
    Reconstruct realised P&L in dollars.

    With floating 2% risk there is no single R-to-dollar multiplier any
    more: each row must be converted with its OWN risk_usd. This mirrors
    exactly what sheets_logger.read_equity() does, so a disagreement
    between this number and the broker balance points at the data, not at
    the arithmetic.

    missing_risk counts closed rows with an r_realized but no risk_usd.
    Those rows are INVISIBLE to the equity calculation — every trade sized
    after them uses a balance that is wrong by their P&L, silently.
    """
    total = 0.0
    counted = 0
    missing_risk = []
    for row in closed:
        r = row.get("_shadow_r_realized")
        risk = row.get("_shadow_risk_usd")
        if r is None:
            continue
        if risk is None:
            missing_risk.append(row.get("_shadow_ticker", "?"))
            continue
        total += float(r) * float(risk)
        counted += 1
    return {"realized_usd": total, "counted": counted,
            "missing_risk": missing_risk,
            "start_equity": start_equity,
            "end_equity": start_equity + total,
            "return_pct": (total / start_equity * 100) if start_equity else None}


def analyse_force_closed(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Separate trades ended by an administrative batch cutoff from those that
    ended by their own rule.

    Force-closed trades are real and stay in the totals, but their outcome
    was truncated by the calendar rather than by the strategy — so a batch
    that ends mid-position distorts the distribution in a way that has
    nothing to do with edge.
    """
    forced = [r for r in closed if r.get("_shadow_force_closed") is True]
    natural = [r for r in closed if r.get("_shadow_force_closed") is not True]

    def _summ(rs):
        rvals = [float(r["_shadow_r_realized"]) for r in rs
                 if r.get("_shadow_r_realized") is not None]
        wins = [r for r in rs if is_win(r)]
        return {"n": len(rs), "wins": len(wins),
                "win_rate": (len(wins) / len(rs) * 100) if rs else None,
                "avg_r": (sum(rvals) / len(rvals)) if rvals else None}

    return {"forced": _summ(forced), "natural": _summ(natural)}


# ============================================================
# PHASE 2.6 — PRINTERS
# ============================================================

def print_entry_type_section(a: Dict[str, Any]) -> None:
    print(f"\n{DIVIDER}")
    print("  ENTRY TYPE — strict vs fallback (two-stage entry)")
    print(f"{DIVIDER}")
    print(f"  {'type':<12}{'n':>5}{'wins':>7}{'win %':>9}{'avg R':>9}")
    for key in ("strict", "fallback", "unknown"):
        b = a[key]
        if b["n"] == 0:
            continue
        print(f"  {key:<12}{b['n']:>5}{b['wins']:>7}"
              f"{_pct(b['win_rate']):>9}{_r(b['avg_r']):>9}")
    if a["fallback"]["n"] == 0:
        print("\n  No fallback entries yet — ENTRY2_ATR_TOLERANCE untested.")
    elif a["fallback"]["n"] < 5:
        print(f"\n  Only {a['fallback']['n']} fallback entries — far too few to")
        print("  judge the 0.15 tolerance. Read as direction, not evidence.")
    print("\n  NOTE: 0.15 was fitted in-sample on 5 Batch 3 near-misses.")
    print("  Do not retune it from this batch's own numbers.")


def print_mfe_section(a: Dict[str, Any]) -> None:
    print(f"\n{DIVIDER}")
    print("  MFE GIVE-BACK — selection problem or exit problem?")
    print(f"{DIVIDER}")
    if a["n_with_mfe"] == 0:
        print(f"  No mfe_r data ({a['missing_mfe']} closed rows missing it).")
        print("  This is the single highest-value column in the log — it is the")
        print("  only way to tell 'never went anywhere' (fix selection) apart")
        print("  from 'went up then gave it back' (fix the exit). Batch 3 could")
        print("  not answer that question at all. Fill mfe_r and days_to_mfe.")
        return

    labels = {
        "tp1_tagged":   f"TP1 tagged (mfe >= {MFE_TP1_TAGGED_R}R)",
        "peaked_early": f"Peaked early (<= day {MFE_PEAK_EARLY_DAY}) -> exit too late",
        "peaked_mid":   "Peaked mid-hold",
        "peaked_late":  f"Peaked late (>= day {MFE_PEAK_LATE_DAY}) -> hold too short",
        "went_nowhere": f"Never moved (mfe < {MFE_WENT_NOWHERE_R}R) -> SELECTION",
    }
    total = a["n_with_mfe"]
    for key, label in labels.items():
        rows = a["buckets"][key]
        if not rows:
            continue
        gb = [e["give_back"] for e in rows if e["give_back"] is not None]
        avg_gb = (sum(gb) / len(gb)) if gb else None
        share = len(rows) / total * 100
        print(f"\n  {label}")
        print(f"    n={len(rows)} ({share:.0f}%)  avg give-back {_r(avg_gb)}")
        for e in rows[:6]:
            day_txt = f"d{e['day']}" if e["day"] is not None else "d?"
            print(f"      {e['ticker']:<6} {e['outcome']:<18} "
                  f"mfe {e['mfe_r']:.2f}R @{day_txt} -> {_r(e['r'])}")

    if a["exec_bugs"]:
        print(f"\n  EXECUTION BUG — TP1 was reachable but never tagged:")
        for e in a["exec_bugs"]:
            print(f"      {e['ticker']:<6} mfe {e['mfe_r']:.2f}R "
                  f"but outcome = {e['outcome']}")
        print("    TP1 order was not placed or did not trigger. This is a")
        print("    process failure, not a strategy result — fix it directly.")

    nowhere = len(a["buckets"]["went_nowhere"])
    if total and nowhere / total >= 0.5:
        print(f"\n  >>> {nowhere}/{total} trades never reached "
              f"{MFE_WENT_NOWHERE_R}R.")
        print("      This is a SELECTION problem. Changing hold period or")
        print("      trailing rules cannot fix trades that never moved.")
    if a["missing_mfe"]:
        print(f"\n  {a['missing_mfe']} closed row(s) still missing mfe_r.")


def print_discretionary_section(a: Dict[str, Any]) -> None:
    print(f"\n{DIVIDER}")
    print("  DISCRETIONARY ATTRIBUTION — system vs operator")
    print(f"{DIVIDER}")
    print(f"  {'group':<10}{'n':>5}{'closed':>8}{'win %':>9}{'avg R':>9}{'total R':>10}")
    for key in ("taken", "vetoed"):
        b = a[key]
        print(f"  {key:<10}{b['n']:>5}{b['closed']:>8}"
              f"{_pct(b['win_rate']):>9}{_r(b['avg_r']):>9}{_r(b['total_r']):>10}")

    if a["vetoed"]["closed"] == 0:
        print("\n  No vetoed trades have a recorded outcome, so the vetoes")
        print("  cannot be scored. Tracking skipped signals on paper is what")
        print("  turns a veto from an opinion into evidence.")
    else:
        t_avg = a["taken"]["avg_r"]
        v_avg = a["vetoed"]["avg_r"]
        if t_avg is not None and v_avg is not None:
            if v_avg < t_avg:
                print(f"\n  Vetoed avg {_r(v_avg)} < taken avg {_r(t_avg)} —")
                print("  vetoes removed weaker trades. Worth examining whether")
                print("  the reasons can be expressed as a rule.")
            else:
                print(f"\n  Vetoed avg {_r(v_avg)} >= taken avg {_r(t_avg)} —")
                print("  the vetoes removed trades that would have done as well")
                print("  or better. Worth knowing early.")
    if a["vetoed_unscored"]:
        print(f"\n  {a['vetoed_unscored']} vetoed row(s) have no outcome recorded.")
    if a["reasons"]:
        print("\n  Veto reasons:")
        for reason, n in sorted(a["reasons"].items(), key=lambda x: -x[1]):
            print(f"    {n:>3}x  {reason}")


def print_dollars_section(a: Dict[str, Any]) -> None:
    print(f"\n{DIVIDER}")
    print("  DOLLAR RECONCILIATION (floating 2% risk)")
    print(f"{DIVIDER}")
    print(f"  Rows converted:   {a['counted']}")
    print(f"  Start equity:     ${a['start_equity']:.2f}")
    print(f"  Realised P&L:     ${a['realized_usd']:+.2f}")
    print(f"  Implied equity:   ${a['end_equity']:.2f}  ({a['return_pct']:+.2f}%)")
    print("\n  Computed the same way sheets_logger.read_equity() computes it:")
    print("  sum(r_realized x risk_usd) per row. If this disagrees with the")
    print("  broker balance, the data is wrong, not the arithmetic.")
    print("  NOTE: gross of fees. Fee drag runs ~0.14-0.16R per trade,")
    print("  roughly 45% of gross expectancy at Batch 3 levels.")
    if a["missing_risk"]:
        print(f"\n  {len(a['missing_risk'])} closed row(s) have NO risk_usd:")
        print(f"    {', '.join(a['missing_risk'][:10])}")
        print("    These are invisible to the running-equity calculation —")
        print("    every trade sized after them used a wrong balance.")


def print_force_closed_section(a: Dict[str, Any]) -> None:
    if a["forced"]["n"] == 0:
        return
    print(f"\n{DIVIDER}")
    print("  FORCE-CLOSED vs NATURAL EXIT")
    print(f"{DIVIDER}")
    print(f"  {'group':<10}{'n':>5}{'wins':>7}{'win %':>9}{'avg R':>9}")
    for key in ("natural", "forced"):
        b = a[key]
        print(f"  {key:<10}{b['n']:>5}{b['wins']:>7}"
              f"{_pct(b['win_rate']):>9}{_r(b['avg_r']):>9}")
    print("\n  Force-closed trades ended on a calendar cutoff, not on their")
    print("  own rule. They stay in the totals but their outcomes are")
    print("  truncated for reasons unrelated to the strategy.")


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
    parser.add_argument(
        "--exclude-vetoed",
        action="store_true",
        help="Phase 2.6: restrict every analysis to discretionary_action == TAKEN",
    )
    parser.add_argument(
        "--start-equity",
        type=float,
        default=100.0,
        metavar="USD",
        help="Starting equity for dollar reconciliation (default: 100)",
    )
    args = parser.parse_args()

    # ── Load ──────────────────────────────────────────────────
    rows = load_shadow_log(args.csv)
    if not rows:
        sys.exit(1)

    # ── Phase 2.6: discretionary filter ───────────────────────
    # Applied BEFORE every other analysis so "how did the executed
    # strategy do" is answered on executed trades only.
    if args.exclude_vetoed:
        before = len(rows)
        rows = get_taken(rows)
        print(f"\n[FILTER] --exclude-vetoed: {before} -> {len(rows)} rows "
              f"({before - len(rows)} vetoed excluded)")

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
        outcome_breakdown = analyse_outcome_breakdown(closed)
        soft_analysis = analyse_f6_f7_vs_outcome(rows_with_outcome)
        rs_lookback_analysis = analyse_rs_lookback_comparison(rows_with_outcome)

        print_fill_rate_section(fill_analysis)
        print_entry_improvement_section(entry_analysis)
        print_outcomes_section(outcome_analysis, outcome_breakdown)
        print_soft_warning_section(soft_analysis)
        print_rs_lookback_comparison(rs_lookback_analysis)

        # ── Phase 2.6 sections ────────────────────────────────
        # Ordered by decision value: MFE first (it decides WHAT to fix
        # next), then entry type, attribution, dollars, force-closed.
        print_mfe_section(analyse_mfe_giveback(closed))
        print_entry_type_section(analyse_entry_type(closed))
        if not args.exclude_vetoed:
            print_discretionary_section(analyse_discretionary(rows))
        print_dollars_section(analyse_dollars(closed, args.start_equity))
        print_force_closed_section(analyse_force_closed(closed))

        print_data_quality_note(rows)

    print(f"\n{DIVIDER}\n")


if __name__ == "__main__":
    main()
