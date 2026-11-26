"""Platform packs: a platform's extractors, vocabulary and construct checklist (docs/solution-ir-contract.md).

The ontology is two layers. Entities always carry a neutral core kind
(`repo_intel.compile.ontology.EntityKind`), which every consumer and target reads. A
pack adds a vocabulary of platform terms (`cf:Template`, `sas:Format`), each mapped to
one core kind with a recorded fidelity:

    exact      the core kind says everything the platform construct means
    lossy      the core kind is right, but native semantics live only in attributes
    unmapped   no core kind fits yet; the entity uses the nearest one and says so

An entity declared through a term carries `attributes.platform` and
`attributes.platform_kind`; the publish gate (`repo_intel.compile.verify`) refuses an
entity whose platform kind is not registered or does not map to its core kind.

A pack also lists the constructs of its platform it looks for. Extractors report each
occurrence with `construct(...)`, saying whether it was modelled, and the compiler turns
the counts into `platform_constructs` coverage per platform, so a view can tell "not
found" from "not looked for".

Each pack is a subpackage of this package that defines `PACK`. Packs are discovered,
never imported by name, and need no change to shared engine files. Discovery is lazy and
runs when `repo_intel.extractors` finishes loading, so a pack imports
`repo_intel.extractors.base` and other extractor modules only inside functions, never at
module level, and never the `repo_intel.extractors` package itself; discovery refuses a
pack that is still importing rather than silently leaving it out. A pack's extractors
run with the others; `supersedes` names extractors the pack replaces (for example
`legacy.cfml`), which then no longer run.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from repo_intel.compile.contracts import Observation, Span
from repo_intel.compile.ontology import EntityKind, EvidenceClass

Fidelity = Literal["exact", "lossy", "unmapped"]
_TERM = re.compile(r"^[a-z][a-z0-9]*:[A-Za-z][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Term:
    """A platform construct that becomes an entity: `cf:Template` -> Screen."""

    name: str  # "<prefix>:<Name>"
    core: EntityKind
    fidelity: Fidelity
    description: str


@dataclass(frozen=True)
class Construct:
    """A platform construct the pack looks for, modelled or not: `cf:cflock`."""

    id: str  # "<prefix>:<name>"
    description: str
    modelled_as: str  # what it becomes: "entity:Screen", "behavior:lock", "relation:CALLS", or "unmapped"


@dataclass(frozen=True)
class PlatformPack:
    id: str  # the platform's value in attributes.platform: "cfml", "sas", "web"
    name: str
    prefix: str  # term and construct prefix: "cf", "sas", "web"
    version: str
    terms: tuple[Term, ...] = ()
    constructs: tuple[Construct, ...] = ()
    extractors: tuple[Any, ...] = ()
    supersedes: tuple[str, ...] = ()  # extractor ids this pack replaces
    inputs: tuple[str, ...] = field(default=())  # out-of-repository inputs it reads, by file pattern (native extraction)
    # Coverage rows the pack computes from compiled shards (unit name -> shard rows), beyond
    # `platform_constructs`: the web pack's `accessibility`, say. `repo.compile` calls it for
    # every pack that sets it, in pack id order.
    coverage: Callable[[dict[str, dict]], list[dict]] | None = None

    def term(self, name: str) -> Term:
        for t in self.terms:
            if t.name == name:
                return t
        raise KeyError(f"{self.id}: no term {name}")


def validate_pack(pack: PlatformPack) -> list[str]:
    problems = []
    if not re.match(r"^[a-z][a-z0-9_]*$", pack.id):
        problems.append(f"pack id {pack.id!r} must be lower-case letters, digits and underscores")
    names = [t.name for t in pack.terms]
    ids = [c.id for c in pack.constructs]
    for label, items in (("term", names), ("construct", ids)):
        if len(items) != len(set(items)):
            problems.append(f"{pack.id}: duplicate {label}")
        for item in items:
            if not _TERM.match(item) or not item.startswith(pack.prefix + ":"):
                problems.append(f"{pack.id}: {label} {item!r} must be '{pack.prefix}:<Name>'")
    for c in pack.constructs:
        kind, _, target = c.modelled_as.partition(":")
        if c.modelled_as != "unmapped" and kind not in ("entity", "relation", "behavior", "attribute", "transform"):
            problems.append(f"{pack.id}: construct {c.id} modelled_as {c.modelled_as!r} is not entity:/relation:/behavior:/attribute:/transform: or unmapped")
        if kind == "entity" and target not in {k.value for k in EntityKind}:
            problems.append(f"{pack.id}: construct {c.id} names unknown kind {target!r}")
        if kind == "transform":
            # A construct that becomes a transform shape (an ETL lookup, a sort stage) names its op.
            from repo_intel.compile.transforms import OPS

            if target not in OPS:
                problems.append(f"{pack.id}: construct {c.id} names unknown transform op {target!r}")
    for e in pack.extractors:
        if not getattr(e, "id", "").startswith(pack.id + "."):
            problems.append(f"{pack.id}: extractor id {getattr(e, 'id', None)!r} must start with '{pack.id}.'")
    return problems


def _discover() -> dict[str, PlatformPack]:
    found: dict[str, PlatformPack] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        if getattr(module.__spec__, "_initializing", False):
            raise RuntimeError(
                f"platform pack {module.__name__} is still importing: import repo_intel.extractors before it, "
                "and import repo_intel.extractors modules inside functions, not at module level"
            )
        pack = getattr(module, "PACK", None)
        if pack is None:
            raise RuntimeError(f"{module.__name__} defines no PACK")
        problems = validate_pack(pack)
        if problems:
            raise RuntimeError("invalid platform pack: " + "; ".join(problems))
        if pack.id in found:
            raise RuntimeError(f"duplicate platform pack {pack.id}")
        found[pack.id] = pack
    return found


_registry: dict[str, Any] = {}


def _load() -> dict[str, Any]:
    if not _registry:
        import repo_intel.extractors  # noqa: F401  (loads extractor base modules; it calls back into this function)

        if not _registry:
            packs = _discover()
            _registry.update(
                PACKS=packs,
                TERMS={t.name: (p, t) for p in packs.values() for t in p.terms},
                CONSTRUCTS={c.id: (p, c) for p in packs.values() for c in p.constructs},
            )
    return _registry


def __getattr__(name: str) -> Any:
    # PACKS, TERMS and CONSTRUCTS are built on first use (see the module docstring).
    if name in ("PACKS", "TERMS", "CONSTRUCTS"):
        return _load()[name]
    raise AttributeError(name)


def packs() -> dict[str, PlatformPack]:
    return _load()["PACKS"]


def pack_extractors() -> list[Any]:
    return [e for p in packs().values() for e in p.extractors]


def superseded() -> set[str]:
    return {x for p in packs().values() for x in p.supersedes}


def pack_fingerprint(pack: PlatformPack) -> dict[str, Any]:
    """What of a pack, beyond its extractors' code, changes compiled output: folded into the toolchain."""
    return {
        "version": pack.version,
        "terms": [[t.name, t.core.value, t.fidelity] for t in pack.terms],
        "constructs": [[c.id, c.modelled_as] for c in pack.constructs],
        "supersedes": sorted(pack.supersedes),
    }


