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

BLACK SWAN (separate 15-min entry point):
  Type A — Crash:      VIX spike + SPY drop → HALT + defensive ideas via Opus
  Type B — Rally:      VIX drop + SPY surge → continue + sector alert
  Type C — Info:       RSS keyword detection + VIX move → info alert only
"""
import logging
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

    Returns list of candidates with brief_data attached.
    """
    candidates = []
    errors = 0
    total = len(universe_df)
    logger.info(f"Screening {total} tickers via mandatory 5-filter screener...")

    for i, (_, row) in enumerate(universe_df.iterrows()):
        ticker = row["ticker"]
        try:
            df = data_mod.fetch_ohlcv(ticker, period="6mo", interval="1d")
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
    logger.info("=" * 60)
    logger.info("DAILY SIGNAL PIPELINE — STARTING")
    logger.info(f"Run timestamp: {datetime.utcnow().isoformat()}Z")
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
        # STAGE 3: Mandatory 5-filter screener (Stage 2 replacement)
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
