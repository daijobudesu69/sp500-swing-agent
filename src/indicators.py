"""
Technical indicators module — traditional + Smart Money Concepts (SMC).

Traditional: RSI, MACD, SMA, ATR, Volume metrics
SMC: Swing detection, Order Blocks, FVG, BOS/CHoCH, Liquidity Sweeps,
     Premium/Discount zones, Multi-TF alignment
"""
import logging
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import numpy as np

from src import config

logger = logging.getLogger(__name__)


# ============================================================
# TRADITIONAL INDICATORS
# ============================================================

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, pd.Series]:
    """MACD line, signal line, histogram."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def sma(close: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — used for volatility-based stops & SMC sizing."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def volume_metrics(df: pd.DataFrame, lookback: int = 20) -> Dict[str, float]:
    """Volume analysis — current vs average, surge detection."""
    vol = df["volume"]
    avg_vol = vol.rolling(lookback).mean()
    current_vol = float(vol.iloc[-1])
    avg_20d = float(avg_vol.iloc[-1]) if not avg_vol.iloc[-1] != avg_vol.iloc[-1] else 0
    return {
        "current_volume": current_vol,
        "avg_20d_volume": avg_20d,
        "volume_ratio": current_vol / avg_20d if avg_20d > 0 else 0,
        "is_surge": (current_vol / avg_20d) >= config.VOLUME_SURGE_MULTIPLIER if avg_20d > 0 else False,
    }


def compute_traditional_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all traditional indicators on a DataFrame, return latest values."""
    if df is None or len(df) < 200:
        return {"error": "Insufficient data (need >= 200 bars)"}

    close = df["close"]
    rsi_series = rsi(close, config.RSI_PERIOD)
    macd_data = macd(close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    atr_series = atr(df, config.ATR_PERIOD)
    sma_20 = sma(close, config.SMA_SHORT)
    sma_50 = sma(close, config.SMA_MEDIUM)
    sma_200 = sma(close, config.SMA_LONG)

    current_price = float(close.iloc[-1])
    current_rsi = float(rsi_series.iloc[-1])
    current_macd = float(macd_data["macd"].iloc[-1])
    current_signal = float(macd_data["signal"].iloc[-1])
    current_hist = float(macd_data["histogram"].iloc[-1])
    prev_hist = float(macd_data["histogram"].iloc[-2])

    # MACD bullish cross within last 5 bars
    macd_cross_recent = False
    for i in range(1, min(6, len(macd_data["histogram"]))):
        if macd_data["histogram"].iloc[-i] > 0 and macd_data["histogram"].iloc[-i - 1] < 0:
            macd_cross_recent = True
            break

    return {
        "price": current_price,
        "rsi": current_rsi,
        "rsi_zone": _classify_rsi(current_rsi),
        "macd": current_macd,
        "macd_signal": current_signal,
        "macd_histogram": current_hist,
        "macd_histogram_rising": current_hist > prev_hist,
        "macd_bullish_cross_5d": macd_cross_recent,
        "atr": float(atr_series.iloc[-1]),
        "sma_20": float(sma_20.iloc[-1]),
        "sma_50": float(sma_50.iloc[-1]),
        "sma_200": float(sma_200.iloc[-1]),
        "above_sma_20": current_price > float(sma_20.iloc[-1]),
        "above_sma_50": current_price > float(sma_50.iloc[-1]),
        "above_sma_200": current_price > float(sma_200.iloc[-1]),
        "volume_metrics": volume_metrics(df, lookback=20),
    }


def _classify_rsi(rsi_val: float) -> str:
    """Classify RSI into zones."""
    if rsi_val >= config.RSI_OVERBOUGHT:
        return "overbought"
    if rsi_val <= config.RSI_OVERSOLD:
        return "oversold"
    if rsi_val >= config.RSI_NEUTRAL_HIGH:
        return "chasing_risk"
    if rsi_val <= config.RSI_NEUTRAL_LOW:
        return "weak"
    return "neutral_bullish"  # 40-65 = ideal swing long zone


# ============================================================
# SMC: SWING DETECTION (foundation for all SMC patterns)
# ============================================================

def find_swing_points(df: pd.DataFrame, lookback: int = 5) -> Dict[str, List[int]]:
    """
    Find pivot highs and lows. A pivot is confirmed if `lookback` bars on each
    side are lower (for high) or higher (for low) than the pivot bar.
    Returns indices of swing points.
    """
    highs = df["high"].values
    lows = df["low"].values
    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(df) - lookback):
        # Pivot high check
        is_pivot_high = all(highs[i] >= highs[i - j] for j in range(1, lookback + 1)) and \
                        all(highs[i] >= highs[i + j] for j in range(1, lookback + 1))
        if is_pivot_high:
            swing_highs.append(i)

        # Pivot low check
        is_pivot_low = all(lows[i] <= lows[i - j] for j in range(1, lookback + 1)) and \
                       all(lows[i] <= lows[i + j] for j in range(1, lookback + 1))
        if is_pivot_low:
            swing_lows.append(i)

    return {"swing_highs": swing_highs, "swing_lows": swing_lows}


