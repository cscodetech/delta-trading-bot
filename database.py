"""
database.py — MySQL database layer for the trading bot.

Tables:
  - trades: All closed trade records
  - orders: Order ID tracking with status polling
  - settings: Runtime settings (max_trades_per_day, etc.)

Uses connection pooling via a simple thread-safe wrapper.
"""

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime

import pymysql
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

log = logging.getLogger("delta_bot")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "delta_trading_bot"),
    "charset": "utf8mb4",
    "autocommit": True,
}

_local = threading.local()


def current_user_id() -> int:
    """
    Returns the active bot/user context, if provided.
    Used by bot.py/dashboard BotManager to tag records with a user_id.
    """
    try:
        return int(os.getenv("BOT_USER_ID") or 0)
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
#  CONNECTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _get_connection() -> pymysql.Connection:
    """Get a thread-local database connection (auto-reconnect)."""
    conn = getattr(_local, "conn", None)
    if conn is None or not conn.open:
        try:
            conn = pymysql.connect(**DB_CONFIG)
            _local.conn = conn
        except pymysql.err.OperationalError:
            # Database might not exist yet — create it
            cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
            tmp = pymysql.connect(**cfg)
            with tmp.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            tmp.close()
            conn = pymysql.connect(**DB_CONFIG)
            _local.conn = conn
    else:
        conn.ping(reconnect=True)
    return conn


@contextmanager
def get_cursor():
    """Thread-safe cursor context manager."""
    conn = _get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    try:
        yield cur
    finally:
        cur.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEMA CREATION
# ═══════════════════════════════════════════════════════════════════════════════

