"""The Ab Initio platform pack (DML record formats and graphs deployed as scripts).

`abinitio.dml` reads `.dml` record formats into `DataSchema`s of `Field`s (contract 10.1
item 6). `abinitio.graph` reads a graph from the `mp` commands of its deployed script, which
the owner commits under `.rkp/inputs/abinitio/` (contract 10.1 item 8): the graph as a `Job`,
its components as `Task`s, flows as DEPENDS_ON, the files and tables components read and
write, and one transform-shape unit per graph (language `abinitio`). `abinitio.binary`
inventories binary graphs (`.mp`) and transforms (`.xfr`) as coverage gaps. Everything read
is declarative, so evidence is `E1`. Behaviour units are not produced: the procedural logic
of a graph is in its `.xfr` transforms, which are not lowered.

Modules: `vocabulary` (terms and constructs), `dml` (the record-format reader), `graph` (the
deployed-script reader) and `source` (the extractors). None of them imports the
`repo_intel.extractors` package at module level: pack discovery runs while that package
loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.abinitio.source import DmlRecordFormat, GraphScript, Unread
from repo_intel.platforms.abinitio.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="abinitio",
    name="Ab Initio (DML record formats; graphs deployed as scripts under .rkp/inputs/abinitio/)",
    prefix="abi",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(DmlRecordFormat(), GraphScript(), Unread()),
    inputs=(".rkp/inputs/abinitio/**",),
)
