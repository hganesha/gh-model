"""The shell pack's vocabulary: platform terms, the commands it recognises and the constructs it counts."""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "shell"  # Program.platform for every script this pack reads, whatever its shell

TERMS = (
    Term("sh:Script", K.PROGRAM, "lossy", "a POSIX shell script (.sh, .ksh, .bash); name is its path, the shell in `shell`"),
    Term("sh:BatchFile", K.PROGRAM, "lossy", "a Windows batch file (.bat, .cmd); name is its path"),
    Term("sh:PowerShellScript", K.PROGRAM, "lossy", "a PowerShell script (.ps1); name is its path"),
    Term("sh:ToolStep", K.TASK, "lossy", "one command that runs a tool or a script (sas, spark-submit, dsjob, pmcmd, dtexec, tbuild, a SQL client given a file, another script); it RUNS what it names"),
)

# SQL command-line clients: command name -> (construct, sql_lineage dialect key or sqlglot dialect, engine).
# The dialect is what `sql_lineage.lineage` reads the text as; `None` is generic SQL.
SQL_CLIENTS: dict[str, tuple[str, str | None, str]] = {
    "sqlplus": ("sh:sql.sqlplus", "oracle", "oracle"),
    "bteq": ("sh:sql.bteq", "teradata", "teradata"),
    "db2": ("sh:sql.db2", None, "db2"),
    "clpplus": ("sh:sql.db2", None, "db2"),
    "nzsql": ("sh:sql.nzsql", "postgres", "netezza"),
    "hive": ("sh:sql.hive", "hive", "hive"),
    "beeline": ("sh:sql.beeline", "hive", "hive"),
    "impala-shell": ("sh:sql.impala", "hive", "impala"),
    "psql": ("sh:sql.psql", "postgres", "postgres"),
    "sqlcmd": ("sh:sql.sqlcmd", "tsql", "tsql"),
    "osql": ("sh:sql.sqlcmd", "tsql", "tsql"),
    "isql": ("sh:sql.isql", "tsql", "sybase"),
    "mysql": ("sh:sql.mysql", "mysql", "mysql"),
    "snowsql": ("sh:sql.snowsql", "snowflake", "snowflake"),
}

# Tools a script runs, by command name -> construct. What each runs is read in `commands.py`.
TOOLS: dict[str, str] = {
    "sas": "sh:run.sas", "sas.exe": "sh:run.sas", "sasbatch": "sh:run.sas",
    "spark-submit": "sh:run.spark-submit", "spark2-submit": "sh:run.spark-submit", "pyspark": "sh:run.spark-submit",
    "dsjob": "sh:run.dsjob",
    "pmcmd": "sh:run.pmcmd",
    "dtexec": "sh:run.dtexec", "dtexec.exe": "sh:run.dtexec",
    "tbuild": "sh:run.tbuild",
    "fastload": "sh:run.fastload", "mload": "sh:run.multiload", "fexp": "sh:run.fastexport", "tpump": "sh:run.tpump",
    "sh": "sh:run.script", "bash": "sh:run.script", "ksh": "sh:run.script", "zsh": "sh:run.script", "cmd": "sh:run.script", "cmd.exe": "sh:run.script",
    "powershell": "sh:run.script", "pwsh": "sh:run.script", "powershell.exe": "sh:run.script",
    "python": "sh:run.python", "python3": "sh:run.python", "python2": "sh:run.python",
    "java": "sh:run.java",
    "hadoop": "sh:run.hadoop", "yarn": "sh:run.hadoop",
    "sqoop": "sh:run.sqoop",
}

# File transfer commands -> construct.
TRANSFERS: dict[str, str] = {
    "ftp": "sh:transfer.ftp", "sftp": "sh:transfer.sftp", "scp": "sh:transfer.scp", "pscp": "sh:transfer.scp",
    "direct": "sh:transfer.connect-direct", "ndmcli": "sh:transfer.connect-direct", "direct.exe": "sh:transfer.connect-direct",
    "rsync": "sh:transfer.rsync",
    "curl": "sh:transfer.http", "wget": "sh:transfer.http",
    "aws": "sh:transfer.cloud", "gsutil": "sh:transfer.cloud", "azcopy": "sh:transfer.cloud", "az": "sh:transfer.cloud",
    "hdfs": "sh:transfer.hdfs",
}
MAILERS = {"mail", "mailx", "sendmail", "mutt", "send-mailmessage"}

