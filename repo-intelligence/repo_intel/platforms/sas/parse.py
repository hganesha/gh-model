"""Group SAS statements into steps, macro definitions and scopes.

A step is a DATA step or a PROC step: it starts at `DATA` or `PROC` and ends at
`RUN;` or `QUIT;`, at the next step, or at the end of the macro definition it
started in. `PROC SQL`, `PROC DATASETS` and the other run-group procedures end
only at `QUIT;` (or the next step). Macro definitions nest. Each scope (open code,
or one macro definition) keeps its own ordered list of items: steps, nested
macro definitions and statements outside any step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repo_intel.platforms.sas.catalog import GLOBAL_STATEMENTS, MACRO_FUNCTIONS, MACRO_STATEMENTS
from repo_intel.platforms.sas.lexer import Lexed, Stmt, Tok

# Procedures in which RUN; runs a group of statements and the step goes on until QUIT;.
EMBEDDED_LANGUAGES = frozenset({"ds2", "fedsql", "lua", "python", "iml"})  # procedures with a language of their own
RUN_GROUP = frozenset({"sql", "datasets", "catalog", "iml", "reg", "glm", "anova", "arima", "plot", "gplot", "gchart", "gmap", "fsedit", "fsview", "optmodel", "casutil", "cas", "fedsql", "ds2", "lua", "python", "http"})
_TRAILING_DIGITS = re.compile(r"\d+$")
_PAREN_KEYWORDS = frozenset({"if", "select", "when", "where", "do", "until", "while", "output", "return", "put", "set", "otherwise", "call", "retain", "array", "delete", "else", "abort", "stop", "by"})


@dataclass
class Param:
    name: str
    keyword: bool
    default: str | None  # the default's text, for keyword parameters


@dataclass
class MacroDef:
    name: str
    name_tok: Tok
    params: list[Param]
    start: int
    end: int
    header: int  # statement index of %MACRO
    parent: int | None
    items: list[tuple[str, int]] = field(default_factory=list)  # ("stmt"|"step"|"macro", index)
    local_names: set[str] = field(default_factory=set)
    closed: bool = False


@dataclass
class Step:
    kind: str  # "data" or "proc"
    proc: str  # procedure name, lower case; "" for a DATA step
    header: int
    stmts: list[int]
    scope: int | None
    start: int
    end: int
    terminator: str = ""  # "run", "quit" or "" (ended by the next step or the end of the file)


@dataclass
class Program:
    lexed: Lexed
    stmts: list[Stmt]
    steps: list[Step]
    macros: list[MacroDef]
    items: list[tuple[str, int]]  # open code


def classify(st: Stmt) -> str:
    toks = st.toks
    t0 = toks[0]
    nxt = toks[1] if len(toks) > 1 else None
    if t0.kind == "mac":
        name = t0.low[1:]
        if nxt is not None and nxt.text == ":" and name not in MACRO_STATEMENTS:
            return "m:label"
        if name in MACRO_STATEMENTS:
            return "m:" + {"inc": "include", "go": "goto"}.get(name, name)
        if name in ("stpbegin", "stpend"):
            return "stp:" + name[3:]
        if name in MACRO_FUNCTIONS:
            return "mexpr"
        return "mcall"
    if t0.kind == "op":
        return "null" if t0.text == ";" else "stmt"
    if t0.kind != "word":
        return "stmt"
    if nxt is not None and nxt.text == "=":
        return "assign"
    if nxt is not None and nxt.text in ("(", "{", "[") and t0.low not in _PAREN_KEYWORDS:  # `a{i} = x`: an array element
        depth = 0
        for j in range(1, len(toks)):
            if toks[j].text in ("(", "{", "["):
                depth += 1
            elif toks[j].text in (")", "}", "]"):
                depth -= 1
                if depth == 0:
                    if j + 1 < len(toks) and toks[j + 1].text == "=":
                        return "assign"
                    break
    w = t0.low
    if w in ("data", "proc", "run", "quit"):
        return w
    base = _TRAILING_DIGITS.sub("", w)
    if base in GLOBAL_STATEMENTS and base not in ("run", "quit"):
        if base == "x" and not (nxt is not None and nxt.kind in ("str", "word")):
            return "stmt"
        return "global:" + base
    return "stmt"


def _macro_header(st: Stmt) -> tuple[Tok, list[Param]] | None:
    toks = st.words()
    if len(toks) < 2 or toks[1].kind != "word" or "&" in toks[1].text or "." in toks[1].text:
        return None
    params: list[Param] = []
    if len(toks) > 2 and toks[2].text == "(":
        depth, cur = 0, []
        for t in toks[2:]:
            if t.text == "(":
                depth += 1
                if depth == 1:
                    continue
            elif t.text == ")":
                depth -= 1
                if depth == 0:
                    if cur:
                        params.append(_param(cur))
                    break
            if depth == 1 and t.text == ",":
                if cur:
                    params.append(_param(cur))
                cur = []
                continue
            cur.append(t)
    return toks[1], params


def _param(toks: list[Tok]) -> Param:
    name = toks[0].low
    if len(toks) > 1 and toks[1].text == "=":
        return Param(name, True, " ".join(t.text for t in toks[2:]) or None)
    return Param(name, False, None)


def parse(lexed: Lexed) -> Program:
    stmts = lexed.stmts
    steps: list[Step] = []
    macros: list[MacroDef] = []
    top: list[tuple[str, int]] = []
    stack: list[int] = []  # open macro definitions
    depth: dict[int | None, int] = {}  # %DO depth per scope
    cur: Step | None = None

    def items(scope: int | None) -> list[tuple[str, int]]:
        return top if scope is None else macros[scope].items

    def close(end_stmt: int | None = None, how: str = "") -> None:
        nonlocal cur
        if cur is not None:
            if end_stmt is not None:
                cur.stmts.append(end_stmt)
                stmts[end_stmt].step = len(steps) - 1
            cur.end = stmts[cur.stmts[-1]].end
            cur.terminator = how
        cur = None

    for i, st in enumerate(stmts):
        st.kind = classify(st)
        st.key = st.kind if st.kind.startswith(("m:", "global:", "stp:")) else (st.toks[0].low if st.toks[0].kind == "word" else st.kind)
        scope = stack[-1] if stack else None
        if st.kind == "m:macro":
            close()
            header = _macro_header(st)
            st.scope = scope
            if header is None:
                st.kind = "stmt"
                items(scope).append(("stmt", i))
                continue
            name_tok, params = header
            macros.append(MacroDef(name_tok.low, name_tok, params, st.start, st.end, i, scope, local_names={p.name for p in params}))
            items(scope).append(("macro", len(macros) - 1))
            stack.append(len(macros) - 1)
            continue
        if st.kind == "m:mend":
            close()
            if stack:
                index = stack.pop()
                macros[index].end, macros[index].closed = st.end, True
                st.scope = index
            else:
                st.scope = None
            continue
        st.scope = scope
        d = depth.get(scope, 0)
        if st.kind == "m:end":
            d = max(0, d - 1)
        st.cond = d > 0 or st.kind in ("m:if", "m:else")
        if st.kind == "m:do" or (st.kind in ("m:if", "m:else") and any(t.kind == "mac" and t.low == "%do" for t in st.toks)):
            d += 1
        depth[scope] = d
        if st.kind == "data" and cur is not None and cur.proc in EMBEDDED_LANGUAGES and cur.scope == scope:
            st.kind = "stmt"  # DS2's own DATA ... ENDDATA block, inside the procedure until QUIT
        if st.kind in ("data", "proc"):
            close()
            proc = ""
            if st.kind == "proc":
                words = st.words()
                proc = words[1].low if len(words) > 1 and words[1].kind == "word" else ""
            cur = Step(st.kind, proc, i, [i], scope, st.start, st.end)
            steps.append(cur)
            st.step = len(steps) - 1
            items(scope).append(("step", len(steps) - 1))
            continue
        if st.kind in ("run", "quit"):
            if cur is not None and (st.kind == "quit" or cur.proc not in RUN_GROUP):
                close(i, st.kind)
                continue
            if cur is not None:
                cur.stmts.append(i)
                st.step = len(steps) - 1
                continue
            items(scope).append(("stmt", i))
            continue
        if cur is not None and cur.scope == scope:
            cur.stmts.append(i)
            st.step = len(steps) - 1
            continue
        close()
        items(scope).append(("stmt", i))
    close()
    for m in macros:
        if not m.closed:
            m.end = stmts[-1].end if stmts else m.end
    for st in stmts:
        if st.step is not None:
            step = steps[st.step]
            step.end = max(step.end, st.end)
    return Program(lexed, stmts, steps, macros, top)
