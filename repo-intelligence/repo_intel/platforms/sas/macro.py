"""Symbolic macro expansion: resolve `&name` from what the file itself states, never by guessing.

SAS resolves macro variables at run time, so most references cannot be read from
source. A reference resolves here only when the file pins its value:

- the variable is assigned exactly once in the scope that is read (open code, or the
  macro definition the reference sits in), by `%LET` with a literal value, outside
  any `%IF` or `%DO` block, before the reference;
- nothing else in the file can assign it: not `CALL SYMPUT`, not `SELECT ... INTO :`,
  not a `%LET` inside a macro that may reach the global scope, and no `%LET` or
  `CALL SYMPUT` through a computed name anywhere in the file;
- inside a macro, the variable is not a parameter or `%DO` index, and the `%LET` is in
  the same macro (a global set in open code may differ when another program calls it).

A literal value is text with no macro call or function other than the quoting
functions (`%STR`, `%NRSTR`, ...); `&` references inside it resolve by the same rules.
Every other reference stays dynamic: it is kept as a placeholder (`{month}` in names,
a template expression in behaviour) and reported once, with the reason.

`&&` and `&name.` follow SAS: each `&&` becomes `&` and the text is scanned again;
a `.` right after a name ends the reference and is dropped. Expansion is bounded
(passes, nesting and output size), so it stays linear in the size of the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from repo_intel.platforms.sas.catalog import QUOTING, is_automatic
from repo_intel.platforms.sas.parse import Program

SENTINEL = "\x00"  # an unresolved reference inside expanded text: \x00name\x00
MAX_PASSES = 10
MAX_DEPTH = 10
MAX_LENGTH = 4096
_NAME = re.compile(r"[A-Za-z_]\w*")
_UNRESOLVED = re.compile(SENTINEL + r"(\w+)" + SENTINEL + r"|&+(\w+)\.?")
_QUOTED = re.compile(r"%(" + "|".join(sorted(QUOTING)) + r")\s*\((.*)\)\s*$", re.S | re.I)


@dataclass
class Assignment:
    pos: int
    scope: int | None
    kind: str  # "let" (literal or not), "runtime"
    raw: str | None = None  # the %LET value text
    conditional: bool = False
    value: str | None = None
    done: bool = False


class Macros:
    def __init__(self, program: Program):
        self.program = program
        self.text = program.lexed.clean()
        self.assignments: dict[tuple[int | None, str], list[Assignment]] = {}
        self.indirect = False  # a %LET or CALL SYMPUT through a computed name exists
        self.dynamic: dict[str, str] = {}  # name -> why its references stay dynamic (first reason)
        self.globals_declared: dict[int, set[str]] = {}
        self.do_index: dict[int | None, set[str]] = {}
        self._collect()

    # ------------------------------------------------------------------ assignments

    def _add(self, scope: int | None, name: str, a: Assignment) -> None:
        self.assignments.setdefault((scope, name), []).append(a)

    def _collect(self) -> None:
        p = self.program
        for m_index, m in enumerate(p.macros):
            self.globals_declared[m_index] = set()
        for st in p.stmts:
            toks = st.toks
            if st.kind == "m:global" and st.scope is not None:
                self.globals_declared[st.scope].update(t.low for t in toks[1:] if t.kind == "word")
            elif st.kind == "m:local" and st.scope is not None:
                p.macros[st.scope].local_names.update(t.low for t in toks[1:] if t.kind == "word")
        for st in p.stmts:
            toks = st.toks
            for i, t in enumerate(toks):
                if t.kind == "mac" and t.low == "%let":
                    self._let(st, i)
                elif t.kind == "mac" and t.low == "%do" and i + 2 < len(toks) and toks[i + 1].kind == "word" and toks[i + 2].text == "=":
                    self.do_index.setdefault(st.scope, set()).add(toks[i + 1].low)
                    self._runtime(st.scope, toks[i + 1].low, t.start)
                elif t.kind == "word" and t.low in ("symput", "symputx", "symputn") and i > 0 and toks[i - 1].low == "call":
                    self._symput(st, i)
                elif t.kind == "word" and t.low == "into" and st.step is not None and p.steps[st.step].proc == "sql":
                    self._into(st, i)
                elif t.kind == "mac" and t.low in ("%input", "%syscall", "%window"):
                    for w in toks[i + 1 :]:
                        if w.kind == "word":
                            self._runtime(st.scope, w.low, w.start)

    def _runtime(self, scope: int | None, name: str, pos: int) -> None:
        self._add(scope, name, Assignment(pos, scope, "runtime"))
        if scope is not None and name not in self.program.macros[scope].local_names:
            self._add(None, name, Assignment(pos, scope, "runtime"))  # may reach the global scope

    def _let(self, st, i: int) -> None:
        toks = st.toks
        if i + 1 >= len(toks) or toks[i + 1].kind != "word":
            return
        name_tok = toks[i + 1]
        if "&" in name_tok.text:
            self.indirect = True
            return
        eq = next((j for j in range(i + 2, len(toks)) if toks[j].text == "="), None)
        if eq is None:
            return
        end = toks[-1].start if toks[-1].text == ";" else toks[-1].end
        raw = self.text[toks[eq].end : end].strip()
        name = name_tok.low
        conditional = st.cond or i > 0
        scope = st.scope
        a = Assignment(toks[i].start, scope, "let", raw, conditional)
        self._add(scope, name, a)
        if scope is not None and name not in self.program.macros[scope].local_names:
            # %LET inside a macro updates an existing global variable, so the global scope
            # cannot rely on its own %LET any more.
            self._add(None, name, Assignment(toks[i].start, scope, "runtime"))

    def _symput(self, st, i: int) -> None:
        toks = st.toks
        if i + 2 < len(toks) and toks[i + 1].text == "(" and toks[i + 2].kind == "str" and toks[i + 3 : i + 4] and toks[i + 3].text == ",":
            from repo_intel.platforms.sas.lexer import unquote

            name = unquote(toks[i + 2]).strip().lower()
            if _NAME.fullmatch(name):
                self._runtime(st.scope, name, toks[i].start)
                return
        self.indirect = True

    def _into(self, st, i: int) -> None:
        toks = st.toks
        for j in range(i + 1, len(toks) - 1):
            if toks[j].low in ("from", ";"):
                break
            if toks[j].text == ":" and toks[j + 1].kind == "word":
                self._runtime(st.scope, toks[j + 1].low.split("-")[0], toks[j].start)

    # ------------------------------------------------------------------ resolution

    def _why(self, name: str, why: str) -> None:
        self.dynamic.setdefault(name, why)

    def lookup(self, name: str, scope: int | None, pos: int, depth: int = 0) -> str | None:
        """The literal value of `&name` at `pos` in `scope`, or None when it is not pinned."""
        name = name.lower()
        if is_automatic(name):
            self._why(name, "an automatic or reserved macro variable, set by SAS at run time")
            return None
        if self.indirect:
            self._why(name, "the file assigns macro variables through computed names")
            return None
        if scope is not None:
            macro = self.program.macros[scope]
            if name in {p.name for p in macro.params}:
                self._why(name, f"a parameter of macro {macro.name}")
                return None
            if name in self.do_index.get(scope, set()):
                self._why(name, f"a %DO index in macro {macro.name}")
                return None
            found = self.assignments.get((scope, name))
            if not found:
                self._why(name, f"not assigned in macro {macro.name}: its value comes from the caller or the global scope")
                return None
        else:
            found = self.assignments.get((None, name))
            if not found:
                self._why(name, "not assigned in this file")
                return None
        return self._single(name, found, pos, depth)

    def _single(self, name: str, found: list[Assignment], pos: int, depth: int) -> str | None:
        if len(found) != 1:
            self._why(name, "assigned more than once")
            return None
        a = found[0]
        if a.kind != "let":
            self._why(name, "assigned at run time (CALL SYMPUT, SELECT INTO, %DO or a macro)")
            return None
        if a.conditional:
            self._why(name, "assigned inside a %IF or %DO block")
            return None
        if a.pos >= pos:
            self._why(name, "referenced before its %LET")
            return None
        value = self._value(a, depth)
        if value is None:
            computed = "%" in (a.raw or "") and not _QUOTED.match(a.raw or "")
            self._why(name, "its %LET value is computed (a macro function or call)" if computed else "its value refers to itself or exceeds the expansion bounds")
        return value

    def _value(self, a: Assignment, depth: int) -> str | None:
        if a.done:
            return a.value
        a.done = True  # a cycle through this assignment reads as dynamic
        raw = a.raw or ""
        q = _QUOTED.match(raw)
        if q:
            raw = q.group(2)
        if "%" in raw:
            return None
        a.value = self.expand(raw, a.scope, a.pos, depth + 1)
        return a.value

    def expand(self, text: str, scope: int | None, pos: int, depth: int = 0) -> str | None:
        """`text` with every pinned reference replaced by its value and every other one by a
        sentinel placeholder. None when the expansion exceeds its bounds."""
        if depth > MAX_DEPTH:
            return None
        cur = text
        for _ in range(MAX_PASSES):
            if "&" not in cur:
                return cur
            out: list[str] = []
            size, i, n, rescan = 0, 0, len(cur), False
            while i < n:
                j = cur.find("&", i)
                if j < 0:
                    out.append(cur[i:])
                    break
                pieces = [cur[i:j]]
                k = j
                while k < n and cur[k] == "&":
                    k += 1
                m = _NAME.match(cur, k)
                if not m:
                    pieces.append(cur[j:k])
                    i = k
                else:
                    pairs, odd = divmod(k - j, 2)
                    pieces.append("&" * pairs)
                    rescan = rescan or pairs > 0
                    e = m.end()
                    if odd:
                        if e < n and cur[e] == ".":
                            e += 1
                        value = self.lookup(m.group(), scope, pos, depth)
                        pieces.append(value if value is not None else SENTINEL + m.group().lower() + SENTINEL)
                    else:
                        pieces.append(m.group())
                    i = e
                out += pieces
                size += sum(len(x) for x in pieces)
                if size > MAX_LENGTH:
                    return None
            cur = "".join(out)
            if len(cur) > MAX_LENGTH:
                return None
            if not rescan:
                return cur
        return None


# ---------------------------------------------------------------------- rendering


def is_dynamic(expanded: str | None) -> bool:
    return expanded is None or SENTINEL in expanded or "&" in expanded


def placeholders(expanded: str) -> str:
    """`stage.customers_\\x00month\\x00` -> `stage.customers_{month}` (as legacy.sas names them)."""
    return _UNRESOLVED.sub(lambda m: "{" + (m.group(1) or m.group(2)).lower() + "}", expanded)


def unresolved_names(expanded: str) -> list[str]:
    return sorted({(m.group(1) or m.group(2)).lower() for m in _UNRESOLVED.finditer(expanded)})


def template(expanded: str) -> dict:
    """A behaviour expression: a literal when nothing is dynamic, else a template."""
    parts: list = []
    pos = 0
    for m in _UNRESOLVED.finditer(expanded):
        if m.start() > pos:
            parts.append(expanded[pos : m.start()])
        parts.append({"e": "var", "scope": "macro", "name": (m.group(1) or m.group(2)).lower()})
        pos = m.end()
    if pos < len(expanded):
        parts.append(expanded[pos:])
    if all(isinstance(p, str) for p in parts):
        return {"e": "lit", "value": "".join(parts), "type": "string"}
    return {"e": "template", "parts": parts}
