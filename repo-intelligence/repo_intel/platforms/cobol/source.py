"""`cobol.source`: COBOL programs and copybooks (`.cbl`, `.cob`, `.cobol`, `.cpy`).

Supersedes `legacy.cobol` and emits at least what it did, with the same kinds and keys:
each program is `Program {platform: "mainframe", name}` (attributes `language`, `cics`,
`db2`); a `CALL 'X'` and a CICS `LINK` or `XCTL` of a literal program CALL
`Program {platform: "mainframe", name: X}` without declaring it; a `COPY` DEPENDS_ON the
copybook's `DataSchema {kind: "copybook", name}`, which a `.cpy` file declares; embedded
SQL goes through the shared SQL lineage scanner (host variables blanked, so offsets
stay); a CICS `READ`, `WRITE`, `REWRITE`, `DELETE`, `STARTBR`, `READNEXT` or `READPREV` of
a literal file reads or writes `Dataset {name}` with `access: cics_file`.

Beyond that (docs/solution-ir-contract.md, section 11):

- sections and paragraphs are `Symbol`s the Program CONTAINS; PERFORM (every procedure of
  a THRU range), GO TO, SORT input and output procedures, `WHENEVER ... GO TO` and CICS
  `HANDLE` labels CALL them, qualifier `via`;
- one behaviour unit (kind `paragraph`) per paragraph and per section's own statements,
  and one owned by the Program for the statements before the first paragraph
  (`lower.Lowerer`); the compiler derives transform shapes from their SQL; SORT and MERGE
  of named files are transform shapes here (language `cobol`);
- a CALL, LINK or XCTL through a data item is resolved only when the item has exactly one
  literal value in the file (its VALUE clause and the literals MOVEd to it, with nothing
  else written to it or to a group holding it), qualifier `resolved_from`; otherwise it
  stays dynamic, with a diagnostic, and is counted;
- each SELECT is a `dd_file` observation `{program, dd, mode, select, record}` (11.1 item
  5; the mode from the OPEN statements) and an entry of `attributes.files`;
- `SEND MAP` RENDERS `Screen {platform: "bms", name: "<MAPSET>.<MAP>"}` and both send and
  receive are in `attributes.maps`; `START TRANSID` and `RETURN TRANSID` CONSUME the
  transaction `Endpoint` (11.1 items 6 and 7); none of these is declared here;
- DL/I calls give `attributes.dli_segments` (the segment names of literal SSAs) and
  `dli_functions`, MQI calls `attributes.mq_calls` and the literal `mq_queues` (11.1 item 8);
- a copybook's data items are `Field`s its DataSchema CONTAINS, and each field's
  88-levels a `CodeList` the field BINDS_TO (11.1 items 3 and 4).

No value of a data item is copied except the 88-level codes, which are reference data,
and literal program, transaction, map, file and queue names, which are names.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.cobol import lexer
from repo_intel.platforms.cobol.lower import DLI_CALLS, MQ_CALLS, Ctx, Lowerer, sql_statement
from repo_intel.platforms.cobol.parse import DataItem, Exec, File, Program, exec_block, layout, parse
from repo_intel.platforms.cobol.vocabulary import LANGUAGE, PLATFORM

_PROGRAM_NAME = re.compile(r"^[A-Z0-9@#$][A-Z0-9@#$_-]{0,29}$")
_SEGMENT = re.compile(r"^([A-Z#@$][A-Z0-9#@$]{0,7})(?=[ (*]|$)")
_HOST = re.compile(r":[\w-]+(?:\.[\w-]+)?")
_DECLARE_CURSOR = re.compile(r"^\s*DECLARE\s+[\w-]+\s+(?:(?:INSENSITIVE|SENSITIVE\s+STATIC|SENSITIVE\s+DYNAMIC|ASENSITIVE|NO\s+SCROLL|SCROLL)\s+)*CURSOR\s+(?:WITH\s+(?:HOLD|RETURN)\s+(?:TO\s+\w+\s+)?)*FOR\b", re.I)
_MAX_VALUES = 200


def _lib():
    """Extractor helpers, imported when first used: a pack module must not import the
    `repo_intel.extractors` package while it is being discovered."""
    from repo_intel import platforms
    from repo_intel.compile.behavior import behavior_observation
    from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
    from repo_intel.compile.transforms import transform_observation
    from repo_intel.extractors import base, sql_lineage
    from repo_intel.extractors.data_python import dataset_name

    return platforms, behavior_observation, transform_observation, EntityKind, EvidenceClass, Predicate, base, sql_lineage, dataset_name


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")  # legacy sources are often not UTF-8
        except UnicodeDecodeError:
            return None


class _Emit:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        (self.platforms, self.behavior_observation, self.transform_observation, self.K, self.EC, self.P, self.base, self.sql, self.dataset_name) = _lib()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self.seen_rel: set[tuple] = set()

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.to_byte(a), self.to_byte(max(a, b)))

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, {k: v for k, v in (attrs or {}).items() if v is not None}, self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, attrs or {}, self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, once: bool = True) -> None:
        key = (repr(subject), str(predicate), repr(obj))
        if once and key in self.seen_rel:
            return
        self.seen_rel.add(key)
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, self.EC.E2))

    def construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), cid, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def line(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1

    def program_ref(self, name: str, declare: bool = True) -> dict[str, Any]:
        return self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": name}, name, declare=declare)

    # -------------------------------------------------------------- copybooks

    def copybook(self, f: File) -> None:
        book = posixpath.splitext(posixpath.basename(self.path))[0].upper()
        schema = self.term(0, len(self.text), "cbl:Copybook", {"kind": "copybook", "name": book}, book, {"language": LANGUAGE})
        self.fields(schema, f"cobol:{book}", book, f.items)
        for c in f.copies:
            self.construct("cbl:copy", c.start, c.end, False)  # a copybook copying another: no relation from a DataSchema
        if f.has_procedure_text:
            self.note(f"copybook {book} holds procedure statements: they are read where a program copies them, which is another file")

    def fields(self, schema: dict[str, Any], table: str, layout_name: str, roots: list[DataItem]) -> None:
        layout(roots)
        named = [i for r in roots for i in r.walk() if i.name]
        counts: dict[str, int] = {}
        for i in named:
            counts[i.name] = counts.get(i.name, 0) + 1
        for item in named:
            name = item.name
            if counts[name] > 1 and item.parent is not None and item.parent.name:
                name = f"{item.parent.name}.{item.name}"
            if ":" in name:
                self.construct("cbl:replacing_tag", item.name_at[0], item.name_at[1], True)
            if item.level == 66:
                self.construct("cbl:renames", item.start, item.end, False)
                continue
            attrs: dict[str, Any] = {"level": item.level, "pic": item.pic, "usage": item.usage, "occurs": item.occurs, "redefines": item.redefines,
                                     "offset": item.offset, "length": item.length}
            if item.occurs_min is not None:
                attrs["occurs_min"] = item.occurs_min
            if item.depending:
                attrs["occurs_depending_on"] = item.depending
                self.construct("cbl:occurs_depending", item.start, item.end, True)
            if item.sync:
                self.construct("cbl:sync", item.start, item.end, True)
            if item.children:
                attrs["group"] = True
            if item.parent is not None and item.parent.name:
                attrs["parent"] = item.parent.name
            field = self.term(item.name_at[0], item.name_at[1], "cbl:Field", {"table": table, "name": name}, name, attrs)
            self.rel(item.name_at[0], item.name_at[1], schema, self.P.CONTAINS, field)
            self.construct("cbl:copybook_field", item.name_at[0], item.name_at[1], True)
            if item.conditions:
                values: list[dict[str, Any]] = []
                for c in item.conditions:
                    for v in c.values:
                        entry: dict[str, Any] = {"code": v.text, "label": c.name or ""}
                        if v.through is not None:
                            entry["through"] = v.through.text
                        values.append(entry)
                    self.construct("cbl:condition_name", c.name_at[0], c.name_at[1], True)
                list_name = f"{layout_name}.{name}"
                attrs = {"values": values[:_MAX_VALUES], "field": name}
                if len(values) > _MAX_VALUES:
                    attrs["truncated"] = True
                first = item.conditions[0]
                code_list = self.term(first.start, item.conditions[-1].end, "cbl:ConditionNames", {"platform": "cobol", "name": list_name}, list_name, attrs)
                self.rel(first.start, item.conditions[-1].end, field, self.P.BINDS_TO, code_list)

    # -------------------------------------------------------------- programs

    def program(self, f: File, p: Program) -> None:
        toks = p.toks
        execs = _execs(toks)
        me_attrs: dict[str, Any] = {"language": LANGUAGE, "cics": any(e.kind == "CICS" for e in execs), "db2": any(e.kind == "SQL" for e in execs),
                                    "format": f.source.format}
        if p.nested_in:
            me_attrs["nested_in"] = p.nested_in
        if p.attrs.get("options"):
            me_attrs["options"] = p.attrs["options"]
        if p.using:
            me_attrs["using"] = p.using
        me = self.program_ref(p.name)
        ctx = Ctx(p, toks, f.source.code, self.at, [pr.name for pr in p.procedures], {})
        for pr in p.procedures:
            ctx.procedure_kinds.setdefault(pr.name, pr.kind)
        for root in p.items:
            for item in root.walk():
                if item.name:
                    ctx.items.setdefault(item.name, []).append(item)
                for c in item.conditions:
                    if c.name:
                        ctx.conditions.setdefault(c.name, []).append(c)
        for fd in p.files:
            ctx.selects.add(fd.select)
            for r in fd.records:
                ctx.records[r] = fd.select
        for ex in execs:
            if ex.kind == "SQL":
                info = sql_statement(toks, ex, f.source.code)
                if info["kind"] == "DECLARE" and info.get("cursor") and "sql" in info:
                    ctx.cursors[info["cursor"]] = (info["sql"], info["params"], info["read"], info["set"])

        # Symbols for sections and paragraphs; repeated names are qualified by their section.
        counts: dict[str, int] = {}
        for pr in p.procedures:
            counts[pr.name] = counts.get(pr.name, 0) + 1
        symbol_name = {id(pr): (f"{pr.section}.{pr.name}" if counts[pr.name] > 1 and pr.section and pr.kind == "paragraph" else pr.name) for pr in p.procedures}
        by_name: dict[str, list] = {}
        for pr in p.procedures:
            by_name.setdefault(pr.name, []).append(pr)
        symbols: dict[int, dict[str, Any]] = {}
        prog_entity_index = len(self.items)
        for pr in p.procedures:
            sname = symbol_name[id(pr)]
            term = "cbl:Section" if pr.kind == "section" else "cbl:Paragraph"
            attrs = {"kind": pr.kind, "section": pr.section if pr.kind == "paragraph" else None, "declarative": True if pr.declarative else None}
            sym = self.term(pr.start, pr.end, term, {"platform": PLATFORM, "program": p.name, "name": sname}, sname, attrs)
            symbols[id(pr)] = sym
            self.rel(pr.start, pr.end, me, self.P.CONTAINS, sym)
            self.construct("cbl:" + pr.kind, pr.start, pr.end, True)

        # Behaviour: the leading statements, then each procedure's. A unit is named by its
        # Symbol's name, so a paragraph repeated in two sections is two units.
        unit_proc = {symbol_name[id(pr)]: pr for pr in p.procedures}

        def owner_of(unit: str | None) -> dict[str, Any]:
            pr = unit_proc.get(unit) if unit is not None else None
            return symbols[id(pr)] if pr is not None else me

        def section_of(unit: str | None) -> str | None:
            pr = unit_proc.get(unit) if unit is not None else None
            if pr is None:
                return None
            return pr.section if pr.kind == "paragraph" else pr.name

        bodies: list[tuple[dict[str, Any], list, int, int]] = []
        if p.leading is not None:
            a, b = p.leading
            body = Lowerer(ctx, None).body(a, b)
            if a < b:
                bodies.append((me, body, toks[a].start, toks[b - 1].end))
        for pr in p.procedures:
            body = Lowerer(ctx, symbol_name[id(pr)]).body(pr.first, pr.stop)
            bodies.append((symbols[id(pr)], body, pr.start, max(pr.body_end, pr.end)))

        # Jumps between procedures: an unqualified name is the one in the jumping
        # procedure's own section when there is one, else the first.
        for j in ctx.jumps:
            subject = owner_of(j.unit)
            here = section_of(j.unit)
            for target in j.targets:
                cands = by_name.get(target) or []
                if cands:
                    pick = next((c for c in cands if c.kind == "paragraph" and c.section == here), cands[0])
                    obj = symbols[id(pick)]
                else:
                    obj = self.base.ref(self.K.SYMBOL, {"platform": PLATFORM, "program": p.name, "name": target}, target, declare=False)
                if obj == subject:
                    continue
                self.rel(j.start, j.end, subject, self.P.CALLS, obj, {"via": j.via})

        # Calls of programs: literal, or through a data item with one literal value.
        dynamic = 0
        for c in ctx.calls:
            target, resolved_from = c.target, None
            if target is None and c.var:
                target = self.one_literal(ctx, c.var)
                resolved_from = c.var if target else None
                cid = "cbl:call_dynamic" if c.via == "call" else f"cbl:cics_{c.via}"
                self.construct(cid, c.start, c.end, target is not None)
                if target is None:
                    dynamic += 1
                    self.unresolved += 1  # as legacy.cobol counted it: a reference whose target is decided at run time
                    self.note(f"{p.name}: {'CALL' if c.via == 'call' else c.via.upper()} through {c.var} at line {self.line(c.start)}: the program is not one literal in this file, so it stays dynamic")
                    continue
                if c.node is not None:
                    c.node["callee"] = {"e": "name", "name": target, "kind": "program", "resolved_from": c.var}
            if not target or not _PROGRAM_NAME.match(target):
                continue
            q: dict[str, Any] = {"via": c.via} if c.via != "call" else {}
            if target in DLI_CALLS or target in MQ_CALLS:
                q["interface"] = "dli" if target in DLI_CALLS else "mq"  # the IMS or MQ stub, kept as legacy.cobol linked it
            if resolved_from:
                q["resolved_from"] = resolved_from
            self.rel(c.start, c.end, me, self.P.CALLS, self.program_ref(target, declare=False), q)
        if dynamic:
            me_attrs["dynamic_calls"] = dynamic

        # Copybooks.
        seen_books: set[str] = set()
        replacing = []
        for c in p.copies:
            where = p.copy_positions.get(c.index, "other")
            if where == "procedure":
                self.construct("cbl:procedure_copy", c.start, c.end, False)
                self.note(f"{p.name}: COPY {c.book} in the procedure division at line {self.line(c.start)}: the paragraphs it brings are not read here")
            if c.replacing:
                replacing.append({"book": c.book, "replacing": [list(r) for r in c.replacing]})
            if c.book in seen_books:
                continue
            seen_books.add(c.book)
            self.rel(c.name_at[0], c.name_at[1], me, self.P.DEPENDS_ON, self.base.ref(self.K.DATA_SCHEMA, {"kind": "copybook", "name": c.book}, c.book, declare=False))
        if replacing:
            me_attrs["copy_replacing"] = replacing

        # Embedded SQL: lineage as legacy.cobol read it, and the SQL INCLUDE members.
        includes: list[str] = []
        data_starts = {e.start for e in p.data_execs}
        for ex in execs:
            if ex.kind != "SQL":
                if ex.start in data_starts:
                    self.construct("cbl:exec_other" if ex.kind not in ("CICS", "DLI") else f"cbl:{ex.kind.lower()}_other" if ex.kind == "CICS" else "cbl:exec_dli", ex.start, ex.end, False)
                continue
            body = f.source.code[ex.body_start : ex.body_end]
            head = body.lstrip()[:40].upper()
            if ex.start in data_starts:  # the procedure division's statements are counted where they are lowered
                word = head.split()[0] if head.split() else ""
                if word == "INCLUDE":
                    self.construct("cbl:sql_include", ex.start, ex.end, True)
                elif word == "DECLARE":
                    cursor = bool(re.match(r"DECLARE\s+[\w-]+\s+(?:\w+\s+){0,3}CURSOR\b", head))
                    self.construct("cbl:sql_declare_cursor" if cursor else "cbl:sql_declare_table", ex.start, ex.end, cursor)
                elif word not in ("BEGIN", "END"):
                    self.construct("cbl:sql_other", ex.start, ex.end, False)
            if re.match(r"(INCLUDE|WHENEVER|BEGIN|END)\b", head):
                if head.startswith("INCLUDE"):
                    member = head.split()[1].rstrip(".") if len(head.split()) > 1 else ""
                    if member:
                        includes.append(member)
                        if member not in ("SQLCA", "SQLDA"):
                            self.rel(ex.start, ex.end, me, self.P.DEPENDS_ON, self.base.ref(self.K.DATA_SCHEMA, {"kind": "copybook", "name": member}, member, declare=False), {"via": "sql_include"})
                continue
            if re.match(r"DECLARE\s+[\w-]+\s+TABLE\b", head) or re.match(r"(PREPARE|EXECUTE|DESCRIBE)\b", head):
                if head.startswith(("PREPARE", "EXECUTE", "DESCRIBE")):
                    self.unresolved += 1
                continue
            sql = _HOST.sub(lambda h: "0".ljust(len(h.group())), body)
            sql = _DECLARE_CURSOR.sub(lambda h: " " * len(h.group()), sql)
            sql = _blank_into(sql)
            try:
                found = self.sql.lineage(sql, "db2")
            except Exception as exc:  # a scanner fault on hostile input: the statement is counted, not read
                self.note(f"SQL at line {self.line(ex.start)} not read: {type(exc).__name__}", "warning")
                self.unresolved += 1
                continue
            self.resolved += len(found.writes) + len(found.reads)
            self.unresolved += found.unresolved
            self.items += self.sql.lineage_relations(self.blob, self.data, self.text, found, me, kind=self.K.TABLE, offset=ex.body_start, to_byte=self.to_byte)
        if includes:
            me_attrs["sql_includes"] = sorted(set(includes))

        # CICS: files, maps, transactions.
        maps: list[dict[str, Any]] = []
        for c in ctx.cics:
            verb = c.command.split(" ")[0] if c.command else ""
            if verb in ("READ", "READNEXT", "READPREV", "STARTBR", "WRITE", "REWRITE", "DELETE") and ("FILE" in c.options or "DATASET" in c.options):
                opt = c.options.get("FILE") or c.options.get("DATASET")
                name = opt.value if opt.kind == "lit" else self.one_literal(ctx, opt.value) if opt.kind == "var" else None
                if opt.kind == "var" and name is None:
                    self.unresolved += 1
                    self.note(f"{p.name}: CICS {verb} of the file named by {opt.value} at line {self.line(c.start)}: not one literal in this file, so not read")
                    continue
                if name:
                    predicate = self.P.READS_FROM if verb.startswith(("READ", "STARTBR")) else self.P.WRITES_TO
                    ds = self.dataset_name(name)
                    if ds:
                        self.resolved += 1
                        target = self.entity(opt.start, opt.end, self.K.DATASET, {"name": ds}, ds, {"access": "cics_file"})
                        self.rel(opt.start, opt.end, me, predicate, target, {"via": "cics"} if opt.kind == "var" else {}, once=False)
            if verb in ("SEND", "RECEIVE") and "MAP" in c.options:
                m = c.options["MAP"]
                ms = c.options.get("MAPSET")
                map_name = m.value if m.kind == "lit" else self.one_literal(ctx, m.value) if m.kind == "var" else None
                set_name = (ms.value if ms.kind == "lit" else self.one_literal(ctx, ms.value) if ms.kind == "var" else None) if ms is not None else map_name
                entry = {"map": map_name or (m.value if m.kind == "var" else None), "mapset": set_name or (ms.value if ms is not None and ms.kind == "var" else None), "op": verb.lower()}
                if not map_name or not set_name:
                    entry["dynamic"] = True
                    self.note(f"{p.name}: {verb} MAP at line {self.line(c.start)}: the map or mapset is not one literal in this file")
                if entry not in maps:
                    maps.append(entry)
                if verb == "SEND" and map_name and set_name:
                    screen = f"{set_name}.{map_name}"
                    self.rel(c.start, c.end, me, self.P.RENDERS, self.base.ref(self.K.SCREEN, {"platform": "bms", "name": screen}, screen, declare=False))
            if verb in ("START", "RETURN") and "TRANSID" in c.options:
                t = c.options["TRANSID"]
                tran = t.value if t.kind == "lit" else self.one_literal(ctx, t.value) if t.kind == "var" else None
                if t.kind == "var":
                    self.construct("cbl:cics_start" if verb == "START" else "cbl:cics_return_transid", c.start, c.end, tran is not None)
                if not tran:
                    self.note(f"{p.name}: {verb} TRANSID at line {self.line(c.start)}: the transaction is not one literal in this file")
                    continue
                tran = tran.upper()
                key = {"component_root": self.base.UNIT, "protocol": "cics", "method": "TRANSACTION", "route": tran}
                self.rel(t.start, t.end, me, self.P.CONSUMES, self.base.ref(self.K.ENDPOINT, key, f"TRANSACTION {tran}", declare=False), {"via": verb.lower()})
        if maps:
            me_attrs["maps"] = sorted(maps, key=lambda e: (str(e["mapset"]), str(e["map"]), e["op"]))

        # DL/I and MQ.
        segments: set[str] = set()
        functions: set[str] = set()
        for d in ctx.dli:
            if d.get("exec"):
                segments.update(d["segments"])
                continue
            args = d["args"]
            if args and args[0]:
                fn = self.literal_value(ctx, args[0])
                if fn:
                    functions.add(fn.strip()[:4])
            for ssa in args[3:]:
                v = self.literal_value(ctx, ssa) if ssa else None
                m = _SEGMENT.match(v.upper()) if v else None
                if m:
                    segments.add(m.group(1))
        if segments:
            me_attrs["dli_segments"] = sorted(segments)
        if functions:
            me_attrs["dli_functions"] = sorted(functions)
        if ctx.mq:
            me_attrs["mq_calls"] = sorted({m for m, _, _ in ctx.mq})
            queues = set()
            for name, vals in ctx.writes.items():
                if name.endswith("OBJECTNAME"):
                    queues.update(v.strip() for v in vals if v and v.strip())
            for name, items in ctx.items.items():
                if name.endswith("OBJECTNAME"):
                    for it in items:
                        queues.update(v.text.strip() for v in it.values if v.kind == "string" and v.text.strip())
            if queues:
                me_attrs["mq_queues"] = sorted(queues)

        # Files: dd_file observations and attributes.files.
        files: list[dict[str, Any]] = []
        for fd in sorted(p.files, key=lambda x: x.select):
            modes = ctx.opens.get(fd.select, set())
            mode = _mode(modes)
            entry = {"select": fd.select, "dd": fd.dd, "assign": fd.assign, "organization": fd.organization, "access": fd.access, "record_key": fd.record_key,
                     "file_status": fd.file_status, "kind": fd.kind, "mode": mode, "records": fd.records or None, "copybooks": fd.copybooks or None,
                     "recording_mode": fd.recording_mode, "record_length": fd.record_length}
            files.append({k: v for k, v in entry.items() if v is not None})
            if fd.dd is None:
                self.construct("cbl:assign_dynamic", fd.start, fd.end, False)
                self.note(f"{p.name}: file {fd.select} is not assigned to a DD name ({fd.assign or 'no ASSIGN'}): it is not joined to a job's dataset")
                continue
            if fd.kind == "sd":
                continue  # sort work files are the sort's own
            payload = {"program": me, "dd": fd.dd, "mode": mode, "select": fd.select, "record": fd.records[0] if fd.records else None}
            self.items.append(self.base.obs("dd_file", self.blob, self.span(fd.start, fd.end), payload, self.EC.E2))
        if files:
            me_attrs["files"] = files

        # The program itself, declared before its parts.
        program_entity = self.platforms.platform_entity(self.blob, self.span(*p.name_at), "cbl:Program", me["natural_key"], p.name, {k: v for k, v in me_attrs.items() if v is not None}, self.EC.E2)
        self.items.insert(prog_entity_index, program_entity)

        # Behaviour units and SORT shapes.
        from repo_intel.platforms.cfml.urls import scrub_unit

        for owner, body, a, b in bodies:
            scrub_unit(body, [])
            scrub_credentials(body)
            params = [{"name": u, "type": "any", "required": True} for u in p.using] if owner is me else []
            try:
                self.items.append(self.behavior_observation(self.blob, self.span(a, b), owner, LANGUAGE, body, params, self.EC.E2, kind="paragraph"))
            except ValueError as exc:  # a lowering fault: the unit keeps its relations, its behaviour is left out
                self.note(f"behaviour of {owner['name']} not emitted: {str(exc)[:160]}", "warning")
        for s in ctx.sorts:
            shapes = _sort_shapes(s, {fd.select: fd for fd in p.files}, self.at)
            if not shapes:
                continue
            owner = owner_of(s["unit"])
            try:
                self.items.append(self.transform_observation(self.blob, self.span(s["start"], s["end"]), owner, LANGUAGE, shapes))
            except ValueError as exc:
                self.note(f"sort shapes at line {self.line(s['start'])} not emitted: {str(exc)[:120]}", "warning")

        for cid, a, b, modelled in p.constructs + ctx.constructs:
            self.construct(cid, a, b, modelled)
        for severity, message in p.diagnostics + ctx.diagnostics:
            self.note(f"{p.name}: {message}", severity)

    # -------------------------------------------------------------- the one-literal rule

    def one_literal(self, ctx: Ctx, name: str) -> str | None:
        """The one literal value `name` can hold in this file, else None (contract: never guessed)."""
        value = self.literal_value(ctx, name, program_name=True)
        return value

    def literal_value(self, ctx: Ctx, name: str, program_name: bool = False) -> str | None:
        items = ctx.items.get(name) or []
        if len(items) > 1:
            return None
        values: set[str] = set()
        if items:
            item = items[0]
            if item.section == "linkage" or item.redefines or item.occurs or any(other.redefines == item.name for other in ctx.items.get(item.redefines or "", [])):
                return None
            if any(i.redefines == name for lst in ctx.items.values() for i in lst):
                return None
            anc = item.parent
            while anc is not None:
                if anc.name and ctx.writes.get(anc.name):
                    return None
                if anc.redefines or any(i.redefines == anc.name for lst in ctx.items.values() for i in lst):
                    return None
                anc = anc.parent
            for v in item.values:
                if v.kind != "string" or v.through is not None:
                    return None
                values.add(v.text)
            if not item.values and item.children:
                first = item.children[0]
                values.update(v.text for v in first.values if v.kind == "string")
        for w in ctx.writes.get(name, []):
            if w is None:
                return None
            values.add(w)
        cleaned = {v.strip().upper() for v in values}
        if len(cleaned) != 1:
            return None
        value = next(iter(cleaned))
        if program_name and not _PROGRAM_NAME.match(value):
            return None
        return value


_CREDENTIAL = re.compile(r"PASSW|PASWD|PSWD|PWD|SECRET|TOKEN|CREDENT|API-?KEY|PASSPHRASE", re.I)
_NESTED = ("body", "then", "else", "cases", "default", "catches", "finally")


def _names(node: Any, out: set[str]) -> None:
    if isinstance(node, list):
        for x in node:
            _names(x, out)
    elif isinstance(node, dict):
        if node.get("e") == "var" and isinstance(node.get("name"), str):
            out.add(node["name"])
        for k, v in node.items():
            if k not in _NESTED:
                _names(v, out)


def _blank(node: Any) -> None:
    if isinstance(node, list):
        for x in node:
            _blank(x)
    elif isinstance(node, dict):
        if node.get("e") == "lit" and isinstance(node.get("value"), str):
            node["value"] = ""
        for k, v in node.items():
            if k not in _NESTED:
                _blank(v)


def scrub_credentials(body: list[Any]) -> None:
    """Blank the string literals of a statement that touches a data item named like a
    credential (`MOVE 'x' TO WS-PASSWORD`): names are kept, values never (contract rule 7)."""
    for stmt in body:
        if not isinstance(stmt, dict):
            continue
        names: set[str] = set()
        _names(stmt, names)
        if any(_CREDENTIAL.search(n) for n in names):
            _blank(stmt)
            stmt["redacted"] = "credential"
        for k in _NESTED:
            v = stmt.get(k)
            if isinstance(v, list):
                if k in ("cases", "catches"):
                    for case in v:
                        if isinstance(case, dict) and isinstance(case.get("body"), list):
                            scrub_credentials(case["body"])
                else:
                    scrub_credentials(v)


def _mode(modes: set[str]) -> str | None:
    if not modes:
        return None
    if len(modes) == 1:
        return next(iter(modes))
    if "i-o" in modes or "input" in modes:
        return "i-o"  # read and written
    return "output" if "output" in modes else "extend"


def _execs(toks) -> list[Exec]:
    out: list[Exec] = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i].is_("EXEC") and i + 1 < n and toks[i + 1].kind == lexer.WORD:
            ex = exec_block(toks, i, n)
            out.append(ex)
            i = ex.stop
            continue
        i += 1
    return out


_INTO = re.compile(r"\bINTO\b[\s0,]*?(?=\bFROM\b)", re.I)


def _blank_into(sql: str) -> str:
    """Blank a SELECT's INTO list of (already blanked) host variables, which is not a table."""
    if not re.match(r"\s*(SELECT|WITH|VALUES)\b", sql, re.I):
        return sql
    return _INTO.sub(lambda m: " " * len(m.group()), sql, count=1)


