"""Cut a designated document into blocks (contract 9.1), from the file's bytes alone.

A block is `{kind, text, at, heading_path}` with `kind` one of `heading`, `paragraph`,
`item`, `table_row`, `code`; `at` is `[start_byte, end_byte]` in the file; table rows
carry `cells` and, from a spreadsheet, `sheet` and `cell` (the row's first cell, `A2`).
Two keys are for the feature readers only: `table` numbers the rows of one table, and
`info` is a fenced code block's language. Blocks are never stored; features cite them
by span.

Formats, by the language `repo_intel.extractors.detect_language` gives the path:

- `markdown`: ATX and underlined headings, list items with their continuation lines,
  pipe tables (the separator row is skipped), fenced code, paragraphs.
- `text`: paragraphs and list items; a line underlined with `===` or `---` is a heading.
- `csv`: one table; the delimiter is sniffed among `, ; tab |`. Its heading is the file
  name without extension.
- `html`: `h1`-`h6`, `p`, `li`, `pre`, table rows (`td`/`th` text as cells) and the direct
  text of containers; `script`, `style`, `head` and `template` are skipped.
- `docx`: `word/document.xml`, read with a scanner over its tags, never an XML parser:
  heading paragraphs by style (`styles.xml` names, `Heading n`, `Title`) or outline level,
  list paragraphs by numbering or a list style, table rows with one cell per `w:tc`.
- `xlsx`: every sheet in workbook order, each row a table row of cached cell values.
  Formulas are never evaluated: a formula cell with no cached value reads as empty and is
  counted. A blank row starts a new table; a table's first row with one filled cell above
  more rows is a heading. A sheet's heading is its name.

Zip containers are opened in memory and read within bounds, as `platforms/sas/egp.py`
reads `.egp` files: at most MAX_MEMBERS members, MAX_MEMBER bytes from one and MAX_TOTAL
in all; encrypted members are not read. Each member is secret-scanned and masked before
it is read. A member stored without compression gives exact spans; a compressed one
gives the member's bytes in the archive as the span of every block it holds.

`pdf` and images are not read: `read` returns the export the owner should commit instead.
"""

from __future__ import annotations

import csv
import html
import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from typing import Any

MAX_BLOCKS = 20000
MAX_MEMBERS = 5000
MAX_MEMBER = 4_000_000
MAX_TOTAL = 16_000_000
MAX_ROWS = 20000  # per sheet
MAX_COLUMNS = 200
MAX_TEXT = 4000  # characters kept of one block

READABLE = ("markdown", "text", "csv", "html", "docx", "xlsx")
EXPORT_NEEDED = "Markdown, DOCX or text"


class Unreadable(Exception):
    """The document cannot be read; the message says why."""


def export_needed(language: str | None) -> str | None:
    """The export to commit in place of a file that is not read (a PDF, an image, any other
    format), or None when it is read."""
    return None if language in READABLE else EXPORT_NEEDED


def _clean(text: str) -> str:
    text = " ".join(text.split())
    return text[:MAX_TEXT]


def _block(kind: str, text: str, at: tuple[int, int] | list[int], heading_path: list[str], **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "text": _clean(text), "at": [int(at[0]), int(at[1])], "heading_path": list(heading_path), **{k: v for k, v in extra.items() if v is not None}}


class _Headings:
    """The heading path at the current position: one title per level."""

    def __init__(self, base: list[str] | None = None):
        self.base = list(base or [])
        self.levels: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        self.levels = [(lv, t) for lv, t in self.levels if lv < level] + [(level, title)]

    def path(self) -> list[str]:
        return self.base + [t for _, t in self.levels]


def _lines(data: bytes) -> list[tuple[int, int, str]]:
    """(start byte, end byte without the line break, decoded text) per line."""
    out = []
    pos = 0
    for raw in data.splitlines(keepends=True):
        body = raw.rstrip(b"\r\n")
        out.append((pos, pos + len(body), body.decode("utf-8", "replace")))
        pos += len(raw)
    return out


# ------------------------------------------------------------------ markdown and text

