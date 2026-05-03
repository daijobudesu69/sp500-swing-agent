"""
Universe fetcher — gets the current S&P 500 ticker list.
Strategy: 3-tier fallback for reliability.
  1. GitHub auto-synced CSV (primary, most reliable)
  2. Wikipedia scrape (backup)
  3. Hardcoded fallback list (last resort)
"""
import logging
from io import StringIO
from typing import List, Dict

import pandas as pd
import requests

from src import config

logger = logging.getLogger(__name__)


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
    return df[["ticker", "sector", "sub_industry"]]


def fetch_fallback() -> pd.DataFrame:
    """Last resort: hardcoded mega-cap list."""
    logger.warning("Using hardcoded fallback universe (mega-caps only)")
    df = pd.DataFrame({
        "ticker": config.FALLBACK_UNIVERSE,
        "sector": ["Unknown"] * len(config.FALLBACK_UNIVERSE),
        "sub_industry": ["Unknown"] * len(config.FALLBACK_UNIVERSE),
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
    """
    base = get_sp500_universe()
    extras = pd.DataFrame({
        "ticker": config.BLACK_SWAN_EXTRA_TICKERS,
        "sector": ["ETF"] * len(config.BLACK_SWAN_EXTRA_TICKERS),
        "sub_industry": ["Defensive"] * len(config.BLACK_SWAN_EXTRA_TICKERS),
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
