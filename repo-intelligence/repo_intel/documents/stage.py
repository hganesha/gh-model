"""The documents compile step (contract 9.1), as `compile.pipeline` runs it.

It runs only when the commit holds `.rkp/documents.json`. The pure steps
(`repo_intel.documents.steps`) read the designations, each designated document and the
names they match; when `CompileSettings.business_rules` is on, `dm.document-rules` names
the rules in each document through the ledger. The result is what `repo.compile` writes
(`compile.compiler._document_knowledge`); its `designation` carries the designation
file's blob digest, so the file is part of what the snapshot's content hash covers even
when an edit to it changes nothing else.
"""

from __future__ import annotations

from typing import Any

from repo_intel.compile.buildgraph import Ref, StepRunner
from repo_intel.compile.contracts import entity_id
from repo_intel.compile.ontology import EntityKind
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.overrides import Overrides
from repo_intel.decisions.runner import Budget, Ledger
from repo_intel.documents import format as fmt
from repo_intel.documents import rules as doc_rules
from repo_intel.documents import steps


def designated_documents(runner: StepRunner, repository_id: str, entries: list[dict[str, Any]], exclusions: list[dict[str, Any]], shard_refs: dict[str, Ref], units: list[dict[str, Any]], *, lift_rules: bool, ledger: Ledger, gateway: Gateway | None, budget: Budget, overrides: Overrides, root_unit: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """What `repo.compile` writes for the designated documents (None without a designation
    file), and the rule decisions made, each tagged with its unit."""
    by_path = {e["path"]: e for e in entries}
    designation = by_path.get(fmt.DESIGNATION_FILE)
    if designation is None:
        return None, []
    unit_of = {p: u["name"] for u in units for p in u["paths"]}
    found = runner.load(runner.run(steps.DESIGNATIONS, {"designation": Ref("sha256:" + designation["blob_sha256"], "bytes")}, {
        "secret_findings": designation.get("secret_findings", []),
        "files": [[e["path"], e["parse"]] for e in sorted(entries, key=lambda e: e["path"])],
        "excluded": [[x["path"], x["reason"]] for x in sorted(exclusions, key=lambda x: x["path"])],
    }))
    reads = {}
    for d in found["documents"]:
        e = by_path[d["path"]]
        reads[d["path"]] = runner.run(steps.READ, {"data": Ref("sha256:" + e["blob_sha256"], "bytes")}, {
            "path": d["path"], "language": d["language"], "features": d["features"], "blob": e["blob_sha256"],
            "secret_findings": e.get("secret_findings", []),
        })
    linked = runner.load(runner.run(steps.LINK, {"shards": shard_refs, "reads": reads}, {"units": {p: unit_of.get(p, root_unit) for p in sorted(reads)}}))

    made: list[dict[str, Any]] = []
    features_of = {d["path"]: d["features"] for d in found["documents"]}
    documents = []
    for doc in linked["documents"]:
        doc = {**doc, "features": features_of[doc["path"]], "rules": []}
        if lift_rules and "rules" in doc["features"] and doc["candidates"]:
            artifact = entity_id(repository_id, EntityKind.ARTIFACT, {"path": doc["path"]})
            decision = doc_rules.lift(doc, artifact, ledger=ledger, gateway=gateway, budget=budget).row()
            made.append({**decision, "@unit": doc["unit"]})
            lifted, confirmations = doc_rules.lifted_rules(repository_id, doc, decision, overrides, ledger)
            doc["rules"] = lifted
            made += [{**c, "@unit": doc["unit"]} for c in confirmations]
        documents.append(doc)
    return {
        "designation": {"path": fmt.DESIGNATION_FILE, "blob_sha256": designation["blob_sha256"], "unit": unit_of.get(fmt.DESIGNATION_FILE, root_unit)},
        "entries": found["entries"],
        "documents": documents,
        "unreadable": [{**u, "unit": unit_of.get(u["path"], root_unit)} for u in found["unreadable"]],
        "problems": found["problems"],
        "rule_lifting": "on" if lift_rules else "off",
    }, made