_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)\s*$")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d{1,9}[.)])\s+(.*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def read_markdown(data: bytes, plain: bool = False) -> list[dict[str, Any]]:
    lines = _lines(data)
    heads = _Headings()
    blocks: list[dict[str, Any]] = []
    para: list[tuple[int, int, str]] = []
    item: list[tuple[int, int, str]] | None = None
    tables = 0
    in_table = False

    def flush() -> None:
        nonlocal para, item
        if para:
            blocks.append(_block("paragraph", " ".join(t.strip().lstrip(">").strip() for _, _, t in para), (para[0][0], para[-1][1]), heads.path()))
            para = []
        if item:
            blocks.append(_block("item", " ".join(t.strip() for _, _, t in item), (item[0][0], item[-1][1]), heads.path()))
            item = None

    i = 0
    while i < len(lines) and len(blocks) < MAX_BLOCKS:
        start, end, text = lines[i]
        fence = None if plain else _FENCE.match(text)
        if fence:
            flush()
            in_table = False
            mark, info = fence.group(1), fence.group(2).lower()
            j = i + 1
            while j < len(lines) and not lines[j][2].strip().startswith(mark):
                j += 1
            inner = lines[i + 1:j]
            close = lines[j][1] if j < len(lines) else (inner[-1][1] if inner else end)
            code = {"kind": "code", "text": "\n".join(t for _, _, t in inner)[:MAX_TEXT], "at": [start, close], "heading_path": heads.path()}
            blocks.append(code | ({"info": info} if info else {}))
            i = j + 1
            continue
        atx = None if plain else _ATX.match(text)
        if atx:
            flush()
            in_table = False
            heads.push(len(atx.group(1)), _clean(atx.group(2)))
            blocks.append(_block("heading", atx.group(2), (start, end), heads.path()[:-1]))
            i += 1
            continue
        if text.strip() and i + 1 < len(lines) and _UNDERLINE.match(lines[i + 1][2]) and not para and item is None and not _ITEM.match(text) and not text.lstrip().startswith("|"):
            flush()
            in_table = False
            heads.push(1 if lines[i + 1][2].strip().startswith("=") else 2, _clean(text))
            blocks.append(_block("heading", text, (start, lines[i + 1][1]), heads.path()[:-1]))
            i += 2
            continue
        if not plain and text.lstrip().startswith("|"):
            flush()
            if _TABLE_SEP.match(text):
                i += 1
                continue
            if not in_table:
                tables += 1
                in_table = True
            cells = _cells(text)
            blocks.append(_block("table_row", " | ".join(cells), (start, end), heads.path(), cells=[_clean(c) for c in cells], table=tables))
            i += 1
            continue
        in_table = False
        if not text.strip():
            flush()
            i += 1
            continue
        bullet = _ITEM.match(text)
        if bullet:
            flush()
            item = [(start, end, bullet.group(2))]
            i += 1
            continue
        if item is not None and (text.startswith(("  ", "\t"))):
            item.append((start, end, text))
            i += 1
            continue
        if item is not None:
            flush()
        para.append((start, end, text))
        i += 1
    flush()
    return blocks


def read_text(data: bytes) -> list[dict[str, Any]]:
    return read_markdown(data, plain=True)


# ------------------------------------------------------------------ csv

def read_csv(data: bytes, path: str) -> list[dict[str, Any]]:
    lines = _lines(data)
    sample = "\n".join(t for _, _, t in lines[:50])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    consumed = [0]

    def feed():
        for n, (_, _, t) in enumerate(lines):
            consumed[0] = n + 1
            yield t

    heading = [posixpath.splitext(posixpath.basename(path))[0]]
    blocks: list[dict[str, Any]] = []
    previous = 0
    try:
        for row in csv.reader(feed(), delimiter=delimiter):
            first, last = previous, consumed[0]
            previous = last
            if len(blocks) >= MAX_BLOCKS:
                break
            cells = [_clean(c) for c in row[:MAX_COLUMNS]]
            if not any(cells) or first >= len(lines):
                continue
            at = (lines[first][0], lines[max(first, last - 1)][1])
            blocks.append(_block("table_row", " | ".join(cells), at, heading, cells=cells, table=1))
    except csv.Error as exc:
        raise Unreadable(f"not CSV: {exc}") from None
    return blocks


# ------------------------------------------------------------------ html

