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


# -------------------------------------------------- PySpark transform shapes

# How DataFrame method chains become transform shapes (repo_intel.compile.transforms). The
# chain is read, never run: a variable bound to a DataFrame keeps the id of the transform
# that made it, so `df2 = df.filter(...)` continues `df`'s chain. A DataFrame whose origin is
# not in the unit (a function parameter) is `{name}`, and only when the chain uses methods
# that only PySpark DataFrames have, so pandas code is never read as Spark.
_SPARK_ONLY = frozenset({"withColumn", "withColumns", "withColumnRenamed", "withColumnsRenamed", "groupBy", "dropDuplicates", "unionByName", "unionAll", "selectExpr", "crossJoin", "orderBy", "createOrReplaceTempView", "saveAsTable", "insertInto", "writeTo", "writeStream"})
_PASS_THROUGH = frozenset({"alias", "cache", "persist", "repartition", "coalesce", "hint", "checkpoint", "localCheckpoint", "repartitionByRange", "sortWithinPartitions", "toDF", "withWatermark", "dropDuplicatesWithinWatermark"})
_TERMINAL = frozenset({"show", "display", "collect", "count", "toPandas", "take", "head", "first", "printSchema", "explain", "foreach", "foreachPartition", "isEmpty", "describe", "summary", "createOrReplaceTempView", "createTempView", "createOrReplaceGlobalTempView", "unpersist"})
_JOIN_HOW = {
    "inner": "inner", "left": "left", "leftouter": "left", "left_outer": "left", "right": "right", "rightouter": "right", "right_outer": "right",
    "full": "full", "outer": "full", "fullouter": "full", "full_outer": "full", "semi": "semi", "leftsemi": "semi", "left_semi": "semi",
    "anti": "anti", "leftanti": "anti", "left_anti": "anti", "cross": "cross",
}
_AGG_FUNCTIONS = frozenset({"sum", "count", "avg", "mean", "min", "max", "first", "last", "collect_list", "collect_set", "countDistinct", "count_distinct", "approx_count_distinct", "stddev", "variance", "sumDistinct", "sum_distinct", "median", "any_value", "max_by", "min_by"})
_RANKS = frozenset({"row_number", "rank", "dense_rank"})
_HASHES = frozenset({"sha1", "sha2", "md5", "hash", "xxhash64", "crc32"})


class _Group:
    """`df.groupBy(keys)` (and `.pivot(column)`), waiting for its aggregation."""

    def __init__(self, ref: str, keys: list[str], at, pivot: str | None = None):
        self.ref, self.keys, self.at, self.pivot = ref, keys, at, pivot


class _Delta:
    """`DeltaTable.forName(spark, name)`, waiting for a merge, update or delete."""

    def __init__(self, name: str):
        self.name = name


