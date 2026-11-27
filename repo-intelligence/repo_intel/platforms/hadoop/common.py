"""What the Hadoop pack's extractors share: emission, path names and substitutions.

Paths. A dataset on a cluster file system is named by its path. The namenode is a host,
so it is never kept: `hdfs://nn1:8020/data/raw/orders` and `${nameNode}/data/raw/orders`
are both the Dataset `/data/raw/orders`. Object stores keep their bucket
(`s3a://landing/orders`). A substitution the file does not resolve (`${runDate}`, Pig's
`$INPUT`) stays in the name as `{runDate}`, and the name is marked parameterized; it is
never resolved, not even from a default the file states (a default is a value).

Substitutions in SQL. HiveQL text with `${hivevar:x}` cannot be parsed as it is. Before
the shared SQL readers see a statement, a substitution in a name position (`${db}.orders`,
`orders_${suffix}`, `FROM ${source}`) becomes an identifier token that no source contains,
one in a value position becomes the bind parameter `:x`, and one inside a string literal
becomes `{x}`. Offsets map back to the original text, and tokens are written back as
`{x}` in everything read from the statement.
"""

from __future__ import annotations

import bisect
import json
import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity

P = Predicate
E1 = EvidenceClass.E1
E2 = EvidenceClass.E2
LANGUAGE = "hadoop"  # the transform language of every shape this pack emits (contract 10.1.3)

_NAMENODE = re.compile(r"^\$?\{\s*(?:(?:hivevar|hiveconf|env|system|var):)?(?:name_?node|namenode|nn|hdfs_?uri|hdfs_?url|fs\.?default\.?fs|default_?fs|fs_?uri)\s*\}", re.I)
_SCHEME_HOST = re.compile(r"^(hdfs|maprfs|viewfs|webhdfs|swebhdfs)://[^/]*", re.I)
_EL = re.compile(r"\$\{([^{}]*)\}")
_PIG_PARAM = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][\w.]*\}")


def placeholder(expr: str) -> str:
    """`{name}` for one substitution: its variable, or the function an expression starts with."""
    expr = expr.strip()
    m = re.match(r"(?:(?:hivevar|hiveconf|env|system|var|define):)?([A-Za-z_][\w.]*)\s*$", expr)
    if m:
        return "{" + m.group(1) + "}"
    m = re.match(r"([A-Za-z_]\w*)(?::([A-Za-z_]\w*))?", expr)
    if m:
        return "{" + (m.group(1) + ("_" + m.group(2) if m.group(2) else "")) + "}"
    return "{expr}"


def dataset_path(raw: str, pig: bool = False) -> tuple[str, bool] | None:
    """A path's Dataset name and whether it is parameterized; None when it is not a path."""
    text = raw.strip().strip("'\"")
    if not text or len(text) > 500 or "\n" in text:
        return None
    text = _NAMENODE.sub("", text)
    text = _SCHEME_HOST.sub("", text)
    if pig:
        text = _PIG_PARAM.sub(lambda m: "{" + (m.group(1) or m.group(2)) + "}", text)
    text = _EL.sub(lambda m: placeholder(m.group(1)), text)
    text = text.split("?", 1)[0].split("#", 1)[0]
    if "://" not in text and "@" in text:
        return None  # user information or a mail address, not a path
    if text not in ("/",):
        text = text.rstrip("/")
    if not text or text.startswith("-") or _PLACEHOLDER.fullmatch(text):
        return None  # an option, or a path given whole at run time: nothing of it is stated
    if "://" not in text and not text.startswith(("/", "{")) and "/" not in text:
        return None  # a bare word is a table or a variable, not a path
    if "://" not in text:
        text = re.sub(r"/{2,}", "/", text)
    return text, bool(_PLACEHOLDER.search(text))


