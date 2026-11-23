"""Rules-only classification of declared external dependencies."""

from __future__ import annotations

from ..capabilities import CapabilityMapping
from ..contracts import DecisionResult, decision_id
from ..hashing import digest

SPEC_ID = "dm.dependency-capability"
SPEC_VERSION = "1.0.0"


def decision(
    subject: str,
    evidence_ids: list[str],
    catalogue_hash: str,
    mapping: CapabilityMapping | None,
) -> DecisionResult:
    # Each positive match is independent. One dependency can provide multiple capabilities.
    label = mapping.capability if mapping else None
    feature_hash = digest({
        "scope": subject,
        "evidence_ids": evidence_ids,
        "catalogue_hash": catalogue_hash,
        "capability": label,
    })
    spec = f"{SPEC_ID}@{SPEC_VERSION}"
    return DecisionResult(
        decision_id=decision_id(spec, feature_hash, ""),
        spec=spec,
        scope_entity=subject,
        feature_set_hash=feature_hash,
        rule_outcome="resolved" if mapping else "insufficient_evidence",
        selected_label=label,
        confidence=mapping.confidence if mapping else None,
        evidence_ids=evidence_ids,
        abstained=mapping is None,
        abstain_reason=None if mapping else "no_catalogue_mapping",
        decision_method="rules",
    )
