"""Check published records for secrets and corroborate ambiguous URL-password matches."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from repo_intel.ingest import secrets
from repo_intel.compile.contracts import Evidence, Span
from repo_intel.storage.hashing import canonical
from repo_intel.storage.store import ObjectStore

_WORD = re.compile(rb"[A-Za-z0-9_$-]+")
_CONTENT_ID = re.compile(rb"(?:ent|as|ev|unk|cf|dec)_[a-z2-7]{26}")
_HASH = re.compile(rb"[0-9a-f]{64}")
_METHODS = {b"GET", b"POST", b"PUT", b"PATCH", b"DELETE", b"HEAD", b"OPTIONS", b"TRACE", b"CONNECT"}
_ENTITY_IDENTITIES = {
    ("name",),
    ("natural_key", "name"), ("natural_key", "path"), ("natural_key", "root"),
    ("natural_key", "manifest_dir"), ("natural_key", "package"), ("natural_key", "qualified_name"),
    ("natural_key", "table"), ("natural_key", "field"), ("natural_key", "column"),
    ("natural_key", "attribute"), ("natural_key", "namespace"), ("natural_key", "route"),
    ("natural_key", "model"), ("natural_key", "key"),
    ("attributes", "name"), ("attributes", "handler"), ("attributes", "jobs"),
    ("attributes", "attribute"), ("attributes", "path"), ("attributes", "table"),
    ("attributes", "base_images"), ("attributes", "images"), ("attributes", "image"),
}
_ID_FIELDS = {
    ("id",), ("subject",), ("object",), ("interface",), ("component",), ("caller",),
    ("evidence_ids",), ("counterevidence_ids",), ("subject_ids",), ("assertion_ids",),
    ("decision_ref",), ("attributes", "decision_ref"), ("attributes", "topology_decision_ref"),
}


def _values(obj: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield (*path, "<key>"), key
            yield from _values(value, (*path, key))
    elif isinstance(obj, list):
        for value in obj:
            yield from _values(value, path)
    elif obj is None:
        yield path, "null"
    elif isinstance(obj, bool):
        yield path, "true" if obj else "false"
    elif isinstance(obj, int):
        yield path, str(obj)
    elif isinstance(obj, float):
        yield path, canonical(obj).decode()


def _publisher_scope(path: tuple[str, ...], value: bytes) -> bool:
    """Content IDs and protocol enums have a publisher-controlled representation."""
    group, *fields = path
    field = tuple(fields)
    if group == "evidence" and field == ("<key>",) and value.decode() in Evidence.model_fields:
        return True
    if group == "evidence" and field == ("span", "<key>") and value.decode() in Span.model_fields:
        return True
    if group in {"entities", "assertions", "evidence", "unknowns", "conflicts", "operations", "unresolved_calls"}:
        if field in _ID_FIELDS and _CONTENT_ID.fullmatch(value):
            return True
        if group == "evidence" and field == ("blob_sha256",) and _HASH.fullmatch(value):
            return True
        if group in {"entities", "operations"} and field in {("method",), ("natural_key", "method")} and value in _METHODS:
            return True
    return False


def _identity_field(path: tuple[str, ...]) -> bool:
    return (
        path[0] == "entities" and path[1:] in _ENTITY_IDENTITIES
        or path in {("unit",), ("coverage", "scope"), ("operations", "route"),
                    ("coverage", "detail", "unresolved_imports")}
    )


def _records(documents: list[Any]) -> Iterator[tuple[tuple[str, ...], str, dict[str, Any]]]:
    """Identify the publisher's top-level record shapes. Untyped values get no exceptions."""
    for doc in documents:
        if isinstance(doc, dict) and {"unit", "entities", "assertions", "evidence", "unknowns"} <= doc.keys():
            for key in doc:
                yield ("<key>",), key, {}
            yield ("unit",), doc["unit"], {}
            for group in ("entities", "assertions", "evidence", "unknowns"):
                for row in doc[group]:
                    for path, value in _values(row, (group,)):
                        yield path, value, row
            for key in doc.keys() - {"unit", "entities", "assertions", "evidence", "unknowns"}:
                for path, value in _values(doc[key], ("other", key)):
                    yield path, value, {}
        elif isinstance(doc, dict) and doc.keys() == {"operations", "unresolved_calls"}:
            for group, rows in doc.items():
                yield ("<key>",), group, {}
                for row in rows:
                    for path, value in _values(row, (group,)):
                        yield path, value, row
        elif isinstance(doc, list):
            for row in doc:
                if isinstance(row, dict) and "domain" in row and "denominator" in row:
                    group = "coverage"
                elif isinstance(row, dict) and "conflict_type" in row and "assertion_ids" in row:
                    group = "conflicts"
                else:
                    group = "other"
                for path, value in _values(row, (group,)):
                    yield path, value, row if isinstance(row, dict) else {}
        else:
            for path, value in _values(doc, ("other",)):
                yield path, value, {}


