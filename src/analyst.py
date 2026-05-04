"""
LLM-based analyst — uses Claude to score setups and produce trade thesis.

Pipeline:
  1. Haiku 4.5 quick filter: rate 50 candidates 0-100, keep top 15
  2. Opus 4.7 deep analysis: full thesis + entry/stop/TP/conviction for top 5

Cost-optimized: prompt caching enabled, output token caps tight.
"""
import json
import logging
from typing import Dict, List, Any, Optional

from anthropic import Anthropic

from src import config

logger = logging.getLogger(__name__)

# Initialize once
_client: Optional[Anthropic] = None


def get_client() -> Anthropic:
    """Lazy-init Anthropic client (avoids errors if key missing during import)."""
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ============================================================
# SYSTEM PROMPTS (cacheable for cost savings)
# ============================================================

FILTER_SYSTEM_PROMPT = """You are a senior quantitative analyst at a top hedge fund (Jane Street style).
Your job: rate swing trade LONG candidates on a scale 0-100.

Universe: S&P 500 stocks. Holding period: 1 day to 1 week max.
Direction bias: LONG ONLY (no shorts, no inverse ETFs).

RATING CRITERIA:
- Technical setup quality (SMC patterns, RSI, MACD, volume)
- Trend alignment across timeframes
- Risk/reward potential at current levels
- Macro regime fit

OUTPUT FORMAT: JSON only. No prose, no explanation.
{"score": <0-100>, "reason": "<10 words max>"}

Rules:
- Score >= 75 = strong setup, deserves deep analysis
- Score 60-74 = decent setup, watch
- Score < 60 = skip
- Be CRITICAL. Only ~30% of candidates should score >= 70.
"""

DEEP_SYSTEM_PROMPT = """You are a senior Portfolio Manager at Jane Street, specializing in equity swing trading.
Your job: produce a full trade thesis for a LONG-ONLY swing setup (max 1 week hold).

DECISION FRAMEWORK (use this hierarchy when indicators conflict):
TIER 1 (VETO POWER - all must pass):
  - Macro regime: VIX < 25 OR SPY > 200MA
  - HTF (1W) not in clear downtrend
  - Achievable RR >= 2:1

TIER 2 (PRIMARY - need 2 of 3):
  - SMC setup (Order Block / FVG / BOS active)
  - Volume confirmation (>1.5x avg or rising on test)
  - Multi-TF alignment (1D up + 4H not extreme)

TIER 3 (SECONDARY - bonus only):
  - RSI in 40-65 zone
  - MACD bullish (histogram positive or recent cross)
  - Sector outperforming SPY

CONFLICT RESOLUTION:
- SMC > Volume > Macro > RSI > MACD (price action leads, lagging indicators follow)
- If TIER 1 fails: conviction = 0, SKIP
- If TIER 2 < 2 confirmations: max conviction = 64 (watchlist only)

CONVICTION SCORING (0-100):
  Technical (30%) + Fundamental (25%) + Macro (20%) + News (15%) + RR (10%)

CONVICTION TIERS:
  80-100 = HIGH (max 2x leverage acceptable)
  65-79 = STANDARD (1x only)
  50-64 = WATCHLIST (do not enter)
  < 50 = SKIP

POSITION SIZING:
  Account: $100. Max risk per trade: 3% ($3).
  Position size = Risk / (Entry - Stop) * Entry

OUTPUT: Strict JSON only. No prose outside JSON.
{
  "ticker": "<symbol>",
  "conviction": <0-100>,
  "breakdown": {
    "technical": <0-30>,
    "fundamental": <0-25>,
    "macro": <0-20>,
    "news_catalyst": <0-15>,
    "rr_asymmetry": <0-10>
  },
  "tier_assessment": {
    "tier_1_pass": <bool>,
    "tier_2_confirmations": <int 0-3>,
    "tier_3_bonus": <int 0-3>
  },
  "direction": "LONG",
  "setup_type": "<short label like 'SMC OB bounce + BOS continuation'>",
  "entry": <float>,
  "stop_loss": <float>,
  "tp1": <float>,
  "tp2": <float>,
  "rr_ratio": <float>,
  "rr_extended": <float>,
  "position_size_usd": <float>,
  "shares": <float>,
  "risk_amount_usd": <float>,
  "max_leverage": "<1x or 2x>",
  "thesis": "<3-4 sentence explanation>",
  "invalidation": ["<condition 1>", "<condition 2>", "<condition 3>"],
  "max_holding_days": <int 1-7>
}

CRITICAL RULES:
- LONG only (entry > stop_loss always)
- RR >= 2:1 minimum (fail this = conviction max 50)
- Risk amount must be <= $3.00
- Be honest. If setup is mediocre, score it mediocre.
"""


