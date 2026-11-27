"""The Easytrieve pack's vocabulary: platform terms and the constructs it looks for.

Keys: a program (one member) is `Program {platform: "mainframe", name: <member>}`
(contract 11.1 item 2); a procedure is `Symbol {platform: "mainframe", program, name}`, as in
the PL/I pack; a FILE with fields is `DataSchema {kind: "easytrieve-file", name:
"<program>.<file>"}` whose fields are `Field {table: "easytrieve:<program>.<file>", name}`
(10.1 item 6 with the pack id as platform); a report is
`Report {platform: "easytrieve", name: "<program>.<report>"}`, declared through the
`ezt:Report` term (`easytrieve` is one of the Explorer's `UI_PLATFORMS`, contract 11.3 item 7).
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("ezt:Program", K.PROGRAM, "lossy", "an Easytrieve program (one member): its activities, files and reports in attributes"),
    Term("ezt:Procedure", K.SYMBOL, "exact", "a procedure (`name. PROC` ... `END-PROC`) of a job, sort or report"),
    Term("ezt:File", K.DATA_SCHEMA, "lossy", "a FILE and its fields: the record layout of the file the DD name binds"),
    Term("ezt:Field", K.FIELD, "lossy", "a field of a FILE: offset, length, data format and decimals in attributes"),
    Term("ezt:Report", K.REPORT, "lossy", "a REPORT section: its printer file, sequence, control breaks, sums and the fields its lines print in attributes; title text is not copied"),
)

_CONSTRUCTS = (
    # Library section.
    ("parm", "PARM options of the program", "unmapped"),
    ("file", "a FILE with a DD name: its fields are a DataSchema", "entity:DataSchema"),
    ("file_special", "a VIRTUAL, CARD, PUNCH, TABLE, SQL, DLI or IDMS file: no DD name to bind", "unmapped"),
    ("field", "a field definition of a FILE", "entity:Field"),
    ("working_storage", "a working-storage field (W or S)", "unmapped"),
    ("copy", "COPY of another file's fields", "unmapped"),
    ("record", "RECORD, ELEMENT-RECORD or LOGICAL-RECORD (IDMS and DL/I files)", "unmapped"),
    ("macro_call", "a %macro invocation: the macro's statements are in another member", "unmapped"),
    ("macro_definition", "MACRO ... MEND in a macro member", "unmapped"),
    # Activities.
    ("job", "JOB INPUT file: an implicit loop over the records", "behavior:loop"),
    ("job_input_null", "JOB INPUT NULL: the job reads its own files", "behavior:data_step"),
    ("synchronized_input", "JOB INPUT of several files with keys (matched processing)", "unmapped"),
    ("sql_input", "JOB INPUT SQL: the rows of an SQL cursor", "unmapped"),
    ("sort", "SORT file TO file USING keys", "transform:sort"),
    ("sort_before", "SORT ... BEFORE proc: a procedure that selects the records sorted", "unmapped"),
    ("program_activity", "a PROGRAM activity that runs others with EXECUTE", "unmapped"),
    ("screen_activity", "a SCREEN activity (online)", "unmapped"),
    ("execute", "EXECUTE of an activity", "unmapped"),
    ("proc", "a procedure: `name. PROC` ... `END-PROC`", "entity:Symbol"),
    # Reports.
    ("report", "a REPORT section", "entity:Report"),
    ("report_sequence", "SEQUENCE of a report", "attribute:sequence"),
    ("report_control", "CONTROL breaks of a report", "attribute:control"),
    ("report_sum", "SUM fields of a report", "attribute:sum"),
    ("report_title", "TITLE lines of a report (their text is not copied)", "attribute:titles"),
    ("report_heading", "HEADING of a field in a report", "unmapped"),
    ("report_line", "LINE: the fields a report prints", "attribute:lines"),
    ("report_procedure", "a report procedure (REPORT-INPUT, BEFORE-BREAK, AFTER-BREAK, BEFORE-LINE, AFTER-LINE, ENDPAGE, TERMINATION)", "entity:Symbol"),
    # Statements.
    ("assignment", "an assignment or arithmetic", "behavior:assign"),
    ("move", "MOVE ... TO", "behavior:assign"),
    ("move_like", "MOVE LIKE: fields moved by name", "unmapped"),
    ("if", "IF ... ELSE-IF ... ELSE ... END-IF", "behavior:if"),
    ("do", "DO WHILE or DO UNTIL ... END-DO", "behavior:loop"),
    ("case", "CASE ... WHEN ... OTHERWISE ... END-CASE", "behavior:switch"),
    ("perform", "PERFORM of a procedure", "relation:CALLS"),
    ("print", "PRINT of a report", "behavior:output"),
    ("put", "PUT of a record to a file", "behavior:output"),
    ("write", "WRITE of a record (UPDATE, ADD, DELETE)", "behavior:output"),
    ("get", "GET of a record", "behavior:platform"),
    ("read", "READ of a keyed record", "behavior:platform"),
    ("point", "POINT to a key", "behavior:platform"),
    ("close", "CLOSE of a file", "behavior:platform"),
    ("display", "DISPLAY", "behavior:output"),
    ("stop", "STOP", "behavior:exit"),
    ("goto", "GOTO JOB (the next record) or GOTO a label", "behavior:continue"),
    ("call", "CALL of a program", "relation:CALLS"),
    ("search", "SEARCH of a table file", "behavior:platform"),
    ("select", "SELECT of a record in a report or sort procedure", "behavior:platform"),
    ("sql", "SQL: an embedded SQL statement", "behavior:query"),
    ("sql_dynamic", "SQL PREPARE or EXECUTE: the statement is built at run time", "unmapped"),
    ("dli", "a DLI statement (IMS)", "unmapped"),
    ("idms", "an IDMS statement", "unmapped"),
    ("other_statement", "a statement this pack does not lower", "unmapped"),
    ("unparsed", "a statement the reader could not read", "unmapped"),
)

CONSTRUCTS = tuple(sorted((Construct(f"ezt:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS), key=lambda c: c.id))
