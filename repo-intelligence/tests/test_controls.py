"""Contract 9.2, the engine follow-ups of wave W7.8: rules a file enforces, Form and control
keys that follow their Screen's `app`, and display names for the new languages.

The Web Forms application root rule is tested with the ASP.NET pack
(`tests/test_platform_aspnet.py`); the owner controls (Explorer, VS Code) have their own
TypeScript tests.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from conftest import REPO_ID
from repo_intel.compile.buildgraph import StepInputs
from repo_intel.compile.compiler import _lifted_knowledge, _Shard
from repo_intel.compile.contracts import Entity, Evidence
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation
from repo_intel.decisions import rules as dm_rules
from repo_intel.decisions.gateway import Gateway, ScriptedLlm
from repo_intel.decisions.overrides import Overrides
from repo_intel.decisions.runner import Ledger
from repo_intel.extractors.base import span
from repo_intel.knowledge import profile
from repo_intel.knowledge.bundle import _architecture_shards
from repo_intel.storage.store import ObjectStore


def _git_repo(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _compile(tmp_path: Path, files: dict[str, str]):
    from repo_intel.compile.pipeline import compile_commit, load_shards

    sha = _git_repo(tmp_path / "repo", files)
    store = ObjectStore(tmp_path / "store", "t")
    shards = load_shards(store, compile_commit(store, tmp_path / "repo", REPO_ID, sha).manifest)
    return {e["id"]: e for s in shards.values() for e in s["entities"]}, [a for s in shards.values() for a in s["assertions"]]


# ---------------------------------------------------------------- Form and control keys


SIGN_IN = """export default function SignIn() {
  return <form action="/api/session" method="post"><input name="email" type="email" /><a href="/help">Help</a><button type="submit">Go</button></form>;
}
"""


def _keys(ents, kind):
    return sorted((tuple(sorted(e["natural_key"].items())) for e in ents.values() if e["kind"] == kind))


def test_forms_and_controls_on_a_screen_that_gains_app_gain_it_too(tmp_path):
    files = {"package.json": '{"name": "suite"}', "apps/shop/app/signin/page.tsx": SIGN_IN, "apps/admin/app/signin/page.tsx": SIGN_IN}
    ents, assertions = _compile(tmp_path, files)
    apps = ("apps/admin", "apps/shop")
    assert _keys(ents, "Form") == [(("app", a), ("name", "form1"), ("screen", "/signin")) for a in apps]
    assert _keys(ents, "UIControl") == [(("app", a), ("container", "/signin#form1"), ("name", n)) for a in apps for n in ("email", "link:/help", "submit")]
    # Every relation end follows its entity: each app's Screen contains its own Form, and
    # each Form its own controls, none shared between the apps.
    contains = sorted((ents[a["subject"]]["kind"], ents[a["subject"]]["natural_key"]["app"], ents[a["object"]]["kind"], ents[a["object"]]["natural_key"]["app"])
                      for a in assertions if a["predicate"] == "CONTAINS" and ents[a["object"]]["kind"] in ("Form", "UIControl"))
    assert contains == sorted([("Screen", a, "Form", a) for a in apps] + [("Form", a, "UIControl", a) for a in apps for _ in range(3)])
    defined = {(ents[a["subject"]]["natural_key"].get("app"), ents[a["object"]]["natural_key"]["path"]) for a in assertions
               if a["predicate"] == "DEFINED_IN" and ents[a["subject"]]["kind"] in ("Form", "UIControl")}
    assert defined == {("apps/admin", "apps/admin/app/signin/page.tsx"), ("apps/shop", "apps/shop/app/signin/page.tsx")}


def test_forms_on_views_and_single_router_units_keep_their_keys(tmp_path):
    view = 'export default function Contact() { return <form name="contact"><input name="email" /></form>; }\n'
    files = {"package.json": '{"name": "suite"}', "apps/shop/app/signin/page.tsx": SIGN_IN, "apps/admin/app/signin/page.tsx": SIGN_IN,
             "apps/shop/components/Contact.tsx": view}
    ents, _ = _compile(tmp_path, files)
    # A view's Screen is named by its file: it has no router, so its Form and controls do not change.
    assert (("name", "contact"), ("screen", "apps/shop/components/Contact.tsx")) in _keys(ents, "Form")
    assert (("container", "apps/shop/components/Contact.tsx#contact"), ("name", "email")) in _keys(ents, "UIControl")

    ents, _ = _compile(tmp_path / "one", {"package.json": '{"name": "shop"}', "app/signin/page.tsx": SIGN_IN})
    assert _keys(ents, "Form") == [(("name", "form1"), ("screen", "/signin"))], "one router: keys stay as they were"
    assert all("app" not in k for k in (dict(x) for x in _keys(ents, "UIControl")))


def test_a_form_whose_file_declares_its_screen_under_two_apps_keeps_its_key():
    from repo_intel.compile.compiler import _screen_apps

    def entity(kind, key, attrs, at=(0, 10)):
        return {"kind": "entity", "evidence": {"span": {"start_byte": at[0], "end_byte": at[1]}}, "payload": {"kind": kind, "natural_key": key, "name": "x", "attributes": attrs}}

    screen = lambda router: entity("Screen", {"platform": "web", "name": "/"}, {"platform": "web", "router": router})
    form = entity("Form", {"screen": "/", "name": "form1"}, {"platform": "web"}, (20, 30))
    other = entity("Form", {"screen": "/", "name": "form1"}, {"platform": "cfml"}, (0, 5))
    rel = {"kind": "relation", "evidence": {"span": {"start_byte": 40, "end_byte": 50}},
           "payload": {"subject": {"kind": "Form", "natural_key": {"screen": "/", "name": "form1"}}, "predicate": "SUBMITS_TO", "object": "@file"}}
    summaries = [{"path": "src/routers.tsx", "observations": [screen("src/routers.tsx#a"), screen("src/routers.tsx#b"), form, rel]},
                 {"path": "legacy/index.cfm", "observations": [other]}]
    out = _screen_apps(summaries, "")
    [shop_form] = [o for o in out[0]["observations"] if o["payload"].get("kind") == "Form"]
    assert "app" not in shop_form["payload"]["natural_key"], "two apps declare '/' in that file: which one the form is on is not known"
    assert out[0]["observations"][3]["payload"]["subject"]["natural_key"] == {"screen": "/", "name": "form1"}
    assert out[1] == summaries[1]


# ---------------------------------------------------------------- rules a file enforces


def test_a_symbol_with_no_container_lets_the_artifact_of_its_candidates_enforce_the_rule(tmp_path):
    data = b"def check(qty):\n    if qty < 1:\n        raise ValueError('Quantity must be at least one')\n"
    blob = hashlib.sha256(data).hexdigest()
    ev = Evidence.make(blob, span(data, 0, len(data)), "behavior").row()
    artifact = Entity.make(REPO_ID, EntityKind.ARTIFACT, {"path": "orders/checks.py"}, "checks.py").row()
    symbol = Entity.make(REPO_ID, EntityKind.SYMBOL, {"package": "orders", "qualified_name": "orders.checks.check"}, "check").row()
    guard = {"op": "if", "at": [16, 90], "cond": {"e": "binop", "op": "<", "left": {"e": "var", "name": "qty"}, "right": {"e": "lit", "value": 1, "type": "number"}},
             "then": [{"op": "throw", "at": [36, 90], "value": {"e": "lit", "value": "Quantity must be at least one", "type": "string"}}]}
    shard = {"unit": "@root", "entities": [artifact, symbol], "assertions": [], "unknowns": [], "evidence": [ev],
             "behaviors": [{"id": "b1", "owner": symbol["id"], "artifact": artifact["id"], "kind": "function", "evidence_ids": [ev["id"]], "body": [guard]}]}
    [group] = dm_rules._features(StepInputs(lambda ref: None, {"shards": {"@root": shard}}), {})["groups"]
    assert group["containers"] == [] and group["candidates"][0]["artifact"] == artifact["id"]
    assert "artifact" not in dm_rules.model_features(group)["candidates"][0], "an entity id is never sent to the model"

    answer = {"rules": [{"name": "quantity-at-least-one", "statement": "Quantity must be at least one.", "rule_kind": "validation", "candidate_ids": ["c1"]}]}
    store = ObjectStore(tmp_path / "store", "t")
    gateway = Gateway({}, frozenset({"scripted"}), {"llm-default": ScriptedLlm(lambda _: answer)})
    decision = dm_rules.lift(group, ledger=Ledger(store), gateway=gateway, budget=None).row()
    [rule], _ = dm_rules.lifted_rules(REPO_ID, group, decision, Overrides(), Ledger(store))
    assert rule["owner"] == symbol["id"] and rule["containers"] == [artifact["id"]]
    assert check_relation(Predicate.ENFORCES, EntityKind.ARTIFACT, EntityKind.BUSINESS_RULE) is None

    g = _Shard("@global")
    _lifted_knowledge(REPO_ID, g, {"rules": {"rules": [rule]}})
    rows = g.rows()
    shards = {"@root": {**shard, "behaviors": []}, "@global": rows}
    tier = _architecture_shards({k: {kk: vv for kk, vv in v.items() if kk != "behaviors"} for k, v in shards.items()}, [])
    kept = [(a["subject"], a["qualifiers"]) for s in tier.values() for a in s["assertions"] if a["predicate"] == "ENFORCES"]
    assert kept == [(artifact["id"], {"basis": "rule_lifting", "via": "symbol"})], "the file enforces the rule where the Symbol is left out"


def test_a_symbol_with_a_container_keeps_it_and_not_the_file():
    group = {"owner": "ent_sym", "owner_kind": "Symbol", "owner_name": "check", "containers": ["ent_prog"], "evidence": [{"id": "ev_a"}],
             "candidates": [{"id": "c1", "kind": "guard", "path": "a.cfc", "artifact": "ent_file", "at": [0, 5], "evidence_id": "ev_a", "applies_to": []}]}
    decision = {"decision_id": "d1", "abstained": False, "hypotheses": [{"statement": "s", "evidence_ids": ["ev_a"]}], "alternatives": ["validation positive c1"], "decision_method": "llm"}

    class NoOverrides:
        set_hash = "none"

        def label_for(self, spec, entity):
            return None

    [rule], made = dm_rules.lifted_rules(REPO_ID, group, decision, NoOverrides(), None)
    assert rule["containers"] == ["ent_prog"] and made == []
    [rule], _ = dm_rules.lifted_rules(REPO_ID, {**group, "owner_kind": "Program", "containers": []}, decision, NoOverrides(), None)
    assert rule["containers"] == [], "a Program owner is in the architecture tier itself"


# ---------------------------------------------------------------- display names


def test_display_names_for_server_pages_the_data_estate_and_documents():
    for language in ("jsp", "aspnet", "plsql", "teradata", "shell", "batch", "powershell", "ssis", "datastage", "abinitio", "hive", "pig"):
        assert profile.LANGUAGES[language] != language, language
    assert profile.LANGUAGES["aspnet"] == "ASP.NET" and profile.LANGUAGES["plsql"] == "Oracle PL/SQL"
    for language in ("markdown", "text", "csv", "html", "docx", "xlsx", "pdf"):
        assert profile.language_name(language) != language, language
    assert profile.language_name("docx") == "Word document (DOCX)" and profile.language_name("yaml") == "yaml"
    # Documents name a format; they are never counted as what the application is written in.
    coverage = [{"domain": "parse", "scope": "a", "detail": {"by_language": {"plsql": {"files": 3}, "markdown": {"files": 9}, "docx": {"files": 2}}}}]
    languages, _ = profile._languages(coverage)
    assert [(v["text"], v["detail"]) for v in languages] == [("Oracle PL/SQL", "100% of source files")]
    values, _ = profile._application_type([], ["aspnet", "dbproc", "informatica"])
    assert values[0]["text"] == "Legacy application" and values[0]["detail"] == "ASP.NET, Database procedures (DB2, Netezza, Sybase), Informatica PowerCenter"
