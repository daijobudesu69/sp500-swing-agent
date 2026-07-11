"""
filter_correlation.py — OFFLINE filter-redundancy diagnostic for SP500 Swing Agent.

SESSION 1 (Linear Algebra) — answers ONE question:
  Are the 7 mandatory/soft filters (F1-F7) actually measuring 7 independent
  things, or are 2-3 of them relabelled versions of the same signal?

WHY THIS IS A STANDALONE OFFLINE SCRIPT (read before running):
  - It does NOT touch the live pipeline, the shadow log, or any config gate.
    It only READS: universe.py (ticker list), data_fetcher.py (OHLCV),
    indicators.py (compute_indicators). Nothing is written back anywhere.
  - It is therefore 100% safe to run at any time during Batch 3 config-freeze.
    Running it changes nothing the pipeline depends on.
  - It deliberately does NOT read shadow_log.csv. The shadow log only
    contains signals that ALREADY passed F1-F5 (hard gates), so every row
    is range-restricted (ADX>25, RSI>45, vol>1.3x, ...). Correlations
    computed on that restricted set are biased toward zero / distorted —
    they cannot answer "are the filters redundant?" honestly. This script
    recomputes across the FULL universe (unrestricted) precisely to avoid
    that selection bias, and also prints the restricted (survivor) subset
    side by side so you can SEE the distortion for yourself.

WHAT IT COMPUTES:
  1. Pearson + Spearman correlation matrix across 11 continuous filter drivers.
     (Spearman catches monotonic-but-nonlinear coupling that Pearson misses —
      financial cross-sections are skewed, so this matters.)
  2. PCA (eigendecomposition of the correlation matrix): how many principal
     components explain ~90% of the variance across all drivers. If 2-3 PCs
     suffice for 11 variables, the "7 filters" are partly an illusion.
  3. FULL universe vs SURVIVOR subset (F1-F5 pass) — the contrast is the
     visual proof of why the shadow log is the wrong tool for this question.

MECHANICAL vs EMPIRICAL (important honesty guard):
  Some pairs MUST correlate because of how they are defined, not because of
  any interesting market fact. Example: ema9_ext_atr = (price-EMA9)/ATR and
  atr_pct = ATR/price both contain ATR, so they are coupled by construction.
  Claiming "ADX and ATR are redundant!" when the coupling is a formula
  artifact would be a false finding. This script tags each suspicious pair as
  MECHANICAL (formula artifact — ignore) or CONCEPTUAL (same idea, different
  window/normalisation — worth a look) so genuine empirical redundancy is not
  confused with algebraic tautology.

VARIABLES (11 continuous drivers, one per filter dimension + MACD bonus):
  ema50_dist_pct   F1  (price - EMA50) / price * 100
  adx_14           F2  ADX(14)
  di_spread        F2  (+DI) - (-DI)          [direction strength]
  rsi_14           F3  RSI(14)
  vol_ratio_14d    F4  volume / 14d avg
  atr_pct          F5  ATR(14) / price * 100
  ema9_ext_atr     F6  (price - EMA9) / ATR   [soft]
  rs_10d_gap       F7  stock 10d ret - SPY 10d ret   [current gate]
  rs_60d_gap       F7  60d ret gap, skip-21 momentum  [shadow]
  rs_126d_gap      F7  126d ret gap, skip-21 momentum [shadow]
  macd_histogram   confluence bonus — is MACD adding info or echoing filters?

USAGE:
  python filter_correlation.py                 # full run (default 1y history)
  python filter_correlation.py --limit 50      # quick test on first 50 tickers
  python filter_correlation.py --min-tickers 100   # abort if fewer usable rows
  python filter_correlation.py --survivors-only    # skip full-universe pass

DEPENDENCIES: numpy + pandas only (same as the repo). No scipy / sklearn.
  Spearman via pandas .corr(method="spearman"); PCA via numpy.linalg.eigh.

OFFLINE — Session 1 of the quant upgrade. Zero pipeline risk.
"""
import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src import config
from src import universe as univ_mod
from src import data_fetcher as data_mod
from src import indicators as ind_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("filter_corr")


# ============================================================
# CONSTANTS
# ============================================================