_HTML_BLOCKS = {"p", "li", "pre", "dt", "dd", "caption", "figcaption", "blockquote", "div", "section", "article", "main", "header", "footer", "aside", "nav", "body", "td", "th", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
_HTML_SKIP = {"script", "style", "head", "template", "noscript", "svg"}
_HTML_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _byte_offsets(text: str) -> Any:
    """Character offset -> byte offset in the file, for text decoded with `surrogateescape`."""
    if text.isascii():
        return lambda i: i
    cumulative = [0]
    for ch in text:
        cumulative.append(cumulative[-1] + (1 if "\udc80" <= ch <= "\udcff" else len(ch.encode("utf-8"))))
    return lambda i: cumulative[min(max(i, 0), len(text))]


class _HtmlBlocks(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.size = len(text)
        self.heads = _Headings()
        self.blocks: list[dict[str, Any]] = []
        self.open: list[dict[str, Any]] = []  # open collectors: tag, start, text parts, cells
        self.skip = 0
        self.tables = 0
        self.table_stack: list[int] = []

    def _pos(self) -> int:
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def _cell_open(self) -> dict[str, Any] | None:
        return next((c for c in reversed(self.open) if c["tag"] in ("td", "th")), None)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _HTML_SKIP:
            self.skip += 1
            return
        if self.skip or tag in _HTML_VOID:
            if tag == "br" and self.open:
                self.open[-1]["text"].append(" ")
            return
        if tag == "table":
            self.tables += 1
            self.table_stack.append(self.tables)
            return
        if tag not in _HTML_BLOCKS:
            return
        if tag == "p" and self.open and self.open[-1]["tag"] == "p":
            self._close(len(self.open) - 1, self._pos())
        if tag in ("td", "th"):
            row = next((c for c in reversed(self.open) if c["tag"] == "tr"), None)
            if row is None:
                return
        elif self._cell_open() is not None:
            return  # text inside a table cell belongs to the cell
        self.open.append({"tag": tag, "start": self._pos(), "text": [], "cells": [] if tag == "tr" else None, "table": self.table_stack[-1] if self.table_stack else 0})

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "table":
            if self.table_stack:
                self.table_stack.pop()
            return
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i]["tag"] == tag:
                self._close(i, self._pos() + len(tag) + 3)
                return

    def handle_data(self, data: str) -> None:
        if self.skip or not self.open:
            return
        self.open[-1]["text"].append(data)

    def _close(self, i: int, end: int) -> None:
        while len(self.open) > i:
            c = self.open.pop()
            self._emit(c, end)

    def _emit(self, c: dict[str, Any], end: int) -> None:
        text = _clean("".join(c["text"]))
        tag = c["tag"]
        if tag in ("td", "th"):
            row = next((r for r in reversed(self.open) if r["tag"] == "tr"), None)
            if row is not None:
                row["cells"].append(text)
            return
        if len(self.blocks) >= MAX_BLOCKS:
            return
        at = (c["start"], end)
        if tag == "tr":
            if any(row for row in c["cells"]):
                self.blocks.append({"kind": "table_row", "text": " | ".join(c["cells"]), "at": list(at), "heading_path": self.heads.path(), "cells": c["cells"][:MAX_COLUMNS], "table": c["table"]})
            return
        if not text:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.blocks.append({"kind": "heading", "text": text, "at": list(at), "heading_path": self.heads.path()})
            self.heads.push(int(tag[1]), text)
        elif tag == "li":
            self.blocks.append({"kind": "item", "text": text, "at": list(at), "heading_path": self.heads.path()})
        elif tag == "pre":
            self.blocks.append({"kind": "code", "text": "".join(c["text"])[:MAX_TEXT], "at": list(at), "heading_path": self.heads.path()})
        else:
            self.blocks.append({"kind": "paragraph", "text": text, "at": list(at), "heading_path": self.heads.path()})

    def finish(self) -> list[dict[str, Any]]:
        self.close()
        self._close(0, self.size)
        return self.blocks


def read_html(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8", "surrogateescape")
    parser = _HtmlBlocks(text)
    parser.feed(text)
    blocks = parser.finish()
    to_byte = _byte_offsets(text)
    for b in blocks:
        b["at"] = [to_byte(b["at"][0]), to_byte(min(b["at"][1], len(text)))]
        b["text"] = b["text"].encode("utf-8", "replace").decode("utf-8")
        if "cells" in b:
            b["cells"] = [c.encode("utf-8", "replace").decode("utf-8") for c in b["cells"]]
    blocks.sort(key=lambda b: (b["at"][0], b["at"][1]))
    return blocks


# ------------------------------------------------------------------ zip containers

class _Archive:
    """A zip container read from the file's bytes within bounds (see the module docstring)."""

    def __init__(self, data: bytes):
        self.data = data
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError, ValueError):
            raise Unreadable("not a zip container") from None
        infos = self.zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise Unreadable(f"the archive has more than {MAX_MEMBERS} members")
        self.names = {i.filename: i for i in infos}
        self.total = 0

    def range(self, info: zipfile.ZipInfo) -> tuple[int, int]:
        h = info.header_offset
        header = self.data[h:h + 30]
        if len(header) < 30 or header[:4] != b"PK\x03\x04":
            return 0, len(self.data)
        start = h + 30 + int.from_bytes(header[26:28], "little") + int.from_bytes(header[28:30], "little")
        return start, min(len(self.data), start + info.compress_size)

    def read(self, name: str) -> tuple[bytes, int, int, bool] | None:
        """(masked bytes, start, end, stored) of one member, or None when it is absent or not read."""
        from repo_intel.ingest import secrets

        info = self.names.get(name)
        if info is None or info.is_dir():
            return None
        if info.flag_bits & 0x1:
            raise Unreadable(f"member {name} is encrypted")
        start, end = self.range(info)
        stored = info.compress_type == zipfile.ZIP_STORED
        if stored:
            raw = self.data[start:min(end, start + MAX_MEMBER + 1)]
        else:
            try:
                with self.zf.open(info) as f:
                    raw = f.read(MAX_MEMBER + 1)
            except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError) as exc:
                raise Unreadable(f"member {name} cannot be read: {type(exc).__name__}") from None
        if len(raw) > MAX_MEMBER:
            raise Unreadable(f"member {name} is larger than {MAX_MEMBER} bytes")
        self.total += len(raw)
        if self.total > MAX_TOTAL:
            raise Unreadable(f"the archive holds more than {MAX_TOTAL} bytes of members")
        return secrets.mask_for_extraction(raw, secrets.scan(raw)), start, end, stored


