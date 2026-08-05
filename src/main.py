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
  9. Shadow log append (CSV + Telegram + Google Sheets)

PHASE 2.3 REVISION (June 2026):
================================
ADDED:
  - RS gap vs SPY (soft F7 context) computed once before screener loop,
    stored as rs_gap_vs_spy_pct in each candidate dict. NOT a hard gate.
  - Shadow log append after send_daily_signals():
    Writes auto-logged fields to shadow_log.csv (config.SHADOW_LOG_PATH).
    Manual outcome fields (_shadow_ema9_filled, _shadow_outcome, etc.)
    are left blank for manual fill-in after trade closes.

PHASE 2.3 — F7 LOOKBACK REVIEW (June 2026):
============================================
ADDED (shadow-log only — NOT a gate, F7 gate logic unchanged):
  - rs_60d_gap and rs_126d_gap computed alongside the existing 10d RS gap.
    Both use the "skip last 21 trading days" momentum convention
    (Jegadeesh & Titman 1993 / Springer 2022 momentum-reversal literature)
    to avoid contaminating the momentum window with short-term reversal noise.
    Window: t-(N+21) -> t-21, where N = 60 or 126.
  - Purpose: at the next filter review, compare predictive power of
    10d (current F7) vs 60d vs 126d RS gap against closed-trade outcomes.
    If 60d/126d separates win/loss better than 10d, F7 gets re-pointed.
    Zero pipeline risk — these are additional context fields only.

PHASE 4a REVISION (June 2026):
================================
ADDED:
  - Google Sheets mirror of the shadow log via src/sheets_logger.py.
    Wired into STAGE 8, runs after the existing CSV append + Telegram
    shadow log message. Non-blocking — failure here never stops the
    pipeline or affects signal delivery (same try/except contract as
    the existing shadow log step).
  - IMPORTANT: the CSV (shadow_log.csv) remains the source of truth.
    The Sheet is a parallel mirror for easier manual outcome entry
    from phone/browser. The two are written independently from two
    separately-built row dicts — there is no two-way sync. If you
    edit the manual fields in one, remember to mirror the edit in
    the other, or pick one as your single working copy going forward.
  - _shadow_outcome categories (manual field — fill in after a trade closes):
      SL                  -> stopped out before TP1
      TP1_TRAIL           -> TP1 hit, 50% locked +2R, remainder trailed to exit
      TP2                 -> full target hit (+3R)
      INVALIDATED_LOSS    -> discretionary exit pre-TP1, exit price < entry
      INVALIDATED_PROFIT  -> discretionary exit pre-TP1, exit price > entry
      FLOAT               -> still open at time of review (mark-to-market)

PHASE 2.5 — BLACK SWAN REDESIGN (June 2026):
================================================
CHANGED — run_black_swan_check():
  - Type B (rally) & Type C (info) DELETED entirely — less noise,
    decision is the user's, not the system's.
  - NO LLM anywhere in black swan path. _generate_defensive_ideas()
    DELETED — Opus is not predictive on conviction (proven in Phase 2.2
    data: losers averaged HIGHER conviction than winners), so it has no
    business picking defensive tickers in a panic either.
  - "SPY drop" now measured via macro_ctx["spy_1d_change_pct"]
    (prev close -> now), NOT intraday open->now. This fixes a latent bug:
    the old code read spy_df["open"]/["close"] (lowercase), but
    data_fetcher._normalize_columns() always returns UPPERCASE columns
    (Open/High/Low/Close/Volume) — so the old lowercase access always
    KeyError'd, was swallowed by try/except, and the function silently
    never fired. spy_1d_change_pct also correctly captures overnight
    gap-downs, which intraday open->now would miss entirely.
  - Dedup added: max 1 Type A alert per UTC calendar day via a marker
    file (config.BLACK_SWAN_MARKER_PATH), committed back to the repo by
    the GitHub Actions workflow. Prevents Telegram spam if a crash stays
    triggered across many consecutive 15-min runs.
  - Alert is informational only — no "HALT" instruction, no defensive
    sector list. The trade decision belongs to the user.
  - Headline context: keyword-matched black-swan headlines first; if
    none match, falls back to top-3 general market headlines. Either
    way the message explicitly states the cause is NOT verified by the
    system and tells the user to research independently.
ADDED — run_black_swan_heartbeat() (new function):
  - Runs once a week (Friday 23:00 UTC, via a second cron entry in the
    same workflow file as the Type A watcher).
  - Reports current VIX/SPY plus this week's worst single-day SPY return
    and largest single-day VIX spike, each compared against the Type A
    thresholds — so the heartbeat also quietly proves the comparison
    logic is reading real data correctly, not just "I'm alive".
  - ALWAYS writes config.BLACK_SWAN_HEARTBEAT_PATH, committed weekly by
    the workflow. This is a deliberate keepalive: GitHub auto-disables
    scheduled workflows on a repo with no commits for ~60 days, which is
    a real risk here in a quiet market (shadow_log.csv is intentionally
    NOT committed, and the Type A marker only commits on a crash day).
    The weekly heartbeat commit keeps the repo "active" so cron schedules
    never silently stop firing.
  - No dedup needed — its own cron only fires once a week.

PHASE 2.6 — REAL CAPITAL PREP (August 2026):
================================================
Batch 4 is the first batch executed with real money, on a $100 account.
Everything below exists to make that batch READABLE, not to raise
expectancy. No F1-F7 threshold is touched (Benjamini-Hochberg on Batch 3
returned 0/9 significant hypotheses — there is no evidence to act on yet).

ADDED — running equity + waterfall sizing:
  Risk is now 2% of CURRENT equity, not a fixed dollar amount. Equity is
  derived from CLOSED shadow-log rows via sheets_logger.read_equity():

      equity = ACCOUNT_START_USD + sum(r_realized * risk_usd)

  Unfilled and FLOAT rows have no r_realized and are skipped automatically.
  Within one run, same-day signals cascade off a running basis:
      signal 1 -> 2% of equity
      signal 2 -> 2% of (equity - signal 1 risk)
      signal 3 -> 2% of (equity - signal 1 - signal 2)
  Cascade order is Haiku score descending (see "HAIKU IS NOW RANK-ONLY").

  WHY 3% -> 2% (structural, not an alpha change): at 3% the required
  notional is risk / stop_pct. Stop is 1.5 x ATR and F5 floors ATR at 1.5%,
  so stop can be as tight as 2.25% -> notional up to 133% of the account,
  which risk_manager RULE 5 rejects. RULE 4 and RULE 5 were mutually
  unsatisfiable for any signal with ATR between 1.5% and 2.0% — that, not
  LLM arithmetic, was the true cause of the long-running "sizing mismatch"
  warnings. At 2%, stop >= 2.25% > 2.00% always, so the conflict is gone
  and RULE 4 could be restored to a hard gate.

ADDED — Sheets read path is now on the sizing critical path:
  This module's long-standing contract is that Sheets failures never block
  signal delivery. That contract is PRESERVED via a cache: the last good
  equity read is written to config.EQUITY_CACHE_PATH and committed back to
  the repo by the workflow. On a failed read the cached value is used AND a
  Telegram warning is sent. Silently falling back to ACCOUNT_START_USD is
  explicitly forbidden — it would mis-size every trade with no symptom.

ADDED — two-stage entry (STAGE 0 + STAGE 9):
  Stage 1 is unchanged: a limit order at EMA9.
  Stage 2 is new: if the session low entered [EMA9, EMA9 + 0.15 x ATR] but
  the limit never filled, the signal is executed at market on the next open.

  The pipeline was STATELESS across days, so this needed somewhere to
  remember yesterday. Chosen: a separate Sheets tab (PENDING_ENTRIES_TAB)
  rather than threading state through the main flow. Reason: anything that
  fails there must not be able to block signal delivery, and a separate tab
  is also immune to future column changes in shadow_log.

  STAGE 9 (end of run) queues every signal just sent as PENDING.
  STAGE 0 (start of next run) reads them back, looks at the session that
  has since completed, and resolves each one:
      low <= ema9        -> FILLED     (entry_type "strict")
      low <= zone_high   -> FALLBACK   (entry_type "fallback", Stage 2 alert)
      otherwise          -> EXPIRED
  Expiry is one trading day: without it a weeks-old near-miss could fire on
  an unrelated price. This also yields an automatic fill-rate metric, which
  previously had to be reconstructed by hand.

  NOTE: the system never learns the ACTUAL fill price — the operator
  executes manually and types it into the pending tab's actual_entry column.
  Stage 2 stop/TP must be recomputed from that real fill, not from EMA9.

