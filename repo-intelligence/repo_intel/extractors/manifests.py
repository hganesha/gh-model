"""Package manifests, workspaces and lockfiles (GI-007).

Manifests: pyproject.toml (PEP 621 or a full Poetry table), setup.py (read with
`ast`, never run), setup.cfg, requirements*.txt, conda environment files,
package.json, pom.xml, build.gradle(.kts) and settings.gradle, build.sbt, .NET project
files (nuget), Gemfile and *.gemspec (rubygems), Package.swift (swiftpm), go.mod,
Cargo.toml. Cargo is inventory-only: its manifest yields a Package and its declared
dependencies, nothing more.

requirements*.txt and conda environment files name no package. Data and ML
repositories often have nothing else, so they declare one named after their
directory, marked `inferred_name`. That declaration is weak: a named declaration
of the same ecosystem in the same directory replaces it (`effective_declarations`),
and its dependencies then belong to the named package.

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

from repo_intel.compile.ontology import EvidenceClass
from repo_intel.extractors.base import Extraction, diagnostic, find_span, obs, span, whole_file

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
            poetry = _poetry(doc)
            if poetry is None:
                return Extraction(members, diagnostics=[diagnostic(self.id, "info", "no [project] name declared")])
            return Extraction(self._poetry(path, data, blob, poetry) + members)
        m = re.search(rb"^name\s*=.*$", data, re.M)
        name_at = span(data, m.start(), m.end()) if m else whole_file(data)
        out = [_package(blob, name_at, "pypi", normalize_pypi(project["name"]), path, {"has_cli_entry": bool(project.get("scripts")), "publishable": _publication_version(project.get("version"))}), *members]
        deps = project.get("dependencies", [])
        for raw in deps if isinstance(deps, list) else []:
            parsed = _REQ.match(raw) if isinstance(raw, str) else None
            if parsed:
                out.append(_dependency(blob, find_span(data, raw) or name_at, "pypi", normalize_pypi(parsed.group(1)), parsed.group(3).strip()))
        return Extraction(out)

    def _poetry(self, path, data, blob, poetry):
        """A Poetry project: `[tool.poetry]` with a name and a version, as Poetry itself requires."""
        m = re.search(rb"^name\s*=.*$", data[max(data.find(b"[tool.poetry]"), 0):], re.M)
        base = max(data.find(b"[tool.poetry]"), 0)
        name_at = span(data, base + m.start(), base + m.end()) if m else whole_file(data)
        out = [_package(blob, name_at, "pypi", normalize_pypi(poetry["name"]), path, {"has_cli_entry": bool(poetry.get("scripts")), "publishable": _publication_version(poetry.get("version")), "manager": "poetry"})]
        sections = [("dependencies", poetry.get("dependencies"), "runtime"), ("dev-dependencies", poetry.get("dev-dependencies"), "dev")]
        groups = poetry.get("group") if isinstance(poetry.get("group"), dict) else {}
        sections += [(f"group.{g}.dependencies", body.get("dependencies"), "dev") for g, body in sorted(groups.items()) if isinstance(body, dict)]
        for section, block, scope in sections:
            start = max(data.find(f"[tool.poetry.{section}]".encode()), 0)
            for dep, spec in sorted(block.items()) if isinstance(block, dict) else []:
                if dep.lower() == "python":
                    continue  # the interpreter, not a dependency
                version = spec if isinstance(spec, str) else spec.get("version", "") if isinstance(spec, dict) else ""
                hit = re.compile(rb"^\s*" + re.escape(dep.encode()) + rb"\s*=", re.M).search(data, start)
                at = span(data, hit.start(), hit.end()) if hit else name_at
                out.append(_dependency(blob, at, "pypi", normalize_pypi(dep), version if isinstance(version, str) else "", scope))
        return out


def _poetry(doc: dict) -> dict | None:
    tool = doc.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict) and isinstance(poetry.get("name"), str) and isinstance(poetry.get("version"), str):
        return poetry
    return None


# ------------------------------------------------------- Python without pyproject


def _infer_name(path: str) -> str:
    """The package name for a manifest that names none: its directory's name."""
    return normalize_pypi(posixpath.basename(posixpath.dirname(path)) or "root")


