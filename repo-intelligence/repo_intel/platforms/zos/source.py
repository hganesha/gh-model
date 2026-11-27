"""The zos pack's extractors: `zos.jcl` for JCL members and `zos.definitions` for the
resource definitions an owner commits.

**What a JCL file gives** (`zos.jcl`, `.jcl`, `.prc`, `.proc`; it supersedes `legacy.jcl`
and gives at least what that gave, with the same kinds and keys):

- Each JOB is a `Job` (`zos:Job`, `{framework: "jcl", name}`) with `attributes.member`, the
  file's stem upper-cased, as `legacy.jcl` gives it. When the member differs from the JOB
  name, `Job {framework: "jcl", name: <member>}` (`zos:Member`) is declared ALIAS_OF the job,
  so a scheduler that names the member links (contract 11.1 item 10). A cataloged PROC is a
  `Job` (`zos:Proc`) named by its PROC statement, else by the member; an in-stream PROC is a
  `Job` of its own name inside the job, which CONTAINS it.
- Each EXEC is a `Task` (`zos:Step`, `{framework, job, name}`; an unnamed step is
  `STEP<n>`, numbered as `legacy.jcl` numbers it) that the Job CONTAINS and that DEPENDS_ON
  the step before it in the same job or procedure. It RUNS the program it names (`Program
  {platform: "mainframe", name}`, not declared here) unless that is a system utility, or
  the procedure it calls. `COND`, the IF/THEN/ELSE constructs around it, the plan of a DB2
  program and the names of overridden symbolics and procedure steps are attributes.
- Symbolic parameters are resolved from the PROC statement's defaults, `SET` statements
  and the calling EXEC's overrides when each value is literal in the file, in the order
  z/OS applies them: a SET value, then the PROC default, then the EXEC override. A name
  with a symbolic that has no literal value keeps it as `{name}`, is marked
  `parameterized`, counted and reported; it is never guessed. A step that runs a program
  named by a symbolic or a referback is not linked.
- Each DD with a catalogued data set gives `Dataset {name}` (lower-cased, members and
  generations stripped, `gdg: true` for a generation) that the step READS_FROM or
  WRITES_TO by its DISP, as `legacy.jcl` decides: NEW, MOD and a (+n) generation write,
  anything else reads. Temporary (`&&X`) and system data sets and load libraries are not
  data. Referbacks (`*.STEP.DD`) take the data set of the DD they name; an unnamed DD
  continues a concatenation; `//procstep.ddname` overrides or adds to a procedure step.
  For each DD of a step that runs a program, a `dd_binding` observation `{task, program,
  dd, dataset, disp, job, step}` lets the linker join the program's own DD names to data
  sets (`link_dd_datasets`, contract 11.1 item 5). An IKJEFT01 step gives one for each
  program its SYSTSIN runs: the `RUN PROGRAM(P)` load module and each REXX exec or CLIST
  (`Program {platform: "tso"}`), since all of them see the step's DDs.
- A call of an in-stream procedure is expanded (contract 11.3 item 5): the procedure's steps
  are `Task {job: <procedure>}` under the procedure's Job, and the calling step carries the
  data sets, bindings and control cards of every procedure step, with the call's symbolics
  and DD overrides applied; a binding's `step` is `<calling step>.<procedure step>`. A
  cataloged procedure in another file is not read; the calling step RUNS it, and only the
  DDs the call itself states are bound.
- Control cards in-stream: IDCAMS `DEFINE CLUSTER`, `GDG`, `AIX` and `PATH` declare their
  data sets with organisation, keys and record size; `DELETE` and `REPRO` are writes and
  reads of the step. DFSORT and ICETOOL statements give transform shapes (`language
  "zos"`, contract 11.1 item 11). IKJEFT01's `SYSTSIN` gives the program `RUN PROGRAM`
  runs (and its plan), the REXX execs and CLISTs `%NAME`, `EX` and `EXEC` run (`Program
  {platform: "tso", name: <member>}`, contract 11.1 item 2), and BIND cards. DFHCSDUP's
  `SYSIN` is read as a CSD export. A control-card member a DD names in a library is
  another file and is counted, not read.
- Each job and procedure has one `flow` behaviour unit: its steps as calls, `COND` as a
  `zos:cond` platform node around a call, IF/THEN/ELSE as `if` with the relational
  expression lowered where it can be read.

**What a definition file gives** (`zos.definitions`, files under `.rkp/inputs/zos/` and
files with a control-card or definition suffix, recognised by content): CICS CSD exports
(transactions as `Endpoint`s their program EXPOSES, files as the data set their DSNAME
names), DCLGEN members (copybooks: `DataSchema {kind: "copybook", name: <member>}` with
`Field {table: "cobol:<member>"}`, contract 11.3 item 4), BIND cards, DB2 DDL outside
`.sql` files (tables and columns lower-cased, as `data.sql_ddl` keys them, contract 11.3
item 1), IDCAMS and DFSORT control-card members (shapes owned by the file, with the DD
names as `{placeholder}` inputs, since the JCL that runs the member binds them), and JCL
that sits there without a JCL suffix.

PARM values, accounting information, programmer names, NOTIFY user ids, OWNER and
QUALIFIER authorisation ids, CSD descriptions and in-stream data other than control cards
are never copied.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind, Predicate
from repo_intel.platforms.zos import cards, csd, db2, jcl
from repo_intel.platforms.zos.emit import E1, E2, Out, decode

K = EntityKind
P = Predicate
INPUT_DIR = ".rkp/inputs/zos/"
JCL_SUFFIXES = (".jcl", ".prc", ".proc")
DEFINITION_SUFFIXES = (".csd", ".dclgen", ".dcl", ".bind", ".bnd", ".ctl", ".cntl", ".sysin", ".ddl", ".db2", ".idcams", ".sort")

# legacy.jcl's lists, kept equal (tests check), plus utilities it did not know.
LIBRARY_DDS = frozenset({"STEPLIB", "JOBLIB", "SYSLIB", "SYSPROC", "SYSEXEC", "DBRMLIB", "SYSLMOD", "ISPPLIB", "ISPMLIB", "ISPSLIB", "ISPTLIB"})
LEGACY_SYSTEM_PROGRAMS = frozenset({"IEFBR14", "IEBGENER", "IEBCOPY", "IDCAMS", "SORT", "ICETOOL", "DFSORT", "SYNCSORT", "IKJEFT01", "IKJEFT1B", "IEBUPDTE", "ADRDSSU", "DSNUTILB", "IEHLIST",
                                    "IEBCOMPR", "DFHCSDUP", "IEBEDIT", "ICEGENER", "FTP"})
SYSTEM_PROGRAMS = LEGACY_SYSTEM_PROGRAMS | {"IKJEFT1A", "ICEMAN", "SYNCTOOL", "IEBPTPCH", "IEHMOVE", "IEHPROGM", "IEHINITT", "IEWL", "HEWL", "IEWBLINK", "HEWLH096", "IGYCRCTL",
                                            "ASMA90", "DSNHPC", "DSNUPROC", "DSNTIAUL", "DSNTEP2", "DSNTEP4", "DSNTIAD", "DFSRRC00", "IRXJCL", "BPXBATCH", "IEFBR15", "AMASPZAP", "IEBDG"}
SYSTEM_PREFIXES = ("SYS1.", "SYS2.", "CEE.", "IGY.", "DSN.", "CBC.", "ISP.")
SORT_PROGRAMS = frozenset({"SORT", "DFSORT", "ICEMAN", "SYNCSORT"})
ICETOOL_PROGRAMS = frozenset({"ICETOOL", "SYNCTOOL"})
TSO_PROGRAMS = frozenset({"IKJEFT01", "IKJEFT1A", "IKJEFT1B"})
EXEC_KEYWORDS = frozenset({"PGM", "PROC", "PARM", "PARMDD", "COND", "REGION", "REGIONX", "TIME", "ACCT", "ADDRSPC", "DPRTY", "DYNAMNBR", "PERFORM", "RD", "CCSID", "MEMLIMIT", "TVSMSG", "TVSAMCOM"})
_MEMBER = re.compile(r"^[A-Z#@$][A-Z0-9#@$]{0,7}$")
_SYMBOL = re.compile(r"&([A-Za-z@#$][A-Za-z0-9@#$]{0,7})(\.?)")
_GENERATION = re.compile(r"\(([+-]?\d+)\)$")
_PDS_MEMBER = re.compile(r"\(([A-Z#@$][A-Z0-9#@$]{0,7})\)$", re.I)


def member_of(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0].upper()


def _job_key(name: str) -> dict[str, Any]:
    return {"framework": "jcl", "name": name}


def _program(name: str, platform: str = "mainframe") -> dict[str, Any]:
    from repo_intel.extractors.base import ref

    return ref(K.PROGRAM, {"platform": platform, "name": name}, name, declare=False)


def _dataset_name(raw: str) -> str | None:
    from repo_intel.extractors.data_python import dataset_name

    return dataset_name(raw)


# ------------------------------------------------------------------------------- symbols

def subst(raw: str, symbols: dict[str, str | None]) -> tuple[str, list[str]]:
    """`raw` with each symbolic replaced by its literal value, or by `{name}` when it has none.
    Returns the text and the names that had no literal value. `&&` is kept as written."""
    out: list[str] = []
    missing: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw.startswith("&&", i):
            out.append("&&")
            i += 2
            m = re.match(r"[A-Za-z@#$][A-Za-z0-9@#$]*", raw[i:])
            if m:
                out.append(m.group())
                i += m.end()
            continue
        m = _SYMBOL.match(raw, i) if raw[i] == "&" else None
        if m:
            name = m.group(1).upper()
            value = symbols.get(name)
            if isinstance(value, str):
                out.append(value)
            else:
                out.append("{" + name.lower() + "}")
                missing.append(name)
            i = m.end()
        else:
            out.append(raw[i])
            i += 1
    return "".join(out), missing


def literal(value: str, symbols: dict[str, str | None] | None = None) -> str | None:
    """A symbolic's value as it substitutes, or None when it is not literal: a value that
    names other symbolics is literal when each of them has a literal value in `symbols`."""
    v = jcl.unquote(value)
    if "&" not in v:
        return v
    if "&&" in v:
        return None
    out, missing = subst(v, symbols or {})
    return None if missing else out


def settle(raw: dict[str, str], base: dict[str, str | None]) -> dict[str, str | None]:
    """`base` with each value of `raw` substituted, a value naming other symbolics taking
    theirs once they are literal; a value left with a symbolic that has none is None."""
    found: dict[str, str | None] = dict(base)
    for key in raw:
        found[key] = None
    for _ in range(len(raw) + 1):
        changed = False
        for key in sorted(raw):
            v = literal(raw[key], {k: x for k, x in found.items() if k != key})
            if v is not None and found.get(key) != v:
                found[key] = v
                changed = True
        if not changed:
            break
    return found


def proc_defaults(proc: jcl.Unit, symbols: dict[str, str | None] | None = None) -> dict[str, str | None]:
    return settle({p.key: p.value for p in proc.params if p.key}, symbols or {}) if proc.kind == "proc" else {}


# ------------------------------------------------------------------------------- IF expressions

_REL = {"EQ": "==", "=": "==", "NE": "!=", "¬=": "!=", "^=": "!=", "\\=": "!=", "GT": ">", ">": ">", "GE": ">=", ">=": ">=", "LT": "<", "<": "<", "LE": "<=", "<=": "<=",
        "NG": "<=", "¬>": "<=", "^>": "<=", "NL": ">=", "¬<": ">=", "^<": ">="}
_IF_TOKEN = re.compile(r"\s*(¬=|\^=|\\=|¬>|\^>|¬<|\^<|>=|<=|[()=<>&|¬^]|[A-Za-z0-9@#$.]+)")


def if_expression(text: str) -> dict[str, Any]:
    """A JCL IF relational expression as a behavioural-IR expression; the text as a
    `zos:if` platform expression when it does not parse."""
    fallback = {"e": "platform", "construct": "zos:if", "text": text}
    toks: list[str] = []
    i = 0
    while i < len(text):
        m = _IF_TOKEN.match(text, i)
        if not m:
            if text[i:].strip() == "":
                break
            return fallback
        toks.append(m.group(1))
        i = m.end()
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def operand() -> dict[str, Any] | None:
        nonlocal pos
        t = peek()
        if t is None:
            return None
        up = t.upper()
        if t == "(":
            pos += 1
            inner = logical()
            if peek() != ")":
                return None
            pos += 1
            return inner
        if up in ("NOT", "¬", "^"):
            pos += 1
            inner = operand()
            return None if inner is None else {"e": "unop", "op": "not", "operand": inner}
        pos += 1
        if re.fullmatch(r"\d+", t):
            return {"e": "lit", "value": int(t), "type": "number"}
        if up in ("TRUE", "FALSE"):
            return {"e": "lit", "value": up == "TRUE", "type": "boolean"}
        if re.fullmatch(r"[SU][0-9A-F]{3,4}", up) and pos >= 2 and toks[pos - 2] in ("=", "EQ", "NE", "¬="):
            return {"e": "lit", "value": up, "type": "string"}
        parts = up.split(".")
        if not all(re.fullmatch(r"[A-Z@#$][A-Z0-9@#$]*", p) for p in parts):
            return None
        node: dict[str, Any] = {"e": "var", "scope": "job" if len(parts) == 1 else "step", "name": parts[0]}
        for p in parts[1:]:
            node = {"e": "member", "object": node, "name": p}
        return node

    def comparison() -> dict[str, Any] | None:
        nonlocal pos
        left = operand()
        t = peek()
        if left is not None and t is not None and t.upper() in _REL:
            pos += 1
            right = operand()
            if right is None:
                return None
            return {"e": "binop", "op": _REL[t.upper()], "left": left, "right": right}
        return left

    def logical() -> dict[str, Any] | None:
        nonlocal pos
        left = comparison()
        while left is not None and peek() is not None and peek().upper() in ("AND", "&", "OR", "|"):
            op = "and" if peek().upper() in ("AND", "&") else "or"
            pos += 1
            right = comparison()
            if right is None:
                return None
            left = {"e": "binop", "op": op, "left": left, "right": right}
        return left

    expr = logical()
    return expr if expr is not None and pos == len(toks) else fallback


# ------------------------------------------------------------------------------- JCL


class Symbols:
    """The symbols in force at each statement of a job or procedure: `base` (the PROC defaults,
    or a call's resolved values), then each SET before the statement, applied in order.

    Statements are asked for in file order, so each SET is applied once: the dict `at`
    returns is updated in place by the next call and is read, never kept, by the caller.
    Asking for an earlier offset starts again from `base`."""

    def __init__(self, u: jcl.Unit, base: dict[str, str | None]):
        self.sets = [(st.start, p) for st, p in u.sets if p.key]
        self.base = base
        self.found: dict[str, str | None] = dict(base)
        self.next = 0
        self.offset = -1

    def at(self, offset: int) -> dict[str, str | None]:
        if offset < self.offset:
            self.found, self.next = dict(self.base), 0
        self.offset = offset
        while self.next < len(self.sets) and self.sets[self.next][0] < offset:
            p = self.sets[self.next][1]
            self.found[p.key] = literal(p.value, self.found)
            self.next += 1
        return self.found


class _Context:
    """Where a step's data sets are emitted: the task that carries them, the symbols in force,
    the job and step names of the `via` qualifier, and the DDs seen so far (for referbacks)."""

    def __init__(self, task: dict[str, Any], symbols: dict[str, str | None], job: str, step: str, procstep: str | None = None):
        self.task, self.symbols, self.job, self.step, self.procstep = task, symbols, job, step, procstep


class JclReader:
    def __init__(self, out: Out, path: str, text: str):
        self.out, self.path, self.text = out, path, text
        self.member = member_of(path)
        # (job/step label, dd) -> the step's context and the data sets of that DD (more than
        # one for a concatenation). A later step of the same label replaces the entry, so a
        # referback names the latest step of that name, and a job that repeats a step label
        # does not make each referback re-emit every earlier step's data sets.
        self.history: dict[tuple[str, str], tuple[_Context, list[tuple[str, dict[str, Any]]]]] = {}
        self._called: dict[int, frozenset[str]] = {}

    # -- entry
    def read(self) -> bool:
        parsed = jcl.parse(self.text, self.member if _MEMBER.match(self.member) else None)
        out = self.out
        for sp in parsed.specials:
            out.construct((sp.start, sp.end), sp.kind, False)
        for st in parsed.orphans:
            if st.op == "DD":
                lib = (st.name or "").upper() in LIBRARY_DDS
                out.construct((st.start, st.end), "dd_library" if lib else "dd_other", False)
            elif st.op in ("INCLUDE", "JCLLIB", "SET", "IF", "ELSE", "ENDIF", "PEND", "EXEC"):
                out.construct((st.start, st.end), {"INCLUDE": "include", "JCLLIB": "jcllib", "SET": "set", "EXEC": "exec_pgm"}.get(st.op, "other_statement"), False)
            else:
                out.construct((st.start, st.end), "other_statement", False)
        if not parsed.units:
            return False
        names = [u.name for u in parsed.units]
        for u in parsed.units:
            # A referback names a DD of the same job: start each job's history afresh, so a
            # file holding two jobs of one name does not join them.
            self.history = {}
            self.unit(u, None, alias=(u is parsed.units[0] and self.member not in names))
        return True

    # -- jobs and procedures
    def unit(self, u: jcl.Unit, parent: jcl.Unit | None, alias: bool = False) -> None:
        out = self.out
        head = (u.stmt.start, u.stmt.end)
        term = "zos:Job" if u.kind == "job" else "zos:Proc"
        attrs: dict[str, Any] = {"kind": u.kind, "member": self.member}
        if u.in_stream:
            attrs["in_stream"] = True
            out.construct(head, "in_stream_proc", True)
        else:
            out.construct(head, u.kind, True)
        cond = u.param("COND") if u.kind == "job" else None
        if cond is not None:
            attrs["cond"] = cond.value
            out.construct((cond.start, cond.end), "cond", True)
        if u.includes:
            attrs["includes"] = [m for _, m in u.includes]
            for st, _ in u.includes:
                out.construct((st.start, st.end), "include", True)
        if u.jcllib:
            attrs["jcllib"] = [lib for _, libs in u.jcllib for lib in libs]
            for st, _ in u.jcllib:
                out.construct((st.start, st.end), "jcllib", True)
        symbols = sorted({p.key for _, p in u.sets if p.key} | ({p.key for p in u.params if p.key} if u.kind == "proc" else set()))
        if symbols:
            attrs["symbols"] = symbols
        for st, _ in u.sets:
            out.construct((st.start, st.end), "set", True)
        if u.procs:
            attrs["in_stream_procs"] = sorted(u.procs)
        for st in u.others:
            out.construct((st.start, st.end), "output" if st.op == "OUTPUT" else "other_statement", False)
        me = out.entity(head, term, K.JOB, _job_key(u.name), u.name, attrs)
        if parent is not None:
            out.relation(head, out.base.ref(K.JOB, _job_key(parent.name), parent.name), P.CONTAINS, me)
        if alias and _MEMBER.match(self.member) and self.member != u.name:
            m = out.entity(head, "zos:Member", K.JOB, _job_key(self.member), self.member, {"kind": "member", "member": self.member, "job": u.name})
            out.relation(head, m, P.ALIAS_OF, me)
            out.construct(head, "member_alias", True)
        for proc in u.procs.values():
            self.unit(proc, u)
        data_here = not (u.in_stream and parent is not None and u.name in self.called(parent))
        previous = None
        in_force = Symbols(u, proc_defaults(u))  # the PROC defaults and the SET values before each step
        job_sets = Symbols(u, {})  # the SET values alone, which a call of an in-stream procedure starts from
        for step in u.steps:
            symbols = in_force.at(step.stmt.start)
            task = self.step(u, step, previous, symbols)
            previous = task
            programs = self.programs(step, symbols)
            if step.pgm is None and step.proc is not None:
                target = u.procs.get(step.proc.value.upper())
                if target is not None:
                    self.expand(step, task, target, job_sets.at(step.stmt.start), u.name)
                    continue
                self.data(_Context(task, symbols, u.name, step.name), step, step.dds + step.overrides, [], bind=False)
                continue
            if data_here:
                self.data(_Context(task, symbols, u.name, step.name), step, step.dds, programs, bind=True)
        self.flow(u, me)

    def called(self, parent: jcl.Unit) -> frozenset[str]:
        """The procedures a job's steps call, found once per job."""
        found = self._called.get(id(parent))
        if found is None:
            found = self._called[id(parent)] = frozenset(s.proc.value.upper() for s in parent.steps if s.pgm is None and s.proc is not None)
        return found

    # -- steps
    def step(self, u: jcl.Unit, step: jcl.Step, previous: dict[str, Any] | None, symbols: dict[str, str | None]) -> dict[str, Any]:
        out = self.out
        at = (step.stmt.start, step.stmt.end)
        attrs: dict[str, Any] = {}
        pgm, proc = step.pgm, step.proc if step.pgm is None else None
        runs: dict[str, Any] | None = None
        if pgm is not None:
            raw = pgm.value.upper()
            name, missing = subst(raw, symbols) if "&" in raw else (raw, [])
            where = (pgm.start, pgm.end)
            if raw.startswith("*") or missing or not _MEMBER.match(name):
                attrs["program"] = raw
                attrs["program_dynamic"] = True
                out.construct(where, "exec_dynamic", False)
                if missing:
                    out.construct(where, "symbolic", False)
                out.info(f"step {u.name}.{step.name}: the program is not literal ({raw[:40]}): not linked")
            else:
                attrs["program"] = name
                if "&" in raw:
                    out.construct(where, "symbolic", True)
                if name in SYSTEM_PROGRAMS:
                    attrs["utility"] = True
                    out.construct(where, "exec_utility", True)
                else:
                    runs = _program(name)
                    out.construct(where, "exec_pgm", True)
        elif proc is not None:
            name = proc.value.upper()
            attrs["procedure"] = name
            if _MEMBER.match(name):
                runs = out.base.ref(K.JOB, _job_key(name), name, declare=False)
                out.construct((proc.start, proc.end), "exec_proc", True)
            else:
                out.construct((proc.start, proc.end), "exec_dynamic", False)
            overridden = sorted({p.key for p in step.params[1:] if p.key and p.key not in EXEC_KEYWORDS and "." not in p.key})
            if overridden:
                attrs["symbols"] = overridden
            procsteps = sorted({p.key for p in step.params if p.key and "." in p.key} | {f"{d.procstep}.{d.ddname}" for d in step.overrides})
            if procsteps:
                attrs["overrides"] = procsteps
            for p in step.params:
                if p.key and "." in p.key:
                    out.construct((p.start, p.end), "exec_override", True)
        for p in step.params:
            if p.key == "PARM" or (p.key or "").startswith("PARM."):
                out.construct((p.start, p.end), "exec_parm", False)
        cond = step.param("COND")
        if cond is not None:
            attrs["cond"] = cond.value
            out.construct((cond.start, cond.end), "cond", True)
        if step.conditions:
            attrs["conditions"] = step.conditions
        # IKJEFT01: the programs, execs and binds in SYSTSIN; DFHCSDUP: the CSD in SYSIN.
        program = attrs.get("program")
        if program in TSO_PROGRAMS:
            self.tso(u, step, attrs)
        tso_runs, tso_plans = attrs.pop("_runs", []), attrs.pop("_plans", [])
        task = out.entity(at, "zos:Step", K.TASK, {"framework": "jcl", "job": u.name, "name": step.name}, step.name, attrs)
        out.relation(at, out.base.ref(K.JOB, _job_key(u.name), u.name), P.CONTAINS, task)
        if previous is not None:
            out.relation(at, task, P.DEPENDS_ON, previous)
        if runs is not None:
            named = pgm if pgm is not None else proc
            out.relation((named.start, named.end), task, P.RUNS, runs)
        for target, where in tso_runs:
            out.relation(where, task, P.RUNS, target)
        for plan, where in tso_plans:
            out.relation(where, task, P.DEPENDS_ON, _program(plan, "db2-plan"), {"via": "plan"})
        if program == "DFHCSDUP":
            sysin = next((d for d in step.dds if d.ddname == "SYSIN" and d.stmt.data is not None), None)
            if sysin is not None:
                read_csd(out, sysin.stmt.data.lines)
        return task

    def programs(self, step: jcl.Step, symbols: dict[str, str | None]) -> list[dict[str, Any]]:
        """The programs whose DD names a step's DDs bind: its PGM, or what IKJEFT01 runs."""
        pgm = step.pgm
        if pgm is None:
            return []
        name, missing = subst(pgm.value.upper(), symbols)
        if missing or not _MEMBER.match(name):
            return []
        if name in TSO_PROGRAMS:
            return [_program(n, platform) for platform, n in self.tso_programs(step)]
        return [] if name in SYSTEM_PROGRAMS else [_program(name)]

    def tso_programs(self, step: jcl.Step) -> list[tuple[str, str]]:
        """(platform, name) of each program an IKJEFT01 step's in-stream SYSTSIN runs: a
        `RUN PROGRAM(P)` load module (`mainframe`) and a REXX exec or CLIST run by `%NAME`,
        `EX` or `EXEC` (`tso`, contract 11.1 item 2). The step's DDs are allocated for all of
        them, so each gets the step's `dd_binding`s."""
        sysin = next((d for d in step.dds if d.ddname == "SYSTSIN" and d.stmt.data is not None), None)
        if sysin is None:
            return []
        found: set[tuple[str, str]] = set()
        for c in cards.commands(sysin.stmt.data.lines, "tso"):
            if c.verb == "RUN":
                prog = cards.value_text(cards.keyword(c.items(), "PROGRAM", "PROG"))
                if prog and _MEMBER.match(prog.upper()):
                    found.add(("mainframe", prog.upper()))
            elif c.verb.startswith("%") or c.verb in ("EX", "EXEC"):
                name = tso_exec(c)
                if name:
                    found.add(("tso", name))
        return sorted(found)

    def tso(self, u: jcl.Unit, step: jcl.Step, attrs: dict[str, Any]) -> None:
        out = self.out
        sysin = next((d for d in step.dds if d.ddname == "SYSTSIN"), None)
        if sysin is None:
            return
        if sysin.stmt.data is None:
            dsn = sysin.param("DSN") or sysin.param("DSNAME")
            if dsn is not None:
                out.construct((dsn.start, dsn.end), "control_member", False)
            return
        runs, plans = [], []
        cmds = cards.commands(sysin.stmt.data.lines, "tso")
        bound, rest = db2.binds(cmds)
        for b in bound:
            emit_bind(out, b)
        for c in rest:
            found = c.items()
            verb = c.verb
            if verb == "RUN":
                prog_item = cards.keyword(found, "PROGRAM", "PROG")
                prog = cards.value_text(prog_item)
                if prog and _MEMBER.match(prog.upper()):
                    runs.append((_program(prog.upper()), (prog_item.start, prog_item.end)))
                    out.construct((prog_item.start, prog_item.end), "tso_run_program", True)
                else:
                    out.construct((c.start, c.end), "tso_run_program", False)
                plan_item = cards.keyword(found, "PLAN")
                plan = cards.value_text(plan_item)
                if plan and _MEMBER.match(plan.upper()):
                    attrs["plan"] = plan.upper()
                    plans.append((plan.upper(), (plan_item.start, plan_item.end)))
                    out.construct((plan_item.start, plan_item.end), "tso_plan", True)
            elif verb in ("DSN", "END"):
                out.construct((c.start, c.end), "tso_dsn", False)
            elif verb.startswith("%") or verb in ("EX", "EXEC"):
                name = tso_exec(c)
                if name:
                    runs.append((_program(name, "tso"), (c.start, c.end)))
                    out.construct((c.start, c.end), "tso_exec", True)
                    attrs.setdefault("execs", []).append(name)
                else:
                    out.construct((c.start, c.end), "tso_exec", False)
                    out.info(f"step {u.name}.{step.name}: an exec is run from a data set with no member: not linked")
            else:
                out.construct((c.start, c.end), "tso_command", False)
        attrs["_runs"] = runs
        attrs["_plans"] = plans

    def expand(self, call: jcl.Step, task: dict[str, Any], proc: jcl.Unit, job_syms: dict[str, str | None], job: str) -> None:
        """A call of an in-stream procedure: each procedure step's data on the calling task."""
        # The values in force: the job's SET values, the PROC defaults, the EXEC overrides,
        # each later one replacing an earlier; a value that names other symbolics takes
        # theirs (a default HLQ=&ENV, an override CYCLE=&RUN).
        raw = {p.key: p.value for p in proc.params if p.key}
        raw.update({p.key: p.value for p in call.params[1:] if p.key and p.key not in EXEC_KEYWORDS and "." not in p.key})
        resolved = settle(raw, job_syms)
        in_force = Symbols(proc, resolved)
        by_step: dict[str, list[jcl.DD]] = {}
        for d in call.overrides:
            by_step.setdefault(d.procstep or "", []).append(d)
        for i, ps in enumerate(proc.steps):
            symbols = in_force.at(ps.stmt.start)
            overrides = by_step.get(ps.name, []) + (list(call.dds) if i == 0 else [])
            dds = merge(ps.dds, overrides)
            self.data(_Context(task, symbols, job, f"{call.name}.{ps.name}", ps.name), ps, dds, self.programs(ps, symbols), bind=True)

    # -- data sets
    def data(self, ctx: _Context, step: jcl.Step, dds: list[jcl.DD], programs: list[dict[str, Any]], bind: bool) -> None:
        out = self.out
        bound: dict[str, list[str]] = {}  # dd -> data set names as written lower-cased (temporary ones included), for control cards
        for dd in dds:
            names = self.dd(ctx, step, dd, programs if bind else [])
            bound.setdefault(dd.ddname, []).extend(names)
        pgm = step.pgm
        program = subst(pgm.value.upper(), ctx.symbols)[0] if pgm is not None else None
        if program in SORT_PROGRAMS:
            sort_step(out, ctx.task, step, dds, bound)
        elif program in ICETOOL_PROGRAMS:
            icetool_step(out, ctx.task, step, dds, bound)
        elif program == "IDCAMS":
            idcams_step(out, ctx.task, step, dds, bound)

    def dd(self, ctx: _Context, step: jcl.Step, dd: jcl.DD, programs: list[dict[str, Any]]) -> list[str]:
        out = self.out
        st = dd.stmt
        at = (st.start, st.end)
        first = next(iter(dd.positional()), None)
        first_v = first.value.upper() if first is not None else ""
        if st.data is not None or first_v in ("*", "DATA"):
            out.construct(at, "dd_implicit_sysin" if st.data is not None and st.data.implied else "dd_in_stream", True)
            return []
        dsn = dd.param("DSN") or dd.param("DSNAME")
        if first_v == "DUMMY" or (dsn is not None and dsn.value.upper() == "NULLFILE"):
            out.construct(at, "dd_dummy", False)
            return []
        if dd.param("SYSOUT") is not None:
            out.construct(at, "dd_sysout", False)
            return []
        path = dd.param("PATH")
        if path is not None:
            raw = jcl.unquote(path.value)
            resolved, missing = subst(raw, ctx.symbols)
            name = _dataset_name(resolved)
            if not name:
                out.construct(at, "dd_path", False)
                return []
            opts = (dd.param("PATHOPTS").value.upper() if dd.param("PATHOPTS") else "")
            writes = any(o in opts for o in ("OWRONLY", "ORDWR", "OCREAT", "OAPPEND", "OTRUNC"))
            self.emit_dataset(ctx, dd, (path.start, path.end), name, writes, "MOD" if writes else "SHR", {"unix": True, "parameterized": True if missing else None}, programs)
            out.construct((path.start, path.end), "dd_path", True)
            return [name]
        if dsn is None:
            out.construct(at, "dd_other", False)
            return []
        raw = jcl.unquote(dsn.value)
        where = (dsn.start, dsn.end)
        if raw.startswith("*."):
            parts = raw[2:].upper().split(".")
            ddname = parts[-1]
            # *.DD is this step; *.STEP.DD a job step, or inside a procedure a step of the same
            # call; *.STEP.PROCSTEP.DD a procedure step of a call.
            if len(parts) == 1:
                step_label = ctx.step
            elif ctx.procstep is not None and len(parts) == 2:
                step_label = f"{ctx.step.rsplit('.', 1)[0]}.{parts[0]}"
            else:
                step_label = ".".join(parts[:-1])
            entry = self.history.get((ctx.job + "/" + step_label, ddname))
            found = entry[1] if entry is not None else None
            if not found:
                out.construct(where, "dd_referback", False)
                out.info(f"step {ctx.job}.{ctx.step}: referback {raw[:40]} names no DD before it: not linked")
                return []
            disp = self.disposition(dd)
            for name, _ in found:
                self.emit_dataset(ctx, dd, where, name, disp in ("NEW", "MOD"), disp, {}, programs)
            out.construct(where, "dd_referback", True)
            return [n for n, _ in found]
        if raw.startswith("&&"):
            out.construct(where, "dd_temp", False)
            return [raw.lower()]
        resolved, missing = subst(raw, ctx.symbols) if "&" in raw else (raw, [])
        if raw.startswith("&") and missing and "." not in raw:
            out.construct(where, "dd_temp", False)  # &NAME with no symbolic of that name is a temporary data set
            return [raw.lower()]
        if resolved.upper().startswith(SYSTEM_PREFIXES):
            out.construct(where, "dd_system", False)
            return []
        if dd.ddname in LIBRARY_DDS:
            out.construct(where, "dd_library", False)
            return []
        for name in sorted(set(missing)):
            out.construct(where, "symbolic", False)
        if "&" in raw and not missing:
            out.construct(where, "symbolic", True)
        generation = _GENERATION.search(resolved)
        member = _PDS_MEMBER.search(resolved) if generation is None else None
        base = re.sub(r"\([^)]*\)$", "", resolved).upper()
        name = _dataset_name(base)
        if not name:
            out.construct(where, "dd_other", False)
            return []
        disp = self.disposition(dd, generation)
        writes = disp in ("NEW", "MOD")
        extra: dict[str, Any] = {}
        if generation is not None:
            extra["gdg"] = True
            out.construct(where, "dd_gdg", True)
        if member is not None:
            out.construct(where, "dd_member", True)
        if missing:
            extra["parameterized"] = True
            out.unresolved += 1
            out.info(f"step {ctx.job}.{ctx.step}: DD {dd.ddname} names its data set through {', '.join('&' + m for m in sorted(set(missing)))}, which has no literal value here: kept as {name}")
        else:
            out.resolved += 1
        self.emit_dataset(ctx, dd, where, name, writes, disp, extra, programs)
        out.construct(where, "dd_override" if dd.procstep else "dd_concatenation" if dd.concat else "dd_dsn", True)
        return [name]

    @staticmethod
    def disposition(dd: jcl.DD, generation: re.Match | None = None) -> str:
        """legacy.jcl's reading of DISP: the status, SHR when absent, NEW when empty; a (+n)
        generation is written whatever the status says."""
        p = dd.param("DISP")
        disp = ((p.value if p else "") or "SHR").strip("()").split(",")[0].upper() or "NEW"
        if generation is not None and generation.group(1).startswith("+") and disp not in ("NEW", "MOD"):
            disp = "NEW"
        return disp

    def emit_dataset(self, ctx: _Context, dd: jcl.DD, where: tuple[int, int], name: str, writes: bool, disp: str, extra: dict[str, Any], programs: list[dict[str, Any]]) -> None:
        out = self.out
        ds = out.entity(where, None, K.DATASET, {"name": name}, name, {"platform": "mainframe", **extra})
        q: dict[str, Any] = {"dd": dd.ddname}
        if ctx.procstep:
            q["step"] = ctx.procstep
        out.relation(where, ctx.task, P.WRITES_TO if writes else P.READS_FROM, ds, q)
        key = (ctx.job + "/" + ctx.step, dd.ddname)
        entry = self.history.get(key)
        if entry is None or entry[0] is not ctx:
            self.history[key] = (ctx, [(name, ds)])
        else:
            entry[1].append((name, ds))
        for program in programs:
            out.observation("dd_binding", where, {"task": ctx.task, "program": program, "dd": dd.ddname, "dataset": {**ds, "declare": False}, "disp": disp,
                                                  "job": ctx.job, "step": ctx.step})
        if programs:
            out.construct(where, "dd_binding", True)

    # -- behaviour
    def flow(self, u: jcl.Unit, me: dict[str, Any]) -> None:
        body = self.block(u.body)
        if body:
            self.out.behavior((u.stmt.start, max(u.end, u.stmt.end)), me, body, "flow")

    def block(self, items: list) -> list[dict[str, Any]]:
        out = self.out
        body: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, jcl.Step):
                at = out.at(it.stmt.start, it.stmt.end)
                named: dict[str, Any] = {}
                if it.pgm is not None:
                    named["program"] = {"e": "lit", "value": it.pgm.value.upper(), "type": "string"} if "&" not in it.pgm.value and not it.pgm.value.startswith("*") \
                        else {"e": "platform", "construct": "zos:exec_dynamic", "text": it.pgm.value.upper()[:40]}
                elif it.proc is not None:
                    named["procedure"] = {"e": "lit", "value": it.proc.value.upper(), "type": "string"}
                call = {"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": it.name, "kind": "procedure"}, "args": [], "named": named}}
                cond = it.param("COND")
                if cond is not None:
                    body.append({"op": "platform", "construct": "zos:cond", "at": at, "cond": cond.value, "body": [call]})
                else:
                    body.append(call)
            elif isinstance(it, jcl.IfNode):
                node: dict[str, Any] = {"op": "if", "at": out.at(it.stmt.start, max(it.end, it.stmt.end)), "cond": if_expression(it.expr), "then": self.block(it.then)}
                if it.els is not None:
                    node["else"] = self.block(it.els)
                out.construct((it.stmt.start, it.stmt.end), "if", True)
                body.append(node)
        return body


