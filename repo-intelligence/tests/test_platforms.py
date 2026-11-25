"""The shared foundation for platform packs (docs/solution-ir-contract.md): the core kinds and
relations, the pack registry and its checks, per-construct coverage, and the behavioural IR."""

import pytest

from repo_intel import extractors, platforms
from repo_intel.compile import behavior, compiler, verify
from repo_intel.compile.contracts import Entity, Span
from repo_intel.compile.ontology import RELATIONS, EntityKind, Predicate, check_relation
from repo_intel.platforms import Construct, PlatformPack, Term

K = EntityKind
AT = Span(start_byte=0, end_byte=4, start_line=1, end_line=1)


class _Extractor:
    def __init__(self, id):
        self.id, self.version = id, "1.0.0"

    def matches(self, path):
        return path.endswith(".demo")

    def extract(self, path, data, blob):  # pragma: no cover - not run here
        raise NotImplementedError


DEMO = PlatformPack(
    id="demo", name="Demo platform", prefix="demo", version="1.0.0",
    terms=(
        Term("demo:Page", K.SCREEN, "lossy", "a page template"),
        Term("demo:Lookup", K.CODE_LIST, "exact", "a lookup table"),
    ),
    constructs=(
        Construct("demo:lock", "a named lock", "behavior:lock"),
        Construct("demo:page", "a page template", "entity:Screen"),
        Construct("demo:magic", "a construct with no neutral form", "unmapped"),
    ),
    extractors=(_Extractor("demo.source"),),
    supersedes=("legacy.cfml",),
)


@pytest.fixture
def demo(monkeypatch):
    registry = platforms._load()
    monkeypatch.setitem(registry, "PACKS", {"demo": DEMO})
    monkeypatch.setitem(registry, "TERMS", {t.name: (DEMO, t) for t in DEMO.terms})
    monkeypatch.setitem(registry, "CONSTRUCTS", {c.id: (DEMO, c) for c in DEMO.constructs})
    return DEMO


# ------------------------------------------------------------------ ontology


def test_every_new_kind_can_be_related():
    new = {K.SCREEN, K.FORM, K.UI_CONTROL, K.STYLE_TOKEN, K.REPORT, K.BUSINESS_RULE, K.WORKFLOW, K.CODE_LIST, K.ROLE, K.PERMISSION}
    related = {k for schema in RELATIONS.values() for k in schema.subjects | schema.objects}
    assert new <= related


@pytest.mark.parametrize("triple, ok", [
    ((Predicate.SUBMITS_TO, K.FORM, K.ENDPOINT), True),
    ((Predicate.SUBMITS_TO, K.SCREEN, K.ENDPOINT), False),
    ((Predicate.RENDERS, K.PROGRAM, K.SCREEN), True),
    ((Predicate.CONTAINS, K.SCREEN, K.FORM), True),
    ((Predicate.CONTAINS, K.FORM, K.UI_CONTROL), True),
    ((Predicate.BINDS_TO, K.UI_CONTROL, K.FIELD), True),
    ((Predicate.ENFORCES, K.PROGRAM, K.BUSINESS_RULE), True),
    ((Predicate.APPLIES_TO, K.BUSINESS_RULE, K.FIELD), True),
    ((Predicate.REQUIRES_ROLE, K.ENDPOINT, K.ROLE), True),
    ((Predicate.GRANTS, K.ROLE, K.PERMISSION), True),
    ((Predicate.READS_FROM, K.PROGRAM, K.CODE_LIST), True),
    ((Predicate.PRODUCES, K.PROGRAM, K.REPORT), True),
])
def test_relation_schema_for_the_new_layers(triple, ok):
    assert (check_relation(*triple) is None) is ok


# ------------------------------------------------------------------ the pack registry


