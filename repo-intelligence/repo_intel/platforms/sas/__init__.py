"""The SAS platform pack: programs, macros, datasets, formats, reports and stored processes.

It replaces `legacy.sas` and emits at least what that extractor emitted, with the same
kinds and natural keys (a Program per file and per macro, Datasets keyed `lib.member`,
`libname:<ref>` ConfigKeys, CALLS for macro calls and `%INCLUDE`s, READS_FROM and
WRITES_TO from DATA steps, procedures and PROC SQL). One exception, by design: a
one-level name in PROC SQL (`from customers`) is a WORK dataset in SAS, which lives only
for the session, so it is not declared, as WORK datasets in DATA steps never were.

Modules:

- `lexer`: one linear pass from text to tokens and statements (comments, quoting,
  macro calls without semicolons, DATALINES);
- `parse`: statements grouped into DATA and PROC steps and macro definitions;
- `macro`: symbolic macro expansion from what the file pins (`&var`, `&&var`, `&var.`);
- `expr`, `lower`: DATA step and macro statements lowered to the behavioural IR;
- `analysis`: entities, relations, construct counts and behaviour units for one file;
- `inputs`: native inputs under `.rkp/inputs/sas/` (metadata exports, `sasv9.cfg`);
- `values`: URLs reduced to parameter names and mail addresses removed (contract rule 7).

Pack modules import `repo_intel.extractors` modules only inside functions: pack
discovery runs while that package is loading.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, PlatformPack, Term
from repo_intel.platforms.sas import egp
from repo_intel.platforms.sas.inputs import SasInputs

NATIVE_INPUTS = ".rkp/inputs/sas/"


class SasSource:
    """`.sas` programs, including `autoexec.sas` and `config.sas` placed under `.rkp/inputs/sas/`."""

    id = "sas.source"
    version = "1.2.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".sas")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base
        from repo_intel.platforms.sas.analysis import Analysis, decode

        text = decode(data)
        if text is None:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        return Analysis(path, data, blob, text).run()


def _t(name: str, core: K, fidelity: str, description: str) -> Term:
    return Term(f"sas:{name}", core, fidelity, description)  # type: ignore[arg-type]


def _c(name: str, modelled_as: str, description: str) -> Construct:
    return Construct(f"sas:{name}", description, modelled_as)


TERMS = (
    _t("Program", K.PROGRAM, "exact", "a .sas program file"),
    _t("Macro", K.PROGRAM, "lossy", "a %MACRO definition; parameters in attributes"),
    _t("StoredProcess", K.PROGRAM, "lossy", "a program run as a stored process (*ProcessBody; or %STPBEGIN)"),
    _t("Dataset", K.DATASET, "exact", "a permanent dataset or view, lib.member"),
    _t("Library", K.CONFIG_KEY, "lossy", "a LIBNAME library reference; engine kept, path and options never"),
    _t("Fileref", K.CONFIG_KEY, "lossy", "a FILENAME file reference; device kept, path never"),
    _t("EnvironmentVariable", K.CONFIG_KEY, "lossy", "an environment variable read by %SYSGET or set by -SET"),
    _t("Format", K.CODE_LIST, "exact", "a PROC FORMAT VALUE format: codes and labels"),
    _t("Informat", K.CODE_LIST, "lossy", "a PROC FORMAT INVALUE informat: labels read back to codes"),
    _t("PictureFormat", K.CODE_LIST, "unmapped", "a PROC FORMAT PICTURE: a display template, not a code list"),
    _t("Report", K.REPORT, "lossy", "output of PROC REPORT, PRINT or TABULATE outside an ODS document"),
    _t("OdsDocument", K.REPORT, "lossy", "a file an ODS destination writes (PDF, RTF, HTML, Excel)"),
    _t("ExportFile", K.REPORT, "lossy", "a file PROC EXPORT writes"),
    _t("StoredProcessOutput", K.REPORT, "lossy", "what a stored process streams back to its caller"),
    _t("PromptPage", K.SCREEN, "lossy", "the prompt page of a stored process"),
    _t("PromptGroup", K.FORM, "lossy", "the prompts a stored process takes, submitted together"),
    _t("Prompt", K.UI_CONTROL, "lossy", "one stored-process prompt, read as a macro variable"),
    _t("MetadataJob", K.JOB, "lossy", "a DI Studio job or Enterprise Guide process flow from a metadata export"),
    _t("MetadataStep", K.TASK, "lossy", "a transformation or program node of a metadata job"),
)

_PROCS = {
    "sort": "PROC SORT: reads DATA=, writes OUT= (or sorts in place)",
    "means": "PROC MEANS: summary statistics, OUTPUT OUT=",
    "summary": "PROC SUMMARY: summary statistics, OUTPUT OUT=",
    "freq": "PROC FREQ: frequency tables, TABLES / OUT=",
    "report": "PROC REPORT: a report",
    "print": "PROC PRINT: a listing report",
    "tabulate": "PROC TABULATE: a tabular report",
    "transpose": "PROC TRANSPOSE: reshapes DATA= into OUT=",
    "export": "PROC EXPORT: writes a dataset to a file",
    "import": "PROC IMPORT: reads a file into OUT=",
    "append": "PROC APPEND: appends DATA= to BASE=",
    "datasets": "PROC DATASETS: library maintenance",
    "contents": "PROC CONTENTS: describes a dataset",
    "copy": "PROC COPY: copies members between libraries",
    "univariate": "PROC UNIVARIATE: distribution statistics",
    "reg": "PROC REG: linear regression",
    "logistic": "PROC LOGISTIC: logistic regression",
    "glm": "PROC GLM: general linear models",
    "mixed": "PROC MIXED: mixed models",
    "genmod": "PROC GENMOD: generalised linear models",
    "sgplot": "PROC SGPLOT: a graph",
    "sgpanel": "PROC SGPANEL: a panel of graphs",
    "gplot": "PROC GPLOT: a graph (SAS/GRAPH)",
    "gchart": "PROC GCHART: a chart (SAS/GRAPH)",
    "rank": "PROC RANK: ranks DATA= into OUT=",
    "corr": "PROC CORR: correlations",
    "compare": "PROC COMPARE: compares two datasets",
    "surveyselect": "PROC SURVEYSELECT: samples DATA= into OUT=",
}
_UNMAPPED_PROCS = {
    "ds2": "PROC DS2: DS2 programs (threads, packages), not lowered",
    "fcmp": "PROC FCMP: user-defined functions, not lowered",
    "http": "PROC HTTP: web requests; URLs are never recorded",
    "iml": "PROC IML: matrix language, not lowered",
    "printto": "PROC PRINTTO: log and output redirection",
    "pwencode": "PROC PWENCODE: password encoding; never read",
    "catalog": "PROC CATALOG: catalog maintenance",
}

CONSTRUCTS = (
    # DATA step
    _c("data_step", "behavior:loop", "a DATA step: one behaviour unit, its implicit loop over the input"),
    _c("data_view", "unmapped", "DATA ... / VIEW=: a stored DATA step view"),
    _c("set", "relation:READS_FROM", "SET: reads datasets"),
    _c("merge", "relation:READS_FROM", "MERGE: match-merges datasets"),
    _c("update", "relation:READS_FROM", "UPDATE: applies transactions to a master dataset"),
    _c("modify", "relation:WRITES_TO", "MODIFY: changes a dataset in place"),
    _c("dataset_list", "unmapped", "a dataset list or physical file named only at run time (ds1-ds9, lib.pre:, '/path')"),
    _c("by_group", "behavior:loop", "BY: BY-group processing"),
    _c("first_last", "behavior:platform", "FIRST.var / LAST.var: BY-group boundaries"),
    _c("retain", "behavior:declare", "RETAIN: values kept across iterations"),
    _c("array", "behavior:declare", "ARRAY: a group of variables"),
    _c("assignment", "behavior:assign", "an assignment statement"),
    _c("sum_statement", "behavior:assign", "a sum statement (var + expr;)"),
    _c("if_then", "behavior:if", "IF / THEN / ELSE"),
    _c("subsetting_if", "behavior:if", "a subsetting IF: drops the observation"),
    _c("where", "behavior:if", "WHERE: filters the input"),
    _c("select", "behavior:switch", "SELECT / WHEN / OTHERWISE"),
    _c("do_loop", "behavior:loop", "iterative DO, DO WHILE, DO UNTIL, DO OVER"),
    _c("do_group", "behavior:if", "a DO; ... END; group"),
    _c("output", "behavior:output", "OUTPUT: writes the observation"),
    _c("delete", "behavior:continue", "DELETE: drops the observation"),
    _c("stop", "behavior:exit", "STOP: ends the step"),
    _c("return", "behavior:return", "RETURN"),
    _c("abort", "behavior:exit", "ABORT and %ABORT"),
    _c("keep_drop", "attribute:keep", "KEEP / DROP"),
    _c("rename", "attribute:rename", "RENAME"),
    _c("length", "behavior:declare", "LENGTH"),
    _c("format_statement", "relation:READS_FROM", "FORMAT / INFORMAT: applies formats"),
    _c("label", "attribute:label", "LABEL"),
    _c("attrib", "behavior:declare", "ATTRIB"),
    _c("infile_input", "behavior:call", "INFILE / INPUT: reads an external file"),
    _c("file_put", "behavior:output", "FILE / PUT / PUTLOG: writes to a file or the log"),
    _c("datalines", "behavior:platform", "DATALINES / CARDS: inline data"),
    _c("call_routine", "behavior:call", "CALL routine"),
    _c("call_execute", "unmapped", "CALL EXECUTE: generates code at run time"),
    _c("call_symput", "behavior:assign", "CALL SYMPUT / SYMPUTX: sets a macro variable at run time"),
    _c("hash_object", "relation:READS_FROM", "DECLARE HASH and hash methods"),
    _c("goto_link", "unmapped", "GOTO / LINK and labels"),
    _c("data_other", "unmapped", "other DATA step statements (WINDOW, DISPLAY, MISSING, ...)"),
    _c("unparsed_statement", "unmapped", "a statement or expression the parser could not read"),
    _c("global_statement", "behavior:platform", "a global statement inside a step"),
    # macro language
    _c("macro_definition", "entity:Program", "%MACRO ... %MEND"),
    _c("macro_call", "relation:CALLS", "a macro call (%name)"),
    _c("let", "behavior:assign", "%LET"),
    _c("macro_if", "behavior:if", "%IF / %THEN / %ELSE"),
    _c("macro_do", "behavior:loop", "%DO, %DO %WHILE, %DO %UNTIL"),
    _c("include", "relation:CALLS", "%INCLUDE"),
    _c("global_local", "behavior:declare", "%GLOBAL / %LOCAL"),
    _c("macro_put", "behavior:output", "%PUT"),
    _c("macro_goto", "unmapped", "%GOTO and %label:"),
    _c("macro_other", "unmapped", "other macro statements (%SYSCALL, %WINDOW, ...)"),
    _c("macro_variable_ref", "attribute:parameterized", "a macro variable reference (&name): resolved or kept as a placeholder"),
    _c("sysfunc", "behavior:call", "%SYSFUNC / %QSYSFUNC"),
    _c("sysget", "relation:REFERENCES_CONFIG", "%SYSGET / SYSGET: reads an environment variable"),
    _c("x_command", "unmapped", "X, %SYSEXEC, SYSTASK: operating-system commands (never recorded)"),
    _c("stpbegin_stpend", "entity:Report", "%STPBEGIN / %STPEND: stored-process output"),
    _c("stored_process", "entity:Program", "a stored process"),
    _c("stp_prompt", "entity:UIControl", "a stored-process prompt"),
    # global statements
    _c("libname", "entity:ConfigKey", "LIBNAME"),
    _c("filename", "entity:ConfigKey", "FILENAME"),
    _c("filename_pipe", "unmapped", "FILENAME PIPE: runs a command"),
    _c("options", "unmapped", "OPTIONS: system options (values not recorded)"),
    _c("title_footnote", "attribute:title", "TITLE / FOOTNOTE"),
    _c("ods_destination", "entity:Report", "ODS destination open and close"),
    _c("ods_output", "relation:WRITES_TO", "ODS OUTPUT: output objects to datasets"),
    _c("ods_other", "unmapped", "other ODS statements (styles, selection, graphics)"),
    _c("endsas", "behavior:exit", "ENDSAS"),
    _c("sas_connect", "unmapped", "SIGNON / RSUBMIT: SAS/CONNECT remote sessions"),
    _c("dm_statement", "unmapped", "DM: display manager commands"),
    _c("global_other", "unmapped", "other global statements"),
    _c("autoexec", "attribute:role", "a program from .rkp/inputs/sas/ (autoexec.sas, config.sas)"),
    # PROC SQL
    _c("proc_sql", "behavior:query", "PROC SQL: one behaviour unit; lineage of its statements"),
    _c("sql_passthrough", "unmapped", "CONNECT TO / CONNECTION TO / EXECUTE BY: database-side SQL; the engine is kept (a query's `remote`, a platform node), connection options never"),
    _c("sql_into_macro", "behavior:query", "SELECT ... INTO :macro-variable"),
    _c("dictionary_tables", "unmapped", "DICTIONARY tables: session metadata"),
    # formats
    _c("proc_format", "entity:CodeList", "PROC FORMAT"),
    _c("format_definition", "entity:CodeList", "VALUE / INVALUE / PICTURE"),
    _c("format_use", "relation:READS_FROM", "a user format applied (FORMAT, PUT, INPUT)"),
    _c("format_cntlin", "unmapped", "PROC FORMAT CNTLIN=: formats built from data"),
    _c("fcmp_function", "unmapped", "PROC FCMP FUNCTION / SUBROUTINE"),
    _c("datasets_maintenance", "unmapped", "PROC DATASETS DELETE / CHANGE / MODIFY / AGE"),
    # procedures
    *(_c(f"proc_{name}", "behavior:call", text) for name, text in _PROCS.items()),
    *(_c(f"proc_{name}", "unmapped", text) for name, text in _UNMAPPED_PROCS.items()),
    _c("proc_other", "behavior:call", "any other procedure: a call node"),
    _c("proc_step", "behavior:platform", "in open code or a macro, a reference to a procedure's own unit (the procedure is counted by its sas:proc_<name> construct)"),
    # native inputs
    _c("metadata_job", "entity:Job", "a job or process flow in a metadata export"),
    _c("metadata_step", "entity:Task", "a step of a metadata job"),
    _c("config_option", "entity:ConfigKey", "an option in a SAS configuration file (-SET declares a ConfigKey)"),
)

PACK = PlatformPack(
    id="sas",
    name="SAS",
    prefix="sas",
    version="1.2.0",
    terms=TERMS + egp.TERMS,
    constructs=CONSTRUCTS + egp.CONSTRUCTS,
    extractors=(SasSource(), SasInputs(), egp.EgpProject()),
    supersedes=("legacy.sas",),
    inputs=(NATIVE_INPUTS + "**",),
)
