"""The Informatica platform pack: PowerCenter XML exports and parameter files.

`informatica.export` reads an export (root `POWERMART`): source and target definitions as
Tables or record layouts, each mapping as a Program with transform shapes (language
`informatica`, evidence E1) and a `flow` behaviour unit, workflows and worklets as Jobs
that contain their task instances, sessions that RUN their mapping and name their
connections, and links as DEPENDS_ON. `informatica.params` reads parameter files by name.

Modules: `vocabulary` (terms, constructs, transformation and task types), `export` (an
export), `params` (a parameter file), `source` (the extractors). XML reading, the
expression language and emission are shared with the SSIS pack
(`repo_intel.platforms.ssis.xmlread`, `etlexpr`, `emit`). Nothing here imports the
`repo_intel.extractors` package at module level: pack discovery runs while it loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.informatica.source import InformaticaExport, InformaticaParameters
from repo_intel.platforms.informatica.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="informatica",
    name="Informatica PowerCenter (XML exports, parameter files)",
    prefix="infa",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(InformaticaExport(), InformaticaParameters()),
    inputs=(".rkp/inputs/informatica/**",),
)
