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
import sqlite3
import threading
import time
import webbrowser

try:
    import webview  # native app-window wrapper for desktop installs (pywebview)
except Exception:  # pragma: no cover
    webview = None
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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

try:
    import bingx_client
except Exception:  # pragma: no cover
    bingx_client = None

try:
    import liquidation_stream
except Exception:  # pragma: no cover
    liquidation_stream = None

# ---------------------------------------------------------------------------
# Desktop-friendly data directory: a per-user folder that's always writable,
# regardless of where the packaged .exe happens to sit (Program Files etc.
# may not be writable). Override with COIN_MONITOR_DATA_DIR if needed.
# ---------------------------------------------------------------------------
def _default_data_dir() -> str:
    override = os.getenv("COIN_MONITOR_DATA_DIR", "").strip()
    if override:
        return override
    appdata = os.getenv("APPDATA")  # Windows
    if appdata:
        return os.path.join(appdata, "CoinMonitor")
    return os.path.join(os.path.expanduser("~"), ".coin-monitor")


DATA_DIR = _default_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "coin_monitor.db")

# Bump this on every release that gets tagged/published via GitHub Actions -
# the update checker compares it against the latest GitHub Release tag.
APP_VERSION = "3.40.0"

# Defaults, used until init_db()+apply_settings() load anything saved from
# the in-app Settings page (or DATABASE-less first run). Env vars still work
# as a fallback for anyone who prefers them (e.g. an advanced Railway setup),
# but the Settings page is the primary way to configure a desktop install.
TARGET_URL = os.getenv("TARGET_URL", "https://e-rang.kr/api/coin.php")
# E-RANG (ADrang) started requiring a logged-in session to see coin.php's
# data - these credentials are used to log in once and keep a persistent
# session (cookie jar), rather than scraping anonymously.
ERANG_USERNAME = os.getenv("ERANG_USERNAME", "").strip()
ERANG_PASSWORD = os.getenv("ERANG_PASSWORD", "")
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
# Telegram/exchange/auto-trade settings are now per-user (see
# USER_SETTINGS_SCHEMA below) rather than single global values - each
# logged-in user has their own copy in the user_settings table.
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip().strip("/")
KST = ZoneInfo("Asia/Seoul")

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Paths reachable without logging in. /healthz stays public for platform
# health checks (e.g. a Railway healthcheckPath) that can't submit a login.
PUBLIC_PATHS = {"/login", "/register", "/healthz"}


@app.before_request
def _require_login() -> Any:
    if request.path in PUBLIC_PATHS:
        return None
    if session.get("user_id"):
        return None
    if request.path.startswith("/api/") or request.path == "/export.csv":
        return jsonify({"ok": False, "error": "login required"}), 401
    return redirect(url_for("login_page", next=request.path))


LOGIN_HTML = r"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>뿌꾸의 코인세상 · 로그인</title>
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
.altlink{text-align:center;margin-top:16px;font-size:13px;color:var(--muted)}
.altlink a{color:var(--blue);text-decoration:none;font-weight:700}
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{--bg:#030711;--panel:#0a1424;--line:#17415f;--text:#e4f6ff;--muted:#5e84a3;--blue:#22e3ff;--red:#ff2f6e}
body{font-family:'Rajdhani',Inter,system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;overflow:hidden;
  background:radial-gradient(ellipse 700px 500px at 18% -8%,rgba(34,227,255,.10),transparent 60%),
    radial-gradient(ellipse 600px 500px at 88% 108%,rgba(255,47,110,.08),transparent 60%),var(--bg)}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(34,227,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(34,227,255,.05) 1px,transparent 1px);
  background-size:40px 40px;mask-image:radial-gradient(circle at 50% 15%,#000 0%,transparent 65%)}
.card{position:relative;z-index:1;border-color:var(--line);box-shadow:0 0 0 1px rgba(34,227,255,.08),0 24px 60px #000a,0 0 26px rgba(34,227,255,.10)}
.card::before,.card::after{content:'';position:absolute;width:16px;height:16px;border:2px solid var(--blue);opacity:.8;pointer-events:none}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none;border-radius:17px 0 0 0}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none;border-radius:0 0 17px 0}
h1{font-weight:700;letter-spacing:.4px;text-shadow:0 0 20px rgba(34,227,255,.4)}
label{font-family:'Rajdhani',sans-serif;letter-spacing:.3px}
input{font-family:'JetBrains Mono',monospace;font-size:13.5px;transition:border-color .15s,box-shadow .15s}
input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 0 18px rgba(34,227,255,.3)}
button{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.6px;box-shadow:0 0 20px rgba(34,227,255,.4);transition:box-shadow .15s,transform .1s}
button:hover{box-shadow:0 0 28px rgba(34,227,255,.6);transform:translateY(-1px)}
</style></head><body>
<div class="card">
<h1>🪙 뿌꾸의 코인세상</h1>
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
<div class="altlink">계정이 없으신가요? <a href="/register">회원가입</a></div>
</div>
</body></html>
"""

REGISTER_HTML = r"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>뿌꾸의 코인세상 · 회원가입</title>
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
.altlink{text-align:center;margin-top:16px;font-size:13px;color:var(--muted)}
.altlink a{color:var(--blue);text-decoration:none;font-weight:700}
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{--bg:#030711;--panel:#0a1424;--line:#17415f;--text:#e4f6ff;--muted:#5e84a3;--blue:#22e3ff;--red:#ff2f6e}
body{font-family:'Rajdhani',Inter,system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;overflow:hidden;
  background:radial-gradient(ellipse 700px 500px at 18% -8%,rgba(34,227,255,.10),transparent 60%),
    radial-gradient(ellipse 600px 500px at 88% 108%,rgba(255,47,110,.08),transparent 60%),var(--bg)}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(34,227,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(34,227,255,.05) 1px,transparent 1px);
  background-size:40px 40px;mask-image:radial-gradient(circle at 50% 15%,#000 0%,transparent 65%)}
.card{position:relative;z-index:1;border-color:var(--line);box-shadow:0 0 0 1px rgba(34,227,255,.08),0 24px 60px #000a,0 0 26px rgba(34,227,255,.10)}
.card::before,.card::after{content:'';position:absolute;width:16px;height:16px;border:2px solid var(--blue);opacity:.8;pointer-events:none}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none;border-radius:17px 0 0 0}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none;border-radius:0 0 17px 0}
h1{font-weight:700;letter-spacing:.4px;text-shadow:0 0 20px rgba(34,227,255,.4)}
label{font-family:'Rajdhani',sans-serif;letter-spacing:.3px}
input{font-family:'JetBrains Mono',monospace;font-size:13.5px;transition:border-color .15s,box-shadow .15s}
input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 0 18px rgba(34,227,255,.3)}
button{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.6px;box-shadow:0 0 20px rgba(34,227,255,.4);transition:box-shadow .15s,transform .1s}
button:hover{box-shadow:0 0 28px rgba(34,227,255,.6);transform:translateY(-1px)}
</style></head><body>
<div class="card">
<h1>회원가입</h1>
<div class="sub">본인만의 아이디로 텔레그램·API 키·자동매매 설정을 따로 저장합니다.</div>
{ERROR_HTML}
<form method="post" action="/register">
<label for="name">이름</label>
<input id="name" name="name" autocomplete="name" required autofocus maxlength="60">
<label for="email">이메일</label>
<input id="email" name="email" type="email" autocomplete="email" required maxlength="120">
<label for="phone">핸드폰번호</label>
<input id="phone" name="phone" type="tel" autocomplete="tel" required maxlength="20" placeholder="010-1234-5678">
<label for="u">아이디</label>
<input id="u" name="username" autocomplete="username" required minlength="3" maxlength="32">
<label for="p">비밀번호</label>
<input id="p" name="password" type="password" autocomplete="new-password" required minlength="6">
<label for="p2">비밀번호 확인</label>
<input id="p2" name="password2" type="password" autocomplete="new-password" required minlength="6">
<button type="submit">가입하기</button>
</form>
<div class="altlink">이미 계정이 있으신가요? <a href="/login">로그인</a></div>
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
    user = get_user_by_username(username)
    # Always run check_password_hash even on a missing user (against a dummy
    # hash) so a nonexistent-username response takes the same time as a
    # wrong-password one - avoids leaking which usernames exist via timing.
    valid = user is not None and check_password_hash(user["password_hash"], password)
    if valid:
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        return redirect(next_path)
    return redirect(url_for("login_page", error="1", next=next_path))


@app.get("/register")
def register_page() -> str:
    error = request.args.get("error")
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return REGISTER_HTML.replace("{ERROR_HTML}", error_html)


@app.post("/register")
def register_submit() -> Response:
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if not name:
        return redirect(url_for("register_page", error="이름을 입력해주세요."))
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return redirect(url_for("register_page", error="올바른 이메일 주소를 입력해주세요."))
    phone_digits = re.sub(r"\D", "", phone)
    if len(phone_digits) < 9:
        return redirect(url_for("register_page", error="올바른 핸드폰번호를 입력해주세요."))
    if len(username) < 3 or len(username) > 32 or not re.match(r"^[A-Za-z0-9_.-]+$", username):
        return redirect(url_for("register_page", error="아이디는 3~32자, 영문/숫자/._-만 사용할 수 있습니다."))
    if len(password) < 6:
        return redirect(url_for("register_page", error="비밀번호는 6자 이상이어야 합니다."))
    if password != password2:
        return redirect(url_for("register_page", error="비밀번호가 서로 다릅니다."))
    if get_user_by_username(username) is not None:
        return redirect(url_for("register_page", error="이미 사용 중인 아이디입니다."))
    if get_user_by_email(email) is not None:
        return redirect(url_for("register_page", error="이미 가입된 이메일입니다."))
    user_id = create_user(username, password, name=name, email=email, phone=phone)
    session.clear()
    session["user_id"] = user_id
    session["username"] = username
    session.permanent = True
    log("AUTH", f"new user registered: {username} (id={user_id})")
    return redirect("/")


@app.get("/logout")
def logout() -> Response:
    session.clear()
    return redirect(url_for("login_page"))


SETTINGS_PAGE_HTML = r"""
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>뿌꾸의 코인세상 · 설정</title>
<style>
:root{--bg:#07101f;--panel:#0e1b31;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--green:#35e29a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:24px;margin:0 0 6px}.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:22px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 14px;color:#c7dcfa}
label{display:block;font-size:13px;color:#a9bfdf;font-weight:700;margin-bottom:6px}
input[type=text],input[type=password],input[type=number]{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line);background:#0a172b;color:var(--text);font-size:14px;margin-bottom:14px;font-family:'JetBrains Mono',monospace}
input:focus{outline:2px solid var(--blue)}
.field{margin-bottom:2px}
.checkrow{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.checkrow input{width:auto;margin:0}
.hint{font-size:12px;color:var(--muted);margin:-10px 0 14px}
.actions{display:flex;gap:10px;align-items:center;margin-top:10px}
button{padding:12px 20px;border-radius:10px;border:none;background:var(--blue);color:#04101f;font-weight:900;font-size:14px;cursor:pointer}
button:hover{filter:brightness(1.08)}
a.btn{color:var(--muted);text-decoration:none;font-size:13px}
.saved{background:rgba(53,226,154,.12);border:1px solid #35e29a55;color:var(--green);padding:10px 14px;border-radius:10px;font-size:13px;margin-bottom:16px}
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{--bg:#030711;--panel:#0a1424;--line:#17415f;--text:#e4f6ff;--muted:#5e84a3;--blue:#22e3ff;--green:#39ffa0}
body{font-family:'Rajdhani',Inter,system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;
  background:radial-gradient(ellipse 700px 500px at 18% -8%,rgba(34,227,255,.09),transparent 60%),
    radial-gradient(ellipse 600px 600px at 90% 30%,rgba(57,255,160,.05),transparent 60%),var(--bg)}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(34,227,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(34,227,255,.04) 1px,transparent 1px);
  background-size:40px 40px;mask-image:radial-gradient(circle at 50% 0%,#000 0%,transparent 55%)}
.wrap{position:relative;z-index:1}
h1{font-weight:700;letter-spacing:.4px;text-shadow:0 0 20px rgba(34,227,255,.35)}
.card{border-color:var(--line);box-shadow:0 0 0 1px rgba(34,227,255,.06),0 16px 40px #0008;position:relative}
.card::before,.card::after{content:'';position:absolute;width:14px;height:14px;border:2px solid var(--blue);opacity:.7;pointer-events:none}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none;border-radius:17px 0 0 0}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none;border-radius:0 0 17px 0}
.card h2{font-family:'Rajdhani',sans-serif;letter-spacing:.3px}
label{font-family:'Rajdhani',sans-serif;letter-spacing:.3px}
input[type=text],input[type=password],input[type=number]{transition:border-color .15s,box-shadow .15s}
input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 0 16px rgba(34,227,255,.28)}
button{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.5px;box-shadow:0 0 18px rgba(34,227,255,.35);transition:box-shadow .15s,transform .1s}
button:hover{box-shadow:0 0 26px rgba(34,227,255,.55);transform:translateY(-1px)}
</style></head><body><div class="wrap">
<h1>설정</h1>
<div class="sub">{USERNAME}님으로 로그인됨 · 여기서 바꾼 값은 저장 즉시 적용됩니다 (재시작 불필요). · <a class="btn" href="/logout" style="text-decoration:underline">로그아웃</a></div>
{SAVED_HTML}
<form method="post" action="/settings">
<div class="card"><h2>E-RANG 수집 / 진입 임박 알림 <span class="hint" style="margin:0">(모든 회원 공통)</span></h2>{FIELDS_COLLECT}</div>
<div class="card"><h2>텔레그램 알림 <span class="hint" style="margin:0">(내 계정 전용)</span></h2>{FIELDS_TELEGRAM}</div>
<div class="card"><h2>Bitget API (읽기 전용 키 권장) <span class="hint" style="margin:0">(내 계정 전용)</span></h2>{FIELDS_BITGET}</div>
<div class="card"><h2>업데이트 확인 <span class="hint" style="margin:0">(모든 회원 공통)</span></h2><div class="hint" style="margin-top:-6px">현재 버전: {APP_VERSION} · GitHub 저장소를 입력하면 새 버전이 나왔을 때 대시보드에 알림이 뜹니다 (자동 다운로드/설치는 안 합니다 - 링크만 보여드립니다).</div>{FIELDS_UPDATE}</div>
<div class="card" style="border-color:#ff536355"><h2 style="color:#ff8a94">⚠️ 자동매매 (BingX · 실제 주문이 나갈 수 있습니다) <span class="hint" style="margin:0">(내 계정 전용)</span></h2><div class="hint" style="margin-top:-6px">드라이런(모의) 모드를 꺼야만 실제 주문이 나갑니다. 처음엔 드라이런 상태로 며칠 로그를 확인해보시길 권장합니다.</div>{FIELDS_AUTOTRADE}</div>
<div class="actions"><button type="submit">저장</button><a class="btn" href="/">← 대시보드로</a></div>
</form>
<div class="card" style="border-color:#ff536355;margin-top:16px"><h2 style="color:#ff8a94">🛑 긴급 정지</h2><div class="hint" style="margin-top:-6px">내 자동매매를 즉시 끄고, 내 BingX에 걸려있는 미체결 주문을 전부 취소합니다. (이미 체결된 포지션 자체는 자동으로 청산하지 않습니다 - TP/SL이 계속 관리합니다.)</div><button type="button" id="killSwitchBtn" style="background:#ff5364;color:#1a0508">지금 즉시 정지</button></div>
<script>
document.getElementById('killSwitchBtn').onclick = async () => {
  if (!confirm('자동매매를 즉시 끄고 미체결 주문을 전부 취소합니다. 계속할까요?')) return;
  const btn = document.getElementById('killSwitchBtn');
  btn.disabled = true; btn.textContent = '처리 중...';
  try {
    const r = await fetch('/api/auto-trade/kill-switch', {method: 'POST'});
    const j = await r.json();
    alert(j.ok ? '✅ 자동매매 정지 완료. 취소된 주문: ' + JSON.stringify(j.cancel_result) : '❌ 실패: ' + JSON.stringify(j));
    location.reload();
  } catch (e) {
    alert('❌ 요청 실패: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = '지금 즉시 정지';
  }
};
</script>
</div></body></html>
"""


def _settings_field_html(key: str, label: str, input_type: str, secret: bool, current: str) -> str:
    if input_type == "checkbox":
        checked = "checked" if str(current).lower() not in {"0", "false", "no", "off", ""} else ""
        return (
            f'<div class="checkrow"><input type="checkbox" id="{key}" name="{key}" {checked}>'
            f'<label for="{key}" style="margin:0">{html.escape(label)}</label></div>'
        )
    value = "" if secret else html.escape(str(current), quote=True)
    placeholder = "변경하려면 입력 (비워두면 기존 값 유지)" if secret else ""
    return (
        f'<div class="field"><label for="{key}">{html.escape(label)}</label>'
        f'<input type="{input_type}" id="{key}" name="{key}" value="{value}" placeholder="{placeholder}"></div>'
    )


@app.get("/settings")
def settings_page() -> str:
    user_id = session["user_id"]
    groups = {
        "FIELDS_COLLECT": {"TARGET_URL", "ERANG_USERNAME", "ERANG_PASSWORD", "INTERVAL_SECONDS", "ENTRY_PROXIMITY_USD"},
        "FIELDS_TELEGRAM": {"DASHBOARD_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_NOTIFY_OFF"},
        "FIELDS_BITGET": {"BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE", "BITGET_CATEGORY"},
        "FIELDS_AUTOTRADE": {
            "AUTO_TRADE_ENABLED", "AUTO_TRADE_DRY_RUN", "AUTO_TRADE_SYMBOL", "AUTO_TRADE_LEVERAGE",
            "AUTO_TRADE_MARGIN_USDT", "AUTO_TRADE_MAX_DAILY_TRADES", "AUTO_TRADE_MAX_DAILY_LOSS_USDT",
            "BINGX_API_KEY", "BINGX_API_SECRET",
        },
        "FIELDS_UPDATE": {"GITHUB_REPO"},
    }
    global_keys = {k for k, _l, _t, _d, _s in GLOBAL_SETTINGS_SCHEMA}
    rendered = {g: "" for g in groups}
    for key, label, input_type, _default, secret in SETTINGS_SCHEMA:
        current = get_setting(key) if key in global_keys else get_user_setting(user_id, key)
        for group, keys in groups.items():
            if key in keys:
                rendered[group] += _settings_field_html(key, label, input_type, secret, current)
    saved_html = '<div class="saved">저장했습니다.</div>' if request.args.get("saved") else ""
    out = SETTINGS_PAGE_HTML
    out = out.replace("{SAVED_HTML}", saved_html)
    out = out.replace("{APP_VERSION}", html.escape(APP_VERSION))
    out = out.replace("{USERNAME}", html.escape(session.get("username", "")))
    for group, content in rendered.items():
        out = out.replace("{" + group + "}", content)
    return out


@app.post("/settings")
def settings_submit() -> Response:
    user_id = session["user_id"]
    global_keys = {k for k, _l, _t, _d, _s in GLOBAL_SETTINGS_SCHEMA}
    for key, _label, input_type, _default, secret in SETTINGS_SCHEMA:
        if input_type == "checkbox":
            value = "true" if request.form.get(key) else "false"
            if key in global_keys:
                save_setting(key, value)
            else:
                save_user_setting(user_id, key, value)
            continue
        value = request.form.get(key, "")
        if secret and not value:
            continue  # blank secret field means "keep the existing value", not "clear it"
        if key in global_keys:
            save_setting(key, value)
        else:
            save_user_setting(user_id, key, value)
    apply_settings()
    try:
        check_for_update()
    except Exception:
        pass
    return redirect(url_for("settings_page", saved="1"))


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
bitget_states: Dict[int, Dict[str, Any]] = {}


def _default_bitget_state() -> Dict[str, Any]:
    return {
        "configured": False,
        "positions": [], "fills": [], "orders": [], "account": {},
        "total_unrealized_pnl": None, "live_open_positions_pnl": None, "total_equity": None,
        "win_rate_pct": None, "win_count": 0, "loss_count": 0, "trade_count": 0,
        "realized_pnl_total": None, "combined_pnl": None,
        "updated_at": None, "last_error": None,
        "_last_fetch_monotonic": None,
    }


def get_bitget_state_for_user(user_id: int, force: bool = False) -> Dict[str, Any]:
    """Bitget data is fetched on-demand for whichever user is viewing the
    dashboard (each user has their own Bitget keys), throttled to at most
    once per BITGET_POLL_INTERVAL seconds so rapid dashboard polling (every
    1s) doesn't turn into rapid-fire real Bitget API calls."""
    state = bitget_states.setdefault(user_id, _default_bitget_state())
    settings = get_all_user_settings(user_id)
    api_key = settings["BITGET_API_KEY"]
    api_secret = settings["BITGET_API_SECRET"]
    api_passphrase = settings["BITGET_API_PASSPHRASE"]
    category = settings["BITGET_CATEGORY"] or "USDT-FUTURES"
    configured = bool(bitget_client and bitget_client.bitget_configured(api_key, api_secret, api_passphrase))
    state["configured"] = configured
    if not configured:
        return state
    now = time.monotonic()
    last_fetch = state.get("_last_fetch_monotonic")
    if not force and last_fetch is not None and (now - last_fetch) < BITGET_POLL_INTERVAL:
        return state
    try:
        summary = bitget_client.fetch_summary(
            api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase, category=category,
        )
        state["fills"] = summary.get("fills") or []
        state["orders"] = summary.get("orders") or []
        state["account"] = summary.get("account") or {}
        state["positions"] = summary.get("positions") or []
        state["total_unrealized_pnl"] = summary.get("total_unrealized_pnl")
        state["live_open_positions_pnl"] = summary.get("live_open_positions_pnl")
        state["total_equity"] = summary.get("total_equity")
        state["win_rate_pct"] = summary.get("win_rate_pct")
        state["win_count"] = summary.get("win_count") or 0
        state["loss_count"] = summary.get("loss_count") or 0
        state["trade_count"] = summary.get("trade_count") or 0
        state["realized_pnl_total"] = summary.get("realized_pnl_total")
        state["combined_pnl"] = summary.get("combined_pnl")
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = summary.get("errors") or None
    except Exception as exc:
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        log("BITGET_ERROR", f"user {user_id}: {state['last_error']}")
    state["_last_fetch_monotonic"] = now
    return state


def _public_bitget_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _new_telegram_state() -> Dict[str, Any]:
    return {
        "enabled": False,  # set from this user's own Telegram token/chat id
        "last_long_signal": None,   # None = unknown yet (e.g. right after boot/first check for this user)
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


# Per-user Telegram edge-trigger state, keyed by user_id - each user's ON/OFF
# tracking and "진입 임박" proximity flags are independent of everyone else's.
telegram_states: Dict[int, Dict[str, Any]] = {}


def _get_telegram_state(user_id: int) -> Dict[str, Any]:
    if user_id not in telegram_states:
        telegram_states[user_id] = _new_telegram_state()
    return telegram_states[user_id]

# How close current price must get to the 1st-stage (25%) entry price to
# trigger a "진입 임박" (entry imminent) alert, in raw price units (USD for BTC).
ENTRY_PROXIMITY_USD = float(os.getenv("ENTRY_PROXIMITY_USD", "100"))
_state_lock = threading.Lock()


def log(tag: str, message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] [{tag}] {message}", flush=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# SQLite connections/cursors aren't context managers the way psycopg's are -
# this wraps "open connection, get cursor, commit on success, always close"
# into the same `with db_cursor() as (conn, cur):` shape the rest of the
# file already uses, so call sites barely had to change.
@contextmanager
def db_cursor():
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    log("DB", f"initializing SQLite schema at {DB_PATH}")
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                target_url TEXT NOT NULL,
                http_status INTEGER,
                success INTEGER NOT NULL DEFAULT 0,
                current_price TEXT,
                current_price_raw TEXT,
                long_signal INTEGER,
                short_signal INTEGER,
                long_color TEXT,
                short_color TEXT,
                entry_message INTEGER,
                long_matched_selector TEXT,
                long_matched_declaration TEXT,
                long_ancestor_classes TEXT,
                long_label_classes TEXT,
                long_own_inline_background TEXT,
                short_matched_selector TEXT,
                short_matched_declaration TEXT,
                short_ancestor_classes TEXT,
                short_label_classes TEXT,
                short_own_inline_background TEXT,
                parsed_json TEXT,
                binance_json TEXT,
                raw_text TEXT,
                raw_html TEXT,
                content_sha256 TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_observed_at ON observations(observed_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_signal ON observations(long_signal, short_signal, observed_at DESC)")
        # v3.58 FINAL data collector migration - additive only, never drops
        # or renames anything: observed_at_kst (convenience copy of
        # observed_at in KST, spec section 2) and long_start/long_end/
        # short_start/short_end (spec section 2 - explicit OFF->ON / ON->OFF
        # transition markers, separate from the plain long_signal/
        # short_signal state columns that already existed).
        for column in ("observed_at_kst", "long_start", "long_end", "short_start", "short_end"):
            try:
                cur.execute(f"ALTER TABLE observations ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_long_start ON observations(long_start)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_short_start ON observations(short_start)")
        cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                email TEXT,
                phone TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Backward-compatible migration for databases created before
        # name/email/phone existed on the users table.
        for column in ("name", "email", "phone"):
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, key),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                symbol TEXT,
                entry_price TEXT,
                quantity TEXT,
                leverage INTEGER,
                take_profit TEXT,
                stop_loss TEXT,
                order_id TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                error TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_auto_trades_created_at ON auto_trades(created_at DESC)")
        # Backward-compatible migration for pre-multi-user databases that
        # already have an auto_trades table without user_id - must run
        # BEFORE the index below, which references that column.
        try:
            cur.execute("ALTER TABLE auto_trades ADD COLUMN user_id INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        cur.execute("CREATE INDEX IF NOT EXISTS idx_auto_trades_user ON auto_trades(user_id, created_at DESC)")
    log("DB", "schema ready")
    _seed_first_admin_user()


def _seed_first_admin_user() -> None:
    """On a brand-new database (or one upgraded from the old single-admin
    version), auto-creates a first user from ADMIN_USERNAME/ADMIN_PASSWORD
    (env vars, or the documented defaults) so nobody who already has this
    app running loses access when it upgrades to multi-user accounts."""
    with db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] > 0:
            return
        username = os.getenv("ADMIN_USERNAME", "admin")
        password = os.getenv("ADMIN_PASSWORD", "1Q2w3e4r5t!!")
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        new_id = cur.lastrowid
        log("BOOT", f"seeded first user '{username}' (id={new_id}) - log in with this, then register more accounts from /register")
        # Carry over any legacy global settings (from the single-admin era)
        # into this first user's own per-user settings, so upgrading doesn't
        # wipe out an existing Telegram/Bitget/BingX/auto-trade setup.
        legacy_keys = [
            "DASHBOARD_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_NOTIFY_OFF",
            "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE", "BITGET_CATEGORY",
            "AUTO_TRADE_ENABLED", "AUTO_TRADE_DRY_RUN", "AUTO_TRADE_SYMBOL", "AUTO_TRADE_LEVERAGE",
            "AUTO_TRADE_MARGIN_USDT", "AUTO_TRADE_MAX_DAILY_TRADES", "AUTO_TRADE_MAX_DAILY_LOSS_USDT",
            "BINGX_API_KEY", "BINGX_API_SECRET",
        ]
        cur.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(legacy_keys))})", legacy_keys)
        for key, value in cur.fetchall():
            cur.execute(
                "INSERT INTO user_settings(user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                (new_id, key, value),
            )


# ---------------------------------------------------------------------------
# Settings are split into two scopes:
#  - GLOBAL_SETTINGS_SCHEMA: one shared value for the whole app (the E-RANG
#    collector itself is a single shared background loop - it doesn't make
#    sense per-user).
#  - USER_SETTINGS_SCHEMA: each logged-in user has their own copy (Telegram
#    token/chat, exchange API keys, auto-trade config) stored in
#    user_settings, keyed by user_id.
# Each entry is (key, label, input_type, default, secret). "secret" fields
# render as type="password" and are masked (not pre-filled) in the form, but
# ARE still submitted/saved normally.
# ---------------------------------------------------------------------------
GLOBAL_SETTINGS_SCHEMA = [
    ("TARGET_URL", "E-RANG 조회 주소", "text", "https://e-rang.kr/api/coin.php", False),
    ("ERANG_USERNAME", "E-RANG(ADrang) 로그인 아이디", "text", "", False),
    ("ERANG_PASSWORD", "E-RANG(ADrang) 로그인 비밀번호", "password", "", True),
    ("INTERVAL_SECONDS", "수집 주기 (초)", "number", "30", False),
    ("ENTRY_PROXIMITY_USD", "진입 임박 알림 기준 (달러)", "number", "100", False),
    ("GITHUB_REPO", "새 버전 확인용 GitHub 저장소 (예: yourname/coin-monitor)", "text", "", False),
]

USER_SETTINGS_SCHEMA = [
    ("DASHBOARD_URL", "대시보드 링크 (텔레그램 메시지에 포함, 선택)", "text", "", False),
    ("TELEGRAM_BOT_TOKEN", "텔레그램 봇 토큰", "text", "", True),
    ("TELEGRAM_CHAT_ID", "텔레그램 채팅 ID", "text", "", False),
    ("TELEGRAM_NOTIFY_OFF", "신호 해제(OFF)시에도 알림 발송", "checkbox", "true", False),
    ("BITGET_API_KEY", "Bitget API Key", "text", "", True),
    ("BITGET_API_SECRET", "Bitget API Secret", "password", "", True),
    ("BITGET_API_PASSPHRASE", "Bitget Passphrase", "password", "", True),
    ("BITGET_CATEGORY", "Bitget 카테고리", "text", "USDT-FUTURES", False),
    # --- Auto-trading (BingX) - real money, handle with care -----------------
    ("AUTO_TRADE_ENABLED", "자동매매 활성화", "checkbox", "false", False),
    ("AUTO_TRADE_DRY_RUN", "드라이런(모의) 모드 - 체크하면 실제 주문 없이 로그만 남김", "checkbox", "true", False),
    ("AUTO_TRADE_SYMBOL", "매매 심볼 (BingX 형식, 예: BTC-USDT)", "text", "BTC-USDT", False),
    ("AUTO_TRADE_LEVERAGE", "레버리지", "number", "5", False),
    ("AUTO_TRADE_MARGIN_USDT", "1회 진입 마진 (USDT)", "number", "50", False),
    ("AUTO_TRADE_MAX_DAILY_TRADES", "일일 최대 진입 횟수", "number", "10", False),
    ("AUTO_TRADE_MAX_DAILY_LOSS_USDT", "일일 최대 손실 한도 (USDT, 초과시 자동정지)", "number", "100", False),
    ("BINGX_API_KEY", "BingX API Key", "text", "", True),
    ("BINGX_API_SECRET", "BingX API Secret", "password", "", True),
]
# Backward-compat alias - some helper code still refers to "SETTINGS_SCHEMA"
# meaning "every field that can show up in a form".
SETTINGS_SCHEMA = GLOBAL_SETTINGS_SCHEMA + USER_SETTINGS_SCHEMA

_settings_cache: Dict[str, str] = {}


def load_settings_cache() -> None:
    global _settings_cache
    try:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT key, value FROM settings")
            _settings_cache = {row["key"]: row["value"] for row in cur.fetchall()}
    except Exception as exc:
        log("SETTINGS_ERROR", f"failed to load settings: {type(exc).__name__}: {exc}")
        _settings_cache = {}


def _default_for(key: str) -> str:
    for schema_key, _label, _type, default, _secret in SETTINGS_SCHEMA:
        if schema_key == key:
            return default
    return ""


def get_setting(key: str) -> str:
    """Global setting (shared by the whole app - the E-RANG collector config)."""
    if key in _settings_cache:
        return _settings_cache[key]
    return _default_for(key)


def save_setting(key: str, value: str) -> None:
    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    _settings_cache[key] = value


def get_user_setting(user_id: int, key: str) -> str:
    """Per-user setting (Telegram/exchange keys/auto-trade config)."""
    try:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key))
            row = cur.fetchone()
            if row is not None and row["value"] is not None:
                return row["value"]
    except Exception as exc:
        log("SETTINGS_ERROR", f"failed to read user_setting {key} for user {user_id}: {type(exc).__name__}: {exc}")
    return _default_for(key)


def save_user_setting(user_id: int, key: str, value: str) -> None:
    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO user_settings(user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, key, value),
        )


def get_all_user_settings(user_id: int) -> Dict[str, str]:
    """All of one user's settings as a dict, with defaults filled in for
    anything they haven't saved yet - used by the background Telegram/
    auto-trade loops so they don't have to do a DB round-trip per field."""
    values = {key: default for key, _label, _type, default, _secret in USER_SETTINGS_SCHEMA}
    with db_cursor() as (conn, cur):
        cur.execute(
            f"SELECT key, value FROM user_settings WHERE user_id = ? AND key IN "
            f"({','.join('?' * len(USER_SETTINGS_SCHEMA))})",
            (user_id, *[k for k, _l, _t, _d, _s in USER_SETTINGS_SCHEMA]),
        )
        for row in cur.fetchall():
            if row["value"] is not None:
                values[row["key"]] = row["value"]
    return values


def list_user_ids() -> list[int]:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users ORDER BY id")
        return [row["id"] for row in cur.fetchall()]


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id, username, password_hash, name, email, phone FROM users WHERE username = ?", (username,))
        return cur.fetchone()


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id, username, name, email, phone FROM users WHERE id = ?", (user_id,))
        return cur.fetchone()


def create_user(
    username: str, password: str,
    name: Optional[str] = None, email: Optional[str] = None, phone: Optional[str] = None,
) -> int:
    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO users (username, password_hash, name, email, phone) VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(password), name, email, phone),
        )
        return int(cur.lastrowid)


