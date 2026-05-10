"""
data_fetcher.py — Market data layer for SP500 Swing Agent.

STAGE 2 REVISION (May 2026):
============================
REMOVED:
  - fetch_multi_timeframe() — 4H/1H dropped entirely
  - _resample_to_4h() — bug source, no longer needed
  - SMC-specific data building

ADDED:
  - spy_1d_change_pct in fetch_market_context() — black swan detection
  - fetch_weekly_trend() — EMA(8) based weekly trend assessment
    Uses weekly EMA(8) to handle all possible weekly patterns robustly
    Individual candle comparison is too fragile on weekly TF

PRESERVED FROM PHASE 1:
  - feedparser for RSS (cleaner than raw XML)
  - FRED via direct requests (no fredapi dependency)
  - t.history() for single ticker fetch
  - detect_black_swan_keywords() scanning title + summary

COLUMN CONTRACT:
  All DataFrames returned have UPPERCASE columns: Open, High, Low, Close, Volume
  Matches indicators.py expectations.

WEEKLY TREND LOGIC (EMA-based, handles all weekly patterns):
  Bullish:  price > EMA(8) AND EMA(8) rising   → confirmed uptrend
  Bearish:  price < EMA(8) AND EMA(8) falling  → confirmed downtrend
  Neutral:  mixed signals (e.g. big bounce but EMA still falling)
  
  TIER 1 only blocks "bearish". Neutral = allowed through.
  
  Handles edge cases:
  - W1:-5%, W2:+3%, W3:+3% → EMA determines if recovery is confirmed
  - W1:-2%, W2:-2%, W3:+5% → likely neutral (EMA still falling despite bounce)
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import feedparser
import numpy as np
import pandas as pd
import requests
import yfinance as yf

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

# Sector name (yfinance) → ETF ticker
SECTOR_ETF_MAP = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
}

# Default RSS feeds (overridden by config.RSS_FEEDS if set)
DEFAULT_RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]

# Default black swan keywords (overridden by config.BLACK_SWAN_KEYWORDS if set)
DEFAULT_BS_KEYWORDS = [
    "crash", "circuit breaker", "market halt", "flash crash",
    "federal reserve emergency", "emergency rate cut", "systemic risk",
    "bank run", "contagion", "margin call", "black swan",
    "recession fear", "yield curve crisis", "credit crisis",
    "liquidity crisis", "market meltdown", "financial crisis",
]

HTTP_TIMEOUT = 10


# ============================================================
# HELPERS
# ============================================================

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize DataFrame columns to uppercase (Open, High, Low, Close, Volume).
    Handles lowercase (t.history) and MultiIndex (yf.download batch) edge cases.
    """
    if df is None or df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    col_map = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
        "adj close": "Close", "adj_close": "Close",
    }
    df.columns = [col_map.get(c.lower(), c) for c in df.columns]
    return df


def _safe_float(value, default=None):
    try:
        v = float(value)
        return v if not (isinstance(v, float) and np.isnan(v)) else default
    except (TypeError, ValueError):
        return default


# ============================================================
# CORE OHLCV
# ============================================================

def fetch_ohlcv(
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV for a single ticker via yfinance.
    Returns DataFrame with uppercase columns, or None on failure.

    Supported intervals: 1d, 1wk (primary for this system).
    4H/1H removed — Stage 2 decision.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, auto_adjust=True)

        if df is None or df.empty:
            logger.debug(f"No data returned for {ticker}")
            return None

        df = _normalize_columns(df)

        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(df.columns):
            logger.debug(f"Missing columns for {ticker}: {list(df.columns)}")
            return None

        df = df.dropna(subset=["Close", "Volume"])
        if len(df) < 10:
            logger.debug(f"Insufficient bars for {ticker}: {len(df)}")
            return None

        return df

    except Exception as e:
        logger.debug(f"fetch_ohlcv failed for {ticker}: {str(e)[:80]}")
        return None


# ============================================================
# MARKET CONTEXT
# ============================================================

