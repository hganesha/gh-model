"""Accessibility facts read from markup and stylesheets (docs/solution-ir-contract.md, section 8.5).

Per control (`accessibility` on a UIControl):

  name_source  where its accessible name comes from, in the order the accessible-name
               computation uses: `aria-labelledby`, `aria-label`, `label-for` (a <label for>
               naming its id), `wrapping-label`, `label-prop` (a library control's `label`),
               `content` (a button's or link's text), `value` (an input button), `alt`,
               `title`, `placeholder`, else `none`
  role         the explicit `role`, else the implicit role of a native element; a library
               component's role is not guessed
  tabindex     an explicit tabindex: the number, or "dynamic" when it is bound

Per screen (`accessibility` on a Screen): the landmarks it declares, its heading levels in
document order, how many times a heading skips a level going down, and how many elements
have a positive tabindex.

Contrast: in a style rule whose `color` and `background-color` (or `background`) are each
exactly one token reference, when both tokens are declared once in the same stylesheet
with a literal opaque colour (following that stylesheet's own aliases), the WCAG contrast
ratio goes on the text token as `contrast`. A token redeclared under another selector (a
dark theme, say) has more than one value, so no ratio is given for it.

`accessibility_coverage` turns the control facts of compiled shards into the
`accessibility` coverage domain: controls with an accessible name over controls.
"""

from __future__ import annotations

import colorsys
import re
from typing import Any

from repo_intel.platforms.web import styles
from repo_intel.platforms.web.markup import Element

_INPUT_ROLES = {
    "text": "textbox", "email": "textbox", "tel": "textbox", "url": "textbox", "password": "textbox", "search": "searchbox",
    "number": "spinbutton", "range": "slider", "checkbox": "checkbox", "radio": "radio",
    "submit": "button", "reset": "button", "button": "button", "image": "button",
}
_NATIVE = frozenset({"input", "select", "textarea", "button", "a"})
_LANDMARK_ROLES = frozenset({"banner", "navigation", "main", "complementary", "contentinfo", "search", "form", "region"})
_LANDMARK_TAGS = {"header": "banner", "nav": "navigation", "main": "main", "aside": "complementary", "footer": "contentinfo", "search": "search"}
_SECTIONING = frozenset({"article", "aside", "main", "nav", "section"})
_HEADING = re.compile(r"^h([1-6])$")
_NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "orange": (255, 165, 0), "yellow": (255, 255, 0), "purple": (128, 0, 128),
    "pink": (255, 192, 203), "brown": (165, 42, 42), "navy": (0, 0, 128), "teal": (0, 128, 128), "silver": (192, 192, 192),
    "maroon": (128, 0, 0), "olive": (128, 128, 0), "lime": (0, 255, 0), "aqua": (0, 255, 255), "fuchsia": (255, 0, 255),
    "crimson": (220, 20, 60), "gold": (255, 215, 0), "indigo": (75, 0, 130), "coral": (255, 127, 80),
}


# ------------------------------------------------------------------ controls


def label_targets(elements: list[Element]) -> set[str]:
    """The ids that a <label for> (JSX `htmlFor`) in this file names."""
    out = set()
    for el in elements:
        if el.tag.lower() == "label":
            target = el.lit("for", "htmlFor")
            if target and target.strip():
                out.add(target.strip())
    return out


def control(el: Element, kind: str, labelled: set[str]) -> dict[str, Any]:
    """A control's accessible name source, role and explicit tabindex."""
    out: dict[str, Any] = {"name_source": _name_source(el, kind, labelled)}
    role = el.lit("role")
    if role and role.strip():
        out["role"] = role.strip().split()[0]
    elif el.tag in _NATIVE:
        implicit = _implicit_role(el)
        if implicit:
            out["role"] = implicit
    tabindex = _tabindex(el)
    if tabindex is not None:
        out["tabindex"] = tabindex
    return out


def _given(el: Element, *keys: str) -> bool:
    """The attribute is present with a non-empty literal value, or bound to an expression."""
    a = el.get(*keys)
    return a is not None and a.value is not None and (not a.literal or bool(a.value.strip()))


