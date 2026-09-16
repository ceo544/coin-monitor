import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional
import requests
from indicators import indicators_from_klines, funding_extra, oi_price_classification

BINANCE_BASE = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
TIMEOUT = float(os.getenv("BINANCE_TIMEOUT", "12"))
_KST = ZoneInfo("Asia/Seoul")


def time_context() -> Dict[str, Any]:
    """시간대/요일 - not fetched from Binance, just derived from the current
    wall-clock time (KST + UTC). Certain hours (e.g. US market open) and
    certain weekdays behave differently, so this is included as a plain
    categorical feature alongside the market data. Session boundaries are
    the commonly-used approximations (UTC): Asia 00:00-09:00, Europe
    07:00-16:00, US 12:00-21:00 - deliberately overlapping, since real
    session activity doesn't snap to a clean boundary and the overlap
    windows (Europe/US especially) are themselves often the most active
    hours."""
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(_KST)
    hour_utc = now_utc.hour
    sessions = []
    if 0 <= hour_utc < 9:
        sessions.append("asia")
    if 7 <= hour_utc < 16:
        sessions.append("europe")
    if 12 <= hour_utc < 21:
        sessions.append("us")
    return {
        "hour_kst": now_kst.hour,
        "hour_utc": hour_utc,
        "weekday": now_kst.weekday(),  # 0=Monday .. 6=Sunday
        "weekday_name": now_kst.strftime("%A"),
        "is_weekend": now_kst.weekday() >= 5,
        "sessions": sessions,  # can be multiple at once during overlaps, or empty during the UTC 21:00-24:00 gap
        "session_overlap": len(sessions) > 1,
    }


def _get_json(path: str, params: Dict[str, Any] | None = None) -> Any:
    url = BINANCE_BASE.rstrip("/") + path
    response = requests.get(url, params=params or {}, timeout=TIMEOUT, headers={"User-Agent": "CoinMonitor/2.0"})
    response.raise_for_status()
    return response.json()


