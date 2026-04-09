"""
strategies.py — Multi-timeframe signal engine.

Architecture:
  1. Individual strategy functions return a Signal object (direction + strength)
  2. Multi-TF analyzer checks entry TF, confirmation TF, and trend TF
  3. Weighted aggregator combines signals with regime-aware weights
  4. Only fires when MIN_CONFIRMATIONS are met
"""

import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass

import config

log = logging.getLogger("delta_bot")


@dataclass
class Signal:
    name: str
    direction: str | None   # "BUY", "SELL", or None
    strength: float          # 0.0 – 1.0
    detail: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
#  INDIVIDUAL STRATEGY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

# ── 1. Triple EMA Crossover (20 / 50 / 200) ──────────────────────────────────

def ema_crossover_signal(df: pd.DataFrame) -> Signal:
    close = df["close"].astype(float)
    ema_f = close.ewm(span=config.EMA_FAST, adjust=False).mean()
    ema_m = close.ewm(span=config.EMA_MID, adjust=False).mean()
    ema_s = close.ewm(span=config.EMA_SLOW, adjust=False).mean()

    price = float(close.iloc[-1])
    ef, em, es = float(ema_f.iloc[-1]), float(ema_m.iloc[-1]), float(ema_s.iloc[-1])

    # Perfect alignment
    if ef > em > es and price > ef:
        # BUY: fast > mid > slow and price above all
        # crossover check
        prev_ef, prev_em = float(ema_f.iloc[-2]), float(ema_m.iloc[-2])
        cross = prev_ef <= prev_em and ef > em
        strength = 1.0 if cross else 0.7
        return Signal("ema_crossover", "BUY", strength,
                       f"EMA aligned bullish, cross={cross}")

    if ef < em < es and price < ef:
        prev_ef, prev_em = float(ema_f.iloc[-2]), float(ema_m.iloc[-2])
        cross = prev_ef >= prev_em and ef < em
        strength = 1.0 if cross else 0.7
        return Signal("ema_crossover", "SELL", strength,
                       f"EMA aligned bearish, cross={cross}")

    # Partial alignment (weaker)
    if ef > em and price > em:
        return Signal("ema_crossover", "BUY", 0.4, "Partial bullish (fast>mid)")
    if ef < em and price < em:
        return Signal("ema_crossover", "SELL", 0.4, "Partial bearish (fast<mid)")

    return Signal("ema_crossover", None, 0.0, "No EMA alignment")


# ── 2. RSI ───────────────────────────────────────────────────────────────────

def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rsi_signal(df: pd.DataFrame) -> Signal:
    close = df["close"].astype(float)
    rsi = _compute_rsi(close, config.RSI_PERIOD)

    prev_rsi = float(rsi.iloc[-2]) if not np.isnan(rsi.iloc[-2]) else 50
    curr_rsi = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50

    # Strong signals: crossing thresholds
    if prev_rsi <= config.RSI_OVERSOLD and curr_rsi > config.RSI_OVERSOLD:
        return Signal("rsi", "BUY", 1.0,
                       f"RSI crossed above oversold ({curr_rsi:.1f})")
    if prev_rsi >= config.RSI_OVERBOUGHT and curr_rsi < config.RSI_OVERBOUGHT:
        return Signal("rsi", "SELL", 1.0,
                       f"RSI crossed below overbought ({curr_rsi:.1f})")

    # Moderate: in OB/OS zone
    if curr_rsi < config.RSI_OVERSOLD:
        return Signal("rsi", "BUY", 0.6, f"RSI oversold ({curr_rsi:.1f})")
    if curr_rsi > config.RSI_OVERBOUGHT:
        return Signal("rsi", "SELL", 0.6, f"RSI overbought ({curr_rsi:.1f})")

    return Signal("rsi", None, 0.0, f"RSI neutral ({curr_rsi:.1f})")


# ── 3. Bollinger Bands (squeeze/bounce) ──────────────────────────────────────

