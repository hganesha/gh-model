"""Run GI-041 on seeded fixtures and optionally three pinned public repositories."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from ..business_graph import describe_subjects, graph
from ..business_map import BusinessMap, Capability, Stage
from ..fixtures import list_fixtures, materialize
from ..pipeline import compile_commit
from ..settings import ModelSelection, PlacementPolicy
from ..store import ObjectStore
from . import placement
from .gateway import Gateway
from .runner import Budget

CAPABILITIES = [
    ("customers", "Customer management", "Create customer records and maintain their profiles."),
    ("orders", "Order fulfilment", "Create and track orders and process order changes."),
    ("catalogue", "Product catalogue", "Browse the catalogue and maintain product listings and prices."),
    ("notifications", "Customer notifications", "Send email messages and order notifications."),
    ("audit", "Order audit", "Track order events and preserve audit records."),
    ("availability", "Service monitoring", "Expose health and status so operators can monitor availability."),
    ("identity", "User account administration", "Manage users, authentication, login and passwords."),
    ("support", "Support assistance", "Answer customer support questions using an assistant."),
    ("analysis", "Warehouse analytics", "Report and analyse orders, customers and warehouse data."),
    ("calculation", "Calculator", "Perform arithmetic and show the result on desktop, web and mobile."),
    ("forecast", "Demand planning", "Forecast demand and plan purchasing and supply."),
    ("returns", "Returns management", "Process returned goods, refunds and exchanges."),
]
FIXTURE_LABELS = {
    "F01": {"": "customers"},
    "F02": {"apps/api": "orders", "apps/web": "orders", "apps/worker": None, "": None, "packages/shared": None, "packages/ui": None},
    "F03": {"backend": "catalogue", "frontend": "catalogue"},
    "F06": {"": "availability"},
    "F07": {"services/audit": "audit", "services/notifier": "notifications", "services/orders": "orders"},
    "F08": {"": "support"},
    "F09": {"": "analysis"},
}
PUBLIC = {
    "fastapi/full-stack-fastapi-template": (
        "77e550a73bb49dd2967622c5ed11bf7a49603c93",
        "Full-stack web application template with FastAPI, React and PostgreSQL",
        {"backend": "identity", "frontend": "identity", "packages/react-email": "notifications", "": None},
    ),
    "developit/express-es6-rest-api": (
        "9b8c005a38a0de820eac7e319e81b4318c320630", "Starter project for an ES6 RESTful Express API",
        {"": None},
    ),
    "benoitvallon/react-native-nw-react-calculator": (
        "4f9628fa75d5ad2c2b8d29b611768990087abc15", "Calculator that runs on the web and on desktop and mobile devices",
        {"": "calculation"},
    ),
}


def samples(store: ObjectStore, directory: Path, include_public: bool) -> tuple[list[dict], dict]:
    rows, commits = [], {}
    for name in [*list_fixtures(), *(PUBLIC if include_public else ())]:
        repo = directory / name.replace("/", "_")
        if name in PUBLIC:
            sha, description, labels = PUBLIC[name]
            subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)
            subprocess.run([
                "git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}", "-c", "credential.helper=",
                "fetch", "-q", "--depth=1", "--no-tags", f"https://github.com/{name}.git", sha,
            ], check=True, timeout=60)
        else:
            sha = materialize(name, repo)[0]
            description, labels = "", FIXTURE_LABELS.get(name[:3], {})
        commits[name] = sha
        result = compile_commit(store, repo, name, sha)
        entities, edges = graph(store, result.manifest)
        for row in describe_subjects({"repository_id": name, "full_name": name, "description": description}, result.manifest, entities, edges, {}):
            if row["kind"] != "Component":
                continue
            root = entities[row["entity_id"]]["natural_key"]["root"]
            rows.append(row | {"root": root, "expected": labels[root]})
    return rows, commits


def run(store: ObjectStore, rows: list[dict], model_aliases: list[str], threshold: float) -> dict:
    policy = PlacementPolicy(threshold=threshold)
    caps = tuple(Capability(id=key, name=name, description=description) for key, name, description in CAPABILITIES)
    sample_map = BusinessMap(stages=(Stage(id="sample", name="Sample capabilities", capabilities=caps),))
    vectors = {c.id: placement.local_embedding(placement._cap_text(c)) for c in caps}
    details, summary = [], {}
    eligible = [row for row in rows if row["component_kind"] in policy.component_kinds]
    for row in rows:
        ranked = placement.rank_candidates(placement.local_embedding(placement._subject_text(row["features"])), vectors)
        details.append({
            "repository": row["repository_id"], "root": row["root"], "name": row["name"],
            "kind": row["component_kind"], "eligible": row in eligible, "expected": row["expected"], "shortlist": ranked,
        })
    positives = [row for row in details if row["eligible"] and row["expected"]]
    allowed = tuple(p for p in os.environ.get("RKP_ALLOW_PROVIDERS", "").split(",") if p)
    gateway = Gateway.from_env(frozenset(allowed)) if model_aliases else None
    for alias in [None, *model_aliases]:
        if alias is not None:
            gateway.resolve(alias)  # Fail before sending data if the model is missing or egress is denied.
        placed = placement.place(
            sample_map, eligible, {}, policy, ModelSelection(alias=alias), store, gateway if alias else None,
            allowed, Budget(max_calls=200),
        )
        by_id = {row.lineage_id: row for row in placed}
        counts = {"accepted_correct": 0, "accepted_wrong": 0, "abstained": 0}
        cases = []
        for subject in eligible:
            row = by_id[subject["lineage_id"]]
            if row.capability_id is None:
                counts["abstained"] += 1
            else:
                label = "accepted_correct" if row.capability_id == subject["expected"] else "accepted_wrong"
                counts[label] += 1
            cases.append({"name": subject["name"], "choice": row.capability_id, "score": row.score, "reason": row.reason})
        summary[alias or "rules-only"] = counts | {"cases": cases}
    return {
        "coverage": {
            "components": len(rows), "eligible": len(eligible), "eligible_with_label": len(positives),
            "shortlist_hits": sum(row["expected"] in row["shortlist"] for row in positives),
        },
        "threshold_tested": threshold, "decisions": summary, "labels": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true", help="include the three pinned public samples")
    parser.add_argument("--model", action="append", default=[], help="configured model alias, requires an explicit egress allow-list")
    parser.add_argument("--threshold", type=float, default=0.88)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="rkp-placement-bench-") as tmp:
        directory = Path(tmp)
        store = ObjectStore(directory / "store", "benchmark")
        rows, commits = samples(store, directory, args.public)
        result = run(store, rows, args.model, args.threshold)
    print(json.dumps({"commits": commits, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
