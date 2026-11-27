"""The IBM i extractors: `ibmi.rpg` (`.rpg`, `.rpgle`, `.sqlrpgle`), `ibmi.cl` (`.clp`,
`.clle`) and `ibmi.dds` (`.pf`, `.lf`, `.dspf`, `.prtf`).

What they declare (natural keys in `vocabulary`, contract section 11.1):

- an RPG or CL member as `Program {platform: "mainframe", name: <member>}` that CONTAINS a
  `Symbol` for each subroutine and procedure, with a behaviour unit for the program's main
  procedure (or CL body) and one per subroutine (unit kind `paragraph`) and procedure
  (unit kind `function`); an exported procedure's export name as a `Symbol` under program
  `*EXPORT`, ALIAS_OF the procedure;
- CALLS: a program called (`CALL`, `CALLP` of an `EXTPGM` prototype, CL `CALL`, `TFRCTL`,
  `SBMJOB CMD(CALL ...)`) as `Program {platform: "mainframe"}`, `declare=False`; a procedure
  or subroutine of the member exactly; a bound procedure of another module (`CALLB`,
  `CALLPRC`, a prototype's `EXTPROC` literal or name) as its export name's `Symbol`,
  `declare=False`. A target named at run time stays dynamic, with a diagnostic, and is counted;
- a database file the program declares (F-spec, `DCL-F`, CL `DCLF`) READS_FROM and WRITES_TO
  by its usage, and the tables embedded SQL reads and writes (the shared lineage scanner,
  `db2` dialect, contract 11.1 item 12). A program cannot tell a physical from a logical
  file, so a declared file is referred to as both `Table` and logical file `DataSchema`,
  neither declared: whichever the repository's DDS declares links;
- a WORKSTN file RENDERS the display record formats `EXFMT` and `WRITE` show (`Screen`,
  `declare=False`), and the format's `Form` SUBMITS_TO the program when it reads the reply;
  a PRINTER file PRODUCES its `Report`;
- from DDS: a physical file as `Table` CONTAINS its `Field`s, a VALUES list as a `CodeList`
  the field BINDS_TO; a logical file as a `DataSchema` with `based_on`, CONTAINS the fields
  it selects; a display file's record formats as `Screen`s with a `Form` and `UIControl`s
  (row, column, usage); a printer file as a `Report`.

`OVRDBF` and data areas are attributes of the program, never applied to a guessed call.
Parameter values, data area values and message texts are not copied.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from typing import Any

from repo_intel.platforms.ibmi.vocabulary import EXPORT_PROGRAM, LANGUAGE, LF_KIND, PLATFORM, UI_PLATFORM

_TOO_BIG = 4_000_000  # bytes: a source member larger than this is inventoried, not read


def _lib():
    """Extractor helpers, imported when first used: a pack module must not import the
    `repo_intel.extractors` package while it is being discovered."""
    from repo_intel import platforms
    from repo_intel.compile.behavior import behavior_observation
    from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
    from repo_intel.extractors import base, sql_lineage

    return platforms, behavior_observation, EntityKind, EvidenceClass, Predicate, base, sql_lineage


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # one byte per character, so spans stay byte spans


_SECRET_NAME = re.compile(r"passw|pwd|secret|token|api_?key|credential|private_?key", re.I)


def _secret_target(node: Any) -> bool:
    """Whether an assignment target or declared name reads as a credential."""
    if isinstance(node, str):
        return bool(_SECRET_NAME.search(node))
    if isinstance(node, dict) and node.get("e") in ("var", "member"):
        return bool(_SECRET_NAME.search(str(node.get("name", "")))) or _secret_target(node.get("object"))
    return False


def _blank_literals(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _blank_literals(item)
    elif isinstance(node, dict):
        if node.get("e") == "lit" and isinstance(node.get("value"), str):
            node["value"] = "<redacted>"
            return
        for k, v in node.items():
            if k != "at":
                _blank_literals(v)


def scrub_secrets(block: list[Any]) -> None:
    """Take text values out of declarations and assignments of names that read as
    credentials (a constant `DB_PASSWORD`, `token = '...'`), in place."""
    for stmt in block:
        if not isinstance(stmt, dict):
            continue
        if stmt.get("op") == "declare" and _secret_target(stmt.get("name", "")):
            _blank_literals(stmt.get("value"))
        elif stmt.get("op") == "assign" and _secret_target(stmt.get("target")):
            _blank_literals(stmt.get("value"))
        for k in ("then", "else", "body", "default", "finally"):
            if isinstance(stmt.get(k), list):
                scrub_secrets(stmt[k])
        for case in stmt.get("cases", []) or []:
            scrub_secrets(case.get("body", []))
        for catch in stmt.get("catches", []) or []:
            scrub_secrets(catch.get("body", []))


def table_name(name: str) -> str:
    """A file's name as a table: lower case, as the SQL lineage scanner names the table
    embedded SQL reads, so DDS, F-specs and embedded SQL name one table."""
    return name.lower()


class _Emit:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        (self.platforms, self.behavior_observation, self.K, self.EC, self.P, self.base, self.sql) = _lib()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.breaks: list[int] = []
        k = data.find(b"\n")
        while k >= 0:
            self.breaks.append(k)
            k = data.find(b"\n", k + 1)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self.seen: set[tuple] = set()

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def span(self, a: int, b: int):
        """`base.span`'s Span, with line numbers from an index of the line breaks, so a
        member with many observations is not read once per observation."""
        from repo_intel.compile.contracts import Span

        start, end = self.to_byte(a), self.to_byte(max(a, b))
        end = max(start, end)
        return Span(start_byte=start, end_byte=end, start_line=bisect.bisect_left(self.breaks, start) + 1, end_line=bisect.bisect_left(self.breaks, max(start, end - 1)) + 1)

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (attrs or {}).items() if v is not None and v != [] and v != {}}
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, clean, self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, attrs or {}, self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        """A relation, once per (subject, predicate, object, qualifiers): the first site is kept."""
        key = (repr(subject["natural_key"]), subject["kind"], str(predicate), obj["kind"], repr(obj["natural_key"]), repr(sorted((qualifiers or {}).items())))
        if key in self.seen:
            return
        self.seen.add(key)
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, self.EC.E2))

    def construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), cid, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    # -------------------------------------------------------------- references

    def program(self, name: str, declare: bool = False) -> dict[str, Any]:
        return self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": name}, name, declare=declare)

    def symbol(self, program: str, name: str, declare: bool = True) -> dict[str, Any]:
        return self.base.ref(self.K.SYMBOL, {"platform": PLATFORM, "program": program, "name": name}, name, declare=declare)

    def files(self, name: str) -> list[dict[str, Any]]:
        """A file a program opens: the physical file's Table or the logical file, undeclared."""
        n = table_name(name)
        return [self.base.ref(self.K.TABLE, {"datastore": "default", "name": n}, n, declare=False),
                self.base.ref(self.K.DATA_SCHEMA, {"kind": LF_KIND, "name": n}, n, declare=False)]

    def screen(self, file: str, fmt: str) -> dict[str, Any]:
        name = f"{file}.{fmt}"
        return self.base.ref(self.K.SCREEN, {"platform": UI_PLATFORM, "name": name}, name, declare=False)

    def form(self, file: str, fmt: str) -> dict[str, Any]:
        return self.base.ref(self.K.FORM, {"screen": f"{file}.{fmt}", "name": fmt}, fmt, declare=False)

    def report(self, file: str) -> dict[str, Any]:
        return self.base.ref(self.K.REPORT, {"platform": UI_PLATFORM, "name": file}, file, declare=False)

    # -------------------------------------------------------------- shared facts

    def sql_facts(self, owner: dict[str, Any], statements: list) -> None:
        for st, start, end, body_start in statements:
            try:
                found = self.sql.lineage(st.lineage, None, views=False)  # the db2 dialect is the scanner's default
            except Exception as exc:  # a scanner fault on hostile input: the statement is counted, not read
                self.note(f"SQL at line {self.text.count(chr(10), 0, start) + 1} not read: {type(exc).__name__}", "warning")
                self.unresolved += 1
                continue
            self.unresolved += found.unresolved
            for predicate, items in ((self.P.WRITES_TO, found.writes), (self.P.READS_FROM, found.reads)):
                for name, a, b in items:
                    self.resolved += 1
                    a, b = body_start + a, body_start + b
                    if not (start <= a <= b <= end):
                        a, b = start, end
                    target = self.entity(a, b, self.K.TABLE, {"datastore": "default", "name": name}, name)
                    self.rel(a, b, owner, predicate, target, {"via": "sql"})

    def behavior(self, owner: dict[str, Any], body: list, params: list, a: int, b: int, kind: str | None) -> None:
        from repo_intel.platforms.cfml.urls import scrub_unit

        params = [dict(p) for p in params]
        scrub_secrets(body)
        scrub_unit(body, params)
        try:
            self.items.append(self.behavior_observation(self.blob, self.span(a, b), owner, LANGUAGE, body, params, self.EC.E2, kind=kind))
        except ValueError as exc:  # a lowering fault: the unit keeps its relations, its behaviour is left out
            self.note(f"behaviour of {owner['name']} not emitted: {str(exc)[:120]}", "warning")

    def file_relations(self, owner: dict[str, Any], name: str, usage: list[str], a: int, b: int, via: str) -> None:
        reads = "input" in usage
        writes = bool({"output", "update", "delete"} & set(usage))
        q = {"via": via, "usage": sorted(usage)}
        for target in self.files(name):
            if reads:
                self.rel(a, b, owner, self.P.READS_FROM, target, q)
            if writes:
                self.rel(a, b, owner, self.P.WRITES_TO, target, q)

    def finish(self):
        from repo_intel.extractors.base import Extraction

        self.items += self.sql.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return Extraction(self.items, diagnostics=self.diagnostics)


