"""Transform shapes: what a unit does to the rows it reads (schema `rkp-transform/1`).

Lineage says a unit reads A and B and writes C. A transform shape says how: the list of
operations between them, each with its inputs, output, keys, ordering, column effect and
source span, and the name of the known idiom it is, when one matched exactly. The
vocabulary (operations, the column effect, primitive names and their categories) comes
from pipeline-builder (sourceform), which uses it to generate pipelines; here it is used to
recognise them. docs/solution-ir-contract.md section 8.1 is the contract.

One observation (`transform_observation`) describes one unit:

    {"schema": "rkp-transform/1",
     "owner": <entity ref of the Program, Symbol or Notebook>, or "@package" / "@file",
     "language": "sql" | "sas" | "pyspark",
     "transforms": [{"id": "t1", "op": "deduplicate", "at": [120, 410],
                     "inputs": ["work.orders_sorted"], "output": "work.orders_latest",
                     "keys": ["order_id"], "order": [{"column": "updated_at", "desc": true}],
                     "primitive": "dedupe_by_key"}]}

Where shapes come from:

- `sql_unit` and `SqlRecognizer`: SQL statements read with sqlglot, never run. Stand-alone
  scripts (`sql_lineage.SqlScripts`) and PySpark (`data_python`) call them and emit
  observations themselves.
- `from_behavior`: a stored behaviour unit (repo_intel.compile.behavior). The compiler calls
  it for every unit it stores: SQL `query` nodes go through the SQL recognizer, SAS DATA steps
  and procedures are read from their lowering (`_SasStep`, `_sas_proc`).

A primitive is given only when the whole idiom matched exactly; a partial match lists
`candidates` instead. A name that is not literal (a macro variable, an f-string) keeps its
`{placeholder}` and marks the transform `dynamic`; a query whose tables are named at run time,
or that runs in another database, is one `platform` transform, never resolved by guesswork.
Output is deterministic: transforms come in source order and every list is built in a fixed
order, so the snapshot hash stays reproducible.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from repo_intel.compile.contracts import Observation, Span
from repo_intel.compile.ontology import EvidenceClass

SCHEMA = "rkp-transform/1"
LANGUAGES = ("sql", "sas", "pyspark")
OPS = ("read", "project", "filter", "join", "union", "deduplicate", "aggregate", "window", "pivot", "unpivot", "sort", "validate", "publish", "platform")
JOIN_TYPES = ("inner", "left", "right", "full", "semi", "anti", "cross")
# pipeline-builder's categories, snake-cased, in the order that breaks ties for `purpose`.
CATEGORIES = ("structural_reshaping", "type_standardization", "cleaning", "deduplication", "cdc", "integration", "business_rules", "aggregation")
# Primitive names recognisable from code, with their category. A name is listed only when its
# idiom can be matched exactly; names that describe business meaning (calculate_score,
# classify_record, derive_status) are left to rule lifting.
PRIMITIVES: dict[str, str] = {
    "select_columns": "structural_reshaping",
    "drop_columns": "structural_reshaping",
    "rename_columns": "structural_reshaping",
    "safe_cast": "type_standardization",
    "parse_timestamp": "type_standardization",
    "derive_column": "business_rules",
    "conditional_flag": "business_rules",
    "map_codes": "business_rules",
    "coalesce_columns": "cleaning",
    "apply_defaults": "cleaning",
    "trim_strings": "cleaning",
    "normalize_case": "cleaning",
    "split_by_condition": "structural_reshaping",
    "union_by_name": "integration",
    "join": "integration",
    "dedupe_by_key": "deduplication",
    "dedupe_by_hash": "deduplication",
    "aggregate": "aggregation",
    "window_function": "aggregation",
    "calculate_running_total": "aggregation",
    "delta_calc": "aggregation",
    "top_n_filter": "aggregation",
    "pivot": "structural_reshaping",
    "unpivot": "structural_reshaping",
    "merge_into": "cdc",
    "apply_scd_type_2": "cdc",
    "apply_deletes": "cdc",
}
FIELDS = ("id", "op", "at", "inputs", "output", "keys", "order", "join", "columns", "predicate", "measures", "primitive", "candidates", "dynamic", "construct")
OWNER_STRINGS = ("@package", "@file")  # the file's package (the file's Artifact when the unit has none), the file's Artifact
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][\w.]*\}")


# ------------------------------------------------------------------ the contract


def _span_ok(at: Any) -> bool:
    return isinstance(at, list) and len(at) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in at) and at[0] <= at[1]


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _check_transform(t: Any, i: int, problems: list[str]) -> None:
    from repo_intel.compile.behavior import _check_expr

    where = f"transforms[{i}]"
    if not isinstance(t, dict):
        problems.append(f"{where}: a transform must be an object")
        return
    for k in t:
        if k not in FIELDS:
            problems.append(f"{where}: unknown field {k!r}")
    if t.get("id") != f"t{i + 1}":
        problems.append(f"{where}: id must be 't{i + 1}', not {t.get('id')!r}")
    if t.get("op") not in OPS:
        problems.append(f"{where}: unknown op {t.get('op')!r}")
    if not _span_ok(t.get("at")):
        problems.append(f"{where}: needs a span [start_byte, end_byte], not {t.get('at')!r}")
    inputs = t.get("inputs", [])
    if not _names(inputs):
        problems.append(f"{where}: inputs must be a list of names")
    else:
        # A table may be called t3, so only a transform reading itself is certainly wrong.
        if t.get("id") in inputs:
            problems.append(f"{where}: a transform cannot read its own result")
    if "output" in t and not (isinstance(t["output"], str) and t["output"]):
        problems.append(f"{where}: output must be a name")
    if "keys" in t and not _names(t["keys"]):
        problems.append(f"{where}: keys must be a list of column names")
    for j, o in enumerate(t.get("order", []) if isinstance(t.get("order", []), list) else [None]):
        if not (isinstance(o, dict) and set(o) == {"column", "desc"} and isinstance(o["column"], str) and isinstance(o["desc"], bool)):
            problems.append(f"{where}.order[{j}]: must be {{column, desc}}")
    if "join" in t:
        j = t["join"]
        if not (isinstance(j, dict) and j.get("type") in JOIN_TYPES and set(j) <= {"type", "on"}):
            problems.append(f"{where}: join must be {{type, on}} with type one of {', '.join(JOIN_TYPES)}")
        elif not all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in j.get("on", [])):
            problems.append(f"{where}: join.on must be a list of [left, right] column pairs")
    if "columns" in t:
        c = t["columns"]
        if not (isinstance(c, dict) and set(c) <= {"add", "replace", "drop", "rename"}):
            problems.append(f"{where}: columns may hold only add, replace, drop and rename")
        else:
            for k in ("add", "replace", "drop"):
                if k in c and not _names(c[k]):
                    problems.append(f"{where}: columns.{k} must be a list of names")
            if "rename" in c and not (isinstance(c["rename"], dict) and all(isinstance(a, str) and isinstance(b, str) for a, b in c["rename"].items())):
                problems.append(f"{where}: columns.rename must map old names to new ones")
    if "predicate" in t:
        p = t["predicate"]
        if isinstance(p, dict) and set(p) == {"sql"}:
            if not isinstance(p["sql"], str):
                problems.append(f"{where}: predicate.sql must be text")
        else:
            _check_expr(p, f"{where}.predicate", problems)
    for j, m in enumerate(t.get("measures", []) if isinstance(t.get("measures", []), list) else [None]):
        if not (isinstance(m, dict) and isinstance(m.get("fn"), str) and set(m) <= {"fn", "column", "as"} and all(isinstance(m[k], str) for k in m)):
            problems.append(f"{where}.measures[{j}]: must be {{fn, column, as}} with text values")
    if "primitive" in t:
        if t["primitive"] not in PRIMITIVES:
            problems.append(f"{where}: unknown primitive {t['primitive']!r}")
        if "candidates" in t:
            problems.append(f"{where}: a transform has a primitive or candidates, never both")
    if "candidates" in t and not (_names(t["candidates"]) and all(c in PRIMITIVES for c in t["candidates"]) and t["candidates"]):
        problems.append(f"{where}: candidates must be a non-empty list of primitive names")
    if "dynamic" in t and t["dynamic"] is not True:
        problems.append(f"{where}: dynamic is present only when true")
    if t.get("op") == "platform" and not isinstance(t.get("construct"), str):
        problems.append(f"{where}: a platform transform names its construct")


def validate_shape(unit: Any) -> list[str]:
    """Every way `unit` breaks the transform-shape contract; empty when it is valid."""
    if not isinstance(unit, dict):
        return ["a transform unit must be an object"]
    problems: list[str] = []
    owner = unit.get("owner")
    if not (owner in OWNER_STRINGS or (isinstance(owner, dict) and {"kind", "natural_key", "name"} <= set(owner))):
        problems.append("owner must be an entity reference {kind, natural_key, name}, '@package' or '@file'")
    if unit.get("language") not in LANGUAGES:
        problems.append(f"language must be one of {', '.join(LANGUAGES)}")
    if unit.get("schema", SCHEMA) != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    transforms = unit.get("transforms")
    if not isinstance(transforms, list) or not transforms:
        problems.append("transforms must be a non-empty list")
        return problems
    for i, t in enumerate(transforms):
        _check_transform(t, i, problems)
    return problems


def transform_observation(blob: str, at: Span, owner: dict[str, Any] | str, language: str, transforms: list[dict[str, Any]], evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """One unit's transform shapes, validated before it is emitted (kind `transform`)."""
    from repo_intel.extractors.base import obs

    unit = {"schema": SCHEMA, "owner": owner, "language": language, "transforms": transforms}
    problems = validate_shape(unit)
    if problems:
        raise ValueError("invalid transform shapes: " + "; ".join(problems[:5]))
    return obs("transform", blob, at, unit, evidence_class)


def moves_data(transforms: Iterable[Mapping[str, Any]]) -> bool:
    """Whether a unit's shapes recognise anything: at least one transform that is not `platform`."""
    return any(t.get("op") != "platform" for t in transforms)


def summary(transforms: Iterable[Mapping[str, Any]]) -> tuple[list[str], dict[str, int]]:
    """The sorted primitive names and how many transforms carry each category's primitives."""
    names: set[str] = set()
    counts: dict[str, int] = {}
    for t in transforms:
        p = t.get("primitive")
        if p in PRIMITIVES:
            names.add(p)
            counts[PRIMITIVES[p]] = counts.get(PRIMITIVES[p], 0) + 1
    return sorted(names), counts


def purpose(counts: Mapping[str, int]) -> str | None:
    """The category with the most primitives, ties broken by CATEGORIES order; None when there are none."""
    best = max(CATEGORIES, key=lambda c: (counts.get(c, 0), -CATEGORIES.index(c)))
    return best if counts.get(best, 0) else None