def bollinger_signal(df: pd.DataFrame) -> Signal:
    close = df["close"].astype(float)
    sma = close.rolling(config.BB_PERIOD).mean()
    std = close.rolling(config.BB_PERIOD).std()
    upper = sma + config.BB_STD_DEV * std
    lower = sma - config.BB_STD_DEV * std

    price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2])
    up = float(upper.iloc[-1])
    lo = float(lower.iloc[-1])
    prev_lo = float(lower.iloc[-2])
    prev_up = float(upper.iloc[-2])

    # Bounce off lower band
    if prev_price <= prev_lo and price > lo:
        return Signal("bollinger_bands", "BUY", 1.0,
                       f"BB bounce off lower band")

    # Rejection from upper band
    if prev_price >= prev_up and price < up:
        return Signal("bollinger_bands", "SELL", 1.0,
                       f"BB rejection from upper band")

    # Near lower band (potential bounce)
    bb_width = up - lo
    if bb_width > 0:
        position = (price - lo) / bb_width  # 0 = lower band, 1 = upper band
        if position < 0.15:
            return Signal("bollinger_bands", "BUY", 0.5,
                           f"Near lower BB ({position:.2f})")
        if position > 0.85:
            return Signal("bollinger_bands", "SELL", 0.5,
                           f"Near upper BB ({position:.2f})")

    return Signal("bollinger_bands", None, 0.0, "BB neutral")


# ── 4. MACD ──────────────────────────────────────────────────────────────────

def macd_signal(df: pd.DataFrame) -> Signal:
    close = df["close"].astype(float)
    ema_fast = close.ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    histogram = macd_line - signal_line

    prev_m = float(macd_line.iloc[-2])
    curr_m = float(macd_line.iloc[-1])
    prev_s = float(signal_line.iloc[-2])
    curr_s = float(signal_line.iloc[-1])
    hist = float(histogram.iloc[-1])
    prev_hist = float(histogram.iloc[-2])

    # Classic MACD crossover
    if prev_m <= prev_s and curr_m > curr_s:
        strength = 1.0 if hist > 0 and hist > prev_hist else 0.7
        return Signal("macd", "BUY", strength,
                       f"MACD cross up, hist={hist:.6f}")

    if prev_m >= prev_s and curr_m < curr_s:
        strength = 1.0 if hist < 0 and hist < prev_hist else 0.7
        return Signal("macd", "SELL", strength,
                       f"MACD cross down, hist={hist:.6f}")

    # Histogram momentum (weaker signal)
    if hist > 0 and hist > prev_hist and curr_m > curr_s:
        return Signal("macd", "BUY", 0.4, "MACD histogram expanding bullish")
    if hist < 0 and hist < prev_hist and curr_m < curr_s:
        return Signal("macd", "SELL", 0.4, "MACD histogram expanding bearish")

    return Signal("macd", None, 0.0, "MACD neutral")


# ── 5. Volume Breakout ──────────────────────────────────────────────────────

def volume_breakout_signal(df: pd.DataFrame) -> Signal:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    avg_vol = volume.rolling(20).mean()
    curr_vol = float(volume.iloc[-1])
    avg = float(avg_vol.iloc[-1]) if not np.isnan(avg_vol.iloc[-1]) else 0

    if avg <= 0:
        return Signal("volume_breakout", None, 0.0, "No volume data")

    vol_ratio = curr_vol / avg

    if vol_ratio < config.VOL_SPIKE_MULT:
        return Signal("volume_breakout", None, 0.0,
                       f"Vol ratio {vol_ratio:.1f}x (need {config.VOL_SPIKE_MULT}x)")

    # Volume spike detected — direction from price action
    price_change = float(close.iloc[-1]) - float(close.iloc[-2])

    strength = min(1.0, vol_ratio / (config.VOL_SPIKE_MULT * 2))
    if price_change > 0:
        return Signal("volume_breakout", "BUY", strength,
                       f"Volume spike {vol_ratio:.1f}x + bullish candle")
    elif price_change < 0:
        return Signal("volume_breakout", "SELL", strength,
                       f"Volume spike {vol_ratio:.1f}x + bearish candle")

    return Signal("volume_breakout", None, 0.0, "Volume spike but doji")


# ── 6. Market Structure (Higher Highs / Lower Lows) ─────────────────────────

