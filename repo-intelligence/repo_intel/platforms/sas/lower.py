"""Lower SAS into the behavioural IR (repo_intel.compile.behavior).

One unit per DATA step, per PROC SQL step, per macro definition and for a
program's open code:

- A DATA step is an `implicit` loop over the datasets its SET, MERGE, UPDATE and
  MODIFY statements read. Inside it: assignments and sum statements, IF/THEN/ELSE,
  SELECT/WHEN as `switch` (or an `if` chain when SELECT has no operand), DO loops,
  OUTPUT as `output` to a dataset, DELETE and subsetting IF as `continue`, STOP as
  `break`. RETAIN, ARRAY, BY, INFILE/INPUT, hash objects and the like are `platform`
  nodes named by their construct id. KEEP, DROP, RENAME and BY variables are kept in
  the loop's `detail`.
- PROC SQL is a list of `query` nodes with the SQL text; unresolved macro references
  become `:name` parameters.
- A macro or open code is the list of its macro statements (`%LET` as `assign`,
  `%IF` as `if`, `%DO` as `loop`, `%INCLUDE` as `include`, macro calls as `call`) and
  one node per step: a `platform` node for a DATA step or PROC SQL step (which have
  units of their own), a `call` of the procedure for the others.

The lowering reports what it finds (datasets read and written, formats applied,
constructs seen) back to the analysis through `self.a`, so a statement is read once.
"""

from __future__ import annotations

from typing import Any

from repo_intel.platforms.sas.catalog import MACRO_STATEMENTS
from repo_intel.platforms.sas.expr import ExprError, Parser
from repo_intel.platforms.sas.lexer import Stmt, Tok, unquote
from repo_intel.platforms.sas.parse import Step

Node = dict[str, Any]
_NAME_OPTIONS = {"keep", "drop", "rename", "where", "in", "obs", "firstobs", "index", "label", "rename", "compress", "sortedby", "type", "encrypt", "pw", "read", "write", "alter", "genmax", "gennum"}
_CALL_CONSTRUCTS = {"execute": "sas:call_execute", "symput": "sas:call_symput", "symputx": "sas:call_symput", "symputn": "sas:call_symput", "system": "sas:x_command"}
_OTHER_DATA_STATEMENTS = {"window", "display", "missing", "lostcard", "list", "error", "dm", "describe", "execute", "redirect"}


def toks_of(st: Stmt) -> list[Tok]:
    return [t for t in st.toks if not (t.kind == "op" and t.text == ";")]


def split_at(toks: list[Tok], word: str) -> int | None:
    """Index of `word` (a keyword, or a macro keyword like `%then`) at parenthesis depth 0."""
    depth = 0
    for i, t in enumerate(toks):
        if t.text in ("(", "{", "["):
            depth += 1
        elif t.text in (")", "}", "]"):
            depth -= 1
        elif depth == 0 and t.low == word:
            return i
    return None


