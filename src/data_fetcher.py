"""
Data fetcher — gets price/indicator/macro/news data for analysis.
All sources are FREE (yfinance, FRED, RSS).
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import pandas as pd
import numpy as np
import yfinance as yf
import feedparser
import requests

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# PRICE DATA (yfinance)
# ============================================================

def fetch_ohlcv(ticker: str, period: str = "1y", interval: str = "1d") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data for a single ticker.
    period: '1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'ytd', 'max'
    interval: '1m', '2m', '5m', '15m', '30m', '60m', '90m', '1h', '1d', '5d', '1wk', '1mo', '3mo'
    """
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            logger.warning(f"No data for {ticker}")
            return None
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        logger.error(f"fetch_ohlcv failed for {ticker}: {e}")
        return None


def fetch_multi_timeframe(ticker: str) -> Dict[str, pd.DataFrame]:
    """
    Fetch all timeframes for a ticker per config.
    Returns dict: {'1wk': df, '1d': df, '4h': df, '1h': df}
    Note: yfinance doesn't have '4h' — we resample from '1h' if needed.
    """
    out = {}

    # 1-week
    df_1w = fetch_ohlcv(ticker, period="2y", interval="1wk")
    if df_1w is not None:
        out["1wk"] = df_1w.tail(config.LOOKBACK_BARS_1W)

    # Daily (primary TF)
    df_1d = fetch_ohlcv(ticker, period="1y", interval="1d")
    if df_1d is not None:
        out["1d"] = df_1d.tail(config.LOOKBACK_BARS_1D)

    # 1-hour
    df_1h = fetch_ohlcv(ticker, period="60d", interval="1h")
    if df_1h is not None:
        out["1h"] = df_1h.tail(config.LOOKBACK_BARS_1H)
        # Resample 1h → 4h for refinement TF
        df_4h = _resample_to_4h(df_1h)
        if df_4h is not None and len(df_4h) > 0:
            out["4h"] = df_4h.tail(config.LOOKBACK_BARS_4H)

    return out


