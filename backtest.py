"""
backtest.py — Historical backtesting engine.

Runs strategies against historical candle data and outputs:
  - Win rate, profit factor, Sharpe ratio
  - Max drawdown, equity curve
  - Trade log
  - Per-strategy breakdown

Usage:
  python backtest.py               # Backtest with defaults
  python backtest.py BTCUSD 1h 500 # Custom symbol/tf/candles
"""

import sys
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

import config
from exchange import DeltaClient
from strategies import (
    collect_signals,
    aggregate_signals,
    get_trend_bias,
    check_confirmation_tf,
)
from market_detector import detect_regime, get_regime_strategy_bias, compute_atr
from risk_manager import RiskManager, TradeRecord

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("backtest")


@dataclass
class BacktestTrade:
    bar_entry: int
    bar_exit: int
    side: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    size: int
    reason: str


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    starting_balance: float = 10000.0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_pct > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl_pct <= 0)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades else 0

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        gross_loss = abs(sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0))
        return (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0
        peak = self.equity_curve[0]
        max_dd = 0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0
        returns = [t.pnl_pct for t in self.trades]
        avg = np.mean(returns)
        std = np.std(returns)
        return (avg / std * math.sqrt(252)) if std > 0 else 0

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return np.mean(wins) if wins else 0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct < 0]
        return np.mean(losses) if losses else 0

    @property
    def max_consecutive_losses(self) -> int:
        max_s = cur = 0
        for t in self.trades:
            if t.pnl_pct < 0:
                cur += 1
                max_s = max(max_s, cur)
            else:
                cur = 0
        return max_s

    def print_report(self):
        print("\n" + "=" * 60)
        print("  BACKTEST REPORT")
        print("=" * 60)
        print(f"  Total Trades     : {self.total_trades}")
        print(f"  Wins / Losses    : {self.wins} / {self.losses}")
        print(f"  Win Rate         : {self.win_rate:.1f}%")
        print(f"  Total PnL (net)  : {self.total_pnl_pct:+.2f}%")
        print(f"  Fees+Slippage    : ~{config.TAKER_FEE_PCT*2 + config.SLIPPAGE_PCT*2:.2f}% per round-trip")
        print(f"  Profit Factor    : {self.profit_factor:.2f}")
        print(f"  Sharpe Ratio     : {self.sharpe_ratio:.2f}")
        print(f"  Max Drawdown     : {self.max_drawdown:.2f}%")
        print(f"  Avg Win          : {self.avg_win:+.2f}%")
        print(f"  Avg Loss         : {self.avg_loss:+.2f}%")
        print(f"  Max Consec Losses: {self.max_consecutive_losses}")

        if self.equity_curve:
            final_bal = self.equity_curve[-1]
            roi = (final_bal - self.starting_balance) / self.starting_balance * 100
            print(f"  Final Balance    : ${final_bal:,.2f} (ROI: {roi:+.1f}%)")

        print("=" * 60)

        if self.trades:
            print("\n  TRADE LOG (last 20):")
            print(f"  {'#':>3} {'Side':5} {'Entry':>10} {'Exit':>10} "
                  f"{'PnL%':>8} {'Reason':15}")
            print(f"  {'-'*55}")
            for i, t in enumerate(self.trades[-20:], 1):
                print(f"  {i:3d} {t.side:5} {t.entry_price:10.4f} "
                      f"{t.exit_price:10.4f} {t.pnl_pct:+7.2f}% {t.reason[:15]}")