def fetch_market_context() -> Dict[str, Any]:
    """
    Fetch real-time market context: VIX, SPY, DXY, sector performance.

    Returns dict with:
      vix, vix_spike_pct, vix_panic, vix_5d_avg
      spy_price, spy_above_200ma, spy_5d_change_pct, spy_1d_change_pct (NEW)
      spy_sma20, spy_sma50, spy_sma200
      dxy, dxy_5d_change_pct
      sector_5d_perf {sector_name: pct}
    """
    ctx = {
        "vix": 20.0,
        "vix_spike_pct": 0.0,
        "vix_5d_avg": 20.0,
        "vix_panic": False,
        "spy_price": 0.0,
        "spy_above_200ma": True,
        "spy_5d_change_pct": 0.0,
        "spy_1d_change_pct": 0.0,
        "spy_sma20": 0.0,
        "spy_sma50": 0.0,
        "spy_sma200": 0.0,
        "dxy": 100.0,
        "dxy_5d_change_pct": 0.0,
        "sector_5d_perf": {},
    }

    # === SPY ===
    try:
        spy_df = fetch_ohlcv("SPY", period="1y", interval="1d")
        if spy_df is not None and len(spy_df) >= 10:
            close = spy_df["Close"]
            ctx["spy_price"] = round(float(close.iloc[-1]), 2)
            ctx["spy_sma20"] = round(float(close.rolling(20).mean().iloc[-1]), 2)
            ctx["spy_sma50"] = round(float(close.rolling(50).mean().iloc[-1]), 2)

            if len(close) >= 200:
                ctx["spy_sma200"] = round(float(close.rolling(200).mean().iloc[-1]), 2)
                ctx["spy_above_200ma"] = ctx["spy_price"] > ctx["spy_sma200"]

            if len(close) >= 6:
                ctx["spy_5d_change_pct"] = round(
                    ((float(close.iloc[-1]) / float(close.iloc[-6])) - 1) * 100, 2
                )
            if len(close) >= 2:
                ctx["spy_1d_change_pct"] = round(
                    ((float(close.iloc[-1]) / float(close.iloc[-2])) - 1) * 100, 2
                )
    except Exception as e:
        logger.warning(f"SPY fetch failed: {e}")

    # === VIX ===
    try:
        vix_df = fetch_ohlcv("^VIX", period="3mo", interval="1d")
        if vix_df is not None and len(vix_df) >= 2:
            vix_close = vix_df["Close"]
            ctx["vix"] = round(float(vix_close.iloc[-1]), 2)
            ctx["vix_5d_avg"] = round(float(vix_close.tail(5).mean()), 2)
            ctx["vix_panic"] = ctx["vix"] > getattr(config, "VIX_PANIC_THRESHOLD", 25.0)
            ctx["vix_spike_pct"] = round(
                ((float(vix_close.iloc[-1]) / float(vix_close.iloc[-2])) - 1) * 100, 2
            )
    except Exception as e:
        logger.warning(f"VIX fetch failed: {e}")

    # === DXY ===
    try:
        dxy_df = fetch_ohlcv("DX-Y.NYB", period="1mo", interval="1d")
        if dxy_df is not None and len(dxy_df) >= 6:
            dxy_close = dxy_df["Close"]
            ctx["dxy"] = round(float(dxy_close.iloc[-1]), 2)
            ctx["dxy_5d_change_pct"] = round(
                ((float(dxy_close.iloc[-1]) / float(dxy_close.iloc[-6])) - 1) * 100, 2
            )
    except Exception as e:
        logger.debug(f"DXY fetch failed: {e}")

    # === Sector ETF performance ===
    sector_map = getattr(config, "SECTOR_ETFS_MAP", SECTOR_ETF_MAP)
    perf = {}
    for sector, etf in sector_map.items():
        try:
            df = fetch_ohlcv(etf, period="1mo", interval="1d")
            if df is not None and len(df) >= 6:
                c = df["Close"]
                perf[sector] = round(
                    ((float(c.iloc[-1]) / float(c.iloc[-6])) - 1) * 100, 2
                )
        except Exception:
            pass
    ctx["sector_5d_perf"] = perf

    logger.info(
        f"Market: VIX={ctx['vix']:.2f} (spike {ctx['vix_spike_pct']:+.1f}%), "
        f"SPY={ctx['spy_price']:.2f} "
        f"(1d {ctx['spy_1d_change_pct']:+.2f}%, 5d {ctx['spy_5d_change_pct']:+.2f}%), "
        f"above 200MA={ctx['spy_above_200ma']}"
    )
    return ctx


# ============================================================
# FRED MACRO SNAPSHOT (direct requests, no fredapi)
# ============================================================

def fetch_fred_series(series_id: str, limit: int = 5) -> Optional[List[Dict]]:
    """Fetch a FRED time series via direct API request."""
    fred_key = getattr(config, "FRED_API_KEY", None)
    if not fred_key:
        return None
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("observations", [])
    except Exception as e:
        logger.warning(f"FRED fetch failed ({series_id}): {str(e)[:60]}")
        return None


