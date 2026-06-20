"""
Universe fetcher — gets the current S&P 500 ticker list.
Strategy: 3-tier fallback for reliability.
  1. GitHub auto-synced CSV (primary, most reliable)
  2. Wikipedia scrape (backup)
  3. Hardcoded fallback list (last resort)

BUGFIX (June 2026):
====================
fetch_fallback() and get_universe_with_blackswan_extras() referenced
config.FALLBACK_UNIVERSE and config.BLACK_SWAN_EXTRA_TICKERS, both of
which were removed from config.py in the Stage 2 revision (see config.py
docstring: "REMOVED: ... BLACK_SWAN_EXTRA_TICKERS, FALLBACK_UNIVERSE").

get_universe_with_blackswan_extras() is not called anywhere in main.py's
daily pipeline (only get_sp500_universe() is), so that bug was dormant.
But get_sp500_universe()'s own 3-tier fallback chain would crash with an
AttributeError if BOTH the GitHub CSV and Wikipedia sources failed on the
same day, since it falls through to fetch_fallback() — defeating the
entire purpose of having a "last resort" fallback (it would itself throw
instead of degrading gracefully).

Fixed by hardcoding the fallback list directly in this module instead of
pulling it from config — keeps this fallback self-contained and immune
to future config.py refactors removing constants it depends on.
"""
import logging
from io import StringIO
from typing import List, Dict

import pandas as pd
import requests

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# HARDCODED FALLBACK UNIVERSE (last resort only)
# ============================================================
# Mega-cap S&P 500 names, deliberately self-contained in this module
# rather than sourced from config.py — see module docstring above for
# why. This is intentionally a small, stable list (large, liquid,
# long-tenured S&P 500 members) rather than an attempt at a full
# 500-ticker mirror — if both GitHub CSV and Wikipedia are down, the
# goal is "don't crash, send something reasonable" not "be complete".
_FALLBACK_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO",
    "TSLA", "BRK-B", "LLY", "JPM", "V", "UNH", "XOM", "MA", "JNJ",
    "PG", "HD", "MRK", "COST", "ABBV", "CVX", "ADBE", "CRM", "BAC",
    "PEP", "KO", "WMT", "MCD", "TMO", "CSCO", "ACN", "NFLX", "ABT",
    "LIN", "ORCL", "AMD", "DHR", "WFC", "DIS", "TXN", "PM", "VZ",
    "INTU", "COP", "CAT", "IBM", "GE", "AMGN",
]


def fetch_from_github() -> pd.DataFrame:
    """
    Primary source: GitHub datasets/s-and-p-500-companies repo.
    Auto-synced CSV with Symbol, Security, GICS Sector, GICS Sub-Industry.
    """
    logger.info("Fetching S&P 500 from GitHub CSV...")
    response = requests.get(config.SP500_CSV_URL, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))
    # Normalize column names
    df.columns = [c.strip() for c in df.columns]
    # Standardize ticker column name
    if "Symbol" in df.columns:
        df = df.rename(columns={"Symbol": "ticker"})
    if "GICS Sector" in df.columns:
        df = df.rename(columns={"GICS Sector": "sector"})
    if "GICS Sub-Industry" in df.columns:
        df = df.rename(columns={"GICS Sub-Industry": "sub_industry"})
    # yfinance uses dash not dot for share classes (BRK.B → BRK-B)
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    logger.info(f"GitHub fetch success: {len(df)} tickers")
    return df[["ticker", "sector", "sub_industry"]]