# Words whose values are never copied into behaviour: a variable named like this keeps its name only.
SENSITIVE_NAME = r"(?i)(pass|pwd|secret|token|key|cred|auth|logon|login|conn|dsn|user|host|server|url|uri)"

_CONSTRUCTS = (
    # Script structure (POSIX shells)
    ("assignment", "variable assignment NAME=value", "behavior:assign"),
    ("export", "export or readonly of a variable", "behavior:assign"),
    ("local", "local, typeset or declare inside a function", "behavior:declare"),
    ("function", "function definition", "behavior:declare"),
    ("if", "if / elif / else", "behavior:if"),
    ("case", "case ... esac", "behavior:switch"),
    ("for", "for loop over words", "behavior:loop"),
    ("while", "while loop", "behavior:loop"),
    ("until", "until loop", "behavior:loop"),
    ("and-or", "a && b, a || b lists", "behavior:if"),
    ("pipeline", "a | b pipeline", "behavior:platform"),
    ("subshell", "( ... ) subshell", "behavior:platform"),
    ("background", "a command run in the background with &", "behavior:platform"),
    ("command-substitution", "$(...) or `...`", "behavior:platform"),
    ("arithmetic", "$((...)) or ((...))", "behavior:platform"),
    ("parameter-expansion", "${var:-default}, ${var%pattern}, ${#var} and the like: the operator is kept, not its operand", "behavior:platform"),
    ("test", "[ ], [[ ]] or test", "behavior:call"),
    ("heredoc", "here-document <<EOF given to a command", "attribute:stdin"),
    ("here-string", "here-string <<< given to a command", "attribute:stdin"),
    ("redirect", "redirection of output or input to a file", "behavior:output"),
    ("source", "source or . of another script", "behavior:include"),
    ("exit", "exit", "behavior:exit"),
    ("return", "return from a function", "behavior:return"),
    ("break-continue", "break or continue", "behavior:break"),
    ("trap", "trap handler", "behavior:platform"),
    ("set-option", "set -e, set -o pipefail and other shell options", "behavior:call"),
    ("eval", "eval: the command is built at run time and not read", "unmapped"),
    ("dynamic-command", "a command whose name is a variable or substitution", "unmapped"),
    ("echo", "echo or printf", "behavior:output"),
    ("command", "any other command", "behavior:call"),
    ("sensitive-value", "a value assigned to a variable named like a password, key, user, host or connection: never copied", "behavior:assign"),
    ("syntax-error", "text the parser could not read", "unmapped"),
    # SQL clients: SQL given inline or on standard input goes through the SQL scanner; a file runs as a step
    ("sql.sqlplus", "Oracle SQL*Plus", "relation:READS_FROM"),
    ("sql.bteq", "Teradata BTEQ", "relation:READS_FROM"),
    ("sql.db2", "DB2 command line processor or CLPPlus", "relation:READS_FROM"),
    ("sql.nzsql", "Netezza nzsql", "relation:READS_FROM"),
    ("sql.hive", "hive -e or -f", "relation:READS_FROM"),
    ("sql.beeline", "beeline -e or -f", "relation:READS_FROM"),
    ("sql.impala", "impala-shell -q or -f", "relation:READS_FROM"),
    ("sql.psql", "PostgreSQL psql", "relation:READS_FROM"),
    ("sql.sqlcmd", "SQL Server sqlcmd or osql", "relation:READS_FROM"),
    ("sql.isql", "Sybase isql", "relation:READS_FROM"),
    ("sql.mysql", "MySQL client", "relation:READS_FROM"),
    ("sql.snowsql", "Snowflake snowsql", "relation:READS_FROM"),
    ("sql.client-command", "a client command inside the SQL text (SQL*Plus SET, BTEQ dot command, psql backslash command)", "unmapped"),
    ("sql.client-file", "a SQL client given a script file (@file, -f, < file): a step that RUNS the file", "relation:RUNS"),
    ("sql.db2-export", "DB2 EXPORT TO file ... SELECT: a dataset written from a query", "relation:WRITES_TO"),
    ("sql.db2-load", "DB2 LOAD or IMPORT FROM file ... INSERT INTO: a table loaded from a dataset", "relation:READS_FROM"),
    ("sql.bteq-file", "BTEQ .EXPORT or .IMPORT of a file", "relation:WRITES_TO"),
    ("sql.dynamic", "SQL text or a table name built from a variable whose value is not in the file", "unmapped"),
    # Tools run: a step that RUNS a Program, Job or Artifact named without declaring it
    ("run.sas", "sas -sysin program.sas", "relation:RUNS"),
    ("run.spark-submit", "spark-submit of a Python file or a jar", "relation:RUNS"),
    ("run.dsjob", "dsjob -run project job (DataStage)", "relation:RUNS"),
    ("run.pmcmd", "pmcmd startworkflow (Informatica PowerCenter)", "relation:RUNS"),
    ("run.dtexec", "dtexec /F package.dtsx or /SQL or /ISServer (SSIS)", "relation:RUNS"),
    ("run.tbuild", "tbuild -f script (Teradata Parallel Transporter)", "relation:RUNS"),
    ("run.fastload", "fastload < script", "relation:RUNS"),
    ("run.multiload", "mload < script", "relation:RUNS"),
    ("run.fastexport", "fexp < script", "relation:RUNS"),
    ("run.tpump", "tpump < script", "unmapped"),
    ("run.script", "another shell, batch or PowerShell script, run by its interpreter or by path", "relation:RUNS"),
    ("run.python", "python script.py", "relation:RUNS"),
    ("run.java", "java -jar or a main class", "unmapped"),
    ("run.hadoop", "hadoop jar or yarn jar", "unmapped"),
    ("run.sqoop", "sqoop import or export", "unmapped"),
    ("run.dynamic", "a tool whose target is built from a variable whose value is not in the file", "unmapped"),
    # File transfers: dataset reads and writes; the host is never copied
    ("transfer.ftp", "ftp with put, get, mput or mget", "relation:WRITES_TO"),
    ("transfer.sftp", "sftp with put or get, from a here-document or a batch file", "relation:WRITES_TO"),
    ("transfer.scp", "scp or pscp between a local and a remote path", "relation:WRITES_TO"),
    ("transfer.connect-direct", "Connect:Direct submit of a COPY process", "relation:WRITES_TO"),
    ("transfer.rsync", "rsync", "unmapped"),
    ("transfer.http", "curl or wget", "unmapped"),
    ("transfer.cloud", "aws s3, gsutil, azcopy or az storage copies", "unmapped"),
    ("transfer.hdfs", "hdfs dfs -put, -get and the like", "unmapped"),
    ("transfer.dynamic", "a transfer whose path is built from a variable whose value is not in the file", "unmapped"),
    ("mail", "mail, mailx or sendmail", "behavior:output"),
    # Windows batch files
    ("bat.set", "set NAME=value", "behavior:assign"),
    ("bat.if", "if ... ( ... ) else ( ... )", "unmapped"),
    ("bat.for", "for loop", "unmapped"),
    ("bat.goto", "goto and labels", "unmapped"),
    ("bat.call", "call of another batch file", "relation:RUNS"),
    ("bat.errorlevel", "an ERRORLEVEL test", "unmapped"),
    # PowerShell
    ("ps.invoke-sqlcmd", "Invoke-Sqlcmd -Query or -InputFile", "relation:READS_FROM"),
    ("ps.here-string", "a here-string @\" \"@ given to a command", "attribute:stdin"),
    ("ps.function", "function definition", "unmapped"),
    ("ps.control", "if, foreach, while, try and switch", "unmapped"),
    ("ps.invoke", "& or Start-Process of a tool or script", "relation:RUNS"),
)
CONSTRUCTS = tuple(Construct(f"sh:{name}", description, modelled_as) for name, description, modelled_as in _CONSTRUCTS)
