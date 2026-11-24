"""Local compile service: repository registry and background compile jobs.

Single-process, for running the whole thing on one machine. The registry and
tracked refs are in PostgreSQL. Compile jobs run on threads and keep their
progress events in memory; `workflows.py` is the durable Temporal route.

Triggers (spec §4.5, GI-018). Compiles are coalesced per (repository, ref):
one runs at a time, at most one waits, and a newer head replaces the waiting
one and cancels the running one at its next checkpoint (between stages and
in the progress callback), unless the running commit is tagged. A request for
a snapshot that is already queued or running joins that job (in-flight dedupe
by snapshot key). Tags compile eagerly; pull requests compile after a debounce
following their last update, eagerly or on the first request for their ref.
`reconcile_once` catches missed webhooks for push-tracked refs.

Lifecycle (GI-019 to GI-021): sampled shadow compiles, quarantine and promote,
lineage reconciliation, retraction, retention and deletion.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import business_graph, business_map, connectors, cross_repository, gc, github, lineage, parity, projection, registry, settings, triggers
from .contracts import Manifest
from .hashing import digest
from .decisions import placement
from .decisions.gateway import Gateway
from .decisions.runner import Budget
from .capabilities import CapabilityCatalogue, match_package
from .pipeline import PublishGateError, load_manifest, load_shards, snapshot_key, stage_compile, stage_snapshot, toolchain_hash
from .store import ObjectStore

PLANES = {
    "architecture": ["Repository", "Package", "Component", "Symbol", "ExternalDependency"],
    "contracts": ["Interface", "Endpoint", "EventTopic"],
    "data": ["DataSchema", "Table", "Field", "Migration", "StorageResource"],
    "ai": ["AIComponent", "ModelEndpoint", "PromptArtifact"],
    "tests": ["Test"],
    "deployment": ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
}


TERMINAL = ("complete", "failed", "cancelled", "superseded")


class Superseded(Exception):
    """A newer head of the same ref arrived; this compile stops at its next checkpoint."""


class SharedLock:
    """Compiles hold it shared; garbage collection and deletion hold it exclusively."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False

    @contextmanager
    def shared(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._writer = True
            while self._readers:
                self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class Service:
    pr_debounce = 60.0  # seconds after a pull request's last update before it compiles
    reconcile_minutes = 10.0

    def __init__(self, store: ObjectStore, store_root: Path, conninfo: str, gateway: Gateway | None = None, allow_local_paths: bool = False):
        self.store, self.conninfo, self.gateway, self.allow_local_paths = store, conninfo, gateway, allow_local_paths
        self._mirrors = Path(store_root) / store.tenant_id / "mirrors"
        self.jobs: dict[str, dict[str, Any]] = {}
        self.mirror_lock = threading.Lock()
        self.mirror_locks: dict[str, Any] = {}
        self.secrets = connectors.SecretReferences()
        self.app_tokens = connectors.InstallationTokens(self.secrets)
        self.device_attempts: dict[str, dict[str, Any]] = {}
        self.device_lock = threading.Lock()
        self.poll_due: dict[str, float] = {}
        self.placement_lock = threading.Lock()
        self.placement_run = threading.Lock()  # one placement run at a time: background and explicit runs read and write the same map
        self.pending_placements: set[str | None] = set()
        self.placement_worker: threading.Thread | None = None
        self.lock = threading.RLock()  # jobs, ref slots and in-flight snapshot keys
        self.ref_slots: dict[tuple[str, str], dict[str, Any]] = {}
        self.inflight: dict[str, dict[str, Any]] = {}
        self.timers: dict[tuple[str, str], threading.Timer] = {}
        self.reconcile_due: dict[str, float] = {}
        self.daily_due: dict[str, float] = {}
        self.maintenance = SharedLock()
        self._store_root = Path(store_root)
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.import_file(conn, self.store.tenant_id, Path(store_root) / store.tenant_id / "registry.json")

    # -- registry

    def _update(self, repository_id: str, **fields: Any) -> dict[str, Any]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.update(conn, self.store.tenant_id, repository_id, **fields)

    def repositories(self) -> list[dict[str, Any]]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.all_repositories(conn, self.store.tenant_id)

    def repository(self, repository_id: str) -> dict[str, Any] | None:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.read(conn, self.store.tenant_id, repository_id)

    # -- connecting and compiling

    def workspace_settings(self) -> tuple[int, settings.Settings]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return settings.read(conn, self.store.tenant_id)

    def compile_configuration(self, document: settings.Settings):
        effective = settings.effective_compile(document)
        policy = effective.model_copy(update={
            "model_functions": {k: v for k, v in effective.model_functions.items() if k == "dm.component-kind"},
            "allowed_providers": tuple(sorted(set(effective.allowed_providers) - {"openai-embeddings"})),
            "disabled_extractors": tuple(sorted(set(effective.disabled_extractors))),
        })
        return policy.gateway(self.gateway), policy

    def _connection(self, connection_id: str) -> tuple[settings.Connection | None, connectors.Connector]:
        cfg = None
        if connection_id != "github-cli":
            cfg = next((c for c in self.workspace_settings()[1].connections if c.id == connection_id), None)
            if cfg is None:
                raise KeyError(connection_id)
        return cfg, connectors.build(cfg, self.secrets, self.app_tokens)

    def browse(self, connection_id: str, page: int) -> dict[str, Any]:
        _, connector = self._connection(connection_id)
        rows = connector.repositories(page)
        return {"repositories": [
            {"selector": r.selector, "full_name": r.full_name, "default_branch": r.default_branch, "private": r.private}
            for r in rows
        ], "has_more": len(rows) == 100}

    def connection_test(self, connection_id: str) -> dict[str, Any]:
        cfg, connector = self._connection(connection_id)
        try:
            visible = connector.repositories()
            scopes = self.app_tokens.permissions.get(connection_id) if cfg and cfg.mode == "github_app" else getattr(connector, "scopes", None)
            status = {"status": "connected", "repositories_sampled": len(visible), "scopes": scopes}
        except github.GitHubError as exc:
            status = {"status": "failed", "message": str(exc)}
        status["checked_at"] = time.time()
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            conn.execute(
                """INSERT INTO connection_checks VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id,connection_id) DO UPDATE SET document=excluded.document""",
                (self.store.tenant_id, connection_id, Jsonb(status)),
            )
        return status

    def start_device(self, connection_id: str) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        if cfg is None or cfg.mode != "github_device":
            raise KeyError(connection_id)
        if cfg.credential_ref and not cfg.credential_ref.startswith(f"{self.secrets.writable}:"):
            raise github.GitHubError(f"Device sign-in stores its token in {'Key Vault' if self.secrets.writable == 'vault' else 'the OS keychain'}: leave the credential reference empty or use a {self.secrets.writable}: reference.")
        state = connectors.begin_device(cfg)
        state["configuration"] = cfg
        with self.device_lock:
            self.device_attempts[connection_id] = state
        return {"user_code": state["user_code"], "verification_uri": "https://github.com/login/device", "interval": state["interval"]}

    def poll_device(self, connection_id: str) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        with self.device_lock:
            state = self.device_attempts.get(connection_id)
            if state is None or state["configuration"] != cfg:
                return {"status": "expired"}
            status = connectors.finish_device(cfg, state, self.secrets)
            if status != "pending":
                self.device_attempts.pop(connection_id, None)
            return {"status": status, "interval": state["interval"]}

    def connect(self, url: str | None = None, ref: str | None = None, local_path: str | None = None, connection_id: str = "github-cli") -> dict[str, Any]:
        """Register a repository and compile one commit."""
        if local_path:
            if not self.allow_local_paths:
                raise github.GitHubError("local repositories are disabled on this server")
            return self._start_local(Path(local_path), ref)
        cfg, connector = self._connection(connection_id)
        info = connector.describe(url or "")
        tracked_ref = ref or info.default_branch
        sha, committed_at = connector.resolve(info, tracked_ref)
        self._update(
            info.repository_id, full_name=info.full_name, selector=info.selector, connection_id=connection_id,
            source=cfg.mode if cfg else "github", default_branch=info.default_branch,
            private=info.private, description=info.description,
        )
        return self._start(info.repository_id, tracked_ref, sha, lambda: connector.fetch(info, sha, self._mirrors), committed_at)

    def _remote(self, repository: dict[str, Any]) -> tuple[connectors.Connector, connectors.RemoteRepository]:
        _, connector = self._connection(repository.get("connection_id", "github-cli"))
        selector = repository.get("selector", repository["full_name"])
        if repository["repository_id"].startswith("github.com/") and not isinstance(connector, connectors.PlainGit):
            selector = repository["repository_id"].rsplit("/", 1)[-1]
        info = connector.describe(selector)
        if info.repository_id != repository["repository_id"]:
            raise github.GitHubError("The remote identity changed. Connect it as a new repository.")
        return connector, info

    def recompile(self, repository_id: str, ref: str | None = None, *, only_if_changed: bool = False) -> dict[str, Any] | None:
        repo = self.repository(repository_id)
        if repo is None:
            raise KeyError(repository_id)
        if repo["source"] == "local":
            if not self.allow_local_paths:
                raise github.GitHubError("local repositories are disabled on this server")
            return self._start_local(Path(repo["local_path"]), ref, only_if_changed=only_if_changed)
        connector, info = self._remote(repo)
        target = ref or info.default_branch
        sha, date = connector.resolve(info, target)
        if only_if_changed and self._is_current(repo, target, sha):
            return None
        return self._start(repository_id, target, sha, lambda: connector.fetch(info, sha, self._mirrors), date)

    def _is_current(self, repo: dict, ref: str, sha: str) -> bool:
        latest = max((s for s in repo["snapshots"] if s["ref"] == ref), default=None,
                     key=lambda s: s.get("requested_at", s["compiled_at"]))
        gateway, policy = self.compile_configuration(self.workspace_settings()[1])
        return bool(latest and latest["commit_sha"] == sha and latest.get("toolchain_hash") == toolchain_hash(gateway, policy))

    def poll_repositories(self) -> None:
        while True:
            for job in (self.poll_once, self.reconcile_once, self.daily_once):
                try:
                    job()
                except Exception:
                    # PostgreSQL or a host may be temporarily unavailable. The next iteration retries.
                    pass
            time.sleep(15)

    def daily_once(self, now: float | None = None) -> None:
        """Once a day: garbage collection, and a cold shadow compile of each default-branch head."""
        now = time.time() if now is None else now
        if self.daily_due.get("gc", 0) <= now:
            self.daily_due["gc"] = now + 86400
            self.collect_garbage(now=now)
        _, document = self.workspace_settings()
        if not document.parity.nightly_head or self.daily_due.get("shadow", 0) > now:
            return
        self.daily_due["shadow"] = now + 86400
        for repo in self.repositories():
            heads = [s for s in repo.get("snapshots", []) if s["ref"] == repo.get("default_branch") and not s.get("retracted")]
            if not heads:
                continue
            head = max(heads, key=lambda s: s.get("compiled_at", 0))
            manifest = load_manifest(self.store, rkp_hash=head["rkp_hash"])
            try:
                if repo["source"] == "local":
                    path = Path(repo["local_path"])
                else:
                    connector, info = self._remote(repo)
                    path = connector.fetch(info, head["commit_sha"], self._mirrors)
                if manifest is not None:
                    self.shadow_compile(manifest, path)
            except Exception:
                continue

    def poll_once(self) -> None:
        _, document = self.workspace_settings()
        for repo_id, policy in document.repositories.items():
            if policy.trigger != "poll" or self.poll_due.get(repo_id, 0) > time.monotonic():
                continue
            self.poll_due[repo_id] = time.monotonic() + 60 * policy.interval_minutes
            for ref in policy.refs:
                try:
                    self.recompile(repo_id, ref, only_if_changed=True)
                except (KeyError, github.GitHubError):
                    continue

    def webhook(self, connection_id: str, headers: Any, raw: bytes) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        if cfg is None:
            raise KeyError(connection_id)
        event = triggers.verify_event(cfg, headers, raw, self.secrets)
        if not event:
            return {"status": "ignored"}
        _, document = self.workspace_settings()
        policy, repo = document.repositories.get(event.repository_id), self.repository(event.repository_id)
        if not repo or repo.get("connection_id") != connection_id or not policy or policy.trigger != "push":
            return {"status": "ignored"}
        if event.kind == "branch" and event.ref not in policy.refs or event.kind == "tag" and not policy.tags or event.kind == "pull_request" and policy.pull_requests == "off":
            return {"status": "ignored"}
        key = (self.store.tenant_id, connection_id, event.delivery)
        if event.action == "closed":
            if not self._claim(key):
                return {"status": "duplicate"}
            self._close_ref(event.repository_id, event.ref)
            return {"status": "closed"}
        connector, info = self._remote(repo)
        # Check the signed commit against the credential's repository visibility before fetching.
        _, date = connector.resolve(info, event.sha)
        if not self._claim(key):
            return {"status": "duplicate"}
        try:
            if event.kind == "pull_request":
                return self._pull_request_moved(event.repository_id, event.ref, event.sha, policy.pull_requests == "eager")
            job = self._start(event.repository_id, event.ref, event.sha, lambda: connector.fetch(info, event.sha, self._mirrors), date)
        except Exception:
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                conn.execute("DELETE FROM webhook_deliveries WHERE tenant_id=%s AND connection_id=%s AND delivery_id=%s", key)
            raise
        return {"status": "queued", "job_id": job["id"]}

    def _claim(self, key: tuple[str, str, str]) -> bool:
        """Record a delivery once; a redelivered webhook carries the same delivery ID."""
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return conn.execute(
                """INSERT INTO webhook_deliveries (tenant_id,connection_id,delivery_id)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING delivery_id""",
                key,
            ).fetchone() is not None

    # -- pull requests: debounce, then compile eagerly or on first request

    def _pull_request_moved(self, repository_id: str, ref: str, sha: str, eager: bool) -> dict[str, Any]:
        due = time.time() + self.pr_debounce
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.track_head(conn, self.store.tenant_id, repository_id, ref, sha, compile_after=due)
            requested = (registry.tracked_ref(conn, self.store.tenant_id, repository_id, ref) or {}).get("requested_at")
        self._cancel_ref(repository_id, ref, "a newer pull request head arrived")
        if eager or requested:
            self._schedule(repository_id, ref, self.pr_debounce)
            return {"status": "scheduled", "compile_after": due}
        return {"status": "recorded", "compile_after": due}

    def _schedule(self, repository_id: str, ref: str, delay: float) -> None:
        with self.lock:
            old = self.timers.pop((repository_id, ref), None)
            if old:
                old.cancel()
            timer = threading.Timer(max(0.0, delay), self._compile_ref_head, args=(repository_id, ref))
            timer.daemon = True
            self.timers[(repository_id, ref)] = timer
            timer.start()

    def _compile_ref_head(self, repository_id: str, ref: str) -> dict[str, Any] | None:
        with self.lock:
            self.timers.pop((repository_id, ref), None)
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            row = registry.tracked_ref(conn, self.store.tenant_id, repository_id, ref)
        repo = self.repository(repository_id)
        if not row or not repo or not row["head_sha"] or row["closed_at"] or row["state"] == "current":
            return None
        try:
            if repo["source"] == "local":
                return self._start_local(Path(repo["local_path"]), ref, sha=row["head_sha"])
            connector, info = self._remote(repo)
            sha = row["head_sha"]
            return self._start(repository_id, ref, sha, lambda: connector.fetch(info, sha, self._mirrors))
        except (KeyError, github.GitHubError):
            return None

    def request_ref(self, repository_id: str, ref: str) -> dict[str, Any] | None:
        """A consumer asked for a ref's snapshot. A stale pull request compiles now, or once its debounce ends."""
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            row = registry.tracked_ref(conn, self.store.tenant_id, repository_id, ref)
            if row is None:
                return None
            if row["kind"] == "pull_request" and row["state"] == "stale" and not row["closed_at"]:
                registry.ref_requested(conn, self.store.tenant_id, repository_id, ref)
                with self.lock:
                    slot = self.ref_slots.get((repository_id, ref), {})
                    busy = bool(slot.get("active") or slot.get("pending")) or (repository_id, ref) in self.timers
                if not busy:
                    self._schedule(repository_id, ref, (row["compile_after"] or 0) - time.time())
            return registry.tracked_ref(conn, self.store.tenant_id, repository_id, ref)

    def _close_ref(self, repository_id: str, ref: str) -> None:
        with self.lock:
            timer = self.timers.pop((repository_id, ref), None)
            if timer:
                timer.cancel()
        self._cancel_ref(repository_id, ref, "the pull request closed")
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.close_ref(conn, self.store.tenant_id, repository_id, ref)

    def _cancel_ref(self, repository_id: str, ref: str, reason: str) -> None:
        with self.lock:
            slot = self.ref_slots.get((repository_id, ref))
            if not slot:
                return
            if slot.get("pending"):
                self._supersede(slot.pop("pending")[0], reason)
            if slot.get("active"):
                slot["active"]["cancel"] = reason

    # -- missed webhooks

    def reconcile_once(self, now: float | None = None) -> list[dict[str, Any]]:
        """Compare push-tracked refs with the host and compile any head that was missed."""
        now = time.time() if now is None else now
        started = []
        _, document = self.workspace_settings()
        for repo_id, policy in document.repositories.items():
            if policy.trigger != "push" or self.reconcile_due.get(repo_id, 0) > now:
                continue
            self.reconcile_due[repo_id] = now + 60 * self.reconcile_minutes
            repo = self.repository(repo_id)
            if repo is None:
                continue
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                rows = {r["ref"]: r for r in registry.tracked_refs(conn, self.store.tenant_id, repo_id)}
            refs = sorted(set(policy.refs) | {r for r, row in rows.items() if row["kind"] == "branch" or row["kind"] == "tag" and policy.tags})
            try:
                connector, info = self._remote(repo)
            except (KeyError, github.GitHubError):
                continue
            for ref in refs:
                try:
                    sha, date = connector.resolve(info, ref)
                except (KeyError, github.GitHubError):
                    continue
                row = rows.get(ref)
                with self.lock:
                    slot = self.ref_slots.get((repo_id, ref), {})
                    busy = any(j and j[0]["commit_sha"] == sha if isinstance(j, tuple) else j and j["commit_sha"] == sha for j in (slot.get("active"), slot.get("pending")))
                if busy or row and row["published_sha"] == sha and row["head_sha"] == sha:
                    continue
                started.append(self._start(repo_id, ref, sha, lambda c=connector, i=info, s=sha: c.fetch(i, s, self._mirrors), date))
        return started

    def _start_local(self, path: Path, ref: str | None, *, only_if_changed: bool = False, sha: str | None = None) -> dict[str, Any] | None:
        import subprocess
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            raise github.GitHubError("not a Git repository")
        try:
            sha = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--verify", "--end-of-options", f"{connectors.checked_ref(sha or ref or 'HEAD')}^{{commit}}"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise github.GitHubError("Local Git ref was not found.") from None
        repository_id = "local/" + uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve())).hex[:12]
        previous = self.repository(repository_id)
        if only_if_changed and self._is_current(previous or {"snapshots": []}, ref or "HEAD", sha):
            return None
        self._update(repository_id, full_name=path.name, source="local", local_path=str(path.resolve()),
                     default_branch=(previous or {}).get("default_branch", ref or "HEAD"), private=True, description="Local repository")
        committed_at = subprocess.run(["git", "-C", str(path), "show", "-s", "--format=%cI", sha], check=True, capture_output=True, text=True).stdout.strip() or None
        return self._start(repository_id, ref or "HEAD", sha, lambda: path, committed_at)

    def _start(self, repository_id: str, ref: str, sha: str, obtain: Any, committed_at: str | None = None) -> dict[str, Any]:
        """Queue a compile of `sha` for `ref`, coalesced with other compiles of that ref."""
        configuration = self.compile_configuration(self.workspace_settings()[1])
        key = snapshot_key(repository_id, sha, toolchain_hash(*configuration))
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.track_head(conn, self.store.tenant_id, repository_id, ref, sha, reopen=registry.ref_kind(ref) != "pull_request")
        with self.lock:
            same = self.inflight.get(key)
            if same is not None and same["status"] not in TERMINAL:
                if ref != same["ref"]:
                    same.setdefault("also_refs", []).append(ref)
                return same  # in-flight dedupe: this snapshot is already being compiled
            job = {"id": "cmp_" + uuid.uuid4().hex[:16], "repository_id": repository_id, "ref": ref, "commit_sha": sha, "committed_at": committed_at, "status": "queued", "rkp_hash": None, "error": None, "events": [], "started_at": time.time(), "finished_at": None, "snapshot_key": key}
            self.jobs[job["id"]] = job
            self.inflight[key] = job
            slot = self.ref_slots.setdefault((repository_id, ref), {"active": None, "pending": None})
            if slot["pending"] is not None:
                self._supersede(slot["pending"][0], f"superseded by {job['id']}")
            active = slot["active"]
            if active is None:
                slot["active"] = job
                self._launch(job, obtain, configuration)
            else:
                if active["commit_sha"] != sha and not self._tagged(repository_id, active["commit_sha"]):
                    active["cancel"] = f"superseded by {job['id']}"
                slot["pending"] = (job, obtain, configuration)
        return job

    def _launch(self, job: dict[str, Any], obtain: Any, configuration: Any) -> None:
        threading.Thread(target=self._run, args=(job, obtain, configuration), daemon=True, name=job["id"]).start()

    def _supersede(self, job: dict[str, Any], reason: str) -> None:
        job["status"], job["error"], job["finished_at"] = "superseded", reason, time.time()
        job["events"].append({"seq": len(job["events"]), "stage": "superseded", "at": time.time()})
        if self.inflight.get(job["snapshot_key"]) is job:
            del self.inflight[job["snapshot_key"]]

    def _tagged(self, repository_id: str, sha: str) -> bool:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return any(r["kind"] == "tag" and r["head_sha"] == sha for r in registry.tracked_refs(conn, self.store.tenant_id, repository_id))

    def _finish_slot(self, job: dict[str, Any]) -> None:
        with self.lock:
            if self.inflight.get(job["snapshot_key"]) is job:
                del self.inflight[job["snapshot_key"]]
            slot = self.ref_slots.get((job["repository_id"], job["ref"]))
            if not slot or slot["active"] is not job:
                return
            slot["active"] = None
            if slot["pending"] is not None:
                nxt, obtain, configuration = slot["pending"]
                slot["pending"] = None
                slot["active"] = nxt
                self._launch(nxt, obtain, configuration)

    def _run(self, job: dict[str, Any], obtain: Any, configuration: Any) -> None:
        def checkpoint() -> None:
            if job.get("cancel"):
                raise Superseded(job["cancel"])

        def tell(stage: str, detail: dict[str, Any] | None = None) -> None:
            job["events"].append({"seq": len(job["events"]), "stage": stage, "at": time.time(), **(detail or {})})
            if stage != "published":
                checkpoint()

        try:
            with self.maintenance.shared():
                job["status"] = "running"
                tell("fetch")
                with self.mirror_lock:
                    lock = self.mirror_locks.setdefault(job["repository_id"], threading.Lock())
                with lock:
                    repo_path = obtain()
                self._used_mirror(repo_path)
                checkpoint()
                pm = stage_snapshot(self.store, repo_path, job["commit_sha"])
                checkpoint()
                gateway, policy = configuration
                result = stage_compile(self.store, job["repository_id"], pm, gateway=gateway, progress=tell, policy=policy)
                checkpoint()
                tell("project")
                previous = self._previous(job["repository_id"], job["ref"], result.manifest.rkp_hash)
                with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                    projection.project_snapshot(conn, self.store, result.manifest)
                job["rkp_hash"] = result.manifest.rkp_hash
                entry = {"rkp_hash": result.manifest.rkp_hash, "commit_sha": job["commit_sha"], "ref": job["ref"], "committed_at": job["committed_at"], "compiled_at": time.time(), "requested_at": job["started_at"], "toolchain_hash": result.manifest.toolchain_hash}
                self._update(job["repository_id"], snapshot=entry)
                with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                    for ref in [job["ref"], *job.get("also_refs", [])]:
                        registry.ref_published(conn, self.store.tenant_id, job["repository_id"], ref, job["commit_sha"], result.manifest.rkp_hash)
                # After publish; a failure here never unpublishes the snapshot (spec §5 Stage 5).
                self._reconcile_lineage(result.manifest, previous, repo_path)
                tell("complete", {"rkp_hash": result.manifest.rkp_hash})
                job["status"] = "complete"
            self.queue_placement(job["repository_id"])
            _, document = self.workspace_settings()
            if parity.sampled(result.manifest.rkp_hash, document.parity.shadow_rate):
                job["shadow"] = "running"
                threading.Thread(target=self._shadow_job, args=(job, result.manifest, repo_path, configuration), daemon=True).start()
        except Superseded as exc:
            job["status"], job["error"] = "cancelled", str(exc)
            job["events"].append({"seq": len(job["events"]), "stage": "cancelled", "at": time.time()})
        except PublishGateError as exc:
            job["status"], job["error"] = "failed", "the snapshot did not pass the publish gate"
            job["events"].append({"seq": len(job["events"]), "stage": "failed", "at": time.time(), "problems": exc.problems[:20]})
        except github.GitHubError as exc:
            job["status"], job["error"] = "failed", str(exc)
            job["events"].append({"seq": len(job["events"]), "stage": "failed", "at": time.time()})
        except Exception as exc:  # report the kind of failure, not repository content
            if job["status"] != "complete":
                job["status"], job["error"] = "failed", f"compile failed: {type(exc).__name__}"
                job["events"].append({"seq": len(job["events"]), "stage": "failed", "at": time.time()})
        finally:
            job["finished_at"] = job["finished_at"] or time.time()
            self._finish_slot(job)

    def _used_mirror(self, repo_path: Any) -> None:
        try:
            path = Path(repo_path)
            if self._mirrors in path.parents:
                os.utime(path)
        except (OSError, TypeError):
            pass

    def _previous(self, repository_id: str, ref: str, rkp_hash: str) -> Manifest | None:
        repository = self.repository(repository_id) or {"snapshots": []}
        old = next((s for s in reversed(repository["snapshots"]) if s["ref"] == ref and s["rkp_hash"] != rkp_hash and not s.get("retracted")), None)
        if old is None:  # a new ref: compare with the newest snapshot on any ref
            old = next((s for s in reversed(repository["snapshots"]) if s["rkp_hash"] != rkp_hash and not s.get("retracted")), None)
        return load_manifest(self.store, rkp_hash=old["rkp_hash"]) if old else None

    def _reconcile_lineage(self, manifest: Manifest, previous: Manifest | None, repo_path: Any) -> None:
        if previous is None:
            return
        try:
            renames = lineage.git_renames(repo_path, previous.commit_sha, manifest.commit_sha)
            lineage.store_renames(self.store, previous, manifest, renames)
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                lineage.reconcile(conn, self.store.tenant_id, self.store, manifest, previous, renames)
        except Exception:
            pass  # lineage is a heuristic over a published snapshot; its failure leaves the snapshot usable

    # -- parity: shadow compiles, quarantine and promote (GI-019)

    def _shadow_job(self, job: dict[str, Any], manifest: Manifest, repo_path: Any, configuration: Any) -> None:
        try:
            job["shadow"] = self.shadow_compile(manifest, repo_path, configuration).row()
        except Exception as exc:
            job["shadow"] = {"error": type(exc).__name__}

    def shadow_compile(self, manifest: Manifest, repo_path: Any, configuration: Any = None) -> parity.ParityReport:
        """Compile the same commit cold and compare. On a mismatch, quarantine the
        incremental snapshot, promote the cold one and open an incident naming the differing shards."""
        gateway, policy = configuration or self.compile_configuration(self.workspace_settings()[1])
        with self.maintenance.shared():
            cold = parity.cold_compile(self.store, manifest.repository_id, repo_path, manifest.commit_sha, gateway=gateway, policy=policy).manifest
        report = parity.compare(manifest, cold, self.store, self.store)
        if report.match:
            return report
        t = self.store.tenant_id
        repository = self.repository(manifest.repository_id) or {"snapshots": []}
        entry = next((s for s in repository["snapshots"] if s["rkp_hash"] == manifest.rkp_hash), None)
        with projection.connect(self.conninfo, t) as conn:
            projection.project_snapshot(conn, self.store, cold)
            projection.set_status(conn, t, manifest.rkp_hash, "quarantined")
            incident = "par_" + uuid.uuid4().hex[:16]
            with conn.transaction():
                conn.execute("INSERT INTO parity_incidents (tenant_id, incident_id, repository_id, incremental_rkp_hash, cold_rkp_hash, differing) VALUES (%s,%s,%s,%s,%s,%s)",
                             (t, incident, manifest.repository_id, manifest.rkp_hash, cold.rkp_hash, Jsonb(report.row())))
            self.store.index_put("quarantine", manifest.rkp_hash, self.store.put_json({"incident": incident, "promoted": cold.rkp_hash}))
            projection.audit(conn, t, "parity-sampler", "snapshot.quarantined", manifest.rkp_hash, {"incident": incident, "promoted": cold.rkp_hash, "differing_shards": [s["unit"] for s in report.differing_shards]})
        if entry:
            self._update(manifest.repository_id, snapshot=entry | {"quarantined": True})
            self._promote(manifest.repository_id, cold, entry["ref"], entry)
        return report

    def promote(self, repository_id: str, rkp_hash: str, actor: str) -> dict[str, Any]:
        """Make a snapshot its ref's current one; a quarantined snapshot is released back to `complete`."""
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            row = projection.get_snapshot(conn, self.store.tenant_id, rkp_hash=rkp_hash)
            if row is None or row["repository_id"] != repository_id or row["status"] not in ("complete", "quarantined"):
                raise KeyError(rkp_hash)
            projection.set_status(conn, self.store.tenant_id, rkp_hash, "complete")
            self.store.index_delete("quarantine", rkp_hash)
            projection.audit(conn, self.store.tenant_id, actor, "snapshot.promoted", rkp_hash, {"previous_status": row["status"]})
        repository = self.repository(repository_id) or {"snapshots": []}
        entry = next((s for s in repository["snapshots"] if s["rkp_hash"] == rkp_hash), None)
        manifest = Manifest.model_validate(row | {"status": "complete"})
        ref = entry["ref"] if entry else repository.get("default_branch", "HEAD")
        self._promote(repository_id, manifest, ref, entry)
        return {"rkp_hash": rkp_hash, "ref": ref, "status": "complete"}

    def _promote(self, repository_id: str, manifest: Manifest, ref: str, entry: dict[str, Any] | None) -> None:
        now = time.time()
        base = {k: v for k, v in (entry or {}).items() if k not in ("quarantined", "rkp_hash", "toolchain_hash")}
        self._update(repository_id, snapshot=base | {"rkp_hash": manifest.rkp_hash, "commit_sha": manifest.commit_sha, "ref": ref, "compiled_at": now, "requested_at": now, "toolchain_hash": manifest.toolchain_hash, "promoted": True})
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.ref_published(conn, self.store.tenant_id, repository_id, ref, manifest.commit_sha, manifest.rkp_hash)

    # -- retraction (GI-021)

    def retract(self, repository_id: str, rkp_hash: str, reason: str, actor: str, replacement: str | None = None) -> dict[str, Any]:
        """Tombstone a published snapshot: it becomes unreadable, its content is purged, and reads point at the replacement."""
        t = self.store.tenant_id
        with projection.connect(self.conninfo, t) as conn:
            row = projection.get_snapshot(conn, t, rkp_hash=rkp_hash)
        if row is None or row["repository_id"] != repository_id:
            raise KeyError(rkp_hash)
        repository = self.repository(repository_id) or {"snapshots": []}
        entry = next((s for s in repository["snapshots"] if s["rkp_hash"] == rkp_hash), None)
        if replacement is None and entry is not None:
            usable = [s for s in repository["snapshots"] if s["rkp_hash"] != rkp_hash and not s.get("retracted") and not s.get("quarantined")]
            # The newest on the same ref, else the newest compiled after it on any ref.
            newer = [s for s in usable if s["ref"] == entry["ref"]] or [s for s in usable if s.get("compiled_at", 0) >= entry.get("compiled_at", 0)]
            replacement = max(newer, key=lambda s: s.get("compiled_at", 0))["rkp_hash"] if newer else None
        record = {"reason": reason, "replacement": replacement, "retracted_at": time.time()}
        with self.maintenance.exclusive():
            with projection.connect(self.conninfo, t) as conn:
                projection.retract(conn, t, rkp_hash, record)
                projection.audit(conn, t, actor, "snapshot.retracted", rkp_hash, {"reason": reason, "replacement": replacement})
                if entry is not None:
                    ref_row = registry.tracked_ref(conn, t, repository_id, entry["ref"])
                    if ref_row and ref_row["rkp_hash"] == rkp_hash:
                        target = next((s for s in repository["snapshots"] if s["rkp_hash"] == replacement), None)
                        conn.execute("UPDATE tracked_refs SET rkp_hash=%s, published_sha=%s WHERE tenant_id=%s AND repository_id=%s AND ref=%s",
                                     (replacement, target["commit_sha"] if target else None, t, repository_id, entry["ref"]))
                        conn.commit()
            gc.tombstone(self.store, rkp_hash, record)
            if entry is not None:
                self._update(repository_id, snapshot=entry | {"retracted": True})
            purged = self._collect(strict=False, grace_seconds=0.0, prune=False)
        return {"rkp_hash": rkp_hash, "status": "retracted", **record, "objects_deleted": purged.objects_deleted}

    def rescan_secrets(self, actor: str = "secret-rescan") -> list[str]:
        """Scan the source of every readable snapshot with the current detectors. A
        snapshot whose published output contains a newly found secret is retracted
        and its ref recompiled, so the replacement masks the value."""
        from . import secrets as scanner
        from .publish_secrets import leaked_secrets

        retracted = []
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            rows = [r for r in projection.snapshot_rows(conn, self.store.tenant_id) if r["status"] != "retracted"]
        for row in rows:
            manifest = load_manifest(self.store, rkp_hash=row["rkp_hash"])
            d = manifest and self.store.index_get("path_manifests", manifest.source_tree_hash)
            if not d or not self.store.has(d):
                continue
            pm = self.store.get_json(d)
            entries, late = [], False
            for entry in pm["entries"]:
                blob = "sha256:" + entry["blob_sha256"]
                if not self.store.has(blob):
                    entries.append(entry)
                    continue
                found = scanner.scan(self.store.get_bytes(blob))
                if found != entry.get("secret_findings", []):
                    late = late or any(f not in entry.get("secret_findings", []) for f in found)
                    entry = entry | {"secret_findings": found}
                entries.append(entry)
            if not late:
                continue
            published = [*load_shards(self.store, manifest).values(), self.store.get_json(manifest.coverage_ref), self.store.get_json(manifest.conflicts_ref)]
            if leaked_secrets(self.store, pm | {"entries": entries}, published):
                self.retract(manifest.repository_id, manifest.rkp_hash, "leaked_secret", actor)
                retracted.append(manifest.rkp_hash)
        return retracted

    # -- retention, garbage collection and deletion (GI-021, GI-006)

    def collect_garbage(self, actor: str = "gc", now: float | None = None, grace_seconds: float = 3600.0) -> dict[str, Any]:
        with self.maintenance.exclusive():
            report = self._collect(strict=False, now=now, grace_seconds=grace_seconds, prune=True)
        row = {**report.row(), "webhook_deliveries_pruned": self._prune_deliveries(now)}
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            projection.audit(conn, self.store.tenant_id, actor, "gc.collected", digest(self.store.tenant_id), {k: v for k, v in row.items() if not k.endswith("_hashes")})
        return row

    def _prune_deliveries(self, now: float | None = None) -> int:
        """Forget delivery IDs past the redelivery window, keeping each connection's latest
        (the settings view reports when a connection last received a webhook)."""
        _, document = self.workspace_settings()
        cutoff = (time.time() if now is None else now) - document.retention.webhook_delivery_days * gc.DAY
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            pruned = conn.execute(
                """DELETE FROM webhook_deliveries d WHERE d.tenant_id=%s AND d.received_at < to_timestamp(%s)
                   AND d.received_at < (SELECT max(received_at) FROM webhook_deliveries l
                                        WHERE l.tenant_id=d.tenant_id AND l.connection_id=d.connection_id)""",
                (self.store.tenant_id, cutoff),
            ).rowcount
            conn.commit()
        return pruned

    def _collect(self, *, strict: bool, now: float | None = None, grace_seconds: float = 3600.0, prune: bool = True, keep: set[str] | None = None) -> gc.Report:
        """Mark and sweep; call with `maintenance` held exclusively."""
        now = time.time() if now is None else now
        _, document = self.workspace_settings()
        t = self.store.tenant_id
        with projection.connect(self.conninfo, t) as conn:
            rows = projection.snapshot_rows(conn, t)
            refs = registry.tracked_refs(conn, t)
        repositories = self.repositories()
        if keep is None:
            keep = gc.retained(repositories, refs, rows, document.retention, now) if prune else {r["rkp_hash"] for r in rows if r["status"] != "retracted"}
        dropped = sorted(r["rkp_hash"] for r in rows if r["rkp_hash"] not in keep and r["status"] != "retracted")
        with projection.connect(self.conninfo, t) as conn:
            deleted = projection.drop_snapshots(conn, dropped)
            for repo in repositories:
                gone = {s["rkp_hash"] for s in repo.get("snapshots", []) if s["rkp_hash"] not in keep and not s.get("retracted")}
                if gone:
                    registry.forget_snapshots(conn, t, repo["repository_id"], gone)
        report = gc.collect_store(self.store, keep, document.retention, now, grace_seconds=grace_seconds, strict=strict, mirrors=self._mirrors if prune else None)
        report.dropped_snapshots = dropped
        report.projection_snapshots_deleted = deleted
        return report

    def delete_repository(self, repository_id: str, actor: str) -> dict[str, Any]:
        """Purge a repository from the store, projection, registry, lineage, refs, mirrors and business map.
        The audit row keeps hashes and counts only."""
        t = self.store.tenant_id
        with self.lock:
            for job in self.jobs.values():
                if job["repository_id"] == repository_id and job["status"] not in TERMINAL:
                    job["cancel"] = "the repository is being deleted"
            for key in [k for k in self.timers if k[0] == repository_id]:
                self.timers.pop(key).cancel()
        with self.maintenance.exclusive():
            repository = self.repository(repository_id)
            with projection.connect(self.conninfo, t) as conn:
                rows = projection.snapshot_rows(conn, t, repository_id)
            hashes = {r["rkp_hash"] for r in rows} | {s["rkp_hash"] for s in (repository or {}).get("snapshots", [])}
            if repository is None and not rows:
                raise KeyError(repository_id)
            entity_ids: set[str] = set()
            for h in hashes:
                manifest = load_manifest(self.store, rkp_hash=h)
                if manifest is not None:
                    for shard in load_shards(self.store, manifest).values():
                        entity_ids.update(e["id"] for e in shard["entities"])
            with projection.connect(self.conninfo, t) as conn:
                purged = projection.purge_repository(conn, repository_id, sorted(entity_ids))
            for h in hashes:
                self.store.index_delete(gc.TOMBSTONES, h)
                self.store.index_delete("quarantine", h)
            with projection.connect(self.conninfo, t) as conn:
                keep = {r["rkp_hash"] for r in projection.snapshot_rows(conn, t) if r["status"] != "retracted"}
            report = self._collect(strict=True, prune=False, keep=keep)
            mirrors = gc.mirror_paths(self._mirrors, repository_id)
            for mirror in mirrors:
                import shutil
                shutil.rmtree(mirror, ignore_errors=True)
            self._forget_local_registry(repository_id)
            with self.lock:
                for job_id in [j for j, job in self.jobs.items() if job["repository_id"] == repository_id]:
                    del self.jobs[job_id]
                for key in [k for k in self.ref_slots if k[0] == repository_id]:
                    del self.ref_slots[key]
            self.poll_due.pop(repository_id, None)
            self.reconcile_due.pop(repository_id, None)
        details = {"snapshots": len(hashes), "placements": purged.get("placements", 0), "objects_deleted": report.objects_deleted + report.blobs_evicted, "index_entries_deleted": report.index_entries_deleted, "mirrors_deleted": len(mirrors)}
        with projection.connect(self.conninfo, t) as conn:
            projection.audit(conn, t, actor, "repository.deleted", digest(repository_id), details)
        return {"repository": digest(repository_id), "status": "deleted", **details}

    def _forget_local_registry(self, repository_id: str) -> None:
        """The legacy registry.json is imported at start-up; a deleted repository must not come back from it."""
        path = self._store_root / self.store.tenant_id / "registry.json"
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("repositories", {}).pop(repository_id, None) is not None:
            path.write_text(json.dumps(data), encoding="utf-8")

    def delete_tenant(self, actor: str) -> dict[str, Any]:
        """Purge everything the tenant holds; only an audit row with hashes remains."""
        t = self.store.tenant_id
        with self.lock:
            for job in self.jobs.values():
                if job["status"] not in TERMINAL:
                    job["cancel"] = "the workspace is being deleted"
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
        with self.maintenance.exclusive():
            with projection.connect(self.conninfo, t) as conn:
                rows = projection.purge_tenant(conn)
            self.store.destroy()
            with self.lock:
                self.jobs.clear()
                self.ref_slots.clear()
                self.inflight.clear()
        with projection.connect(self.conninfo, t) as conn:
            projection.audit(conn, t, actor, "tenant.deleted", digest(t), {"rows_deleted": rows})
        return {"tenant": digest(t), "status": "deleted", "rows_deleted": rows}

    def settings_view(self) -> dict[str, Any]:
        version, document = self.workspace_settings()
        gateway, compiler = self.compile_configuration(document)
        current_toolchain = toolchain_hash(gateway, compiler)
        repositories = self.repositories()
        for repo in repositories:
            repo["snapshots"] = [
                snap | {"stale": snap.get("toolchain_hash") != current_toolchain}
                for snap in repo["snapshots"]
            ]
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            checks = dict(conn.execute(
                "SELECT connection_id,document FROM connection_checks WHERE tenant_id=%s",
                (self.store.tenant_id,),
            ))
            hooks = dict(conn.execute(
                "SELECT connection_id,extract(epoch from max(received_at)) FROM webhook_deliveries WHERE tenant_id=%s GROUP BY connection_id",
                (self.store.tenant_id,),
            ))
            author = conn.execute(
                "SELECT author,updated_at FROM workspace_settings_revisions WHERE tenant_id=%s AND version=%s",
                (self.store.tenant_id, version),
            ).fetchone()
        from .extractors import EXTRACTORS
        return {
            "version": version, "settings": document.row(), "environment": settings.environment_settings(),
            "available_models": gateway.describe() if gateway else {},
            "repositories": repositories, "checks": checks, "last_webhook": hooks,
            "author": author[0] if author else None, "updated_at": author[1] if author else None,
            # The GitHub CLI connection only works where `gh` is installed (not in the container image).
            "github_cli": shutil.which("gh") is not None, "credential_store": self.secrets.writable,
            "coverage_domains": list(settings.COVERAGE_DOMAINS), "providers": list(settings.PROVIDERS),
            "extractors": [{
                "id": e.id, "version": e.version,
                "tier": "inventory" if e.id in {"manifest.maven", "manifest.go"} else ("A" if e.id in {"python.source", "typescript.source", "contract.openapi"} else "B"),
            } for e in EXTRACTORS],
        }

    def map_document(self) -> business_map.BusinessMap:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return business_map.read(conn, self.store.tenant_id)

    def _business_snapshots(self, repository_id: str | None = None):
        rows = [r for r in self.repositories() if repository_id is None or r["repository_id"] == repository_id]
        for repo, snap in business_graph.latest(rows):
            manifest = load_manifest(self.store, rkp_hash=snap["rkp_hash"])
            if manifest is None:
                continue
            entities, edges = business_graph.graph(self.store, manifest)
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                aliases = business_graph.identities(conn, self.store.tenant_id, manifest.rkp_hash)
            subjects = business_graph.describe_subjects(repo, manifest, entities, edges, aliases)
            yield manifest, entities, edges, aliases, subjects

    def map_view(self) -> dict[str, Any]:
        current = self.map_document()
        assigned = {row.lineage_id: row.capability_id for row in current.placements if row.status in ("confirmed", "ai") and row.capability_id}
        subjects, dependencies = [], {}
        snapshots = list(self._business_snapshots())
        for manifest, entities, edges, aliases, found in snapshots:
            subjects.extend({k: v for k, v in s.items() if k not in ("features", "evidence_ids")} for s in found)
            for edge in business_graph.capability_dependencies(assigned, entities, edges, aliases, manifest.rkp_hash):
                key = edge["from"], edge["to"]
                if key not in dependencies:
                    dependencies[key] = edge
                else:
                    dependencies[key]["evidence"].extend(edge["evidence"])
                    if edge["resolution"] != "exact":
                        dependencies[key]["resolution"] = "candidate"
        entries = {s["entity_id"]: s["lineage_id"] for s in subjects}
        links, unknowns = cross_repository.candidates(self.store, snapshots)
        for link in links:
            start = assigned.get(entries.get(link["from"], link["from"]))
            end = assigned.get(entries.get(link["to"], link["to"]))
            if start and end and start != end:
                edge = dependencies.setdefault((start, end), {"from": start, "to": end, "resolution": "candidate", "evidence": []})
                edge["resolution"] = "candidate"
                edge["evidence"].extend(item | {"assertion_id": link["id"], "predicate": link["predicate"]} for item in link["evidence"])
        _, config = self.workspace_settings()
        return {
            "cross_repository_links": links, "cross_repository_unknowns": unknowns,
            "map": current.row(), "subjects": subjects,
            "dependencies": list(dependencies.values()),
            "eligible_kinds": config.business_map.component_kinds,
        }

    def queue_placement(self, repository_id: str | None = None) -> None:
        with self.placement_lock:
            self.pending_placements.add(repository_id)
            if self.placement_worker is None:
                self.placement_worker = threading.Thread(target=self._drain_placement, daemon=True, name="business-placement")
                self.placement_worker.start()

    def _drain_placement(self) -> None:
        while True:
            with self.placement_lock:
                if not self.pending_placements:
                    self.placement_worker = None
                    return
                if None in self.pending_placements:
                    target = None
                    self.pending_placements.clear()
                else:
                    target = self.pending_placements.pop()
            for _ in range(2):
                try:
                    self.run_placement(repository_id=target)
                except business_map.StaleVersion:
                    continue
                except Exception:
                    # A placement outage must leave the published RKP usable.
                    break
                else:
                    break

    def run_placement(self, *, repository_id: str | None = None, attempts: int = 1) -> business_map.BusinessMap:
        """Place components on the active map. With `attempts` > 1, a map edited
        during the run is read again and placed again instead of failing."""
        for attempt in range(attempts):
            try:
                with self.placement_run:
                    return self._place_once(repository_id)
            except business_map.StaleVersion:
                if attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable")

    def _place_once(self, repository_id: str | None) -> business_map.BusinessMap:
        current = self.map_document()
        if not current.capabilities():
            return current
        _, document = self.workspace_settings()
        config = settings.effective_compile(document)
        gateway = config.gateway(self.gateway)
        subjects, hints = [], {}
        for manifest, _, _, _, found in self._business_snapshots(repository_id):
            subjects.extend(found)
            declared = business_graph.declared_hints(self.store, manifest)
            by_name: dict[str, list[dict]] = {}
            for subject in found:
                if subject["kind"] == "Component":
                    by_name.setdefault(subject["name"], []).append(subject)
            for name, value in declared.items():
                if len(by_name.get(name, ())) == 1:
                    hints[by_name[name][0]["lineage_id"]] = value
        rows = placement.place(
            current, subjects, hints, document.business_map,
            config.model_functions.get(placement.SPEC_ID, settings.ModelSelection()),
            self.store, gateway, config.allowed_providers, Budget(max_calls=config.decision_budget),
        )
        if rows == current.placements:
            return current
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return business_map.save(conn, self.store.tenant_id, current.model_copy(update={"placements": rows}), "placement")

    def map_for_snapshot(self, manifest: Manifest) -> dict[str, Any]:
        current = self.map_document()
        if not current.placements:
            return {"components": {}}
        capabilities = current.capabilities()
        kinds = set(self.workspace_settings()[1].business_map.component_kinds)
        entities, _ = business_graph.graph(self.store, manifest)
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            aliases = business_graph.identities(conn, self.store.tenant_id, manifest.rkp_hash)
        rows = {row.lineage_id: row for row in current.placements}
        return {
            "components": {
                entity_id: row.row() | {"capability": capabilities[row.capability_id].name if row.capability_id in capabilities else None}
                for entity_id, entity in entities.items() if entity["kind"] in business_graph.SUBJECT_KINDS
                if (row := rows.get(aliases.get(entity_id, entity_id))) is not None
            },
            "version": current.version,
            "capabilities": [{"id": cap.id, "name": cap.name} for cap in capabilities.values()],
            # Components of a kind the placement policy covers; the denominator for "components placed".
            "eligible_components": sum(
                entity["kind"] == "Component" and entity["attributes"].get("component_kind") in kinds
                for entity in entities.values()
            ),
        }

    def dependency_groups(self, manifest: Manifest) -> dict[str, Any]:
        entities, edges = business_graph.graph(self.store, manifest)
        memberships: dict[str, list] = {}
        for edge in edges.values():
            if edge["predicate"] == "PROVIDES":
                memberships.setdefault(edge["object"], []).append(entities[edge["subject"]])
        classified = {row["id"] for rows in memberships.values() for row in rows}
        groups = [{
            "capability": entities[key], "dependencies": sorted(rows, key=lambda r: (r["name"], r["id"])),
        } for key, rows in memberships.items()]
        other = sorted((row for key, row in entities.items() if row["kind"] == "ExternalDependency" and key not in classified), key=lambda r: (r["name"], r["id"]))
        # Where one component declares more than one package for the same capability, name them.
        adjacent, _ = business_graph.related_graph(entities, edges)
        overlaps = []
        for component in (row for row in entities.values() if row["kind"] == "Component"):
            by_capability: dict[str, set[str]] = {}
            for contained in adjacent[component["id"]]["CONTAINS"]:
                if entities.get(contained["object"], {}).get("kind") != "Package":
                    continue
                for dependency in adjacent[contained["object"]]["DEPENDS_ON"]:
                    for provided in adjacent[dependency["object"]]["PROVIDES"]:
                        if provided["object"] in entities and dependency["object"] in entities:
                            by_capability.setdefault(provided["object"], set()).add(dependency["object"])
            overlaps.extend({
                "component": component, "capability": entities[capability],
                "dependencies": sorted((entities[d] for d in found), key=lambda r: (r["name"], r["id"])),
            } for capability, found in by_capability.items() if len(found) > 1)
        overlaps.sort(key=lambda row: (row["component"]["name"], row["capability"]["name"]))
        # The catalogue this snapshot was compiled with, not today's: its version and why each package maps.
        catalogue, rationales = None, {}
        try:
            pinned = CapabilityCatalogue.model_validate(self.store.get_json(self.store.get_json(manifest.toolchain_hash)["capability_catalogue_hash"]))
        except (KeyError, ValueError, FileNotFoundError):
            pinned = None
        if pinned is not None:
            catalogue = {"name": pinned.taxonomy.name, "version": pinned.taxonomy.version}
            names = {item.key: item.name for item in pinned.capabilities}
            by_package = pinned.package_mappings()
            for capability, rows in memberships.items():
                for dependency in rows:
                    key = dependency["natural_key"]
                    for mapping in match_package(by_package, key.get("ecosystem", ""), key.get("name", "")):
                        if names.get(mapping.capability) == entities[capability]["name"]:
                            rationales.setdefault(dependency["id"], {})[capability] = mapping.rationale
        return {
            "groups": sorted(groups, key=lambda g: g["capability"]["name"]), "unclassified": other, "overlaps": overlaps,
            "catalogue": catalogue, "rationales": rationales,
        }

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