# ------------------------------------------------------------------ building a unit


class Builder:
    """Collects one unit's transforms. Ids are provisional (`~n`) until `finish`, which joins
    the idioms that span several transforms and numbers them t1, t2, ... in order."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._n = 0

    @staticmethod
    def is_id(ref: Any) -> bool:
        return isinstance(ref, str) and ref.startswith("~")

    def get(self, ref: Any) -> dict[str, Any] | None:
        return next((t for t in self.items if t["id"] == ref), None) if self.is_id(ref) else None

    def add(self, op: str, at: list[int], inputs: Iterable[str], **fields: Any) -> str:
        self._n += 1
        item: dict[str, Any] = {"id": f"~{self._n}", "op": op, "at": [int(at[0]), int(at[1])], "inputs": [i for i in inputs if i]}
        for k, v in fields.items():
            if v is None or v is False or (isinstance(v, (list, dict, tuple)) and not v):
                continue
            item[k] = list(v) if isinstance(v, tuple) else v
        self.items.append(item)
        return item["id"]

    def ensure(self, ref: str, at: list[int]) -> str:
        """A transform id for `ref`: itself, or a `read` of the table it names."""
        return ref if self.is_id(ref) else self.add("read", at, [ref])

    def write(self, ref: str | None, output: str, at: list[int], **hints: Any) -> str | None:
        """`ref`'s result is written to `output`: the transform that made it names it, or a read
        (for a table copied as it is) or a publish (for a result already written) does."""
        if ref is None:
            return None
        item = self.get(ref)
        if item is None:
            ref = self.add("read", at, [ref])
            item = self.get(ref)
        elif "output" in item:
            ref = self.add("publish", at, [ref])
            item = self.get(ref)
        assert item is not None
        item["output"] = output
        item.update({k: v for k, v in hints.items() if v is not None})
        return ref

    def uses(self, ref: str) -> int:
        return sum(t["inputs"].count(ref) for t in self.items)

    def finish(self) -> list[dict[str, Any]]:
        _collapse_ranks(self)
        _scd_type_2(self)
        numbers = {t["id"]: f"t{i + 1}" for i, t in enumerate(self.items)}
        out = []
        for t in self.items:
            row = {k: v for k, v in t.items() if not k.startswith("_")}
            row["id"] = numbers[t["id"]]
            row["inputs"] = [numbers.get(i, i) for i in t["inputs"]]
            if "primitive" in row:
                row.pop("candidates", None)
            elif "candidates" in row:
                row["candidates"] = sorted(set(row["candidates"]))
            if any(_PLACEHOLDER.search(n) for n in [*(i for i in t["inputs"] if not self.is_id(i)), row.get("output", "")]):
                row["dynamic"] = True
            out.append({k: row[k] for k in FIELDS if k in row})
        return out


def _collapse_ranks(b: Builder) -> None:
    """ROW_NUMBER() OVER (PARTITION BY k ORDER BY ts DESC) then `rn = 1` is one deduplication by
    key; `rn <= n` keeps the top n. The window becomes that transform, the filter goes, and
    projections that only keep columns between them stay where they are."""
    for f in list(b.items):
        test = f.get("_rank_test")
        if not test or len(f["inputs"]) != 1:
            continue
        column, n = test
        ref = f["inputs"][0]
        node = b.get(ref)
        while node is not None and node["op"] == "project" and node.get("primitive") == "select_columns" and "columns" not in node and b.uses(node["id"]) == 1:
            node = b.get(node["inputs"][0]) if len(node["inputs"]) == 1 else None
        if node is None or node.get("_rank", {}).get("as") != column or b.uses(node["id"]) != 1:
            continue
        fn = node["_rank"]["fn"]
        node.pop("measures", None)
        node.pop("primitive", None)
        node.pop("candidates", None)
        node["at"] = [min(node["at"][0], f["at"][0]), max(node["at"][1], f["at"][1])]
        if fn == "row_number" and n == 1 and node.get("keys"):
            node["op"], node["primitive"] = "deduplicate", "dedupe_by_key"
        elif fn == "row_number":
            node["op"], node["primitive"] = "filter", "top_n_filter"
        else:  # rank and dense_rank keep ties: several rows per key may pass
            node["op"], node["candidates"] = ("deduplicate", ["dedupe_by_key"]) if n == 1 and node.get("keys") else ("filter", ["top_n_filter"])
        node.pop("_rank")
        replacement = ref
        for t in b.items:
            t["inputs"] = [replacement if i == f["id"] else i for i in t["inputs"]]
        target = b.get(replacement)
        if "output" in f and target is not None:
            target["output"] = f["output"]
            for k in ("_inserts",):
                if k in f:
                    target[k] = f[k]
        b.items.remove(f)


def _scd_type_2(b: Builder) -> None:
    """An UPDATE (or MERGE) that end-dates rows of a table, followed in the same unit by an
    INSERT into that table, is a type 2 slowly changing dimension. Alone it is a candidate."""
    for i, t in enumerate(b.items):
        target = t.get("_end_dating")
        if not target:
            continue
        if any(later.get("_inserts") == target for later in b.items[i + 1 :]):
            t["primitive"] = "apply_scd_type_2"
            t.pop("candidates", None)
        elif "primitive" not in t:
            t["candidates"] = sorted({*t.get("candidates", []), "apply_scd_type_2"})


# ------------------------------------------------------------------ the column effect


_BOOL_LITERALS = ({0, 1}, {"y", "n"}, {"yes", "no"}, {"true", "false"}, {"t", "f"}, {True, False})
# The order in which a SELECT list's column effects are written, one transform per primitive.
PROJECT_ORDER = ("safe_cast", "parse_timestamp", "trim_strings", "normalize_case", "apply_defaults", "coalesce_columns", "map_codes", "conditional_flag", "derive_column", None)


def is_flag_pair(values: list[Any]) -> bool:
    """Two literal results that read as true and false (1/0, 'Y'/'N', true/false)."""
    norm = {v.lower() if isinstance(v, str) else v for v in values}
    return len(values) == 2 and len(norm) == 2 and any(norm == s for s in _BOOL_LITERALS)


def column_item(target: str | None, sources: Iterable[str], primitive: str | None, candidates: Iterable[str] = (), single: str | None = None) -> dict[str, Any]:
    """One SELECT-list entry, assignment or withColumn: its column effect and the primitive.

    A value computed from one column `single` replaces it when written back under its own
    name; any other value replaces the target when it reads it, else adds it."""
    sources = sorted(set(sources))
    if single is not None:
        effect = "replace" if target == single else "add"
    else:
        effect = "replace" if target in sources else "add"
    return {"target": target, "effect": effect, "primitive": primitive, "candidates": sorted(set(candidates))}


def keep_item(source: str, target: str | None = None) -> dict[str, Any]:
    if target and target != source:
        return {"target": target, "source": source, "effect": "rename", "primitive": "rename_columns", "candidates": []}
    return {"target": source, "effect": "keep", "primitive": None, "candidates": []}


def emit_projection(b: Builder, at: list[int], ref: str, items: list[dict[str, Any]], keep_only: bool) -> str:
    """Project transforms for `items` in PROJECT_ORDER, then renames, then (when the list names
    every column it keeps) one `select_columns`. Returns the last transform's id, or `ref`."""
    groups: dict[tuple[str | None, tuple[str, ...]], dict[str, list[str]]] = {}
    renames: dict[str, str] = {}
    for item in items:
        if item["effect"] == "keep" or not item.get("target"):
            continue
        if item["effect"] == "rename":
            renames[item["source"]] = item["target"]
            continue
        cols = groups.setdefault((item["primitive"], tuple(item["candidates"])), {})
        cols.setdefault(item["effect"], [])
        if item["target"] not in cols[item["effect"]]:
            cols[item["effect"]].append(item["target"])
    order = {p: i for i, p in enumerate(PROJECT_ORDER)}
    for (primitive, candidates), cols in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], len(order)), kv[0][1])):
        ref = b.add("project", at, [ref], columns={k: v for k, v in sorted(cols.items())}, primitive=primitive, candidates=list(candidates) if primitive is None else None)
    if renames:
        ref = b.add("project", at, [ref], columns={"rename": renames}, primitive="rename_columns")
    if keep_only:
        ref = b.add("project", at, [ref], primitive="select_columns")
    return ref


def columns_effect(keep: list[str] | None = None, drop: list[str] | None = None, rename: Mapping[str, str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """KEEP, DROP and RENAME as (primitive, fields) pairs, in the order SAS applies them."""
    out: list[tuple[str, dict[str, Any]]] = []
    if drop:
        out.append(("drop_columns", {"columns": {"drop": list(drop)}}))
    if keep:
        out.append(("select_columns", {}))
    if rename:
        out.append(("rename_columns", {"columns": {"rename": dict(rename)}}))
    return out


# ------------------------------------------------------------------ SQL


_WRITE_NAMES = re.compile(r"(?:^|_)(end|to|thru|until|expir\w*|valid_to|effective_to|end_dt|end_date|end_ts)(?:_|$)", re.I)
_FLAG_NAMES = re.compile(r"^(?:is_)?(?:current|curr|active|latest)(?:_flag|_ind|_row|_record)?$", re.I)
_TIMESTAMP_FUNCTIONS = frozenset({"to_date", "to_timestamp", "parse_date", "parse_timestamp", "str_to_date", "try_to_date", "try_to_timestamp", "date_parse", "to_datetime", "strptime", "parse_datetime"})
_TRIM_FUNCTIONS = frozenset({"ltrim", "rtrim", "btrim", "strip"})
_CASE_FUNCTIONS = frozenset({"initcap", "proper", "upcase", "lowcase", "propcase", "ucase", "lcase"})
_RUNNING = re.compile(r"^(rows|range)\s+between\s+unbounded\s+preceding\s+and\s+current\s+row$", re.I)


def _exp():
    from sqlglot import exp

    return exp


def parse_sql(text: str, dialect: str | None = None) -> Any:
    """The sqlglot tree of one statement; None when it does not parse as one we read."""
    import sqlglot

    exp = _exp()
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except Exception:  # sqlglot errors, and RecursionError on hostile input
        return None
    if tree is None or isinstance(tree, exp.Command):
        return None
    return tree


def _ident(node: Any) -> str:
    return (node.name if node is not None else "").lower()


def _flatten_and(node: Any) -> list[Any]:
    exp = _exp()
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _flatten_and(node.this) if isinstance(node.this, exp.And) else [node]
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    return [node]


def _int(node: Any) -> int | None:
    exp = _exp()
    if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"\d+", node.this or ""):
        return int(node.this)
    return None


def _literal(node: Any) -> tuple[bool, Any]:
    exp = _exp()
    if isinstance(node, exp.Literal):
        if node.is_string:
            return True, node.this
        try:
            return True, int(node.this)
        except ValueError:
            return True, node.this
    if isinstance(node, exp.Boolean):
        return True, bool(node.this)
    if isinstance(node, exp.Null):
        return True, None
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return True, "-" + str(node.this.this)
    return False, None


def _fn_name(node: Any) -> str:
    exp = _exp()
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    try:
        return node.sql_name().lower()
    except Exception:
        return type(node).__name__.lower()


def rank_test(node: Any) -> tuple[str, int] | None:
    """`rn = 1`, `rn <= 3`, `rn < 4` (either way round) as (column, n)."""
    exp = _exp()
    if isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, (exp.EQ, exp.LTE, exp.LT, exp.GTE, exp.GT)):
        return None
    left, right = node.left, node.right
    flipped = not isinstance(left, exp.Column)
    if flipped:
        left, right = right, left
    n = _int(right)
    if not isinstance(left, exp.Column) or n is None:
        return None
    op = type(node)
    if flipped:
        op = {exp.LTE: exp.GTE, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.GT: exp.LT}.get(op, op)
    if op is exp.EQ and n == 1:
        return _ident(left), 1
    if op is exp.LTE and n >= 1:
        return _ident(left), n
    if op is exp.LT and n >= 2:
        return _ident(left), n - 1
    return None