def structure_signal(df: pd.DataFrame) -> Signal:
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    if len(df) < 20:
        return Signal("structure", None, 0.0, "Not enough data")

    # Find swing points (last 5 swing highs/lows using 5-bar pivot)
    pivot = 3
    swing_highs = []
    swing_lows = []

    for i in range(pivot, len(df) - pivot):
        h = float(high.iloc[i])
        l = float(low.iloc[i])
        if all(h >= float(high.iloc[i - j]) for j in range(1, pivot + 1)) and \
           all(h >= float(high.iloc[i + j]) for j in range(1, pivot + 1)):
            swing_highs.append(h)
        if all(l <= float(low.iloc[i - j]) for j in range(1, pivot + 1)) and \
           all(l <= float(low.iloc[i + j]) for j in range(1, pivot + 1)):
            swing_lows.append(l)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return Signal("structure", None, 0.0, "Not enough swing points")

    # Check last 2 swings
    hh = swing_highs[-1] > swing_highs[-2]  # Higher high
    hl = swing_lows[-1] > swing_lows[-2]     # Higher low
    lh = swing_highs[-1] < swing_highs[-2]   # Lower high
    ll = swing_lows[-1] < swing_lows[-2]     # Lower low

    if hh and hl:
        return Signal("structure", "BUY", 0.8,
                       f"HH+HL: {swing_highs[-2]:.2f}→{swing_highs[-1]:.2f}")
    if lh and ll:
        return Signal("structure", "SELL", 0.8,
                       f"LH+LL: {swing_lows[-2]:.2f}→{swing_lows[-1]:.2f}")
    if hh and not hl:
        return Signal("structure", "BUY", 0.4, "HH only (no HL confirmation)")
    if ll and not lh:
        return Signal("structure", "SELL", 0.4, "LL only (no LH confirmation)")

    return Signal("structure", None, 0.0, "No clear structure")


# ── 7. VWAP (Volume-Weighted Average Price) ──────────────────────────────────