# ------------------------------------------------------------------ RPG


def _emit_rpg(o: _Emit, path: str) -> None:
    from repo_intel.platforms.ibmi.rpg import read_member

    m = read_member(path, o.text, o.at)
    main = m.units[0]
    a, b = 0, min(len(o.text), o.text.find("\n") if o.text.find("\n") > 0 else len(o.text))
    files = [{"name": f.name, "device": f.device.lower(), "usage": f.usage, **({"object": f.object} if f.object != f.name else {}),
              **({"keyed": True} if f.keyed else {}), **({"program_described": True} if not f.external else {}),
              **({"dynamic": True} if f.dynamic else {}), **({"procedure": f.procedure.upper()} if f.procedure else {})} for f in m.files]
    attrs = {"language": "rpg", "rpg_version": m.version, "form": m.form, "sql": m.sql or None, "params": [p["name"] for p in main.params],
             "options": dict(sorted(m.options.items())) or None, "files": files, "copies": sorted(set(m.copies)), "layouts": m.layouts,
             "module": True if m.options.get("nomain") else None}
    me = o.term(a, b, "ibmi:RpgProgram", {"platform": PLATFORM, "name": m.name}, m.name, attrs)
    names = {u.name for u in m.units[1:]}
    exports: dict[str, dict[str, Any]] = {}
    owners: dict[str, dict[str, Any]] = {}
    for u in m.units[1:]:
        if u.kind == "subroutine":
            sattrs = {"symbol_kind": "subroutine", **({"procedure": u.name.split(".")[0]} if "." in u.name else {})}
            sym = o.term(u.start, u.end, "ibmi:Subroutine", {"platform": PLATFORM, "program": m.name, "name": u.name}, u.name, sattrs)
        else:
            sattrs = {"symbol_kind": "procedure", "exported": True if u.export else None, "returns": u.returns, "params": [p["name"] for p in u.params]}
            sym = o.term(u.start, u.end, "ibmi:Procedure", {"platform": PLATFORM, "program": m.name, "name": u.name}, u.name, sattrs)
            if u.export and u.export_name:
                alias = o.term(u.start, u.end, "ibmi:ExportedProcedure", {"platform": PLATFORM, "program": EXPORT_PROGRAM, "name": u.export_name}, u.export_name, {"module": m.name})
                o.rel(u.start, u.end, alias, o.P.ALIAS_OF, sym, {"via": "export"})
                exports[u.export_name] = sym
        o.rel(u.start, u.end, me, o.P.CONTAINS, sym)
        owners[u.name] = sym
    owners[m.name] = me

    # Declared files.
    for f in m.files:
        owner = owners.get(f.procedure.upper(), me) if f.procedure else me
        if f.dynamic:
            continue
        if f.device == "DISK":
            o.construct("ibmi:file_declaration", f.start, f.end, True)
            o.file_relations(owner, f.object, f.usage, f.start, f.end, "file")
            if not f.external:
                o.construct("ibmi:program_described_file", f.start, f.end, True)
        elif f.device == "PRINTER":
            o.construct("ibmi:printer_file", f.start, f.end, True)
            o.rel(f.start, f.end, owner, o.P.PRODUCES, o.report(f.object), {"via": "printer_file"})
        elif f.device == "WORKSTN":
            o.construct("ibmi:workstn_file", f.start, f.end, True)
        else:
            o.construct("ibmi:other_device", f.start, f.end, False)

    # Unit facts and behaviour.
    workstn = [f for f in m.files if f.device == "WORKSTN" and not f.dynamic]
    file_names = {f.name for f in m.files}
    for u in m.units:
        owner = me if u.kind == "main" else owners[u.name]
        for call in u.calls:
            target = None
            if call.kind == "program":
                if call.target != m.name:
                    target = o.program(call.target)
            elif call.kind in ("symbol", "subroutine"):
                target = o.symbol(m.name, call.target, declare=call.target in names)
            elif call.kind == "export":
                target = exports.get(call.target.upper()) or exports.get(call.target) or o.symbol(EXPORT_PROGRAM, call.target, declare=False)
            if target is not None and target != owner:
                o.rel(call.start, call.end, owner, o.P.CALLS, target, {"via": call.via})
        for use in u.files:
            if use.name in file_names or use.op not in ("EXFMT", "WRITE", "READ", "READC"):
                continue
            targets = [f for f in workstn if use.name in f.formats or use.name in f.sfile] or workstn
            for ws in targets:
                if use.op in ("EXFMT", "WRITE"):
                    o.rel(use.start, use.end, me, o.P.RENDERS, o.screen(ws.object, use.name), {"op": use.op.lower()})
                if use.op in ("EXFMT", "READ"):
                    o.rel(use.start, use.end, o.form(ws.object, use.name), o.P.SUBMITS_TO, me, {"op": use.op.lower()})
        o.sql_facts(owner, u.sql)
        if u.kind == "main":
            if u.body or u.params:
                o.behavior(me, u.body, u.params, 0, len(o.text), None)
        else:
            o.behavior(owner, u.body, u.params, u.start, u.end, "paragraph" if u.kind == "subroutine" else "function")
    for cid, ca, cb, modelled in m.constructs:
        o.construct(cid, ca, cb, modelled)
    for severity, message in m.diagnostics:
        o.note(message, severity)


