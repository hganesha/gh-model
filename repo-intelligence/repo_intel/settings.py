"""Versioned workspace settings and their immutable compile inputs."""

from __future__ import annotations

import os
import re
from typing import Literal, Self

import psycopg
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from .business_map import Key, Name, StaleVersion
from .contracts import DecisionSpec, Model
from .decisions.gateway import Gateway
from .extractors import EXTRACTORS

TABLES = {"workspace_settings": "SELECT, INSERT, UPDATE", "workspace_settings_revisions": "SELECT, INSERT"}
DDL = """
CREATE TABLE IF NOT EXISTS workspace_settings (
  tenant_id text PRIMARY KEY, version int NOT NULL, document jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_settings_revisions (
  tenant_id text NOT NULL, version int NOT NULL, document jsonb NOT NULL,
  author text NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, version));
"""
REF = re.compile(r"^(keychain|managed|vault):[a-zA-Z0-9._-]{1,100}$")
PROVIDERS = ("jev", "microsoft-decision", "azure-openai", "cohere-rerank", "openai-embeddings")
EXTRACTOR_IDS = frozenset(e.id for e in EXTRACTORS)
MODEL_FUNCTIONS = ("dm.component-kind", "dm.capability-placement")
# Coverage domains every snapshot must report, with denominators (spec §9.2, §14.1).
COVERAGE_DOMAINS = ("inventory", "parse", "import_resolution", "route_to_handler", "data_access", "contract_match", "deployment_refs", "decisions", "dependency_capability", "event_topics")
DEFAULT_REQUIRED_COVERAGE = ("inventory", "parse", "contract_match", "deployment_refs", "decisions")


class Connection(Model):
    id: Key
    name: Name
    mode: Literal["github_pat", "github_device", "github_app", "gitlab_token", "git_https", "git_ssh"]
    host: str = Field(max_length=255)
    credential_ref: str | None = Field(None, max_length=120)
    webhook_ref: str | None = Field(None, max_length=120)
    app_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,19}$")
    installation_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,19}$")
    remote: str | None = Field(None, max_length=500)
    client_id: str | None = Field(None, pattern=r"^[a-zA-Z0-9_]{1,64}$")

    @model_validator(mode="after")
    def connection_shape(self) -> Self:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?", self.host):
            raise ValueError("Use a lowercase host, with an optional port.")
        if self.mode.startswith("github_") and self.host != "github.com":
            raise ValueError("GitHub connections currently use github.com.")
        if any(ref and not REF.fullmatch(ref) for ref in (self.credential_ref, self.webhook_ref)):
            raise ValueError("Use keychain:<name> or managed:<name> for credential references.")
        if self.mode == "github_app" and not (self.app_id and self.installation_id):
            raise ValueError("Enter the GitHub App ID and installation ID.")
        if self.mode.startswith("git_") != bool(self.remote):
            raise ValueError("A plain Git connection requires exactly one remote URL.")
        if self.remote:
            from .connectors import validate_git_remote
            validate_git_remote(self.remote, self.host, self.mode)
        if self.mode == "github_device" and not self.client_id:
            raise ValueError("Enter the OAuth application's client ID.")
        if self.mode != "github_device" and not self.credential_ref:
            raise ValueError("Enter a reference to the credential.")
        return self


class RepositoryTracking(Model):
    refs: tuple[str, ...] = Field(default=(), max_length=50)
    trigger: Literal["manual", "push", "poll"] = "manual"
    interval_minutes: int = Field(15, ge=1, le=1440)
    # Providers this repository's excerpts may go to. None inherits the workspace policy;
    # otherwise the effective set is the intersection with it, so a repository can only narrow.
    allowed_providers: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def valid_refs(self) -> Self:
        if len(set(self.refs)) != len(self.refs) or any(
            not re.fullmatch(r"(?![-./])[a-zA-Z0-9._/-]{1,200}", ref)
            or ".." in ref or "//" in ref or ref.endswith(("/", "."))
            for ref in self.refs
        ):
            raise ValueError("Use unique valid Git refs.")
        if self.allowed_providers is not None and set(self.allowed_providers) - set(PROVIDERS):
            raise ValueError("Unknown provider.")
        return self


class ModelSelection(Model):
    alias: Key | None = None
    threshold: float | None = Field(None, ge=0, le=1)
    margin: float | None = Field(None, ge=0, le=1)

    def configure(self, spec: DecisionSpec) -> DecisionSpec:
        overrides = {"model_ref": self.alias, "accept_threshold": self.threshold, "min_margin": self.margin}
        return spec.model_copy(update={"strategy": [
            strategy.model_copy(update={k: v for k, v in overrides.items() if v is not None})
            if strategy.kind == "decision_model" else strategy for strategy in spec.strategy
        ]})


