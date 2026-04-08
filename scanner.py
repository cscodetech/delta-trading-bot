"""
scanner.py — Scans all Delta Exchange perpetuals and scores them.
Picks the best crypto to trade based on volume, volatility, and signal strength.
"""

import logging
import pandas as pd
import numpy as np
import config
from exchange import DeltaClient
from strategies import collect_signals, aggregate_signals

log = logging.getLogger("delta_bot")

# Read settings from config
TOP_N_BY_VOLUME = getattr(config, "TOP_N_BY_VOLUME", 15)
MIN_VOLUME_USD  = getattr(config, "MIN_VOLUME_USD", 500_000)


def get_tradable_symbols(client: DeltaClient) -> list[dict]:
    """Fetch all active USDT perpetual contracts from Delta Exchange."""
    data = client._get("/v2/products")
    products = data.get("result", [])
    tradable = []
    for p in products:
        symbol = p.get("symbol", "")
        state  = p.get("state", "")
        ptype  = p.get("contract_type", "")
        # Only active perpetual contracts
        if (state == "live" and
                "perpetual" in ptype.lower()):
            tradable.append({
                "symbol":     symbol,
                "product_id": p["id"],
                "name":       p.get("description", symbol),
            })
    log.info(f"Found {len(tradable)} active USDT perpetual contracts.")
    return tradable


def score_symbol(client: DeltaClient, symbol: str, timeframe: str) -> dict | None:
    """
    Score a single symbol. Returns a dict with score details, or None on error.

    Scoring (100 pts total):
      - Volume rank     : 30 pts  (higher 24h volume = better liquidity)
      - Volatility      : 30 pts  (moderate volatility = more opportunity)
      - Signal strength : 40 pts  (how many strategies agree on a direction)
    """
    try:
        df = client.get_candles(symbol, timeframe, limit=100)
        if df.empty or len(df) < 15:
            log.debug(f"  Skipping {symbol}: only {len(df)} candles (need 15)")
            return None

        close = df["close"]

        # ── Volume score (use last candle's volume as proxy) ──────────────────
        avg_volume = df["volume"].astype(float).mean()

        # ── Volatility score (ATR-based as % of price) ────────────────────────
        high = df["high"].astype(float)
        low  = df["low"].astype(float)
        atr  = (high - low).rolling(14).mean().iloc[-1]
        price = float(close.iloc[-1])
        volatility_pct = (atr / price) * 100  # e.g. 1.5 means 1.5% average range

        # ── Signal strength (how many strategies agree) ───────────────────────
        signals = collect_signals(df)
        active_signals = [s for s in signals if s.direction is not None]
        buys = sum(1 for s in active_signals if s.direction == "BUY")
        sells = sum(1 for s in active_signals if s.direction == "SELL")
        total_strategies = len(signals) or 1
        agreement = max(buys, sells)
        direction = "BUY" if buys >= sells else "SELL"

        signal_score = (agreement / total_strategies) * 40  # max 40 pts

        # Volatility score: sweet spot is 0.5%–3%, penalise extremes
        if 0.5 <= volatility_pct <= 3.0:
            vol_score = 30
        elif volatility_pct < 0.5:
            vol_score = volatility_pct / 0.5 * 15   # too quiet
        else:
            vol_score = max(0, 30 - (volatility_pct - 3.0) * 5)  # too wild

        return {
            "symbol":          symbol,
            "price":           price,
            "avg_volume":      avg_volume,
            "volatility_pct":  round(volatility_pct, 3),
            "signal_agreement": agreement,
            "direction":       direction,
            "signal_score":    round(signal_score, 1),
            "vol_score":       round(vol_score, 1),
            "signals":         signals,
        }

    except Exception as e:
        log.warning(f"  Skipping {symbol}: {e}")
        return None


