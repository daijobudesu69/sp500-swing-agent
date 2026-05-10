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

FUNCTIONS:
  send_daily_signals()       — Main signal delivery (header + each signal)
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
            logger.warning(f"Markdown parse error, retrying as plain text")
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


def _format_signal(signal: Dict[str, Any]) -> str:
    """Format a single signal into a Telegram message."""
    ticker = signal.get("ticker", "?")
    conviction = int(signal.get("conviction", 0))
    setup = signal.get("setup_type", "N/A")
    emoji, tier_label = _conviction_tier(conviction)

    entry = float(signal.get("entry", 0))
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

    # Header
    msg = f"{emoji} *{ticker}* — {conviction}/100 ({tier_label})\n"
    msg += f"_{setup}_\n\n"

    # Price levels
    msg += "💰 *Levels*\n"
    msg += f"Entry: `${entry:.2f}`\n"
    msg += f"Stop:  `${stop:.2f}` ({stop_pct:+.2f}%)\n"
    msg += f"TP1:   `${tp1:.2f}` ({tp1_pct:+.2f}%, RR {rr1:.2f})\n"
    msg += f"TP2:   `${tp2:.2f}` ({tp2_pct:+.2f}%, RR {rr2:.2f})\n\n"

    # Sizing (informational — user manages capital separately)
    msg += "📦 *Sizing (reference)*\n"
    msg += f"Position: ${pos_size:.2f} ({shares:.4f} shares)\n"
    msg += f"Risk:     ${risk:.2f}\n\n"

    # Mandatory section (all 5 passed by definition)
    mandatory_pass = config.MANDATORY_PASS_EMOJI
    msg += f"{mandatory_pass} *MANDATORY* (all 5 passed)\n"
    msg += "✓ Price > EMA(50)\n"
    msg += "✓ ADX > 25 + (+DI dominant)\n"
    msg += "✓ RSI > 45\n"
    msg += "✓ Volume > 1.3x avg\n"
    msg += "✓ ATR > 1.5% of price\n\n"

    # Optional confluence
    confluence_display = signal.get("confluence_display", "N/A")
    confluence_emoji = config.CONFLUENCE_EMOJI
    msg += f"{confluence_emoji} *CONFLUENCE*: {confluence_display}\n\n"

    # Conviction breakdown if available
    breakdown = signal.get("breakdown", {})
    if breakdown:
        msg += "📊 *Score breakdown*\n"
        msg += f"• Technical:   {breakdown.get('technical', 0)}/30\n"
        msg += f"• Fundamental: {breakdown.get('fundamental', 0)}/25\n"
        msg += f"• Macro:       {breakdown.get('macro', 0)}/20\n"
        msg += f"• News:        {breakdown.get('news_catalyst', 0)}/15\n"
        msg += f"• RR:          {breakdown.get('rr_asymmetry', 0)}/10\n\n"

    # Warnings
    rr_warn = signal.get("_rr_warning", "")
    tier_warn = signal.get("_tier1_rr_warning", "")
    warnings_text = ""
    if rr_warn:
        warnings_text += f"• {rr_warn}\n"
    if tier_warn:
        warnings_text += f"• {tier_warn}\n"

    warning_emoji = config.WARNING_EMOJI
    if warnings_text:
        msg += f"{warning_emoji} *Warnings*\n{warnings_text}\n"
    else:
        msg += f"{warning_emoji} Warnings: None\n\n"

    # Thesis
    thesis = signal.get("thesis", "")
    if thesis:
        thesis_short = thesis[:400]
        msg += f"💭 *Thesis*\n{thesis_short}\n\n"

    # Invalidation
    invalidation = signal.get("invalidation", [])
    if invalidation:
        msg += "❌ *Invalidation*\n"
        for inv in invalidation[:3]:
            msg += f"• {inv}\n"
        msg += "\n"

    # Hold period
    max_days = signal.get("max_holding_days", 7)
    msg += f"📅 Hold: {max_days} days max"

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
    header_emoji = config.SIGNAL_HEADER_EMOJI

    # Header message
    header = f"{header_emoji} *DAILY SIGNALS — {today}*\n\n"
    header += _format_macro_header(macro) + "\n\n"
    header += f"📤 *{len(signals)} signal(s)* approved\n\n"

    # Quick summary list
    for s in signals:
        emoji, _ = _conviction_tier(s.get("conviction", 0))
        header += f"{emoji} {s.get('ticker')} — {s.get('conviction')}/100\n"

    success = _send_message(header)
    if not success:
        logger.error("Failed to send signals header")

    # Each signal as own message
    all_ok = success
    for sig in signals:
        ok = _send_message(_format_signal(sig))
        if not ok:
            logger.error(f"Failed to send signal for {sig.get('ticker')}")
            all_ok = False

    return all_ok


