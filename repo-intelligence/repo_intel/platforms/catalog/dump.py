"""The `catalog.dump` extractor: one catalogue dump file in, Table, Field, view, routine,
role and permission observations out (see the package docstring for the keys).

Two file shapes, both under `.rkp/inputs/catalog/`:

JSON, one object holding one array per information_schema view, named as the view is
(`tables`, `columns`, `views`, `routines`, `table_privileges`, ...), plus optional
`database` and `dialect` names:

    {"database": "shopdb", "dialect": "postgres",
     "tables": [{"table_schema": "public", "table_name": "orders", "table_type": "BASE TABLE"}],
     "columns": [{"table_schema": "public", "table_name": "orders", "column_name": "id", "data_type": "integer"}]}

CSV, one file per view with the view's own column names as the header. The file is
named `<view>.csv` or `<database>.<view>.csv`; without either, the header says which
view it is. Column names are matched without regard to case, so SQL Server's
upper-case `TABLE_SCHEMA` exports read the same as PostgreSQL's.

The database name comes from the row (`table_catalog` and its kin), else the JSON
`database`, else the file name. Rows in system schemas (`information_schema`,
`pg_catalog`, `sys`, ...) are skipped.

The extractor sees one file. A CSV file of columns does not know whether its table is a
table or a view, so it refers to the container without declaring it: the relation is
kept only when another file in the same unit declares the table or view.

Helpers from `repo_intel.extractors` are imported inside functions, because a pack
module is imported while that package is still loading (docs/solution-ir-contract.md,
section 2, rule 1).
"""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms import construct, platform_entity

K = EntityKind
E1 = EvidenceClass.E1
INPUT_DIR = ".rkp/inputs/catalog/"

# information_schema view (and common equivalents) -> the section this extractor reads it as.
SECTIONS: dict[str, str] = {
    "tables": "tables",
    "columns": "columns",
    "views": "views",
    "view_table_usage": "view_table_usage",
    "routines": "routines",
    "parameters": "parameters",
    "table_constraints": "table_constraints",
    "key_column_usage": "key_column_usage",
    "constraint_column_usage": "constraint_column_usage",
    "check_constraints": "check_constraints",
    "table_privileges": "table_grants",
    "role_table_grants": "table_grants",
    "grants": "table_grants",
    "column_privileges": "column_grants",
    "role_column_grants": "column_grants",
    "routine_privileges": "routine_grants",
    "role_routine_grants": "routine_grants",
    "applicable_roles": "role_members",
    "role_members": "role_members",
    "schemata": "schemata",
    "sequences": "sequences",
    "triggers": "triggers",
    "indexes": "indexes",
    "statistics": "indexes",  # MySQL lists index columns here
    "synonyms": "synonyms",
    "domains": "user_defined_types",
    "user_defined_types": "user_defined_types",
}
# Sections counted as constructs but not modelled: one construct occurrence per row.
UNMAPPED = {"schemata": "db:schema", "sequences": "db:sequence", "triggers": "db:trigger", "indexes": "db:index", "synonyms": "db:synonym", "user_defined_types": "db:user_defined_type", "check_constraints": "db:check_constraint"}
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "sys", "mysql", "performance_schema"}
DEFAULT_SCHEMA = "public"  # left out of a table's name, as data.sql_ddl leaves it out
# dialect named in the dump -> (Program platform as data.sql_lineage names it, sqlglot dialect)
DIALECTS = {
    "postgres": ("sql", "postgres"), "postgresql": ("sql", "postgres"), "mysql": ("sql", "mysql"), "mariadb": ("sql", "mysql"),
    "oracle": ("sql", "oracle"), "snowflake": ("sql", "snowflake"), "bigquery": ("sql", "bigquery"), "sqlite": ("sql", "sqlite"),
    "tsql": ("tsql", "tsql"), "sqlserver": ("tsql", "tsql"), "mssql": ("tsql", "tsql"), "synapse": ("tsql", "tsql"),
    "teradata": ("teradata", "teradata"),
}
_CATALOG_COLUMNS = ("table_catalog", "routine_catalog", "specific_catalog", "constraint_catalog", "object_catalog", "sequence_catalog", "trigger_catalog", "catalog_name", "udt_catalog", "domain_catalog")
_WS = re.compile(r"\s*")


