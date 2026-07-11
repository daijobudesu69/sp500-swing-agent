"""
hurst_lookback_analysis.py — OFFLINE Hurst exponent + OU half-life diagnostic
for SP500 Swing Agent.

SESSION 2 (Stochastic Calculus / Mean Reversion) — answers ONE question:
  Is the S&P 500 universe in a mean-reverting or trending (momentum) regime
  at the 10-day, 60-day, and 126-day horizons? Tests whether F7's current
  10-day RS-vs-SPY lookback sits in the short-term reversal zone flagged in
  PHASE_2_3_FILTER_REVIEW.md (Jegadeesh 1990), vs whether 60d/126d are
  structurally better aligned with a momentum regime (Jegadeesh & Titman
  1993).

ROUND 2 (this version) — ADF gate + detrend fix:
  The first run (see QUANT_UPGRADE session notes) computed half-life via
  AR(1) regression directly on RAW price levels. Result: 93-99% of tickers
  got a "defined" half-life (beta<0) at every window, CONTRADICTING the
  Hurst exponent's own 99% "trending" classification for the same tickers
  in the same windows. Root cause: AR(1)-on-raw-price is mis-specified for
  a trending (non-stationary) series — it spuriously finds "reversion" to
  a constant mean that doesn't exist, because a rising price with any
  noise will often show negative-beta reversion around ITS OWN upward
  path even though it never reverts to a fixed level. This is a textbook
  small-sample / non-stationarity artifact, not a real finding.

  Fix, in two parts:
    1. DETREND — before any AR(1)/half-life fit, subtract an OLS linear
       trend (in log-price, vs. time index) from the window. Half-life is
       now "how fast does price revert to ITS TREND LINE", not "how fast
       does it revert to a fixed level" — the correct question for a
       market that may be legitimately trending.
    2. ADF GATE — run an Augmented Dickey-Fuller test (own numpy
       implementation, constant-only regression, AIC-selected lag 0-4,
       MacKinnon-interpolated critical values — no scipy/statsmodels
       dependency added) on the DETRENDED residual. Half-life is only
       computed and reported for tickers/windows where the residual
       rejects the unit-root null at 5% AND has >=20 observations. This
       is the check the mean-reversion skill itself calls mandatory
       ("Before trading mean reversion, you must statistically confirm
       the series is mean-reverting") and that Round 1 skipped.

  Expected effect: the 10d window's "93% half-life defined" figure should
  collapse toward ~0% (correctly — n=10 log-prices gives only 9 diffed
  points, below the n>=20 floor this script now enforces for ADF). The
  60d/126d windows' inflated "97-99% defined" should shrink to whatever
  fraction genuinely passes ADF — likely much smaller, and now internally
  consistent with the Hurst "99% trending" call instead of contradicting
  it.

WHY THIS IS A STANDALONE OFFLINE SCRIPT (same contract as filter_correlation.py):
  - Reads ONLY universe.py (ticker list) and data_fetcher.py (OHLCV).
    Nothing is written back anywhere — no pipeline, config, or shadow-log touch.
  - Safe to run any time during Batch 3 config-freeze.

METHOD (locked with Dew, unchanged from Round 1):
  - Single-snapshot, per-ticker (not pooled) — report the DISTRIBUTION per
    window, not one aggregate number.

FOR EACH TICKER, FOR EACH WINDOW (10d / 60d / 126d):
  1. Trailing W daily closes -> log price.
  2. Hurst exponent via R/S on log RETURNS (chunk-demeaned internally, so
     this is reasonably trend-robust as-is — UNCHANGED from Round 1).
  3. Variance Ratio on raw log returns (also var-based, already mean-
     centered — UNCHANGED from Round 1).
  4. NEW: detrend log price (OLS vs. time index) -> residual series.
  5. NEW: ADF test on the residual. Only if it rejects the unit-root null
     (5% level, n>=20) -> half-life via AR(1) on the DETRENDED residual
     (not raw price, per the fix above).

HONESTY GUARD — 10-DAY WINDOW (still applies, now for TWO reasons):
  - R/S Hurst: 9 log returns is below the >=3-chunk-size floor for a
    reliable log(R/S) vs log(size) regression -> H=None (unchanged from
    Round 1).
  - NEW: ADF now also requires n>=20 observations -> the 10d window's
    half-life will be ~universally None post-fix, which is the CORRECT,
    honest outcome (Round 1's 93% figure was the bug, not this).
  Read the VR column for 10d, as before.

USAGE:
  python hurst_lookback_analysis.py                 # full run (1y history)
  python hurst_lookback_analysis.py --limit 50       # quick test, first 50 tickers
  python hurst_lookback_analysis.py --min-tickers 100

DEPENDENCIES: numpy + pandas only. ADF implemented natively (own OLS +
MacKinnon-interpolated critical value table) — deliberately did NOT add
scipy/statsmodels, to keep this consistent with filter_correlation.py's
minimal-dependency choice and avoid touching requirements.txt for an
offline diagnostic script.

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

MEAN_REVERT_CEILING = 0.45
TREND_FLOOR = 0.55

FETCH_PERIOD = "1y"

ADF_MIN_OBS = 20          # below this, ADF is not attempted (10d floor)
ADF_ALPHA_5PCT = 0.05     # nominal — we compare stat vs. critical value directly

# MacKinnon-style ADF critical values, "constant, no trend" regression,
# 5% level, keyed by residual sample size (post-lag). Linearly interpolated
# between anchor points. Standard reference values (Fuller 1976 / MacKinnon).
_ADF_CRIT_TABLE_5PCT: List[Tuple[int, float]] = [
    (25, -3.00), (50, -2.93), (100, -2.89),
    (250, -2.88), (500, -2.87), (1000, -2.86),
]


# ============================================================
# CORE MATH — R/S Hurst, Variance Ratio (unchanged from Round 1)
# ============================================================

def hurst_rs(returns: np.ndarray, min_chunk: int = 3) -> Tuple[Optional[float], int]:
    """Hurst exponent via classic R/S analysis on log returns. Unchanged
    from Round 1 — chunk-demeaning already gives this reasonable trend
    robustness without needing the explicit detrend used for half-life."""
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
    """VR < 1 = mean-reverting, VR = 1 = random walk, VR > 1 = trending.
    Unchanged from Round 1 — np.var() is already mean-centered."""
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


# ============================================================
# NEW — DETREND + ADF GATE
# ============================================================

def detrend_log_series(log_prices: np.ndarray) -> np.ndarray:
    """
    OLS-detrend: subtract the best-fit linear trend (log-price vs. time
    index) from the series. The residual is what AR(1)/half-life should
    actually be tested and fit on — raw log-price of a trending stock is
    non-stationary by construction and produces spurious "mean reversion"
    from a plain AR(1) fit (see module docstring, Round 2 rationale).
    """
    n = len(log_prices)
    t = np.arange(n, dtype=float)
    X = np.column_stack([np.ones(n), t])
    coefs, *_ = np.linalg.lstsq(X, log_prices, rcond=None)
    trend = X @ coefs
    return log_prices - trend


def _adf_critical_value_5pct(n_obs: int) -> float:
    """Interpolate the 5%-level ADF critical value (constant, no trend
    case) for a given residual sample size, from the MacKinnon table."""
    table = _ADF_CRIT_TABLE_5PCT
    if n_obs <= table[0][0]:
        return table[0][1]
    if n_obs >= table[-1][0]:
        return table[-1][1]
    for (n1, c1), (n2, c2) in zip(table, table[1:]):
        if n1 <= n_obs <= n2:
            frac = (n_obs - n1) / (n2 - n1)
            return c1 + frac * (c2 - c1)
    return table[-1][1]


def adf_test(series: np.ndarray, max_candidate_lag: int = 4) -> Optional[Dict[str, Any]]:
    """
    Augmented Dickey-Fuller test, own numpy implementation (no scipy/
    statsmodels — keeps this script's dependency footprint at numpy+pandas,
    matching filter_correlation.py).

    Regression (constant, no trend — series is ALREADY detrended by the
    caller, so no separate trend term here):
        delta_y_t = c + phi*y_{t-1} + sum_{i=1..k} gamma_i * delta_y_{t-i} + eps_t
    H0: phi = 0 (unit root, non-stationary). H1: phi < 0 (stationary).
    Lag order k selected by AIC over 0..max_candidate_lag.

    Returns None if series is too short (< ADF_MIN_OBS) to test honestly.
    Otherwise returns {stat, crit_5pct, is_stationary, n_obs, lag}.
    """
    n = len(series)
    if n < ADF_MIN_OBS:
        return None

    y = series
    dy = np.diff(y)
    y_lag1 = y[:-1]  # aligned with dy, both length n-1

    best: Optional[Dict[str, Any]] = None
    for k in range(0, max_candidate_lag + 1):
        usable = len(dy) - k
        if usable < 10:
            continue
        Y = dy[k:]
        cols = [np.ones(len(Y)), y_lag1[k:]]
        for j in range(1, k + 1):
            cols.append(dy[k - j: len(dy) - j])
        X = np.column_stack(cols)

        try:
            coefs, *_ = np.linalg.lstsq(X, Y, rcond=None)
        except np.linalg.LinAlgError:
            continue

        resid = Y - X @ coefs
        dof = len(Y) - X.shape[1]
        if dof <= 0:
            continue
        sigma2 = float(np.sum(resid ** 2) / dof)
        if sigma2 <= 0:
            continue
        try:
            xtx_inv = np.linalg.pinv(X.T @ X)
        except np.linalg.LinAlgError:
            continue
        se_phi = float(np.sqrt(sigma2 * xtx_inv[1, 1]))
        if se_phi <= 0 or not np.isfinite(se_phi):
            continue
        phi = float(coefs[1])
        tstat = phi / se_phi
        aic = len(Y) * np.log(sigma2) + 2 * X.shape[1]

        candidate = {"lag": k, "n_obs": len(Y), "tstat": tstat, "aic": aic}
        if best is None or aic < best["aic"]:
            best = candidate

    if best is None:
        return None

    crit5 = _adf_critical_value_5pct(best["n_obs"])
    return {
        "stat": best["tstat"],
        "crit_5pct": crit5,
        "is_stationary": best["tstat"] < crit5,
        "n_obs": best["n_obs"],
        "lag": best["lag"],
    }


def half_life_ar1(series: np.ndarray) -> Optional[float]:
    """
    Mean-reversion half-life via AR(1) regression (SKILL.md formula),
    now applied to the DETRENDED residual series, not raw price (Round 2
    fix). Half-life is in the same units as the series' spacing (trading
    days here) and now means "days to revert to the trend line", which is
    the coherent quantity for a possibly-trending market. Returns None if
    beta >= 0 (not mean-reverting even around its own trend).
    """
    y = np.diff(series)
    x = series[:-1]
    X = np.column_stack([np.ones(len(x)), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0][1]
    if beta >= 0:
        return None
    return float(-np.log(2) / np.log(1 + beta))


# ============================================================
# FEATURE EXTRACTION
# ============================================================

def build_ticker_windows(closes: np.ndarray) -> Dict[str, Optional[Dict[str, Any]]]:
    """For one ticker's close-price array, compute H/VR (raw) + ADF-gated
    half-life (detrended) per window."""
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

        # --- Round 2: detrend + ADF gate before half-life ---
        detrended = detrend_log_series(log_p)
        adf_result = adf_test(detrended)
        hl: Optional[float] = None
        if adf_result is not None and adf_result["is_stationary"]:
            hl = half_life_ar1(detrended)

        out[label] = {
            "H": H, "n_sizes": n_sizes, "VR": vr,
            "adf": adf_result, "half_life": hl,
        }
    return out


def collect_windows(universe_df: pd.DataFrame, limit: Optional[int]) -> pd.DataFrame:
    tickers = universe_df["ticker"].tolist()
    if limit:
        tickers = tickers[:limit]
    total = len(tickers)
    logger.info(f"Computing Hurst/VR/ADF/half-life across {total} tickers "
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
                    row[f"adf_stat_{label}"] = None
                    row[f"adf_stationary_{label}"] = False
                    row[f"half_life_{label}"] = None
                else:
                    adf = w_data["adf"]
                    row[f"h_{label}"] = w_data["H"]
                    row[f"n_sizes_{label}"] = w_data["n_sizes"]
                    row[f"vr_{label}"] = w_data["VR"]
                    row[f"adf_stat_{label}"] = adf["stat"] if adf else None
                    row[f"adf_stationary_{label}"] = bool(adf["is_stationary"]) if adf else False
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
    h_col, n_col, vr_col = f"h_{label}", f"n_sizes_{label}", f"vr_{label}"
    adf_stat_col, adf_pass_col, hl_col = (
        f"adf_stat_{label}", f"adf_stationary_{label}", f"half_life_{label}"
    )

    h_series = df[h_col].dropna()
    n_series = df[n_col]
    vr_series = df[vr_col].dropna()
    adf_pass_series = df[adf_pass_col]
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
        print(f"\n  Hurst exponent: insufficient computable rows ({len(h_series)}).")
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

    # --- NEW: ADF gate reporting ---
    n_adf_attempted = df[adf_stat_col].notna().sum()
    pct_adf_pass = (adf_pass_series.sum() / max(len(df), 1)) * 100
    print(f"\n  ADF stationarity gate (on DETRENDED residual, 5% level, "
          f"n>=20 required):")
    print(f"    ADF attempted (n>=20 obs available): {n_adf_attempted}/{len(df)}")
    print(f"    Tickers passing ADF (residual IS stationary): "
          f"{int(adf_pass_series.sum())}/{len(df)} ({pct_adf_pass:.0f}%)")
    result.update({"pct_adf_pass": pct_adf_pass, "n_adf_attempted": int(n_adf_attempted)})

    if len(hl_series) >= 5:
        print(f"\n  Half-life (AR(1) on DETRENDED residual, ONLY for ADF-passing "
              f"tickers — Round 2 fix):")
        print(f"    n tickers with a defined, ADF-gated half-life: {len(hl_series)} "
              f"({len(hl_series)/max(len(df),1)*100:.0f}% of window)")
        print(f"    median half-life: {hl_series.median():.1f} trading days "
              f"(25th {hl_series.quantile(.25):.1f} / 75th {hl_series.quantile(.75):.1f})")
        print(f"    NOTE: this is now 'days to revert to the trend line', not "
              f"'days to revert to a fixed price'.")
        result.update({"median_half_life": hl_series.median(), "n_half_life": len(hl_series)})
    else:
        print(f"\n  Half-life: too few ADF-gated mean-reverting tickers in this "
              f"window ({len(hl_series)}) for a meaningful median.")
        result.update({"median_half_life": None, "n_half_life": len(hl_series)})

    return result


def final_verdict(summaries: List[Dict[str, Any]]) -> None:
    print(f"\n{'#'*78}")
    print("#  HEADLINE — F7 lookback regime comparison (10d vs 60d vs 126d)")
    print("#  Round 2: half-life now ADF-gated + detrended (see module docstring)")
    print(f"{'#'*78}")
    print(f"\n  {'window':<8}{'median H':>10}{'median VR':>12}{'%MR (H)':>10}"
          f"{'%trend (H)':>12}{'%ADF-pass':>11}{'median HL(d)':>14}")
    print(f"  {'-'*77}")
    for s in summaries:
        h = f"{s['median_H']:.3f}" if s['median_H'] is not None else "N/A"
        vr = f"{s['median_VR']:.3f}" if s['median_VR'] is not None else "N/A"
        pmr = f"{s['pct_mr']:.0f}%" if s['pct_mr'] is not None else "N/A"
        ptr = f"{s['pct_trend']:.0f}%" if s['pct_trend'] is not None else "N/A"
        padf = f"{s['pct_adf_pass']:.0f}%" if s.get('pct_adf_pass') is not None else "N/A"
        hl = f"{s['median_half_life']:.1f}" if s['median_half_life'] is not None else "N/A"
        print(f"  {s['window']:<8}{h:>10}{vr:>12}{pmr:>10}{ptr:>12}{padf:>11}{hl:>14}")

    print(f"\n  Read this table against:")
    print(f"    - PHASE_2_3_FILTER_REVIEW.md's F7 hypothesis (10d = reversal zone,")
    print(f"      60d/126d skip-21 = momentum zone per Jegadeesh 1990 / 1993).")
    print(f"    - %ADF-pass is the NEW honesty check: only this fraction of each")
    print(f"      window's half-life values are trustworthy. Compare this to")
    print(f"      Round 1's uncorrected 93-99% 'defined' figures — if %ADF-pass")
    print(f"      is much lower, that confirms Round 1's numbers were a")
    print(f"      non-stationarity artifact, not a real signal.")
    print(f"    - For 10d specifically, %ADF-pass will likely be ~0% (n=10 is")
    print(f"      below the ADF_MIN_OBS=20 floor by construction) — read VR")
    print(f"      instead, same as Round 1's guidance.")
    print(f"    - Median half-life is only a valid input to Dew's open")
    print(f"      Requirement #1 (extend max hold 7->10-14 days) for the")
    print(f"      ADF-passing subset specifically — NOT the window's full")
    print(f"      population, and NOT if that window is dominated by trending")
    print(f"      tickers per the Hurst/%trend column.")
    print(f"    - Still a SINGLE-SNAPSHOT, single-regime read — re-run on a")
    print(f"      different VIX/trend day before this drives a config decision.")
    print(f"{'#'*78}\n")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline Hurst/OU lookback-regime diagnostic, ADF-gated (Session 2 Round 2)",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N universe tickers (quick test)")
    parser.add_argument("--min-tickers", type=int, default=30,
                        help="Abort if fewer than N usable rows collected")
    args = parser.parse_args()

    print(f"\n{'#'*78}")
    print("#  SP500 SWING AGENT — HURST / OU LOOKBACK-REGIME DIAGNOSTIC")
    print("#  Session 2 (Stochastic Calculus) — Round 2: ADF gate + detrend fix")
    print(f"{'#'*78}")

    try:
        ctx = data_mod.fetch_market_context()
        print(f"\n  SNAPSHOT CONTEXT (this run only): "
              f"VIX {ctx.get('vix', '?')} | SPY ${ctx.get('spy_price', '?')} "
              f"| SPY 1d {ctx.get('spy_1d_change_pct', '?')}% "
              f"| above 200MA {ctx.get('spy_above_200ma', '?')}")
        print("  NOTE: single-snapshot by design. Re-run on a different day/regime")
        print("  before treating the verdict below as final if it drives a config")
        print("  decision.")
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
