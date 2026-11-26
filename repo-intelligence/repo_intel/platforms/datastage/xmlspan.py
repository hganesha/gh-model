"""A small XML tree that remembers where each element is in the file's bytes.

`xml.etree` drops positions, so evidence could only cite a whole file. This builds the
tree with expat, which reports the byte offset of every start tag: an element's span runs
from its `<` to the `>` that closes it. Documents in UTF-16 are read through a UTF-8 copy,
and their elements then carry the whole file's span (`exact` is False).

Documents with a DOCTYPE or entity declarations are refused, and so are documents with
more than `max_elements` elements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.parsers import expat

MAX_ELEMENTS = 200_000
_DECLARATIONS = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)


class XmlError(ValueError):
    pass


@dataclass
class Node:
    tag: str  # as written, with any prefix: "talendfile:ProcessType"
    attrs: dict[str, str]
    start: int
    end: int = 0
    text: str = ""
    children: list["Node"] = field(default_factory=list)

    @property
    def local(self) -> str:
        return self.tag.rsplit(":", 1)[-1].rsplit("}", 1)[-1]

    def get(self, name: str, default: str | None = None) -> str | None:
        """An attribute by name, ignoring case and any prefix."""
        if name in self.attrs:
            return self.attrs[name]
        low = name.lower()
        for k, v in self.attrs.items():
            if k.rsplit(":", 1)[-1].lower() == low:
                return v
        return default

    def find(self, local: str) -> list["Node"]:
        low = local.lower()
        return [c for c in self.children if c.local.lower() == low]

    def first(self, local: str) -> "Node | None":
        found = self.find(local)
        return found[0] if found else None

    def iter(self):
        yield self
        for c in self.children:
            yield from c.iter()


def _tag_end(data: bytes, start: int) -> int:
    """The offset just past the `>` that closes the tag starting at `start`, honouring quotes."""
    quote = None
    i = start
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if quote:
            if c == quote:
                quote = None
        elif c in (b'"', b"'"):
            quote = c
        elif c == b">":
            return i + 1
        i += 1
    return n


def parse(data: bytes, max_elements: int = MAX_ELEMENTS) -> tuple[Node, bool]:
    """The root element and whether its spans are exact byte offsets into `data`."""
    exact = True
    raw = data
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or (len(raw) > 1 and raw[1:2] == b"\x00" and raw[:1] == b"<"):
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-16-le")
        except UnicodeDecodeError as exc:
            raise XmlError("not decodable") from exc
        raw = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿")).encode("utf-8")
        exact = False
    if _DECLARATIONS.search(raw):
        raise XmlError("a DOCTYPE or entity declarations")
    parser = expat.ParserCreate()
    parser.buffer_text = True
    try:
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except AttributeError:  # pragma: no cover - older expat
        pass
    stack: list[Node] = []
    roots: list[Node] = []
    count = [0]

    def start(tag, attrs):
        count[0] += 1
        if count[0] > max_elements:
            raise XmlError(f"more than {max_elements} elements")
        node = Node(tag, dict(attrs), parser.CurrentByteIndex)
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(tag):
        node = stack.pop()
        i = parser.CurrentByteIndex
        if raw.startswith(b"</", i):
            node.end = _tag_end(raw, i)
        else:  # an empty element (`<a/>`): expat reports the offset just past it
            node.end = i if i > node.start else _tag_end(raw, node.start)

    def chars(text):
        if stack:
            stack[-1].text += text

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise XmlError(f"not well-formed: {expat.ErrorString(exc.code)} at line {exc.lineno}") from exc
    if not roots:
        raise XmlError("no root element")
    root = roots[0]
    if not exact:
        for n in root.iter():
            n.start, n.end = 0, len(data)
    return root, exact
