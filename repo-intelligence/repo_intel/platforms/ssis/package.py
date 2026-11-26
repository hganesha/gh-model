"""One SSIS package (`.dtsx` XML), read into Jobs, Tasks, Programs, connections and shapes.

The package format of SQL Server 2012 and later is read, with control-flow properties also
taken from the 2005 and 2008 form (`<DTS:Property DTS:Name="ObjectName">` children and
precedence constraints that name executables by `IDREF`). What it declares, all evidence E1:

- the package as a Job (`ssis:Package`, framework `ssis`, named by its path), its user
  variables and parameters by name as attributes;
- each control-flow task and container as a Task (`ssis:Task`) the Job CONTAINS, named by
  the path of names from the package down (`Load/Stage orders`); a precedence constraint
  is `To DEPENDS_ON From`, qualified by what it waits on (`success`, `failure`,
  `completion`) and whether an expression is part of it;
- each connection manager as a ConfigKey `connection:<name>`, which the tasks and programs
  using it REFERENCE; a flat file connection's columns as a record layout (a DataSchema
  `ssis:<name>` CONTAINING Fields, contract 10.1.6). Connection strings are never read;
- each package parameter as a ConfigKey `parameter:<package>#$Package::<name>`; references
  to project parameters name `parameter:<project>#$Project::<name>` (declared by
  `Project.params`); values are never read;
- an Execute SQL task with its SQL typed in as a Program (`ssis:SqlTask`) the Task RUNS,
  with a `sql_step` behaviour unit of `query` nodes (the compiler derives their transform
  shapes) and the tables its SQL reads and writes. SQL from a variable, a file or an
  expression is dynamic: counted and reported, never guessed;
- a Data Flow task as a Program (`ssis:DataFlow`) the Task RUNS, with one `transform`
  observation (language `ssis`) whose transforms follow the pipeline's paths: sources,
  lookups, merge joins, aggregates, conditional splits, derived columns, sorts, unions and
  destinations, embedded SQL read by the shared recognizer (T-SQL). When the flow has
  derived columns or conditional splits it also gets a `flow` behaviour unit: an implicit
  row loop with their assignments and branches, other components as `platform` nodes;
- an Execute Package task of the same project RUNS the child package's Job.

Script tasks and components, FTP, mail, process and file tasks, event handlers, log
providers and package configurations are counted as constructs, not modelled.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Callable

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms.ssis import etlexpr
from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.vocabulary import COMPONENTS, CONTAINERS, FRAMEWORK, PLATFORM, task_type
from repo_intel.platforms.ssis.xmlread import Doc, Place, attr, child, children, descendants, local

P = Predicate
E2 = EvidenceClass.E2
DIALECT = "tsql"  # SSIS SQL is read as T-SQL; the provider is in the connection string, which is never read
_PROTECTION = {"0": "DontSaveSensitive", "1": "EncryptSensitiveWithUserKey", "2": "EncryptSensitiveWithPassword",
               "3": "EncryptAllWithPassword", "4": "EncryptAllWithUserKey", "5": "ServerStorage"}
_DATA_TYPES = {"2": "i2", "3": "i4", "4": "r4", "5": "r8", "6": "cy", "7": "date", "11": "bool", "14": "decimal", "16": "i1", "17": "ui1",
               "18": "ui2", "19": "ui4", "20": "i8", "21": "ui8", "72": "guid", "128": "bytes", "129": "str", "130": "wstr", "131": "numeric",
               "133": "dbDate", "134": "dbTime", "135": "dbTimeStamp", "145": "dbTime2", "146": "dbTimeStampOffset", "301": "text", "302": "ntext"}
_PARAM_REF = re.compile(r"\$(Project|Package)::([A-Za-z_][\w ]*?)(?=\]|$|[^\w ])")
_NAME_OK = re.compile(r"[^\x00-\x1f<>]{1,200}")
_AGGREGATIONS = {"1": "count", "2": "count", "3": "count_distinct", "4": "sum", "5": "avg", "6": "min", "7": "max"}


def prop(el, name: str) -> str | None:
    """A DTS property: the attribute (2012 and later) or a `<DTS:Property DTS:Name=...>` child (2008)."""
    value = attr(el, name)
    if value is not None:
        return value
    for c in children(el, "Property"):
        if attr(c, "Name") == name:
            return (c.text or "").strip()
    return None


def comp_props(el) -> dict[str, str]:
    return {attr(p, "name") or "": (p.text or "").strip() for p in children(child(el, "properties"), "property")}


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "-1", "1")


def placeholder(variable: str) -> str:
    """`User::TableName` as the `{user.tablename}` placeholder of a dynamic name."""
    ns, _, name = variable.replace("@[", "").replace("]", "").partition("::")
    cleaned = re.sub(r"[^\w.]", "_", f"{ns}.{name}" if name else ns).strip("_").lower() or "variable"
    return "{" + (cleaned if re.match(r"[A-Za-z_]", cleaned) else "v_" + cleaned) + "}"


def table_name(raw: str | None) -> str | None:
    from repo_intel.extractors.sql_lineage import clean_name

    if not raw or not raw.strip():
        return None
    name = clean_name(raw.strip())
    return name if re.fullmatch(r"[\w$#@.]+", name) else None


def table_ref(name: str, declare: bool = True) -> dict[str, Any]:
    out = {"kind": "Table", "natural_key": {"datastore": "default", "name": name}, "name": name}
    return out if declare else {**out, "declare": False}


def layout_key(connection: str) -> dict[str, str]:
    return {"kind": "record_layout", "name": f"{PLATFORM}:{connection}"}


def layout_ref(connection: str, declare: bool = True) -> dict[str, Any]:
    out = {"kind": "DataSchema", "natural_key": layout_key(connection), "name": connection}
    return out if declare else {**out, "declare": False}


def config_ref(name: str, declare: bool = True) -> dict[str, Any]:
    out = {"kind": "ConfigKey", "natural_key": {"name": name}, "name": name}
    return out if declare else {**out, "declare": False}


def declare_connection(out: Out, at, el, scope: str, place_at: Callable[[Any], Any]) -> str | None:
    """A connection manager element as a ConfigKey and, for a flat file, its record layout."""
    name = prop(el, "ObjectName")
    creation = prop(el, "CreationName") or ""
    if not name or not _NAME_OK.fullmatch(name):
        return None
    expressions = [e for e in children(el, "PropertyExpression") if (attr(e, "Name") or "").lower() == "connectionstring"]
    for e in expressions:
        out.construct(place_at(e), "ssis:connection_expression", False)
    kind = creation.split(":", 1)[0].strip()
    attrs = {"creation_name": kind if re.fullmatch(r"[\w.]{1,60}", kind) else None, "scope": scope, "expression": True if expressions else None}
    out.entity(at, "ssis:ConnectionManager", {"name": f"connection:{name}"}, f"connection:{name}", attrs)
    columns = descendants(el, "FlatFileColumn")
    if kind.upper() in ("FLATFILE", "MULTIFLATFILE") and columns:
        out.construct(at, "ssis:flat_file_layout", True)
        layout = out.entity(at, "ssis:FlatFileLayout", layout_key(name), name, {"connection": f"connection:{name}", "columns": len(columns)})
        for i, c in enumerate(columns):
            col = prop(c, "ObjectName")
            if not col or not _NAME_OK.fullmatch(col):
                continue
            cat = place_at(c)
            dtype = prop(c, "DataType")
            field = out.entity(cat, "ssis:FlatFileColumn", {"table": f"{PLATFORM}:{name}", "name": col}, col,
                               {"type": _DATA_TYPES.get(dtype or "", dtype if dtype and re.fullmatch(r"\w{1,20}", dtype) else None), "position": i + 1})
            out.relation(cat, layout, P.CONTAINS, field)
    return name


class PackageReader:
    """One package document. `job_name` keys the Job; `project` scopes project parameters;
    `sibling(name)` names the Job of another package of the same project."""

    def __init__(self, out: Out, doc: Doc, data: bytes, place: Place, job_name: str, project: str, sibling: Callable[[str], str],
                 extra: dict[str, Any] | None = None, project_connections: dict[str, str] | None = None):
        self.out, self.doc, self.data, self.place = out, doc, data, place
        self.job_name, self.project, self.sibling = job_name, project, sibling
        self.extra = extra or {}
        # DTSID, refId or name -> connection manager name. A project connection manager is
        # known by its DTSID only when its .conmgr is read with the package (in an .ispac).
        self.connections: dict[str, str] = dict(project_connections or {})
        self.variables: set[str] = set()
        self.parameters: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {}  # refId or DTSID -> task ref
        self.job: dict[str, Any] = {}

    # ------------------------------------------------------------------ positions

    def at(self, el):
        s = self.doc.span(el)
        return self.out.span(self.place, *s) if s else self.out.span(self.place, 0, len(self.data))

    def at_list(self, el) -> list[int]:
        s = self.doc.span(el) or (0, len(self.data))
        return list(self.place.at(*s))

    # ------------------------------------------------------------------ the package

    def run(self) -> None:
        root = self.doc.root
        out = self.out
        whole = self.at(root)
        name = prop(root, "ObjectName") or posixpath.basename(self.job_name)
        out.construct(whole, "ssis:package", True)
        if descendants(root, "EncryptedData"):
            out.note("info", f"package {self.job_name}: its content is encrypted (EncryptAllWith...): only what is outside the encrypted part is read")
        self.job = {"kind": "Job", "natural_key": {"framework": FRAMEWORK, "name": self.job_name}, "name": name if _NAME_OK.fullmatch(name) else posixpath.basename(self.job_name)}
        for cm in children(child(root, "ConnectionManagers"), "ConnectionManager"):
            cname = declare_connection(out, self.at(cm), cm, "package", self.at)
            out.construct(self.at(cm), "ssis:connection_manager", cname is not None)
            if cname:
                for k in (prop(cm, "DTSID"), attr(cm, "refId"), cname):
                    if k:
                        self.connections[k] = cname
        for p in children(child(root, "PackageParameters"), "PackageParameter"):
            pname = prop(p, "ObjectName")
            ok = bool(pname and _NAME_OK.fullmatch(pname))
            out.construct(self.at(p), "ssis:package_parameter", ok)
            if ok:
                self.parameters.append(pname)  # type: ignore[arg-type]
                key = self.param_name("Package", pname)  # type: ignore[arg-type]
                out.entity(self.at(p), "ssis:Parameter", {"name": key}, key, {"scope": "package", "data_type": _DATA_TYPES.get(prop(p, "DataType") or ""),
                                                                            "required": True if is_true(prop(p, "Required")) else None,
                                                                            "sensitive": True if is_true(prop(p, "Sensitive")) else None})
        for c in children(child(root, "Configurations"), "Configuration"):
            out.construct(self.at(c), "ssis:configuration", False)
        for lp in children(child(root, "LogProviders"), "LogProvider"):
            out.construct(self.at(lp), "ssis:log_provider", False)
        self.collect_variables(root)
        self.executables(root, [], self.job)
        self.event_handlers(root)
        attrs = {
            "package_name": name if _NAME_OK.fullmatch(name) else None, "protection_level": _PROTECTION.get(prop(root, "ProtectionLevel") or "", None),
            "format_version": prop(root, "PackageFormatVersion") if re.fullmatch(r"\d{1,3}", prop(root, "PackageFormatVersion") or "") else None,
            "variables": sorted(self.variables), "parameters": sorted(self.parameters), **self.extra,
        }
        job = out.entity(whole, "ssis:Package", self.job["natural_key"], self.job["name"], attrs)
        self.job = job
        # Connections referenced from package-level property expressions.
        for e in children(root, "PropertyExpression"):
            self.param_refs(self.at(e), job, e.text or "")

    def param_name(self, scope: str, name: str) -> str:
        if scope == "Package":
            return f"parameter:{self.job_name}#$Package::{name}"
        return f"parameter:{self.project}#$Project::{name}"

    def param_refs(self, at, subject, text: str) -> None:
        for m in _PARAM_REF.finditer(text or ""):
            scope, name = m.group(1), m.group(2).strip()
            key = self.param_name(scope, name)
            self.out.construct(at, "ssis:parameter_reference", True)
            declared = scope == "Package" and name in self.parameters
            self.out.relation(at, subject, P.REFERENCES_CONFIG, config_ref(key, declare=declared), {"via": "expression"})

    def collect_variables(self, el) -> None:
        for v in children(child(el, "Variables"), "Variable"):
            ns, vname = prop(v, "Namespace") or "User", prop(v, "ObjectName")
            if vname and ns == "User" and _NAME_OK.fullmatch(vname):
                self.out.construct(self.at(v), "ssis:variable", True)
                self.variables.add(f"User::{vname}")

    def event_handlers(self, el) -> None:
        for h in children(child(el, "EventHandlers"), "EventHandler"):
            self.out.construct(self.at(h), "ssis:event_handler", False)

    def connection_of(self, value: str | None) -> str | None:
        if not value:
            return None
        if value in self.connections:
            return self.connections[value]
        bare = value.split(":", 1)[0] if value.startswith("{") else value  # `{guid}:external` names a project connection
        if bare in self.connections:
            return self.connections[bare]
        m = re.search(r"(?:Package|Project)\.ConnectionManagers\[(.+)\]$", value)
        if m and _NAME_OK.fullmatch(m.group(1)):
            return self.connections.get(m.group(1), m.group(1))
        return None

    # ------------------------------------------------------------------ control flow

    def executables(self, container, parent: list[str], parent_ref) -> None:
        out = self.out
        seen: dict[str, int] = {}
        local_ids: dict[str, dict[str, Any]] = {}
        # 2012 and later wrap tasks in <DTS:Executables>; 2005 and 2008 put them directly in the container.
        wrapper = child(container, "Executables")
        for ex in children(wrapper if wrapper is not None else container, "Executable"):
            name = prop(ex, "ObjectName")
            etype = prop(ex, "ExecutableType") or prop(ex, "CreationName") or ""
            kind, cid = task_type(etype)
            at = self.at(ex)
            if not name or not _NAME_OK.fullmatch(name):
                out.construct(at, cid, False)
                continue
            n = seen[name] = seen.get(name, 0) + 1
            label = name if n == 1 else f"{name}~{n}"
            path = [*parent, label]
            task_name = "/".join(path)
            creation = etype.split(",", 1)[0].strip()
            attrs: dict[str, Any] = {
                "type": kind, "label": name, "creation_name": creation if re.fullmatch(r"[\w.:]{1,120}", creation) else None,
                "container": "/".join(parent) or None, "disabled": True if is_true(prop(ex, "Disabled")) else None,
            }
            if kind == "foreach_loop":
                enum = child(ex, "ForEachEnumerator")
                ename = (prop(enum, "CreationName") or "") if enum is not None else ""
                m = re.search(r"ForEach(\w+?)Enumerator", ename)
                attrs["enumerator"] = m.group(1).lower() if m else None
            key = {"framework": FRAMEWORK, "job": self.job_name, "name": task_name}
            ref = out.entity(at, "ssis:Task", key, name, attrs)
            out.relation(at, self.job, P.CONTAINS, ref)
            for k in (attr(ex, "refId"), prop(ex, "DTSID")):
                if k:
                    self.tasks[k] = ref
                    local_ids[k] = ref
            self.collect_variables(ex)
            self.event_handlers(ex)
            for e in children(ex, "PropertyExpression"):
                out.construct(self.at(e), "ssis:property_expression", False)
                self.param_refs(self.at(e), ref, e.text or "")
            if kind == "execute_sql":
                self.sql_task(ex, task_name, ref)
            elif kind == "data_flow":
                out.construct(at, cid, True)
                self.data_flow(ex, task_name, ref)
            elif kind == "execute_package":
                self.execute_package(ex, task_name, ref)
            elif kind in CONTAINERS:
                out.construct(at, cid, True)
                self.executables(ex, path, ref)
            else:
                out.construct(at, cid, kind == "other")
        self.constraints(container, local_ids)

    def constraints(self, container, ids: dict[str, dict[str, Any]]) -> None:
        out = self.out
        constraints = child(container, "PrecedenceConstraints")
        for pc in children(constraints if constraints is not None else container, "PrecedenceConstraint"):
            at = self.at(pc)
            src, dst = attr(pc, "From"), attr(pc, "To")
            if src is None or dst is None:  # the 2008 form: <DTS:Executable IDREF=... DTS:IsFrom="-1"/>
                for e in children(pc, "Executable"):
                    if is_true(attr(e, "IsFrom")):
                        src = attr(e, "IDREF")
                    else:
                        dst = attr(e, "IDREF")
            a, b = ids.get(src or ""), ids.get(dst or "")
            out.construct(at, "ssis:precedence_constraint", a is not None and b is not None)
            if a is None or b is None:
                continue
            value = prop(pc, "Value") or "0"
            evalop = prop(pc, "EvalOp") or "2"
            condition = {"1": "expression", "3": "expression_and_constraint", "4": "expression_or_constraint"}.get(evalop)
            on = None if evalop == "1" else {"0": "success", "1": "failure", "2": "completion"}.get(value, "success")
            q = {"via": "precedence", "on": on, "condition": condition, "logical_and": False if prop(pc, "LogicalAnd") is not None and not is_true(prop(pc, "LogicalAnd")) else None}
            out.relation(at, b, P.DEPENDS_ON, a, q)
            expression = prop(pc, "Expression")
            if condition and expression:
                if etlexpr.parse(expression, "ssis") is None:
                    out.construct(at, "ssis:expression_unread", False)
                self.param_refs(at, b, expression)

    # ------------------------------------------------------------------ Execute SQL

    def program_ref(self, term_name: str, task_name: str) -> tuple[dict[str, Any], str]:
        name = f"{self.job_name}#{task_name}"
        return {"kind": "Program", "natural_key": {"platform": PLATFORM, "name": name}, "name": name.rsplit("/", 1)[-1].rsplit("#", 1)[-1]}, name

    def sql_task(self, ex, task_name: str, task_ref) -> None:
        from repo_intel.compile.behavior import behavior_observation

        out = self.out
        at = self.at(ex)
        data = next(iter(descendants(child(ex, "ObjectData"), "SqlTaskData")), None)
        source_type = (attr(data, "SqlStmtSourceType") or "DirectInput") if data is not None else "DirectInput"
        sql = attr(data, "SqlStatementSource") if data is not None else None
        expression = any((attr(e, "Name") or "").lower() == "sqlstatementsource" for e in children(ex, "PropertyExpression"))
        connection = self.connection_of(attr(data, "Connection")) if data is not None else None
        if connection is None and data is not None and attr(data, "Connection"):
            out.note("info", f"Execute SQL task {task_name} of {self.job_name} names a connection manager this package does not hold (a project connection known only by its id): not linked")
        if data is None or source_type.lower() != "directinput" or expression or not (sql or "").strip():
            out.construct(at, "ssis:task.execute_sql_dynamic", False)
            why = "an expression" if expression else "a variable" if source_type.lower() == "variable" else "a file connection" if source_type.lower() == "fileconnection" else "nowhere it can read"
            out.note("info", f"Execute SQL task {task_name} of {self.job_name} takes its SQL from {why}: not read")
            out.unresolved += 1
            if connection:
                out.relation(at, task_ref, P.REFERENCES_CONFIG, config_ref(f"connection:{connection}", declare=False), {"via": "execute_sql"})
            return
        out.construct(at, "ssis:task.execute_sql", True)
        program, pname = self.program_ref("ssis:SqlTask", task_name)
        decl = out.entity(at, "ssis:SqlTask", program["natural_key"], program["name"], {"kind": "sql_task", "language": "sql", "package": self.job_name, "task": task_name,
                                                                                       "connection": f"connection:{connection}" if connection else None})
        out.relation(at, task_ref, P.RUNS, decl)
        if connection:
            for subject in (decl, task_ref):
                out.relation(at, subject, P.REFERENCES_CONFIG, config_ref(f"connection:{connection}", declare=False), {"via": "execute_sql"})
        params = []
        for b in children(data, "ParameterBinding"):
            var = attr(b, "DtsVariableName") or ""
            pname_ = (attr(b, "ParameterName") or str(len(params))).strip()
            ns, _, vname = var.partition("::")
            value = {"e": "var", "scope": {"$project": "project", "$package": "package", "system": "system"}.get(ns.lower(), "user"), "name": vname or var}
            entry = {"name": pname_, "value": value, **({"direction": attr(b, "ParameterDirection").lower()} if (attr(b, "ParameterDirection") or "").lower() in ("output", "returnvalue") else {})}
            params.append(entry)
            self.param_refs(at, decl, var)
        statements = self.statements(sql or "")
        body = []
        positional = all(re.fullmatch(r"\d{1,3}", p["name"]) for p in params)
        taken = 0
        for s in statements:
            if positional:
                # OLE DB markers bind in order across the whole batch.
                n = len(re.findall(r"\?", s))
                mine, taken = params[taken:taken + n], taken + n
            else:
                mine = [p for p in params if re.search(re.escape(p["name"]) + r"\b", s)]
            node = {"op": "query", "at": self.at_list(ex), "sql": s, "params": mine}
            if connection:
                node["datasource"] = connection
            body.append(node)
        if not body:
            return
        out.add(behavior_observation(out.blob, at, decl, PLATFORM, body, evidence_class=EvidenceClass.E1, kind="sql_step"))
        self.lineage(at, sql or "", [decl, task_ref], "execute_sql")

    @staticmethod
    def statements(sql: str) -> list[str]:
        from repo_intel.extractors.contracts_data import split_statements
        from repo_intel.extractors.sql_lineage import scrub_commands

        clean = scrub_commands(sql)
        return [clean[a:b].strip().rstrip(";").strip() for a, b in split_statements(clean) if clean[a:b].strip().strip(";").strip()]

    def lineage(self, at, sql: str, subjects: list[dict[str, Any]], via: str) -> list[str]:
        """READS_FROM and WRITES_TO for the tables `sql` names; the tables read."""
        from repo_intel.extractors import sql_lineage

        found = sql_lineage.lineage(sql_lineage.scrub_commands(sql), DIALECT)
        reads = []
        for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
            for name, _, _ in items:
                t = self.out.neutral(at, K.TABLE, {"datastore": "default", "name": name}, name, {}, E2)
                for i, s in enumerate(subjects):
                    self.out.relation(at, s, predicate, t, {"via": via} if i else None, E2)
                if predicate == P.READS_FROM:
                    reads.append(name)
        self.out.resolved += len(found.writes) + len(found.reads)
        self.out.unresolved += found.unresolved
        return reads

    # ------------------------------------------------------------------ Execute Package

    def execute_package(self, ex, task_name: str, task_ref) -> None:
        out = self.out
        at = self.at(ex)
        spec = next(iter(descendants(child(ex, "ObjectData"), "ExecutePackageTask")), None)
        use_project = is_true((child(spec, "UseProjectReference").text if child(spec, "UseProjectReference") is not None else None)) if spec is not None else False
        package = (child(spec, "PackageName").text or "").strip() if spec is not None and child(spec, "PackageName") is not None else ""
        dynamic = any((attr(e, "Name") or "").lower() in ("packagename", "connection") for e in children(ex, "PropertyExpression"))
        if use_project and package and not dynamic and re.fullmatch(r"[^\\/:*?\"<>|]{1,200}\.dtsx", package, re.I):
            out.construct(at, "ssis:task.execute_package", True)
            child_job = self.sibling(package)
            out.relation(at, task_ref, P.RUNS, {"kind": "Job", "natural_key": {"framework": FRAMEWORK, "name": child_job}, "name": package, "declare": False}, {"via": "execute_package"})
            return
        out.construct(at, "ssis:task.execute_package_external", False)
        conn = self.connection_of((child(spec, "Connection").text or "").strip()) if spec is not None and child(spec, "Connection") is not None else None
        if conn:
            out.relation(at, task_ref, P.REFERENCES_CONFIG, config_ref(f"connection:{conn}", declare=False), {"via": "execute_package"})
        out.note("info", f"Execute Package task {task_name} of {self.job_name} runs a package named by {'an expression' if dynamic else 'a connection'}: not linked")

    # ------------------------------------------------------------------ data flow

    def data_flow(self, ex, task_name: str, task_ref) -> None:
        DataFlow(self, ex, task_name, task_ref).run()


class DataFlow:
    """The pipeline of one Data Flow task as transform shapes and a row-level flow unit."""

    def __init__(self, pkg: PackageReader, ex, task_name: str, task_ref):
        from repo_intel.compile.transforms import Builder

        self.pkg, self.out, self.ex, self.task_name, self.task_ref = pkg, pkg.out, ex, task_name, task_ref
        self.b = Builder()
        self.flow: list[dict[str, Any]] = []
        self.lowered = False
        self.program, _ = pkg.program_ref("ssis:DataFlow", task_name)
        self.lineage_names: dict[str, str] = {}
        self.subjects: list[dict[str, Any]] = []

    def run(self) -> None:
        from repo_intel.compile.behavior import behavior_observation
        from repo_intel.compile.transforms import transform_observation

        pkg, out = self.pkg, self.out
        at = pkg.at(self.ex)
        pipeline = next(iter(descendants(child(self.ex, "ObjectData"), "pipeline")), None)
        decl = out.entity(at, "ssis:DataFlow", self.program["natural_key"], self.program["name"], {"kind": "data_flow", "package": pkg.job_name, "task": self.task_name})
        out.relation(at, self.task_ref, P.RUNS, decl)
        self.subjects = [decl, self.task_ref]
        if pipeline is None:
            out.note("info", f"data flow {self.task_name} of {pkg.job_name} has no pipeline to read")
            return
        comps = children(child(pipeline, "components"), "component")
        for el in pipeline.iter():
            lid = el.get("lineageId")
            nm = el.get("cachedName") or el.get("name")
            if lid and nm and local(el.tag) in ("outputColumn", "inputColumn", "externalMetadataColumn"):
                self.lineage_names.setdefault(lid, nm)
        out_owner: dict[str, tuple[int, Any]] = {}  # output id -> (component index, output element)
        in_owner: dict[str, tuple[int, Any]] = {}
        for i, c in enumerate(comps):
            for o in children(child(c, "outputs"), "output"):
                for k in (o.get("refId"), o.get("id")):
                    if k:
                        out_owner[k] = (i, o)
            for inp in children(child(c, "inputs"), "input"):
                for k in (inp.get("refId"), inp.get("id")):
                    if k:
                        in_owner[k] = (i, inp)
        feeds: dict[int, list[tuple[Any, str]]] = {}  # component -> [(its input element, upstream output id)]
        deps: dict[int, set[int]] = {i: set() for i in range(len(comps))}
        for path in children(child(pipeline, "paths"), "path"):
            start, end = path.get("startId") or "", path.get("endId") or ""
            src, dst = out_owner.get(start), in_owner.get(end)
            pat = pkg.at(path)
            if src is not None and (src[1].get("isErrorOut") or "").lower() == "true":
                out.construct(pat, "ssis:dataflow.error_output", False)
                continue
            out.construct(pat, "ssis:dataflow.path", src is not None and dst is not None)
            if src is None or dst is None:
                continue
            feeds.setdefault(dst[0], []).append((dst[1], start))
            deps[dst[0]].add(src[0])
        results: dict[str, str | None] = {}  # output id -> transform id or name
        done: set[int] = set()
        order: list[int] = []
        while len(order) < len(comps):
            ready = next((i for i in range(len(comps)) if i not in done and deps[i] <= done), None)
            if ready is None:  # a cycle or a dangling path: the rest in document order
                ready = next(i for i in range(len(comps)) if i not in done)
            done.add(ready)
            order.append(ready)
        for i in order:
            c = comps[i]
            inputs = []
            for inp in children(child(c, "inputs"), "input"):
                ids = [up for (el, up) in feeds.get(i, []) if el is inp]
                inputs.append((inp, results.get(ids[0]) if ids else None))
            outputs = [o for o in children(child(c, "outputs"), "output") if (o.get("isErrorOut") or "").lower() != "true"]
            refs = self.component(c, inputs, outputs)
            for o, r in zip(outputs, refs):
                for k in (o.get("refId"), o.get("id")):
                    if k:
                        results[k] = r
        transforms = self.b.finish()
        if transforms:
            out.add(transform_observation(out.blob, at, decl, PLATFORM, transforms, evidence_class=EvidenceClass.E1))
        if self.lowered and self.flow:
            body = [{"op": "loop", "kind": "implicit", "at": pkg.at_list(self.ex), "body": self.flow}]
            out.add(behavior_observation(out.blob, at, decl, PLATFORM, body, evidence_class=EvidenceClass.E1, kind="flow"))

    # -- helpers

    def col(self, lineage: str | None) -> str | None:
        if not lineage:
            return None
        lid = lineage.strip()
        if lid.startswith("#{") and lid.endswith("}"):
            lid = lid[2:-1]
        name = self.lineage_names.get(lid)
        if name is None:
            m = re.search(r"\.Columns\[([^\]]+)\]$", lid)
            name = m.group(1) if m else None
        return name.lower() if name else None

    def relate(self, at, predicate, obj, extra: dict[str, Any] | None = None) -> None:
        for i, s in enumerate(self.subjects):
            self.out.relation(at, s, predicate, obj, {**(extra or {}), **({"via": "data_flow"} if i else {})})

    def connection(self, c) -> str | None:
        for conn in children(child(c, "connections"), "connection"):
            name = self.pkg.connection_of(conn.get("connectionManagerRefId")) or self.pkg.connection_of(conn.get("connectionManagerID"))
            if name:
                return name
        return None

    def use_connection(self, at, c) -> str | None:
        name = self.connection(c)
        if name:
            self.relate(at, P.REFERENCES_CONFIG, config_ref(f"connection:{name}", declare=False))
        return name

    def sql(self, sql: str, at_list: list[int]) -> str:
        """The shared recognizer on one SQL text; the id of its result."""
        from repo_intel.compile.transforms import sql_query

        before = len(self.b.items)
        ref = sql_query(self.b, sql, at_list, dialect=DIALECT)
        if ref is not None:
            return ref
        if len(self.b.items) > before:
            return self.b.items[-1]["id"]
        return self.b.add("platform", at_list, [], construct="ssis:dataflow.source")

    def platform(self, construct: str, at, name: str) -> None:
        self.flow.append({"op": "platform", "construct": construct, "at": at, "detail": {"component": name}})

    def expression(self, text: str | None, at) -> dict[str, Any] | None:
        if not text:
            return None
        e = etlexpr.parse(text, "ssis")
        if e is None:
            self.out.construct(at, "ssis:expression_unread", False)
        self.pkg.param_refs(at, self.subjects[0], text)
        return e

    # -- components

    def component(self, c, inputs: list[tuple[Any, str | None]], outputs: list[Any]) -> list[str | None]:
        out, b = self.out, self.b
        at = self.pkg.at(c)
        al = self.pkg.at_list(c)
        cls = (c.get("componentClassID") or "").lower()
        role, cid = COMPONENTS.get(cls, ("other", "ssis:dataflow.other"))
        name = c.get("name") or cls
        props = comp_props(c)
        ups = [r for _, r in inputs if r is not None]
        up = ups[0] if ups else None
        same = lambda r: [r] * len(outputs)  # noqa: E731

        if role == "source_db":
            mode = props.get("AccessMode", "")
            conn = self.use_connection(at, c)
            sql_text = props.get("SqlCommand", "")
            table = props.get("OpenRowset") or props.get("TableOrViewName")
            variable = props.get("OpenRowsetVariable") or props.get("SqlCommandVariable")
            if mode in ("1", "3") and variable:
                out.construct(at, "ssis:dataflow.source_dynamic", False)
                out.note("info", f"source {name} of data flow {self.task_name} reads a table or SQL named by the variable {variable}: not resolved")
                out.unresolved += 1
                self.platform("ssis:dataflow.source_dynamic", al, name)
                return same(b.add("read", al, [placeholder(variable)]))
            if sql_text.strip() and mode in ("2", ""):
                out.construct(at, cid, True)
                ref = self.sql(sql_text, al)
                self.pkg.lineage(at, sql_text, self.subjects, "data_flow")
                self.platform(cid, al, name)
                return same(ref)
            tname = table_name(table)
            if tname:
                out.construct(at, cid, True)
                t = out.neutral(at, K.TABLE, {"datastore": "default", "name": tname}, tname)
                self.relate(at, P.READS_FROM, t, {"connection": f"connection:{conn}" if conn else None})
                out.resolved += 1
                self.platform(cid, al, name)
                return same(b.add("read", al, [tname]))
            out.construct(at, cid, False)
            self.platform(cid, al, name)
            return same(b.add("platform", al, [], construct=cid))
        if role in ("source_file", "dest_file"):
            conn = self.connection(c)
            if conn is None:
                out.construct(at, cid, False)
                self.platform(cid, al, name)
                return same(b.add("platform", al, ups, construct=cid))
            out.construct(at, cid, True)
            self.relate(at, P.REFERENCES_CONFIG, config_ref(f"connection:{conn}", declare=False))
            self.relate(at, P.READS_FROM if role == "source_file" else P.WRITES_TO, layout_ref(conn, declare=False))
            out.resolved += 1
            self.platform(cid, al, name)
            target = f"{PLATFORM}:{conn}".lower()
            if role == "source_file":
                return same(b.add("read", al, [target]))
            b.write(up, target, al) if up is not None else b.add("publish", al, [], output=target)
            return []
        if role == "dest_db":
            conn = self.use_connection(at, c)
            mode = props.get("AccessMode", "")
            variable = props.get("OpenRowsetVariable")
            tname = table_name(props.get("OpenRowset") or props.get("TableOrViewName"))
            self.platform(cid, al, name)
            if mode in ("1", "4") and variable:
                out.construct(at, "ssis:dataflow.destination_dynamic", False)
                out.note("info", f"destination {name} of data flow {self.task_name} writes a table named by the variable {variable}: not resolved")
                out.unresolved += 1
                b.write(up, placeholder(variable), al) if up is not None else b.add("publish", al, [], output=placeholder(variable))
                return []
            if tname is None:
                out.construct(at, cid, False)
                b.add("platform", al, ups, construct=cid)
                return []
            out.construct(at, cid, True)
            t = out.neutral(at, K.TABLE, {"datastore": "default", "name": tname}, tname)
            self.relate(at, P.WRITES_TO, t, {"connection": f"connection:{conn}" if conn else None})
            out.resolved += 1
            if up is not None:
                b.write(up, tname, al)
            else:
                b.add("publish", al, [], output=tname)
            return []
        if up is None and role not in ("source_other",):
            out.construct(at, cid, False)
            self.platform(cid, al, name)
            return same(None)
        if role == "lookup":
            return self.lookup(c, at, al, cid, name, props, up, outputs)
        if role == "merge_join":
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            left = inputs[0][1] if inputs else None
            right = inputs[1][1] if len(inputs) > 1 else None
            jtype = {"0": "full", "1": "left", "2": "inner"}.get(props.get("JoinType", "2"), "inner")
            keys = int(props.get("NumKeyColumns", "0") or 0) if (props.get("NumKeyColumns", "0") or "0").isdigit() else 0

            def sort_keys(inp) -> list[str]:
                cols = [(int(ic.get("cachedSortKeyPosition")), (ic.get("cachedName") or "").lower()) for ic in children(child(inp, "inputColumns"), "inputColumn")
                        if (ic.get("cachedSortKeyPosition") or "").lstrip("-").isdigit() and int(ic.get("cachedSortKeyPosition")) != 0]
                return [n for _, n in sorted(cols, key=lambda x: abs(x[0]))][:keys]

            lk = sort_keys(inputs[0][0]) if inputs else []
            rk = sort_keys(inputs[1][0]) if len(inputs) > 1 else []
            pairs = [[a_, b_] for a_, b_ in zip(lk, rk)] if keys and len(lk) == len(rk) == keys else []
            fields: dict[str, Any] = {"join": {"type": jtype, **({"on": pairs} if pairs else {})}}
            if pairs:
                fields.update(keys=lk, primitive="join")
            else:
                fields["candidates"] = ["join"]
            return same(b.add("join", al, [r for r in (left, right) if r is not None], **fields))
        if role == "aggregate":
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            output = outputs[0] if outputs else None
            group: list[str] = []
            measures: list[dict[str, Any]] = []
            exact = True
            for oc in children(child(output, "outputColumns"), "outputColumn") if output is not None else []:
                cp = comp_props(oc)
                kind = cp.get("AggregationType", "")
                source = self.col(cp.get("AggregationColumnId")) or None
                alias = (oc.get("name") or "").lower()
                if kind == "0":
                    group.append(source or alias)
                elif kind in _AGGREGATIONS:
                    m = {"fn": _AGGREGATIONS[kind], "as": alias}
                    if source and kind != "2":
                        m["column"] = source
                    elif kind != "2":
                        exact = False
                    measures.append(m)
                else:
                    exact = False
            ref = b.add("aggregate", al, [up], keys=group, measures=measures, primitive="aggregate" if exact and measures else None, candidates=None if exact and measures else ["aggregate"])
            return [ref] + [ref] * (len(outputs) - 1)
        if role == "split":
            return self.split(at, al, cid, name, up, outputs)
        if role == "derived":
            return same(self.derived(c, at, al, cid, name, up))
        if role in ("convert", "copy", "charmap"):
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            items = []
            for oc in children(child(outputs[0], "outputColumns"), "outputColumn") if outputs else []:
                cp = comp_props(oc)
                src = self.col(cp.get("SourceInputColumnLineageID") or cp.get("copyColumnId"))
                target = (oc.get("name") or "").lower()
                if not target:
                    continue
                cand = ["safe_cast"] if role == "convert" else ["normalize_case"] if role == "charmap" else []
                items.append(self._item(target, [src] if src else [], None, cand, single=src))
            if role == "charmap":
                for ic in children(child(child(child(c, "inputs"), "input"), "inputColumns"), "inputColumn"):
                    if (ic.get("usageType") or "").lower() == "readwrite" and ic.get("cachedName"):
                        n = ic.get("cachedName").lower()
                        items.append({**self._item(n, [n], None, ["normalize_case"], single=n), "effect": "replace"})
            return same(self._project(al, up, items))
        if role == "sort":
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            keyed = []
            for ic in children(child(child(child(c, "inputs"), "input"), "inputColumns"), "inputColumn"):
                pos = comp_props(ic).get("NewSortKeyPosition", "0")
                if pos.lstrip("-").isdigit() and int(pos) != 0 and ic.get("cachedName"):
                    keyed.append((abs(int(pos)), {"column": ic.get("cachedName").lower(), "desc": int(pos) < 0}))
            order = [o for _, o in sorted(keyed, key=lambda x: x[0])]
            ref = b.add("sort", al, [up], order=order)
            if is_true(props.get("EliminateDuplicates")):
                ref = b.add("deduplicate", al, [ref], keys=[o["column"] for o in order], primitive="dedupe_by_key" if order else None, candidates=None if order else ["dedupe_by_hash"])
            return same(ref)
        if role == "union":
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            return same(b.add("union", al, ups) if len(ups) > 1 else up)
        if role == "pass":
            out.construct(at, cid, cid == "ssis:dataflow.multicast")
            self.platform(cid, al, name)
            return same(up)
        if role == "command":
            sql_text = props.get("SqlCommand", "")
            self.use_connection(at, c)
            self.platform(cid, al, name)
            if not sql_text.strip():
                out.construct(at, cid, False)
                return same(up)
            out.construct(at, cid, True)
            self.sql(sql_text, al)
            self.pkg.lineage(at, sql_text, self.subjects, "data_flow")
            return same(up)
        if role in ("pivot", "unpivot"):
            out.construct(at, cid, True)
            self.platform(cid, al, name)
            return same(b.add(role, al, [up], candidates=[role]))
        if role == "scd":
            out.construct(at, cid, False)
            self.platform(cid, al, name)
            historical = any(comp_props(ic).get("ColumnType") == "2" for ic in descendants(c, "inputColumn"))
            return same(b.add("platform", al, [up], construct=cid, candidates=["apply_scd_type_2"] if historical else ["merge_into"]))
        out.construct(at, cid, False)
        if role == "other" and cid == "ssis:dataflow.other":
            out.note("info", f"data flow {self.task_name}: component {name} of a class this reader does not know is counted, not read")
        self.platform(cid, al, name)
        return same(b.add("platform", al, ups, construct=cid))

    def _item(self, target: str, sources: list[str], primitive: str | None, candidates: list[str], single: str | None = None) -> dict[str, Any]:
        from repo_intel.compile.transforms import column_item

        return column_item(target, sources, primitive, candidates, single=single)

    def _project(self, al: list[int], up: str | None, items: list[dict[str, Any]]) -> str | None:
        from repo_intel.compile.transforms import emit_projection

        if up is None:
            return None
        if not items:
            return up
        return emit_projection(self.b, al, up, items, keep_only=False)

    def lookup(self, c, at, al, cid, name, props, up, outputs) -> list[str | None]:
        out, b = self.out, self.b
        out.construct(at, cid, True)
        self.platform(cid, al, name)
        self.use_connection(at, c)
        sql_text = props.get("SqlCommand", "")
        if props.get("CacheType") == "2" or not sql_text.strip():
            right = b.add("platform", al, [], construct="ssis:dataflow.lookup")
        else:
            right = self.sql(sql_text, al)
            self.pkg.lineage(at, sql_text, self.subjects, "data_flow")
        pairs = []
        for ic in descendants(child(c, "inputs"), "inputColumn"):
            refcol = comp_props(ic).get("JoinToReferenceColumn")
            if refcol and ic.get("cachedName"):
                pairs.append([ic.get("cachedName").lower(), refcol.lower()])
        added = [(oc.get("name") or "").lower() for o in outputs[:1] for oc in children(child(o, "outputColumns"), "outputColumn") if comp_props(oc).get("CopyFromReferenceColumn")]
        no_match_rows = props.get("NoMatchBehavior") == "1"
        match_output = outputs[0] if outputs else None
        ignore = (match_output.get("errorRowDisposition") if match_output is not None else "") == "IgnoreFailure"
        jtype = "left" if (not no_match_rows and ignore) else "inner"

        def join(kind: str, columns: list[str]) -> str:
            fields: dict[str, Any] = {"join": {"type": kind, **({"on": pairs} if pairs else {})}}
            if pairs:
                fields.update(keys=[p[0] for p in pairs], primitive="join")
            else:
                fields["candidates"] = ["join"]
            if columns:
                fields["columns"] = {"add": columns}
            return b.add("join", al, [up, right], **fields)

        refs: list[str | None] = []
        for i, o in enumerate(outputs):
            if i == 0:
                refs.append(join(jtype, added))
            elif i == 1 and no_match_rows:
                refs.append(join("anti", []))
            else:
                refs.append(None)
        return refs

    def split(self, at, al, cid, name, up, outputs) -> list[str | None]:
        out, b = self.out, self.b
        out.construct(at, cid, True)
        branches = []
        default = None
        for o in outputs:
            op = comp_props(o)
            if is_true(op.get("IsDefaultOut")) or is_true(o.get("isDefaultOut")):
                default = o
                continue
            order = op.get("EvaluationOrder", "")
            text = op.get("FriendlyExpression") or op.get("Expression") or ""
            branches.append((int(order) if order.isdigit() else len(branches), o, text))
        branches.sort(key=lambda x: x[0])
        conds = [(o, self.expression(text, at), text) for _, o, text in branches]
        exact = all(e is not None for _, e, _ in conds) and default is not None and len(conds) >= 1
        results: dict[int, str] = {}
        negations: list[dict[str, Any]] = []
        chain: list[tuple[dict[str, Any], str]] = []
        for o, e, text in conds:
            cond = e if e is not None else etlexpr.unread(text, "ssis:expression_unread")
            pred = cond
            for n in reversed(negations):
                pred = {"e": "binop", "op": "and", "left": n, "right": pred}
            results[id(o)] = b.add("filter", al, [up], predicate=pred, primitive="split_by_condition" if exact else None, candidates=None if exact else ["split_by_condition"])
            negations.append({"e": "unop", "op": "not", "operand": cond})
            chain.append((cond, o.get("name") or "output"))
        if default is not None:
            pred: dict[str, Any] | None = None
            for n in negations:
                pred = n if pred is None else {"e": "binop", "op": "and", "left": pred, "right": n}
            results[id(default)] = b.add("filter", al, [up], predicate=pred, primitive="split_by_condition" if exact else None, candidates=None if exact else ["split_by_condition"]) if pred else up  # type: ignore[assignment]
        # The flow unit: the first output whose condition holds takes the row.
        node: list[dict[str, Any]] = [{"op": "output", "at": al, "channel": "dataset", "value": {"e": "lit", "value": default.get("name") or "default", "type": "string"}}] if default is not None else []
        for cond, oname in reversed(chain):
            node = [{"op": "if", "at": al, "cond": cond, "then": [{"op": "output", "at": al, "channel": "dataset", "value": {"e": "lit", "value": oname, "type": "string"}}], **({"else": node} if node else {})}]
        self.flow += node
        self.lowered = self.lowered or bool(chain)
        return [results.get(id(o)) for o in outputs]

    def derived(self, c, at, al, cid, name, up) -> str | None:
        out = self.out
        out.construct(at, cid, True)
        items = []
        for ic in descendants(child(c, "inputs"), "inputColumn"):
            cp = comp_props(ic)
            text = cp.get("FriendlyExpression") or cp.get("Expression")
            target = (ic.get("cachedName") or "").lower()
            if text and target:
                e = self.expression(text, at)
                items.append({**etlexpr.classify(e, target), "effect": "replace"})
                self.flow.append({"op": "assign", "at": al, "target": {"e": "var", "scope": "column", "name": ic.get("cachedName")}, "value": e or etlexpr.unread(text, "ssis:expression_unread")})
        for o in children(child(c, "outputs"), "output"):
            if (o.get("isErrorOut") or "").lower() == "true":
                continue
            for oc in children(child(o, "outputColumns"), "outputColumn"):
                cp = comp_props(oc)
                text = cp.get("FriendlyExpression") or cp.get("Expression")
                target = (oc.get("name") or "").lower()
                if text and target:
                    e = self.expression(text, at)
                    items.append(etlexpr.classify(e, target))
                    self.flow.append({"op": "assign", "at": al, "target": {"e": "var", "scope": "column", "name": oc.get("name")}, "value": e or etlexpr.unread(text, "ssis:expression_unread")})
        self.lowered = self.lowered or bool(items)
        return self._project(al, up, items)