class IbmiRpg:
    id = "ibmi.rpg"
    version = "1.0.0"
    SUFFIXES = (".rpg", ".rpgle", ".sqlrpgle")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        if len(data) > _TOO_BIG:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "source member too large to read")], parsed=False)
        o = _Emit(self.id, path, data, blob, decode(data))
        try:
            _emit_rpg(o, path)
        except RecursionError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "nesting too deep to read")], parsed=False)
        return o.finish()


# ------------------------------------------------------------------ CL


def _emit_cl(o: _Emit, path: str) -> None:
    from repo_intel.compile.transforms import Builder, transform_observation
    from repo_intel.platforms.ibmi.cl import read_member

    m = read_member(path, o.text, o.at)
    main = m.units[0]
    a = next((c[1] for c in m.constructs if c[0] == "ibmi:cl_pgm"), 0)
    b = next((c[2] for c in m.constructs if c[0] == "ibmi:cl_pgm"), a)
    attrs = {"language": "cl", "params": [p["name"] for p in main.params], "files": m.files, "display_files": m.display_files,
             "overrides": m.overrides, "data_areas": m.data_areas, "monmsg": m.monmsg or None}
    me = o.term(a, b, "ibmi:ClProgram", {"platform": PLATFORM, "name": m.name}, m.name, attrs)
    owners = {m.name: me}
    names = {u.name for u in m.units[1:]}
    for u in m.units[1:]:
        sym = o.term(u.start, u.end, "ibmi:Subroutine", {"platform": PLATFORM, "program": m.name, "name": u.name}, u.name, {"symbol_kind": "subroutine"})
        o.rel(u.start, u.end, me, o.P.CONTAINS, sym)
        owners[u.name] = sym
    display = set(m.display_files)
    for u in m.units:
        owner = owners[u.name] if u.kind != "main" else me
        for call in u.calls:
            target = None
            if call.kind == "program" and call.target != m.name:
                target = o.program(call.target)
            elif call.kind == "subroutine":
                target = o.symbol(m.name, call.target, declare=call.target in names)
            elif call.kind == "export":
                target = o.symbol(EXPORT_PROGRAM, call.target, declare=False)
            if target is not None:
                o.rel(call.start, call.end, owner, o.P.CALLS, target, {"via": call.via})
        for f in u.files:
            if f.op == "dclf":
                if f.file not in display:
                    o.file_relations(me, f.file, ["input"], f.start, f.end, "dclf")
            elif f.op in ("sndf", "sndrcvf", "rcvf") and f.file in display:
                if f.format and f.op in ("sndf", "sndrcvf"):
                    o.rel(f.start, f.end, owner, o.P.RENDERS, o.screen(f.file, f.format), {"op": f.op})
                if f.format and f.op in ("sndrcvf", "rcvf"):
                    o.rel(f.start, f.end, o.form(f.file, f.format), o.P.SUBMITS_TO, me, {"op": f.op})
            elif f.op in ("cpyf_from", "opnqryf"):
                o.file_relations(owner, f.file, ["input"], f.start, f.end, f.op.split("_")[0])
            elif f.op in ("cpyf_to", "clrpfm"):
                o.file_relations(owner, f.file, ["output"], f.start, f.end, f.op.split("_")[0])
        if u.copies:
            builder = Builder()
            for src, dst, ca, cb in u.copies:
                builder.write(table_name(src), table_name(dst), o.at(ca, cb))
            try:
                o.items.append(transform_observation(o.blob, o.span(u.copies[0][2], u.copies[-1][3]), owner, LANGUAGE, builder.finish(), o.EC.E2))
            except ValueError as exc:
                o.note(f"transform shapes of {u.name} not emitted: {str(exc)[:120]}", "warning")
        o.sql_facts(owner, u.sql)
        if u.kind == "main":
            o.behavior(me, u.body, u.params, 0, len(o.text), None)
        else:
            o.behavior(owner, u.body, u.params, u.start, u.end, "paragraph")
    for cid, ca, cb, modelled in m.constructs:
        o.construct(cid, ca, cb, modelled)
    for severity, message in m.diagnostics:
        o.note(message, severity)


