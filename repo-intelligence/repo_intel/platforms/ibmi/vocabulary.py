"""The IBM i pack's vocabulary: platform terms and the constructs it looks for.

Keys follow docs/solution-ir-contract.md, section 11.1:

- an RPG or CL source member is `Program {platform: "mainframe", name: <member, upper case>}`
  (item 2), the key every CALL, CALLP and SBMJOB uses, with `attributes.language`;
- a subroutine or procedure is `Symbol {platform: "mainframe", program: <member>, name}`,
  following the PL/SQL pack (a Symbol's platform is its Program's); a local subroutine of a
  procedure is named `<procedure>.<subroutine>`; an exported procedure's export name is
  also declared as `Symbol {platform: "mainframe", program: "*EXPORT", name: <export name>}`
  ALIAS_OF the procedure, so a bound call from another member, which cannot know the module
  that exports the name, links when the repository holds that module (the way 11.1 item 10
  gives a JCL member an alias);
- a physical file is `Table {datastore: "default", name}` and its fields
  `Field {table, name}` (item 9); names are lower-cased, as the shared SQL scanner names a
  table embedded SQL reads, so DDS, F-specs and embedded SQL name one table;
- a logical file is `DataSchema {kind: "ibmi-lf", name}` (lower case, item 9);
- a display file record format is `Screen {platform: "ibmi", name: "<FILE>.<FORMAT>"}`
  with a `Form {screen, name: <FORMAT>}` and `UIControl {container: "<FILE>.<FORMAT>", name: <FIELD>}`
  (item 7, upper case as IBM i names objects);
- a printer file is `Report {platform: "ibmi", name: <FILE>}` (item 9);
- a DDS VALUES keyword is `CodeList {platform: "ibmi", name: "<layout>.<field>"}` (item 4),
  the layout being the physical file (lower case) or the display record (`<FILE>.<FORMAT>`).
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "mainframe"  # Program.platform for a *PGM or *MODULE (contract 11.1, item 2)
UI_PLATFORM = "ibmi"  # Screen, Report and CodeList platform
LANGUAGE = "ibmi"  # behaviour and transform language: the pack id
EXPORT_PROGRAM = "*EXPORT"  # the `program` of an export name's Symbol
LF_KIND = "ibmi-lf"

TERMS = (
    Term("ibmi:RpgProgram", K.PROGRAM, "lossy", "an RPG III or RPG IV source member (fixed, free or mixed form, with or without embedded SQL), as the program or module it compiles to; named by the member"),
    Term("ibmi:ClProgram", K.PROGRAM, "lossy", "a CL source member (OPM .clp or ILE .clle), as the program it compiles to; named by the member"),
    Term("ibmi:Subroutine", K.SYMBOL, "exact", "an RPG subroutine (BEGSR ... ENDSR) or a CL subroutine (SUBR ... ENDSUBR)"),
    Term("ibmi:Procedure", K.SYMBOL, "exact", "an RPG procedure (DCL-PROC ... END-PROC, or P-specs B and E)"),
    Term("ibmi:ExportedProcedure", K.SYMBOL, "lossy", "the export name of an EXPORTed procedure, under program '*EXPORT', ALIAS_OF the procedure, so bound calls from other members link"),
    Term("ibmi:PhysicalFile", K.TABLE, "lossy", "a DDS physical file: the table SQL names by the same name; record format, keys and UNIQUE in attributes"),
    Term("ibmi:Field", K.FIELD, "lossy", "a field of a physical file: DDS type, length, decimals, byte offset and keywords in attributes"),
    Term("ibmi:LogicalFile", K.DATA_SCHEMA, "lossy", "a DDS logical file: the physical files it is based on, its keys, select and omit tests and joins in attributes"),
    Term("ibmi:DisplayRecord", K.SCREEN, "lossy", "a record format of a DDS display file; subfile, window, overlay and command keys in attributes"),
    Term("ibmi:DisplayForm", K.FORM, "lossy", "the input a display record format returns to the program that shows it"),
    Term("ibmi:DisplayField", K.UI_CONTROL, "lossy", "a named field of a display record format: row, column, usage, length and type in attributes"),
    Term("ibmi:PrinterFile", K.REPORT, "lossy", "a DDS printer file; its record formats and their fields in attributes"),
    Term("ibmi:FieldValues", K.CODE_LIST, "exact", "the values a DDS VALUES keyword allows for a field"),
)

_CONSTRUCTS = (
    # RPG: source forms and specifications.
    ("rpg_fixed_form", "fixed-form specifications read by column (RPG III, RPG IV)", "attribute:form"),
    ("rpg_free_form", "free-form calculations (/FREE ... /END-FREE, or columns 8 to 80)", "attribute:form"),
    ("rpg_fully_free", "a fully free-form member (**FREE)", "attribute:form"),
    ("control_options", "H-spec or CTL-OPT: activation group, binding directories, MAIN, NOMAIN", "attribute:options"),
    ("file_declaration", "F-spec or DCL-F of a database file: reads and writes by its usage", "relation:READS_FROM"),
    ("workstn_file", "a WORKSTN file: the record formats a program shows", "relation:RENDERS"),
    ("printer_file", "a PRINTER file: the report a program writes", "relation:PRODUCES"),
    ("other_device", "a SEQ or SPECIAL file", "unmapped"),
    ("program_described_file", "a file described in the program (format F) with I and O specs", "attribute:files"),
    ("extfile_dynamic", "EXTFILE or EXTMBR naming the file at run time from a variable", "unmapped"),
    ("file_keyword", "file keywords that change which record formats or members are used (RENAME, PREFIX, INCLUDE, IGNORE, EXTMBR)", "attribute:files"),
    ("standalone", "a standalone field (DCL-S, D-spec S)", "behavior:declare"),
    ("named_constant", "a named constant (DCL-C, D-spec C)", "behavior:declare"),
    ("data_structure", "a data structure (DCL-DS, D-spec DS)", "behavior:declare"),
    ("external_ds", "a data structure laid out from a file's record format (EXTNAME, LIKEREC, E in column 22)", "attribute:layouts"),
    ("prototype", "a prototype (DCL-PR, D-spec PR): EXTPGM or EXTPROC", "relation:CALLS"),
    ("procedure_interface", "a procedure or program interface (DCL-PI, D-spec PI)", "behavior:params"),
    ("entry_plist", "*ENTRY PLIST: the program's parameters in fixed form", "behavior:params"),
    ("subroutine", "a subroutine (BEGSR ... ENDSR)", "entity:Symbol"),
    ("procedure", "a procedure (DCL-PROC ... END-PROC, P-specs)", "entity:Symbol"),
    ("exported_procedure", "a procedure with EXPORT", "entity:Symbol"),
    ("input_spec", "I-specs: program-described input or renamed externally described fields", "unmapped"),
    ("output_spec", "O-specs: program-described output", "unmapped"),
    ("rpg3_array_spec", "RPG III E-specs (arrays and tables) and L-specs (line counters)", "unmapped"),
    ("copy_directive", "/COPY or /INCLUDE of another source member: named in attributes, not read", "attribute:copies"),
    ("conditional_directive", "/DEFINE, /UNDEFINE, /IF, /ELSEIF, /ELSE, /ENDIF, /EOF", "unmapped"),
    ("compile_time_data", "compile-time array or table data after ** or **CTDATA", "unmapped"),
    # RPG: operations.
    ("eval", "EVAL, EVALR and assignment (x = y, x += y)", "behavior:assign"),
    ("eval_corr", "EVAL-CORR: assignment of corresponding subfields", "behavior:platform"),
    ("fixed_arithmetic", "Z-ADD, Z-SUB, ADD, SUB, MULT, DIV", "behavior:assign"),
    ("move", "MOVE, MOVEL, MOVEA", "behavior:platform"),
    ("if", "IF, ELSEIF, ELSE, ENDIF", "behavior:if"),
    ("fixed_compare", "IFxx, DOWxx, DOUxx, WHENxx, ANDxx, ORxx, CASxx compare operations", "behavior:if"),
    ("select", "SELECT, WHEN, OTHER, ENDSL", "behavior:if"),
    ("loop_dow", "DOW ... ENDDO", "behavior:loop"),
    ("loop_dou", "DOU ... ENDDO", "behavior:loop"),
    ("loop_for", "FOR ... ENDFOR", "behavior:loop"),
    ("loop_do", "fixed-form DO ... ENDDO counted loop", "behavior:loop"),
    ("leave_iter", "LEAVE, ITER", "behavior:break"),
    ("return", "RETURN, LEAVESR", "behavior:return"),
    ("goto", "GOTO, TAG, CABxx", "behavior:platform"),
    ("exsr", "EXSR: a call of a subroutine", "relation:CALLS"),
    ("callp", "CALLP or a prototyped call in an expression", "relation:CALLS"),
    ("call", "fixed-form CALL of a program and its PARM lines", "relation:CALLS"),
    ("callb", "fixed-form CALLB of a bound procedure", "relation:CALLS"),
    ("call_dynamic", "a program or procedure called through a variable: kept dynamic", "unmapped"),
    ("call_unresolved", "a prototyped call whose prototype is not in this member (it comes from a /COPY member)", "unmapped"),
    ("monitor", "MONITOR, ON-ERROR, ENDMON", "behavior:try"),
    ("indicator", "indicators (*INxx, *INLR) set or tested, and SETON, SETOFF", "behavior:assign"),
    ("conditioning_indicators", "fixed-form conditioning indicators on a calculation", "behavior:if"),
    ("resulting_indicators", "fixed-form resulting indicators (found, error, end of file)", "behavior:platform"),
    ("chain", "CHAIN: read a record by key", "behavior:platform"),
    ("read", "READ, READE, READP, READPE, READC", "behavior:platform"),
    ("position", "SETLL, SETGT", "behavior:platform"),
    ("write", "WRITE of a record format", "behavior:platform"),
    ("update", "UPDATE of a record format", "behavior:platform"),
    ("delete", "DELETE of a record", "behavior:platform"),
    ("exfmt", "EXFMT: show a display record format and read the reply", "relation:RENDERS"),
    ("file_control", "OPEN, CLOSE, UNLOCK, FEOD, ACQ, REL, POST, NEXT", "behavior:platform"),
    ("except", "EXCEPT: program-described output", "behavior:output"),
    ("commit", "COMMIT, ROLBK", "behavior:transaction"),
    ("dsply", "DSPLY", "behavior:output"),
    ("klist", "KLIST and KFLD: a composite key", "behavior:platform"),
    ("fixed_string_op", "CAT, SUBST, SCAN, CHECK, CHECKR, XLATE, LOOKUP and other fixed-form string and array operations", "behavior:platform"),
    ("data_area", "IN, OUT and UNLOCK of a data area, DTAARA", "unmapped"),
    ("other_opcode", "another operation (CLEAR, RESET, SORTA, TIME, TEST, DEALLOC, DUMP, ...)", "behavior:platform"),
    ("unrecognised_statement", "a statement this reader does not read: kept as a platform node", "behavior:platform"),
    ("unrecognised_expression", "an expression this reader does not read: kept as a platform node", "behavior:platform"),
    # Embedded SQL.
    ("exec_sql_select_into", "EXEC SQL SELECT ... INTO and VALUES ... INTO", "behavior:query"),
    ("exec_sql_dml", "EXEC SQL INSERT, UPDATE, DELETE, MERGE", "behavior:query"),
    ("exec_sql_cursor", "EXEC SQL DECLARE CURSOR, OPEN, FETCH, CLOSE", "behavior:query"),
    ("exec_sql_dynamic", "EXEC SQL PREPARE, EXECUTE, EXECUTE IMMEDIATE: the text is built at run time", "behavior:query"),
    ("exec_sql_call", "EXEC SQL CALL of a stored procedure", "behavior:query"),
    ("exec_sql_transaction", "EXEC SQL COMMIT, ROLLBACK", "behavior:transaction"),
    ("exec_sql_other", "EXEC SQL SET OPTION, WHENEVER, INCLUDE, GET DIAGNOSTICS, DDL and the like", "unmapped"),
    # CL.
    ("cl_pgm", "PGM ... ENDPGM and its parameters", "entity:Program"),
    ("cl_dcl", "DCL of a variable", "behavior:declare"),
    ("cl_dclf", "DCLF of a database or display file", "relation:READS_FROM"),
    ("cl_call", "CALL PGM()", "relation:CALLS"),
    ("cl_callprc", "CALLPRC of a bound procedure", "relation:CALLS"),
    ("cl_tfrctl", "TFRCTL PGM()", "relation:CALLS"),
    ("cl_sbmjob", "SBMJOB CMD(CALL ...): a program submitted to batch", "relation:CALLS"),
    ("cl_sbmjob_other", "SBMJOB of a command other than CALL", "unmapped"),
    ("cl_call_dynamic", "CALL, TFRCTL or SBMJOB of a program named by a variable: kept dynamic", "unmapped"),
    ("cl_ovrdbf", "OVRDBF FILE() TOFILE(): recorded, never applied to a guessed call", "attribute:overrides"),
    ("cl_override_other", "OVRPRTF, OVRDSPF, DLTOVR", "attribute:overrides"),
    ("cl_rtvdtaara", "RTVDTAARA: a value read from a data area", "attribute:data_areas"),
    ("cl_chgdtaara", "CHGDTAARA: a data area changed; the value is never copied", "attribute:data_areas"),
    ("cl_monmsg", "MONMSG: a message monitor", "unmapped"),
    ("cl_chgvar", "CHGVAR", "behavior:assign"),
    ("cl_if", "IF ... THEN, ELSE", "behavior:if"),
    ("cl_select", "SELECT, WHEN, OTHERWISE, ENDSELECT", "behavior:if"),
    ("cl_loop", "DOWHILE, DOUNTIL, DOFOR ... ENDDO", "behavior:loop"),
    ("cl_leave_iterate", "LEAVE, ITERATE", "behavior:break"),
    ("cl_goto", "GOTO and labels", "behavior:platform"),
    ("cl_subroutine", "SUBR ... ENDSUBR", "entity:Symbol"),
    ("cl_callsubr", "CALLSUBR", "relation:CALLS"),
    ("cl_return", "RETURN, RTNSUBR", "behavior:return"),
    ("cl_file_io", "RCVF, SNDF, SNDRCVF", "relation:RENDERS"),
    ("cl_cpyf", "CPYF FROMFILE() TOFILE()", "transform:publish"),
    ("cl_clrpfm", "CLRPFM: clear a physical file member", "relation:WRITES_TO"),
    ("cl_opnqryf", "OPNQRYF: an open query over a file", "relation:READS_FROM"),
    ("cl_runsql", "RUNSQL with a literal statement", "behavior:query"),
    ("cl_runsqlstm", "RUNSQLSTM of a source member", "unmapped"),
    ("cl_message", "SNDPGMMSG, SNDMSG, SNDUSRMSG, RCVMSG, RTVMSG", "behavior:output"),
    ("cl_commitment", "STRCMTCTL, ENDCMTCTL, COMMIT, ROLLBACK", "behavior:transaction"),
    ("cl_other_command", "another CL command (CHKOBJ, ALCOBJ, RTVJOBA, CRTDUPOBJ, ...)", "behavior:platform"),
    # DDS.
    ("dds_physical_file", "a physical file (PF)", "entity:Table"),
    ("dds_logical_file", "a logical file (LF)", "entity:DataSchema"),
    ("dds_display_record", "a display file record format", "entity:Screen"),
    ("dds_printer_record", "a printer file record format", "attribute:record_formats"),
    ("dds_field", "a field of a physical file", "entity:Field"),
    ("dds_lf_field", "a field a logical file selects", "relation:CONTAINS"),
    ("dds_display_field", "a named field of a display record format", "entity:UIControl"),
    ("dds_printer_field", "a field of a printer record format", "attribute:record_formats"),
    ("dds_key", "a key field (K)", "attribute:keys"),
    ("dds_select_omit", "a select or omit test (S, O) of a logical file", "attribute:select_omit"),
    ("dds_join", "a join logical file (JFILE, J, JOIN, JFLD, JREF)", "attribute:join"),
    ("dds_values", "VALUES: the values a field allows", "entity:CodeList"),
    ("dds_validation", "RANGE, CMP, COMP, CHECK", "attribute:validation"),
    ("dds_reference", "a reference to another file's field (REF, REFFLD, R in column 29)", "relation:BINDS_TO"),
    ("dds_constant", "a constant text on a display or printer record", "attribute:constants"),
    ("dds_unnamed_field", "an unnamed display or printer field (DATE, TIME, USER, SYSNAME, MSGCON)", "unmapped"),
    ("dds_subfile", "SFL, SFLCTL and the subfile keywords", "attribute:subfile"),
    ("dds_command_key", "CAnn, CFnn command keys", "attribute:command_keys"),
    ("dds_window", "WINDOW, WDWBORDER", "attribute:window"),
    ("dds_conditioning", "option indicators conditioning a field or keyword", "unmapped"),
    ("dds_display_attribute", "DSPATR, COLOR, EDTCDE, EDTWRD and other presentation keywords", "unmapped"),
    ("dds_message", "ERRMSG, ERRMSGID, SFLMSG, MSGLOC and other message keywords", "unmapped"),
    ("dds_help", "help specifications (H) and help keywords", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"ibmi:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
