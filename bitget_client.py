from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Dict, List, Optional

import requests

BITGET_BASE_URL = os.getenv("BITGET_BASE_URL", "https://api.bitget.com")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE", "").strip()
BITGET_TIMEOUT = float(os.getenv("BITGET_TIMEOUT", "12"))
BITGET_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
BITGET_MARGIN_COIN = os.getenv("BITGET_MARGIN_COIN", "USDT")
# Bitget rejects the Classic Account "mix" endpoints with error 40085 for
# accounts running in Unified Trading Account (UTA) mode - the account type
# changes which URL segment ("mix" vs "uta") the same operations live under.
# This is a best-effort guess at the UTA path segment since it can't be
# verified against live docs from here; if it's wrong, Bitget will return a
# different, equally specific error (404, or another {code, msg} pair) that
# tells us how to correct it - one env var change away, no redeploy of code.
BITGET_ACCOUNT_MODULE = os.getenv("BITGET_ACCOUNT_MODULE", "uta").strip().strip("/")

# Same field names the uploaded dashboard used to spot PnL/side columns
# across whatever shape Bitget's response happens to have.
PNL_KEYS = ("unrealizedPL", "unrealizedPl", "pnl", "profit", "achievedProfits")


def bitget_configured() -> bool:
    return bool(BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE)


def _build_query(params: Dict[str, Any]) -> str:
    items = [(k, v) for k, v in params.items() if v not in (None, "")]
    if not items:
        return ""
    return "?" + "&".join(f"{k}={v}" for k, v in items)


def _sign(secret: str, message: str) -> str:
    """Bitget v2 signing: base64(HMAC-SHA256(secret, timestamp+method+path+query+body))."""
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    if not bitget_configured():
        raise RuntimeError("BITGET_API_KEY / BITGET_API_SECRET / BITGET_API_PASSPHRASE not set")
    params = params or {}
    qs = _build_query(params)
    timestamp = str(int(time.time() * 1000))
    prehash = timestamp + "GET" + path + qs
    signature = _sign(BITGET_API_SECRET, prehash)
    resp = requests.get(
        BITGET_BASE_URL + path + qs,
        timeout=BITGET_TIMEOUT,
        headers={
            "ACCESS-KEY": BITGET_API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
            "Content-Type": "application/json",
            "locale": "ko-KR",
        },
    )
    # Bitget returns a JSON body with its own {code, msg} even on 4xx/5xx HTTP
    # statuses - that body is the actual reason (bad signature, IP not
    # whitelisted, wrong passphrase, invalid param, etc.). Parse it BEFORE
    # raising on the HTTP status, otherwise raise_for_status() would throw
    # a generic "400 Bad Request" and hide the real cause.
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Bitget returned a non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")
    code = data.get("code")
    if code and code != "00000":
        raise RuntimeError(f"[{code}] {data.get('msg') or 'Bitget API error'} (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} with no Bitget error code: {data}")
    return data.get("data")


def fetch_positions() -> List[Dict[str, Any]]:
    data = _get(
        f"/api/v2/{BITGET_ACCOUNT_MODULE}/position/all-position",
        {"productType": BITGET_PRODUCT_TYPE, "marginCoin": BITGET_MARGIN_COIN},
    )
    return data or []


def fetch_pending_orders() -> List[Dict[str, Any]]:
    data = _get(f"/api/v2/{BITGET_ACCOUNT_MODULE}/order/orders-pending", {"productType": BITGET_PRODUCT_TYPE})
    if isinstance(data, dict):
        return data.get("entrustedList") or []
    return data or []


def fetch_fills(limit: int = 50) -> List[Dict[str, Any]]:
    data = _get(f"/api/v2/{BITGET_ACCOUNT_MODULE}/order/fills", {"productType": BITGET_PRODUCT_TYPE, "limit": limit})
    if isinstance(data, dict):
        return data.get("fillList") or []
    return data or []


