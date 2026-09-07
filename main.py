from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import psycopg
import requests
from flask import Flask, Response, jsonify, redirect, request, session, url_for
from psycopg.types.json import Jsonb

from parser import parse_page
from signal_analysis import signal_snapshot, summarize_signal_records

try:
    from binance_data import collect_binance_snapshot, fetch_last_price
except Exception:  # pragma: no cover
    collect_binance_snapshot = None
    fetch_last_price = None

try:
    import bitget_client
except Exception:  # pragma: no cover
    bitget_client = None

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
# How often the server polls Bitget for the "my position" card - kept on its
# own slower loop (not per-browser-request) so multiple open dashboard tabs
# never multiply real Bitget API calls.
BITGET_POLL_INTERVAL = float(os.getenv("BITGET_POLL_INTERVAL_SECONDS", "5"))
# Telegram alert on Long/Short signal turning ON (edge-triggered: fires once
# when it flips from OFF to ON, not on every 30s poll while it stays ON).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_NOTIFY_OFF = os.getenv("TELEGRAM_NOTIFY_OFF", "true").lower() not in {"0", "false", "no", "off"}
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
KST = ZoneInfo("Asia/Seoul")

# Simple single-user login so the dashboard/API/CSV export aren't publicly
# viewable. Defaults match what was requested, but can (and for a real
# deployment, should) be overridden via Railway env vars instead of leaving
# credentials in source. FLASK_SECRET_KEY should also be set to a fixed
# value in production - otherwise a fresh random key is generated on every
# restart/redeploy, which invalidates existing login sessions and forces
# everyone to log in again each time the app restarts.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1Q2w3e4r5t!!")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
if not FLASK_SECRET_KEY:
    print("[BOOT] FLASK_SECRET_KEY not set - using a random key, so everyone will be logged out on the next restart/redeploy", flush=True)

# Paths reachable without logging in. /healthz stays public for platform
# health checks (e.g. a Railway healthcheckPath) that can't submit a login.
PUBLIC_PATHS = {"/login", "/healthz"}


@app.before_request
def _require_login() -> Any:
    if request.path in PUBLIC_PATHS:
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/") or request.path == "/export.csv":
        return jsonify({"ok": False, "error": "login required"}), 401
    return redirect(url_for("login_page", next=request.path))


