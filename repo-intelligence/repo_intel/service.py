"""Local compile service: repository registry and background compile jobs.

Single-process and single-user, for running the whole thing on one machine.
The registry is a JSON file in the tenant's store; jobs run on threads and keep
their progress events in memory. The durable route (Temporal workflow, tracked
refs, webhooks) is `workflows.py` plus M3 work and replaces this later.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import github, projection
from .decisions.gateway import Gateway
from .pipeline import PublishGateError, stage_compile, stage_snapshot
from .store import ObjectStore

PLANES = {
    "architecture": ["Repository", "Package", "Component", "Symbol", "ExternalDependency"],
    "contracts": ["Interface", "Endpoint", "EventTopic"],
    "data": ["DataSchema", "Table", "Field", "Migration", "StorageResource"],
    "ai": ["AIComponent", "ModelEndpoint", "PromptArtifact"],
    "tests": ["Test"],
    "deployment": ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
}


class Service:
    def __init__(self, store: ObjectStore, store_root: Path, conninfo: str, gateway: Gateway | None = None, allow_local_paths: bool = False):
        self.store, self.conninfo, self.gateway, self.allow_local_paths = store, conninfo, gateway, allow_local_paths
        self._file = Path(store_root) / store.tenant_id / "registry.json"
        self._mirrors = Path(store_root) / store.tenant_id / "mirrors"
        self._lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    # -- registry

    def _load(self) -> dict[str, Any]:
        return json.loads(self._file.read_text()) if self._file.exists() else {"repositories": {}}

    def _update(self, repository_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            repo = data["repositories"].setdefault(repository_id, {"repository_id": repository_id, "snapshots": []})
            snapshot = fields.pop("snapshot", None)
            repo.update(fields)
            if snapshot:
                repo["snapshots"] = [s for s in repo["snapshots"] if s["rkp_hash"] != snapshot["rkp_hash"]] + [snapshot]
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self._file)
            return repo

    def repositories(self) -> list[dict[str, Any]]:
        return sorted(self._load()["repositories"].values(), key=lambda r: r.get("full_name", ""))

    def repository(self, repository_id: str) -> dict[str, Any] | None:
        return self._load()["repositories"].get(repository_id)

    # -- connecting and compiling

    def connect(self, url: str | None = None, ref: str | None = None, local_path: str | None = None) -> dict[str, Any]:
        """Register a repository and start compiling one commit. Returns the job."""
        if local_path:
            if not self.allow_local_paths:
                raise github.GitHubError("local repositories are disabled on this server")
            return self._start_local(Path(local_path), ref)
        info = github.describe(url or "")
        sha, committed_at = github.resolve_commit(info, ref)
        self._update(info.repository_id, full_name=f"{info.owner}/{info.name}", source="github", default_branch=info.default_branch, private=info.private, description=info.description)
        return self._start(info.repository_id, ref or info.default_branch, sha, lambda: github.fetch_commit(info, sha, self._mirrors), committed_at)

    def recompile(self, repository_id: str, ref: str | None = None) -> dict[str, Any]:
        repo = self.repository(repository_id)
        if repo is None:
            raise KeyError(repository_id)
        if repo["source"] == "local":
            return self._start_local(Path(repo["local_path"]), ref)
        return self.connect(repo["full_name"], ref)

    def _start_local(self, path: Path, ref: str | None) -> dict[str, Any]:
        import subprocess
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            raise github.GitHubError("not a Git repository")
        sha = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}"], check=True, capture_output=True, text=True).stdout.strip()
        repository_id = "local/" + uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve())).hex[:12]
        self._update(repository_id, full_name=path.name, source="local", local_path=str(path.resolve()), default_branch=ref or "HEAD", private=True, description="Local repository")
        committed_at = subprocess.run(["git", "-C", str(path), "show", "-s", "--format=%cI", sha], check=True, capture_output=True, text=True).stdout.strip() or None
        return self._start(repository_id, ref or "HEAD", sha, lambda: path, committed_at)

    def _start(self, repository_id: str, ref: str, sha: str, obtain: Any, committed_at: str | None = None) -> dict[str, Any]:
        job = {"id": "cmp_" + uuid.uuid4().hex[:16], "repository_id": repository_id, "ref": ref, "commit_sha": sha, "committed_at": committed_at, "status": "queued", "rkp_hash": None, "error": None, "events": [], "started_at": time.time(), "finished_at": None}
        self.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job, obtain), daemon=True, name=job["id"]).start()
        return job

    def _run(self, job: dict[str, Any], obtain: Any) -> None:
        def tell(stage: str, detail: dict[str, Any] | None = None) -> None:
            job["events"].append({"seq": len(job["events"]), "stage": stage, "at": time.time(), **(detail or {})})

        try:
            job["status"] = "running"
            tell("fetch")
            repo_path = obtain()
            pm = stage_snapshot(self.store, repo_path, job["commit_sha"])
            result = stage_compile(self.store, job["repository_id"], pm, gateway=self.gateway, progress=tell)
            tell("project")
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                projection.project_snapshot(conn, self.store, result.manifest)
            job["rkp_hash"] = result.manifest.rkp_hash
            self._update(job["repository_id"], snapshot={"rkp_hash": result.manifest.rkp_hash, "commit_sha": job["commit_sha"], "ref": job["ref"], "committed_at": job["committed_at"], "compiled_at": time.time()})
            job["status"] = "complete"
            tell("complete", {"rkp_hash": result.manifest.rkp_hash})
        except PublishGateError as exc:
            job["status"], job["error"] = "failed", "the snapshot did not pass the publish gate"
            tell("failed", {"problems": exc.problems[:20]})
        except github.GitHubError as exc:
            job["status"], job["error"] = "failed", str(exc)
            tell("failed")
        except Exception as exc:  # report the kind of failure, not repository content
            job["status"], job["error"] = "failed", f"compile failed: {type(exc).__name__}"
            tell("failed")
        finally:
            job["finished_at"] = time.time()

    # -- reading a snapshot

    def summary(self, manifest: dict[str, Any]) -> dict[str, Any]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            counts = projection.count_by_kind(conn, self.store.tenant_id, manifest["rkp_hash"])
            repo_entity = projection.list_entities(conn, self.store.tenant_id, manifest["rkp_hash"], ["Repository"], None, 1)
        coverage = self.store.get_json(manifest["coverage_ref"])
        unknowns = self.store.get_json(manifest["unknowns_ref"])
        conflicts = self.store.get_json(manifest["conflicts_ref"])
        totals: dict[str, list[int]] = {}
        for c in coverage:
            t = totals.setdefault(c["domain"], [0, 0])
            t[0] += c["numerator"]
            t[1] += c["denominator"]
        return {
            "manifest": manifest,
            "topology": (repo_entity[0]["attributes"].get("topology") if repo_entity else None),
            "counts_by_kind": counts,
            "planes": {plane: {"kinds": {k: counts.get(k, 0) for k in kinds}, "total": sum(counts.get(k, 0) for k in kinds)} for plane, kinds in PLANES.items()},
            "files": counts.get("Artifact", 0),
            "coverage_totals": {d: {"numerator": n, "denominator": m} for d, (n, m) in sorted(totals.items())},
            "coverage": coverage,
            "unknowns": unknowns,
            "conflicts": conflicts,
        }

    def decisions(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        entries = self.store.get_json(manifest["ledger_entries_ref"])
        return [self.store.get_json(e["digest"]) for e in entries[:1000]]