# ============================================================
# STAGE 1: HAIKU FILTER (cheap, fast)
# ============================================================

def haiku_filter_candidate(ticker: str, brief_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Quick LLM filter for a single candidate.
    Input: small data summary. Output: {score, reason}.
    Cost: ~$0.002 per call.
    """
    client = get_client()

    user_prompt = f"""Stock: {ticker}
Sector: {brief_data.get('sector', 'Unknown')}
Price: ${brief_data.get('price', 0):.2f}
RSI(14): {brief_data.get('rsi', 0):.1f} ({brief_data.get('rsi_zone', '?')})
MACD histogram: {brief_data.get('macd_histogram', 0):.3f} (rising: {brief_data.get('macd_histogram_rising', False)})
MACD bullish cross last 5d: {brief_data.get('macd_bullish_cross_5d', False)}
Above 20MA: {brief_data.get('above_sma_20', False)} | 50MA: {brief_data.get('above_sma_50', False)} | 200MA: {brief_data.get('above_sma_200', False)}
Volume ratio (vs 20d avg): {brief_data.get('volume_ratio', 0):.2f}x
SMC Order Blocks active: {brief_data.get('num_order_blocks', 0)}
SMC Unfilled FVGs: {brief_data.get('num_fvgs', 0)}
SMC Structure: BOS_bullish={brief_data.get('bos_bullish', False)}, CHoCH_bearish={brief_data.get('choch_bearish', False)}
SMC Zone: {brief_data.get('zone', 'unknown')}
HTF aligned bullish: {brief_data.get('htf_aligned_bullish', False)}
Macro regime: {brief_data.get('macro_regime', 'unknown')}

Rate this LONG swing setup 0-100. JSON only."""

    try:
        response = client.messages.create(
            model=config.LLM_MODEL_FILTER,
            max_tokens=config.LLM_MAX_TOKENS_FILTER,
            temperature=config.LLM_TEMPERATURE,
            system=[{
                "type": "text",
                "text": FILTER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"} if config.LLM_USE_PROMPT_CACHING else None,
            }] if config.LLM_USE_PROMPT_CACHING else FILTER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
        # Strip code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
        parsed = json.loads(raw_text)
        return {
            "ticker": ticker,
            "score": int(parsed.get("score", 0)),
            "reason": parsed.get("reason", ""),
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
        }
    except json.JSONDecodeError as e:
        logger.error(f"Haiku JSON parse failed for {ticker}: {e} | raw: {raw_text[:200]}")
        return {"ticker": ticker, "score": 0, "reason": "parse_error"}
    except Exception as e:
        logger.error(f"Haiku filter failed for {ticker}: {e}")
        return {"ticker": ticker, "score": 0, "reason": f"error: {str(e)[:50]}"}


def filter_candidates_haiku(candidates: List[Dict[str, Any]], top_n: int = 15) -> List[Dict[str, Any]]:
    """
    Run Haiku filter on a list of candidates, return top N by score.
    """
    scored = []
    for cand in candidates:
        ticker = cand.get("ticker")
        brief = cand.get("brief_data", {})
        result = haiku_filter_candidate(ticker, brief)
        scored.append({**cand, **result})
        logger.info(f"Haiku scored {ticker}: {result['score']} ({result['reason']})")

    scored.sort(key=lambda x: -x.get("score", 0))
    return scored[:top_n]


# ============================================================
# STAGE 2: OPUS DEEP ANALYSIS (premium quality)
# ============================================================

def opus_deep_analysis(ticker: str, full_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full thesis generation for a strong candidate.
    Input: comprehensive data. Output: full trade signal JSON.
    Cost: ~$0.05 per call.
    """
    client = get_client()

    user_prompt = _build_deep_analysis_prompt(ticker, full_data)

    try:
        response = client.messages.create(
            model=config.LLM_MODEL_DEEP,
            max_tokens=config.LLM_MAX_TOKENS_DEEP,
            temperature=config.LLM_TEMPERATURE,
            system=[{
                "type": "text",
                "text": DEEP_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"} if config.LLM_USE_PROMPT_CACHING else None,
            }] if config.LLM_USE_PROMPT_CACHING else DEEP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
        parsed = json.loads(raw_text)
        parsed["_tokens_used"] = response.usage.input_tokens + response.usage.output_tokens
        parsed["_input_tokens"] = response.usage.input_tokens
        parsed["_output_tokens"] = response.usage.output_tokens
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"Opus JSON parse failed for {ticker}: {e} | raw: {raw_text[:300]}")
        return {"ticker": ticker, "error": "parse_error", "raw": raw_text[:500]}
    except Exception as e:
        logger.error(f"Opus deep analysis failed for {ticker}: {e}")
        return {"ticker": ticker, "error": str(e)[:200]}


def _build_deep_analysis_prompt(ticker: str, data: Dict[str, Any]) -> str:
    """Construct the user prompt for Opus deep analysis."""
    fund = data.get("fundamentals", {})
    tech = data.get("technical", {}).get("traditional", {})
    smc = data.get("technical", {}).get("smc", {})
    mtf = data.get("technical", {}).get("mtf_alignment", {})
    macro = data.get("macro_context", {})
    news = data.get("news", [])

    # Format SMC findings
    obs = smc.get("order_blocks", [])
    fvgs = smc.get("fair_value_gaps", [])
    structure = smc.get("structure", {})
    liq_sweep = smc.get("liquidity_sweep", {})
    zone = smc.get("zone", {})

    ob_summary = "\n".join([
        f"  - OB at ${ob['low']:.2f}-${ob['high']:.2f} (mid ${ob['mid']:.2f}), {ob['bars_since_formed']} bars old, impulse {ob['impulse_strength_atr']:.1f}x ATR"
        for ob in obs[:3]
    ]) or "  (none active)"

    fvg_summary = "\n".join([
        f"  - FVG ${fvg['gap_low']:.2f}-${fvg['gap_high']:.2f} (size {fvg['size_atr']:.2f}x ATR), {fvg['bars_since_formed']} bars old"
        for fvg in fvgs[:3]
    ]) or "  (none unfilled)"

    news_summary = "\n".join([
        f"  - {n.get('title', '')[:100]}"
        for n in news[:3]
    ]) or "  (no recent news)"

    return f"""TICKER: {ticker}
COMPANY: {fund.get('name', ticker)}
SECTOR: {fund.get('sector', 'Unknown')} / {fund.get('industry', 'Unknown')}

=== PRICE & MOMENTUM (1D) ===
Current price: ${tech.get('price', 0):.2f}
ATR(14): ${tech.get('atr', 0):.2f}
RSI(14): {tech.get('rsi', 0):.1f} (zone: {tech.get('rsi_zone', '?')})
MACD histogram: {tech.get('macd_histogram', 0):.3f} (rising: {tech.get('macd_histogram_rising', False)})
MACD bullish cross last 5d: {tech.get('macd_bullish_cross_5d', False)}

=== MOVING AVERAGES ===
SMA20: ${tech.get('sma_20', 0):.2f} | above: {tech.get('above_sma_20', False)}
SMA50: ${tech.get('sma_50', 0):.2f} | above: {tech.get('above_sma_50', False)}
SMA200: ${tech.get('sma_200', 0):.2f} | above: {tech.get('above_sma_200', False)}

=== VOLUME ===
Current: {tech.get('volume_metrics', {}).get('current_volume', 0):,.0f}
20-day avg: {tech.get('volume_metrics', {}).get('avg_20d_volume', 0):,.0f}
Ratio: {tech.get('volume_metrics', {}).get('volume_ratio', 0):.2f}x (surge: {tech.get('volume_metrics', {}).get('is_surge', False)})

=== SMC: ORDER BLOCKS (Bullish, Active) ===
{ob_summary}

=== SMC: UNFILLED FVGs ===
{fvg_summary}

=== SMC: STRUCTURE ===
BOS bullish: {structure.get('bos_bullish', False)}
CHoCH bearish: {structure.get('choch_bearish', False)}
Last swing high: ${structure.get('last_swing_high', 0):.2f}
Last swing low: ${structure.get('last_swing_low', 0):.2f}

=== SMC: LIQUIDITY ===
Bullish sweep detected: {liq_sweep.get('liquidity_sweep_bullish', False)}
Swept low level: ${liq_sweep.get('swept_swing_low', 0):.2f}

=== SMC: ZONE ===
Current zone: {zone.get('zone', 'unknown')} (recommendation: {zone.get('recommendation', '?')})
Fib position: {zone.get('fib_position_pct', 0):.1f}% of swing range

=== MULTI-TIMEFRAME TRENDS ===
1W: {mtf.get('1wk', {}).get('trend', '?')}
1D: {mtf.get('1d', {}).get('trend', '?')}
4H: {mtf.get('4h', {}).get('trend', '?')}
1H: {mtf.get('1h', {}).get('trend', '?')}
HTF aligned bullish: {mtf.get('htf_aligned_bullish', False)}

=== FUNDAMENTALS ===
Market cap: ${fund.get('market_cap', 0):,.0f}
Forward P/E: {fund.get('forward_pe', 'N/A')}
PEG ratio: {fund.get('peg_ratio', 'N/A')}
Beta: {fund.get('beta', 'N/A')}
Profit margin: {fund.get('profit_margin', 'N/A')}
Revenue growth: {fund.get('revenue_growth', 'N/A')}
Earnings growth: {fund.get('earnings_growth', 'N/A')}

=== MACRO REGIME ===
SPY: ${macro.get('spy_price', 0):.2f} (vs 200MA: above={macro.get('spy_above_200ma', False)})
SPY 5d change: {macro.get('spy_5d_change_pct', 0):+.2f}%
VIX: {macro.get('vix', 0):.1f} (panic mode: {macro.get('vix_panic', False)})
DXY: {macro.get('dxy', 0):.2f}
10Y yield: {macro.get('10y_yield', 'N/A')}
Fed funds: {macro.get('fed_funds', 'N/A')}
Yield curve inverted: {macro.get('yield_curve_inverted', False)}
Sector ({fund.get('sector', '?')}) 5d perf: {macro.get('sector_5d_perf', {}).get(fund.get('sector', ''), 'N/A')}%

=== RECENT NEWS ===
{news_summary}

=== TASK ===
Produce full trade signal JSON per the system prompt schema.
Account size: $100. Max risk: $3 (3%). Min RR: 2:1.
"""


# ============================================================
# UTILITY: TOKEN/COST TRACKING
# ============================================================

def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Rough cost estimate per Anthropic April 2026 pricing."""
    rates = {
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-opus-4-7": (5.0, 25.0),
    }
    in_rate, out_rate = rates.get(model, (5.0, 25.0))
    return (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)


if __name__ == "__main__":
    # Quick smoke test (requires ANTHROPIC_API_KEY env var)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Test Haiku filter
    test_brief = {
        "sector": "Technology",
        "price": 208.50,
        "rsi": 58.2,
        "rsi_zone": "neutral_bullish",
        "macd_histogram": 0.45,
        "macd_histogram_rising": True,
        "macd_bullish_cross_5d": True,
        "above_sma_20": True,
        "above_sma_50": True,
        "above_sma_200": True,
        "volume_ratio": 1.8,
        "num_order_blocks": 1,
        "num_fvgs": 1,
        "bos_bullish": True,
        "choch_bearish": False,
        "zone": "discount",
        "htf_aligned_bullish": True,
        "macro_regime": "risk_on_bull",
    }
    print("\n=== Haiku Filter Test (NVDA) ===")
    result = haiku_filter_candidate("NVDA", test_brief)
    print(json.dumps(result, indent=2))
