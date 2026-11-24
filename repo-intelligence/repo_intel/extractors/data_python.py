"""Data and ML work in Python and notebooks (spec §8.1–8.2).

One extractor reads Python files, Jupyter notebooks (`.ipynb`) and Databricks
notebook exports (a `.py`, `.sql` or `.scala` file whose first line is
`Databricks notebook source`). Python is read with the standard `ast` module and
never run. It finds:

- PySpark reads and writes: `spark.read…table/load/parquet`, `spark.table`,
  `readStream`, `.write…saveAsTable/insertInto/save`, `writeTo`, `toTable`,
  `DeltaTable.forName/forPath` (a write when merged into), Delta Live Tables
  (`@dlt.table`, `dlt.read`, `apply_changes`), and `spark.sql` text through the
  shared SQL lineage scanner. pandas `read_*` and `to_*` with literal paths.
- Databricks: `%sql` cells, `%run` and `dbutils.notebook.run` (RUNS a notebook),
  `dbutils.secrets.get` and `dbutils.widgets` (ConfigKeys: names only), and
  feature-store tables.
- MLflow: `log_model(registered_model_name=…)` and `register_model` PRODUCE an
  MLModel; `load_model("models:/…")` and `spark_udf` READ one. Hugging Face
  `from_pretrained`, `pipeline(model=…)` and `SentenceTransformer` read `hf:` models.
  Training calls (`fit`, `Trainer`, `mlflow.start_run`, `autolog`) are recorded as
  "ml_signal" observations for the component-kind decision.
- Airflow DAGs: `with DAG(…)`, `DAG(…)` and `@dag` are Jobs; operators with a
  `task_id` and `@task` functions are Tasks; `>>`, `<<`, `set_upstream` and
  `set_downstream` are DEPENDS_ON edges; `SparkSubmitOperator`,
  `DatabricksRunNowOperator`, SQL operators and `outlets` give RUNS and lineage.

A dataset name built from a literal, a module constant, or an f-string over them
is resolved; an f-string over other names keeps them as `{name}` placeholders and
is marked `parameterized`; anything else is counted in the file's
"lineage_summary" as unresolved, never guessed.

The subject of a notebook's reads and writes is the Notebook; elsewhere it is the
package that owns the file ("@package").
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import PACKAGE, Extraction, diagnostic, entity, obs, ref, relation, span, whole_file
from repo_intel.extractors.sql_lineage import lineage

K = EntityKind
P = Predicate
E2 = EvidenceClass.E2

NOTEBOOK_HEADER = re.compile(rb"^\s*(#|--|//)\s*Databricks notebook source")
_COMMAND = re.compile(r"^\s*(?:#|--|//)\s*COMMAND -{6,}\s*$")
_MAGIC = re.compile(r"^\s*(?:#|--|//)\s*MAGIC ?(.*)$")
NOTEBOOK_EXTS = (".py", ".ipynb", ".sql", ".scala", ".r")

READ_METHODS = {"table", "load", "parquet", "csv", "json", "orc", "text", "delta", "avro", "xml", "jdbc"}
WRITE_METHODS = {"saveAsTable", "insertInto", "save", "parquet", "csv", "json", "orc", "text", "toTable", "delta", "avro", "xml", "start"}
PANDAS_READ = {"read_csv", "read_parquet", "read_json", "read_excel", "read_table", "read_feather", "read_orc", "read_delta"}
PANDAS_WRITE = {"to_csv", "to_parquet", "to_json", "to_excel", "to_feather", "to_orc"}
TRAINING_LIBS = ("sklearn", "xgboost", "lightgbm", "catboost", "torch", "tensorflow", "keras", "transformers", "pytorch_lightning", "lightning", "statsmodels", "prophet")


@dataclass
class Cell:
    kind: str  # python | sql | run
    text: str
    lines: list[tuple[int, int]]  # byte span in the file of each line of `text`


@dataclass
class Context:
    path: str
    data: bytes
    blob: str
    subject: Any
    out: list = field(default_factory=list)
    resolved: int = 0
    unresolved: int = 0
    signals: set = field(default_factory=set)


# ------------------------------------------------------------------ notebooks


def _line_spans(data: bytes) -> list[tuple[int, int]]:
    out, offset = [], 0
    for line in data.splitlines(keepends=True):
        out.append((offset, offset + len(line.rstrip(b"\r\n"))))
        offset += len(line)
    return out or [(0, 0)]


def _databricks_cells(data: bytes) -> tuple[str, list[Cell]]:
    """Cells of a Databricks source export. The language is the file's own; `%sql`
    and `%run` cells are written as MAGIC comment lines."""
    text = data.decode("utf-8")
    spans = _line_spans(data)
    lines = text.splitlines()
    marker = lines[0].lstrip()[:2] if lines else "#"
    language = {"#": "python", "--": "sql", "//": "scala"}.get(marker, "python")
    cells: list[Cell] = []
    block: list[int] = []

    def flush():
        if not block:
            return
        magic = [(_MAGIC.match(lines[i]), i) for i in block if lines[i].strip()]
        if magic and all(m for m, _ in magic):
            body = [(m.group(1), i) for m, i in magic]
            head = body[0][0].strip()
            if head.startswith("%sql"):
                cells.append(Cell("sql", "\n".join([head[4:]] + [t for t, _ in body[1:]]), [spans[i] for _, i in body]))
            elif head.startswith("%run"):
                cells.append(Cell("run", head[4:].strip(), [spans[body[0][1]]]))
            elif head.startswith("%python"):
                cells.append(Cell("python", "\n".join([head[7:]] + [t for t, _ in body[1:]]), [spans[i] for _, i in body]))
        elif language in ("python", "sql"):
            cells.append(Cell(language, "\n".join(lines[i] for i in block), [spans[i] for i in block]))
        block.clear()

    for i, line in enumerate(lines):
        if i == 0 or _COMMAND.match(line):
            flush()
            continue
        block.append(i)
    flush()
    return language, cells


def _ipynb_cells(data: bytes) -> tuple[str, list[Cell]]:
    return notebook_cells(json.loads(data.decode("utf-8")), data)


def notebook_cells(doc: Any, data: bytes) -> tuple[str, list[Cell]]:
    """Cells of a Jupyter-shaped document (also a Synapse notebook's `properties`)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("cells"), list):
        raise ValueError("no cells")
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    language = str(((meta.get("kernelspec") or {}).get("language") or (meta.get("language_info") or {}).get("name") or "python")).lower()
    cells: list[Cell] = []
    cursor = 0
    for cell in doc["cells"]:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        raw_lines = source if isinstance(source, list) else str(source).splitlines(keepends=True)
        lines, spans = [], []
        for raw in raw_lines:
            if not isinstance(raw, str):
                continue
            line = raw.rstrip("\r\n")
            needle = json.dumps(line, ensure_ascii=False)[1:-1].encode("utf-8") if line else b""
            at = data.find(needle, cursor) if needle else -1
            if at >= 0:
                spans.append((at, at + len(needle)))
                cursor = at + len(needle)
            else:
                spans.append((cursor, cursor))
            lines.append(line)
        if not lines:
            continue
        head = lines[0].strip()
        if head.startswith("%sql") or head.startswith("%%sql"):
            cells.append(Cell("sql", "\n".join([head.split("sql", 1)[1]] + lines[1:]), spans))
        elif head.startswith("%run"):
            cells.append(Cell("run", head[4:].strip(), spans[:1]))
        elif language == "python" and not head.startswith(("%%", "%md", "%sh", "%scala", "%r ")):
            # Line magics and shell escapes are not Python: blank them, keeping line numbers.
            cells.append(Cell("python", "\n".join("" if l.lstrip().startswith(("%", "!")) else l for l in lines), spans))
    return language, cells


# --------------------------------------------------------------- AST helpers


def _attr_chain(node: ast.AST) -> tuple[list[str], ast.AST | None]:
    """`spark.read.format("delta").load` -> (["read", "format", "load"], Name(spark))."""
    names: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return list(reversed(names)), node


def _root_name(node: ast.AST | None) -> str:
    return node.id if isinstance(node, ast.Name) else ""


def _calls_in_chain(node: ast.AST) -> list[ast.Call]:
    out = []
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        if isinstance(node, ast.Call):
            out.append(node)
            node = node.func
        else:
            node = node.value
    return out


def _kw(call: ast.Call, *names: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg in names), None)


