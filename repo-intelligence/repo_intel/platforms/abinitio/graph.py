"""Ab Initio graphs exported as text: the `mp` commands of a graph's deployed script.

A graph designed in the GDE is stored in a binary `.mp` file, which this pack does not
read. Deployed as a script, the same graph is a list of shell-interface commands, which the
owner commits under `.rkp/inputs/abinitio/`:

    mp job load_customers
    mp ifile in_customers file:/data/landing/customers.dat -metadata dml/customers.dml
    mp reformat clean -transform $AI_XFR/clean.xfr
    mp filter-by-expression active -expression "status == \\"A\\""
    mp sort srt -key "{cust_id}"
    mp dedup-sorted ddp -key "{cust_id}"
    mp output-table load -table DW.CUSTOMERS -dbc_file $AI_DB/dw.dbc
    mp straight-flow in_customers.read clean.in
    mp straight-flow clean.out active.in
    ...
    mp run

Read at evidence `E1`: `mp job` is a `Job` (`abi:Graph`, framework `abinitio`); each
component (`mp ifile`, `mp ofile` and the component commands this module lists) is a `Task`
(`abi:Component`) the job CONTAINS; each flow (`straight-flow`, `fan-in-flow`,
`fan-out-flow`, `all-to-all-flow`) makes the downstream component DEPENDS_ON the upstream
one, with the flow kind and ports. Input and output files are `Dataset`s named by their
path (`file:` removed); a path made of parameters (`$AI_SERIAL/customers.dat`) stays dynamic
as `{AI_SERIAL}/customers.dat`, with a diagnostic. Tables are named by `-table`, or by the
tables `-select` and `-sql` statements read and write (the shared scanner, never run). One
`transform` observation per graph (language `abinitio`) gives each component's operation in
flow order; a component whose logic is in an `.xfr` transform (`reformat`, `normalize`,
`scan`, ...) is a `platform` transform, since `.xfr` files are not read. Database
configuration files (`-dbc_file`), layouts, shell variables and other commands are never
read; other `mp` commands are counted.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate

P = Predicate
E1 = EvidenceClass.E1
FRAMEWORK = "abinitio"
LANGUAGE = "abinitio"

# component command -> (family, description)
COMPONENTS: dict[str, tuple[str, str]] = {
    "ifile": ("input_file", "an input file"),
    "ofile": ("output_file", "an output file"),
    "intermediate-file": ("output_file", "an intermediate file"),
    "input-table": ("input_table", "read a database table or query; the database configuration is never read"),
    "output-table": ("output_table", "load a database table"),
    "update-table": ("update_table", "update or insert rows of a database table"),
    "run-sql": ("run_sql", "run SQL statements"),
    "reformat": ("xfr", "change each record with a transform (.xfr, not read)"),
    "filter-by-expression": ("filter", "keep the records an expression selects"),
    "sort": ("sort", "sort by a key"),
    "sort-within-groups": ("sort", "sort within groups"),
    "dedup-sorted": ("dedup", "keep one record per key"),
    "rollup": ("aggregate", "aggregate records per key with a transform"),
    "aggregate": ("aggregate", "aggregate records per key"),
    "join": ("join", "join flows on a key with a transform"),
    "merge": ("union", "merge sorted flows"),
    "concatenate": ("union", "append flows"),
    "gather": ("union", "gather the partitions of flows"),
    "interleave": ("union", "interleave flows"),
    "replicate": ("pass", "copy a flow to several outputs"),
    "broadcast": ("pass", "copy a flow to every partition"),
    "partition-by-key": ("pass", "partition by key"),
    "partition-by-round-robin": ("pass", "partition by round robin"),
    "partition-by-expression": ("pass", "partition by an expression"),
    "departition": ("pass", "collect partitions"),
    "normalize": ("xfr", "make several records of one with a transform (.xfr, not read)"),
    "denormalize-sorted": ("xfr", "make one record of several with a transform (.xfr, not read)"),
    "scan": ("xfr", "running values with a transform (.xfr, not read)"),
    "lookup-file": ("other", "a lookup file used by transforms"),
    "run-program": ("other", "run a program; its command is never copied"),
    "custom": ("other", "a custom component"),
}
FLOWS = ("straight-flow", "fan-in-flow", "fan-out-flow", "all-to-all-flow")
_IGNORED = ("run", "layout", "ilayout", "olayout", "metadata", "parameter", "graph-parameter")
_NAME = re.compile(r"[A-Za-z_][\w.$-]{0,254}")
_VAR = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
_HASH_VAR = re.compile(r"#([A-Za-z_]\w*)#")


def looks_like_graph(text: str) -> bool:
    return re.search(r"(?m)^\s*mp\s+job\s+\S", text) is not None


@dataclass
class Command:
    words: list[str]
    start: int  # character offsets of the logical line
    end: int


def commands(text: str) -> list[Command]:
    """The `mp` commands of a script, continuation lines joined; other lines are skipped."""
    out: list[Command] = []
    lines = text.splitlines(keepends=True)
    pos = 0
    i = 0
    while i < len(lines):
        start = pos
        logical = ""
        while True:
            line = lines[i]
            pos += len(line)
            i += 1
            body = line.rstrip("\r\n")
            if body.endswith("\\") and i < len(lines):
                logical += body[:-1] + " "
                continue
            logical += body
            break
        stripped = logical.strip()
        if not stripped.startswith("mp ") or stripped.startswith("#"):
            continue
        try:
            words = shlex.split(stripped, comments=True, posix=True)
        except ValueError:
            words = stripped.split()
        if len(words) >= 2:
            out.append(Command(words[1:], start, start + len(logical.rstrip())))
    return out


def options(words: list[str]) -> dict[str, str]:
    """`-name value` pairs after a component's label (a flag with no value maps to "")."""
    out: dict[str, str] = {}
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            value = words[i + 1] if i + 1 < len(words) and not words[i + 1].startswith("-") else ""
            out.setdefault(w[1:].lower().replace("_", "-"), value)
            i += 2 if value else 1
        else:
            i += 1
    return out


