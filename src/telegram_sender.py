"""
telegram_sender.py — Telegram message delivery layer.

STAGE 2 REVISION (May 2026):
============================
ADDED:
  - Mandatory vs Optional clear display in signals (per user request)
  - RR warning flags surfaced from risk_manager (_rr_warning, _tier1_rr_warning)
  - Conviction tier emoji selection (HIGH 🔥 / STANDARD 📈 / WATCHLIST 👁️)

PHASE 2.3 REVISION (June 2026):
================================
CHANGED:
  - Entry line: now shows EMA9 limit price (not yesterday's close)
  - Confluence label: "context only" — no longer implies quality signal
  - Soft warnings section added: F6 (EMA9 ext) + F7 (RS gap vs SPY)
    Shows ✓ if within range, ⚠ if breaching threshold
  - Removed: max_holding_days line (per user revision)

ADDED:
  - send_shadow_log(): sends shadow log summary message after daily signals

PHASE 2.5 — BLACK SWAN REDESIGN (June 2026):
================================================
CHANGED — send_black_swan_crash():
  - Dropped the `trade_ideas` parameter entirely. The old function would
    render a "Defensive watchlist for tomorrow" section sourced from
    Opus-generated tickers (main.py's _generate_defensive_ideas(), now
    deleted). LLM conviction scores were proven non-predictive in Phase
    2.2 data (losers averaged HIGHER conviction than winners), so Opus
    has no business picking defensive tickers in a panic either — that
    whole code path is gone, not just hidden.
  - Dropped the prescriptive "HALT new long entries, wait 1-2 days" line
    and the "Defensive sectors to watch" list. The trade decision belongs
    to the user now, not the system.
  - Headline section reworked: keyword-matched headlines are labelled as
    such; if the watcher found none, it falls back to general market
    headlines and labels them differently. Either way the message now
    states explicitly that the cause is NOT verified by the system and
    tells the user to research independently before acting.
DELETED — send_black_swan_rally(), send_black_swan_info():
  - Type B (rally) and Type C (info) alerts are gone from the black swan
    watcher (main.py run_black_swan_check() no longer detects or calls
    these). Dead code removed rather than left orphaned.
ADDED — send_black_swan_heartbeat():
  - Weekly (Friday 23:00 UTC) liveness + keepalive message. Reports
    current VIX/SPY plus the worst single-day SPY move and largest
    single-day VIX spike seen in the past week, each compared against
    the Type A thresholds — so the heartbeat also proves the threshold
    comparison logic is reading real data, not just "I'm alive".

WARNING DISPLAY FIX (June 2026):
==================================
ADDED:
  - _sizing_warning and _conviction_warning surfaced in the same
    "Warnings" block as the existing _rr_warning / _tier1_rr_warning.
    These two fields started being set by risk_manager.py once the
    sizing-mismatch check (RULE 4) and the conviction threshold check
    (RULE 6) were downgraded from hard rejects to soft, non-blocking
    warnings — signals that previously never reached Telegram at all
    (e.g. URI/JPM/CVS on sizing, INTC on conviction — confirmed across
    runs #48/#50, June 20 2026) now pass through but should still show
    the underlying flag here so the user has the same visibility they'd
    have gotten from a rejection log line, just without the signal being
    blocked. Without this change, those two warnings only ever showed up
    in GitHub Actions logs, never in the message the user actually reads.

FUNCTIONS:
  send_daily_signals()         — Main signal delivery (header + each signal)
  send_shadow_log()            — Shadow log summary (after daily signals)
  send_no_signal_day()         — Explain why no signals fired
  send_black_swan_crash()      — Type A alert (alert-only, no LLM, no ideas)
  send_black_swan_heartbeat()  — Weekly liveness + keepalive message
  send_test_message()          — Connectivity test
  _send_message()              — Low-level Telegram API call

API DETAILS:
  Uses legacy Markdown parse mode (no escaping for dots/dollar signs)
  Per-message char limit: 4096
  Multiple signals = multiple messages (no combining)
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src import config

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
HTTP_TIMEOUT = 15
MAX_MESSAGE_LENGTH = 4096


# ============================================================
# CORE API
# ============================================================

def _send_message(
    text: str,
    parse_mode: Optional[str] = None,
) -> bool:
    """
    Send a single Telegram message via Bot API.
    Returns True on success, False on failure.
    """
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        logger.warning("Telegram credentials not set — skipping message")
        return False

    if not text:
        logger.warning("Empty message — skipping")
        return False

    # Truncate if too long
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH - 50] + "\n\n... (truncated)"

    if parse_mode is None:
        parse_mode = getattr(config, "TELEGRAM_PARSE_MODE", "Markdown")

    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        # Markdown parse error — retry without parse_mode
        if "can't parse entities" in str(resp.text).lower():
            logger.warning("Markdown parse error, retrying as plain text")
            payload["parse_mode"] = ""
            try:
                resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                return True
            except Exception as e2:
                logger.error(f"Telegram retry failed: {e2}")
                return False
        logger.error(f"Telegram HTTP error: {e}")
        return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_test_message() -> bool:
    """Quick connectivity test."""
    return _send_message(
        f"✅ *Test message*\nSP500 Swing Agent connected.\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ============================================================
# FORMATTING HELPERS
# ============================================================

def _conviction_tier(conviction: int) -> tuple:
    """Returns (emoji, label) for conviction level."""
    if conviction >= config.CONVICTION_HIGH:
        return config.HIGH_CONVICTION_EMOJI, "HIGH"
    if conviction >= config.CONVICTION_STANDARD:
        return config.STANDARD_CONVICTION_EMOJI, "STANDARD"
    return config.WATCHLIST_EMOJI, "WATCHLIST"


def _format_macro_header(macro: Dict[str, Any]) -> str:
    """Compact macro snapshot line."""
    vix = macro.get("vix", 0)
    spy = macro.get("spy_price", 0)
    spy_1d = macro.get("spy_1d_change_pct", 0)
    above_200 = macro.get("spy_above_200ma", False)
    return (
        f"{config.MACRO_ALERT_EMOJI} VIX: {vix:.1f} | "
        f"SPY: ${spy:.0f} ({spy_1d:+.2f}% 1d) | "
        f"{'above' if above_200 else 'below'} 200MA"
    )


def _format_soft_warning_line(label: str, value: float, unit: str,
                               threshold: float, breach_direction: str = "above") -> str:
    """
    Format a single soft warning line with pass/fail indicator.

    breach_direction:
      "above"  — breach if value > threshold (F6: ext > 1.5×ATR)
      "below"  — breach if value < threshold (F7: RS gap < -3.5%)
    """
    if breach_direction == "above":
        breached = value > threshold
    else:
        breached = value < threshold

    status = "⚠" if breached else "✓"
    range_label = "EXTENDED" if breached else "within range"
    return f"- {label} — {range_label} {status}\n"


def _format_signal(signal: Dict[str, Any]) -> str:
    """
    Format a single approved signal into a Telegram message.

    Phase 2.3 changes:
      - Entry = EMA9 limit price
      - Confluence label = "context only"
      - Soft warnings section (F6 + F7)
      - max_holding_days line removed

    June 2026 changes:
      - Risk manager warnings block now also surfaces _sizing_warning and
        _conviction_warning (see module docstring "WARNING DISPLAY FIX").
    """
    ticker = signal.get("ticker", "?")
    conviction = int(signal.get("conviction", 0))
    setup = signal.get("setup_type", "N/A")
    emoji, tier_label = _conviction_tier(conviction)

    entry = float(signal.get("entry", 0))       # Phase 2.3: entry = EMA9
    stop = float(signal.get("stop_loss", 0))
    tp1 = float(signal.get("tp1", 0))
    tp2 = float(signal.get("tp2", 0))
    rr1 = float(signal.get("rr_ratio", 0))
    rr2 = float(signal.get("rr_extended", 0))

    stop_pct = ((stop - entry) / entry * 100) if entry > 0 else 0
    tp1_pct = ((tp1 - entry) / entry * 100) if entry > 0 else 0
    tp2_pct = ((tp2 - entry) / entry * 100) if entry > 0 else 0

    # ── Header ──────────────────────────────────────────────
    msg = f"{emoji} *{ticker}* — {conviction}/100 ({tier_label})\n"
    msg += f"_{setup}_\n\n"

    # ── Price levels (Phase 2.3: entry = EMA9 limit) ────────
    msg += "💰 *Levels*\n"
    msg += f"Entry: `${entry:.2f}`\n"
    msg += f"Stop:  `${stop:.2f}` ({stop_pct:+.2f}%)\n"
    msg += f"TP1:   `${tp1:.2f}` ({tp1_pct:+.2f}%, RR {rr1:.2f})\n"
    msg += f"TP2:   `${tp2:.2f}` ({tp2_pct:+.2f}%, RR {rr2:.2f})\n\n"

    # ── Sizing (Phase 2.6) ──────────────────────────────────
    # Shows the equity BASIS this signal was sized against and the resulting
    # dollar risk. Notional is deliberately NOT shown: the operator sizes on
    # risk, and an extra number here is noise at the moment of execution.
    #
    # The basis differs per signal within a run — the waterfall cascade
    # deducts each signal's risk before sizing the next, so signal #2 is
    # sized against a smaller basis than #1. Showing the basis makes that
    # visible instead of looking like an inconsistency.
    basis = signal.get("_equity_basis")
    risk_usd = signal.get("risk_amount_usd")
    if basis is not None and risk_usd is not None:
        rank = signal.get("_size_rank")
        rank_txt = f" (#{rank})" if rank else ""
        msg += f"📐 *Sizing*{rank_txt}\n"
        msg += f"Basis: `${float(basis):.2f}` → Risk: `${float(risk_usd):.2f}`"
        msg += f" ({config.MAX_RISK_PER_TRADE_PCT}%)\n"
        if signal.get("_risk_capped"):
            msg += "⚠️ Risk below budget — stop tighter than risk %\n"
        msg += "\n"

    # ── Mandatory filters (all 5 passed by definition) ──────
    msg += f"{config.MANDATORY_PASS_EMOJI} *MANDATORY* (all 5 passed)\n"
    # Phase 2.6: read from config rather than repeating literals. These were
    # hard-coded here AND in indicators.py, so a threshold change would have
    # silently left this display announcing the old numbers.
    _ema_p = getattr(config, "EMA_TREND_ANCHOR_PERIOD", 50)
    _adx_t = getattr(config, "ADX_THRESHOLD", 25)
    _rsi_t = getattr(config, "RSI_MIN", 45)
    _vol_t = getattr(config, "VOLUME_RATIO_MIN", 1.3)
    _atr_t = getattr(config, "ATR_PCT_MIN", 1.5)
    msg += f"✓ Price > EMA({_ema_p})\n"
    msg += f"✓ ADX > {_adx_t} + (+DI dominant)\n"
    msg += f"✓ RSI > {_rsi_t}\n"
    msg += f"✓ Volume > {_vol_t}x avg\n"
    msg += f"✓ ATR > {_atr_t}% of price\n\n"

    # ── Confluence (Phase 2.3: "context only" — not predictive) ─
    confluence_display = signal.get("confluence_display", "N/A")
    msg += f"{config.CONFLUENCE_EMOJI} *CONFLUENCE* (context only): {confluence_display}\n\n"

    # ── Soft warnings (Phase 2.3: F6 + F7) ──────────────────
    shadow = signal.get("_shadow_indicators", {})
    ema9_ext_atr = float(shadow.get("ema9_ext_atr", 0))
    ema9_ext_pct = float(shadow.get("ema9_gap_pct", 0))
    rs_gap = float(shadow.get("rs_gap_vs_spy", 0))

    ema9_threshold = getattr(config, "EMA9_MAX_EXTENSION_ATR", 1.5)
    rs_threshold = getattr(config, "RS_MIN_GAP_VS_SPY", -0.035) * 100  # convert to %

    msg += f"{config.WARNING_EMOJI} *SOFT WARNINGS*\n"
    msg += _format_soft_warning_line(
        f"EMA9 ext: {ema9_ext_atr:.2f}×ATR ({ema9_ext_pct:.2f}%)",
        ema9_ext_atr, "", ema9_threshold, breach_direction="above",
    )
    msg += _format_soft_warning_line(
        f"RS vs SPY (10d): {rs_gap:+.1f}%",
        rs_gap, "", rs_threshold, breach_direction="below",
    )
    msg += "\n"

    # ── Score breakdown ──────────────────────────────────────
    breakdown = signal.get("breakdown", {})
    if breakdown:
        msg += "📊 *Score breakdown*\n"
        msg += f"- Technical:   {breakdown.get('technical', 0)}/30\n"
        msg += f"- Fundamental: {breakdown.get('fundamental', 0)}/25\n"
        msg += f"- Macro:       {breakdown.get('macro', 0)}/20\n"
        msg += f"- News:        {breakdown.get('news_catalyst', 0)}/15\n"
        msg += f"- RR:          {breakdown.get('rr_asymmetry', 0)}/10\n\n"

    # ── Risk manager warnings ────────────────────────────────
    # CHANGED (June 2026): added _sizing_warning and _conviction_warning —
    # see module docstring "WARNING DISPLAY FIX" for why these two exist now.
    rr_warn = signal.get("_rr_warning", "")
    tier_warn = signal.get("_tier1_rr_warning", "")
    sizing_warn = signal.get("_sizing_warning", "")
    conviction_warn = signal.get("_conviction_warning", "")
    warnings_text = ""
    if rr_warn:
        warnings_text += f"- {rr_warn}\n"
    if tier_warn:
        warnings_text += f"- {tier_warn}\n"
    if sizing_warn:
        warnings_text += f"- {sizing_warn}\n"
    if conviction_warn:
        warnings_text += f"- {conviction_warn}\n"

    if warnings_text:
        msg += f"{config.WARNING_EMOJI} *Warnings*\n{warnings_text}\n"
    else:
        msg += f"{config.WARNING_EMOJI} Warnings: None\n\n"

    # ── Thesis ───────────────────────────────────────────────
    thesis = signal.get("thesis", "")
    if thesis:
        msg += f"💭 *Thesis*\n{thesis[:400]}\n\n"

    # ── Invalidation ─────────────────────────────────────────
    invalidation = signal.get("invalidation", [])
    if invalidation:
        msg += "❌ *Invalidation*\n"
        for inv in invalidation[:3]:
            msg += f"- {inv}\n"

    return msg


# ============================================================
# DAILY SIGNALS
# ============================================================

def send_daily_signals(
    signals: List[Dict[str, Any]],
    macro: Dict[str, Any],
    equity_basis: Optional[float] = None,
    equity_source: str = "",
) -> bool:
    """
    Send approved signals via Telegram.
    Sends header message + one message per signal.
    Returns True if all messages sent successfully.

    Phase 2.6: the header now states the equity basis and where it came
    from. equity_source is one of "sheets" (live), "cache" (stale — the
    Sheets read failed) or "start" (no cache either). Anything other than
    "sheets" is flagged inline, because sizing computed off a stale balance
    looks completely normal otherwise — the failure has no other symptom.

    Both new args are optional so existing callers keep working.
    """
    if not signals:
        logger.warning("send_daily_signals called with empty list")
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Header message
    header = f"{config.SIGNAL_HEADER_EMOJI} *DAILY SIGNALS — {today}*\n\n"
    # PHASE 2.6.1: a verification run sends REAL messages. Without this
    # banner they are indistinguishable from tradable signals, and the
    # whole point of running the check early is that it happens before the
    # operator is in "execute what Telegram says" mode.
    if getattr(config, "VERIFICATION_RUN", False):
        header = (
            "🧪 *VERIFICATION RUN — DO NOT TRADE*\n"
            "_Pipeline check only. These rows are logged VETOED and are "
            "excluded from equity and from Batch 4 statistics._\n\n"
        ) + header
    header += _format_macro_header(macro) + "\n\n"
    header += f"📤 *{len(signals)} signal(s)* approved\n"
    if equity_basis is not None:
        src_flag = {
            "sheets": "",
            "cache": "  ⚠️ CACHED — verify sheet",
            "start": "  🔴 NO DATA — do not execute",
        }.get(equity_source, "")
        header += f"💵 Equity: `${float(equity_basis):.2f}`{src_flag}\n"
    header += "\n"

    for s in signals:
        sig_emoji, _ = _conviction_tier(s.get("conviction", 0))
        header += f"{sig_emoji} {s.get('ticker')} — {s.get('conviction')}/100\n"

    success = _send_message(header)
    if not success:
        logger.error("Failed to send signals header")

    all_ok = success
    for sig in signals:
        ok = _send_message(_format_signal(sig))
        if not ok:
            logger.error(f"Failed to send signal for {sig.get('ticker')}")
            all_ok = False

    return all_ok


# ============================================================
# SHADOW LOG (Phase 2.3)
# ============================================================

def send_shadow_log(
    signals: List[Dict[str, Any]],
    run_date: str,
) -> bool:
    """
    Send shadow log summary message after daily signals.
    Shows EMA9 gap, extension, and RS gap per signal for quick daily review.
    Always sent regardless of signal count (even 1 signal).

    Format:
      📝 SHADOW LOG — 2026-06-01
      ──────────────────────────────────────────
      NVDA  | Close $182.50 → EMA9 $180.20 | gap 1.27% | ext 0.80×ATR | RS −1.2%
      IBM   | Close $235.10 → EMA9 $231.80 | gap 1.42% | ext 1.10×ATR | RS +0.5%
      ──────────────────────────────────────────
      Avg EMA9 gap: 1.35% | Fill likely if gap ≤ 1.5%
    """
    if not signals:
        return False

    sep = "──────────────────────────────────────────"
    msg = f"📝 *SHADOW LOG — {run_date}*\n"
    msg += f"`{sep}`\n"

    gaps = []
    for sig in signals:
        ticker = sig.get("ticker", "?")
        shadow = sig.get("_shadow_indicators", {})

        close_price = float(shadow.get("close_price", 0))
        ema9_price = float(shadow.get("ema9_price", 0))
        ema9_gap_pct = float(shadow.get("ema9_gap_pct", 0))
        ema9_ext_atr = float(shadow.get("ema9_ext_atr", 0))
        rs_gap = float(shadow.get("rs_gap_vs_spy", 0))

        gaps.append(ema9_gap_pct)

        msg += (
            f"`{ticker:<6}` | "
            f"Close ${close_price:.2f} → EMA9 ${ema9_price:.2f} | "
            f"gap {ema9_gap_pct:.2f}% | "
            f"ext {ema9_ext_atr:.2f}×ATR | "
            f"RS {rs_gap:+.1f}%\n"
        )

    msg += f"`{sep}`\n"

    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    msg += f"Avg EMA9 gap: {avg_gap:.2f}% | Fill likely if gap ≤ 1.5%"

    return _send_message(msg)


# ============================================================
# NO SIGNAL DAY
# ============================================================

# ============================================================
# PHASE 2.6 — TWO-STAGE ENTRY ALERTS + OPS WARNINGS
# ============================================================

def send_stage2_alerts(fallbacks: List[Dict[str, Any]], run_date: str) -> bool:
    """
    Alert for near-miss signals eligible for Stage-2 execution.

    Context: yesterday's EMA9 limit did not fill, but the session low DID
    enter [EMA9, EMA9 + ENTRY2_ATR_TOLERANCE x ATR]. The setup is executed
    at MARKET on today's open instead.

    Two things this message must make unmissable, because getting either
    wrong silently corrupts the R-multiple on the trade:

      1. Stop and TP must be recomputed from the ACTUAL fill price, not
         from EMA9. A market entry above EMA9 with an EMA9-derived stop is
         a wider-than-1R risk wearing a 1R label.
      2. The actual fill must be typed back into the pending tab. The
         system never observes it — execution is manual.

    Why this exists at all: on Batch 3, unfilled EMA9 orders went on to hit
    TP1 at a rate significantly higher than filled ones (Fisher exact
    p ~ 0.007 on 8/8). The limit was systematically filling on weakness and
    missing the strongest continuations. ENTRY2_ATR_TOLERANCE = 0.15 is an
    IN-SAMPLE fit on 5 observed near-misses and catches 2 of them — it is
    provisional, and this alert path is how it gets tested out-of-sample.
    """
    if not fallbacks:
        return False

    # Phase 2.6.1: read from config rather than restating literals, so the
    # alert can never drift from what _apply_fallback_geometry() actually
    # computes. Same lesson as the unwired filter thresholds (2.6 §7.4).
    stop_mult = float(getattr(config, "STOP_ATR_MULT", 1.5))
    tp1_mult = float(getattr(config, "TP1_R_MULT", 2.0))
    tp2_mult = float(getattr(config, "TP2_R_MULT", 3.0))

    sep = "──────────────────────────────────────────"
    msg = f"🔁 *STAGE 2 ENTRY — {run_date}*\n"
    msg += "_Yesterday's EMA9 limit missed by a hair. Market on open._\n"
    msg += f"`{sep}`\n\n"

    for fb in fallbacks:
        ticker = fb.get("ticker", "?")
        ema9 = float(fb.get("ema9_price", 0) or 0)
        zone_high = float(fb.get("zone_high", 0) or 0)
        low = float(fb.get("day_low", 0) or 0)
        atr = float(fb.get("atr", 0) or 0)
        miss_atr = ((low - ema9) / atr) if atr > 0 else 0.0

        msg += f"*{ticker}* (signalled {fb.get('signal_date', '?')})\n"
        msg += f"EMA9 limit: `${ema9:.2f}`\n"
        msg += f"Session low: `${low:.2f}` — missed by {miss_atr:.2f}×ATR\n"
        msg += f"Zone was: `${ema9:.2f}` → `${zone_high:.2f}`\n"
        msg += "➡️ Execute at MARKET on open\n"

        # PHASE 2.6.1: show the levels for the WORST fill still inside the
        # zone. Previously this message gave only formulas, which meant the
        # operator had to do ATR arithmetic on a phone at the open — and if
        # they skipped it, the trade ran on EMA9 levels that no longer
        # matched the fill. Concrete numbers make the correct action the
        # lazy one. 1R is identical either way (stop travels with the fill),
        # so these are a floor, not an estimate of the actual fill.
        if atr > 0:
            ref = zone_high if zone_high > 0 else ema9
            stop_ref = ref - stop_mult * atr
            r_ref = ref - stop_ref
            msg += (
                f"_If filled at zone top ${ref:.2f}:_ "
                f"stop `${stop_ref:.2f}` · TP1 `${ref + tp1_mult * r_ref:.2f}` "
                f"· TP2 `${ref + tp2_mult * r_ref:.2f}` (1R = ${r_ref:.2f})\n"
            )
        msg += "\n"

    msg += f"`{sep}`\n"
    msg += "⚠️ *Recompute stop and TP from your ACTUAL fill*, not from EMA9.\n"
    msg += (
        f"Stop = fill − {stop_mult}×ATR | "
        f"TP1 = fill + {tp1_mult:g}R | TP2 = fill + {tp2_mult:g}R\n"
    )
    msg += "📌 Share count and risk $ are UNCHANGED — 1R is the same wherever\n"
    msg += "you fill, because the stop moves with it. Only the levels shift.\n"
    msg += "📝 Enter the fill price in `pending_entries` → `actual_entry`.\n"
    msg += "_Next run rewrites the shadow log's stop/TP from it automatically._"

    return _send_message(msg)


def send_ops_warnings(warnings: List[str]) -> bool:
    """
    One batched message for operational problems the operator must act on:
    a cached (stale) equity value, shadow-log rows still missing an outcome,
    closed rows missing risk_usd, an unreadable pending tab.

    Batched rather than interleaved with signals on purpose — these are easy
    to scroll past when scattered, and every one of them means the numbers
    in the signal messages are quietly less trustworthy than they look.

    Sent on EVERY exit path, including no-signal days. A stale-equity warning
    matters more on a quiet day, because nothing else would reveal it.
    """
    if not warnings:
        return False
    msg = "🛠️ *OPS WARNINGS*\n"
    msg += "_Signals above are still valid — but check these first._\n\n"
    for w in warnings:
        msg += f"• {w}\n\n"
    return _send_message(msg)


def send_no_signal_day(
    reason: str,
    macro: Dict[str, Any],
) -> bool:
    """Send explanation message when no signals qualify."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    msg = f"{config.NO_SIGNAL_EMOJI} *No Signal — {today}*\n\n"
    msg += _format_macro_header(macro) + "\n\n"
    msg += f"📋 *Reason*\n{reason}\n"

    return _send_message(msg)


