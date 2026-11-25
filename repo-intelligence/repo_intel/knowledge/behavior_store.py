"""Behaviour of one entity, read from a snapshot's shards, and the facts it states.

`unit.compile` stores each `behavior` observation (repo_intel.compile.behavior) as a row of
its shard's `behaviors` table, keyed by the entity that owns it (a Symbol or Program):

    {"id": "bhv_...", "owner": <entity id>, "artifact": <entity id of the file>,
     "schema": "rkp-behavior/1", "language": ..., "params": [...], "body": [...],
     "evidence_ids": [<evidence id>], "evidence_class": "E2"}

This module loads those rows for an entity and walks their statements to summarise what
the code says it does: the functions and components it calls by name, the SQL it runs
(with the tables a simple scanner finds), what it includes, where it redirects and what it
writes on each output channel. The summary is read from the code as written: a target
held in a variable stays dynamic and is counted, never guessed. Nothing here adds
relations to the graph.

Behaviour is function-level detail: an architecture-tier bundle does not commit it
(repo_intel.knowledge.bundle.DETAIL_TABLES), so a fresh clone has none until it compiles.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from repo_intel.compile.behavior import walk
from repo_intel.compile.contracts import Manifest
from repo_intel.storage.store import ObjectStore

TABLE = "behaviors"


# ------------------------------------------------------------------ loading


def index(shards: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Behaviour rows by owner entity id, each owner's rows ordered by row id."""
    by_owner: dict[str, dict[str, dict[str, Any]]] = {}
    for shard in shards:
        for row in shard.get(TABLE, []):
            by_owner.setdefault(row["owner"], {}).setdefault(row["id"], row)
    return {owner: [rows[i] for i in sorted(rows)] for owner, rows in sorted(by_owner.items())}


def load(store: ObjectStore, manifest: Manifest, entity_id: str) -> list[dict[str, Any]]:
    """The behaviour units the snapshot holds for `entity_id`; empty when it holds none."""
    found: dict[str, dict[str, Any]] = {}
    for ref in manifest.shards:
        for row in store.get_json(ref.digest).get(TABLE, []):
            if row["owner"] == entity_id:
                found.setdefault(row["id"], row)
    return [found[i] for i in sorted(found)]


# ------------------------------------------------------------------ SQL tables

# One SQL token. Strings and comments are matched so that their contents are never read as
# names; an identifier may be quoted ("x", [x], `x`) and qualified (schema.table).
_PART = r'(?:"[^"]*"|\[[^\]]*\]|`[^`]*`|[A-Za-z_#&@:$][\w#&@$]*)'
_SQL_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*|/\*.*?(?:\*/|$))|(?P<string>'(?:[^']|'')*'?)"
    rf"|(?P<ident>{_PART}(?:\s*\.\.?\s*{_PART})*)|(?P<punct>[(),;])|(?P<other>.)",
    re.S,
)
# Words that end a table list or cannot be a table name.
_STOP = frozenset("""
    WHERE GROUP ORDER HAVING LIMIT OFFSET FETCH UNION EXCEPT INTERSECT MINUS WINDOW RETURNING
    JOIN INNER LEFT RIGHT FULL CROSS OUTER NATURAL ON USING SET VALUES VALUE SELECT WHEN THEN
    FOR AS WITH LATERAL ONLY DEFAULT OUTPUT FROM INTO UPDATE DELETE INSERT MERGE TABLE TOP
""".split())
_MAIN = ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE")
# Functions whose argument list uses FROM: EXTRACT(YEAR FROM d), TRIM(x FROM y), ...
_FROM_FUNCTIONS = frozenset({"EXTRACT", "SUBSTRING", "TRIM", "POSITION", "OVERLAY"})
_PSEUDO_TABLES = frozenset({"DUAL"})


def _tokens(sql: str) -> list[tuple[str, str]]:
    out = []
    for m in _SQL_TOKEN.finditer(sql):
        kind = m.lastgroup or "other"
        if kind in ("space", "comment"):
            continue
        out.append((kind, m.group()))
    return out


def _name(text: str) -> str:
    """The name without quoting and without spaces around its dots (`db..t` keeps both dots)."""
    unquoted = re.sub(_PART, lambda m: m.group()[1:-1] if m.group()[:1] in '"[`' else m.group(), text)
    return re.sub(r"\s*(\.\.?)\s*", r"\1", unquoted)


def _dynamic(text: str) -> bool:
    """A name built at run time (#var# in CFML, &var in SAS) cannot be read from the text."""
    return "#" in text or "&" in text


def _variable(text: str) -> bool:
    """A host or bind variable (:name, @name): never a table."""
    return text[:1] in (":", "@")


