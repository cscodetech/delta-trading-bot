"""
dashboard.py — Centralized Trading Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Features:
  - Live positions with real-time P&L
  - Auto-close monitor (SL/TP enforcement every 10s)
  - Full trade history
  - Account stats (win rate, profit factor, drawdown)
  - Manual close button for any position
  - Auto-refresh every 5 seconds

Run:  python dashboard.py
Open: http://localhost:5000
"""

import json
import hashlib
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, date
from functools import wraps

from flask import Flask, jsonify, request, redirect, session, make_response

import config
import database as db
from exchange import DeltaClient

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
app.secret_key = config.DASHBOARD_SECRET
client = DeltaClient(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE PERSISTENCE — MySQL database
# ═══════════════════════════════════════════════════════════════════════════════

def load_trades() -> list[dict]:
    return db.get_trades(100)


def save_trade(trade: dict):
    # Calculate fees
    entry = float(trade.get("entry_price", 0))
    exit_p = float(trade.get("exit_price", 0))
    pnl = float(trade.get("pnl_pct", 0))
    fee_pct = round((config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
    trade["fee_pct"] = fee_pct
    trade["net_pnl_pct"] = round(pnl - fee_pct, 4)
    if "closed_at" not in trade:
        trade["closed_at"] = datetime.now()
    # Compute dollar PnL
    cv = client.get_contract_values()
    sym = trade.get("symbol", "")
    cval = cv.get(sym, 1)
    notional_usd = float(trade.get("size", 0)) * cval * entry
    trade["pnl_usd"] = round(notional_usd * pnl / 100, 4)
    trade["net_pnl_usd"] = round(notional_usd * trade["net_pnl_pct"] / 100, 4)
    db.insert_trade(trade)


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_cv_cache = {}
_cv_cache_time = 0

def _contract_values_cache() -> dict:
    """Cache contract values for 60s to avoid repeated API calls."""
    global _cv_cache, _cv_cache_time
    if time.time() - _cv_cache_time > 60 or not _cv_cache:
        _cv_cache = client.get_contract_values()
        _cv_cache_time = time.time()
    return _cv_cache


def _get_symbol_for_product(product_id: int) -> str:
    for p in client.get_products():
        if p["id"] == product_id:
            return p.get("symbol", str(product_id))
    return str(product_id)


def get_live_positions() -> list[dict]:
    """Fetch all active positions from exchange with live P&L."""
    try:
        raw = client.get_positions()
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return []

    positions = []
    for p in raw:
        size = int(p.get("size", 0))
        if size == 0:
            continue

        entry = float(p.get("entry_price", 0))
        mark = float(p.get("mark_price", 0) or entry)
        product_id = p.get("product_id", 0)
        symbol = _get_symbol_for_product(product_id)
        side = "BUY" if size > 0 else "SELL"
        abs_size = abs(size)

        if entry > 0:
            if side == "BUY":
                pnl_pct = (mark - entry) / entry * 100
            else:
                pnl_pct = (entry - mark) / entry * 100
        else:
            pnl_pct = 0

        # Dollar PnL
        cv = _contract_values_cache()
        cval = cv.get(symbol, 1.0)
        notional = abs_size * cval * entry
        pnl_usd = round(notional * pnl_pct / 100, 4)

        positions.append({
            "product_id": product_id,
            "symbol": symbol,
            "side": side,
            "size": abs_size,
            "entry_price": entry,
            "mark_price": mark,
            "pnl_pct": round(pnl_pct, 3),
            "pnl_usd": pnl_usd,
            "created_at": p.get("created_at", ""),
        })

    return positions


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTO-CLOSE MONITOR — background thread
# ═══════════════════════════════════════════════════════════════════════════════

class AutoCloseMonitor:
    """Watches all live positions every 10s and auto-closes if SL/TP hit."""

    def __init__(self):
        self.running = False
        self.thread = None
        self.sl_pct = config.SL_FIXED_PCT if config.SL_MODE == "fixed" else 2.0
        self.tp_pct = config.TP_FIXED_PCT if config.TP_MODE == "fixed" else 4.0
        self.last_check = ""
        self.auto_closed: list[dict] = []

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log.info("Auto-close monitor started")

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._check_positions()
            except Exception as e:
                log.error(f"Monitor error: {e}")
            time.sleep(10)

    def _check_positions(self):
        self.last_check = datetime.now().strftime("%H:%M:%S")
        positions = get_live_positions()

        for pos in positions:
            pnl = pos["pnl_pct"]

            # Auto-close on stop loss
            if pnl <= -self.sl_pct:
                log.warning(f"AUTO-CLOSE SL: {pos['symbol']} {pos['side']} "
                            f"PnL={pnl:.2f}% (limit: -{self.sl_pct}%)")
                self._close_position(pos, f"Auto SL ({pnl:.2f}%)")

            # Auto-close on take profit
            elif pnl >= self.tp_pct:
                log.info(f"AUTO-CLOSE TP: {pos['symbol']} {pos['side']} "
                         f"PnL={pnl:.2f}% (target: +{self.tp_pct}%)")
                self._close_position(pos, f"Auto TP ({pnl:.2f}%)")

    def _close_position(self, pos: dict, reason: str):
        try:
            close_side = "sell" if pos["side"] == "BUY" else "buy"
            client.place_market_order(
                pos["product_id"], close_side, pos["size"],
                symbol=pos.get("symbol", ""))

            pnl = pos["pnl_pct"]
            fee_pct = round(
                (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
            record = {
                "symbol": pos["symbol"],
                "side": pos["side"],
                "entry_price": pos["entry_price"],
                "exit_price": pos["mark_price"],
                "pnl_pct": pos["pnl_pct"],
                "fee_pct": fee_pct,
                "net_pnl_pct": round(pnl - fee_pct, 4),
                "size": pos["size"],
                "reason": reason,
                "closed_at": datetime.now(),
            }
            db.insert_trade(record)
            self.auto_closed.append(record)
            log.info(f"  Closed {pos['symbol']} — {reason}")
        except Exception as e:
            log.error(f"  Failed to auto-close {pos['symbol']}: {e}")


monitor = AutoCloseMonitor()


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT PROCESS MANAGER — start/stop bot.py as subprocess
# ═══════════════════════════════════════════════════════════════════════════════

class BotManager:
    """Manages the bot.py trading process."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.started_at: str = ""
        self.max_trades_per_day: int = config.MAX_TRADES_PER_DAY

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running:
            return
        # Write current max_trades_per_day to config before starting
        self._patch_config()
        bot_script = os.path.join(os.path.dirname(__file__), "bot.py")
        self.process = subprocess.Popen(
            [sys.executable, bot_script],
            cwd=os.path.dirname(__file__),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32" else 0,
        )
        self.started_at = datetime.now().strftime("%H:%M:%S")
        log.info(f"Bot started (PID {self.process.pid})")

    def stop(self):
        if not self.running:
            return
        log.info(f"Stopping bot (PID {self.process.pid})...")
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        except Exception as e:
            log.error(f"Failed to stop bot: {e}")
        self.process = None
        log.info("Bot stopped")

    def get_recent_logs(self, lines: int = 50) -> list[str]:
        """Read last N lines from bot.log."""
        log_file = os.path.join(os.path.dirname(__file__), "bot.log")
        if not os.path.exists(log_file):
            return []
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            return [l.rstrip() for l in all_lines[-lines:]]
        except Exception:
            return []

    def _patch_config(self):
        """Update MAX_TRADES_PER_DAY in the live config module."""
        config.MAX_TRADES_PER_DAY = self.max_trades_per_day

    def get_today_trade_count(self) -> int:
        """Count trades executed today from MySQL."""
        return db.get_today_trade_count()


bot_mgr = BotManager()


# ═══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
@login_required
def api_status():
    bal = client.get_wallet_balance()
    avail_bal = client.get_available_balance()
    stats = db.get_trade_stats()

    return jsonify({
        "balance": round(bal, 4),
        "available_balance": round(avail_bal, 4),
        "total_trades": stats["total_trades"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["win_rate"],
        "total_pnl_pct": stats["total_pnl_pct"],
        "total_pnl_usd": stats["total_pnl_usd"],
        "profit_factor": stats["profit_factor"],
        "total_fees_pct": stats["total_fees_pct"],
        "monitor_active": monitor.running,
        "last_check": monitor.last_check,
        "auto_closed_count": len(monitor.auto_closed),
        "sl_pct": monitor.sl_pct,
        "tp_pct": monitor.tp_pct,
        "testnet": config.TESTNET,
        "bot_running": bot_mgr.running,
        "bot_started_at": bot_mgr.started_at,
        "bot_pid": bot_mgr.process.pid if bot_mgr.running else None,
        "max_trades_per_day": bot_mgr.max_trades_per_day,
        "today_trades": bot_mgr.get_today_trade_count(),
    })


@app.route("/api/positions")
@login_required
def api_positions():
    return jsonify(get_live_positions())


@app.route("/api/trades")
@login_required
def api_trades():
    trades = load_trades()
    return jsonify(trades[:100])


@app.route("/api/close", methods=["POST"])
@login_required
def api_close():
    data = request.get_json()
    product_id = data.get("product_id")
    size = data.get("size")
    side = data.get("side")
    entry_price = data.get("entry_price", 0)
    mark_price = data.get("mark_price", 0)
    symbol = data.get("symbol", "")

    if not all([product_id, size, side]):
        return jsonify({"error": "Missing product_id, size, or side"}), 400

    try:
        close_side = "sell" if side.upper() == "BUY" else "buy"
        resp = client.place_market_order(
            int(product_id), close_side, int(size), symbol=symbol)

        pnl = 0
        if entry_price and mark_price and float(entry_price) > 0:
            if side.upper() == "BUY":
                pnl = (float(mark_price) - float(entry_price)) / float(entry_price) * 100
            else:
                pnl = (float(entry_price) - float(mark_price)) / float(entry_price) * 100

        fee_pct = round(
            (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
        save_trade({
            "symbol": symbol,
            "side": side.upper(),
            "entry_price": float(entry_price),
            "exit_price": float(mark_price),
            "pnl_pct": round(pnl, 3),
            "fee_pct": fee_pct,
            "net_pnl_pct": round(pnl - fee_pct, 4),
            "size": int(size),
            "reason": "Manual Close (Dashboard)",
            "closed_at": datetime.now(),
        })

        return jsonify({"ok": True, "response": str(resp)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/monitor", methods=["POST"])
@login_required
def api_monitor():
    """Toggle monitor or update SL/TP thresholds."""
    data = request.get_json() or {}
    action = data.get("action", "toggle")

    if action == "toggle":
        if monitor.running:
            monitor.stop()
        else:
            monitor.start()
    elif action == "update":
        sl = data.get("sl_pct")
        tp = data.get("tp_pct")
        if sl is not None:
            monitor.sl_pct = float(sl)
        if tp is not None:
            monitor.tp_pct = float(tp)

    return jsonify({
        "running": monitor.running,
        "sl_pct": monitor.sl_pct,
        "tp_pct": monitor.tp_pct,
    })


@app.route("/api/bot", methods=["POST"])
@login_required
def api_bot():
    """Start/stop bot, update max trades per day."""
    data = request.get_json() or {}
    action = data.get("action")

    if action == "start":
        bot_mgr.start()
    elif action == "stop":
        bot_mgr.stop()
    elif action == "toggle":
        if bot_mgr.running:
            bot_mgr.stop()
        else:
            bot_mgr.start()
    elif action == "update_settings":
        mtpd = data.get("max_trades_per_day")
        if mtpd is not None:
            bot_mgr.max_trades_per_day = max(1, int(mtpd))
            config.MAX_TRADES_PER_DAY = bot_mgr.max_trades_per_day

    return jsonify({
        "running": bot_mgr.running,
        "pid": bot_mgr.process.pid if bot_mgr.running else None,
        "started_at": bot_mgr.started_at,
        "max_trades_per_day": bot_mgr.max_trades_per_day,
    })


@app.route("/api/bot/logs")
@login_required
def api_bot_logs():
    lines = request.args.get("lines", 50, type=int)
    return jsonify(bot_mgr.get_recent_logs(min(lines, 200)))


@app.route("/api/sync-trades", methods=["POST"])
@login_required
def api_sync_trades():
    """Fetch fills from Delta Exchange and sync missing trades into DB."""
    try:
        count = db.sync_past_trades(client)
        return jsonify({"ok": True, "synced": count,
                        "message": f"Synced {count} new trade(s) from exchange"})
    except Exception as e:
        log.error(f"Trade sync failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/filter-log")
@login_required
def api_filter_log():
    """Return recent filter blocks (why trades were blocked)."""
    limit = request.args.get("limit", 50, type=int)
    rows = db.get_recent_filter_blocks(min(limit, 200))
    for r in rows:
        if "created_at" in r and r["created_at"]:
            r["created_at"] = str(r["created_at"])
    return jsonify(rows)


@app.route("/api/strategy-stats")
@login_required
def api_strategy_stats():
    """Return per-strategy performance stats."""
    rows = db.get_strategy_stats()
    for r in rows:
        for k in ("total_pnl",):
            if k in r and r[k] is not None:
                r[k] = float(r[k])
        for k in ("trades", "wins", "losses"):
            if k in r and r[k] is not None:
                r[k] = int(r[k])
    return jsonify(rows)


@app.route("/api/strategy-stats/toggle", methods=["POST"])
@login_required
def api_strategy_toggle():
    """Enable/disable a strategy in the tracker."""
    data = request.get_json() or {}
    strategy = data.get("strategy", "")
    enabled = data.get("enabled", True)
    if not strategy:
        return jsonify({"error": "Missing strategy name"}), 400
    db.set_strategy_enabled(strategy, bool(enabled))
    return jsonify({"ok": True, "strategy": strategy, "enabled": bool(enabled)})


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS API
# ═══════════════════════════════════════════════════════════════════════════════

# Keys we allow reading/writing via the settings page
_SETTINGS_KEYS = [
    "api_key", "api_secret", "testnet",
    "telegram_token", "telegram_chat_id",
    "dashboard_password",
]

# Mask secret values when reading
def _mask(val: str) -> str:
    if not val or len(val) < 8:
        return val
    return val[:4] + "•" * (len(val) - 8) + val[-4:]


@app.route("/api/settings")
@login_required
def api_settings_get():
    """Return current settings (secrets masked)."""
    return jsonify({
        "api_key": _mask(config.API_KEY),
        "api_secret": _mask(config.API_SECRET),
        "testnet": config.TESTNET,
        "telegram_token": _mask(config.TELEGRAM_TOKEN),
        "telegram_chat_id": config.TELEGRAM_CHAT_ID,
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_post():
    """Save settings to DB and update config module in-memory."""
    data = request.get_json() or {}

    saved = []

    # API Key — only save if not masked (user entered a new one)
    api_key = data.get("api_key", "").strip()
    if api_key and "•" not in api_key:
        db.set_setting("api_key", api_key)
        config.API_KEY = api_key
        saved.append("api_key")

    api_secret = data.get("api_secret", "").strip()
    if api_secret and "•" not in api_secret:
        db.set_setting("api_secret", api_secret)
        config.API_SECRET = api_secret
        saved.append("api_secret")

    # Testnet
    testnet_val = data.get("testnet", "true")
    is_testnet = testnet_val in (True, "true", "True", "1")
    db.set_setting("testnet", "1" if is_testnet else "0")
    config.TESTNET = is_testnet
    saved.append("testnet")

    # Telegram
    tg_token = data.get("telegram_token", "").strip()
    if "•" not in tg_token:
        db.set_setting("telegram_token", tg_token)
        config.TELEGRAM_TOKEN = tg_token
        config.TELEGRAM_ENABLED = bool(tg_token)
        saved.append("telegram_token")

    tg_chat = data.get("telegram_chat_id", "").strip()
    db.set_setting("telegram_chat_id", tg_chat)
    config.TELEGRAM_CHAT_ID = tg_chat
    saved.append("telegram_chat_id")

    # Dashboard password
    new_pw = data.get("dashboard_password", "")
    if new_pw:
        db.set_setting("dashboard_password", new_pw)
        config.DASHBOARD_PASSWORD = new_pw
        saved.append("dashboard_password")

    # Reinitialise the dashboard's exchange client if API keys changed
    if "api_key" in saved or "api_secret" in saved:
        global client
        client = DeltaClient(config.API_KEY, config.API_SECRET,
                             testnet=config.TESTNET)

    return jsonify({"ok": True, "saved": saved})


@app.route("/api/settings/test-connection", methods=["POST"])
@login_required
def api_test_connection():
    """Test API credentials by fetching wallet balance. Uses form values if provided."""
    data = request.get_json() or {}
    api_key = data.get("api_key", "").strip()
    api_secret = data.get("api_secret", "").strip()
    testnet_val = data.get("testnet", "true")
    # Use form values if provided and not masked, else fall back to config
    key = api_key if api_key and "\u2022" not in api_key else config.API_KEY
    secret = api_secret if api_secret and "\u2022" not in api_secret else config.API_SECRET
    is_testnet = testnet_val in (True, "true", "True", "1")
    try:
        test_client = DeltaClient(key, secret, testnet=is_testnet)
        balance = test_client.get_wallet_balance()
        return jsonify({"ok": True, "balance": balance})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/settings/test-telegram", methods=["POST"])
@login_required
def api_test_telegram():
    """Send a test Telegram message. Uses form values if provided."""
    import requests as req
    data = request.get_json() or {}
    token = data.get("telegram_token", "").strip()
    chat_id = data.get("telegram_chat_id", "").strip()
    # Fall back to config if form values are masked or empty
    if not token or "\u2022" in token:
        token = config.TELEGRAM_TOKEN
    if not chat_id:
        chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Token or Chat ID not set"}), 400
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = req.post(url, json={
            "chat_id": chat_id,
            "text": "✅ Delta Trading Bot — Test message from Settings page!",
            "parse_mode": "Markdown",
        }, timeout=10)
        if resp.status_code == 200:
            return jsonify({"ok": True})
        else:
            err = resp.json().get("description", resp.text)
            return jsonify({"ok": False, "error": err}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGIN / LOGOUT ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Login — Delta Trading</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--accent:#1f6feb;--red:#f85149}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
    background:var(--bg);color:var(--text);display:flex;justify-content:center;align-items:center;min-height:100vh}
  .login-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:40px;width:360px}
  .login-box h1{font-size:22px;margin-bottom:8px;text-align:center}
  .login-box p{color:#8b949e;font-size:13px;text-align:center;margin-bottom:24px}
  .login-box input{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
    border-radius:6px;color:var(--text);font-size:14px;margin-bottom:16px}
  .login-box button{width:100%;padding:10px;background:var(--accent);color:#fff;border:none;
    border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
  .login-box button:hover{opacity:0.9}
  .error{color:var(--red);font-size:13px;text-align:center;margin-bottom:12px}
</style>
</head>
<body>
<div class="login-box">
  <h1>🔒 Delta Trading</h1>
  <p>Enter password to access the dashboard</p>
  <div class="error" id="err"></div>
  <form onsubmit="return doLogin(event)">
    <input type="password" id="pw" placeholder="Password" autofocus>
    <button type="submit">Login</button>
  </form>
</div>
<script>
async function doLogin(e) {
  e.preventDefault();
  const r = await fetch('/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value})
  });
  if (r.ok) { window.location.href='/'; }
  else { document.getElementById('err').textContent='Wrong password'; }
  return false;
}
</script>
</body></html>"""


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect("/")
    return LOGIN_HTML


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json() or {}
    password = data.get("password", "")
    if password == config.DASHBOARD_PASSWORD:
        session["authenticated"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid password"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Delta Trading Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --blue: #58a6ff; --yellow: #d29922;
    --accent: #1f6feb;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 20px; line-height: 1.5;
  }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }
  .header h1 { font-size: 22px; }
  .header .live-dot {
    width: 10px; height: 10px; background: var(--green);
    border-radius: 50%; display: inline-block; margin-right: 8px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; } 50% { opacity: 0.4; }
  }
  .header .meta { color: var(--muted); font-size: 13px; }

  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }
  .stat-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px;
  }
  .stat-card .label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
  .stat-card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
  .stat-card .value.green { color: var(--green); }
  .stat-card .value.red { color: var(--red); }

  .section { margin-bottom: 24px; }
  .section-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
  }
  .section-header h2 { font-size: 16px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 12px; font-weight: 600;
  }
  .badge.green { background: #238636; color: #fff; }
  .badge.red { background: #da3633; color: #fff; }
  .badge.blue { background: var(--accent); color: #fff; }
  .badge.yellow { background: #9e6a03; color: #fff; }

  table {
    width: 100%; border-collapse: collapse; background: var(--card);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  }
  th {
    text-align: left; padding: 10px 14px; font-size: 12px;
    color: var(--muted); text-transform: uppercase;
    border-bottom: 1px solid var(--border); background: #0d1117;
  }
  td {
    padding: 10px 14px; font-size: 14px;
    border-bottom: 1px solid var(--border);
  }
  tr:last-child td { border-bottom: none; }
  tr:hover { background: rgba(56, 139, 253, 0.05); }

  .pnl-pos { color: var(--green); font-weight: 600; }
  .pnl-neg { color: var(--red); font-weight: 600; }
  .side-buy { color: var(--green); }
  .side-sell { color: var(--red); }

  .btn {
    padding: 6px 14px; border: none; border-radius: 6px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  .btn-red { background: #da3633; color: #fff; }
  .btn-blue { background: var(--accent); color: #fff; }
  .btn-sm { padding: 4px 10px; font-size: 12px; }

  .control-bar {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 12px;
  }
  .control-bar label { font-size: 13px; color: var(--muted); }
  .control-bar input {
    width: 70px; padding: 4px 8px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-size: 13px;
  }
  .control-bar .sep {
    width: 1px; height: 28px; background: var(--border);
  }
  .btn-green { background: #238636; color: #fff; }
  .bot-status { font-size: 12px; }
  .bot-status .dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 4px;
  }
  .bot-status .dot.on { background: var(--green); }
  .bot-status .dot.off { background: var(--red); }
  .log-box {
    background: #0d1117; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font-family: monospace; font-size: 12px;
    max-height: 250px; overflow-y: auto; white-space: pre-wrap;
    color: var(--muted); display: none; margin-bottom: 24px;
  }
  .empty-msg {
    text-align: center; padding: 40px; color: var(--muted);
    font-size: 14px;
  }
  .refresh-info {
    text-align: center; color: var(--muted); font-size: 12px;
    margin-top: 12px;
  }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1><span class="live-dot"></span> Delta Trading Dashboard</h1>
    </div>
    <div class="meta">
      <span id="clock"></span> &nbsp;|&nbsp;
      <span id="testnet-badge"></span> &nbsp;|&nbsp;
      Auto-refresh: 5s &nbsp;|&nbsp;
      <a href="/settings" style="color:var(--blue);text-decoration:none;font-weight:600">⚙ Settings</a> &nbsp;|&nbsp;
      <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600">Logout</a>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-grid" id="stats-grid"></div>

  <!-- Bot Controls -->
  <div class="control-bar">
    <strong>Trading Bot</strong>
    <button class="btn btn-sm" id="bot-toggle" onclick="toggleBot()">Loading...</button>
    <span class="bot-status" id="bot-status"></span>
    <div class="sep"></div>
    <label>Max Trades/Day: <input type="number" id="mtpd-input" step="1" min="1" max="50" value="5" onchange="updateBotSettings()"></label>
    <span style="color:var(--muted);font-size:12px" id="today-trades"></span>
    <div class="sep"></div>
    <button class="btn btn-blue btn-sm" onclick="toggleLogs()">Logs</button>
  </div>

  <!-- Monitor Controls -->
  <div class="control-bar">
    <strong>Auto-Close Monitor</strong>
    <button class="btn btn-blue btn-sm" id="monitor-toggle" onclick="toggleMonitor()">Loading...</button>
    <label>SL %: <input type="number" id="sl-input" step="0.5" min="0.5" value="2.0" onchange="updateMonitor()"></label>
    <label>TP %: <input type="number" id="tp-input" step="0.5" min="0.5" value="4.0" onchange="updateMonitor()"></label>
    <span style="color: var(--muted); font-size: 12px;" id="monitor-status"></span>
  </div>

  <!-- Bot Logs -->
  <div class="log-box" id="log-box"></div>

  <!-- Live Positions -->
  <div class="section">
    <div class="section-header">
      <h2>Live Positions</h2>
      <span class="badge blue" id="pos-count">0</span>
    </div>
    <div id="positions-table"></div>
  </div>

  <!-- Trade History -->
  <div class="section">
    <div class="section-header">
      <h2>Trade History</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <button onclick="syncTrades()" id="sync-btn" style="padding:4px 12px;background:#1f6feb;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer">Sync from Exchange</button>
        <span class="badge yellow" id="trade-count">0</span>
      </div>
    </div>
    <div id="trades-table"></div>
  </div>

  <!-- Strategy Performance -->
  <div class="section">
    <div class="section-header">
      <h2>Strategy Performance</h2>
      <span class="badge blue" id="strat-count">0</span>
    </div>
    <div id="strategy-table"></div>
  </div>

  <!-- Filter Log -->
  <div class="section">
    <div class="section-header">
      <h2>Filter Log</h2>
      <span class="badge yellow" id="filter-count">0</span>
    </div>
    <div id="filter-table" style="max-height:300px;overflow-y:auto"></div>
  </div>

  <div class="refresh-info">Dashboard updates every 5 seconds. Auto-close monitor checks every 10 seconds.</div>

<script>
const API = '';

function $(id) { return document.getElementById(id); }

function pnlClass(v) { return v >= 0 ? 'pnl-pos' : 'pnl-neg'; }
function pnlStr(v) { return (v >= 0 ? '+' : '') + v.toFixed(3) + '%'; }
function sideClass(s) { return s === 'BUY' ? 'side-buy' : 'side-sell'; }

function updateClock() {
  $('clock').textContent = new Date().toLocaleTimeString();
}

async function syncTrades() {
  const btn = $('sync-btn');
  btn.disabled = true; btn.textContent = 'Syncing...';
  try {
    const r = await fetch(API + '/api/sync-trades', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      btn.textContent = d.synced > 0 ? `Synced ${d.synced} trade(s)!` : 'Already up to date';
      refreshAll();
    } else {
      btn.textContent = 'Sync failed';
    }
  } catch(e) { btn.textContent = 'Sync error'; }
  setTimeout(() => { btn.disabled = false; btn.textContent = 'Sync from Exchange'; }, 3000);
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (r.status === 401) { window.location.href = '/login'; return null; }
    return await r.json();
  } catch(e) { console.error(url, e); return null; }
}

async function refreshStatus() {
  const s = await fetchJSON(API + '/api/status');
  if (!s) return;

  $('testnet-badge').innerHTML = s.testnet
    ? '<span class="badge yellow">TESTNET</span>'
    : '<span class="badge red">LIVE</span>';

  const pnlCls = s.total_pnl_pct >= 0 ? 'green' : 'red';

  $('stats-grid').innerHTML = `
    <div class="stat-card">
      <div class="label">Balance</div>
      <div class="value">$${s.balance.toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Available Balance</div>
      <div class="value" style="color:var(--blue)">$${(s.available_balance || 0).toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Trades</div>
      <div class="value">${s.total_trades}</div>
    </div>
    <div class="stat-card">
      <div class="label">Win Rate</div>
      <div class="value ${s.win_rate >= 50 ? 'green' : 'red'}">${s.win_rate.toFixed(1)}%</div>
    </div>
    <div class="stat-card">
      <div class="label">Total PnL</div>
      <div class="value ${pnlCls}">${s.total_pnl_pct >= 0 ? '+' : ''}${s.total_pnl_pct.toFixed(3)}%</div>
    </div>
    <div class="stat-card">
      <div class="label">Total $ PnL</div>
      <div class="value ${pnlCls}">${s.total_pnl_usd >= 0 ? '+$' : '-$'}${Math.abs(s.total_pnl_usd || 0).toFixed(4)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Fees</div>
      <div class="value red">-${(s.total_fees_pct || 0).toFixed(3)}%</div>
    </div>
    <div class="stat-card">
      <div class="label">Profit Factor</div>
      <div class="value">${s.profit_factor.toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">W / L</div>
      <div class="value"><span class="pnl-pos">${s.wins}</span> / <span class="pnl-neg">${s.losses}</span></div>
    </div>
    <div class="stat-card">
      <div class="label">Auto-Closed</div>
      <div class="value">${s.auto_closed_count}</div>
    </div>
    <div class="stat-card">
      <div class="label">SL / TP Guard</div>
      <div class="value" style="font-size:16px">-${s.sl_pct}% / +${s.tp_pct}%</div>
    </div>
  `;

  // Monitor controls
  const btn = $('monitor-toggle');
  btn.textContent = s.monitor_active ? 'ON — Stop' : 'OFF — Start';
  btn.className = s.monitor_active ? 'btn btn-red btn-sm' : 'btn btn-blue btn-sm';
  $('monitor-status').textContent = s.last_check ? `Last check: ${s.last_check}` : '';
  $('sl-input').value = s.sl_pct;
  $('tp-input').value = s.tp_pct;

  // Bot controls
  const botBtn = $('bot-toggle');
  if (s.bot_running) {
    botBtn.textContent = 'Stop Bot';
    botBtn.className = 'btn btn-red btn-sm';
    $('bot-status').innerHTML = `<span class="dot on"></span> Running (PID ${s.bot_pid}) since ${s.bot_started_at}`;
  } else {
    botBtn.textContent = 'Start Bot';
    botBtn.className = 'btn btn-green btn-sm';
    $('bot-status').innerHTML = '<span class="dot off"></span> Stopped';
  }
  $('mtpd-input').value = s.max_trades_per_day;
  $('today-trades').textContent = `Today: ${s.today_trades} / ${s.max_trades_per_day}`;
}

async function refreshPositions() {
  const pos = await fetchJSON(API + '/api/positions');
  if (!pos) return;

  $('pos-count').textContent = pos.length;

  if (pos.length === 0) {
    $('positions-table').innerHTML = '<div class="empty-msg">No open positions</div>';
    return;
  }

  let html = `<table>
    <tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>PnL</th><th>$ PnL</th><th>Action</th></tr>`;

  for (const p of pos) {
    const usd = p.pnl_usd || 0;
    const usdStr = (usd >= 0 ? '+$' : '-$') + Math.abs(usd).toFixed(4);
    html += `<tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="${sideClass(p.side)}">${p.side}</td>
      <td>${p.size}</td>
      <td>${p.entry_price.toFixed(4)}</td>
      <td>${p.mark_price.toFixed(4)}</td>
      <td class="${pnlClass(p.pnl_pct)}">${pnlStr(p.pnl_pct)}</td>
      <td class="${pnlClass(usd)}" style="font-weight:600">${usdStr}</td>
      <td><button class="btn btn-red btn-sm" onclick="closePos(${p.product_id}, ${p.size}, '${p.side}', ${p.entry_price}, ${p.mark_price}, '${p.symbol}')">Close</button></td>
    </tr>`;
  }

  html += '</table>';
  $('positions-table').innerHTML = html;
}

async function refreshTrades() {
  const trades = await fetchJSON(API + '/api/trades');
  if (!trades) return;

  $('trade-count').textContent = trades.length;

  if (trades.length === 0) {
    $('trades-table').innerHTML = '<div class="empty-msg">No trades yet</div>';
    return;
  }

  let html = `<table>
    <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Fees</th><th>Net PnL</th><th>$ PnL</th><th>Reason</th></tr>`;

  for (const t of trades) {
    const ts = t.closed_at ? new Date(t.closed_at).toLocaleString() : '-';
    const netPnl = t.net_pnl_pct != null ? t.net_pnl_pct : (t.pnl_pct || 0);
    const netUsd = t.net_pnl_usd || 0;
    const usdStr = (netUsd >= 0 ? '+$' : '-$') + Math.abs(netUsd).toFixed(4);
    html += `<tr>
      <td style="font-size:12px;color:var(--muted)">${ts}</td>
      <td><strong>${t.symbol || '-'}</strong></td>
      <td class="${sideClass(t.side)}">${t.side}</td>
      <td>${(t.entry_price || 0).toFixed(4)}</td>
      <td>${(t.exit_price || 0).toFixed(4)}</td>
      <td class="${pnlClass(t.pnl_pct)}">${pnlStr(t.pnl_pct || 0)}</td>
      <td style="color:var(--red);font-size:12px">-${(t.fee_pct || 0).toFixed(3)}%</td>
      <td class="${pnlClass(netPnl)}">${pnlStr(netPnl)}</td>
      <td class="${pnlClass(netUsd)}" style="font-weight:600">${usdStr}</td>
      <td style="font-size:12px">${t.reason || '-'}</td>
    </tr>`;
  }

  html += '</table>';
  $('trades-table').innerHTML = html;
}

async function closePos(productId, size, side, entry, mark, symbol) {
  if (!confirm(`Close ${side} ${symbol} (${size} contracts)?`)) return;
  const r = await fetch(API + '/api/close', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({product_id: productId, size, side, entry_price: entry, mark_price: mark, symbol})
  });
  const data = await r.json();
  if (data.error) alert('Error: ' + data.error);
  else refresh();
}

async function toggleMonitor() {
  await fetch(API + '/api/monitor', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'toggle'})
  });
  refreshStatus();
}

async function updateMonitor() {
  await fetch(API + '/api/monitor', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      action: 'update',
      sl_pct: parseFloat($('sl-input').value),
      tp_pct: parseFloat($('tp-input').value),
    })
  });
}

