"""
indicators.py — Technical indicator computation for SP500 Swing Agent.

STAGE 2 REVISION (May 2026):
============================
REMOVED:
  - All SMC logic (Order Blocks, FVGs, BOS, CHoCH, Liquidity Sweeps, Zones)
  - 4H / 1H multi-timeframe support (kept 1D only for swing)
  - Golden cross / death cross detection
  - EMA(200) and SMA helpers
  - ROC indicator

NEW MANDATORY FRAMEWORK (5 filters — ALL must pass):
  1. Price > EMA(50)
  2. ADX(14) > 25 AND +DI > -DI
  3. RSI(14) > 45
  4. Volume ratio (vs 14d avg, excluding today) > 1.3x
  5. ATR(14) as % of price > 1.5%

CONFLUENCE DISPLAY (informational, not gating):
  - MACD histogram > 0
  - Price > EMA(21)
  - Price > EMA(9)

SAFETY GUARDS:
  - Minimum 50 bars of data
  - Required OHLCV columns present
  - Partial-bar detection (rejects if vol_today < 30% of 14d avg)

All values calibrated to match TradingView rendering:
  - RSI / ATR / ADX use Wilder's smoothing (alpha = 1/period)
  - EMAs use standard span-based EMA
"""
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd


# ============================================================
# CONSTANTS
# ============================================================

PARTIAL_BAR_THRESHOLD = 0.30  # vol_today must be >= 30% of 14d avg
MIN_BARS_REQUIRED = 50


# ============================================================
# SMOOTHING HELPERS
# ============================================================

