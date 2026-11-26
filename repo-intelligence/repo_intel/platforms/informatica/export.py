"""A PowerCenter XML export (root `POWERMART`) into the graph.

What one export gives, folder by folder:

- Source and target definitions: a relational one as `Table {datastore: "default", name}`
  (`owner.name` when the definition names its owner, lower case); a flat file, XML or VSAM
  one as a record layout `DataSchema {kind: "record_layout", name: "informatica:<folder>/<name>"}`
  with a `Field` per field.
- Each mapping as `Program {platform: "informatica", name: "<folder>/<mapping>"}` that
  READS_FROM its sources and lookup tables and WRITES_TO its targets, with transform shapes
  (language `informatica`, evidence E1) built from its instances and connectors, and a
  `flow` behaviour unit when it has expressions, filters or router groups. Mapping
  parameters become `ConfigKey parameter:$$NAME`; their default values are never read.
- Each workflow and worklet as `Job {framework: "informatica", name: "<folder>/<name>"}`
  that CONTAINS its task instances as `Task {framework, job, name: <instance name>}`, with
  DEPENDS_ON from workflow links. A session RUNS its mapping and REFERENCES_CONFIG the
  connections its readers, writers and lookups use, by name (`connection:<name>`), or the
  session parameter that names one (`parameter:$DBConnection_...`). The Start task becomes
  the Job's `entry` attribute; the scheduler its `schedule` attribute.

Never read: connect strings, user names and passwords (an export holds none, but session
attributes and command tasks may), the commands of command tasks, mail addresses, file
paths, parameter and variable values. A table or connection named by a parameter stays a
parameter, with a diagnostic; it is not resolved by guesswork. SQL overrides go through the
shared SQL recognizer with `$$NAME` read as a bind parameter `:NAME`, so a table it names is
dynamic.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass
from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.informatica.vocabulary import DIALECTS, FRAMEWORK, NON_RELATIONAL, PLATFORM, task_type, transformation
from repo_intel.platforms.ssis import etlexpr
from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.xmlread import Doc, Place, attr, child, children, local

E2 = EvidenceClass.E2
_NAME_OK = re.compile(r"[^\x00-\x1f<>\[\]]{1,200}")
_PARAM = re.compile(r"\$\$([A-Za-z_]\w*)")
_STATUS = re.compile(r"^\s*\$([\w.]+)\.(?:Status|PrevTaskStatus)\s*=\s*(SUCCEEDED|FAILED|STOPPED|ABORTED)\s*$", re.I)
_OVERRIDES = ("Sql Query", "User Defined Join", "Source Filter", "Lookup Sql Override", "Lookup table name", "Table Name Prefix", "Target Table Name", "Update Override", "Pre SQL", "Post SQL")


def table_attrs(el) -> dict[str, str]:
    """TABLEATTRIBUTE (and ATTRIBUTE) name -> value of an element."""
    out: dict[str, str] = {}
    for c in el:
        if local(c.tag) in ("TABLEATTRIBUTE", "ATTRIBUTE") and attr(c, "NAME"):
            out.setdefault(attr(c, "NAME"), attr(c, "VALUE") or "")
    return out


def ok_name(name: str | None) -> bool:
    return bool(name and _NAME_OK.fullmatch(name))


def config_ref(name: str, declare: bool = True) -> dict[str, Any]:
    out = {"kind": "ConfigKey", "natural_key": {"name": name}, "name": name}
    return out if declare else {**out, "declare": False}


def bind_parameters(sql: str) -> str:
    """`$$NAME` as the bind parameter `:NAME` the shared recognizer reads."""
    return _PARAM.sub(lambda m: ":" + m.group(1), sql)


def table_name(raw: str | None) -> str | None:
    from repo_intel.extractors.sql_lineage import clean_name

    if not raw or not raw.strip() or "$" in raw:
        return None
    name = clean_name(raw.strip())
    return name if re.fullmatch(r"[\w#@.]+", name) else None


class Definition:
    """A source or target definition: where its rows are, for transforms and relations."""

    def __init__(self, kind: str, name: str, ref: dict[str, Any], dialect: str | None):
        self.kind, self.name, self.ref, self.dialect = kind, name, ref, dialect  # kind: table | layout


class Export:
    """One export: every folder's definitions, mappings, workflows and worklets."""

    def __init__(self, out: Out, doc: Doc, data: bytes):
        self.out, self.doc, self.data = out, doc, data
        self.place = Place(0, len(data), True)

    def at(self, el):
        s = self.doc.span(el)
        return self.out.span(self.place, *s) if s else self.out.whole()

    def at_list(self, el) -> list[int]:
        s = self.doc.span(el)
        return [s[0], s[1]] if s else [0, len(self.data)]

    def run(self) -> None:
        out = self.out
        root = self.doc.root
        out.construct(self.at(root), "infa:export", True)
        for repo in children(root, "REPOSITORY"):
            for folder in children(repo, "FOLDER"):
                self.folder(folder)

    # ------------------------------------------------------------------ folders

    def folder(self, f) -> None:
        out = self.out
        fname = attr(f, "NAME")
        if not ok_name(fname) or "/" in fname:
            out.note("info", f"a folder with an unusable name in {out.path}: not read")
            return
        out.construct(self.at(f), "infa:folder", True)
        self.fname = fname
        self.sources: dict[str, Definition] = {}
        self.targets: dict[str, Definition] = {}
        self.reusable = {attr(t, "NAME"): t for t in children(f, "TRANSFORMATION") if attr(t, "NAME")}
        self.sessions = {attr(s, "NAME"): s for s in children(f, "SESSION") if attr(s, "NAME")}
        self.tasks = {attr(t, "NAME"): t for t in children(f, "TASK") if attr(t, "NAME")}
        self.worklets = {attr(w, "NAME") for w in children(f, "WORKLET") if attr(w, "NAME")}
        self.mappings = {attr(m, "NAME") for m in children(f, "MAPPING") if attr(m, "NAME")}
        for el in children(f, "SOURCE"):
            d = self.definition(el, "source")
            if d:
                self.sources[attr(el, "NAME")] = d
        for el in children(f, "TARGET"):
            d = self.definition(el, "target")
            if d:
                self.targets[attr(el, "NAME")] = d
        for el in children(f, "SHORTCUT"):
            out.construct(self.at(el), "infa:shortcut", False)
            out.note("info", f"shortcut {attr(el, 'NAME')} in folder {fname} points to an object of folder {attr(el, 'FOLDERNAME') or 'unknown'}: not in this export")
        for el in children(f, "MAPPLET"):
            out.construct(self.at(el), "infa:mapplet", False)
        for el in children(f, "CONFIG"):
            out.construct(self.at(el), "infa:session_config", False)
        for el in children(f, "MAPPING"):
            if ok_name(attr(el, "NAME")):
                Mapping(self, el).run()
        for el in children(f, "WORKLET"):
            if ok_name(attr(el, "NAME")):
                Flow(self, el, "worklet").run()
        for el in children(f, "WORKFLOW"):
            if ok_name(attr(el, "NAME")):
                Flow(self, el, "workflow").run()

    def definition(self, el, role: str) -> Definition | None:
        out = self.out
        name = attr(el, "NAME")
        if not ok_name(name):
            return None
        at = self.at(el)
        dbtype = (attr(el, "DATABASETYPE") or "").strip()
        dialect_key = DIALECTS.get(dbtype.lower())
        if dbtype.lower() in NON_RELATIONAL:
            out.construct(at, f"infa:{role}.flat_file", True)
            layout = f"{PLATFORM}:{self.fname}/{name}"
            key = {"kind": "record_layout", "name": layout}
            fields = [x for x in el if local(x.tag) in ("SOURCEFIELD", "TARGETFIELD") and ok_name(attr(x, "NAME"))]
            ref = out.entity(at, "infa:FlatFile", key, name, {"role": role, "database_type": dbtype, "folder": self.fname, "fields": len(fields)})
            for i, fl in enumerate(fields, 1):
                number = attr(fl, "FIELDNUMBER")
                f = out.entity(self.at(fl), "infa:FlatFileField", {"table": layout, "name": attr(fl, "NAME")}, attr(fl, "NAME"), {
                    "type": attr(fl, "DATATYPE"), "position": int(number) if number and number.isdigit() else i,
                    "length": int(attr(fl, "PRECISION")) if (attr(fl, "PRECISION") or "").isdigit() else None,
                })
                out.relation(self.at(fl), ref, P.CONTAINS, f)
            return Definition("layout", layout.lower(), ref, None)
        owner = (attr(el, "OWNERNAME") or "").strip()
        tname = table_name(f"{owner}.{name}" if owner and role == "source" else name)
        if tname is None:
            out.construct(at, f"infa:{role}.relational", False)
            return None
        out.construct(at, f"infa:{role}.relational", True)
        ref = out.neutral(at, K.TABLE, {"datastore": "default", "name": tname}, tname)
        from repo_intel.extractors.sql_lineage import DIALECT

        return Definition("table", tname, ref, DIALECT.get(dialect_key) if dialect_key else None)

    def connection(self, at, subject, value: str | None, via: dict[str, Any] | None = None) -> None:
        """A connection named in a session or lookup: by name, by session parameter, or built in."""
        out = self.out
        value = (value or "").strip()
        if not value:
            return
        if ":" in value and not value.startswith("$"):
            value = value.split(":", 1)[1].strip()  # "Relational:ORA_DW"
        if value.lower() in ("$source", "$target"):
            out.construct(at, "infa:connection_builtin", False)
            return
        if value.startswith("$"):
            if not re.fullmatch(r"\$[A-Za-z_]\w*", value):
                return
            out.construct(at, "infa:connection_variable", True)
            key = f"parameter:{value}"
            ref = out.entity(at, "infa:Parameter", {"name": key}, key, {"kind": "session_parameter"})
            out.relation(at, subject, P.REFERENCES_CONFIG, ref, {**(via or {}), "resolved_by": "parameter_file"})
            return
        if not ok_name(value):
            return
        out.construct(at, "infa:connection_reference", True)
        key = f"connection:{value}"
        ref = out.entity(at, "infa:Connection", {"name": key}, key)
        out.relation(at, subject, P.REFERENCES_CONFIG, ref, via)

    def lineage(self, at, sql: str, subjects: list[dict[str, Any]], dialect: str | None, via: str) -> None:
        from repo_intel.extractors import sql_lineage

        found = sql_lineage.lineage(sql_lineage.scrub_commands(bind_parameters(sql)), dialect)
        for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
            for name, _, _ in items:
                if ":" in name:
                    continue
                t = self.out.neutral(at, K.TABLE, {"datastore": "default", "name": name}, name, {}, E2)
                for i, s in enumerate(subjects):
                    self.out.relation(at, s, predicate, t, {"via": via} if i else None, E2)
        self.out.resolved += len(found.writes) + len(found.reads)
        self.out.unresolved += found.unresolved