def _name_source(el: Element, kind: str, labelled: set[str]) -> str:
    if _given(el, "aria-labelledby"):
        return "aria-labelledby"
    if _given(el, "aria-label"):
        return "aria-label"
    ident = (el.lit("id") or "").strip()
    if ident and ident in labelled:
        return "label-for"
    if any(a.tag.lower() == "label" for a in el.ancestors()):
        return "wrapping-label"
    if el.tag not in _NATIVE and _given(el, "label"):
        return "label-prop"
    input_type = (el.lit("type") or "").strip().lower() if el.tag in ("input", "Input") else ""
    if input_type in ("submit", "reset", "button"):
        return "value" if _given(el, "value") or input_type in ("submit", "reset") else "none"  # submit and reset have a default name
    if input_type == "image":
        return "alt" if _given(el, "alt") else "none"
    if kind in ("button", "link") and _has_content(el):
        return "content"
    if _given(el, "title"):
        return "title"
    if kind not in ("button", "link") and _given(el, "placeholder"):
        return "placeholder"
    return "none"


def _has_content(el: Element) -> bool:
    """Text inside the element, static or computed (`{t("nav.home")}`, `{{ $t('x') }}`), or an
    image child with alt text."""
    if el.deep_text.strip():
        return True
    node = getattr(el, "node", None)
    if node is not None and any(c.type == "jsx_expression" and c.named_children for c in node.children):
        return True
    return any(c.tag.lower() in ("img", "svg") and (_given(c, "alt") or _given(c, "aria-label") or _given(c, "title")) for c in el.children)


def _implicit_role(el: Element) -> str | None:
    tag = el.tag
    if tag == "input":
        return _INPUT_ROLES.get((el.lit("type") or "text").strip().lower())
    if tag == "select":
        size = (el.lit("size") or "").strip()
        return "listbox" if el.has("multiple") or (size.isdigit() and int(size) > 1) else "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "button":
        return "button"
    if tag == "a":
        return "link" if el.has("href") else None
    return None


def _tabindex(el: Element) -> int | str | None:
    a = el.get("tabindex", "tabIndex")
    if a is None or a.value is None:
        return None
    value = a.value.strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return "dynamic"


# ------------------------------------------------------------------ screens


def screen(elements: list[Element]) -> dict[str, Any]:
    """Landmarks, heading levels in document order, level skips and positive tabindex count."""
    landmarks: list[str] = []
    headings: list[int] = []
    positive = 0
    for el in elements:
        tag = el.tag.lower() if el.tag[:1].islower() else el.tag
        role = (el.lit("role") or "").strip().lower()
        landmark = role if role in _LANDMARK_ROLES else None
        if landmark is None and not role:
            landmark = _LANDMARK_TAGS.get(tag)
            if tag in ("header", "footer") and any(a.tag.lower() in _SECTIONING for a in el.ancestors()):
                landmark = None  # a header or footer inside a section is not the page's banner or contentinfo
            if tag in ("section", "form") and (_given(el, "aria-label") or _given(el, "aria-labelledby") or _given(el, "title")):
                landmark = "region" if tag == "section" else "form"
        if landmark and landmark not in landmarks:
            landmarks.append(landmark)
        m = _HEADING.match(tag)
        level = int(m.group(1)) if m else None
        if role == "heading":
            raw = (el.lit("aria-level") or "").strip()
            level = int(raw) if raw.isdigit() and 1 <= int(raw) <= 6 else level
        if level is not None and len(headings) < 64:
            headings.append(level)
        t = _tabindex(el)
        if isinstance(t, int) and t > 0:
            positive += 1
    skips = sum(1 for prev, cur in zip(headings, headings[1:]) if cur > prev + 1)
    out: dict[str, Any] = {"landmarks": sorted(landmarks), "headings": headings, "heading_skips": skips, "positive_tabindex": positive}
    return {k: v for k, v in out.items() if v != []}


# ------------------------------------------------------------------ contrast


def parse_colour(value: str | None) -> tuple[float, float, float] | None:
    """An opaque literal colour as (r, g, b) in 0..255; None for anything else (a reference,
    a keyword such as `currentcolor`, a colour with transparency, a function this does not read)."""
    if value is None:
        return None
    v = " ".join(value.lower().split())
    if v in _NAMED:
        return _NAMED[v]
    m = re.fullmatch(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 8 and h[6:] != "ff":
            return None
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"(rgba?|hsla?)\((.*)\)", v)
    if not m:
        return None
    parts = [p for p in re.split(r"[\s,/]+", m.group(2).strip()) if p]
    if len(parts) == 4:
        alpha = _number(parts[3], 1.0)
        if alpha is None or alpha < 1:
            return None
        parts = parts[:3]
    if len(parts) != 3:
        return None
    if m.group(1).startswith("rgb"):
        channels = [_number(p, 255.0) for p in parts]
        if any(c is None or c < 0 or c > 255 for c in channels):
            return None
        return tuple(channels)  # type: ignore[return-value]
    hue = _number(parts[0].removesuffix("deg"), None)
    sat, light = _number(parts[1], 1.0), _number(parts[2], 1.0)
    if hue is None or sat is None or light is None or not parts[1].endswith("%") or not parts[2].endswith("%"):
        return None
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, min(max(light, 0), 1), min(max(sat, 0), 1))
    return r * 255, g * 255, b * 255