def _order(node: Any) -> list[dict[str, Any]]:
    exp = _exp()
    out = []
    for o in (node.expressions if node is not None else []):
        col = o.this if isinstance(o, exp.Ordered) else o
        name = _ident(col) if isinstance(col, exp.Column) else col.sql().lower()
        out.append({"column": name, "desc": bool(isinstance(o, exp.Ordered) and o.args.get("desc"))})
    return out


def _cols(nodes: Iterable[Any]) -> list[str]:
    exp = _exp()
    return [_ident(n) if isinstance(n, exp.Column) else n.sql().lower() for n in nodes]


def classify_sql_value(node: Any, target: str | None) -> dict[str, Any]:
    """The column effect and primitive of one computed SELECT-list value written as `target`."""
    exp = _exp()
    sources = [_ident(c) for c in node.find_all(exp.Column)]

    def col(n: Any) -> str | None:
        return _ident(n) if isinstance(n, exp.Column) else None

    if isinstance(node, exp.Paren):
        node = node.this
    args = [node.this, *node.expressions] if isinstance(node, (exp.Anonymous, exp.Coalesce)) and not isinstance(node, exp.Anonymous) else list(node.expressions) if isinstance(node, exp.Anonymous) else []
    if isinstance(node, (exp.TryCast,)) or (isinstance(node, exp.Cast) and node.args.get("safe")):
        if col(node.this):
            return column_item(target, sources, "safe_cast", single=col(node.this))
    if isinstance(node, exp.Cast) and col(node.this):
        return column_item(target, sources, None, ["safe_cast"], single=col(node.this))
    if isinstance(node, (exp.StrToTime, exp.StrToDate, exp.TsOrDsToDate, exp.TsOrDsToTimestamp)) and col(node.this):
        return column_item(target, sources, "parse_timestamp", single=col(node.this))
    if isinstance(node, exp.Anonymous) and _fn_name(node) in _TIMESTAMP_FUNCTIONS and args and col(args[0]):
        return column_item(target, sources, "parse_timestamp", single=col(args[0]))
    if isinstance(node, exp.Trim) and col(node.this):
        return column_item(target, sources, "trim_strings", single=col(node.this))
    if isinstance(node, exp.Anonymous) and _fn_name(node) in _TRIM_FUNCTIONS and args and col(args[0]):
        return column_item(target, sources, "trim_strings", single=col(args[0]))
    if isinstance(node, (exp.Upper, exp.Lower, exp.Initcap)) and col(node.this):
        return column_item(target, sources, "normalize_case", single=col(node.this))
    if isinstance(node, exp.Anonymous) and _fn_name(node) in _CASE_FUNCTIONS and args and col(args[0]):
        return column_item(target, sources, "normalize_case", single=col(args[0]))
    if isinstance(node, exp.Coalesce):
        values = [node.this, *node.expressions]
        if col(values[0]) and len(values) >= 2 and all(_literal(v)[0] for v in values[1:]):
            return column_item(target, sources, "apply_defaults", single=col(values[0]))
        if len(values) >= 2 and all(col(v) for v in values):
            return column_item(target, sources, "coalesce_columns")
    if isinstance(node, exp.Anonymous) and _fn_name(node) in ("put", "putc", "putn") and len(args) == 2 and col(args[0]) and _literal(args[1])[0] and _user_format(str(_literal(args[1])[1])):
        return column_item(target, sources, "map_codes", single=col(args[0]))
    if isinstance(node, exp.Case):
        return _classify_case(node, target, sources)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.In, exp.Is, exp.Like, exp.Between)):
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")


def _user_format(fmt: str) -> bool:
    """A SAS format name the catalogue does not list: one PROC FORMAT defines (a code list)."""
    from repo_intel.platforms.sas.catalog import BUILTIN_FORMATS

    m = re.match(r"\$?([A-Za-z_][A-Za-z0-9_]*?)\d*\.\d*$", fmt.strip())
    return bool(m) and m.group(1).lower() not in BUILTIN_FORMATS and f"${m.group(1).lower()}" not in BUILTIN_FORMATS


def _classify_case(node: Any, target: str | None, sources: list[str]) -> dict[str, Any]:
    exp = _exp()
    ifs = node.args.get("ifs") or []
    default = node.args.get("default")
    results = [i.args.get("true") for i in ifs] + ([default] if default is not None else [])
    literal_results = all(_literal(r)[0] for r in results)
    subject = node.this
    if isinstance(subject, exp.Column):
        if literal_results and all(_literal(i.this)[0] for i in ifs):
            return column_item(target, sources, "map_codes", single=_ident(subject))
    else:
        tested = set()
        for i in ifs:
            c = i.this
            if isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and _literal(c.right)[0]:
                tested.add(_ident(c.left))
            elif isinstance(c, exp.In) and isinstance(c.this, exp.Column) and all(_literal(v)[0] for v in c.expressions):
                tested.add(_ident(c.this))
            else:
                tested.add(None)
        if literal_results and len(tested) == 1 and None not in tested and len(ifs) >= 2:
            return column_item(target, sources, "map_codes", single=tested.pop())
    if literal_results and is_flag_pair([_literal(r)[1] for r in results]) and len(results) == 2:
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")


def _end_dating(sets: list[tuple[str, Any]]) -> bool:
    """SET valid_to = CURRENT_DATE, is_current = 0, ...: a row being closed, not changed."""
    exp = _exp()
    for name, value in sets:
        if _WRITE_NAMES.search(name) and not isinstance(value, exp.Null):
            return True
        ok, lit = _literal(value)
        if _FLAG_NAMES.match(name) and ok and (lit in (0, False) or (isinstance(lit, str) and lit.lower() in ("n", "no", "false", "f", "0"))):
            return True
    return False