ADDED — shadow-log columns (see sheets_logger.HEADER for the full list):
  risk_usd, entry_type, force_closed, discretionary_action, veto_reason,
  spy_vs_sma20, vix_level, pct_above_50dma.

  risk_usd is REQUIRED for the equity calculation: r_realized is scale-free
  and cannot be converted back into dollars once risk floats with equity.

  discretionary_action / veto_reason exist because Batch 4 is manually
  filtered. Without them the batch is unreadable — there would be no way to
  separate "the system performed well" from "the operator vetoed the bad
  ones". Recording them also lets vetoes be scored against actual outcomes.

  The three regime fields are LOG ONLY and gate nothing. A regime hard gate
  was tested on Batch 3 and rejected: it improved backtested R but removed
  the 6 largest winners (+8.4R combined) and cut breadth 32 -> 13, roughly
  36% of statistical power.

  NAMING: the field is spy_vs_sma20, not spy_vs_ema20 as originally
  sketched. fetch_market_context() returns spy_sma20 (a simple moving
  average); labelling an SMA as an EMA would silently mislead whoever
  analyses this column later.

FIXED — shadow-log field list drift:
  _append_shadow_log() kept its own hard-coded copy of the field list,
  parallel to sheets_logger.HEADER. Two lists meant the CSV and the Sheet
  could silently disagree. The CSV list is now DERIVED from
  sheets_logger.HEADER, so they cannot drift again.

HAIKU IS NOW RANK-ONLY:
  Haiku scoring logic is unchanged and still ranks candidates for Opus, but
  the score is not displayed. It is also the cascade order for waterfall
  sizing. Honest caveat: the score failed Benjamini-Hochberg on Batch 3, so
  as a ranker it is close to arbitrary — it is used because it is already
  there and adds no new code, not because it is known to work.