def _arg(call: ast.Call, index: int, *names: str) -> ast.AST | None:
    return call.args[index] if len(call.args) > index else _kw(call, *names)


class _Strings:
    """Resolve string expressions from literals and single-assignment module constants."""

    def __init__(self, trees: list[ast.AST]):
        counts: dict[str, int] = {}
        values: dict[str, ast.AST] = {}
        for tree in trees:
            for node in getattr(tree, "body", []):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            counts[t.id] = counts.get(t.id, 0) + 1
                            if node.value is not None:
                                values[t.id] = node.value
        self.values = {k: v for k, v in values.items() if counts[k] == 1}

    def get(self, node: ast.AST | None, depth: int = 0) -> tuple[str | None, bool]:
        """(text, parameterized). text is None when the value is not knowable."""
        if node is None or depth > 5:
            return None, False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, False
        if isinstance(node, ast.Name) and node.id in self.values:
            return self.get(self.values[node.id], depth + 1)
        if isinstance(node, ast.JoinedStr):
            parts, param = [], False
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    text, p = self.get(v.value, depth + 1)
                    if text is None:
                        if isinstance(v.value, ast.Name):
                            text, p = "{" + v.value.id + "}", True
                        else:
                            return None, False
                    parts.append(text)
                    param = param or p
            return "".join(parts), param
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, lp = self.get(node.left, depth + 1)
            right, rp = self.get(node.right, depth + 1)
            return (left + right, lp or rp) if left is not None and right is not None else (None, False)
        return None, False


