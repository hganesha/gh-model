"""Reading ETL exports: XML with the byte span of every element, and zip members within bounds.

Shared by the SSIS pack and the Informatica pack (`repo_intel.platforms.informatica`
imports it inside this package's namespace; nothing here imports `repo_intel.extractors`).

`parse(data)` reads one XML document from its bytes:

- a document whose DOCTYPE has an internal subset, or that declares an entity, is refused
  (an external DOCTYPE, as `<!DOCTYPE POWERMART SYSTEM "powrmart.dtd">`, is kept and never
  fetched);
- at most `MAX_ELEMENTS` elements;
- every element's byte span `[start, end)` is found by a tokenizer over the same bytes, in
  the order `ElementTree.iter()` visits elements, so evidence points at the exact element.
  For an encoding whose markup is not ASCII-compatible (UTF-16) no spans are found and
  callers fall back to the whole file.

`ZipReader` opens an archive (an SSIS `.ispac`) from the file's bytes, never on disk, and
reads members within `MAX_ENTRIES`, `MAX_MEMBER` and `MAX_TOTAL`. Each member read is scanned
for secrets and masked, as admission does for ordinary files (admission cannot see inside a
compressed archive). A `Place` maps offsets in a member to the archive's bytes: exact for a
member stored without compression, the member's whole range otherwise.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

MAX_ELEMENTS = 200_000
MAX_ENTRIES = 2000
MAX_MEMBER = 4_000_000
MAX_TOTAL = 16_000_000

_TOKEN = re.compile(
    rb"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE[^>\[]*>|</[^>]*>"
    rb"|<[A-Za-z_][^\s/>]*(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*\s*/?>",
    re.S,
)


@dataclass
class Doc:
    root: ET.Element
    spans: dict[int, tuple[int, int]] = field(default_factory=dict)

    def span(self, el: ET.Element | None) -> tuple[int, int] | None:
        return self.spans.get(id(el)) if el is not None else None


def local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def attr(el: ET.Element | None, name: str) -> str | None:
    """An attribute by its local name (`DTS:ObjectName` and `ObjectName` alike), case-sensitive."""
    if el is None:
        return None
    if name in el.attrib:
        return el.attrib[name]
    for k, v in el.attrib.items():
        if local(k) == name:
            return v
    return None


def children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if local(c.tag) == name] if el is not None else []


def child(el: ET.Element | None, name: str) -> ET.Element | None:
    return next(iter(children(el, name)), None)


def descendants(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el.iter() if local(c.tag) == name] if el is not None else []


def _encoding(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or (len(data) > 1 and data[1:2] == b"\x00"):
        return "utf-16"
    m = re.match(rb"\s*<\?xml[^>]*encoding\s*=\s*[\"']([\w.:-]+)[\"']", data.lstrip(b"\xef\xbb\xbf"))
    return m.group(1).decode("ascii").lower() if m else "utf-8"


def element_spans(data: bytes) -> list[tuple[int, int]] | None:
    """Spans of the elements of `data` in document order (start tags), or None when the
    markup does not balance."""
    out: list[list[int]] = []
    stack: list[int] = []
    for m in _TOKEN.finditer(data):
        tok = m.group()
        if tok.startswith((b"<!", b"<?")):
            continue
        if tok.startswith(b"</"):
            if not stack:
                return None
            out[stack.pop()][1] = m.end()
            continue
        out.append([m.start(), m.end()])
        if not tok.endswith(b"/>"):
            stack.append(len(out) - 1)
    if stack:
        return None
    return [(a, b) for a, b in out]


def parse(data: bytes) -> tuple[Doc | None, str | None]:
    """The document, or None and why it was not read."""
    encoding = _encoding(data)
    probe = data.decode("utf-16", errors="replace") if encoding == "utf-16" else data.decode("latin-1")
    if re.search(r"<!ENTITY", probe, re.I) or re.search(r"<!DOCTYPE[^>]*\[", probe, re.I):
        return None, "XML with an internal DTD or entity declarations is not read"
    try:
        parser = ET.XMLParser()
        if encoding == "utf-16":
            text = re.sub(r"^﻿?\s*<\?xml[^>]*\?>", "", data.decode("utf-16"))
            root = ET.fromstring(text, parser=parser)
        else:
            root = ET.fromstring(data, parser=parser)
    except (ET.ParseError, LookupError, UnicodeDecodeError, ValueError) as exc:
        return None, f"not well-formed XML: {type(exc).__name__}"
    elements = list(root.iter())
    if len(elements) > MAX_ELEMENTS:
        return None, f"more than {MAX_ELEMENTS} elements: not read"
    doc = Doc(root)
    if encoding != "utf-16":
        spans = element_spans(data)
        if spans is not None and len(spans) == len(elements):
            doc.spans = {id(el): s for el, s in zip(elements, spans)}
    return doc, None


def head_text(data: bytes, limit: int = 4096) -> str:
    """The first bytes of a file as text, for recognising a format by its root element."""
    raw = data[:limit]
    if _encoding(raw) == "utf-16":
        return raw.decode("utf-16", errors="ignore")
    return raw.decode("latin-1")


# ------------------------------------------------------------------ zip archives


@dataclass
class Place:
    """Where one member's bytes are in the archive (or the file itself, for a plain file)."""

    start: int
    end: int
    exact: bool  # a member stored without compression, or a plain file: offsets map one to one

    def at(self, a: int, b: int) -> tuple[int, int]:
        if self.exact:
            return self.start + a, self.start + b
        return self.start, self.end