def table_name(raw: str, pig: bool = False) -> tuple[str, bool] | None:
    """A table name as written (`db.table`), lower-cased, with substitutions as placeholders."""
    text = raw.strip().strip("'\"").replace("`", "")
    if pig:
        text = _PIG_PARAM.sub(lambda m: "{" + (m.group(1) or m.group(2)) + "}", text)
    text = _EL.sub(lambda m: placeholder(m.group(1)), text)
    if not re.fullmatch(r"[\w{}.$]+", text) or text.startswith("."):
        return None
    text = text.lower()
    return text, bool(_PLACEHOLDER.search(text))


def resolve_relative(base_dir: str, target: str) -> str | None:
    """A literal path relative to an application directory, as a repository path; None when it
    is absolute, names a variable or leaves the repository."""
    t = target.strip()
    if not t or t.startswith(("/", "$", "{")) or "://" in t or "${" in t:
        return None
    t = t.split("#", 1)[0]
    joined = posixpath.normpath(posixpath.join(base_dir, t)) if base_dir else posixpath.normpath(t)
    if joined.startswith("../") or joined == ".." or joined.startswith("/"):
        return None
    return joined


# ------------------------------------------------------------------ emission


class Out:
    """Observations and diagnostics of one file."""

    def __init__(self, extractor: str, path: str, data: bytes, blob: str, evidence_class: EvidenceClass = E2):
        from repo_intel.extractors import base

        self.base = base
        self.extractor, self.path, self.data, self.blob = extractor, path, data, blob
        self.evidence_class = evidence_class
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = 0
        self.unresolved = 0

    def span(self, a: int, b: int):
        a = max(0, min(a, len(self.data)))
        return self.base.span(self.data, a, max(a, min(b, len(self.data))))

    def whole(self):
        return self.base.whole_file(self.data)

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.extractor, severity, message))

    def term(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, evidence_class: EvidenceClass | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}
        o = platform_entity(self.blob, at, term, key, name, clean, evidence_class or self.evidence_class)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, at, kind: K, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}
        self.items.append(self.base.entity(self.blob, at, kind, key, name, clean, self.evidence_class))
        return self.base.ref(kind, key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, evidence_class: EvidenceClass | None = None) -> None:
        clean = {k: v for k, v in (qualifiers or {}).items() if v not in (None, [], "", {})}
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, clean, evidence_class or self.evidence_class))

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=self.evidence_class))

    def table(self, at, name: str, parameterized: bool = False, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.entity(at, K.TABLE, {"datastore": "default", "name": name}, name, {**(attrs or {}), **({"parameterized": True} if parameterized else {})})

    def dataset(self, at, name: str, parameterized: bool = False, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.entity(at, K.DATASET, {"name": name}, name, {**(attrs or {}), **({"parameterized": True} if parameterized else {})})

    def moves(self, at, subject, predicate, target, parameterized: bool = False, qualifiers: dict[str, Any] | None = None) -> None:
        self.relation(at, subject, predicate, target, {**(qualifiers or {}), **({"parameterized": True} if parameterized else {})})
        self.resolved += 1

    def transforms(self, at, owner, transforms: list[dict[str, Any]]) -> None:
        from repo_intel.compile.transforms import transform_observation

        if transforms:
            self.items.append(transform_observation(self.blob, at, owner, LANGUAGE, transforms, self.evidence_class))

    def behavior(self, at, owner, body: list[dict[str, Any]], kind: str, params: list[dict[str, Any]] | None = None) -> None:
        from repo_intel.compile.behavior import behavior_observation

        self.items.append(behavior_observation(self.blob, at, owner, LANGUAGE, body, params or [], self.evidence_class, kind=kind))

    def finish(self, parsed: bool = True):
        from repo_intel.extractors import sql_lineage

        items = self.items + sql_lineage.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return self.base.Extraction(items, self.diagnostics, parsed=parsed)


def program(platform: str, path: str) -> dict[str, Any]:
    return {"kind": "Program", "natural_key": {"platform": platform, "name": path}, "name": posixpath.basename(path)}


# ------------------------------------------------------------------ substitutions in SQL

_SQL_SUB = re.compile(r"\$\{([^{}]*)\}")
_NAME_KEYWORDS = re.compile(r"(?:\b(?:from|join|table|into|update|use|exists|view|overwrite|database|schema|merge|location|stats|metadata|refresh|describe|truncate)\s+)$", re.I)


class Substituted:
    """One statement with its substitutions replaced, and the way back to the original text."""

    def __init__(self, text: str, tokens: dict[str, str] | None = None, avoid: str = ""):
        """`tokens` is shared by every statement of a file, so a token means one substitution
        throughout it; `avoid` is the file's text, which no token may occur in."""
        self.original = text
        self.avoid = (avoid or text).lower()
        self.tokens: dict[str, str] = tokens if tokens is not None else {}  # token -> "{name}"
        self.names: list[str] = []  # the substitutions as written inside ${...}, in order
        out: list[str] = []
        self._new: list[int] = []  # start of each replacement in the new text
        self._old: list[int] = []  # and in the original
        self._new_end: list[int] = []
        self._old_end: list[int] = []
        pos = 0
        length = 0
        quote = None
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if quote:
                if c == quote:
                    quote = None
                elif c == "\\":
                    i += 2
                    continue
            elif c in "'\"`":
                quote = c
            elif text.startswith("--", i):
                end = text.find("\n", i)
                i = n if end < 0 else end
                continue
            if c == "$" and text.startswith("${", i):
                m = _SQL_SUB.match(text, i)
                if m:
                    expr = m.group(1).strip()
                    self.names.append(expr)
                    ph = placeholder(expr)
                    if quote:
                        rep = ph
                    elif self._name_position(text, m.start(), m.end()):
                        rep = self._token(ph)
                    else:
                        rep = ":" + ph[1:-1].replace(".", "_")
                    out.append(text[pos:m.start()])
                    length += m.start() - pos
                    self._new.append(length)
                    self._old.append(m.start())
                    out.append(rep)
                    length += len(rep)
                    self._new_end.append(length)
                    self._old_end.append(m.end())
                    pos = i = m.end()
                    continue
            i += 1
        out.append(text[pos:])
        self.text = "".join(out)

    def _token(self, ph: str) -> str:
        for token, value in self.tokens.items():
            if value == ph:
                return token
        token = f"zqv{len(self.tokens)}q"
        while token in self.avoid or any(t.startswith(token) or token.startswith(t) for t in self.tokens):
            token += "q"
        self.tokens[token] = ph
        return token

    @staticmethod
    def _name_position(text: str, a: int, b: int) -> bool:
        before, after = text[a - 1:a] if a else "", text[b:b + 1]
        if re.match(r"[\w.]", before) or re.match(r"[\w.]", after):
            return True
        return bool(_NAME_KEYWORDS.search(text[max(0, a - 40):a]))

    def to_original(self, i: int) -> int:
        """The original offset of offset `i` in the substituted text."""
        k = bisect.bisect_right(self._new, i) - 1
        if k < 0:
            return i
        if i < self._new_end[k]:
            return self._old[k]  # inside a replacement: its start
        return self._old_end[k] + (i - self._new_end[k])

    def restore(self, value: str) -> str:
        for token, ph in self.tokens.items():
            value = re.sub(re.escape(token), lambda _m, ph=ph: ph.lower(), value, flags=re.I)
        return value

    def restore_all(self, node: Any) -> Any:
        if not self.tokens:
            return node
        return json.loads(self.restore(json.dumps(node)))


def mark_dynamic(transforms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set `dynamic` on transforms whose inputs or output keep a `{placeholder}` (after tokens
    are restored), with each transform's fields in the contract's order."""
    from repo_intel.compile.transforms import FIELDS

    out = []
    for t in transforms:
        ids = {f"t{i}" for i in range(1, len(transforms) + 1)}
        names = [i for i in t.get("inputs", []) if i not in ids] + ([t["output"]] if t.get("output") else [])
        if any(_PLACEHOLDER.search(n) for n in names):
            t = {**t, "dynamic": True}
        out.append({k: t[k] for k in FIELDS if k in t})
    return out