# The 11 continuous drivers, in display order. Keeping an explicit ordered
# list (not just dict keys) so the correlation matrix rows/cols are stable.
FEATURE_ORDER = [
    "ema50_dist_pct",   # F1
    "adx_14",           # F2
    "di_spread",        # F2 direction
    "rsi_14",           # F3
    "vol_ratio_14d",    # F4
    "atr_pct",          # F5
    "ema9_ext_atr",     # F6
    "rs_10d_gap",       # F7 (gate)
    "rs_60d_gap",       # F7 shadow
    "rs_126d_gap",      # F7 shadow
    "macd_histogram",   # confluence bonus
]

# Pairs coupled by FORMULA (share a term) — correlation here is an artifact,
# not a finding. Frozenset so (a,b) == (b,a).
MECHANICAL_PAIRS = {
    frozenset(("ema9_ext_atr", "atr_pct")):
        "both contain ATR (ext = dist/ATR, atr_pct = ATR/price) — coupled by construction",
    frozenset(("adx_14", "di_spread")):
        "ADX is derived from +DI/-DI — partially mechanical (ADX = smoothed |DI diff| / DI sum)",
}

# Pairs measuring the SAME IDEA via different window/normalisation — NOT a
# formula artifact, but expected to correlate; redundancy here is a real
# (and actionable) design question, not a tautology.
CONCEPTUAL_PAIRS = {
    frozenset(("ema50_dist_pct", "ema9_ext_atr")):
        "both are 'distance of price above a moving average' (EMA50 vs EMA9)",
    frozenset(("rs_10d_gap", "rs_60d_gap")):
        "same RS-vs-SPY idea, different lookback (10d vs 60d)",
    frozenset(("rs_10d_gap", "rs_126d_gap")):
        "same RS-vs-SPY idea, different lookback (10d vs 126d)",
    frozenset(("rs_60d_gap", "rs_126d_gap")):
        "same RS-vs-SPY idea, different lookback (60d vs 126d)",
}

# Correlation magnitude above which a pair is worth calling out.
HIGH_CORR_THRESHOLD = 0.70


# ============================================================
# RS GAP HELPERS — copied VERBATIM from main.py so the correlation
# uses byte-identical definitions to the live F7 pipeline. Do NOT
# "improve" these here — they must mirror main.py exactly.
# ============================================================

def _compute_return_pct(close_series: pd.Series, lookback_days: int,
                        skip_recent_days: int = 0) -> Optional[float]:
    """% return over lookback, optionally skipping the most recent N days
    (skip-21 momentum convention). Mirrors main.py._compute_return_pct."""
    end_idx = -(skip_recent_days + 1) if skip_recent_days > 0 else -1
    start_idx = -(lookback_days + skip_recent_days + 1)
    if len(close_series) < abs(start_idx):
        return None
    end_price = float(close_series.iloc[end_idx])
    start_price = float(close_series.iloc[start_idx])
    if start_price == 0:
        return None
    return ((end_price - start_price) / start_price) * 100


def _rs_gaps(stock_close: pd.Series, spy_close: pd.Series,
             spy_10d_return: float) -> Dict[str, Optional[float]]:
    """Compute all three RS gaps (10d gate + 60d/126d shadow) for one stock,
    using the same skip-21 convention and the same SPY series as main.py."""
    skip = getattr(config, "RS_SKIP_RECENT_DAYS", 21)
    lb10 = getattr(config, "RS_LOOKBACK_DAYS", 10)
    lb60 = getattr(config, "RS_60D_LOOKBACK", 60)
    lb126 = getattr(config, "RS_126D_LOOKBACK", 126)

    out: Dict[str, Optional[float]] = {
        "rs_10d_gap": None, "rs_60d_gap": None, "rs_126d_gap": None,
    }

    # 10d — no skip (matches the live gate exactly)
    if len(stock_close) >= lb10 + 1:
        s_end = float(stock_close.iloc[-1])
        s_start = float(stock_close.iloc[-(lb10 + 1)])
        if s_start != 0:
            out["rs_10d_gap"] = round((((s_end - s_start) / s_start) * 100) - spy_10d_return, 2)

    # 60d / 126d — skip-21 momentum convention
    for field, lb in (("rs_60d_gap", lb60), ("rs_126d_gap", lb126)):
        s_ret = _compute_return_pct(stock_close, lb, skip_recent_days=skip)
        spy_ret = _compute_return_pct(spy_close, lb, skip_recent_days=skip)
        if s_ret is not None and spy_ret is not None:
            out[field] = round(s_ret - spy_ret, 2)

    return out


# ============================================================
# FEATURE EXTRACTION
# ============================================================