def _requirement(blob, data, raw, at, scope):
    """One PEP 508 line, or None. URLs and local paths are never copied."""
    line = raw.split(" #", 1)[0].strip()
    if not line or line.startswith(("#", "-", ".", "/")) or "://" in line.split("@", 1)[0]:
        return None
    name_part, _, _url = line.partition(" @ ")
    parsed = _REQ.match(name_part)
    if not parsed:
        return None
    specifier = "" if _url else parsed.group(3).strip()
    return _dependency(blob, at, "pypi", normalize_pypi(parsed.group(1)), specifier, scope)


_REQUIREMENTS = re.compile(r"^requirements([-_.][A-Za-z0-9_.-]+)?\.(txt|in)$")
_DEV_HINT = re.compile(r"dev|test|lint|doc|ci", re.I)


class Requirements(_Manifest):
    """requirements.txt and its variants (requirements-dev.txt, requirements.in)."""

    id = "manifest.requirements"
    version = "1.0.0"

    def matches(self, path):
        return bool(_REQUIREMENTS.match(posixpath.basename(path)))

    def extract(self, path, data, blob):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        suffix = _REQUIREMENTS.match(posixpath.basename(path)).group(1) or ""
        scope = "dev" if _DEV_HINT.search(suffix) else "runtime"
        out = [_package(blob, whole_file(data), "pypi", _infer_name(path), path, {"inferred_name": True})]
        offset = 0
        for line in data.splitlines(keepends=True):
            raw = line.rstrip(b"\r\n")
            if raw.strip():
                dep = _requirement(blob, data, raw.decode("utf-8"), span(data, offset, offset + len(raw)), scope)
                if dep:
                    out.append(dep)
            offset += len(line)
        return Extraction(out)


class SetupPy(_Manifest):
    """setup.py, read as a syntax tree. Only literal arguments count; nothing is run."""

    id = "manifest.setuppy"
    basename = "setup.py"
    version = "1.0.0"

    def extract(self, path, data, blob):
        import ast
        try:
            tree = ast.parse(data.decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {type(exc).__name__}")], parsed=False)
        call = next((n for n in ast.walk(tree) if isinstance(n, ast.Call) and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "setup"), None)
        if call is None:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no setup() call")])
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        name = kw.get("name")
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "setup() has no literal name")])
        lines = data.split(b"\n")
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line) + 1)

        def at(node):
            start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].decode("utf-8", "replace")[: node.col_offset].encode())
            end_line = getattr(node, "end_lineno", node.lineno)
            end = offsets[end_line - 1] + len(lines[end_line - 1].decode("utf-8", "replace")[: getattr(node, "end_col_offset", node.col_offset)].encode())
            return span(data, start, max(end, start))

        version = kw.get("version")
        entry_points = kw.get("entry_points")
        cli = "scripts" in kw or (isinstance(entry_points, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "console_scripts" for k in entry_points.keys)) or (isinstance(entry_points, ast.Constant) and "console_scripts" in str(entry_points.value))
        attrs = {"has_cli_entry": bool(cli), "publishable": isinstance(version, ast.Constant) and _publication_version(version.value)}
        out = [_package(blob, at(name), "pypi", normalize_pypi(name.value), path, attrs)]
        for key, scope in (("install_requires", "runtime"), ("tests_require", "dev"), ("setup_requires", "build")):
            node = kw.get(key)
            for item in node.elts if isinstance(node, (ast.List, ast.Tuple)) else []:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    dep = _requirement(blob, data, item.value, at(item), scope)
                    if dep:
                        out.append(dep)
        return Extraction(out)


