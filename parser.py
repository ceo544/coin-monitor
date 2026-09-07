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


def _background_declarations(text: str) -> str:
    """Keep only background/background-color declarations for signal colors."""
    found = []
    for m in re.finditer(r"(?:^|;)\s*(background(?:-color)?)\s*:\s*([^;}{]+)", text or "", re.I):
        found.append(f"{m.group(1)}:{m.group(2).strip()}")
    return ";".join(found)


def _label_node(node: Optional[Tag]) -> Optional[Tag]:
    if node is None:
        return None
    # The actual E-RANG signal is the Long/Short label cell. Never inherit
    # header/row/container colors into the decision.
    if node.name in {"td", "th"}:
        return node
    td = node.find_parent(["td", "th"])
    return td if isinstance(td, Tag) else node


def _visual_context(node: Optional[Tag]) -> str:
    node = _label_node(node)
    if node is None:
        return ""
    cls = node.get("class", [])
    cls_text = cls if isinstance(cls, str) else " ".join(str(x) for x in cls)
    style_bg = _background_declarations(str(node.get("style", "")))
    data_text = " ".join(f"{k}={v}" for k, v in node.attrs.items() if k.startswith("data-"))
    return f"<{node.name}> class='{cls_text}' background='{style_bg}' {data_text} text='{_node_text(node)[:120]}'"


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


def _own_style_background(node: Optional[Tag]) -> str:
    node = _label_node(node)
    if node is None:
        return ""
    return _background_declarations(str(node.get("style", "")))


def _simple_selector_requirements(simple: str) -> tuple[Optional[str], List[str], set]:
    """Parse a single compound selector token (e.g. '.a.b#id') into
    (tag, ids, classes). Pseudo-classes/elements are stripped."""
    simple = simple.split(":", 1)[0]
    ids = re.findall(r"#([A-Za-z0-9_-]+)", simple)
    classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", simple))
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)", simple)
    tag = tag_match.group(1).lower() if tag_match else None
    return tag, ids, classes


def _compound_matches_tag(tag_el: Tag, tag: Optional[str], ids: List[str], classes: set) -> bool:
    if not (tag or ids or classes):
        return False
    node_classes = tag_el.get("class", [])
    if isinstance(node_classes, str):
        node_classes = node_classes.split()
    class_set = {str(c) for c in node_classes if c}
    node_id = str(tag_el.get("id") or "")
    if ids and any(x != node_id for x in ids):
        return False
    if not classes.issubset(class_set):
        return False
    if tag and tag != (tag_el.name or "").lower():
        return False
    return True


def _selector_matches_node(selector: str, node: Tag) -> bool:
    """Approximate CSS descendant-combinator matching.

    Every compound token in a space-separated selector must match the node
    itself (the last token) AND each earlier token must match some ancestor,
    in order, from innermost to outermost. This matters because rules like
    '.row.active .label { background: red }' must only apply when the
    ANCESTOR actually carries the 'active' class - not merely because the
    label's own class matches the final compound selector. Checking only the
    last token (as before) makes such conditional/toggle rules always match,
    which was the root cause of Long/Short both being permanently reported
    as ON regardless of the site's real active/inactive state.
    """
    tokens = [t for t in selector.strip().split() if t]
    if not tokens:
        return False
    *ancestor_tokens, last_token = tokens
    tag, ids, classes = _simple_selector_requirements(last_token)
    if not _compound_matches_tag(node, tag, ids, classes):
        return False
    pos: Tag = node
    for token in reversed(ancestor_tokens):
        tag, ids, classes = _simple_selector_requirements(token)
        if not (tag or ids or classes):
            return False
        found = None
        parent = pos.parent
        while isinstance(parent, Tag):
            if _compound_matches_tag(parent, tag, ids, classes):
                found = parent
                break
            parent = parent.parent
        if found is None:
            return False
        pos = found
    return True


def _selector_ancestor_classes(selector: str) -> List[str]:
    """Classes required on ancestors (every token except the last) of a
    descendant-combinator selector, e.g. for
    '.coin-strategy__long.blue .coin-strategy__side' this returns
    ['blue', 'coin-strategy__long'] - i.e. exactly the toggle/state classes
    that had to be present on some ancestor for this rule to apply. This is
    precise, structured evidence of *why* a signal activated (not just the
    resulting color), useful for building a dataset from collected data."""
    tokens = [t for t in selector.strip().split() if t]
    if len(tokens) <= 1:
        return []
    classes: List[str] = []
    for token in tokens[:-1]:
        _, _, cls = _simple_selector_requirements(token)
        classes.extend(sorted(cls))
    return classes


def _matching_backgrounds(soup: BeautifulSoup, node: Optional[Tag], with_selector: bool) -> List[str]:
    node = _label_node(node)
    if node is None:
        return []
    results: List[str] = []
    for style in soup.find_all("style"):
        css = style.get_text(" ", strip=True)
        for m in re.finditer(r"([^{}]+)\{([^{}]+)\}", css, re.I):
            selector_group, declarations = m.group(1), m.group(2)
            bg = _background_declarations(declarations)
            if not bg:
                continue
            for selector in selector_group.split(","):
                if _selector_matches_node(selector, node):
                    results.append(f"css {selector.strip()} {{{bg}}}" if with_selector else bg)
                    break
    return results