LOGIN_HTML = r"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Coin Monitor · 로그인</title>
<style>
:root{--bg:#07101f;--panel:#0e1b31;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--red:#ff5364}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:32px;box-shadow:0 14px 32px #0006;width:100%;max-width:360px}
h1{margin:0 0 6px;font-size:22px}.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
label{display:block;font-size:13px;color:#a9bfdf;font-weight:700;margin-bottom:6px}
input{width:100%;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:#0a172b;color:var(--text);font-size:15px;margin-bottom:16px}
input:focus{outline:2px solid var(--blue)}
button{width:100%;padding:12px;border-radius:10px;border:none;background:var(--blue);color:#04101f;font-weight:900;font-size:15px;cursor:pointer}
button:hover{filter:brightness(1.08)}
.error{color:var(--red);font-size:13px;margin:-10px 0 16px}
</style></head><body>
<div class="card">
<h1>Coin Monitor</h1>
<div class="sub">로그인 후 이용할 수 있습니다.</div>
{ERROR_HTML}
<form method="post" action="/login">
<input type="hidden" name="next" value="{NEXT}">
<label for="u">아이디</label>
<input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">비밀번호</label>
<input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">로그인</button>
</form>
</div>
</body></html>
"""


@app.get("/login")
def login_page() -> str:
    next_path = request.args.get("next", "/")
    error_html = '<div class="error">아이디 또는 비밀번호가 올바르지 않습니다.</div>' if request.args.get("error") else ""
    return LOGIN_HTML.replace("{ERROR_HTML}", error_html).replace("{NEXT}", html.escape(next_path, quote=True))


@app.post("/login")
def login_submit() -> Response:
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    next_path = request.form.get("next") or "/"
    if not next_path.startswith("/"):
        next_path = "/"
    # Constant-time comparison to avoid leaking password length/prefix via timing.
    valid = hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD)
    if valid:
        session.clear()
        session["authenticated"] = True
        session.permanent = True
        return redirect(next_path)
    return redirect(url_for("login_page", error="1", next=next_path))


@app.get("/logout")
def logout() -> Response:
    session.clear()
    return redirect(url_for("login_page"))

# Shared column list for SELECTs against `observations`, used by db_summary(),
# api_history(), api_signal_analysis(), api_signals() and export_csv() so the
# column order (and therefore row_to_dict()'s indices) stays in one place.
OBSERVATION_COLUMNS = """id, observed_at, http_status, success, current_price, current_price_raw,
       long_signal, short_signal, long_color, short_color, entry_message,
       long_matched_selector, long_matched_declaration, long_ancestor_classes,
       long_label_classes, long_own_inline_background,
       short_matched_selector, short_matched_declaration, short_ancestor_classes,
       short_label_classes, short_own_inline_background,
       error, parsed_json, binance_json"""


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
bitget_state: Dict[str, Any] = {
    "started": False,
    "configured": False,
    "positions": [],
    "fills": [],
    "orders": [],
    "account": {},
    "total_unrealized_pnl": None,
    "live_open_positions_pnl": None,
    "total_equity": None,
    "win_rate_pct": None,
    "win_count": 0,
    "loss_count": 0,
    "trade_count": 0,
    "realized_pnl_total": None,
    "combined_pnl": None,
    "updated_at": None,
    "last_error": None,
}
telegram_state: Dict[str, Any] = {
    "enabled": False,  # set once at boot after checking token/chat id
    "last_long_signal": None,   # None = unknown yet (e.g. right after boot)
    "last_short_signal": None,
    "long_active_color": None,   # last known "on" color, kept even after it turns off, so the OFF message can reference what it was
    "short_active_color": None,
    "long_activated_at": None,  # ISO timestamp of when it turned ON, cleared when it turns OFF, used to report how long it stayed ON
    "short_activated_at": None,
    "long_near_entry": False,   # edge-trigger flags for the "진입 임박" proximity alert, so it fires once per approach rather than every 30s while price lingers nearby
    "short_near_entry": False,
    "last_sent_at": None,
    "last_error": None,
    "sent_count": 0,
}
# How close current price must get to the 1st-stage (25%) entry price to
# trigger a "진입 임박" (entry imminent) alert, in raw price units (USD for BTC).
ENTRY_PROXIMITY_USD = float(os.getenv("ENTRY_PROXIMITY_USD", "100"))
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
            # Precise, structured entry-basis evidence (v3.6) - split out of the
            # combined visual_evidence text so it's directly queryable/exportable
            # for building a separate dataset from collected observations.
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_matched_selector TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_matched_declaration TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_ancestor_classes TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_label_classes TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_own_inline_background TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_matched_selector TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_matched_declaration TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_ancestor_classes TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_label_classes TEXT",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_own_inline_background TEXT",
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
                long_matched_selector, long_matched_declaration, long_ancestor_classes,
                long_label_classes, long_own_inline_background,
                short_matched_selector, short_matched_declaration, short_ancestor_classes,
                short_label_classes, short_own_inline_background,
                parsed_json, binance_json, raw_text, raw_html, content_sha256, error
            ) VALUES (
                %(observed_at)s, %(target_url)s, %(http_status)s, %(success)s,
                %(current_price)s, %(current_price_raw)s,
                %(long_signal)s, %(short_signal)s, %(long_color)s, %(short_color)s, %(entry_message)s,
                %(long_matched_selector)s, %(long_matched_declaration)s, %(long_ancestor_classes)s,
                %(long_label_classes)s, %(long_own_inline_background)s,
                %(short_matched_selector)s, %(short_matched_declaration)s, %(short_ancestor_classes)s,
                %(short_label_classes)s, %(short_own_inline_background)s,
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
    """The actual E-RANG evidence (detected background color + matched CSS
    rule + ancestor toggle classes + label text), built from the parser's
    structured evidence fields (v3.6) rather than re-parsing the combined
    visual_evidence display string. Escaped for safe inclusion in a Telegram
    HTML-mode message (raw '<'/'&' from CSS selector text like ':not(...)'
    or '&nbsp;' would otherwise be parsed as broken HTML and silently drop
    or fail the whole message)."""
    sig = ((parsed.get("signals") or {}).get(side)) or {}
    if not sig.get("active"):
        return ""
    color = sig.get("detected_color") or "-"
    out = f"감지 배경색 {color}"
    if sig.get("own_inline_background"):
        out += f" · 인라인 style=\"{sig['own_inline_background']}\""
    elif sig.get("matched_css_selector"):
        out += f" · css {sig['matched_css_selector']} {{{sig.get('matched_css_declaration') or ''}}}"
        ancestor_classes = sig.get("ancestor_classes") or []
        if ancestor_classes:
            out += f" · 조상 토글 클래스: {', '.join(ancestor_classes)}"
    if sig.get("matched_text"):
        out += f" · 라벨 '{sig['matched_text']}'"
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


def _to_float_loose(value: Any) -> Optional[float]:
    """Parse numbers that may still have thousands separators (e.g. from
    parsed entry/TP/SL strings like '68,450.5')."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _rr_opinion(risk_reward: Optional[float]) -> Optional[str]:
    if risk_reward is None:
        return None
    if risk_reward < 1:
        return f"손익비 {risk_reward:.2f}:1로 낮은 편 — SL까지 거리가 TP까지 거리보다 멀어, TP 도달 전에 SL에 먼저 닿을 위험이 상대적으로 큽니다"
    if risk_reward <= 3:
        return f"손익비 {risk_reward:.2f}:1로 무난한 편입니다"
    return f"손익비 {risk_reward:.2f}:1로 매우 높은 편 — TP가 SL 대비 멀리 잡혀 있어 도달 확률은 낮아질 수 있습니다"


def _atr_distance_opinion(tp_distance: Optional[float], atr: Optional[float]) -> Optional[str]:
    if tp_distance is None or atr is None or atr <= 0:
        return None
    ratio = tp_distance / atr
    if ratio < 0.5:
        return f"15분봉 ATR 대비 TP 거리가 {ratio:.2f}배로 타이트함 — 단기 변동성만으로도 비교적 빠르게 도달할 수 있는 거리입니다"
    if ratio <= 2:
        return f"15분봉 ATR 대비 TP 거리가 {ratio:.2f}배로 무난한 거리입니다"
    return f"15분봉 ATR 대비 TP 거리가 {ratio:.2f}배로 먼 편 — 추세가 이어져야 도달 가능한 거리입니다"


def _momentum_caution(side: str, rsi: Optional[float], funding_rate: Optional[float]) -> list[str]:
    msgs: list[str] = []
    if rsi is not None:
        if side == "long" and rsi >= 70:
            msgs.append(f"15분봉 RSI {rsi:.1f}로 이미 과매수권 — 진입 직후 눌림(되돌림) 리스크에 유의하세요")
        elif side == "short" and rsi <= 30:
            msgs.append(f"15분봉 RSI {rsi:.1f}로 이미 과매도권 — 진입 직후 반등 리스크에 유의하세요")
    if funding_rate is not None:
        if side == "long" and funding_rate >= 0.0005:
            msgs.append(f"펀딩비 {funding_rate * 100:.4f}%로 롱 포지션이 몰려 과열된 편 — 숏 스퀴즈성 되돌림 리스크에 유의하세요")
        elif side == "short" and funding_rate <= -0.0005:
            msgs.append(f"펀딩비 {funding_rate * 100:.4f}%로 숏 포지션이 몰려 과열된 편 — 숏 커버링 반등 리스크에 유의하세요")
    return msgs


def _ai_tp_opinion(
    side: str,
    entry: Any,
    tp: Any,
    sl: Any,
    atr15: Optional[float],
    rsi15: Optional[float],
    funding_rate: Optional[float],
) -> list[str]:
    """Rule-based (non-LLM) read on whether the 1st-stage TP looks reasonable
    given the SL distance (risk/reward) and current 15m volatility (ATR).
    This is a deterministic heuristic, not a real trading recommendation -
    it never claims to predict what the market will actually do."""
    entry_f, tp_f, sl_f = _to_float_loose(entry), _to_float_loose(tp), _to_float_loose(sl)
    if entry_f is None or tp_f is None:
        return []
    tp_distance = abs(tp_f - entry_f)
    sl_distance = abs(entry_f - sl_f) if sl_f is not None else None
    risk_reward = (tp_distance / sl_distance) if sl_distance else None
    bullets: list[str] = []
    rr_txt = _rr_opinion(risk_reward)
    if rr_txt:
        bullets.append(rr_txt)
    atr_txt = _atr_distance_opinion(tp_distance, atr15)
    if atr_txt:
        bullets.append(atr_txt)
    bullets.extend(_momentum_caution(side, rsi15, funding_rate))
    return bullets


def _format_duration(started_iso: Optional[str], now_dt: datetime) -> Optional[str]:
    if not started_iso:
        return None
    try:
        started = datetime.fromisoformat(started_iso)
    except (TypeError, ValueError):
        return None
    secs = max(0, int((now_dt - started).total_seconds()))
    if secs < 60:
        return f"{secs}초"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}분 {secs}초"
    hours, mins = divmod(mins, 60)
    return f"{hours}시간 {mins}분"


def _build_signal_message(
    side: str,
    turned_on: bool,
    parsed: Dict[str, Any],
    current_price_raw: Optional[str],
    color: Optional[str],
    binance_snapshot: Optional[Dict[str, Any]] = None,
    prev_color: Optional[str] = None,
    duration_text: Optional[str] = None,
) -> str:
    label_kr = "롱(LONG)" if side == "long" else "숏(SHORT)"
    emoji = "🟦" if side == "long" else "🟥"
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")

    if not turned_on:
        # Release messages are intentionally minimal - just the announcement
        # and when it happened. No entry tables, no evidence detail, no
        # indicators: the dashboard already has all of that if it's needed.
        lines = [f"{emoji} <b>E-RANG {label_kr} 신호해제</b>", now_kst + " (KST)"]
        if DASHBOARD_URL:
            lines.append(f'<a href="{html.escape(DASHBOARD_URL, quote=True)}">대시보드 열기</a>')
        return "\n".join(lines)

    rows = _row_map_from_parsed(parsed)
    lines = [
        f"{emoji} <b>E-RANG {label_kr} 신호발생</b>",
        f"현재가: {_fmt_num(current_price_raw)}",
    ]

    # Only the triggered side's 1st-stage (25%) entry - not all 5 stages,
    # and not the other side's table.
    side_label = "LONG" if side == "long" else "SHORT"
    entries = rows.get(side) or []
    tps = rows.get("tpLong" if side == "long" else "tpShort") or []
    sls = rows.get("slLong" if side == "long" else "slShort") or []
    if entries:
        entry1 = entries[0]
        tp1 = tps[0] if tps else None
        sl1 = sls[0] if sls else None
        piece = f"진입1(25%): {_fmt_num(entry1)}"
        if tp1 is not None:
            piece += f" · TP {_fmt_num(tp1)}"
        if sl1 is not None:
            piece += f" · SL {_fmt_num(sl1)}"
        lines.append("")
        lines.append(f"<b>[{side_label} 1차 진입가]</b>")
        lines.append(piece)
    lines.append("")
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
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    with _state_lock:
        prev_long = telegram_state["last_long_signal"]
        prev_short = telegram_state["last_short_signal"]
        telegram_state["last_long_signal"] = long_active
        telegram_state["last_short_signal"] = short_active
        # Capture "what it was" BEFORE updating, so an OFF message can report
        # the color/duration that led up to this exact turn-off.
        long_prev_color = telegram_state.get("long_active_color")
        long_activated_at = telegram_state.get("long_activated_at")
        short_prev_color = telegram_state.get("short_active_color")
        short_activated_at = telegram_state.get("short_activated_at")
        if long_active:
            if long_color:
                telegram_state["long_active_color"] = long_color
            if not telegram_state.get("long_activated_at"):
                telegram_state["long_activated_at"] = now_iso
        else:
            telegram_state["long_activated_at"] = None
        if short_active:
            if short_color:
                telegram_state["short_active_color"] = short_color
            if not telegram_state.get("short_activated_at"):
                telegram_state["short_activated_at"] = now_iso
        else:
            telegram_state["short_activated_at"] = None
    # Only fire on a real transition, and never on the very first cycle after
    # boot (prev is None) - otherwise every restart would re-announce
    # whatever state happened to already be true. Each side is wrapped in its
    # own try/except so a failure building/sending the LONG message (say, an
    # unexpected data shape) can never suppress the SHORT message in the same
    # cycle, and vice versa - every real transition gets its own send attempt
    # no matter what happened to the other side.
    if prev_long is not None and long_active != prev_long:
        if long_active or TELEGRAM_NOTIFY_OFF:
            try:
                duration = None if long_active else _format_duration(long_activated_at, now_dt)
                send_telegram_message(_build_signal_message(
                    "long", long_active, parsed, current_price_raw, long_color, binance_snapshot,
                    prev_color=long_prev_color, duration_text=duration,
                ))
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send LONG {'ON' if long_active else 'OFF'} message: {type(exc).__name__}: {exc}")
    if prev_short is not None and short_active != prev_short:
        if short_active or TELEGRAM_NOTIFY_OFF:
            try:
                duration = None if short_active else _format_duration(short_activated_at, now_dt)
                send_telegram_message(_build_signal_message(
                    "short", short_active, parsed, current_price_raw, short_color, binance_snapshot,
                    prev_color=short_prev_color, duration_text=duration,
                ))
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send SHORT {'ON' if short_active else 'OFF'} message: {type(exc).__name__}: {exc}")


def _maybe_notify_entry_proximity(parsed: Dict[str, Any], current_price_raw: Optional[str]) -> None:
    """Sends a '진입 임박' (entry imminent) alert when the current price gets
    within ENTRY_PROXIMITY_USD of the 1st-stage (25%) LONG or SHORT entry
    price - independent of whether the E-RANG ON/OFF signal itself is
    active, since the entry price levels are published regardless of that.
    Edge-triggered (state kept in telegram_state) so it fires once when
    price first comes within range, not every 30s while it lingers there;
    it re-arms once price moves back out of range."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    current_price = _to_float_loose(current_price_raw)
    if current_price is None:
        return
    rows = _row_map_from_parsed(parsed)
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    for side, entries_key, state_key, label, emoji in (
        ("long", "long", "long_near_entry", "롱(LONG)", "🟦"),
        ("short", "short", "short_near_entry", "숏(SHORT)", "🟥"),
    ):
        entries = rows.get(entries_key) or []
        entry1 = _to_float_loose(entries[0]) if entries else None
        if entry1 is None:
            continue
        is_near = abs(current_price - entry1) <= ENTRY_PROXIMITY_USD
        with _state_lock:
            was_near = telegram_state.get(state_key, False)
            telegram_state[state_key] = is_near
        if is_near and not was_near:
            try:
                msg = (
                    f"{emoji} <b>E-RANG {label} 진입 임박</b>\n"
                    f"현재가: {_fmt_num(current_price_raw)}\n"
                    f"1차 진입가: {_fmt_num(entries[0])} (차이 {abs(current_price - entry1):.1f} 이내)\n"
                    f"{now_kst} (KST)"
                )
                if DASHBOARD_URL:
                    msg += f'\n<a href="{html.escape(DASHBOARD_URL, quote=True)}">대시보드 열기</a>'
                send_telegram_message(msg)
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send {label} 진입임박 message: {type(exc).__name__}: {exc}")


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
        "long_matched_selector": None,
        "long_matched_declaration": None,
        "long_ancestor_classes": None,
        "long_label_classes": None,
        "long_own_inline_background": None,
        "short_matched_selector": None,
        "short_matched_declaration": None,
        "short_ancestor_classes": None,
        "short_label_classes": None,
        "short_own_inline_background": None,
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

        def _join_classes(values: Any) -> Optional[str]:
            vals = [str(v) for v in (values or []) if v]
            return ", ".join(vals) if vals else None

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
                "long_matched_selector": long_sig.get("matched_css_selector"),
                "long_matched_declaration": long_sig.get("matched_css_declaration"),
                "long_ancestor_classes": _join_classes(long_sig.get("ancestor_classes")),
                "long_label_classes": _join_classes(long_sig.get("label_own_classes")),
                "long_own_inline_background": long_sig.get("own_inline_background"),
                "short_matched_selector": short_sig.get("matched_css_selector"),
                "short_matched_declaration": short_sig.get("matched_css_declaration"),
                "short_ancestor_classes": _join_classes(short_sig.get("ancestor_classes")),
                "short_label_classes": _join_classes(short_sig.get("label_own_classes")),
                "short_own_inline_background": short_sig.get("own_inline_background"),
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
        try:
            _maybe_notify_entry_proximity(parsed, current_price_raw)
        except Exception as exc:
            log("TELEGRAM_ERROR", f"entry-proximity notify failed: {type(exc).__name__}: {exc}")
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


def bitget_loop() -> None:
    log("BITGET_BOOT", f"Bitget position loop started interval={BITGET_POLL_INTERVAL}s")
    while True:
        started = time.monotonic()
        try:
            summary = bitget_client.fetch_summary()
            with _state_lock:
                bitget_state["fills"] = summary.get("fills") or []
                bitget_state["orders"] = summary.get("orders") or []
                bitget_state["account"] = summary.get("account") or {}
                bitget_state["positions"] = summary.get("positions") or []
                bitget_state["total_unrealized_pnl"] = summary.get("total_unrealized_pnl")
                bitget_state["live_open_positions_pnl"] = summary.get("live_open_positions_pnl")
                bitget_state["total_equity"] = summary.get("total_equity")
                bitget_state["win_rate_pct"] = summary.get("win_rate_pct")
                bitget_state["win_count"] = summary.get("win_count") or 0
                bitget_state["loss_count"] = summary.get("loss_count") or 0
                bitget_state["trade_count"] = summary.get("trade_count") or 0
                bitget_state["realized_pnl_total"] = summary.get("realized_pnl_total")
                bitget_state["combined_pnl"] = summary.get("combined_pnl")
                bitget_state["updated_at"] = datetime.now(timezone.utc).isoformat()
                bitget_state["last_error"] = summary.get("errors") or None
        except Exception as exc:
            with _state_lock:
                bitget_state["last_error"] = f"{type(exc).__name__}: {exc}"
            log("BITGET_ERROR", bitget_state["last_error"])
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, BITGET_POLL_INTERVAL - elapsed))


def start_bitget_loop_once() -> None:
    with _state_lock:
        bitget_state["configured"] = bool(bitget_client and bitget_client.bitget_configured())
        if bitget_state["started"] or not bitget_state["configured"]:
            if not bitget_state["configured"]:
                log("BITGET_BOOT", "BITGET_API_KEY/SECRET/PASSPHRASE not set, Bitget position card disabled")
            return
        bitget_state["started"] = True
    thread = threading.Thread(target=bitget_loop, name="bitget-position-poll", daemon=True)
    thread.start()


def db_summary() -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE success), COUNT(*) FILTER (WHERE NOT success) FROM observations")
            total, ok, fail = cur.fetchone()
            cur.execute(
                f"""
                SELECT {OBSERVATION_COLUMNS}
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
        # Precise, structured entry-basis evidence (v3.6) - what specifically
        # caused the color to be detected, separate from the combined
        # human-readable text inside parsed.signals.<side>.visual_evidence.
        "long_evidence": {
            "matched_selector": row[11],
            "matched_declaration": row[12],
            "ancestor_classes": row[13],
            "label_classes": row[14],
            "own_inline_background": row[15],
        },
        "short_evidence": {
            "matched_selector": row[16],
            "matched_declaration": row[17],
            "ancestor_classes": row[18],
            "label_classes": row[19],
            "own_inline_background": row[20],
        },
        "error": row[21],
        "parsed": row[22] or {},
        "binance": row[23] or {},
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
        bitget = dict(bitget_state)
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
            "bitget": bitget,
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


@app.get("/api/bitget-live")
def api_bitget_live() -> Response:
    with _state_lock:
        return jsonify(dict(bitget_state))


@app.get("/api/history")
def api_history() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "80"))))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {OBSERVATION_COLUMNS}
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
            f"""
            SELECT {OBSERVATION_COLUMNS}
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
            f"""
            SELECT {OBSERVATION_COLUMNS}
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
        # Precise, structured entry-basis evidence (v3.6) - exactly what
        # caused each side's color to be detected, as flat CSV columns so
        # this is directly usable for building a separate dataset without
        # having to parse it back out of the JSON blob columns below.
        "long_matched_selector", "long_matched_declaration", "long_ancestor_classes",
        "long_label_classes", "long_own_inline_background",
        "short_matched_selector", "short_matched_declaration", "short_ancestor_classes",
        "short_label_classes", "short_own_inline_background",
        "content_sha256", "error", "parsed_json", "binance_json",
    ])
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, observed_at, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_color, short_color, entry_message,
                   long_matched_selector, long_matched_declaration, long_ancestor_classes,
                   long_label_classes, long_own_inline_background,
                   short_matched_selector, short_matched_declaration, short_ancestor_classes,
                   short_label_classes, short_own_inline_background,
                   content_sha256, error, parsed_json, binance_json
            FROM observations ORDER BY id ASC
            """
        )
        for row in cur:
            writer.writerow([
                row[0], row[1].isoformat() if row[1] else None, row[2], row[3], row[4], row[5],
                row[6], row[7], row[8], row[9], row[10],
                row[11], row[12], row[13], row[14], row[15],
                row[16], row[17], row[18], row[19], row[20],
                row[21], row[22],
                json.dumps(row[23] or {}, ensure_ascii=False),
                json.dumps(row[24] or {}, ensure_ascii=False),
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
:root{--bg:#07101f;--panel:#0e1b31;--panel2:#101f38;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--red:#ff5364;--green:#35e29a;--yellow:#ffc83d}*{box-sizing:border-box}html{overflow-x:hidden}body{margin:0;overflow-x:hidden;max-width:100vw;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1540px;margin:auto;padding:24px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.topTitle{min-width:0;flex:1 1 auto}.top h1{margin:0;font-size:30px;overflow-wrap:break-word}.actions{min-width:0}.sub,.muted{color:var(--muted)}.sub{margin-top:5px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#152540;color:#fff;padding:11px 15px;border-radius:11px;text-decoration:none;font-weight:800;cursor:pointer}.live{color:var(--green);font-weight:900}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:18px;box-shadow:0 14px 32px #0004;min-width:0}.s2{grid-column:span 2}.s3{grid-column:span 3}.s4{grid-column:span 4}.s6{grid-column:span 6}.s8{grid-column:span 8}.s12{grid-column:span 12}.label{font-size:13px;color:#a9bfdf;font-weight:800}.big{font-size:29px;font-weight:950;margin-top:7px}.hero{display:flex;align-items:center;gap:22px;min-height:110px}.heroSignal{font-size:42px;font-weight:1000}.short{color:var(--red)}.long{color:var(--blue)}.wait{color:var(--yellow)}.ok{color:var(--green)}h2{font-size:18px;margin:0 0 14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{background:#132947;color:#c7dcfa;font-size:12px}.rowlong.on td:first-child{font-weight:950;color:var(--blue)}.rowshort.on td:first-child{background:#ef3340;color:#fff;font-weight:950}.entryrow{display:grid;grid-template-columns:82px repeat(5,1fr);gap:8px;align-items:stretch;margin-bottom:10px}.sideLabel{display:flex;align-items:center;font-size:20px;font-weight:950}.entry{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:10px;min-width:0}.entry b{font-size:12px;color:#9fb8db;display:block}.entry strong{font-size:17px;display:block;margin-top:5px;white-space:nowrap}.dist{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.dist .entry strong{font-size:20px}.tabs{display:flex;gap:7px;margin:12px 0}.tab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer}.tab.active{background:#168cff;color:white}.metricTop{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metricTop6{display:grid;grid-template-columns:repeat(6,1fr);gap:9px}.metricTop7{display:grid;grid-template-columns:repeat(7,1fr);gap:9px}.metricTop8{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:12px}.metric b{display:block;color:#9fb8db;font-size:12px}.metric strong{display:block;font-size:19px;margin-top:6px}.indtable{min-width:0}.indtable td:nth-child(2){font-weight:800}.statusUp{color:var(--green)}.statusDown{color:var(--red)}.statusNeutral{color:#dbe7f8}.evidence{line-height:1.7}.evidence strong{font-size:18px}.foot{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:13px;gap:12px}.nowrap{white-space:nowrap}.clickrow{cursor:pointer}.clickrow:hover{background:#132947}.analysisGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.analysisBox{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:14px}.analysisBox h3{margin:0 0 10px;font-size:17px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{background:#102746;border:1px solid var(--line);border-radius:999px;padding:6px 9px;font-size:12px}.detailHead{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.hint{font-size:12px;color:var(--muted)}.reasonCell{text-align:left;white-space:normal;min-width:220px;max-width:320px}.profitCell{text-align:left;white-space:normal;min-width:170px}.profitCell div{margin-bottom:4px;font-weight:800}.profitCell div:last-child{margin-bottom:0}.profitCell .muted{font-weight:600;font-size:11px}.reasonMini{font-size:11px;line-height:1.45;margin-bottom:5px;padding:5px 7px;border-radius:7px;background:#0a172b;border:1px solid var(--line)}.reasonMini:last-child{margin-bottom:0}.reasonMini.long{color:#bcdcff;border-color:#2563eb55}.reasonMini.short{color:#ffd0d6;border-color:#ef334055}.reasonMini b{font-weight:900}.reasonList{margin:9px 0 0;padding-left:18px;font-size:12px;color:#c7dcfa;line-height:1.6}.reasonList li{margin-bottom:3px}.reasonSummary{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:6px}.reasonSummary b{display:block;margin-bottom:6px;font-size:13px;color:#dbe7f8}.ptabs{display:flex;gap:7px;margin:12px 0}.ptab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer;text-align:center}.ptab.active{background:#168cff;color:white}.ppanel{display:none}.ppanel.active{display:block}.pside-long{color:var(--blue);background:rgba(56,165,255,.14);padding:2px 8px;border-radius:5px;font-size:12px;font-weight:800}.pside-short{color:var(--red);background:rgba(255,83,100,.14);padding:2px 8px;border-radius:5px;font-size:12px;font-weight:800}.ptable td.pnum{text-align:right}.ptable{min-width:0}.posAvatar{display:inline-block;width:20px;height:20px;border-radius:50%;background:#102746;margin-right:5px;object-fit:cover;vertical-align:middle;border:1px solid var(--line)}.posMini{font-size:12px;line-height:1.7;margin-top:7px}.posMini .prow{display:flex;justify-content:space-between;gap:8px}.posMini .prow b{font-weight:800}.tvChartBox{height:760px}
@media(max-width:1050px){.s2,.s3,.s4,.s6,.s8{grid-column:span 12}.metricTop{grid-template-columns:1fr 1fr}.metricTop6{grid-template-columns:repeat(3,1fr)}.metricTop7{grid-template-columns:repeat(4,1fr)}.metricTop8{grid-template-columns:repeat(4,1fr)}.two{grid-template-columns:1fr}.entryrow{grid-template-columns:70px repeat(5,130px);overflow-x:auto}.dist{grid-template-columns:repeat(5,140px);overflow-x:auto}.tvChartBox{height:520px}}@media(max-width:600px){.wrap{padding:12px}.card{padding:14px}.top{flex-direction:column}.metricTop{grid-template-columns:1fr 1fr}.metricTop6{grid-template-columns:1fr 1fr}.metricTop7{grid-template-columns:1fr 1fr}.metricTop8{grid-template-columns:1fr 1fr}.heroSignal{font-size:34px}.tvChartBox{height:400px}.actions{width:100%}.actions .btn{flex:1 1 auto;text-align:center}.metric b{font-size:11px}.metric strong{font-size:16px}}@media(max-width:380px){.top h1{font-size:24px}.metricTop6{grid-template-columns:1fr}.metricTop7{grid-template-columns:1fr}.metricTop8{grid-template-columns:1fr}.ptabs{flex-wrap:wrap}.ptab{flex:1 1 45%}}
</style></head><body><div class="wrap">
<div class="top"><div class="topTitle"><h1>Coin Monitor</h1><div class="sub">e-rang coin.php 1분 수집 + LONG/SHORT 색상 신호 + Binance 보조 데이터</div></div><div class="actions"><a class="btn" href="/export.csv">CSV 다운로드</a><button id="collect" class="btn">강제 수집</button><button id="telegramTest" class="btn">텔레그램 현재상태 발송</button><a class="btn" href="/logout">로그아웃</a><span id="live" class="live">● 정상 수집 중</span><span id="lastSync" class="muted"></span><span id="lastTop" class="muted"></span></div></div>
<div class="grid">
<div class="card s4 hero"><div><div class="label">현재 E-RANG 판정</div><div id="heroSignal" class="heroSignal wait">WAIT</div></div><div><div id="signalBits" class="big" style="font-size:15px">LONG OFF / SHORT OFF</div><div class="muted">E-RANG 화면의 색상 신호를 기준으로 판정합니다.</div></div></div>
<div class="card s2"><div class="label">BTCUSDT 현재가</div><div id="price" class="big">-</div><div id="priceDelta" class="muted">Binance 실시간</div></div>
<div class="card s2"><div class="label">수집 상태</div><div id="collectState" class="big ok">정상</div><div id="counts" class="muted">-</div></div>
<div class="card s2"><div class="label">DB / 서버</div><div id="db" class="big ok" style="font-size:21px">-</div><div id="server" class="muted">-</div></div>
<div class="card s2"><div class="label"><img class="posAvatar" src="data:image/png;base64,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" alt="뿌꾸"> 현재 포지션</div><div id="posMini" class="posMini"><span class="muted">-</span></div></div>
<div class="card s12"><h2>실시간 BTC 선물 차트 <span class="muted">(Binance BTCUSDT Perpetual · TradingView 위젯)</span></h2><div class="tradingview-widget-container tvChartBox" style="width:100%"><div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div><script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
{
"autosize": true,
"symbol": "BINANCE:BTCUSDT.P",
"interval": "15",
"timezone": "Asia/Seoul",
"theme": "dark",
"style": "1",
"locale": "kr",
"backgroundColor": "rgba(14, 27, 49, 1)",
"gridColor": "rgba(36, 59, 95, 0.35)",
"hide_top_toolbar": false,
"hide_legend": false,
"save_image": false,
"calendar": false,
"support_host": "https://www.tradingview.com"
}
</script></div></div>
<div class="card s12"><div class="detailHead"><h2>내 Bitget 계좌 <span class="muted">(통합계좌 · 실계좌 · 읽기 전용)</span></h2><span class="hint" id="bgHint"></span></div><div class="metricTop metricTop8"><div class="metric"><b>지금까지 수익PNL (계정 전체)</b><strong id="bgLifetimePnl">-</strong></div><div class="metric"><b>총 PNL (실시간 · 포지션 없으면 사라짐)</b><strong id="bgLivePnl">-</strong></div><div class="metric"><b>총자산 (Account Equity)</b><strong id="bgAccountEquity">-</strong></div><div class="metric"><b>USDT 잔고</b><strong id="bgEquity">-</strong></div><div class="metric"><b>미실현 PNL</b><strong id="bgPnl">-</strong></div><div class="metric"><b>유효자산 (Eff. Equity)</b><strong id="bgEffEquity">-</strong></div><div class="metric"><b>승률</b><strong id="bgWinRate">-</strong></div><div class="metric"><b>PNL (통합)</b><strong id="bgCombinedPnl">-</strong></div></div><div class="ptabs"><button class="ptab active" data-ptab="positions">현재 포지션</button><button class="ptab" data-ptab="fills">체결 내역</button><button class="ptab" data-ptab="orders">주문 내역</button></div><div id="ppanel-positions" class="ppanel active"><div class="muted">-</div></div><div id="ppanel-fills" class="ppanel"><div class="muted">-</div></div><div id="ppanel-orders" class="ppanel"><div class="muted">-</div></div><div class="foot"><span>ⓘ 가격/손익은 Bitget API 응답을 그대로 표시합니다. 승률/PNL(통합)은 청산(close) 체결의 실현손익 기준이며, 매매 판단 참고용입니다.</span><span id="bgUpdate"></span></div></div>
<div class="card s6"><h2>E-RANG 진입가 <span class="muted">(현재 화면 기준)</span></h2><div class="tablewrap"><table><thead><tr><th>구분</th><th>진입 1<br>(25%)</th><th>진입 2<br>(40%)</th><th>진입 3<br>(60%)</th><th>진입 4<br>(100%)</th><th>진입 5<br>(예비)</th></tr></thead><tbody id="erangRows"></tbody></table></div></div>
<div class="card s6"><h2>Binance 보조 지표 (BTCUSDT)</h2><div class="metricTop"><div class="metric"><b>현재가 (Last Price)</b><strong id="bLast">-</strong></div><div class="metric"><b>펀딩비 (Funding Rate)</b><strong id="funding">-</strong></div><div class="metric"><b>미결제약정 (Open Interest)</b><strong id="oi">-</strong></div><div class="metric"><b>24h 거래량</b><strong id="vol24">-</strong></div></div><div class="tabs"><button class="tab" data-tf="1m">1분</button><button class="tab" data-tf="5m">5분</button><button class="tab active" data-tf="15m">15분</button><button class="tab" data-tf="1h">1시간</button></div><div class="tablewrap"><table class="indtable"><thead><tr><th>지표</th><th>현재값</th><th>상태</th></tr></thead><tbody id="indicatorRows"></tbody></table></div><div class="foot"><span>ⓘ 최근 220개 캔들 데이터 기반 계산</span><span id="bUpdate"></span></div></div>
<div class="card s6"><h2>현재가와 주요 진입가 거리 <span class="muted">(Long 기준)</span></h2><div id="distanceLong" class="dist"></div><h2 style="margin-top:16px">현재가와 주요 진입가 거리 <span class="muted">(Short 기준)</span></h2><div id="distanceShort" class="dist"></div></div>
<div class="card s6"><h2>신호 판정 근거</h2><div id="evidence" class="evidence muted">-</div></div>
<div class="card s12"><div class="detailHead"><h2>LONG / SHORT ON 공통 보조지표 패턴</h2><span class="hint">※ E-RANG ON 당시 Binance 지표의 상관 패턴이며 ON의 원인으로 확정한 값은 아닙니다.</span></div><div id="analysisSummary" class="analysisGrid"></div></div>
<div class="card s12"><div class="detailHead"><h2>선택한 수집 시점 보조지표</h2><span class="hint">아래 최근 수집 데이터 행을 클릭하면 당시 1m·5m·15m·1h 상태를 확인합니다.</span></div><div id="eventDetail" class="muted">수집 데이터 행을 선택하세요.</div></div>
<div class="card s12"><h2 id="historyToggle" style="cursor:pointer;user-select:none" title="클릭해서 펼치기/접기">최근 수집 데이터 <span class="muted" style="font-size:12px">(행 클릭 → 당시 보조지표)</span> <span id="historyChevron" class="muted">▶ 펼치기</span></h2><div id="historyBody" style="display:none"><div class="tablewrap" style="max-height:360px;overflow:auto"><table><thead><tr><th>ID</th><th>시간</th><th>BTC</th><th>LONG</th><th>SHORT</th><th>예상 수익<br><span class="hint">($5,000·5x·1차 TP)</span></th><th>판정 근거</th><th>15m RSI</th><th>15m MACD Hist</th><th>15m EMA20 관계</th><th>HTTP</th></tr></thead><tbody id="history"></tbody></table></div></div></div>
</div></div><script>
const $=id=>document.getElementById(id); let latest={},activeTF='15m',liveBinance={};
const n=v=>{let x=Number(v);return Number.isFinite(x)?x.toLocaleString('en-US',{maximumFractionDigits:4}):'-'}; const kst=v=>v?new Date(v).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'-';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function sigReason(r,side){
  let s=(r.parsed&&r.parsed.signals&&r.parsed.signals[side])||{};
  if(!s.active)return '';
  let color=s.detected_color||(side==='long'?r.long_color:r.short_color)||'-';
  let ev=(side==='long'?r.long_evidence:r.short_evidence)||{};
  let out=`감지 배경색 ${color}`;
  if(ev.own_inline_background){
    out+=` · 인라인 style="${ev.own_inline_background}"`;
  } else if(ev.matched_selector){
    out+=` · css ${ev.matched_selector} {${ev.matched_declaration||''}}`;
    if(ev.ancestor_classes)out+=` · 조상 토글 클래스: ${ev.ancestor_classes}`;
  } else {
    // Fallback for observations collected before v3.6 structured evidence existed.
    let parts=(s.visual_evidence||'').split(' | ').filter(Boolean);
    let cssRule=parts.find(p=>p.startsWith('css '));
    if(cssRule)out+=` · ${cssRule}`;
  }
  if(s.matched_text)out+=` · 라벨 '${s.matched_text}'`;
  return out;
}
function historyReasonCell(r){
  let blocks=[];
  if(r.long_signal)blocks.push(`<div class="reasonMini long"><b>LONG</b> ${esc(sigReason(r,'long'))}</div>`);
  if(r.short_signal)blocks.push(`<div class="reasonMini short"><b>SHORT</b> ${esc(sigReason(r,'short'))}</div>`);
  return blocks.length?blocks.join(''):'<span class="muted">-</span>';
}
function rowMap(parsed){let rows=parsed.table_rows||[], out={}; for(const r of rows){let t=(r.text||'').trim(), nums=r.numbers_raw||[]; if(/^Long\b/i.test(t))out.long=nums.slice(0,5); else if(/^Short\b/i.test(t))out.short=nums.slice(0,5); else if(/^TP\b/i.test(t)){if(!out.tpLong)out.tpLong=nums.slice(0,5);else out.tpShort=nums.slice(0,5)} else if(/^SL\b/i.test(t)){if(!out.slLong)out.slLong=nums.slice(0,5);else out.slShort=nums.slice(0,5)}} let sides=parsed.sides||{}; out.long=out.long||(sides.long?.entry_prices_guess||[]);out.short=out.short||(sides.short?.entry_prices_guess||[]);return out}
const PROFIT_MARGIN=5000, PROFIT_LEVERAGE=5;
function calcExpectedProfit(entry,tp){
  let e=Number(entry), t=Number(tp);
  if(!Number.isFinite(e)||!Number.isFinite(t)||e<=0)return null;
  let notional=PROFIT_MARGIN*PROFIT_LEVERAGE, qty=notional/e, profit=qty*Math.abs(t-e);
  return {profit, pct:profit/PROFIT_MARGIN*100};
}
function expectedProfitCell(r){
  let rm=rowMap(r.parsed||{}), parts=[];
  if(r.long_signal){
    let c=calcExpectedProfit(rm.long?.[0], rm.tpLong?.[0]);
    if(c)parts.push(`<div class="long">LONG +$${c.profit.toLocaleString('en-US',{maximumFractionDigits:0})} <span class="muted">(마진 대비 +${c.pct.toFixed(1)}%)</span></div>`);
  }
  if(r.short_signal){
    let c=calcExpectedProfit(rm.short?.[0], rm.tpShort?.[0]);
    if(c)parts.push(`<div class="short">SHORT +$${c.profit.toLocaleString('en-US',{maximumFractionDigits:0})} <span class="muted">(마진 대비 +${c.pct.toFixed(1)}%)</span></div>`);
  }
  return parts.length?parts.join(''):'<span class="muted">-</span>';
}
function cells(a){return [0,1,2,3,4].map(i=>`<td>${n(a?.[i])}</td>`).join('')}
function renderErang(parsed,priceOverride){let r=rowMap(parsed); let longOn=!!latest.long_signal, shortOn=!!latest.short_signal; $('erangRows').innerHTML=`<tr class="rowlong${longOn?' on':''}"><td>Long</td>${cells(r.long)}</tr><tr><td>TP (Long)</td>${cells(r.tpLong)}</tr><tr><td>SL (Long)</td>${cells(r.slLong)}</tr><tr class="rowshort${shortOn?' on':''}"><td>Short</td>${cells(r.short)}</tr><tr><td>TP (Short)</td>${cells(r.tpShort)}</tr><tr><td>SL (Short)</td>${cells(r.slShort)}</tr>`; let p=Number.isFinite(priceOverride)&&priceOverride>0?priceOverride:Number(latest.current_price||latest.current_price_raw); let renderDist=(elId,arr)=>{$(elId).innerHTML=[0,1,2,3,4].map(i=>{let x=Number(arr?.[i]),d=x-p,pct=p?d/p*100:0;return `<div class="entry"><b>진입 ${i+1}</b><strong>${Number.isFinite(d)?(d>=0?'+':'')+n(d):'-'}</strong><span class="${d>=0?'short':'long'}">${Number.isFinite(pct)?(pct>=0?'+':'')+pct.toFixed(2)+'%':'-'}</span></div>`}).join('')}; renderDist('distanceLong',r.long); renderDist('distanceShort',r.short)}
function statusFor(name,val,ind){if(val==null)return '-';if(name==='RSI 14')return val>=70?'과매수':val<=30?'과매도':'중립';if(name.startsWith('EMA')){let c=Number(ind.close);return c>val?'▲ 현재가 상회':'▼ 현재가 하회'}if(name==='MACD Histogram')return val>0?'▲ 양수 (상승 모멘텀)':val<0?'▼ 음수 (하락 모멘텀)':'중립';if(name==='MACD Line')return val>Number(ind.macd?.signal)?'▲ Signal 상회':'▼ Signal 하회';return '-'}
function clsStatus(s){return s.includes('▲')?'statusUp':s.includes('▼')?'statusDown':'statusNeutral'}
function renderIndicators(){let b=liveBinance&&Object.keys(liveBinance).length?liveBinance:(latest.binance||{}), ind=b.indicators?.[activeTF]||{}, mac=ind.macd||{}, bol=ind.bollinger20||{};let rows=[['현재가 (Close)',ind.close],['고가 (High)',ind.high],['저가 (Low)',ind.low],['거래량 (Volume)',ind.volume],['EMA 20',ind.ema20],['EMA 50',ind.ema50],['EMA 200',ind.ema200],['RSI 14',ind.rsi14],['MACD Line',mac.macd],['MACD Signal',mac.signal],['MACD Histogram',mac.histogram],['Bollinger 상단',bol.upper],['Bollinger 중단',bol.middle],['Bollinger 하단',bol.lower],['ATR 14',ind.atr14]];$('indicatorRows').innerHTML=rows.map(([name,val])=>{let st=statusFor(name,Number(val),ind);return `<tr><td>${name}</td><td>${n(val)}</td><td class="${clsStatus(st)}">${st}</td></tr>`}).join('');}
function renderEvidence(){let p=latest.parsed||{},s=p.signals||{},L=s.long||{},S=s.short||{};let active=latest.short_signal?'SHORT':latest.long_signal?'LONG':'WAIT';$('evidence').innerHTML=`<strong class="${active==='SHORT'?'short':active==='LONG'?'long':'wait'}">● ${active==='WAIT'?'활성 신호 없음':active+' 활성화 감지'}</strong><br>• Long 감지색: ${L.detected_color||'-'}<br>• Short 감지색: ${S.detected_color||'-'}<br>• 판정 기준: E-RANG Long/Short 라벨 셀의 활성 스타일/클래스`}
const BG_PRIORITY_KEYS=['symbol','posSide','avgPrice','markPrice','leverage','holdSize','positionValue','unrealisedPnl','liqPrice','positionBalance','marginMode','side','tradeSide','qty','avgEntryPrice','execPrice','execQty','execValue','execPnl','feeDetail','createdTime','orderId'];
const BG_PNL_KEYS=['execpnl','unrealisedpnl','unrealizedpl','pnl','profit'];
const BG_HIDE_KEYS=['userId','marginCoin','posMode'];
function bgLooksNumeric(v){return v!==''&&v!==null&&v!==undefined&&!Number.isNaN(Number(v))}
function renderBitgetTable(container,rows,emptyMsg){
  if(!rows||rows.length===0){container.innerHTML=`<div class="muted" style="padding:20px 4px">${emptyMsg}</div>`;return}
  let allKeys=Object.keys(rows[0]).filter(k=>!BG_HIDE_KEYS.includes(k));
  let ordered=[...BG_PRIORITY_KEYS.filter(k=>allKeys.includes(k)),...allKeys.filter(k=>!BG_PRIORITY_KEYS.includes(k))];
  let out='<div class="tablewrap"><table class="ptable"><thead><tr>'+ordered.map(k=>`<th>${esc(k)}</th>`).join('')+'</tr></thead><tbody>';
  rows.forEach(row=>{
    out+='<tr>';
    ordered.forEach(k=>{
      let v=row[k];
      let isPnl=BG_PNL_KEYS.some(p=>k.toLowerCase().includes(p));
      if(k==='side'||k==='tradeSide'||k==='posSide'){
        let isLong=String(v).toLowerCase().includes('buy')||String(v).toLowerCase()==='open'||String(v).toLowerCase().includes('long');
        out+=`<td><span class="${isLong?'pside-long':'pside-short'}">${esc(v)}</span></td>`;
      } else if(k==='createdTime'){
        out+=`<td>${esc(kst(Number(v)))}</td>`;
      } else if(k==='orderId'){
        out+=`<td>${esc(v??'')}</td>`;
      } else if(bgLooksNumeric(v)){
        let cls=isPnl?(Number(v)>=0?'statusUp':'statusDown'):'';
        out+=`<td class="pnum ${cls}">${n(v)}</td>`;
      } else {
        out+=`<td>${esc(v??'')}</td>`;
      }
    });
    out+='</tr>';
  });
  out+='</tbody></table></div>';
  container.innerHTML=out;
}
function renderPositionMini(bg){
  let el=$('posMini');
  if(!bg||!bg.configured){el.innerHTML='<span class="muted">키 미설정</span>';return}
  let positions=bg.positions||[];
  let rows='';
  if(!positions.length){
    rows='<span class="muted">보유 포지션 없음</span>';
  } else {
    const MAX_ROWS=3;
    rows=positions.slice(0,MAX_ROWS).map(p=>{
      let isLong=String(p.posSide).toLowerCase()==='long';
      let pnl=Number(p.unrealisedPnl);
      let pnlTxt=Number.isFinite(pnl)?(pnl>=0?'+':'')+n(pnl):'-';
      let pnlCls=Number.isFinite(pnl)?(pnl>=0?'statusUp':'statusDown'):'';
      return `<div class="prow"><span class="${isLong?'long':'short'}">${esc(p.symbol||'-')} ${isLong?'LONG':'SHORT'}</span><b class="${pnlCls}">${pnlTxt}</b></div>`;
    }).join('');
    if(positions.length>MAX_ROWS)rows+=`<div class="muted">+${positions.length-MAX_ROWS}개 더</div>`;
  }
  let winRate=bg.win_rate_pct,combined=Number(bg.combined_pnl),equity=Number(bg.total_equity);
  let combinedCls=Number.isFinite(combined)?(combined>=0?'statusUp':'statusDown'):'';
  let livePnl=bg.live_open_positions_pnl,livePnlNum=Number(livePnl);
  let liveOk=livePnl!=null&&Number.isFinite(livePnlNum);
  let liveCls=liveOk?(livePnlNum>=0?'statusUp':'statusDown'):'';
  let summary=`<div class="muted" style="margin-top:6px;font-size:11px;line-height:1.6">총 PNL(실시간) <span class="${liveCls}">${liveOk?(livePnlNum>=0?'+':'')+n(livePnlNum):'-'}</span><br>승률 ${winRate!=null?winRate+'%':'-'} · PNL(통합) <span class="${combinedCls}">${Number.isFinite(combined)?(combined>=0?'+':'')+n(combined):'-'}</span><br>총 USDT ${Number.isFinite(equity)?n(equity):'-'}</div>`;
  el.innerHTML=rows+summary;
}
function renderBitget(bg){
  if(!bg||!bg.configured){
    $('bgHint').textContent='Bitget API 키 미설정';
    $('bgAccountEquity').textContent='-';$('bgEquity').textContent='-';$('bgPnl').textContent='-';$('bgEffEquity').textContent='-';
    $('bgWinRate').textContent='-';$('bgCombinedPnl').textContent='-';$('bgLivePnl').textContent='-';$('bgLifetimePnl').textContent='-';
    ['positions','fills','orders'].forEach(k=>$('ppanel-'+k).innerHTML='<div class="muted" style="padding:20px 4px">BITGET_API_KEY / BITGET_API_SECRET / BITGET_API_PASSPHRASE 환경변수를 설정하면 표시됩니다.</div>');
    renderPositionMini(bg);
    return;
  }
  let fills=bg.fills||[],orders=bg.orders||[],acct=bg.account||{},positions=bg.positions||[];
  $('bgAccountEquity').textContent=Number.isFinite(Number(acct.account_equity))?n(acct.account_equity):'-';
  $('bgEffEquity').textContent=Number.isFinite(Number(acct.eff_equity))?n(acct.eff_equity):'-';
  let pnl=Number(bg.total_unrealized_pnl);
  let pnlEl=$('bgPnl');
  pnlEl.textContent=Number.isFinite(pnl)?n(pnl):'-';
  pnlEl.className='num '+(Number.isFinite(pnl)?(pnl>=0?'statusUp':'statusDown'):'');
  let livePnl=bg.live_open_positions_pnl,livePnlEl=$('bgLivePnl');
  let livePnlNum=Number(livePnl);
  // Intentionally shows '-' (not '0') whenever there are no open positions,
  // so this metric visibly disappears the moment every position closes,
  // rather than lingering on a stale number.
  livePnlEl.textContent=(livePnl!=null&&Number.isFinite(livePnlNum))?(livePnlNum>=0?'+':'')+n(livePnlNum):'-';
  livePnlEl.className='num '+((livePnl!=null&&Number.isFinite(livePnlNum))?(livePnlNum>=0?'statusUp':'statusDown'):'');
  let winRateEl=$('bgWinRate');
  winRateEl.textContent=bg.win_rate_pct!=null?bg.win_rate_pct+'% ('+(bg.win_count||0)+'승 '+(bg.loss_count||0)+'패)':'-';
  let combined=Number(bg.combined_pnl),combinedEl=$('bgCombinedPnl');
  combinedEl.textContent=Number.isFinite(combined)?(combined>=0?'+':'')+n(combined):'-';
  combinedEl.className='num '+(Number.isFinite(combined)?(combined>=0?'statusUp':'statusDown'):'');
  let lifetimeEl=$('bgLifetimePnl');
  lifetimeEl.textContent=Number.isFinite(combined)?(combined>=0?'+':'')+n(combined):'-';
  lifetimeEl.className='num '+(Number.isFinite(combined)?(combined>=0?'statusUp':'statusDown'):'');
  let equity=Number(bg.total_equity);
  $('bgEquity').textContent=Number.isFinite(equity)?n(equity):'-';
  renderBitgetTable($('ppanel-positions'),positions,'보유 중인 포지션이 없습니다.');
  renderBitgetTable($('ppanel-fills'),fills,'체결 내역이 없습니다.');
  renderBitgetTable($('ppanel-orders'),orders,'주문 내역이 없습니다.');
  $('bgHint').textContent=bg.last_error?('오류: '+JSON.stringify(bg.last_error)):'';
  $('bgUpdate').textContent='업데이트: '+kst(bg.updated_at);
  renderPositionMini(bg);
}
document.querySelectorAll('.ptab').forEach(x=>x.onclick=()=>{document.querySelectorAll('.ptab').forEach(y=>y.classList.remove('active'));document.querySelectorAll('.ppanel').forEach(y=>y.classList.remove('active'));x.classList.add('active');$('ppanel-'+x.dataset.ptab).classList.add('active')});
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
async function refresh(){try{let [sr,hr,ar]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/history?limit=80',{cache:'no-store'}),fetch('/api/signal-analysis?limit=200',{cache:'no-store'})]),s=await sr.json(),h=await hr.json(),a=await ar.json(),db=s.db||{};renderAnalysis(a);latest=db.latest||{};let sig=latest.short_signal&&!latest.long_signal?'SHORT':latest.long_signal&&!latest.short_signal?'LONG':latest.short_signal&&latest.long_signal?'BOTH':'WAIT';$('heroSignal').textContent=sig;$('heroSignal').className='heroSignal '+(sig==='SHORT'?'short':sig==='LONG'?'long':'wait');$('signalBits').textContent=`LONG ${latest.long_signal?'ON':'OFF'} / SHORT ${latest.short_signal?'ON':'OFF'}`;let lp=s.live_price||{},livePriceNum=Number(lp.price);$('price').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?n(livePriceNum):n(latest.current_price||latest.current_price_raw);$('priceDelta').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?('Binance 실시간 · '+kst(lp.updated_at)):'E-RANG (Binance 실시간가 대기중)';$('collectState').textContent=latest.success?'정상':'오류';$('counts').textContent=`성공 ${n(db.successful||0)} / 실패 ${n(db.failed||0)}`;$('db').textContent=db.database_ok?'Postgres OK':'Postgres 오류';$('server').textContent=`collector ${(s.collector||{}).running?'running':'idle'}`;$('lastTop').textContent='마지막 수집: '+kst(latest.observed_at);renderErang(latest.parsed||{},livePriceNum);let lb=s.live_binance?.snapshot||{};liveBinance=Object.keys(lb).length?lb:(latest.binance||{});let b=liveBinance;$('bLast').textContent=n(b.ticker_24h?.lastPrice);$('funding').textContent=b.premium_index?.lastFundingRate??'-';$('oi').textContent=n(b.open_interest?.openInterest);$('vol24').textContent=n(b.ticker_24h?.volume);$('bUpdate').textContent='업데이트: '+kst(s.live_binance?.updated_at||latest.observed_at);renderIndicators();renderEvidence();renderBitget(s.bitget);$('history').innerHTML=(h.items||[]).map((r,idx)=>{let i=eventTF(r,'15m'),mh=i.macd?.histogram,rel=Number(i.close)>Number(i.ema20)?'상회':Number(i.close)<Number(i.ema20)?'하회':'-';return `<tr class="clickrow" data-idx="${idx}"><td>${r.id}</td><td>${kst(r.observed_at)}</td><td>${n(r.current_price||r.current_price_raw)}</td><td class="${r.long_signal?'long':''}">${r.long_signal?'ON':'OFF'}</td><td class="${r.short_signal?'short':''}">${r.short_signal?'ON':'OFF'}</td><td class="profitCell">${expectedProfitCell(r)}</td><td class="reasonCell">${historyReasonCell(r)}</td><td>${n(i.rsi14)}</td><td class="${Number(mh)>=0?'statusUp':'statusDown'}">${n(mh)}</td><td>${rel}</td><td>${r.http_status||'-'}</td></tr>`}).join('');document.querySelectorAll('.clickrow').forEach(tr=>tr.onclick=()=>renderEventDetail((h.items||[])[Number(tr.dataset.idx)]));let firstOn=(h.items||[]).find(r=>r.long_signal||r.short_signal);if(firstOn)renderEventDetail(firstOn);$('live').textContent='● 실시간 동기화 중';$('live').className='live';lastSyncAt=Date.now();$('lastSync').textContent='방금 갱신'}catch(e){$('live').textContent='● UI 오류 (재시도 중)';$('live').className='short'}}
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{document.querySelectorAll('.tab').forEach(y=>y.classList.remove('active'));x.classList.add('active');activeTF=x.dataset.tf;renderIndicators()});$('collect').onclick=async()=>{await fetch('/api/collect-now',{method:'POST',cache:'no-store'});refresh()};
$('telegramTest').onclick=async()=>{let b=$('telegramTest'),orig=b.textContent;b.disabled=true;b.textContent='발송 중...';try{let r=await fetch('/api/telegram-test',{method:'POST',cache:'no-store'});let j=await r.json();alert(j.ok?('✅ 텔레그램 발송 완료'+(j.previewed_side?` (미리보기: ${j.previewed_side.toUpperCase()})`:' (신호 없음, 안내 메시지)')):('❌ 발송 실패: '+(j.error||'알 수 없는 오류')))}catch(e){alert('❌ 요청 실패: '+e)}finally{b.disabled=false;b.textContent=orig}};
let historyOpen=false;
$('historyToggle').onclick=()=>{historyOpen=!historyOpen;$('historyBody').style.display=historyOpen?'block':'none';$('historyChevron').textContent=historyOpen?'▼ 접기':'▶ 펼치기'};
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
    start_bitget_loop_once()
    port = int(os.getenv("PORT", "8080"))
    log("WEB", f"listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
