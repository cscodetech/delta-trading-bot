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
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, date
from functools import wraps

from flask import Flask, jsonify, request, redirect, session, g
from werkzeug.security import check_password_hash, generate_password_hash
import requests

import config
import database as db
from exchange import DeltaClient

log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
app.secret_key = config.DASHBOARD_SECRET


#  AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def current_user_id() -> int:
    try:
        return int(session.get("user_id") or 0)
    except Exception:
        return 0


def current_is_admin() -> bool:
    return bool(session.get("is_admin"))


def _parse_bool(val) -> bool:
    return val in (True, "true", "True", "1", 1, "yes", "on")


def client_for_user(user_id: int) -> DeltaClient:
    uid = int(user_id or 0)
    api_key = (db.get_user_setting(uid, "api_key") or "").strip()
    api_secret = (db.get_user_setting(uid, "api_secret") or "").strip()
    testnet_raw = (db.get_user_setting(uid, "testnet", "1") or "1").strip()
    is_testnet = _parse_bool(testnet_raw)
    if not api_key or not api_secret:
        raise RuntimeError("API key/secret not set. Go to Settings and save your exchange credentials.")
    return DeltaClient(api_key, api_secret, testnet=is_testnet)


def current_client() -> DeltaClient:
    uid = current_user_id()
    if uid <= 0:
        raise RuntimeError("Not logged in")
    cached_uid = getattr(g, "_delta_client_uid", None)
    if getattr(g, "_delta_client", None) is not None and cached_uid == uid:
        return g._delta_client
    c = client_for_user(uid)
    g._delta_client = c
    g._delta_client_uid = uid
    return c


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE PERSISTENCE — MySQL database
# ═══════════════════════════════════════════════════════════════════════════════

def load_trades(user_id: int | None = None) -> list[dict]:
    uid = current_user_id() if user_id is None else int(user_id or 0)
    return db.get_trades(100, user_id=uid)