def _xml_text(raw: bytes) -> str:
    return html.unescape(raw.decode("utf-8", "replace"))


def _attr(attrs: bytes, name: bytes) -> str | None:
    m = re.search(rb"(?:^|\s)" + re.escape(name) + rb'\s*=\s*"([^"]*)"', attrs) or re.search(rb"(?:^|\s)" + re.escape(name) + rb"\s*=\s*'([^']*)'", attrs)
    return _xml_text(m.group(1)) if m else None


_DOCX_TAG = re.compile(rb"<(/?)(w:(?:p|pStyle|numPr|outlineLvl|t|tab|br|cr|tbl|tr|tc))\b([^>]*?)(/?)>")


def _docx_styles(archive: _Archive) -> dict[str, str]:
    member = archive.read("word/styles.xml")
    if member is None:
        return {}
    out = {}
    for m in re.finditer(rb"<w:style\b([^>]*)>(.*?)</w:style>", member[0], re.S):
        sid = _attr(m.group(1), b"w:styleId")
        name = re.search(rb'<w:name\s+w:val="([^"]*)"', m.group(2))
        if sid and name:
            out[sid] = _xml_text(name.group(1))
    return out


def _heading_level(style: str | None, names: dict[str, str], outline: int | None) -> int | None:
    if outline is not None:
        return outline + 1
    if not style:
        return None
    for candidate in (names.get(style, ""), style):
        low = candidate.lower().replace(" ", "")
        if low == "title":
            return 1
        m = re.fullmatch(r"heading(\d)", low)
        if m:
            return int(m.group(1))
    return None


