"""Helpers for extractors that read source text with a lexer and patterns.

Languages without a full parser here (Java, Kotlin, Go, C#, Ruby, Scala, Swift,
protobuf, GraphQL, Bicep) are lexed with Pygments only to blank their comments:
every comment character becomes a space, so offsets still point into the file and
a commented-out route or query is never read. What remains is matched with
patterns, so facts from these files are declared at evidence class E2 when the
construct is unambiguous (an annotation with a literal path) and E4 otherwise.

This module defines no extractors.
"""

from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from typing import Any

from repo_intel.compile.contracts import Span
from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import PACKAGE, UNIT, Extraction, entity, obs, ref, relation, span, whole_file

K = EntityKind
P = Predicate


@lru_cache(maxsize=None)
def _lexer(name: str):
    from pygments.lexers import get_lexer_by_name

    return get_lexer_by_name(name, stripnl=False, stripall=False, ensurenl=False, tabsize=0)


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return None


def without_comments(text: str, lexer: str) -> str:
    """`text` with every comment character (newlines kept) replaced by a space."""
    from pygments.token import Comment

    out = list(text)
    for index, token, value in _lexer(lexer).get_tokens_unprocessed(text):
        if token in Comment and not (token in Comment.Preproc or token in Comment.PreprocFile):
            for i in range(index, min(index + len(value), len(out))):
                if out[i] != "\n":
                    out[i] = " "
    return "".join(out)


def blank(text: str, pattern: re.Pattern[str] | str, flags: int = 0) -> str:
    """Replace every match of `pattern` with spaces (newlines kept)."""
    rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
    return rx.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def matching(text: str, open_at: int, pair: str = "{}") -> int:
    """Index just past the bracket that closes the one at `open_at` (strings skipped)."""
    opener, closer = pair
    depth, i, quote = 0, open_at, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def unquote(raw: str) -> str | None:
    """The value of a plain string literal ("x", 'x', `x`, @"x"), else None."""
    raw = raw.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'`" and "\\" not in raw[1:-1] and "${" not in raw and "#{" not in raw:
        return raw[1:-1]
    return None


def join_route(*parts: str) -> str:
    """Join path segments into one route with single slashes and a leading slash."""
    pieces = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/" + "/".join(pieces) if pieces else "/"


class Doc:
    """One file's text and the observations found in it. Offsets are characters."""

    def __init__(self, path: str, data: bytes, blob: str, text: str):
        self.path, self.data, self.blob, self.text = path, data, blob, text
        self.out: list = []
        self.resolved = self.unresolved = 0
        ascii_only = len(text) == len(data)
        self._to_byte = (lambda i: i) if ascii_only else (lambda i: len(text[:i].encode("utf-8")))

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path)

    def at(self, a: int, b: int) -> Span:
        return span(self.data, self._to_byte(a), self._to_byte(b))

    def whole(self) -> Span:
        return whole_file(self.data)

    def entity(self, at: Span, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = EvidenceClass.E2) -> dict:
        self.out.append(entity(self.blob, at, kind, key, name, attrs or {}, ev))
        return ref(kind, key, name)

    def relation(self, at: Span, subject, predicate, obj, qualifiers: dict | None = None, ev: EvidenceClass = EvidenceClass.E2) -> None:
        self.out.append(relation(self.blob, at, subject, predicate, obj, qualifiers or {}, ev))

    def observe(self, kind: str, at: Span, payload: dict[str, Any], ev: EvidenceClass = EvidenceClass.E2) -> None:
        self.out.append(obs(kind, self.blob, at, payload, ev))

    def endpoint(self, at: Span, method: str, route: str, framework: str, handler: str | None = None, protocol: str = "http", ev: EvidenceClass = EvidenceClass.E2) -> dict:
        """An implemented endpoint of the unit this file belongs to."""
        key = {"component_root": UNIT, "protocol": protocol, "method": method, "route": route}
        attrs = {"framework": framework, **({"handler": handler} if handler else {})}
        return self.entity(at, K.ENDPOINT, key, f"{method} {route}", attrs, ev)

    def table(self, at: Span, name: str, predicate=None, attrs: dict | None = None, ev: EvidenceClass = EvidenceClass.E2) -> dict:
        target = self.entity(at, K.TABLE, {"datastore": "default", "name": name}, name, attrs, ev)
        if predicate is not None:
            self.relation(at, PACKAGE, predicate, target, {}, ev)
        return target

    def topic(self, at: Span, role: str, transport: str, topic: str | None = None, name: str | None = None) -> None:
        """A publish or subscribe call; the linker turns it into an EventTopic edge."""
        payload: dict[str, Any] = {"role": role, "transport": transport}
        if topic:
            payload["topic"] = topic
        elif name:
            payload["name"] = name
        else:
            payload["dynamic"] = True
        self.observe("topic_use", at, payload)

    def done(self, parsed: bool = True, diagnostics=None) -> Extraction:
        from repo_intel.extractors.sql_lineage import lineage_summary

        return Extraction(self.out + lineage_summary(self.blob, self.data, self.resolved, self.unresolved), diagnostics or [], parsed)


