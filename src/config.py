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

PHASE 2.5 — BLACK SWAN REDESIGN (June 2026):
================================================
ADDED:
  - BLACK_SWAN_MARKER_PATH    — dedup marker for Type A alerts (main.py
    run_black_swan_check() writes the run date here only after a Type A
    alert sends successfully; reads it first to skip if already alerted
    today, capping it at 1 alert/UTC-day even if the crash condition
    stays true across many consecutive 15-min runs).
  - BLACK_SWAN_HEARTBEAT_PATH — weekly keepalive marker (main.py
    run_black_swan_heartbeat() always writes the run date here every
    Friday, regardless of Telegram send status — this is what keeps the
    repo "active" so GitHub doesn't auto-disable the scheduled workflow
    after ~60 days with no commits in a quiet market).

NOTE — left intentionally untouched (Phase 2.5):
  BLACK_SWAN_TYPE_B, BLACK_SWAN_TYPE_C, BLACK_SWAN_RALLY_EMOJI,
  BLACK_SWAN_INFO_EMOJI, SPY_BLACK_SWAN_RALLY_PCT, VIX_BLACK_SWAN_DROP_PCT
  are no longer read by anything (run_black_swan_check() now only handles
  Type A; send_black_swan_rally()/send_black_swan_info() were deleted
  from telegram_sender.py). Left in place rather than deleted — orphaned
  config is harmless at zero runtime cost, and removing it risks breaking
  something outside this file's visibility. Safe to clean up later as a
  separate, isolated change if desired.

PHASE 2.6 — BATCH 4 REAL-CAPITAL PREP (August 2026):
====================================================
CHANGED:
  - MAX_RISK_PER_TRADE_PCT: 3.0 -> 2.0
    Reason (structural, NOT an alpha/filter change): at 3% risk the required
    notional is risk / stop_pct. Stop = 1.5 x ATR and F5 floors ATR at 1.5%,
    so stop can be as tight as 2.25% -> notional up to 133% of equity, which
    risk_manager RULE 5 (position <= account) rejects. Any signal with ATR
    between 1.5% and 2.0% was therefore mathematically un-sizeable, and that
    — not LLM arithmetic — is the real root cause of the recurring
    "sizing mismatch" warnings (ADM, URI, JPM, CVS).
    At 2% the constraint is stop_pct >= risk_pct -> 1.5 x ATR >= 2.0%
    -> ATR >= 1.333%. F5's 1.5% floor already guarantees this, so the
    notional cap can never bind and risk is always exactly 2%.
    Worst case (ATR 1.5%): notional = 88.9% of equity. Fits.
    NOTE: this leaves every F1-F7 threshold untouched, so it does NOT
    violate the Batch 3 "do not change filters" rule (Benjamini-Hochberg
    0/9). R-multiples are scale-free and unaffected.

  - MAX_HOLDING_DAYS: 7 -> 10
    Config said 7 for the whole of Batch 3 while the operating rule was
    10 TRADING days (and TIME_TP / TIME_SL are defined at hold >= 10).
    risk_manager RULE 7 and the Opus prompt both hard-gate on this value,
    so they were enforcing a limit nobody was actually trading to.
    10 trading days ~= 14 calendar days = the stated 2-week max hold.

ADDED:
  - ACCOUNT_START_USD        — Batch 4 starting equity, the base of the
    running-equity calculation. ACCOUNT_SIZE_USD is kept as an alias for
    backwards compatibility (risk_manager reads it) but is now DERIVED and
    should be treated as "starting equity", not "current equity".
  - EQUITY_CACHE_PATH        — fallback cache for running equity
  - EQUITY_STALE_WARN_DAYS   — warn when a row is still outcome-less past this
  - ENTRY2_ATR_TOLERANCE     — two-stage entry zone width, in ATR units
  - PENDING_ENTRIES_TAB      — Google Sheets tab holding near-miss state
  - PENDING_ENTRY_EXPIRY_DAYS— how long a near-miss stays executable

