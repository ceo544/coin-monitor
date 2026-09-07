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
# V3 Unified Trading Account API groups markets by "category" (e.g.
# USDT-FUTURES), not v2's "productType". An optional symbol filter can be
# set to scope fills/orders to one instrument; unset means "all symbols".
BITGET_CATEGORY = os.getenv("BITGET_CATEGORY", "USDT-FUTURES")
BITGET_SYMBOL = os.getenv("BITGET_SYMBOL", "").strip() or None


def bitget_configured() -> bool:
    return bool(BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE)


def _build_query(params: Dict[str, Any]) -> str:
    items = [(k, v) for k, v in params.items() if v not in (None, "")]
    if not items:
        return ""
    return "?" + "&".join(f"{k}={v}" for k, v in items)


def _sign(secret: str, message: str) -> str:
    """Bitget signing: base64(HMAC-SHA256(secret, timestamp+method+path+query+body))."""
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
            "locale": "en-US",
        },
    )
    # Parse the JSON body (which carries Bitget's own {code, msg}) before
    # raising on HTTP status, so a 4xx/5xx shows the real reason instead of
    # a generic "400/404 Client Error".
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


def fetch_account_assets() -> List[Dict[str, Any]]:
    """Overall Unified Trading Account balance/equity snapshot: accountEquity,
    usdtEquity, unrealisedPnl, usdtUnrealisedPnl, effEquity."""
    data = _get("/api/v3/account/assets")
    if isinstance(data, dict):
        return [data]
    return data or []