class SqlRecognizer:
    """Transforms of SQL statements into one Builder. `qualify` maps a table name as written
    (lower-case, schema kept) to the name the unit uses (SAS: a one-level name is WORK)."""

    def __init__(self, builder: Builder, qualify: Any = None):
        self.b = builder
        self.qualify = qualify or (lambda n: n)
        self.ctes: dict[str, str] = {}

    # -- names

    def table(self, t: Any) -> str:
        parts = [t.text("catalog"), t.text("db"), t.name]
        name = ".".join(p for p in parts if p).lower()
        return self.ctes.get(name) or self.qualify(name)

    def target(self, node: Any) -> str | None:
        exp = _exp()
        if isinstance(node, exp.Schema):
            node = node.this
        return self.table(node) if isinstance(node, exp.Table) and node.name else None

    # -- statements

    def statement(self, tree: Any, at: list[int]) -> str | None:
        """Add `tree`'s transforms; the id or table name of its result (for a query), else None."""
        exp = _exp()
        b = self.b
        if isinstance(tree, exp.Insert):
            target = self.target(tree.this)
            src = tree.expression
            if target is None:
                return None
            if src is None or isinstance(src, exp.Values) or (isinstance(src, exp.Tuple)):
                b.add("publish", at, [], output=target, _inserts=target)
                return None
            b.write(self.query(_name_by_columns(src, tree.this), at), target, at, _inserts=target)
            return None
        if isinstance(tree, exp.Create):
            if tree.expression is None or (tree.kind or "").upper() not in ("TABLE", "VIEW"):
                return None  # DDL moves no data
            target = self.target(tree.this)
            ref = self.query(tree.expression, at)
            if target is not None:
                b.write(ref, target, at, _inserts=target)
            return None
        if isinstance(tree, exp.Merge):
            return self.merge(tree, at)
        if isinstance(tree, exp.Update):
            return self.update(tree, at)
        if isinstance(tree, exp.Delete):
            return self.delete(tree, at)
        if isinstance(tree, exp.TruncateTable):
            for t in tree.expressions:
                if isinstance(t, exp.Table):
                    b.add("platform", at, [], output=self.table(t), construct="sql:truncate")
            return None
        if isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)):
            into = tree.args.get("into") if isinstance(tree, exp.Select) else None
            if isinstance(into, exp.Into) and isinstance(into.this, exp.Table):
                target = self.table(into.this)
                tree = tree.copy()
                tree.set("into", None)
                b.write(self.query(tree, at), target, at, _inserts=target)
                return None
            return self.query(tree, at)
        return None

    def merge(self, tree: Any, at: list[int]) -> str | None:
        exp = _exp()
        target = self.target(tree.this)
        if target is None:
            return None
        using = tree.args.get("using")
        source = self.source(using, at)
        aliases_t = {_ident(tree.this.args.get("alias")) or tree.this.name.lower()}
        pairs = _equi_pairs(tree.args.get("on"), aliases_t, None) or []
        whens = (tree.args.get("whens").expressions if tree.args.get("whens") is not None else []) or tree.args.get("expressions") or []
        update = insert = delete = False
        sets: list[tuple[str, Any]] = []
        for w in whens:
            then = w.args.get("then")
            text = then.sql().upper() if then is not None else ""
            if isinstance(then, exp.Update) or text.startswith("UPDATE"):
                update = True
                for e in (then.expressions if isinstance(then, exp.Update) else []):
                    if isinstance(e, exp.EQ) and isinstance(e.left, exp.Column):
                        sets.append((_ident(e.left), e.right))
            elif isinstance(then, exp.Insert) or text.startswith("INSERT"):
                insert = True
            elif text.startswith("DELETE"):
                delete = True
        fields: dict[str, Any] = {"keys": [p[0] for p in pairs], "output": target}
        if update and insert:
            fields["primitive"] = "merge_into"
        elif delete and not insert and not update:
            fields["primitive"] = "apply_deletes"
        else:
            fields["candidates"] = ["merge_into"] + (["apply_deletes"] if delete else [])
        if sets:
            fields["columns"] = {"replace": sorted({n for n, _ in sets})}
        self.b.add("publish", at, [source], _end_dating=target if _end_dating(sets) else None, **fields)
        return None

    def update(self, tree: Any, at: list[int]) -> str | None:
        exp = _exp()
        target = self.target(tree.this)
        if target is None:
            return None
        inputs = [target]
        frm = tree.args.get("from_") or tree.args.get("from")
        sources = [frm.this] if frm is not None else []
        sources += [j.this for j in (tree.args.get("joins") or []) + (frm.this.args.get("joins") or [] if frm is not None else [])]
        for node in sources:
            ref = self.source(node, at)
            if ref not in inputs:
                inputs.append(ref)
        sets: list[tuple[str, Any]] = []
        items = []
        for e in tree.expressions:
            if isinstance(e, exp.EQ) and isinstance(e.left, exp.Column):
                name = _ident(e.left)
                sets.append((name, e.right))
                items.append(classify_sql_value(e.right, name) if not isinstance(e.right, exp.Column) else column_item(name, [_ident(e.right)], "derive_column"))
        where = tree.args.get("where")
        primitives = {i["primitive"] for i in items}
        fields: dict[str, Any] = {"output": target, "columns": {"replace": sorted({n for n, _ in sets})}}
        if where is not None:
            fields["predicate"] = {"sql": where.this.sql()}
        if len(primitives) == 1 and None not in primitives and where is None and frm is None:
            fields["primitive"] = primitives.pop()
        else:
            cands = {p for i in items for p in ([i["primitive"]] if i["primitive"] else i["candidates"])}
            if cands:
                fields["candidates"] = sorted(cands)
        self.b.add("project", at, inputs, _end_dating=target if _end_dating(sets) else None, **fields)
        return None

    def delete(self, tree: Any, at: list[int]) -> str | None:
        exp = _exp()
        target = self.target(tree.this)
        if target is None:
            return None
        where = tree.args.get("where")
        if where is None:
            self.b.add("platform", at, [target], output=target, construct="sql:delete_all")
            return None
        cond = where.this
        sub = cond.find(exp.Select)
        fields: dict[str, Any] = {"predicate": {"sql": f"NOT ({cond.sql()})"}, "output": target}
        inputs = [target]
        if sub is not None:
            frm = sub.args.get("from_") or sub.args.get("from")
            if frm is not None and isinstance(frm.this, exp.Table):
                inputs.append(self.table(frm.this))
            fields["candidates"] = ["apply_deletes"]
        self.b.add("filter", at, inputs, **fields)
        return None

    # -- queries

    def query(self, node: Any, at: list[int]) -> str:
        exp = _exp()
        b = self.b
        if isinstance(node, exp.Subquery):
            return self.query(node.this, at)
        if isinstance(node, exp.Paren):
            return self.query(node.this, at)
        if isinstance(node, exp.Union):
            left, right = self.query(node.left, at), self.query(node.right, at)
            by_name = bool(node.args.get("by_name"))
            ref = b.add("union", at, [left, right], primitive="union_by_name" if by_name else None)
            if node.args.get("distinct"):
                ref = b.add("deduplicate", at, [ref], candidates=["dedupe_by_hash"])
            return ref
        if isinstance(node, (exp.Except, exp.Intersect)):
            left, right = self.query(node.left, at), self.query(node.right, at)
            kind = "anti" if isinstance(node, exp.Except) else "semi"
            return b.add("join", at, [left, right], join={"type": kind}, candidates=["join"])
        if isinstance(node, exp.Select):
            return self.select(node, at)
        if isinstance(node, exp.Table):
            return self.source(node, at)
        if isinstance(node, exp.Values):
            return b.add("platform", at, [], construct="sql:values")
        return b.add("platform", at, [], construct=f"sql:{type(node).__name__.lower()}")

    def source(self, node: Any, at: list[int]) -> str:
        """A FROM or JOIN item: a table name, or the id of the transforms that make it."""
        exp = _exp()
        if node is None:
            return self.b.add("platform", at, [], construct="sql:no_source")
        if isinstance(node, exp.Table) and node.name and not isinstance(node.this, exp.Anonymous):
            ref = self.table(node)
        elif isinstance(node, exp.Table) and isinstance(node.this, exp.Anonymous):
            ref = self.b.add("platform", at, [], construct=f"sql:{_fn_name(node.this)}")
        elif isinstance(node, (exp.Subquery, exp.Select, exp.Union)):
            ref = self.query(node, at)
        else:
            ref = self.b.add("platform", at, [], construct=f"sql:{type(node).__name__.lower()}")
        for pivot in node.args.get("pivots") or []:
            measures = [_measure(m) for m in pivot.expressions]
            fields = [f for f in pivot.args.get("fields") or []]
            keys = _cols([f.this for f in fields if isinstance(f, exp.In)])
            if pivot.args.get("unpivot"):
                ref = self.b.add("unpivot", at, [ref], keys=keys, primitive="unpivot")
            else:
                ref = self.b.add("pivot", at, [ref], keys=keys, measures=[m for m in measures if m], primitive="pivot")
        return ref

    def select(self, node: Any, at: list[int]) -> str:
        exp = _exp()
        b = self.b
        saved = dict(self.ctes)
        with_ = node.args.get("with_") or node.args.get("with")
        for cte in (with_.expressions if with_ is not None else []):
            self.ctes[cte.alias_or_name.lower()] = self.query(cte.this, at)
        frm = node.args.get("from_") or node.args.get("from")
        ref = self.source(frm.this, at) if frm is not None else b.add("platform", at, [], construct="sql:no_source")
        left_aliases = {_alias(frm.this)} if frm is not None else set()
        joins: list[tuple[str, set[str], list[list[str]]]] = []
        for j in node.args.get("joins") or []:
            right = self.source(j.this, at)
            right_alias = {_alias(j.this)}
            kind, side = (j.args.get("kind") or "").upper(), (j.side or "").upper()
            on, using = j.args.get("on"), j.args.get("using")
            if kind in ("SEMI", "ANTI"):
                jtype = kind.lower()
            elif side in ("LEFT", "RIGHT", "FULL"):
                jtype = side.lower()
            elif kind == "CROSS" or (on is None and not using):
                jtype = "cross"
            else:
                jtype = "inner"
            pairs = [[_ident(u), _ident(u)] for u in using] if using else _equi_pairs(on, left_aliases, right_alias) if on is not None else []
            fields: dict[str, Any] = {"join": {"type": jtype, **({"on": pairs} if pairs else {})}}
            if pairs:
                fields["keys"] = list(dict.fromkeys(p[0] for p in pairs))
            if pairs is None:  # a condition that is not only column equalities
                fields["join"] = {"type": jtype}
                fields["predicate"] = {"sql": on.sql()}
                fields["candidates"] = ["join"]
            else:
                fields["primitive"] = "join"
            ref = b.add("join", at, [ref, right], **fields)
            joins.append((ref, right_alias, pairs or []))
            left_aliases |= right_alias

        # WHERE: anti and semi joins first, then what is left is a filter.
        where = node.args.get("where")
        rest = []
        for c in _flatten_and(where.this if where is not None else None):
            used = self._anti_by_null(c, joins) or self._exists(c, ref, at, left_aliases)
            if isinstance(used, str):
                ref = used
            elif not used:
                rest.append(c)
        if rest:
            pred = " AND ".join(c.sql() for c in rest)
            ref = b.add("filter", at, [ref], predicate={"sql": pred}, _rank_test=rank_test(rest[0]) if len(rest) == 1 else None)

        # GROUP BY and aggregates.
        projections = list(node.expressions)
        group = node.args.get("group")
        aggregated = [p for p in projections if _has_agg(p)]
        grouped = group is not None or bool(aggregated)
        if grouped:
            keys_nodes = list(group.expressions) if group is not None else []
            keys = _cols(keys_nodes)
            measures, exact = [], all(isinstance(k, exp.Column) for k in keys_nodes)
            for p in aggregated:
                inner = p.this if isinstance(p, exp.Alias) else p
                if isinstance(inner, exp.AggFunc):
                    m = _measure(inner)
                    if m is not None:
                        if p.alias:
                            m["as"] = p.alias.lower()
                        measures.append(m)
                else:
                    exact = False
                    measures += [m for m in (_measure(a) for a in inner.find_all(exp.AggFunc)) if m]
            ref = b.add("aggregate", at, [ref], keys=keys, measures=measures, primitive="aggregate" if exact else None, candidates=None if exact else ["aggregate"])
            having = node.args.get("having")
            if having is not None:
                ref = b.add("filter", at, [ref], predicate={"sql": having.this.sql()})
            keyset = set(keys)
            extra = [p for p in projections if not _has_agg(p) and not (isinstance(p, exp.Column) and _ident(p) in keyset) and not (isinstance(p, exp.Alias) and isinstance(p.this, exp.Column) and _ident(p.this) in keyset and p.alias.lower() == _ident(p.this))]
            items = [self._item(p) for p in extra if not _has_window(p)]
            ref = emit_projection(b, at, ref, [i for i in items if i], keep_only=False)

        # Window functions, then QUALIFY.
        for p in projections:
            if _has_window(p) and not _has_agg_outside_window(p):
                ref = self._window(p, ref, at)
        qualify = node.args.get("qualify")
        if qualify is not None:
            cond = qualify.this
            inline = next((s for s in (getattr(cond, "left", None), getattr(cond, "right", None)) if isinstance(s, exp.Window)), None)
            if inline is not None:
                ref = self._window(exp.alias_(inline.copy(), "__qualify"), ref, at)
                twin = cond.copy()
                twin.set("this" if cond.left is inline else "expression", exp.column("__qualify"))
                cond = twin
            ref = b.add("filter", at, [ref], predicate={"sql": qualify.this.sql()}, _rank_test=rank_test(cond))

        # The SELECT list's column effect.
        if not grouped:
            star = any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in projections)
            items = [self._item(p) for p in projections if not _has_window(p)]
            ref = emit_projection(b, at, ref, [i for i in items if i], keep_only=not star and bool(projections))

        distinct = node.args.get("distinct")
        if distinct is not None:
            on = distinct.args.get("on")
            if on is not None:
                ref = b.add("deduplicate", at, [ref], keys=_cols(on.expressions if hasattr(on, "expressions") else [on]), order=_order(node.args.get("order")), primitive="dedupe_by_key")
            else:
                ref = b.add("deduplicate", at, [ref], candidates=["dedupe_by_hash"])
        order, limit = node.args.get("order"), node.args.get("limit")
        if order is not None and limit is not None:
            ref = b.add("filter", at, [ref], order=_order(order), primitive="top_n_filter")
        elif order is not None and not (distinct is not None and distinct.args.get("on") is not None):
            ref = b.add("sort", at, [ref], order=_order(order))
        elif limit is not None:
            ref = b.add("filter", at, [ref], predicate={"sql": limit.sql()})
        self.ctes = saved
        return ref

    def _item(self, p: Any) -> dict[str, Any] | None:
        exp = _exp()
        if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)):
            return None
        if isinstance(p, exp.Column):
            return keep_item(_ident(p))
        if isinstance(p, exp.Alias):
            target = p.alias.lower() or None
            if isinstance(p.this, exp.Column):
                return keep_item(_ident(p.this), target)
            return classify_sql_value(p.this, target)
        return None  # an unnamed computed value: no column it can be read back by

    def _window(self, p: Any, ref: str, at: list[int]) -> str:
        exp = _exp()
        alias = p.alias.lower() if isinstance(p, exp.Alias) and p.alias else None
        inner = p.this if isinstance(p, exp.Alias) else p
        w = inner if isinstance(inner, exp.Window) else inner.find(exp.Window)
        fn = w.this
        name = _fn_name(fn)
        keys = _cols(w.args.get("partition_by") or [])
        order = _order(w.args.get("order"))
        spec = w.args.get("spec")
        measure = {"fn": name, **({"column": c} if (c := _arg_column(fn)) else {}), **({"as": alias} if alias else {})}
        fields: dict[str, Any] = {"keys": keys, "order": order, "measures": [measure]}
        hint = None
        if isinstance(inner, exp.Sub) and isinstance(inner.right, exp.Window) and isinstance(fn, exp.Lag) and isinstance(inner.left, exp.Column) and _arg_column(fn) == _ident(inner.left):
            fields["measures"] = [{"fn": "lag", "column": _ident(inner.left), **({"as": alias} if alias else {})}]
            fields["primitive"] = "delta_calc"
        elif inner is not w:
            fields["candidates"] = ["window_function"]
        elif isinstance(fn, (exp.Sum, exp.Count)) and order and (spec is None or _RUNNING.match(spec.sql().strip())):
            fields["primitive"] = "calculate_running_total"
        else:
            fields["primitive"] = "window_function"
            if isinstance(fn, (exp.RowNumber,)) or name in ("row_number", "rank", "dense_rank"):
                hint = {"fn": name, "as": alias}
        return self.b.add("window", at, [ref], _rank=hint, **fields)

    def _anti_by_null(self, c: Any, joins: list) -> bool:
        """`LEFT JOIN b ON a.k = b.k WHERE b.k IS NULL` keeps the rows of a with no match in b."""
        exp = _exp()
        if not (isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and isinstance(c.this, exp.Column)):
            return False
        col, alias = _ident(c.this), _ident(c.this.args.get("table"))
        for jid, right_alias, pairs in reversed(joins):
            item = self.b.get(jid)
            if item and item["join"]["type"] == "left" and alias in right_alias and any(p[1] == col for p in pairs):
                item["join"]["type"] = "anti"
                return True
        return False

    def _exists(self, c: Any, ref: str, at: list[int], outer: set[str]) -> str | bool:
        """EXISTS, NOT EXISTS, IN and NOT IN over a subquery: a semi or anti join."""
        exp = _exp()
        negated = isinstance(c, exp.Not)
        inner = c.this if negated else c
        if isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.Exists):
            sub = inner.this
            if not isinstance(sub, exp.Select):
                return False
            frm = sub.args.get("from_") or sub.args.get("from")
            if frm is None or not isinstance(frm.this, exp.Table) or sub.args.get("joins"):
                return False
            sub_alias = {_alias(frm.this)}
            conds = _flatten_and(sub.args["where"].this) if sub.args.get("where") is not None else []
            pairs, other = [], []
            for x in conds:
                pair = _equi_pairs(x, outer, sub_alias)
                (pairs.extend(pair) if pair else other.append(x))
            fields: dict[str, Any] = {"join": {"type": "anti" if negated else "semi", **({"on": pairs} if pairs else {})}, "keys": list(dict.fromkeys(p[0] for p in pairs))}
            if pairs and not other:
                fields["primitive"] = "join"
            else:
                fields["candidates"] = ["join"]
                fields["predicate"] = {"sql": c.sql()}
            return self.b.add("join", at, [ref, self.table(frm.this)], **fields)
        if isinstance(inner, exp.In) and inner.args.get("query") is not None and isinstance(inner.this, exp.Column):
            sub = inner.args["query"]
            sub = sub.this if isinstance(sub, exp.Subquery) else sub
            if not isinstance(sub, exp.Select) or len(sub.expressions) != 1 or not isinstance(sub.expressions[0], exp.Column):
                return False
            right = self.query(sub, at)
            pair = [_ident(inner.this), _ident(sub.expressions[0])]
            fields = {"join": {"type": "anti" if negated else "semi", "on": [pair]}, "keys": [pair[0]]}
            # NOT IN differs from an anti join when the subquery returns NULL: a candidate only.
            fields.update({"candidates": ["join"]} if negated else {"primitive": "join"})
            return self.b.add("join", at, [ref, right], **fields)
        return False


