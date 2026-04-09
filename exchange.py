"""
exchange.py — Delta Exchange REST API client.
Docs: https://docs.delta.exchange
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import requests
import pandas as pd

import config
import database as db

log = logging.getLogger("delta_bot")

LIVE_URL = "https://api.delta.exchange"
TEST_URL = "https://cdn-ind.testnet.deltaex.org"

TIMEFRAME_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
    "1d": "1d", "1w": "1w",
}

_RES_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
    "1d": 1440, "1w": 10080,
}


class DeltaClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = TEST_URL if testnet else LIVE_URL
        self.session    = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "python-rest-client",
        })
        # Cache for product lookups
        self._products_cache: list[dict] | None = None
        self._products_cache_ts: float = 0

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str,
              query_string: str = "", payload: str = "") -> dict:
        timestamp = str(int(time.time()))
        message   = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "api-key":   self.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    # ── HTTP helpers with retry ──────────────────────────────────────────────

    def _get(self, path: str, params: dict = None, auth: bool = False) -> dict:
        def _snippet(r: requests.Response) -> str:
            try:
                t = (r.text or "").strip().replace("\n", " ")
                return t[:500]
            except Exception:
                return ""

        url = self.base_url + path
        last_err: Exception | None = None

        for attempt in range(config.API_RETRY_COUNT):
            try:
                if auth:
                    qs = "" if not params else "?" + urlencode(params)
                    headers = self._sign("GET", path, query_string=qs)
                else:
                    headers = {}

                resp = self.session.get(url, params=params, headers=headers, timeout=10)
                if resp.status_code >= 400:
                    log.warning(f"  API GET {path} response [{resp.status_code}]: {_snippet(resp)}")
                    # Don't retry auth failures (usually invalid keys, wrong net, or IP whitelist)
                    if resp.status_code in (401, 403):
                        resp.raise_for_status()
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (401, 403):
                    raise
            except requests.exceptions.RequestException as e:
                last_err = e

            if attempt < config.API_RETRY_COUNT - 1:
                log.warning(f"  API GET {path} failed (attempt {attempt+1}): {last_err}")
                time.sleep(config.API_RETRY_DELAY)

        if last_err:
            raise last_err
        raise RuntimeError(f"API GET {path} failed")

    def _post(self, path: str, body: dict) -> dict:
        payload = json.dumps(body)
        for attempt in range(config.API_RETRY_COUNT):
            try:
                headers = self._sign("POST", path, payload=payload)
                resp = self.session.post(
                    self.base_url + path, data=payload,
                    headers=headers, timeout=10
                )
                if resp.status_code >= 400:
                    log.warning(f"  API {path} response [{resp.status_code}]: {resp.text[:300]}")
                    if resp.status_code in (401, 403):
                        resp.raise_for_status()
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt < config.API_RETRY_COUNT - 1:
                    log.warning(f"  API POST {path} failed (attempt {attempt+1}): {e}")
                    time.sleep(config.API_RETRY_DELAY)
                else:
                    raise

    def _delete(self, path: str, body: dict = None) -> dict:
        payload = json.dumps(body or {})
        headers = self._sign("DELETE", path, payload=payload)
        resp = self.session.delete(
            self.base_url + path, data=payload,
            headers=headers, timeout=10
        )
        if resp.status_code >= 400:
            try:
                log.warning(f"  API DELETE {path} response [{resp.status_code}]: {(resp.text or '')[:300]}")
            except Exception:
                pass
        resp.raise_for_status()
        return resp.json()

    # ── Market Data ──────────────────────────────────────────────────────────

    def get_products(self) -> list[dict]:
        """Fetch products with 60-second cache."""
        if self._products_cache and (time.time() - self._products_cache_ts < 60):
            return self._products_cache
        data = self._get("/v2/products")
        self._products_cache = data.get("result", [])
        self._products_cache_ts = time.time()
        return self._products_cache

    def get_product_id(self, symbol: str) -> int:
        """Resolve symbol string → product_id integer."""
        for p in self.get_products():
            if p.get("symbol") == symbol:
                return p["id"]
        raise ValueError(f"Symbol '{symbol}' not found on Delta Exchange.")

    def get_contract_values(self) -> dict[str, float]:
        """Return {symbol: contract_value} for all products."""
        return {
            p["symbol"]: float(p.get("contract_value", 1))
            for p in self.get_products()
            if p.get("symbol")
        }

    def get_candles(self, symbol: str, timeframe: str,
                    limit: int = 200) -> pd.DataFrame:
        """Fetch OHLCV candles as a DataFrame."""
        resolution = TIMEFRAME_MAP.get(timeframe, "1h")
        minutes = _RES_MINUTES.get(resolution, 60)
        end   = int(time.time())
        start = end - minutes * 60 * limit

        params = {
            "symbol":     symbol,
            "resolution": resolution,
            "start":      start,
            "end":        end,
        }
        data = self._get("/v2/history/candles", params=params)
        candles = data.get("result", [])
        if not candles:
            raise RuntimeError(f"No candle data for {symbol}/{timeframe}.")

        df = pd.DataFrame(candles)
        df.rename(columns={"o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume", "t": "time"},
                  inplace=True)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df.sort_values("time", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def get_ticker(self, symbol: str) -> dict:
        return self._get(f"/v2/tickers/{symbol}")

    def get_orderbook(self, product_id: int) -> dict:
        """Fetch L2 orderbook snapshot."""
        data = self._get("/v2/l2orderbook", params={"product_id": product_id})
        return data.get("result", {})

    # ── Account & Wallet ─────────────────────────────────────────────────────

    def get_wallet_balance(self) -> float:
        """Return total USDT balance."""
        try:
            data = self._get("/v2/wallet/balances", auth=True)
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                raise
            log.warning(f"  Failed to fetch wallet balance: {e}")
            return 0.0
        except Exception as e:
            log.warning(f"  Failed to fetch wallet balance: {e}")
            return 0.0

        balances = data.get("result", [])
        for b in balances:
            asset = b.get("asset_symbol", "").upper()
            if asset in ("USDT", "USD"):
                return float(b.get("balance", 0))
        if balances:
            return float(balances[0].get("balance", 0))
        return 0.0

    def get_available_balance(self) -> float:
        """Return available (free) USDT balance for new positions."""
        try:
            data = self._get("/v2/wallet/balances", auth=True)
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                raise
            log.warning(f"  Failed to fetch available balance: {e}")
            return 0.0
        except Exception as e:
            log.warning(f"  Failed to fetch available balance: {e}")
            return 0.0

        balances = data.get("result", [])
        for b in balances:
            asset = b.get("asset_symbol", "").upper()
            if asset in ("USDT", "USD"):
                return float(b.get("available_balance", 0))
        return 0.0

    def get_positions(self) -> list:
        data = self._get("/v2/positions/margined", auth=True)
        return data.get("result", [])

    def get_open_orders(self, product_id: int) -> list:
        data = self._get("/v2/orders",
                         params={"product_id": product_id}, auth=True)
        return data.get("result", [])

    # ── Order Management ─────────────────────────────────────────────────────

    def place_market_order(self, product_id: int, side: str, qty: int,
                           symbol: str = "") -> dict:
        body = {
            "product_id": product_id,
            "side":       side.lower(),
            "order_type": "market_order",
            "size":       qty,
        }
        resp = self._post("/v2/orders", body)

        # Track order in database
        order_id = 0
        try:
            result = resp.get("result", {})
            order_id = int(result.get("id", 0))
            if order_id:
                db.insert_order({
                    "order_id": order_id,
                    "product_id": product_id,
                    "symbol": symbol,
                    "side": side.upper(),
                    "order_type": "market_order",
                    "size": qty,
                    "status": result.get("state", "pending"),
                    "fill_price": float(result.get("average_fill_price", 0) or 0),
                    "response_json": json.dumps(result)[:500],
                })
        except Exception as e:
            log.warning(f"  Order tracking failed: {e}")

        return resp

    def place_limit_order(self, product_id: int, side: str,
                          qty: int, price: float, symbol: str = "") -> dict:
        body = {
            "product_id":  product_id,
            "side":        side.lower(),
            "order_type":  "limit_order",
            "size":        qty,
            "limit_price": str(price),
        }
        resp = self._post("/v2/orders", body)

        # Track order in database (so Dashboard can show open orders)
        order_id = 0
        try:
            result = resp.get("result", {})
            order_id = int(result.get("id", 0))
            if order_id:
                db.insert_order({
                    "order_id": order_id,
                    "product_id": product_id,
                    "symbol": symbol or "",
                    "side": side.upper(),
                    "order_type": "limit_order",
                    "size": qty,
                    "price": float(price) if price is not None else None,
                    "status": result.get("state", "open"),
                    "fill_price": float(result.get("average_fill_price", 0) or 0),
                    "response_json": json.dumps(result)[:500],
                })
        except Exception as e:
            log.warning(f"  Order tracking failed: {e}")

        return resp

    def cancel_order(self, order_id: int, product_id: int) -> dict:
        body = {"id": order_id, "product_id": product_id}
        return self._delete("/v2/orders", body)

    def close_position(self, product_id: int, size: int, side: str,
                        symbol: str = "") -> dict:
        """Close an open position by placing an opposite market order."""
        close_side = "sell" if side.lower() == "buy" else "buy"
        return self.place_market_order(product_id, close_side, size, symbol)

    def poll_order_status(self, order_id: int, product_id: int) -> dict:
        """Check current status of an order and update the database."""
        try:
            def _filled_size(payload: dict) -> int:
                for k in ("filled_size", "filled_qty", "filled_quantity", "filled"):
                    if k in payload and payload.get(k) is not None:
                        try:
                            return int(float(payload.get(k) or 0))
                        except Exception:
                            pass
                return 0

            # 1) Open orders endpoint
            data = self._get("/v2/orders", params={"product_id": product_id}, auth=True)
            orders = data.get("result", []) or []
            for o in orders:
                if int(o.get("id", 0) or 0) == int(order_id):
                    status = str(o.get("state", "unknown") or "unknown").lower()
                    fill_price = float(o.get("average_fill_price", 0) or 0)
                    filled_sz = _filled_size(o)
                    db.update_order_status(order_id, status, fill_price if fill_price else None)
                    return {"status": status, "fill_price": fill_price, "filled_size": filled_sz}

            # 2) Order history endpoint (cancelled/filled/rejected/etc)
            try:
                history = self.get_order_history(page_size=50) or []
                for o in history:
                    if int(o.get("id", 0) or 0) == int(order_id):
                        status = str(o.get("state", "unknown") or "unknown").lower()
                        fill_price = float(o.get("average_fill_price", 0) or 0)
                        filled_sz = _filled_size(o)
                        if filled_sz == 0 and status in ("filled", "closed"):
                            try:
                                filled_sz = int(float(o.get("size", 0) or 0))
                            except Exception:
                                filled_sz = 0
                        db.update_order_status(order_id, status, fill_price if fill_price else None)
                        return {"status": status, "fill_price": fill_price, "filled_size": filled_sz}
            except Exception:
                pass

            # 3) Fills endpoint (best-effort fallback)
            try:
                fills = self.get_fills(page_size=100) or []
                rel = []
                for f in fills:
                    oid = f.get("order_id")
                    if oid is None and isinstance(f.get("order"), dict):
                        oid = f["order"].get("id")
                    try:
                        if int(oid or 0) == int(order_id):
                            rel.append(f)
                    except Exception:
                        continue

                if rel:
                    qty = 0.0
                    notional = 0.0
                    for f in rel:
                        p = f.get("price") or f.get("fill_price") or f.get("average_fill_price")
                        s = f.get("size") or f.get("qty") or f.get("filled_size")
                        try:
                            fp = float(p or 0)
                            fs = float(s or 0)
                        except Exception:
                            continue
                        if fp <= 0 or fs <= 0:
                            continue
                        qty += fs
                        notional += fp * fs
                    avg = (notional / qty) if qty > 0 else 0.0
                    if qty > 0:
                        db.update_order_status(order_id, "filled", avg if avg else None)
                        return {"status": "filled", "fill_price": float(avg), "filled_size": int(qty)}
            except Exception:
                pass

            # Unknown (do not assume filled)
            return {"status": "unknown", "fill_price": 0, "filled_size": 0}
        except Exception as e:
            log.warning(f"  Order poll failed for {order_id}: {e}")
            return {"status": "unknown", "fill_price": 0, "filled_size": 0}

    # ── Trade History ────────────────────────────────────────────────────────

    def get_fills(self, page_size: int = 50) -> list[dict]:
        """Fetch all fills with pagination. Returns list sorted oldest-first."""
        all_fills = []
        after_cursor = None
        for _ in range(20):  # Max 20 pages = 1000 fills
            params = {"page_size": page_size}
            if after_cursor:
                params["after"] = after_cursor
            data = self._get("/v2/fills", params=params, auth=True)
            fills = data.get("result", [])
            if not fills:
                break
            all_fills.extend(fills)
            meta = data.get("meta", {})
            after_cursor = meta.get("after")
            if not after_cursor:
                break
        all_fills.reverse()  # oldest first
        return all_fills

    def get_order_history(self, page_size: int = 50) -> list[dict]:
        """Fetch closed/cancelled order history with pagination."""
        all_orders = []
        after_cursor = None
        for _ in range(20):
            params = {"page_size": page_size}
            if after_cursor:
                params["after"] = after_cursor
            data = self._get("/v2/orders/history", params=params, auth=True)
            orders = data.get("result", [])
            if not orders:
                break
            all_orders.extend(orders)
            meta = data.get("meta", {})
            after_cursor = meta.get("after")
            if not after_cursor:
                break
        return all_orders
