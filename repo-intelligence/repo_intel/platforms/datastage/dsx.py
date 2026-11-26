r"""DataStage export files (`.dsx`), text and XML forms, read into one record model.

The text form, as the DataStage Designer exports it:

    BEGIN HEADER
       CharacterSet "CP1252"
       ...
    END HEADER
    BEGIN DSJOB
       Identifier "LoadCustomers"
       BEGIN DSRECORD
          Identifier "V0S1"
          OLEType "CTransformerStage"
          Name "xfm_clean"
          Columns "COutputColumn"             a collection: the subrecords that follow belong to it
          BEGIN DSSUBRECORD
             Name "CUST_ID"
             Derivation "lnk_src.CUST_ID"
          END DSSUBRECORD
          Source =+=+=+=                      a value of several lines
    ...
    =+=+=+=
       END DSRECORD
    END DSJOB

Values are double-quoted with backslash escapes: `\\`, `\"` and `\(hh)`, a character by
its hexadecimal code (DataStage separates the parts of a parallel stage's property with
`\(2)`, `\(1)` and `\(3)`). A subrecord belongs to the collection named by the last
property written before it in its record.

The XML form (`<DSExport>`, written by the Designer's "export to XML" and inside some
`.isx` archives) holds the same records:

    <DSExport><Header .../>
      <Job Identifier="LoadCustomers">
        <Record Identifier="V0S1" Type="CTransformerStage">
          <Property Name="Name">xfm_clean</Property>
          <Collection Name="Columns" Type="COutputColumn">
            <SubRecord><Property Name="Name">CUST_ID</Property>...</SubRecord>
          </Collection>
        </Record>
      </Job>
      <TableDefinitions><Record ...>...</Record></TableDefinitions>
    </DSExport>

Every value keeps the byte span it came from. The header (server name, tool instance,
dates) is never read beyond the character set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repo_intel.platforms.datastage import xmlspan

MAX_RECORDS = 50_000
MULTILINE = "=+=+=+="

# Section names of the text form and the elements of the XML form, by what they hold.
SECTIONS = {
    "header": "header", "dsjob": "job", "job": "job", "dsshared_container": "shared_container", "dssharedcontainer": "shared_container",
    "sharedcontainer": "shared_container", "dsroutines": "routines", "routines": "routines", "dstabledefs": "table_definitions",
    "tabledefinitions": "table_definitions", "dsexecjob": "compiled_job", "execjob": "compiled_job", "dsstagetypes": "stage_types",
    "stagetypes": "stage_types", "dsdataelements": "data_elements", "dataelements": "data_elements", "dstransforms": "transforms",
    "transforms": "transforms", "dsparametersets": "parameter_sets", "dsparamsets": "parameter_sets", "parametersets": "parameter_sets",
}


@dataclass
class Value:
    text: str
    start: int
    end: int


@dataclass
class Record:
    ident: str
    type: str
    start: int
    end: int
    props: dict[str, Value] = field(default_factory=dict)
    collections: dict[str, list[dict[str, Value]]] = field(default_factory=dict)

    def value(self, *names: str) -> Value | None:
        return lookup(self.props, *names)

    def get(self, *names: str) -> str | None:
        v = self.value(*names)
        return v.text if v is not None else None

    def collection(self, name: str) -> list[dict[str, Value]]:
        low = name.lower()
        for k, v in self.collections.items():
            if k.lower() == low:
                return v
        return []


@dataclass
class Section:
    kind: str  # SECTIONS values, or "other"
    name: str  # as written: DSJOB, Job, ...
    ident: str | None
    start: int
    end: int
    records: list[Record] = field(default_factory=list)


@dataclass
class Export:
    form: str  # "text" or "xml"
    sections: list[Section]
    exact: bool = True  # spans are exact byte offsets (False for a UTF-16 XML export)


class DsxError(ValueError):
    pass


def lookup(props: dict[str, Value], *names: str) -> Value | None:
    for name in names:
        if name in props:
            return props[name]
    lows = {n.lower() for n in names}
    for k, v in props.items():
        if k.lower() in lows:
            return v
    return None


def unescape_codes(raw: str) -> str:
    """Only the `\\(hh)` character codes: the XML form keeps other backslashes as written."""
    return re.sub(r"\\\(([0-9A-Fa-f]{1,6})\)", lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) < 0x110000 else "", raw)


def unescape(raw: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) is not None:
            try:
                return chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return m.group(2)

    return re.sub(r"\\(?:\(([0-9A-Fa-f]{1,6})\)|(.))", sub, raw)


def is_text_export(data: bytes) -> bool:
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"BEGIN HEADER") or head.startswith(b"BEGIN DSJOB")


def is_xml_export(data: bytes) -> bool:
    head = data[:4096]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            head = data[:8192].decode("utf-16").encode("utf-8")
        except UnicodeDecodeError:
            return False
    return b"<DSExport" in head


def parse(data: bytes) -> Export:
    if is_text_export(data):
        return parse_text(data)
    if is_xml_export(data):
        return parse_xml(data)
    raise DsxError("not a DataStage export")


# ------------------------------------------------------------------ text form

_BEGIN = re.compile(r"^[ \t]*BEGIN[ \t]+(\w+)[ \t]*$")
_END = re.compile(r"^[ \t]*END[ \t]+(\w+)[ \t]*$")
_QUOTED = re.compile(r'^[ \t]*([A-Za-z_][\w.]*)[ \t]+"(.*)"[ \t]*$')
_MULTI = re.compile(r"^[ \t]*([A-Za-z_][\w.]*)[ \t]+=\+=\+=\+=[ \t]*$")
_BARE = re.compile(r"^[ \t]*([A-Za-z_][\w.]*)[ \t]+(\S.*?)[ \t]*$")


def _decode(data: bytes) -> tuple[str, list[int] | None]:
    """The text and, when characters are not single bytes, the byte offset of each character."""
    raw = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    skip = len(data) - len(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), [i + skip for i in range(len(raw) + 1)] if skip else None
    if len(text) == len(raw) and not skip:
        return text, None
    offsets = [skip]
    for ch in text:
        offsets.append(offsets[-1] + len(ch.encode("utf-8")))
    return text, offsets


def parse_text(data: bytes) -> Export:
    text, offsets = _decode(data)

    def b(i: int) -> int:
        return offsets[min(i, len(offsets) - 1)] if offsets is not None else i

    sections: list[Section] = []
    section: Section | None = None
    record: Record | None = None
    sub: dict[str, Value] | None = None
    sub_collection = ""
    last_prop = ""
    depth: list[tuple[str, int]] = []  # (block name, start char)
    n_records = 0
    lines = text.splitlines(keepends=True)
    pos = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        start, stripped = pos, line.rstrip("\r\n")
        pos += len(line)
        i += 1
        m = _BEGIN.match(stripped)
        if m:
            name = m.group(1).upper()
            depth.append((name, start))
            if name == "DSRECORD" and section is not None:
                n_records += 1
                if n_records > MAX_RECORDS:
                    raise DsxError(f"more than {MAX_RECORDS} records")
                record = Record("", "", b(start), b(start))
                last_prop = ""
            elif name == "DSSUBRECORD" and record is not None:
                sub = {}
                sub_collection = last_prop
            elif len(depth) == 1:
                section = Section(SECTIONS.get(name.lower(), "other"), name, None, b(start), b(start))
            continue
        m = _END.match(stripped)
        if m:
            name = m.group(1).upper()
            if depth and depth[-1][0] == name:
                depth.pop()
            if name == "DSSUBRECORD" and record is not None and sub is not None:
                record.collections.setdefault(sub_collection or "Items", []).append(sub)
                sub = None
            elif name == "DSRECORD" and record is not None and section is not None:
                record.end = b(pos)
                record.ident = record.get("Identifier") or ""
                record.type = record.get("OLEType") or ""
                section.records.append(record)
                record = None
            elif not depth and section is not None:
                section.end = b(pos)
                sections.append(section)
                section = None
            continue
        key = value = None
        vstart = vend = 0
        m = _MULTI.match(stripped)
        if m:
            key = m.group(1)
            body_start = pos
            chunks = []
            while i < len(lines) and lines[i].strip() != MULTILINE:
                chunks.append(lines[i])
                pos += len(lines[i])
                i += 1
            vstart, vend = body_start, pos
            if i < len(lines):  # the closing marker
                pos += len(lines[i])
                i += 1
            value = "".join(chunks).rstrip("\r\n")
        else:
            m = _QUOTED.match(stripped) or _BARE.match(stripped)
            if m:
                key = m.group(1)
                raw = m.group(2)
                vstart = start + m.start(2)
                vend = start + m.end(2)
                value = unescape(raw) if m.re is _QUOTED else raw
        if key is None or value is None:
            continue
        v = Value(value, b(vstart), b(vend))
        if sub is not None:
            sub.setdefault(key, v)
        elif record is not None:
            record.props.setdefault(key, v)
            last_prop = key
        elif section is not None and key.lower() == "identifier" and section.ident is None:
            section.ident = value
    if section is not None:  # an export cut short: keep what was read
        section.end = len(data)
        sections.append(section)
    return Export("text", sections)


# ------------------------------------------------------------------ XML form


def _props(node: xmlspan.Node) -> dict[str, Value]:
    out: dict[str, Value] = {}
    for p in node.find("Property"):
        name = p.get("Name")
        if name:
            out.setdefault(name, Value(unescape_codes(p.text.strip("\r\n")) if p.text else "", p.start, p.end))
    return out


def parse_xml(data: bytes) -> Export:
    try:
        root, exact = xmlspan.parse(data)
    except xmlspan.XmlError as exc:
        raise DsxError(str(exc)) from exc
    if root.local != "DSExport":
        raise DsxError("not a DataStage export")
    sections: list[Section] = []
    n_records = 0
    for el in root.children:
        kind = SECTIONS.get(el.local.lower(), "other")
        section = Section(kind, el.local, el.get("Identifier"), el.start, el.end)
        records = el.find("Record")
        for r in records:
            n_records += 1
            if n_records > MAX_RECORDS:
                raise DsxError(f"more than {MAX_RECORDS} records")
            rec = Record(r.get("Identifier") or "", r.get("Type") or "", r.start, r.end, _props(r))
            for c in r.find("Collection"):
                name = c.get("Name") or "Items"
                rec.collections.setdefault(name, []).extend(_props(s) for s in c.find("SubRecord"))
            section.records.append(rec)
        sections.append(section)
    return Export("xml", sections, exact)
