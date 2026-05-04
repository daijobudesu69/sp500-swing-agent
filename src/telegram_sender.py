"""
Telegram sender — formats signals and pushes to user's chat.

Message types:
  1. Daily signals (3-5 trades)
  2. No-signal day (skip with explanation)
  3. Black swan Type A (CRASH — with immediate trade ideas)
  4. Black swan Type B (RALLY — alert only, wait for daily scan)
  5. Black swan Type C (INFO — informational alert)
"""
import logging
import time
from datetime import datetime
from typing import Dict, List, Any, Optional

import requests

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# CORE TELEGRAM API WRAPPER
# ============================================================

def _send_message(text: str, parse_mode: str = "Markdown", disable_preview: bool = True) -> bool:
    """Low-level Telegram API call. Returns True on success."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not set")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        result = r.json()
        if not result.get("ok"):
            logger.error(f"Telegram API error: {result}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_long_message(text: str, max_chunk: int = 3500) -> bool:
    """
    Telegram has 4096 char limit per message. Split if needed.
    Splits on double newline to keep formatting clean.
    """
    if len(text) <= max_chunk:
        return _send_message(text)

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_chunk:
            if current:
                chunks.append(current.strip())
            current = paragraph + "\n\n"
        else:
            current += paragraph + "\n\n"
    if current.strip():
        chunks.append(current.strip())

    all_ok = True
    for i, chunk in enumerate(chunks):
        prefix = f"_(part {i+1}/{len(chunks)})_\n" if len(chunks) > 1 else ""
        if not _send_message(prefix + chunk):
            all_ok = False
        time.sleep(0.5)  # avoid rate limit
    return all_ok


# ============================================================
# FORMATTERS — DAILY SIGNALS
# ============================================================

def format_daily_signals(signals: List[Dict[str, Any]], macro_context: Dict[str, Any]) -> str:
    """Format full daily signal message."""
    now = datetime.now(config.JAKARTA_TZ)
    date_str = now.strftime("%d %b %Y")

    # Header
    lines = [
        f"{config.SIGNAL_HEADER_EMOJI} *DAILY SWING SIGNALS — {date_str}*",
        "═" * 30,
        "",
        _format_macro_block(macro_context),
        "",
    ]

    # Signals
    for i, sig in enumerate(signals, start=1):
        lines.append(_format_single_signal(sig, rank=i))
        lines.append("─" * 30)

    # Footer
    lines.extend([
        "",
        "_Auto-logged to Sheet_",
        f"_Next scan: tomorrow 7PM WIB_",
    ])

    return "\n".join(lines)


def _format_single_signal(sig: Dict[str, Any], rank: int = 1) -> str:
    """Format one signal entry."""
    ticker = sig.get("ticker", "?")
    conviction = int(sig.get("conviction", 0))

    # Emoji by tier
    if conviction >= config.CONVICTION_HIGH:
        emoji = config.HIGH_CONVICTION_EMOJI
        tier = "HIGH"
    elif conviction >= config.CONVICTION_STANDARD:
        emoji = config.STANDARD_CONVICTION_EMOJI
        tier = "STANDARD"
    else:
        emoji = config.WATCHLIST_EMOJI
        tier = "WATCHLIST"

    setup = sig.get("setup_type", "?")
    entry = float(sig.get("entry", 0))
    stop = float(sig.get("stop_loss", 0))
    tp1 = float(sig.get("tp1", 0))
    tp2 = float(sig.get("tp2", 0))
    rr = float(sig.get("rr_ratio", 0))
    rr_ext = float(sig.get("rr_extended", 0))
    pos_size = float(sig.get("position_size_usd", 0))
    risk_amt = float(sig.get("risk_amount_usd", 0))
    leverage = sig.get("max_leverage", "1x")
    hold_days = sig.get("max_holding_days", 7)
    shares = float(sig.get("shares", 0))

    # Compute % moves for context
    stop_pct = ((stop - entry) / entry) * 100
    tp1_pct = ((tp1 - entry) / entry) * 100
    tp2_pct = ((tp2 - entry) / entry) * 100

    thesis = sig.get("thesis", "")
    invalidations = sig.get("invalidation", [])
    invalidation_text = "\n".join([f"  • {inv}" for inv in invalidations[:3]])

    return f"""{emoji} *#{rank} — {ticker}* ({conviction}/100 {tier})
  *Setup:* {setup}

  *Entry:*    `${entry:.2f}`
  *Stop:*     `${stop:.2f}`  ({stop_pct:+.1f}%)
  *TP1:*      `${tp1:.2f}`  ({tp1_pct:+.1f}%, {rr:.2f}R)
  *TP2:*      `${tp2:.2f}`  ({tp2_pct:+.1f}%, {rr_ext:.2f}R)

  *Position:* ${pos_size:.2f} ({shares:.4f} shares)
  *Risk:*     ${risk_amt:.2f}
  *Leverage:* up to {leverage}
  *Hold:*     max {hold_days} days

  *Thesis:* {thesis}

  ⚠️ *Invalidation:*
{invalidation_text}"""


def _format_macro_block(macro: Dict[str, Any]) -> str:
    """Format the macro regime header."""
    if not macro:
        return f"{config.MACRO_ALERT_EMOJI} _Macro context unavailable_"

    spy_above = macro.get("spy_above_200ma", False)
    vix = macro.get("vix", 0)
    vix_panic = macro.get("vix_panic", False)
    spy_5d = macro.get("spy_5d_change_pct", 0)

    # Determine regime label
    if vix_panic and not spy_above:
        regime = "Risk-Off Bear"
    elif vix < 20 and spy_above:
        regime = "Risk-On Bull"
    elif vix < 25 and spy_above:
        regime = "Cautious Bull"
    else:
        regime = "Mixed / Choppy"

    return f"""{config.MACRO_ALERT_EMOJI} *MACRO REGIME:* {regime}
  VIX `{vix:.1f}` | SPY 5d: `{spy_5d:+.2f}%`
  Above 200MA: `{spy_above}`"""


# ============================================================
# NO-SIGNAL DAY
# ============================================================

def format_no_signal_day(reason: str, macro_context: Dict[str, Any]) -> str:
    """When no setups qualify — explicit skip message."""
    now = datetime.now(config.JAKARTA_TZ)
    date_str = now.strftime("%d %b %Y")

    return f"""{config.NO_SIGNAL_EMOJI} *NO SIGNAL — {date_str}*