def sql_in(doc: Doc, sql: str, offset: int, dialect: str | None = None, subject=PACKAGE) -> None:
    """Tables a SQL string literal reads and writes, as Table edges of `subject`."""
    from repo_intel.extractors.sql_lineage import lineage

    found = lineage(sql, dialect)
    doc.resolved += len(found.writes) + len(found.reads)
    doc.unresolved += found.unresolved
    for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
        for name, a, b in items:
            at = doc.at(offset + a, offset + b)
            target = doc.entity(at, K.TABLE, {"datastore": "default", "name": name}, name, {"parameterized": True} if "{" in name else {})
            doc.relation(at, subject, predicate, target)


_SQL_START = re.compile(r"^\s*(select|insert|update|delete|merge|with|replace|upsert)\b", re.I)


def looks_like_sql(value: str) -> bool:
    return bool(_SQL_START.match(value)) and len(value) > 12


# Message brokers named the same way across languages: a queue URL or ARN is reduced
# to the queue or topic name it ends in.
def queue_name(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or "{" in raw or "$" in raw:
        return None
    if raw.startswith("arn:"):
        raw = raw.rsplit(":", 1)[-1]
    elif "://" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
    elif raw.startswith("projects/"):
        raw = raw.rsplit("/", 1)[-1]
    return raw if re.fullmatch(r"[A-Za-z0-9._-]{1,260}", raw) else None


# Nested blocks that add a route prefix (`route("/api") { ... }`, `r.Route("/v1", func...{`,
# `namespace :admin do ... end`): each is (start, end, prefix).
def prefix_at(pos: int, blocks: list[tuple[int, int, str]]) -> str:
    """The joined prefixes of every block that contains `pos`, outermost first."""
    return join_route(*(p for a, b, p in sorted(blocks) if a <= pos < b))


def string_args(args: str) -> list[tuple[str, int, int]]:
    """Every plain string literal in an argument list, with its offsets inside `args`."""
    return [(v, m.start(), m.end()) for m in re.finditer(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`[^`]*`', args) if (v := unquote(m.group())) is not None]


def keyword_arg(args: str, *names: str) -> str | None:
    """The raw text of `name = value` (or `name: value`) in an argument list, up to the next top-level comma."""
    for name in names:
        m = re.search(rf"(?<![\w.]){re.escape(name)}\s*[=:]\s*", args)
        if m:
            depth, i = 0, m.end()
            while i < len(args):
                c = args[i]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif c == "," and depth == 0:
                    break
                elif c in "\"'":
                    j = args.find(c, i + 1)
                    i = j if j > 0 else len(args)
                i += 1
            return args[m.end():i].strip()
    return None


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


# -- code traits ------------------------------------------------------------------

_DECLARATIONS = {
    # a method or function header ending in its body's `{`: Java, Kotlin, C#
    "c": re.compile(r"\b(?!(?:if|for|foreach|while|switch|catch|using|lock|return|new|else|do|try|synchronized|when|fun|typeof|nameof|sizeof|default)\b)([A-Za-z_]\w*)\s*\((?:[^;{}()]|\([^;{}()]*\))*\)\s*(?::\s*[\w<>?,.\[\] ]+)?(?:throws\s+[\w.,\s]+)?\{"),
    "go": re.compile(r"\bfunc\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\([^{]*\{"),
}


class Functions:
    """The functions of a file, found by their headers, with the span of each body."""

    def __init__(self, text: str, style: str):
        self._bodies = [(m.start(), matching(text, m.end() - 1), m.group(1), m.end()) for m in _DECLARATIONS[style].finditer(text)]

    def around(self, pos: int) -> str:
        """The innermost function whose body holds `pos`, else `<module>`."""
        inside = [(start, name) for start, end, name, body in self._bodies if body <= pos < end]
        return max(inside)[1] if inside else "<module>"

    def after(self, pos: int) -> str:
        """The first function declared after `pos`: the one an annotation or attribute is on."""
        return next((name for start, end, name, body in self._bodies if start >= pos), "<module>")


def code_traits(d: Doc, rules, style: str) -> None:
    """Code traits (spec §8.8) from pattern rules: each rule is (pattern, make, placement),
    where make turns a match into a payload, a list of payloads or None, and placement says
    whether the construct sits in a function body ("in") or annotates the next one ("on")."""
    functions = Functions(d.text, style)
    for pattern, make, placement in rules:
        for m in pattern.finditer(d.text):
            made = make(m) if callable(make) else [dict(x) for x in make] if isinstance(make, list) else dict(make)
            for payload in made if isinstance(made, list) else [made] if made else []:
                function = functions.after(m.end()) if placement == "on" else functions.around(m.start())
                d.observe("code_trait", d.at(m.start(), m.end()), {"function": function, **payload})
