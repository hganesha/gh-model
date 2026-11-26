"""What a Talend job item declares: the job, its components, connections, data, shapes and behaviour.

A job is stored as two files in the project's `process/` folder (`joblets/` for a joblet):
`<label>_<version>.item`, the job's design (`talendfile:ProcessType`), and
`<label>_<version>.properties`, its repository properties. The `.item` holds:

    <talendfile:ProcessType jobType="Standard" ...>
      <context name="Default"><contextParameter name="in_dir" type="id_String" value="..."/></context>
      <node componentName="tFileInputDelimited" ...>
        <elementParameter field="TEXT" name="UNIQUE_NAME" value="tFileInputDelimited_1"/>
        <elementParameter field="FILE" name="FILENAME" value="&quot;/data/in/customers.csv&quot;"/>
        <metadata connector="FLOW" name="tFileInputDelimited_1"><column name="CUST_ID" .../></metadata>
      </node>
      <node componentName="tMap" ...><nodeData xsi:type="MapperData">...</nodeData></node>
      <connection connectorName="FLOW" label="row1" source="tFileInputDelimited_1" target="tMap_1" .../>
    </talendfile:ProcessType>

Read at evidence `E1`:

- the job is a `Job` (`tal:StandardJob`, `tal:BigDataJob` or `tal:Joblet`), framework
  `talend`, named by the item's label: the file name without its version. Its version and
  folder, and its context variables by name only (never their values), are attributes;
- each component (`node`) is a `Task` (`tal:Component`) named by its unique name, which the
  job CONTAINS; a connection from A to B makes B DEPENDS_ON A (`via: flow` for rows,
  `via: trigger` for subjob and component triggers, Run if and iterate), with the connector
  and the connection's label;
- file components read and write a `Dataset` named by the file setting, database
  components a `Table` named by their table setting (with its schema) or by the tables
  their query reads and writes (the shared scanner `sql_lineage.lineage`, never run). A
  name made from context variables or code is not resolved: the transform keeps
  `{context.x}`, is marked dynamic, and a diagnostic says so;
- one `transform` observation per job (language `talend`), owned by the job: each
  component's operation in connection order. tMap lookups are joins on their key
  expressions, its filters filters, its output expressions projections;
- one `behavior` unit (kind `flow`, language `talend`) per tMap, owned by its Task: the
  implicit loop over the main input, the variables, and per output its filter and the
  expression of each column (`expr`); and, for a job with tRunJob components, one owned by
  the job: its components in trigger order, a tRunJob as a call of the job it runs,
  guarded by Run if conditions;
- a tRunJob RUNS the Job it names (`declare=False`: linked when the repository holds it).

Connection settings (host, port, user, password), commands, mail addresses, Java code and
context values are never read.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.datastage import xmlspan
from repo_intel.platforms.talend import javaexpr as jx
from repo_intel.platforms.talend.vocabulary import DB_TYPES, VENDOR_OF, component_kind

P = Predicate
E1 = EvidenceClass.E1
FRAMEWORK = "talend"
LANGUAGE = "talend"
_VERSION = re.compile(r"^(?P<label>.+?)_(?P<version>\d+(?:\.\d+)+)$")
# Connectors that carry rows; any other connector is a trigger.
_TRIGGERS = {"SUBJOB_OK", "SUBJOB_ERROR", "COMPONENT_OK", "COMPONENT_ERROR", "RUN_IF", "ITERATE", "ON_SUBJOB_OK", "ON_SUBJOB_ERROR",
             "ON_COMPONENT_OK", "ON_COMPONENT_ERROR", "SYNCHRONIZE", "PARALLELIZE", "RUN_AFTER", "RUN_BEFORE"}
_LOOKUP_STYLE = "8"  # a connection's lineStyle for a lookup (reference) row
_TRIM = {"trim", "stringhandling.trim", "stringhandling.btrim", "stringhandling.ltrim", "stringhandling.rtrim", "strip"}
_CASE = {"touppercase", "tolowercase", "stringhandling.upcase", "stringhandling.downcase"}
_DATES = {"talenddate.parsedate", "talenddate.parsedatelocale", "talenddate.parsedateinutc"}
_CASTS = {"integer.parseint", "integer.valueof", "long.parselong", "long.valueof", "double.parsedouble", "double.valueof", "float.parsefloat",
          "string.valueof", "boolean.parseboolean", "talenddate.formatdate", "(integer)", "(long)", "(double)", "(string)", "(float)",
          "(java.math.bigdecimal)", "(bigdecimal)", "tostring"}
_ISNULL = {"relational.isnull"}
_NAME = re.compile(r"[A-Za-z_][\w.$-]{0,254}")


def job_identity(path: str) -> tuple[str, str | None]:
    """The job's label and version from its file name: `process/a/LoadCustomers_0.1.item`."""
    stem = posixpath.basename(path)
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    m = _VERSION.match(stem)
    return (m.group("label"), m.group("version")) if m else (stem, None)


