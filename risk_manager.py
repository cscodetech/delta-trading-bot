"""
risk_manager.py — Capital preservation engine.

Responsibilities:
  - Position sizing (ATR-based or fixed)
  - Daily P&L tracking with auto-shutdown
  - Max drawdown kill switch
  - Trade counting (no revenge trading)
  - Consecutive loss/win tracking for adaptive sizing
  - Cooldown after losses
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import config
import database as db

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")

log = logging.getLogger("delta_bot")


@dataclass
class TradeRecord:
    symbol: str
    side: str           # "BUY" or "SELL"
    entry_price: float
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    fee_pct: float = 0.0
    net_pnl_pct: float = 0.0
    size: int = 0
    timestamp: float = 0.0
    reason: str = ""
    order_id: int = 0
    close_order_id: int = 0
    regime: str = ""
    confirmations: int = 0


class RiskManager:
    """Enforces all risk rules. Query this BEFORE every trade."""

    def __init__(self):
        self.starting_balance: float = 0.0
        self.current_balance: float = 0.0
        self.peak_balance: float = 0.0

        # Daily tracking
        self._today: date = date.today()
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

        # Streak tracking
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._cooldown_ticks: int = 0

        # Trade log
        self.trade_history: list[TradeRecord] = []

        # Kill switch
        self.killed: bool = False
        self.kill_reason: str = ""

    # ── Initialise with account balance ──────────────────────

    def init_balance(self, balance: float):
        self.starting_balance = balance
        self.current_balance = balance
        self.peak_balance = balance
        log.info(f"  RiskManager initialised: balance=${balance:,.2f}")

    # ── Daily reset ──────────────────────────────────────────

    def _check_day_rollover(self):
        today = date.today()
        if today != self._today:
            log.info(f"  New day: resetting daily counters. "
                     f"Yesterday PnL: {self._daily_pnl:+.4f}")
            self._today = today
            self._daily_pnl = 0.0
            self._daily_trades = 0

    # ── Can we trade? ────────────────────────────────────────

    def can_open_trade(self) -> tuple[bool, str]:
        """Returns (allowed, reason). Check this before every entry."""
        self._check_day_rollover()

        if self.killed:
            return False, f"KILLED: {self.kill_reason}"

        # Daily loss limit
        if self.starting_balance > 0:
            daily_loss_pct = abs(min(0, self._daily_pnl)) / self.starting_balance * 100
            if daily_loss_pct >= config.DAILY_LOSS_LIMIT_PCT:
                self.killed = True
                self.kill_reason = (
                    f"Daily loss limit hit: {daily_loss_pct:.2f}% "
                    f"(limit: {config.DAILY_LOSS_LIMIT_PCT}%)"
                )
                return False, self.kill_reason

        # Max drawdown from peak
        if self.peak_balance > 0:
            drawdown_pct = (self.peak_balance - self.current_balance) / self.peak_balance * 100
            if drawdown_pct >= config.MAX_DRAWDOWN_PCT:
                self.killed = True
                self.kill_reason = (
                    f"Max drawdown hit: {drawdown_pct:.2f}% "
                    f"(limit: {config.MAX_DRAWDOWN_PCT}%)"
                )
                return False, self.kill_reason

        # Max trades per day
        if self._daily_trades >= config.MAX_TRADES_PER_DAY:
            return False, f"Daily trade limit reached ({config.MAX_TRADES_PER_DAY})"

        # Cooldown after loss
        if self._cooldown_ticks > 0:
            self._cooldown_ticks -= 1
            return False, f"Cooling down ({self._cooldown_ticks + 1} ticks left)"

        return True, "OK"

    # ── Position sizing ──────────────────────────────────────

    def calculate_size(self, price: float, atr: float,
                       contract_value: float = 1.0,
                       available_balance: float = 0.0) -> int:
        """
        ATR-based position sizing:
          risk_amount = available_balance * risk_per_trade%
          stop_distance = ATR * SL multiplier
          size (contracts) = risk_amount / (stop_distance * contract_value)
        Falls back to BASE_QTY if dynamic sizing is off.
        """
        cv = max(contract_value, 1e-9)
        # Use available balance (free margin) if provided, else fall back
        bal = available_balance if available_balance > 0 else self.current_balance

        if not config.DYNAMIC_SIZING or bal <= 0 or atr <= 0:
            size = config.BASE_QTY
        else:
            risk_amount = bal * (config.RISK_PER_TRADE_PCT / 100)
            if config.SL_MODE == "atr":
                stop_distance = atr * config.SL_ATR_MULT
            else:
                stop_distance = price * (config.SL_FIXED_PCT / 100)

            if stop_distance <= 0:
                size = config.BASE_QTY
            else:
                # Each contract = contract_value units of base asset
                # Dollar risk per contract = stop_distance * contract_value
                size = max(1, int(risk_amount / (stop_distance * cv)))

        # Reduce after consecutive losses
        if config.REDUCE_AFTER_LOSSES and self._consecutive_losses >= 2:
            size = max(1, size // 2)
            log.info(f"  Size halved due to {self._consecutive_losses} consecutive losses → {size}")

        # Compound after consecutive wins
        if config.COMPOUND_WINS and self._consecutive_wins >= 3:
            size = int(size * 1.5)
            log.info(f"  Size increased due to {self._consecutive_wins} consecutive wins → {size}")

        return size

    # ── SL/TP calculation ────────────────────────────────────

    def calculate_sl_tp(self, price: float, side: str, atr: float) -> dict:
        """Returns dict with sl_price, tp_price, trail_distance."""
        if config.SL_MODE == "atr" and atr > 0:
            sl_dist = atr * config.SL_ATR_MULT
        else:
            sl_dist = price * (config.SL_FIXED_PCT / 100)

        if config.TP_MODE == "atr" and atr > 0:
            tp_dist = atr * config.TP_ATR_MULT
        else:
            tp_dist = price * (config.TP_FIXED_PCT / 100)

        trail_dist = atr * config.TRAIL_ATR_MULT if atr > 0 else sl_dist

        if side == "BUY":
            sl_price = price - sl_dist
            tp_price = price + tp_dist
        else:
            sl_price = price + sl_dist
            tp_price = price - tp_dist

        return {
            "sl_price": round(sl_price, 6),
            "tp_price": round(tp_price, 6),
            "sl_pct": round(sl_dist / price * 100, 3),
            "tp_pct": round(tp_dist / price * 100, 3),
            "trail_distance": round(trail_dist, 6),
        }

    # ── Record trade result ──────────────────────────────────

    @staticmethod
    def calculate_fees(entry_price: float, exit_price: float,
                       size: int) -> float:
        """Calculate round-trip fees + slippage as a percentage."""
        fee_rate = config.TAKER_FEE_PCT / 100   # per side
        slippage = config.SLIPPAGE_PCT / 100     # per side
        cost_per_side = fee_rate + slippage
        # Entry cost + exit cost as % of entry notional
        total_cost_pct = cost_per_side * 2 * 100
        return round(total_cost_pct, 4)

    def record_trade(self, record: TradeRecord):
        """Call after closing a position."""
        # Calculate fees if not already set
        if record.fee_pct == 0:
            record.fee_pct = self.calculate_fees(
                record.entry_price, record.exit_price, record.size)
        record.net_pnl_pct = round(record.pnl_pct - record.fee_pct, 4)

        self.trade_history.append(record)
        self._daily_trades += 1
        self._daily_pnl += record.net_pnl_pct

        # Update balance using net PnL (after fees)
        pnl_amount = self.current_balance * (record.net_pnl_pct / 100)
        self.current_balance += pnl_amount
        self.peak_balance = max(self.peak_balance, self.current_balance)

        # Streak tracking
        if record.net_pnl_pct < 0:
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            self._cooldown_ticks = config.COOLDOWN_AFTER_LOSS
            log.info(f"  Loss #{self._consecutive_losses} — "
                     f"cooldown {config.COOLDOWN_AFTER_LOSS} ticks")
        else:
            self._consecutive_wins += 1
            self._consecutive_losses = 0

        log.info(
            f"  Trade recorded: {record.side} {record.symbol} "
            f"PnL={record.pnl_pct:+.2f}% fees={record.fee_pct:.2f}% "
            f"net={record.net_pnl_pct:+.2f}% | "
            f"Daily={self._daily_pnl:+.2f}% | "
            f"Trades today={self._daily_trades}/{config.MAX_TRADES_PER_DAY} | "
            f"Streak: W{self._consecutive_wins}/L{self._consecutive_losses}"
        )

        # Persist to MySQL database
        self._persist_trade(record)

    def _persist_trade(self, record: TradeRecord):
        """Save trade to MySQL database."""
        try:
            db.insert_trade({
                "symbol": record.symbol,
                "side": record.side,
                "entry_price": record.entry_price,
                "exit_price": record.exit_price,
                "pnl_pct": round(record.pnl_pct, 4),
                "fee_pct": record.fee_pct,
                "net_pnl_pct": record.net_pnl_pct,
                "size": record.size,
                "reason": record.reason,
                "order_id": record.order_id or None,
                "close_order_id": record.close_order_id or None,
                "regime": record.regime,
                "confirmations": record.confirmations,
                "closed_at": datetime.now(),
            })
        except Exception as e:
            log.warning(f"  Failed to persist trade to MySQL: {e}")

    # ── Stats ────────────────────────────────────────────────

    def get_stats(self) -> dict:
        total = len(self.trade_history)
        if total == 0:
            return {"total_trades": 0}

        wins = [t for t in self.trade_history if t.pnl_pct > 0]
        losses = [t for t in self.trade_history if t.pnl_pct <= 0]

        total_profit = sum(t.pnl_pct for t in wins)
        total_loss = abs(sum(t.pnl_pct for t in losses))

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / total * 100, 1),
            "total_pnl_pct": round(sum(t.pnl_pct for t in self.trade_history), 2),
            "profit_factor": round(total_profit / total_loss, 2) if total_loss > 0 else 999,
            "avg_win": round(total_profit / len(wins), 2) if wins else 0,
            "avg_loss": round(-total_loss / len(losses), 2) if losses else 0,
            "max_consecutive_losses": self._max_streak(False),
            "daily_pnl": round(self._daily_pnl, 3),
            "daily_trades": self._daily_trades,
            "drawdown_pct": round(
                (self.peak_balance - self.current_balance) / self.peak_balance * 100, 2
            ) if self.peak_balance > 0 else 0,
        }

    def _max_streak(self, wins: bool) -> int:
        max_s = cur = 0
        for t in self.trade_history:
            if (t.pnl_pct > 0) == wins:
                cur += 1
                max_s = max(max_s, cur)
            else:
                cur = 0
        return max_s

    # ── Manual kill switch ───────────────────────────────────

    def kill(self, reason: str = "Manual"):
        self.killed = True
        self.kill_reason = reason
        log.warning(f"  KILL SWITCH activated: {reason}")

    def reset_kill(self):
        self.killed = False
        self.kill_reason = ""
        log.info("  Kill switch reset.")
