"""Character-level scanning shared by the CFML markup and script parsers.

CFML strings double their quote to escape it (`'it''s'`) and, wherever an
expression is evaluated, hold `#expression#` interpolations, with `##` for a
literal `#`. An interpolation may itself hold quoted strings (`"#fmt("x")#"`),
so finding where a string ends means finding where each of its interpolations
ends. Every function here moves forward only: scanning is linear in the text.
"""

from __future__ import annotations

import re

# Where a string scan has to stop and look: its own quote, or a `#`.
_STOP_HASH = {'"': re.compile(r'["#]'), "'": re.compile(r"['#]")}
_STOP_PLAIN = {'"': re.compile(r'"'), "'": re.compile(r"'")}
_HASH_STOP = re.compile(r"[#\"']")

# A string part: ("lit", start, end, text) for literal text (escapes undone), or
# ("expr", start, end, None) for the expression inside a `#...#`, without the hashes.
Part = tuple[str, int, int, "str | None"]


def scan_string(text: str, i: int, end: int, hashes: bool = True) -> tuple[int, list[Part], bool]:
    """Scan the quoted string starting at `text[i]` (a quote).

    Returns (index after the closing quote, parts, terminated). An unterminated
    string runs to `end`."""
    quote = text[i]
    stop = (_STOP_HASH if hashes else _STOP_PLAIN)[quote]
    parts: list[Part] = []
    buf: list[str] = []
    seg = j = i + 1

    def flush(upto: int) -> None:
        text_ = "".join(buf)
        if text_:
            parts.append(("lit", seg, upto, text_))
        buf.clear()

    while True:
        m = stop.search(text, j, end)
        if m is None:
            buf.append(text[j:end])
            flush(end)
            return end, parts, False
        k = m.start()
        buf.append(text[j:k])
        c = text[k]
        if c == quote:
            if k + 1 < end and text[k + 1] == quote:
                buf.append(quote)
                j = k + 2
                continue
            flush(k)
            return k + 1, parts, True
        # c == "#"
        if k + 1 < end and text[k + 1] == "#":
            buf.append("#")
            j = k + 2
            continue
        close = scan_hash(text, k + 1, end)
        if close < 0:
            buf.append("#")
            j = k + 1
            continue
        flush(k)
        parts.append(("expr", k + 1, close, None))
        j = close + 1
        seg = j


def scan_hash(text: str, i: int, end: int) -> int:
    """Index of the `#` that closes an interpolation whose expression starts at `i`, or -1.

    Quoted strings inside the expression are skipped whole, so CFML's
    `"#f("x")#"`, which nests the same quote, ends at the right place."""
    if i >= end or text[i] in " \t\r\n<>":
        return -1  # `# ` or `#>` is a literal hash (a colour, an anchor), not an interpolation
    j = i
    while True:
        m = _HASH_STOP.search(text, j, end)
        if m is None:
            return -1
        k = m.start()
        if text[k] == "#":
            return k if k > i else -1
        nested_end, _, ok = scan_string(text, k, end, hashes=True)
        if not ok:
            return -1
        j = nested_end


def hash_parts(text: str, a: int, b: int) -> list[Part]:
    """Literal text and `#expression#` parts of text[a:b], as evaluated inside `cfoutput`."""
    parts: list[Part] = []
    buf: list[str] = []
    seg = j = a
    while True:
        k = text.find("#", j, b)
        if k < 0:
            buf.append(text[j:b])
            break
        buf.append(text[j:k])
        if k + 1 < b and text[k + 1] == "#":
            buf.append("#")
            j = k + 2
            continue
        close = scan_hash(text, k + 1, b)
        if close < 0:
            buf.append("#")
            j = k + 1
            continue
        if buf and "".join(buf):
            parts.append(("lit", seg, k, "".join(buf)))
        buf.clear()
        parts.append(("expr", k + 1, close, None))
        j = close + 1
        seg = j
    if buf and "".join(buf):
        parts.append(("lit", seg, b, "".join(buf)))
    return parts


def cf_comment_end(text: str, i: int) -> int:
    """End of the CFML comment `<!--- ... --->` opening at `i`. CFML comments nest."""
    depth = 0
    j = i
    pattern = _COMMENT
    while True:
        m = pattern.search(text, j)
        if m is None:
            return len(text)
        if m.group() == "<!---":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.end()
        j = m.end()


_COMMENT = re.compile(r"<!---|--->")


def decode(data: bytes) -> str | None:
    """Source text; legacy CFML is often Windows-1252 rather than UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return None


def byte_offsets(text: str, data: bytes):
    """Character offset -> byte offset for `text` decoded from `data`."""
    if len(text) == len(data):
        return lambda i: i
    prefix = [0]
    for ch in text:
        prefix.append(prefix[-1] + len(ch.encode("utf-8")))
    return lambda i: prefix[min(max(i, 0), len(text))]