def key_columns(raw: str | None) -> list[str]:
    """`{cust_id; order_date descending}` as its column names."""
    if not raw:
        return []
    out = []
    for part in re.split(r"[;,]", raw.strip().strip("{}")):
        words = part.split()
        if words and re.fullmatch(r"[A-Za-z_]\w*", words[0]) and words[0] not in out:
            out.append(words[0])
    return out


def descending(raw: str | None) -> list[bool]:
    return [bool(re.search(r"\bdesc(?:ending)?\b", part)) for part in re.split(r"[;,]", (raw or "").strip().strip("{}")) if part.split()]


def path_name(raw: str) -> tuple[str, bool]:
    """A file URL as a dataset name: `file:` and any `//host` removed (host names are never copied), parameters as `{NAME}`."""
    text = re.sub(r"^(?:file|mfile):(?://[^/]*)?", "", raw.strip())
    text, n = _VAR.subn(lambda m: "{" + m.group(1) + "}", text)
    return text, bool(n)


@dataclass
class _Comp:
    command: Command
    kind: str
    family: str
    label: str
    opts: dict[str, str]
    args: list[str]
    ref: dict[str, Any] | None = None
    inputs: list["_Flow"] = field(default_factory=list)
    outputs: list["_Flow"] = field(default_factory=list)


@dataclass
class _Flow:
    kind: str
    src: str
    dst: str
    src_port: str
    dst_port: str
    command: Command


