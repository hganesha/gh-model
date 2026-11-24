"""`unit.link`: cross-file resolution inside one compilation unit (spec §5, Stage 2).

Works only from FileSummary observations. It installs nothing and runs no
language server, so resolution is graded and never claimed to be sound:

  exact      the reference resolves to one definition by the module rules below
  candidate  plausible by a heuristic; kept, marked, and never used as proof
  unresolved counted in coverage and reported, never guessed

Output:
  observations  entity/relation observations (same shape extractors use, with a
                list of evidence rows), compiled by `unit.compile`
  unknowns      questions asked and not answerable (e.g. dynamic SQL)
  coverage      import_resolution, route_to_handler, data_access for this unit
  exports       what repo-level linking needs: contract operations and HTTP calls
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections.abc import Mapping
from typing import Any

from .buildgraph import Step, StepInputs
from .contracts import CoverageEntry
from .extractors.base import ref
from .extractors.manifests import normalize_pypi
from .ontology import EntityKind, EvidenceClass, Predicate
from .planning import pick_config, probe

ROOT_UNIT = "@root"
PY_IMPORT_TO_DIST = {"yaml": "pyyaml", "PIL": "pillow", "sklearn": "scikit-learn", "cv2": "opencv-python", "dateutil": "python-dateutil", "dotenv": "python-dotenv", "jwt": "pyjwt", "bs4": "beautifulsoup4"}
NODE_BUILTINS = {"assert", "buffer", "child_process", "cluster", "crypto", "dns", "events", "fs", "http", "http2", "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib"}
AI_SDKS = {"anthropic": "anthropic", "openai": "openai", "google.generativeai": "google", "google.genai": "google", "mistralai": "mistral", "cohere": "cohere",
           "@anthropic-ai/sdk": "anthropic", "@google/generative-ai": "google", "@mistralai/mistralai": "mistral", "cohere-ai": "cohere"}
TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TEST_PY = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")


def _of(summary: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [o for o in summary["observations"] if o["kind"] == kind]


def _entities(summary: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [o for o in summary["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == kind]


def _strip_ts(path: str) -> str:
    for ext in (".d.ts",) + TS_EXT:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _local_ts_configs(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tsconfig aliases from this unit's own files, for a unit planned without them."""
    out = []
    for s in summaries:
        for o in _of(s, "ts_config"):
            p = o["payload"]
            if p.get("paths"):
                root = posixpath.normpath(posixpath.join(p["dir"], p["base_url"])) if p.get("base_url") is not None else p["dir"]
                out.append({"config": s["path"], "dir": p["dir"], "root": "" if root == "." else root, "paths": p["paths"], "evidence": [o["evidence"]]})
    return sorted(out, key=lambda c: (c["dir"], c["config"]))


def _aliases(payload: Mapping[str, Any]) -> dict[str, str]:
    """original imported name -> local name, for `import x as y` forms."""
    raw = payload.get("aliases") or {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v}
    return {}


