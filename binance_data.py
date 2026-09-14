import os
from typing import Any, Dict
import requests
from indicators import indicators_from_klines

BINANCE_BASE = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
TIMEOUT = float(os.getenv("BINANCE_TIMEOUT", "12"))


def _get_json(path: str, params: Dict[str, Any] | None = None) -> Any:
    url = BINANCE_BASE.rstrip("/") + path
    response = requests.get(url, params=params or {}, timeout=TIMEOUT, headers={"User-Agent": "CoinMonitor/2.0"})
    response.raise_for_status()
    return response.json()


def fetch_last_price() -> Dict[str, Any]:
    """Lightweight, single-field price fetch (no 24h stats, no klines).

    Meant to be polled every few seconds independently of the heavier
    collect_binance_snapshot() call, so the displayed current price can
    stay continuously live instead of only updating on the e-rang.kr
    collection cycle.
    """
    data = _get_json("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    return {"symbol": data.get("symbol", SYMBOL), "price": data.get("price")}


def fetch_order_book_imbalance(depth_limit: int = 50) -> Dict[str, Any]:
    """Sums bid vs ask volume within the top `depth_limit` price levels of
    the live order book - a positive imbalance means more resting buy
    orders than sell orders sitting near the current price (buy-side
    pressure), and vice versa. This is a point-in-time snapshot (order
    books move fast), useful as one more feature alongside the slower
    kline-based indicators, not a standalone signal."""
    try:
        data = _get_json("/fapi/v1/depth", {"symbol": SYMBOL, "limit": depth_limit})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    try:
        bid_volume = sum(float(b[1]) for b in bids)
        ask_volume = sum(float(a[1]) for a in asks)
    except (TypeError, ValueError, IndexError):
        return {"error": "malformed depth response"}
    total = bid_volume + ask_volume
    imbalance = (bid_volume - ask_volume) / total if total else None
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "imbalance": imbalance,  # -1 (all sell pressure) .. +1 (all buy pressure)
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None,
    }


def fetch_funding_rate_history(limit: int = 8) -> list[Dict[str, Any]]:
    """Last `limit` funding rate settlements (funding happens every 8h, so
    8 records = ~2.7 days). Combined with the repeated 30s snapshots this
    app already takes, this gives both the recent trend (from this call)
    and a continuously-growing longer history (from observations
    accumulating over time)."""
    try:
        data = _get_json("/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": limit})
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    return data if isinstance(data, list) else []


def collect_binance_snapshot() -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {"symbol": SYMBOL}
    errors: Dict[str, str] = {}
    calls = {
        "ticker_24h": ("/fapi/v1/ticker/24hr", {"symbol": SYMBOL}),
        "premium_index": ("/fapi/v1/premiumIndex", {"symbol": SYMBOL}),
        "open_interest": ("/fapi/v1/openInterest", {"symbol": SYMBOL}),
    }
    for key, (path, params) in calls.items():
        try:
            snapshot[key] = _get_json(path, params)
        except Exception as exc:
            errors[key] = f"{type(exc).__name__}: {exc}"
    snapshot["order_book"] = fetch_order_book_imbalance()
    snapshot["funding_rate_history"] = fetch_funding_rate_history()
    intervals = os.getenv("BINANCE_INTERVALS", "1m,5m,15m,1h").split(",")
    snapshot["klines"] = {}
    snapshot["indicators"] = {}
    for interval in [x.strip() for x in intervals if x.strip()]:
        try:
            klines = _get_json("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": 220})
            snapshot["klines"][interval] = klines[-5:]  # keep last candles compact; indicators use full response below
            snapshot["indicators"][interval] = indicators_from_klines(klines)
        except Exception as exc:
            errors[f"klines_{interval}"] = f"{type(exc).__name__}: {exc}"
    if errors:
        snapshot["errors"] = errors
    return snapshot