def merge(base: list[jcl.DD], overrides: list[jcl.DD]) -> list[jcl.DD]:
    """A procedure step's DDs with a call's overrides applied: a DD of the same name takes the
    override's keywords (all of it, when the override is in-stream, DUMMY or another kind of
    DD); a DD the step does not have is added."""
    out = list(base)
    index: dict[tuple[str, int], int] = {}  # (DD name, place in its concatenation) -> the first such DD
    for i, d in enumerate(out):
        index.setdefault((d.ddname, d.concat), i)
    for o in overrides:
        idx = index.get((o.ddname, o.concat))
        if idx is None:
            index[(o.ddname, o.concat)] = len(out)
            out.append(jcl.DD(o.stmt, o.ddname, o.procstep, o.concat, o.params))
            continue
        b = out[idx]
        if o.positional() or o.stmt.data is not None:
            out[idx] = jcl.DD(o.stmt, o.ddname, o.procstep, o.concat, o.params)
            continue
        keys = {p.key for p in o.params if p.key}
        if keys & {"DSN", "DSNAME"}:
            keys |= {"DSN", "DSNAME"}
        params = [p for p in b.params if p.key not in keys and p.key is not None] + [p for p in o.params if p.key]
        out[idx] = jcl.DD(o.stmt, o.ddname, o.procstep, o.concat, params)
    return out


