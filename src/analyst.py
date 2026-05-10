"""
analyst.py — LLM-based analyst using Claude to score setups and produce trade thesis.

STAGE 2 REVISION (May 2026):
============================
BUGS FIXED:
  - Removed `temperature` parameter (caused 400 errors on all Opus calls)
  - Fixed cache_control: None issue (key now omitted when caching disabled)
  - Promotion threshold lowered: 75 → 65 (was starving Opus of candidates)
  - Top N promoted to Opus: 3 → 5

REMOVED:
  - All SMC references from Haiku + Opus prompts and input data
  - 4H / HTF alignment references
  - TIER 2 SMC criteria from Opus system prompt

NEW:
  - Haiku now receives pre-screened candidates (mandatory 5 filters already passed)
  - Haiku job: quality RANKING only, not basic filtering
  - Opus TIER structure: TIER 1 veto (macro + RR) only
  - Confluence display injected into Opus output

Pipeline:
  1. Haiku 4.5: quality score candidates that passed mandatory screener, keep top N
  2. Opus 4: full thesis + entry/stop/TP/conviction for top 5
"""
import json
import logging
from typing import Dict, List, Any, Optional

from anthropic import Anthropic

from src import config

logger = logging.getLogger(__name__)

_client: Optional[Anthropic] = None


def get_client() -> Anthropic:
    """Lazy-init Anthropic client."""
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ============================================================
# SYSTEM PROMPTS
# ============================================================

FILTER_SYSTEM_PROMPT = """You are a senior quantitative analyst at a top hedge fund (Jane Street style).
Your job: rate swing trade LONG candidates on a scale 0-100.

IMPORTANT: All stocks sent to you have ALREADY passed mandatory technical filters:
  - Price > EMA(50)
  - ADX(14) > 25 AND +DI > -DI (confirmed uptrend)
  - RSI(14) > 45 (not oversold)
  - Volume ratio (14d avg) > 1.3x (above-average activity)
  - ATR(14) > 1.5% of price (sufficient volatility for 2:1 RR)

Your job is NOT basic filtering. Your job is QUALITY RANKING within this pre-screened universe.

Universe: S&P 500 stocks. Holding period: 1 day to 1 week max.
Direction: LONG ONLY.

RATING CRITERIA (focus on these):
  1. Trend quality — how clean and strong is the uptrend? (EMA alignment, ADX strength)
  2. Entry timing — is price at a good risk/reward entry point or already extended?
  3. Momentum conviction — MACD histogram, volume surge quality
  4. Macro regime fit — does the sector/stock benefit from current macro?
  5. Fundamental quality — is the business backing the technical move?

SCORING GUIDE:
  80-100 = Exceptional setup. Strong trend, clean entry, volume confirmed, macro aligned.
  65-79  = Good setup. Most criteria met, minor concerns.
  50-64  = Watchlist only. Passes filters but lacks conviction.
  < 50   = Weak. Skip even though it passed mandatory filters.

OUTPUT FORMAT: JSON only. No prose, no explanation outside JSON.
{"score": <0-100>, "reason": "<15 words max>"}

Rules:
  - Be CRITICAL. Only ~30% of candidates should score >= 65.
  - Penalize stocks that are extended (RSI near 80, far above EMAs).
  - Reward stocks showing fresh momentum (MACD turning positive, volume surging today).
  - Consider if the setup is early enough for a 7-day hold to generate 2:1 RR.
"""

DEEP_SYSTEM_PROMPT = """You are a senior Portfolio Manager at Jane Street, specializing in equity swing trading.
Your job: produce a full trade thesis for a LONG-ONLY swing setup (max 1 week hold).

CONTEXT: All stocks reaching you have passed:
  (a) Mandatory technical screener (EMA50, ADX+DI, RSI>45, Volume>1.3x, ATR>1.5%)
  (b) Haiku quality filter (score >= 65)

Your role is DEEP VALIDATION and THESIS GENERATION, not re-screening basics.

DECISION FRAMEWORK:

TIER 1 — VETO POWER (all 3 must pass, or conviction = 0 and output SKIP):
  1. Macro regime: VIX < 25 OR SPY above 200MA
  2. Weekly (1W) trend: NOT in clear downtrend (no consecutive lower highs/lows on weekly)
  3. RR achievable: entry/stop/target math produces RR >= 2:1

CONVICTION SCORING (0-100):
  Technical  (30%) — EMA alignment, ADX strength, momentum quality
  Fundamental (25%) — earnings growth, margin, valuation reasonableness
  Macro      (20%) — sector tailwind, VIX, SPY trend, rate environment
  News       (15%) — catalyst presence, earnings proximity risk
  RR         (10%) — actual entry/stop/TP math quality

CONVICTION TIERS:
  80-100 = HIGH conviction
  65-79  = STANDARD conviction
  50-64  = WATCHLIST (do not enter — flag only)
  < 50   = SKIP

POSITION SIZING:
  Account: $100. Max risk per trade: 3% ($3).
  Position size = Risk / (Entry - Stop)
  Stop = Entry - (1.5 x ATR)
  TP1  = Entry + (2.0 x (Entry - Stop))
  TP2  = Entry + (3.0 x (Entry - Stop))

OUTPUT: Strict JSON only. No prose outside JSON.
{
  "ticker": "<symbol>",
  "conviction": <0-100>,
  "tier_1_pass": <bool>,
  "tier_1_fail_reason": "<null or reason why TIER 1 failed>",
  "breakdown": {
    "technical": <0-30>,
    "fundamental": <0-25>,
    "macro": <0-20>,
    "news_catalyst": <0-15>,
    "rr_asymmetry": <0-10>
  },
  "direction": "LONG",
  "setup_type": "<short label, e.g. 'Trend continuation + volume surge'>",
  "entry": <float>,
  "stop_loss": <float>,
  "tp1": <float>,
  "tp2": <float>,
  "rr_ratio": <float>,
  "rr_extended": <float>,
  "position_size_usd": <float>,
  "shares": <float>,
  "risk_amount_usd": <float>,
  "thesis": "<3-4 sentences. Why this stock, why now, what confirms the move>",
  "invalidation": ["<condition 1>", "<condition 2>", "<condition 3>"],
  "max_holding_days": <int 1-7>,
  "confluence_display": "<pass through from input data, e.g. '2/3 (MACD✓ EMA9✗ EMA21✓)'>"
}

CRITICAL RULES:
  - LONG only (entry > stop_loss ALWAYS — if not, output is rejected)
  - RR >= 2:1 minimum (fail = conviction max 50)
  - Risk amount must be <= $3.00
  - Be honest. Mediocre setup = mediocre score.
  - If TIER 1 fails: set conviction=0, tier_1_pass=false, fill tier_1_fail_reason.
"""


