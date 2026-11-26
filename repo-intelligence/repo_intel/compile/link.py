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

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import Assertion, CoverageEntry
from repo_intel.extractors.base import ref
from repo_intel.extractors.infrastructure import image_repository
from repo_intel.extractors.manifests import effective_declarations, normalize_pypi
from repo_intel.compile.ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate
from repo_intel.compile.planning import pick_config, probe

ROOT_UNIT = "@root"
PY_IMPORT_TO_DIST = {"yaml": "pyyaml", "PIL": "pillow", "sklearn": "scikit-learn", "cv2": "opencv-python", "dateutil": "python-dateutil", "dotenv": "python-dotenv", "jwt": "pyjwt", "bs4": "beautifulsoup4"}
NODE_BUILTINS = {"assert", "buffer", "child_process", "cluster", "crypto", "dns", "events", "fs", "http", "http2", "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib"}
AI_SDKS = {"anthropic": "anthropic", "openai": "openai", "google.generativeai": "google", "google.genai": "google", "mistralai": "mistral", "cohere": "cohere",
           "@anthropic-ai/sdk": "anthropic", "@google/generative-ai": "google", "@mistralai/mistralai": "mistral", "cohere-ai": "cohere"}
TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
SYMBOL_END = "@symbol:"  # a relation end naming a top-level exported symbol of the same file (section 8.5)
_TEST_PY = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
_TEST_TS = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(^|/)__tests__/")


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


