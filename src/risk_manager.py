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

RULE 4 DESIGN (SIZING MISMATCH) — CHANGED June 2026:
  - Downgraded from hard reject to soft warning (_sizing_warning).
  - Position sizing is explicitly out of scope for this system (signal
    quality + R:R only — sizing is managed manually by the user, per
    locked design principle). Rejecting valid high-conviction signals
    (URI, JPM, CVS — confirmed across runs #48/#50) over a fractional-
    share rounding mismatch in Opus's own math contradicted that
    principle. The mismatch is still computed and surfaced for
    visibility — it just no longer blocks the signal.
  - The budget check (claimed risk vs MAX_RISK_PER_TRADE_PCT) remains a
    HARD gate — that's a different concern (claim vs account limit, not
    Opus's internal math consistency) and has no evidence of being broken.

RULE 6 DESIGN (CONVICTION) — CHANGED June 2026:
  - Downgraded from hard reject to soft warning (_conviction_warning).
  - Conviction score is proven non-predictive in this system's own data
    (Phase 2.2 Finding 1: loser avg conviction 75.1 vs winner avg 72.6).
    A numerical floor with no statistical basis was rejecting signals
    (e.g. INTC at 58) with no evidence they were actually worse than
    signals that passed. Conviction is now FYI-only for the user,
    consistent with the locked principle "conviction score is display-
    only, never feeds gating logic" (Phase 2.5 §5).
  - Range sanity (0-100) remains a hard gate — that's a data-integrity
    check, not a quality gate, and is unaffected by this change.

RULE 8 DESIGN (TIER 1):
  - If tier_1_pass=False due to RR only → soft warning, not rejection
  - Other TIER 1 failures (macro, weekly downtrend) → hard rejection
  - RESOLVED (verified Aug 2026, Phase 2.6 code audit): the analyst.py Opus
    prompt no longer lists RR as a TIER 1 veto condition. TIER 1 now has
    exactly two conditions — macro regime (VIX < 25 OR SPY > 200MA) and
    weekly trend (fails only on "bearish"). RR was demoted to a 10% scoring
    component. That change actually shipped in Phase 2.2 (see analyst.py
    module docstring: "TIER 1 veto (macro + weekly trend only — RR
    removed)"); only this "pending revision" note was left behind and was
    still describing work that had already been done.

CATCHES:
  - LLM hallucinations (impossible prices, wrong direction, math errors)
  - Risk rule violations (>3% risk)
  - Sector concentration limits
  - Long-only enforcement
  (Position sizing math errors and conviction tier are now soft warnings
   only — see RULE 4 / RULE 6 above.)
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

def apply_position_sizing(signal: dict, equity_basis: float) -> dict:
    """
    PHASE 2.6 — deterministic position sizing. Call this BEFORE validate_signal().

    This is the fix for the recurring "sizing mismatch" warnings (ADM, URI,
    JPM, CVS). Those were never an LLM arithmetic problem. Two things caused
    them:

      1. The Opus prompt gave exactly ONE sizing formula,
             "Position size = Risk / (Entry - Stop)"
         which yields SHARES, not dollars — while asking the model to report
         position_size_usd, shares AND risk_amount_usd as three independent
         numbers. Three numbers, one formula, no constraint tying them
         together: they were free to disagree.

      2. At 3% risk the required notional is risk / stop_pct. Stop is
         1.5 x ATR and F5 floors ATR at 1.5%, so stop could be as tight as
         2.25% -> notional up to 133% of a $100 account, which RULE 5
         rejects. RULE 4 and RULE 5 were mutually unsatisfiable for any
         signal with ATR between 1.5% and 2.0%. There was no correct answer
         to give.

    Dropping risk to 2% removes (2): stop >= 2.25% > 2.00% always, so the
    notional cap can never bind. This function removes (1): shares is the
    single source of truth and the other two are DERIVED from it, so they
    cannot disagree by construction.

        shares   = risk_usd / (entry - stop)
        notional = shares * entry
        risk_usd = shares * (entry - stop)      <- reported, not claimed

    equity_basis is the running balance from main.py's intra-run waterfall:
    signal 1 sizes off full equity, signal 2 off equity minus signal 1's
    risk, and so on — so a batch of same-day signals cannot collectively
    commit more than the risk budget allows.

    Returns the signal (mutated in place) with _sizing_applied=True. Any
    signal reaching validate_signal() WITHOUT that flag was not sized by
    this function, which is itself worth knowing.
    """
    try:
        entry = float(signal.get("entry", 0) or 0)
        stop = float(signal.get("stop_loss", 0) or 0)
    except (TypeError, ValueError):
        entry = stop = 0.0

    risk_per_share = entry - stop
    if entry <= 0 or risk_per_share <= 0:
        # Malformed geometry — leave untouched and let RULE 1/2 reject it
        # with the specific error rather than masking it here.
        signal["_sizing_applied"] = False
        signal["_equity_basis"] = float(equity_basis)
        return signal

    risk_budget = float(equity_basis) * (config.MAX_RISK_PER_TRADE_PCT / 100.0)

    # Notional cap at 1x equity (no leverage). With risk at 2% and F5's ATR
    # floor this is mathematically unreachable, so it exists only as a
    # guard against a future config change silently re-introducing the
    # RULE 4 / RULE 5 contradiction described above.
    shares = risk_budget / risk_per_share
    max_shares_by_capital = float(equity_basis) / entry
    risk_capped = shares > max_shares_by_capital
    if risk_capped:
        shares = max_shares_by_capital
        logger.warning(
            f"  {signal.get('ticker')} notional-capped: stop {risk_per_share/entry*100:.2f}% "
            f"is tighter than risk {config.MAX_RISK_PER_TRADE_PCT}% — "
            f"risk reduced below budget. Check ATR floor vs risk %."
        )

    signal["shares"] = round(shares, 6)
    signal["position_size_usd"] = round(shares * entry, 2)
    signal["risk_amount_usd"] = round(shares * risk_per_share, 2)
    signal["_equity_basis"] = round(float(equity_basis), 4)
    signal["_risk_capped"] = bool(risk_capped)
    signal["_sizing_applied"] = True
    return signal


def validate_signal(signal: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate a single signal against all hard rules.
    Returns (is_valid, list_of_violations).

    Note: Some checks add warnings to signal dict (_rr_warning,
    _tier1_rr_warning, _sizing_warning, _conviction_warning) without
    rejecting. These are surfaced in Telegram messages for user awareness.
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
        # Tolerance of 0.01 avoids false warning on exactly 2.0 (floating point edge case)
        if actual_rr < config.MIN_RR_RATIO - 0.01:
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
    # Budget-vs-claim check remains a HARD gate (unchanged).
    # ============================================================
    # PHASE 2.6: the budget is now a % of the CURRENT equity basis passed in
    # by main.py's waterfall, not a % of a static $100 constant.
    # _equity_basis falls back to ACCOUNT_START_USD only if sizing was never
    # applied (e.g. a unit test calling validate_signal directly).
    equity_basis = float(signal.get("_equity_basis", config.ACCOUNT_START_USD))
    max_risk_usd = equity_basis * (config.MAX_RISK_PER_TRADE_PCT / 100)
    if risk_amt > max_risk_usd + 0.05:
        violations.append(
            f"Risk ${risk_amt:.2f} exceeds max ${max_risk_usd:.2f} "
            f"({config.MAX_RISK_PER_TRADE_PCT}% of ${equity_basis:.2f})"
        )

    # Recompute risk to verify sizing math.
    #
    # PHASE 2.6: RESTORED TO A HARD GATE.
    # It was downgraded to a soft warning in June 2026 because it kept firing
    # on valid setups. The real cause was never LLM arithmetic — it was that
    # RULE 4 (risk = X) and RULE 5 (position <= account) were mutually
    # unsatisfiable at 3% risk whenever the stop was tighter than 3%, which
    # F5's 1.5% ATR floor allowed. The model had no consistent answer to give.
    # Dropping risk to 2% removes that contradiction (see config docstring),
    # and apply_position_sizing() below now DERIVES risk from shares rather
    # than letting two independent numbers be reported, so a mismatch is
    # arithmetically impossible on the normal path. If one appears anyway,
    # something upstream is genuinely broken and must not reach real capital.
    #
    # The June 2026 downgrade note explicitly said to re-litigate this
    # "if/when position sizing becomes automated or capital is materially
    # increased". Both are now true.
    if pos_size > 0 and entry > 0:
        actual_risk = pos_size * (risk_per_share / entry)
        if abs(actual_risk - risk_amt) > 0.20:
            violations.append(
                f"Sizing mismatch: reported risk ${risk_amt:.2f} vs "
                f"computed ${actual_risk:.2f} from position ${pos_size:.2f}"
            )
            logger.error(
                f"  {signal.get('ticker')} SIZING MISMATCH (hard reject): "
                f"reported ${risk_amt:.2f}, computed ${actual_risk:.2f}"
            )

    # ============================================================
    # RULE 5: POSITION SIZE WITHIN ACCOUNT (1x, no leverage)
    # ============================================================
    # PHASE 2.6: compared against the CURRENT equity basis, not a static
    # $100. At 2% risk this can never bind (stop >= 2.25% via F5's ATR floor
    # implies notional <= 88.9% of equity), so it now acts purely as a
    # tripwire: if it ever fires, an upstream assumption has broken.
    max_position = equity_basis * 1.0
    if pos_size > max_position + 0.50:
        violations.append(
            f"Position ${pos_size:.2f} exceeds equity ${equity_basis:.2f}"
        )

    # ============================================================
    # RULE 6: CONVICTION RANGE SANITY + SOFT THRESHOLD
    # Range sanity (0-100) remains a HARD gate (data integrity).
    # CHANGED (June 2026): the CONVICTION_STANDARD threshold is downgraded
    # from hard reject to soft warning. See module docstring "RULE 6 DESIGN"
    # for rationale (conviction proven non-predictive in this system's data).
    # ============================================================
    if conviction < 0 or conviction > 100:
        violations.append(f"Conviction {conviction} outside 0-100 range")
    elif conviction < config.CONVICTION_STANDARD:
        warning = (
            f"ℹ️ Conviction {conviction} below {config.CONVICTION_STANDARD} "
            f"reference threshold — FYI only, not a quality signal (see Phase 2.2 "
            f"Finding 1: conviction is non-predictive in this system's data)"
        )
        signal["_conviction_warning"] = warning
        logger.warning(
            f"  {signal.get('ticker')} conviction below reference threshold "
            f"(non-blocking): {conviction} < {config.CONVICTION_STANDARD}"
        )

    # ============================================================
    # RULE 7: HOLDING PERIOD
    # ============================================================
    # PHASE 2.6: default was a hard-coded 7 while config said 7 and the
    # ACTUAL operating rule was 10 TRADING days — and TIME_TP / TIME_SL are
    # defined at hold >= 10. Config, prompt and this gate now all say 10.
    max_days = signal.get("max_holding_days", config.MAX_HOLDING_DAYS)
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
            sizing_warn = sig.get("_sizing_warning", "")
            conviction_warn = sig.get("_conviction_warning", "")
            logger.info(
                f"✓ {sig.get('ticker')} passed "
                f"(conviction {sig.get('conviction')}, "
                f"RR {sig.get('rr_ratio')})"
                + (f" | {rr_warn}" if rr_warn else "")
                + (f" | {tier_warn}" if tier_warn else "")
                + (f" | {sizing_warn}" if sizing_warn else "")
                + (f" | {conviction_warn}" if conviction_warn else "")
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

    # Signal 6: Sizing mismatch (like URI/JPM/CVS) — should PASS with warning
    sizing_mismatch_signal = {
        **clean_signal,
        "ticker": "URI",
        "entry": 254.10,
        "stop_loss": 248.50,
        "tp1": 265.30,
        "tp2": 276.50,
        "rr_ratio": 2.00,
        "position_size_usd": 91.00,
        "risk_amount_usd": 3.00,   # claimed — but computed will be ~0.13
        "conviction": 71,
    }

    # Signal 7: Low conviction (like INTC) — should PASS with warning
    low_conviction_signal = {
        **clean_signal,
        "ticker": "INTC",
        "conviction": 58,
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

    print("\n=== Signal 6: URI (sizing mismatch — should now PASS) ===")
    v, viol = validate_signal(sizing_mismatch_signal)
    print(f"Valid: {v} | Sizing Warning: {sizing_mismatch_signal.get('_sizing_warning', 'none')}")

    print("\n=== Signal 7: INTC (low conviction — should now PASS) ===")
    v, viol = validate_signal(low_conviction_signal)
    print(f"Valid: {v} | Conviction Warning: {low_conviction_signal.get('_conviction_warning', 'none')}")

    print("\n=== Full Pipeline ===")
    result = validate_and_filter(
        [clean_signal, low_rr_signal, hallucination_signal,
         tier_rr_signal, macro_fail_signal, sizing_mismatch_signal,
         low_conviction_signal],
        ticker_metadata={
            "NVDA": {"sector": "Technology", "sub_industry": "Semiconductors"},
            "AMD": {"sector": "Technology", "sub_industry": "Semiconductors"},
        }
    )
    print(f"Approved: {len(result['approved'])} — "
          f"{[s['ticker'] for s in result['approved']]}")
    print(f"Rejected: {len(result['individually_rejected'])} — "
          f"{[s['ticker'] for s in result['individually_rejected']]}")