def test_pack_definitions_are_checked():
    assert platforms.validate_pack(DEMO) == []
    bad = PlatformPack(
        id="Bad-Pack", name="x", prefix="bad", version="1",
        terms=(Term("other:Thing", K.SCREEN, "exact", ""), Term("other:Thing", K.SCREEN, "exact", "")),
        constructs=(Construct("bad:x", "", "entity:NoSuchKind"), Construct("bad:y", "", "guess")),
        extractors=(_Extractor("someone.else"),),
    )
    problems = " | ".join(platforms.validate_pack(bad))
    for expected in ("lower-case", "duplicate term", "'bad:<Name>'", "unknown kind 'NoSuchKind'", "modelled_as 'guess'", "must start with 'Bad-Pack.'"):
        assert expected in problems, expected


def test_registered_packs_are_valid():
    for pack in platforms.packs().values():
        assert platforms.validate_pack(pack) == [], pack.id


def test_platform_entities_carry_their_term(demo):
    o = platforms.platform_entity("b" * 64, AT, "demo:Page", {"platform": "demo", "name": "orders"}, "orders", {"title": "Orders"})
    payload = o.payload
    assert payload["kind"] == "Screen"
    assert payload["attributes"] == {"title": "Orders", "platform": "demo", "platform_kind": "demo:Page", "fidelity": "lossy"}
    exact = platforms.platform_entity("b" * 64, AT, "demo:Lookup", {"platform": "demo", "name": "status"}, "status")
    assert "fidelity" not in exact.payload["attributes"]
    with pytest.raises(KeyError):
        platforms.platform_entity("b" * 64, AT, "demo:Nope", {}, "x")
    with pytest.raises(KeyError):
        platforms.construct("b" * 64, AT, "demo:nope", True)


def test_the_publish_gate_checks_platform_kinds(demo):
    assert platforms.check_platform_kind("Screen", {"platform": "demo", "platform_kind": "demo:Page"}) is None
    assert platforms.check_platform_kind("Screen", {}) is None
    assert "maps to Screen, not Form" in platforms.check_platform_kind("Form", {"platform": "demo", "platform_kind": "demo:Page"})
    assert "not in any registered" in platforms.check_platform_kind("Screen", {"platform": "demo", "platform_kind": "demo:Gone"})
    assert "belongs to platform demo" in platforms.check_platform_kind("Screen", {"platform": "cfml", "platform_kind": "demo:Page"})

    def shard(attributes):
        row = Entity.make("r", K.SCREEN, {"platform": "demo", "name": "p"}, "p", attributes=attributes).row()
        return {"unit": ".", "entities": [row], "assertions": [], "evidence": [], "unknowns": []}

    def problems(attributes):
        return verify.verify_snapshot(repository_id="r", path_manifest={"entries": []}, shards={".": shard(attributes)}, shard_digests={}, coverage=[], coverage_ref="", conflicts=[], conflicts_ref="", content_hash="", required_coverage=[], hypothesis_decisions=set())

    assert not [p for p in problems({"platform": "demo", "platform_kind": "demo:Page"}) if "platform_kind" in p]
    assert [p for p in problems({"platform": "demo", "platform_kind": "demo:Lookup"}) if "maps to CodeList, not Screen" in p]


def test_packs_add_extractors_and_replace_the_ones_they_supersede(demo):
    merged = {e.id for e in extractors._with_packs(extractors._discover())}
    assert "demo.source" in merged and "legacy.cfml" not in merged and "legacy.sas" in merged
    v = extractors.extractor_version(DEMO.extractors[0])
    extractors._EXTRACTOR_VERSIONS.clear()
    changed = PlatformPack(**{**DEMO.__dict__, "terms": DEMO.terms[:1]})
    platforms._load()["PACKS"] = {"demo": changed}
    assert extractors.extractor_version(changed.extractors[0]) != v, "a vocabulary change re-extracts the pack's files"
    extractors._EXTRACTOR_VERSIONS.clear()


def test_superseding_an_unknown_extractor_is_refused(demo, monkeypatch):
    monkeypatch.setattr(platforms, "superseded", lambda: {"legacy.nothing"})
    with pytest.raises(AssertionError, match="unknown extractors"):
        extractors._with_packs(extractors._discover())