@dataclass
class _Row:
    section: str
    values: dict[str, Any]
    start: int  # character offsets into the text
    end: int

    def get(self, *keys: str) -> str | None:
        """The first of `keys` that holds a non-empty name. Only text is a name."""
        for key in keys:
            value = self.values.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


@dataclass
class _Dump:
    rows: dict[str, list[_Row]] = field(default_factory=lambda: defaultdict(list))
    database: str | None = None
    dialect: str | None = None
    ignored: list[str] = field(default_factory=list)


def _section_of(name: str) -> str | None:
    return SECTIONS.get(name.strip().lower())


def _lower_keys(value: Any) -> dict[str, Any] | None:
    return {str(k).strip().lower(): v for k, v in value.items()} if isinstance(value, dict) else None


def read_json(text: str) -> _Dump:
    """The sections of a JSON dump, with each row's character span. Raises ValueError."""
    decoder = json.JSONDecoder()
    dump = _Dump()

    def ws(i: int) -> int:
        return _WS.match(text, i).end()

    def expect(i: int, char: str) -> int:
        if i >= len(text) or text[i] != char:
            raise ValueError(f"expected {char!r} at character {i}")
        return ws(i + 1)

    i = expect(ws(0), "{")
    if i < len(text) and text[i] == "}":
        return dump
    while True:
        key, i = decoder.raw_decode(text, i)
        if not isinstance(key, str):
            raise ValueError("object keys must be strings")
        i = expect(ws(i), ":")
        section = _section_of(key)
        if i < len(text) and text[i] == "[":
            i = ws(i + 1)
            rows = []
            if i < len(text) and text[i] == "]":
                i += 1
            else:
                while True:
                    start = i
                    value, i = decoder.raw_decode(text, i)
                    rows.append((value, start, i))
                    i = ws(i)
                    if i < len(text) and text[i] == ",":
                        i = ws(i + 1)
                        continue
                    i = expect(i, "]")
                    break
            if section is None:
                dump.ignored.append(key)
            else:
                for value, start, end in rows:
                    row = _lower_keys(value)
                    if row is not None:
                        dump.rows[section].append(_Row(section, row, start, end))
        else:
            value, i = decoder.raw_decode(text, i)
            if key.lower() == "database" and isinstance(value, str) and value.strip():
                dump.database = value.strip()
            elif key.lower() == "dialect" and isinstance(value, str):
                dump.dialect = value.strip().lower()
            elif key.lower() not in ("format", "exported_by", "version"):
                dump.ignored.append(key)
        i = ws(i)
        if i < len(text) and text[i] == ",":
            i = ws(i + 1)
            continue
        expect(i, "}")
        return dump


def _csv_section(stem: str, header: list[str]) -> tuple[str | None, str | None]:
    """(section, database named by the file) for a CSV file: by file name, else by header."""
    head, _, last = stem.rpartition(".")
    if _section_of(last):
        return _section_of(last), head or None
    if _section_of(stem):
        return _section_of(stem), None
    h = set(header)
    if "privilege_type" in h:
        return ("column_grants" if "column_name" in h else "routine_grants" if h & {"routine_name", "specific_name"} else "table_grants"), None
    for section, needs in (
        ("role_members", {"role_name", "grantee"}), ("views", {"view_definition"}), ("routines", {"routine_name"}),
        ("parameters", {"parameter_name"}), ("triggers", {"trigger_name"}), ("sequences", {"sequence_name"}),
        ("indexes", {"index_name"}), ("check_constraints", {"check_clause"}),
        ("table_constraints", {"constraint_type"}), ("key_column_usage", {"constraint_name", "column_name"}),
        ("columns", {"column_name"}), ("tables", {"table_type"}), ("schemata", {"schema_name"}),
    ):
        if needs <= h and not (section == "table_constraints" and "column_name" in h):
            return section, None
    return None, None


