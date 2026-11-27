"""`natural.source`: Natural source exports, one object per file, by suffix.

- Programs (`.nsp`), subprograms (`.nsn`), external subroutines (`.nss`), helproutines
  (`.nsh`) and copycode (`.nsc`) are `Program {platform: "natural", name}` with
  `attributes.object_type` and `mode`. The name is the object's (the file's stem, upper
  case), except that an external subroutine is named by its DEFINE SUBROUTINE name, which
  is what PERFORM uses; its object name is `attributes.object_name`. Inline subroutines are
  `Symbol {platform: "natural", program, name}` the Program CONTAINS. Each has a behaviour
  unit: kind `function` for a subroutine, `open_code` for the object's main code.
- What the code does: `DEFINE DATA ... USING` DEPENDS_ON the data area
  (`DataSchema {kind: "natural-lda" | "natural-pda" | "natural-gda", name}`); READ, FIND,
  HISTOGRAM and GET READ_FROM, and STORE, UPDATE and DELETE WRITE_TO, the DDM's
  `Table {datastore: "adabas", name}` through the view the object defines; CALLNAT, FETCH,
  FETCH RETURN, RUN and PERFORM of an external subroutine CALL `Program {platform:
  "natural"}`; CALL of another program CALLS `Program {platform: "mainframe"}`; INPUT USING
  MAP and WRITE USING MAP or FORM RENDER `Screen {platform: "natural"}`; INCLUDE DEPENDS_ON
  the copycode's Program; Natural SQL READS_FROM and WRITES_TO `Table {datastore:
  "default"}` through the shared SQL lineage scanner. Targets are referred to without being
  declared, so they link only when the repository holds them. A target named by a variable
  stays dynamic, with a diagnostic. A view defined in a data area in another file cannot be
  resolved here: the program's access through it is recorded as a `natural_view_use`
  observation (program, view, data areas it uses) and counted, not guessed.
- Data areas (`.nsl`, `.nsa`, `.nsg`) are `DataSchema`s that CONTAIN their fields as
  `Field {table: "natural:<data area>", name}` (contract 11.1 item 3): a field is named by
  its data name, and a name used twice in one data area by `<parent>.<name>`.
- Maps (`.nsm`) are `Screen {platform: "natural", name}` that CONTAIN a `UIControl
  {container: <map>, name: <field>}` per field.
- DDMs (`.nsd`) are `Table {datastore: "adabas", name}` that CONTAIN their fields as
  `Field {table: "adabas:<DDM>", name: <long name>}` (contract 11.1 item 8).

Where a database loop updates the record it reads, or stores what it read into another
file, the unit's transform shapes (language `natural`) say so. Initial values, parameter
values and the database and file numbers of a DDM are never copied, nor is a literal
assigned to, compared with or passed beside a field named for a password, key, user or
host: it becomes a `nat:sensitive_value` node (`scrub_sensitive`).
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.natural import data as natdata
from repo_intel.platforms.natural import ddm as natddm
from repo_intel.platforms.natural import maps as natmaps
from repo_intel.platforms.natural.lexer import Lines, prepare, tokens
from repo_intel.platforms.natural.parse import Module, Parser, Unit, detect_mode
from repo_intel.platforms.natural.vocabulary import ADABAS, DATA_AREA_KINDS, MAINFRAME, OBJECT_TYPES, PLATFORM, USING_KINDS

LANGUAGE = "natural"
MAX_BYTES = 4 * 1024 * 1024
_CODE_TERMS = {".nsp": "nat:Program", ".nsn": "nat:Subprogram", ".nss": "nat:Subroutine", ".nsh": "nat:Helproutine", ".nsc": "nat:Copycode"}
_AREA_TERMS = {".nsl": "nat:LocalDataArea", ".nsa": "nat:ParameterDataArea", ".nsg": "nat:GlobalDataArea"}
_AREA_SECTIONS = {".nsl": "LOCAL", ".nsa": "PARAMETER", ".nsg": "GLOBAL"}
_READS = ("read", "find", "histogram", "get")
_PARAM_TYPES = natdata.TYPES
# Field names that say the value is a credential or names a system: their literal values are not copied.
_SENSITIVE = re.compile(r"(?i)(pass|pwd|secret|token|cred|logon|login|signon|user|host|server|dsn|conn|apikey|api-key)")


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


class _Emit:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        (self.platforms, self.behavior_observation, self.K, self.EC, self.P, self.base, self.sql) = _lib()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self._lines: Lines | None = None

    def line(self, offset: int) -> int:
        """The 1-based line of a character offset, for diagnostics."""
        if self._lines is None:
            self._lines = Lines(self.text)
        return self._lines.index(offset) + 1

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.to_byte(a), self.to_byte(max(a, b)))

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ec=None) -> dict[str, Any]:
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, {k: v for k, v in (attrs or {}).items() if v is not None}, ec or self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, attrs or {}, self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, ec=None) -> None:
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, ec or self.EC.E2))

    def construct(self, cid: str, a: int, b: int, modelled: bool, ec=None) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), cid, modelled, ec or self.EC.E2))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def done(self):
        self.items += self.sql.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return self.base.Extraction(self.items, diagnostics=self.diagnostics)

    # -------------------------------------------------------------- code objects

    def code(self, ext: str, stem: str, text: str, mode_header: str | None, numbers: dict[int, int]):
        toks = tokens(text)
        mode = detect_mode(toks, mode_header)
        parser = Parser(text, toks, mode, self.to_byte, numbers)
        object_type = OBJECT_TYPES[ext]
        try:
            module = parser.module_(object_type, stem)
        except RecursionError:
            # The object is still declared, so what calls it links; what it does is not read.
            self.note("nesting too deep to read: the object is declared, its statements are not read", "warning")
            self.term(0, len(text), _CODE_TERMS[ext], {"platform": PLATFORM, "name": stem}, stem, {"object_type": object_type, "mode": mode, "language": LANGUAGE})
            return None
        name = stem
        main_sub: Unit | None = None
        if ext == ".nss" and module.subroutines:
            main_sub = module.subroutines[0]
            name = main_sub.name
        attrs: dict[str, Any] = {"object_type": object_type, "mode": mode, "language": LANGUAGE}
        if name != stem:
            attrs["object_name"] = stem
        views = module.data.views() if module.data else {}
        if views:
            attrs["views"] = dict(sorted(views.items()))
        whole = (0, len(text))
        me = self.term(whole[0], whole[1], _CODE_TERMS[ext], {"platform": PLATFORM, "name": name}, name, attrs)
        self.construct(f"nat:{object_type}", 0, min(len(text), 1), True)
        usings = module.data.usings if module.data else []
        for u in usings:
            kind = USING_KINDS[u.section]
            self.rel(u.start, u.end, me, self.P.DEPENDS_ON, self.base.ref(self.K.DATA_SCHEMA, {"kind": kind, "name": u.name}, u.name, declare=False), {"via": f"{u.section.lower()} using"})
        inline = {s.name for s in module.subroutines if s is not main_sub}
        owners: list[tuple[Unit, dict[str, Any], str]] = []
        if main_sub is not None:
            owners.append((main_sub, me, "function"))
        if module.main is not None and (module.main.body or main_sub is None):
            owners.append((module.main, me, "open_code"))
        for sub in module.subroutines:
            if sub is main_sub:
                continue
            sym = self.term(sub.start, sub.end, "nat:InlineSubroutine", {"platform": PLATFORM, "program": name, "name": sub.name}, sub.name, {"symbol_kind": "subroutine"})
            self.rel(sub.start, sub.end, me, self.P.CONTAINS, sym)
            owners.append((sub, sym, "function"))
        params = _params(module.data) if object_type in ("subprogram", "subroutine", "helproutine") else []
        for unit, owner, kind in owners:
            self.unit_facts(unit, owner, me, name, views, usings, inline, mode)
            body = unit.body
            if kind == "open_code" and module.data is not None:
                body = _declarations(module.data, self.to_byte) + body
            if body or kind == "function":
                self.behavior(owner, body, params if (unit is module.main or unit is main_sub) else [], unit.start, unit.end, kind)
            self.shapes(owner, unit, views, mode, usings)
        for cid, a, b, modelled in module.constructs:
            self.construct(cid, a, b, modelled)
        for severity, message in module.diagnostics:
            self.note(message, severity)
        return module

    def ddm_of(self, view: str | None, views: dict[str, str], mode: str, usings) -> str | None:
        """The DDM a view names: a view this object defines, or in reporting mode with no data
        area in another file, the DDM named directly."""
        if view is None:
            return None
        if view in views:
            return views[view]
        if mode == "reporting" and not usings:
            return view
        return None

    def unit_facts(self, unit: Unit, owner: dict[str, Any], program: dict[str, Any], program_name: str, views: dict[str, str], usings, inline: set[str], mode: str) -> None:
        seen: set[tuple] = set()

        def once(*key) -> bool:
            if key in seen:
                return False
            seen.add(key)
            return True

        for c in unit.calls:
            if c.name is None:
                continue
            if c.kind == "call":
                target = self.base.ref(self.K.PROGRAM, {"platform": MAINFRAME, "name": c.name}, c.name, declare=False)
            else:
                target = self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": c.name}, c.name, declare=False)
            if target["natural_key"] == program["natural_key"] and target["natural_key"]["platform"] == PLATFORM:
                continue
            if once("call", c.kind, c.name):
                self.rel(c.start, c.end, owner, self.P.CALLS, target, {"via": c.kind})
        for name, a, b in unit.performs:
            if name in inline:
                target = self.base.ref(self.K.SYMBOL, {"platform": PLATFORM, "program": program_name, "name": name}, name)
                if target["natural_key"] == owner.get("natural_key"):
                    continue
            else:
                if name == program_name:
                    continue
                target = self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": name}, name, declare=False)
            if once("perform", name):
                self.rel(a, b, owner, self.P.CALLS, target, {"via": "perform"})
        for d in unit.db:
            ddm = self.ddm_of(d.view, views, mode, usings)
            predicate = self.P.READS_FROM if d.op in _READS else self.P.WRITES_TO
            if ddm is None:
                self.unresolved += 1
                if d.view is not None:
                    self.items.append(self.base.obs("natural_view_use", self.blob, self.span(d.start, d.end), {
                        "program": owner, "view": d.view, "statement": d.op, "data_areas": sorted({u.name for u in usings})}, self.EC.E2))
                    if once("unresolved", d.view):
                        where = "a data area in another file" if usings else "no view this object defines"
                        self.note(f"view {d.view} at line {self.line(d.start)} is defined in {where}: its DDM is not read here")
                continue
            self.resolved += 1
            if once("db", predicate, ddm):
                table = self.entity(d.start, d.end, self.K.TABLE, {"datastore": ADABAS, "name": ddm}, ddm)
                self.rel(d.start, d.end, owner, predicate, table, {"view": d.view} if d.view and d.view != ddm else {})
        for s in unit.sql:
            try:
                found = self.sql.lineage(s.text, None, views=False)  # DB2: read as generic SQL (contract 11.1 item 12)
            except Exception as exc:  # a scanner fault on hostile input: counted, not read
                self.note(f"SQL at line {self.line(s.start)} not read: {type(exc).__name__}", "warning")
                self.unresolved += 1
                continue
            self.unresolved += found.unresolved
            for predicate, rows in ((self.P.WRITES_TO, found.writes), (self.P.READS_FROM, found.reads)):
                for table_name, a, b in rows:
                    self.resolved += 1
                    if once("sql", predicate, table_name):
                        table = self.entity(s.start + a, s.start + b, self.K.TABLE, {"datastore": "default", "name": table_name}, table_name)
                        self.rel(s.start + a, s.start + b, owner, predicate, table)
        for map_name, a, b, how in unit.maps:
            if map_name and once("map", map_name):
                self.rel(a, b, owner, self.P.RENDERS, self.base.ref(self.K.SCREEN, {"platform": PLATFORM, "name": map_name}, map_name, declare=False), {"via": how})
        for inc, a, b in unit.includes:
            if once("include", inc):
                self.rel(a, b, owner, self.P.DEPENDS_ON, self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": inc}, inc, declare=False), {"via": "include"})

    def behavior(self, owner: dict[str, Any], body: list, params: list, a: int, b: int, kind: str) -> None:
        from repo_intel.platforms.cfml.urls import scrub_unit

        statements = list(body)
        params = [dict(p) for p in params]
        scrub_unit(statements, params)
        self.scrub_sensitive(statements)
        try:
            self.items.append(self.behavior_observation(self.blob, self.span(a, b), owner, LANGUAGE, statements, params, self.EC.E2, kind=kind))
        except ValueError as exc:  # a lowering fault: the unit keeps its relations, its behaviour is left out
            self.note(f"behaviour of {owner['name']} not emitted: {str(exc)[:120]}", "warning")

    def scrub_sensitive(self, statements: list) -> None:
        """Values of fields whose names say they hold a password, key, user or host are not
        copied. An assignment of a literal to such a field keeps its target and its value
        becomes a `nat:sensitive_value` platform node, and so does every other assignment
        of the same statement (`MOVE 'x' TO #A #PASSWORD`). Any other statement that names
        such a field (a comparison, a DECIDE value, a database search value, a CALLNAT
        argument, an output list, Natural SQL) has its string literals replaced the same
        way, since a literal compared to or passed beside a credential is its value."""
        from repo_intel.compile.behavior import walk

        groups: dict[tuple, list[dict[str, Any]]] = {}
        for node in walk(statements):
            if node.get("op") in ("assign", "declare") and isinstance(node.get("at"), list):
                groups.setdefault(tuple(node["at"]), []).append(node)
        for nodes in groups.values():
            if not any(_SENSITIVE.search(_target_name(n)) and _literal_value(n.get("value")) for n in nodes):
                continue
            for node in nodes:
                if _literal_value(node.get("value")):
                    a, b = node["at"]
                    node["value"] = {"e": "platform", "construct": "nat:sensitive_value"}
                    self.items.append(self.platforms.construct(self.blob, self.base.span(self.data, a, b), "nat:sensitive_value", True, self.EC.E2))
        self._scrub_block(statements)

    def _scrub_block(self, block: list) -> None:
        for stmt in block:
            if not isinstance(stmt, dict):
                continue
            own = [{k: v for k, v in stmt.items() if k not in _BLOCKS and k != "at"}]
            sql = stmt.get("sql") if stmt.get("op") == "query" and isinstance(stmt.get("sql"), str) else None
            masked = False
            if _names_sensitive(own) or (sql is not None and _SENSITIVE.search(_SQL_STRING.sub("", sql))):
                masked = _mask(own)
                if sql is not None:
                    stmt["sql"] = _SQL_STRING.sub("''", sql)
                    masked = masked or stmt["sql"] != sql
            cases = stmt.get("cases")
            if isinstance(cases, list):
                on_sensitive = _names_sensitive([stmt.get("on")])
                for case in cases:
                    if isinstance(case, dict) and (on_sensitive or _names_sensitive([case.get("match")])):
                        masked = _mask([case.get("match")]) or masked
            if masked and isinstance(stmt.get("at"), list):
                a, b = stmt["at"]
                self.items.append(self.platforms.construct(self.blob, self.base.span(self.data, a, b), "nat:sensitive_value", True, self.EC.E2))
            for k in _BLOCKS:
                v = stmt.get(k)
                if isinstance(v, list):
                    if k in ("cases", "catches"):
                        for case in v:
                            if isinstance(case, dict) and isinstance(case.get("body"), list):
                                self._scrub_block(case["body"])
                    else:
                        self._scrub_block(v)

    def shapes(self, owner: dict[str, Any], unit: Unit, views: dict[str, str], mode: str, usings) -> None:
        """Transform shapes of the database loops that write: a loop that UPDATEs the record it
        reads is a projection of the file onto itself; one that STOREs into another file
        publishes what it read there."""
        from repo_intel.compile.transforms import Builder, transform_observation

        b = Builder()
        for loop in _db_loops(unit.body):
            detail = loop["over"].get("detail", {})
            source = self.ddm_of(detail.get("view"), views, mode, usings)
            writes = list(_writes(loop["body"]))
            if source is None or not writes:
                continue
            at = loop["at"]
            last = b.add("read", at, [source.lower()])
            test = detail.get("where") or detail.get("criteria")
            if test is not None:
                last = b.add("filter", at, [last], predicate=test)
            for node, assigned in writes:
                d = node.get("detail", {})
                target = self.ddm_of(d.get("view"), views, mode, usings)
                if target is None:
                    continue
                if node["construct"] == "nat:update" and target == source:
                    fields = sorted({f.lower() for f in assigned})
                    b.add("project", node["at"], [last], output=target.lower(), columns={"replace": fields} if fields else None)
                elif node["construct"] == "nat:store" and target != source:
                    b.add("publish", node["at"], [last], output=target.lower())
        transforms = b.finish()
        if any(t["op"] in ("project", "publish") for t in transforms):
            try:
                self.items.append(transform_observation(self.blob, self.span(unit.start, unit.end), owner, LANGUAGE, transforms, self.EC.E2))
            except ValueError as exc:
                self.note(f"transform shapes of {owner['name']} not emitted: {str(exc)[:120]}", "warning")

    # -------------------------------------------------------------- data areas

    def data_area(self, ext: str, stem: str, text: str) -> None:
        toks = tokens(text)
        start = 0
        section = _AREA_SECTIONS[ext]
        for k in range(len(toks) - 1):
            if toks[k].up == "DEFINE" and toks[k + 1].up == "DATA":
                start = k + 2
                self.construct("nat:define_data", toks[k].start, toks[k + 1].end, True)
                break
        defn = natdata.read(toks, text, start, section)
        kind = DATA_AREA_KINDS[ext]
        items = list(defn.walk())
        if not items:
            if _editor_layout(text):
                self.construct("nat:data_area_editor_format", 0, len(text), False)
                self.note("data area in the data area editor's layout: export it as DEFINE DATA source to read its fields")
            else:
                self.note("no data definitions found")
        views = defn.views()
        attrs = {"object_type": OBJECT_TYPES[ext], "fields": len(items), **({"views": dict(sorted(views.items()))} if views else {})}
        schema = self.term(0, len(text), _AREA_TERMS[ext], {"kind": kind, "name": stem}, stem, attrs)
        self.construct(f"nat:{OBJECT_TYPES[ext]}", 0, min(1, len(text)), True)
        layout = f"natural:{stem}"
        for item, name in _field_names(items):
            fattrs = {"level": item.level, **item.attrs}
            if item.children:
                fattrs["group"] = True
            if item.view_of:
                fattrs["view_of"] = item.view_of
            if item.parent is not None:
                fattrs["parent"] = item.parent.name
                if item.parent.attrs.get("redefinition"):
                    fattrs.pop("parent")
                    fattrs["redefines"] = item.parent.name
            fattrs.pop("redefinition", None)
            fref = self.term(item.start, item.end, "nat:DataField", {"table": layout, "name": name}, name, fattrs)
            self.rel(item.start, item.end, schema, self.P.CONTAINS, fref)
        for view, ddm in sorted(views.items()):
            self.items.append(self.base.obs("natural_view", self.blob, self.base.whole_file(self.data), {"schema": schema, "view": view, "ddm": ddm}, self.EC.E2))
        for u in defn.usings:
            self.note(f"{u.section} USING {u.name} inside a data area is not followed")
        for cid, a, b, modelled in defn.constructs:
            if cid.endswith("_using"):
                modelled = False
            self.construct(cid, a, b, modelled)

    # -------------------------------------------------------------- maps

    def map(self, stem: str, text: str) -> None:
        toks = tokens(text)
        src = natmaps.read(text, toks)
        params = {it.name: it for it in src.params.walk()} if src.params else {}
        fields = [it for it in src.items if it.kind == "field"]
        screen = self.term(0, len(text), "nat:Map", {"platform": PLATFORM, "name": stem}, stem,
                           {"object_type": "map", "fields": len(fields), **({"parameters": len([p for p in params.values() if p.level == 1])} if params else {})})
        self.construct("nat:map", 0, min(1, len(text)), True)
        if src.params:
            for cid, a, b, modelled in src.params.constructs:
                if cid == "nat:data_field":
                    continue  # the map's parameters are its controls' variables, not entities of their own
                self.construct(cid, a, b, modelled)
        counts: dict[str, int] = {}
        labelled = set()
        for it in fields:
            counts[it.name] = counts.get(it.name, 0) + 1
            name = it.name if counts[it.name] == 1 else f"{it.name}~{counts[it.name]}"
            attrs: dict[str, Any] = {"line": it.line, "column": it.column, **it.attrs}
            base_name = it.name.split("(", 1)[0]
            p = params.get(base_name)
            if p is not None and p.attrs.get("format"):
                attrs["format"] = p.attrs["format"]
            if "label" in attrs:
                labelled.add((it.line,))
            control = self.term(it.start, it.end, "nat:MapField", {"container": stem, "name": name}, name, attrs)
            self.rel(it.start, it.end, screen, self.P.CONTAINS, control)
            self.construct("nat:map_field", it.start, it.end, True)
        for it in src.items:
            if it.kind == "text":
                self.construct("nat:map_text", it.start, it.end, (it.line,) in labelled and it.line is not None)
        for a, b in src.rules:
            self.construct("nat:map_rule", a, b, False)
        if src.rules:
            self.note(f"{len(src.rules)} statement(s) of the map other than its fields (processing rules) not read")

    # -------------------------------------------------------------- DDMs

    def ddm(self, stem: str, text: str) -> None:
        parsed = natddm.parse(text)
        name = parsed.name or stem
        a, b = parsed.name_at if parsed.name else (0, len(text))
        if parsed.type not in ("ADABAS", "ADA", "A"):
            self.construct("nat:ddm_sql", 0, len(text), False)
            self.note(f"DDM {name} is of type {parsed.type}: its table is read from the SQL that names it, not from the DDM")
            return
        table = self.term(a, b, "nat:Ddm", {"datastore": ADABAS, "name": name}, name, {"object_type": "ddm", "fields": len(parsed.fields)}, self.EC.E1)
        self.construct("nat:ddm", a, b, True, self.EC.E1)
        layout = f"{ADABAS}:{name}"
        seen: set[str] = set()
        for f in parsed.fields:
            if f.name in seen:
                self.construct("nat:ddm_unread", f.start, f.end, False, self.EC.E1)
                continue
            seen.add(f.name)
            fref = self.term(f.start, f.end, "nat:DdmField", {"table": layout, "name": f.name}, f.name, f.attrs, self.EC.E1)
            self.rel(f.start, f.end, table, self.P.CONTAINS, fref, None, self.EC.E1)
            self.construct("nat:ddm_field", f.start, f.end, True, self.EC.E1)
            for flag, cid in (("group", "nat:ddm_group"), ("mu", "nat:ddm_mu"), ("pe", "nat:ddm_pe"), ("descriptor", "nat:ddm_descriptor")):
                if f.attrs.get(flag) and not (flag == "group" and f.attrs.get("pe")):
                    self.construct(cid, f.start, f.end, True, self.EC.E1)
        for x, y in parsed.unread:
            self.construct("nat:ddm_unread", x, y, False, self.EC.E1)
        if not parsed.fields:
            self.note(f"DDM {name}: no field lines found")


# Keys of a statement that hold statement lists (or cases and catches that hold them).
_BLOCKS = ("body", "then", "else", "default", "finally", "cases", "catches")
_SQL_STRING = re.compile(r"'(?:[^'\n]|'')*'")


def _target_name(node: dict[str, Any]) -> str:
    target = node.get("target") or {"name": node.get("name")}
    while isinstance(target, dict) and target.get("e") == "index":
        target = target.get("object") or {}  # #PASSWORDS(2): the field #PASSWORDS
    return str(target.get("name") or "") if isinstance(target, dict) else ""


def _literal_value(value: Any) -> bool:
    return isinstance(value, dict) and value.get("e") in ("lit", "template", "array", "call")


def _names_sensitive(values: list) -> bool:
    """Whether the expressions in `values` name a sensitive field: a variable, a view field,
    a database loop's descriptor or a query's host variable."""
    stack = list(values)
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if x.get("e") in ("var", "member") and _SENSITIVE.search(str(x.get("name") or "")):
                return True
            if isinstance(x.get("descriptor"), str) and _SENSITIVE.search(x["descriptor"]):
                return True
            hosts = x.get("host_vars")
            if isinstance(hosts, dict) and any(isinstance(h, str) and _SENSITIVE.search(h) for v in hosts.values() if isinstance(v, list) for h in v):
                return True
            stack.extend(v for k, v in x.items() if k not in _BLOCKS)
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _mask(values: list) -> bool:
    """Replace the string literals in `values` with `nat:sensitive_value` nodes, in place;
    whether any was replaced."""
    found = False
    stack = list(values)
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            e = x.get("e")
            text = x.get("text")
            quoted = isinstance(text, str) and ("'" in text or '"' in text)
            if (e == "lit" and isinstance(x.get("value"), str)) or e == "template" or (
                    e == "platform" and x.get("construct") == "nat:unrecognised_expression" and quoted):
                x.clear()
                x.update({"e": "platform", "construct": "nat:sensitive_value"})
                found = True
                continue
            stack.extend(v for k, v in x.items() if k not in _BLOCKS)
    return found


def _params(defn: natdata.DataDefinition | None) -> list[dict[str, Any]]:
    """The behaviour unit's parameters: the level-1 fields of the PARAMETER section."""
    if defn is None:
        return []
    out = []
    for it in defn.items:
        if it.section == "PARAMETER" and not it.attrs.get("redefinition"):
            p: dict[str, Any] = {"name": it.name, "type": "struct" if it.children else it.attrs.get("type", "any"), "required": not it.attrs.get("optional")}
            out.append(p)
    return out