# ------------------------------------------------------------------------------- TSO execs


def tso_exec(c: cards.Command) -> str | None:
    """The member a `%NAME`, `EX 'lib(member)'` or `EXEC lib(member)` runs."""
    verb = c.verb
    if verb.startswith("%"):
        name = verb[1:].upper()
        return name if _MEMBER.match(name) else None
    found = c.items()
    if len(found) < 2:
        return None
    target = found[1]
    text = target.text if target.kind == "word" else target.text + "(" + (target.value.text if target.value else "") + ")"
    m = re.search(r"\(([A-Za-z#@$][A-Za-z0-9#@$]{0,7})\)'?$", text.strip())
    if m:
        return m.group(1).upper()
    return None


# ------------------------------------------------------------------------------- BIND


def emit_bind(out: Out, b: db2.Bind) -> None:
    where = (b.start, b.end)
    if b.verb != "BIND" or b.copy is not None or not _MEMBER.match(b.name.split(".")[-1] or "-"):
        out.construct(where, "bind_other", False)
        return
    for opt in b.options:
        out.construct((opt.start, opt.end), "bind_option", False)
    if b.what == "PACKAGE":
        collection = b.name.split(".")[-1]
        if not b.members:
            out.construct(where, "bind_package", False)
            return
        for member, s, e in b.members:
            if not _MEMBER.match(member):
                out.construct((s, e), "bind_member", False)
                continue
            name = db2.package_name(collection, member)
            pkg = out.entity(where, "zos:Db2Package", K.PROGRAM, {"platform": "db2-package", "name": name}, name, {"collection": collection, "member": member, "language": "db2"})
            out.relation((s, e), pkg, P.DEPENDS_ON, _program(member), {"via": "dbrm"})
            out.construct(where, "bind_package", True)
            out.construct((s, e), "bind_member", True)
        return
    plan = b.name
    collections = sorted({e.split(".")[-2] for e, _, _ in b.pklist if e.endswith(".*") and len(e.split(".")) >= 2 and e.split(".")[-2] != "*"})
    packages = []
    for entry, s, e in b.pklist:
        parts = entry.split(".")
        if len(parts) >= 2 and parts[-1] != "*" and parts[-2] != "*" and _MEMBER.match(parts[-1]):
            packages.append((f"{parts[-2]}.{parts[-1]}", s, e))
        out.construct((s, e), "bind_pklist", True if (entry.endswith(".*") and len(parts) >= 2 and parts[-2] != "*") or (packages and packages[-1][1] == s) else False)
    attrs = {"collections": collections, "packages": sorted({p for p, _, _ in packages}), "members": sorted({m for m, _, _ in b.members}), "language": "db2"}
    me = out.entity(where, "zos:Db2Plan", K.PROGRAM, {"platform": "db2-plan", "name": plan}, plan, attrs)
    out.construct(where, "bind_plan", True)
    for name, s, e in packages:
        out.relation((s, e), me, P.CONTAINS, _program(name, "db2-package"))
    for member, s, e in b.members:
        if _MEMBER.match(member):
            out.relation((s, e), me, P.DEPENDS_ON, _program(member), {"via": "dbrm"})
            out.construct((s, e), "bind_member", True)
        else:
            out.construct((s, e), "bind_member", False)


