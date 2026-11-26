"""The PL/SQL pack's vocabulary: platform terms and the constructs it looks for.

Programs, Symbols and Jobs are keyed as the contract says: a package, stand-alone
procedure or function, trigger or script is `Program {platform: "oracle", name}`; a
package's procedure or function is `Symbol {platform: "oracle", program, name}`; a
scheduler job is `Job {framework, name}`. Oracle names are case-insensitive unless
quoted, so unquoted names are lower-cased.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "oracle"  # Program.platform for Oracle (docs/solution-ir-contract.md, section 10.1)
REALM = "oracle"  # Role and Permission realm: a source file does not name its database

TERMS = (
    Term("ora:Package", K.PROGRAM, "lossy", "a package: its specification and body declare the same Program, which CONTAINS its procedures and functions"),
    Term("ora:Procedure", K.PROGRAM, "exact", "a stand-alone procedure (CREATE PROCEDURE)"),
    Term("ora:Function", K.PROGRAM, "exact", "a stand-alone function (CREATE FUNCTION)"),
    Term("ora:Trigger", K.PROGRAM, "lossy", "a database trigger; the table, events and timing it fires on are in attributes.on"),
    Term("ora:Script", K.PROGRAM, "lossy", "the anonymous blocks of a script that run code or SQL, named by the script's path"),
    Term("ora:PackageProcedure", K.SYMBOL, "exact", "a procedure of a package"),
    Term("ora:PackageFunction", K.SYMBOL, "exact", "a function of a package"),
    Term("ora:DatabaseLink", K.CONFIG_KEY, "lossy", "a database link by name (`dblink:<name>`); the user, password and connect string are never read"),
    Term("ora:Role", K.ROLE, "exact", "a grantee of a GRANT: a role, a user or PUBLIC; realm 'oracle'"),
    Term("ora:Grant", K.PERMISSION, "lossy", "one privilege, on one object when it names one: `SELECT on sales.orders`, `CREATE SESSION`"),
    Term("ora:SchedulerJob", K.JOB, "lossy", "a DBMS_SCHEDULER job with a literal name; job type and repeat interval in attributes"),
    Term("ora:Job", K.JOB, "lossy", "a DBMS_JOB job, named by the procedures its literal WHAT block calls; its interval in attributes"),
)

_CONSTRUCTS = (
    # Units of code.
    ("create_package", "package specification (CREATE PACKAGE)", "entity:Program"),
    ("create_package_body", "package body (CREATE PACKAGE BODY)", "entity:Program"),
    ("create_procedure", "stand-alone procedure", "entity:Program"),
    ("create_function", "stand-alone function", "entity:Program"),
    ("create_trigger", "trigger on a table or view, or on a schema or database event", "entity:Program"),
    ("compound_trigger", "compound trigger: its timing sections become platform nodes", "behavior:platform"),
    ("create_type", "object or collection type specification", "unmapped"),
    ("create_type_body", "object type body and its methods", "unmapped"),
    ("create_java", "CREATE JAVA SOURCE or CLASS", "unmapped"),
    ("package_procedure", "procedure declared or defined in a package", "entity:Symbol"),
    ("package_function", "function declared or defined in a package", "entity:Symbol"),
    ("local_subprogram", "procedure or function declared inside another one", "behavior:declare"),
    ("call_spec", "call specification: LANGUAGE JAVA, LANGUAGE C or EXTERNAL", "unmapped"),
    ("wrapped", "wrapped (obfuscated) source: the unwrapped source is needed", "unmapped"),
    ("initialization_section", "a package body's initialization block", "behavior:open_code"),
    ("anonymous_block", "anonymous block in a script (DECLARE or BEGIN ... END)", "behavior:open_code"),
    # Declarations.
    ("variable", "variable declaration", "behavior:declare"),
    ("constant", "constant declaration", "behavior:declare"),
    ("cursor", "explicit cursor declaration; its query runs where it is opened", "behavior:query"),
    ("type_declaration", "TYPE or SUBTYPE: record, collection or REF CURSOR type", "unmapped"),
    ("exception_declaration", "user-defined exception", "behavior:declare"),
    ("pragma_autonomous_transaction", "PRAGMA AUTONOMOUS_TRANSACTION", "attribute:autonomous_transaction"),
    ("pragma_exception_init", "PRAGMA EXCEPTION_INIT", "unmapped"),
    ("pragma_other", "another pragma (SERIALLY_REUSABLE, RESTRICT_REFERENCES, INLINE, UDF, DEPRECATED)", "unmapped"),
    ("subprogram_option", "DETERMINISTIC, PIPELINED, PARALLEL_ENABLE, RESULT_CACHE, AUTHID, ACCESSIBLE BY", "attribute:options"),
    # Statements.
    ("assignment", "assignment", "behavior:assign"),
    ("if", "IF ... ELSIF ... ELSE", "behavior:if"),
    ("case_statement", "CASE statement", "behavior:switch"),
    ("basic_loop", "LOOP ... END LOOP", "behavior:loop"),
    ("while_loop", "WHILE loop", "behavior:loop"),
    ("for_loop", "numeric FOR loop", "behavior:loop"),
    ("cursor_for_loop", "cursor FOR loop over a cursor or a query", "behavior:loop"),
    ("exit", "EXIT or CONTINUE, with or without WHEN", "behavior:break"),
    ("goto", "GOTO", "behavior:platform"),
    ("label", "a <<label>>", "unmapped"),
    ("return", "RETURN", "behavior:return"),
    ("raise", "RAISE of a named or the current exception", "behavior:throw"),
    ("raise_application_error", "RAISE_APPLICATION_ERROR", "behavior:throw"),
    ("exception_handler", "WHEN ... THEN in an EXCEPTION section", "behavior:try"),
    ("procedure_call", "call of a procedure or function that is not built in", "relation:CALLS"),
    ("select_into", "SELECT ... INTO", "behavior:query"),
    ("insert", "INSERT", "behavior:query"),
    ("update", "UPDATE", "behavior:query"),
    ("delete", "DELETE", "behavior:query"),
    ("merge", "MERGE", "behavior:query"),
    ("lock_table", "LOCK TABLE", "behavior:query"),
    ("returning_into", "RETURNING ... INTO of a DML statement", "behavior:query"),
    ("bulk_collect", "BULK COLLECT INTO", "behavior:query"),
    ("forall", "FORALL bulk DML", "behavior:platform"),
    ("open_cursor", "OPEN of an explicit cursor", "behavior:query"),
    ("open_for", "OPEN ... FOR of a cursor variable", "behavior:query"),
    ("fetch", "FETCH", "behavior:assign"),
    ("close_cursor", "CLOSE", "behavior:platform"),
    ("cursor_attribute", "%FOUND, %NOTFOUND, %ROWCOUNT, %ISOPEN, %BULK_ROWCOUNT", "behavior:member"),
    ("execute_immediate", "EXECUTE IMMEDIATE: modelled when its text is literal; text built at run time stays dynamic", "behavior:query"),
    ("dbms_sql", "DBMS_SQL: modelled when the text PARSE is given is literal", "behavior:query"),
    ("commit", "COMMIT", "behavior:transaction"),
    ("rollback", "ROLLBACK, to a savepoint or not", "behavior:transaction"),
    ("savepoint", "SAVEPOINT", "behavior:transaction"),
    ("set_transaction", "SET TRANSACTION", "behavior:transaction"),
    ("pipe_row", "PIPE ROW of a pipelined function", "behavior:output"),
    ("dbms_output", "DBMS_OUTPUT", "behavior:output"),
    ("utl_file", "UTL_FILE: file reads and writes through a directory object", "behavior:output"),
    ("htp", "HTP and HTF (the PL/SQL web toolkit)", "behavior:output"),
    ("mail", "UTL_MAIL or UTL_SMTP; addresses are never copied", "behavior:output"),
    ("utl_http", "UTL_HTTP request", "unmapped"),
    ("dbms_aq", "DBMS_AQ enqueue or dequeue", "unmapped"),
    ("dbms_pipe", "DBMS_PIPE or DBMS_ALERT", "unmapped"),
    ("dbms_lock", "DBMS_LOCK", "unmapped"),
    ("sequence", "sequence NEXTVAL or CURRVAL", "unmapped"),
    ("remote_object", "a table or view over a database link (name@link): REFERENCES_CONFIG the link", "relation:REFERENCES_CONFIG"),
    ("remote_call", "a call over a database link", "unmapped"),
    ("unrecognised_statement", "a statement this parser does not read: kept as a platform node", "behavior:platform"),
    ("unrecognised_expression", "an expression this parser does not read: kept as a platform node", "behavior:platform"),
    # Scripts and DDL.
    ("create_synonym", "synonym: ALIAS_OF from its name to the object, kept when the unit declares both as tables or both as programs", "relation:ALIAS_OF"),
    ("create_database_link", "database link", "entity:ConfigKey"),
    ("grant_object", "GRANT of a privilege on an object", "relation:GRANTS"),
    ("grant_system", "GRANT of a system privilege", "relation:GRANTS"),
    ("grant_role", "GRANT of a role to a user or role", "relation:GRANTS"),
    ("revoke", "REVOKE", "unmapped"),
    ("dbms_scheduler_job", "DBMS_SCHEDULER.CREATE_JOB", "entity:Job"),
    ("dbms_scheduler_program", "DBMS_SCHEDULER.CREATE_PROGRAM: what a job names by program", "attribute:program"),
    ("dbms_scheduler_schedule", "DBMS_SCHEDULER.CREATE_SCHEDULE: a job's named schedule", "attribute:schedule"),
    ("dbms_scheduler_chain", "DBMS_SCHEDULER chains and chain steps", "unmapped"),
    ("dbms_scheduler_other", "another DBMS_SCHEDULER call (RUN_JOB, ENABLE, SET_ATTRIBUTE, DROP_JOB, ...)", "unmapped"),
    ("dbms_job", "DBMS_JOB.SUBMIT", "entity:Job"),
    ("sqlplus", "SQL*Plus command (SET, PROMPT, SPOOL, SHOW ERRORS, WHENEVER, DEFINE, @script, ...)", "unmapped"),
    ("other_ddl", "other DDL (tables, indexes, sequences, views, ALTER, DROP): read by the SQL DDL extractor in .sql files", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"ora:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