def run_backtest(symbol: str = "BTCUSD",
                 timeframe: str = "15m",
                 candle_count: int = 500,
                 starting_balance: float = 10000.0,
                 quiet: bool = False) -> BacktestResult:
    """
    Walk-forward backtest:
    - Fetch historical candles
    - At each bar (after warmup), run the full strategy pipeline
    - Simulate entries/exits with ATR-based SL/TP
    """
    if not quiet:
        print(f"\n  Backtesting {symbol} on {timeframe} ({candle_count} candles)...")
        print(f"  Starting balance: ${starting_balance:,.2f}")

    client = DeltaClient(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
    df = client.get_candles(symbol, timeframe, limit=candle_count)

    if len(df) < 100:
        if not quiet:
            print(f"  ERROR: Only {len(df)} candles fetched (need 100+)")
        return BacktestResult()

    if not quiet:
        print(f"  Fetched {len(df)} candles "
              f"({df['time'].iloc[0]} → {df['time'].iloc[-1]})")

    result = BacktestResult(starting_balance=starting_balance)
    risk = RiskManager()
    risk.init_balance(starting_balance)

    balance = starting_balance
    equity_curve = [balance]

    # Position state
    in_position = False
    pos_side = ""
    entry_price = 0.0
    entry_bar = 0
    sl_price = 0.0
    tp_price = 0.0
    trail_price = 0.0
    trail_dist = 0.0
    pos_size = 1
    partial_taken = False

    warmup = 210  # Need 200+ bars for EMA200

    for i in range(warmup, len(df)):
        # Build the lookback window
        window = df.iloc[:i + 1].copy()
        bar_close = float(window["close"].iloc[-1])
        bar_high = float(window["high"].iloc[-1])
        bar_low = float(window["low"].iloc[-1])

        # ── Check exits ──────────────────────────────────────
        if in_position:
            # Time-based exit
            bars_held = i - entry_bar
            if bars_held >= config.TIME_EXIT_BARS:
                pnl = _calc_pnl(pos_side, entry_price, bar_close)
                _record_exit(result, risk, symbol, pos_side, entry_bar, i,
                             entry_price, bar_close, pnl, pos_size, "Time Exit")
                balance = risk.current_balance
                in_position = False

            # SL hit
            elif (pos_side == "BUY" and bar_low <= sl_price) or \
                 (pos_side == "SELL" and bar_high >= sl_price):
                exit_price = sl_price
                pnl = _calc_pnl(pos_side, entry_price, exit_price)
                _record_exit(result, risk, symbol, pos_side, entry_bar, i,
                             entry_price, exit_price, pnl, pos_size, "Stop Loss")
                balance = risk.current_balance
                in_position = False

            # TP hit
            elif (pos_side == "BUY" and bar_high >= tp_price) or \
                 (pos_side == "SELL" and bar_low <= tp_price):
                if config.PARTIAL_TP and not partial_taken:
                    # Partial TP: take half at first target
                    partial_taken = True
                    # Move SL to breakeven
                    sl_price = entry_price
                else:
                    exit_price = tp_price
                    pnl = _calc_pnl(pos_side, entry_price, exit_price)
                    _record_exit(result, risk, symbol, pos_side, entry_bar, i,
                                 entry_price, exit_price, pnl, pos_size, "Take Profit")
                    balance = risk.current_balance
                    in_position = False

            # Trailing stop update
            elif config.TRAILING_STOP and trail_dist > 0:
                if pos_side == "BUY":
                    new_trail = bar_high - trail_dist
                    if new_trail > trail_price:
                        trail_price = new_trail
                    if bar_low <= trail_price:
                        pnl = _calc_pnl(pos_side, entry_price, trail_price)
                        _record_exit(result, risk, symbol, pos_side, entry_bar, i,
                                     entry_price, trail_price, pnl, pos_size, "Trailing Stop")
                        balance = risk.current_balance
                        in_position = False
                else:
                    new_trail = bar_low + trail_dist
                    if new_trail < trail_price:
                        trail_price = new_trail
                    if bar_high >= trail_price:
                        pnl = _calc_pnl(pos_side, entry_price, trail_price)
                        _record_exit(result, risk, symbol, pos_side, entry_bar, i,
                                     entry_price, trail_price, pnl, pos_size, "Trailing Stop")
                        balance = risk.current_balance
                        in_position = False

        # ── Check entries ─────────────────────────────────────
        if not in_position:
            can_trade, reason = risk.can_open_trade()
            if not can_trade:
                equity_curve.append(balance)
                continue

            # Run strategy pipeline
            regime_info = detect_regime(window)
            regime_weights = get_regime_strategy_bias(regime_info)
            trend_bias = get_trend_bias(window)

            signals = collect_signals(window, regime_weights)
            agg = aggregate_signals(signals, regime_weights, trend_bias, True)

            direction = agg["direction"]
            if direction is not None:
                atr = compute_atr(window, config.ATR_PERIOD)
                atr_val = float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0

                sl_tp = risk.calculate_sl_tp(bar_close, direction, atr_val)
                pos_size = risk.calculate_size(bar_close, atr_val)

                in_position = True
                pos_side = direction
                # Apply slippage to entry price
                entry_price = _apply_slippage(bar_close, direction, True)
                entry_bar = i
                sl_price = sl_tp["sl_price"]
                tp_price = sl_tp["tp_price"]
                trail_dist = sl_tp["trail_distance"]
                trail_price = (bar_close - trail_dist) if direction == "BUY" \
                    else (bar_close + trail_dist)
                partial_taken = False

        equity_curve.append(balance)

    # Close any remaining position
    if in_position:
        final_price = float(df["close"].iloc[-1])
        pnl = _calc_pnl(pos_side, entry_price, final_price)
        _record_exit(result, risk, symbol, pos_side, entry_bar, len(df) - 1,
                     entry_price, final_price, pnl, pos_size, "End of Data")
        balance = risk.current_balance

    result.equity_curve = equity_curve
    return result


def _calc_pnl(side: str, entry: float, exit: float) -> float:
    if side == "BUY":
        return (exit - entry) / entry * 100
    return (entry - exit) / entry * 100


def _apply_slippage(price: float, side: str, is_entry: bool) -> float:
    """Apply estimated slippage to fill price."""
    slip = config.SLIPPAGE_PCT / 100
    # Entry buy / exit sell: price goes up (worse fill)
    # Entry sell / exit buy: price goes down (worse fill)
    if (side == "BUY" and is_entry) or (side == "SELL" and not is_entry):
        return price * (1 + slip)
    return price * (1 - slip)


def _calc_fees() -> float:
    """Round-trip fee percentage (entry + exit)."""
    return (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100


def _record_exit(result: BacktestResult, risk: RiskManager, symbol: str,
                 side: str, bar_entry: int, bar_exit: int,
                 entry_price: float, exit_price: float,
                 pnl_pct: float, size: int, reason: str):
    fee_pct = round(_calc_fees(), 4)
    net_pnl = round(pnl_pct - fee_pct, 4)
    result.trades.append(BacktestTrade(
        bar_entry=bar_entry, bar_exit=bar_exit, side=side,
        entry_price=entry_price, exit_price=exit_price,
        pnl_pct=round(net_pnl, 4), size=size, reason=reason,
    ))
    risk.record_trade(TradeRecord(
        symbol=symbol, side=side, entry_price=entry_price,
        exit_price=exit_price, pnl_pct=pnl_pct,
        fee_pct=fee_pct, net_pnl_pct=net_pnl,
        size=size, timestamp=0, reason=reason,
    ))


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"
    tf = sys.argv[2] if len(sys.argv) > 2 else config.TF_CONFIRM
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    res = run_backtest(sym, tf, count)
    res.print_report()