class CompileSettings(Model):
    disabled_extractors: tuple[str, ...] = ()
    allowed_providers: tuple[str, ...] = ()
    default_model: Key | None = None
    model_functions: dict[str, ModelSelection] = Field(default_factory=dict)
    decision_budget: int = Field(200, ge=0, le=2000)
    # DM08 behaviour candidates: an LLM writes hypotheses. Experimental, so off by default.
    behavior_candidates: bool = False
    # The compile profile: coverage domains the publish gate requires, each with a denominator.
    required_coverage: tuple[str, ...] = DEFAULT_REQUIRED_COVERAGE
    # Per-repository egress narrowing, copied from `Settings.repositories` by `effective_compile`.
    repository_egress: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_options(self) -> Self:
        if set(self.disabled_extractors) - EXTRACTOR_IDS:
            raise ValueError("Unknown extractor.")
        if set(self.allowed_providers) - set(PROVIDERS):
            raise ValueError("Unknown provider.")
        if set(self.model_functions) - set(MODEL_FUNCTIONS):
            raise ValueError("Unknown decision function.")
        if set(self.required_coverage) - set(COVERAGE_DOMAINS):
            raise ValueError("Unknown coverage domain.")
        return self

    def for_repository(self, repository_id: str) -> "CompileSettings":
        """The settings one repository compiles under. Other repositories' egress rules are
        dropped, so changing one repository's policy never changes another's toolchain."""
        narrowed = self.repository_egress.get(repository_id)
        update: dict = {"repository_egress": {}}
        if narrowed is not None:
            update["allowed_providers"] = tuple(sorted(set(self.allowed_providers) & set(narrowed)))
            update["repository_egress"] = {repository_id: tuple(sorted(narrowed))}
        return self.model_copy(update=update)

    def gateway(self, base: Gateway | None) -> Gateway | None:
        if base is None:
            return None
        aliases = dict(base.aliases)
        if self.default_model is not None:
            if self.default_model in aliases:
                aliases["decision-default"] = aliases[self.default_model]
            else:
                aliases.pop("decision-default", None)
        elif aliases.get("decision-default") and aliases["decision-default"].provider in PROVIDERS:
            aliases.pop("decision-default", None)
        return Gateway(aliases, frozenset(self.allowed_providers) | (base.allowed_providers - set(PROVIDERS)), base.llms, base.rates)


class PlacementPolicy(Model):
    auto_place: bool = True
    threshold: float = Field(0.88, ge=0, le=1)
    component_kinds: tuple[Literal["service", "worker", "frontend_app", "library", "tooling", "cli", "unknown"], ...] = (
        "service", "worker", "frontend_app",
    )
    embedding_provider: Literal["local", "openai-embeddings"] = "local"


class Settings(Model):
    connections: tuple[Connection, ...] = Field(default=(), max_length=50)
    repositories: dict[str, RepositoryTracking] = Field(default_factory=dict, max_length=500)
    compile: CompileSettings = Field(default_factory=CompileSettings)
    business_map: PlacementPolicy = Field(default_factory=PlacementPolicy)

    @model_validator(mode="after")
    def unique_connections(self) -> Self:
        ids = [c.id for c in self.connections]
        if "github-cli" in ids or len(ids) != len(set(ids)):
            raise ValueError("Connection IDs must be unique.")
        return self


class SaveSettings(Settings):
    expected_version: int = Field(ge=0)


def read(conn: psycopg.Connection, tenant: str) -> tuple[int, Settings]:
    row = conn.execute("SELECT version, document FROM workspace_settings WHERE tenant_id=%s", (tenant,)).fetchone()
    return (row[0], Settings.model_validate(row[1])) if row else (0, Settings())


def save(conn: psycopg.Connection, tenant: str, body: SaveSettings, actor: str) -> int:
    document = Jsonb(body.model_dump(mode="json", exclude={"expected_version"}))
    with conn.transaction():
        if body.expected_version == 0:
            row = conn.execute(
                "INSERT INTO workspace_settings VALUES (%s,1,%s) ON CONFLICT DO NOTHING RETURNING version",
                (tenant, document),
            ).fetchone()
        else:
            row = conn.execute(
                """UPDATE workspace_settings SET version=version+1,document=%s
                   WHERE tenant_id=%s AND version=%s RETURNING version""",
                (document, tenant, body.expected_version),
            ).fetchone()
        if row is None:
            raise StaleVersion("Settings changed. Reload them before saving.")
        conn.execute(
            "INSERT INTO workspace_settings_revisions (tenant_id,version,document,author) VALUES (%s,%s,%s,%s)",
            (tenant, row[0], document, actor),
        )
    return row[0]


def environment_settings() -> dict:
    result = {}
    if "RKP_ALLOW_PROVIDERS" in os.environ:
        result["allowed_providers"] = sorted(set(filter(None, os.environ["RKP_ALLOW_PROVIDERS"].split(","))) & set(PROVIDERS))
    if "RKP_DECISION_DEFAULT" in os.environ:
        result["default_model"] = os.environ["RKP_DECISION_DEFAULT"] or None
    return result


def effective_compile(settings: Settings) -> CompileSettings:
    egress = {repo: tuple(sorted(t.allowed_providers)) for repo, t in sorted(settings.repositories.items()) if t.allowed_providers is not None}
    return settings.compile.model_copy(update={**environment_settings(), "repository_egress": egress})