def dataset_name(raw: str) -> str | None:
    """Normalize a table name or storage path; None when it is not a dataset reference."""
    raw = raw.strip()
    if not raw or len(raw) > 400 or "\n" in raw:
        return None
    if re.fullmatch(r"[a-z][a-z0-9]*:[\w./-]+", raw) and "://" not in raw:  # azureml:name, dbfs:/path
        return raw.rstrip("/")
    if "://" in raw or raw.startswith(("/", "./", "../")) or "/" in raw:
        return raw.split("?", 1)[0].rstrip("/") or None
    name = raw.replace("`", "")
    if not re.fullmatch(r"[\w${}.-]+", name):
        return None
    return name.lower()


# ----------------------------------------------------------------- the scanner


class _Scan(ast.NodeVisitor):
    def __init__(self, ctx: Context, strings: _Strings, cell: Cell, imports: set[str]):
        self.ctx, self.strings, self.cell, self.imports = ctx, strings, cell, imports
        self.parents: dict[int, ast.AST] = {}

    # -- spans and emission

    def at(self, node: ast.AST):
        lines = self.cell.lines
        first = min(max(getattr(node, "lineno", 1), 1), len(lines)) - 1
        last = min(max(getattr(node, "end_lineno", first + 1), first + 1), len(lines)) - 1
        a, b = lines[first][0], lines[last][1]
        if self.cell.kind == "python" and self.ctx.data[a:b].decode("utf-8", "replace") == self.cell.text.split("\n")[first] and first == last:
            col, end = getattr(node, "col_offset", 0), getattr(node, "end_col_offset", None)
            raw = self.ctx.data[a:b]
            return span(self.ctx.data, a + min(col, len(raw)), a + min(end if end is not None else len(raw), len(raw)))
        return span(self.ctx.data, a, max(b, a))

    def emit(self, o) -> None:
        self.ctx.out.append(o)

    def dataset(self, node: ast.AST, predicate: Predicate, value: ast.AST | None, subject=None, count: bool = True) -> None:
        text, param = self.strings.get(value)
        name = dataset_name(text) if text is not None else None
        if name is None:
            if count:
                self.ctx.unresolved += 1
            return
        self.ctx.resolved += 1
        at = self.at(node)
        target = ref(K.DATASET, {"name": name}, name)
        self.emit(entity(self.ctx.blob, at, K.DATASET, target["natural_key"], name, {"parameterized": True} if param else {}))
        self.emit(relation(self.ctx.blob, at, subject or self.ctx.subject, predicate, target, {"parameterized": True} if param else {}))

    def sql(self, node: ast.AST, value: ast.AST | None, subject=None) -> None:
        text, _ = self.strings.get(value)
        if text is None:
            self.ctx.unresolved += 1
            return
        found = lineage(text, "spark")
        self.ctx.unresolved += found.unresolved
        at = self.at(node)
        for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
            for name, _, _ in items:
                target = ref(K.DATASET, {"name": name}, name)
                self.ctx.resolved += 1
                self.emit(entity(self.ctx.blob, at, K.DATASET, target["natural_key"], name, {}))
                self.emit(relation(self.ctx.blob, at, subject or self.ctx.subject, predicate, target))

    def model(self, node: ast.AST, predicate: Predicate, name: str | None) -> None:
        if not name:
            return
        at = self.at(node)
        target = ref(K.ML_MODEL, {"name": name}, name)
        self.emit(entity(self.ctx.blob, at, K.ML_MODEL, target["natural_key"], name, {"source": "huggingface"} if name.startswith("hf:") else {}))
        self.emit(relation(self.ctx.blob, at, self.ctx.subject, predicate, target))

    def config(self, node: ast.AST, name: str, source: str) -> None:
        at = self.at(node)
        key = {"name": name}
        self.emit(entity(self.ctx.blob, at, K.CONFIG_KEY, key, name, {"source": source}))
        self.emit(relation(self.ctx.blob, at, self.ctx.subject, P.REFERENCES_CONFIG, ref(K.CONFIG_KEY, key, name)))

    # -- traversal

    def run(self, tree: ast.AST) -> None:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent
        self.visit(tree)

    def merged_into(self, node: ast.AST) -> bool:
        cur = self.parents.get(id(node))
        while cur is not None and isinstance(cur, (ast.Attribute, ast.Call)):
            if isinstance(cur, ast.Attribute) and cur.attr == "merge":
                return True
            cur = self.parents.get(id(cur))
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            chain, root = _attr_chain(dec)
            if _root_name(root) == "dlt" and chain and chain[0] in ("table", "view", "materialized_view", "streaming_table"):
                call = dec if isinstance(dec, ast.Call) else None
                value = _kw(call, "name") if call else None
                self.dataset(node, P.WRITES_TO, value or ast.Constant(node.name))
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        chain, root = _attr_chain(node.func)
        base = _root_name(root)
        method = chain[-1] if chain else (base if isinstance(node.func, ast.Name) else "")
        lowered = base.lower()

        # PySpark
        if method in READ_METHODS and ("read" in chain[:-1] or "readStream" in chain[:-1]):
            self.dataset(node, P.READS_FROM, _arg(node, 1, "table") if method == "jdbc" else _arg(node, 0, "path", "tableName"))
        elif method in WRITE_METHODS and ("write" in chain[:-1] or "writeStream" in chain[:-1]):
            value = _arg(node, 0, "path", "name", "tableName")
            if value is None:
                option = next((c for c in _calls_in_chain(node.func) if isinstance(c.func, ast.Attribute) and c.func.attr == "option" and len(c.args) == 2 and self.strings.get(c.args[0])[0] == "path"), None)
                value = option.args[1] if option else None
            if not (method == "start" and value is None):
                self.dataset(node, P.WRITES_TO, value)
        elif method == "writeTo":
            self.dataset(node, P.WRITES_TO, _arg(node, 0, "table"))
        elif method == "table" and len(chain) == 1 and ("spark" in lowered or lowered in ("sqlcontext", "session")):
            self.dataset(node, P.READS_FROM, _arg(node, 0, "tableName"))
        elif method == "sql" and len(chain) == 1 and ("spark" in lowered or lowered in ("sqlcontext", "session")):
            self.sql(node, _arg(node, 0, "sqlQuery"))
        elif base == "DeltaTable" and method in ("forName", "forPath"):
            self.dataset(node, P.WRITES_TO if self.merged_into(node) else P.READS_FROM, _arg(node, 1, "tableOrViewName", "path"))
        # Delta Live Tables
        elif base == "dlt" and method in ("read", "read_stream"):
            self.dataset(node, P.READS_FROM, _arg(node, 0, "name"))
        elif base == "dlt" and method in ("create_streaming_table", "create_streaming_live_table"):
            self.dataset(node, P.WRITES_TO, _arg(node, 0, "name"))
        elif base == "dlt" and method in ("apply_changes", "create_auto_cdc_flow", "apply_changes_from_snapshot"):
            self.dataset(node, P.WRITES_TO, _kw(node, "target"))
            self.dataset(node, P.READS_FROM, _kw(node, "source"), count=False)
        # pandas and polars: literal paths only
        elif method in PANDAS_READ and base in ("pd", "pandas", "pl", "polars"):
            self.dataset(node, P.READS_FROM, _arg(node, 0, "path", "filepath_or_buffer", "io", "source"), count=False)
        elif method in PANDAS_WRITE and chain and len(chain) == 1:
            self.dataset(node, P.WRITES_TO, _arg(node, 0, "path", "path_or_buf", "excel_writer"), count=False)
        # Databricks utilities
        elif base == "dbutils" and chain[:2] == ["secrets", "get"]:
            scope, key = self.strings.get(_arg(node, 0, "scope"))[0], self.strings.get(_arg(node, 1, "key"))[0]
            if scope and key:
                self.config(node, f"{scope}/{key}", "databricks_secret")
        elif base == "dbutils" and chain[:1] == ["widgets"] and len(chain) == 2 and chain[1] in ("get", "getArgument", "text", "dropdown", "combobox", "multiselect"):
            name = self.strings.get(_arg(node, 0, "name"))[0]
            if name:
                self.config(node, name, "databricks_widget")
        elif base == "dbutils" and chain == ["notebook", "run"]:
            target = self.strings.get(_arg(node, 0, "path"))[0]
            if target:
                self.ctx.out += run_notebook(self.ctx, target, self.at(node))
        elif isinstance(node.func, ast.Name) and node.func.id == "getArgument":
            name = self.strings.get(_arg(node, 0))[0]
            if name:
                self.config(node, name, "databricks_widget")
        # Feature store
        elif method in ("create_table", "write_table") and ("fe" in lowered or "fs" in lowered or "feature" in lowered):
            self.dataset(node, P.WRITES_TO, _kw(node, "name"))
        elif method == "FeatureLookup" or (isinstance(node.func, ast.Name) and node.func.id == "FeatureLookup"):
            self.dataset(node, P.READS_FROM, _kw(node, "table_name"))
        # MLflow
        elif base == "mlflow" and method == "log_model":
            self.ctx.signals.add("mlflow_log_model")
            name = self.strings.get(_kw(node, "registered_model_name"))[0]
            self.model(node, P.PRODUCES, name)
        elif base == "mlflow" and method == "register_model":
            self.ctx.signals.add("mlflow_log_model")
            self.model(node, P.PRODUCES, self.strings.get(_arg(node, 1, "name"))[0])
        elif base == "mlflow" and method in ("load_model", "spark_udf"):
            uri = self.strings.get(_arg(node, 1, "model_uri") if method == "spark_udf" else _arg(node, 0, "model_uri"))[0]
            if uri and uri.startswith("models:/"):
                self.model(node, P.READS_FROM, uri[len("models:/"):].split("/")[0].split("@")[0])
        elif base == "mlflow" and (method in ("start_run", "autolog") or chain[-1:] == ["autolog"]):
            self.ctx.signals.add("mlflow_run")
        # Hugging Face
        elif method == "from_pretrained" or (isinstance(node.func, ast.Name) and node.func.id in ("SentenceTransformer", "pipeline")):
            value = _arg(node, 0, "pretrained_model_name_or_path", "model_name_or_path") if method == "from_pretrained" or (isinstance(node.func, ast.Name) and node.func.id == "SentenceTransformer") else _kw(node, "model")
            text = self.strings.get(value)[0]
            if text and re.fullmatch(r"[A-Za-z0-9][\w.-]*(/[\w.-]+)?", text):
                self.model(node, P.READS_FROM, f"hf:{text}")
        # Training signals
        if method in ("fit", "fit_generator", "partial_fit") and self.imports & set(TRAINING_LIBS):
            self.ctx.signals.add("fit")
        elif method == "train" and isinstance(node.func, ast.Attribute) and ("trainer" in lowered or "xgb" in lowered or "lgb" in lowered):
            self.ctx.signals.add("fit")
        elif isinstance(node.func, ast.Name) and node.func.id in ("Trainer", "SFTTrainer", "Seq2SeqTrainer"):
            self.ctx.signals.add("fit")
        self.generic_visit(node)


