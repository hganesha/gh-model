"""Reference lists for SAS: macro statements and functions, built-in formats, procedure groups.

Kept apart from the parser so the lists can grow without touching parsing code. Every name is
lower case.
"""

from __future__ import annotations

# Macro statements: `%let x = 1;`. They are never calls to user macros.
MACRO_STATEMENTS = frozenset({
    "let", "put", "if", "then", "else", "do", "end", "to", "by", "while", "until", "global", "local", "include", "inc",
    "macro", "mend", "return", "abort", "goto", "go", "sysexec", "syscall", "symdel", "window", "display", "input",
    "copy", "sysmacdelete", "syslput", "sysrput", "sysmstoreclear", "run",
})

# Macro functions, quoting functions and the autocall macros SAS ships. A `%name` in this set is
# not a call to a macro of the repository.
MACRO_FUNCTIONS = frozenset({
    "sysfunc", "qsysfunc", "str", "nrstr", "quote", "nrquote", "bquote", "nrbquote", "superq", "unquote", "eval", "sysevalf",
    "scan", "qscan", "substr", "qsubstr", "upcase", "qupcase", "lowcase", "qlowcase", "index", "length", "cmpres", "qcmpres",
    "trim", "qtrim", "left", "qleft", "verify", "datatyp", "sysget", "sysprod", "symexist", "symglobl", "symlocal", "sysmacexist",
    "sysmexecname", "symput", "tslit", "compstor", "keydef", "mbsfunc", "kscan", "ksubstr", "kupcase", "klength", "kindex",
    "qkscan", "qksubstr", "kverify", "kcmpres", "qkcmpres", "kleft", "qkleft", "ktrim", "qktrim", "klowcase", "qklowcase",
    "sysmexecdepth", "ds2", "list", "stpbegin", "stpend",
})

MACRO_KEYWORDS = MACRO_STATEMENTS | MACRO_FUNCTIONS

# Quoting functions whose argument is literal text: `%str(a;b)` is the text `a;b`.
QUOTING = frozenset({"str", "nrstr", "quote", "nrquote", "bquote", "nrbquote", "superq"})

# Automatic macro variables and stored-process reserved variables: always set at run time.
AUTOMATIC_PREFIXES = ("sys", "_")

# Libraries that are temporary or ship with SAS: their members are not application data.
SYSTEM_LIBRARIES = frozenset({"work", "sashelp", "sasuser", "dictionary", "maps", "mapsgfk", "mapssas", "sasmsg"})

# DATA-step statements that SET, MERGE, UPDATE or MODIFY read.
READ_STATEMENTS = {"set": "sas:set", "merge": "sas:merge", "update": "sas:update", "modify": "sas:modify"}

# Statements that are global wherever they appear (outside or inside a step).
GLOBAL_STATEMENTS = frozenset({
    "libname", "filename", "options", "option", "title", "footnote", "ods", "x", "endsas", "dm", "signon", "signoff",
    "rsubmit", "endrsubmit", "goptions", "axis", "symbol", "pattern", "legend", "sasfile", "lock", "catname", "missing",
    "page", "skip", "resetline", "systask", "waitfor", "run", "quit",
})

