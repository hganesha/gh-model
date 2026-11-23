"""Model gateway for decision models (spec §7.2, §7.4).

Jev (TypeSafe AI) and Microsoft-Decision-1.1 expose the same "System One"
request format, so one client serves both and a provider is just a URL, an
auth header and a model name:

    POST <base>/v1/systemone                         (Jev)
    POST <endpoint>/providers/microsoft/v1/systemone (Microsoft Foundry)
    {"model": ..., "state": ..., "questions": {"q": {"type": "choice",
      "instructions": ..., "criteria": {option: description}}}}
    -> {"model": ..., "answers": {"q": {"type": "choice", "choice": ...,
        "confidence": ..., "probabilities": {option: p}}}, "usage": {...}}

Sources (read 2026-11-23): docs.typesafe.ai/api and /primitives/choice;
learn.microsoft.com .../use-foundry-models-microsoft-decision.

Live status: the Microsoft-Decision-1.1 path was run against a real Foundry
deployment on 2026-11-23 (Entra auth via the Azure CLI session); the response
does carry `answers.<q>.probabilities`, as Jev documents. The Jev path has NOT
been run live: no TypeSafe credentials were available.

Egress is denied unless the repository's policy names the provider. A denied
or failed call makes the decision abstain; it never fails the compile.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ModelUnavailable(RuntimeError):
    """The provider could not be reached or returned an unusable answer."""


class EgressDenied(RuntimeError):
    """Repository policy does not allow sending data to this provider."""


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    model: str  # the model that actually answered, as reported by the provider
    input_tokens: int = 0


class DecisionModel(Protocol):
    provider: str

    def choose(self, state: Any, instructions: str, options: Mapping[str, str]) -> ChoiceAnswer: ...


def _post(client: Any, body: dict[str, Any]) -> httpx.Response:
    """POST with bounded retry on HTTP 429, honoring the provider's stated wait."""
    try:
        with httpx.Client(timeout=client.timeout, transport=client.transport) as http:
            for attempt in range(client.max_retries + 1):
                response = http.post(client.url, json=body, headers={"Content-Type": "application/json", "Accept": "application/json", **client.headers, **(client.auth() if client.auth else {})})
                if response.status_code != 429 or attempt == client.max_retries:
                    break
                try:
                    wait = float(response.headers.get("retry-after") or response.headers.get("x-ratelimit-reset-requests") or "")
                except ValueError:
                    wait = 2.0 * (attempt + 1)
                client.sleep(min(max(wait, 0.5), client.max_wait))
            return response
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"{client.provider}: {type(exc).__name__}") from exc