class _SparkShapes:
    """The transform shapes of one scope of PySpark code (a notebook's or module's top level,
    or one function), built into a `transforms.Builder`."""

    def __init__(self, scan: "_Scan", strings: _Strings, functions: set[str], names: set[str]):
        from repo_intel.compile import transforms as tx

        self.tx, self.scan, self.strings = tx, scan, strings
        self.functions, self.names = functions, names  # aliases of pyspark.sql.functions, and names imported from it
        self.b = tx.Builder()
        self.env: dict[str, Any] = {}
        self.windows: dict[str, dict[str, Any]] = {}
        self.views: dict[str, str] = {}  # temporary view name -> the transform that made it
        self.hash_columns: set[str] = set()
        self.spark_chain = False

    def span(self, node: ast.AST) -> list[int]:
        s = self.scan.at(node)
        return [s.start_byte, s.end_byte]

    # -- statements

    def block(self, body: list[ast.stmt], dlt_target: str | None = None) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # its own unit
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                window = self.window(stmt.value)
                if window is not None and isinstance(target, ast.Name):
                    self.windows[target.id] = window
                    continue
                value = self.expr(stmt.value)
                if isinstance(target, ast.Name):
                    self.bind(target.id, value)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                self.bind(stmt.target.id, self.expr(stmt.value))
            elif isinstance(stmt, ast.Expr):
                self.expr(stmt.value)
            elif isinstance(stmt, ast.Return) and stmt.value is not None:
                value = self.expr(stmt.value)
                if dlt_target and isinstance(value, str):
                    self.b.write(value, dlt_target, self.span(stmt))
            elif isinstance(stmt, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                for field_name in ("body", "orelse", "finalbody"):
                    self.block(getattr(stmt, field_name, []) or [])
                for handler in getattr(stmt, "handlers", []) or []:
                    self.block(handler.body)

    def bind(self, name: str, value: Any) -> None:
        if value is not None:
            self.env[name] = value
        else:  # rebound to something that is not a DataFrame
            self.env.pop(name, None)
            self.windows.pop(name, None)

    def expr(self, node: ast.AST) -> Any:
        self.spark_chain = any(isinstance(n, ast.Attribute) and n.attr in _SPARK_ONLY for n in ast.walk(node))
        try:
            return self.df(node)
        finally:
            self.spark_chain = False

    # -- names and columns

    def fn(self, node: ast.AST) -> str | None:
        """The pyspark.sql.functions function a call calls: `F.trim(...)` or an imported `trim(...)`."""
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.functions:
            return f.attr
        if isinstance(f, ast.Name) and f.id in self.names:
            return f.id
        return None

    def column(self, node: ast.AST | None, strings: bool = True) -> str | None:
        """A column named literally: `F.col("x")`, `"x"` (where a name is expected), `df["x"]`, `df.x`."""
        if node is None:
            return None
        if strings and isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[\w.]+", node.value):
            return node.value.lower().rsplit(".", 1)[-1]
        if self.fn(node) in ("col", "column") and node.args:
            return self.column(node.args[0])
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in self.env and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            return node.slice.value.lower()
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.env and not node.attr.startswith("_"):
            return node.attr.lower()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("alias", "name") and not node.args[1:]:
            return self.column(node.func.value, strings)
        return None

    def columns_in(self, node: ast.AST) -> set[str]:
        out = set()
        for n in ast.walk(node):
            c = self.column(n, strings=False)
            if c:
                out.add(c)
        if self.fn(node) is not None:
            out |= {c for a in node.args if (c := self.column(a))}
        return out

    def names_of(self, args: list[ast.AST]) -> list[str] | None:
        """Column names from arguments that are names, columns or lists of them; None otherwise."""
        out: list[str] = []
        for a in args:
            items = a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]
            for item in items:
                c = self.column(item)
                if c is None:
                    return None
                out.append(c)
        return out

    def order_of(self, args: list[ast.AST]) -> list[dict[str, Any]] | None:
        out = []
        for a in args:
            for item in (a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]):
                desc = False
                if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr in ("desc", "asc", "desc_nulls_last", "desc_nulls_first", "asc_nulls_first", "asc_nulls_last") and not item.args:
                    desc, item = item.func.attr.startswith("desc"), item.func.value
                elif self.fn(item) in ("desc", "asc") and item.args:
                    desc, item = self.fn(item) == "desc", item.args[0]
                c = self.column(item)
                if c is None:
                    return None
                out.append({"column": c, "desc": desc})
        return out

    @staticmethod
    def literal(node: ast.AST) -> tuple[bool, Any]:
        if isinstance(node, ast.Constant):
            return True, node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            return True, -node.operand.value if isinstance(node.operand.value, (int, float)) else None
        return False, None

    def lit(self, node: ast.AST) -> tuple[bool, Any]:
        if self.fn(node) == "lit" and node.args:
            return self.literal(node.args[0])
        return self.literal(node)

    def window(self, node: ast.AST | None) -> dict[str, Any] | None:
        """`Window.partitionBy(k).orderBy(t).rowsBetween(...)` as keys, order and frame."""
        if isinstance(node, ast.Name):
            return self.windows.get(node.id)
        chain, root = _attr_chain(node) if node is not None else ([], None)
        if _root_name(root) != "Window" or not chain:
            return None
        spec: dict[str, Any] = {"keys": [], "order": [], "frame": None}
        for call in reversed(_calls_in_chain(node)):
            method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            if method == "partitionBy":
                spec["keys"] = self.names_of(call.args) or []
            elif method == "orderBy":
                spec["order"] = self.order_of(call.args) or []
            elif method in ("rowsBetween", "rangeBetween"):
                bounds = [a.attr if isinstance(a, ast.Attribute) else self.literal(a)[1] for a in call.args[:2]]
                spec["frame"] = "running" if bounds[:1] == ["unboundedPreceding"] and bounds[1:] in (["currentRow"], [0]) else "other"
        return spec

    # -- column effects

    def item(self, node: ast.AST, target: str | None) -> dict[str, Any]:
        """The column effect of one column expression written as `target`."""
        tx = self.tx
        sources = self.columns_in(node)
        fn = self.fn(node)
        first = self.column(node.args[0]) if fn and node.args else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("cast", "astype"):
            c = self.column(node.func.value, strings=False)
            return tx.column_item(target, sources, None, ["safe_cast"], single=c) if c else tx.column_item(target, sources, "derive_column")
        if fn in ("trim", "ltrim", "rtrim", "btrim") and first:
            return tx.column_item(target, sources, "trim_strings", single=first)
        if fn in ("upper", "lower", "initcap") and first:
            return tx.column_item(target, sources, "normalize_case", single=first)
        if fn in ("to_date", "to_timestamp", "to_timestamp_ntz", "to_timestamp_ltz", "try_to_timestamp") and first:
            return tx.column_item(target, sources, "parse_timestamp", single=first)
        if fn == "coalesce" and len(node.args) >= 2 and first:
            if all(self.lit(a)[0] for a in node.args[1:]):
                return tx.column_item(target, sources, "apply_defaults", single=first)
            if all(self.column(a) for a in node.args):
                return tx.column_item(target, sources, "coalesce_columns")
        if fn == "expr" and node.args:
            text = self.strings.get(node.args[0])[0]
            tree = tx.parse_sql(text, "spark") if text else None
            if tree is not None:
                return tx.classify_sql_value(tree, target)
        if fn in _HASHES and target:
            self.hash_columns.add(target)
        if self.when_chain(node) is not None:
            return self.when_item(node, target, sources)
        if isinstance(node, (ast.Compare, ast.BoolOp)) or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr))) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert)):
            return tx.column_item(target, sources, "conditional_flag")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("isNull", "isNotNull", "isin", "like", "rlike", "between", "startswith", "endswith", "contains", "eqNullSafe"):
            return tx.column_item(target, sources, "conditional_flag")
        return tx.column_item(target, sources, "derive_column")

    def when_chain(self, node: ast.AST) -> list[tuple[ast.AST | None, ast.AST]] | None:
        """`F.when(c1, v1).when(c2, v2).otherwise(v3)` as [(c1, v1), (c2, v2), (None, v3)]."""
        chain: list[tuple[ast.AST | None, ast.AST]] = []
        while isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "otherwise" and len(node.args) == 1 and not chain:
                chain.append((None, node.args[0]))
                node = node.func.value
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "when" and len(node.args) == 2 and self.fn(node) is None:
                chain.append((node.args[0], node.args[1]))
                node = node.func.value
            elif self.fn(node) == "when" and len(node.args) == 2:
                chain.append((node.args[0], node.args[1]))
                return list(reversed(chain))
            else:
                return None
        return None

    def when_item(self, node: ast.AST, target: str | None, sources: set[str]) -> dict[str, Any]:
        tx = self.tx
        chain = self.when_chain(node) or []
        values = [self.lit(v) for _, v in chain]
        if not all(ok for ok, _ in values):
            return tx.column_item(target, sources, "derive_column")
        if len(chain) == 2 and chain[1][0] is None and tx.is_flag_pair([v for _, v in values]):
            return tx.column_item(target, sources, "conditional_flag")
        tested = set()
        for cond, _ in chain:
            if cond is None:
                continue
            if isinstance(cond, ast.Compare) and len(cond.ops) == 1 and isinstance(cond.ops[0], ast.Eq) and self.lit(cond.comparators[0])[0]:
                tested.add(self.column(cond.left, strings=False))
            elif isinstance(cond, ast.Call) and isinstance(cond.func, ast.Attribute) and cond.func.attr == "isin":
                tested.add(self.column(cond.func.value, strings=False))
            else:
                tested.add(None)
        if len(tested) == 1 and None not in tested and len(chain) >= 2:
            return tx.column_item(target, sources, "map_codes", single=next(iter(tested)))
        return tx.column_item(target, sources, "derive_column")

    def predicate(self, node: ast.AST) -> dict[str, Any] | None:
        """A column condition as a behavioural-IR expression; None when it is not one we read."""
        c = self.column(node, strings=False)
        if c:
            return {"e": "var", "name": c}
        ok, value = self.lit(node)
        if ok:
            kind = "null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            return {"e": "lit", "value": value, "type": kind}
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}.get(type(node.ops[0]))
            left, right = self.predicate(node.left), self.predicate(node.comparators[0])
            return {"e": "binop", "op": op, "left": left, "right": right} if op and left and right else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr)):
            left, right = self.predicate(node.left), self.predicate(node.right)
            return {"e": "binop", "op": "and" if isinstance(node.op, ast.BitAnd) else "or", "left": left, "right": right} if left and right else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Invert, ast.Not)):
            inner = self.predicate(node.operand)
            return {"e": "unop", "op": "not", "operand": inner} if inner else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            subject = self.predicate(node.func.value)
            if subject is None:
                return None
            if node.func.attr == "isNull" and not node.args:
                return {"e": "unop", "op": "missing", "operand": subject}
            if node.func.attr == "isNotNull" and not node.args:
                return {"e": "unop", "op": "not", "operand": {"e": "unop", "op": "missing", "operand": subject}}
            if node.func.attr == "isin":
                items = [self.predicate(a) for a in node.args]
                return {"e": "binop", "op": "in", "left": subject, "right": {"e": "array", "items": items}} if items and all(items) else None
        return None

    def rank_test(self, node: ast.AST) -> tuple[str, int] | None:
        """`F.col("rn") == 1`, `col("rn") <= 3` as (column, n)."""
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
            return None
        c, (ok, n) = self.column(node.left, strings=False), self.lit(node.comparators[0])
        if not c or not ok or not isinstance(n, int) or isinstance(n, bool):
            return None
        op = node.ops[0]
        if isinstance(op, ast.Eq) and n == 1:
            return c, 1
        if isinstance(op, ast.LtE) and n >= 1:
            return c, n
        if isinstance(op, ast.Lt) and n >= 2:
            return c, n - 1
        return None

    # -- DataFrames

    def source_name(self, value: ast.AST | None) -> str | None:
        text, _ = self.strings.get(value)
        return dataset_name(text) if text is not None else None

    def read(self, node: ast.AST, value: ast.AST | None, construct: str) -> str:
        name = self.source_name(value)
        return name if name is not None else self.b.add("platform", self.span(node), [], construct=construct, dynamic=True)

    def df(self, node: ast.AST | None) -> Any:
        """The transform id (or table name) `node` evaluates to, a pending groupBy or Delta
        table, or None when it is not a DataFrame this unit tracks."""
        b = self.b
        if node is None:
            return None
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            if self.spark_chain and node.id not in ("spark", "F", "dlt", "self", "Window", "DeltaTable") and node.id not in self.functions:
                self.env[node.id] = "{" + node.id + "}"  # a DataFrame from outside the unit, kept by name
                return self.env[node.id]
            return None
        if isinstance(node, ast.Await):
            return self.df(node.value)
        if not isinstance(node, ast.Call):
            return None
        chain, root = _attr_chain(node.func)
        base = _root_name(root)
        method = chain[-1] if chain else ""
        at = self.span(node)
        lowered = base.lower()
        # Reads and writes, as the lineage scanner reads them.
        if method in READ_METHODS and ("read" in chain[:-1] or "readStream" in chain[:-1]):
            return self.read(node, _arg(node, 1, "table") if method == "jdbc" else _arg(node, 0, "path", "tableName"), "pyspark:read")
        if method == "table" and len(chain) == 1 and ("spark" in lowered or lowered in ("sqlcontext", "session")):
            return self.read(node, _arg(node, 0, "tableName"), "pyspark:table")
        if base == "dlt" and method in ("read", "read_stream") and len(chain) == 1:
            return self.read(node, _arg(node, 0, "name"), "pyspark:dlt_read")
        if method == "sql" and len(chain) == 1 and ("spark" in lowered or lowered in ("sqlcontext", "session")):
            text, _ = self.strings.get(_arg(node, 0, "sqlQuery"))
            if text is None:
                return b.add("platform", at, [], construct="pyspark:sql", dynamic=True)
            return self.tx.sql_query(b, text, at, dialect="spark", qualify=lambda n: self.views.get(n, n))
        if base == "DeltaTable" and method in ("forName", "forPath") and len(chain) == 1:
            name = self.source_name(_arg(node, 1, "tableOrViewName", "path"))
            return _Delta(name) if name else None
        if "write" in chain[:-1] or "writeStream" in chain[:-1] or "writeTo" in chain:
            return self.write(node, chain, at)
        if not isinstance(node.func, ast.Attribute):
            return None
        recv = self.df(node.func.value)
        if isinstance(recv, _Group):
            return self.aggregate(recv, method, node, at)
        if isinstance(recv, _Delta):
            return _Delta(recv.name) if method == "alias" else self.delta(recv, node, at)
        if not isinstance(recv, str):
            return None
        return self.method(recv, method, node, at)

    def write(self, node: ast.Call, chain: list[str], at: list[int]) -> None:
        """`df.write.mode(...).saveAsTable(name)`, `df.writeTo(name).append()`: the DataFrame is
        written to `name`, or to a name the unit does not know (dynamic)."""
        target_call = None
        source = None
        for call in _calls_in_chain(node):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "writeTo":
                target_call, source = call, call.func.value
        cur: ast.AST = node.func
        while source is None and isinstance(cur, (ast.Attribute, ast.Call)):
            if isinstance(cur, ast.Attribute) and cur.attr in ("write", "writeStream"):
                source = cur.value
                break
            cur = cur.func if isinstance(cur, ast.Call) else cur.value
        method = chain[-1]
        if target_call is not None:
            value = _arg(target_call, 0, "table")
        elif method in WRITE_METHODS:
            value = _arg(node, 0, "path", "name", "tableName")
            if value is None:
                option = next((c for c in _calls_in_chain(node.func) if isinstance(c.func, ast.Attribute) and c.func.attr == "option" and len(c.args) == 2 and self.strings.get(c.args[0])[0] == "path"), None)
                value = option.args[1] if option else None
        else:
            return None  # a writer being configured, not yet written
        ref = self.df(source)
        if not isinstance(ref, str):
            return None
        name = self.source_name(value)
        if name is None:
            if method == "start" and value is None:
                return None  # a stream to a sink the code configures elsewhere
            self.b.add("platform", at, [ref], construct="pyspark:write", dynamic=True)
            return None
        self.b.write(ref, name, at)
        return None

    def method(self, ref: str, method: str, node: ast.Call, at: list[int]) -> Any:
        b, tx = self.b, self.tx
        args = node.args
        if method in _PASS_THROUGH:
            return ref
        if method in ("createOrReplaceTempView", "createTempView", "createOrReplaceGlobalTempView", "createGlobalTempView") and args:
            view = self.strings.get(args[0])[0]
            if view:
                self.views[view.lower()] = ref  # `spark.sql` text that reads the view continues this chain
            return None
        if method in _TERMINAL:
            return None
        if method in ("select",):
            items, star = [], False
            for a in args:
                for item in (a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]):
                    if isinstance(item, ast.Constant) and item.value == "*":
                        star = True
                        continue
                    if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "alias" and item.args:
                        target = (self.strings.get(item.args[0])[0] or "").lower() or None
                        inner = item.func.value
                        c = self.column(inner)
                        if c:
                            items.append(tx.keep_item(c, target))
                        elif self.is_window(inner):
                            ref = self.over(ref, inner, target, at)
                        else:
                            items.append(self.item(inner, target))
                        continue
                    c = self.column(item)
                    if c:
                        items.append(tx.keep_item(c))
                    else:
                        items.append(self.item(item, None))
            return tx.emit_projection(b, at, ref, [i for i in items if i.get("target")], keep_only=not star)
        if method == "selectExpr":
            texts = [self.strings.get(a)[0] for a in args]
            tree = tx.parse_sql("SELECT " + ", ".join(texts) + " FROM __df", "spark") if texts and all(texts) else None
            if tree is None:
                return b.add("platform", at, [ref], construct="pyspark:selectExpr")
            r = tx.SqlRecognizer(b)
            r.ctes["__df"] = ref
            return r.query(tree, at)
        if method in ("withColumn", "withColumns"):
            pairs = []
            if method == "withColumn" and len(args) >= 2:
                pairs = [(self.strings.get(args[0])[0], args[1])]
            elif method == "withColumns" and args and isinstance(args[0], ast.Dict):
                pairs = [(self.strings.get(k)[0] if k is not None else None, v) for k, v in zip(args[0].keys, args[0].values)]
            if not pairs or any(name is None for name, _ in pairs):
                return b.add("platform", at, [ref], construct=f"pyspark:{method}")
            items = []
            for name, value in pairs:
                if self.is_window(value) or self.delta_window(value):
                    if items:
                        ref = tx.emit_projection(b, at, ref, items, keep_only=False)
                        items = []
                    ref = self.over(ref, value, name.lower(), at)
                else:
                    items.append(self.item(value, name.lower()))
            return tx.emit_projection(b, at, ref, items, keep_only=False) if items else ref
        if method == "withColumnRenamed" and len(args) == 2:
            old, new = self.strings.get(args[0])[0], self.strings.get(args[1])[0]
            if old and new:
                return b.add("project", at, [ref], columns={"rename": {old.lower(): new.lower()}}, primitive="rename_columns")
        if method == "withColumnsRenamed" and args and isinstance(args[0], ast.Dict):
            pairs = {self.strings.get(k)[0]: self.strings.get(v)[0] for k, v in zip(args[0].keys, args[0].values) if k is not None}
            if pairs and all(k and v for k, v in pairs.items()):
                return b.add("project", at, [ref], columns={"rename": {k.lower(): v.lower() for k, v in pairs.items()}}, primitive="rename_columns")
        if method == "drop":
            names = self.names_of(args)
            if names:
                return b.add("project", at, [ref], columns={"drop": names}, primitive="drop_columns")
        if method in ("filter", "where") and len(args) == 1:
            text = args[0].value if isinstance(args[0], ast.Constant) and isinstance(args[0].value, str) else None
            if text is not None:
                tree = tx.parse_sql(f"SELECT 1 WHERE {text}", "spark")
                where = tree.args.get("where") if tree is not None else None
                return b.add("filter", at, [ref], predicate={"sql": text}, _rank_test=tx.rank_test(where.this) if where is not None else None)
            return b.add("filter", at, [ref], predicate=self.predicate(args[0]), _rank_test=self.rank_test(args[0]))
        if method in ("join", "crossJoin"):
            return self.join(ref, method, node, at)
        if method == "groupBy":
            keys = self.names_of(args)
            return _Group(ref, keys or [], at) if keys is not None else b.add("platform", at, [ref], construct="pyspark:groupBy")
        if method == "agg":  # df.agg(...) without groupBy: one row
            return self.aggregate(_Group(ref, [], at), "agg", node, at)
        if method in ("dropDuplicates", "drop_duplicates"):
            subset = self.names_of(args) if args else (self.names_of([_kw(node, "subset")]) if _kw(node, "subset") is not None else [])
            if subset is None:
                return b.add("deduplicate", at, [ref], candidates=["dedupe_by_key"])
            if not subset:
                return b.add("deduplicate", at, [ref], candidates=["dedupe_by_hash"])
            primitive = "dedupe_by_hash" if len(subset) == 1 and subset[0] in self.hash_columns else "dedupe_by_key"
            return b.add("deduplicate", at, [ref], keys=subset, primitive=primitive)
        if method == "distinct":
            return b.add("deduplicate", at, [ref], candidates=["dedupe_by_hash"])
        if method in ("orderBy", "sort"):
            order = self.order_of(args)
            return b.add("sort", at, [ref], order=order) if order is not None else b.add("platform", at, [ref], construct=f"pyspark:{method}")
        if method == "limit":
            item = b.get(ref)
            if item is not None and item["op"] == "sort" and b.uses(ref) == 0 and "output" not in item:
                item["op"], item["primitive"] = "filter", "top_n_filter"
                item["at"] = [min(item["at"][0], at[0]), max(item["at"][1], at[1])]
                return ref
            return b.add("filter", at, [ref])
        if method in ("union", "unionAll", "unionByName") and args:
            other = self.df(args[0]) if not isinstance(args[0], ast.Name) or args[0].id in self.env else "{" + args[0].id + "}"
            if not isinstance(other, str):
                return b.add("platform", at, [ref], construct=f"pyspark:{method}")
            return b.add("union", at, [ref, other], primitive="union_by_name" if method == "unionByName" else None)
        if method in ("unpivot", "melt") and args:
            ids = self.names_of(args[:1]) if args else None
            return b.add("unpivot", at, [ref], keys=ids or [], primitive="unpivot" if ids is not None else None, candidates=None if ids is not None else ["unpivot"])
        if method in ("fillna",) or (method == "fill" and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "na"):
            subset = _kw(node, "subset") or (args[1] if len(args) > 1 else None)
            names = self.names_of([subset]) if subset is not None else None
            if args and isinstance(args[0], ast.Dict):
                names = [self.strings.get(k)[0].lower() for k in args[0].keys if k is not None and self.strings.get(k)[0]]
            return b.add("project", at, [ref], columns={"replace": names} if names else None, primitive="apply_defaults")
        if method in ("dropna",) or (method == "drop" and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "na"):
            return b.add("filter", at, [ref])
        if method == "na":
            return ref
        return b.add("platform", at, [ref], construct=f"pyspark:{method}")

    def is_window(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "over"

    def delta_window(self, node: ast.AST) -> bool:
        return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and self.is_window(node.right)

    def over(self, ref: str, node: ast.AST, target: str | None, at: list[int]) -> str:
        """`fn(...).over(w)` (or `x - lag(x).over(w)`) written as `target`: a window transform."""
        b = self.b
        delta = self.delta_window(node)
        call = node.right if delta else node
        spec = self.window(call.args[0] if call.args else None) or {"keys": [], "order": [], "frame": None}
        inner = call.func.value
        name = self.fn(inner) or (inner.func.attr if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) else "window")
        column = self.column(inner.args[0]) if isinstance(inner, ast.Call) and inner.args else None
        measure = {"fn": name, **({"column": column} if column else {}), **({"as": target} if target else {})}
        fields: dict[str, Any] = {"keys": spec["keys"], "order": spec["order"], "measures": [measure]}
        hint = None
        if delta:
            if name == "lag" and column and self.column(node.left, strings=False) == column:
                fields["primitive"] = "delta_calc"
            else:
                fields["candidates"] = ["window_function"]
        elif name in ("sum", "count") and spec["order"] and spec["frame"] in (None, "running"):
            fields["primitive"] = "calculate_running_total"
        else:
            fields["primitive"] = "window_function"
            if name in _RANKS:
                hint = {"fn": name, "as": target}
        return b.add("window", at, [ref], _rank=hint, **fields)

    def measure(self, node: ast.AST) -> tuple[dict[str, Any] | None, bool]:
        """One aggregation expression as a measure, and whether it is a plain aggregate of a column."""
        target = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "alias" and node.args:
            target = (self.strings.get(node.args[0])[0] or "").lower() or None
            node = node.func.value
        fn = self.fn(node)
        if fn in _AGG_FUNCTIONS:
            arg = node.args[0] if node.args else None
            column = "*" if isinstance(arg, ast.Constant) and arg.value in ("*", 1) else self.column(arg) if arg is not None else None
            m = {"fn": fn, **({"column": column} if column else {}), **({"as": target} if target else {})}
            return m, column is not None or (fn == "count" and arg is None)
        return None, False

    def aggregate(self, group: _Group, method: str, node: ast.Call, at: list[int]) -> Any:
        b = self.b
        if method == "pivot":
            column = self.column(node.args[0]) if node.args else None
            return _Group(group.ref, group.keys, group.at, pivot=column or "?")
        measures: list[dict[str, Any]] = []
        exact = True
        if method == "agg":
            if node.args and isinstance(node.args[0], ast.Dict):
                for k, v in zip(node.args[0].keys, node.args[0].values):
                    col, fn = self.strings.get(k)[0] if k is not None else None, self.strings.get(v)[0]
                    if col and fn:
                        measures.append({"fn": fn.lower(), "column": col.lower()})
                    else:
                        exact = False
            else:
                for a in node.args:
                    m, plain = self.measure(a)
                    exact = exact and plain
                    if m is not None:
                        measures.append(m)
                exact = exact and bool(measures)
        elif method in ("count", "sum", "avg", "mean", "min", "max"):
            cols = self.names_of(node.args) or []
            measures = [{"fn": method}] if method == "count" else [{"fn": method, "column": c} for c in cols]
            exact = method == "count" or bool(cols)
        else:
            return b.add("platform", at, [group.ref], construct=f"pyspark:groupBy.{method}")
        span = [min(group.at[0], at[0]), max(group.at[1], at[1])]
        if group.pivot is not None:
            keys = [*group.keys, group.pivot] if group.pivot != "?" else group.keys
            return b.add("pivot", span, [group.ref], keys=keys, measures=measures, primitive="pivot" if exact and group.pivot != "?" else None, candidates=None if exact and group.pivot != "?" else ["pivot"])
        return b.add("aggregate", span, [group.ref], keys=group.keys, measures=measures, primitive="aggregate" if exact else None, candidates=None if exact else ["aggregate"])

    def join(self, ref: str, method: str, node: ast.Call, at: list[int]) -> str:
        b = self.b
        other_node = node.args[0] if node.args else _kw(node, "other")
        other = self.df(other_node) if not (isinstance(other_node, ast.Name) and other_node.id not in self.env) else "{" + other_node.id + "}"
        if not isinstance(other, str):
            return b.add("platform", at, [ref], construct=f"pyspark:{method}")
        if method == "crossJoin":
            return b.add("join", at, [ref, other], join={"type": "cross"}, primitive="join")
        on = node.args[1] if len(node.args) > 1 else _kw(node, "on")
        how_node = node.args[2] if len(node.args) > 2 else _kw(node, "how")
        how = (self.strings.get(how_node)[0] or "").lower() if how_node is not None else "inner"
        jtype = _JOIN_HOW.get(how.replace(" ", ""))
        if jtype is None:
            return b.add("join", at, [ref, other], join={"type": "inner"}, candidates=["join"])
        if on is None:
            return b.add("join", at, [ref, other], join={"type": "cross" if jtype == "inner" else jtype}, candidates=["join"])
        names = self.names_of([on])
        pairs = [[n, n] for n in names] if names else self.join_pairs(on)
        if pairs:
            return b.add("join", at, [ref, other], keys=list(dict.fromkeys(p[0] for p in pairs)), join={"type": jtype, "on": pairs}, primitive="join")
        return b.add("join", at, [ref, other], join={"type": jtype}, predicate=self.predicate(on), candidates=["join"])

    def join_pairs(self, on: ast.AST) -> list[list[str]] | None:
        """`a.k == b.k` (and `&` of several) as column pairs; None when the condition is anything else."""
        if isinstance(on, ast.BinOp) and isinstance(on.op, ast.BitAnd):
            left, right = self.join_pairs(on.left), self.join_pairs(on.right)
            return left + right if left is not None and right is not None else None
        if isinstance(on, ast.Compare) and len(on.ops) == 1 and isinstance(on.ops[0], ast.Eq):
            a, c = self.column(on.left, strings=False), self.column(on.comparators[0], strings=False)
            return [[a, c]] if a and c else None
        if isinstance(on, (ast.List, ast.Tuple)):
            out: list[list[str]] = []
            for item in on.elts:
                pair = self.join_pairs(item)
                if pair is None:
                    return None
                out += pair
            return out
        return None

    def delta(self, table: _Delta, node: ast.Call, at: list[int]) -> Any:
        """`DeltaTable...merge(source, cond).whenMatched...().execute()`, `.delete(cond)`, `.update(...)`."""
        b = self.b
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        calls = list(reversed(_calls_in_chain(node)))
        names = [c.func.attr for c in calls if isinstance(c.func, ast.Attribute)]
        if method == "execute" and "merge" in names:
            merge = next(c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "merge")
            source_node = merge.args[0] if merge.args else _kw(merge, "source")
            source = self.df(source_node)
            if not isinstance(source, str):
                source = "{" + source_node.id + "}" if isinstance(source_node, ast.Name) else b.add("platform", at, [], construct="pyspark:merge_source")
            cond = self.strings.get(merge.args[1] if len(merge.args) > 1 else _kw(merge, "condition"))[0]
            keys = []
            if cond:
                tree = self.tx.parse_sql(f"SELECT 1 WHERE {cond}", "spark")
                pairs = self.tx._equi_pairs(tree.args["where"].this, None, None) if tree is not None and tree.args.get("where") is not None else None
                keys = list(dict.fromkeys(p[0] for p in pairs or []))
            update = any(n.startswith("whenMatchedUpdate") for n in names)
            insert = any(n.startswith("whenNotMatchedInsert") for n in names)
            delete = any(n in ("whenMatchedDelete", "whenNotMatchedBySourceDelete") for n in names)
            fields: dict[str, Any] = {"keys": keys, "output": table.name}
            if update and insert:
                fields["primitive"] = "merge_into"
            elif delete and not update and not insert:
                fields["primitive"] = "apply_deletes"
            else:
                fields["candidates"] = ["merge_into"] + (["apply_deletes"] if delete else [])
            b.add("publish", at, [source], **fields)
            return None
        if method == "delete":
            cond = self.strings.get(_arg(node, 0, "condition"))[0]
            b.add("filter", at, [table.name], output=table.name, predicate={"sql": f"NOT ({cond})"} if cond else None, candidates=["apply_deletes"])
            return None
        if method in ("update", "updateExpr"):
            b.add("project", at, [table.name], output=table.name, candidates=["derive_column"])
            return None
        if method in ("toDF",):
            return table.name
        return _Delta(table.name) if method in ("merge", "alias") or method.startswith("when") else None