def check_platform_kind(kind: str, attributes: dict[str, Any]) -> str | None:
    """Why an entity's platform attributes are inconsistent, or None."""
    name = attributes.get("platform_kind")
    if name is None:
        return None
    terms = _load()["TERMS"]
    if name not in terms:
        return f"platform_kind {name!r} is not in any registered platform pack"
    pack, term = terms[name]
    if term.core.value != kind:
        return f"platform_kind {name} maps to {term.core.value}, not {kind}"
    if attributes.get("platform") != pack.id:
        return f"platform_kind {name} belongs to platform {pack.id}, not {attributes.get('platform')!r}"
    return None


# ------------------------------------------------------------------ helpers for pack extractors


def platform_entity(blob: str, at: Span, term_name: str, natural_key: dict[str, Any], name: str, attributes: dict[str, Any] | None = None, evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """Declare an entity through a platform term: core kind from the term, platform attributes set."""
    from repo_intel.extractors.base import entity

    terms = _load()["TERMS"]
    if term_name not in terms:
        raise KeyError(f"unknown platform term {term_name}")
    pack, term = terms[term_name]
    attrs = {**(attributes or {}), "platform": pack.id, "platform_kind": term.name}
    if term.fidelity != "exact":
        attrs["fidelity"] = term.fidelity
    return entity(blob, at, term.core, natural_key, name, attrs, evidence_class)


def construct(blob: str, at: Span, construct_id: str, modelled: bool, evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """Report one occurrence of a platform construct, and whether this extractor modelled it."""
    from repo_intel.extractors.base import obs

    if construct_id not in _load()["CONSTRUCTS"]:
        raise KeyError(f"unknown platform construct {construct_id}")
    return obs("construct", blob, at, {"construct": construct_id, "modelled": bool(modelled)}, evidence_class)


__all__ = [
    "Construct", "Fidelity", "PlatformPack", "Term",
    "check_platform_kind", "construct", "pack_extractors", "pack_fingerprint", "packs", "platform_entity", "superseded", "validate_pack",
]
