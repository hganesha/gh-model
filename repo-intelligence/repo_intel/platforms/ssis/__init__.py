"""The SSIS platform pack: SQL Server Integration Services packages and projects.

`ssis.package` reads `.dtsx` packages: the package as a Job, control-flow tasks and
containers as Tasks with DEPENDS_ON from precedence constraints, connection managers as
ConfigKeys by name, Execute SQL tasks as Programs with a `sql_step` behaviour unit, and
each Data Flow task as a Program with transform shapes (language `ssis`, evidence E1) and,
where it has derived columns or conditional splits, a `flow` behaviour unit.
`ssis.project` reads project connection managers (`.conmgr`) and `Project.params`;
`ssis.ispac` reads a project deployment file (a zip archive) from its bytes.

Modules: `vocabulary` (terms, constructs, task and component types), `xmlread` (XML with
element spans and bounded zip reading), `etlexpr` (the SSIS and Informatica expression
languages into the behavioural IR), `emit` (one file's observations), `package` (a
package), `source` (the extractors). `xmlread`, `etlexpr` and `emit` are shared with the
Informatica pack. None of them imports the `repo_intel.extractors` package at module
level: pack discovery runs while that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.ssis.source import SsisPackageSource, SsisProjectArchive, SsisProjectFiles
from repo_intel.platforms.ssis.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="ssis",
    name="SQL Server Integration Services (packages, project files, .ispac)",
    prefix="ssis",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(SsisPackageSource(), SsisProjectFiles(), SsisProjectArchive()),
    inputs=(".rkp/inputs/ssis/**",),
)