def _sort_shapes(s: dict[str, Any], files: dict[str, Any], at) -> list[dict[str, Any]]:
    """SORT or MERGE of named files: union (a MERGE of several) and sort, by file name as written."""
    using = [u.lower() for u in s.get("using", [])]
    giving = [g.lower() for g in s.get("giving", [])]
    if not using or not giving or not s.get("keys"):
        return []
    span = at(s["start"], s["end"])
    order = [{"column": k["column"].lower(), "desc": bool(k["desc"])} for k in s["keys"]]
    out: list[dict[str, Any]] = []
    inputs: list[str] = using
    if len(using) > 1:
        out.append({"id": "t1", "op": "union", "at": span, "inputs": using})
        inputs = ["t1"]
    out.append({"id": f"t{len(out) + 1}", "op": "sort", "at": span, "inputs": inputs, "order": order, **({"output": giving[0]} if len(giving) == 1 else {})})
    return out


class CobolSource:
    id = "cobol.source"
    version = "1.0.0"
    SUFFIXES = (".cbl", ".cob", ".cobol", ".cpy")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        text = decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        try:
            src = lexer.read(text)
            f = parse(src)
        except RecursionError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "nesting too deep to read")], parsed=False)
        o = _Emit(self.id, path, data, blob, text)
        for a, b in src.directives:
            o.construct("cbl:compiler_directive", a, b, False)
        if src.debug_lines:
            o.construct("cbl:debug_line", 0, 0, False)
        if path.lower().endswith(".cpy"):
            o.copybook(f)
            for cid, a, b, modelled in f.constructs:
                if cid != "cbl:copy":
                    o.construct(cid, a, b, modelled)
            return Extraction(o.items, diagnostics=o.diagnostics)
        if not f.programs:
            return Extraction(o.items, diagnostics=[diagnostic(self.id, "info", "no PROGRAM-ID")], parsed=True)
        for p in f.programs:
            try:
                o.program(f, p)
            except RecursionError:
                o.note(f"{p.name}: nesting too deep to read", "warning")
        for cid, a, b, modelled in f.constructs:
            o.construct(cid, a, b, modelled)
        o.items += o.sql.lineage_summary(blob, data, o.resolved, o.unresolved)
        return Extraction(o.items, diagnostics=o.diagnostics)


__all__ = ["CobolSource", "decode"]