def find_best_symbol(client: DeltaClient, timeframe: str) -> dict | None:
    """
    Full scan pipeline:
    1. Get all tradable symbols
    2. Filter by minimum volume
    3. Score remaining symbols
    4. Return the highest-scoring one
    """
    log.info("🔍 Auto-scanner starting — finding best crypto to trade...")

    symbols = get_tradable_symbols(client)
    if not symbols:
        log.error("No tradable symbols found!")
        return None

    # Quick volume filter using tickers
    log.info(f"  Checking 24h volumes...")
    volume_data = []
    for s in symbols:
        try:
            ticker = client.get_ticker(s["symbol"])
            result = ticker.get("result", {})
            vol    = float(result.get("volume", 0) or 0)
            volume_data.append({**s, "volume_24h": vol})
        except Exception:
            pass

    # Sort by volume, take top N
    volume_data.sort(key=lambda x: x["volume_24h"], reverse=True)
    top_symbols = [
        s for s in volume_data[:TOP_N_BY_VOLUME]
        if s["volume_24h"] >= MIN_VOLUME_USD
    ]

    # Fallback: if nothing passed the volume filter, use all available sorted by volume
    if not top_symbols and volume_data:
        log.warning(f"  No symbols above MIN_VOLUME_USD={MIN_VOLUME_USD:,}. "
                    f"Falling back to top {min(TOP_N_BY_VOLUME, len(volume_data))} by volume.")
        top_symbols = volume_data[:TOP_N_BY_VOLUME]

    log.info(f"  Scoring top {len(top_symbols)} symbols by volume...")

    # Assign volume rank score (30 pts for #1, scaled down)
    scored = []
    for rank, s in enumerate(top_symbols):
        result = score_symbol(client, s["symbol"], timeframe)
        if result is None:
            continue
        volume_rank_score = 30 * (1 - rank / len(top_symbols))
        result["volume_rank_score"] = round(volume_rank_score, 1)
        result["total_score"] = round(
            result["signal_score"] + result["vol_score"] + volume_rank_score, 1
        )
        scored.append(result)
        log.info(
            f"    {result['symbol']:15s} | "
            f"score={result['total_score']:5.1f} | "
            f"signal={result['direction']} ({result['signal_agreement']}/4) | "
            f"volatility={result['volatility_pct']}%"
        )

    if not scored:
        log.warning("No symbols could be scored via strategies.")
        # Hard fallback: pick the highest-volume tradable symbol
        if top_symbols:
            fallback = top_symbols[0]
            log.info(f"  Fallback: using highest-volume symbol {fallback['symbol']}")
            return {
                "symbol":           fallback["symbol"],
                "price":            0,
                "avg_volume":       fallback.get("volume_24h", 0),
                "volatility_pct":   0,
                "signal_agreement": 0,
                "direction":        "BUY",
                "signal_score":     0,
                "vol_score":        0,
                "total_score":      0,
            }
        return None

    # Pick the winner
    best = max(scored, key=lambda x: x["total_score"])
    log.info(f"  Best symbol: {best['symbol']} "
             f"(score={best['total_score']}, direction={best['direction']})")
    return best


def find_all_tradeable(client: DeltaClient, timeframe: str,
                       min_score: float = 30.0) -> list[dict]:
    """
    Like find_best_symbol, but returns ALL symbols with a score above min_score,
    sorted best-first. Each must have at least 1 signal agreement.
    """
    log.info("🔍 Multi-symbol scan — checking all cryptos for signals...")

    symbols = get_tradable_symbols(client)
    if not symbols:
        return []

    log.info("  Checking 24h volumes...")
    volume_data = []
    for s in symbols:
        try:
            ticker = client.get_ticker(s["symbol"])
            result = ticker.get("result", {})
            vol = float(result.get("volume", 0) or 0)
            volume_data.append({**s, "volume_24h": vol})
        except Exception:
            pass

    volume_data.sort(key=lambda x: x["volume_24h"], reverse=True)
    top_symbols = [
        s for s in volume_data[:TOP_N_BY_VOLUME]
        if s["volume_24h"] >= MIN_VOLUME_USD
    ]
    if not top_symbols and volume_data:
        top_symbols = volume_data[:TOP_N_BY_VOLUME]

    log.info(f"  Scoring {len(top_symbols)} symbols...")

    results = []
    for rank, s in enumerate(top_symbols):
        result = score_symbol(client, s["symbol"], timeframe)
        if result is None:
            continue
        volume_rank_score = 30 * (1 - rank / len(top_symbols))
        result["volume_rank_score"] = round(volume_rank_score, 1)
        result["total_score"] = round(
            result["signal_score"] + result["vol_score"] + volume_rank_score, 1
        )
        result["product_id"] = s["product_id"]

        log.info(
            f"    {result['symbol']:15s} | "
            f"score={result['total_score']:5.1f} | "
            f"signal={result['direction']} ({result['signal_agreement']}/4) | "
            f"volatility={result['volatility_pct']}%"
        )

        # Only include if score is decent AND has at least 1 signal agreement
        if result["total_score"] >= min_score and result["signal_agreement"] >= 1:
            results.append(result)

    results.sort(key=lambda x: x["total_score"], reverse=True)
    log.info(f"  Tradeable symbols: {[r['symbol'] for r in results]}")
    return results
