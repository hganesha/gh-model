"""Memoized build-graph runtime (spec §3.3).

A step is a pure function of its declared inputs. Its cache key is the digest
of (step name, step version, config, input digests). A compile evaluates the
graph; steps whose key is cached are not re-run. Incremental and cold compiles
therefore run the same graph and differ only in cache hits.

Hermeticity in M0 is enforced at the API level: a step receives a `StepInputs`
object that can resolve only what was declared, and no handle to the store.
OS-level isolation (read-only mounts of declared inputs, no network) is the
remaining part of GI-004.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .hashing import digest
from .store import ObjectStore


class UndeclaredInputError(KeyError):
    """A step asked for an input it did not declare."""


@dataclass(frozen=True)
class Ref:
    """A declared input held in the object store, identified by digest."""

    digest: str
    kind: str = "json"  # "json" or "bytes"


class StepInputs:
    def __init__(self, store: ObjectStore, declared: Mapping[str, Any]):
        self.__store = store
        self.__declared = dict(declared)

    def __getitem__(self, name: str) -> Any:
        if name not in self.__declared:
            raise UndeclaredInputError(name)
        return self._resolve(self.__declared[name])

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, Ref):
            return self.__store.get_bytes(value.digest) if value.kind == "bytes" else self.__store.get_json(value.digest)
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve(v) for k, v in value.items()}
        return value

    def names(self) -> list[str]:
        return sorted(self.__declared)


StepFn = Callable[[StepInputs, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class Step:
    name: str
    version: str
    fn: StepFn


def _key_form(value: Any) -> Any:
    """Inputs as they enter the cache key: refs collapse to their digest."""
    if isinstance(value, Ref):
        return {"$ref": value.digest, "kind": value.kind}
    if isinstance(value, list):
        return [_key_form(v) for v in value]
    if isinstance(value, dict):
        return {k: _key_form(v) for k, v in value.items()}
    return value


@dataclass
class RunStats:
    executed: Counter = field(default_factory=Counter)
    hits: Counter = field(default_factory=Counter)

    @property
    def total_executed(self) -> int:
        return sum(self.executed.values())


class StepRunner:
    def __init__(self, store: ObjectStore, use_cache: bool = True):
        self.store = store
        self.use_cache = use_cache
        self.stats = RunStats()

    def cache_key(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any]) -> str:
        return digest({"step": step.name, "version": step.version, "config": dict(config), "inputs": _key_form(dict(inputs))})

    def run(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> Ref:
        """Evaluate a step and return a ref to its JSON output."""
        config = config or {}
        key = self.cache_key(step, inputs, config)
        if self.use_cache:
            cached = self.store.index_get("step_cache", key)
            if cached is not None and self.store.has(cached):
                self.stats.hits[step.name] += 1
                return Ref(cached)
        output = step.fn(StepInputs(self.store, inputs), config)
        out_digest = self.store.put_json(output)
        self.store.index_put("step_cache", key, out_digest)
        self.stats.executed[step.name] += 1
        return Ref(out_digest)

    def load(self, ref: Ref) -> Any:
        return self.store.get_json(ref.digest)
