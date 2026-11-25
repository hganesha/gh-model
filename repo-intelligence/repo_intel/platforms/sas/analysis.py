"""One SAS file: entities, relations, construct counts and behaviour units.

It produces everything `legacy.sas` did, with the same kinds and natural keys
(Programs for the file and each macro, Datasets keyed by `lib.member`, a
`libname:<ref>` ConfigKey per LIBNAME, CALLS for macro calls and relative
`%INCLUDE`s, SQL lineage of `PROC SQL`), and adds:

- symbolic macro expansion (`repo_intel.platforms.sas.macro`): a dataset named
  through a macro variable the file pins (`%LET lib = rpt;`) gets its real name;
  others keep a `{name}` placeholder, are marked `parameterized`, and are reported;
- CodeLists from `PROC FORMAT` (VALUE, INVALUE, PICTURE), written by the program
  that defines them and read by every FORMAT statement and PUT/INPUT call that
  applies them;
- Reports: an ODS document (`ODS PDF FILE=...`) that the procedures run while it is
  open contribute to; outside ODS, each PROC REPORT, PRINT and TABULATE; a PROC
  EXPORT file; a stored process's streamed output;
- stored processes (a `*ProcessBody;` comment or `%STPBEGIN`): the program, a prompt
  screen and form, and one control per `%GLOBAL` prompt variable;
- DERIVED_FROM between permanent datasets, through WORK datasets of the same file;
- FILENAME filerefs as ConfigKeys (device only, never the path), and `%SYSGET`
  environment variables as ConfigKeys that `config` inputs declare;
- one behaviour unit per DATA step, PROC SQL step, other procedure, macro and open
  code, each with its `kind`.

Paths, connection options and option values are never copied: a library keeps its
engine, a fileref its device, an ODS document or exported file its base name. A URL
anywhere in the output keeps its parameter names but never their values, and mail
addresses are never copied (`values`). What a program under `.rkp/inputs/sas/`
declares is evidence E1; everything else is parsed (E2).
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.behavior import behavior_observation
from repo_intel.compile.contracts import Span
from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.sas.catalog import (
    BUILTIN_FORMATS, FILENAME_DEVICES, MACRO_KEYWORDS, ODS_DOCUMENTS, OUTPUT_PROCS, REPORT_PROCS, SYSTEM_LIBRARIES, is_automatic,
)
from repo_intel.platforms.sas.lexer import Stmt, Tok, lex, tokens, unquote
from repo_intel.platforms.sas.lower import Container, DataStep, Lowerer, skip_parens, split_at, toks_of
from repo_intel.platforms.sas.macro import SENTINEL, Macros, is_dynamic, placeholders, template
from repo_intel.platforms.sas.parse import Step, parse
from repo_intel.platforms.sas.values import scrub, scrub_tree

K = EntityKind
P = Predicate
E1, E2 = EvidenceClass.E1, EvidenceClass.E2
LANGUAGE = "sas"
NATIVE_PREFIX = ".rkp/inputs/sas/"
MAX_DYNAMIC_DIAGNOSTICS = 25
MAX_FORMAT_ENTRIES = 100
MAX_DERIVED = 2000

# Procedure options that name a dataset read or written. PROC COPY's IN= and OUT= are
# libraries and PROC PWENCODE's IN= is a password: neither is listed.
READ_OPTIONS = frozenset({"data", "new", "cntlin", "inest", "datain", "dataset", "score", "innov", "inmodel"})
WRITE_OPTIONS = frozenset({
    "out", "outest", "outstat", "outp", "outs", "outsscp", "outcov", "outfit", "outmodel", "outsurv", "outtrans",
    "outpost", "outcorr", "cntlout", "base", "outstats", "outsum", "outparms", "outvar", "outrank", "outpct",
})
# Procedures with a construct of their own; the rest count as sas:proc_other.
KNOWN_PROCS = frozenset({
    "sql", "sort", "means", "summary", "freq", "report", "print", "tabulate", "transpose", "format", "export", "import", "append",
    "datasets", "contents", "copy", "univariate", "reg", "logistic", "glm", "mixed", "sgplot", "gplot", "ds2", "fcmp", "http",
    "rank", "corr", "compare", "printto", "iml", "pwencode", "sgpanel", "gchart", "surveyselect", "genmod", "catalog",
})
# Procedures whose meaning no core kind carries: counted as not modelled.
UNMAPPED_PROCS = frozenset({"ds2", "fcmp", "http", "iml", "printto", "pwencode", "catalog", "lua", "python", "groovy", "javaobj", "proto", "cas", "casutil"})
_FORMAT = re.compile(r"(\$?)([A-Za-z_][A-Za-z0-9_]*?)(\d*)\.(\d*)$")
_MACRO_IN_STRING = re.compile(r"%([A-Za-z_]\w*)(?![\w&])")
_MACRO_REF = re.compile(r"&+([A-Za-z_]\w*)\.?")
_MACRO_RUN = re.compile(r"(?:&+[A-Za-z_]\w*\.?)+")  # adjacent references SAS resolves together
_MACRO_REF_NAME = re.compile(r"&+([A-Za-z_]\w*)")
_SQL_FORMAT = re.compile(r"\b(?:put[nc]?|input[nc]?)\s*\(\s*[^,()]+,\s*(\$?[A-Za-z_]\w*\.\d*)|\b(?:format|informat)\s*=\s*(\$?[A-Za-z_]\w*\.\d*)", re.I)
_CONNECTION = re.compile(r"\bconnection\s+to\s+(\w+)\s*\(", re.I)
_EXECUTE_BY = re.compile(r"\)\s*by\s+(\w+)\s*;?\s*$", re.I)


def _lib():
    # Imported on first use: this package is loaded while repo_intel.extractors finishes
    # loading (pack discovery), so it must not import that package at import time.
    from repo_intel.extractors import base, sql_lineage

    return base, sql_lineage


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")  # SAS sources are often Windows-encoded
        except UnicodeDecodeError:
            return None


def program_ref(name: str, declare: bool = True) -> dict:
    out = {"kind": "Program", "natural_key": {"platform": "sas", "name": name}, "name": name.rsplit("/", 1)[-1]}
    return out if declare else {**out, "declare": False}


def dataset_ref(name: str) -> dict:
    return {"kind": "Dataset", "natural_key": {"name": name}, "name": name}


def codelist_ref(name: str, declare: bool = True) -> dict:
    out = {"kind": "CodeList", "natural_key": {"platform": "sas", "name": name}, "name": name}
    return out if declare else {**out, "declare": False}


def config_ref(name: str, declare: bool = True) -> dict:
    out = {"kind": "ConfigKey", "natural_key": {"name": name}, "name": name}
    return out if declare else {**out, "declare": False}


def report_ref(name: str) -> dict:
    return {"kind": "Report", "natural_key": {"platform": "sas", "name": name}, "name": name}


def basename(path: str) -> str:
    """The last part of a path; for a URL, of the URL without user information, query values or fragment."""
    path = scrub(path)
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


@dataclass
class DsName:
    name: str  # lower case; "work.x" for a one-level name; placeholders for what stays dynamic
    tok: Tok
    permanent: bool
    dynamic: bool
    library: str | None
    expr: dict

    @property
    def label(self) -> str:
        return self.name


@dataclass
class Doc:
    """An ODS document (or a stored process's output stream) while it is open."""

    name: str
    term: str
    destination: str
    at: tuple[int, int]
    owner: dict
    dynamic: bool = False
    procs: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    title: str | None = None


class Analysis:
    def __init__(self, path: str, data: bytes, blob: str, text: str):
        self.base, self.sqlmod = _lib()
        self.path, self.data, self.blob, self.text = path, data, blob, text
        self.lexed = lex(text)
        self.prog = parse(self.lexed)
        self.stmts = self.prog.stmts
        self.macros = Macros(self.prog)
        self.clean = self.macros.text
        self.to_byte = self.sqlmod.byte_offsets(text, data)
        self.newlines = [m.start() for m in re.finditer(b"\n", data)]
        self.entities: list = []
        self.relations: list = []
        self.behaviors: list = []
        self.constructs: list[tuple[int, int, str, bool]] = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self.file_ref = program_ref(path)
        self.file_used = False
        self.native = path.startswith(NATIVE_PREFIX)
        # A program under .rkp/inputs/sas/ (autoexec.sas, config.sas) is configuration the
        # owner exported: what it holds is declared (E1), not parsed from application code.
        self.ev = E1 if self.native else E2
        self.is_stp = self.lexed.stored_process_marker is not None or any(t.kind == "mac" and t.low == "%stpbegin" for st in self.stmts for t in st.toks)
        self.step_nodes: dict[int, list[dict]] = {}
        self.derivations: list[tuple[list[DsName], list[DsName]]] = []
        self.docs: dict[str, Doc] = {}
        self.ods_events: list[tuple[int, str, str, Doc | None]] = []  # (position, "open"/"close", destination, doc)
        self.titles: list[tuple[int, str]] = []
        self.filerefs: dict[str, str] = {}  # fileref -> base name of its literal path
        self.report_counts: dict[tuple[str, str], int] = {}
        self.produced: set[tuple[str, str]] = set()
        self.seen_relations: set[tuple] = set()

    # ------------------------------------------------------------------ positions and emission

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def span(self, a: int, b: int):
        """`base.span`, with lines found by bisection so a file costs linear time, not lines x items."""
        start, end = self.to_byte(a), self.to_byte(max(a, b))
        return Span(start_byte=start, end_byte=end, start_line=bisect.bisect_left(self.newlines, start) + 1, end_line=bisect.bisect_left(self.newlines, max(start, end - 1)) + 1)

    def line(self, a: int) -> int:
        return bisect.bisect_left(self.newlines, self.to_byte(a)) + 1

    def text_between(self, a: int, b: int) -> str:
        return self.clean[a:b].strip().rstrip(";").strip()

    def owner(self, scope: int | None) -> dict:
        return self.file_ref if scope is None else program_ref(self.prog.macros[scope].name)

    def owner_name(self, owner: dict) -> str:
        return owner["natural_key"]["name"]

    def entity(self, term: str, key: dict, name: str, attrs: dict, a: int, b: int) -> None:
        self.entities.append(platform_entity(self.blob, self.span(a, b), term, key, name, scrub_tree(attrs), self.ev))

    def relation(self, subject: dict, predicate: Predicate, obj: dict, a: int, b: int, qualifiers: dict | None = None) -> None:
        if subject is self.file_ref or subject == self.file_ref:
            self.file_used = True
        self.relations.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, self.ev))

    def construct(self, a: int, b: int, construct_id: str, modelled: bool) -> None:
        self.constructs.append((a, b, construct_id, modelled))

    def diagnostic(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic("sas.source", severity, scrub(message)))

    def unit(self, a: int, b: int, owner: dict, body: list, kind: str, params: list | None = None) -> None:
        """One behaviour unit; `kind` (data_step, sql_step, proc_step, macro, open_code) tells apart the units of one owner."""
        try:
            # Literals copied from the source keep a URL's parameter names, never its values (rule 7).
            self.behaviors.append(behavior_observation(self.blob, self.span(a, b), owner, LANGUAGE, scrub_tree(body), scrub_tree(params), kind=kind))
        except ValueError as exc:  # a lowering bug must not lose the rest of the file
            self.diagnostic("error", f"behaviour unit at line {self.line(a)} dropped: {exc}")

    # ------------------------------------------------------------------ macro text

    def expand(self, text: str, scope: int | None, pos: int) -> str:
        if "&" not in text:
            return text
        out = self.macros.expand(text, scope, pos)
        if out is None:
            self.diagnostic("info", f"macro expansion of {text[:60]!r} exceeds its bounds and stays dynamic")
            return _MACRO_REF.sub(lambda m: SENTINEL + m.group(1).lower() + SENTINEL, text)
        return out

    def template(self, tok: Tok, scope: int | None) -> dict:
        """A token as a behaviour expression: literal, or a template of what stays dynamic."""
        if tok.kind == "str":
            body = unquote(tok)
            if tok.text[0] == "'":
                return {"e": "lit", "value": body, "type": "string"}
            return template(self.expand(body, scope, tok.start))
        return template(self.expand(tok.text, scope, tok.start))

    def macro_value(self, text: str, scope: int | None, pos: int) -> dict:
        """A %LET value or macro argument: a macro call, or text with references resolved."""
        if text.startswith("%"):
            from repo_intel.platforms.sas.expr import ExprError, Parser

            try:
                return Parser(tokens(text, pos), macro_mode=True).parse()
            except (ExprError, RecursionError):
                pass
        return template(self.expand(text, scope, pos))

    # ------------------------------------------------------------------ datasets

    def names_from_text(self, raw: str, tok: Tok, scope: int | None) -> list[DsName]:
        if raw.lower() in ("_null_", "_data_", "_last_", "_all_"):
            return []
        expanded = self.expand(raw, scope, tok.start)
        out = []
        for piece in expanded.split():
            parts = piece.split(".")
            if len(parts) == 1:
                lib, member = "work", parts[0]
            elif len(parts) == 2:
                lib, member = parts
            else:
                continue  # a catalog entry (lib.cat.entry), not a dataset
            if not lib or not member or not re.match(r"[\w\x00{}]", member):
                continue
            dynamic = is_dynamic(piece)
            name = placeholders(f"{lib}.{member}").lower()
            library = placeholders(lib).lower()
            if library in SYSTEM_LIBRARIES and library != "work":
                continue
            expr = template(f"{lib}.{member}".lower()) if dynamic else {"e": "name", "name": name, "kind": "dataset"}
            out.append(DsName(name, tok, library != "work", dynamic, None if is_dynamic(lib) else library, expr))
        return out

    def ds_names(self, tok: Tok, scope: int | None) -> list[DsName]:
        raw = unquote(tok) if tok.kind == "str" else tok.text
        return self.names_from_text(raw, tok, scope)

    def dataset_use(self, ds: DsName, role: str, owner: dict, construct_id: str) -> None:
        if role not in ("read", "write") or not ds.permanent:
            return
        attrs: dict[str, Any] = {}
        param = {"parameterized": True} if ds.dynamic else {}
        if "&" in ds.tok.text and not ds.dynamic:
            attrs["macro_resolved"] = True
        a, b = ds.tok.start, ds.tok.end
        self.resolved += 1
        self.entity("sas:Dataset", {"name": ds.name}, ds.name, {**attrs, **param}, a, b)
        self.relation(owner, P.READS_FROM if role == "read" else P.WRITES_TO, dataset_ref(ds.name), a, b, param)
        if ds.library and ("lib", self.owner_name(owner), ds.library) not in self.seen_relations:
            self.seen_relations.add(("lib", self.owner_name(owner), ds.library))
            # The library is allocated elsewhere (autoexec, another program): kept only when declared.
            self.relation(owner, P.REFERENCES_CONFIG, config_ref(f"libname:{ds.library}", declare=False), a, b)

    def dynamic_list(self, tok: Tok, scope: int | None, role: str) -> None:
        self.unresolved += 1
        self.construct(tok.start, tok.end, "sas:dataset_list", False)
        self.diagnostic("info", f"dataset list {tok.text!r} ({role}) names its members only at run time")

    def physical_file(self, tok: Tok, role: str) -> None:
        self.unresolved += 1
        self.construct(tok.start, tok.end, "sas:dataset_list", False)

    # ------------------------------------------------------------------ formats

    def use_format(self, text: str, tok: Tok, owner: dict) -> None:
        m = _FORMAT.match(text)
        if not m or m.group(1) + m.group(2) == "":
            return
        name = (m.group(1) + m.group(2)).lower()
        if name in BUILTIN_FORMATS or name.lstrip("$") in BUILTIN_FORMATS:
            return
        self.construct(tok.start, tok.end, "sas:format_use", True)
        # Defined by PROC FORMAT in this or another file of the unit, or not kept.
        self.relation(owner, P.READS_FROM, codelist_ref(name, declare=False), tok.start, tok.end)

    def format_tokens(self, toks: list[Tok], owner: dict) -> None:
        """Every format named in a FORMAT, INFORMAT or ATTRIB statement or a FORMAT= option."""
        j = 0
        while j < len(toks):
            t = toks[j]
            if t.kind == "op" and t.text == "$" and j + 1 < len(toks) and toks[j + 1].kind == "word" and toks[j + 1].start == t.end:
                self.use_format("$" + toks[j + 1].text, toks[j + 1], owner)
                j += 2
                continue
            if t.kind == "word" and "." in t.text and not t.low.startswith(("first.", "last.")) and _FORMAT.match(t.text):
                self.use_format(t.text, t, owner)
            j += 1

    def format_statement(self, toks: list[Tok], owner: dict) -> None:
        self.format_tokens(toks[1:], owner)

    def function(self, name: str, raw: list[Tok], tok: Tok, scope: int | None, owner: dict) -> None:
        if name in ("put", "putc", "putn", "input", "inputc", "inputn") and raw:
            depth, comma = 0, None
            for j, t in enumerate(raw):
                depth += {"(": 1, ")": -1}.get(t.text, 0)
                if t.text == "," and depth == 0:
                    comma = j
                    break
            if comma is not None:
                rest = raw[comma + 1 :]
                if rest and rest[0].kind == "str":
                    self.use_format(unquote(rest[0]), rest[0], owner)
                else:
                    self.format_tokens(rest, owner)
        elif name == "sysget" and raw and raw[0].kind == "str":
            self.environment(unquote(raw[0]), raw[0], owner)

    def environment(self, name: str, tok: Tok, owner: dict) -> None:
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            return
        self.construct(tok.start, tok.end, "sas:sysget", True)
        self.relation(owner, P.REFERENCES_CONFIG, config_ref(f"sas_env:{name.upper()}", declare=False), tok.start, tok.end)

    # ------------------------------------------------------------------ files, includes, calls

    def file_target(self, tok: Tok | None, scope: int | None, owner: dict) -> dict:
        if tok is None:
            return {"e": "lit", "value": "", "type": "string"}
        if tok.kind == "str":
            expanded = self.expand(unquote(tok), scope, tok.start)
            return template(basename(expanded))
        if tok.kind == "word" and tok.low not in ("print", "log", "datalines", "cards", "datalines4", "cards4") and "&" not in tok.text:
            self.relation(owner, P.REFERENCES_CONFIG, config_ref(f"filename:{tok.low}", declare=False), tok.start, tok.end)
        return {"e": "name", "name": tok.low, "kind": "fileref"}

    def include_target(self, toks: list[Tok], scope: int | None) -> tuple[dict, str | None, Tok | None]:
        """The %INCLUDE target as an expression, the repository path it names (or None) and its token."""
        target = next((t for t in toks[1:] if t.kind in ("str", "word")), None)
        if target is None:
            return {"e": "lit", "value": "", "type": "string"}, None, None
        if target.kind == "word":
            return {"e": "name", "name": target.low, "kind": "fileref"}, None, target
        expanded = self.expand(unquote(target), scope, target.start)
        expr = template(expanded)
        if is_dynamic(expanded) or expanded.startswith(("/", "\\")) or ":" in expanded:
            return expr, None, target
        return expr, posixpath.normpath(posixpath.join(posixpath.dirname(self.path), expanded.replace("\\", "/"))), target

    def include(self, toks: list[Tok], st: Stmt, scope: int | None, owner: dict) -> dict:
        return self.include_target(toks, scope)[0]

    def call_execute(self, toks: list[Tok], st: Stmt, scope: int | None, owner: dict) -> None:
        self.unresolved += 1
        self.diagnostic("info", f"CALL EXECUTE at line {self.line(st.start)} generates code at run time; only macros it names literally are linked")
        for t in toks:
            if t.kind == "str":
                for m in _MACRO_IN_STRING.finditer(unquote(t)):
                    name = m.group(1).lower()
                    if name not in MACRO_KEYWORDS:
                        self.relation(owner, P.CALLS, program_ref(name, declare=False), t.start, t.end)

    def hash_datasets(self, toks: list[Tok], scope: int | None, owner: dict, inputs: list) -> None:
        for j in range(len(toks) - 2):
            if toks[j].low == "dataset" and toks[j + 1].text == ":" and toks[j + 2].kind == "str":
                raw = self.expand(unquote(toks[j + 2]), scope, toks[j + 2].start)
                names = self.names_from_text(raw.split("(")[0], toks[j + 2], None)
                for ds in names:
                    self.dataset_use(ds, "read", owner, "sas:hash_object")
                    inputs.append(ds)

    def dynamic_macro_call(self, tok: Tok, scope: int | None) -> None:
        pass  # reported by the token pass (`scan_tokens`)

    # ------------------------------------------------------------------ pass 1: tokens

    def scan_tokens(self) -> None:
        for st in self.stmts:
            owner = self.owner(st.scope)
            toks = st.toks
            for idx, t in enumerate(toks):
                if t.kind == "mac" and "&" in t.text:  # %&which, %load_&kind: named at run time
                    expanded = self.expand(t.text[1:], st.scope, t.start)
                    if is_dynamic(expanded):
                        self.unresolved += 1
                        self.construct(t.start, t.end, "sas:macro_call", False)
                        self.diagnostic("info", f"macro call {t.text} at line {self.line(t.start)} names its macro at run time")
                    else:
                        self.construct(t.start, t.end, "sas:macro_call", True)
                        self.relation(owner, P.CALLS, program_ref(expanded.lower(), declare=False), t.start, t.end)
                elif t.kind == "mac":
                    name = t.low[1:]
                    if name in ("sysfunc", "qsysfunc"):
                        self.construct(t.start, t.end, "sas:sysfunc", True)
                    elif name == "sysget" and idx + 2 < len(toks) and toks[idx + 1].text == "(" and toks[idx + 2].kind == "word":
                        self.environment(toks[idx + 2].text, toks[idx + 2], owner)
                    if name in MACRO_KEYWORDS:
                        continue
                    if idx + 1 < len(toks) and toks[idx + 1].text == ":" and idx == 0:
                        continue  # a %label:
                    self.construct(t.start, t.end, "sas:macro_call", True)
                    self.relation(owner, P.CALLS, program_ref(name, declare=False), t.start, t.end)
                elif t.kind == "str" and t.text[0] == '"':
                    for m in _MACRO_IN_STRING.finditer(t.text):
                        name = m.group(1).lower()
                        if name in ("sysfunc", "qsysfunc"):
                            self.construct(t.start, t.end, "sas:sysfunc", True)
                        if name not in MACRO_KEYWORDS:
                            self.construct(t.start, t.end, "sas:macro_call", True)
                            self.relation(owner, P.CALLS, program_ref(name, declare=False), t.start, t.end)
                if "&" in t.text and (t.kind == "word" or (t.kind == "str" and t.text[0] == '"')):
                    for m in _MACRO_REF.finditer(t.text):
                        expanded = self.expand(m.group(), st.scope, t.start)
                        self.construct(t.start, t.end, "sas:macro_variable_ref", not is_dynamic(expanded))

    # ------------------------------------------------------------------ pass 2: global statements

    def global_statements(self) -> None:
        for st in self.stmts:
            toks = toks_of(st)
            if not toks:
                continue
            owner = self.owner(st.scope)
            kind = st.kind
            a, b = toks[0].start, st.end
            if kind == "global:libname":
                self.libname(toks, st, owner)
            elif kind == "global:filename":
                self.filename(toks, st, owner)
            elif kind == "global:ods":
                self.ods(toks, st, owner)
            elif kind in ("global:title", "global:footnote"):
                self.construct(a, b, "sas:title_footnote", True)
                if kind == "global:title" and toks[0].low in ("title", "title1"):
                    text = next((t for t in toks[1:] if t.kind == "str"), None)
                    self.titles.append((st.start, placeholders(self.expand(unquote(text), st.scope, text.start)) if text else ""))
            elif kind in ("global:options", "global:option"):
                self.construct(a, b, "sas:options", False)
            elif kind == "global:x":
                self.construct(a, b, "sas:x_command", False)
                self.diagnostic("info", f"X statement at line {self.line(a)} runs an operating-system command (not recorded)")
            elif kind == "global:endsas":
                self.construct(a, b, "sas:endsas", True)
            elif kind in ("global:signon", "global:signoff", "global:rsubmit", "global:endrsubmit"):
                self.construct(a, b, "sas:sas_connect", False)
            elif kind == "global:dm":
                self.construct(a, b, "sas:dm_statement", False)
            elif kind.startswith("global:"):
                self.construct(a, b, "sas:global_other", False)
            elif kind == "m:include":
                self.include_facts(toks, st, owner)
            elif kind in ("stp:begin", "stp:end"):
                # Counted as a construct where it is lowered (`Lowerer.macro_statement`).
                if kind == "stp:begin":
                    name = f"{self.path}#output"
                    doc = self.docs.get(name) or Doc(name, "sas:StoredProcessOutput", "_webout", (a, b), owner)
                    self.docs[name] = doc
                    self.ods_events.append((st.start, "open", "_webout", doc))
                    self.produce(owner, name, a, b)
                else:
                    self.ods_events.append((st.start, "close", "_webout", None))

    def libname(self, toks: list[Tok], st: Stmt, owner: dict) -> None:
        a, b = toks[0].start, st.end
        if len(toks) < 2 or toks[1].kind != "word":
            return
        raw = toks[1]
        ref = self.expand(raw.text, st.scope, raw.start)
        if is_dynamic(ref) or ref.lower() == "_all_":
            if is_dynamic(ref):
                self.unresolved += 1
                self.construct(a, b, "sas:libname", False)
                self.diagnostic("info", f"LIBNAME {raw.text} names its library at run time")
            return
        self.construct(a, b, "sas:libname", True)
        name = ref.lower()
        attrs: dict[str, Any] = {"source": "sas_libname"}
        if len(toks) > 2:
            e = toks[2]
            follows = toks[3] if len(toks) > 3 else None
            if e.kind == "word" and not (follows is not None and follows.text == "=") and "&" not in e.text:
                if e.low in ("clear", "list"):
                    attrs["action"] = e.low
                else:
                    attrs["engine"] = e.low
            if e.text == "(" or (follows is not None and follows.text == "(" and e.kind == "word" and "engine" in attrs):
                start = 2 if e.text == "(" else 3
                end = skip_parens(toks, start)
                members = [t.low for t in toks[start + 1 : end - 1] if t.kind == "word"]
                if members:
                    attrs["concatenates"] = members
        if self.native:
            attrs["declared_in"] = basename(self.path)
        key = {"name": f"libname:{name}"}
        self.entity("sas:Library", key, key["name"], attrs, raw.start, raw.end)
        self.relation(owner, P.REFERENCES_CONFIG, config_ref(key["name"]), raw.start, raw.end)

    def filename(self, toks: list[Tok], st: Stmt, owner: dict) -> None:
        a, b = toks[0].start, st.end
        if len(toks) < 2 or toks[1].kind != "word" or "&" in toks[1].text:
            self.construct(a, b, "sas:filename", False)
            return
        ref = toks[1].low
        device = "disk"
        path_tok = None
        rest = toks[2:]
        if rest and rest[0].kind == "word" and rest[0].low in FILENAME_DEVICES:
            device = rest[0].low
            rest = rest[1:]
        if rest and rest[0].low == "clear":
            self.construct(a, b, "sas:filename", True)
            return
        if rest and rest[0].kind == "str":
            path_tok = rest[0]
        self.construct(a, b, "sas:filename", True)
        if device == "pipe":
            self.construct(a, b, "sas:filename_pipe", False)
            self.diagnostic("info", f"FILENAME {ref} PIPE at line {self.line(a)} runs an operating-system command (not recorded)")
        if path_tok is not None and device in ("disk", "temp"):
            expanded = self.expand(unquote(path_tok), st.scope, path_tok.start)
            self.filerefs[ref] = placeholders(basename(expanded))
        attrs = {"source": "sas_filename", "device": device}
        key = {"name": f"filename:{ref}"}
        self.entity("sas:Fileref", key, key["name"], attrs, toks[1].start, toks[1].end)
        self.relation(owner, P.REFERENCES_CONFIG, config_ref(key["name"]), toks[1].start, toks[1].end)

    def include_facts(self, toks: list[Tok], st: Stmt, owner: dict) -> None:
        a, b = toks[0].start, st.end
        expr, target, tok = self.include_target(toks, st.scope)
        if target is None:
            self.unresolved += 1
            self.construct(a, b, "sas:include", False)
            if tok is not None:
                why = "a fileref" if tok.kind == "word" else "a path set at run time" if is_dynamic(self.expand(unquote(tok), st.scope, tok.start)) else "an absolute path, which the repository layout does not show"
                self.diagnostic("info", f"%INCLUDE at line {self.line(a)} names {why}: not linked")
            return
        self.construct(a, b, "sas:include", True)
        self.relation(owner, P.CALLS, program_ref(target, declare=False), tok.start + 1, tok.end - 1)

    def ods(self, toks: list[Tok], st: Stmt, owner: dict) -> None:
        a, b = toks[0].start, st.end
        if len(toks) < 2:
            return
        dest = toks[1].low
        words = {t.low for t in toks[2:]}
        if dest == "_all_" and "close" in words:
            self.construct(a, b, "sas:ods_destination", True)
            self.ods_events.append((st.start, "close", "_all_", None))
            return
        if dest == "output":
            self.construct(a, b, "sas:ods_output", True)
            for j in range(2, len(toks) - 2):
                if toks[j + 1].text == "=" and toks[j + 2].kind == "word":
                    for ds in self.ds_names(toks[j + 2], st.scope):
                        self.dataset_use(ds, "write", owner, "sas:ods_output")
            return
        if dest in ODS_DOCUMENTS or dest.startswith("tagsets."):
            if "close" in words:
                self.construct(a, b, "sas:ods_destination", True)
                self.ods_events.append((st.start, "close", dest, None))
                return
            file_tok = None
            for j in range(2, len(toks) - 2):
                if toks[j].low in ("file", "body") and toks[j + 1].text == "=":
                    file_tok = toks[j + 2]
                    break
            if file_tok is None:
                self.construct(a, b, "sas:ods_destination", False)  # the default location: no named document
                return
            self.construct(a, b, "sas:ods_destination", True)
            if file_tok.kind == "str":
                expanded = self.expand(unquote(file_tok), st.scope, file_tok.start)
                name, dynamic = placeholders(basename(expanded)), is_dynamic(expanded)
            elif file_tok.low == "_webout":
                name, dynamic = f"{self.path}#output", False
            else:
                name, dynamic = self.filerefs.get(file_tok.low, file_tok.low), "&" in file_tok.text
                self.relation(owner, P.REFERENCES_CONFIG, config_ref(f"filename:{file_tok.low}", declare=False), file_tok.start, file_tok.end)
            term = "sas:StoredProcessOutput" if name.endswith("#output") else "sas:OdsDocument"
            doc = self.docs.get(name) or Doc(name, term, dest, (file_tok.start, file_tok.end), owner, dynamic)
            doc.title = doc.title or self.title_at(st.start)
            self.docs[name] = doc
            self.ods_events.append((st.start, "open", dest, doc))
            self.produce(owner, name, file_tok.start, file_tok.end)
            return
        self.construct(a, b, "sas:ods_other", False)

    def title_at(self, pos: int) -> str | None:
        current = None
        for p, text in self.titles:
            if p > pos:
                break
            current = text
        return current or None

    # ------------------------------------------------------------------ pass 3: steps

    def open_docs(self, pos: int, state: dict, cursor: list[int]) -> list[Doc]:
        events = self.ods_events
        while cursor[0] < len(events) and events[cursor[0]][0] < pos:
            _, action, dest, doc = events[cursor[0]]
            if action == "open":
                state[dest] = doc
            elif dest == "_all_":
                state.clear()
            else:
                state.pop(dest, None)
            cursor[0] += 1
        return list(state.values())

    def steps(self) -> None:
        self.ods_events.sort(key=lambda e: e[0])
        self.titles.sort()
        state: dict[str, Doc] = {}
        cursor = [0]
        for index, step in enumerate(self.prog.steps):
            owner = self.owner(step.scope)
            docs = self.open_docs(step.start, state, cursor)
            if step.kind == "data":
                self.data_step(index, step, owner)
            elif step.proc == "sql":
                self.proc_sql(index, step, owner, docs)
            elif step.proc == "format":
                self.proc_format(index, step, owner)
            else:
                self.proc(index, step, owner, docs)

    def data_step(self, index: int, step: Step, owner: dict) -> None:
        lowerer = DataStep(self, step, owner)
        loop = lowerer.lower()
        self.unit(step.start, step.end, owner, [loop], "data_step")
        self.derivations.append((lowerer.outputs, lowerer.inputs))
        if step.scope is None:
            self.file_used = True
        self.step_nodes[index] = [{
            "op": "platform", "construct": "sas:data_step", "at": self.at(step.start, step.end),
            "detail": {"unit": self.at(step.start, step.end), "outputs": [n.label for n in lowerer.outputs], "inputs": [n.label for n in lowerer.inputs]},
        }]

    def options(self, toks: list[Tok]) -> list[tuple[str, Tok]]:
        """`key=value` pairs at parenthesis depth 0; the value's token."""
        out, depth, j = [], 0, 0
        while j < len(toks):
            t = toks[j]
            if t.text in ("(", "{", "["):
                depth += 1
            elif t.text in (")", "}", "]"):
                depth -= 1
            elif depth == 0 and t.kind == "word" and j + 2 < len(toks) + 1 and j + 1 < len(toks) and toks[j + 1].text == "=" and j + 2 < len(toks):
                value = toks[j + 2]
                if value.text == "$" and j + 3 < len(toks):
                    value = Tok("word", "$" + toks[j + 3].text, value.start, toks[j + 3].end)
                out.append((t.low, value))
                j += 3
                continue
            j += 1
        return out

    def proc(self, index: int, step: Step, owner: dict, docs: list[Doc]) -> None:
        name = step.proc
        header_st = self.stmts[step.header]
        header = toks_of(header_st)
        cid = f"sas:proc_{name}" if name in KNOWN_PROCS else "sas:proc_other"
        self.construct(header[0].start, header_st.end, cid, name not in UNMAPPED_PROCS)
        if step.scope is None:
            self.file_used = True
        reads: list[DsName] = []
        writes: list[DsName] = []
        named: dict[str, dict] = {}
        export_file: Tok | None = None
        dbms = None
        inner = [self.stmts[j] for j in step.stmts[1:] if self.stmts[j].kind not in ("run", "quit")]

        def use(key: str, value: Tok, scope: int | None) -> None:
            if value.kind not in ("word",) and not (value.kind == "str" and value.text.lower().endswith("n")):
                return
            role = "read" if key in READ_OPTIONS else "write" if key in WRITE_OPTIONS else None
            if role is None:
                return
            for ds in self.ds_names(value, scope):
                self.dataset_use(ds, role, owner, cid)
                (reads if role == "read" else writes).append(ds)
                named.setdefault(key, ds.expr)

        for key, value in self.options(header[2:]):
            if name == "copy" and key in ("in", "out", "inlib", "outlib"):
                if value.kind == "word" and "&" not in value.text and value.low not in SYSTEM_LIBRARIES:
                    self.relation(owner, P.REFERENCES_CONFIG, config_ref(f"libname:{value.low}", declare=False), value.start, value.end)
                continue
            if name == "export" and key == "outfile":
                export_file = value
                continue
            if key == "dbms" and value.kind == "word":
                dbms = value.low
            if name in ("pwencode", "http") or (name == "import" and key == "datafile"):
                continue  # never a dataset; never copied
            if key in ("format",):
                self.format_tokens([value], owner)
            use(key, value, step.scope)
        for st in inner:
            toks = toks_of(st)
            if not toks:
                continue
            w = toks[0].low
            if w in ("format", "informat"):
                self.format_statement(toks, owner)
                continue
            if name == "fcmp" and w in ("function", "subroutine"):
                self.construct(toks[0].start, st.end, "sas:fcmp_function", False)
                continue
            if name == "datasets" and w in ("delete", "change", "modify", "age", "exchange", "save", "copy"):
                self.construct(toks[0].start, st.end, "sas:datasets_maintenance", False)
                continue
            for key, value in self.options(toks[1:]):
                if key in ("format", "f"):
                    self.format_tokens([value], owner)
                elif name not in ("pwencode", "http"):
                    use(key, value, st.scope)
            if name == "report" and w == "define":
                self.format_tokens(toks, owner)
        if name == "sort" and not any(k == "out" for k, _ in self.options(header[2:])):
            for ds in list(reads):
                self.dataset_use(ds, "write", owner, cid)  # sorted in place
                writes.append(ds)
        if reads or writes:
            self.derivations.append((writes, reads))
        noprint = any(t.low == "noprint" for t in header)
        if name == "export" and export_file is not None:
            self.export_report(export_file, step, owner, reads, dbms)
        elif name in OUTPUT_PROCS and not noprint:
            if docs:
                for doc in docs:
                    doc.procs.add(name)
                    doc.sources.update(ds.name for ds in reads if ds.permanent)
                    self.produce(owner, doc.name, header[0].start, header_st.end)
            elif name in REPORT_PROCS:
                self.standalone_report(name, step, owner, reads)
        call: dict[str, Any] = {"e": "call", "callee": {"e": "name", "name": name or "unknown", "kind": "procedure"}, "args": []}
        if named:
            call["named"] = named
        node: dict[str, Any] = {"op": "call", "at": self.at(step.start, step.end), "call": call}
        if name in UNMAPPED_PROCS:
            node = {"op": "platform", "construct": cid, "at": self.at(step.start, step.end), "detail": {"procedure": name}}
        self.proc_reference(index, step, name or "unknown")
        self.unit(step.start, step.end, owner, [node], "proc_step")

    def proc_reference(self, index: int, step: Step, name: str) -> None:
        """What open code or the enclosing macro holds for a procedure: a reference to the
        procedure's own unit, as for DATA steps and PROC SQL, so its call is stated once."""
        at = self.at(step.start, step.end)
        self.step_nodes[index] = [{"op": "platform", "construct": "sas:proc_step", "at": at, "detail": {"unit": at, "proc": name}}]

    def produce(self, owner: dict, report: str, a: int, b: int) -> None:
        key = (self.owner_name(owner), report)
        if key not in self.produced:
            self.produced.add(key)
            self.relation(owner, P.PRODUCES, report_ref(report), a, b)

    def standalone_report(self, proc: str, step: Step, owner: dict, reads: list[DsName]) -> None:
        owner_name = self.owner_name(owner)
        n = self.report_counts.get((owner_name, proc), 0) + 1
        self.report_counts[(owner_name, proc)] = n
        name = f"{owner_name}#{proc}{n}"
        attrs: dict[str, Any] = {"procedure": proc, "destination": "listing"}
        sources = sorted({ds.name for ds in reads if ds.permanent})
        if sources:
            attrs["sources"] = sources
        title = self.title_at(step.start)
        if title:
            attrs["title"] = title
        header = self.stmts[step.header]
        self.entity("sas:Report", {"platform": "sas", "name": name}, name, attrs, header.start, header.end)
        self.produce(owner, name, header.start, header.end)

    def export_report(self, tok: Tok, step: Step, owner: dict, reads: list[DsName], dbms: str | None) -> None:
        if tok.kind == "str":
            expanded = self.expand(unquote(tok), step.scope, tok.start)
            name, dynamic = placeholders(basename(expanded)), is_dynamic(expanded)
        else:
            name, dynamic = self.filerefs.get(tok.low, tok.low), "&" in tok.text
        attrs: dict[str, Any] = {"procedure": "export"}
        if dbms:
            attrs["format"] = dbms
        sources = sorted({ds.name for ds in reads if ds.permanent})
        if sources:
            attrs["sources"] = sources
        if dynamic:
            attrs["parameterized"] = True
        self.entity("sas:ExportFile", {"platform": "sas", "name": name}, name, attrs, tok.start, tok.end)
        self.produce(owner, name, tok.start, tok.end)

    # ------------------------------------------------------------------ PROC SQL

    def proc_sql(self, index: int, step: Step, owner: dict, docs: list[Doc]) -> None:
        header = self.stmts[step.header]
        self.construct(header.start, header.end, "sas:proc_sql", True)
        if step.scope is None:
            self.file_used = True
        body: list[dict] = []
        all_reads: list[str] = []
        all_writes: list[str] = []
        prints = False
        for j in step.stmts[1:]:
            st = self.stmts[j]
            toks = toks_of(st)
            if not toks or st.kind in ("run", "quit"):
                continue
            w = toks[0].low
            at = self.at(toks[0].start, st.end)
            if toks[0].kind == "mac":
                lowerer = Lowerer(self, st.scope, owner, data_step=False)
                lowerer.entries = [st]
                nodes, _ = lowerer.statement(toks, st, 1)
                body += nodes
                continue
            if w in ("connect", "disconnect"):
                engine = toks[2].low if len(toks) > 2 and toks[1].low in ("to", "from") else ""
                self.construct(toks[0].start, st.end, "sas:sql_passthrough", False)
                body.append({"op": "platform", "construct": "sas:sql_passthrough", "at": at, "detail": {"action": w, "engine": engine}})
                continue  # never the text: CONNECT carries credentials
            if st.kind.startswith("global:") or w in ("reset", "validate", "describe"):
                continue
            raw = self.clean[toks[0].start : st.end].rstrip().rstrip(";")
            by = _EXECUTE_BY.search(raw) if w == "execute" else None
            if by:
                # EXECUTE (...) BY engine runs wholly in the database: not a query of this
                # program, and its text names the database's tables, not SAS datasets.
                engine = by.group(1).lower()
                self.construct(toks[0].start, st.end, "sas:sql_passthrough", False)
                self.diagnostic("info", f"EXECUTE BY {engine} at line {self.line(toks[0].start)} runs in the database: what it reads and writes is not linked")
                body.append({"op": "platform", "construct": "sas:sql_passthrough", "at": at, "detail": {"action": "execute", "engine": engine}})
                continue
            reads, writes, node = self.sql_statement(raw, toks[0].start, st, owner, at)
            body.append(node)
            all_reads += [d.name for d in reads]
            all_writes += [d.name for d in writes]
            if reads or writes:
                self.derivations.append((writes, reads))
            if w == "select" and not writes:
                prints = True
        if prints:
            for doc in docs:
                doc.procs.add("sql")
                self.produce(owner, doc.name, header.start, header.end)
        self.unit(step.start, step.end, owner, body, "sql_step")
        self.step_nodes[index] = [{
            "op": "platform", "construct": "sas:proc_sql", "at": self.at(step.start, step.end),
            "detail": {"unit": self.at(step.start, step.end), "writes": sorted(set(all_writes)), "reads": sorted(set(all_reads))},
        }]

    def sql_statement(self, raw: str, offset: int, st: Stmt, owner: dict, at: list[int]) -> tuple[list[DsName], list[DsName], dict]:
        scope = st.scope
        engine = None
        masked = sas_sql(raw)
        pos = 0
        while (c := _CONNECTION.search(masked, pos)) is not None:
            engine = engine or c.group(1).lower()
            close = skip_parens_text(masked, c.end() - 1)
            # The database-side query is the database's own text, in its own dialect, naming
            # database tables rather than SAS datasets: lineage sees a subquery, and the
            # tables it names are not linked (no libref says which SAS library they are).
            masked = masked[: c.start()] + "(select 1)".ljust(close - c.start()) + masked[close:]
            pos = c.start() + 1
            self.construct(offset + c.start(), offset + close, "sas:sql_passthrough", False)
            self.diagnostic("info", f"pass-through query to {c.group(1).lower()} at line {self.line(offset + c.start())} runs in the database: the tables it reads are not linked")
        found = self.sqlmod.lineage(masked)
        self.unresolved += found.unresolved
        reads, writes = [], []
        for role, items in (("write", found.writes), ("read", found.reads)):
            for _, a, b in items:
                text = re.sub(r"[\"`\[\]]", "", raw[a:b])
                tok = Tok("word", text, offset + a, offset + b)
                for ds in self.names_from_text(text, tok, scope):
                    if ds.library == "dictionary":
                        continue
                    self.dataset_use(ds, role, owner, "sas:proc_sql")
                    (writes if role == "write" else reads).append(ds)
        if "dictionary." in raw.lower():
            self.construct(offset, offset + len(raw), "sas:dictionary_tables", False)
        if re.search(r"\binto\s*:", raw, re.I):
            self.construct(offset, offset + len(raw), "sas:sql_into_macro", True)
        for m in _SQL_FORMAT.finditer(raw):
            fmt = m.group(1) or m.group(2)
            g = 1 if m.group(1) else 2
            self.use_format(fmt, Tok("word", fmt, offset + m.start(g), offset + m.end(g)), owner)
        params: list[dict] = []
        seen: dict[str, dict] = {}

        def parameter(m: re.Match) -> str:
            # A run of references (`&&v&i`, `&a.&b`) is expanded as one, as SAS rescans it.
            expanded = self.expand(m.group(), scope, offset + m.start())
            if not is_dynamic(expanded):
                return expanded
            # What stays dynamic is text SAS substitutes before the query runs, not a bind
            # parameter: the param is `inline`, named after the variables the run references
            # and valued by what they expand to (one variable, or a template of several).
            value = template(expanded)
            parts = value.get("parts", [])
            if len(parts) == 1 and isinstance(parts[0], dict):
                value = parts[0]
            name = "_".join(n.lower() for n in _MACRO_REF_NAME.findall(m.group()))
            base, n = name, 1
            while name in seen and seen[name] != value:
                n += 1
                name = f"{base}_{n}"
            if name not in seen:
                seen[name] = value
                params.append({"name": name, "value": value, "type": "string", "inline": True})
            return ":" + name

        # URLs in the text keep their parameter names, never values (rule 7). Scrubbed before
        # parameters are made, so a reference in a query value never becomes one.
        sql = re.sub(r"\s+", " ", _MACRO_RUN.sub(parameter, scrub(raw))).strip()
        node: dict[str, Any] = {"op": "query", "at": at, "sql": sql, "params": params}
        if engine:
            # Part of the text runs in that database: consumers read no table names from it.
            node["datasource"] = engine
            node["remote"] = engine
        if writes:
            node["target"] = writes[0].expr
        return reads, writes, node

    # ------------------------------------------------------------------ PROC FORMAT

    def proc_format(self, index: int, step: Step, owner: dict) -> None:
        header_st = self.stmts[step.header]
        header = toks_of(header_st)
        self.construct(header[0].start, header_st.end, "sas:proc_format", True)
        if step.scope is None:
            self.file_used = True
        library = None
        reads: list[DsName] = []
        writes: list[DsName] = []
        for key, value in self.options(header[2:]):
            if key in ("library", "lib") and value.kind == "word":
                library = value.low
            elif key == "cntlin" and value.kind == "word":
                self.construct(value.start, value.end, "sas:format_cntlin", False)
                self.diagnostic("info", f"PROC FORMAT CNTLIN={value.text} builds formats from data: their names are not in the source")
                for ds in self.ds_names(value, step.scope):
                    self.dataset_use(ds, "read", owner, "sas:proc_format")
                    reads.append(ds)
            elif key == "cntlout" and value.kind == "word":
                for ds in self.ds_names(value, step.scope):
                    self.dataset_use(ds, "write", owner, "sas:proc_format")
                    writes.append(ds)
        defined: list[str] = []
        for j in step.stmts[1:]:
            st = self.stmts[j]
            toks = toks_of(st)
            if not toks or toks[0].low not in ("value", "invalue", "picture"):
                continue
            name = self.format_definition(toks, st, owner, library)
            if name:
                defined.append(name)
        if reads or writes:
            self.derivations.append((writes, reads))
        node = {"op": "call", "at": self.at(step.start, step.end), "call": {"e": "call", "callee": {"e": "name", "name": "format", "kind": "procedure"}, "args": [{"e": "lit", "value": n, "type": "string"} for n in defined]}}
        self.proc_reference(index, step, "format")
        self.unit(step.start, step.end, owner, [node], "proc_step")

    def format_definition(self, toks: list[Tok], st: Stmt, owner: dict, library: str | None) -> str | None:
        kind = toks[0].low
        j = 1
        character = False
        if j < len(toks) and toks[j].text == "$":
            character = True
            j += 1
        if j >= len(toks) or toks[j].kind != "word":
            self.construct(toks[0].start, st.end, "sas:format_definition", False)
            return None
        name_tok = toks[j]
        expanded = self.expand(name_tok.text, st.scope, name_tok.start)
        dynamic = is_dynamic(expanded)
        name = ("$" if character else "") + placeholders(expanded).lower()
        j += 1
        if j < len(toks) and toks[j].text == "(":
            j = skip_parens(toks, j)
        entries: list[dict[str, str]] = []
        count = 0
        cur: list[Tok] = []
        while j < len(toks):
            t = toks[j]
            if t.text == "=" and cur:
                label_tok = toks[j + 1] if j + 1 < len(toks) else None
                j += 2
                if label_tok is not None and label_tok.text == "[":  # a nested format: [date9.]
                    k = j
                    while k < len(toks) and toks[k].text != "]":
                        k += 1
                    label = "[" + "".join(t.text for t in toks[j:k]) + "]"
                    j = k + 1
                else:
                    label = unquote(label_tok) if label_tok is not None else ""
                count += 1
                if len(entries) < MAX_FORMAT_ENTRIES:
                    code = " ".join(unquote(t) if t.kind == "str" else t.text for t in cur)
                    entries.append({"code": code.replace(" , ", ", ").replace("- <", "-<").replace("< -", "<-"), "label": label})
                cur = []
                continue
            if t.text == "(" and not cur:  # options after a picture label: (prefix='$')
                j = skip_parens(toks, j)
                continue
            cur.append(t)
            j += 1
        term = {"value": "sas:Format", "invalue": "sas:Informat", "picture": "sas:PictureFormat"}[kind]
        attrs: dict[str, Any] = {"format_type": kind, "character": character, "entry_count": count, "entries": entries}
        if library:
            attrs["library"] = library
        if dynamic:
            attrs["parameterized"] = True
        self.construct(toks[0].start, st.end, "sas:format_definition", True)
        self.entity(term, {"platform": "sas", "name": name}, name, attrs, name_tok.start, name_tok.end)
        self.relation(owner, P.WRITES_TO, codelist_ref(name), name_tok.start, name_tok.end)
        return name

    # ------------------------------------------------------------------ containers

    def container_node(self, toks: list[Tok], st: Stmt, scope: int | None, owner: dict) -> list[dict]:
        at = self.at(toks[0].start, st.end)
        kind = st.kind
        if kind in ("null", "run", "quit"):
            return []
        if kind == "global:libname":
            return [{"op": "platform", "construct": "sas:libname", "at": at, "detail": {"libref": toks[1].low if len(toks) > 1 else ""}}]
        if kind == "global:filename":
            return [{"op": "platform", "construct": "sas:filename", "at": at, "detail": {"fileref": toks[1].low if len(toks) > 1 else ""}}]
        if kind == "global:ods":
            return [{"op": "platform", "construct": "sas:ods_destination", "at": at, "detail": {"destination": toks[1].low if len(toks) > 1 else "", "close": any(t.low == "close" for t in toks)}}]
        if kind == "global:endsas":
            return [{"op": "exit", "at": at}]
        if kind == "global:x":
            return [{"op": "platform", "construct": "sas:x_command", "at": at, "detail": {}}]
        if kind.startswith("global:"):
            return [{"op": "platform", "construct": "sas:global_statement", "at": at, "detail": {"statement": kind.split(":", 1)[1]}}]
        self.construct(toks[0].start, st.end, "sas:unparsed_statement", False)
        return [{"op": "platform", "construct": "sas:unparsed_statement", "at": at, "detail": {"keyword": toks[0].low[:40]}}]

    def step_or_macro_node(self, kind: str, index: int, owner: dict) -> list[dict]:
        if kind == "step":
            return self.step_nodes.get(index, [])
        return []  # a nested macro definition has a unit of its own

    # ------------------------------------------------------------------ macros and open code

    def macro_units(self) -> None:
        for index, m in enumerate(self.prog.macros):
            ref = program_ref(m.name)
            self.construct(m.start, m.end, "sas:macro_definition", True)
            attrs: dict[str, Any] = {"kind": "macro", "language": LANGUAGE, "parameters": [p.name for p in m.params]}
            self.entity("sas:Macro", ref["natural_key"], m.name, attrs, m.name_tok.start, m.name_tok.end)
            body = Container(self, index, ref, m.items).lower()
            params = [{"name": p.name, "type": "string", "required": not p.keyword, **({"default": self.macro_value(p.default, index, m.start)} if p.default else {})} for p in m.params]
            self.unit(m.start, m.end, ref, body, "macro", params)

    def prompts(self) -> list[tuple[str, Tok]]:
        out, seen = [], set()
        for st in self.stmts:
            if st.kind == "m:global" and st.scope is None:
                for t in st.toks[1:]:
                    if t.kind == "word" and not is_automatic(t.low) and t.low not in seen:
                        seen.add(t.low)
                        out.append((t.low, t))
        return out

    def stored_process(self) -> None:
        marker = self.lexed.stored_process_marker
        a, b = marker if marker else (0, 0)
        self.construct(a, b, "sas:stored_process", True)
        prompts = self.prompts()
        if not prompts:
            return
        path = self.path
        screen = {"platform": "sas", "name": path}
        form = {"screen": path, "name": "prompts"}
        first = prompts[0][1]
        self.entity("sas:PromptPage", screen, basename(path), {"stored_process": path}, first.start, first.end)
        self.entity("sas:PromptGroup", form, "prompts", {"stored_process": path}, first.start, first.end)
        screen_ref = {"kind": "Screen", "natural_key": screen, "name": basename(path)}
        form_ref = {"kind": "Form", "natural_key": form, "name": "prompts"}
        self.relations.append(self.base.relation(self.blob, self.span(first.start, first.end), screen_ref, P.CONTAINS, form_ref, {}, self.ev))
        self.relations.append(self.base.relation(self.blob, self.span(first.start, first.end), form_ref, P.SUBMITS_TO, self.file_ref, {}, self.ev))
        for name, tok in prompts:
            self.construct(tok.start, tok.end, "sas:stp_prompt", True)
            key = {"container": f"{path}#prompts", "name": name}
            self.entity("sas:Prompt", key, name, {"macro_variable": name, "control": "prompt"}, tok.start, tok.end)
            self.relations.append(self.base.relation(self.blob, self.span(tok.start, tok.end), form_ref, P.CONTAINS, {"kind": "UIControl", "natural_key": key, "name": name}, {}, self.ev))
        self.file_used = True

    def open_code(self) -> None:
        if not self.file_used:
            return
        body = Container(self, None, self.file_ref, self.prog.items).lower()
        params = [{"name": n, "type": "string", "required": False} for n, _ in self.prompts()] if self.is_stp else []
        self.unit(0, len(self.text), self.file_ref, body, "open_code", params)

    # ------------------------------------------------------------------ reports and lineage

    def emit_reports(self) -> None:
        for doc in self.docs.values():
            attrs: dict[str, Any] = {"destination": doc.destination}
            if doc.procs:
                attrs["procedures"] = sorted(doc.procs)
            if doc.sources:
                attrs["sources"] = sorted(doc.sources)
            if doc.title:
                attrs["title"] = doc.title
            if doc.dynamic:
                attrs["parameterized"] = True
            self.entity(doc.term, {"platform": "sas", "name": doc.name}, doc.name, attrs, *doc.at)

    def derived_from(self) -> None:
        producers: dict[str, list[DsName]] = {}
        first_write: dict[str, DsName] = {}
        for outs, ins in self.derivations:
            for o in outs:
                producers.setdefault(o.name, []).extend(i for i in ins if i.name != o.name)
                first_write.setdefault(o.name, o)
        emitted: set[tuple[str, str]] = set()
        for name, out in first_write.items():
            if not out.permanent:
                continue
            stack, seen = list(producers.get(name, [])), {name}
            while stack and len(emitted) < MAX_DERIVED:
                src = stack.pop()
                if src.name in seen:
                    continue
                seen.add(src.name)
                if src.permanent:
                    if (name, src.name) not in emitted:
                        emitted.add((name, src.name))
                        self.relations.append(self.base.relation(self.blob, self.span(out.tok.start, out.tok.end), dataset_ref(name), P.DERIVED_FROM, dataset_ref(src.name), {"through_work": True} if src not in producers.get(name, []) else {}, self.ev))
                else:
                    stack.extend(producers.get(src.name, []))

    # ------------------------------------------------------------------ the whole file

    def run(self):
        self.scan_tokens()
        self.global_statements()
        self.steps()
        self.macro_units()
        if self.is_stp:
            self.stored_process()
        if any(st.scope is None and st.kind not in ("null", "m:let", "m:global", "m:local", "m:put", "global:options", "global:option", "global:title", "global:footnote", "m:macro", "m:mend") for st in self.stmts):
            self.file_used = True
        self.open_code()
        self.emit_reports()
        self.derived_from()
        names = sorted(self.macros.dynamic)
        for n in names[:MAX_DYNAMIC_DIAGNOSTICS]:
            self.diagnostic("info", f"macro variable &{n} stays dynamic: {self.macros.dynamic[n]}")
        if len(names) > MAX_DYNAMIC_DIAGNOSTICS:
            self.diagnostic("info", f"{len(names) - MAX_DYNAMIC_DIAGNOSTICS} more macro variables stay dynamic")
        items = []
        # Every file with code is a Program: one holding only macro definitions is still
        # what a %INCLUDE or a job names (legacy.sas declared it only for open code).
        if self.stmts:
            term = "sas:StoredProcess" if self.is_stp else "sas:Program"
            attrs: dict[str, Any] = {"kind": "program", "language": LANGUAGE}
            if self.is_stp:
                attrs["stored_process"] = True
            if not self.file_used and self.prog.macros:
                attrs["macro_library"] = True
            if self.native:
                role = basename(self.path).lower().split(".")[0]
                attrs["role"] = "autoexec" if role.startswith("autoexec") else "config" if role.startswith("config") else "input"
                self.construct(0, 0, "sas:autoexec", True)
            items.append(platform_entity(self.blob, self.base.whole_file(self.data), term, self.file_ref["natural_key"], self.file_ref["name"], attrs, self.ev))
        items += self.entities + self.relations + self.behaviors
        items += [construct_observation(self.blob, self.span(a, b), cid, modelled, evidence_class=self.ev) for a, b, cid, modelled in self.constructs]
        items += self.sqlmod.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return self.base.Extraction(items, self.diagnostics)


