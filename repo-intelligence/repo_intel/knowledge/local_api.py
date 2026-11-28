"""The Explorer's read API over a repository's `.rkp/` bundle, with no PostgreSQL.

Editor shells start `rkp serve --stdio` and send one request per line,
`{"id": ..., "method": "GET", "path": "snapshots/<hash>/summary", "body": ...}`, and
read one `{"id": ..., "status": 200, "body": ...}` per request. Paths are the
Explorer's (`repo_intel/server/api.py` without the `/v1/` prefix), so its views run
unchanged against a bundle.

Answers mirror the projection (`repo_intel/knowledge/projection.py`), built from
in-memory indexes of the snapshot's shards instead of SQL: the same filters, limits,
ordering and pooling of an entity declared in several shards (the shard with the
higher digest wins, as `ORDER BY shard_digest DESC` does).

What a single bundle cannot answer says so with 404 or 501 and a message: history
across commits (lineage), the cross-repository business map, and compiling. Owner
overrides are read from and written to `.rkp/overrides.json`.

`snapshots/<hash>/behavior/<entity id>` returns the behaviour units an entity owns, as
stored (each with its `kind` when the pack gave one), with the facts
`repo_intel.knowledge.behavior_store` derives from them, and the entity's transform shapes
(contract section 8.1); an entity with shapes and no units still answers. Behaviour is detail: on
a clone that has only the architecture tier it answers 404 `detail_not_committed`, never
an empty list.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from repo_intel.compile.contracts import Manifest
from repo_intel.compile.pipeline import render_evidence
from repo_intel.decisions.overrides import OverrideDocument, Overrides, SaveOverrides
from repo_intel.ingest import secrets
from repo_intel.knowledge import behavior_store, bundle, profile
from repo_intel.storage.store import ObjectStore
from repo_intel.workspace import business_graph

# The Explorer's planes; kept equal to repo_intel.server.service.PLANES by a test.
PLANES = {
    "architecture": ["Repository", "Package", "Component", "Symbol", "ExternalDependency", "ArchitecturePattern", "Antipattern"],
    "contracts": ["Interface", "Endpoint", "EventTopic"],
    "data": ["DataSchema", "Table", "Dataset", "Field", "Migration", "StorageResource"],
    "jobs": ["Job", "Task", "Notebook", "Program"],
    "ai": ["AIComponent", "ModelEndpoint", "PromptArtifact", "MLModel"],
    "tests": ["Test"],
    "deployment": ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
    "experience": ["Screen", "Form", "UIControl", "StyleToken", "Report"],
    "business": ["BusinessRule", "Workflow", "CodeList", "Requirement", "Feature"],
    "security": ["Role", "Permission"],
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _not_found() -> HttpError:
    return HttpError(404, "not found")


class Snapshot:
    """One published snapshot, indexed for the Explorer's queries."""

    def __init__(self, store: ObjectStore, manifest: Manifest, rank: dict[str, str] | None = None, detail: str = "complete"):
        """`rank` orders an entity's rows by unit as the projection would; it maps each unit to
        its full shard digest when `manifest` names an architecture tier's reduced shards."""
        self.store, self.manifest, self.detail = store, manifest, detail
        self.row = manifest.row()
        # entity_id -> [(shard_digest, row)], highest digest first
        self.entity_rows: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self.assertions: dict[str, dict[str, Any]] = {}
        self.touching: dict[str, set[str]] = defaultdict(set)
        self.evidence: dict[str, dict[str, Any]] = {}
        shards = []
        for ref in manifest.shards:
            shard = store.get_json(ref.digest)
            shards.append(shard)
            key = (rank or {}).get(ref.unit, ref.digest)
            for row in shard["entities"]:
                self.entity_rows[row["id"]].append((key, row))
            for row in shard["assertions"]:
                self.assertions.setdefault(row["id"], row)
                self.touching[row["subject"]].add(row["id"])
                self.touching[row["object"]].add(row["id"])
            for row in shard["evidence"]:
                self.evidence.setdefault(row["id"], row)
        for rows in self.entity_rows.values():
            rows.sort(key=lambda pair: pair[0], reverse=True)
        # owner entity id -> behaviour units, and -> transform-shape rows; empty when the
        # snapshot holds no detail
        self.behaviors = behavior_store.index(shards)
        self.transforms = behavior_store.transform_index(shards)

    def first(self, entity_id: str) -> dict[str, Any] | None:
        rows = self.entity_rows.get(entity_id)
        return rows[0][1] if rows else None

    # -- projection equivalents

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for entity_id, rows in self.entity_rows.items():
            for kind in {row["kind"] for _, row in rows}:
                counts[kind] += 1
        return dict(counts)

    def list_entities(self, kinds: list[str] | None, text: str | None, limit: int) -> list[dict[str, Any]]:
        needle = text.lower() if text else None
        found = {}
        for entity_id, rows in self.entity_rows.items():
            for _, row in rows:  # DISTINCT ON (kind, name, entity_id), highest shard digest first
                if kinds and row["kind"] not in kinds:
                    continue
                if needle and needle not in row["name"].lower() and needle not in str((row.get("natural_key") or {}).get("path", "")).lower():
                    continue
                found.setdefault((row["kind"], row["name"], entity_id), row)
        return [found[key] for key in sorted(found)[:limit]]

    def find_entities(self, kind: str | None, name: str | None) -> list[str]:
        return sorted(
            entity_id for entity_id, rows in self.entity_rows.items()
            if any((not kind or row["kind"] == kind) and (not name or row["name"] == name) for _, row in rows)
        )

    def query_graph(self, seeds: list[str], allowed: list[str] | None, max_depth: int, max_nodes: int) -> dict[str, Any]:
        known = {s for s in seeds if s in self.entity_rows}
        visited = sorted(known)[:max_nodes]
        truncated = len(known) > max_nodes
        seen = set(visited)
        frontier = list(visited)
        edges: dict[str, dict[str, Any]] = {}
        for _ in range(max_depth):
            if not frontier:
                break
            found = [self.assertions[a] for f in frontier for a in self.touching.get(f, ())]
            if allowed is not None:
                found = [a for a in found if a["predicate"] in allowed]
            nxt = sorted({a[end] for a in found for end in ("subject", "object")} - seen)
            room = max_nodes - len(visited)
            if len(nxt) > room:
                truncated = True
                nxt = nxt[:room]
            seen.update(nxt)
            visited += nxt
            for a in found:
                if a["subject"] in seen and a["object"] in seen:
                    edges[a["id"]] = a
            frontier = nxt
        # As the projection: rows ordered by shard digest descending into a dict, so the
        # lowest digest's row is the one kept.
        ents = {i: rows[-1][1] for i in visited if (rows := self.entity_rows.get(i))}
        ev_ids = sorted({i for a in edges.values() for i in a["evidence_ids"]} | {i for e in ents.values() for i in e["evidence_ids"]})
        evs = {i: self.evidence[i] for i in ev_ids if i in self.evidence}
        return {"entities": [ents[i] for i in sorted(ents)], "assertions": [edges[i] for i in sorted(edges)], "evidence": [evs[i] for i in sorted(evs)], "truncated": truncated}

    def entity_detail(self, entity_id: str, limit: int = 400) -> dict[str, Any] | None:
        rows = [row for _, row in self.entity_rows.get(entity_id, [])]
        if not rows:
            return None
        entity = rows[0]
        for other in rows[1:]:
            entity = {**entity, "attributes": {**other["attributes"], **entity["attributes"]}, "evidence_ids": sorted(set(entity["evidence_ids"]) | set(other["evidence_ids"]))}
        found = [self.assertions[a] for a in sorted(self.touching.get(entity_id, ()))][:limit]
        ids = sorted({a["subject"] for a in found} | {a["object"] for a in found})
        names = {i: {"id": i, "kind": row["kind"], "name": row["name"]} for i in ids if (row := self.first(i)) is not None}
        return {"entity": entity, "assertions": found, "neighbours": names, "truncated": len(found) >= limit}