# ============================================================
# SMC: ORDER BLOCKS
# ============================================================

def find_order_blocks(df: pd.DataFrame, atr_series: pd.Series) -> List[Dict]:
    """
    Bullish Order Block = last DOWN candle before strong UP move.
    Marks zone where institutional buy orders likely sit.
    Returns active OBs (not yet invalidated).
    """
    obs = []
    lookback_bars = min(config.SMC_OB_LOOKBACK_BARS, len(df) - 5)
    min_move = config.SMC_OB_MIN_MOVE_ATR

    for i in range(len(df) - lookback_bars, len(df) - 3):
        if i < 1:
            continue
        # Look for impulsive UP move starting at i+1
        candle_open = df["open"].iloc[i + 1]
        impulse_high = df["high"].iloc[i + 1:i + 4].max()
        atr_val = atr_series.iloc[i] if not pd.isna(atr_series.iloc[i]) else 0
        if atr_val == 0:
            continue
        move_size = (impulse_high - candle_open) / atr_val
        if move_size < min_move:
            continue
        # Previous candle (i) must be bearish (close < open)
        if df["close"].iloc[i] >= df["open"].iloc[i]:
            continue
        # That bearish candle = bullish OB zone
        ob_high = float(df["high"].iloc[i])
        ob_low = float(df["low"].iloc[i])
        # Check if still valid (price hasn't broken below OB low)
        bars_since = len(df) - 1 - i
        if bars_since > config.SMC_OB_VALIDITY_BARS:
            continue
        subsequent_lows = df["low"].iloc[i + 1:].min()
        if subsequent_lows < ob_low:
            continue  # OB invalidated
        obs.append({
            "type": "bullish_OB",
            "bar_index": i,
            "high": ob_high,
            "low": ob_low,
            "mid": (ob_high + ob_low) / 2,
            "bars_since_formed": bars_since,
            "impulse_strength_atr": float(move_size),
        })
    return obs


# ============================================================
# SMC: FAIR VALUE GAPS (FVG)
# ============================================================

def find_fair_value_gaps(df: pd.DataFrame, atr_series: pd.Series) -> List[Dict]:
    """
    Bullish FVG = 3-candle pattern where candle 3's low > candle 1's high.
    The "gap" between them is unfilled liquidity that often acts as support.
    """
    fvgs = []
    lookback = min(config.SMC_FVG_LOOKBACK_BARS, len(df) - 3)
    min_size_atr = config.SMC_FVG_MIN_SIZE_ATR

    for i in range(len(df) - lookback, len(df) - 2):
        if i < 1:
            continue
        c1_high = df["high"].iloc[i]
        c3_low = df["low"].iloc[i + 2]
        if c3_low <= c1_high:
            continue  # no gap
        gap_size = c3_low - c1_high
        atr_val = atr_series.iloc[i + 2] if not pd.isna(atr_series.iloc[i + 2]) else 0
        if atr_val == 0:
            continue
        gap_atr = gap_size / atr_val
        if gap_atr < min_size_atr:
            continue
        # Check if FVG still unfilled (price hasn't dipped into the gap)
        subsequent_lows = df["low"].iloc[i + 3:].min() if i + 3 < len(df) else c3_low
        is_filled = subsequent_lows < c1_high
        fvgs.append({
            "type": "bullish_FVG",
            "bar_index": i + 1,  # middle candle
            "gap_low": float(c1_high),
            "gap_high": float(c3_low),
            "gap_mid": float((c1_high + c3_low) / 2),
            "size_atr": float(gap_atr),
            "is_filled": bool(is_filled),
            "bars_since_formed": len(df) - 1 - (i + 1),
        })
    # Return only unfilled ones (relevant for entries)
    return [f for f in fvgs if not f["is_filled"]]