async function toggleBot() {
  await fetch(API + '/api/bot', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'toggle'})
  });
  refreshStatus();
}

async function updateBotSettings() {
  await fetch(API + '/api/bot', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      action: 'update_settings',
      max_trades_per_day: parseInt($('mtpd-input').value),
    })
  });
}

let logsVisible = false;
async function toggleLogs() {
  logsVisible = !logsVisible;
  const box = $('log-box');
  box.style.display = logsVisible ? 'block' : 'none';
  if (logsVisible) refreshLogs();
}

async function refreshLogs() {
  if (!logsVisible) return;
  const lines = await fetchJSON(API + '/api/bot/logs?lines=60');
  if (lines) $('log-box').textContent = lines.join('\n');
}

async function refreshStrategyStats() {
  const rows = await fetchJSON(API + '/api/strategy-stats');
  if (!rows) return;
  $('strat-count').textContent = rows.length;
  if (rows.length === 0) {
    $('strategy-table').innerHTML = '<div class="empty-msg">No strategy stats yet</div>';
    return;
  }
  let html = `<table><tr><th>Strategy</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>PnL</th><th>Enabled</th><th>Action</th></tr>`;
  for (const s of rows) {
    const wr = s.trades > 0 ? (s.wins / s.trades * 100) : 0;
    const pnl = s.total_pnl || 0;
    const enabled = s.enabled !== 0 && s.enabled !== false;
    html += `<tr>
      <td><strong>${s.strategy}</strong></td>
      <td>${s.trades}</td>
      <td class="pnl-pos">${s.wins}</td>
      <td class="pnl-neg">${s.losses}</td>
      <td class="${wr >= 50 ? 'pnl-pos' : 'pnl-neg'}">${wr.toFixed(1)}%</td>
      <td class="${pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(3)}%</td>
      <td>${enabled ? '<span style="color:var(--green)">Yes</span>' : '<span style="color:var(--red)">No</span>'}</td>
      <td><button class="btn ${enabled ? 'btn-red' : 'btn-green'} btn-sm" onclick="toggleStrategy('${s.strategy}', ${!enabled})">${enabled ? 'Disable' : 'Enable'}</button></td>
    </tr>`;
  }
  html += '</table>';
  $('strategy-table').innerHTML = html;
}