def build_feature_row(ticker: str, df: pd.DataFrame,
                      spy_close: pd.Series, spy_10d_return: float
                      ) -> Optional[Dict[str, Any]]:
    """
    Extract the 11 continuous drivers for one ticker from a single
    compute_indicators() call, plus the survivor flag (F1-F5 all_pass).
    Returns None if indicators can't be computed (insufficient bars etc).
    """
    result = ind_mod.compute_indicators(df)
    if "error" in result:
        return None

    price = result["price"]
    ind = result["indicators"]
    survivor = result["screener"]["all_pass"]

    if price is None or price <= 0:
        return None

    ema50 = ind.get("ema_50")
    ema50_dist_pct = ((price - ema50) / price * 100) if ema50 else None

    rs = _rs_gaps(df["Close"], spy_close, spy_10d_return)

    return {
        "ticker": ticker,
        "survivor": bool(survivor),
        "ema50_dist_pct": ema50_dist_pct,
        "adx_14": ind.get("adx_14"),
        "di_spread": (ind.get("plus_di", 0) - ind.get("minus_di", 0)),
        "rsi_14": ind.get("rsi_14"),
        "vol_ratio_14d": ind.get("vol_ratio_14d"),
        "atr_pct": ind.get("atr_pct"),
        "ema9_ext_atr": ind.get("ema9_ext_atr"),
        "rs_10d_gap": rs["rs_10d_gap"],
        "rs_60d_gap": rs["rs_60d_gap"],
        "rs_126d_gap": rs["rs_126d_gap"],
        "macd_histogram": ind.get("macd_histogram"),
    }


def collect_features(universe_df: pd.DataFrame, limit: Optional[int]
                     ) -> pd.DataFrame:
    """
    Recompute the 11 drivers across the full universe. Fetches SPY once.
    Returns a DataFrame (one row per usable ticker) with a 'survivor' column.
    """
    # --- SPY once (period 1y to cover 126+21 skip) ---
    spy_close = None
    spy_10d_return = 0.0
    try:
        spy_df = data_mod.fetch_ohlcv("SPY", period="1y", interval="1d")
        if spy_df is not None:
            spy_close = spy_df["Close"]
            lb10 = getattr(config, "RS_LOOKBACK_DAYS", 10)
            if len(spy_close) >= lb10 + 1:
                s_end = float(spy_close.iloc[-1])
                s_start = float(spy_close.iloc[-(lb10 + 1)])
                spy_10d_return = ((s_end - s_start) / s_start) * 100
        logger.info(f"SPY loaded — 10d return {spy_10d_return:+.2f}%")
    except Exception as e:
        logger.warning(f"SPY fetch failed: {e} — RS gaps default toward 0/None")

    if spy_close is None:
        logger.error("No SPY data — RS gap columns will be empty. Aborting.")
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    errors = 0
    tickers = universe_df["ticker"].tolist()
    if limit:
        tickers = tickers[:limit]
    total = len(tickers)
    logger.info(f"Recomputing filter drivers across {total} tickers "
                f"(full universe, unrestricted)...")

    for i, ticker in enumerate(tickers):
        try:
            df = data_mod.fetch_ohlcv(ticker, period="1y", interval="1d")
            if df is None:
                continue
            row = build_feature_row(ticker, df, spy_close, spy_10d_return)
            if row is not None:
                rows.append(row)
        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.warning(f"  {ticker}: {str(e)[:70]}")
        if (i + 1) % 100 == 0:
            logger.info(f"  progress {i + 1}/{total} ({len(rows)} usable rows)")

    if errors:
        logger.warning(f"{errors} tickers errored during fetch/compute")
    logger.info(f"Collected {len(rows)} usable rows "
                f"({sum(r['survivor'] for r in rows)} survivors)")
    return pd.DataFrame(rows)


# ============================================================
# CORRELATION
# ============================================================

def _tag(a: str, b: str) -> str:
    """Return a MECH/CONCEPT tag for a pair, or '' if neither."""
    key = frozenset((a, b))
    if key in MECHANICAL_PAIRS:
        return "MECH"
    if key in CONCEPTUAL_PAIRS:
        return "CONCEPT"
    return ""


