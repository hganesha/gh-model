import os

import pytest

pytestmark = pytest.mark.temporal


@pytest.mark.skipif(os.environ.get("RKP_TEMPORAL_TESTS") != "1", reason="starts a Temporal dev server, which the SDK downloads on first use; opt in with RKP_TEMPORAL_TESTS=1")
def test_compile_workflow_end_to_end(tmp_path, f01, pg_conninfo, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from conftest import REPO_ID
    from repo_intel import projection
    from repo_intel.workflows import ACTIVITIES, TASK_QUEUE, CompileCommit, CompileRequest, workflow_id

    repo, shas = f01
    monkeypatch.setenv("RKP_STORE_ROOT", str(tmp_path / "store"))
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
    monkeypatch.setenv("RKP_PG_CONNINFO", pg_conninfo.replace("user=rkp ", "user=rkp_app "))
    req = CompileRequest("tenant-a", REPO_ID, str(repo), shas[0])

    async def run():
        async with await WorkflowEnvironment.start_local() as env:
            with ThreadPoolExecutor() as pool:
                async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[CompileCommit], activities=ACTIVITIES, activity_executor=pool):
                    first = await env.client.execute_workflow(CompileCommit.run, req, id=workflow_id(req), task_queue=TASK_QUEUE)
                    second = await env.client.execute_workflow(CompileCommit.run, req, id=workflow_id(req) + "-again", task_queue=TASK_QUEUE)
                    return first, second

    first, second = asyncio.run(run())
    assert first.rkp_hash == second.rkp_hash and first.steps_executed > 0 and second.steps_executed == 0
    with psycopg.connect(pg_conninfo) as conn:
        assert projection.get_snapshot(conn, "tenant-a", rkp_hash=first.rkp_hash)["commit_sha"] == shas[0]