def fetch_account_list() -> List[Dict[str, Any]]:
    """Futures account balance/equity per margin coin."""
    data = _get(f"/api/v2/{BITGET_ACCOUNT_MODULE}/account/accounts", {"productType": BITGET_PRODUCT_TYPE})
    return data or []


def fetch_position_history(limit: int = 100) -> List[Dict[str, Any]]:
    """Closed positions (each one a completed trade with realized PnL) -
    used to compute win rate and realized PnL, since open positions alone
    can't tell you whether past trades were winners or losers."""
    data = _get(
        f"/api/v2/{BITGET_ACCOUNT_MODULE}/position/history-position",
        {"productType": BITGET_PRODUCT_TYPE, "pageSize": limit},
    )
    if isinstance(data, dict):
        return data.get("list") or []
    return data or []


def _pnl_of(position: Dict[str, Any]) -> float:
    for k in PNL_KEYS:
        if k in position:
            try:
                return float(position[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


# Field names vary across Bitget account/position-history response shapes;
# try each in order rather than assuming one exact name.
REALIZED_PNL_KEYS = ("netProfit", "pnl", "totalPnl", "realizedPL", "realisedPnl")
EQUITY_KEYS = ("usdtEquity", "accountEquity", "equity", "usdtBalance")


def _realized_pnl_of(closed_position: Dict[str, Any]) -> float:
    for k in REALIZED_PNL_KEYS:
        if k in closed_position:
            try:
                return float(closed_position[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _equity_of(account: Dict[str, Any]) -> float:
    for k in EQUITY_KEYS:
        if k in account:
            try:
                return float(account[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def fetch_summary() -> Dict[str, Any]:
    """One combined snapshot for the dashboard's 'my Bitget position' card:
    open positions, pending orders, recent fills, account balance, and a
    win-rate/PnL readout built from closed-position history. Each piece is
    fetched independently so one failing call (e.g. a transient rate limit,
    or an endpoint whose exact response shape differs from what's assumed
    here) doesn't blank out the others - it just reports its own error."""
    errors: Dict[str, str] = {}
    positions: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    fills: List[Dict[str, Any]] = []
    accounts: List[Dict[str, Any]] = []
    closed_positions: List[Dict[str, Any]] = []
    try:
        positions = fetch_positions()
    except Exception as exc:
        errors["positions"] = f"{type(exc).__name__}: {exc}"
    try:
        orders = fetch_pending_orders()
    except Exception as exc:
        errors["orders"] = f"{type(exc).__name__}: {exc}"
    try:
        fills = fetch_fills()
    except Exception as exc:
        errors["fills"] = f"{type(exc).__name__}: {exc}"
    try:
        accounts = fetch_account_list()
    except Exception as exc:
        errors["accounts"] = f"{type(exc).__name__}: {exc}"
    try:
        closed_positions = fetch_position_history()
    except Exception as exc:
        errors["position_history"] = f"{type(exc).__name__}: {exc}"

    unrealized_pnl = sum(_pnl_of(p) for p in positions)
    total_equity = sum(_equity_of(a) for a in accounts)
    realized_pnls = [_realized_pnl_of(p) for p in closed_positions]
    wins = sum(1 for v in realized_pnls if v > 0)
    losses = sum(1 for v in realized_pnls if v < 0)
    decided = wins + losses  # trades that closed exactly break-even don't count either way
    win_rate_pct = round(wins / decided * 100, 1) if decided else None
    realized_pnl_total = sum(realized_pnls)

    return {
        "positions": positions,
        "orders": orders,
        "fills": fills,
        "total_unrealized_pnl": unrealized_pnl,
        "total_equity": total_equity if accounts else None,
        "win_rate_pct": win_rate_pct,
        "win_count": wins,
        "loss_count": losses,
        "trade_count": len(closed_positions),
        "realized_pnl_total": realized_pnl_total,
        "combined_pnl": realized_pnl_total + unrealized_pnl,
        "errors": errors or None,
    }
