"""
bot.py — v3 Professional Trading Bot Orchestrator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Architecture:
  Exchange ──→ Scanner ──→ Market Detector ──→ Strategy Engine ──→ Risk Manager ──→ Execution
                                                    ↓
                                              Multi-Timeframe
                                            (Entry / Confirm / Trend)
                                                    ↓
                                              Weighted Aggregation
                                                    ↓
                                               Alerts (Telegram)

Run: python bot.py
"""

import time
import logging
from datetime import datetime, date, timezone

import config
import database as db
from exchange import DeltaClient
from scanner import find_best_symbol, find_all_tradeable
from market_detector import (
    detect_regime, should_trade_in_regime,
    get_regime_strategy_bias, compute_atr, MarketRegime,
)
from strategies import (
    collect_signals, aggregate_signals,
    get_trend_bias, check_confirmation_tf,
)
from risk_manager import RiskManager, TradeRecord
import alerts

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(open(1, 'w', encoding='utf-8', closefd=False)),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("delta_bot")


# ── Position State ────────────────────────────────────────────────────────────

class Position:
    """Tracks the current open position with SL/TP/trailing."""

    def __init__(self):
        self.active: bool = False
        self.side: str = ""         # "BUY" or "SELL"
        self.entry_price: float = 0.0
        self.size: int = 0
        self.product_id: int = 0
        self.symbol: str = ""
        self.sl_price: float = 0.0
        self.tp_price: float = 0.0
        self.trail_distance: float = 0.0
        self.trail_price: float = 0.0
        self.entry_tick: int = 0
        self.partial_taken: bool = False
        self.order_id: int = 0
        self.regime: str = ""
        self.confirmations: int = 0
        self.strategies_used: list[str] = []

    def open(self, side: str, price: float, size: int, product_id: int,
             symbol: str, sl_tp: dict, tick: int):
        self.active = True
        self.side = side
        self.entry_price = price
        self.size = size
        self.product_id = product_id
        self.symbol = symbol
        self.sl_price = sl_tp["sl_price"]
        self.tp_price = sl_tp["tp_price"]
        self.trail_distance = sl_tp["trail_distance"]
        self.entry_tick = tick
        self.partial_taken = False
        self.order_id = 0
        # Init trailing stop
        if side == "BUY":
            self.trail_price = price - self.trail_distance
        else:
            self.trail_price = price + self.trail_distance

    def close(self):
        self.active = False
        self.side = ""
        self.entry_price = 0.0
        self.size = 0

    def pnl_pct(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - current_price) / self.entry_price * 100

    def update_trailing_stop(self, current_price: float):
        if not config.TRAILING_STOP or self.trail_distance <= 0:
            return

        # ── Accelerating trail: tighten at 2R / 3R profit ───
        effective_distance = self.trail_distance
        if getattr(config, 'ACCEL_TRAIL', False) and self.entry_price > 0:
            risk_1r = abs(self.entry_price - self.sl_price) or self.trail_distance
            profit = abs(current_price - self.entry_price)
            if profit >= 3 * risk_1r:
                effective_distance = self.trail_distance * getattr(
                    config, 'ACCEL_TRAIL_3R', 0.3)
            elif profit >= 2 * risk_1r:
                effective_distance = self.trail_distance * getattr(
                    config, 'ACCEL_TRAIL_2R', 0.5)

        if self.side == "BUY":
            new_trail = current_price - effective_distance
            if new_trail > self.trail_price:
                self.trail_price = new_trail
        else:
            new_trail = current_price + effective_distance
            if new_trail < self.trail_price:
                self.trail_price = new_trail