def _name_by_columns(query: Any, target: Any) -> Any:
    """`INSERT INTO t (a, b) SELECT x, f(y) ...`: the SELECT list named by the column list, by
    position, so each value has the column it is written to (`x AS a, f(y) AS b`)."""
    exp = _exp()
    columns = [c for c in (target.expressions if isinstance(target, exp.Schema) else [])]
    if not isinstance(query, exp.Select) or not columns or len(columns) != len(query.expressions) or any(isinstance(p, exp.Star) for p in query.expressions):
        return query
    query = query.copy()
    named = []
    for p, c in zip(query.expressions, columns):
        name = c.name if isinstance(c, (exp.Identifier, exp.Column)) else None
        inner = p.this if isinstance(p, exp.Alias) else p
        named.append(exp.alias_(inner, name) if name else p)
    query.set("expressions", named)
    return query


def _alias(node: Any) -> str:
    exp = _exp()
    alias = node.args.get("alias") if node is not None else None
    if alias is not None and _ident(alias.this if isinstance(alias, exp.TableAlias) else alias):
        return _ident(alias.this if isinstance(alias, exp.TableAlias) else alias)
    return node.name.lower() if isinstance(node, exp.Table) else ""


def _equi_pairs(cond: Any, left: set[str] | None, right: set[str] | None) -> list[list[str]] | None:
    """[[left column, right column], ...] when `cond` is only column equalities; None otherwise.
    A pair is oriented by its columns' table qualifiers when they say which side is which."""
    exp = _exp()
    pairs = []
    for c in _flatten_and(cond):
        if isinstance(c, exp.Paren):
            c = c.this
        if not (isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and isinstance(c.right, exp.Column)):
            return None
        a, b_ = c.left, c.right
        ta, tb = _ident(a.args.get("table")), _ident(b_.args.get("table"))
        if (right and ta in right) or (left and tb in left and ta not in left):
            a, b_ = b_, a
        pairs.append([_ident(a), _ident(b_)])
    return pairs or None


def _measure(node: Any) -> dict[str, Any] | None:
    exp = _exp()
    if node is None:
        return None
    if isinstance(node, exp.Alias):
        m = _measure(node.this)
        if m is not None and node.alias:
            m["as"] = node.alias.lower()
        return m
    out: dict[str, Any] = {"fn": _fn_name(node)}
    col = _arg_column(node)
    if col:
        out["column"] = col
    return out


def _arg_column(fn: Any) -> str | None:
    exp = _exp()
    arg = fn.this if not isinstance(fn, exp.Anonymous) else (fn.expressions[0] if fn.expressions else None)
    if isinstance(arg, exp.Distinct):
        arg = arg.expressions[0] if arg.expressions else None
    if isinstance(arg, exp.Star):
        return "*"
    if isinstance(arg, exp.Column):
        return "*" if isinstance(arg.this, exp.Star) else _ident(arg)
    if isinstance(arg, exp.Expression):
        return arg.sql().lower()
    return None


def _has_agg(node: Any) -> bool:
    exp = _exp()
    return any(not a.find_ancestor(exp.Window) for a in node.find_all(exp.AggFunc)) and not isinstance(node, exp.Window)


def _has_window(node: Any) -> bool:
    exp = _exp()
    return node.find(exp.Window) is not None


def _has_agg_outside_window(node: Any) -> bool:
    exp = _exp()
    return any(a.find_ancestor(exp.Window) is None for a in node.find_all(exp.AggFunc))


# SAS PROC SQL as SQL sqlglot reads: SELECT ... INTO :macro_var, CALCULATED, column options
# and dataset options removed; a format argument of PUT or INPUT kept as a quoted name.
_SAS_INTO = re.compile(r"\binto\s*:[^;]*?(?=\bfrom\b)", re.I | re.S)
_SAS_COLUMN_OPTION = re.compile(r"\b(?:format|informat|label|length|transcode)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\$?[A-Za-z_]\w*\.\d*|\$?\d+\.?\d*)", re.I)
_SAS_FORMAT_ARG = re.compile(r"(\b(?:put[nc]?|input[nc]?)\s*\(\s*[^,()]+,\s*)(\$?[A-Za-z_]\w*?\d*\.\d*)(?=\s*\))", re.I)
_SAS_DATASET_OPTIONS = re.compile(r"(?<=[\w}])\s*\(\s*(?:keep|drop|where|rename|obs|firstobs|in|index|compress|sortedby|label|type)\s*=", re.I)


_SAS_NOW = re.compile(r"\b(datetime|today|date|time)\s*\(\s*\)", re.I)  # SAS functions with no argument


def sas_sql_text(sql: str) -> str:
    out = _SAS_INTO.sub(" ", sql)
    out = _SAS_NOW.sub(lambda m: "current_timestamp" if m.group(1).lower() in ("datetime", "time") else "current_date", out)
    out = re.sub(r"\bcalculated\b", " ", out, flags=re.I)
    out = _SAS_COLUMN_OPTION.sub(" ", out)
    out = _SAS_FORMAT_ARG.sub(lambda m: m.group(1) + "'" + m.group(2) + "'", out)
    for m in reversed(list(_SAS_DATASET_OPTIONS.finditer(out))):
        open_at = out.index("(", m.start())
        depth, j = 0, open_at
        while j < len(out):
            depth += {"(": 1, ")": -1}.get(out[j], 0)
            j += 1
            if depth == 0:
                break
        out = out[:open_at] + out[j:]
    return out


_BLOCK_OPEN = re.compile(r"^\s*begin\b(?!\s+(?:tran|transaction|distributed)\b)(?:\s+atomic\b)?", re.I)


def _sas_name(name: str) -> str:
    return name if "." in name else f"work.{name}"


def _placeholders(name: str) -> str:
    """`orders_#region#` (an inline substitution) as `orders_{region}`."""
    return re.sub(r"#([A-Za-z_][\w.]*)#", r"{\1}", name).lower()


