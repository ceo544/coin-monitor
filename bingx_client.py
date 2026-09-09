from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

BINGX_BASE_URL = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com")
BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "").strip()
BINGX_TIMEOUT = float(os.getenv("BINGX_TIMEOUT", "12"))
BINGX_RECV_WINDOW = os.getenv("BINGX_RECV_WINDOW", "5000")


def bingx_configured() -> bool:
    return bool(BINGX_API_KEY and BINGX_API_SECRET)


def configure(api_key: Optional[str] = None, api_secret: Optional[str] = None) -> None:
    """Updates credentials at runtime (from the Settings page) without a restart."""
    global BINGX_API_KEY, BINGX_API_SECRET
    if api_key is not None:
        BINGX_API_KEY = api_key.strip()
    if api_secret is not None:
        BINGX_API_SECRET = api_secret.strip()


def _sign(params: Dict[str, Any]) -> str:
    """BingX (like Binance-style futures APIs) signs the alphabetically-sorted
    query string with HMAC-SHA256, hex-encoded. NOTE: this is the standard
    convention for this API family, matched against the docs provided, but
    was not verified against a live BingX account by Claude - place a TEST
    ORDER (endpoint [20] in the docs) or a tiny real order first before
    trusting this with real size."""
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    return hmac.new(BINGX_API_SECRET.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Any:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {}
    if signed:
        if not bingx_configured():
            raise RuntimeError("BINGX_API_KEY / BINGX_API_SECRET not set")
        params["timestamp"] = str(int(time.time() * 1000))
        params.setdefault("recvWindow", BINGX_RECV_WINDOW)
        params["signature"] = _sign(params)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    url = BINGX_BASE_URL + path
    if method == "GET":
        resp = requests.get(url, params=params, headers=headers, timeout=BINGX_TIMEOUT)
    elif method == "POST":
        resp = requests.post(url, params=params, headers=headers, timeout=BINGX_TIMEOUT)
    elif method == "DELETE":
        resp = requests.delete(url, params=params, headers=headers, timeout=BINGX_TIMEOUT)
    else:
        raise ValueError(f"unsupported method {method}")
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"BingX returned a non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")
    code = data.get("code")
    if code not in (0, None):
        raise RuntimeError(f"[{code}] {data.get('msg') or 'BingX API error'} (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} with no BingX error code: {data}")
    return data.get("data")


# --- Market data (public, unsigned) -----------------------------------------

def fetch_current_price(symbol: str) -> Optional[float]:
    data = _request("GET", "/openApi/swap/v1/ticker/price", {"symbol": symbol}, signed=False)
    if isinstance(data, list):
        data = data[0] if data else {}
    try:
        return float((data or {}).get("price"))
    except (TypeError, ValueError):
        return None


# --- Account / positions (signed, read-only) ---------------------------------

def fetch_balance() -> List[Dict[str, Any]]:
    data = _request("GET", "/openApi/swap/v3/user/balance")
    if isinstance(data, dict):
        # Some BingX responses wrap the balance list/object under "balance".
        inner = data.get("balance")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return [inner]
        return [data]
    return data or []


def fetch_positions(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol})
    return data or []


def fetch_open_orders(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol})
    if isinstance(data, dict):
        return data.get("orders") or []
    return data or []


def fetch_order_history(symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    data = _request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": symbol, "limit": limit})
    if isinstance(data, dict):
        return data.get("orders") or []
    return data or []


def fetch_fills(symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    data = _request("GET", "/openApi/swap/v2/trade/fillHistory", {"symbol": symbol, "limit": limit})
    if isinstance(data, dict):
        return data.get("fill_orders") or data.get("fills") or []
    return data or []


def fetch_leverage(symbol: str) -> Dict[str, Any]:
    return _request("GET", "/openApi/swap/v2/trade/leverage", {"symbol": symbol}) or {}


def fetch_position_mode() -> Dict[str, Any]:
    """dualSidePosition: true = Hedge Mode (LONG and SHORT can be open at the
    same time, required for the positionSide=LONG/SHORT order style this
    client uses), false = One-Way Mode."""
    return _request("GET", "/openApi/swap/v1/positionSide/dual") or {}


# --- Trading (signed, WRITES REAL ORDERS - handle with care) -----------------

def set_leverage(symbol: str, side: str, leverage: int) -> Dict[str, Any]:
    """side: 'LONG' or 'SHORT' (matches positionSide)."""
    return _request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol, "side": side, "leverage": leverage,
    }) or {}