@dataclass
class SystemOneClient:
    """HTTP client for the shared System One `choice` question."""

    provider: str
    url: str
    model: str
    headers: dict[str, str]
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None  # injected in tests
    auth: Callable[[], dict[str, str]] | None = None  # fresh auth headers per call (short-lived tokens)
    max_retries: int = 3  # on HTTP 429 only
    max_wait: float = 20.0
    sleep: Callable[[float], None] = time.sleep

    def choose(self, state: Any, instructions: str, options: Mapping[str, str]) -> ChoiceAnswer:
        body = {"model": self.model, "state": state, "questions": {"decision": {"type": "choice", "instructions": instructions, "criteria": dict(options)}}}
        response = _post(self, body)
        if response.status_code != 200:
            raise ModelUnavailable(f"{self.provider}: HTTP {response.status_code}")  # the body may echo our input; do not log it
        try:
            payload = response.json()
            answer = payload["answers"]["decision"]
            choice, probabilities = answer["choice"], answer["probabilities"]
            if answer.get("type") != "choice" or choice not in options or set(probabilities) != set(options):
                raise ValueError("answer does not match the options that were sent")
            return ChoiceAnswer(choice=choice, probabilities={k: float(v) for k, v in probabilities.items()}, model=str(payload.get("model", self.model)), input_tokens=int(payload.get("usage", {}).get("input_tokens", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelUnavailable(f"{self.provider}: unusable answer ({type(exc).__name__})") from exc


def render_state(state: Any, indent: int = 0) -> str:
    """Flatten a JSON state into plain `key: value` lines for models that take text."""
    pad = "  " * indent
    if isinstance(state, Mapping):
        return "\n".join(f"{pad}{k}:\n{render_state(v, indent + 1)}" if isinstance(v, (Mapping, list)) and v else f"{pad}{k}: {json.dumps(v)}" for k, v in state.items())
    if isinstance(state, list):
        return "\n".join(f"{pad}- {json.dumps(v)}" for v in state)
    return f"{pad}{state}"


def render_positive_facts(state: Any) -> str:
    """Only what was observed, as short plain statements. A reranker matches on wording,
    so a fact that is false ("declares importable library entry point: false") would
    still pull it towards that option."""
    facts: list[str] = []

    def walk(value: Any, label: str) -> None:
        text = label.replace("_", " ")
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, bool):
            if value:
                facts.append(text)
        elif isinstance(value, (int, float)):
            if value > 0:
                facts.append(f"{text}: {value}")
        elif isinstance(value, list):
            if value:
                facts.append(f"{text}: {', '.join(map(str, value))}")
        elif isinstance(value, str) and value and label not in ("trust", "scope"):
            facts.append(f"{text}: {value}")

    walk(state if isinstance(state, Mapping) else {"state": state}, "")
    return "\n".join(f"- {f}" for f in facts) or "- nothing distinctive was observed"


@dataclass
class RerankClient:
    """A reranker used as a decision model (Cohere Rerank v2 API, as served by Azure Foundry).

        POST <endpoint>/providers/cohere/v2/rerank
        {"model": <deployment>, "query": ..., "documents": [...], "top_n": n}
        -> {"results": [{"index": i, "relevance_score": s}, ...]}

    The query is the question plus the state as text; each option is one
    document. A reranker scores each document's relevance independently, so the
    scores are NOT probabilities over the options. They are divided by their
    sum here only to fit the common interface: the ranking is meaningful, the
    absolute values are not comparable with a decision model's, and acceptance
    thresholds must be calibrated separately for this provider.
    Run live against Cohere-rerank-v5-pro on Azure on 2026-11-23.
    """

    provider: str
    url: str
    model: str
    headers: dict[str, str]
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None
    auth: Callable[[], dict[str, str]] | None = None
    max_retries: int = 3
    max_wait: float = 20.0
    sleep: Callable[[float], None] = time.sleep

    def choose(self, state: Any, instructions: str, options: Mapping[str, str]) -> ChoiceAnswer:
        names = list(options)
        query = f"{instructions}\n\nWhat was observed:\n{render_positive_facts(state)}"
        body = {"model": self.model, "query": query, "documents": [f"{n.replace('_', ' ')}: {options[n]}" for n in names], "top_n": len(names)}
        response = _post(self, body)
        if response.status_code != 200:
            raise ModelUnavailable(f"{self.provider}: HTTP {response.status_code}")
        try:
            payload = response.json()
            raw = {names[int(r["index"])]: float(r["relevance_score"]) for r in payload["results"]}
            if set(raw) != set(names):
                raise ValueError("a score is missing for some option")
            total = sum(raw.values())
            probabilities = {n: (raw[n] / total if total > 0 else 1.0 / len(names)) for n in names}
            usage = payload.get("meta", {}).get("billed_units", {})
            return ChoiceAnswer(max(names, key=lambda n: (raw[n], n)), probabilities, self.model, int(usage.get("input_tokens", 0)))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ModelUnavailable(f"{self.provider}: unusable answer ({type(exc).__name__})") from exc


def cohere_rerank_azure(endpoint: str, deployment: str, api_key: str | None = None, credential: Any = None, **kw: Any) -> RerankClient:
    if bool(api_key) == bool(credential):
        raise ValueError("give exactly one of api_key or credential")
    url = endpoint.rstrip("/") + "/providers/cohere/v2/rerank"
    if credential is not None:
        return RerankClient("cohere-rerank", url, deployment, {}, auth=lambda: {"Authorization": f"Bearer {credential.get_token(ENTRA_SCOPE).token}"}, **kw)
    return RerankClient("cohere-rerank", url, deployment, {"api-key": api_key}, **kw)


def jev(api_key: str, model: str = "jev-latest", base_url: str = "https://api.typesafe.ai", **kw: Any) -> SystemOneClient:
    return SystemOneClient("jev", base_url.rstrip("/") + "/v1/systemone", model, {"Authorization": f"Bearer {api_key}"}, **kw)


ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"


def microsoft_decision(endpoint: str, deployment: str, api_key: str | None = None, entra_token: str | None = None, credential: Any = None, **kw: Any) -> SystemOneClient:
    """`deployment` is the Foundry deployment name, which goes in the `model` field.

    Auth is exactly one of: an API key, a fixed Entra token, or an azure-identity
    credential object (for example AzureCliCredential, which uses the existing
    `az login` session). With a credential, a token is fetched per call and is
    never stored or logged here.
    """
    if sum(map(bool, (api_key, entra_token, credential))) != 1:
        raise ValueError("give exactly one of api_key, entra_token or credential")
    url = endpoint.rstrip("/") + "/providers/microsoft/v1/systemone"
    if credential is not None:
        return SystemOneClient("microsoft-decision", url, deployment, {}, auth=lambda: {"Authorization": f"Bearer {credential.get_token(ENTRA_SCOPE).token}"}, **kw)
    headers = {"api-key": api_key} if api_key else {"Authorization": f"Bearer {entra_token}"}
    return SystemOneClient("microsoft-decision", url, deployment, headers, **kw)


@dataclass
class ScriptedModel:
    """Deterministic stand-in for tests and offline runs: a function picks the option."""

    pick: Callable[[Any, list[str]], str | dict[str, float]]
    provider: str = "scripted"
    calls: list[list[str]] = field(default_factory=list)

    def choose(self, state: Any, instructions: str, options: Mapping[str, str]) -> ChoiceAnswer:
        names = list(options)
        self.calls.append(names)
        result = self.pick(state, names)
        if isinstance(result, dict):
            probabilities = {n: float(result.get(n, 0.0)) for n in names}
        else:
            probabilities = {n: (0.94 if n == result else 0.06 / max(1, len(names) - 1)) for n in names}
        return ChoiceAnswer(max(probabilities, key=lambda n: (probabilities[n], n)), probabilities, "scripted-1")


@dataclass
class Gateway:
    """Resolves `model_ref` aliases and enforces the per-repository egress policy."""

    aliases: dict[str, DecisionModel] = field(default_factory=dict)
    allowed_providers: frozenset[str] = frozenset()  # egress is denied by default

    def resolve(self, model_ref: str) -> DecisionModel:
        model = self.aliases.get(model_ref)
        if model is None:
            raise ModelUnavailable(f"no model configured for alias {model_ref!r}")
        if model.provider not in self.allowed_providers:
            raise EgressDenied(model.provider)
        return model

    def describe(self) -> dict[str, str]:
        """Alias -> provider/model, pinned into the toolchain so a change is attributable."""
        return {alias: f"{m.provider}/{getattr(m, 'model', '')}" for alias, m in sorted(self.aliases.items())}

    @classmethod
    def from_env(cls, allowed_providers: frozenset[str] = frozenset(), env: Mapping[str, str] = os.environ) -> "Gateway":
        """Build aliases from credentials present in the environment. Keys are never logged."""
        aliases: dict[str, DecisionModel] = {}
        if env.get("RKP_JEV_API_KEY"):
            aliases["jev"] = jev(env["RKP_JEV_API_KEY"], env.get("RKP_JEV_MODEL", "jev-latest"))
        if env.get("RKP_MSDECISION_ENDPOINT") and env.get("RKP_MSDECISION_DEPLOYMENT"):
            if env.get("RKP_MSDECISION_API_KEY"):
                aliases["microsoft-decision"] = microsoft_decision(env["RKP_MSDECISION_ENDPOINT"], env["RKP_MSDECISION_DEPLOYMENT"], api_key=env["RKP_MSDECISION_API_KEY"])
            elif env.get("RKP_MSDECISION_AUTH") == "azure-cli":  # use the existing `az login` session; no key is handled
                from azure.identity import AzureCliCredential
                aliases["microsoft-decision"] = microsoft_decision(env["RKP_MSDECISION_ENDPOINT"], env["RKP_MSDECISION_DEPLOYMENT"], credential=AzureCliCredential())
        if env.get("RKP_COHERE_RERANK_ENDPOINT") and env.get("RKP_COHERE_RERANK_DEPLOYMENT"):
            if env.get("RKP_COHERE_RERANK_API_KEY"):
                aliases["cohere-rerank"] = cohere_rerank_azure(env["RKP_COHERE_RERANK_ENDPOINT"], env["RKP_COHERE_RERANK_DEPLOYMENT"], api_key=env["RKP_COHERE_RERANK_API_KEY"])
            elif env.get("RKP_COHERE_RERANK_AUTH") == "azure-cli":
                from azure.identity import AzureCliCredential
                aliases["cohere-rerank"] = cohere_rerank_azure(env["RKP_COHERE_RERANK_ENDPOINT"], env["RKP_COHERE_RERANK_DEPLOYMENT"], credential=AzureCliCredential())
        default = env.get("RKP_DECISION_DEFAULT")
        if default in aliases:
            aliases["decision-default"] = aliases[default]
        return cls(aliases, allowed_providers)