def _number(text: str, percent_of: float | None) -> float | None:
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100 * percent_of if percent_of is not None else None
        return float(text)
    except ValueError:
        return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    def linear(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """The WCAG 2 contrast ratio of two opaque colours, rounded to two places."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def _token_colour(scan: styles.StyleScan, name: str) -> tuple[float, float, float] | None:
    seen = set()
    while name in scan.tokens and name not in seen and len(seen) < 8:
        seen.add(name)
        token = scan.tokens[name]
        if token.declarations > 1:
            return None  # more than one value (a theme override): which one applies is not read here
        target = styles.alias_of(token.value)
        if target is None:
            return parse_colour(token.value)
        name = target
    return None


def contrast_pairs(scan: styles.StyleScan) -> dict[str, list[dict[str, Any]]]:
    """For each text token: the background tokens it is paired with in one rule, with the ratio."""
    out: dict[str, list[dict[str, Any]]] = {}
    for rule in scan.rules:
        fg = rule.declarations.get("color")
        bg = rule.declarations.get("background-color") or rule.declarations.get("background")
        if fg is None or bg is None:
            continue
        text_token, back_token = styles.alias_of(fg[0]), styles.alias_of(bg[0])
        if text_token is None or back_token is None or text_token not in scan.tokens or back_token not in scan.tokens:
            continue
        a, b = _token_colour(scan, text_token), _token_colour(scan, back_token)
        if a is None or b is None:
            continue
        pair = {"background": back_token, "ratio": contrast_ratio(a, b), "selector": rule.selector}
        if pair not in out.setdefault(text_token, []):
            out[text_token].append(pair)
    return {k: sorted(v, key=lambda p: (p["background"], p["selector"]))[:10] for k, v in out.items()}


# ------------------------------------------------------------------ coverage


def accessibility_coverage(shards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`accessibility` coverage, one entry per platform whose controls carry accessibility
    facts: controls with an accessible name over controls. The detail counts name sources,
    screens with positive tabindex or skipped heading levels, and contrast pairs below 4.5:1."""
    from repo_intel.compile.contracts import CoverageEntry
    from repo_intel.compile.ontology import EntityKind

    by_platform: dict[str, dict[str, Any]] = {}
    for name in sorted(shards):
        for e in shards[name]["entities"]:
            facts = e["attributes"].get("accessibility")
            if not isinstance(facts, dict):
                continue
            platform = e["attributes"].get("platform") or "unknown"
            row = by_platform.setdefault(platform, {"named": 0, "controls": 0, "sources": {}, "screens": 0, "positive_tabindex": 0, "heading_skips": 0})
            if e["kind"] == EntityKind.UI_CONTROL:
                source = facts.get("name_source", "none")
                row["controls"] += 1
                row["named"] += source != "none"
                row["sources"][source] = row["sources"].get(source, 0) + 1
            elif e["kind"] == EntityKind.SCREEN:
                row["screens"] += 1
                row["positive_tabindex"] += bool(facts.get("positive_tabindex"))
                row["heading_skips"] += bool(facts.get("heading_skips"))
    low: dict[str, list[str]] = {}
    pairs: dict[str, int] = {}
    for name in sorted(shards):
        for e in shards[name]["entities"]:
            for pair in e["attributes"].get("contrast") or [] if e["kind"] == EntityKind.STYLE_TOKEN else []:
                platform = e["attributes"].get("platform") or "unknown"
                pairs[platform] = pairs.get(platform, 0) + 1
                if pair.get("ratio", 0) < 4.5:
                    low.setdefault(platform, []).append(f"{e['natural_key']['system']} {e['name']} on {pair['background']}")
    out = []
    for platform, row in sorted(by_platform.items()):
        if not row["controls"]:
            continue
        out.append(CoverageEntry(
            domain="accessibility", scope=platform, numerator=row["named"], denominator=row["controls"],
            detail={"name_sources": dict(sorted(row["sources"].items())), "screens": row["screens"],
                    "screens_with_positive_tabindex": row["positive_tabindex"], "screens_with_heading_skips": row["heading_skips"],
                    "contrast_pairs": pairs.get(platform, 0), "contrast_below_4_5": sorted(low.get(platform, []))[:50]},
        ).row())
    return out
