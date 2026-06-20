"""
config.py — Central configuration for SP500 Swing Agent.

STAGE 2 REVISION (May 2026):
============================
REMOVED:
  - All SMC parameters (Order Blocks, FVGs, BOS, zones, swing detection)
  - Golden cross / death cross detection
  - TIER 2 confirmations system + TIER2_MIN_CONFIRMATIONS
  - TIER 3 secondary indicators list
  - INDICATOR_PRIORITY (was SMC-related)
  - 4H / 1H timeframe references + LOOKBACK_BARS_4H, _1H
  - TF_REFINEMENT, TF_ENTRY_TRIGGER (4H, 1H removed)
  - All SMA references (we use EMA only now)
  - LLM_TEMPERATURE (caused Opus 400 errors)
  - BLACK_SWAN_EXTRA_TICKERS, FALLBACK_UNIVERSE (universe.py handles)

UPDATED:
  - Schedule: 7PM Jakarta → 8AM Jakarta (8:00 + 8:30 backup)
  - Volume threshold: 1.5x → 1.3x
  - RSI: range 40-65 → RSI > 45 (no upper cap)
  - LLM_TEMPERATURE removed entirely

ADDED:
  - Mandatory filter thresholds (tunable):
    EMA_TREND_ANCHOR_PERIOD, ADX_THRESHOLD, RSI_MIN,
    VOLUME_RATIO_MIN, ATR_PCT_MIN
  - LLM_HAIKU_TOP_N (number promoted to Opus)

PHASE 2.3 REVISION (June 2026):
================================
CHANGED:
  - EMA9_MAX_EXTENSION_PCT (2%) → EMA9_MAX_EXTENSION_ATR = 1.5 (ATR units, not %)
    F6 demoted from hard gate to soft Haiku context
  - RS_MIN_GAP_VS_SPY: -0.03 → -0.035
    F7 demoted from hard gate to soft Haiku context
ADDED:
  - SHADOW_LOG_PATH = "shadow_log.csv"

PHASE 2.3 — F7 LOOKBACK REVIEW (June 2026):
============================================
ADDED (shadow-log only — F7 gate logic UNCHANGED, still gates on 10d):
  - RS_60D_LOOKBACK, RS_126D_LOOKBACK, RS_SKIP_RECENT_DAYS
    Feed _compute_momentum_rs_gaps() in main.py to log rs_60d_gap /
    rs_126d_gap to shadow_log.csv alongside the existing 10d gap.
    Rationale: 10-day RS sits in the short-term reversal zone
    (Jegadeesh 1990), while batch data showed losers had HIGHER
    10d RS than winners (loss avg +7.4% vs win avg +2.9%) — the
    filter may be selecting at the top of relative strength right
    before mean-reversion. 60d/126d (skip last 21d per Jegadeesh &
    Titman 1993 / Springer 2022 momentum convention) will be
    compared against 10d at the next filter review before any
    gate change is made. Zero pipeline risk — additive context only.
"""
import os
from zoneinfo import ZoneInfo


# ============================================================
# IDENTITY & TIMEZONE
# ============================================================
JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
US_EASTERN_TZ = ZoneInfo("America/New_York")


# ============================================================
# API KEYS (from GitHub Secrets / env vars)
# ============================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# CAPITAL & RISK MANAGEMENT
# ============================================================
ACCOUNT_SIZE_USD = 100.0                # Update manually as portfolio changes
MAX_RISK_PER_TRADE_PCT = 3.0            # Risk = 3% of current account
MIN_RR_RATIO = 2.0                      # Guideline (warning if below, not hard reject)
BULL_REGIME_RR_TARGET = 3.0             # Stretch target in bull regimes
MAX_HOLDING_DAYS = 7                    # Max swing duration

# Direction discipline — STRICT LONG ONLY
DIRECTION_BIAS = "LONG_ONLY"
ALLOW_INVERSE_ETF = False
SKIP_DAY_IF_NO_EDGE = True


# ============================================================
# CONVICTION TIERS
# ============================================================
CONVICTION_HIGH = 80
CONVICTION_STANDARD = 65                # Promotion threshold to Opus + risk_manager
CONVICTION_WATCHLIST = 50


# ============================================================
# SCORING WEIGHTS (sum to 1.0) — used in Opus system prompt
# ============================================================
WEIGHT_TECHNICAL = 0.30
WEIGHT_FUNDAMENTAL = 0.25
WEIGHT_MACRO = 0.20
WEIGHT_NEWS_CATALYST = 0.15
WEIGHT_RR_ASYMMETRY = 0.10


