"""The COBOL pack's vocabulary: platform terms and the constructs it looks for.

Keys (docs/solution-ir-contract.md, section 11.1):

- a program is `Program {platform: "mainframe", name: <PROGRAM-ID, upper case>}`, the key
  every CALL, LINK, XCTL and job step uses, with `attributes.language` `cobol`;
- a section or paragraph is `Symbol {platform: "mainframe", program: <PROGRAM-ID>, name}`,
  as the PL/SQL pack keys a package's procedures; a paragraph name that repeats in two
  sections is `<section>.<paragraph>`;
- a copybook is `DataSchema {kind: "copybook", name: <member>}`; its data items are
  `Field {table: "cobol:<member>", name}` (a name repeated in the copybook is
  `<parent>.<name>`; FILLER is not declared), and the 88-levels under a field are one
  `CodeList {platform: "cobol", name: "<member>.<field>"}` the field BINDS_TO.

Names are upper case: COBOL is not case-sensitive.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "mainframe"  # Program.platform of a load module (contract 11.1 item 2)
LANGUAGE = "cobol"

TERMS = (
    Term("cbl:Program", K.PROGRAM, "lossy", "a COBOL program (PROGRAM-ID): the load module key {platform: mainframe, name}; files, maps, DL/I and MQ use in attributes"),
    Term("cbl:Section", K.SYMBOL, "exact", "a section of the procedure division"),
    Term("cbl:Paragraph", K.SYMBOL, "exact", "a paragraph of the procedure division; its section in attributes"),
    Term("cbl:Copybook", K.DATA_SCHEMA, "lossy", "a copybook member: its record layouts are Fields, its 88-levels CodeLists"),
    Term("cbl:Field", K.FIELD, "lossy", "a data item of a copybook: level, PIC, USAGE, OCCURS, REDEFINES, offset and length in attributes"),
    Term("cbl:ConditionNames", K.CODE_LIST, "lossy", "the 88-level condition names of one field: their values with the names as labels"),
)

_CONSTRUCTS = (
    # Structure.
    ("nested_program", "a program nested in another (END PROGRAM)", "entity:Program"),
    ("section", "a section of the procedure division", "entity:Symbol"),
    ("paragraph", "a paragraph of the procedure division", "entity:Symbol"),
    ("declaratives", "DECLARATIVES and USE procedures (error and label handling)", "unmapped"),
    ("copy", "COPY of a copybook: DEPENDS_ON the copybook, which is not expanded here", "relation:DEPENDS_ON"),
    ("copy_replacing", "COPY ... REPLACING: the operands are recorded on the program", "attribute:copy_replacing"),
    ("procedure_copy", "COPY in the procedure division: the copied statements are another file's", "unmapped"),
    ("replace", "REPLACE statement", "unmapped"),
    ("replacing_tag", "a data name with a REPLACING tag (:TAG:-NAME): declared as written", "entity:Field"),
    ("debug_line", "a debugging line (D in the indicator column): read as a comment", "unmapped"),
    ("compiler_directive", ">> directive, CBL or PROCESS card", "unmapped"),
    # Files.
    ("select", "SELECT ... ASSIGN: a dd_file observation and attributes.files", "attribute:files"),
    ("assign_dynamic", "ASSIGN USING or DYNAMIC, or an assignment that is not a DD name", "unmapped"),
    ("fd", "FD file description", "attribute:files"),
    ("sd", "SD sort file description", "attribute:files"),
    ("open", "OPEN: the modes become the files' dd_file mode", "behavior:platform"),
    ("close", "CLOSE", "behavior:platform"),
    ("read", "READ", "behavior:platform"),
    ("write", "WRITE", "behavior:output"),
    ("rewrite", "REWRITE", "behavior:output"),
    ("delete", "DELETE of a record", "behavior:platform"),
    ("start", "START (position an indexed or relative file)", "behavior:platform"),
    ("io_condition", "AT END, INVALID KEY and END-OF-PAGE phrases", "behavior:if"),
    ("sort", "SORT: keys, USING and GIVING files, input and output procedures", "transform:sort"),
    ("merge", "MERGE", "transform:union"),
    ("release", "RELEASE (to a sort)", "behavior:platform"),
    ("sort_return", "RETURN (from a sort)", "behavior:platform"),
    # Data.
    ("copybook_field", "a named data item of a copybook", "entity:Field"),
    ("condition_name", "an 88-level condition name", "entity:CodeList"),
    ("renames", "a 66-level RENAMES item", "unmapped"),
    ("occurs_depending", "OCCURS ... DEPENDING ON: offsets after it are not fixed", "attribute:occurs"),
    ("sync", "SYNCHRONIZED: slack bytes are not computed", "attribute:offset"),
    ("report_section", "REPORT SECTION and report writer descriptions", "unmapped"),
    ("screen_section", "SCREEN SECTION", "unmapped"),
    ("communication_section", "COMMUNICATION SECTION", "unmapped"),
    ("local_storage", "LOCAL-STORAGE SECTION", "attribute:scope"),
    # Statements.
    ("move", "MOVE", "behavior:assign"),
    ("move_corresponding", "MOVE CORRESPONDING", "behavior:platform"),
    ("compute", "COMPUTE", "behavior:assign"),
    ("add", "ADD", "behavior:assign"),
    ("subtract", "SUBTRACT", "behavior:assign"),
    ("multiply", "MULTIPLY", "behavior:assign"),
    ("divide", "DIVIDE", "behavior:assign"),
    ("arithmetic_corresponding", "ADD or SUBTRACT CORRESPONDING", "behavior:platform"),
    ("initialize", "INITIALIZE", "behavior:platform"),
    ("set", "SET (indexes, condition names, pointers)", "behavior:assign"),
    ("set_condition", "SET condition-name TO TRUE when the 88-level is in another file", "behavior:platform"),
    ("set_address", "SET ADDRESS OF", "unmapped"),
    ("accept", "ACCEPT", "behavior:assign"),
    ("display", "DISPLAY", "behavior:output"),
    ("string", "STRING", "behavior:platform"),
    ("unstring", "UNSTRING", "behavior:platform"),
    ("inspect", "INSPECT", "behavior:platform"),
    ("if", "IF ... ELSE", "behavior:if"),
    ("evaluate", "EVALUATE", "behavior:switch"),
    ("evaluate_also", "EVALUATE with ALSO subjects", "behavior:switch"),
    ("next_sentence", "NEXT SENTENCE", "behavior:platform"),
    ("continue", "CONTINUE", "behavior:platform"),
    ("perform", "PERFORM of a paragraph or section: CALLS the Symbol", "relation:CALLS"),
    ("perform_thru", "PERFORM ... THRU: CALLS every procedure in the range", "relation:CALLS"),
    ("perform_inline", "inline PERFORM ... END-PERFORM", "behavior:loop"),
    ("perform_until", "PERFORM ... UNTIL", "behavior:loop"),
    ("perform_varying", "PERFORM ... VARYING", "behavior:loop"),
    ("perform_times", "PERFORM ... TIMES", "behavior:loop"),
    ("go_to", "GO TO a paragraph or section: CALLS the Symbol", "relation:CALLS"),
    ("go_to_depending", "GO TO ... DEPENDING ON", "relation:CALLS"),
    ("alter", "ALTER, and the GO TO it changes", "unmapped"),
    ("search", "SEARCH and SEARCH ALL", "behavior:platform"),
    ("exit", "EXIT, EXIT PARAGRAPH, EXIT SECTION, EXIT PERFORM", "behavior:return"),
    ("exit_program", "EXIT PROGRAM", "behavior:return"),
    ("goback", "GOBACK", "behavior:return"),
    ("stop_run", "STOP RUN", "behavior:exit"),
    ("stop_literal", "STOP literal (an operator message)", "unmapped"),
    ("call", "CALL of a literal program name", "relation:CALLS"),
    ("call_dynamic", "CALL through a data item: CALLS when the item has exactly one literal value in the file, else dynamic", "relation:CALLS"),
    ("cancel", "CANCEL", "behavior:platform"),
    ("entry", "ENTRY (an alternate entry point)", "unmapped"),
    ("invoke", "INVOKE (object-oriented COBOL)", "unmapped"),
    ("xml", "XML GENERATE or XML PARSE", "unmapped"),
    ("json", "JSON GENERATE or JSON PARSE", "unmapped"),
    ("storage", "ALLOCATE or FREE", "unmapped"),
    ("report_writer", "GENERATE, INITIATE or TERMINATE of the report writer", "unmapped"),
    ("trace", "READY TRACE or RESET TRACE", "unmapped"),
    ("unrecognised_statement", "a statement this reader does not follow: a platform node", "unmapped"),
    # Interfaces called as programs.
    ("dli_call", "CALL 'CBLTDLI' (or AIBTDLI, CEETDLI): segment names of literal SSAs in attributes.dli_segments", "attribute:dli_segments"),
    ("exec_dli", "EXEC DLI: segment names in attributes.dli_segments", "attribute:dli_segments"),
    ("mq_call", "an MQI call (MQOPEN, MQPUT, MQGET ...): calls in attributes.mq_calls, literal queue names in attributes.mq_queues", "attribute:mq_calls"),
    # Embedded SQL.
    ("sql_select", "SELECT INTO, VALUES INTO and WITH: a query node", "behavior:query"),
    ("sql_dml", "INSERT, UPDATE, DELETE, MERGE, TRUNCATE, LOCK TABLE, SET: a query node", "behavior:query"),
    ("sql_call", "CALL of a stored procedure: a query node", "behavior:query"),
    ("sql_declare_cursor", "DECLARE CURSOR: its query runs where the cursor is opened", "behavior:query"),
    ("sql_declare_table", "DECLARE TABLE (a DCLGEN declaration)", "unmapped"),
    ("sql_open", "OPEN of a cursor: the cursor's query", "behavior:query"),
    ("sql_fetch", "FETCH ... INTO", "behavior:platform"),
    ("sql_close", "CLOSE of a cursor", "behavior:platform"),
    ("sql_transaction", "COMMIT or ROLLBACK", "behavior:transaction"),
    ("sql_whenever", "WHENEVER: a GO TO target CALLS the procedure", "relation:CALLS"),
    ("sql_include", "EXEC SQL INCLUDE (SQLCA, SQLDA, a DCLGEN member)", "attribute:sql_includes"),
    ("sql_dynamic", "PREPARE, EXECUTE, EXECUTE IMMEDIATE, DESCRIBE: text built at run time", "unmapped"),
    ("sql_other", "another embedded SQL statement (CONNECT, SET CURRENT, ...)", "unmapped"),
    # EXEC CICS.
    ("cics_link", "LINK PROGRAM: CALLS", "relation:CALLS"),
    ("cics_xctl", "XCTL PROGRAM: CALLS", "relation:CALLS"),
    ("cics_return", "RETURN", "behavior:return"),
    ("cics_return_transid", "RETURN TRANSID: CONSUMES the transaction Endpoint", "relation:CONSUMES"),
    ("cics_start", "START TRANSID: CONSUMES the transaction Endpoint", "relation:CONSUMES"),
    ("cics_send_map", "SEND MAP: RENDERS the BMS Screen", "relation:RENDERS"),
    ("cics_receive_map", "RECEIVE MAP: the map in attributes.maps", "attribute:maps"),
    ("cics_terminal", "SEND, RECEIVE or CONVERSE without a map (SEND TEXT, SEND CONTROL)", "unmapped"),
    ("cics_file", "READ, WRITE, REWRITE, DELETE, STARTBR, READNEXT, READPREV, ENDBR, RESETBR and the other file commands", "relation:READS_FROM"),
    ("cics_syncpoint", "SYNCPOINT [ROLLBACK]", "behavior:transaction"),
    ("cics_abend", "ABEND", "behavior:throw"),
    ("cics_handle", "HANDLE CONDITION, HANDLE AID, HANDLE ABEND, IGNORE CONDITION: label targets CALL the procedure", "relation:CALLS"),
    ("cics_queue", "WRITEQ, READQ, DELETEQ of a TS or TD queue", "unmapped"),
    ("cics_retrieve", "RETRIEVE (data passed by START)", "unmapped"),
    ("cics_storage", "GETMAIN, FREEMAIN", "unmapped"),
    ("cics_time", "ASKTIME, FORMATTIME", "unmapped"),
    ("cics_enq", "ENQ, DEQ", "unmapped"),
    ("cics_container", "PUT, GET CONTAINER (channels)", "unmapped"),
    ("cics_web", "WEB, DOCUMENT, INVOKE SERVICE", "unmapped"),
    ("cics_inquire", "ASSIGN, ADDRESS, INQUIRE", "unmapped"),
    ("cics_other", "another CICS command", "unmapped"),
    ("exec_other", "EXEC of another kind (SQLIMS, HTML ...)", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"cbl:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
