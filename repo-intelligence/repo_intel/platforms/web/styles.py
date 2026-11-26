"""A linear scanner for CSS, SCSS and LESS: design tokens, references to them, imports
and the at-rules a stylesheet uses.

One pass over the text tracks strings, comments, interpolation (`#{...}`, `@{...}`),
parentheses and the block stack, and hands each statement (up to `;`, `{` or `}`)
to a small classifier. Nothing is evaluated: a token's value is copied only when it
is short (see `safe_value`), and its category is read from its name and value.

Tokens:
  CSS custom property  `--brand: #0a6;` anywhere (the first declaration wins; the
                       selectors that redeclare it are listed)
  SCSS variable        `$brand: #0a6;` at the top level only (inside a block it is local)
  LESS variable        `@brand: #0a6;` at the top level only
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_VALUE = 64
_VAR_REF = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
_SCSS_REF = re.compile(r"(?<![\w$-])\$([A-Za-z_][\w-]*)")
_LESS_REF = re.compile(r"@\{?([A-Za-z_][\w-]*)\}?")
_DECL = re.compile(r"^(--[A-Za-z0-9_-]+|\$[A-Za-z_][\w-]*|@[A-Za-z_][\w-]*)\s*:")
_QUOTED = re.compile(r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)|['"]([^'"]+)['"]""")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_FN = re.compile(r"^(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\(", re.I)
_LENGTH = re.compile(r"^-?\d*\.?\d+(?:px|rem|em|%|vh|vw|vmin|vmax|pt|ch|ex|fr)$")  # a bare number (a z-index, an opacity) is not a length
_NAMED_COLORS = frozenset({
    "black", "white", "red", "green", "blue", "gray", "grey", "orange", "yellow", "purple", "pink", "brown", "navy", "teal",
    "transparent", "currentcolor", "silver", "maroon", "olive", "lime", "aqua", "fuchsia", "crimson", "gold", "indigo", "coral",
})
_LESS_AT_RULES = frozenset({"media", "import", "plugin", "charset", "namespace", "supports", "font-face", "keyframes", "page", "layer", "container", "property", "document"})
_SECRET_RUN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


@dataclass
class Token:
    name: str  # "--brand", "$brand", "@brand"
    value: str
    start: int
    end: int
    dialect: str  # css | scss | less
    selectors: list[str] = field(default_factory=list)
    declarations: int = 1


@dataclass
class Rule:
    """A style rule's selector and its plain declarations (not custom properties or variables)."""

    selector: str
    start: int
    end: int = 0
    declarations: dict[str, tuple[str, int, int]] = field(default_factory=dict)  # property -> (value, start, end); the last one wins


@dataclass
class StyleScan:
    tokens: dict[str, Token] = field(default_factory=dict)
    refs: list[tuple[str, int, int]] = field(default_factory=list)  # (token name, start, end)
    imports: list[tuple[str, str, int, int]] = field(default_factory=list)  # (rule, specifier, start, end)
    constructs: list[tuple[str, int, int]] = field(default_factory=list)  # (construct id, start, end)
    rules: list[Rule] = field(default_factory=list)  # style rules in document order (at-rule blocks are not rules)


_ALIAS = (
    re.compile(r"^var\(\s*(--[A-Za-z0-9_-]+)\s*\)$"),  # --a: var(--b)
    re.compile(r"^(?:[A-Za-z_][\w-]*\.)?(\$[A-Za-z_][\w-]*)$"),  # $a: $b, or $a: tokens.$b after @use
    re.compile(r"^(@[A-Za-z_][\w-]*)$"),  # LESS @a: @b
)


def alias_of(value: str) -> str | None:
    """The token a value is defined as, when the whole value is one token reference
    (`var(--b)`, `$b`, `ns.$b`, `@b`); None for anything else, a fallback included."""
    v = " ".join(value.split())
    for pattern in _ALIAS:
        m = pattern.match(v)
        if m and m.group(1)[1:].lower() not in _LESS_AT_RULES:
            return m.group(1)
    return None


def dialect_of(path_or_lang: str) -> str:
    p = path_or_lang.lower()
    if p.endswith((".scss", ".sass")) or p in ("scss", "sass"):
        return "scss"
    if p.endswith(".less") or p == "less":
        return "less"
    return "css"


def safe_value(value: str) -> str | None:
    """The value to record, or None: too long, multi-line, or carrying a long opaque run."""
    v = " ".join(value.split())
    if not v or len(v) > MAX_VALUE or "url(" in v.lower() or _SECRET_RUN.search(v):
        return None
    return v


def category(name: str, value: str | None) -> str:
    """color, size, spacing, radius, font, shadow or other, from the token's name and value."""
    n = name.lower().lstrip("-$@").replace("_", "-")
    v = (value or "").strip().lower()
    if "radius" in n or "radii" in n or "rounded" in n:
        return "radius"
    if "shadow" in n or "elevation" in n:
        return "shadow"
    if "font-size" in n or "fontsize" in n or "text-size" in n:
        return "size"
    if "font" in n or "family" in n or "typeface" in n or "weight" in n or "line-height" in n or "lineheight" in n or "letter" in n:
        return "font"
    if any(w in n for w in ("space", "spacing", "gap", "margin", "padding", "gutter", "inset")):
        return "spacing"
    if _HEX.match(v) or _COLOR_FN.match(v) or v in _NAMED_COLORS:
        return "color"
    if any(w in n for w in ("color", "colour", "palette", "bg", "background", "foreground", "fill", "stroke", "brand", "accent")):
        return "color"
    if _LENGTH.match(v) or any(w in n for w in ("size", "width", "height", "breakpoint", "screen")):
        return "size"
    return "other"