"""
import csv
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

import pandas as pd

from src import config
from src import universe as univ_mod
from src import data_fetcher as data_mod
from src import indicators as ind_mod
from src import analyst as analyst_mod
from src import risk_manager as risk_mod
from src import telegram_sender as tg_mod
from src import sheets_logger as sheets_mod

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
# RS GAP HELPER — Phase 2.3 F7 lookback review (shadow-only)
# ============================================================

def _compute_return_pct(close_series: pd.Series, lookback_days: int, skip_recent_days: int = 0) -> Optional[float]:
    """
    Generic % return over a lookback window, optionally skipping the most
    recent N trading days (momentum convention — avoids short-term reversal
    contamination per Jegadeesh & Titman 1993 / Springer 2022).

    skip_recent_days=0  -> return from t-lookback to t       (used for F7 10d, unchanged)
    skip_recent_days=21 -> return from t-(lookback+21) to t-21 (used for 60d/126d shadow fields)

    Returns None if insufficient history (caller defaults to 0.0 / skips field).
    """
    end_idx = -(skip_recent_days + 1) if skip_recent_days > 0 else -1
    start_idx = -(lookback_days + skip_recent_days + 1)

    if len(close_series) < abs(start_idx):
        return None

    end_price = float(close_series.iloc[end_idx])
    start_price = float(close_series.iloc[start_idx])
    if start_price == 0:
        return None
    return ((end_price - start_price) / start_price) * 100


def _compute_momentum_rs_gaps(
    stock_close: pd.Series,
    spy_close: pd.Series,
) -> Dict[str, Optional[float]]:
    """
    Phase 2.3 F7 review — shadow-only momentum RS gaps.
    Computes 60d and 126d RS gap (stock - SPY), each skipping the most
    recent 21 trading days. NOT used for gating — F7 gate still runs on
    the existing 10d gap untouched in run_mandatory_screener().

    Returns dict with rs_60d_gap / rs_126d_gap, values may be None if
    insufficient price history (e.g. recent IPO).
    """
    skip = getattr(config, "RS_SKIP_RECENT_DAYS", 21)
    lookbacks = {
        "rs_60d_gap": getattr(config, "RS_60D_LOOKBACK", 60),
        "rs_126d_gap": getattr(config, "RS_126D_LOOKBACK", 126),
    }

    out: Dict[str, Optional[float]] = {}
    for field, lb in lookbacks.items():
        stock_ret = _compute_return_pct(stock_close, lb, skip_recent_days=skip)
        spy_ret = _compute_return_pct(spy_close, lb, skip_recent_days=skip)
        if stock_ret is None or spy_ret is None:
            out[field] = None
        else:
            out[field] = round(stock_ret - spy_ret, 2)
    return out


# ============================================================
# SHADOW LOG — CSV APPEND
# ============================================================

def _build_shadow_rows(signals: List[Dict[str, Any]], run_date: str) -> List[Dict[str, Any]]:
    """
    Build the list of shadow-log row dicts (auto fields populated, manual
    fields left blank) for a batch of approved signals.

    Shared by both the CSV writer (_append_shadow_log) and the Google
    Sheets mirror (STAGE 8) so there is exactly ONE place that defines
    what a shadow-log row looks like. Add new fields here only.
    """
    rows = []
    # PHASE 2.6.1: a verification run stamps its own rows as VETOED as they
    # are written, so they can never reach equity (read_equity() counts only
    # TAKEN/MODIFIED) and are trivially filterable out of Batch 4 stats.
    # Doing this here rather than as a manual cleanup step removes the
    # operator's memory from the loop entirely.
    verify = bool(getattr(config, "VERIFICATION_RUN", False))
    veto_reason = getattr(config, "VERIFICATION_VETO_REASON", "") if verify else ""
    for sig in signals:
        ind = sig.get("_shadow_indicators", {})
        rows.append({
            "_shadow_date": run_date,
            "_shadow_ticker": sig.get("ticker", ""),
            "_shadow_close_price": ind.get("close_price", ""),
            "_shadow_ema9_price": ind.get("ema9_price", ""),
            "_shadow_ema9_gap_pct": ind.get("ema9_gap_pct", ""),
            "_shadow_ema9_ext_atr": ind.get("ema9_ext_atr", ""),
            "_shadow_atr": ind.get("atr", ""),
            "_shadow_atr_pct": ind.get("atr_pct", ""),
            "_shadow_rs_gap_vs_spy": ind.get("rs_gap_vs_spy", ""),
            "_shadow_spy_10d_return": ind.get("spy_10d_return", ""),
            "_shadow_stock_10d_return": ind.get("stock_10d_return", ""),
            "_shadow_rs_60d_gap": ind.get("rs_60d_gap", ""),
            "_shadow_rs_126d_gap": ind.get("rs_126d_gap", ""),
            "_shadow_conviction": sig.get("conviction", ""),
            "_shadow_confluence_count": ind.get("confluence_count", ""),
            "_shadow_haiku_score": sig.get("_haiku_score", ""),
            "_shadow_adx": ind.get("adx", ""),
            "_shadow_rsi": ind.get("rsi", ""),
            "_shadow_volume_ratio": ind.get("volume_ratio", ""),
            "_shadow_entry_close": ind.get("entry_close", ""),
            "_shadow_entry_ema9": sig.get("entry", ""),     # Opus sets entry = EMA9
            "_shadow_stop": sig.get("stop_loss", ""),
            "_shadow_tp1": sig.get("tp1", ""),
            "_shadow_tp2": sig.get("tp2", ""),
            "_shadow_rr": sig.get("rr_ratio", ""),
            # Manual — blank on write, filled in by hand after trade closes
            "_shadow_ema9_filled": "",
            "_shadow_outcome": "",
            "_shadow_r_realized": "",
            "_shadow_hold_days": "",

            # ---- PHASE 2.6 ----
            # risk_usd is AUTO and must never be blank: read_equity() needs it
            # to convert scale-free r_realized back into dollars. A blank here
            # makes the row invisible to the equity calculation.
            "_shadow_risk_usd": sig.get("risk_amount_usd", ""),
            # "strict" at write time. STAGE 0 of the NEXT run rewrites the
            # pending tab with the resolved value; a fallback entry is
            # identified there, not here.
            "_shadow_entry_type": ind.get("entry_type", "strict"),
            # Manual: TRUE only when an administrative batch cutoff closed the
            # position, rather than its own stop/target/time rule.
            "_shadow_force_closed": "",
            # Manual: TAKEN / VETOED / MODIFIED, plus free-text reason.
            # Batch 4 is manually filtered; without these the batch cannot be
            # attributed between system and operator.
            # On a VERIFICATION_RUN both are pre-filled — see config.
            "_shadow_discretionary_action": "VETOED" if verify else "",
            "_shadow_veto_reason": veto_reason,
            # Regime context — LOG ONLY, gates nothing.
            "_shadow_spy_vs_sma20": ind.get("spy_vs_sma20", ""),
            "_shadow_vix_level": ind.get("vix_level", ""),
            "_shadow_pct_above_50dma": ind.get("pct_above_50dma", ""),
        })
    return rows


def _append_shadow_log(signals: List[Dict[str, Any]], run_date: str) -> List[Dict[str, Any]]:
    """
    Append auto-logged shadow fields for each approved signal to shadow_log.csv.
    Manual outcome fields are written as empty strings for manual fill-in later.

    Returns the row list that was written, so callers (e.g. the Google
    Sheets mirror in STAGE 8) can reuse the exact same rows instead of
    rebuilding them — keeps CSV and Sheet permanently in sync by
    construction rather than by two parallel copies of the same logic.

    Fields written:
      Auto: date, ticker, close_price, ema9_price, ema9_gap_pct, ema9_ext_atr,
            atr, atr_pct, rs_gap_vs_spy, spy_10d_return, stock_10d_return,
            rs_60d_gap, rs_126d_gap, conviction, confluence_count, haiku_score,
            adx, rsi, volume_ratio, entry_close, entry_ema9, stop, tp1, tp2, rr
      Manual (blank): ema9_filled, outcome, r_realized, hold_days
    """
    log_path = getattr(config, "SHADOW_LOG_PATH", "shadow_log.csv")

    # PHASE 2.6: derived from sheets_logger.HEADER instead of being a second
    # hard-coded copy. Previously the CSV list and the Sheet list were
    # maintained separately, so adding a column to one silently desynced it
    # from the other. One list, one source of truth.
    fieldnames = list(sheets_mod.HEADER)

    rows = _build_shadow_rows(signals, run_date)
    file_exists = os.path.isfile(log_path)

    try:
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)

        logger.info(f"Shadow log appended: {len(rows)} row(s) → {log_path}")

    except Exception as e:
        logger.error(f"Shadow log append failed: {e}")

    return rows


# ============================================================
# MANDATORY SCREENER (Stage 2 — replaces SMC screen)
# ============================================================

def run_mandatory_screener(
    universe_df: pd.DataFrame,
    macro_regime: str,
    stats_out: Optional[Dict[str, Any]] = None,
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

    Phase 2.3 — Soft F7 (NOT a hard gate):
      Computes rs_gap_vs_spy_pct (stock 10d return - SPY 10d return) for each
      candidate and stores it in the candidate dict + brief_data for Haiku context.
      SPY is fetched ONCE before the loop (not per ticker).

    Phase 2.6 — market breadth (NOT a gate, shadow-only):
      If stats_out is provided, fills in pct_above_ema50 = the share of
      successfully-screened tickers trading above their own EMA(50).
      This is free: F1 already computes above_ema_50 for every ticker in the
      universe, so breadth costs no extra fetch and no extra compute — it was
      simply being discarded. Optional keyword arg so existing callers are
      unaffected.

      Recorded for the Batch 4 regime review only. It gates nothing: a regime
      hard gate was tested on Batch 3 and rejected (better backtested R, but
      it removed the 6 largest winners and cut breadth 32 -> 13).

    Phase 2.3 — F7 lookback review (NOT a hard gate, shadow-only):
      Also computes rs_60d_gap and rs_126d_gap (skip-21d momentum convention)
      for the SAME candidates that already passed F1-F5, using the SAME
      SPY series fetched once above. These are stored alongside the 10d gap
      purely for the upcoming filter review — F7 gating logic is untouched.
    """
    candidates = []
    errors = 0
    screened_ok = 0          # Phase 2.6 breadth denominator
    above_ema50 = 0          # Phase 2.6 breadth numerator
    total = len(universe_df)
    logger.info(f"Screening {total} tickers via mandatory 5-filter screener...")

    # --------------------------------------------------------
    # Phase 2.3: Fetch SPY 10d return ONCE before the loop (soft F7 context)
    # Also fetch the longer SPY close series here so the 60d/126d helper
    # below doesn't need a second SPY call.
    # --------------------------------------------------------
    spy_10d_return = 0.0
    spy_close_series = None
    try:
        spy_df = data_mod.fetch_ohlcv("SPY", period="1y", interval="1d")  # widened from 3mo to cover 126d+21d skip
        rs_lookback = getattr(config, "RS_LOOKBACK_DAYS", 10)
        if spy_df is not None:
            spy_close_series = spy_df["Close"]
            if len(spy_close_series) >= rs_lookback + 1:
                spy_end = float(spy_close_series.iloc[-1])
                spy_start = float(spy_close_series.iloc[-(rs_lookback + 1)])
                spy_10d_return = ((spy_end - spy_start) / spy_start) * 100
                logger.info(f"SPY {rs_lookback}d return: {spy_10d_return:+.2f}%")
            else:
                logger.warning("SPY data insufficient for 10d RS gap — defaulting to 0.0%")
        else:
            logger.warning("SPY fetch returned None — RS gaps will default/skip")
    except Exception as e:
        logger.warning(f"SPY fetch for RS gap failed: {e} — defaulting to 0.0%")

    for i, (_, row) in enumerate(universe_df.iterrows()):
        ticker = row["ticker"]
        try:
            # Widened from 6mo to 1y so rs_126d_gap (needs ~147 trading days
            # of history: 126 + 21 skip) has enough bars. F1-F5 indicators
            # are unaffected — they only look at the tail of this series.
            df = data_mod.fetch_ohlcv(ticker, period="1y", interval="1d")
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

            # Phase 2.6 breadth tally — counted for EVERY successfully
            # screened ticker, not just those passing all five filters.
            # Counting only the passers would make breadth ~100% by
            # construction and therefore meaningless.
            screened_ok += 1
            if brief.get("above_ema_50"):
                above_ema50 += 1

            if brief.get("all_mandatory_pass"):
                # ------------------------------------------------
                # Phase 2.3: Compute soft F7 — RS gap vs SPY (10d, unchanged)
                # NOT a gate. Stored for Haiku penalisation context.
                # ------------------------------------------------
                rs_gap_vs_spy_pct = 0.0
                stock_10d_return = 0.0
                try:
                    rs_lookback = getattr(config, "RS_LOOKBACK_DAYS", 10)
                    if len(df) >= rs_lookback + 1:
                        stock_close = df["Close"]
                        stock_end = float(stock_close.iloc[-1])
                        stock_start = float(stock_close.iloc[-(rs_lookback + 1)])
                        stock_10d_return = ((stock_end - stock_start) / stock_start) * 100
                        rs_gap_vs_spy_pct = stock_10d_return - spy_10d_return
                except Exception as rs_err:
                    logger.debug(f"RS gap (10d) compute failed for {ticker}: {rs_err}")

                # Inject into brief_data so Haiku receives it (unchanged)
                brief["rs_gap_vs_spy_pct"] = round(rs_gap_vs_spy_pct, 2)
                brief["stock_10d_return"] = round(stock_10d_return, 2)
                brief["spy_10d_return"] = round(spy_10d_return, 2)

                # ------------------------------------------------
                # Phase 2.3 F7 review: 60d / 126d RS gaps — SHADOW ONLY.
                # Not passed to Haiku, not gated. Logged for the F7
                # lookback comparison at the next filter review.
                # ------------------------------------------------
                if spy_close_series is not None:
                    try:
                        momentum_gaps = _compute_momentum_rs_gaps(
                            stock_close=df["Close"],
                            spy_close=spy_close_series,
                        )
                    except Exception as mom_err:
                        logger.debug(f"Momentum RS gap compute failed for {ticker}: {mom_err}")
                        momentum_gaps = {"rs_60d_gap": None, "rs_126d_gap": None}
                else:
                    momentum_gaps = {"rs_60d_gap": None, "rs_126d_gap": None}

                brief["rs_60d_gap"] = momentum_gaps.get("rs_60d_gap")
                brief["rs_126d_gap"] = momentum_gaps.get("rs_126d_gap")

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

    # ------------------------------------------------------------------
    # PHASE 2.6.1 — BREADTH COVERAGE GUARD
    #
    # breadth feeds `pct_above_50dma` (see STAGE 5, ind[...] assignment),
    # one of the three regime columns Edge B (regime-scaled risk) is built
    # on. It is a RATIO, so it always produces a plausible-looking number
    # no matter how few tickers survived the fetch — and that is exactly
    # the failure mode observed on 4 Aug 2026:
    #
    #   14:45 UTC run -> 58.67% breadth, computed from  75 tickers
    #   23:12 UTC run -> 71.66% breadth, computed from 501 tickers
    #
    # Same market, same session, 13 points apart. The 58.67% was not a
    # market reading at all — it was an artefact of a biased 15% sample
    # that yfinance rate limiting happened to leave behind. Nothing in the
    # log flagged it, because a ratio of a small sample looks identical to
    # a ratio of a large one.
    #
    # A regime column that is silently wrong on degraded-fetch days is
    # worse than one that is missing: the analysis cannot tell the two
    # apart, and Batch 4 would mix real regime readings with artefacts.
    # So below the coverage floor, breadth is written BLANK. The gap is
    # then visible in the data and those days can be excluded from regime
    # analysis rather than quietly poisoning it.
    #
    # Same principle as _to_float() refusing to guess at "1,25" and
    # read_equity() refusing to run without discretionary_action: surface
    # the gap, never invent the number.
    # ------------------------------------------------------------------
    min_screened = int(getattr(config, "MIN_SCREENED_FOR_BREADTH", 400))
    coverage_pct = round(screened_ok / total * 100, 1) if total else 0.0

    if screened_ok >= min_screened:
        breadth = round(above_ema50 / screened_ok * 100, 2)
        breadth_valid = True
    else:
        breadth = ""          # deliberately blank — see rationale above
        breadth_valid = False

    if stats_out is not None:
        stats_out["pct_above_ema50"] = breadth
        stats_out["screened_ok"] = screened_ok
        stats_out["screened_total"] = total
        stats_out["coverage_pct"] = coverage_pct
        stats_out["breadth_valid"] = breadth_valid

    if breadth_valid:
        logger.info(
            f"Mandatory screener: {len(candidates)} candidates from {total} universe "
            f"| breadth above EMA50: {breadth}% of {screened_ok} screened "
            f"({coverage_pct}% coverage)"
        )
    else:
        # Loud, at WARNING — the 4 Aug 14:45 run logged its 75/503 at INFO
        # inside a routine line and nobody noticed until the log was read
        # by hand days later.
        logger.warning(
            f"DEGRADED FETCH COVERAGE: only {screened_ok}/{total} tickers "
            f"({coverage_pct}%) returned usable data — below the "
            f"{min_screened} floor. Breadth SUPPRESSED (logged blank) "
            f"because a ratio of this sample is not a market reading. "
            f"Candidate count ({len(candidates)}) is also unreliable: the "
            f"screener only saw part of the universe."
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
# DAILY PIPELINE
# ============================================================


# ============================================================
# PHASE 2.6 — RUNNING EQUITY (Sheets read + local cache fallback)
# ============================================================

def _load_equity_cache() -> Optional[float]:
    """
    Read the last known-good equity from config.EQUITY_CACHE_PATH.

    The file is written after every successful Sheets read and committed
    back to the repo by the workflow — each Actions run is a fresh
    checkout, so without that commit the cache would never survive.
    """
    path = getattr(config, "EQUITY_CACHE_PATH", ".last_equity")
    try:
        with open(path, "r") as f:
            return float(f.read().strip())
    except Exception:
        return None


def _save_equity_cache(equity: float) -> None:
    """Persist equity for the next run's fallback. Never raises."""
    path = getattr(config, "EQUITY_CACHE_PATH", ".last_equity")
    try:
        with open(path, "w") as f:
            f.write(f"{equity:.4f}\n")
    except Exception as e:
        logger.warning(f"Could not write equity cache: {e}")


def _resolve_equity() -> Dict[str, Any]:
    """
    Resolve the equity basis for this run, in strict priority order:

      1. Live Sheets read      -> authoritative, refreshes the cache
      2. Local cache           -> stale but real; ALWAYS warns
      3. ACCOUNT_START_USD     -> last resort, warns LOUDLY

    Level 3 is the dangerous one and is deliberately noisy. Silently sizing
    off the starting balance after months of trading would understate or
    overstate every position with no visible symptom — which is exactly the
    failure mode that let the Sheets mirror no-op for 22 days unnoticed.

    Returns {equity, source, warnings[]}.
    """
    out = {"equity": float(config.ACCOUNT_START_USD), "source": "start", "warnings": []}

    equity, meta = sheets_mod.read_equity()

    if equity is not None:
        out["equity"] = float(equity)
        out["source"] = "sheets"
        _save_equity_cache(out["equity"])
        logger.info(
            f"Equity from Sheets: ${out['equity']:.2f} "
            f"({meta.get('closed_rows', 0)} closed row(s))"
        )
        if meta.get("skipped_no_risk"):
            out["warnings"].append(
                f"{meta['skipped_no_risk']} closed row(s) have r_realized but no "
                f"risk_usd — their P&L is INVISIBLE to equity. Backfill risk_usd."
            )
        stale = meta.get("stale_rows") or []
        if stale:
            listed = ", ".join(f"{t} ({int(d)}d)" for t, d in stale[:5])
            out["warnings"].append(
                f"{len(stale)} row(s) still have no outcome past "
                f"{config.EQUITY_STALE_WARN_DAYS}d: {listed}. "
                f"Equity is running behind reality until they are filled in."
            )
        # PHASE 2.6.1: closed rows excluded from equity because the
        # execution decision was never recorded. This is the warning that
        # keeps the two-track split honest — silence here would mean either
        # paper P&L moving real capital, or real P&L going missing, and
        # neither has any other visible symptom.
        unmarked = meta.get("unmarked_rows") or []
        if unmarked:
            out["warnings"].append(
                f"{len(unmarked)} CLOSED row(s) have no `discretionary_action`: "
                f"{', '.join(str(t) for t in unmarked[:5])}. They are EXCLUDED "
                f"from equity. Mark each TAKEN / MODIFIED / VETOED — if any was "
                f"really executed, the balance below is understated."
            )
        return out

    cached = _load_equity_cache()
    if cached is not None:
        out["equity"] = float(cached)
        out["source"] = "cache"
        out["warnings"].append(
            f"Sheets equity read FAILED ({meta.get('error', 'unknown')}). "
            f"Using cached equity ${cached:.2f}. Sizing may be stale — verify "
            f"the sheet before executing."
        )
        logger.warning(f"Equity from CACHE: ${cached:.2f}")
        return out

    out["warnings"].append(
        f"Sheets equity read FAILED and NO cache exists. Falling back to "
        f"starting equity ${config.ACCOUNT_START_USD:.2f}. If this is not "
        f"genuinely the first run, DO NOT execute these sizes."
    )
    logger.error("Equity unresolved — using ACCOUNT_START_USD.")
    return out


def _apply_waterfall_sizing(signals: List[Dict[str, Any]], equity: float) -> float:
    """
    Size a batch of same-day signals off a running basis, highest Haiku
    score first.

    PHASE 2.6.2 — WHAT GETS DEDUCTED NOW DEPENDS ON THE SIZING MODE.

    notional mode (Batch 4):
        signal 1 -> 4% of equity
        signal 2 -> 4% of (equity - signal 1 NOTIONAL)
        ...
      The running basis tracks CASH. Total capital deployed can never
      exceed the balance, which is the whole point of the mode.

    risk mode (Phase 2.6 behaviour):
        deducts RISK. Total notional across signals could then exceed the
        cash balance — a real gap, since the cascade would report room for
        more signals than the account could actually fund. Observed on the
        5 Aug run: basis went $100 -> $96.04 (risk $3.96 deducted) while
        the two positions actually required $68.90 of capital. With only
        low-ATR names that day, they would not have fit.

    Deducting the wrong quantity is the same class of error either way:
    a budget that is checked against something other than the constraint
    that actually binds.

    Returns the remaining basis after the cascade.
    """
    basis = float(equity)
    mode = str(getattr(config, "SIZING_MODE", "risk")).strip().lower()
    deduct_key = "position_size_usd" if mode == "notional" else "risk_amount_usd"

    ranked = sorted(
        signals,
        key=lambda x: (_safe_float(x.get("_haiku_score"), 0.0)),
        reverse=True,
    )
    for rank, sig in enumerate(ranked, start=1):
        risk_mod.apply_position_sizing(sig, basis)
        sig["_size_rank"] = rank
        basis -= float(sig.get(deduct_key, 0) or 0)
        if basis <= 0:
            logger.warning("Waterfall basis exhausted — remaining signals size to ~0.")
            basis = 0.0
    return basis


def _flush_ops_warnings(warnings: List[str]) -> None:
    """
    Send accumulated operational warnings as ONE Telegram message at the end
    of the run. Batched deliberately: these are things the operator must act
    on (stale rows, a cached equity value, an unreadable pending tab) and
    scattering them between signal messages makes them easy to scroll past.

    Called on every exit path — including early returns from a macro veto or
    a total risk-validation rejection. A stale-equity warning matters MORE on
    a no-signal day, because nothing else in the message would reveal it.
    """
    if not warnings:
        return
    try:
        tg_mod.send_ops_warnings(warnings)
    except Exception as e:
        logger.warning(f"Ops warning delivery failed: {e}")


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ============================================================
# PHASE 2.6 — TWO-STAGE ENTRY (cross-day near-miss state)
# ============================================================

def _resolve_pending_entries(run_date: str) -> Dict[str, Any]:
    """
    STAGE 0. Read yesterday's queued signals and resolve each against the
    session that has since completed.

        low <= ema9_price   -> FILLED    (Stage 1 limit worked, "strict")
        low <= zone_high    -> FALLBACK  (near-miss -> execute at next open)
        otherwise           -> EXPIRED

    zone_high = ema9_price + ENTRY2_ATR_TOLERANCE * atr.

    Rows whose signal date has no completed session yet are left PENDING
    (e.g. a US holiday) rather than being expired — expiring them would
    silently drop a live setup.

    Fully non-blocking: any failure returns an empty result and the daily
    pipeline continues. A missed Stage-2 alert is an inconvenience; a
    blocked pipeline is a lost trading day.
    """
    out = {"fallbacks": [], "filled": 0, "expired": 0, "checked": 0,
           "error": None, "shadow_missed": []}
    try:
        pending = sheets_mod.read_pending()
        if pending is None:
            out["error"] = "pending tab unreadable"
            logger.warning("Pending entries unreadable — Stage 2 skipped this run.")
            return out
        if not pending:
            return out

        expiry_days = int(getattr(config, "PENDING_ENTRY_EXPIRY_DAYS", 1))
        shadow_updates = []

        for row in pending:
            ticker = str(row.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            out["checked"] += 1

            ema9 = _safe_float(row.get("ema9_price"), 0.0)
            zone_high = _safe_float(row.get("zone_high"), 0.0)
            sig_date = str(row.get("signal_date", "")).strip()[:10]
            row_no = row.get("_row")

            df = data_mod.fetch_ohlcv(ticker, period="1mo", interval="1d")
            if df is None or df.empty or ema9 <= 0:
                continue

            # ------------------------------------------------------------
            # PHASE 2.6.1 — SESSION SELECTION
            #
            # The order is live in the first session STRICTLY AFTER the last
            # completed bar the signal was computed from. Anchoring on that
            # bar (`bar_date`) makes this correct at any run time:
            #
            #   01:00 UTC run: last bar = Mon -> order live Tue -> > Mon = Tue
            #   23:12 UTC run: last bar = Tue -> order live Wed -> > Tue = Wed
            #
            # The original code anchored on `signal_date` (wall-clock UTC
            # date) with ">", which skipped the correct session on the 01:00
            # cron and resolved a day late against a session the order was
            # never live in. Switching that to ">=" fixed the 01:00 case but
            # broke any off-schedule run — the anchor was the real defect,
            # not the operator. bar_date fixes both at once.
            #
            # LEGACY FALLBACK: rows queued before bar_date existed have it
            # blank. For those, keep the ">= signal_date" behaviour, which is
            # correct for the 01:00 UTC scheduled run they were written by.
            # ------------------------------------------------------------
            bar_date = str(row.get("bar_date", "")).strip()[:10]
            try:
                if bar_date:
                    after = df[df.index.strftime("%Y-%m-%d") > bar_date]
                else:
                    after = df[df.index.strftime("%Y-%m-%d") >= sig_date]
            except Exception:
                after = df.tail(1)

            if len(after) == 0:
                # No session has traded on or since the signal date
                # (holiday / weekend / early run). Leave PENDING — do not
                # expire a setup that never got a chance to fill.
                continue

            if len(after) > expiry_days:
                sheets_mod.update_pending_status(
                    row_no, "EXPIRED", run_date,
                    f"{len(after)} sessions elapsed > expiry {expiry_days}"
                )
                out["expired"] += 1
                shadow_updates.append({
                    "ticker": ticker, "signal_date": sig_date,
                    "fields": {"entry_type": "expired", "ema9_filled": "FALSE"},
                })
                continue

            bar = after.iloc[0]
            day_low = float(bar["Low"])
            session_date = str(after.index[0].strftime("%Y-%m-%d"))

            if day_low <= ema9:
                sheets_mod.update_pending_status(
                    row_no, "FILLED", run_date,
                    f"session {session_date}: low {day_low:.2f} <= EMA9 {ema9:.2f}"
                )
                out["filled"] += 1
                # Strict fill: geometry already correct in the shadow log
                # (entry IS the EMA9 the row was written with). Only the
                # fill flags need confirming.
                shadow_updates.append({
                    "ticker": ticker, "signal_date": sig_date,
                    "fields": {"entry_type": "strict", "ema9_filled": "TRUE"},
                })
            elif zone_high > 0 and day_low <= zone_high:
                sheets_mod.update_pending_status(
                    row_no, "FALLBACK", run_date,
                    f"session {session_date}: near-miss, low {day_low:.2f} "
                    f"in [{ema9:.2f}, {zone_high:.2f}] — enter actual_entry"
                )
                out["fallbacks"].append({
                    "ticker": ticker,
                    "ema9_price": ema9,
                    "zone_high": zone_high,
                    "day_low": day_low,
                    "atr": _safe_float(row.get("atr"), 0.0),
                    "signal_date": sig_date,
                })
                # ema9_filled stays FALSE — the LIMIT genuinely did not fill.
                # entry_type carries "this was traded via Stage 2". Keeping
                # them separate preserves the project's two-metric split:
                # fill-rate measures the entry MECHANISM, entry_type measures
                # the TRADE. Geometry is corrected later by Stage 0c, once
                # the operator has typed in the real fill price.
                shadow_updates.append({
                    "ticker": ticker, "signal_date": sig_date,
                    "fields": {"entry_type": "fallback", "ema9_filled": "FALSE"},
                })
            else:
                sheets_mod.update_pending_status(
                    row_no, "EXPIRED", run_date,
                    f"session {session_date}: low {day_low:.2f} never entered "
                    f"zone (high {zone_high:.2f})"
                )
                out["expired"] += 1
                shadow_updates.append({
                    "ticker": ticker, "signal_date": sig_date,
                    "fields": {"entry_type": "expired", "ema9_filled": "FALSE"},
                })

        # Write the resolved state back into the shadow log. Previously
        # nothing did this, so _shadow_entry_type was frozen at its
        # write-time guess of "strict" for every row, forever — including
        # rows that never filled at all.
        if shadow_updates:
            res = sheets_mod.update_shadow_rows(shadow_updates)
            if res.get("missed"):
                out["shadow_missed"] = res["missed"]
            if res.get("error"):
                logger.warning(f"Shadow entry_type write-back failed: {res['error']}")

        logger.info(
            f"Pending resolved: {out['filled']} filled, "
            f"{len(out['fallbacks'])} fallback, {out['expired']} expired "
            f"(of {out['checked']} checked)"
        )
    except Exception as e:
        out["error"] = str(e)[:200]
        logger.warning(f"Pending resolution failed (non-blocking): {e}")
    return out


def _apply_fallback_geometry(run_date: str) -> Dict[str, Any]:
    """
    STAGE 0c (Phase 2.6.1) — the other half of the Bug 3 fix.

    A Stage-2 trade is entered at MARKET, above EMA9. Its shadow-log row,
    however, was written at signal time and still carries EMA9-derived
    entry / stop / TP1 / TP2. Nothing ever corrected them, so:

      - the recorded stop was BELOW the real 1.5xATR stop for that fill,
        i.e. the row understated the actual risk taken
      - r_realized computed against those levels was wrong for every
        fallback trade
      - Phase 2.6 §3.4 says to "recompute from the actual fill", but no
        code path existed to get that number back into the log

    The system cannot observe a fill price — execution is manual. So the
    operator types ONE number into `pending_entries.actual_entry`, and this
    stage does the arithmetic:

        stop = fill - STOP_ATR_MULT x ATR
        TP1  = fill + TP1_R_MULT x (fill - stop)
        TP2  = fill + TP2_R_MULT x (fill - stop)

    IMPORTANT — sizing is NOT touched. risk_per_share = fill - stop
    = 1.5 x ATR regardless of where the fill lands, so `shares` and
    `risk_usd` are invariant and 1R is preserved exactly. Only the price
    levels and the notional shift. This is why the fix is safe to apply
    after the trade is already open.

    Rows are marked EXECUTED so the same fill is never applied twice.
    Fully non-blocking.
    """
    out = {"applied": 0, "awaiting": 0, "error": None, "missed": []}
    try:
        rows = sheets_mod.read_pending(statuses=("FALLBACK",))
        if rows is None:
            out["error"] = "pending tab unreadable"
            return out
        if not rows:
            return out

        stop_mult = float(getattr(config, "STOP_ATR_MULT", 1.5))
        tp1_mult = float(getattr(config, "TP1_R_MULT", 2.0))
        tp2_mult = float(getattr(config, "TP2_R_MULT", 3.0))

        updates = []
        for row in rows:
            ticker = str(row.get("ticker", "")).strip().upper()
            fill = _safe_float(row.get("actual_entry"), 0.0)
            atr = _safe_float(row.get("atr"), 0.0)
            sig_date = str(row.get("signal_date", "")).strip()[:10]

            if fill <= 0 or atr <= 0:
                # Operator has not entered the fill yet. Leave FALLBACK so
                # it is picked up on a later run — never expire it, the
                # trade may well be open.
                out["awaiting"] += 1
                continue

            stop = fill - stop_mult * atr
            risk_per_share = fill - stop
            if risk_per_share <= 0:
                out["missed"].append(f"{ticker} (bad geometry)")
                continue

            updates.append({
                "ticker": ticker, "signal_date": sig_date,
                "fields": {
                    "entry_ema9": round(fill, 4),      # the entry actually used
                    "stop": round(stop, 4),
                    "tp1": round(fill + tp1_mult * risk_per_share, 4),
                    "tp2": round(fill + tp2_mult * risk_per_share, 4),
                    "entry_type": "fallback",
                },
            })
            sheets_mod.update_pending_status(
                row["_row"], "EXECUTED", run_date,
                f"geometry recomputed from fill {fill:.2f} "
                f"(stop {stop:.2f}, 1R {risk_per_share:.2f})"
            )

        if updates:
            res = sheets_mod.update_shadow_rows(updates)
            out["applied"] = res.get("updated", 0)
            out["missed"].extend(res.get("missed", []))
            if res.get("error"):
                out["error"] = res["error"]

        if out["applied"] or out["awaiting"]:
            logger.info(
                f"Fallback geometry: {out['applied']} recomputed, "
                f"{out['awaiting']} awaiting actual_entry"
            )
    except Exception as e:
        out["error"] = str(e)[:200]
        logger.warning(f"Fallback geometry step failed (non-blocking): {e}")
    return out


def _queue_pending_entries(signals: List[Dict[str, Any]], run_date: str) -> bool:
    """
    STAGE 9. Queue every signal just sent as PENDING so the NEXT run can
    resolve whether its EMA9 limit filled, near-missed, or expired.

    Queued unconditionally — including signals the operator will end up
    vetoing. Fill behaviour is a property of the SIGNAL, not of the
    operator's decision, and mixing the two would contaminate the fill-rate
    metric that ENTRY2_ATR_TOLERANCE has to be validated against.
    """
    rows = []
    tol = float(getattr(config, "ENTRY2_ATR_TOLERANCE", 0.15))

    # PHASE 2.6.1 — anchor on the DATA, not the wall clock. One extra fetch
    # of SPY gives the last completed session for the whole market, which is
    # the same for every ticker queued this run. The limit order is live in
    # the first session strictly AFTER this date, regardless of what time
    # the workflow fired. See PENDING_HEADER["bar_date"] for the full
    # rationale and the two timing cases it resolves.
    bar_date = ""
    try:
        # period="1mo" (~21 bars), NOT "5d". fetch_ohlcv() rejects any frame
        # with fewer than 10 bars (data_fetcher.py: `if len(df) < 10: return
        # None`), so a 5-day window returns None and bar_date silently ends
        # up blank — observed on the 5 Aug 00:03 UTC run, which logged
        # "bar_date unavailable" and fell back to the legacy anchor.
        # Only the LAST index is used; the extra bars cost nothing.
        spy_df = data_mod.fetch_ohlcv("SPY", period="1mo", interval="1d")
        if spy_df is not None and not spy_df.empty:
            bar_date = str(spy_df.index[-1].strftime("%Y-%m-%d"))
    except Exception as e:
        logger.warning(f"Could not determine bar_date for pending rows: {e}")

    if not bar_date:
        # Fall back to legacy behaviour rather than blocking the queue, but
        # say so — resolution will be less precise for these rows.
        logger.warning(
            "bar_date unavailable — pending rows will be resolved using the "
            "legacy run_date anchor, which is only correct for the 01:00 UTC "
            "scheduled run."
        )

    for sig in signals:
        ind = sig.get("_shadow_indicators", {})
        ema9 = _safe_float(sig.get("entry"), 0.0)
        atr = _safe_float(ind.get("atr"), 0.0)
        if ema9 <= 0:
            continue
        rows.append({
            "signal_date": run_date,
            "ticker": sig.get("ticker", ""),
            "ema9_price": round(ema9, 4),
            "atr": round(atr, 4),
            "zone_high": round(ema9 + tol * atr, 4),
            "day_low": "",
            "status": "PENDING",
            "execute_date": "",
            "actual_entry": "",
            "note": "",
            "bar_date": bar_date,
        })
    if not rows:
        return True
    return sheets_mod.append_pending(rows)


def run_daily_signals() -> Dict[str, Any]:
    """
    Main entry point — full daily signal pipeline.
    Triggered by GitHub Actions cron at 01:00 UTC (8:00 AM WIB).
    """
    start_time = time.time()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logger.info("=" * 60)
    logger.info("DAILY SIGNAL PIPELINE — STARTING")
    logger.info(f"Run timestamp: {run_date}T{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z")
    if getattr(config, "VERIFICATION_RUN", False):
        logger.warning("*** VERIFICATION RUN *** — every shadow row written by "
                       "this run is stamped VETOED and excluded from equity. "
                       "Signals sent are NOT tradable.")
    logger.info("=" * 60)

    result = {
        "status": "unknown",
        "signals_sent": 0,
        "stage_timings": {},
        "errors": [],
    }

    ops_warnings: List[str] = []

    try:
        # --------------------------------------------------------
        # STAGE 0: Resolve yesterday's pending entries (Phase 2.6)
        #
        # Runs FIRST, before any macro veto can return early. A near-miss
        # queued yesterday is already a committed setup — whether TODAY
        # happens to be a no-signal day is irrelevant to it, and letting a
        # veto skip this step would silently strand it.
        # --------------------------------------------------------
        t = time.time()
        pending_result = _resolve_pending_entries(run_date)
        result["stage_timings"]["pending_resolve"] = round(time.time() - t, 2)
        result["pending"] = {
            "filled": pending_result.get("filled", 0),
            "fallback": len(pending_result.get("fallbacks", [])),
            "expired": pending_result.get("expired", 0),
        }
        if pending_result.get("fallbacks"):
            try:
                tg_mod.send_stage2_alerts(pending_result["fallbacks"], run_date)
            except Exception as st2_err:
                logger.warning(f"Stage 2 alert failed (non-blocking): {st2_err}")
        if pending_result.get("error"):
            ops_warnings.append(
                f"Pending-entry resolution failed: {pending_result['error']}. "
                f"Two-stage entry is blind this run."
            )
        if pending_result.get("shadow_missed"):
            ops_warnings.append(
                f"entry_type write-back could not match "
                f"{len(pending_result['shadow_missed'])} shadow row(s): "
                f"{', '.join(pending_result['shadow_missed'][:5])}. "
                f"Those rows still say 'strict' — fix by hand before the "
                f"entry_type analysis is run."
            )

        # --------------------------------------------------------
        # STAGE 0c: Recompute geometry for executed fallback entries
        # (Phase 2.6.1). Runs right after resolution so a fill typed in
        # overnight is corrected before anything else reads the log.
        # --------------------------------------------------------
        geom_result = _apply_fallback_geometry(run_date)
        result["fallback_geometry"] = {
            "applied": geom_result.get("applied", 0),
            "awaiting": geom_result.get("awaiting", 0),
        }
        if geom_result.get("awaiting"):
            ops_warnings.append(
                f"{geom_result['awaiting']} fallback trade(s) still have no "
                f"`actual_entry` in the pending tab. Until you enter the real "
                f"fill price, their shadow-log stop/TP remain EMA9-based and "
                f"their r_realized will be WRONG."
            )
        if geom_result.get("missed"):
            ops_warnings.append(
                f"Fallback geometry could not be written for: "
                f"{', '.join(geom_result['missed'][:5])}."
            )

        # --------------------------------------------------------
        # STAGE 0b: Resolve running equity (Phase 2.6)
        #
        # Done early so a Sheets problem surfaces in the same message as the
        # signals rather than after them. Never blocks: falls back to cache,
        # then to starting equity, warning at each level.
        # --------------------------------------------------------
        equity_info = _resolve_equity()
        equity_basis = equity_info["equity"]
        ops_warnings.extend(equity_info.get("warnings", []))
        result["equity"] = equity_basis
        result["equity_source"] = equity_info["source"]

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
            _flush_ops_warnings(ops_warnings)
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
        # STAGE 3: Mandatory 5-filter screener + soft RS gap (F7)
        # --------------------------------------------------------
        t = time.time()
        screen_stats: Dict[str, Any] = {}
        candidates = run_mandatory_screener(
            universe_df, regime["regime"], stats_out=screen_stats
        )
        # Phase 2.6 — LOG ONLY regime context, gates nothing.
        breadth_pct_above_ema50 = screen_stats.get("pct_above_ema50", "")
        result["stage_timings"]["screener"] = round(time.time() - t, 2)

        # PHASE 2.6.1 — carry fetch coverage into the result payload and the
        # Telegram ops block. On 4 Aug the 75/503 coverage was buried in a
        # routine INFO line and went unnoticed for days; it needs to be
        # somewhere the operator actually looks.
        result["fetch_coverage"] = {
            "screened_ok": screen_stats.get("screened_ok", 0),
            "total": screen_stats.get("screened_total", 0),
            "coverage_pct": screen_stats.get("coverage_pct", 0.0),
            "breadth_valid": screen_stats.get("breadth_valid", False),
        }
        if not screen_stats.get("breadth_valid", False):
            ops_warnings.append(
                f"DEGRADED FETCH: only {screen_stats.get('screened_ok', 0)}/"
                f"{screen_stats.get('screened_total', 0)} tickers returned data "
                f"({screen_stats.get('coverage_pct', 0)}%). Breadth logged BLANK "
                f"— this day must be EXCLUDED from regime analysis. Signals "
                f"below come from a partial universe; treat the candidate list "
                f"as incomplete, not as 'the market had nothing better'."
            )

        if not candidates:
            msg = "No stocks passed mandatory 5-filter screener"
            logger.warning(msg)
            tg_mod.send_no_signal_day(msg, macro_context)
            # PHASE 2.6.1: this early return previously discarded every
            # accumulated ops warning. A no-candidate day caused by degraded
            # fetch coverage is exactly when the operator most needs to know
            # WHY there were no candidates.
            _flush_ops_warnings(ops_warnings)
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
            _flush_ops_warnings(ops_warnings)   # PHASE 2.6.1 — see above
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
                # Carry haiku score forward for shadow log
                signal["_haiku_score"] = cand.get("score", "")
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
            _flush_ops_warnings(ops_warnings)   # PHASE 2.6.1 — see above
            result["status"] = "opus_all_failed"
            return result

        # --------------------------------------------------------
        # STAGE 5b: Regime + sizing context (Phase 2.6)
        #
        # Regime fields are attached to every signal for the shadow log.
        # LOG ONLY — nothing here gates anything. A regime hard gate was
        # tested on Batch 3 and rejected (removed the 6 largest winners,
        # +8.4R, and cut breadth 32 -> 13).
        #
        # Sizing is applied BEFORE validation because RULE 4 and RULE 5 now
        # check against _equity_basis. Pass 1 sizes every signal at the FULL
        # basis so the budget check is meaningful for each independently;
        # the cascade is applied in pass 2, after rejects are gone, so a
        # rejected signal never consumes budget from an approved one.
        # --------------------------------------------------------
        spy_price = _safe_float(macro_context.get("spy_price"), 0.0)
        spy_sma20 = _safe_float(macro_context.get("spy_sma20"), 0.0)
        spy_vs_sma20 = round((spy_price / spy_sma20 - 1) * 100, 2) if spy_sma20 > 0 else ""
        for sig in deep_signals:
            ind = sig.setdefault("_shadow_indicators", {})
            ind["spy_vs_sma20"] = spy_vs_sma20
            ind["vix_level"] = macro_context.get("vix", "")
            ind["pct_above_50dma"] = breadth_pct_above_ema50
            risk_mod.apply_position_sizing(sig, equity_basis)

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
            _flush_ops_warnings(ops_warnings)
            result["status"] = "skipped_risk_validation"
            return result

        # --------------------------------------------------------
        # STAGE 6b: Waterfall sizing, pass 2 (Phase 2.6)
        #
        # Authoritative sizing. Re-runs the cascade over APPROVED signals
        # only, highest Haiku score first, so budget consumed by a signal
        # that was later rejected is returned to the pool.
        # --------------------------------------------------------
        remaining_basis = _apply_waterfall_sizing(approved, equity_basis)
        logger.info(
            f"Waterfall sizing: basis ${equity_basis:.2f} -> ${remaining_basis:.2f} "
            f"across {len(approved)} signal(s) at "
            f"{config.NOTIONAL_PER_TRADE_PCT}% notional each"
            if str(getattr(config, "SIZING_MODE", "risk")).lower() == "notional"
            else f"Waterfall sizing: basis ${equity_basis:.2f} -> "
                 f"${remaining_basis:.2f} across {len(approved)} signal(s) at "
                 f"{config.MAX_RISK_PER_TRADE_PCT}% risk each"
        )

        # --------------------------------------------------------
        # STAGE 7: Send to Telegram
        # --------------------------------------------------------
        t = time.time()
        sent_ok = tg_mod.send_daily_signals(
            approved, macro_context,
            equity_basis=equity_basis,
            equity_source=equity_info["source"],
        )
        result["stage_timings"]["telegram_send"] = round(time.time() - t, 2)
        result["signals_sent"] = len(approved) if sent_ok else 0
        result["status"] = "success" if sent_ok else "telegram_failed"

        # --------------------------------------------------------
        # STAGE 8: Shadow log append (Phase 2.3) + Google Sheets mirror (Phase 4a)
        # Runs after Telegram send — non-blocking, never gates the pipeline.
        #
        # _append_shadow_log() writes shadow_log.csv AND returns the exact
        # row list it wrote. We pass that SAME list to sheets_mod so the
        # CSV and the Sheet are always built from one row-construction
        # path (_build_shadow_rows) instead of two parallel copies of the
        # same field-mapping logic drifting apart over time.
        #
        # CSV remains the source of truth. The Sheet is a parallel mirror
        # for easier manual outcome entry from phone/browser — there is
        # still no two-way sync between them once you start hand-editing
        # the manual fields, so pick one as your "working copy" for edits
        # and treat the other as read-only reference, or remember to
        # mirror edits both ways.
        # --------------------------------------------------------
        shadow_rows: List[Dict[str, Any]] = []
        try:
            shadow_rows = _append_shadow_log(approved, run_date)
            tg_mod.send_shadow_log(approved, run_date)
        except Exception as shadow_err:
            logger.warning(f"Shadow log step failed (non-critical): {shadow_err}")

        try:
            if shadow_rows:
                sheets_mod.append_shadow_rows(shadow_rows)
            else:
                logger.warning("Skipping Google Sheets mirror — no shadow rows available")
        except Exception as sheets_err:
            logger.warning(f"Google Sheets mirror failed (non-critical): {sheets_err}")

        # --------------------------------------------------------
        # STAGE 9: Queue signals for tomorrow's two-stage resolution
        # (Phase 2.6). Non-blocking, and deliberately last: everything the
        # operator needs has already been delivered by this point.
        # --------------------------------------------------------
        try:
            _queue_pending_entries(approved, run_date)
        except Exception as pend_err:
            logger.warning(f"Pending queue failed (non-blocking): {pend_err}")

        _flush_ops_warnings(ops_warnings)

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
# BLACK SWAN — MARKER HELPERS (Phase 2.5)
#
# Generic read/write — shared by the Type A dedup marker AND the
# heartbeat keepalive marker. Two different files, same mechanism.
# ============================================================

def _read_marker(path: str) -> str:
    """Read marker file content (string). Returns '' if missing/unreadable."""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        logger.warning(f"Could not read marker {path}: {e}")
    return ""


def _write_marker(path: str, content: str) -> None:
    """Write a string to a marker file. The GitHub Actions workflow commits it back."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Marker written: {path} = {content}")
    except Exception as e:
        logger.warning(f"Could not write marker {path}: {e}")


# ============================================================
# BLACK SWAN CHECK — Type A only (every 15 min during US hours)
#
# Phase 2.5 redesign — see module docstring for full rationale:
#   - Type B (rally) & Type C (info): DELETED — less noise (user decision).
#   - NO LLM. Defensive-idea generation cut entirely. Alert-only; the
#     trade decision belongs to the user, not the system.
#   - SPY move measured via spy_1d_change_pct (Opsi X) — fixes the old
#     lowercase-column bug and correctly captures overnight gaps.
#   - Dedup: max 1 alert per UTC day via marker file.
#   - Silent on non-events. Headline context with explicit "unverified,
#     research yourself" framing.
# ============================================================

def run_black_swan_check() -> Dict[str, Any]:
    """Monitor crash intraday — Type A only. See module docstring for design."""
    logger.info("Black swan check starting (Type A crash watch)...")
    result = {"status": "no_event", "alert_sent": False}

    try:
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker_path = getattr(config, "BLACK_SWAN_MARKER_PATH", ".last_alert_date")

        # Dedup — already alerted today?
        if _read_marker(marker_path) == run_date:
            logger.info(f"Already alerted today ({run_date}) — skip (dedup).")
            result["status"] = "already_alerted"
            return result

        macro_ctx = data_mod.fetch_market_context()
        vix = macro_ctx.get("vix", 0.0)
        vix_spike_pct = macro_ctx.get("vix_spike_pct", 0.0)
        spy_1d = macro_ctx.get("spy_1d_change_pct", 0.0)  # Opsi X

        type_a_cfg = getattr(config, "BLACK_SWAN_TYPE_A", {})
        triggers = type_a_cfg.get("triggers", {})
        vix_min = triggers.get("vix_spike_pct_min", 30.0)
        spy_max = triggers.get("spy_drop_pct_max", -3.0)

        is_type_a = (vix_spike_pct >= vix_min) or (spy_1d <= spy_max)

        if not is_type_a:
            logger.info(
                f"No Type A trigger (VIX spike {vix_spike_pct:+.1f}% < {vix_min}, "
                f"SPY 1d {spy_1d:+.2f}% > {spy_max}). Silent pass."
            )
            return result

        # ---- Type A fired ----
        logger.warning(
            f"BLACK SWAN TYPE A — VIX spike {vix_spike_pct:+.1f}%, "
            f"SPY 1d {spy_1d:+.2f}%"
        )

        # Headline context: keyword-match first, fallback to general market news.
        headlines: List[str] = []
        headline_source = "none"
        try:
            news = data_mod.fetch_market_news_rss(max_items_per_feed=10)
            bs_news = data_mod.detect_black_swan_keywords(news)
            if bs_news:
                headlines = [n["title"] for n in bs_news[:5]]
                headline_source = "keyword"
            elif news:
                headlines = [n["title"] for n in news[:3]]
                headline_source = "general"
        except Exception as news_err:
            logger.warning(f"Headline fetch failed (non-critical): {news_err}")

        event_summary = {
            "market_moves": {
                "VIX": f"{vix:.2f} ({vix_spike_pct:+.1f}% intraday)",
                "SPY today": f"{spy_1d:+.2f}% (1d)",
            },
            "headlines": headlines,
            "headline_source": headline_source,
        }

        sent = tg_mod.send_black_swan_crash(event_summary)
        if sent:
            _write_marker(marker_path, run_date)  # only mark if the alert actually sent
        result["status"] = "type_a_crash"
        result["alert_sent"] = sent
        return result

    except Exception as e:
        logger.error(f"Black swan check error: {e}")
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ============================================================
# BLACK SWAN HEARTBEAT — weekly, Friday 23:00 UTC (Phase 2.5)
#
# See module docstring for full rationale. Short version: proves the
# watcher's threshold-comparison logic is reading real data each week
# (A2), and doubles as a repo-activity keepalive so GitHub never
# auto-disables the scheduled cron during a quiet market.
# ============================================================

def _compute_weekly_extremes() -> Dict[str, float]:
    """
    Worst single-day SPY return & largest single-day VIX spike over the
    last ~5 trading days. Fail-soft — returns 0.0 on insufficient data.

    PHASE 2.6.1 BUG FIX — this function was returning 0.0 / 0.0 EVERY WEEK.

    It requested period="5d", which yields ~5 bars. fetch_ohlcv() rejects
    anything under 10 bars (`if len(df) < 10: return None`) and returns None
    WITHOUT raising, so:
      - `if spy_df is not None` simply failed
      - the `except` block never fired, so nothing was logged
      - `out` was returned untouched at its 0.0 defaults

    The heartbeat's entire purpose (Phase 2.5, check A2) is to prove each
    week that the watcher's threshold-comparison logic is reading REAL
    market data. Reporting a hardcoded 0.0 dressed up as "worst SPY day"
    proved the opposite of what it claimed — and a fail-soft default that
    always reads "perfectly calm" is the most dangerous possible default
    for a crash detector's self-test.

    Fixed by requesting 1mo (~21 bars, clears the floor) and slicing to the
    last 6 closes, which yields the 5 daily returns actually wanted.
    """
    out = {"worst_spy_day_pct": 0.0, "max_vix_spike_pct": 0.0}
    ok = {"spy": False, "vix": False}

    try:
        spy_df = data_mod.fetch_ohlcv("SPY", period="1mo", interval="1d")
        if spy_df is not None and len(spy_df) >= 2:
            daily_rets = spy_df["Close"].tail(6).pct_change().dropna() * 100
            if len(daily_rets) > 0:
                out["worst_spy_day_pct"] = round(float(daily_rets.min()), 2)
                ok["spy"] = True
    except Exception as e:
        logger.warning(f"Weekly SPY extreme compute failed: {e}")

    try:
        vix_df = data_mod.fetch_ohlcv("^VIX", period="1mo", interval="1d")
        if vix_df is not None and len(vix_df) >= 2:
            daily_spikes = vix_df["Close"].tail(6).pct_change().dropna() * 100
            if len(daily_spikes) > 0:
                out["max_vix_spike_pct"] = round(float(daily_spikes.max()), 2)
                ok["vix"] = True
    except Exception as e:
        logger.warning(f"Weekly VIX extreme compute failed: {e}")

    # Say so when the numbers are defaults rather than measurements. A
    # silent 0.0 is indistinguishable from a genuinely flat week, which is
    # exactly how this went unnoticed.
    if not ok["spy"] or not ok["vix"]:
        logger.warning(
            f"Weekly extremes INCOMPLETE (spy_ok={ok['spy']}, vix_ok={ok['vix']}) "
            f"— any 0.0 below is a default, NOT a measurement."
        )
    else:
        logger.info(
            f"Weekly extremes: worst SPY day {out['worst_spy_day_pct']:+.2f}%, "
            f"max VIX spike {out['max_vix_spike_pct']:+.2f}%"
        )

    return out


def run_black_swan_heartbeat() -> Dict[str, Any]:
    """
    Weekly heartbeat — Friday 23:00 UTC. Always sends Telegram + always
    commits the keepalive marker (no dedup here; its own cron only fires
    once a week by design).
    """
    logger.info("Black swan heartbeat starting...")
    result = {"status": "sent", "alert_sent": False}

    try:
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        heartbeat_path = getattr(config, "BLACK_SWAN_HEARTBEAT_PATH", ".last_heartbeat")

        macro_ctx = data_mod.fetch_market_context()
        weekly_extremes = _compute_weekly_extremes()

        type_a_cfg = getattr(config, "BLACK_SWAN_TYPE_A", {})
        triggers = type_a_cfg.get("triggers", {})
        vix_min = triggers.get("vix_spike_pct_min", 30.0)
        spy_max = triggers.get("spy_drop_pct_max", -3.0)

        event_summary = {
            "vix_now": macro_ctx.get("vix", 0.0),
            "spy_price_now": macro_ctx.get("spy_price", 0.0),
            "worst_spy_day_pct": weekly_extremes["worst_spy_day_pct"],
            "max_vix_spike_pct": weekly_extremes["max_vix_spike_pct"],
            "spy_threshold": spy_max,
            "vix_threshold": vix_min,
        }

        sent = tg_mod.send_black_swan_heartbeat(event_summary)
        result["alert_sent"] = sent

        # Keepalive: always commit regardless of send status — this is
        # what prevents GitHub from auto-disabling the cron on a quiet repo.
        _write_marker(heartbeat_path, run_date)

    except Exception as e:
        logger.error(f"Black swan heartbeat error: {e}")
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
        choices=["daily", "black_swan", "heartbeat", "test"],
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
    elif args.mode == "heartbeat":
        result = run_black_swan_heartbeat()
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
