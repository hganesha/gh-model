"""Legacy code for migration work: COBOL and JCL, SAS, ColdFusion, Synapse and Data Factory (spec §8.1).

Legacy repositories rarely have a manifest, so each source file declares the
`Program` it is (natural key `{platform, name}`), and programs refer to each other
by name, as their platforms do. A reference to a program, copybook or notebook is
made without declaring it: it is kept only when the repository defines it.

- COBOL (`.cbl`, `.cob`, `.cobol`): `PROGRAM-ID` is a Program; `CALL 'X'`, CICS
  `LINK` and `XCTL` CALL another; `COPY X` DEPENDS_ON a copybook (a DataSchema
  that `.cpy` files declare); `EXEC SQL` goes through the SQL lineage scanner and
  CICS `READ`/`WRITE` FILE name datasets. A `CALL` through a variable is counted
  as unresolved.
- JCL (`.jcl`, `.prc`, `.proc`): `JOB` and `PROC` cards are Jobs, with the file's stem
  upper-cased as `attributes.member` (the library member); each `EXEC` step is
  a Task that RUNS its program (or procedure, or the DB2 program run under
  IKJEFT01) and depends on the step before it; `DD DSN=` datasets are read
  (`DISP=SHR`/`OLD`) or written (`NEW`, `MOD`, `(+1)` generations). Temporary
  `&&` datasets and system libraries are skipped.
- SAS (`.sas`): `%macro` is a Program, the rest of the file another; `DATA` steps
  write their datasets and read `SET`/`MERGE`/`UPDATE`; `PROC SQL` goes through the
  SQL scanner; procedures' `DATA=`/`OUT=`/`BASE=` read and write; macro calls and
  `%include` CALL; `LIBNAME` declares a ConfigKey named for the library, with its
  engine, never its path or options. `WORK` (one-level) datasets are temporary
  and skipped.
- ColdFusion (`.cfc`, `.cfm`): a component is a Program named by its dotted path
  and EXPOSES an Endpoint for each `remote` function; a page is a Program and an
  Endpoint; `cfquery` and `queryExecute` SQL goes through the SQL scanner, with
  `#expressions#` and `cfqueryparam` blanked; `cfinvoke`, `createObject`, `new`
  and `cfinclude` CALL. Data source names are ConfigKeys.
- Synapse and Data Factory JSON (`pipeline/`, `trigger/`, `dataset/`,
  `sqlscript/`, `notebook/`): pipelines are Jobs whose activities are Tasks with
  `dependsOn` edges; Copy activities read and write datasets; notebook,
  stored-procedure and execute-pipeline activities RUN what they name; triggers
  are Jobs that RUN pipelines; a dataset is an ALIAS_OF the table or path it
  points at; SQL scripts are T-SQL Programs; notebooks are scanned as notebooks.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction, diagnostic, entity, find_span, ref, relation, span, whole_file
from repo_intel.extractors.data_python import dataset_name, notebook_cells, scan_cells
from repo_intel.extractors.sql_lineage import Lineage, byte_offsets, clean_name, lineage, lineage_relations, lineage_summary, program, table

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1
E2 = EvidenceClass.E2


class _Out:
    def __init__(self, path: str, data: bytes, blob: str, text: str):
        self.path, self.data, self.blob, self.text = path, data, blob, text
        self.to_byte = byte_offsets(text, data)
        self.items: list = []
        self.resolved = self.unresolved = 0

    def at(self, a: int, b: int):
        return span(self.data, self.to_byte(a), self.to_byte(b))

    def entity(self, at, kind, key, name, attrs=None, ev=E2):
        self.items.append(entity(self.blob, at, kind, key, name, attrs or {}, ev))
        return ref(kind, key, name)

    def relation(self, at, subject, predicate, obj, qualifiers=None, ev=E2):
        self.items.append(relation(self.blob, at, subject, predicate, obj, qualifiers or {}, ev))

    def sql(self, subject, sql: str, offset: int, dialect: str | None = None, kind=K.TABLE, rename=None) -> None:
        found = lineage(sql, dialect)
        if rename is not None:  # names taken from the text before placeholders were masked
            found = Lineage([(rename(a, b) or n, a, b) for n, a, b in found.writes], [(rename(a, b) or n, a, b) for n, a, b in found.reads], found.unresolved)
        self.resolved += len(found.writes) + len(found.reads)
        self.unresolved += found.unresolved
        self.items += lineage_relations(self.blob, self.data, self.text, found, subject, kind=kind, offset=offset, to_byte=self.to_byte)

    def dataset(self, at, subject, predicate, raw: str | None, attrs=None):
        name = dataset_name(raw) if raw else None
        if name:
            self.resolved += 1
            self.relation(at, subject, predicate, self.entity(at, K.DATASET, {"name": name}, name, attrs))

    def done(self, diagnostics=None) -> Extraction:
        return Extraction(self.items + lineage_summary(self.blob, self.data, self.resolved, self.unresolved), diagnostics or [])


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")  # legacy sources are often not UTF-8
        except UnicodeDecodeError:
            return None


# ------------------------------------------------------------------- COBOL


def _cobol_code(text: str) -> str:
    """Blank sequence numbers, comment lines and columns past 72 of fixed-format
    COBOL, keeping every character offset."""
    lines = text.split("\n")
    fixed = sum(1 for l in lines if len(l) > 6 and (l[:6].strip().isdigit() or not l[:6].strip()) and l[6:7] in (" ", "*", "-", "/", "D", "d")) >= max(1, len([l for l in lines if l.strip()]) // 2)
    out = []
    for line in lines:
        if fixed and len(line) > 6:
            if line[6] in "*/":
                out.append(" " * len(line))
            else:
                out.append(" " * 7 + line[7:72] + " " * max(0, len(line) - 72))
        elif not fixed and line.lstrip().startswith("*>"):
            out.append(" " * len(line))
        else:
            out.append(line)
    return "\n".join(out)


_PROGRAM_ID = re.compile(r"\bPROGRAM-ID\.?\s+['\"]?([A-Z0-9][\w-]*)", re.I)
_COBOL_CALL = re.compile(r"\bCALL\s+(?:(['\"])([A-Z0-9][\w-]*)\1|([A-Z][\w-]*))", re.I)
_COPY = re.compile(r"\bCOPY\s+['\"]?([A-Z0-9][\w-]*)['\"]?", re.I)
_EXEC = re.compile(r"\bEXEC\s+(SQL|CICS)\b(.*?)\bEND-EXEC\b", re.I | re.S)
_CICS_PROGRAM = re.compile(r"\b(LINK|XCTL)\b.*?\bPROGRAM\s*\(\s*(?:'([^']+)'|\"([^\"]+)\"|([\w-]+))\s*\)", re.I | re.S)
_CICS_FILE = re.compile(r"\b(READ|READNEXT|READPREV|STARTBR|WRITE|REWRITE|DELETE)\b.*?\b(?:FILE|DATASET)\s*\(\s*'([^']+)'\s*\)", re.I | re.S)


class Cobol:
    id = "legacy.cobol"
    version = "1.0.0"
    SUFFIXES = (".cbl", ".cob", ".cobol", ".cpy")

    def matches(self, path):
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path, data, blob):
        text = _decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        code = _cobol_code(text)
        o = _Out(path, data, blob, code)
        if path.lower().endswith(".cpy"):
            name = posixpath.splitext(posixpath.basename(path))[0].upper()
            o.entity(whole_file(data), K.DATA_SCHEMA, {"kind": "copybook", "name": name}, name, {"platform": "mainframe"})
            return o.done()
        m = _PROGRAM_ID.search(code)
        if not m:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no PROGRAM-ID")], parsed=True)
        name = m.group(1).upper()
        me = program("mainframe", name)
        o.entity(o.at(m.start(1), m.end(1)), K.PROGRAM, me["natural_key"], name, {"language": "cobol", "cics": bool(re.search(r"\bEXEC\s+CICS\b", code, re.I)), "db2": bool(re.search(r"\bEXEC\s+SQL\b", code, re.I))})
        execs = [(e.start(), e.end()) for e in _EXEC.finditer(code)]
        inside = lambda i: any(a <= i < b for a, b in execs)
        for c in _COBOL_CALL.finditer(code):
            if inside(c.start()):
                continue
            if c.group(2):
                o.relation(o.at(c.start(2), c.end(2)), me, P.CALLS, program("mainframe", c.group(2).upper(), declare=False))
            else:
                o.unresolved += 1  # CALL through a data item: the target is decided at run time
        seen = set()
        for c in _COPY.finditer(code):
            if inside(c.start()) or c.group(1).upper() in seen:
                continue
            seen.add(c.group(1).upper())
            book = c.group(1).upper()
            o.relation(o.at(c.start(1), c.end(1)), me, P.DEPENDS_ON, ref(K.DATA_SCHEMA, {"kind": "copybook", "name": book}, book, declare=False))
        for e in _EXEC.finditer(code):
            body_start = e.start(2)
            if e.group(1).upper() == "SQL":
                sql = re.sub(r":[\w-]+(?:\.[\w-]+)?", lambda h: "0".ljust(len(h.group())), e.group(2))  # host variables
                if re.match(r"\s*(INCLUDE|WHENEVER|BEGIN|END)\b", sql, re.I):
                    continue
                sql = re.sub(r"^\s*DECLARE\s+[\w-]+\s+CURSOR\s+(?:WITH\s+HOLD\s+)?FOR\b", lambda h: " " * len(h.group()), sql, flags=re.I)
                o.sql(me, sql, body_start)
            else:
                for p in _CICS_PROGRAM.finditer(e.group(2)):
                    target = p.group(2) or p.group(3)
                    if target:
                        o.relation(o.at(body_start + p.start(), body_start + p.end()), me, P.CALLS, program("mainframe", target.upper(), declare=False))
                    else:
                        o.unresolved += 1
                for f in _CICS_FILE.finditer(e.group(2)):
                    predicate = P.READS_FROM if f.group(1).upper().startswith(("READ", "STARTBR")) else P.WRITES_TO
                    o.dataset(o.at(body_start + f.start(2), body_start + f.end(2)), me, predicate, f.group(2).strip(), {"access": "cics_file"})
        return o.done()


# --------------------------------------------------------------------- JCL


_JCL_STMT = re.compile(r"^//([A-Z@#$][\w@#$]{0,7})?\s+(JOB|EXEC|DD|PROC)\b\s*(.*)$", re.I)
_LIBRARY_DDS = {"STEPLIB", "JOBLIB", "SYSLIB", "SYSPROC", "SYSEXEC", "DBRMLIB", "SYSLMOD", "ISPPLIB", "ISPMLIB", "ISPSLIB", "ISPTLIB"}
_SYSTEM_PROGRAMS = {"IEFBR14", "IEBGENER", "IEBCOPY", "IDCAMS", "SORT", "ICETOOL", "DFSORT", "SYNCSORT", "IKJEFT01", "IKJEFT1B", "IEBUPDTE", "ADRDSSU", "DSNUTILB", "IEHLIST", "IEBCOMPR", "DFHCSDUP", "IEBEDIT", "ICEGENER", "FTP"}


def _jcl_statements(text: str) -> list[tuple[str | None, str, str, int, int, int]]:
    """(name, verb, operands, start, end, line_start) per statement, continuations joined."""
    out: list[tuple[str | None, str, str, int, int, int]] = []
    offset = 0
    current: list[Any] | None = None
    for line in text.split("\n"):
        start = offset
        offset += len(line) + 1
        body = line[:72].rstrip()
        if body.startswith("//*") or not body.startswith("//"):
            current = None if not body.startswith("//") else current
            continue
        m = _JCL_STMT.match(body)
        if m:
            operands = m.group(3).split(" ", 1)[0] if not m.group(3).startswith("'") else m.group(3)
            current = [m.group(1), m.group(2).upper(), operands, start, start + len(body), start]
            out.append(tuple(current))  # type: ignore[arg-type]
        elif current is not None and out and out[-1][2].endswith(","):
            more = body[2:].strip().split(" ", 1)[0]
            prev = out[-1]
            out[-1] = (prev[0], prev[1], prev[2] + more, prev[3], start + len(body), prev[5])
    return out


def _param(operands: str, key: str) -> str | None:
    m = re.search(rf"(?:^|,){key}=(\([^)]*\)|'[^']*'|[^,]+)", operands, re.I)
    return m.group(1).strip("'") if m else None


class Jcl:
    id = "legacy.jcl"
    version = "1.1.0"
    SUFFIXES = (".jcl", ".prc", ".proc")

    def matches(self, path):
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path, data, blob):
        text = _decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        o = _Out(path, data, blob, text)
        job = None
        job_name = None
        previous = None
        task = None
        statements = _jcl_statements(text)
        for i, (name, verb, operands, a, b, _) in enumerate(statements):
            at = o.at(a, b)
            if verb in ("JOB", "PROC") and name and job is None:
                job_name = name.upper()
                # The library member a scheduler submits is the file's name; it need not equal
                # the JOB statement's name, which keys the Job.
                member = posixpath.splitext(posixpath.basename(path))[0].upper()
                job = o.entity(at, K.JOB, {"framework": "jcl", "name": job_name}, job_name, {"kind": verb.lower(), "member": member}, E1)
            elif verb == "EXEC" and job is not None:
                step = (name or f"STEP{i}").upper()
                pgm = _param(operands, "PGM")
                proc = _param(operands, "PROC") or (operands.split(",", 1)[0] if operands and "=" not in operands.split(",", 1)[0] else None)
                task = o.entity(at, K.TASK, {"framework": "jcl", "job": job_name, "name": step}, step, {"program": pgm.upper()} if pgm else {"procedure": proc.upper()} if proc else {}, E1)
                o.relation(at, job, P.CONTAINS, task, ev=E1)
                if previous is not None:
                    o.relation(at, task, P.DEPENDS_ON, previous, ev=E1)
                previous = task
                if pgm and pgm.upper() not in _SYSTEM_PROGRAMS and not pgm.startswith("*"):
                    o.relation(at, task, P.RUNS, program("mainframe", pgm.upper(), declare=False), ev=E1)
                elif proc:
                    o.relation(at, task, P.RUNS, ref(K.JOB, {"framework": "jcl", "name": proc.upper()}, proc.upper(), declare=False), ev=E1)
                if pgm and pgm.upper() in ("IKJEFT01", "IKJEFT1B"):
                    end = statements[i + 1][3] if i + 1 < len(statements) else len(text)
                    nxt = next((s for s in statements[i + 1:] if s[1] == "EXEC"), None)
                    region = text[b: nxt[3] if nxt else len(text)]
                    for r in re.finditer(r"\bRUN\s+PROGRAM\s*\(\s*([A-Z0-9#@$]+)\s*\)", region, re.I):
                        o.relation(o.at(b + r.start(1), b + r.end(1)), task, P.RUNS, program("mainframe", r.group(1).upper(), declare=False), ev=E1)
            elif verb == "DD" and task is not None:
                dsn = _param(operands, "DSN") or _param(operands, "DSNAME")
                if not dsn or dsn.startswith("&") or dsn.upper().startswith(("SYS1.", "SYS2.", "CEE.", "IGY.", "DSN.", "CBC.", "ISP.")):
                    continue
                if name.upper() in _LIBRARY_DDS:  # load and procedure libraries are code, not data
                    continue
                generation = re.search(r"\(([+-]?\d+)\)$", dsn)
                base = re.sub(r"\([^)]*\)$", "", dsn).upper()
                disp = (_param(operands, "DISP") or "SHR").strip("()").split(",")[0].upper() or "NEW"
                writes = disp in ("NEW", "MOD") or (generation is not None and generation.group(1).startswith("+"))
                o.dataset(at, task, P.WRITES_TO if writes else P.READS_FROM, base, {"platform": "mainframe", **({"gdg": True} if generation else {})})
        if job is None:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no JOB or PROC statement")], parsed=True)
        return o.done()


# --------------------------------------------------------------------- SAS


_SAS_COMMENTS = re.compile(r"/\*.*?\*/|^\s*\*[^;]*;|;\s*\*[^;]*;|%\*[^;]*;", re.S | re.M)
_SAS_MACRO = re.compile(r"%macro\s+([A-Za-z_]\w*)(.*?)%mend\b[^;]*;", re.I | re.S)
_SAS_KEYWORDS = {
    "let", "put", "if", "then", "else", "do", "end", "to", "by", "while", "until", "include", "inc", "macro", "mend", "global", "local", "sysfunc", "qsysfunc", "str", "nrstr",
    "eval", "sysevalf", "scan", "qscan", "substr", "qsubstr", "upcase", "qupcase", "lowcase", "index", "length", "quote", "bquote", "nrbquote", "superq", "unquote", "nrquote",
    "symput", "return", "abort", "goto", "sysexec", "syscall", "cmpres", "trim", "left", "verify", "datatyp", "sysget", "sysprod", "window", "display", "input", "copy", "symdel",
    "symexist", "symglobl", "symlocal", "sysmacexist", "sysmexecname", "sysmacdelete", "syslput", "sysrput", "macro", "ds2", "tslit", "compstor", "keydef", "mbsfunc",
}
_SAS_DATA = re.compile(r"(?:^|;)\s*data\s+([^;]*?);(.*?)(?:\brun\s*;|(?=;\s*(?:data|proc)\s))", re.I | re.S)
_SAS_SET = re.compile(r"(?:^|;)\s*(set|merge|update|modify)\s+([^;]*);", re.I)
_SAS_PROC_SQL = re.compile(r"\bproc\s+sql\b[^;]*;(.*?)\bquit\s*;", re.I | re.S)
_SAS_PROC = re.compile(r"\bproc\s+(\w+)\b([^;]*);", re.I)
_SAS_LIBNAME = re.compile(r"(?:^|;)\s*libname\s+(\w+)\s+(\w+)?", re.I | re.M)
_SAS_INCLUDE = re.compile(r"%include\s+(['\"])([^'\"]+)\1", re.I)
_SAS_MACRO_CALL = re.compile(r"%([A-Za-z_]\w*)\b")


def _sas_names(segment: str, start: int) -> list[tuple[str, int, int]]:
    """Two-level dataset names in a DATA or SET list; options in parentheses skipped."""
    out, depth, i = [], 0, 0
    for m in re.finditer(r"\(|\)|[A-Za-z_&][\w&.]*", segment):
        tok = m.group()
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and "." in tok and not tok.startswith("&") and tok.count(".") == 1:
            lib = tok.split(".")[0].lower()
            if lib not in ("work", "sashelp", "sasuser") and not tok.lower().endswith((".sas7bdat",)):
                # A macro variable in the name stays as a {placeholder}, as f-strings do in Python.
                out.append((re.sub(r"&(\w+)\.?", r"{\1}", tok).lower(), start + m.start(), start + m.end()))
    return out


class Sas:
    id = "legacy.sas"
    version = "1.0.0"

    def matches(self, path):
        return path.lower().endswith(".sas")

    def extract(self, path, data, blob):
        text = _decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        code = _SAS_COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
        o = _Out(path, data, blob, code)
        macros = list(_SAS_MACRO.finditer(code))
        regions = [(program("sas", m.group(1).lower()), m.start(2), m.end(2), m) for m in macros]
        top = code
        for m in macros:
            top = top[: m.start()] + re.sub(r"[^\n]", " ", m.group()) + top[m.end():]
        regions.append((program("sas", path), 0, len(top), None))
        defined = {m.group(1).lower() for m in macros}
        for me, a, b, m in regions:
            body = (top if m is None else code)[a:b]
            if m is not None:
                o.entity(o.at(m.start(1), m.end(1)), K.PROGRAM, me["natural_key"], me["name"], {"kind": "macro", "language": "sas"})
            before = len(o.items)
            self._body(o, me, body, a, path, defined)
            if m is None and len(o.items) > before:
                o.items.insert(before, entity(blob, whole_file(data), K.PROGRAM, me["natural_key"], me["name"], {"kind": "program", "language": "sas"}, E2))
        return o.done()

    def _body(self, o: _Out, me, body: str, offset: int, path: str, defined: set[str]) -> None:
        sql_spans = []
        for s in _SAS_PROC_SQL.finditer(body):
            raw = s.group(1)
            sql = re.sub(r"&\w+\.?", lambda h: "x".ljust(len(h.group()), "_"), raw)
            # A macro variable in a table name stays as a {placeholder}.
            rename = lambda a, b, raw=raw: re.sub(r"&(\w+)\.?", r"{\1}", raw[a:b]).lower() if "&" in raw[a:b] else None
            o.sql(me, sql, offset + s.start(1), kind=K.DATASET, rename=rename)
            sql_spans.append((s.start(), s.end()))
        outside = lambda i: not any(a <= i < b for a, b in sql_spans)
        for d in _SAS_DATA.finditer(body):
            if not outside(d.start()):
                continue
            outs = _sas_names(d.group(1), offset + d.start(1))
            for name, a, b in outs:
                o.resolved += 1
                at = o.at(a, b)
                param = {"parameterized": True} if "{" in name else {}
                o.relation(at, me, P.WRITES_TO, o.entity(at, K.DATASET, {"name": name}, name, {"platform": "sas", **param}), param)
            for s in _SAS_SET.finditer(d.group(2)):
                for name, a, b in _sas_names(s.group(2), offset + d.start(2) + s.start(2)):
                    if any(name == n for n, _, _ in outs):
                        continue
                    o.resolved += 1
                    at = o.at(a, b)
                    param = {"parameterized": True} if "{" in name else {}
                    o.relation(at, me, P.READS_FROM, o.entity(at, K.DATASET, {"name": name}, name, {"platform": "sas", **param}), param)
        for p in _SAS_PROC.finditer(body):
            if p.group(1).lower() == "sql" or not outside(p.start()):
                continue
            for key, predicate in (("data", P.READS_FROM), ("out", P.WRITES_TO), ("base", P.WRITES_TO)):
                v = re.search(rf"\b{key}\s*=\s*([A-Za-z_&][\w&]*\.[\w&]+)", p.group(2), re.I)
                if v and not v.group(1).startswith("&") and v.group(1).split(".")[0].lower() not in ("work", "sashelp", "sasuser"):
                    name = v.group(1).lower()
                    o.resolved += 1
                    at = o.at(offset + p.start(2) + v.start(1), offset + p.start(2) + v.end(1))
                    o.relation(at, me, predicate, o.entity(at, K.DATASET, {"name": name}, name, {"platform": "sas"}))
        for lib in _SAS_LIBNAME.finditer(body):
            name = lib.group(1).lower()
            if name in ("_all_",):
                continue
            engine = (lib.group(2) or "").lower()
            at = o.at(offset + lib.start(1), offset + lib.end(1))
            key = {"name": f"libname:{name}"}
            o.relation(at, me, P.REFERENCES_CONFIG, o.entity(at, K.CONFIG_KEY, key, key["name"], {"source": "sas_libname", **({"engine": engine} if engine and engine not in ("clear", "list") and not engine.startswith(("'", '"')) else {})}))
        for inc in _SAS_INCLUDE.finditer(body):
            target = inc.group(2)
            if "&" in target or target.startswith("/") or ":" in target:
                o.unresolved += 1
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            o.relation(o.at(offset + inc.start(2), offset + inc.end(2)), me, P.CALLS, program("sas", resolved, declare=False))
        for call in _SAS_MACRO_CALL.finditer(body):
            name = call.group(1).lower()
            if name in _SAS_KEYWORDS:
                continue
            if body[max(0, call.start() - 6): call.start()].lower().endswith("macro "):
                continue
            o.relation(o.at(offset + call.start(1), offset + call.end(1)), me, P.CALLS, program("sas", name, declare=False))


# -------------------------------------------------------------- ColdFusion


_CF_FUNCTION_TAG = re.compile(r"<cffunction\b([^>]*)>", re.I)
_CF_REMOTE_SCRIPT = re.compile(r"\bremote\s+(?:[\w.]+\s+)?function\s+(\w+)\s*\(", re.I)
_CF_QUERY = re.compile(r"<cfquery\b([^>]*)>(.*?)</cfquery>", re.I | re.S)
_CF_DS_OPTION = re.compile(r"\bdatasource\s*[=:]\s*['\"]([\w.-]+)['\"]", re.I)
_CF_QUERY_EXECUTE = re.compile(r"\bqueryExecute\s*\(\s*(['\"])(.*?)(?<!\\)\1", re.I | re.S)
_CF_INVOKE = re.compile(r"<cfinvoke\b[^>]*\bcomponent\s*=\s*['\"]([\w.]+)['\"]", re.I)
_CF_CREATE = re.compile(r"\bcreateObject\s*\(\s*['\"]component['\"]\s*,\s*['\"]([\w.]+)['\"]|\bnew\s+([a-z_][\w]*(?:\.[\w]+)+)\s*\(", re.I)
_CF_INCLUDE = re.compile(r"<cfinclude\b[^>]*\btemplate\s*=\s*['\"]([^'\"#]+)['\"]|\binclude\s+['\"]([^'\"#]+\.cfm)['\"]", re.I)
_CF_ATTR = lambda name: re.compile(rf"\b{name}\s*=\s*['\"]([^'\"]*)['\"]", re.I)


def _cf_sql(body: str) -> str:
    body = re.sub(r"<cfqueryparam\b[^>]*>", lambda m: "0".ljust(len(m.group())), body, flags=re.I)
    body = re.sub(r"</?cf\w+\b[^>]*>", lambda m: " " * len(m.group()), body, flags=re.I)
    return re.sub(r"#[^#\n]*#", lambda m: "x".ljust(len(m.group()), "_"), body)


class ColdFusion:
    id = "legacy.cfml"
    version = "1.0.0"

    def matches(self, path):
        return path.lower().endswith((".cfc", ".cfm"))

    def extract(self, path, data, blob):
        text = _decode(data)
        if text is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        code = re.sub(r"<!---.*?--->", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
        o = _Out(path, data, blob, code)
        base = posixpath.basename(path)
        is_component = path.lower().endswith(".cfc")
        name = posixpath.splitext(path)[0].replace("/", ".").lower() if is_component else path
        me = program("cfml", name)
        o.entity(whole_file(data), K.PROGRAM, me["natural_key"], me["name"] if not is_component else name, {"kind": "component" if is_component else "page", "language": "cfml"})
        route_base = "/" + path

        def endpoint(at, route: str, attrs: dict) -> None:
            key = {"component_root": "@unit", "protocol": "http", "method": "ANY", "route": route}
            o.relation(at, me, P.EXPOSES, o.entity(at, K.ENDPOINT, key, f"ANY {route}", {"framework": "coldfusion", **attrs}))

        if is_component:
            for f in _CF_FUNCTION_TAG.finditer(code):
                fname, access = _CF_ATTR("name").search(f.group(1)), _CF_ATTR("access").search(f.group(1))
                if fname and access and access.group(1).lower() == "remote":
                    endpoint(o.at(f.start(), f.end()), f"{route_base}?method={fname.group(1)}", {"handler": fname.group(1)})
            for f in _CF_REMOTE_SCRIPT.finditer(code):
                endpoint(o.at(f.start(1), f.end(1)), f"{route_base}?method={f.group(1)}", {"handler": f.group(1)})
        elif not base.lower().startswith(("application.", "onrequestend.")):
            endpoint(whole_file(data), route_base, {})
        def datasource(ds: str, a: int, b: int) -> None:
            if ds and "#" not in ds:  # #variables# name no fixed datasource
                key = {"name": f"datasource:{ds}"}
                o.relation(o.at(a, b), me, P.REFERENCES_CONFIG, o.entity(o.at(a, b), K.CONFIG_KEY, key, key["name"], {"source": "cfml_datasource"}))

        for q in _CF_QUERY.finditer(code):
            source = _CF_ATTR("datasource").search(q.group(1))
            if source:
                datasource(source.group(1), q.start(1) + source.start(1), q.start(1) + source.end(1))
            o.sql(me, _cf_sql(q.group(2)), q.start(2))
        for q in _CF_QUERY_EXECUTE.finditer(code):
            # queryExecute(sql, params, {datasource = "x"}): the options follow the SQL string
            rest = code[q.end(): q.end() + 400]
            close = re.search(r"\)\s*;|\)\s*>", rest)
            option = _CF_DS_OPTION.search(rest, 0, close.start() if close else len(rest))
            if option:
                datasource(option.group(1), q.end() + option.start(1), q.end() + option.end(1))
            o.sql(me, _cf_sql(q.group(2)), q.start(2))
        targets = [(m.group(1), m) for m in _CF_INVOKE.finditer(code)] + [(m.group(1) or m.group(2), m) for m in _CF_CREATE.finditer(code)]
        # ColdFusion looks a dotted component name up beside the caller, then from the
        # web root and mappings: try every ancestor directory; names that exist survive.
        dirs = posixpath.dirname(path).split("/") if posixpath.dirname(path) else []
        for target, m in targets:
            for depth in range(len(dirs), -1, -1):
                candidate = ".".join(dirs[:depth] + [target]).lower()
                o.relation(o.at(m.start(), m.end()), me, P.CALLS, program("cfml", candidate, declare=False))
        for m in _CF_INCLUDE.finditer(code):
            target = m.group(1) or m.group(2)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target)) if not target.startswith("/") else target.lstrip("/")
            o.relation(o.at(m.start(), m.end()), me, P.CALLS, program("cfml", resolved, declare=False))
        return o.done()


# ---------------------------------------------- Synapse and Data Factory JSON


_SYNAPSE_PATH = re.compile(r"(?:^|/)(pipeline|trigger|dataset|sqlscript|notebook)/[^/]+\.json$")


def _ref_name(value: Any) -> str | None:
    return value.get("referenceName") if isinstance(value, dict) and isinstance(value.get("referenceName"), str) else None


def _plain(value: Any) -> str | None:
    """A literal string, not a Data Factory expression object or `@` expression."""
    return value if isinstance(value, str) and value and not value.startswith("@") else None


class Synapse:
    id = "legacy.synapse"
    version = "1.0.0"

    def matches(self, path):
        return bool(_SYNAPSE_PATH.search(path))

    def extract(self, path, data, blob):
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        props = doc.get("properties") if isinstance(doc, dict) else None
        name = doc.get("name") if isinstance(doc, dict) else None
        if not isinstance(props, dict) or not isinstance(name, str):
            return Extraction(parsed=False)
        folder = _SYNAPSE_PATH.search(path).group(1)
        if folder == "notebook":
            language, cells = notebook_cells(props, data)
            return scan_cells(path, data, blob, language, cells, "synapse", self.id)
        text = data.decode("utf-8-sig")
        o = _Out(path, data, blob, text)
        name_at = find_span(data, json.dumps(name)) or whole_file(data)
        if folder == "pipeline":
            job = o.entity(name_at, K.JOB, {"framework": "synapse", "name": name}, name, {"kind": "pipeline"}, E1)
            activities = props.get("activities") if isinstance(props.get("activities"), list) else []
            self._activities(o, name, job, activities, data)
        elif folder == "trigger":
            kind = props.get("type") if isinstance(props.get("type"), str) else "trigger"
            recurrence = (props.get("typeProperties") or {}).get("recurrence") if isinstance(props.get("typeProperties"), dict) else None
            attrs = {"kind": "trigger", "trigger": kind}
            if isinstance(recurrence, dict) and isinstance(recurrence.get("frequency"), str):
                attrs["schedule"] = f"every {recurrence.get('interval', 1)} {recurrence['frequency']}"
            job = o.entity(name_at, K.JOB, {"framework": "synapse", "name": name}, name, attrs, E1)
            for p in props.get("pipelines") if isinstance(props.get("pipelines"), list) else []:
                target = _ref_name(p.get("pipelineReference")) if isinstance(p, dict) else None
                if target:
                    at = find_span(data, json.dumps(target)) or name_at
                    o.relation(at, job, P.RUNS, ref(K.JOB, {"framework": "synapse", "name": target}, target, declare=False), ev=E1)
        elif folder == "dataset":
            tp = props.get("typeProperties") if isinstance(props.get("typeProperties"), dict) else {}
            me = o.entity(name_at, K.DATASET, {"name": name.lower()}, name.lower(), {"kind": "synapse_dataset", **({"format": props["type"]} if isinstance(props.get("type"), str) else {})}, E1)
            schema, tbl = _plain(tp.get("schema")), _plain(tp.get("table")) or _plain(tp.get("tableName"))
            if tbl:
                full = clean_name(f"{schema}.{tbl}" if schema and "." not in tbl else tbl)
                o.relation(name_at, me, P.ALIAS_OF, o.entity(name_at, K.TABLE, table(full)["natural_key"], full, {}, E1), ev=E1)
            loc = tp.get("location") if isinstance(tp.get("location"), dict) else {}
            parts = [_plain(loc.get(k)) for k in ("fileSystem", "container", "folderPath", "fileName")]
            if not tbl and any(parts):  # ALIAS_OF is single-valued: the table, else the location
                raw = "/".join(p.strip("/") for p in parts if p)
                path_name = dataset_name(raw if "/" in raw else raw + "/")
                if path_name and path_name != name.lower():
                    o.relation(name_at, me, P.ALIAS_OF, o.entity(name_at, K.DATASET, {"name": path_name}, path_name, {}, E1), ev=E1)
        elif folder == "sqlscript":
            content = props.get("content") if isinstance(props.get("content"), dict) else {}
            query = content.get("query") if isinstance(content.get("query"), str) else None
            me = program("tsql", name.lower())
            o.entity(name_at, K.PROGRAM, me["natural_key"], me["name"], {"kind": "script", "language": "sql"})
            if query:
                found = lineage(query, "tsql")
                at = find_span(data, '"query"') or name_at
                o.resolved += len(found.writes) + len(found.reads)
                o.unresolved += found.unresolved
                for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
                    for t, _, _ in items:
                        o.relation(at, me, predicate, o.entity(at, K.TABLE, table(t)["natural_key"], t))
        return o.done()

    def _activities(self, o: _Out, pipeline: str, job, activities: list, data: bytes, prefix: str = "") -> None:
        cursor = 0
        for act in activities:
            if not isinstance(act, dict) or not isinstance(act.get("name"), str):
                continue
            name = prefix + act["name"]
            kind = act.get("type") if isinstance(act.get("type"), str) else "Activity"
            at = find_span(data, json.dumps(act["name"]), cursor) or whole_file(data)
            cursor = at.end_byte
            me = o.entity(at, K.TASK, {"framework": "synapse", "job": pipeline, "name": name}, name, {"type": kind}, E1)
            o.relation(at, job, P.CONTAINS, me, ev=E1)
            for dep in act.get("dependsOn") if isinstance(act.get("dependsOn"), list) else []:
                upstream = dep.get("activity") if isinstance(dep, dict) else None
                if isinstance(upstream, str):
                    o.relation(at, me, P.DEPENDS_ON, ref(K.TASK, {"framework": "synapse", "job": pipeline, "name": prefix + upstream}, prefix + upstream), ev=E1)
            tp = act.get("typeProperties") if isinstance(act.get("typeProperties"), dict) else {}
            for section, predicate in (("inputs", P.READS_FROM), ("outputs", P.WRITES_TO)):
                for item in act.get(section) if isinstance(act.get(section), list) else []:
                    ds = _ref_name(item)
                    if ds:
                        o.resolved += 1
                        o.relation(at, me, predicate, ref(K.DATASET, {"name": ds.lower()}, ds.lower(), declare=False), ev=E1)
            if kind in ("SynapseNotebook",):
                nb = _ref_name(tp.get("notebook"))
                if nb:
                    o.relation(at, me, P.RUNS, ref(K.NOTEBOOK, {"path": posixpath.join(posixpath.dirname(posixpath.dirname(o.path)), "notebook", f"{nb}.json").lstrip("/")}, nb, declare=False), ev=E1)
            elif kind in ("SqlPoolStoredProcedure", "SqlServerStoredProcedure"):
                proc = _plain(tp.get("storedProcedureName"))
                if proc:
                    o.relation(at, me, P.RUNS, program("tsql", clean_name(proc), declare=False), ev=E1)
                else:
                    o.unresolved += 1
            elif kind == "ExecutePipeline":
                target = _ref_name(tp.get("pipeline"))
                if target:
                    o.relation(at, me, P.RUNS, ref(K.JOB, {"framework": "synapse", "name": target}, target, declare=False), ev=E1)
            elif kind == "DatabricksNotebook":
                o.unresolved += 0 if _plain(tp.get("notebookPath")) else 1
            for key in ("sqlReaderQuery", "query"):
                source = tp.get("source") if isinstance(tp.get("source"), dict) else {}
                q = _plain(source.get(key)) or _plain(tp.get(key))
                if q:
                    found = lineage(q, "tsql")
                    o.resolved += len(found.writes) + len(found.reads)
                    for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
                        for t, _, _ in items:
                            o.relation(at, me, predicate, o.entity(at, K.TABLE, table(t)["natural_key"], t))
                    break
            for nested in ("activities", "ifTrueActivities", "ifFalseActivities"):
                if isinstance(tp.get(nested), list):
                    self._activities(o, pipeline, job, tp[nested], data, prefix=f"{name}.")


EXTRACTORS = [Cobol(), Jcl(), Sas(), ColdFusion(), Synapse()]