# ============================================================
# MANDATORY FILTERS (5 — ALL must pass for signal to exist)
# ============================================================
# 1. Trend anchor: Price > EMA(50)
EMA_TREND_ANCHOR_PERIOD = 50

# 2. Trend strength: ADX(14) > threshold AND +DI > -DI
ADX_PERIOD = 14
ADX_THRESHOLD = 25                      # Above 25 = real trend, below 20 = choppy

# 3. Momentum floor: RSI(14) > min
RSI_PERIOD = 14
RSI_MIN = 45                            # Below 45 = weak (no upper cap in Stage 2)

# 4. Volume confirmation: ratio (vs 14d avg) > min
VOLUME_RATIO_PERIOD = 14
VOLUME_RATIO_MIN = 1.3                  # Above-avg activity threshold

# 5. Volatility floor: ATR(14) > min % of price
ATR_PERIOD = 14
ATR_PCT_MIN = 1.5                       # Need movement for 2:1 RR


# ============================================================
# OPTIONAL CONFLUENCE (3 — informational only, NOT gating)
# ============================================================
EMA_FAST_PERIOD = 9
EMA_MEDIUM_PERIOD = 21

# MACD parameters
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


# ============================================================
# SOFT WARNINGS — F6 & F7 (context passed to Haiku, NOT hard gates)
# Phase 2.3: demoted from mandatory filters
# ============================================================
# F6: Anti-exhaustion — extension of close above EMA9, measured in ATR units
# Penalise in Haiku scoring if > 1.5×ATR (extended entry risk)
EMA9_MAX_EXTENSION_ATR = 1.5            # (close - EMA9) / ATR threshold

# F7: Relative strength vs SPY — 10-day return gap
# Penalise in Haiku scoring if < -3.5% (sector laggard risk)
RS_LOOKBACK_DAYS = 10
RS_MIN_GAP_VS_SPY = -0.035              # stock 10d return - SPY 10d return

# F7 lookback review (June 2026) — SHADOW LOG ONLY, does not gate anything.
# 10-day RS sits in the short-term reversal zone (Jegadeesh 1990): batch
# data showed losers had HIGHER 10d RS than winners (+7.4% vs +2.9%),
# consistent with buying right before mean-reversion. These two windows
# let us log a momentum-zone alternative (skip last 21d, per Jegadeesh &
# Titman 1993 / Springer 2022 convention) to compare against the 10d gap
# at the next filter review before deciding whether to re-point F7.
RS_60D_LOOKBACK = 60                    # momentum window #1 — shortest valid horizon
RS_126D_LOOKBACK = 126                  # momentum window #2 — robustness check (~6mo)
RS_SKIP_RECENT_DAYS = 21                # skip last ~1 month before measuring momentum


# ============================================================
# SHADOW LOGGING (Phase 2.3)
# ============================================================
SHADOW_LOG_PATH = "shadow_log.csv"


# ============================================================
# TIER 1 VETO (single tier in Stage 2 — applied by Opus)
# ============================================================
# 1. Macro regime: VIX < panic OR SPY > 200MA
# 2. Weekly trend: must be "bullish" or "neutral" (NOT "bearish")
# (RR removed from TIER 1 — handled as warning by risk_manager)
TIER1_VETO_INDICATORS = ["macro_regime", "weekly_trend"]


# ============================================================
# TIMEFRAMES
# ============================================================
TF_MEGA_TREND = "1wk"                   # Weekly trend assessment (TIER 1 input)
TF_PRIMARY = "1d"                       # MAIN decision timeframe
# TF_REFINEMENT and TF_ENTRY_TRIGGER (4H, 1H) removed


# ============================================================
# MACRO REGIME THRESHOLDS
# ============================================================
VIX_PANIC_THRESHOLD = 25.0              # Above = risk-off mode
VIX_BLACK_SWAN_SPIKE_PCT = 30.0         # Used in Type A trigger
SPY_BLACK_SWAN_DAILY_DROP_PCT = -3.0    # Used in Type A trigger
SPY_BLACK_SWAN_RALLY_PCT = 2.0          # Used in Type B trigger
VIX_BLACK_SWAN_DROP_PCT = -25.0         # Used in Type B trigger


# ============================================================
# BLACK SWAN TIERED RESPONSE
# ============================================================
# Type A — RISK-OFF CRASH: HALT new entries + Opus generates defensive ideas
BLACK_SWAN_TYPE_A = {
    "name": "RISK_OFF_CRASH",
    "triggers": {
        "vix_spike_pct_min": 30.0,
        "spy_drop_pct_max": -3.0,
    },
    "action": "GENERATE_DEFENSIVE_LONGS",
    "preferred_sectors": ["Gold", "Energy", "Defense", "Utilities", "Staples"],
    "preferred_tickers": [
        "GLD", "IAU",                   # gold
        "XOM", "CVX", "OXY",            # oil majors
        "LMT", "RTX", "NOC", "GD",      # defense
        "XLU", "XLP",                   # utilities & staples ETFs
    ],
    "skip_sectors": ["Tech", "Discretionary", "Small-caps"],
}

