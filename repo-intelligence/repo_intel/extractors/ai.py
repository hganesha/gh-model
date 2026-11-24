"""AI integration signals (DM06): model identifiers and prompt files.

Both are pattern-based, so they are declared at evidence class E4 (heuristic).
SDK usage itself comes from import observations and is handled by the linker.
A model identifier is a public product name, not a configuration secret.
"""

from __future__ import annotations

import posixpath
import re

from repo_intel.compile.ontology import EntityKind, EvidenceClass
from repo_intel.extractors.base import Extraction, entity, obs, span, whole_file

_MODELS = [
    ("anthropic", re.compile(rb"(?<![\w-])claude-(?:opus|sonnet|haiku|fable|mythos|instant|\d)[a-z0-9.-]*[a-z0-9]")),
    ("openai", re.compile(rb"(?<![\w-])(?:gpt-\d[a-z0-9.-]*[a-z0-9]|o[134]-(?:mini|preview)[a-z0-9-]*|text-embedding-[a-z0-9-]+)")),
    ("google", re.compile(rb"(?<![\w-])gemini-\d[a-z0-9.-]*[a-z0-9]")),
    ("mistral", re.compile(rb"(?<![\w-])mistral-(?:large|medium|small)[a-z0-9-]*")),
]
_TEXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".yaml", ".yml", ".json", ".toml"}
_PROMPT = re.compile(r"(^|/)prompts?/[^/]+\.(md|txt|j2|jinja|yaml|yml)$|(^|/)[^/]*prompt[^/]*\.(md|txt|j2|jinja)$", re.I)


class ModelReferences:
    id = "ai.model_reference"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.splitext(path)[1].lower() in _TEXT and posixpath.basename(path) not in ("package-lock.json",)

    def extract(self, path, data, blob):
        out, seen = [], set()
        for provider, pattern in _MODELS:
            for m in pattern.finditer(data):
                model = m.group(0).decode()
                if (provider, model) in seen:
                    continue
                seen.add((provider, model))
                at = span(data, m.start(), m.end())
                out.append(entity(blob, at, EntityKind.MODEL_ENDPOINT, {"provider": provider, "model": model}, model, {"basis": "identifier_pattern"}, EvidenceClass.E4))
        return Extraction(out)


class PromptFiles:
    id = "ai.prompt_file"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return bool(_PROMPT.search(path))

    def extract(self, path, data, blob):
        # The file's text is repository data like any other; nothing in it is interpreted.
        return Extraction([entity(blob, whole_file(data), EntityKind.PROMPT_ARTIFACT, {"path": path}, posixpath.basename(path), {"basis": "path_pattern"}, EvidenceClass.E4)])


EXTRACTORS = [ModelReferences(), PromptFiles()]