class SetupCfg(_Manifest):
    id = "manifest.setupcfg"
    basename = "setup.cfg"
    version = "1.0.0"

    def extract(self, path, data, blob):
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(data.decode("utf-8"))
        except (configparser.Error, UnicodeDecodeError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {type(exc).__name__}")], parsed=False)
        name = parser.get("metadata", "name", fallback="").strip()
        if not name:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no [metadata] name declared")])
        m = re.search(rb"^name\s*=.*$", data, re.M)
        name_at = span(data, m.start(), m.end()) if m else whole_file(data)
        cli = parser.has_option("options.entry_points", "console_scripts") or parser.has_option("options", "scripts")
        out = [_package(blob, name_at, "pypi", normalize_pypi(name), path, {"has_cli_entry": cli, "publishable": _publication_version(parser.get("metadata", "version", fallback="").strip())})]
        start = max(data.find(b"install_requires"), 0)
        for raw in parser.get("options", "install_requires", fallback="").splitlines():
            if raw.strip():
                dep = _requirement(blob, data, raw.strip(), find_span(data, raw.strip(), start) or name_at, "runtime")
                if dep:
                    out.append(dep)
        return Extraction(out)


_CONDA_SPEC = re.compile(r"^(?:[\w.-]+::)?([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")


class CondaEnvironment(_Manifest):
    """environment.yml and conda.yaml: conda packages (ecosystem `conda`) and pip ones."""

    id = "manifest.conda"
    version = "1.0.0"
    basenames = ("environment.yml", "environment.yaml", "conda.yaml", "conda.yml")

    def matches(self, path):
        return posixpath.basename(path) in self.basenames

    def extract(self, path, data, blob):
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        deps = doc.get("dependencies") if isinstance(doc, dict) else None
        if not isinstance(deps, list):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no dependencies list")])
        out = [_package(blob, find_span(data, "dependencies") or whole_file(data), "pypi", _infer_name(path), path, {"inferred_name": True, "manager": "conda"})]
        cursor = 0
        for item in deps:
            if isinstance(item, str):
                m = _CONDA_SPEC.match(item.strip())
                if not m or m.group(1).lower() in ("python", "pip"):
                    continue
                at = find_span(data, item, cursor) or whole_file(data)
                cursor = at.end_byte
                spec = m.group(2).strip()
                out.append(_dependency(blob, at, "conda", normalize_pypi(m.group(1)), spec if spec.startswith(("=", "<", ">", "!", "~")) else ("==" + spec if spec else ""), "runtime"))
            elif isinstance(item, dict) and isinstance(item.get("pip"), list):
                for raw in item["pip"]:
                    if isinstance(raw, str):
                        at = find_span(data, raw, cursor) or whole_file(data)
                        cursor = at.end_byte
                        dep = _requirement(blob, data, raw, at, "runtime")
                        if dep:
                            out.append(dep)
        return Extraction(out)


class DbtProject(_Manifest):
    """dbt_project.yml: a dbt project is a package of the `dbt` ecosystem."""

    id = "manifest.dbt"
    basename = "dbt_project.yml"
    version = "1.0.0"

    def extract(self, path, data, blob):
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        name = doc.get("name") if isinstance(doc, dict) else None
        if not isinstance(name, str) or not name:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no project name declared")])
        m = re.search(rb"^name\s*:.*$", data, re.M)
        at = span(data, m.start(), m.end()) if m else whole_file(data)
        return Extraction([_package(blob, at, "dbt", name, path, {"kind": "dbt_project"})])


def effective_declarations(declarations: list[tuple[str, dict]]) -> tuple[list[tuple[str, dict]], dict[str, dict]]:
    """Package declarations after weak ones give way (see the module docstring).

    `declarations` is (manifest path, package_declaration observation). Returns the
    declarations that stand, and for each manifest whose declaration gave way, the
    payload of the one it gave way to: a named declaration of the same ecosystem in
    the same directory, else the first inferred one by path.
    """
    weak = lambda o: bool(o["payload"].get("attributes", {}).get("inferred_name"))
    winner: dict[tuple[str, str], dict] = {}
    for path, o in sorted(declarations, key=lambda x: (weak(x[1]), x[0])):
        winner.setdefault((o["payload"]["ecosystem"], o["payload"]["manifest_dir"]), o)
    kept, redirect = [], {}
    for path, o in declarations:
        w = winner[(o["payload"]["ecosystem"], o["payload"]["manifest_dir"])]
        if not weak(o) or w is o:
            kept.append((path, o))
        else:
            redirect[path] = w["payload"]
    return kept, redirect


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
    """pom.xml: the artifact, its dependencies and, for a `pom`-packaged parent, its
    `<modules>` (a workspace). A `jar` with no plugin that makes it runnable is an
    importable library; Spring Boot, Quarkus, Micronaut and exec plugins make it a
    program."""

    id = "manifest.maven"
    version = "1.4.0"
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
        packaging = child(root, "packaging") or "jar"
        modules = [m.text.strip() for c in root if _NS.sub("", c.tag) == "modules" for m in c if _NS.sub("", m.tag) == "module" and m.text]
        plugins = {child(p, "artifactId") for b in root if _NS.sub("", b.tag) == "build" for ps in b if _NS.sub("", ps.tag) == "plugins" for p in ps}
        runnable = bool(plugins & {"spring-boot-maven-plugin", "quarkus-maven-plugin", "micronaut-maven-plugin", "exec-maven-plugin", "appassembler-maven-plugin"})
        shape = {"build_tool": "maven", "packaging": packaging, "is_workspace_root": packaging == "pom" and bool(modules),
                 "has_library_entry": packaging == "jar" and not runnable, "has_start_script": runnable or packaging == "war"}
        out = [_package(blob, name_at, "maven", f"{group}:{artifact}", path, shape)]
        if modules:
            out.append(_workspace(blob, find_span(data, "<modules>") or name_at, "maven", "maven", path, modules))
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


# ------------------------------------------------- JVM, .NET, Ruby and Swift
#
# Build scripts in Groovy, Kotlin, Scala, Ruby and Swift are programs; they are
# read with patterns for the literal forms only (`implementation "g:a:v"`,
# `gem "x"`, `.package(url: ...)`), never run. Anything computed is skipped.


def _strip_line_comments(text: str) -> str:
    """`//` and `#`-free text for pattern matching, with offsets kept."""
    return re.sub(r"(?m)(?<![:\"'])//[^\n]*", lambda m: " " * len(m.group()), text)


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


_GRADLE_DEP = re.compile(r"""(?m)^\s*(implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|kapt|ksp|annotationProcessor|developmentOnly)\s*\(?\s*(?:platform\(\s*)?["']([\w.-]+):([\w.-]+)(?::([\w.+-]+))?["']""")
_GRADLE_PROJECT = re.compile(r"""(?m)^\s*(implementation|api|compileOnly|runtimeOnly|testImplementation)\s*\(?\s*project\(\s*(?:path\s*[:=]\s*)?["']:?([\w:.-]+)["']""")
_GRADLE_SCOPES = {"testImplementation": "dev", "testRuntimeOnly": "dev", "compileOnly": "build", "kapt": "build", "ksp": "build", "annotationProcessor": "build", "developmentOnly": "dev"}
_GRADLE_PLUGIN = re.compile(r"""\bid\s*\(?\s*["']([\w.-]+)["']|\bapply\s+plugin\s*:\s*["']([\w.-]+)["']|(?m:^|\{)\s*(application|`java-library`|java-library)\b""")
_GRADLE_GROUP = re.compile(r"""(?m)^\s*group\s*=\s*["']([\w.-]+)["']""")


class Gradle(_Manifest):
    """build.gradle / build.gradle.kts: a Gradle project, named after its directory as
    Gradle does (settings.gradle may rename the root). Dependencies are Maven
    coordinates; `project(":lib")` is a dependency on the workspace project `lib`."""

    id = "manifest.gradle"
    version = "1.0.0"
    basenames = ("build.gradle", "build.gradle.kts")

    def matches(self, path):
        return posixpath.basename(path) in self.basenames

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        text = _strip_line_comments(raw)
        plugins = {m.group(1) or m.group(2) or m.group(3).strip("`") for m in _GRADLE_PLUGIN.finditer(text)}
        group = _GRADLE_GROUP.search(text)
        shape = {"inferred_name": True, "build_tool": "gradle", "has_cli_entry": "application" in plugins, "has_library_entry": "java-library" in plugins,
                 "has_start_script": bool(plugins & {"org.springframework.boot", "io.ktor.plugin", "io.micronaut.application", "io.quarkus"}), **({"group": group.group(1)} if group else {})}
        out = [_package(blob, whole_file(data), "maven", posixpath.basename(posixpath.dirname(path)) or "root", path, shape)]
        for m in _GRADLE_DEP.finditer(text):
            out.append(_dependency(blob, span(data, len(raw[:m.start(2)].encode()), len(raw[:m.end(3)].encode())), "maven", f"{m.group(2)}:{m.group(3)}", m.group(4) or "", _GRADLE_SCOPES.get(m.group(1), "runtime")))
        for m in _GRADLE_PROJECT.finditer(text):
            out.append(_dependency(blob, span(data, len(raw[:m.start(2)].encode()), len(raw[:m.end(2)].encode())), "maven", m.group(2).rsplit(":", 1)[-1], "workspace", _GRADLE_SCOPES.get(m.group(1), "runtime")))
        return Extraction(out)


_GRADLE_INCLUDE = re.compile(r"""(?m)^\s*include\s*\(?\s*((?:["'][\w:.-]+["']\s*,?\s*)+)\)?""")
_GRADLE_ROOT = re.compile(r"""(?m)^\s*rootProject\.name\s*=\s*["']([\w.-]+)["']""")


class GradleSettings(_Manifest):
    """settings.gradle(.kts): the root project's name and the included projects (a workspace)."""

    id = "manifest.gradle_settings"
    version = "1.0.0"
    basenames = ("settings.gradle", "settings.gradle.kts")

    def matches(self, path):
        return posixpath.basename(path) in self.basenames

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        text = _strip_line_comments(raw)
        out: list = []
        root = _GRADLE_ROOT.search(text)
        members = [p.strip(":").replace(":", "/") for m in _GRADLE_INCLUDE.finditer(text) for p in re.findall(r"""["']([\w:.-]+)["']""", m.group(1))]
        if root:
            at = span(data, len(raw[:root.start(1)].encode()), len(raw[:root.end(1)].encode()))
            out.append(_package(blob, at, "maven", root.group(1), path, {"build_tool": "gradle", "is_workspace_root": bool(members)}))
        if members:
            out.append(_workspace(blob, find_span(data, "include") or whole_file(data), "maven", "gradle", path, members))
        return Extraction(out)


_SBT_SETTING = re.compile(r"""(?m)^\s*(?:ThisBuild\s*/\s*)?(name|organization)\s*:=\s*"([\w.-]+)\"""")
_SBT_DEP = re.compile(r""""([\w.-]+)"\s*(%%%?|%)\s*"([\w.-]+)"\s*%\s*(?:"([\w.+-]+)"|\w+)(?:\s*%\s*"?(Test|test|Provided|provided)"?)?""")
_SBT_PROJECT = re.compile(r"""(?m)lazy\s+val\s+(\w+)\s*=\s*(?:\(\s*)?project(?:\s*\.in\(\s*file\(\s*"([\w./-]+)"\s*\)\s*\)|\s*\)?\s*\.in\(\s*file\(\s*"([\w./-]+)"\s*\)\s*\))?""")


class Sbt(_Manifest):
    """build.sbt: the project (organization:name), Maven-coordinate dependencies and
    `lazy val x = project.in(file("x"))` subprojects as a workspace."""

    id = "manifest.sbt"
    version = "1.0.0"
    basename = "build.sbt"

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        text = _strip_line_comments(raw)
        settings = {m.group(1): m for m in _SBT_SETTING.finditer(text)}
        name = settings.get("name")
        out: list = []
        members = sorted({(m.group(2) or m.group(3) or m.group(1)) for m in _SBT_PROJECT.finditer(text)} - {"."})
        if name:
            at = span(data, len(raw[:name.start(2)].encode()), len(raw[:name.end(2)].encode()))
            out.append(_package(blob, at, "maven", name.group(2), path, {"build_tool": "sbt", "is_workspace_root": bool(members), **({"group": settings["organization"].group(2)} if "organization" in settings else {})}))
        else:
            out.append(_package(blob, whole_file(data), "maven", posixpath.basename(posixpath.dirname(path)) or "root", path, {"build_tool": "sbt", "inferred_name": True, "is_workspace_root": bool(members)}))
        for m in _SBT_DEP.finditer(text):
            scope = "dev" if (m.group(5) or "").lower() == "test" else "runtime"  # Provided: supplied by the cluster at run time
            out.append(_dependency(blob, span(data, len(raw[:m.start()].encode()), len(raw[:m.end(3) + 1].encode())), "maven", f"{m.group(1)}:{m.group(3)}", m.group(4) or "", scope))
        if members:
            out.append(_workspace(blob, find_span(data, "lazy val") or whole_file(data), "maven", "sbt", path, members))
        return Extraction(out)


_MSBUILD_PROJECT = re.compile(r"\.(cs|fs|vb)proj$")


class MsBuildProject(_Manifest):
    """A .NET project file: the assembly, its NuGet packages and its project references.
    The Web SDK brings ASP.NET Core with it, so a Web project depends on
    Microsoft.AspNetCore.App though no PackageReference says so."""

    id = "manifest.nuget"
    version = "1.0.0"
    basename = ""

    def matches(self, path):
        return bool(_MSBUILD_PROJECT.search(path))

    def extract(self, path, data, blob):
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "error", f"unparseable: {exc}")], parsed=False)
        tag = lambda n: _NS.sub("", n.tag)
        props = {tag(c): (c.text or "").strip() for g in root if tag(g) == "PropertyGroup" for c in g}
        stem = posixpath.splitext(posixpath.basename(path))[0]
        name = props.get("PackageId") or props.get("AssemblyName") or stem
        if "$(" in name:
            name = stem
        sdk = root.get("Sdk") or ""
        output = props.get("OutputType", "").lower()
        shape = {"build_tool": "msbuild", "sdk": sdk, "has_cli_entry": output == "exe" and not sdk.endswith(".Web"), "has_library_entry": output in ("", "library") and not sdk.endswith((".Web", ".Worker")),
                 "has_start_script": sdk.endswith((".Web", ".Worker")), "publishable": props.get("IsPackable", "").lower() == "true"}
        if props.get("TargetFramework") or props.get("TargetFrameworks"):
            shape["target_framework"] = props.get("TargetFramework") or props.get("TargetFrameworks")
        at = find_span(data, f"<PackageId>{name}") or find_span(data, f"<AssemblyName>{name}") or find_span(data, "<Project") or whole_file(data)
        out = [_package(blob, at, "nuget", name, path, shape)]
        if sdk.endswith(".Web"):
            out.append(_dependency(blob, find_span(data, sdk) or at, "nuget", "Microsoft.AspNetCore.App", "sdk", "runtime"))
        for item in (c for g in root if tag(g) == "ItemGroup" for c in g):
            include = item.get("Include") or item.get("Update")
            if not include:
                continue
            if tag(item) in ("PackageReference", "FrameworkReference"):
                version = item.get("Version") or next((c.text or "" for c in item if tag(c) == "Version"), "")
                scope = "dev" if (item.get("PrivateAssets") or "").lower() == "all" else "runtime"
                out.append(_dependency(blob, find_span(data, f'"{include}"') or at, "nuget", include, version.strip(), scope))
            elif tag(item) == "ProjectReference":
                ref_name = posixpath.splitext(posixpath.basename(include.replace("\\", "/")))[0]
                out.append(_dependency(blob, find_span(data, include) or at, "nuget", ref_name, "workspace", "runtime"))
        return Extraction(out)