def run_notebook(ctx: Context, target: str, at) -> list:
    """RUNS edges to the notebook `target` names, resolved against this notebook's
    directory. Databricks paths omit the extension, so every candidate is referred
    to without being declared, and only the one that exists in the unit survives."""
    target = target.strip().strip("\"'")
    if not target or "{" in target or target.startswith(("/Workspace", "/Repos", "/Shared", "/Users")):
        ctx.unresolved += 1
        return []
    path = posixpath.normpath(posixpath.join(posixpath.dirname(ctx.path), target)) if not target.startswith("/") else target.lstrip("/")
    if path.startswith(".."):
        return []
    ctx.resolved += 1
    stem, ext = posixpath.splitext(path)
    candidates = [path] if ext in NOTEBOOK_EXTS else [path + e for e in NOTEBOOK_EXTS]
    return [relation(ctx.blob, at, ctx.subject, P.RUNS, ref(K.NOTEBOOK, {"path": c}, posixpath.basename(c), declare=False)) for c in candidates]


# -------------------------------------------------------------------- Airflow


_SQL_OPERATOR = re.compile(r"(Sql|SQL|Query|Snowflake|Postgres|Mssql|Mysql|Redshift|Databricks)\w*Operator$")


class _Airflow:
    def __init__(self, ctx: Context, strings: _Strings, scan: _Scan):
        self.ctx, self.strings, self.scan = ctx, strings, scan
        self.task_funcs: dict[str, str] = {}
        self.datasets: dict[str, ast.Call] = {}  # module-level NAME = Dataset(...)

    def dataset_call(self, node: ast.AST) -> ast.Call | None:
        """An Airflow Dataset/Asset written inline or through a module-level name."""
        if isinstance(node, ast.Name):
            return self.datasets.get(node.id)
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("Dataset", "Asset"):
            return node
        return None

    def task_ref(self, dag: str, task: str):
        return ref(K.TASK, {"framework": "airflow", "job": dag, "name": task}, task)

    def run(self, tree: ast.Module) -> None:
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if (call := self.dataset_call(node.value)) is not None:
                    self.datasets[node.targets[0].id] = call
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    chain, root = _attr_chain(dec)
                    if _root_name(root) == "task" or (isinstance(dec, ast.Name) and dec.id == "task"):
                        call = dec if isinstance(dec, ast.Call) else None
                        self.task_funcs[node.name] = (self.strings.get(_kw(call, "task_id"))[0] if call else None) or node.name
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    if self.is_dag(item.context_expr):
                        self.dag(item.context_expr, node.body, item.optional_vars)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Name) and dec.id == "dag") or (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "dag"):
                        call = dec if isinstance(dec, ast.Call) else ast.Call(func=dec, args=[], keywords=[])
                        dag_id = self.strings.get(_kw(call, "dag_id"))[0] or node.name
                        self.dag(call, node.body, None, dag_id=dag_id, at_node=node)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and self.is_dag(node.value) and isinstance(node.targets[0], ast.Name):
                # dag = DAG(...): its tasks name it with dag=dag
                body = [n for n in tree.body if isinstance(n, (ast.Assign, ast.Expr)) and any(isinstance(k, ast.keyword) and k.arg == "dag" and isinstance(k.value, ast.Name) and k.value.id == node.targets[0].id for c in ast.walk(n) if isinstance(c, ast.Call) for k in c.keywords)]
                edges = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.BinOp)]
                self.dag(node.value, body + edges, None)

    @staticmethod
    def is_dag(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "DAG") or (isinstance(node.func, ast.Attribute) and node.func.attr == "DAG"))

    def dag(self, call: ast.Call, body: list[ast.stmt], alias, dag_id: str | None = None, at_node: ast.AST | None = None) -> None:
        dag_id = dag_id or self.strings.get(_arg(call, 0, "dag_id"))[0]
        if not dag_id:
            self.ctx.unresolved += 1
            return
        attrs: dict[str, Any] = {}
        schedule = _kw(call, "schedule", "schedule_interval")
        text = self.strings.get(schedule)[0]
        if text:
            attrs["schedule"] = text
        elif isinstance(schedule, ast.Constant) and schedule.value is None:
            attrs["schedule"] = "none"
        elif isinstance(schedule, (ast.List, ast.Tuple)):
            attrs["schedule"] = "datasets"
        at = self.scan.at(at_node or call)
        job = ref(K.JOB, {"framework": "airflow", "name": dag_id}, dag_id)
        self.ctx.out.append(entity(self.ctx.blob, at, K.JOB, job["natural_key"], dag_id, attrs, E2))
        if isinstance(schedule, (ast.List, ast.Tuple)):
            for item in schedule.elts:
                if (ds := self.dataset_call(item)) is not None:
                    self.scan.dataset(item, P.READS_FROM, _arg(ds, 0, "uri"), subject=job)
        tasks: dict[str, str] = {}  # variable -> task id
        groups: list[str] = []

        def declare(node: ast.Call, task_id: str, operator: str) -> str:
            full = ".".join(groups + [task_id])
            me = self.task_ref(dag_id, full)
            where = self.scan.at(node)
            self.ctx.out.append(entity(self.ctx.blob, where, K.TASK, me["natural_key"], full, {"operator": operator}, E2))
            self.ctx.out.append(relation(self.ctx.blob, where, job, P.CONTAINS, me, {}, E2))
            if operator == "SparkSubmitOperator":
                app = self.strings.get(_kw(node, "application"))[0]
                if app and not app.startswith("/") and "{" not in app:
                    self.ctx.out.append(relation(self.ctx.blob, where, me, P.RUNS, ref(K.ARTIFACT, {"path": posixpath.normpath(app)}, posixpath.basename(app), declare=False)))
            elif operator == "DatabricksRunNowOperator":
                name = self.strings.get(_kw(node, "job_name"))[0]
                if name:
                    self.ctx.out.append(relation(self.ctx.blob, where, me, P.RUNS, ref(K.JOB, {"framework": "databricks", "name": name}, name, declare=False)))
            elif _SQL_OPERATOR.search(operator) and _kw(node, "sql") is not None:
                self.scan.sql(node, _kw(node, "sql"), subject=me)
            for key, predicate in (("outlets", P.WRITES_TO), ("inlets", P.READS_FROM)):
                items = _kw(node, key)
                for item in (items.elts if isinstance(items, (ast.List, ast.Tuple)) else []):
                    if (ds := self.dataset_call(item)) is not None:
                        self.scan.dataset(item, predicate, _arg(ds, 0, "uri"), subject=me)
            return full

        def task_of(node: ast.AST) -> list[str]:
            if isinstance(node, ast.Name):
                return [tasks[node.id]] if node.id in tasks else []
            if isinstance(node, (ast.List, ast.Tuple)):
                return [t for e in node.elts for t in task_of(e)]
            if isinstance(node, ast.Call):
                return operator_call(node)
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
                left, right = task_of(node.left), task_of(node.right)
                upstream, downstream = (left, right) if isinstance(node.op, ast.RShift) else (right, left)
                for d in downstream:
                    for u in upstream:
                        edge(node, d, u)
                return right
            return []

        def edge(node: ast.AST, downstream: str, upstream: str) -> None:
            if downstream != upstream:
                self.ctx.out.append(relation(self.ctx.blob, self.scan.at(node), self.task_ref(dag_id, downstream), P.DEPENDS_ON, self.task_ref(dag_id, upstream), {}, E2))

        def operator_call(node: ast.Call) -> list[str]:
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            task_id = self.strings.get(_kw(node, "task_id"))[0]
            if task_id:
                return [declare(node, task_id, name)]
            if name in self.task_funcs:
                me = declare(node, self.task_funcs[name], "task")
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    for upstream in task_of(arg):  # TaskFlow: passing a task's output makes it upstream
                        edge(node, me, upstream)
                return [me]
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("set_downstream", "set_upstream"):
                me = task_of(node.func.value)
                others = [t for a in node.args for t in task_of(a)]
                for m in me:
                    for o in others:
                        edge(node, o, m) if node.func.attr == "set_downstream" else edge(node, m, o)
            return []

        def walk(stmts: list[ast.stmt]) -> None:
            for stmt in stmts:
                if isinstance(stmt, ast.With):
                    group = next((self.strings.get(_arg(i.context_expr, 0, "group_id"))[0] for i in stmt.items if isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "id", getattr(i.context_expr.func, "attr", "")) == "TaskGroup"), None)
                    if group:
                        groups.append(group)
                    walk(stmt.body)
                    if group:
                        groups.pop()
                elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Call, ast.BinOp)):
                    found = task_of(stmt.value)
                    for t in stmt.targets:
                        if isinstance(t, ast.Name) and found:
                            tasks[t.id] = found[-1]
                elif isinstance(stmt, ast.Expr):
                    task_of(stmt.value)
                elif isinstance(stmt, (ast.For, ast.If)):
                    walk(stmt.body)

        walk(body)