def read_docx(data: bytes) -> list[dict[str, Any]]:
    archive = _Archive(data)
    member = archive.read("word/document.xml")
    if member is None:
        raise Unreadable("no word/document.xml in the archive")
    xml, mstart, mend, stored = member
    names = _docx_styles(archive)
    heads = _Headings()
    blocks: list[dict[str, Any]] = []

    def span(a: int, b: int) -> tuple[int, int]:
        return (mstart + a, mstart + b) if stored else (mstart, mend)

    depth = 0  # table nesting
    row: dict[str, Any] | None = None
    cell: list[str] | None = None
    para: dict[str, Any] | None = None
    in_text = False
    text_from = 0
    tables = 0
    for m in _DOCX_TAG.finditer(xml):
        closing, tag, attrs, selfclose = m.group(1) == b"/", m.group(2), m.group(3), m.group(4) == b"/"
        if tag == b"w:t":
            if not closing and not selfclose:
                in_text, text_from = True, m.end()
            elif closing and in_text:
                in_text = False
                if para is not None:
                    para["text"].append(_xml_text(xml[text_from:m.start()]))
            continue
        if tag in (b"w:tab", b"w:br", b"w:cr"):
            if para is not None and not closing:
                para["text"].append(" ")
            continue
        if tag == b"w:p":
            if closing:
                if para is None:
                    continue
                text = _clean("".join(para["text"]))
                if cell is not None and depth > 0:
                    if text:
                        cell.append(text)
                elif text and len(blocks) < MAX_BLOCKS:
                    level = _heading_level(para["style"], names, para["outline"])
                    at = span(para["start"], m.end())
                    style_name = (names.get(para["style"] or "", "") + " " + (para["style"] or "")).lower()
                    if level is not None:
                        blocks.append(_block("heading", text, at, heads.path()))
                        heads.push(level, text)
                    elif para["numbered"] or "list" in style_name:
                        blocks.append(_block("item", text, at, heads.path()))
                    elif "code" in style_name or "source" in style_name:
                        blocks.append(_block("code", text, at, heads.path()))
                    else:
                        blocks.append(_block("paragraph", text, at, heads.path()))
                para = None
            elif selfclose:
                continue
            else:
                para = {"start": m.start(), "text": [], "style": None, "numbered": False, "outline": None}
            continue
        if tag == b"w:pStyle" and para is not None:
            para["style"] = _attr(attrs, b"w:val")
            continue
        if tag == b"w:numPr" and para is not None and not closing:
            para["numbered"] = True
            continue
        if tag == b"w:outlineLvl" and para is not None:
            v = _attr(attrs, b"w:val")
            para["outline"] = int(v) if v and v.isdigit() and int(v) < 9 else None
            continue
        if tag == b"w:tbl":
            if closing:
                depth = max(0, depth - 1)
            elif not selfclose:
                depth += 1
                if depth == 1:
                    tables += 1
            continue
        if tag == b"w:tr" and depth == 1:
            if closing and row is not None:
                cells = row["cells"][:MAX_COLUMNS]
                if any(cells) and len(blocks) < MAX_BLOCKS:
                    blocks.append(_block("table_row", " | ".join(cells), span(row["start"], m.end()), heads.path(), cells=cells, table=tables))
                row = None
            elif not closing and not selfclose:
                row = {"start": m.start(), "cells": []}
            continue
        if tag == b"w:tc" and depth == 1 and row is not None:
            if closing and cell is not None:
                row["cells"].append(_clean(" ".join(cell)))
                cell = None
            elif not closing and not selfclose:
                cell = []
            continue
    return blocks


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n - 1


