"""The DataStage pack's vocabulary: platform terms, stage types and the constructs it looks for.

A stage is named by its stage type: the `StageType` property of a custom stage (the
parallel operators `PxSort`, `PxJoin`, the connectors `OracleConnectorPX`, ...), else the
record's `OLEType` (`CTransformerStage`, the sequence activities `CJSJobActivity`, ...).
`STAGES` gives each known type the family that says how the pack reads it and, for
database stages, the SQL dialect of its statements. A type the table does not know is
counted as `ds:stage.unknown`.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("ds:ParallelJob", K.JOB, "lossy", "a parallel job (job type 3): its stages, links and parameters; named by the job name"),
    Term("ds:ServerJob", K.JOB, "lossy", "a server job (job type 0)"),
    Term("ds:SequenceJob", K.JOB, "lossy", "a job sequence (job type 2): activities run in trigger order"),
    Term("ds:MainframeJob", K.JOB, "lossy", "a mainframe job (job type 1); its stages are read like a parallel job's"),
    Term("ds:Stage", K.TASK, "lossy", "a stage of a parallel, server or mainframe job; its stage type and family in attributes"),
    Term("ds:Activity", K.TASK, "lossy", "an activity of a job sequence: run a job or routine, wait, notify, branch"),
    Term("ds:Routine", K.PROGRAM, "lossy", "a server or parallel routine; its BASIC or C++ source is not lowered"),
    Term("ds:TableDefinition", K.DATA_SCHEMA, "lossy", "a table definition from the repository's metadata; named datastage:<category path>"),
    Term("ds:Column", K.FIELD, "lossy", "a column of a table definition; SQL type, length, scale, nullability and key position in attributes"),
)

# stage type (lower case) -> (canonical name, family, dialect, description)
_STAGES: list[tuple[str, str, str | None, str]] = [
    # files and data sets
    ("PxSequentialFile", "file", None, "sequential file read or written by a parallel job"),
    ("PxDataSet", "file", None, "parallel data set (.ds)"),
    ("PxFileSet", "file", None, "parallel file set"),
    ("PxLookupFileSet", "file", None, "lookup file set"),
    ("PxComplexFlatFile", "file", None, "complex flat file (COBOL layouts)"),
    ("CSeqFileStage", "file", None, "server sequential file"),
    ("CHashedFileStage", "file", None, "server hashed file"),
    ("PxExternalSource", "other", None, "external source program"),
    ("PxExternalTarget", "other", None, "external target program"),
    ("XMLStagePX", "other", None, "hierarchical data (XML, JSON) stage"),
    ("PxXMLInput", "other", None, "XML input"),
    ("PxXMLOutput", "other", None, "XML output"),
    # databases
    ("OracleConnectorPX", "database", "oracle", "Oracle connector"),
    ("DB2ConnectorPX", "database", "db2", "DB2 connector"),
    ("ODBCConnectorPX", "database", None, "ODBC connector"),
    ("TeradataConnectorPX", "database", "teradata", "Teradata connector"),
    ("NetezzaConnectorPX", "database", "netezza", "Netezza connector"),
    ("SybaseConnectorPX", "database", "sybase", "Sybase connector"),
    ("PxOracle", "database", "oracle", "Oracle enterprise stage"),
    ("PxDB2", "database", "db2", "DB2 enterprise stage"),
    ("PxTeradata", "database", "teradata", "Teradata enterprise stage"),
    ("PxODBC", "database", None, "ODBC enterprise stage"),
    ("PxNetezza", "database", "netezza", "Netezza enterprise stage"),
    ("ORAOCI9", "database", "oracle", "server Oracle OCI stage"),
    ("ORAOCIBL", "database", "oracle", "server Oracle bulk loader"),
    ("ODBC", "database", None, "server ODBC stage"),
    ("DB2UDBAPI", "database", "db2", "server DB2 API stage"),
    ("DRS", "database", None, "dynamic relational stage"),
    ("TDMLoad", "database", "teradata", "server Teradata MultiLoad"),
    # processing
    ("CTransformerStage", "transformer", None, "transformer: constraints, derivations and stage variables"),
    ("PxLookup", "lookup", None, "lookup against reference links"),
    ("PxJoin", "join", None, "join on keys"),
    ("PxMerge", "merge", None, "merge of a master and update links on keys"),
    ("PxSort", "sort", None, "sort"),
    ("PxRemDup", "dedup", None, "remove duplicates by key"),
    ("PxAggregator", "aggregate", None, "aggregator: group by keys"),
    ("PxFunnel", "funnel", None, "funnel: several inputs into one"),
    ("PxFilter", "filter", None, "filter by where clauses, one per output"),
    ("PxSwitch", "switch", None, "switch on a selector column"),
    ("PxCopy", "copy", None, "copy rows to one or more outputs, dropping or renaming columns"),
    ("PxModify", "modify", None, "modify: keep, drop, rename and convert columns"),
    ("PxPivot", "pivot", None, "pivot: columns into rows"),
    ("PxChangeCapture", "change_capture", None, "change capture: compare before and after data"),
    ("PxChangeApply", "change_apply", None, "change apply: apply change codes to before data"),
    ("PxSCD", "scd", None, "slowly changing dimension"),
    ("PxSurrogateKeyGenerator", "other", None, "surrogate key generator"),
    ("PxRowGenerator", "other", None, "row generator"),
    ("PxColumnGenerator", "other", None, "column generator"),
    ("PxPeek", "other", None, "peek: rows to the job log"),
    ("PxHead", "other", None, "first rows"),
    ("PxTail", "other", None, "last rows"),
    ("PxSample", "other", None, "sample of rows"),
    ("PxCompare", "other", None, "compare two inputs"),
    ("PxDifference", "other", None, "difference of two inputs"),
    ("PxExternalFilter", "other", None, "external filter program"),
    ("PxWrapped", "other", None, "wrapped operating-system command"),
    ("PxBuildOp", "other", None, "build stage (C++ code)"),
    ("PxCustom", "other", None, "custom operator"),
    ("PxChecksum", "other", None, "checksum"),
    ("PxColumnImport", "other", None, "column import"),
    ("PxColumnExport", "other", None, "column export"),
    ("PxMakeVector", "other", None, "make vector"),
    ("PxSplitVector", "other", None, "split vector"),
    ("PxCombineRecords", "other", None, "combine records"),
    ("PxMakeSubrec", "other", None, "make subrecord"),
    ("PxSplitSubrec", "other", None, "split subrecord"),
    ("PxPromoteSubrec", "other", None, "promote subrecord"),
    ("PxEncode", "other", None, "encode"),
    ("PxDecode", "other", None, "decode"),
    ("PxCompress", "other", None, "compress"),
    ("PxExpand", "other", None, "expand"),
    ("CContainerStage", "container", None, "local or shared container"),
    # sequence activities
    ("CJSJobActivity", "job_activity", None, "run a job"),
    ("CJSRoutineActivity", "routine_activity", None, "run a routine"),
    ("CJSExecCmdActivity", "command_activity", None, "run an operating-system command; the command is never copied"),
    ("CJSMailActivity", "activity", None, "send a notification; addresses are never copied"),
    ("CJSSequencer", "activity", None, "sequencer: wait for all or any inputs"),
    ("CJSCondition", "activity", None, "nested condition"),
    ("CJSStartLoop", "activity", None, "start of a loop"),
    ("CJSEndLoop", "activity", None, "end of a loop"),
    ("CJSUserVarsActivity", "activity", None, "user variables"),
    ("CJSWaitForFileActivity", "activity", None, "wait for a file"),
    ("CJSTerminatorActivity", "activity", None, "terminate the sequence"),
    ("CJSExceptionHandler", "activity", None, "exception handler"),
]
STAGES: dict[str, tuple[str, str, str | None]] = {name.lower(): (name, family, dialect) for name, family, dialect, _ in _STAGES}
# The ways a stage is modelled, by family: what its construct becomes.
_FAMILY_MODELLED = {
    "file": "entity:Dataset", "database": "entity:Table", "transformer": "behavior:flow", "lookup": "attribute:transforms",
    "join": "attribute:transforms", "merge": "attribute:transforms", "sort": "attribute:transforms", "dedup": "attribute:transforms",
    "aggregate": "attribute:transforms", "funnel": "attribute:transforms", "filter": "attribute:transforms", "switch": "attribute:transforms",
    "copy": "attribute:transforms", "modify": "attribute:transforms", "pivot": "attribute:transforms", "change_capture": "unmapped",
    "change_apply": "attribute:transforms", "scd": "attribute:transforms", "other": "unmapped", "container": "unmapped",
    "job_activity": "relation:RUNS", "routine_activity": "relation:RUNS", "command_activity": "unmapped", "activity": "entity:Task",
}

_OTHER = (
    ("job", "entity:Job", "a job: parallel, server, sequence or mainframe"),
    ("job_parameter", "attribute:parameters", "a job parameter: its name only, never its default value"),
    ("parameter_set", "unmapped", "a parameter set: named values shared by jobs"),
    ("link", "relation:DEPENDS_ON", "a link between two stages, or a trigger between two activities"),
    ("stage_variable", "behavior:assign", "a transformer stage variable"),
    ("constraint", "behavior:if", "a transformer output link's constraint"),
    ("derivation", "behavior:assign", "a transformer output column's derivation"),
    ("expression_unparsed", "unmapped", "an expression the pack's grammar does not read: kept as a platform node"),
    ("substring", "behavior:platform", "a substring x[start, length]"),
    ("trigger_expression", "behavior:if", "a custom trigger expression of a sequence"),
    ("table_definition", "entity:DataSchema", "a table definition with its columns"),
    ("routine", "entity:Program", "a routine"),
    ("routine_source", "unmapped", "the BASIC or C++ source of a routine: not lowered"),
    ("job_control", "unmapped", "job control code written in BASIC in a server job or sequence"),
    ("before_after_routine", "unmapped", "a before-job or after-job subroutine"),
    ("shared_container", "unmapped", "a shared container definition"),
    ("compiled_job", "unmapped", "compiled job runtime information (DSEXECJOB)"),
    ("annotation", "unmapped", "a description annotation on the canvas"),
    ("export_other", "unmapped", "another kind of exported object (stage types, data elements, transforms, IMS definitions)"),
    ("dynamic_name", "unmapped", "a file or table named by job parameters: the transform stays dynamic"),
    ("isx_member", "unmapped", "a member of an .isx archive that is not a DataStage export"),
    ("stage.unknown", "unmapped", "a stage type this pack does not know"),
)


def _constructs() -> tuple[Construct, ...]:
    out = [Construct(f"ds:stage.{name}", text, _FAMILY_MODELLED[family]) for name, family, _, text in _STAGES]
    out += [Construct(f"ds:{name}", text, modelled) for name, modelled, text in _OTHER]
    return tuple(out)


CONSTRUCTS = _constructs()


def stage_kind(stage_type: str) -> tuple[str, str, str | None] | None:
    """(canonical name, family, dialect) of a stage type, or None when the pack does not know it.
    A sequence activity's type is matched with or without its leading `C` (`JSJobActivity`)."""
    low = stage_type.lower()
    if low in STAGES:
        return STAGES[low]
    if low.startswith("js") and ("c" + low) in STAGES:
        return STAGES["c" + low]
    return None