def skip_parens(toks: list[Tok], i: int) -> int:
    """Index after the balanced group that starts at toks[i] (`(`, `{` or `[`)."""
    depth = 0
    while i < len(toks):
        if toks[i].text in ("(", "{", "["):
            depth += 1
        elif toks[i].text in (")", "}", "]"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


class Lowerer:
    """Lowers a list of entries (statements, or for containers also steps and macro definitions)."""

    def __init__(self, analysis: Any, scope: int | None, owner: dict, *, data_step: bool):
        self.a = analysis
        self.scope, self.owner = scope, owner
        self.data_step = data_step
        self.arrays: set[str] = set()
        self.objects: set[str] = set()
        self.channel = "log"
        self.inputs: list[Any] = []  # DsName read by SET/MERGE/UPDATE/MODIFY and hash objects
        self.outputs: list[Any] = []
        self.detail: dict[str, Any] = {}
        self.entries: list[Any] = []

    # ------------------------------------------------------------------ helpers

    def at(self, a: int, b: int) -> list[int]:
        return self.a.at(a, b)

    def node_at(self, toks: list[Tok], st: Stmt) -> list[int]:
        return self.at(toks[0].start if toks else st.start, st.end)

    def expr(self, toks: list[Tok], *, where: bool = False, macro: bool = False) -> Node:
        try:
            return Parser(
                toks, macro_mode=macro, where=where, arrays=self.arrays, objects=self.objects,
                template=lambda t: self.a.template(t, self.scope),
                on_function=lambda name, raw, t: self.a.function(name, raw, t, self.scope, self.owner),
            ).parse()
        except (ExprError, RecursionError):
            text = " ".join(t.text for t in toks)[:200]
            if toks:
                self.a.construct(toks[0].start, toks[-1].end, "sas:unparsed_statement", False)
            return {"e": "platform", "construct": "sas:unparsed_statement", "text": text}

    def platform(self, construct: str, toks: list[Tok], st: Stmt, modelled: bool = True, **detail: Any) -> Node:
        if toks:
            self.a.construct(toks[0].start, st.end, construct, modelled)
        return {"op": "platform", "construct": construct, "at": self.node_at(toks, st), "detail": detail}

    def ds_list(self, toks: list[Tok], i: int, role: str, construct: str) -> tuple[list[Any], int]:
        """Dataset names from toks[i:], up to an option (`end=`), a `/` or the end."""
        out = []
        while i < len(toks):
            t = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.text == "/" or (t.kind == "word" and nxt is not None and nxt.text == "="):
                break
            if t.kind == "word" or (t.kind == "str" and t.text.lower().endswith("n")):
                if nxt is not None and nxt.text in ("-", ":"):  # ds1-ds9 or lib.pre: lists
                    self.a.dynamic_list(t, self.scope, role)
                    i += 3 if nxt.text == "-" else 2
                    continue
                for name in self.a.ds_names(t, self.scope):
                    out.append(name)
                    self.a.dataset_use(name, role, self.owner, construct)
                i += 1
                if i < len(toks) and toks[i].text == "(":
                    i = skip_parens(toks, i)
                continue
            if t.kind == "str":
                self.a.physical_file(t, role)
            i += 1
        return out, i

    # ------------------------------------------------------------------ blocks

    def block(self, i: int, ends: frozenset[str]) -> tuple[list[Node], int]:
        out: list[Node] = []
        while i < len(self.entries):
            entry = self.entries[i]
            if isinstance(entry, Stmt):
                toks = toks_of(entry)
                if toks and toks[0].low in ends:
                    return out, i
                nodes, i = self.statement(toks, entry, i + 1)
                out += nodes
            else:
                out += self.other(entry)
                i += 1
        return out, i

    def other(self, entry: Any) -> list[Node]:
        return []

    def statement(self, toks: list[Tok], st: Stmt, i: int) -> tuple[list[Node], int]:
        if not toks:
            return [], i
        first = toks[0]
        if first.kind == "mac":
            return self.macro_statement(toks, st, i)
        if self.data_step:
            return self.data_statement(toks, st, i)
        return self.container_statement(toks, st, i)

    # ------------------------------------------------------------------ macro statements

    def macro_statement(self, toks: list[Tok], st: Stmt, i: int) -> tuple[list[Node], int]:
        key = toks[0].low[1:]
        at = self.node_at(toks, st)
        if len(toks) > 1 and toks[1].text == ":" and key not in MACRO_STATEMENTS:  # %label:
            nodes, i = self.statement(toks[2:], st, i)
            return [self.platform("sas:macro_goto", toks, st, False, label=key)] + nodes, i
        if key == "if":
            k = split_at(toks, "%then")
            cond = self.expr(toks[1:k] if k else toks[1:], macro=True)
            self.a.construct(toks[0].start, st.end, "sas:macro_if", True)
            then, i = self.statement(toks[k + 1 :] if k else [], st, i)
            node: Node = {"op": "if", "at": at, "cond": cond, "then": then, "macro": True}
            if i < len(self.entries) and isinstance(self.entries[i], Stmt):
                nt = toks_of(self.entries[i])
                if nt and nt[0].low == "%else":
                    other, i = self.statement(nt[1:], self.entries[i], i + 1)
                    node["else"] = other
            return [node], i
        if key == "do":
            self.a.construct(toks[0].start, st.end, "sas:macro_do", True)
            body, j = self.block(i, frozenset({"%end"}))
            j = j + 1 if j < len(self.entries) else j
            rest = toks[1:]
            if not rest:
                return body, j
            return [self.loop(rest, body, at, macro=True)], j
        if key == "end":
            return [], i  # an unmatched %END
        if key == "else":
            return self.statement(toks[1:], st, i)
        if key == "let":
            self.a.construct(toks[0].start, st.end, "sas:let", True)
            eq = split_at(toks, "=")
            name = toks[1].low if len(toks) > 1 else ""
            value_text = self.a.text_between(toks[eq].end, st.end) if eq else ""
            return [{"op": "assign", "at": at, "target": {"e": "var", "scope": "macro", "name": name}, "value": self.a.macro_value(value_text, self.scope, toks[0].start)}], i
        if key in ("global", "local"):
            self.a.construct(toks[0].start, st.end, "sas:global_local", True)
            return [{"op": "declare", "at": at, "name": t.low, "scope": key} for t in toks[1:] if t.kind == "word"], i
        if key == "put":
            self.a.construct(toks[0].start, st.end, "sas:macro_put", True)
            return [{"op": "output", "at": at, "channel": "log", "value": self.a.macro_value(self.a.text_between(toks[0].end, st.end), self.scope, toks[0].start)}], i
        if key == "include":
            target = self.a.include(toks, st, self.scope, self.owner)
            return [{"op": "include", "at": at, "target": target}], i
        if key in ("return",):
            return [{"op": "return", "at": at}], i
        if key == "abort":
            self.a.construct(toks[0].start, st.end, "sas:abort", True)
            return [{"op": "exit", "at": at}], i
        if key in ("goto", "label"):
            return [self.platform("sas:macro_goto", toks, st, False)], i
        if key == "sysexec":
            return [self.platform("sas:x_command", toks, st, True)], i
        if key in ("stpbegin", "stpend"):
            return [self.platform("sas:stpbegin_stpend", toks, st, True, action=key[3:])], i
        if key in ("then", "to", "by", "while", "until", "mend", "macro"):
            return [], i
        if key in MACRO_STATEMENTS:
            return [self.platform("sas:macro_other", toks, st, False, statement=key)], i
        # A macro call or a macro function used as a statement.
        return [self.macro_call_node(toks, st)], i

    def macro_call_node(self, toks: list[Tok], st: Stmt) -> Node:
        name = toks[0].low[1:]
        args: list[Node] = []
        named: dict[str, Node] = {}
        if len(toks) > 1 and toks[1].text == "(":
            depth, cur, j = 0, [], 1
            groups: list[list[Tok]] = []
            while j < len(toks):
                t = toks[j]
                if t.text == "(":
                    depth += 1
                    if depth == 1:
                        j += 1
                        continue
                elif t.text == ")":
                    depth -= 1
                    if depth == 0:
                        groups.append(cur)
                        break
                if depth == 1 and t.text == ",":
                    groups.append(cur)
                    cur = []
                else:
                    cur.append(t)
                j += 1
            for g in groups:
                if not g:
                    continue
                if len(g) > 1 and g[0].kind == "word" and g[1].text == "=":
                    named[g[0].low] = self.a.macro_value(self.a.text_between(g[1].end, g[-1].end), self.scope, g[0].start) if len(g) > 2 else {"e": "lit", "value": "", "type": "string"}
                else:
                    args.append(self.a.macro_value(self.a.text_between(g[0].start, g[-1].end), self.scope, g[0].start))
        callee: Node = {"e": "name", "name": name, "kind": "macro"}
        if "&" in name:  # %&which or %load_&kind: the macro is named at run time
            self.a.dynamic_macro_call(toks[0], self.scope)
            callee = self.a.template(Tok("word", toks[0].text[1:], toks[0].start + 1, toks[0].end), self.scope)
            if callee.get("e") == "lit":
                callee = {"e": "name", "name": str(callee["value"]).lower(), "kind": "macro"}
        call: Node = {"e": "call", "callee": callee, "args": args}
        if named:
            call["named"] = named
        return {"op": "call", "at": self.node_at(toks, st), "call": call}

    def loop(self, rest: list[Tok], body: list[Node], at: list[int], *, macro: bool = False) -> Node:
        """A loop from the tokens after DO / %DO."""
        words = [t.low for t in rest]
        to_word, by_word, while_word, until_word = ("%to", "%by", "%while", "%until") if macro else ("to", "by", "while", "until")
        if words[0] in (while_word, until_word):
            return {"op": "loop", "at": at, "kind": "while" if words[0] == while_word else "until", "cond": self.expr(rest[1:], macro=macro), "body": body}
        if words[0] == "over" and len(rest) > 1:  # DO OVER array (the old implicit-array form)
            return {"op": "loop", "at": at, "kind": "each", "over": {"e": "var", "name": rest[1].low, "scope": "array"}, "body": body}
        eq = split_at(rest, "=")
        if eq is None:
            return {"op": "loop", "at": at, "kind": "while", "cond": self.expr(rest, macro=macro), "body": body}
        var = {"e": "var", "name": rest[0].low, "scope": "macro" if macro else "pdv"}
        spec = rest[eq + 1 :]
        cond_kind, cond = None, None
        for w in (while_word, until_word):
            k = split_at(spec, w)
            if k is not None:
                cond_kind, cond = w.lstrip("%"), self.expr(spec[k + 1 :], macro=macro)
                spec = spec[:k]
        t = split_at(spec, to_word)
        if t is not None:
            b = split_at(spec, by_word)
            node: Node = {"op": "loop", "at": at, "kind": "range", "var": var, "from": self.expr(spec[:t], macro=macro), "to": self.expr(spec[t + 1 : b] if b else spec[t + 1 :], macro=macro), "body": body}
            if b is not None:
                node["step"] = self.expr(spec[b + 1 :], macro=macro)
            if cond is not None:
                node["cond"], node["cond_kind"] = cond, cond_kind
            return node
        items, cur, depth = [], [], 0
        for tok in spec:
            depth += {"(": 1, ")": -1}.get(tok.text, 0)
            if tok.text == "," and depth == 0:
                items.append(cur)
                cur = []
            else:
                cur.append(tok)
        items.append(cur)
        return {"op": "loop", "at": at, "kind": "each", "var": var, "over": {"e": "array", "items": [self.expr(x, macro=macro) for x in items if x]}, "body": body}

    # ------------------------------------------------------------------ DATA step statements

    def data_statement(self, toks: list[Tok], st: Stmt, i: int) -> tuple[list[Node], int]:
        first = toks[0]
        w = first.low
        at = self.node_at(toks, st)
        c = lambda cid, modelled=True: self.a.construct(first.start, st.end, cid, modelled)  # noqa: E731
        if first.kind == "word" and len(toks) > 1 and toks[1].text == ":" and w not in ("if", "else"):  # a label
            c("sas:goto_link", False)
            nodes, i = self.statement(toks[2:], st, i)
            return [{"op": "platform", "construct": "sas:goto_link", "at": at, "detail": {"label": w}}] + nodes, i
        if (first.kind == "word" and len(toks) > 1 and toks[1].text == "=") or self._array_assign(toks):
            return [self.assign(toks, st)], i
        if len(toks) > 1 and toks[1].text == "+" and first.kind == "word" and w not in ("if", "put", "output"):
            c("sas:sum_statement")
            target = {"e": "var", "name": w, "scope": "pdv"}
            return [{"op": "assign", "at": at, "target": target, "value": {"e": "binop", "op": "+", "left": target, "right": self.expr(toks[2:])}, "sum": True}], i
        if w == "if":
            k = split_at(toks, "then")
            if k is None:
                c("sas:subsetting_if")
                return [{"op": "if", "at": at, "cond": {"e": "unop", "op": "not", "operand": self.expr(toks[1:])}, "then": [{"op": "continue", "at": at}], "subsetting": True}], i
            c("sas:if_then")
            then, i = self.statement(toks[k + 1 :], st, i)
            node: Node = {"op": "if", "at": at, "cond": self.expr(toks[1:k]), "then": then}
            if i < len(self.entries) and isinstance(self.entries[i], Stmt):
                nt = toks_of(self.entries[i])
                if nt and nt[0].low == "else":
                    other, i = self.statement(nt[1:], self.entries[i], i + 1)
                    node["else"] = other
            return [node], i
        if w == "else":
            return self.statement(toks[1:], st, i)
        if w == "where":
            c("sas:where")
            rest = toks[1:]
            if rest and rest[0].low in ("also", "same") and len(rest) > 1 and rest[1].low == "and":
                rest = rest[2:]
            return [{"op": "if", "at": at, "cond": {"e": "unop", "op": "not", "operand": self.expr(rest, where=True)}, "then": [{"op": "continue", "at": at}], "where": True}], i
        if w == "do":
            c("sas:do_loop" if len(toks) > 1 else "sas:do_group")
            body, j = self.block(i, frozenset({"end"}))
            j = j + 1 if j < len(self.entries) else j
            if len(toks) == 1:
                return body, j
            return [self.loop(toks[1:], body, at)], j
        if w == "end":
            return [], i
        if w == "select":
            c("sas:select")
            return self.select(toks, st, i)
        if w in ("set", "merge", "update", "modify"):
            cid = {"set": "sas:set", "merge": "sas:merge", "update": "sas:update", "modify": "sas:modify"}[w]
            c(cid)
            names, _ = self.ds_list(toks, 1, "read", cid)
            if w == "modify":
                for name in names:
                    self.a.dataset_use(name, "write", self.owner, cid)
            self.inputs += names
            return [{"op": "platform", "construct": cid, "at": at, "detail": {"datasets": [n.label for n in names]}}], i
        if w == "by":
            c("sas:by_group")
            names = [t.low for t in toks[1:] if t.kind == "word" and t.low not in ("descending", "notsorted", "groupformat")]
            self.detail.setdefault("by", []).extend(names)
            return [{"op": "platform", "construct": "sas:by_group", "at": at, "detail": {"variables": names, "descending": [toks[j + 1].low for j in range(1, len(toks) - 1) if toks[j].low == "descending"]}}], i
        if w == "retain":
            c("sas:retain")
            return [{"op": "platform", "construct": "sas:retain", "at": at, "detail": {"variables": [t.low for t in toks[1:] if t.kind == "word"]}}], i
        if w == "array":
            c("sas:array")
            return [self.array(toks, at)], i
        if w in ("keep", "drop"):
            c("sas:keep_drop")
            self.detail.setdefault(w, []).extend(t.low for t in toks[1:] if t.kind == "word")
            return [], i
        if w == "rename":
            c("sas:rename")
            ren = self.detail.setdefault("rename", {})
            for j in range(1, len(toks) - 2):
                if toks[j + 1].text == "=" and toks[j].kind == "word" and toks[j + 2].kind == "word":
                    ren[toks[j].low] = toks[j + 2].low
            return [], i
        if w == "length":
            c("sas:length")
            out, j = [], 1
            while j < len(toks):
                if toks[j].kind == "word" and toks[j].low != "default":
                    char = j + 1 < len(toks) and toks[j + 1].text == "$"
                    out.append({"op": "declare", "at": at, "name": toks[j].low, "type": "string" if char else "number"})
                j += 1
            return out, i
        if w in ("format", "informat"):
            c("sas:format_statement")
            self.a.format_statement(toks, self.owner)
            return [], i
        if w in ("label", "attrib"):
            c("sas:label" if w == "label" else "sas:attrib")
            self.a.format_statement(toks, self.owner)
            return [], i
        if w == "output":
            c("sas:output")
            names, _ = self.ds_list(toks, 1, "output", "sas:output")
            value: Node | None = None
            if names:
                value = names[0].expr if len(names) == 1 else {"e": "array", "items": [n.expr for n in names]}
            node = {"op": "output", "at": at, "channel": "dataset"}
            if value is not None:
                node["value"] = value
            return [node], i
        if w == "delete":
            c("sas:delete")
            return [{"op": "continue", "at": at, "delete": True}], i
        if w in ("stop", "leave"):
            c("sas:stop")
            return [{"op": "break", "at": at}], i
        if w == "continue":
            return [{"op": "continue", "at": at}], i
        if w == "return":
            c("sas:return")
            return [{"op": "return", "at": at}], i
        if w == "abort":
            c("sas:abort")
            return [{"op": "exit", "at": at}], i
        if w in ("goto", "go", "link"):
            c("sas:goto_link", False)
            return [{"op": "platform", "construct": "sas:goto_link", "at": at, "detail": {"kind": w, "label": toks[-1].low}}], i
        if w in ("put", "putlog"):
            c("sas:file_put")
            channel = "log" if w == "putlog" else self.channel
            parts = [self.a.template(t, self.scope) if t.kind == "str" else {"e": "var", "name": t.low, "scope": "pdv"} for t in toks[1:] if t.kind == "str" or (t.kind == "word" and not t.low.endswith("."))]
            return [{"op": "output", "at": at, "channel": channel, "value": {"e": "array", "items": parts}}], i
        if w == "file":
            c("sas:file_put")
            target = toks[1] if len(toks) > 1 else None
            if target is not None and target.low in ("print", "log"):
                self.channel = "report" if target.low == "print" else "log"
            else:
                self.channel = "file"
            return [{"op": "platform", "construct": "sas:file_put", "at": at, "detail": {"target": self.a.file_target(target, self.scope, self.owner)}}], i
        if w in ("infile", "input"):
            c("sas:infile_input")
            detail: dict[str, Any] = {"statement": w}
            if w == "infile" and len(toks) > 1:
                detail["source"] = self.a.file_target(toks[1], self.scope, self.owner)
            else:
                detail["variables"] = [t.low for t in toks[1:] if t.kind == "word" and not t.low.endswith(".")]
            return [{"op": "platform", "construct": "sas:infile_input", "at": at, "detail": detail}], i
        if w in ("datalines", "cards", "lines", "datalines4", "cards4", "lines4"):
            c("sas:datalines")
            return [{"op": "platform", "construct": "sas:datalines", "at": at, "detail": {}}], i
        if w == "call" and len(toks) > 1:
            routine = toks[1].low
            c(_CALL_CONSTRUCTS.get(routine, "sas:call_routine"))
            parser_toks = toks[1:]
            call = self.expr(parser_toks)
            if routine == "execute":
                self.a.call_execute(toks, st, self.scope, self.owner)
            if call.get("e") != "call":
                call = {"e": "call", "callee": {"e": "name", "name": routine, "kind": "builtin"}, "args": []}
            return [{"op": "call", "at": at, "call": call}], i
        if w in ("declare", "dcl") and len(toks) > 2:
            c("sas:hash_object")
            kind, name = toks[1].low, toks[2].low
            self.objects.add(name)
            args: list[Node] = []
            if len(toks) > 3 and toks[3].text == "(":
                self.a.hash_datasets(toks[3:], self.scope, self.owner, self.inputs)
                try:
                    args = Parser([toks[2], *toks[3:]], objects=self.objects | {"_"}, template=lambda t: self.a.template(t, self.scope)).parse().get("args", [])
                except (ExprError, RecursionError):
                    args = []
            return [{"op": "declare", "at": at, "name": name, "type": "unknown", "value": {"e": "new", "type": kind, "args": args}, "construct": "sas:hash_object"}], i
        if first.kind == "word" and "." in w and len(toks) > 1 and toks[1].text == "(":  # h.definekey('k');
            c("sas:hash_object")
            return [{"op": "call", "at": at, "call": self.expr(toks)}], i
        if st.kind.startswith("global:") and first is st.toks[0]:
            # Counted by the analysis's pass over global statements; kept here for order.
            return [{"op": "platform", "construct": "sas:global_statement", "at": at, "detail": {"statement": w}}], i
        if w in _OTHER_DATA_STATEMENTS:
            c("sas:data_other", False)
            return [{"op": "platform", "construct": "sas:data_other", "at": at, "detail": {"statement": w}}], i
        c("sas:unparsed_statement", False)
        return [{"op": "platform", "construct": "sas:unparsed_statement", "at": at, "detail": {"keyword": w[:40]}}], i

    def _array_assign(self, toks: list[Tok]) -> bool:
        if len(toks) < 3 or toks[0].kind != "word" or toks[1].text not in ("{", "[", "("):
            return False
        j = skip_parens(toks, 1)
        return j < len(toks) and toks[j].text == "=" and toks[0].low not in ("if", "select", "when", "where", "do", "output", "put")

    def assign(self, toks: list[Tok], st: Stmt) -> Node:
        eq = next(j for j, t in enumerate(toks) if t.text == "=")
        self.a.construct(toks[0].start, st.end, "sas:assignment", True)
        target = self.expr(toks[:eq])
        if target.get("e") not in ("var", "index", "member"):
            target = {"e": "var", "name": toks[0].low, "scope": "pdv"}
        return {"op": "assign", "at": self.node_at(toks, st), "target": target, "value": self.expr(toks[eq + 1 :])}

    def array(self, toks: list[Tok], at: list[int]) -> Node:
        name = toks[1].low if len(toks) > 1 else ""
        self.arrays.add(name)
        j = 2
        size = ""
        if j < len(toks) and toks[j].text in ("{", "[", "("):
            k = skip_parens(toks, j)
            size = " ".join(t.text for t in toks[j + 1 : k - 1])
            j = k
        elements, temporary = [], False
        while j < len(toks) and toks[j].text != "(":
            if toks[j].low == "_temporary_":
                temporary = True
            elif toks[j].kind == "word":
                elements.append(toks[j].low)
            j += 1
        return {"op": "platform", "construct": "sas:array", "at": at, "detail": {"name": name, "size": size, "elements": elements, "temporary": temporary}}

    def select(self, toks: list[Tok], st: Stmt, i: int) -> tuple[list[Node], int]:
        at = self.node_at(toks, st)
        on = self.expr(toks[2:-1]) if len(toks) > 2 and toks[1].text == "(" else None
        cases: list[dict[str, Any]] = []
        default: list[Node] | None = None
        while i < len(self.entries):
            entry = self.entries[i]
            if not isinstance(entry, Stmt):
                i += 1
                continue
            et = toks_of(entry)
            if not et:
                i += 1
                continue
            w = et[0].low
            if w == "end":
                i += 1
                break
            if w == "when" and len(et) > 1 and et[1].text == "(":
                close = skip_parens(et, 1)
                match_toks = et[2 : close - 1]
                groups, cur, depth = [], [], 0
                for t in match_toks:
                    depth += {"(": 1, ")": -1}.get(t.text, 0)
                    if t.text == "," and depth == 0:
                        groups.append(cur)
                        cur = []
                    else:
                        cur.append(t)
                groups.append(cur)
                body, i = self.statement(et[close:], entry, i + 1)
                cases.append({"match": [self.expr(g) for g in groups if g], "body": body})
                continue
            if w == "otherwise":
                default, i = self.statement(et[1:], entry, i + 1)
                continue
            nodes, i = self.statement(et, entry, i + 1)  # anything else: keep it in order
            (default if default is not None else cases[-1]["body"] if cases else []).extend(nodes)
        if on is not None:
            node: Node = {"op": "switch", "at": at, "on": on, "cases": cases}
            if default is not None:
                node["default"] = default
            return [node], i
        # SELECT; WHEN (condition) ...: an if/else chain
        chain: list[Node] = default or []
        for case in reversed(cases):
            cond = case["match"][0] if len(case["match"]) == 1 else {"e": "binop", "op": "or", "left": case["match"][0], "right": case["match"][1]} if case["match"] else {"e": "lit", "value": True, "type": "boolean"}
            chain = [{"op": "if", "at": at, "cond": cond, "then": case["body"], **({"else": chain} if chain else {})}]
        return chain, i

    # ------------------------------------------------------------------ containers

    def container_statement(self, toks: list[Tok], st: Stmt, i: int) -> tuple[list[Node], int]:
        return self.a.container_node(toks, st, self.scope, self.owner), i


class DataStep(Lowerer):
    def __init__(self, analysis: Any, step: Step, owner: dict):
        super().__init__(analysis, step.scope, owner, data_step=True)
        self.step = step
        stmts = [analysis.stmts[j] for j in step.stmts]
        self.header = stmts[0]
        self.entries = [s for s in stmts[1:] if s.kind not in ("run", "quit")]

    def lower(self) -> Node:
        header = toks_of(self.header)
        self.a.construct(header[0].start, self.header.end, "sas:data_step", True)
        slash = split_at(header, "/")
        names, _ = self.ds_list(header[: slash] if slash else header, 1, "write", "sas:data_step")
        self.outputs = names
        if slash is not None and any(t.low == "view" for t in header[slash:]):
            self.a.construct(header[slash].start, self.header.end, "sas:data_view", False)
        body, _ = self.block(0, frozenset())
        detail = {"data": [n.label for n in names], **{k: v for k, v in self.detail.items() if v}}
        return {
            "op": "loop", "kind": "implicit", "at": self.a.at(self.step.start, self.step.end),
            "over": {"e": "array", "items": [n.expr for n in self.inputs]}, "body": body, "detail": detail,
        }


class Container(Lowerer):
    """Open code or one macro definition: macro statements and one node per step."""

    def __init__(self, analysis: Any, scope: int | None, owner: dict, items: list[tuple[str, int]]):
        super().__init__(analysis, scope, owner, data_step=False)
        self.entries = []
        for kind, index in items:
            self.entries.append(analysis.stmts[index] if kind == "stmt" else (kind, index))

    def other(self, entry: Any) -> list[Node]:
        kind, index = entry
        return self.a.step_or_macro_node(kind, index, self.owner)

    def lower(self) -> list[Node]:
        body, _ = self.block(0, frozenset())
        return body


def string_value(tok: Tok) -> str:
    return unquote(tok) if tok.kind == "str" else tok.text
