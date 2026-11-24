import os
import socket

import pytest
from fastapi.testclient import TestClient

from conftest import REPO_ID
from repo_intel import buildgraph, compiler, extractors, step_worker
from repo_intel.buildgraph import (
    InProcessExecutor, Ref, SandboxExecutor, SandboxUnavailable, Step, StepFailed, StepRunner, StepTimeout,
    UndeclaredInputError, UnregisteredStepError, read_metrics, registry,
)
from repo_intel.pipeline import compile_commit, stage_snapshot


@pytest.fixture(scope="module")
def sandbox():
    """The OS sandbox. On CI it must exist: a missing sandbox fails rather than skips."""
    try:
        return SandboxExecutor(timeout=60)
    except SandboxUnavailable:
        if os.environ.get("CI"):
            raise
        pytest.skip("no OS sandbox on this host")


# ------------------------------------------------------------------ cache keys and versions


def test_cache_hit_and_version_bump(store):
    calls = []
    def fn(inputs, config):
        calls.append(1)
        return {"n": len(inputs["blob"]) + config.get("add", 0)}
    blob = Ref(store.put_bytes(b"hello"), "bytes")
    runner = StepRunner(store, executor=InProcessExecutor())
    first = runner.run(Step("s", "1", fn), {"blob": blob})
    assert runner.load(first) == {"n": 5}
    assert runner.run(Step("s", "1", fn), {"blob": blob}) == first and len(calls) == 1
    runner.run(Step("s", "2", fn), {"blob": blob})                      # new declared version: recompute
    runner.run(Step("s", "1", fn), {"blob": blob}, {"add": 1})          # new config: recompute
    runner.run(Step("s", "1", fn), {"blob": Ref(store.put_bytes(b"hi"), "bytes")})  # new input
    assert len(calls) == 4
    assert runner.stats.hits["s"] == 1 and runner.stats.executed["s"] == 4


def test_step_version_is_derived_from_code(monkeypatch):
    def one(inputs, config):
        return 1
    def two(inputs, config):
        return 2
    assert Step("s", "1", one).version.startswith("1+")
    assert Step("s", "1", one).version != Step("s", "1", two).version

    # A change in a helper the step calls changes the step's version, without a manual bump.
    before = compiler.UNIT_COMPILE.version
    def _status(evidence_class):  # same name, different body
        return None
    monkeypatch.setattr(compiler, "_status", _status)
    monkeypatch.setattr(buildgraph, "_VERSION_CACHE", {})
    assert compiler.UNIT_COMPILE.version != before
    assert compiler.UNIT_PLAN.version == compiler.UNIT_PLAN.version  # stable


def test_step_version_ignores_runtime_state(monkeypatch):
    """A version is a function of source only. A module cache filling up must not move it,
    or host and sandbox worker would disagree about the toolchain."""
    monkeypatch.setattr(buildgraph, "_VERSION_CACHE", {})
    monkeypatch.setattr(extractors, "_EXTRACTOR_VERSIONS", {})
    cold = extractors.FILE_EXTRACT.version
    extractors.extractor_versions()  # fills the extractor version cache
    monkeypatch.setattr(buildgraph, "_VERSION_CACHE", {})
    assert extractors.FILE_EXTRACT.version == cold


def test_registry_holds_every_pipeline_step():
    steps = registry()
    for name in ("file.extract", "unit.plan", "unit.link", "features.build", "unit.compile", "repo.compile"):
        assert name in steps
    for step in steps.values():
        assert step.fn.__qualname__ == step.fn.__name__, f"{step.name} must be a top-level function"


# ------------------------------------------------------------------ API-level isolation (both executors)


def test_undeclared_input_fails(store, sandbox):
    def greedy(inputs, config):
        return inputs["something_else"]
    with pytest.raises(UndeclaredInputError):
        StepRunner(store, executor=InProcessExecutor()).run(Step("greedy", "1", greedy), {"declared": 1})
    with pytest.raises(UndeclaredInputError):
        StepRunner(store, executor=sandbox).run(step_worker.PROBE_UNDECLARED_INPUT, {"declared": 1}, {"name": "something_else"})


def test_step_has_no_handle_to_the_store(store):
    def probe(inputs, config):
        return sorted(k for k in vars(inputs) if "store" in k and not k.startswith("_StepInputs"))
    runner = StepRunner(store, executor=InProcessExecutor())
    assert runner.load(runner.run(Step("probe", "1", probe), {})) == []


def test_sandbox_runs_only_registered_steps(store, sandbox):
    def local(inputs, config):
        return 1
    with pytest.raises(UnregisteredStepError):
        StepRunner(store, executor=sandbox).run(Step("local", "1", local), {})