# ============================================================
# BLACK SWAN — TYPE A ALERT (Phase 2.5 redesign)
#
# trade_ideas parameter removed entirely — no LLM, no defensive watchlist.
# No prescriptive action line ("HALT...") — decision belongs to the user.
# Headline section now carries an explicit "unverified, research yourself"
# caveat, and distinguishes keyword-matched headlines from general
# fallback headlines so the user knows how strong the context actually is.
# ============================================================

def send_black_swan_crash(event_summary: Dict[str, Any]) -> bool:
    """
    Type A: crash detected. Alert-only (no LLM, no defensive ideas).

    event_summary expected keys:
      market_moves      — dict of label -> formatted string (VIX, SPY today)
      headlines          — list of headline strings (possibly empty)
      headline_source    — "keyword" | "general" | "none"
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{config.BLACK_SWAN_CRASH_EMOJI} *BLACK SWAN — TYPE A: CRASH*\n"
    msg += f"_{timestamp}_\n\n"

    msg += "📉 *Market moves*\n"
    for k, v in event_summary.get("market_moves", {}).items():
        msg += f"- {k}: {v}\n"
    msg += "\n"

    headlines = event_summary.get("headlines", [])
    source = event_summary.get("headline_source", "none")

    if headlines:
        if source == "keyword":
            msg += "📰 *Possible context* (keyword-matched headlines)\n"
        else:
            msg += "📰 *Possible context* (general market headlines)\n"
        for h in headlines[:5]:
            msg += f"- {h[:110]}\n"
        msg += "\n"
        msg += "⚠️ *News can't be verified — do your own research NOW.*\n"
        msg += "_Cause not confirmed by the system; headlines are context only._\n\n"
    else:
        msg += "⚠️ *No headlines retrieved — cause unknown. Do your own research NOW.*\n\n"

    msg += "🔎 Threshold breached. The trade decision is yours."

    return _send_message(msg)


# ============================================================
# BLACK SWAN — WEEKLY HEARTBEAT (Phase 2.5, new)
#
# A2 design: not just "I'm alive" — reports this week's worst SPY day
# and largest VIX spike compared against the same Type A thresholds, so
# the message also proves the comparison logic is reading real data.
# Sent every Friday 23:00 UTC regardless of whether Type A fired that
# week. The marker write that follows this (in main.py) is what actually
# keeps the repo "active" so GitHub doesn't auto-disable the cron.
# ============================================================

def send_black_swan_heartbeat(event_summary: Dict[str, Any]) -> bool:
    """
    Weekly liveness + keepalive message.

    event_summary expected keys:
      vix_now              — float
      spy_price_now        — float
      worst_spy_day_pct    — float (most negative single-day SPY return this week)
      max_vix_spike_pct    — float (largest single-day VIX spike this week)
      spy_threshold        — float (Type A SPY trigger, e.g. -3.0)
      vix_threshold        — float (Type A VIX trigger, e.g. 30.0)
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    vix_now = event_summary.get("vix_now", 0.0)
    spy_now = event_summary.get("spy_price_now", 0.0)
    worst_spy_day = event_summary.get("worst_spy_day_pct", 0.0)
    max_vix_spike = event_summary.get("max_vix_spike_pct", 0.0)
    spy_threshold = event_summary.get("spy_threshold", -3.0)
    vix_threshold = event_summary.get("vix_threshold", 30.0)

    spy_breached = worst_spy_day <= spy_threshold
    vix_breached = max_vix_spike >= vix_threshold

    msg = "💓 *BLACK SWAN WATCHER — Weekly Heartbeat*\n"
    msg += f"_{timestamp}_\n\n"

    msg += "📊 *Right now*\n"
    msg += f"- VIX: {vix_now:.2f}\n"
    msg += f"- SPY: ${spy_now:.2f}\n\n"

    msg += "📅 *This week's extremes vs Type A thresholds*\n"
    msg += (
        f"- Worst SPY day: {worst_spy_day:+.2f}% "
        f"(threshold {spy_threshold:+.1f}%) "
        f"{'⚠ breached' if spy_breached else '✓ within range'}\n"
    )
    msg += (
        f"- Max VIX spike: {max_vix_spike:+.1f}% "
        f"(threshold {vix_threshold:+.1f}%) "
        f"{'⚠ breached' if vix_breached else '✓ within range'}\n\n"
    )

    if spy_breached or vix_breached:
        msg += "ℹ️ A threshold was crossed this week — check your crash alerts.\n"
    else:
        msg += "✅ Watcher alive. No Type A crash this week."

    return _send_message(msg)


