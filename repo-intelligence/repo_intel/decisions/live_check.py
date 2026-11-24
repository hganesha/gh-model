"""Smoke-test a real decision model with synthetic component features.

    RKP_MSDECISION_ENDPOINT=... RKP_MSDECISION_DEPLOYMENT=... RKP_MSDECISION_AUTH=azure-cli \\
        python -m repo_intel.decisions.live_check microsoft-decision

Sends only hand-written feature sets (no repository content). Prints, per case,
the choice, the per-option scores and whether the answer survived reshuffling.
"""

from __future__ import annotations

import sys
import tempfile

from repo_intel.storage.store import ObjectStore
from repo_intel.decisions import component as dm
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.runner import Budget, Ledger, RuleResult, evaluate

_NONE = {"http_endpoints_declared_in_code": 0, "depends_on_http_server_framework": False, "depends_on_web_frontend_framework": False, "depends_on_message_queue_or_job_consumer_library": False,
         "has_container_build_with_start_command": False, "declares_command_line_entry_point": False, "declares_importable_library_entry_point": False, "declares_start_script": False,
         "is_workspace_root_that_only_lists_other_packages": False}


def _case(ecosystem: str = "pypi", language: str = "python", **evidence: object) -> dict:
    return {"ecosystem": ecosystem, "package": "example", "source_files_by_language": {language: 6}, "evidence": {**_NONE, **evidence}, "named_dependencies": {"http_server_frameworks": [], "web_frontend_frameworks": [], "message_consumer_libraries": []}}


CASES = {
    "http api with Dockerfile (expect service)": _case(http_endpoints_declared_in_code=7, depends_on_http_server_framework=True, has_container_build_with_start_command=True),
    "queue consumer (expect worker)": _case(depends_on_message_queue_or_job_consumer_library=True, has_container_build_with_start_command=True),
    "importable helpers (expect library)": _case("npm", "typescript", declares_importable_library_entry_point=True),
    "console entry point (expect cli)": _case(declares_command_line_entry_point=True),
    "nothing distinctive (expect decline or low scores)": _case(),
}


def main() -> int:
    alias = sys.argv[1] if len(sys.argv) > 1 else "decision-default"
    gateway = Gateway.from_env()
    if alias not in gateway.aliases:
        print(f"no credentials configured for alias {alias!r}; configured: {sorted(gateway.aliases)}")
        return 2
    gateway = Gateway(gateway.aliases, frozenset({gateway.aliases[alias].provider}))  # explicit, for this run only
    if hasattr(gateway.aliases[alias], "max_wait"):
        gateway.aliases[alias].max_wait = 90.0  # a smoke test can afford to sit out a per-minute limit
    results = run_check(gateway, alias)
    for result in results:
        scores = ", ".join(f"{k}={v:.3f}" for k, v in sorted((result["option_scores"] or {}).items(), key=lambda kv: -kv[1]))
        print(f"{result['case']}\n   -> {result['selected_label'] or 'ABSTAIN'} ({result['abstain_reason'] or 'accepted'}) by {result['model_ref']}; attempts={result['attempts']}\n   scores: {scores or 'none'}")
    failures = any(row["abstain_reason"] in ("model_unavailable", "egress_denied") for row in results)
    return 1 if failures else 0


def run_check(gateway: Gateway, alias: str) -> list[dict]:
    spec = dm.SPEC.model_copy(update={"strategy": [
        step.model_copy(update={"model_ref": alias}) if step.kind == "decision_model" else step
        for step in dm.SPEC.strategy
    ]})
    budget = Budget(max_calls=40)
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(ObjectStore(tmp, "live-check"))
        return [
            {"case": name, **evaluate(
                spec, "ent_live_check", features,
                lambda _: RuleResult("ambiguous", candidates=("service", "worker", "cli", "library", "tooling")),
                ledger=ledger, gateway=gateway, budget=budget, criteria=dm.CRITERIA,
            ).row()}
            for name, features in CASES.items()
        ]


if __name__ == "__main__":
    sys.exit(main())
