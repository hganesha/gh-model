"""tsconfig / jsconfig path aliases, for the linker's import resolution.

Only `compilerOptions.baseUrl` and `compilerOptions.paths` are read. `extends`
is not followed: a file is looked at on its own, like every extractor.
"""

from __future__ import annotations

import json
import posixpath
import re

from ..ontology import EvidenceClass
from .base import Extraction, diagnostic, find_span, obs, whole_file

_NAME = re.compile(r"^(tsconfig|jsconfig)(\.[A-Za-z0-9_-]+)?\.json$")


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, leaving string contents alone."""
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class TsConfig:
    id = "manifest.tsconfig"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return bool(_NAME.match(posixpath.basename(path)))

    def extract(self, path, data, blob):
        try:
            doc = json.loads(strip_jsonc(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "unparseable tsconfig")], parsed=False)
        options = doc.get("compilerOptions") if isinstance(doc, dict) else None
        paths = options.get("paths") if isinstance(options, dict) else None
        if not isinstance(paths, dict) or not paths:
            return Extraction()
        clean = {str(k): [t for t in v if isinstance(t, str)] for k, v in paths.items() if isinstance(v, list)}
        base_url = options.get("baseUrl") if isinstance(options.get("baseUrl"), str) else "."
        at = find_span(data, '"paths"') or whole_file(data)
        return Extraction([obs("ts_paths", blob, at, {"dir": posixpath.dirname(path), "base_url": base_url, "paths": clean}, EvidenceClass.E1)])


EXTRACTORS = [TsConfig()]
