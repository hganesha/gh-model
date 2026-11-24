"""Temporal orchestration (spec §3.4, §4.5).

`CompileCommit` is one workflow per (repository, commit, toolchain); that tuple
is the workflow ID, which deduplicates redelivered webhooks and concurrent
requests. It fetches the pinned commit, snapshots it, compiles and projects.
Activities pass IDs, paths and digests only.

`RefCompiler` is one long-lived workflow per (repository, ref). Webhooks and
the reconciliation job signal it with each new head (`signal_with_start`).
It runs one `CompileCommit` child at a time; when a newer head arrives the
running child is cancelled (unless its commit is tagged) and only the newest
waiting head runs next. A head equal to the running or waiting one is
dropped, so duplicate deliveries cause no work. The compile activity
heartbeats through the progress callback, so a cancellation stops it between
steps instead of letting it run to the end.

Configuration comes from the worker's environment: RKP_STORE_ROOT and
RKP_PG_CONNINFO.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

TASK_QUEUE = "rkp-compile"


@dataclass
class CompileRequest:
    tenant_id: str
    repository_id: str
    repo_path: str  # a local repository, or where the fetch activity left the mirror
    commit_sha: str
    ref: str = ""
    remote: str | None = None  # a Git URL to fetch the commit from into the tenant's mirror
    tagged: bool = False  # a tagged commit is never cancelled for a newer head


@dataclass
class CompileOutcome:
    rkp_hash: str
    manifest_digest: str
    steps_executed: int


@dataclass
class RefState:
    published: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    dropped: int = 0


def _store(tenant_id: str):
    from repo_intel.storage.store import ObjectStore
    return ObjectStore(os.environ["RKP_STORE_ROOT"], tenant_id)


@activity.defn
def fetch_activity(req: CompileRequest) -> str:
    """Make the pinned commit available locally; return the path to compile from.

    With `remote`, the commit is fetched shallowly into a bare per-tenant mirror
    (no hooks, no tags, no submodules). Without it, `repo_path` must already
    contain the commit."""
    git = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=user"]
    path = Path(req.repo_path)
    if req.remote:
        name = hashlib.sha256(req.repository_id.encode()).hexdigest() + ".git"
        path = Path(os.environ["RKP_STORE_ROOT"]) / req.tenant_id / "mirrors" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([*git, "init", "-q", "--bare", str(path)], check=True, capture_output=True)
    import fcntl

    with open(str(path) + ".lock" if req.remote else os.devnull, "a") as lock:
        if req.remote:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one fetch per mirror at a time, across workers on this host
        have = subprocess.run([*git, "-C", str(path), "cat-file", "-e", f"{req.commit_sha}^{{commit}}"], capture_output=True)
        if have.returncode != 0:
            if not req.remote:
                raise RuntimeError("the commit is not in the local repository")
            subprocess.run([*git, "-C", str(path), "fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules", req.remote, req.commit_sha], check=True, capture_output=True, timeout=900)
    if req.remote:
        os.utime(path)
    return str(path)


@activity.defn
def snapshot_activity(req: CompileRequest) -> str:
    from repo_intel.compile.pipeline import stage_snapshot
    return stage_snapshot(_store(req.tenant_id), req.repo_path, req.commit_sha)


@activity.defn
def compile_activity(req: CompileRequest, path_manifest_digest: str) -> CompileOutcome:
    from repo_intel.compile.pipeline import stage_compile

    def progress(stage: str, detail: dict) -> None:
        activity.heartbeat(stage)
        if activity.is_cancelled():
            raise CancelledError("superseded by a newer head")

    result = stage_compile(_store(req.tenant_id), req.repository_id, path_manifest_digest, progress=progress)
    return CompileOutcome(result.manifest.rkp_hash, result.manifest_digest, result.stats.total_executed)


@activity.defn
def project_activity(req: CompileRequest, rkp_hash: str) -> None:
    from repo_intel.knowledge import projection, registry
    from repo_intel.compile.pipeline import load_manifest
    store = _store(req.tenant_id)
    manifest = load_manifest(store, rkp_hash=rkp_hash)
    # The schema is created by a migration run as the owner, not by workers.
    with projection.connect(os.environ["RKP_PG_CONNINFO"], req.tenant_id) as conn:
        projection.project_snapshot(conn, store, manifest)
        if req.ref:
            registry.ref_published(conn, req.tenant_id, req.repository_id, req.ref, req.commit_sha, rkp_hash)


def workflow_id(req: CompileRequest) -> str:
    from repo_intel.compile.pipeline import snapshot_key
    return "compile-" + snapshot_key(req.repository_id, req.commit_sha)


def ref_workflow_id(tenant_id: str, repository_id: str, ref: str) -> str:
    return "ref-" + hashlib.sha256(f"{tenant_id}\0{repository_id}\0{ref}".encode()).hexdigest()[:32]


@workflow.defn
class CompileCommit:
    @workflow.run
    async def run(self, req: CompileRequest) -> CompileOutcome:
        opts = {"start_to_close_timeout": timedelta(minutes=20), "retry_policy": RetryPolicy(maximum_attempts=3)}
        path = await workflow.execute_activity(fetch_activity, req, **opts)
        pinned = replace(req, repo_path=path)
        pm = await workflow.execute_activity(snapshot_activity, pinned, **opts)
        outcome = await workflow.execute_activity(compile_activity, args=[pinned, pm], heartbeat_timeout=timedelta(minutes=2), **opts)
        await workflow.execute_activity(project_activity, args=[pinned, outcome.rkp_hash], **opts)
        return outcome


@workflow.defn
class RefCompiler:
    """Coalesce compiles for one ref: newest head wins (spec §4.5)."""

    def __init__(self) -> None:
        self.pending: CompileRequest | None = None
        self.current: CompileRequest | None = None
        self.state = RefState()

    @workflow.signal
    def head(self, req: CompileRequest) -> None:
        if any(r is not None and r.commit_sha == req.commit_sha for r in (self.current, self.pending)):
            self.state.dropped += 1  # a duplicate delivery or a head already queued
            return
        if self.pending is not None:
            self.state.dropped += 1
        self.pending = req

    @workflow.query
    def status(self) -> RefState:
        return self.state

    @workflow.run
    async def run(self, first: CompileRequest | None, idle_seconds: float = 3600.0, state: RefState | None = None) -> RefState:
        self.state = state or self.state
        if first is not None and self.pending is None:
            self.pending = first
        for _ in range(200):
            try:
                await workflow.wait_condition(lambda: self.pending is not None, timeout=timedelta(seconds=idle_seconds))
            except asyncio.TimeoutError:
                return self.state
            self.current, self.pending = self.pending, None
            req = self.current
            # No I/O in workflow code: the child ID derives from the request alone. The
            # counter lets the same head run again after an earlier run was cancelled.
            n = len(self.state.published) + len(self.state.cancelled)
            child = await workflow.start_child_workflow(CompileCommit.run, req, id=f"{workflow.info().workflow_id}-{req.commit_sha[:12]}-{n}")
            task = asyncio.ensure_future(self._outcome(child))
            await workflow.wait_condition(lambda: task.done() or (self.pending is not None and not req.tagged))
            if not task.done():
                child.cancel()
            outcome = await task
            if outcome is None:
                self.state.cancelled.append(req.commit_sha)
            else:
                self.state.published.append(outcome.rkp_hash)
            self.current = None
        workflow.continue_as_new(args=[self.pending, idle_seconds, self.state])

    @staticmethod
    async def _outcome(child) -> CompileOutcome | None:
        try:
            return await child
        except (ChildWorkflowError, CancelledError, ActivityError):
            return None


ACTIVITIES = [fetch_activity, snapshot_activity, compile_activity, project_activity]
WORKFLOWS = [CompileCommit, RefCompiler]
