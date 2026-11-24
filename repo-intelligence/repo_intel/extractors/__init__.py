"""`file.extract`: run every matching extractor over one file (spec §5, Stage 1).

Extractors are discovered from the modules in this package: each module that
defines `EXTRACTORS = [...]` contributes its instances. The step version is
Each extractor has its own version: its declared version plus a digest of its
code (`extractor_versions()`). A file's cache key includes the versions of only
the extractors that apply to it, carried in the step config, so changing one
extractor re-extracts only the files it matches. The reported `file.extract`
version still folds in every extractor, so the toolchain hash moves with any of
them.

A failing extractor yields a diagnostic and a coverage gap, never a failed
compile.
"""

from __future__ import annotations

import importlib
import pkgutil
import posixpath
from collections.abc import Mapping
from typing import Any

from repo_intel.ingest import secrets
from repo_intel.compile.buildgraph import Ref, Step, StepInputs, StepRunner, code_digest
from repo_intel.compile.contracts import FileSummary
from repo_intel.storage.hashing import digest, sha256_hex
from repo_intel.compile.ontology import EvidenceClass
from repo_intel.extractors.base import Extraction, Extractor, diagnostic, obs, whole_file
from repo_intel.extractors.manifests import MANIFEST_BASENAMES

LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".toml": "toml", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".sql": "sql", ".prisma": "prisma",
    ".java": "java", ".go": "go", ".tf": "terraform", ".rb": "ruby", ".sh": "shell", ".xml": "xml", ".ini": "ini",
    # Data and ML work, and legacy platforms (spec §8.1).
    ".ipynb": "jupyter", ".scala": "scala", ".r": "r", ".cfg": "ini",
    ".cbl": "cobol", ".cob": "cobol", ".cobol": "cobol", ".cpy": "cobol", ".jcl": "jcl", ".prc": "jcl", ".proc": "jcl",
    ".sas": "sas", ".cfm": "cfml", ".cfc": "cfml", ".bteq": "bteq", ".btq": "bteq", ".tpt": "tpt",
}
BASENAMES = {"Dockerfile": "dockerfile", "pyproject.toml": "pyproject", "package.json": "npm-manifest", "pom.xml": "maven-manifest", "go.mod": "go-manifest", "MLproject": "yaml"}


def detect_language(path: str) -> str | None:
    base = posixpath.basename(path)
    return BASENAMES.get(base) or LANGUAGES.get(posixpath.splitext(base)[1].lower())


def _discover() -> list[Extractor]:
    found: list[Extractor] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        found += list(getattr(module, "EXTRACTORS", []))
    ids = [e.id for e in found]
    assert len(ids) == len(set(ids)), f"duplicate extractor id in {ids}"
    return sorted(found, key=lambda e: e.id)


EXTRACTORS: list[Extractor] = _discover()
_EXTRACTOR_VERSIONS: dict[tuple[str, str, type], str] = {}


def extractor_version(extractor: Extractor) -> str:
    """Declared version plus a digest of the extractor's code."""
    key = (extractor.id, extractor.version, type(extractor))
    if key not in _EXTRACTOR_VERSIONS:
        _EXTRACTOR_VERSIONS[key] = f"{extractor.version}+{code_digest(type(extractor))[:12]}"
    return _EXTRACTOR_VERSIONS[key]


def extractor_versions() -> dict[str, str]:
    """Every extractor's version, by id. Recorded in the toolchain manifest for delta attribution."""
    return {e.id: extractor_version(e) for e in EXTRACTORS}


def applicable_extractors(path: str, disabled: set[str] | None = None) -> dict[str, str]:
    """The extractors that run on `path`, with their versions."""
    return {e.id: extractor_version(e) for e in EXTRACTORS if not (disabled and e.id in disabled) and e.matches(path)}


def _file_extract(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    path: str = inputs["path"]
    data: bytes = inputs["blob"]
    blob = sha256_hex(data)
    observations = [obs("file", blob, whole_file(data), {"language": detect_language(path), "size": len(data)}, EvidenceClass.E2)]
    diagnostics = []
    parsed = False
    if not config.get("parse", True):
        diagnostics.append(diagnostic("inventory", "warning", "oversize: inventoried, not parsed"))
    else:
        data = secrets.mask_for_extraction(data, config.get("secret_findings", []))
        wanted: dict[str, str] = config.get("extractors", {})
        for extractor in (e for e in EXTRACTORS if e.id in wanted):
            if extractor_version(extractor) != wanted[extractor.id]:
                raise RuntimeError(f"extractor skew: {extractor.id} is {extractor_version(extractor)}, the cache key expects {wanted[extractor.id]}")
            try:
                result = extractor.extract(path, data, blob)
            except Exception as exc:  # an extractor bug must not fail the compile
                result = Extraction(diagnostics=[diagnostic(extractor.id, "error", f"extractor crashed: {type(exc).__name__}: {exc}")], parsed=False)
            observations += result.observations
            diagnostics += result.diagnostics
            parsed = parsed or result.parsed
    interface = [o.row() for o in observations if o.kind != "file"]
    return FileSummary(path=path, blob_sha256=blob, language=detect_language(path), parsed=parsed, observations=observations, diagnostics=diagnostics, interface_hash=digest(interface), body_hash="sha256:" + blob).row()


# The extractor list is versioned per extractor, through the config, not through this step's code.
FILE_EXTRACT = Step("file.extract", "1.0.0", _file_extract, toolchain_extra=extractor_versions, version_skip=frozenset({"EXTRACTORS"}))


def extract_file(runner: StepRunner, entry: dict[str, Any], disabled: set[str] | None = None) -> Ref:
    path = entry["path"]
    config: dict[str, Any] = {"parse": entry["parse"], "extractors": applicable_extractors(path, disabled) if entry["parse"] else {}}
    if entry["parse"] and entry.get("secret_findings"):
        config["secret_findings"] = entry["secret_findings"]
    return runner.run(FILE_EXTRACT, {"path": path, "blob": Ref("sha256:" + entry["blob_sha256"], "bytes")}, config)


__all__ = ["EXTRACTORS", "FILE_EXTRACT", "MANIFEST_BASENAMES", "applicable_extractors", "detect_language", "extract_file", "extractor_version", "extractor_versions"]