def apply_settings() -> None:
    """Refreshes the GLOBAL config constants from the settings store (falling
    back to each field's default). Called once at boot after init_db(), and
    again after every global-settings save. Per-user settings (Telegram,
    exchange keys, auto-trade config) are NOT module globals anymore - they
    vary per user, so the background loops fetch them fresh per user via
    get_all_user_settings()."""
    global TARGET_URL, INTERVAL, ENTRY_PROXIMITY_USD, GITHUB_REPO, FLASK_SECRET_KEY
    global ERANG_USERNAME, ERANG_PASSWORD
    TARGET_URL = get_setting("TARGET_URL") or "https://e-rang.kr/api/coin.php"
    new_erang_username = (get_setting("ERANG_USERNAME") or "").strip()
    new_erang_password = get_setting("ERANG_PASSWORD") or ""
    if new_erang_username != ERANG_USERNAME or new_erang_password != ERANG_PASSWORD:
        # Credentials changed (e.g. just entered/updated in Settings) -
        # drop the cached session so the very next fetch logs in fresh
        # rather than keep reusing a session tied to the OLD account.
        _reset_erang_session()
    ERANG_USERNAME = new_erang_username
    ERANG_PASSWORD = new_erang_password
    try:
        INTERVAL = max(1, int(get_setting("INTERVAL_SECONDS") or 30))
    except (TypeError, ValueError):
        INTERVAL = 30
    try:
        ENTRY_PROXIMITY_USD = float(get_setting("ENTRY_PROXIMITY_USD") or 100)
    except (TypeError, ValueError):
        ENTRY_PROXIMITY_USD = 100.0
    GITHUB_REPO = (get_setting("GITHUB_REPO") or "").strip().strip("/")
    # Persist a stable Flask secret key across restarts once one exists, so
    # people don't get logged out every time the app is relaunched - unlike
    # a cloud redeploy, a desktop app restarts constantly (every time it's
    # opened), so a fresh random key each time would be a real annoyance.
    stored_key = get_setting("FLASK_SECRET_KEY")
    if not stored_key:
        stored_key = FLASK_SECRET_KEY or secrets.token_hex(32)
        save_setting("FLASK_SECRET_KEY", stored_key)
    FLASK_SECRET_KEY = stored_key
    app.secret_key = FLASK_SECRET_KEY


# ---------------------------------------------------------------------------
# Update check: compares APP_VERSION against the latest GitHub Release tag
# for GITHUB_REPO. Deliberately notify-only (never downloads/replaces the
# running exe automatically) - a silent self-update mechanism is exactly
# the kind of thing that's easy to get subtly wrong without being able to
# test it on a real Windows machine, and a broken silent updater is much
# worse than just showing a banner with a download link.
# ---------------------------------------------------------------------------
update_state: Dict[str, Any] = {
    "current_version": APP_VERSION,
    "latest_version": None,
    "update_available": False,
    "release_url": None,
    "checked_at": None,
    "error": None,
}


def _version_tuple(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update() -> None:
    if not GITHUB_REPO:
        with _state_lock:
            update_state["error"] = None
            update_state["update_available"] = False
        return
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"CoinMonitor/{APP_VERSION}",  # GitHub's API returns 403 without a User-Agent header
            },
        )
        resp.raise_for_status()
        data = resp.json()
        latest_tag = str(data.get("tag_name") or "").strip()
        release_url = data.get("html_url")
        is_newer = _version_tuple(latest_tag) > _version_tuple(APP_VERSION)
        with _state_lock:
            update_state["current_version"] = APP_VERSION
            update_state["latest_version"] = latest_tag or None
            update_state["update_available"] = bool(latest_tag and is_newer)
            update_state["release_url"] = release_url
            update_state["checked_at"] = datetime.now(timezone.utc).isoformat()
            update_state["error"] = None
        if is_newer:
            log("UPDATE", f"new version available: {latest_tag} (current: {APP_VERSION})")
    except Exception as exc:
        with _state_lock:
            update_state["error"] = f"{type(exc).__name__}: {exc}"
            update_state["checked_at"] = datetime.now(timezone.utc).isoformat()


def update_check_loop() -> None:
    while True:
        try:
            check_for_update()
        except Exception as exc:
            log("UPDATE_ERROR", f"{type(exc).__name__}: {exc}")
        time.sleep(6 * 3600)  # every 6 hours is plenty for a version check


_update_loop_started = False


def start_update_check_loop_once() -> None:
    global _update_loop_started
    with _state_lock:
        if _update_loop_started:
            return
        _update_loop_started = True
    threading.Thread(target=update_check_loop, name="update-checker", daemon=True).start()


def _decimal_from_raw(raw: Optional[str]) -> Optional[Decimal]:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


_erang_session: Optional[requests.Session] = None
_erang_session_lock = threading.Lock()


def _reset_erang_session() -> None:
    """Drops the cached logged-in session so the next fetch logs in fresh -
    called when credentials change, or when a fetch detects it's been
    logged out (session expired)."""
    global _erang_session
    with _erang_session_lock:
        _erang_session = None