# ============================================================
# SMOKE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("\n=== Connectivity Test ===")
    ok = send_test_message()
    print(f"Test message sent: {ok}")

    print("\n=== Mock Signal Test ===")
    mock_signal = {
        "ticker": "NVDA",
        "conviction": 78,
        "tier_1_pass": True,
        "direction": "LONG",
        "setup_type": "Trend continuation + volume surge",
        "entry": 180.20,        # EMA9 limit
        "stop_loss": 177.50,
        "tp1": 185.90,
        "tp2": 191.60,
        "rr_ratio": 2.00,
        "rr_extended": 3.00,
        "position_size_usd": 91.00,
        "shares": 0.5049,
        "risk_amount_usd": 2.70,
        "thesis": (
            "NVDA breaking out of 6-week consolidation with strong volume confirmation. "
            "AI sector tailwind continues with strong earnings momentum."
        ),
        "invalidation": [
            "Close below EMA9 support",
            "VIX spikes > 25",
            "Volume dries up < 0.8x avg",
        ],
        "confluence_display": "3/3 (MACD✓ EMA9✓ EMA21✓)",
        "breakdown": {
            "technical": 26,
            "fundamental": 20,
            "macro": 16,
            "news_catalyst": 11,
            "rr_asymmetry": 8,
        },
        "_shadow_indicators": {
            "close_price": 182.50,
            "ema9_price": 180.20,
            "ema9_gap_pct": 1.27,
            "ema9_ext_atr": 0.80,
            "rs_gap_vs_spy": -1.2,
            "spy_10d_return": 5.0,
            "stock_10d_return": 3.8,
            "confluence_count": 3,
            "adx": 38.2,
            "rsi": 61.4,
            "volume_ratio": 1.85,
            "entry_close": 182.50,
        },
    }

    mock_macro = {
        "vix": 17.2,
        "spy_price": 510.50,
        "spy_1d_change_pct": 0.45,
        "spy_above_200ma": True,
    }

    print("\n=== Mock Signal Format ===")
    print(_format_signal(mock_signal))

    print("\n=== Mock Signal With Sizing + Conviction Warnings ===")
    mock_signal_warned = {
        **mock_signal,
        "ticker": "URI",
        "conviction": 58,
        "_sizing_warning": (
            "⚠️ Sizing mismatch: Opus claimed $3.00, computed $0.13 — "
            "sizing is manual, info only"
        ),
        "_conviction_warning": (
            "ℹ️ Conviction 58 below 65 reference threshold — FYI only, not a "
            "quality signal (see Phase 2.2 Finding 1: conviction is "
            "non-predictive in this system's data)"
        ),
    }
    print(_format_signal(mock_signal_warned))

    print("\n=== Mock Shadow Log ===")
    mock_shadow_msg = send_shadow_log([mock_signal], "2026-06-01")
    print(f"Shadow log sent: {mock_shadow_msg}")

    print("\n=== Mock Black Swan Crash (keyword) ===")
    mock_crash_event = {
        "market_moves": {
            "VIX": "34.20 (+31.5% intraday)",
            "SPY today": "-3.40% (1d)",
        },
        "headlines": [
            "Markets tumble as Fed signals emergency rate concerns",
            "Bank stocks slide amid contagion fears",
        ],
        "headline_source": "keyword",
    }
    print(f"Crash alert sent: {send_black_swan_crash(mock_crash_event)}")

    print("\n=== Mock Black Swan Heartbeat ===")
    mock_heartbeat_event = {
        "vix_now": 16.8,
        "spy_price_now": 745.30,
        "worst_spy_day_pct": -1.2,
        "max_vix_spike_pct": 12.4,
        "spy_threshold": -3.0,
        "vix_threshold": 30.0,
    }
    print(f"Heartbeat sent: {send_black_swan_heartbeat(mock_heartbeat_event)}")
