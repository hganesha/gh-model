"""Business rules lifted from behaviour units (docs/solution-ir-contract.md, 8.2). Off unless
the compile settings turn it on, like DM08.

Two halves, kept apart on purpose:

1. **Candidates are deterministic.** `features.rules`, a pure step, walks every stored
   behaviour unit (compile.behavior) and keeps the conditions and computed values that can
   carry a business rule: a branch that throws, redirects, stops or outputs an error; SAS
   `WHERE`, subsetting `IF`, conditional assignments and computed assignments in a DATA
   step; SQL `WHERE` conjuncts that are not key lookups, and `CASE` expressions. Each
   candidate keeps its span, the unit's evidence and the entities its condition reads
   (a Field, Table, Dataset or Form that already exists). The owner's transform shapes
   (section 8.1, the shard's `transforms` rows) travel with the candidates as context
   when the table is present.
2. **Names come from a model.** `lift` asks an LLM (spec `dm.business-rules`) to group an
   owner's candidates into rules. Egress is denied unless policy names the provider; any
   failure abstains. A rule is kept only if its name is a lower-case slug of at most 60
   characters, its statement at most 280 characters, its kind one of three, and every
   candidate it cites was supplied.

A lifted rule is a hypothesis (`E5`, `inferred`). An owner confirms or rejects it with a
`decisions` override on the rule's entity id (`confirmation`).

How a rule is stored in the ledger: `DecisionResult` has no field for structured items,
so each rule is one `Hypothesis` (its statement and the evidence ids of the candidates it
cites) and the entry at the same index of `alternatives` is `"<rule_kind> <name> <c1,c2>"`.
`rules_of` reads them back. Both lists are written in one order, so replay is exact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Disagreement, Entity, Hypothesis, decision_id
from repo_intel.compile.ontology import EntityKind
from repo_intel.decisions.gateway import EgressDenied, Gateway, ModelUnavailable
from repo_intel.decisions.overrides import CONFIRMATION_LABELS, Overrides
from repo_intel.decisions.runner import TRANSIENT, Budget, Ledger, _policy, verifier
from repo_intel.storage.hashing import canonical, digest

SPEC = DecisionSpec.model_validate({
    "id": "dm.business-rules", "version": "0.1.0", "scope": "owner",
    "input_schema": "RuleCandidates.v1", "output_schema": "BusinessRules.v1",
    "labels": ["rules", "confirmed", "rejected", "none"],
    "feature_sources": ["behaviors", "transforms"],
    "strategy": [{"kind": "llm", "when": "always", "model_ref": "llm-default", "prompt_ref": "prompts/business-rules-v1"}],
    "abstain_required_if_missing": ["candidates"],
    "verification": "vfy.rule-citations-v1",
    "max_input_bytes": 30000,
})

INSTRUCTIONS = (
    "You name the business rules in one unit of code, from candidate conditions and computed values "
    "found in it. Group the candidates that express one rule. For each rule give: name, a lower-case "
    "slug of words joined by hyphens, at most 60 characters; statement, one plain sentence of at most "
    "280 characters saying what the rule requires or computes; rule_kind, one of validation, "
    "calculation or decision; and candidate_ids, chosen only from the ids given. Leave out candidates "
    "that are technical checks rather than business rules. Say nothing you cannot tie to a candidate."
)

RULE_KINDS = ("validation", "calculation", "decision")
MAX_CANDIDATES = 40  # per owner; the rest are counted, not sent
MAX_RULES = 12  # per owner
MAX_TRANSFORMS = 20  # per owner, as context
TEXT_LIMIT = 200  # characters of a rendered condition or effect
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

RULES_SCHEMA = {"type": "object", "required": ["rules"], "properties": {"rules": {"type": "array", "items": {
    "type": "object", "required": ["name", "statement", "rule_kind", "candidate_ids"],
    "properties": {
        "name": {"type": "string", "maxLength": 60}, "statement": {"type": "string", "maxLength": 280},
        "rule_kind": {"type": "string", "enum": list(RULE_KINDS)},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
    }}}}}


@verifier("vfy.rule-citations-v1")
def verify_citations(f: Mapping[str, Any], label: str) -> str:
    return "unchecked"  # each rule's citations are checked in `lift`


def enabled(policy: Any, flag: str = "business_rules") -> bool:
    """Whether the compile settings turn one 8.2 feature on: `business_rules` (rules and
    workflows) or `captured_screens`. Each has its own switch; DM08's `behavior_candidates`
    turns on neither."""
    return policy is not None and bool(getattr(policy, flag, False))


# ------------------------------------------------------------------ rendering the behavioural IR

_QUIET_SCOPES = frozenset({None, "", "pdv", "local", "variables", "work"})


def render(e: Any, limit: int = TEXT_LIMIT) -> str:
    """A behavioural-IR expression as short, deterministic text for a reader or a model."""
    text = _render(e)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _render(e: Any) -> str:
    if not isinstance(e, Mapping):
        return "?"
    kind = e.get("e")
    if kind == "lit":
        value = e.get("value")
        if isinstance(value, str):
            return json.dumps(value if len(value) <= 60 else value[:57] + "...")
        return json.dumps(value)
    if kind == "var":
        scope = e.get("scope")
        return str(e.get("name")) if scope in _QUIET_SCOPES else f"{scope}.{e.get('name')}"
    if kind == "name":
        return str(e.get("name"))
    if kind == "member":
        return f"{_render(e.get('object'))}.{e.get('name')}"
    if kind == "index":
        return f"{_render(e.get('object'))}[{_render(e.get('index'))}]"
    if kind == "call":
        args = ", ".join(_render(a) for a in e.get("args") or [])
        return f"{_render(e.get('callee'))}({args})"
    if kind == "binop":
        return f"{_operand(e.get('left'))} {e.get('op')} {_operand(e.get('right'))}"
    if kind == "unop":
        op = e.get("op")
        return f"{op} {_operand(e.get('operand'))}" if op in ("not", "exists", "missing") else f"{op}{_operand(e.get('operand'))}"
    if kind == "cond":
        return f"{_operand(e.get('test'))} ? {_operand(e.get('then'))} : {_operand(e.get('else'))}"
    if kind == "template":
        parts = [p if isinstance(p, str) else "{" + _render(p) + "}" for p in e.get("parts") or []]
        return json.dumps("".join(parts))
    if kind == "array":
        return "[" + ", ".join(_render(i) for i in e.get("items") or []) + "]"
    if kind == "struct":
        return "{" + ", ".join(f"{k}: {_render(v)}" for k, v in sorted((e.get("fields") or {}).items())) + "}"
    if kind == "new":
        return f"new {e.get('type')}()"
    if kind == "lambda":
        return "<function>"
    if kind == "platform":
        if e.get("construct") == "sas:first_last" and e.get("var"):
            return f"{e.get('which')}.{e.get('var')}"
        return f"<{e.get('construct')}>"
    return "?"


def _operand(e: Any) -> str:
    text = _render(e)
    return f"({text})" if isinstance(e, Mapping) and e.get("e") in ("binop", "cond") else text


def reads(e: Any) -> list[tuple[str | None, str]]:
    """(scope, name) of every variable an expression reads, in first-seen order."""
    out: list[tuple[str | None, str]] = []

    def walk(x: Any) -> None:
        if isinstance(x, Mapping):
            if x.get("e") == "var" and isinstance(x.get("name"), str):
                if (x.get("scope"), x["name"]) not in out:
                    out.append((x.get("scope"), x["name"]))
                return
            if x.get("e") == "member" and isinstance(x.get("object"), Mapping) and x["object"].get("e") == "var":
                # q.status: the column `status` of the query or struct `q`
                pair = (None, str(x.get("name")))
                if pair not in out:
                    out.append(pair)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(e)
    return out


# ------------------------------------------------------------------ SQL conditions

def sql_tree(sql: str) -> Any | None:
    """A sqlglot tree of a query node's text, or None when it does not parse. Never run."""
    import sqlglot
    try:
        return sqlglot.parse_one(sql)
    except Exception:  # sqlglot raises several error types; any of them means "not read"
        return None