class Mapping:
    """One mapping: definitions read, instances wired by connectors, transforms and a flow unit."""

    def __init__(self, exp: Export, el):
        from repo_intel.compile.transforms import Builder

        self.exp, self.out, self.el = exp, exp.out, el
        self.name = f"{exp.fname}/{attr(el, 'NAME')}"
        self.b = Builder()
        self.flow: list[dict[str, Any]] = []
        self.lowered = False
        self.local = {attr(t, "NAME"): t for t in children(el, "TRANSFORMATION") if attr(t, "NAME")}
        self.program = {"kind": "Program", "natural_key": {"platform": PLATFORM, "name": self.name}, "name": attr(el, "NAME")}

    def run(self) -> None:
        from repo_intel.compile.behavior import behavior_observation
        from repo_intel.compile.transforms import transform_observation

        exp, out, el = self.exp, self.out, self.el
        at = exp.at(el)
        params, variables = [], []
        for v in children(el, "MAPPINGVARIABLE"):
            vname = attr(v, "NAME") or ""
            if not re.fullmatch(r"\$\$[A-Za-z_]\w*", vname):
                continue
            if (attr(v, "ISPARAM") or "").upper() == "YES":
                params.append((v, vname))
            else:
                variables.append(vname)
                out.construct(exp.at(v), "infa:mapping_variable", True)
        out.construct(at, "infa:mapping", True)
        decl = out.entity(at, "infa:Mapping", self.program["natural_key"], self.name, {
            "kind": "mapping", "folder": exp.fname, "mapping": attr(el, "NAME"),
            "valid": False if (attr(el, "ISVALID") or "").upper() == "NO" else None,
            "parameters": sorted(n for _, n in params), "variables": sorted(variables),
        })
        self.decl = decl
        for v, vname in params:
            vat = exp.at(v)
            out.construct(vat, "infa:mapping_parameter", True)
            key = f"parameter:{vname}"
            ref = out.entity(vat, "infa:Parameter", {"name": key}, key, {"kind": "mapping_parameter", "data_type": attr(v, "DATATYPE")})
            out.relation(vat, decl, P.REFERENCES_CONFIG, ref, {"via": "mapping_parameter"})

        instances = [i for i in children(el, "INSTANCE") if ok_name(attr(i, "NAME"))]
        self.instances = {attr(i, "NAME"): i for i in instances}
        self.fields: dict[tuple[str, str], tuple[str, str]] = {}  # (to instance, to port) -> (from instance, from port)
        deps: dict[str, list[str]] = {attr(i, "NAME"): [] for i in instances}
        for c in children(el, "CONNECTOR"):
            a, b_ = attr(c, "FROMINSTANCE"), attr(c, "TOINSTANCE")
            if a not in self.instances or b_ not in self.instances:
                continue
            out.construct(exp.at(c), "infa:connector", True)
            self.fields.setdefault((b_, attr(c, "TOFIELD") or ""), (a, attr(c, "FROMFIELD") or ""))
            if a not in deps[b_]:
                deps[b_].append(a)
        self.deps = deps
        order: list[str] = []
        done: set[str] = set()
        names = [attr(i, "NAME") for i in instances]
        while len(order) < len(names):
            ready = next((n for n in names if n not in done and all(d in done for d in deps[n])), None)
            if ready is None:  # a cycle: the rest in document order
                ready = next(n for n in names if n not in done)
            done.add(ready)
            order.append(ready)
        self.results: dict[Any, str | None] = {}
        for n in order:
            self.instance(self.instances[n])
        transforms = self.b.finish()
        if transforms:
            out.add(transform_observation(out.blob, at, decl, PLATFORM, transforms, evidence_class=EvidenceClass.E1))
        if self.lowered and self.flow:
            body = [{"op": "loop", "kind": "implicit", "at": exp.at_list(el), "body": self.flow}]
            out.add(behavior_observation(out.blob, at, decl, PLATFORM, body, evidence_class=EvidenceClass.E1, kind="flow"))

    # -- helpers

    def definition_of(self, inst):
        """The TRANSFORMATION element an instance uses: the mapping's own, else the folder's reusable one."""
        tname = attr(inst, "TRANSFORMATION_NAME") or attr(inst, "NAME")
        el = self.local.get(tname)
        if el is None and tname in self.exp.reusable:
            el = self.exp.reusable[tname]
            self.out.construct(self.exp.at(inst), "infa:reusable_transformation", True)
        return el

    def upstream(self, inst_name: str, ports: set[str] | None = None) -> list[str]:
        """Results feeding an instance, in connector order (only from `ports` when given).

        Ports taken from a transformation and from one further down the same pipeline (a
        connected lookup returns its rows in line with its input) are one stream: only the
        furthest result is kept."""
        refs: list[str] = []
        for (to, port), (frm, fport) in self.fields.items():
            if to != inst_name or (ports is not None and port not in ports):
                continue
            r = self.result_of(frm, fport)
            if r is not None and r not in refs:
                refs.append(r)
        return [r for r in refs if not any(r != other and r in self.ancestors(other) for other in refs)]

    def ancestors(self, ref: str) -> set[str]:
        seen: set[str] = set()
        todo = [ref]
        while todo:
            item = self.b.get(todo.pop())
            for i in item["inputs"] if item else []:
                if i not in seen:
                    seen.add(i)
                    todo.append(i)
        return seen

    def result_of(self, frm: str, fport: str) -> str | None:
        if (frm, "#groups") in self.results:
            group = self.results.get((frm, "#port", fport))
            return self.results.get((frm, group)) if group else None
        return self.results.get(frm)

    def source_port(self, inst_name: str, port: str) -> str:
        """The upstream column a port of an instance is connected to, lower case."""
        hit = self.fields.get((inst_name, port))
        return (hit[1] if hit else port).lower()

    def platform(self, construct: str, al: list[int], name: str) -> None:
        self.flow.append({"op": "platform", "construct": construct, "at": al, "detail": {"instance": name}})

    def expression(self, text: str | None, at) -> dict[str, Any] | None:
        if not text or not text.strip():
            return None
        e = etlexpr.parse(text, "informatica")
        if e is None:
            self.out.construct(at, "infa:expression_unread", False)
        return e

    def sql(self, sql: str, al: list[int], dialect: str | None) -> str:
        from repo_intel.compile.transforms import sql_query

        before = len(self.b.items)
        ref = sql_query(self.b, bind_parameters(sql), al, dialect=dialect)
        if ref is not None:
            return ref
        if len(self.b.items) > before:
            return self.b.items[-1]["id"]
        return self.b.add("platform", al, [], construct="infa:transformation.sql_override")

    def relate(self, at, predicate, obj, extra: dict[str, Any] | None = None) -> None:
        self.out.relation(at, self.decl, predicate, obj, extra)

    # -- instances

    def instance(self, inst) -> None:
        out, b, exp = self.out, self.b, self.exp
        name = attr(inst, "NAME")
        at, al = exp.at(inst), exp.at_list(inst)
        kind = (attr(inst, "TYPE") or "").upper()
        tname = attr(inst, "TRANSFORMATION_NAME") or name
        if kind in ("SOURCE", "TARGET"):
            d = (exp.sources if kind == "SOURCE" else exp.targets).get(tname)
            if d is None:
                out.construct(at, "infa:instance_unresolved", False)
                out.note("info", f"mapping {self.name} uses {kind.lower()} {tname}, whose definition is not in this export: not linked")
                out.unresolved += 1
                self.platform("infa:instance_unresolved", al, name)
                if kind == "TARGET":
                    ups = self.upstream(name)
                    b.add("platform", al, ups[:1], construct="infa:instance_unresolved")
                return
            if kind == "SOURCE":
                out.construct(at, "infa:source_instance", True)
                self.relate(at, P.READS_FROM, d.ref)
                out.resolved += 1
                self.results[name] = d.name
                self.platform("infa:source_instance", al, name)
                return
            out.construct(at, "infa:target_instance", True)
            self.relate(at, P.WRITES_TO, d.ref)
            out.resolved += 1
            self.platform("infa:target_instance", al, name)
            ups = self.upstream(name)
            if ups:
                b.write(ups[0], d.name, al)
            else:
                b.add("publish", al, [], output=d.name)
            return
        if kind == "MAPPLET":
            out.construct(at, "infa:mapplet_instance", False)
            self.platform("infa:mapplet_instance", al, name)
            self.results[name] = b.add("platform", al, self.upstream(name), construct="infa:mapplet_instance")
            return
        el = self.definition_of(inst)
        role, cid = transformation(attr(inst, "TRANSFORMATION_TYPE") or (attr(el, "TYPE") if el is not None else None))
        if el is None:
            out.construct(at, "infa:instance_unresolved", False)
            out.note("info", f"mapping {self.name} uses transformation {tname}, whose definition is not in this export: not read")
            self.platform("infa:instance_unresolved", al, name)
            self.results[name] = b.add("platform", al, self.upstream(name), construct="infa:instance_unresolved")
            return
        tat, tal = exp.at(el), exp.at_list(el)
        handler = getattr(self, f"t_{role}", None)
        if handler is None:
            out.construct(tat, cid, False)
            self.platform(cid, tal, name)
            self.results[name] = b.add("platform", tal, self.upstream(name), construct=cid)
            return
        handler(inst, el, name, tat, tal, cid)

    # -- transformations

    def ports(self, el) -> list[Any]:
        return [p for p in children(el, "TRANSFORMFIELD") if ok_name(attr(p, "NAME"))]

    def t_source_qualifier(self, inst, el, name, at, al, cid) -> None:
        out, b, exp = self.out, self.b, self.exp
        attrs = table_attrs(el)
        sources = [n for n in self.deps.get(name, []) if (attr(self.instances[n], "TYPE") or "").upper() == "SOURCE"]
        defs = [exp.sources.get(attr(self.instances[n], "TRANSFORMATION_NAME") or n) for n in sources]
        defs = [d for d in defs if d is not None]
        dialect = next((d.dialect for d in defs if d.dialect), None)
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        for key in ("Pre SQL", "Post SQL"):
            if attrs.get(key, "").strip():
                out.construct(at, "infa:transformation.pre_post_sql", True)
                exp.lineage(at, attrs[key], [self.decl], dialect, "pre_post_sql")
        query = attrs.get("Sql Query", "").strip()
        if query:
            out.construct(at, "infa:transformation.sql_override", True)
            self.results[name] = self.sql(query, al, dialect)
            exp.lineage(at, query, [self.decl], dialect, "sql_override")
            return
        reads = [b.add("read", al, [d.name]) for d in defs]
        if not reads:
            self.results[name] = b.add("platform", al, [], construct=cid)
            return
        ref = reads[0]
        join = attrs.get("User Defined Join", "").strip()
        for r in reads[1:]:
            pairs = etlexpr.equi_pairs(join, "informatica") if join else None
            fields: dict[str, Any] = {"join": {"type": "inner", **({"on": pairs} if pairs else {})}}
            if pairs:
                fields.update(keys=[p[0] for p in pairs], primitive="join")
            else:
                fields["candidates"] = ["join"]
            ref = b.add("join", al, [ref, r], **fields)
        filt = attrs.get("Source Filter", "").strip()
        if filt:
            ref = b.add("filter", al, [ref], predicate={"sql": bind_parameters(filt)})
        sorted_n = attrs.get("Number Of Sorted Ports", "0").strip()
        if sorted_n.isdigit() and int(sorted_n) > 0:
            cols = [attr(p, "NAME").lower() for p in self.ports(el)][: int(sorted_n)]
            ref = b.add("sort", al, [ref], order=[{"column": c, "desc": False} for c in cols])
        if attrs.get("Select Distinct", "").strip().upper() == "YES":
            ref = b.add("deduplicate", al, [ref], candidates=["dedupe_by_hash"])
        self.results[name] = ref

    def t_expression(self, inst, el, name, at, al, cid) -> None:
        from repo_intel.compile.transforms import emit_projection

        out = self.out
        ups = self.upstream(name)
        out.construct(at, cid, True)
        if not ups:
            self.platform(cid, al, name)
            self.results[name] = None
            return
        items = []
        for p in self.ports(el):
            ptype = (attr(p, "PORTTYPE") or "").upper()
            pname = attr(p, "NAME")
            text = attr(p, "EXPRESSION") or ""
            pat = self.exp.at_list(p)
            variable = "VARIABLE" in ptype
            if not (variable or ("OUTPUT" in ptype and "INPUT" not in ptype)):
                continue
            if not text.strip() or text.strip().lower() == pname.lower():
                continue
            e = self.expression(text, self.exp.at(p))
            value = e if e is not None else etlexpr.unread(text, "infa:expression_unread")
            self.flow.append({"op": "assign", "at": pat, "target": {"e": "var", "scope": "local" if variable else "column", "name": pname}, "value": value})
            self.lowered = True
            if not variable:
                items.append(etlexpr.classify(e, pname.lower()))
        self.results[name] = emit_projection(self.b, al, ups[0], items, keep_only=False) if items else ups[0]

    def t_filter(self, inst, el, name, at, al, cid) -> None:
        out = self.out
        ups = self.upstream(name)
        out.construct(at, cid, True)
        text = table_attrs(el).get("Filter Condition", "").strip()
        if not ups:
            self.results[name] = None
            return
        if not text or text.upper() == "TRUE":
            self.results[name] = ups[0]
            return
        e = self.expression(text, at)
        self.results[name] = self.b.add("filter", al, [ups[0]], predicate=e if e is not None else {"sql": text})
        self.flow.append({"op": "if", "at": al, "cond": {"e": "unop", "op": "not", "operand": e if e is not None else etlexpr.unread(text, "infa:expression_unread")}, "then": [{"op": "continue", "at": al}]})
        self.lowered = True

    def t_router(self, inst, el, name, at, al, cid) -> None:
        out, b = self.out, self.b
        ups = self.upstream(name)
        out.construct(at, cid, True)
        groups = [g for g in children(el, "GROUP") if attr(g, "NAME")]
        outputs = [g for g in groups if (attr(g, "TYPE") or "").upper() == "OUTPUT"]
        default = next((g for g in groups if "DEFAULT" in (attr(g, "TYPE") or "").upper()), None)
        self.results[(name, "#groups")] = None
        for p in self.ports(el):
            if attr(p, "GROUP"):
                self.results[(name, "#port", attr(p, "NAME"))] = attr(p, "GROUP")
        if not ups:
            return
        conds = [(g, self.expression(attr(g, "EXPRESSION"), self.exp.at(g)), attr(g, "EXPRESSION") or "") for g in outputs]
        exact = bool(conds) and default is not None and all(e is not None for _, e, _ in conds)
        for g, e, text in conds:
            gal = self.exp.at_list(g)
            fields: dict[str, Any] = {"predicate": e if e is not None else {"sql": text}}
            fields.update(primitive="split_by_condition") if exact else fields.update(candidates=["split_by_condition"])
            self.results[(name, attr(g, "NAME"))] = b.add("filter", gal, [ups[0]], **fields)
            cond = e if e is not None else etlexpr.unread(text, "infa:expression_unread")
            self.flow.append({"op": "if", "at": gal, "cond": cond, "then": [{"op": "output", "at": gal, "channel": "dataset", "value": {"e": "lit", "value": attr(g, "NAME"), "type": "string"}}]})
        if default is not None:
            dal = self.exp.at_list(default)
            fields = {}
            if exact:
                anyof = conds[0][1]
                for _, e, _ in conds[1:]:
                    anyof = {"e": "binop", "op": "or", "left": anyof, "right": e}
                fields = {"predicate": {"e": "unop", "op": "not", "operand": anyof}, "primitive": "split_by_condition"}
                self.flow.append({"op": "if", "at": dal, "cond": fields["predicate"], "then": [{"op": "output", "at": dal, "channel": "dataset", "value": {"e": "lit", "value": attr(default, "NAME"), "type": "string"}}]})
            else:
                fields = {"candidates": ["split_by_condition"]}
            self.results[(name, attr(default, "NAME"))] = b.add("filter", dal, [ups[0]], **fields)
        self.lowered = True

    def t_joiner(self, inst, el, name, at, al, cid) -> None:
        out, b = self.out, self.b
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        ports = self.ports(el)
        master_ports = {attr(p, "NAME") for p in ports if "MASTER" in (attr(p, "PORTTYPE") or "").upper()}
        detail_ports = {attr(p, "NAME") for p in ports if attr(p, "NAME") not in master_ports}
        master = self.upstream(name, master_ports)
        detail = self.upstream(name, detail_ports)
        if not master or not detail:
            self.results[name] = b.add("platform", al, master + detail, construct=cid)
            return
        attrs = table_attrs(el)
        jtype = {"normal join": "inner", "master outer join": "left", "detail outer join": "right", "full outer join": "full"}.get(attrs.get("Join Type", "Normal Join").strip().lower(), "inner")
        pairs = etlexpr.equi_pairs(attrs.get("Join Condition", ""), "informatica")
        on: list[list[str]] = []
        lower_master = {p.lower(): p for p in master_ports}
        lower_detail = {p.lower(): p for p in detail_ports}
        for x, y in pairs or []:
            if x in lower_master and y in lower_detail:
                x, y = y, x
            if x in lower_detail and y in lower_master:
                on.append([self.source_port(name, lower_detail[x]), self.source_port(name, lower_master[y])])
        fields: dict[str, Any] = {"join": {"type": jtype, **({"on": on} if on else {})}}
        if on and len(on) == len(pairs or []):
            fields.update(keys=[p[0] for p in on], primitive="join")
        else:
            fields["candidates"] = ["join"]
        self.results[name] = b.add("join", al, [detail[0], master[0]], **fields)

    def t_lookup(self, inst, el, name, at, al, cid) -> None:
        out, b, exp = self.out, self.b, self.exp
        attrs = table_attrs(el)
        ups = self.upstream(name)
        raw = attrs.get("Lookup table name", "").strip()
        query = attrs.get("Lookup Sql Override", "").strip()
        exp.connection(at, self.decl, attrs.get("Connection Information"), {"via": "lookup", "instance": name})
        right: str | None = None
        before = len(b.items)
        if query:
            out.construct(at, "infa:transformation.sql_override", True)
            exp.lineage(at, query, [self.decl], None, "lookup")
            right = self.sql(query, al, None)
        else:
            tname = table_name(raw)
            if tname:
                t = out.neutral(at, K.TABLE, {"datastore": "default", "name": tname}, tname)
                self.relate(at, P.READS_FROM, t, {"via": "lookup"})
                out.resolved += 1
                right = b.add("read", al, [tname])
            elif raw:
                out.note("info", f"lookup {name} of mapping {self.name} reads a table named by {raw}: not resolved")
                out.unresolved += 1
                right = b.add("read", al, ["{" + re.sub(r"[^\w.]", "", raw.replace("$$", "")).lower() + "}"])
        if not ups:
            # Called from expressions (:LKP): the table is read, but there is no row stream to join.
            del b.items[before:]
            out.construct(at, "infa:transformation.lookup_unconnected", True)
            self.platform("infa:transformation.lookup_unconnected", al, name)
            self.results[name] = None
            return
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        ports = self.ports(el)
        inputs = {attr(p, "NAME").lower(): attr(p, "NAME") for p in ports if "INPUT" in (attr(p, "PORTTYPE") or "").upper() and "LOOKUP" not in (attr(p, "PORTTYPE") or "").upper()}
        lookups = {attr(p, "NAME").lower() for p in ports if "LOOKUP" in (attr(p, "PORTTYPE") or "").upper()}
        used = {fport for (_, _), (frm, fport) in self.fields.items() if frm == name}
        added = sorted(attr(p, "NAME").lower() for p in ports if "LOOKUP" in (attr(p, "PORTTYPE") or "").upper() and "OUTPUT" in (attr(p, "PORTTYPE") or "").upper() and attr(p, "NAME") in used)
        pairs = etlexpr.equi_pairs(attrs.get("Lookup condition", ""), "informatica")
        on: list[list[str]] = []
        for x, y in pairs or []:
            if x in inputs and y in lookups:
                x, y = y, x
            if x in lookups and y in inputs:
                on.append([self.source_port(name, inputs[y]), x])
        fields: dict[str, Any] = {"join": {"type": "left", **({"on": on} if on else {})}}
        if on and len(on) == len(pairs or []):
            fields.update(keys=[p[0] for p in on], primitive="join")
        else:
            fields["candidates"] = ["join"]
        if added:
            fields["columns"] = {"add": added}
        self.results[name] = b.add("join", al, [ups[0], *([right] if right else [])], **fields)

    def t_aggregator(self, inst, el, name, at, al, cid) -> None:
        out = self.out
        ups = self.upstream(name)
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        if not ups:
            self.results[name] = None
            return
        keys, measures, exact = [], [], True
        for p in self.ports(el):
            ptype = (attr(p, "PORTTYPE") or "").upper()
            if (attr(p, "EXPRESSIONTYPE") or "").upper() == "GROUPBY":
                keys.append(self.source_port(name, attr(p, "NAME")))
                continue
            if "OUTPUT" in ptype and "INPUT" not in ptype:
                m, ok = etlexpr.measure(self.expression(attr(p, "EXPRESSION"), self.exp.at(p)), attr(p, "NAME").lower())
                if m is None:
                    exact = False
                    continue
                measures.append(m)
                exact = exact and ok
        fields: dict[str, Any] = {"keys": keys, "measures": measures}
        if exact and measures:
            fields["primitive"] = "aggregate"
        else:
            fields["candidates"] = ["aggregate"]
        self.results[name] = self.b.add("aggregate", al, [ups[0]], **fields)

    def t_sorter(self, inst, el, name, at, al, cid) -> None:
        out = self.out
        ups = self.upstream(name)
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        if not ups:
            self.results[name] = None
            return
        order = [{"column": attr(p, "NAME").lower(), "desc": (attr(p, "SORTDIRECTION") or "").upper() == "DESCENDING"} for p in self.ports(el) if (attr(p, "ISSORTKEY") or "").upper() == "YES"]
        ref = self.b.add("sort", al, [ups[0]], order=order)
        if table_attrs(el).get("Distinct", "").strip().upper() == "YES":
            ref = self.b.add("deduplicate", al, [ref], candidates=["dedupe_by_hash"])
        self.results[name] = ref

    def t_union(self, inst, el, name, at, al, cid) -> None:
        self.out.construct(at, cid, True)
        self.platform(cid, al, name)
        ups = self.upstream(name)
        self.results[name] = self.b.add("union", al, ups) if len(ups) > 1 else (ups[0] if ups else None)

    def t_rank(self, inst, el, name, at, al, cid) -> None:
        self.out.construct(at, cid, True)
        self.platform(cid, al, name)
        ups = self.upstream(name)
        keys = [self.source_port(name, attr(p, "NAME")) for p in self.ports(el) if (attr(p, "EXPRESSIONTYPE") or "").upper() == "GROUPBY"]
        self.results[name] = self.b.add("window", al, ups[:1], keys=keys, candidates=["top_n_filter"]) if ups else None

    def t_update_strategy(self, inst, el, name, at, al, cid) -> None:
        self.out.construct(at, cid, True)
        ups = self.upstream(name)
        text = table_attrs(el).get("Update Strategy Expression", "").strip().upper()
        self.flow.append({"op": "platform", "construct": cid, "at": al, "detail": {"instance": name}})
        if not ups:
            self.results[name] = None
            return
        if text in ("", "DD_INSERT", "0"):
            self.results[name] = ups[0]
            return
        candidates = []
        if "DD_UPDATE" in text or re.search(r"\b1\b", text):
            candidates.append("merge_into")
        if "DD_DELETE" in text or re.search(r"\b2\b", text):
            candidates.append("apply_deletes")
        self.results[name] = self.b.add("platform", al, [ups[0]], construct=cid, candidates=candidates or None)

    def t_sequence(self, inst, el, name, at, al, cid) -> None:
        self.out.construct(at, cid, False)
        self.results[name] = None

    def t_pass(self, inst, el, name, at, al, cid) -> None:
        self.out.construct(at, cid, False)
        ups = self.upstream(name)
        self.results[name] = ups[0] if ups else None