WIRING FIX (silent no-op, found reading the code):
  - ADX_THRESHOLD / RSI_MIN / VOLUME_RATIO_MIN / ATR_PCT_MIN /
    EMA_TREND_ANCHOR_PERIOD were defined here but referenced ZERO times
    outside this file. indicators.py hard-coded 25 / 45 / 1.3 / 1.5 inline,
    so editing this file changed nothing at all. indicators.py now reads
    these constants. Any future threshold edit here is now real — which
    also means the "config freeze" discipline finally applies to a file
    that actually controls something.
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
# Batch 4 starting equity. Running equity is computed at runtime as
#   equity = ACCOUNT_START_USD + sum(r_realized * risk_usd) over CLOSED rows
# read from the Google Sheet shadow log. Unfilled and FLOAT rows are skipped
# automatically because they have no outcome. Do NOT edit this mid-batch to
# "update the balance" — that would double-count realised P&L.
ACCOUNT_START_USD = 100.0

# Backwards-compatible alias. Several call sites (risk_manager RULE 4/RULE 5)
# still read ACCOUNT_SIZE_USD. It means STARTING equity, not current equity;
# current equity comes from the running-equity calculation described above.
ACCOUNT_SIZE_USD = ACCOUNT_START_USD

# Risk per trade, as a % of CURRENT equity (waterfall within a run:
# signal 1 sizes off equity, signal 2 off equity minus signal 1's risk, etc).
# 3.0 -> 2.0 in Phase 2.6 for the structural reason in the module docstring.
MAX_RISK_PER_TRADE_PCT = 2.0

MIN_RR_RATIO = 2.0                      # Guideline (warning if below, not hard reject)
BULL_REGIME_RR_TARGET = 3.0             # Stretch target in bull regimes

# Max swing duration, in TRADING days (~14 calendar days = the 2-week cap).
# Hard-gated by risk_manager RULE 7 and by the Opus prompt's max_holding_days
# range — both must stay in sync with this number.
MAX_HOLDING_DAYS = 10

# ---- Running equity: fallback cache (Phase 2.6) ----
# main.py computes equity from the Sheet each run and writes the result here.
# If the Sheets read fails, the cached value is used instead AND a Telegram
# warning is sent, so a silent fall back to starting equity can never happen.
EQUITY_CACHE_PATH = ".last_equity"

# Warn (never block) when a shadow-log row still has a blank outcome after
# this many trading days. A stale row means realised P&L is missing from the
# equity calculation, so sizing is running off an out-of-date balance.
EQUITY_STALE_WARN_DAYS = 10

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
# TWO-STAGE ENTRY (Phase 2.6)
# ============================================================
# Stage 1 (unchanged): limit order at EMA9.
# Stage 2 (new): if the session low entered [EMA9, EMA9 + tol x ATR] but the
# limit never filled, the signal is queued as a near-miss and executed at
# market on the next open. Stop/TP are recomputed from the ACTUAL fill price,
# not from EMA9, so 1R stays consistent.
#
# 0.15 is an IN-SAMPLE fit on 5 observed Batch 3 near-misses
# (MNST 0.05 / BALL 0.12 / JPM 0.30 / DLTR 0.33 / CAT 0.34, in ATR units).
# It catches 2 of 5 — chosen deliberately over the 0.35 that catches 5 of 5,
# to limit entry-price degradation on signals that would have filled normally.
# PROVISIONAL. Must be re-evaluated OUT-OF-SAMPLE on Batch 4 via the
# entry_type (strict/fallback) column before it is treated as final.
ENTRY2_ATR_TOLERANCE = 0.15

# Google Sheets tab holding cross-day near-miss state. Deliberately a plain
# constant, NOT a new GitHub Secret: the Sheets secrets were once created
# correctly but never wired into the workflow env block, and the mirror
# silently no-op'd for 22 days. Fewer secrets, fewer places to forget.
PENDING_ENTRIES_TAB = "pending_entries"

