import re
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup, Tag

NUM_RE = r"-?\d[\d,]*(?:\.\d+)?"

LONG_COLOR_HINTS = (
    "green", "blue", "lime", "emerald", "cyan", "teal",
    "#16a34a", "#22c55e", "#10b981", "#008000", "#00ff00",
    "#0000ff", "#2563eb", "#3b82f6", "#1d4ed8", "#0ea5e9",
    "rgb(0,128,0)", "rgb(0, 128, 0)", "rgb(0,0,255)", "rgb(0, 0, 255)",
)
SHORT_COLOR_HINTS = (
    "red", "rose", "pink", "crimson", "danger",
    "#dc2626", "#ef4444", "#f00", "#ff0000", "#b91c1c",
    "rgb(255,0,0)", "rgb(255, 0, 0)",
)
SIDE_ALIASES = {
    "long": ("long", "롱", "매수"),
    "short": ("short", "숏", "매도"),
}
ENTRY_WORDS = ("진입", "entry", "buy", "sell")
TP_WORDS = ("tp", "take profit", "목표", "익절")
SL_WORDS = ("sl", "stop loss", "손절", "스탑")


def _clean_num(value: str) -> str:
    return value.strip().replace(" ", "")


def _numbers(text: str) -> List[str]:
    return [_clean_num(x) for x in re.findall(NUM_RE, text)]


def _lower_join(values: List[str]) -> str:
    return " ".join(v for v in values if v).lower().replace(" ", "")


def _visible_text(soup: BeautifulSoup) -> str:
    return "\n".join(s.strip() for s in soup.stripped_strings if s.strip())


def _node_text(node: Optional[Tag]) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _side_match_text(side: str) -> re.Pattern[str]:
    aliases = [re.escape(x) for x in SIDE_ALIASES[side]]
    return re.compile(r"(?:^|\b|\s)(" + "|".join(aliases) + r")(?:\b|\s|$)", re.I)


def _find_side_nodes(soup: BeautifulSoup, side: str) -> List[Tag]:
    pattern = _side_match_text(side)
    nodes: List[Tag] = []
    for string in soup.find_all(string=pattern):
        parent = string.parent
        # CSS/JS source can contain words like long/short and active color rules.
        # Those are not visible labels and must never be treated as signal nodes.
        if isinstance(parent, Tag) and parent.name not in {"style", "script", "noscript", "template"} and parent not in nodes:
            nodes.append(parent)
    # Add table rows/cards containing the side label because colors are often on ancestors.
    expanded: List[Tag] = []
    for node in nodes:
        for candidate in [node, node.find_parent("td"), node.find_parent("tr"), node.find_parent("div"), node.find_parent("section")]:
            if isinstance(candidate, Tag) and candidate not in expanded:
                expanded.append(candidate)
    return expanded


def _visual_context(node: Optional[Tag]) -> str:
    """Return only the visual scope that can actually belong to this side label.

    Important: do not walk up to <table>/<div> containers or inspect unrelated
    siblings. e-rang uses a blue table header; the old broad scan could see that
    header while parsing the Long row and incorrectly report LONG=True.
    """
    if node is None:
        return ""
    parts: List[str] = []
    candidates: List[Tag] = [node]
    td = node.find_parent("td")
    tr = node.find_parent("tr")
    for candidate in (td, tr):
        if isinstance(candidate, Tag) and candidate not in candidates:
            candidates.append(candidate)
    for cur in candidates:
        cls = cur.get("class", [])
        cls_text = cls if isinstance(cls, str) else " ".join(str(x) for x in cls)
        style_text = str(cur.get("style", ""))
        data_text = " ".join(f"{k}={v}" for k, v in cur.attrs.items() if k.startswith("data-"))
        parts.append(f"<{cur.name}> class='{cls_text}' style='{style_text}' {data_text} text='{_node_text(cur)[:240]}'")
    return " | ".join(parts)