def _resample_to_4h(df_1h: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Resample 1h bars to 4h bars."""
    try:
        df_4h = df_1h.resample("4H").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()
        return df_4h
    except Exception as e:
        logger.error(f"4h resample failed: {e}")
        return None


def fetch_basic_info(ticker: str) -> Dict[str, Any]:
    """
    Fetch fundamental + meta info for a ticker.
    Returns: market_cap, pe_ratio, sector, industry, beta, etc.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info
        return {
            "ticker": ticker,
            "name": info.get("longName", ticker),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "beta": info.get("beta"),
            "profit_margin": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "free_cashflow": info.get("freeCashflow"),
            "next_earnings": info.get("earningsTimestamp"),
            "avg_volume": info.get("averageVolume"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        }
    except Exception as e:
        logger.error(f"fetch_basic_info failed for {ticker}: {e}")
        return {"ticker": ticker, "error": str(e)}


# ============================================================
# MARKET-WIDE DATA (SPY, VIX, sectors)
# ============================================================

def fetch_market_context() -> Dict[str, Any]:
    """
    Fetch SPY, VIX, sector ETFs — used for regime detection.
    """
    out = {}

    # SPY
    spy = fetch_ohlcv("SPY", period="1y", interval="1d")
    if spy is not None and len(spy) > 200:
        spy_close = spy["close"]
        out["spy_price"] = float(spy_close.iloc[-1])
        out["spy_sma20"] = float(spy_close.rolling(20).mean().iloc[-1])
        out["spy_sma50"] = float(spy_close.rolling(50).mean().iloc[-1])
        out["spy_sma200"] = float(spy_close.rolling(200).mean().iloc[-1])
        out["spy_above_200ma"] = out["spy_price"] > out["spy_sma200"]
        out["spy_5d_change_pct"] = float(((spy_close.iloc[-1] / spy_close.iloc[-6]) - 1) * 100)
        out["spy_20d_change_pct"] = float(((spy_close.iloc[-1] / spy_close.iloc[-21]) - 1) * 100)

    # VIX
    vix = fetch_ohlcv("^VIX", period="3mo", interval="1d")
    if vix is not None and len(vix) > 5:
        vix_close = vix["close"]
        out["vix"] = float(vix_close.iloc[-1])
        out["vix_5d_avg"] = float(vix_close.tail(5).mean())
        out["vix_panic"] = out["vix"] > config.VIX_PANIC_THRESHOLD
        # Spike check: today vs yesterday
        if len(vix_close) >= 2:
            spike_pct = ((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1) * 100
            out["vix_spike_pct"] = float(spike_pct)

    # DXY (dollar index proxy)
    dxy = fetch_ohlcv("DX-Y.NYB", period="3mo", interval="1d")
    if dxy is not None and len(dxy) > 5:
        out["dxy"] = float(dxy["close"].iloc[-1])
        out["dxy_5d_change_pct"] = float(((dxy["close"].iloc[-1] / dxy["close"].iloc[-6]) - 1) * 100)

    # Sector ETF performance (5-day)
    sector_perf = {}
    for sector_name, etf in config.SECTOR_ETFS_MAP.items():
        df = fetch_ohlcv(etf, period="1mo", interval="1d")
        if df is not None and len(df) >= 6:
            change_5d = ((df["close"].iloc[-1] / df["close"].iloc[-6]) - 1) * 100
            sector_perf[sector_name] = float(change_5d)
    out["sector_5d_perf"] = sector_perf

    return out


# ============================================================
# MACRO DATA (FRED)
# ============================================================

def fetch_fred_series(series_id: str, limit: int = 30) -> Optional[List[Dict]]:
    """
    Fetch a FRED time series. Common series:
    - DGS10: 10-year Treasury yield
    - DGS2: 2-year Treasury yield
    - DFF: Federal Funds Rate
    - UNRATE: Unemployment rate
    - CPIAUCSL: CPI
    """
    if not config.FRED_API_KEY:
        logger.warning("FRED_API_KEY not set, skipping macro fetch")
        return None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("observations", [])
    except Exception as e:
        logger.error(f"FRED fetch failed for {series_id}: {e}")
        return None


def fetch_macro_snapshot() -> Dict[str, Any]:
    """
    Get key macro indicators in one call.
    """
    out = {}
    series_map = {
        "10y_yield": "DGS10",
        "2y_yield": "DGS2",
        "fed_funds": "DFF",
        "unemployment": "UNRATE",
        "cpi": "CPIAUCSL",
    }
    for name, series_id in series_map.items():
        data = fetch_fred_series(series_id, limit=2)
        if data and len(data) > 0:
            try:
                latest_val = data[0].get("value")
                if latest_val and latest_val != ".":
                    out[name] = float(latest_val)
                    out[f"{name}_date"] = data[0].get("date")
            except (ValueError, TypeError):
                pass
        time.sleep(0.2)  # be polite to FRED API

    # Yield curve inversion check
    if "10y_yield" in out and "2y_yield" in out:
        out["yield_curve_spread"] = out["10y_yield"] - out["2y_yield"]
        out["yield_curve_inverted"] = out["yield_curve_spread"] < 0

    return out


# ============================================================
# NEWS (RSS + yfinance news)
# ============================================================

def fetch_news_for_ticker(ticker: str, max_items: int = 10) -> List[Dict]:
    """
    Get recent news for a specific ticker via yfinance.
    """
    try:
        t = yf.Ticker(ticker)
        news_items = t.news[:max_items] if t.news else []
        return [
            {
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "link": item.get("link", ""),
                "published_ts": item.get("providerPublishTime", 0),
            }
            for item in news_items
        ]
    except Exception as e:
        logger.error(f"News fetch failed for {ticker}: {e}")
        return []


def fetch_market_news_rss(max_items_per_feed: int = 15) -> List[Dict]:
    """
    Aggregate market news from RSS feeds — used for black swan keyword detection.
    """
    all_items = []
    for feed_url in config.RSS_FEEDS:
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
            logger.warning(f"RSS feed failed {feed_url}: {e}")
            continue
    return all_items


def detect_black_swan_keywords(news_items: List[Dict]) -> List[Dict]:
    """
    Scan news headlines for black swan keywords.
    Returns matched items with the trigger keyword.
    """
    matches = []
    keywords = [k.lower() for k in config.BLACK_SWAN_KEYWORDS]
    for item in news_items:
        title_lower = item.get("title", "").lower()
        summary_lower = item.get("summary", "").lower()
        combined = title_lower + " " + summary_lower
        matched_kws = [k for k in keywords if k in combined]
        if matched_kws:
            matches.append({**item, "matched_keywords": matched_kws})
    return matches


# ============================================================
# COMBINED SNAPSHOT (used by analyzer)
# ============================================================

def build_full_snapshot(ticker: str) -> Dict[str, Any]:
    """
    One-shot fetch of everything needed for deep LLM analysis on a ticker.
    """
    snapshot = {
        "ticker": ticker,
        "fetched_at": datetime.utcnow().isoformat(),
    }
    snapshot["price_data"] = fetch_multi_timeframe(ticker)
    snapshot["fundamentals"] = fetch_basic_info(ticker)
    snapshot["news"] = fetch_news_for_ticker(ticker, max_items=5)
    return snapshot


if __name__ == "__main__":
    # Quick local test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("\n=== Market Context ===")
    ctx = fetch_market_context()
    for k, v in ctx.items():
        if k != "sector_5d_perf":
            print(f"  {k}: {v}")
    print(f"\n  Sector 5d performance:")
    for s, p in sorted(ctx.get("sector_5d_perf", {}).items(), key=lambda x: -x[1]):
        print(f"    {s}: {p:+.2f}%")

    print("\n=== Macro Snapshot ===")
    macro = fetch_macro_snapshot()
    for k, v in macro.items():
        print(f"  {k}: {v}")

    print("\n=== Black Swan News Scan ===")
    news = fetch_market_news_rss()
    matches = detect_black_swan_keywords(news)
    print(f"  Total news items: {len(news)}")
    print(f"  Black swan matches: {len(matches)}")
    for m in matches[:3]:
        print(f"    - {m['title'][:80]}... [{', '.join(m['matched_keywords'])}]")
