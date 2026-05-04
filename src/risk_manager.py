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
                f"Max holding days {max_days} outside 1-{config.MAX_HOLDING_DAYS} swing range"
            )
    except (ValueError, TypeError):
        violations.append(f"Invalid max_holding_days: {max_days}")

    # ============================================================
    # RULE 8: TIER ASSESSMENT (TIER 1 must pass)
    # ============================================================
    tier_assess = signal.get("tier_assessment", {})
    if isinstance(tier_assess, dict):
        if tier_assess.get("tier_1_pass") is False:
            violations.append("TIER 1 (veto) failed — signal must be skipped")
        tier_2_conf = tier_assess.get("tier_2_confirmations", 0)
        if tier_2_conf < config.TIER2_MIN_CONFIRMATIONS and conviction >= config.CONVICTION_STANDARD:
            violations.append(
                f"TIER 2 only {tier_2_conf}/3 confirmations but conviction is {conviction} — inconsistent"
            )

    return len(violations) == 0, violations


# ============================================================
# PORTFOLIO-LEVEL VALIDATION
# ============================================================

def apply_portfolio_constraints(signals: List[Dict[str, Any]], ticker_metadata: Dict[str, Dict] = None) -> List[Dict[str, Any]]:
    """
    Apply diversification rules across the day's signals.
    - Max signals per sector
    - Max correlated signals (sub-industry level)
    - Max daily signals cap
    Returns filtered list (drops lowest-conviction overflow).
    """
    if ticker_metadata is None:
        ticker_metadata = {}

    # Sort by conviction descending — keep highest-conviction when culling
    signals_sorted = sorted(signals, key=lambda s: -int(s.get("conviction", 0)))

    sector_count = defaultdict(int)
    subindustry_count = defaultdict(int)
    accepted = []
    rejected = []

    for sig in signals_sorted:
        ticker = sig.get("ticker")
        meta = ticker_metadata.get(ticker, {})
        sector = meta.get("sector", "Unknown")
        subindustry = meta.get("sub_industry", "Unknown")

        # Check sector cap
        if sector_count[sector] >= config.MAX_SIGNALS_PER_SECTOR:
            rejected.append({
                **sig,
                "_rejection_reason": f"Sector cap reached ({sector}, max {config.MAX_SIGNALS_PER_SECTOR})"
            })
            continue
        # Check sub-industry cap (correlated names)
        if subindustry_count[subindustry] >= config.MAX_CORRELATED_SIGNALS:
            rejected.append({
                **sig,
                "_rejection_reason": f"Correlation cap reached ({subindustry})"
            })
            continue
        # Check daily total cap
        if len(accepted) >= config.MAX_DAILY_SIGNALS:
            rejected.append({
                **sig,
                "_rejection_reason": f"Daily signal cap reached ({config.MAX_DAILY_SIGNALS})"
            })
            continue

        accepted.append(sig)
        sector_count[sector] += 1
        subindustry_count[subindustry] += 1

    if rejected:
        logger.info(f"Portfolio constraints filtered out {len(rejected)} signals")
        for r in rejected:
            logger.info(f"  REJECTED {r.get('ticker')}: {r['_rejection_reason']}")

    return accepted


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def validate_and_filter(signals: List[Dict[str, Any]], ticker_metadata: Dict[str, Dict] = None) -> Dict[str, Any]:
    """
    Full validation pipeline:
    1. Validate each signal individually (drop violators)
    2. Apply portfolio constraints (sector/correlation/daily caps)
    3. Return final approved list + audit trail
    """
    individually_valid = []
    individually_rejected = []

    for sig in signals:
        if "error" in sig:
            individually_rejected.append({**sig, "_rejection_reason": f"upstream_error: {sig['error']}"})
            continue

        is_valid, violations = validate_signal(sig)
        if is_valid:
            individually_valid.append(sig)
            logger.info(f"✓ {sig.get('ticker')} passed validation (conviction {sig.get('conviction')})")
        else:
            individually_rejected.append({
                **sig,
                "_rejection_reason": "; ".join(violations)
            })
            logger.warning(f"✗ {sig.get('ticker')} REJECTED: {'; '.join(violations)}")

    # Apply portfolio constraints
    final = apply_portfolio_constraints(individually_valid, ticker_metadata)

    return {
        "approved": final,
        "individually_rejected": individually_rejected,
        "portfolio_rejected_count": len(individually_valid) - len(final),
        "stats": {
            "input_count": len(signals),
            "passed_individual": len(individually_valid),
            "final_count": len(final),
        }
    }


# ============================================================
# COMPUTE POSITION SIZE (utility for code, not LLM)
# ============================================================

def compute_position_size(entry: float, stop: float, max_risk_usd: float = None) -> Dict[str, float]:
    """
    Compute position size given entry/stop and max risk.
    Used for sanity-checking LLM's output.
    """
    if max_risk_usd is None:
        max_risk_usd = config.ACCOUNT_SIZE_USD * (config.MAX_RISK_PER_TRADE_PCT / 100)

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {"error": "Invalid stop (must be below entry for LONG)"}

    shares = max_risk_usd / risk_per_share
    position_usd = shares * entry

    return {
        "shares": round(shares, 4),
        "position_usd": round(position_usd, 2),
        "risk_usd": round(max_risk_usd, 2),
        "risk_per_share": round(risk_per_share, 2),
        "stop_distance_pct": round((risk_per_share / entry) * 100, 2),
    }


if __name__ == "__main__":
    # Test with a sample signal
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    test_signal = {
        "ticker": "NVDA",
        "conviction": 87,
        "tier_assessment": {
            "tier_1_pass": True,
            "tier_2_confirmations": 3,
            "tier_3_bonus": 2,
        },
        "direction": "LONG",
        "setup_type": "SMC OB bounce + BOS",
        "entry": 208.50,
        "stop_loss": 201.80,
        "tp1": 222.00,
        "tp2": 234.50,
        "rr_ratio": 2.01,
        "rr_extended": 3.88,
        "position_size_usd": 47.00,
        "shares": 0.225,
        "risk_amount_usd": 1.51,
        "max_leverage": "2x",
        "thesis": "Test thesis",
        "invalidation": ["Close below 201"],
        "max_holding_days": 5,
    }

    print("\n=== Single Signal Validation ===")
    is_valid, violations = validate_signal(test_signal)
    print(f"Valid: {is_valid}")
    if violations:
        for v in violations:
            print(f"  - {v}")

    print("\n=== Position Size Calculator ===")
    pos = compute_position_size(208.50, 201.80)
    print(pos)

    print("\n=== Full Pipeline Test ===")
    bad_signal = {**test_signal, "ticker": "BAD", "stop_loss": 215.00}  # stop ABOVE entry
    result = validate_and_filter(
        [test_signal, bad_signal],
        ticker_metadata={"NVDA": {"sector": "Technology", "sub_industry": "Semiconductors"}}
    )
    print(f"Approved: {len(result['approved'])}")
    print(f"Rejected: {len(result['individually_rejected'])}")
    for r in result['individually_rejected']:
        print(f"  {r['ticker']}: {r['_rejection_reason']}")