def read_binds(out: Out, lines: list[tuple[int, str]]) -> None:
    bound, rest = db2.binds(cards.commands(lines, "tso"))
    for b in bound:
        emit_bind(out, b)
    for c in rest:
        out.construct((c.start, c.end), "tso_dsn" if c.verb in ("DSN", "END") else "tso_command", False)


# ------------------------------------------------------------------------------- CSD


def read_csd(out: Out, lines: list[tuple[int, str]]) -> None:
    defs, other = csd.definitions(lines)
    for c in other:
        out.construct((c.start, c.end), "csd_group", False)
    programs = {d.name: d for d in defs if d.resource == "PROGRAM"}
    entries = {d.name: d for d in defs if d.resource == "DB2ENTRY"}
    plans: dict[str, tuple[str, tuple[int, int]]] = {}
    for d in defs:
        if d.resource == "DB2ENTRY" and "PLAN" in d.keywords and "TRANSID" in d.keywords:
            plans[d.keywords["TRANSID"][0].upper()] = (d.keywords["PLAN"][0].upper(), (d.start, d.end))
    for d in defs:
        if d.resource == "DB2TRAN" and "TRANSID" in d.keywords and "ENTRY" in d.keywords:
            e = entries.get(d.keywords["ENTRY"][0].upper())
            if e is not None and "PLAN" in e.keywords:
                plans.setdefault(d.keywords["TRANSID"][0].upper(), (e.keywords["PLAN"][0].upper(), (d.start, d.end)))
    trans = {d.name for d in defs if d.resource == "TRANSACTION"}
    named_programs = {d.keywords["PROGRAM"][0].upper() for d in defs if d.resource == "TRANSACTION" and "PROGRAM" in d.keywords}
    aliased: dict[str, str] = {}
    for d in defs:
        where = (d.start, d.end)
        kw = d.keywords
        group = kw.get("GROUP", ("",))[0].upper() or None
        if d.resource == "TRANSACTION":
            if not re.fullmatch(r"[A-Z0-9#@$¢.<>(+|&!*);¬\-/%_?:=]{1,4}", d.name) or not d.name:
                out.construct(where, "csd_transaction", False)
                continue
            program = kw.get("PROGRAM", ("",))[0].upper() or None
            attrs: dict[str, Any] = {"group": group, "program": program, "profile": kw.get("PROFILE", ("",))[0].upper() or None,
                                     "tranclass": kw.get("TRANCLASS", ("",))[0].upper() or None,
                                     "remote": True if "REMOTESYSTEM" in kw else None}
            if program and program in programs and "LANGUAGE" in programs[program].keywords:
                attrs["program_language"] = programs[program].keywords["LANGUAGE"][0].upper()
            if d.name in plans:
                attrs["plan"] = plans[d.name][0]
            key = {"component_root": out.base.UNIT, "protocol": "cics", "method": "TRANSACTION", "route": d.name}
            ep = out.entity(d.name_at, "zos:Transaction", K.ENDPOINT, key, f"TRANSACTION {d.name}", attrs)
            if program and _MEMBER.match(program):
                out.relation(where, _program(program), P.EXPOSES, ep, {"via": "csd"})
            out.construct(where, "csd_transaction", True)
        elif d.resource == "PROGRAM":
            out.construct(where, "csd_program", d.name in named_programs and "LANGUAGE" in kw)
        elif d.resource == "FILE":
            name = _dataset_name(d.name)
            if not name:
                out.construct(where, "csd_file", False)
                continue
            ops = sorted(k.lower() for k in ("ADD", "BROWSE", "DELETE", "READ", "UPDATE") if kw.get(k, ("",))[0].upper() == "YES")
            f = out.entity(d.name_at, None, K.DATASET, {"name": name}, name, {"platform": "mainframe", "access": "cics_file", "cics_group": group, "operations": ops})
            dsn = kw.get("DSNAME")
            target = _dataset_name(dsn[0].upper()) if dsn else None
            if target and not target.startswith("{"):
                if name in aliased and aliased[name] != target:
                    out.warning(f"CICS file {d.name} is defined twice with different data sets: the first is kept")
                    out.construct(where, "csd_file", False)
                    continue
                aliased[name] = target
                ds = out.entity((dsn[1], dsn[2]), None, K.DATASET, {"name": target}, target, {"platform": "mainframe"})
                out.relation(where, f, P.ALIAS_OF, ds, {"via": "csd"})
                out.construct(where, "csd_file", True)
            else:
                out.construct(where, "csd_file", False)
        elif d.resource in ("DB2ENTRY", "DB2TRAN"):
            transid = kw.get("TRANSID", ("",))[0].upper()
            out.construct(where, "csd_db2", bool(transid and transid in trans and transid in plans))
        else:
            out.construct(where, csd.RESOURCES.get(d.resource, "csd_other"), False)


