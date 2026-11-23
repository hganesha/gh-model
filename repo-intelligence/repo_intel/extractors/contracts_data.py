"""Contracts and data definitions: OpenAPI documents and SQL DDL.

An OpenAPI document yields an Interface and one non-compiled
"contract_operation" observation per operation. It never yields an Endpoint:
a declared operation is not an implemented one, and the cross-file linker
decides which operations have an implementation.

SQL files are parsed statement by statement with sqlglot (never executed).
CREATE TABLE yields a Table and its Fields, CREATE VIEW a DataSchema; DML and
everything else is ignored.
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
_CREATE = re.compile(r"\bcreate\b", re.I)


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
    version = "1.0.0"

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

        out: list = []
        diagnostics = []
        failed = 0
        statements = split_statements(text)
        for a, b in statements:
            m = _CREATE.search(text, a, b)
            if not m:  # DML, comments, session settings: nothing to learn, do not even parse
                continue
            a = m.start()
            try:
                tree = sqlglot.parse_one(text[a:b], read="postgres")
            except Exception:  # sqlglot.errors.* and the occasional RecursionError on hostile input
                failed += 1
                diagnostics.append(diagnostic(self.id, "warning", f"unparseable SQL statement at line {text.count(chr(10), 0, a) + 1}"))
                continue
            if not isinstance(tree, exp.Create):
                continue
            kind = (tree.kind or "").upper()
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if not isinstance(target, exp.Table) or not target.name:
                continue
            name = _qualified(target)
            head = find(rf"""create\b.*?["`]?{re.escape(target.name)}["`]?""", a, b) or at(a, b)
            if kind == "TABLE":
                if tree.find(exp.TemporaryProperty):
                    continue
                key = {"datastore": "default", "name": name}
                table = ref(K.TABLE, key, name)
                out.append(entity(blob, head, K.TABLE, key, name, {}))
                cursor = head.end_byte if ascii_only else len(data[: head.end_byte].decode("utf-8"))
                columns = tree.this.expressions if isinstance(tree.this, exp.Schema) else []
                for column in (c for c in columns if isinstance(c, exp.ColumnDef) and c.name):
                    col_at = find(rf"""(?<![\w"`])["`]?{re.escape(column.name)}["`]?(?![\w"`])""", cursor, b)
                    if col_at and ascii_only:
                        cursor = col_at.end_byte
                    col_at = col_at or head
                    field_key = {"table": name, "name": column.name}
                    dtype = column.args.get("kind")
                    out.append(entity(blob, col_at, K.FIELD, field_key, column.name, {"type": dtype.sql(dialect="postgres")} if dtype is not None else {}))
                    out.append(relation(blob, col_at, table, Predicate.CONTAINS, ref(K.FIELD, field_key, column.name)))
            elif kind == "VIEW":
                ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
                reads = sorted({_qualified(t) for t in tree.find_all(exp.Table) if t is not target and t.name and not (t.name in ctes and not t.text("db"))})
                attributes = {"reads": reads}
                if tree.find(exp.MaterializedProperty):
                    attributes["materialized"] = True
                out.append(entity(blob, head, K.DATA_SCHEMA, {"kind": "view", "name": name}, name, attributes))
        parsed = not (failed and not out)
        return Extraction(out, diagnostics, parsed=parsed)


EXTRACTORS = [OpenAPI(), SqlDdl()]
