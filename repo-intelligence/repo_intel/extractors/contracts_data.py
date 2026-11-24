"""Contracts and data definitions: OpenAPI documents and SQL DDL.

An OpenAPI document yields an Interface and one non-compiled
"contract_operation" observation per operation. It never yields an Endpoint:
a declared operation is not an implemented one, and the cross-file linker
decides which operations have an implementation.

SQL files are parsed statement by statement with sqlglot (never executed).
CREATE / ALTER / DROP TABLE statements are replayed in order, and the tables
and fields that exist at the end of the file are declared: a table created
and later dropped in the same file is not, a column added by ALTER is. CREATE
VIEW yields a DataSchema; DML and everything else is ignored.

A SQL migration (Prisma `migrations/<name>/migration.sql`, Flyway
`V<version>__<description>.sql`, or any .sql file under a `migrations/` or
`migrate/` directory) is a Migration entity with one MIGRATES edge per
operation, and declares no tables itself: one migration cannot know what a
later one drops. It emits "migration_op" observations instead, and the linker
replays every migration of the unit in revision order and declares the
schema that results.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from ..ontology import EntityKind, EvidenceClass, Predicate
from .base import Extraction, diagnostic, entity, obs, ref, relation, span, whole_file
from .infrastructure import YAML_ERRORS, YAML_SUFFIXES, is_compose, is_workflow, load_yaml_documents, search_span

E1 = EvidenceClass.E1
K = EntityKind

# --------------------------------------------------------------------------- OpenAPI

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SPEC_KEY = r"""["']?(?:openapi|swagger)["']?\s*:"""
_YAML_MARKER = re.compile(rf"^{_SPEC_KEY}".encode(), re.M)
_JSON_MARKER = re.compile(rb'"(?:openapi|swagger)"\s*:')


class OpenAPI:
    id = "contract.openapi"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        low = path.lower()
        return low.endswith(YAML_SUFFIXES + (".json",)) and not is_workflow(path) and not is_compose(path)

    def extract(self, path, data, blob):
        is_json = path.lower().endswith(".json")
        # Cheap pre-check so unrelated YAML/JSON (tsconfig with comments, schemas, ...) is neither parsed nor reported on.
        if not (_JSON_MARKER if is_json else _YAML_MARKER).search(data):
            return Extraction()
        try:
            doc = json.loads(data.decode("utf-8")) if is_json else (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:  # json.JSONDecodeError is a ValueError
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable contract: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict) or not ("openapi" in doc or "swagger" in doc):
            return Extraction()
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        title = info.get("title") if isinstance(info.get("title"), str) and info.get("title") else posixpath.basename(path)
        version = info.get("version")
        at = search_span(data, rf"""({_SPEC_KEY}[ \t]*["']?[\w.\-]*["']?)""", group=1) or whole_file(data)
        out = [entity(blob, at, K.INTERFACE, {"path": path, "title": title}, title, {"spec": "openapi", "version": str(version) if version is not None else None}, E1)]
        paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
        section = search_span(data, r"""["']?paths["']?\s*:""")
        cursor = section.end_byte if section else 0
        for route, item in paths.items():
            if not isinstance(route, str) or not isinstance(item, dict):
                continue
            route_at = search_span(data, rf"""["']?{re.escape(route)}["']?\s*:""", cursor)
            for method in HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                op_at = search_span(data, rf"""["']?\b{method}\b["']?\s*:""", route_at.end_byte) if route_at else None
                operation_id = op.get("operationId") if isinstance(op.get("operationId"), str) else None
                out.append(obs("contract_operation", blob, op_at or route_at or at, {"method": method.upper(), "route": route, "operation_id": operation_id}, E1))
        return Extraction(out)


# --------------------------------------------------------------------------- SQL DDL

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
_DDL = re.compile(r"\b(create|alter|drop)\b", re.I)
_PRISMA_MIGRATION = re.compile(r"(?:^|/)migrations/([^/]+)/migration\.sql$")
_FLYWAY = re.compile(r"(?:^|/)[VU]([0-9][0-9._]*)__[^/]*\.sql$", re.I)
_MIGRATION_DIR = re.compile(r"(?:^|/)(?:migrations|migrate)/(?:[^/]+/)*[^/]+\.sql$", re.I)


def migration_identity(path: str) -> tuple[str, str] | None:
    """(tool, revision) when a .sql file is a migration, else None."""
    if m := _PRISMA_MIGRATION.search(path):
        return "prisma", m.group(1)
    if m := _FLYWAY.search(path):
        return "flyway", m.group(1).replace("_", ".")
    if _MIGRATION_DIR.search(path):
        return "sql", path
    return None


def split_statements(text: str) -> list[tuple[int, int]]:
    """Character ranges of the statements in a SQL script (without the terminating `;`).

    Understands quoted strings and identifiers, line and block comments and
    PostgreSQL dollar quoting, so a `;` inside a function body does not split.
    """
    out, i, start, n = [], 0, 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            end = text.find(c, i + 1)
            while end != -1 and text[end + 1 : end + 2] == c:  # doubled quote is an escape
                end = text.find(c, end + 2)
            i = n if end == -1 else end + 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif c == "$" and (tag := _DOLLAR_TAG.match(text, i)):
            end = text.find(tag.group(), tag.end())
            i = n if end == -1 else end + len(tag.group())
        elif c == ";":
            out.append((start, i))
            start = i = i + 1
        else:
            i += 1
    out.append((start, n))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _qualified(table: Any) -> str:
    db = table.text("db")
    return table.name if not db or db.lower() == "public" else f"{db}.{table.name}"


class SqlDdl:
    id = "data.sql_ddl"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".sql")

    def extract(self, path, data, blob):
        import sqlglot
        from sqlglot import exp

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        ascii_only = len(text) == len(data)
        to_byte = (lambda i: i) if ascii_only else (lambda i: len(text[:i].encode("utf-8")))

        def at(a: int, b: int):
            return span(data, to_byte(a), to_byte(b))

        def find(pattern: str, a: int, b: int, group: int = 0):
            m = re.compile(pattern, re.I | re.S).search(text, a, b)
            return at(m.start(group), m.end(group)) if m else None

        def name_span(name: str, a: int, b: int, fallback):
            return find(rf"""(?<![\w"`])["`]?{re.escape(name)}["`]?(?![\w"`])""", a, b) or fallback

        identity = migration_identity(path)
        out: list = []
        diagnostics = []
        failed = 0
        # name -> {"at": span, "created": bool, "columns": {column: (span, type or None)}, "dropped": set}
        tables: dict[str, dict[str, Any]] = {}
        ops: list[tuple[Any, str, str, str | None, dict[str, Any]]] = []  # (span, op, table, column, extra)
        created: dict[int, dict[str, tuple[Any, str | None]]] = {}  # op index -> columns of a CREATE TABLE
        alter_types: dict[int, str] = {}  # op index -> new type of an ALTER COLUMN ... TYPE
        first_at = None
        for a, b in split_statements(text):
            m = _DDL.search(text, a, b)
            if not m:  # DML, comments, session settings: nothing to learn, do not even parse
                continue
            a = m.start()
            try:
                tree = sqlglot.parse_one(text[a:b], read="postgres")
            except Exception:  # sqlglot.errors.* and the occasional RecursionError on hostile input
                failed += 1
                diagnostics.append(diagnostic(self.id, "warning", f"unparseable SQL statement at line {text.count(chr(10), 0, a) + 1}"))
                continue
            whole = at(a, b)
            if isinstance(tree, exp.Create):
                kind = (tree.kind or "").upper()
                target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
                if not isinstance(target, exp.Table) or not target.name:
                    continue
                name = _qualified(target)
                head = find(rf"""create\b.*?["`]?{re.escape(target.name)}["`]?""", a, b) or whole
                if kind == "TABLE":
                    if tree.find(exp.TemporaryProperty):
                        continue
                    first_at = first_at or head
                    columns: dict[str, tuple[Any, str | None]] = {}
                    cursor = head.end_byte if ascii_only else len(data[: head.end_byte].decode("utf-8"))
                    for column in (c for c in (tree.this.expressions if isinstance(tree.this, exp.Schema) else []) if isinstance(c, exp.ColumnDef) and c.name):
                        col_at = find(rf"""(?<![\w"`])["`]?{re.escape(column.name)}["`]?(?![\w"`])""", cursor, b)
                        if col_at and ascii_only:
                            cursor = col_at.end_byte
                        dtype = column.args.get("kind")
                        columns[column.name] = (col_at or head, dtype.sql(dialect="postgres") if dtype is not None else None)
                    tables[name] = {"at": head, "created": True, "columns": columns, "dropped": set()}
                    created[len(ops)] = dict(columns)
                    ops.append((head, "create_table", name, None, {}))
                elif kind == "VIEW":
                    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
                    reads = sorted({_qualified(t) for t in tree.find_all(exp.Table) if t is not target and t.name and not (t.name in ctes and not t.text("db"))})
                    attributes = {"reads": reads}
                    if tree.find(exp.MaterializedProperty):
                        attributes["materialized"] = True
                    out.append(entity(blob, head, K.DATA_SCHEMA, {"kind": "view", "name": name}, name, attributes))
            elif isinstance(tree, exp.Alter) and (tree.args.get("kind") or "").upper() == "TABLE" and isinstance(tree.this, exp.Table) and tree.this.name:
                name = _qualified(tree.this)
                first_at = first_at or whole
                state = tables.setdefault(name, {"at": whole, "created": False, "columns": {}, "dropped": set()})
                for action in tree.args.get("actions") or []:
                    if isinstance(action, exp.ColumnDef) and action.name:
                        dtype = action.args.get("kind")
                        state["columns"][action.name] = (name_span(action.name, a, b, whole), dtype.sql(dialect="postgres") if dtype is not None else None)
                        state["dropped"].discard(action.name)
                        ops.append((whole, "add_column", name, action.name, {"type": state["columns"][action.name][1]}))
                    elif isinstance(action, exp.Drop) and (action.args.get("kind") or "").upper() == "COLUMN":
                        for column in (c for c in action.args.get("tables") or [] if c.name):
                            state["columns"].pop(column.name, None)
                            state["dropped"].add(column.name)
                            ops.append((whole, "drop_column", name, column.name, {}))
                    elif isinstance(action, exp.RenameColumn) and action.this is not None and action.args.get("to") is not None:
                        old, new = action.this.name, action.args["to"].name
                        kept = state["columns"].pop(old, (None, None))
                        state["columns"][new] = (name_span(new, a, b, whole), kept[1])
                        state["dropped"].add(old)
                        ops.append((whole, "rename_column", name, old, {"new_column_name": new}))
                    elif isinstance(action, exp.AlterColumn) and action.name:
                        dtype = action.args.get("dtype")
                        if action.name in state["columns"] and dtype is not None:
                            state["columns"][action.name] = (state["columns"][action.name][0], dtype.sql(dialect="postgres"))
                        if dtype is not None:
                            alter_types[len(ops)] = dtype.sql(dialect="postgres")
                        ops.append((whole, "alter_column", name, action.name, {}))
                    elif isinstance(action, exp.AlterRename) and isinstance(action.this, exp.Table) and action.this.name:
                        new = _qualified(action.this)
                        tables[new] = {**tables.pop(name), "at": whole}
                        ops.append((whole, "rename_table", name, None, {"new_table_name": new}))
                        name, state = new, tables[new]
            elif isinstance(tree, exp.Drop) and (tree.args.get("kind") or "").upper() == "TABLE":
                first_at = first_at or whole
                for t in (t for t in tree.args.get("tables") or [] if isinstance(t, exp.Table) and t.name):
                    tables.pop(_qualified(t), None)
                    ops.append((whole, "drop_table", _qualified(t), None, {}))

        for name, state in ({} if identity is not None else tables).items():
            table = ref(K.TABLE, {"datastore": "default", "name": name}, name, declare=state["created"])
            if state["created"]:
                out.append(entity(blob, state["at"], K.TABLE, table["natural_key"], name, {}))
            for column, (col_at, dtype) in state["columns"].items():
                field_key = {"table": name, "name": column}
                out.append(entity(blob, col_at, K.FIELD, field_key, column, {"type": dtype} if dtype else {}))
                out.append(relation(blob, col_at, table, Predicate.CONTAINS, ref(K.FIELD, field_key, column)))
        if identity is not None and ops:
            tool, revision = identity
            me = ref(K.MIGRATION, {"tool": tool, "revision": revision}, revision)
            out.append(entity(blob, first_at or whole_file(data), K.MIGRATION, me["natural_key"], revision, {"operations": len(ops)}, E1))
            for seq, (where, op, name, column, extra) in enumerate(ops):
                target_name = extra.get("new_table_name", name)
                qualifiers = {"op": op, **({"column": column} if column else {}), **{k: v for k, v in extra.items() if v is not None}}
                # Refer without declaring: the linker declares what exists after every migration ran.
                out.append(relation(blob, where, me, Predicate.MIGRATES, ref(K.TABLE, {"datastore": "default", "name": target_name}, target_name, declare=False), qualifiers, E1))
                field = extra.get("new_column_name") or column
                if field:
                    out.append(relation(blob, where, me, Predicate.MIGRATES, ref(K.FIELD, {"table": target_name, "name": field}, field, declare=False), qualifiers, E1))
                step = {"tool": tool, "revision": revision, "seq": seq, "op": op, "table": name, **({"column": column} if column else {}), **extra}
                if op == "create_table":
                    step["columns"] = {c: t for c, (_, t) in created[seq].items()}
                elif op == "alter_column" and alter_types.get(seq):
                    step["type"] = alter_types[seq]
                out.append(obs("migration_op", blob, where, {k: v for k, v in step.items() if k != "type" or v}, E1))
        parsed = not (failed and not out)
        return Extraction(out, diagnostics, parsed=parsed)


EXTRACTORS = [OpenAPI(), SqlDdl()]
