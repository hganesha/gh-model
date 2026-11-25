"""The behavioural IR: one statement and expression form that every platform lowers into.

A platform pack parses a function, page, DATA step or macro and emits one `behavior`
observation (`behavior_observation`) whose payload is a BehaviorUnit:

    {"owner": <entity ref of the Symbol or Program it belongs to>,
     "language": "cfml" | "sas" | ...,
     "params": [{"name": str, "type": <type>, "required": bool, "default": <expr> | None}],
     "body": [<statement>, ...]}

Statements and expressions are JSON objects with an "op" (statements) or "e"
(expressions) field. Each may carry "at": [start_byte, end_byte] into the source file,
so every node is evidence-backed. Nothing here runs code; it records what code says.

The form is structured (if/loop/try nest), not a control-flow graph: a graph, data flow
and SQL lineage are derived from it. A construct with no neutral equivalent is kept as
`{"op": "platform", "construct": "sas:retain", ...}` (or `{"e": "platform", ...}`), named
by its platform-pack construct id, so nothing is dropped and consumers can see what they
do not handle.

`validate_unit` is the contract: packs run it in their tests on everything they emit.
docs/solution-ir-contract.md describes each node with an example.
"""

from __future__ import annotations

from typing import Any

from repo_intel.compile.contracts import Observation, Span
from repo_intel.compile.ontology import EvidenceClass

SCHEMA = "rkp-behavior/1"
TYPES = frozenset({"string", "number", "integer", "boolean", "date", "datetime", "binary", "array", "struct", "query", "dataset", "any", "null", "unknown"})
# Statement ops, with their required fields and which fields hold statement lists or expressions.
STATEMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "assign": {"required": ("target", "value"), "exprs": ("target", "value")},
    "declare": {"required": ("name",), "exprs": ("value",)},  # var/local/RETAIN-less declaration; "scope" optional
    "call": {"required": ("call",), "exprs": ("call",)},  # an expression evaluated for its effect
    "if": {"required": ("cond", "then"), "exprs": ("cond",), "blocks": ("then", "else")},
    "switch": {"required": ("on", "cases"), "exprs": ("on",), "blocks": ("default",)},  # cases: [{"match": [expr], "body": [stmt]}]
    "loop": {"required": ("kind", "body"), "exprs": ("cond", "from", "to", "step", "over", "var"), "blocks": ("body",)},  # kind: for|while|until|each|range|implicit
    "break": {"required": ()},
    "continue": {"required": ()},
    "return": {"required": (), "exprs": ("value",)},
    "throw": {"required": (), "exprs": ("value",)},
    "try": {"required": ("body",), "blocks": ("body", "finally")},  # catches: [{"type": str, "var": str, "body": [stmt]}]
    "query": {"required": ("sql",), "exprs": ("target",)},  # sql: text with parameters as :name; params: [{"name", "value": expr, "type"}]; datasource: str
    "output": {"required": ("channel",), "exprs": ("value",)},  # channel: response|log|file|report|dataset|mail
    "include": {"required": ("target",), "exprs": ("target",)},  # a template or %include; dynamic when target is not a literal
    "redirect": {"required": ("target",), "exprs": ("target",)},  # cflocation, a response redirect
    "transaction": {"required": ("body",), "blocks": ("body",)},  # action: begin|commit|rollback optional
    "lock": {"required": ("body",), "blocks": ("body",)},  # name, scope, kind: exclusive|readonly
    "exit": {"required": ()},  # stop the request, program or step
    "platform": {"required": ("construct",), "blocks": ("body",)},  # anything else, named by construct id
}
EXPRESSIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "lit": {"required": ("value",)},  # "type" recommended
    "var": {"required": ("name",)},  # "scope": session|application|request|form|url|cgi|local|arguments|variables|this|macro|work|...
    "member": {"required": ("object", "name"), "exprs": ("object",)},
    "index": {"required": ("object", "index"), "exprs": ("object", "index")},
    "call": {"required": ("callee",), "exprs": ("callee",)},  # args: [expr]; named: {name: expr}; callee is an expression or {"e": "name", "name": ...}
    "name": {"required": ("name",)},  # a function, component or macro named directly: "kind": builtin|function|component|macro|procedure
    "binop": {"required": ("op", "left", "right"), "exprs": ("left", "right")},
    "unop": {"required": ("op", "operand"), "exprs": ("operand",)},
    "cond": {"required": ("test", "then", "else"), "exprs": ("test", "then", "else")},
    "new": {"required": ("type",)},  # createObject / new Component(); "args" optional
    "array": {"required": ("items",)},
    "struct": {"required": ("fields",)},  # fields: {name: expr}
    "lambda": {"required": ("body",), "blocks": ("body",)},
    "template": {"required": ("parts",)},  # string interpolation (#x# in CFML, &x in SAS): parts: [str | expr]
    "platform": {"required": ("construct",)},
}
BINARY_OPS = frozenset({"+", "-", "*", "/", "%", "^", "\\", "&", "==", "!=", "<", "<=", ">", ">=", "and", "or", "xor", "eqv", "imp", "in", "contains", "not_contains", "like", "min", "max"})
UNARY_OPS = frozenset({"-", "+", "not", "exists", "missing"})


