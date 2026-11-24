"""Package manifests, workspaces and lockfiles (GI-007).

Manifests: pyproject.toml, package.json, pom.xml, go.mod, Cargo.toml. Maven, Go
and Cargo are inventory-only in v1: their manifests yield a Package and its
declared dependencies, nothing more.

Workspaces: package.json `workspaces` (npm and yarn), pnpm-workspace.yaml,
`[tool.uv.workspace]` and Cargo `[workspace]` yield a "workspace_declaration"
with the member globs. `unit.plan` expands the globs against the repository
paths; a file cannot do that on its own.

Lockfiles: package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml, yarn.lock,
poetry.lock, uv.lock and Cargo.lock yield one "lockfile" observation with the
resolved versions. `unit.plan` matches them to each package's declared
dependencies. Only names and versions are read; registry URLs and integrity
hashes are not copied.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
import xml.etree.ElementTree as ET

import yaml

from ..ontology import EvidenceClass
from .base import Extraction, diagnostic, find_span, obs, span, whole_file

E1 = EvidenceClass.E1


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _package(blob, at, ecosystem, name, path, attributes=None):
    """Attributes describe the shape of the source manifest."""
    return obs("package_declaration", blob, at, {"ecosystem": ecosystem, "name": name, "manifest_dir": posixpath.dirname(path), "attributes": attributes or {}}, E1)


def _dependency(blob, at, ecosystem, name, specifier, scope="runtime"):
    return obs("dependency_declaration", blob, at, {"ecosystem": ecosystem, "name": name, "specifier": specifier, "scope": scope}, E1)


def _workspace(blob, at, ecosystem, tool, path, globs, exclude=()):
    globs = [g for g in globs if isinstance(g, str) and g]
    exclude = [g for g in exclude if isinstance(g, str) and g]
    return obs("workspace_declaration", blob, at, {"ecosystem": ecosystem, "tool": tool, "dir": posixpath.dirname(path), "globs": globs, "exclude": exclude}, E1)


class _Manifest:
    basename = ""
    version = "1.3.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path) == self.basename


def _publication_version(raw) -> bool:
    return isinstance(raw, str) and bool(re.fullmatch(r"[0-9][A-Za-z0-9._+!-]{0,63}", raw))


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;]*)")


class PyProject(_Manifest):
    id = "manifest.pyproject"
    basename = "pyproject.toml"

    def extract(self, path, data, blob):
        try:
            doc = tomllib.loads(data.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {exc}")], parsed=False)
        project = doc.get("project")
        uv = doc.get("tool", {}).get("uv", {}).get("workspace") if isinstance(doc.get("tool"), dict) and isinstance(doc["tool"].get("uv"), dict) else None
        members = []
        if isinstance(uv, dict) and isinstance(uv.get("members"), list):
            at = find_span(data, "[tool.uv.workspace]") or whole_file(data)
            members.append(_workspace(blob, at, "pypi", "uv", path, uv["members"], uv.get("exclude") if isinstance(uv.get("exclude"), list) else ()))
        if not isinstance(project, dict) or not isinstance(project.get("name"), str):
            return Extraction(members, diagnostics=[diagnostic(self.id, "info", "no [project] name declared")])
        m = re.search(rb"^name\s*=.*$", data, re.M)
        name_at = span(data, m.start(), m.end()) if m else whole_file(data)
        out = [_package(blob, name_at, "pypi", normalize_pypi(project["name"]), path, {"has_cli_entry": bool(project.get("scripts")), "publishable": _publication_version(project.get("version"))}), *members]
        deps = project.get("dependencies", [])
        for raw in deps if isinstance(deps, list) else []:
            parsed = _REQ.match(raw) if isinstance(raw, str) else None
            if parsed:
                out.append(_dependency(blob, find_span(data, raw) or name_at, "pypi", normalize_pypi(parsed.group(1)), parsed.group(3).strip()))
        return Extraction(out)


class PackageJson(_Manifest):
    id = "manifest.npm"
    basename = "package.json"
    SCOPES = {"dependencies": "runtime", "devDependencies": "dev", "peerDependencies": "peer", "optionalDependencies": "optional"}

    def extract(self, path, data, blob):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {exc}")], parsed=False)
        if not isinstance(doc, dict) or not isinstance(doc.get("name"), str):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no package name declared")])
        name_at = find_span(data, json.dumps(doc["name"])) or whole_file(data)
        scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
        shape = {"is_workspace_root": "workspaces" in doc, "has_cli_entry": "bin" in doc, "has_library_entry": any(k in doc for k in ("main", "module", "exports", "types")), "has_start_script": "start" in scripts, "private": doc.get("private") is True, "publishable": doc.get("private") is not True and _publication_version(doc.get("version"))}
        out = [_package(blob, name_at, "npm", doc["name"], path, shape)]
        workspaces = doc.get("workspaces")
        globs = workspaces.get("packages") if isinstance(workspaces, dict) else workspaces  # yarn: {packages: [...]}
        if isinstance(globs, list):
            out.append(_workspace(blob, find_span(data, '"workspaces"') or name_at, "npm", "npm", path, globs))
        entries = _entry_points(doc)
        if entries or doc.get("exports") is not None:
            out.append(obs("package_entry", blob, find_span(data, '"exports"') or find_span(data, '"main"') or find_span(data, '"types"') or name_at, {"ecosystem": "npm", "name": doc["name"], "manifest_dir": posixpath.dirname(path), "entries": entries, "exports": doc.get("exports") is not None}, E1))
        for section, scope in self.SCOPES.items():
            block = doc.get(section)
            start = max(data.find(f'"{section}"'.encode()), 0)
            for dep, spec in block.items() if isinstance(block, dict) else []:
                if isinstance(spec, str):
                    out.append(_dependency(blob, find_span(data, json.dumps(dep), start) or name_at, "npm", dep, spec, scope))
        return Extraction(out)


# Export conditions in the order a TypeScript-aware resolver would use them.
_CONDITIONS = ("types", "typings", "import", "module", "default", "require", "node", "browser")


def _condition_target(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # fallback arrays: the first usable target
        return next((t for t in map(_condition_target, value) if t), None)
    if isinstance(value, dict):
        for key in _CONDITIONS:
            if key in value:
                found = _condition_target(value[key])
                if found:
                    return found
        return next((t for k, v in sorted(value.items()) if not k.startswith(".") and (t := _condition_target(v))), None)
    return None


def _entry_points(doc: dict) -> dict[str, str]:
    """Public subpath -> target file, from `exports`, else `types` / `module` / `main`.

    "." is the package root; a "./*" pattern keeps its star. Conditions are
    collapsed to one target, preferring type declarations and ES modules.
    """
    exports = doc.get("exports")
    out: dict[str, str] = {}
    if isinstance(exports, (str, list)) or (isinstance(exports, dict) and exports and not any(k.startswith(".") for k in exports)):
        target = _condition_target(exports)
        if target:
            out["."] = target
    elif isinstance(exports, dict):
        for subpath, value in exports.items():
            target = _condition_target(value)
            if isinstance(subpath, str) and subpath.startswith(".") and target:
                out[subpath] = target
    if "." not in out and exports is None:
        for key in ("types", "typings", "module", "main"):
            if isinstance(doc.get(key), str) and doc[key]:
                out["."] = doc[key]
                break
    return out


_NS = re.compile(r"^\{[^}]*\}")


class Pom(_Manifest):
    id = "manifest.maven"
    basename = "pom.xml"

    def extract(self, path, data, blob):
        try:
            root = ET.fromstring(data)  # stdlib parser does not resolve external entities
        except ET.ParseError as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {exc}")], parsed=False)
        child = lambda node, tag: next((c.text.strip() for c in node if _NS.sub("", c.tag) == tag and c.text), None)
        group = child(root, "groupId") or next((child(c, "groupId") for c in root if _NS.sub("", c.tag) == "parent"), None)
        artifact = child(root, "artifactId")
        if not (group and artifact):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no groupId/artifactId declared")])
        name_at = find_span(data, f"<artifactId>{artifact}</artifactId>") or whole_file(data)
        out = [_package(blob, name_at, "maven", f"{group}:{artifact}", path)]
        cursor = name_at.end_byte
        for deps in (c for c in root if _NS.sub("", c.tag) == "dependencies"):
            for d in (c for c in deps if _NS.sub("", c.tag) == "dependency"):
                g, a = child(d, "groupId"), child(d, "artifactId")
                if g and a:
                    at = find_span(data, f"<artifactId>{a}</artifactId>", cursor) or name_at
                    cursor = at.end_byte
                    out.append(_dependency(blob, at, "maven", f"{g}:{a}", child(d, "version") or "", child(d, "scope") or "runtime"))
        return Extraction(out)


_GO_REQ = re.compile(rb"^\s*(?:require\s+)?([\w./~-]+\.[\w./~-]+)\s+(v[\w.+-]+)", re.M)


class GoMod(_Manifest):
    id = "manifest.go"
    basename = "go.mod"

    def extract(self, path, data, blob):
        m = re.search(rb"^module\s+(\S+)", data, re.M)
        if not m:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no module directive")])
        out = [_package(blob, span(data, m.start(), m.end()), "go", m.group(1).decode(), path)]
        for r in _GO_REQ.finditer(data):
            out.append(_dependency(blob, span(data, r.start(1), r.end(2)), "go", r.group(1).decode(), r.group(2).decode()))
        return Extraction(out)


class CargoToml(_Manifest):
    """Inventory only: the crate, its dependencies and a `[workspace]` if any."""

    id = "manifest.cargo"
    basename = "Cargo.toml"
    SCOPES = {"dependencies": "runtime", "dev-dependencies": "dev", "build-dependencies": "build"}

    def extract(self, path, data, blob):
        try:
            doc = tomllib.loads(data.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {exc}")], parsed=False)
        out: list = []
        workspace = doc.get("workspace")
        if isinstance(workspace, dict) and isinstance(workspace.get("members"), list):
            at = find_span(data, "[workspace]") or whole_file(data)
            out.append(_workspace(blob, at, "cargo", "cargo", path, workspace["members"], workspace.get("exclude") if isinstance(workspace.get("exclude"), list) else ()))
        package = doc.get("package")
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            return Extraction(out, diagnostics=[diagnostic(self.id, "info", "no [package] name declared")])
        m = re.search(rb"^name\s*=.*$", data, re.M)
        name_at = span(data, m.start(), m.end()) if m else whole_file(data)
        out.insert(0, _package(blob, name_at, "cargo", package["name"], path, {"publishable": package.get("publish") is not False and _publication_version(package.get("version"))}))
        for section, scope in self.SCOPES.items():
            block = doc.get(section)
            start = max(data.find(f"[{section}]".encode()), 0)
            for dep, spec in sorted(block.items()) if isinstance(block, dict) else []:
                version = spec if isinstance(spec, str) else spec.get("version", "") if isinstance(spec, dict) else ""
                if isinstance(spec, dict) and spec.get("workspace") is True:
                    version = "workspace"
                m = re.compile(rb"^\s*" + re.escape(dep.encode()) + rb"\s*=", re.M).search(data, start)
                at = span(data, m.start(), m.end()) if m else name_at
                out.append(_dependency(blob, at, "cargo", dep, version if isinstance(version, str) else "", scope))
        return Extraction(out)


class PnpmWorkspace(_Manifest):
    id = "manifest.pnpm_workspace"
    basename = "pnpm-workspace.yaml"

    def extract(self, path, data, blob):
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        globs = doc.get("packages") if isinstance(doc, dict) else None
        if not isinstance(globs, list):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no packages list declared")])
        include = [g for g in globs if isinstance(g, str) and not g.startswith("!")]
        exclude = [g[1:] for g in globs if isinstance(g, str) and g.startswith("!")]
        return Extraction([_workspace(blob, find_span(data, "packages") or whole_file(data), "npm", "pnpm", path, include, exclude)])


# ----------------------------------------------------------------- lockfiles


def _strip_peer(version: str) -> str:
    """pnpm writes `5.1.0(react@19.2.6)`; the resolved version is the part before `(`."""
    return version.split("(", 1)[0].strip()


def _lockfile(blob, data, ecosystem, tool, path, **payload):
    return obs("lockfile", blob, whole_file(data), {"ecosystem": ecosystem, "tool": tool, "dir": posixpath.dirname(path), **payload}, E1)


class _Lockfile(_Manifest):
    version = "1.0.0"


class PackageLock(_Lockfile):
    """package-lock.json / npm-shrinkwrap.json, lockfile versions 1 to 3."""

    id = "lockfile.npm"
    basenames = ("package-lock.json", "npm-shrinkwrap.json")

    def matches(self, path):
        return posixpath.basename(path) in self.basenames

    def extract(self, path, data, blob):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        importers: dict[str, dict[str, str]] = {}
        packages = doc.get("packages") if isinstance(doc, dict) else None
        if isinstance(packages, dict):
            for key, entry in packages.items():
                if not isinstance(entry, dict) or "/node_modules/" in f"/{key}/":
                    continue
                declared = {d for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies") if isinstance(entry.get(section), dict) for d in entry[section]}
                found = {}
                for dep in sorted(declared):
                    hit = packages.get(f"{key}/node_modules/{dep}" if key else f"node_modules/{dep}") or packages.get(f"node_modules/{dep}")
                    if isinstance(hit, dict) and not hit.get("link") and isinstance(hit.get("version"), str):
                        found[dep] = hit["version"]
                importers[key or "."] = found
        elif isinstance(doc, dict) and isinstance(doc.get("dependencies"), dict):  # lockfile v1
            importers["."] = {k: v["version"] for k, v in sorted(doc["dependencies"].items()) if isinstance(v, dict) and isinstance(v.get("version"), str) and not v["version"].startswith("file:")}
        else:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "no packages or dependencies in lockfile")], parsed=False)
        return Extraction([_lockfile(blob, data, "npm", "npm", path, importers=importers)])


class PnpmLock(_Lockfile):
    id = "lockfile.pnpm"
    basename = "pnpm-lock.yaml"

    def extract(self, path, data, blob):
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "lockfile is not a mapping")], parsed=False)
        raw = doc.get("importers") if isinstance(doc.get("importers"), dict) else {".": doc}
        importers: dict[str, dict[str, str]] = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            found = {}
            for section in ("dependencies", "devDependencies", "optionalDependencies"):
                for dep, value in entry.get(section, {}).items() if isinstance(entry.get(section), dict) else []:
                    version = value.get("version") if isinstance(value, dict) else value  # v6+: {specifier, version}; v5: version
                    if isinstance(version, (str, int, float)) and not str(version).startswith(("link:", "file:", "workspace:")):
                        found[str(dep)] = _strip_peer(str(version))
            importers[str(key)] = dict(sorted(found.items()))
        return Extraction([_lockfile(blob, data, "npm", "pnpm", path, importers=importers)])


_YARN_HEADER = re.compile(r"^(\S.*):\s*$")
_YARN_VERSION = re.compile(r"^\s+version:?\s+\"?([^\"\s]+)\"?\s*$")


class YarnLock(_Lockfile):
    """yarn.lock, classic (v1) and berry: `name@spec` keys to resolved versions."""

    id = "lockfile.yarn"
    basename = "yarn.lock"

    def extract(self, path, data, blob):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        specs: dict[str, str] = {}
        keys: list[str] = []
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            m = _YARN_HEADER.match(line)
            if m and not line.startswith(" "):
                keys = [k.strip().strip('"') for k in m.group(1).split(",")]
                continue
            v = _YARN_VERSION.match(line)
            if v and keys:
                for key in keys:
                    if "@workspace:" not in key and "@link:" not in key and "@file:" not in key and key != "__metadata":
                        specs[key] = v.group(1)
                keys = []
        return Extraction([_lockfile(blob, data, "npm", "yarn", path, specs=dict(sorted(specs.items())))])


class TomlLock(_Lockfile):
    """poetry.lock, uv.lock and Cargo.lock: `[[package]]` name and version."""

    TOOLS = {"poetry.lock": ("pypi", "poetry"), "uv.lock": ("pypi", "uv"), "Cargo.lock": ("cargo", "cargo")}

    def __init__(self, basename: str):
        self.basename = basename
        self.ecosystem, self.tool = self.TOOLS[basename]
        self.id = f"lockfile.{self.tool}"

    def extract(self, path, data, blob):
        try:
            doc = tomllib.loads(data.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        versions: dict[str, str] = {}
        for entry in doc.get("package", []) if isinstance(doc.get("package"), list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("version"), str):
                continue
            source = entry.get("source")
            if isinstance(source, dict) and ("editable" in source or "virtual" in source or "workspace" in source) or (self.ecosystem == "cargo" and source is None):
                continue  # a project of this repository, not a resolved dependency
            name = normalize_pypi(entry["name"]) if self.ecosystem == "pypi" else entry["name"]
            versions[name] = entry["version"]
        return Extraction([_lockfile(blob, data, self.ecosystem, self.tool, path, versions=dict(sorted(versions.items())))])


EXTRACTORS = [PyProject(), PackageJson(), Pom(), GoMod(), CargoToml(), PnpmWorkspace(), PackageLock(), PnpmLock(), YarnLock(), TomlLock("poetry.lock"), TomlLock("uv.lock"), TomlLock("Cargo.lock")]
_TSCONFIG = re.compile(r"^(tsconfig|jsconfig)(\.[A-Za-z0-9_-]+)?\.json$")


class _ManifestBasenames(frozenset):
    """Basenames `unit.plan` reads: every manifest, workspace file and lockfile, and
    tsconfig/jsconfig files (any `tsconfig.*.json`), whose `extends` chains and
    `paths` cross unit boundaries."""

    def __contains__(self, name: object) -> bool:
        return frozenset.__contains__(self, name) or (isinstance(name, str) and bool(_TSCONFIG.match(name)))


MANIFEST_BASENAMES = _ManifestBasenames(b for e in EXTRACTORS for b in getattr(e, "basenames", (e.basename,)))
