"""Declared batch work: Databricks bundles, Azure ML jobs, MLflow projects, DVC and dbt (spec §8.1–8.2).

Each of these is declarative configuration, so what it yields is E1:

- Databricks Asset Bundles (`databricks.yml` and the resource files it includes):
  jobs and their tasks (`depends_on` gives DEPENDS_ON), what each task runs
  (notebook, Python file, SQL file, pipeline or another job), schedules and
  triggers as attributes, Delta Live Tables pipelines and their notebooks, model
  serving endpoints (a DeploymentUnit that SERVES an MLModel), registered models
  and deployment targets (Environments). Paths are relative to the file that
  declares them; a notebook path without an extension is matched to whichever
  notebook exists.
- Azure ML job YAML (`$schema` on azureml): command, sweep and pipeline jobs, the
  script a command runs, and inputs and outputs named by a literal path.
- MLflow `MLproject`: the project is a Job and each entry point a Task.
- DVC: `dvc.yaml` stages are Tasks with their dependencies and outputs, and a stage
  that consumes another's output depends on it; a `.dvc` file tracks a Dataset.
- dbt: each model under `models/` is a Dataset DERIVED_FROM what it `ref`s and
  `source`s and the tables its SQL names; `schema.yml` sources are Datasets and its
  tests are Tests of the model they check.

Interpolated values (`${…}`, `${{…}}`) are never resolved or copied, except for the
`${resources.<kind>.<key>.id}` references a bundle uses to point at its own jobs
and pipelines.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import PACKAGE, Extraction, diagnostic, entity, ref, relation, span, whole_file
from repo_intel.extractors.data_python import NOTEBOOK_EXTS, dataset_name
from repo_intel.extractors.infrastructure import YAML_ERRORS, YAML_SUFFIXES, search_span, yaml_key_span
from repo_intel.extractors.sql_lineage import lineage, lineage_summary

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1
E2 = EvidenceClass.E2

_RESOURCE_REF = re.compile(r"^\$\{resources\.(jobs|pipelines)\.([\w-]+)\.id\}$")
_PYTHON_SCRIPT = re.compile(r"\bpython[\d.]*\s+(?:-u\s+)?(?:([\w./-]+\.py)\b|-m\s+([A-Za-z_][\w.]*))")


def _scripts(command: str) -> list[str]:
    """The Python files a shell command runs: `python x.py`, or `python -m a.b`
    as a/b.py or a/b/__main__.py (whichever exists survives linking)."""
    out = []
    for script, module in _PYTHON_SCRIPT.findall(command):
        if script:
            out.append(script)
        else:
            base = module.replace(".", "/")
            out += [f"{base}.py", f"{base}/__main__.py"]
    return out


def _rel(base: str, target: str) -> str | None:
    """`target` relative to the directory `base`, as a repository path; None if it leaves the repository."""
    if not isinstance(target, str) or not target or "${" in target or "://" in target or target.startswith(("/Workspace", "/Shared", "/Users", "/Volumes", "/dbfs")):
        return None
    path = posixpath.normpath(posixpath.join(base, target)) if not target.startswith("/") else target.lstrip("/")
    return None if path.startswith("..") or path == "." else path


def _notebook_refs(blob, at, subject, path: str) -> list:
    stem, ext = posixpath.splitext(path)
    candidates = [path] if ext in NOTEBOOK_EXTS else [path + e for e in NOTEBOOK_EXTS]
    return [relation(blob, at, subject, P.RUNS, ref(K.NOTEBOOK, {"path": c}, posixpath.basename(c), declare=False), {}, E1) for c in candidates]


def _artifact(blob, at, subject, path: str) -> list:
    return [relation(blob, at, subject, P.RUNS, ref(K.ARTIFACT, {"path": path}, posixpath.basename(path), declare=False), {}, E1)]


def _literal(value: Any) -> str | None:
    return value if isinstance(value, str) and value and "${" not in value else None


class _Doc:
    """Helpers bound to one file."""

    def __init__(self, path: str, data: bytes, blob: str):
        self.path, self.data, self.blob = path, data, blob
        self.dir = posixpath.dirname(path)
        self.out: list = []

    def at(self, key: str, value: Any = None, start: int = 0):
        return yaml_key_span(self.data, key, value if isinstance(value, str) else None, start) or yaml_key_span(self.data, key, None, start) or whole_file(self.data)

    def entity(self, at, kind, key, name, attrs=None, ev=E1):
        self.out.append(entity(self.blob, at, kind, key, name, attrs or {}, ev))
        return ref(kind, key, name)

    def relation(self, at, subject, predicate, obj, qualifiers=None, ev=E1):
        self.out.append(relation(self.blob, at, subject, predicate, obj, qualifiers or {}, ev))

    def dataset(self, at, subject, predicate, raw: str | None, attrs=None):
        name = dataset_name(raw) if raw else None
        if name:
            self.relation(at, subject, predicate, self.entity(at, K.DATASET, {"name": name}, name, attrs or {}))


# ------------------------------------------------------------- Databricks


def _bundle(doc: dict, d: _Doc) -> None:
    resources = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}
    environments = []
    targets = doc.get("targets") if isinstance(doc.get("targets"), dict) else {}
    for target, body in sorted(targets.items()):
        if isinstance(target, str):
            attrs = {"platform": "databricks"}
            if isinstance(body, dict) and isinstance(body.get("mode"), str):
                attrs["mode"] = body["mode"]
            environments.append(d.entity(d.at(target), K.ENVIRONMENT, {"name": target}, target, attrs))
    jobs = resources.get("jobs") if isinstance(resources.get("jobs"), dict) else {}
    for key, job in sorted(jobs.items()):
        if not isinstance(key, str) or not isinstance(job, dict):
            continue
        job_at = d.at(key)
        attrs: dict[str, Any] = {"kind": "job"}
        if _literal(job.get("name")):
            attrs["display_name"] = job["name"]
        schedule = job.get("schedule")
        if isinstance(schedule, dict) and isinstance(schedule.get("quartz_cron_expression"), str):
            attrs["schedule"] = schedule["quartz_cron_expression"]
        trigger = job.get("trigger")
        if isinstance(trigger, dict):
            kinds = sorted(k for k in trigger if k in ("file_arrival", "periodic", "table_update", "table"))
            if kinds:
                attrs["trigger"] = kinds[0]
        if job.get("continuous"):
            attrs["trigger"] = "continuous"
        me = d.entity(job_at, K.JOB, {"framework": "databricks", "name": key}, key, attrs)
        for env in environments:
            d.relation(job_at, me, P.DEPLOYED_TO, env)
        if isinstance(trigger, dict):
            for section in ("table_update", "table"):
                names = trigger.get(section, {}).get("table_names") if isinstance(trigger.get(section), dict) else None
                for name in names if isinstance(names, list) else []:
                    d.dataset(job_at, me, P.READS_FROM, _literal(name))
        tasks = job.get("tasks") if isinstance(job.get("tasks"), list) else []
        cursor = job_at.end_byte
        for task in tasks:
            _task(task, key, me, d, cursor)
    pipelines = resources.get("pipelines") if isinstance(resources.get("pipelines"), dict) else {}
    for key, pipe in sorted(pipelines.items()):
        if not isinstance(key, str) or not isinstance(pipe, dict):
            continue
        at = d.at(key)
        attrs = {"kind": "pipeline"}
        if pipe.get("continuous"):
            attrs["trigger"] = "continuous"
        if pipe.get("serverless"):
            attrs["serverless"] = True
        me = d.entity(at, K.JOB, {"framework": "databricks", "name": key}, key, attrs)
        for env in environments:
            d.relation(at, me, P.DEPLOYED_TO, env)
        for lib in pipe.get("libraries") if isinstance(pipe.get("libraries"), list) else []:
            if not isinstance(lib, dict):
                continue
            for kind in ("notebook", "file"):
                path = _rel(d.dir, lib.get(kind, {}).get("path")) if isinstance(lib.get(kind), dict) else None
                if path:
                    lib_at = d.at("path", lib[kind]["path"], at.start_byte)
                    d.out.extend(_notebook_refs(d.blob, lib_at, me, path) if kind == "notebook" else _artifact(d.blob, lib_at, me, path))
    endpoints = resources.get("model_serving_endpoints") if isinstance(resources.get("model_serving_endpoints"), dict) else {}
    for key, endpoint in sorted(endpoints.items()):
        if not isinstance(key, str) or not isinstance(endpoint, dict):
            continue
        at = d.at(key)
        unit = d.entity(at, K.DEPLOYMENT_UNIT, {"kind": "databricks.ServingEndpoint", "namespace": "databricks", "name": key}, key, {})
        for env in environments:
            d.relation(at, unit, P.DEPLOYED_TO, env)
        config = endpoint.get("config") if isinstance(endpoint.get("config"), dict) else {}
        served = (config.get("served_entities") or []) + (config.get("served_models") or [])
        for entry in served if isinstance(served, list) else []:
            name = _literal(entry.get("entity_name") or entry.get("model_name")) if isinstance(entry, dict) else None
            if name:
                model_at = d.at("entity_name" if "entity_name" in entry else "model_name", name, at.start_byte)
                d.relation(model_at, unit, P.SERVES, d.entity(model_at, K.ML_MODEL, {"name": name.lower()}, name.lower(), {"registry": "databricks"}))
    models = resources.get("registered_models") if isinstance(resources.get("registered_models"), dict) else {}
    for key, model in sorted(models.items()):
        if not isinstance(key, str) or not isinstance(model, dict):
            continue
        parts = [_literal(model.get(k)) for k in ("catalog_name", "schema_name", "name")]
        name = ".".join(parts) if all(parts) else _literal(model.get("name")) or key
        d.entity(d.at(key), K.ML_MODEL, {"name": name.lower()}, name.lower(), {"registry": "databricks"})


def _task(task: Any, job_key: str, job, d: _Doc, cursor: int) -> None:
    if not isinstance(task, dict) or not isinstance(task.get("task_key"), str):
        return
    name = task["task_key"]
    at = d.at("task_key", name, cursor)
    kind = next((k for k in task if k.endswith("_task")), "task")
    body = task.get(kind) if isinstance(task.get(kind), dict) else {}
    attrs = {"type": kind, **({"package": body["package_name"]} if kind == "python_wheel_task" and _literal(body.get("package_name")) else {})}
    me = d.entity(at, K.TASK, {"framework": "databricks", "job": job_key, "name": name}, name, attrs)
    d.relation(at, job, P.CONTAINS, me)
    for dep in task.get("depends_on") if isinstance(task.get("depends_on"), list) else []:
        upstream = dep.get("task_key") if isinstance(dep, dict) else None
        if isinstance(upstream, str):
            d.relation(d.at("task_key", upstream, at.start_byte), me, P.DEPENDS_ON, ref(K.TASK, {"framework": "databricks", "job": job_key, "name": upstream}, upstream))
    if kind == "notebook_task":
        path = _rel(d.dir, body.get("notebook_path"))
        if path:
            d.out.extend(_notebook_refs(d.blob, d.at("notebook_path", body["notebook_path"], at.start_byte), me, path))
    elif kind == "spark_python_task":
        path = _rel(d.dir, body.get("python_file"))
        if path:
            d.out.extend(_artifact(d.blob, d.at("python_file", body["python_file"], at.start_byte), me, path))
    elif kind == "sql_task" and isinstance(body.get("file"), dict):
        path = _rel(d.dir, body["file"].get("path"))
        if path:
            d.out.extend(_artifact(d.blob, d.at("path", body["file"]["path"], at.start_byte), me, path))
    elif kind in ("pipeline_task", "run_job_task"):
        raw = body.get("pipeline_id") or body.get("job_id")
        m = _RESOURCE_REF.match(raw) if isinstance(raw, str) else None
        if m:
            d.relation(d.at("pipeline_id" if kind == "pipeline_task" else "job_id", None, at.start_byte), me, P.RUNS, ref(K.JOB, {"framework": "databricks", "name": m.group(2)}, m.group(2), declare=False))
    elif kind == "for_each_task" and isinstance(body.get("task"), dict):
        _task({**body["task"], "task_key": f"{name}.{body['task'].get('task_key', 'iteration')}"}, job_key, job, d, at.start_byte)


# ---------------------------------------------------------------- Azure ML


def _azureml(doc: dict, d: _Doc) -> None:
    name = _literal(doc.get("name")) or _literal(doc.get("display_name")) or _literal(doc.get("experiment_name")) or posixpath.splitext(posixpath.basename(d.path))[0]
    at = d.at("name", name) if doc.get("name") else d.at("type") if doc.get("type") else whole_file(d.data)
    kind = doc.get("type") if isinstance(doc.get("type"), str) else "command"
    job = d.entity(at, K.JOB, {"framework": "azureml", "name": name}, name, {"kind": kind})

    def step(subject, body: dict, start: int) -> None:
        code = body.get("code") if isinstance(body.get("code"), str) else "."
        command = body.get("command")
        if isinstance(command, str):
            for script in _scripts(command):
                path = _rel(posixpath.join(d.dir, code) if not code.startswith("azureml:") else d.dir, script)
                if path:
                    d.out.extend(_artifact(d.blob, d.at("command", None, start), subject, path))
        for section, predicate in (("inputs", P.READS_FROM), ("outputs", P.WRITES_TO)):
            block = body.get(section) if isinstance(body.get(section), dict) else {}
            for io_name, io in sorted(block.items()):
                raw = io.get("path") if isinstance(io, dict) else io if isinstance(io, str) else None
                if not _literal(raw) or "{{" in raw:
                    continue
                if raw.startswith("azureml:"):
                    raw = "azureml:" + raw.split(":")[1]  # drop the version
                d.dataset(d.at(io_name, None, start), subject, predicate, raw)

    if kind == "pipeline" and isinstance(doc.get("jobs"), dict):
        for step_name, body in sorted(doc["jobs"].items()):
            if not isinstance(step_name, str) or not isinstance(body, dict):
                continue
            step_at = d.at(step_name)
            me = d.entity(step_at, K.TASK, {"framework": "azureml", "job": name, "name": step_name}, step_name, {"type": body.get("type") if isinstance(body.get("type"), str) else "command"})
            d.relation(step_at, job, P.CONTAINS, me)
            step(me, body, step_at.start_byte)
            text = yaml.safe_dump(body)
            for upstream in sorted(set(re.findall(r"parent\.jobs\.([\w-]+)\.outputs", text))):
                if upstream != step_name:
                    d.relation(step_at, me, P.DEPENDS_ON, ref(K.TASK, {"framework": "azureml", "job": name, "name": upstream}, upstream))
    else:
        step(job, doc, 0)


# ----------------------------------------------------------- MLflow and DVC


def _mlproject(doc: dict, d: _Doc) -> None:
    name = _literal(doc.get("name")) or posixpath.basename(d.dir) or "mlproject"
    job = d.entity(d.at("name", name) if doc.get("name") else whole_file(d.data), K.JOB, {"framework": "mlflow", "name": name}, name, {"kind": "project"})
    entry_points = doc.get("entry_points") if isinstance(doc.get("entry_points"), dict) else {}
    for ep, body in sorted(entry_points.items()):
        if not isinstance(ep, str):
            continue
        at = d.at(ep)
        me = d.entity(at, K.TASK, {"framework": "mlflow", "job": name, "name": ep}, ep, {"type": "entry_point"})
        d.relation(at, job, P.CONTAINS, me)
        command = body.get("command") if isinstance(body, dict) else None
        for script in _scripts(command) if isinstance(command, str) else []:
            path = _rel(d.dir, script)
            if path:
                d.out.extend(_artifact(d.blob, d.at("command", None, at.start_byte), me, path))


def _paths(items: Any) -> list[str]:
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out += [k for k in item if isinstance(k, str)]
    return out


def _dvc(doc: dict, d: _Doc) -> None:
    stages = doc.get("stages") if isinstance(doc.get("stages"), dict) else {}
    name = posixpath.basename(d.dir) or "dvc"
    job = d.entity(d.at("stages"), K.JOB, {"framework": "dvc", "name": name}, name, {"kind": "pipeline"})
    produced: dict[str, str] = {}
    for stage, body in stages.items():
        if isinstance(stage, str) and isinstance(body, dict):
            wdir = posixpath.join(d.dir, body["wdir"]) if isinstance(body.get("wdir"), str) else d.dir
            for out in _paths(body.get("outs")) + _paths(body.get("metrics")):
                path = _rel(wdir, out)
                if path:
                    produced[path] = stage
    for stage, body in sorted(stages.items()):
        if not isinstance(stage, str) or not isinstance(body, dict) or "foreach" in body:
            continue
        at = d.at(stage)
        me = d.entity(at, K.TASK, {"framework": "dvc", "job": name, "name": stage}, stage, {"type": "stage"})
        d.relation(at, job, P.CONTAINS, me)
        wdir = posixpath.join(d.dir, body["wdir"]) if isinstance(body.get("wdir"), str) else d.dir
        scripts = set()
        for cmd in body.get("cmd") if isinstance(body.get("cmd"), list) else [body.get("cmd")]:
            for script in _scripts(cmd) if isinstance(cmd, str) else []:
                path = _rel(wdir, script)
                if path:
                    scripts.add(path)
        for dep in _paths(body.get("deps")):
            path = _rel(wdir, dep)
            if not path:
                continue
            dep_at = d.at("deps", None, at.start_byte)
            if path.endswith(".py"):
                scripts.add(path)
                continue
            upstream = next((s for p, s in produced.items() if s != stage and (path == p or path.startswith(p + "/"))), None)
            if upstream:
                d.relation(dep_at, me, P.DEPENDS_ON, ref(K.TASK, {"framework": "dvc", "job": name, "name": upstream}, upstream))
            d.dataset(dep_at, me, P.READS_FROM, path, {"tracked_by": "dvc"})
        for script in sorted(scripts):
            d.out.extend(_artifact(d.blob, d.at("cmd", None, at.start_byte), me, script))
        for out in _paths(body.get("outs")):
            path = _rel(wdir, out)
            if path:
                d.dataset(d.at("outs", None, at.start_byte), me, P.WRITES_TO, path, {"tracked_by": "dvc"})


def _dvc_file(doc: dict, d: _Doc) -> None:
    for out in doc.get("outs") if isinstance(doc.get("outs"), list) else []:
        path = _rel(d.dir, out.get("path")) if isinstance(out, dict) else None
        name = dataset_name(path) if path else None
        if name:
            d.entity(d.at("path", out["path"]), K.DATASET, {"name": name}, name, {"tracked_by": "dvc"})


# ---------------------------------------------------------------------- dbt


_DBT_CALL = re.compile(r"\{\{\s*(ref|source)\s*\(\s*(['\"])([^'\"]+)\2\s*(?:,\s*(['\"])([^'\"]+)\4\s*)?(?:,\s*v(?:ersion)?\s*=\s*[^)]*)?\)\s*\}\}")
_DBT_CONFIG = re.compile(r"\{\{\s*config\s*\((.*?)\)\s*\}\}", re.S)
_DBT_TESTS = ("tests", "data_tests")


def dbt_model_path(path: str) -> bool:
    return path.lower().endswith(".sql") and bool(re.search(r"(?:^|/)(models|snapshots)/", path))


def _dbt_model(path: str, data: bytes, blob: str) -> Extraction:
    text = data.decode("utf-8")
    name = posixpath.splitext(posixpath.basename(path))[0]
    d = _Doc(path, data, blob)
    attrs: dict[str, Any] = {"framework": "dbt", "kind": "snapshot" if "/snapshots/" in f"/{path}" else "model"}
    config = _DBT_CONFIG.search(text)
    if config and (m := re.search(r"materialized\s*=\s*['\"](\w+)['\"]", config.group(1))):
        attrs["materialized"] = m.group(1)
    me = d.entity(whole_file(data), K.DATASET, {"name": name.lower()}, name.lower(), attrs, E2)
    d.relation(whole_file(data), PACKAGE, P.WRITES_TO, me, {}, E2)
    # Replace each ref()/source() with an identifier of the same length, so the
    # SQL still parses and the tables it names outright can be told apart.
    masked = list(text)
    upstream: list[tuple[str, int, int]] = []
    for m in _DBT_CALL.finditer(text):
        target = m.group(3) if m.group(1) == "ref" and not m.group(5) else f"{m.group(3)}.{m.group(5)}" if m.group(1) == "source" and m.group(5) else m.group(5) or m.group(3)
        upstream.append((target.lower(), m.start(), m.end()))
        filler = "__dbt__".ljust(m.end() - m.start(), "_")
        masked[m.start():m.end()] = filler
    sql = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", lambda m: re.sub(r"[^\n]", " ", m.group()), "".join(masked), flags=re.S)
    to_byte = (lambda i: i) if len(text) == len(data) else (lambda i: len(text[:i].encode("utf-8")))
    seen = set()
    for target, a, b in upstream:
        if target in seen:
            continue
        seen.add(target)
        at = span(data, to_byte(a), to_byte(b))
        src = ref(K.DATASET, {"name": target}, target, declare="." in target)  # a source declares; a ref names a model
        if "." in target:
            d.entity(at, K.DATASET, {"name": target}, target, {"framework": "dbt", "kind": "source"}, E2)
        d.relation(at, me, P.DERIVED_FROM, src, {}, E2)
    found = lineage(sql, None)
    for table, a, b in found.reads:
        if table.startswith("__dbt__") or table in seen:
            continue
        at = span(data, to_byte(a), to_byte(b))
        d.relation(at, me, P.DERIVED_FROM, d.entity(at, K.DATASET, {"name": table}, table, {}, E2), {}, E2)
    d.out += lineage_summary(blob, data, len(seen) + len(found.reads), found.unresolved)
    return Extraction(d.out)


def _dbt_schema(doc: dict, d: _Doc) -> None:
    for source in doc.get("sources") if isinstance(doc.get("sources"), list) else []:
        if not isinstance(source, dict) or not isinstance(source.get("name"), str):
            continue
        src_at = d.at("name", source["name"])
        for table in source.get("tables") if isinstance(source.get("tables"), list) else []:
            if isinstance(table, dict) and isinstance(table.get("name"), str):
                name = f"{source['name']}.{table['name']}".lower()
                at = d.at("name", table["name"], src_at.start_byte)
                me = d.entity(at, K.DATASET, {"name": name}, name, {"framework": "dbt", "kind": "source"})
                # The warehouse table it stands for: [database.]schema.identifier,
                # where schema defaults to the source name and identifier to the table name.
                parts = [_literal(source.get("database")), _literal(source.get("schema")) or source["name"], _literal(table.get("identifier")) or table["name"]]
                physical = ".".join(p for p in parts if p).lower()
                if physical != name and "{" not in physical:
                    d.relation(at, me, P.ALIAS_OF, d.entity(at, K.DATASET, {"name": physical}, physical, {}))
    for model in doc.get("models") if isinstance(doc.get("models"), list) else []:
        if not isinstance(model, dict) or not isinstance(model.get("name"), str):
            continue
        model_name = model["name"].lower()
        at = d.at("name", model["name"])
        target = ref(K.DATASET, {"name": model_name}, model_name, declare=False)
        checks: list[tuple[str, str | None]] = []
        for key in _DBT_TESTS:
            checks += [(t, None) for t in model.get(key) or [] if isinstance(model.get(key), list)]
        for column in model.get("columns") if isinstance(model.get("columns"), list) else []:
            if isinstance(column, dict) and isinstance(column.get("name"), str):
                for key in _DBT_TESTS:
                    checks += [(t, column["name"]) for t in column.get(key) or [] if isinstance(column.get(key), list)]
        for test, column in checks:
            kind = test if isinstance(test, str) else next(iter(test), None) if isinstance(test, dict) and test else None
            if not isinstance(kind, str):
                continue
            kind = kind.split(".")[-1]
            test_name = "_".join(p for p in (kind, model_name, column) if p)
            test_at = search_span(d.data, rf"\b{re.escape(kind)}\b", at.start_byte) or at
            me = d.entity(test_at, K.TEST, {"path": d.path, "name": test_name}, test_name, {"framework": "dbt", **({"column": column} if column else {})})
            d.relation(test_at, target, P.TESTED_BY, me)


# ---------------------------------------------------------------- extractor


class DataJobs:
    id = "data.jobs"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        base = posixpath.basename(path)
        return path.lower().endswith(YAML_SUFFIXES) or base == "MLproject" or base.endswith(".dvc") or dbt_model_path(path)

    def extract(self, path, data, blob):
        base = posixpath.basename(path)
        if dbt_model_path(path):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
            return _dbt_model(path, data, blob) if "{{" in text or "/models/" in f"/{path}" else Extraction(parsed=False)
        markers = (b"resources:", b"targets:", b"azureml", b"entry_points:", b"stages:", b"outs:", b"models:", b"sources:")
        if base not in ("MLproject", "dvc.yaml") and not any(m in data for m in markers):
            return Extraction(parsed=False)
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except YAML_ERRORS as exc:
            if base in ("databricks.yml", "databricks.yaml", "dvc.yaml", "MLproject"):
                return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
            return Extraction(parsed=False)
        if not isinstance(doc, dict):
            return Extraction(parsed=False)
        d = _Doc(path, data, blob)
        schema = doc.get("$schema") if isinstance(doc.get("$schema"), str) else ""
        if base in ("databricks.yml", "databricks.yaml") or (isinstance(doc.get("resources"), dict) and any(k in doc["resources"] for k in ("jobs", "pipelines", "model_serving_endpoints", "registered_models"))):
            _bundle(doc, d)
        elif "azureml" in schema:
            _azureml(doc, d)
        elif base == "MLproject":
            _mlproject(doc, d)
        elif base == "dvc.yaml":
            _dvc(doc, d)
        elif base.endswith(".dvc"):
            _dvc_file(doc, d)
        elif ("models" in doc or "sources" in doc) and (doc.get("version") == 2 or re.search(r"(?:^|/)models/", path)):
            _dbt_schema(doc, d)
        return Extraction(d.out)


EXTRACTORS = [DataJobs()]