def fetch_macro_snapshot() -> Dict[str, Any]:
    """
    Fetch key macro indicators from FRED.
    Graceful fallback if FRED_API_KEY missing.

    Returns dict with:
      10y_yield, 2y_yield, fed_funds, yield_curve_spread, yield_curve_inverted
    """
    out = {
        "10y_yield": "N/A",
        "2y_yield": "N/A",
        "fed_funds": "N/A",
        "yield_curve_spread": "N/A",
        "yield_curve_inverted": False,
    }

    fred_key = getattr(config, "FRED_API_KEY", None)
    if not fred_key:
        logger.debug("FRED_API_KEY not set — skipping macro snapshot")
        return out

    series_map = {
        "10y_yield": "DGS10",
        "2y_yield": "DGS2",
        "fed_funds": "DFF",
    }

    for name, series_id in series_map.items():
        data = fetch_fred_series(series_id, limit=2)
        if data:
            try:
                val = data[0].get("value")
                if val and val != ".":
                    out[name] = round(float(val), 3)
            except (ValueError, TypeError):
                pass
        time.sleep(0.2)  # polite to FRED API

    # Yield curve spread
    if isinstance(out["10y_yield"], float) and isinstance(out["2y_yield"], float):
        spread = out["10y_yield"] - out["2y_yield"]
        out["yield_curve_spread"] = round(spread, 3)
        out["yield_curve_inverted"] = spread < 0

    return out


# ============================================================
# FUNDAMENTALS
# ============================================================

def fetch_basic_info(ticker: str) -> Dict[str, Any]:
    """
    Fetch fundamental data via yfinance.
    Used in Opus deep analysis prompt.
    """
    info = {
        "ticker": ticker,
        "name": ticker,
        "sector": "Unknown",
        "industry": "Unknown",
        "market_cap": 0,
        "forward_pe": "N/A",
        "peg_ratio": "N/A",
        "beta": "N/A",
        "profit_margin": "N/A",
        "revenue_growth": "N/A",
        "earnings_growth": "N/A",
        "next_earnings": None,
    }
    try:
        t = yf.Ticker(ticker)
        raw = t.info or {}

        info["name"] = raw.get("longName") or raw.get("shortName") or ticker
        info["sector"] = raw.get("sector", "Unknown")
        info["industry"] = raw.get("industry", "Unknown")
        info["market_cap"] = raw.get("marketCap", 0) or 0
        info["forward_pe"] = _safe_float(raw.get("forwardPE"), "N/A")
        info["peg_ratio"] = _safe_float(raw.get("pegRatio"), "N/A")
        info["beta"] = _safe_float(raw.get("beta"), "N/A")
        info["profit_margin"] = _safe_float(raw.get("profitMargins"), "N/A")
        info["revenue_growth"] = _safe_float(raw.get("revenueGrowth"), "N/A")
        info["earnings_growth"] = _safe_float(raw.get("earningsGrowth"), "N/A")
        info["next_earnings"] = raw.get("earningsTimestamp")

    except Exception as e:
        logger.debug(f"fetch_basic_info failed for {ticker}: {str(e)[:60]}")

    return info


# ============================================================
# NEWS
# ============================================================

def fetch_news_for_ticker(
    ticker: str,
    max_items: int = 5,
) -> List[Dict[str, str]]:
    """
    Fetch recent news headlines for a specific ticker via yfinance.
    Returns list of {title, publisher, link, published_ts} dicts.
    """
    results = []
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        for item in news[:max_items]:
            results.append({
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "link": item.get("link", ""),
                "published_ts": str(item.get("providerPublishTime", "")),
            })
    except Exception as e:
        logger.debug(f"Ticker news failed for {ticker}: {str(e)[:60]}")
    return results


def fetch_market_news_rss(
    max_items_per_feed: int = 15,
) -> List[Dict[str, str]]:
    """
    Fetch general market news from RSS feeds using feedparser.
    Used for black swan Type C keyword detection.
    """
    all_items = []
    feeds = getattr(config, "RSS_FEEDS", DEFAULT_RSS_FEEDS)

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:max_items_per_feed]:
                all_items.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:300],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": feed_url,
                })
        except Exception as e:
            logger.warning(f"RSS feed failed ({feed_url[:50]}): {str(e)[:60]}")

    logger.info(f"RSS: {len(all_items)} items from {len(feeds)} feeds")
    return all_items


