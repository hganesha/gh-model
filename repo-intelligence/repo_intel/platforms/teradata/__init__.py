"""The `teradata` platform pack: load and export utility scripts.

FastLoad, MultiLoad, TPump and FastExport scripts and Teradata Parallel Transporter job
scripts. BTEQ scripts and Teradata procedures stay with `data.sql_lineage`, which gives a
script the same `Program {platform: "teradata", name: <path>}` key.

Keys:

- a script: `Program {platform: "teradata", name: <path>}`, attributes `utility`
  (fastload, multiload, tpump, fastexport, tpt), `job` (TPT), `log_table`, `error_tables`,
  `work_tables`, `default_database`, `format`;
- an input or output file: `Dataset {name}` as `data_python.dataset_name` gives it;
- a table: `Table {datastore: "default", name}`, qualified with the script's DATABASE;
- a record layout: `DataSchema {kind: "record_layout", name: "teradata:<layout>"}` (FastLoad's
  unnamed layout: `teradata:<path>`), and its fields `Field {table: "teradata:<layout>", name}`.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.teradata.utilities import Utilities
from repo_intel.platforms.teradata.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="teradata",
    name="Teradata load and export utilities (FastLoad, MultiLoad, TPump, FastExport, TPT)",
    prefix="td",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(Utilities(),),
)
