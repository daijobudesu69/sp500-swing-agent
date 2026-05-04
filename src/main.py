"""
Main orchestrator — runs the full daily signal pipeline.

Pipeline:
  1. Fetch universe (S&P 500)
  2. Fetch macro context (SPY, VIX, sector rotation, FRED)
  3. Macro regime check (TIER 1 veto)
  4. Technical screening (funnel: 500 → ~50)
  5. Compute indicators per candidate
  6. Haiku filter (50 → 15)
  7. Opus deep analysis (top 5)
  8. Risk manager validation
  9. Send to Telegram + log to console
  
Two entry points:
  - run_daily_signals(): main 7PM scan
  - run_black_swan_check(): every 15min during US hours
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
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


# ============================================================
# STAGE 1: MACRO REGIME CHECK (TIER 1 veto)
# ============================================================

def assess_macro_regime(macro_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Top-level regime classification.
    Returns {regime, allow_longs, reason}.
    """
    vix = macro_ctx.get("vix", 20.0)
    spy_above_200ma = macro_ctx.get("spy_above_200ma", True)
    vix_panic = macro_ctx.get("vix_panic", False)

    # TIER 1 VETO: VIX panic + SPY below 200MA
    if vix_panic and not spy_above_200ma:
        return {
            "regime": "Risk-Off Bear",
            "allow_longs": False,
            "rr_target": config.MIN_RR_RATIO,
            "reason": f"VIX {vix:.1f} > {config.VIX_PANIC_THRESHOLD} AND SPY below 200MA — capital preservation mode",
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

    # Mixed: SPY above 200MA but VIX elevated, or vice versa
    return {
        "regime": "Mixed / Choppy",
        "allow_longs": True,
        "rr_target": config.MIN_RR_RATIO,
        "reason": "Elevated VIX or weakening trend — be selective",
    }


# ============================================================
# STAGE 2: TECHNICAL SCREENING (cheap filter, no LLM)
# ============================================================

def technical_screen(universe_df: pd.DataFrame, max_candidates: int = 50) -> List[Dict[str, Any]]:
    """
    Funnel ~500 stocks down to ~50 candidates using cheap technical filters.
    No LLM called here.
    """
    candidates = []
    total = len(universe_df)
    logger.info(f"Screening {total} tickers technically...")

    for idx, row in universe_df.iterrows():
        ticker = row["ticker"]
        try:
            # Fetch only daily bars for fast screen
            df_1d = data_mod.fetch_ohlcv(ticker, period="6mo", interval="1d")
            if df_1d is None or len(df_1d) < 50:
                continue

            close = df_1d["close"]
            current_price = float(close.iloc[-1])
            avg_vol = float(df_1d["volume"].tail(20).mean())

            # Filter 1: liquidity & price
            if current_price < config.MIN_PRICE:
                continue
            if avg_vol < config.MIN_AVG_VOLUME:
                continue

            # Filter 2: above 20MA (uptrend confirm)
            sma_20_val = float(close.rolling(20).mean().iloc[-1])
            if current_price < sma_20_val:
                continue

            # Filter 3: RSI in healthy long zone (40-65)
            rsi_series = ind_mod.rsi(close, config.RSI_PERIOD)
            current_rsi = float(rsi_series.iloc[-1])
            if current_rsi < config.RSI_NEUTRAL_LOW or current_rsi > config.RSI_NEUTRAL_HIGH:
                continue

            # Filter 4: volume surge OR steady accumulation
            current_vol = float(df_1d["volume"].iloc[-1])
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0
            # Allow either today's surge OR consistent above-avg volume past 5 days
            recent_vol_avg = float(df_1d["volume"].tail(5).mean())
            steady_volume = recent_vol_avg > avg_vol
            if not (vol_ratio >= config.VOLUME_SURGE_MULTIPLIER or steady_volume):
                continue

            candidates.append({
                "ticker": ticker,
                "sector": row.get("sector", "Unknown"),
                "sub_industry": row.get("sub_industry", "Unknown"),
                "current_price": current_price,
                "rsi": current_rsi,
                "vol_ratio": vol_ratio,
                "_df_1d": df_1d,  # cache for later use
            })

            if (idx + 1) % 50 == 0:
                logger.info(f"  Screened {idx + 1}/{total}, {len(candidates)} survived so far...")

        except Exception as e:
            logger.debug(f"Skipping {ticker}: {e}")
            continue

    logger.info(f"Technical screen: {len(candidates)} candidates from {total} universe")

    # Sort by volume ratio (strongest momentum first), cap at max_candidates
    candidates.sort(key=lambda c: -c["vol_ratio"])
    return candidates[:max_candidates]


# ============================================================
# STAGE 3: COMPUTE INDICATORS (per candidate)
# ============================================================

def compute_brief_data(candidate: Dict[str, Any], macro_regime: str) -> Optional[Dict[str, Any]]:
    """
    Compute lightweight indicator data for Haiku filtering stage.
    Doesn't fetch all timeframes (saves time/API calls).
    """
    ticker = candidate["ticker"]
    df_1d = candidate.get("_df_1d")
    if df_1d is None or len(df_1d) < 200:
        # Refetch with longer period
        df_1d = data_mod.fetch_ohlcv(ticker, period="1y", interval="1d")
        if df_1d is None or len(df_1d) < 200:
            return None

    try:
        traditional = ind_mod.compute_traditional_indicators(df_1d)
        smc = ind_mod.compute_smc_analysis(df_1d)

        if "error" in traditional or "error" in smc:
            return None

        return {
            "sector": candidate.get("sector", "Unknown"),
            "price": traditional.get("price"),
            "rsi": traditional.get("rsi"),
            "rsi_zone": traditional.get("rsi_zone"),
            "macd_histogram": traditional.get("macd_histogram"),
            "macd_histogram_rising": traditional.get("macd_histogram_rising"),
            "macd_bullish_cross_5d": traditional.get("macd_bullish_cross_5d"),
            "above_sma_20": traditional.get("above_sma_20"),
            "above_sma_50": traditional.get("above_sma_50"),
            "above_sma_200": traditional.get("above_sma_200"),
            "volume_ratio": traditional.get("volume_metrics", {}).get("volume_ratio", 0),
            "num_order_blocks": len(smc.get("order_blocks", [])),
            "num_fvgs": len(smc.get("fair_value_gaps", [])),
            "bos_bullish": smc.get("structure", {}).get("bos_bullish", False),
            "choch_bearish": smc.get("structure", {}).get("choch_bearish", False),
            "zone": smc.get("zone", {}).get("zone", "unknown"),
            "htf_aligned_bullish": False,  # filled in deep stage
            "macro_regime": macro_regime,
        }
    except Exception as e:
        logger.error(f"compute_brief_data failed for {ticker}: {e}")
        return None


# ============================================================
# STAGE 4: BUILD FULL DATA (for Opus deep analysis)
# ============================================================

def build_full_data_for_deep_analysis(
    ticker: str,
    macro_context: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Build comprehensive data package for Opus deep analysis.
    Fetches multi-TF, fundamentals, news.
    """
    try:
        tf_data = data_mod.fetch_multi_timeframe(ticker)
        if not tf_data or "1d" not in tf_data:
            return None

        fundamentals = data_mod.fetch_basic_info(ticker)
        news = data_mod.fetch_news_for_ticker(ticker, max_items=5)
        technical = ind_mod.full_technical_analysis(tf_data)

        return {
            "ticker": ticker,
            "fundamentals": fundamentals,
            "technical": technical,
            "news": news,
            "macro_context": macro_context,
        }
    except Exception as e:
        logger.error(f"build_full_data failed for {ticker}: {e}")
        return None


# ============================================================
# DAILY PIPELINE
# ============================================================

def run_daily_signals() -> Dict[str, Any]:
    """
    Main entry point — runs the full daily signal pipeline.
    Triggered by GitHub Actions cron at 12:00 UTC (7PM Jakarta).
    """
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("DAILY SIGNAL PIPELINE — STARTING")
    logger.info("=" * 60)

    result = {
        "status": "unknown",
        "signals_sent": 0,
        "stage_timings": {},
        "errors": [],
    }

    try:
        # ------------------------------------------------------------
        # STAGE 1: Macro context
        # ------------------------------------------------------------
        t = time.time()
        macro_market = data_mod.fetch_market_context()
        macro_econ = data_mod.fetch_macro_snapshot()
        macro_context = {**macro_market, **macro_econ}
        result["stage_timings"]["macro_fetch"] = round(time.time() - t, 2)
        logger.info(f"Macro context: VIX={macro_context.get('vix', '?')}, "
                    f"SPY above 200MA={macro_context.get('spy_above_200ma', '?')}")

        regime = assess_macro_regime(macro_context)
        logger.info(f"Regime: {regime['regime']} — {regime['reason']}")

        # TIER 1 VETO: capital preservation mode
        if not regime["allow_longs"]:
            logger.warning(f"TIER 1 VETO: {regime['reason']} — sending no-signal day")
            tg_mod.send_no_signal_day(regime["reason"], macro_context)
            result["status"] = "skipped_macro_veto"
            return result

        # ------------------------------------------------------------
        # STAGE 2: Universe
        # ------------------------------------------------------------
        t = time.time()
        universe_df = univ_mod.get_sp500_universe()
        result["stage_timings"]["universe_fetch"] = round(time.time() - t, 2)
        logger.info(f"Universe loaded: {len(universe_df)} tickers")

        # Build sector metadata for portfolio constraints
        ticker_meta = {
            row["ticker"]: {
                "sector": row.get("sector", "Unknown"),
                "sub_industry": row.get("sub_industry", "Unknown"),
            }
            for _, row in universe_df.iterrows()
        }

        # ------------------------------------------------------------
        # STAGE 3: Technical screen (500 → ~50)
        # ------------------------------------------------------------
        t = time.time()
        candidates = technical_screen(universe_df, max_candidates=config.FUNNEL_MAX_AFTER_SCREEN)
        result["stage_timings"]["technical_screen"] = round(time.time() - t, 2)
        if not candidates:
            logger.warning("No candidates passed technical screen")
            tg_mod.send_no_signal_day(
                "No stocks passed technical screen (price/volume/trend filters).",
                macro_context,
            )
            result["status"] = "skipped_no_candidates"
            return result

        # ------------------------------------------------------------
        # STAGE 4: Compute brief indicators per candidate
        # ------------------------------------------------------------
        t = time.time()
        haiku_inputs = []
        for cand in candidates:
            brief = compute_brief_data(cand, regime["regime"])
            if brief is None:
                continue
            haiku_inputs.append({
                "ticker": cand["ticker"],
                "sector": cand["sector"],
                "sub_industry": cand["sub_industry"],
                "brief_data": brief,
            })
        result["stage_timings"]["brief_indicators"] = round(time.time() - t, 2)
        logger.info(f"Brief indicators computed for {len(haiku_inputs)} candidates")

        # ------------------------------------------------------------
        # STAGE 5: Haiku filter (50 → 15)
        # ------------------------------------------------------------
        t = time.time()
        haiku_top = analyst_mod.filter_candidates_haiku(
            haiku_inputs, top_n=config.FUNNEL_MAX_AFTER_HAIKU
        )
        result["stage_timings"]["haiku_filter"] = round(time.time() - t, 2)
        logger.info(f"Haiku top {len(haiku_top)}: {[(c['ticker'], c['score']) for c in haiku_top]}")

        # Filter further: only score >= 70 deserves Opus deep analysis
        opus_candidates = [c for c in haiku_top if c.get("score", 0) >= 70]
        opus_candidates = opus_candidates[:config.FUNNEL_MAX_DEEP_ANALYSIS]
        logger.info(f"Promoting {len(opus_candidates)} to Opus deep analysis")

        if not opus_candidates:
            logger.warning("No candidates scored >= 70 from Haiku — skipping day")
            tg_mod.send_no_signal_day(
                "No setups scored above 70 in cheap filter — quality bar not met.",
                macro_context,
            )
            result["status"] = "skipped_low_haiku_scores"
            return result

        # ------------------------------------------------------------
        # STAGE 6: Opus deep analysis (top 5)
        # ------------------------------------------------------------
        t = time.time()
        deep_signals = []
        for cand in opus_candidates:
            ticker = cand["ticker"]
            full_data = build_full_data_for_deep_analysis(ticker, macro_context)
            if full_data is None:
                logger.warning(f"Skipping {ticker} — couldn't build full data")
                continue
            signal = analyst_mod.opus_deep_analysis(ticker, full_data)
            if "error" not in signal:
                deep_signals.append(signal)
                logger.info(f"Opus {ticker}: conviction {signal.get('conviction', '?')}")
            else:
                logger.warning(f"Opus failed for {ticker}: {signal.get('error')}")
        result["stage_timings"]["opus_deep"] = round(time.time() - t, 2)

        # ------------------------------------------------------------
        # STAGE 7: Risk validation
        # ------------------------------------------------------------
        t = time.time()
        validation = risk_mod.validate_and_filter(deep_signals, ticker_meta)
        result["stage_timings"]["risk_validation"] = round(time.time() - t, 2)
        approved = validation["approved"]
        logger.info(f"Risk manager: {validation['stats']['final_count']}/{validation['stats']['input_count']} signals approved")

        if not approved:
            reason = "All signals failed risk validation. Common causes: RR < 2:1, conviction < 65, price logic errors."
            logger.warning(reason)
            tg_mod.send_no_signal_day(reason, macro_context)
            result["status"] = "skipped_risk_validation"
            return result

        # ------------------------------------------------------------
        # STAGE 8: Send to Telegram
        # ------------------------------------------------------------
        t = time.time()
        sent_ok = tg_mod.send_daily_signals(approved, macro_context)
        result["stage_timings"]["telegram_send"] = round(time.time() - t, 2)
        result["signals_sent"] = len(approved) if sent_ok else 0
        result["status"] = "success" if sent_ok else "telegram_failed"

        logger.info(f"PIPELINE COMPLETE: {len(approved)} signals sent in {time.time() - start_time:.1f}s")

    except Exception as e:
        err_msg = f"Pipeline crashed: {e}\n{traceback.format_exc()}"
        logger.error(err_msg)
        result["status"] = "error"
        result["errors"].append(str(e))
        # Try to send error notification
        try:
            tg_mod._send_message(
                f"🔴 *Daily pipeline error*\n\n{str(e)[:500]}\n\n_Check GitHub Actions logs._"
            )
        except Exception:
            pass

    return result


# ============================================================
# BLACK SWAN PIPELINE (every 15min during US hours)
# ============================================================

def run_black_swan_check() -> Dict[str, Any]:
    """
    Lightweight check — only fires alert if thresholds breached.
    Triggered every 15min during US market hours.
    """
    logger.info("Black swan check starting...")
    result = {"status": "no_event", "alert_sent": False}

    try:
        macro_ctx = data_mod.fetch_market_context()
        vix = macro_ctx.get("vix", 0)
        vix_spike = macro_ctx.get("vix_spike_pct", 0)
        spy_5d = macro_ctx.get("spy_5d_change_pct", 0)
        # Use recent SPY change as proxy for "today" — yfinance often misses intraday
        spy_change_today = macro_ctx.get("spy_5d_change_pct", 0) / 5  # rough daily proxy

        # Try to get more accurate today's SPY move
        spy_df = data_mod.fetch_ohlcv("SPY", period="5d", interval="1d")
        if spy_df is not None and len(spy_df) >= 2:
            today_open = spy_df["open"].iloc[-1]
            today_close = spy_df["close"].iloc[-1]
            spy_change_today = ((today_close - today_open) / today_open) * 100

        # Fetch news for keyword detection
        news = data_mod.fetch_market_news_rss(max_items_per_feed=10)
        bs_news = data_mod.detect_black_swan_keywords(news)

        # Build market moves summary
        market_moves = {
            "VIX": f"{vix:.1f} ({vix_spike:+.1f}% intraday)" if vix_spike else f"{vix:.1f}",
            "SPY today": f"{spy_change_today:+.2f}%",
            "DXY 5d": f"{macro_ctx.get('dxy_5d_change_pct', 0):+.2f}%",
        }
        headlines = [n["title"] for n in bs_news[:5]]
        event_summary = {
            "headlines": headlines,
            "market_moves": market_moves,
        }

        # ------------------------------------------------------------
        # TYPE A: CRASH (with immediate trade ideas)
        # ------------------------------------------------------------
        type_a = config.BLACK_SWAN_TYPE_A["triggers"]
        if vix_spike >= type_a["vix_spike_pct_min"] or spy_change_today <= type_a["spy_drop_pct_max"]:
            logger.warning("BLACK SWAN TYPE A detected — generating defensive ideas")
            event_summary["preferred_sectors"] = config.BLACK_SWAN_TYPE_A["preferred_sectors"]
            trade_ideas = _generate_defensive_ideas(macro_ctx)
            tg_mod.send_black_swan_crash(event_summary, trade_ideas)
            result["status"] = "type_a_crash"
            result["alert_sent"] = True
            return result

        # ------------------------------------------------------------
        # TYPE B: RALLY (alert only)
        # ------------------------------------------------------------
        type_b = config.BLACK_SWAN_TYPE_B["triggers"]
        vix_drop = -vix_spike if vix_spike < 0 else 0
        if vix_drop <= type_b["vix_drop_pct_max"] or spy_change_today >= type_b["spy_rally_pct_min"]:
            logger.info("BLACK SWAN TYPE B detected — sending rally alert")
            event_summary["preferred_sectors"] = config.BLACK_SWAN_TYPE_B["preferred_sectors"]
            event_summary["skip_sectors"] = config.BLACK_SWAN_TYPE_B["skip_sectors"]
            tg_mod.send_black_swan_rally(event_summary)
            result["status"] = "type_b_rally"
            result["alert_sent"] = True
            return result

        # ------------------------------------------------------------
        # TYPE C: INFO (only if news matches keywords)
        # ------------------------------------------------------------
        if bs_news and abs(vix_spike) >= config.BLACK_SWAN_TYPE_C["triggers"]["vix_change_pct_abs_min"]:
            logger.info("BLACK SWAN TYPE C — sending info alert")
            tg_mod.send_black_swan_info(event_summary)
            result["status"] = "type_c_info"
            result["alert_sent"] = True
            return result

        logger.info("No black swan triggers fired")

    except Exception as e:
        logger.error(f"Black swan check error: {e}")
        result["status"] = "error"

    return result


def _generate_defensive_ideas(macro_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Quickly generate 3 defensive long ideas during a crash.
    Lightweight — uses Opus on top defensive tickers without full screening.
    """
    defensive_tickers = config.BLACK_SWAN_TYPE_A["preferred_tickers"][:6]
    ideas = []
    for ticker in defensive_tickers:
        full_data = build_full_data_for_deep_analysis(ticker, macro_ctx)
        if full_data is None:
            continue
        signal = analyst_mod.opus_deep_analysis(ticker, full_data)
        if "error" not in signal and signal.get("conviction", 0) >= config.CONVICTION_STANDARD:
            ideas.append(signal)
        if len(ideas) >= 3:
            break
    # Validate via risk manager
    if ideas:
        validation = risk_mod.validate_and_filter(ideas, {})
        return validation["approved"]
    return []


# ============================================================
# CLI ENTRY POINT
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "black_swan", "test"], default="daily")
    args = parser.parse_args()

    if args.mode == "daily":
        result = run_daily_signals()
    elif args.mode == "black_swan":
        result = run_black_swan_check()
    elif args.mode == "test":
        ok = tg_mod.send_test_message()
        result = {"telegram_test": "ok" if ok else "failed"}
    
    logger.info(f"Final result: {result}")