# ------------------------------------------------------------------------------- DCLGEN


def read_dclgen(out: Out, path: str, text: str) -> bool:
    from repo_intel.extractors.sql_lineage import clean_name

    found = db2.dclgen(text)
    if found is None:
        return False
    member = member_of(path)
    table = clean_name(found.table)
    # A DCLGEN member is a copybook (contract 11.3 item 4): the key and the field table are
    # the ones the cobol pack gives a copybook, so `COPY <member>` and `EXEC SQL INCLUDE
    # <member>` in a program link to it.
    key = {"kind": "copybook", "name": member}
    layout = f"cobol:{member}"
    attrs: dict[str, Any] = {"table": table, "columns": len(found.columns), "language": found.language}
    if found.record is not None:
        attrs["record"] = found.record.name
    schema = out.entity(found.table_at, "zos:Dclgen", K.DATA_SCHEMA, key, member, attrs)
    out.construct(found.table_at, "dclgen_table", True)
    if found.language != "cobol":
        out.construct(found.table_at, "dclgen_other_language", False)
    if found.record is None:
        return True
    tops = [c for c in found.record.children]
    column_of = {id(t): found.columns[i] for i, t in enumerate(tops) if i < len(found.columns)} if len(tops) == len(found.columns) else {}
    if tops and not column_of:
        out.info(f"DCLGEN {member}: {len(tops)} host variables for {len(found.columns)} columns: columns not matched to fields")
    count = 0
    for name, item, parent, top in db2.flatten(found.record):
        fa = db2.attributes_of(item)
        if parent is not None:
            fa["parent"] = parent.name
        col = column_of.get(id(item))
        if col is not None:
            fa.update({"column": col.name, "sql_type": col.sql_type, "nullable": col.nullable})
        elif top is not None and id(top) in column_of and item is not top:
            fa["column"] = column_of[id(top)].name
        f = out.entity((item.start, item.end), "zos:DclgenField", K.FIELD, {"table": layout, "name": name}, name, fa)
        out.relation((item.start, item.end), schema, P.CONTAINS, f)
        out.construct((item.start, item.end), "dclgen_field", True)
        count += 1
    out.attrs_of(K.DATA_SCHEMA, key)["fields"] = count
    if found.indicator is not None:
        out.construct((found.indicator.start, found.indicator.end), "dclgen_indicator", False)
    return True