def folder(path: str) -> str | None:
    """The repository folder of an item below its top folder (`process/Customers/x.item` -> `Customers`)."""
    parts = path.replace("\\", "/").split("/")
    for top in ("process", "joblets", "process_mr", "process_storm", "context", "code", "metadata"):
        if top in parts:
            rest = parts[parts.index(top) + 1:-1]
            if top == "code" and rest:
                rest = rest[1:]
            return "/".join(rest) or None
    return None


def params(node: xmlspan.Node) -> dict[str, xmlspan.Node]:
    """A node's `elementParameter`s by name (first occurrence)."""
    out: dict[str, xmlspan.Node] = {}
    for p in node.find("elementParameter"):
        name = p.get("name")
        if name and name not in out:
            out[name] = p
    return out


def table_rows(param: xmlspan.Node | None) -> list[dict[str, str]]:
    """A TABLE setting's rows: each run of `elementValue`s from one first column to the next."""
    if param is None:
        return []
    rows: list[dict[str, str]] = []
    first: str | None = None
    for ev in param.find("elementValue"):
        ref = ev.get("elementRef") or ""
        if first is None:
            first = ref
        if ref == first or not rows:
            rows.append({})
        rows[-1].setdefault(ref, ev.get("value") or "")
    return rows


def plain(value: str | None) -> str:
    """A setting's value with Java string quotes removed when it is one literal."""
    if value is None:
        return ""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"' and re.fullmatch(r'"(?:[^"\\]|\\.)*"', v):
        return jx.unquote(v)
    return v


@dataclass
class _Comp:
    node: xmlspan.Node
    name: str
    component: str
    canonical: str | None
    family: str
    dialect: str | None
    vendor: str | None
    params: dict[str, xmlspan.Node]
    active: bool = True
    target: str | None = None  # the job a tRunJob runs
    ref: dict[str, Any] | None = None
    inputs: list["_Conn"] = field(default_factory=list)
    outputs: list["_Conn"] = field(default_factory=list)

    def value(self, name: str) -> str | None:
        p = self.params.get(name)
        return p.get("value") if p is not None else None

    def flag(self, name: str) -> bool:
        return (self.value(name) or "").strip().lower() == "true"


@dataclass
class _Conn:
    node: xmlspan.Node
    connector: str
    label: str
    src: str
    dst: str
    style: str
    order: int

    @property
    def flow(self) -> bool:
        return self.connector.upper() not in _TRIGGERS


class ItemReader:
    """Observations for one job item: `span(node)` gives a node's file span."""

    def __init__(self, path: str, blob: str, base, span, whole, extractor: str):
        self.path, self.blob, self.base, self.span, self.whole, self.extractor = path, blob, base, span, whole, extractor
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0

    # -- emission

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.extractor, severity, message))

    def at(self, node: xmlspan.Node | None) -> Any:
        return self.span(node) if node is not None else self.whole

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}, E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    # -- the job

    def process(self, root: xmlspan.Node) -> None:
        label, version = job_identity(self.path)
        if not _NAME.fullmatch(label):
            self.note("warning", "a job item whose file name is not a job label: not read")
            self.construct(self.whole, "tal:job", False)
            return
        parts = self.path.replace("\\", "/").split("/")
        job_type = (root.get("jobType") or "Standard").strip()
        term = "tal:Joblet" if "joblets" in parts else ("tal:BigDataJob" if "big data" in job_type.lower() else "tal:StandardJob")
        contexts = root.find("context")
        names: set[str] = set()
        for ctx in contexts:
            for cp in ctx.find("contextParameter"):
                n = (cp.get("name") or "").strip()
                if re.fullmatch(r"[A-Za-z_]\w{0,127}", n):
                    if n not in names:
                        self.construct(self.at(cp), "tal:context_parameter", True)
                    names.add(n)
        key = {"framework": FRAMEWORK, "name": label}
        attrs = {"version": version, "job_type": job_type.lower().replace(" ", "_"), "folder": folder(self.path),
                 "context_parameters": sorted(names), "contexts": sorted({c.get("name") for c in contexts if c.get("name")})}
        job_ref = self.entity(self.whole, term, key, label, attrs)
        self.construct(self.whole, "tal:job", True)
        for n in root.find("note"):
            self.construct(self.at(n), "tal:note", False)

        comps: dict[str, _Comp] = {}
        seen: dict[str, int] = {}
        for node in root.find("node"):
            component = (node.get("componentName") or "").strip()
            ps = params(node)
            unique = plain(ps["UNIQUE_NAME"].get("value")) if "UNIQUE_NAME" in ps else ""
            if not unique or not component:
                self.construct(self.at(node), "tal:component.unknown", False)
                continue
            n = seen[unique] = seen.get(unique, 0) + 1
            name = unique if n == 1 else f"{unique}~{n}"
            known = component_kind(component)
            canonical, family, dialect = known if known else (None, "unknown", None)
            vendor = VENDOR_OF.get(component.lower())
            c = _Comp(node, name, component, canonical, family, dialect, vendor, ps)
            if c.canonical and c.canonical.startswith("tDB") and c.value("TYPE"):
                dialect_vendor = DB_TYPES.get(plain(c.value("TYPE")).lower())
                if dialect_vendor:
                    c.dialect, c.vendor = dialect_vendor
            c.active = (c.value("ACTIVATE") or "true").strip().lower() != "false"
            label_v = plain(c.value("LABEL"))
            sattrs = {"component": canonical or component, "family": family, "database": c.vendor if family.startswith("db_") else None,
                      "label": label_v if label_v and "__" not in label_v and label_v != name else None, "active": None if c.active else False}
            c.ref = self.entity(self.at(node), "tal:Component", {"framework": FRAMEWORK, "job": label, "name": name}, name, sattrs)
            self.relation(self.at(node), job_ref, P.CONTAINS, c.ref)
            if not c.active:
                self.construct(self.at(node), "tal:inactive_component", False)
            comps[unique] = comps.get(unique) or c

        conns: list[_Conn] = []
        for i, cn in enumerate(root.find("connection")):
            src, dst = (cn.get("source") or "").strip(), (cn.get("target") or "").strip()
            if src not in comps or dst not in comps or src == dst:
                continue
            ps = params(cn)
            label_c = (cn.get("label") or "").strip() or (plain(ps["UNIQUE_NAME"].get("value")) if "UNIQUE_NAME" in ps else "")
            conn = _Conn(cn, (cn.get("connectorName") or "").strip(), label_c, src, dst, (cn.get("lineStyle") or "").strip(), i)
            conns.append(conn)
            comps[src].outputs.append(conn)
            comps[dst].inputs.append(conn)
            q: dict[str, Any] = {"via": "flow" if conn.flow else "trigger", "connector": conn.connector, "link": conn.label or None}
            self.construct(self.at(cn), "tal:connection" if conn.flow else "tal:trigger", True)
            self.relation(self.at(cn), comps[dst].ref, P.DEPENDS_ON, comps[src].ref, {k: v for k, v in q.items() if v})
        flow = _Flow(self, label, job_ref, comps, conns)
        flow.run()
        if any(c.family == "run_job" for c in comps.values()):
            self.orchestration(label, job_ref, comps, conns)

    # -- a job that runs jobs

    def run_if(self, conn: _Conn) -> dict[str, Any] | None:
        p = params(conn.node).get("CONDITION")
        if p is None or not (p.get("value") or "").strip():
            return None
        try:
            e = jx.parse(p.get("value") or "", jx.Scope())
            self.construct(self.at(p), "tal:run_if_condition", True)
            return e
        except jx.ExprError:
            self.construct(self.at(p), "tal:expression_unparsed", False)
            return {"e": "platform", "construct": "tal:expression_unparsed"}

    def orchestration(self, job: str, job_ref, comps: dict[str, _Comp], conns: list[_Conn]) -> None:
        body: list[dict[str, Any]] = []
        for c in _ordered(comps):
            at = self.at(c.node)
            span = [at.start_byte, at.end_byte]
            cid = f"tal:component.{c.canonical}" if c.canonical else "tal:component.unknown"
            target = c.target if c.family == "run_job" else None
            if target:
                stmt = {"op": "call", "at": span, "call": {"e": "call", "callee": {"e": "name", "name": target, "kind": "job"}, "args": []}}
            elif c.canonical == "tDie":
                stmt = {"op": "throw", "at": span}
            else:
                stmt = {"op": "platform", "at": span, "construct": cid}
            conds = []
            guarded = False
            for conn in c.inputs:
                if conn.connector.upper() == "RUN_IF":
                    cond = self.run_if(conn)
                    if cond is not None:
                        guarded = True
                        conds.append(cond)
                        continue
                conds.append({"e": "platform", "construct": "tal:connection" if conn.flow else "tal:trigger", "connector": conn.connector, **({"link": conn.label} if conn.label else {})})
            if guarded:
                cond = conds[0]
                for other in conds[1:]:
                    cond = {"e": "binop", "op": "or", "left": cond, "right": other}
                stmt = {"op": "if", "at": span, "cond": cond, "then": [stmt]}
            body.append(stmt)
        if body:
            from repo_intel.compile.behavior import behavior_observation

            self.items.append(behavior_observation(self.blob, self.whole, job_ref, LANGUAGE, body, evidence_class=E1, kind="flow"))