_GEM = re.compile(r"""(?m)^\s*gem\s+["']([\w.-]+)["'](?:\s*,\s*["']([^"'\n]+)["'])?""")
_GEM_GROUP = re.compile(r"(?m)^\s*group\s+([^\n]*?)\s+do\s*$")


class Gemfile(_Manifest):
    """Gemfile: Bundler dependencies. A Gemfile names no gem, so like requirements.txt
    the package is named after its directory unless a gemspec there names it."""

    id = "manifest.gemfile"
    version = "1.0.0"
    basename = "Gemfile"

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        text = re.sub(r"(?m)#[^\n]*", lambda m: " " * len(m.group()), raw)
        dev_ranges = []
        for g in _GEM_GROUP.finditer(text):
            if not re.search(r":(development|test)\b", g.group(1)) or re.search(r":production\b", g.group(1)):
                continue
            depth, end = 1, len(text)
            for tok in re.finditer(r"(?m)^\s*(?:end\b|.*\bdo\b\s*(?:\|[^|]*\|)?\s*$)", text[g.end():]):
                depth += -1 if tok.group().strip() == "end" else 1
                if depth == 0:
                    end = g.end() + tok.start()
                    break
            dev_ranges.append((g.end(), end))
        out = [_package(blob, whole_file(data), "rubygems", posixpath.basename(posixpath.dirname(path)) or "root", path, {"inferred_name": True, "build_tool": "bundler"})]
        for m in _GEM.finditer(text):
            inline_dev = re.search(r"group:\s*\[?\s*:(development|test)", text[m.end(): text.find("\n", m.end())])
            scope = "dev" if inline_dev or any(a <= m.start() < b for a, b in dev_ranges) else "runtime"
            out.append(_dependency(blob, span(data, len(raw[:m.start(1)].encode()), len(raw[:m.end(1)].encode())), "rubygems", m.group(1), m.group(2) or "", scope))
        return Extraction(out)