def _pyspark_functions(trees: list[ast.Module]) -> tuple[set[str], set[str]]:
    """Names that refer to pyspark.sql.functions: module aliases (`F`) and imported functions."""
    aliases, names = {"F"}, set()
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "pyspark.sql.functions" and a.asname:
                        aliases.add(a.asname)
            elif isinstance(n, ast.ImportFrom) and n.module == "pyspark.sql.functions":
                names |= {a.asname or a.name for a in n.names if a.name != "*"}
            elif isinstance(n, ast.ImportFrom) and n.module == "pyspark.sql":
                aliases |= {a.asname or a.name for a in n.names if a.name == "functions"}
    return aliases, names


def pyspark_shapes(ctx: Context, strings: _Strings, trees: list[tuple[Cell, ast.Module]], imports: set[str], owner: Any, notebook: bool) -> list:
    """`transform` observations for the PySpark code in `trees`: the top level (of every cell of
    a notebook, which share their variables) as one unit, and each function as one unit."""
    from repo_intel.compile.transforms import transform_observation

    if "pyspark" not in imports and "dlt" not in imports and "delta" not in imports and not notebook:
        return []
    functions, names = _pyspark_functions([t for _, t in trees])
    out = []

    def emit(shapes: _SparkShapes, at) -> None:
        transforms = shapes.b.finish()
        if transforms:
            out.append(transform_observation(ctx.blob, at, owner, "pyspark", transforms))

    top: _SparkShapes | None = None
    first = last = None
    for cell, tree in trees:
        scan = _Scan(ctx, strings, cell, imports)
        if top is None:
            top = _SparkShapes(scan, strings, functions, names)
        else:
            top.scan = scan  # spans are per cell; the variables carry over
        top.block(tree.body)
        first = cell.lines[0][0] if first is None else first
        last = cell.lines[-1][1]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = _SparkShapes(scan, strings, functions, names)
                fn.windows = dict(top.windows)  # module-level window specs are visible in functions
                target = None
                for dec in node.decorator_list:
                    chain, root = _attr_chain(dec)
                    if _root_name(root) == "dlt" and chain and chain[0] in ("table", "view", "materialized_view", "streaming_table"):
                        call = dec if isinstance(dec, ast.Call) else None
                        text, _ = strings.get(_kw(call, "name")) if call else (None, False)
                        target = dataset_name(text or node.name)
                fn.block(node.body, dlt_target=target)
                emit(fn, scan.at(node))
    if top is not None and first is not None:
        emit(top, span(ctx.data, first, max(first, last)))
    return out


