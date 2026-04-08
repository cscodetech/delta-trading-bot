# ============================================================
#  DELTA EXCHANGE TRADING BOT v3 — PROFESSIONAL CONFIGURATION
#  Designed by a quant: capital preservation > speculation
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── API CREDENTIALS (loaded from .env) ───────────────────────
API_KEY    = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

# ── EXCHANGE FEES ────────────────────────────────────────────
TAKER_FEE_PCT  = 0.05         # 0.05% taker fee (Delta Exchange)
MAKER_FEE_PCT  = 0.02         # 0.02% maker fee
SLIPPAGE_PCT   = 0.03         # Estimated slippage per trade

# ── MODE ─────────────────────────────────────────────────────
TESTNET       = True           # True = testnet, False = real money
PAPER_TRADING = False          # True = simulate orders (no real execution)

# ── AUTO-SCAN SETTINGS ──────────────────────────────────────
AUTO_SCAN          = True
SCAN_EVERY_N_TICKS = 3        # Re-scan every N ticks (when all slots full)
MIN_VOLUME_USD     = 0        # 0 for testnet; set 500_000+ for live
TOP_N_BY_VOLUME    = 15
SYMBOL             = "BTCUSD" # Fallback if AUTO_SCAN = False

# ── MULTI-TIMEFRAME ─────────────────────────────────────────
TF_ENTRY        = "15m"       # Entry precision (was 5m — less noise)
TF_CONFIRM      = "1h"        # Signal confirmation (was 15m)
TF_TREND        = "4h"        # Trend direction (was 1h — bigger picture)
CANDLE_LIMIT    = 200         # Candles to fetch per timeframe

# ── RISK MANAGEMENT (CRITICAL) ──────────────────────────────
RISK_PER_TRADE_PCT   = 1.0    # Max 1% of capital risked per trade
DAILY_LOSS_LIMIT_PCT = 3.0    # Stop trading if daily loss exceeds 3%
MAX_DRAWDOWN_PCT     = 10.0   # Kill switch if drawdown from peak > 10%
MAX_TRADES_PER_DAY   = 5      # No revenge trading
MAX_OPEN_TRADES      = 3      # Concurrent positions (multi-symbol)
MIN_CONFIRMATIONS    = 2      # Signals needed before entry (out of max ~6)
COOLDOWN_AFTER_LOSS  = 2      # Skip N ticks after a losing trade

# ── POSITION SIZING ─────────────────────────────────────────
BASE_QTY              = 1     # Minimum contract size
DYNAMIC_SIZING        = True  # Scale size with ATR / account balance
REDUCE_AFTER_LOSSES   = True  # Halve size after 2 consecutive losses
COMPOUND_WINS         = True  # Increase size after 3 consecutive wins

# ── STOP LOSS & TAKE PROFIT ─────────────────────────────────
SL_MODE          = "atr"      # "fixed" or "atr"
SL_FIXED_PCT     = 1.5        # Used if SL_MODE = "fixed"
SL_ATR_MULT      = 1.5        # SL = ATR(14) * this multiplier
TP_MODE          = "atr"      # "fixed" or "atr"
TP_FIXED_PCT     = 3.0        # Used if TP_MODE = "fixed"
TP_ATR_MULT      = 4.0        # TP = ATR(14) * this multiplier (was 3.0 — wider R:R)
TRAILING_STOP    = True        # Enable trailing stop
TRAIL_ATR_MULT   = 0.75       # Trail distance = ATR * this (was 1.0 — tighter lock)
TRAILING_ATR_MULT = TRAIL_ATR_MULT  # Alias used by bot.py
PARTIAL_TP       = True        # Take partial profit
PARTIAL_TP_PCT   = 50         # Close 50% at first TP
TIME_EXIT_BARS   = 50         # Exit if no movement after N candles (was 20 — let winners run)

# ── STRATEGY ENGINE ──────────────────────────────────────────
STRATEGIES = {
    "ema_crossover":   True,   # EMA 20/50/200 system
    "rsi":             True,   # RSI overbought/oversold
    "bollinger_bands": True,   # BB squeeze/bounce
    "macd":            True,   # MACD cross + divergence
    "volume_breakout": True,   # Volume spike confirmation
    "structure":       True,   # Higher-high / lower-low
    "vwap":            True,   # VWAP institutional-grade
    "candle_pattern":  True,   # Engulfing / Pin bar patterns
}

# EMA Crossover (triple)
EMA_FAST   = 20
EMA_MID    = 50
EMA_SLOW   = 200

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70

# Bollinger Bands
BB_PERIOD  = 20
BB_STD_DEV = 2.0

# MACD
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# ATR (used everywhere)
ATR_PERIOD = 14

# Volume
VOL_SPIKE_MULT = 1.5          # Volume must be > 1.5x average

# ── MARKET REGIME DETECTION ─────────────────────────────────
ADX_PERIOD       = 14
ADX_TREND_THRESH = 25         # ADX > 25 = trending market
BB_SQUEEZE_THRESH = 0.02      # BB width < 2% of price = squeeze/range
VOLATILITY_HIGH_ATR = 3.0     # ATR% > 3 = high volatility regime