def scan(text: str, dialect: str, base: int = 0) -> StyleScan:
    out = StyleScan()
    n = len(text)
    stack: list[str] = []
    rules: list[Rule | None] = []  # the rule each open block is, None for an at-rule block
    i = 0
    stmt = 0
    paren = 0
    line_comments = dialect in ("scss", "less")
    while i < n:
        c = text[i]
        if c in "\"'":
            close = text.find(c, i + 1)
            while close > 0 and text[close - 1] == "\\":
                close = text.find(c, close + 1)
            i = n if close < 0 else close + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if text[stmt:i].strip() == "":
                stmt = n if close < 0 else close + 2
            i = n if close < 0 else close + 2
            continue
        if line_comments and c == "/" and i + 1 < n and text[i + 1] == "/" and paren == 0 and (i == 0 or text[i - 1] != ":"):
            close = text.find("\n", i)
            if text[stmt:i].strip() == "":
                stmt = n if close < 0 else close + 1
            i = n if close < 0 else close + 1
            continue
        if c == "{" and i > 0 and text[i - 1] in "#@":  # interpolation, not a block
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            i = j
            continue
        if c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)
        elif c == "{":
            _prelude(out, text, stmt, i, base, dialect)
            stack.append(text[stmt:i].strip()[:80])
            selector = " ".join(text[stmt:i].split())[:80]
            rule = Rule(selector, base + _trimmed(text, stmt, i)[0]) if selector and not selector.startswith("@") else None
            if rule is not None:
                out.rules.append(rule)
            rules.append(rule)
            stmt, paren = i + 1, 0
        elif c == ";" or c == "}":
            _statement(out, text, stmt, i, base, stack, dialect, rules[-1] if rules else None)
            if c == "}" and stack:
                stack.pop()
                closed = rules.pop() if rules else None
                if closed is not None:
                    closed.end = base + i + 1
            stmt, paren = i + 1, 0
        i += 1
    _statement(out, text, stmt, n, base, stack, dialect, rules[-1] if rules else None)
    return out


def _trimmed(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


def _prelude(out: StyleScan, text: str, a: int, b: int, base: int, dialect: str) -> None:
    a, b = _trimmed(text, a, b)
    if a >= b:
        return
    s = text[a:b]
    low = s.lower()
    at = (base + a, base + b)
    if low.startswith("@media"):
        out.constructs.append(("web:media-query", *at))
    elif "keyframes" in low.split(" ", 1)[0]:
        out.constructs.append(("web:keyframes", *at))
    elif low.startswith("@font-face"):
        out.constructs.append(("web:font-face", *at))
    elif low.startswith("@mixin") or low.startswith("@include"):
        out.constructs.append(("web:scss-mixin", *at))
    _refs(out, s, base + a, dialect)


def _statement(out: StyleScan, text: str, a: int, b: int, base: int, stack: list[str], dialect: str, rule: Rule | None = None) -> None:
    a, b = _trimmed(text, a, b)
    if a >= b:
        return
    s = text[a:b]
    low = s.lower()
    at = (base + a, base + b)
    if low.startswith(("@import", "@use", "@forward")):
        rule = low.split(None, 1)[0][1:]
        for m in _QUOTED.finditer(s):
            spec = m.group(1) or m.group(2)
            out.imports.append((rule, spec, base + a + m.start(), base + a + m.end()))
        return
    if low.startswith("@include"):
        out.constructs.append(("web:scss-mixin", *at))
        _refs(out, s, base + a, dialect)
        return
    if low.startswith("@tailwind") or low.startswith("@apply"):
        out.constructs.append(("web:tailwind", *at))
        return
    m = _DECL.match(s)
    if m:
        name = m.group(1)
        value_at = m.end()
        value = s[value_at:].replace("!default", "").replace("!global", "").strip()
        if name.startswith("--"):
            _declare(out, name, value, at, "css", stack)
        elif name.startswith("$") and dialect == "scss" and not stack:
            _declare(out, name, value, at, "scss", stack)
        elif name.startswith("@") and dialect == "less" and not stack and name[1:].lower() not in _LESS_AT_RULES:
            _declare(out, name, value, at, "less", stack)
        _refs(out, s[value_at:], base + a + value_at, dialect)
        return
    colon = s.find(":")
    if colon > 0:
        if rule is not None:
            prop = s[:colon].strip().lower()
            if re.fullmatch(r"[a-z-]+", prop):
                rule.declarations[prop] = (" ".join(s[colon + 1:].replace("!important", "").split()), base + a, base + b)
        _refs(out, s[colon + 1:], base + a + colon + 1, dialect)


def _declare(out: StyleScan, name: str, value: str, at: tuple[int, int], dialect: str, stack: list[str]) -> None:
    construct = {"css": "web:css-custom-property", "scss": "web:scss-variable", "less": "web:less-variable"}[dialect]
    out.constructs.append((construct, *at))
    selector = stack[-1] if stack else ""
    known = out.tokens.get(name)
    if known is None:
        out.tokens[name] = Token(name=name, value=value, start=at[0], end=at[1], dialect=dialect, selectors=[selector] if selector else [])
        return
    known.declarations += 1
    if selector and selector not in known.selectors and len(known.selectors) < 5:
        known.selectors.append(selector)


def _refs(out: StyleScan, s: str, offset: int, dialect: str = "css") -> None:
    for m in _VAR_REF.finditer(s):
        out.refs.append((m.group(1), offset + m.start(), offset + m.end()))
    if dialect == "scss":
        for m in _SCSS_REF.finditer(s):
            out.refs.append(("$" + m.group(1), offset + m.start(), offset + m.end()))
    elif dialect == "less":
        for m in _LESS_REF.finditer(s):
            if m.group(1).lower() not in _LESS_AT_RULES:
                out.refs.append(("@" + m.group(1), offset + m.start(), offset + m.end()))
