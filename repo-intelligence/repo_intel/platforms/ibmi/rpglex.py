"""RPG tokens and expressions: the free-form tokenizer and an expression parser that lowers
into the behavioural IR (repo_intel.compile.behavior).

The tokenizer reads a character range of a text in one pass. RPG names are case-insensitive:
names are kept as written in `Tok.text` and compared through `Tok.up`. A `*` directly before
a letter in operand position is a special word (`*ON`, `*INLR`, `*IN50`, `*BLANKS`); `%`
before a letter is a built-in function. Hyphenated opcodes and declarations (`DCL-S`,
`EVAL-CORR`, `ON-ERROR`, `Z-ADD`) are one token.

The parser follows RPG IV precedence, lowest first: OR, AND, NOT, comparisons
(`= <> < > <= >=`), `+ -`, `* /`, `**`, unary `+ -`. A name followed by parentheses is a
call when the member declares it as a procedure or prototype, an index when it declares an
array, a call when it has no or several arguments (an array takes one subscript), and
otherwise an index marked `maybe_call`, since a prototype may come from a /COPY member this
reader does not see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_#@$")
_NAME_CHARS = _NAME_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")
HYPHENATED = frozenset({
    "DCL-S", "DCL-C", "DCL-DS", "DCL-PR", "DCL-PI", "DCL-PROC", "DCL-F", "DCL-SUBF", "DCL-PARM", "DCL-ENUM",
    "END-DS", "END-PR", "END-PI", "END-PROC", "END-ENUM", "CTL-OPT", "EVAL-CORR", "ON-ERROR", "ON-EXIT",
    "WHEN-IS", "WHEN-IN", "Z-ADD", "Z-SUB", "XML-INTO", "XML-SAX", "DATA-INTO", "DATA-GEN", "SND-MSG", "END-EXEC",
})
_OPS3 = ("**=",)
_OPS2 = ("**", "+=", "-=", "*=", "/=", "<>", "<=", ">=")


@dataclass
class Tok:
    kind: str  # name, special, bif, num, str, op
    text: str  # as written; a string's value with '' read as '
    start: int
    end: int
    space: bool  # whitespace (or a line break) before it
    prefix: str = ""  # a typed literal's letter: X, D, T, Z, G, U, C

    @property
    def up(self) -> str:
        return self.text.upper()

    def is_(self, *words: str) -> bool:
        return self.kind == "name" and self.up in words

    def op(self, *texts: str) -> bool:
        return self.kind == "op" and self.text in texts


def tokenize(text: str, a: int = 0, b: int | None = None) -> list[Tok]:
    """Tokens of `text[a:b]`, in one pass."""
    b = len(text) if b is None else b
    out: list[Tok] = []
    i = a
    space = True
    while i < b:
        c = text[i]
        if c in " \t\r\n\f":
            i += 1
            space = True
            continue
        start = i
        if c == "'" or (c.upper() in "XDTZGUC" and i + 1 < b and text[i + 1] == "'" and (not out or out[-1].end != i or out[-1].kind == "op")):
            prefix = "" if c == "'" else c.upper()
            i += 1 if c == "'" else 2
            value: list[str] = []
            while i < b:
                if text[i] == "'":
                    if i + 1 < b and text[i + 1] == "'":
                        value.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                if text[i] == "\n":  # strings do not span lines here: an unclosed one ends at the line
                    break
                value.append(text[i])
                i += 1
            out.append(Tok("str", "".join(value), start, i, space, prefix))
        elif c in _DIGITS or (c == "." and i + 1 < b and text[i + 1] in _DIGITS):
            while i < b and text[i] in _DIGITS:
                i += 1
            if i < b and text[i] == "." and i + 1 < b and text[i + 1] in _DIGITS:
                i += 1
                while i < b and text[i] in _DIGITS:
                    i += 1
            out.append(Tok("num", text[start:i], start, i, space))
        elif c in _NAME_START:
            while i < b and text[i] in _NAME_CHARS:
                i += 1
            # A hyphenated keyword is one token: DCL-S, EVAL-CORR, Z-ADD.
            if i < b and text[i] == "-" and i + 1 < b and text[i + 1] in _NAME_START:
                j = i + 1
                while j < b and text[j] in _NAME_CHARS:
                    j += 1
                if text[start:j].upper() in HYPHENATED:
                    i = j
            out.append(Tok("name", text[start:i], start, i, space))
        elif c == "%" and i + 1 < b and text[i + 1] in _NAME_START:
            i += 1
            while i < b and text[i] in _NAME_CHARS:
                i += 1
            out.append(Tok("bif", text[start:i], start, i, space))
        elif c == "*" and i + 1 < b and text[i + 1] in _NAME_START and _operand_position(out):
            # *ON, *INLR, *IN50, *BLANKS, *ENTRY: a special word where an operand may start.
            i += 1
            while i < b and text[i] in _NAME_CHARS:
                i += 1
            out.append(Tok("special", text[start:i], start, i, space))
        else:
            three, two = text[i : i + 3], text[i : i + 2]
            if three in _OPS3:
                i += 3
            elif two in _OPS2:
                i += 2
            else:
                i += 1
            out.append(Tok("op", text[start:i], start, i, space))
        space = False
    return out


# Words after which `*NAME` is an operand, not a multiplication: opcodes and keywords.
_BEFORE_SPECIAL = frozenset({
    "IF", "ELSEIF", "DOW", "DOU", "WHEN", "RETURN", "CHAIN", "READ", "READE", "READP", "READPE", "SETLL", "SETGT", "DELETE",
    "WRITE", "UPDATE", "CLEAR", "RESET", "OPEN", "CLOSE", "UNLOCK", "TO", "DOWNTO", "BY", "IN", "OUT", "ON-ERROR", "DSPLY",
    "LEAVESR", "EXSR", "BEGSR", "ENDSR", "INZ", "CTL-OPT", "DCL-S", "DCL-C", "DCL-PI", "DCL-PR", "DCL-DS", "NOT", "AND", "OR",
    "EVAL", "EVALR", "CALLP", "SORTA", "DEALLOC", "TEST", "FEOD", "EXFMT", "READC", "WHEN-IS", "WHEN-IN", "COMMIT", "ROLBK",
})


def _operand_position(out: list[Tok]) -> bool:
    """Whether an operand may start after the tokens so far: at the start, after an operator
    other than `)`, or after an opcode or keyword (`IF *IN03`, `RETURN *ON`)."""
    if not out:
        return True
    prev = out[-1]
    if prev.kind == "op":
        return prev.text != ")"
    return prev.kind == "name" and prev.up in _BEFORE_SPECIAL


def split_groups(toks: list[Tok]) -> list[list[Tok]]:
    """Operands of an operation: token groups at depth 0 separated by blanks where one
    operand ends and another begins (`chain (a:b) custmast ds`)."""
    groups: list[list[Tok]] = []
    depth = 0
    for t in toks:
        if not groups:
            groups.append([t])
        else:
            prev = groups[-1][-1]
            ends = prev.kind in ("name", "special", "bif", "num", "str") or prev.op(")")
            begins = t.kind in ("name", "special", "bif", "num", "str") or t.op("(")
            if depth == 0 and t.space and ends and begins and not _binary_word(prev) and not _binary_word(t):
                groups.append([t])
            else:
                groups[-1].append(t)
        if t.op("("):
            depth += 1
        elif t.op(")"):
            depth = max(0, depth - 1)
    return groups


def _binary_word(t: Tok) -> bool:
    return t.kind == "name" and t.up in ("AND", "OR", "NOT")


# --------------------------------------------------------------------- expressions

_BINARY = {"OR": (1, "or"), "AND": (2, "and"), "=": (4, "=="), "<>": (4, "!="), "<": (4, "<"), ">": (4, ">"), "<=": (4, "<="), ">=": (4, ">="),
           "+": (5, "+"), "-": (5, "-"), "*": (6, "*"), "/": (6, "/"), "**": (7, "^")}
FIGURATIVE = {"*ON": (True, "boolean"), "*OFF": (False, "boolean"), "*BLANK": ("", "string"), "*BLANKS": ("", "string"),
              "*ZERO": (0, "integer"), "*ZEROS": (0, "integer"), "*NULL": (None, "null")}


def indicator_name(word: str) -> str | None:
    """`*IN50` -> `*in50`, `*INLR` -> `*inlr`; None for other special words."""
    w = word.upper()
    if w.startswith("*IN") and len(w) in (5, 6) and (w[3:].isdigit() or w[3:] in ("LR", "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "RT", "OA", "OB", "OC", "OD", "OE", "OF", "OG", "OV", "MR") or (len(w) == 5 and w[3] == "K")):
        return w.lower()
    return None


class Expr:
    """Expression parser over `toks[i:stop]`. `kind_of(name)` says what the member declares
    a name as: "procedure", "array" or None."""

    def __init__(self, toks: list[Tok], kind_of: Callable[[str], str | None], on_unread: Callable[[Tok], None] | None = None):
        self.toks = toks
        self.i = 0
        self.kind_of = kind_of
        self.on_unread = on_unread
        self.calls: list[tuple[str, Tok]] = []  # names called as procedures, in order

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def done(self) -> bool:
        return self.i >= len(self.toks)

    def parse(self, min_prec: int = 0) -> dict[str, Any]:
        t = self.peek()
        if t is not None and t.is_("NOT") and min_prec <= 3:
            self.next()
            left: dict[str, Any] = {"e": "unop", "op": "not", "operand": self.parse(3)}
        else:
            left = self.unary()
        while True:
            t = self.peek()
            if t is None:
                break
            key = t.up if t.kind == "name" else t.text if t.kind == "op" else None
            if key not in _BINARY:
                break
            prec, op = _BINARY[key]
            if prec < min_prec:
                break
            self.next()
            right = self.parse(prec if op == "^" else prec + 1)
            left = {"e": "binop", "op": op, "left": left, "right": right}
        return left

    def unary(self) -> dict[str, Any]:
        t = self.peek()
        if t is not None and t.op("-", "+"):
            self.next()
            operand = self.parse(7)
            if t.text == "-" and operand.get("e") == "lit" and isinstance(operand.get("value"), (int, float)) and not isinstance(operand.get("value"), bool):
                return {**operand, "value": -operand["value"]}
            return {"e": "unop", "op": t.text, "operand": operand}
        if t is not None and t.is_("NOT"):
            self.next()
            return {"e": "unop", "op": "not", "operand": self.parse(3)}
        return self.postfix(self.primary())

    def args(self) -> list[dict[str, Any]]:
        """`( a : b : c )`, the opening parenthesis next."""
        self.next()
        out: list[dict[str, Any]] = []
        t = self.peek()
        if t is not None and t.op(")"):
            self.next()
            return out
        while not self.done():
            out.append(self.parse())
            t = self.peek()
            if t is None:
                break
            if t.op(":", ","):
                self.next()
                continue
            if t.op(")"):
                self.next()
                break
            self.unread(self.next())
        return out

    def unread(self, t: Tok) -> dict[str, Any]:
        if self.on_unread is not None:
            self.on_unread(t)
        return {"e": "platform", "construct": "ibmi:unrecognised_expression", "text": t.text[:40]}

    def primary(self) -> dict[str, Any]:
        if self.done():
            return {"e": "platform", "construct": "ibmi:unrecognised_expression", "text": ""}
        t = self.next()
        if t.kind == "num":
            if "." in t.text:
                return {"e": "lit", "value": float(t.text), "type": "number"}
            return {"e": "lit", "value": int(t.text), "type": "integer"}
        if t.kind == "str":
            kind = {"X": "binary", "D": "date", "Z": "datetime"}.get(t.prefix, "string")
            return {"e": "lit", "value": t.text, "type": kind}
        if t.kind == "special":
            ind = indicator_name(t.text)
            if ind is not None:
                return {"e": "var", "scope": "indicator", "name": ind}
            if t.up == "*IN":
                nxt = self.peek()
                if nxt is not None and nxt.op("("):
                    args = self.args()
                    if len(args) == 1 and args[0].get("e") == "lit" and isinstance(args[0].get("value"), int):
                        return {"e": "var", "scope": "indicator", "name": f"*in{args[0]['value']:02d}"}
                    return {"e": "index", "object": {"e": "var", "scope": "indicator", "name": "*in"}, "index": args[0] if args else {"e": "lit", "value": 0, "type": "integer"}}
            if t.up in FIGURATIVE:
                value, kind = FIGURATIVE[t.up]
                return {"e": "lit", "value": value, "type": kind}
            nxt = self.peek()
            if t.up == "*ALL" and nxt is not None and nxt.kind == "str" and not nxt.space:
                self.next()
                return {"e": "call", "callee": {"e": "name", "name": "*all", "kind": "builtin"}, "args": [{"e": "lit", "value": nxt.text, "type": "string"}]}
            return {"e": "name", "name": t.text.lower(), "kind": "special"}
        if t.kind == "bif":
            nxt = self.peek()
            args = self.args() if nxt is not None and nxt.op("(") else []
            return {"e": "call", "callee": {"e": "name", "name": t.text.lower(), "kind": "builtin"}, "args": args}
        if t.kind == "name":
            name = t.text.lower()
            nxt = self.peek()
            if nxt is not None and nxt.op("("):
                args = self.args()
                kind = self.kind_of(name)
                if kind == "procedure" or (kind != "array" and len(args) != 1):
                    self.calls.append((name, t))
                    return {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": args}
                node: dict[str, Any] = {"e": "index", "object": {"e": "var", "name": name}, "index": args[0]}
                if kind is None:
                    node["maybe_call"] = True
                    self.calls.append((name, t))
                return node
            return {"e": "var", "name": name}
        if t.op("("):
            inner = self.parse()
            items = [inner]
            while self.peek() is not None and self.peek().op(":"):
                self.next()
                items.append(self.parse())
            if self.peek() is not None and self.peek().op(")"):
                self.next()
            return inner if len(items) == 1 else {"e": "array", "items": items}
        return self.unread(t)

    def postfix(self, node: dict[str, Any]) -> dict[str, Any]:
        while True:
            t = self.peek()
            if t is None or not t.op(".") or self.i + 1 >= len(self.toks) or self.toks[self.i + 1].kind != "name":
                return node
            self.next()
            member = self.next().text.lower()
            nxt = self.peek()
            node = {"e": "member", "object": node, "name": member}
            if nxt is not None and nxt.op("(") and not nxt.space:
                args = self.args()
                node = {"e": "index", "object": node, "index": args[0] if args else {"e": "lit", "value": 0, "type": "integer"}}


def parse_expr(toks: list[Tok], kind_of: Callable[[str], str | None], on_unread: Callable[[Tok], None] | None = None) -> tuple[dict[str, Any], list[tuple[str, Tok]]]:
    """One expression over all of `toks`; tokens left over make it a platform node."""
    p = Expr(toks, kind_of, on_unread)
    node = p.parse()
    if not p.done():
        rest = p.next()
        if on_unread is not None:
            on_unread(rest)
        node = {"e": "platform", "construct": "ibmi:unrecognised_expression", "text": " ".join(t.text for t in toks)[:80]}
    return node, p.calls


def find_assignment(toks: list[Tok]) -> int | None:
    """Index of a statement's assignment operator (`=`, `+=`, ...) at depth 0, if any."""
    depth = 0
    for k, t in enumerate(toks):
        if t.op("("):
            depth += 1
        elif t.op(")"):
            depth -= 1
        elif depth == 0 and t.op("=", "+=", "-=", "*=", "/=", "**="):
            return k
    return None