class Flow:
    """A workflow or worklet: task instances, links, sessions and their connections."""

    def __init__(self, exp: Export, el, kind: str):
        self.exp, self.out, self.el, self.kind = exp, exp.out, el, kind
        self.name = f"{exp.fname}/{attr(el, 'NAME')}"
        self.job = {"kind": "Job", "natural_key": {"framework": FRAMEWORK, "name": self.name}, "name": attr(el, "NAME")}

    def task_ref(self, instance: str) -> dict[str, Any]:
        return {"kind": "Task", "natural_key": {"framework": FRAMEWORK, "job": self.name, "name": instance}, "name": instance}

    def run(self) -> None:
        exp, out, el = self.exp, self.out, self.el
        at = exp.at(el)
        out.construct(at, f"infa:{self.kind}", True)
        local_sessions = {attr(s, "NAME"): s for s in children(el, "SESSION") if attr(s, "NAME")}
        local_tasks = {attr(t, "NAME"): t for t in children(el, "TASK") if attr(t, "NAME")}
        starts: set[str] = set()
        declared: dict[str, dict[str, Any]] = {}
        for ti in children(el, "TASKINSTANCE"):
            iname = attr(ti, "NAME")
            if not ok_name(iname):
                continue
            tat = exp.at(ti)
            ttype, cid = task_type(attr(ti, "TASKTYPE"))
            tname = attr(ti, "TASKNAME") or iname
            if ttype == "start":
                out.construct(tat, cid, True)
                starts.add(iname)
                continue
            modelled = ttype in ("session", "decision", "worklet")
            out.construct(tat, cid, modelled)
            attrs: dict[str, Any] = {"type": ttype, "task_name": tname if tname != iname else None,
                                     "reusable": True if (attr(ti, "REUSABLE") or "").upper() == "YES" else None,
                                     "disabled": True if (attr(ti, "ISENABLED") or "").upper() == "NO" else None}
            session = None
            if ttype == "session":
                session = local_sessions.get(tname) or exp.sessions.get(tname)
                mapping = attr(session, "MAPPINGNAME") if session is not None else None
                attrs["mapping"] = f"{exp.fname}/{mapping}" if ok_name(mapping) else None
                pfile = table_attrs(session).get("Parameter Filename", "") if session is not None else ""
                attrs["parameter_file"] = self.parameter_file(tat, pfile)
            ref = out.entity(tat, "infa:Session" if ttype == "session" else "infa:Task", self.task_ref(iname)["natural_key"], iname, attrs)
            declared[iname] = ref
            out.relation(tat, self.job, P.CONTAINS, ref)
            if ttype == "session":
                self.session(tat, ref, iname, session)
            elif ttype == "worklet":
                wl = {"kind": "Job", "natural_key": {"framework": FRAMEWORK, "name": f"{exp.fname}/{tname}"}, "name": tname}
                out.relation(tat, ref, P.RUNS, wl if tname in exp.worklets else {**wl, "declare": False}, {"via": "worklet"})
        entry: list[str] = []
        for link in children(el, "WORKFLOWLINK"):
            a, b_ = attr(link, "FROMTASK"), attr(link, "TOTASK")
            lat = exp.at(link)
            if a in starts and b_ in declared:
                out.construct(lat, "infa:workflow_link", True)
                entry.append(b_)
                continue
            if a not in declared or b_ not in declared:
                out.construct(lat, "infa:workflow_link", False)
                continue
            out.construct(lat, "infa:workflow_link", True)
            cond = (attr(link, "CONDITION") or "").strip()
            m = _STATUS.match(cond)
            on = {"succeeded": "success", "failed": "failure", "stopped": "stopped", "aborted": "aborted"}[m.group(2).lower()] if m else ("expression" if cond else "completion")
            out.relation(lat, declared[b_], P.DEPENDS_ON, declared[a], {"via": "link", "on": on})
        variables = []
        for v in children(el, "WORKFLOWVARIABLE"):
            vname = attr(v, "NAME") or ""
            if (attr(v, "USERDEFINED") or "").upper() == "YES" and re.fullmatch(r"\$\$[A-Za-z_]\w*", vname):
                out.construct(exp.at(v), "infa:workflow_variable", True)
                variables.append(vname)
        schedule = None
        sched = child(el, "SCHEDULER")
        if sched is not None:
            info = child(sched, "SCHEDULEINFO")
            stype = (attr(info, "SCHEDULETYPE") or "").strip().lower()
            out.construct(exp.at(sched), "infa:scheduler", bool(stype))
            schedule = {"ondemand": "on_demand", "onserverinit": "on_server_init", "continuous": "continuous", "customized": "custom", "runonce": "run_once"}.get(stype, stype or None)
        pfile = self.parameter_file(at, table_attrs(el).get("Parameter Filename", ""))
        out.entity(at, "infa:Workflow" if self.kind == "workflow" else "infa:Worklet", self.job["natural_key"], self.name, {
            "kind": self.kind, "folder": exp.fname, "entry": sorted(set(entry)), "schedule": schedule,
            "parameter_file": pfile, "variables": sorted(variables),
            "valid": False if (attr(el, "ISVALID") or "").upper() == "NO" else None,
        })

    def parameter_file(self, at, value: str) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        base = posixpath.basename(value.replace("\\", "/"))
        self.out.construct(at, "infa:parameter_file_name", bool(base) and "$" not in base)
        return base if base and "$" not in base and ok_name(base) else None

    def session(self, at, ref, iname: str, session) -> None:
        exp, out = self.exp, self.out
        if session is None:
            out.note("info", f"session {iname} of {self.kind} {self.name} is not defined in this export: its mapping is not linked")
            return
        mapping = attr(session, "MAPPINGNAME")
        if ok_name(mapping):
            program = {"kind": "Program", "natural_key": {"platform": PLATFORM, "name": f"{exp.fname}/{mapping}"}, "name": mapping}
            out.relation(at, ref, P.RUNS, program if mapping in exp.mappings else {**program, "declare": False}, {"via": "session"})
        for ext in children(session, "SESSIONEXTENSION"):
            for cref in children(ext, "CONNECTIONREFERENCE"):
                via = {"via": (attr(ext, "TYPE") or "").lower() or None, "instance": attr(ext, "SINSTANCENAME")}
                name = (attr(cref, "CONNECTIONNAME") or "").strip()
                variable = (attr(cref, "VARIABLE") or "").strip()
                exp.connection(exp.at(cref), ref, name or variable, via)
        for sti in children(session, "SESSTRANSFORMATIONINST"):
            attrs = table_attrs(sti)
            for key in _OVERRIDES:
                if attrs.get(key, "").strip():
                    out.construct(exp.at(sti), "infa:session_override", False)
                    out.note("info", f"session {iname} overrides {key} of instance {attr(sti, 'SINSTANCENAME')}: the mapping's own value is what the transforms show")
                    if key in ("Sql Query", "Lookup Sql Override", "Pre SQL", "Post SQL"):
                        exp.lineage(exp.at(sti), attrs[key], [ref], None, "session_override")
