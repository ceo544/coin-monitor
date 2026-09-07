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
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coin Monitor</title>
  <style>
    :root{--bg:#0f1220;--panel:#171b2e;--card:#20263d;--text:#eef2ff;--muted:#9aa4bf;--line:#313852;--long:#22c55e;--short:#ef4444;--accent:#8b5cf6;--warn:#f59e0b}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(135deg,#090b14,#14182a);color:var(--text);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    .wrap{max-width:1280px;margin:0 auto;padding:24px} .top{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
    h1{margin:0;font-size:26px;letter-spacing:-.03em}.sub{color:var(--muted);font-size:13px;margin-top:6px}.badge{display:inline-flex;align-items:center;gap:7px;padding:7px 11px;border-radius:999px;background:rgba(34,197,94,.14);color:#86efac;font-weight:700;font-size:13px}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:rgba(32,38,61,.88);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 10px 30px rgba(0,0,0,.22)}
    .span3{grid-column:span 3}.span4{grid-column:span 4}.span6{grid-column:span 6}.span8{grid-column:span 8}.span12{grid-column:span 12}
    .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:700}.value{font-size:28px;font-weight:850;margin-top:8px;letter-spacing:-.03em}.small{font-size:13px;color:var(--muted);margin-top:6px}.signal{display:flex;align-items:center;justify-content:space-between;gap:12px}.signal strong{font-size:24px}.pill{border-radius:999px;padding:8px 12px;font-weight:800;font-size:13px}.on-long{background:rgba(34,197,94,.16);color:#86efac}.on-short{background:rgba(239,68,68,.16);color:#fca5a5}.off{background:rgba(148,163,184,.12);color:#cbd5e1}.actions{display:flex;gap:10px;flex-wrap:wrap}button,a.btn{appearance:none;border:1px solid var(--line);background:#252b45;color:var(--text);border-radius:12px;padding:10px 13px;font-weight:800;text-decoration:none;cursor:pointer}button:hover,a.btn:hover{border-color:var(--accent)}
    table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-size:12px;font-weight:800}.num{text-align:right;font-variant-numeric:tabular-nums}.ok{color:#86efac}.bad{color:#fca5a5}.warn{color:#fbbf24}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.scroll{overflow:auto;max-height:560px}.section-title{font-weight:850;margin:0 0 12px 0;font-size:17px}.sidebox{display:grid;grid-template-columns:1fr 1fr;gap:12px}.entry-list{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.entry{background:#151a2c;border:1px solid var(--line);border-radius:12px;padding:10px}.entry b{display:block;font-size:11px;color:var(--muted);margin-bottom:6px}.entry span{font-variant-numeric:tabular-nums;font-weight:800}.errorbox{white-space:pre-wrap;color:#fca5a5;font-size:13px}.json{white-space:pre-wrap;max-height:260px;overflow:auto;background:#111526;border:1px solid var(--line);border-radius:12px;padding:12px;color:#c4b5fd;font-size:12px}
    @media(max-width:900px){.span3,.span4,.span6,.span8{grid-column:span 12}.wrap{padding:14px}.entry-list{grid-template-columns:repeat(2,1fr)}.value{font-size:23px}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div><h1>Coin Monitor</h1><div class="sub">e-rang coin.php 1분 수집 + LONG/SHORT 색상 신호 + Binance 보조 데이터</div></div>
      <div class="actions"><a class="btn" href="/export.csv">CSV 다운로드</a><button id="collectBtn" type="button">강제 수집</button><span id="liveBadge" class="badge"><span class="dot"></span>LIVE</span></div>
    </div>
    <div class="grid">
      <div class="card span12" id="decisionCard"><div class="label">현재 E-RANG 판정</div><div id="decision" class="value" style="font-size:38px">판정 대기</div><div id="decisionSub" class="small">LONG/SHORT 활성 색상을 확인합니다.</div></div>
      <div class="card span3"><div class="label">BTCUSDT</div><div id="btc" class="value">-</div><div id="btcSub" class="small">대기 중</div></div>
      <div class="card span3"><div class="label">LONG SIGNAL</div><div class="signal"><strong id="longText">-</strong><span id="longPill" class="pill off">OFF</span></div><div id="longColor" class="small">color: -</div></div>
      <div class="card span3"><div class="label">SHORT SIGNAL</div><div class="signal"><strong id="shortText">-</strong><span id="shortPill" class="pill off">OFF</span></div><div id="shortColor" class="small">color: -</div></div>
      <div class="card span3"><div class="label">수집 상태</div><div id="statusText" class="value">-</div><div id="statusSub" class="small">-</div></div>
      <div class="card span4"><div class="label">총 수집건수</div><div id="total" class="value">0</div><div id="successCount" class="small">성공 0 / 실패 0</div></div>
      <div class="card span4"><div class="label">마지막 수집</div><div id="lastTime" class="value" style="font-size:20px">-</div><div id="nextTime" class="small">다음 수집 -</div></div>
      <div class="card span4"><div class="label">DB / 서버</div><div id="dbText" class="value" style="font-size:22px">-</div><div id="serverSub" class="small">-</div></div>
      <div class="card span6"><h2 class="section-title">진입가 추정 배열</h2><div id="entryGrid" class="sidebox"></div></div>
      <div class="card span6"><h2 class="section-title">Binance 보조 지표</h2><div id="binanceBox" class="json">-</div></div>
      <div class="card span12"><h2 class="section-title">최근 수집 데이터</h2><div class="scroll"><table><thead><tr><th>ID</th><th>시간</th><th class="num">BTC</th><th>LONG</th><th>SHORT</th><th>HTTP</th><th>오류</th></tr></thead><tbody id="history"></tbody></table></div></div>
      <div class="card span12"><h2 class="section-title">최근 원본 파싱 JSON</h2><div id="latestJson" class="json">-</div></div>
    </div>
  </div>
  <script>
    const $ = (id)=>document.getElementById(id);
    const fmt = (v)=> v === null || v === undefined || v === '' ? '-' : String(v);
    const fmtNum = (v)=>{ const n=Number(v); return Number.isFinite(n)?n.toLocaleString('en-US',{maximumFractionDigits:2}):fmt(v); };
    function kst(iso){ if(!iso) return '-'; return new Date(iso).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}); }
    function setPill(el,on,cls){ el.className='pill '+(on?cls:'off'); el.textContent=on?'ON':'OFF'; }
    function sideEntries(title, obj){
      const nums = (obj && (obj.entry_prices_guess || obj.numbers_raw)) || [];
      const cards = [0,1,2,3,4].map(i=>`<div class="entry"><b>${i+1}차</b><span>${fmtNum(nums[i])}</span></div>`).join('');
      return `<div><div class="label" style="margin-bottom:8px">${title}</div><div class="entry-list">${cards}</div></div>`;
    }
    async function refresh(){
      try{
        const [statusRes, histRes] = await Promise.all([fetch('/api/status'), fetch('/api/history?limit=80')]);
        const status = await statusRes.json(); const hist = await histRes.json();
        const db = status.db || {}; const latest = db.latest || {}; const parsed = latest.parsed || {}; const sides = parsed.sides || {};
        const isLong=!!latest.long_signal, isShort=!!latest.short_signal; const decision=isLong&&!isShort?'LONG':isShort&&!isLong?'SHORT':isLong&&isShort?'충돌':'WAIT'; $('decision').textContent=decision; $('decision').className='value '+(decision==='SHORT'?'bad':decision==='LONG'?'ok':'warn'); $('decisionSub').textContent=`LONG ${isLong?'ON':'OFF'} / SHORT ${isShort?'ON':'OFF'} · 5초마다 UI 갱신`;
        $('btc').textContent = fmtNum(latest.current_price || latest.current_price_raw);
        $('btcSub').textContent = latest.success ? '수집 성공' : (latest.error || '아직 데이터 없음');
        $('longText').textContent = latest.long_signal ? 'ACTIVE' : 'WAIT'; setPill($('longPill'), latest.long_signal, 'on-long'); $('longColor').textContent = 'color: '+fmt(latest.long_color);
        $('shortText').textContent = latest.short_signal ? 'ACTIVE' : 'WAIT'; setPill($('shortPill'), latest.short_signal, 'on-short'); $('shortColor').textContent = 'color: '+fmt(latest.short_color);
        $('statusText').textContent = latest.success ? '정상' : (db.database_ok ? '대기/오류' : 'DB 오류'); $('statusText').className='value '+(latest.success?'ok':'bad');
        $('statusSub').textContent = status.target_url + ' / '+status.interval_seconds+'초';
        $('total').textContent = fmtNum(db.total || 0); $('successCount').textContent = `성공 ${fmtNum(db.successful||0)} / 실패 ${fmtNum(db.failed||0)}`;
        $('lastTime').textContent = kst(latest.observed_at); $('nextTime').textContent = '다음 수집: '+kst((status.collector||{}).next_run_at);
        $('dbText').textContent = db.database_ok ? 'Postgres OK' : 'Postgres 오류'; $('dbText').className='value '+(db.database_ok?'ok':'bad');
        $('serverSub').textContent = 'collector started='+fmt((status.collector||{}).started)+' / running='+fmt((status.collector||{}).running);
        $('entryGrid').innerHTML = sideEntries('LONG', sides.long) + sideEntries('SHORT', sides.short);
        const bj = latest.binance || {}; const compact = { ticker_lastPrice: bj.ticker_24h && bj.ticker_24h.lastPrice, fundingRate: bj.premium_index && bj.premium_index.lastFundingRate, openInterest: bj.open_interest && bj.open_interest.openInterest, indicators: bj.indicators || {}, errors: bj.errors || {} };
        $('binanceBox').textContent = JSON.stringify(compact,null,2);
        $('latestJson').textContent = JSON.stringify(parsed,null,2);
        $('history').innerHTML = (hist.items||[]).map(r=>`<tr><td class="mono">${r.id}</td><td>${kst(r.observed_at)}</td><td class="num">${fmtNum(r.current_price||r.current_price_raw)}</td><td>${r.long_signal?'<span class="pill on-long">ON</span>':'<span class="pill off">OFF</span>'}</td><td>${r.short_signal?'<span class="pill on-short">ON</span>':'<span class="pill off">OFF</span>'}</td><td>${fmt(r.http_status)}</td><td class="errorbox">${fmt(r.error)}</td></tr>`).join('');
      }catch(err){ $('liveBadge').textContent='UI ERROR'; $('statusText').textContent='UI 오류'; $('statusSub').textContent=err.message; }
    }
    $('collectBtn').addEventListener('click', async ()=>{ $('collectBtn').disabled=true; $('collectBtn').textContent='수집 중'; try{ await fetch('/api/collect-now',{method:'POST'}); await refresh(); } finally { $('collectBtn').disabled=false; $('collectBtn').textContent='강제 수집'; }});
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>
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