async function toggleStrategy(name, enable) {
  await fetch(API + '/api/strategy-stats/toggle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({strategy: name, enabled: enable})
  });
  refreshStrategyStats();
}

async function refreshFilterLog() {
  const rows = await fetchJSON(API + '/api/filter-log?limit=50');
  if (!rows) return;
  $('filter-count').textContent = rows.length;
  if (rows.length === 0) {
    $('filter-table').innerHTML = '<div class="empty-msg">No filter blocks recorded yet</div>';
    return;
  }
  let html = `<table><tr><th>Time</th><th>Symbol</th><th>Filter</th><th>Detail</th></tr>`;
  for (const r of rows) {
    const ts = r.created_at ? new Date(r.created_at).toLocaleTimeString() : '-';
    html += `<tr>
      <td style="font-size:12px;color:var(--muted)">${ts}</td>
      <td><strong>${r.symbol}</strong></td>
      <td><span class="badge red" style="font-size:11px">${r.filter_name}</span></td>
      <td style="font-size:12px">${r.detail || '-'}</td>
    </tr>`;
  }
  html += '</table>';
  $('filter-table').innerHTML = html;
}

async function refresh() {
  await Promise.all([refreshStatus(), refreshPositions(), refreshTrades(),
                     refreshStrategyStats(), refreshFilterLog()]);
  if (logsVisible) refreshLogs();
}