# A near-miss is executable on the NEXT trading day only. Anything older is
# marked EXPIRED and never traded — without this, a stale near-miss from
# weeks ago could fire on an unrelated price.
PENDING_ENTRY_EXPIRY_DAYS = 1


# ============================================================
# BLACK SWAN MARKERS (Phase 2.5)
# ============================================================
# Type A dedup — main.py reads this before alerting; writes the run date
# only after a successful send. Caps Telegram spam to 1 alert/UTC-day.
BLACK_SWAN_MARKER_PATH = ".last_alert_date"

# Weekly heartbeat keepalive — main.py always writes the run date here
# every Friday run, independent of Telegram send status. The commit this
# produces is what prevents GitHub from auto-disabling the scheduled
# workflow during long stretches with no other repo activity.
BLACK_SWAN_HEARTBEAT_PATH = ".last_heartbeat"


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
SPY_BLACK_SWAN_RALLY_PCT = 2.0          # Was used in Type B trigger (Type B removed, Phase 2.5)
VIX_BLACK_SWAN_DROP_PCT = -25.0         # Was used in Type B trigger (Type B removed, Phase 2.5)


# ============================================================
# BLACK SWAN TIERED RESPONSE
# ============================================================
# Type A — RISK-OFF CRASH: alert only (Phase 2.5 — defensive-idea
# generation removed; preferred_sectors/preferred_tickers/skip_sectors
# below are now unused by run_black_swan_check() but kept here in case
# they're useful reference data later).
BLACK_SWAN_TYPE_A = {
    "name": "RISK_OFF_CRASH",
    "triggers": {
        "vix_spike_pct_min": 30.0,
        "spy_drop_pct_max": -3.0,
    },
    "action": "ALERT_ONLY",             # Phase 2.5: was GENERATE_DEFENSIVE_LONGS
    "preferred_sectors": ["Gold", "Energy", "Defense", "Utilities", "Staples"],
    "preferred_tickers": [
        "GLD", "IAU",                   # gold
        "XOM", "CVX", "OXY",            # oil majors
        "LMT", "RTX", "NOC", "GD",      # defense
        "XLU", "XLP",                   # utilities & staples ETFs
    ],
    "skip_sectors": ["Tech", "Discretionary", "Small-caps"],
}

# Type B — RISK-ON RALLY: DEPRECATED, Phase 2.5 — no longer detected by
# run_black_swan_check(), no longer sent by telegram_sender.py. Kept here
# (orphaned, zero runtime cost) rather than deleted.
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

# Type C — INFORMATIONAL: DEPRECATED, Phase 2.5 — same status as Type B above.
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
# UTC: 13:00 - 21:00, Mon-Fri (widened window to safely cover both EST/EDT)
BLACK_SWAN_CRON_UTC = "*/15 13-21 * * 1-5"

# Black swan heartbeat (Phase 2.5) — weekly, Friday 23:00 UTC (after US close
# in both EST and EDT)
BLACK_SWAN_HEARTBEAT_CRON_UTC = "0 23 * * 5"


# ============================================================
# TELEGRAM FORMATTING
# ============================================================
TELEGRAM_PARSE_MODE = "Markdown"

SIGNAL_HEADER_EMOJI = "📊"
HIGH_CONVICTION_EMOJI = "🔥"
STANDARD_CONVICTION_EMOJI = "📈"
WATCHLIST_EMOJI = "👁️"
BLACK_SWAN_CRASH_EMOJI = "🚨"
BLACK_SWAN_RALLY_EMOJI = "🚀"            # DEPRECATED Phase 2.5 — Type B removed
BLACK_SWAN_INFO_EMOJI = "⚡"             # DEPRECATED Phase 2.5 — Type C removed
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
# BLACK SWAN KEYWORD DETECTION
# Still used by run_black_swan_check() (Type A headline context) even
# though Type C itself is deprecated — detect_black_swan_keywords() is
# now called directly for crash headline matching, not for a Type C alert.
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
