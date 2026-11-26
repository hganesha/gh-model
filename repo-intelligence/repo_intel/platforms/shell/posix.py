"""A parser for POSIX shell scripts (sh, ksh, bash): words, here-documents and compound commands.

It reads the grammar real batch scripts use: simple commands with assignments and
redirections, pipelines, `&&` and `||` lists, background jobs, `if`, `case`, `for`,
`while`, `until`, `select`, functions (`name() {...}` and `function name {...}`), groups,
subshells, `[[ ]]` and `(( ))`. Words keep their parts (literal text, `$var` and `${var...}`,
`$(...)` and backticks, `$((...))`), so a later step can tell a literal from a value made at
run time. Here-documents keep the span of their body, and whether their delimiter was
quoted (no expansion inside).

Nothing is run or expanded. The parser is tolerant: a token it cannot place is skipped and
reported in `Script.problems`, so one odd line does not lose the rest of the file. Every
node keeps character offsets into the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RESERVED = {"if", "then", "elif", "else", "fi", "do", "done", "case", "esac", "while", "until", "for", "select", "in", "function", "{", "}", "!", "[[", "]]", "time"}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?(\+?=)")


# ------------------------------------------------------------------ nodes


@dataclass
class Part:
    kind: str  # lit | var | cmd | arith | procsub
    text: str  # lit: the text; var: the name; cmd/arith/procsub: the inner source
    start: int
    end: int
    modifier: str = ""  # var: what follows the name inside ${...} (`:-default`, `%.*`, ...)
    quoted: bool = False
    inner: int = 0  # cmd/arith/procsub: offset of the inner source


@dataclass
class Word:
    parts: list[Part]
    start: int
    end: int
    quoted: bool = False

    def literal(self) -> str | None:
        """The word's text when it holds no expansion, else None."""
        if all(p.kind == "lit" for p in self.parts):
            return "".join(p.text for p in self.parts)
        return None

    def raw(self, text: str) -> str:
        return text[self.start:self.end]


@dataclass
class Heredoc:
    delimiter: str
    quoted: bool  # a quoted delimiter: the body is literal
    strip_tabs: bool
    start: int = 0  # body span
    end: int = 0
    parts: list[Part] = field(default_factory=list)  # the body's parts when it is expanded


@dataclass
class Redirect:
    op: str  # >, >>, <, <<, <<-, <<<, >&, <&, <>, >|, &>, &>>
    fd: str
    target: Word | None
    start: int
    end: int
    heredoc: Heredoc | None = None


@dataclass
class Node:
    start: int
    end: int


@dataclass
class Command(Node):
    assigns: list[tuple[str, Word | None, int, int]] = field(default_factory=list)  # name, value, span
    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Pipeline(Node):
    commands: list[Node] = field(default_factory=list)
    negated: bool = False


@dataclass
class AndOr(Node):
    first: Node | None = None
    rest: list[tuple[str, Node]] = field(default_factory=list)


@dataclass
class Background(Node):
    body: Node | None = None


@dataclass
class If(Node):
    clauses: list[tuple[list[Node], list[Node]]] = field(default_factory=list)
    else_body: list[Node] | None = None
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Loop(Node):
    kind: str = "while"  # for | select | while | until | arith
    var: str | None = None
    items: list[Word] | None = None
    cond: list[Node] = field(default_factory=list)
    body: list[Node] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    arith: str | None = None  # for ((...))


@dataclass
class Case(Node):
    word: Word | None = None
    items: list[tuple[list[Word], list[Node], int, int]] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Function(Node):
    name: str = ""
    body: list[Node] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Group(Node):
    body: list[Node] = field(default_factory=list)
    subshell: bool = False
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Test(Node):
    """`[[ ... ]]` (kind `test`) or `(( ... ))` (kind `arith`); words of a [[ ]] test."""

    kind: str = "test"
    words: list[Word] = field(default_factory=list)
    text: str = ""
    redirects: list[Redirect] = field(default_factory=list)


@dataclass
class Script:
    body: list[Node]
    problems: list[tuple[int, int, str]]
    heredocs: list[Heredoc]


# ------------------------------------------------------------------ the lexer


@dataclass
class Tok:
    kind: str  # word | op | newline | redirect | eof
    value: Any  # word: Word; op: text; redirect: Redirect
    start: int
    end: int