updateClock();
refresh();
setInterval(updateClock, 1000);
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.route("/")
@login_required
def dashboard():
    return DASHBOARD_HTML


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS PAGE
# ═══════════════════════════════════════════════════════════════════════════════

SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Settings — Delta Trading Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --blue: #58a6ff; --accent: #1f6feb;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 20px; line-height: 1.5;
  }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }
  .header h1 { font-size: 22px; }
  .header .meta { color: var(--muted); font-size: 13px; }
  .section {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 20px; margin-bottom: 20px;
  }
  .section h2 { font-size: 16px; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
  .form-group input, .form-group select {
    width: 100%; max-width: 500px; padding: 8px 12px;
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; font-size: 14px;
  }
  .form-group input:focus, .form-group select:focus { outline: none; border-color: var(--accent); }
  .form-row { display: flex; gap: 16px; flex-wrap: wrap; }
  .form-row .form-group { flex: 1; min-width: 200px; }
  .btn {
    padding: 8px 20px; border: none; border-radius: 6px;
    cursor: pointer; font-size: 14px; font-weight: 600;
  }
  .btn-blue { background: var(--accent); color: #fff; }
  .btn-blue:hover { background: #388bfd; }
  .btn-green { background: var(--green); color: #fff; }
  .btn-red { background: var(--red); color: #fff; }
  .msg { padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; display: none; }
  .msg.ok { background: #0d2818; border: 1px solid var(--green); color: var(--green); }
  .msg.err { background: #2d0c0c; border: 1px solid var(--red); color: var(--red); }
  .test-result { margin-left: 12px; font-size: 13px; }
  .hint { font-size: 11px; color: var(--muted); margin-top: 2px; }
</style>
</head>
<body>
  <div class="header">
    <div><h1>⚙ Settings</h1></div>
    <div class="meta">
      <a href="/" style="color:var(--blue);text-decoration:none;font-weight:600">← Back to Dashboard</a>
      &nbsp;|&nbsp;
      <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600">Logout</a>
    </div>
  </div>

  <div id="msg" class="msg"></div>

  <!-- Delta Exchange API -->
  <div class="section">
    <h2>Delta Exchange API</h2>
    <div class="form-row">
      <div class="form-group">
        <label>API Key</label>
        <input type="text" id="api_key" placeholder="Your Delta Exchange API key">
      </div>
      <div class="form-group">
        <label>API Secret</label>
        <input type="password" id="api_secret" placeholder="Your Delta Exchange API secret">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Mode</label>
        <select id="testnet">
          <option value="true">Testnet</option>
          <option value="false">Live (Real Money)</option>
        </select>
      </div>
    </div>
    <div style="margin-top:12px">
      <button class="btn btn-blue" onclick="testConnection()">Test Connection</button>
      <span id="conn-result" class="test-result"></span>
    </div>
    <div class="hint" style="margin-top:8px">⚠ Changing API credentials requires bot restart to take effect.</div>
  </div>

  <!-- Telegram Alerts -->
  <div class="section">
    <h2>Telegram Alerts</h2>
    <div class="form-row">
      <div class="form-group">
        <label>Bot Token</label>
        <input type="text" id="telegram_token" placeholder="123456789:ABCdefGHI...">
        <div class="hint">Get from @BotFather on Telegram</div>
      </div>
      <div class="form-group">
        <label>Chat ID</label>
        <input type="text" id="telegram_chat_id" placeholder="-1001234567890">
        <div class="hint">Get from @userinfobot or @RawDataBot</div>
      </div>
    </div>
    <div style="margin-top:12px">
      <button class="btn btn-blue" onclick="testTelegram()">Send Test Message</button>
      <span id="tg-result" class="test-result"></span>
    </div>
  </div>

  <!-- Dashboard Settings -->
  <div class="section">
    <h2>Dashboard</h2>
    <div class="form-row">
      <div class="form-group">
        <label>Dashboard Password</label>
        <input type="password" id="dashboard_password" placeholder="Enter new password (leave blank to keep)">
        <div class="hint">Leave blank to keep current password</div>
      </div>
    </div>
  </div>

  <div style="display:flex;gap:12px;margin-top:8px">
    <button class="btn btn-green" onclick="saveSettings()">Save All Settings</button>
  </div>

<script>
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    document.getElementById('api_key').value = d.api_key || '';
    document.getElementById('api_secret').value = d.api_secret || '';
    document.getElementById('testnet').value = d.testnet ? 'true' : 'false';
    document.getElementById('telegram_token').value = d.telegram_token || '';
    document.getElementById('telegram_chat_id').value = d.telegram_chat_id || '';
  } catch(e) { showMsg('Failed to load settings', true); }
}

function showMsg(text, isError) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + (isError ? 'err' : 'ok');
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 5000);
}

async function saveSettings() {
  const payload = {
    api_key: document.getElementById('api_key').value.trim(),
    api_secret: document.getElementById('api_secret').value.trim(),
    testnet: document.getElementById('testnet').value,
    telegram_token: document.getElementById('telegram_token').value.trim(),
    telegram_chat_id: document.getElementById('telegram_chat_id').value.trim(),
    dashboard_password: document.getElementById('dashboard_password').value,
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.ok) {
      showMsg('Settings saved! API/Telegram changes take effect immediately. Restart bot for API key changes.', false);
      document.getElementById('dashboard_password').value = '';
    } else {
      showMsg(d.error || 'Save failed', true);
    }
  } catch(e) { showMsg('Network error', true); }
}

async function testConnection() {
  const el = document.getElementById('conn-result');
  el.textContent = 'Testing...';
  el.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/settings/test-connection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        api_key: document.getElementById('api_key').value.trim(),
        api_secret: document.getElementById('api_secret').value.trim(),
        testnet: document.getElementById('testnet').value
      })
    });
    const d = await r.json();
    if (d.ok) {
      el.textContent = '✓ Connected! Balance: $' + (d.balance || 0).toFixed(2);
      el.style.color = 'var(--green)';
    } else {
      el.textContent = '✗ ' + (d.error || 'Connection failed');
      el.style.color = 'var(--red)';
    }
  } catch(e) { el.textContent = '✗ Network error'; el.style.color = 'var(--red)'; }
}

async function testTelegram() {
  const el = document.getElementById('tg-result');
  el.textContent = 'Sending...';
  el.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/settings/test-telegram', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        telegram_token: document.getElementById('telegram_token').value.trim(),
        telegram_chat_id: document.getElementById('telegram_chat_id').value.trim()
      })
    });
    const d = await r.json();
    if (d.ok) {
      el.textContent = '✓ Test message sent!';
      el.style.color = 'var(--green)';
    } else {
      el.textContent = '✗ ' + (d.error || 'Failed');
      el.style.color = 'var(--red)';
    }
  } catch(e) { el.textContent = '✗ Network error'; el.style.color = 'var(--red)'; }
}

loadSettings();
</script>
</body>
</html>"""


@app.route("/settings")
@login_required
def settings_page():
    return SETTINGS_HTML


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    monitor.start()
    print("=" * 50)
    print("  Delta Trading Dashboard")
    print(f"  URL: http://localhost:5050")
    print(f"  Mode: {'TESTNET' if config.TESTNET else 'LIVE'}")
    print(f"  Auto-close: SL={monitor.sl_pct}% / TP={monitor.tp_pct}%")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5050, debug=False)
