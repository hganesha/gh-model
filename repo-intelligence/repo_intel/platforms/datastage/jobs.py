"""What a DataStage export declares: jobs, stages, links, data, transform shapes and behaviour.

Read from the record model of `dsx` (both export forms), everything at evidence `E1`:

- each job (`DSJOB`) is a `Job` (`ds:ParallelJob`, `ds:ServerJob`, `ds:SequenceJob`,
  `ds:MainframeJob` by its `JobType`), named by the job name, framework `datastage`; its
  parameters by name only (`attributes.parameters`), never their default values;
- each stage of a job is a `Task` (`ds:Stage`, or `ds:Activity` in a sequence) that the
  job CONTAINS; a link from stage A to stage B makes B DEPENDS_ON A (`via: link`, the link's
  name); a trigger between two activities does the same, with its condition type code;
- file stages read and write a `Dataset` named by its path, database stages a `Table`
  (`{datastore: default}`, lower case) named by the stage's table property or by the tables
  its SQL reads and writes (the shared scanner `sql_lineage.lineage`, never run). A name
  made of job parameters (`#SRC_DIR#/customers.txt`) is not resolved: the transform keeps
  `{SRC_DIR}/customers.txt`, is marked dynamic, and a diagnostic says so;
- one `transform` observation per parallel, server or mainframe job (language
  `datastage`), owned by the job: each stage's operation in link order (read, publish,
  filter, project, join, sort, deduplicate, aggregate, union, unpivot, ...), each citing the
  stage's record. Stages the pack cannot read are `platform` transforms naming their
  construct;
- one `behavior` unit (kind `flow`, language `datastage`) per transformer stage, owned by
  its Task: the implicit row loop over its primary input, stage variables, and per output
  link its constraint and the derivation of each column (`expr`); and one per job
  sequence, owned by the Job: its activities in trigger order, a job activity as a call of
  the job it runs, guarded by custom trigger expressions;
- a sequence's job activity RUNS the Job it names and a routine activity RUNS the routine
  (`declare=False`: linked only when the repository defines it);
- table definitions (`DSTABLEDEFS`) are `DataSchema`s CONTAINing their columns as `Field`s
  (`{table: "datastage:<category path>"}`, contract 10.1 item 6); routines are `Program`s
  whose source is counted, not lowered.

Commands of command activities, notification addresses, server names, connection
properties (server, user, password) and parameter values are never read.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.datastage import dsx
from repo_intel.platforms.datastage import expr as dsexpr
from repo_intel.platforms.datastage.vocabulary import stage_kind

P = Predicate
E1 = EvidenceClass.E1
FRAMEWORK = "datastage"
LANGUAGE = "datastage"
_JOB_TYPES = {"0": ("server", "ds:ServerJob"), "1": ("mainframe", "ds:MainframeJob"), "2": ("sequence", "ds:SequenceJob"), "3": ("parallel", "ds:ParallelJob")}
_STAGE_ID = re.compile(r"V\d+S\d+")
_PIN_ID = re.compile(r"(V\d+S\d+)P\d+")
_PARAM = re.compile(r"#\$?([A-Za-z_][\w.]*)#")
_FILE_PROPS = ("file", "filename", "dataset", "fileset", "lookupfileset", "filepath", "datasetname")
_TABLE_PROPS = ("tablename", "table", "tablenames")
_SELECT_PROPS = ("selectstatement", "userdefinedsql", "sql", "usersql", "query", "sqlstatement")
_WRITE_PROPS = ("insertstatement", "updatestatement", "deletestatement", "userdefinedsql", "usersql", "sql", "sqlstatement")
_SQL_TYPES = {"1": "char", "12": "varchar", "-1": "longvarchar", "-8": "nchar", "-9": "nvarchar", "-10": "longnvarchar", "2": "numeric", "3": "decimal",
              "4": "integer", "5": "smallint", "-6": "tinyint", "-5": "bigint", "6": "float", "7": "real", "8": "double", "-7": "bit", "9": "date",
              "91": "date", "10": "time", "92": "time", "11": "timestamp", "93": "timestamp", "-2": "binary", "-3": "varbinary", "-4": "longvarbinary"}
_TRIM = {"trim", "trimb", "trimf", "trimleadingtrailing", "strip"}
_CASE = {"upcase", "downcase"}
_DATES = {"stringtodate", "stringtotimestamp", "stringtotime"}
_CASTS = {"stringtodecimal", "decimaltostring", "stringtoint", "asinteger", "asfloat", "asdouble", "decimaltodecimal", "int32tostring", "stringtoustring", "ustringtostring", "datetostring", "timestamptostring"}
_DEFAULTS = {"nulltovalue", "nulltozero", "nulltoempty"}


def _strip_params(raw: str) -> tuple[str, bool]:
    """`#SRC_DIR#/customers.txt` as `{SRC_DIR}/customers.txt`, and whether any part is a parameter."""
    out, n = _PARAM.subn(lambda m: "{" + m.group(1) + "}", raw.strip())
    return out, bool(n) or "{" in out


def px_values(raw: str) -> list[str]:
    """The parts of a parallel stage property value, split at DataStage's control-character separators."""
    return [t.strip() for t in re.split(r"[\x00-\x1f]+", raw) if t.strip()]


def _first_line(text: str | None, limit: int = 200) -> str | None:
    if not text:
        return None
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:limit] or None


def _category(raw: str | None) -> str | None:
    if not raw:
        return None
    out = raw.replace("\\", "/").strip("/ ")
    return out or None


def xml_properties(text: str) -> dict[str, str]:
    """Leaf elements of a connector's XMLProperties, by lower-case tag, first occurrence."""
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip())
    if not body.startswith("<") or re.search(r"<!(?:DOCTYPE|ENTITY)", body, re.I):
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for el in root.iter():
        if isinstance(el.tag, str) and len(el) == 0 and el.text and el.text.strip():
            out.setdefault(el.tag.rsplit("}", 1)[-1].lower(), el.text.strip())
    return out