def read_csv(text: str, stem: str) -> _Dump:
    """The rows of one CSV file, each with its character span."""
    dump = _Dump()
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        return dump
    section, database = _csv_section(stem, header)
    dump.database = database
    if section is None:
        dump.ignored.append(stem)
        return dump
    before = reader.line_num
    for record in reader:
        after = reader.line_num
        if any(cell.strip() for cell in record):
            values = {header[i]: cell for i, cell in enumerate(record) if i < len(header)}
            dump.rows[section].append(_Row(section, values, starts[min(before, len(lines))], starts[min(after, len(lines))]))
        before = after
    return dump


def qualified(schema: str | None, name: str) -> str:
    """A table's natural-key name, as data.sql_ddl forms it: the default schema is left out."""
    return name if not schema or schema.lower() == DEFAULT_SCHEMA else f"{schema}.{name}"


def _program_name(schema: str | None, name: str) -> str:
    """A routine's Program name, as data.sql_lineage forms a procedure's: dotted lower case."""
    return qualified(schema, name).lower()


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().upper() in ("YES", "Y", "TRUE", "1"):
        return True
    if isinstance(value, str) and value.strip().upper() in ("NO", "N", "FALSE", "0"):
        return False
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class CatalogDump:
    id = "catalog.dump"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.startswith(INPUT_DIR) and path.lower().endswith((".json", ".csv")) and "/" not in path[len(INPUT_DIR):]

    def extract(self, path: str, data: bytes, blob: str):
        # Imported here: a pack module is imported while `repo_intel.extractors` loads.
        from repo_intel.extractors.base import Extraction, diagnostic

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        stem, ext = posixpath.splitext(posixpath.basename(path))
        try:
            dump = read_json(text) if ext.lower() == ".json" else read_csv(text, stem)
        except (ValueError, csv.Error) as error:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"not a catalogue dump: {error}")], parsed=False)
        return _Emitter(self.id, path, stem if ext.lower() == ".json" else (dump.database or ""), data, text, blob, dump).run()


