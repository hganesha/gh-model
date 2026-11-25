"""A linear scanner for HTML, Vue templates and Angular templates.

It reads tags and attributes in one pass, keeps the content of `<script>` and
`<style>` raw, and builds an element tree with a stack (void elements and
`/>` close themselves; an unmatched closing tag is ignored, an unclosed element
ends at its parent's end). It never runs anything.

Text is decoded as Latin-1, so a character offset is a byte offset into the file;
values are re-decoded as UTF-8 when they are read out.

Attribute names are normalised so that one lookup serves every template syntax:

    href="/x"                     href      literal
    :href="x"  v-bind:href="x"    href      expression   (Vue)
    [routerLink]="['/x']"         routerLink expression  (Angular)
    @submit.prevent="save"        on:submit expression   (Vue)
    (ngSubmit)="save()"           on:ngSubmit expression (Angular)
    [(ngModel)]="order.qty"       ngModel   expression   (Angular two-way)
    v-model="form.qty"            v-model   expression   (Vue)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
RAW = frozenset({"script", "style"})
_RAW_END = {name: re.compile("</" + name, re.I) for name in RAW}
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_SPACE = frozenset(" \t\r\n\f")


def utf8(text: str) -> str:
    """A Latin-1 decoded slice back to the text it is."""
    return text.encode("latin-1").decode("utf-8", "replace")


@dataclass
class Attr:
    key: str  # normalised name (see the module docstring)
    raw: str  # the name as written
    value: str | None  # literal text, or the expression text, or None for a bare attribute
    literal: bool  # True for a plain attribute, False for a bound expression or event
    start: int
    end: int
    node: object | None = None  # the syntax node of a JSX expression, when there is one


@dataclass
class Element:
    tag: str
    attrs: dict[str, Attr]
    start: int  # the '<' of the opening tag
    end: int  # just past the opening tag's '>'
    close: int = -1  # just past the element (its closing tag), or `end`
    parent: "Element | None" = None
    children: list["Element"] = field(default_factory=list)
    text: str = ""  # direct static text, collapsed and capped
    raw: str | None = None  # the content of <script> and <style>
    raw_start: int = 0
    spreads: list[Attr] = field(default_factory=list)  # JSX {...props}

    def get(self, *keys: str) -> Attr | None:
        for k in keys:
            a = self.attrs.get(k)
            if a is not None:
                return a
        return None

    def lit(self, *keys: str) -> str | None:
        """A literal value of the first of `keys` that is a plain attribute."""
        a = self.get(*keys)
        return a.value if a is not None and a.literal and a.value is not None else None

    def has(self, key: str) -> bool:
        return key in self.attrs

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


def normalise(raw: str) -> tuple[str, bool]:
    """(key, literal) for an attribute name as written."""
    if raw.startswith("[(") and raw.endswith(")]"):
        return raw[2:-2], False
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return (inner[5:] if inner.startswith("attr.") else inner), False
    if raw.startswith("(") and raw.endswith(")"):
        return "on:" + raw[1:-1], False
    if raw.startswith("v-bind:"):
        return raw[7:].split(".")[0], False
    if raw.startswith(":") and len(raw) > 1:
        return raw[1:].split(".")[0], False
    if raw.startswith("v-on:"):
        return "on:" + raw[5:].split(".")[0], False
    if raw.startswith("@") and len(raw) > 1:
        return "on:" + raw[1:].split(".")[0], False
    if raw == "v-model" or raw.startswith("v-model:") or raw.startswith("v-model."):
        return "v-model", False
    if raw.startswith("v-") or raw.startswith("*"):
        return raw, False
    return raw, True


def scan(text: str, base: int = 0, lower: bool = False) -> list[Element]:
    """Every element of `text` in document order, linked into a tree. Offsets are
    `base` plus the position in `text`. `lower` lower-cases tag names (plain HTML)."""
    out: list[Element] = []
    stack: list[Element] = []
    n = len(text)
    i = 0
    text_start = 0

    def add_text(a: int, b: int) -> None:
        if stack and b > a:
            el = stack[-1]
            if len(el.text) < 120:
                chunk = " ".join(text[a:b].split())
                if chunk:
                    el.text = (el.text + " " + chunk).strip()[:120]

    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            add_text(text_start, n)
            break
        if text.startswith("<!--", lt):
            add_text(text_start, lt)
            end = text.find("-->", lt + 4)
            i = text_start = n if end < 0 else end + 3
            continue
        if lt + 1 < n and text[lt + 1] in "!?":
            add_text(text_start, lt)
            end = text.find(">", lt + 2)
            i = text_start = n if end < 0 else end + 1
            continue
        closing = lt + 1 < n and text[lt + 1] == "/"
        j = lt + (2 if closing else 1)
        if j >= n or text[j] not in _NAME_START:
            i = lt + 1
            continue
        add_text(text_start, lt)
        k = j
        while k < n and text[k] not in _SPACE and text[k] not in "/>":
            k += 1
        tag = text[j:k]
        if lower:
            tag = tag.lower()
        if closing:
            gt = text.find(">", k)
            end = n if gt < 0 else gt + 1
            for depth in range(len(stack) - 1, max(-1, len(stack) - 65), -1):  # a bounded search keeps malformed input linear
                if stack[depth].tag == tag:
                    for el in stack[depth:]:
                        el.close = base + end
                    del stack[depth:]
                    break
            i = text_start = end
            continue
        attrs, k, self_closing = _attributes(text, k, base)
        el = Element(tag=tag, attrs={}, start=base + lt, end=base + k, parent=stack[-1] if stack else None)
        for a in attrs:
            el.attrs.setdefault(a.key, a)
        if el.parent is not None:
            el.parent.children.append(el)
        out.append(el)
        name = tag.lower()
        if name in RAW and not self_closing:
            m = _RAW_END[name].search(text, k)
            close = n if m is None else m.start()
            el.raw, el.raw_start = text[k:close], base + k
            gt = text.find(">", close)
            el.close = base + (n if gt < 0 else gt + 1)
            i = text_start = n if gt < 0 else gt + 1
            continue
        if self_closing or (name in VOID):
            el.close = el.end
        else:
            stack.append(el)
        i = text_start = k
    for el in stack:
        el.close = base + n
    return out


def _attributes(text: str, k: int, base: int) -> tuple[list[Attr], int, bool]:
    """Attributes from position `k` to the end of the tag; returns them, the position
    just past '>', and whether the tag closed itself."""
    n = len(text)
    attrs: list[Attr] = []
    while k < n:
        while k < n and text[k] in _SPACE:
            k += 1
        if k >= n:
            break
        if text[k] == ">":
            return attrs, k + 1, False
        if text.startswith("/>", k):
            return attrs, k + 2, True
        if text[k] == "/":
            k += 1
            continue
        s = k
        while k < n and text[k] not in _SPACE and text[k] not in "=>" and not text.startswith("/>", k):
            k += 1
        raw = text[s:k]
        while k < n and text[k] in _SPACE:
            k += 1
        value = None
        if k < n and text[k] == "=":
            k += 1
            while k < n and text[k] in _SPACE:
                k += 1
            if k < n and text[k] in "\"'":
                q = text[k]
                close = text.find(q, k + 1)
                close = n if close < 0 else close
                value = text[k + 1:close]
                k = close + 1
            else:
                v = k
                while k < n and text[k] not in _SPACE and text[k] != ">":
                    k += 1
                value = text[v:k]
        if raw:
            key, literal = normalise(raw)
            attrs.append(Attr(key=key, raw=raw, value=utf8(value) if value is not None else None, literal=literal, start=base + s, end=base + k))
    return attrs, n, False


def sfc_blocks(text: str) -> list[tuple[str, dict[str, str], int, int]]:
    """The top-level blocks of a Vue single-file component: (tag, attributes, content
    start, content end). A top-level block opens at the start of a line; the
    template block runs to the last `</template>` before the next block."""
    blocks: list[tuple[str, dict[str, str], int, int]] = []
    starts: list[tuple[str, int, int, dict[str, str]]] = []
    pos = 0
    n = len(text)
    while pos < n:
        line_end = text.find("\n", pos)
        line_end = n if line_end < 0 else line_end
        for tag in ("template", "script", "style"):
            if text.startswith("<" + tag, pos) and pos + len(tag) + 1 < n and text[pos + len(tag) + 1] in " >\t\r\n":
                attrs, end, _ = _attributes(text, pos + len(tag) + 1, 0)
                starts.append((tag, pos, end, {a.raw: (a.value or "") for a in attrs}))
                break
        pos = line_end + 1
    for idx, (tag, at, content, attrs) in enumerate(starts):
        limit = starts[idx + 1][1] if idx + 1 < len(starts) else n
        close = text.rfind("</" + tag, content, limit)
        if close < 0:
            continue
        blocks.append((tag, attrs, content, close))
    return blocks