@dataclass
class Member:
    name: str
    data: bytes
    place: Place


class ZipReader:
    """Members of an archive read from its bytes, within bounds; `notes` says what was skipped."""

    def __init__(self, data: bytes):
        self.data = data
        self.notes: list[tuple[str, str]] = []
        self.total = 0
        self.zf: zipfile.ZipFile | None = None
        self.infos: list[zipfile.ZipInfo] = []

    def open(self) -> str | None:
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(self.data))
        except (zipfile.BadZipFile, OSError, ValueError):
            return "not a zip archive"
        self.infos = self.zf.infolist()
        if len(self.infos) > MAX_ENTRIES:
            return f"archive has more than {MAX_ENTRIES} members: not read"
        return None

    def names(self) -> list[str]:
        return sorted(i.filename for i in self.infos if not i.is_dir())

    def _range(self, info: zipfile.ZipInfo) -> tuple[int, int]:
        h = info.header_offset
        header = self.data[h:h + 30]
        if len(header) < 30 or header[:4] != b"PK\x03\x04":
            return 0, len(self.data)
        start = h + 30 + int.from_bytes(header[26:28], "little") + int.from_bytes(header[28:30], "little")
        return start, min(len(self.data), start + info.compress_size)

    def read(self, name: str) -> Member | None:
        from repo_intel.ingest import secrets

        info = next((i for i in self.infos if i.filename == name), None)
        if info is None or info.is_dir() or self.zf is None:
            return None
        if info.flag_bits & 0x1:
            self.notes.append(("info", f"member {name} is encrypted: not read"))
            return None
        start, end = self._range(info)
        stored = info.compress_type == zipfile.ZIP_STORED
        if stored:
            # The stored bytes as admitted (masking keeps their length; a CRC check would reject it).
            raw = self.data[start:min(end, start + MAX_MEMBER + 1)]
        else:
            try:
                with self.zf.open(info) as f:
                    raw = f.read(MAX_MEMBER + 1)
            except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError) as exc:
                self.notes.append(("warning", f"member {name} cannot be read: {type(exc).__name__}"))
                return None
        if len(raw) > MAX_MEMBER:
            self.notes.append(("info", f"member {name} is larger than {MAX_MEMBER} bytes: not read"))
            return None
        self.total += len(raw)
        if self.total > MAX_TOTAL:
            self.notes.append(("info", f"archive holds more than {MAX_TOTAL} bytes of members: the rest is not read"))
            return None
        raw = secrets.mask_for_extraction(raw, secrets.scan(raw))
        return Member(name, raw, Place(start, end, stored))

    def close(self) -> None:
        if self.zf is not None:
            self.zf.close()