def set_position_mode(hedge_mode: bool) -> Dict[str, Any]:
    return _request("POST", "/openApi/swap/v1/positionSide/dual", {
        "dualSidePosition": "true" if hedge_mode else "false",
    }) or {}


def _tp_sl_payload(stop_price: float, is_take_profit: bool) -> str:
    """BingX's create-order endpoint takes takeProfit/stopLoss as a JSON
    string blob (not a plain number) per their documented convention for
    this API family. workingType MARK_PRICE avoids a brief wick on the last
    trade price triggering it early."""
    return json.dumps({
        "type": "TAKE_PROFIT_MARKET" if is_take_profit else "STOP_MARKET",
        "stopPrice": stop_price,
        "workingType": "MARK_PRICE",
    })


def create_order(
    symbol: str,
    side: str,  # "BUY" or "SELL"
    position_side: str,  # "LONG" or "SHORT"
    order_type: str,  # "LIMIT" or "MARKET"
    quantity: float,
    price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Places a real order. LIMIT orders require `price`. take_profit_price/
    stop_loss_price, if given, are attached to the same order so the exchange
    manages the exit automatically once the entry fills."""
    params: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": order_type,
        "quantity": quantity,
    }
    if order_type == "LIMIT":
        if price is None:
            raise ValueError("price is required for LIMIT orders")
        params["price"] = price
        params["timeInForce"] = "GTC"
    if take_profit_price is not None:
        params["takeProfit"] = _tp_sl_payload(take_profit_price, is_take_profit=True)
    if stop_loss_price is not None:
        params["stopLoss"] = _tp_sl_payload(stop_loss_price, is_take_profit=False)
    if client_order_id:
        params["clientOrderId"] = client_order_id
    return _request("POST", "/openApi/swap/v2/trade/order", params) or {}


def test_order(**kwargs: Any) -> Dict[str, Any]:
    """Same params as create_order, but validated by BingX WITHOUT actually
    placing it - use this to sanity-check params (min quantity, precision,
    etc.) before ever sending a real order."""
    symbol = kwargs.get("symbol")
    side = kwargs.get("side")
    position_side = kwargs.get("position_side")
    order_type = kwargs.get("order_type", "LIMIT")
    quantity = kwargs.get("quantity")
    price = kwargs.get("price")
    params: Dict[str, Any] = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": order_type, "quantity": quantity,
    }
    if order_type == "LIMIT" and price is not None:
        params["price"] = price
        params["timeInForce"] = "GTC"
    return _request("POST", "/openApi/swap/v2/trade/order/test", params) or {}


def cancel_order(symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> Dict[str, Any]:
    if not order_id and not client_order_id:
        raise ValueError("cancel_order needs order_id or client_order_id")
    return _request("DELETE", "/openApi/swap/v2/trade/order", {
        "symbol": symbol, "orderId": order_id, "clientOrderId": client_order_id,
    }) or {}


def cancel_all_open_orders(symbol: str) -> Dict[str, Any]:
    """Emergency-stop helper: cancels every open (unfilled) order for a
    symbol in one call."""
    orders = fetch_open_orders(symbol)
    results = []
    for o in orders:
        try:
            oid = o.get("orderId")
            results.append(cancel_order(symbol, order_id=oid))
        except Exception as exc:
            results.append({"error": f"{type(exc).__name__}: {exc}", "orderId": o.get("orderId")})
    return {"cancelled": len(results), "results": results}
