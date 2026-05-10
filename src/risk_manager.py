"""
risk_manager.py — Final validation gate before signals are released.

STAGE 2 REVISION (May 2026):
============================
CHANGES FROM PHASE 1:
  - Removed TIER 2 confirmations check (TIER 2 dropped from Opus schema)
  - Removed `max_leverage` logic (field removed from Opus schema)
  - Removed 2x leverage conviction gate
  - RULE 8 simplified: only validates `tier_1_pass` boolean
  - Added `tier_1_fail_reason` to rejection log for debug clarity
  - Default `tier_1_pass = True` for backwards compat

RULE 3 DESIGN (RR):
  - 2:1 RR is a MINIMUM GUIDELINE, not a hard gate
  - Signals below 2:1 are FLAGGED with _rr_warning, NOT rejected
  - Only hard rejection: Opus over-claims RR by >0.5 (hallucination)
  - Rationale: a strong setup at 1.8:1 is better than no signal at all
    User makes final entry decision with full context

RULE 8 DESIGN (TIER 1):
  - If tier_1_pass=False due to RR only → soft warning, not rejection
  - Other TIER 1 failures (macro, weekly downtrend) → hard rejection
  - NOTE: analyst.py Opus prompt should also remove RR from TIER 1 veto
    conditions (pending revision)

CATCHES:
  - LLM hallucinations (impossible prices, wrong direction, math errors)
  - Risk rule violations (>3% risk)
  - Position sizing math errors
  - Sector concentration limits
  - Conviction tier violations
  - Long-only enforcement
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from src import config

logger = logging.getLogger(__name__)

# Keywords used to detect if TIER 1 failed purely due to RR
_RR_KEYWORDS = ["rr", "reward", "r:r", "risk:reward", "risk/reward", "2:1", "ratio"]


# ============================================================
# SINGLE-SIGNAL VALIDATION
# ============================================================

def validate_signal(signal: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a single signal against all hard rules.
    Returns (is_valid, list_of_violations).

    Note: Some checks add warnings to signal dict (_rr_warning, _tier1_rr_warning)
    without rejecting. These are surfaced in Telegram messages for user awareness.
    """
    violations = []

    # === Required fields ===
    required_fields = [
        "ticker", "direction", "conviction",
        "entry", "stop_loss", "tp1", "tp2",
        "rr_ratio", "position_size_usd", "risk_amount_usd",
    ]
    for field in required_fields:
        if field not in signal or signal.get(field) is None:
            violations.append(f"Missing required field: {field}")

    if violations:
        return False, violations

    # === Type sanity (LLM sometimes returns strings) ===
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
        violations.append(
            f"LONG stop ({stop:.2f}) must be BELOW entry ({entry:.2f})"
        )
    if tp1 <= entry:
        violations.append(
            f"TP1 ({tp1:.2f}) must be ABOVE entry ({entry:.2f}) for LONG"
        )
    if tp2 <= tp1:
        violations.append(
            f"TP2 ({tp2:.2f}) must be ABOVE TP1 ({tp1:.2f})"
        )

    # Stop distance sanity
    if entry > 0 and stop > 0 and stop < entry:
        stop_distance_pct = ((entry - stop) / entry) * 100
        if stop_distance_pct < 0.3:
            violations.append(
                f"Stop too tight ({stop_distance_pct:.2f}% from entry) — likely LLM bug"
            )
        if stop_distance_pct > 15.0:
            violations.append(
                f"Stop too wide ({stop_distance_pct:.2f}% from entry) — exceeds swing trade norms"
            )

    # ============================================================
    # RULE 3: R:R CHECK
    # Design: 2:1 is a minimum guideline, NOT a hard gate.
    # - Below 2:1 → WARNING flag on signal, user decides
    # - Opus over-claims by >0.5 → hard rejection (hallucination)
    # - Actual better than claimed → pass silently
    # ============================================================
    risk_per_share = entry - stop
    reward_per_share_tp1 = tp1 - entry

    if risk_per_share <= 0:
        violations.append("Risk per share is zero or negative")
    else:
        actual_rr = reward_per_share_tp1 / risk_per_share

        # Hard reject: Opus significantly over-claims RR (hallucination)
        if actual_rr < rr - 0.5:
            violations.append(
                f"RR over-claimed: Opus said {rr:.2f}, actual {actual_rr:.2f} — "
                f"math error or hallucination"
            )

        # Soft warning: below 2:1 guideline (does NOT reject)
        elif actual_rr < config.MIN_RR_RATIO:
            warning = (
                f"⚠️ RR {actual_rr:.2f} below {config.MIN_RR_RATIO}:1 guideline — "
                f"review setup quality before entry"
            )
            signal["_rr_warning"] = warning
            logger.warning(
                f"  {signal.get('ticker')} RR soft warning: "
                f"actual {actual_rr:.2f} < {config.MIN_RR_RATIO} minimum"
            )

        # Good: actual better than or equal to claimed
        else:
            logger.info(
                f"  {signal.get('ticker')} RR OK: "
                f"actual {actual_rr:.2f} (claimed {rr:.2f})"
            )

    # ============================================================
    # RULE 4: RISK AMOUNT WITHIN BUDGET
    # ============================================================
    max_risk_usd = config.ACCOUNT_SIZE_USD * (config.MAX_RISK_PER_TRADE_PCT / 100)
    if risk_amt > max_risk_usd + 0.05:
        violations.append(
            f"Risk ${risk_amt:.2f} exceeds max ${max_risk_usd:.2f} "
            f"({config.MAX_RISK_PER_TRADE_PCT}% of ${config.ACCOUNT_SIZE_USD})"
        )

    # Recompute risk to verify Opus math
    if pos_size > 0 and entry > 0:
        actual_risk = pos_size * (risk_per_share / entry)
        if abs(actual_risk - risk_amt) > 0.20:
            violations.append(
                f"Risk amount mismatch: claimed ${risk_amt:.2f}, "
                f"computed ${actual_risk:.2f}"
            )

    # ============================================================
    # RULE 5: POSITION SIZE WITHIN ACCOUNT (1x, no leverage)
    # ============================================================
    max_position = config.ACCOUNT_SIZE_USD * 1.0
    if pos_size > max_position + 0.50:
        violations.append(
            f"Position ${pos_size:.2f} exceeds account ${config.ACCOUNT_SIZE_USD}"
        )

    # ============================================================
    # RULE 6: CONVICTION TIER ENFORCEMENT
    # ============================================================
    if conviction < 0 or conviction > 100:
        violations.append(f"Conviction {conviction} outside 0-100 range")
    if conviction < config.CONVICTION_STANDARD:
        violations.append(
            f"Conviction {conviction} below threshold {config.CONVICTION_STANDARD}"
        )

    # ============================================================
    # RULE 7: HOLDING PERIOD
    # ============================================================
    max_days = signal.get("max_holding_days", 7)
    try:
        max_days = int(max_days)
        if max_days < 1 or max_days > config.MAX_HOLDING_DAYS:
            violations.append(
                f"Holding days {max_days} outside 1-{config.MAX_HOLDING_DAYS} range"
            )
    except (ValueError, TypeError):
        violations.append(f"Invalid max_holding_days: {max_days}")

    # ============================================================
    # RULE 8: TIER 1 VETO
    # Design: if tier_1_pass=False due to RR only → soft warning
    # Other TIER 1 failures (macro, trend) → hard rejection
    # NOTE: pending analyst.py revision to remove RR from TIER 1 conditions
    # ============================================================
    tier_1_pass = signal.get("tier_1_pass", True)
    if tier_1_pass is False:
        fail_reason = signal.get("tier_1_fail_reason", "unspecified")
        fail_lower = fail_reason.lower()

        # Check if failure is RR-related only
        rr_only_fail = any(kw in fail_lower for kw in _RR_KEYWORDS)
        macro_fail = any(
            kw in fail_lower
            for kw in ["vix", "spy", "200ma", "downtrend", "macro", "regime"]
        )

        if rr_only_fail and not macro_fail:
            # Soft: RR-only veto → warning, not rejection
            warning = (
                f"⚠️ TIER 1 soft fail (RR): {fail_reason} — "
                f"signal passed but review R:R carefully"
            )
            signal["_tier1_rr_warning"] = warning
            logger.warning(
                f"  {signal.get('ticker')} TIER 1 RR-only soft fail: {fail_reason}"
            )
        else:
            # Hard: macro/trend veto → reject
            violations.append(f"TIER 1 veto (macro/trend): {fail_reason}")

    return len(violations) == 0, violations