def fetch_from_wikipedia() -> pd.DataFrame:
    """
    Backup source: Wikipedia S&P 500 list.
    pandas.read_html parses the table directly.
    """
    logger.info("Fetching S&P 500 from Wikipedia...")
    tables = pd.read_html(config.SP500_WIKIPEDIA_URL)
    df = tables[0]  # first table is the constituents
    df.columns = [c.strip() for c in df.columns]
    if "Symbol" in df.columns:
        df = df.rename(columns={"Symbol": "ticker"})
    if "GICS Sector" in df.columns:
        df = df.rename(columns={"GICS Sector": "sector"})
    if "GICS Sub-Industry" in df.columns:
        df = df.rename(columns={"GICS Sub-Industry": "sub_industry"})
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    logger.info(f"Wikipedia fetch success: {len(df)} tickers")
    return df[["ticker", "sub_industry"]] if "sub_industry" in df.columns else df[["ticker"]]


def fetch_fallback() -> pd.DataFrame:
    """
    Last resort: hardcoded mega-cap list (see _FALLBACK_TICKERS above).
    No longer depends on config.FALLBACK_UNIVERSE (removed in Stage 2;
    referencing it here used to crash this exact fallback path).
    """
    logger.warning(
        f"Using hardcoded fallback universe ({len(_FALLBACK_TICKERS)} mega-caps only) "
        f"— both GitHub CSV and Wikipedia sources failed"
    )
    df = pd.DataFrame({
        "ticker": _FALLBACK_TICKERS,
        "sector": ["Unknown"] * len(_FALLBACK_TICKERS),
        "sub_industry": ["Unknown"] * len(_FALLBACK_TICKERS),
    })
    return df


def get_sp500_universe() -> pd.DataFrame:
    """
    Main entry point. Tries each source in order, returns first success.
    Returns DataFrame with columns: ticker, sector, sub_industry
    """
    sources = [
        ("GitHub CSV", fetch_from_github),
        ("Wikipedia", fetch_from_wikipedia),
        ("Hardcoded fallback", fetch_fallback),
    ]
    for source_name, fetch_func in sources:
        try:
            df = fetch_func()
            if df is not None and len(df) > 0:
                logger.info(f"Universe loaded from: {source_name}")
                return df
        except Exception as e:
            logger.warning(f"{source_name} failed: {e}")
            continue
    raise RuntimeError("All universe sources failed")


def get_universe_with_blackswan_extras() -> pd.DataFrame:
    """
    Returns S&P 500 + extra defensive tickers used during black swan events.
    GLD, XLU, XLP, etc. — not in S&P 500 but tradable on Pluang.

    NOTE: this function is not called anywhere in main.py's daily pipeline
    (run_daily_signals() and run_black_swan_check() both source defensive
    tickers from config.BLACK_SWAN_TYPE_A["preferred_tickers"] instead,
    which DOES still exist in config.py). This function previously
    referenced config.BLACK_SWAN_EXTRA_TICKERS, also removed in Stage 2 —
    fixed below by sourcing the same defensive list main.py already uses,
    so there is only one place defensive tickers are defined.
    """
    base = get_sp500_universe()
    defensive_tickers = getattr(
        config, "BLACK_SWAN_TYPE_A", {}
    ).get("preferred_tickers", [])

    extras = pd.DataFrame({
        "ticker": defensive_tickers,
        "sector": ["ETF"] * len(defensive_tickers),
        "sub_industry": ["Defensive"] * len(defensive_tickers),
    })
    combined = pd.concat([base, extras], ignore_index=True)
    combined = combined.drop_duplicates(subset=["ticker"], keep="first")
    return combined


def get_sector_breakdown(df: pd.DataFrame) -> Dict[str, int]:
    """Quick sanity check: how many tickers per sector."""
    if "sector" not in df.columns:
        return {}
    return df["sector"].value_counts().to_dict()


if __name__ == "__main__":
    # Quick local test (run with: python -m src.universe)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    df = get_universe_with_blackswan_extras()
    print(f"\nTotal tickers: {len(df)}")
    print(f"\nSector breakdown:")
    for sector, count in sorted(get_sector_breakdown(df).items(), key=lambda x: -x[1]):
        print(f"  {sector}: {count}")
    print(f"\nFirst 10 tickers: {df['ticker'].head(10).tolist()}")
