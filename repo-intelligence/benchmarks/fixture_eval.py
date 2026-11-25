"""Ask a real decision model for the component kind of every fixture component, or
(with --decision deployment-granularity) for each fixture's deployment granularity (DM11).

    python benchmarks/fixture_eval.py microsoft-decision [out.json]
    python benchmarks/fixture_eval.py --decision deployment-granularity microsoft-decision [out.json]

For DM11 the expected label is the golden's `granularity` label; fixtures without one
(no deployed application) are skipped. Either output feeds `calibrate.py`.

Unlike a compile, this sends EVERY component to the model (not only the ones
rules leave ambiguous) and offers every label, so the model can be compared
with the rules and with the fixture goldens. Fixtures are synthetic; only
their structured features are sent.
"""

from __future__ import annotations

import json
import posixpath
import sys
import tempfile
from pathlib import Path

from repo_intel.compile.buildgraph import StepRunner
from repo_intel.compile.capabilities import load_catalogue
from repo_intel.extractors import MANIFEST_BASENAMES, extract_file
from repo_intel.compile.compiler import UNIT_PLAN
from repo_intel.compile.fixtures import list_fixtures, load_golden, materialize
from repo_intel.compile.pipeline import stage_snapshot
from repo_intel.storage.store import ObjectStore
from repo_intel.decisions import component as dm
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.runner import Budget, Ledger, RuleResult, evaluate

LABELS = ("service", "frontend_app", "worker", "cli", "library", "tooling")


def component_features(name: str, root: Path) -> list[dict]:
    """(unit root, features) for each package-bearing unit of a fixture's base commit."""
    shas = materialize(name, root / "repo")
    store = ObjectStore(root / "store", "eval")
    runner = StepRunner(store)
    pm = store.get_json(stage_snapshot(store, root / "repo", shas[0]))
    summaries = {e["path"]: extract_file(runner, e) for e in pm["entries"]}
    manifests = [summaries[p] for p in sorted(summaries) if posixpath.basename(p) in MANIFEST_BASENAMES]
    plan = runner.load(runner.run(UNIT_PLAN, {"paths": sorted(summaries), "manifest_summaries": manifests}))
    component_roles = load_catalogue().component_roles()
    out = []
    for unit in plan["units"]:
        feat = runner.load(runner.run(dm.FEATURES_BUILD, {
            "unit": unit, "summaries": [summaries[p] for p in unit["paths"]], "component_roles": component_roles,
        }))["component"]
        if feat and feat["features"]["ecosystem"] in dm.SUPPORTED_ECOSYSTEMS:
            out.append({"root": "" if unit["name"] == "." else unit["name"], "features": feat["features"]})
    return out


def expected_kind(golden: dict, root: str) -> tuple[str | None, str]:
    for e in golden["entities"]:
        if e["kind"] == "Component" and e["natural_key"]["root"] == root and e.get("attributes", {}).get("component_kind"):
            return e["attributes"]["component_kind"], "golden"
    for c in golden.get("decision_cases", []):
        if c["scope_root"] == root:
            return c["human_label"], "golden (ambiguous case, human label)"
    return None, "none"


def granularity_features(name: str, root: Path) -> dict | None:
    """DM11's features for a fixture's base commit, as the compiler builds them."""
    from repo_intel.compile import pipeline
    from repo_intel.decisions import architecture as dm_architecture

    seen: dict[str, dict] = {}
    original = dm_architecture.granularity_rules

    def spy(features):
        seen["features"] = dict(features)
        return original(features)

    shas = materialize(name, root / "repo")
    pipeline.dm_architecture.granularity_rules = spy
    try:
        pipeline.compile_commit(ObjectStore(root / "store", "eval"), root / "repo", "github.com/0", shas[0])
    finally:
        pipeline.dm_architecture.granularity_rules = original
    return seen.get("features")


