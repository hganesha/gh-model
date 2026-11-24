"""tsconfig / jsconfig: `extends`, `compilerOptions.baseUrl` and `compilerOptions.paths`.

A file is looked at on its own, like every extractor: one "ts_config"
observation records what this file declares. `unit.plan` follows `extends`
chains across files (a package's tsconfig.json extending a root
tsconfig.base.json) and hands each unit its effective path aliases.
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
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return bool(_NAME.match(posixpath.basename(path)))

    def extract(self, path, data, blob):
        try:
            doc = json.loads(strip_jsonc(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "unparseable tsconfig")], parsed=False)
        if not isinstance(doc, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "tsconfig is not an object")], parsed=False)
        options = doc.get("compilerOptions") if isinstance(doc.get("compilerOptions"), dict) else {}
        paths = options.get("paths")
        raw_extends = doc.get("extends")
        extends = [raw_extends] if isinstance(raw_extends, str) else [e for e in raw_extends if isinstance(e, str)] if isinstance(raw_extends, list) else []
        payload = {
            "dir": posixpath.dirname(path),
            "extends": extends,
            "base_url": options["baseUrl"] if isinstance(options.get("baseUrl"), str) else None,
            "paths": {str(k): [t for t in v if isinstance(t, str)] for k, v in paths.items() if isinstance(v, list)} if isinstance(paths, dict) else None,
        }
        at = find_span(data, '"paths"') or find_span(data, '"extends"') or find_span(data, '"baseUrl"') or whole_file(data)
        return Extraction([obs("ts_config", blob, at, payload, EvidenceClass.E1)])


EXTRACTORS = [TsConfig()]