def test_construct_coverage_per_platform(demo):
    artifact = lambda path, constructs: Entity.make("r", K.ARTIFACT, {"path": path}, path, attributes={"language": "demo", "parsed": True, "constructs": constructs}).row()
    shards = {
        ".": {"entities": [artifact("a.demo", {"demo:lock": {"seen": 3, "modelled": 1}}), artifact("b.demo", {"demo:lock": {"seen": 1, "modelled": 1}, "demo:magic": {"seen": 2, "modelled": 0}})]},
        "x": {"entities": [Entity.make("r", K.SCREEN, {"platform": "demo", "name": "p"}, "p").row()]},
    }
    [entry] = compiler.platform_construct_coverage(shards)
    assert entry["domain"] == "platform_constructs" and entry["scope"] == "demo"
    assert (entry["numerator"], entry["denominator"]) == (2, 6)
    assert entry["detail"]["constructs"] == {"demo:lock": {"seen": 4, "modelled": 2}, "demo:magic": {"seen": 2, "modelled": 0}}
    assert entry["detail"]["not_seen"] == ["demo:page"]
    assert compiler.platform_construct_coverage({".": {"entities": []}}) == []


# ------------------------------------------------------------------ behavioural IR

OWNER = {"kind": "Program", "natural_key": {"platform": "cfml", "name": "orders"}, "name": "orders"}


def _unit(body, params=None):
    return {"owner": OWNER, "language": "cfml", "params": params or [], "body": body}


VALID = [
    {"op": "assign", "at": [0, 10], "target": {"e": "var", "scope": "local", "name": "total"}, "value": {"e": "lit", "value": 0, "type": "number"}},
    {"op": "if", "cond": {"e": "binop", "op": ">", "left": {"e": "var", "scope": "form", "name": "qty"}, "right": {"e": "lit", "value": 0}},
     "then": [{"op": "query", "sql": "UPDATE orders SET qty = :qty", "params": [{"name": "qty", "value": {"e": "var", "scope": "form", "name": "qty"}, "type": "integer"}], "datasource": "dsn"}],
     "else": [{"op": "redirect", "target": {"e": "lit", "value": "error.cfm"}}]},
    {"op": "loop", "kind": "each", "var": {"e": "var", "name": "row"}, "over": {"e": "var", "name": "rows"}, "body": [
        {"op": "call", "call": {"e": "call", "callee": {"e": "name", "name": "writeLog", "kind": "builtin"}, "args": [{"e": "template", "parts": ["row ", {"e": "var", "name": "row"}]}]}},
    ]},
    {"op": "try", "body": [{"op": "lock", "name": "orders", "body": [{"op": "transaction", "body": []}]}], "catches": [{"type": "database", "var": "e", "body": [{"op": "throw"}]}]},
    {"op": "switch", "on": {"e": "var", "name": "status"}, "cases": [{"match": [{"e": "lit", "value": "A"}], "body": [{"op": "break"}]}], "default": []},
    {"op": "platform", "construct": "sas:retain", "detail": {"vars": ["total"]}},
    {"op": "output", "channel": "response", "value": {"e": "member", "object": {"e": "var", "name": "order"}, "name": "id"}},
]


def test_a_valid_unit_passes_and_every_node_is_walked():
    assert behavior.validate_unit(_unit(VALID, [{"name": "id", "type": "integer", "required": True}])) == []
    nodes = list(behavior.walk(VALID))
    assert {n.get("op") for n in nodes} >= {"assign", "if", "query", "redirect", "loop", "call", "try", "lock", "transaction", "throw", "switch", "break", "platform", "output"}
    assert {n.get("e") for n in nodes} >= {"var", "lit", "binop", "call", "name", "template", "member"}


