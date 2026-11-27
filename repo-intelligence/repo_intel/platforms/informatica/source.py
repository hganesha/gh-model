"""The Informatica pack's extractors.

- `informatica.export` reads a PowerCenter XML export. `.xml` is not mapped to a language,
  so the extractor looks at every `.xml` file and reads only those whose root is
  `POWERMART`; any other XML file is left alone without a diagnostic.
- `informatica.params` reads a parameter file by name only (repo_intel.platforms.informatica.params).
"""

from __future__ import annotations

import re

from repo_intel.platforms.informatica import params
from repo_intel.platforms.informatica.export import Export
from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.xmlread import head_text, local, parse

_ROOT = re.compile(r"<POWERMART[\s>]")


class InformaticaExport:
    id = "informatica.export"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".xml")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if not _ROOT.search(head_text(data)):
            return base.Extraction(parsed=False)
        out = Out(self.id, path, data, blob, base)
        doc, problem = parse(data)
        if doc is None:
            out.note("warning", f"PowerCenter export {path}: {problem}")
            return out.extraction(parsed=False)
        if local(doc.root.tag) != "POWERMART":
            return base.Extraction(parsed=False)
        Export(out, doc, data).run()
        return out.extraction()


class InformaticaParameters:
    id = "informatica.params"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".par", ".prm", ".parm"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        out = Out(self.id, path, data, blob, base)
        if not params.read(out, data):
            return base.Extraction(parsed=False)
        return out.extraction()
