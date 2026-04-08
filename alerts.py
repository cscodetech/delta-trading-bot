"""
alerts.py — Telegram alert system + structured logging.

Sends trade entries, exits, daily summaries, and error alerts.
Gracefully degrades if Telegram is disabled or fails.
"""

import logging
import requests
from datetime import datetime

import config

log = logging.getLogger("delta_bot")

_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_telegram(message: str):
    """Send a message to the configured Telegram chat. Never raises."""
    if not config.TELEGRAM_ENABLED:
        return
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    try:
        url = _BASE_URL.format(token=config.TELEGRAM_TOKEN)
        resp = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=5)
        if resp.status_code != 200:
            log.warning(f"Telegram error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ALERT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def alert_entry(symbol: str, side: str, price: float, size: int,
                sl: float, tp: float, confirmations: int, regime: str):
    """Alert when opening a new position."""
    msg = (
        f"🟢 *ENTRY — {side}*\n"
        f"Symbol: `{symbol}`\n"
        f"Price: `{price}`\n"
        f"Size: `{size}` contracts\n"
        f"SL: `{sl}` | TP: `{tp}`\n"
        f"Confirmations: {confirmations}\n"
        f"Regime: {regime}\n"
        f"Time: {datetime.now().strftime('%H:%M:%S')}"
    )
    log.info(msg.replace("*", "").replace("`", ""))
    _send_telegram(msg)


def alert_exit(symbol: str, side: str, entry_price: float,
               exit_price: float, pnl_pct: float, reason: str):
    """Alert when closing a position."""
    emoji = "✅" if pnl_pct >= 0 else "🔴"
    msg = (
        f"{emoji} *EXIT — {side}*\n"
        f"Symbol: `{symbol}`\n"
        f"Entry: `{entry_price}` → Exit: `{exit_price}`\n"
        f"PnL: `{pnl_pct:+.2f}%`\n"
        f"Reason: {reason}\n"
        f"Time: {datetime.now().strftime('%H:%M:%S')}"
    )
    log.info(msg.replace("*", "").replace("`", ""))
    _send_telegram(msg)


def alert_partial_tp(symbol: str, side: str, price: float,
                     pct_closed: int, pnl_pct: float):
    """Alert for partial take profit."""
    msg = (
        f"💰 *PARTIAL TP*\n"
        f"Symbol: `{symbol}` ({side})\n"
        f"Closed {pct_closed}% at `{price}`\n"
        f"Running PnL: `{pnl_pct:+.2f}%`"
    )
    log.info(msg.replace("*", "").replace("`", ""))
    _send_telegram(msg)


def alert_risk_block(reason: str):
    """Alert when risk manager blocks a trade."""
    msg = f"⚠️ *RISK BLOCK*\n{reason}"
    log.warning(f"RISK BLOCK: {reason}")
    _send_telegram(msg)


def alert_kill_switch(reason: str):
    """Alert when kill switch activates."""
    msg = f"🛑 *KILL SWITCH ACTIVATED*\n{reason}"
    log.critical(f"KILL SWITCH: {reason}")
    _send_telegram(msg)


def alert_error(error: str):
    """Alert on critical errors."""
    msg = f"❗ *BOT ERROR*\n`{error[:200]}`"
    log.error(f"BOT ERROR: {error}")
    _send_telegram(msg)


def alert_daily_report(stats: dict):
    """Send end-of-day summary."""
    msg = (
        f"📊 *DAILY REPORT*\n"
        f"Trades: {stats.get('daily_trades', 0)}\n"
        f"Win Rate: {stats.get('win_rate', 0):.1f}%\n"
        f"Daily PnL: `{stats.get('daily_pnl', 0):+.2f}%`\n"
        f"Total PnL: `{stats.get('total_pnl_pct', 0):+.2f}%`\n"
        f"Drawdown: {stats.get('drawdown_pct', 0):.2f}%\n"
        f"Profit Factor: {stats.get('profit_factor', 0):.2f}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    log.info(msg.replace("*", "").replace("`", ""))
    _send_telegram(msg)


def alert_symbol_switch(old_symbol: str, new_symbol: str, score: float):
    """Alert when auto-scanner switches symbol."""
    msg = (
        f"🔄 *SYMBOL SWITCH*\n"
        f"`{old_symbol}` → `{new_symbol}`\n"
        f"Score: {score:.1f}"
    )
    log.info(f"Symbol switch: {old_symbol} → {new_symbol} (score={score:.1f})")
    _send_telegram(msg)


def alert_bot_start(mode: str, strategies: list[str]):
    """Alert on bot startup."""
    msg = (
        f"🚀 *BOT STARTED*\n"
        f"Mode: {mode}\n"
        f"Strategies: {', '.join(strategies)}\n"
        f"Risk: {config.RISK_PER_TRADE_PCT}% per trade\n"
        f"Daily limit: {config.DAILY_LOSS_LIMIT_PCT}%\n"
        f"Max drawdown: {config.MAX_DRAWDOWN_PCT}%\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    log.info(msg.replace("*", "").replace("`", ""))
    _send_telegram(msg)