def _declarations(defn: natdata.DataDefinition, to_byte) -> list[dict[str, Any]]:
    """`declare` statements for the object's own LOCAL and INDEPENDENT fields."""
    out = []
    for it in defn.walk():
        if it.section not in ("LOCAL", "INDEPENDENT", "CONTEXT") or it.attrs.get("redefinition"):
            continue
        node: dict[str, Any] = {"op": "declare", "at": [to_byte(it.start), to_byte(it.end)], "name": it.name, "scope": "global" if it.section == "INDEPENDENT" else "local",
                                "type": "struct" if it.children else it.attrs.get("type", "any"), "level": it.level}
        if it.view_of:
            node["view_of"] = it.view_of
        if it.attrs.get("occurs") is not None:
            node["occurs"] = it.attrs["occurs"]
        if it.parent is not None:
            node["parent"] = it.parent.name
        out.append(node)
    return out


def _field_names(items: list[natdata.DataItem]):
    """Each item with its Field name: its data name, or `<parent>.<name>` for a name used
    more than once in the data area (then `~2`, `~3` if that repeats too)."""
    items = [it for it in items if not it.attrs.get("redefinition")]  # `1 REDEFINE #X` is not a field
    counts: dict[str, int] = {}
    for it in items:
        counts[it.name] = counts.get(it.name, 0) + 1
    used: dict[str, int] = {}
    for it in items:
        name = it.name
        if counts[name] > 1 and it.parent is not None:
            name = f"{it.parent.name}.{it.name}"
        used[name] = used.get(name, 0) + 1
        yield it, (name if used[name] == 1 else f"{name}~{used[name]}")


