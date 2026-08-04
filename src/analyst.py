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
  - Opus TIER structure: TIER 1 veto (macro + weekly trend only — RR removed)
  - Confluence display injected into Opus output

Pipeline:
  1. Mandatory screener (in indicators.py) → 5-10 candidates
  2. Haiku 4.5 quality scoring → keep top N (default 5)
  3. Opus 4.7 deep analysis → full thesis with entry/stop/TP/conviction

PHASE 2.3 REVISION (June 2026):
================================
HAIKU CHANGES:
  - Scoring weights updated: filter strength 50%, fundamentals 30%, news 20%
  - Confluence reward REMOVED from scoring (data showed 3/3 = 71% loss rate)
  - Confluence count passed as neutral context only
  - F6 soft warning added to Haiku user prompt (ema9_ext_atr)
  - F7 soft warning added to Haiku user prompt (rs_gap_vs_spy_pct)

OPUS CHANGES:
  - Entry changed from yesterday's close to EMA9 value (limit order)
  - Entry = ema_9 value from brief_data (already in prompt data)
  - Stop / TP1 / TP2 all computed from EMA9 entry
  - Shadow fields added to Opus output dict (_shadow_indicators)

PHASE 2.3 — F7 LOOKBACK REVIEW BUGFIX (June 2026):
====================================================
FIXED:
  - _shadow_indicators in opus_deep_analysis() was missing rs_60d_gap and
    rs_126d_gap. These fields were added to brief_data in main.py's
    run_mandatory_screener() (F7 lookback review) but the manual field
    list here was never updated to carry them through, so every shadow
    log row (CSV + Google Sheet) silently wrote blank values for both
    columns despite the upstream computation working correctly.
  - IMPORTANT — _shadow_indicators is a hand-picked subset of `indicators`,
    NOT a full passthrough. Any new field added to brief_data in main.py
    that needs to reach the shadow log MUST be added explicitly to the
    dict below, or it will silently be dropped at this exact point.