def _css_colors(visual: str) -> List[tuple[int, int, int, str]]:
    colors: List[tuple[int, int, int, str]] = []
    for raw in re.findall(r"#[0-9a-fA-F]{3,6}\b", visual):
        h = raw[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            colors.append((int(h[0:2],16), int(h[2:4],16), int(h[4:6],16), raw.lower()))
    for m in re.finditer(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", visual, re.I):
        r,g,b = (min(255, int(m.group(i))) for i in (1,2,3))
        colors.append((r,g,b,f"rgb({r},{g},{b})"))
    return colors


def _stylesheet_context(soup: BeautifulSoup, node: Optional[Tag]) -> str:
    """Return CSS declarations that actually match this node's current class/id.

    Older versions searched for any selector containing one of the node classes.
    That incorrectly matched rules such as `.long-label.active` even when the
    element only had `class="long-label"`, making inactive LONG and SHORT both ON.
    """
    if node is None:
        return ""
    classes = node.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    class_set = {str(c) for c in classes if c}
    node_id = str(node.get("id") or "")
    tag_name = (node.name or "").lower()

    def selector_matches(selector: str) -> bool:
        # Only evaluate the final simple selector. This is sufficient for the
        # label-cell rules we need and, importantly, is conservative.
        simple = selector.strip().split()[-1] if selector.strip() else ""
        simple = simple.split(":", 1)[0]  # ignore :hover/:focus etc.
        if not simple:
            return False
        ids = re.findall(r"#([A-Za-z0-9_-]+)", simple)
        if ids and any(x != node_id for x in ids):
            return False
        required_classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", simple))
        if not required_classes.issubset(class_set):
            return False
        tag_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)", simple)
        if tag_match and tag_match.group(1).lower() != tag_name:
            return False
        return bool(ids or required_classes or tag_match)

    evidence = []
    for style in soup.find_all("style"):
        css = style.get_text(" ", strip=True)
        for m in re.finditer(r"([^{}]+)\{([^{}]+)\}", css, re.I):
            selector_group, declarations = m.group(1), m.group(2)
            for selector in selector_group.split(","):
                if selector_matches(selector):
                    evidence.append(f"css {selector.strip()} {{{declarations.strip()}}}")
                    break
    return " | ".join(evidence)


def _detect_color(side: str, visual: str) -> Optional[str]:
    low = visual.lower().replace(" ", "")
    hints = LONG_COLOR_HINTS if side == "long" else SHORT_COLOR_HINTS
    hinted = next((hint for hint in hints if hint.replace(" ", "") in low), None)
    if hinted:
        return hinted
    # Accept close CSS shades too, e.g. e-rang's bright #ff3b30 SHORT badge.
    for r, g, b, raw in _css_colors(visual):
        if side == "short" and r >= 180 and r >= g * 1.45 and r >= b * 1.35:
            return raw
        if side == "long" and ((g >= 110 and g >= r * 1.20 and g >= b * 1.05) or (b >= 150 and b >= r * 1.25 and b >= g * 1.05)):
            return raw
    return None


def _signal_for(soup: BeautifulSoup, side: str) -> Dict[str, Any]:
    best: Dict[str, Any] = {
        "active": False,
        "detected_color": None,
        "visual_evidence": "",
        "label_html": None,
        "matched_text": None,
    }
    for node in _find_side_nodes(soup, side):
        # Prefer the exact Long/Short label element. Include CSS rules that target
        # that element's class/id, because the rendered background may come from
        # a stylesheet instead of an inline style.
        visual = _visual_context(node)
        css_visual = _stylesheet_context(soup, node)
        combined_visual = " | ".join(x for x in (visual, css_visual) if x)
        color = _detect_color(side, combined_visual)
        active = bool(color)
        text = _node_text(node)
        candidate = {
            "active": active,
            "detected_color": color,
            "visual_evidence": combined_visual,
            "label_html": str(node)[:3000],
            "matched_text": text,
        }
        if active:
            return candidate
        if not best["label_html"]:
            best = candidate
    return best

def _extract_side_blocks_from_text(text: str) -> Dict[str, Dict[str, Any]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)
    result: Dict[str, Dict[str, Any]] = {}
    side_positions = []
    for side in ("long", "short"):
        pattern = _side_match_text(side)
        m = pattern.search(joined)
        if m:
            side_positions.append((m.start(), side))
    side_positions.sort()
    for idx, (start, side) in enumerate(side_positions):
        end = side_positions[idx + 1][0] if idx + 1 < len(side_positions) else len(joined)
        block = joined[start:end]
        nums = _numbers(block)
        result[side] = {
            "text_block": block[:5000],
            "numbers_raw": nums,
            "entry_prices_guess": nums[:5],
            "extra_numbers": nums[5:],
            "tp_values_guess": _labeled_numbers(block, TP_WORDS),
            "sl_values_guess": _labeled_numbers(block, SL_WORDS),
        }
    return result


def _labeled_numbers(text: str, labels: tuple[str, ...]) -> List[str]:
    found: List[str] = []
    for label in labels:
        pattern = re.compile(re.escape(label) + rf"[^0-9\-]{{0,60}}({NUM_RE})", re.I)
        found.extend(_clean_num(m.group(1)) for m in pattern.finditer(text))
    return found[:20]


def _extract_rows(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        text = _node_text(tr)
        if not text:
            continue
        nums = _numbers(text)
        side = None
        low = text.lower()
        if any(alias in low for alias in SIDE_ALIASES["long"]):
            side = "long"
        if any(alias in low for alias in SIDE_ALIASES["short"]):
            side = "short"
        if side or nums:
            rows.append({
                "side": side,
                "text": text[:1000],
                "numbers_raw": nums,
                "class": " ".join(tr.get("class", [])) if not isinstance(tr.get("class", []), str) else tr.get("class", ""),
                "style": str(tr.get("style", "")),
            })
    return rows[:80]


def _current_price_from_text(text: str) -> Optional[str]:
    patterns = [
        rf"(?:현재가|Current\s*Price|BTCUSDT|BTC\s*USDT)[^0-9\-]{{0,80}}({NUM_RE})",
        rf"({NUM_RE})[^\n]{{0,30}}(?:BTCUSDT|BTC\s*USDT)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return _clean_num(m.group(1))
    nums = _numbers(text)
    # BTC price is usually the first large 5+ digit number when labels are absent.
    for n in nums:
        try:
            if abs(float(n.replace(",", ""))) >= 10000:
                return n
        except ValueError:
            pass
    return None


def parse_page(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    text = _visible_text(soup)
    sides = _extract_side_blocks_from_text(text)
    rows = _extract_rows(soup)
    result: Dict[str, Any] = {
        "page_text": text,
        "current_price_raw": _current_price_from_text(text),
        "sides": sides,
        "table_rows": rows,
        "signals": {
            "long": _signal_for(soup, "long"),
            "short": _signal_for(soup, "short"),
        },
        "entry_message": bool(re.search(r"진입\s*(?:해도\s*)?(?:좋|가능)|진입\s*추천|entry\s*(?:ok|signal|possible)", text, re.I)),
        "parser_version": "3.0.0",
    }
    return result