# ============================================================
# SMC: BREAK OF STRUCTURE / CHANGE OF CHARACTER
# ============================================================

def detect_bos_choch(df: pd.DataFrame, swing_data: Dict) -> Dict:
    """
    BOS = Break of Structure: price breaks prior swing high (uptrend continuation).
    CHoCH = Change of Character: price breaks prior swing low (trend reversal).
    """
    swing_highs = swing_data["swing_highs"]
    swing_lows = swing_data["swing_lows"]
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bos_bullish": False, "choch_bearish": False}

    last_swing_high_idx = swing_highs[-1]
    last_swing_high = df["high"].iloc[last_swing_high_idx]
    last_swing_low_idx = swing_lows[-1]
    last_swing_low = df["low"].iloc[last_swing_low_idx]

    # Check for BOS: did price close above last swing high after it formed?
    bos_bullish = False
    bars_after_swing = df.iloc[last_swing_high_idx + 1:]
    if len(bars_after_swing) > 0:
        if config.SMC_BOS_CONFIRMATION_CLOSE:
            bos_bullish = bool((bars_after_swing["close"] > last_swing_high).any())
        else:
            bos_bullish = bool((bars_after_swing["high"] > last_swing_high).any())

    # CHoCH bearish: price closed below last swing low (only relevant if uptrend prior)
    choch_bearish = False
    bars_after_swing_low = df.iloc[last_swing_low_idx + 1:]
    if len(bars_after_swing_low) > 0:
        choch_bearish = bool((bars_after_swing_low["close"] < last_swing_low).any())

    return {
        "bos_bullish": bos_bullish,
        "choch_bearish": choch_bearish,
        "last_swing_high": float(last_swing_high),
        "last_swing_low": float(last_swing_low),
        "swing_high_bar_idx": last_swing_high_idx,
        "swing_low_bar_idx": last_swing_low_idx,
    }


# ============================================================
# SMC: LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(df: pd.DataFrame, swing_data: Dict, lookback: int = 10) -> Dict:
    """
    Liquidity sweep = wick spikes through prior swing low (grabbing stops)
    then closes back above it. Often precedes reversal up.
    """
    if len(swing_data["swing_lows"]) == 0:
        return {"liquidity_sweep_bullish": False}

    last_swing_low_idx = swing_data["swing_lows"][-1]
    last_swing_low = df["low"].iloc[last_swing_low_idx]

    # Check last `lookback` bars for sweep pattern
    recent = df.tail(lookback)
    sweep_detected = False
    sweep_bar_idx = None
    for i in range(len(recent)):
        bar = recent.iloc[i]
        bar_range = bar["high"] - bar["low"]
        if bar_range == 0:
            continue
        wick_size = (min(bar["open"], bar["close"]) - bar["low"])
        wick_pct = wick_size / bar_range
        if wick_pct >= config.SMC_LIQUIDITY_WICK_PCT and \
           bar["low"] < last_swing_low and \
           bar["close"] > last_swing_low:
            sweep_detected = True
            sweep_bar_idx = recent.index[i]
            break

    return {
        "liquidity_sweep_bullish": sweep_detected,
        "sweep_bar_idx": str(sweep_bar_idx) if sweep_bar_idx is not None else None,
        "swept_swing_low": float(last_swing_low),
    }


# ============================================================
# SMC: PREMIUM / DISCOUNT ZONES
# ============================================================

def classify_premium_discount(df: pd.DataFrame, swing_data: Dict) -> Dict:
    """
    Premium = price above 0.5 fib of recent swing range (avoid longs).
    Discount = price below 0.5 fib (favor longs).
    Uses last major swing high vs swing low.
    """
    if len(swing_data["swing_highs"]) == 0 or len(swing_data["swing_lows"]) == 0:
        return {"zone": "unknown"}

    last_high_idx = swing_data["swing_highs"][-1]
    last_low_idx = swing_data["swing_lows"][-1]

    # Use the most recent swing range
    swing_high = float(df["high"].iloc[last_high_idx])
    swing_low = float(df["low"].iloc[last_low_idx])

    if swing_high <= swing_low:
        return {"zone": "unknown"}

    fib_50 = (swing_high + swing_low) / 2
    current_price = float(df["close"].iloc[-1])

    if current_price >= fib_50:
        zone = "premium"
        recommendation = "avoid_longs"
    else:
        zone = "discount"
        recommendation = "favor_longs"

    return {
        "zone": zone,
        "recommendation": recommendation,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "fib_50_level": fib_50,
        "current_price": current_price,
        "fib_position_pct": float((current_price - swing_low) / (swing_high - swing_low) * 100),
    }