HAIKU TIE-BREAK FIX (June 2026):
==================================
FIXED:
  - filter_candidates_haiku() previously sorted candidates by Haiku score
    only (`-score`). Python's sort() is stable, so any tie at the same
    score was silently broken by whatever order the candidate happened
    to arrive in — which traces back to S&P 500 universe CSV row order
    in main.py's run_mandatory_screener(). That has zero relationship to
    setup quality. Confirmed in production logs (run #48/#50, June 20):
    ties at score 78 and 72 existed across multiple tickers, and which
    ones made the top-5 cut was determined purely by CSV position — e.g.
    a ticker with the strongest ADX margin in its tie group (MMM, ADX
    58.3) did not make the cut while a ticker with a much weaker margin
    in the same tie group (APH, ADX 41.2) did, simply because of row
    order, not quality.
  - Fix: secondary sort key on ADX(14) (`brief_data["adx_14"]`), descending.
    ADX margin was chosen over the other four mandatory filters (F1, F3,
    F4, F5) because it's the only one with a clean monotonic "higher is
    strictly better" relationship in this system's own data — F3 (RSI)
    and F4 (volume ratio) both have evidence of the OPPOSITE: a high
    margin on those two is associated with exhaustion/climax entries
    (Phase 2.2 diagnostic: DELL RSI 89.4, FIX/KR/LITE near-zero MFE on
    volume-surge entries), so using them as a quality tie-break would
    actively reward the failure pattern. ADX margin is also already the
    dominant phrase in Haiku's own scoring rationale across production
    logs, and is the explicit example given in FILTER_SYSTEM_PROMPT's
    "filter strength 50%" weighting ("ADX 38 >> ADX 26").
  - No new data fetch required — adx_14 is already present in every
    candidate's brief_data from the mandatory screener stage.
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

IMPORTANT: All stocks sent to you are from the S&P 500 universe and have ALREADY passed
mandatory technical filters:
  - Price > EMA(50)
  - ADX(14) > 25 AND +DI > -DI (confirmed uptrend)
  - RSI(14) > 45 (not oversold)
  - Volume ratio (14d avg) > 1.3x (above-average activity)
  - ATR(14) > 1.5% of price (sufficient volatility for 2:1 RR)

Your job is NOT basic filtering. Your job is QUALITY RANKING within this pre-screened universe.

Universe: S&P 500 stocks ONLY.
Holding period: 1 day to 1 week max.
Direction: LONG ONLY.

SCORING WEIGHTS (use these explicitly):
  Filter strength    50% — how far above threshold each F1-F5 passed
                          (e.g. ADX 38 >> ADX 26, RSI 65 >> RSI 46)
                          Reward strong filter margin, penalise borderline passes.
  Fundamental quality 30% — revenue growth, earnings trend, sector momentum
  News sentiment      20% — catalyst presence, analyst upgrades, risk events

CONFLUENCE NOTE (IMPORTANT):
  Confluence count (0/1/2/3: MACD, EMA9, EMA21) is provided as context ONLY.
  Do NOT reward higher confluence counts. Data shows 3/3 confluence = 71% loss rate.
  Treat it as neutral information. Do NOT penalise low confluence counts either.

SOFT WARNINGS (penalise if severe — do not hard-reject):
  EMA9 extension > 1.5×ATR → penalise (extended entry risk, likely buying the surge)
  RS gap vs SPY < -3.5% → penalise (sector laggard in relative terms)
  Penalise proportionally: 2.0×ATR extension is mild; 3.0×ATR is severe.

SCORING GUIDE:
  80-100 = Exceptional setup. Strong filter margins, clean fundamentals, macro aligned.
  65-79  = Good setup. Most criteria met, minor concerns.
  50-64  = Watchlist only. Passes filters but lacks conviction.
  < 50   = Weak. Skip even though it passed mandatory filters.

OUTPUT FORMAT: JSON only. No prose, no explanation outside JSON.
{"score": <0-100>, "reason": "<15 words max>"}

Rules:
  - Be CRITICAL. Only ~30% of candidates should score >= 65.
  - Penalize stocks that are extended (RSI near 80, far above EMAs in ATR terms).
  - Reward stocks with strong ADX margin (38 vs 26), volume well above average.
  - Consider if the setup is early enough for a 10-day hold to generate 2:1 RR.
"""

DEEP_SYSTEM_PROMPT = """You are a senior Portfolio Manager at Jane Street, specializing in equity swing trading.
Your job: produce a full trade thesis for a LONG-ONLY swing setup (max 10 trading days hold).

CONTEXT: All stocks reaching you are S&P 500 names that have passed:
  (a) Mandatory technical screener (EMA50, ADX+DI, RSI>45, Volume>1.3x, ATR>1.5%)
  (b) Haiku quality filter (score >= 65)

Your role is DEEP VALIDATION and THESIS GENERATION, not re-screening basics.

DECISION FRAMEWORK:

TIER 1 — VETO POWER (both must pass, or conviction = 0 and output SKIP):
  1. Macro regime: VIX < 25 OR SPY above 200MA
  2. Weekly (1W) trend: must be "bullish" or "neutral" — fails ONLY if "bearish"
     - "bearish"  = TIER 1 fails → veto
     - "neutral"  = TIER 1 passes → continue
     - "bullish"  = TIER 1 passes → continue
     The weekly_trend value is provided in the user prompt.

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

ENTRY AND POSITION SIZING:
  Account: $100 starting equity. Max risk per trade: 2% of CURRENT equity.
  DO NOT try to compute exact position size. The system recomputes shares,
  notional and risk deterministically after you respond, using the live
  equity balance (which you cannot see). Report your best estimate in the
  sizing fields; they will be overwritten. Focus on entry/stop/TP geometry.
  Entry = EMA9 value (provided in prompt data as ema_9).
         This is a LIMIT ORDER — you place the limit at EMA9 before US open.
         If price never pulls back to EMA9, the order does not fill. That is correct.
  Stop  = Entry - (1.5 x ATR)
  TP1   = Entry + (2.0 x (Entry - Stop))
  TP2   = Entry + (3.0 x (Entry - Stop))
  Shares        = Risk / (Entry - Stop)      <- this yields SHARES
  Position size = Shares x Entry             <- this yields USD
  (These were previously collapsed into one line, which produced share
   counts reported as dollar amounts. Keep them separate.)

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
  "max_holding_days": <int 1-10>,
  "confluence_display": "<pass through from input data, e.g. '2/3 (MACD✓ EMA9✗ EMA21✓)'>"
}

