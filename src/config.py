"""
Central configuration for SP500 Swing Agent — v3 FINAL
All trading rules, thresholds, and parameters live here.
"""
import os
from zoneinfo import ZoneInfo

# ============================================================
# IDENTITY & TIMEZONE
# ============================================================
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
US_EASTERN_TZ = ZoneInfo("America/New_York")

# ============================================================
# API KEYS (loaded from GitHub Secrets / env vars)
# ============================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# CAPITAL & RISK MANAGEMENT
# ============================================================
ACCOUNT_SIZE_USD = 100.0
MAX_RISK_PER_TRADE_PCT = 3.0
MIN_RR_RATIO = 2.0
BULL_REGIME_RR_TARGET = 3.0
MAX_HOLDING_DAYS = 7

# Direction discipline — STRICT LONG ONLY
DIRECTION_BIAS = "LONG_ONLY"
ALLOW_INVERSE_ETF = False              # no SQQQ/SH/SDS even in crashes
SKIP_DAY_IF_NO_EDGE = True             # better no trade than bad trade

# ============================================================
# CONVICTION TIERS
# ============================================================
CONVICTION_HIGH = 80                   # >= 80 = HIGH (max 2x leverage OK)
CONVICTION_STANDARD = 65               # 65-79 = STANDARD (1x only)
CONVICTION_WATCHLIST = 50              # 50-64 = WATCHLIST (no entry)
# < 50 = SKIP entirely

# ============================================================
# SCORING WEIGHTS (sum to 1.0)
# ============================================================
WEIGHT_TECHNICAL = 0.30
WEIGHT_FUNDAMENTAL = 0.25
WEIGHT_MACRO = 0.20
WEIGHT_NEWS_CATALYST = 0.15
WEIGHT_RR_ASYMMETRY = 0.10

# ============================================================
# INDICATOR HIERARCHY — TIER 1/2/3 SYSTEM
# ============================================================
# TIER 1 — VETO POWER (must all pass, else SKIP signal)
TIER1_VETO_INDICATORS = [
    "macro_regime",       # VIX < 25 OR SPY > 200MA
    "htf_trend",          # 1W not in clear downtrend
    "rr_ratio",           # achievable >= 2:1
]

# TIER 2 — PRIMARY (need 2 of 3 to confirm)
TIER2_PRIMARY_INDICATORS = [
    "smc_setup",          # active OB / FVG / BOS pattern
    "volume_confirm",     # vol > 1.5x avg OR OB tested with rising vol
    "mtf_align",          # 1D up + 4H not extreme
]
TIER2_MIN_CONFIRMATIONS = 2

# TIER 3 — SECONDARY (bonus points only, never gate)
TIER3_SECONDARY_INDICATORS = [
    "rsi_position",       # 40-65 range ideal
    "macd_signal",        # bullish histogram or recent cross
    "sector_strength",    # sector ETF outperforming SPY 5d
]

# Conflict resolution priority (when indicators disagree)
INDICATOR_PRIORITY = {
    "smc": 1,             # SMC always wins (price action leads)
    "volume": 2,           # Volume = where smart money is
    "macro": 3,            # Macro for regime, not single trade
    "rsi": 4,              # Lagging
    "macd": 5,             # Lagging
}

# ============================================================
# TIMEFRAMES (multi-TF analysis)
# ============================================================
TF_MEGA_TREND = "1wk"                  # context only — long-term trend check
TF_PRIMARY = "1d"                      # MAIN decision TF (SMC/indicators/levels)
TF_REFINEMENT = "4h"                   # entry zone refinement
TF_ENTRY_TRIGGER = "1h"                # optional manual entry trigger

# Data fetch lookback per TF (bars needed for indicators)
LOOKBACK_BARS_1W = 52                  # ~1 year weekly
LOOKBACK_BARS_1D = 252                 # ~1 year daily (need 200 for SMA200)
LOOKBACK_BARS_4H = 180                 # ~30 trading days
LOOKBACK_BARS_1H = 168                 # ~7 trading days

# ============================================================
# TRADITIONAL INDICATORS
# ============================================================
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_NEUTRAL_LOW = 40                   # below = bearish bias
RSI_NEUTRAL_HIGH = 65                  # above = chasing risk

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