def sql_cell_shapes(ctx: Context, cell: Cell, owner: Any) -> list:
    """A `transform` observation for one `%sql` cell, whose statements run in Spark SQL."""
    from repo_intel.compile.transforms import Builder, sql_query, transform_observation
    from repo_intel.extractors.contracts_data import split_statements

    def byte_of(offset: int) -> int:
        line = min(cell.text.count("\n", 0, offset), len(cell.lines) - 1)
        return cell.lines[line][0]

    b = Builder()
    for a, e in split_statements(cell.text):
        if cell.text[a:e].strip():
            line_end = min(cell.text.count("\n", 0, e), len(cell.lines) - 1)
            sql_query(b, cell.text[a:e], [byte_of(a), max(byte_of(a), cell.lines[line_end][1])], dialect="databricks")
    transforms = b.finish()
    if not transforms:
        return []
    return [transform_observation(ctx.blob, span(ctx.data, cell.lines[0][0], max(cell.lines[0][0], cell.lines[-1][1])), owner, "sql", transforms)]


# ------------------------------------------------------------------ extractor


class DataPython:
    id = "data.python"
    version = "1.1.0"

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
    try:  # transform shapes: a recognizer fault leaves the lineage above as it is
        out += pyspark_shapes(ctx, strings, trees, imports, subject, notebook_format is not None)
        if notebook_format:
            for cell in cells:
                if cell.kind == "sql":
                    out += sql_cell_shapes(ctx, cell, subject)
    except (ValueError, RecursionError) as exc:
        diagnostics.append(diagnostic(extractor_id, "warning", f"transform shapes not derived: {exc}"[:200]))
    if ctx.signals:
        out.append(obs("ml_signal", blob, whole_file(data), {"signals": sorted(ctx.signals)}, E2))
    if ctx.resolved or ctx.unresolved:
        out.append(obs("lineage_summary", blob, whole_file(data), {"resolved": ctx.resolved, "unresolved": ctx.unresolved}, E2))
    if not notebook_format and not out:
        return Extraction(parsed=False)
    return Extraction(out, diagnostics)


EXTRACTORS = [DataPython()]