def _span_ok(at: Any) -> bool:
    return isinstance(at, list) and len(at) == 2 and all(isinstance(x, int) and x >= 0 for x in at) and at[0] <= at[1]


def _check_expr(node: Any, path: str, problems: list[str]) -> None:
    if not isinstance(node, dict) or node.get("e") not in EXPRESSIONS:
        problems.append(f"{path}: not an expression ({node!r:.60})")
        return
    spec = EXPRESSIONS[node["e"]]
    for f in spec.get("required", ()):
        if f not in node:
            problems.append(f"{path}: {node['e']} needs {f!r}")
    if "at" in node and not _span_ok(node["at"]):
        problems.append(f"{path}: bad span {node['at']!r}")
    if "type" in node and node["e"] == "lit" and node["type"] not in TYPES:
        problems.append(f"{path}: unknown type {node['type']!r}")
    if node["e"] == "binop" and node.get("op") not in BINARY_OPS:
        problems.append(f"{path}: unknown binary op {node.get('op')!r}")
    if node["e"] == "unop" and node.get("op") not in UNARY_OPS:
        problems.append(f"{path}: unknown unary op {node.get('op')!r}")
    for f in spec.get("exprs", ()):
        if f in node and node[f] is not None:
            _check_expr(node[f], f"{path}.{f}", problems)
    for f in spec.get("blocks", ()):
        if f in node:
            _check_block(node[f], f"{path}.{f}", problems)
    for i, arg in enumerate(node.get("args", []) if node["e"] in ("call", "new") else []):
        _check_expr(arg, f"{path}.args[{i}]", problems)
    for k, arg in (node.get("named", {}) if node["e"] == "call" else {}).items():
        _check_expr(arg, f"{path}.named.{k}", problems)
    for i, item in enumerate(node.get("items", []) if node["e"] == "array" else []):
        _check_expr(item, f"{path}.items[{i}]", problems)
    for k, item in (node.get("fields", {}) if node["e"] == "struct" else {}).items():
        _check_expr(item, f"{path}.fields.{k}", problems)
    for i, part in enumerate(node.get("parts", []) if node["e"] == "template" else []):
        if not isinstance(part, str):
            _check_expr(part, f"{path}.parts[{i}]", problems)


