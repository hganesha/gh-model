"""The teradata pack's vocabulary: load and export utility scripts (BTEQ stays with `sql_lineage`)."""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "teradata"  # Program.platform, as `sql_lineage` uses it for BTEQ scripts and procedures

TERMS = (
    Term("td:FastLoadScript", K.PROGRAM, "lossy", "a FastLoad script: loads one empty table from a file; name is its path"),
    Term("td:MultiLoadScript", K.PROGRAM, "lossy", "a MultiLoad script: inserts, updates, upserts or deletes rows of up to five tables from a file; name is its path"),
    Term("td:TPumpScript", K.PROGRAM, "lossy", "a TPump script: continuous row-level loads with the MultiLoad script language; name is its path"),
    Term("td:FastExportScript", K.PROGRAM, "lossy", "a FastExport script: writes the result of SELECTs to a file; name is its path"),
    Term("td:TptJob", K.PROGRAM, "lossy", "a Teradata Parallel Transporter job script (DEFINE JOB); name is its path, the job name in `job`"),
    Term("td:RecordLayout", K.DATA_SCHEMA, "lossy", "a record layout of the input or output file: a MultiLoad or TPump .LAYOUT, a FastLoad DEFINE or a TPT DEFINE SCHEMA"),
    Term("td:LayoutField", K.FIELD, "lossy", "a field of a record layout: `{table: \"teradata:<layout>\", name}`, its type and position in attributes"),
)

_CONSTRUCTS = (
    # Shared by FastLoad, MultiLoad, TPump and FastExport
    ("logon", ".LOGON: the logon string is never read", "unmapped"),
    ("logoff", ".LOGOFF or QUIT", "unmapped"),
    ("logtable", ".LOGTABLE restart log table", "attribute:log_table"),
    ("database", "DATABASE: the default database for names without one", "attribute:default_database"),
    ("sessions", "SESSIONS, TENACITY, SLEEP, CHECKPOINT, ERRLIMIT and other run options", "unmapped"),
    ("layout", ".LAYOUT (MultiLoad, TPump, FastExport) or DEFINE (FastLoad): the input record layout", "entity:DataSchema"),
    ("field", ".FIELD or a DEFINE field", "entity:Field"),
    ("filler", ".FILLER: a field that is read and skipped", "attribute:filler"),
    ("layout-table", ".TABLE: a layout taken from a table's columns", "unmapped"),
    ("record-format", "SET RECORD, RECORD or FORMAT: how the file is read", "attribute:format"),
    ("import", ".IMPORT INFILE or FastLoad's FILE=: the input file", "relation:READS_FROM"),
    ("inmod", "an INMOD or AXSMOD input routine: the file is read by code that is not here", "unmapped"),
    ("insert", "INSERT of the input fields", "relation:WRITES_TO"),
    ("update", "UPDATE from the input fields", "relation:WRITES_TO"),
    ("delete", "DELETE (a MultiLoad delete task or a DML label)", "relation:WRITES_TO"),
    ("upsert", "DO INSERT FOR MISSING UPDATE ROWS: an upsert", "attribute:primitive"),
    ("sql", "other SQL in the script (DDL, a DELETE before the load, COLLECT STATISTICS)", "relation:WRITES_TO"),
    ("support-if", ".IF, .ELSE, .ENDIF of the support environment", "unmapped"),
    ("support-set", ".SET, .ACCEPT and &variable substitution of the support environment", "unmapped"),
    ("support-other", ".DISPLAY, .ROUTE, .SYSTEM, .RUN FILE, .DATEFORM and other support commands", "unmapped"),
    ("dynamic", "a table or file named through a variable whose value is not in the file", "unmapped"),
    # FastLoad
    ("begin-loading", "BEGIN LOADING: the target table and its error tables", "relation:WRITES_TO"),
    ("end-loading", "END LOADING", "unmapped"),
    # MultiLoad and TPump
    ("begin-mload", ".BEGIN [IMPORT|DELETE] MLOAD or .BEGIN LOAD (TPump): target, work and error tables", "relation:WRITES_TO"),
    ("dml-label", ".DML LABEL: a named group of DML statements applied to input rows", "behavior:query"),
    ("apply", "APPLY label [WHERE condition] in .IMPORT", "attribute:apply"),
    ("end-mload", ".END MLOAD or .END LOAD", "unmapped"),
    # FastExport
    ("begin-export", ".BEGIN EXPORT", "unmapped"),
    ("export", ".EXPORT OUTFILE: the output file", "relation:WRITES_TO"),
    ("select", "SELECT whose rows are exported", "relation:READS_FROM"),
    ("end-export", ".END EXPORT", "unmapped"),
    # Teradata Parallel Transporter
    ("tpt.job", "DEFINE JOB", "entity:Program"),
    ("tpt.schema", "DEFINE SCHEMA, with its columns or FROM TABLE", "entity:DataSchema"),
    ("tpt.operator.load", "a LOAD, UPDATE, STREAM or INSERTER operator: writes TargetTable", "relation:WRITES_TO"),
    ("tpt.operator.export", "an EXPORT, SELECTOR or ODBC operator: reads SelectStmt", "relation:READS_FROM"),
    ("tpt.operator.file", "a DATACONNECTOR PRODUCER or CONSUMER: reads or writes DirectoryPath and FileName", "relation:READS_FROM"),
    ("tpt.operator.ddl", "a DDL operator", "relation:WRITES_TO"),
    ("tpt.operator.other", "an OS COMMAND or other operator", "unmapped"),
    ("tpt.apply", "APPLY ... TO OPERATOR ... SELECT ... FROM OPERATOR", "relation:WRITES_TO"),
    ("tpt.step", "STEP", "unmapped"),
    ("tpt.include", "INCLUDE of another script file", "unmapped"),
    ("tpt.job-variable", "@variable: a job variable whose value is in a job variables file, not here", "unmapped"),
    ("tpt.case", "CASE WHEN ... THEN (DML) in APPLY", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"td:{name}", description, modelled_as) for name, description, modelled_as in _CONSTRUCTS)
