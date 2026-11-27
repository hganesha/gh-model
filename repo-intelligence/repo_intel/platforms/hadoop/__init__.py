"""The Hadoop platform pack: Hive and Impala scripts, Pig Latin, Oozie and Sqoop.

Four extractors, each reading one file's bytes:

- `hadoop.hive` reads Hive and Impala scripts (`.hql` always; `.q` or `.sql` only when the
  file is under a `hive/` directory or its text is clearly HiveQL) as Programs (platform
  `hive`) with table lineage, the tables and views they create, and transform shapes through
  the shared SQL readers in the `hive` dialect. Multi-table inserts, LOAD DATA and INSERT
  OVERWRITE DIRECTORY are read here, since the shared readers do not take them.
- `hadoop.pig` reads Pig Latin (`.pig`) as Programs (platform `pig`) whose relations are
  transform shapes (language `hadoop`), with the paths and HCatalog tables they load and store.
- `hadoop.oozie` reads Oozie definitions, recognised by the root element: a workflow is a Job
  whose actions are Tasks ordered by DEPENDS_ON from their ok and error transitions, with a
  behaviour unit (kind `flow`) for the order; a coordinator is a Job with its schedule and
  datasets; a bundle is a Job that RUNS its coordinators. Sqoop actions are dataset moves.
- `hadoop.sqoop` reads Sqoop options files that state a tool: a Program that moves data
  between a table and a path.

No extractor keeps a connection string, password, host name, parameter value or default.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.hadoop.hive import HiveScripts
from repo_intel.platforms.hadoop.oozie import OozieDefinitions
from repo_intel.platforms.hadoop.pig import PigScripts
from repo_intel.platforms.hadoop.sqoop import SqoopOptionsFiles
from repo_intel.platforms.hadoop.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="hadoop",
    name="Hadoop (Hive and Impala, Pig, Oozie, Sqoop)",
    prefix="hdp",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(HiveScripts(), PigScripts(), OozieDefinitions(), SqoopOptionsFiles()),
)