# ------------------------------------------------------------------ extractor


class DataPython:
    id = "data.python"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(NOTEBOOK_EXTS)

    def extract(self, path, data, blob):
        lower = path.lower()
        notebook_format = "ipynb" if lower.endswith(".ipynb") else "databricks" if NOTEBOOK_HEADER.match(data[:200]) else None
        if notebook_format is None and not lower.endswith(".py"):
            return Extraction(parsed=False)
        try:
            if notebook_format == "ipynb":
                language, cells = _ipynb_cells(data)
            elif notebook_format == "databricks":
                language, cells = _databricks_cells(data)
            else:
                language, cells = "python", [Cell("python", data.decode("utf-8"), _line_spans(data))]
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unreadable notebook: {type(exc).__name__}")], parsed=notebook_format is None)
        return scan_cells(path, data, blob, language, cells, notebook_format, self.id)


def scan_cells(path: str, data: bytes, blob: str, language: str, cells: list[Cell], notebook_format: str | None, extractor_id: str = "data.python") -> Extraction:
    """Everything this module finds in `cells`; a notebook format makes the file a Notebook."""
    out: list = []
    subject: Any = PACKAGE
    if notebook_format:
        me = ref(K.NOTEBOOK, {"path": path}, posixpath.basename(path))
        out.append(entity(blob, whole_file(data) if not cells else span(data, cells[0].lines[0][0], cells[-1].lines[-1][1]), K.NOTEBOOK, me["natural_key"], me["name"], {"format": notebook_format, "language": language}, E2))
        subject = me
    ctx = Context(path, data, blob, subject, out)
    trees: list[tuple[Cell, ast.Module]] = []
    diagnostics = []
    for cell in cells:
        if cell.kind == "python":
            try:
                trees.append((cell, ast.parse(cell.text)))
            except (SyntaxError, ValueError, RecursionError):
                if notebook_format is None:
                    return Extraction(parsed=False)  # python_source reports the syntax error
                diagnostics.append(diagnostic(extractor_id, "info", "a notebook cell is not valid Python"))
    strings = _Strings([t for _, t in trees])
    imports = {(a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0] for _, t in trees for n in ast.walk(t) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    for cell in cells:
        scan = _Scan(ctx, strings, cell, imports)
        if cell.kind == "sql":
            found = lineage(cell.text, "databricks")
            ctx.unresolved += found.unresolved
            for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
                for name, a, b in items:
                    line = min(cell.text.count("\n", 0, a), len(cell.lines) - 1)
                    at = span(data, *cell.lines[line])
                    target = ref(K.DATASET, {"name": name}, name)
                    ctx.resolved += 1
                    out.append(entity(blob, at, K.DATASET, target["natural_key"], name, {}))
                    out.append(relation(blob, at, subject, predicate, target))
        elif cell.kind == "run":
            out += run_notebook(ctx, cell.text.split()[0] if cell.text.split() else "", span(data, *cell.lines[0]))
    for cell, tree in trees:
        scan = _Scan(ctx, strings, cell, imports)
        scan.run(tree)
        if "airflow" in imports:
            _Airflow(ctx, strings, scan).run(tree)
    if ctx.signals:
        out.append(obs("ml_signal", blob, whole_file(data), {"signals": sorted(ctx.signals)}, E2))
    if ctx.resolved or ctx.unresolved:
        out.append(obs("lineage_summary", blob, whole_file(data), {"resolved": ctx.resolved, "unresolved": ctx.unresolved}, E2))
    if not notebook_format and not out:
        return Extraction(parsed=False)
    return Extraction(out, diagnostics)


EXTRACTORS = [DataPython()]
