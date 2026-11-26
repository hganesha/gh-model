"""Workflows: the states a business object moves through, found in code (contract 8.2).

States and transitions are facts read without a model. `features.workflows`, a pure step,
walks every stored behaviour unit for literal assignments to a column named `status`,
`state` or `stage`:

- SQL `UPDATE t SET status = 'approved'`, with a `:param` whose value is a literal;
- SQL `INSERT INTO t (..., status) VALUES (..., 'draft')`, the state a row starts in;
- a SAS DATA-step assignment `status = 'review';`, for each dataset the step writes.

A transition's `from` state is read from the same statement (`WHERE status = 'submitted'`)
or from an enclosing condition that compares the column with a literal; its guard is the
text of the conditions around it. An assignment whose value is not a literal (a macro
variable, a function argument) stays dynamic: it is counted and reported, never guessed.

A column with at least two states across the repository is a `Workflow {scope: <table or
dataset as written>, name: <column>}`. Spec `dm.workflows` only names it and its states;
when the model abstains the workflow is still published, unnamed. An owner confirms or
rejects it with a `decisions` override, as for rules.

A naming answer is stored like a rule (see `rules`): `hypotheses[0]` is the workflow's
name with `alternatives[0] == "workflow"`, then one hypothesis per labelled state with
`alternatives[i] == "state:<value>"`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Entity, Hypothesis
from repo_intel.compile.ontology import EntityKind
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.overrides import Overrides
from repo_intel.decisions.rules import _Index, call_llm, confirmation, render, sql_tree, table_name
from repo_intel.decisions.runner import Budget, Ledger, verifier

SPEC = DecisionSpec.model_validate({
    "id": "dm.workflows", "version": "0.1.0", "scope": "workflow",
    "input_schema": "WorkflowStates.v1", "output_schema": "WorkflowNames.v1",
    "labels": ["names", "confirmed", "rejected", "none"],
    "feature_sources": ["behaviors"],
    "strategy": [{"kind": "llm", "when": "always", "model_ref": "llm-default", "prompt_ref": "prompts/workflow-names-v1"}],
    "abstain_required_if_missing": ["states"],
    "verification": "vfy.workflow-citations-v1",
    "max_input_bytes": 20000,
})

INSTRUCTIONS = (
    "You name a business workflow found in code: the states a record moves through and the "
    "transitions between them, as read from assignments to one column. Give the workflow a short "
    "name of at most 80 characters, and for each state you can explain a label of at most 60 "
    "characters, using only the state values given. Cite the evidence_ids you relied on, chosen only "
    "from the evidence_ids given."
)

STATE_COLUMNS = frozenset({"status", "state", "stage"})
MAX_TRANSITIONS = 60  # per workflow, as data for the model and as an attribute

NAMES_SCHEMA = {"type": "object", "required": ["name", "evidence_ids"], "properties": {
    "name": {"type": "string", "maxLength": 80},
    "states": {"type": "array", "items": {"type": "object", "required": ["value", "label"], "properties": {"value": {"type": "string"}, "label": {"type": "string", "maxLength": 60}}}},
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
}}


@verifier("vfy.workflow-citations-v1")
def verify_citations(f: Mapping[str, Any], label: str) -> str:
    return "unchecked"  # citations are checked in `name_workflow`


# ------------------------------------------------------------------ state assignments

def _lit(e: Any) -> str | None:
    """A literal state value as text, or None when the expression is not a literal."""
    if isinstance(e, Mapping) and e.get("e") == "lit" and isinstance(e.get("value"), (str, int, float)) and not isinstance(e.get("value"), bool):
        value = str(e["value"]).strip()
        return value if value and len(value) <= 60 else None
    return None


def _column_of(e: Any) -> str | None:
    """The column a condition operand names: `status`, `q.status`, `form.status`."""
    if not isinstance(e, Mapping):
        return None
    if e.get("e") in ("var", "member") and isinstance(e.get("name"), str):
        return e["name"].lower()
    return None


def _from_states(guards: list[Any], column: str) -> list[str]:
    """States an enclosing condition requires: `status == "submitted"` on the then side."""
    out: list[str] = []

    def walk(c: Any) -> None:
        if not isinstance(c, Mapping) or c.get("e") != "binop":
            return
        if c.get("op") == "and":
            walk(c.get("left"))
            walk(c.get("right"))
        elif c.get("op") == "==":
            for a, b in ((c.get("left"), c.get("right")), (c.get("right"), c.get("left"))):
                if _column_of(a) == column and _lit(b) is not None:
                    out.append(_lit(b))  # type: ignore[arg-type]

    for g in guards:
        walk(g)
    return sorted(set(out))


def _guard_text(guards: list[Any], extra: list[str] | None = None) -> str | None:
    parts = [render(g, 120) for g in guards] + list(extra or [])
    text = " and ".join(p if len(guards) + len(extra or []) == 1 else f"({p})" for p in parts)
    return (text if len(text) <= 200 else text[:197] + "...") or None


def _param_value(name: str, params: list[Any]) -> Any:
    return next((p.get("value") for p in params if isinstance(p, Mapping) and p.get("name") == name), None)


def _sql_value(node: Any, params: list[Any]) -> tuple[str | None, bool]:
    """(literal value, dynamic) of the right-hand side of `SET status = ...` or a VALUES item."""
    from sqlglot import exp
    if isinstance(node, exp.Literal):
        value = str(node.this).strip()
        return (value if value and len(value) <= 60 else None), not value
    if isinstance(node, (exp.Placeholder, exp.Parameter)):
        value = _lit(_param_value(node.name, params))
        return value, value is None
    if isinstance(node, exp.Null):
        return None, False
    return None, True


def _sql_assignments(s: Mapping[str, Any]) -> list[dict[str, Any]]:
    """State assignments in one `query` node: (table, column, value or dynamic, from states, guard)."""
    from sqlglot import exp
    if s.get("remote") or not isinstance(s.get("sql"), str):
        return []
    tree = sql_tree(s["sql"])
    if tree is None:
        return []
    params = s.get("params") or []
    inline = any(isinstance(p, Mapping) and p.get("inline") for p in params)
    out = []
    if isinstance(tree, exp.Update) and isinstance(tree.this, exp.Table):
        table = table_name(tree.this)
        where = tree.args.get("where")
        conj = []
        if where is not None:
            stack = [where.this]
            while stack:
                c = stack.pop()
                if isinstance(c, exp.And):
                    stack += [c.expression, c.this]
                else:
                    conj.append(c)
        for item in tree.expressions:
            if not isinstance(item, exp.EQ) or not isinstance(item.this, exp.Column):
                continue
            column = item.this.name.lower()
            if column not in STATE_COLUMNS:
                continue
            value, dynamic = _sql_value(item.expression, params)
            froms, others = [], []
            for c in conj:
                if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and c.this.name.lower() == column and isinstance(c.expression, exp.Literal):
                    froms.append(str(c.expression.this))
                elif isinstance(c, exp.In) and isinstance(c.this, exp.Column) and c.this.name.lower() == column and all(isinstance(x, exp.Literal) for x in c.expressions):
                    froms += [str(x.this) for x in c.expressions]
                elif not (isinstance(c, exp.EQ) and all(isinstance(x, (exp.Column, exp.Placeholder, exp.Parameter)) for x in (c.this, c.expression))):
                    others.append(" ".join(c.sql().split()))
            out.append({"table": table, "column": column, "value": value, "dynamic": dynamic or inline, "from": sorted(set(froms)), "sql_guard": others, "kind": "update"})
    elif isinstance(tree, exp.Insert) and isinstance(tree.this, exp.Schema) and isinstance(tree.this.this, exp.Table):
        table = table_name(tree.this.this)
        columns = [c.name.lower() for c in tree.this.expressions]
        values = tree.args.get("expression")
        rows = values.expressions if isinstance(values, exp.Values) else []
        for row in rows:
            items = row.expressions if isinstance(row, exp.Tuple) else [row]
            for column, node in zip(columns, items):
                if column in STATE_COLUMNS:
                    value, dynamic = _sql_value(node, params)
                    out.append({"table": table, "column": column, "value": value, "dynamic": dynamic or inline, "from": [], "sql_guard": [], "kind": "insert"})
    return [a for a in out if a["value"] is not None or a["dynamic"]]


def unit_assignments(body: list[Any]) -> list[dict[str, Any]]:
    """Assignments to a state column in one behaviour unit, in source order."""
    out: list[dict[str, Any]] = []

    def walk(stmts: list[Any], written: list[str] | None, guards: list[Any]) -> None:
        for s in stmts or []:
            if not isinstance(s, Mapping):
                continue
            op = s.get("op")
            at = [int(s["at"][0]), int(s["at"][1])] if isinstance(s.get("at"), list) else None
            if op == "loop" and s.get("kind") == "implicit":
                detail = s.get("detail") if isinstance(s.get("detail"), Mapping) else {}
                walk(s.get("body") or [], [d.lower() for d in detail.get("data", []) if isinstance(d, str)], guards)
                continue
            if op == "if":
                cond = s.get("cond")
                walk(s.get("then") or [], written, guards + [cond])
                walk(s.get("else") or [], written, guards + [{"e": "unop", "op": "not", "operand": cond}])
                continue
            if op == "assign" and written is not None and at is not None:
                column = _column_of(s.get("target")) if isinstance(s.get("target"), Mapping) and s["target"].get("e") == "var" else None
                if column in STATE_COLUMNS:
                    value = _lit(s.get("value"))
                    for table in written:
                        out.append({"table": table, "column": column, "value": value, "dynamic": value is None, "from": _from_states(guards, column), "guard": _guard_text(guards), "at": at, "kind": "assign"})
                continue
            if op == "query" and at is not None:
                for a in _sql_assignments(s):
                    froms = sorted(set(a["from"]) | set(_from_states(guards, a["column"])))
                    out.append({"table": a["table"], "column": a["column"], "value": a["value"], "dynamic": a["dynamic"], "from": froms, "guard": _guard_text(guards, a["sql_guard"]), "at": at, "kind": a["kind"]})
                continue
            for key in ("then", "else", "body", "default", "finally"):
                if isinstance(s.get(key), list):
                    walk(s[key], written, guards)
            for nested in list(s.get("cases") or []) + list(s.get("catches") or []):
                if isinstance(nested, Mapping) and isinstance(nested.get("body"), list):
                    walk(nested["body"], written, guards)

    walk(body, None, [])
    return out


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """Workflow candidates across the repository: per (table, column), the literal states,
    the transitions with their evidence, and every assignment that was not a literal."""
    shards: dict[str, dict[str, Any]] = inputs["shards"]
    index = _Index(shards)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    dynamic: list[dict[str, Any]] = []
    total = literal = 0
    for unit in sorted(shards):
        for row in shards[unit].get("behaviors", []):
            if not row["evidence_ids"]:
                continue
            path = index.path_of(row["artifact"])
            for a in unit_assignments(row["body"]):
                total += 1
                if a["dynamic"]:
                    dynamic.append({"artifact": row["artifact"], "unit": unit, "path": path, "at": a["at"], "table": a["table"], "column": a["column"]})
                    continue
                literal += 1
                g = groups.setdefault((a["table"], a["column"]), {"table": a["table"], "column": a["column"], "transitions": {}, "evidence": set()})
                ev = row["evidence_ids"][0]
                g["evidence"].add(ev)
                for source in a["from"] or [None]:
                    t = {"from": source, "to": a["value"], "kind": a["kind"], "guard": a["guard"], "path": path, "at": a["at"], "evidence_id": ev}
                    g["transitions"][(path, a["at"][0], a["at"][1], source or "", a["value"])] = t
    workflows = []
    for key in sorted(groups):
        g = groups[key]
        transitions = [g["transitions"][k] for k in sorted(g["transitions"])]
        states = sorted({t["to"] for t in transitions} | {t["from"] for t in transitions if t["from"]})
        if len(states) < 2:
            continue
        workflows.append({
            "table": g["table"], "column": g["column"], "states": states, "transitions": transitions[:MAX_TRANSITIONS],
            "omitted_transitions": max(0, len(transitions) - MAX_TRANSITIONS),
            "applies_to": index.data(g["table"]),
            "evidence": [index.evidence[e] for e in sorted(g["evidence"]) if e in index.evidence],
        })
    dynamic.sort(key=lambda d: (d["path"], d["at"][0], d["at"][1], d["table"], d["column"]))
    return {"workflows": workflows, "dynamic": dynamic, "assignments": total, "literal": literal}


FEATURES_BUILD = Step("features.workflows", "0.1.0", _features)


# ------------------------------------------------------------------ naming and confirmation

def model_features(w: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "table": w["table"], "column": w["column"], "states": list(w["states"]),
        "transitions": [{k: t[k] for k in ("from", "to", "kind", "guard", "path", "evidence_id") if t.get(k) is not None} for t in w["transitions"]],
    }


def name_workflow(scope_entity: str, w: Mapping[str, Any], *, ledger: Ledger, gateway: Gateway | None, budget: Budget | None) -> DecisionResult:
    """Ask the model to name one workflow and its states (spec dm.workflows)."""
    evidence = tuple(sorted({ev["id"] for ev in w["evidence"]}))
    states = set(w["states"])

    def accept(content: Mapping[str, Any]) -> tuple[list[Hypothesis], list[str]] | None:
        name, cited = content.get("name"), content.get("evidence_ids")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            return None
        if not isinstance(cited, list) or not cited or not set(cited) <= set(evidence):
            return None
        cited = sorted(set(cited))
        labels: dict[str, str] = {}
        for item in content.get("states") or []:
            if isinstance(item, Mapping) and item.get("value") in states and isinstance(item.get("label"), str) and item["label"].strip() and len(item["label"]) <= 60:
                labels.setdefault(item["value"], " ".join(item["label"].split()))
        hypotheses = [Hypothesis(statement=" ".join(name.split()), evidence_ids=cited)] + [Hypothesis(statement=labels[v], evidence_ids=cited) for v in sorted(labels)]
        return hypotheses, ["workflow"] + [f"state:{v}" for v in sorted(labels)]

    return call_llm(SPEC, scope_entity, model_features(w), ledger=ledger, evidence_ids=evidence, gateway=gateway, budget=budget, instructions=INSTRUCTIONS, schema=NAMES_SCHEMA, accept=accept, label="names")


def names_of(decision: Mapping[str, Any]) -> tuple[str | None, dict[str, str]]:
    """(workflow name, {state value: label}) from a naming decision."""
    if decision["abstained"]:
        return None, {}
    name, labels = None, {}
    for h, alt in zip(decision["hypotheses"], decision["alternatives"]):
        if alt == "workflow":
            name = h["statement"]
        elif alt.startswith("state:"):
            labels[alt[len("state:"):]] = h["statement"]
    return name, labels


def lifted_workflows(repository_id: str, found: Mapping[str, Any], *, ledger: Ledger, gateway: Gateway | None, budget: Budget | None, overrides: Overrides) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The workflows for the compiler, and every decision made for them (naming, confirmation)."""
    out, made = [], []
    for w in found["workflows"]:
        key = {"scope": w["table"], "name": w["column"]}
        wid = Entity.make(repository_id, EntityKind.WORKFLOW, key, w["column"]).id
        naming = name_workflow(wid, w, ledger=ledger, gateway=gateway, budget=budget).row()
        made.append(naming)
        evidence = sorted(ev["id"] for ev in w["evidence"])
        confirmed = confirmation(SPEC, wid, naming, evidence, overrides, ledger)
        if confirmed is not None:
            made.append(confirmed.row())
        label, state_labels = names_of(naming)
        out.append({
            "id": wid, "natural_key": key, "states": w["states"], "transitions": w["transitions"],
            "omitted_transitions": w["omitted_transitions"], "applies_to": w["applies_to"], "evidence": w["evidence"],
            "label": label, "state_labels": state_labels, "naming": {"decision_ref": naming["decision_id"], "abstained": naming["abstained"], "abstain_reason": naming["abstain_reason"]},
            "status": "rejected" if confirmed and confirmed.selected_label == "rejected" else "confirmed" if confirmed else "hypothesis",
            "confirmation_ref": confirmed.decision_id if confirmed else None,
        })
    return out, made
