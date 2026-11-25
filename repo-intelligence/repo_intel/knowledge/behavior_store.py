"""Behaviour of one entity, read from a snapshot's shards, and the facts it states.

`unit.compile` stores each `behavior` observation (repo_intel.compile.behavior) as a row of
its shard's `behaviors` table, keyed by the entity that owns it (a Symbol or Program):

    {"id": "bhv_...", "owner": <entity id>, "artifact": <entity id of the file>,
     "schema": "rkp-behavior/1", "language": ..., "params": [...], "body": [...],
     "kind": "data_step" (only when the pack gave one),
     "evidence_ids": [<evidence id>], "evidence_class": "E2"}

This module loads those rows for an entity and walks their statements to summarise what
the code says it does: the functions and components it calls by name, the SQL it runs
(with the tables a simple scanner finds), what it includes, where it redirects and what it
writes on each output channel. The summary is read from the code as written: a target
held in a variable, or a table name built from text substituted into the SQL, stays
dynamic and is counted, never guessed. Nothing here adds relations to the graph.

Behaviour is function-level detail: an architecture-tier bundle does not commit it
(repo_intel.knowledge.bundle.DETAIL_TABLES), so a fresh clone has none until it compiles.
"""

from __future__ import annotations

import re
from collections import Counter
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
# names; an identifier may be quoted ("x", [x], `x`) and qualified (schema.table). A `:name`
# inside a word (`orders_:region`) stays part of it: the lowering put a substitution there.
_PART = r'(?:"[^"]*"|\[[^\]]*\]|`[^`]*`|[A-Za-z_#&@:$](?:[\w#&@$]|:(?=[A-Za-z_]))*)'
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
_MAIN = ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE", "CREATE")
# Words between CREATE and TABLE or VIEW: CREATE OR REPLACE GLOBAL TEMPORARY TABLE ...
_CREATE_MODIFIERS = frozenset({"OR", "REPLACE", "GLOBAL", "LOCAL", "TEMP", "TEMPORARY", "UNLOGGED", "MATERIALIZED", "RECURSIVE"})
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


_QUOTED = re.compile(r'"[^"]*"|\[[^\]]*\]|`[^`]*`')


def _dynamic(text: str) -> bool:
    """A name built at run time cannot be read from the text: #var# in CFML, &var in SAS, or a
    `:name` parameter that is only part of the name (`orders_:region`, `:schema.orders`)."""
    return "#" in text or "&" in text or ":" in _QUOTED.sub("", text)


def _variable(text: str) -> bool:
    """A host or bind variable on its own (:name, @name): never a table."""
    return re.fullmatch(r"[:@][\w$]+", text) is not None


def _inline(param: Any) -> bool:
    """A parameter whose text is substituted into the SQL before it runs, not bound.

    The pack says so with `"inline": true`. A SAS macro variable is always substituted as
    text (PROC SQL has no bind parameters), so a parameter whose value is one counts too.
    """
    if not isinstance(param, dict) or not isinstance(param.get("name"), str):
        return False
    value = param.get("value")
    return param.get("inline") is True or (isinstance(value, dict) and value.get("e") == "var" and value.get("scope") == "macro")


def mark_inline(sql: str, params: Iterable[Any] = ()) -> str:
    """The SQL with each inline parameter written back as a substitution, `:name` -> `#name#`.

    A lowering writes CFML `orders_#url.region#` and SAS `orders_&region` as
    `orders_:region`; marking the substitution keeps the scanner from reading `orders_` as
    the table, and lists `orders_#region#` as dynamic instead. A placeholder that is longer
    than any parameter's name (`:region_2024` for `#region#_2024`) is matched by its longest
    inline prefix. Bind parameters and `::type` casts are left as they are.
    """
    params = [p for p in params if isinstance(p, dict) and isinstance(p.get("name"), str)]
    inline = {p["name"] for p in params if _inline(p)}
    if not inline:
        return sql
    bound = {p["name"] for p in params} - inline
    longest = sorted(inline, key=lambda n: (-len(n), n))

    def mark(m: re.Match[str]) -> str:
        word = m.group(1)
        if word in inline:
            return f"#{word}#"
        if word in bound:
            return m.group()
        prefix = next((n for n in longest if word.startswith(n)), None)
        return f"#{prefix}#{word[len(prefix):]}" if prefix else m.group()

    return re.sub(r"(?<!:):([A-Za-z_]\w*)", mark, sql)


_CONNECTION_TO = re.compile(r"\bCONNECTION\s+TO\s+(\w+)\s*\(", re.IGNORECASE)
_EXECUTE_BY = re.compile(r"^\s*EXECUTE\s*\(", re.IGNORECASE)
_BY_ENGINE = re.compile(r"\s*BY\s+(\w+)", re.IGNORECASE)