def _erang_login(session: requests.Session) -> bool:
    """Logs into e-rang.kr (branded 'ADrang') using the documented login
    form at /libs/member/process.php - the site started requiring a logged
    -in session to see coin.php's real data, so an anonymous GET no longer
    works. Returns True if login looks like it succeeded."""
    if not ERANG_USERNAME or not ERANG_PASSWORD:
        log("ERANG_LOGIN", "ERANG_USERNAME/ERANG_PASSWORD not set - cannot log in, will keep trying anonymous fetches")
        return False
    try:
        resp = session.post(
            "https://e-rang.kr/libs/member/process.php",
            data={
                "shop_action": "member_login",
                "gubun": "customer",
                "retUrl": "https://e-rang.kr/m/",
                "IdSaveChk": "1",
                "member_id": ERANG_USERNAME,
                "member_pw": ERANG_PASSWORD,
            },
            headers={"User-Agent": USER_AGENT, "Referer": "https://e-rang.kr/libs/member/process.php"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        # This site doesn't return a clean JSON success/failure - the most
        # reliable signal is whether the login FORM shows up again in the
        # response (still-logged-out) vs. it doesn't (logged in).
        if resp.status_code < 400 and 'name="member_pw"' not in resp.text:
            log("ERANG_LOGIN", f"login looks successful (user: {ERANG_USERNAME})")
            return True
        log("ERANG_LOGIN", f"login form still present after POST - likely wrong ID/PW (HTTP {resp.status_code})")
        return False
    except Exception as exc:
        log("ERANG_LOGIN_ERROR", f"{type(exc).__name__}: {exc}")
        return False


def _get_erang_session() -> requests.Session:
    global _erang_session
    with _erang_session_lock:
        if _erang_session is None:
            _erang_session = requests.Session()
            _erang_login(_erang_session)
        return _erang_session


def fetch_target() -> tuple[int, str]:
    log("FETCH", f"GET {TARGET_URL}")
    session = _get_erang_session()
    response = session.get(
        TARGET_URL,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    log("HTTP", f"{response.status_code} {response.reason}; {len(response.content)} bytes")
    # If the session has expired (or login never succeeded), coin.php
    # redirects to/renders a login prompt instead of real data. Detect that,
    # log back in once, and retry the fetch a single time before giving up -
    # avoids permanently getting stuck logged-out until the next restart.
    if 'name="member_pw"' in response.text or 'mq=login' in response.url:
        log("ERANG_LOGIN", "session appears logged out - re-authenticating and retrying once")
        if _erang_login(session):
            response = session.get(
                TARGET_URL,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
            )
            log("HTTP", f"(retry) {response.status_code} {response.reason}; {len(response.content)} bytes")
    response.raise_for_status()
    html = response.text
    if len(html.encode("utf-8", "replace")) > MAX_HTML_BYTES:
        html = html.encode("utf-8", "replace")[:MAX_HTML_BYTES].decode("utf-8", "replace")
        log("WARN", f"HTML truncated to {MAX_HTML_BYTES} bytes")
    return response.status_code, html


def _prepare_sqlite_params(record: Dict[str, Any]) -> Dict[str, Any]:
    """SQLite has no native datetime/JSONB/boolean types the way Postgres
    does - this converts the record dict's Python-typed values into the
    plain str/int forms sqlite3 can bind directly."""
    params = dict(record)
    if isinstance(params.get("observed_at"), datetime):
        params["observed_at"] = params["observed_at"].isoformat()
    if isinstance(params.get("current_price"), Decimal):
        params["current_price"] = str(params["current_price"])
    for key in ("parsed_json", "binance_json"):
        params[key] = json.dumps(params.get(key) or {}, ensure_ascii=False)
    for key in ("success", "long_signal", "short_signal", "entry_message"):
        if key in params and params[key] is not None:
            params[key] = int(bool(params[key]))
    return params


def insert_observation(record: Dict[str, Any]) -> int:
    """Guards against inserting two rows with the exact same observed_at
    (spec section 33) - a soft, application-level check rather than a hard
    SQL UNIQUE constraint, since adding a UNIQUE constraint retroactively
    to an existing table could fail outright if any duplicates already
    slipped in before this check existed. In normal operation this should
    essentially never trigger (observed_at is stamped fresh, to
    microsecond precision, once per collection cycle), but a Flask debug-
    reloader double-invocation or a manual double-call to collect_once()
    could otherwise create one."""
    params = _prepare_sqlite_params(record)
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM observations WHERE observed_at = ?", (params["observed_at"],))
        existing = cur.fetchone()
        if existing:
            log("DB", f"skipped duplicate observed_at={params['observed_at']} (existing id={existing[0]})")
            return int(existing[0])
        cur.execute(
            """
            INSERT INTO observations (
                observed_at, observed_at_kst, target_url, http_status, success,
                current_price, current_price_raw,
                long_signal, short_signal, long_start, long_end, short_start, short_end,
                long_color, short_color, entry_message,
                long_matched_selector, long_matched_declaration, long_ancestor_classes,
                long_label_classes, long_own_inline_background,
                short_matched_selector, short_matched_declaration, short_ancestor_classes,
                short_label_classes, short_own_inline_background,
                parsed_json, binance_json, raw_text, raw_html, content_sha256, error
            ) VALUES (
                :observed_at, :observed_at_kst, :target_url, :http_status, :success,
                :current_price, :current_price_raw,
                :long_signal, :short_signal, :long_start, :long_end, :short_start, :short_end,
                :long_color, :short_color, :entry_message,
                :long_matched_selector, :long_matched_declaration, :long_ancestor_classes,
                :long_label_classes, :long_own_inline_background,
                :short_matched_selector, :short_matched_declaration, :short_ancestor_classes,
                :short_label_classes, :short_own_inline_background,
                :parsed_json, :binance_json, :raw_text, :raw_html, :content_sha256, :error
            )
            """,
            params,
        )
        saved_id = cur.lastrowid
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


def send_telegram_message(text: str, bot_token: str, chat_id: str, user_id: Optional[int] = None) -> None:
    if not bot_token or not chat_id:
        return
    state = _get_telegram_state(user_id) if user_id is not None else None
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            if state is not None:
                with _state_lock:
                    state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
                    state["last_error"] = None
                    state["sent_count"] += 1
            log("TELEGRAM", f"notification sent (user_id={user_id})")
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if state is not None:
                with _state_lock:
                    state["last_error"] = err
            log("TELEGRAM_ERROR", err)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        if state is not None:
            with _state_lock:
                state["last_error"] = err
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
    dashboard_url: str = "",
) -> str:
    label_kr = "롱(LONG)" if side == "long" else "숏(SHORT)"
    emoji = "🟦" if side == "long" else "🟥"
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")

    if not turned_on:
        # Release messages are intentionally minimal - just the announcement
        # and when it happened. No entry tables, no evidence detail, no
        # indicators: the dashboard already has all of that if it's needed.
        lines = [f"{emoji} <b>E-RANG {label_kr} 신호해제</b>", now_kst + " (KST)"]
        if dashboard_url:
            lines.append(f'<a href="{html.escape(dashboard_url, quote=True)}">대시보드 열기</a>')
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
    if dashboard_url:
        lines.append(f'<a href="{html.escape(dashboard_url, quote=True)}">대시보드 열기</a>')
    return "\n".join(lines)


def _maybe_notify_telegram(
    user_id: int,
    bot_token: str,
    chat_id: str,
    notify_off: bool,
    parsed: Dict[str, Any],
    long_active: bool,
    short_active: bool,
    current_price_raw: Optional[str],
    long_color: Optional[str],
    short_color: Optional[str],
    binance_snapshot: Optional[Dict[str, Any]] = None,
    dashboard_url: str = "",
) -> None:
    if not bot_token or not chat_id:
        return
    telegram_state = _get_telegram_state(user_id)
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
        if long_active or notify_off:
            try:
                duration = None if long_active else _format_duration(long_activated_at, now_dt)
                send_telegram_message(_build_signal_message(
                    "long", long_active, parsed, current_price_raw, long_color, binance_snapshot,
                    prev_color=long_prev_color, duration_text=duration, dashboard_url=dashboard_url,
                ), bot_token, chat_id, user_id)
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send LONG {'ON' if long_active else 'OFF'} message for user {user_id}: {type(exc).__name__}: {exc}")
    if prev_short is not None and short_active != prev_short:
        if short_active or notify_off:
            try:
                duration = None if short_active else _format_duration(short_activated_at, now_dt)
                send_telegram_message(_build_signal_message(
                    "short", short_active, parsed, current_price_raw, short_color, binance_snapshot,
                    prev_color=short_prev_color, duration_text=duration, dashboard_url=dashboard_url,
                ), bot_token, chat_id, user_id)
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send SHORT {'ON' if short_active else 'OFF'} message for user {user_id}: {type(exc).__name__}: {exc}")


def _maybe_notify_entry_proximity(
    user_id: int, bot_token: str, chat_id: str, dashboard_url: str,
    parsed: Dict[str, Any], current_price_raw: Optional[str],
) -> None:
    """Sends a '진입 임박' (entry imminent) alert when the current price gets
    within ENTRY_PROXIMITY_USD of the 1st-stage (25%) entry price for a side
    whose E-RANG signal is currently ON. If that side isn't ON, its
    proximity state resets so a later approach (once it turns ON) still
    fires fresh. Edge-triggered (state kept per-user) so it fires once when
    price first comes within range, not every 30s while it lingers there;
    it re-arms once price moves back out of range."""
    if not bot_token or not chat_id:
        return
    telegram_state = _get_telegram_state(user_id)
    current_price = _to_float_loose(current_price_raw)
    if current_price is None:
        return
    rows = _row_map_from_parsed(parsed)
    signals = parsed.get("signals") or {}
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    for side, entries_key, state_key, label, emoji in (
        ("long", "long", "long_near_entry", "롱(LONG)", "🟦"),
        ("short", "short", "short_near_entry", "숏(SHORT)", "🟥"),
    ):
        active = bool((signals.get(side) or {}).get("active"))
        if not active:
            with _state_lock:
                telegram_state[state_key] = False
            continue
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
                if dashboard_url:
                    msg += f'\n<a href="{html.escape(dashboard_url, quote=True)}">대시보드 열기</a>'
                send_telegram_message(msg, bot_token, chat_id, user_id)
            except Exception as exc:
                log("TELEGRAM_ERROR", f"failed to build/send {label} 진입임박 message for user {user_id}: {type(exc).__name__}: {exc}")


# =============================================================================
# Auto-trading (BingX) - places REAL orders with REAL money when enabled and
# not in dry-run mode. Every decision (trade or skip) is logged to the
# auto_trades table for audit. Defaults are deliberately conservative:
# AUTO_TRADE_ENABLED=False and AUTO_TRADE_DRY_RUN=True until someone opts in
# to both explicitly via the Settings page. Everything here is per-user -
# each user's own settings/credentials/edge-trigger state, since each
# registered account trades independently with their own BingX keys.
# =============================================================================
auto_trade_states: Dict[int, Dict[str, Any]] = {}


def _get_auto_trade_state(user_id: int) -> Dict[str, Any]:
    if user_id not in auto_trade_states:
        auto_trade_states[user_id] = {
            "long_near_entry": False,   # edge-trigger flags, same pattern as telegram_state's "진입 임박" tracking
            "short_near_entry": False,
        }
    return auto_trade_states[user_id]


def _auto_trade_log(user_id: int, side: str, action: str, **kwargs: Any) -> None:
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                """
                INSERT INTO auto_trades (user_id, side, action, symbol, entry_price, quantity, leverage,
                                          take_profit, stop_loss, order_id, dry_run, detail, error)
                VALUES (:user_id, :side, :action, :symbol, :entry_price, :quantity, :leverage,
                        :take_profit, :stop_loss, :order_id, :dry_run, :detail, :error)
                """,
                {
                    "user_id": user_id, "side": side, "action": action,
                    "symbol": kwargs.get("symbol"),
                    "entry_price": str(kwargs["entry_price"]) if kwargs.get("entry_price") is not None else None,
                    "quantity": str(kwargs["quantity"]) if kwargs.get("quantity") is not None else None,
                    "leverage": kwargs.get("leverage"),
                    "take_profit": str(kwargs["take_profit"]) if kwargs.get("take_profit") is not None else None,
                    "stop_loss": str(kwargs["stop_loss"]) if kwargs.get("stop_loss") is not None else None,
                    "order_id": kwargs.get("order_id"),
                    "dry_run": int(bool(kwargs.get("dry_run"))),
                    "detail": kwargs.get("detail"),
                    "error": kwargs.get("error"),
                },
            )
    except Exception as exc:
        log("AUTO_TRADE_ERROR", f"failed to write auto_trades log row: {type(exc).__name__}: {exc}")


def _count_today_trades(user_id: int) -> int:
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                "SELECT COUNT(*) FROM auto_trades WHERE user_id = ? AND action='order_placed' AND date(created_at) = date('now')",
                (user_id,),
            )
            return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def _today_realized_pnl_usdt(symbol: str, api_key: str, api_secret: str) -> float:
    """Sums today's realizedPnl from BingX fill history. Returns 0.0 (i.e.
    "no loss detected") if this can't be determined - see the fail-closed
    duplicate-position check below for why that's NOT the safe default in
    general; here it's acceptable because a failed PnL check only affects
    the loss-limit guard, not whether an order gets placed at all, and the
    daily *trade count* limit still provides a hard ceiling regardless."""
    if bingx_client is None or not bingx_client.bingx_configured(api_key, api_secret):
        return 0.0
    try:
        fills = bingx_client.fetch_fills(symbol=symbol, limit=200, api_key=api_key, api_secret=api_secret)
    except Exception as exc:
        log("AUTO_TRADE_ERROR", f"could not fetch fills for daily PnL check: {exc}")
        return 0.0
    today = datetime.now(timezone.utc).astimezone(KST).date()
    total = 0.0
    for f in fills:
        try:
            ts = int(f.get("time") or 0)
            if ts <= 0:
                continue
            fill_date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(KST).date()
            if fill_date == today:
                total += float(f.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _has_open_position(symbol: str, side: str, api_key: str, api_secret: str) -> Optional[bool]:
    """True/False if the live position check succeeded, or None if it
    failed - callers must treat None as "skip the trade to be safe" (a
    failed check is exactly the situation where placing an unverified
    duplicate order is most likely, so failing open here would be the
    worst possible default for a system that moves real money)."""
    if bingx_client is None:
        return None
    try:
        positions = bingx_client.fetch_positions(symbol, api_key=api_key, api_secret=api_secret)
    except Exception as exc:
        log("AUTO_TRADE_ERROR", f"position check failed, skipping trade to be safe: {exc}")
        return None
    want_side = "LONG" if side == "long" else "SHORT"
    for p in positions:
        try:
            amt = float(p.get("positionAmt") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if p.get("positionSide") == want_side and abs(amt) > 1e-9:
            return True
    return False


def _execute_auto_trade(user_id: int, side: str, rows: Dict[str, list], settings: Dict[str, str]) -> None:
    symbol = settings["AUTO_TRADE_SYMBOL"]
    dry_run = str(settings["AUTO_TRADE_DRY_RUN"]).lower() not in {"0", "false", "no", "off", ""}
    leverage = int(float(settings["AUTO_TRADE_LEVERAGE"] or 5))
    margin_usdt = float(settings["AUTO_TRADE_MARGIN_USDT"] or 50)
    max_daily_trades = int(float(settings["AUTO_TRADE_MAX_DAILY_TRADES"] or 10))
    max_daily_loss_usdt = float(settings["AUTO_TRADE_MAX_DAILY_LOSS_USDT"] or 100)
    api_key = settings["BINGX_API_KEY"]
    api_secret = settings["BINGX_API_SECRET"]

    entries = rows.get(side) or []
    tps = rows.get("tpLong" if side == "long" else "tpShort") or []
    sls = rows.get("slLong" if side == "long" else "slShort") or []
    if not entries:
        _auto_trade_log(user_id, side, "skipped_no_entry_data", symbol=symbol, dry_run=dry_run, detail="E-RANG 진입가 데이터 없음")
        return
    entry_price = _to_float_loose(entries[0])
    tp_price = _to_float_loose(tps[0]) if tps else None
    sl_price = _to_float_loose(sls[0]) if sls else None
    if entry_price is None or entry_price <= 0:
        _auto_trade_log(user_id, side, "skipped_bad_entry_price", symbol=symbol, dry_run=dry_run, detail=f"entry_price={entries[0]!r}")
        return

    has_position = _has_open_position(symbol, side, api_key, api_secret)
    if has_position is None or has_position:
        _auto_trade_log(
            user_id, side, "skipped_duplicate", symbol=symbol, entry_price=entry_price, dry_run=dry_run,
            detail="이미 열려있는 포지션으로 판단됨" if has_position else "포지션 확인 실패로 안전하게 건너뜀",
        )
        return

    today_count = _count_today_trades(user_id)
    if today_count >= max_daily_trades:
        _auto_trade_log(
            user_id, side, "skipped_daily_trade_limit", symbol=symbol, entry_price=entry_price, dry_run=dry_run,
            detail=f"오늘 진입 {today_count}회로 한도({max_daily_trades}회) 도달",
        )
        return

    today_pnl = _today_realized_pnl_usdt(symbol, api_key, api_secret)
    if today_pnl <= -abs(max_daily_loss_usdt):
        _auto_trade_log(
            user_id, side, "skipped_daily_loss_limit", symbol=symbol, entry_price=entry_price, dry_run=dry_run,
            detail=f"오늘 실현손익 {today_pnl:.2f} USDT로 한도(-{max_daily_loss_usdt:.2f}) 초과",
        )
        return

    notional = margin_usdt * leverage
    quantity = round(notional / entry_price, 6)
    if quantity <= 0:
        _auto_trade_log(user_id, side, "skipped_bad_quantity", symbol=symbol, entry_price=entry_price, dry_run=dry_run)
        return

    if dry_run:
        _auto_trade_log(
            user_id, side, "dry_run", symbol=symbol, entry_price=entry_price, quantity=quantity,
            leverage=leverage, take_profit=tp_price, stop_loss=sl_price, dry_run=True,
            detail="드라이런 모드 - 실제 주문 없음",
        )
        log("AUTO_TRADE", f"[DRY RUN] user={user_id} would {side.upper()} {symbol} qty={quantity} @ {entry_price} TP={tp_price} SL={sl_price}")
        return

    try:
        bingx_client.set_leverage(symbol, "LONG" if side == "long" else "SHORT", leverage, api_key=api_key, api_secret=api_secret)
    except Exception as exc:
        log("AUTO_TRADE_ERROR", f"set_leverage failed (continuing anyway): {exc}")

    try:
        order = bingx_client.create_order(
            symbol=symbol,
            side="BUY" if side == "long" else "SELL",
            position_side="LONG" if side == "long" else "SHORT",
            order_type="LIMIT",
            quantity=quantity,
            price=entry_price,
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
            api_key=api_key, api_secret=api_secret,
        )
        order_id = str((order or {}).get("orderId") or (order or {}).get("order", {}).get("orderId") or "")
        _auto_trade_log(
            user_id, side, "order_placed", symbol=symbol, entry_price=entry_price, quantity=quantity,
            leverage=leverage, take_profit=tp_price, stop_loss=sl_price, order_id=order_id,
            dry_run=False, detail=json.dumps(order, ensure_ascii=False)[:1000] if order else None,
        )
        log("AUTO_TRADE", f"order placed: user={user_id} {side.upper()} {symbol} qty={quantity} @ {entry_price} orderId={order_id}")
        bot_token, chat_id = settings["TELEGRAM_BOT_TOKEN"], settings["TELEGRAM_CHAT_ID"]
        if bot_token and chat_id:
            try:
                emoji = "🟦" if side == "long" else "🟥"
                msg = (
                    f"{emoji} <b>자동매매 진입</b>\n{html.escape(symbol)} {side.upper()} {quantity} @ {_fmt_num(entry_price)}\n"
                    f"TP {_fmt_num(tp_price) if tp_price else '-'} · SL {_fmt_num(sl_price) if sl_price else '-'}"
                )
                send_telegram_message(msg, bot_token, chat_id, user_id)
            except Exception:
                pass
    except Exception as exc:
        _auto_trade_log(
            user_id, side, "order_failed", symbol=symbol, entry_price=entry_price, quantity=quantity,
            leverage=leverage, take_profit=tp_price, stop_loss=sl_price,
            dry_run=False, error=f"{type(exc).__name__}: {exc}",
        )
        log("AUTO_TRADE_ERROR", f"order failed for user {user_id}: {type(exc).__name__}: {exc}")


def _maybe_auto_trade_for_user(
    user_id: int, parsed: Dict[str, Any], long_active: bool, short_active: bool, current_price_raw: Optional[str],
) -> None:
    """Real entries happen when price actually approaches the 1st-stage
    (25%) E-RANG entry level, NOT the instant the signal turns ON - the
    signal being ON is a necessary precondition (gates which side is even
    eligible), but by itself it doesn't mean price is anywhere near a
    sensible entry. This mirrors the "진입 임박" Telegram alert's trigger
    exactly (same ENTRY_PROXIMITY_USD threshold, same edge-triggering so it
    fires once per approach and re-arms once price moves back out of
    range) rather than duplicating separate logic."""
    if bingx_client is None:
        return
    settings = get_all_user_settings(user_id)
    enabled = str(settings["AUTO_TRADE_ENABLED"]).lower() not in {"0", "false", "no", "off", ""}
    if not enabled:
        return
    current_price = _to_float_loose(current_price_raw)
    if current_price is None:
        return
    rows = _row_map_from_parsed(parsed)
    state = _get_auto_trade_state(user_id)
    for side, active, state_key in (("long", long_active, "long_near_entry"), ("short", short_active, "short_near_entry")):
        if not active:
            with _state_lock:
                state[state_key] = False
            continue
        entries = rows.get(side) or []
        entry1 = _to_float_loose(entries[0]) if entries else None
        if entry1 is None:
            continue
        is_near = abs(current_price - entry1) <= ENTRY_PROXIMITY_USD
        with _state_lock:
            was_near = state.get(state_key, False)
            state[state_key] = is_near
        if is_near and not was_near:
            try:
                _execute_auto_trade(user_id, side, rows, settings)
            except Exception as exc:
                log("AUTO_TRADE_ERROR", f"unhandled error executing {side.upper()} auto-trade for user {user_id}: {type(exc).__name__}: {exc}")


def _maybe_auto_trade_all_users(parsed: Dict[str, Any], long_active: bool, short_active: bool, current_price_raw: Optional[str]) -> None:
    """Runs the auto-trade check for every registered user - each trades (or
    doesn't) independently based on their own settings."""
    try:
        user_ids = list_user_ids()
    except Exception as exc:
        log("AUTO_TRADE_ERROR", f"could not list users: {type(exc).__name__}: {exc}")
        return
    for user_id in user_ids:
        try:
            _maybe_auto_trade_for_user(user_id, parsed, long_active, short_active, current_price_raw)
        except Exception as exc:
            log("AUTO_TRADE_ERROR", f"unhandled error for user {user_id}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# "1/3/5/15분 변화량" - real wall-clock-minute lookback (not candle-count),
# built by looking up THIS app's own past observations rather than
# recomputing indicators on truncated kline data - a candle-count lookback
# means something different on every timeframe (1 candle back on 1h is an
# hour, on 1m is a minute), whereas "5 minutes ago" should mean the same
# thing regardless of which timeframe's indicators are being compared.
# ---------------------------------------------------------------------------
MINUTE_HISTORY_LOOKBACKS = (1, 3, 5, 15)
MINUTE_HISTORY_PATHS = {
    "rsi14": ("rsi14",),
    "macd": ("macd", "macd"),
    "macd_hist": ("macd", "histogram"),
    "cci20": ("cci20",),
    "stoch_k": ("stochastic", "k"),
    "adx": ("adx14", "adx"),
    "plus_di": ("adx14", "plus_di"),
    "minus_di": ("adx14", "minus_di"),
    "atr_pct": ("atr_pct",),
    "vwap_distance_pct": ("vwap_distance_pct",),
    "volume": ("volume",),
    "cvd": ("cvd", "cvd"),
    "taker_buy_ratio": ("taker_flow", "taker_buy_ratio"),
}
# Top-level (not per-interval) fields tracked the same way as OI - see
# _build_minute_history()'s handling of "oi" for the pattern this follows.
MINUTE_HISTORY_TOP_LEVEL_PATHS = {
    "long_short_ratio": ("long_short_ratio", "long_short_ratio"),
    "order_book_imbalance": ("order_book", "imbalance"),
}


def _extract_path(data: Any, path: tuple) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _binance_json_at(minutes_ago: int) -> Optional[Dict[str, Any]]:
    target = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                "SELECT binance_json FROM observations WHERE observed_at <= ? ORDER BY observed_at DESC LIMIT 1",
                (target,),
            )
            row = cur.fetchone()
    except Exception as exc:
        log("MINUTE_HISTORY_ERROR", f"lookup failed for {minutes_ago}min ago: {type(exc).__name__}: {exc}")
        return None
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _build_minute_history(current_binance_json: Dict[str, Any]) -> Dict[str, Any]:
    """For each configured timeframe and each tracked indicator, reports the
    current value plus the value from 1/3/5/15 real minutes ago (looked up
    from this app's own stored observations) and the deltas - directly
    answering "is this rising/falling and how fast", independent of
    candle-count-based history tracked separately per indicator."""
    snapshots = {m: _binance_json_at(m) for m in MINUTE_HISTORY_LOOKBACKS}
    current_indicators = current_binance_json.get("indicators") or {}
    result: Dict[str, Any] = {}
    for interval, cur_ind in current_indicators.items():
        interval_out: Dict[str, Any] = {}
        for field, path in MINUTE_HISTORY_PATHS.items():
            cur_val = _extract_path(cur_ind, path)
            field_out: Dict[str, Any] = {"current": cur_val}
            for m in MINUTE_HISTORY_LOOKBACKS:
                snap = snapshots.get(m)
                past_ind = (_extract_path(snap, ("indicators", interval)) or {}) if snap else {}
                past_val = _extract_path(past_ind, path) if past_ind else None
                field_out[f"m{m}_ago"] = past_val
                if isinstance(cur_val, (int, float)) and isinstance(past_val, (int, float)):
                    field_out[f"delta_{m}m"] = cur_val - past_val
                else:
                    field_out[f"delta_{m}m"] = None
            interval_out[field] = field_out
        result[interval] = interval_out
    # Open interest is a top-level (not per-interval) field.
    cur_oi = _to_float_loose(_extract_path(current_binance_json, ("open_interest", "openInterest")))
    oi_out: Dict[str, Any] = {"current": cur_oi}
    for m in MINUTE_HISTORY_LOOKBACKS:
        snap = snapshots.get(m)
        past_oi = _to_float_loose(_extract_path(snap, ("open_interest", "openInterest"))) if snap else None
        oi_out[f"m{m}_ago"] = past_oi
        oi_out[f"delta_{m}m"] = (cur_oi - past_oi) if (cur_oi is not None and past_oi is not None) else None
    result["oi"] = oi_out
    # Other top-level (not per-interval) fields - same pattern as OI above.
    for field, path in MINUTE_HISTORY_TOP_LEVEL_PATHS.items():
        cur_val = _to_float_loose(_extract_path(current_binance_json, path))
        field_out: Dict[str, Any] = {"current": cur_val}
        for m in MINUTE_HISTORY_LOOKBACKS:
            snap = snapshots.get(m)
            past_val = _to_float_loose(_extract_path(snap, path)) if snap else None
            field_out[f"m{m}_ago"] = past_val
            field_out[f"delta_{m}m"] = (cur_val - past_val) if (cur_val is not None and past_val is not None) else None
        result[field] = field_out
    return result


# ---------------------------------------------------------------------------
# long_start/long_end/short_start/short_end (spec section 2) - the E-RANG
# signal itself is global/shared (one collector for everyone), so this is
# process-wide state, not per-user. long_start/short_start are carried
# forward on every row FOR AS LONG AS that side stays ON (so any row can
# answer "when did the current streak begin" directly, which minutes_since_
# long_start/short_start then use) - long_end/short_end are stamped only on
# the exact row where that side turns OFF, marking when it just ended.
# ---------------------------------------------------------------------------
_signal_streak_state: Dict[str, Optional[str]] = {"long_start": None, "short_start": None}
_signal_streak_lock = threading.Lock()


def _init_signal_streak_state() -> None:
    """Recovers in-progress streak state after a restart by checking the
    most recent observation - if a side was already ON, look back for when
    that streak actually began instead of resetting to 'started now'."""
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                "SELECT long_signal, short_signal, long_start, short_start FROM observations ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            with _signal_streak_lock:
                if row[0]:
                    _signal_streak_state["long_start"] = row[2]
                if row[1]:
                    _signal_streak_state["short_start"] = row[3]
    except Exception as exc:
        log("DB_ERROR", f"failed to recover signal streak state: {type(exc).__name__}: {exc}")


def _update_signal_streak(observed_at_iso: str, long_active: bool, short_active: bool) -> Dict[str, Optional[str]]:
    with _signal_streak_lock:
        long_end = None
        short_end = None
        if long_active and not _signal_streak_state["long_start"]:
            _signal_streak_state["long_start"] = observed_at_iso
        elif not long_active and _signal_streak_state["long_start"]:
            long_end = observed_at_iso
            _signal_streak_state["long_start"] = None
        if short_active and not _signal_streak_state["short_start"]:
            _signal_streak_state["short_start"] = observed_at_iso
        elif not short_active and _signal_streak_state["short_start"]:
            short_end = observed_at_iso
            _signal_streak_state["short_start"] = None
        return {
            "long_start": _signal_streak_state["long_start"] if long_active else None,
            "long_end": long_end,
            "short_start": _signal_streak_state["short_start"] if short_active else None,
            "short_end": short_end,
        }


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
        "observed_at_kst": None,
        "long_start": None, "long_end": None, "short_start": None, "short_end": None,
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
            try:
                binance_json["minute_history"] = _build_minute_history(binance_json)
            except Exception as exc:
                log("MINUTE_HISTORY_ERROR", f"{type(exc).__name__}: {exc}")
            if liquidation_stream is not None:
                try:
                    binance_json["liquidations"] = liquidation_stream.liquidation_summary()
                except Exception as exc:
                    log("LIQUIDATION_ERROR", f"{type(exc).__name__}: {exc}")
        sha = hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()

        def _join_classes(values: Any) -> Optional[str]:
            vals = [str(v) for v in (values or []) if v]
            return ", ".join(vals) if vals else None

        observed_at_kst = observed_at.astimezone(KST).isoformat()
        long_active_now = bool(long_sig.get("active"))
        short_active_now = bool(short_sig.get("active"))
        streak = _update_signal_streak(observed_at.isoformat(), long_active_now, short_active_now)
        record.update(
            {
                "http_status": status_code,
                "success": True,
                "current_price": current_price,
                "current_price_raw": current_price_raw,
                "long_signal": long_active_now,
                "short_signal": short_active_now,
                "observed_at_kst": observed_at_kst,
                "long_start": streak["long_start"], "long_end": streak["long_end"],
                "short_start": streak["short_start"], "short_end": streak["short_end"],
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
            user_ids = list_user_ids()
        except Exception as exc:
            user_ids = []
            log("TELEGRAM_ERROR", f"could not list users for notify: {type(exc).__name__}: {exc}")
        for uid in user_ids:
            try:
                usettings = get_all_user_settings(uid)
                _maybe_notify_telegram(
                    uid,
                    usettings["TELEGRAM_BOT_TOKEN"], usettings["TELEGRAM_CHAT_ID"],
                    str(usettings["TELEGRAM_NOTIFY_OFF"]).lower() not in {"0", "false", "no", "off", ""},
                    parsed,
                    bool(long_sig.get("active")),
                    bool(short_sig.get("active")),
                    current_price_raw,
                    long_sig.get("detected_color"),
                    short_sig.get("detected_color"),
                    binance_json,
                    dashboard_url=usettings["DASHBOARD_URL"],
                )
            except Exception as exc:
                log("TELEGRAM_ERROR", f"notify failed for user {uid}: {type(exc).__name__}: {exc}")
            try:
                _maybe_notify_entry_proximity(
                    uid, usettings["TELEGRAM_BOT_TOKEN"], usettings["TELEGRAM_CHAT_ID"], usettings["DASHBOARD_URL"],
                    parsed, current_price_raw,
                )
            except Exception as exc:
                log("TELEGRAM_ERROR", f"entry-proximity notify failed for user {uid}: {type(exc).__name__}: {exc}")
        try:
            _maybe_auto_trade_all_users(parsed, bool(long_sig.get("active")), bool(short_sig.get("active")), current_price_raw)
        except Exception as exc:
            log("AUTO_TRADE_ERROR", f"auto-trade check failed: {type(exc).__name__}: {exc}")
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
        with db_cursor() as (conn, cur):
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
        "observed_at": row[1],
        "http_status": row[2],
        "success": bool(row[3]) if row[3] is not None else None,
        "current_price": str(row[4]) if row[4] is not None else None,
        "current_price_raw": row[5],
        "long_signal": bool(row[6]) if row[6] is not None else None,
        "short_signal": bool(row[7]) if row[7] is not None else None,
        "long_color": row[8],
        "short_color": row[9],
        "entry_message": bool(row[10]) if row[10] is not None else None,
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
        "parsed": json.loads(row[22]) if row[22] else {},
        "binance": json.loads(row[23]) if row[23] else {},
    }


@app.get("/healthz")
def healthz() -> Response:
    summary = db_summary()
    status_code = 200 if summary.get("database_ok") else 503
    return jsonify({"ok": summary.get("database_ok"), "collector": collector_state, "db": summary}), status_code


@app.get("/api/status")
def api_status() -> Response:
    summary = db_summary()
    user_id = session["user_id"]
    with _state_lock:
        state = dict(collector_state)
        live_price = dict(live_price_state)
        live_binance = dict(live_binance_state)
        telegram = dict(_get_telegram_state(user_id))
    bitget = _public_bitget_state(get_bitget_state_for_user(user_id))
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
    return jsonify(_public_bitget_state(get_bitget_state_for_user(session["user_id"])))


@app.get("/api/history")
def api_history() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "80"))))
    with db_cursor() as (conn, cur):
        cur.execute(
            f"""
            SELECT {OBSERVATION_COLUMNS}
            FROM observations ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = [row_to_dict(row) for row in cur.fetchall()]
    return jsonify({"items": rows})


@app.get("/api/signal-analysis")
def api_signal_analysis() -> Response:
    limit = max(1, min(500, int(request.args.get("limit", "200"))))
    with db_cursor() as (conn, cur):
        cur.execute(
            f"""
            SELECT {OBSERVATION_COLUMNS}
            FROM observations
            WHERE (COALESCE(long_signal, 0) OR COALESCE(short_signal, 0))
            ORDER BY id DESC LIMIT ?
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
    with db_cursor() as (conn, cur):
        cur.execute(
            f"""
            SELECT {OBSERVATION_COLUMNS}
            FROM observations
            WHERE COALESCE(long_signal, 0) OR COALESCE(short_signal, 0)
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = [row_to_dict(row) for row in cur.fetchall()]
    return jsonify({"items": rows})


@app.get("/api/debug-signal")
def api_debug_signal() -> Response:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT id, observed_at, parsed_json, raw_html FROM observations ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "no observations"}), 404
    parsed = json.loads(row[2]) if row[2] else {}
    html = row[3] or ""
    low = html.lower()
    snippets = {}
    for side in ("long", "short"):
        pos = low.find(side)
        snippets[side] = html[max(0, pos-500):pos+1200] if pos >= 0 else ""
    return jsonify({
        "ok": True, "id": row[0], "observed_at": row[1],
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
    user_id = session["user_id"]
    settings = get_all_user_settings(user_id)
    bot_token, chat_id = settings["TELEGRAM_BOT_TOKEN"], settings["TELEGRAM_CHAT_ID"]
    if not bot_token or not chat_id:
        return jsonify({"ok": False, "error": "텔레그램 봇 토큰 / 채팅 ID가 설정 안 됨 - /settings 에서 입력해주세요"}), 400
    now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    summary = db_summary()
    latest = summary.get("latest") or {}
    parsed = latest.get("parsed") or {}
    side = "long" if latest.get("long_signal") else ("short" if latest.get("short_signal") else None)
    if side and parsed.get("signals"):
        binance_snapshot = current_binance_snapshot() or latest.get("binance") or {}
        color = latest.get("long_color") if side == "long" else latest.get("short_color")
        preview = _build_signal_message(
            side, True, parsed, latest.get("current_price_raw"), color, binance_snapshot,
            dashboard_url=settings["DASHBOARD_URL"],
        )
        msg = f"🧪 <b>[테스트 미리보기 · 실제 알림과 동일한 형식]</b>\n\n{preview}"
    else:
        msg = (
            f"✅ Coin Monitor 테스트 메시지\n{now_kst} (KST)\n텔레그램 알림 설정이 정상 동작합니다.\n"
            "(지금은 LONG/SHORT가 둘 다 OFF라서 근거 포함 미리보기는 다음 ON 시점에 실제로 보내드릴게요.)"
        )
    send_telegram_message(msg, bot_token, chat_id, user_id)
    telegram_state = _get_telegram_state(user_id)
    with _state_lock:
        err = telegram_state.get("last_error")
    return jsonify({"ok": err is None, "error": err, "previewed_side": side})


@app.get("/api/update-status")
def api_update_status() -> Response:
    with _state_lock:
        return jsonify(dict(update_state))


@app.get("/api/events")
def api_events() -> Response:
    """Every LONG/SHORT start/end event (spec section 32) - richer than
    /api/chart-markers: each start event carries the FULL flattened
    indicator snapshot from that exact row, so a caller can see "what did
    every tracked indicator look like at the moment this signal began"
    without a second lookup. Built directly from the long_start/long_end/
    short_start/short_end columns (spec section 2) rather than re-deriving
    transitions from long_signal/short_signal."""
    days = max(1, min(180, int(request.args.get("days", "30"))))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, observed_at, long_start, long_end, short_start, short_end, parsed_json, binance_json "
            "FROM observations WHERE observed_at >= ? ORDER BY id ASC",
            (since,),
        )
        rows = cur.fetchall()
    events = []
    seen_long_start = None
    seen_short_start = None
    for rid, observed_at, long_start, long_end, short_start, short_end, parsed_text, binance_text in rows:
        if long_start and long_start != seen_long_start:
            events.append({
                "side": "long", "event": "start", "time": long_start, "row_id": rid,
                "entries": _flatten_entries_for_csv(parsed_text),
                "indicators": _flatten_binance_for_csv(binance_text),
            })
        seen_long_start = long_start
        if long_end:
            events.append({"side": "long", "event": "end", "time": long_end, "row_id": rid})
        if short_start and short_start != seen_short_start:
            events.append({
                "side": "short", "event": "start", "time": short_start, "row_id": rid,
                "entries": _flatten_entries_for_csv(parsed_text),
                "indicators": _flatten_binance_for_csv(binance_text),
            })
        seen_short_start = short_start
        if short_end:
            events.append({"side": "short", "event": "end", "time": short_end, "row_id": rid})
    return jsonify({"events": events})


@app.get("/export-signals.csv")
def export_signals_csv() -> Response:
    """One row per LONG/SHORT START event (spec section 32) - the same
    column set as /export.csv, but pre-filtered to just the moments a
    signal began, so this is directly usable as a labeled dataset ("what
    did every indicator look like right when LONG/SHORT started") without
    filtering the full observation history yourself."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "side", "start_time", "row_id",
        *CSV_ENTRY_COLUMNS,
        *CSV_MARKET_COLUMNS,
        *CSV_INDICATOR_COLUMNS,
    ])
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, long_start, short_start, parsed_json, binance_json FROM observations ORDER BY id ASC"
        )
        rows = cur.fetchall()
    seen_long_start = None
    seen_short_start = None
    for rid, long_start, short_start, parsed_text, binance_text in rows:
        if long_start and long_start != seen_long_start:
            entries = _flatten_entries_for_csv(parsed_text)
            market = _flatten_binance_for_csv(binance_text)
            writer.writerow([
                "long", long_start, rid,
                *[entries.get(c) for c in CSV_ENTRY_COLUMNS],
                *[market.get(c) for c in CSV_MARKET_COLUMNS],
                *[market.get(c) for c in CSV_INDICATOR_COLUMNS],
            ])
        seen_long_start = long_start
        if short_start and short_start != seen_short_start:
            entries = _flatten_entries_for_csv(parsed_text)
            market = _flatten_binance_for_csv(binance_text)
            writer.writerow([
                "short", short_start, rid,
                *[entries.get(c) for c in CSV_ENTRY_COLUMNS],
                *[market.get(c) for c in CSV_MARKET_COLUMNS],
                *[market.get(c) for c in CSV_INDICATOR_COLUMNS],
            ])
        seen_short_start = short_start
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=coin_signal_events.csv"},
    )


