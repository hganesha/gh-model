"""Windows batch files (.bat, .cmd) and PowerShell scripts (.ps1).

Both are read line by line into the commands `commands.interpret` understands, so a batch
file that runs `dtexec /F load.dtsx` or a PowerShell script that sends a here-string to
`sqlplus` gives the same steps, lineage and transfers as a POSIX script.

- **Batch files**: logical lines (`^` continues one), `rem` and `::` comments, labels,
  `set NAME=value` (and `set "NAME=value"`), `if` and `for` heads with the command they
  run or the parenthesised block they open, `call`, `&`, `&&`, `||` and `|`, and `%NAME%`,
  `!NAME!`, `%1` and `%~dp0` (the script's directory). The behaviour unit is flat: `set` is an
  `assign`, an `if` or `for` is a platform node holding what it runs, `goto` and labels are
  platform nodes; the control flow `goto` makes is not lowered.
- **PowerShell**: comments, here-strings (`@" "@` expands, `@' '@` does not), assignments of
  literals and here-strings, `Invoke-Sqlcmd -Query`/`-InputFile`, a variable piped to a SQL
  client, `&` and `Start-Process` of a tool, and native commands; `$name`, `${name}`,
  `$env:NAME` and `$PSScriptRoot`. Control flow and functions are counted, not lowered, so no
  behaviour unit is emitted.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.shell.commands import SCRIPT_DIR, Arg, Cmd, Context, Resolver, arg_expr, interpret, var_expr
from repo_intel.platforms.shell.emit import Expanded, Out, decode, pack_input, program_ref
from repo_intel.platforms.shell.posix import Part
from repo_intel.platforms.shell.vocabulary import PLATFORM, SENSITIVE_NAME, SQL_CLIENTS

_SENSITIVE = re.compile(SENSITIVE_NAME)


def _key(path: str) -> dict[str, str]:
    return {"platform": PLATFORM, "name": path}


def _split(text: str, a: int, b: int, seps: tuple[str, ...], single_quotes: bool = True) -> list[tuple[int, int, str]]:
    """text[a:b] cut at separators outside quotes and parentheses: (start, end, the separator
    that follows). Batch files do not quote with `'`."""
    out = []
    start = i = a
    quote = None
    depth = 0
    while i < b:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            i += 1
            continue
        if c == '"' or (c == "'" and single_quotes):
            quote = c
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            for sep in seps:
                if text.startswith(sep, i):
                    out.append((start, i, sep))
                    i += len(sep)
                    start = i
                    break
            else:
                i += 1
            continue
        i += 1
    out.append((start, b, ""))
    return out


def _words(text: str, a: int, b: int, var: re.Pattern, script_dir: re.Pattern, single_literal: bool) -> list[Arg]:
    """Whitespace-separated words of text[a:b]; quotes removed, variables as `var` parts."""
    out: list[Arg] = []
    i = a
    while i < b:
        while i < b and text[i] in " \t\r":
            i += 1
        if i >= b:
            break
        start = i
        parts: list[Part] = []
        while i < b and text[i] not in " \t\r":
            c = text[i]
            if c in "\"'" and (c == '"' or single_literal):
                end = text.find(c, i + 1, b)
                end = b if end < 0 else end
                inner_a, inner_b = i + 1, end
                parts += _parts(text, inner_a, inner_b, var, script_dir, literal_only=(c == "'"), quoted=True)
                i = min(end + 1, b)
                continue
            j = i
            while j < b and text[j] not in " \t\r\"" and not (single_literal and text[j] == "'"):
                j += 1
            parts += _parts(text, i, j, var, script_dir)
            i = j
        out.append(Arg(parts or [Part("lit", "", start, i)], start, i))
    return out


def _parts(text: str, a: int, b: int, var: re.Pattern, script_dir: re.Pattern, literal_only: bool = False, quoted: bool = False) -> list[Part]:
    if literal_only:
        return [Part("lit", text[a:b], a, b, quoted=quoted)]
    out: list[Part] = []
    pos = a
    for m in var.finditer(text, a, b):
        if m.start() > pos:
            out.append(Part("lit", text[pos:m.start()], pos, m.start(), quoted=quoted))
        raw = m.group()
        if script_dir.fullmatch(raw):
            out.append(Part("var", SCRIPT_DIR, m.start(), m.end(), quoted=quoted))
        elif raw.startswith("$("):
            out.append(Part("cmd", raw[2:-1], m.start(), m.end(), quoted=quoted, inner=m.start() + 2))
        else:
            name = re.sub(r"^[%!$]~?|[%!]$|^\$\{|\}$|^\$env:", "", raw, flags=re.I)
            out.append(Part("var", name, m.start(), m.end(), quoted=quoted))
        pos = m.end()
    if pos < b or not out:
        out.append(Part("lit", text[pos:b], pos, b, quoted=quoted))
    return out


def _redirects(words: list[Arg]) -> tuple[list[Arg], Arg | None, Arg | None]:
    """Words less redirections, with the `<` file and the `>` file."""
    kept: list[Arg] = []
    stdin = stdout = None
    i = 0
    while i < len(words):
        w = words[i]
        lit = "".join(p.text for p in w.parts) if all(p.kind == "lit" for p in w.parts) else None
        if lit in ("<", ">", ">>", "1>", "2>", "2>>", "1>>") and i + 1 < len(words):
            if lit == "<":
                stdin = words[i + 1]
            elif not lit.startswith("2"):
                stdout = words[i + 1]
            i += 2
            continue
        if lit and re.match(r"^(?:[12]?>>?|<)\S", lit):
            op = re.match(r"^(?:[12]?>>?|<)", lit).group()
            rest = Arg([Part("lit", lit[len(op):], w.start + len(op), w.end)] + w.parts[1:], w.start + len(op), w.end)
            if op == "<":
                stdin = rest
            elif not op.startswith("2"):
                stdout = rest
            i += 1
            continue
        if lit and re.fullmatch(r"2>&1|>nul|2>nul|>&2", lit, re.I):
            i += 1
            continue
        kept.append(w)
        i += 1
    return kept, stdin, stdout


def _resolve(assignments: dict[str, list[tuple[str | None, int, bool]]]) -> Resolver:
    """Values assigned once, at the top level, to a literal (or to values already resolved)."""
    values: dict[str, tuple[str, int]] = {}
    for name, items in sorted(assignments.items(), key=lambda kv: kv[1][0][1]):
        if len(items) == 1 and items[0][0] is not None and items[0][2]:
            values[name] = (items[0][0], items[0][1])
    return Resolver(values)


# ------------------------------------------------------------------ batch files


_BAT_VAR = re.compile(r"%~[a-z]*dp0|%~?\d|%\*|%[A-Za-z_][\w.$#@-]*%|![A-Za-z_][\w.$#@-]*!", re.I)
_BAT_DIR = re.compile(r"%~[a-z]*dp0", re.I)
_BAT_SET = re.compile(r'(?i)^set\s+(?:/([ap])\s+)?(")?([A-Za-z_][\w.$#@-]*)=(.*?)(?(2)"|)\s*$')
_BAT_IF = re.compile(r'(?i)^if\s+(?:/i\s+)?(not\s+)?(errorlevel\s+\d+|exist\s+(?:"[^"]*"|\S+)|defined\s+\S+|(?:"[^"]*"|\S+)\s*(?:==|\s(?:equ|neq|lss|leq|gtr|geq)\s)\s*(?:"[^"]*"|\S+))\s*(.*)$')
_BAT_FOR = re.compile(r'(?i)^for\s+(?:/[a-z]\s+(?:"[^"]*"\s+)?)?%%?\w\s+in\s*\((.*?)\)\s*do\s+(.*)$')


class _Batch:
    def __init__(self, out: Out, me: dict[str, Any], path: str, text: str):
        self.out, self.me, self.path, self.text = out, me, path, text
        self.ctx = Context(out, me, path, Resolver(), windows=True)

    def lines(self) -> list[tuple[int, int]]:
        out = []
        i, n = 0, len(self.text)
        start = 0
        while i <= n:
            end = self.text.find("\n", i)
            end = n if end < 0 else end
            line = self.text[i:end].rstrip("\r")
            if line.endswith("^") and end < n:
                i = end + 1
                continue
            out.append((start, end))
            i = start = end + 1
            if end >= n:
                break
        return out

    def statement_text(self, a: int, b: int) -> tuple[int, int]:
        while a < b and self.text[a] in " \t@":
            a += 1
        while b > a and self.text[b - 1] in " \t\r":
            b -= 1
        return a, b

    def resolver(self, lines: list[tuple[int, int]]) -> Resolver:
        assigned: dict[str, list[tuple[str | None, int, bool]]] = {}
        depth = 0
        for a, b in lines:
            a, b = self.statement_text(a, b)
            s = self.text[a:b]
            m = _BAT_SET.match(s)
            if m:
                value: str | None = m.group(4)
                here = _BAT_DIR.match(value)
                if m.group(1):  # set /a computes, set /p prompts
                    value = None
                elif here:
                    rest = value[here.end():]
                    value = None if _BAT_VAR.search(rest) else SCRIPT_DIR + rest.replace("\\", "/")
                elif _BAT_VAR.search(value):
                    value = None
                assigned.setdefault(m.group(3).lower(), []).append((value, a, depth == 0))
            depth += s.count("(") - s.count(")") if not s.lower().startswith(("rem", "::", "echo")) else 0
            depth = max(depth, 0)
        resolver = _resolve(assigned)
        return _Caseless(resolver.values)

    def lower(self) -> list[dict[str, Any]]:
        lines = self.lines()
        self.ctx.resolver = self.resolver(lines)
        root: list[dict[str, Any]] = []
        stack: list[list[dict[str, Any]]] = [root]
        for a, b in lines:
            a, b = self.statement_text(a, b)
            s = self.text[a:b]
            low = s.lower()
            if not s or low.startswith(("rem ", "rem\t", "::")) or low == "rem":
                continue
            if low.startswith(("echo off", "echo on", "setlocal", "endlocal", "cls", "pause", "title ")):
                continue
            if s.startswith(":"):
                self.out.construct("sh:bat.goto", a, b, False)
                stack[-1].append({"op": "platform", "at": self.out.at(a, b), "construct": "sh:bat.goto", "detail": {"label": s[1:].split()[0] if s[1:].split() else ""}})
                continue
            while s.startswith(")"):
                if len(stack) > 1:
                    stack.pop()
                s, a = s[1:].lstrip(), a + 1 + (len(s[1:]) - len(s[1:].lstrip()))
                if s.lower().startswith("else"):
                    node = {"op": "platform", "at": self.out.at(a, b), "construct": "sh:bat.if", "detail": {"branch": "else"}, "body": []}
                    stack[-1].append(node)
                    rest = s[4:].strip()
                    if rest == "(":
                        stack.append(node["body"])
                        s = ""
                    else:
                        node["body"] += self.segment(b - len(rest), b) if rest else []
                        s = ""
            if s:
                self.head(a, b, stack)
        return root

    def head(self, a: int, b: int, stack: list[list[dict[str, Any]]]) -> None:
        s = self.text[a:b]
        m = _BAT_IF.match(s) or _BAT_FOR.match(s)
        if m is None:
            stack[-1] += self.segment(a, b)
            return
        is_if = m.re is _BAT_IF
        construct = "sh:bat.if" if is_if else "sh:bat.for"
        self.out.construct(construct, a, b, False)
        test = m.group(2).split()[0].lower() if is_if else "for"
        if is_if and "errorlevel" in test:
            self.out.construct("sh:bat.errorlevel", a, b, False)
        rest_at = a + m.start(3 if is_if else 2)
        node = {"op": "platform", "at": self.out.at(a, b), "construct": construct, "detail": {"test": test if test in ("errorlevel", "exist", "defined", "for") else "compare", **({"negated": True} if is_if and m.group(1) else {})}, "body": []}
        stack[-1].append(node)
        rest = self.text[rest_at:b].strip()
        if rest.startswith("(") and rest.endswith(")") and len(rest) > 1:
            node["body"] += self.segment(rest_at + 1 + (len(self.text[rest_at:b]) - len(self.text[rest_at:b].lstrip())), b - 1)
        elif rest.startswith("("):
            stack.append(node["body"])
        elif rest:
            node["body"] += self.segment(b - len(self.text[rest_at:b].lstrip()), b)

    def segment(self, a: int, b: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pieces = _split(self.text, a, b, ("&&", "||", "&", "|"), single_quotes=False)
        i = 0
        while i < len(pieces):
            pa, pb, sep = pieces[i]
            if sep == "|" and i + 1 < len(pieces):
                nxt = pieces[i + 1]
                left = self.text[pa:pb].strip()
                if left.lower().startswith("echo ") and self._client(nxt[0], nxt[1]):
                    ea = pa + self.text[pa:pb].lower().index("echo") + 5
                    x = self.ctx.expanded(_parts(self.text, ea, pb, _BAT_VAR, _BAT_DIR), ea, pb)
                    out += self.command(nxt[0], nxt[1], stdin=x)
                    i += 2
                    continue
            out += self.command(pa, pb)
            i += 1
        return out

    def _client(self, a: int, b: int) -> bool:
        words = self.text[a:b].split()
        return bool(words) and posixpath.basename(words[0].replace("\\", "/")).lower().removesuffix(".exe") in SQL_CLIENTS

    def command(self, a: int, b: int, stdin: Expanded | None = None) -> list[dict[str, Any]]:
        a, b = self.statement_text(a, b)
        if a >= b:
            return []
        s = self.text[a:b]
        m = _BAT_SET.match(s)
        if m:
            name = m.group(3)
            self.out.construct("sh:bat.set", a, b, True)
            sensitive = bool(_SENSITIVE.search(name))
            if sensitive:
                self.out.construct("sh:sensitive-value", a, b, True)
            va = a + m.start(4)
            value = arg_expr(Arg(_parts(self.text, va, a + m.end(4), _BAT_VAR, _BAT_DIR), va, a + m.end(4)), sensitive) if not m.group(1) else {"e": "platform", "construct": "sh:arithmetic" if m.group(1).lower() == "a" else "sh:bat.set"}
            return [{"op": "assign", "at": self.out.at(a, b), "target": var_expr(name.lower()), "value": value}]
        words = _words(self.text, a, b, _BAT_VAR, _BAT_DIR, single_literal=False)
        if not words:
            return []
        words, stdin_file, stdout_file = _redirects(words)
        if not words:
            return []
        head = "".join(p.text for p in words[0].parts) if all(p.kind == "lit" for p in words[0].parts) else None
        low = (head or "").lower()
        if low == "goto":
            self.out.construct("sh:bat.goto", a, b, False)
            return [{"op": "platform", "at": self.out.at(a, b), "construct": "sh:bat.goto", "detail": {"target": "".join(p.text for p in words[1].parts) if len(words) > 1 else ""}}]
        if low == "exit":
            self.out.construct("sh:exit", a, b, True)
            return [{"op": "exit", "at": self.out.at(a, b)}]
        if low == "echo":
            self.out.construct("sh:echo", a, b, True)
            return [{"op": "output", "at": self.out.at(a, b), "channel": "file" if stdout_file is not None else "log"}]
        if low == "call":
            words = words[1:]
            if not words:
                return []
            head = "".join(p.text for p in words[0].parts) if all(p.kind == "lit" for p in words[0].parts) else None
            if head is not None and head.startswith(":"):
                self.out.construct("sh:bat.goto", a, b, False)
                return [{"op": "platform", "at": self.out.at(a, b), "construct": "sh:bat.goto", "detail": {"call": head[1:]}}]
            if head is None or head.lower().endswith((".bat", ".cmd")) or any(p.kind == "var" for p in words[0].parts):
                self.out.construct("sh:bat.call", a, b, True)
                return interpret(self.ctx, Cmd("cmd", None, [Arg([Part("lit", "/c", a, a)], a, a), words[0]], a, b))
        if head is None:
            value = self.ctx.value(words[0])
            if value is not None and value.lower().endswith((".bat", ".cmd", ".ps1", ".sh")):
                return interpret(self.ctx, Cmd("cmd", None, [Arg([Part("lit", "/c", a, a)], a, a), words[0]], a, b))
            self.out.construct("sh:dynamic-command", a, b, False)
            self.out.note("a command whose name is built at run time is not read")
            return [{"op": "call", "at": self.out.at(a, b), "call": {"e": "call", "callee": {"e": "platform", "construct": "sh:dynamic-command"}}}]
        name = posixpath.basename(head.replace("\\", "/")).lower()
        cmd = Cmd(name, words[0], words[1:], a, b, stdin, (a, b) if stdin is not None else None, stdin_file, stdout_file)
        return interpret(self.ctx, cmd)


class _Caseless(Resolver):
    """Batch variables are not case-sensitive."""

    def value(self, name: str, at: int, modifier: str = "") -> str | None:
        return super().value(name.lower(), at, modifier)


class BatchSource:
    id = "shell.batch"
    version = "1.1.0"
    SUFFIXES = (".bat", ".cmd")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES) and not pack_input(path)

    def extract(self, path: str, data: bytes, blob: str):
        text = decode(data)
        out = Out(self.id, path, data, blob, text)
        me = out.term(0, len(text), "sh:BatchFile", _key(path), posixpath.basename(path), {"shell": "cmd", "language": "batch"})
        body = _Batch(out, me, path, text).lower()
        out.behavior(0, len(text), me, "batch", body, "open_code")
        return out.done()


# ------------------------------------------------------------------ PowerShell


_PS_VAR = re.compile(r"\$\{[^}]+\}|\$env:[A-Za-z_]\w*|\$PSScriptRoot\b|\$\((?:[^()]|\([^()]*\))*\)|\$[A-Za-z_]\w*(?::[A-Za-z_]\w*)?", re.I)
_PS_DIR = re.compile(r"\$PSScriptRoot|\$\(\s*Split-Path\s+(?:-Parent\s+)?\$MyInvocation\.MyCommand\.(?:Path|Definition)(?:\s+-Parent)?\s*\)", re.I)
_PS_ASSIGN = re.compile(r"(?i)^\$([A-Za-z_]\w*)\s*=\s*(.*)$", re.S)
_PS_CONTROL = re.compile(r"(?i)^(if|elseif|else|foreach|for|while|do|until|switch|try|catch|finally|trap|param|begin|process|end)\b")
_PS_FUNCTION = re.compile(r"(?i)^(function|filter|workflow)\s+[\w-]+")
_PS_INVOKE_SQL = re.compile(r"(?i)^invoke-sqlcmd\b")


class _PowerShell:
    def __init__(self, out: Out, me: dict[str, Any], path: str, text: str):
        self.out, self.me, self.path, self.text = out, me, path, text
        self.ctx = Context(out, me, path, Resolver(), windows=True)
        self.blanked = self._blank_comments(text)
        self.heres: dict[str, tuple[Expanded, int, int]] = {}  # variable -> its here-string

    @staticmethod
    def _blank_comments(text: str) -> str:
        def blank(m: re.Match) -> str:
            return re.sub(r"[^\n]", " ", m.group())
        text = re.sub(r"<#.*?#>", blank, text, flags=re.S)
        return re.sub(r"(?m)(?<![`\w$])#[^\n]*", lambda m: blank(m) if not _in_string(text, m.start()) else m.group(), text)

    def statements(self) -> list[tuple[int, int, tuple[int, int, bool] | None]]:
        """Logical statements: (start, end, here-string body span and whether it expands)."""
        t = self.blanked
        out = []
        i, n = 0, len(t)
        while i < n:
            end = t.find("\n", i)
            end = n if end < 0 else end
            line = t[i:end]
            m = re.search(r"@([\"'])\s*$", line)
            if m:  # a here-string runs to a line starting with "@ or '@
                body_start = end + 1
                close = re.compile(rf"(?m)^{m.group(1)}@").search(t, body_start)
                stop = close.start() if close else n
                after = t.find("\n", stop)
                after = n if after < 0 else after
                self.out.construct("sh:ps.here-string", i, after, True)
                out.append((i, after, (body_start, max(body_start, stop - 1), m.group(1) == '"')))
                i = after + 1
                continue
            for a, b, _ in _split(t, i, end, (";",)):
                out.append((a, b, None))
            i = end + 1
        return out

    def run(self) -> None:
        stmts = self.statements()
        assigned: dict[str, list[tuple[str | None, int, bool]]] = {}
        depth = 0
        for a, b, here in stmts:
            s = self.blanked[a:b].strip()
            m = _PS_ASSIGN.match(s)
            if m and here is None:
                raw = m.group(2).strip()
                value = raw[1:-1] if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\"" and not (raw[0] == '"' and _PS_VAR.search(raw)) else None
                if value is None and _PS_DIR.fullmatch(raw):
                    value = SCRIPT_DIR
                elif value is None and raw[:1] == '"' and raw[-1:] == '"' and _PS_DIR.match(raw[1:]) and not _PS_VAR.search(raw[1 + _PS_DIR.match(raw[1:]).end():-1]):
                    value = SCRIPT_DIR + raw[1 + _PS_DIR.match(raw[1:]).end():-1].replace("\\", "/")
                assigned.setdefault(m.group(1).lower(), []).append((value, a, depth == 0))
            elif m:
                assigned.setdefault(m.group(1).lower(), []).append((None, a, depth == 0))
            depth = max(0, depth + s.count("{") - s.count("}"))
        self.ctx.resolver = _Caseless(_resolve(assigned).values)
        for a, b, here in stmts:
            self.statement(a, b, here)

    def statement(self, a: int, b: int, here: tuple[int, int, bool] | None) -> None:
        t = self.blanked
        while a < b and t[a] in " \t\r{}":
            a += 1
        while b > a and t[b - 1] in " \t\r{}":
            b -= 1
        s = t[a:b]
        if not s:
            return
        if _PS_FUNCTION.match(s):
            self.out.construct("sh:ps.function", a, b, False)
            return
        c = _PS_CONTROL.match(s)
        if c:
            self.out.construct("sh:ps.control", a, b, False)
            inner = re.search(r"\{(.*)$", s)  # `if (...) { cmd }` on one line: read the command
            if inner and inner.group(1).strip():
                self.statement(a + inner.start(1), b, None)
            return
        m = _PS_ASSIGN.match(s)
        if m and here is not None:
            ha, hb, expands = here
            self.heres[m.group(1).lower()] = (self._expanded(ha, hb, expands), ha, hb)
            return
        if m:
            rhs_a = a + m.start(2)
            self.pipeline(rhs_a, b)
            return
        self.pipeline(a, b, here)

    def _expanded(self, a: int, b: int, expands: bool) -> Expanded:
        if not expands:
            x = Expanded()
            x.add(self.text[a:b], a, b)
            return x
        return self.ctx.expanded(_parts(self.text, a, b, _PS_VAR, _PS_DIR), a, b)

    def pipeline(self, a: int, b: int, here: tuple[int, int, bool] | None = None) -> None:
        pieces = _split(self.blanked, a, b, ("|",))
        stdin: Expanded | None = None
        if here is not None and len(pieces) >= 1 and self.blanked[pieces[0][0]:pieces[0][1]].strip().startswith("@"):
            stdin = self._expanded(here[0], here[1], here[2])
            pieces = pieces[1:]
        for i, (pa, pb, _) in enumerate(pieces):
            seg = self.blanked[pa:pb].strip()
            v = re.fullmatch(r"\$([A-Za-z_]\w*)", seg)
            if v and v.group(1).lower() in self.heres and i + 1 < len(pieces):
                stdin = self.heres[v.group(1).lower()][0]
                continue
            self.command(pa, pb, stdin)
            stdin = None

    def command(self, a: int, b: int, stdin: Expanded | None) -> None:
        while a < b and self.blanked[a] in " \t":
            a += 1
        s = self.blanked[a:b].rstrip()
        b = a + len(s)
        if not s:
            return
        words = _words(self.text, a, b, _PS_VAR, _PS_DIR, single_literal=True)
        if not words:
            return
        words, stdin_file, stdout_file = _redirects(words)
        if not words:
            return
        head = "".join(p.text for p in words[0].parts) if all(p.kind == "lit" for p in words[0].parts) else None
        low = (head or "").lower()
        if low == "&" and len(words) > 1:
            self.out.construct("sh:ps.invoke", a, b, True)
            words = words[1:]
            head = self.ctx.value(words[0])
            low = (head or "").lower()
        elif low == "start-process":
            self.out.construct("sh:ps.invoke", a, b, True)
            opts = {(("".join(p.text for p in w.parts)).lower() if all(p.kind == "lit" for p in w.parts) else ""): i for i, w in enumerate(words)}
            fp = opts.get("-filepath")
            target = words[fp + 1] if fp is not None and fp + 1 < len(words) else (words[1] if len(words) > 1 else None)
            al = opts.get("-argumentlist")
            args = []
            if al is not None and al + 1 < len(words):
                raw = words[al + 1]
                args = _words(self.text, raw.parts[0].start, raw.parts[-1].end, _PS_VAR, _PS_DIR, single_literal=True) if raw.parts else []
            head = self.ctx.value(target) if target is not None else None
            low = (head or "").lower()
            words = ([target] if target is not None else []) + args
        if _PS_INVOKE_SQL.match(low):
            self._invoke_sqlcmd(a, b, words)
            return
        if not head:
            return  # cmdlets with computed names, expressions
        name = posixpath.basename(head.replace("\\", "/")).lower()
        if "-" in name and not name.endswith(".exe") and name not in ("spark-submit", "spark2-submit", "impala-shell", "send-mailmessage"):
            return  # a cmdlet (Get-Date, Write-Host, Copy-Item, ...): not a tool run
        cmd = Cmd(name, words[0], words[1:], a, b, stdin, (a, b) if stdin is not None else None, stdin_file, stdout_file)
        interpret(self.ctx, cmd)

    def _invoke_sqlcmd(self, a: int, b: int, words: list[Arg]) -> None:
        self.out.construct("sh:ps.invoke-sqlcmd", a, b, True)
        names = [("".join(p.text for p in w.parts)).lower() if all(p.kind == "lit" for p in w.parts) else "" for w in words]
        sql: Expanded | None = None
        if "-query" in names and names.index("-query") + 1 < len(words):
            q = words[names.index("-query") + 1]
            v = re.fullmatch(r"\$([A-Za-z_]\w*)", self.text[q.start:q.end])
            if v and v.group(1).lower() in self.heres:
                sql = self.heres[v.group(1).lower()][0]
            elif q.parts:
                sql = self.ctx.expanded(q.parts, q.parts[0].start, q.parts[-1].end)
        if "-inputfile" in names and names.index("-inputfile") + 1 < len(words):
            f = words[names.index("-inputfile") + 1]
            interpret(self.ctx, Cmd("sqlcmd", None, [Arg([Part("lit", "-i", a, a)], a, a), f], a, b))
        if sql is not None:
            interpret(self.ctx, Cmd("sqlcmd", None, [], a, b, sql, (a, b)))


def _in_string(text: str, i: int) -> bool:
    line_start = text.rfind("\n", 0, i) + 1
    segment = text[line_start:i]
    return segment.count('"') % 2 == 1 or segment.count("'") % 2 == 1


class PowerShellSource:
    id = "shell.powershell"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".ps1", ".psm1")) and not pack_input(path)

    def extract(self, path: str, data: bytes, blob: str):
        text = decode(data)
        out = Out(self.id, path, data, blob, text)
        me = out.term(0, len(text), "sh:PowerShellScript", _key(path), posixpath.basename(path), {"shell": "powershell", "language": "powershell"})
        _PowerShell(out, me, path, text).run()
        return out.done()


__all__ = ["BatchSource", "PowerShellSource", "program_ref"]
