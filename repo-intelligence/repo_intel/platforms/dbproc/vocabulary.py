"""The dbproc pack's vocabulary: DB2 SQL PL, Netezza NZPLSQL and Sybase ASE routines."""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

ENGINES = ("db2", "netezza", "sybase")  # Program.platform, as sql_lineage.DIALECT names them
LANGUAGE = {"db2": "sqlpl", "netezza": "nzplsql", "sybase": "tsql"}

TERMS = (
    Term("dbp:Procedure", K.PROGRAM, "lossy", "a stored procedure: `{platform: db2|netezza|sybase, name: <schema.name, lower case>}`"),
    Term("dbp:Function", K.PROGRAM, "lossy", "a SQL function (DB2 SQL PL or Sybase): same key as a procedure"),
    Term("dbp:Trigger", K.PROGRAM, "lossy", "a database trigger, with `attributes.on` {table, events, timing}"),
)

_CONSTRUCTS = (
    # routines
    ("procedure", "CREATE PROCEDURE", "entity:Program"),
    ("function", "CREATE FUNCTION", "entity:Program"),
    ("trigger", "CREATE TRIGGER", "entity:Program"),
    ("parameter", "a routine parameter (IN, OUT, INOUT; @name; a positional $n)", "attribute:params"),
    ("routine-option", "LANGUAGE, SPECIFIC, DETERMINISTIC, SQL DATA access and other routine options", "unmapped"),
    ("result-sets", "DYNAMIC RESULT SETS, or a SELECT that returns rows to the caller", "attribute:result_sets"),
    ("alias", "Netezza `name ALIAS FOR $n`", "behavior:declare"),
    # declarations
    ("declare", "a variable declaration", "behavior:declare"),
    ("declare-cursor", "DECLARE ... CURSOR FOR", "behavior:declare"),
    ("declare-condition", "DECLARE ... CONDITION FOR SQLSTATE", "behavior:declare"),
    ("handler", "DECLARE CONTINUE, EXIT or UNDO HANDLER", "behavior:try"),
    ("exception-block", "EXCEPTION WHEN ... THEN (Netezza)", "behavior:try"),
    ("temp-table", "DECLARE GLOBAL TEMPORARY TABLE, CREATE TEMP TABLE or a #table", "behavior:query"),
    # statements
    ("assign", "SET, :=, SELECT @v = and VALUES ... INTO", "behavior:assign"),
    ("select-into", "SELECT ... INTO variables", "behavior:query"),
    ("if", "IF ... THEN ... ELSEIF ... ELSE ... END IF", "behavior:if"),
    ("case", "a CASE statement", "behavior:switch"),
    ("while", "WHILE", "behavior:loop"),
    ("loop", "LOOP ... END LOOP", "behavior:loop"),
    ("repeat", "REPEAT ... UNTIL", "behavior:loop"),
    ("for", "FOR over a query or a range", "behavior:loop"),
    ("leave", "LEAVE, EXIT, BREAK", "behavior:break"),
    ("iterate", "ITERATE, CONTINUE", "behavior:continue"),
    ("goto", "GOTO and labels", "unmapped"),
    ("return", "RETURN", "behavior:return"),
    ("call", "CALL or EXEC of a procedure", "behavior:call"),
    ("dynamic-sql", "EXECUTE IMMEDIATE, PREPARE/EXECUTE or EXEC of text built at run time", "unmapped"),
    ("signal", "SIGNAL, RESIGNAL, RAISE EXCEPTION, RAISERROR", "behavior:throw"),
    ("print", "PRINT or RAISE NOTICE", "behavior:output"),
    ("transaction", "COMMIT, ROLLBACK, SAVEPOINT, BEGIN TRANSACTION", "behavior:transaction"),
    ("open", "OPEN cursor: runs its query", "behavior:query"),
    ("fetch", "FETCH cursor INTO", "unmapped"),
    ("close", "CLOSE or DEALLOCATE cursor", "unmapped"),
    ("dml", "INSERT, UPDATE, DELETE, MERGE, SELECT or TRUNCATE", "behavior:query"),
    ("ddl", "CREATE, DROP or ALTER in a routine body", "behavior:query"),
    ("get-diagnostics", "GET DIAGNOSTICS", "unmapped"),
    ("session-option", "SET NOCOUNT, SET CHAINED and other session options", "unmapped"),
    ("waitfor", "WAITFOR", "unmapped"),
    ("unparsed", "a statement the reader does not understand", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"dbp:{name}", description, modelled_as) for name, description, modelled_as in _CONSTRUCTS)