def sql_query(b: Builder, sql: str, at: list[int], *, dialect: str | None = None, params: Iterable[Any] = (), qualify: Any = None, remote: str | None = None, sas: bool = False) -> str | None:
    """One SQL statement's transforms into `b`; the id or table of a query's result, else None.

    Text that runs in another database, or that names a table only at run time, gives one
    `platform` transform marked dynamic. Text sqlglot cannot read gives a `platform`
    transform naming the statement keyword, so the unit is still counted as moving data.
    """
    from repo_intel.knowledge.behavior_store import sql_tables

    params = list(params)
    scan = sql_tables(sql, params)
    if remote or scan.get("remote"):
        engine = remote or scan["remote"][0]
        b.add("platform", at, [], construct=f"sql:remote:{engine}", dynamic=True)
        return None
    if scan["dynamic"]:
        q = qualify or (lambda n: n)
        names = [_placeholders(n) for n in scan["dynamic"]]
        out = [q(n) for n in scan["writes"]] + [n for n in names if scan["statement"] in ("insert", "update", "merge", "delete", "create")][:1]
        b.add("platform", at, [*(q(n) for n in scan["reads"]), *(n for n in names if n not in out)], output=out[0] if out else None, construct="sql:dynamic_table", dynamic=True)
        return None
    text = sas_sql_text(sql) if sas else sql
    text = _BLOCK_OPEN.sub("", text, count=1)  # a procedure body's first statement starts with its BEGIN
    tree = parse_sql(text, dialect)
    if tree is None:
        statement = scan.get("statement") or "statement"
        if statement in ("select", "insert", "update", "delete", "merge", "create", "replace", "upsert"):
            q = qualify or (lambda n: n)
            b.add("platform", at, [q(n) for n in scan["reads"]], output=q(scan["writes"][0]) if scan["writes"] else None, construct=f"sql:unparsed_{statement}")
        return None
    return SqlRecognizer(b, qualify).statement(tree, at)


def sql_unit(text: str, statements: Iterable[tuple[int, int, list[int]]], dialect: str | None = None) -> list[dict[str, Any]]:
    """Transforms of the statements of one stand-alone SQL program: `statements` are
    (start, end) character offsets into `text` with the byte span to cite for each."""
    b = Builder()
    for a, e, at in statements:
        sql_query(b, text[a:e], at, dialect=dialect)
    return b.finish()


# ------------------------------------------------------------------ from stored behaviour


def from_behavior(unit: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
    """The transform shapes of one stored behaviour unit: (language, transforms), or None when
    the unit moves no data (it runs no query, and is not a SAS step that writes a dataset)."""
    from repo_intel.compile.behavior import walk

    body = unit.get("body") or []
    sas = unit.get("language") == "sas"
    if sas and unit.get("kind") == "data_step":
        loop = next((n for n in body if n.get("op") == "loop" and n.get("kind") == "implicit"), None)
        out = _SasStep(loop).run() if loop is not None else None
        return ("sas", out) if out else None
    if sas and unit.get("kind") == "proc_step":
        node = next((n for n in body if n.get("op") == "call"), None)
        out = _sas_proc(node) if node is not None else None
        return ("sas", out) if out else None
    b = Builder()
    for node in walk(body):
        if node.get("op") == "query" and isinstance(node.get("sql"), str):
            at = node.get("at") or unit.get("at") or [0, 0]
            sql_query(b, node["sql"], at, params=node.get("params") or [], qualify=_sas_name if sas else None, remote=node.get("remote") if isinstance(node.get("remote"), str) else None, sas=sas)
        elif sas and node.get("op") == "platform" and node.get("construct") == "sas:sql_passthrough" and (node.get("detail") or {}).get("action") == "execute":
            b.add("platform", node.get("at") or [0, 0], [], construct="sas:sql_passthrough", dynamic=True)
    out = b.finish()
    return ("sql", out) if out else None


# ------------------------------------------------------------------ SAS


def _ds_name(expr: Any) -> str | None:
    """A dataset name from the lowering: a name node, or a template whose dynamic parts keep
    their `{placeholder}`."""
    if not isinstance(expr, dict):
        return None
    if expr.get("e") == "name" and isinstance(expr.get("name"), str):
        return expr["name"]
    if expr.get("e") == "template":
        parts = []
        for p in expr.get("parts", []):
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("name"), str):
                parts.append("{" + p["name"].lower() + "}")
            else:
                return None
        return "".join(parts).lower()
    if expr.get("e") == "lit" and isinstance(expr.get("value"), str):
        return expr["value"].lower()
    return None


def _var(node: Any) -> str | None:
    return node["name"] if isinstance(node, dict) and node.get("e") == "var" and isinstance(node.get("name"), str) else None


def _lit(node: Any) -> tuple[bool, Any]:
    if isinstance(node, dict) and node.get("e") == "lit":
        return True, node.get("value")
    if isinstance(node, dict) and node.get("e") == "unop" and node.get("op") == "-" and isinstance(node.get("operand"), dict) and node["operand"].get("e") == "lit":
        return True, node["operand"].get("value")
    return False, None


def _callee(node: Any) -> str | None:
    if isinstance(node, dict) and node.get("e") == "call" and isinstance(node.get("callee"), dict) and node["callee"].get("e") == "name":
        return str(node["callee"]["name"]).lower()
    return None


def _vars_in(node: Any) -> set[str]:
    from repo_intel.compile.behavior import _walk_expr

    return {n["name"] for n in _walk_expr(node) if n.get("e") == "var" and isinstance(n.get("name"), str)} if isinstance(node, dict) else set()


def _first_last(node: Any) -> tuple[str, str] | None:
    if isinstance(node, dict) and node.get("e") == "platform" and node.get("construct") == "sas:first_last":
        return node.get("which"), node.get("var")
    return None


_SAS_DATE_INFORMATS = re.compile(r"^(date|datetime|time|yymmdd|ddmmyy|mmddyy|anydt\w*|e8601\w*|b8601\w*|is8601\w*|ymddttm|nldate\w*|monyy|yymm|julian|ddmmyyb|yyq)\d*\.\d*$", re.I)


def classify_sas_value(value: Any, target: str) -> dict[str, Any]:
    """The column effect and primitive of a DATA-step assignment `target = value`."""
    sources = _vars_in(value)
    fn = _callee(value)
    args = value.get("args", []) if fn else []
    first = _var(args[0]) if args else None
    if _var(value):
        return column_item(target, sources, "derive_column")
    if fn in ("upcase", "lowcase", "propcase") and first and len(args) == 1:
        return column_item(target, sources, "normalize_case", single=first)
    if fn in ("strip", "trim", "trimn", "left") and first and len(args) == 1:
        return column_item(target, sources, "trim_strings", single=first)
    if fn in ("put", "putc", "putn") and first and len(args) == 2 and _lit(args[1])[0] and _user_format(str(_lit(args[1])[1])):
        return column_item(target, sources, "map_codes", single=first)
    if fn in ("input", "inputn", "inputc") and first and len(args) == 2 and _lit(args[1])[0]:
        fmt = str(_lit(args[1])[1])
        if _SAS_DATE_INFORMATS.match(fmt.lstrip("$")):
            return column_item(target, sources, "parse_timestamp", single=first)
        return column_item(target, sources, None, ["safe_cast"], single=first)
    if fn in ("coalesce", "coalescec") and first and len(args) >= 2:
        if all(_lit(a)[0] for a in args[1:]):
            return column_item(target, sources, "apply_defaults", single=first)
        if all(_var(a) for a in args):
            return column_item(target, sources, "coalesce_columns")
    if fn == "dif" and first and len(args) == 1:
        return {"window": True, "measure": {"fn": "dif", "column": first, "as": target}, "primitive": "delta_calc"}
    if isinstance(value, dict) and value.get("e") == "binop" and value.get("op") == "-" and _var(value.get("left")) and _callee(value.get("right")) == "lag" and [_var(a) for a in value["right"].get("args", [])][:1] == [_var(value["left"])]:
        return {"window": True, "measure": {"fn": "lag", "column": _var(value["left"]), "as": target}, "primitive": "delta_calc"}
    if isinstance(value, dict) and value.get("e") in ("binop", "unop") and value.get("op") in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "not", "in"):
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")


def _eval_flags(node: Any, flags: dict[str, bool]) -> bool | None:
    """Evaluate a condition over IN= flag variables; None when it reads anything else."""
    if not isinstance(node, dict):
        return None
    e = node.get("e")
    if e == "var" and node.get("name") in flags:
        return flags[node["name"]]
    if e == "lit" and node.get("value") in (0, 1):
        return bool(node["value"])
    if e == "unop" and node.get("op") == "not":
        v = _eval_flags(node.get("operand"), flags)
        return None if v is None else not v
    if e == "binop" and node.get("op") in ("and", "or", "==", "!="):
        a, b = _eval_flags(node.get("left"), flags), _eval_flags(node.get("right"), flags)
        if a is None or b is None:
            return None
        return {"and": a and b, "or": a or b, "==": a == b, "!=": a != b}[node["op"]]
    return None