# ------------------------------------------------------------------------------- DDL


def read_ddl(out: Out, path: str, data: bytes, blob: str, text: str) -> bool:
    from repo_intel.extractors.contracts_data import SqlDdl, split_statements

    statements = split_statements(text)
    masked, kinds = db2.mask_ddl(text, statements)
    if not kinds:
        return False
    if len(masked) != len(text):
        return False
    try:
        data.decode("utf-8")
        masked_bytes = masked.encode("utf-8")
    except UnicodeDecodeError:
        masked_bytes = masked.encode("cp1252", "replace")
    if len(masked_bytes) != len(data):
        out.warning("DDL with characters that change length when masked: not read")
        return False
    result = SqlDdl().extract(path, masked_bytes, blob)
    spans = [(o.evidence.span.start_byte, o) for o in result.observations if o.kind == "entity"]
    for kind, a, b in kinds:
        ba, bb = out.to_byte(a), out.to_byte(b)
        modelled = kind in ("ddl_table", "ddl_view", "ddl_other") and any(ba <= s <= bb for s, _ in spans)
        out.construct((a, b), kind, modelled, ev=E2)
    out.others.extend(result.observations)
    out.diagnostics.extend(result.diagnostics)
    return True


# ------------------------------------------------------------------------------- control cards


def _instream(dds: list[jcl.DD], name: str) -> jcl.DD | None:
    return next((d for d in dds if d.ddname == name), None)


def _control_lines(out: Out, dds: list[jcl.DD], name: str) -> list[tuple[int, str]] | None:
    dd = _instream(dds, name)
    if dd is None:
        return None
    if dd.stmt.data is None:
        dsn = dd.param("DSN") or dd.param("DSNAME")
        if dsn is not None:
            out.construct((dsn.start, dsn.end), "control_member", False)
        return None
    return dd.stmt.data.lines


class _Shapes:
    def __init__(self, out: Out):
        self.out = out
        self.items: list[dict[str, Any]] = []

    def add(self, op: str, where: tuple[int, int], inputs: list[str], **fields: Any) -> str:
        t: dict[str, Any] = {"id": f"t{len(self.items) + 1}", "op": op, "at": self.out.at(*where), "inputs": [i for i in inputs if i]}
        for k, v in fields.items():
            if v not in (None, [], {}):
                t[k] = v
        if any(_placeholder(i) for i in t["inputs"]) or _placeholder(t.get("output")):
            t["dynamic"] = True
        self.items.append(t)
        return t["id"]


def _placeholder(name: Any) -> bool:
    return isinstance(name, str) and bool(re.search(r"\{[A-Za-z_][\w.]*\}", name))


