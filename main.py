from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import psycopg
import requests
from flask import Flask, Response, jsonify, request
from psycopg.types.json import Jsonb

from parser import parse_page
from signal_analysis import signal_snapshot, summarize_signal_records

try:
    from binance_data import collect_binance_snapshot, fetch_last_price
except Exception:  # pragma: no cover
    collect_binance_snapshot = None
    fetch_last_price = None

TARGET_URL = os.getenv("TARGET_URL", "https://e-rang.kr/api/coin.php")
INTERVAL = max(1, int(os.getenv("INTERVAL_SECONDS", "30")))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
USER_AGENT = os.getenv("USER_AGENT", "CoinMonitor/2.0 (+public-page-observation)")
ENABLE_BINANCE = os.getenv("ENABLE_BINANCE", "true").lower() not in {"0", "false", "no", "off"}
MAX_HTML_BYTES = int(os.getenv("MAX_HTML_BYTES", "2000000"))
# Safety valve for aggressive polling intervals (e.g. INTERVAL_SECONDS=1):
# if the target starts failing repeatedly (likely a rate limit / block),
# back off instead of hammering it forever at the same short interval.
BACKOFF_AFTER_FAILURES = int(os.getenv("BACKOFF_AFTER_FAILURES", "5"))
BACKOFF_SECONDS = float(os.getenv("BACKOFF_SECONDS", "30"))
# Binance's public REST API is far more forgiving than scraping e-rang.kr,
# so ALL Binance-derived data (price, funding, open interest, 1m/5m/15m/1h
# indicators) is refreshed on its own fast, independent loop below - not on
# the 30s e-rang scrape cycle, which only exists to read the Long/Short
# signal that can't be obtained anywhere else.
LIVE_PRICE_INTERVAL = float(os.getenv("LIVE_PRICE_INTERVAL_SECONDS", "2"))
BINANCE_SNAPSHOT_INTERVAL = float(os.getenv("BINANCE_SNAPSHOT_INTERVAL_SECONDS", "3"))
# Telegram alert on Long/Short signal turning ON (edge-triggered: fires once
# when it flips from OFF to ON, not on every 30s poll while it stays ON).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_NOTIFY_OFF = os.getenv("TELEGRAM_NOTIFY_OFF", "true").lower() not in {"0", "false", "no", "off"}
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
KST = ZoneInfo("Asia/Seoul")

app = Flask(__name__)


@app.after_request
def _no_cache_api(resp: Response) -> Response:
    # Ensure the dashboard's polling fetch() calls always get fresh data,
    # never a stale cached response from the browser or an intermediate proxy.
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp
collector_state: Dict[str, Any] = {
    "started": False,
    "booted_at": None,
    "last_started_at": None,
    "last_finished_at": None,
    "next_run_at": None,
    "last_error": None,
    "last_saved_id": None,
    "running": False,
    "consecutive_failures": 0,
}
live_price_state: Dict[str, Any] = {
    "started": False,
    "symbol": None,
    "price": None,
    "updated_at": None,
    "last_error": None,
}
live_binance_state: Dict[str, Any] = {
    "started": False,
    "snapshot": {},
    "updated_at": None,
    "last_error": None,
}
telegram_state: Dict[str, Any] = {
    "enabled": False,  # set once at boot after checking token/chat id
    "last_long_signal": None,   # None = unknown yet (e.g. right after boot)
    "last_short_signal": None,
    "last_sent_at": None,
    "last_error": None,
    "sent_count": 0,
}
_state_lock = threading.Lock()


def log(tag: str, message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] [{tag}] {message}", flush=True)


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is missing. Add Postgres DATABASE_URL in Railway Variables.")
    return url


def get_conn() -> psycopg.Connection:
    return psycopg.connect(database_url(), autocommit=True)


