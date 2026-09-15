"""
Binance Futures liquidation stream (!forceOrder@arr).

Unlike every other data source in this app, liquidation data has no simple
REST polling endpoint that returns market-wide liquidation volume - Binance
only exposes it via a live WebSocket stream. This module keeps one
persistent WebSocket connection open in a background thread (auto-
reconnecting on drop), and maintains a rolling window of recent liquidation
events for the target symbol so collect_binance_snapshot() can read an
aggregated summary (count/volume, split by long-liquidation vs
short-liquidation, over the last 1/5/15 minutes) without itself needing to
know anything about WebSockets.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

try:
    import websocket  # websocket-client package
except Exception:  # pragma: no cover
    websocket = None

SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
STREAM_URL = os.getenv("BINANCE_LIQUIDATION_WS_URL", "wss://fstream.binance.com/ws/!forceOrder@arr")
# How long to keep events around - only needs to cover the longest lookback
# window callers ask for (15 min), plus a little slack.
RETENTION_SECONDS = int(os.getenv("LIQUIDATION_RETENTION_SECONDS", "1200"))
RECONNECT_DELAY_SECONDS = float(os.getenv("LIQUIDATION_RECONNECT_DELAY", "5"))

_events: Deque[Dict[str, Any]] = deque()
_events_lock = threading.Lock()
_state: Dict[str, Any] = {
    "connected": False,
    "last_message_at": None,
    "last_error": None,
    "reconnect_count": 0,
}
_started = False
_started_lock = threading.Lock()


def _prune_locked(now_ts: float) -> None:
    cutoff = now_ts - RETENTION_SECONDS
    while _events and _events[0]["ts"] < cutoff:
        _events.popleft()


def _on_message(ws: Any, message: str) -> None:
    try:
        data = json.loads(message)
        order = data.get("o") or {}
        if order.get("s") != SYMBOL:
            return
        qty = float(order.get("q") or 0)
        price = float(order.get("ap") or order.get("p") or 0)
        side = order.get("S")  # "SELL" = a LONG position got force-liquidated; "BUY" = a SHORT got liquidated
        now_ts = time.time()
        with _events_lock:
            _events.append({
                "ts": now_ts,
                "side": side,
                "qty": qty,
                "notional": qty * price,
            })
            _prune_locked(now_ts)
        _state["last_message_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        _state["last_error"] = f"on_message: {type(exc).__name__}: {exc}"


def _on_error(ws: Any, error: Any) -> None:
    _state["last_error"] = f"{type(error).__name__}: {error}"


def _on_close(ws: Any, close_status_code: Any, close_msg: Any) -> None:
    _state["connected"] = False


def _on_open(ws: Any) -> None:
    _state["connected"] = True
    _state["last_error"] = None


def _run_forever_loop() -> None:
    if websocket is None:
        _state["last_error"] = "websocket-client package not installed"
        return
    while True:
        try:
            app = websocket.WebSocketApp(
                STREAM_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            app.run_forever(ping_interval=180, ping_timeout=10)
        except Exception as exc:
            _state["last_error"] = f"run_forever: {type(exc).__name__}: {exc}"
        _state["connected"] = False
        _state["reconnect_count"] += 1
        time.sleep(RECONNECT_DELAY_SECONDS)


def start_liquidation_stream_once() -> None:
    """Starts the background WebSocket thread exactly once per process."""
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run_forever_loop, name="liquidation-stream", daemon=True).start()


def liquidation_summary(windows_minutes: Optional[List[int]] = None) -> Dict[str, Any]:
    """Aggregated liquidation count/volume/notional over each requested
    window, split by long-liquidations (side=SELL) vs short-liquidations
    (side=BUY). Returns None-filled placeholders (not an error) if the
    stream hasn't connected/received data yet, since that's the normal
    state for the first few seconds after boot."""
    windows = windows_minutes or [1, 5, 15]
    now_ts = time.time()
    with _events_lock:
        _prune_locked(now_ts)
        events = list(_events)

    result: Dict[str, Any] = {
        "connected": _state["connected"],
        "last_message_at": _state["last_message_at"],
        "last_error": _state["last_error"],
    }
    for minutes in windows:
        cutoff = now_ts - minutes * 60
        window_events = [e for e in events if e["ts"] >= cutoff]
        long_liqs = [e for e in window_events if e["side"] == "SELL"]
        short_liqs = [e for e in window_events if e["side"] == "BUY"]
        long_notional = sum(e["notional"] for e in long_liqs)
        short_notional = sum(e["notional"] for e in short_liqs)
        total_notional = long_notional + short_notional
        result[f"m{minutes}"] = {
            "long_liq_count": len(long_liqs),
            "long_liq_qty": sum(e["qty"] for e in long_liqs),
            "long_liq_notional": long_notional,
            "short_liq_count": len(short_liqs),
            "short_liq_qty": sum(e["qty"] for e in short_liqs),
            "short_liq_notional": short_notional,
            # -1 (all short-liquidations) .. +1 (all long-liquidations), same
            # sign convention as order_book imbalance for consistency.
            "liq_imbalance": ((long_notional - short_notional) / total_notional) if total_notional else None,
        }
    return result