CRITICAL RULES:
  - LONG only (entry > stop_loss ALWAYS — if not, output is rejected)
  - Entry MUST equal the ema_9 value provided. Do not use close price as entry.
  - RR: calculate accurately. If RR < 2:1, report it honestly — do NOT auto-cap conviction. User decides entry.
  - Risk amount is set by the system (2% of current equity), not by you.
  - Be honest. Mediocre setup = mediocre score.
  - If TIER 1 fails (macro/trend only): conviction=0, tier_1_pass=false, tier_1_fail_reason required.
  - TIER 1 never fails due to RR alone — RR is informational only.
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

=== CONFLUENCE (context only — do NOT reward higher counts) ===
{brief_data.get('confluence_display', 'N/A')}

=== SOFT WARNINGS (penalise if severe) ===
EMA9 extension: {brief_data.get('ema9_extension_pct', 0):.1f}% ({brief_data.get('ema9_ext_atr', 0):.2f}× ATR)
  → Penalise if > 1.5×ATR (extended entry risk)
RS gap vs SPY (10d): {brief_data.get('rs_gap_vs_spy_pct', 0):.1f}%
  → Penalise if < -3.5% (sector laggard risk)

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

    Sort key: (-score, -adx_14). See module docstring "HAIKU TIE-BREAK FIX"
    for why ADX(14) was chosen as the tie-break over the other mandatory
    filters. Without the secondary key, Python's stable sort would silently
    break ties by candidate list order (i.e. S&P 500 universe CSV row
    order), which has no relationship to setup quality — confirmed in
    production logs (run #48/#50, June 2026).
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

    scored.sort(
        key=lambda x: (
            -x.get("score", 0),
            -x.get("brief_data", {}).get("adx_14", 0),
        )
    )
    top = scored[:top_n]
    logger.info(
        f"Haiku top {top_n}: "
        f"{[(c['ticker'], c['score'], c.get('brief_data', {}).get('adx_14', 0)) for c in top]}"
    )
    return top


# ============================================================
# STAGE 2: OPUS DEEP ANALYSIS (premium quality)
# ============================================================

def opus_deep_analysis(ticker: str, full_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full thesis generation for a strong candidate.
    Input: comprehensive data from data_fetcher + indicators.
    Output: full trade signal JSON + _shadow_indicators dict for logging.

    NOTE — _shadow_indicators below is a hand-picked subset of the
    `indicators` dict (i.e. brief_data from main.py), not a full
    passthrough. Any field added upstream in main.py's brief_data that
    needs to show up in the shadow log (CSV or Google Sheet) MUST be
    added explicitly here too, or it will silently write blank in every
    downstream log. This already bit us once (rs_60d_gap / rs_126d_gap
    from the F7 lookback review were computed correctly upstream but
    missing here, so every shadow row had those two columns blank).
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

        # --------------------------------------------------------
        # Phase 2.3: Attach shadow indicator snapshot for logging
        # Carries indicator values at signal time into the signal dict
        # so _append_shadow_log() / sheets_logger can read them without
        # re-fetching data.
        # --------------------------------------------------------
        ind = full_data.get("indicators", {})
        parsed["_shadow_indicators"] = {
            "close_price": ind.get("price", ""),
            "ema9_price": ind.get("ema_9", ""),
            "ema9_gap_pct": ind.get("ema9_extension_pct", ""),
            "ema9_ext_atr": ind.get("ema9_ext_atr", ""),
            "atr": ind.get("atr_14", ""),
            "atr_pct": ind.get("atr_pct", ""),
            "rs_gap_vs_spy": ind.get("rs_gap_vs_spy_pct", ""),
            "spy_10d_return": ind.get("spy_10d_return", ""),
            "stock_10d_return": ind.get("stock_10d_return", ""),
            "rs_60d_gap": ind.get("rs_60d_gap", ""),      # FIX (F7 review) — was missing
            "rs_126d_gap": ind.get("rs_126d_gap", ""),    # FIX (F7 review) — was missing
            "confluence_count": ind.get("confluence_count", ""),
            "adx": ind.get("adx_14", ""),
            "rsi": ind.get("rsi_14", ""),
            "volume_ratio": ind.get("vol_ratio_14d", ""),
            "entry_close": ind.get("price", ""),   # close price at signal time
            # ---- PHASE 2.6 additions ----
            # REMINDER (this exact dict caused the rs_60d_gap / rs_126d_gap
            # blank-column bug): _shadow_indicators is a HAND-PICKED subset of
            # `indicators`, not a passthrough. Any new field that must reach
            # the shadow log has to be listed here explicitly or it silently
            # writes blank.
            #
            # entry_type defaults to "strict" (Stage 1: EMA9 limit filled).
            # main.py overwrites it with "fallback" for two-stage near-miss
            # entries executed at the next open. This column is the whole
            # out-of-sample test of ENTRY2_ATR_TOLERANCE — without it the
            # 0.15 threshold can never be validated.
            "entry_type": "strict",
            # Regime context — LOG ONLY, never gates anything. A regime hard
            # gate was tested on Batch 3 and rejected: better backtested R,
            # but it removed the 6 largest winners (+8.4R) and cut breadth
            # 32 -> 13 (~36% of statistical power).
            "spy_vs_ema20": ind.get("spy_vs_ema20", ""),
            "vix_level": ind.get("vix_level", ""),
            "pct_above_50dma": ind.get("pct_above_50dma", ""),
        }

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

Current price: ${ind.get('price', 0):.2f}
EMA9 (LIMIT ENTRY): ${ind.get('ema_9', 0):.2f}

=== 🔒 MANDATORY FILTERS (all pre-verified as PASS — these gated the signal) ===

  1. Price > EMA(50): PASS
     Price ${ind.get('price', 0):.2f} > EMA50 ${ind.get('ema_50', 0):.2f}

  2. ADX(14) > 25 AND +DI > -DI: PASS
     ADX={ind.get('adx_14', 0):.1f} | +DI={ind.get('plus_di', 0):.1f} | -DI={ind.get('minus_di', 0):.1f}

  3. RSI(14) > 45: PASS
     RSI={ind.get('rsi_14', 0):.1f}

  4. Volume ratio (14d) > 1.3x: PASS
     Today: {ind.get('vol_today', 0):,} | 14d avg: {ind.get('vol_avg_14d', 0):,} | Ratio: {ind.get('vol_ratio_14d', 0):.2f}x

  5. ATR(14) > 1.5% of price: PASS
     ATR=${ind.get('atr_14', 0):.2f} ({ind.get('atr_pct', 0):.2f}% of price)

=== ➕ OPTIONAL CONFLUENCE (informational — displayed only, does NOT gate) ===

  - MACD histogram > 0: {ind.get('macd_positive', False)}
    Value: {ind.get('macd_histogram', 0):.4f}
  - Price > EMA(21): {ind.get('above_ema_21', False)}
    EMA21 = ${ind.get('ema_21', 0):.2f}
  - Price > EMA(9): {ind.get('above_ema_9', False)}
    EMA9 = ${ind.get('ema_9', 0):.2f}

Confluence score: {confluence_display}

=== ⚠️ SOFT WARNINGS (context — factor into conviction, not a veto) ===

  EMA9 extension: {ind.get('ema9_extension_pct', 0):.1f}% ({ind.get('ema9_ext_atr', 0):.2f}× ATR)
  RS gap vs SPY (10d): {ind.get('rs_gap_vs_spy_pct', 0):.1f}%
    (Stock {ind.get('stock_10d_return', 0):.1f}% vs SPY {ind.get('spy_10d_return', 0):.1f}%)

=== 📅 WEEKLY TREND (TIER 1 input) ===
1W trend: {data.get('weekly_trend', 'unknown')}
TIER 1 rule: passes if "bullish" or "neutral"; fails ONLY if "bearish"

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
ENTRY MUST BE EMA9 = ${ind.get('ema_9', 0):.2f} (limit order — NOT close price).
Stop = EMA9 - (1.5 × ATR). TP1 = EMA9 + (2 × risk). TP2 = EMA9 + (3 × risk).
Pass through confluence_display exactly as shown above.
"""


# ============================================================
# UTILITY — COST TRACKING
# ============================================================

def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """
    Rough cost estimate per Anthropic pricing (verified May 2026).
    Source: docs.claude.com/en/about-claude/pricing
    """
    rates = {
        "claude-haiku-4-5-20251001": (1.0, 5.0),   # Haiku 4.5
        "claude-opus-4-8": (5.0, 25.0),             # Opus 4.8
        "claude-sonnet-4-6": (3.0, 15.0),           # Sonnet 4.6
    }
    in_rate, out_rate = rates.get(model, (5.0, 25.0))  # Default to Opus pricing
    return (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)
