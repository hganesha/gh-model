"""URLs in behaviour literals, as rule 7 of the solution IR contract keeps them.

A URL keeps its scheme, host, path and query parameter names; query values,
fragments and user information are dropped, and so are the values of path
parameters (`;jsessionid=...`). `login.cfm?token=abc123` is written
`login.cfm?token=`. A URL built from a template (`"edit.cfm?id=#id#&mode=x"`) keeps
its structure: substituted parts stay expressions, literal values go. A URL whose
scheme has no host (`mailto:`, `tel:`, `javascript:`) keeps only its scheme: what
follows is an address or code.

`scrub_unit` applies this to a lowered behaviour body in place, once, before it is
emitted. Three kinds of value are treated as URLs:

- every value in a URL slot: a redirect's target and any attribute or argument
  named `url`;
- any other string literal or template that reads as a URL (an absolute URL, or a
  relative one to a file with a query string, fragment or path parameter);
- the `href`, `action`, `formaction` and `src` attribute values inside markup a
  literal or template holds (`<a href="edit.cfm?id=#id#&token=abc">` in a
  `cfoutput`).
"""

from __future__ import annotations

import re
from typing import Any

# A token is one literal character or one expression (a dict).
Token = Any

_AUTHORITY = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?//", re.I)
_OPAQUE = re.compile(r"(?:mailto|tel|sms|javascript):", re.I)
# A relative URL worth scrubbing: a file path followed by a query, fragment or path
# parameter; an absolute path followed by one; or a query on its own ("?page=2").
_RELATIVE = re.compile(
    r"(?:\.{1,2}/|/)?(?:[\w.~%-]+/)*[\w~%-][\w.~%-]*\.[a-z][a-z0-9]{0,4}[?#;]"
    r"|/[\w.~%/-]*[?#;]"
    r"|\?[\w.\[\]-]+=",
    re.I,
)
_EXPR_THEN_PATH = re.compile(r"\x00+[/?][^\x00]*\?")  # "#base#/login.cfm?x=1"
_URL_ATTR = re.compile(r"(?<![\w-])(?:href|action|formaction|src)\s*=\s*([\"']?)", re.I)
_URL_KEYS = frozenset({"url"})

PATH, PNAME, PVALUE, NAME, VALUE, FRAGMENT = range(6)


def _tokens(parts: list[Any]) -> list[Token]:
    out: list[Token] = []
    for p in parts:
        if isinstance(p, str):
            out.extend(p)
        else:
            out.append(p)
    return out


def _parts(tokens: list[Token]) -> list[Any]:
    out: list[Any] = []
    buf: list[str] = []
    for t in tokens:
        if isinstance(t, str):
            buf.append(t)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(t)
    if buf:
        out.append("".join(buf))
    return out


def _flat(tokens_or_parts: list[Any]) -> str:
    """Literal text with each expression as one NUL character: for matching only."""
    return "".join(t if isinstance(t, str) else "\x00" for t in tokens_or_parts)


def _scrub_tokens(tokens: list[Token]) -> list[Token]:
    """One URL, as tokens: keep scheme, host, path and parameter names."""
    flat = _flat(tokens)
    i = len(flat) - len(flat.lstrip())
    out: list[Token] = list(tokens[:i])
    rest = flat[i:]
    m = _OPAQUE.match(rest)
    if m:
        return out + list(m.group())
    m = _AUTHORITY.match(rest)
    if m:
        out += tokens[i: i + m.end()]
        i += m.end()
        j = i
        while j < len(tokens) and not (isinstance(tokens[j], str) and tokens[j] in "/?#"):
            j += 1
        authority = tokens[i:j]
        at = max((k for k, t in enumerate(authority) if t == "@"), default=-1)
        out += authority[at + 1:]  # user information is never kept
        i = j
    state = PATH
    for t in tokens[i:]:
        if not isinstance(t, str):
            if state != FRAGMENT:
                out.append(t)  # a substituted part stays an expression
            continue
        if state == FRAGMENT:
            continue
        if t == "#":
            state = FRAGMENT
            continue
        if state == PATH:
            out.append(t)
            state = NAME if t == "?" else PNAME if t == ";" else PATH
        elif state == PNAME:
            out.append(t)
            state = PVALUE if t == "=" else PATH if t == "/" else NAME if t == "?" else PNAME
        elif state == PVALUE:
            if t in ";/?":
                out.append(t)
                state = PNAME if t == ";" else PATH if t == "/" else NAME
        elif state == NAME:
            out.append(t)
            if t == "=":
                state = VALUE
        elif state == VALUE:
            if t == "&":
                out.append(t)
                state = NAME
    return out


def _scrub_markup(tokens: list[Token]) -> list[Token] | None:
    """URL attribute values inside markup, scrubbed; None when there are none."""
    flat = _flat(tokens)
    out: list[Token] = []
    pos = 0
    for m in _URL_ATTR.finditer(flat):
        if m.start() < pos:
            continue
        start, quote = m.end(), m.group(1)
        if quote:
            end = flat.find(quote, start)
            end = len(flat) if end < 0 else end
        else:
            stop = re.compile(r"[\s>]").search(flat, start)
            end = stop.start() if stop else len(flat)
        out += tokens[pos:start]
        out += _scrub_tokens(tokens[start:end])
        pos = end
    if not pos:
        return None
    return out + tokens[pos:]


def looks_like_url(parts: list[Any]) -> bool:
    flat = _flat(parts).lstrip()
    return bool(_AUTHORITY.match(flat) or _OPAQUE.match(flat) or _RELATIVE.match(flat) or _EXPR_THEN_PATH.match(flat))


def scrub_parts(parts: list[Any], url: bool = False) -> list[Any]:
    """A string's parts (literal text and expressions) with URL values taken out.

    `url` says the whole string is a URL; otherwise it is treated as one only if it
    reads as one, and else only URL attributes in markup it holds are scrubbed."""
    flat = _flat(parts)
    if not any(c in flat for c in "?#;@:"):
        return parts  # nothing a URL could hide a value behind
    if url or looks_like_url(parts):
        return _parts(_scrub_tokens(_tokens(parts)))
    if _URL_ATTR.search(flat):
        scrubbed = _scrub_markup(_tokens(parts))
        if scrubbed is not None:
            return _parts(scrubbed)
    return parts


def scrub_url(value: str, url: bool = True) -> str:
    """A literal string with URL values taken out (see `scrub_parts`)."""
    return "".join(scrub_parts([value], url))


def _visit(node: Any, url: bool = False) -> None:
    if isinstance(node, list):
        for item in node:
            _visit(item)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("e")
    if kind == "lit" and isinstance(node.get("value"), str):
        node["value"] = scrub_url(node["value"], url)
        return
    if kind == "template" and isinstance(node.get("parts"), list):
        node["parts"] = scrub_parts(node["parts"], url) or [""]
        for p in node["parts"]:
            if not isinstance(p, str):
                _visit(p)
        return
    redirect = node.get("op") == "redirect"
    for key, value in node.items():
        if key == "at":
            continue
        _visit(value, (redirect and key == "target") or (isinstance(key, str) and key.lower() in _URL_KEYS))


def scrub_unit(body: list[Any], params: list[Any]) -> None:
    """Take URL values out of a behaviour body and its parameters, in place."""
    _visit(body)
    _visit(params)