def save_trade(trade: dict, exchange_client: DeltaClient | None = None, user_id: int | None = None):
    # Calculate fees
    uid = current_user_id() if user_id is None else int(user_id or 0)
    trade["user_id"] = uid
    exchange_client = exchange_client or current_client()
    entry = float(trade.get("entry_price", 0))
    # exit_p = float(trade.get("exit_price", 0))  # Removed unused variable
    pnl = float(trade.get("pnl_pct", 0))
    fee_pct = round((config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
    trade["fee_pct"] = fee_pct
    trade["net_pnl_pct"] = round(pnl - fee_pct, 4)
    if "closed_at" not in trade:
        trade["closed_at"] = datetime.now()
    # Compute dollar PnL
    cv = _contract_values_cache(exchange_client, uid)
    sym = trade.get("symbol", "")
    cval = cv.get(sym, 1)
    notional_usd = float(trade.get("size", 0)) * cval * entry
    trade["pnl_usd"] = round(notional_usd * pnl / 100, 4)
    trade["net_pnl_usd"] = round(notional_usd * trade["net_pnl_pct"] / 100, 4)
    db.insert_trade(trade)


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_cv_cache_by_user: dict[int, dict] = {}
_cv_cache_time_by_user: dict[int, float] = {}


def _contract_values_cache(exchange_client: DeltaClient, user_id: int) -> dict:
    """Cache contract values per user for 60s to avoid repeated API calls."""
    uid = int(user_id or 0)
    last = _cv_cache_time_by_user.get(uid, 0)
    cached = _cv_cache_by_user.get(uid) or {}
    if time.time() - last > 60 or not cached:
        cached = exchange_client.get_contract_values()
        _cv_cache_by_user[uid] = cached
        _cv_cache_time_by_user[uid] = time.time()
    return cached


_products_cache_by_user: dict[int, list[dict]] = {}
_products_cache_time_by_user: dict[int, float] = {}


def _products_cache(exchange_client: DeltaClient, user_id: int) -> list[dict]:
    uid = int(user_id or 0)
    last = _products_cache_time_by_user.get(uid, 0)
    cached = _products_cache_by_user.get(uid) or []
    if time.time() - last > 60 or not cached:
        cached = exchange_client.get_products()
        _products_cache_by_user[uid] = cached
        _products_cache_time_by_user[uid] = time.time()
    return cached


def _get_symbol_for_product(exchange_client: DeltaClient, user_id: int, product_id: int) -> str:
    for p in _products_cache(exchange_client, user_id):
        if p["id"] == product_id:
            return p.get("symbol", str(product_id))
    return str(product_id)


def get_live_positions(exchange_client: DeltaClient, user_id: int) -> list[dict]:
    """Fetch all active positions from exchange with live P&L."""
    try:
        raw = exchange_client.get_positions()
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
        symbol = _get_symbol_for_product(exchange_client, user_id, product_id)
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
        cv = _contract_values_cache(exchange_client, user_id)
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
        self.user_id: int = 0
        self.client: DeltaClient | None = None
        self.sl_pct = config.SL_FIXED_PCT if config.SL_MODE == "fixed" else 2.0
        self.tp_pct = config.TP_FIXED_PCT if config.TP_MODE == "fixed" else 4.0
        self.last_check = ""
        self.auto_closed: list[dict] = []

    def start(self, user_id: int | None = None):
        if self.running:
            return
        uid = int(user_id or 0)
        if uid <= 0:
            raise RuntimeError("Monitor requires a logged-in user context")
        self.user_id = uid
        self.client = client_for_user(uid)
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log.info("Auto-close monitor started")

    def stop(self):
        self.running = False
        self.client = None

    def _loop(self):
        while self.running:
            try:
                self._check_positions()
            except Exception as e:
                log.error(f"Monitor error: {e}")
            time.sleep(10)

    def _check_positions(self):
        self.last_check = datetime.now().strftime("%H:%M:%S")

        # Safety: never fight the main bot process
        try:
            if globals().get("bot_mgr") and bot_mgr.running:
                return
        except Exception:
            pass

        if not self.client:
            return
        positions = get_live_positions(self.client, self.user_id)

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
            if not self.client:
                return
            close_side = "sell" if pos["side"] == "BUY" else "buy"
            resp = self.client.place_market_order(
                pos["product_id"], close_side, pos["size"],
                symbol=pos.get("symbol", ""))

            exit_price = float(pos.get("mark_price") or 0)
            try:
                avg_fill = float((resp.get("result", {}) or {}).get("average_fill_price", 0) or 0)
                if avg_fill > 0:
                    exit_price = avg_fill
            except Exception:
                pass

            entry_price = float(pos.get("entry_price") or 0)
            pnl = 0.0
            if entry_price > 0 and exit_price > 0:
                if pos["side"] == "BUY":
                    pnl = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - exit_price) / entry_price * 100
            fee_pct = round(
                (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
            record = {
                "user_id": self.user_id,
                "symbol": pos["symbol"],
                "side": pos["side"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": round(pnl, 4),
                "fee_pct": fee_pct,
                "net_pnl_pct": round(pnl - fee_pct, 4),
                "size": pos["size"],
                "reason": reason,
                "closed_at": datetime.now(),
            }
            save_trade(record, exchange_client=self.client, user_id=self.user_id)
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
        self.user_id: int = 0
        self.started_by_user_id: int = 0
        self.started_by_username: str = ""
        self.max_trades_per_day: int = config.MAX_TRADES_PER_DAY

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, user_id: int, started_by_username: str = ""):
        if self.running:
            return
        self.user_id = int(user_id or 0)
        self.started_by_user_id = int(user_id or 0)
        self.started_by_username = started_by_username or ""
        bot_script = os.path.join(os.path.dirname(__file__), "bot.py")
        env = os.environ.copy()
        env["BOT_USER_ID"] = str(self.user_id)
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, bot_script],
            cwd=os.path.dirname(__file__),
            env=env,
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
        self.user_id = 0
        self.started_by_user_id = 0
        self.started_by_username = ""
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

    def get_today_trade_count(self) -> int:
        """Count trades closed today for the bot's current user_id."""
        return db.get_today_trade_count(user_id=self.user_id or None)


bot_mgr = BotManager()

_public_ip_cache: dict[str, object] = {"ts": 0.0, "ip": ""}


def _get_public_ip() -> str:
    """Best-effort: resolve this server's public egress IP (useful for API IP whitelists)."""
    now = time.time()
    try:
        ts = float(_public_ip_cache.get("ts") or 0.0)
        cached_ip = str(_public_ip_cache.get("ip") or "")
    except Exception:
        ts = 0.0
        cached_ip = ""

    if cached_ip and (now - ts) < 300:
        return cached_ip

    ip = ""
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=5)
        if r.status_code == 200:
            ip = str((r.json() or {}).get("ip") or "").strip()
    except Exception:
        ip = ""

    _public_ip_cache["ts"] = now
    _public_ip_cache["ip"] = ip
    return ip


@app.route("/api/server-ip", methods=["GET"])
@login_required
def api_server_ip():
    """
    Helps debug Delta API auth / IP whitelist issues.
    - public_ip: this server's egress IP (best-effort)
    - client_ip: the browser's IP as seen by the server (may be proxy/localhost)
    """
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    client_ip = (xff.split(",")[0].strip() if xff else (request.remote_addr or "")).strip()
    return jsonify({
        "public_ip": _get_public_ip(),
        "client_ip": client_ip,
        "x_forwarded_for": xff,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status", methods=["GET"])
@login_required
def api_status():
    uid = current_user_id()
    stats = db.get_trade_stats(user_id=uid)

    bal = 0.0
    avail_bal = 0.0
    err = ""
    try:
        c = current_client()
        bal = float(c.get_wallet_balance() or 0)
        avail_bal = float(c.get_available_balance() or 0)
    except Exception as e:
        msg = str(e)
        if ("401" in msg and "Unauthorized" in msg) or ("403" in msg and "Forbidden" in msg):
            ip = _get_public_ip()
            ip_note = f" Server IP: {ip}" if ip else ""
            msg = (
                "Delta API Unauthorized. Check API key/secret and Testnet/Live mode."
                " If you enabled IP whitelist in Delta, add this server IP."
                + ip_note
            )
        err = msg

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
        "monitor_active": bool(monitor.running and monitor.user_id in (0, uid)),
        "last_check": monitor.last_check if (monitor.running and monitor.user_id in (0, uid)) else "",
        "auto_closed_count": sum(1 for t in monitor.auto_closed if int(t.get("user_id") or 0) == uid),
        "sl_pct": monitor.sl_pct,
        "tp_pct": monitor.tp_pct,
        "testnet": _parse_bool(db.get_user_setting(uid, "testnet", "1") or "1"),
        "bot_running": bot_mgr.running,
        "bot_started_at": bot_mgr.started_at,
        "bot_pid": bot_mgr.process.pid if bot_mgr.running else None,
        "bot_user_id": bot_mgr.user_id,
        "bot_started_by_user_id": bot_mgr.started_by_user_id,
        "bot_started_by_username": bot_mgr.started_by_username,
        "max_trades_per_day": bot_mgr.max_trades_per_day,
        "today_trades": db.get_today_trade_count(user_id=uid),
        "user_id": uid,
        "username": session.get("username") or "",
        "error": err,
    })


@app.route("/api/positions", methods=["GET"])
@login_required
def api_positions():
    try:
        uid = current_user_id()
        return jsonify(get_live_positions(current_client(), uid))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/orders", methods=["GET"])
@login_required
def api_orders():
    """Return pending/open orders tracked in the DB (for Dashboard display)."""
    try:
        rows = db.get_pending_orders(user_id=current_user_id())
        out = []
        for r in rows:
            for k in ("created_at", "updated_at"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k].isoformat()
            out.append({
                "order_id": r.get("order_id"),
                "product_id": r.get("product_id"),
                "symbol": r.get("symbol") or "",
                "side": r.get("side") or "",
                "order_type": r.get("order_type") or "",
                "size": r.get("size") or 0,
                "price": r.get("price"),
                "status": r.get("status") or "",
                "fill_price": r.get("fill_price"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/orders/cancel", methods=["POST"])
@login_required
def api_cancel_order():
    """Cancel a pending/open order by order_id + product_id."""
    data = request.get_json() or {}
    order_id = int(data.get("order_id") or 0)
    product_id = int(data.get("product_id") or 0)
    if not order_id or not product_id:
        return jsonify({"ok": False, "error": "Missing order_id or product_id"}), 400
    try:
        uid = current_user_id()
        row = db.get_order_by_id(order_id)
        if not row:
            return jsonify({"ok": False, "error": "Order not found"}), 404
        if int(row.get("user_id") or 0) not in (0, uid):
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        resp = current_client().cancel_order(order_id, product_id)
        try:
            db.update_order_status(order_id, "cancelled")
        except Exception:
            pass
        return jsonify({"ok": True, "result": resp.get("result") if isinstance(resp, dict) else resp})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/trades", methods=["GET"])
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
        resp = current_client().place_market_order(
            int(product_id), close_side, int(size), symbol=symbol)

        exit_price = float(mark_price or 0)
        try:
            avg_fill = float((resp.get("result", {}) or {}).get("average_fill_price", 0) or 0)
            if avg_fill > 0:
                exit_price = avg_fill
        except Exception:
            pass

        pnl = 0.0
        if entry_price and exit_price and float(entry_price) > 0:
            if side.upper() == "BUY":
                pnl = (float(exit_price) - float(entry_price)) / float(entry_price) * 100
            else:
                pnl = (float(entry_price) - float(exit_price)) / float(entry_price) * 100

        fee_pct = round(
            (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) * 2 / 100 * 100, 4)
        save_trade({
            "symbol": symbol,
            "side": side.upper(),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
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
    uid = current_user_id()

    if action == "toggle":
        if monitor.running:
            if not (current_is_admin() or monitor.user_id in (0, uid)):
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            monitor.stop()
        else:
            try:
                monitor.start(user_id=uid)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400
    elif action == "update":
        if monitor.running and not (current_is_admin() or monitor.user_id in (0, uid)):
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        sl = data.get("sl_pct")
        tp = data.get("tp_pct")
        if sl is not None:
            monitor.sl_pct = float(sl)
        if tp is not None:
            monitor.tp_pct = float(tp)

    return jsonify({
        "ok": True,
        "running": monitor.running and monitor.user_id in (0, uid),
        "user_id": monitor.user_id,
        "sl_pct": monitor.sl_pct,
        "tp_pct": monitor.tp_pct,
    })


@app.route("/api/bot", methods=["POST"])
@login_required
def api_bot():
    """Start/stop bot, update max trades per day."""
    data = request.get_json() or {}
    action = data.get("action")
    uid = current_user_id()
    uname = session.get("username") or ""

    if action == "start":
        if bot_mgr.running:
            return jsonify({"ok": False, "error": "Bot already running"}), 400
        if not (db.get_user_setting(uid, "api_key") and db.get_user_setting(uid, "api_secret")):
            return jsonify({"ok": False, "error": "Add API Key & Secret in Settings first"}), 400
        bot_mgr.start(uid, started_by_username=uname)
    elif action == "stop":
        if bot_mgr.running and not (current_is_admin() or bot_mgr.started_by_user_id in (0, uid)):
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        bot_mgr.stop()
    elif action == "toggle":
        if bot_mgr.running:
            if not (current_is_admin() or bot_mgr.started_by_user_id in (0, uid)):
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            bot_mgr.stop()
        else:
            if not (db.get_user_setting(uid, "api_key") and db.get_user_setting(uid, "api_secret")):
                return jsonify({"ok": False, "error": "Add API Key & Secret in Settings first"}), 400
            bot_mgr.start(uid, started_by_username=uname)
    elif action == "update_settings":
        mtpd = data.get("max_trades_per_day")
        if mtpd is not None:
            bot_mgr.max_trades_per_day = max(1, int(mtpd))
            try:
                db.set_user_setting(uid, "max_trades_per_day", str(bot_mgr.max_trades_per_day))
            except Exception:
                pass

    return jsonify({
        "ok": True,
        "running": bot_mgr.running,
        "pid": bot_mgr.process.pid if bot_mgr.running else None,
        "started_at": bot_mgr.started_at,
        "max_trades_per_day": bot_mgr.max_trades_per_day,
        "bot_user_id": bot_mgr.user_id,
        "started_by_user_id": bot_mgr.started_by_user_id,
        "started_by_username": bot_mgr.started_by_username,
    })


@app.route("/api/bot/logs", methods=["GET"])
@login_required
def api_bot_logs():
    lines = request.args.get("lines", 50, type=int)
    return jsonify(bot_mgr.get_recent_logs(min(lines, 200)))


@app.route("/api/sync-trades", methods=["POST"])
@login_required
def api_sync_trades():
    """Fetch fills from Delta Exchange and sync missing trades into DB."""
    try:
        uid = current_user_id()
        count = db.sync_past_trades(current_client(), user_id=uid)
        return jsonify({"ok": True, "synced": count,
                        "message": f"Synced {count} new trade(s) from exchange"})
    except Exception as e:
        log.error(f"Trade sync failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/filter-log", methods=["GET"])
@login_required
def api_filter_log():
    """Return recent filter blocks (why trades were blocked)."""
    limit = request.args.get("limit", 50, type=int)
    rows = db.get_recent_filter_blocks(min(limit, 200), user_id=current_user_id())
    for r in rows:
        if "created_at" in r and r["created_at"]:
            r["created_at"] = str(r["created_at"])
    return jsonify(rows)


@app.route("/api/strategy-stats", methods=["GET"])
@login_required
def api_strategy_stats():
    """Return per-strategy performance stats."""
    rows = db.get_strategy_stats(user_id=current_user_id())
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
    db.set_strategy_enabled(strategy, bool(enabled), user_id=current_user_id())
    return jsonify({"ok": True, "strategy": strategy, "enabled": bool(enabled)})


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS API
# ═══════════════════════════════════════════════════════════════════════════════

# Keys we allow reading/writing via the settings page
_SETTINGS_KEYS = [
    "api_key", "api_secret", "testnet",
    "telegram_token", "telegram_chat_id",
    "dashboard_password",
    "anthropic_api_key", "ai_enabled",
]

# Mask secret values when reading
def _mask(val: str) -> str:
    if not val or len(val) < 8:
        return val
    return val[:4] + "\u2022" * (len(val) - 8) + val[-4:]


def _is_masked(val: str) -> bool:
    v = val or ""
    return "\u2022" in v


def _normalize_ai_provider(val: str) -> str:
    v = (val or "").strip().lower()
    return v if v in ("claude", "gemini", "both") else "claude"


@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get():
    """Return current settings (secrets masked)."""
    uid = current_user_id()
    ai_provider = _normalize_ai_provider(db.get_user_setting(uid, "ai_provider") or "claude")
    try:
        if (db.get_user_setting(uid, "ai_provider") or "").strip().lower() != ai_provider:
            db.set_user_setting(uid, "ai_provider", ai_provider)
    except Exception:
        pass
    return jsonify({
        "api_key": _mask(db.get_user_setting(uid, "api_key") or ""),
        "api_secret": _mask(db.get_user_setting(uid, "api_secret") or ""),
        "testnet": _parse_bool(db.get_user_setting(uid, "testnet", "1") or "1"),
        "telegram_token": _mask(db.get_user_setting(uid, "telegram_token") or ""),
        "telegram_chat_id": db.get_user_setting(uid, "telegram_chat_id") or "",
        "anthropic_api_key": _mask(db.get_user_setting(uid, "anthropic_api_key") or ""),
        "gemini_api_key": _mask(db.get_user_setting(uid, "gemini_api_key") or ""),
        "ai_provider": ai_provider,
        "ai_enabled": _parse_bool(db.get_user_setting(uid, "ai_enabled", "0") or "0"),
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_post():
    """Save user settings to DB."""
    data = request.get_json() or {}
    uid = current_user_id()

    saved = []

    # API Key — only save if not masked (user entered a new one)
    api_key = data.get("api_key", "").strip()
    if api_key == "":
        db.set_user_setting(uid, "api_key", "")
        saved.append("api_key")
    elif not _is_masked(api_key):
        db.set_user_setting(uid, "api_key", api_key)
        saved.append("api_key")

    api_secret = data.get("api_secret", "").strip()
    if api_secret == "":
        db.set_user_setting(uid, "api_secret", "")
        saved.append("api_secret")
    elif not _is_masked(api_secret):
        db.set_user_setting(uid, "api_secret", api_secret)
        saved.append("api_secret")

    # Testnet
    testnet_val = data.get("testnet", "true")
    is_testnet = testnet_val in (True, "true", "True", "1")
    db.set_user_setting(uid, "testnet", "1" if is_testnet else "0")
    saved.append("testnet")

    # Telegram
    tg_token = data.get("telegram_token", "").strip()
    if tg_token == "":
        db.set_user_setting(uid, "telegram_token", "")
        saved.append("telegram_token")
    elif not _is_masked(tg_token):
        db.set_user_setting(uid, "telegram_token", tg_token)
        saved.append("telegram_token")

    tg_chat = data.get("telegram_chat_id", "").strip()
    db.set_user_setting(uid, "telegram_chat_id", tg_chat)
    saved.append("telegram_chat_id")

    # Account password (current user)
    new_pw = data.get("dashboard_password", "")
    if new_pw:
        db.update_user_password(uid, generate_password_hash(new_pw))
        saved.append("password")

    anthropic_key = data.get("anthropic_api_key", "").strip()
    if anthropic_key == "":
        db.set_user_setting(uid, "anthropic_api_key", "")
        saved.append("anthropic_api_key")
    elif not _is_masked(anthropic_key):
        db.set_user_setting(uid, "anthropic_api_key", anthropic_key)
        saved.append("anthropic_api_key")

    gemini_key = data.get("gemini_api_key", "").strip()
    if gemini_key == "":
        db.set_user_setting(uid, "gemini_api_key", "")
        saved.append("gemini_api_key")
    elif not _is_masked(gemini_key):
        db.set_user_setting(uid, "gemini_api_key", gemini_key)
        saved.append("gemini_api_key")

    ai_provider = data.get("ai_provider", "claude")
    if ai_provider in ("claude", "gemini", "both"):
        db.set_user_setting(uid, "ai_provider", ai_provider)
        saved.append("ai_provider")

    ai_enabled = data.get("ai_enabled", False)
    ai_val = "1" if ai_enabled in (True, "true", "1") else "0"
    db.set_user_setting(uid, "ai_enabled", ai_val)
    saved.append("ai_enabled")

    return jsonify({"ok": True, "saved": saved})


@app.route("/api/settings/test-connection", methods=["POST"])
@login_required
def api_test_connection():
    """Test API credentials by fetching wallet balance. Uses form values if provided."""
    data = request.get_json() or {}
    uid = current_user_id()
    api_key = data.get("api_key", "").strip()
    api_secret = data.get("api_secret", "").strip()
    testnet_val = data.get("testnet", "true")
    # Use form values if provided and not masked, else fall back to stored user settings
    stored_key = (db.get_user_setting(uid, "api_key") or "").strip()
    stored_secret = (db.get_user_setting(uid, "api_secret") or "").strip()
    key = api_key if api_key and not _is_masked(api_key) else stored_key
    secret = api_secret if api_secret and not _is_masked(api_secret) else stored_secret
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
    uid = current_user_id()
    token = data.get("telegram_token", "").strip()
    chat_id = data.get("telegram_chat_id", "").strip()
    # Fall back to stored user settings if form values are masked or empty
    if not token or _is_masked(token):
        token = db.get_user_setting(uid, "telegram_token") or ""
    if not chat_id:
        chat_id = db.get_user_setting(uid, "telegram_chat_id") or ""
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


@app.route("/api/settings/test-ai", methods=["POST"])
@login_required
def api_test_ai():
    """Test Anthropic API key by making a minimal API call."""
    data = request.get_json() or {}
    uid = current_user_id()
    key = data.get("anthropic_api_key", "").strip()
    if not key or _is_masked(key):
        key = db.get_user_setting(uid, "anthropic_api_key") or ""
    if not key:
        return jsonify({"ok": False, "error": "No Anthropic API key set"}), 400
    try:
        import anthropic as _anthropic
        c = _anthropic.Anthropic(api_key=key)
        resp = c.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=20,
            messages=[{"role": "user", "content": "Reply with OK only"}],
        )
        reply = resp.content[0].text.strip()
        return jsonify({"ok": True, "reply": reply})
    except ImportError:
        return jsonify({"ok": False, "error": "anthropic SDK not installed — run: pip install anthropic"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/settings/test-gemini", methods=["POST"])
@login_required
def api_test_gemini():
    """Test Google Gemini API key."""
    data = request.get_json() or {}
    uid = current_user_id()
    key = data.get("gemini_api_key", "").strip()
    if not key or _is_masked(key):
        key = db.get_user_setting(uid, "gemini_api_key") or ""
    if not key:
        return jsonify({"ok": False, "error": "No Gemini API key set"}), 400
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel("gemini-2.0-flash-lite")
        resp = model.generate_content("Reply with OK only")
        return jsonify({"ok": True, "reply": resp.text.strip()})
    except ImportError:
        return jsonify({"ok": False, "error": "google-generativeai not installed — run: pip install google-generativeai"}), 400
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
  .links{margin-top:14px;text-align:center;font-size:13px}
  .links a{color:var(--accent);text-decoration:none}
</style>
</head>
<body>
<div class="login-box">
  <h1>🔒 Delta Trading</h1>
  <p>Login to access the dashboard</p>
  <div class="error" id="err"></div>
  <form onsubmit="return doLogin(event)">
    <input type="text" id="username" placeholder="Username" autocomplete="username" autofocus>
    <input type="password" id="pw" placeholder="Password" autocomplete="current-password">
    <button type="submit">Login</button>
  </form>
  <div class="links"><a href="/register">Create account</a></div>
</div>
<script>
async function doLogin(e) {
  e.preventDefault();
  const r = await fetch('/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      username: document.getElementById('username').value,
      password: document.getElementById('pw').value
    })
  });
  if (r.ok) { window.location.href='/'; }
  else {
    try {
      const d = await r.json();
      document.getElementById('err').textContent = d.error || 'Login failed';
    } catch(e) {
      document.getElementById('err').textContent='Login failed';
    }
  }
  return false;
}
</script>
</body></html>"""


@app.route("/login", methods=["GET"])
def login_page():
    if db.count_users() == 0:
        return redirect("/register")
    if session.get("user_id"):
        return redirect("/")
    return LOGIN_HTML


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Missing username or password"}), 400

    u = db.get_user_by_username(username)
    if not u or not check_password_hash(u.get("password_hash") or "", password):
        return jsonify({"error": "Invalid username or password"}), 401

    session["user_id"] = int(u["id"])
    session["username"] = u.get("username") or username
    session["is_admin"] = bool(u.get("is_admin"))
    session.permanent = True
    return jsonify({"ok": True})


REGISTER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Register â€” Delta Trading</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--accent:#1f6feb;--red:#f85149}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
    background:var(--bg);color:var(--text);display:flex;justify-content:center;align-items:center;min-height:100vh}
  .box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:40px;width:360px}
  .box h1{font-size:22px;margin-bottom:8px;text-align:center}
  .box p{color:#8b949e;font-size:13px;text-align:center;margin-bottom:24px}
  .box input{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);
    border-radius:6px;color:var(--text);font-size:14px;margin-bottom:16px}
  .box button{width:100%;padding:10px;background:var(--accent);color:#fff;border:none;
    border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
  .box button:hover{opacity:0.9}
  .error{color:var(--red);font-size:13px;text-align:center;margin-bottom:12px}
  .links{margin-top:14px;text-align:center;font-size:13px}
  .links a{color:var(--accent);text-decoration:none}
</style>
</head>
<body>
<div class="box">
  <h1>Create Account</h1>
  <p>Register a new dashboard user</p>
  <div class="error" id="err"></div>
  <form onsubmit="return doRegister(event)">
    <input type="text" id="username" placeholder="Username" autocomplete="username" autofocus>
    <input type="password" id="pw" placeholder="Password" autocomplete="new-password">
    <input type="password" id="pw2" placeholder="Confirm password" autocomplete="new-password">
    <button type="submit">Register</button>
  </form>
  <div class="links"><a href="/login">Back to login</a></div>
</div>
<script>
async function doRegister(e) {
  e.preventDefault();
  const pw = document.getElementById('pw').value;
  const pw2 = document.getElementById('pw2').value;
  if (pw !== pw2) { document.getElementById('err').textContent='Passwords do not match'; return false; }
  const r = await fetch('/register', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      username: document.getElementById('username').value,
      password: pw
    })
  });
  if (r.ok) { window.location.href='/'; }
  else {
    try { const d = await r.json(); document.getElementById('err').textContent = d.error || 'Register failed'; }
    catch(e) { document.getElementById('err').textContent = 'Register failed'; }
  }
  return false;
}
</script>
</body></html>"""


@app.route("/register", methods=["GET"])
def register_page():
    if session.get("user_id"):
        return redirect("/")
    return REGISTER_HTML


@app.route("/register", methods=["POST"])
def register_submit():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Missing username or password"}), 400
    if len(username) < 3 or len(username) > 50:
        return jsonify({"error": "Username must be 3-50 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": "Username already exists"}), 400

    is_admin = db.count_users() == 0
    uid = db.create_user(username, generate_password_hash(password), is_admin=is_admin)
    if is_admin:
        try:
            db.seed_user_settings_from_global(uid)
        except Exception:
            pass
    else:
        # Minimal safe defaults for new users (no secret auto-fill)
        try:
            db.set_user_setting(uid, "testnet", db.get_setting("testnet", "1") or "1")
            db.set_user_setting(uid, "ai_provider", "claude")
            db.set_user_setting(uid, "ai_enabled", "0")
            # Create empty placeholders so bot processes never fall back to admin/global secrets
            for k in ("api_key", "api_secret", "telegram_token", "telegram_chat_id",
                      "anthropic_api_key", "gemini_api_key"):
                db.set_user_setting(uid, k, "")
        except Exception:
            pass

    session["user_id"] = int(uid)
    session["username"] = username
    session["is_admin"] = bool(is_admin)
    session.permanent = True
    return jsonify({"ok": True, "user_id": uid})


@app.route("/logout", methods=["GET"])
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
  .alert {
    background: rgba(210, 153, 34, 0.12);
    border: 1px solid rgba(210, 153, 34, 0.5);
    color: var(--text);
    border-radius: 10px;
    padding: 12px 14px;
    margin: 0 0 18px 0;
    font-size: 13px;
  }
  .alert a { color: var(--blue); font-weight: 700; text-decoration: none; }
  .alert .muted { color: var(--muted); font-weight: 500; }

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

  /* Responsive helpers */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }
  .table-wrap table { min-width: 860px; }

  @media (max-width: 1024px) {
    body { padding: 14px; }
    .header { gap: 10px; }
    .stat-card { padding: 14px; }
    .stat-card .value { font-size: 20px; }
    .stats-grid { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  }

  @media (max-width: 768px) {
    body { padding: 12px; }
    .header { flex-direction: column; align-items: flex-start; }
    .header h1 { font-size: 18px; }
    .header .meta { font-size: 12px; }
    .control-bar { gap: 10px; padding: 10px 12px; }
    .control-bar .sep { display: none; }
    .control-bar label { width: 100%; }
    .control-bar input { width: 92px; }
    .btn { padding: 6px 12px; }
    th { padding: 8px 10px; }
    td { padding: 8px 10px; }
    .table-wrap table { min-width: 760px; }
  }

  @media (max-width: 480px) {
    body { padding: 10px; }
    .stat-card { padding: 12px; }
    .stat-card .value { font-size: 18px; }
    .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    th, td { font-size: 12px; }
    .table-wrap table { min-width: 720px; }
  }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1><span class="live-dot"></span> Delta Trading Dashboard</h1>
    </div>
    <div class="meta">
      <span id="user-badge" style="color:var(--text);font-weight:700"></span> &nbsp;|&nbsp;
      <span id="clock"></span> &nbsp;|&nbsp;
      <span id="testnet-badge"></span> &nbsp;|&nbsp;
      Auto-refresh: 5s &nbsp;|&nbsp;
      <a href="/settings" style="color:var(--blue);text-decoration:none;font-weight:600">⚙ Settings</a> &nbsp;|&nbsp;
      <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600">Logout</a>
    </div>
  </div>

  <div id="global-alert" class="alert" style="display:none"></div>

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

  <!-- Open Orders -->
  <div class="section">
    <div class="section-header">
      <h2>Open Orders</h2>
      <span class="badge blue" id="order-count">0</span>
    </div>
    <div id="orders-table"></div>
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

function escapeHtml(str) {
  return String(str || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

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

  // Current user badge (helps verify user-wise view)
  $('user-badge').textContent = s.username ? `User: ${s.username} (#${s.user_id})` : `User ID: ${s.user_id}`;

  // Global warning (missing API keys, exchange errors, etc.)
  const alertEl = $('global-alert');
  if (s.error) {
    alertEl.style.display = 'block';
    alertEl.innerHTML = `⚠ ${escapeHtml(s.error)} <span class="muted">→</span> <a href="/settings">Add API Key & Secret</a>`;
  } else {
    alertEl.style.display = 'none';
    alertEl.textContent = '';
  }

  const needsSetup = !!s.error;

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
    botBtn.disabled = false;
    $('bot-status').innerHTML = `<span class="dot on"></span> Running (PID ${s.bot_pid}) since ${s.bot_started_at}`;
  } else {
    botBtn.textContent = 'Start Bot';
    botBtn.className = 'btn btn-green btn-sm';
    botBtn.disabled = needsSetup;
    $('bot-status').innerHTML = '<span class="dot off"></span> Stopped';
  }
  const syncBtn = $('sync-btn');
  if (syncBtn) syncBtn.disabled = needsSetup;
  $('mtpd-input').value = s.max_trades_per_day;
  $('today-trades').textContent = `Today: ${s.today_trades} / ${s.max_trades_per_day}`;
}

async function refreshPositions() {
  const pos = await fetchJSON(API + '/api/positions');
  if (!pos) return;

  if (!Array.isArray(pos)) {
    $('pos-count').textContent = '0';
    const msg = pos && pos.error ? pos.error : 'Exchange not configured. Add API Key & Secret in Settings.';
    $('positions-table').innerHTML = `<div class="empty-msg">${escapeHtml(msg)}</div>`;
    return;
  }

  $('pos-count').textContent = pos.length;

  if (pos.length === 0) {
    $('positions-table').innerHTML = '<div class="empty-msg">No open positions</div>';
    return;
  }

  let html = `<div class="table-wrap"><table>
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

  html += '</table></div>';
  $('positions-table').innerHTML = html;
}

async function refreshOrders() {
  const orders = await fetchJSON(API + '/api/orders');
  if (!orders) return;

  $('order-count').textContent = orders.length;

  if (orders.length === 0) {
    $('orders-table').innerHTML = '<div class="empty-msg">No open orders</div>';
    return;
  }

  let html = `<div class="table-wrap"><table>
    <tr><th>Time</th><th>Order ID</th><th>Symbol</th><th>Side</th><th>Type</th><th>Size</th><th>Price</th><th>Status</th><th>Action</th></tr>`;

  for (const o of orders) {
    const ts = o.created_at ? new Date(o.created_at).toLocaleTimeString() : '-';
    const px = (o.price == null) ? '-' : Number(o.price).toFixed(4);
    html += `<tr>
      <td style="font-size:12px;color:var(--muted)">${ts}</td>
      <td><strong>${o.order_id}</strong></td>
      <td><strong>${o.symbol || '-'}</strong></td>
      <td class="${sideClass(o.side)}">${o.side || '-'}</td>
      <td style="font-size:12px;color:var(--muted)">${(o.order_type || '').replace('_',' ')}</td>
      <td>${o.size || 0}</td>
      <td>${px}</td>
      <td><span class="badge yellow" style="font-size:11px">${o.status || '-'}</span></td>
      <td><button class="btn btn-red btn-sm" onclick="cancelOrder('${o.order_id}', ${o.product_id})">Cancel</button></td>
    </tr>`;
  }
  html += '</table></div>';
  $('orders-table').innerHTML = html;
}

async function refreshTrades() {
  const trades = await fetchJSON(API + '/api/trades');
  if (!trades) return;

  $('trade-count').textContent = trades.length;

  if (trades.length === 0) {
    $('trades-table').innerHTML = '<div class="empty-msg">No trades yet</div>';
    return;
  }

  let html = `<div class="table-wrap"><table>
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

  html += '</table></div>';
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

async function cancelOrder(orderId, productId) {
  if (!confirm(`Cancel order ${orderId}?`)) return;
  const r = await fetch(API + '/api/orders/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order_id: orderId, product_id: productId})
  });
  const data = await r.json();
  if (!data.ok) alert('Error: ' + (data.error || 'Cancel failed'));
  else refreshOrders();
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
  let html = `<div class="table-wrap"><table><tr><th>Strategy</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>PnL</th><th>Enabled</th><th>Action</th></tr>`;
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
  html += '</table></div>';
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
  let html = `<div class="table-wrap"><table><tr><th>Time</th><th>Symbol</th><th>Filter</th><th>Detail</th></tr>`;
  for (const r of rows) {
    const ts = r.created_at ? new Date(r.created_at).toLocaleTimeString() : '-';
    html += `<tr>
      <td style="font-size:12px;color:var(--muted)">${ts}</td>
      <td><strong>${r.symbol}</strong></td>
      <td><span class="badge red" style="font-size:11px">${r.filter_name}</span></td>
      <td style="font-size:12px">${r.detail || '-'}</td>
    </tr>`;
  }
  html += '</table></div>';
  $('filter-table').innerHTML = html;
}

async function refresh() {
  await Promise.all([refreshStatus(), refreshPositions(), refreshOrders(), refreshTrades(),
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


@app.route("/", methods=["GET"])
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
  .ai-provider-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    margin-top: 8px;
  }
  .ai-provider-card {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 14px 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    cursor: pointer;
    min-height: 78px;
    background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
    transition: border-color .15s ease, background .15s ease, transform .05s ease;
  }
  .ai-provider-card:hover {
    border-color: #44515f;
    background: rgba(88,166,255,0.06);
  }
  .ai-provider-card:active { transform: translateY(1px); }
  .ai-provider-card.selected {
    border-color: var(--accent);
    background: rgba(31,111,235,0.10);
    box-shadow: 0 0 0 1px rgba(31,111,235,0.25) inset;
  }
  .ai-provider-card input[type="radio"] {
    position: absolute;
    opacity: 0;
    pointer-events: none;
    width: auto;
    max-width: none;
    padding: 0;
  }
  .ai-provider-dot {
    width: 14px;
    height: 14px;
    border-radius: 999px;
    border: 2px solid var(--border);
    margin-top: 2px;
    flex: 0 0 auto;
  }
  .ai-provider-card.selected .ai-provider-dot {
    border-color: var(--accent);
    background: var(--accent);
    box-shadow: 0 0 0 3px rgba(31,111,235,0.15);
  }
  .ai-provider-info { min-width: 0; flex: 1; }
  .ai-provider-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .ai-provider-title { font-size: 13px; font-weight: 700; }
  .ai-provider-sub { color: var(--muted); font-size: 12px; margin-top: 4px; line-height: 1.35; }
  .ai-provider-badge {
    font-size: 11px;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 2px 8px;
    border-radius: 999px;
    white-space: nowrap;
  }
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

  @media (max-width: 1024px) {
    body { padding: 14px; }
    .section { padding: 18px; }
  }

  @media (max-width: 768px) {
    body { padding: 12px; }
    .header { flex-direction: column; align-items: flex-start; gap: 10px; }
    .header h1 { font-size: 18px; }
    .section { padding: 16px; }
    .form-row { gap: 12px; }
    .form-row .form-group { min-width: 100%; }
    .ai-provider-grid { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
  }

  @media (max-width: 480px) {
    body { padding: 10px; }
    .section { padding: 14px; }
    .btn { width: 100%; }
    .test-result { display: block; margin-left: 0; margin-top: 10px; }
  }
</style>
</head>
<body>
  <div class="header">
    <div><h1>⚙ Settings</h1></div>
    <div class="meta">
      <span id="user-badge" style="color:var(--text);font-weight:700"></span> &nbsp;|&nbsp;
      <a href="/" style="color:var(--blue);text-decoration:none;font-weight:600">← Back to Dashboard</a>
      &nbsp;|&nbsp;
      <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600">Logout</a>
    </div>
  </div>

  <div id="msg" class="msg"></div>

  <!-- Delta Exchange API -->
  <div class="section">
    <h2>Delta Exchange API</h2>
    <div class="hint" style="margin-top:6px">Tip: delete a key and click Save to remove it for this user.</div>
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
    <div class="form-row">
      <div class="form-group">
        <label>Server Public IP (for Delta IP whitelist)</label>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;max-width:500px">
          <input type="text" id="public_ip" placeholder="Loading..." readonly style="flex:1;min-width:220px">
          <button class="btn btn-blue" type="button" onclick="refreshServerIp()">Refresh</button>
        </div>
        <div class="hint">If you enabled IP whitelist in Delta, add this IP. On Railway/Cloud hosting, the IP can change.</div>
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

  <!-- Claude AI Brain -->
  <div class="section">
    <h2>🤖 AI Brain</h2>

    <!-- Master toggle -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding:14px 16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;">
      <div>
        <div style="font-size:14px;font-weight:600;">AI-Powered Trade Decisions</div>
        <div class="hint" style="margin-top:3px;">When ON — AI analyses all signals, regime &amp; history before every trade.</div>
      </div>
      <label style="position:relative;display:inline-block;width:52px;height:28px;flex-shrink:0;margin-left:16px;">
        <input type="checkbox" id="ai_enabled" onchange="onAiToggle(this.checked)" style="opacity:0;width:0;height:0;position:absolute;">
        <span id="ai-toggle-track" style="position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#30363d;border-radius:28px;transition:.3s;">
          <span id="ai-toggle-thumb" style="position:absolute;height:20px;width:20px;left:4px;bottom:4px;background:#fff;border-radius:50%;transition:.3s;"></span>
        </span>
      </label>
    </div>

    <!-- Provider selector -->
    <div class="form-group" style="margin-bottom:20px;">
      <label>Active AI Provider</label>
      <div class="ai-provider-grid">
        <label id="prov-claude" class="ai-provider-card" onclick="selectProvider('claude')">
          <input type="radio" name="ai_provider" value="claude" id="radio_claude">
          <span class="ai-provider-dot" aria-hidden="true"></span>
          <div class="ai-provider-info">
            <div class="ai-provider-top">
              <div class="ai-provider-title">🤖 Claude (Anthropic)</div>
              <span class="ai-provider-badge">Paid</span>
            </div>
            <div class="ai-provider-sub">Haiku 3.5 — fast &amp; analytical</div>
          </div>
        </label>

        <label id="prov-gemini" class="ai-provider-card" onclick="selectProvider('gemini')">
          <input type="radio" name="ai_provider" value="gemini" id="radio_gemini">
          <span class="ai-provider-dot" aria-hidden="true"></span>
          <div class="ai-provider-info">
            <div class="ai-provider-top">
              <div class="ai-provider-title">✨ Gemini (Google)</div>
              <span class="ai-provider-badge">Free tier</span>
            </div>
            <div class="ai-provider-sub">2.0 Flash — fast &amp; free tier</div>
          </div>
        </label>



        <label id="prov-both" class="ai-provider-card" onclick="selectProvider('both')">
          <input type="radio" name="ai_provider" value="both" id="radio_both">
          <span class="ai-provider-dot" aria-hidden="true"></span>
          <div class="ai-provider-info">
            <div class="ai-provider-top">
              <div class="ai-provider-title">⚡ Both (Consensus)</div>
              <span class="ai-provider-badge">Strict</span>
            </div>
            <div class="ai-provider-sub">Runs Claude + Gemini and requires agreement</div>
          </div>
        </label>
      </div>
    </div>

    <!-- Claude key -->
    <div style="padding:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;margin-bottom:12px;">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px;">🤖 Claude — Anthropic API Key</div>
      <div class="form-row">
        <div class="form-group" style="margin-bottom:8px;">
          <input type="password" id="anthropic_api_key" placeholder="sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx">
          <div class="hint">Get at <a href="https://console.anthropic.com" target="_blank" style="color:var(--blue)">console.anthropic.com</a> — ~$3–5/month</div>
        </div>
      </div>
      <button class="btn btn-blue" onclick="testAI()" style="font-size:13px;padding:6px 14px;">Test Claude Key</button>
      <span id="ai-result" class="test-result"></span>
    </div>

    <!-- Gemini key -->
    <div style="padding:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;margin-bottom:16px;">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px;">✨ Gemini — Google AI API Key</div>
      <div class="form-row">
        <div class="form-group" style="margin-bottom:8px;">
          <input type="password" id="gemini_api_key" placeholder="AIzaSy-xxxxxxxxxxxxxxxxxxxxxxxx">
          <div class="hint">Get free at <a href="https://aistudio.google.com/apikey" target="_blank" style="color:var(--blue)">aistudio.google.com/apikey</a> — free tier available</div>
        </div>
      </div>
      <button class="btn btn-blue" onclick="testGemini()" style="font-size:13px;padding:6px 14px;">Test Gemini Key</button>
      <span id="gemini-result" class="test-result"></span>
    </div>



    <div id="ai-status-box" style="display:none;margin-bottom:12px;padding:12px 16px;border-radius:6px;font-size:13px;"></div>

    <!-- Feature list -->
    <div style="padding:12px 16px;background:var(--bg);border-left:3px solid var(--accent);border-radius:0 6px 6px 0;">
      <div style="font-size:12px;color:var(--muted);line-height:1.8;">
        <strong style="color:var(--text);">What AI adds:</strong><br>
        ✦ Reads all 8 indicators + regime + account state together<br>
        ✦ Returns ENTER / SKIP / HOLD with plain-English reasoning<br>
        ✦ Daily review of losing trades with config suggestions<br>
        ✦ Smart Telegram alerts explaining each decision<br>
        ✦ Auto-fallback to rule-based scoring if AI is unreachable
      </div>
    </div>
  </div>

  <!-- Dashboard Settings -->
  <div class="section">
    <h2>Dashboard</h2>
    <div class="form-row">
      <div class="form-group">
        <label>Account Password</label>
        <input type="password" id="dashboard_password" placeholder="New password (leave blank to keep)">
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
    // Show which user is active (prevents confusion about user-wise data)
    try {
      const s = await fetch('/api/status');
      if (s.status === 401) { window.location.href = '/login'; return; }
      const sd = await s.json();
      const badge = document.getElementById('user-badge');
      if (badge) badge.textContent = sd.username ? `User: ${sd.username} (#${sd.user_id})` : `User ID: ${sd.user_id}`;
    } catch(e) {}

    const r = await fetch('/api/settings');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    document.getElementById('api_key').value = d.api_key || '';
    document.getElementById('api_secret').value = d.api_secret || '';
    document.getElementById('testnet').value = d.testnet ? 'true' : 'false';
    document.getElementById('telegram_token').value = d.telegram_token || '';
    document.getElementById('telegram_chat_id').value = d.telegram_chat_id || '';
    document.getElementById('anthropic_api_key').value = d.anthropic_api_key || '';
    document.getElementById('gemini_api_key').value = d.gemini_api_key || '';

    selectProvider(d.ai_provider || 'claude');
    const aiOn = d.ai_enabled === true;
    document.getElementById('ai_enabled').checked = aiOn;
    applyToggleStyle(aiOn);
    refreshServerIp();
  } catch(e) { showMsg('Failed to load settings', true); }
}

async function refreshServerIp() {
  const el = document.getElementById('public_ip');
  if (!el) return;
  el.value = 'Loading...';
  try {
    const r = await fetch('/api/server-ip');
    if (r.status === 401) { window.location.href = '/login'; return; }
    const d = await r.json();
    el.value = d.public_ip || '(unavailable)';
  } catch (e) {
    el.value = '(unavailable)';
  }
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
    anthropic_api_key: document.getElementById('anthropic_api_key').value.trim(),
    gemini_api_key: document.getElementById('gemini_api_key').value.trim(),
    ai_provider: document.querySelector('input[name="ai_provider"]:checked')?.value || 'claude',
    ai_enabled: document.getElementById('ai_enabled').checked,
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

function selectProvider(val) {
  ['claude','gemini','both'].forEach(p => {
    const el = document.getElementById('radio_' + p);
    const lbl = document.getElementById('prov-' + p);
    if (el) el.checked = (p === val);
    if (lbl) lbl.classList.toggle('selected', p === val);
  });
}

function applyToggleStyle(on) {
  const track = document.getElementById('ai-toggle-track');
  const thumb = document.getElementById('ai-toggle-thumb');
  track.style.background = on ? '#1f6feb' : '#30363d';
  thumb.style.transform = on ? 'translateX(24px)' : 'translateX(0)';
}

function onAiToggle(checked) {
  applyToggleStyle(checked);
  const box = document.getElementById('ai-status-box');
  if (checked) {
    box.style.display = 'block';
    box.style.background = '#0d2818';
    box.style.borderLeft = '3px solid var(--green)';
    box.style.color = 'var(--green)';
    box.innerHTML = '✅ AI Brain is <strong>ON</strong> — the selected provider will evaluate every trade signal before entry. Save settings to persist.';
  } else {
    box.style.display = 'block';
    box.style.background = '#2d1a00';
    box.style.borderLeft = '3px solid #e3b341';
    box.style.color = '#e3b341';
    box.innerHTML = '⏸ AI Brain is <strong>OFF</strong> — Bot will use standard rule-based signal scoring.';
  }
}

async function testAI() {
  const el = document.getElementById('ai-result');
  el.textContent = 'Testing...';
  el.style.color = 'var(--muted)';
  const key = document.getElementById('anthropic_api_key').value.trim();
  try {
    const r = await fetch('/api/settings/test-ai', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ anthropic_api_key: key })
    });
    const d = await r.json();
    if (d.ok) {
      el.textContent = '✓ AI key valid! Claude responded: ' + (d.reply || 'OK');
      el.style.color = 'var(--green)';
    } else {
      el.textContent = '✗ ' + (d.error || 'Test failed');
      el.style.color = 'var(--red)';
    }
  } catch(e) { el.textContent = '✗ Network error'; el.style.color = 'var(--red)'; }
}

async function testGemini() {
  const el = document.getElementById('gemini-result');
  el.textContent = 'Testing...';
  el.style.color = 'var(--muted)';
  const key = document.getElementById('gemini_api_key').value.trim();
  try {
    const r = await fetch('/api/settings/test-gemini', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ gemini_api_key: key })
    });
    const d = await r.json();
    if (d.ok) {
      el.textContent = '✓ Gemini key valid! Response: ' + (d.reply || 'OK');
      el.style.color = 'var(--green)';
    } else {
      el.textContent = '✗ ' + (d.error || 'Test failed');
      el.style.color = 'var(--red)';
    }
  } catch(e) { el.textContent = '✗ Network error'; el.style.color = 'var(--red)'; }
}

loadSettings();
</script>
</body>
</html>"""


@app.route("/settings", methods=["GET"])
@login_required
def settings_page():
    return SETTINGS_HTML


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", str(config.DASHBOARD_PORT)))
    print("=" * 50)
    print("  Delta Trading Dashboard")
    print(f"  URL: http://localhost:{port}")
    print("  Auto-close monitor: OFF (enable from dashboard)")
    print("=" * 50)
    app.run(host=config.DASHBOARD_HOST, port=port, debug=False)
