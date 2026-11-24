"""Monorepo build systems: Bazel, Nx and Turborepo.

Each build target is a BuildTarget keyed {tool, label}, and the targets it depends
on inside the repository are DEPENDS_ON edges between BuildTargets.

- Bazel: BUILD and BUILD.bazel files are Starlark, which parses as Python, so they
  are read with `ast` and never evaluated. Every rule call with a literal `name` is
  a target labelled `//package:name`; `deps` (and `runtime_deps`, `embed`, `data`, `exports`)
  given as literal labels are resolved against the package. Labels in other
  repositories (`@x//...`) and anything computed (`glob`, `select`, variables) are
  not edges: the external ones are kept as an attribute, the computed ones counted.
- Nx: project.json targets are labelled `project:target`. `dependsOn` naming a
  target of the same project is an edge; `^target` (the same target in every
  project this one depends on) is recorded as an attribute, since which projects
  those are is only known from the package graph.
- Turborepo: turbo.json `tasks` (or `pipeline` before 2.0) are labelled by task
  name, as they run in every workspace package; `pkg#task` keeps its package.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction, diagnostic, find_span, span, whole_file
from repo_intel.extractors.textscan import Doc

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1


def _target(d: Doc, at, tool: str, label: str, attrs: dict[str, Any]) -> dict:
    return d.entity(at, K.BUILD_TARGET, {"tool": tool, "label": label}, label, attrs, E1)


def _ref(tool: str, label: str) -> dict:
    from repo_intel.extractors.base import ref

    return ref(K.BUILD_TARGET, {"tool": tool, "label": label}, label)


# ---------------------------------------------------------------------- Bazel

_DEP_ATTRS = ("deps", "runtime_deps", "embed", "data", "exports", "srcs")
_LABEL = re.compile(r"^(@[\w.~+-]*)?(//[\w./+@=,~-]*)?(?::([\w./+@=,~-]+))?$")


def bazel_label(package: str, raw: str) -> str | None:
    """The canonical `//pkg:name` form of a label written in `package`, or None for
    a label in another repository or a file path that is not a label."""
    if re.fullmatch(r"[\w.+-]+", raw):  # a bare name: a target of this package
        return f"//{package}:{raw}"
    m = _LABEL.match(raw)
    if not m or raw.startswith("@") and not raw.startswith("@//"):
        return None
    pkg, name = m.group(2), m.group(3)
    if pkg is None:
        if name is None:  # a bare word: a target in this package in deps, a file in srcs
            return f"//{package}:{raw}" if not raw.startswith(("/", ".")) else None
        return f"//{package}:{name}"
    pkg = pkg[2:].rstrip("/")
    return f"//{pkg}:{name or posixpath.basename(pkg)}"


class Bazel:
    id = "build.bazel"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path) in ("BUILD", "BUILD.bazel")

    def extract(self, path, data, blob):
        try:
            tree = ast.parse(data.decode("utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable BUILD file: {type(exc).__name__}")], parsed=False)
        package = posixpath.dirname(path)
        d = Doc(path, data, blob, "")
        starts = [0]
        for line in data.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        at = lambda n: span(data, starts[n.lineno - 1] + n.col_offset, starts[(n.end_lineno or n.lineno) - 1] + (n.end_col_offset or n.col_offset))
        computed = 0
        for stmt in tree.body:
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not isinstance(call, ast.Call) or not isinstance(call.func, (ast.Name, ast.Attribute)):
                continue
            rule = call.func.id if isinstance(call.func, ast.Name) else call.func.attr
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            name = kw.get("name")
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)) or rule in ("package", "load", "exports_files", "licenses"):
                continue
            label = f"//{package}:{name.value}"
            deps, external = [], []
            for attr in _DEP_ATTRS:
                value = kw.get(attr)
                items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value] if value is not None else []
                for item in items:
                    if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                        computed += 1
                        continue
                    raw = item.value
                    if attr == "srcs" and not raw.startswith((":", "//")):
                        continue  # a source file, not a target
                    if raw.startswith("@") and not raw.startswith("@//"):
                        external.append(raw)
                        continue
                    target = bazel_label(package, raw)
                    if target and target != label:
                        deps.append((target, item))
            attrs: dict[str, Any] = {"rule": rule}
            visibility = kw.get("visibility")
            if isinstance(visibility, ast.List) and all(isinstance(v, ast.Constant) for v in visibility.elts):
                attrs["visibility"] = [v.value for v in visibility.elts]
            if external:
                attrs["external_deps"] = sorted(set(external))
            me = _target(d, at(name), "bazel", label, attrs)
            for target, node in deps:
                d.relation(at(node), me, P.DEPENDS_ON, _ref("bazel", target), {"basis": "bazel_deps"}, E1)
        notes = [diagnostic(self.id, "info", f"{computed} computed labels (glob, select or variables) not resolved")] if computed else []
        return Extraction(d.out, notes)


# ------------------------------------------------------------------------- Nx


class NxProject:
    id = "build.nx"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path) == "project.json"

    def extract(self, path, data, blob):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Extraction(parsed=False)
        if not isinstance(doc, dict) or not isinstance(doc.get("targets"), dict) and "nx" not in str(doc.get("$schema", "")):
            return Extraction(parsed=False)
        d = Doc(path, data, blob, data.decode("utf-8"))
        project = doc.get("name") if isinstance(doc.get("name"), str) else posixpath.basename(posixpath.dirname(path)) or "root"
        implicit = [x for x in doc.get("implicitDependencies") or [] if isinstance(x, str)]
        targets = doc.get("targets") if isinstance(doc.get("targets"), dict) else {}
        cursor = 0
        for name, body in targets.items():
            if not isinstance(body, dict):
                body = {}
            at = find_span(data, json.dumps(name), cursor) or whole_file(data)
            cursor = at.end_byte
            upstream, local = [], []
            for dep in body.get("dependsOn") or []:
                dep = dep.get("target") if isinstance(dep, dict) and not dep.get("projects") else dep
                if isinstance(dep, str):
                    (upstream if dep.startswith("^") else local).append(dep.lstrip("^"))
            attrs: dict[str, Any] = {"project": project}
            for key in ("executor", "command"):
                if isinstance(body.get(key), str):
                    attrs[key] = body[key]
            if isinstance(doc.get("projectType"), str):
                attrs["project_type"] = doc["projectType"]
            if upstream:
                attrs["depends_on_upstream"] = sorted(set(upstream))
            if implicit:
                attrs["implicit_dependencies"] = implicit
            me = _target(d, at, "nx", f"{project}:{name}", attrs)
            for dep in local:
                label = dep if ":" in dep else f"{project}:{dep}"
                d.relation(at, me, P.DEPENDS_ON, _ref("nx", label), {"basis": "nx_depends_on"}, E1)
        return Extraction(d.out)


# ----------------------------------------------------------------- Turborepo


class Turbo:
    id = "build.turbo"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path) == "turbo.json"

    def extract(self, path, data, blob):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable turbo.json: {type(exc).__name__}")], parsed=False)
        tasks = doc.get("tasks", doc.get("pipeline")) if isinstance(doc, dict) else None
        if not isinstance(tasks, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no tasks declared")])
        d = Doc(path, data, blob, data.decode("utf-8"))
        cursor = 0
        for name, body in tasks.items():
            body = body if isinstance(body, dict) else {}
            at = find_span(data, json.dumps(name), cursor) or whole_file(data)
            cursor = at.end_byte
            upstream, local = [], []
            for dep in body.get("dependsOn") or []:
                if isinstance(dep, str) and not dep.startswith("$"):
                    (upstream if dep.startswith("^") else local).append(dep.lstrip("^"))
            attrs: dict[str, Any] = {}
            if upstream:
                attrs["depends_on_upstream"] = sorted(set(upstream))
            if body.get("cache") is False:
                attrs["cache"] = False
            if body.get("persistent") is True:
                attrs["persistent"] = True
            outputs = [o for o in body.get("outputs") or [] if isinstance(o, str)]
            if outputs:
                attrs["outputs"] = outputs
            me = _target(d, at, "turbo", name, attrs)
            for dep in local:
                d.relation(at, me, P.DEPENDS_ON, _ref("turbo", dep), {"basis": "turbo_depends_on"}, E1)
        return Extraction(d.out)


EXTRACTORS = [Bazel(), NxProject(), Turbo()]