def _check_stmt(node: Any, path: str, problems: list[str]) -> None:
    if not isinstance(node, dict) or node.get("op") not in STATEMENTS:
        problems.append(f"{path}: not a statement ({node!r:.60})")
        return
    spec = STATEMENTS[node["op"]]
    for f in spec.get("required", ()):
        if f not in node:
            problems.append(f"{path}: {node['op']} needs {f!r}")
    if "at" in node and not _span_ok(node["at"]):
        problems.append(f"{path}: bad span {node['at']!r}")
    for f in spec.get("exprs", ()):
        if node.get(f) is not None:
            _check_expr(node[f], f"{path}.{f}", problems)
    for f in spec.get("blocks", ()):
        if f in node:
            _check_block(node[f], f"{path}.{f}", problems)
    if node["op"] == "switch":
        for i, case in enumerate(node.get("cases", [])):
            for j, m in enumerate(case.get("match", [])):
                _check_expr(m, f"{path}.cases[{i}].match[{j}]", problems)
            _check_block(case.get("body", []), f"{path}.cases[{i}].body", problems)
    if node["op"] == "try":
        for i, catch in enumerate(node.get("catches", [])):
            _check_block(catch.get("body", []), f"{path}.catches[{i}].body", problems)
    if node["op"] == "query":
        if not isinstance(node.get("sql"), str):
            problems.append(f"{path}: query sql must be text")
        for i, param in enumerate(node.get("params", [])):
            if "value" in param:
                _check_expr(param["value"], f"{path}.params[{i}].value", problems)
    if node["op"] == "loop" and node.get("kind") not in ("for", "while", "until", "each", "range", "implicit"):
        problems.append(f"{path}: unknown loop kind {node.get('kind')!r}")


def _check_block(block: Any, path: str, problems: list[str]) -> None:
    if not isinstance(block, list):
        problems.append(f"{path}: a block must be a list of statements")
        return
    for i, stmt in enumerate(block):
        _check_stmt(stmt, f"{path}[{i}]", problems)


def validate_unit(unit: Any) -> list[str]:
    """Every way `unit` breaks the behavioural IR contract; empty when it is valid."""
    problems: list[str] = []
    if not isinstance(unit, dict):
        return ["a behaviour unit must be an object"]
    owner = unit.get("owner")
    if not isinstance(owner, dict) or not {"kind", "natural_key", "name"} <= set(owner):
        problems.append("owner must be an entity reference {kind, natural_key, name}")
    if not isinstance(unit.get("language"), str):
        problems.append("language must be text")
    for i, param in enumerate(unit.get("params", [])):
        if not isinstance(param, dict) or not isinstance(param.get("name"), str):
            problems.append(f"params[{i}]: needs a name")
        elif param.get("type", "any") not in TYPES:
            problems.append(f"params[{i}]: unknown type {param.get('type')!r}")
        elif param.get("default") is not None:
            _check_expr(param["default"], f"params[{i}].default", problems)
    _check_block(unit.get("body"), "body", problems)
    return problems


def behavior_observation(blob: str, at: Span, owner: dict[str, Any], language: str, body: list[dict[str, Any]], params: list[dict[str, Any]] | None = None, evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """One unit of behaviour for `owner` (a Symbol or Program ref), validated before it is emitted."""
    from repo_intel.extractors.base import obs

    unit = {"schema": SCHEMA, "owner": owner, "language": language, "params": params or [], "body": body}
    problems = validate_unit(unit)
    if problems:
        raise ValueError("invalid behaviour unit: " + "; ".join(problems[:5]))
    return obs("behavior", blob, at, unit, evidence_class)


def walk(block: list[dict[str, Any]]):
    """Every statement and expression node under `block`, depth first: for consumers and tests."""
    for stmt in block:
        yield stmt
        spec = STATEMENTS.get(stmt.get("op"), {})
        for f in spec.get("exprs", ()):
            if stmt.get(f) is not None:
                yield from _walk_expr(stmt[f])
        for f in spec.get("blocks", ()):
            yield from walk(stmt.get(f, []))
        for case in stmt.get("cases", []):
            yield from walk(case.get("body", []))
        for catch in stmt.get("catches", []):
            yield from walk(catch.get("body", []))


def _walk_expr(node: dict[str, Any]):
    yield node
    spec = EXPRESSIONS.get(node.get("e"), {})
    for f in spec.get("exprs", ()):
        if node.get(f) is not None:
            yield from _walk_expr(node[f])
    for f in spec.get("blocks", ()):
        yield from walk(node.get(f, []))
    for arg in node.get("args", []):
        yield from _walk_expr(arg)
    for arg in node.get("named", {}).values():
        yield from _walk_expr(arg)
    for item in node.get("items", []):
        yield from _walk_expr(item)
    for item in node.get("fields", {}).values():
        yield from _walk_expr(item)
    for part in node.get("parts", []):
        if isinstance(part, dict):
            yield from _walk_expr(part)
