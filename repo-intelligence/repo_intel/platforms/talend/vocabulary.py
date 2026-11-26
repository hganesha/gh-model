"""The Talend pack's vocabulary: platform terms, components and the constructs it looks for.

A component is named by its `componentName` (`tFileInputDelimited`, `tMap`, `tOracleOutput`,
...). `COMPONENTS` gives each known component the family that says how the pack reads it
and, for database components, the SQL dialect of its statements (a sqlglot dialect name,
or None for the generic one). Database components are listed for each vendor Talend ships
them for; a component the table does not know is counted as `tal:component.unknown`.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("tal:StandardJob", K.JOB, "lossy", "a Data Integration job (a process .item): its components, connections and context; named by the item's label"),
    Term("tal:BigDataJob", K.JOB, "lossy", "a Big Data batch or streaming job; its components are read like a standard job's"),
    Term("tal:Joblet", K.JOB, "lossy", "a joblet: a reusable fragment of components"),
    Term("tal:Component", K.TASK, "lossy", "a component of a job, named by its unique name (tMap_1); its component name and family in attributes"),
    Term("tal:Routine", K.PROGRAM, "lossy", "a code routine, declared by its .properties; its Java source is not lowered"),
)

# (component, family, dialect, description)
_FIXED: list[tuple[str, str, str | None, str]] = [
    # files
    ("tFileInputDelimited", "file_input", None, "read a delimited file"),
    ("tFileInputPositional", "file_input", None, "read a fixed-width file"),
    ("tFileInputExcel", "file_input", None, "read an Excel sheet"),
    ("tFileInputJSON", "file_input", None, "read a JSON file"),
    ("tFileInputXML", "file_input", None, "read an XML file"),
    ("tFileInputFullRow", "file_input", None, "read a file a line at a time"),
    ("tFileInputRaw", "file_input", None, "read a whole file"),
    ("tFileInputParquet", "file_input", None, "read a Parquet file"),
    ("tFileOutputDelimited", "file_output", None, "write a delimited file"),
    ("tFileOutputPositional", "file_output", None, "write a fixed-width file"),
    ("tFileOutputExcel", "file_output", None, "write an Excel sheet"),
    ("tFileOutputJSON", "file_output", None, "write a JSON file"),
    ("tFileOutputXML", "file_output", None, "write an XML file"),
    ("tFileOutputParquet", "file_output", None, "write a Parquet file"),
    ("tFileList", "iterate", None, "iterate over the files of a directory"),
    ("tFileCopy", "file_op", None, "copy a file"),
    ("tFileDelete", "file_op", None, "delete a file"),
    ("tFileExist", "file_op", None, "test that a file exists"),
    ("tFileArchive", "file_op", None, "zip files"),
    ("tFileUnarchive", "file_op", None, "unzip a file"),
    ("tFileTouch", "file_op", None, "create an empty file"),
    ("tFileProperties", "file_op", None, "read a file's properties"),
    ("tFTPGet", "file_op", None, "download over FTP; the host is never copied"),
    ("tFTPPut", "file_op", None, "upload over FTP; the host is never copied"),
    ("tSCPPut", "file_op", None, "upload over SCP; the host is never copied"),
    # processing
    ("tMap", "map", None, "map rows: lookups, filters, variables and output expressions"),
    ("tXMLMap", "other", None, "map hierarchical (XML) rows"),
    ("tFilterRow", "filter", None, "filter rows on conditions"),
    ("tFilterColumns", "filter_columns", None, "keep some columns"),
    ("tAggregateRow", "aggregate", None, "group and aggregate rows"),
    ("tSortRow", "sort", None, "sort rows"),
    ("tUniqRow", "dedup", None, "split unique and duplicate rows by key"),
    ("tJoin", "join", None, "join the main flow to a lookup flow"),
    ("tUnite", "union", None, "union flows"),
    ("tReplicate", "replicate", None, "copy a flow to several outputs"),
    ("tConvertType", "convert", None, "convert column types"),
    ("tNormalize", "other", None, "split a column's values into rows"),
    ("tDenormalize", "other", None, "join rows' values into one column"),
    ("tSplitRow", "other", None, "split one row into several"),
    ("tSampleRow", "other", None, "keep a sample of rows"),
    ("tSchemaComplianceCheck", "other", None, "check rows against a schema"),
    ("tFixedFlowInput", "flow_input", None, "rows written in the component"),
    ("tRowGenerator", "flow_input", None, "generated rows"),
    ("tHashOutput", "other", None, "rows kept in memory"),
    ("tHashInput", "other", None, "rows read from memory"),
    ("tBufferOutput", "other", None, "rows handed to the calling job"),
    # orchestration and code
    ("tRunJob", "run_job", None, "run another job"),
    ("tJava", "java", None, "Java code run once; not lowered"),
    ("tJavaRow", "java", None, "Java code run for each row; not lowered"),
    ("tJavaFlex", "java", None, "Java code with start, main and end parts; not lowered"),
    ("tSystem", "java", None, "run an operating-system command; the command is never copied"),
    ("tLoop", "iterate", None, "a loop"),
    ("tFlowToIterate", "iterate", None, "iterate over a flow's rows"),
    ("tIterateToFlow", "iterate", None, "rows from an iteration"),
    ("tPrejob", "control", None, "the start of a job's pre-job part"),
    ("tPostjob", "control", None, "the start of a job's post-job part"),
    ("tWarn", "control", None, "log a warning"),
    ("tDie", "control", None, "stop the job with an error"),
    ("tSleep", "control", None, "wait"),
    ("tLogRow", "control", None, "print rows to the log"),
    ("tLogCatcher", "control", None, "catch log messages"),
    ("tStatCatcher", "control", None, "catch statistics"),
    ("tFlowMeterCatcher", "control", None, "catch flow meters"),
    ("tSendMail", "control", None, "send a mail; addresses are never copied"),
    ("tSetGlobalVar", "control", None, "set global variables"),
    ("tContextLoad", "control", None, "load context values from a flow; the values are never copied"),
    ("tParallelize", "control", None, "run subjobs in parallel"),
    ("tChronometerStart", "control", None, "start a timer"),
    ("tChronometerStop", "control", None, "stop a timer"),
]
_VENDORS: list[tuple[str, str | None, str]] = [
    ("DB", None, "generic"), ("JDBC", None, "jdbc"), ("Oracle", "oracle", "oracle"), ("Mysql", "mysql", "mysql"),
    ("Postgresql", "postgres", "postgresql"), ("MSSql", "tsql", "mssql"), ("DB2", None, "db2"), ("Teradata", "teradata", "teradata"),
    ("Snowflake", "snowflake", "snowflake"), ("Hive", "hive", "hive"), ("Redshift", "redshift", "redshift"), ("Sybase", "tsql", "sybase"),
    ("Netezza", "postgres", "netezza"), ("Vertica", "postgres", "vertica"), ("Greenplum", "postgres", "greenplum"), ("Informix", None, "informix"),
]
_DB_SUFFIXES: list[tuple[str, str, str]] = [
    ("Input", "db_input", "read rows with a query or from a table"),
    ("Output", "db_output", "write rows to a table"),
    ("Row", "db_row", "run a SQL statement for each row or once"),
    ("SP", "other", "call a stored procedure"),
    ("BulkExec", "other", "bulk load a file into a table"),
    ("Connection", "db_control", "open a connection; its settings are never read"),
    ("Commit", "db_control", "commit a connection"),
    ("Rollback", "db_control", "roll a connection back"),
    ("Close", "db_control", "close a connection"),
]
_COMPONENTS: list[tuple[str, str, str | None, str]] = _FIXED + [
    (f"t{vendor}{suffix}", family, dialect, f"{label} database: {text}") for vendor, dialect, label in _VENDORS for suffix, family, text in _DB_SUFFIXES
]
# component -> (canonical name, family, dialect, vendor label)
COMPONENTS: dict[str, tuple[str, str, str | None]] = {name.lower(): (name, family, dialect) for name, family, dialect, _ in _COMPONENTS}
VENDOR_OF: dict[str, str] = {f"t{vendor}{suffix}".lower(): label for vendor, _, label in _VENDORS for suffix, _, _ in _DB_SUFFIXES}
# The generic tDB components name their database in the TYPE setting.
DB_TYPES: dict[str, tuple[str | None, str]] = {
    "oracle": ("oracle", "oracle"), "mysql": ("mysql", "mysql"), "postgresql": ("postgres", "postgresql"), "mssql": ("tsql", "mssql"),
    "db2": (None, "db2"), "teradata": ("teradata", "teradata"), "snowflake": ("snowflake", "snowflake"), "redshift": ("redshift", "redshift"),
    "sybase": ("tsql", "sybase"), "netezza": ("postgres", "netezza"), "vertica": ("postgres", "vertica"), "greenplum": ("postgres", "greenplum"),
}

_FAMILY_MODELLED = {
    "file_input": "entity:Dataset", "file_output": "entity:Dataset", "db_input": "entity:Table", "db_output": "entity:Table",
    "db_row": "relation:WRITES_TO", "db_control": "entity:Task", "map": "behavior:flow", "filter": "attribute:transforms",
    "filter_columns": "attribute:transforms", "aggregate": "attribute:transforms", "sort": "attribute:transforms", "dedup": "attribute:transforms",
    "join": "attribute:transforms", "union": "attribute:transforms", "replicate": "attribute:transforms", "convert": "attribute:transforms",
    "flow_input": "unmapped", "run_job": "relation:RUNS", "java": "unmapped", "iterate": "unmapped", "control": "entity:Task",
    "file_op": "unmapped", "other": "unmapped",
}
_OTHER = (
    ("job", "entity:Job", "a job or joblet item"),
    ("context_parameter", "attribute:context_parameters", "a context variable: its name only, never its value"),
    ("connection", "relation:DEPENDS_ON", "a row connection between two components (main, lookup, filter, reject, ...)"),
    ("trigger", "relation:DEPENDS_ON", "a trigger between two components (on subjob ok, on component error, run if, iterate)"),
    ("run_if_condition", "behavior:if", "the condition of a Run if trigger"),
    ("tmap_lookup", "attribute:transforms", "a tMap lookup input: a join on its key expressions"),
    ("tmap_variable", "behavior:assign", "a tMap variable"),
    ("tmap_filter", "behavior:if", "a tMap output or lookup filter expression"),
    ("tmap_expression", "behavior:assign", "a tMap output column's expression"),
    ("filter_condition", "attribute:transforms", "a tFilterRow condition"),
    ("expression_unparsed", "unmapped", "a Java expression outside the pack's subset: kept as a platform node"),
    ("dynamic_name", "unmapped", "a file, table or job named by context variables or code: the transform stays dynamic"),
    ("inactive_component", "unmapped", "a deactivated component: kept as a Task, its data is not read"),
    ("note", "unmapped", "a note on the job's canvas"),
    ("properties_item", "attribute:version", "the .properties of an item: label, version, purpose and status"),
    ("routine", "entity:Program", "a code routine declared by its .properties"),
    ("context_item", "unmapped", "a context group item: named variables shared by jobs; values are never read"),
    ("metadata_item", "unmapped", "a repository metadata item (connection, file schema); its settings are never read"),
    ("code_item", "unmapped", "the source of a routine or bean: not lowered"),
    ("item_other", "unmapped", "another kind of repository item"),
    ("component.unknown", "unmapped", "a component this pack does not know (a custom component or a joblet)"),
)


def _constructs() -> tuple[Construct, ...]:
    out = [Construct(f"tal:component.{name}", text, _FAMILY_MODELLED[family]) for name, family, _, text in _COMPONENTS]
    out += [Construct(f"tal:{name}", text, modelled) for name, modelled, text in _OTHER]
    return tuple(out)


CONSTRUCTS = _constructs()


def component_kind(name: str) -> tuple[str, str, str | None] | None:
    """(canonical name, family, dialect) of a component, or None when the pack does not know it."""
    return COMPONENTS.get(name.lower())