# Type B — RISK-ON RALLY: alert + sector guidance, no defensive ideas
BLACK_SWAN_TYPE_B = {
    "name": "RISK_ON_RALLY",
    "triggers": {
        "vix_drop_pct_max": -25.0,
        "spy_rally_pct_min": 2.0,
    },
    "action": "ALERT_AND_FLAG_SECTORS",
    "preferred_sectors": ["Tech", "Discretionary", "Small-caps", "Industrials"],
    "skip_sectors": ["Defense", "Gold", "Utilities"],
    "wait_for_next_scan": True,
}

# Type C — INFORMATIONAL: RSS keyword + VIX move alert only
BLACK_SWAN_TYPE_C = {
    "name": "MIXED_INFORMATIONAL",
    "triggers": {
        "vix_change_pct_abs_min": 15.0,
    },
    "action": "INFORMATIONAL_ALERT",
    "wait_for_next_scan": True,
}


# ============================================================
# UNIVERSE
# ============================================================
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Liquidity & quality filters (used by universe.py / data_fetcher)
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
MIN_MARKET_CAP = 5_000_000_000

# Funnel sizing
LLM_HAIKU_TOP_N = 5                     # NEW: number Haiku promotes to Opus
FUNNEL_MAX_DEEP_ANALYSIS = 5            # Cap on Opus calls per run

# Output cap
MAX_DAILY_SIGNALS = 5
MIN_DAILY_SIGNALS = 0                   # Skip day if nothing qualifies


# ============================================================
# DIVERSIFICATION RULES
# ============================================================
MAX_SIGNALS_PER_SECTOR = 2
MAX_CORRELATED_SIGNALS = 3              # Max signals per sub-industry

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
# Daily signal — 8 AM Jakarta = 01:00 UTC, runs Tue-Sat (covers US Mon-Fri sessions)
DAILY_SIGNAL_CRON_PRIMARY_UTC = "0 1 * * 2-6"     # 8:00 AM WIB
DAILY_SIGNAL_CRON_BACKUP_UTC = "30 1 * * 2-6"     # 8:30 AM WIB (backup if primary fails)

# Black swan watcher — every 15min during US market hours (9:30 AM - 4 PM ET)
# UTC: 13:30 - 21:00, Mon-Fri
BLACK_SWAN_CRON_UTC = "*/15 13-21 * * 1-5"


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
MANDATORY_PASS_EMOJI = "🔒"
CONFLUENCE_EMOJI = "➕"
WARNING_EMOJI = "⚠️"


# ============================================================
# LLM CONFIG
# ============================================================
LLM_MODEL_FILTER = "claude-haiku-4-5-20251001"   # Stage 1: cheap quality scoring
LLM_MODEL_DEEP = "claude-opus-4-8"                # Stage 2: deep thesis generation

LLM_MAX_TOKENS_FILTER = 512                       # ~$0.0026 per Haiku call
LLM_MAX_TOKENS_DEEP = 2048                        # ~$0.05 per Opus call

# LLM_TEMPERATURE removed — caused 400 errors on Opus 4.7
LLM_USE_PROMPT_CACHING = True                     # Cost optimization

DAILY_LLM_BUDGET_USD = 1.50                       # Hard stop if exceeded


# ============================================================
# DATA SOURCES
# ============================================================
PRICE_DATA_SOURCE = "yfinance"
MACRO_DATA_SOURCE = "fred"
NEWS_SOURCES = ["yfinance_news", "rss_feeds"]

RSS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
]


# ============================================================
# BLACK SWAN KEYWORD DETECTION (for Type C alerts via RSS)
# ============================================================
BLACK_SWAN_KEYWORDS = [
    # War / geopolitical
    "war", "invasion", "attack", "missile", "strike", "ceasefire",
    "nuclear", "sanctions",
    # Financial crisis
    "bank run", "default", "bankruptcy", "collapse", "contagion",
    "flash crash", "circuit breaker", "market halt",
    # Fed / monetary
    "emergency rate", "fed emergency", "rate cut emergency",
    "systemic risk", "liquidity crisis", "credit crisis",
    # Pandemic / disaster
    "pandemic", "outbreak", "lockdown",
    # Political
    "impeachment", "resignation",
    # Market state
    "black swan", "market meltdown", "financial crisis",
]
