import random

from hypothesis import given, strategies as st

from repo_intel.contracts import Assertion, Evidence, Span, assertion_id, entity_id
from repo_intel.hashing import digest, make_id, merkle_root, rows_digest

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-2**53 + 1, 2**53 - 1) | st.text(max_size=20),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)


def _shuffled_keys(value, rng):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: _shuffled_keys(v, rng) for k, v in items}
    if isinstance(value, list):
        return [_shuffled_keys(v, rng) for v in value]
    return value


@given(json_values, st.integers())
def test_digest_ignores_key_order(value, seed):
    assert digest(value) == digest(_shuffled_keys(value, random.Random(seed)))


@given(st.lists(st.dictionaries(st.text(max_size=5), st.integers(0, 9), max_size=3), max_size=6, unique_by=lambda d: str(sorted(d.items()))), st.integers())
def test_rows_digest_ignores_row_order(payloads, seed):
    rows = [{"id": f"r{i}", **{f"k{k}": v for k, v in p.items()}} for i, p in enumerate(payloads)]
    shuffled = rows[:]
    random.Random(seed).shuffle(shuffled)
    assert rows_digest(rows) == rows_digest(shuffled)


def test_merkle_root_ignores_insertion_order_and_sees_changes():
    a = {"x": "sha256:" + "1" * 64, "y": "sha256:" + "2" * 64}
    assert merkle_root(a) == merkle_root(dict(reversed(a.items())))
    assert merkle_root(a) != merkle_root({**a, "y": "sha256:" + "3" * 64})


def test_ids_are_content_derived_and_prefixed():
    a = entity_id("github.com/1", "Package", {"name": "x", "ecosystem": "pypi"})
    assert a == entity_id("github.com/1", "Package", {"ecosystem": "pypi", "name": "x"})
    assert a != entity_id("github.com/2", "Package", {"ecosystem": "pypi", "name": "x"})
    assert a.startswith("ent_") and len(a) == 30
    assert make_id("as", "s", "P", "o", {}) == assertion_id("s", "P", "o", {})


def test_tampered_rows_are_rejected():
    import pytest
    ev = Evidence.make("ab" * 32, Span(start_byte=0, end_byte=4, start_line=1, end_line=1), "file")
    with pytest.raises(ValueError):
        Evidence.model_validate({**ev.row(), "blob_sha256": "cd" * 32})
    ok = Assertion.make("ent_a", "DEPENDS_ON", "ent_b", assertion_kind="observed_fact", epistemic_status="supported", evidence_class="E1", evidence_ids=[ev.id])
    with pytest.raises(ValueError):
        Assertion.model_validate({**ok.row(), "object": "ent_c"})
    with pytest.raises(ValueError):  # model judgment cannot be 'supported' without a verifier
        Assertion.make("ent_a", "DEPENDS_ON", "ent_b", assertion_kind="inferred_relation", epistemic_status="supported", evidence_class="E5", evidence_ids=[ev.id])
    with pytest.raises(ValueError):  # no assertion without evidence
        Assertion.make("ent_a", "DEPENDS_ON", "ent_b", assertion_kind="observed_fact", epistemic_status="supported", evidence_class="E1", evidence_ids=[])
