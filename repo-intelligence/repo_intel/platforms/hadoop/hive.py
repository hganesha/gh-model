"""`hadoop.hive`: Hive and Impala scripts as Programs with lineage and transform shapes.

Which files: every `.hql`; a `.q` or `.sql` only when its path is under a `hive/` directory
or its text is clearly HiveQL (INSERT OVERWRITE, STORED AS, PARTITIONED BY, ROW FORMAT,
LATERAL VIEW, `${hivevar:...}`, ADD JAR, MSCK REPAIR, Impala's COMPUTE STATS or INVALIDATE
METADATA). A `.sql` migration or dbt model is left to the extractors that read those.

What it declares (E2):

- the script as a Program (`hdp:HiveScript`, platform `hive`, named by its path), with
  `engine` `hive` or `impala`, and the names (never the values) of the settings it makes
  (`SET name=value`), the substitutions it uses and the databases it switches to;
- per statement, the tables it reads and writes through the shared SQL lineage reader
  (`sql_lineage.lineage`, dialect `hive`) and the shared recognizer
  (`transforms.sql_query`): INSERT OVERWRITE and INSERT INTO, CTAS, MERGE, UPDATE and
  DELETE. Hive forms the shared readers do not know are read here: a multi-table insert
  (`FROM src INSERT ... INSERT ...`) is split into one insert per target; LOAD DATA INPATH
  reads the path's Dataset and writes the table; INSERT OVERWRITE DIRECTORY writes the
  path's Dataset;
- `USE db` qualifies the one-part names that follow it, as Hive does;
- CREATE TABLE as a Table with its Fields (`{table, name}`, partition columns marked);
  an external table's LOCATION is a Dataset the Table is DERIVED_FROM; a view is a
  DataSchema `{kind: view, name}` with the tables it reads;
- one `transform` observation (language `hadoop`) with every statement's shapes.

A table named through a substitution (`${hivevar:db}.orders`) keeps it as a placeholder
(`{db}.orders`), is marked parameterized, and its transforms dynamic; a diagnostic names
it. Nothing a substitution stands for is resolved, not even from a SET in the same file.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms.hadoop.common import P, Out, Substituted, dataset_path, mark_dynamic, program

EXTRACTOR = "hadoop.hive"

_HIVEQL = re.compile(
    r"\binsert\s+overwrite\s+(?:table|local\s+directory|directory)\b|\bstored\s+as\s+(?:orc|parquet|textfile|sequencefile|avro|rcfile|jsonfile|kudu)\b"
    r"|\bpartitioned\s+by\s*\(|\brow\s+format\s+(?:delimited|serde)\b|\$\{(?:hivevar|hiveconf):|\blateral\s+view\b|\bmsck\s+repair\b"
    r"|^\s*add\s+(?:jar|file|archive)s?\b|\btblproperties\s*\(|\binvalidate\s+metadata\b|\bcompute\s+(?:incremental\s+)?stats\b",
    re.I | re.M,
)
_IMPALA = re.compile(r"\binvalidate\s+metadata\b|\bcompute\s+(?:incremental\s+)?stats\b|^\s*refresh\s+[\w`]|\bstored\s+as\s+kudu\b|\$\{var:", re.I | re.M)
_LEAD = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)
_STRING = r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
_LOAD = re.compile(rf"^load\s+data\s+(local\s+)?inpath\s+{_STRING}\s+(overwrite\s+)?into\s+table\s+([\w`.{{}}]+)", re.I | re.S)
_DIRECTORY = re.compile(rf"^insert\s+overwrite\s+(local\s+)?directory\s+{_STRING}", re.I | re.S)
_QUERY_START = re.compile(r"\b(select|with|from)\b", re.I)
_SET = re.compile(r"^set\s+([\w.:-]+)\s*=", re.I)
_USE = re.compile(r"^use\s+([\w`{}]+)\s*$", re.I | re.S)
_CREATE_TABLE = re.compile(r"^create\s+(temporary\s+)?(external\s+)?(?:transactional\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w`.{}]+)", re.I)
_CREATE_VIEW = re.compile(r"^create\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:if\s+not\s+exists\s+)?([\w`.{}]+)", re.I)
_LOCATION = re.compile(rf"\blocation\s+{_STRING}", re.I)
_PARTITION = re.compile(r"\bpartition\s*\(([^()]*)\)", re.I)
_LATERAL = re.compile(r"\blateral\s+view\b", re.I)
_TRANSFORM = re.compile(r"\b(?:transform|map|reduce)\s*\([^()]*\)\s*using\b|\busing\s+'[^']*'\s+as\b", re.I)


def under_hive_dir(path: str) -> bool:
    return "hive" in [p.lower() for p in path.split("/")[:-1]]


def is_hiveql(path: str, text: str) -> bool:
    """Whether a `.q` or `.sql` file is read as HiveQL (its directory or its text says so)."""
    return under_hive_dir(path) or _HIVEQL.search(text) is not None


class HiveScripts:
    id = EXTRACTOR
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".hql", ".q", ".sql"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        ext = posixpath.splitext(path)[1].lower()
        if ext in (".q", ".sql"):
            if not is_hiveql(path, text):
                return base.Extraction(parsed=False)
            if ext == ".sql":
                from repo_intel.extractors.contracts_data import migration_identity

                if migration_identity(path) is not None or "{{" in text or "{%" in text:
                    return base.Extraction(parsed=False)  # a migration or a dbt model
        return _Script(path, data, blob, text).run()


class _Script:
    def __init__(self, path: str, data: bytes, blob: str, text: str):
        from repo_intel.extractors.sql_lineage import byte_offsets

        self.path, self.data, self.text = path, data, text
        self.out = Out(EXTRACTOR, path, data, blob)
        self.to_byte = byte_offsets(text, data)
        self.me = program("hive", path)
        self.tokens: dict[str, str] = {}
        self.database: str | None = None
        self.settings: set[str] = set()
        self.variables: set[str] = set()
        self.databases: set[str] = set()
        self.dynamic_partitions: set[str] = set()
        self.declared: set[tuple[str, str]] = set()
        self.moved: set[tuple[str, str, str]] = set()
        self.parameterized: set[str] = set()
        self.ddl = posixpath.splitext(path)[1].lower() != ".sql"  # data.sql_ddl reads tables of a .sql file
        from repo_intel.compile.transforms import Builder

        self.builder = Builder()

    # ------------------------------------------------------------------ helpers

    def at(self, a: int, b: int):
        return self.out.span(self.to_byte(a), self.to_byte(b))

    def qualify(self, name: str) -> str:
        """A one-part name after `USE db` is in that database."""
        if "." not in name and self.database and not name.startswith("{"):
            return f"{self.database}.{name}"
        return name

    def name_of(self, sub: Substituted, raw: str) -> str:
        from repo_intel.extractors.sql_lineage import clean_name

        name = self.qualify(sub.restore(clean_name(raw)))
        if "{" in name and name not in self.parameterized:
            self.parameterized.add(name)
            self.out.note("info", f"table name made at run time: {name}")
        return name

    def table(self, at, name: str) -> dict[str, Any]:
        param = "{" in name
        key = ("table", name)
        if key not in self.declared:
            self.declared.add(key)
            return self.out.table(at, name, param)
        from repo_intel.extractors.sql_lineage import table

        return table(name)

    def dataset(self, at, name: str, param: bool, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        key = ("dataset", name)
        if key not in self.declared:
            self.declared.add(key)
            return self.out.dataset(at, name, param, attrs)
        return self.out.base.ref(K.DATASET, {"name": name}, name)

    def move(self, at, predicate, target: dict[str, Any], param: bool) -> None:
        key = (str(predicate), target["kind"], target["name"] if target["kind"] == "Dataset" else target["natural_key"]["name"])
        if key in self.moved:
            return
        self.moved.add(key)
        self.out.moves(at, self.me, predicate, target, param)

    def lineage(self, sub: Substituted, query: str, offset: int, a: int, whole=None) -> int:
        """READS_FROM and WRITES_TO of one statement's SQL (`query` starts at `offset` of the
        substituted statement, which starts at `a` of the file). Returns the writes found."""
        from repo_intel.extractors.sql_lineage import lineage

        found = lineage(query, "hive", views=False)
        self.out.unresolved += found.unresolved
        for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
            for raw, s, e in items:
                name = self.name_of(sub, raw)
                at = whole or self.at(a + sub.to_original(offset + s), a + sub.to_original(offset + e))
                self.move(at, predicate, self.table(at, name), "{" in name)
        return len(found.writes)

    def shapes(self, sql: str, at: list[int], into: str | None = None) -> None:
        """The shared recognizer's transforms of one statement; `into` names a path the result is written to."""
        from repo_intel.compile.transforms import sql_query

        try:
            ref = sql_query(self.builder, sql, at, dialect="hive", qualify=lambda n: self.qualify(self._restore(n)))
            if into is not None and ref is not None:
                self.builder.write(ref, into, at)
        except Exception as exc:  # a recognizer fault loses this statement's shapes, never the script
            # The type only: an exception's text can quote the SQL it failed on.
            self.out.note("warning", f"transform shapes not derived for a statement: {type(exc).__name__}")

    def _restore(self, name: str) -> str:
        for token, ph in self.tokens.items():
            name = re.sub(re.escape(token), ph.lower(), name, flags=re.I)
        return name

    # ------------------------------------------------------------------ the script

    def run(self):
        from repo_intel.extractors.contracts_data import split_statements

        out = self.out
        whole = out.whole()
        out.construct(whole, "hdp:hive_script", True)
        for a, b in split_statements(self.text):
            lead = _LEAD.match(self.text, a)
            start = lead.end() if lead else a
            if start >= b:
                continue
            self.statement(start, b)
        attrs = {
            "kind": "script", "language": "hiveql", "engine": "impala" if self.impala() else "hive",
            "settings": sorted(self.settings), "variables": sorted(self.variables), "databases": sorted(self.databases),
            "dynamic_partitions": sorted(self.dynamic_partitions),
        }
        out.term(whole, "hdp:HiveScript", self.me["natural_key"], self.me["name"], attrs)
        out.items.insert(0, out.items.pop())  # the Program first, for readable output
        transforms = self.builder.finish()
        if transforms:
            restored = mark_dynamic(Substituted("", self.tokens).restore_all(transforms))
            out.transforms(whole, self.me, restored)
        return out.finish()

    def impala(self) -> bool:
        return "impala" in [p.lower() for p in self.path.split("/")[:-1]] or _IMPALA.search(self.text) is not None

    def statement(self, a: int, b: int) -> None:
        out = self.out
        sub = Substituted(self.text[a:b], self.tokens, self.text)
        sql = sub.text
        at = self.at(a, b)
        span = [at.start_byte, at.end_byte]
        for name in sub.names:
            self.variables.add(name.split("(", 1)[0].strip())
            out.construct(at, "hdp:hive_variable", True)
        words = sql.lstrip().split(None, 3)
        head = [w.lower() for w in words[:3]] + ["", "", ""]
        kw = head[0]
        if _LATERAL.search(sql):
            out.construct(at, "hdp:hive_lateral_view", False)
        if _TRANSFORM.search(sql):
            out.construct(at, "hdp:hive_transform_script", False)
        if kw == "set":
            m = _SET.match(sql.strip())
            if m:
                self.settings.add(m.group(1).lower())
            out.construct(at, "hdp:hive_set", m is not None)
        elif kw == "use":
            m = _USE.match(sql.strip())
            if m:
                self.database = self._restore(m.group(1).strip("`").lower())
                self.databases.add(self.database)
            out.construct(at, "hdp:hive_use", m is not None)
        elif kw == "add" and head[1] in ("jar", "jars", "file", "files", "archive", "archives"):
            out.construct(at, "hdp:hive_add_resource", False)
        elif kw == "create" and ("function" in head[1:3]):
            out.construct(at, "hdp:hive_temporary_function", False)
        elif kw == "source":
            out.construct(at, "hdp:hive_source", False)
            out.note("info", "a script run with `source` is not followed")
        elif kw.startswith("!") or kw == "dfs":
            out.construct(at, "hdp:hive_shell_command", False)
        elif kw == "msck":
            out.construct(at, "hdp:hive_msck_repair", False)
        elif kw == "analyze":
            out.construct(at, "hdp:hive_analyze", False)
        elif kw == "compute":
            out.construct(at, "hdp:impala_compute_stats", False)
        elif kw == "invalidate":
            out.construct(at, "hdp:impala_invalidate_metadata", False)
        elif kw == "refresh":
            out.construct(at, "hdp:impala_refresh", False)
        elif kw == "drop":
            out.construct(at, "hdp:hive_drop", False)
        elif kw == "alter":
            out.construct(at, "hdp:hive_alter_table", False)
        elif kw == "create" and head[1] in ("database", "schema"):
            out.construct(at, "hdp:hive_create_database", False)
        elif kw in ("show", "describe", "desc", "explain", "reset", "list") or (kw == "delete" and head[1] in ("jar", "jars", "file", "files", "archive", "archives")):
            pass  # session commands that neither move data nor change the catalogue
        elif kw == "load":
            self.load(sub, a, at, span)
        elif kw == "insert" and _DIRECTORY.match(sql.strip()):
            self.directory(sub, a, at, span)
        elif kw == "create" and _CREATE_VIEW.match(sql.strip()):
            self.view(sub, a, at)
        elif kw == "create" and _CREATE_TABLE.match(sql.strip()):
            self.create_table(sub, a, at, span)
        elif kw in ("insert", "from", "with", "select", "merge", "update", "delete", "truncate", "upsert"):
            self.dml(sub, a, at, span, kw)
        else:
            out.construct(at, "hdp:hive_unparsed", False)

    # ------------------------------------------------------------------ statements

    def dml(self, sub: Substituted, a: int, at, span: list[int], kw: str) -> None:
        from repo_intel.compile.transforms import parse_sql

        out = self.out
        sql = sub.text
        tree = parse_sql(sql, "hive")
        if tree is not None and type(tree).__name__ == "MultitableInserts":
            self.multi_insert(sub, tree, a, at, span)
            return
        for m in _PARTITION.finditer(sql):
            if kw == "insert" and any("=" not in part for part in m.group(1).split(",") if part.strip()):
                target = re.search(r"\b(?:into|overwrite)\s+(?:table\s+)?([\w`.{}]+)", sql, re.I)
                if target:
                    self.dynamic_partitions.add(self.name_of(sub, target.group(1)))
                out.construct(at, "hdp:hive_dynamic_partition", True)
        writes = self.lineage(sub, sql, 0, a)
        self.shapes(sql, span)
        low = sql.lower()
        if kw == "merge":
            cid = "hdp:hive_merge"
        elif kw in ("update", "delete", "truncate"):
            cid = "hdp:hive_update_delete"
        elif re.search(r"\binsert\s+overwrite\b", low):
            cid = "hdp:hive_insert_overwrite"
        elif re.search(r"\binsert\s+into\b", low):
            cid = "hdp:hive_insert_into"
        elif writes:
            cid = "hdp:hive_insert_into"
        else:
            cid = "hdp:hive_select"
        out.construct(at, cid, tree is not None or writes > 0)

    def multi_insert(self, sub: Substituted, tree: Any, a: int, at, span: list[int]) -> None:
        """`FROM src INSERT ... SELECT ... INSERT ... SELECT ...`: one insert per target, each
        reading the shared source, read as the insert it stands for."""
        source = tree.args.get("source")
        inserts = list(tree.expressions or [])
        self.out.construct(at, "hdp:hive_multi_insert", bool(inserts) and source is not None)
        for ins in inserts:
            one = ins.copy()
            select = one.expression
            if source is None or select is None:
                continue
            select.set("from", None)
            select.set("from_", None)
            one.set("expression", select.from_(source.copy(), copy=False))
            text = one.sql(dialect="hive")
            self.lineage(sub, text, 0, a, whole=at)
            self.shapes(text, span)

    def load(self, sub: Substituted, a: int, at, span: list[int]) -> None:
        out = self.out
        m = _LOAD.match(sub.text.strip())
        if not m:
            out.construct(at, "hdp:hive_load_data", False)
            return
        local, raw_path, _, raw_table = m.groups()
        found = dataset_path(raw_path[1:-1])
        if found is None:
            out.construct(at, "hdp:hive_load_data", False)
            return
        path, param = found
        path = f"file:{path}" if local else path
        name = self.name_of(sub, raw_table)
        out.construct(at, "hdp:hive_load_data", True)
        ds = self.dataset(at, path, param, {"local": True} if local else None)
        self.move(at, P.READS_FROM, ds, param)
        self.move(at, P.WRITES_TO, self.table(at, name), "{" in name)
        self.builder.write(path, name, span)

    def directory(self, sub: Substituted, a: int, at, span: list[int]) -> None:
        out = self.out
        sql = sub.text
        lead = len(sql) - len(sql.lstrip())
        m = _DIRECTORY.match(sql.strip())
        assert m is not None
        local, raw_path = m.groups()
        found = dataset_path(raw_path[1:-1])
        q = _QUERY_START.search(sql, lead + m.end())
        if found is None or q is None:
            out.construct(at, "hdp:hive_insert_directory", False)
            return
        path, param = found
        path = f"file:{path}" if local else path
        out.construct(at, "hdp:hive_insert_directory", True)
        query = sql[q.start():]
        self.lineage(sub, query, q.start(), a)
        ds = self.dataset(at, path, param, {"local": True} if local else None)
        self.move(at, P.WRITES_TO, ds, param)
        self.shapes(query, span, into=path)

    def view(self, sub: Substituted, a: int, at) -> None:
        from repo_intel.extractors.sql_lineage import lineage

        m = _CREATE_VIEW.match(sub.text.strip())
        assert m is not None
        name = self.name_of(sub, m.group(1))
        q = re.search(r"\bas\b\s*\(?\s*(select|with)\b", sub.text, re.I)
        reads = sorted({self.name_of(sub, n) for n, _, _ in lineage(sub.text[q.start(1):], "hive").reads}) if q else []
        self.out.entity(at, K.DATA_SCHEMA, {"kind": "view", "name": name}, name, {"reads": reads})
        self.out.construct(at, "hdp:hive_create_view", True)

    def create_table(self, sub: Substituted, a: int, at, span: list[int]) -> None:
        from repo_intel.compile.transforms import parse_sql

        out = self.out
        sql = sub.text.strip()
        m = _CREATE_TABLE.match(sql)
        assert m is not None
        temporary, external = bool(m.group(1)), bool(m.group(2))
        name = self.name_of(sub, m.group(3))
        tree = parse_sql(sub.text, "hive")
        if re.search(r"\bas\s+(?:select|with)\b", sql, re.I) or (tree is not None and tree.args.get("expression") is not None):
            writes = self.lineage(sub, sub.text, 0, a)
            self.shapes(sub.text, span)
            out.construct(at, "hdp:hive_ctas", writes > 0)
            return
        if temporary:
            out.construct(at, "hdp:hive_create_table", False)
            return
        columns, partitions = _columns(tree)
        attrs: dict[str, Any] = {}
        if external:
            attrs["external"] = True
        if partitions:
            attrs["partitioned_by"] = [c for c, _ in partitions]
            out.construct(at, "hdp:hive_partitioned_by", True)
        stored = re.search(r"\bstored\s+as\s+(\w+)", sql, re.I)
        if stored:
            attrs["stored_as"] = stored.group(1).lower()
        key = ("table", name)
        if key in self.declared:
            from repo_intel.extractors.sql_lineage import table as table_ref

            table = table_ref(name)
            out.entity(at, K.TABLE, table["natural_key"], name, attrs)
        else:
            self.declared.add(key)
            table = out.table(at, name, "{" in name, attrs)
        out.construct(at, "hdp:hive_create_table", True)
        if self.ddl and "{" not in name:
            cursor = m.end(3) + (len(sub.text) - len(sub.text.lstrip()))
            for column, dtype in columns + partitions:
                field_key = {"table": name, "name": column}
                fattrs: dict[str, Any] = {"type": dtype} if dtype else {}
                if (column, dtype) in partitions:
                    fattrs["partition"] = True
                found_at = re.compile(rf"(?<![\w`]){re.escape(column)}(?![\w`])", re.I).search(sub.text, cursor)
                where = at
                if found_at:
                    cursor = found_at.end()
                    where = self.at(a + sub.to_original(found_at.start()), a + sub.to_original(found_at.end()))
                out.entity(where, K.FIELD, field_key, column, fattrs)
                out.relation(where, table, P.CONTAINS, out.base.ref(K.FIELD, field_key, column))
        loc = _LOCATION.search(sub.text)
        if loc:
            found = dataset_path(sub.restore(loc.group(1)[1:-1]))
            if found is not None:
                path, param = found
                where = self.at(a + sub.to_original(loc.start(1)), a + sub.to_original(loc.end(1)))
                ds = self.dataset(where, path, param)
                out.relation(where, table, P.DERIVED_FROM, ds, {"parameterized": True} if param else None)
                out.construct(where, "hdp:hive_external_table", True)
            else:
                out.construct(at, "hdp:hive_external_table", False)


def _columns(tree: Any) -> tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]]:
    """The columns and the partition columns of a CREATE TABLE, with their types as written in Hive."""
    if tree is None:
        return [], []
    from sqlglot import exp

    def defs(schema: Any) -> list[tuple[str, str | None]]:
        out = []
        for c in (schema.expressions if isinstance(schema, exp.Schema) else []):
            if isinstance(c, exp.ColumnDef) and c.name:
                kind = c.args.get("kind")
                out.append((c.name.lower(), kind.sql(dialect="hive").lower() if kind is not None else None))
        return out

    columns = defs(tree.this)
    partitions: list[tuple[str, str | None]] = []
    for prop in tree.find_all(exp.PartitionedByProperty):
        partitions += defs(prop.this)
    return columns, partitions
