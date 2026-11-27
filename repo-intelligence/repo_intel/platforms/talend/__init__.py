"""The Talend platform pack (Talend Open Studio and Talend Data Integration project items).

`talend.item` reads job and joblet designs (`process/**/<label>_<version>.item`): the job
as a `Job`, its components as `Task`s in connection order, the files and tables they read
and write, one transform-shape unit per job (language `talend`), behaviour for tMap
components and for jobs that run other jobs, and RUNS from tRunJob to the job it names.
`talend.properties` reads each item's repository properties: a job's version, purpose and
status, and routines as `Program`s. Everything is read from declarative XML, so evidence
is `E1`.

Modules: `vocabulary` (terms, components and constructs), `javaexpr` (the Java expressions
Talend settings and tMap use), `item` (what a job item declares) and `source` (the
extractors). The XML tree with byte spans is the DataStage pack's `xmlspan`. None of them
imports the `repo_intel.extractors` package at module level: pack discovery runs while
that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.talend.source import TalendItem, TalendProperties
from repo_intel.platforms.talend.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="talend",
    name="Talend (Data Integration jobs and joblets; .item and .properties project files)",
    prefix="tal",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(TalendItem(), TalendProperties()),
)
