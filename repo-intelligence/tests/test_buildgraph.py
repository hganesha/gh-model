import pytest

from repo_intel.buildgraph import Ref, Step, StepRunner, UndeclaredInputError


def test_cache_hit_and_version_bump(store):
    calls = []
    def fn(inputs, config):
        calls.append(1)
        return {"n": len(inputs["blob"]) + config.get("add", 0)}
    blob = Ref(store.put_bytes(b"hello"), "bytes")
    runner = StepRunner(store)
    first = runner.run(Step("s", "1", fn), {"blob": blob})
    assert runner.load(first) == {"n": 5}
    assert runner.run(Step("s", "1", fn), {"blob": blob}) == first and len(calls) == 1
    runner.run(Step("s", "2", fn), {"blob": blob})                      # new version: recompute
    runner.run(Step("s", "1", fn), {"blob": blob}, {"add": 1})          # new config: recompute
    runner.run(Step("s", "1", fn), {"blob": Ref(store.put_bytes(b"hi"), "bytes")})  # new input
    assert len(calls) == 4
    assert runner.stats.hits["s"] == 1 and runner.stats.executed["s"] == 4


def test_undeclared_input_fails(store):
    def greedy(inputs, config):
        return inputs["something_else"]
    with pytest.raises(UndeclaredInputError):
        StepRunner(store).run(Step("greedy", "1", greedy), {"declared": 1})


def test_step_has_no_handle_to_the_store(store):
    def probe(inputs, config):
        return sorted(k for k in vars(inputs) if "store" in k and not k.startswith("_StepInputs"))
    assert StepRunner(store).load(StepRunner(store).run(Step("probe", "1", probe), {})) == []


def test_tenants_do_not_share_objects(tmp_path):
    from repo_intel.store import ObjectStore
    a, b = ObjectStore(tmp_path, "a"), ObjectStore(tmp_path, "b")
    d = a.put_bytes(b"same bytes")
    assert a.has(d) and not b.has(d)
    with pytest.raises(ValueError):
        ObjectStore(tmp_path, "../escape")