class LocalApi:
    """Answers the Explorer's requests for one repository's bundle."""

    def __init__(self, repo: str | Path):
        self.repo, _ = bundle.source_root(repo)
        self._snapshot: Snapshot | None = None
        self._key: tuple[str, float] | None = None

    # -- loading

    def snapshot(self) -> Snapshot:
        """The bundle's snapshot; reloaded when `knowledge/manifest.json` changes."""
        path = self.repo / bundle.BUNDLE / "knowledge" / "manifest.json"
        try:
            key = (str(path), path.stat().st_mtime)
        except FileNotFoundError:
            raise HttpError(404, "No knowledge has been written for this repository; run Analyze Repository first.", "missing") from None
        if self._snapshot is None or self._key != key:
            document = bundle.read_manifest(self.repo)
            assert document is not None
            manifest = Manifest.model_validate(document["snapshot"])
            store = bundle.store_for(self.repo)
            if bundle.has_snapshot(store, manifest):  # compiled on this machine: detail included
                self._snapshot = Snapshot(store, manifest)
            else:
                store, loaded = bundle.hydrate(self.repo, store)
                rank = {ref.unit: ref.digest for ref in manifest.shards}
                detail = "architecture" if document.get("tier") == "architecture" else "complete"
                self._snapshot = Snapshot(store, loaded, rank, detail)
            self._key = key
        return self._snapshot

    def _at(self, rkp_hash: str) -> Snapshot:
        snap = self.snapshot()
        if rkp_hash != snap.manifest.rkp_hash:
            raise HttpError(404, "This snapshot is no longer the one in .rkp/knowledge/.", "replaced")
        return snap

    # -- dispatch

    def handle(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        try:
            return 200, self._route(method.upper(), path, body)
        except HttpError as error:
            return error.status, {"message": error.message, "code": error.code}
        except bundle.BundleError as error:
            return 409, {"message": str(error), "code": "bundle"}
        except ValidationError as error:
            return 422, {"message": str(error), "code": "invalid"}

    def _route(self, method: str, path: str, body: Any) -> Any:
        parts = urlsplit(path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        segments = [unquote(s) for s in parts.path.strip("/").split("/") if s]
        match method, segments:
            case "GET", ["local"]:
                return self.local()
            case "GET", ["snapshots", h, "summary"]:
                return self.summary(self._at(h))
            case "GET", ["snapshots", h, "entities"]:
                return self.entities(self._at(h), query)
            case "GET", ["snapshots", h, "entities", entity_id]:
                return self.entity(self._at(h), entity_id)
            case "GET", ["snapshots", h, "behavior", entity_id]:
                return self.behavior(self._at(h), entity_id)
            case "GET", ["snapshots", h, "dependencies"]:
                snap = self._at(h)
                return business_graph.dependency_groups(snap.store, snap.manifest)
            case "GET", ["snapshots", h, "placements"]:
                self._at(h)
                return {"components": {}}
            case "GET", ["snapshots", h, "decisions"]:
                snap = self._at(h)
                entries = snap.store.get_json(snap.manifest.ledger_entries_ref)
                return {"decisions": [snap.store.get_json(e["digest"]) for e in entries[:1000]]}
            case "GET", ["snapshots", h, "exclusions"]:
                return self.exclusions(self._at(h), int(query.get("limit", 500)))
            case "POST", ["knowledge", "query"]:
                return self.query(body or {})
            case "GET", ["evidence", evidence_id]:
                return self.evidence(self._at(query.get("rkp_hash", "")), evidence_id)
            case "GET", ["entities", _, "lineage"]:
                raise HttpError(404, "History across commits is not available in the editor yet.", "unsupported")
            case "GET", ["repositories", *rest] if rest[-1:] == ["overrides"]:
                return self.overrides()
            case "PUT", ["repositories", *rest] if rest[-1:] == ["overrides"]:
                return self.save_overrides(body or {})
            case "GET", ["repositories", *rest] if rest[-1:] == ["documents"]:
                return self.documents()
            case "PUT", ["repositories", *rest] if rest[-1:] == ["documents"]:
                return self.save_documents(body or {})
            case _, ["business-maps", *_]:
                raise HttpError(501, "The business map spans repositories and is not available in the editor.", "unsupported")
        raise _not_found()

    # -- handlers

    def local(self) -> dict[str, Any]:
        snap = self.snapshot()
        document = bundle.read_manifest(self.repo) or {}
        return {
            "repository": str(self.repo), "repository_id": snap.manifest.repository_id, "rkp_hash": snap.manifest.rkp_hash,
            "commit_sha": snap.manifest.commit_sha, "working_tree": document.get("working_tree", False), "status": snap.manifest.status,
            **({"source_kind": "folder"} if document.get("source_kind") == "folder" else {}),
            "tier": document.get("tier", "full"), "detail": snap.detail,
        }

    def summary(self, snap: Snapshot) -> dict[str, Any]:
        counts = snap.count_by_kind()
        repo_entity = snap.list_entities(["Repository"], None, 1)
        coverage = snap.store.get_json(snap.manifest.coverage_ref)
        totals: dict[str, list[int]] = {}
        for c in coverage:
            t = totals.setdefault(c["domain"], [0, 0])
            t[0] += c["numerator"]
            t[1] += c["denominator"]
        return {
            "manifest": snap.row,
            "topology": repo_entity[0]["attributes"].get("topology") if repo_entity else None,
            "counts_by_kind": counts,
            "planes": {plane: {"kinds": {k: counts.get(k, 0) for k in kinds}, "total": sum(counts.get(k, 0) for k in kinds)} for plane, kinds in PLANES.items()},
            "files": counts.get("Artifact", 0),
            "profile": profile.for_snapshot(snap.list_entities, counts, coverage),
            "coverage_totals": {d: {"numerator": n, "denominator": m} for d, (n, m) in sorted(totals.items())},
            "coverage": coverage,
            "unknowns": snap.store.get_json(snap.manifest.unknowns_ref),
            "conflicts": snap.store.get_json(snap.manifest.conflicts_ref),
        }

    def entities(self, snap: Snapshot, query: dict[str, str]) -> dict[str, Any]:
        plane, kind = query.get("plane"), query.get("kind")
        if plane is not None and plane not in PLANES and plane != "files":
            raise _not_found()
        kinds = [kind] if kind else (["Artifact"] if plane == "files" else PLANES.get(plane or "") or None)
        limit = max(1, min(int(query.get("limit", 200)), 500))
        rows = snap.list_entities(kinds, (query.get("q") or "")[:100] or None, limit + 1)
        return {"entities": rows[:limit], "truncated": len(rows) > limit}

    def entity(self, snap: Snapshot, entity_id: str) -> dict[str, Any]:
        detail = snap.entity_detail(entity_id)
        if detail is None:
            raise _not_found()
        detail["unknowns"] = [u for u in snap.store.get_json(snap.manifest.unknowns_ref) if u["subject"] == entity_id]
        detail["conflicts"] = [c for c in snap.store.get_json(snap.manifest.conflicts_ref) if entity_id in c["subject_ids"]]
        return detail

    def behavior(self, snap: Snapshot, entity_id: str) -> dict[str, Any]:
        """The behaviour units an entity owns, what they state, and its transform shapes
        (repo_intel.knowledge.behavior_store). Shapes alone are an answer: a PySpark notebook
        has no behaviour units but says what it does to its rows."""
        if snap.detail == "architecture":
            raise HttpError(404, "Behaviour is function-level detail, and this bundle commits only the architecture tier. Run Analyze Repository to compile it on this machine.", "detail_not_committed")
        units = snap.behaviors.get(entity_id, [])
        transforms = snap.transforms.get(entity_id, [])
        entity = snap.first(entity_id)
        if entity is None:
            raise _not_found()
        if not units and not transforms:
            raise HttpError(404, f"No behaviour was recorded for {entity['kind']} {entity['name']}.", "no_behavior")
        return {
            "entity": {"id": entity_id, "kind": entity["kind"], "name": entity["name"]},
            "units": units, "summary": behavior_store.summarize(units), "transforms": transforms,
        }

    def exclusions(self, snap: Snapshot, limit: int) -> dict[str, Any]:
        d = snap.store.index_get("path_manifests", snap.manifest.source_tree_hash)
        if d is not None and snap.store.has(d):
            pm = snap.store.get_json(d)
            rows, policy = pm.get("exclusions", []), pm.get("exclusion_policy")
        else:  # a hydrated clone: the bundle keeps each exclusion's path and reason
            rows = [{"path": f["path"], "reason": f["excluded"]} for f in self._files() if "excluded" in f]
            policy = None
        limit = max(1, min(limit, 2000))
        return {"available": True, "exclusions": rows[:limit], "policy_digest": policy, "total": len(rows), "truncated": len(rows) > limit}

    def query(self, body: dict[str, Any]) -> dict[str, Any]:
        snap = self._at(str(body.get("rkp_hash", "")))
        seeds = body.get("seed_entities")
        if seeds is None:
            selector = body.get("seed_selector") or {}
            seeds = snap.find_entities(selector.get("kind"), selector.get("name"))
        max_depth = max(0, min(int(body.get("max_depth", 1)), 5))
        max_nodes = max(1, min(int(body.get("max_nodes", 200)), 2000))
        result = snap.query_graph([str(s) for s in seeds], body.get("allowed_edge_types"), max_depth, max_nodes)
        return {"rkp_hash": snap.manifest.rkp_hash, "commit_sha": snap.manifest.commit_sha, "coverage_ref": snap.manifest.coverage_ref, **result}

    def evidence(self, snap: Snapshot, evidence_id: str) -> dict[str, Any]:
        row = snap.evidence.get(evidence_id)
        if row is None:
            raise _not_found()
        rendered = render_evidence(snap.store, snap.manifest, row)
        if rendered["status"] == "ok":
            return rendered
        # Not in the local cache (a fresh clone): read the working-tree file, if it still
        # has the analysed content, and mask it with a fresh secret scan.
        for entry in self._files():
            if entry.get("blob_sha256") != row["blob_sha256"]:
                continue
            path = (self.repo / entry["path"]).resolve()
            if not path.is_relative_to(self.repo) or not path.is_file():
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["blob_sha256"]:
                continue
            span = row["span"]
            end = min(span["end_byte"], span["start_byte"] + 8192)
            text, masked = secrets.mask(data, span["start_byte"], end, secrets.scan(data))
            return {"evidence_id": evidence_id, "status": "ok", "paths": [entry["path"]], "span": span, "text": text.decode("utf-8", "replace"), "masked": masked, "truncated": end < span["end_byte"], "trust_label": row["trust_label"]}
        return rendered

    def _files(self) -> list[dict[str, Any]]:
        path = self.repo / bundle.BUNDLE / "knowledge" / "files.jsonl"
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    # -- owner overrides, in .rkp/overrides.json

    def _overrides_file(self) -> Path:
        return self.repo / bundle.BUNDLE / "overrides.json"

    def _version(self) -> int:
        """A content version for optimistic saves: 0 when there is no file."""
        try:
            data = self._overrides_file().read_bytes()
        except FileNotFoundError:
            return 0
        return int(hashlib.sha256(data).hexdigest()[:13], 16) or 1

    def overrides(self) -> dict[str, Any]:
        current = bundle.load_overrides(self.repo)
        snap = self.snapshot()
        return {"repository_id": snap.manifest.repository_id, "version": self._version(), "override_set_hash": current.set_hash, **current.document, "history": []}

    def save_overrides(self, body: dict[str, Any]) -> dict[str, Any]:
        request = SaveOverrides.model_validate(body)
        if request.expected_version != self._version():
            raise HttpError(409, "The overrides changed since they were read; reload and try again.", "stale")
        document = OverrideDocument(decisions=request.decisions, preferences=request.preferences)
        current = Overrides.of(document)
        path = self._overrides_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current.document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        tmp.replace(path)
        return {"repository_id": self.snapshot().manifest.repository_id, "version": self._version(), "override_set_hash": current.set_hash}

    # -- designated documents, in .rkp/documents.json (contract 9.1; repo_intel.documents.controls)

    def documents(self) -> dict[str, Any]:
        """`{documents, candidates, problems, version}`; answers before the first compile too."""
        from repo_intel.documents import controls
        return controls.listing(self.repo)

    def save_documents(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate `{documents: [...]}` and write `.rkp/documents.json`; 422 when an entry is
        not acceptable, 409 when `expected_version` is given and the file changed since."""
        from repo_intel.documents import controls, format as designations
        try:
            return controls.save(self.repo, body)
        except designations.DesignationError as error:
            raise HttpError(422, str(error), "invalid") from None
        except controls.StaleDesignations as error:
            raise HttpError(409, str(error), "stale") from None


def serve_stdio(repo: str | Path, stdin: Any = None, stdout: Any = None, log: Callable[[str], None] | None = None) -> int:
    """One JSON request per line in, one JSON response per line out, until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    api = LocalApi(repo)
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            ident = request.get("id")
            status, payload = api.handle(str(request.get("method", "GET")), str(request["path"]), request.get("body"))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            ident, status, payload = None, 400, {"message": f"bad request: {error}", "code": "bad_request"}
        except Exception as error:  # one failed request never stops the server
            if log:
                log(repr(error))
            status, payload = 500, {"message": str(error) or type(error).__name__, "code": "internal"}
            ident = request.get("id") if isinstance(request, dict) else None
        stdout.write(json.dumps({"id": ident, "status": status, "body": payload}, sort_keys=True) + "\n")
        stdout.flush()
    return 0