class IdentityEvidence:
    """A metadata word must exist independently in its cited, sanitized source."""

    def __init__(self, store: ObjectStore, manifest: dict[str, Any], documents: list[Any]):
        self.store = store
        self.by_blob = {entry["blob_sha256"]: entry for entry in manifest["entries"]}
        self.by_path = {entry["path"].encode(): entry["blob_sha256"] for entry in manifest["entries"]}
        paths = {entry["path"] for entry in [*manifest["entries"], *manifest.get("exclusions", [])]}
        self.paths = paths | {
            "/".join(parts[:i]) for path in paths if (parts := path.split("/"))
            for i in range(1, len(parts))
        }
        self.evidence: dict[str, str] = {}
        dependencies: dict[str, set[str]] = defaultdict(set)
        self.packages: dict[str, set[str]] = defaultdict(set)
        for doc in documents:
            if not isinstance(doc, dict) or not {"entities", "assertions", "evidence"} <= doc.keys():
                continue
            self.evidence.update((row["id"], row["blob_sha256"]) for row in doc["evidence"])
            for row in doc["assertions"]:
                if row["predicate"] == "DEPENDS_ON":
                    dependencies[row["object"]].update(row["evidence_ids"])
            for row in doc["entities"]:
                if row["kind"] == "Package":
                    self.packages[row["name"]].update(row["evidence_ids"])
        self.dependencies = dependencies
        self.source = lru_cache(maxsize=128)(self._read_source)
        self.contract_notes: dict[tuple[str, bytes], bytes] = {}
        for doc in documents:
            if not isinstance(doc, dict) or doc.keys() != {"operations", "unresolved_calls"}:
                continue
            for operation in doc["operations"]:
                if operation["method"].encode() not in _METHODS:
                    continue
                for ref in operation["evidence_ids"]:
                    entry = self.by_blob.get(self.evidence.get(ref, ""))
                    if entry:
                        note = f"{operation['method']} {operation['route']} is declared in {entry['path']} and no implementation was found"
                        self.contract_notes[(ref, note.encode())] = operation["route"].encode()

    def _read_source(self, blob: str) -> tuple[bytes, bytes]:
        entry = self.by_blob.get(blob)
        if entry is None:
            return b"", b""
        data = self.store.get_bytes("sha256:" + blob) if entry.get("parse", True) else b""
        return secrets.mask_for_extraction(data, entry.get("secret_findings", [])), entry["path"].encode()

    def supports(self, path: tuple[str, ...], value: bytes, word: bytes, row: dict[str, Any]) -> bool:
        if path in {("unit",), ("coverage", "scope")} or path[-1] in {"path", "root", "manifest_dir"}:
            return value.decode() in self.paths
        if path[-1] == "name" and row.get("kind") == "Artifact":
            claimed = row.get("natural_key", {}).get("path", "")
            return claimed in self.paths and value.decode() == claimed.rsplit("/", 1)[-1]
        refs = set(row.get("evidence_ids", ()))
        if path == ("entities", "natural_key", "package"):
            refs.update(self.packages.get(value.decode(), ()))
        if row.get("kind") == "ExternalDependency":
            refs.update(self.dependencies.get(row["id"], ()))
        blobs = {self.evidence[ref] for ref in refs if ref in self.evidence}
        pattern = re.compile(rb"(?<![A-Za-z0-9_$-])" + re.escape(word) + rb"(?![A-Za-z0-9_$-])")
        if path == ("coverage", "detail", "unresolved_imports"):
            source_path = value.partition(b": ")[0]
            blob = self.by_path.get(source_path)
            if blob is not None:
                if pattern.search(source_path):
                    return True
                blobs.add(blob)
        for blob in blobs:
            data, source_path = self.source(blob)
            if pattern.search(data):
                return True
            if path[-1] in {"qualified_name", "package"} and pattern.search(source_path):
                return True
        return False

    def contract_route(self, value: bytes, row: dict[str, Any]) -> bytes | None:
        if row.get("conflict_type") == "declared_vs_implemented":
            for ref in row["evidence_ids"]:
                route = self.contract_notes.get((ref, value))
                if route is not None:
                    return route
        return None