def eval_granularity(alias: str, gateway: Gateway, out_path: Path | None) -> int:
    from repo_intel.decisions import architecture as dm_architecture

    labels = tuple(dm_architecture.LABEL_TO_ENTRY)
    spec = dm_architecture.GRANULARITY_SPEC
    spec = spec.model_copy(update={"strategy": [s.model_copy(update={"model_ref": alias}) if s.kind == "decision_model" else s for s in spec.strategy]})
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(ObjectStore(Path(tmp) / "ledger", "eval"))
        for name in list_fixtures():
            want = (load_golden(name).get("granularity") or {}).get("label")
            if want is None:
                continue
            features = granularity_features(name, Path(tmp) / name)
            by_rules = dm_architecture.granularity_rules(features)
            r = evaluate(spec, f"{name}:@repo", features, lambda f: RuleResult("ambiguous", candidates=labels), ledger=ledger, gateway=gateway, budget=Budget(max_calls=10), criteria=dm_architecture.GRANULARITY_CRITERIA)
            scores = sorted((r.option_scores or {}).items(), key=lambda kv: -kv[1])
            rows.append({"fixture": name, "rules": by_rules.label or by_rules.outcome, "expected": want, "expected_source": "golden granularity",
                         "model": r.selected_label, "abstain_reason": r.abstain_reason, "top": scores[:2], "answered_by": r.model_ref, "features": features,
                         "agrees": r.selected_label == want, "top_pick": scores[0][0] if scores else None, "top_pick_agrees": bool(scores) and scores[0][0] == want})
            top = ", ".join(f"{k}={v:.3f}" for k, v in scores[:2])
            print(f"{name:26} rules={rows[-1]['rules']:22} expected={want:17} model={r.selected_label or 'ABSTAIN:' + str(r.abstain_reason):28} {top}", flush=True)
    print(f"\nfixtures: {len(rows)}; model answered: {sum(1 for r in rows if r['model'])}; agrees: {sum(1 for r in rows if r['agrees'])}; answered and wrong: {sum(1 for r in rows if r['model'] and not r['agrees'])}")
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


def main() -> int:
    args = sys.argv[1:]
    decision = "component-kind"
    if args[:1] == ["--decision"]:
        decision, args = args[1], args[2:]
    alias = args[0] if args else "decision-default"
    out_path = Path(args[1]) if len(args) > 1 else None
    configured = Gateway.from_env()
    if alias not in configured.aliases:
        print(f"no credentials configured for alias {alias!r}")
        return 2
    model = configured.aliases[alias]
    if hasattr(model, "max_wait"):
        model.max_wait = 90.0
    gateway = Gateway(configured.aliases, frozenset({model.provider}))
    if decision == "deployment-granularity":
        return eval_granularity(alias, gateway, out_path)
    spec = dm.SPEC.model_copy(update={"strategy": [s.model_copy(update={"model_ref": alias}) if s.kind == "decision_model" else s for s in dm.SPEC.strategy]})
    ask_everything = lambda f: RuleResult("ambiguous", candidates=LABELS)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(ObjectStore(Path(tmp) / "ledger", "eval"))
        for name in list_fixtures():
            golden = load_golden(name)
            for comp in component_features(name, Path(tmp) / name):
                by_rules = dm.rules(comp["features"])
                want, source = expected_kind(golden, comp["root"])
                if want is None and by_rules.outcome == "resolved":
                    want, source = by_rules.label, "rules only (no golden)"
                r = evaluate(spec, f"{name}:{comp['root']}", comp["features"], ask_everything, ledger=ledger, gateway=gateway, budget=Budget(max_calls=10), criteria=dm.CRITERIA)
                scores = sorted((r.option_scores or {}).items(), key=lambda kv: -kv[1])
                row = {"fixture": name, "root": comp["root"] or ".", "package": comp["features"]["package"], "rules": by_rules.label or by_rules.outcome, "expected": want, "expected_source": source,
                       "model": r.selected_label, "abstain_reason": r.abstain_reason, "top": scores[:2], "answered_by": r.model_ref, "features": comp["features"]}
                row["agrees"] = None if want is None else (r.selected_label == want)
                row["top_pick"] = scores[0][0] if scores else None  # the model's first choice, whatever the threshold says
                row["top_pick_agrees"] = None if want is None or not scores else (scores[0][0] == want)
                rows.append(row)
                top = ", ".join(f"{k}={v:.3f}" for k, v in scores[:2])
                print(f"{name:26} {row['root']:16} rules={row['rules']:13} expected={str(want):13} model={r.selected_label or 'ABSTAIN:' + str(r.abstain_reason):24} {top}", flush=True)
    answered = [r for r in rows if r["model"]]
    judged = [r for r in rows if r["expected"]]
    print(f"\ncomponents: {len(rows)}; model answered: {len(answered)}; abstained: {len(rows) - len(answered)}")
    print(f"agrees with expected: {sum(1 for r in judged if r['agrees'])} of {len(judged)}; answered and wrong: {sum(1 for r in judged if r['model'] and not r['agrees'])}")
    print(f"top pick agrees with expected (ignoring thresholds): {sum(1 for r in judged if r['top_pick_agrees'])} of {len(judged)}")
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