def _close(sql: str, open_at: int) -> int:
    """The index just past the parenthesis that closes the one at `open_at`, skipping quoted text."""
    level, quote, i = 0, None, open_at
    while i < len(sql):
        c = sql[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            level += 1
        elif c == ")":
            level -= 1
            if level == 0:
                return i + 1
        i += 1
    return len(sql)


def _without_passthrough(sql: str) -> tuple[str, list[str], bool]:
    """SAS pass-through SQL with the remote database's text taken out.

    `CONNECTION TO <engine> ( ... )` and `EXECUTE ( ... ) BY <engine>` hold SQL in the remote
    database's own dialect, naming its tables, not SAS ones: they are never read as tables here.
    Returns the local text, the engines named, and whether the whole statement runs remotely.
    """
    engines: list[str] = []
    if _EXECUTE_BY.match(sql):
        end = _close(sql, sql.index("("))
        by = _BY_ENGINE.match(sql, end)
        if by:
            return "", [by.group(1).lower()], True
    while (m := _CONNECTION_TO.search(sql)):
        engines.append(m.group(1).lower())
        sql = sql[:m.start()] + "(SELECT NULL)" + sql[_close(sql, m.end() - 1):]
    return sql, engines, False


def sql_tables(sql: str, params: Iterable[Any] = ()) -> dict[str, Any]:
    """The tables a SQL text names after FROM, JOIN, USING, INTO, UPDATE and CREATE, by a token scan.

    `reads` are tables after FROM, JOIN and USING; `writes` the targets of INSERT/SELECT
    INTO, UPDATE, MERGE INTO, DELETE FROM and CREATE TABLE/VIEW. Names made at run time are
    listed under `dynamic` as written and not resolved: `#x#` and `&x` substitutions, a word
    with a `:name` in it, and every `params` entry marked inline (see `mark_inline`).
    Common table expressions are not tables. `statement` is the main statement's keyword,
    lower-case. SAS pass-through text is the remote database's and is not scanned; the
    engines it goes to are listed under `remote`.
    """
    sql, remote, remote_only = _without_passthrough(sql)
    extra = {"remote": sorted(set(remote))} if remote else {}
    if remote_only:
        return {"statement": "execute", "reads": [], "writes": [], "dynamic": [], **extra}
    toks = _tokens(mark_inline(sql, params))
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
        elif depth == 0 and kind == "ident" and u not in _STOP and (not i or up[i - 1] not in ("TABLE", "VIEW", "EXISTS")):
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
        elif u == "CREATE":
            j = i + 1
            while j < len(toks) and up[j] in _CREATE_MODIFIERS:
                j += 1
            if j < len(toks) and up[j] in ("TABLE", "VIEW"):
                j += 1
                if up[j:j + 3] == ["IF", "NOT", "EXISTS"]:
                    j += 3
                table_at(j, True, as_list=False, call_ok=True)  # a column list may follow the name
    return {"statement": statement.lower(), "reads": sorted(reads), "writes": sorted(writes), "dynamic": sorted(dynamic), **extra}


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


def _origin(unit: Mapping[str, Any], stmt: Mapping[str, Any]) -> dict[str, Any]:
    """Where a fact comes from: the unit's row id, its kind when it has one, and the span."""
    kind = unit.get("kind")
    return {"behavior_id": unit["id"], **({"unit_kind": kind} if isinstance(kind, str) else {}), "at": stmt.get("at")}


def _target(stmt: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, Any]:
    literal = _literal(stmt.get("target"))
    out: dict[str, Any] = _origin(unit, stmt)
    if literal is not None:
        out["target"] = literal
    else:
        out["dynamic"] = True
        out["expression"] = (stmt.get("target") or {}).get("e")
    return out


def summarize(units: list[Mapping[str, Any]]) -> dict[str, Any]:
    """What the behaviour units of one entity state, in source order within each unit.

    Calls are aggregated by callee; queries, includes and redirects are listed with the
    unit (and its `kind`, as `unit_kind`, when it has one) and span they come from; outputs
    are counted by channel and units by kind. Calls, includes and redirects whose target is
    computed are counted as dynamic, not resolved; so are table names a query builds from
    inline parameters (`inline_params`).
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
                params = [p for p in node.get("params", []) if isinstance(p, dict) and isinstance(p.get("name"), str)]
                query: dict[str, Any] = {
                    **_origin(unit, node), "sql": node["sql"],
                    "datasource": node.get("datasource") if isinstance(node.get("datasource"), str) else None,
                    "params": [p["name"] for p in params],
                    "inline_params": [p["name"] for p in params if _inline(p)],
                    **sql_tables(node["sql"], params),
                }
                if isinstance(node.get("target"), dict) and _dotted(node["target"]):
                    query["result"] = _dotted(node["target"])
                queries.append(query)
            elif op == "include":
                includes.append(_target(node, unit))
            elif op == "redirect":
                redirects.append(_target(node, unit))
            elif op == "output":
                channel = node.get("channel") if isinstance(node.get("channel"), str) else "unknown"
                outputs[channel] = outputs.get(channel, 0) + 1
            elif op == "platform":
                constructs[node["construct"]] = constructs.get(node["construct"], 0) + 1
    return {
        "units": len(units),
        "languages": sorted({u["language"] for u in units}),
        "unit_kinds": dict(sorted(Counter(u["kind"] for u in units if isinstance(u.get("kind"), str)).items())),
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