═══════════════════════════════

{_format_macro_block(macro_context)}

*Verdict:* SKIP DAY
*Reason:* {reason}

_Better to sit out than force a trade.
Discipline > activity._

_Next scan: tomorrow 7PM WIB_"""


# ============================================================
# BLACK SWAN — TYPE A (CRASH WITH TRADE IDEAS)
# ============================================================

def format_black_swan_crash(
    event_summary: Dict[str, Any],
    trade_ideas: List[Dict[str, Any]]
) -> str:
    """Type A: immediate trade ideas during crash."""
    now = datetime.now(config.JAKARTA_TZ)
    time_str = now.strftime("%d %b %Y, %H:%M WIB")

    headlines = event_summary.get("headlines", [])
    headlines_text = "\n".join([f"  • {h[:100]}" for h in headlines[:3]]) or "  (event detected via market data)"

    market_moves = event_summary.get("market_moves", {})
    moves_text = "\n".join([
        f"  • {k}: {v}" for k, v in market_moves.items()
    ])

    lines = [
        f"{config.BLACK_SWAN_CRASH_EMOJI} *BLACK SWAN — RISK-OFF CRASH*",
        "═" * 30,
        f"🕘 {time_str}",
        "",
        f"*EVENT TRIGGERS:*",
        headlines_text,
        "",
        f"*MARKET REACTION:*",
        moves_text,
        "",
        f"🎯 *IMMEDIATE TRADE IDEAS* (long-only defensive):",
        "",
    ]

    for i, idea in enumerate(trade_ideas, start=1):
        lines.append(_format_single_signal(idea, rank=i))
        lines.append("─" * 30)

    lines.extend([
        "",
        "⚠️ *ACTIONS:*",
        "  • Review any open longs in Tech / Discretionary",
        "  • Consider tightening stops on existing positions",
        "  • Defensive longs above are not 'must do' — only if setup confirms",
        "",
        "_System will continue monitoring._",
    ])

    return "\n".join(lines)


# ============================================================
# BLACK SWAN — TYPE B (RALLY, ALERT ONLY)
# ============================================================

def format_black_swan_rally(event_summary: Dict[str, Any]) -> str:
    """Type B: rally — alert + sectors, no immediate ideas."""
    now = datetime.now(config.JAKARTA_TZ)
    time_str = now.strftime("%d %b %Y, %H:%M WIB")

    headlines = event_summary.get("headlines", [])
    headlines_text = "\n".join([f"  • {h[:100]}" for h in headlines[:3]]) or "  (event detected via market data)"

    market_moves = event_summary.get("market_moves", {})
    moves_text = "\n".join([f"  • {k}: {v}" for k, v in market_moves.items()])

    sectors_in = event_summary.get("preferred_sectors", [])
    sectors_out = event_summary.get("skip_sectors", [])

    return f"""{config.BLACK_SWAN_RALLY_EMOJI} *BLACK SWAN — RISK-ON RALLY*
