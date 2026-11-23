"""Curated package capabilities, adapted from StackGraph's technical taxonomy."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from functools import cache
from importlib.resources import files
from typing import Literal, Self

from pydantic import Field, model_validator

from .contracts import Model
from .extractors.manifests import normalize_pypi

Ecosystem = Literal["npm", "pypi"]
ComponentRole = Literal["server", "frontend", "consumer"]


class Taxonomy(Model):
    key: str
    version: str
    status: Literal["ACTIVE"]
    name: str
    description: str
    metadata: dict[str, str]


class CapabilityDefinition(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9.-]{1,127}$")
    name: str
    description: str
    aliases: tuple[str, ...] = ()


class CapabilityMapping(Model):
    ecosystem: Ecosystem
    package: str
    capability: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    symbol: str | None = None
    evidence: dict[str, str]
    component_role: ComponentRole | None = None


def _package_name(ecosystem: str, name: str) -> str:
    name = name.strip().lower()
    return normalize_pypi(name) if ecosystem == "pypi" else name


class CapabilityCatalogue(Model):
    taxonomy: Taxonomy
    capabilities: tuple[CapabilityDefinition, ...] = Field(min_length=1)
    mappings: tuple[CapabilityMapping, ...]

    @model_validator(mode="after")
    def _validate_mappings(self) -> Self:
        keys = {item.key for item in self.capabilities}
        if len(keys) != len(self.capabilities):
            raise ValueError("duplicate capability key")
        seen: set[tuple[str, str, str, str | None]] = set()
        for item in self.mappings:
            if item.capability not in keys:
                raise ValueError(f"unknown capability {item.capability}")
            if not item.package or item.package != _package_name(item.ecosystem, item.package):
                raise ValueError(f"package name must be normalized: {item.package}")
            if item.component_role and item.symbol is not None:
                raise ValueError("component roles require a package-level mapping")
            key = (item.ecosystem, item.package, item.capability, item.symbol)
            if key in seen:
                raise ValueError(f"duplicate capability mapping {key}")
            seen.add(key)
        return self

    def component_roles(self) -> dict[str, dict[str, list[str]]]:
        roles: dict[str, dict[str, list[str]]] = {}
        for item in self.mappings:
            if item.component_role:
                roles.setdefault(item.ecosystem, {}).setdefault(item.component_role, []).append(item.package)
        return {
            ecosystem: {role: sorted(set(packages)) for role, packages in hints.items()}
            for ecosystem, hints in roles.items()
        }

    def package_mappings(self) -> dict[tuple[str, str], list[CapabilityMapping]]:
        by_package: dict[tuple[str, str], list[CapabilityMapping]] = {}
        for item in self.mappings:
            by_package.setdefault((item.ecosystem, item.package), []).append(item)
        return by_package


def match_package(
    mappings: dict[tuple[str, str], list[CapabilityMapping]],
    ecosystem: str,
    name: str,
    symbols: Iterable[str] = (),
) -> tuple[CapabilityMapping, ...]:
    """Keep the strongest rule per capability. Symbol rules require reference evidence."""
    best: dict[str, CapabilityMapping] = {}
    symbols = tuple(symbols)
    for item in mappings.get((ecosystem, _package_name(ecosystem, name)), ()):
        if item.symbol is not None and not any(fnmatchcase(symbol, item.symbol) for symbol in symbols):
            continue
        previous = best.get(item.capability)
        if previous is None or item.confidence > previous.confidence:
            best[item.capability] = item
    return tuple(best[key] for key in sorted(best))


@cache
def load_catalogue() -> CapabilityCatalogue:
    return CapabilityCatalogue.model_validate_json(files(__package__).joinpath("technical_capabilities.json").read_bytes())
