"""
Risk Manager — final validation gate before signals are released.

Catches:
- LLM hallucinations (impossible prices, negative stops, wrong direction)
- Risk rule violations (>3% risk, <2:1 RR)
- Position sizing math errors
- Sector concentration limits
- Conviction tier violations
- Long-only enforcement
"""
import logging
from typing import Dict, List, Any, Tuple
from collections import defaultdict

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# SINGLE-SIGNAL VALIDATION
# ============================================================

def validate_signal(signal: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a single signal against all hard rules.
    Returns (is_valid, list_of_violations).
    """
    violations = []

    # Required fields check
    required_fields = [
        "ticker", "direction", "conviction", "entry", "stop_loss",
        "tp1", "tp2", "rr_ratio", "position_size_usd", "risk_amount_usd"
    ]
    for field in required_fields:
        if field not in signal or signal.get(field) is None:
            violations.append(f"Missing required field: {field}")

    if violations:
        return False, violations

    # Type sanity (LLM sometimes sends strings)
    try:
        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        tp1 = float(signal["tp1"])
        tp2 = float(signal["tp2"])
        rr = float(signal["rr_ratio"])
        pos_size = float(signal["position_size_usd"])
        risk_amt = float(signal["risk_amount_usd"])
        conviction = int(signal["conviction"])
    except (ValueError, TypeError) as e:
        return False, [f"Type conversion error: {e}"]

    # ============================================================
    # RULE 1: LONG ONLY ENFORCEMENT
    # ============================================================
    direction = signal.get("direction", "").upper()
    if direction != "LONG":
        violations.append(f"Direction must be LONG, got: {direction}")

    # ============================================================
    # RULE 2: PRICE LOGIC (LONG-specific)
    # ============================================================
    if entry <= 0:
        violations.append(f"Invalid entry price: {entry}")
    if stop <= 0:
        violations.append(f"Invalid stop price: {stop}")
    if stop >= entry:
        violations.append(f"LONG stop ({stop}) must be BELOW entry ({entry})")
    if tp1 <= entry:
        violations.append(f"TP1 ({tp1}) must be ABOVE entry ({entry}) for LONG")
    if tp2 <= tp1:
        violations.append(f"TP2 ({tp2}) must be ABOVE TP1 ({tp1})")

    # Sanity: stop too close (<0.3% from entry) = likely LLM bug
    stop_distance_pct = ((entry - stop) / entry) * 100
    if stop_distance_pct < 0.3:
        violations.append(f"Stop too tight ({stop_distance_pct:.2f}% from entry) — likely LLM bug")
    # Stop too far (>15% from entry) = probably wrong instrument or volatility issue
    if stop_distance_pct > 15.0:
        violations.append(f"Stop too wide ({stop_distance_pct:.2f}% from entry) — exceeds swing trade norms")

    # ============================================================
    # RULE 3: RISK:REWARD MINIMUM
    # ============================================================
    # Recompute RR from entry/stop/tp1 to verify LLM's claim
    risk_per_share = entry - stop
    reward_per_share_tp1 = tp1 - entry
    if risk_per_share <= 0:
        violations.append("Risk per share is zero or negative")
    else:
        actual_rr = reward_per_share_tp1 / risk_per_share
        if abs(actual_rr - rr) > 0.15:
            violations.append(
                f"RR mismatch: claimed {rr:.2f}, computed {actual_rr:.2f}"
            )
        if actual_rr < config.MIN_RR_RATIO:
            violations.append(
                f"RR {actual_rr:.2f} below minimum {config.MIN_RR_RATIO}"
            )

    # ============================================================
    # RULE 4: RISK AMOUNT WITHIN BUDGET
    # ============================================================
    max_risk_usd = config.ACCOUNT_SIZE_USD * (config.MAX_RISK_PER_TRADE_PCT / 100)
    if risk_amt > max_risk_usd + 0.05:  # tiny tolerance for rounding
        violations.append(
            f"Risk ${risk_amt:.2f} exceeds max ${max_risk_usd:.2f} ({config.MAX_RISK_PER_TRADE_PCT}% of ${config.ACCOUNT_SIZE_USD})"
        )

    # Recompute risk to verify
    if pos_size > 0 and entry > 0:
        actual_risk = pos_size * (risk_per_share / entry)
        if abs(actual_risk - risk_amt) > 0.20:
            violations.append(
                f"Risk amount mismatch: claimed ${risk_amt:.2f}, computed ${actual_risk:.2f}"
            )

    # ============================================================
    # RULE 5: POSITION SIZE WITHIN ACCOUNT
    # ============================================================
    # For 1x leverage, position can't exceed account size.
    # For 2x, can be up to 2x.
    max_lev_str = signal.get("max_leverage", "1x").lower().replace("x", "")
    try:
        max_lev = float(max_lev_str)
    except ValueError:
        max_lev = 1.0
    if max_lev > 2.0:
        violations.append(f"Leverage {max_lev}x exceeds maximum 2x")

    max_position = config.ACCOUNT_SIZE_USD * max_lev
    if pos_size > max_position + 0.50:
        violations.append(
            f"Position ${pos_size:.2f} exceeds max ${max_position:.2f} at {max_lev}x leverage"
        )

    # ============================================================
    # RULE 6: CONVICTION TIER ENFORCEMENT
    # ============================================================
    if conviction < 0 or conviction > 100:
        violations.append(f"Conviction {conviction} outside 0-100 range")
    if conviction < config.CONVICTION_STANDARD:
        violations.append(
            f"Conviction {conviction} below standard threshold {config.CONVICTION_STANDARD} — should not enter"
        )
    # Leverage gate: 2x only allowed if conviction >= 80
    if max_lev > 1.0 and conviction < config.CONVICTION_HIGH:
        violations.append(
            f"Leverage {max_lev}x requires conviction >= {config.CONVICTION_HIGH}, got {conviction}"
        )

    # ============================================================
    # RULE 7: HOLDING PERIOD
    # ============================================================
    max_days = signal.get("max_holding_days", 7)
    try:
        max_days = int(max_days)
        if max_days < 1 or max_days > config.MAX_HOLDING_DAYS:
            violations.append(
                f"Max holding days {max_days} outside 1-{config.MAX_HOLDING_DAYS}
