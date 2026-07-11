"""
hurst_lookback_analysis.py — OFFLINE Hurst exponent + OU half-life diagnostic
for SP500 Swing Agent.

SESSION 2 (Stochastic Calculus / Mean Reversion) — answers ONE question:
  Is the S&P 500 universe in a mean-reverting or trending (momentum) regime
  at the 10-day, 60-day, and 126-day horizons? This directly tests whether
  F7's current 10-day RS-vs-SPY lookback sits in the short-term reversal
  zone flagged in PHASE_2_3_FILTER_REVIEW.md (Jegadeesh 1990), or whether
  the 60d/126d shadow-log candidates are structurally better aligned with
  a momentum regime (Jegadeesh & Titman 1993).

WHY THIS IS A STANDALONE OFFLINE SCRIPT (same contract as filter_correlation.py):
  - Reads ONLY universe.py (ticker list) and data_fetcher.py (OHLCV).
    Nothing is written back anywhere — no pipeline, config, or shadow-log touch.
  - Safe to run any time during Batch 3 config-freeze.
  - Deliberately does NOT touch shadow_log.csv or any trade outcome data —
    per the workstream's Session 2 scope, this question is answerable from
    price history alone.

METHOD (per QUANT_UPGRADE_SESSION_1_SUMMARY.md §4, decisions locked with
Dew before this script was written):
  - Single-snapshot (not multi-date/multi-regime) — first-pass, matches
    Session 1's approach.
  - Per-ticker (not pooled) — compute Hurst/OU independently for all ~503
    tickers, then report the DISTRIBUTION per window, not one aggregate
    number. A single pooled Hurst estimate across heterogeneous tickers
    would mix trending and mean-reverting names into a meaningless average.

FOR EACH TICKER, FOR EACH WINDOW (10d / 60d / 126d):
  1. Take the trailing W daily closes.
  2. Hurst exponent via classic Rescaled Range (R/S) analysis on LOG RETURNS
     within that window (not raw price — R/S on a trending price level
     trivially returns H~1 and tells you nothing; returns are the correct
     input). H < 0.5 = mean-reverting, H > 0.5 = trending.
  3. Variance Ratio test (cross-check, same skill) — reported alongside H,
     not as a replacement. VR is more robust than R/S in very short windows.
  4. Half-life via AR(1) regression on the raw price LEVEL series (per
     mean-reversion skill's exact formula) — only meaningful/reported when
     the AR(1) beta is negative (i.e. the window shows mean reversion).

HONESTY GUARD — 10-DAY WINDOW RELIABILITY (read before trusting the 10d row):
  Classic R/S analysis needs several DISTINCT chunk sizes to regress
  log(R/S) vs log(chunk size) reliably. A 10-day window gives only 9 log
  returns — after chunk-size stepping (see hurst_rs()), most tickers will
  have fewer than 3 valid chunk sizes, and the script returns H=None rather
  than fabricate a number off an underpowered regression. This is EXPECTED
  and is itself informative: it is the quantitative reason the 10d window
  is a poor fit for R/S-based analysis in the first place. The Variance
  Ratio test is reported specifically because it degrades more gracefully
  at n~10 and is the primary evidence for the 10d row's regime call — read
  the VR column, not the (mostly missing) H column, for 10d.

USAGE:
  python hurst_lookback_analysis.py                 # full run (1y history)
  python hurst_lookback_analysis.py --limit 50       # quick test, first 50 tickers
  python hurst_lookback_analysis.py --min-tickers 100

RUNTIME NOTE: same cost profile as filter_correlation.py — one fetch_ohlcv()
call per ticker (period="1y"). Do not run in the same window as the 08:00 WIB
daily pipeline (yfinance rate-limit risk). Validate with --limit 50 before a
full-universe run, same pattern as Session 1.

DEPENDENCIES: numpy + pandas only (matches filter_correlation.py — no scipy/
statsmodels; R/S, VR, AR(1) half-life and OU params all implemented from the
formulas in /mnt/skills/user/mean-reversion/SKILL.md, since that skill's own
reference scripts were not present in-repo to import from).

OFFLINE — Session 2 of the quant upgrade. Zero pipeline risk.
"""
import argparse
import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src import universe as univ_mod
from src import data_fetcher as data_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("hurst_lookback")