def _revision_key(revision: str) -> tuple[Any, ...]:
    """Order revisions as their tools do: Flyway "1.10" after "1.2"; Prisma timestamps as text."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", revision) if part)


def _lower_first(name: str) -> str:
    return name[:1].lower() + name[1:]


def _join_route(prefix: str, route: str) -> str:
    """FastAPI concatenates prefixes: "/api" + "/orders" = "/api/orders"; "/api" + "" = "/api"."""
    joined = (prefix.rstrip("/") + route) if route else (prefix or "/")
    return re.sub(r"/{2,}", "/", joined) or "/"


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
        standing, _ = effective_declarations([(s["path"], o) for s in summaries for o in _of(s, "package_declaration")])
        own = sorted((o["payload"] for _, o in standing if (o["payload"]["manifest_dir"] or ".") == self.unit), key=lambda p: (p["ecosystem"], p["name"]))
        self.package = own[0]["name"] if own else self.unit
        self.has_package = bool(own)
        self.package_ref = ref(EntityKind.PACKAGE, {k: own[0][k] for k in ("ecosystem", "name", "manifest_dir")}, own[0]["name"]) if own else None
        self.declared = {(o["payload"]["ecosystem"], o["payload"]["name"]) for s in summaries for o in _of(s, "dependency_declaration")}
        self.workspace = {(p["ecosystem"], p["name"]) for p in workspace}
        self.out: list[dict[str, Any]] = []
        self.unknowns: list[dict[str, Any]] = []
        self.symbols: dict[str, dict[str, dict[str, Any]]] = {p: {o["payload"]["name"]: o for o in _of(s, "symbol")} for p, s in self.files.items()}
        self.bindings: dict[str, dict[str, tuple[str, str]]] = {p: {} for p in self.files}  # local name -> (file, original name)
        self.modules: dict[str, dict[str, str]] = {p: {} for p in self.files}  # local (dotted) module name -> file
        self.ts_configs = unit.get("ts_configs") or _local_ts_configs(summaries)
        self.packages = {(p["ecosystem"], p["name"]): p for p in workspace}
        self.external_bindings: dict[str, dict[str, tuple[str, str, str]]] = {p: {} for p in self.files}  # local name -> (package, file, name)
        self.workspace_bound: list[str] = []
        self.imports = {"internal": 0, "workspace": 0, "external": 0, "stdlib": 0, "unresolved": 0}
        self.unresolved_imports: list[str] = []
        self.inline_linked: set[tuple[str, str]] = set()
        self.file_imports: dict[tuple[str, str], dict[str, Any]] = {}  # (importer, imported file) -> first import's evidence

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
        if not names and target is not None:  # `import a.b` / `import a.b as c`: a module binding
            self.modules[path][p.get("alias") or module] = target
        for name in names:  # `from pkg import mod`: the name may be a submodule
            sub = self._py_target(path, f"{module}.{name}" if module else name, level) if name != "*" else None
            if sub is not None:
                self.modules[path][aliases.get(name, name)] = sub
        if target is None and level and not module:  # `from . import x`: each name may itself be a module
            hits = {n: self._py_target(path, n, level) for n in names}
            if names and all(hits.values()):
                self.imports["internal"] += 1
                for file in hits.values():
                    self._file_import(path, file, o)
                return
        if target is not None:
            self.imports["internal"] += 1
            subs = [self._py_target(path, f"{module}.{n}" if module else n, level) for n in names if n != "*"]
            for file in [target, *(s for s in subs if s)]:
                if not file.endswith("__init__.py") or file == target and not any(subs):
                    self._file_import(path, file, o)
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
            self._file_import(path, alias[1], o)
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

    def _file_import(self, path: str, file: str, o: dict[str, Any]) -> None:
        if file != path:
            self.file_imports.setdefault((path, file), o["evidence"])

    def link_file_imports(self) -> None:
        """Artifact DEPENDS_ON Artifact for each import that resolves to a file of this unit:
        the module graph that layering and coupling are read from."""
        for (path, file), evidence in sorted(self.file_imports.items()):
            self.relate(path, "@file", Predicate.DEPENDS_ON, ref(EntityKind.ARTIFACT, {"path": file}, posixpath.basename(file), declare=False), [evidence], {"via": "import"}, evidence_class=EvidenceClass.E2)

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

    def resolve_object(self, path: str, dotted: str) -> tuple[str, str] | None:
        """(file, name) of the module-level object `dotted` names in `path`: defined
        here, imported by name, or reached through an imported module."""
        if "." not in dotted:
            if dotted in self.bindings[path]:
                return self.bindings[path][dotted]
            return path, dotted
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            if head in self.modules[path]:
                return self.modules[path][head], ".".join(parts[i:])
        return None

    def link_routers(self) -> dict[str, Any]:
        """FastAPI routers and sub-applications: compose `include_router(prefix=)` and
        `mount` prefixes across files, and emit each router route's Endpoint at its
        served path. Returns the endpoints per route_handler for `link_routes`."""
        decls = {(path, o["payload"]["name"]): o for path in self.files for o in _of(self.files[path], "router_decl")}
        parents: dict[tuple[str, str], list[tuple[tuple[str, str], str | None, dict[str, Any], str]]] = {}
        for path in sorted(self.files):
            for o in _of(self.files[path], "router_include"):
                p = o["payload"]
                owner, target = self.resolve_object(path, p["owner"]), self.resolve_object(path, p["target"])
                if owner in decls and target in decls:
                    parents.setdefault(target, []).append((owner, p["prefix"], o["evidence"], p["via"]))
        self.router_counts = {"composed": 0, "unmounted": 0, "dynamic_prefix": 0, "unknown_router": 0}

        def prefixes(node: tuple[str, str], seen: frozenset) -> list[tuple[str | None, list[dict[str, Any]]]]:
            """Every full prefix under which `node` is served, with the include calls on the way."""
            if node not in parents or node in seen:
                return [("", [])]
            out = []
            for owner, prefix, ev, _ in sorted(parents[node], key=lambda x: (x[0], x[1] or "")):
                # An APIRouter's own prefix applies to what is included into it, too.
                own = decls[owner]["payload"].get("prefix", "")
                for above, chain in prefixes(owner, seen | {node}):
                    out.append((None if above is None or prefix is None or own is None else above + own + prefix, chain + [ev]))
            return out

        found: dict[str, dict[int, list[dict[str, Any]]]] = {}
        for path in sorted(self.files):
            for i, o in enumerate(_of(self.files[path], "route_handler")):
                p = o["payload"]
                if p.get("framework", "fastapi") != "fastapi" or "router" not in p:
                    continue
                node = self.resolve_object(path, p["router"])
                decl = decls.get(node) if node else None
                if decl is None:
                    self.router_counts["unknown_router"] += 1
                    if node is None or node[0] == path:
                        continue  # an object of this file that is no FastAPI app or router
                    places = [("", [])]  # a router imported from a file this unit does not hold
                else:
                    places = prefixes(node, frozenset())
                local = decl is not None and decl["payload"]["factory"] == "FastAPI" and node[0] == path
                mounted = node in parents
                for prefix, chain in places:
                    if prefix is None:
                        self.router_counts["dynamic_prefix"] += 1
                        continue
                    route = _join_route(prefix, p["route"])
                    if local and not chain:
                        continue  # the extractor already emitted this one: an app route at its own path
                    key = {"component_root": "@unit", "protocol": "http", "method": p["method"], "route": route}
                    endpoint = ref(EntityKind.ENDPOINT, key, f"{p['method']} {route}")
                    attributes = {"framework": "fastapi", "handler": p.get("handler")}
                    if decl is not None and decl["payload"]["factory"] == "APIRouter" and not mounted:
                        attributes["mounted"] = False
                        self.router_counts["unmounted"] += 1
                    elif chain:
                        self.router_counts["composed"] += 1
                    self.emit("entity", path, {**endpoint, "attributes": attributes}, [o["evidence"], *chain], EvidenceClass.E3 if chain else EvidenceClass.E2)
                    found.setdefault(path, {}).setdefault(i, []).append(endpoint)
                    if local and chain:  # a sub-application mounted under a prefix: its own path is an alias
                        own = ref(EntityKind.ENDPOINT, {**key, "route": p["route"]}, f"{p['method']} {p['route']}", declare=False)
                        self.relate(path, own, Predicate.ALIAS_OF, endpoint, [o["evidence"], *chain], {"basis": "mounted_sub_application", "prefix": prefix})
        return found

    def link_routes(self, composed: dict[str, dict[int, list[dict[str, Any]]]] | None = None) -> dict[str, int]:
        counts = {"symbol": 0, "inline": 0, "unresolved": 0}
        composed = composed or {}
        for path in sorted(self.files):
            endpoints = {(e["payload"]["natural_key"]["method"], e["payload"]["natural_key"]["route"]): e for e in _entities(self.files[path], "Endpoint")}
            for i, o in enumerate(_of(self.files[path], "route_handler")):
                p = o["payload"]
                handler = p.get("handler")
                targets = composed.get(path, {}).get(i) or ([ref(EntityKind.ENDPOINT, endpoints[(p["method"], p["route"])]["payload"]["natural_key"], endpoints[(p["method"], p["route"])]["payload"]["name"])] if (p["method"], p["route"]) in endpoints else [])
                if handler is None:
                    counts["inline"] += 1
                    continue
                found = self.lookup(path, handler)
                if not targets or found is None or found[1] not in self.symbols[found[0]]:
                    counts["unresolved"] += 1
                    continue
                counts["symbol"] += 1
                for endpoint in targets:
                    self.relate(path, self.symbol_ref(*found), Predicate.IMPLEMENTS, endpoint, [o["evidence"], self.symbols[found[0]][found[1]]["evidence"]])
        return counts

    # -- call graph (Python)

    def link_calls(self) -> dict[str, int]:
        """CALLS between symbols of this unit, where the callee resolves exactly: a local
        function or class, an imported name, `module.f`, `Class.method`, `self.method`."""
        counts = {"resolved": 0, "unresolved": 0, "not_a_symbol": 0}
        for path in sorted(p for p in self.files if p.endswith(".py")):
            for o in _of(self.files[path], "call"):
                caller, callee = o["payload"]["caller"], o["payload"]["callee"]
                if caller not in self.symbols[path]:
                    continue
                parts = callee.split(".")
                target: tuple[str, str] | None = None
                if parts[0] in ("self", "cls") and "." in caller and len(parts) == 2:
                    target = (path, f"{caller.split('.')[0]}.{parts[1]}")
                elif len(parts) > 1 and parts[0] in self.modules[path] and parts[0] not in self.symbols[path]:
                    target = self.resolve_object(path, callee)
                elif parts[0] in self.symbols[path] or parts[0] in self.bindings[path]:
                    base = self.lookup(path, parts[0])
                    target = (base[0], ".".join([base[1], *parts[1:]])) if base else None
                else:
                    target = self.resolve_object(path, callee) if len(parts) > 1 else None
                    if target is not None and target[1] not in self.symbols.get(target[0], {}):
                        head, _, rest = target[1].partition(".")  # module.name re-exported from a package
                        found = self.lookup(target[0], head) if target[0] in self.files else None
                        target = (found[0], ".".join(filter(None, [found[1], rest]))) if found else target
                    elif target is None:
                        continue  # not a name this unit binds: a library or builtin call
                if target is None or target[0] not in self.symbols:
                    counts["unresolved"] += 1
                    continue
                if target[1] not in self.symbols[target[0]]:
                    # An object imported by name that is no function or class (`Session =
                    # sessionmaker()`) is not a symbol; anything else that does not resolve is unresolved.
                    by_name = len(parts) == 1 and parts[0] in self.bindings[path]
                    counts["not_a_symbol" if by_name else "unresolved"] += 1
                    continue
                if (target[0], target[1]) == (path, caller):
                    continue  # recursion adds nothing
                counts["resolved"] += 1
                self.relate(path, self.symbol_ref(path, caller), Predicate.CALLS, self.symbol_ref(*target), [o["evidence"], self.symbols[target[0]][target[1]]["evidence"]])
        return counts

    # -- data access

    def _table_for(self, path: str, model: str, orm: str | None = None) -> dict[str, Any] | None:
        if orm == "prisma":  # a client delegate (`order`, `orderItem`) names its model, lower-camel
            tables = [t for p in sorted(self.files) for t in _entities(self.files[p], "Table") if t["payload"]["attributes"].get("orm") == "prisma"]
            hits = [t for t in tables if _lower_first(t["payload"]["attributes"].get("model") or "") == model]
            return hits[0] if len(hits) == 1 else None
        found = self.lookup(path, model)
        if found is None:
            return None
        target, name = found
        return next((t for t in _entities(self.files[target], "Table") if t["payload"]["attributes"].get("model") == name), None)

    def _function_ref(self, path: str, function: str, evidence: dict[str, Any], kind: str = "method") -> dict[str, Any]:
        if function not in self.symbols[path]:  # a method or an inline callback: declare it on demand
            self.emit("entity", path, {**self.symbol_ref(path, function), "attributes": {"symbol_kind": kind, "path": path}}, [evidence], EvidenceClass.E2)
            if kind == "route_handler":  # an inline route callback implements the route it is named after
                endpoint = next((e for e in _entities(self.files[path], "Endpoint") if e["payload"]["name"] == function), None)
                if endpoint is not None and (path, function) not in self.inline_linked:
                    self.inline_linked.add((path, function))
                    e = endpoint["payload"]
                    self.relate(path, self.symbol_ref(path, function), Predicate.IMPLEMENTS, ref(EntityKind.ENDPOINT, e["natural_key"], e["name"]), [endpoint["evidence"], evidence])
        return self.symbol_ref(path, function)

    def link_data_access(self) -> dict[str, int]:
        counts = {"linked": 0, "unresolved_model": 0, "dynamic_sql": 0}
        scope = f"data_access:{self.unit}"
        for path in sorted(self.files):
            for o in _of(self.files[path], "orm_use"):
                p = o["payload"]
                table = self._table_for(path, p["model"], p.get("orm"))
                subject = self._function_ref(path, p["function"], o["evidence"], p.get("function_kind", "method"))
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
                subject = self._function_ref(path, o["payload"]["function"], o["evidence"], o["payload"].get("function_kind", "method"))
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
        """Test cases in pytest files and in TypeScript/JavaScript test files (`*.test.ts`,
        `*.spec.tsx`, `__tests__/`), linked as candidates to the symbols their file imports."""
        for path in sorted(p for p in self.files if _TEST_PY.search(p) or _TEST_TS.search(p)):
            tests = _of(self.files[path], "test")
            targets = sorted({found for found in self.bindings[path].values() if found[0] in self.symbols and found[1] in self.symbols[found[0]]})
            for t in tests:
                test = ref(EntityKind.TEST, {"path": path, "name": t["payload"]["name"]}, t["payload"]["name"])
                self.emit("entity", path, {**test, "attributes": {"framework": t["payload"].get("framework", "pytest")}}, [t["evidence"]], EvidenceClass.E2)
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

    # -- aliases (GI-016)

    def link_model_aliases(self) -> None:
        """An ORM model class and the table it maps are one thing under two names."""
        for path in sorted(self.files):
            for t in _entities(self.files[path], "Table"):
                attrs = t["payload"]["attributes"]
                model = attrs.get("model")
                if model and model in self.symbols[path]:
                    table = ref(EntityKind.TABLE, t["payload"]["natural_key"], t["payload"]["name"])
                    self.relate(path, self.symbol_ref(path, model), Predicate.ALIAS_OF, table, [self.symbols[path][model]["evidence"], t["evidence"]], {"basis": "orm_model", "orm": attrs.get("orm")}, evidence_class=EvidenceClass.E2)

    # -- SQL migrations (GI-010)

    def link_migrations(self) -> None:
        """Replay the unit's SQL migrations in revision order and declare the schema that
        results: a table created by one migration and dropped by a later one is not declared,
        and a column a later migration adds or renames lands on the table it changed."""
        steps: dict[str, list[tuple[Any, ...]]] = {}
        for path in sorted(self.files):
            for o in _of(self.files[path], "migration_op"):
                p = o["payload"]
                steps.setdefault(p["tool"], []).append((_revision_key(p["revision"]), p["seq"], path, p, o["evidence"]))
        for tool in sorted(steps):
            tables: dict[str, dict[str, Any]] = {}  # name -> {"path", "evidence", "created", "columns": {name: (path, evidence, type)}}
            for _, _, path, p, ev in sorted(steps[tool], key=lambda t: t[:3]):
                op, name = p["op"], p["table"]
                if op == "create_table":
                    tables[name] = {"path": path, "evidence": ev, "created": True, "columns": {c: (path, ev, t) for c, t in p.get("columns", {}).items()}}
                    continue
                if op == "drop_table":
                    tables.pop(name, None)
                    continue
                state = tables.setdefault(name, {"path": path, "evidence": ev, "created": False, "columns": {}})
                column = p.get("column")
                if op == "add_column":
                    state["columns"][column] = (path, ev, p.get("type"))
                elif op == "drop_column":
                    state["columns"].pop(column, None)
                elif op == "rename_column":
                    kept = state["columns"].pop(column, (path, ev, None))
                    state["columns"][p["new_column_name"]] = (path, ev, kept[2])
                elif op == "alter_column" and column in state["columns"] and p.get("type"):
                    state["columns"][column] = (*state["columns"][column][:2], p["type"])
                elif op == "rename_table":
                    tables[p["new_table_name"]] = tables.pop(name)
            for name, state in sorted(tables.items()):
                table = ref(EntityKind.TABLE, {"datastore": "default", "name": name}, name, declare=state["created"])
                if state["created"]:
                    self.emit("entity", state["path"], {**table, "attributes": {}}, [state["evidence"]], EvidenceClass.E1)
                for column, (path, ev, dtype) in sorted(state["columns"].items()):
                    field = ref(EntityKind.FIELD, {"table": name, "name": column}, column)
                    self.emit("entity", path, {**field, "attributes": {"type": dtype} if dtype else {}}, [ev], EvidenceClass.E1)
                    self.relate(path, table, Predicate.CONTAINS, field, [ev], evidence_class=EvidenceClass.E1)

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

    # -- per-file Symbol ends (docs/solution-ir-contract.md, section 8.5)

    def symbol_ends(self) -> dict[str, dict[str, dict[str, Any] | None]]:
        """What each "@symbol:<name>" relation end of a file resolves to.

        An extractor may name a relation end "@symbol:<name>": the top-level exported symbol
        `<name>` declared in the same file. One file cannot build that Symbol's key, which
        needs the unit's package, so the linker resolves it here. The result maps each file
        that uses such an end to {name: the Symbol ref, as `symbol_ref` builds it, or None
        when the file exports no symbol of that name}. `unit.compile` then relates the
        Symbol, or falls back to "@package" with qualifier `component: <name>`.

        Only symbols the extractor marks `exported` count (a Python method symbol, say, is
        not an export). A name the file declares and exports only as its default
        (`const X = ...; export default X`, `export { X as default }`) resolves to the
        `default` Symbol, through the `local` name that symbol records.
        """
        out: dict[str, dict[str, dict[str, Any] | None]] = {}
        for path in sorted(self.files):
            exported = {n for n, o in self.symbols[path].items() if o["payload"].get("exported") is True}
            default = self.symbols[path].get("default", {}).get("payload", {})
            local_default = default.get("local") if "default" in exported else None
            for o in _of(self.files[path], "relation"):
                for end in (o["payload"].get("subject"), o["payload"].get("object")):
                    if isinstance(end, str) and end.startswith(SYMBOL_END):
                        name = end[len(SYMBOL_END):]
                        target = name if name in exported else "default" if name == local_default else None
                        out.setdefault(path, {})[name] = {**self.symbol_ref(path, target), "declare": False} if target else None
        return out

    # -- routes below an application root (ASP.NET Web Forms, docs/solution-ir-contract.md section 8.6)

    def link_app_relative_routes(self) -> None:
        """Endpoints for pages served at their path below an application root.

        A Web Forms page answers at its path below the application root, the nearest
        ancestor directory holding a Web.config, not at its repository path. One file
        cannot see that, so the extractor emits an `app_relative_route` observation
        ({subject, path, root_marker, method, attributes, requires_roles, unkeyed_reason})
        and this pass looks for the marker file among the unit's files, deepest directory
        first. The Endpoint is keyed by "/" + the page path relative to that directory,
        with the page and the marker file as evidence (E3). With no marker above the page
        in this unit, no Endpoint is keyed: the root is not guessed, and an Unknown on the
        page records why.
        """
        markers: dict[str, dict[str, str]] = {}  # lower-cased basename -> {directory: marker path}
        for path in sorted(self.files):
            markers.setdefault(posixpath.basename(path).lower(), {}).setdefault(posixpath.dirname(path), path)
        for path in sorted(self.files):
            for o in _of(self.files[path], "app_relative_route"):
                p = o["payload"]
                found = markers.get(str(p["root_marker"]).lower(), {})
                parts = posixpath.dirname(p["path"]).split("/") if posixpath.dirname(p["path"]) else []
                root = next((d for d in ("/".join(parts[:i]) for i in range(len(parts), -1, -1)) if d in found), None)
                if root is None:
                    self.unknowns.append({"path": path, "subject": p["subject"], "predicate": str(Predicate.EXPOSES), "status": "unknown", "reason": p["unkeyed_reason"], "coverage_ref": f"app_routes:{self.unit}"})
                    continue
                route = "/" + (p["path"][len(root) + 1:] if root else p["path"])
                marker = self.files[found[root]]
                evidence = [o["evidence"], *(m["evidence"] for m in _of(marker, "file"))]
                endpoint = ref(EntityKind.ENDPOINT, {"component_root": "@unit", "protocol": "http", "method": p["method"], "route": route}, f"{p['method']} {route}")
                self.emit("entity", path, {**endpoint, "attributes": {**p["attributes"], "app_root": root or "."}}, evidence, EvidenceClass.E3)
                self.relate(path, p["subject"], Predicate.EXPOSES, endpoint, evidence, {"app_root": root or "."})
                for role in p.get("requires_roles", []):
                    self.relate(path, endpoint, Predicate.REQUIRES_ROLE, role, evidence, {"via": "Authorize"})

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
                # `method` only when the source states it (typescript_source); absent otherwise.
                method = o["payload"].get("method")
                calls.append({"path": path, "call_path": o["payload"]["path"], **({"method": method} if method else {}), "evidence": o["evidence"]})
        return {"contract_operations": operations, "http_calls": calls, "has_package": self.has_package}


def _unit_link(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    unit = inputs["unit"]
    linker = _Linker(unit, inputs["summaries"], inputs["workspace_packages"])
    linker.declare_symbols()
    linker.resolve_imports()
    linker.link_file_imports()
    routes = linker.link_routes(linker.link_routers())
    calls = linker.link_calls()
    data = linker.link_data_access()
    topics = linker.link_topics()
    linker.link_tests()
    linker.link_ai()
    linker.link_resolved()
    linker.link_migrations()
    linker.link_model_aliases()
    linker.link_app_relative_routes()

    imp = linker.imports
    total_imports = sum(imp.values())
    total_routes = sum(routes.values())
    total_data = sum(data.values())
    coverage = [
        CoverageEntry(domain="import_resolution", scope=unit["name"], numerator=total_imports - imp["unresolved"], denominator=total_imports, detail={**imp, "unresolved_imports": sorted(linker.unresolved_imports)[:50], **({"workspace_bound": sorted(linker.workspace_bound)[:50]} if linker.workspace_bound else {})}).row(),
        CoverageEntry(domain="route_to_handler", scope=unit["name"], numerator=routes["symbol"] + routes["inline"], denominator=total_routes, detail={**routes, **({"routers": linker.router_counts} if any(linker.router_counts.values()) else {})}).row(),
        CoverageEntry(domain="data_access", scope=unit["name"], numerator=data["linked"], denominator=total_data, detail=data).row(),
    ]
    if calls["resolved"] + calls["unresolved"]:  # reported only where a unit has calls to its own names
        coverage.append(CoverageEntry(domain="call_graph", scope=unit["name"], numerator=calls["resolved"], denominator=calls["resolved"] + calls["unresolved"], detail=calls).row())
    if sum(topics.values()):  # reported only where a unit uses a message broker
        coverage.append(CoverageEntry(domain="event_topics", scope=unit["name"], numerator=topics["literal"] + topics["constant"], denominator=sum(topics.values()), detail=topics).row())
    lineage = sorted(((s["path"], o["payload"]) for s in inputs["summaries"] for o in _of(s, "lineage_summary")), key=lambda x: x[0])
    if lineage:  # reported only where a unit reads or writes data by name (spec §8.2)
        resolved, unresolved = sum(p["resolved"] for _, p in lineage), sum(p["unresolved"] for _, p in lineage)
        coverage.append(CoverageEntry(domain="data_lineage", scope=unit["name"], numerator=resolved, denominator=resolved + unresolved, detail={"files": len(lineage), "files_with_unresolved": sorted(path for path, p in lineage if p["unresolved"])[:50]}).row())
    return {"unit": unit["name"], "observations": linker.out, "unknowns": linker.unknowns, "coverage": coverage, "exports": linker.exports(), "symbol_ends": linker.symbol_ends()}


UNIT_LINK = Step("unit.link", "0.12.0", _unit_link)


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


def link_repo(shards: Mapping[str, Mapping[str, Any]], g: Any) -> list[dict[str, Any]]:
    """Repo-level links between code and deployment (GI-016), added to the global shard `g`.

    CONFIGURED_BY  a component (or package) is configured by a ConfigKey its code reads
                   when a deployment unit also supplies that key: the key is the contract
                   between the two. Names only; values never enter the graph.
    BUILT_BY       a deployment unit runs an image a workflow builds (same image
                   repository): BUILT_BY that Pipeline and the Dockerfile it builds from.
                   A lead, not proof: candidate, E4.
    ALIAS_OF       deployment units that run the same image repository (candidate, E4).
    """
    ents = {e["id"]: e for s in shards.values() for e in s["entities"]}
    ev = {e["id"]: e for s in shards.values() for e in s["evidence"]}
    component_of = {(e["natural_key"]["root"] or "."): e["id"] for e in ents.values() if e["kind"] == EntityKind.COMPONENT}
    package_of = {(e["natural_key"]["manifest_dir"] or "."): e["id"] for e in sorted(ents.values(), key=lambda e: e["id"]) if e["kind"] == EntityKind.PACKAGE}

    def cite(ids: list[str]) -> list[str]:
        return [g.cite(ev[i]) for i in ids[:1] if i in ev]

    def assert_(subject: str, predicate: Predicate, obj: str, evidence: list[str], qualifiers: dict[str, Any], strong: bool) -> None:
        g.assertion(Assertion.make(subject, predicate, obj, qualifiers=qualifiers, assertion_kind=AssertionKind.DERIVED_RELATION,
                                   epistemic_status=EpistemicStatus.SUPPORTED if strong else EpistemicStatus.INFERRED,
                                   evidence_class=EvidenceClass.E3 if strong else EvidenceClass.E4, evidence_ids=sorted(set(evidence)), resolution="exact" if strong else "candidate"))

    coverage: list[dict[str, Any]] = []
    # CONFIGURED_BY
    code: dict[str, list[tuple[str, str, list[str]]]] = {}  # key -> [(unit, subject, evidence ids)]
    supplied: dict[str, list[tuple[str, list[str]]]] = {}  # key -> [(deployment unit, evidence ids)]
    for unit in sorted(shards):
        for a in shards[unit]["assertions"]:
            if a["predicate"] != Predicate.REFERENCES_CONFIG or a["subject"] not in ents:
                continue
            kind = ents[a["subject"]]["kind"]
            if kind == EntityKind.DEPLOYMENT_UNIT:
                supplied.setdefault(a["object"], []).append((a["subject"], a["evidence_ids"]))
            elif kind != EntityKind.INFRASTRUCTURE_RESOURCE:
                code.setdefault(a["object"], []).append((unit, a["subject"], a["evidence_ids"]))
    for key in sorted(code):
        units = supplied.get(key, [])
        if not units:
            continue
        by = sorted({ents[d]["name"] for d, _ in units})
        deploy_ev = [i for _, ids in sorted(units) for i in cite(ids)]
        for unit, subject, ids in sorted(code[key]):
            owner = component_of.get(unit) or (subject if ents[subject]["kind"] in (EntityKind.PACKAGE, EntityKind.COMPONENT) else package_of.get(unit))
            if owner is not None:
                assert_(owner, Predicate.CONFIGURED_BY, key, cite(ids) + deploy_ev, {"basis": "shared_config_key", "supplied_by": by}, True)
    if code and supplied:
        coverage.append(CoverageEntry(domain="config_supply", scope="@repo", numerator=sum(1 for k in code if k in supplied), denominator=len(code),
                                      detail={"unsupplied": sorted(ents[k]["name"] for k in code if k not in supplied)[:50]}).row())

    # images: deployment units, the pipelines that build them, and each other
    units = sorted((e for e in ents.values() if e["kind"] == EntityKind.DEPLOYMENT_UNIT and e["evidence_ids"]), key=lambda e: e["id"])
    runs: dict[str, list[str]] = {}  # image repository -> deployment unit ids
    for du in units:
        images = list(du["attributes"].get("images") or []) + ([du["attributes"]["image"]] if du["attributes"].get("image") else [])
        for repo in dict.fromkeys(image_repository(i) for i in images if isinstance(i, str) and i):
            runs.setdefault(repo, []).append(du["id"])
    # Container builds are keyed by their Dockerfile path; Bazel, Nx and Turborepo targets by {tool, label}.
    targets = {e["natural_key"]["path"]: e for e in ents.values() if e["kind"] == EntityKind.BUILD_TARGET and "path" in e["natural_key"]}
    built: set[str] = {a["subject"] for s in shards.values() for a in s["assertions"] if a["predicate"] == Predicate.BUILT_BY}
    for p in sorted((e for e in ents.values() if e["kind"] == EntityKind.PIPELINE), key=lambda e: e["id"]):
        for b in p["attributes"].get("image_builds") or []:
            for du in runs.get(b["image"], []):
                built.add(du)
                evidence = cite(ents[du]["evidence_ids"]) + cite(p["evidence_ids"])
                assert_(du, Predicate.BUILT_BY, p["id"], evidence, {"basis": "same_image", "image": b["image"]}, False)
                target = targets.get(b.get("dockerfile") or "")
                if target is not None:
                    assert_(du, Predicate.BUILT_BY, target["id"], evidence + cite(target["evidence_ids"]), {"basis": "same_image", "image": b["image"], "pipeline": p["name"]}, False)
    for repo, ids in sorted(runs.items()):
        for other in ids[1:]:
            assert_(other, Predicate.ALIAS_OF, ids[0], cite(ents[other]["evidence_ids"]) + cite(ents[ids[0]]["evidence_ids"]), {"basis": "same_image", "image": repo}, False)
    if units:
        coverage.append(CoverageEntry(domain="build_provenance", scope="@repo", numerator=sum(1 for du in units if du["id"] in built), denominator=len(units)).row())
    return coverage
