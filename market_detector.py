"""
market_detector.py — Detects market regime and adapts strategy.

Regimes:
  TRENDING_UP   — Strong uptrend (ADX>25, price > EMA200, higher highs)
  TRENDING_DOWN — Strong downtrend (ADX>25, price < EMA200, lower lows)
  RANGING       — Sideways (ADX<25, tight BB, no clear direction)
  VOLATILE      — High volatility / news event (ATR spike)
  LOW_LIQUIDITY — Thin orderbook, wide spreads (skip trading)
"""

import logging
import numpy as np
import pandas as pd
import config

log = logging.getLogger("delta_bot")


class MarketRegime:
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_bb_width(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> pd.Series:
    """Bollinger Band width as % of price."""
    close = df["close"].astype(float)
    sma = close.rolling(period).mean()
    band_std = close.rolling(period).std()
    upper = sma + std * band_std
    lower = sma - std * band_std
    return (upper - lower) / sma * 100


def detect_regime(df: pd.DataFrame) -> dict:
    """
    Analyse a DataFrame and return the current market regime.

    Returns dict:
      regime     : str (MarketRegime constant)
      adx        : float
      atr_pct    : float (ATR as % of price)
      bb_width   : float
      trend_dir  : "UP" / "DOWN" / "NEUTRAL"
      confidence : float 0-100
    """
    if df is None or len(df) < 50:
        return {
            "regime": MarketRegime.LOW_LIQUIDITY,
            "adx": 0, "atr_pct": 0, "bb_width": 0,
            "trend_dir": "NEUTRAL", "confidence": 0,
        }

    close = df["close"].astype(float)
    price = float(close.iloc[-1])

    # Indicators
    adx = compute_adx(df, config.ADX_PERIOD)
    atr = compute_atr(df, config.ATR_PERIOD)
    bb_w = compute_bb_width(df, config.BB_PERIOD, config.BB_STD_DEV)

    adx_val = float(adx.iloc[-1]) if not np.isnan(adx.iloc[-1]) else 0
    atr_val = float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0
    atr_pct = (atr_val / price * 100) if price > 0 else 0
    bb_val = float(bb_w.iloc[-1]) if not np.isnan(bb_w.iloc[-1]) else 0

    # EMA for trend direction
    ema200 = close.ewm(span=200, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    above_ema200 = price > float(ema200.iloc[-1])
    above_ema50 = price > float(ema50.iloc[-1])

    # Higher highs / lower lows (last 20 bars)
    recent_highs = df["high"].astype(float).tail(20)
    recent_lows = df["low"].astype(float).tail(20)
    hh = float(recent_highs.iloc[-1]) > float(recent_highs.iloc[-10])
    ll = float(recent_lows.iloc[-1]) < float(recent_lows.iloc[-10])

    # Determine trend direction
    if above_ema200 and above_ema50:
        trend_dir = "UP"
    elif not above_ema200 and not above_ema50:
        trend_dir = "DOWN"
    else:
        trend_dir = "NEUTRAL"

    # Regime classification
    confidence = 0
    if atr_pct >= config.VOLATILITY_HIGH_ATR:
        regime = MarketRegime.VOLATILE
        confidence = min(100, atr_pct / config.VOLATILITY_HIGH_ATR * 80)
    elif adx_val >= config.ADX_TREND_THRESH:
        if trend_dir == "UP" or hh:
            regime = MarketRegime.TRENDING_UP
        elif trend_dir == "DOWN" or ll:
            regime = MarketRegime.TRENDING_DOWN
        else:
            regime = MarketRegime.TRENDING_UP if above_ema50 else MarketRegime.TRENDING_DOWN
        confidence = min(100, adx_val / 50 * 100)
    elif bb_val < config.BB_SQUEEZE_THRESH * 100:
        regime = MarketRegime.RANGING
        confidence = 60
    else:
        regime = MarketRegime.RANGING
        confidence = 40

    return {
        "regime": regime,
        "adx": round(adx_val, 2),
        "atr_pct": round(atr_pct, 4),
        "atr_value": round(atr_val, 6),
        "bb_width": round(bb_val, 3),
        "trend_dir": trend_dir,
        "confidence": round(confidence, 1),
    }


def should_trade_in_regime(regime_info: dict) -> tuple[bool, str]:
    """Decide whether to trade given the detected regime."""
    regime = regime_info["regime"]

    if regime == MarketRegime.LOW_LIQUIDITY:
        return False, "Low liquidity — skipping"

    if regime == MarketRegime.VOLATILE and regime_info["atr_pct"] > config.VOLATILITY_HIGH_ATR * 2:
        return False, f"Extreme volatility (ATR={regime_info['atr_pct']:.1f}%) — too risky"

    if regime == MarketRegime.RANGING and regime_info["confidence"] > 70:
        return True, f"Range-bound (BB={regime_info['bb_width']:.2f}%) — use mean reversion"

    if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
        return True, f"Trending {regime_info['trend_dir']} (ADX={regime_info['adx']:.1f}) — use trend following"

    return True, f"Regime: {regime} — proceeding with caution"


def get_regime_strategy_bias(regime_info: dict) -> dict:
    """
    Returns strategy weight adjustments based on regime.
    Strategies that match the regime get higher weight.
    """
    regime = regime_info["regime"]

    if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
        return {
            "ema_crossover": 2.0,     # Strong in trends
            "macd": 1.5,              # Good for momentum
            "rsi": 0.5,               # Less useful in trends (stays OB/OS)
            "bollinger_bands": 0.5,   # Mean-reversion less reliable
            "volume_breakout": 1.5,
            "structure": 2.0,
            "vwap": 1.5,              # VWAP useful in trends
            "candle_pattern": 1.5,    # Patterns confirm reversals/continuations
        }

    if regime == MarketRegime.RANGING:
        return {
            "ema_crossover": 0.3,     # Choppy = false MA signals
            "macd": 0.5,
            "rsi": 2.0,               # Great for overbought/oversold bounce
            "bollinger_bands": 2.0,   # BB bounce works in ranges
            "volume_breakout": 1.0,
            "structure": 0.5,
            "vwap": 1.5,              # VWAP acts as magnet in ranges
            "candle_pattern": 2.0,    # Patterns strong at range boundaries
        }

    if regime == MarketRegime.VOLATILE:
        return {
            "ema_crossover": 0.5,
            "macd": 1.0,
            "rsi": 1.0,
            "bollinger_bands": 1.5,
            "volume_breakout": 2.0,   # Volume spikes matter
            "structure": 1.0,
            "vwap": 1.0,              # Neutral in volatility
            "candle_pattern": 1.0,    # Neutral in volatility
        }

    # Default: equal weights
    return {k: 1.0 for k in config.STRATEGIES}