def init_db() -> None:
    log("DB", "initializing schema")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id BIGSERIAL PRIMARY KEY,
                observed_at TIMESTAMPTZ NOT NULL,
                target_url TEXT NOT NULL,
                http_status INTEGER,
                success BOOLEAN NOT NULL DEFAULT FALSE,
                current_price NUMERIC,
                current_price_raw TEXT,
                long_signal BOOLEAN,
                short_signal BOOLEAN,
                long_color TEXT,
                short_color TEXT,
                entry_message BOOLEAN,
                parsed_json JSONB,
                binance_json JSONB,
                raw_text TEXT,
                raw_html TEXT,
                content_sha256 TEXT,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_observed_at ON observations(observed_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_signal ON observations(long_signal, short_signal, observed_at DESC)")
        # Backward-compatible migrations for older zip versions.
        migrations = [
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS target_url TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS current_price_raw TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS binance_json JSONB",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        ]
        for statement in migrations:
            cur.execute(statement)
    log("DB", "schema ready")


def _decimal_from_raw(raw: Optional[str]) -> Optional[Decimal]:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def fetch_target() -> tuple[int, str]:
    log("FETCH", f"GET {TARGET_URL}")
    response = requests.get(
        TARGET_URL,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    log("HTTP", f"{response.status_code} {response.reason}; {len(response.content)} bytes")
    response.raise_for_status()
    html = response.text
    if len(html.encode("utf-8", "replace")) > MAX_HTML_BYTES:
        html = html.encode("utf-8", "replace")[:MAX_HTML_BYTES].decode("utf-8", "replace")
        log("WARN", f"HTML truncated to {MAX_HTML_BYTES} bytes")
    return response.status_code, html


def insert_observation(record: Dict[str, Any]) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO observations (
                observed_at, target_url, http_status, success,
                current_price, current_price_raw,
                long_signal, short_signal, long_color, short_color, entry_message,
                parsed_json, binance_json, raw_text, raw_html, content_sha256, error
            ) VALUES (
                %(observed_at)s, %(target_url)s, %(http_status)s, %(success)s,
                %(current_price)s, %(current_price_raw)s,
                %(long_signal)s, %(short_signal)s, %(long_color)s, %(short_color)s, %(entry_message)s,
                %(parsed_json)s, %(binance_json)s, %(raw_text)s, %(raw_html)s, %(content_sha256)s, %(error)s
            ) RETURNING id
            """,
            {
                **record,
                "parsed_json": Jsonb(record.get("parsed_json") or {}),
                "binance_json": Jsonb(record.get("binance_json") or {}),
            },
        )
        saved_id = cur.fetchone()[0]
    return int(saved_id)


def _row_map_from_parsed(parsed: Dict[str, Any]) -> Dict[str, list]:
    """Python port of the dashboard's client-side rowMap() so Telegram alerts
    can include the same Long/TP/SL/Short entry-price rows shown on screen."""
    out: Dict[str, list] = {}
    for row in parsed.get("table_rows") or []:
        text = (row.get("text") or "").strip()
        nums = row.get("numbers_raw") or []
        if re.match(r"^Long\b", text, re.I):
            out["long"] = nums[:5]
        elif re.match(r"^Short\b", text, re.I):
            out["short"] = nums[:5]
        elif re.match(r"^TP\b", text, re.I):
            if "tpLong" not in out:
                out["tpLong"] = nums[:5]
            else:
                out["tpShort"] = nums[:5]
        elif re.match(r"^SL\b", text, re.I):
            if "slLong" not in out:
                out["slLong"] = nums[:5]
            else:
                out["slShort"] = nums[:5]
    sides = parsed.get("sides") or {}
    out.setdefault("long", (sides.get("long") or {}).get("entry_prices_guess") or [])
    out.setdefault("short", (sides.get("short") or {}).get("entry_prices_guess") or [])
    return out


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            with _state_lock:
                telegram_state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
                telegram_state["last_error"] = None
                telegram_state["sent_count"] += 1
            log("TELEGRAM", "notification sent")
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            with _state_lock:
                telegram_state["last_error"] = err
            log("TELEGRAM_ERROR", err)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        with _state_lock:
            telegram_state["last_error"] = err
        log("TELEGRAM_ERROR", err)


def _fmt_num(value: Any) -> str:
    try:
        return f"{float(str(value).replace(',', '')):,.1f}"
    except (TypeError, ValueError):
        return "-"


def _sig_evidence_text(parsed: Dict[str, Any], side: str) -> str:
    """Python port of the dashboard's sigReason(): the actual E-RANG
    evidence (detected background color + matched CSS rule + label text).
    Escaped for safe inclusion in a Telegram HTML-mode message (raw '<'/'&'
    from CSS selector text like ':not(...)' or '&nbsp;' would otherwise be
    parsed as broken HTML and silently drop or fail the whole message)."""
    sig = ((parsed.get("signals") or {}).get(side)) or {}
    if not sig.get("active"):
        return ""
    color = sig.get("detected_color") or "-"
    parts = [p for p in (sig.get("visual_evidence") or "").split(" | ") if p]
    css_rule = next((p for p in parts if p.startswith("css ")), None)
    text = f"라벨 '{sig['matched_text']}'" if sig.get("matched_text") else ""
    out = f"감지 배경색 {color}"
    if css_rule:
        out += f" · {css_rule}"
    if text:
        out += f" · {text}"
    return html.escape(out)


def _narrative_for(ind: Dict[str, Any]) -> list[str]:
    """Python port of the dashboard's narrativeFor(): a short, honest,
    correlation-only readout of Binance indicators at this moment - not a
    claim about what caused the E-RANG label to change color."""
    if not ind:
        return []
    macd = ind.get("macd") or {}
    boll = ind.get("bollinger20") or {}
    bullets: list[str] = []

    def to_f(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rsi = to_f(ind.get("rsi14"))
    if rsi is not None:
        if rsi <= 30:
            bullets.append(f"RSI {rsi:,.1f} · 과매도권")
        elif rsi >= 70:
            bullets.append(f"RSI {rsi:,.1f} · 과매수권")
        else:
            bullets.append(f"RSI {rsi:,.1f} · 중립 구간")
    hist = to_f(macd.get("histogram"))
    if hist is not None:
        bullets.append(f"MACD 히스토그램 {hist:,.1f} · {'상승 모멘텀' if hist >= 0 else '하락 모멘텀'}")
    close, ema20, ema50 = to_f(ind.get("close")), to_f(ind.get("ema20")), to_f(ind.get("ema50"))
    if close is not None and ema20 is not None:
        bullets.append(f"종가가 EMA20({ema20:,.1f}) {'상회' if close >= ema20 else '하회'}")
    if ema20 is not None and ema50 is not None:
        bullets.append(f"EMA20 {'≥' if ema20 >= ema50 else '미만'} EMA50 · {'단기 상승 배열' if ema20 >= ema50 else '단기 하락 배열'}")
    upper, lower = to_f(boll.get("upper")), to_f(boll.get("lower"))
    if close is not None and upper is not None and lower is not None and (upper - lower) > 0:
        pos = (close - lower) / (upper - lower)
        if pos <= 0.15:
            bullets.append("볼린저밴드 하단 근접 · 되돌림 반등 구간 가능성")
        elif pos >= 0.85:
            bullets.append("볼린저밴드 상단 근접 · 과열·되돌림 하락 구간 가능성")
    return bullets


def _build_signal_message(
    side: str,
    turned_on: bool,
    parsed: Dict[str, Any],
    current_price_raw: Optional[str],
    color: Optional[str],
    binance_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    rows = _row_map_from_parsed(parsed)
    key = "long" if side == "long" else "short"
    tp_key = "tpLong" if side == "long" else "tpShort"
    sl_key = "slLong" if side == "long" else "slShort"
    entries = rows.get(key) or []
    tps = rows.get(tp_key) or []
    sls = rows.get(sl_key) or []
    label_kr = "롱(LONG)" if side == "long" else "숏(SHORT)"
    emoji = "🟦" if side == "long" else "🟥"
    state_kr = "진입 신호 발생" if turned_on else "신호 해제"
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"{emoji} <b>E-RANG {label_kr} {state_kr}</b>",
        f"현재가: {_fmt_num(current_price_raw)}",
    ]
    if entries:
        stage_labels = ["25%", "40%", "60%", "100%", "예비"]
        for idx, val in enumerate(entries[:5]):
            tp = tps[idx] if idx < len(tps) else None
            sl = sls[idx] if idx < len(sls) else None
            stage = stage_labels[idx] if idx < len(stage_labels) else str(idx + 1)
            piece = f"진입{idx+1}({stage}): {_fmt_num(val)}"
            if tp is not None:
                piece += f" · TP {_fmt_num(tp)}"
            if sl is not None:
                piece += f" · SL {_fmt_num(sl)}"
            lines.append(piece)
    # Actual E-RANG evidence (the real cause: label background color/CSS rule).
    evidence = _sig_evidence_text(parsed, side)
    if evidence:
        lines.append("")
        lines.append(f"📌 <b>E-RANG 실제 판정 근거</b>\n{evidence}")
    # Binance indicator context - correlation only, never claimed as cause.
    indicators = (binance_snapshot or {}).get("indicators") or {}
    tf_bullets = []
    for tf in ("15m", "1h"):
        bullets = _narrative_for(indicators.get(tf) or {})
        if bullets:
            safe_bullets = [html.escape(b) for b in bullets]
            tf_bullets.append(f"<b>{tf}</b>\n" + "\n".join(f"· {b}" for b in safe_bullets))
    if tf_bullets:
        lines.append("")
        lines.append("📊 <b>Binance 지표 참고 (참고용, 확정 원인 아님)</b>")
        lines.extend(tf_bullets)
    lines.append(now_kst + " (KST)")
    if DASHBOARD_URL:
        lines.append(f'<a href="{html.escape(DASHBOARD_URL, quote=True)}">대시보드 열기</a>')
    return "\n".join(lines)


def _maybe_notify_telegram(
    parsed: Dict[str, Any],
    long_active: bool,
    short_active: bool,
    current_price_raw: Optional[str],
    long_color: Optional[str],
    short_color: Optional[str],
    binance_snapshot: Optional[Dict[str, Any]] = None,
) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    with _state_lock:
        prev_long = telegram_state["last_long_signal"]
        prev_short = telegram_state["last_short_signal"]
        telegram_state["last_long_signal"] = long_active
        telegram_state["last_short_signal"] = short_active
    # Only fire on a real transition, and never on the very first cycle after
    # boot (prev is None) - otherwise every restart would re-announce
    # whatever state happened to already be true.
    if prev_long is not None and long_active != prev_long:
        if long_active or TELEGRAM_NOTIFY_OFF:
            send_telegram_message(_build_signal_message("long", long_active, parsed, current_price_raw, long_color, binance_snapshot))
    if prev_short is not None and short_active != prev_short:
        if short_active or TELEGRAM_NOTIFY_OFF:
            send_telegram_message(_build_signal_message("short", short_active, parsed, current_price_raw, short_color, binance_snapshot))


def collect_once() -> Dict[str, Any]:
    observed_at = datetime.now(timezone.utc)
    with _state_lock:
        collector_state["running"] = True
        collector_state["last_started_at"] = observed_at.isoformat()
        collector_state["last_error"] = None
    record: Dict[str, Any] = {
        "observed_at": observed_at,
        "target_url": TARGET_URL,
        "http_status": None,
        "success": False,
        "current_price": None,
        "current_price_raw": None,
        "long_signal": None,
        "short_signal": None,
        "long_color": None,
        "short_color": None,
        "entry_message": None,
        "parsed_json": {},
        "binance_json": {},
        "raw_text": None,
        "raw_html": "",
        "content_sha256": None,
        "error": None,
    }
    try:
        status_code, html = fetch_target()
        parsed = parse_page(html)
        current_price_raw = parsed.get("current_price_raw")
        current_price = _decimal_from_raw(current_price_raw)
        signals = parsed.get("signals") or {}
        long_sig = signals.get("long") or {}
        short_sig = signals.get("short") or {}
        log("PARSE", f"BTC={current_price_raw or '-'} text_len={len(parsed.get('page_text') or '')}")
        log(
            "SIGNAL",
            f"LONG={long_sig.get('active')} color={long_sig.get('detected_color')} | "
            f"SHORT={short_sig.get('active')} color={short_sig.get('detected_color')} | entry_message={parsed.get('entry_message')}",
        )
        binance_json: Dict[str, Any] = current_binance_snapshot() if ENABLE_BINANCE else {}
        if ENABLE_BINANCE and not binance_json and collect_binance_snapshot is not None:
            # The independent live-snapshot loop may not have completed its
            # first fetch yet (e.g. right after a fresh deploy/restart).
            # Fall back to a direct synchronous fetch so this observation -
            # and any Telegram alert built from it - still gets indicator data
            # instead of an empty "지표 참고" section.
            try:
                binance_json = collect_binance_snapshot()
            except Exception as exc:
                binance_json = {"errors": {"snapshot": f"{type(exc).__name__}: {exc}"}}
        if ENABLE_BINANCE:
            b_price = (binance_json.get("ticker_24h") or {}).get("lastPrice")
            log("BINANCE", f"using snapshot lastPrice={b_price or '-'} errors={len(binance_json.get('errors') or {})}")
        sha = hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()
        record.update(
            {
                "http_status": status_code,
                "success": True,
                "current_price": current_price,
                "current_price_raw": current_price_raw,
                "long_signal": bool(long_sig.get("active")),
                "short_signal": bool(short_sig.get("active")),
                "long_color": long_sig.get("detected_color"),
                "short_color": short_sig.get("detected_color"),
                "entry_message": bool(parsed.get("entry_message")),
                "parsed_json": parsed,
                "binance_json": binance_json,
                "raw_text": parsed.get("page_text"),
                "raw_html": html,
                "content_sha256": sha,
            }
        )
        try:
            _maybe_notify_telegram(
                parsed,
                bool(long_sig.get("active")),
                bool(short_sig.get("active")),
                current_price_raw,
                long_sig.get("detected_color"),
                short_sig.get("detected_color"),
                binance_json,
            )
        except Exception as exc:
            log("TELEGRAM_ERROR", f"notify failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        log("ERROR", record["error"])
    finally:
        try:
            saved_id = insert_observation(record)
            log("DB", f"saved observation id={saved_id} success={record['success']}")
            record["id"] = saved_id
            with _state_lock:
                collector_state["last_saved_id"] = saved_id
                collector_state["last_error"] = record.get("error")
        except Exception as db_exc:
            log("DB_ERROR", f"{type(db_exc).__name__}: {db_exc}")
            with _state_lock:
                collector_state["last_error"] = f"DB_ERROR {type(db_exc).__name__}: {db_exc}"
        finished = datetime.now(timezone.utc)
        with _state_lock:
            collector_state["running"] = False
            collector_state["last_finished_at"] = finished.isoformat()
            collector_state["next_run_at"] = datetime.fromtimestamp(time.time() + INTERVAL, timezone.utc).isoformat()
    return record


def collector_loop() -> None:
    log("BOOT", f"collector started interval={INTERVAL}s target={TARGET_URL} binance={ENABLE_BINANCE}")
    consecutive_failures = 0
    while True:
        started = time.monotonic()
        record = collect_once()
        elapsed = time.monotonic() - started
        if record.get("success"):
            consecutive_failures = 0
            sleep_for = max(1.0, INTERVAL - elapsed)
        else:
            consecutive_failures += 1
            if consecutive_failures >= BACKOFF_AFTER_FAILURES:
                # The target is very likely rate-limiting or blocking us at
                # this interval. Back off instead of retrying at full speed
                # forever, which would only make being blocked more likely.
                sleep_for = max(BACKOFF_SECONDS, INTERVAL - elapsed)
                log("BACKOFF", f"{consecutive_failures} consecutive failures, backing off to {sleep_for:.1f}s")
            else:
                sleep_for = max(1.0, INTERVAL - elapsed)
        with _state_lock:
            collector_state["consecutive_failures"] = consecutive_failures
            collector_state["next_run_at"] = datetime.fromtimestamp(time.time() + sleep_for, timezone.utc).isoformat()
        log("NEXT", f"sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)


def start_collector_once() -> None:
    with _state_lock:
        if collector_state["started"]:
            return
        collector_state["started"] = True
        collector_state["booted_at"] = datetime.now(timezone.utc).isoformat()
    thread = threading.Thread(target=collector_loop, name="coin-collector", daemon=True)
    thread.start()


def live_price_loop() -> None:
    if fetch_last_price is None:
        log("LIVE_PRICE", "binance_data.fetch_last_price unavailable, live price loop not starting")
        return
    log("LIVE_PRICE_BOOT", f"live price loop started interval={LIVE_PRICE_INTERVAL}s")
    while True:
        started = time.monotonic()
        try:
            data = fetch_last_price()
            with _state_lock:
                live_price_state["symbol"] = data.get("symbol")
                live_price_state["price"] = data.get("price")
                live_price_state["updated_at"] = datetime.now(timezone.utc).isoformat()
                live_price_state["last_error"] = None
        except Exception as exc:
            with _state_lock:
                live_price_state["last_error"] = f"{type(exc).__name__}: {exc}"
            log("LIVE_PRICE_ERROR", live_price_state["last_error"])
        elapsed = time.monotonic() - started
        time.sleep(max(0.5, LIVE_PRICE_INTERVAL - elapsed))


def start_live_price_once() -> None:
    with _state_lock:
        if live_price_state["started"]:
            return
        live_price_state["started"] = True
    thread = threading.Thread(target=live_price_loop, name="binance-live-price", daemon=True)
    thread.start()


def binance_snapshot_loop() -> None:
    if collect_binance_snapshot is None:
        log("BINANCE_LIVE", "collect_binance_snapshot unavailable, snapshot loop not starting")
        return
    log("BINANCE_LIVE_BOOT", f"binance snapshot loop started interval={BINANCE_SNAPSHOT_INTERVAL}s")
    while True:
        started = time.monotonic()
        try:
            snapshot = collect_binance_snapshot()
            with _state_lock:
                live_binance_state["snapshot"] = snapshot
                live_binance_state["updated_at"] = datetime.now(timezone.utc).isoformat()
                live_binance_state["last_error"] = (snapshot.get("errors") or None)
        except Exception as exc:
            with _state_lock:
                live_binance_state["last_error"] = f"{type(exc).__name__}: {exc}"
            log("BINANCE_LIVE_ERROR", live_binance_state["last_error"])
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, BINANCE_SNAPSHOT_INTERVAL - elapsed))


def start_binance_snapshot_once() -> None:
    with _state_lock:
        if live_binance_state["started"]:
            return
        live_binance_state["started"] = True
    thread = threading.Thread(target=binance_snapshot_loop, name="binance-live-snapshot", daemon=True)
    thread.start()


def current_binance_snapshot() -> Dict[str, Any]:
    """Freshest Binance snapshot available (updated every BINANCE_SNAPSHOT_INTERVAL
    seconds, independent of the e-rang scrape cycle). Used both for the live
    dashboard view and to tag each e-rang observation with the indicator
    context at that moment, without making a new Binance call per e-rang row."""
    with _state_lock:
        return dict(live_binance_state.get("snapshot") or {})


def db_summary() -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE success), COUNT(*) FILTER (WHERE NOT success) FROM observations")
            total, ok, fail = cur.fetchone()
            cur.execute(
                """
                SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                       long_signal, short_signal, long_color, short_color, entry_message, error, parsed_json, binance_json
                FROM observations ORDER BY id DESC LIMIT 1
                """
            )
            row = cur.fetchone()
        return {
            "database_ok": True,
            "total": int(total or 0),
            "successful": int(ok or 0),
            "failed": int(fail or 0),
            "latest": row_to_dict(row) if row else None,
        }
    except Exception as exc:
        return {"database_ok": False, "error": f"{type(exc).__name__}: {exc}", "total": 0, "successful": 0, "failed": 0, "latest": None}


def row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        "id": row[0],
        "observed_at": row[1].isoformat() if row[1] else None,
        "http_status": row[2],
        "success": row[3],
        "current_price": str(row[4]) if row[4] is not None else None,
        "current_price_raw": row[5],
        "long_signal": row[6],
        "short_signal": row[7],
        "long_color": row[8],
        "short_color": row[9],
        "entry_message": row[10],
        "error": row[11],
        "parsed": row[12] or {},
        "binance": row[13] or {},
    }


@app.get("/healthz")
def healthz() -> Response:
    summary = db_summary()
    status_code = 200 if summary.get("database_ok") else 503
    return jsonify({"ok": summary.get("database_ok"), "collector": collector_state, "db": summary}), status_code


@app.get("/api/status")
def api_status() -> Response:
    summary = db_summary()
    with _state_lock:
        state = dict(collector_state)
        live_price = dict(live_price_state)
        live_binance = dict(live_binance_state)
        telegram = dict(telegram_state)
    return jsonify(
        {
            "service": "coin-monitor",
            "target_url": TARGET_URL,
            "interval_seconds": INTERVAL,
            "binance_enabled": ENABLE_BINANCE,
            "collector": state,
            "live_price": live_price,
            "live_binance": live_binance,
            "telegram": telegram,
            "db": summary,
        }
    )


@app.get("/api/live-price")
def api_live_price() -> Response:
    with _state_lock:
        return jsonify(dict(live_price_state))


@app.get("/api/binance-live")
def api_binance_live() -> Response:
    with _state_lock:
        return jsonify(dict(live_binance_state))


@app.get("/api/history")
def api_history() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "80"))))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_color, short_color, entry_message, error, parsed_json, binance_json
            FROM observations ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        rows = [row_to_dict(row) for row in cur.fetchall()]
    return jsonify({"items": rows})


@app.get("/api/signal-analysis")
def api_signal_analysis() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "200"))))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_color, short_color, entry_message, error, parsed_json, binance_json
            FROM observations
            WHERE (COALESCE(long_signal, false) OR COALESCE(short_signal, false))
            ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        records = [row_to_dict(row) for row in cur.fetchall()]
    return jsonify({
        "note": "Binance indicators are context captured when E-RANG turned ON; they show correlation, not proven causation.",
        "summary": summarize_signal_records(records),
        "events": [signal_snapshot(r) for r in records],
    })


@app.get("/api/signals")
def api_signals() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "200"))))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_color, short_color, entry_message, error, parsed_json, binance_json
            FROM observations
            WHERE COALESCE(long_signal, false) OR COALESCE(short_signal, false)
            ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        rows = [row_to_dict(row) for row in cur.fetchall()]
    return jsonify({"items": rows})


@app.get("/api/debug-signal")
def api_debug_signal() -> Response:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, observed_at, parsed_json, raw_html FROM observations ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "no observations"}), 404
    parsed = row[2] or {}
    html = row[3] or ""
    low = html.lower()
    snippets = {}
    for side in ("long", "short"):
        pos = low.find(side)
        snippets[side] = html[max(0, pos-500):pos+1200] if pos >= 0 else ""
    return jsonify({
        "ok": True, "id": row[0], "observed_at": row[1].isoformat() if row[1] else None,
        "parser_version": parsed.get("parser_version"),
        "signals": parsed.get("signals") or {},
        "html_snippets": snippets,
    })


@app.post("/api/collect-now")
def api_collect_now() -> Response:
    record = collect_once()
    return jsonify({"ok": record.get("success"), "id": record.get("id"), "error": record.get("error")})


@app.post("/api/telegram-test")
def api_telegram_test() -> Response:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"}), 400
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    summary = db_summary()
    latest = summary.get("latest") or {}
    parsed = latest.get("parsed") or {}
    side = "long" if latest.get("long_signal") else ("short" if latest.get("short_signal") else None)
    if side and parsed.get("signals"):
        binance_snapshot = current_binance_snapshot() or latest.get("binance") or {}
        color = latest.get("long_color") if side == "long" else latest.get("short_color")
        preview = _build_signal_message(side, True, parsed, latest.get("current_price_raw"), color, binance_snapshot)
        msg = f"🧪 <b>[테스트 미리보기 · 실제 알림과 동일한 형식]</b>\n\n{preview}"
    else:
        msg = (
            f"✅ Coin Monitor 테스트 메시지\n{now_kst} (KST)\n텔레그램 알림 설정이 정상 동작합니다.\n"
            "(지금은 LONG/SHORT가 둘 다 OFF라서 근거 포함 미리보기는 다음 ON 시점에 실제로 보내드릴게요.)"
        )
    send_telegram_message(msg)
    with _state_lock:
        err = telegram_state.get("last_error")
    return jsonify({"ok": err is None, "error": err, "previewed_side": side})


@app.get("/export.csv")
def export_csv() -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "id", "observed_at", "http_status", "success", "current_price", "current_price_raw",
        "long_signal", "short_signal", "long_color", "short_color", "entry_message",
        "content_sha256", "error", "parsed_json", "binance_json",
    ])
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_color, short_color, entry_message,
                   content_sha256, error, parsed_json, binance_json
            FROM observations ORDER BY id ASC
            """
        )
        for row in cur:
            writer.writerow([
                row[0], row[1].isoformat() if row[1] else None, row[2], row[3], row[4], row[5],
                row[6], row[7], row[8], row[9], row[10], row[11], row[12],
                json.dumps(row[13] or {}, ensure_ascii=False),
                json.dumps(row[14] or {}, ensure_ascii=False),
            ])
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=coin_observations.csv"},
    )


@app.get("/")
def dashboard() -> str:
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Coin Monitor</title>
<style>
:root{--bg:#07101f;--panel:#0e1b31;--panel2:#101f38;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--red:#ff5364;--green:#35e29a;--yellow:#ffc83d}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1540px;margin:auto;padding:24px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.top h1{margin:0;font-size:30px}.sub,.muted{color:var(--muted)}.sub{margin-top:5px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#152540;color:#fff;padding:11px 15px;border-radius:11px;text-decoration:none;font-weight:800;cursor:pointer}.live{color:var(--green);font-weight:900}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:18px;box-shadow:0 14px 32px #0004;min-width:0}.s2{grid-column:span 2}.s3{grid-column:span 3}.s4{grid-column:span 4}.s6{grid-column:span 6}.s8{grid-column:span 8}.s12{grid-column:span 12}.label{font-size:13px;color:#a9bfdf;font-weight:800}.big{font-size:29px;font-weight:950;margin-top:7px}.hero{display:flex;align-items:center;gap:22px;min-height:110px}.heroSignal{font-size:42px;font-weight:1000}.short{color:var(--red)}.long{color:var(--blue)}.wait{color:var(--yellow)}.ok{color:var(--green)}h2{font-size:18px;margin:0 0 14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{background:#132947;color:#c7dcfa;font-size:12px}.rowlong.on td:first-child{font-weight:950;color:var(--blue)}.rowshort.on td:first-child{background:#ef3340;color:#fff;font-weight:950}.entryrow{display:grid;grid-template-columns:82px repeat(5,1fr);gap:8px;align-items:stretch;margin-bottom:10px}.sideLabel{display:flex;align-items:center;font-size:20px;font-weight:950}.entry{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:10px;min-width:0}.entry b{font-size:12px;color:#9fb8db;display:block}.entry strong{font-size:17px;display:block;margin-top:5px;white-space:nowrap}.dist{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.dist .entry strong{font-size:20px}.tabs{display:flex;gap:7px;margin:12px 0}.tab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer}.tab.active{background:#168cff;color:white}.metricTop{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:12px}.metric b{display:block;color:#9fb8db;font-size:12px}.metric strong{display:block;font-size:19px;margin-top:6px}.indtable{min-width:0}.indtable td:nth-child(2){font-weight:800}.statusUp{color:var(--green)}.statusDown{color:var(--red)}.statusNeutral{color:#dbe7f8}.evidence{line-height:1.7}.evidence strong{font-size:18px}.foot{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:13px;gap:12px}.nowrap{white-space:nowrap}.clickrow{cursor:pointer}.clickrow:hover{background:#132947}.analysisGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.analysisBox{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:14px}.analysisBox h3{margin:0 0 10px;font-size:17px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{background:#102746;border:1px solid var(--line);border-radius:999px;padding:6px 9px;font-size:12px}.detailHead{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.hint{font-size:12px;color:var(--muted)}.reasonCell{text-align:left;white-space:normal;min-width:220px;max-width:320px}.reasonMini{font-size:11px;line-height:1.45;margin-bottom:5px;padding:5px 7px;border-radius:7px;background:#0a172b;border:1px solid var(--line)}.reasonMini:last-child{margin-bottom:0}.reasonMini.long{color:#bcdcff;border-color:#2563eb55}.reasonMini.short{color:#ffd0d6;border-color:#ef334055}.reasonMini b{font-weight:900}.reasonList{margin:9px 0 0;padding-left:18px;font-size:12px;color:#c7dcfa;line-height:1.6}.reasonList li{margin-bottom:3px}.reasonSummary{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:6px}.reasonSummary b{display:block;margin-bottom:6px;font-size:13px;color:#dbe7f8}
@media(max-width:1050px){.s2,.s3,.s4,.s6,.s8{grid-column:span 12}.metricTop{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}.entryrow{grid-template-columns:70px repeat(5,130px);overflow-x:auto}.dist{grid-template-columns:repeat(5,140px);overflow-x:auto}}@media(max-width:600px){.wrap{padding:12px}.top{flex-direction:column}.metricTop{grid-template-columns:1fr 1fr}.heroSignal{font-size:34px}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Coin Monitor</h1><div class="sub">e-rang coin.php 1분 수집 + LONG/SHORT 색상 신호 + Binance 보조 데이터</div></div><div class="actions"><a class="btn" href="/export.csv">CSV 다운로드</a><button id="collect" class="btn">강제 수집</button><span id="live" class="live">● 정상 수집 중</span><span id="lastSync" class="muted"></span><span id="lastTop" class="muted"></span></div></div>
<div class="grid">
<div class="card s6 hero"><div><div class="label">현재 E-RANG 판정</div><div id="heroSignal" class="heroSignal wait">WAIT</div></div><div><div id="signalBits" class="big" style="font-size:15px">LONG OFF / SHORT OFF</div><div class="muted">E-RANG 화면의 색상 신호를 기준으로 판정합니다.</div></div></div>
<div class="card s2"><div class="label">BTCUSDT 현재가</div><div id="price" class="big">-</div><div id="priceDelta" class="muted">Binance 실시간</div></div>
<div class="card s2"><div class="label">수집 상태</div><div id="collectState" class="big ok">정상</div><div id="counts" class="muted">-</div></div>
<div class="card s2"><div class="label">DB / 서버</div><div id="db" class="big ok" style="font-size:21px">-</div><div id="server" class="muted">-</div></div>
<div class="card s6"><h2>E-RANG 진입가 <span class="muted">(현재 화면 기준)</span></h2><div class="tablewrap"><table><thead><tr><th>구분</th><th>진입 1<br>(25%)</th><th>진입 2<br>(40%)</th><th>진입 3<br>(60%)</th><th>진입 4<br>(100%)</th><th>진입 5<br>(예비)</th></tr></thead><tbody id="erangRows"></tbody></table></div></div>
<div class="card s6"><h2>Binance 보조 지표 (BTCUSDT)</h2><div class="metricTop"><div class="metric"><b>현재가 (Last Price)</b><strong id="bLast">-</strong></div><div class="metric"><b>펀딩비 (Funding Rate)</b><strong id="funding">-</strong></div><div class="metric"><b>미결제약정 (Open Interest)</b><strong id="oi">-</strong></div><div class="metric"><b>24h 거래량</b><strong id="vol24">-</strong></div></div><div class="tabs"><button class="tab" data-tf="1m">1분</button><button class="tab" data-tf="5m">5분</button><button class="tab active" data-tf="15m">15분</button><button class="tab" data-tf="1h">1시간</button></div><div class="tablewrap"><table class="indtable"><thead><tr><th>지표</th><th>현재값</th><th>상태</th></tr></thead><tbody id="indicatorRows"></tbody></table></div><div class="foot"><span>ⓘ 최근 220개 캔들 데이터 기반 계산</span><span id="bUpdate"></span></div></div>
<div class="card s6"><h2>현재가와 주요 진입가 거리 <span class="muted">(Long 기준)</span></h2><div id="distanceLong" class="dist"></div><h2 style="margin-top:16px">현재가와 주요 진입가 거리 <span class="muted">(Short 기준)</span></h2><div id="distanceShort" class="dist"></div></div>
<div class="card s6"><h2>신호 판정 근거</h2><div id="evidence" class="evidence muted">-</div></div>
<div class="card s12"><div class="detailHead"><h2>LONG / SHORT ON 공통 보조지표 패턴</h2><span class="hint">※ E-RANG ON 당시 Binance 지표의 상관 패턴이며 ON의 원인으로 확정한 값은 아닙니다.</span></div><div id="analysisSummary" class="analysisGrid"></div></div>
<div class="card s12"><div class="detailHead"><h2>선택한 수집 시점 보조지표</h2><span class="hint">아래 최근 수집 데이터 행을 클릭하면 당시 1m·5m·15m·1h 상태를 확인합니다.</span></div><div id="eventDetail" class="muted">수집 데이터 행을 선택하세요.</div></div>
<div class="card s12"><h2>최근 수집 데이터 <span class="muted" style="font-size:12px">(행 클릭 → 당시 보조지표)</span></h2><div class="tablewrap" style="max-height:360px;overflow:auto"><table><thead><tr><th>ID</th><th>시간</th><th>BTC</th><th>LONG</th><th>SHORT</th><th>판정 근거</th><th>15m RSI</th><th>15m MACD Hist</th><th>15m EMA20 관계</th><th>HTTP</th></tr></thead><tbody id="history"></tbody></table></div></div>
</div></div><script>
const $=id=>document.getElementById(id); let latest={},activeTF='15m',liveBinance={};
const n=v=>{let x=Number(v);return Number.isFinite(x)?x.toLocaleString('en-US',{maximumFractionDigits:4}):'-'}; const kst=v=>v?new Date(v).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'-';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function sigReason(r,side){
  let s=(r.parsed&&r.parsed.signals&&r.parsed.signals[side])||{};
  if(!s.active)return '';
  let color=s.detected_color||(side==='long'?r.long_color:r.short_color)||'-';
  let parts=(s.visual_evidence||'').split(' | ').filter(Boolean);
  let cssRule=parts.find(p=>p.startsWith('css '));
  let text=s.matched_text?`라벨 '${s.matched_text}'`:'';
  let out=`감지 배경색 ${color}`;
  if(cssRule)out+=` · ${cssRule}`;
  if(text)out+=` · ${text}`;
  return out;
}
function historyReasonCell(r){
  let blocks=[];
  if(r.long_signal)blocks.push(`<div class="reasonMini long"><b>LONG</b> ${esc(sigReason(r,'long'))}</div>`);
  if(r.short_signal)blocks.push(`<div class="reasonMini short"><b>SHORT</b> ${esc(sigReason(r,'short'))}</div>`);
  return blocks.length?blocks.join(''):'<span class="muted">-</span>';
}
function rowMap(parsed){let rows=parsed.table_rows||[], out={}; for(const r of rows){let t=(r.text||'').trim(), nums=r.numbers_raw||[]; if(/^Long\b/i.test(t))out.long=nums.slice(0,5); else if(/^Short\b/i.test(t))out.short=nums.slice(0,5); else if(/^TP\b/i.test(t)){if(!out.tpLong)out.tpLong=nums.slice(0,5);else out.tpShort=nums.slice(0,5)} else if(/^SL\b/i.test(t)){if(!out.slLong)out.slLong=nums.slice(0,5);else out.slShort=nums.slice(0,5)}} let sides=parsed.sides||{}; out.long=out.long||(sides.long?.entry_prices_guess||[]);out.short=out.short||(sides.short?.entry_prices_guess||[]);return out}
function cells(a){return [0,1,2,3,4].map(i=>`<td>${n(a?.[i])}</td>`).join('')}
function renderErang(parsed,priceOverride){let r=rowMap(parsed); let longOn=!!latest.long_signal, shortOn=!!latest.short_signal; $('erangRows').innerHTML=`<tr class="rowlong${longOn?' on':''}"><td>Long</td>${cells(r.long)}</tr><tr><td>TP (Long)</td>${cells(r.tpLong)}</tr><tr><td>SL (Long)</td>${cells(r.slLong)}</tr><tr class="rowshort${shortOn?' on':''}"><td>Short</td>${cells(r.short)}</tr><tr><td>TP (Short)</td>${cells(r.tpShort)}</tr><tr><td>SL (Short)</td>${cells(r.slShort)}</tr>`; let p=Number.isFinite(priceOverride)&&priceOverride>0?priceOverride:Number(latest.current_price||latest.current_price_raw); let renderDist=(elId,arr)=>{$(elId).innerHTML=[0,1,2,3,4].map(i=>{let x=Number(arr?.[i]),d=x-p,pct=p?d/p*100:0;return `<div class="entry"><b>진입 ${i+1}</b><strong>${Number.isFinite(d)?(d>=0?'+':'')+n(d):'-'}</strong><span class="${d>=0?'short':'long'}">${Number.isFinite(pct)?(pct>=0?'+':'')+pct.toFixed(2)+'%':'-'}</span></div>`}).join('')}; renderDist('distanceLong',r.long); renderDist('distanceShort',r.short)}
function statusFor(name,val,ind){if(val==null)return '-';if(name==='RSI 14')return val>=70?'과매수':val<=30?'과매도':'중립';if(name.startsWith('EMA')){let c=Number(ind.close);return c>val?'▲ 현재가 상회':'▼ 현재가 하회'}if(name==='MACD Histogram')return val>0?'▲ 양수 (상승 모멘텀)':val<0?'▼ 음수 (하락 모멘텀)':'중립';if(name==='MACD Line')return val>Number(ind.macd?.signal)?'▲ Signal 상회':'▼ Signal 하회';return '-'}
function clsStatus(s){return s.includes('▲')?'statusUp':s.includes('▼')?'statusDown':'statusNeutral'}
function renderIndicators(){let b=liveBinance&&Object.keys(liveBinance).length?liveBinance:(latest.binance||{}), ind=b.indicators?.[activeTF]||{}, mac=ind.macd||{}, bol=ind.bollinger20||{};let rows=[['현재가 (Close)',ind.close],['고가 (High)',ind.high],['저가 (Low)',ind.low],['거래량 (Volume)',ind.volume],['EMA 20',ind.ema20],['EMA 50',ind.ema50],['EMA 200',ind.ema200],['RSI 14',ind.rsi14],['MACD Line',mac.macd],['MACD Signal',mac.signal],['MACD Histogram',mac.histogram],['Bollinger 상단',bol.upper],['Bollinger 중단',bol.middle],['Bollinger 하단',bol.lower],['ATR 14',ind.atr14]];$('indicatorRows').innerHTML=rows.map(([name,val])=>{let st=statusFor(name,Number(val),ind);return `<tr><td>${name}</td><td>${n(val)}</td><td class="${clsStatus(st)}">${st}</td></tr>`}).join('');}
function renderEvidence(){let p=latest.parsed||{},s=p.signals||{},L=s.long||{},S=s.short||{};let active=latest.short_signal?'SHORT':latest.long_signal?'LONG':'WAIT';$('evidence').innerHTML=`<strong class="${active==='SHORT'?'short':active==='LONG'?'long':'wait'}">● ${active==='WAIT'?'활성 신호 없음':active+' 활성화 감지'}</strong><br>• Long 감지색: ${L.detected_color||'-'}<br>• Short 감지색: ${S.detected_color||'-'}<br>• 판정 기준: E-RANG Long/Short 라벨 셀의 활성 스타일/클래스`}
function eventTF(r,tf){return r?.binance?.indicators?.[tf]||{}}
function narrativeFor(ind){
  if(!ind||!Object.keys(ind).length)return [];
  let m=ind.macd||{}, b=ind.bollinger20||{}, bullets=[];
  let rsi=Number(ind.rsi14);
  if(Number.isFinite(rsi)){
    if(rsi<=30)bullets.push(`RSI ${n(rsi)} · 과매도권`);
    else if(rsi>=70)bullets.push(`RSI ${n(rsi)} · 과매수권`);
    else bullets.push(`RSI ${n(rsi)} · 중립 구간`);
  }
  let hist=Number(m.histogram);
  if(Number.isFinite(hist))bullets.push(`MACD 히스토그램 ${n(hist)} · ${hist>=0?'상승 모멘텀':'하락 모멘텀'}`);
  let close=Number(ind.close), ema20=Number(ind.ema20), ema50=Number(ind.ema50);
  if(Number.isFinite(close)&&Number.isFinite(ema20))bullets.push(`종가가 EMA20(${n(ema20)}) ${close>=ema20?'상회':'하회'}`);
  if(Number.isFinite(ema20)&&Number.isFinite(ema50))bullets.push(`EMA20 ${ema20>=ema50?'≥':'<'} EMA50 · ${ema20>=ema50?'단기 상승 배열':'단기 하락 배열'}`);
  if(Number.isFinite(close)&&Number.isFinite(b.upper)&&Number.isFinite(b.lower)&&(b.upper-b.lower)>0){
    let pos=(close-b.lower)/(b.upper-b.lower);
    if(pos<=0.15)bullets.push('볼린저밴드 하단 근접 · 되돌림 반등 구간 가능성');
    else if(pos>=0.85)bullets.push('볼린저밴드 상단 근접 · 과열·되돌림 하락 구간 가능성');
  }
  return bullets;
}
function renderEventDetail(r){if(!r)return;let sig=r.short_signal&&!r.long_signal?'SHORT':r.long_signal&&!r.short_signal?'LONG':r.short_signal&&r.long_signal?'BOTH':'WAIT';let cards=['1m','5m','15m','1h'].map(tf=>{let i=eventTF(r,tf),m=i.macd||{},b=i.bollinger20||{},reasons=narrativeFor(i);return `<div class="analysisBox"><h3>${tf} <span class="${sig==='SHORT'?'short':sig==='LONG'?'long':'wait'}">${sig}</span></h3><div class="chips"><span class="chip">RSI ${n(i.rsi14)}</span><span class="chip">EMA20 ${n(i.ema20)}</span><span class="chip">EMA50 ${n(i.ema50)}</span><span class="chip">EMA200 ${n(i.ema200)}</span><span class="chip">MACD Hist ${n(m.histogram)}</span><span class="chip">ATR ${n(i.atr14)}</span><span class="chip">BB 상 ${n(b.upper)}</span><span class="chip">BB 중 ${n(b.middle)}</span><span class="chip">BB 하 ${n(b.lower)}</span></div>${reasons.length?`<ul class="reasonList">${reasons.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`}).join('');let erangReasons=[r.long_signal?sigReason(r,'long'):'',r.short_signal?sigReason(r,'short'):''].filter(Boolean);let erangBlock=(r.long_signal||r.short_signal)?`<div class="reasonSummary"><b>E-RANG 실제 판정 근거</b>${r.long_signal?`<div class="reasonMini long"><b>LONG</b> ${esc(sigReason(r,'long'))}</div>`:''}${r.short_signal?`<div class="reasonMini short"><b>SHORT</b> ${esc(sigReason(r,'short'))}</div>`:''}</div>`:'';$('eventDetail').innerHTML=`<div style="margin-bottom:12px"><strong>ID ${r.id} · ${kst(r.observed_at)} · BTC ${n(r.current_price||r.current_price_raw)}</strong> · Funding ${r.binance?.premium_index?.lastFundingRate??'-'} · OI ${n(r.binance?.open_interest?.openInterest)}</div>${erangBlock}<div class="hint" style="margin:10px 0">※ 아래는 이 시점의 Binance 보조지표 상태를 정리한 참고용 관측입니다. E-RANG의 실제 ON 판정은 위 라벨 배경색 기준이며, 아래 지표 조합이 ON의 확정 원인이라는 뜻은 아닙니다.</div><div class="analysisGrid">${cards}</div>`}
function renderAnalysis(a){let sm=a?.summary||{};$('analysisSummary').innerHTML=['LONG','SHORT'].map(side=>{let g=sm[side]||{},t=g.timeframes?.['15m']||{};return `<div class="analysisBox"><h3 class="${side==='LONG'?'long':'short'}">${side} ON · ${g.count||0}건</h3><div class="chips"><span class="chip">15m 평균 RSI ${n(t.avg_rsi14)}</span><span class="chip">15m 평균 MACD Hist ${n(t.avg_macd_histogram)}</span><span class="chip">MACD Hist 양수 ${t.macd_hist_positive_pct??'-'}%</span><span class="chip">현재가 &gt; EMA20 ${t.price_above_ema20_pct??'-'}%</span><span class="chip">평균 ATR ${n(t.avg_atr14)}</span><span class="chip">평균 Funding ${n(g.avg_funding_rate)}</span></div><div class="hint" style="margin-top:10px">1m/5m/15m/1h 상세는 ON 발생 행을 클릭해서 확인</div></div>`}).join('')}
async function refresh(){try{let [sr,hr,ar]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/history?limit=80',{cache:'no-store'}),fetch('/api/signal-analysis?limit=200',{cache:'no-store'})]),s=await sr.json(),h=await hr.json(),a=await ar.json(),db=s.db||{};renderAnalysis(a);latest=db.latest||{};let sig=latest.short_signal&&!latest.long_signal?'SHORT':latest.long_signal&&!latest.short_signal?'LONG':latest.short_signal&&latest.long_signal?'BOTH':'WAIT';$('heroSignal').textContent=sig;$('heroSignal').className='heroSignal '+(sig==='SHORT'?'short':sig==='LONG'?'long':'wait');$('signalBits').textContent=`LONG ${latest.long_signal?'ON':'OFF'} / SHORT ${latest.short_signal?'ON':'OFF'}`;let lp=s.live_price||{},livePriceNum=Number(lp.price);$('price').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?n(livePriceNum):n(latest.current_price||latest.current_price_raw);$('priceDelta').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?('Binance 실시간 · '+kst(lp.updated_at)):'E-RANG (Binance 실시간가 대기중)';$('collectState').textContent=latest.success?'정상':'오류';$('counts').textContent=`성공 ${n(db.successful||0)} / 실패 ${n(db.failed||0)}`;$('db').textContent=db.database_ok?'Postgres OK':'Postgres 오류';$('server').textContent=`collector ${(s.collector||{}).running?'running':'idle'}`;$('lastTop').textContent='마지막 수집: '+kst(latest.observed_at);renderErang(latest.parsed||{},livePriceNum);let lb=s.live_binance?.snapshot||{};liveBinance=Object.keys(lb).length?lb:(latest.binance||{});let b=liveBinance;$('bLast').textContent=n(b.ticker_24h?.lastPrice);$('funding').textContent=b.premium_index?.lastFundingRate??'-';$('oi').textContent=n(b.open_interest?.openInterest);$('vol24').textContent=n(b.ticker_24h?.volume);$('bUpdate').textContent='업데이트: '+kst(s.live_binance?.updated_at||latest.observed_at);renderIndicators();renderEvidence();$('history').innerHTML=(h.items||[]).map((r,idx)=>{let i=eventTF(r,'15m'),mh=i.macd?.histogram,rel=Number(i.close)>Number(i.ema20)?'상회':Number(i.close)<Number(i.ema20)?'하회':'-';return `<tr class="clickrow" data-idx="${idx}"><td>${r.id}</td><td>${kst(r.observed_at)}</td><td>${n(r.current_price||r.current_price_raw)}</td><td class="${r.long_signal?'long':''}">${r.long_signal?'ON':'OFF'}</td><td class="${r.short_signal?'short':''}">${r.short_signal?'ON':'OFF'}</td><td class="reasonCell">${historyReasonCell(r)}</td><td>${n(i.rsi14)}</td><td class="${Number(mh)>=0?'statusUp':'statusDown'}">${n(mh)}</td><td>${rel}</td><td>${r.http_status||'-'}</td></tr>`}).join('');document.querySelectorAll('.clickrow').forEach(tr=>tr.onclick=()=>renderEventDetail((h.items||[])[Number(tr.dataset.idx)]));let firstOn=(h.items||[]).find(r=>r.long_signal||r.short_signal);if(firstOn)renderEventDetail(firstOn);$('live').textContent='● 실시간 동기화 중';$('live').className='live';lastSyncAt=Date.now();$('lastSync').textContent='방금 갱신'}catch(e){$('live').textContent='● UI 오류 (재시도 중)';$('live').className='short'}}
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{document.querySelectorAll('.tab').forEach(y=>y.classList.remove('active'));x.classList.add('active');activeTF=x.dataset.tf;renderIndicators()});$('collect').onclick=async()=>{await fetch('/api/collect-now',{method:'POST',cache:'no-store'});refresh()};
let lastSyncAt=Date.now();
setInterval(()=>{let s=Math.max(0,Math.round((Date.now()-lastSyncAt)/1000));$('lastSync').textContent=s<=1?'방금 갱신':s+'초 전 갱신'},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
refresh();
setInterval(refresh,1000);
</script></body></html>
"""


if __name__ == "__main__":
    log("BOOT", "starting Coin Monitor v2")
    try:
        init_db()
    except Exception as exc:
        log("DB_ERROR", f"initial schema failed: {type(exc).__name__}: {exc}")
    with _state_lock:
        telegram_state["enabled"] = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    if telegram_state["enabled"]:
        try:
            summary = db_summary()
            latest = summary.get("latest") or {}
            with _state_lock:
                telegram_state["last_long_signal"] = latest.get("long_signal")
                telegram_state["last_short_signal"] = latest.get("short_signal")
            log("TELEGRAM_BOOT", f"seeded prev state long={latest.get('long_signal')} short={latest.get('short_signal')}")
        except Exception as exc:
            log("TELEGRAM_ERROR", f"failed to seed previous signal state: {type(exc).__name__}: {exc}")
    else:
        log("TELEGRAM_BOOT", "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, notifications disabled")
    start_collector_once()
    start_live_price_once()
    start_binance_snapshot_once()
    port = int(os.getenv("PORT", "8080"))
    log("WEB", f"listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