# ── Bot Core ──────────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        self.client = DeltaClient(config.API_KEY, config.API_SECRET,
                                  testnet=config.TESTNET)
        self.risk = RiskManager()
        self.positions: dict[str, Position] = {}  # symbol -> Position
        self.tick = 0
        self.last_report_date: date | None = None
        self.symbol_cooldowns: dict[str, int] = {}  # symbol -> tick when cooldown expires

    # ── Initialisation ───────────────────────────────────────

    def init(self):
        """Fetch wallet balance, load existing positions, initialise risk."""
        log.info("Fetching wallet balance...")
        balance = self.client.get_wallet_balance()
        if balance <= 0:
            log.warning("Could not fetch wallet balance — using $10,000 default")
            balance = 10000.0
        self.risk.init_balance(balance)

        # ── Load existing positions from exchange ────────────
        self._discover_exchange_positions()

        active_strats = [k for k, v in config.STRATEGIES.items() if v]
        mode = "AUTO-SCAN" if config.AUTO_SCAN else config.SYMBOL
        alerts.alert_bot_start(mode, active_strats)

    def _discover_exchange_positions(self):
        """Detect positions already open on the exchange and track them."""
        try:
            exchange_positions = self.client.get_positions()
            # Build product_id -> symbol map
            products = self.client.get_products()
            pid_to_sym = {p["id"]: p["symbol"] for p in products}

            for ep in exchange_positions:
                size = int(ep.get("size", 0))
                if abs(size) == 0:
                    continue
                pid = ep.get("product_id", 0)
                symbol = pid_to_sym.get(pid, ep.get("symbol", f"PID_{pid}"))
                entry_price = float(ep.get("entry_price", 0))
                side = "BUY" if size > 0 else "SELL"

                if symbol in self.positions and self.positions[symbol].active:
                    continue  # Already tracked

                log.info(f"  Found existing position: {side} {symbol} "
                         f"size={abs(size)} entry={entry_price}")
                pos = Position()
                pos.active = True
                pos.side = side
                pos.entry_price = entry_price
                pos.size = abs(size)
                pos.product_id = pid
                pos.symbol = symbol
                # Set conservative SL/TP based on current entry
                atr_est = entry_price * 0.005  # 0.5% estimate
                if side == "BUY":
                    pos.sl_price = entry_price * (1 - config.SL_ATR_MULT * 0.005)
                    pos.tp_price = entry_price * (1 + config.TP_ATR_MULT * 0.005)
                    pos.trail_price = entry_price - atr_est * config.TRAILING_ATR_MULT
                else:
                    pos.sl_price = entry_price * (1 + config.SL_ATR_MULT * 0.005)
                    pos.tp_price = entry_price * (1 - config.TP_ATR_MULT * 0.005)
                    pos.trail_price = entry_price + atr_est * config.TRAILING_ATR_MULT
                pos.trail_distance = atr_est * config.TRAILING_ATR_MULT
                pos.entry_tick = self.tick
                self.positions[symbol] = pos

            if self.positions:
                log.info(f"  Loaded {len(self.positions)} existing position(s): "
                         f"{list(self.positions.keys())}")
            else:
                log.info("  No existing positions on exchange.")
        except Exception as e:
            log.warning(f"  Failed to load exchange positions: {e}")

    # ── Scanner ──────────────────────────────────────────────

    def get_open_symbols(self) -> set[str]:
        """Symbols with active positions."""
        return {sym for sym, pos in self.positions.items() if pos.active}

    def _is_correlated_blocked(self, symbol: str) -> bool:
        """Check if we already have a position in the same correlation group."""
        open_syms = self.get_open_symbols()
        if not open_syms:
            return False
        for group in getattr(config, 'CORRELATION_GROUPS', []):
            if symbol not in group:
                continue
            max_per = getattr(config, 'MAX_PER_CORR_GROUP', 1)
            count = sum(1 for s in open_syms if s in group)
            if count >= max_per:
                log.info(f"  BLOCKED: {symbol} correlated with "
                         f"{[s for s in open_syms if s in group]} "
                         f"(max {max_per} per group)")
                return True
        return False

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if symbol is on cooldown after a recent close."""
        if symbol in self.symbol_cooldowns:
            if self.tick < self.symbol_cooldowns[symbol]:
                remaining = self.symbol_cooldowns[symbol] - self.tick
                log.info(f"  BLOCKED: {symbol} on cooldown "
                         f"({remaining} ticks remaining)")
                return True
            else:
                del self.symbol_cooldowns[symbol]
        return False

    def scan_candidates(self) -> list[dict]:
        """Scan all symbols, return those with signals (excluding already-open)."""
        if not config.AUTO_SCAN:
            sym = config.SYMBOL
            pid = self.client.get_product_id(sym)
            return [{"symbol": sym, "product_id": pid, "direction": "BUY",
                     "total_score": 50}]

        candidates = find_all_tradeable(self.client, config.TF_ENTRY)
        open_syms = self.get_open_symbols()
        # Filter out symbols we already have a position in
        return [c for c in candidates if c["symbol"] not in open_syms]

    # ── Multi-Timeframe Data ─────────────────────────────────

    def _fetch_candles(self, symbol: str, tf: str,
                       limit: int = None) -> object:
        """Fetch candles; return None on failure."""
        try:
            return self.client.get_candles(
                symbol, tf, limit=limit or config.CANDLE_LIMIT)
        except Exception as e:
            log.warning(f"  Failed to fetch {tf} candles for {symbol}: {e}")
            return None

    # ── Exit Logic ───────────────────────────────────────────

    def check_exits(self, pos: Position, current_price: float) -> str | None:
        """
        Check all exit conditions for a specific position.
        Priority: SL > trailing > TP (partial/full) > time > signal flip
        """
        if not pos.active:
            return None

        pnl = pos.pnl_pct(current_price)

        # 1. Stop Loss
        if pos.side == "BUY" and current_price <= pos.sl_price:
            return f"Stop Loss (PnL: {pnl:+.2f}%)"
        if pos.side == "SELL" and current_price >= pos.sl_price:
            return f"Stop Loss (PnL: {pnl:+.2f}%)"

        # 2. Trailing Stop
        if config.TRAILING_STOP:
            pos.update_trailing_stop(current_price)
            if pos.side == "BUY" and current_price <= pos.trail_price:
                return f"Trailing Stop (PnL: {pnl:+.2f}%)"
            if pos.side == "SELL" and current_price >= pos.trail_price:
                return f"Trailing Stop (PnL: {pnl:+.2f}%)"

        # 2b. Break-even stop: move SL to entry after 1×ATR profit
        if pos.trail_distance > 0 and not getattr(pos, '_breakeven_set', False):
            profit_dist = abs(current_price - pos.entry_price)
            if profit_dist >= pos.trail_distance / config.TRAILING_ATR_MULT * config.SL_ATR_MULT:
                # Price moved 1R in our favor — lock in breakeven
                if pos.side == "BUY" and pos.sl_price < pos.entry_price:
                    pos.sl_price = pos.entry_price
                    pos._breakeven_set = True
                    log.info(f"  {pos.symbol}: SL moved to breakeven "
                             f"({pos.entry_price:.4f})")
                elif pos.side == "SELL" and pos.sl_price > pos.entry_price:
                    pos.sl_price = pos.entry_price
                    pos._breakeven_set = True
                    log.info(f"  {pos.symbol}: SL moved to breakeven "
                             f"({pos.entry_price:.4f})")

        # 3. Take Profit
        if pos.side == "BUY" and current_price >= pos.tp_price:
            if config.PARTIAL_TP and not pos.partial_taken:
                self._partial_close(pos, current_price)
                return None
            return f"Take Profit (PnL: {pnl:+.2f}%)"
        if pos.side == "SELL" and current_price <= pos.tp_price:
            if config.PARTIAL_TP and not pos.partial_taken:
                self._partial_close(pos, current_price)
                return None
            return f"Take Profit (PnL: {pnl:+.2f}%)"

        # 4. Time-based exit
        bars_held = self.tick - pos.entry_tick
        if bars_held >= config.TIME_EXIT_BARS:
            return f"Time Exit ({bars_held} bars, PnL: {pnl:+.2f}%)"

        # No exit
        log.info(f"  {pos.symbol}: {pos.side} | "
                 f"PnL: {pnl:+.2f}% | "
                 f"SL: {pos.sl_price:.4f} | "
                 f"TP: {pos.tp_price:.4f} | "
                 f"Trail: {pos.trail_price:.4f}")
        return None

    def _partial_close(self, pos: Position, current_price: float):
        """Close partial position (50%) and move SL to breakeven."""
        partial_size = max(1, int(pos.size * config.PARTIAL_TP_PCT / 100))
        pnl = pos.pnl_pct(current_price)

        log.info(f"  Partial TP: closing {partial_size}/{pos.size} "
                 f"contracts at {current_price}")
        try:
            close_side = "sell" if pos.side == "BUY" else "buy"
            self.client.place_market_order(pos.product_id,
                                           close_side, partial_size,
                                           symbol=pos.symbol)
            pos.size -= partial_size
            pos.partial_taken = True
            pos.sl_price = pos.entry_price
            log.info(f"  SL moved to breakeven: {pos.entry_price}")
            alerts.alert_partial_tp(pos.symbol, pos.side,
                                    current_price, config.PARTIAL_TP_PCT, pnl)
        except Exception as e:
            log.error(f"  Partial close failed: {e}")

    # ── Entry Logic ──────────────────────────────────────────

    def try_entry(self, symbol: str, product_id: int,
                  df_entry, df_confirm, df_trend,
                  regime_info: dict, current_price: float):
        """Run the full signal pipeline and enter if conditions are met."""
        # Risk check
        can_trade, reason = self.risk.can_open_trade()
        if not can_trade:
            alerts.alert_risk_block(reason)
            if self.risk.killed:
                alerts.alert_kill_switch(self.risk.kill_reason)
            db.log_filter_block(symbol, "risk_manager", reason)
            return

        # ── Filter: Session time (trade only during active hours) ─
        if getattr(config, 'SESSION_FILTER', False):
            utc_hour = datetime.now(timezone.utc).hour
            start_h = getattr(config, 'SESSION_START_UTC', 8)
            end_h = getattr(config, 'SESSION_END_UTC', 22)
            if not (start_h <= utc_hour < end_h):
                log.info(f"  BLOCKED: Outside session hours "
                         f"(UTC {utc_hour}h, allowed {start_h}-{end_h})")
                db.log_filter_block(symbol, "session_time",
                                    f"UTC {utc_hour}h outside {start_h}-{end_h}")
                return

        # ── Filter 0: Symbol cooldown ────────────────────────
        if self._is_on_cooldown(symbol):
            db.log_filter_block(symbol, "cooldown",
                                f"Cooldown until tick {self.symbol_cooldowns.get(symbol, 0)}")
            return

        # ── Filter 0b: Correlation check ─────────────────────
        if self._is_correlated_blocked(symbol):
            db.log_filter_block(symbol, "correlation",
                                f"Correlated symbol already open")
            return

        # ── Filter 0c: Minimum ATR% (fees filter) ───────────
        atr_pct = regime_info.get("atr_pct", 0)
        min_atr = getattr(config, 'MIN_ATR_PCT', 0.15)
        if atr_pct < min_atr:
            log.info(f"  BLOCKED: ATR%={atr_pct:.3f}% < {min_atr}% "
                     f"(too low — fees would eat profits)")
            db.log_filter_block(symbol, "min_atr",
                                f"ATR%={atr_pct:.3f}% < {min_atr}%")
            return

        # ── Filter 0d: Minimum ADX (trend strength) ─────────
        adx_val = regime_info.get("adx", 0)
        min_adx = getattr(config, 'MIN_ADX_ENTRY', 20)
        if adx_val < min_adx:
            log.info(f"  BLOCKED: ADX={adx_val:.1f} < {min_adx} "
                     f"(no clear trend — choppy market)")
            db.log_filter_block(symbol, "min_adx",
                                f"ADX={adx_val:.1f} < {min_adx}")
            return

        # Regime check
        trade_ok, regime_msg = should_trade_in_regime(regime_info)
        if not trade_ok:
            log.info(f"  Regime block: {regime_msg}")
            db.log_filter_block(symbol, "regime", regime_msg)
            return

        # Regime-aware strategy weights
        regime_weights = get_regime_strategy_bias(regime_info)

        # Higher-TF trend bias
        trend_bias = get_trend_bias(df_trend)

        # Run entry-TF signals
        signals = collect_signals(df_entry, regime_weights)

        # ── Strategy tracker: remove signals from disabled strategies ─
        if getattr(config, 'STRATEGY_TRACKER', False):
            disabled = db.get_disabled_strategies()
            if disabled:
                before = len(signals)
                signals = [s for s in signals if s.name not in disabled]
                if len(signals) < before:
                    log.info(f"  Removed {before - len(signals)} disabled "
                             f"strategy signals: {disabled}")

        # Log individual signals
        for sig in signals:
            direction_str = sig.direction or "HOLD"
            log.info(f"  {sig.name:20s} → {direction_str:4s} "
                     f"({sig.strength:.1f}) {sig.detail}")

        # Preliminary aggregation to get direction for confirmation check
        prelim = aggregate_signals(signals, regime_weights, trend_bias, True)
        direction = prelim["direction"]

        if direction is None:
            for d in prelim["details"][-2:]:
                if "BLOCKED" in d or "PENALTY" in d:
                    log.info(d)
            log.info(f"  No actionable signal "
                     f"(confirms={prelim['confirmations']}, "
                     f"score={prelim['total_score']:.2f})")
            db.log_filter_block(symbol, "no_signal",
                                f"score={prelim['total_score']:.2f}, "
                                f"confirms={prelim['confirmations']}")
            return

        # Now check confirmation TF and re-aggregate with all filters
        confirm_ok = check_confirmation_tf(df_confirm, direction)
        agg = aggregate_signals(signals, regime_weights, trend_bias, confirm_ok)
        direction = agg["direction"]

        # Log all penalty/block details
        for d in agg["details"]:
            if "PENALTY" in d or "BLOCKED" in d:
                log.info(d)

        if direction is None:
            log.info(f"  Signal blocked after all filters "
                     f"(score={agg['total_score']:.2f})")
            db.log_filter_block(symbol, "signal_filtered",
                                f"score={agg['total_score']:.2f} after confirm/trend")
            return

        # Calculate ATR for sizing and SL/TP
        from market_detector import compute_atr
        atr = compute_atr(df_entry, config.ATR_PERIOD)
        atr_val = float(atr.iloc[-1]) if len(atr) > 0 and not \
            __import__('numpy').isnan(atr.iloc[-1]) else 0

        # Position sizing — account for contract_value and available margin
        cv = self.client.get_contract_values()
        cval = cv.get(symbol, 1.0)
        avail_bal = self.client.get_available_balance()
        size = self.risk.calculate_size(current_price, atr_val, cval,
                                        available_balance=avail_bal)
        log.info(f"  Sizing: available=${avail_bal:.2f}, contract_value={cval}, size={size}")

        # SL/TP calculation
        sl_tp = self.risk.calculate_sl_tp(current_price, direction, atr_val)

        # ── Filter: Quick backtest check ─────────────────────
        if getattr(config, 'BACKTEST_BEFORE_LIVE', False):
            try:
                from backtest import run_backtest
                bt = run_backtest(symbol, config.TF_ENTRY, candle_count=200,
                                  starting_balance=10000, quiet=True)
                min_wr = getattr(config, 'BACKTEST_MIN_WINRATE', 40)
                if bt.total_trades > 0 and bt.win_rate < min_wr:
                    log.info(f"  BLOCKED: {symbol} backtest WR={bt.win_rate:.0f}% "
                             f"< {min_wr}% ({bt.total_trades} trades)")
                    db.log_filter_block(symbol, "backtest",
                                        f"WR={bt.win_rate:.0f}% < {min_wr}%")
                    return
                elif bt.total_trades > 0:
                    log.info(f"  Backtest OK: {symbol} WR={bt.win_rate:.0f}% "
                             f"PF={bt.profit_factor:.1f} ({bt.total_trades} trades)")
            except Exception as e:
                log.warning(f"  Backtest check failed: {e} — proceeding anyway")

        # Execute
        self._open_position(symbol, product_id, direction, current_price,
                            size, sl_tp, agg["confirmations"], regime_info,
                            signals)

    # ── Execution ────────────────────────────────────────────

    def _open_position(self, symbol: str, product_id: int,
                       direction: str, price: float, size: int,
                       sl_tp: dict, confirmations: int, regime_info: dict,
                       signals=None):
        side = "buy" if direction == "BUY" else "sell"
        log.info(f"  Opening {direction} {symbol} | "
                 f"size={size} | price≈{price} | "
                 f"SL={sl_tp['sl_price']} | TP={sl_tp['tp_price']}")
        try:
            if config.PAPER_TRADING:
                log.info("  [PAPER] Order simulated")
                resp = {"result": {"id": "paper"}}
            elif getattr(config, 'USE_LIMIT_ORDERS', False):
                # Limit order: offset price slightly for better fill
                offset = getattr(config, 'LIMIT_OFFSET_PCT', 0.02) / 100
                if direction == "BUY":
                    limit_price = price * (1 + offset)
                else:
                    limit_price = price * (1 - offset)
                # Keep same precision as market price
                price_str = f"{price}"
                decimals = len(price_str.split('.')[-1]) if '.' in price_str else 2
                limit_price = round(limit_price, max(decimals, 2))
                log.info(f"  Using LIMIT order at {limit_price} "
                         f"(offset={offset*100:.3f}%)")
                resp = self.client.place_limit_order(
                    product_id, side, size, limit_price)
            else:
                resp = self.client.place_market_order(
                    product_id, side, size,
                    symbol=symbol)
            log.info(f"  Order response: {resp}")

            pos = Position()
            pos.open(direction, price, size, product_id,
                     symbol, sl_tp, self.tick)

            # Track order ID
            order_id = 0
            try:
                order_id = int(resp.get("result", {}).get("id", 0))
            except (ValueError, TypeError):
                pass
            pos.order_id = order_id
            pos.regime = regime_info.get("regime", "")
            pos.confirmations = confirmations
            # Track which strategies contributed to this entry
            if signals:
                pos.strategies_used = [
                    s.name for s in signals
                    if s.direction == direction and s.strength > 0
                ]

            self.positions[symbol] = pos

            alerts.alert_entry(
                symbol, direction, price, size,
                sl_tp["sl_price"], sl_tp["tp_price"],
                confirmations, regime_info["regime"],
            )
        except Exception as e:
            log.error(f"  Failed to open {symbol}: {e}")
            alerts.alert_error(f"Open position failed: {e}")

    def _close_position(self, pos: Position, reason: str):
        if not pos.active:
            return

        try:
            df = self._fetch_candles(pos.symbol, config.TF_ENTRY, limit=5)
            current_price = float(df["close"].iloc[-1]) if df is not None \
                else pos.entry_price
        except Exception:
            current_price = pos.entry_price

        pnl = pos.pnl_pct(current_price)
        log.info(f"  Closing {pos.side} {pos.symbol} | "
                 f"reason={reason} | PnL={pnl:+.2f}%")

        try:
            if not config.PAPER_TRADING:
                close_resp = self.client.close_position(
                    pos.product_id, pos.size, pos.side,
                    symbol=pos.symbol)
                close_order_id = 0
                try:
                    close_order_id = int(
                        close_resp.get("result", {}).get("id", 0))
                except (ValueError, TypeError):
                    pass
            else:
                close_order_id = 0

            record = TradeRecord(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=current_price,
                pnl_pct=pnl,
                size=pos.size,
                timestamp=time.time(),
                reason=reason,
                order_id=pos.order_id,
                close_order_id=close_order_id,
                regime=getattr(pos, 'regime', ''),
                confirmations=getattr(pos, 'confirmations', 0),
            )
            self.risk.record_trade(record)

            # ── Strategy tracker: update per-strategy stats ──
            if getattr(config, 'STRATEGY_TRACKER', False):
                strats = getattr(pos, 'strategies_used', [])
                is_win = pnl > 0
                for strat in strats:
                    db.update_strategy_stat(strat, is_win, pnl)
                    # Auto-disable underperforming strategies
                    stats_row = db.get_strategy_stats()
                    for sr in stats_row:
                        if sr["strategy"] == strat:
                            min_t = getattr(config, 'STRATEGY_MIN_TRADES', 10)
                            min_wr = getattr(config, 'STRATEGY_MIN_WINRATE', 30)
                            if sr["trades"] >= min_t:
                                wr = sr["wins"] / sr["trades"] * 100 if sr["trades"] > 0 else 0
                                if wr < min_wr:
                                    db.set_strategy_enabled(strat, False)
                                    log.warning(f"  Strategy '{strat}' auto-disabled: "
                                                f"WR={wr:.0f}% < {min_wr}% "
                                                f"after {sr['trades']} trades")

            alerts.alert_exit(
                pos.symbol, pos.side,
                pos.entry_price, current_price, pnl, reason,
            )
        except Exception as e:
            log.error(f"  Failed to close {pos.symbol}: {e}")
            alerts.alert_error(f"Close position failed: {e}")
        finally:
            symbol = pos.symbol
            pos.close()
            if symbol in self.positions:
                del self.positions[symbol]
            # Set cooldown for this symbol
            cooldown = getattr(config, 'SYMBOL_COOLDOWN', 5)
            if cooldown > 0:
                self.symbol_cooldowns[symbol] = self.tick + cooldown

    # ── Daily Report ─────────────────────────────────────────

    def maybe_send_daily_report(self):
        today = date.today()
        if self.last_report_date != today and datetime.now().hour >= 23:
            stats = self.risk.get_stats()
            alerts.alert_daily_report(stats)
            self.last_report_date = today

    # ── Sync with Exchange ───────────────────────────────────

    def sync_position_from_exchange(self):
        """Reconcile local state with exchange every tick."""
        try:
            exchange_positions = self.client.get_positions()
            active_pids = {}
            for p in exchange_positions:
                size = int(p.get("size", 0))
                if abs(size) > 0:
                    active_pids[p.get("product_id", 0)] = p

            # Build pid->symbol map for discovery
            pid_to_sym = {}
            if active_pids:
                products = self.client.get_products()
                pid_to_sym = {p["id"]: p["symbol"] for p in products}

            # Track local product_ids
            local_pids = {pos.product_id for pos in self.positions.values()
                          if pos.active}

            # Discover positions not tracked locally
            for pid, ep in active_pids.items():
                if pid in local_pids:
                    continue
                symbol = pid_to_sym.get(pid, f"PID_{pid}")
                size = int(ep.get("size", 0))
                entry_price = float(ep.get("entry_price", 0))
                side = "BUY" if size > 0 else "SELL"
                log.warning(f"  Discovered untracked position: "
                            f"{side} {symbol} size={abs(size)}")
                pos = Position()
                pos.active = True
                pos.side = side
                pos.entry_price = entry_price
                pos.size = abs(size)
                pos.product_id = pid
                pos.symbol = symbol
                atr_est = entry_price * 0.005
                if side == "BUY":
                    pos.sl_price = entry_price * (1 - config.SL_ATR_MULT * 0.005)
                    pos.tp_price = entry_price * (1 + config.TP_ATR_MULT * 0.005)
                    pos.trail_price = entry_price - atr_est * config.TRAILING_ATR_MULT
                else:
                    pos.sl_price = entry_price * (1 + config.SL_ATR_MULT * 0.005)
                    pos.tp_price = entry_price * (1 - config.TP_ATR_MULT * 0.005)
                    pos.trail_price = entry_price + atr_est * config.TRAILING_ATR_MULT
                pos.trail_distance = atr_est * config.TRAILING_ATR_MULT
                pos.entry_tick = self.tick
                self.positions[symbol] = pos

            # Check for positions closed externally
            closed_syms = []
            for sym, pos in self.positions.items():
                if not pos.active:
                    continue
                if pos.product_id not in active_pids:
                    log.warning(f"  {sym} closed externally — syncing")
                    closed_syms.append(sym)
                else:
                    # Reconcile size
                    ep = active_pids[pos.product_id]
                    exchange_size = abs(int(ep.get("size", 0)))
                    exchange_entry = float(ep.get("entry_price", 0))
                    if exchange_size != pos.size:
                        log.warning(f"  {sym} size mismatch: "
                                    f"local={pos.size} exchange={exchange_size}")
                        pos.size = exchange_size
                    if exchange_entry > 0 and pos.entry_price > 0:
                        drift = abs(exchange_entry - pos.entry_price
                                    ) / pos.entry_price
                        if drift > 0.01:
                            log.warning(f"  {sym} entry mismatch: "
                                        f"local={pos.entry_price} "
                                        f"exchange={exchange_entry}")
                            pos.entry_price = exchange_entry

            for sym in closed_syms:
                pos = self.positions[sym]
                # Record the closed trade in DB
                try:
                    exit_price = pos.entry_price  # Best estimate
                    df = self._fetch_candles(sym, config.TF_ENTRY, limit=5)
                    if df is not None and len(df) > 0:
                        exit_price = float(df["close"].iloc[-1])
                except Exception:
                    exit_price = pos.entry_price

                pnl = pos.pnl_pct(exit_price)
                record = TradeRecord(
                    symbol=sym,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl,
                    size=pos.size,
                    timestamp=time.time(),
                    reason="Closed Externally",
                    order_id=getattr(pos, 'order_id', 0),
                    close_order_id=0,
                    regime=getattr(pos, 'regime', ''),
                    confirmations=getattr(pos, 'confirmations', 0),
                )
                self.risk.record_trade(record)
                log.info(f"  Recorded {sym} external close: PnL={pnl:+.2f}%")

                pos.close()
                del self.positions[sym]

            # Poll pending orders
            pending = db.get_pending_orders()
            for o in pending:
                self.client.poll_order_status(
                    o["order_id"], o["product_id"])

        except Exception as e:
            log.warning(f"  Position sync failed: {e}")

    # ── Main Loop ────────────────────────────────────────────

    def run(self):
        log.info("=" * 60)
        log.info("  Delta Exchange Trading Bot v3 — Multi-Symbol")
        log.info(f"  Mode       : {'AUTO-SCAN' if config.AUTO_SCAN else config.SYMBOL}")
        log.info(f"  Max Trades : {config.MAX_OPEN_TRADES} concurrent")
        log.info(f"  Timeframes : Entry={config.TF_ENTRY} "
                 f"Confirm={config.TF_CONFIRM} Trend={config.TF_TREND}")
        log.info(f"  Risk       : {config.RISK_PER_TRADE_PCT}% per trade, "
                 f"{config.DAILY_LOSS_LIMIT_PCT}% daily limit")
        log.info(f"  Strategies : "
                 f"{[k for k,v in config.STRATEGIES.items() if v]}")
        log.info(f"  Testnet    : {config.TESTNET}")
        log.info(f"  Paper      : {config.PAPER_TRADING}")
        log.info("=" * 60)

        self.init()

        while True:
            try:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                log.info(f"\n{'─'*50}")
                log.info(f"  Tick #{self.tick} | {ts}")
                log.info(f"  Open positions: {list(self.positions.keys()) or 'none'}")
                log.info(f"{'─'*50}")

                # ── 1. Check exits on ALL open positions ─────────
                closed_symbols = []
                for sym, pos in list(self.positions.items()):
                    if not pos.active:
                        continue
                    df = self._fetch_candles(sym, config.TF_ENTRY, limit=5)
                    if df is None or len(df) < 1:
                        continue
                    price = float(df["close"].iloc[-1])
                    exit_reason = self.check_exits(pos, price)
                    if exit_reason:
                        self._close_position(pos, exit_reason)
                        closed_symbols.append(sym)

                # ── 2. Signal flip detection on open positions ───
                for sym, pos in list(self.positions.items()):
                    if not pos.active:
                        continue
                    df_e = self._fetch_candles(sym, config.TF_ENTRY)
                    df_t = self._fetch_candles(sym, config.TF_TREND)
                    if df_e is None or len(df_e) < 20:
                        continue
                    regime_info = detect_regime(df_e)
                    regime_weights = get_regime_strategy_bias(regime_info)
                    signals = collect_signals(df_e, regime_weights)
                    trend_bias = get_trend_bias(df_t) if df_t is not None else None
                    agg = aggregate_signals(signals, regime_weights,
                                            trend_bias, True)
                    if agg["direction"] and agg["direction"] != pos.side:
                        log.info(f"  {sym}: Signal flipped "
                                 f"{pos.side} → {agg['direction']}")
                        self._close_position(pos, "Signal Flip")

                # ── 3. Scan for NEW entries if we have capacity ──
                open_count = len([p for p in self.positions.values()
                                  if p.active])

                # Dynamic max positions based on current regime
                max_trades = config.MAX_OPEN_TRADES
                if getattr(config, 'DYNAMIC_MAX_POS', False):
                    # Sample regime from first available symbol
                    try:
                        sample_sym = config.SYMBOL
                        sample_df = self._fetch_candles(sample_sym,
                                                        config.TF_ENTRY)
                        if sample_df is not None and len(sample_df) >= 50:
                            sample_regime = detect_regime(sample_df)
                            r = sample_regime.get("regime", "")
                            if r == MarketRegime.VOLATILE:
                                max_trades = getattr(config, 'MAX_POS_VOLATILE', 1)
                            elif r == MarketRegime.RANGING:
                                max_trades = getattr(config, 'MAX_POS_RANGING', 2)
                            elif r in (MarketRegime.TRENDING_UP,
                                       MarketRegime.TRENDING_DOWN):
                                max_trades = getattr(config, 'MAX_POS_TRENDING', 3)
                            elif r == MarketRegime.LOW_LIQUIDITY:
                                max_trades = getattr(config, 'MAX_POS_VOLATILE', 1)
                            log.info(f"  Dynamic max positions: "
                                     f"regime={r} → max={max_trades}")
                    except Exception as e:
                        log.warning(f"  Dynamic max pos failed: {e}")

                slots = max_trades - open_count

                if slots > 0:
                    should_scan = (
                        self.tick == 0 or
                        slots > 0 or  # Always scan when we have capacity
                        self.tick % config.SCAN_EVERY_N_TICKS == 0
                    )
                    if should_scan:
                        candidates = self.scan_candidates()
                        log.info(f"  Candidates to evaluate: "
                                 f"{[c['symbol'] for c in candidates]}")
                        for cand in candidates:
                            sym = cand["symbol"]
                            pid = cand.get("product_id") or \
                                self.client.get_product_id(sym)

                            log.info(f"  Evaluating {sym} "
                                     f"(score={cand['total_score']})...")

                            df_entry = self._fetch_candles(sym, config.TF_ENTRY)
                            df_confirm = self._fetch_candles(
                                sym, config.TF_CONFIRM)
                            df_trend = self._fetch_candles(sym, config.TF_TREND)

                            if df_entry is None or len(df_entry) < 20:
                                log.info(f"  {sym}: insufficient data — skip")
                                continue

                            price = float(df_entry["close"].iloc[-1])
                            regime_info = detect_regime(df_entry)
                            log.info(f"  {sym}: price={price} "
                                     f"regime={regime_info['regime']}")

                            self.try_entry(sym, pid, df_entry, df_confirm,
                                           df_trend, regime_info, price)

                            # Re-check slots
                            open_count = len([p for p in self.positions.values()
                                              if p.active])
                            if open_count >= max_trades:
                                break

                # ── 4. Sync with exchange ────────────────────────
                self.sync_position_from_exchange()

                # ── 5. Daily report ──────────────────────────────
                self.maybe_send_daily_report()

                # ── 6. Risk stats ────────────────────────────────
                stats = self.risk.get_stats()
                if stats["total_trades"] > 0:
                    log.info(f"  Stats: {stats['total_trades']} trades, "
                             f"WR={stats['win_rate']:.0f}%, "
                             f"PF={stats['profit_factor']:.2f}, "
                             f"PnL={stats['total_pnl_pct']:+.2f}%")

                self.tick += 1

            except KeyboardInterrupt:
                log.info("\nBot stopped by user.")
                for sym, pos in list(self.positions.items()):
                    if pos.active:
                        log.info(f"Closing {sym} before exit...")
                        self._close_position(pos, "Bot Shutdown")
                stats = self.risk.get_stats()
                if stats["total_trades"] > 0:
                    log.info(f"\nFinal stats: {stats}")
                break

            except Exception as e:
                log.error(f"  Loop error: {e}", exc_info=True)
                alerts.alert_error(str(e))
                self.tick += 1

            time.sleep(config.POLL_INTERVAL_SEC)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