def fetch_fills(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Executed trades. Each fill has tradeSide ('open'/'close') and, for
    closes, execPnl - the realized PnL for that specific close."""
    params = {"category": BITGET_CATEGORY, "symbol": symbol or BITGET_SYMBOL}
    data = _get("/api/v3/trade/fills", params)
    if isinstance(data, dict):
        return data.get("fillList") or data.get("list") or []
    return data or []


def fetch_current_positions(symbol: Optional[str] = None, pos_side: Optional[str] = None) -> List[Dict[str, Any]]:
    """Live currently-open positions - the real thing (entry price, mark
    price, leverage, margin, liquidation price, unrealized PnL), not a
    derived estimate. posSide can be 'long' or 'short' to filter one side."""
    params = {"category": BITGET_CATEGORY, "symbol": symbol or BITGET_SYMBOL, "posSide": pos_side}
    data = _get("/api/v3/position/current-position", params)
    if isinstance(data, dict):
        return data.get("list") or data.get("positionList") or []
    return data or []


def fetch_history_orders(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Historical futures orders (filled/cancelled/etc.) - order-level detail,
    coarser than fills."""
    params = {"category": BITGET_CATEGORY, "symbol": symbol or BITGET_SYMBOL}
    data = _get("/api/v3/trade/history-orders", params)
    if isinstance(data, dict):
        return data.get("orderList") or data.get("list") or []
    return data or []


def _account_summary(assets: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Pull the five documented fields straight off the first (only)
    account-assets row - these field names are confirmed by Bitget's own
    docs, no guessing/fallback needed here."""
    def f(row: Dict[str, Any], key: str) -> Optional[float]:
        if key not in row:
            return None
        try:
            return float(row[key])
        except (TypeError, ValueError):
            return None

    if not assets:
        return {
            "account_equity": None, "usdt_equity": None,
            "unrealized_pnl": None, "usdt_unrealized_pnl": None,
            "eff_equity": None,
        }
    row = assets[0]
    return {
        "account_equity": f(row, "accountEquity"),
        "usdt_equity": f(row, "usdtEquity"),
        "unrealized_pnl": f(row, "unrealisedPnl"),
        "usdt_unrealized_pnl": f(row, "usdtUnrealisedPnl"),
        "eff_equity": f(row, "effEquity"),
    }


def _realized_pnl_of_fill(fill: Dict[str, Any]) -> Optional[float]:
    """Only closing fills realize PnL - opens by definition haven't closed
    out a position yet, so they don't count as a win or a loss. Bitget's
    real tradeSide values are 'open_long'/'open_short'/'close_long'/
    'close_short' (direction-qualified), not plain 'open'/'close', so this
    checks a prefix rather than exact equality - an exact-match check here
    was the actual bug that made real close_long/close_short fills (and
    their execPnl) silently vanish from the win-rate/realized-PnL totals."""
    trade_side = str(fill.get("tradeSide") or "").lower()
    if not trade_side.startswith("close"):
        return None
    try:
        return float(fill.get("execPnl"))
    except (TypeError, ValueError):
        return None


def _fill_time(fill: Dict[str, Any]) -> int:
    try:
        return int(fill.get("createdTime") or 0)
    except (TypeError, ValueError):
        return 0


def compute_open_positions(fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Estimate currently-open positions by netting 'open' fills against
    'close' fills per symbol, oldest fill first. Bitget's V3 Unified Trading
    Account API has no live "current positions" endpoint, so this is the
    closest available substitute - accurate as long as ALL of a position's
    opening fills are within the fetched fill history. If an older opening
    fill falls outside that window, the estimated size will be too small.
    Deliberately does not fabricate an unrealized-PnL figure here, since
    that would need a live mark price this client doesn't fetch per symbol."""
    ordered = sorted(fills, key=_fill_time)
    book: Dict[str, Dict[str, float]] = {}
    for f in ordered:
        symbol = f.get("symbol")
        if not symbol:
            continue
        side = (f.get("side") or "").lower()
        trade_side = (f.get("tradeSide") or "").lower()
        try:
            qty = float(f.get("execQty") or 0)
            price = float(f.get("execPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        b = book.setdefault(symbol, {"long_qty": 0.0, "long_cost": 0.0, "short_qty": 0.0, "short_cost": 0.0})
        # Real values are direction-qualified ('open_long', 'close_short',
        # etc.) - match by prefix, not exact equality, same fix as above.
        if trade_side.startswith("open"):
            if side == "buy":
                b["long_cost"] += qty * price
                b["long_qty"] += qty
            elif side == "sell":
                b["short_cost"] += qty * price
                b["short_qty"] += qty
        elif trade_side.startswith("close"):
            if side == "sell" and b["long_qty"] > 0:  # closing a long
                reduce = min(qty, b["long_qty"])
                b["long_cost"] *= (b["long_qty"] - reduce) / b["long_qty"]
                b["long_qty"] -= reduce
            elif side == "buy" and b["short_qty"] > 0:  # closing a short
                reduce = min(qty, b["short_qty"])
                b["short_cost"] *= (b["short_qty"] - reduce) / b["short_qty"]
                b["short_qty"] -= reduce

    positions: List[Dict[str, Any]] = []
    for symbol, b in book.items():
        if b["long_qty"] > 1e-9:
            positions.append({
                "symbol": symbol, "side": "long", "qty": round(b["long_qty"], 8),
                "avgEntryPrice": round(b["long_cost"] / b["long_qty"], 6),
            })
        if b["short_qty"] > 1e-9:
            positions.append({
                "symbol": symbol, "side": "short", "qty": round(b["short_qty"], 8),
                "avgEntryPrice": round(b["short_cost"] / b["short_qty"], 6),
            })
    return positions


def _position_unrealized_pnl(position: Dict[str, Any]) -> float:
    try:
        return float(position.get("unrealisedPnl") or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_summary() -> Dict[str, Any]:
    """One combined snapshot for the dashboard's Bitget card: account
    balance/equity, live currently-open positions, recent fills, recent
    order history, and a win-rate/PnL readout built from closing fills'
    execPnl. Each piece is fetched independently so one failing call
    doesn't blank out the others."""
    errors: Dict[str, str] = {}
    assets: List[Dict[str, Any]] = []
    positions: List[Dict[str, Any]] = []
    fills: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    try:
        assets = fetch_account_assets()
    except Exception as exc:
        errors["assets"] = f"{type(exc).__name__}: {exc}"
    try:
        positions = fetch_current_positions()
    except Exception as exc:
        errors["positions"] = f"{type(exc).__name__}: {exc}"
    try:
        fills = fetch_fills()
    except Exception as exc:
        errors["fills"] = f"{type(exc).__name__}: {exc}"
    try:
        orders = fetch_history_orders()
    except Exception as exc:
        errors["history_orders"] = f"{type(exc).__name__}: {exc}"

    acct = _account_summary(assets)
    unrealized_pnl = acct["usdt_unrealized_pnl"] if acct["usdt_unrealized_pnl"] is not None else acct["unrealized_pnl"]
    close_pnls = [v for v in (_realized_pnl_of_fill(f) for f in fills) if v is not None]
    wins = sum(1 for v in close_pnls if v > 0)
    losses = sum(1 for v in close_pnls if v < 0)
    decided = wins + losses  # exact-break-even closes don't count either way
    win_rate_pct = round(wins / decided * 100, 1) if decided else None
    realized_pnl_total = sum(close_pnls)
    # Direct real-time sum of each currently-open position's own unrealisedPnl
    # field (plus AND minus positions all summed together, hence "통합").
    # Deliberately None (not 0) when there are no open positions, so the
    # dashboard can show this metric as empty/blank rather than a stale
    # zero - i.e. it "disappears" the moment every position is closed,
    # exactly mirroring live position state rather than the account's
    # broader (and possibly slightly lagged) aggregate figure.
    live_open_positions_pnl = sum(_position_unrealized_pnl(p) for p in positions) if positions else None

    return {
        "positions": positions,
        "fills": fills,
        "orders": orders,
        "account": acct,
        "total_equity": acct["usdt_equity"] if acct["usdt_equity"] is not None else acct["account_equity"],
        "total_unrealized_pnl": unrealized_pnl,
        "live_open_positions_pnl": live_open_positions_pnl,
        "win_rate_pct": win_rate_pct,
        "win_count": wins,
        "loss_count": losses,
        "trade_count": len(close_pnls),
        "realized_pnl_total": realized_pnl_total,
        "combined_pnl": realized_pnl_total + (unrealized_pnl or 0.0),
        "errors": errors or None,
    }
