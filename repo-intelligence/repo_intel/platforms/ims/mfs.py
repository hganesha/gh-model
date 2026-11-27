"""IMS Message Format Service source: FMT, DEV, DIV, DPAGE, DFLD, FMTEND, MSG, LPAGE, SEG,
MFLD, MSGEND.

A format (FMT) lays out device fields (DFLD) for a device; a message descriptor (MSG)
maps the fields of a message to or from them: an input descriptor (MID, `TYPE=INPUT`)
builds the message the program receives, an output descriptor (MOD, `TYPE=OUTPUT`) shows
the message the program sends. Each MID and MOD is a `Screen {platform: "ims", name}`
(contract 11.1 item 7), with `message_type`, `format`, `devices`, `next` and the number
of message segments in attributes.

Each message field (MFLD) that names a device field of the format becomes a `UIControl
{container: <MID or MOD name>, name: <device field>}` the Screen CONTAINS, with the device
field's `pos`, `length`, `attr` and `control` (`display` when protected, else `input`)
from the format's first device, and the message field's `message_length`, `initial`
(a literal default; not copied, `initial_redacted`, when the field is named for a password,
secret, token or key) and `segment` (1-based). A short literal device field just before a
named one is its `label`, as for BMS. Device fields of later devices repeat the first
device's names and are counted.

A MSG's `NXT=` NAVIGATES_TO the next message descriptor's Screen. An input message whose
first field is a literal starts with the transaction code IMS routes it by, so the MID
NAVIGATES_TO that transaction (`Endpoint`, never declared here) when the literal's first
word is a valid code. A device field filled with a system literal (`(TODAY,DATE2)`) keeps
the literal's name as `system_literal`. Message fields that map no device field (the
control area, a PF key field, a name the format lacks) and repeated fields (DO ... ENDDO)
are counted and not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.bms.emit import Emitter
from repo_intel.platforms.bms.source import CREDENTIAL, label_text
from repo_intel.platforms.bms.statements import Quoted, Statement, dynamic, first, is_name, number, words
from repo_intel.platforms.ims.psb import endpoint_ref, is_transaction_code
from repo_intel.platforms.ims.vocabulary import PLATFORM

LISTING = frozenset({"PRINT", "TITLE", "EJECT", "SPACE", "END", "ICTL", "PUNCH"})


@dataclass
class _Dfld:
    stmt: Statement
    attrs: dict[str, Any]
    label_stmt: Statement | None = None
    used: bool = False


@dataclass
class _Format:
    stmt: Statement
    name: str
    devices: list[str] = field(default_factory=list)
    fields: dict[str, _Dfld] = field(default_factory=dict)
    later: list[Statement] = field(default_factory=list)  # named device fields of the second and later devices
    pfk: str | None = None


@dataclass
class _Mfld:
    stmt: Statement
    name: str | None
    literal: str | None
    system: bool  # no device field: the scratch pad or control area (SCA), or nothing
    length: int | None
    system_literal: str | None = None  # DATE2, TIME, LTNAME ... the device field is filled with


@dataclass
class _Message:
    stmt: Statement
    name: str
    type: str | None
    format: str | None
    next: str | None
    segments: list[list[_Mfld]] = field(default_factory=list)
    end: int = 0


def _pos(v: Any) -> Any:
    if isinstance(v, list) and len(v) == 2 and all(number(x) is not None for x in v):
        return [number(v[0]), number(v[1])]
    return number(v)


class MfsReader:
    def __init__(self, em: Emitter):
        self.em = em
        self.formats: dict[str, _Format] = {}
        self.format: _Format | None = None
        self.messages: list[_Message] = []
        self.message: _Message | None = None
        self.pending: tuple[Statement, str] | None = None
        self.literals: list[tuple[Statement, bool]] = []  # literal device fields: used as a label or not
        self.read = 0

    def statement(self, s: Statement) -> None:
        em = self.em
        for o in s.operands:
            if dynamic(o.value):
                em.construct(o.start, o.end, "ims:symbolic_parameter", False)
        op = s.op
        if op == "FMT":
            self.close_format()
            if is_name(s.name) and s.name not in self.formats:
                self.format = self.formats[s.name] = _Format(s, s.name)
                em.construct(s.start, s.end, "ims:format", True)
            else:
                em.construct(s.start, s.end, "ims:format", False)
        elif op == "DEV":
            self.flush_pending()
            fmt = self.format
            dtype = s.value("TYPE")
            if fmt is not None:
                fmt.devices.append(",".join(x for x in words(dtype)) if isinstance(dtype, list) else (dtype if isinstance(dtype, str) else "?"))
                pfk = s.value("PFK")
                if pfk is not None:
                    name = first(pfk)
                    fmt.pfk = name if is_name(name) else fmt.pfk
                    em.construct(s.start, s.end, "ims:pf_keys", False)
            em.construct(s.start, s.end, "ims:device", fmt is not None)
        elif op == "DIV":
            em.construct(s.start, s.end, "ims:division", False)
        elif op == "DPAGE":
            em.construct(s.start, s.end, "ims:device_page", False)
        elif op == "DFLD":
            self.dfld(s)
        elif op == "FMTEND":
            self.close_format()
        elif op == "MSG":
            self.close_message()
            mtype = first(s.value("TYPE"))
            sor = first(s.value("SOR"))
            nxt = first(s.value("NXT"))
            if not is_name(s.name):
                em.note(f"line {s.line}: a MSG without a label: not read", "warning")
                em.construct(s.start, s.end, "ims:output_message" if mtype == "OUTPUT" else "ims:input_message", False)
                return
            self.message = _Message(s, s.name, mtype if mtype in ("INPUT", "OUTPUT") else None, sor if is_name(sor) else None, nxt if is_name(nxt) else None, end=s.end)
        elif op == "SEG":
            if self.message is not None:
                self.message.segments.append([])
                self.message.end = s.end
            em.construct(s.start, s.end, "ims:message_segment", self.message is not None)
        elif op == "LPAGE":
            em.construct(s.start, s.end, "ims:logical_page", False)
        elif op == "MFLD":
            self.mfld(s)
        elif op == "MSGEND":
            self.close_message()
        elif op in ("DO", "ENDDO"):
            if op == "DO":
                em.construct(s.start, s.end, "ims:repeated_fields", False)
        elif op in ("PDB", "PD", "PDBEND"):
            if op != "PDBEND":
                em.construct(s.start, s.end, "ims:partition", False)
        elif op in ("TABLE", "IF", "TABLEEND"):
            if op == "TABLE":
                em.construct(s.start, s.end, "ims:table_edit", False)
        elif op == "COPY":
            em.construct(s.start, s.end, "ims:copy", False)
        elif op not in LISTING:
            em.construct(s.start, s.end, "ims:other_statement", False)

    # ------------------------------------------------------------------ formats

    def dfld(self, s: Statement) -> None:
        fmt = self.format
        if fmt is None:
            self.em.construct(s.start, s.end, "ims:device_field", False)
            return
        lead = s.positional()
        literal = lead[0].value if lead and isinstance(lead[0].value, Quoted) else None
        if not s.name:
            self.flush_pending()
            text = label_text(literal) if len(fmt.devices) <= 1 else None
            if text is not None:
                self.pending = (s, text)
            else:
                self.literals.append((s, False))
            return
        if len(fmt.devices) > 1:
            fmt.later.append(s)
            self.flush_pending()
            return
        if s.name in fmt.fields:
            self.em.note(f"line {s.line}: device field {s.name} is defined twice in format {fmt.name}: the second is not read", "warning")
            fmt.later.append(s)
            self.flush_pending()
            return
        attrs: dict[str, Any] = {}
        pos = _pos(s.value("POS"))
        if pos is not None:
            attrs["pos"] = pos
        length = number(s.value("LTH"))
        if length is not None:
            attrs["length"] = length
        attr = s.value("ATTR")
        if attr is not None and not dynamic(attr):
            attrs["attr"] = words(attr)
        attrs["control"] = "display" if "PROT" in attrs.get("attr", []) else "input"
        if literal is not None:
            if CREDENTIAL.search(s.name):
                attrs["initial_redacted"] = True  # a credential field's default is its value
            else:
                attrs["initial"] = str(literal)
        d = _Dfld(s, attrs)
        if self.pending is not None:
            attrs["label"] = self.pending[1]
            d.label_stmt = self.pending[0]
            self.pending = None
        fmt.fields[s.name] = d

    def flush_pending(self) -> None:
        if self.pending is not None:
            self.literals.append((self.pending[0], False))
            self.pending = None

    def close_format(self) -> None:
        self.flush_pending()
        self.format = None

    # ------------------------------------------------------------------ messages

    def mfld(self, s: Statement) -> None:
        m = self.message
        if m is None:
            self.em.construct(s.start, s.end, "ims:message_field", False)
            return
        if not m.segments:
            m.segments.append([])  # a message with no SEG statement has one segment
        m.end = s.end
        lead = s.positional()
        v = lead[0].value if lead else None
        name = literal = system_literal = None
        if isinstance(v, Quoted):
            literal = str(v)
        elif isinstance(v, list):
            head = v[0] if v else None
            name = head if is_name(head) else None
            second = v[1] if len(v) > 1 else None
            if isinstance(second, Quoted):
                literal = str(second)
            elif is_name(second):
                system_literal = second  # a system literal (DATE2, TIME, LTNAME) or, with no name, the SCA
        elif is_name(v):
            name = v
        m.segments[-1].append(_Mfld(s, name, literal, name is None and literal is None, number(s.value("LTH")), system_literal))

    def close_message(self) -> None:
        if self.message is not None:
            self.messages.append(self.message)
            self.message = None

    # ------------------------------------------------------------------ emission

    def finish(self) -> None:
        self.close_format()
        self.close_message()
        em = self.em
        for m in self.messages:
            s = m.stmt
            fmt = self.formats.get(m.format) if m.format else None
            if m.format and fmt is None:
                em.note(f"message {m.name}: format {m.format} is not in this file: its device fields are not read", "info")
            attrs = {"message_type": m.type.lower() if m.type else None, "format": m.format, "devices": fmt.devices if fmt else None,
                     "next": m.next, "segments": len(m.segments)}
            controls: list[tuple[_Mfld, str, dict[str, Any]]] = []
            seen: dict[str, int] = {}
            for index, seg in enumerate(m.segments, start=1):
                for f in seg:
                    d = fmt.fields.get(f.name) if fmt and f.name else None
                    known = f.name is not None and (fmt is None or d is not None)
                    if not known or (fmt is not None and f.name == fmt.pfk):
                        if f.name is None and f.literal is not None and not f.system:
                            continue  # a literal only: below
                        em.construct(f.stmt.start, f.stmt.end, "ims:system_message_field", False)
                        continue
                    n = seen[f.name] = seen.get(f.name, 0) + 1
                    cname = f.name if n == 1 else f"{f.name}~{n}"
                    cattrs = dict(d.attrs) if d else {}
                    cattrs.update({"message_length": f.length, "segment": index})
                    if f.literal is not None:
                        if CREDENTIAL.search(f.name):
                            cattrs.pop("initial", None)
                            cattrs["initial_redacted"] = True  # a credential field's default is its value
                        else:
                            cattrs["initial"] = f.literal
                    if f.system_literal is not None:
                        cattrs["system_literal"] = f.system_literal
                    if d is not None:
                        d.used = True
                    controls.append((f, cname, cattrs))
            attrs["fields"] = len(controls)
            cid = "ims:output_message" if m.type == "OUTPUT" else "ims:input_message"
            em.construct(s.start, s.end, cid, m.type is not None)
            screen = em.term(s.start, m.end, "ims:Message", {"platform": PLATFORM, "name": m.name}, m.name, attrs)
            for f, cname, cattrs in controls:
                ref = em.term(f.stmt.start, f.stmt.end, "ims:DeviceField", {"container": m.name, "name": cname}, cname, cattrs)
                em.rel(f.stmt.start, f.stmt.end, screen, P.CONTAINS, ref)
                em.construct(f.stmt.start, f.stmt.end, "ims:message_field", True)
            self.transaction_code(m, screen)
            if m.next:
                em.rel(s.start, s.end, screen, P.NAVIGATES_TO, em.ref(K.SCREEN, {"platform": PLATFORM, "name": m.next}, m.next, declare=False), {"via": "next_message"})
                em.construct(s.start, s.end, "ims:next_message", True)
            self.read += 1
        for fmt in self.formats.values():
            for d in fmt.fields.values():
                em.construct(d.stmt.start, d.stmt.end, "ims:device_field", d.used)
                if d.label_stmt is not None:
                    self.literals.append((d.label_stmt, d.used))
            for later in fmt.later:
                em.construct(later.start, later.end, "ims:device_field", False)
        for s, used in sorted(self.literals, key=lambda x: x[0].start):
            em.construct(s.start, s.end, "ims:literal_device_field", used)

    def transaction_code(self, m: _Message, screen: dict[str, Any]) -> None:
        """The transaction code a literal at the start of an input message names."""
        em = self.em
        for index, seg in enumerate(m.segments):
            for position, f in enumerate(seg):
                if f.name is not None or f.literal is None or f.system:
                    continue
                code = f.literal.strip().split(" ")[0] if f.literal.strip() else ""
                first_field = index == 0 and position == 0
                if m.type == "INPUT" and first_field and is_transaction_code(code):
                    em.rel(f.stmt.start, f.stmt.end, screen, P.NAVIGATES_TO, endpoint_ref(em, code.upper(), declare=False), {"via": "transaction_code"})
                    em.construct(f.stmt.start, f.stmt.end, "ims:literal_message_field", True)
                else:
                    em.construct(f.stmt.start, f.stmt.end, "ims:literal_message_field", False)


__all__ = ["MfsReader"]