def _editor_layout(text: str) -> bool:
    return any(line.lstrip().startswith("I T L Name") or line.lstrip().startswith("T L Name") for line in text.split("\n")[:40])


def _db_loops(body: list[dict[str, Any]]):
    """Database loops in a body, outermost first, at any depth."""
    from repo_intel.compile.behavior import walk

    for node in walk(body):
        if node.get("op") == "loop" and isinstance(node.get("over"), dict) and node["over"].get("construct") in ("nat:read", "nat:find", "nat:histogram"):
            yield node


def _writes(body: list[dict[str, Any]]):
    """STORE and UPDATE nodes in a loop body (not inside a nested database loop), each with
    the view fields assigned before it in that body."""
    assigned: list[str] = []
    stack = [list(body)]
    order: list[dict[str, Any]] = []

    def visit(block):
        for node in block:
            if node.get("op") == "loop" and isinstance(node.get("over"), dict) and node["over"].get("construct") in ("nat:read", "nat:find", "nat:histogram"):
                continue
            order.append(node)
            for k in ("then", "else", "body"):
                if isinstance(node.get(k), list):
                    visit(node[k])
            for case in node.get("cases", []) or []:
                visit(case.get("body", []))
            if isinstance(node.get("default"), list):
                visit(node["default"])

    visit(stack[0])
    for node in order:
        if node.get("op") == "assign":
            t = node.get("target") or {}
            while t.get("e") == "index" and isinstance(t.get("object"), dict):
                t = t["object"]  # SALARY(1): the field SALARY
            if t.get("e") == "member" and isinstance(t.get("object"), dict) and t["object"].get("scope") == "view":
                assigned.append(t.get("name", ""))
        if node.get("op") == "platform" and node.get("construct") in ("nat:store", "nat:update"):
            yield node, [a for a in assigned if a]


class NaturalSource:
    id = "natural.source"
    version = "1.1.0"
    SUFFIXES = tuple(OBJECT_TYPES)

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        if len(data) > MAX_BYTES:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        raw = decode(data)
        ext = posixpath.splitext(path)[1].lower()
        stem = posixpath.splitext(posixpath.basename(path))[0].upper()
        o = _Emit(self.id, path, data, blob, raw)
        if ext == ".nsd":
            o.ddm(stem, raw)
            return o.done()
        text, mode, numbers = prepare(raw)
        if ext in _AREA_TERMS:
            o.data_area(ext, stem, text)
        elif ext == ".nsm":
            o.map(stem, text)
        else:
            o.code(ext, stem, text, mode, numbers)
        return o.done()


__all__ = ["NaturalSource"]