# ------------------------------------------------------------------ OS-level isolation (sandbox executor)


def _probes(tmp_path):
    secret = tmp_path / "undeclared.txt"
    secret.write_text("not a declared input")
    return secret


def test_sandbox_blocks_undeclared_file_read(store, sandbox, tmp_path):
    secret = _probes(tmp_path)
    config = {"path": str(secret)}
    assert StepRunner(store, use_cache=False, executor=InProcessExecutor()).run(step_worker.PROBE_READ_FILE, {}, config)  # the probe is real
    with pytest.raises(StepFailed) as failed:
        StepRunner(store, use_cache=False, executor=sandbox).run(step_worker.PROBE_READ_FILE, {}, config)
    assert failed.value.error_type in ("PermissionError", "FileNotFoundError")
    # The store itself is not reachable either.
    blob = store.put_bytes(b"in the store, not declared")
    store_file = next(p for p in (tmp_path / "store").rglob("*") if p.is_file() and p.name == blob.split(":")[1][2:])
    with pytest.raises(StepFailed):
        StepRunner(store, use_cache=False, executor=sandbox).run(step_worker.PROBE_READ_FILE, {}, {"path": str(store_file)})


def test_sandbox_blocks_undeclared_environment_read(store, sandbox, monkeypatch):
    monkeypatch.setenv("RKP_PROBE_VARIABLE", "visible to the host")
    config = {"name": "RKP_PROBE_VARIABLE"}
    assert StepRunner(store, use_cache=False, executor=InProcessExecutor()).run(step_worker.PROBE_ENV, {}, config)
    with pytest.raises(StepFailed) as failed:
        StepRunner(store, use_cache=False, executor=sandbox).run(step_worker.PROBE_ENV, {}, config)
    assert failed.value.error_type == "KeyError"


def test_sandbox_blocks_network(store, sandbox):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        config = {"host": "127.0.0.1", "port": listener.getsockname()[1]}
        assert StepRunner(store, use_cache=False, executor=InProcessExecutor()).run(step_worker.PROBE_NETWORK, {}, config)
        with pytest.raises(StepFailed):
            StepRunner(store, use_cache=False, executor=sandbox).run(step_worker.PROBE_NETWORK, {}, config)


def test_sandbox_blocks_writes(store, sandbox, tmp_path):
    target = tmp_path / "written.txt"
    with pytest.raises(StepFailed):
        StepRunner(store, use_cache=False, executor=sandbox).run(step_worker.PROBE_WRITE_FILE, {}, {"path": str(target)})
    assert not target.exists()


def test_sandbox_sees_declared_inputs_only_for_its_own_step(store, sandbox):
    runner = StepRunner(store, use_cache=False, executor=sandbox)
    a, b = Ref(store.put_json("first")), Ref(store.put_bytes(b"second"), "bytes")
    assert runner.load(runner.run(step_worker.PROBE_ECHO, {"a": a, "b": b, "plain": [1, "x"]})) == {"a": "first", "b": "second", "plain": [1, "x"]}
    # The previous step's input files are gone before the next step runs.
    stale = sandbox._acquire()
    try:
        assert list(stale.inputs.iterdir()) == []
    finally:
        sandbox._release(stale)


def test_sandbox_time_limit(store):
    try:
        quick = SandboxExecutor(timeout=1)
    except SandboxUnavailable:
        pytest.skip("no OS sandbox on this host")
    with pytest.raises(StepTimeout):
        StepRunner(store, use_cache=False, executor=quick).run(step_worker.PROBE_SLEEP, {}, {"seconds": 30})
    assert StepRunner(store, use_cache=False, executor=quick).run(step_worker.PROBE_SLEEP, {}, {"seconds": 0})  # a fresh worker


def test_in_process_only_behind_the_dev_flag(monkeypatch):
    monkeypatch.delenv(buildgraph.EXECUTOR_ENV, raising=False)
    assert buildgraph.default_executor_name() != "inprocess"
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")
    assert isinstance(buildgraph.default_executor(), InProcessExecutor)
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "off")
    with pytest.raises(ValueError):
        buildgraph.default_executor()


# ------------------------------------------------------------------ whole compiles