# Built-in formats and informats, without width: `date9.` is `date`, `$char10.` is `$char`.
BUILTIN_FORMATS = frozenset({
    "best", "bestd", "comma", "commax", "dollar", "dollarx", "euro", "eurox", "nlnum", "nlmny", "percent", "percentn", "pvalue",
    "z", "w", "d", "e", "f", "fract", "hex", "octal", "binary", "roman", "words", "wordf", "negparen", "numx", "ssn",
    "date", "datetime", "dtdate", "dtmonyy", "dtyear", "dtwkdatx", "time", "timeampm", "tod", "hhmm", "hour", "mmss",
    "ddmmyy", "ddmmyyb", "ddmmyyc", "ddmmyyd", "ddmmyyn", "ddmmyyp", "ddmmyys", "mmddyy", "mmddyyb", "mmddyyc",
    "mmddyyd", "mmddyyn", "mmddyyp", "mmddyys", "yymmdd", "yymmddb", "yymmddc", "yymmddd", "yymmddn", "yymmddp",
    "yymmdds", "yymm", "yymmc", "yymmd", "yymmn", "yymmp", "yymms", "yymon", "yyq", "yyqc", "yyqd", "yyqn", "yyqp",
    "yyqs", "yyqr", "mmyy", "mmyyc", "mmyyd", "mmyyn", "mmyyp", "mmyys", "monyy", "monname", "month", "monthc", "qtr", "qtrr",
    "year", "weekdate", "weekdatx", "weekday", "worddate", "worddatx", "downame", "day", "julian", "juldate", "juldaz",
    "e8601da", "e8601dt", "e8601dn", "e8601tm", "e8601dz", "e8601tz", "b8601da", "b8601dt", "b8601tm", "is8601da",
    "is8601dt", "anydtdte", "anydtdtm", "anydttme", "nldate", "nldatm", "nltime", "datew", "ib", "pib", "pd", "rb", "s370fpd",
    "s370fzd", "zd", "bz", "trailsgn", "char", "$char", "$", "$upcase", "$quote", "$hex", "$varying", "$ascii", "$ebcdic",
    "$w", "$f", "$base64x", "$binary", "$octal", "$reverj", "$revers", "$utf8x", "$md5", "$charzb", "$n8601b", "$n8601e",
    "nenge", "commaw", "yen", "mrb", "ieee", "float", "time5", "stimer",
})

# Procedures grouped by what they do with data and output.
REPORT_PROCS = frozenset({"report", "print", "tabulate"})  # each becomes a Report of its own outside an ODS file
OUTPUT_PROCS = REPORT_PROCS | frozenset({
    "freq", "means", "univariate", "corr", "reg", "logistic", "glm", "mixed", "genmod", "anova", "ttest", "npar1way",
    "sgplot", "sgpanel", "sgscatter", "sgrender", "gplot", "gchart", "gmap", "gbarline", "contents", "compare", "chart",
    "plot", "standard", "rank", "surveyselect", "surveymeans", "surveyfreq", "phreg", "lifetest", "arima", "forecast",
    "timeseries", "esm", "factor", "princomp", "cluster", "fastclus", "discrim", "hpsplit", "hplogistic", "hpreg",
    "glmselect", "quantreg", "nlmixed", "glimmix", "catmod", "sql",
})
STAT_PROCS = frozenset({
    "reg", "logistic", "glm", "mixed", "genmod", "glimmix", "glmselect", "phreg", "nlmixed", "quantreg", "hplogistic", "hpreg",
    "hpsplit", "discrim", "factor", "princomp", "cluster", "fastclus", "arima", "forecast", "esm", "timeseries", "anova",
    "ttest", "npar1way", "lifetest", "catmod", "corr", "standard", "rank", "surveyselect", "surveymeans", "surveyfreq",
})
GRAPH_PROCS = frozenset({"sgplot", "sgpanel", "sgscatter", "sgrender", "gplot", "gchart", "gmap", "gbarline", "chart", "plot"})

# ODS destinations that write a document a person reads.
ODS_DOCUMENTS = frozenset({
    "pdf", "rtf", "html", "html5", "excel", "csv", "csvall", "powerpoint", "epub", "epub3", "word", "printer", "ps", "pcl",
    "markup", "tagsets.excelxp", "tagsets.rtf", "tagsets.csv", "tagsets.html5", "tagsets.msoffice2k", "tagsets.tableeditor",
    "tagsets.sasreport13", "tagsets.pdf", "layout",
})

# FILENAME devices that run something or reach outside the file system.
FILENAME_DEVICES = frozenset({"pipe", "url", "ftp", "sftp", "email", "temp", "catalog", "dummy", "terminal", "zip", "socket", "printer", "plotter", "disk", "http", "s3", "webdav", "hadoop", "azure", "gzip"})


def is_automatic(name: str) -> bool:
    return name.lower().startswith(AUTOMATIC_PREFIXES)