# ============================================================
# CONSTANTS
# ============================================================

WINDOWS: Dict[str, int] = {"10d": 10, "60d": 60, "126d": 126}
WINDOW_ORDER: List[str] = ["10d", "60d", "126d"]

# Bands around H=0.5 to absorb estimation noise, not a hard cutoff.
MEAN_REVERT_CEILING = 0.45
TREND_FLOOR = 0.55

FETCH_PERIOD = "1y"   # matches filter_correlation.py — ~252 trading days,
                       # enough for the 126d window plus buffer.


# ============================================================
# CORE MATH — R/S Hurst, Variance Ratio, AR(1) half-life, OU params
# Formulas from /mnt/skills/user/mean-reversion/SKILL.md. That skill's own
# reference implementations (references/statistical_tests.md,
# scripts/mean_reversion_test.py) are not present in this environment, so
# these are implemented directly from the skill's documented formulas.
# ============================================================

def hurst_rs(returns: np.ndarray, min_chunk: int = 3) -> Tuple[Optional[float], int]:
    """
    Hurst exponent via classic Rescaled Range (R/S) analysis.

    `returns` must be log returns, NOT prices (see module docstring).
    Splits `returns` into non-overlapping chunks at several sizes
    (min_chunk, ~1.5x, ~2.25x, ...), computes mean R/S per size, regresses
    log(R/S) on log(size). Slope of that regression = H.

    Returns (H, n_sizes_used). n_sizes_used < 3 means the regression could
    not run (returns None for H) — this is the expected, honest outcome for
    very short windows (see module docstring's 10d caveat).
    """
    n = len(returns)
    max_size = n // 2
    sizes: List[int] = []
    size = min_chunk
    while size <= max_size:
        sizes.append(size)
        size = int(size * 1.5) + 1

    rs_means: List[float] = []
    valid_sizes: List[int] = []
    for size in sizes:
        n_chunks = n // size
        chunk_rs = []
        for i in range(n_chunks):
            chunk = returns[i * size:(i + 1) * size]
            mean_adj = chunk - chunk.mean()
            cum_dev = np.cumsum(mean_adj)
            R = cum_dev.max() - cum_dev.min()
            S = chunk.std(ddof=1) if len(chunk) > 1 else 0.0
            if S > 0 and R > 0:
                chunk_rs.append(R / S)
        if chunk_rs:
            rs_means.append(float(np.mean(chunk_rs)))
            valid_sizes.append(size)

    if len(valid_sizes) < 3:
        return None, len(valid_sizes)

    log_sizes = np.log(valid_sizes)
    log_rs = np.log(rs_means)
    slope, _ = np.polyfit(log_sizes, log_rs, 1)
    return float(slope), len(valid_sizes)


def variance_ratio(prices: np.ndarray, q: int) -> Optional[float]:
    """
    Variance Ratio test. VR < 1 = mean-reverting (negative autocorrelation),
    VR = 1 = random walk, VR > 1 = trending. More robust than R/S at small n
    — primary regime evidence for the 10d window.
    """
    if len(prices) < q * 4:
        return None
    log_p = np.log(prices)
    returns = np.diff(log_p)
    var_1 = np.var(returns)
    if var_1 == 0:
        return None
    returns_q = np.diff(log_p[::q])
    if len(returns_q) < 2:
        return None
    var_q = np.var(returns_q)
    return float(var_q / (q * var_1))


