from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import psycopg
import requests
from flask import Flask, Response, jsonify, request
from psycopg.types.json import Jsonb

from parser import parse_page
from signal_analysis import signal_snapshot, summarize_signal_records

try:
    from binance_data import collect_binance_snapshot
except Exception:  # pragma: no cover
    collect_binance_snapshot = None

TARGET_URL = os.getenv("TARGET_URL", "https://e-rang.kr/api/coin.php")
INTERVAL = max(60, int(os.getenv("INTERVAL_SECONDS", "60")))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
USER_AGENT = os.getenv("USER_AGENT", "CoinMonitor/2.0 (+public-page-observation)")
ENABLE_BINANCE = os.getenv("ENABLE_BINANCE", "true").lower() not in {"0", "false", "no", "off"}
MAX_HTML_BYTES = int(os.getenv("MAX_HTML_BYTES", "2000000"))

app = Flask(__name__)
collector_state: Dict[str, Any] = {
    "started": False,
    "booted_at": None,
    "last_started_at": None,
    "last_finished_at": None,
    "next_run_at": None,
    "last_error": None,
    "last_saved_id": None,
    "running": False,
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
        binance_json: Dict[str, Any] = {}
        if ENABLE_BINANCE and collect_binance_snapshot is not None:
            try:
                binance_json = collect_binance_snapshot()
                b_price = (binance_json.get("ticker_24h") or {}).get("lastPrice")
                log("BINANCE", f"snapshot ok lastPrice={b_price or '-'} errors={len(binance_json.get('errors') or {})}")
            except Exception as exc:
                binance_json = {"errors": {"snapshot": f"{type(exc).__name__}: {exc}"}}
                log("BINANCE_ERROR", binance_json["errors"]["snapshot"])
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
    while True:
        started = time.monotonic()
        collect_once()
        elapsed = time.monotonic() - started
        sleep_for = max(1.0, INTERVAL - elapsed)
        with _state_lock:
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
    return jsonify(
        {
            "service": "coin-monitor",
            "target_url": TARGET_URL,
            "interval_seconds": INTERVAL,
            "binance_enabled": ENABLE_BINANCE,
            "collector": state,
            "db": summary,
        }
    )


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
:root{--bg:#07101f;--panel:#0e1b31;--panel2:#101f38;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--red:#ff5364;--green:#35e29a;--yellow:#ffc83d}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1540px;margin:auto;padding:24px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.top h1{margin:0;font-size:30px}.sub,.muted{color:var(--muted)}.sub{margin-top:5px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#152540;color:#fff;padding:11px 15px;border-radius:11px;text-decoration:none;font-weight:800;cursor:pointer}.live{color:var(--green);font-weight:900}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:18px;box-shadow:0 14px 32px #0004;min-width:0}.s2{grid-column:span 2}.s3{grid-column:span 3}.s4{grid-column:span 4}.s6{grid-column:span 6}.s8{grid-column:span 8}.s12{grid-column:span 12}.label{font-size:13px;color:#a9bfdf;font-weight:800}.big{font-size:29px;font-weight:950;margin-top:7px}.hero{display:flex;align-items:center;gap:22px;min-height:110px}.heroSignal{font-size:42px;font-weight:1000}.short{color:var(--red)}.long{color:var(--blue)}.wait{color:var(--yellow)}.ok{color:var(--green)}h2{font-size:18px;margin:0 0 14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{background:#132947;color:#c7dcfa;font-size:12px}.rowlong.on td:first-child{font-weight:950;color:var(--blue)}.rowshort.on td:first-child{background:#ef3340;color:#fff;font-weight:950}.entryrow{display:grid;grid-template-columns:82px repeat(5,1fr);gap:8px;align-items:stretch;margin-bottom:10px}.sideLabel{display:flex;align-items:center;font-size:20px;font-weight:950}.entry{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:10px;min-width:0}.entry b{font-size:12px;color:#9fb8db;display:block}.entry strong{font-size:17px;display:block;margin-top:5px;white-space:nowrap}.dist{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.dist .entry strong{font-size:20px}.tabs{display:flex;gap:7px;margin:12px 0}.tab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer}.tab.active{background:#168cff;color:white}.metricTop{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:12px}.metric b{display:block;color:#9fb8db;font-size:12px}.metric strong{display:block;font-size:19px;margin-top:6px}.indtable{min-width:0}.indtable td:nth-child(2){font-weight:800}.statusUp{color:var(--green)}.statusDown{color:var(--red)}.statusNeutral{color:#dbe7f8}.evidence{line-height:1.7}.evidence strong{font-size:18px}.foot{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:13px;gap:12px}.nowrap{white-space:nowrap}.clickrow{cursor:pointer}.clickrow:hover{background:#132947}.analysisGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.analysisBox{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:14px}.analysisBox h3{margin:0 0 10px;font-size:17px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{background:#102746;border:1px solid var(--line);border-radius:999px;padding:6px 9px;font-size:12px}.detailHead{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.hint{font-size:12px;color:var(--muted)}
@media(max-width:1050px){.s2,.s3,.s4,.s6,.s8{grid-column:span 12}.metricTop{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}.entryrow{grid-template-columns:70px repeat(5,130px);overflow-x:auto}.dist{grid-template-columns:repeat(5,140px);overflow-x:auto}}@media(max-width:600px){.wrap{padding:12px}.top{flex-direction:column}.metricTop{grid-template-columns:1fr 1fr}.heroSignal{font-size:34px}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Coin Monitor</h1><div class="sub">e-rang coin.php 1분 수집 + LONG/SHORT 색상 신호 + Binance 보조 데이터</div></div><div class="actions"><a class="btn" href="/export.csv">CSV 다운로드</a><button id="collect" class="btn">강제 수집</button><span id="live" class="live">● 정상 수집 중</span><span id="lastTop" class="muted"></span></div></div>
<div class="grid">
<div class="card s6 hero"><div><div class="label">현재 E-RANG 판정</div><div id="heroSignal" class="heroSignal wait">WAIT</div></div><div><div id="signalBits" class="big" style="font-size:15px">LONG OFF / SHORT OFF</div><div class="muted">E-RANG 화면의 색상 신호를 기준으로 판정합니다.</div></div></div>
<div class="card s2"><div class="label">BTCUSDT 현재가</div><div id="price" class="big">-</div><div id="priceDelta" class="muted">E-RANG</div></div>
<div class="card s2"><div class="label">수집 상태</div><div id="collectState" class="big ok">정상</div><div id="counts" class="muted">-</div></div>
<div class="card s2"><div class="label">DB / 서버</div><div id="db" class="big ok" style="font-size:21px">-</div><div id="server" class="muted">-</div></div>
<div class="card s6"><h2>E-RANG 진입가 <span class="muted">(현재 화면 기준)</span></h2><div class="tablewrap"><table><thead><tr><th>구분</th><th>진입 1<br>(25%)</th><th>진입 2<br>(40%)</th><th>진입 3<br>(60%)</th><th>진입 4<br>(100%)</th><th>진입 5<br>(예비)</th></tr></thead><tbody id="erangRows"></tbody></table></div></div>
<div class="card s6"><h2>Binance 보조 지표 (BTCUSDT)</h2><div class="metricTop"><div class="metric"><b>현재가 (Last Price)</b><strong id="bLast">-</strong></div><div class="metric"><b>펀딩비 (Funding Rate)</b><strong id="funding">-</strong></div><div class="metric"><b>미결제약정 (Open Interest)</b><strong id="oi">-</strong></div><div class="metric"><b>24h 거래량</b><strong id="vol24">-</strong></div></div><div class="tabs"><button class="tab" data-tf="1m">1분</button><button class="tab" data-tf="5m">5분</button><button class="tab active" data-tf="15m">15분</button><button class="tab" data-tf="1h">1시간</button></div><div class="tablewrap"><table class="indtable"><thead><tr><th>지표</th><th>현재값</th><th>상태</th></tr></thead><tbody id="indicatorRows"></tbody></table></div><div class="foot"><span>ⓘ 최근 220개 캔들 데이터 기반 계산</span><span id="bUpdate"></span></div></div>
<div class="card s6"><h2>현재가와 주요 진입가 거리 <span class="muted">(Long 기준)</span></h2><div id="distanceLong" class="dist"></div><h2 style="margin-top:16px">현재가와 주요 진입가 거리 <span class="muted">(Short 기준)</span></h2><div id="distanceShort" class="dist"></div></div>
<div class="card s6"><h2>신호 판정 근거</h2><div id="evidence" class="evidence muted">-</div></div>
<div class="card s12"><div class="detailHead"><h2>LONG / SHORT ON 공통 보조지표 패턴</h2><span class="hint">※ E-RANG ON 당시 Binance 지표의 상관 패턴이며 ON의 원인으로 확정한 값은 아닙니다.</span></div><div id="analysisSummary" class="analysisGrid"></div></div>
<div class="card s12"><div class="detailHead"><h2>선택한 수집 시점 보조지표</h2><span class="hint">아래 최근 수집 데이터 행을 클릭하면 당시 1m·5m·15m·1h 상태를 확인합니다.</span></div><div id="eventDetail" class="muted">수집 데이터 행을 선택하세요.</div></div>
<div class="card s12"><h2>최근 수집 데이터 <span class="muted" style="font-size:12px">(행 클릭 → 당시 보조지표)</span></h2><div class="tablewrap" style="max-height:360px;overflow:auto"><table><thead><tr><th>ID</th><th>시간</th><th>BTC</th><th>LONG</th><th>SHORT</th><th>15m RSI</th><th>15m MACD Hist</th><th>15m EMA20 관계</th><th>HTTP</th></tr></thead><tbody id="history"></tbody></table></div></div>
</div></div><script>
const $=id=>document.getElementById(id); let latest={},activeTF='15m';
const n=v=>{let x=Number(v);return Number.isFinite(x)?x.toLocaleString('en-US',{maximumFractionDigits:4}):'-'}; const kst=v=>v?new Date(v).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'-';
function rowMap(parsed){let rows=parsed.table_rows||[], out={}; for(const r of rows){let t=(r.text||'').trim(), nums=r.numbers_raw||[]; if(/^Long\b/i.test(t))out.long=nums.slice(0,5); else if(/^Short\b/i.test(t))out.short=nums.slice(0,5); else if(/^TP\b/i.test(t)){if(!out.tpLong)out.tpLong=nums.slice(0,5);else out.tpShort=nums.slice(0,5)} else if(/^SL\b/i.test(t)){if(!out.slLong)out.slLong=nums.slice(0,5);else out.slShort=nums.slice(0,5)}} let sides=parsed.sides||{}; out.long=out.long||(sides.long?.entry_prices_guess||[]);out.short=out.short||(sides.short?.entry_prices_guess||[]);return out}
function cells(a){return [0,1,2,3,4].map(i=>`<td>${n(a?.[i])}</td>`).join('')}
function renderErang(parsed){let r=rowMap(parsed); let longOn=!!latest.long_signal, shortOn=!!latest.short_signal; $('erangRows').innerHTML=`<tr class="rowlong${longOn?' on':''}"><td>Long</td>${cells(r.long)}</tr><tr><td>TP (Long)</td>${cells(r.tpLong)}</tr><tr><td>SL (Long)</td>${cells(r.slLong)}</tr><tr class="rowshort${shortOn?' on':''}"><td>Short</td>${cells(r.short)}</tr><tr><td>TP (Short)</td>${cells(r.tpShort)}</tr><tr><td>SL (Short)</td>${cells(r.slShort)}</tr>`; let p=Number(latest.current_price||latest.current_price_raw); let renderDist=(elId,arr)=>{$(elId).innerHTML=[0,1,2,3,4].map(i=>{let x=Number(arr?.[i]),d=x-p,pct=p?d/p*100:0;return `<div class="entry"><b>진입 ${i+1}</b><strong>${Number.isFinite(d)?(d>=0?'+':'')+n(d):'-'}</strong><span class="${d>=0?'short':'long'}">${Number.isFinite(pct)?(pct>=0?'+':'')+pct.toFixed(2)+'%':'-'}</span></div>`}).join('')}; renderDist('distanceLong',r.long); renderDist('distanceShort',r.short)}
function statusFor(name,val,ind){if(val==null)return '-';if(name==='RSI 14')return val>=70?'과매수':val<=30?'과매도':'중립';if(name.startsWith('EMA')){let c=Number(ind.close);return c>val?'▲ 현재가 상회':'▼ 현재가 하회'}if(name==='MACD Histogram')return val>0?'▲ 양수 (상승 모멘텀)':val<0?'▼ 음수 (하락 모멘텀)':'중립';if(name==='MACD Line')return val>Number(ind.macd?.signal)?'▲ Signal 상회':'▼ Signal 하회';return '-'}
function clsStatus(s){return s.includes('▲')?'statusUp':s.includes('▼')?'statusDown':'statusNeutral'}
function renderIndicators(){let b=latest.binance||{}, ind=b.indicators?.[activeTF]||{}, mac=ind.macd||{}, bol=ind.bollinger20||{};let rows=[['현재가 (Close)',ind.close],['고가 (High)',ind.high],['저가 (Low)',ind.low],['거래량 (Volume)',ind.volume],['EMA 20',ind.ema20],['EMA 50',ind.ema50],['EMA 200',ind.ema200],['RSI 14',ind.rsi14],['MACD Line',mac.macd],['MACD Signal',mac.signal],['MACD Histogram',mac.histogram],['Bollinger 상단',bol.upper],['Bollinger 중단',bol.middle],['Bollinger 하단',bol.lower],['ATR 14',ind.atr14]];$('indicatorRows').innerHTML=rows.map(([name,val])=>{let st=statusFor(name,Number(val),ind);return `<tr><td>${name}</td><td>${n(val)}</td><td class="${clsStatus(st)}">${st}</td></tr>`}).join('');}
function renderEvidence(){let p=latest.parsed||{},s=p.signals||{},L=s.long||{},S=s.short||{};let active=latest.short_signal?'SHORT':latest.long_signal?'LONG':'WAIT';$('evidence').innerHTML=`<strong class="${active==='SHORT'?'short':active==='LONG'?'long':'wait'}">● ${active==='WAIT'?'활성 신호 없음':active+' 활성화 감지'}</strong><br>• Long 감지색: ${L.detected_color||'-'}<br>• Short 감지색: ${S.detected_color||'-'}<br>• 판정 기준: E-RANG Long/Short 라벨 셀의 활성 스타일/클래스`}
function eventTF(r,tf){return r?.binance?.indicators?.[tf]||{}}
function renderEventDetail(r){if(!r)return;let sig=r.short_signal&&!r.long_signal?'SHORT':r.long_signal&&!r.short_signal?'LONG':r.short_signal&&r.long_signal?'BOTH':'WAIT';let cards=['1m','5m','15m','1h'].map(tf=>{let i=eventTF(r,tf),m=i.macd||{},b=i.bollinger20||{};return `<div class="analysisBox"><h3>${tf} <span class="${sig==='SHORT'?'short':sig==='LONG'?'long':'wait'}">${sig}</span></h3><div class="chips"><span class="chip">RSI ${n(i.rsi14)}</span><span class="chip">EMA20 ${n(i.ema20)}</span><span class="chip">EMA50 ${n(i.ema50)}</span><span class="chip">EMA200 ${n(i.ema200)}</span><span class="chip">MACD Hist ${n(m.histogram)}</span><span class="chip">ATR ${n(i.atr14)}</span><span class="chip">BB 상 ${n(b.upper)}</span><span class="chip">BB 중 ${n(b.middle)}</span><span class="chip">BB 하 ${n(b.lower)}</span></div></div>`}).join('');$('eventDetail').innerHTML=`<div style="margin-bottom:12px"><strong>ID ${r.id} · ${kst(r.observed_at)} · BTC ${n(r.current_price||r.current_price_raw)}</strong> · Funding ${r.binance?.premium_index?.lastFundingRate??'-'} · OI ${n(r.binance?.open_interest?.openInterest)}</div><div class="analysisGrid">${cards}</div>`}
function renderAnalysis(a){let sm=a?.summary||{};$('analysisSummary').innerHTML=['LONG','SHORT'].map(side=>{let g=sm[side]||{},t=g.timeframes?.['15m']||{};return `<div class="analysisBox"><h3 class="${side==='LONG'?'long':'short'}">${side} ON · ${g.count||0}건</h3><div class="chips"><span class="chip">15m 평균 RSI ${n(t.avg_rsi14)}</span><span class="chip">15m 평균 MACD Hist ${n(t.avg_macd_histogram)}</span><span class="chip">MACD Hist 양수 ${t.macd_hist_positive_pct??'-'}%</span><span class="chip">현재가 &gt; EMA20 ${t.price_above_ema20_pct??'-'}%</span><span class="chip">평균 ATR ${n(t.avg_atr14)}</span><span class="chip">평균 Funding ${n(g.avg_funding_rate)}</span></div><div class="hint" style="margin-top:10px">1m/5m/15m/1h 상세는 ON 발생 행을 클릭해서 확인</div></div>`}).join('')}
async function refresh(){try{let [sr,hr,ar]=await Promise.all([fetch('/api/status'),fetch('/api/history?limit=80'),fetch('/api/signal-analysis?limit=200')]),s=await sr.json(),h=await hr.json(),a=await ar.json(),db=s.db||{};renderAnalysis(a);latest=db.latest||{};let sig=latest.short_signal&&!latest.long_signal?'SHORT':latest.long_signal&&!latest.short_signal?'LONG':latest.short_signal&&latest.long_signal?'BOTH':'WAIT';$('heroSignal').textContent=sig;$('heroSignal').className='heroSignal '+(sig==='SHORT'?'short':sig==='LONG'?'long':'wait');$('signalBits').textContent=`LONG ${latest.long_signal?'ON':'OFF'} / SHORT ${latest.short_signal?'ON':'OFF'}`;$('price').textContent=n(latest.current_price||latest.current_price_raw);$('collectState').textContent=latest.success?'정상':'오류';$('counts').textContent=`성공 ${n(db.successful||0)} / 실패 ${n(db.failed||0)}`;$('db').textContent=db.database_ok?'Postgres OK':'Postgres 오류';$('server').textContent=`collector ${(s.collector||{}).running?'running':'idle'}`;$('lastTop').textContent='마지막 수집: '+kst(latest.observed_at);renderErang(latest.parsed||{});let b=latest.binance||{};$('bLast').textContent=n(b.ticker_24h?.lastPrice);$('funding').textContent=b.premium_index?.lastFundingRate??'-';$('oi').textContent=n(b.open_interest?.openInterest);$('vol24').textContent=n(b.ticker_24h?.volume);$('bUpdate').textContent='업데이트: '+kst(latest.observed_at);renderIndicators();renderEvidence();$('history').innerHTML=(h.items||[]).map((r,idx)=>{let i=eventTF(r,'15m'),mh=i.macd?.histogram,rel=Number(i.close)>Number(i.ema20)?'상회':Number(i.close)<Number(i.ema20)?'하회':'-';return `<tr class="clickrow" data-idx="${idx}"><td>${r.id}</td><td>${kst(r.observed_at)}</td><td>${n(r.current_price||r.current_price_raw)}</td><td class="${r.long_signal?'long':''}">${r.long_signal?'ON':'OFF'}</td><td class="${r.short_signal?'short':''}">${r.short_signal?'ON':'OFF'}</td><td>${n(i.rsi14)}</td><td class="${Number(mh)>=0?'statusUp':'statusDown'}">${n(mh)}</td><td>${rel}</td><td>${r.http_status||'-'}</td></tr>`}).join('');document.querySelectorAll('.clickrow').forEach(tr=>tr.onclick=()=>renderEventDetail((h.items||[])[Number(tr.dataset.idx)]));let firstOn=(h.items||[]).find(r=>r.long_signal||r.short_signal);if(firstOn)renderEventDetail(firstOn)}catch(e){$('live').textContent='● UI 오류';$('live').className='short'}}
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{document.querySelectorAll('.tab').forEach(y=>y.classList.remove('active'));x.classList.add('active');activeTF=x.dataset.tf;renderIndicators()});$('collect').onclick=async()=>{await fetch('/api/collect-now',{method:'POST'});refresh()};refresh();setInterval(refresh,5000);
</script></body></html>
"""


if __name__ == "__main__":
    log("BOOT", "starting Coin Monitor v2")
    try:
        init_db()
    except Exception as exc:
        log("DB_ERROR", f"initial schema failed: {type(exc).__name__}: {exc}")
    start_collector_once()
    port = int(os.getenv("PORT", "8080"))
    log("WEB", f"listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
