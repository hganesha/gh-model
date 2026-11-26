"""Owner controls over `.rkp/documents.json` (contract 9.1), shared by the local API
(`GET`/`PUT repositories/{id}/documents`) and `rkp documents list|set`.

The listing is `{documents, candidates, problems, version}`:

- `documents`: the file's usable entries, sorted by path;
- `candidates`: `{path, language}` of every file the last compile inventoried whose
  language is a document language (`.rkp/` itself left out), read from the bundle's
  `files.jsonl`, so excluded PDFs are offered too; empty before the first compile;
- `problems`: `{path, message}` for every entry the next compile would ignore, and for
  every entry matching no inventoried file;
- `version`: a digest of the file's bytes (0 when there is none), for optimistic saves.

Writing validates the whole document first and replaces the file atomically. A change
takes effect at the next compile, since the file is a compile input.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from repo_intel.documents import format as fmt

LANGUAGES = ("markdown", "text", "csv", "html", "docx", "xlsx", "pdf")


class StaleDesignations(RuntimeError):
    """The file changed since the caller read it."""


def designation_file(repo: Path) -> Path:
    return repo / fmt.DESIGNATION_FILE


def version(repo: Path) -> int:
    try:
        data = designation_file(repo).read_bytes()
    except FileNotFoundError:
        return 0
    return int(hashlib.sha256(data).hexdigest()[:13], 16) or 1


def _inventory(repo: Path) -> list[dict[str, Any]] | None:
    """The last compile's files, or None when nothing has been compiled here."""
    path = repo / ".rkp" / "knowledge" / "files.jsonl"
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return None


def candidates(files: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    from repo_intel.extractors import detect_language

    out = []
    for f in files or []:
        if f["path"].startswith(".rkp/"):
            continue
        language = detect_language(f["path"])
        if language in LANGUAGES:
            out.append({"path": f["path"], "language": language})
    return sorted(out, key=lambda c: c["path"])


def read(repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """(usable entries, problems, whether the file can be rewritten without losing anything
    it holds: absent, or a readable `rkp-documents/1` document)."""
    try:
        data = designation_file(repo).read_bytes()
    except FileNotFoundError:
        return [], [], True
    doc, problems = fmt.parse(data)
    if doc is None:
        return [], problems, False
    entries, problems = fmt.load_document(doc)
    return entries, problems, doc.get("schema") == fmt.SCHEMA and isinstance(doc.get("documents"), list)


def listing(repo: Path) -> dict[str, Any]:
    entries, problems, _ = read(repo)
    files = _inventory(repo)
    if files is not None:
        _, unmatched = fmt.resolve(entries, [f["path"] for f in files if f["path"] != fmt.DESIGNATION_FILE])
        problems = problems + [{**p, "message": p["message"].replace("in this commit", "in the last compile")} for p in unmatched]
    return {"documents": fmt.canonical(entries)["documents"], "candidates": candidates(files), "problems": problems, "version": version(repo)}


def save(repo: Path, body: Any) -> dict[str, Any]:
    """Validate `{documents: [...], expected_version?}` and write the file; the new listing.
    DesignationError when the body is not acceptable, StaleDesignations when
    `expected_version` is given and the file changed since."""
    entries = fmt.validate_for_save(body)
    expected = body.get("expected_version") if isinstance(body, dict) else None
    if expected is not None and expected != version(repo):
        raise StaleDesignations("The designations changed since they were read; reload and try again.")
    _write(repo, entries)
    return listing(repo)


def _write(repo: Path, entries: list[dict[str, Any]]) -> None:
    path = designation_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(fmt.dump(entries))
    os.replace(tmp, path)


def set_document(repo: Path, path: str, features: list[str] | None, note: str | None = None) -> dict[str, Any]:
    """Designate `path` (a repository path or glob) for exactly `features`, or with
    `features` None remove its entry. Other usable entries are kept as they are; entries
    the compile would ignore (an unknown feature, a path outside the repository) are not
    written back."""
    target = fmt.normalize_path(path)
    if target is None:
        raise fmt.DesignationError(f"{path}: give a repository path or glob, relative and inside the repository")
    entries, _, rewritable = read(repo)
    if not rewritable:
        raise fmt.DesignationError(f"{fmt.DESIGNATION_FILE} cannot be read as {fmt.SCHEMA}; fix or remove it first")
    kept = [e for e in entries if e["path"] != target]
    if features is not None:
        old = next((e for e in entries if e["path"] == target), None)
        entry = {"path": target, "features": list(features)}
        note = note if note is not None else (old or {}).get("note")
        if note:
            entry["note"] = note
        kept.append(entry)
    entries = fmt.validate_for_save({"documents": kept})
    _write(repo, entries)
    return listing(repo)