class IbmiCl:
    id = "ibmi.cl"
    version = "1.0.0"
    SUFFIXES = (".clp", ".clle")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        if len(data) > _TOO_BIG:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "source member too large to read")], parsed=False)
        o = _Emit(self.id, path, data, blob, decode(data))
        _emit_cl(o, path)
        return o.finish()


# ------------------------------------------------------------------ DDS


def _field_attrs(f, dds_mod) -> dict[str, Any]:
    kws = f.keywords
    attrs: dict[str, Any] = {"type": dds_mod.sql_type(f), "dds_type": f.type, "length": f.length, "decimals": f.decimals, "bytes": dds_mod.byte_length(f),
                             "text": dds_mod.text_of(kws), "colhdg": dds_mod.text_of(kws, "COLHDG"), "reference": True if f.reference else None}
    alias = dds_mod.kw(kws, "ALIAS")
    if alias:
        attrs["alias"] = alias.strip().upper()
    checks = [f"{w}({' '.join((args or '').split())})" if args is not None else w for w, args in kws if w in ("RANGE", "CMP", "COMP", "CHECK")]
    if checks:
        attrs["validation"] = checks
    return attrs


def _code_list(o: _Emit, subject: dict[str, Any], layout: str, field: str, f, dds_mod) -> None:
    """A VALUES keyword as `CodeList {platform: "ibmi", name: "<layout>.<field>"}` (11.1 item 4)."""
    raw = dds_mod.kw(f.keywords, "VALUES")
    if raw is None:
        return
    vals = dds_mod.values(raw)
    attrs: dict[str, Any] = {"values": vals[:200], "field": f.name}
    if len(vals) > 200:
        attrs["truncated"] = True
    name = f"{layout}.{field}"
    cl = o.term(f.start, f.end, "ibmi:FieldValues", {"platform": UI_PLATFORM, "name": name}, name, attrs)
    o.rel(f.start, f.end, subject, o.P.BINDS_TO, cl)