def _matching_backgrounds_structured(soup: BeautifulSoup, node: Optional[Tag]) -> List[Dict[str, Any]]:
    """Same matching as _matching_backgrounds, but keeps the selector,
    background declaration and required-ancestor-classes as separate,
    structured fields instead of one combined display string."""
    node = _label_node(node)
    if node is None:
        return []
    results: List[Dict[str, Any]] = []
    for style in soup.find_all("style"):
        css = style.get_text(" ", strip=True)
        for m in re.finditer(r"([^{}]+)\{([^{}]+)\}", css, re.I):
            selector_group, declarations = m.group(1), m.group(2)
            bg = _background_declarations(declarations)
            if not bg:
                continue
            for selector in selector_group.split(","):
                sel = selector.strip()
                if _selector_matches_node(sel, node):
                    results.append({
                        "selector": sel,
                        "declaration": bg,
                        "ancestor_classes": _selector_ancestor_classes(sel),
                    })
                    break
    return results


def _stylesheet_backgrounds(soup: BeautifulSoup, node: Optional[Tag]) -> List[str]:
    """Background declaration values only (no selector text), for color detection."""
    return _matching_backgrounds(soup, node, with_selector=False)


def _stylesheet_context(soup: BeautifulSoup, node: Optional[Tag]) -> str:
    """Human-readable evidence text (selector + background), for display only."""
    return " | ".join(_matching_backgrounds(soup, node, with_selector=True))


def _detect_color(side: str, visual: str) -> Optional[str]:
    # visual contains only label class names + background declarations.
    low = visual.lower().replace(" ", "")
    hints = LONG_COLOR_HINTS if side == "long" else SHORT_COLOR_HINTS
    hinted = next((hint for hint in hints if hint.replace(" ", "") in low), None)
    if hinted:
        return hinted
    for r, g, b, raw in _css_colors(visual):
        if side == "short" and r >= 180 and r >= g * 1.45 and r >= b * 1.35:
            return raw
        if side == "long" and ((g >= 110 and g >= r * 1.20 and g >= b * 1.05) or (b >= 150 and b >= r * 1.25 and b >= g * 1.05)):
            return raw
    return None


def _signal_for(soup: BeautifulSoup, side: str) -> Dict[str, Any]:
    best: Dict[str, Any] = {
        "active": False, "detected_color": None, "visual_evidence": "", "label_html": None, "matched_text": None,
        # Precise, structured entry-basis fields (kept separate from the
        # human-readable visual_evidence string above so downstream data
        # collection/analysis doesn't have to parse text back out of it).
        "matched_css_selector": None,        # exact selector that produced the detected color, e.g. ".coin-strategy__long.blue .coin-strategy__side"
        "matched_css_declaration": None,     # e.g. "background:#44C27B"
        "ancestor_classes": [],              # toggle/state classes required on an ancestor for the rule to apply, e.g. ["blue"]
        "own_inline_background": None,       # the label's own inline style background, if that's what decided the color
        "label_tag": None,                   # e.g. "td"
        "label_own_classes": [],             # classes on the label cell itself (not ancestors)
    }
    seen = set()
    for raw_node in _find_side_nodes(soup, side):
        node = _label_node(raw_node)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        visual = _visual_context(node)
        css_visual = _stylesheet_context(soup, node)
        combined_visual = " | ".join(x for x in (visual, css_visual) if x)
        # Color detection must only ever look at actual background declarations
        # (inline style or matched stylesheet rules), never at raw class names,
        # text color/border color, or node text. Class names like
        # "text-blue-600" or "border-green-500" contain color words but do not
        # mean the label cell's background is that color, so they must not be
        # able to flip a signal ON.
        own_bg = _own_style_background(node)
        structured_matches = _matching_backgrounds_structured(soup, node)
        sheet_bgs = [m["declaration"] for m in structured_matches]
        background_only = ";".join(x for x in [own_bg, *sheet_bgs] if x)
        color = _detect_color(side, background_only)
        # Identify exactly which single source (inline style, or which one
        # matched CSS rule) actually produced the detected color, so the
        # evidence points at one concrete cause rather than a blob of
        # everything that was checked.
        matched_selector = matched_declaration = None
        ancestor_classes: List[str] = []
        own_inline_background = None
        if color:
            if own_bg and _detect_color(side, own_bg) == color:
                own_inline_background = own_bg
            else:
                for m in structured_matches:
                    if _detect_color(side, m["declaration"]) == color:
                        matched_selector = m["selector"]
                        matched_declaration = m["declaration"]
                        ancestor_classes = m["ancestor_classes"]
                        break
        node_classes = node.get("class", [])
        if isinstance(node_classes, str):
            node_classes = node_classes.split()
        candidate = {
            "active": bool(color), "detected_color": color,
            "visual_evidence": combined_visual, "label_html": str(node)[:3000],
            "matched_text": _node_text(node),
            "matched_css_selector": matched_selector,
            "matched_css_declaration": matched_declaration,
            "ancestor_classes": ancestor_classes,
            "own_inline_background": own_inline_background,
            "label_tag": node.name,
            "label_own_classes": [str(c) for c in node_classes if c],
        }
        if candidate["active"]:
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
        "parser_version": "3.6",
    }
    return result