def detect_black_swan_keywords(
    news_items: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    Scan news title + summary for black swan keywords.
    Returns matched items with trigger keywords listed.
    """
    keywords = [k.lower() for k in getattr(config, "BLACK_SWAN_KEYWORDS", DEFAULT_BS_KEYWORDS)]
    matched = []

    for item in news_items:
        title_lower = item.get("title", "").lower()
        summary_lower = item.get("summary", "").lower()
        combined = title_lower + " " + summary_lower
        matched_kws = [k for k in keywords if k in combined]
        if matched_kws:
            matched.append({**item, "matched_keywords": matched_kws})

    if matched:
        logger.info(
            f"Black swan keywords matched {len(matched)} headlines: "
            f"{[m['title'][:50] for m in matched[:3]]}"
        )
    return matched


# ============================================================
# WEEKLY TREND (EMA-based)
# ============================================================

def fetch_weekly_trend(ticker: str) -> str:
    """
    Assess weekly trend using EMA(8) on weekly closes.
    More robust than comparing individual weekly bars.

    4-state EMA logic:
      price > EMA AND EMA rising  → "bullish"  (confirmed uptrend)
      price < EMA AND EMA falling → "bearish"  (confirmed downtrend)
      price > EMA AND EMA falling → "neutral"  (bounce, EMA not confirmed yet)
      price < EMA AND EMA rising  → "neutral"  (EMA turning, price lagging)

    Examples:
      W1:-5%, W2:+3%, W3:+3% → depends on EMA position
        If price recovered above EMA and EMA started rising → "bullish"
        If EMA still falling despite bounce → "neutral"

      W1:-2%, W2:-2%, W3:+5% → likely "neutral"
        EMA would still be falling after 2 down weeks
        Big W3 bounce puts price near/above EMA but EMA not yet rising
        Correct: "neutral" (not bearish = TIER 1 passes)

    TIER 1 only blocks "bearish". Neutral passes through.

    Returns: "bullish", "bearish", or "neutral"
    """
    try:
        df = fetch_ohlcv(ticker, period="6mo", interval="1wk")

        if df is None or len(df) < 10:
            logger.debug(f"Insufficient weekly bars for {ticker}")
            return "neutral"

        closes = df["Close"].dropna()
        if len(closes) < 10:
            return "neutral"

        # Weekly EMA(8) — standard for weekly timeframe analysis
        ema8 = closes.ewm(span=8, adjust=False).mean()

        current_close = float(closes.iloc[-1])
        current_ema = float(ema8.iloc[-1])
        prev_ema = float(ema8.iloc[-2])

        price_above_ema = current_close > current_ema
        ema_rising = current_ema > prev_ema

        if price_above_ema and ema_rising:
            trend = "bullish"
        elif not price_above_ema and not ema_rising:
            trend = "bearish"
        else:
            trend = "neutral"

        logger.debug(
            f"Weekly trend {ticker}: {trend} "
            f"(close={current_close:.2f}, EMA8={current_ema:.2f}, "
            f"EMA rising={ema_rising})"
        )
        return trend

    except Exception as e:
        logger.debug(f"fetch_weekly_trend failed for {ticker}: {str(e)[:60]}")
        return "neutral"


# ============================================================
# SMOKE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("\n=== OHLCV Test ===")
    df = fetch_ohlcv("NVDA", period="6mo", interval="1d")
    if df is not None:
        print(f"NVDA daily: {len(df)} bars | close ${df['Close'].iloc[-1]:.2f}")
        print(f"Columns: {list(df.columns)}")

    print("\n=== Market Context ===")
    ctx = fetch_market_context()
    for k, v in ctx.items():
        if k != "sector_5d_perf":
            print(f"  {k}: {v}")
    print("  Top sectors by 5d perf:")
    for s, p in sorted(ctx["sector_5d_perf"].items(), key=lambda x: -x[1])[:3]:
        print(f"    {s}: {p:+.2f}%")

    print("\n=== Macro Snapshot ===")
    snap = fetch_macro_snapshot()
    print(snap)

    print("\n=== Fundamentals ===")
    info = fetch_basic_info("NVDA")
    for k, v in info.items():
        print(f"  {k}: {v}")

    print("\n=== Weekly Trend ===")
    for t in ["NVDA", "AMD", "SPY"]:
        trend = fetch_weekly_trend(t)
        print(f"  {t}: {trend}")

    print("\n=== RSS + Black Swan ===")
    news = fetch_market_news_rss(max_items_per_feed=5)
    matches = detect_black_swan_keywords(news)
    print(f"  RSS items: {len(news)} | BS matches: {len(matches)}")