class _Emitter:
    """Turns one file's rows into observations."""

    def __init__(self, extractor: str, path: str, file_database: str, data: bytes, text: str, blob: str, dump: _Dump):
        from repo_intel.extractors.sql_lineage import byte_offsets

        self.extractor, self.path, self.data, self.blob, self.dump = extractor, path, data, blob, dump
        # A byte-order mark is decoded away; offsets are into the bytes after it.
        self.bom = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        self.to_byte = byte_offsets(text, data[self.bom:])
        self.out: list = []
        self.diagnostics: list = []
        self.platform, self.sql_dialect = DIALECTS.get(dump.dialect or "", ("sql", None))
        if dump.dialect and dump.dialect not in DIALECTS:
            self._diagnose("info", f"dialect {dump.dialect!r} is not one this pack knows; routines are read as generic SQL")
        self.file_database = dump.database or file_database or None
        databases = {self._database(r) for rows in dump.rows.values() for r in rows} - {None}
        # One database per file keys tables as every other extractor does. A file holding
        # several would merge same-named tables, so each database becomes its own datastore.
        self.several = len(databases) > 1
        if self.several:
            self._diagnose("warning", f"this file describes {len(databases)} databases; their tables are keyed by database and will not match unqualified references in code")
        self.tables: dict[tuple[str | None, str], dict[str, Any]] = {}  # (database, lower qualified) -> {"kind", "name", "ref"}
        self.skipped = 0

    # -- helpers

    def _diagnose(self, severity: str, message: str) -> None:
        from repo_intel.extractors.base import diagnostic

        self.diagnostics.append(diagnostic(self.extractor, severity, message))

    def _at(self, row: _Row):
        from repo_intel.extractors.base import span

        return span(self.data, self.bom + self.to_byte(row.start), self.bom + self.to_byte(row.end))

    def _database(self, row: _Row) -> str | None:
        return row.get(*_CATALOG_COLUMNS) or self.dump.database or self.file_database

    def _realm(self, row: _Row) -> str:
        return self._database(row) or "catalog"

    def _system(self, schema: str | None) -> bool:
        return bool(schema) and (schema.lower() in SYSTEM_SCHEMAS or schema.lower().startswith("pg_temp"))

    def _datastore(self, database: str | None) -> str:
        return database if self.several and database else "default"

    def _table_ref(self, kind: str, database: str | None, name: str, declare: bool = True) -> dict[str, Any]:
        from repo_intel.extractors.base import ref

        if kind == "view":
            return ref(K.DATA_SCHEMA, {"kind": "view", "name": name}, name, declare=declare)
        return ref(K.TABLE, {"datastore": self._datastore(database), "name": name}, name, declare=declare)

    def _known(self, database: str | None, name: str) -> dict[str, Any] | None:
        return self.tables.get((database if self.several else None, name.lower()))

    def _relation(self, row: _Row, subject: dict[str, Any], predicate: Predicate, obj: dict[str, Any], qualifiers: dict[str, Any] | None = None) -> None:
        from repo_intel.extractors.base import relation

        self.out.append(relation(self.blob, self._at(row), subject, predicate, obj, qualifiers, E1))

    def _entity(self, row: _Row, term: str, natural_key: dict[str, Any], name: str, attributes: dict[str, Any]) -> None:
        self.out.append(platform_entity(self.blob, self._at(row), term, natural_key, name, {k: v for k, v in attributes.items() if v is not None}, E1))

    def _construct(self, row: _Row, construct_id: str, modelled: bool) -> None:
        self.out.append(construct(self.blob, self._at(row), construct_id, modelled))

    def _rows(self, section: str) -> list[_Row]:
        kept = []
        for row in self.dump.rows.get(section, []):
            schema = row.get("table_schema", "routine_schema", "specific_schema", "constraint_schema", "object_schema", "schema_name", "sequence_schema", "trigger_schema", "event_object_schema")
            if self._system(schema):
                self.skipped += 1
                continue
            kept.append(row)
        return kept

    # -- sections

    def run(self):
        from repo_intel.extractors.base import Extraction

        self._tables()
        self._views()
        self._columns()
        self._constraints()
        self._routines()
        self._grants()
        self._role_members()
        for section, construct_id in UNMAPPED.items():
            for row in self._rows(section):
                self._construct(row, construct_id, False)
        if self.skipped:
            self._diagnose("info", f"{self.skipped} rows in system schemas were skipped")
        if self.dump.ignored:
            self._diagnose("info", "not read: " + ", ".join(sorted(set(self.dump.ignored))))
        if not self.file_database and any(self.dump.rows.values()) and not any(self._database(r) for rows in self.dump.rows.values() for r in rows):
            self._diagnose("warning", "no database is named: roles and permissions use the realm 'catalog'")
        parsed = bool(self.out) or not self.dump.ignored
        return Extraction(self.out, self.diagnostics, parsed=parsed)

    def _declare_table(self, row: _Row, kind: str, schema: str | None, table: str, attributes: dict[str, Any]) -> None:
        database = self._database(row)
        name = qualified(schema, table)
        key = (database if self.several else None, name.lower())
        if key in self.tables and self.tables[key]["kind"] != kind:
            self._diagnose("warning", f"{name} is listed both as a table and as a view; kept as a {self.tables[key]['kind']}")
            kind = self.tables[key]["kind"]
        attrs = {"database": database, "schema": schema, **attributes}
        if kind == "view":
            self._entity(row, "db:View", {"kind": "view", "name": name}, name, attrs)
        else:
            self._entity(row, "db:Table", {"datastore": self._datastore(database), "name": name}, name, attrs)
        self.tables.setdefault(key, {"kind": kind, "name": name, "database": database, "ref": self._table_ref(kind, database, name)})

    def _tables(self) -> None:
        for row in self._rows("tables"):
            table = row.get("table_name")
            if not table:
                continue
            schema = row.get("table_schema", "table_owner")
            table_type = (row.get("table_type") or "BASE TABLE").upper()
            if "TEMP" in table_type:
                continue  # a session's own table, not part of the schema
            if "VIEW" in table_type:
                self._declare_table(row, "view", schema, table, {"materialized": True if "MATERIALIZED" in table_type else None})
                self._construct(row, "db:view", True)
            else:
                external = any(word in table_type for word in ("FOREIGN", "EXTERNAL"))
                self._declare_table(row, "table", schema, table, {"table_type": table_type, "external": True if external else None})
                self._construct(row, "db:table", True)

    def _view_reads(self, definition: str) -> list[str]:
        from repo_intel.extractors.sql_lineage import lineage

        try:
            found = lineage(definition, self.sql_dialect)
        except Exception:  # sqlglot errors on hostile text: the view stays, its reads are unknown
            return []
        return sorted({self._table_name(name) for name, _, _ in found.reads})

    def _table_name(self, name: str) -> str:
        """A table named in SQL text, spelled as this file declares it when it does."""
        bare = name[len(DEFAULT_SCHEMA) + 1:] if name.lower().startswith(DEFAULT_SCHEMA + ".") else name
        known = self.tables.get((None, bare.lower())) if not self.several else None
        return known["name"] if known else bare

    def _views(self) -> None:
        usage: dict[tuple[str | None, str], set[str]] = defaultdict(set)
        for row in self._rows("view_table_usage"):
            view, table = row.get("view_name"), row.get("table_name")
            if view and table:
                usage[(self._database(row), qualified(row.get("view_schema"), view).lower())].add(qualified(row.get("table_schema"), table))
        seen = set()
        for row in self._rows("views"):
            view = row.get("table_name", "view_name")
            if not view:
                continue
            schema = row.get("table_schema", "view_schema")
            definition = row.values.get("view_definition")
            reads = set(self._view_reads(definition)) if isinstance(definition, str) and definition.strip() else set()
            key = (self._database(row), qualified(schema, view).lower())
            reads |= usage.get(key, set())
            seen.add(key)
            listed = self._known(key[0], key[1]) is not None  # already counted from the tables section
            self._declare_table(row, "view", schema, view, {"reads": sorted(reads) if reads else None})
            if not listed:
                self._construct(row, "db:view", True)
            if reads:
                self._construct(row, "db:view_dependency", True)
        for row in self._rows("view_table_usage"):
            view = row.get("view_name")
            if not view:
                continue
            key = (self._database(row), qualified(row.get("view_schema"), view).lower())
            known = self._known(key[0], key[1])
            if key in seen:
                continue  # already folded into the view's reads above
            if known and known["kind"] == "view":
                self._declare_table(row, "view", row.get("view_schema"), view, {"reads": sorted(usage[key])})
                self._construct(row, "db:view_dependency", True)
            else:
                self._construct(row, "db:view_dependency", False)

    def _columns(self) -> None:
        from repo_intel.extractors.base import ref

        for row in self._rows("columns"):
            table, column = row.get("table_name"), row.get("column_name")
            if not table or not column:
                continue
            database = self._database(row)
            name = qualified(row.get("table_schema"), table)
            known = self._known(database, name)
            if known:
                name = known["name"]
            attributes = {"type": row.get("data_type", "udt_name", "column_type"), "nullable": _truthy(row.values.get("is_nullable")), "position": _integer(row.values.get("ordinal_position"))}
            key = {"table": name, "name": column}
            self._entity(row, "db:Column", key, column, attributes)
            field_ref = ref(K.FIELD, key, column)
            if known:
                self._relation(row, known["ref"], Predicate.CONTAINS, field_ref)
            else:  # this file does not say whether it is a table or a view: whichever the unit declares
                for kind in ("table", "view"):
                    self._relation(row, self._table_ref(kind, database, name, declare=False), Predicate.CONTAINS, field_ref)
            self._construct(row, "db:column", True)

    def _constraints(self) -> None:
        kinds: dict[tuple[str | None, str], str] = {}
        first: dict[tuple[str | None, str], _Row] = {}
        for row in self._rows("table_constraints"):
            name = row.get("constraint_name")
            if name:
                key = (row.get("constraint_schema", "table_schema"), name)
                kinds[key] = (row.get("constraint_type") or "").upper()
                first.setdefault(key, row)
        targets: dict[tuple[str | None, str], list[tuple[str, str]]] = defaultdict(list)
        for row in self._rows("constraint_column_usage"):
            name, table, column = row.get("constraint_name"), row.get("table_name"), row.get("column_name")
            if name and table and column:
                targets[(row.get("constraint_schema", "table_schema"), name)].append((qualified(row.get("table_schema"), table), column))
        columns: dict[tuple[str | None, str], list[_Row]] = defaultdict(list)
        for row in self._rows("key_column_usage"):
            name = row.get("constraint_name")
            if name and row.get("table_name") and row.get("column_name"):
                key = (row.get("constraint_schema", "table_schema"), name)
                columns[key].append(row)
                first.setdefault(key, row)
        construct_of = {"PRIMARY KEY": "db:primary_key", "UNIQUE": "db:unique_constraint", "FOREIGN KEY": "db:foreign_key", "CHECK": "db:check_constraint"}
        for key in sorted(first, key=lambda k: (k[0] or "", k[1])):
            rows = columns.get(key, [])
            kind = kinds.get(key) or next((r.get("constraint_type").upper() for r in rows if r.get("constraint_type")), None)
            if kind is None and rows:
                kind = "FOREIGN KEY" if any(r.get("referenced_table_name") for r in rows) else "PRIMARY KEY" if key[1].upper() == "PRIMARY" else None
            construct_id = construct_of.get(kind or "")
            if construct_id is None:
                continue  # NOT NULL and other kinds the dump may list
            modelled = False
            if construct_id != "db:check_constraint":
                refs = targets.get(key, [])
                for i, row in enumerate(sorted(rows, key=lambda r: _integer(r.values.get("ordinal_position")) or 0)):
                    attribute: dict[str, Any]
                    if kind == "PRIMARY KEY":
                        attribute = {"primary_key": True}
                    elif kind == "UNIQUE":
                        attribute = {"unique": True}
                    else:
                        target = None
                        if row.get("referenced_table_name") and row.get("referenced_column_name"):
                            target = (qualified(row.get("referenced_table_schema"), row.get("referenced_table_name")), row.get("referenced_column_name"))
                        elif len(refs) == len(rows):
                            target = refs[i]
                        if target is None:
                            continue
                        attribute = {"references": f"{target[0]}.{target[1]}"}
                    table = qualified(row.get("table_schema"), row.get("table_name"))
                    known = self._known(self._database(row), table)
                    table = known["name"] if known else table
                    self._entity(row, "db:Column", {"table": table, "name": row.get("column_name")}, row.get("column_name"), attribute)
                    modelled = True
                if kind == "FOREIGN KEY" and rows and not modelled:
                    self._diagnose("info", f"foreign key {key[1]}: the referenced columns are not in this file")
            self._construct(first[key], construct_id, modelled)

    def _routines(self) -> None:
        params: dict[tuple[str | None, str], list[str]] = defaultdict(list)
        param_rows: dict[tuple[str | None, str], list[_Row]] = defaultdict(list)
        for row in self._rows("parameters"):
            specific, name = row.get("specific_name"), row.get("parameter_name")
            if specific:
                key = (row.get("specific_schema"), specific)
                param_rows[key].append(row)
                if name:
                    params[key].append(name)
        used: set[tuple[str | None, str]] = set()
        for row in self._rows("routines"):
            routine = row.get("routine_name")
            if not routine:
                continue
            schema = row.get("routine_schema")
            name = _program_name(schema, routine)
            specific = (row.get("specific_schema") or schema, row.get("specific_name") or routine)
            names = params.get(specific)
            if specific in param_rows:
                used.add(specific)
                for p in param_rows[specific]:
                    self._construct(p, "db:routine_parameter", True)
            kind = (row.get("routine_type") or "routine").lower()
            self._entity(row, "db:Routine", {"platform": self.platform, "name": name}, name, {"kind": kind, "language": "sql", "database": self._database(row), "schema": schema, "parameters": names})
            self._construct(row, "db:routine", True)
            definition = row.values.get("routine_definition")
            if isinstance(definition, str) and definition.strip():
                self._routine_lineage(row, name, definition)
        for key, rows in param_rows.items():
            if key not in used:
                for p in rows:
                    self._construct(p, "db:routine_parameter", False)

    def _routine_lineage(self, row: _Row, name: str, definition: str) -> None:
        from repo_intel.extractors.base import ref
        from repo_intel.extractors.sql_lineage import lineage

        try:
            found = lineage(definition, self.sql_dialect, views=False)
        except Exception:
            self._diagnose("warning", f"routine {name}: its definition could not be read")
            self._construct(row, "db:routine_lineage", False)
            return
        me = ref(K.PROGRAM, {"platform": self.platform, "name": name}, name)
        linked = 0
        for predicate, items in ((Predicate.WRITES_TO, found.writes), (Predicate.READS_FROM, found.reads)):
            for table, _, _ in items:
                if "{" in table:
                    continue
                spelled = self._table_name(table)
                known = self._known(self._database(row), spelled)
                # Only tables and views this file declares: a routine's scratch tables are not invented.
                target = known["ref"] if known else self._table_ref("table", self._database(row), spelled, declare=False)
                self._relation(row, me, predicate, target, {"via": "routine_definition"})
                linked += 1
        if found.unresolved:
            self._diagnose("info", f"routine {name}: {found.unresolved} statements name no table that can be read (dynamic SQL)")
        self._construct(row, "db:routine_lineage", bool(linked))

    def _grant(self, row: _Row, object_kind: str, object_name: str, construct_id: str) -> None:
        from repo_intel.extractors.base import ref

        grantee, privilege = row.get("grantee"), row.get("privilege_type")
        if not grantee or not privilege:
            self._construct(row, construct_id, False)
            return
        realm = self._realm(row)
        privilege = privilege.upper()
        permission = f"{privilege} on {object_name}"
        role_key = {"realm": realm, "name": grantee}
        self._entity(row, "db:Role", role_key, grantee, {"pseudo": True if grantee.upper() == "PUBLIC" else None})
        self._entity(row, "db:Grant", {"realm": realm, "name": permission}, permission, {"privilege": privilege, "object_kind": object_kind, "object": object_name, "grantable": _truthy(row.values.get("is_grantable"))})
        self._relation(row, ref(K.ROLE, role_key, grantee), Predicate.GRANTS, ref(K.PERMISSION, {"realm": realm, "name": permission}, permission))
        self._construct(row, construct_id, True)

    def _grants(self) -> None:
        for row in self._rows("table_grants"):
            table = row.get("table_name", "object_name")
            if not table:
                self._construct(row, "db:table_grant", False)
                continue
            name = qualified(row.get("table_schema", "object_schema"), table)
            known = self._known(self._database(row), name)
            self._grant(row, known["kind"] if known else "table", known["name"] if known else name, "db:table_grant")
        for row in self._rows("column_grants"):
            table, column = row.get("table_name"), row.get("column_name")
            if not table or not column:
                self._construct(row, "db:column_grant", False)
                continue
            name = qualified(row.get("table_schema"), table)
            known = self._known(self._database(row), name)
            self._grant(row, "column", f"{known['name'] if known else name}.{column}", "db:column_grant")
        for row in self._rows("routine_grants"):
            routine = row.get("routine_name", "specific_name")
            if not routine:
                self._construct(row, "db:routine_grant", False)
                continue
            self._grant(row, "routine", _program_name(row.get("routine_schema", "specific_schema"), routine), "db:routine_grant")

    def _role_members(self) -> None:
        from repo_intel.extractors.base import ref

        for row in self._rows("role_members"):
            member, role = row.get("grantee", "member"), row.get("role_name", "role")
            if not member or not role:
                self._construct(row, "db:role_membership", False)
                continue
            realm = self._realm(row)
            for name in (member, role):
                self._entity(row, "db:Role", {"realm": realm, "name": name}, name, {"pseudo": True if name.upper() == "PUBLIC" else None})
            # Holding the member confers the role, as holding a role confers its permissions.
            self._relation(row, ref(K.ROLE, {"realm": realm, "name": member}, member), Predicate.GRANTS, ref(K.ROLE, {"realm": realm, "name": role}, role), {"via": "membership"})
            self._construct(row, "db:role_membership", True)