def correlation_report(feat: pd.DataFrame, label: str) -> None:
    """Print Pearson + Spearman matrices and a flagged high-correlation list."""
    cols = [c for c in FEATURE_ORDER if c in feat.columns]
    X = feat[cols].apply(pd.to_numeric, errors="coerce")

    n_before = len(X)
    X = X.dropna()
    n_after = len(X)

    print(f"\n{'='*78}")
    print(f"  CORRELATION — {label}")
    print(f"  Rows: {n_after} usable "
          f"({n_before - n_after} dropped for missing values, "
          f"often short-history tickers with no 126d RS gap)")
    print(f"{'='*78}")

    if n_after < 10:
        print("  Too few complete rows (<10) for a meaningful correlation. Skipping.")
        return

    pearson = X.corr(method="pearson")
    spearman = X.corr(method="spearman")

    _print_matrix(pearson, "PEARSON (linear)")
    _print_matrix(spearman, "SPEARMAN (monotonic — catches nonlinear coupling)")

    # High-correlation callouts, tagged
    print(f"\n  HIGH CORRELATIONS (|r| >= {HIGH_CORR_THRESHOLD}) — Pearson & Spearman:")
    print(f"  {'-'*72}")
    found = False
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            pr = pearson.loc[a, b]
            sp = spearman.loc[a, b]
            if abs(pr) >= HIGH_CORR_THRESHOLD or abs(sp) >= HIGH_CORR_THRESHOLD:
                found = True
                tag = _tag(a, b)
                tag_str = ""
                if tag == "MECH":
                    tag_str = f"  ⚙ MECHANICAL — {MECHANICAL_PAIRS[frozenset((a, b))]}"
                elif tag == "CONCEPT":
                    tag_str = f"  ◆ CONCEPTUAL — {CONCEPTUAL_PAIRS[frozenset((a, b))]}"
                else:
                    tag_str = "  ★ EMPIRICAL — no formula/definitional reason; genuine redundancy signal"
                print(f"    {a:<16} ~ {b:<16} "
                      f"Pearson {pr:+.2f} | Spearman {sp:+.2f}{tag_str}")
    if not found:
        print("    (none above threshold — filters look reasonably independent)")

    print("\n  Legend: ⚙ MECHANICAL = formula artifact, ignore. "
          "◆ CONCEPTUAL = same idea diff window, design question. "
          "★ EMPIRICAL = real, unexpected redundancy — investigate.")


def _print_matrix(mat: pd.DataFrame, title: str) -> None:
    print(f"\n  {title}")
    cols = list(mat.columns)
    # short labels for column headers
    short = {c: c[:8] for c in cols}
    header = "  " + " " * 16 + "".join(f"{short[c]:>9}" for c in cols)
    print(header)
    for r in cols:
        line = f"  {r:<16}"
        for c in cols:
            v = mat.loc[r, c]
            line += f"{v:>9.2f}"
        print(line)


# ============================================================
# PCA (numpy eigendecomposition of the correlation matrix)
# ============================================================