@app.get("/api/chart-klines")
def api_chart_klines() -> Response:
    """Fetches OHLC candles server-side and returns them ready for the
    chart - routed through our own backend (which is already proven to
    reach Binance reliably for the indicator pipeline) instead of having
    the browser call Binance directly, since a person's own network/
    browser may not be able to reach it (CORS, firewall, region). Time
    values are shifted by +9h (KST) so Lightweight Charts - which always
    labels its axis in UTC - displays Korean wall-clock time instead."""
    interval = request.args.get("interval", "15m")
    if interval not in {"1m", "5m", "15m", "1h", "4h"}:
        interval = "15m"
    limit = max(50, min(500, int(request.args.get("limit", "200"))))
    try:
        import binance_data
        klines = binance_data.fetch_klines(interval, limit)
        candles = [
            {
                "time": int(k[0]) // 1000 + 9 * 3600,  # ms->s, then shift to KST wall-clock for chart display
                "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines
        ]
        return jsonify({"candles": candles})
    except Exception as exc:
        return jsonify({"candles": [], "error": f"{type(exc).__name__}: {exc}"}), 502


@app.get("/api/chart-markers")
def api_chart_markers() -> Response:
    """LONG/SHORT signal OFF->ON transition points (edge-triggered, same
    definition used everywhere else in the app) within the requested
    lookback window, for overlaying as arrows on the price chart. E-RANG
    signal collection is shared/global (one collector for everyone), so
    this isn't scoped per-user."""
    days = max(1, min(90, int(request.args.get("days", "14"))))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT observed_at, long_signal, short_signal FROM observations "
            "WHERE observed_at >= ? ORDER BY id ASC",
            (since,),
        )
        rows = cur.fetchall()
    markers = []
    prev_long: Optional[bool] = None
    prev_short: Optional[bool] = None
    for observed_at, long_sig, short_sig in rows:
        long_active = bool(long_sig)
        short_active = bool(short_sig)
        if prev_long is not None and long_active and not prev_long:
            markers.append({"time": observed_at, "side": "long"})
        if prev_short is not None and short_active and not prev_short:
            markers.append({"time": observed_at, "side": "short"})
        prev_long, prev_short = long_active, short_active
    return jsonify({"markers": markers})


@app.get("/api/auto-trade/status")
def api_auto_trade_status() -> Response:
    user_id = session["user_id"]
    settings = get_all_user_settings(user_id)
    symbol = settings["AUTO_TRADE_SYMBOL"]
    api_key, api_secret = settings["BINGX_API_KEY"], settings["BINGX_API_SECRET"]
    configured = bool(bingx_client and bingx_client.bingx_configured(api_key, api_secret))
    today_pnl = None
    if configured:
        try:
            today_pnl = _today_realized_pnl_usdt(symbol, api_key, api_secret)
        except Exception:
            today_pnl = None
    return jsonify({
        "enabled": str(settings["AUTO_TRADE_ENABLED"]).lower() not in {"0", "false", "no", "off", ""},
        "dry_run": str(settings["AUTO_TRADE_DRY_RUN"]).lower() not in {"0", "false", "no", "off", ""},
        "symbol": symbol,
        "leverage": settings["AUTO_TRADE_LEVERAGE"],
        "margin_usdt": settings["AUTO_TRADE_MARGIN_USDT"],
        "max_daily_trades": settings["AUTO_TRADE_MAX_DAILY_TRADES"],
        "max_daily_loss_usdt": settings["AUTO_TRADE_MAX_DAILY_LOSS_USDT"],
        "today_trade_count": _count_today_trades(user_id),
        "today_realized_pnl_usdt": today_pnl,
        "bingx_configured": configured,
    })


@app.get("/api/auto-trade/log")
def api_auto_trade_log() -> Response:
    user_id = session["user_id"]
    limit = max(1, min(500, int(request.args.get("limit", "100"))))
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, created_at, side, action, symbol, entry_price, quantity, leverage, "
            "take_profit, stop_loss, order_id, dry_run, detail, error "
            "FROM auto_trades WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        items = [
            {
                "id": r[0], "created_at": r[1], "side": r[2], "action": r[3], "symbol": r[4],
                "entry_price": r[5], "quantity": r[6], "leverage": r[7], "take_profit": r[8],
                "stop_loss": r[9], "order_id": r[10], "dry_run": bool(r[11]), "detail": r[12], "error": r[13],
            }
            for r in cur.fetchall()
        ]
    return jsonify({"items": items})


@app.post("/api/auto-trade/kill-switch")
def api_auto_trade_kill_switch() -> Response:
    """Emergency stop: turns off MY OWN auto-trading immediately and cancels
    every open order for my configured symbol (does not affect other
    users). Does NOT close already-filled positions (those still have
    their own TP/SL orders working) - closing live positions automatically
    is a separate, deliberately-not-included action since doing that wrong
    (e.g. on a flaky connection) could itself cause a loss."""
    user_id = session["user_id"]
    settings = get_all_user_settings(user_id)
    symbol = settings["AUTO_TRADE_SYMBOL"]
    dry_run = str(settings["AUTO_TRADE_DRY_RUN"]).lower() not in {"0", "false", "no", "off", ""}
    save_user_setting(user_id, "AUTO_TRADE_ENABLED", "false")
    cancel_result = None
    api_key, api_secret = settings["BINGX_API_KEY"], settings["BINGX_API_SECRET"]
    if bingx_client is not None and bingx_client.bingx_configured(api_key, api_secret):
        try:
            cancel_result = bingx_client.cancel_all_open_orders(symbol, api_key=api_key, api_secret=api_secret)
        except Exception as exc:
            cancel_result = {"error": f"{type(exc).__name__}: {exc}"}
    _auto_trade_log(
        user_id, "both", "kill_switch", symbol=symbol, dry_run=dry_run,
        detail=json.dumps(cancel_result, ensure_ascii=False) if cancel_result else None,
    )
    log("AUTO_TRADE", f"KILL SWITCH activated for user {user_id} - orders cancelled: {cancel_result}")
    return jsonify({"ok": True, "auto_trade_enabled": False, "cancel_result": cancel_result})


CSV_INDICATOR_INTERVALS = ["1m", "5m", "15m", "1h", "4h"]
CSV_INDICATOR_FIELDS = [
    "close", "ema9", "ema20", "ema21", "ema50", "ema100", "ema200", "ema_alignment", "ema_fast_cross",
    "dema20", "hma20",
    "rsi7", "rsi14", "rsi21", "roc12", "atr7", "atr14", "atr_pct",
    "macd", "macd_signal", "macd_hist", "macd_cross",
    "bb_upper", "bb_middle", "bb_lower", "bb_width", "bb_width_pct", "bb_position",
    "kc_upper", "kc_middle", "kc_lower", "bb_keltner_squeeze",
    "stoch_k", "stoch_d", "stoch_cross", "adx", "plus_di", "minus_di", "di_cross", "di_diff",
    "cci14", "cci", "cci_zero_cross", "cci_plus100_cross", "cci_minus100_cross",
    "vwap", "vwap_distance_pct", "vwap_diff", "vwap_diff_atr", "vwap_slope", "taker_buy_ratio", "taker_sell_ratio",
    "volume_ma20", "volume_ratio", "volume_ma_5", "volume_ma_50", "volume_delta", "volume_slope", "volume_spike",
    "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b",
    "ichimoku_chikou", "ichimoku_cloud_top", "ichimoku_cloud_bottom", "ichimoku_price_vs_cloud",
    "ichimoku_cloud_thickness", "ichimoku_distance_to_cloud_pct",
    "stoch_rsi_k", "stoch_rsi_d",
    "supertrend_value", "supertrend_direction",
    "structure_last_high_type", "structure_last_low_type", "structure", "structure_bos", "structure_choch",
    "pivot_high", "pivot_low", "dist_to_pivot_high_pct", "dist_to_pivot_low_pct",
    "highest_high", "lowest_low", "dist_to_highest_high_pct", "dist_to_lowest_low_pct",
    "price_change_pct", "return_1", "return_3", "return_5", "return_15",
    "close_vs_ema9_pct", "close_vs_ema20_pct", "close_vs_ema50_pct", "close_vs_ema100_pct", "close_vs_ema200_pct",
    "ema9_ema21_pct", "ema20_ema50_pct", "ema50_ema200_pct",
    "cvd", "cvd_slope5", "cvd_divergence",
]
# "변화량/기울기" (rate-of-change) columns - current / 1-candle-ago /
# 5-candles-ago / deltas / slope, for the core momentum+trend indicators.
CSV_HISTORY_INDICATORS = ["rsi14", "macd_hist", "atr_pct", "cci20", "stoch_k", "adx14"]
CSV_HISTORY_SUFFIXES = ["current", "prev1", "prev5", "delta1", "delta5", "slope5"]
# Real wall-clock-minute lookback columns (see _build_minute_history) -
# separate from the candle-count history above.
CSV_MINUTE_HISTORY_FIELDS = list(MINUTE_HISTORY_PATHS.keys())
CSV_MINUTE_HISTORY_SUFFIXES = ["current"] + [f"m{m}_ago" for m in MINUTE_HISTORY_LOOKBACKS] + [f"delta_{m}m" for m in MINUTE_HISTORY_LOOKBACKS]