def leaked_secrets(
    store: ObjectStore, path_manifest: dict[str, Any], documents: list[Any], catalogue: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    """Report source paths associated with leaks, without including the secret value.

    Strong and assignment-based detections are matched in every output key and
    value, including their JSON-escaped form.
    URL userinfo is always checked in output. Detached URL passwords still fail
    in arbitrary fields. A match in typed metadata can be a coincidence when
    its whole identifier occurs independently in the redacted, pinned source.
    """
    strong: dict[bytes, set[str]] = defaultdict(set)
    urls: dict[bytes, set[str]] = defaultdict(set)
    for entry in path_manifest["entries"]:
        findings = entry.get("secret_findings")
        if not findings:
            continue
        data = store.get_bytes("sha256:" + entry["blob_sha256"])
        for finding, value in zip(findings, secrets.values(data, findings), strict=True):
            if value:
                target = urls if finding["detector"] == "url_credentials" else strong
                target[value].add(entry["path"])

    def compile_matcher(values: dict[bytes, set[str]]) -> re.Pattern[bytes] | None:
        return re.compile(b"|".join(re.escape(v) for v in sorted(values, key=lambda v: (-len(v), v)))) if values else None

    leaks = set()
    strong_matcher = compile_matcher(strong)
    minimum_strong = min(map(len, strong), default=0)
    matcher = compile_matcher(urls)
    identity = IdentityEvidence(store, path_manifest, documents) if matcher else None
    for path, text, row in _records(documents):
        value = text.encode()
        if strong_matcher is not None:
            if len(value) >= minimum_strong:
                for match in strong_matcher.finditer(value):
                    leaks.update(strong[match[0]])
            if '\\' in text or '"' in text or not text.isprintable():
                for match in strong_matcher.finditer(canonical(text)):
                    leaks.update(strong[match[0]])
        if b"://" in value:
            for match in secrets.URL_CREDENTIALS.finditer(value):
                leaks.update(urls.get(match[1], {"<published output>"}))
        if matcher is None or not matcher.search(value):
            continue
        if _publisher_scope(path, value):
            continue
        if row.get("kind") == "TechnicalCapability":
            key = row["natural_key"].get("key")
            definition = (catalogue or {}).get(key)
            if definition is not None and (
                path == ("entities", "name") and text == definition[0]
                or path == ("entities", "attributes", "description") and text == definition[1]
                or path == ("entities", "natural_key", "key") and text == key
            ):
                continue
        # Endpoint display names include a fixed method and route from their natural key.
        key = row.get("natural_key", {})
        if row.get("kind") == "Endpoint" and path == ("entities", "name"):
            method, _, route = value.partition(b" ")
            if method in _METHODS and key.get("method") == method.decode() and key.get("route") == route.decode():
                value = route
        is_identity = _identity_field(path)
        if path == ("conflicts", "basis"):
            contract_route = identity.contract_route(value, row)
            if contract_route is not None:
                value, is_identity = contract_route, True
        words = list(_WORD.finditer(value)) if is_identity else []
        for match in matcher.finditer(value):
            word = next((w[0] for w in words if w.start() <= match.start() and match.end() <= w.end()), None)
            if not word or not identity.supports(path, value, word, row):
                leaks.update(urls[match[0]])
    return sorted(leaks)