def _letters(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def read_xlsx(data: bytes) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Blocks of every sheet, and counts: `formulas_without_value`."""
    archive = _Archive(data)
    workbook = archive.read("xl/workbook.xml")
    if workbook is None:
        raise Unreadable("no xl/workbook.xml in the archive")
    rels_member = archive.read("xl/_rels/workbook.xml.rels")
    targets: dict[str, str] = {}
    for m in re.finditer(rb"<Relationship\b([^>]*?)/?>", rels_member[0] if rels_member else b""):
        rid, target = _attr(m.group(1), b"Id"), _attr(m.group(1), b"Target")
        if rid and target:
            targets[rid] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    shared: list[str] = []
    strings = archive.read("xl/sharedStrings.xml")
    for m in re.finditer(rb"<si\b[^>]*>(.*?)</si>", strings[0] if strings else b"", re.S):
        body = re.sub(rb"<rPh\b.*?</rPh>", b"", m.group(1), flags=re.S)
        shared.append("".join(_xml_text(t) for t in re.findall(rb"<t\b[^>]*>(.*?)</t>", body, re.S)))
    blocks: list[dict[str, Any]] = []
    counts = {"formulas_without_value": 0}
    tables = 0
    for sm in re.finditer(rb"<sheet\b([^>]*?)/?>", workbook[0]):
        name = _attr(sm.group(1), b"name") or "sheet"
        rid = _attr(sm.group(1), b"r:id")
        target = targets.get(rid or "")
        member = archive.read(target) if target else None
        if member is None:
            continue
        xml, mstart, mend, stored = member
        group: list[dict[str, Any]] = []

        def close_group() -> None:
            nonlocal tables
            if not group:
                return
            tables += 1
            head = [name]
            rows = list(group)
            first = rows[0]
            if len(rows) > 1 and sum(1 for c in first["cells"] if c) == 1:
                title = next(c for c in first["cells"] if c)
                blocks.append(_block("heading", title, first["at"], [name], sheet=name, cell=first["cell"]))
                head = [name, _clean(title)]
                rows = rows[1:]
            for r in rows:
                blocks.append(_block("table_row", " | ".join(r["cells"]), r["at"], head, cells=r["cells"], sheet=name, cell=r["cell"], table=tables))
            group.clear()

        seen_rows = 0
        last_row = 0
        for rm in re.finditer(rb"<row\b([^>]*?)(?:/>|>(.*?)</row>)", xml, re.S):
            seen_rows += 1
            if seen_rows > MAX_ROWS or len(blocks) >= MAX_BLOCKS:
                break
            number_text = _attr(rm.group(1), b"r")
            number = int(number_text) if number_text and number_text.isdigit() else last_row + 1
            values: dict[int, str] = {}
            first_ref = None
            for cm in re.finditer(rb"<c\b([^>]*?)(?:/>|>(.*?)</c>)", rm.group(2) or b"", re.S):
                ref = _attr(cm.group(1), b"r") or ""
                col = _column(ref) if ref else len(values)
                if col < 0 or col >= MAX_COLUMNS:
                    continue
                kind = _attr(cm.group(1), b"t") or "n"
                inner = cm.group(2) or b""
                v = re.search(rb"<v\b[^>]*>(.*?)</v>", inner, re.S)
                if kind == "inlineStr":
                    value = "".join(_xml_text(t) for t in re.findall(rb"<t\b[^>]*>(.*?)</t>", inner, re.S))
                elif v is None:
                    value = ""
                    if re.search(rb"<f\b", inner):
                        counts["formulas_without_value"] += 1
                elif kind == "s":
                    idx = _xml_text(v.group(1)).strip()
                    value = shared[int(idx)] if idx.isdigit() and int(idx) < len(shared) else ""
                elif kind == "b":
                    value = "TRUE" if _xml_text(v.group(1)).strip() == "1" else "FALSE"
                else:
                    value = _xml_text(v.group(1))
                value = _clean(value)
                if value:
                    values[col] = value
                    if first_ref is None or col < _column(first_ref):
                        first_ref = ref or f"{_letters(col)}{number}"
            gap = number > last_row + 1
            last_row = number
            if not values:
                close_group()
                continue
            if gap and group:
                close_group()
            cells = [values.get(c, "") for c in range(max(values) + 1)]
            at = (mstart + rm.start(), mstart + rm.end()) if stored else (mstart, mend)
            group.append({"cells": cells, "at": at, "cell": first_ref or f"{_letters(min(values))}{number}"})
        close_group()
    return blocks, counts


# ------------------------------------------------------------------ dispatch

def read(path: str, language: str | None, data: bytes) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The blocks of one document and reading counts; Unreadable when it cannot be read."""
    if language == "markdown":
        return read_markdown(data), {}
    if language == "text":
        return read_text(data), {}
    if language == "csv":
        return read_csv(data, path), {}
    if language == "html":
        return read_html(data), {}
    if language == "docx":
        return read_docx(data), {}
    if language == "xlsx":
        return read_xlsx(data)
    raise Unreadable(f"{language or 'this format'} is not read; commit it as {EXPORT_NEEDED}")