def pca_report(feat: pd.DataFrame, label: str) -> None:
    """
    PCA via eigendecomposition of the correlation matrix (z-scored data).
    Reports variance explained per component, how many PCs reach ~90%, and
    the PC1/PC2 loadings so you can SEE which drivers cluster together.
    """
    cols = [c for c in FEATURE_ORDER if c in feat.columns]
    X = feat[cols].apply(pd.to_numeric, errors="coerce").dropna()

    print(f"\n{'='*78}")
    print(f"  PCA — {label}")
    print(f"  Rows: {len(X)}  |  Variables: {len(cols)}")
    print(f"{'='*78}")

    if len(X) < len(cols) + 5:
        print("  Too few rows relative to variables for stable PCA. Skipping.")
        return

    # z-score
    Xv = X.values.astype(float)
    mean = Xv.mean(axis=0)
    std = Xv.std(axis=0, ddof=1)
    std[std == 0] = 1.0
    Z = (Xv - mean) / std

    # correlation matrix + symmetric eigendecomposition
    R = np.corrcoef(Z, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(R)
    order = np.argsort(eigvals)[::-1]
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]

    total = eigvals.sum()
    var_explained = eigvals / total
    cum = np.cumsum(var_explained)

    print("\n  Scree (variance explained per principal component):")
    print(f"  {'PC':<5}{'eigenvalue':>12}{'var %':>10}{'cumulative %':>15}")
    print(f"  {'-'*42}")
    n_for_90 = None
    for i in range(len(eigvals)):
        marker = ""
        if n_for_90 is None and cum[i] >= 0.90:
            n_for_90 = i + 1
            marker = "  <- 90% reached here"
        print(f"  PC{i+1:<4}{eigvals[i]:>12.3f}"
              f"{var_explained[i]*100:>9.1f}%{cum[i]*100:>14.1f}%{marker}")

    print(f"\n  ==> {n_for_90} principal component(s) explain ~90% of variance "
          f"across {len(cols)} filter drivers.")
    if n_for_90 is not None and n_for_90 <= len(cols) - 3:
        print(f"      This means the {len(cols)} drivers effectively live in a "
              f"~{n_for_90}-dimensional space — several filters are redundant "
              f"in an information sense.")
    else:
        print("      The drivers are fairly spread out — limited redundancy.")

    # PC1 / PC2 loadings — which variables move together
    print("\n  PC1 & PC2 loadings (which drivers cluster on each axis):")
    print(f"  {'driver':<16}{'PC1':>9}{'PC2':>9}")
    print(f"  {'-'*34}")
    for j, c in enumerate(cols):
        print(f"  {c:<16}{eigvecs[j,0]:>9.2f}{eigvecs[j,1]:>9.2f}")
    print("\n  Reading it: drivers with large loadings of the SAME sign on PC1")
    print("  move together (candidate redundancy). Opposite signs = they")
    print("  counterbalance. Near-zero on PC1 but large on PC2 = that driver")
    print("  carries independent information the others don't.")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline filter-redundancy diagnostic (Session 1 / Linear Algebra)",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N universe tickers (quick test)")
    parser.add_argument("--min-tickers", type=int, default=30,
                        help="Abort if fewer than N usable rows collected")
    parser.add_argument("--survivors-only", action="store_true",
                        help="Only report the F1-F5 survivor subset (skip full universe)")
    args = parser.parse_args()

    print(f"\n{'#'*78}")
    print("#  SP500 SWING AGENT — FILTER CORRELATION / REDUNDANCY DIAGNOSTIC")
    print("#  Session 1 (Linear Algebra) — OFFLINE, zero pipeline / shadow-log touch")
    print(f"{'#'*78}")

    # --- regime context in the header (single-snapshot caveat) ---
    try:
        ctx = data_mod.fetch_market_context()
        print(f"\n  SNAPSHOT CONTEXT (this run only): "
              f"VIX {ctx.get('vix', '?')} | SPY ${ctx.get('spy_price', '?')} "
              f"| SPY 1d {ctx.get('spy_1d_change_pct', '?')}% "
              f"| above 200MA {ctx.get('spy_above_200ma', '?')}")
        print("  NOTE: correlations below reflect THIS market day. If the day is")
        print("  extreme (big gap / spike), values can be pulled. Re-run on a calm")
        print("  day to confirm, or extend to multi-date if results look regime-bound.")
    except Exception as e:
        logger.warning(f"Could not fetch regime context: {e}")

    # --- universe ---
    try:
        universe_df = univ_mod.get_sp500_universe()
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        sys.exit(1)
    logger.info(f"Universe: {len(universe_df)} tickers")

    # --- collect ---
    feat = collect_features(universe_df, limit=args.limit)
    if len(feat) < args.min_tickers:
        logger.error(f"Only {len(feat)} usable rows — need {args.min_tickers}. Aborting.")
        sys.exit(1)

    survivors = feat[feat["survivor"]].copy()
    print(f"\n  Collected {len(feat)} usable rows | "
          f"{len(survivors)} survivors (passed F1-F5)")

    # --- FULL UNIVERSE (the honest, unrestricted answer) ---
    if not args.survivors_only:
        correlation_report(feat, "FULL UNIVERSE (unrestricted — the honest answer)")
        pca_report(feat, "FULL UNIVERSE")

    # --- SURVIVOR SUBSET (contrast — shows range-restriction distortion) ---
    if len(survivors) >= 10:
        correlation_report(
            survivors,
            "SURVIVOR SUBSET (F1-F5 pass — range-restricted, for CONTRAST only)")
        print("\n  ^ Compare this to the full-universe matrix above. Correlations")
        print("    here are compressed/distorted by range restriction — this is")
        print("    exactly why the shadow log (survivors only) cannot answer the")
        print("    redundancy question. Full universe is the valid basis.")
    else:
        print(f"\n  Survivor subset too small ({len(survivors)} rows) for a "
              f"contrast matrix today — full-universe result stands alone.")

    print(f"\n{'#'*78}")
    print("#  DONE. Interpretation priority: look at ★ EMPIRICAL pairs first")
    print("#  (genuine redundancy), treat ⚙ MECHANICAL as noise, and use the PCA")
    print("#  '90% reached at PC-N' line as the headline redundancy verdict.")
    print(f"{'#'*78}\n")


if __name__ == "__main__":
    main()