def fetch_klines(interval: str = "15m", limit: int = 200) -> list:
    """Raw OHLCV candles for chart display - a thin public wrapper so
    callers (like the /api/chart-klines endpoint) don't need to reach into
    the private _get_json helper directly."""
    return _get_json("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})


TOP_COIN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT"]


def fetch_top_coin_prices(symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Price + 24h change for a curated list of high-volume coins, in ONE
    API call (Binance's ticker/24hr endpoint accepts a JSON-array-string
    `symbols` param covering multiple symbols at once, avoiding N separate
    requests for N coins)."""
    import json as _json
    syms = symbols or TOP_COIN_SYMBOLS
    data = _get_json("/fapi/v1/ticker/24hr", {"symbols": _json.dumps(syms)})
    rows = data if isinstance(data, list) else [data]
    by_symbol = {row.get("symbol"): row for row in rows if isinstance(row, dict)}
    out = []
    for sym in syms:
        row = by_symbol.get(sym)
        if not row:
            continue
        try:
            out.append({
                "symbol": sym,
                "price": float(row.get("lastPrice")),
                "change_pct": float(row.get("priceChangePercent")),
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_last_price() -> Dict[str, Any]:
    """Lightweight, single-field price fetch (no 24h stats, no klines).

    Meant to be polled every few seconds independently of the heavier
    collect_binance_snapshot() call, so the displayed current price can
    stay continuously live instead of only updating on the e-rang.kr
    collection cycle.
    """
    data = _get_json("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    return {"symbol": data.get("symbol", SYMBOL), "price": data.get("price")}


def fetch_order_book_imbalance(depth_limit: int = 20) -> Dict[str, Any]:
    """Sums bid vs ask volume within the top N price levels of the live
    order book, at three depths (5/10/20) computed from a SINGLE API call
    (no extra requests needed - just slicing the same response three
    ways). A positive imbalance means more resting buy orders than sell
    orders sitting near the current price (buy-side pressure), and vice
    versa. This is a point-in-time snapshot (order books move fast), useful
    as one more feature alongside the slower kline-based indicators, not a
    standalone signal."""
    try:
        data = _get_json("/fapi/v1/depth", {"symbol": SYMBOL, "limit": depth_limit})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    bids = data.get("bids") or []
    asks = data.get("asks") or []

    def _imbalance_at(n: int) -> Any:
        try:
            bid_vol = sum(float(b[1]) for b in bids[:n])
            ask_vol = sum(float(a[1]) for a in asks[:n])
        except (TypeError, ValueError, IndexError):
            return None
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total else None

    try:
        bid_volume = sum(float(b[1]) for b in bids)
        ask_volume = sum(float(a[1]) for a in asks)
    except (TypeError, ValueError, IndexError):
        return {"error": "malformed depth response"}
    total = bid_volume + ask_volume
    imbalance = (bid_volume - ask_volume) / total if total else None
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "imbalance": imbalance,  # -1 (all sell pressure) .. +1 (all buy pressure)
        "imbalance_5": _imbalance_at(5),
        "imbalance_10": _imbalance_at(10),
        "imbalance_20": _imbalance_at(20),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": (spread / best_bid * 100) if (spread is not None and best_bid) else None,
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


def fetch_long_short_ratio(period: str = "5m") -> Dict[str, Any]:
    """Aggregated (across all accounts trading this symbol) long vs short
    account ratio from Binance's public futures market-data endpoint - a
    ratio above 1 means more accounts are net long than net short."""
    try:
        data = _get_json("/futures/data/globalLongShortAccountRatio", {"symbol": SYMBOL, "period": period, "limit": 1})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not data:
        return {}
    latest = data[-1]
    try:
        return {
            "long_short_ratio": float(latest.get("longShortRatio")),
            "long_account_pct": float(latest.get("longAccount")),
            "short_account_pct": float(latest.get("shortAccount")),
        }
    except (TypeError, ValueError):
        return {"error": "malformed long/short ratio response"}


def fetch_top_trader_ratios(period: str = "5m") -> Dict[str, Any]:
    """Same idea as fetch_long_short_ratio() but restricted to Binance's
    "top trader" cohort (their largest accounts by position/margin) via two
    separate endpoints - by ACCOUNT count and by POSITION size. These often
    diverge from the global retail-heavy ratio and from each other, which
    is exactly why the spec asks for both rather than just one."""
    out: Dict[str, Any] = {}
    try:
        acct_data = _get_json("/futures/data/topLongShortAccountRatio", {"symbol": SYMBOL, "period": period, "limit": 1})
        if acct_data:
            latest = acct_data[-1]
            out["top_account_long_short_ratio"] = float(latest.get("longShortRatio"))
            out["top_account_long_pct"] = float(latest.get("longAccount"))
            out["top_account_short_pct"] = float(latest.get("shortAccount"))
    except Exception as exc:
        out["top_account_error"] = f"{type(exc).__name__}: {exc}"
    try:
        pos_data = _get_json("/futures/data/topLongShortPositionRatio", {"symbol": SYMBOL, "period": period, "limit": 1})
        if pos_data:
            latest = pos_data[-1]
            out["top_position_long_short_ratio"] = float(latest.get("longShortRatio"))
            out["top_position_long_pct"] = float(latest.get("longAccount"))
            out["top_position_short_pct"] = float(latest.get("shortAccount"))
    except Exception as exc:
        out["top_position_error"] = f"{type(exc).__name__}: {exc}"
    return out


def fetch_oi_change(period: str = "5m", limit: int = 6) -> Dict[str, Any]:
    """Open interest change over the last `limit` snapshots of Binance's
    own OI history endpoint (each `period` apart - default 5m*6=30min of
    history per call), so a percentage change is available immediately
    rather than needing to wait for this app's own observations to
    accumulate."""
    try:
        data = _get_json("/futures/data/openInterestHist", {"symbol": SYMBOL, "period": period, "limit": limit})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not data or len(data) < 2:
        return {"oi_latest": None, "oi_change_pct": None}
    try:
        first = float(data[0].get("sumOpenInterest"))
        last = float(data[-1].get("sumOpenInterest"))
    except (TypeError, ValueError):
        return {"error": "malformed open interest history response"}
    change_pct = ((last - first) / first * 100) if first else None
    return {"oi_latest": last, "oi_change_pct": change_pct}


def collect_binance_snapshot() -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {"symbol": SYMBOL}
    snapshot["time_context"] = time_context()
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
    snapshot["long_short_ratio"] = fetch_long_short_ratio()
    snapshot["top_trader_ratio"] = fetch_top_trader_ratios()
    snapshot["oi_change"] = fetch_oi_change()
    intervals = os.getenv("BINANCE_INTERVALS", "1m,5m,15m,1h,4h").split(",")
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
    # Cross-referencing features that combine two already-fetched pieces of
    # data, computed once here rather than duplicated per-caller.
    try:
        current_funding = float((snapshot.get("premium_index") or {}).get("lastFundingRate"))
    except (TypeError, ValueError):
        current_funding = None
    snapshot["funding_extra"] = funding_extra(current_funding, snapshot.get("funding_rate_history") or [])
    price_change_5m = ((snapshot.get("indicators") or {}).get("5m") or {}).get("price_returns", {}).get("price_change_pct")
    snapshot["oi_price_classification"] = oi_price_classification(price_change_5m, (snapshot.get("oi_change") or {}).get("oi_change_pct"))
    return snapshot