def _flatten_indicator_row(ind: Dict[str, Any]) -> Dict[str, Any]:
    macd_d = ind.get("macd") or {}
    bb_d = ind.get("bollinger20") or {}
    kc_d = ind.get("keltner20") or {}
    stoch_d = ind.get("stochastic") or {}
    adx_d = ind.get("adx14") or {}
    taker_d = ind.get("taker_flow") or {}
    ichi_d = ind.get("ichimoku") or {}
    ichi_extra_d = ind.get("ichimoku_extra") or {}
    stoch_rsi_d = ind.get("stoch_rsi") or {}
    supertrend_d = ind.get("supertrend") or {}
    structure_d = ind.get("market_structure") or {}
    structure_breaks_d = ind.get("market_structure_breaks") or {}
    cvd_d = ind.get("cvd") or {}
    ema_align_d = ind.get("ema_alignment") or {}
    ema_dist_d = ind.get("ema_distance") or {}
    volume_ma_d = ind.get("volume_ma20") or {}
    volume_extra_d = ind.get("volume_extra") or {}
    pivot_d = ind.get("pivot_levels") or {}
    recent_hl_d = ind.get("recent_high_low") or {}
    returns_d = ind.get("price_returns") or {}
    bb_extra_d = ind.get("bollinger_extra") or {}
    vwap_extra_d = ind.get("vwap_extra") or {}
    cci_cross_d = ind.get("cci_cross") or {}
    taker_buy_ratio = taker_d.get("taker_buy_ratio")
    plus_di, minus_di = adx_d.get("plus_di"), adx_d.get("minus_di")
    out = {
        "close": ind.get("close"),
        "ema9": ind.get("ema9"), "ema20": ind.get("ema20"), "ema21": ind.get("ema21"),
        "ema50": ind.get("ema50"), "ema100": ind.get("ema100"), "ema200": ind.get("ema200"),
        "ema_alignment": ema_align_d.get("alignment"), "ema_fast_cross": ema_align_d.get("fast_cross"),
        "dema20": ind.get("dema20"), "hma20": ind.get("hma20"),
        "rsi7": ind.get("rsi7"), "rsi14": ind.get("rsi14"), "rsi21": ind.get("rsi21"), "roc12": ind.get("roc12"),
        "atr7": ind.get("atr7"), "atr14": ind.get("atr14"), "atr_pct": ind.get("atr_pct"),
        "macd": macd_d.get("macd"), "macd_signal": macd_d.get("signal"), "macd_hist": macd_d.get("histogram"),
        "macd_cross": ind.get("macd_cross"),
        "bb_upper": bb_d.get("upper"), "bb_middle": bb_d.get("middle"), "bb_lower": bb_d.get("lower"),
        "bb_width": bb_extra_d.get("bb_width"), "bb_width_pct": bb_extra_d.get("bb_width_pct"),
        "bb_position": bb_extra_d.get("bb_position"),
        "kc_upper": kc_d.get("upper"), "kc_middle": kc_d.get("middle"), "kc_lower": kc_d.get("lower"),
        "bb_keltner_squeeze": ind.get("bb_keltner_squeeze"),
        "stoch_k": stoch_d.get("k"), "stoch_d": stoch_d.get("d"), "stoch_cross": ind.get("stoch_cross"),
        "adx": adx_d.get("adx"), "plus_di": plus_di, "minus_di": minus_di,
        "di_cross": ind.get("di_cross"),
        "di_diff": (plus_di - minus_di) if (plus_di is not None and minus_di is not None) else None,
        "cci14": ind.get("cci14"), "cci": ind.get("cci20"),
        "cci_zero_cross": cci_cross_d.get("zero_cross"), "cci_plus100_cross": cci_cross_d.get("plus100_cross"),
        "cci_minus100_cross": cci_cross_d.get("minus100_cross"),
        "vwap": ind.get("vwap"), "vwap_distance_pct": ind.get("vwap_distance_pct"),
        "vwap_diff": vwap_extra_d.get("vwap_diff"), "vwap_diff_atr": vwap_extra_d.get("vwap_diff_atr"),
        "vwap_slope": vwap_extra_d.get("vwap_slope"),
        "taker_buy_ratio": taker_buy_ratio,
        "taker_sell_ratio": (1 - taker_buy_ratio) if taker_buy_ratio is not None else None,
        "volume_ma20": volume_ma_d.get("volume_ma"), "volume_ratio": volume_ma_d.get("volume_ratio"),
        "volume_ma_5": volume_extra_d.get("volume_ma_5"), "volume_ma_50": volume_extra_d.get("volume_ma_50"),
        "volume_delta": volume_extra_d.get("volume_delta"), "volume_slope": volume_extra_d.get("volume_slope"),
        "volume_spike": volume_extra_d.get("volume_spike"),
        "ichimoku_tenkan": ichi_d.get("tenkan_sen"), "ichimoku_kijun": ichi_d.get("kijun_sen"),
        "ichimoku_senkou_a": ichi_d.get("senkou_span_a"), "ichimoku_senkou_b": ichi_d.get("senkou_span_b"),
        "ichimoku_chikou": ichi_d.get("chikou_span"),
        "ichimoku_cloud_top": ichi_d.get("cloud_top"), "ichimoku_cloud_bottom": ichi_d.get("cloud_bottom"),
        "ichimoku_price_vs_cloud": ichi_d.get("price_vs_cloud"),
        "ichimoku_cloud_thickness": ichi_extra_d.get("cloud_thickness"),
        "ichimoku_distance_to_cloud_pct": ichi_extra_d.get("distance_to_cloud_pct"),
        "stoch_rsi_k": stoch_rsi_d.get("k"), "stoch_rsi_d": stoch_rsi_d.get("d"),
        "supertrend_value": supertrend_d.get("value"), "supertrend_direction": supertrend_d.get("direction"),
        "structure_last_high_type": structure_d.get("last_high_type"),
        "structure_last_low_type": structure_d.get("last_low_type"),
        "structure": structure_d.get("structure"),
        "structure_bos": structure_breaks_d.get("bos"), "structure_choch": structure_breaks_d.get("choch"),
        "pivot_high": pivot_d.get("recent_pivot_high"), "pivot_low": pivot_d.get("recent_pivot_low"),
        "dist_to_pivot_high_pct": pivot_d.get("dist_to_pivot_high_pct"),
        "dist_to_pivot_low_pct": pivot_d.get("dist_to_pivot_low_pct"),
        "highest_high": recent_hl_d.get("highest_high"), "lowest_low": recent_hl_d.get("lowest_low"),
        "dist_to_highest_high_pct": recent_hl_d.get("dist_to_highest_high_pct"),
        "dist_to_lowest_low_pct": recent_hl_d.get("dist_to_lowest_low_pct"),
        "price_change_pct": returns_d.get("price_change_pct"),
        "return_1": returns_d.get("return_1"), "return_3": returns_d.get("return_3"),
        "return_5": returns_d.get("return_5"), "return_15": returns_d.get("return_15"),
        "close_vs_ema9_pct": ema_dist_d.get("close_vs_ema9_pct"), "close_vs_ema20_pct": ema_dist_d.get("close_vs_ema20_pct"),
        "close_vs_ema50_pct": ema_dist_d.get("close_vs_ema50_pct"), "close_vs_ema100_pct": ema_dist_d.get("close_vs_ema100_pct"),
        "close_vs_ema200_pct": ema_dist_d.get("close_vs_ema200_pct"),
        "ema9_ema21_pct": ema_dist_d.get("ema9_ema21_pct"), "ema20_ema50_pct": ema_dist_d.get("ema20_ema50_pct"),
        "ema50_ema200_pct": ema_dist_d.get("ema50_ema200_pct"),
        "cvd": cvd_d.get("cvd"), "cvd_slope5": cvd_d.get("cvd_slope5"), "cvd_divergence": ind.get("cvd_divergence"),
    }
    for hist_key in CSV_HISTORY_INDICATORS:
        hist_d = ind.get(f"{hist_key}_history") or {}
        for suffix in CSV_HISTORY_SUFFIXES:
            out[f"{hist_key}_{suffix}"] = hist_d.get(suffix)
    return out


def _flatten_binance_for_csv(binance_json_text: Optional[str]) -> Dict[str, Any]:
    """Turns one row's nested binance_json blob into flat column_name ->
    value pairs (per-interval indicators + order book + funding), so the
    exported CSV can be analyzed directly (e.g. loaded into pandas) without
    parsing JSON per cell."""
    out: Dict[str, Any] = {}
    try:
        data = json.loads(binance_json_text) if binance_json_text else {}
    except (TypeError, ValueError):
        data = {}
    indicators = data.get("indicators") or {}
    for interval in CSV_INDICATOR_INTERVALS:
        flat = _flatten_indicator_row(indicators.get(interval) or {})
        for field in CSV_INDICATOR_FIELDS_WITH_HISTORY:
            out[f"{interval}_{field}"] = flat.get(field)
    ob = data.get("order_book") or {}
    out["order_book_bid_volume"] = ob.get("bid_volume")
    out["order_book_ask_volume"] = ob.get("ask_volume")
    out["order_book_imbalance"] = ob.get("imbalance")
    out["order_book_imbalance_5"] = ob.get("imbalance_5")
    out["order_book_imbalance_10"] = ob.get("imbalance_10")
    out["order_book_imbalance_20"] = ob.get("imbalance_20")
    out["order_book_spread"] = ob.get("spread")
    out["order_book_spread_pct"] = ob.get("spread_pct")
    premium = data.get("premium_index") or {}
    out["funding_rate_latest"] = premium.get("lastFundingRate")
    fx = data.get("funding_extra") or {}
    out["funding_change"] = fx.get("funding_change")
    out["funding_extreme"] = fx.get("funding_extreme")
    oi = data.get("open_interest") or {}
    out["open_interest"] = oi.get("openInterest")
    ls = data.get("long_short_ratio") or {}
    out["long_short_ratio"] = ls.get("long_short_ratio")
    out["long_account_pct"] = ls.get("long_account_pct")
    out["short_account_pct"] = ls.get("short_account_pct")
    tt = data.get("top_trader_ratio") or {}
    out["top_account_long_short_ratio"] = tt.get("top_account_long_short_ratio")
    out["top_position_long_short_ratio"] = tt.get("top_position_long_short_ratio")
    oi_change = data.get("oi_change") or {}
    out["oi_change_pct"] = oi_change.get("oi_change_pct")
    out["oi_price_classification"] = data.get("oi_price_classification")
    tc = data.get("time_context") or {}
    out["hour_kst"] = tc.get("hour_kst")
    out["hour_utc"] = tc.get("hour_utc")
    out["weekday"] = tc.get("weekday")
    out["is_weekend"] = tc.get("is_weekend")
    sessions = tc.get("sessions") or []
    out["session_asia"] = "asia" in sessions
    out["session_europe"] = "europe" in sessions
    out["session_us"] = "us" in sessions
    out["session_overlap"] = tc.get("session_overlap")
    minute_history = data.get("minute_history") or {}
    for interval in CSV_INDICATOR_INTERVALS:
        interval_hist = minute_history.get(interval) or {}
        for field in CSV_MINUTE_HISTORY_FIELDS:
            field_hist = interval_hist.get(field) or {}
            for suffix in CSV_MINUTE_HISTORY_SUFFIXES:
                out[f"{interval}_{field}_{suffix}"] = field_hist.get(suffix)
    oi_hist = minute_history.get("oi") or {}
    for suffix in CSV_MINUTE_HISTORY_SUFFIXES:
        out[f"oi_{suffix}"] = oi_hist.get(suffix)
    for field in MINUTE_HISTORY_TOP_LEVEL_PATHS:
        field_hist = minute_history.get(field) or {}
        for suffix in CSV_MINUTE_HISTORY_SUFFIXES:
            out[f"{field}_{suffix}"] = field_hist.get(suffix)
    liq = data.get("liquidations") or {}
    out["liq_connected"] = liq.get("connected")
    for minutes in (1, 5, 15):
        w = liq.get(f"m{minutes}") or {}
        for field in ("long_liq_count", "long_liq_qty", "long_liq_notional", "short_liq_count", "short_liq_qty", "short_liq_notional", "liq_imbalance"):
            out[f"liq_m{minutes}_{field}"] = w.get(field)
    return out


def _flatten_entries_for_csv(parsed_json_text: Optional[str]) -> Dict[str, Any]:
    """1st-stage (25%) E-RANG entry/TP/SL for both sides, as flat columns -
    the same values the Telegram/auto-trade triggers already key off of."""
    try:
        parsed = json.loads(parsed_json_text) if parsed_json_text else {}
    except (TypeError, ValueError):
        parsed = {}
    rows = _row_map_from_parsed(parsed) if parsed else {}
    def first(key: str) -> Any:
        vals = rows.get(key) or []
        return vals[0] if vals else None
    return {
        "long_entry1": first("long"), "long_tp1": first("tpLong"), "long_sl1": first("slLong"),
        "short_entry1": first("short"), "short_tp1": first("tpShort"), "short_sl1": first("slShort"),
    }


CSV_INDICATOR_FIELDS_WITH_HISTORY = CSV_INDICATOR_FIELDS + [
    f"{k}_{s}" for k in CSV_HISTORY_INDICATORS for s in CSV_HISTORY_SUFFIXES
] + [
    f"{k}_{s}" for k in CSV_MINUTE_HISTORY_FIELDS for s in CSV_MINUTE_HISTORY_SUFFIXES
]
CSV_INDICATOR_COLUMNS = [f"{iv}_{field}" for iv in CSV_INDICATOR_INTERVALS for field in CSV_INDICATOR_FIELDS_WITH_HISTORY]
CSV_MARKET_COLUMNS = [
    "order_book_bid_volume", "order_book_ask_volume", "order_book_imbalance",
    "order_book_imbalance_5", "order_book_imbalance_10", "order_book_imbalance_20",
    "order_book_spread", "order_book_spread_pct",
    "funding_rate_latest", "funding_change", "funding_extreme", "open_interest",
    "long_short_ratio", "long_account_pct", "short_account_pct",
    "top_account_long_short_ratio", "top_position_long_short_ratio",
    "oi_change_pct", "oi_price_classification",
    "hour_kst", "hour_utc", "weekday", "is_weekend",
    "session_asia", "session_europe", "session_us", "session_overlap",
] + [f"oi_{s}" for s in CSV_MINUTE_HISTORY_SUFFIXES] + [
    f"{field}_{s}" for field in MINUTE_HISTORY_TOP_LEVEL_PATHS for s in CSV_MINUTE_HISTORY_SUFFIXES
] + [
    "liq_connected",
] + [
    f"liq_m{m}_{f}" for m in (1, 5, 15)
    for f in ("long_liq_count", "long_liq_qty", "long_liq_notional", "short_liq_count", "short_liq_qty", "short_liq_notional", "liq_imbalance")
]
CSV_ENTRY_COLUMNS = ["long_entry1", "long_tp1", "long_sl1", "short_entry1", "short_tp1", "short_sl1"]


EXPECTED_BINANCE_TOP_KEYS = [
    "ticker_24h", "premium_index", "open_interest", "order_book",
    "funding_rate_history", "long_short_ratio", "top_trader_ratio",
    "oi_change", "indicators", "time_context", "minute_history", "liquidations",
]


def _data_quality(binance_json_text: Optional[str]) -> tuple:
    """data_quality_score (spec section 30): % of the expected top-level
    Binance data groups that are actually present and non-empty for this
    row, penalized further for any recorded per-call errors - lets a
    downstream model (or a human) filter out/weight down rows where a lot
    of context was missing (e.g. Binance had a bad moment) rather than
    silently treating a partially-empty row the same as a complete one."""
    try:
        data = json.loads(binance_json_text) if binance_json_text else {}
    except (TypeError, ValueError):
        data = {}
    if not data:
        return None, None
    present = sum(1 for k in EXPECTED_BINANCE_TOP_KEYS if data.get(k))
    error_count = len(data.get("errors") or {})
    missing_count = (len(EXPECTED_BINANCE_TOP_KEYS) - present) + error_count
    score = round(present / len(EXPECTED_BINANCE_TOP_KEYS) * 100, 1)
    return score, missing_count


def _minutes_since(start_iso: Optional[str], observed_at_iso: Optional[str]) -> Optional[float]:
    """minutes_since_long_start/short_start (spec section 29) - how long
    the CURRENT ON streak has been running as of this row, computed from
    the carried-forward long_start/short_start columns."""
    if not start_iso or not observed_at_iso:
        return None
    try:
        start_dt = datetime.fromisoformat(start_iso)
        obs_dt = datetime.fromisoformat(observed_at_iso)
        return round((obs_dt - start_dt).total_seconds() / 60, 2)
    except (TypeError, ValueError):
        return None


