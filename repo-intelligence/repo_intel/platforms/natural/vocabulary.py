"""The Natural pack's vocabulary: platform terms and the constructs it looks for.

Keys (docs/solution-ir-contract.md, section 11.1): a Natural object is
`Program {platform: "natural", name}` with `attributes.object_type`; an inline subroutine
is `Symbol {platform: "natural", program, name}`; a data area is
`DataSchema {kind: "natural-lda" | "natural-pda" | "natural-gda", name}` and its fields
`Field {table: "natural:<data area>", name}`; a map is `Screen {platform: "natural", name}`
with `UIControl {container: <map>, name: <field>}`; an Adabas DDM is
`Table {datastore: "adabas", name}` with `Field {table: "adabas:<DDM>", name}`. Natural
object names are upper case, as Natural keeps them.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "natural"  # Program.platform, Screen.platform and Symbol.platform for Natural objects
MAINFRAME = "mainframe"  # Program.platform of a program CALLed that is not Natural (section 11.1 item 2)
ADABAS = "adabas"  # Table.datastore of an Adabas DDM (section 11.1 item 8)

# Object types by source suffix (NaturalONE and SYSOBJH exports).
OBJECT_TYPES = {
    ".nsp": "program", ".nsn": "subprogram", ".nss": "subroutine", ".nsh": "helproutine", ".nsc": "copycode",
    ".nsm": "map", ".nsl": "local_data_area", ".nsa": "parameter_data_area", ".nsg": "global_data_area", ".nsd": "ddm",
}
DATA_AREA_KINDS = {".nsl": "natural-lda", ".nsa": "natural-pda", ".nsg": "natural-gda"}
USING_KINDS = {"LOCAL": "natural-lda", "PARAMETER": "natural-pda", "GLOBAL": "natural-gda"}

TERMS = (
    Term("nat:Program", K.PROGRAM, "lossy", "a Natural program (.nsp); its mode in attributes"),
    Term("nat:Subprogram", K.PROGRAM, "lossy", "a Natural subprogram (.nsn), run by CALLNAT with its parameter data"),
    Term("nat:Subroutine", K.PROGRAM, "lossy", "an external subroutine (.nss), run by PERFORM: named by its DEFINE SUBROUTINE name, its object name in attributes"),
    Term("nat:Helproutine", K.PROGRAM, "lossy", "a helproutine (.nsh), run for a field's help (HE=)"),
    Term("nat:Copycode", K.PROGRAM, "lossy", "copycode (.nsc): source INCLUDEd into another object when it is compiled"),
    Term("nat:InlineSubroutine", K.SYMBOL, "exact", "a subroutine defined inside an object (DEFINE SUBROUTINE ... END-SUBROUTINE)"),
    Term("nat:LocalDataArea", K.DATA_SCHEMA, "lossy", "a local data area (.nsl) a program names with LOCAL USING"),
    Term("nat:ParameterDataArea", K.DATA_SCHEMA, "lossy", "a parameter data area (.nsa) a subprogram names with PARAMETER USING"),
    Term("nat:GlobalDataArea", K.DATA_SCHEMA, "lossy", "a global data area (.nsg) programs share with GLOBAL USING"),
    Term("nat:DataField", K.FIELD, "lossy", "a field of a data area: level, format, length, occurrences, redefinition and the DDM of a view in attributes"),
    Term("nat:Map", K.SCREEN, "lossy", "a Natural map (.nsm): its fields as controls, at their line and column"),
    Term("nat:MapField", K.UI_CONTROL, "lossy", "a field of a map, named by the variable it shows; line, column, mode, label and help routine in attributes"),
    Term("nat:Ddm", K.TABLE, "lossy", "an Adabas file as its data definition module (.nsd) describes it; periodic groups and multiple-value fields are attributes of its fields"),
    Term("nat:DdmField", K.FIELD, "lossy", "a field of a DDM, by its long name: short name, format, length, descriptor, PE and MU in attributes"),
)

_CONSTRUCTS = (
    # Objects.
    ("program", "entity:Program", "a program (.nsp)"),
    ("subprogram", "entity:Program", "a subprogram (.nsn)"),
    ("subroutine", "entity:Program", "an external subroutine (.nss)"),
    ("helproutine", "entity:Program", "a helproutine (.nsh)"),
    ("copycode", "entity:Program", "copycode (.nsc)"),
    ("map", "entity:Screen", "a map (.nsm)"),
    ("local_data_area", "entity:DataSchema", "a local data area (.nsl)"),
    ("parameter_data_area", "entity:DataSchema", "a parameter data area (.nsa)"),
    ("global_data_area", "entity:DataSchema", "a global data area (.nsg)"),
    ("ddm", "entity:Table", "an Adabas DDM (.nsd)"),
    ("ddm_sql", "unmapped", "a DDM over a SQL table: its table is named by the SQL a program runs, not by the DDM"),
    ("data_area_editor_format", "unmapped", "a data area in the data area editor's column layout, not as DEFINE DATA source"),
    ("define_data", "attribute:define_data", "DEFINE DATA ... END-DEFINE"),
    ("local_using", "relation:DEPENDS_ON", "LOCAL USING a local data area"),
    ("parameter_using", "relation:DEPENDS_ON", "PARAMETER USING a parameter data area"),
    ("global_using", "relation:DEPENDS_ON", "GLOBAL USING a global data area"),
    ("global_block", "unmapped", "GLOBAL USING ... WITH a block of the global data area"),
    ("data_field", "behavior:declare", "a data definition: a field or group in DEFINE DATA, or in a data area as a Field"),
    ("view_definition", "attribute:views", "a view of a DDM (VIEW OF): the DDM a program reads and writes through it"),
    ("redefine", "attribute:redefines", "REDEFINE of a field or group"),
    ("init_value", "attribute:initialized", "INIT or CONST: that a field has an initial value (the value is not kept)"),
    ("dynamic_variable", "attribute:dynamic", "a DYNAMIC field, or an alphanumeric or binary field with no length"),
    ("define_subroutine", "entity:Symbol", "DEFINE SUBROUTINE: an inline subroutine, or the body of an external one"),
    ("define_window", "unmapped", "DEFINE WINDOW"),
    ("define_printer", "unmapped", "DEFINE PRINTER"),
    ("define_prototype", "unmapped", "DEFINE PROTOTYPE ... END-PROTOTYPE"),
    ("define_function", "unmapped", "DEFINE FUNCTION or DEFINE CLASS in a source exported with another object type"),
    ("end", "behavior:exit", "the END statement that closes an object"),
    # Assignments and computation.
    ("assign", "behavior:assign", "ASSIGN, COMPUTE or `:=`"),
    ("sensitive_value", "behavior:assign", "a literal assigned to a field named for a password, key, user or host: the assignment is kept, the value is not"),
    ("move", "behavior:assign", "MOVE (also EDITED, ROUNDED, LEFT/RIGHT JUSTIFIED, ALL, SUBSTRING)"),
    ("move_by_name", "behavior:platform", "MOVE BY NAME or BY POSITION between groups or views"),
    ("arithmetic", "behavior:assign", "ADD, SUBTRACT, MULTIPLY or DIVIDE, with GIVING and REMAINDER"),
    ("reset", "behavior:assign", "RESET, RESET INITIAL"),
    ("compress", "behavior:assign", "COMPRESS ... INTO"),
    ("examine", "behavior:platform", "EXAMINE: search, replace, count in a field"),
    ("separate", "behavior:platform", "SEPARATE a field into parts"),
    ("array_resize", "behavior:platform", "EXPAND, REDUCE or RESIZE of an array or dynamic field"),
    # Control.
    ("if", "behavior:if", "IF ... ELSE ... END-IF (in reporting mode, with DO ... DOEND)"),
    ("do_doend", "behavior:if", "DO ... DOEND: a reporting-mode statement group"),
    ("decide_on", "behavior:switch", "DECIDE ON FIRST or EVERY VALUE"),
    ("decide_for", "behavior:switch", "DECIDE FOR FIRST or EVERY CONDITION"),
    ("value_range", "behavior:platform", "a range of values `low:high` a DECIDE ON ... VALUE clause matches"),
    ("condition", "behavior:platform", "a test with no neutral form: MASK, SCAN, IS (format), MODIFIED, SPECIFIED, BREAK"),
    ("for", "behavior:loop", "FOR ... END-FOR"),
    ("repeat", "behavior:loop", "REPEAT with UNTIL or WHILE at the top or the bottom"),
    ("accept_reject", "behavior:if", "ACCEPT or REJECT IF in a processing loop"),
    ("escape", "behavior:break", "ESCAPE TOP, BOTTOM, ROUTINE or MODULE, and RETURN"),
    ("stop", "behavior:exit", "STOP or TERMINATE"),
    ("at_break", "behavior:platform", "AT BREAK and BEFORE BREAK PROCESSING"),
    ("break_processing", "behavior:platform", "PERFORM BREAK PROCESSING"),
    ("at_start_of_data", "behavior:platform", "AT START OF DATA"),
    ("at_end_of_data", "behavior:platform", "AT END OF DATA"),
    ("at_page", "behavior:platform", "AT TOP OF PAGE or AT END OF PAGE"),
    ("on_error", "behavior:platform", "ON ERROR ... END-ERROR"),
    ("if_no_records", "behavior:platform", "IF NO RECORDS FOUND in a FIND loop"),
    ("sort", "behavior:platform", "END-ALL and SORT ... END-SORT"),
    ("limit", "behavior:platform", "LIMIT on the records a loop reads"),
    ("stack", "behavior:platform", "STACK of data or a command"),
    ("release", "behavior:platform", "RELEASE of the stack, variables or sets"),
    ("retry", "behavior:platform", "RETRY after a record hold"),
    ("passw", "behavior:platform", "PASSW to Natural Security (the password is never kept)"),
    ("process", "behavior:platform", "PROCESS COMMAND or PROCESS PAGE"),
    ("request", "behavior:platform", "REQUEST DOCUMENT"),
    ("close", "behavior:platform", "CLOSE WORK FILE, CLOSE PRINTER, CLOSE PC FILE"),
    ("unrecognised_statement", "behavior:platform", "a statement this parser does not read: kept as a platform node"),
    ("unrecognised_expression", "behavior:platform", "an expression this parser does not read: kept as a platform node"),
    ("dynamic_target", "behavior:platform", "a call, map or program named at run time"),
    # Database access.
    ("read", "relation:READS_FROM", "READ (physical, logical by descriptor, by ISN) of a view"),
    ("find", "relation:READS_FROM", "FIND with search criteria"),
    ("find_number", "relation:READS_FROM", "FIND NUMBER"),
    ("find_first_unique", "relation:READS_FROM", "FIND FIRST or FIND UNIQUE"),
    ("histogram", "relation:READS_FROM", "HISTOGRAM of a descriptor"),
    ("get", "relation:READS_FROM", "GET of a record by ISN, GET SAME"),
    ("store", "relation:WRITES_TO", "STORE"),
    ("update", "relation:WRITES_TO", "UPDATE of the record a loop or GET holds"),
    ("delete", "relation:WRITES_TO", "DELETE of the record a loop or GET holds"),
    ("end_transaction", "behavior:transaction", "END TRANSACTION"),
    ("backout", "behavior:transaction", "BACKOUT TRANSACTION"),
    ("transaction_data", "unmapped", "GET TRANSACTION DATA"),
    ("work_file", "behavior:output", "READ WORK FILE, WRITE WORK FILE, DEFINE WORK FILE (by number; the dataset is bound outside the program)"),
    ("pc_file", "unmapped", "DOWNLOAD or UPLOAD PC FILE"),
    ("sql_select", "behavior:query", "Natural SQL SELECT ... END-SELECT and SELECT SINGLE"),
    ("sql_insert", "behavior:query", "Natural SQL INSERT"),
    ("sql_update", "behavior:query", "Natural SQL UPDATE ... SET"),
    ("sql_delete", "behavior:query", "Natural SQL DELETE FROM"),
    ("sql_commit", "behavior:transaction", "Natural SQL COMMIT or ROLLBACK"),
    ("sql_other", "unmapped", "PROCESS SQL, CALLDBPROC, READ RESULT SET"),
    # Calls.
    ("callnat", "relation:CALLS", "CALLNAT of a subprogram"),
    ("fetch", "relation:CALLS", "FETCH of a program (control does not come back)"),
    ("fetch_return", "relation:CALLS", "FETCH RETURN of a program"),
    ("run", "relation:CALLS", "RUN of a program"),
    ("perform", "relation:CALLS", "PERFORM of an inline or external subroutine"),
    ("call", "relation:CALLS", "CALL of a program that is not Natural"),
    ("include", "relation:DEPENDS_ON", "INCLUDE of copycode"),
    # Screens and reports.
    ("input_map", "relation:RENDERS", "INPUT USING MAP"),
    ("write_using_map", "relation:RENDERS", "WRITE or DISPLAY USING MAP or FORM"),
    ("input_inline", "behavior:platform", "INPUT of fields and texts in the program, with no map"),
    ("reinput", "behavior:platform", "REINPUT with a message"),
    ("write", "behavior:output", "WRITE, DISPLAY or PRINT to a report"),
    ("format", "unmapped", "FORMAT of a report or screen"),
    ("newpage", "unmapped", "NEWPAGE, EJECT, SKIP, SUSPEND IDENTICAL SUPPRESS"),
    ("set", "unmapped", "SET KEY, SET CONTROL, SET WINDOW, SET GLOBALS, SET TIME"),
    ("options", "unmapped", "OPTIONS"),
    ("settime", "unmapped", "SETTIME"),
    ("obtain", "behavior:platform", "OBTAIN of fields in reporting mode"),
    ("inline_definition", "unmapped", "a reporting-mode field defined where it is first used, `#TOTAL (P9.2)`: its format is not kept"),
    # Maps.
    ("map_field", "entity:UIControl", "a field of a map"),
    ("map_text", "attribute:label", "a text constant on a map: the label of the field after it on its line"),
    ("map_rule", "unmapped", "a processing rule or other statement in a map: not read"),
    # DDMs.
    ("ddm_field", "entity:Field", "a field of a DDM"),
    ("ddm_group", "attribute:group", "a group of a DDM (G)"),
    ("ddm_mu", "attribute:mu", "a multiple-value field (M)"),
    ("ddm_pe", "attribute:pe", "a periodic group (P)"),
    ("ddm_descriptor", "attribute:descriptor", "a descriptor, unique, super, phonetic or hyper descriptor"),
    ("ddm_unread", "unmapped", "a line of a DDM's field table this reader does not read"),
)
CONSTRUCTS = tuple(sorted((Construct(f"nat:{name}", text, modelled) for name, modelled, text in _CONSTRUCTS), key=lambda c: c.id))
