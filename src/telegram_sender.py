"""
telegram_sender.py — Telegram message delivery layer.

STAGE 2 REVISION (May 2026):
============================
ADDED:
  - Mandatory vs Optional clear display in signals (per user request)
  - RR warning flags surfaced from risk_manager (_rr_warning, _tier1_rr_warning)
  - Conviction tier emoji selection (HIGH 🔥 / STANDARD 📈 / WATCHLIST 👁️)
  - Type A black swan defensive ideas display
  - Type B/C alert formatting

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

FUNCTIONS:
  send_daily_signals()       — Main signal delivery (header + each signal)
  send_shadow_log()          — Shadow log summary (after daily signals)
  send_no_signal_day()       — Explain why no signals fired
  send_black_swan_crash()    — Type A alert + defensive ideas
  send_black_swan_rally()    — Type B alert + sector guidance
  send_black_swan_info()     — Type C informational alert
  send_test_message()        — Connectivity test
  _send_message()            — Low-level Telegram API call

API DETAILS:
  Uses legacy Markdown parse mode (no escaping for dots/dollar signs)
  Per-message char limit: 4096
  Multiple signals = multiple messages (no combining)
"""
import logging
from datetime import datetime
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
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
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
    return f"- {label}: {value:.2f}{unit} — {range_label} {status}\n"


def _format_signal(signal: Dict[str, Any]) -> str:
    """
    Format a single approved signal into a Telegram message.

    Phase 2.3 changes:
      - Entry = EMA9 limit price
      - Confluence label = "context only"
      - Soft warnings section (F6 + F7)
      - max_holding_days line removed
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

    pos_size = float(signal.get("position_size_usd", 0))
    shares = float(signal.get("shares", 0))
    risk = float(signal.get("risk_amount_usd", 0))

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

    # ── Sizing ───────────────────────────────────────────────
    msg += "📦 *Sizing (reference)*\n"
    msg += f"Position: ${pos_size:.2f} ({shares:.4f} shares)\n"
    msg += f"Risk:     ${risk:.2f}\n\n"

    # ── Mandatory filters (all 5 passed by definition) ──────
    msg += f"{config.MANDATORY_PASS_EMOJI} *MANDATORY* (all 5 passed)\n"
    msg += "✓ Price > EMA(50)\n"
    msg += "✓ ADX > 25 + (+DI dominant)\n"
    msg += "✓ RSI > 45\n"
    msg += "✓ Volume > 1.3x avg\n"
    msg += "✓ ATR > 1.5% of price\n\n"

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
    rr_warn = signal.get("_rr_warning", "")
    tier_warn = signal.get("_tier1_rr_warning", "")
    warnings_text = ""
    if rr_warn:
        warnings_text += f"- {rr_warn}\n"
    if tier_warn:
        warnings_text += f"- {tier_warn}\n"

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
) -> bool:
    """
    Send approved signals via Telegram.
    Sends header message + one message per signal.
    Returns True if all messages sent successfully.
    """
    if not signals:
        logger.warning("send_daily_signals called with empty list")
        return False

    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Header message
    header = f"{config.SIGNAL_HEADER_EMOJI} *DAILY SIGNALS — {today}*\n\n"
    header += _format_macro_header(macro) + "\n\n"
    header += f"📤 *{len(signals)} signal(s)* approved\n\n"

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

def send_no_signal_day(
    reason: str,
    macro: Dict[str, Any],
) -> bool:
    """Send explanation message when no signals qualify."""
    today = datetime.utcnow().strftime("%Y-%m-%d")

    msg = f"{config.NO_SIGNAL_EMOJI} *No Signal — {today}*\n\n"
    msg += _format_macro_header(macro) + "\n\n"
    msg += f"📋 *Reason*\n{reason}\n"

    return _send_message(msg)


# ============================================================
# BLACK SWAN ALERTS
# ============================================================

def send_black_swan_crash(
    event_summary: Dict[str, Any],
    trade_ideas: List[Dict[str, Any]],
) -> bool:
    """
    Type A: Risk-off crash detected.
    Sends alert + defensive trade ideas (if Opus generated any).
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{config.BLACK_SWAN_CRASH_EMOJI} *BLACK SWAN — TYPE A: CRASH*\n"
    msg += f"_{timestamp}_\n\n"

    msg += "📉 *Market moves*\n"
    for k, v in event_summary.get("market_moves", {}).items():
        msg += f"- {k}: {v}\n"
    msg += "\n"

    msg += "⚠️ *Action*\n"
    msg += "HALT new long entries. Wait 1-2 days for stabilization.\n\n"

    preferred = event_summary.get("preferred_sectors", [])
    if preferred:
        msg += "🛡️ *Defensive sectors to watch*\n"
        for sector in preferred:
            msg += f"- {sector}\n"
        msg += "\n"

    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Recent headlines*\n"
        for h in headlines[:5]:
            msg += f"- {h[:100]}\n"
        msg += "\n"

    success = _send_message(msg)

    if trade_ideas:
        ideas_header = (
            f"💡 *Defensive watchlist for tomorrow* — {len(trade_ideas)} ideas\n\n"
        )
        for s in trade_ideas:
            sig_emoji, _ = _conviction_tier(s.get("conviction", 0))
            ideas_header += f"{sig_emoji} {s.get('ticker')} — {s.get('conviction')}/100\n"
        _send_message(ideas_header)

        for idea in trade_ideas:
            _send_message(_format_signal(idea))

    return success


def send_black_swan_rally(
    event_summary: Dict[str, Any],
) -> bool:
    """
    Type B: Risk-on rally detected.
    Alert + sector guidance only — no defensive ideas.
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{config.BLACK_SWAN_RALLY_EMOJI} *BLACK SWAN — TYPE B: RALLY*\n"
    msg += f"_{timestamp}_\n\n"

    msg += "📈 *Market moves*\n"
    for k, v in event_summary.get("market_moves", {}).items():
        msg += f"- {k}: {v}\n"
    msg += "\n"

    msg += "⚠️ *Action*\n"
    msg += "Avoid chasing breakouts. Wait for pullback or consolidation.\n\n"

    preferred = event_summary.get("preferred_sectors", [])
    if preferred:
        msg += "✅ *Preferred sectors*\n"
        msg += ", ".join(preferred) + "\n\n"

    skip = event_summary.get("skip_sectors", [])
    if skip:
        msg += "❌ *Skip sectors*\n"
        msg += ", ".join(skip) + "\n\n"

    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Recent headlines*\n"
        for h in headlines[:3]:
            msg += f"- {h[:100]}\n"

    return _send_message(msg)


def send_black_swan_info(
    event_summary: Dict[str, Any],
) -> bool:
    """
    Type C: Informational alert (RSS keywords + VIX move).
    No action required, just FYI.
    """
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{config.BLACK_SWAN_INFO_EMOJI} *BLACK SWAN — TYPE C: NEWS ALERT*\n"
    msg += f"_{timestamp}_\n\n"

    msg += "📊 *Market moves*\n"
    for k, v in event_summary.get("market_moves", {}).items():
        msg += f"- {k}: {v}\n"
    msg += "\n"

    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Triggered keywords in headlines*\n"
        for h in headlines[:5]:
            msg += f"- {h[:120]}\n"
        msg += "\n"

    msg += "ℹ️ Informational only. No action required."
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

    print("\n=== Mock Shadow Log ===")
    mock_shadow_msg = send_shadow_log([mock_signal], "2026-06-01")
    print(f"Shadow log sent: {mock_shadow_msg}")