_GEMSPEC_NAME = re.compile(r"""(?m)^\s*\w+\.name\s*=\s*["']([\w.-]+)["']""")
_GEMSPEC_DEP = re.compile(r"""(?m)^\s*\w+\.add_(runtime_|development_)?dependency\s*\(?\s*["']([\w.-]+)["'](?:\s*,\s*["']([^"'\n]+)["'])?""")


class Gemspec(_Manifest):
    id = "manifest.gemspec"
    version = "1.0.0"
    basename = ""

    def matches(self, path):
        return path.endswith(".gemspec")

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        name = _GEMSPEC_NAME.search(raw)
        if not name:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no literal gem name")])
        at = span(data, len(raw[:name.start(1)].encode()), len(raw[:name.end(1)].encode()))
        out = [_package(blob, at, "rubygems", name.group(1), path, {"build_tool": "gem", "has_library_entry": True, "has_cli_entry": ".executables" in raw})]
        for m in _GEMSPEC_DEP.finditer(raw):
            out.append(_dependency(blob, span(data, len(raw[:m.start(2)].encode()), len(raw[:m.end(2)].encode())), "rubygems", m.group(2), m.group(3) or "", "dev" if m.group(1) == "development_" else "runtime"))
        return Extraction(out)


_SWIFT_NAME = re.compile(r"""Package\s*\(\s*name\s*:\s*"([\w.-]+)\"""")
_SWIFT_DEP = re.compile(r"""\.package\(\s*(?:name\s*:\s*"[^"]*"\s*,\s*)?(url|path)\s*:\s*"([^"]+)"(?:\s*,\s*(?:from|exact|branch|revision)\s*:\s*"([^"]+)")?""")
_SWIFT_PRODUCT = re.compile(r"""\.(executable|library)\s*\(""")


