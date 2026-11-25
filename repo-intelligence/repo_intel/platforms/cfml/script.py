"""CFScript and CFML expressions: a lexer and a recursive-descent parser that lowers straight
into the behavioural IR.

The same expression parser reads `<cfset>` and `<cfif>` expressions, `#...#`
interpolations and tag attribute values, so tag and script code lower to the same
nodes. Statements cover script components (`component { ... }`), functions and
closures, `var`, `if`/`else`, `for`, `for-in`, `while`, `do-while`, `switch`,
`try`/`catch`/`finally`, `return`, `throw`, `include`, `abort`, the tags written as
script statements (`lock name="x" { }`, `transaction { }`, `cfhttp(...)`), and the
built-ins with a neutral form (`queryExecute`, `writeOutput`, `location`).

The parser never guesses. A statement it cannot read is kept as a `platform` node
named `cf:unparsed` with its span, and parsing resumes after it. Tokens are read
once and each brace's partner is found in one pass, so parsing is linear in the
text.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.cfml.ir import (
    COUNTED_SCOPES, SCOPES, Ctx, Expr, Function, QueryFact, Ref, Stmt, assign_target, cf_type, lit, lit_str, platform, roles_list,
    sql_type, tag_statements, var,
)
from repo_intel.platforms.cfml.scan import scan_string

_TOKEN = re.compile(r"""
  (?P<ws>\s+)
 |(?P<lc>//[^\n]*)
 |(?P<bc>/\*.*?(?:\*/|\Z))
 |(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
 |(?P<id>[A-Za-z_$][\w$]*)
 |(?P<op>===|!==|\?\.|\?:|=>|\+\+|--|\+=|-=|\*=|/=|&=|%=|==|!=|<>|<=|>=|&&|\|\||::|[-+*/\\%^&=<>!?:.,;()\[\]{}\#@|~])
""", re.X | re.S)
MAX_DEPTH = 120
ACCESS = frozenset({"public", "private", "package", "remote"})
MODIFIERS = ACCESS | {"static", "final", "abstract"}
# Tags written as script statements: `lock name="x" { }`, `transaction { }`, `param name="x";`.
SCRIPT_TAGS = frozenset({
    "lock", "transaction", "thread", "savecontent", "param", "http", "mail", "query", "setting", "cookie", "header", "content", "log", "dump", "file",
    "directory", "schedule", "loginuser", "login", "logout", "invoke", "module", "flush", "zip", "pdf", "spreadsheet", "ftp", "execute", "storedproc",
    "procparam", "httpparam", "mailparam", "queryparam", "document", "ldap", "pop", "imap", "registry", "wddx", "image", "timer", "trace", "cache",
    "location", "dbinfo", "processingdirective", "pageencoding", "silent", "abort", "exit", "include", "application", "object",
})
BUILTINS = frozenset("""
abs arrayappend arrayavg arrayclear arraycontains arraydeleteat arrayeach arrayfilter arrayfind arrayfindnocase arrayinsertat arrayisempty arraylen
arraymap arraynew arrayprepend arrayreduce arrayresize arrayset arraysort arraysum arrayslice arraytolist asc bitand ceiling chr compare comparenocase
createdate createdatetime createobject createodbcdate createodbcdatetime createtimespan createuuid dateadd datecompare datediff dateformat datepart day
dayofweek de decimalformat decrypt deserializejson directoryexists directorylist dollarformat duplicate encodeforhtml encodeforurl encrypt entitydelete
entityload entityloadbypk entitynew entitysave evaluate expandpath fileexists fileread filewrite find findnocase fix getauthuser gettickcount hash
htmleditformat iif int invoke isarray isboolean isdate isdefined isnull isnumeric isquery issimplevalue isstruct isuserinanyrole isuserinrole isuserloggedin
isvalid javacast lcase left len listappend listcontains listcontainsnocase listfind listfindnocase listfirst listgetat listlast listlen listprepend listsort
listtoarray location lsdateformat lsnumberformat ltrim max mid min month now numberformat ormexecutequery ormflush parsedatetime querySetCell queryaddrow
queryexecute querynew randrange refind refindnocase rematch replace replacelist replacenocase rereplace rereplacenocase reverse right round rtrim
serializejson setvariable sleep structappend structclear structcount structcopy structdelete structeach structfind structinsert structisempty structkeyarray
structkeyexists structkeylist structnew structupdate throw tobase64 tostring trim ucase urldecode urlencodedformat val writedump writelog writeoutput xmlparse year
""".lower().split())
ORM_FUNCTIONS = frozenset({"entityload", "entityloadbypk", "entitysave", "entitydelete", "entitynew", "ormexecutequery", "entitymerge", "entityreload", "ormflush"})
DYNAMIC_FUNCTIONS = frozenset({"evaluate", "iif", "setvariable", "de", "precisionevaluate"})
# Word operators: (words, IR operator, precedence).
_WORD_OPS: list[tuple[tuple[str, ...], str, int]] = [
    (("greater", "than", "or", "equal", "to"), ">=", 7), (("less", "than", "or", "equal", "to"), "<=", 7), (("does", "not", "contain"), "not_contains", 7),
    (("greater", "than"), ">", 7), (("less", "than"), "<", 7), (("not", "equal"), "!=", 7), (("is", "not"), "!=", 7),
    (("imp",), "imp", 1), (("eqv",), "eqv", 2), (("xor",), "xor", 3), (("or",), "or", 4), (("and",), "and", 5),
    (("eq",), "==", 7), (("is",), "==", 7), (("equal",), "==", 7), (("neq",), "!=", 7), (("gt",), ">", 7), (("lt",), "<", 7), (("gte",), ">=", 7),
    (("ge",), ">=", 7), (("lte",), "<=", 7), (("le",), "<=", 7), (("contains",), "contains", 7), (("mod",), "%", 10),
]
_SYMBOL_OPS = {
    "||": ("or", 4), "&&": ("and", 5), "==": ("==", 7), "===": ("==", 7), "!=": ("!=", 7), "!==": ("!=", 7), "<>": ("!=", 7), "<": ("<", 7), ">": (">", 7),
    "<=": ("<=", 7), ">=": (">=", 7), "&": ("&", 8), "+": ("+", 9), "-": ("-", 9), "%": ("%", 10), "\\": ("\\", 11), "*": ("*", 12), "/": ("/", 12), "^": ("^", 13),
}
_COMPOUND = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "&=": "&", "%=": "%"}


class ParseError(Exception):
    def __init__(self, message: str, pos: int):
        super().__init__(message)
        self.pos = pos


class Tok:
    __slots__ = ("kind", "val", "a", "b", "parts")

    def __init__(self, kind: str, val: Any, a: int, b: int, parts: Any = None):
        self.kind, self.val, self.a, self.b, self.parts = kind, val, a, b, parts

    def low(self) -> str:
        return self.val.lower() if self.kind == "id" else ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Tok({self.kind},{self.val!r})"


def lex(text: str, a: int, b: int) -> list[Tok]:
    out: list[Tok] = []
    i = a
    while i < b:
        c = text[i]
        if c == '"' or c == "'":
            j, parts, _ = scan_string(text, i, b, True)
            out.append(Tok("str", None, i, j, parts))
            i = j
            continue
        m = _TOKEN.match(text, i, b)
        if m is None:
            out.append(Tok("op", c, i, i + 1))
            i += 1
            continue
        kind = m.lastgroup
        if kind in ("ws", "lc", "bc"):
            i = m.end()
            continue
        out.append(Tok(kind, m.group(), i, m.end()))
        i = m.end()
    out.append(Tok("eof", None, b, b))
    return out


def parse_expression(ctx: Ctx, a: int, b: int) -> Expr:
    """One expression in text[a:b]; unreadable text is a `platform` expression with a diagnostic."""
    p = Parser(ctx, a, b)
    try:
        e = p.expression()
        if p.peek().kind != "eof":
            raise ParseError("unexpected text after expression", p.peek().a)
        return e
    except ParseError as exc:
        ctx.note(f"unparsed expression: {exc}", exc.pos)
        ctx.construct("cf:unparsed", a, b, False)
        return {"e": "platform", "construct": "cf:unparsed", "at": ctx.at(a, b)}


def parse_statements(ctx: Ctx, a: int, b: int) -> list[Stmt]:
    """A cfscript block, or the expression statement of a `<cfset>`."""
    p = Parser(ctx, a, b)
    return p.statements(top=True)


class Parser:
    def __init__(self, ctx: Ctx, a: int, b: int):
        self.ctx = ctx
        self.text = ctx.text
        self.toks = lex(ctx.text, a, b)
        self.i = 0
        self.start, self.end = a, b
        self.match = self._pairs()
        self._chained: list[Stmt] = []  # assignments a chained `a = b = 0` makes before its last one
        self._str_tok: dict[int, Tok] = {}  # id(string literal expression) -> its token

    def _pairs(self) -> list[int]:
        match = [-1] * len(self.toks)
        stack: list[int] = []
        opening = {"(": ")", "[": "]", "{": "}"}
        for i, t in enumerate(self.toks):
            if t.kind != "op":
                continue
            if t.val in opening:
                stack.append(i)
            elif t.val in (")", "]", "}"):
                while stack and opening[self.toks[stack[-1]].val] != t.val:
                    stack.pop()  # an unbalanced opener: leave it unmatched
                if stack:
                    j = stack.pop()
                    match[j], match[i] = i, j
        return match

    # -- tokens

    def peek(self, k: int = 0) -> Tok:
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def next(self) -> Tok:
        t = self.toks[self.i]
        if self.i < len(self.toks) - 1:
            self.i += 1
        return t

    def is_op(self, val: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == "op" and t.val == val

    def is_id(self, word: str | None = None, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == "id" and (word is None or t.val.lower() == word)

    def expect(self, val: str) -> Tok:
        t = self.peek()
        if t.kind != "op" or t.val != val:
            raise ParseError(f"expected {val!r}", t.a)
        return self.next()

    def accept(self, val: str) -> bool:
        if self.is_op(val):
            self.next()
            return True
        return False

    def prev_end(self) -> int:
        return self.toks[self.i - 1].b if self.i > 0 else self.start

    def at(self, a: int, b: int | None = None) -> list[int]:
        return self.ctx.at(a, self.prev_end() if b is None else b)

    def enter(self) -> None:
        self.ctx.depth += 1
        if self.ctx.depth > MAX_DEPTH:
            self.ctx.depth = 0
            raise ParseError("nesting too deep", self.peek().a)

    def leave(self) -> None:
        self.ctx.depth = max(0, self.ctx.depth - 1)

    # ================================================================ statements

    def statements(self, top: bool = False) -> list[Stmt]:
        out: list[Stmt] = []
        while True:
            t = self.peek()
            if t.kind == "eof":
                break
            if t.kind == "op" and t.val == "}" and not top:
                break
            if t.kind == "op" and t.val == "}":
                self.next()  # a stray closing brace at the top of a block
                continue
            start = self.i
            depth = self.ctx.depth
            try:
                out.extend(self.statement())
            except ParseError as exc:
                self.ctx.depth = depth
                out.append(self.recover(start, exc))
        return out

    def recover(self, start: int, exc: ParseError) -> Stmt:
        """Skip the statement that failed: to the `;` that ends it, or the brace that ends its block."""
        self.i = start
        a = self.peek().a
        depth = 0
        while True:
            t = self.peek()
            if t.kind == "eof":
                break
            if t.kind == "op":
                if t.val in ("(", "[", "{"):
                    depth += 1
                elif t.val in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                    if depth == 0 and t.val == "}":
                        self.next()
                        break
                elif t.val == ";" and depth == 0:
                    self.next()
                    break
            self.next()
        if self.i == start:
            self.next()
        b = max(a, self.prev_end())
        self.ctx.note(f"unparsed script: {exc}", exc.pos)
        self.ctx.construct("cf:unparsed", a, b, False)
        return {"op": "platform", "construct": "cf:unparsed", "at": self.ctx.at(a, b)}

    def block_or_statement(self) -> list[Stmt]:
        if self.is_op("{"):
            self.next()
            body = self.statements()
            self.expect("}")
            return body
        return self.statement()

    def end_statement(self) -> None:
        self.accept(";")

    def statement(self) -> list[Stmt]:
        self.enter()
        try:
            return self._statement()
        finally:
            self.leave()

    def _statement(self) -> list[Stmt]:
        t = self.peek()
        if t.kind == "op":
            if t.val == "{":
                return self.block_or_statement()
            if t.val == ";":
                self.next()
                return []
        if t.kind == "id":
            kw = t.low()
            if self.is_function_decl():
                self.function_decl()
                return []
            handler = getattr(self, "st_" + kw, None)
            if handler is not None and kw not in ("function",):
                out = handler()
                if out is not None:
                    return out
            if kw in SCRIPT_TAGS and self._tag_form():
                return self.script_tag(kw)
            if kw.startswith("cf") and self.is_op("(", 1) and (kw[2:] in SCRIPT_TAGS or kw[2:] in _TAG_CALLS):
                return self.tag_call(kw)
        return self.expression_statement()

    def _tag_form(self) -> bool:
        """`lock name="x"`, `transaction {`, `abort;`: a tag written as a statement, not an expression."""
        nxt = self.peek(1)
        if nxt.kind == "id" and (self.is_op("=", 2) or self.is_op(";", 2) or self.is_op("{", 2) or self.peek(2).kind == "id"):
            return True
        if nxt.kind == "op" and nxt.val in ("{", ";"):
            return True
        return nxt.kind == "str" and self.peek(0).low() in ("include", "abort", "location", "exit")

    # -- control flow

    def st_if(self) -> list[Stmt]:
        a = self.next().a
        self.expect("(")
        cond = self.expression()
        self.expect(")")
        then = self.block_or_statement()
        stmt: Stmt = {"op": "if", "at": None, "cond": cond, "then": then}
        if self.is_id("else"):
            self.next()
            stmt["else"] = self.block_or_statement()
        stmt["at"] = self.at(a)
        return [stmt]

    def st_while(self) -> list[Stmt]:
        a = self.next().a
        self.expect("(")
        cond = self.expression()
        self.expect(")")
        body = self.block_or_statement()
        return [{"op": "loop", "at": self.at(a), "kind": "while", "cond": cond, "body": body}]

    def st_do(self) -> list[Stmt]:
        a = self.next().a
        body = self.block_or_statement()
        if not self.is_id("while"):
            raise ParseError("expected while after do", self.peek().a)
        self.next()
        self.expect("(")
        cond = self.expression()
        self.expect(")")
        self.end_statement()
        return [{"op": "loop", "at": self.at(a), "kind": "while", "cond": cond, "body": body, "test": "after"}]

    def st_for(self) -> list[Stmt]:
        a = self.next().a
        open_i = self.i
        self.expect("(")
        close = self.match[open_i]
        j = self.i
        if self.toks[j].kind == "id" and self.toks[j].low() == "var":
            j += 1
        k = j
        while self.toks[k].kind == "id" and k + 1 < len(self.toks) and self.toks[k + 1].kind == "op" and self.toks[k + 1].val == ".":
            k += 2
        if self.toks[k].kind == "id" and self.toks[k + 1].kind == "id" and self.toks[k + 1].low() == "in" and (close < 0 or k + 1 < close):
            declared = self.is_id("var")
            if declared:
                self.next()
            target = self.postfix(self.primary(), self.peek().a)
            if declared and target.get("e") == "var":
                target = {**target, "scope": "local"}
                if self.ctx.func is not None:
                    self.ctx.func.locals.add(target["name"].lower())
            else:
                target = assign_target(self.ctx, target)
            self.next()  # in
            over = self.expression()
            self.expect(")")
            body = self.block_or_statement()
            return [{"op": "loop", "at": self.at(a), "kind": "each", "var": target, "over": over, "body": body}]
        init = [] if self.is_op(";") else self.simple_statement()
        self.expect(";")
        cond = None if self.is_op(";") else self.expression()
        self.expect(";")
        update = [] if self.is_op(")") else self.simple_statement()
        self.expect(")")
        body = self.block_or_statement()
        stmt: Stmt = {"op": "loop", "at": self.at(a), "kind": "for", "body": body, "init": init, "update": update}
        if cond is not None:
            stmt["cond"] = cond
        if len(init) == 1 and init[0]["op"] in ("assign", "declare"):
            first = init[0]
            stmt["var"] = first["target"] if first["op"] == "assign" else var(first["name"], first.get("scope"))
            if first.get("value") is not None:
                stmt["from"] = first["value"]
        return [stmt]

    def st_switch(self) -> list[Stmt]:
        a = self.next().a
        self.expect("(")
        on = self.expression()
        self.expect(")")
        self.expect("{")
        cases: list[dict[str, Any]] = []
        default: list[Stmt] | None = None
        pending: list[Expr] = []
        while not self.is_op("}") and self.peek().kind != "eof":
            if self.is_id("case"):
                self.next()
                pending.append(self.expression())
                self.expect(":")
                body = self._case_body()
                if body:
                    cases.append({"match": pending, "body": body})
                    pending = []
            elif self.is_id("default"):
                self.next()
                self.expect(":")
                default = self._case_body()
                if pending:
                    cases.append({"match": pending, "body": default})
                    pending = []
            else:
                raise ParseError("expected case or default", self.peek().a)
        if pending:
            cases.append({"match": pending, "body": []})
        self.expect("}")
        stmt: Stmt = {"op": "switch", "at": self.at(a), "on": on, "cases": cases}
        if default is not None:
            stmt["default"] = default
        return [stmt]

    def _case_body(self) -> list[Stmt]:
        body: list[Stmt] = []
        while not (self.is_id("case") or self.is_id("default") or self.is_op("}") or self.peek().kind == "eof"):
            if self.is_id("default") and self.is_op(":", 1):
                break
            start = self.i
            try:
                body.extend(self.statement())
            except ParseError as exc:
                body.append(self.recover(start, exc))
        return body

    def st_try(self) -> list[Stmt]:
        a = self.next().a
        body = self.block_or_statement()
        catches = []
        final: list[Stmt] | None = None
        while self.is_id("catch"):
            self.next()
            self.expect("(")
            words = []
            while not self.is_op(")") and self.peek().kind != "eof":
                t = self.next()
                if t.kind == "id":
                    words.append(t.val)
                elif t.kind == "str":
                    words.append("".join(p[3] or "" for p in t.parts))
                elif t.kind == "op" and t.val == ".":
                    words.append(".")
            self.expect(")")
            joined = " ".join(words).replace(" . ", ".")
            pieces = joined.split()
            ctype, cvar = (pieces[0], pieces[-1]) if len(pieces) >= 2 else ("any", pieces[0] if pieces else "e")
            self.ctx.frames.append(({cvar.lower()}, set()))
            try:
                cbody = self.block_or_statement()
            finally:
                self.ctx.frames.pop()
            catches.append({"type": ctype.lower(), "var": cvar, "body": cbody})
        if self.is_id("finally"):
            self.next()
            final = self.block_or_statement()
        stmt: Stmt = {"op": "try", "at": self.at(a), "body": body, "catches": catches}
        if final is not None:
            stmt["finally"] = final
        self.ctx.construct("cf:cftry", a, self.prev_end())
        return [stmt]

    def st_return(self) -> list[Stmt]:
        a = self.next().a
        if self.is_op(";") or self.is_op("}") or self.peek().kind == "eof":
            self.end_statement()
            self.ctx.construct("cf:cfreturn", a, self.prev_end())
            return [{"op": "return", "at": self.at(a)}]
        value = self.expression()
        self.end_statement()
        self.ctx.construct("cf:cfreturn", a, self.prev_end())
        query = self.ctx.query_ops.get(id(value))
        if query is not None:
            return [{**query, "at": self.at(a), "returned": True}]
        return [{"op": "return", "at": self.at(a), "value": value}]

    def st_break(self) -> list[Stmt]:
        a = self.next().a
        self.end_statement()
        self.ctx.construct("cf:cfbreak", a, self.prev_end())
        return [{"op": "break", "at": self.at(a)}]

    def st_continue(self) -> list[Stmt]:
        a = self.next().a
        self.end_statement()
        self.ctx.construct("cf:cfcontinue", a, self.prev_end())
        return [{"op": "continue", "at": self.at(a)}]

    def st_rethrow(self) -> list[Stmt]:
        a = self.next().a
        self.end_statement()
        self.ctx.construct("cf:cfrethrow", a, self.prev_end())
        return [{"op": "throw", "at": self.at(a), "rethrow": True}]

    def st_throw(self) -> list[Stmt] | None:
        if self.is_op("(", 1):
            return None  # throw(...) is read as a call, then lowered by expression_statement
        a = self.next().a
        if self.peek().kind == "id" and self.is_op("=", 1):
            attrs = self.tag_attrs()
            self.end_statement()
            self.ctx.construct("cf:cfthrow", a, self.prev_end())
            return [{"op": "throw", "at": self.at(a), "value": {"e": "struct", "fields": attrs}}]
        value = None if self.is_op(";") else self.expression()
        self.end_statement()
        self.ctx.construct("cf:cfthrow", a, self.prev_end())
        stmt: Stmt = {"op": "throw", "at": self.at(a)}
        if value is not None:
            stmt["value"] = value
        return [stmt]

    def st_var(self) -> list[Stmt] | None:
        if self.peek(1).kind != "id":
            return None
        return self.simple_statement_end()

    def st_import(self) -> list[Stmt]:
        a = self.next().a
        while not self.is_op(";") and self.peek().kind != "eof" and not self.is_op("}"):
            self.next()
        self.end_statement()
        self.ctx.construct("cf:cfimport", a, self.prev_end(), False)
        return []

    def st_property(self) -> list[Stmt] | None:
        if self.is_op("=", 1) or self.is_op("(", 1) or self.is_op(".", 1):
            return None
        a = self.next().a
        attrs: dict[str, Expr] = {}
        words: list[str] = []
        while not self.is_op(";") and not self.is_op("}") and self.peek().kind != "eof":
            if self.peek().kind == "id" and self.is_op("=", 1):
                key = self.next().val.lower()
                self.next()
                attrs[key] = self.expression()
            else:
                t = self.next()
                if t.kind == "id":
                    words.append(t.val)
        self.end_statement()
        name = lit_str(attrs.get("name")) or (words[-1] if words else None)
        ptype = lit_str(attrs.get("type")) or (words[0] if len(words) >= 2 else None)
        if name:
            self.ctx.properties.append({"name": name, "type": ptype, "column": lit_str(attrs.get("column")), "fieldtype": lit_str(attrs.get("fieldtype"))})
        self.ctx.construct("cf:cfproperty", a, self.prev_end())
        return []

    def st_include(self) -> list[Stmt] | None:
        if self.is_op("(", 1) or self.is_op("=", 1) or self.is_op(".", 1):
            return None
        a = self.next().a
        if self.is_id("template") and self.is_op("=", 1):
            self.next()
            self.next()
        target = self.expression()
        self.end_statement()
        return tag_statements(self.ctx, "include", {"template": target}, None, a, self.prev_end())

    # -- functions

    def is_function_decl(self) -> bool:
        j = self.i
        for _ in range(6):
            t = self.toks[j]
            if t.kind != "id":
                return False
            if t.low() == "function":
                nxt = self.toks[j + 1] if j + 1 < len(self.toks) else None
                after = self.toks[j + 2] if j + 2 < len(self.toks) else None
                return bool(nxt and nxt.kind == "id" and after and after.kind == "op" and after.val == "(")
            j += 1
            while self.toks[j].kind == "op" and self.toks[j].val in (".", "[", "]") and j + 1 < len(self.toks):
                j += 1
                if self.toks[j].kind == "id":
                    j += 1
        return False

    def function_decl(self) -> Function:
        a = self.peek().a
        words: list[str] = []
        while not self.is_id("function"):
            t = self.next()
            if t.kind == "id":
                if words and self.toks[self.i - 2].kind == "op" and self.toks[self.i - 2].val == ".":
                    words[-1] += "." + t.val
                else:
                    words.append(t.val)
        self.next()  # function
        name_tok = self.next()
        access = next((w.lower() for w in words if w.lower() in ACCESS), "public")
        rtypes = [w for w in words if w.lower() not in MODIFIERS]
        fn = Function(name_tok.val, a, a, name_tok.a, name_tok.b, access=access, returntype=rtypes[-1] if rtypes else None, script=True)
        fn.params = self.params(fn)
        attrs: dict[str, Expr] = {}
        while self.peek().kind == "id":
            key = self.next().val.lower()
            if self.accept("="):
                attrs[key] = self.primary()
            else:
                attrs[key] = lit(True)
        if lit_str(attrs.get("access")):
            fn.access = lit_str(attrs["access"]).lower()
        if lit_str(attrs.get("roles")) is not None:
            fn.roles = roles_list(lit_str(attrs["roles"]))
            fn.roles_at = (name_tok.a, self.prev_end())
        out = attrs.get("output")
        if out is not None and out.get("e") == "lit":
            fn.output = str(out["value"]).lower()
        if lit_str(attrs.get("returntype")):
            fn.returntype = lit_str(attrs["returntype"])
        if self.is_op("{"):
            open_i = self.i
            close_i = self.match[open_i]
            fn.locals = self._var_names(open_i, close_i if close_i > 0 else len(self.toks) - 1)
            self.run_function(fn, lambda: self._braced_body())
        else:
            self.end_statement()  # an interface method or abstract function has no body
        fn.end = self.prev_end()
        self.ctx.construct("cf:script-function", a, fn.end)
        self.ctx.functions.append(fn)
        return fn

    def _braced_body(self) -> list[Stmt]:
        self.expect("{")
        body = self.statements()
        self.expect("}")
        return body

    def run_function(self, fn: Function, parse) -> None:
        saved, saved_frames, saved_sinks = self.ctx.func, self.ctx.frames, self.ctx.sinks
        self.ctx.func, self.ctx.frames, self.ctx.sinks = fn, [], ["response"]
        try:
            fn.body = parse()
        finally:
            self.ctx.func, self.ctx.frames, self.ctx.sinks = saved, saved_frames, saved_sinks

    def _var_names(self, a: int, b: int) -> set[str]:
        names = set()
        for k in range(a, b):
            t = self.toks[k]
            if t.kind == "id" and t.low() == "var" and k + 1 < b and self.toks[k + 1].kind == "id":
                names.add(self.toks[k + 1].low())
        return names

    def params(self, fn: Function | None) -> list[dict[str, Any]]:
        self.expect("(")
        out: list[dict[str, Any]] = []
        while not self.is_op(")"):
            if self.peek().kind == "eof":
                raise ParseError("unterminated parameter list", self.peek().a)
            required = False
            if self.is_id("required") and self.peek(1).kind == "id":
                self.next()
                required = True
            first = self.dotted()
            ptype, pname = None, first
            if self.peek().kind == "id":  # `string name`: a type, then the name
                ptype, pname = first, self.next().val
            param: dict[str, Any] = {"name": pname, "type": cf_type(ptype), "required": required}
            if ptype and cf_type(ptype) == "any" and ptype.lower() != "any":
                param["declared_type"] = ptype
            if self.accept("="):
                param["default"] = self.expression()
            while self.peek().kind == "id" and self.is_op("=", 1):
                self.next()
                self.next()
                self.primary()
            out.append(param)
            if fn is not None:
                fn.args.add(pname.lower())
            if not self.accept(","):
                break
        self.expect(")")
        return out

    def dotted(self) -> str:
        t = self.next()
        if t.kind != "id":
            raise ParseError("expected a name", t.a)
        name = t.val
        while self.is_op(".") and self.peek(1).kind == "id":
            self.next()
            name += "." + self.next().val
        if self.is_op("[") and self.is_op("]", 1):  # string[]
            self.next()
            self.next()
            name += "[]"
        return name

    # -- components

    def component(self) -> list[Stmt]:
        """A script component: imports, `component attrs { body }`. Returns the pseudo-constructor."""
        while self.is_id("import"):
            self.st_import()
        modifiers = []
        while self.is_id("abstract") or self.is_id("final"):
            modifiers.append(self.next().low())
        if not (self.is_id("component") or self.is_id("interface")):
            raise ParseError("expected component", self.peek().a)
        kw = self.next()
        attrs: dict[str, Expr] = {}
        while not self.is_op("{") and self.peek().kind != "eof":
            t = self.next()
            if t.kind == "id" and self.accept("="):
                attrs[t.val.lower()] = self.primary()
            elif t.kind == "id":
                attrs[t.val.lower()] = lit(True)
        self.ctx.component = {k: v for k, v in ((k, lit_str(v) if lit_str(v) is not None else (v.get("value") if v.get("e") == "lit" else None)) for k, v in attrs.items()) if v is not None}
        self.ctx.component["kind"] = kw.low()
        for word in modifiers:
            self.ctx.component[word] = True
        self.ctx.construct("cf:script-component", kw.a, kw.b)
        self.expect("{")
        body = self.statements()
        self.accept("}")
        return body

    # -- tags as statements

    def tag_attrs(self) -> dict[str, Expr]:
        attrs: dict[str, Expr] = {}
        while self.peek().kind == "id" and self.is_op("=", 1):
            key = self.next().val.lower()
            self.next()
            attrs[key] = self.expression()
            self.accept(",")
        return attrs

    def script_tag(self, name: str) -> list[Stmt]:
        a = self.next().a
        attrs: dict[str, Expr] = {}
        if self.peek().kind == "str" and name in ("include", "abort", "location", "exit"):
            first = {"include": "template", "abort": "showerror", "location": "url", "exit": "method"}[name]
            attrs[first] = self.expression()
        attrs.update(self.tag_attrs())
        if name == "param" and not attrs and self.peek().kind == "id":
            # param [type] name [= default];
            words = [self.dotted()]
            if self.peek().kind == "id":
                words.append(self.dotted())
            attrs["name"] = lit(words[-1])
            if len(words) == 2:
                attrs["type"] = lit(words[0])
            if self.accept("="):
                attrs["default"] = self.expression()
        body = None
        if self.is_op("{"):
            body = self._tag_body(name, attrs)
        else:
            self.end_statement()
        return self._tag(name, attrs, body, a, self.prev_end())

    def _tag_body(self, name: str, attrs: dict[str, Expr]) -> list[Stmt]:
        sink = {"savecontent": "capture", "silent": "silent", "mail": "mail"}.get(name)
        if sink:
            self.ctx.sinks.append(sink)
        try:
            self.expect("{")
            body = self.statements()
            self.expect("}")
        finally:
            if sink:
                self.ctx.sinks.pop()
        return body

    def tag_call(self, kw: str) -> list[Stmt]:
        a = self.next().a
        name = kw[2:]
        self.expect("(")
        attrs: dict[str, Expr] = {}
        while not self.is_op(")") and self.peek().kind != "eof":
            if (self.peek().kind == "id" or self.peek().kind == "str") and (self.is_op("=", 1) or self.is_op(":", 1)):
                t = self.next()
                key = t.val.lower() if t.kind == "id" else "".join(p[3] or "" for p in t.parts).lower()
                self.next()
                attrs[key] = self.expression()
            else:
                attrs[f"arg{len(attrs)}"] = self.expression()
            if not self.accept(","):
                break
        self.expect(")")
        body = self._tag_body(name, attrs) if self.is_op("{") else None
        if body is None:
            self.end_statement()
        return self._tag(name, attrs, body, a, self.prev_end())

    def _tag(self, name: str, attrs: dict[str, Expr], body: list[Stmt] | None, a: int, b: int) -> list[Stmt]:
        if name == "query":
            self.ctx.construct("cf:cfquery", a, b)
            ds = lit_str(attrs.get("datasource"))
            self.ctx.queries.append(QueryFact(self.ctx.func, None, None, ds, (a, b) if ds else None, (a, b)))
            stmt: Stmt = {"op": "query", "at": self.ctx.at(a, b), "sql": "", "dynamic": True, "params": []}
            if ds:
                stmt["datasource"] = ds
            if lit_str(attrs.get("name")):
                stmt["target"] = assign_target(self.ctx, var(lit_str(attrs["name"])))
            return [stmt]
        if name == "mail":
            self.ctx.construct("cf:cfmail", a, b)
            from repo_intel.platforms.cfml.ir import clean_attrs

            return [{"op": "output", "at": self.ctx.at(a, b), "channel": "mail", "value": {"e": "struct", "fields": clean_attrs(attrs)}}, *(body or [])]
        if name in ("savecontent", "silent"):
            self.ctx.construct(f"cf:cf{name}", a, b)
            detail = {"variable": attrs["variable"]} if "variable" in attrs else {}
            return [platform(self.ctx, f"cf:cf{name}", self.ctx.at(a, b), {}, body or [], **detail)]
        if name in ("queryparam", "procparam", "mailparam", "pageencoding"):
            cid = f"cf:cf{name}" if name != "pageencoding" else "cf:cfprocessingdirective"
            self.ctx.construct(cid, a, b)
            from repo_intel.platforms.cfml.ir import clean_attrs

            return [platform(self.ctx, cid, self.ctx.at(a, b), clean_attrs(attrs), body)]
        return tag_statements(self.ctx, name, attrs, body, a, b)

    # -- simple statements

    def simple_statement_end(self) -> list[Stmt]:
        out = self.simple_statement()
        self.end_statement()
        return out

    def simple_statement(self) -> list[Stmt]:
        """`var x = e`, `target = e`, `target op= e`, `x++`, or an expression evaluated for its effect."""
        mark = len(self._chained)
        out = self._simple()
        extra = self._chained[mark:]
        del self._chained[mark:]
        return [*extra, *out]

    def _simple(self) -> list[Stmt]:
        a = self.peek().a
        if self.is_id("var") and self.peek(1).kind == "id":
            self.next()
            name_tok = self.next()
            self.ctx.construct("cf:var", a, name_tok.b)
            if self.ctx.frames:
                self.ctx.frames[-1][0].add(name_tok.low())
            elif self.ctx.func is not None:
                self.ctx.func.locals.add(name_tok.low())
            stmt: Stmt = {"op": "declare", "at": None, "name": name_tok.val, "scope": "local"}
            if self.accept("="):
                value = self.assignment_value()
                query = self.ctx.query_ops.get(id(value))
                if query is not None:
                    return [{**query, "at": self.at(a), "target": var(name_tok.val, "local")}]
                stmt["value"] = value
            stmt["at"] = self.at(a)
            return [stmt]
        lhs = self.expression()
        t = self.peek()
        if t.kind == "op" and t.val == "=":
            self.next()
            value = self.assignment_value()
            return [self._assign(lhs, value, a)]
        if t.kind == "op" and t.val in _COMPOUND:
            self.next()
            value = self.expression()
            target = assign_target(self.ctx, lhs)
            return [{"op": "assign", "at": self.at(a), "target": target, "value": {"e": "binop", "op": _COMPOUND[t.val], "left": lhs, "right": value}}]
        if t.kind == "op" and t.val in ("++", "--"):
            self.next()
            self.ctx.construct("cf:increment", a, t.b)
            return [{"op": "assign", "at": self.at(a), "target": assign_target(self.ctx, lhs), "value": {"e": "binop", "op": t.val[0], "left": lhs, "right": lit(1)}}]
        if lhs.get("e") == "unop" and lhs.get("increment"):
            operand = lhs["operand"]
            return [{"op": "assign", "at": self.at(a), "target": assign_target(self.ctx, operand), "value": {"e": "binop", "op": lhs["increment"], "left": operand, "right": lit(1)}}]
        return self.lower_call_statement(lhs, a)

    def assignment_value(self) -> Expr:
        """The right side of `=`, which may itself be an assignment (`a = b = 0`): the innermost value."""
        a = self.peek().a
        value = self.expression()
        if self.is_op("="):
            self.next()
            inner = self.assignment_value()
            self._chained.append(self._assign(value, inner, a))
            return inner
        return value

    def _assign(self, lhs: Expr, value: Expr, a: int) -> Stmt:
        query = self.ctx.query_ops.get(id(value))
        target = assign_target(self.ctx, lhs)
        self._record_setting(lhs, value, a)
        if query is not None:
            return {**query, "at": self.at(a), "target": target}
        return {"op": "assign", "at": self.at(a), "target": target, "value": value}

    def _record_setting(self, lhs: Expr, value: Expr, a: int) -> None:
        """`this.name = "x"` and friends at the top of Application.cfc."""
        if self.ctx.func is not None:
            return
        if lhs.get("e") == "var" and lhs.get("scope") == "this":
            key = lhs["name"].lower()
            if value.get("e") == "lit" and key in ("name", "datasource", "sessionmanagement", "clientmanagement", "loginstorage", "ormenabled", "setclientcookies", "scriptprotect"):
                self.ctx.settings[key] = value["value"]
                self.ctx.construct("cf:application-setting", a, self.prev_end())
            elif key in ("mappings", "customtagpaths", "sessiontimeout", "applicationtimeout", "ormsettings", "datasources"):
                self.ctx.settings.setdefault(key, True)
                self.ctx.construct("cf:application-setting", a, self.prev_end())
                if key == "mappings" and value.get("e") == "struct":
                    self.ctx.mappings.extend(value["fields"])
                if key == "datasources" and value.get("e") == "struct":
                    for ds in value["fields"]:  # names only: the values hold connection details
                        self.ctx.datasources.append((ds, (a, self.prev_end()), None))
        elif lhs.get("e") == "index" and lhs["object"].get("e") == "var" and lhs["object"].get("scope") == "this" and lhs["object"]["name"].lower() == "mappings":
            key = lit_str(lhs["index"])
            if key:
                self.ctx.mappings.append(key)
                self.ctx.construct("cf:application-setting", a, self.prev_end())

    def expression_statement(self) -> list[Stmt]:
        out = self.simple_statement()
        self.end_statement()
        return out

    def lower_call_statement(self, e: Expr, a: int) -> list[Stmt]:
        """An expression statement: built-ins with a neutral form become their statement."""
        at = self.at(a)
        query = self.ctx.query_ops.get(id(e))
        if query is not None:
            return [{**query, "at": at}]
        if e.get("e") == "call" and e["callee"].get("e") == "name":
            fname = e["callee"]["name"].lower()
            args, named = e.get("args", []), e.get("named", {})
            first = args[0] if args else None
            if fname in ("writeoutput", "echo"):
                self.ctx.construct("cf:writeOutput", a, self.prev_end())
                return [self.ctx.output(at, first or named.get("output"))]
            if fname in ("writedump", "dump"):
                self.ctx.construct("cf:cfdump", a, self.prev_end())
                return [self.ctx.output(at, first or named.get("var"), dump=True)]
            if fname == "writelog":
                self.ctx.construct("cf:cflog", a, self.prev_end())
                value = named.get("text") or first
                return [{"op": "output", "at": at, "channel": "log", **({"value": value} if value else {})}]
            if fname == "location":
                target = named.get("url") or first or lit("")
                return tag_statements(self.ctx, "location", {"url": target, **{k: v for k, v in named.items() if k != "url"}}, None, a, self.prev_end())
            if fname == "throw":
                self.ctx.construct("cf:cfthrow", a, self.prev_end())
                value = {"e": "struct", "fields": named} if named else first
                return [{"op": "throw", "at": at, **({"value": value} if value else {})}]
            if fname == "abort":
                return tag_statements(self.ctx, "abort", {}, None, a, self.prev_end())
        return [{"op": "call", "at": at, "call": e}]

    # ================================================================ expressions

    def expression(self) -> Expr:
        self.enter()
        try:
            return self.ternary()
        finally:
            self.leave()

    def ternary(self) -> Expr:
        a = self.peek().a
        test = self.binary(0)
        if self.is_op("?:"):
            self.next()
            other = self.ternary()
            return {"e": "cond", "at": self.at(a), "test": {"e": "unop", "op": "exists", "operand": test}, "then": test, "else": other}
        if self.is_op("?"):
            self.next()
            then = self.ternary()
            self.expect(":")
            other = self.ternary()
            return {"e": "cond", "at": self.at(a), "test": test, "then": then, "else": other}
        return test

    def binop_here(self) -> tuple[str, int, int] | None:
        """The binary operator at the cursor: (IR op, precedence, tokens it spans)."""
        t = self.peek()
        if t.kind == "op":
            hit = _SYMBOL_OPS.get(t.val)
            return (hit[0], hit[1], 1) if hit else None
        if t.kind != "id":
            return None
        for words, op, prec in _WORD_OPS:
            if t.low() != words[0]:
                continue
            if all(self.peek(k).kind == "id" and self.peek(k).low() == w for k, w in enumerate(words)):
                return op, prec, len(words)
        return None

    def binary(self, min_prec: int) -> Expr:
        a = self.peek().a
        left = self.prefix()
        while True:
            hit = self.binop_here()
            if hit is None or hit[1] < min_prec:
                return left
            op, prec, n = hit
            for _ in range(n):
                self.next()
            right = self.binary(prec if op == "^" else prec + 1)
            left = {"e": "binop", "at": self.at(a), "op": op, "left": left, "right": right}

    def prefix(self) -> Expr:
        t = self.peek()
        if (t.kind == "id" and t.low() == "not") or (t.kind == "op" and t.val == "!"):
            self.next()
            self.enter()
            try:
                operand = self.binary(7)
            finally:
                self.leave()
            return {"e": "unop", "at": self.at(t.a), "op": "not", "operand": operand}
        if t.kind == "op" and t.val in ("-", "+"):
            self.next()
            self.enter()
            try:
                operand = self.binary(14)
            finally:
                self.leave()
            return {"e": "unop", "at": self.at(t.a), "op": t.val, "operand": operand}
        if t.kind == "op" and t.val in ("++", "--"):
            self.next()
            operand = self.postfix(self.primary(), self.peek().a)
            self.ctx.construct("cf:increment", t.a, self.prev_end())
            return {"e": "unop", "at": self.at(t.a), "op": "+", "operand": operand, "increment": t.val[0]}
        return self.postfix(self.primary(), t.a)

    def postfix(self, e: Expr, start: int) -> Expr:
        while True:
            t = self.peek()
            if t.kind != "op":
                return e
            if t.val in (".", "?.") and self.peek(1).kind in ("id", "num"):
                self.next()
                member = self.next()
                name = member.val
                if e.get("e") == "name" and e.get("kind") == "scope" and t.val == ".":
                    e = self._scoped(e["name"], name, start, member.b)
                    continue
                e = {"e": "member", "at": self.at(start), "object": e, "name": name}
                if t.val == "?.":
                    e["safe"] = True
            elif t.val == "[":
                self.next()
                index = self.expression()
                self.expect("]")
                e = {"e": "index", "at": self.at(start), "object": e, "index": index}
            elif t.val == "(":
                args, named = self.call_args()
                e = self.make_call(e, args, named, start)
            elif t.val in ("++", "--"):
                if self.peek(1).kind in ("op", "eof") and not (self.peek(1).kind == "op" and self.peek(1).val in ("(", "[")):
                    self.next()
                    self.ctx.construct("cf:increment", start, t.b)
                    return {"e": "unop", "at": self.at(start), "op": "+", "operand": e, "increment": t.val[0], "postfix": True}
                return e
            elif t.val == "::" and self.peek(1).kind == "id":
                self.next()
                member = self.next()
                e = {"e": "member", "at": self.at(start), "object": e, "name": member.val, "static": True}
            else:
                return e

    def _scoped(self, scope: str, name: str, a: int, b: int) -> Expr:
        if scope in COUNTED_SCOPES:
            self.ctx.construct(f"cf:scope.{scope}", a, b)
        return var(name, scope, self.ctx.at(a, b))

    def call_args(self) -> tuple[list[Expr], dict[str, Expr]]:
        self.expect("(")
        args: list[Expr] = []
        named: dict[str, Expr] = {}
        while not self.is_op(")"):
            if self.peek().kind == "eof":
                raise ParseError("unterminated argument list", self.peek().a)
            t = self.peek()
            if t.kind in ("id", "str") and (self.is_op("=", 1) or self.is_op(":", 1)) and not self.is_op("==", 1):
                self.next()
                key = t.val if t.kind == "id" else "".join(p[3] or "" for p in t.parts)
                self.next()
                named[key] = self.expression()
            else:
                args.append(self.expression())
            if not self.accept(","):
                break
        self.expect(")")
        return args, named

    def make_call(self, callee: Expr, args: list[Expr], named: dict[str, Expr], a: int) -> Expr:
        call: Expr = {"e": "call", "at": self.at(a), "callee": callee, "args": args}
        if named:
            call["named"] = named
        if callee.get("e") != "name":
            if callee.get("e") == "var" and callee.get("scope") in ("this", "variables"):
                self.ctx.refs.append(Ref("function", callee["name"], (a, self.prev_end()), self.ctx.func))
                call["callee"] = {"e": "member", "object": {"e": "name", "name": callee["scope"], "kind": "scope"}, "name": callee["name"]}
            return call
        fname = callee["name"]
        low = fname.lower()
        b = self.prev_end()
        if low in BUILTINS or low.startswith("cf"):
            callee["kind"] = "builtin"
        else:
            callee["kind"] = "function"
            self.ctx.refs.append(Ref("function", fname, (a, b), self.ctx.func))
        if low == "queryexecute":
            self._query_execute(call, a, b)
        elif low == "createobject":
            return self._create_object(call, args, named, a, b)
        elif low in ("isuserinrole", "isuserinanyrole"):
            self.ctx.construct("cf:isUserInRole", a, b)
            raw = lit_str(args[0]) if args else lit_str(named.get("role") or named.get("rolename"))
            if raw is None:
                self.ctx.note(f"{fname} with a computed role: role not resolved", a)
            for role in roles_list(raw):
                self.ctx.role_checks.append((role, (a, b), self.ctx.func, low))
        elif low in DYNAMIC_FUNCTIONS:
            self.ctx.construct("cf:evaluate", a, b, False)
            self.ctx.note(f"{fname}() evaluates code built at run time: not followed", a)
        elif low in ORM_FUNCTIONS:
            self.ctx.construct("cf:orm-function", a, b)
        elif low == "invoke":
            self.ctx.construct("cf:invoke", a, b)
            target = lit_str(args[0]) if args else None
            method = lit_str(args[1]) if len(args) > 1 else None
            if target:
                self.ctx.refs.append(Ref("component", target, (a, b), self.ctx.func, method))
        elif low == "getauthuser":
            self.ctx.construct("cf:getAuthUser", a, b)
        elif low == "location":
            pass  # lowered as a statement; counted there
        return call

    def _create_object(self, call: Expr, args: list[Expr], named: dict[str, Expr], a: int, b: int) -> Expr:
        self.ctx.construct("cf:createObject", a, b)
        kind = lit_str(args[0]) if args else lit_str(named.get("type"))
        target = args[1] if len(args) > 1 else named.get("component") or named.get("class")
        if len(args) == 1 and kind and kind.lower() not in ("component", "java", "webservice", "com", "corba", "dotnet", ".net"):
            kind, target = "component", args[0]  # createObject("path.to.Component")
        kind = (kind or "component").lower()
        name = lit_str(target)
        if kind == "component":
            self.ctx.refs.append(Ref("component", name, (a, b), self.ctx.func))
        if name is None:
            self.ctx.note("createObject of a computed name: target not resolved", a)
        new: Expr = {"e": "new", "at": self.at(a), "type": name or "?", "kind": kind, "args": args[2:] if len(args) > 2 else []}
        if name is None and target is not None:
            new["type_expr"] = target
        return new

    def _query_execute(self, call: Expr, a: int, b: int) -> None:
        self.ctx.construct("cf:queryExecute", a, b)
        args, named = call.get("args", []), call.get("named", {})
        sql_e = args[0] if args else named.get("sql")
        params_e = args[1] if len(args) > 1 else named.get("params")
        options_e = args[2] if len(args) > 2 else named.get("options")
        sql, params, dynamic = sql_text(sql_e)
        params += query_params(params_e)
        stmt: Stmt = {"op": "query", "at": self.ctx.at(a, b), "sql": sql, "params": params}
        if dynamic:
            stmt["dynamic"] = True
        ds = None
        if isinstance(options_e, dict) and options_e.get("e") == "struct":
            for key, value in options_e["fields"].items():
                low = key.lower()
                if low == "datasource":
                    ds = lit_str(value)
                    if ds is None:
                        stmt["datasource_dynamic"] = True
                elif low in ("result", "returntype", "dbtype"):
                    if lit_str(value) is not None:
                        stmt[low] = lit_str(value)
        if ds:
            stmt["datasource"] = ds
        region = self._sql_region(sql_e)
        masked = None
        if region is not None:
            from repo_intel.platforms.cfml.lower import mask_sql

            masked = mask_sql(self.text[region[0]: region[1]])
        dbtype = stmt.get("dbtype")
        span = None
        if ds:  # evidence for the datasource: its name where the options give it
            k = self.text.find(ds, a, b)
            span = (k, k + len(ds)) if k >= 0 else (a, b)
        self.ctx.queries.append(QueryFact(self.ctx.func, region, masked, ds, span, (a, b), dbtype))
        self.ctx.query_ops[id(call)] = stmt

    def _sql_region(self, sql_e: Expr | None) -> tuple[int, int] | None:
        """The first string literal of the SQL argument, as written: what table lineage reads."""
        if not isinstance(sql_e, dict):
            return None
        node = sql_e
        while node.get("e") == "binop" and node.get("op") == "&":
            node = node["left"]
        t = self._str_tok.get(id(node))
        if t is not None:
            return (t.a + 1, max(t.a + 1, t.b - 1))
        return None

    def primary(self) -> Expr:
        t = self.next()
        a = t.a
        if t.kind == "num":
            value: Any = float(t.val) if any(c in t.val for c in ".eE") else int(t.val)
            return lit(value, self.ctx.at(t.a, t.b))
        if t.kind == "str":
            return self.string(t)
        if t.kind == "op":
            if t.val == "(":
                open_i = self.i - 1
                close = self.match[open_i]
                if close > 0 and close + 1 < len(self.toks) and self.toks[close + 1].kind == "op" and self.toks[close + 1].val == "=>":
                    self.i = open_i
                    return self.arrow(a)
                e = self.expression()
                self.expect(")")
                return e
            if t.val == "[":
                return self.array_or_ordered(a)
            if t.val == "{":
                return self.struct(a, "}")
            if t.val == "#":
                e = self.expression()
                self.expect("#")
                return e
            raise ParseError(f"unexpected {t.val!r}", t.a)
        if t.kind == "id":
            low = t.low()
            if low in ("true", "false"):
                return lit(low == "true", self.ctx.at(t.a, t.b))
            if low == "null":
                return lit(None, self.ctx.at(t.a, t.b))
            if low == "new" and self.peek().kind in ("id", "str"):
                return self.new_expr(a)
            if low == "function" and self.is_op("("):
                return self.closure(a)
            if self.is_op("=>"):
                self.i -= 1
                return self.arrow(a)
            if low in SCOPES and not self.is_op("(") and self.ctx.scope_of(t.val) is None:
                return {"e": "name", "name": low, "kind": "scope", "at": self.ctx.at(t.a, t.b)}
            if self.is_op("("):
                return {"e": "name", "name": t.val, "at": self.ctx.at(t.a, t.b)}
            scope = self.ctx.scope_of(t.val)
            return var(t.val, scope, self.ctx.at(t.a, t.b))
        raise ParseError("unexpected end of expression", t.a)

    def string(self, t: Tok) -> Expr:
        at = self.ctx.at(t.a, t.b)
        if not any(p[0] == "expr" for p in t.parts):
            e = lit("".join(p[3] or "" for p in t.parts), at)
            self._str_tok[id(e)] = t
            return e
        parts: list[Any] = []
        for kind, pa, pb, value in t.parts:
            if kind == "lit":
                parts.append(value)
            else:
                parts.append(self.ctx.parse_expr(pa, pb))
        if len(parts) == 1 and not isinstance(parts[0], str):
            return parts[0]
        e = {"e": "template", "at": at, "parts": parts}
        self._str_tok[id(e)] = t
        return e

    def new_expr(self, a: int) -> Expr:
        if self.peek().kind == "str":
            type_name = lit_str(self.string(self.next()))
        else:
            type_name = self.dotted()
        args: list[Expr] = []
        named: dict[str, Expr] = {}
        if self.is_op("("):
            args, named = self.call_args()
        b = self.prev_end()
        self.ctx.construct("cf:new", a, b)
        prefix = type_name.split(":", 1)[0].lower() if type_name and ":" in type_name else None
        kind = "java" if prefix == "java" else "component"
        if kind == "component":
            self.ctx.refs.append(Ref("component", type_name, (a, b), self.ctx.func))
        new: Expr = {"e": "new", "at": self.at(a), "type": type_name or "?", "kind": kind, "args": args}
        if named:
            new["named"] = named
        return new

    def closure(self, a: int) -> Expr:
        frame: tuple[set[str], set[str]] = (set(), set())
        params = self.params(None)
        frame[1].update(p["name"].lower() for p in params)
        while self.peek().kind == "id" and self.is_op("=", 1):
            self.next()
            self.next()
            self.primary()
        open_i = self.i
        frame[0].update(self._var_names(open_i, max(open_i, self.match[open_i])))
        self.ctx.frames.append(frame)
        try:
            body = self._braced_body()
        finally:
            self.ctx.frames.pop()
        self.ctx.construct("cf:closure", a, self.prev_end())
        return {"e": "lambda", "at": self.at(a), "params": [p["name"] for p in params], "body": body}

    def arrow(self, a: int) -> Expr:
        if self.is_op("("):
            params = [p["name"] for p in self.params(None)]
        else:
            params = [self.next().val]
        self.expect("=>")
        frame: tuple[set[str], set[str]] = (set(), {p.lower() for p in params})
        self.ctx.frames.append(frame)
        try:
            if self.is_op("{"):
                open_i = self.i
                frame[0].update(self._var_names(open_i, max(open_i, self.match[open_i])))
                body = self._braced_body()
            else:
                value = self.expression()
                body = [{"op": "return", "at": value.get("at") or self.at(a), "value": value}]
        finally:
            self.ctx.frames.pop()
        self.ctx.construct("cf:closure", a, self.prev_end())
        return {"e": "lambda", "at": self.at(a), "params": params, "body": body}

    def array_or_ordered(self, a: int) -> Expr:
        if self.is_op("]"):
            self.next()
            return {"e": "array", "at": self.at(a), "items": []}
        first = self.peek()
        if first.kind in ("id", "str") and (self.is_op(":", 1) or self.is_op("=", 1)):
            e = self.struct(a, "]")
            e["ordered"] = True
            return e
        items = [self.expression()]
        while self.accept(","):
            if self.is_op("]"):
                break
            items.append(self.expression())
        self.expect("]")
        return {"e": "array", "at": self.at(a), "items": items}

    def struct(self, a: int, close: str) -> Expr:
        fields: dict[str, Expr] = {}
        while not self.is_op(close):
            t = self.next()
            if t.kind == "id":
                key = t.val
                while self.is_op(".") and self.peek(1).kind == "id":  # {a.b = 1} sets a nested key
                    self.next()
                    key += "." + self.next().val
            elif t.kind == "str":
                key = "".join(p[3] or "" for p in t.parts) if not any(p[0] == "expr" for p in t.parts) else self.text[t.a + 1: t.b - 1]
            elif t.kind == "num":
                key = t.val
            else:
                raise ParseError("expected a struct key", t.a)
            if not (self.accept(":") or self.accept("=")):
                raise ParseError("expected : or = in a struct literal", self.peek().a)
            fields[key] = self.expression()
            if not self.accept(","):
                break
        self.expect(close)
        return {"e": "struct", "at": self.at(a), "fields": fields}


# Tags callable as functions in script: `cfhttp(url = "...")`, `cflocation(url = "...")`.
_TAG_CALLS = frozenset({"include", "location", "abort", "query", "mail", "http", "lock", "transaction", "thread", "savecontent", "param", "log", "dump", "file", "invoke", "module", "schedule", "loginuser", "header", "content", "cookie", "setting", "directory", "execute", "ftp", "storedproc", "spreadsheet", "pdf", "document", "zip", "image", "httpparam", "queryparam", "mailparam", "procparam", "object", "throw"})


def sql_text(e: Expr | None) -> tuple[str, list[dict[str, Any]], bool]:
    """SQL text for a query from its expression: interpolations become `:name` parameters.

    Returns (sql, inline parameters, dynamic). A string built at run time is dynamic."""
    params: list[dict[str, Any]] = []
    names: set[str] = set()
    if e is None:
        return "", params, True
    pieces: list[Any] = []
    dynamic = False

    def flatten(node: Expr) -> None:
        nonlocal dynamic
        if node.get("e") == "binop" and node.get("op") == "&":
            flatten(node["left"])
            flatten(node["right"])
        elif node.get("e") == "lit":
            pieces.append(str(node.get("value") if node.get("value") is not None else ""))
        elif node.get("e") == "template":
            pieces.extend(node["parts"])
        else:
            dynamic = True
            pieces.append(node)

    flatten(e)
    out: list[str] = []
    for piece in pieces:
        if isinstance(piece, str):
            out.append(piece)
        else:
            name = param_name(piece, names, len(params))
            out.append(f":{name}")
            params.append({"name": name, "value": piece, "type": "any", "inline": True})
    return "".join(out), params, dynamic


def param_name(value: Expr, taken: set[str], n: int) -> str:
    base = None
    if value.get("e") == "var":
        base = value["name"]
    elif value.get("e") == "member":
        base = value["name"]
    base = re.sub(r"\W", "_", base or f"p{n + 1}")
    name, k = base, 2
    while name.lower() in taken:
        name, k = f"{base}_{k}", k + 1
    taken.add(name.lower())
    return name


def query_params(e: Expr | None) -> list[dict[str, Any]]:
    """`{id = x}`, `{id = {value = x, cfsqltype = "cf_sql_integer"}}` or `[x, y]` as query parameters."""
    if not isinstance(e, dict):
        return []
    out = []
    if e.get("e") == "struct":
        for key, value in e["fields"].items():
            param = {"name": key, "value": value, "type": "any"}
            if value.get("e") == "struct":
                fields = {k.lower(): v for k, v in value["fields"].items()}
                if "value" in fields:
                    param["value"] = fields["value"]
                t = lit_str(fields.get("cfsqltype") or fields.get("sqltype"))
                if t:
                    param["type"] = sql_type(t)
                if fields.get("list") is not None:
                    param["list"] = True
            out.append(param)
    elif e.get("e") == "array":
        for i, value in enumerate(e["items"], start=1):
            param = {"name": str(i), "value": value, "type": "any", "position": i}
            if value.get("e") == "struct":
                fields = {k.lower(): v for k, v in value["fields"].items()}
                if "value" in fields:
                    param["value"] = fields["value"]
                t = lit_str(fields.get("cfsqltype") or fields.get("sqltype"))
                if t:
                    param["type"] = sql_type(t)
            out.append(param)
    else:
        out.append({"name": "params", "value": e, "type": "struct", "dynamic": True})
    return out


__all__ = ["ParseError", "Parser", "lex", "parse_expression", "parse_statements", "sql_text"]