def _ordered(comps: dict[str, _Comp]) -> list[_Comp]:
    """Components upstream first; ties and cycles in the order the item lists them."""
    order = list(comps)
    pending = {k: {conn.src for conn in c.inputs if conn.src != k} for k, c in comps.items()}
    done: list[str] = []
    while pending:
        ready = [k for k in order if k in pending and not (pending[k] - set(done))]
        k = ready[0] if ready else next(k for k in order if k in pending)
        done.append(k)
        del pending[k]
    return [comps[k] for k in done]


# ------------------------------------------------------------------ data flow


class _Flow:
    """The transform shapes of one job, and its tMaps' behaviour."""

    def __init__(self, reader: ItemReader, job: str, job_ref, comps: dict[str, _Comp], conns: list[_Conn]):
        from repo_intel.compile.transforms import Builder

        self.r, self.job, self.job_ref, self.comps, self.conns = reader, job, job_ref, comps, conns
        self.b = Builder()
        self.refs: dict[int, str] = {}  # id(connection) -> the transform (or data name) its rows come from

    def run(self) -> None:
        from repo_intel.compile.transforms import transform_observation

        for c in _ordered(self.comps):
            at = self.r.at(c.node)
            span = [at.start_byte, at.end_byte]
            cid = f"tal:component.{c.canonical}" if c.canonical else "tal:component.unknown"
            flow_in = [conn for conn in c.inputs if conn.flow]
            flow_out = [conn for conn in c.outputs if conn.flow]
            inputs = [self.refs[id(conn)] for conn in flow_in if id(conn) in self.refs]
            before = len(self.b.items)
            handler = getattr(self, f"comp_{c.family}", None)
            if c.family == "run_job":
                modelled = self.comp_run_job(c, span, inputs)
            elif not c.active:
                modelled = False
            else:
                try:
                    modelled = handler(c, span, inputs) if handler else False
                except Exception as exc:  # one component's fault must not lose the job
                    self.r.note("warning", f"component {c.name} of {self.job} not read: {type(exc).__name__}")
                    modelled = False
            if any(id(conn) not in self.refs for conn in flow_out) or (not flow_out and inputs and len(self.b.items) == before):
                ref = self.b.add("platform", span, inputs, construct=cid)
                for conn in flow_out:
                    self.refs.setdefault(id(conn), ref)
            self.r.construct(at, cid, bool(modelled) and c.canonical is not None)
        transforms = self.b.finish()
        if transforms:
            self.r.items.append(transform_observation(self.r.blob, self.r.whole, self.job_ref, LANGUAGE, transforms, E1))

    # -- names and data

    def name_of(self, c: _Comp, setting: str, sql: bool = False) -> tuple[str | None, bool, xmlspan.Node | None]:
        p = c.params.get(setting)
        if p is None:
            return None, False, None
        text, dynamic = jx.string_value(p.get("value"), sql=sql)
        if text is None or not text.strip():
            return None, False, p
        return text.strip(), dynamic, p

    def data(self, c: _Comp, at, kind: str, name: str, dynamic: bool, predicate: Predicate) -> str:
        """The data name a component reads or writes; declares it and the relation when it is literal."""
        if kind == "table":
            from repo_intel.extractors.sql_lineage import clean_name, table

            dynamic = dynamic or "#" in name or "{" in name
            if dynamic:
                name = re.sub(r"#([\w.]+)#", r"{\1}", name).lower()
            else:
                name = clean_name(name)
                t = table(name)
                self.r.items.append(self.r.base.entity(self.r.blob, at, K.TABLE, t["natural_key"], name, {}, E1))
                self.r.relation(at, c.ref, predicate, t)
        elif not dynamic:
            ref = self.r.base.ref(K.DATASET, {"name": name}, name)
            self.r.items.append(self.r.base.entity(self.r.blob, at, K.DATASET, {"name": name}, name, {"format": "file"}, E1))
            self.r.relation(at, c.ref, predicate, ref)
        if dynamic:
            self.r.unresolved += 1
            self.r.construct(at, "tal:dynamic_name", False)
            verb = "reads" if predicate == P.READS_FROM else "writes"
            self.r.note("info", f"component {c.name} of {self.job} {verb} a {'table' if kind == 'table' else 'file'} named by context variables or code: not resolved")
        else:
            self.r.resolved += 1
        return name

    def table_name(self, c: _Comp) -> tuple[str | None, bool, xmlspan.Node | None]:
        name, dynamic, p = self.name_of(c, "TABLE", sql=True)
        if not name:
            return None, False, p
        schema, sdyn, _ = self.name_of(c, "SCHEMA_DB", sql=True)
        if schema and "." not in name:
            name, dynamic = f"{schema}.{name}", dynamic or sdyn
        return name, dynamic, p

    def columns(self, c: _Comp, connector: str = "FLOW") -> list[str]:
        for md in c.node.find("metadata"):
            if (md.get("connector") or "FLOW").upper() == connector:
                return [col.get("name") for col in md.find("column") if col.get("name")]
        return []

    def out(self, c: _Comp, ref: str, connectors: tuple[str, ...] | None = None) -> None:
        for conn in c.outputs:
            if conn.flow and (connectors is None or conn.connector.upper() in connectors):
                self.refs[id(conn)] = ref

    # -- families

    def comp_file_input(self, c: _Comp, span, inputs) -> bool:
        name, dynamic, p = self.name_of(c, "FILENAME")
        if not name:
            self.r.note("info", f"component {c.name} of {self.job} names no file this pack reads")
            return False
        name = self.data(c, self.r.at(p), "file", name, dynamic, P.READS_FROM)
        ref = self.b.add("read", span, [name], **({"dynamic": True} if dynamic else {}))
        self.out(c, ref)
        return True

    def comp_file_output(self, c: _Comp, span, inputs) -> bool:
        name, dynamic, p = self.name_of(c, "FILENAME")
        if not name or not inputs:
            return False
        name = self.data(c, self.r.at(p), "file", name, dynamic, P.WRITES_TO)
        ref = self.b.add("publish", span, inputs[:1], output=name, **({"dynamic": True} if dynamic else {}))
        self.out(c, ref)
        return True

    def comp_db_input(self, c: _Comp, span, inputs) -> bool:
        from repo_intel.compile.transforms import sql_query
        from repo_intel.extractors import sql_lineage

        sql, dynamic, p = self.name_of(c, "QUERY", sql=True)
        if sql and re.search(r"\bselect\b", sql, re.I):
            at = self.r.at(p)
            n = len(self.b.items)
            result = sql_query(self.b, sql, span, dialect=c.dialect)
            ref = self.b.ensure(result, span) if result else (self.b.items[-1]["id"] if len(self.b.items) > n else None)
            for name, _, _ in sql_lineage.lineage(sql, c.dialect).reads:
                self.data(c, at, "table", name, False, P.READS_FROM)
            if dynamic and re.search(r"#[\w.]+#", re.sub(r"'(?:[^']|'')*'", "''", sql)):
                self.r.unresolved += 1
                self.r.construct(at, "tal:dynamic_name", False)
                self.r.note("info", f"component {c.name} of {self.job} reads with a query that context variables or code complete: tables they name are not resolved")
            if ref is None:
                return False
            self.out(c, ref)
            return True
        name, dynamic, p = self.table_name(c)
        if not name:
            self.r.note("info", f"component {c.name} of {self.job} names no table or query this pack reads")
            return False
        name = self.data(c, self.r.at(p), "table", name, dynamic, P.READS_FROM)
        self.out(c, self.b.add("read", span, [name], **({"dynamic": True} if dynamic else {})))
        return True

    def comp_db_output(self, c: _Comp, span, inputs) -> bool:
        name, dynamic, p = self.table_name(c)
        if not name or not inputs:
            return False
        name = self.data(c, self.r.at(p), "table", name, dynamic, P.WRITES_TO)
        action = plain(c.value("DATA_ACTION")).upper()
        fields: dict[str, Any] = {}
        if action in ("INSERT_OR_UPDATE", "UPDATE_OR_INSERT", "UPSERT"):
            fields["primitive"] = "merge_into"
        elif action == "DELETE":
            fields["primitive"] = "apply_deletes"
        elif action == "INSERT_IF_NOT_EXIST":
            fields["candidates"] = ["merge_into"]
        if dynamic:
            fields["dynamic"] = True
        ref = self.b.add("publish", span, inputs[:1], output=name, **fields)
        self.out(c, ref)
        return True

    def comp_db_row(self, c: _Comp, span, inputs) -> bool:
        from repo_intel.compile.transforms import sql_query
        from repo_intel.extractors import sql_lineage

        sql, dynamic, p = self.name_of(c, "QUERY", sql=True)
        if not sql:
            return False
        at = self.r.at(p)
        found = sql_lineage.lineage(sql, c.dialect)
        for name, _, _ in found.reads:
            self.data(c, at, "table", name, False, P.READS_FROM)
        for name, _, _ in found.writes:
            self.data(c, at, "table", name, False, P.WRITES_TO)
        sql_query(self.b, sql, span, dialect=c.dialect)
        if inputs:
            self.out(c, inputs[0])
        return bool(found.reads or found.writes)

    def comp_db_control(self, c: _Comp, span, inputs) -> bool:
        return True

    def comp_control(self, c: _Comp, span, inputs) -> bool:
        if inputs:
            self.out(c, inputs[0])
        return True

    def comp_replicate(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        self.out(c, inputs[0])
        return True

    def comp_run_job(self, c: _Comp, span, inputs) -> bool:
        p = c.params.get("PROCESS")
        raw = plain(p.get("value")) if p is not None else ""
        if c.flag("USE_DYNAMIC_JOB") or not raw or not _NAME.fullmatch(raw):
            self.r.construct(self.r.at(p or c.node), "tal:dynamic_name", False)
            self.r.note("info", f"component {c.name} of {self.job} runs a job chosen at run time: not resolved")
            return False
        c.target = raw
        self.r.relation(self.r.at(p), c.ref, P.RUNS, self.r.base.ref(K.JOB, {"framework": FRAMEWORK, "name": raw}, raw, declare=False))
        if inputs:
            self.out(c, inputs[0])
        return True

    def comp_filter(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        scope = jx.Scope(links={"input_row"})
        cond: dict[str, Any] | None = None
        ok = True
        if c.flag("USE_ADVANCED") and (c.value("ADVANCED_COND") or "").strip():
            p = c.params["ADVANCED_COND"]
            try:
                cond = jx.parse(p.get("value") or "", scope)
                self.r.construct(self.r.at(p), "tal:filter_condition", True)
            except jx.ExprError:
                ok = False
                self.r.construct(self.r.at(p), "tal:expression_unparsed", False)
        else:
            parts = []
            for row in table_rows(c.params.get("CONDITIONS")):
                col, op, rvalue = row.get("INPUT_COLUMN", ""), row.get("OPERATOR", ""), row.get("RVALUE", "")
                fn = row.get("FUNCTION", "").strip()
                if not re.fullmatch(r"[A-Za-z_]\w*", col) or op not in ("==", "!=", "<", "<=", ">", ">="):
                    ok = False
                    continue
                left: dict[str, Any] = {"e": "member", "object": {"e": "var", "scope": "link", "name": "input_row"}, "name": col}
                if fn and fn not in ("", "EMPTY"):
                    left = {"e": "call", "callee": {"e": "name", "name": fn, "kind": "function"}, "args": [left]}
                try:
                    right = jx.parse(rvalue, scope)
                except jx.ExprError:
                    ok = False
                    continue
                parts.append({"e": "binop", "op": op, "left": left, "right": right})
            if parts:
                logical = "or" if plain(c.value("LOGICAL_OP")).strip() in ("||", "OR") else "and"
                cond = parts[0]
                for e in parts[1:]:
                    cond = {"e": "binop", "op": logical, "left": cond, "right": e}
                self.r.construct(self.r.at(c.params.get("CONDITIONS")), "tal:filter_condition", ok)
        rejects = [conn for conn in c.outputs if conn.flow and conn.connector.upper() == "REJECT"]
        split = bool(rejects) and any(conn.flow and conn.connector.upper() != "REJECT" for conn in c.outputs)
        kept = self.b.add("filter", span, inputs[:1], predicate=cond, primitive="split_by_condition" if split else None)
        self.out(c, kept, ("FILTER", "FLOW", "MAIN"))
        if rejects:
            rejected = self.b.add("filter", span, inputs[:1], predicate={"e": "unop", "op": "not", "operand": cond} if cond else None,
                                  primitive="split_by_condition" if split else None)
            self.out(c, rejected, ("REJECT",))
        return ok and cond is not None

    def comp_filter_columns(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        self.out(c, self.b.add("project", span, inputs[:1], primitive="select_columns"))
        return True

    def comp_convert(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        self.out(c, self.b.add("project", span, inputs[:1], candidates=["safe_cast"]))
        return True

    def comp_aggregate(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        keys = []
        for row in table_rows(c.params.get("GROUPBYS")):
            k = row.get("INPUT_COLUMN") or row.get("OUTPUT_COLUMN") or ""
            if re.fullmatch(r"[A-Za-z_]\w*", k) and k not in keys:
                keys.append(k)
        self.out(c, self.b.add("aggregate", span, inputs[:1], keys=keys, primitive="aggregate"))
        return True

    def comp_sort(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        order = []
        for row in table_rows(c.params.get("CRITERIA")):
            col = row.get("COLNAME", "")
            if re.fullmatch(r"[A-Za-z_]\w*", col):
                order.append({"column": col, "desc": row.get("ORDER", "").lower() == "desc"})
        self.out(c, self.b.add("sort", span, inputs[:1], order=order))
        return bool(order)

    def comp_dedup(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        keys = [row.get("SCHEMA_COLUMN", "") for row in table_rows(c.params.get("UNIQUE_KEY")) if row.get("KEY_ATTRIBUTE", "").lower() == "true"]
        keys = [k for k in keys if re.fullmatch(r"[A-Za-z_]\w*", k)]
        unique = self.b.add("deduplicate", span, inputs[:1], keys=keys, primitive="dedupe_by_key" if keys else None, candidates=None if keys else ["dedupe_by_key"])
        self.out(c, unique, ("UNIQUE", "FLOW", "MAIN"))
        if any(conn.flow and conn.connector.upper() == "DUPLICATE" for conn in c.outputs):
            self.out(c, self.b.add("filter", span, inputs[:1]), ("DUPLICATE",))
        return bool(keys)

    def comp_join(self, c: _Comp, span, inputs) -> bool:
        main = [conn for conn in c.inputs if conn.flow and conn.style != _LOOKUP_STYLE]
        lookup = [conn for conn in c.inputs if conn.flow and conn.style == _LOOKUP_STYLE]
        if not main and len(c.inputs) >= 2:
            main, lookup = c.inputs[:1], c.inputs[1:2]
        refs = [self.refs.get(id(conn)) for conn in main[:1] + lookup[:1]]
        if len(refs) < 2 or None in refs:
            return False
        pairs = [[row.get("INPUT_COLUMN", ""), row.get("LOOKUP_COLUMN", "")] for row in table_rows(c.params.get("JOIN_KEY"))]
        pairs = [p for p in pairs if all(re.fullmatch(r"[A-Za-z_]\w*", x) for x in p)]
        jtype = "inner" if c.flag("USE_INNER_JOIN") else "left"
        if pairs:
            ref = self.b.add("join", span, refs, keys=[p[0] for p in pairs], join={"type": jtype, "on": pairs}, primitive="join")
        else:
            ref = self.b.add("join", span, refs, candidates=["join"])
        self.out(c, ref, ("FLOW", "MAIN"))
        if any(conn.flow and conn.connector.upper() == "REJECT" for conn in c.outputs):
            self.out(c, self.b.add("filter", span, [ref]), ("REJECT",))
        return bool(pairs)

    def comp_union(self, c: _Comp, span, inputs) -> bool:
        if not inputs:
            return False
        self.out(c, self.b.add("union", span, inputs, candidates=["union_by_name"]))
        return True

    # -- tMap

    def comp_map(self, c: _Comp, span, inputs) -> bool:
        from repo_intel.compile.transforms import emit_projection

        data = c.node.first("nodeData")
        if data is None:
            return False
        in_tables = data.find("inputTables")
        out_tables = data.find("outputTables")
        var_tables = data.find("varTables")
        if not in_tables:
            return False
        by_label = {conn.label: conn for conn in c.inputs if conn.flow}
        links = {t.get("name") or "" for t in in_tables} - {""}
        scope = jx.Scope(links=links, var_tables={t.get("name") or "Var" for t in var_tables} or {"Var"})
        main = in_tables[0]
        main_name = main.get("name") or ""
        ref = self.refs.get(id(by_label[main_name])) if main_name in by_label else (inputs[0] if inputs else None)
        if ref is None:
            return False
        ok = True
        loop_body: list[dict[str, Any]] = []

        def parse(node: xmlspan.Node, attr: str, construct: str) -> dict[str, Any] | None:
            nonlocal ok
            text = (node.get(attr) or "").strip()
            if not text:
                return None
            try:
                e = jx.parse(text, scope)
                self.r.construct(self.r.at(node), construct, True)
                return e
            except jx.ExprError:
                ok = False
                self.r.construct(self.r.at(node), "tal:expression_unparsed", False)
                return {"e": "platform", "construct": "tal:expression_unparsed"}

        filter_on = lambda t: (t.get("activateExpressionFilter") or "").lower() == "true" and (t.get("expressionFilter") or "").strip()
        main_filter = parse(main, "expressionFilter", "tal:tmap_filter") if filter_on(main) else None
        if main_filter is not None:
            ref = self.b.add("filter", self.span_of(main, span), [ref], predicate=main_filter)
        for t in in_tables[1:]:
            tname = t.get("name") or ""
            conn = by_label.get(tname)
            other = self.refs.get(id(conn)) if conn else None
            if other is None:
                ok = False
                continue
            tspan = self.span_of(t, span)
            lookup_filter = parse(t, "expressionFilter", "tal:tmap_filter") if filter_on(t) else None
            if lookup_filter is not None:
                other = self.b.add("filter", tspan, [other], predicate=lookup_filter)
            pairs = []
            for entry in t.find("mapperTableEntries"):
                expr = parse(entry, "expression", "tal:tmap_expression")
                col = entry.get("name") or ""
                src = _column(expr) if expr else None
                if src and re.fullmatch(r"[A-Za-z_]\w*", col):
                    pairs.append([src, col])
            matching = (t.get("matchingMode") or "").upper()
            jtype = "inner" if (t.get("innerJoin") or "").lower() == "true" else "left"
            self.r.construct(self.r.at(t), "tal:tmap_lookup", bool(pairs))
            if pairs and matching != "ALL_ROWS":
                ref = self.b.add("join", tspan, [ref, other], keys=[p[0] for p in pairs], join={"type": jtype, "on": pairs}, primitive="join")
            elif matching == "ALL_ROWS":
                ref = self.b.add("join", tspan, [ref, other], join={"type": "cross", "on": []}, candidates=["join"])
            else:
                ref = self.b.add("join", tspan, [ref, other], candidates=["join"])
        for vt in var_tables:
            for entry in vt.find("mapperTableEntries"):
                name = (entry.get("name") or "").strip()
                value = parse(entry, "expression", "tal:tmap_variable")
                if not name or value is None:
                    continue
                vat = self.r.at(entry)
                loop_body.append({"op": "assign", "at": [vat.start_byte, vat.end_byte], "target": {"e": "var", "scope": "variable", "name": name}, "value": value})
        in_columns = [e.get("name") for e in main.find("mapperTableEntries") if e.get("name")]
        filtered = [t for t in out_tables if filter_on(t) or (t.get("reject") or "").lower() == "true" or (t.get("rejectInnerJoin") or "").lower() == "true"]
        out_by_label = {conn.label: conn for conn in c.outputs if conn.flow}
        for t in out_tables:
            tname = t.get("name") or ""
            tspan = self.span_of(t, span)
            r = ref
            cond = parse(t, "expressionFilter", "tal:tmap_filter") if filter_on(t) else None
            split = len(filtered) > 1 or (len(filtered) == 1 and len(out_tables) > 1)
            if cond is not None:
                r = self.b.add("filter", tspan, [r], predicate=cond, primitive="split_by_condition" if split else None)
            elif (t.get("reject") or "").lower() == "true" or (t.get("rejectInnerJoin") or "").lower() == "true":
                r = self.b.add("filter", tspan, [r], primitive="split_by_condition" if split else None)  # the rows no other output took
            items = []
            used: set[str] = set()
            out_stmts: list[dict[str, Any]] = []
            for entry in t.find("mapperTableEntries"):
                col = (entry.get("name") or "").strip()
                if not col:
                    continue
                value = parse(entry, "expression", "tal:tmap_expression") or {"e": "lit", "value": None, "type": "null"}
                eat = self.r.at(entry)
                out_stmts.append({"op": "assign", "at": [eat.start_byte, eat.end_byte],
                                  "target": {"e": "member", "object": {"e": "var", "scope": "link", "name": tname}, "name": col}, "value": value})
                item = classify(col, value, links)
                if item.get("source"):
                    used.add(item["source"])
                elif item["effect"] == "keep":
                    used.add(col)
                items.append(item)
            keep_only = bool(in_columns) and bool(set(in_columns) - used - {i["target"] for i in items})
            r = emit_projection(self.b, tspan, r, items, keep_only) if items else r
            if tname in out_by_label:
                self.refs[id(out_by_label[tname])] = r
            out_stmts.append({"op": "output", "at": tspan, "channel": "dataset", "name": tname})
            loop_body += [{"op": "if", "at": tspan, "cond": cond, "then": out_stmts}] if cond is not None else out_stmts
        at = self.r.at(c.node)
        body = [{"op": "loop", "kind": "implicit", "at": [at.start_byte, at.end_byte], "over": {"e": "var", "scope": "link", "name": main_name}, "body": loop_body}]
        from repo_intel.compile.behavior import behavior_observation

        self.r.items.append(behavior_observation(self.r.blob, at, c.ref, LANGUAGE, body, evidence_class=E1, kind="flow"))
        return ok

    def span_of(self, node: xmlspan.Node, default: list[int]) -> list[int]:
        at = self.r.at(node)
        return [at.start_byte, at.end_byte]


def _column(node: Any) -> str | None:
    """The column an expression is, when it is one input row's column as it is."""
    if isinstance(node, dict) and node.get("e") == "member" and isinstance(node.get("object"), dict) and node["object"].get("e") == "var" and node["object"].get("scope") == "link":
        return str(node["name"])
    return None


def _call_name(node: dict[str, Any]) -> tuple[str | None, list[Any]]:
    """A call's lower-case function or method name and its arguments, the method's object first."""
    if node.get("e") != "call" or not isinstance(node.get("callee"), dict):
        return None, []
    callee = node["callee"]
    if callee.get("e") == "name":
        return str(callee["name"]).lower(), list(node.get("args", []))
    if callee.get("e") == "member":
        return str(callee["name"]).lower(), [callee["object"], *node.get("args", [])]
    return None, []


def classify(target: str, value: dict[str, Any], links: set[str]) -> dict[str, Any]:
    """The column effect and primitive of a tMap output expression `target = value`."""
    from repo_intel.compile.transforms import column_item, is_flag_pair, keep_item

    sources = jx.link_columns(value)
    col = _column(value)
    if col and value["object"].get("name") in links:
        return keep_item(col, target)
    fn, args = _call_name(value)
    first = next((_column(a) for a in args if _column(a)), None)
    if fn in _TRIM and first and len(args) == 1:
        return column_item(target, sources, "trim_strings", single=first)
    if fn in _CASE and first and len(args) == 1:
        return column_item(target, sources, "normalize_case", single=first)
    if fn in _DATES and first:
        return column_item(target, sources, "parse_timestamp", single=first)
    if fn in _CASTS and first and len(sources) == 1:
        return column_item(target, sources, None, ["safe_cast"], single=first)
    if value.get("e") == "cond":
        test = value.get("test") or {}
        tested = None
        if test.get("e") == "binop" and test.get("op") == "==" and (test.get("right") or {}).get("e") == "lit" and test["right"].get("value") is None:
            tested = _column(test.get("left"))
        elif _call_name(test)[0] in _ISNULL:
            tested = next((_column(a) for a in _call_name(test)[1] if _column(a)), None)
        if tested and _column(value.get("else")) == tested and (value.get("then") or {}).get("e") == "lit":
            return column_item(target, sources, "apply_defaults", single=tested)
        if all(isinstance(value.get(k), dict) and value[k].get("e") == "lit" for k in ("then", "else")) and is_flag_pair([value["then"].get("value"), value["else"].get("value")]):
            return column_item(target, sources, "conditional_flag")
    if value.get("e") in ("binop", "unop") and value.get("op") in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "not"):
        return column_item(target, sources, "conditional_flag")
    if fn == "equals" and sources:
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")
