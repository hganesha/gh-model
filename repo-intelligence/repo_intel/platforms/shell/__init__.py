"""The shell pack: POSIX shell scripts, Windows batch files and PowerShell scripts that drive a warehouse.

No other extractor reads these files (`data_jobs` only reads the Python a job's command
line names), so this pack is their only reader. Each script is a `Program`
`{platform: "shell", name: <path>}` (`sh:Script`, `sh:BatchFile`, `sh:PowerShellScript`):

- SQL a script sends to a client (`sqlplus`, `bteq`, `db2`, `nzsql`, `hive -e`, `beeline`,
  `psql`, `sqlcmd`, `isql`, ...) in a here-document, a here-string, an `echo` pipe or an
  inline option goes through the SQL scanner in the client's dialect: the script READS_FROM
  and WRITES_TO the tables it names, and CALLS the procedures it calls.
- Each command that runs a tool or a script (`sas`, `spark-submit`, `dsjob`, `pmcmd`,
  `dtexec`, `tbuild`, `fastload`, `mload`, `fexp`, `python`, a SQL client given a file,
  another script) is a `Task` (`sh:ToolStep`, `{framework: "shell", job: <script path>,
  name: <tool>:<target>}`) the Program CONTAINS, which RUNS the Program, Job or Artifact it
  names, referred to without declaring it.
- File transfers (`ftp`, `sftp`, `scp`, Connect:Direct) read and write Datasets; a remote
  file is `<protocol>:<path>`, never its host or user.
- POSIX scripts get one behaviour unit (`open_code`); batch files one flat unit; PowerShell
  is read for commands only.

Modules: `posix` (the POSIX shell parser), `lower` (behaviour), `commands` (what a command
does, shared by every shell), `windows` (batch files and PowerShell), `emit` (one file's
observations, shared with the `teradata` and `dbproc` packs), `vocabulary` and `source`.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.shell.source import ShellSource
from repo_intel.platforms.shell.vocabulary import CONSTRUCTS, TERMS
from repo_intel.platforms.shell.windows import BatchSource, PowerShellSource

PACK = PlatformPack(
    id="shell",
    name="Shell scripts (sh, ksh, bash, Windows batch, PowerShell)",
    prefix="sh",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(ShellSource(), BatchSource(), PowerShellSource()),
)