class SwiftPackage(_Manifest):
    """Package.swift: the package and its dependencies, named by the last part of
    their URL (`https://github.com/vapor/vapor.git` is `vapor`)."""

    id = "manifest.swiftpm"
    version = "1.0.0"
    basename = "Package.swift"

    def extract(self, path, data, blob):
        raw = _text(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        text = _strip_line_comments(raw)
        name = _SWIFT_NAME.search(text)
        if not name:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no literal package name")])
        products = {m.group(1) for m in _SWIFT_PRODUCT.finditer(text)}
        at = span(data, len(raw[:name.start(1)].encode()), len(raw[:name.end(1)].encode()))
        out = [_package(blob, at, "swiftpm", name.group(1), path, {"build_tool": "swiftpm", "has_cli_entry": ("executable" in products or ".executableTarget" in text) and "Vapor" not in text, "has_library_entry": "library" in products, "has_start_script": ".executableTarget" in text and "Vapor" in text})]
        for m in _SWIFT_DEP.finditer(text):
            source = m.group(2).rstrip("/")
            dep = posixpath.basename(source[:-4] if source.endswith(".git") else source)
            if m.group(1) == "path":
                out.append(_dependency(blob, span(data, len(raw[:m.start(2)].encode()), len(raw[:m.end(2)].encode())), "swiftpm", dep, "workspace"))
            elif re.fullmatch(r"[\w.-]+", dep):
                out.append(_dependency(blob, span(data, len(raw[:m.start(2)].encode()), len(raw[:m.end(2)].encode())), "swiftpm", dep.lower(), m.group(3) or ""))
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


EXTRACTORS = [PyProject(), SetupPy(), SetupCfg(), Requirements(), CondaEnvironment(), DbtProject(), PackageJson(), Pom(), GoMod(), CargoToml(),
              Gradle(), GradleSettings(), Sbt(), MsBuildProject(), Gemfile(), Gemspec(), SwiftPackage(), PnpmWorkspace(), PackageLock(), PnpmLock(), YarnLock(), TomlLock("poetry.lock"), TomlLock("uv.lock"), TomlLock("Cargo.lock")]
_TSCONFIG = re.compile(r"^(tsconfig|jsconfig)(\.[A-Za-z0-9_-]+)?\.json$")


class _ManifestBasenames(frozenset):
    """Basenames `unit.plan` reads: every manifest, workspace file and lockfile, and
    tsconfig/jsconfig files (any `tsconfig.*.json`), whose `extends` chains and
    `paths` cross unit boundaries."""

    def __contains__(self, name: object) -> bool:
        return frozenset.__contains__(self, name) or (isinstance(name, str) and bool(_TSCONFIG.match(name) or _REQUIREMENTS.match(name) or _MSBUILD_PROJECT.search(name) or name.endswith(".gemspec")))


MANIFEST_BASENAMES = _ManifestBasenames(b for e in EXTRACTORS for b in getattr(e, "basenames", (e.basename,)) if b)