@app.get("/export.csv")
def export_csv() -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "id", "observed_at", "observed_at_kst", "http_status", "success", "current_price", "current_price_raw",
        "long_signal", "short_signal", "long_start", "long_end", "short_start", "short_end",
        "minutes_since_long_start", "minutes_since_short_start",
        "long_color", "short_color", "entry_message",
        # Precise, structured entry-basis evidence (v3.6) - exactly what
        # caused each side's color to be detected, as flat CSV columns so
        # this is directly usable for building a separate dataset without
        # having to parse it back out of the JSON blob columns below.
        "long_matched_selector", "long_matched_declaration", "long_ancestor_classes",
        "long_label_classes", "long_own_inline_background",
        "short_matched_selector", "short_matched_declaration", "short_ancestor_classes",
        "short_label_classes", "short_own_inline_background",
        # E-RANG's own 1st-stage entry/TP/SL, flattened (v3.52).
        *CSV_ENTRY_COLUMNS,
        # Binance order book / funding / open interest, flattened (v3.52).
        *CSV_MARKET_COLUMNS,
        # Full technical-indicator set per timeframe, flattened (v3.52) -
        # this is the main payload for building an independent long/short
        # model: EMA/RSI/ATR/MACD/Bollinger/Stochastic/ADX/CCI/VWAP/taker-flow
        # for each of 1m/5m/15m/1h/4h, one column per (interval, field) pair.
        *CSV_INDICATOR_COLUMNS,
        "data_quality_score", "missing_fields_count",
        "content_sha256", "error", "parsed_json", "binance_json",
    ])
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT id, observed_at, observed_at_kst, http_status, success, current_price, current_price_raw,
                   long_signal, short_signal, long_start, long_end, short_start, short_end,
                   long_color, short_color, entry_message,
                   long_matched_selector, long_matched_declaration, long_ancestor_classes,
                   long_label_classes, long_own_inline_background,
                   short_matched_selector, short_matched_declaration, short_ancestor_classes,
                   short_label_classes, short_own_inline_background,
                   content_sha256, error, parsed_json, binance_json
            FROM observations ORDER BY id ASC
            """
        )
        for row in cur:
            (rid, observed_at, observed_at_kst, http_status, success, current_price, current_price_raw,
             long_signal, short_signal, long_start, long_end, short_start, short_end,
             long_color, short_color, entry_message,
             long_sel, long_decl, long_anc, long_lbl, long_bg,
             short_sel, short_decl, short_anc, short_lbl, short_bg,
             sha, error, parsed_json_text, binance_json_text) = row
            entries = _flatten_entries_for_csv(parsed_json_text)
            market = _flatten_binance_for_csv(binance_json_text)
            dq_score, dq_missing = _data_quality(binance_json_text)
            writer.writerow([
                rid, observed_at, observed_at_kst, http_status, success, current_price, current_price_raw,
                long_signal, short_signal, long_start, long_end, short_start, short_end,
                _minutes_since(long_start, observed_at), _minutes_since(short_start, observed_at),
                long_color, short_color, entry_message,
                long_sel, long_decl, long_anc, long_lbl, long_bg,
                short_sel, short_decl, short_anc, short_lbl, short_bg,
                *[entries.get(c) for c in CSV_ENTRY_COLUMNS],
                *[market.get(c) for c in CSV_MARKET_COLUMNS],
                *[market.get(c) for c in CSV_INDICATOR_COLUMNS],
                dq_score, dq_missing,
                sha, error,
                parsed_json_text or "{}",
                binance_json_text or "{}",
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
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>🪙 뿌꾸의 코인세상</title>
<style>
:root{--bg:#07101f;--panel:#0e1b31;--panel2:#101f38;--line:#243b5f;--text:#f4f7ff;--muted:#8fa7c9;--blue:#38a5ff;--red:#ff5364;--green:#35e29a;--yellow:#ffc83d}*{box-sizing:border-box}html{overflow-x:hidden}body{margin:0;overflow-x:hidden;max-width:100vw;background:radial-gradient(circle at 20% 0,#102442 0,#07101f 45%);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1540px;margin:auto;padding:24px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.topTitle{min-width:0;flex:1 1 auto}.top h1{margin:0;font-size:30px;overflow-wrap:break-word}.actions{min-width:0}.sub,.muted{color:var(--muted)}.sub{margin-top:5px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#152540;color:#fff;padding:11px 15px;border-radius:11px;text-decoration:none;font-weight:800;cursor:pointer}.live{color:var(--green);font-weight:900}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,rgba(16,31,56,.98),rgba(10,24,44,.98));border:1px solid var(--line);border-radius:17px;padding:18px;box-shadow:0 14px 32px #0004;min-width:0}.s2{grid-column:span 2}.s3{grid-column:span 3}.s4{grid-column:span 4}.s6{grid-column:span 6}.s8{grid-column:span 8}.s12{grid-column:span 12}.label{font-size:13px;color:#a9bfdf;font-weight:800}.big{font-size:29px;font-weight:950;margin-top:7px}.hero{display:flex;align-items:center;gap:22px;min-height:110px}.heroSignal{font-size:42px;font-weight:1000}.short{color:var(--red)}.long{color:var(--blue)}.wait{color:var(--yellow)}.ok{color:var(--green)}h2{font-size:18px;margin:0 0 14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:680px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{background:#132947;color:#c7dcfa;font-size:12px}.rowlong.on td:first-child{font-weight:950;color:var(--blue)}.rowshort.on td:first-child{background:#ef3340;color:#fff;font-weight:950}.entryrow{display:grid;grid-template-columns:82px repeat(5,1fr);gap:8px;align-items:stretch;margin-bottom:10px}.sideLabel{display:flex;align-items:center;font-size:20px;font-weight:950}.entry{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:10px;min-width:0}.entry b{font-size:12px;color:#9fb8db;display:block}.entry strong{font-size:17px;display:block;margin-top:5px;white-space:nowrap}.dist{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.dist .entry strong{font-size:20px}.tabs{display:flex;gap:7px;margin:12px 0}.tab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer}.tab.active{background:#168cff;color:white}.metricTop{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metricTop6{display:grid;grid-template-columns:repeat(6,1fr);gap:9px}.metricTop7{display:grid;grid-template-columns:repeat(7,1fr);gap:9px}.metricTop8{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{background:#0a172b;border:1px solid var(--line);border-radius:11px;padding:12px}.metric b{display:block;color:#9fb8db;font-size:12px}.metric strong{display:block;font-size:19px;margin-top:6px}.indtable{min-width:0}.indtable td:nth-child(2){font-weight:800}.statusUp{color:var(--green)}.statusDown{color:var(--red)}.statusNeutral{color:#dbe7f8}.evidence{line-height:1.7}.evidence strong{font-size:18px}.foot{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:13px;gap:12px}.nowrap{white-space:nowrap}.clickrow{cursor:pointer}.clickrow:hover{background:#132947}.analysisGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.analysisBox{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:14px}.analysisBox h3{margin:0 0 10px;font-size:17px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{background:#102746;border:1px solid var(--line);border-radius:999px;padding:6px 9px;font-size:12px}.detailHead{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.hint{font-size:12px;color:var(--muted)}.reasonCell{text-align:left;white-space:normal;min-width:220px;max-width:320px}.profitCell{text-align:left;white-space:normal;min-width:170px}.profitCell div{margin-bottom:4px;font-weight:800}.profitCell div:last-child{margin-bottom:0}.profitCell .muted{font-weight:600;font-size:11px}.reasonMini{font-size:11px;line-height:1.45;margin-bottom:5px;padding:5px 7px;border-radius:7px;background:#0a172b;border:1px solid var(--line)}.reasonMini:last-child{margin-bottom:0}.reasonMini.long{color:#bcdcff;border-color:#2563eb55}.reasonMini.short{color:#ffd0d6;border-color:#ef334055}.reasonMini b{font-weight:900}.reasonList{margin:9px 0 0;padding-left:18px;font-size:12px;color:#c7dcfa;line-height:1.6}.reasonList li{margin-bottom:3px}.reasonSummary{background:#0a172b;border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:6px}.reasonSummary b{display:block;margin-bottom:6px;font-size:13px;color:#dbe7f8}.ptabs{display:flex;gap:7px;margin:12px 0}.ptab{flex:1;border:1px solid var(--line);background:#102746;color:#c8daf4;padding:9px;border-radius:9px;font-weight:850;cursor:pointer;text-align:center}.ptab.active{background:#168cff;color:white}.ppanel{display:none}.ppanel.active{display:block}.pside-long{color:var(--blue);background:rgba(56,165,255,.14);padding:2px 8px;border-radius:5px;font-size:12px;font-weight:800}.pside-short{color:var(--red);background:rgba(255,83,100,.14);padding:2px 8px;border-radius:5px;font-size:12px;font-weight:800}.ptable td.pnum{text-align:right}.ptable{min-width:0}.posAvatar{display:inline-block;width:20px;height:20px;border-radius:50%;background:#102746;margin-right:5px;object-fit:cover;vertical-align:middle;border:1px solid var(--line)}.posMini{font-size:12px;line-height:1.7;margin-top:7px}.posMini .prow{display:flex;justify-content:space-between;gap:8px}.posMini .prow b{font-weight:800}.tvChartBox{height:760px;border-radius:12px;overflow:hidden;resize:vertical;min-height:320px;max-height:1400px}
@media(max-width:1050px){.s2,.s3,.s4,.s6,.s8{grid-column:span 12}.metricTop{grid-template-columns:1fr 1fr}.metricTop6{grid-template-columns:repeat(3,1fr)}.metricTop7{grid-template-columns:repeat(4,1fr)}.metricTop8{grid-template-columns:repeat(4,1fr)}.two{grid-template-columns:1fr}.entryrow{grid-template-columns:70px repeat(5,130px);overflow-x:auto}.dist{grid-template-columns:repeat(5,140px);overflow-x:auto}.tvChartBox{height:520px}}@media(max-width:600px){.wrap{padding:12px}.card{padding:14px}.top{flex-direction:column}.metricTop{grid-template-columns:1fr 1fr}.metricTop6{grid-template-columns:1fr 1fr}.metricTop7{grid-template-columns:1fr 1fr}.metricTop8{grid-template-columns:1fr 1fr}.heroSignal{font-size:34px}.tvChartBox{height:400px}.actions{width:100%}.actions .btn{flex:1 1 auto;text-align:center}.metric b{font-size:11px}.metric strong{font-size:16px}}@media(max-width:380px){.top h1{font-size:24px}.metricTop6{grid-template-columns:1fr}.metricTop7{grid-template-columns:1fr}.metricTop8{grid-template-columns:1fr}.ptabs{flex-wrap:wrap}.ptab{flex:1 1 45%}}
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
:root{--bg:#030711;--panel:#0a1424;--panel2:#0d1a30;--line:#17415f;--text:#e4f6ff;--muted:#5e84a3;--blue:#22e3ff;--red:#ff2f6e;--green:#39ffa0;--yellow:#ffe14d}
html{scrollbar-color:var(--line) var(--bg)}
body{font-family:'Rajdhani',Inter,system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;
  background:radial-gradient(ellipse 900px 600px at 12% -10%,rgba(34,227,255,.09),transparent 55%),
    radial-gradient(ellipse 800px 600px at 92% 8%,rgba(255,47,110,.07),transparent 55%),
    radial-gradient(ellipse 900px 700px at 50% 115%,rgba(57,255,160,.05),transparent 55%),var(--bg)}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(34,227,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(34,227,255,.045) 1px,transparent 1px);
  background-size:42px 42px;mask-image:radial-gradient(ellipse 1200px 700px at 50% 0%,#000 0%,transparent 60%)}
.wrap{position:relative;z-index:1}
.top h1{font-weight:700;letter-spacing:.4px;text-shadow:0 0 22px rgba(34,227,255,.4)}
h2{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.3px}
.label{font-family:'Rajdhani',sans-serif;letter-spacing:.4px}
.big,.heroSignal,.metric strong,.entry strong{font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
.heroSignal.long{text-shadow:0 0 24px rgba(34,227,255,.55)}
.heroSignal.short{text-shadow:0 0 24px rgba(255,47,110,.55)}
.heroSignal.wait{text-shadow:0 0 20px rgba(255,225,77,.4)}
.card{border-color:var(--line);box-shadow:0 0 0 1px rgba(34,227,255,.06),0 18px 44px #0008;position:relative;transition:box-shadow .2s}
.card::before,.card::after{content:'';position:absolute;width:16px;height:16px;border:2px solid var(--blue);opacity:.65;pointer-events:none}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none;border-radius:17px 0 0 0}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none;border-radius:0 0 17px 0}
.btn{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.4px;background:rgba(23,65,95,.35);border-color:var(--line);transition:box-shadow .15s,border-color .15s,transform .1s}
.btn:hover{border-color:var(--blue);box-shadow:0 0 16px rgba(34,227,255,.35);transform:translateY(-1px)}
.live{position:relative;padding-left:16px}
.live::before{content:'';position:absolute;left:0;top:50%;width:8px;height:8px;margin-top:-4px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green);animation:livepulse 1.8s ease-in-out infinite}
@keyframes livepulse{0%,100%{opacity:1;box-shadow:0 0 6px var(--green)}50%{opacity:.55;box-shadow:0 0 14px var(--green)}}
th{background:rgba(23,65,95,.28);color:#bfe8ff;font-family:'Rajdhani',sans-serif;letter-spacing:.4px}
td{font-family:'JetBrains Mono',monospace;font-size:12.5px}
td:first-child{font-family:'Rajdhani',sans-serif;font-size:14px}
.rowshort.on td:first-child{background:rgba(255,47,110,.85);box-shadow:0 0 14px rgba(255,47,110,.4) inset}
.tab,.ptab{font-family:'Rajdhani',sans-serif;font-weight:700;letter-spacing:.3px;background:rgba(23,65,95,.25);transition:box-shadow .15s}
.tab.active,.ptab.active{background:var(--blue);color:#031018;box-shadow:0 0 18px rgba(34,227,255,.5)}
.metric,.entry{background:rgba(10,20,36,.7);border-color:var(--line);transition:border-color .15s}
.chip{background:rgba(23,65,95,.3);font-family:'JetBrains Mono',monospace;font-size:11px}
input,select{font-family:'JetBrains Mono',monospace}
#lwChartBox{border:1px solid var(--line);box-shadow:0 0 0 1px rgba(34,227,255,.06),0 0 30px rgba(34,227,255,.06) inset}
.ovBtn.active{background:var(--blue);color:#031018;box-shadow:0 0 12px rgba(34,227,255,.4)}
#lwOscBox{border:1px solid var(--line);border-radius:8px}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}::-webkit-scrollbar-thumb:hover{background:var(--blue)}
</style>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
</head><body><div class="wrap">
<div class="top"><div class="topTitle"><h1>🪙 뿌꾸의 코인세상</h1></div><div class="actions"><a class="btn" href="/export.csv">CSV 다운로드</a><button id="collect" class="btn">강제 수집</button><button id="telegramTest" class="btn">텔레그램 현재상태 발송</button><a class="btn" href="/settings">설정</a><a class="btn" href="/logout">로그아웃</a><span id="live" class="live">● 정상 수집 중</span><span id="lastSync" class="muted"></span><span id="lastTop" class="muted"></span></div></div>
<div id="updateBanner" style="display:none;background:linear-gradient(90deg,#1a3a6e,#0e1b31);border:1px solid #38a5ff88;border-radius:12px;padding:12px 16px;margin-bottom:16px;align-items:center;justify-content:space-between;gap:12px"><span>🎮 <b>새 버전이 나왔어요!</b> <span id="updateVersionText" class="muted"></span></span><a id="updateDownloadLink" class="btn" href="#" target="_blank" style="background:#38a5ff;color:#04101f">지금 다운로드</a></div>
<div class="grid">
<div class="card s4 hero"><div><div class="label">현재 판정</div><div id="heroSignal" class="heroSignal wait">WAIT</div></div><div><div id="signalBits" class="big" style="font-size:15px">LONG OFF / SHORT OFF</div><div class="muted">색상 신호를 기준으로 판정합니다.</div></div></div>
<div class="card s2"><div class="label">BTCUSDT 현재가</div><div id="price" class="big">-</div><div id="priceDelta" class="muted">Binance 실시간</div></div>
<div class="card s2"><div class="label">수집 상태</div><div id="collectState" class="big ok">정상</div><div id="counts" class="muted">-</div></div>
<div class="card s2"><div class="label">DB / 서버</div><div id="db" class="big ok" style="font-size:21px">-</div><div id="server" class="muted">-</div></div>
<div class="card s2"><div class="label"><img class="posAvatar" src="data:image/png;base64,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" alt="뿌꾸"> 현재 포지션</div><div id="posMini" class="posMini"><span class="muted">-</span></div></div>
<div class="card s6"><h2>진입가 <span class="muted">(현재 화면 기준)</span></h2><div class="tablewrap"><table><thead><tr><th>구분</th><th>진입 1<br>(25%)</th><th>진입 2<br>(40%)</th><th>진입 3<br>(60%)</th><th>진입 4<br>(100%)</th><th>진입 5<br>(예비)</th></tr></thead><tbody id="erangRows"></tbody></table></div></div>
<div class="card s6"><div class="detailHead"><h2>실시간 BTC 선물 차트 <span class="muted">(Binance BTCUSDT · LONG/SHORT 신호 마커 표시)</span></h2><div style="display:flex;gap:6px"><button type="button" class="btn chartSizeBtn" data-h="420" style="padding:6px 11px;font-size:12px">작게</button><button type="button" class="btn chartSizeBtn" data-h="760" style="padding:6px 11px;font-size:12px">보통</button><button type="button" class="btn chartSizeBtn" data-h="1100" style="padding:6px 11px;font-size:12px">크게</button></div></div>
<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">
  <button type="button" class="btn ovBtn" data-ov="ema" style="padding:5px 10px;font-size:11px">EMA</button>
  <button type="button" class="btn ovBtn" data-ov="bb" style="padding:5px 10px;font-size:11px">볼린저밴드</button>
  <button type="button" class="btn ovBtn" data-ov="vwap" style="padding:5px 10px;font-size:11px">VWAP</button>
  <button type="button" class="btn ovBtn" data-ov="ichimoku" style="padding:5px 10px;font-size:11px">일목구름</button>
  <button type="button" class="btn ovBtn" data-ov="supertrend" style="padding:5px 10px;font-size:11px">Supertrend</button>
  <span style="width:1px;background:var(--line);margin:2px 4px"></span>
  <select id="oscPick" class="btn" style="padding:5px 10px;font-size:11px;cursor:pointer">
    <option value="">보조지표 없음</option>
    <option value="rsi">RSI</option>
    <option value="macd">MACD</option>
    <option value="stoch">Stochastic</option>
    <option value="adx">ADX/DI</option>
    <option value="cci">CCI</option>
  </select>
</div>
<div id="lwChartBox" class="tvChartBox" style="width:100%"></div>
<div id="lwOscBox" style="width:100%;height:140px;margin-top:6px;display:none"></div>
<div id="lwClickInfo" class="hint" style="margin-top:8px">캔들을 클릭하면 그 시점 정보가 여기에 표시됩니다.</div>
</div>
<div class="card s12"><div class="detailHead"><h2>내 Bitget 계좌 <span class="muted">(통합계좌 · 실계좌 · 읽기 전용)</span></h2><span class="hint" id="bgHint"></span></div><div class="metricTop metricTop8"><div class="metric"><b>지금까지 수익PNL (계정 전체)</b><strong id="bgLifetimePnl">-</strong></div><div class="metric"><b>총 PNL (실시간 · 포지션 없으면 사라짐)</b><strong id="bgLivePnl">-</strong></div><div class="metric"><b>총자산 (Account Equity)</b><strong id="bgAccountEquity">-</strong></div><div class="metric"><b>USDT 잔고</b><strong id="bgEquity">-</strong></div><div class="metric"><b>미실현 PNL</b><strong id="bgPnl">-</strong></div><div class="metric"><b>유효자산 (Eff. Equity)</b><strong id="bgEffEquity">-</strong></div><div class="metric"><b>승률</b><strong id="bgWinRate">-</strong></div><div class="metric"><b>PNL (통합)</b><strong id="bgCombinedPnl">-</strong></div></div><div class="ptabs"><button class="ptab active" data-ptab="positions">현재 포지션</button><button class="ptab" data-ptab="fills">체결 내역</button><button class="ptab" data-ptab="orders">주문 내역</button></div><div id="ppanel-positions" class="ppanel active"><div class="muted">-</div></div><div id="ppanel-fills" class="ppanel"><div class="muted">-</div></div><div id="ppanel-orders" class="ppanel"><div class="muted">-</div></div><div class="foot"><span>ⓘ 가격/손익은 Bitget API 응답을 그대로 표시합니다. 승률/PNL(통합)은 청산(close) 체결의 실현손익 기준이며, 매매 판단 참고용입니다.</span><span id="bgUpdate"></span></div></div>
<div class="card s6"><h2>Binance 보조 지표 (BTCUSDT)</h2><div class="metricTop"><div class="metric"><b>현재가 (Last Price)</b><strong id="bLast">-</strong></div><div class="metric"><b>펀딩비 (Funding Rate)</b><strong id="funding">-</strong></div><div class="metric"><b>미결제약정 (Open Interest)</b><strong id="oi">-</strong></div><div class="metric"><b>24h 거래량</b><strong id="vol24">-</strong></div></div><div class="tabs"><button class="tab" data-tf="1m">1분</button><button class="tab" data-tf="5m">5분</button><button class="tab active" data-tf="15m">15분</button><button class="tab" data-tf="1h">1시간</button></div><div class="tablewrap"><table class="indtable"><thead><tr><th>지표</th><th>현재값</th><th>상태</th></tr></thead><tbody id="indicatorRows"></tbody></table></div><div class="foot"><span>ⓘ 최근 220개 캔들 데이터 기반 계산</span><span id="bUpdate"></span></div></div>
<div class="card s6"><h2>현재가와 주요 진입가 거리 <span class="muted">(Long 기준)</span></h2><div id="distanceLong" class="dist"></div><h2 style="margin-top:16px">현재가와 주요 진입가 거리 <span class="muted">(Short 기준)</span></h2><div id="distanceShort" class="dist"></div></div>
<div class="card s6"><h2>신호 판정 근거</h2><div id="evidence" class="evidence muted">-</div></div>
<div class="card s12"><div class="detailHead"><h2>LONG / SHORT ON 공통 보조지표 패턴</h2><span class="hint">※ 신호 ON 당시 Binance 지표의 상관 패턴이며 ON의 원인으로 확정한 값은 아닙니다.</span></div><div id="analysisSummary" class="analysisGrid"></div></div>
<div class="card s12"><div class="detailHead"><h2>선택한 수집 시점 보조지표</h2><span class="hint">아래 최근 수집 데이터 행을 클릭하면 당시 1m·5m·15m·1h 상태를 확인합니다.</span></div><div id="eventDetail" class="muted">수집 데이터 행을 선택하세요.</div></div>
<div class="card s12"><h2 id="historyToggle" style="cursor:pointer;user-select:none" title="클릭해서 펼치기/접기">최근 수집 데이터 <span class="muted" style="font-size:12px">(행 클릭 → 당시 보조지표)</span> <span id="historyChevron" class="muted">▶ 펼치기</span></h2><div id="historyBody" style="display:none"><div class="tablewrap" style="max-height:360px;overflow:auto"><table><thead><tr><th>ID</th><th>시간</th><th>BTC</th><th>LONG</th><th>SHORT</th><th>예상 수익<br><span class="hint">($5,000·5x·1차 TP)</span></th><th>판정 근거</th><th>15m RSI</th><th>15m MACD Hist</th><th>15m EMA20 관계</th><th>HTTP</th></tr></thead><tbody id="history"></tbody></table></div></div></div>
</div></div><script>
const $=id=>document.getElementById(id); let latest={},activeTF='15m',liveBinance={};
const n=v=>{let x=Number(v);return Number.isFinite(x)?x.toLocaleString('en-US',{maximumFractionDigits:4}):'-'}; const kst=v=>v?new Date(v).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'-';
const kstShort=v=>{if(!v)return '-';let d=new Date(v);if(Number.isNaN(d.getTime()))return '-';let p=new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(d),g=t=>p.find(x=>x.type===t)?.value||'';return `${g('month')}/${g('day')} ${g('hour')}:${g('minute')}`;};
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
function statusFor(name,val,ind){
  if(val==null||(typeof val==='number'&&Number.isNaN(val)))return '-';
  if(name.startsWith('RSI'))return val>=70?'과매수':val<=30?'과매도':'중립';
  if(name.startsWith('EMA')&&!name.includes('정배열')){let c=Number(ind.close);return c>val?'▲ 현재가 상회':'▼ 현재가 하회'}
  if(name==='MACD Histogram')return val>0?'▲ 양수 (상승 모멘텀)':val<0?'▼ 음수 (하락 모멘텀)':'중립';
  if(name==='MACD Line')return val>Number(ind.macd?.signal)?'▲ Signal 상회':'▼ Signal 하회';
  if(name==='EMA 정배열/역배열')return val==='bullish'?'▲ 정배열':val==='bearish'?'▼ 역배열':'혼조';
  if(name==='MACD 크로스'||name==='Stoch 크로스'||name==='DI 크로스')return val==='bullish'?'▲ 골든크로스':val==='bearish'?'▼ 데드크로스':'-';
  if(name.startsWith('Stochastic')||name.startsWith('StochRSI'))return val>=80?'과매수':val<=20?'과매도':'중립';
  if(name==='ADX')return val>=25?'강한 추세':val<20?'약한 추세(횡보)':'중간';
  if(name.startsWith('CCI'))return val>=100?'과매수':val<=-100?'과매도':'중립';
  if(name==='VWAP 거리%'||name.startsWith('수익률'))return val>0?'▲ 상회':val<0?'▼ 하회':'-';
  if(name==='Supertrend 방향')return val==='up'?'▲ 상승추세':val==='down'?'▼ 하락추세':'-';
  if(name==='일목 구름위치')return val==='above'?'▲ 구름 위':val==='below'?'▼ 구름 아래':val==='inside'?'구름 안':'-';
  if(name==='시장구조')return val==='uptrend'?'▲ 상승구조':val==='downtrend'?'▼ 하락구조':val==='mixed'?'혼조':'-';
  if(name==='BOS'||name==='CHOCH')return val==='bullish'?'▲ 상승':val==='bearish'?'▼ 하락':'-';
  if(name==='CVD 다이버전스')return val==='bullish_divergence'?'▲ 강세 다이버전스':val==='bearish_divergence'?'▼ 약세 다이버전스':'-';
  if(name==='거래량 급증')return val===true?'⚡ 급증':val===false?'평상':'-';
  if(name==='BB-Keltner 스퀴즈')return val===true?'스퀴즈 (변동성 축소)':val===false?'-':'-';
  return '-';
}
function clsStatus(s){return s.includes('▲')?'statusUp':s.includes('▼')?'statusDown':'statusNeutral'}
function renderIndicators(){
  let b=liveBinance&&Object.keys(liveBinance).length?liveBinance:(latest.binance||{}), ind=b.indicators?.[activeTF]||{},
      mac=ind.macd||{}, bol=ind.bollinger20||{}, kc=ind.keltner20||{}, bbEx=ind.bollinger_extra||{},
      stoch=ind.stochastic||{}, stochRsi=ind.stoch_rsi||{}, adx=ind.adx14||{}, vol20=ind.volume_ma20||{},
      volEx=ind.volume_extra||{}, ichi=ind.ichimoku||{}, ichiEx=ind.ichimoku_extra||{}, st=ind.supertrend||{},
      ms=ind.market_structure||{}, msB=ind.market_structure_breaks||{}, pivot=ind.pivot_levels||{},
      hilo=ind.recent_high_low||{}, ret=ind.price_returns||{}, cvd=ind.cvd||{}, emaAlign=ind.ema_alignment||{},
      taker=ind.taker_flow||{};
  let rows=[
    ['현재가 (Close)',ind.close],['고가 (High)',ind.high],['저가 (Low)',ind.low],['거래량 (Volume)',ind.volume],
    ['EMA 9',ind.ema9],['EMA 20',ind.ema20],['EMA 21',ind.ema21],['EMA 50',ind.ema50],['EMA 100',ind.ema100],['EMA 200',ind.ema200],
    ['EMA 정배열/역배열',emaAlign.alignment],['DEMA 20',ind.dema20],['HMA 20',ind.hma20],
    ['RSI 7',ind.rsi7],['RSI 14',ind.rsi14],['RSI 21',ind.rsi21],['ROC 12',ind.roc12],
    ['MACD Line',mac.macd],['MACD Signal',mac.signal],['MACD Histogram',mac.histogram],['MACD 크로스',ind.macd_cross],
    ['Stochastic %K',stoch.k],['Stochastic %D',stoch.d],['Stoch 크로스',ind.stoch_cross],
    ['StochRSI %K',stochRsi.k],['StochRSI %D',stochRsi.d],
    ['ADX',adx.adx],['+DI',adx.plus_di],['-DI',adx.minus_di],['DI 크로스',ind.di_cross],
    ['CCI 14',ind.cci14],['CCI 20',ind.cci20],
    ['ATR 7',ind.atr7],['ATR 14',ind.atr14],['ATR %',ind.atr_pct],
    ['Bollinger 상단',bol.upper],['Bollinger 중단',bol.middle],['Bollinger 하단',bol.lower],['BB 폭%',bbEx.bb_width_pct],['BB 포지션',bbEx.bb_position],
    ['Keltner 상단',kc.upper],['Keltner 하단',kc.lower],['BB-Keltner 스퀴즈',ind.bb_keltner_squeeze],
    ['VWAP',ind.vwap],['VWAP 거리%',ind.vwap_distance_pct],
    ['거래량 MA20',vol20.volume_ma],['거래량 비율',vol20.volume_ratio],['거래량 급증',volEx.volume_spike],
    ['체결매수비율',taker.taker_buy_ratio],
    ['CVD',cvd.cvd],['CVD 다이버전스',ind.cvd_divergence],
    ['일목 전환선',ichi.tenkan_sen],['일목 기준선',ichi.kijun_sen],['일목 구름위치',ichi.price_vs_cloud],['구름 두께',ichiEx.cloud_thickness],
    ['Supertrend 방향',st.direction],
    ['시장구조',ms.structure],['BOS',msB.bos],['CHOCH',msB.choch],
    ['피벗 고점',pivot.recent_pivot_high],['피벗 저점',pivot.recent_pivot_low],
    ['최근 20봉 최고',hilo.highest_high],['최근 20봉 최저',hilo.lowest_low],
    ['수익률 1봉',ret.return_1],['수익률 5봉',ret.return_5],['수익률 15봉',ret.return_15],
  ];
  $('indicatorRows').innerHTML=rows.map(([name,val])=>{
    let stTxt=statusFor(name,val,ind);
    let display=(val==null)?'-':(typeof val==='boolean')?(val?'예':'아니오'):(typeof val==='string')?val:n(val);
    return `<tr><td>${name}</td><td>${display}</td><td class="${clsStatus(stTxt)}">${stTxt}</td></tr>`;
  }).join('');
}
function renderEvidence(){
  let active=latest.short_signal?'SHORT':latest.long_signal?'LONG':'WAIT';
  let activeCls=active==='SHORT'?'short':active==='LONG'?'long':'wait';
  let b=liveBinance&&Object.keys(liveBinance).length?liveBinance:(latest.binance||{});
  let ind=b.indicators?.[activeTF]||{};
  let reasons=narrativeFor(ind);
  let list=reasons.length?`<ul class="reasonList">${reasons.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<div class="muted" style="margin-top:8px">현재 시간대 지표 데이터를 기다리는 중입니다.</div>';
  $('evidence').innerHTML=`<strong class="${activeCls}">● ${active==='WAIT'?'활성 신호 없음':active+' 활성 상태'}</strong>`+
    `<div class="hint" style="margin-top:6px">${activeTF} 기준 보조지표로 본 현재 시장 상태 (참고용 · 신호 자체는 원본 판정 기준을 따릅니다)</div>${list}`;
}
const BG_PRIORITY_KEYS=['symbol','posSide','avgPrice','markPrice','breakEvenPrice','leverage','holdSize','positionValue','unrealisedPnl','curRealisedPnl','liqPrice','positionBalance','marginMode','side','tradeSide','qty','avgEntryPrice','execPrice','execQty','execValue','execPnl','feeDetail','createdTime','orderId'];
const BG_PNL_KEYS=['execpnl','unrealisedpnl','unrealizedpl','currealisedpnl','pnl','profit'];
// Fields that aren't useful for a personal monitoring dashboard (always 0,
// redundant with another shown column, or too low-level) are hidden
// entirely rather than translated.
const BG_HIDE_KEYS=['userId','marginCoin','posMode','cashDividend','category','closeFeeTotal','frozen','available'];
const BG_LABELS={
  symbol:'종목', posSide:'방향', side:'방향', tradeSide:'구분',
  avgPrice:'평균단가', avgEntryPrice:'평균단가', markPrice:'마크가', breakEvenPrice:'손익분기가',
  leverage:'레버리지', holdSize:'수량', qty:'수량', execQty:'체결수량',
  unrealisedPnl:'미실현손익', execPnl:'실현손익', curRealisedPnl:'실현손익(누적)',
  positionBalance:'마진', positionValue:'포지션가치', execValue:'체결금액',
  marginMode:'마진모드', liqPrice:'청산가',
  createdTime:'시간', orderId:'주문번호', execPrice:'체결가', feeDetail:'수수료',
};
function bgLabel(key){return BG_LABELS[key]||key}
function bgLooksNumeric(v){return v!==''&&v!==null&&v!==undefined&&!Number.isNaN(Number(v))}
function marginBasisOf(row){
  // Prefer positionValue (notional) / leverage - both fields are documented
  // consistently across exchanges, so this is guaranteed to reflect
  // leverage correctly. Only fall back to positionBalance (whose exact
  // semantics can vary by exchange - it may already be leveraged margin,
  // or something else) if positionValue/leverage aren't both available.
  let positionValue=Number(row.positionValue),leverage=Number(row.leverage);
  if(Number.isFinite(positionValue)&&Number.isFinite(leverage)&&leverage>0){
    return positionValue/leverage;
  }
  let margin=Number(row.positionBalance);
  return Number.isFinite(margin)?margin:NaN;
}
function renderBitgetTable(container,rows,emptyMsg){
  if(!rows||rows.length===0){container.innerHTML=`<div class="muted" style="padding:20px 4px">${emptyMsg}</div>`;return}
  let allKeys=Object.keys(rows[0]).filter(k=>!BG_HIDE_KEYS.includes(k));
  let ordered=[...BG_PRIORITY_KEYS.filter(k=>allKeys.includes(k)),...allKeys.filter(k=>!BG_PRIORITY_KEYS.includes(k))];
  let out='<div class="tablewrap"><table class="ptable"><thead><tr>'+ordered.map(k=>`<th>${esc(bgLabel(k))}</th>`).join('')+'</tr></thead><tbody>';
  rows.forEach(row=>{
    out+='<tr>';
    ordered.forEach(k=>{
      let v=row[k];
      let isPnl=BG_PNL_KEYS.some(p=>k.toLowerCase().includes(p));
      if(k==='side'||k==='tradeSide'||k==='posSide'){
        let isLong=String(v).toLowerCase().includes('buy')||String(v).toLowerCase()==='open'||String(v).toLowerCase().includes('long');
        out+=`<td><span class="${isLong?'pside-long':'pside-short'}">${esc(v)}</span></td>`;
      } else if(k==='createdTime'){
        out+=`<td class="nowrap">${esc(kstShort(Number(v)))}</td>`;
      } else if(k==='orderId'){
        out+=`<td>${esc(v??'')}</td>`;
      } else if(k==='unrealisedPnl'||k==='execPnl'){
        let pnlNum=Number(v),margin=marginBasisOf(row);
        let cls=Number.isFinite(pnlNum)?(pnlNum>=0?'statusUp':'statusDown'):'';
        let pctText='';
        if(Number.isFinite(pnlNum)&&Number.isFinite(margin)&&margin!==0){
          let pct=pnlNum/margin*100;
          pctText=` <span class="muted">(${pct>=0?'+':''}${pct.toFixed(2)}%)</span>`;
        }
        out+=`<td class="pnum ${cls}">${n(v)}${pctText}</td>`;
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
      let pnl=Number(p.unrealisedPnl),margin=marginBasisOf(p);
      let pctTxt='';
      if(Number.isFinite(pnl)&&Number.isFinite(margin)&&margin!==0){
        let pct=pnl/margin*100;
        pctTxt=` (${pct>=0?'+':''}${pct.toFixed(1)}%)`;
      }
      let pnlTxt=Number.isFinite(pnl)?(pnl>=0?'+':'')+n(pnl)+pctTxt:'-';
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
  let equity=Number(bg.total_equity);
  let lifetimeEl=$('bgLifetimePnl');
  if(Number.isFinite(combined)){
    let lifetimeTxt=(combined>=0?'+':'')+n(combined);
    if(Number.isFinite(equity)&&equity!==0){
      let lifetimePct=combined/equity*100;
      lifetimeTxt+=` (${lifetimePct>=0?'+':''}${lifetimePct.toFixed(2)}%)`;
    }
    lifetimeEl.textContent=lifetimeTxt;
  } else {
    lifetimeEl.textContent='-';
  }
  lifetimeEl.className='num '+(Number.isFinite(combined)?(combined>=0?'statusUp':'statusDown'):'');
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
  let m=ind.macd||{}, b=ind.bollinger20||{}, stoch=ind.stochastic||{}, adx=ind.adx14||{}, ichi=ind.ichimoku||{},
      st=ind.supertrend||{}, ms=ind.market_structure||{}, cvd=ind.cvd||{}, emaAlign=ind.ema_alignment||{}, bullets=[];
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
  if(emaAlign.alignment&&emaAlign.alignment!=='mixed')bullets.push(`EMA 전체 ${emaAlign.alignment==='bullish'?'정배열 · 추세적 상승 흐름':'역배열 · 추세적 하락 흐름'}`);
  if(Number.isFinite(close)&&Number.isFinite(b.upper)&&Number.isFinite(b.lower)&&(b.upper-b.lower)>0){
    let pos=(close-b.lower)/(b.upper-b.lower);
    if(pos<=0.15)bullets.push('볼린저밴드 하단 근접 · 되돌림 반등 구간 가능성');
    else if(pos>=0.85)bullets.push('볼린저밴드 상단 근접 · 과열·되돌림 하락 구간 가능성');
  }
  let k=Number(stoch.k);
  if(Number.isFinite(k)){
    if(k<=20)bullets.push(`Stochastic %K ${n(k)} · 과매도권`);
    else if(k>=80)bullets.push(`Stochastic %K ${n(k)} · 과매수권`);
  }
  let adxVal=Number(adx.adx);
  if(Number.isFinite(adxVal)&&adxVal>=25){
    let plusDi=Number(adx.plus_di), minusDi=Number(adx.minus_di);
    bullets.push(`ADX ${n(adxVal)} · 강한 추세 (${plusDi>minusDi?'+DI 우위 · 상승추세':'-DI 우위 · 하락추세'})`);
  }
  if(ichi.price_vs_cloud==='above')bullets.push('일목균형표 구름 위 · 강세 구간');
  else if(ichi.price_vs_cloud==='below')bullets.push('일목균형표 구름 아래 · 약세 구간');
  if(st.direction)bullets.push(`Supertrend ${st.direction==='up'?'상승추세':'하락추세'} 전환 상태`);
  if(ms.structure==='uptrend')bullets.push('시장구조 HH/HL · 상승 구조');
  else if(ms.structure==='downtrend')bullets.push('시장구조 LH/LL · 하락 구조');
  if(ind.cvd_divergence==='bearish_divergence')bullets.push('CVD 약세 다이버전스 · 가격 상승 대비 매수 체결 약화');
  else if(ind.cvd_divergence==='bullish_divergence')bullets.push('CVD 강세 다이버전스 · 가격 하락 대비 매도 체결 약화');
  return bullets;
}
function renderEventDetail(r){if(!r)return;let sig=r.short_signal&&!r.long_signal?'SHORT':r.long_signal&&!r.short_signal?'LONG':r.short_signal&&r.long_signal?'BOTH':'WAIT';let cards=['1m','5m','15m','1h'].map(tf=>{let i=eventTF(r,tf),m=i.macd||{},b=i.bollinger20||{},reasons=narrativeFor(i);return `<div class="analysisBox"><h3>${tf} <span class="${sig==='SHORT'?'short':sig==='LONG'?'long':'wait'}">${sig}</span></h3><div class="chips"><span class="chip">RSI ${n(i.rsi14)}</span><span class="chip">EMA20 ${n(i.ema20)}</span><span class="chip">EMA50 ${n(i.ema50)}</span><span class="chip">EMA200 ${n(i.ema200)}</span><span class="chip">MACD Hist ${n(m.histogram)}</span><span class="chip">ATR ${n(i.atr14)}</span><span class="chip">BB 상 ${n(b.upper)}</span><span class="chip">BB 중 ${n(b.middle)}</span><span class="chip">BB 하 ${n(b.lower)}</span></div>${reasons.length?`<ul class="reasonList">${reasons.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`}).join('');let erangReasons=[r.long_signal?sigReason(r,'long'):'',r.short_signal?sigReason(r,'short'):''].filter(Boolean);let erangBlock=(r.long_signal||r.short_signal)?`<div class="reasonSummary"><b>실제 판정 근거 (원본 신호)</b>${r.long_signal?`<div class="reasonMini long"><b>LONG</b> ${esc(sigReason(r,'long'))}</div>`:''}${r.short_signal?`<div class="reasonMini short"><b>SHORT</b> ${esc(sigReason(r,'short'))}</div>`:''}</div>`:'';$('eventDetail').innerHTML=`<div style="margin-bottom:12px"><strong>ID ${r.id} · ${kst(r.observed_at)} · BTC ${n(r.current_price||r.current_price_raw)}</strong> · Funding ${r.binance?.premium_index?.lastFundingRate??'-'} · OI ${n(r.binance?.open_interest?.openInterest)}</div>${erangBlock}<div class="hint" style="margin:10px 0">※ 아래는 이 시점의 Binance 보조지표 상태를 정리한 참고용 관측입니다. 실제 ON 판정은 위 원본 신호 기준이며, 아래 지표 조합이 ON의 확정 원인이라는 뜻은 아닙니다.</div><div class="analysisGrid">${cards}</div>`}
function renderAnalysis(a){let sm=a?.summary||{};$('analysisSummary').innerHTML=['LONG','SHORT'].map(side=>{let g=sm[side]||{},t=g.timeframes?.['15m']||{};return `<div class="analysisBox"><h3 class="${side==='LONG'?'long':'short'}">${side} ON · ${g.count||0}건</h3><div class="chips"><span class="chip">15m 평균 RSI ${n(t.avg_rsi14)}</span><span class="chip">15m 평균 MACD Hist ${n(t.avg_macd_histogram)}</span><span class="chip">MACD Hist 양수 ${t.macd_hist_positive_pct??'-'}%</span><span class="chip">현재가 &gt; EMA20 ${t.price_above_ema20_pct??'-'}%</span><span class="chip">평균 ATR ${n(t.avg_atr14)}</span><span class="chip">평균 Funding ${n(g.avg_funding_rate)}</span></div><div class="hint" style="margin-top:10px">1m/5m/15m/1h 상세는 ON 발생 행을 클릭해서 확인</div></div>`}).join('')}
async function refresh(){try{let [sr,hr,ar]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/history?limit=80',{cache:'no-store'}),fetch('/api/signal-analysis?limit=200',{cache:'no-store'})]),s=await sr.json(),h=await hr.json(),a=await ar.json(),db=s.db||{};renderAnalysis(a);latest=db.latest||{};let sig=latest.short_signal&&!latest.long_signal?'SHORT':latest.long_signal&&!latest.short_signal?'LONG':latest.short_signal&&latest.long_signal?'BOTH':'WAIT';$('heroSignal').textContent=sig;$('heroSignal').className='heroSignal '+(sig==='SHORT'?'short':sig==='LONG'?'long':'wait');$('signalBits').textContent=`LONG ${latest.long_signal?'ON':'OFF'} / SHORT ${latest.short_signal?'ON':'OFF'}`;let lp=s.live_price||{},livePriceNum=Number(lp.price);tickLwChart(livePriceNum);$('price').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?n(livePriceNum):n(latest.current_price||latest.current_price_raw);$('priceDelta').textContent=Number.isFinite(livePriceNum)&&livePriceNum>0?('Binance 실시간 · '+kst(lp.updated_at)):'실시간가 대기중';$('collectState').textContent=latest.success?'정상':'오류';$('counts').textContent=`성공 ${n(db.successful||0)} / 실패 ${n(db.failed||0)}`;$('db').textContent=db.database_ok?'DB 정상':'DB 오류';$('server').textContent=`collector ${(s.collector||{}).running?'running':'idle'}`;$('lastTop').textContent='마지막 수집: '+kst(latest.observed_at);renderErang(latest.parsed||{},livePriceNum);let lb=s.live_binance?.snapshot||{};liveBinance=Object.keys(lb).length?lb:(latest.binance||{});let b=liveBinance;$('bLast').textContent=n(b.ticker_24h?.lastPrice);$('funding').textContent=b.premium_index?.lastFundingRate??'-';$('oi').textContent=n(b.open_interest?.openInterest);$('vol24').textContent=n(b.ticker_24h?.volume);$('bUpdate').textContent='업데이트: '+kst(s.live_binance?.updated_at||latest.observed_at);renderIndicators();renderEvidence();renderBitget(s.bitget);$('history').innerHTML=(h.items||[]).map((r,idx)=>{let i=eventTF(r,'15m'),mh=i.macd?.histogram,rel=Number(i.close)>Number(i.ema20)?'상회':Number(i.close)<Number(i.ema20)?'하회':'-';return `<tr class="clickrow" data-idx="${idx}"><td>${r.id}</td><td>${kst(r.observed_at)}</td><td>${n(r.current_price||r.current_price_raw)}</td><td class="${r.long_signal?'long':''}">${r.long_signal?'ON':'OFF'}</td><td class="${r.short_signal?'short':''}">${r.short_signal?'ON':'OFF'}</td><td class="profitCell">${expectedProfitCell(r)}</td><td class="reasonCell">${historyReasonCell(r)}</td><td>${n(i.rsi14)}</td><td class="${Number(mh)>=0?'statusUp':'statusDown'}">${n(mh)}</td><td>${rel}</td><td>${r.http_status||'-'}</td></tr>`}).join('');document.querySelectorAll('.clickrow').forEach(tr=>tr.onclick=()=>renderEventDetail((h.items||[])[Number(tr.dataset.idx)]));let firstOn=(h.items||[]).find(r=>r.long_signal||r.short_signal);if(firstOn)renderEventDetail(firstOn);$('live').textContent='● 실시간 동기화 중';$('live').className='live';lastSyncAt=Date.now();$('lastSync').textContent='방금 갱신'}catch(e){$('live').textContent='● UI 오류 (재시도 중)';$('live').className='short'}}
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{document.querySelectorAll('.tab').forEach(y=>y.classList.remove('active'));x.classList.add('active');activeTF=x.dataset.tf;renderIndicators()});$('collect').onclick=async()=>{await fetch('/api/collect-now',{method:'POST',cache:'no-store'});refresh()};
$('telegramTest').onclick=async()=>{let b=$('telegramTest'),orig=b.textContent;b.disabled=true;b.textContent='발송 중...';try{let r=await fetch('/api/telegram-test',{method:'POST',cache:'no-store'});let j=await r.json();alert(j.ok?('✅ 텔레그램 발송 완료'+(j.previewed_side?` (미리보기: ${j.previewed_side.toUpperCase()})`:' (신호 없음, 안내 메시지)')):('❌ 발송 실패: '+(j.error||'알 수 없는 오류')))}catch(e){alert('❌ 요청 실패: '+e)}finally{b.disabled=false;b.textContent=orig}};
let historyOpen=false;
$('historyToggle').onclick=()=>{historyOpen=!historyOpen;$('historyBody').style.display=historyOpen?'block':'none';$('historyChevron').textContent=historyOpen?'▼ 접기':'▶ 펼치기'};
let lastSyncAt=Date.now();
setInterval(()=>{let s=Math.max(0,Math.round((Date.now()-lastSyncAt)/1000));$('lastSync').textContent=s<=1?'방금 갱신':s+'초 전 갱신'},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
let lwChart=null,lwCandleSeries=null;
function initLwChart(){
  const box=document.getElementById('lwChartBox');
  if(!box||typeof LightweightCharts==='undefined')return false;
  lwChart=LightweightCharts.createChart(box,{
    layout:{background:{color:'transparent'},textColor:'#c7dcfa'},
    grid:{vertLines:{color:'rgba(36,59,95,0.35)'},horzLines:{color:'rgba(36,59,95,0.35)'}},
    timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#243b5f'},
    rightPriceScale:{borderColor:'#243b5f'},
    crosshair:{mode:0},
  });
  lwCandleSeries=lwChart.addCandlestickSeries({
    upColor:'#39ffa0',downColor:'#ff2f6e',borderVisible:false,
    wickUpColor:'#39ffa0',wickDownColor:'#ff2f6e',
  });
  new ResizeObserver(entries=>{
    for(const entry of entries){
      if(lwChart)lwChart.resize(entry.contentRect.width,entry.contentRect.height);
    }
  }).observe(box);
  setupLwClickHandler();
  return true;
}
let lwLastCandle=null,lwChartInterval='15m';
const LW_INTERVAL_SECONDS={'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400};
async function fetchLwKlines(interval){
  try{
    // Routed through our own backend (not the browser calling Binance
    // directly) so this works regardless of the visitor's own network/
    // region - the server already talks to Binance reliably for every
    // other indicator on this page. Times come back already shifted to
    // KST so the chart's axis reads Korean wall-clock time.
    let r=await fetch(`/api/chart-klines?interval=${interval}&limit=200`,{cache:'no-store'});
    let j=await r.json();
    return j.candles||[];
  }catch(e){return [];}
}
async function fetchChartMarkers(){
  try{
    let r=await fetch('/api/chart-markers?days=14',{cache:'no-store'});
    let j=await r.json();
    return j.markers||[];
  }catch(e){return [];}
}
async function refreshLwChart(){
  if(!lwChart){if(!initLwChart())return;}
  let [klines,markers]=await Promise.all([fetchLwKlines(lwChartInterval),fetchChartMarkers()]);
  if(klines.length){
    lwCandleSeries.setData(klines);
    lwLastCandle=klines[klines.length-1];
    lwKlinesCache=klines;
    redrawActiveOverlays();
    if(lwOscKind)drawOscillator(lwOscKind,klines);
  }
  let markerObjs=markers.map(m=>({
    time:Math.floor(new Date(m.time).getTime()/1000)+9*3600,
    position:m.side==='long'?'belowBar':'aboveBar',
    color:m.side==='long'?'#39ffa0':'#ff2f6e',
    shape:m.side==='long'?'arrowUp':'arrowDown',
    text:m.side==='long'?'LONG':'SHORT',
  })).sort((a,b)=>a.time-b.time);
  lwCandleSeries.setMarkers(markerObjs);
}
function tickLwChart(livePrice){
  // Ticks the LAST candle on every live price update (every ~1s, reusing
  // the price this page already polls for the rest of the dashboard) so
  // the chart visibly moves in real time between the periodic full
  // refreshes above, instead of only updating once every 30s.
  if(!lwChart||!lwCandleSeries||!lwLastCandle||!livePrice)return;
  const price=Number(livePrice);
  if(!Number.isFinite(price))return;
  const nowBucket=Math.floor((Date.now()/1000+9*3600)/LW_INTERVAL_SECONDS[lwChartInterval])*LW_INTERVAL_SECONDS[lwChartInterval];
  if(nowBucket>lwLastCandle.time){
    // a new candle period has started - open a fresh bar at the live price
    lwLastCandle={time:nowBucket,open:price,high:price,low:price,close:price};
  } else {
    lwLastCandle={...lwLastCandle,high:Math.max(lwLastCandle.high,price),low:Math.min(lwLastCandle.low,price),close:price};
  }
  lwCandleSeries.update(lwLastCandle);
}
// ---------------------------------------------------------------------
// Client-side indicator math for chart overlays/oscillators - computed
// in the browser from the same kline data already fetched for the
// candles, so toggling an indicator on/off is instant (no extra network
// round-trip). These mirror the formulas in indicators.py but are a
// separate implementation purely for chart drawing - small floating-
// point differences from the backend's own numbers are expected and
// don't matter for a visual overlay.
// ---------------------------------------------------------------------
function calcEMA(closes,period){
  let out=new Array(closes.length).fill(null);
  if(closes.length<period)return out;
  let k=2/(period+1), sum=0;
  for(let i=0;i<period;i++)sum+=closes[i];
  let prev=sum/period; out[period-1]=prev;
  for(let i=period;i<closes.length;i++){prev=closes[i]*k+prev*(1-k);out[i]=prev;}
  return out;
}
function calcBB(closes,period,mult){
  let mid=new Array(closes.length).fill(null),up=new Array(closes.length).fill(null),low=new Array(closes.length).fill(null);
  for(let i=period-1;i<closes.length;i++){
    let w=closes.slice(i-period+1,i+1),mean=w.reduce((a,b)=>a+b,0)/period;
    let variance=w.reduce((a,b)=>a+(b-mean)*(b-mean),0)/period,std=Math.sqrt(variance);
    mid[i]=mean;up[i]=mean+mult*std;low[i]=mean-mult*std;
  }
  return {mid,up,low};
}
function calcVWAP(klines){
  let out=new Array(klines.length).fill(null),cumPV=0,cumVol=0;
  for(let i=0;i<klines.length;i++){
    let tp=(klines[i].high+klines[i].low+klines[i].close)/3,vol=klines[i].volume||0;
    cumPV+=tp*vol;cumVol+=vol;out[i]=cumVol?cumPV/cumVol:null;
  }
  return out;
}
function calcRSI(closes,period){
  let out=new Array(closes.length).fill(null);
  if(closes.length<=period)return out;
  let gains=0,losses=0;
  for(let i=1;i<=period;i++){let d=closes[i]-closes[i-1];if(d>0)gains+=d;else losses-=d;}
  let avgGain=gains/period,avgLoss=losses/period;
  out[period]=avgLoss===0?100:100-100/(1+avgGain/avgLoss);
  for(let i=period+1;i<closes.length;i++){
    let d=closes[i]-closes[i-1],g=d>0?d:0,l=d<0?-d:0;
    avgGain=(avgGain*(period-1)+g)/period;avgLoss=(avgLoss*(period-1)+l)/period;
    out[i]=avgLoss===0?100:100-100/(1+avgGain/avgLoss);
  }
  return out;
}
function calcMACD(closes){
  let e12=calcEMA(closes,12),e26=calcEMA(closes,26);
  let macdLine=closes.map((_,i)=>(e12[i]!=null&&e26[i]!=null)?e12[i]-e26[i]:null);
  let startIdx=macdLine.findIndex(v=>v!=null),signal=new Array(closes.length).fill(null);
  if(startIdx>=0){
    let sub=macdLine.slice(startIdx),subEma=calcEMA(sub,9);
    for(let i=0;i<subEma.length;i++)signal[startIdx+i]=subEma[i];
  }
  let hist=closes.map((_,i)=>(macdLine[i]!=null&&signal[i]!=null)?macdLine[i]-signal[i]:null);
  return {macdLine,signal,hist};
}
function calcStochastic(klines,period,dPeriod){
  let k=new Array(klines.length).fill(null);
  for(let i=period-1;i<klines.length;i++){
    let w=klines.slice(i-period+1,i+1),hh=Math.max(...w.map(c=>c.high)),ll=Math.min(...w.map(c=>c.low));
    k[i]=hh===ll?50:100*(klines[i].close-ll)/(hh-ll);
  }
  let d=new Array(klines.length).fill(null);
  for(let i=period-1+dPeriod-1;i<klines.length;i++){
    let w=k.slice(i-dPeriod+1,i+1);
    if(w.every(v=>v!=null))d[i]=w.reduce((a,b)=>a+b,0)/dPeriod;
  }
  return {k,d};
}
function calcADX(klines,period){
  let n=klines.length,plusDM=new Array(n).fill(0),minusDM=new Array(n).fill(0),tr=new Array(n).fill(0);
  for(let i=1;i<n;i++){
    let up=klines[i].high-klines[i-1].high,down=klines[i-1].low-klines[i].low;
    plusDM[i]=(up>down&&up>0)?up:0;minusDM[i]=(down>up&&down>0)?down:0;
    tr[i]=Math.max(klines[i].high-klines[i].low,Math.abs(klines[i].high-klines[i-1].close),Math.abs(klines[i].low-klines[i-1].close));
  }
  let adx=new Array(n).fill(null),plusDI=new Array(n).fill(null),minusDI=new Array(n).fill(null);
  if(n<period*2+1)return {adx,plusDI,minusDI};
  let smTR=tr.slice(1,period+1).reduce((a,b)=>a+b,0),smP=plusDM.slice(1,period+1).reduce((a,b)=>a+b,0),smM=minusDM.slice(1,period+1).reduce((a,b)=>a+b,0);
  let dxArr=[];
  for(let i=period;i<n;i++){
    if(i>period){smTR=smTR-smTR/period+tr[i];smP=smP-smP/period+plusDM[i];smM=smM-smM/period+minusDM[i];}
    let pdi=smTR?100*smP/smTR:0,mdi=smTR?100*smM/smTR:0;
    plusDI[i]=pdi;minusDI[i]=mdi;
    let s=pdi+mdi;dxArr.push(s?100*Math.abs(pdi-mdi)/s:0);
  }
  if(dxArr.length>=period){
    let a=dxArr.slice(0,period).reduce((a,b)=>a+b,0)/period,idx=period*2-1;
    if(idx<n)adx[idx]=a;
    for(let j=period;j<dxArr.length;j++){a=(a*(period-1)+dxArr[j])/period;idx++;if(idx<n)adx[idx]=a;}
  }
  return {adx,plusDI,minusDI};
}
function calcCCI(klines,period){
  let out=new Array(klines.length).fill(null),tp=klines.map(c=>(c.high+c.low+c.close)/3);
  for(let i=period-1;i<klines.length;i++){
    let w=tp.slice(i-period+1,i+1),mean=w.reduce((a,b)=>a+b,0)/period;
    let md=w.reduce((a,b)=>a+Math.abs(b-mean),0)/period;
    out[i]=md?(tp[i]-mean)/(0.015*md):0;
  }
  return out;
}
function calcIchimoku(klines){
  let n=klines.length;
  function mid(period,i){
    if(i<period-1)return null;
    let w=klines.slice(i-period+1,i+1);
    return (Math.max(...w.map(c=>c.high))+Math.min(...w.map(c=>c.low)))/2;
  }
  let tenkan=[],kijun=[],senkouA=[],senkouB=[];
  for(let i=0;i<n;i++){
    let t=mid(9,i),k=mid(26,i),b=mid(52,i);
    tenkan.push(t);kijun.push(k);senkouA.push((t!=null&&k!=null)?(t+k)/2:null);senkouB.push(b);
  }
  return {tenkan,kijun,senkouA,senkouB};
}
function calcSupertrend(klines,period,mult){
  let n=klines.length,tr=new Array(n).fill(0);
  for(let i=1;i<n;i++)tr[i]=Math.max(klines[i].high-klines[i].low,Math.abs(klines[i].high-klines[i-1].close),Math.abs(klines[i].low-klines[i-1].close));
  let atr=new Array(n).fill(null);
  if(n>period){
    let s=tr.slice(1,period+1).reduce((a,b)=>a+b,0)/period;atr[period]=s;
    for(let i=period+1;i<n;i++){s=(s*(period-1)+tr[i])/period;atr[i]=s;}
  }
  let finalUpper=new Array(n).fill(null),finalLower=new Array(n).fill(null),trend=new Array(n).fill(null),dir=new Array(n).fill(null);
  for(let i=period;i<n;i++){
    if(atr[i]==null)continue;
    let hl2=(klines[i].high+klines[i].low)/2,bu=hl2+mult*atr[i],bl=hl2-mult*atr[i];
    if(finalUpper[i-1]==null){finalUpper[i]=bu;finalLower[i]=bl;dir[i]='down';trend[i]=bu;continue;}
    let pc=klines[i-1].close;
    finalUpper[i]=(bu<finalUpper[i-1]||pc>finalUpper[i-1])?bu:finalUpper[i-1];
    finalLower[i]=(bl>finalLower[i-1]||pc<finalLower[i-1])?bl:finalLower[i-1];
    let close=klines[i].close;
    if(dir[i-1]==='down'){
      if(close>finalUpper[i]){dir[i]='up';trend[i]=finalLower[i];}else{dir[i]='down';trend[i]=finalUpper[i];}
    } else {
      if(close<finalLower[i]){dir[i]='down';trend[i]=finalUpper[i];}else{dir[i]='up';trend[i]=finalLower[i];}
    }
  }
  return trend;
}
// --- Overlay (drawn on main price chart, same scale) management ---
let lwOverlaySeries={}, lwActiveOverlays=new Set(), lwKlinesCache=[];
function seriesOf(name){let s=lwOverlaySeries[name];return s?(Array.isArray(s)?s:[s]):[];}
function clearOverlay(name){seriesOf(name).forEach(s=>{try{lwChart.removeSeries(s)}catch(e){}});delete lwOverlaySeries[name];}
function toPoints(klines,arr){return klines.map((c,i)=>arr[i]!=null?{time:c.time,value:arr[i]}:null).filter(Boolean);}
function drawOverlay(name,klines){
  clearOverlay(name);
  let closes=klines.map(c=>c.close);
  const line=(color,width,style)=>lwChart.addLineSeries({color,lineWidth:width||1,priceLineVisible:false,lastValueVisible:false,lineStyle:style||0});
  if(name==='ema'){
    lwOverlaySeries.ema=[
      (()=>{let s=line('#22e3ff',1.5);s.setData(toPoints(klines,calcEMA(closes,20)));return s})(),
      (()=>{let s=line('#ffe14d',1.5);s.setData(toPoints(klines,calcEMA(closes,50)));return s})(),
      (()=>{let s=line('#ff2f6e',1.5);s.setData(toPoints(klines,calcEMA(closes,200)));return s})(),
    ];
  } else if(name==='bb'){
    let {mid,up,low}=calcBB(closes,20,2);
    lwOverlaySeries.bb=[
      (()=>{let s=line('#5e84a3',1,2);s.setData(toPoints(klines,up));return s})(),
      (()=>{let s=line('#22e3ff',1);s.setData(toPoints(klines,mid));return s})(),
      (()=>{let s=line('#5e84a3',1,2);s.setData(toPoints(klines,low));return s})(),
    ];
  } else if(name==='vwap'){
    let s=line('#ffe14d',1.5);s.setData(toPoints(klines,calcVWAP(klines)));lwOverlaySeries.vwap=s;
  } else if(name==='ichimoku'){
    let {tenkan,kijun,senkouA,senkouB}=calcIchimoku(klines);
    let cloud=lwChart.addAreaSeries({topColor:'rgba(34,227,255,.14)',bottomColor:'rgba(34,227,255,.02)',lineColor:'rgba(34,227,255,.25)',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
    cloud.setData(toPoints(klines,senkouA.map((v,i)=>v!=null&&senkouB[i]!=null?Math.max(v,senkouB[i]):null)));
    lwOverlaySeries.ichimoku=[
      (()=>{let s=line('#22e3ff',1);s.setData(toPoints(klines,tenkan));return s})(),
      (()=>{let s=line('#ff2f6e',1);s.setData(toPoints(klines,kijun));return s})(),
      cloud,
    ];
  } else if(name==='supertrend'){
    let s=line('#39ffa0',2);s.setData(toPoints(klines,calcSupertrend(klines,10,3)));lwOverlaySeries.supertrend=s;
  }
}
function redrawActiveOverlays(){lwActiveOverlays.forEach(name=>drawOverlay(name,lwKlinesCache));}
document.querySelectorAll('.ovBtn').forEach(btn=>{
  btn.onclick=()=>{
    let name=btn.dataset.ov;
    if(lwActiveOverlays.has(name)){lwActiveOverlays.delete(name);clearOverlay(name);btn.classList.remove('active');}
    else{lwActiveOverlays.add(name);btn.classList.add('active');if(lwKlinesCache.length)drawOverlay(name,lwKlinesCache);}
  };
});
// --- Oscillator sub-panel (separate mini chart, own price scale) ---
let lwOscChart=null,lwOscSeries=null,lwOscKind='';
function initLwOscChart(){
  const box=document.getElementById('lwOscBox');
  if(!box||lwOscChart)return;
  lwOscChart=LightweightCharts.createChart(box,{
    layout:{background:{color:'transparent'},textColor:'#c7dcfa'},
    grid:{vertLines:{color:'rgba(36,59,95,.3)'},horzLines:{color:'rgba(36,59,95,.18)'}},
    timeScale:{visible:false},rightPriceScale:{borderColor:'#243b5f'},crosshair:{mode:0},
  });
  new ResizeObserver(entries=>{for(const e of entries){if(lwOscChart)lwOscChart.resize(e.contentRect.width,e.contentRect.height)}}).observe(box);
  lwChart.timeScale().subscribeVisibleLogicalRangeChange(r=>{if(r&&lwOscChart)lwOscChart.timeScale().setVisibleLogicalRange(r)});
}
function drawOscillator(kind,klines){
  if(!lwOscChart)initLwOscChart();
  if(lwOscSeries){(Array.isArray(lwOscSeries)?lwOscSeries:[lwOscSeries]).forEach(s=>{try{lwOscChart.removeSeries(s)}catch(e){}});lwOscSeries=null;}
  let closes=klines.map(c=>c.close);
  const line=(chart,color,width)=>chart.addLineSeries({color,lineWidth:width||1.5,priceLineVisible:false});
  if(kind==='rsi'){
    let s=line(lwOscChart,'#22e3ff');s.setData(toPoints(klines,calcRSI(closes,14)));lwOscSeries=s;
  } else if(kind==='macd'){
    let {macdLine,signal,hist}=calcMACD(closes);
    let h=lwOscChart.addHistogramSeries({priceLineVisible:false});
    h.setData(klines.map((c,i)=>hist[i]!=null?{time:c.time,value:hist[i],color:hist[i]>=0?'#39ffa0':'#ff2f6e'}:null).filter(Boolean));
    let m=line(lwOscChart,'#22e3ff',1),sig=line(lwOscChart,'#ffe14d',1);
    m.setData(toPoints(klines,macdLine));sig.setData(toPoints(klines,signal));
    lwOscSeries=[h,m,sig];
  } else if(kind==='stoch'){
    let {k,d}=calcStochastic(klines,14,3);
    let kS=line(lwOscChart,'#22e3ff'),dS=line(lwOscChart,'#ffe14d');
    kS.setData(toPoints(klines,k));dS.setData(toPoints(klines,d));
    lwOscSeries=[kS,dS];
  } else if(kind==='adx'){
    let {adx,plusDI,minusDI}=calcADX(klines,14);
    let a=line(lwOscChart,'#e4f6ff'),p=line(lwOscChart,'#39ffa0',1),m=line(lwOscChart,'#ff2f6e',1);
    a.setData(toPoints(klines,adx));p.setData(toPoints(klines,plusDI));m.setData(toPoints(klines,minusDI));
    lwOscSeries=[a,p,m];
  } else if(kind==='cci'){
    let s=line(lwOscChart,'#22e3ff');s.setData(toPoints(klines,calcCCI(klines,20)));lwOscSeries=s;
  }
}
$('oscPick').onchange=e=>{
  lwOscKind=e.target.value;
  let box=$('lwOscBox');
  if(!lwOscKind){
    box.style.display='none';
    if(lwOscChart&&lwOscSeries){(Array.isArray(lwOscSeries)?lwOscSeries:[lwOscSeries]).forEach(s=>{try{lwOscChart.removeSeries(s)}catch(e){}});lwOscSeries=null;}
    return;
  }
  box.style.display='block';
  if(lwKlinesCache.length)drawOscillator(lwOscKind,lwKlinesCache);
};
// --- Click a candle to see its OHLCV ---
function setupLwClickHandler(){
  lwChart.subscribeClick(param=>{
    if(!param||!param.time||!lwKlinesCache.length)return;
    let candle=lwKlinesCache.find(c=>c.time===param.time);
    if(!candle)return;
    let dateStr=new Date(candle.time*1000).toISOString().replace('T',' ').substring(0,16)+' (KST)';
    $('lwClickInfo').innerHTML=`<b>${esc(dateStr)}</b> · 시가 ${n(candle.open)} · 고가 ${n(candle.high)} · 저가 ${n(candle.low)} · 종가 ${n(candle.close)} · 거래량 ${n(candle.volume)}`;
  });
}
document.querySelectorAll('.chartSizeBtn').forEach(b=>b.onclick=()=>{
  document.querySelector('.tvChartBox').style.height=b.dataset.h+'px';
  if(lwChart){let box=document.getElementById('lwChartBox');lwChart.resize(box.clientWidth,parseInt(b.dataset.h));}
});
async function checkUpdate(){
  try{
    let r=await fetch('/api/update-status',{cache:'no-store'});
    let s=await r.json();
    if(s.update_available && s.release_url){
      $('updateVersionText').textContent=`(현재 ${s.current_version} → ${s.latest_version})`;
      $('updateDownloadLink').href=s.release_url;
      $('updateBanner').style.display='flex';
    } else {
      $('updateBanner').style.display='none';
    }
  }catch(e){/* non-critical */}
}
refresh();
checkUpdate();
refreshLwChart();
setInterval(refresh,1000);
setInterval(checkUpdate,60000);
setInterval(refreshLwChart,30000);
</script></body></html>
"""


