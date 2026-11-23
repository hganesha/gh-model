"""Temporal orchestration (spec §3.4).

One workflow per (repository, commit, toolchain); that tuple is the workflow
ID, which deduplicates redelivered webhooks and concurrent requests. Activities
pass IDs and digests only. Configuration comes from the worker's environment:
RKP_STORE_ROOT and RKP_PG_CONNINFO.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow

TASK_QUEUE = "rkp-compile"


@dataclass
class CompileRequest:
    tenant_id: str
    repository_id: str
    repo_path: str
    commit_sha: str


@dataclass
class CompileOutcome:
    rkp_hash: str
    manifest_digest: str
    steps_executed: int


def _store(tenant_id: str):
    from .store import ObjectStore
    return ObjectStore(os.environ["RKP_STORE_ROOT"], tenant_id)


@activity.defn
def snapshot_activity(req: CompileRequest) -> str:
    from .pipeline import stage_snapshot
    return stage_snapshot(_store(req.tenant_id), req.repo_path, req.commit_sha)


@activity.defn
def compile_activity(req: CompileRequest, path_manifest_digest: str) -> CompileOutcome:
    from .pipeline import stage_compile
    result = stage_compile(_store(req.tenant_id), req.repository_id, path_manifest_digest)
    return CompileOutcome(result.manifest.rkp_hash, result.manifest_digest, result.stats.total_executed)


@activity.defn
def project_activity(req: CompileRequest, rkp_hash: str) -> None:
    from . import projection
    from .pipeline import load_manifest
    store = _store(req.tenant_id)
    manifest = load_manifest(store, rkp_hash=rkp_hash)
    # The schema is created by a migration run as the owner, not by workers.
    with projection.connect(os.environ["RKP_PG_CONNINFO"], req.tenant_id) as conn:
        projection.project_snapshot(conn, store, manifest)


def workflow_id(req: CompileRequest) -> str:
    from .pipeline import snapshot_key
    return "compile-" + snapshot_key(req.repository_id, req.commit_sha)


@workflow.defn
class CompileCommit:
    @workflow.run
    async def run(self, req: CompileRequest) -> CompileOutcome:
        opts = {"start_to_close_timeout": timedelta(minutes=20)}
        pm = await workflow.execute_activity(snapshot_activity, req, **opts)
        outcome = await workflow.execute_activity(compile_activity, args=[req, pm], **opts)
        await workflow.execute_activity(project_activity, args=[req, outcome.rkp_hash], **opts)
        return outcome


ACTIVITIES = [snapshot_activity, compile_activity, project_activity]