# ============================================================
# PORTFOLIO-LEVEL VALIDATION
# ============================================================

def apply_portfolio_constraints(
    signals: List[Dict[str, Any]],
    ticker_metadata: Dict[str, Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Diversification rules across the day's signals.
    Drops lowest conviction when caps hit.
    """
    if ticker_metadata is None:
        ticker_metadata = {}

    signals_sorted = sorted(
        signals, key=lambda s: -int(s.get("conviction", 0))
    )

    sector_count = defaultdict(int)
    subindustry_count = defaultdict(int)
    accepted = []
    rejected = []

    for sig in signals_sorted:
        ticker = sig.get("ticker")
        meta = ticker_metadata.get(ticker, {})
        sector = meta.get("sector", "Unknown")
        subindustry = meta.get("sub_industry", "Unknown")

        if sector_count[sector] >= config.MAX_SIGNALS_PER_SECTOR:
            rejected.append({
                **sig,
                "_rejection_reason": (
                    f"Sector cap ({sector}, max {config.MAX_SIGNALS_PER_SECTOR})"
                ),
            })
            continue

        if subindustry_count[subindustry] >= config.MAX_CORRELATED_SIGNALS:
            rejected.append({
                **sig,
                "_rejection_reason": f"Correlation cap ({subindustry})",
            })
            continue

        if len(accepted) >= config.MAX_DAILY_SIGNALS:
            rejected.append({
                **sig,
                "_rejection_reason": (
                    f"Daily cap reached ({config.MAX_DAILY_SIGNALS})"
                ),
            })
            continue

        accepted.append(sig)
        sector_count[sector] += 1
        subindustry_count[subindustry] += 1

    if rejected:
        logger.info(f"Portfolio constraints filtered {len(rejected)} signals")
        for r in rejected:
            logger.info(f"  REMOVED {r.get('ticker')}: {r['_rejection_reason']}")

    return accepted


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def validate_and_filter(
    signals: List[Dict[str, Any]],
    ticker_metadata: Dict[str, Dict] = None,
) -> Dict[str, Any]:
    """
    Full validation pipeline:
      1. Validate each signal individually
      2. Apply portfolio diversification constraints
      3. Return approved list + audit trail
    """
    individually_valid = []
    individually_rejected = []

    for sig in signals:
        if "error" in sig:
            individually_rejected.append({
                **sig,
                "_rejection_reason": f"upstream_error: {sig['error']}",
            })
            continue

        is_valid, violations = validate_signal(sig)
        if is_valid:
            individually_valid.append(sig)
            rr_warn = sig.get("_rr_warning", "")
            tier_warn = sig.get("_tier1_rr_warning", "")
            logger.info(
                f"✓ {sig.get('ticker')} passed "
                f"(conviction {sig.get('conviction')}, "
                f"RR {sig.get('rr_ratio')})"
                + (f" | {rr_warn}" if rr_warn else "")
                + (f" | {tier_warn}" if tier_warn else "")
            )
        else:
            individually_rejected.append({
                **sig,
                "_rejection_reason": "; ".join(violations),
            })
            logger.warning(
                f"✗ {sig.get('ticker')} REJECTED: {'; '.join(violations)}"
            )

    final = apply_portfolio_constraints(individually_valid, ticker_metadata)

    return {
        "approved": final,
        "individually_rejected": individually_rejected,
        "portfolio_rejected_count": len(individually_valid) - len(final),
        "stats": {
            "input_count": len(signals),
            "passed_individual": len(individually_valid),
            "final_count": len(final),
        },
    }


# ============================================================
# UTILITY: POSITION SIZE COMPUTATION
# ============================================================

def compute_position_size(
    entry: float,
    stop: float,
    max_risk_usd: float = None,
) -> Dict[str, float]:
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


# ============================================================
# SMOKE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Signal 1: Clean 2:1 RR — should pass cleanly
    clean_signal = {
        "ticker": "NVDA",
        "conviction": 78,
        "tier_1_pass": True,
        "tier_1_fail_reason": None,
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
        "thesis": "Test thesis",
        "invalidation": ["Close below 137"],
        "max_holding_days": 5,
        "confluence_display": "3/3 (MACD✓ EMA9✓ EMA21✓)",
    }

    # Signal 2: Strong setup but 1.8:1 RR — should PASS with warning
    low_rr_signal = {
        **clean_signal,
        "ticker": "AMD",
        "entry": 142.50,
        "stop_loss": 137.80,
        "tp1": 150.90,   # RR = 1.8:1
        "tp2": 158.00,
        "rr_ratio": 1.8,
        "conviction": 82,
    }

    # Signal 3: Opus over-claims 5:1 but actual is 1.2:1 — should REJECT
    hallucination_signal = {
        **clean_signal,
        "ticker": "BAD",
        "entry": 142.50,
        "stop_loss": 137.80,
        "tp1": 149.00,   # actual RR = 1.38
        "rr_ratio": 5.0, # LLM hallucinated
    }

    # Signal 4: TIER 1 fail due to RR only — should PASS with warning
    tier_rr_signal = {
        **clean_signal,
        "ticker": "SOFT",
        "tier_1_pass": False,
        "tier_1_fail_reason": "RR 1.8:1 below minimum 2:1 requirement",
        "rr_ratio": 1.8,
        "tp1": 150.90,
        "conviction": 75,
    }

    # Signal 5: TIER 1 fail due to macro — should REJECT
    macro_fail_signal = {
        **clean_signal,
        "ticker": "MACRO",
        "tier_1_pass": False,
        "tier_1_fail_reason": "VIX > 25 AND SPY below 200MA — risk-off regime",
        "conviction": 0,
    }

    print("\n=== Signal 1: NVDA (clean 2:1 RR) ===")
    v, viol = validate_signal(clean_signal)
    print(f"Valid: {v} | Warnings: {clean_signal.get('_rr_warning', 'none')}")

    print("\n=== Signal 2: AMD (1.8:1 RR — below guideline) ===")
    v, viol = validate_signal(low_rr_signal)
    print(f"Valid: {v} | RR Warning: {low_rr_signal.get('_rr_warning', 'none')}")

    print("\n=== Signal 3: BAD (RR hallucination) ===")
    v, viol = validate_signal(hallucination_signal)
    print(f"Valid: {v} | Violations: {viol}")

    print("\n=== Signal 4: SOFT (TIER 1 RR-only fail) ===")
    v, viol = validate_signal(tier_rr_signal)
    print(f"Valid: {v} | Tier Warning: {tier_rr_signal.get('_tier1_rr_warning', 'none')}")

    print("\n=== Signal 5: MACRO (TIER 1 macro fail) ===")
    v, viol = validate_signal(macro_fail_signal)
    print(f"Valid: {v} | Violations: {viol}")

    print("\n=== Full Pipeline ===")
    result = validate_and_filter(
        [clean_signal, low_rr_signal, hallucination_signal,
         tier_rr_signal, macro_fail_signal],
        ticker_metadata={
            "NVDA": {"sector": "Technology", "sub_industry": "Semiconductors"},
            "AMD": {"sector": "Technology", "sub_industry": "Semiconductors"},
        }
    )
    print(f"Approved: {len(result['approved'])} — "
          f"{[s['ticker'] for s in result['approved']]}")
    print(f"Rejected: {len(result['individually_rejected'])} — "
          f"{[s['ticker'] for s in result['individually_rejected']]}")