# ============================================================
# HELPERS — cache_control safe builder
# ============================================================

def _build_system(prompt_text: str, use_cache: bool) -> Any:
    """
    Build the system prompt arg correctly.
    Avoids passing cache_control=None which can cause API issues.
    """
    if use_cache:
        return [{
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }]
    return prompt_text  # Plain string when caching disabled


# ============================================================
# STAGE 1: HAIKU FILTER (cheap, fast)
# ============================================================

def haiku_filter_candidate(ticker: str, brief_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Quick LLM quality score for a single pre-screened candidate.
    Input: brief_data from indicators.build_brief_data().
    Output: {score, reason, tokens_used}.
    """
    client = get_client()

    user_prompt = f"""Stock: {ticker}
Sector: {brief_data.get('sector', 'Unknown')}
Price: ${brief_data.get('price', 0):.2f}

=== TREND ===
Above EMA(50): {brief_data.get('above_ema_50', False)}
Above EMA(21): {brief_data.get('above_ema_21', False)}
Above EMA(9):  {brief_data.get('above_ema_9', False)}
EMA(50): ${brief_data.get('ema_50', 0):.2f} | EMA(21): ${brief_data.get('ema_21', 0):.2f} | EMA(9): ${brief_data.get('ema_9', 0):.2f}

=== MOMENTUM ===
RSI(14): {brief_data.get('rsi_14', 0):.1f}
MACD histogram: {brief_data.get('macd_histogram', 0):.4f} (positive: {brief_data.get('macd_positive', False)})

=== TREND STRENGTH ===
ADX(14): {brief_data.get('adx_14', 0):.1f}
+DI: {brief_data.get('plus_di', 0):.1f} | -DI: {brief_data.get('minus_di', 0):.1f}
ADX bullish (+DI > -DI): {brief_data.get('adx_bullish', False)}

=== VOLATILITY ===
ATR(14): ${brief_data.get('atr_14', 0):.2f} ({brief_data.get('atr_pct', 0):.2f}% of price)

=== VOLUME ===
Volume ratio vs 14d avg: {brief_data.get('vol_ratio_14d', 0):.2f}x (surge: {brief_data.get('vol_surge', False)})

=== CONFLUENCE ===
{brief_data.get('confluence_display', 'N/A')}

=== MACRO ===
Regime: {brief_data.get('macro_regime', 'unknown')}

Note: This stock ALREADY passed all 5 mandatory filters.
Rate the QUALITY of this swing setup 0-100. JSON only."""

    try:
        response = client.messages.create(
            model=config.LLM_MODEL_FILTER,
            max_tokens=config.LLM_MAX_TOKENS_FILTER,
            system=_build_system(FILTER_SYSTEM_PROMPT, config.LLM_USE_PROMPT_CACHING),
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
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
        logger.error(f"Haiku JSON parse failed for {ticker}: {e}")
        return {"ticker": ticker, "score": 0, "reason": "parse_error"}
    except Exception as e:
        logger.error(f"Haiku filter failed for {ticker}: {e}")
        return {"ticker": ticker, "score": 0, "reason": f"error: {str(e)[:50]}"}


def filter_candidates_haiku(candidates: List[Dict[str, Any]],
                            top_n: int = None) -> List[Dict[str, Any]]:
    """
    Run Haiku quality scoring on pre-screened candidates, return top N by score.
    top_n defaults to config.LLM_HAIKU_TOP_N (5).
    """
    if top_n is None:
        top_n = getattr(config, "LLM_HAIKU_TOP_N", 5)

    scored = []
    for cand in candidates:
        ticker = cand.get("ticker")
        brief = cand.get("brief_data", {})
        result = haiku_filter_candidate(ticker, brief)
        scored.append({**cand, **result})
        logger.info(f"Haiku scored {ticker}: {result['score']} ({result['reason']})")

    scored.sort(key=lambda x: -x.get("score", 0))
    top = scored[:top_n]
    logger.info(
        f"Haiku top {top_n}: {[(c['ticker'], c['score']) for c in top]}"
    )
    return top


# ============================================================
# STAGE 2: OPUS DEEP ANALYSIS (premium quality)
# ============================================================

def opus_deep_analysis(ticker: str, full_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full thesis generation for a strong candidate.
    Input: comprehensive data from data_fetcher + indicators.
    Output: full trade signal JSON.
    """
    client = get_client()
    user_prompt = _build_deep_analysis_prompt(ticker, full_data)

    try:
        response = client.messages.create(
            model=config.LLM_MODEL_DEEP,
            max_tokens=config.LLM_MAX_TOKENS_DEEP,
            system=_build_system(DEEP_SYSTEM_PROMPT, config.LLM_USE_PROMPT_CACHING),
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
    ind = data.get("indicators", {})
    macro = data.get("macro_context", {})
    news = data.get("news", [])
    confluence_display = data.get("confluence_display", "N/A")

    news_summary = "\n".join([
        f"  - {n.get('title', '')[:100]}"
        for n in news[:3]
    ]) or "  (no recent news)"

    return f"""TICKER: {ticker}
COMPANY: {fund.get('name', ticker)}
SECTOR: {fund.get('sector', 'Unknown')} / {fund.get('industry', 'Unknown')}

=== MANDATORY FILTERS STATUS (all pre-verified as PASS) ===
Price > EMA(50): PASS
ADX > 25 + (+DI > -DI): PASS
RSI > 45: PASS
Volume ratio > 1.3x: PASS
ATR > 1.5%: PASS

=== PRICE & TREND ===
Current price: ${ind.get('price', 0):.2f}
EMA(9):  ${ind.get('ema_9', 0):.2f} | EMA(21): ${ind.get('ema_21', 0):.2f} | EMA(50): ${ind.get('ema_50', 0):.2f}
Above EMA(9):  {ind.get('above_ema_9', False)}
Above EMA(21): {ind.get('above_ema_21', False)}
Above EMA(50): True (mandatory filter)

=== MOMENTUM ===
RSI(14): {ind.get('rsi_14', 0):.1f}
MACD histogram: {ind.get('macd_histogram', 0):.4f} (positive: {ind.get('macd_positive', False)})

=== TREND STRENGTH ===
ADX(14): {ind.get('adx_14', 0):.1f}
+DI: {ind.get('plus_di', 0):.1f} | -DI: {ind.get('minus_di', 0):.1f}

=== VOLATILITY ===
ATR(14): ${ind.get('atr_14', 0):.2f} ({ind.get('atr_pct', 0):.2f}% of price)

=== VOLUME ===
Today: {ind.get('vol_today', 0):,}
14d avg: {ind.get('vol_avg_14d', 0):,}
Ratio: {ind.get('vol_ratio_14d', 0):.2f}x

=== CONFLUENCE (informational) ===
{confluence_display}

=== WEEKLY TREND (1W) ===
1W trend: {data.get('weekly_trend', 'unknown')}

=== FUNDAMENTALS ===
Market cap: ${fund.get('market_cap', 0):,.0f}
Forward P/E: {fund.get('forward_pe', 'N/A')}
PEG ratio: {fund.get('peg_ratio', 'N/A')}
Beta: {fund.get('beta', 'N/A')}
Profit margin: {fund.get('profit_margin', 'N/A')}
Revenue growth: {fund.get('revenue_growth', 'N/A')}
Earnings growth: {fund.get('earnings_growth', 'N/A')}

=== MACRO REGIME ===
SPY: ${macro.get('spy_price', 0):.2f} (above 200MA: {macro.get('spy_above_200ma', False)})
SPY 5d change: {macro.get('spy_5d_change_pct', 0):+.2f}%
VIX: {macro.get('vix', 0):.2f} (panic: {macro.get('vix_panic', False)})
10Y yield: {macro.get('10y_yield', 'N/A')}
Sector ({fund.get('sector', '?')}) 5d perf: {macro.get('sector_5d_perf', {}).get(fund.get('sector', ''), 'N/A')}%

=== RECENT NEWS ===
{news_summary}

=== TASK ===
Produce full trade signal JSON per the system prompt schema.
Account: $100. Max risk: $3 (3%). Min RR: 2:1.
Pass through confluence_display exactly as shown above.
"""


# ============================================================
# UTILITY — COST TRACKING
# ============================================================

def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Rough cost estimate (Anthropic May 2026 pricing)."""
    rates = {
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-opus-4-20250514": (15.0, 75.0),
    }
    in_rate, out_rate = rates.get(model, (15.0, 75.0))
    return (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)