class _SasStep:
    """Transform shapes of one DATA step, read from its lowering (repo_intel.platforms.sas.lower)."""

    def __init__(self, loop: Mapping[str, Any]):
        self.loop = loop
        self.at = loop.get("at") or [0, 0]
        self.detail = loop.get("detail") or {}
        self.body = loop.get("body") or []
        self.b = Builder()
        self.used: set[int] = set()  # statements an idiom accounted for

    def run(self) -> list[dict[str, Any]] | None:
        outputs = [n for n in self.detail.get("data", []) if isinstance(n, str)]
        if not outputs:
            return None  # DATA _NULL_: writes no dataset
        b = self.b
        reads = [n for n in self.body if n.get("op") == "platform" and n.get("construct") in ("sas:set", "sas:merge", "sas:update", "sas:modify")]
        by_node = next((n for n in self.body if n.get("op") == "platform" and n.get("construct") == "sas:by_group"), None)
        self.by = list((by_node or {}).get("detail", {}).get("variables", []))
        self.descending = set((by_node or {}).get("detail", {}).get("descending", []))
        self.ifs_on_flags: list[dict] = []
        ref = self.source(reads)
        if ref is None:
            return None
        ref = self.statements(ref)
        # KEEP, DROP and RENAME statements, then the options of the datasets written.
        for primitive, fields in columns_effect(self.detail.get("keep"), self.detail.get("drop"), self.detail.get("rename")):
            ref = b.add("project", self.at, [ref], primitive=primitive, **fields)
        options = self.detail.get("options", {})
        written = [o for o in outputs if not self.split_outputs or o not in self.split_outputs]
        for i, name in enumerate(written):
            r = ref
            for primitive, fields in columns_effect(**{k: options.get(name, {}).get(k) for k in ("keep", "drop", "rename")}):
                r = b.add("project", self.at, [r], primitive=primitive, **fields)
            b.write(r, name, self.at)
        return b.finish()

    # -- reading

    def _read(self, name: str, options: Mapping[str, Any], at: list[int]) -> str:
        """A dataset as read, with its KEEP=, DROP=, RENAME= and WHERE= options applied."""
        ref = name
        if options.get("where"):
            ref = self.b.add("filter", at, [ref], predicate={"sql": options["where"]})
        for primitive, fields in columns_effect(options.get("keep"), options.get("drop"), options.get("rename")):
            ref = self.b.add("project", at, [ref], primitive=primitive, **fields)
        return ref

    def source(self, reads: list[dict]) -> str | None:
        b = self.b
        self.split_outputs: set[str] = set()
        if not reads:
            construct = next((n["construct"] for n in self.body if n.get("op") == "platform" and n.get("construct") in ("sas:infile_input", "sas:datalines")), "sas:data_step")
            return b.add("platform", self.at, [], construct=construct)
        if len(reads) > 1:
            return b.add("platform", self.at, [d for n in reads for d in n.get("detail", {}).get("datasets", [])], construct="sas:multiple_reads")
        node = reads[0]
        self.used.add(id(node))
        at = node.get("at") or self.at
        datasets = list(node.get("detail", {}).get("datasets", []))
        options = node.get("detail", {}).get("options", {})
        if not datasets:
            return b.add("platform", at, [], construct=node["construct"])
        refs = [self._read(d, options.get(d, {}), at) for d in datasets]
        construct = node["construct"]
        if construct == "sas:set":
            if len(refs) == 1:
                return refs[0]
            # SET a b: concatenation, columns matched by name (interleaved when BY is given).
            return b.add("union", at, refs, primitive="union_by_name", keys=self.by)
        if construct == "sas:update" and len(refs) == 2 and self.by:
            # UPDATE master transactions; BY k: matching rows updated, new ones added.
            return b.add("publish", at, [refs[1], refs[0]], keys=self.by, primitive="merge_into")
        if construct == "sas:modify":
            return b.add("publish", at, refs, keys=self.by, candidates=["merge_into"])
        if construct == "sas:merge":
            if not self.by:
                return b.add("platform", at, refs, construct="sas:merge_positional")
            if len(refs) != 2:
                return b.add("join", at, refs, keys=self.by, join={"type": "full", "on": [[k, k] for k in self.by]}, candidates=["join"])
            flags = {options.get(d, {}).get("in"): i for i, d in enumerate(datasets) if options.get(d, {}).get("in")}
            jtype, predicate = self.join_type(flags)
            fields: dict[str, Any] = {"keys": self.by, "join": {"type": jtype or "full", "on": [[k, k] for k in self.by]}}
            if jtype is None:
                fields.update(candidates=["join"], predicate=predicate)
            else:
                fields["primitive"] = "join"
            if jtype == "anti_right":
                refs = [refs[1], refs[0]]
                fields["join"]["type"] = "anti"
            return b.add("join", at, refs, **fields)
        return b.add("platform", at, refs, construct=construct)

    def join_type(self, flags: dict[str, int]) -> tuple[str | None, Any]:
        """The join a MERGE with IN= flags and a subsetting IF on them makes, read from the rows
        the condition keeps: both (inner), the first (left), the first only (anti), ..."""
        cond_node = None
        for n in self.body:
            if n.get("op") != "if" or not flags:
                continue
            if n.get("subsetting"):
                cond = (n.get("cond") or {}).get("operand")
            elif self._is_output_only(n.get("then")) and "else" not in n and self._outputs() == 1:
                cond = n.get("cond")
            else:
                continue
            if _vars_in(cond) and _vars_in(cond) <= set(flags) and _first_last(cond) is None:
                cond_node = (n, cond)
                break
        if cond_node is None:
            return "full", None  # MERGE ... BY with no condition on the flags keeps every row
        node, cond = cond_node
        names = sorted(flags, key=lambda k: flags[k])
        if len(names) == 1:
            names.append("\x00")  # a dataset without IN=: always unknown to the condition
        kept = set()
        for a in (True, False):
            for b in (True, False):
                if not (a or b):
                    continue
                values = {names[0]: a, names[1]: b} if flags.get(names[0]) == 0 else {names[0]: b, names[1]: a}
                if "\x00" in values and len(flags) == 1:
                    only = next(iter(flags))
                    values = {only: a if flags[only] == 0 else b}
                v = _eval_flags(cond, values)
                if v is None:
                    return None, cond
                if v:
                    kept.add((a, b))
        self.used.add(id(node))
        table = {
            frozenset({(True, True)}): "inner", frozenset({(True, True), (True, False)}): "left",
            frozenset({(True, True), (False, True)}): "right", frozenset({(True, True), (True, False), (False, True)}): "full",
            frozenset({(True, False)}): "anti", frozenset({(False, True)}): "anti_right",
        }
        return table.get(frozenset(kept)), cond

    # -- statements

    def _outputs(self) -> int:
        from repo_intel.compile.behavior import walk

        return sum(1 for n in walk(self.body) if n.get("op") == "output" and n.get("channel") == "dataset")

    @staticmethod
    def _is_output_only(block: Any) -> bool:
        return isinstance(block, list) and len(block) == 1 and block[0].get("op") == "output" and block[0].get("channel") == "dataset"

    def statements(self, ref: str) -> str:
        b = self.b
        retained = {v for n in self.body if n.get("construct") == "sas:retain" for v in n.get("detail", {}).get("variables", [])}
        ref = self.accumulators(ref, retained)
        ref = self.dedupe(ref)
        arrays = {n["detail"]["name"]: n["detail"].get("elements", []) for n in self.body if n.get("construct") == "sas:array" and isinstance(n.get("detail"), dict)}
        items: list[dict[str, Any]] = []
        items_at: list[int] | None = None

        def flush(r: str) -> str:
            nonlocal items, items_at
            if items:
                r = emit_projection(b, items_at or self.at, r, items, keep_only=False)
            items, items_at = [], None
            return r

        for n in self.body:
            if id(n) in self.used:
                continue
            op, at = n.get("op"), n.get("at") or self.at
            if op == "assign" and isinstance(n.get("target"), dict) and _var(n["target"]):
                c = classify_sas_value(n.get("value"), _var(n["target"]))
                if c.get("window"):
                    ref = flush(ref)
                    keys = list(self.by)
                    ref = b.add("window", at, [ref], keys=keys, measures=[c["measure"]], primitive=c["primitive"])
                    continue
                items.append(c)
                items_at = _join_at(items_at, at)
            elif op == "if" and (n.get("subsetting") or n.get("where")):
                ref = flush(ref)
                ref = b.add("filter", at, [ref], predicate=(n.get("cond") or {}).get("operand") or {"sql": ""})
            elif op == "if":
                c = self.conditional(n)
                if c is not None:
                    items.append(c)
                    items_at = _join_at(items_at, at)
                    continue
                split = self.split(n, ref)
                if split:
                    ref = flush(ref)
                    continue
                ref = flush(ref)
                ref = b.add("platform", at, [ref], construct="sas:conditional_logic")
            elif op == "switch":
                target = self._switch_target(n)
                if target:
                    items.append(column_item(target, _vars_in(n.get("on")), "map_codes", single=_var(n.get("on"))))
                    items_at = _join_at(items_at, at)
                else:
                    ref = flush(ref)
                    ref = b.add("platform", at, [ref], construct="sas:select")
            elif op == "loop":
                cols = self.default_loop(n, arrays)
                if cols:
                    for c in cols:
                        items.append(column_item(c, [c], "apply_defaults", single=c))
                    items_at = _join_at(items_at, at)
                else:
                    ref = flush(ref)
                    ref = b.add("platform", at, [ref], construct="sas:do_loop")
            elif op == "platform" and n.get("construct") in ("sas:hash_object", "sas:infile_input", "sas:call_execute", "sas:goto_link"):
                ref = flush(ref)
                ref = b.add("platform", at, [ref], construct=n["construct"])
            elif op == "declare" and n.get("construct") == "sas:hash_object":
                ref = flush(ref)
                ref = b.add("platform", at, [ref], construct="sas:hash_object")
        return flush(ref)

    def accumulators(self, ref: str, retained: set[str]) -> str:
        """RETAIN (or a sum statement) reset at first.k: a total per BY group, written once per
        group at last.k (an aggregate) or on every row (a running total)."""
        acc: dict[str, dict[str, Any]] = {}
        for n in self.body:
            if n.get("op") != "assign" or not _var(n.get("target")):
                continue
            name, value = _var(n["target"]), n.get("value") or {}
            if value.get("e") == "binop" and value.get("op") == "+" and _var(value.get("left")) == name and (n.get("sum") or name in retained):
                right = value.get("right")
                ok, lit = _lit(right)
                measure = {"fn": "count", "as": name} if ok and lit == 1 else {"fn": "sum", "column": _var(right), "as": name} if _var(right) else None
                if measure is not None:
                    acc[name] = {"node": n, "measure": measure, "reset": None}
        if not acc:
            return ref
        last_output = None
        for n in self.body:
            fl = _first_last(n.get("cond")) if n.get("op") == "if" else None
            if fl and fl[0] == "first" and all(m.get("op") == "assign" and _var(m.get("target")) in acc for m in n.get("then", [])) and n.get("then"):
                for m in n["then"]:
                    acc[_var(m["target"])]["reset"] = (n, fl[1])
            if n.get("op") == "if" and not n.get("subsetting") and _first_last(n.get("cond")) and _first_last(n.get("cond"))[0] == "last" and self._is_output_only(n.get("then")) and "else" not in n:
                last_output = (n, _first_last(n["cond"])[1])
            if n.get("op") == "if" and n.get("subsetting") and _first_last((n.get("cond") or {}).get("operand")) and _first_last(n["cond"]["operand"])[0] == "last":
                last_output = (n, _first_last(n["cond"]["operand"])[1])
        resets = {r[1] for r in (a["reset"] for a in acc.values()) if r}
        level = resets.pop() if len(resets) == 1 and all(a["reset"] for a in acc.values()) else None
        span = _join_at(None, *[a["node"].get("at") or self.at for a in acc.values()])
        measures = [acc[k]["measure"] for k in sorted(acc)]
        for a in acc.values():
            self.used.add(id(a["node"]))
            if a["reset"]:
                self.used.add(id(a["reset"][0]))
        if level is not None and level in self.by:
            keys = self.by[: self.by.index(level) + 1]
            if last_output is not None and last_output[1] == level and self._outputs() <= 1:
                self.used.add(id(last_output[0]))
                return self.b.add("aggregate", _join_at(span, last_output[0].get("at")), [ref], keys=keys, measures=measures, primitive="aggregate")
            order = [{"column": c, "desc": c in self.descending} for c in self.by[len(keys):]]
            return self.b.add("window", span, [ref], keys=keys, order=order, measures=measures, primitive="calculate_running_total" if last_output is None else None, candidates=["aggregate", "calculate_running_total"] if last_output is not None else None)
        if not self.by and not any(a["reset"] for a in acc.values()) and last_output is None:
            return self.b.add("window", span, [ref], measures=measures, primitive="calculate_running_total")
        return self.b.add("window", span, [ref], keys=self.by, measures=measures, candidates=["aggregate", "calculate_running_total"])

    def dedupe(self, ref: str) -> str:
        """BY k ...; IF FIRST.k (or LAST.k): one row per key, the first (or last) in BY order."""
        for n in self.body:
            if id(n) in self.used or n.get("op") != "if":
                continue
            if n.get("subsetting"):
                fl = _first_last((n.get("cond") or {}).get("operand"))
            elif self._is_output_only(n.get("then")) and "else" not in n and self._outputs() == 1:
                fl = _first_last(n.get("cond"))
            else:
                continue
            if not fl:
                continue
            self.used.add(id(n))
            which, var = fl
            at = n.get("at") or self.at
            if var not in self.by:
                return self.b.add("deduplicate", at, [ref], keys=[var], candidates=["dedupe_by_key"])
            keys = self.by[: self.by.index(var) + 1]
            rest = self.by[len(keys):]
            desc = lambda c: (c in self.descending) != (which == "last")  # noqa: E731
            return self.b.add("deduplicate", at, [ref], keys=keys, order=[{"column": c, "desc": desc(c)} for c in rest], primitive="dedupe_by_key")
        return ref

    def conditional(self, n: Mapping[str, Any]) -> dict[str, Any] | None:
        """IF ... THEN x = lit; ELSE x = lit as a flag, a code mapping, or a default."""
        chain: list[tuple[Any, Any]] = []  # (condition, assigned literal)
        target = None
        node: Any = n
        while isinstance(node, dict) and node.get("op") == "if":
            then = node.get("then") or []
            if len(then) != 1 or then[0].get("op") != "assign" or not _var(then[0].get("target")):
                return None
            t = _var(then[0]["target"])
            if target not in (None, t):
                return None
            target = t
            chain.append((node.get("cond"), then[0].get("value")))
            other = node.get("else")
            if other is None:
                node = None
                break
            if len(other) == 1 and other[0].get("op") == "if":
                node = other[0]
                continue
            if len(other) == 1 and other[0].get("op") == "assign" and _var(other[0].get("target")) == target:
                chain.append((None, other[0].get("value")))
                node = None
                break
            return None
        if target is None:
            return None
        values = [_lit(v) for _, v in chain]
        sources = set().union(*(_vars_in(c) for c, _ in chain if c is not None))
        # IF MISSING(x) THEN x = 0; (or x = .): a default.
        if len(chain) == 1 and values[0][0]:
            cond = chain[0][0]
            if (_callee(cond) == "missing" and [_var(a) for a in cond.get("args", [])] == [target]) or (isinstance(cond, dict) and cond.get("e") == "unop" and cond.get("op") == "missing" and _var(cond.get("operand")) == target):
                return column_item(target, [target], "apply_defaults", single=target)
        if not all(ok for ok, _ in values):
            return column_item(target, sources, "derive_column")
        if len(chain) == 2 and chain[1][0] is None and is_flag_pair([v for _, v in values]):
            return column_item(target, sources, "conditional_flag")
        tested = set()
        for cond, _ in chain:
            if cond is None:
                continue
            if isinstance(cond, dict) and cond.get("e") == "binop" and cond.get("op") in ("==", "in") and _var(cond.get("left")) and (_lit(cond.get("right"))[0] or (cond.get("right") or {}).get("e") == "array"):
                tested.add(_var(cond["left"]))
            else:
                tested.add(None)
        if len(tested) == 1 and None not in tested and len(chain) >= 2:
            return column_item(target, sources, "map_codes", single=next(iter(tested)))
        return column_item(target, sources, "derive_column")

    def split(self, n: Mapping[str, Any], ref: str) -> bool:
        """IF c THEN OUTPUT a; ELSE OUTPUT b; rows routed to datasets by condition."""
        branches: list[tuple[list[Any], str]] = []  # (conditions that hold, dataset)
        node: Any = n
        negations: list[Any] = []
        while isinstance(node, dict) and node.get("op") == "if":
            then = node.get("then") or []
            if not self._is_output_only(then) or not _ds_name(then[0].get("value")):
                return False
            branches.append(([*negations, node.get("cond")], _ds_name(then[0]["value"])))
            negations.append({"e": "unop", "op": "not", "operand": node.get("cond")})
            other = node.get("else")
            if other is None:
                node = None
                break
            if len(other) == 1 and other[0].get("op") == "if":
                node = other[0]
                continue
            if self._is_output_only(other) and _ds_name(other[0].get("value")):
                branches.append((list(negations), _ds_name(other[0]["value"])))
                node = None
                break
            return False
        if not branches:
            return False
        exact = n.get("else") is not None and len(branches) >= 2
        for conds, name in branches:
            pred = conds[0]
            for c in conds[1:]:
                pred = {"e": "binop", "op": "and", "left": pred, "right": c}
            self.b.add("filter", n.get("at") or self.at, [ref], predicate=pred, output=name, primitive="split_by_condition" if exact else None, candidates=None if exact else ["split_by_condition"])
            self.split_outputs.add(name)
        return True

    def _switch_target(self, n: Mapping[str, Any]) -> str | None:
        if not _var(n.get("on")):
            return None
        target = None
        for case in [*n.get("cases", []), {"body": n.get("default") or []}]:
            body = case.get("body") or []
            if not body:
                continue
            if len(body) != 1 or body[0].get("op") != "assign" or not _lit(body[0].get("value"))[0]:
                return None
            t = _var(body[0].get("target"))
            if target not in (None, t):
                return None
            target = t
        return target

    @staticmethod
    def default_loop(n: Mapping[str, Any], arrays: Mapping[str, list[str]]) -> list[str] | None:
        """DO i = 1 TO DIM(a); IF MISSING(a{i}) THEN a{i} = 0; END; defaults for every element."""
        body = n.get("body") or []
        if len(body) != 1 or body[0].get("op") != "if" or "else" in body[0]:
            return None
        cond, then = body[0].get("cond"), body[0].get("then") or []
        if len(then) != 1 or then[0].get("op") != "assign" or not _lit(then[0].get("value"))[0]:
            return None
        target = then[0].get("target") or {}
        if target.get("e") != "index":
            return None
        array = _var(target.get("object"))
        tested = cond.get("args", [None])[0] if _callee(cond) == "missing" else cond.get("operand") if isinstance(cond, dict) and cond.get("op") == "missing" else None
        if not isinstance(tested, dict) or tested.get("e") != "index" or _var(tested.get("object")) != array or array not in arrays:
            return None
        return list(arrays[array]) or None