# ============================================================
# COMBINED SMC ANALYSIS
# ============================================================

def compute_smc_analysis(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all SMC pattern detection on a DataFrame."""
    if df is None or len(df) < 50:
        return {"error": "Insufficient data for SMC (need >= 50 bars)"}

    atr_series = atr(df, config.ATR_PERIOD)
    swing_data = find_swing_points(df, config.SMC_SWING_LOOKBACK)

    return {
        "swings": {
            "num_swing_highs": len(swing_data["swing_highs"]),
            "num_swing_lows": len(swing_data["swing_lows"]),
        },
        "order_blocks": find_order_blocks(df, atr_series),
        "fair_value_gaps": find_fair_value_gaps(df, atr_series),
        "structure": detect_bos_choch(df, swing_data),
        "liquidity_sweep": detect_liquidity_sweep(df, swing_data, config.SMC_LIQUIDITY_LOOKBACK),
        "zone": classify_premium_discount(df, swing_data),
    }


# ============================================================
# MULTI-TIMEFRAME ALIGNMENT
# ============================================================

def assess_mtf_alignment(tf_data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    Check trend alignment across timeframes.
    For LONG bias: 1W up + 1D up + 4H not in extreme oversold/overbought.
    """
    out = {}
    for tf_name in ["1wk", "1d", "4h", "1h"]:
        df = tf_data.get(tf_name)
        if df is None or len(df) < 50:
            out[tf_name] = {"trend": "unknown"}
            continue

        close = df["close"]
        sma_20_val = close.rolling(20).mean().iloc[-1]
        sma_50_val = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None
        current = float(close.iloc[-1])

        if sma_50_val is not None and not pd.isna(sma_50_val):
            if current > sma_20_val > sma_50_val:
                trend = "uptrend"
            elif current < sma_20_val < sma_50_val:
                trend = "downtrend"
            else:
                trend = "sideways"
        else:
            trend = "uptrend" if current > sma_20_val else "downtrend"

        out[tf_name] = {
            "trend": trend,
            "price": current,
            "sma_20": float(sma_20_val) if not pd.isna(sma_20_val) else None,
        }

    # Overall alignment score
    htf_trends = [out.get(tf, {}).get("trend") for tf in ["1wk", "1d"]]
    htf_aligned_bullish = all(t == "uptrend" for t in htf_trends if t != "unknown")
    out["htf_aligned_bullish"] = htf_aligned_bullish
    return out


# ============================================================
# FULL TECHNICAL ANALYSIS (called per ticker)
# ============================================================

def full_technical_analysis(tf_data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    One-shot full technical analysis using primary TF (1d) for indicators
    and SMC, plus multi-TF alignment check.
    """
    df_1d = tf_data.get(config.TF_PRIMARY)
    if df_1d is None or len(df_1d) < 200:
        return {"error": "No primary TF data"}

    return {
        "traditional": compute_traditional_indicators(df_1d),
        "smc": compute_smc_analysis(df_1d),
        "mtf_alignment": assess_mtf_alignment(tf_data),
    }


if __name__ == "__main__":
    # Quick test with NVDA
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from src.data_fetcher import fetch_multi_timeframe

    tf_data = fetch_multi_timeframe("NVDA")
    result = full_technical_analysis(tf_data)

    print("\n=== Traditional Indicators (NVDA, 1D) ===")
    for k, v in result.get("traditional", {}).items():
        if k != "volume_metrics":
            print(f"  {k}: {v}")
    print(f"  Volume: {result['traditional'].get('volume_metrics')}")

    print("\n=== SMC Analysis ===")
    smc = result.get("smc", {})
    print(f"  Order Blocks active: {len(smc.get('order_blocks', []))}")
    print(f"  Unfilled FVGs: {len(smc.get('fair_value_gaps', []))}")
    print(f"  Structure: {smc.get('structure')}")
    print(f"  Liquidity sweep: {smc.get('liquidity_sweep')}")
    print(f"  Zone: {smc.get('zone')}")

    print("\n=== Multi-TF Alignment ===")
    for tf, data in result.get("mtf_alignment", {}).items():
        if isinstance(data, dict):
            print(f"  {tf}: {data}")