def sql_tables(sql: str) -> dict[str, Any]:
    """The tables a SQL text names after FROM, JOIN, USING, INTO and UPDATE, by a token scan.

    `reads` are tables after FROM, JOIN and USING; `writes` the targets of INSERT/SELECT
    INTO, UPDATE, MERGE INTO and DELETE FROM. Names made at run time are listed under
    `dynamic` as written and not resolved. Common table expressions are not tables.
    `statement` is the main statement's keyword, lower-case.
    """
    toks = _tokens(sql)
    up = [t.upper() if k == "ident" else t for k, t in toks]

    # The main statement, and the names of common table expressions (`name [(cols)] AS (`).
    statement = next((u for (k, _), u in zip(toks, up) if k == "ident" and u in _MAIN), up[0].lower() if up and toks[0][0] == "ident" else "")
    ctes: set[str] = set()
    depth = 0
    for i, ((kind, text), u) in enumerate(zip(toks, up)):
        if text == "(":
            depth += 1
        elif text == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and kind == "ident" and u not in _STOP:
            j = i + 1
            if j < len(toks) and toks[j][1] == "(":  # an optional column list
                level = 0
                while j < len(toks):
                    level += {"(": 1, ")": -1}.get(toks[j][1], 0)
                    j += 1
                    if level == 0:
                        break
            if j + 1 < len(toks) and up[j] == "AS" and toks[j + 1][1] == "(":
                ctes.add(_name(text).upper())

    reads: set[str] = set()
    writes: set[str] = set()
    dynamic: set[str] = set()
    stack: list[str] = []  # for each open parenthesis, the word before it
    depth = 0
    delete_target_open = statement.upper() == "DELETE"

    def table_at(j: int, write: bool, as_list: bool, call_ok: bool) -> None:
        while j < len(toks):
            kind, text = toks[j]
            if kind != "ident" or up[j] in _STOP:
                return
            if not call_ok and j + 1 < len(toks) and toks[j + 1][1] == "(":
                # A table function, not a table: step over its arguments to the next item.
                j, level = j + 1, 0
                while j < len(toks):
                    level += {"(": 1, ")": -1}.get(toks[j][1], 0)
                    if level == 0:
                        break
                    j += 1
            else:
                name = _name(text)
                if _variable(text):
                    pass
                elif _dynamic(text):
                    dynamic.add(text)
                elif name.upper() not in ctes and name.upper() not in _PSEUDO_TABLES:
                    (writes if write else reads).add(name)
            if not as_list:
                return
            j += 1  # an alias, then perhaps a comma and another table
            if j < len(toks) and up[j] == "AS":
                j += 1
            if j < len(toks) and toks[j][0] == "ident" and up[j] not in _STOP:
                j += 1
            if j < len(toks) and toks[j][1] == ",":
                j += 1
                continue
            return

    for i, ((kind, text), u) in enumerate(zip(toks, up)):
        if text == "(":
            stack.append(up[i - 1] if i and toks[i - 1][0] == "ident" else "")
            depth += 1
            continue
        if text == ")":
            if stack:
                stack.pop()
            depth = max(0, depth - 1)
            continue
        if kind != "ident":
            continue
        prev = up[i - 1] if i else ""
        nxt = up[i + 1] if i + 1 < len(toks) else ""
        if u == "FROM":
            if prev == "DISTINCT" or (stack and stack[-1] in _FROM_FUNCTIONS):
                continue
            write = delete_target_open and depth == 0
            delete_target_open = False if depth == 0 else delete_target_open
            table_at(i + 1, write, as_list=True, call_ok=False)
        elif u == "JOIN":
            table_at(i + 1, False, as_list=False, call_ok=False)
        elif u == "USING" and nxt != "(":
            table_at(i + 1, False, as_list=False, call_ok=False)
        elif u == "INTO":
            table_at(i + 1, True, as_list=False, call_ok=True)
        elif u == "UPDATE" and nxt != "SET" and prev not in ("FOR", "KEY", "THEN", "ON"):
            table_at(i + 1, True, as_list=False, call_ok=False)
    return {"statement": statement.lower(), "reads": sorted(reads), "writes": sorted(writes), "dynamic": sorted(dynamic)}


# ------------------------------------------------------------------ facts from behaviour


def _dotted(node: Mapping[str, Any]) -> str | None:
    """`a.b.c` for a member chain of names and variables; None when any part is computed."""
    if node.get("e") in ("name", "var") and isinstance(node.get("name"), str):
        return node["name"]
    if node.get("e") == "member" and isinstance(node.get("name"), str) and isinstance(node.get("object"), dict):
        base = _dotted(node["object"])
        return f"{base}.{node['name']}" if base is not None else None
    return None


