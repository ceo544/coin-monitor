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
