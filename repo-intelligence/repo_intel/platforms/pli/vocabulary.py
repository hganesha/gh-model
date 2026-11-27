"""The PL/I pack's vocabulary: platform terms and the constructs it looks for.

Keys (docs/solution-ir-contract.md, section 11.1): an external procedure is the load module
`Program {platform: "mainframe", name: <entry name, upper case>}` (item 2); an internal
procedure is `Symbol {platform: "mainframe", program: <that name>, name: <procedure name>}`,
following the PL/SQL pack's Symbol key; an `%INCLUDE` member is
`DataSchema {kind: "include", name: <member>}` and its declarations are
`Field {table: "pli:<member>", name}` (item 3).
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("pli:Program", K.PROGRAM, "lossy", "an external procedure (OPTIONS(MAIN) or a subroutine): the load module, named by its entry; options, entries and files in attributes"),
    Term("pli:Procedure", K.SYMBOL, "exact", "an internal procedure of a program"),
    Term("pli:IncludeMember", K.DATA_SCHEMA, "lossy", "an %INCLUDE member that declares data: its structures are the member's Fields"),
    Term("pli:Field", K.FIELD, "lossy", "an element of a structure or scalar declared in an %INCLUDE member; level, type, usage, picture, occurs, redefines, offset and length in attributes"),
)

_CONSTRUCTS = (
    # Compilation and procedures.
    ("process", "*PROCESS or %PROCESS compiler options (MARGINS are applied)", "attribute:margins"),
    ("procedure_main", "an external procedure with OPTIONS(MAIN)", "entity:Program"),
    ("procedure_external", "an external procedure that is not MAIN: a subroutine or function load module", "entity:Program"),
    ("internal_procedure", "a procedure nested in another", "entity:Symbol"),
    ("package", "a PACKAGE: its procedures are external entries", "attribute:package"),
    ("begin_block", "a BEGIN block (lowered in line)", "behavior:block"),
    ("entry_statement", "a secondary ENTRY statement of a procedure", "attribute:entries"),
    ("include", "%INCLUDE or %XINCLUDE of a member", "relation:DEPENDS_ON"),
    ("preprocessor", "another preprocessor statement (%DCL, %IF, %DO, %PAGE, %SKIP, %REPLACE ...)", "unmapped"),
    # Declarations.
    ("declare", "a level-1 declaration in a procedure", "behavior:declare"),
    ("member_declare", "a level-1 declaration in an %INCLUDE member", "entity:Field"),
    ("unnamed_element", "an unnamed (*) element of a structure in an %INCLUDE member: not declared", "unmapped"),
    ("declare_file", "a FILE constant: its DD name and how it is opened", "attribute:files"),
    ("declare_entry", "an ENTRY declaration (EXTERNAL entries are the programs called)", "relation:CALLS"),
    ("based_or_controlled", "BASED or CONTROLLED storage: its address or allocation is set at run time", "unmapped"),
    # Statements.
    ("assignment", "an assignment, with several targets, a compound operator or BY NAME", "behavior:assign"),
    ("if", "IF ... THEN ... ELSE", "behavior:if"),
    ("do_group", "a DO; ... END; group (lowered in line)", "behavior:block"),
    ("do_while", "DO WHILE or DO UNTIL", "behavior:loop"),
    ("do_forever", "DO FOREVER or DO LOOP", "behavior:loop"),
    ("do_range", "an iterative DO (TO, BY, REPEAT, several specifications)", "behavior:loop"),
    ("do_list", "DO over a list of values", "behavior:loop"),
    ("select", "SELECT ... WHEN ... OTHERWISE", "behavior:switch"),
    ("call", "CALL of an internal procedure or an external program by name", "relation:CALLS"),
    ("call_dynamic", "CALL through an ENTRY variable or parameter: the target is decided at run time", "unmapped"),
    ("service_call", "CALL of a run-time service (PLIRETC, PLIDUMP, CEE... Language Environment services, DSNTIAR)", "behavior:call"),
    ("plitdli", "a DL/I call (PLITDLI, AIBTDLI, CEETDLI): function and literal SSA segments", "attribute:dli_segments"),
    ("mq_call", "an MQ call (MQCONN, MQOPEN, MQGET, MQPUT ...)", "unmapped"),
    ("plisort", "a sort through PLISRTA, PLISRTB, PLISRTC or PLISRTD: its control statements are strings", "unmapped"),
    ("return", "RETURN", "behavior:return"),
    ("stop", "STOP or EXIT", "behavior:exit"),
    ("goto", "GO TO", "behavior:platform"),
    ("leave", "LEAVE", "behavior:break"),
    ("iterate", "ITERATE", "behavior:continue"),
    ("signal", "SIGNAL or RESIGNAL", "behavior:throw"),
    ("revert", "REVERT of an ON-unit", "behavior:platform"),
    ("on_unit", "an ON-unit: ENDFILE, ERROR, KEY, CONVERSION ...", "behavior:platform"),
    ("allocate", "ALLOCATE or FREE of controlled or based storage", "behavior:platform"),
    ("fetch", "FETCH or RELEASE of a load module", "behavior:platform"),
    ("read", "READ FILE(...) INTO or SET", "behavior:platform"),
    ("write", "WRITE, REWRITE or LOCATE FILE(...) FROM", "behavior:output"),
    ("delete", "DELETE or UNLOCK of a keyed record", "behavior:platform"),
    ("open", "OPEN FILE(...) INPUT, OUTPUT or UPDATE, with TITLE", "attribute:files"),
    ("close", "CLOSE FILE(...)", "behavior:platform"),
    ("get", "GET LIST, EDIT, DATA or STRING", "behavior:platform"),
    ("put", "PUT LIST, EDIT, DATA or STRING", "behavior:output"),
    ("display", "DISPLAY", "behavior:output"),
    ("exec_sql_query", "EXEC SQL SELECT, INSERT, UPDATE, DELETE, MERGE and other statements", "behavior:query"),
    ("exec_sql_cursor", "EXEC SQL DECLARE CURSOR, OPEN, FETCH and CLOSE", "behavior:query"),
    ("exec_sql_include", "EXEC SQL INCLUDE of a member (SQLCA and SQLDA are not followed)", "relation:DEPENDS_ON"),
    ("exec_sql_commit", "EXEC SQL COMMIT or ROLLBACK", "behavior:transaction"),
    ("exec_sql_whenever", "EXEC SQL WHENEVER", "behavior:platform"),
    ("exec_sql_dynamic", "EXEC SQL PREPARE, EXECUTE or DESCRIBE: the statement is built at run time", "unmapped"),
    ("exec_sql_other", "another EXEC SQL statement (DECLARE TABLE, CONNECT ...)", "unmapped"),
    ("exec_cics", "an EXEC CICS command (LINK and XCTL to a literal program are CALLS; SEND MAP RENDERS the map)", "relation:CALLS"),
    ("exec_other", "EXEC DLI or another EXEC interface", "unmapped"),
    ("other_statement", "a statement this pack does not lower (DELAY, WAIT, FORMAT, ATTACH, DEFAULT ...)", "unmapped"),
    ("unparsed", "a statement the parser could not read", "unmapped"),
)

CONSTRUCTS = tuple(sorted((Construct(f"pli:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS), key=lambda c: c.id))