# ============================================================
# NO SIGNAL DAY
# ============================================================

def send_no_signal_day(
    reason: str,
    macro: Dict[str, Any],
) -> bool:
    """Send explanation message when no signals qualify."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    no_sig_emoji = config.NO_SIGNAL_EMOJI

    msg = f"{no_sig_emoji} *No Signal — {today}*\n\n"
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
    crash_emoji = config.BLACK_SWAN_CRASH_EMOJI
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{crash_emoji} *BLACK SWAN — TYPE A: CRASH*\n"
    msg += f"_{timestamp}_\n\n"

    # Market moves
    msg += "📉 *Market moves*\n"
    moves = event_summary.get("market_moves", {})
    for k, v in moves.items():
        msg += f"• {k}: {v}\n"
    msg += "\n"

    # Action
    msg += "⚠️ *Action*\n"
    msg += "HALT new long entries. Wait 1-2 days for stabilization.\n\n"

    # Preferred sectors
    preferred = event_summary.get("preferred_sectors", [])
    if preferred:
        msg += "🛡️ *Defensive sectors to watch*\n"
        for sector in preferred:
            msg += f"• {sector}\n"
        msg += "\n"

    # Recent headlines
    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Recent headlines*\n"
        for h in headlines[:5]:
            msg += f"• {h[:100]}\n"
        msg += "\n"

    success = _send_message(msg)

    # Send trade ideas as separate messages (use full signal format)
    if trade_ideas:
        ideas_header = (
            f"💡 *Defensive watchlist for tomorrow* — {len(trade_ideas)} ideas\n\n"
        )
        for s in trade_ideas:
            emoji, _ = _conviction_tier(s.get("conviction", 0))
            ideas_header += f"{emoji} {s.get('ticker')} — {s.get('conviction')}/100\n"
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
    rally_emoji = config.BLACK_SWAN_RALLY_EMOJI
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{rally_emoji} *BLACK SWAN — TYPE B: RALLY*\n"
    msg += f"_{timestamp}_\n\n"

    # Market moves
    msg += "📈 *Market moves*\n"
    moves = event_summary.get("market_moves", {})
    for k, v in moves.items():
        msg += f"• {k}: {v}\n"
    msg += "\n"

    # Action
    msg += "⚠️ *Action*\n"
    msg += "Avoid chasing breakouts. Wait for pullback or consolidation.\n\n"

    # Preferred sectors
    preferred = event_summary.get("preferred_sectors", [])
    if preferred:
        msg += "✅ *Preferred sectors*\n"
        msg += ", ".join(preferred) + "\n\n"

    # Skip sectors
    skip = event_summary.get("skip_sectors", [])
    if skip:
        msg += "❌ *Skip sectors*\n"
        msg += ", ".join(skip) + "\n\n"

    # Headlines
    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Recent headlines*\n"
        for h in headlines[:3]:
            msg += f"• {h[:100]}\n"

    return _send_message(msg)


def send_black_swan_info(
    event_summary: Dict[str, Any],
) -> bool:
    """
    Type C: Informational alert (RSS keywords + VIX move).
    No action required, just FYI.
    """
    info_emoji = config.BLACK_SWAN_INFO_EMOJI
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"{info_emoji} *BLACK SWAN — TYPE C: NEWS ALERT*\n"
    msg += f"_{timestamp}_\n\n"

    # Market moves
    msg += "📊 *Market moves*\n"
    moves = event_summary.get("market_moves", {})
    for k, v in moves.items():
        msg += f"• {k}: {v}\n"
    msg += "\n"

    # Headlines
    headlines = event_summary.get("headlines", [])
    if headlines:
        msg += "📰 *Triggered keywords in headlines*\n"
        for h in headlines[:5]:
            msg += f"• {h[:120]}\n"
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
        "entry": 142.50,
        "stop_loss": 137.80,
        "tp1": 152.00,
        "tp2": 161.50,
        "rr_ratio": 2.02,
        "rr_extended": 4.04,
        "position_size_usd": 91.00,
        "shares": 0.638,
        "risk_amount_usd": 2.99,
        "thesis": "NVDA breaking out of 6-week consolidation with strong volume confirmation. AI sector tailwind continues with strong earnings momentum.",
        "invalidation": ["Close below 137.50", "Volume drying up <0.8x avg"],
        "max_holding_days": 5,
        "confluence_display": "3/3 (MACD✓ EMA9✓ EMA21✓)",
        "breakdown": {
            "technical": 26,
            "fundamental": 20,
            "macro": 16,
            "news_catalyst": 11,
            "rr_asymmetry": 8,
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