def init_db():
    """Create all tables if they don't exist."""
    with get_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                user_id     INT          NOT NULL DEFAULT 0,
                symbol      VARCHAR(30)  NOT NULL,
                side        VARCHAR(4)   NOT NULL,
                entry_price DOUBLE       NOT NULL DEFAULT 0,
                exit_price  DOUBLE       NOT NULL DEFAULT 0,
                pnl_pct     DOUBLE       NOT NULL DEFAULT 0,
                fee_pct     DOUBLE       NOT NULL DEFAULT 0,
                net_pnl_pct DOUBLE       NOT NULL DEFAULT 0,
                size        INT          NOT NULL DEFAULT 0,
                reason      VARCHAR(100) NOT NULL DEFAULT '',
                order_id    BIGINT       DEFAULT NULL,
                close_order_id BIGINT    DEFAULT NULL,
                regime      VARCHAR(30)  DEFAULT '',
                confirmations INT        DEFAULT 0,
                closed_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pnl_usd     DOUBLE       NOT NULL DEFAULT 0,
                net_pnl_usd DOUBLE       NOT NULL DEFAULT 0,
                INDEX idx_closed_at (closed_at),
                INDEX idx_symbol (symbol),
                INDEX idx_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Migration: add pnl_usd columns if missing
        try:
            cur.execute("SELECT pnl_usd FROM trades LIMIT 1")
        except Exception:
            cur.execute("ALTER TABLE trades ADD COLUMN pnl_usd DOUBLE NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE trades ADD COLUMN net_pnl_usd DOUBLE NOT NULL DEFAULT 0")

        # Migration: add user_id if missing
        try:
            cur.execute("SELECT user_id FROM trades LIMIT 1")
        except Exception:
            cur.execute("ALTER TABLE trades ADD COLUMN user_id INT NOT NULL DEFAULT 0")
            try:
                cur.execute("CREATE INDEX idx_user_id ON trades (user_id)")
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                user_id       INT          NOT NULL DEFAULT 0,
                order_id      BIGINT       NOT NULL,
                product_id    INT          NOT NULL,
                symbol        VARCHAR(30)  NOT NULL,
                side          VARCHAR(4)   NOT NULL,
                order_type    VARCHAR(20)  NOT NULL DEFAULT 'market_order',
                size          INT          NOT NULL DEFAULT 0,
                price         DOUBLE       DEFAULT NULL,
                status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
                fill_price    DOUBLE       DEFAULT NULL,
                response_json TEXT         DEFAULT NULL,
                created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_order_id (order_id),
                INDEX idx_status (status),
                INDEX idx_orders_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Migration: add user_id if missing
        try:
            cur.execute("SELECT user_id FROM orders LIMIT 1")
        except Exception:
            cur.execute("ALTER TABLE orders ADD COLUMN user_id INT NOT NULL DEFAULT 0")
            try:
                cur.execute("CREATE INDEX idx_orders_user_id ON orders (user_id)")
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                `key`   VARCHAR(50) PRIMARY KEY,
                `value` VARCHAR(200) NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_stats (
                strategy    VARCHAR(30) PRIMARY KEY,
                trades      INT NOT NULL DEFAULT 0,
                wins        INT NOT NULL DEFAULT 0,
                losses      INT NOT NULL DEFAULT 0,
                total_pnl   DOUBLE NOT NULL DEFAULT 0,
                enabled     TINYINT NOT NULL DEFAULT 1,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Per-user strategy performance (multi-user)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_stats_user (
                user_id     INT NOT NULL,
                strategy    VARCHAR(30) NOT NULL,
                trades      INT NOT NULL DEFAULT 0,
                wins        INT NOT NULL DEFAULT 0,
                losses      INT NOT NULL DEFAULT 0,
                total_pnl   DOUBLE NOT NULL DEFAULT 0,
                enabled     TINYINT NOT NULL DEFAULT 1,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, strategy),
                INDEX idx_strategy_user (user_id),
                INDEX idx_strategy_name (strategy)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS filter_log (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                user_id     INT          NOT NULL DEFAULT 0,
                symbol      VARCHAR(30) NOT NULL,
                filter_name VARCHAR(50) NOT NULL,
                detail      VARCHAR(200) NOT NULL DEFAULT '',
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_created (created_at),
                INDEX idx_filter_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Migration: add user_id if missing
        try:
            cur.execute("SELECT user_id FROM filter_log LIMIT 1")
        except Exception:
            cur.execute("ALTER TABLE filter_log ADD COLUMN user_id INT NOT NULL DEFAULT 0")
            try:
                cur.execute("CREATE INDEX idx_filter_user_id ON filter_log (user_id)")
            except Exception:
                pass

        # Multi-user auth
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                username      VARCHAR(50) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                is_admin      TINYINT NOT NULL DEFAULT 0,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Ensure there is always exactly one "first admin" user (helps migrations from older DBs).
        try:
            cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_admin=1")
            admin_cnt = int(cur.fetchone()["cnt"] or 0)
            if admin_cnt <= 0:
                cur.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1")
                first = cur.fetchone()
                if first:
                    cur.execute("UPDATE users SET is_admin=1 WHERE id=%s", (int(first["id"]),))
        except Exception:
            pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                id         INT AUTO_INCREMENT PRIMARY KEY,
                user_id    INT NOT NULL,
                `key`      VARCHAR(50) NOT NULL,
                `value`    VARCHAR(500) NOT NULL DEFAULT '',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_user_key (user_id, `key`),
                INDEX idx_user_settings_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # One-time migration: copy legacy strategy_stats -> strategy_stats_user (admin only)
        try:
            cur.execute("SELECT COUNT(*) AS cnt FROM strategy_stats_user")
            has_user_stats = int(cur.fetchone()["cnt"] or 0) > 0
            if not has_user_stats:
                cur.execute("SELECT COUNT(*) AS cnt FROM strategy_stats")
                legacy_cnt = int(cur.fetchone()["cnt"] or 0)
                if legacy_cnt > 0:
                    cur.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id ASC LIMIT 1")
                    admin = cur.fetchone()
                    admin_id = int(admin["id"]) if admin else 1
                    cur.execute("""
                        INSERT INTO strategy_stats_user
                            (user_id, strategy, trades, wins, losses, total_pnl, enabled, updated_at)
                        SELECT
                            %s, strategy, trades, wins, losses, total_pnl, enabled, updated_at
                        FROM strategy_stats
                    """, (admin_id,))
        except Exception:
            pass

        # Normalize ai_provider values (ollama removed; allow only claude/gemini/both)
        try:
            cur.execute(
                "UPDATE user_settings SET `value`='claude' "
                "WHERE `key`='ai_provider' AND `value` NOT IN ('claude','gemini','both')"
            )
        except Exception:
            pass
        try:
            cur.execute(
                "UPDATE settings SET `value`='claude' "
                "WHERE `key`='ai_provider' AND `value` NOT IN ('claude','gemini','both')"
            )
        except Exception:
            pass

        # Cleanup: if non-admin users were auto-seeded with admin secrets, remove duplicates.
        try:
            cur.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id ASC LIMIT 1")
            admin = cur.fetchone()
            admin_id = int(admin["id"]) if admin else 0
            if admin_id > 0:
                # Assign legacy user_id=0 records to the admin, so other users never see them.
                try:
                    cur.execute("UPDATE trades SET user_id=%s WHERE user_id=0", (admin_id,))
                except Exception:
                    pass
                try:
                    cur.execute("UPDATE orders SET user_id=%s WHERE user_id=0", (admin_id,))
                except Exception:
                    pass
                try:
                    cur.execute("UPDATE filter_log SET user_id=%s WHERE user_id=0", (admin_id,))
                except Exception:
                    pass

                sensitive = [
                    "api_key", "api_secret",
                    "telegram_token", "telegram_chat_id",
                    "anthropic_api_key", "gemini_api_key",
                ]
                cur.execute(
                    "SELECT `key`, `value` FROM user_settings WHERE user_id=%s AND `key` IN ("
                    + ", ".join(["%s"] * len(sensitive))
                    + ")",
                    tuple([admin_id] + sensitive),
                )
                admin_map = {row["key"]: (row.get("value") or "") for row in cur.fetchall()}
                try:
                    cur.execute(
                        "SELECT `key`, `value` FROM settings WHERE `key` IN ("
                        + ", ".join(["%s"] * len(sensitive))
                        + ")",
                        tuple(sensitive),
                    )
                    global_map = {row["key"]: (row.get("value") or "") for row in cur.fetchall()}
                except Exception:
                    global_map = {}

                for k in sensitive:
                    v = (admin_map.get(k) or "").strip()
                    if not v:
                        v = ""
                    gv = (global_map.get(k) or "").strip()

                    # If non-admin users were seeded with the admin/global secrets, blank them out.
                    # (keep rows so bots never fall back to global settings).
                    if v:
                        cur.execute(
                            "UPDATE user_settings SET `value`='' "
                            "WHERE user_id IN (SELECT id FROM users WHERE is_admin=0) "
                            "AND `key`=%s AND TRIM(`value`)=%s",
                            (k, v),
                        )
                    if gv and gv != v:
                        cur.execute(
                            "UPDATE user_settings SET `value`='' "
                            "WHERE user_id IN (SELECT id FROM users WHERE is_admin=0) "
                            "AND `key`=%s AND TRIM(`value`)=%s",
                            (k, gv),
                        )
        except Exception:
            pass

    log.info("  Database tables initialised")


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def insert_trade(trade: dict) -> int:
    """Insert a closed trade record. Returns the inserted ID."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO trades
                (user_id, symbol, side, entry_price, exit_price, pnl_pct, fee_pct,
                 net_pnl_pct, size, reason, order_id, close_order_id,
                 regime, confirmations, closed_at, pnl_usd, net_pnl_usd)
            VALUES
                (%(user_id)s, %(symbol)s, %(side)s, %(entry_price)s, %(exit_price)s,
                 %(pnl_pct)s, %(fee_pct)s, %(net_pnl_pct)s, %(size)s,
                 %(reason)s, %(order_id)s, %(close_order_id)s,
                 %(regime)s, %(confirmations)s, %(closed_at)s,
                 %(pnl_usd)s, %(net_pnl_usd)s)
        """, {
            "user_id": int(trade.get("user_id", current_user_id()) or 0),
            "symbol": trade.get("symbol", ""),
            "side": trade.get("side", ""),
            "entry_price": trade.get("entry_price", 0),
            "exit_price": trade.get("exit_price", 0),
            "pnl_pct": trade.get("pnl_pct", 0),
            "fee_pct": trade.get("fee_pct", 0),
            "net_pnl_pct": trade.get("net_pnl_pct", 0),
            "size": trade.get("size", 0),
            "reason": trade.get("reason", ""),
            "order_id": trade.get("order_id"),
            "close_order_id": trade.get("close_order_id"),
            "regime": trade.get("regime", ""),
            "confirmations": trade.get("confirmations", 0),
            "closed_at": trade.get("closed_at", datetime.now()),
            "pnl_usd": trade.get("pnl_usd", 0),
            "net_pnl_usd": trade.get("net_pnl_usd", 0),
        })
        return cur.lastrowid


def get_trades(limit: int = 100, user_id: int | None = None) -> list[dict]:
    """Get recent trades, newest first."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM trades WHERE user_id=%s ORDER BY closed_at DESC LIMIT %s",
            (uid, limit),
        )
        rows = cur.fetchall()
        # Convert datetime objects to ISO strings for JSON
        for r in rows:
            for k in ("closed_at", "created_at"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k].isoformat()
        return rows


def get_today_trade_count(user_id: int | None = None) -> int:
    """Count trades closed today."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM trades WHERE user_id=%s AND DATE(closed_at) = CURDATE()",
            (uid,),
        )
        return cur.fetchone()["cnt"]