def sort_shapes(out: Out, lines: list[tuple[int, str]], inputs: list[str], output: str | None, outfil: dict[str, str]) -> tuple[list[dict[str, Any]], tuple[int, int] | None]:
    """Transform shapes of DFSORT control statements, in the order DFSORT applies them:
    JOINKEYS, INCLUDE or OMIT, INREC, SORT or MERGE, SUM, OUTREC, OUTFIL."""
    stmts = cards.sort_statements(lines)
    if not stmts:
        return [], None
    span = (stmts[0].start, stmts[-1].end)
    by_op: dict[str, list[cards.SortStatement]] = {}
    for st in stmts:
        by_op.setdefault(st.op, []).append(st)
    b = _Shapes(out)
    cur = list(inputs)
    keys: list[str] = []
    copy = False
    joins = by_op.get("JOINKEYS", [])
    if joins:
        sides: dict[str, tuple[cards.SortStatement, list[cards.SortField] | None, str]] = {}
        for st in joins:
            params = st.params()
            fields = cards.sort_fields(params.get("FIELDS", cards.Chars()).text, ordered=True, formatted=False)
            side = (params.get("FILE") or params.get("F") or cards.Chars()).text.upper()
            dd = f"SORTJN{side}" if side in ("F1", "F2") else None
            for label in ("F1", "F2"):
                if label in params:
                    side, dd = label, params[label].text.upper()
            if side in ("F1", "F2") and side not in sides:
                sides[side] = (st, fields, outfil.get(dd or "", "{" + (dd or side).lower() + "}"))
        unpaired: set[str] = set()
        for st in by_op.get("JOIN", []):
            unpaired |= {w.strip().upper() for w in jcl.subparams(st.params().get("UNPAIRED", cards.Chars()).text)}
            unpaired |= {w.strip().upper() for w in st.operands.text.upper().replace("UNPAIRED", "").split(",") if w.strip() in ("F1", "F2", "ONLY")}
        kept = unpaired & {"F1", "F2"}
        jt = None if "ONLY" in unpaired and len(kept) != 1 else "anti" if "ONLY" in unpaired else {frozenset(): "inner", frozenset({"F1"}): "left", frozenset({"F2"}): "right"}.get(frozenset(kept), "full")
        left, right = sides.get("F1"), sides.get("F2")
        ok = left is not None and right is not None and left[1] and right[1] and len(left[1]) == len(right[1]) and not any(f.symbolic for f in left[1] + right[1]) and jt is not None
        where = (joins[0].start, joins[-1].end)
        if ok:
            first, second = (right, left) if jt == "anti" and kept == {"F2"} else (left, right)  # the side whose unpaired rows are kept comes first
            inputs_j = [first[2], second[2]]
            on = [[x.name, y.name] for x, y in zip(first[1], second[1])]
            cur = [b.add("join", where, inputs_j, join={"type": jt, "on": on}, keys=[a for a, _ in on], primitive="join")]
        else:
            cur = [b.add("platform", where, [x[2] for x in (left, right) if x is not None], construct="zos:sort_joinkeys")]
        for st in joins + by_op.get("JOIN", []) + by_op.get("REFORMAT", []):
            out.construct((st.start, st.end), "sort_joinkeys", bool(ok))
    for op in ("INCLUDE", "OMIT"):
        for st in by_op.get(op, []):
            params = st.params()
            text = (params.get("COND") or st.operands).text
            cond = cards.sort_condition(text)
            pred = cond.expr if cond.expr is not None else {"e": "platform", "construct": "zos:sort_include", "text": " ".join(text.split())[:200]}
            if op == "OMIT":
                pred = {"e": "unop", "op": "not", "operand": pred}
            cur = [b.add("filter", (st.start, st.end), cur, predicate=pred)]
            out.construct((st.start, st.end), "sort_include", True)
    for st in by_op.get("INREC", []):
        cur = _reformat(out, b, st, cur)
    for op in ("SORT", "MERGE"):
        for st in by_op.get(op, []):
            fields_text = st.params().get("FIELDS", cards.Chars()).text
            if fields_text.upper() == "COPY":
                copy = True
                out.construct((st.start, st.end), "sort_copy", True)
                continue
            fields = cards.sort_fields(fields_text, ordered=True, formatted=True)
            if fields is None or any(f.symbolic for f in fields):
                out.construct((st.start, st.end), "sort_symbolic_field" if fields else ("sort_fields" if op == "SORT" else "merge_fields"), False)
                if fields:
                    out.construct((st.start, st.end), "sort_fields" if op == "SORT" else "merge_fields", False)
                cur = [b.add("platform", (st.start, st.end), cur, construct=f"zos:{'sort_fields' if op == 'SORT' else 'merge_fields'}")]
                continue
            keys = [f.name for f in fields]
            order = [{"column": f.name, "desc": f.order == "D"} for f in fields]
            if op == "MERGE" and len(cur) > 1:
                cur = [b.add("union", (st.start, st.end), cur)]
            cur = [b.add("sort", (st.start, st.end), cur, keys=keys, order=order)]
            out.construct((st.start, st.end), "sort_fields" if op == "SORT" else "merge_fields", True)
    for st in by_op.get("OPTION", []):
        if re.search(r"(?:^|,)COPY(?:,|$)", st.operands.text.upper()):
            copy = True
            out.construct((st.start, st.end), "sort_copy", True)
        else:
            out.construct((st.start, st.end), "sort_option", False)
    for st in by_op.get("SUM", []):
        text = st.params().get("FIELDS", cards.Chars()).text
        if not keys:
            out.construct((st.start, st.end), "sort_sum_none" if text.upper() == "NONE" else "sort_sum", False)
            continue
        if text.upper() in ("NONE", "(NONE)"):
            cur = [b.add("deduplicate", (st.start, st.end), cur, keys=keys, primitive="dedupe_by_key")]
            out.construct((st.start, st.end), "sort_sum_none", True)
            continue
        fields = cards.sort_fields(text, ordered=False, formatted=True)
        if not fields or any(f.symbolic for f in fields):
            out.construct((st.start, st.end), "sort_sum", False)
            continue
        cur = [b.add("aggregate", (st.start, st.end), cur, keys=keys, measures=[{"fn": "sum", "column": f.name, "as": f.name} for f in fields], primitive="aggregate")]
        out.construct((st.start, st.end), "sort_sum", True)
    for st in by_op.get("OUTREC", []):
        cur = _reformat(out, b, st, cur)
    fils = by_op.get("OUTFIL", [])
    for st in fils:
        params = st.params()
        names = [n.strip() for n in jcl.subparams((params.get("FNAMES") or params.get("FNAME") or cards.Chars("SORTOUT")).text)]
        targets = [outfil.get(n.upper()) for n in names]
        cond_p = params.get("INCLUDE") or params.get("OMIT")
        pred = None
        if cond_p is not None:
            cond = cards.sort_condition(cond_p.text)
            pred = cond.expr if cond.expr is not None else {"e": "platform", "construct": "zos:sort_include", "text": " ".join(cond_p.text.split())[:200]}
            if "OMIT" in params and "INCLUDE" not in params:
                pred = {"e": "unop", "op": "not", "operand": pred}
        for target in targets:
            if pred is not None:
                b.add("filter", (st.start, st.end), cur, predicate=pred, output=target)
            else:
                b.add("read", (st.start, st.end), cur, output=target)
        out.construct((st.start, st.end), "sort_outfil", bool(targets) and all(targets))
    for op in ("RECORD", "ALTSEQ", "MODS", "END", "DEBUG"):
        for st in by_op.get(op, []):
            out.construct((st.start, st.end), "sort_option", False)
    if not fils:
        if b.items:
            if output:
                b.items[-1]["output"] = output
                if _placeholder(output):
                    b.items[-1]["dynamic"] = True
        elif copy:
            b.add("read", span, cur, output=output)
    return b.items, span


def _reformat(out: Out, b: _Shapes, st: cards.SortStatement, cur: list[str]) -> list[str]:
    params = st.params()
    fields = params.get("FIELDS") or params.get("BUILD")
    if fields is None:
        out.construct((st.start, st.end), "sort_reformat_other", False)
        return cur
    r = cards.reformat(fields.text)
    if r is None:
        out.construct((st.start, st.end), "sort_reformat", False)
        return [b.add("platform", (st.start, st.end), cur, construct="zos:sort_reformat")]
    columns: dict[str, Any] = {}
    renames = {a: c for a, c in r.moved if a != c}
    if renames:
        columns["rename"] = renames
    if r.added:
        columns["add"] = r.added
    out.construct((st.start, st.end), "sort_reformat", True)
    return [b.add("project", (st.start, st.end), cur, columns=columns, primitive=None if r.added else "select_columns")]


def sort_step(out: Out, task: dict[str, Any], step: jcl.Step, dds: list[jcl.DD], bound: dict[str, list[str]]) -> None:
    lines = _control_lines(out, dds, "SYSIN")
    if lines is None:
        return
    inputs = [n for dd in sorted(bound) if re.fullmatch(r"SORTIN(\d\d)?", dd) for n in bound[dd]]
    outputs = bound.get("SORTOUT", [])
    outfil = {dd: names[0] for dd, names in bound.items() if names}
    shapes, span = sort_shapes(out, lines, inputs, outputs[0] if outputs else None, outfil)
    if shapes:
        out.transform(span, task, shapes)


def icetool_step(out: Out, task: dict[str, Any], step: jcl.Step, dds: list[jcl.DD], bound: dict[str, list[str]]) -> None:
    lines = _control_lines(out, dds, "TOOLIN")
    if lines is None:
        return
    b = _Shapes(out)
    first = last = None
    for c in cards.commands(lines, "tso"):
        found = c.items()
        verb = c.verb
        where = (c.start, c.end)
        first = first or c.start
        last = c.end
        src = cards.value_text(cards.keyword(found, "FROM"))
        to = cards.keyword(found, "TO")
        ins = [n for dd in (src.upper().split() if src else []) for n in bound.get(dd, [])]
        outs = [n for dd in (to.words() if to else []) for n in bound.get(dd.upper(), [])]
        if verb == "COPY" and ins:
            for o in outs or [None]:
                b.add("read", where, ins, output=o)
            out.construct(where, "icetool_copy", True)
        elif verb == "SORT" and ins:
            using = cards.value_text(cards.keyword(found, "USING"))
            ctl = _control_lines(out, dds, f"{using.upper()}CNTL") if using else None
            if ctl:
                shapes, _ = sort_shapes(out, ctl, ins, outs[0] if outs else None, {dd: n[0] for dd, n in bound.items() if n})
                base = len(b.items)
                for t in shapes:  # renumber into this unit
                    t = dict(t)
                    t["inputs"] = [f"t{int(i[1:]) + base}" if re.fullmatch(r"t\d+", i) else i for i in t["inputs"]]
                    t["id"] = f"t{len(b.items) + 1}"
                    b.items.append(t)
                out.construct(where, "icetool_sort", bool(shapes))
            else:
                out.construct(where, "icetool_sort", False)
        elif verb == "SELECT" and ins:
            on = [i for i in found if i.kind == "keyword" and i.text == "ON"]
            keys = []
            for o in on:
                fields = cards.sort_fields(o.value.text if o.value else "", ordered=False, formatted=True)
                keys += [f.name for f in fields or []]
            mode = next((w.text.upper() for w in found if w.kind == "word" and w.text.upper() in ("FIRST", "LAST", "NODUPS", "ALLDUPS", "FIRSTDUP", "LASTDUP", "HIGHER", "LOWER", "EQUAL")), None)
            if keys and mode in ("FIRST", "LAST"):
                b.add("deduplicate", where, ins, keys=keys, primitive="dedupe_by_key", output=outs[0] if outs else None)
                out.construct(where, "icetool_select", True)
            elif keys and mode:
                b.add("deduplicate", where, ins, keys=keys, candidates=["dedupe_by_key"], output=outs[0] if outs else None)
                out.construct(where, "icetool_select", True)
            else:
                out.construct(where, "icetool_select", False)
        else:
            out.construct(where, {"COPY": "icetool_copy", "SORT": "icetool_sort", "SELECT": "icetool_select"}.get(verb, "icetool_other"), False)
    if b.items:
        out.transform((first, last), task, b.items)


_IDCAMS_ORG = {"INDEXED": "ksds", "IXD": "ksds", "NONINDEXED": "esds", "NIXD": "esds", "NUMBERED": "rrds", "NUMD": "rrds", "LINEAR": "lds", "LIN": "lds"}