class _Lexer:
    def __init__(self, text: str, a: int = 0, b: int | None = None):
        self.s = text
        self.n = len(text) if b is None else b
        self.i = a
        self.toks: list[Tok] = []
        self.pending: list[Heredoc] = []
        self.heredocs: list[Heredoc] = []
        self.problems: list[tuple[int, int, str]] = []

    def run(self) -> list[Tok]:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\f\v":
                self.i += 1
            elif c == "\\" and s.startswith("\\\n", self.i):
                self.i += 2
            elif c == "#":
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
            elif c == "\n":
                self.toks.append(Tok("newline", "\n", self.i, self.i + 1))
                self.i += 1
                if self.pending:
                    self._bodies()
            elif c in "<>" or (c == "&" and s.startswith(">", self.i + 1)):
                self._redirect("")
            elif c in ";&|()":
                self._op()
            elif c.isdigit() and self._fd_redirect():
                pass
            else:
                self._word_token()
        if self.pending:
            for h in self.pending:
                self.problems.append((self.n, self.n, f"here-document {h.delimiter} has no end"))
                h.start = h.end = self.n
            self.pending = []
        self.toks.append(Tok("eof", None, self.n, self.n))
        return self.toks

    # -- operators

    def _op(self) -> None:
        s, i = self.s, self.i
        for op in (";;&", ";;", ";&", "&&", "||", "|&", "((", ";", "&", "|", "(", ")"):
            if s.startswith(op, i):
                if op == "((" and not self._command_start():
                    op = "("
                self.toks.append(Tok("op", op, i, i + len(op)))
                self.i = i + len(op)
                return

    def _command_start(self) -> bool:
        if not self.toks:
            return True
        last = self.toks[-1]
        return last.kind == "newline" or (last.kind == "op" and last.value in (";", "&", "&&", "||", "|", "(", ")", "((", ";;", "|&")) or (
            last.kind == "word" and last.value.literal() in ("do", "then", "else", "elif", "if", "while", "until", "{", "!", "time"))

    def _fd_redirect(self) -> bool:
        m = re.compile(r"\d+(?=[<>])").match(self.s, self.i)
        if not m:
            return False
        prev = self.s[self.i - 1] if self.i else " "
        if prev not in " \t\n;&|()":
            return False
        self.i = m.end()
        self._redirect(m.group())
        return True

    def _redirect(self, fd: str) -> None:
        s, start = self.s, self.i - len(fd)
        if s.startswith("<(", self.i) or s.startswith(">(", self.i):  # process substitution is a word
            self.i = start
            self._word_token()
            return
        for op in ("&>>", "<<<", "<<-", "<<", "<&", "<>", ">>", ">&", ">|", "&>", "<", ">"):
            if s.startswith(op, self.i):
                break
        self.i += len(op)
        while self.i < self.n and s[self.i] in " \t":
            self.i += 1
        target = None
        if self.i < self.n and s[self.i] not in "\n;&|()<>":
            target = self._word()
        r = Redirect(op, fd, target, start, self.i)
        if op in ("<<", "<<-") and target is not None:
            delim = "".join(p.text if p.kind == "lit" else s[p.start:p.end] for p in target.parts)
            h = Heredoc(delim, target.quoted, op == "<<-")
            r.heredoc = h
            self.pending.append(h)
            self.heredocs.append(h)
        self.toks.append(Tok("redirect", r, start, self.i))

    def _bodies(self) -> None:
        """Read the bodies of the here-documents opened on the line just ended."""
        s = self.s
        for h in self.pending:
            h.start = self.i
            while self.i < self.n:
                end = s.find("\n", self.i)
                line_end = self.n if end < 0 else end
                line = s[self.i:line_end]
                check = line.lstrip("\t") if h.strip_tabs else line
                if check.rstrip("\r") == h.delimiter:
                    h.end = self.i
                    self.i = line_end + 1 if end >= 0 else self.n
                    break
                self.i = line_end + 1 if end >= 0 else self.n
            else:
                h.end = self.n
                self.problems.append((h.start, h.end, f"here-document {h.delimiter} has no end"))
            if not h.quoted:
                h.parts = expansions(s, h.start, h.end)
        self.pending = []

    # -- words

    def _word_token(self) -> None:
        w = self._word()
        if w.start == w.end:  # nothing readable: skip a character so the loop moves on
            self.i += 1
            return
        self.toks.append(Tok("word", w, w.start, w.end))

    def _word(self) -> Word:
        s, n = self.s, self.n
        start = self.i
        parts: list[Part] = []
        quoted = False
        buf: list[str] = []
        buf_start = self.i

        def flush(at: int) -> None:
            nonlocal buf, buf_start
            if buf:
                parts.append(Part("lit", "".join(buf), buf_start, at))
            buf = []
            buf_start = at

        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n;&|()<>":
                if c in "<>" and s.startswith("(", self.i + 1):  # <(...) process substitution
                    flush(self.i)
                    a = self.i
                    end = _balanced(s, self.i + 1, "(", ")")
                    parts.append(Part("procsub", s[self.i + 2:end - 1], a, end, inner=self.i + 2))
                    self.i = end
                    buf_start = end
                    continue
                if c == "(" and buf and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=", "".join(buf)) and not parts:
                    end = _balanced(s, self.i, "(", ")")  # an array assignment a=(x y)
                    buf.append(s[self.i:end])
                    self.i = end
                    continue
                break
            if c == "\\":
                flush(self.i)
                if s.startswith("\\\n", self.i):
                    self.i += 2
                    buf_start = self.i
                    continue
                if self.i + 1 < n:
                    parts.append(Part("lit", s[self.i + 1], self.i, self.i + 2, quoted=True))
                    quoted = True
                self.i += 2
                buf_start = self.i
                continue
            if c == "'":
                flush(self.i)
                end = s.find("'", self.i + 1)
                end = n if end < 0 else end
                parts.append(Part("lit", s[self.i + 1:end], self.i, min(end + 1, n), quoted=True))
                quoted = True
                self.i = min(end + 1, n)
                buf_start = self.i
                continue
            if c == "$" and s.startswith("$'", self.i):
                flush(self.i)
                end = self.i + 2
                while end < n and s[end] != "'":
                    end += 2 if s[end] == "\\" else 1
                parts.append(Part("lit", _ansi(s[self.i + 2:min(end, n)]), self.i, min(end + 1, n), quoted=True))
                quoted = True
                self.i = min(end + 1, n)
                buf_start = self.i
                continue
            if c == '"':
                flush(self.i)
                end = min(_dquote_end(s, self.i), n)
                inner_end = end - 1 if end - 1 > self.i and s[end - 1] == '"' else end
                parts.extend(expansions(s, self.i + 1, inner_end, quoted=True))
                if inner_end <= self.i + 1:  # "" is an empty literal
                    parts.append(Part("lit", "", self.i, end, quoted=True))
                quoted = True
                self.i = end
                buf_start = self.i
                continue
            if c == "$" or c == "`":
                flush(self.i)
                p, self.i = _expansion(s, self.i)
                if p is None:
                    buf.append(c)
                    buf_start = self.i - 1
                    continue
                parts.append(p)
                buf_start = self.i
                continue
            buf.append(c)
            self.i += 1
        flush(self.i)
        return Word(parts, start, self.i, quoted)