SMA_SHORT = 20
SMA_MEDIUM = 50
SMA_LONG = 200                         # regime filter

VOLUME_SURGE_MULTIPLIER = 1.5          # confirmation threshold
ATR_PERIOD = 14                        # used in SMC + stop sizing

# ============================================================
# SMART MONEY CONCEPTS (SMC) PARAMETERS
# ============================================================
# Swing detection
SMC_SWING_LOOKBACK = 5                 # bars on each side for pivot

# Order Blocks (OB)
SMC_OB_LOOKBACK_BARS = 30
SMC_OB_MIN_MOVE_ATR = 1.5              # impulsive move threshold
SMC_OB_VALIDITY_BARS = 50              # OB invalidated after this

# Fair Value Gaps (FVG)
SMC_FVG_MIN_SIZE_ATR = 0.3
SMC_FVG_LOOKBACK_BARS = 20

# Break of Structure / Change of Character
SMC_BOS_CONFIRMATION_CLOSE = True      # require body close, not wick

# Liquidity Sweep
SMC_LIQUIDITY_WICK_PCT = 0.3
SMC_LIQUIDITY_LOOKBACK = 10

# Premium/Discount Zones (Fibonacci-based)
SMC_PREMIUM_THRESHOLD = 0.5            # above = avoid longs
SMC_DISCOUNT_THRESHOLD = 0.5           # below = favor longs

# ============================================================
# MACRO REGIME FILTER (Jane Street-style preservation)
# ============================================================
VIX_PANIC_THRESHOLD = 25.0             # > this + SPY < 200MA = NO new longs
VIX_BLACK_SWAN_SPIKE_PCT = 30.0        # VIX +30% intraday = alert
SPY_BLACK_SWAN_DAILY_DROP_PCT = -3.0   # SPY -3% intraday = alert
SPY_BLACK_SWAN_RALLY_PCT = 2.0         # SPY +2% on news = risk-on alert
VIX_BLACK_SWAN_DROP_PCT = -25.0        # VIX -25% on news = risk-on alert

# ============================================================
# BLACK SWAN TIERED RESPONSE
# ============================================================
# Type A — RISK-OFF CRASH: generate trade ideas IMMEDIATELY
BLACK_SWAN_TYPE_A = {
    "name": "RISK_OFF_CRASH",
    "triggers": {
        "vix_spike_pct_min": 30.0,
        "spy_drop_pct_max": -3.0,
    },
    "action": "GENERATE_DEFENSIVE_LONGS",
    "preferred_sectors": ["Gold", "Energy", "Defense", "Utilities", "Staples"],
    "preferred_tickers": [
        "GLD", "IAU",                  # gold
        "XOM", "CVX", "OXY",           # oil majors (supply shock benef)
        "LMT", "RTX", "NOC", "GD",     # defense
        "XLU", "XLP",                  # utilities & staples ETFs
    ],
    "skip_sectors": ["Tech", "Discretionary", "Small-caps"],
}

# Type B — RISK-ON RALLY: alert + sectors, NO immediate trades
BLACK_SWAN_TYPE_B = {
    "name": "RISK_ON_RALLY",
    "triggers": {
        "vix_drop_pct_max": -25.0,
        "spy_rally_pct_min": 2.0,
    },
    "action": "ALERT_AND_FLAG_SECTORS",
    "preferred_sectors": ["Tech", "Discretionary", "Small-caps", "Industrials"],
    "skip_sectors": ["Defense", "Gold", "Utilities"],
    "wait_for_next_scan": True,        # don't chase rallies
}

# Type C — UNCERTAIN/MIXED: informational only
BLACK_SWAN_TYPE_C = {
    "name": "MIXED",
    "triggers": {
        "vix_change_pct_abs_min": 15.0,
    },
    "action": "INFORMATIONAL_ALERT",
    "wait_for_next_scan": True,
}