@dataclass
class _Pin:
    rec: dsx.Record
    stage: str  # owning stage's identifier
    name: str
    output: bool


@dataclass
class _Link:
    name: str
    src: str  # stage identifiers
    dst: str
    out_pin: _Pin | None
    in_pin: _Pin | None
    order: int


@dataclass
class _Stage:
    rec: dsx.Record
    name: str
    stype: str
    canonical: str | None
    family: str
    dialect: str | None
    ref: dict[str, Any] | None = None
    inputs: list[_Link] = field(default_factory=list)
    outputs: list[_Link] = field(default_factory=list)


class Reader:
    """Observations for one export: `span(a, b)` maps a value's offsets to a file span."""

    def __init__(self, path: str, blob: str, base, span: Callable[[int, int], Any], whole: Any, extractor: str):
        self.path, self.blob, self.base, self.span, self.whole, self.extractor = path, blob, base, span, whole, extractor
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self.jobs: list[str] = []

    # ------------------------------------------------------------------ emission

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.extractor, severity, message))

    def at(self, thing: Any) -> Any:
        if isinstance(thing, (dsx.Record, dsx.Value, dsx.Section)):
            return self.span(thing.start, thing.end)
        return self.whole

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}, E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def neutral(self, at, kind: K, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, at, kind, key, name, attrs or {}, E1))
        return self.base.ref(kind, key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    # ------------------------------------------------------------------ the export

    def run(self, export: dsx.Export) -> None:
        for section in export.sections:
            if section.kind == "job":
                self.job(section)
            elif section.kind == "table_definitions":
                for r in section.records:
                    self.table_definition(r)
            elif section.kind == "routines":
                for r in section.records:
                    self.routine(r)
            elif section.kind == "shared_container":
                self.construct(self.at(section), "ds:shared_container", False)
            elif section.kind == "compiled_job":
                self.construct(self.at(section), "ds:compiled_job", False)
            elif section.kind == "parameter_sets":
                for r in section.records or [None]:
                    self.construct(self.at(r or section), "ds:parameter_set", False)
            elif section.kind != "header":
                self.construct(self.at(section), "ds:export_other", False)

    # ------------------------------------------------------------------ metadata

    def table_definition(self, r: dsx.Record) -> None:
        ident = _category(r.ident or r.get("Name"))
        at = self.at(r)
        if not ident:
            self.construct(at, "ds:table_definition", False)
            return
        layout = f"datastage:{ident}"
        self.construct(at, "ds:table_definition", True)
        schema = self.entity(at, "ds:TableDefinition", {"kind": "table_definition", "name": layout}, posixpath.basename(ident),
                             {"table_name": r.get("Name"), "category": _category(r.get("Category"))})
        seen: set[str] = set()
        for i, col in enumerate(r.collection("Columns"), start=1):
            name = (dsx.lookup(col, "Name") or dsx.Value("", 0, 0)).text.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cat = self.at(dsx.lookup(col, "Name"))
            sql_type = (dsx.lookup(col, "SqlType") or dsx.Value("", 0, 0)).text.strip()
            attrs: dict[str, Any] = {"position": i, "type": _SQL_TYPES.get(sql_type, f"sql_type:{sql_type}" if sql_type else None)}
            for prop, out in (("Precision", "length"), ("Scale", "scale")):
                v = dsx.lookup(col, prop)
                if v is not None and re.fullmatch(r"\d{1,6}", v.text.strip()) and (out == "length" or v.text.strip() != "0"):
                    attrs[out] = int(v.text.strip())
            nullable = dsx.lookup(col, "Nullable")
            if nullable is not None and nullable.text.strip() in ("0", "1"):
                attrs["nullable"] = nullable.text.strip() == "1"
            keypos = dsx.lookup(col, "KeyPosition")
            if keypos is not None and re.fullmatch(r"\d+", keypos.text.strip()) and int(keypos.text.strip()) > 0:
                attrs["key"] = True
            f = self.entity(cat, "ds:Column", {"table": layout, "name": name}, name, attrs)
            self.relation(cat, schema, P.CONTAINS, f)

    def routine(self, r: dsx.Record) -> None:
        name = (r.get("Name") or "").strip()
        at = self.at(r)
        if not name:
            self.construct(at, "ds:routine", False)
            return
        self.construct(at, "ds:routine", True)
        code = r.get("RoutineType")
        self.entity(at, "ds:Routine", {"platform": "datastage", "name": name}, name,
                    {"routine_type_code": code if code and code.isdigit() else None, "category": _category(r.get("Category"))})
        source = r.value("Source", "Code", "Body", "SourceCode")
        if source is not None and source.text.strip():
            self.construct(self.at(source), "ds:routine_source", False)

    # ------------------------------------------------------------------ a job

    def job(self, section: dsx.Section) -> None:
        records = section.records
        root = next((r for r in records if r.type == "CJobDefn" or r.ident == "ROOT"), None)
        name = ((root.get("Name") if root else None) or section.ident or "").strip()
        at = self.at(root or section)
        if not name:
            self.construct(at, "ds:job", False)
            self.note("warning", "a job with no name: not read")
            return
        stages, pins = self.stages_and_pins(records)
        code = (root.get("JobType") if root else None) or ""
        if code.strip() in _JOB_TYPES:
            job_type, term = _JOB_TYPES[code.strip()]
        elif any(s.family.endswith("activity") for s in stages.values()):
            job_type, term = "sequence", "ds:SequenceJob"
        else:
            job_type, term = "parallel", "ds:ParallelJob"
        params: list[tuple[str, dsx.Value]] = []
        for sub in (root.collection("Parameters") if root else []):
            v = dsx.lookup(sub, "Name")
            if v is not None and re.fullmatch(r"\$?[A-Za-z_][\w.]{0,127}", v.text.strip()):
                params.append((v.text.strip(), v))
        key = {"framework": FRAMEWORK, "name": name}
        attrs = {"job_type": job_type, "category": _category(root.get("Category") if root else None),
                 "parameters": sorted({p for p, _ in params}), "description": _first_line(root.get("Description") if root else None)}
        job_ref = self.entity(at, term, key, name, attrs)
        self.jobs.append(name)
        self.construct(at, "ds:job", True)
        for _, v in params:
            self.construct(self.at(v), "ds:job_parameter", True)
        if root is not None:
            jc = root.value("JobControl")
            if jc is not None and jc.text.strip():
                self.construct(self.at(jc), "ds:job_control", False)
            for prop in ("BeforeSubr", "AfterSubr"):
                v = root.value(prop)
                if v is not None and v.text.strip() not in ("", "0"):
                    self.construct(self.at(v), "ds:before_after_routine", False)
        for r in records:
            if r.type == "CAnnotation":
                self.construct(self.at(r), "ds:annotation", False)

        links = self.links(stages, pins)
        names: dict[str, int] = {}
        for s in stages.values():
            n = names[s.name] = names.get(s.name, 0) + 1
            s.name = s.name if n == 1 else f"{s.name}~{n}"
            term_s = "ds:Activity" if job_type == "sequence" else "ds:Stage"
            sattrs: dict[str, Any] = {"stage_type": s.canonical or s.stype, "family": s.family, "dialect": s.dialect}
            s.ref = self.entity(self.at(s.rec), term_s, {"framework": FRAMEWORK, "job": name, "name": s.name}, s.name, sattrs)
            self.relation(self.at(s.rec), job_ref, P.CONTAINS, s.ref)
        for link in links:
            a, b = stages[link.src], stages[link.dst]
            pin = link.out_pin or link.in_pin
            q: dict[str, Any] = {"via": "trigger" if job_type == "sequence" else "link", "link": link.name}
            ctype = pin.rec.get("ConditionType") if pin else None
            if job_type == "sequence" and ctype and ctype.strip().isdigit():
                q["condition_type"] = int(ctype.strip())
            self.construct(self.at(pin.rec if pin else b.rec), "ds:link", True)
            self.relation(self.at(pin.rec if pin else b.rec), b.ref, P.DEPENDS_ON, a.ref, q)
        scope = dsexpr.Scope(params={p for p, _ in params})
        if job_type == "sequence":
            self.sequence(name, job_ref, stages, links, scope, at)
        else:
            _Flow(self, name, job_ref, stages, links, scope).run(at)

    def stages_and_pins(self, records: list[dsx.Record]) -> tuple[dict[str, _Stage], dict[str, _Pin]]:
        stages: dict[str, _Stage] = {}
        pins: dict[str, _Pin] = {}
        for r in records:
            m = _PIN_ID.fullmatch(r.ident)
            if m:
                out = "output" in r.type.lower()
                pins[r.ident] = _Pin(r, m.group(1), (r.get("Name") or r.ident).strip(), out)
                continue
            if not _STAGE_ID.fullmatch(r.ident) or r.type in ("CContainerView", "CAnnotation", "CJobDefn"):
                continue
            stype = (r.get("StageType") or r.type or "").strip()
            known = stage_kind(stype) or stage_kind(r.type)
            name = (r.get("Name") or r.ident).strip()
            if known:
                stages[r.ident] = _Stage(r, name, stype, known[0], known[1], known[2])
            else:
                stages[r.ident] = _Stage(r, name, stype, None, "unknown", None)
        # Pin direction from the stage's pin lists, when the stage gives them.
        for s in stages.values():
            for prop, out in (("InputPins", False), ("OutputPins", True)):
                for pid in (s.rec.get(prop) or "").split("|"):
                    if pid.strip() in pins:
                        pins[pid.strip()].output = out
        return stages, pins

    def links(self, stages: dict[str, _Stage], pins: dict[str, _Pin]) -> list[_Link]:
        found: dict[tuple[str, str, str], _Link] = {}
        order = {r: i for i, r in enumerate(pins)}
        for pid, pin in pins.items():
            partner = (pin.rec.get("Partner") or "").split("|")
            other = partner[0].strip() if partner and partner[0].strip() else None
            other_pin = pins.get(partner[1].strip()) if len(partner) > 1 else None
            if other is None or other not in stages or pin.stage not in stages or other == pin.stage:
                continue
            src, dst = (pin.stage, other) if pin.output else (other, pin.stage)
            out_pin, in_pin = (pin, other_pin) if pin.output else (other_pin, pin)
            name = (out_pin or in_pin or pin).name
            k = (src, dst, name)
            if k not in found:
                found[k] = _Link(name, src, dst, out_pin, in_pin, order[pid])
            else:
                found[k].out_pin = found[k].out_pin or out_pin
                found[k].in_pin = found[k].in_pin or in_pin
        links = sorted(found.values(), key=lambda link: link.order)
        for link in links:
            stages[link.src].outputs.append(link)
            stages[link.dst].inputs.append(link)
        for s in stages.values():  # in the order the stage lists its pins
            s.inputs.sort(key=lambda link: _pin_rank(s.rec.get("InputPins"), link.in_pin))
            s.outputs.sort(key=lambda link: _pin_rank(s.rec.get("OutputPins"), link.out_pin))
        return links

    # ------------------------------------------------------------------ sequences

    def sequence(self, job: str, job_ref, stages: dict[str, _Stage], links: list[_Link], scope: dsexpr.Scope, at) -> None:
        scope.activities = {s.name for s in stages.values()}
        body: list[dict[str, Any]] = []
        for s in _ordered(stages, links):
            sat = self.at(s.rec)
            span = [sat.start_byte, sat.end_byte]
            cid = f"ds:stage.{s.canonical}" if s.canonical else "ds:stage.unknown"
            stmt: dict[str, Any] | None = None
            modelled = s.canonical is not None and s.family != "command_activity"
            if s.family == "job_activity":
                target_v = s.rec.value("Jobname", "JobName")
                target = (target_v.text.strip() if target_v else "")
                literal, dynamic = _strip_params(target)
                if target and not dynamic and re.fullmatch(r"[A-Za-z_][\w.]{0,254}", target):
                    self.relation(self.at(target_v), s.ref, P.RUNS, self.base.ref(K.JOB, {"framework": FRAMEWORK, "name": target}, target, declare=False))
                    stmt = {"op": "call", "at": span, "call": {"e": "call", "callee": {"e": "name", "name": target, "kind": "job"}, "args": []}}
                else:
                    modelled = False
                    self.construct(sat, "ds:dynamic_name", False)
                    self.note("info", f"activity {s.name} of {job} runs a job named at run time: not resolved")
                    stmt = {"op": "platform", "at": span, "construct": cid}
            elif s.family == "routine_activity":
                target_v = s.rec.value("RoutineName", "Routine")
                target = target_v.text.strip() if target_v else ""
                if target and re.fullmatch(r"[A-Za-z_][\w.]{0,254}", target):
                    self.relation(self.at(target_v), s.ref, P.RUNS, self.base.ref(K.PROGRAM, {"platform": "datastage", "name": target}, target, declare=False))
                    stmt = {"op": "call", "at": span, "call": {"e": "call", "callee": {"e": "name", "name": target, "kind": "routine"}, "args": []}}
                else:
                    modelled = False
                    stmt = {"op": "platform", "at": span, "construct": cid}
            elif s.canonical == "CJSTerminatorActivity":
                stmt = {"op": "exit", "at": span}
            else:
                stmt = {"op": "platform", "at": span, "construct": cid}
            self.construct(sat, cid, modelled)
            # A custom trigger's expression guards the activity. When it has several incoming
            # triggers, any one of them starts it; a trigger of another condition type (OK,
            # failed, unconditional) is kept as a platform node naming its link.
            conds = []
            custom = False
            for link in s.inputs:
                pin = link.out_pin
                text = pin.rec.get("Expression", "ExpressionText", "Condition") if pin else None
                if text and text.strip():
                    custom = True
                    v = pin.rec.value("Expression", "ExpressionText", "Condition")
                    try:
                        conds.append(dsexpr.parse(text, scope))
                        self.construct(self.at(v), "ds:trigger_expression", True)
                    except dsexpr.ExprError:
                        conds.append({"e": "platform", "construct": "ds:expression_unparsed"})
                        self.construct(self.at(v), "ds:expression_unparsed", False)
                else:
                    code = (pin.rec.get("ConditionType") or "").strip() if pin else ""
                    conds.append({"e": "platform", "construct": "ds:link", "link": link.name, **({"condition_type": int(code)} if code.isdigit() else {})})
            if custom:
                cond = conds[0]
                for c in conds[1:]:
                    cond = {"e": "binop", "op": "or", "left": cond, "right": c}
                stmt = {"op": "if", "at": span, "cond": cond, "then": [stmt]}
            body.append(stmt)
        if body:
            from repo_intel.compile.behavior import behavior_observation

            self.items.append(behavior_observation(self.blob, at, job_ref, LANGUAGE, body, evidence_class=E1, kind="flow"))


def _pin_rank(listing: str | None, pin: _Pin | None) -> int:
    ids = [p.strip() for p in (listing or "").split("|")]
    if pin is not None and pin.rec.ident in ids:
        return ids.index(pin.rec.ident)
    return len(ids)


def _ordered(stages: dict[str, _Stage], links: list[_Link]) -> list[_Stage]:
    """Stages upstream first; ties and cycles in the order the export lists them."""
    pending = {k: {link.src for link in s.inputs if link.src != k} for k, s in stages.items()}
    order = list(stages)
    done: list[str] = []
    while pending:
        ready = [k for k in order if k in pending and not (pending[k] - set(done))]
        if not ready:
            ready = [next(k for k in order if k in pending)]
        k = ready[0]
        done.append(k)
        del pending[k]
    return [stages[k] for k in done]


# ------------------------------------------------------------------ data flow


class _Flow:
    """The transform shapes of one parallel, server or mainframe job, and its transformers' behaviour."""

    def __init__(self, reader: Reader, job: str, job_ref, stages: dict[str, _Stage], links: list[_Link], scope: dsexpr.Scope):
        from repo_intel.compile.transforms import Builder

        self.r, self.job, self.job_ref, self.stages, self.links, self.scope = reader, job, job_ref, stages, links, scope
        self.b = Builder()
        self.refs: dict[int, str] = {}  # id(link) -> the transform (or table) its rows come from

    def run(self, at) -> None:
        from repo_intel.compile.transforms import transform_observation

        for s in _ordered(self.stages, self.links):
            sat = self.r.at(s.rec)
            span = [sat.start_byte, sat.end_byte]
            cid = f"ds:stage.{s.canonical}" if s.canonical else "ds:stage.unknown"
            handler = getattr(self, f"stage_{s.family}", None)
            inputs = [self.refs.get(id(link)) for link in s.inputs]
            inputs = [i for i in inputs if i]
            before = len(self.b.items)
            try:
                modelled = handler(s, span, inputs) if handler else False
            except Exception as exc:  # one stage's fault must not lose the job
                self.r.note("warning", f"stage {s.name} of {self.job} not read: {type(exc).__name__}")
                modelled = False
            if any(id(link) not in self.refs for link in s.outputs) or (not s.outputs and inputs and len(self.b.items) == before):
                # what the stage's family did not cover: a platform transform keeps the flow whole
                ref = self.b.add("platform", span, inputs, construct=cid)
                for link in s.outputs:
                    self.refs.setdefault(id(link), ref)
            self.r.construct(sat, cid, bool(modelled) and s.canonical is not None)
        transforms = self.b.finish()
        if transforms:
            self.r.items.append(transform_observation(self.r.blob, at, self.job_ref, LANGUAGE, transforms, E1))

    # -- names and data

    def data(self, s: _Stage, at, kind: str, raw: str, predicate: Predicate) -> tuple[str, bool]:
        """The data name a stage reads or writes; declares it and the relation when it is literal."""
        if kind == "table":
            from repo_intel.extractors.sql_lineage import clean_name, table

            name, dynamic = _strip_params(raw)
            name = clean_name(name) if not dynamic else name.lower()
            if not dynamic:
                t = table(name)
                self.r.items.append(self.r.base.entity(self.r.blob, at, K.TABLE, t["natural_key"], name, {}, E1))
                self.r.relation(at, s.ref, predicate, t)
        else:
            name, dynamic = _strip_params(raw)
            if not dynamic:
                ref = self.r.neutral(at, K.DATASET, {"name": name}, name, {"format": "file"})
                self.r.relation(at, s.ref, predicate, ref)
        if dynamic:
            self.r.unresolved += 1
            self.r.construct(at, "ds:dynamic_name", False)
            verb = "reads" if predicate == P.READS_FROM else "writes"
            self.r.note("info", f"stage {s.name} of {self.job} {verb} a {'table' if kind == 'table' else 'file'} named by job parameters: not resolved")
        else:
            self.r.resolved += 1
        return name, dynamic

    def props(self, s: _Stage, pin: _Pin | None) -> tuple[dict[str, dsx.Value], dict[str, str]]:
        """Simple properties (pin first, then stage, then their Properties collections) and connector XML properties."""
        simple: dict[str, dsx.Value] = {}
        xml: dict[str, str] = {}
        for rec in [p.rec for p in (pin,) if p is not None] + [s.rec]:
            for k, v in rec.props.items():
                simple.setdefault(k.lower(), v)
            for sub in rec.collection("Properties"):
                n = dsx.lookup(sub, "Name")
                v = dsx.lookup(sub, "Value")
                if n is None or v is None:
                    continue
                if n.text.strip().lower() == "xmlproperties":
                    for k, val in xml_properties(v.text).items():
                        xml.setdefault(k, val)
                else:
                    simple.setdefault(n.text.strip().lower(), v)
        return simple, xml

    def values(self, s: _Stage, pin: _Pin | None, names: tuple[str, ...]) -> list[str]:
        """Every value of the named properties of a stage (and pin), parts split."""
        out: list[str] = []
        for rec in [p.rec for p in (pin,) if p is not None] + [s.rec]:
            for sub in rec.collection("Properties"):
                n = dsx.lookup(sub, "Name")
                v = dsx.lookup(sub, "Value")
                if n is not None and v is not None and n.text.strip().lower() in names:
                    parts = px_values(v.text)
                    if parts and parts[0].lower() == n.text.strip().lower():
                        parts = parts[1:]
                    out += parts[:1]
        return out

    def pin_span(self, pin: _Pin | None, default: list[int]) -> list[int]:
        if pin is None:
            return default
        at = self.r.at(pin.rec)
        return [at.start_byte, at.end_byte]

    def columns(self, pin: _Pin | None) -> list[dict[str, dsx.Value]]:
        return pin.rec.collection("Columns") if pin is not None else []

    def column_names(self, link: _Link) -> list[str]:
        for pin in (link.out_pin, link.in_pin):
            names = [(dsx.lookup(c, "Name") or dsx.Value("", 0, 0)).text.strip() for c in self.columns(pin)]
            names = [n for n in names if n]
            if names:
                return names
        return []

    # -- families

    def stage_file(self, s: _Stage, span, inputs) -> bool:
        ok = True
        for link in s.outputs:
            raw, v = self.file_name(s, link.out_pin)
            if raw is None:
                ok = False
                continue
            name, _ = self.data(s, self.r.at(v), "file", raw, P.READS_FROM)
            self.refs[id(link)] = self.b.add("read", self.pin_span(link.out_pin, span), [name])
        for link, ref in zip(s.inputs, [self.refs.get(id(i)) for i in s.inputs]):
            raw, v = self.file_name(s, link.in_pin)
            if raw is None or ref is None:
                ok = False
                continue
            name, _ = self.data(s, self.r.at(v), "file", raw, P.WRITES_TO)
            self.b.add("publish", self.pin_span(link.in_pin, span), [ref], output=name)
        return ok

    def file_name(self, s: _Stage, pin: _Pin | None) -> tuple[str | None, Any]:
        simple, xml = self.props(s, pin)
        for k in _FILE_PROPS:
            v = simple.get(k)
            if v is None:
                continue
            parts = px_values(v.text)
            if parts and parts[0].lower() == k:
                parts = parts[1:]
            path = next((p for p in parts if re.search(r"[/\\.#]", p) and not re.fullmatch(r"[\d.]+", p)), None)
            if path:
                return path, v
        for k in ("filename", "file"):
            if xml.get(k):
                return xml[k], pin.rec if pin else s.rec
        self.r.note("info", f"stage {s.name} of {self.job} names no file this pack reads")
        return None, None

    def stage_database(self, s: _Stage, span, inputs) -> bool:
        from repo_intel.compile.transforms import sql_query
        from repo_intel.extractors import sql_lineage

        ok = True
        dialect = sql_lineage.DIALECT.get(s.dialect or "sql")
        for link in s.outputs:
            simple, xml = self.props(s, link.out_pin)
            sql = next((xml[k] for k in _SELECT_PROPS if xml.get(k)), None) or next((simple[k].text for k in _SELECT_PROPS if k in simple and simple[k].text.strip()), None)
            table = next((xml[k] for k in _TABLE_PROPS if xml.get(k)), None) or next((simple[k].text for k in _TABLE_PROPS if k in simple and simple[k].text.strip()), None)
            at = self.r.at(link.out_pin.rec if link.out_pin else s.rec)
            pspan = self.pin_span(link.out_pin, span)
            if sql and re.search(r"\bselect\b", sql, re.I):
                n = len(self.b.items)
                result = sql_query(self.b, sql, pspan, dialect=dialect)
                ref = self.b.ensure(result, pspan) if result else (self.b.items[-1]["id"] if len(self.b.items) > n else None)
                found = sql_lineage.lineage(sql, dialect)
                for name, _, _ in found.reads:
                    self.data(s, at, "table", name, P.READS_FROM)
                if _PARAM.search(re.sub(r"'(?:[^']|'')*'", "''", sql)):  # a parameter outside string literals
                    self.r.unresolved += 1
                    self.r.construct(at, "ds:dynamic_name", False)
                    self.r.note("info", f"stage {s.name} of {self.job} reads with SQL that job parameters complete: tables they name are not resolved")
                if ref is None:
                    ok = False
                    continue
                self.refs[id(link)] = ref
            elif table:
                name, _ = self.data(s, at, "table", table, P.READS_FROM)
                self.refs[id(link)] = self.b.add("read", pspan, [name])
            else:
                ok = False
                self.r.note("info", f"stage {s.name} of {self.job} names no table this pack reads")
        for link in s.inputs:
            ref = self.refs.get(id(link))
            simple, xml = self.props(s, link.in_pin)
            at = self.r.at(link.in_pin.rec if link.in_pin else s.rec)
            statements = [xml[k] for k in _WRITE_PROPS if xml.get(k)] + [simple[k].text for k in _WRITE_PROPS if k in simple and simple[k].text.strip()]
            statements = [t for t in statements if re.search(r"\b(insert|update|delete|merge)\b", t, re.I)]
            table = next((xml[k] for k in _TABLE_PROPS if xml.get(k)), None) or next((simple[k].text for k in _TABLE_PROPS if k in simple and simple[k].text.strip()), None)
            mode = (xml.get("writemode") or (simple["writemode"].text if "writemode" in simple else "") or "").strip().lower()
            targets: list[str] = []
            if table:
                targets.append(self.data(s, at, "table", table, P.WRITES_TO)[0])
            for sql in statements:
                found = sql_lineage.lineage(sql, dialect)
                for name, _, _ in found.writes:
                    if name not in targets:
                        targets.append(self.data(s, at, "table", name, P.WRITES_TO)[0])
            if not targets or ref is None:
                ok = False
                continue
            kinds = {m.group(1).lower() for t in statements for m in [re.match(r"\s*(\w+)", t)] if m}
            fields: dict[str, Any] = {}
            if "merge" in kinds or {"insert", "update"} <= kinds or mode in ("upsert", "insert then update", "update then insert", "insert or update", "update or insert"):
                fields["primitive"] = "merge_into"
            elif kinds == {"delete"} or mode == "delete":
                fields["primitive"] = "apply_deletes"
            self.b.add("publish", self.pin_span(link.in_pin, span), [ref], output=targets[0], **fields)
        return ok

    def stage_span(self, s: _Stage) -> Any:
        """The span of a stage's record and all its pins' records."""
        recs = [s.rec] + [link.in_pin.rec for link in s.inputs if link.in_pin] + [link.out_pin.rec for link in s.outputs if link.out_pin]
        ats = [self.r.at(r) for r in recs]
        lo = min(a.start_byte for a in ats)
        hi = max(a.end_byte for a in ats)
        return self.r.span(lo, hi) if lo < hi else ats[0]

    def stage_transformer(self, s: _Stage, span, inputs) -> bool:
        from repo_intel.compile.transforms import emit_projection

        if not s.inputs or not inputs:
            return False
        primary = s.inputs[0]
        links = {link.name for link in s.inputs}
        stage_vars = [(dsx.lookup(v, "Name"), dsx.lookup(v, "Expression", "Derivation")) for v in s.rec.collection("StageVars") + s.rec.collection("StageVariables")]
        stage_vars = [(n, e) for n, e in stage_vars if n is not None and n.text.strip()]
        scope = dsexpr.Scope(links=links, stage_vars={n.text.strip() for n, _ in stage_vars}, params=self.scope.params)
        ok = True
        loop_body: list[dict[str, Any]] = []
        for n, e in stage_vars:
            target = {"e": "var", "scope": "stage", "name": n.text.strip()}
            value = self.expression(e, scope)
            ok = ok and value.get("e") != "platform"
            vat = self.r.at(e or n)
            loop_body.append({"op": "assign", "at": [vat.start_byte, vat.end_byte], "target": target, "value": value})
            self.r.construct(vat, "ds:stage_variable", True)
        ref = self.refs.get(id(primary)) or inputs[0]
        for link in s.inputs[1:]:  # server reference links: a lookup whose failure rule is not read
            other = self.refs.get(id(link))
            if other:
                ref = self.b.add("join", self.pin_span(link.in_pin, span), [ref, other], candidates=["join"])
        constrained = [link for link in s.outputs if link.out_pin and (link.out_pin.rec.get("Constraint") or "").strip()]
        in_columns = self.column_names(primary)
        for link in s.outputs:
            pin = link.out_pin
            pspan = self.pin_span(pin, span)
            r = ref
            out_stmts: list[dict[str, Any]] = []
            cond = None
            if pin is not None and (pin.rec.get("Constraint") or "").strip():
                cv = pin.rec.value("Constraint")
                cond = self.expression(cv, scope)
                self.r.construct(self.r.at(cv), "ds:constraint", cond.get("e") != "platform")
                r = self.b.add("filter", pspan, [r], predicate=cond, primitive="split_by_condition" if len(constrained) > 1 else None)
            elif pin is not None and (pin.rec.get("Reject") or "").strip() == "1":
                r = self.b.add("filter", pspan, [r])  # the rows no other output took
            items = []
            used: set[str] = set()
            for col in self.columns(pin):
                cn = dsx.lookup(col, "Name")
                dv = dsx.lookup(col, "Derivation")
                if cn is None or not cn.text.strip():
                    continue
                target_name = cn.text.strip()
                value = self.expression(dv, scope) if dv is not None and dv.text.strip() else {"e": "lit", "value": None, "type": "null"}
                ok = ok and value.get("e") != "platform"
                cat = self.r.at(dv or cn)
                self.r.construct(cat, "ds:derivation", value.get("e") != "platform")
                out_stmts.append({"op": "assign", "at": [cat.start_byte, cat.end_byte],
                                  "target": {"e": "member", "object": {"e": "var", "scope": "link", "name": link.name}, "name": target_name}, "value": value})
                item = classify(target_name, value, links)
                if item.get("source"):
                    used.add(item["source"])
                elif item["effect"] == "keep":
                    used.add(target_name)
                items.append(item)
            keep_only = bool(in_columns) and bool(set(in_columns) - used - {i["target"] for i in items})
            r = emit_projection(self.b, pspan, r, items, keep_only) if items else r
            self.refs[id(link)] = r
            out_stmts.append({"op": "output", "at": pspan, "channel": "dataset", "name": link.name})
            if cond is not None:
                loop_body.append({"op": "if", "at": pspan, "cond": cond, "then": out_stmts})
            else:
                loop_body += out_stmts
        whole = self.stage_span(s)
        body = [{"op": "loop", "kind": "implicit", "at": [whole.start_byte, whole.end_byte], "over": {"e": "var", "scope": "link", "name": primary.name}, "body": loop_body}]
        from repo_intel.compile.behavior import behavior_observation

        self.r.items.append(behavior_observation(self.r.blob, whole, s.ref, LANGUAGE, body, evidence_class=E1, kind="flow"))
        return ok

    def expression(self, v: dsx.Value | None, scope: dsexpr.Scope) -> dict[str, Any]:
        if v is None or not v.text.strip():
            return {"e": "lit", "value": None, "type": "null"}
        try:
            return dsexpr.parse(v.text, scope)
        except dsexpr.ExprError:
            self.r.construct(self.r.at(v), "ds:expression_unparsed", False)
            return {"e": "platform", "construct": "ds:expression_unparsed"}

    def keys(self, s: _Stage) -> list[str]:
        keys = []
        for k in self.values(s, None, ("key", "keys")):
            if re.fullmatch(r"[A-Za-z_][\w$#]*", k) and k not in keys:
                keys.append(k)
        return keys

    def stage_lookup(self, s: _Stage, span, inputs) -> bool:
        from repo_intel.compile.transforms import emit_projection

        if not s.inputs or not inputs:
            return False
        primary = s.inputs[0]
        ref = self.refs.get(id(primary)) or inputs[0]
        ok = True
        for link in s.inputs[1:]:
            other = self.refs.get(id(link))
            if not other:
                ok = False
                continue
            pairs, keys = [], []
            for col in self.columns(link.in_pin) or self.columns(link.out_pin):
                cn, dv = dsx.lookup(col, "Name"), dsx.lookup(col, "Derivation")
                if cn is None or dv is None:
                    continue
                m = re.fullmatch(rf"\s*{re.escape(primary.name)}\.([A-Za-z_]\w*)\s*", dv.text)
                if m:
                    pairs.append([m.group(1), cn.text.strip()])
                    keys.append(cn.text.strip())
            fail = (link.in_pin.rec.get("LookupFail", "LookupFailure", "IfNotFound") if link.in_pin else None) or ""
            jtype = {"continue": "left", "drop": "inner", "reject": "inner", "fail": "inner"}.get(fail.strip().lower())
            if jtype:
                ref = self.b.add("join", span, [ref, other], keys=keys, join={"type": jtype, "on": pairs}, primitive="join")
            else:
                ref = self.b.add("join", span, [ref, other], keys=keys, candidates=["join"])
        for link in s.outputs:
            items = []
            for col in self.columns(link.out_pin):
                cn, dv = dsx.lookup(col, "Name"), dsx.lookup(col, "Derivation")
                if cn is None or dv is None or not dv.text.strip():
                    continue
                items.append(classify(cn.text.strip(), self.expression(dv, dsexpr.Scope(links={i.name for i in s.inputs})), {i.name for i in s.inputs}))
            self.refs[id(link)] = emit_projection(self.b, span, ref, items, False) if items else ref
        return ok

    def stage_join(self, s: _Stage, span, inputs) -> bool:
        if len(inputs) < 2:
            return False
        keys = self.keys(s)
        op = " ".join(self.values(s, None, ("operator", "jointype"))).lower()
        jtype = next((t for k, t in (("leftouter", "left"), ("rightouter", "right"), ("fullouter", "full"), ("inner", "inner")) if k in op), None)
        ref = inputs[0]
        for other in inputs[1:]:
            if jtype and keys:
                ref = self.b.add("join", span, [ref, other], keys=keys, join={"type": jtype, "on": [[k, k] for k in keys]}, primitive="join")
            else:
                ref = self.b.add("join", span, [ref, other], keys=keys, candidates=["join"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return bool(keys)

    def stage_merge(self, s: _Stage, span, inputs) -> bool:
        if len(inputs) < 2:
            return False
        keys = self.keys(s)
        ref = self.b.add("join", span, inputs, keys=keys, candidates=["join"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return bool(keys)

    def stage_sort(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        order = []
        for sub in s.rec.collection("Properties"):
            n, v = dsx.lookup(sub, "Name"), dsx.lookup(sub, "Value")
            if n is None or v is None or n.text.strip().lower() != "key":
                continue
            parts = px_values(v.text)
            parts = parts[1:] if parts and parts[0].lower() == "key" else parts
            if parts and re.fullmatch(r"[A-Za-z_]\w*", parts[0]):
                desc = any(re.fullmatch(r"desc(?:ending)?", p, re.I) for p in parts[1:])
                order.append({"column": parts[0], "desc": desc})
        ref = self.b.add("sort", span, inputs[:1], order=order)
        for link in s.outputs:
            self.refs[id(link)] = ref
        return bool(order)

    def stage_dedup(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        keys = self.keys(s)
        ref = self.b.add("deduplicate", span, inputs[:1], keys=keys, primitive="dedupe_by_key" if keys else None, candidates=None if keys else ["dedupe_by_key"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return bool(keys)

    def stage_aggregate(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        keys = self.keys(s)
        ref = self.b.add("aggregate", span, inputs[:1], keys=keys, primitive="aggregate")
        for link in s.outputs:
            self.refs[id(link)] = ref
        return True

    def stage_funnel(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        ref = self.b.add("union", span, inputs, candidates=["union_by_name"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return True

    def stage_filter(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        wheres = self.values(s, None, ("where", "whereclause"))
        split = len(s.outputs) > 1
        for link in s.outputs:
            predicate = {"sql": wheres[0]} if len(wheres) == 1 and len(s.outputs) == 1 else None
            self.refs[id(link)] = self.b.add("filter", span, inputs[:1], predicate=predicate, primitive="split_by_condition" if split else None)
        return True

    def stage_switch(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        for link in s.outputs:
            self.refs[id(link)] = self.b.add("filter", span, inputs[:1], primitive="split_by_condition" if len(s.outputs) > 1 else None)
        return True

    def stage_copy(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        in_cols = self.column_names(s.inputs[0]) if s.inputs else []
        for link in s.outputs:
            out_cols = [(dsx.lookup(c, "Name") or dsx.Value("", 0, 0)).text.strip() for c in self.columns(link.out_pin)]
            dropped = [c for c in in_cols if out_cols and c not in out_cols]
            if dropped:
                self.refs[id(link)] = self.b.add("project", span, inputs[:1], columns={"drop": dropped}, primitive="drop_columns")
            else:
                self.refs[id(link)] = inputs[0]
        return True

    def stage_modify(self, s: _Stage, span, inputs) -> bool:
        from repo_intel.compile.transforms import column_item, emit_projection, keep_item

        if not inputs:
            return False
        ref = inputs[0]
        items, ok = [], True
        for spec in self.values(s, None, ("specification",)):
            spec = spec.strip().rstrip(";")
            m = re.fullmatch(r"(?i)(drop|keep)\s+(.+)", spec)
            if m:
                cols = [c.strip() for c in m.group(2).split(",") if re.fullmatch(r"[A-Za-z_]\w*", c.strip())]
                primitive, effect = ("drop_columns", {"drop": cols}) if m.group(1).lower() == "drop" else ("select_columns", {})
                ref = self.b.add("project", span, [ref], columns=effect or None, primitive=primitive)
                continue
            m = re.fullmatch(r"([A-Za-z_]\w*)(?::\s*[\w()\[\],. ]+?)?\s*=\s*(.+)", spec)
            if not m:
                ok = False
                continue
            target, value = m.group(1), m.group(2).strip()
            if re.fullmatch(r"[A-Za-z_]\w*", value):
                items.append(keep_item(value, target))
            elif re.match(r"(?i)handle_null\s*\(", value):
                items.append(column_item(target, re.findall(r"\(\s*([A-Za-z_]\w*)", value)[:1], "apply_defaults", single=(re.findall(r"\(\s*([A-Za-z_]\w*)", value) or [None])[0]))
            elif re.match(r"(?i)(?:date|timestamp|time)_from_\w+\s*\(", value):
                src = (re.findall(r"\(\s*([A-Za-z_]\w*)", value) or [None])[0]
                items.append(column_item(target, [src] if src else [], "parse_timestamp", single=src))
            elif re.match(r"(?i)\w+_from_\w+\s*\(", value):
                src = (re.findall(r"\(\s*([A-Za-z_]\w*)", value) or [None])[0]
                items.append(column_item(target, [src] if src else [], None, ["safe_cast"], single=src))
            else:
                items.append(column_item(target, re.findall(r"[A-Za-z_]\w*", value), "derive_column"))
        ref = emit_projection(self.b, span, ref, items, False) if items else ref
        if ref == inputs[0]:
            ref = self.b.add("project", span, [ref])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return ok

    def stage_pivot(self, s: _Stage, span, inputs) -> bool:
        if not inputs:
            return False
        ref = self.b.add("unpivot", span, inputs[:1], candidates=["unpivot"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return True

    def stage_change_apply(self, s: _Stage, span, inputs) -> bool:
        if len(inputs) < 2:
            return False
        ref = self.b.add("publish", span, inputs, keys=self.keys(s), candidates=["apply_deletes", "merge_into"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return True

    def stage_scd(self, s: _Stage, span, inputs) -> bool:
        ref = self.b.add("platform", span, inputs, construct="ds:stage.PxSCD", candidates=["apply_scd_type_2"])
        for link in s.outputs:
            self.refs[id(link)] = ref
        return True


def classify(target: str, value: dict[str, Any], links: set[str]) -> dict[str, Any]:
    """The column effect and primitive of a derivation `target = value` (see transforms.column_item).
    A column of one of the stage's input `links` taken as it is keeps (or renames) it."""
    from repo_intel.compile.transforms import column_item, is_flag_pair, keep_item

    sources = dsexpr.link_columns(value)

    def column(node: Any) -> str | None:
        if isinstance(node, dict) and node.get("e") == "member" and isinstance(node.get("object"), dict) and node["object"].get("scope") == "link":
            return str(node["name"])
        return None

    if column(value) and value["object"].get("name") in links:
        return keep_item(column(value), target)
    fn = value["callee"]["name"].lower() if value.get("e") == "call" and isinstance(value.get("callee"), dict) else None
    args = value.get("args", []) if fn else []
    first = column(args[0]) if args else None
    if fn in _TRIM and first and len(args) == 1:
        return column_item(target, sources, "trim_strings", single=first)
    if fn in _CASE and first and len(args) == 1:
        return column_item(target, sources, "normalize_case", single=first)
    if fn in _DEFAULTS and first and (len(args) == 1 or (len(args) == 2 and args[1].get("e") == "lit")):
        return column_item(target, sources, "apply_defaults", single=first)
    if fn in _DATES and first:
        return column_item(target, sources, "parse_timestamp", single=first)
    if fn in _CASTS and first:
        return column_item(target, sources, None, ["safe_cast"], single=first)
    if value.get("e") == "cond" and all(isinstance(value.get(k), dict) and value[k].get("e") == "lit" for k in ("then", "else")) and is_flag_pair([value["then"].get("value"), value["else"].get("value")]):
        return column_item(target, sources, "conditional_flag")
    if value.get("e") in ("binop", "unop") and value.get("op") in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "not"):
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")