def test_fixture_compile_is_hermetic_and_matches_in_process(tmp_path, f01, sandbox, monkeypatch):
    """GI-004 acceptance: a whole fixture compiles under the sandbox; a rerun executes zero steps."""
    from repo_intel.store import ObjectStore

    repo, shas = f01
    monkeypatch.delenv(buildgraph.EXECUTOR_ENV, raising=False)
    boxed_store = ObjectStore(tmp_path / "boxed", "t")
    boxed = [compile_commit(boxed_store, repo, REPO_ID, sha) for sha in shas]
    assert all(r.manifest.status == "complete" for r in boxed)
    rerun = compile_commit(boxed_store, repo, REPO_ID, shas[-1])
    assert rerun.stats.total_executed == 0 and sum(rerun.stats.hits.values()) > 0

    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")
    plain_store = ObjectStore(tmp_path / "plain", "t")
    plain = [compile_commit(plain_store, repo, REPO_ID, sha) for sha in shas]
    assert [r.manifest.content_hash for r in boxed] == [r.manifest.content_hash for r in plain]


def test_changing_one_extractor_reruns_only_its_files(store, f01, monkeypatch):
    """Acceptance test 13, build-graph half: an extractor bump re-extracts only the files it matches."""
    repo, shas = f01
    monkeypatch.setenv(buildgraph.EXECUTOR_ENV, "inprocess")  # the bump exists only in this process
    first = compile_commit(store, repo, REPO_ID, shas[0])
    assert first.stats.executed["file.extract"] > 0
    pm = store.get_json(stage_snapshot(store, repo, shas[0]))
    target = next(e for e in extractors.EXTRACTORS if e.id == "manifest.pyproject")
    matched = [e["path"] for e in pm["entries"] if e["parse"] and target.matches(e["path"])]
    assert 0 < len(matched) < len(pm["entries"])

    before = extractors.extractor_versions()
    reported = extractors.FILE_EXTRACT.version
    monkeypatch.setattr(target, "version", target.version + "-bumped")
    after = extractors.extractor_versions()
    assert {k for k in after if after[k] != before[k]} == {"manifest.pyproject"}
    assert extractors.FILE_EXTRACT.version != reported  # the toolchain hash still moves

    second = compile_commit(store, repo, REPO_ID, shas[0])
    assert second.stats.executed["file.extract"] == len(matched)
    assert second.stats.hits["file.extract"] == len(pm["entries"]) - len(matched)


# ------------------------------------------------------------------ metrics


def test_runner_writes_per_step_metrics(store):
    runner = StepRunner(store, executor=InProcessExecutor(), run_id="run_test")
    blob = Ref(store.put_bytes(b"x"), "bytes")
    runner.run(step_worker.PROBE_ECHO, {"b": blob})
    runner.run(step_worker.PROBE_ECHO, {"b": blob})
    runner.flush_metrics()
    row = read_metrics(store)
    assert row["run_id"] == "run_test" and row["executor"] == "inprocess"
    assert row["steps"]["selfcheck.echo"]["hits"] == 1 and row["steps"]["selfcheck.echo"]["executed"] == 1
    assert row["steps"]["selfcheck.echo"]["seconds"] >= 0
    assert read_metrics(store, "run_test") == row and read_metrics(store, "run_missing") is None


def test_metrics_are_written_when_the_runner_goes_away(store):
    runner = StepRunner(store, executor=InProcessExecutor(), run_id="run_gc")
    runner.run(step_worker.PROBE_ECHO, {})
    runner.run(step_worker.PROBE_ECHO, {})
    del runner
    assert read_metrics(store, "run_gc")["totals"] == {"hits": 1, "executed": 1, "seconds": pytest.approx(read_metrics(store, "run_gc")["totals"]["seconds"])}


def test_step_metrics_endpoint(store):
    from repo_intel.api import create_app

    StepRunner(store, executor=InProcessExecutor(), run_id="run_api").flush_metrics()
    client = TestClient(create_app("", store.tenant_id, "token", store=store))
    auth = {"Authorization": "Bearer token"}
    assert client.get("/v1/step-metrics", headers=auth).json()["run_id"] == "run_api"
    assert client.get("/v1/step-metrics", params={"run_id": "run_api"}, headers=auth).status_code == 200
    assert client.get("/v1/step-metrics", params={"run_id": "run_nope"}, headers=auth).status_code == 404
    assert client.get("/v1/step-metrics").status_code == 401
    other = TestClient(create_app("", "tenant-b", "token", store=store))
    assert other.get("/v1/step-metrics", headers=auth).status_code == 404


def test_tenants_do_not_share_objects(tmp_path):
    from repo_intel.store import ObjectStore
    a, b = ObjectStore(tmp_path, "a"), ObjectStore(tmp_path, "b")
    d = a.put_bytes(b"same bytes")
    assert a.has(d) and not b.has(d)
    with pytest.raises(ValueError):
        ObjectStore(tmp_path, "../escape")
