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


@pytest.mark.skipif(os.environ.get("RKP_TEMPORAL_TESTS") != "1", reason="starts a Temporal dev server; opt in with RKP_TEMPORAL_TESTS=1")
def test_ref_workflow_coalesces_rapid_pushes_and_duplicates(tmp_path, pg_conninfo, monkeypatch):
    """Acceptance test 12 on the durable route: ten heads signalled in quick succession,
    each delivered twice, publish one snapshot, for the last head."""
    import asyncio
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from conftest import REPO_ID
    from repo_intel import projection, registry
    from repo_intel.fixtures import materialize
    from repo_intel.workflows import ACTIVITIES, TASK_QUEUE, WORKFLOWS, CompileRequest, RefCompiler, ref_workflow_id

    origin = tmp_path / "origin"
    shas = materialize("F01-python-api", origin)[:1]
    for i in range(9):
        (origin / "NOTES.md").write_text(f"note {i}\n")
        subprocess.run(["git", "-C", str(origin), "add", "NOTES.md"], check=True)
        subprocess.run(["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", f"note {i}"], check=True)
        shas.append(subprocess.check_output(["git", "-C", str(origin), "rev-parse", "HEAD"], text=True).strip())
    monkeypatch.setenv("RKP_STORE_ROOT", str(tmp_path / "store"))
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    monkeypatch.setenv("RKP_PG_CONNINFO", pg_conninfo.replace("user=rkp ", "user=rkp_app "))
    requests = [CompileRequest("tenant-a", REPO_ID, "", sha, ref="main", remote=f"file://{origin}") for sha in shas]
    wid = ref_workflow_id("tenant-a", REPO_ID, "main")

    async def run():
        async with await WorkflowEnvironment.start_local() as env:
            with ThreadPoolExecutor() as pool:
                async with Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=ACTIVITIES, activity_executor=pool):
                    handle = await env.client.start_workflow(RefCompiler.run, args=[None, 3.0], id=wid, task_queue=TASK_QUEUE)
                    for req in requests:
                        await handle.signal(RefCompiler.head, req)
                        await handle.signal(RefCompiler.head, req)  # a redelivered webhook
                    return await handle.result()

    state = asyncio.run(run())
    with psycopg.connect(pg_conninfo) as conn:
        rows = projection.snapshot_rows(conn, "tenant-a")
        ref = registry.tracked_ref(conn, "tenant-a", REPO_ID, "main")
    assert [r["commit_sha"] for r in rows] == [shas[-1]], "one published snapshot, for the final head"
    assert len(state.published) == 1 and ref["rkp_hash"] == state.published[0] and ref["published_sha"] == shas[-1]
    assert state.dropped >= len(shas), "every redelivery, and every head replaced while waiting, is dropped without a compile"
    assert len(state.published) + len(state.cancelled) + state.dropped == 2 * len(shas)
    assert len(state.cancelled) <= 3, "only a head that had started is cancelled"