def _emit_dds(o: _Emit, path: str) -> None:
    from repo_intel.platforms.ibmi import dds as dds_mod

    d = dds_mod.read(path, o.text)
    ref_file = dds_mod.kw(d.keywords, "REF")
    ref_file = dds_mod.files_named(ref_file)[0] if ref_file and dds_mod.files_named(ref_file) else None
    first = d.records[0] if d.records else None
    a, b = (first.start, first.end) if first else (0, min(len(o.text), 1))
    if d.kind == "pf":
        name = table_name(d.name)
        rec = first
        fields = [f for f in (rec.fields if rec else []) if f.kind == "field"]
        keys = [k for k, _ in (rec.keys if rec else [])]
        attrs = {"record_format": rec.name if rec else None, "unique": True if dds_mod.kw(d.keywords, "UNIQUE") is not None else None,
                 "keys": [k.lower() for k in keys], "text": dds_mod.text_of(rec.keywords) if rec else None, "ref": ref_file.lower() if ref_file else None,
                 "file": d.name}
        if len(d.records) > 1:
            o.note(f"physical file {d.name} declares {len(d.records)} record formats: the first is read", "warning")
        me = o.term(a, b, "ibmi:PhysicalFile", {"datastore": "default", "name": name}, name, attrs)
        offset: int | None = 0
        for f in fields:
            fattrs = _field_attrs(f, dds_mod)
            size = fattrs.get("bytes")
            fattrs["offset"] = offset if offset is not None and size is not None else None
            offset = offset + size if offset is not None and size is not None else None
            if f.name in keys:
                fattrs["key"] = keys.index(f.name) + 1
            fname = f.name.lower()
            fld = o.term(f.start, f.end, "ibmi:Field", {"table": name, "name": fname}, fname, fattrs)
            o.construct("ibmi:dds_field", f.start, f.end, True)
            o.rel(f.start, f.end, me, o.P.CONTAINS, fld)
            _code_list(o, fld, name, fname, f, dds_mod)
            if f.reference:
                rf = dds_mod.kw(f.keywords, "REFFLD")
                file, other = dds_mod.reffld(rf, ref_file) if rf else (ref_file, f.name)
                if file and other:
                    o.rel(f.start, f.end, fld, o.P.BINDS_TO, o.base.ref(o.K.FIELD, {"table": table_name(file), "name": other.lower()}, other.lower(), declare=False), {"via": "reference"})
    elif d.kind == "lf":
        name = table_name(d.name)
        based: list[str] = []
        for rec in d.records:
            for word in ("PFILE", "JFILE"):
                raw = dds_mod.kw(rec.keywords, word)
                if raw:
                    based += [table_name(x) for x in dds_mod.files_named(raw)]
        based = sorted(set(based))
        attrs = {"based_on": based, "record_formats": [r.name for r in d.records], "file": d.name,
                 "keys": sorted({k.lower() for r in d.records for k, _ in r.keys}),
                 "select_omit": [{"type": s["type"], "field": s["field"].lower(), **({"tests": s["tests"]} if s["tests"] else {}), **({"all": True} if s.get("all") else {})} for r in d.records for s in r.select_omit],
                 "join": True if any(r.joins for r in d.records) or any(dds_mod.kw(r.keywords, "JFILE") for r in d.records) else None,
                 "dynamic_select": True if dds_mod.kw(d.keywords, "DYNSLT") is not None else None,
                 "unique": True if dds_mod.kw(d.keywords, "UNIQUE") is not None else None}
        me = o.term(a, b, "ibmi:LogicalFile", {"kind": LF_KIND, "name": name}, name, attrs)
        for rec in d.records:
            pfiles = [table_name(x) for x in dds_mod.files_named(dds_mod.kw(rec.keywords, "PFILE") or "")]
            for f in rec.fields:
                if f.kind != "field":
                    continue
                o.construct("ibmi:dds_lf_field", f.start, f.end, len(pfiles) == 1)
                if len(pfiles) == 1:
                    n = f.name.lower()
                    o.rel(f.start, f.end, me, o.P.CONTAINS, o.base.ref(o.K.FIELD, {"table": pfiles[0], "name": n}, n, declare=False))
    elif d.kind == "dspf":
        file_keys = [w for w, _ in d.keywords if len(w) == 4 and w[:2] in ("CA", "CF") and w[2:].isdigit()]
        for rec in d.records:
            sname = f"{d.name}.{rec.name}"
            rec_keys = [w for w, _ in rec.keywords if len(w) == 4 and w[:2] in ("CA", "CF") and w[2:].isdigit()]
            sflctl = dds_mod.kw(rec.keywords, "SFLCTL")
            window = dds_mod.kw(rec.keywords, "WINDOW")
            named = [f for f in rec.fields if f.kind == "field"]
            constants = sum(1 for f in rec.fields if f.kind == "constant")
            unnamed = sum(1 for f in rec.fields if f.kind == "unnamed")
            attrs = {"file": d.name, "record_format": rec.name, "subfile": True if dds_mod.kw(rec.keywords, "SFL") is not None else None,
                     "subfile_control": sflctl.strip().upper() if sflctl else None, "command_keys": sorted(set(file_keys + rec_keys)),
                     "window": True if window is not None else None, "overlay": True if dds_mod.kw(rec.keywords, "OVERLAY") is not None else None,
                     "constants": constants or None, "unnamed_fields": unnamed or None, "text": dds_mod.text_of(rec.keywords)}
            screen = o.term(rec.start, rec.end, "ibmi:DisplayRecord", {"platform": UI_PLATFORM, "name": sname}, sname, attrs)
            inputs = [f for f in named if f.usage in ("both", "input")]
            form = None
            if inputs:
                form = o.term(rec.start, rec.end, "ibmi:DisplayForm", {"screen": sname, "name": rec.name}, rec.name, {"fields": sorted({f.name for f in inputs})})
                o.rel(rec.start, rec.end, screen, o.P.CONTAINS, form)
            seen: set[str] = set()
            for f in named:
                if f.name in seen:
                    continue
                seen.add(f.name)
                fattrs = {"row": f.row, "column": f.column, "usage": f.usage or "output", "length": f.length, "dds_type": f.type, "decimals": f.decimals,
                          "reference": True if f.reference else None, "conditioned": True if f.conditioned else None}
                ctl = o.term(f.start, f.end, "ibmi:DisplayField", {"container": sname, "name": f.name}, f.name, fattrs)
                o.construct("ibmi:dds_display_field", f.start, f.end, True)
                holder = form if form is not None and f.usage in ("both", "input") else screen
                o.rel(f.start, f.end, holder, o.P.CONTAINS, ctl)
                _code_list(o, ctl, sname, f.name, f, dds_mod)
                if f.reference:
                    rf = dds_mod.kw(f.keywords, "REFFLD")
                    file, other = dds_mod.reffld(rf, ref_file) if rf else (ref_file, f.name)
                    if file and other:
                        o.rel(f.start, f.end, ctl, o.P.BINDS_TO, o.base.ref(o.K.FIELD, {"table": table_name(file), "name": other.lower()}, other.lower(), declare=False), {"via": "reference"})
    else:  # prtf
        formats = [{"name": r.name, "fields": [f.name for f in r.fields if f.kind == "field"]} for r in d.records]
        for r in d.records:
            for f in r.fields:
                if f.kind == "field":
                    o.construct("ibmi:dds_printer_field", f.start, f.end, True)
        o.term(a, b, "ibmi:PrinterFile", {"platform": UI_PLATFORM, "name": d.name}, d.name, {"record_formats": formats, "file": d.name})
    for cid, ca, cb, modelled in d.constructs:
        o.construct(cid, ca, cb, modelled)
    for severity, message in d.diagnostics:
        o.note(message, severity)


class IbmiDds:
    id = "ibmi.dds"
    version = "1.0.0"
    SUFFIXES = (".pf", ".lf", ".dspf", ".prtf")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        if len(data) > _TOO_BIG:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "source member too large to read")], parsed=False)
        o = _Emit(self.id, path, data, blob, decode(data))
        _emit_dds(o, posixpath.normpath(path))
        return Extraction(o.items, diagnostics=o.diagnostics)


__all__ = ["IbmiCl", "IbmiDds", "IbmiRpg", "decode", "scrub_secrets", "table_name"]