def get_trade_stats(user_id: int | None = None) -> dict:
    """Aggregate trade statistics."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                          AS total_trades,
                SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                SUM(CASE WHEN net_pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                ROUND(SUM(net_pnl_pct), 3)                        AS total_pnl,
                ROUND(SUM(CASE WHEN net_pnl_usd > 0
                          THEN net_pnl_usd ELSE 0 END), 4)       AS gross_profit,
                ROUND(ABS(SUM(CASE WHEN net_pnl_usd < 0
                          THEN net_pnl_usd ELSE 0 END)), 4)      AS gross_loss,
                ROUND(SUM(fee_pct), 4)                             AS total_fees,
                ROUND(SUM(net_pnl_usd), 4)                         AS total_pnl_usd
            FROM trades
            WHERE user_id=%s
        """, (uid,))
        row = cur.fetchone()
        total = int(row["total_trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        gross_profit = float(row["gross_profit"] or 0)
        gross_loss = float(row["gross_loss"] or 0)
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
            "total_pnl_pct": float(row["total_pnl"] or 0),
            "total_pnl_usd": float(row["total_pnl_usd"] or 0),
            "profit_factor": round(gross_profit / gross_loss, 2)
                             if gross_loss > 0 else 0,
            "total_fees_pct": float(row["total_fees"] or 0),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY PERFORMANCE TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

def update_strategy_stat(strategy: str, won: bool, pnl: float):
    """Increment a strategy's win/loss counter."""
    uid = current_user_id()
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_stats_user (user_id, strategy, trades, wins, losses, total_pnl)
            VALUES (%s, %s, 1, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                trades = trades + 1,
                wins = wins + VALUES(wins),
                losses = losses + VALUES(losses),
                total_pnl = total_pnl + VALUES(total_pnl)
        """, (uid, strategy, 1 if won else 0, 0 if won else 1, pnl))


def get_strategy_stats(user_id: int | None = None) -> list[dict]:
    """Get performance stats for all strategies."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute(
            "SELECT strategy, trades, wins, losses, total_pnl, enabled, updated_at "
            "FROM strategy_stats_user WHERE user_id=%s ORDER BY strategy",
            (uid,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_disabled_strategies(user_id: int | None = None) -> set[str]:
    """Return set of auto-disabled strategy names."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute(
            "SELECT strategy FROM strategy_stats_user WHERE user_id=%s AND enabled = 0",
            (uid,),
        )
        return {row["strategy"] for row in cur.fetchall()}


def set_strategy_enabled(strategy: str, enabled: bool, user_id: int | None = None):
    """Enable or disable a strategy."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_stats_user (user_id, strategy, enabled)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE enabled = VALUES(enabled)
        """, (uid, strategy, 1 if enabled else 0))


# ═══════════════════════════════════════════════════════════════════════════════
#  FILTER LOG — track why trades are blocked
# ═══════════════════════════════════════════════════════════════════════════════

def log_filter_block(symbol: str, filter_name: str, detail: str, user_id: int | None = None):
    """Record a filter block event for dashboard display."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO filter_log (user_id, symbol, filter_name, detail)
            VALUES (%s, %s, %s, %s)
        """, (uid, symbol, filter_name, detail[:200]))


def get_recent_filter_blocks(limit: int = 50, user_id: int | None = None) -> list[dict]:
    """Get recent filter blocks for dashboard."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute("""
            SELECT symbol, filter_name, detail, created_at
            FROM filter_log
            WHERE user_id=%s
            ORDER BY created_at DESC
            LIMIT %s
        """, (uid, limit))
        return [dict(row) for row in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════════════
#  ORDER TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def insert_order(order: dict) -> int:
    """Track a new order placed on the exchange."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO orders
                (user_id, order_id, product_id, symbol, side, order_type,
                 size, price, status, response_json)
            VALUES
                (%(user_id)s, %(order_id)s, %(product_id)s, %(symbol)s, %(side)s,
                 %(order_type)s, %(size)s, %(price)s, %(status)s,
                 %(response_json)s)
        """, {
            "user_id": int(order.get("user_id", current_user_id()) or 0),
            "order_id": order.get("order_id", 0),
            "product_id": order.get("product_id", 0),
            "symbol": order.get("symbol", ""),
            "side": order.get("side", ""),
            "order_type": order.get("order_type", "market_order"),
            "size": order.get("size", 0),
            "price": order.get("price"),
            "status": order.get("status", "pending"),
            "response_json": order.get("response_json", ""),
        })
        return cur.lastrowid


def update_order_status(order_id: int, status: str,
                        fill_price: float = None):
    """Update an order's status after polling."""
    with get_cursor() as cur:
        if fill_price is not None:
            cur.execute(
                "UPDATE orders SET status=%s, fill_price=%s WHERE order_id=%s",
                (status, fill_price, order_id)
            )
        else:
            cur.execute(
                "UPDATE orders SET status=%s WHERE order_id=%s",
                (status, order_id)
            )


def get_pending_orders(user_id: int | None = None) -> list[dict]:
    """Get all orders that haven't been confirmed as filled/cancelled."""
    uid = current_user_id() if user_id is None else int(user_id or 0)
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM orders "
            "WHERE user_id=%s AND status IN ('pending','open','partially_filled','unknown') "
            "ORDER BY created_at DESC",
            (uid,),
        )
        return cur.fetchall()


def get_order_by_id(order_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
        return cur.fetchone()


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "") -> str:
    """
    Read a setting.

    If BOT_USER_ID is set (>0), user_settings takes priority (including empty values).
    For sensitive keys, we never fall back to global settings to avoid leaking admin
    credentials into other users' bot processes.
    """
    uid = current_user_id()
    sensitive = {
        "api_key", "api_secret",
        "telegram_token", "telegram_chat_id",
        "anthropic_api_key", "gemini_api_key",
    }
    with get_cursor() as cur:
        if uid > 0:
            cur.execute(
                "SELECT `value` FROM user_settings WHERE user_id=%s AND `key`=%s",
                (uid, key),
            )
            row = cur.fetchone()
            if row is not None:
                return row.get("value", default)
            if key in sensitive:
                return default

        cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO settings (`key`, `value`) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE `value`=%s
        """, (key, value, value))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  MULTI-USER: USERS + PER-USER SETTINGS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def count_users() -> int:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        return int(cur.fetchone()["cnt"])


def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s)",
            (username, password_hash, 1 if is_admin else 0),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        return cur.fetchone()


def get_user_by_id(user_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (int(user_id),))
        return cur.fetchone()


def update_user_password(user_id: int, password_hash: str):
    with get_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (password_hash, int(user_id)),
        )


def get_user_setting(user_id: int, key: str, default: str = "") -> str:
    with get_cursor() as cur:
        cur.execute(
            "SELECT `value` FROM user_settings WHERE user_id=%s AND `key`=%s",
            (int(user_id), key),
        )
        row = cur.fetchone()
        return row["value"] if row else default


def set_user_setting(user_id: int, key: str, value: str):
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (user_id, `key`, `value`) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE `value`=%s",
            (int(user_id), key, value, value),
        )


def get_user_settings(user_id: int) -> dict[str, str]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT `key`, `value` FROM user_settings WHERE user_id=%s",
            (int(user_id),),
        )
        out: dict[str, str] = {}
        for row in cur.fetchall():
            out[str(row["key"])] = str(row.get("value") or "")
        return out


def seed_user_settings_from_global(user_id: int, keys: list[str] | None = None):
    """
    Copy global settings into user_settings if the user doesn't already have the key.
    Useful when migrating from single-user to multi-user.
    """
    keys = keys or [
        "api_key", "api_secret", "testnet",
        "telegram_token", "telegram_chat_id",
        "anthropic_api_key", "gemini_api_key",
        "ai_provider", "ai_enabled",
    ]
    with get_cursor() as cur:
        cur.execute(
            "SELECT `key`, `value` FROM settings WHERE `key` IN ("
            + ", ".join(["%s"] * len(keys))
            + ")",
            tuple(keys),
        )
        global_map = {row["key"]: row["value"] for row in cur.fetchall()}

    allowed_ai = {"claude", "gemini", "both"}

    for k in keys:
        existing = get_user_setting(int(user_id), k, default="__MISSING__")
        if existing != "__MISSING__":
            continue
        val = str(global_map.get(k) or "")
        if k == "ai_provider":
            v = val.strip().lower()
            val = v if v in allowed_ai else "claude"
        if val != "":
            set_user_setting(int(user_id), k, val)


# ═══════════════════════════════════════════════════════════════════════════════
#  MIGRATION: Import existing trades.json into MySQL
# ═══════════════════════════════════════════════════════════════════════════════

def migrate_json_trades(json_path: str):
    """One-time migration from trades.json to MySQL."""
    import json
    if not os.path.exists(json_path):
        return 0

    with open(json_path, "r") as f:
        trades = json.load(f)

    count = 0
    for t in trades:
        pnl = t.get("pnl_pct", 0)
        fee = round(abs(pnl) * 0.001, 4)  # Estimate 0.1% fee retroactively
        insert_trade({
            "symbol": t.get("symbol", ""),
            "side": t.get("side", ""),
            "entry_price": t.get("entry_price", 0),
            "exit_price": t.get("exit_price", 0),
            "pnl_pct": pnl,
            "fee_pct": fee,
            "net_pnl_pct": round(pnl - fee, 4),
            "size": t.get("size", 0),
            "reason": t.get("reason", ""),
            "closed_at": t.get("closed_at", datetime.now().isoformat()),
        })
        count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
#  SYNC PAST TRADES FROM EXCHANGE FILLS
# ═══════════════════════════════════════════════════════════════════════════════

def sync_past_trades(exchange_client, user_id: int | None = None) -> int:
    """
    Fetch fills from the exchange API, reconstruct round-trip trades,
    and insert any missing ones into the DB.  Deduplicates by close_order_id.
    Returns the number of newly inserted trades.
    """
    fills = exchange_client.get_fills()
    if not fills:
        return 0

    # Contract values for dollar PnL
    cv = exchange_client.get_contract_values()

    # Group fills by symbol (already sorted oldest-first from get_fills)
    by_symbol: dict[str, list[dict]] = {}
    for f in fills:
        sym = f["product_symbol"]
        by_symbol.setdefault(sym, []).append(f)

    uid = current_user_id() if user_id is None else int(user_id or 0)

    # Existing close_order_ids for dedup (scoped per user)
    existing_close_ids: set[int] = set()
    with get_cursor() as cur:
        cur.execute(
            "SELECT close_order_id FROM trades "
            "WHERE close_order_id IS NOT NULL AND user_id=%s",
            (uid,),
        )
        for row in cur.fetchall():
            existing_close_ids.add(int(row["close_order_id"]))

    trades_added = 0

    for sym, sym_fills in by_symbol.items():
        pos = 0            # running position: +long / -short
        entry_fills = []   # fills that built the current position

        for fill in sym_fills:
            side = fill["side"]
            size = int(fill["size"])
            price = float(fill["price"])
            commission = float(fill.get("commission", 0))
            order_id = int(fill.get("order_id", 0))
            ts = fill.get("created_at", "")

            delta = size if side == "buy" else -size
            new_pos = pos + delta

            # ── Position closed or flipped through zero ──────────────
            if pos != 0 and (
                (pos > 0 and new_pos <= 0) or (pos < 0 and new_pos >= 0)
            ):
                close_size = abs(pos)
                trade_side = "buy" if pos > 0 else "sell"

                # Weighted average entry
                if entry_fills:
                    total_val = sum(
                        abs(int(f["size"])) * float(f["price"])
                        for f in entry_fills
                    )
                    total_sz = sum(abs(int(f["size"])) for f in entry_fills)
                    avg_entry = total_val / total_sz
                    total_entry_comm = sum(
                        float(f.get("commission", 0)) for f in entry_fills
                    )
                    entry_oid = int(entry_fills[0].get("order_id", 0))
                else:
                    avg_entry = price
                    total_entry_comm = 0
                    entry_oid = 0

                # PnL
                if trade_side == "buy":
                    pnl_pct = ((price - avg_entry) / avg_entry) * 100
                else:
                    pnl_pct = ((avg_entry - price) / avg_entry) * 100

                # Fees from actual commissions
                close_frac = close_size / size if size > 0 else 1
                exit_comm = commission * close_frac
                total_comm = total_entry_comm + exit_comm
                notional = close_size * avg_entry
                fee_pct = (total_comm / notional * 100) if notional > 0 else 0.16

                # Timestamp
                try:
                    closed_at = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    )
                except Exception:
                    closed_at = datetime.now()

                if order_id and order_id not in existing_close_ids:
                    # Dollar PnL using contract_value
                    cval = cv.get(sym, 1)
                    notional_usd = close_size * cval * avg_entry
                    pnl_usd = round(notional_usd * pnl_pct / 100, 4)
                    net_pnl_usd = round(notional_usd * (pnl_pct - fee_pct) / 100, 4)

                    insert_trade({
                        "user_id": uid,
                        "symbol": sym,
                        "side": trade_side,
                        "entry_price": round(avg_entry, 6),
                        "exit_price": round(price, 6),
                        "pnl_pct": round(pnl_pct, 4),
                        "fee_pct": round(fee_pct, 4),
                        "net_pnl_pct": round(pnl_pct - fee_pct, 4),
                        "size": close_size,
                        "reason": "API Sync",
                        "order_id": entry_oid or None,
                        "close_order_id": order_id,
                        "regime": "",
                        "confirmations": 0,
                        "closed_at": closed_at,
                        "pnl_usd": pnl_usd,
                        "net_pnl_usd": net_pnl_usd,
                    })
                    trades_added += 1
                    existing_close_ids.add(order_id)

                # Position flipped — start new entry with the overflow
                if new_pos != 0:
                    overflow = abs(new_pos)
                    entry_fills = [{
                        "size": str(overflow),
                        "price": fill["price"],
                        "commission": str(commission * (overflow / size)),
                        "order_id": fill.get("order_id", "0"),
                    }]
                else:
                    entry_fills = []

            # ── Opening a new position ───────────────────────────────
            elif pos == 0 and new_pos != 0:
                entry_fills = [fill]

            # ── Adding to existing position ──────────────────────────
            else:
                entry_fills.append(fill)

            pos = new_pos

    log.info(f"  Synced {trades_added} past trades from exchange fills")
    return trades_added


# ═══════════════════════════════════════════════════════════════════════════════
#  INIT ON IMPORT
# ═══════════════════════════════════════════════════════════════════════════════

try:
    init_db()
except Exception as e:
    log.warning(f"Database init deferred: {e}")