# ── ENTRY QUALITY FILTERS ───────────────────────────────────
MIN_ATR_PCT         = 0.15    # Skip if ATR% < this (fees eat profit)
MIN_ADX_ENTRY       = 20      # Skip if ADX < this (no trend = chop)
MIN_SIGNAL_SCORE    = 2.0     # Minimum weighted score after penalties
SYMBOL_COOLDOWN     = 5       # Ticks to wait before re-entering same symbol
BACKTEST_BEFORE_LIVE = True   # Run quick backtest before live entry
BACKTEST_MIN_WINRATE = 40     # Minimum backtest win rate % to allow entry

# ── CORRELATION GROUPS (don't open >1 in same group) ────────
CORRELATION_GROUPS = [
    ["BTCUSD", "ETHUSD"],                        # Major crypto — move together
    ["ADAUSD", "XRPUSD", "SOLUSD", "ONDOUSD"],  # Alt-coins — high correlation
    ["DOGEUSD", "1000SHIBUSD"],                   # Meme coins
]
MAX_PER_CORR_GROUP = 1        # Max positions per correlation group

# ── SESSION TIME FILTER ─────────────────────────────────────
SESSION_FILTER       = True   # Only trade during active hours
SESSION_START_UTC    = 8      # Start hour UTC (8:00 = EU open)
SESSION_END_UTC      = 22     # End hour UTC (22:00 = US close)

# ── ACCELERATING TRAIL ──────────────────────────────────────
ACCEL_TRAIL          = True   # Tighten trail as profit grows
ACCEL_TRAIL_2R       = 0.5    # Trail ATR mult after 2R profit
ACCEL_TRAIL_3R       = 0.3    # Trail ATR mult after 3R profit

# ── ORDER TYPE ──────────────────────────────────────────────
USE_LIMIT_ORDERS     = True   # Use limit orders (maker fee) instead of market
LIMIT_OFFSET_PCT     = 0.02   # Place limit this % inside the spread

# ── STRATEGY PERFORMANCE TRACKER ────────────────────────────
STRATEGY_TRACKER     = True   # Track per-strategy win rate
STRATEGY_MIN_TRADES  = 10     # Min trades before auto-disable
STRATEGY_MIN_WINRATE = 30     # Auto-disable if WR% < this

# ── DYNAMIC REGIME SIZING ───────────────────────────────────
DYNAMIC_MAX_POS      = True   # Adjust max positions by regime
MAX_POS_VOLATILE     = 1      # Only 1 position in VOLATILE regime
MAX_POS_RANGING      = 2      # Max 2 in RANGING
MAX_POS_TRENDING     = 3      # Full capacity in TRENDING

# ── CANDLE PATTERNS ─────────────────────────────────────────
CANDLE_PATTERNS      = True   # Add engulfing/pin-bar confirmation

# ── TELEGRAM ALERTS ──────────────────────────────────────────
TELEGRAM_ENABLED  = bool(os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

# ── DASHBOARD AUTH ───────────────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin123")
DASHBOARD_SECRET   = os.getenv("DASHBOARD_SECRET", "change-me")

# ── LOOP TIMING ──────────────────────────────────────────────
POLL_INTERVAL_SEC = 60        # Seconds between ticks
API_RETRY_COUNT   = 3         # Retry failed API calls
API_RETRY_DELAY   = 2         # Seconds between retries

# ── DASHBOARD ────────────────────────────────────────────────
DASHBOARD_PORT    = 5050      # Web dashboard port
DASHBOARD_HOST    = "0.0.0.0" # Bind address (0.0.0.0 = all interfaces)


# ═════════════════════════════════════════════════════════════
#  OVERRIDE FROM DATABASE — settings saved via dashboard
#  DB values take priority over .env values
# ═════════════════════════════════════════════════════════════

def _load_db_settings():
    """Load settings from the database and override module globals."""
    try:
        import database as _db
        _overrides = {
            "api_key":            "API_KEY",
            "api_secret":         "API_SECRET",
            "telegram_token":     "TELEGRAM_TOKEN",
            "telegram_chat_id":   "TELEGRAM_CHAT_ID",
            "dashboard_password": "DASHBOARD_PASSWORD",
        }
        g = globals()
        for db_key, cfg_key in _overrides.items():
            val = _db.get_setting(db_key)
            if val:  # Only override if DB has a non-empty value
                g[cfg_key] = val

        # Testnet is stored as "1"/"0"
        testnet_val = _db.get_setting("testnet")
        if testnet_val:
            g["TESTNET"] = testnet_val == "1"

        # Re-derive TELEGRAM_ENABLED
        g["TELEGRAM_ENABLED"] = bool(g.get("TELEGRAM_TOKEN", ""))

    except Exception:
        pass  # DB not available yet — use .env defaults

_load_db_settings()