def _literal(node: Any) -> str | None:
    """The text of a literal string, or of a template with no interpolated part."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    if node.get("e") == "lit" and isinstance(node.get("value"), str):
        return node["value"]
    if node.get("e") == "template" and all(isinstance(p, str) for p in node.get("parts", [])):
        return "".join(node["parts"])
    return None


def _callee(node: Mapping[str, Any]) -> tuple[str, str] | None:
    """(callee, kind) for a call or `new` whose target is written out in the source."""
    if node.get("e") == "new":
        name = _literal(node.get("type"))
        return (name, "new") if name else None
    callee = node.get("callee")
    if not isinstance(callee, dict):
        return None
    if callee.get("e") == "name" and isinstance(callee.get("name"), str):
        return callee["name"], callee.get("kind") or "function"
    if callee.get("e") == "member":
        dotted = _dotted(callee)
        return (dotted, "method") if dotted else None
    name = _literal(callee)
    return (name, "function") if name else None


def _target(stmt: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    literal = _literal(stmt.get("target"))
    out: dict[str, Any] = {"behavior_id": unit_id, "at": stmt.get("at")}
    if literal is not None:
        out["target"] = literal
    else:
        out["dynamic"] = True
        out["expression"] = (stmt.get("target") or {}).get("e")
    return out


def summarize(units: list[Mapping[str, Any]]) -> dict[str, Any]:
    """What the behaviour units of one entity state, in source order within each unit.

    Calls are aggregated by callee; queries, includes and redirects are listed with the
    unit and span they come from; outputs are counted by channel. Calls, includes and
    redirects whose target is computed are counted as dynamic, not resolved.
    """
    calls: dict[tuple[str, str], int] = {}
    dynamic_calls = 0
    queries: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []
    redirects: list[dict[str, Any]] = []
    outputs: dict[str, int] = {}
    constructs: dict[str, int] = {}
    statements = 0
    for unit in units:
        for node in walk(unit.get("body", [])):
            op, e = node.get("op"), node.get("e")
            if op is not None:
                statements += 1
            if e in ("call", "new"):
                found = _callee(node)
                if found is None:
                    dynamic_calls += 1
                else:
                    calls[found] = calls.get(found, 0) + 1
            elif e == "platform":
                constructs[node["construct"]] = constructs.get(node["construct"], 0) + 1
            if op == "query":
                found_tables = sql_tables(node["sql"])
                query: dict[str, Any] = {
                    "behavior_id": unit["id"], "at": node.get("at"), "sql": node["sql"],
                    "datasource": node.get("datasource") if isinstance(node.get("datasource"), str) else None,
                    "params": [p["name"] for p in node.get("params", []) if isinstance(p, dict) and isinstance(p.get("name"), str)],
                    **found_tables,
                }
                if isinstance(node.get("target"), dict) and _dotted(node["target"]):
                    query["result"] = _dotted(node["target"])
                queries.append(query)
            elif op == "include":
                includes.append(_target(node, unit["id"]))
            elif op == "redirect":
                redirects.append(_target(node, unit["id"]))
            elif op == "output":
                channel = node.get("channel") if isinstance(node.get("channel"), str) else "unknown"
                outputs[channel] = outputs.get(channel, 0) + 1
            elif op == "platform":
                constructs[node["construct"]] = constructs.get(node["construct"], 0) + 1
    return {
        "units": len(units),
        "languages": sorted({u["language"] for u in units}),
        "statements": statements,
        "calls": [{"callee": name, "kind": kind, "count": n} for (name, kind), n in sorted(calls.items())],
        "dynamic_calls": dynamic_calls,
        "queries": queries,
        "datasources": sorted({q["datasource"] for q in queries if q["datasource"]}),
        "tables": {
            "reads": sorted({t for q in queries for t in q["reads"]}),
            "writes": sorted({t for q in queries for t in q["writes"]}),
            "dynamic": sorted({t for q in queries for t in q["dynamic"]}),
        },
        "includes": includes,
        "redirects": redirects,
        "outputs": dict(sorted(outputs.items())),
        "platform_constructs": dict(sorted(constructs.items())),
    }


def behavior_of(store: ObjectStore, manifest: Manifest, entity_id: str) -> dict[str, Any] | None:
    """The units and summary for `entity_id`, or None when the snapshot holds no behaviour for it."""
    units = load(store, manifest, entity_id)
    if not units:
        return None
    return {"entity_id": entity_id, "units": units, "summary": summarize(units)}