class GraphReader:
    def __init__(self, path: str, blob: str, base, span, extractor: str):
        self.path, self.blob, self.base, self.span, self.extractor = path, blob, base, span, extractor
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.extractor, severity, message))

    def construct(self, at, cid: str, modelled: bool) -> None:
        from repo_intel.platforms import construct as construct_observation

        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        from repo_intel.platforms import platform_entity

        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}, E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def run(self, text: str) -> None:
        cmds = commands(text)
        job_name: str | None = None
        job_ref = None
        comps: dict[str, _Comp] = {}
        flows: list[_Flow] = []
        for cmd in cmds:
            at = self.span(cmd.start, cmd.end)
            verb = cmd.words[0].lower()
            if verb == "job":
                if job_ref is not None:
                    self._finish(job_name, job_ref, comps, flows)
                    comps, flows = {}, []
                name = cmd.words[1] if len(cmd.words) > 1 else ""
                if not _NAME.fullmatch(name) or _VAR.search(name):
                    self.construct(at, "abi:graph", False)
                    self.note("warning", "a graph whose name is not literal: not read")
                    job_name, job_ref = None, None
                    continue
                job_name = name
                job_ref = self.entity(at, "abi:Graph", {"framework": FRAMEWORK, "name": name}, name, {"source": "deployed_script"})
                self.construct(at, "abi:graph", True)
            elif job_ref is None:
                continue
            elif verb in FLOWS:
                if len(cmd.words) < 3:
                    self.construct(at, "abi:flow", False)
                    continue
                (a, ap), (b, bp) = (_port(cmd.words[1]), _port(cmd.words[2]))
                if a in comps and b in comps and a != b:
                    fl = _Flow(verb, a, b, ap, bp, cmd)
                    flows.append(fl)
                    comps[a].outputs.append(fl)
                    comps[b].inputs.append(fl)
                else:
                    self.construct(at, "abi:flow", False)
            elif verb in COMPONENTS:
                label = cmd.words[1] if len(cmd.words) > 1 else ""
                if not _NAME.fullmatch(label) or label in comps:
                    self.construct(at, f"abi:component.{verb}", False)
                    continue
                rest = cmd.words[2:]
                comps[label] = _Comp(cmd, verb, COMPONENTS[verb][0], label, options(rest), [w for w in rest if not w.startswith("-")][:1])
            elif verb in _IGNORED:
                continue
            else:
                self.construct(at, "abi:command_unknown", False)
        if job_ref is not None:
            self._finish(job_name, job_ref, comps, flows)

    def _finish(self, job: str, job_ref, comps: dict[str, _Comp], flows: list[_Flow]) -> None:
        from repo_intel.compile.transforms import Builder, transform_observation

        for c in comps.values():
            at = self.span(c.command.start, c.command.end)
            record_format = c.opts.get("metadata") or c.opts.get("dml")
            attrs = {"component": c.kind, "family": c.family,
                     "record_format": record_format if record_format and not _VAR.search(record_format) else None}
            c.ref = self.entity(at, "abi:Component", {"framework": FRAMEWORK, "job": job, "name": c.label}, c.label, attrs)
            self.relation(at, job_ref, P.CONTAINS, c.ref)
        for fl in flows:
            at = self.span(fl.command.start, fl.command.end)
            self.construct(at, "abi:flow", True)
            q = {"via": "flow", "flow": fl.kind, "from_port": fl.src_port or None, "to_port": fl.dst_port or None}
            self.relation(at, comps[fl.dst].ref, P.DEPENDS_ON, comps[fl.src].ref, {k: v for k, v in q.items() if v})
        b = Builder()
        refs: dict[int, str] = {}
        for c in _ordered(comps):
            at = self.span(c.command.start, c.command.end)
            span = [at.start_byte, at.end_byte]
            inputs = [refs[id(f)] for f in sorted(c.inputs, key=lambda f: f.dst_port) if id(f) in refs]
            cid = f"abi:component.{c.kind}"
            before = len(b.items)
            try:
                ok, ref = self._component(b, c, at, span, inputs)
            except Exception as exc:  # one component's fault must not lose the graph
                self.note("warning", f"component {c.label} of {job} not read: {type(exc).__name__}")
                ok, ref = False, None
            if ref is None and (c.outputs or (inputs and len(b.items) == before)):
                ref = b.add("platform", span, inputs, construct=cid)
            for f in c.outputs:
                if ref is not None:
                    refs[id(f)] = ref
            self.construct(at, cid, ok)
            if c.opts.get("transform"):
                self.construct(at, "abi:transform_ref", False)
        transforms = b.finish()
        if transforms:
            first = min(c.command.start for c in comps.values())
            last = max(c.command.end for c in comps.values())
            self.items.append(transform_observation(self.blob, self.span(first, last), job_ref, LANGUAGE, transforms, E1))

    def data(self, c: _Comp, at, kind: str, raw: str, predicate: Predicate) -> str:
        if kind == "table":
            from repo_intel.extractors.sql_lineage import clean_name, table

            raw = _HASH_VAR.sub(lambda m: "{" + m.group(1) + "}", raw)
            name, dynamic = path_name(raw) if _VAR.search(raw) else (raw, "{" in raw)
            if dynamic:
                name = name.strip()
            else:
                name = clean_name(name)
                t = table(name)
                self.items.append(self.base.entity(self.blob, at, K.TABLE, t["natural_key"], name, {}, E1))
                self.relation(at, c.ref, predicate, t)
        else:
            name, dynamic = path_name(raw)
            if not dynamic:
                ref = self.base.ref(K.DATASET, {"name": name}, name)
                self.items.append(self.base.entity(self.blob, at, K.DATASET, {"name": name}, name, {"format": "file"}, E1))
                self.relation(at, c.ref, predicate, ref)
        if dynamic:
            self.unresolved += 1
            self.construct(at, "abi:dynamic_name", False)
            verb = "reads" if predicate == P.READS_FROM else "writes"
            self.note("info", f"component {c.label} {verb} a {'table' if kind == 'table' else 'file'} named by parameters: not resolved")
        else:
            self.resolved += 1
        return name

    def _component(self, b, c: _Comp, at, span, inputs) -> tuple[bool, str | None]:
        from repo_intel.compile.transforms import sql_query
        from repo_intel.extractors import sql_lineage

        o = c.opts
        dialect = None
        if c.family == "input_file":
            raw = c.args[0] if c.args else o.get("url")
            if not raw:
                return False, None
            name = self.data(c, at, "file", raw, P.READS_FROM)
            return True, b.add("read", span, [name], **({"dynamic": True} if "{" in name else {}))
        if c.family == "output_file":
            raw = c.args[0] if c.args else o.get("url")
            if not raw or not inputs:
                return False, None
            name = self.data(c, at, "file", raw, P.WRITES_TO)
            return True, b.add("publish", span, inputs[:1], output=name, **({"dynamic": True} if "{" in name else {}))
        if c.family == "input_table":
            sql = _sql(o.get("select") or o.get("sql"))
            if sql and re.search(r"\bselect\b", sql, re.I):
                n = len(b.items)
                result = sql_query(b, sql, span, dialect=dialect)
                ref = b.ensure(result, span) if result else (b.items[-1]["id"] if len(b.items) > n else None)
                for name, _, _ in sql_lineage.lineage(sql, dialect).reads:
                    self.data(c, at, "table", name, P.READS_FROM)
                return ref is not None, ref
            if o.get("table"):
                name = self.data(c, at, "table", o["table"], P.READS_FROM)
                return True, b.add("read", span, [name], **({"dynamic": True} if "{" in name else {}))
            return False, None
        if c.family in ("output_table", "update_table"):
            if not o.get("table") or not inputs:
                return False, None
            name = self.data(c, at, "table", o["table"], P.WRITES_TO)
            fields = {"candidates": ["merge_into"]} if c.family == "update_table" else {}
            if "{" in name:
                fields["dynamic"] = True
            return True, b.add("publish", span, inputs[:1], output=name, **fields)
        if c.family == "run_sql":
            sql = _sql(o.get("sql") or o.get("select"))
            if not sql:
                return False, None
            found = sql_lineage.lineage(sql, dialect)
            for name, _, _ in found.reads:
                self.data(c, at, "table", name, P.READS_FROM)
            for name, _, _ in found.writes:
                self.data(c, at, "table", name, P.WRITES_TO)
            sql_query(b, sql, span, dialect=dialect)
            return bool(found.reads or found.writes), inputs[0] if inputs else None
        if not inputs:
            return False, None
        keys = key_columns(o.get("key"))
        if c.family == "filter":
            return True, b.add("filter", span, inputs[:1], primitive="split_by_condition" if len(c.outputs) > 1 else None)
        if c.family == "sort":
            desc = descending(o.get("key"))
            order = [{"column": k, "desc": desc[i] if i < len(desc) else False} for i, k in enumerate(keys)]
            return bool(order), b.add("sort", span, inputs[:1], order=order)
        if c.family == "dedup":
            return bool(keys), b.add("deduplicate", span, inputs[:1], keys=keys, primitive="dedupe_by_key" if keys else None, candidates=None if keys else ["dedupe_by_key"])
        if c.family == "aggregate":
            return True, b.add("aggregate", span, inputs[:1], keys=keys, primitive="aggregate")
        if c.family == "join":
            if len(inputs) < 2:
                return False, None
            jt = (o.get("join-type") or "").lower()
            jtype = {"inner": "inner", "full": "full", "outer": "full", "full-outer": "full"}.get(jt)
            if jtype and keys:
                return True, b.add("join", span, inputs, keys=keys, join={"type": jtype, "on": [[k, k] for k in keys]}, primitive="join")
            return bool(keys), b.add("join", span, inputs, keys=keys, candidates=["join"])
        if c.family == "union":
            return True, b.add("union", span, inputs, candidates=["union_by_name"])
        if c.family == "pass":
            return True, inputs[0]
        return False, None


def _sql(text: str | None) -> str | None:
    """SQL text with graph parameters (`$AI_SCHEMA`, `${RUN_DATE}`) as the scanner's `#NAME#` placeholders."""
    if not text:
        return None
    return _VAR.sub(lambda m: "#" + m.group(1) + "#", text)


def _port(word: str) -> tuple[str, str]:
    name, _, port = word.partition(".")
    return name, port


def _ordered(comps: dict[str, _Comp]) -> list[_Comp]:
    order = list(comps)
    pending = {k: {f.src for f in c.inputs if f.src != k} for k, c in comps.items()}
    done: list[str] = []
    while pending:
        ready = [k for k in order if k in pending and not (pending[k] - set(done))]
        k = ready[0] if ready else next(k for k in order if k in pending)
        done.append(k)
        del pending[k]
    return [comps[k] for k in done]