_SQL_COLUMN_OPTION = re.compile(r"\b(?:format|informat|label|length|transcode)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\$?[A-Za-z_]\w*\.\d*|\$?\d+\.?\d*)", re.I)
_SQL_FORMAT_ARG = re.compile(r"(,\s*)(\$?[A-Za-z_]\w*?\d*\.\d*)(?=\s*\))")
_SQL_DATASET_OPTIONS = re.compile(r"(?<=[\w}])\s*\(\s*(?:keep|drop|where|rename|obs|firstobs|in|alter|pw|read|write|index|compress|sortedby|bufsize|label|type|genmax)\s*=", re.I)
_SQL_SAS_WORDS = re.compile(r"\bcalculated\b", re.I)


def sas_sql(raw: str) -> str:
    """PROC SQL text as SQL a generic parser reads, every character offset kept: macro
    references and calls masked as identifiers, column options (FORMAT=, LABEL=),
    format arguments of PUT and INPUT, dataset options and CALCULATED blanked."""
    out = _MACRO_REF.sub(lambda m: "x".ljust(len(m.group()), "_"), raw)
    out = re.sub(r"%[A-Za-z_]\w*", lambda m: "x".ljust(len(m.group()), "_"), out)
    blank = lambda m: " " * len(m.group())  # noqa: E731
    out = _SQL_COLUMN_OPTION.sub(blank, out)
    out = _SQL_FORMAT_ARG.sub(lambda m: m.group(1) + "'" + "f" * (len(m.group(2)) - 2) + "'", out)  # formats are at least 2 characters
    out = _SQL_SAS_WORDS.sub(blank, out)
    for m in reversed(list(_SQL_DATASET_OPTIONS.finditer(out))):
        open_at = out.index("(", m.start())
        close = skip_parens_text(out, open_at)
        out = out[:open_at] + " " * (close - open_at) + out[close:]
    return out


def skip_parens_text(text: str, i: int) -> int:
    """Index after the balanced parentheses that open at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)