if __name__ == "__main__":
    log("BOOT", "starting Coin Monitor")
    try:
        init_db()
    except Exception as exc:
        log("DB_ERROR", f"initial schema failed: {type(exc).__name__}: {exc}")
    load_settings_cache()
    apply_settings()  # pulls E-RANG/GitHub/etc. global config from the settings DB (or seeds defaults on first run)
    _init_signal_streak_state()
    try:
        summary = db_summary()
        latest = summary.get("latest") or {}
        for uid in list_user_ids():
            state = _get_telegram_state(uid)
            state["last_long_signal"] = latest.get("long_signal")
            state["last_short_signal"] = latest.get("short_signal")
        log("TELEGRAM_BOOT", f"seeded prev state for all users: long={latest.get('long_signal')} short={latest.get('short_signal')}")
    except Exception as exc:
        log("TELEGRAM_ERROR", f"failed to seed previous signal state: {type(exc).__name__}: {exc}")
    start_collector_once()
    start_live_price_once()
    start_binance_snapshot_once()
    start_update_check_loop_once()
    if liquidation_stream is not None and ENABLE_BINANCE:
        try:
            liquidation_stream.start_liquidation_stream_once()
        except Exception as exc:
            log("LIQUIDATION_ERROR", f"failed to start liquidation stream: {type(exc).__name__}: {exc}")
    port = int(os.getenv("PORT", "8765"))
    # Desktop installs bind to localhost only (127.0.0.1) by default - this
    # is a single-user local app, not a public service. Hosting platforms
    # like Railway inject PORT automatically, which is used here as the
    # signal to default to 0.0.0.0 instead (still overridable via HOST).
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    host = os.getenv("HOST", default_host)
    log("WEB", f"listening on {host}:{port}")
    # Auto-open the dashboard in the default browser shortly after the
    # server starts listening - this is what makes a packaged .exe feel
    # like "an app that opens" instead of "a server you have to know a URL for".
    # Not a hosting platform (no PORT env var injected) = a desktop run, where
    # we prefer a real native app window over a browser tab so it looks/feels
    # like an installed program rather than a website. Falls back to opening
    # a browser tab (the old behavior) if pywebview isn't available or the
    # native window fails to start for any reason - never a hard crash.
    is_desktop_run = not os.getenv("PORT")
    use_native_window = (
        is_desktop_run
        and webview is not None
        and os.getenv("COIN_MONITOR_NO_WINDOW", "").lower() not in {"1", "true", "yes"}
    )

    if use_native_window:
        def _run_flask_server() -> None:
            app.run(host=host, port=port, threaded=True, use_reloader=False)

        flask_thread = threading.Thread(target=_run_flask_server, name="flask-server", daemon=True)
        flask_thread.start()
        time.sleep(1.0)  # give Flask a moment to bind before pointing the window at it
        try:
            webview.create_window(
                "Coin Monitor", f"http://127.0.0.1:{port}",
                width=1440, height=900, min_size=(1000, 700),
            )
            webview.start()  # blocks here, owns the main thread's event loop, until the window is closed
        except Exception as exc:
            log("WINDOW_ERROR", f"native window failed, falling back to a browser tab: {type(exc).__name__}: {exc}")
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception as exc2:
                log("BROWSER_ERROR", f"{type(exc2).__name__}: {exc2}")
            flask_thread.join()  # app.run() already moved to the background thread above - keep the process alive
    else:
        if os.getenv("COIN_MONITOR_NO_BROWSER", "").lower() not in {"1", "true", "yes"}:
            def _open_browser() -> None:
                time.sleep(1.2)
                try:
                    webbrowser.open(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
                except Exception as exc:
                    log("BROWSER_ERROR", f"{type(exc).__name__}: {exc}")
            threading.Thread(target=_open_browser, name="browser-launcher", daemon=True).start()
        app.run(host=host, port=port, threaded=True)
