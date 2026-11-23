"""`file.extract`: run every matching extractor over one file (spec §5, Stage 1).

Extractors are discovered from the modules in this package: each module that
defines `EXTRACTORS = [...]` contributes its instances. The step version is
derived from every extractor's id and version, so changing one extractor
changes the toolchain hash and invalidates exactly the cached extractions.

A failing extractor yields a diagnostic and a coverage gap, never a failed
compile.
"""

from __future__ import annotations

import importlib
import pkgutil
import posixpath
from collections.abc import Mapping
from typing import Any

from ..buildgraph import Step, StepInputs
from ..contracts import FileSummary
from ..hashing import digest, sha256_hex
from ..ontology import EvidenceClass
from .base import Extraction, Extractor, diagnostic, obs, whole_file
from .manifests import MANIFEST_BASENAMES

LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".toml": "toml", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".sql": "sql", ".prisma": "prisma",
    ".java": "java", ".go": "go", ".tf": "terraform", ".rb": "ruby", ".sh": "shell", ".xml": "xml", ".ini": "ini",
}
BASENAMES = {"Dockerfile": "dockerfile", "pyproject.toml": "pyproject", "package.json": "npm-manifest", "pom.xml": "maven-manifest", "go.mod": "go-manifest"}


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
        for extractor in (e for e in EXTRACTORS if e.matches(path)):
            try:
                result = extractor.extract(path, data, blob)
            except Exception as exc:  # an extractor bug must not fail the compile
                result = Extraction(diagnostics=[diagnostic(extractor.id, "error", f"extractor crashed: {type(exc).__name__}: {exc}")], parsed=False)
            observations += result.observations
            diagnostics += result.diagnostics
            parsed = parsed or result.parsed
    interface = [o.row() for o in observations if o.kind != "file"]
    return FileSummary(path=path, blob_sha256=blob, language=detect_language(path), parsed=parsed, observations=observations, diagnostics=diagnostics, interface_hash=digest(interface), body_hash="sha256:" + blob).row()


FILE_EXTRACT = Step("file.extract", digest([[e.id, e.version] for e in EXTRACTORS]).split(":")[1][:16], _file_extract)

__all__ = ["EXTRACTORS", "FILE_EXTRACT", "MANIFEST_BASENAMES", "detect_language"]
