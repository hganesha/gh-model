"""The z/OS pack: JCL jobs, procedures and steps, the data sets they bind, the control
cards they pass to utilities, and the resource definitions around a mainframe application
(CICS CSD exports, DCLGEN members, DB2 BIND cards and DDL).

It replaces `legacy.jcl` and emits at least what that extractor emitted, with the same
kinds and natural keys: a Job per JOB or PROC statement (`attributes.kind` and
`attributes.member`), a Task per EXEC that the Job CONTAINS and that DEPENDS_ON the step
before it, RUNS for each program or procedure, and READS_FROM / WRITES_TO data sets keyed
by their lower-cased name. One departure, by design: the steps of an in-stream procedure
belong to the procedure (`Task {job: <proc>}`), under the procedure's Job, not to the job
that defines it; the calling step's overrides apply to them, and the calling step carries
the data sets they bind (contract 11.3 item 5).

A DCLGEN member is a copybook, `DataSchema {kind: "copybook"}` with `Field {table:
"cobol:<member>"}` (contract 11.3 item 4); DB2 DDL tables and columns are lower-cased as
`data.sql_ddl` keys them (11.3 item 1).

Modules:

- `jcl`: one linear pass from text to statements (continuations, quoting, in-stream data,
  comments), then jobs, procedures, steps and DDs;
- `cards`: control-card commands (IDCAMS, TSO, DFHCSDUP) and DFSORT statements;
- `csd`, `db2`: CSD definitions; DCLGEN members, BIND commands and DB2 DDL;
- `emit`: one file's observations, each entity and relation once;
- `source`: the extractors `zos.jcl` and `zos.definitions`;
- `vocabulary`: terms and constructs.

Pack modules import `repo_intel.extractors` modules only inside functions: pack
discovery runs while that package is loading.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.zos.source import INPUT_DIR, ZosDefinitions, ZosJcl
from repo_intel.platforms.zos.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="zos",
    name="z/OS",
    prefix="zos",
    version="1.1.0",  # 1.1.0: a DCLGEN member is a copybook; IKJEFT01 execs get dd_bindings; referbacks stay in their job
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(ZosJcl(), ZosDefinitions()),
    supersedes=("legacy.jcl",),
    inputs=(INPUT_DIR + "**",),
)
