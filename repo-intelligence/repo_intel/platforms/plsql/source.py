"""`plsql.source`: Oracle PL/SQL source files and Oracle deployment scripts.

Reads `.pks`, `.pkb`, `.pls`, `.plb`, `.fnc` and `.trg` files, and `.prc` and `.sql` files
whose content is Oracle's (`recognise`): a `.prc` is also a JCL procedure suffix, so one
is read only when it creates a PL/SQL unit and has no JCL statements; a `.sql` file only
when it creates a package, procedure, function or trigger in Oracle's syntax, or holds
statements only Oracle has (synonyms, database links, DBMS_SCHEDULER and DBMS_JOB calls).

What it declares (natural keys in `vocabulary`):

- a package as a `Program` that CONTAINS a `Symbol` for each procedure and function; the
  specification and the body declare the same entities, each naming its own file;
- a stand-alone procedure or function, and a trigger (contract 10.1, item 7), as a
  `Program`; a trigger's `attributes.on` gives `{table, events, timing}` (a schema or
  database trigger has `scope` instead of a table);
- a behaviour unit (repo_intel.compile.behavior) for every procedure, function, trigger,
  package initialization and anonymous block that runs SQL or calls code; the compiler
  derives transform shapes from their `query` nodes;
- the tables each one reads and writes, read with the shared SQL lineage scanner in the
  `oracle` dialect; a table over a database link (`orders@hq`) is
  `Table {datastore: <link>, name}`, and the code REFERENCES_CONFIG the link;
- CALLS to the procedures and functions it calls: within the package exactly, elsewhere by
  name without declaring them, so a call links only when the unit defines its target;
- database links as `ConfigKey`s, synonyms as ALIAS_OF between their name and the object
  they stand for, GRANTs as `Role GRANTS Permission` (or `Role GRANTS Role`), and
  DBMS_SCHEDULER and DBMS_JOB jobs as `Job`s that RUN the procedures their literal actions
  name.

Dynamic SQL (EXECUTE IMMEDIATE, OPEN ... FOR, DBMS_SQL) whose text is built at run time
stays dynamic, with a diagnostic, and is counted. Wrapped source is inventoried and
reported as a gap. A unit whose structure the parser cannot read keeps its Program, and
its SQL is read for lineage and transform shapes (language `plsql`) without behaviour.
Passwords, connect strings and mail addresses are never copied.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.plsql.parse import Body, Job, Parser, Script, Unit, block_calls, scrub_text
from repo_intel.platforms.plsql.vocabulary import PLATFORM, REALM

LANGUAGE = "plsql"
_NATIVE = (".pks", ".pkb", ".pls", ".plb", ".fnc", ".trg")
_JCL = re.compile(r"^//[A-Za-z0-9@#$]*\s+(?:JOB|PROC|EXEC|DD|PEND|SET|INCLUDE|JCLLIB)\b", re.M | re.I)
_HEAD = r"^[ \t]*create\s+(?:or\s+replace\s+)?(?:(?:editionable|noneditionable)\s+)?"
_PACKAGE = re.compile(_HEAD + r"package\b", re.I | re.M)
_SUBPROGRAM = re.compile(_HEAD + r"(?:procedure|function)\s+[\w$#.\"]+(.{0,4000}?)\b(?:is|as)\b(\s*\$\$|\s*'|\s*begin\b)?", re.I | re.M | re.S)
_TRIGGER = re.compile(_HEAD + r"trigger\s+[\w$#.\"]+\s+(?:before|after|instead\s+of|for)\b(.{0,2000}?)\b(?:begin|declare|call|compound\s+trigger)\b", re.I | re.M | re.S)
_ORACLE_ONLY = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?(?:public\s+)?synonym\b|\bcreate\s+(?:shared\s+)?(?:public\s+)?database\s+link\b|\bdbms_(?:scheduler|job)\s*\.", re.I)
_OR_REPLACE = re.compile(r"\bcreate\s+or\s+replace\b", re.I)
_SYSTEM_PRIVILEGES = frozenset({"SYSDBA", "SYSOPER", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "ALL", "ALL PRIVILEGES"})


def recognise(path: str, text: str) -> bool:
    """Whether a file is Oracle PL/SQL or an Oracle deployment script."""
    ext = posixpath.splitext(path)[1].lower()
    if ext in _NATIVE:
        return True
    if ext == ".prc":
        return not _JCL.search(text) and (_PACKAGE.search(text) is not None or _oracle_subprogram(text) or _TRIGGER.search(text) is not None)
    if ext == ".sql":
        from repo_intel.extractors.sql_lineage import sql_platform

        # The Teradata test there takes `replace procedure` as Teradata's REPLACE PROCEDURE,
        # which `create or replace procedure` also contains, so that phrase is set aside first.
        if sql_platform(path, _OR_REPLACE.sub("create", text)) != "sql":
            return False  # T-SQL or Teradata: the SQL lineage extractor's
        return bool(_PACKAGE.search(text) or _oracle_subprogram(text) or _oracle_trigger(text) or _ORACLE_ONLY.search(text))
    return False


def _oracle_subprogram(text: str) -> bool:
    for m in _SUBPROGRAM.finditer(text):
        head, after = m.group(1), m.group(2)
        if after and after.strip().lower() != "begin" or re.search(r"\b(?:returns|language|begin|declare)\b|@\w", head, re.I):
            continue  # PostgreSQL ($$ or a quoted body, RETURNS, LANGUAGE), MySQL or DB2 (BEGIN with no IS), T-SQL (@parameters)
        return True
    return False


def _oracle_trigger(text: str) -> bool:
    return any(not re.search(r"\bexecute\s+(?:function|procedure)\b", m.group(1), re.I) for m in _TRIGGER.finditer(text))


def _lib():
    """Extractor helpers, imported when first used: a pack module must not import the
    `repo_intel.extractors` package while it is being discovered."""
    from repo_intel import platforms
    from repo_intel.compile.behavior import behavior_observation
    from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
    from repo_intel.extractors import base, sql_lineage

    return platforms, behavior_observation, EntityKind, EvidenceClass, Predicate, base, sql_lineage


class _Emit:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        (self.platforms, self.behavior_observation, self.K, self.EC, self.P, self.base, self.sql) = _lib()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.to_byte(a), self.to_byte(max(a, b)))

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, {k: v for k, v in (attrs or {}).items() if v is not None}, self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, attrs or {}, self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, self.EC.E2))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    # -------------------------------------------------------------- references

    def program(self, name: str, declare: bool = True) -> dict[str, Any]:
        return self.base.ref(self.K.PROGRAM, {"platform": PLATFORM, "name": name}, name, declare=declare)

    def symbol(self, program: str, name: str, declare: bool = True) -> dict[str, Any]:
        return self.base.ref(self.K.SYMBOL, {"platform": PLATFORM, "program": program, "name": name}, name, declare=declare)

    def callees(self, parts: list[str], package: str | None, members: set[str]) -> list[dict[str, Any]]:
        """What a call or a job's action may name. Within the package it is exact; elsewhere it
        is every reading of the dotted name, none declared, so only a defined target links."""
        short = package.rsplit(".", 1)[-1] if package else None
        if len(parts) == 1:
            if package and parts[0] in members:
                return [self.symbol(package, parts[0])]
            return [self.program(parts[0], declare=False)]
        if len(parts) == 2:
            if package and parts[0] in (package, short):
                return [self.symbol(package, parts[1], declare=parts[1] in members)]
            return [self.symbol(parts[0], parts[1], declare=False), self.program(".".join(parts), declare=False)]
        head = ".".join(parts[:-1])
        if package and head == package:
            return [self.symbol(package, parts[-1], declare=parts[-1] in members)]
        return [self.symbol(head, parts[-1], declare=False)]

    # -------------------------------------------------------------- facts of a body

    def table(self, name: str) -> tuple[dict[str, Any], str | None]:
        base, _, link = name.partition("@")
        if link:
            return self.base.ref(self.K.TABLE, {"datastore": link, "name": base}, base), link
        return self.base.ref(self.K.TABLE, {"datastore": "default", "name": name}, name), None

    def body_facts(self, owner: dict[str, Any], body: Body, package: str | None, members: set[str]) -> None:
        seen: set[tuple[str, str]] = set()
        links: set[str] = set()
        for stmt in body.sql:
            try:
                found = self.sql.lineage(stmt.text, "oracle", views=False)
            except Exception as exc:  # a scanner fault on hostile input: the statement is counted, not read
                self.note(f"SQL at line {self.text.count(chr(10), 0, stmt.start) + 1} not read: {type(exc).__name__}", "warning")
                self.unresolved += 1
                continue
            self.unresolved += found.unresolved
            for predicate, items in ((self.P.WRITES_TO, found.writes), (self.P.READS_FROM, found.reads)):
                for name, a, b in items:
                    if (predicate, name) in seen:
                        continue
                    seen.add((predicate, name))
                    self.resolved += 1
                    a, b = (stmt.start, stmt.end) if stmt.dynamic else (stmt.start + a, stmt.start + b)
                    target, link = self.table(name)
                    self.entity(a, b, self.K.TABLE, target["natural_key"], target["name"], {"db_link": link} if link else {})
                    self.rel(a, b, owner, predicate, target)
                    if link:
                        links.add(link)
        for link, a, b in body.links:
            if link in links:
                self.platforms_construct("ora:remote_object", a, b, True)
                links.discard(link)
                self.rel(a, b, owner, self.P.REFERENCES_CONFIG, self.base.ref(self.K.CONFIG_KEY, {"name": f"dblink:{link}"}, f"dblink:{link}", declare=False))
        called: set[str] = set()
        for call in body.calls:
            key = ".".join(call.parts)
            if key in called:
                continue
            called.add(key)
            for target in self.callees(call.parts, package, members):
                if target == owner:
                    continue  # recursion
                self.rel(call.start, call.end, owner, self.P.CALLS, target)

    def platforms_construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), cid, modelled))

    def behavior(self, owner: dict[str, Any], body: Body, a: int, b: int, kind: str) -> None:
        from repo_intel.platforms.cfml.urls import scrub_unit

        params = [dict(p) for p in body.params]
        statements = body.statements
        scrub_unit(statements, params)
        try:
            self.items.append(self.behavior_observation(self.blob, self.span(a, b), owner, LANGUAGE, statements, params, self.EC.E2, kind=kind))
        except ValueError as exc:  # a lowering fault: the unit keeps its relations, its behaviour is left out
            self.note(f"behaviour of {owner['name']} not emitted: {str(exc)[:120]}", "warning")

    # -------------------------------------------------------------- units

    def unit(self, unit: Unit) -> None:
        a, b = unit.name_at
        if unit.kind in ("package", "package_body"):
            attrs = {"kind": "package", ("body" if unit.kind == "package_body" else "specification"): self.path, **({"wrapped": True} if unit.wrapped else {})}
            if unit.attrs.get("options"):
                attrs["options"] = unit.attrs["options"]
            me = self.term(a, b, "ora:Package", {"platform": PLATFORM, "name": unit.name}, unit.name, attrs)
            members = {s.name for s in unit.subprograms}
            counts: dict[str, int] = {}
            for s in unit.subprograms:
                counts[s.name] = counts.get(s.name, 0) + 1
            for s in unit.subprograms:
                term = "ora:PackageFunction" if s.kind == "function" else "ora:PackageProcedure"
                sattrs: dict[str, Any] = {"symbol_kind": s.kind}
                if unit.kind == "package":
                    sattrs["public"] = True
                elif counts[s.name] > 1:
                    sattrs["overloads"] = counts[s.name]
                if s.returns and counts[s.name] == 1:
                    sattrs["returns"] = s.returns
                if s.autonomous:
                    sattrs["autonomous_transaction"] = True
                if s.options:
                    sattrs["options"] = s.options
                if s.call_spec:
                    sattrs["call_spec"] = True
                sym = self.term(s.name_at[0], s.name_at[1], term, {"platform": PLATFORM, "program": unit.name, "name": s.name}, s.name, sattrs)
                self.rel(s.name_at[0], s.name_at[1], me, self.P.CONTAINS, sym)
                if s.body is not None:
                    self.body_facts(sym, s.body, unit.name, members)
                    self.behavior(sym, s.body, s.start, s.end, "function")
            if unit.init is not None:
                self.body_facts(me, unit.init, unit.name, members)
                self.behavior(me, unit.init, unit.start, unit.end, "open_code")
            if unit.error:
                self.fallback(me, unit)
            return
        term = {"procedure": "ora:Procedure", "function": "ora:Function", "trigger": "ora:Trigger"}[unit.kind]
        attrs = {"kind": unit.kind, **{k: v for k, v in unit.attrs.items() if k != "params" or v}, **({"wrapped": True} if unit.wrapped else {})}
        me = self.term(a, b, term, {"platform": PLATFORM, "name": unit.name}, unit.name, attrs)
        if unit.body is not None:
            self.body_facts(me, unit.body, None, set())
            self.behavior(me, unit.body, unit.start, unit.end, "trigger" if unit.kind == "trigger" else "function")
        if unit.error:
            self.fallback(me, unit)

    def fallback(self, owner: dict[str, Any], unit: Unit) -> None:
        """A unit whose structure was not read: its SQL statements, for lineage and transform shapes."""
        from repo_intel.compile.transforms import sql_unit, transform_observation
        from repo_intel.extractors.contracts_data import split_statements

        text = self.text[unit.start : unit.end]
        body = Body()
        statements: list[tuple[int, int, list[int]]] = []
        for x, y in split_statements(text):
            m = re.search(r"\b(select|insert|update|delete|merge|with)\b", text[x:y], re.I)
            if not m:
                continue
            s = x + m.start()
            stmt = text[s:y]
            if m.group(1).lower() in ("select", "with"):
                stmt = re.sub(r"\b(?:bulk\s+collect\s+)?into\b.*?(?=\bfrom\b)", lambda q: " " * len(q.group()), stmt, count=1, flags=re.I | re.S)
            stmt = re.sub(r"\breturning\b.*$", lambda q: " " * len(q.group()), stmt, flags=re.I | re.S)
            from repo_intel.platforms.plsql.parse import Sql

            body.sql.append(Sql(m.group(1).lower(), stmt, unit.start + s, unit.start + y))
            statements.append((s, y, [self.to_byte(unit.start + s), self.to_byte(unit.start + y)]))
        self.body_facts(owner, body, None, set())
        if not statements:
            return
        blanked = text
        for sql in body.sql:
            s = sql.start - unit.start
            blanked = blanked[:s] + sql.text + blanked[s + len(sql.text) :]
        try:
            shapes = sql_unit(blanked, statements, "oracle")
            if shapes:
                self.items.append(transform_observation(self.blob, self.span(unit.start, unit.end), owner, LANGUAGE, shapes))
        except Exception as exc:  # a recognizer fault: the lineage stands, the shapes are left out
            self.note(f"transform shapes of {unit.name} not derived: {type(exc).__name__}", "warning")

    # -------------------------------------------------------------- scripts

    def blocks(self, script: Script) -> None:
        running = [(body, a, b) for body, a, b in script.blocks if body.sql or body.calls]
        if not running:
            return
        me = self.term(running[0][1], running[0][1], "ora:Script", {"platform": PLATFORM, "name": self.path}, posixpath.basename(self.path), {"kind": "script"})
        for body, a, b in running:
            self.body_facts(me, body, None, set())
            self.behavior(me, body, a, b, "open_code")

    def grants(self, script: Script) -> None:
        for g in script.grants:
            for privilege, columns in g.privileges:
                if not privilege:
                    continue
                for grantee in g.grantees:
                    role = self.term(g.start, g.end, "ora:Role", {"realm": REALM, "name": grantee}, grantee, {"pseudo": True} if grantee == "public" else {})
                    qualifiers = {"grant_option": True} if g.grant_option else {}
                    if g.object is None and " " not in privilege and privilege not in _SYSTEM_PRIVILEGES:
                        granted = privilege.lower()
                        other = self.term(g.start, g.end, "ora:Role", {"realm": REALM, "name": granted}, granted)
                        self.rel(g.start, g.end, role, self.P.GRANTS, other, {"via": "role", **qualifiers})
                        self.platforms_construct("ora:grant_role", g.start, g.end, True)
                        continue
                    names = [f"{privilege} on {g.object}.{c}" for c in columns] if g.object and columns else [f"{privilege} on {g.object}" if g.object else privilege]
                    for name in names:
                        attrs = {"privilege": privilege, "object": g.object, "object_kind": g.object_kind}
                        perm = self.term(g.start, g.end, "ora:Grant", {"realm": REALM, "name": name}, name, attrs)
                        self.rel(g.start, g.end, role, self.P.GRANTS, perm, qualifiers)
                    self.platforms_construct("ora:grant_object" if g.object else "ora:grant_system", g.start, g.end, True)

    def synonyms(self, script: Script) -> None:
        for s in script.synonyms:
            q = {"via": "synonym", **({"public": True} if s.public else {})}
            target = self.base.ref(self.K.TABLE, {"datastore": s.link or "default", "name": s.target}, s.target, declare=False)
            self.rel(s.start, s.end, self.base.ref(self.K.TABLE, {"datastore": "default", "name": s.name}, s.name, declare=False), self.P.ALIAS_OF, target, q)
            if not s.link:
                self.rel(s.start, s.end, self.program(s.name, declare=False), self.P.ALIAS_OF, self.program(s.target, declare=False), q)

    def dblinks(self, script: Script) -> None:
        for d in script.dblinks:
            name = f"dblink:{d.name}"
            self.term(d.start, d.end, "ora:DatabaseLink", {"name": name}, name, {"public": d.public or None, "shared": d.shared or None, "authentication": d.authentication})

    def jobs(self, script: Script, parser: Parser) -> None:
        for job in script.jobs:
            if job.name is None:
                continue
            attrs = dict(job.attrs)
            runs = list(job.runs)
            if job.runs_program and job.runs_program in script.programs:
                ptype, action = script.programs[job.runs_program]
                attrs["job_type"] = ptype.lower()
                runs = parser.action_calls(ptype, action)
            if attrs.get("schedule_name") in script.schedules:
                attrs["schedule"] = script.schedules[attrs["schedule_name"]]
            if "schedule" in attrs:
                attrs["schedule"] = scrub_text(str(attrs["schedule"]))
            term = "ora:SchedulerJob" if job.framework == "dbms_scheduler" else "ora:Job"
            me = self.term(job.start, job.end, term, {"framework": job.framework, "name": job.name}, job.name, attrs)
            for parts in runs:
                for target in self.callees(parts, None, set()):
                    self.rel(job.start, job.end, me, self.P.RUNS, target)


class PlsqlSource:
    id = "plsql.source"
    version = "1.0.0"
    SUFFIXES = (".pks", ".pkb", ".pls", ".plb", ".prc", ".fnc", ".trg", ".sql")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")  # one byte per character, so spans stay byte spans
        if not recognise(path, text):
            return Extraction(parsed=False)
        o = _Emit(self.id, path, data, blob, text)
        parser = Parser(text, o.to_byte)
        try:
            script = parser.script()
        except RecursionError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "nesting too deep to read")], parsed=False)
        for unit in script.units:
            o.unit(unit)
        o.blocks(script)
        o.grants(script)
        o.synonyms(script)
        o.dblinks(script)
        o.jobs(script, parser)
        for cid, a, b, modelled in script.constructs:
            o.platforms_construct(cid, a, b, modelled)
        for severity, message in script.diagnostics:
            o.note(message, severity)
        o.items += o.sql.lineage_summary(blob, data, o.resolved, o.unresolved)
        return Extraction(o.items, diagnostics=o.diagnostics)


__all__ = ["PlsqlSource", "block_calls", "recognise"]