═══════════════════════════════
🕘 {time_str}

*EVENT TRIGGERS:*
{headlines_text}

*MARKET REACTION:*
{moves_text}

🧠 *PM ASSESSMENT:*
Geopolitical/macro catalyst driving risk-on rotation.
*Don't chase the gap* — wait for tomorrow's scan.

📊 *SECTORS TO WATCH TOMORROW:*
  ✅ *Long bias:* {', '.join(sectors_in) if sectors_in else 'TBD'}
  ❌ *Avoid:* {', '.join(sectors_out) if sectors_out else 'TBD'}

⏳ *No immediate trade ideas.*
_Tomorrow's 7PM scan will reflect new regime._"""


# ============================================================
# BLACK SWAN — TYPE C (INFORMATIONAL)
# ============================================================

def format_black_swan_info(event_summary: Dict[str, Any]) -> str:
    """Type C: informational only."""
    now = datetime.now(config.JAKARTA_TZ)
    time_str = now.strftime("%d %b %Y, %H:%M WIB")

    headlines = event_summary.get("headlines", [])
    headlines_text = "\n".join([f"  • {h[:100]}" for h in headlines[:3]]) or "  (volatility detected)"

    market_moves = event_summary.get("market_moves", {})
    moves_text = "\n".join([f"  • {k}: {v}" for k, v in market_moves.items()])

    return f"""{config.BLACK_SWAN_INFO_EMOJI} *MARKET VOLATILITY ALERT*
═══════════════════════════════
🕘 {time_str}

*HEADLINES:*
{headlines_text}

*MARKET MOVES:*
{moves_text}

📊 *Status:* Informational only.
Direction unclear or magnitude moderate.
Tomorrow's 7PM scan will reassess.

_System continues monitoring._"""


# ============================================================
# PUBLIC SEND FUNCTIONS
# ============================================================

def send_daily_signals(signals: List[Dict[str, Any]], macro_context: Dict[str, Any]) -> bool:
    """Send the daily signal batch."""
    if not signals:
        return send_no_signal_day("No setups passed all validation tiers", macro_context)
    text = format_daily_signals(signals, macro_context)
    logger.info(f"Sending {len(signals)} signals to Telegram")
    return send_long_message(text)


def send_no_signal_day(reason: str, macro_context: Dict[str, Any]) -> bool:
    """Send skip-the-day message."""
    text = format_no_signal_day(reason, macro_context)
    logger.info(f"Sending no-signal day notification: {reason}")
    return send_long_message(text)


def send_black_swan_crash(event_summary: Dict[str, Any], trade_ideas: List[Dict[str, Any]]) -> bool:
    """Send Type A (crash with immediate ideas)."""
    text = format_black_swan_crash(event_summary, trade_ideas)
    logger.warning("Sending BLACK SWAN CRASH alert with trade ideas")
    return send_long_message(text)


def send_black_swan_rally(event_summary: Dict[str, Any]) -> bool:
    """Send Type B (rally alert)."""
    text = format_black_swan_rally(event_summary)
    logger.info("Sending BLACK SWAN RALLY alert (no immediate ideas)")
    return send_long_message(text)


def send_black_swan_info(event_summary: Dict[str, Any]) -> bool:
    """Send Type C (info alert)."""
    text = format_black_swan_info(event_summary)
    logger.info("Sending BLACK SWAN info alert")
    return send_long_message(text)


def send_test_message() -> bool:
    """Quick connectivity test."""
    text = (
        f"🤖 *SP500 Swing Agent — Connection Test*\n\n"
        f"Time: {datetime.now(config.JAKARTA_TZ).strftime('%d %b %Y %H:%M WIB')}\n"
        f"Status: ✅ Bot is alive and ready.\n\n"
        f"_If you see this, Telegram integration works._"
    )
    return _send_message(text)


if __name__ == "__main__":
    # Test connectivity (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("\n=== Sending test message ===")
    ok = send_test_message()
    print(f"Result: {'✓ sent' if ok else '✗ failed'}")