def half_life_ar1(prices: np.ndarray) -> Optional[float]:
    """
    Mean-reversion half-life via AR(1) regression on raw price levels
    (formula from SKILL.md — delta_X_t = alpha + beta * X_{t-1}).
    Returns None if beta >= 0 (not mean-reverting — half-life undefined).
    """
    y = np.diff(prices)
    x = prices[:-1]
    X = np.column_stack([np.ones(len(x)), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0][1]
    if beta >= 0:
        return None
    return float(-np.log(2) / np.log(1 + beta))


def estimate_ou_params(prices: np.ndarray, dt: float = 1.0) -> Optional[Dict[str, float]]:
    """OU (theta, mu, sigma, half_life) from the same AR(1) fit (SKILL.md formula)."""
    y = np.diff(prices)
    x = prices[:-1]
    X = np.column_stack([np.ones(len(x)), x])
    alpha, beta = np.linalg.lstsq(X, y, rcond=None)[0]
    if beta >= 0:
        return None
    theta = -np.log(1 + beta) / dt
    mu = -alpha / beta if beta != 0 else float(np.mean(prices))
    residuals = y - (alpha + beta * x)
    denom = 1 - np.exp(-2 * theta * dt)
    sigma = float(np.std(residuals) * np.sqrt(2 * theta / denom)) if denom > 0 else float("nan")
    return {
        "theta": float(theta), "mu": float(mu), "sigma": sigma,
        "half_life": float(np.log(2) / theta) if theta > 0 else None,
    }


# ============================================================
# FEATURE EXTRACTION
# ============================================================

def build_ticker_windows(closes: np.ndarray) -> Dict[str, Optional[Dict[str, Any]]]:
    """For one ticker's close-price array, compute H/VR/half-life per window."""
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for label in WINDOW_ORDER:
        w = WINDOWS[label]
        if len(closes) < w + 1:
            out[label] = None
            continue
        window_prices = closes[-w:]
        log_p = np.log(window_prices)
        rets = np.diff(log_p)

        H, n_sizes = hurst_rs(rets)
        q = max(2, min(5, w // 5))
        vr = variance_ratio(window_prices, q=q)
        hl = half_life_ar1(window_prices)

        out[label] = {"H": H, "n_sizes": n_sizes, "VR": vr, "half_life": hl}
    return out


def collect_windows(universe_df: pd.DataFrame, limit: Optional[int]) -> pd.DataFrame:
    """Loop the universe, fetch OHLCV, compute per-window stats. Flat DataFrame,
    one row per ticker, columns prefixed by window (e.g. h_10d, vr_60d, ...)."""
    tickers = universe_df["ticker"].tolist()
    if limit:
        tickers = tickers[:limit]
    total = len(tickers)
    logger.info(f"Computing Hurst/VR/half-life across {total} tickers "
                f"(windows: {', '.join(WINDOW_ORDER)})...")

    rows: List[Dict[str, Any]] = []
    errors = 0
    for i, ticker in enumerate(tickers):
        try:
            df = data_mod.fetch_ohlcv(ticker, period=FETCH_PERIOD, interval="1d")
            if df is None or len(df) < WINDOWS["10d"] + 1:
                continue
            closes = df["Close"].dropna().values.astype(float)
            windows = build_ticker_windows(closes)

            row: Dict[str, Any] = {"ticker": ticker, "n_bars": len(closes)}
            for label in WINDOW_ORDER:
                w_data = windows[label]
                if w_data is None:
                    row[f"h_{label}"] = None
                    row[f"n_sizes_{label}"] = 0
                    row[f"vr_{label}"] = None
                    row[f"half_life_{label}"] = None
                else:
                    row[f"h_{label}"] = w_data["H"]
                    row[f"n_sizes_{label}"] = w_data["n_sizes"]
                    row[f"vr_{label}"] = w_data["VR"]
                    row[f"half_life_{label}"] = w_data["half_life"]
            rows.append(row)
        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.warning(f"  {ticker}: {str(e)[:70]}")
        if (i + 1) % 100 == 0:
            logger.info(f"  progress {i + 1}/{total} ({len(rows)} usable rows)")

    if errors:
        logger.warning(f"{errors} tickers errored during fetch/compute")
    logger.info(f"Collected {len(rows)} usable rows")
    return pd.DataFrame(rows)


# ============================================================
# REPORTING
# ============================================================

def _classify(h: Optional[float]) -> str:
    if h is None:
        return "N/A"
    if h < MEAN_REVERT_CEILING:
        return "mean-reverting"
    if h > TREND_FLOOR:
        return "trending"
    return "random-walk"


def window_report(df: pd.DataFrame, label: str) -> Dict[str, Any]:
    """Print + return the distribution summary for one window."""
    h_col, n_col, vr_col, hl_col = (f"h_{label}", f"n_sizes_{label}",
                                     f"vr_{label}", f"half_life_{label}")

    h_series = df[h_col].dropna()
    n_series = df[n_col]
    vr_series = df[vr_col].dropna()
    hl_series = df[hl_col].dropna()

    print(f"\n{'='*78}")
    print(f"  WINDOW: {label}")
    print(f"{'='*78}")
    print(f"  Tickers with usable price history for this window: {len(df)}")
    print(f"  Tickers with a computable Hurst exponent (>=3 valid chunk sizes): "
          f"{len(h_series)} ({len(h_series)/max(len(df),1)*100:.0f}%)")
    low_flag = " (LOW — treat H for this window with caution, lean on VR)" if n_series.median() < 4 else ""
    print(f"  Median # chunk sizes used in R/S regression: {n_series.median():.1f}{low_flag}")

    result: Dict[str, Any] = {"window": label}

    if len(h_series) >= 10:
        classes = h_series.apply(_classify)
        pct_mr = (classes == "mean-reverting").mean() * 100
        pct_rw = (classes == "random-walk").mean() * 100
        pct_tr = (classes == "trending").mean() * 100
        print(f"\n  Hurst exponent (R/S) distribution:")
        print(f"    median H = {h_series.median():.3f}  "
              f"(25th {h_series.quantile(.25):.3f} / 75th {h_series.quantile(.75):.3f})")
        print(f"    mean-reverting (H<{MEAN_REVERT_CEILING}): {pct_mr:.0f}%")
        print(f"    random-walk    ({MEAN_REVERT_CEILING}-{TREND_FLOOR}):        {pct_rw:.0f}%")
        print(f"    trending       (H>{TREND_FLOOR}): {pct_tr:.0f}%")
        result.update({"median_H": h_series.median(), "pct_mr": pct_mr,
                        "pct_rw": pct_rw, "pct_trend": pct_tr})
    else:
        print(f"\n  Hurst exponent: insufficient computable rows ({len(h_series)}) "
              f"— see honesty guard in module docstring. Reading VR below instead.")
        result.update({"median_H": None, "pct_mr": None, "pct_rw": None, "pct_trend": None})

    if len(vr_series) >= 10:
        q_used = max(2, min(5, WINDOWS[label] // 5))
        pct_vr_mr = (vr_series < 1.0).mean() * 100
        print(f"\n  Variance Ratio (q={q_used}):")
        print(f"    median VR = {vr_series.median():.3f}  "
              f"(VR<1 -> mean-reverting, VR>1 -> trending)")
        print(f"    % tickers with VR<1 (mean-reverting signature): {pct_vr_mr:.0f}%")
        result.update({"median_VR": vr_series.median(), "pct_vr_mr": pct_vr_mr})
    else:
        print(f"\n  Variance Ratio: insufficient rows ({len(vr_series)}).")
        result.update({"median_VR": None, "pct_vr_mr": None})

    if len(hl_series) >= 5:
        print(f"\n  Half-life (AR(1), only computed where beta<0 — i.e. tickers "
              f"the model itself calls mean-reverting):")
        print(f"    n tickers with a defined half-life: {len(hl_series)} "
              f"({len(hl_series)/max(len(df),1)*100:.0f}% of window)")
        print(f"    median half-life: {hl_series.median():.1f} trading days "
              f"(25th {hl_series.quantile(.25):.1f} / 75th {hl_series.quantile(.75):.1f})")
        result.update({"median_half_life": hl_series.median(), "n_half_life": len(hl_series)})
    else:
        print(f"\n  Half-life: too few mean-reverting tickers in this window "
              f"({len(hl_series)}) for a meaningful median.")
        result.update({"median_half_life": None, "n_half_life": len(hl_series)})

    return result


def final_verdict(summaries: List[Dict[str, Any]]) -> None:
    print(f"\n{'#'*78}")
    print("#  HEADLINE — F7 lookback regime comparison (10d vs 60d vs 126d)")
    print(f"{'#'*78}")
    print(f"\n  {'window':<8}{'median H':>10}{'median VR':>12}{'%MR (H)':>10}"
          f"{'%trend (H)':>12}{'median HL(d)':>14}")
    print(f"  {'-'*66}")
    for s in summaries:
        h = f"{s['median_H']:.3f}" if s['median_H'] is not None else "N/A"
        vr = f"{s['median_VR']:.3f}" if s['median_VR'] is not None else "N/A"
        pmr = f"{s['pct_mr']:.0f}%" if s['pct_mr'] is not None else "N/A"
        ptr = f"{s['pct_trend']:.0f}%" if s['pct_trend'] is not None else "N/A"
        hl = f"{s['median_half_life']:.1f}" if s['median_half_life'] is not None else "N/A"
        print(f"  {s['window']:<8}{h:>10}{vr:>12}{pmr:>10}{ptr:>12}{hl:>14}")

    print(f"\n  Read this table against:")
    print(f"    - PHASE_2_3_FILTER_REVIEW.md's F7 hypothesis (10d = reversal zone,")
    print(f"      60d/126d skip-21 = momentum zone per Jegadeesh 1990 / 1993).")
    print(f"    - For 10d specifically, weight the VR column over H (see module")
    print(f"      docstring honesty guard — R/S is underpowered at n=9 returns).")
    print(f"    - This is a SINGLE-SNAPSHOT, single-regime read (per the locked")
    print(f"      Session 2 scope) — same caveat as Session 1: re-run on a")
    print(f"      different VIX/trend day before treating this as final if the")
    print(f"      result materially changes a config decision.")
    print(f"    - Median half-life (where defined) is also a direct input to")
    print(f"      Dew's separate, still-open Requirement #1 (extend max hold")
    print(f"      from 7 to ~10-14 days) — but ONLY for windows/tickers the")
    print(f"      model itself classifies mean-reverting. If a window is")
    print(f"      dominated by trending tickers (%trend high), half-life is")
    print(f"      not the right basis for a hold-period decision there — that")
    print(f"      would need a separate trend-persistence measure, not built")
    print(f"      in this script.")
    print(f"{'#'*78}\n")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline Hurst/OU lookback-regime diagnostic (Session 2 / Stochastic)",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N universe tickers (quick test)")
    parser.add_argument("--min-tickers", type=int, default=30,
                        help="Abort if fewer than N usable rows collected")
    args = parser.parse_args()

    print(f"\n{'#'*78}")
    print("#  SP500 SWING AGENT — HURST / OU LOOKBACK-REGIME DIAGNOSTIC")
    print("#  Session 2 (Stochastic Calculus) — OFFLINE, zero pipeline / shadow-log touch")
    print(f"{'#'*78}")

    try:
        ctx = data_mod.fetch_market_context()
        print(f"\n  SNAPSHOT CONTEXT (this run only): "
              f"VIX {ctx.get('vix', '?')} | SPY ${ctx.get('spy_price', '?')} "
              f"| SPY 1d {ctx.get('spy_1d_change_pct', '?')}% "
              f"| above 200MA {ctx.get('spy_above_200ma', '?')}")
        print("  NOTE: single-snapshot, single-regime by design (locked with Dew for")
        print("  Session 2). Re-run on a different day/regime before treating the")
        print("  verdict below as final if it drives a config decision.")
    except Exception as e:
        logger.warning(f"Could not fetch regime context: {e}")

    try:
        universe_df = univ_mod.get_sp500_universe()
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        sys.exit(1)
    logger.info(f"Universe: {len(universe_df)} tickers")

    df = collect_windows(universe_df, limit=args.limit)
    if len(df) < args.min_tickers:
        logger.error(f"Only {len(df)} usable rows — need {args.min_tickers}. Aborting.")
        sys.exit(1)

    summaries = [window_report(df, label) for label in WINDOW_ORDER]
    final_verdict(summaries)


if __name__ == "__main__":
    main()
