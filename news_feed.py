"""
Crypto market news ticker - pulls headlines from a few public RSS feeds
(no API key needed) so the dashboard can show a scrolling "주요 시황" strip.

Each source is fetched and parsed independently, so one feed being down or
changing its RSS URL never blanks out the others - it just quietly
contributes zero headlines and shows up in the "errors" dict for
debugging.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")

# (display name, RSS feed URL). Mix of English and Korean crypto outlets so
# the ticker isn't one-sided. Each is independently wrapped in try/except -
# if a URL goes stale or a site changes its feed path, that source just
# silently drops out rather than breaking the whole ticker.
NEWS_SOURCES: List[Tuple[str, str]] = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("TokenPost", "https://www.tokenpost.kr/rss"),
]
USER_AGENT = os.getenv("NEWS_USER_AGENT", "Mozilla/5.0 (compatible; CoinMonitorNewsBot/1.0; +https://github.com)")
TIMEOUT = float(os.getenv("NEWS_TIMEOUT", "8"))
ITEMS_PER_SOURCE = 12


def _parse_pub_date(raw: str):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def fetch_feed(name: str, url: str) -> Tuple[List[Dict[str, Any]], str]:
    """Returns (items, error_message). error_message is empty on success -
    checked as falsy by the caller, so a real error is always a non-empty
    string."""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"

    items: List[Dict[str, Any]] = []
    for item in root.findall(".//item")[:ITEMS_PER_SOURCE]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub_dt = _parse_pub_date(item.findtext("pubDate") or "")
        items.append({
            "title": title,
            "link": link,
            "source": name,
            "published_at": pub_dt.isoformat() if pub_dt else None,
            "_sort_ts": pub_dt.timestamp() if pub_dt else 0,
        })
    return items, ""


def fetch_all_news(limit: int = 24) -> Dict[str, Any]:
    all_items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    for name, url in NEWS_SOURCES:
        items, err = fetch_feed(name, url)
        all_items.extend(items)
        if err:
            errors[name] = err
    all_items.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    for item in all_items:
        item.pop("_sort_ts", None)
    return {
        "items": all_items[:limit],
        "errors": errors or None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Economic calendar (CPI, FOMC rate decisions, NFP, etc.) - these US macro
# releases move crypto prices too, not just forex/stocks, so they're worth
# surfacing alongside plain news headlines. Pulled from the widely-used
# public JSON feed that mirrors ForexFactory's calendar (no API key, no
# auth) - a de-facto standard source many open-source trading dashboards
# already rely on for exactly this.
# ---------------------------------------------------------------------------
CALENDAR_URL = os.getenv("ECON_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
CALENDAR_COUNTRIES = {"USD"}  # US macro data is what moves crypto most
CALENDAR_IMPACT_LEVELS = {"High"}


def fetch_economic_calendar() -> Dict[str, Any]:
    """Returns upcoming high-impact USD events (CPI, FOMC, NFP, etc.) for
    the current week, soonest first. Each event's own "date" field already
    carries its timezone offset (parsed via fromisoformat), converted to
    UTC for consistent sorting/comparison by the caller."""
    try:
        resp = requests.get(CALENDAR_URL, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        return {"items": [], "error": f"{type(exc).__name__}: {exc}"}

    events = []
    for row in raw if isinstance(raw, list) else []:
        try:
            if row.get("country") not in CALENDAR_COUNTRIES:
                continue
            if row.get("impact") not in CALENDAR_IMPACT_LEVELS:
                continue
            date_raw = row.get("date")
            if not date_raw:
                continue
            dt = datetime.fromisoformat(date_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            events.append({
                "title": row.get("title") or "",
                "country": row.get("country"),
                "date": dt.astimezone(timezone.utc).isoformat(),
                "forecast": row.get("forecast") or None,
                "previous": row.get("previous") or None,
            })
        except (TypeError, ValueError):
            continue
    events.sort(key=lambda e: e["date"])
    return {"items": events, "error": None}


# ---------------------------------------------------------------------------
# "오늘은 위험한 날" 배너 - 경제지표 캘린더(CPI/FOMC 등, 오늘 날짜인 것만) +
# 뉴스 헤드라인 중 코인 관련 법안/규제 이슈(클래리티법 등, 오늘 게재된 것만)를
# 합쳐서 반환합니다. 매일 뜨는 게 아니라 그런 이슈가 있는 날에만 뜨도록,
# fetch_all_news()의 "48시간 이내" 우선순위 로직과는 별개로 "오늘(KST)"
# 기준으로 엄격하게 필터링합니다.
# ---------------------------------------------------------------------------
LEGISLATION_KEYWORDS = [
    "클래리티", "clarity", "법안", "규제", "sec ", "청문회", "제재", "소송",
    "부결", "가결", "행정명령", "가상자산법", "코인법안", "stablecoin bill",
    "market structure bill", "규제안", "입법",
]


_translation_cache: Dict[str, str] = {}
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_HANGUL_RE = None  # set below, avoids importing re at module top just for this


def _looks_korean_already(text: str) -> bool:
    import re
    global _HANGUL_RE
    if _HANGUL_RE is None:
        _HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
    hangul_count = len(_HANGUL_RE.findall(text))
    return hangul_count >= max(3, len(text) // 6)  # 짧은 영단어 하나 섞인 한글기사는 오탐 안 하게 여유를 둠


def translate_to_korean(text: str) -> str:
    """제목이 이미 한글(TokenPost 등 국내 매체)이면 그대로 두고, 영어(CoinDesk/
    CoinTelegraph 등)면 번역합니다. API 키가 필요 없는 구글 번역 비공식
    엔드포인트를 씁니다 - 실패해도(네트워크 문제 등) 원문을 그대로 반환해서
    "오늘은 위험한 날" 배너 자체가 깨지지 않게 합니다. 같은 제목을 반복해서
    다시 번역하지 않도록 캐싱합니다."""
    if not text or _looks_korean_already(text):
        return text
    if text in _translation_cache:
        return _translation_cache[text]
    try:
        resp = requests.get(TRANSLATE_URL, params={
            "client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text,
        }, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        if translated:
            _translation_cache[text] = translated
            return translated
    except Exception:
        pass
    return text  # 번역 실패 시 원문 그대로 (배너가 비거나 에러 나는 것보다 나음)


def detect_today_risk(news_items: List[Dict[str, Any]], calendar_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """오늘(KST 기준) 발생하는 위험 요소 목록을 반환합니다. 각 항목은
    {"type": "calendar"|"news", "title": ..., "detail": ...} 형태."""
    today_kst = datetime.now(KST).date()
    risks: List[Dict[str, str]] = []

    for ev in calendar_items or []:
        try:
            ev_dt = datetime.fromisoformat(ev["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if ev_dt.astimezone(KST).date() == today_kst:
            time_str = ev_dt.astimezone(KST).strftime("%H:%M")
            risks.append({"type": "calendar", "title": translate_to_korean(ev.get("title", "")), "detail": f"{time_str} KST"})

    for item in news_items or []:
        title = item.get("title") or ""
        pub = item.get("published_at")
        if not pub:
            continue
        try:
            pub_dt = datetime.fromisoformat(pub)
        except (TypeError, ValueError):
            continue
        if pub_dt.astimezone(KST).date() != today_kst:
            continue
        if any(kw in title.lower() for kw in LEGISLATION_KEYWORDS):
            risks.append({"type": "news", "title": translate_to_korean(title), "detail": item.get("source", "")})

    return risks