def _ansi(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\", "'": "'"}.get(m.group(1), m.group(1)), body)


def _dquote_end(s: str, i: int) -> int:
    """The offset just past the double-quoted string that starts at s[i]."""
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
        elif c == '"':
            return j + 1
        elif c == "$" and s.startswith("$(", j):
            j = _balanced(s, j + 1, "(", ")")
        elif c == "$" and s.startswith("${", j):
            j = _balanced(s, j + 1, "{", "}")
        elif c == "`":
            k = s.find("`", j + 1)
            j = n if k < 0 else k + 1
        else:
            j += 1
    return n


def _balanced(s: str, i: int, open_: str, close: str) -> int:
    """The offset just past the bracket that closes s[i] (which is `open_`), skipping quotes."""
    depth = 0
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'" and open_ != "{":
            k = s.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if c == '"':
            j = _dquote_end(s, j)
            continue
        if c == "#" and open_ == "(" and (j == 0 or s[j - 1] in " \t\n"):
            k = s.find("\n", j)
            j = n if k < 0 else k
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _expansion(s: str, i: int) -> tuple[Part | None, int]:
    """The `$...` or backtick expansion at s[i], and the offset after it."""
    n = len(s)
    if s[i] == "`":
        k = i + 1
        while k < n and s[k] != "`":
            k += 2 if s[k] == "\\" else 1
        return Part("cmd", s[i + 1:min(k, n)], i, min(k + 1, n), inner=i + 1), min(k + 1, n)
    if s.startswith("$((", i):
        end = _balanced(s, i + 1, "(", ")")
        return Part("arith", s[i + 3:max(i + 3, end - 2)], i, end, inner=i + 3), end
    if s.startswith("$(", i):
        end = _balanced(s, i + 1, "(", ")")
        return Part("cmd", s[i + 2:max(i + 2, end - 1)], i, end, inner=i + 2), end
    if s.startswith("${", i):
        end = _balanced(s, i + 1, "{", "}")
        inner = s[i + 2:max(i + 2, end - 1)]
        m = re.match(r"[#!]?([A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])", inner)
        if not m:
            return Part("var", inner, i, end, modifier="?"), end
        name = m.group(1)
        modifier = inner[m.end():]
        if inner[:1] in "#!" and m.group(0) != inner[:1]:
            modifier = inner[:1] + "name" + modifier  # ${#x} length, ${!x} indirection
        return Part("var", name, i, end, modifier=modifier), end
    m = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|\d|[@*#?$!-])").match(s, i)
    if m:
        return Part("var", m.group(1), i, m.end()), m.end()
    return None, i + 1


def expansions(s: str, a: int, b: int, quoted: bool = False) -> list[Part]:
    """The parts of s[a:b] read as double-quoted text (a here-document body or a "string")."""
    parts: list[Part] = []
    buf: list[str] = []
    buf_start = a
    i = a
    while i < b:
        c = s[i]
        if c == "\\" and i + 1 < b and s[i + 1] in '$`"\\\n':
            if s[i + 1] != "\n":
                buf.append(s[i + 1])
            i += 2
            continue
        if c in "$`":
            p, j = _expansion(s, i)
            if p is not None and j <= b + (1 if c == "`" else 0):
                if buf:
                    parts.append(Part("lit", "".join(buf), buf_start, i, quoted=quoted))
                    buf = []
                p.quoted = quoted
                parts.append(p)
                i = j
                buf_start = i
                continue
        buf.append(c)
        i += 1
    if buf:
        parts.append(Part("lit", "".join(buf), buf_start, b, quoted=quoted))
    return parts


# ------------------------------------------------------------------ the parser


class _Parser:
    def __init__(self, text: str, toks: list[Tok], problems: list[tuple[int, int, str]]):
        self.text = text
        self.toks = toks
        self.k = 0
        self.problems = problems

    # -- token helpers

    @property
    def tok(self) -> Tok:
        return self.toks[self.k]

    def word_is(self, *values: str) -> bool:
        t = self.tok
        return t.kind == "word" and not t.value.quoted and t.value.literal() in values

    def op_is(self, *values: str) -> bool:
        return self.tok.kind == "op" and self.tok.value in values

    def advance(self) -> Tok:
        t = self.toks[self.k]
        if t.kind != "eof":
            self.k += 1
        return t

    def newlines(self) -> None:
        while self.tok.kind == "newline":
            self.advance()

    def expect_word(self, *values: str) -> bool:
        self.newlines()
        if self.word_is(*values):
            self.advance()
            return True
        self.problems.append((self.tok.start, self.tok.end, f"expected {' or '.join(values)}"))
        return False

    # -- lists

    def body(self, stops: tuple[str, ...] = (), stop_ops: tuple[str, ...] = ()) -> list[Node]:
        out: list[Node] = []
        while True:
            while self.tok.kind == "newline" or self.op_is(";", "&"):
                self.advance()
            t = self.tok
            if t.kind == "eof" or (stops and self.word_is(*stops)) or (stop_ops and self.op_is(*stop_ops)):
                return out
            if self.word_is("then", "elif", "else", "fi", "do", "done", "esac", "}", "in") or self.op_is(")", ";;", ";&", ";;&", "||", "&&", "|", "|&"):
                self.problems.append((t.start, t.end, f"unexpected {t.value if t.kind == 'op' else t.value.literal()}"))
                self.advance()
                continue
            node = self.and_or()
            if node is None:
                self.problems.append((t.start, t.end, "unreadable command"))
                self.advance()
                continue
            if self.op_is("&"):
                amp = self.advance()
                node = Background(node.start, amp.end, node)
            out.append(node)

    def and_or(self) -> Node | None:
        first = self.pipeline()
        if first is None:
            return None
        rest: list[tuple[str, Node]] = []
        while self.op_is("&&", "||"):
            op = self.advance().value
            self.newlines()
            nxt = self.pipeline()
            if nxt is None:
                break
            rest.append((op, nxt))
        if not rest:
            return first
        return AndOr(first.start, rest[-1][1].end, first, rest)

    def pipeline(self) -> Node | None:
        negated = False
        start = self.tok.start
        while self.word_is("time"):
            self.advance()
        if self.word_is("!"):
            negated = True
            self.advance()
        first = self.command()
        if first is None:
            return None
        commands = [first]
        while self.op_is("|", "|&"):
            self.advance()
            self.newlines()
            nxt = self.command()
            if nxt is None:
                break
            commands.append(nxt)
        if len(commands) == 1 and not negated:
            return first
        return Pipeline(start, commands[-1].end, commands, negated)

    # -- commands

    def command(self) -> Node | None:
        t = self.tok
        if t.kind == "word" and not t.value.quoted:
            lit = t.value.literal()
            if lit == "if":
                return self.redirected(self.if_())
            if lit in ("while", "until"):
                return self.redirected(self.while_(lit))
            if lit in ("for", "select"):
                return self.redirected(self.for_(lit))
            if lit == "case":
                return self.redirected(self.case())
            if lit == "{":
                return self.redirected(self.group())
            if lit == "[[":
                return self.redirected(self.test())
            if lit == "function":
                return self.function_keyword()
            if lit is not None and _NAME.fullmatch(lit) and self.toks[self.k + 1].kind == "op" and self.toks[self.k + 1].value == "(" \
                    and self.toks[self.k + 2].kind == "op" and self.toks[self.k + 2].value == ")":
                return self.function_parens()
        if self.op_is("("):
            return self.redirected(self.subshell())
        if self.op_is("(("):
            return self.redirected(self.arith())
        if t.kind in ("word", "redirect"):
            return self.simple()
        return None

    def redirected(self, node: Node) -> Node:
        while self.tok.kind == "redirect":
            r = self.advance().value
            getattr(node, "redirects").append(r)
            node.end = r.end
        return node

    def simple(self) -> Command:
        start = self.tok.start
        cmd = Command(start, start)
        while self.tok.kind in ("word", "redirect"):
            t = self.advance()
            if t.kind == "redirect":
                cmd.redirects.append(t.value)
            else:
                w: Word = t.value
                m = _ASSIGN.match(w.raw(self.text)) if not cmd.words else None
                if m and w.parts and w.parts[0].kind == "lit" and w.parts[0].start == w.start and len(w.parts[0].text) >= m.end() and not w.parts[0].quoted:
                    cmd.assigns.append((m.group(1), _strip_prefix(w, m.end()), w.start, w.end))
                else:
                    cmd.words.append(w)
            cmd.end = t.end
        return cmd

    def if_(self) -> If:
        start = self.advance().start
        node = If(start, start)
        cond = self.body(("then",))
        self.expect_word("then")
        node.clauses.append((cond, self.body(("elif", "else", "fi"))))
        while self.word_is("elif"):
            self.advance()
            cond = self.body(("then",))
            self.expect_word("then")
            node.clauses.append((cond, self.body(("elif", "else", "fi"))))
        if self.word_is("else"):
            self.advance()
            node.else_body = self.body(("fi",))
        node.end = self.tok.end
        self.expect_word("fi")
        return node

    def while_(self, kind: str) -> Loop:
        start = self.advance().start
        cond = self.body(("do",))
        self.expect_word("do")
        body = self.body(("done",))
        end = self.tok.end
        self.expect_word("done")
        return Loop(start, end, kind=kind, cond=cond, body=body)

    def for_(self, kind: str) -> Loop:
        start = self.advance().start
        if self.op_is("(("):  # for ((i=0; i<n; i++))
            t = self.advance()
            end = _balanced(self.text, t.start, "(", ")")
            arith = self.text[t.end:max(t.end, end - 2)]
            while self.tok.kind != "eof" and self.tok.start < end:
                self.advance()
            while self.op_is(";") or self.tok.kind == "newline":
                self.advance()
            self.expect_word("do")
            body = self.body(("done",))
            stop = self.tok.end
            self.expect_word("done")
            return Loop(start, stop, kind="arith", body=body, arith=arith)
        var = None
        if self.tok.kind == "word":
            var = self.advance().value.literal()
        items: list[Word] | None = None
        self.newlines()
        if self.word_is("in"):
            self.advance()
            items = []
            while self.tok.kind == "word":
                items.append(self.advance().value)
        while self.op_is(";") or self.tok.kind == "newline":
            self.advance()
        self.expect_word("do")
        body = self.body(("done",))
        end = self.tok.end
        self.expect_word("done")
        return Loop(start, end, kind=kind, var=var, items=items, body=body)

    def case(self) -> Case:
        start = self.advance().start
        word = self.advance().value if self.tok.kind == "word" else None
        node = Case(start, start, word)
        self.expect_word("in")
        while True:
            self.newlines()
            if self.word_is("esac") or self.tok.kind == "eof":
                break
            a = self.tok.start
            if self.op_is("("):
                self.advance()
            patterns: list[Word] = []
            while self.tok.kind == "word":
                patterns.append(self.advance().value)
                if self.op_is("|"):
                    self.advance()
            if self.op_is(")"):
                self.advance()
            else:
                self.problems.append((self.tok.start, self.tok.end, "case pattern without )"))
                if not patterns:
                    self.advance()
                    continue
            body = self.body(("esac",), (";;", ";&", ";;&"))
            b = self.tok.end
            if self.op_is(";;", ";&", ";;&"):
                self.advance()
            node.items.append((patterns, body, a, b))
        node.end = self.tok.end
        self.expect_word("esac")
        return node

    def group(self) -> Group:
        start = self.advance().start
        body = self.body(("}",))
        end = self.tok.end
        self.expect_word("}")
        return Group(start, end, body)

    def subshell(self) -> Group:
        start = self.advance().start
        body = self.body((), (")",))
        end = self.tok.end
        if self.op_is(")"):
            self.advance()
        else:
            self.problems.append((self.tok.start, self.tok.end, "subshell without )"))
        return Group(start, end, body, subshell=True)

    def arith(self) -> Test:
        t = self.advance()
        end = _balanced(self.text, t.start, "(", ")")
        while self.tok.kind != "eof" and self.tok.start < end:
            self.advance()
        return Test(t.start, end, kind="arith", text=self.text[t.end:max(t.end, end - 2)])

    def test(self) -> Test:
        start = self.advance().start
        words: list[Word] = []
        while self.tok.kind != "eof" and not self.word_is("]]"):
            t = self.advance()
            if t.kind == "word":
                words.append(t.value)
            elif t.kind == "newline":
                continue
            else:
                words.append(Word([Part("lit", str(t.value) if t.kind == "op" else self.text[t.start:t.end], t.start, t.end)], t.start, t.end))
        end = self.tok.end
        self.expect_word("]]")
        return Test(start, end, kind="test", words=words)

    def function_parens(self) -> Function:
        name_tok = self.advance()
        self.advance()
        self.advance()
        return self._function_body(name_tok.start, name_tok.value.literal() or "")

    def function_keyword(self) -> Function | None:
        start = self.advance().start
        if self.tok.kind != "word":
            return None
        name = self.advance().value.literal() or ""
        if self.op_is("(") and self.toks[self.k + 1].kind == "op" and self.toks[self.k + 1].value == ")":
            self.advance()
            self.advance()
        return self._function_body(start, name)

    def _function_body(self, start: int, name: str) -> Function:
        self.newlines()
        inner = self.command()
        body = inner.body if isinstance(inner, Group) and not inner.subshell else ([inner] if inner is not None else [])
        node = Function(start, inner.end if inner is not None else self.tok.start, name, body)
        return self.redirected(node)  # type: ignore[return-value]


def _strip_prefix(w: Word, n: int) -> Word | None:
    """The value of an assignment word `NAME=value`: the word less its first `n` characters."""
    first = w.parts[0]
    rest = first.text[n:]
    parts = ([Part("lit", rest, first.start + n, first.end)] if rest else []) + w.parts[1:]
    if not parts:
        return Word([Part("lit", "", w.end, w.end)], w.end, w.end, False)
    return Word(parts, parts[0].start, w.end, w.quoted)


def parse(text: str, a: int = 0, b: int | None = None) -> Script:
    """The script in text[a:b] (all of it by default); offsets are into `text`, so a command
    substitution's source is parsed in place."""
    lexer = _Lexer(text, a, b)
    toks = lexer.run()
    parser = _Parser(text, toks, lexer.problems)
    body = parser.body()
    return Script(body, sorted(parser.problems), lexer.heredocs)


__all__ = ["AndOr", "Background", "Case", "Command", "Function", "Group", "Heredoc", "If", "Loop", "Node", "Part", "Pipeline", "Redirect", "Script", "Test", "Word", "expansions", "parse"]