@pytest.mark.parametrize("body, message", [
    ([{"op": "goto"}], "not a statement"),
    ([{"op": "if", "cond": {"e": "lit", "value": True}}], "needs 'then'"),
    ([{"op": "assign", "target": {"e": "var", "name": "x"}, "value": {"e": "binop", "op": "**", "left": {"e": "lit", "value": 1}, "right": {"e": "lit", "value": 2}}}], "unknown binary op"),
    ([{"op": "loop", "kind": "forever", "body": []}], "unknown loop kind"),
    ([{"op": "query", "sql": 3}], "sql must be text"),
    ([{"op": "exit", "at": [5, 2]}], "bad span"),
    ([{"op": "call", "call": {"e": "call", "callee": {"e": "name", "name": "f"}, "args": [{"value": 1}]}}], "not an expression"),
    ("not a list", "must be a list"),
])
def test_invalid_units_say_why(body, message):
    problems = behavior.validate_unit(_unit(body))
    assert any(message in p for p in problems), problems


def test_behavior_observations_are_validated_before_emission():
    o = behavior.behavior_observation("b" * 64, AT, OWNER, "cfml", VALID)
    assert o.kind == "behavior" and o.payload["schema"] == behavior.SCHEMA and o.payload["body"] == VALID
    with pytest.raises(ValueError, match="invalid behaviour unit"):
        behavior.behavior_observation("b" * 64, AT, {"kind": "Program"}, "cfml", VALID)


def test_units_may_say_what_they_are_and_mark_inline_params():
    o = behavior.behavior_observation("b" * 64, AT, OWNER, "sas", VALID, kind="data_step")
    assert o.payload["kind"] == "data_step"
    assert any("unknown unit kind" in p for p in behavior.validate_unit({**_unit(VALID), "kind": "lambda"}))
    inline = [{"op": "query", "sql": "SELECT * FROM t_:m", "params": [{"name": "m", "value": {"e": "var", "name": "m"}, "inline": True}]}]
    assert behavior.validate_unit(_unit(inline)) == []
    inline[0]["params"][0]["inline"] = "yes"
    assert any("inline must be true or false" in p for p in behavior.validate_unit(_unit(inline)))


def test_constructs_carry_an_evidence_class(demo):
    from repo_intel.compile.ontology import EvidenceClass

    assert platforms.construct("b" * 64, AT, "demo:lock", True).evidence_class == EvidenceClass.E2
    assert platforms.construct("b" * 64, AT, "demo:lock", True, EvidenceClass.E1).evidence_class == EvidenceClass.E1


def test_platform_attributes_merge_together():
    merge = compiler._merge_attributes
    jcl, sas = {"platform": "mainframe", "dsorg": "PS"}, {"platform": "sas", "platform_kind": "sas:Dataset", "fidelity": "lossy"}
    assert merge(jcl, sas) == {"dsorg": "PS", "platform": "sas", "platform_kind": "sas:Dataset", "fidelity": "lossy", "platforms": ["mainframe", "sas"]}
    assert merge(sas, jcl) == merge(jcl, sas)
    assert merge({"platform": "a"}, {"z": 1}) == {"platform": "a", "z": 1}, "neutral declarations merge as before"


def test_discovery_refuses_a_pack_that_is_still_importing(tmp_path):
    import subprocess
    import sys

    probe = "import sys, types, importlib.util\n" \
        "import repo_intel.platforms as p\n" \
        "m = types.ModuleType('repo_intel.platforms.zz'); m.__spec__ = importlib.util.spec_from_loader('repo_intel.platforms.zz', loader=None); m.__spec__._initializing = True\n" \
        "sys.modules['repo_intel.platforms.zz'] = m\n" \
        "import pkgutil; real = pkgutil.iter_modules\n" \
        "pkgutil.iter_modules = lambda path=None: [*real(path), pkgutil.ModuleInfo(None, 'zz', True)] if list(path or []) == list(p.__path__) else real(path)\n" \
        "try:\n    p.packs()\nexcept RuntimeError as e:\n    print('refused' if 'still importing' in str(e) else e)\n"
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert run.stdout.strip() == "refused", run.stderr