# ============================================================
# UNIVERSE — DYNAMIC FETCHING (S&P 500 + black swan additions)
# ============================================================
# Primary source: GitHub auto-synced CSV (most reliable)
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# Fallback: Wikipedia scrape if GitHub fails
SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Black-swan-only additions (defensive ETFs not in S&P 500)
BLACK_SWAN_EXTRA_TICKERS = ["GLD", "IAU", "XLU", "XLP", "XLE", "XLV"]

# Liquidity & quality filters (applied after fetching universe)
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
MIN_MARKET_CAP = 5_000_000_000

# Funnel sizing (saves cost — most filtering is non-LLM)
FUNNEL_MAX_AFTER_SCREEN = 50           # post technical screen
FUNNEL_MAX_AFTER_HAIKU = 15            # post cheap LLM filter
FUNNEL_MAX_DEEP_ANALYSIS = 5           # final Opus deep dive

# Output cap
MAX_DAILY_SIGNALS = 5
MIN_DAILY_SIGNALS = 0                  # SKIP DAY if nothing qualifies

# Fallback ticker list if both GitHub and Wikipedia fail
FALLBACK_UNIVERSE = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "TSM", "BRK-B", "JPM", "V", "MA", "WMT", "LLY",
    "UNH", "XOM", "ORCL", "PG", "JNJ", "HD", "COST",
]

# ============================================================
# DIVERSIFICATION RULES
# ============================================================
MAX_SIGNALS_PER_SECTOR = 2             # avoid 4 semis on same day
MAX_CORRELATED_SIGNALS = 3             # max same-theme exposure
SECTOR_ETFS_MAP = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# ============================================================
# SCHEDULING (cron expressions in UTC)
# ============================================================
DAILY_SIGNAL_CRON_UTC = "0 12 * * 1-5"           # 7PM Jakarta = 12 UTC
BLACK_SWAN_CRON_UTC = "*/15 13-21 * * 1-5"       # every 15min during US market

# ============================================================
# TELEGRAM FORMATTING
# ============================================================
TELEGRAM_PARSE_MODE = "Markdown"
SIGNAL_HEADER_EMOJI = "📊"
HIGH_CONVICTION_EMOJI = "🔥"
STANDARD_CONVICTION_EMOJI = "📈"
WATCHLIST_EMOJI = "👁️"
BLACK_SWAN_CRASH_EMOJI = "🚨"
BLACK_SWAN_RALLY_EMOJI = "🚀"
BLACK_SWAN_INFO_EMOJI = "⚡"
NO_SIGNAL_EMOJI = "💤"
MACRO_ALERT_EMOJI = "🌍"

# ============================================================
# LLM CONFIG
# ============================================================
LLM_MODEL_FILTER = "claude-haiku-4-5-20251001"   # cheap funnel filter
LLM_MODEL_DEEP = "claude-opus-4-7"                # latest Opus, deep analysis

# Output token caps (input is dynamic, output is capped)
LLM_MAX_TOKENS_FILTER = 512                       # ~$0.0026 max per Haiku call
LLM_MAX_TOKENS_DEEP = 2048                        # ~$0.05 max per Opus call

LLM_TEMPERATURE = 0.3                             # low = consistent reasoning
LLM_USE_PROMPT_CACHING = True                     # save 90% on cached system prompts

# Daily safety cap (prevent runaway costs)
DAILY_LLM_BUDGET_USD = 1.50                       # hard stop if exceeded

# ============================================================
# DATA SOURCES (priority order)
# ============================================================
PRICE_DATA_SOURCE = "yfinance"
MACRO_DATA_SOURCE = "fred"
NEWS_SOURCES = ["finnhub_free", "yfinance_news", "rss_feeds"]
RSS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",       # markets
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
]

# Black swan keyword detection (lowercase, in news headlines)
BLACK_SWAN_KEYWORDS = [
    # War / geopolitical
    "war", "invasion", "attack", "missile", "strike", "ceasefire",
    "nuclear", "sanctions",
    # Financial crisis
    "bank run", "default", "bankruptcy", "collapse", "contagion",
    # Fed / monetary
    "emergency rate", "fed emergency", "rate cut emergency",
    # Pandemic / disaster
    "pandemic", "outbreak", "lockdown",
    # Political
    "impeachment", "resignation",
]
