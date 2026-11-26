"""A POSIX shell script lowered into one behaviour unit, with what its commands say emitted.

The script's top level is one unit (`open_code`) owned by its Program; a function is a
`declare` whose value is a `lambda`, so its name is kept. Statements map as follows:

- `NAME=value`, `export`, `readonly` -> `assign`; `local`, `typeset`, `declare` -> `declare`;
  a value assigned to a variable named like a password, key, user, host or connection is
  never copied (`sh:sensitive-value`).
- `if`/`elif`/`else` -> `if`; `[ ]`, `[[ ]]` and `test` become comparisons and file tests;
  any other condition command runs first and the test is its exit status (`$? == 0`).
- `a && b` and `a || b` -> `a`, then `if $? == 0` (or `!= 0`) `b`.
- `for`, `select` -> `loop each`; `while`, `until` -> `loop`; `case` -> `switch`.
- `exit`, `return`, `break`, `continue`, `.`/`source` (`include`), `echo`/`printf`
  (`output`: the log, or the file it is redirected to).
- pipelines, subshells, background jobs, `trap` and `(( ))` are `platform` nodes; `eval` is
  counted and kept as a platform node, its command never read.
- every other command goes through `commands.interpret`: SQL clients, tools, transfers.

`resolver` (run first) finds the values a script gives with certainty: a variable assigned
exactly once, at the top level, to a literal or to other such variables, used after that
assignment; and `$(dirname "$0")` and its spellings, which name the script's directory.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.shell import posix
from repo_intel.platforms.shell.commands import SCRIPT_DIR, Arg, Cmd, Context, Resolver, arg_expr, interpret, literal, var_expr
from repo_intel.platforms.shell.emit import Expanded, P, program_ref
from repo_intel.platforms.shell.posix import AndOr, Background, Case, Command, Function, Group, If, Loop, Pipeline, Test, Word
from repo_intel.platforms.shell.vocabulary import PLATFORM, SENSITIVE_NAME, SQL_CLIENTS, TRANSFERS

_SENSITIVE = re.compile(SENSITIVE_NAME)
_DECLARERS = {"export", "readonly", "local", "typeset", "declare", "integer"}
_DIRNAME = re.compile(r"""^\s*(?:dirname\s+["']?\$\{?0\}?["']?|cd\s+["']?\$\((?:dirname\s+["']?\$\{?0\}?["']?)\)["']?\s*(?:&&|;)\s*pwd(?:\s+-P)?)\s*$""")
_FILE_TESTS = {"-f": "file_exists", "-d": "dir_exists", "-e": "exists", "-a": "exists", "-s": "file_not_empty", "-r": "readable", "-w": "writable", "-x": "executable", "-L": "symlink", "-h": "symlink"}
_COMPARE = {"=": "==", "==": "==", "!=": "!=", "-eq": "==", "-ne": "!=", "-lt": "<", "-le": "<=", "-gt": ">", "-ge": ">=", "<": "<", ">": ">", "=~": "like"}
_TEST_BUILTINS = {"[", "test", "true", "false", ":"}


# ------------------------------------------------------------------ certain values


def _walk(nodes: list[posix.Node], depth: int, visit) -> None:
    for n in nodes:
        visit(n, depth)
        if isinstance(n, If):
            for cond, body in n.clauses:
                _walk(cond, depth + 1, visit)
                _walk(body, depth + 1, visit)
            _walk(n.else_body or [], depth + 1, visit)
        elif isinstance(n, Loop):
            _walk(n.cond, depth + 1, visit)
            _walk(n.body, depth + 1, visit)
        elif isinstance(n, Case):
            for _, body, _, _ in n.items:
                _walk(body, depth + 1, visit)
        elif isinstance(n, (Function, Group)):
            _walk(n.body, depth + 1, visit)
        elif isinstance(n, Pipeline):
            _walk(n.commands, depth + 1, visit)
        elif isinstance(n, AndOr):
            _walk([n.first] + [x for _, x in n.rest], depth + 1, visit)
        elif isinstance(n, Background) and n.body is not None:
            _walk([n.body], depth + 1, visit)


def script_dir_cmd(text: str) -> bool:
    return bool(_DIRNAME.match(text))


def resolver(script: posix.Script, text: str) -> Resolver:
    counts: dict[str, int] = {}
    top: dict[str, tuple[Word | None, int]] = {}

    def count(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    def visit(n: posix.Node, depth: int) -> None:
        if isinstance(n, Command):
            words = [w.literal() for w in n.words]
            if not n.words:
                for name, value, a, _ in n.assigns:
                    count(name)
                    if depth == 0:
                        top[name] = (value, a)
            if words and words[0] in _DECLARERS:
                for w in n.words[1:]:
                    m = re.match(r"([A-Za-z_]\w*)(\+?=)?", w.raw(text))
                    if m and m.group(2):
                        count(m.group(1))
                        if depth == 0 and words[0] in ("export", "readonly") and m.group(2) == "=":
                            top[m.group(1)] = (_after(w, m.end()), w.start)
                    elif m and words[0] in ("local", "typeset", "declare"):
                        count(m.group(1))
            if words and words[0] in ("read", "getopts", "unset", "let", "mapfile", "readarray"):
                for w in n.words[1:]:
                    lit = w.literal() or ""
                    m = re.match(r"([A-Za-z_]\w*)", lit)
                    if m and not lit.startswith("-"):
                        count(m.group(1))
            for w in n.words:
                for p in w.parts:
                    if p.kind == "var" and p.modifier.startswith((":=", "=")):
                        count(p.text)
        elif isinstance(n, Loop) and n.var:
            count(n.var)
        elif isinstance(n, Test) and n.kind == "arith":
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:[-+*/%]?=(?!=)|\+\+|--)", n.text):
                count(m.group(1))

    _walk(script.body, 0, visit)
    values: dict[str, tuple[str, int]] = {}
    for name, (word, at) in sorted(top.items(), key=lambda kv: kv[1][1]):
        if counts.get(name) != 1 or word is None:
            continue
        r = Resolver(values)
        out = []
        for p in word.parts:
            if p.kind == "lit":
                out.append(p.text)
            elif p.kind == "var":
                v = r.value(p.text, at, p.modifier)
                if v is None:
                    break
                out.append(v)
            elif p.kind == "cmd" and script_dir_cmd(p.text):
                out.append(SCRIPT_DIR)
            else:
                break
        else:
            values[name] = ("".join(out), at)
    return Resolver(values)


def _after(w: Word, n: int) -> Word | None:
    first = w.parts[0] if w.parts else None
    if first is None or first.kind != "lit" or len(first.text) < n:
        return None
    rest = first.text[n:]
    parts = ([posix.Part("lit", rest, first.start + n, first.end)] if rest else []) + w.parts[1:]
    return Word(parts or [posix.Part("lit", "", w.end, w.end)], w.start + n, w.end)


# ------------------------------------------------------------------ lowering


class Lowering:
    def __init__(self, ctx: Context, text: str):
        self.ctx = ctx
        self.out = ctx.out
        self.text = text
        ctx.expand_cmd = lambda p: SCRIPT_DIR if script_dir_cmd(p.text) else None

    def at(self, n: Any) -> list[int]:
        return self.out.at(n.start, n.end)

    def count(self, construct: str, n: Any, modelled: bool = True) -> None:
        self.out.construct(construct, n.start, n.end, modelled)

    # -- words and expressions

    def word(self, w: Word | None, sensitive: bool = False) -> dict[str, Any]:
        if w is None:
            return {"e": "lit", "value": "", "type": "string"}
        return arg_expr(Arg(w.parts, w.start, w.end), sensitive)

    def args(self, words: list[Word]) -> list[Arg]:
        return [Arg(w.parts, w.start, w.end) for w in words]

    def cmd(self, c: Command, stdin: Expanded | None = None, stdin_at: tuple[int, int] | None = None) -> Cmd:
        name = c.words[0].literal() or ""
        out = Cmd(name, Arg(c.words[0].parts, c.words[0].start, c.words[0].end), self.args(c.words[1:]), c.start, c.end, stdin, stdin_at)
        for r in c.redirects:
            if r.heredoc is not None:
                h = r.heredoc
                self.count("sh:heredoc", h)
                if out.stdin is None:
                    out.stdin = self._body(h)
                    out.stdin_at = (h.start, h.end)
                    out.end = max(out.end, h.end)
            elif r.op == "<<<" and r.target is not None:
                self.count("sh:here-string", r)
                if out.stdin is None:
                    out.stdin = self._herestring(r.target)
                    out.stdin_at = (r.target.start, r.target.end)
            elif r.op in ("<", "<>") and r.target is not None and r.fd in ("", "0"):
                out.stdin_file = Arg(r.target.parts, r.target.start, r.target.end)
            elif r.op in (">", ">>", ">|", "&>", "&>>") and r.target is not None and r.fd in ("", "1"):
                out.stdout_file = Arg(r.target.parts, r.target.start, r.target.end)
        return out

    def _body(self, h: posix.Heredoc) -> Expanded:
        if h.quoted:
            x = Expanded()
            x.add(self.text[h.start:h.end], h.start, h.end)
            return x
        return self.ctx.expanded(h.parts, h.start, h.end)

    def _herestring(self, w: Word) -> Expanded:
        return self.ctx.expanded(w.parts, w.start, w.end)

    def test_expr(self, words: list[Word], n: Any) -> dict[str, Any]:
        lits = [w.literal() if not w.quoted else None for w in words]
        if lits and lits[0] in ("[", "test"):
            words, lits = words[1:], lits[1:]
            if lits and lits[-1] == "]":
                words, lits = words[:-1], lits[:-1]
        for joiner, op in (("||", "or"), ("-o", "or"), ("&&", "and"), ("-a", "and")):
            if joiner in lits and (joiner != "-a" or len(words) > 3):
                i = lits.index(joiner)
                return {"e": "binop", "op": op, "left": self.test_expr(words[:i], n), "right": self.test_expr(words[i + 1:], n)}
        if lits and lits[0] == "!":
            return {"e": "unop", "op": "not", "operand": self.test_expr(words[1:], n)}
        if len(words) == 3 and lits[1] in _COMPARE:
            return {"e": "binop", "op": _COMPARE[lits[1]], "left": self.word(words[0]), "right": self.word(words[2])}
        if len(words) == 2 and lits[0] in ("-z", "-n"):
            missing = {"e": "unop", "op": "missing", "operand": self.word(words[1])}
            return missing if lits[0] == "-z" else {"e": "unop", "op": "not", "operand": missing}
        if len(words) == 2 and lits[0] in _FILE_TESTS:
            return {"e": "call", "callee": {"e": "name", "name": _FILE_TESTS[lits[0]], "kind": "builtin"}, "args": [self.word(words[1])]}
        if len(words) == 1:
            return {"e": "unop", "op": "not", "operand": {"e": "unop", "op": "missing", "operand": self.word(words[0])}}
        return {"e": "call", "callee": {"e": "name", "name": "test", "kind": "builtin"}, "args": [self.word(w) for w in words]}

    def condition(self, cond: list[posix.Node]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Statements that run before the test, and the test."""
        if not cond:
            return [], {"e": "lit", "value": True, "type": "boolean"}
        *before, last = cond
        pre = self.block(before)
        negated = False
        if isinstance(last, Pipeline) and last.negated and len(last.commands) == 1:
            negated, last = True, last.commands[0]
        expr: dict[str, Any] | None = None
        if isinstance(last, Test):
            self.count("sh:test" if last.kind == "test" else "sh:arithmetic", last)
            expr = self.test_expr(last.words, last) if last.kind == "test" else {"e": "platform", "construct": "sh:arithmetic"}
        elif isinstance(last, Command) and last.words and last.words[0].literal() in _TEST_BUILTINS and not last.redirects:
            name = last.words[0].literal()
            self.count("sh:test", last)
            expr = {"e": "lit", "value": name != "false", "type": "boolean"} if name in ("true", "false", ":") else self.test_expr(last.words, last)
        if expr is None:
            pre += self.block([last])
            expr = {"e": "binop", "op": "==", "left": var_expr("?"), "right": {"e": "lit", "value": 0, "type": "integer"}}
        return pre, ({"e": "unop", "op": "not", "operand": expr} if negated else expr)

    # -- statements

    def block(self, nodes: list[posix.Node]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for n in nodes:
            out += self.stmt(n)
        return out

    def stmt(self, n: posix.Node) -> list[dict[str, Any]]:
        if isinstance(n, Command):
            return self.command(n)
        if isinstance(n, If):
            self.count("sh:if", n)
            return self._if(n.clauses, n.else_body, n)
        if isinstance(n, Loop):
            return self.loop(n)
        if isinstance(n, Case):
            self.count("sh:case", n)
            cases = []
            default = None
            for patterns, body, a, b in n.items:
                if any(p.literal() == "*" for p in patterns):
                    default = self.block(body)
                    continue
                cases.append({"match": [self.word(p) for p in patterns], "body": self.block(body)})
            node = {"op": "switch", "at": self.at(n), "on": self.word(n.word), "cases": cases}
            if default is not None:
                node["default"] = default
            return [node]
        if isinstance(n, Function):
            self.count("sh:function", n)
            return [{"op": "declare", "at": self.at(n), "name": n.name, "scope": "function", "value": {"e": "lambda", "body": self.block(n.body)}}]
        if isinstance(n, Group):
            if n.subshell:
                self.count("sh:subshell", n)
                return [{"op": "platform", "at": self.at(n), "construct": "sh:subshell", "body": self.block(n.body)}]
            return self.block(n.body)
        if isinstance(n, Pipeline):
            return self.pipeline(n)
        if isinstance(n, AndOr):
            self.count("sh:and-or", n)
            out = self.stmt(n.first) if n.first is not None else []
            status = var_expr("?")
            for op, node in n.rest:
                test = {"e": "binop", "op": "==" if op == "&&" else "!=", "left": status, "right": {"e": "lit", "value": 0, "type": "integer"}}
                out.append({"op": "if", "at": self.at(node), "cond": test, "then": self.stmt(node)})
            return out
        if isinstance(n, Background):
            self.count("sh:background", n)
            return [{"op": "platform", "at": self.at(n), "construct": "sh:background", "body": self.stmt(n.body) if n.body is not None else []}]
        if isinstance(n, Test):
            if n.kind == "arith":
                self.count("sh:arithmetic", n)
                return [{"op": "platform", "at": self.at(n), "construct": "sh:arithmetic"}]
            self.count("sh:test", n)
            return [{"op": "call", "at": self.at(n), "call": self.test_expr(n.words, n)}]
        return []

    def _if(self, clauses, else_body, n) -> list[dict[str, Any]]:
        (cond, body), rest = clauses[0], clauses[1:]
        pre, test = self.condition(cond)
        node: dict[str, Any] = {"op": "if", "at": self.at(n), "cond": test, "then": self.block(body)}
        if rest:
            node["else"] = self._if(rest, else_body, n)
        elif else_body is not None:
            node["else"] = self.block(else_body)
        return pre + [node]

    def loop(self, n: Loop) -> list[dict[str, Any]]:
        if n.kind in ("for", "select"):
            self.count("sh:for", n)
            over = {"e": "array", "items": [self.word(w) for w in n.items]} if n.items is not None else var_expr("@")
            return [{"op": "loop", "at": self.at(n), "kind": "each", "var": var_expr(n.var or "_"), "over": over, "body": self.block(n.body)}]
        if n.kind == "arith":
            self.count("sh:for", n)
            self.count("sh:arithmetic", n)
            return [{"op": "loop", "at": self.at(n), "kind": "for", "cond": {"e": "platform", "construct": "sh:arithmetic"}, "body": self.block(n.body)}]
        self.count(f"sh:{n.kind}", n)
        pre, test = self.condition(n.cond)  # a condition command runs before each test
        return [{"op": "loop", "at": self.at(n), "kind": n.kind, "cond": test, "body": pre + self.block(n.body)}]

    def pipeline(self, n: Pipeline) -> list[dict[str, Any]]:
        cmds = n.commands
        # echo "SQL" | sqlplus, cat <<EOF | sftp ...: the first command's text is the second's input
        if len(cmds) == 2 and all(isinstance(c, Command) and c.words for c in cmds):
            first, second = cmds
            feeder = first.words[0].literal()
            consumer = second.words[0].literal() or ""
            target = consumer.rsplit("/", 1)[-1]
            if feeder in ("echo", "printf", "cat") and (target in SQL_CLIENTS or target in TRANSFERS):
                stdin = self._piped(first)
                if stdin is not None:
                    self.count("sh:pipeline", n)
                    return [{"op": "platform", "at": self.at(n), "construct": "sh:pipeline", "body": self.command(second, stdin, (first.start, first.end))}]
        self.count("sh:pipeline", n)
        body: list[dict[str, Any]] = []
        for c in cmds:
            body += self.stmt(c)
        node: dict[str, Any] = {"op": "platform", "at": self.at(n), "construct": "sh:pipeline", "body": body}
        if n.negated:
            node["detail"] = {"negated": True}
        return [node]

    def _piped(self, c: Command) -> Expanded | None:
        name = c.words[0].literal()
        if name == "cat":
            h = next((r.heredoc for r in c.redirects if r.heredoc is not None), None)
            if h is None or len(c.words) > 1:
                return None
            self.count("sh:heredoc", h)
            return self._body(h)
        words = [w for w in c.words[1:] if not (name == "echo" and w.literal() in ("-e", "-n", "-ne", "-en"))]
        if not words:
            return None
        parts: list[posix.Part] = []
        for i, w in enumerate(words):
            if i:
                parts.append(posix.Part("lit", " ", words[i - 1].end, w.start))
            parts += w.parts
        x = self.ctx.expanded(parts, words[0].start, words[-1].end)
        if name == "printf" or (name == "echo" and any(w.literal() in ("-e", "-ne", "-en") for w in c.words[1:])):
            x.text = x.text.replace("\\n", "\n  ")  # keep the length: two characters become two
        return x

    def command(self, c: Command, stdin: Expanded | None = None, stdin_at: tuple[int, int] | None = None) -> list[dict[str, Any]]:
        if not c.words:
            return [self.assign(name, value, a, b) for name, value, a, b in c.assigns]
        head = c.words[0]
        name = head.literal()
        if name is None:
            self.count("sh:dynamic-command", c, False)
            self.out.note("a command whose name is built at run time is not read")
            return [{"op": "call", "at": self.at(c), "call": {"e": "call", "callee": {"e": "platform", "construct": "sh:dynamic-command", "value": self.word(head)}}}]
        rest = c.words[1:]
        if name in _DECLARERS:
            self.count("sh:local" if name in ("local", "typeset", "declare", "integer") else "sh:export", c)
            out = []
            for w in rest:
                m = re.match(r"([A-Za-z_]\w*)(\+?=)?", w.raw(self.text))
                if not m:
                    continue
                if m.group(2):
                    out.append(self.assign(m.group(1), _after(w, m.end()), w.start, w.end, local=name not in ("export", "readonly"), export=name == "export"))
                elif name in ("local", "typeset", "declare", "integer"):
                    out.append({"op": "declare", "at": self.at(w), "name": m.group(1), "scope": "local"})
                else:
                    out.append({"op": "platform", "at": self.at(w), "construct": "sh:export", "detail": {"name": m.group(1)}})
            return out
        if name in (".", "source"):
            self.count("sh:source", c)
            target = rest[0] if rest else None
            if target is not None:
                value = self.ctx.value(Arg(target.parts, target.start, target.end))
                if value is None:
                    self.out.note("a script sourced from a path built at run time is not resolved")
                else:
                    for p in self.ctx.file_candidates(value):
                        self.out.rel(c.start, c.end, self.ctx.me, P.DEPENDS_ON, program_ref(PLATFORM, p, declare=False), {"basis": "source"})
            return [{"op": "include", "at": self.at(c), "target": self.word(target)}]
        if name == "exit":
            self.count("sh:exit", c)
            return [{"op": "exit", "at": self.at(c), **({"code": self.word(rest[0])} if rest else {})}]
        if name == "return":
            self.count("sh:return", c)
            return [{"op": "return", "at": self.at(c), **({"value": self.word(rest[0])} if rest else {})}]
        if name in ("break", "continue"):
            self.count("sh:break-continue", c)
            return [{"op": name, "at": self.at(c)}]
        if name in ("echo", "printf", "print") and stdin is None:
            self.count("sh:echo", c)
            node: dict[str, Any] = {"op": "output", "at": self.at(c), "channel": "log", "value": self._joined(rest)}
            target = next((r.target for r in c.redirects if r.op in (">", ">>", ">|") and r.fd in ("", "1") and r.target is not None), None)
            if target is not None:
                self.count("sh:redirect", c)
                node["channel"] = "file"
                node["target"] = self.word(target)
            return [node]
        if name == "eval":
            self.count("sh:eval", c, False)
            self.out.note("eval runs a command built at run time: it is not read")
            return [{"op": "platform", "at": self.at(c), "construct": "sh:eval"}]
        if name == "trap":
            self.count("sh:trap", c)
            return [{"op": "platform", "at": self.at(c), "construct": "sh:trap", "detail": {"signals": [w.literal() for w in rest[1:] if w.literal()]}}]
        if name == "set":
            self.count("sh:set-option", c)
            return [{"op": "call", "at": self.at(c), "call": {"e": "call", "callee": {"e": "name", "name": "set", "kind": "builtin"}, "args": [self.word(w) for w in rest if (w.literal() or "").startswith(("-", "+")) or w.literal() in ("errexit", "pipefail", "nounset", "xtrace")]}}]
        if name in ("read",):
            self.count("sh:assignment", c)
            names = [w.literal() for w in rest if w.literal() and not w.literal().startswith("-") and re.fullmatch(r"[A-Za-z_]\w*", w.literal())]
            return [{"op": "assign", "at": self.at(c), "target": var_expr(v), "value": {"e": "call", "callee": {"e": "name", "name": "read", "kind": "builtin"}}} for v in names]
        if name in _TEST_BUILTINS:
            self.count("sh:test", c)
            return [{"op": "call", "at": self.at(c), "call": self.test_expr(c.words, c) if name in ("[", "test") else {"e": "lit", "value": name != "false", "type": "boolean"}}]
        return interpret(self.ctx, self.cmd(c, stdin, stdin_at))

    def _joined(self, words: list[Word]) -> dict[str, Any]:
        if not words:
            return {"e": "lit", "value": "", "type": "string"}
        if len(words) == 1:
            return self.word(words[0])
        parts: list[Any] = []
        for i, w in enumerate(words):
            if i:
                parts.append(" ")
            e = self.word(w)
            parts += e["parts"] if e["e"] == "template" else [e["value"]] if e["e"] == "lit" else [e]
        return {"e": "template", "parts": parts}

    def assign(self, name: str, value: Word | None, a: int, b: int, local: bool = False, export: bool = False) -> dict[str, Any]:
        sensitive = bool(_SENSITIVE.search(name))
        self.out.construct("sh:sensitive-value" if sensitive else "sh:export" if export else "sh:assignment", a, b, True)
        expr = self.word(value, sensitive)
        if value is not None and len(value.parts) == 1 and value.parts[0].kind == "cmd" and script_dir_cmd(value.parts[0].text):
            expr = {"e": "name", "name": "script_dir", "kind": "builtin"}
        if local:
            return {"op": "declare", "at": self.out.at(a, b), "name": name, "scope": "local", "value": expr}
        node = {"op": "assign", "at": self.out.at(a, b), "target": var_expr(name), "value": expr}
        if export:
            node["export"] = True
        return node


def lower(ctx: Context, script: posix.Script) -> list[dict[str, Any]]:
    return Lowering(ctx, ctx.out.text).block(script.body)


__all__ = ["Lowering", "lower", "resolver", "script_dir_cmd"]