class _Linker:
    def __init__(self, unit: Mapping[str, Any], summaries: list[dict[str, Any]], workspace: list[dict[str, Any]]):
        self.unit_row = unit
        self.unit = unit["name"]
        self.root = "" if self.unit in (".", ROOT_UNIT) else self.unit
        self.files = {s["path"]: s for s in summaries}
        own = sorted((o["payload"] for s in summaries for o in _of(s, "package_declaration") if (o["payload"]["manifest_dir"] or ".") == self.unit), key=lambda p: (p["ecosystem"], p["name"]))
        self.package = own[0]["name"] if own else self.unit
        self.has_package = bool(own)
        self.package_ref = ref(EntityKind.PACKAGE, {k: own[0][k] for k in ("ecosystem", "name", "manifest_dir")}, own[0]["name"]) if own else None
        self.declared = {(o["payload"]["ecosystem"], o["payload"]["name"]) for s in summaries for o in _of(s, "dependency_declaration")}
        self.workspace = {(p["ecosystem"], p["name"]) for p in workspace}
        self.out: list[dict[str, Any]] = []
        self.unknowns: list[dict[str, Any]] = []
        self.symbols: dict[str, dict[str, dict[str, Any]]] = {p: {o["payload"]["name"]: o for o in _of(s, "symbol")} for p, s in self.files.items()}
        self.bindings: dict[str, dict[str, tuple[str, str]]] = {p: {} for p in self.files}  # local name -> (file, original name)
        self.ts_configs = unit.get("ts_configs") or _local_ts_configs(summaries)
        self.packages = {(p["ecosystem"], p["name"]): p for p in workspace}
        self.external_bindings: dict[str, dict[str, tuple[str, str, str]]] = {p: {} for p in self.files}  # local name -> (package, file, name)
        self.workspace_bound: list[str] = []
        self.imports = {"internal": 0, "workspace": 0, "external": 0, "stdlib": 0, "unresolved": 0}
        self.unresolved_imports: list[str] = []

    # -- helpers

    def rel(self, path: str) -> str:
        return path[len(self.root) + 1:] if self.root and path.startswith(self.root + "/") else path

    def qualified(self, path: str, name: str) -> str:
        rel = self.rel(path)
        if rel.endswith(".py"):
            module = rel[:-3].removesuffix("/__init__").removesuffix("__init__").replace("/", ".")
            return f"{module}.{name}" if module else name
        return f"{posixpath.splitext(rel)[0]}#{name}"

    def symbol_ref(self, path: str, name: str) -> dict[str, Any]:
        return ref(EntityKind.SYMBOL, {"package": self.package, "qualified_name": self.qualified(path, name)}, name)

    def emit(self, kind: str, path: str, payload: dict[str, Any], evidence: list[dict[str, Any]], evidence_class: EvidenceClass = EvidenceClass.E3, resolution: str = "exact") -> None:
        self.out.append({"kind": kind, "path": path, "payload": payload, "evidence": evidence, "evidence_class": str(evidence_class), "resolution": resolution})

    def relate(self, path: str, subject: Any, predicate: Predicate, obj: Any, evidence: list[dict[str, Any]], qualifiers: dict[str, Any] | None = None, **kw: Any) -> None:
        self.emit("relation", path, {"subject": subject, "predicate": str(predicate), "object": obj, "qualifiers": qualifiers or {}}, evidence, **kw)

    # -- symbols

    def declare_symbols(self) -> None:
        for path in sorted(self.files):
            for name, o in sorted(self.symbols[path].items()):
                r = self.symbol_ref(path, name)
                self.emit("entity", path, {**r, "attributes": {"symbol_kind": o["payload"].get("symbol_kind"), "path": path}}, [o["evidence"]], EvidenceClass.E2)

    # -- imports

    def _py_target(self, path: str, module: str, level: int) -> str | None:
        if level:
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            stems = [posixpath.join(base, *module.split(".")) if module else base]
        else:
            parts = module.split(".")
            stems = [posixpath.join(self.root, *parts), posixpath.join(self.root, "src", *parts)]
        for stem in stems:
            for candidate in (stem + ".py", posixpath.join(stem, "__init__.py")):
                if candidate in self.files:
                    return candidate
        return None

    def _py_import(self, path: str, o: dict[str, Any]) -> None:
        p = o["payload"]
        module, level, names = p.get("module") or "", int(p.get("level") or 0), list(p.get("names") or [])
        aliases = _aliases(p)
        target = self._py_target(path, module, level)
        if target is None and level and not module:  # `from . import x`: each name may itself be a module
            hits = {n: self._py_target(path, n, level) for n in names}
            if names and all(hits.values()):
                self.imports["internal"] += 1
                return
        if target is not None:
            self.imports["internal"] += 1
            for name in names:
                self.bindings[path][aliases.get(name, name)] = (target, name)
            return
        if level:
            self._unresolved(path, "." * level + module)
            return
        top = module.split(".")[0]
        dist = normalize_pypi(PY_IMPORT_TO_DIST.get(top, top))
        if top in sys.stdlib_module_names:
            self.imports["stdlib"] += 1
        elif ("pypi", dist) in self.declared:
            self.imports["external"] += 1
        elif ("pypi", dist) in self.workspace:
            self.imports["workspace"] += 1
        else:
            self._unresolved(path, module)

    def _ts_file(self, stem: str) -> str | None:
        return probe(stem, self.files.keys())

    def _owner(self, file: str) -> dict[str, Any] | None:
        """The workspace npm package whose directory holds `file`, if any."""
        owners = [p for p in self.packages.values() if p["ecosystem"] == "npm" and (p["manifest_dir"] == "" or file.startswith(p["manifest_dir"] + "/"))]
        return max(owners, key=lambda p: len(p["manifest_dir"])) if owners else None

    def _ts_alias(self, path: str, spec: str) -> tuple[str, str] | None:
        """Resolve a tsconfig `paths` alias such as `@/components/x`.

        Returns ("internal", file) for a file in this unit, ("workspace", file or
        stem) for a target in a sibling package, or None. The config used is the
        one TypeScript would pick for `path`, with `extends` already followed."""
        config = pick_config(path, self.ts_configs)
        if config is None:
            return None
        for pattern, targets in sorted(config["paths"].items(), key=lambda kv: -len(kv[0].split("*")[0])):
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if not (spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix)):
                    continue
                rest = spec[len(prefix): len(spec) - len(suffix)]
                stems = [posixpath.join(config["root"], t.replace("*", rest, 1)) for t in targets]
            elif spec == pattern:
                stems = [posixpath.join(config["root"], t) for t in targets]
            else:
                continue
            for stem in stems:
                found = self._ts_file(stem)
                if found:
                    return "internal", found
            for stem in stems:
                stem = posixpath.normpath(stem)
                if self.root and stem.startswith(self.root + "/"):
                    continue
                owner = self._owner(stem)
                if owner is not None and (owner["manifest_dir"] or ".") != self.unit:
                    exact = owner.get("entries", {}).get(".") if owner.get("entries", {}).get(".") and _strip_ts(owner["entries"]["."]) == _strip_ts(stem) else None
                    return "workspace", exact or stem
            return None
        return None

    def _workspace_entry(self, spec: str, package: str) -> tuple[str | None, bool]:
        """(file, known) for `@acme/ui` or `@acme/ui/button` from the sibling's `exports`,
        `types` or `main`. known=False when the package declares nothing to bind to."""
        p = self.packages.get(("npm", package))
        entries = (p or {}).get("entries")
        if not entries and not (p or {}).get("exports_strict"):
            return None, False
        subpath = "." + spec[len(package):]
        if subpath in entries:
            return entries[subpath], True
        for pattern, target in entries.items():
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if subpath.startswith(prefix) and subpath.endswith(suffix):
                    return target.replace("*", subpath[len(prefix): len(subpath) - len(suffix)], 1), True
        if subpath != "." and not p.get("exports_strict"):
            return posixpath.join(p["manifest_dir"], subpath[2:]), True  # no exports map: any subpath is importable
        return None, True

    def _ts_import(self, path: str, o: dict[str, Any]) -> None:
        p = o["payload"]
        spec = p.get("specifier") or ""
        names = p.get("names") or []
        if spec.startswith("."):
            target = self._ts_file(posixpath.join(posixpath.dirname(path), spec))
            alias = ("internal", target) if target else None
        else:
            alias = self._ts_alias(path, spec)
        if alias is not None and alias[0] == "internal":
            self.imports["internal"] += 1
            for name in names:
                self.bindings[path][name] = (alias[1], name)
            return
        if alias is not None:  # a path alias into a sibling package
            self.imports["workspace"] += 1
            owner = self._owner(alias[1])
            self.workspace_bound.append(f"{path}: {spec} -> {alias[1]}")
            for name in names:
                self.external_bindings[path][name] = (owner["name"] if owner else "", alias[1], name)
            return
        if spec.startswith("."):
            self._unresolved(path, spec)
            return
        package = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
        if spec.startswith("node:") or package in NODE_BUILTINS:
            self.imports["stdlib"] += 1
        elif ("npm", package) in self.workspace and ("npm", package) in self.declared:
            file, known = self._workspace_entry(spec, package)
            if known and file is None:
                self._unresolved(path, f"{spec} (not exported by {package})")
                return
            self.imports["workspace"] += 1
            if file:
                self.workspace_bound.append(f"{path}: {spec} -> {file}")
                for name in names:
                    self.external_bindings[path][name] = (package, file, name)
        elif ("npm", package) in self.declared:
            self.imports["external"] += 1
        else:
            self._unresolved(path, spec)

    def _unresolved(self, path: str, what: str) -> None:
        self.imports["unresolved"] += 1
        self.unresolved_imports.append(f"{path}: {what}")

    def resolve_imports(self) -> None:
        for path in sorted(self.files):
            for o in _of(self.files[path], "import"):
                (self._py_import if path.endswith(".py") else self._ts_import)(path, o)

    def lookup(self, path: str, name: str) -> tuple[str, str] | None:
        """Where does `name`, as used in `path`, come from? (file, name) or None."""
        head = name.split(".")[-1]
        if name in self.symbols[path]:
            return path, name
        if name in self.bindings[path]:
            return self.bindings[path][name]
        if "." in name and head in self.symbols[path]:
            return path, head
        return None

    # -- routes

    def link_routes(self) -> dict[str, int]:
        counts = {"symbol": 0, "inline": 0, "unresolved": 0}
        for path in sorted(self.files):
            endpoints = {(e["payload"]["natural_key"]["method"], e["payload"]["natural_key"]["route"]): e for e in _entities(self.files[path], "Endpoint")}
            for o in _of(self.files[path], "route_handler"):
                p = o["payload"]
                handler = p.get("handler")
                endpoint = endpoints.get((p["method"], p["route"]))
                if handler is None:
                    counts["inline"] += 1
                    continue
                found = self.lookup(path, handler)
                if endpoint is None or found is None or found[1] not in self.symbols[found[0]]:
                    counts["unresolved"] += 1
                    continue
                counts["symbol"] += 1
                e = endpoint["payload"]
                self.relate(path, self.symbol_ref(*found), Predicate.IMPLEMENTS, ref(EntityKind.ENDPOINT, e["natural_key"], e["name"]), [o["evidence"], self.symbols[found[0]][found[1]]["evidence"]])
        return counts

    # -- data access

    def _table_for(self, path: str, model: str) -> dict[str, Any] | None:
        found = self.lookup(path, model)
        if found is None:
            return None
        target, name = found
        return next((t for t in _entities(self.files[target], "Table") if t["payload"]["attributes"].get("model") == name), None)

    def _function_ref(self, path: str, function: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if function not in self.symbols[path]:  # a method: declare it on demand
            self.emit("entity", path, {**self.symbol_ref(path, function), "attributes": {"symbol_kind": "method", "path": path}}, [evidence], EvidenceClass.E2)
        return self.symbol_ref(path, function)

    def link_data_access(self) -> dict[str, int]:
        counts = {"linked": 0, "unresolved_model": 0, "dynamic_sql": 0}
        scope = f"data_access:{self.unit}"
        for path in sorted(self.files):
            for o in _of(self.files[path], "orm_use"):
                p = o["payload"]
                table = self._table_for(path, p["model"])
                subject = self._function_ref(path, p["function"], o["evidence"])
                predicate = Predicate.WRITES_TO if p["access"] == "write" else Predicate.READS_FROM
                if table is None:
                    counts["unresolved_model"] += 1
                    self.unknowns.append({"path": path, "subject": subject, "predicate": str(predicate), "status": "unknown", "reason": f"model {p['model']} does not resolve to a table in this unit", "coverage_ref": scope})
                    continue
                counts["linked"] += 1
                t = table["payload"]
                self.relate(path, subject, predicate, ref(EntityKind.TABLE, t["natural_key"], t["name"]), [o["evidence"], table["evidence"]], {"via": "orm"})
            for o in _of(self.files[path], "dynamic_sql"):
                counts["dynamic_sql"] += 1
                subject = self._function_ref(path, o["payload"]["function"], o["evidence"])
                self.unknowns.append({"path": path, "subject": subject, "predicate": str(Predicate.READS_FROM), "status": "unknown", "reason": "dynamic_sql: the statement is built at runtime", "coverage_ref": scope})
        return counts

    # -- event topics (Tier B)

    def _constant(self, path: str, name: str) -> dict[str, Any] | None:
        """The string constant `name` refers to in `path`: defined there, or imported by name.

        `Topics.ORDERS` is an object member when `Topics` is defined or imported
        here; a Python `module.NAME` falls back to `NAME` in this file.
        """
        head, _, rest = name.partition(".")
        candidates = []
        if name in self.bindings[path]:
            candidates.append(self.bindings[path][name])
        if rest and head in self.bindings[path]:
            target, original = self.bindings[path][head]
            candidates.append((target, f"{original}.{rest}"))
        candidates.append((path, name))
        if rest:
            candidates.append((path, name.split(".")[-1]))
        for target, original in candidates:
            found = next((o for o in _of(self.files.get(target, {"observations": []}), "string_constant") if o["payload"]["name"] == original), None)
            if found is not None:
                return found
        return None

    def link_topics(self) -> dict[str, int]:
        counts = {"literal": 0, "constant": 0, "unresolved": 0}
        scope = f"event_topics:{self.unit}"
        for path in sorted(self.files):
            for o in _of(self.files[path], "topic_use"):
                p = o["payload"]
                predicate = Predicate.PUBLISHES_TO if p["role"] == "publish" else Predicate.SUBSCRIBES_TO
                evidence = [o["evidence"]]
                topic = p.get("topic")
                if topic is not None:
                    counts["literal"] += 1
                elif p.get("name"):
                    constant = self._constant(path, p["name"])
                    if constant is not None:
                        topic = constant["payload"]["value"]
                        evidence.append(constant["evidence"])
                        counts["constant"] += 1
                if topic is None:
                    counts["unresolved"] += 1
                    if self.package_ref is not None:
                        reason = f"topic {p['name']} does not resolve to a string constant in this unit" if p.get("name") else "the topic is computed at run time"
                        self.unknowns.append({"path": path, "subject": self.package_ref, "predicate": str(predicate), "status": "unknown", "reason": reason, "coverage_ref": scope})
                    continue
                entity = ref(EntityKind.EVENT_TOPIC, {"name": topic}, topic)
                self.emit("entity", path, {**entity, "attributes": {"transport": p.get("transport") or "unknown"}}, evidence, EvidenceClass.E3 if len(evidence) > 1 else EvidenceClass.E2)
                self.relate(path, "@package", predicate, entity, evidence, {"via": "constant" if len(evidence) > 1 else "literal"})
        return counts

    # -- tests

    def link_tests(self) -> None:
        for path in sorted(p for p in self.files if _TEST_PY.search(p)):
            tests = _of(self.files[path], "test")
            targets = sorted({found for found in self.bindings[path].values() if found[1] in self.symbols[found[0]]})
            for t in tests:
                test = ref(EntityKind.TEST, {"path": path, "name": t["payload"]["name"]}, t["payload"]["name"])
                self.emit("entity", path, {**test, "attributes": {"framework": "pytest"}}, [t["evidence"]], EvidenceClass.E2)
                for target in targets:  # the test file imports it; whether this test calls it is not established
                    self.relate(path, self.symbol_ref(*target), Predicate.TESTED_BY, test, [t["evidence"], self.symbols[target[0]][target[1]]["evidence"]], {"basis": "test_file_import"}, evidence_class=EvidenceClass.E4, resolution="candidate")

    # -- lockfile versions (GI-007)

    def link_resolved(self) -> None:
        """The versions a lockfile pins for each package's declared dependencies, as
        package attributes: what the stack facts (DM02) report as the version in use."""
        for r in self.unit_row.get("resolved", []):
            if r["manifest"] not in self.files:
                continue
            pkg = ref(EntityKind.PACKAGE, r["package"], r["package"]["name"])
            self.emit("entity", r["manifest"], {**pkg, "attributes": {"resolved_dependencies": r["versions"], "lockfile": r["lockfile"]}}, r["evidence"], EvidenceClass.E1)

    # -- AI integrations (DM06)

    def link_ai(self) -> None:
        sdks: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(self.files):
            for o in _of(self.files[path], "import"):
                name = o["payload"].get("module") if path.endswith(".py") else o["payload"].get("specifier")
                provider = AI_SDKS.get(name or "") or AI_SDKS.get((name or "").split(".")[0])
                if provider:
                    sdks.setdefault(provider, []).append({"path": path, "evidence": o["evidence"]})
        models = [(path, m) for path in sorted(self.files) for m in _entities(self.files[path], "ModelEndpoint")]
        for provider in sorted(sdks):
            first = sdks[provider][0]
            component = ref(EntityKind.AI_COMPONENT, {"component_root": "@unit", "sdk": provider}, f"{provider} client")
            self.emit("entity", first["path"], {**component, "attributes": {"sdk": provider, "call_sites": len(sdks[provider])}}, [u["evidence"] for u in sdks[provider]], EvidenceClass.E3)
            for path, m in models:  # a model identifier in the same package as the SDK: a lead, not proof
                if m["payload"]["natural_key"]["provider"] == provider:
                    self.relate(first["path"], component, Predicate.CONSUMES, ref(EntityKind.MODEL_ENDPOINT, m["payload"]["natural_key"], m["payload"]["name"]), [first["evidence"], m["evidence"]], {"basis": "same_package"}, evidence_class=EvidenceClass.E4, resolution="candidate")

    # -- exports for repo-level linking

    def exports(self) -> dict[str, Any]:
        operations, calls = [], []
        for path in sorted(self.files):
            interfaces = _entities(self.files[path], "Interface")
            for o in _of(self.files[path], "contract_operation"):
                if interfaces:
                    i = interfaces[0]["payload"]
                    operations.append({"path": path, "method": o["payload"]["method"], "route": o["payload"]["route"], "interface": ref(EntityKind.INTERFACE, i["natural_key"], i["name"]), "evidence": o["evidence"]})
            for o in _of(self.files[path], "http_call"):
                calls.append({"path": path, "call_path": o["payload"]["path"], "evidence": o["evidence"]})
        return {"contract_operations": operations, "http_calls": calls, "has_package": self.has_package}


def _unit_link(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    unit = inputs["unit"]
    linker = _Linker(unit, inputs["summaries"], inputs["workspace_packages"])
    linker.declare_symbols()
    linker.resolve_imports()
    routes = linker.link_routes()
    data = linker.link_data_access()
    topics = linker.link_topics()
    linker.link_tests()
    linker.link_ai()
    linker.link_resolved()

    imp = linker.imports
    total_imports = sum(imp.values())
    total_routes = sum(routes.values())
    total_data = sum(data.values())
    coverage = [
        CoverageEntry(domain="import_resolution", scope=unit["name"], numerator=total_imports - imp["unresolved"], denominator=total_imports, detail={**imp, "unresolved_imports": sorted(linker.unresolved_imports)[:50], **({"workspace_bound": sorted(linker.workspace_bound)[:50]} if linker.workspace_bound else {})}).row(),
        CoverageEntry(domain="route_to_handler", scope=unit["name"], numerator=routes["symbol"] + routes["inline"], denominator=total_routes, detail=routes).row(),
        CoverageEntry(domain="data_access", scope=unit["name"], numerator=data["linked"], denominator=total_data, detail=data).row(),
    ]
    if sum(topics.values()):  # reported only where a unit uses a message broker
        coverage.append(CoverageEntry(domain="event_topics", scope=unit["name"], numerator=topics["literal"] + topics["constant"], denominator=sum(topics.values()), detail=topics).row())
    return {"unit": unit["name"], "observations": linker.out, "unknowns": linker.unknowns, "coverage": coverage, "exports": linker.exports()}


UNIT_LINK = Step("unit.link", "0.6.0", _unit_link)


# ---------------------------------------------------------------- repo-level


_PARAM = re.compile(r"\{[^/}]*\}|:[A-Za-z_][A-Za-z0-9_]*|\[[^/\]]*\]")


def normalize_route(route: str) -> str:
    """Compare routes across frameworks: /a/{id}, /a/:id and /a/[id] are the same."""
    route = _PARAM.sub("{}", route.split("?")[0])
    return "/" + route.strip("/")


def match_call(call_path: str, route: str) -> bool:
    """A client path matches a route when it equals it, or ends with it (a proxy or base URL prefix)."""
    call = normalize_route(call_path.replace("{}", "", 1) if call_path.startswith("{}") else call_path)
    target = normalize_route(route)
    return target != "/" and (call == target or call.endswith(target))
