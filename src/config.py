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
ACCOUNT_START_USD = 82.35

# Backwards-compatible alias. Several call sites (risk_manager RULE 4/RULE 5)
# still read ACCOUNT_SIZE_USD. It means STARTING equity, not current equity;
# current equity comes from the running-equity calculation described above.
ACCOUNT_SIZE_USD = ACCOUNT_START_USD

# ============================================================
# POSITION SIZING MODE (Phase 2.6.2)
# ============================================================
# "notional" — capital deployed per trade is fixed at a % of equity, and
#              RISK is whatever the stop distance implies. Selected for
#              Batch 4.
# "risk"     — risk per trade is fixed at a % of equity, and NOTIONAL is
#              whatever the stop distance implies (the Phase 2.6 behaviour).
#
# WHY THE SWITCH: risk-based sizing is standard practice, but it assumes
# margin headroom. On an unleveraged cash account it produces notionals of
# 22-89% of equity for a single position (notional = risk / stop_pct, and
# stop = 1.5 x ATR with F5 flooring ATR at 1.5%). At 2% risk that means
# ONE position can consume most of the account, while the waterfall — which
# deducts RISK, not capital — happily reports room for four more. The cash
# constraint was invisible to the sizing logic.
#
# Notional mode inverts the relationship: capital per trade is predictable
# and bounded, and risk floats with volatility instead.
SIZING_MODE = "notional"

# Capital deployed per trade, as a % of CURRENT equity (waterfall within a
# run: signal 1 sizes off equity, signal 2 off equity minus signal 1's
# NOTIONAL, etc). 4.0 -> at most 25 concurrent positions.
#
# Held CONSTANT for all of Batch 4. A range (e.g. 3-5% by perceived signal
# strength) was considered and rejected: every candidate strength metric —
# Opus conviction, Haiku score, confluence count — has tested non-predictive,
# and 0 of 9 hypotheses passed BH at FDR 5% in Batch 3. Varying size on an
# unvalidated signal would also add a second uncontrolled variable to the
# batch whose entire purpose is the out-of-sample entry_type test, making a
# Batch 3 vs Batch 4 difference unattributable. Record the size you WOULD
# have chosen in the manual `intended_notional_pct` column instead, and test
# it against realised R after the batch closes.
NOTIONAL_PER_TRADE_PCT = 4.0

# Risk per trade as a % of CURRENT equity.
#
# In "risk" mode this is the sizing input. In "notional" mode it becomes a
# CEILING: risk is derived from the stop, and if it would exceed this the
# position is scaled DOWN. This is the mirror image of RULE 5 (the notional
# cap that guarded risk-mode against tight stops) — notional mode needs the
# opposite guard, against very wide stops.
#
# At 4% notional the ceiling only binds above ~33% ATR, which F5's universe
# never produces, so in practice it is a tripwire for a future config change
# rather than an active constraint. Kept at 2.0 so RULE 4 keeps meaning the
# same thing it did in Batch 3.
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
# TRADE GEOMETRY (Phase 2.6.1)
# ============================================================
# These mirror the multipliers stated in the Opus prompt (analyst.py):
#   Stop = Entry - 1.5 x ATR
#   TP1  = Entry + 2.0 x (Entry - Stop)
#   TP2  = Entry + 3.0 x (Entry - Stop)
#
# They exist here because a Stage-2 (fallback) entry fills ABOVE EMA9, and
# stop/TP must be recomputed from that ACTUAL fill or the trade carries a
# wider-than-1R risk wearing a 1R label. Previously these numbers lived only
# inside a prompt string, so nothing else in the codebase could recompute
# geometry — the same "defined in one place, needed in two" failure as the
# unwired filter thresholds found in Phase 2.6 §7.4.
#
# NOTE: 1R is INVARIANT to the fill price. risk_per_share = fill - stop
# = 1.5 x ATR regardless of where the fill lands, so `shares` and `risk_usd`
# do NOT change on a fallback — only entry / stop / TP1 / TP2 shift up, and
# the notional rises by at most ENTRY2_ATR_TOLERANCE x ATR x shares.
STOP_ATR_MULT = 1.5
TP1_R_MULT = 2.0
TP2_R_MULT = 3.0


# ============================================================
# EQUITY ATTRIBUTION (Phase 2.6.1)
# ============================================================
# Batch 4 runs TWO tracks on one shadow log (Phase 2.6 §2.6):
#   - statistical track: EVERY signal is logged and outcome-tracked at full n
#   - execution track:   only the signals the operator actually executed
#
# Equity is REAL CAPITAL and must only ever move on the execution track.
# Without this filter, read_equity() summed r_realized x risk_usd across
# every closed row — so the paper P&L of a VETOED trade would silently move
# the real account balance, and the next run's 2% sizing would be computed
# off a number that does not exist in any brokerage account.
#
# A row contributes to equity ONLY if discretionary_action is in this set.
# MODIFIED counts because the trade was taken; if its size differed from the
# system's, correct it by editing that row's risk_usd, not by excluding it.
EQUITY_COUNTED_ACTIONS = ("TAKEN", "MODIFIED")

# Actions that mean "logged for statistics, never bought". Excluded from
# equity silently and without warning — this is the expected steady state.
EQUITY_EXCLUDED_ACTIONS = ("VETOED", "SKIPPED", "NOT_TAKEN")