def idcams_commands(out: Out, task: dict[str, Any] | None, lines: list[tuple[int, str]], bound: dict[str, list[str]]) -> None:
    """IDCAMS commands: definitions declare data sets; DELETE and REPRO are the step's writes
    and reads (only when a step runs them: a member alone has no task)."""
    for c in cards.commands(lines, "idcams"):
        found = c.items()
        verb = c.verb
        where = (c.start, c.end)
        if verb in ("DEFINE", "DEF"):
            kind_item = found[1] if len(found) > 1 else None
            kind = (kind_item.text if kind_item is not None else "").upper()
            body = kind_item.items() if kind_item is not None and kind_item.kind == "keyword" else (found[2].items() if len(found) > 2 and found[2].kind == "group" else [])
            subs = {it.text: it.items() for it in found[2:] if it.kind == "keyword" and it.text in ("DATA", "INDEX", "IX")}
            groups = [it for it in found[2:] if it.kind == "word" and it.text.upper() in ("DATA", "INDEX", "IX")]
            for g in groups:  # DATA (NAME(...)) with a blank before the parenthesis
                idx = found.index(g)
                if idx + 1 < len(found) and found[idx + 1].kind == "group":
                    subs[g.text.upper()] = found[idx + 1].items()
            name = cards.value_text(cards.keyword(body, "NAME"))
            dsname = _dataset_name(name.upper()) if name else None
            if kind in ("CLUSTER", "CL"):
                if not dsname:
                    out.construct(where, "idcams_define_cluster", False)
                    continue
                attrs: dict[str, Any] = {"platform": "mainframe"}
                everywhere = body + subs.get("DATA", [])
                org = next((_IDCAMS_ORG[w.text.upper()] for w in everywhere if w.kind == "word" and w.text.upper() in _IDCAMS_ORG), "ksds" if cards.keyword(everywhere, "KEYS") else None)
                attrs["vsam"] = org
                keys = cards.keyword(everywhere, "KEYS")
                if keys is not None and len(keys.words()) == 2 and all(w.isdigit() for w in keys.words()):
                    attrs["key_length"], attrs["key_offset"] = int(keys.words()[0]), int(keys.words()[1])
                size = cards.keyword(everywhere, "RECORDSIZE", "RECSZ")
                if size is not None and len(size.words()) == 2 and all(w.isdigit() for w in size.words()):
                    attrs["average_record_length"], attrs["record_length"] = int(size.words()[0]), int(size.words()[1])
                ds = out.entity(where, None, K.DATASET, {"name": dsname}, dsname, attrs)
                if task is not None:
                    out.relation(where, task, P.WRITES_TO, ds, {"op": "define"})
                out.construct(where, "idcams_define_cluster", True)
            elif kind in ("GENERATIONDATAGROUP", "GDG"):
                if not dsname:
                    out.construct(where, "idcams_define_gdg", False)
                    continue
                limit = cards.keyword(body, "LIMIT", "LIM")
                attrs = {"platform": "mainframe", "gdg": True}
                if limit is not None and limit.words() and limit.words()[0].isdigit():
                    attrs["gdg_limit"] = int(limit.words()[0])
                ds = out.entity(where, None, K.DATASET, {"name": dsname}, dsname, attrs)
                if task is not None:
                    out.relation(where, task, P.WRITES_TO, ds, {"op": "define"})
                out.construct(where, "idcams_define_gdg", True)
            elif kind in ("ALTERNATEINDEX", "AIX"):
                relate = cards.value_text(cards.keyword(body, "RELATE", "REL"))
                base = _dataset_name(relate.upper()) if relate else None
                if not dsname or not base:
                    out.construct(where, "idcams_define_aix", False)
                    continue
                attrs = {"platform": "mainframe", "vsam": "aix", "alternate_index_of": base}
                keys = cards.keyword(body, "KEYS")
                if keys is not None and len(keys.words()) == 2 and all(w.isdigit() for w in keys.words()):
                    attrs["key_length"], attrs["key_offset"] = int(keys.words()[0]), int(keys.words()[1])
                out.entity(where, None, K.DATASET, {"name": dsname}, dsname, attrs)
                out.construct(where, "idcams_define_aix", True)
            elif kind == "PATH":
                entry = cards.value_text(cards.keyword(body, "PATHENTRY", "PENT"))
                target = _dataset_name(entry.upper()) if entry else None
                if not dsname or not target:
                    out.construct(where, "idcams_define_path", False)
                    continue
                p = out.entity(where, None, K.DATASET, {"name": dsname}, dsname, {"platform": "mainframe", "vsam": "path"})
                out.relation(where, p, P.ALIAS_OF, out.base.ref(K.DATASET, {"name": target}, target, declare=False), {"via": "idcams_path"})
                out.construct(where, "idcams_define_path", True)
            else:
                out.construct(where, "idcams_define_other", False)
        elif verb in ("DELETE", "DEL"):
            target = found[1] if len(found) > 1 else None
            names = ([target.text] if target is not None and target.kind == "word" else target.words() if target is not None and target.kind == "group" else [])
            ok = False
            for raw in names:
                n = _dataset_name(raw.strip("'").upper())
                if n and "*" not in raw and task is not None:
                    out.relation(where, task, P.WRITES_TO, out.base.ref(K.DATASET, {"name": n}, n, declare=False), {"op": "delete"})
                    ok = True
            out.construct(where, "idcams_delete", ok)
        elif verb == "REPRO":
            src = _repro_side(found, ("INFILE", "IFILE"), ("INDATASET", "IDS"), bound)
            dst = _repro_side(found, ("OUTFILE", "OFILE"), ("OUTDATASET", "ODS"), bound)
            if task is not None and src and dst:
                for n in src:
                    if not n.startswith("&"):
                        out.relation(where, task, P.READS_FROM, out.base.ref(K.DATASET, {"name": n}, n, declare=False), {"op": "repro"})
                for n in dst:
                    if not n.startswith("&"):
                        out.relation(where, task, P.WRITES_TO, out.base.ref(K.DATASET, {"name": n}, n, declare=False), {"op": "repro"})
                out.construct(where, "idcams_repro", True)
            else:
                out.construct(where, "idcams_repro", False)
        elif verb in ("IF", "SET", "ELSE", "DO", "END", "THEN"):
            out.construct(where, "idcams_condition", False)
        else:
            out.construct(where, "idcams_other", False)


def _repro_side(found: list[cards.Item], dd_keys: tuple[str, ...], ds_keys: tuple[str, ...], bound: dict[str, list[str]]) -> list[str]:
    dd = cards.value_text(cards.keyword(found, *dd_keys))
    if dd:
        return list(bound.get(dd.split("/")[0].upper(), []))
    ds = cards.value_text(cards.keyword(found, *ds_keys))
    n = _dataset_name(ds.upper()) if ds else None
    return [n] if n else []


def idcams_step(out: Out, task: dict[str, Any], step: jcl.Step, dds: list[jcl.DD], bound: dict[str, list[str]]) -> None:
    lines = _control_lines(out, dds, "SYSIN")
    if lines is not None:
        idcams_commands(out, task, lines, bound)


# ------------------------------------------------------------------------------- the extractors


class ZosJcl:
    id = "zos.jcl"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(JCL_SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        text = decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        out = Out(self.id, path, data, blob, text)
        if not JclReader(out, path, text).read():
            return Extraction(out.observations(), [*out.diagnostics, diagnostic(self.id, "info", "no JOB or PROC statement")], parsed=True)
        return out.result()


_JCL_CONTENT = re.compile(r"(?m)^//[A-Z@#$][A-Z0-9@#$]{0,7}\s+(?:JOB|PROC)\b")
_IDCAMS_CONTENT = re.compile(r"(?im)^\s+(?:DEFINE\s+(?:CLUSTER|GDG|GENERATIONDATAGROUP|AIX|ALTERNATEINDEX|PATH)\b|REPRO\b|DELETE\s+\S)")
_SORT_CONTENT = re.compile(r"(?im)^\s+(?:SORT|MERGE)\s+FIELDS=|^\s+(?:INCLUDE|OMIT)\s+COND=|^\s+OUTREC\s+(?:FIELDS|BUILD)=")
_BIND_CONTENT = re.compile(r"(?im)^\s*(?:BIND|REBIND|FREE)\s+(?:PACKAGE|PLAN)\s*\(")


class ZosDefinitions:
    id = "zos.definitions"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        low = path.lower()
        base = posixpath.basename(low)
        if base.startswith(".") or low.endswith((".sql",) + JCL_SUFFIXES):
            return False
        return low.startswith(INPUT_DIR) or low.endswith(DEFINITION_SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        text = decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        out = Out(self.id, path, data, blob, text)
        lines = jcl._lines(text)
        read = []
        if _JCL_CONTENT.search(text):
            if JclReader(out, path, text).read():
                read.append("jcl")
        elif csd.looks_like_csd(text):
            read_csd(out, lines)
            read.append("csd")
        else:
            if read_dclgen(out, path, text):
                read.append("dclgen")
            if _BIND_CONTENT.search(text):
                read_binds(out, lines)
                read.append("bind")
            if "dclgen" not in read and db2.looks_like_ddl(text) and read_ddl(out, path, data, blob, text):
                read.append("ddl")
            if _IDCAMS_CONTENT.search(text) and "ddl" not in read:
                idcams_commands(out, None, lines, {})
                read.append("idcams")
            if _SORT_CONTENT.search(text):
                shapes, span = sort_shapes(out, lines, ["{sortin}"], "{sortout}", {})
                if shapes:
                    out.transform(span, "@file", shapes)
                read.append("sort")
        if not read:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "not a definition this pack reads (JCL, CSD export, DCLGEN, BIND cards, DB2 DDL, IDCAMS or DFSORT control cards)")], parsed=False)
        return out.result()

