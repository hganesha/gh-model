"""The designation file `.rkp/documents.json`, schema `rkp-documents/1` (contract 9.1).

    {"schema": "rkp-documents/1",
     "documents": [{"path": "docs/pricing-rules.md", "features": ["rules"], "note": "signed off by finance"},
                   {"path": "specs/**/*.docx", "features": ["requirements", "schema"]}]}

`path` is a repository path or a glob: `*` matches within one path segment, `**` any
number of segments (none included), `?` one character. `features` is a non-empty subset
of FEATURES. `note` is the owner's free text, at most 500 characters, kept as written.

Reading is lenient and says why: `load` returns the entries it can use and a problem
for each one it cannot (an unknown feature, a path that is absolute or leaves the
repository, a malformed entry). A path that matches no file is a problem too, found by
`resolve`. Writing is strict: `validate_for_save` refuses what `load` would ignore, and
`dump` writes entries sorted by path with features sorted, so the file diffs cleanly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = "rkp-documents/1"
DESIGNATION_FILE = ".rkp/documents.json"
FEATURES = ("code_lists", "requirements", "rules", "schema")
MAX_ENTRIES = 1000
MAX_NOTE = 500
MAX_PATH = 512


class DesignationError(ValueError):
    """A designation document that cannot be written as given."""


def problem(path: str | None, message: str) -> dict[str, Any]:
    return {"path": path, "message": message}


def normalize_path(path: Any) -> str | None:
    """A designation path as a repository path, or None when it cannot be one: absolute,
    empty, leaving the repository (`..`), or too long."""
    if not isinstance(path, str):
        return None
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or len(p) > MAX_PATH or re.match(r"^[A-Za-z]:/", p):
        return None
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def is_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` within a segment, `**` across segments, `?` one character; anything else literal."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matches(pattern: str, path: str) -> bool:
    return bool(glob_regex(pattern).fullmatch(path)) if is_glob(pattern) else pattern == path


def _features(raw: Any) -> tuple[list[str], list[str]]:
    """(known features, unknown names) of an entry's `features`, each sorted and unique."""
    if not isinstance(raw, list):
        return [], []
    names = [f for f in raw if isinstance(f, str)]
    known = sorted({f for f in names if f in FEATURES})
    unknown = sorted({f for f in names if f not in FEATURES} | ({"<not a string>"} if len(names) != len(raw) else set()))
    return known, unknown


def parse(data: bytes) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The JSON document, or None with the problem that stops it being read."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [problem(DESIGNATION_FILE, f"{DESIGNATION_FILE} is not JSON: {exc.__class__.__name__}")]
    if not isinstance(doc, Mapping):
        return None, [problem(DESIGNATION_FILE, f"{DESIGNATION_FILE} must be a JSON object")]
    return dict(doc), []


def load_document(doc: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Usable entries `{path, features, note?}` in file order, and a problem per entry left out."""
    problems: list[dict[str, Any]] = []
    if doc.get("schema") != SCHEMA:
        return [], [problem(DESIGNATION_FILE, f"schema must be {SCHEMA!r}, not {doc.get('schema')!r}: nothing is read")]
    items = doc.get("documents")
    if not isinstance(items, list):
        return [], [problem(DESIGNATION_FILE, "documents must be a list")]
    if len(items) > MAX_ENTRIES:
        problems.append(problem(DESIGNATION_FILE, f"more than {MAX_ENTRIES} entries: the rest are not read"))
        items = items[:MAX_ENTRIES]
    out: list[dict[str, Any]] = []
    for n, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(problem(None, f"entry {n} is not an object"))
            continue
        raw_path = item.get("path")
        path = normalize_path(raw_path)
        if path is None:
            problems.append(problem(raw_path if isinstance(raw_path, str) else None, f"entry {n}: path must be a repository path or glob, relative and inside the repository"))
            continue
        known, unknown = _features(item.get("features"))
        if unknown:
            problems.append(problem(path, f"unknown feature {', '.join(unknown)}; features are {', '.join(FEATURES)}: the entry is ignored"))
            continue
        if not known:
            problems.append(problem(path, "features must name at least one of " + ", ".join(FEATURES) + ": the entry is ignored"))
            continue
        entry: dict[str, Any] = {"path": path, "features": known}
        note = item.get("note")
        if isinstance(note, str) and note.strip():
            entry["note"] = note.strip()[:MAX_NOTE]
        out.append(entry)
    return out, problems


def load(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    doc, problems = parse(data)
    if doc is None:
        return [], problems
    return load_document(doc)


def resolve(entries: Iterable[Mapping[str, Any]], paths: Iterable[str]) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """{file path: features} for every file some entry matches (the union of their features),
    and a problem for each entry that matches no file."""
    paths = sorted(set(paths))
    found: dict[str, set[str]] = {}
    problems = []
    for entry in entries:
        hits = [p for p in paths if matches(entry["path"], p)]
        if not hits:
            problems.append(problem(entry["path"], f"{entry['path']} matches no file in this commit"))
        for p in hits:
            found.setdefault(p, set()).update(entry["features"])
    return {p: sorted(f) for p, f in sorted(found.items())}, problems


def canonical(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The document as written: entries merged by path (features united, the first note kept),
    sorted by path, features sorted."""
    merged: dict[str, dict[str, Any]] = {}
    for e in entries:
        slot = merged.setdefault(e["path"], {"path": e["path"], "features": set()})
        slot["features"].update(e["features"])
        if e.get("note") and "note" not in slot:
            slot["note"] = e["note"]
    docs = []
    for path in sorted(merged):
        slot = merged[path]
        docs.append({"path": path, "features": sorted(slot["features"]), **({"note": slot["note"]} if slot.get("note") else {})})
    return {"schema": SCHEMA, "documents": docs}


def validate_for_save(body: Any) -> list[dict[str, Any]]:
    """Entries to write from a request body `{documents: [...]}`; DesignationError naming
    every entry that is not acceptable, so nothing half-valid is written."""
    if not isinstance(body, Mapping) or not isinstance(body.get("documents"), list):
        raise DesignationError("the body must be an object with a documents list")
    doc = {"schema": SCHEMA, "documents": body["documents"]}
    entries, problems = load_document(doc)
    for n, item in enumerate(body["documents"]):
        if isinstance(item, Mapping):
            extra = sorted(set(item) - {"path", "features", "note"})
            if extra:
                problems.append(problem(item.get("path") if isinstance(item.get("path"), str) else None, f"entry {n}: unknown key {', '.join(extra)}"))
            note = item.get("note")
            if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
                problems.append(problem(item.get("path") if isinstance(item.get("path"), str) else None, f"entry {n}: note must be text of at most {MAX_NOTE} characters"))
    if problems:
        raise DesignationError("; ".join(p["message"] for p in problems))
    return entries


def dump(entries: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(canonical(entries), indent=2, ensure_ascii=False) + "\n"
