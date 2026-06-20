"""
main.py — Pipeline orchestrator for SP500 Swing Agent.

STAGE 2 REVISION (May 2026):
============================
CHANGES FROM PHASE 1:
  - Mandatory 5-filter screener via indicators.py (replaces SMC-based screen)
  - Dropped 4H timeframe entirely (1D only)
  - SMC brief_data stage removed (bundled into indicators.py)
  - Haiku promotion threshold: 70 → 65
  - Bug fixes: temperature param, cache_control, 4H resample

PRESERVED FROM PHASE 1:
  - Black swan Type A/B/C logic with full depth
  - _generate_defensive_ideas() — Opus on defensive tickers during crash
  - RSS keyword detection for Type C
  - Intraday VIX spike % detection
  - Sector preferences per black swan type
  - Config-driven black swan thresholds
  - ticker_meta passed to risk manager for correlation checks
  - assess_macro_regime() regime classification + TIER 1 veto
  - Two entry points: run_daily_signals() + run_black_swan_check()

PIPELINE FLOW:
  1. Fetch macro context
  2. Macro regime check (TIER 1 veto)
  3. Load S&P 500 universe
  4. Mandatory 5-filter screener (indicators.py)
  5. Haiku quality scoring (top 5, threshold 65)
  6. Opus deep analysis
  7. Risk manager validation
  8. Send to Telegram
  9. Shadow log append (CSV + Telegram + Google Sheets)

BLACK SWAN (separate 15-min entry point):
  Type A — Crash:      VIX spike + SPY drop → HALT + defensive ideas via Opus
  Type B — Rally:      VIX drop + SPY surge → continue + sector alert
  Type C — Info:       RSS keyword detection + VIX move → info alert only

PHASE 2.3 REVISION (June 2026):
================================
ADDED:
  - RS gap vs SPY (soft F7 context) computed once before screener loop,
    stored as rs_gap_vs_spy_pct in each candidate dict. NOT a hard gate.
  - Shadow log append after send_daily_signals():
    Writes auto-logged fields to shadow_log.csv (config.SHADOW_LOG_PATH).
    Manual outcome fields (_shadow_ema9_filled, _shadow_outcome, etc.)
    are left blank for manual fill-in after trade closes.

PHASE 2.3 — F7 LOOKBACK REVIEW (June 2026):
============================================
ADDED (shadow-log only — NOT a gate, F7 gate logic unchanged):
  - rs_60d_gap and rs_126d_gap computed alongside the existing 10d RS gap.
    Both use the "skip last 21 trading days" momentum convention
    (Jegadeesh & Titman 1993 / Springer 2022 momentum-reversal literature)
    to avoid contaminating the momentum window with short-term reversal noise.
    Window: t-(N+21) -> t-21, where N = 60 or 126.
  - Purpose: at the next filter review, compare predictive power of
    10d (current F7) vs 60d vs 126d RS gap against closed-trade outcomes.
    If 60d/126d separates win/loss better than 10d, F7 gets re-pointed.
    Zero pipeline risk — these are additional context fields only.

PHASE 4a REVISION (June 2026):
================================
ADDED:
  - Google Sheets mirror of the shadow log via src/sheets_logger.py.
    Wired into STAGE 8, runs after the existing CSV append + Telegram
    shadow log message. Non-blocking — failure here never stops the
    pipeline or affects signal delivery (same try/except contract as
    the existing shadow log step).
  - IMPORTANT: the CSV (shadow_log.csv) remains the source of truth.
    The Sheet is a parallel mirror for easier manual outcome entry
    from phone/browser. The two are written independently from two
    separately-built row dicts — there is no two-way sync. If you
    edit the manual fields in one, remember to mirror the edit in
    the other, or pick one as your single working copy going forward.
  - _shadow_outcome categories (manual field — fill in after a trade closes):
      SL                  -> stopped out before TP1
      TP1_TRAIL           -> TP1 hit, 50% locked +2R, remainder trailed to exit
      TP2                 -> full target hit (+3R)
      INVALIDATED_LOSS    -> discretionary exit pre-TP1, exit price < entry
      INVALIDATED_PROFIT  -> discretionary exit pre-TP1, exit price > entry
      FLOAT               -> still open at time of review (mark-to-market)
"""
import csv
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Any, Optional

import pandas as pd

from src import config
from src import universe as univ_mod
from src import data_fetcher as data_mod
from src import indicators as ind_mod
from src import analyst as analyst_mod
from src import risk_manager as risk_mod
from src import telegram_sender as tg_mod
from src import sheets_logger as sheets_mod

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


# ============================================================
# MACRO REGIME CLASSIFICATION
# ============================================================