def table_name(t: Any) -> str:
    """A table as written, lower-cased, with its schema or library kept."""
    name = (t.name or "").lower()
    db = (t.db or "").lower()
    return f"{db}.{name}" if db else name


def _is_key_lookup(c: Any) -> bool:
    """`id = :id` or `a.k = b.k`: a lookup or a join, not a rule."""
    from sqlglot import exp
    if not isinstance(c, exp.EQ):
        return False
    sides = (c.this, c.expression)
    return all(isinstance(s, (exp.Column, exp.Placeholder, exp.Parameter)) for s in sides)


def _conjuncts(cond: Any) -> list[Any]:
    from sqlglot import exp
    if isinstance(cond, exp.And):
        return _conjuncts(cond.this) + _conjuncts(cond.expression)
    if isinstance(cond, exp.Paren):
        return _conjuncts(cond.this) if isinstance(cond.this, exp.And) else [cond]
    return [cond]


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


def sql_candidates(sql: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Rule candidates in one SQL statement, and the tables it names (as written)."""
    from sqlglot import exp
    tree = sql_tree(sql)
    if tree is None:
        return [], []
    tables = sorted({table_name(t) for t in tree.find_all(exp.Table) if t.name})
    out: list[dict[str, Any]] = []
    for where in tree.find_all(exp.Where):
        kept = [c for c in _conjuncts(where.this) if not _is_key_lookup(c)]
        if kept:
            columns = sorted({c.name.lower() for k in kept for c in k.find_all(exp.Column) if c.name})
            out.append({"kind": "sql_where", "condition": _clip(" AND ".join(k.sql() for k in kept)), "effect": "rows are kept only where the condition holds", "columns": columns})
    for case in tree.find_all(exp.Case):
        alias = case.parent.alias if isinstance(case.parent, exp.Alias) else ""
        columns = sorted({c.name.lower() for c in case.find_all(exp.Column) if c.name})
        out.append({"kind": "sql_case", "condition": _clip(case.sql()), "effect": f"computes {alias}" if alias else "computes a value", "columns": columns})
    return out, tables


# ------------------------------------------------------------------ candidates from behaviour

_ERROR_TEXT = re.compile(r"error|invalid|required|must|not allowed|denied|cannot|failed", re.I)


def _effects(block: list[Any]) -> list[str]:
    """What a branch does that marks its condition as a guard: throw, redirect, stop, or an error message."""
    out = []
    for s in block or []:
        if not isinstance(s, Mapping):
            continue
        op = s.get("op")
        if op == "throw":
            out.append("throws " + render(s.get("value"), 120) if s.get("value") else "throws")
        elif op == "redirect":
            out.append("redirects to " + render(s.get("target"), 120))
        elif op == "exit":
            out.append("stops the " + ("request" if s.get("kind") == "abort" else "unit"))
        elif op == "output" and s.get("channel") == "response" and s.get("value") is not None:
            text = render(s.get("value"), 120)
            if _ERROR_TEXT.search(text):
                out.append("outputs " + text)
    return out


def _assigned(block: list[Any]) -> list[str]:
    names = []
    for s in block or []:
        if isinstance(s, Mapping) and s.get("op") == "assign":
            names.append(render(s.get("target"), 60))
    return names


def _is_first_last(cond: Any) -> bool:
    """`first.k` / `last.k` alone: a BY-group idiom (deduplication), not a rule."""
    return isinstance(cond, Mapping) and cond.get("e") == "platform" and cond.get("construct") == "sas:first_last"


def _computed(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("e") in ("binop", "call", "cond", "unop")


def _blocks(s: Mapping[str, Any]) -> list[list[Any]]:
    """Every statement list directly under a statement, in source order."""
    out = []
    for key in ("then", "else", "body", "default", "finally"):
        if isinstance(s.get(key), list):
            out.append(s[key])
    for case in s.get("cases") or []:
        if isinstance(case, Mapping) and isinstance(case.get("body"), list):
            out.append(case["body"])
    for catch in s.get("catches") or []:
        if isinstance(catch, Mapping) and isinstance(catch.get("body"), list):
            out.append(catch["body"])
    return out


def _datasets(x: Any) -> list[str]:
    out = []
    if isinstance(x, Mapping):
        for item in x.get("items") or []:
            if isinstance(item, Mapping) and item.get("e") == "name" and isinstance(item.get("name"), str):
                out.append(item["name"].lower())
    return out


def unit_candidates(body: list[Any]) -> list[dict[str, Any]]:
    """Rule candidates in one behaviour unit's body, in source order. Each has `kind`, `at`,
    `condition`, `effect` and what it reads: `vars` [(scope, name)], `tables` and
    `datasets_read` / `datasets_written` for a DATA step."""
    out: list[dict[str, Any]] = []

    def walk(stmts: list[Any], step: dict[str, list[str]] | None, in_branch: bool) -> None:
        for s in stmts or []:
            if not isinstance(s, Mapping) or not isinstance(s.get("at"), list):
                continue
            op = s.get("op")
            at = [int(s["at"][0]), int(s["at"][1])]
            ctx = {"datasets_read": (step or {}).get("read", []), "datasets_written": (step or {}).get("written", [])}
            if op == "loop" and s.get("kind") == "implicit":  # a DATA step's row loop
                detail = s.get("detail") if isinstance(s.get("detail"), Mapping) else {}
                written = [d.lower() for d in detail.get("data", []) if isinstance(d, str)]
                walk(s.get("body") or [], {"read": _datasets(s.get("over")), "written": written}, False)
                continue
            if op == "if":
                cond = s.get("cond")
                if s.get("where") or s.get("subsetting"):
                    # `if not (c) then continue`: the rows kept are those where c holds
                    kept = cond.get("operand") if isinstance(cond, Mapping) and cond.get("e") == "unop" and cond.get("op") == "not" else cond
                    out.append({"kind": "where" if s.get("where") else "subsetting_if", "at": at, "condition": render(kept), "effect": "rows are kept only where the condition holds", "vars": reads(kept), **ctx})
                elif not _is_first_last(cond):
                    effects = _effects(s.get("then") or []) + _effects(s.get("else") or [])
                    if effects:
                        out.append({"kind": "guard", "at": at, "condition": render(cond), "effect": _clip("; ".join(effects)), "vars": reads(cond), **ctx})
                    elif step is not None:
                        assigned = sorted(set(_assigned(s.get("then") or []) + _assigned(s.get("else") or [])))
                        if assigned:
                            out.append({"kind": "conditional_assignment", "at": at, "condition": render(cond), "effect": _clip("sets " + ", ".join(assigned)), "vars": reads(cond), **ctx})
                for block in _blocks(s):
                    walk(block, step, True)
                continue
            if op == "assign" and step is not None and not in_branch and _computed(s.get("value")):
                target = render(s.get("target"), 60)
                out.append({"kind": "computed", "at": at, "condition": None, "effect": _clip(f"{target} = {render(s.get('value'))}"), "vars": reads(s.get("value")), **ctx})
                continue
            if op == "query" and isinstance(s.get("sql"), str) and not s.get("remote"):
                inline = any(isinstance(p, Mapping) and p.get("inline") for p in s.get("params") or [])
                found, tables = sql_candidates(s["sql"])
                for c in found:
                    out.append({"kind": c["kind"], "at": at, "condition": c["condition"], "effect": c["effect"], "vars": [(None, col) for col in c["columns"]], "tables": [] if inline else tables, **ctx})
                continue
            for block in _blocks(s):
                walk(block, step, in_branch)

    walk(body, None, False)
    return out


class _Index:
    """Lookups over the compiled shards: entities by kind and name, forms by submit target."""

    def __init__(self, shards: Mapping[str, Mapping[str, Any]]):
        self.entities: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.by_name: dict[tuple[str, str], set[str]] = {}
        self.fields: dict[tuple[str, str], set[str]] = {}
        self.submits: dict[str, set[str]] = {}  # submit target -> forms
        self.renders: dict[str, set[str]] = {}  # code -> screens it renders
        self.contained_by: dict[str, set[str]] = {}  # entity -> what CONTAINS it
        self.packages: dict[tuple[str, str], set[str]] = {}  # (unit, package name) -> Package entities
        self.unit_of: dict[str, str] = {}  # entity -> the unit whose shard declares it
        for unit in sorted(shards):
            shard = shards[unit]
            for e in shard["entities"]:
                self.entities[e["id"]] = e
                self.unit_of.setdefault(e["id"], unit)
                key = e["natural_key"]
                if e["kind"] in (EntityKind.TABLE, EntityKind.DATASET) and isinstance(key.get("name"), str):
                    self.by_name.setdefault((e["kind"], key["name"].lower()), set()).add(e["id"])
                elif e["kind"] == EntityKind.FIELD and isinstance(key.get("table"), str) and isinstance(key.get("name"), str):
                    self.fields.setdefault((key["table"].lower(), key["name"].lower()), set()).add(e["id"])
                elif e["kind"] == EntityKind.PACKAGE and isinstance(key.get("name"), str):
                    self.packages.setdefault((unit, key["name"]), set()).add(e["id"])
            for ev in shard["evidence"]:
                self.evidence[ev["id"]] = ev
            for a in shard["assertions"]:
                if a["predicate"] == "SUBMITS_TO":
                    self.submits.setdefault(a["object"], set()).add(a["subject"])
                elif a["predicate"] == "RENDERS":
                    self.renders.setdefault(a["subject"], set()).add(a["object"])
                elif a["predicate"] == "CONTAINS":
                    self.contained_by.setdefault(a["object"], set()).add(a["subject"])

    def data(self, name: str) -> str | None:
        """The one Table or Dataset with this name, if exactly one exists."""
        hits = self.by_name.get((EntityKind.TABLE, name), set()) | self.by_name.get((EntityKind.DATASET, name), set())
        return next(iter(hits)) if len(hits) == 1 else None

    def field(self, table: str, column: str) -> str | None:
        hits = self.fields.get((table.lower(), column.lower()), set())
        return next(iter(hits)) if len(hits) == 1 else None

    def forms_for(self, owner: str) -> set[str]:
        targets = {owner} | self.renders.get(owner, set())
        return {f for t in sorted(targets) for f in self.submits.get(t, set())}

    def path_of(self, artifact: str) -> str:
        e = self.entities.get(artifact)
        return e["natural_key"].get("path", "") if e else ""

    def symbol_containers(self, symbol: str) -> list[str]:
        """The Program, Component or Package a Symbol belongs to, so a rule a function enforces
        stays attached in the architecture tier, which leaves Symbols out.

        First what CONTAINS it (a ColdFusion component's Program contains its functions). Else
        the Package its own key names (`{package, qualified_name}`), when its unit declares
        exactly one Package of that name. Else nothing: an Artifact cannot be the subject of
        ENFORCES (compile.ontology), so a Symbol with neither keeps only its own edge."""
        kinds = (EntityKind.PROGRAM, EntityKind.COMPONENT, EntityKind.PACKAGE)
        found = sorted(c for c in self.contained_by.get(symbol, set()) if self.entities.get(c, {}).get("kind") in kinds)
        if found:
            return found
        e = self.entities.get(symbol)
        name = e["natural_key"].get("package") if e else None
        hits = self.packages.get((self.unit_of.get(symbol, ""), name), set()) if isinstance(name, str) else set()
        return sorted(hits) if len(hits) == 1 else []


def _applies_to(c: Mapping[str, Any], owner: str, index: _Index) -> list[str]:
    """The existing entities a candidate's condition reads: Fields, Tables, Datasets, Forms."""
    found: set[str] = set()
    names = [n.lower() for _, n in c["vars"]]
    if c["kind"] in ("sql_where", "sql_case"):
        sources = list(c.get("tables", []))
    elif c["kind"] in ("computed", "conditional_assignment"):
        sources = list(c.get("datasets_written", []))
    else:
        sources = list(c.get("datasets_read", []))
    for source in sources:
        hit = index.data(source)
        if hit:
            found.add(hit)
        for n in names:
            field = index.field(source, n) or (index.field(source.split(".")[-1], n) if "." in source else None)
            if field:
                found.add(field)
    if any(scope == "form" for scope, _ in c["vars"]):
        found |= index.forms_for(owner)
    return sorted(found)


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """Per owner with candidates: the candidates (numbered c1, c2, ... in path and span
    order), the transform shapes of its units, and the evidence rows they cite."""
    shards: dict[str, dict[str, Any]] = inputs["shards"]
    index = _Index(shards)
    owners: dict[str, dict[str, Any]] = {}
    for unit in sorted(shards):
        shard = shards[unit]
        shapes: dict[str, list[dict[str, Any]]] = {}
        for t in shard.get("transforms", []):  # 8.1; present only when the compiler derived some
            for item in t.get("transforms", []):
                shapes.setdefault(t["owner"], []).append({k: item[k] for k in ("op", "inputs", "output", "keys", "primitive") if k in item})
        for row in shard.get("behaviors", []):
            owner = index.entities.get(row["owner"])
            if owner is None or not row["evidence_ids"]:
                continue
            group = owners.setdefault(row["owner"], {"owner": row["owner"], "owner_kind": owner["kind"], "owner_name": owner["name"], "unit": unit, "raw": [], "transforms": shapes.get(row["owner"], [])})
            path = index.path_of(row["artifact"])
            for c in unit_candidates(row["body"]):
                c.update(path=path, evidence_id=row["evidence_ids"][0], unit_kind=row.get("kind"))
                group["raw"].append(c)
    groups = []
    for oid in sorted(owners):
        g = owners[oid]
        raw = sorted(g.pop("raw"), key=lambda c: (c["path"], c["at"][0], c["at"][1], c["kind"], c["condition"] or "", c["effect"]))
        if not raw:
            continue
        candidates = []
        for n, c in enumerate(raw[:MAX_CANDIDATES], 1):
            candidates.append({
                "id": f"c{n}", "kind": c["kind"], "path": c["path"], "at": c["at"], "unit_kind": c["unit_kind"],
                "condition": c["condition"], "effect": c["effect"], "evidence_id": c["evidence_id"],
                "applies_to": _applies_to(c, oid, index),
            })
        evidence = sorted({c["evidence_id"] for c in candidates})
        containers = index.symbol_containers(oid) if g["owner_kind"] == EntityKind.SYMBOL else []
        groups.append({**g, "transforms": g["transforms"][:MAX_TRANSFORMS], "candidates": candidates, "omitted": max(0, len(raw) - MAX_CANDIDATES), "evidence": [index.evidence[e] for e in evidence if e in index.evidence], "containers": containers})
    return {"groups": groups}


FEATURES_BUILD = Step("features.rules", "0.2.0", _features)


def model_features(group: Mapping[str, Any]) -> dict[str, Any]:
    """What the model sees of one owner: its kind and name, its candidates without internal
    references, and its transform shapes."""
    return {
        "owner": {"kind": group["owner_kind"], "name": group["owner_name"]},
        "candidates": [{k: c[k] for k in ("id", "kind", "path", "unit_kind", "condition", "effect") if c.get(k) is not None} for c in group["candidates"]],
        "transforms": list(group["transforms"]),
        **({"omitted_candidates": group["omitted"]} if group["omitted"] else {}),
    }


# ------------------------------------------------------------------ the model call

def slug(name: Any) -> str | None:
    """A model-given name as the contract's slug, or None when it cannot be one."""
    if not isinstance(name, str):
        return None
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s if s and len(s) <= 60 and SLUG.match(s) else None


def _answered_rules(content: Mapping[str, Any], candidates: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str, list[str]]]:
    """(rule_kind, name, statement, candidate ids) for each acceptable rule; first of a name wins."""
    kept: dict[str, tuple[str, str, str, list[str]]] = {}
    items = content.get("rules") if isinstance(content, Mapping) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        name, statement, kind, cited = slug(item.get("name")), item.get("statement"), item.get("rule_kind"), item.get("candidate_ids")
        if name is None or kind not in RULE_KINDS:
            continue
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 280:
            continue
        if not isinstance(cited, list) or not cited or not all(isinstance(c, str) and c in candidates for c in cited):
            continue
        if name not in kept:
            kept[name] = (kind, name, " ".join(statement.split()), sorted(set(cited), key=lambda c: int(c[1:])))
    return [kept[n] for n in sorted(kept)][:MAX_RULES]


def call_llm(spec: DecisionSpec, scope_entity: str, features: Mapping[str, Any], *, ledger: Ledger, evidence_ids: tuple[str, ...], gateway: Gateway | None, budget: Budget | None, instructions: str, schema: Mapping[str, Any], accept: Any, label: str) -> DecisionResult:
    """One open-ended LLM decision, held to its form as `runner.hypothesize` holds DM08's.

    `accept(content)` returns `(hypotheses, alternatives)` for an acceptable answer or None;
    an unacceptable answer is discarded and counted as an attempt. The decision key leaves
    the override set out on purpose: an owner confirming a result must not ask the model
    again, or a renamed result would orphan the confirmation. Confirmations are their own
    decisions (`confirmation`), keyed with the override set.
    """
    step = next(s for s in spec.strategy if s.kind == "llm")
    inputs = {"features": dict(features), "evidence_ids": sorted(evidence_ids), "scope": scope_entity, "decision_policy": _policy([step], {"@instructions": instructions}, [], gateway, {})}
    name = f"{spec.id}@{spec.version}"
    fsh = digest(inputs)
    did = decision_id(name, fsh, "")
    cached = ledger.get(did)
    if cached is not None:
        return cached
    base = dict(decision_id=did, spec=name, scope_entity=scope_entity, feature_set_hash=fsh, rule_outcome="ambiguous", evidence_ids=sorted(evidence_ids), decision_method="llm")

    def abstain(reason: str, attempts: int = 0) -> DecisionResult:
        return ledger.put(DecisionResult(**base, selected_label=None, abstained=True, abstain_reason=reason, attempts=max(attempts, 1), model_ref=step.model_ref), reusable=reason not in TRANSIENT)

    missing = [k for k in spec.abstain_required_if_missing if not features.get(k)]
    if missing:
        return abstain("missing_required_feature:" + ",".join(missing))
    if gateway is None:
        return abstain("ambiguous_no_model")
    try:
        llm = gateway.resolve_llm(step.model_ref or "llm-default")
    except EgressDenied:
        return abstain("egress_denied")
    except ModelUnavailable:
        return abstain("model_unavailable")
    # Repository-derived content travels only as data, labelled as such.
    data = {"trust": "untrusted_data", "scope": spec.scope, "features": dict(features), "evidence_ids": sorted(evidence_ids)}
    if len(canonical({"data": data, "instructions": instructions})) > spec.max_input_bytes:
        return abstain("input_too_large")
    budget = budget or Budget()
    attempts, reason = 0, "model_unavailable"
    for _ in range(spec.max_attempts):
        if not budget.take(1):
            return abstain("budget_exhausted", attempts)
        attempts += 1
        try:
            answer = llm.complete(instructions, data, schema)
        except ModelUnavailable:
            reason = "model_unavailable"
            continue
        accepted = accept(answer.content)
        if not accepted:
            reason = "invalid_answer"
            continue
        hypotheses, alternatives = accepted
        return ledger.put(DecisionResult(**base, selected_label=label, abstained=False, hypotheses=hypotheses, alternatives=alternatives, attempts=attempts, model_ref=f"{llm.provider}/{answer.model}"))
    return abstain(reason, attempts)


def lift(group: Mapping[str, Any], *, ledger: Ledger, gateway: Gateway | None, budget: Budget | None) -> DecisionResult:
    """Ask the model to name one owner's rules (spec dm.business-rules)."""
    by_id = {c["id"]: c for c in group["candidates"]}

    def accept(content: Mapping[str, Any]) -> tuple[list[Hypothesis], list[str]] | None:
        rules = _answered_rules(content, by_id)
        if not rules:
            return None
        hypotheses = [Hypothesis(statement=statement, evidence_ids=sorted({by_id[c]["evidence_id"] for c in cited})) for _, _, statement, cited in rules]
        return hypotheses, [f"{kind} {name} {','.join(cited)}" for kind, name, _, cited in rules]

    evidence = tuple(sorted({c["evidence_id"] for c in group["candidates"]}))
    return call_llm(SPEC, group["owner"], model_features(group), ledger=ledger, evidence_ids=evidence, gateway=gateway, budget=budget, instructions=INSTRUCTIONS, schema=RULES_SCHEMA, accept=accept, label="rules")


def rules_of(decision: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rules a `lift` decision holds: {rule_kind, name, statement, candidate_ids}."""
    if decision["abstained"]:
        return []
    out = []
    for h, alt in zip(decision["hypotheses"], decision["alternatives"]):
        kind, name, cited = alt.split(" ", 2)
        out.append({"rule_kind": kind, "name": name, "statement": h["statement"], "candidate_ids": cited.split(",")})
    return out


# ------------------------------------------------------------------ owner confirmation

def confirmation(spec: DecisionSpec, entity: str, lifted: Mapping[str, Any], evidence_ids: list[str], overrides: Overrides, ledger: Ledger) -> DecisionResult | None:
    """The owner's word on one lifted rule or workflow, as its own ledger decision, or None.

    Only `confirmed` and `rejected` count (`decisions.overrides` refuses other labels for these
    specs). A rejection records the compiler's `hypothesis` as a disagreement, so the snapshot
    keeps a `label_disagreement` conflict for the rule it drops.
    """
    label = overrides.label_for(spec.id, entity)
    if label not in CONFIRMATION_LABELS:
        return None
    name = f"{spec.id}@{spec.version}"
    fsh = digest({"scope": entity, "lifted_by": lifted.get("decision_id"), "evidence_ids": sorted(evidence_ids)})
    result = DecisionResult(
        decision_id=decision_id(name, fsh, overrides.set_hash), spec=name, scope_entity=entity, feature_set_hash=fsh,
        rule_outcome="resolved", selected_label=label, evidence_ids=sorted(evidence_ids), abstained=False,
        decision_method="owner_override",
        disagreements=[Disagreement(method=lifted.get("decision_method") or "llm", label="hypothesis")] if label == "rejected" else [],
    )
    return ledger.put(result)


def lifted_rules(repository_id: str, group: Mapping[str, Any], decision: Mapping[str, Any], overrides: Overrides, ledger: Ledger) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rules one lift decision yields for the compiler, and the confirmation decisions made.

    A rule cites the candidates it names; its evidence is their units' evidence, its
    `applies_to` the entities their conditions read. A rule a Symbol enforces also names the
    Symbol's `containers` (`_Index.symbol_containers`), which enforce it through the Symbol.
    """
    by_id = {c["id"]: c for c in group["candidates"]}
    evidence_rows = {ev["id"]: ev for ev in group["evidence"]}
    out, made = [], []
    for r in rules_of(decision):
        cited = [by_id[c] for c in r["candidate_ids"] if c in by_id]
        if not cited:
            continue
        key = {"scope": group["owner_name"], "name": r["name"]}
        rid = Entity.make(repository_id, EntityKind.BUSINESS_RULE, key, r["name"]).id
        evidence = sorted({c["evidence_id"] for c in cited if c["evidence_id"] in evidence_rows})
        if not evidence:
            continue
        confirmed = confirmation(SPEC, rid, decision, evidence, overrides, ledger)
        if confirmed is not None:
            made.append(confirmed.row())
        out.append({
            "id": rid, "natural_key": key, "owner": group["owner"], "containers": list(group.get("containers", [])),
            "statement": r["statement"], "rule_kind": r["rule_kind"],
            "status": "rejected" if confirmed and confirmed.selected_label == "rejected" else "confirmed" if confirmed else "hypothesis",
            "candidates": [{"id": c["id"], "kind": c["kind"], "path": c["path"], "at": c["at"]} for c in cited],
            "applies_to": sorted({e for c in cited for e in c["applies_to"]}),
            "evidence": [evidence_rows[e] for e in evidence],
            "decision_ref": decision["decision_id"], "confirmation_ref": confirmed.decision_id if confirmed else None,
        })
    return out, made
