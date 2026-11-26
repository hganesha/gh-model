"""The `dbproc` platform pack: DB2 SQL PL, Netezza NZPLSQL and Sybase ASE stored routines.

Keys:

- a procedure, function or trigger: `Program {platform: "db2" | "netezza" | "sybase", name:
  <schema.name, lower case>}`, attributes `kind`, `language` (sqlpl, nzplsql, tsql),
  `params`, `result_sets`, `specific`, `returns`, and for a trigger `on` {table, events,
  timing};
- a table: `Table {datastore: "default", name}`, as `sql_lineage` names it;
- a called procedure: `Program {platform: <same engine>, name}`, not declared (CALLS);
- the Program `sql_lineage` declares for the same procedure in a `.sql` file, `{platform:
  "sql" | "tsql", name}`, is ALIAS_OF the engine's Program.

Each routine has one behaviour unit (language `db2`, `netezza` or `sybase`; kind `function`
or `trigger`). Transform shapes come from its `query` nodes, as for every behaviour unit.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.dbproc.source import Routines
from repo_intel.platforms.dbproc.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="dbproc",
    name="DB2, Netezza and Sybase stored routines",
    prefix="dbp",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(Routines(),),
)
