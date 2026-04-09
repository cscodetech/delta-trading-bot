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
    pnl_usd: float = 0.0
    net_pnl_usd: float = 0.0
    contract_value: float = 1.0
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
        self._daily_start_balance: float = 0.0
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
        self._daily_start_balance = balance
        log.info(f"  RiskManager initialised: balance=${balance:,.2f}")

    # ── Daily reset ──────────────────────────────────────────

    def _check_day_rollover(self):
        today = date.today()
        if today != self._today:
            try:
                y_pnl_usd = self.current_balance - self._daily_start_balance
                y_pnl_pct = (y_pnl_usd / self._daily_start_balance * 100) if self._daily_start_balance > 0 else 0.0
            except Exception:
                y_pnl_usd = 0.0
                y_pnl_pct = 0.0
            log.info(
                "  New day: resetting daily counters. "
                f"Yesterday PnL: {y_pnl_usd:+.2f} USD ({y_pnl_pct:+.2f}%)"
            )
            self._today = today
            self._daily_start_balance = self.current_balance
            self._daily_trades = 0

    # ── Can we trade? ────────────────────────────────────────

    def can_open_trade(self) -> tuple[bool, str]:
        """Returns (allowed, reason). Check this before every entry."""
        self._check_day_rollover()

        if self.killed:
            return False, f"KILLED: {self.kill_reason}"

        # Daily loss limit
        if self._daily_start_balance > 0:
            daily_pnl_usd = self.current_balance - self._daily_start_balance
            daily_loss_pct = max(0.0, (-daily_pnl_usd) / self._daily_start_balance * 100)
            if daily_loss_pct >= float(config.DAILY_LOSS_LIMIT_PCT):
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

    def calculate_sl_tp(self, price: float, side: str, atr: float, ai_confidence: float = None, volatility: float = None) -> dict:
        """
        Returns dict with sl_price, tp_price, trail_distance.
        Adaptive: tightens SL/TP in high volatility, widens in low volatility, and scales with AI confidence.
        """
        sl_mult = config.SL_ATR_MULT
        tp_mult = config.TP_ATR_MULT
        trail_mult = config.TRAIL_ATR_MULT
        # Adapt SL/TP based on AI confidence
        if ai_confidence is not None:
            if ai_confidence >= 0.8:
                sl_mult *= 0.9
                tp_mult *= 1.2
            elif ai_confidence < 0.65:
                sl_mult *= 0.7
                tp_mult *= 0.9
        # Adapt SL/TP based on volatility (ATR as % of price)
        if volatility is not None:
            if volatility > 2.0:  # High volatility, tighten SL
                sl_mult *= 0.8
                tp_mult *= 0.9
            elif volatility < 0.5:  # Low volatility, widen SL/TP
                sl_mult *= 1.2
                tp_mult *= 1.2
        if config.SL_MODE == "atr" and atr > 0:
            sl_dist = atr * sl_mult
        else:
            sl_dist = price * (config.SL_FIXED_PCT / 100)

        if config.TP_MODE == "atr" and atr > 0:
            tp_dist = atr * tp_mult
        else:
            tp_dist = price * (config.TP_FIXED_PCT / 100)

        trail_dist = atr * trail_mult if atr > 0 else sl_dist

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

        # Compute USD PnL from notional if caller didn't provide it
        cv = float(getattr(record, "contract_value", 1.0) or 1.0)
        notional_usd = float(record.size or 0) * cv * float(record.entry_price or 0)
        if notional_usd > 0:
            if not getattr(record, "pnl_usd", 0.0):
                record.pnl_usd = round(notional_usd * (record.pnl_pct / 100.0), 4)
            if not getattr(record, "net_pnl_usd", 0.0):
                record.net_pnl_usd = round(notional_usd * (record.net_pnl_pct / 100.0), 4)

        self.trade_history.append(record)
        self._daily_trades += 1

        # Update wallet balance in USD terms (trade PnL is on notional, not on full wallet)
        self.current_balance += float(record.net_pnl_usd or 0.0)
        self.peak_balance = max(self.peak_balance, self.current_balance)

        # Streak tracking
        if float(getattr(record, "net_pnl_usd", 0.0) or 0.0) < 0:
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            self._cooldown_ticks = config.COOLDOWN_AFTER_LOSS
            log.info(f"  Loss #{self._consecutive_losses} — "
                     f"cooldown {config.COOLDOWN_AFTER_LOSS} ticks")
        else:
            self._consecutive_wins += 1
            self._consecutive_losses = 0

        try:
            daily_pnl_usd = self.current_balance - self._daily_start_balance
            daily_pnl_pct = (daily_pnl_usd / self._daily_start_balance * 100) if self._daily_start_balance > 0 else 0.0
        except Exception:
            daily_pnl_usd = 0.0
            daily_pnl_pct = 0.0

        log.info(
            f"  Trade recorded: {record.side} {record.symbol} "
            f"PnL={record.pnl_pct:+.2f}% fees={record.fee_pct:.2f}% "
            f"net={record.net_pnl_pct:+.2f}% | "
            f"net_usd={record.net_pnl_usd:+.2f} | "
            f"Daily={daily_pnl_usd:+.2f} USD ({daily_pnl_pct:+.2f}%) | "
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
                "pnl_usd": float(getattr(record, "pnl_usd", 0.0) or 0.0),
                "net_pnl_usd": float(getattr(record, "net_pnl_usd", 0.0) or 0.0),
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

        wins = [t for t in self.trade_history if float(getattr(t, "net_pnl_usd", 0.0) or 0.0) > 0]
        losses = [t for t in self.trade_history if float(getattr(t, "net_pnl_usd", 0.0) or 0.0) <= 0]

        gross_profit = sum(float(getattr(t, "net_pnl_usd", 0.0) or 0.0) for t in wins)
        gross_loss = abs(sum(float(getattr(t, "net_pnl_usd", 0.0) or 0.0) for t in losses))

        total_pnl_usd = self.current_balance - self.starting_balance
        total_pnl_pct = (total_pnl_usd / self.starting_balance * 100) if self.starting_balance > 0 else 0.0
        daily_pnl_usd = self.current_balance - self._daily_start_balance
        daily_pnl_pct = (daily_pnl_usd / self._daily_start_balance * 100) if self._daily_start_balance > 0 else 0.0

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / total * 100, 1),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999,
            "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0,
            "max_consecutive_losses": self._max_streak(False),
            "daily_pnl": round(daily_pnl_pct, 3),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "daily_trades": self._daily_trades,
            "drawdown_pct": round(
                (self.peak_balance - self.current_balance) / self.peak_balance * 100, 2
            ) if self.peak_balance > 0 else 0,
        }

    def _max_streak(self, wins: bool) -> int:
        max_s = cur = 0
        for t in self.trade_history:
            is_win = float(getattr(t, "net_pnl_usd", 0.0) or 0.0) > 0
            if is_win == wins:
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
