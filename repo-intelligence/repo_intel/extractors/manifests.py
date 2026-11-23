"""Package manifests: pyproject.toml, package.json, pom.xml, go.mod (GI-007).

Maven and Go are inventory-only languages in v1: their manifests yield a
Package and its declared dependencies, nothing more.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
import xml.etree.ElementTree as ET

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


class _Manifest:
    basename = ""
    version = "1.2.0"

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
        if not isinstance(project, dict) or not isinstance(project.get("name"), str):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no [project] name declared")])
        m = re.search(rb"^name\s*=.*$", data, re.M)
        name_at = span(data, m.start(), m.end()) if m else whole_file(data)
        out = [_package(blob, name_at, "pypi", normalize_pypi(project["name"]), path, {"has_cli_entry": bool(project.get("scripts")), "publishable": _publication_version(project.get("version"))})]
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
        for section, scope in self.SCOPES.items():
            block = doc.get(section)
            start = max(data.find(f'"{section}"'.encode()), 0)
            for dep, spec in block.items() if isinstance(block, dict) else []:
                if isinstance(spec, str):
                    out.append(_dependency(blob, find_span(data, json.dumps(dep), start) or name_at, "npm", dep, spec, scope))
        return Extraction(out)


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


EXTRACTORS = [PyProject(), PackageJson(), Pom(), GoMod()]
MANIFEST_BASENAMES = {e.basename for e in EXTRACTORS}