def assess_macro_regime(macro_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Classify market regime. Returns {regime, allow_longs, rr_target, reason}.
    TIER 1 VETO: VIX panic + SPY below 200MA = capital preservation mode.
    """
    vix = macro_ctx.get("vix", 20.0)
    spy_above_200ma = macro_ctx.get("spy_above_200ma", True)
    vix_panic = macro_ctx.get("vix_panic", False)

    if vix_panic and not spy_above_200ma:
        return {
            "regime": "Risk-Off Bear",
            "allow_longs": False,
            "rr_target": config.MIN_RR_RATIO,
            "reason": (
                f"VIX {vix:.1f} > {config.VIX_PANIC_THRESHOLD} "
                f"AND SPY below 200MA — capital preservation mode"
            ),
        }

    if vix < 18 and spy_above_200ma:
        return {
            "regime": "Risk-On Bull",
            "allow_longs": True,
            "rr_target": config.BULL_REGIME_RR_TARGET,
            "reason": "Calm VIX + strong trend — bull regime, can stretch RR",
        }

    if vix < 25 and spy_above_200ma:
        return {
            "regime": "Cautious Bull",
            "allow_longs": True,
            "rr_target": config.MIN_RR_RATIO,
            "reason": "Moderate VIX, trend intact — standard 2:1 RR",
        }

    return {
        "regime": "Mixed / Choppy",
        "allow_longs": True,
        "rr_target": config.MIN_RR_RATIO,
        "reason": "Elevated VIX or weakening trend — be selective",
    }


# ============================================================
# RS GAP HELPER — Phase 2.3 F7 lookback review (shadow-only)
# ============================================================

def _compute_return_pct(close_series: pd.Series, lookback_days: int, skip_recent_days: int = 0) -> Optional[float]:
    """
    Generic % return over a lookback window, optionally skipping the most
    recent N trading days (momentum convention — avoids short-term reversal
    contamination per Jegadeesh & Titman 1993 / Springer 2022).

    skip_recent_days=0  -> return from t-lookback to t       (used for F7 10d, unchanged)
    skip_recent_days=21 -> return from t-(lookback+21) to t-21 (used for 60d/126d shadow fields)

    Returns None if insufficient history (caller defaults to 0.0 / skips field).
    """
    end_idx = -(skip_recent_days + 1) if skip_recent_days > 0 else -1
    start_idx = -(lookback_days + skip_recent_days + 1)

    if len(close_series) < abs(start_idx):
        return None

    end_price = float(close_series.iloc[end_idx])
    start_price = float(close_series.iloc[start_idx])
    if start_price == 0:
        return None
    return ((end_price - start_price) / start_price) * 100


def _compute_momentum_rs_gaps(
    stock_close: pd.Series,
    spy_close: pd.Series,
) -> Dict[str, Optional[float]]:
    """
    Phase 2.3 F7 review — shadow-only momentum RS gaps.
    Computes 60d and 126d RS gap (stock - SPY), each skipping the most
    recent 21 trading days. NOT used for gating — F7 gate still runs on
    the existing 10d gap untouched in run_mandatory_screener().

    Returns dict with rs_60d_gap / rs_126d_gap, values may be None if
    insufficient price history (e.g. recent IPO).
    """
    skip = getattr(config, "RS_SKIP_RECENT_DAYS", 21)
    lookbacks = {
        "rs_60d_gap": getattr(config, "RS_60D_LOOKBACK", 60),
        "rs_126d_gap": getattr(config, "RS_126D_LOOKBACK", 126),
    }

    out: Dict[str, Optional[float]] = {}
    for field, lb in lookbacks.items():
        stock_ret = _compute_return_pct(stock_close, lb, skip_recent_days=skip)
        spy_ret = _compute_return_pct(spy_close, lb, skip_recent_days=skip)
        if stock_ret is None or spy_ret is None:
            out[field] = None
        else:
            out[field] = round(stock_ret - spy_ret, 2)
    return out


# ============================================================
# SHADOW LOG — CSV APPEND
# ============================================================

def _build_shadow_rows(signals: List[Dict[str, Any]], run_date: str) -> List[Dict[str, Any]]:
    """
    Build the list of shadow-log row dicts (auto fields populated, manual
    fields left blank) for a batch of approved signals.

    Shared by both the CSV writer (_append_shadow_log) and the Google
    Sheets mirror (STAGE 8) so there is exactly ONE place that defines
    what a shadow-log row looks like. Add new fields here only.
    """
    rows = []
    for sig in signals:
        ind = sig.get("_shadow_indicators", {})
        rows.append({
            "_shadow_date": run_date,
            "_shadow_ticker": sig.get("ticker", ""),
            "_shadow_close_price": ind.get("close_price", ""),
            "_shadow_ema9_price": ind.get("ema9_price", ""),
            "_shadow_ema9_gap_pct": ind.get("ema9_gap_pct", ""),
            "_shadow_ema9_ext_atr": ind.get("ema9_ext_atr", ""),
            "_shadow_atr": ind.get("atr", ""),
            "_shadow_atr_pct": ind.get("atr_pct", ""),
            "_shadow_rs_gap_vs_spy": ind.get("rs_gap_vs_spy", ""),
            "_shadow_spy_10d_return": ind.get("spy_10d_return", ""),
            "_shadow_stock_10d_return": ind.get("stock_10d_return", ""),
            "_shadow_rs_60d_gap": ind.get("rs_60d_gap", ""),
            "_shadow_rs_126d_gap": ind.get("rs_126d_gap", ""),
            "_shadow_conviction": sig.get("conviction", ""),
            "_shadow_confluence_count": ind.get("confluence_count", ""),
            "_shadow_haiku_score": sig.get("_haiku_score", ""),
            "_shadow_adx": ind.get("adx", ""),
            "_shadow_rsi": ind.get("rsi", ""),
            "_shadow_volume_ratio": ind.get("volume_ratio", ""),
            "_shadow_entry_close": ind.get("entry_close", ""),
            "_shadow_entry_ema9": sig.get("entry", ""),     # Opus sets entry = EMA9
            "_shadow_stop": sig.get("stop_loss", ""),
            "_shadow_tp1": sig.get("tp1", ""),
            "_shadow_tp2": sig.get("tp2", ""),
            "_shadow_rr": sig.get("rr_ratio", ""),
            # Manual — blank on write, filled in by hand after trade closes
            "_shadow_ema9_filled": "",
            "_shadow_outcome": "",
            "_shadow_r_realized": "",
            "_shadow_hold_days": "",
        })
    return rows


def _append_shadow_log(signals: List[Dict[str, Any]], run_date: str) -> List[Dict[str, Any]]:
    """
    Append auto-logged shadow fields for each approved signal to shadow_log.csv.
    Manual outcome fields are written as empty strings for manual fill-in later.

    Returns the row list that was written, so callers (e.g. the Google
    Sheets mirror in STAGE 8) can reuse the exact same rows instead of
    rebuilding them — keeps CSV and Sheet permanently in sync by
    construction rather than by two parallel copies of the same logic.

    Fields written:
      Auto: date, ticker, close_price, ema9_price, ema9_gap_pct, ema9_ext_atr,
            atr, atr_pct, rs_gap_vs_spy, spy_10d_return, stock_10d_return,
            rs_60d_gap, rs_126d_gap, conviction, confluence_count, haiku_score,
            adx, rsi, volume_ratio, entry_close, entry_ema9, stop, tp1, tp2, rr
      Manual (blank): ema9_filled, outcome, r_realized, hold_days
    """
    log_path = getattr(config, "SHADOW_LOG_PATH", "shadow_log.csv")

    fieldnames = [
        # Auto-logged
        "_shadow_date",
        "_shadow_ticker",
        "_shadow_close_price",
        "_shadow_ema9_price",
        "_shadow_ema9_gap_pct",
        "_shadow_ema9_ext_atr",
        "_shadow_atr",
        "_shadow_atr_pct",
        "_shadow_rs_gap_vs_spy",
        "_shadow_spy_10d_return",
        "_shadow_stock_10d_return",
        "_shadow_rs_60d_gap",
        "_shadow_rs_126d_gap",
        "_shadow_conviction",
        "_shadow_confluence_count",
        "_shadow_haiku_score",
        "_shadow_adx",
        "_shadow_rsi",
        "_shadow_volume_ratio",
        "_shadow_entry_close",
        "_shadow_entry_ema9",
        "_shadow_stop",
        "_shadow_tp1",
        "_shadow_tp2",
        "_shadow_rr",
        # Manual fill-in (blank on write)
        "_shadow_ema9_filled",
        "_shadow_outcome",
        "_shadow_r_realized",
        "_shadow_hold_days",
    ]

    rows = _build_shadow_rows(signals, run_date)
    file_exists = os.path.isfile(log_path)

    try:
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)

        logger.info(f"Shadow log appended: {len(rows)} row(s) → {log_path}")

    except Exception as e:
        logger.error(f"Shadow log append failed: {e}")

    return rows


# ============================================================
# MANDATORY SCREENER (Stage 2 — replaces SMC screen)
# ============================================================

def run_mandatory_screener(
    universe_df: pd.DataFrame,
    macro_regime: str,
) -> List[Dict[str, Any]]:
    """
    Run 5-filter mandatory screener via indicators.py on full universe.
    Replaces Phase 1 technical_screen() + compute_brief_data() stages.

    Mandatory filters (ALL must pass):
      1. Price > EMA(50)
      2. ADX(14) > 25 AND +DI > -DI
      3. RSI(14) > 45
      4. Volume ratio (14d avg) > 1.3x
      5. ATR(14) > 1.5% of price

    Phase 2.3 — Soft F7 (NOT a hard gate):
      Computes rs_gap_vs_spy_pct (stock 10d return - SPY 10d return) for each
      candidate and stores it in the candidate dict + brief_data for Haiku context.
      SPY is fetched ONCE before the loop (not per ticker).

    Phase 2.3 — F7 lookback review (NOT a hard gate, shadow-only):
      Also computes rs_60d_gap and rs_126d_gap (skip-21d momentum convention)
      for the SAME candidates that already passed F1-F5, using the SAME
      SPY series fetched once above. These are stored alongside the 10d gap
      purely for the upcoming filter review — F7 gating logic is untouched.
    """
    candidates = []
    errors = 0
    total = len(universe_df)
    logger.info(f"Screening {total} tickers via mandatory 5-filter screener...")

    # --------------------------------------------------------
    # Phase 2.3: Fetch SPY 10d return ONCE before the loop (soft F7 context)
    # Also fetch the longer SPY close series here so the 60d/126d helper
    # below doesn't need a second SPY call.
    # --------------------------------------------------------
    spy_10d_return = 0.0
    spy_close_series = None
    try:
        spy_df = data_mod.fetch_ohlcv("SPY", period="1y", interval="1d")  # widened from 3mo to cover 126d+21d skip
        rs_lookback = getattr(config, "RS_LOOKBACK_DAYS", 10)
        if spy_df is not None:
            spy_close_series = spy_df["Close"]
            if len(spy_close_series) >= rs_lookback + 1:
                spy_end = float(spy_close_series.iloc[-1])
                spy_start = float(spy_close_series.iloc[-(rs_lookback + 1)])
                spy_10d_return = ((spy_end - spy_start) / spy_start) * 100
                logger.info(f"SPY {rs_lookback}d return: {spy_10d_return:+.2f}%")
            else:
                logger.warning("SPY data insufficient for 10d RS gap — defaulting to 0.0%")
        else:
            logger.warning("SPY fetch returned None — RS gaps will default/skip")
    except Exception as e:
        logger.warning(f"SPY fetch for RS gap failed: {e} — defaulting to 0.0%")

    for i, (_, row) in enumerate(universe_df.iterrows()):
        ticker = row["ticker"]
        try:
            # Widened from 6mo to 1y so rs_126d_gap (needs ~147 trading days
            # of history: 126 + 21 skip) has enough bars. F1-F5 indicators
            # are unaffected — they only look at the tail of this series.
            df = data_mod.fetch_ohlcv(ticker, period="1y", interval="1d")
            if df is None:
                continue

            brief = ind_mod.build_brief_data(
                df,
                ticker=ticker,
                sector=row.get("sector", "Unknown"),
                macro_regime=macro_regime,
            )
            if brief is None:
                continue

            if brief.get("all_mandatory_pass"):
                # ------------------------------------------------
                # Phase 2.3: Compute soft F7 — RS gap vs SPY (10d, unchanged)
                # NOT a gate. Stored for Haiku penalisation context.
                # ------------------------------------------------
                rs_gap_vs_spy_pct = 0.0
                stock_10d_return = 0.0
                try:
                    rs_lookback = getattr(config, "RS_LOOKBACK_DAYS", 10)
                    if len(df) >= rs_lookback + 1:
                        stock_close = df["Close"]
                        stock_end = float(stock_close.iloc[-1])
                        stock_start = float(stock_close.iloc[-(rs_lookback + 1)])
                        stock_10d_return = ((stock_end - stock_start) / stock_start) * 100
                        rs_gap_vs_spy_pct = stock_10d_return - spy_10d_return
                except Exception as rs_err:
                    logger.debug(f"RS gap (10d) compute failed for {ticker}: {rs_err}")

                # Inject into brief_data so Haiku receives it (unchanged)
                brief["rs_gap_vs_spy_pct"] = round(rs_gap_vs_spy_pct, 2)
                brief["stock_10d_return"] = round(stock_10d_return, 2)
                brief["spy_10d_return"] = round(spy_10d_return, 2)

                # ------------------------------------------------
                # Phase 2.3 F7 review: 60d / 126d RS gaps — SHADOW ONLY.
                # Not passed to Haiku, not gated. Logged for the F7
                # lookback comparison at the next filter review.
                # ------------------------------------------------
                if spy_close_series is not None:
                    try:
                        momentum_gaps = _compute_momentum_rs_gaps(
                            stock_close=df["Close"],
                            spy_close=spy_close_series,
                        )
                    except Exception as mom_err:
                        logger.debug(f"Momentum RS gap compute failed for {ticker}: {mom_err}")
                        momentum_gaps = {"rs_60d_gap": None, "rs_126d_gap": None}
                else:
                    momentum_gaps = {"rs_60d_gap": None, "rs_126d_gap": None}

                brief["rs_60d_gap"] = momentum_gaps.get("rs_60d_gap")
                brief["rs_126d_gap"] = momentum_gaps.get("rs_126d_gap")

                candidates.append({
                    "ticker": ticker,
                    "sector": row.get("sector", "Unknown"),
                    "sub_industry": row.get("sub_industry", "Unknown"),
                    "df": df,
                    "brief_data": brief,
                })

        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.warning(f"Screen error for {ticker}: {str(e)[:80]}")

        if (i + 1) % 100 == 0:
            logger.info(
                f"  Screener progress: {i + 1}/{total} "
                f"({len(candidates)} passed so far)"
            )

    if errors > 0:
        logger.warning(f"Screener completed with {errors} fetch errors")

    logger.info(
        f"Mandatory screener: {len(candidates)} candidates from {total} universe"
    )
    return candidates


# ============================================================
# BUILD FULL DATA FOR OPUS
# ============================================================

def build_full_data_for_opus(
    ticker: str,
    df: pd.DataFrame,
    brief_data: Dict[str, Any],
    macro_context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Build comprehensive data package for Opus deep analysis.
    Fetches fundamentals, news, weekly trend. 1D only (no 4H).
    """
    try:
        fundamentals = data_mod.fetch_basic_info(ticker)
        news = data_mod.fetch_news_for_ticker(ticker, max_items=5)
        weekly_trend = data_mod.fetch_weekly_trend(ticker)

        return {
            "ticker": ticker,
            "fundamentals": fundamentals,
            "indicators": brief_data,
            "confluence_display": brief_data.get("confluence_display", "N/A"),
            "weekly_trend": weekly_trend,
            "news": news,
            "macro_context": macro_context,
        }
    except Exception as e:
        logger.error(f"build_full_data failed for {ticker}: {e}")
        return None


# ============================================================
# BLACK SWAN — DEFENSIVE IDEAS (preserved from Phase 1)
# ============================================================

def _generate_defensive_ideas(
    macro_ctx: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    On crash days: run Opus on config-defined defensive tickers,
    return approved signals as watchlist ideas.
    Lightweight — bypasses full screener.
    """
    defensive_tickers = getattr(
        config, "BLACK_SWAN_TYPE_A", {}
    ).get("preferred_tickers", [])[:6]

    if not defensive_tickers:
        logger.warning("No defensive tickers configured in BLACK_SWAN_TYPE_A")
        return []

    ideas = []
    for ticker in defensive_tickers:
        try:
            df = data_mod.fetch_ohlcv(ticker, period="6mo", interval="1d")
            if df is None:
                continue
            brief = ind_mod.build_brief_data(df, ticker=ticker, macro_regime="crash")
            if brief is None:
                continue
            full_data = build_full_data_for_opus(ticker, df, brief, macro_ctx)
            if full_data is None:
                continue
            signal = analyst_mod.opus_deep_analysis(ticker, full_data)
            if "error" not in signal and signal.get("conviction", 0) >= config.CONVICTION_STANDARD:
                ideas.append(signal)
        except Exception as e:
            logger.warning(f"Defensive idea failed for {ticker}: {e}")

        if len(ideas) >= 3:
            break

    if ideas:
        validated = risk_mod.validate_and_filter(ideas, {})
        return validated.get("approved", [])
    return []


# ============================================================
# DAILY PIPELINE
# ============================================================

def run_daily_signals() -> Dict[str, Any]:
    """
    Main entry point — full daily signal pipeline.
    Triggered by GitHub Actions cron at 01:00 UTC (8:00 AM WIB).
    """
    start_time = time.time()
    run_date = datetime.utcnow().strftime("%Y-%m-%d")
    logger.info("=" * 60)
    logger.info("DAILY SIGNAL PIPELINE — STARTING")
    logger.info(f"Run timestamp: {run_date}T{datetime.utcnow().strftime('%H:%M:%S')}Z")
    logger.info("=" * 60)

    result = {
        "status": "unknown",
        "signals_sent": 0,
        "stage_timings": {},
        "errors": [],
    }

    try:
        # --------------------------------------------------------
        # STAGE 1: Macro context
        # --------------------------------------------------------
        t = time.time()
        macro_market = data_mod.fetch_market_context()
        macro_econ = data_mod.fetch_macro_snapshot()
        macro_context = {**macro_market, **macro_econ}
        result["stage_timings"]["macro_fetch"] = round(time.time() - t, 2)
        logger.info(
            f"Macro context: VIX={macro_context.get('vix', '?'):.2f}, "
            f"SPY above 200MA={macro_context.get('spy_above_200ma', '?')}"
        )

        regime = assess_macro_regime(macro_context)
        logger.info(f"Regime: {regime['regime']} — {regime['reason']}")

        # TIER 1 VETO
        if not regime["allow_longs"]:
            logger.warning(f"TIER 1 VETO: {regime['reason']}")
            tg_mod.send_no_signal_day(regime["reason"], macro_context)
            result["status"] = "skipped_macro_veto"
            return result

        # --------------------------------------------------------
        # STAGE 2: Universe
        # --------------------------------------------------------
        t = time.time()
        universe_df = univ_mod.get_sp500_universe()
        result["stage_timings"]["universe_fetch"] = round(time.time() - t, 2)
        logger.info(f"Universe loaded: {len(universe_df)} tickers")

        ticker_meta = {
            row["ticker"]: {
                "sector": row.get("sector", "Unknown"),
                "sub_industry": row.get("sub_industry", "Unknown"),
            }
            for _, row in universe_df.iterrows()
        }

        # --------------------------------------------------------
        # STAGE 3: Mandatory 5-filter screener + soft RS gap (F7)
        # --------------------------------------------------------
        t = time.time()
        candidates = run_mandatory_screener(universe_df, regime["regime"])
        result["stage_timings"]["screener"] = round(time.time() - t, 2)

        if not candidates:
            msg = "No stocks passed mandatory 5-filter screener"
            logger.warning(msg)
            tg_mod.send_no_signal_day(msg, macro_context)
            result["status"] = "skipped_no_candidates"
            return result

        # --------------------------------------------------------
        # STAGE 4: Haiku quality scoring
        # --------------------------------------------------------
        t = time.time()
        haiku_top = analyst_mod.filter_candidates_haiku(
            candidates,
            top_n=getattr(config, "LLM_HAIKU_TOP_N", 5),
        )
        result["stage_timings"]["haiku_filter"] = round(time.time() - t, 2)
        logger.info(
            f"Haiku top {len(haiku_top)}: "
            f"{[(c['ticker'], c['score']) for c in haiku_top]}"
        )

        promotion_threshold = getattr(config, "CONVICTION_STANDARD", 65)
        promoted = [
            c for c in haiku_top
            if c.get("score", 0) >= promotion_threshold
        ][:getattr(config, "FUNNEL_MAX_DEEP_ANALYSIS", 5)]
        logger.info(f"Promoting {len(promoted)} to Opus deep analysis")

        if not promoted:
            msg = (
                f"No candidates scored >= {promotion_threshold}. "
                f"Top scores: {[(c['ticker'], c['score']) for c in haiku_top[:3]]}"
            )
            logger.warning(msg)
            tg_mod.send_no_signal_day(msg, macro_context)
            result["status"] = "skipped_low_haiku_scores"
            return result

        # --------------------------------------------------------
        # STAGE 5: Opus deep analysis
        # --------------------------------------------------------
        t = time.time()
        deep_signals = []
        for cand in promoted:
            ticker = cand["ticker"]
            full_data = build_full_data_for_opus(
                ticker=ticker,
                df=cand["df"],
                brief_data=cand["brief_data"],
                macro_context=macro_context,
            )
            if full_data is None:
                logger.warning(f"Skipping {ticker} — full data build failed")
                continue
            signal = analyst_mod.opus_deep_analysis(ticker, full_data)
            if "error" not in signal:
                # Carry haiku score forward for shadow log
                signal["_haiku_score"] = cand.get("score", "")
                deep_signals.append(signal)
                logger.info(
                    f"Opus {ticker}: conviction={signal.get('conviction', '?')}"
                )
            else:
                logger.warning(f"Opus failed for {ticker}: {signal.get('error')}")
        result["stage_timings"]["opus_deep"] = round(time.time() - t, 2)

        if not deep_signals:
            msg = "All Opus deep analyses failed"
            logger.warning(msg)
            tg_mod.send_no_signal_day(msg, macro_context)
            result["status"] = "opus_all_failed"
            return result

        # --------------------------------------------------------
        # STAGE 6: Risk manager validation
        # --------------------------------------------------------
        t = time.time()
        validation = risk_mod.validate_and_filter(deep_signals, ticker_meta)
        result["stage_timings"]["risk_validation"] = round(time.time() - t, 2)
        approved = validation.get("approved", [])
        stats = validation.get("stats", {})
        logger.info(
            f"Risk manager: {stats.get('final_count', len(approved))}/"
            f"{stats.get('input_count', len(deep_signals))} signals approved"
        )

        if not approved:
            msg = (
                "All signals failed risk validation. "
                "Common causes: RR < 2:1, conviction < 65, price logic errors."
            )
            logger.warning(msg)
            tg_mod.send_no_signal_day(msg, macro_context)
            result["status"] = "skipped_risk_validation"
            return result

        # --------------------------------------------------------
        # STAGE 7: Send to Telegram
        # --------------------------------------------------------
        t = time.time()
        sent_ok = tg_mod.send_daily_signals(approved, macro_context)
        result["stage_timings"]["telegram_send"] = round(time.time() - t, 2)
        result["signals_sent"] = len(approved) if sent_ok else 0
        result["status"] = "success" if sent_ok else "telegram_failed"

        # --------------------------------------------------------
        # STAGE 8: Shadow log append (Phase 2.3) + Google Sheets mirror (Phase 4a)
        # Runs after Telegram send — non-blocking, never gates the pipeline.
        #
        # _append_shadow_log() writes shadow_log.csv AND returns the exact
        # row list it wrote. We pass that SAME list to sheets_mod so the
        # CSV and the Sheet are always built from one row-construction
        # path (_build_shadow_rows) instead of two parallel copies of the
        # same field-mapping logic drifting apart over time.
        #
        # CSV remains the source of truth. The Sheet is a parallel mirror
        # for easier manual outcome entry from phone/browser — there is
        # still no two-way sync between them once you start hand-editing
        # the manual fields, so pick one as your "working copy" for edits
        # and treat the other as read-only reference, or remember to
        # mirror edits both ways.
        # --------------------------------------------------------
        shadow_rows: List[Dict[str, Any]] = []
        try:
            shadow_rows = _append_shadow_log(approved, run_date)
            tg_mod.send_shadow_log(approved, run_date)
        except Exception as shadow_err:
            logger.warning(f"Shadow log step failed (non-critical): {shadow_err}")

        try:
            if shadow_rows:
                sheets_mod.append_shadow_rows(shadow_rows)
            else:
                logger.warning("Skipping Google Sheets mirror — no shadow rows available")
        except Exception as sheets_err:
            logger.warning(f"Google Sheets mirror failed (non-critical): {sheets_err}")

        total = round(time.time() - start_time, 2)
        logger.info(
            f"PIPELINE COMPLETE — {len(approved)} signals sent in {total}s"
        )

    except Exception as e:
        err_msg = f"Pipeline crashed: {e}\n{traceback.format_exc()}"
        logger.error(err_msg)
        result["status"] = "error"
        result["errors"].append(str(e))
        try:
            tg_mod._send_message(
                f"🔴 *Daily pipeline error*\n\n{str(e)[:500]}"
                f"\n\n_Check GitHub Actions logs._"
            )
        except Exception:
            pass

    return result


# ============================================================
# BLACK SWAN CHECK (every 15 min during US hours)
# ============================================================

def run_black_swan_check() -> Dict[str, Any]:
    """
    Lightweight 15-min intraday monitor.
    Checks VIX spike, SPY move, RSS keywords.
    Does NOT run full screener — Telegram alert only (+ defensive ideas on Type A).
    """
    logger.info("Black swan check starting...")
    result = {"status": "no_event", "alert_sent": False}

    try:
        macro_ctx = data_mod.fetch_market_context()
        vix = macro_ctx.get("vix", 0)
        vix_spike_pct = macro_ctx.get("vix_spike_pct", 0)

        # Get accurate intraday SPY move
        spy_df = data_mod.fetch_ohlcv("SPY", period="5d", interval="1d")
        spy_change_today = 0.0
        if spy_df is not None and len(spy_df) >= 2:
            today_open = float(spy_df["open"].iloc[-1])
            today_close = float(spy_df["close"].iloc[-1])
            spy_change_today = ((today_close - today_open) / today_open) * 100

        # RSS keyword scan
        news = data_mod.fetch_market_news_rss(max_items_per_feed=10)
        bs_news = data_mod.detect_black_swan_keywords(news)

        market_moves = {
            "VIX": f"{vix:.2f} ({vix_spike_pct:+.1f}% intraday)",
            "SPY today": f"{spy_change_today:+.2f}%",
            "DXY 5d": f"{macro_ctx.get('dxy_5d_change_pct', 0):+.2f}%",
        }
        headlines = [n["title"] for n in bs_news[:5]]
        event_summary = {
            "headlines": headlines,
            "market_moves": market_moves,
        }

        # ------------------------------------------------------------
        # TYPE A: CRASH — halt + defensive Opus ideas
        # ------------------------------------------------------------
        type_a_cfg = getattr(config, "BLACK_SWAN_TYPE_A", {})
        type_a_triggers = type_a_cfg.get("triggers", {})
        is_type_a = (
            vix_spike_pct >= type_a_triggers.get("vix_spike_pct_min", 20)
            or spy_change_today <= type_a_triggers.get("spy_drop_pct_max", -3.0)
        )
        if is_type_a:
            logger.warning(
                f"BLACK SWAN TYPE A — VIX spike {vix_spike_pct:+.1f}%, "
                f"SPY {spy_change_today:+.2f}%"
            )
            event_summary["preferred_sectors"] = type_a_cfg.get("preferred_sectors", [])
            trade_ideas = _generate_defensive_ideas(macro_ctx)
            tg_mod.send_black_swan_crash(event_summary, trade_ideas)
            result["status"] = "type_a_crash"
            result["alert_sent"] = True
            return result

        # ------------------------------------------------------------
        # TYPE B: RALLY — alert + sector guidance
        # ------------------------------------------------------------
        type_b_cfg = getattr(config, "BLACK_SWAN_TYPE_B", {})
        type_b_triggers = type_b_cfg.get("triggers", {})
        vix_drop_pct = -vix_spike_pct if vix_spike_pct < 0 else 0
        is_type_b = (
            vix_drop_pct >= type_b_triggers.get("vix_drop_pct_max", 15)
            or spy_change_today >= type_b_triggers.get("spy_rally_pct_min", 3.0)
        )
        if is_type_b:
            logger.info(
                f"BLACK SWAN TYPE B — VIX drop {vix_drop_pct:.1f}%, "
                f"SPY {spy_change_today:+.2f}%"
            )
            event_summary["preferred_sectors"] = type_b_cfg.get("preferred_sectors", [])
            event_summary["skip_sectors"] = type_b_cfg.get("skip_sectors", [])
            tg_mod.send_black_swan_rally(event_summary)
            result["status"] = "type_b_rally"
            result["alert_sent"] = True
            return result

        # ------------------------------------------------------------
        # TYPE C: INFO — RSS keyword match + VIX movement
        # ------------------------------------------------------------
        type_c_cfg = getattr(config, "BLACK_SWAN_TYPE_C", {})
        type_c_triggers = type_c_cfg.get("triggers", {})
        vix_abs_change = abs(vix_spike_pct)
        is_type_c = (
            bs_news
            and vix_abs_change >= type_c_triggers.get("vix_change_pct_abs_min", 10)
        )
        if is_type_c:
            logger.info(f"BLACK SWAN TYPE C — {len(bs_news)} keyword matches, VIX {vix_abs_change:.1f}% move")
            tg_mod.send_black_swan_info(event_summary)
            result["status"] = "type_c_info"
            result["alert_sent"] = True
            return result

        logger.info(
            f"No black swan triggers fired "
            f"(VIX={vix:.2f}, spike={vix_spike_pct:+.1f}%, SPY={spy_change_today:+.2f}%)"
        )

    except Exception as e:
        logger.error(f"Black swan check error: {e}")
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ============================================================
# CLI ENTRY POINT
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SP500 Swing Agent")
    parser.add_argument(
        "--mode",
        choices=["daily", "black_swan", "test"],
        default="daily",
    )
    parser.add_argument(
        "--ticker",
        default="NVDA",
        help="Ticker for single-stock test mode",
    )
    args = parser.parse_args()

    if args.mode == "daily":
        result = run_daily_signals()
    elif args.mode == "black_swan":
        result = run_black_swan_check()
    elif args.mode == "test":
        # Quick single-ticker debug — skips full pipeline
        logger.info(f"=== TEST MODE: {args.ticker} ===")
        macro = data_mod.fetch_market_context()
        regime = assess_macro_regime(macro)
        df = data_mod.fetch_ohlcv(args.ticker, period="6mo", interval="1d")
        brief = ind_mod.build_brief_data(df, ticker=args.ticker, macro_regime=regime["regime"])
        logger.info(f"Brief: {brief}")
        if brief and brief.get("all_mandatory_pass"):
            haiku = analyst_mod.haiku_filter_candidate(args.ticker, brief)
            logger.info(f"Haiku: {haiku}")
        result = {"test": args.ticker, "brief": brief}

    logger.info(f"Final result: {result}")