def _join_at(a: list[int] | None, *more: Any) -> list[int]:
    spans = [s for s in (a, *more) if isinstance(s, list) and len(s) == 2]
    return [min(s[0] for s in spans), max(s[1] for s in spans)] if spans else [0, 0]


def _sas_proc(node: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Transform shapes of a procedure step, from its call's named datasets and the detail the
    SAS pack keeps (options, BY, CLASS, VAR, ID and OUTPUT statistics)."""
    call = node.get("call") or {}
    name = str((call.get("callee") or {}).get("name") or "").lower()
    named = {k: _ds_name(v) for k, v in (call.get("named") or {}).items()}
    detail = node.get("detail") or {}
    at = node.get("at") or [0, 0]
    data, out = named.get("data"), named.get("out")
    options = set(detail.get("options", []))
    by = detail.get("by", [])
    by_cols = [o["column"] for o in by]
    b = Builder()
    if name == "sort" and data:
        target = out or data
        ref = b.add("sort", at, [data], order=by)
        if "nodupkey" in options:
            ref = b.add("deduplicate", at, [ref], keys=by_cols, primitive="dedupe_by_key")
        elif options & {"nodup", "noduprecs"}:
            ref = b.add("deduplicate", at, [ref], candidates=["dedupe_by_hash"])
        b.write(ref, target, at)
    elif name in ("means", "summary") and data and out:
        keys = by_cols + detail.get("class", [])
        measures, exact = _proc_measures(detail)
        exact = exact and ("nway" in options or not detail.get("class"))
        b.write(b.add("aggregate", at, [data], keys=keys, measures=measures, primitive="aggregate" if exact else None, candidates=None if exact else ["aggregate"]), out, at)
    elif name == "freq" and data and out:
        tables = detail.get("tables", [])
        b.write(b.add("aggregate", at, [data], keys=by_cols + tables, measures=[{"fn": "count", "as": "count"}], primitive="aggregate" if tables else None, candidates=None if tables else ["aggregate"]), out, at)
    elif name == "transpose" and data and out:
        if detail.get("id"):
            ref = b.add("pivot", at, [data], keys=by_cols, measures=[{"fn": "first", "column": c} for c in detail.get("var", [])], primitive="pivot")
        elif by_cols and detail.get("var"):
            ref = b.add("unpivot", at, [data], keys=by_cols, columns={"drop": list(detail["var"])}, primitive="unpivot")
        else:
            ref = b.add("pivot", at, [data], candidates=["pivot", "unpivot"])
        b.write(ref, out, at)
    elif name == "append" and named.get("base") and data:
        b.write(b.add("union", at, [named["base"], data], primitive="union_by_name"), named["base"], at)
    elif name == "rank" and data:
        ranks = detail.get("ranks", [])
        measures = [{"fn": "rank", "column": c, **({"as": ranks[i]} if i < len(ranks) else {})} for i, c in enumerate(detail.get("var", []))]
        b.write(b.add("window", at, [data], keys=by_cols, measures=measures, primitive="window_function"), out or data, at)
    else:
        writes = [v for k, v in sorted(named.items()) if v and k not in ("data",) and k.startswith("out")]
        if not writes:
            return None  # a report or a check: the procedure writes no dataset
        ref = b.add("platform", at, [data] if data else [], construct=f"sas:proc_{name or 'unknown'}")
        b.write(ref, writes[0], at)
    return b.finish()


def _proc_measures(detail: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """OUTPUT OUT= statistics as measures; exact only when every statistic names its result."""
    measures: list[dict[str, Any]] = []
    exact = bool(detail.get("output"))
    variables = detail.get("var", [])
    for output in detail.get("output", []):
        for s in output.get("stats", []):
            cols = s.get("vars") or variables
            names = s.get("names", [])
            if not names and not output.get("autoname"):
                exact = False
            for i, c in enumerate(cols):
                m = {"fn": s["stat"], "column": c}
                if i < len(names):
                    m["as"] = names[i]
                elif output.get("autoname"):
                    m["as"] = f"{c}_{s['stat']}"
                else:
                    exact = False
                measures.append(m)
    return measures, exact