def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing — equivalent to TradingView for RSI/ATR/ADX."""
    return series.ewm(alpha=1 / period, adjust=False).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Standard EMA — for moving averages and MACD."""
    return series.ewm(span=period, adjust=False).mean()


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI with Wilder's smoothing (matches TradingView)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 14) -> pd.Series:
    """ATR with Wilder's smoothing."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _wilder(tr, period)


def compute_adx_di(high: pd.Series, low: pd.Series, close: pd.Series,
                   period: int = 14) -> Dict[str, pd.Series]:
    """
    ADX with +DI and -DI (Wilder's standard method).
    Returns dict with 'adx', 'plus_di', 'minus_di' series.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    atr_series = compute_atr(high, low, close, period)

    plus_di = 100 * _wilder(plus_dm, period) / atr_series.replace(0, np.nan)
    minus_di = 100 * _wilder(minus_dm, period) / atr_series.replace(0, np.nan)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = _wilder(dx, period)

    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> Dict[str, pd.Series]:
    """Standard MACD (12, 26, 9). Returns dict with 'macd', 'signal', 'histogram'."""
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


# ============================================================
# DATA VALIDATION
# ============================================================

def _validate_data(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Run all sanity checks on the DataFrame before computing indicators.
    Returns None if data is valid, or an error dict if invalid.
    """
    if df is None or len(df) < MIN_BARS_REQUIRED:
        return {
            "error": "insufficient_data",
            "bars": len(df) if df is not None else 0,
            "required": MIN_BARS_REQUIRED,
        }

    required_cols = {"Open", "High", "Low", "Close", "Volume"}
    if not required_cols.issubset(df.columns):
        return {
            "error": "missing_columns",
            "have": list(df.columns),
            "need": list(required_cols),
        }

    df_clean = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df_clean) < MIN_BARS_REQUIRED:
        return {
            "error": "insufficient_data_after_clean",
            "bars": len(df_clean),
            "required": MIN_BARS_REQUIRED,
        }

    # Partial-bar safety check
    volume = df_clean["Volume"]
    if len(volume) >= 15:
        vol_today = float(volume.iloc[-1])
        vol_avg_14 = float(volume.iloc[-15:-1].mean())
        if vol_avg_14 > 0 and vol_today < (vol_avg_14 * PARTIAL_BAR_THRESHOLD):
            return {
                "error": "possible_partial_bar",
                "reason": (
                    f"Today's volume ({int(vol_today):,}) is below "
                    f"{int(PARTIAL_BAR_THRESHOLD * 100)}% of 14d avg "
                    f"({int(vol_avg_14):,}). Likely incomplete trading day "
                    f"or stale data. Skipping to avoid false signal."
                ),
                "vol_today": int(vol_today),
                "vol_avg_14d": int(vol_avg_14),
                "ratio": round(vol_today / vol_avg_14, 3),
            }

    return None  # All checks passed


# ============================================================
# MAIN PIPELINE
# ============================================================

def compute_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Compute all indicators + run mandatory screener + confluence.

    Args:
        df: DataFrame with columns ['Open', 'High', 'Low', 'Close', 'Volume'],
            indexed by date, sorted ascending. Minimum 50 bars required.

    Returns:
        dict with keys:
          - 'price': latest close
          - 'indicators': all computed values
          - 'screener': pass/fail for each mandatory + 'all_pass' boolean
          - 'confluence': MACD/EMA9/EMA21 booleans + count + display string
          - 'error' (optional): if data is insufficient or invalid
    """
    # === Validation gate ===
    error = _validate_data(df)
    if error is not None:
        return error

    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    price = float(close.iloc[-1])

    # === EMAs ===
    ema_9 = float(_ema(close, 9).iloc[-1])
    ema_21 = float(_ema(close, 21).iloc[-1])
    ema_50 = float(_ema(close, 50).iloc[-1])

    # === RSI ===
    rsi_14 = float(compute_rsi(close, 14).iloc[-1])

    # === ATR ===
    atr_14 = float(compute_atr(high, low, close, 14).iloc[-1])
    atr_pct = (atr_14 / price) * 100 if price > 0 else 0.0

    # === ADX + DI ===
    adx_data = compute_adx_di(high, low, close, 14)
    adx_14 = float(adx_data["adx"].iloc[-1])
    plus_di_val = float(adx_data["plus_di"].iloc[-1])
    minus_di_val = float(adx_data["minus_di"].iloc[-1])

    # === MACD ===
    macd_data = compute_macd(close)
    macd_histogram = float(macd_data["histogram"].iloc[-1])

    # === Volume ratio (14d avg, EXCLUDING today) ===
    vol_avg_14 = float(volume.iloc[-15:-1].mean())
    vol_today = float(volume.iloc[-1])
    vol_ratio = vol_today / vol_avg_14 if vol_avg_14 > 0 else 0.0

    # ============================================================
    # MANDATORY SCREENER (5 filters — ALL must pass)
    # ============================================================
    pass_ema_50 = price > ema_50
    pass_adx_di = (adx_14 > 25) and (plus_di_val > minus_di_val)
    pass_rsi = rsi_14 > 45
    pass_volume = vol_ratio > 1.3
    pass_atr = atr_pct > 1.5

    all_pass = all([pass_ema_50, pass_adx_di, pass_rsi, pass_volume, pass_atr])

    # ============================================================
    # CONFLUENCE (informational — does NOT gate signal)
    # ============================================================
    conf_macd = macd_histogram > 0
    conf_ema_9 = price > ema_9
    conf_ema_21 = price > ema_21
    confluence_count = int(conf_macd) + int(conf_ema_9) + int(conf_ema_21)

    confluence_display = (
        f"{confluence_count}/3 ("
        f"MACD{'✓' if conf_macd else '✗'} "
        f"EMA9{'✓' if conf_ema_9 else '✗'} "
        f"EMA21{'✓' if conf_ema_21 else '✗'})"
    )

    return {
        "price": round(price, 2),
        "indicators": {
            "ema_9": round(ema_9, 2),
            "ema_21": round(ema_21, 2),
            "ema_50": round(ema_50, 2),
            "rsi_14": round(rsi_14, 2),
            "atr_14": round(atr_14, 2),
            "atr_pct": round(atr_pct, 2),
            "adx_14": round(adx_14, 2),
            "plus_di": round(plus_di_val, 2),
            "minus_di": round(minus_di_val, 2),
            "macd_histogram": round(macd_histogram, 4),
            "vol_ratio_14d": round(vol_ratio, 2),
            "vol_today": int(vol_today),
            "vol_avg_14d": int(vol_avg_14),
        },
        "screener": {
            "pass_ema_50": bool(pass_ema_50),
            "pass_adx_di": bool(pass_adx_di),
            "pass_rsi": bool(pass_rsi),
            "pass_volume": bool(pass_volume),
            "pass_atr": bool(pass_atr),
            "all_pass": bool(all_pass),
        },
        "confluence": {
            "macd_positive": bool(conf_macd),
            "above_ema_9": bool(conf_ema_9),
            "above_ema_21": bool(conf_ema_21),
            "count": confluence_count,
            "display": confluence_display,
        },
    }


def screen_passes(df: pd.DataFrame) -> bool:
    """Quick boolean: does this stock pass all 5 mandatory filters?"""
    result = compute_indicators(df)
    if "error" in result:
        return False
    return result["screener"]["all_pass"]


def build_brief_data(df: pd.DataFrame, ticker: str = "",
                     sector: str = "Unknown",
                     macro_regime: str = "unknown") -> Optional[Dict[str, Any]]:
    """
    Build a flat dict for Haiku filter input (no SMC fields).
    Returns None if data insufficient or partial-bar detected.

    Output structure organized by:
      - MANDATORY filters (5): EMA50, ADX+DI, RSI, Volume, ATR
        These ALL must pass for the signal to exist.
      - OPTIONAL confluence (3): EMA9, EMA21, MACD histogram
        These are informational — boost conviction but don't gate.
    """
    result = compute_indicators(df)
    if "error" in result:
        return None

    ind = result["indicators"]
    scr = result["screener"]
    conf = result["confluence"]

    return {
        "ticker": ticker,
        "sector": sector,
        "price": result["price"],

        # ===== MANDATORY FILTERS (must pass — gates signal) =====
        # 1. Trend anchor: Price > EMA(50)
        "ema_50": ind["ema_50"],
        "above_ema_50": scr["pass_ema_50"],
        # 2. Trend strength: ADX(14) > 25 AND +DI > -DI
        "adx_14": ind["adx_14"],
        "plus_di": ind["plus_di"],
        "minus_di": ind["minus_di"],
        "adx_bullish": scr["pass_adx_di"],
        # 3. Momentum floor: RSI(14) > 45
        "rsi_14": ind["rsi_14"],
        # 4. Volume confirmation: ratio > 1.3x
        "vol_ratio_14d": ind["vol_ratio_14d"],
        "vol_today": ind["vol_today"],
        "vol_avg_14d": ind["vol_avg_14d"],
        "vol_surge": scr["pass_volume"],
        # 5. Volatility floor: ATR > 1.5% of price
        "atr_14": ind["atr_14"],
        "atr_pct": ind["atr_pct"],

        # ===== OPTIONAL CONFLUENCE (informational — boosts conviction, not gating) =====
        "ema_9": ind["ema_9"],
        "ema_21": ind["ema_21"],
        "above_ema_21": conf["above_ema_21"],
        "above_ema_9": conf["above_ema_9"],
        "macd_histogram": ind["macd_histogram"],
        "macd_positive": conf["macd_positive"],

        # ===== AGGREGATE STATUS =====
        "all_mandatory_pass": scr["all_pass"],
        "confluence_count": conf["count"],
        "confluence_display": conf["display"],
        "macro_regime": macro_regime,
    }


# ============================================================
# SMOKE TEST
# ============================================================

if __name__ == "__main__":
    import yfinance as yf

    print("=== Indicators Smoke Test ===\n")
    for ticker in ["NVDA", "AMD", "TSLA"]:
        df = yf.download(ticker, period="6mo", interval="1d",
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        result = compute_indicators(df)
        if "error" in result:
            print(f"{ticker}: ERROR — {result}")
            continue

        print(f"--- {ticker} (${result['price']}) ---")
        print(f"Indicators: {result['indicators']}")
        print(f"Screener: {result['screener']}")
        print(f"Confluence: {result['confluence']['display']}")
        print(f"PASSES SCREEN: {result['screener']['all_pass']}\n")