# What to do with a CLOSED row that has r_realized + risk_usd but a BLANK
# discretionary_action. It is genuinely ambiguous: it could be an executed
# trade the operator forgot to annotate, or a paper row.
#
# False (default) = exclude it and raise a Telegram warning.
# Deliberately the opposite default to entry_diagnostic.get_taken(), which
# treats blank as TAKEN. That asymmetry is intentional, not an inconsistency:
# a diagnostic that drops rows loses sample; an equity figure that invents
# capital mis-sizes real money. When in doubt, the report guesses and the
# bank account does not.
EQUITY_COUNT_UNMARKED = False


# ============================================================
# VERIFICATION RUN (Phase 2.6.1)
# ============================================================
# A pre-Batch-4 pipeline check is a REAL run: it spends API budget, sends
# real Telegram messages, and writes real rows to the shadow log and the
# pending tab. Those rows are dated BEFORE the batch window and were never
# traded, so they must never reach either track:
#   - equity  (they are not real capital)
#   - Batch 4 statistics (they are outside the batch window)
#
# Set VERIFICATION_RUN=true in the workflow env to have main.py stamp every
# row it writes as VETOED at write time.
#
# WHY AT WRITE TIME, not by cleaning up afterwards: the cleanup depends on
# the operator remembering to do it. This repo already has one 22-day
# silent failure caused by exactly that class of dependency (the Sheets
# secrets that were created but never wired). A flag that tags rows as they
# are written cannot be forgotten — the worst case is a run that was NOT a
# verification being tagged, which is visible and reversible, rather than a
# verification run silently contaminating real equity, which is neither.
VERIFICATION_RUN = os.getenv("VERIFICATION_RUN", "").strip().lower() in ("1", "true", "yes")

# Written into veto_reason. Deliberately greppable and self-explanatory —
# a future session reading the sheet must be able to tell in one glance why
# a cluster of rows is vetoed on a date outside every batch window.
VERIFICATION_VETO_REASON = "VERIFICATION_RUN — pre-Batch-4 pipeline check, never traded"


# ============================================================
# DATA QUALITY FLOORS (Phase 2.6.1)
# ============================================================
# Minimum number of tickers that must return usable OHLCV data before the
# screener's breadth figure is treated as a market reading at all.
#
# Observed 4 Aug 2026, same session, two runs:
#   14:45 UTC (US market open) ->  75/503 fetched, breadth "58.67%"
#   23:12 UTC (US market shut) -> 501/503 fetched, breadth "71.66%"
#
# yfinance rate limiting during US market hours silently removed 85% of the
# universe. Breadth is a RATIO, so it still produced a clean-looking number
# from the 15% that survived. That number feeds pct_above_50dma, one of the
# three regime columns Edge B depends on.
#
# 400/503 ~= 80% coverage. Below that, breadth is written BLANK so the day
# is visibly excludable from regime analysis instead of silently corrupting
# it. Tune only with evidence about what coverage actually distorts breadth
# — this is a guard, not a tuned parameter.
MIN_SCREENED_FOR_BREADTH = 400


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

# ⚠️ NOT ENFORCED (verified by grep, Phase 2.6.3 audit).
# The comment here used to read "used by universe.py / data_fetcher". It is
# not: these three constants are referenced ZERO times outside this file.
# universe.py returns the raw S&P 500 constituent list with no liquidity or
# market-cap screen applied, and never has.
#
# They are NOT being wired now, on purpose. Turning them on would change the
# screener universe mid-project, which is a filter change — and Benjamini-
# Hochberg returned 0/9 on Batch 3, so no filter change is authorised. In
# practice the S&P 500 membership rules already dominate all three thresholds
# (no constituent trades under $5 or below a $5B cap), so the expected effect
# is nil — but "expected nil" is a hypothesis, not a measurement.
#
# Left as documentation of intent. Wire them only with evidence, and only at
# a batch boundary.
MIN_PRICE = 5.0                         # NOT ENFORCED
MIN_AVG_VOLUME = 1_000_000              # NOT ENFORCED
MIN_MARKET_CAP = 5_000_000_000          # NOT ENFORCED

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
# PHASE 2.6.3 — NOT DEPLOYED. This constant described a backup run that does
# not exist in .github/workflows/daily_signal.yml, which has exactly ONE cron.
# It is kept as documentation of a REJECTED design, not as a live schedule:
# a second daily cron would run the whole pipeline twice, sending duplicate
# signals and queueing every pending row twice (the resolver matches on
# (ticker, signal_date), so a duplicate row is ambiguous and would be SKIPPED
# by update_shadow_rows' "len(matches) != 1" guard — silently losing the
# entry_type write-back for that ticker).
# If the primary cron fails, use workflow_dispatch, not a second schedule.
DAILY_SIGNAL_CRON_BACKUP_UTC = None                # deliberately not scheduled

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

# ⚠️ NOT ENFORCED (verified by grep, Phase 2.6.3 audit). The comment used to
# say "Hard stop if exceeded". There is no hard stop: this constant is read
# ZERO times outside this file. Actual per-run cost is bounded structurally
# instead — Haiku runs once per F1-F5 survivor (typically 1-20) and Opus is
# capped at FUNNEL_MAX_DEEP_ANALYSIS calls, so a worst-case run is well under
# $0.40. A mid-run hard stop is deliberately NOT added: truncating a batch
# part-way would change which signals exist that day, making Batch 4
# unattributable for the sake of a cost ceiling that has never been hit.
DAILY_LLM_BUDGET_USD = 1.50                       # NOT ENFORCED — see note


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