def vwap_signal(df: pd.DataFrame) -> Signal:
    """
    Institutional-grade signal: price vs VWAP.
    - Price crossing above VWAP on volume = BUY
    - Price crossing below VWAP on volume = SELL
    - Distance from VWAP for strength scaling
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    if volume.sum() == 0:
        return Signal("vwap", None, 0.0, "No volume data for VWAP")

    # Rolling VWAP (using all available bars as session proxy)
    typical_price = (high + low + close) / 3.0
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    vwap = cum_tp_vol / cum_vol

    price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2])
    vwap_now = float(vwap.iloc[-1])
    vwap_prev = float(vwap.iloc[-2]) if not np.isnan(vwap.iloc[-2]) else vwap_now

    if np.isnan(vwap_now) or vwap_now == 0:
        return Signal("vwap", None, 0.0, "VWAP not calculable")

    # Distance from VWAP as percentage
    dist_pct = abs(price - vwap_now) / vwap_now * 100

    # Crossover detection
    crossed_above = prev_price <= vwap_prev and price > vwap_now
    crossed_below = prev_price >= vwap_prev and price < vwap_now

    # Volume confirmation
    avg_vol = float(volume.rolling(20).mean().iloc[-1])
    curr_vol = float(volume.iloc[-1])
    vol_ok = avg_vol > 0 and curr_vol > avg_vol * 1.2  # 20% above average

    if crossed_above:
        strength = 1.0 if vol_ok else 0.7
        return Signal("vwap", "BUY", strength,
                       f"Price crossed above VWAP, dist={dist_pct:.2f}%")

    if crossed_below:
        strength = 1.0 if vol_ok else 0.7
        return Signal("vwap", "SELL", strength,
                       f"Price crossed below VWAP, dist={dist_pct:.2f}%")

    # Sustained position (weaker signal)
    if price > vwap_now and dist_pct > 0.1:
        strength = min(0.5, dist_pct / 2.0)
        return Signal("vwap", "BUY", strength,
                       f"Above VWAP by {dist_pct:.2f}%")

    if price < vwap_now and dist_pct > 0.1:
        strength = min(0.5, dist_pct / 2.0)
        return Signal("vwap", "SELL", strength,
                       f"Below VWAP by {dist_pct:.2f}%")

    return Signal("vwap", None, 0.0, f"At VWAP (dist={dist_pct:.3f}%)")


# ── 8. Candle Patterns (Engulfing / Pin Bar) ─────────────────────────────────

def candle_pattern_signal(df: pd.DataFrame) -> Signal:
    """
    Detect engulfing candles and pin bars.
    - Bullish engulfing: red candle followed by larger green candle
    - Bearish engulfing: green candle followed by larger red candle
    - Pin bar: long wick (2x body), small body at one end
    """
    if len(df) < 3:
        return Signal("candle_pattern", None, 0.0, "Not enough data")

    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    # Current and previous candle
    c1_open, c1_high, c1_low, c1_close = (
        float(o.iloc[-1]), float(h.iloc[-1]),
        float(l.iloc[-1]), float(c.iloc[-1]))
    c0_open, c0_high, c0_low, c0_close = (
        float(o.iloc[-2]), float(h.iloc[-2]),
        float(l.iloc[-2]), float(c.iloc[-2]))

    body1 = abs(c1_close - c1_open)
    body0 = abs(c0_close - c0_open)
    range1 = c1_high - c1_low

    if range1 == 0:
        return Signal("candle_pattern", None, 0.0, "Doji candle")

    # ── Engulfing patterns ───────────────────────────────────
    # Bullish engulfing: prev was red, current is green and engulfs it
    if (c0_close < c0_open and c1_close > c1_open and
            c1_close > c0_open and c1_open <= c0_close and body1 > body0):
        return Signal("candle_pattern", "BUY", 0.8,
                       "Bullish engulfing")

    # Bearish engulfing: prev was green, current is red and engulfs it
    if (c0_close > c0_open and c1_close < c1_open and
            c1_close < c0_open and c1_open >= c0_close and body1 > body0):
        return Signal("candle_pattern", "SELL", 0.8,
                       "Bearish engulfing")

    # ── Pin bar patterns ─────────────────────────────────────
    upper_wick = c1_high - max(c1_open, c1_close)
    lower_wick = min(c1_open, c1_close) - c1_low
    body_ratio = body1 / range1 if range1 > 0 else 1

    # Bullish pin bar: long lower wick, small body at top
    if lower_wick > body1 * 2 and body_ratio < 0.35 and upper_wick < body1:
        return Signal("candle_pattern", "BUY", 0.7,
                       f"Bullish pin bar (wick={lower_wick/range1:.0%})")

    # Bearish pin bar: long upper wick, small body at bottom
    if upper_wick > body1 * 2 and body_ratio < 0.35 and lower_wick < body1:
        return Signal("candle_pattern", "SELL", 0.7,
                       f"Bearish pin bar (wick={upper_wick/range1:.0%})")

    return Signal("candle_pattern", None, 0.0, "No pattern")


# ═══════════════════════════════════════════════════════════════════════════════
#  MULTI-TIMEFRAME TREND CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def get_trend_bias(df_trend: pd.DataFrame) -> str | None:
    """
    Check higher-TF trend direction using EMA 50/200.
    Returns "BUY" (uptrend), "SELL" (downtrend), or None (no clear trend).
    """
    if df_trend is None or len(df_trend) < 50:
        return None

    close = df_trend["close"].astype(float)
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    price = float(close.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])

    if price > e50 > e200:
        return "BUY"
    if price < e50 < e200:
        return "SELL"
    return None


def check_confirmation_tf(df_confirm: pd.DataFrame, direction: str) -> bool:
    """
    Verify signal on the confirmation timeframe.
    Returns True if the confirmation TF agrees with the proposed direction.
    """
    if df_confirm is None or len(df_confirm) < 20:
        return True  # Can't confirm = pass through

    close = df_confirm["close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    rsi = _compute_rsi(close, 14)

    price = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    rsi_val = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50

    if direction == "BUY":
        return price > e20 and rsi_val < 75
    if direction == "SELL":
        return price < e20 and rsi_val > 25

    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  WEIGHTED AGGREGATOR
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_FUNCS = {
    "ema_crossover":   ema_crossover_signal,
    "rsi":             rsi_signal,
    "bollinger_bands": bollinger_signal,
    "macd":            macd_signal,
    "volume_breakout": volume_breakout_signal,
    "structure":       structure_signal,
    "vwap":            vwap_signal,
    "candle_pattern":  candle_pattern_signal,
}


def collect_signals(df_entry: pd.DataFrame,
                    regime_weights: dict | None = None) -> list[Signal]:
    """Run all enabled strategies on the entry timeframe."""
    signals = []
    for name, func in STRATEGY_FUNCS.items():
        if not config.STRATEGIES.get(name, False):
            continue
        try:
            sig = func(df_entry)
            signals.append(sig)
        except Exception as e:
            log.warning(f"  Strategy {name} error: {e}")
    return signals


def aggregate_signals(signals: list[Signal],
                      regime_weights: dict | None = None,
                      trend_bias: str | None = None,
                      confirm_ok: bool = True) -> dict:
    """
    Weighted aggregation with multi-TF filtering.

    Returns:
      direction     : "BUY" / "SELL" / None
      confirmations : int  (number agreeing)
      total_score   : float (weighted sum)
      details       : list[str]
    """
    if not signals:
        return {"direction": None, "confirmations": 0,
                "total_score": 0.0, "details": []}

    weights = regime_weights or {k: 1.0 for k in config.STRATEGIES}

    buy_score = 0.0
    sell_score = 0.0
    buy_count = 0
    sell_count = 0
    details = []

    for sig in signals:
        w = weights.get(sig.name, 1.0)
        contribution = sig.strength * w

        if sig.direction == "BUY":
            buy_score += contribution
            buy_count += 1
            details.append(f"  {sig.name:20s} → BUY  ({sig.strength:.1f} × {w:.1f}) {sig.detail}")
        elif sig.direction == "SELL":
            sell_score += contribution
            sell_count += 1
            details.append(f"  {sig.name:20s} → SELL ({sig.strength:.1f} × {w:.1f}) {sig.detail}")
        else:
            details.append(f"  {sig.name:20s} → HOLD  {sig.detail}")

    # Determine direction (raw)
    if buy_score > sell_score and buy_count >= 1:
        direction = "BUY"
        confirmations = buy_count
        total_score = buy_score
    elif sell_score > buy_score and sell_count >= 1:
        direction = "SELL"
        confirmations = sell_count
        total_score = sell_score
    else:
        return {
            "direction": None,
            "confirmations": 0,
            "total_score": 0.0,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "dominance": 0.0,
            "confidence": 0.0,
            "details": details,
        }

    # Conflict / edge checks (reduce noisy flips)
    eps = 1e-9
    raw_total = buy_score + sell_score
    score_diff = abs(buy_score - sell_score)
    dominance = score_diff / (raw_total + eps) if raw_total > 0 else 0.0
    edge_ratio = (total_score / (min(buy_score, sell_score) + eps)) if min(buy_score, sell_score) > 0 else 999.0

    min_dom = getattr(config, "MIN_SCORE_DOMINANCE", 0.28)
    min_edge_ratio = getattr(config, "MIN_SCORE_EDGE_RATIO", 1.25)  # winner must be >= 1.25× loser
    min_diff = getattr(config, "MIN_SCORE_DIFF", 0.75)

    if (buy_count > 0 and sell_count > 0) and dominance < min_dom:
        details.append(f"  BLOCKED: conflicting signals (dominance {dominance:.2f} < {min_dom})")
        return {
            "direction": None,
            "confirmations": confirmations,
            "total_score": total_score,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "dominance": dominance,
            "confidence": 0.0,
            "details": details,
        }

    if (buy_count > 0 and sell_count > 0) and (edge_ratio < min_edge_ratio) and (score_diff < min_diff):
        details.append(
            f"  BLOCKED: weak edge (diff {score_diff:.2f} < {min_diff} and edge {edge_ratio:.2f} < {min_edge_ratio})"
        )
        return {
            "direction": None,
            "confirmations": confirmations,
            "total_score": total_score,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "dominance": dominance,
            "confidence": 0.0,
            "details": details,
        }

    # Filter 1: minimum confirmations
    if confirmations < config.MIN_CONFIRMATIONS:
        details.append(f"  BLOCKED: {confirmations} confirmations < {config.MIN_CONFIRMATIONS} required")
        return {
            "direction": None,
            "confirmations": confirmations,
            "total_score": total_score,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "dominance": dominance,
            "confidence": 0.0,
            "details": details,
        }

    # Filter 2: trend alignment — penalise but don't hard-block
    if trend_bias is not None and direction != trend_bias:
        total_score *= 0.5   # Halve score for counter-trend
        details.append(f"  PENALTY: {direction} against trend ({trend_bias}) — score halved to {total_score:.2f}")

    # Filter 3: confirmation TF — penalise but don't hard-block
    if not confirm_ok:
        total_score *= 0.6
        details.append(f"  PENALTY: confirmation TF disagrees — score reduced to {total_score:.2f}")

    # Final gate: need minimum score after penalties
    min_score = config.MIN_SIGNAL_SCORE if hasattr(config, 'MIN_SIGNAL_SCORE') else 2.0
    if total_score < min_score:
        details.append(f"  BLOCKED: score {total_score:.2f} < {min_score} after penalties")
        return {
            "direction": None,
            "confirmations": confirmations,
            "total_score": total_score,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "dominance": dominance,
            "confidence": 0.0,
            "details": details,
        }

    # Rule-based confidence (0..1), used for SL/TP sizing without paid AI
    score_factor = min(1.0, total_score / (min_score * 1.6)) if min_score > 0 else 0.0
    conf_factor = min(1.0, confirmations / max(config.MIN_CONFIRMATIONS, 1))
    confidence = (0.45 * score_factor) + (0.35 * dominance) + (0.20 * conf_factor)
    if trend_bias is not None and direction != trend_bias:
        confidence *= 0.85
    if not confirm_ok:
        confidence *= 0.85
    confidence = max(0.0, min(0.95, confidence))

    details.append(f"  SIGNAL: {direction} (confirms={confirmations}, score={total_score:.2f}, conf={confidence:.2f})")
    return {
        "direction": direction,
        "confirmations": confirmations,
        "total_score": total_score,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "dominance": dominance,
        "confidence": confidence,
        "details": details,
    }
