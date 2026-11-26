"""The IBM DataStage platform pack (InfoSphere and Information Server DataStage exports).

`datastage.dsx` reads `.dsx` export files, in the text form and the XML form;
`datastage.isx` reads `.isx` archives (zip files of exports) from the file's bytes. Both
declare jobs and sequences as `Job`s, stages and activities as `Task`s in link and trigger
order, the files and tables stages read and write, one transform-shape unit per job
(language `datastage`), behaviour for transformer stages and sequences, and table
definitions as `DataSchema`s of `Field`s. Everything is read from a declarative export, so
evidence is `E1`.

Modules: `xmlspan` (an XML tree with byte spans), `dsx` (the export's record model),
`expr` (transformer and trigger expressions), `vocabulary` (terms, stage types and
constructs), `jobs` (what a job declares) and `source` (the extractors). None of them
imports the `repo_intel.extractors` package at module level: pack discovery runs while
that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.datastage.source import DsxExport, IsxArchive
from repo_intel.platforms.datastage.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="datastage",
    name="IBM DataStage (parallel, server and sequence jobs; .dsx and .isx exports)",
    prefix="ds",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(DsxExport(), IsxArchive()),
)
