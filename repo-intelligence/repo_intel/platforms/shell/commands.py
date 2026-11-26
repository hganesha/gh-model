"""What a command in a script does: SQL it sends, tools and scripts it runs, files it transfers.

Shared by the three shells this pack reads. A shell's front end (`lower.py` for POSIX
shells, `windows.py` for batch files and PowerShell) builds a `Cmd`: the command name, its
arguments as source parts (literal text, variables, substitutions) and what it reads on
standard input. `interpret` emits the facts through the file's `Out` and returns the
behaviour statements that stand for the command.

- **SQL clients** (`sqlplus`, `bteq`, `db2`, `nzsql`, `hive`, `beeline`, `psql`, `sqlcmd`,
  `isql`, ...): SQL given inline (`-e`, `-c`, `-q`, a `db2` statement) or on standard input
  (a here-document, a here-string, `echo ... |`) goes through the SQL scanner
  (`sql_lineage.lineage`) in the client's dialect after the client's own commands are
  blanked; the script READS_FROM and WRITES_TO the tables, and each statement is a `query`
  node. `CALL` and `EXEC` of a procedure CALL that procedure's Program, named without
  declaring it. A script file given to a client (`@file`, `-f`, `< file`) is a step.
- **Tools** (`sas`, `spark-submit`, `dsjob`, `pmcmd`, `dtexec`, `tbuild`, `fastload`,
  `mload`, `fexp`, `python`, another script): one `Task` (`sh:ToolStep`) that the script's
  Program CONTAINS and that RUNS what the command names, referred to without declaring it,
  so it links only when the repository defines it.
- **Transfers** (`ftp`, `sftp`, `scp`, Connect:Direct): the local and the remote file as
  Datasets the script reads or writes. A remote file is named `<protocol>:<path>`; the host,
  the user and every credential are never copied.

A value is used only when the file gives it with certainty (`Resolver`); otherwise the
target stays dynamic, a diagnostic says so, and nothing is guessed. Option values that are
not targets (parameters, connection strings, users, passwords, URLs) are never read.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.shell.emit import PLACEHOLDER, Expanded, K, Out, P, artifact_ref, param_name, program_ref
from repo_intel.platforms.shell.posix import Part
from repo_intel.platforms.shell.vocabulary import MAILERS, PLATFORM, SENSITIVE_NAME, SQL_CLIENTS, TOOLS, TRANSFERS

SCRIPT_DIR = "\x00dir\x00"  # the value of $(dirname "$0"), %~dp0 and $PSScriptRoot: the script's own directory
_SENSITIVE = re.compile(SENSITIVE_NAME)
_SECRETISH = re.compile(r"(?i)(?:^|[\s;])(?:password|pwd|user\s*id|uid)\s*=|\S+/\S*@\S+|^[^/\s]+@[^\s]+$|://[^/\s]*@")
SCRIPT_SUFFIXES = (".sh", ".ksh", ".bash", ".bat", ".cmd", ".ps1", ".zsh")


# ------------------------------------------------------------------ arguments


@dataclass
class Arg:
    parts: list[Part]
    start: int
    end: int


@dataclass
class Cmd:
    name: str  # the command as written, lower-cased for Windows shells
    name_arg: Arg | None
    args: list[Arg]
    start: int
    end: int
    stdin: Expanded | None = None  # a here-document, here-string or piped echo, expanded
    stdin_at: tuple[int, int] | None = None
    stdin_file: Arg | None = None  # `< file`
    stdout_file: Arg | None = None  # `> file` / `>> file`


class Resolver:
    """Values the file gives with certainty: `values[name]` is set only for a variable assigned
    once, at the top level, to a literal (or to other such variables); `$(dirname "$0")` and
    its equivalents are the script's own directory. Everything else is dynamic."""

    def __init__(self, values: dict[str, tuple[str, int]] | None = None):
        self.values = values or {}

    def value(self, name: str, at: int, modifier: str = "") -> str | None:
        v = self.values.get(name)
        if v is None or v[1] > at:
            return None
        if modifier in ("", ":-", "-") or modifier.startswith((":-", "-", ":=", "=")):
            return v[0] if (v[0] or not modifier.startswith(":")) else None
        return None


def var_expr(name: str) -> dict[str, Any]:
    if name.isdigit() or name in ("@", "*", "#"):
        return {"e": "var", "scope": "arguments", "name": name}
    if name in ("?", "$", "!", "-"):
        return {"e": "var", "scope": "shell", "name": name}
    return {"e": "var", "scope": "shell", "name": name}


def part_expr(p: Part) -> dict[str, Any]:
    if p.kind == "lit":
        return {"e": "lit", "value": p.text, "type": "string"}
    if p.kind == "var":
        if SCRIPT_DIR in p.text:
            return {"e": "name", "name": "script_dir", "kind": "builtin"}
        e = var_expr(p.text)
        # ${x:-default}, ${x%.csv}, ${#x}: the operator is kept, never the default's value
        return e if not p.modifier else {"e": "platform", "construct": "sh:parameter-expansion", "detail": {"var": p.text, "operator": re.match(r"[:#%/^,@!]*[-=+?]?|", p.modifier).group()[:3] or "?"}, "value": e}
    if p.kind == "cmd":
        return {"e": "platform", "construct": "sh:command-substitution", "detail": {"command": _first_word(p.text)}}
    if p.kind == "arith":
        return {"e": "platform", "construct": "sh:arithmetic"}
    return {"e": "platform", "construct": "sh:command-substitution", "detail": {"command": _first_word(p.text), "process": True}}


def _first_word(text: str) -> str:
    m = re.match(r"\s*([\w./-]+)", text)
    return m.group(1).rsplit("/", 1)[-1] if m else ""


def arg_expr(arg: Arg, sensitive: bool = False) -> dict[str, Any]:
    """An argument as an expression. A literal that reads as a credential, a connection or a
    remote `user@host` is withheld."""
    if not arg.parts:
        return {"e": "lit", "value": "", "type": "string"}
    if sensitive or any(p.kind == "lit" and _SECRETISH.search(p.text) for p in arg.parts):
        return {"e": "platform", "construct": "sh:sensitive-value"}
    if len(arg.parts) == 1:
        return part_expr(arg.parts[0])
    if all(p.kind == "lit" for p in arg.parts):
        return {"e": "lit", "value": "".join(p.text for p in arg.parts), "type": "string"}
    return {"e": "template", "parts": [p.text if p.kind == "lit" else part_expr(p) for p in arg.parts]}


def literal(arg: Arg | None) -> str | None:
    if arg is None or not all(p.kind == "lit" for p in arg.parts):
        return None
    return "".join(p.text for p in arg.parts)


# ------------------------------------------------------------------ the context


@dataclass
class Context:
    out: Out
    me: dict[str, Any]  # the script's Program
    path: str
    resolver: Resolver
    windows: bool = False
    names: dict[str, int] = field(default_factory=dict)
    expand_cmd: Callable[[Part], str | None] | None = None  # the value of a $(...) when certain

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path)

    def value(self, arg: Arg | None) -> str | None:
        """The argument's text when every part is literal or certain; None when it is dynamic."""
        if arg is None:
            return None
        out = []
        for p in arg.parts:
            if p.kind == "lit":
                out.append(p.text)
            elif p.kind == "var":
                if SCRIPT_DIR in p.text:
                    out.append(SCRIPT_DIR)
                    continue
                v = self.resolver.value(p.text, p.start, p.modifier)
                if v is None:
                    return None
                out.append(v)
            elif p.kind == "cmd" and self.expand_cmd is not None:
                v = self.expand_cmd(p)
                if v is None:
                    return None
                out.append(v)
            else:
                return None
        return "".join(out)

    def expanded(self, parts: list[Part], a: int, b: int) -> Expanded:
        """Parts spanning a..b as text: literals as written, certain variables by value, the rest
        by a placeholder that keeps its parameter name."""
        x = Expanded()
        pos = a
        n = 0
        for p in parts:
            if p.start > pos:
                x.add(self.out.text[pos:p.start], pos, p.start)
            if p.kind == "lit":
                x.add(p.text, p.start, p.end)
            else:
                v = self.value(Arg([p], p.start, p.end))
                name = param_name(p.text) if p.kind == "var" else f"cmd{n + 1}"
                if p.kind != "var":
                    n += 1
                if v is not None and SCRIPT_DIR not in v:
                    x.add(v, p.start, p.end, "value", name, part_expr(p))
                else:
                    x.add(f"{PLACEHOLDER}{len(x.pieces)}", p.start, p.end, "dynamic", name, part_expr(p))
            pos = max(pos, p.end)
        if pos < b:
            x.add(self.out.text[pos:b], pos, b)
        return x

    def task_name(self, label: str) -> str:
        n = self.names.get(label, 0) + 1
        self.names[label] = n
        return label if n == 1 else f"{label}~{n}"

    def file_candidates(self, value: str) -> list[str]:
        """Repository paths a file argument can name: relative to the script's directory and to
        the repository root. An absolute path, or one that leaves the repository, names none."""
        value = value.replace("\\", "/") if self.windows or "\\" in value else value
        if value.startswith(SCRIPT_DIR):
            rest = value[len(SCRIPT_DIR):].lstrip("/")
            p = posixpath.normpath(posixpath.join(self.dir, rest))
            return [] if p.startswith("..") or p == "." else [p]
        if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:/", value) or SCRIPT_DIR in value:
            return []
        out = []
        for base in (self.dir, ""):
            p = posixpath.normpath(posixpath.join(base, value))
            if not p.startswith("..") and p != "." and p not in out:
                out.append(p)
        return out


# ------------------------------------------------------------------ steps


def step(ctx: Context, cmd: Cmd, tool: str, construct: str, targets: list[dict[str, Any]], label: str, attrs: dict[str, Any] | None = None, dynamic: bool = False) -> dict[str, Any]:
    """A `Task` the script CONTAINS that RUNS each target; returns its `call` statement."""
    out = ctx.out
    name = ctx.task_name(label)
    task = out.term(cmd.start, cmd.end, "sh:ToolStep", {"framework": "shell", "job": ctx.path, "name": name}, name, {"tool": tool, **(attrs or {}), **({"dynamic": True} if dynamic else {})})
    out.rel(cmd.start, cmd.end, ctx.me, P.CONTAINS, task)
    for t in targets:
        out.rel(cmd.start, cmd.end, task, P.RUNS, t)
    out.construct(construct if not dynamic else "sh:run.dynamic", cmd.start, cmd.end, bool(targets))
    if dynamic:
        out.note(f"{tool} runs a target built at run time: the step is kept, what it runs is not resolved")
        out.unresolved += 1
    return {"op": "call", "at": out.at(cmd.start, cmd.end), "call": {"e": "call", "callee": {"e": "name", "name": tool, "kind": "program"},
                                                                      "args": [{"e": "lit", "value": label.split(":", 1)[1], "type": "string"}] if ":" in label else []},
            "task": name}


def file_step(ctx: Context, cmd: Cmd, tool: str, construct: str, arg: Arg | None, make_ref: Callable[[str], dict[str, Any]], attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    """A step that runs a file: RUNS the ref `make_ref` gives each repository path it can name."""
    value = ctx.value(arg)
    if value is None:
        return step(ctx, cmd, tool, construct, [], tool, attrs, dynamic=True)
    candidates = ctx.file_candidates(value)
    base = posixpath.basename(value.replace("\\", "/")) or tool
    if not candidates:
        ctx.out.note(f"{tool} runs {base} from outside the repository: not linked")
    return step(ctx, cmd, tool, construct, [make_ref(c) for c in candidates], f"{tool}:{base}", attrs)


def _options(args: list[Arg], ctx: Context, with_value: set[str], prefix: str = "-") -> tuple[dict[str, Arg | None], list[Arg]]:
    """Options (lower-cased) with their value argument, and the positional arguments."""
    opts: dict[str, Arg | None] = {}
    positional: list[Arg] = []
    i = 0
    while i < len(args):
        text = literal(args[i])
        if text is not None and text.startswith(tuple(prefix)) and len(text) > 1:
            key, eq, inline = text.partition("=")
            key = key.lower()
            if eq:
                opts[key] = Arg([Part("lit", inline, args[i].start + len(key) + 1, args[i].end)], args[i].start + len(key) + 1, args[i].end)
            elif key in with_value and i + 1 < len(args):
                opts[key] = args[i + 1]
                i += 1
            else:
                opts[key] = None
        else:
            positional.append(args[i])
        i += 1
    return opts, positional


# ------------------------------------------------------------------ tools


_SPARK_VALUE = {"--class", "--master", "--deploy-mode", "--conf", "-c", "--name", "--jars", "--py-files", "--files", "--driver-memory", "--executor-memory", "--num-executors",
                "--executor-cores", "--queue", "--packages", "--properties-file", "--keytab", "--principal", "--archives", "--driver-class-path", "--total-executor-cores",
                "--driver-cores", "--exclude-packages", "--repositories", "--driver-java-options", "--driver-library-path", "--proxy-user"}
_DSJOB_VALUE = {"-mode", "-param", "-warn", "-rows", "-opmetadata", "-queue", "-server", "-user", "-password", "-domain", "-file", "-useid"}
_PMCMD_VALUE = {"-sv", "-service", "-d", "-domain", "-u", "-user", "-p", "-password", "-uv", "-pv", "-usd", "-f", "-folder", "-paramfile", "-rin", "-osprofile", "-localparamfile", "-wfrunid", "-runinsname", "-timeout", "-hostname", "-port"}
_DTEXEC_VALUE = {"/f", "/file", "/sq", "/sql", "/is", "/isserver", "/dts", "/set", "/conf", "/configfile", "/conn", "/connection", "/de", "/decrypt", "/par", "/parameter",
                 "/ser", "/server", "/u", "/user", "/p", "/password", "/l", "/logger", "/vl", "/va", "/rep", "/reporting", "/x86", "/env", "/envreference", "/m", "/maxconcurrent",
                 "-f", "-file", "-sql", "-isserver", "-set", "-conn", "-de", "-par", "-ser", "-u", "-p"}


def _tool(ctx: Context, cmd: Cmd, tool: str) -> dict[str, Any] | None:
    construct = TOOLS[tool]
    base = tool.removesuffix(".exe")
    if base in ("sas", "sasbatch"):
        opts, pos = _options(cmd.args, ctx, {"-sysin", "-log", "-print", "-config", "-autoexec", "-sysparm", "-work", "-altlog", "-altprint", "-set", "-initstmt", "-termstmt", "-logparm"})
        target = opts.get("-sysin") or next((a for a in pos if (literal(a) or "").lower().endswith(".sas") or (ctx.value(a) or "").lower().endswith(".sas")), None)
        if target is None:
            return step(ctx, cmd, "sas", construct, [], "sas", dynamic=True)
        return file_step(ctx, cmd, "sas", construct, target, lambda p: program_ref("sas", p, declare=False))
    if base in ("spark-submit", "spark2-submit", "pyspark"):
        opts, pos = _options(cmd.args, ctx, _SPARK_VALUE)
        main = literal(opts.get("--class"))
        return file_step(ctx, cmd, "spark-submit", construct, pos[0] if pos else None, artifact_ref, {"main_class": main})
    if base in ("python", "python2", "python3"):
        opts, pos = _options(cmd.args, ctx, {"-m", "-c", "-W", "-X"})
        if "-m" in opts and opts["-m"] is not None:
            module = ctx.value(opts["-m"])
            if module is None or not re.fullmatch(r"[\w.]+", module):
                return step(ctx, cmd, "python", construct, [], "python", dynamic=True)
            stem = module.replace(".", "/")
            return step(ctx, cmd, "python", construct, [artifact_ref(f"{stem}.py"), artifact_ref(f"{stem}/__main__.py")], f"python:{module}")
        if "-c" in opts:
            return None  # inline code: a command, not a step
        return file_step(ctx, cmd, "python", construct, pos[0] if pos else None, artifact_ref)
    if base == "dsjob":
        opts, pos = _options(cmd.args, ctx, _DSJOB_VALUE)
        if "-run" not in opts:
            ctx.out.construct(construct, cmd.start, cmd.end, False)
            return None  # -jobinfo, -logsum and the like ask about a job; they do not run one
        project, job = (ctx.value(pos[-2]) if len(pos) >= 2 else None), (ctx.value(pos[-1]) if pos else None)
        if not job or not re.fullmatch(r"[\w.$-]+", job):
            return step(ctx, cmd, "dsjob", construct, [], "dsjob", dynamic=True)
        name = job.split(".", 1)[0]  # job.invocation
        ref = {"kind": "Job", "natural_key": {"framework": "datastage", "name": name}, "name": name, "declare": False}
        return step(ctx, cmd, "dsjob", construct, [ref], f"dsjob:{name}", {"project": project, "invocation": job.split(".", 1)[1] if "." in job else None})
    if base == "pmcmd":
        sub = (literal(cmd.args[0]) or "").lower() if cmd.args else ""
        if sub != "startworkflow":
            ctx.out.construct(construct, cmd.start, cmd.end, False)
            return None
        opts, pos = _options(cmd.args[1:], ctx, _PMCMD_VALUE)
        wf = ctx.value(pos[-1]) if pos else None
        if not wf or not re.fullmatch(r"[\w.$-]+", wf):
            return step(ctx, cmd, "pmcmd", construct, [], "pmcmd", dynamic=True)
        folder = ctx.value(opts.get("-f") or opts.get("-folder"))
        ref = {"kind": "Job", "natural_key": {"framework": "informatica", "name": wf}, "name": wf, "declare": False}
        return step(ctx, cmd, "pmcmd", construct, [ref], f"pmcmd:{wf}", {"folder": folder})
    if base == "dtexec":
        opts, _ = _options(cmd.args, ctx, _DTEXEC_VALUE, prefix="/-")
        file_arg = opts.get("/f") or opts.get("/file") or opts.get("-f") or opts.get("-file")
        if file_arg is not None:
            return file_step(ctx, cmd, "dtexec", construct, file_arg, artifact_ref)
        stored = opts.get("/sql") or opts.get("/sq") or opts.get("/isserver") or opts.get("/is") or opts.get("-sql") or opts.get("-isserver")
        value = ctx.value(stored)
        if value is None:
            return step(ctx, cmd, "dtexec", construct, [], "dtexec", dynamic=True)
        package = re.split(r"[\\/]", value.strip("\"'"))[-1]
        package = re.sub(r"(?i)\.dtsx$", "", package)
        ref = {"kind": "Job", "natural_key": {"framework": "ssis", "name": package}, "name": package, "declare": False}
        return step(ctx, cmd, "dtexec", construct, [ref], f"dtexec:{package}", {"catalog_path": value.replace("\\", "/")})
    if base == "tbuild":
        opts, _ = _options(cmd.args, ctx, {"-f", "-v", "-u", "-j", "-l", "-c", "-r", "-z", "-h", "-s", "-o", "-t", "-n", "-m", "-p", "-e", "-x"})
        return file_step(ctx, cmd, "tbuild", construct, opts.get("-f"), lambda p: program_ref("teradata", p, declare=False))
    if base in ("fastload", "mload", "fexp", "tpump"):
        if cmd.stdin is not None:
            ctx.out.construct(construct, cmd.start, cmd.end, False)
            ctx.out.note(f"{base} reads a script from a here-document: utility scripts are read from their own files")
            return None
        if base == "tpump":
            ctx.out.construct(construct, cmd.start, cmd.end, False)
            return None
        return file_step(ctx, cmd, base, construct, cmd.stdin_file, lambda p: program_ref("teradata", p, declare=False))
    if base in ("sh", "bash", "ksh", "zsh", "cmd", "powershell", "pwsh"):
        if base == "cmd":
            opts, pos = _options(cmd.args, ctx, set(), prefix="/")
        elif base in ("powershell", "pwsh"):
            opts, pos = _options(cmd.args, ctx, {"-file", "-command", "-executionpolicy", "-ep", "-c", "-f"})
            pos = [opts[k] for k in ("-file", "-f") if opts.get(k) is not None] or ([] if ("-command" in opts or "-c" in opts) else pos)
        else:
            opts, pos = _options(cmd.args, ctx, {"-o", "+o"})
            if "-c" in opts:
                return None
        if not pos:
            return None
        return file_step(ctx, cmd, base, construct, pos[0], lambda p: program_ref(PLATFORM, p, declare=False))
    ctx.out.construct(construct, cmd.start, cmd.end, False)  # java, hadoop, yarn, sqoop: counted
    return None


# ------------------------------------------------------------------ SQL clients


_SQLPLUS_COMMANDS = re.compile(r"(?im)^[ \t]*(?:set|spool|whenever|exit|quit|prompt|col(?:umn)?|ttitle|btitle|break|compute|conn(?:ect)?|def(?:ine)?|undef(?:ine)?|var(?:iable)?|print|pause|host|show|timing|clear|rem(?:ark)?|store|desc(?:ribe)?|sho)\b[^\n]*|^[ \t]*/[ \t]*$|^[ \t]*!\S[^\n]*")
_SQLPLUS_RUN = re.compile(r"(?m)^[ \t]*@@?([^\s;]+)")
_SQLPLUS_EXEC = re.compile(r"(?im)^[ \t]*exec(?:ute)?\s+((?:[\w$#]+\.){0,2}[\w$#]+)")
_BACKSLASH = re.compile(r"(?m)^[ \t]*\\[a-z!][^\n]*")
_DB2_CLP = re.compile(r"(?im)^[ \t]*(?:connect\s+(?:to|reset)\b|terminate\b|quit\b|disconnect\b|list\b|get\b|update\s+(?:db|database|dbm)\s+cfg\b|runstats\b|reorg\b|set\s+(?:schema|current|integrity|path)\b)[^;\n]*;?")
_DB2_EXPORT = re.compile(r"(?is)\bexport\s+to\s+(\S+)\s+of\s+\w+(.*?)\b(select\b.*|with\b.*)$")
_DB2_LOAD = re.compile(r"(?is)\b(load|import)\s+(?:client\s+)?from\s+(\S+)\s+of\s+\w+.*?\b(?:insert|replace|insert_update|merge|restart|terminate)\s+into\s+((?:\"?[\w$#@]+\"?\.)?\"?[\w$#@]+\"?)")
_BTEQ_FILE = re.compile(r"(?im)^[ \t]*\.(export|import)\b[^;\n]*?\bfile\s*=\s*([^\s,;]+)")
_HIVE_SET = re.compile(r"(?im)^[ \t]*(?:set\s+[\w.:-]+\s*=[^;\n]*;?|add\s+(?:jar|file|archive)[^;\n]*;?|use\s+\w+\s*;?)")
_SQLCMD = re.compile(r"(?im)^[ \t]*(?::\w+[^\n]*|go(?:\s+\d+)?\s*)$")
_CALL = re.compile(r"(?is)^\s*(?:call|exec(?:ute)?)\s+((?:[\"\[\]\w$#@]+\.){0,2}[\"\[\]\w$#@]+)\s*(\(|$|\s)")
_ENGINE_PLATFORM = {"oracle": "oracle", "teradata": "teradata", "db2": "db2", "netezza": "netezza", "tsql": "tsql", "sybase": "sybase", "hive": "hive", "impala": "hive"}


def _blank(text: str, pattern: re.Pattern) -> str:
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def sql_text(ctx: Context, cmd: Cmd, tool: str, x: Expanded) -> list[dict[str, Any]]:
    """The SQL a client receives as text `x`: lineage, procedure calls, files it moves, and one
    `query` node per statement inside a platform node named for the client."""
    out = ctx.out
    construct, dialect, engine = SQL_CLIENTS[tool]
    text = x.text
    body: list[dict[str, Any]] = []
    sql = _sql_module()

    def client(pattern: re.Pattern) -> None:
        nonlocal text
        for m in pattern.finditer(text):
            if m.group().strip():
                a, b = x.src_span(m.start(), m.end())
                out.construct("sh:sql.client-command", a, b, False)
        text = _blank(text, pattern)

    if tool == "sqlplus":
        for m in _SQLPLUS_RUN.finditer(text):
            a, b = x.src_span(m.start(1), m.end(1))
            body.append(_client_file(ctx, cmd, tool, m.group(1), x.dynamic_in(m.start(1), m.end(1)), a, b))
        for m in _SQLPLUS_EXEC.finditer(text):
            a, b = x.src_span(m.start(1), m.end(1))
            body.append(_procedure_call(ctx, engine, m.group(1), a, b))
        text = _blank(text, _SQLPLUS_RUN)
        text = _blank(text, _SQLPLUS_EXEC)
        client(_SQLPLUS_COMMANDS)
    elif tool == "bteq":
        for m in _BTEQ_FILE.finditer(text):
            a, b = x.src_span(m.start(2), m.end(2))
            body += _file_move(ctx, P.WRITES_TO if m.group(1).lower() == "export" else P.READS_FROM, m.group(2), x.dynamic_in(m.start(2), m.end(2)), a, b, "sh:sql.bteq-file", "bteq")
        for m in re.finditer(r"(?im)^[ \t]*\.[a-z][^\n]*", text):
            a, b = x.src_span(m.start(), m.end())
            out.construct("sh:sql.client-command", a, b, False)
        text = sql.scrub_commands(text)
    elif tool in ("psql", "nzsql"):
        client(_BACKSLASH)
    elif tool in ("db2", "clpplus"):
        client(_DB2_CLP)
    elif tool in ("hive", "beeline", "impala-shell"):
        client(_HIVE_SET)
    elif tool in ("sqlcmd", "osql", "isql"):
        text = _blank(text, _SQLCMD) if tool != "isql" else sql.scrub_commands(text)
    from repo_intel.extractors.contracts_data import split_statements

    statements = split_statements(text)
    for s, e in statements:
        chunk = text[s:e]
        if not chunk.strip():
            continue
        a, b = x.src_span(s, e)
        m = _CALL.match(chunk)
        if m and not re.match(r"(?is)\s*exec(?:ute)?\s+immediate\b", chunk):
            name_a, name_b = x.src_span(s + m.start(1), s + m.end(1))
            if x.dynamic_in(s + m.start(1), s + m.end(1)):
                out.note(f"{tool} calls a procedure named at run time: not resolved")
                out.unresolved += 1
                body.append({"op": "call", "at": out.at(a, b), "call": {"e": "call", "callee": {"e": "platform", "construct": "sql.dynamic"}}})
            else:
                body.append(_procedure_call(ctx, engine, m.group(1), name_a, name_b))
            continue
        if tool in ("db2", "clpplus"):
            moved = _db2_move(ctx, x, chunk, s, dialect)
            if moved is not None:
                body += moved
                continue
        tx = re.match(r"(?is)\s*(commit|rollback|begin\s+(?:tran|transaction|work)\b|start\s+transaction|bt|et)\b(?:\s+(?:work|tran|transaction))?\s*;?\s*$", chunk)
        if tx:
            action = tx.group(1).lower().split()[0]
            body.append({"op": "transaction", "at": out.at(a, b), "action": {"bt": "begin", "et": "commit", "start": "begin"}.get(action, action), "body": []})
            continue
        found = sql.lineage(text[s:e], dialect)
        _lineage(ctx, x, found, s, tool)
        query_sql, params = x.sql(s, e, text)
        if query_sql:
            node: dict[str, Any] = {"op": "query", "at": out.at(a, b), "sql": query_sql, "params": params, "engine": engine}
            body.append(node)
    out.construct(construct, cmd.start, cmd.end, True)
    if any(p.kind == "dynamic" for p in x.pieces):
        out.construct("sh:sql.dynamic", cmd.start, cmd.end, False)
    return [{"op": "platform", "at": out.at(cmd.start, cmd.end), "construct": construct, "detail": {"engine": engine}, "body": body}]


def _sql_module():
    from repo_intel.extractors import sql_lineage

    return sql_lineage


def _lineage(ctx: Context, x: Expanded, found, offset: int, tool: str) -> None:
    out = ctx.out
    for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
        for name, a, b in items:
            if PLACEHOLDER in name or x.dynamic_in(offset + a, offset + b):
                out.note(f"{tool} SQL names a table through a variable whose value is not in the file: the table is not resolved")
                out.unresolved += 1
                continue
            sa, sb = x.src_span(offset + a, offset + b)
            out.table(sa, sb, ctx.me, predicate, name)
    out.unresolved += found.unresolved


def _procedure_call(ctx: Context, engine: str, raw: str, a: int, b: int) -> dict[str, Any]:
    from repo_intel.extractors.sql_lineage import clean_name

    name = clean_name(raw)
    platform = _ENGINE_PLATFORM.get(engine)
    if platform and PLACEHOLDER not in name:
        ctx.out.rel(a, b, ctx.me, P.CALLS, program_ref(platform, name, declare=False))
    return {"op": "call", "at": ctx.out.at(a, b), "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}}}


def _client_file(ctx: Context, cmd: Cmd, tool: str, raw: str, dynamic: bool, a: int, b: int) -> dict[str, Any]:
    sub = Cmd(tool, None, [], a, b)
    if dynamic or PLACEHOLDER in raw:
        return step(ctx, sub, tool, "sh:sql.client-file", [], tool, dynamic=True)
    value = raw if "." in posixpath.basename(raw) else raw + ".sql"
    if raw.startswith(SCRIPT_DIR):
        value = raw
    arg = Arg([Part("lit", value, a, b)], a, b)
    return file_step(ctx, sub, tool, "sh:sql.client-file", arg, artifact_ref)


def _file_move(ctx: Context, predicate, raw: str, dynamic: bool, a: int, b: int, construct: str, tool: str) -> list[dict[str, Any]]:
    out = ctx.out
    raw = raw.strip("'\"")
    if dynamic or PLACEHOLDER in raw or SCRIPT_DIR in raw:
        out.construct("sh:transfer.dynamic", a, b, False)
        out.note(f"{tool} moves a file named at run time: the dataset is not resolved")
        out.unresolved += 1
        return []
    out.dataset(a, b, ctx.me, predicate, raw)
    out.construct(construct, a, b, True)
    return [{"op": "output" if predicate == P.WRITES_TO else "platform", "at": out.at(a, b), **({"channel": "file", "value": {"e": "lit", "value": raw, "type": "string"}} if predicate == P.WRITES_TO else {"construct": construct, "detail": {"reads": raw}})}]


def _db2_move(ctx: Context, x: Expanded, chunk: str, offset: int, dialect: str | None) -> list[dict[str, Any]] | None:
    sql = _sql_module()
    m = _DB2_EXPORT.search(chunk)
    if m:
        a, b = x.src_span(offset + m.start(1), offset + m.end(1))
        nodes = _file_move(ctx, P.WRITES_TO, m.group(1), x.dynamic_in(offset + m.start(1), offset + m.end(1)), a, b, "sh:sql.db2-export", "db2")
        q = offset + m.start(3)
        found = sql.lineage(chunk[m.start(3):], dialect)
        _lineage(ctx, x, found, q, "db2")
        query_sql, params = x.sql(q, offset + len(chunk))
        qa, qb = x.src_span(q, offset + len(chunk))
        return [{"op": "query", "at": ctx.out.at(qa, qb), "sql": query_sql, "params": params, "engine": "db2"}] + nodes
    m = _DB2_LOAD.search(chunk)
    if m:
        a, b = x.src_span(offset + m.start(2), offset + m.end(2))
        nodes = _file_move(ctx, P.READS_FROM, m.group(2), x.dynamic_in(offset + m.start(2), offset + m.end(2)), a, b, "sh:sql.db2-load", "db2")
        ta, tb = x.src_span(offset + m.start(3), offset + m.end(3))
        if x.dynamic_in(offset + m.start(3), offset + m.end(3)):
            ctx.out.note("db2 loads a table named at run time: not resolved")
            ctx.out.unresolved += 1
        else:
            ctx.out.table(ta, tb, ctx.me, P.WRITES_TO, sql.clean_name(m.group(3)))
        return nodes + [{"op": "platform", "at": ctx.out.at(ta, tb), "construct": "sh:sql.db2-load", "detail": {"utility": m.group(1).lower(), "table": sql.clean_name(m.group(3))}}]
    return None


def _inline_sql(ctx: Context, cmd: Cmd, tool: str) -> Arg | None:
    """The SQL given as an option or argument: `-e`, `-c`, `-q`, `-Q`, `--query`, or a `db2` statement."""
    keys = {"hive": ("-e",), "beeline": ("-e",), "impala-shell": ("-q", "--query"), "psql": ("-c", "--command"), "nzsql": ("-c",),
            "sqlcmd": ("-q", "-Q"), "osql": ("-q", "-Q"), "mysql": ("-e", "--execute"), "snowsql": ("-q", "--query")}.get(tool, ())
    for i, a in enumerate(cmd.args):
        text = literal(a) or ""
        for k in keys:
            if text == k and i + 1 < len(cmd.args):
                return cmd.args[i + 1]
            if text.startswith(k + "=") and len(text) > len(k) + 1:
                return _tail(a, len(k) + 1)
    return None


def _tail(arg: Arg, n: int) -> Arg:
    first = arg.parts[0]
    parts = [Part("lit", first.text[n:], first.start + n, first.end, quoted=first.quoted)] + arg.parts[1:]
    return Arg(parts, arg.start + n, arg.end)


def _script_file(ctx: Context, cmd: Cmd, tool: str) -> Arg | None:
    if tool == "sqlplus":
        for a in cmd.args:
            text = literal(a)
            if text is not None and text.startswith("@") and len(text) > 1:
                return _tail(a, 2 if text.startswith("@@") else 1)
            if a.parts and a.parts[0].kind == "lit" and a.parts[0].text.startswith("@") and len(a.parts) > 1:
                return _tail(a, 1)
        return None
    keys = {"psql": ("-f", "--file"), "nzsql": ("-f",), "hive": ("-f",), "beeline": ("-f",), "impala-shell": ("-f", "--query_file"),
            "sqlcmd": ("-i",), "osql": ("-i",), "isql": ("-i",), "snowsql": ("-f", "--filename"), "clpplus": ()}.get(tool, ())
    for i, a in enumerate(cmd.args):
        text = literal(a) or ""
        if tool == "db2" and re.fullmatch(r"-[a-z]*f", text) and i + 1 < len(cmd.args):
            return cmd.args[i + 1]
        if text in keys and i + 1 < len(cmd.args):
            return cmd.args[i + 1]
    return cmd.stdin_file if tool in ("bteq", "mysql", "sqlplus", "isql", "nzsql", "psql", "db2") else None


def _sql_client(ctx: Context, cmd: Cmd, tool: str) -> list[dict[str, Any]]:
    construct = SQL_CLIENTS[tool][0]
    nodes: list[dict[str, Any]] = []
    script = _script_file(ctx, cmd, tool)
    if script is not None:
        nodes.append(file_step(ctx, cmd, tool, "sh:sql.client-file", script, artifact_ref))
        ctx.out.construct(construct, cmd.start, cmd.end, True)
    inline = _inline_sql(ctx, cmd, tool)
    if inline is None and tool == "db2" and script is None and cmd.stdin is None:
        words = [a for a in cmd.args if not (literal(a) or "").startswith("-")]
        if words:
            inline = _joined(words)
    if inline is not None and inline.parts:
        nodes += sql_text(ctx, cmd, tool, ctx.expanded(inline.parts, inline.parts[0].start if inline.parts else inline.start, inline.parts[-1].end if inline.parts else inline.end))
    if cmd.stdin is not None:
        nodes += sql_text(ctx, cmd, tool, cmd.stdin)
    if not nodes:
        ctx.out.construct(construct, cmd.start, cmd.end, False)
    return nodes


def _joined(words: list[Arg]) -> Arg:
    """`db2 connect to x` and `db2 select ... from t`: the words as one statement, separated by the source's spaces."""
    parts: list[Part] = []
    for i, w in enumerate(words):
        if i:
            gap_a = words[i - 1].end
            parts.append(Part("lit", " ", gap_a, w.start))
        parts += w.parts
    return Arg(parts, words[0].start, words[-1].end)


# ------------------------------------------------------------------ transfers


_REMOTE = re.compile(r"^(?:[^@/\s:]+@)?(\[[^\]]+\]|[A-Za-z0-9][\w.-]*):(.*)$")


def _remote_name(protocol: str, path: str) -> str:
    return f"{protocol}:{path if path.startswith('/') else '~/' + path if path else '~'}"


def _transfer(ctx: Context, cmd: Cmd, tool: str) -> list[dict[str, Any]]:
    out = ctx.out
    construct = TRANSFERS[tool]
    base = tool.removesuffix(".exe")
    reads: list[str] = []
    writes: list[str] = []
    dynamic = False

    def side(value: str | None, remote_protocol: str | None, predicate) -> None:
        nonlocal dynamic
        if value is None or SCRIPT_DIR in value:
            dynamic = True
            return
        name = _remote_name(remote_protocol, value) if remote_protocol else value
        out.dataset(cmd.start, cmd.end, ctx.me, predicate, name, {"transfer": base, **({"remote": True} if remote_protocol else {})})
        (writes if predicate == P.WRITES_TO else reads).append(name)

    if base in ("scp", "pscp"):
        _, pos = _options(cmd.args, ctx, {"-P", "-i", "-o", "-F", "-c", "-l", "-S", "-J", "-pw"})
        if len(pos) < 2:
            out.construct(construct, cmd.start, cmd.end, False)
            return []
        *sources, dest = pos
        dest_remote = _remote_spec(ctx, dest)
        for s in sources:
            s_remote = _remote_spec(ctx, s)
            if s_remote is not None:
                side(s_remote, "scp", P.READS_FROM)
            else:
                side(ctx.value(s), None, P.READS_FROM)
        if dest_remote is not None:
            side(dest_remote, "scp", P.WRITES_TO)
        else:
            side(ctx.value(dest), None, P.WRITES_TO)
    elif base in ("ftp", "sftp"):
        script = cmd.stdin
        if script is None:
            _, pos = _options(cmd.args, ctx, {"-P", "-i", "-o", "-F", "-b", "-c", "-l", "-s", "-D", "-R", "-B"})
            remote = [r for r in (_remote_spec(ctx, a) for a in pos) if r is not None]
            if base == "sftp" and remote:  # sftp host:/path [local]: a get
                side(remote[0], "sftp", P.READS_FROM)
                local = ctx.value(pos[-1]) if len(pos) > 1 else "."
                if local is not None:
                    side(posixpath.join(local, posixpath.basename(remote[0])) if local in (".", "") or local.endswith("/") else local, None, P.WRITES_TO)
            else:
                out.construct(construct, cmd.start, cmd.end, False)
                out.note(f"{base} reads its commands from a file or a terminal: what it moves is not read")
                return []
        else:
            rcwd, lcwd = "", ""
            for m in re.finditer(r"(?im)^[ \t]*(l?cd|m?put|m?get|reput|reget)[ \t]+([^\n]*)$", script.text):
                verb = m.group(1).lower()
                words = m.group(2).split()
                if not words:
                    continue
                if script.dynamic_in(m.start(2), m.end(2)) or any(PLACEHOLDER in w for w in words):
                    dynamic = True
                    continue
                if verb == "cd":
                    rcwd = posixpath.join(rcwd, words[0]) if rcwd else words[0]
                    continue
                if verb == "lcd":
                    lcwd = posixpath.join(lcwd, words[0]) if lcwd else words[0]
                    continue
                files = words if verb.startswith("m") else words[:1]
                other = words[1] if len(words) > 1 and not verb.startswith("m") else None
                for f in files:
                    if verb.endswith("put"):
                        local = posixpath.join(lcwd, f) if lcwd and not f.startswith("/") else f
                        remote = other or posixpath.basename(f)
                        side(local, None, P.READS_FROM)
                        side(posixpath.join(rcwd, remote) if rcwd and not remote.startswith("/") else remote, base, P.WRITES_TO)
                    else:
                        remote = posixpath.join(rcwd, f) if rcwd and not f.startswith("/") else f
                        local = other or posixpath.basename(f)
                        side(remote, base, P.READS_FROM)
                        side(posixpath.join(lcwd, local) if lcwd and not local.startswith("/") else local, None, P.WRITES_TO)
    elif base in ("direct", "ndmcli"):
        script = cmd.stdin
        if script is None:
            out.construct(construct, cmd.start, cmd.end, False)
            return []
        for m in re.finditer(r"(?is)\bcopy\s+from\s*\((.*?)\)\s*(?:ckpt\s*=\s*\S+\s*)?(?:compress\b[^)]*?\s*)?to\s*\((.*?)\)", script.text):
            for group, default_remote in ((1, False), (2, True)):
                body = m.group(group)
                f = re.search(r"(?i)\b(?:file|dsn)\s*=\s*([^\s)]+)", body)
                if not f:
                    continue
                if script.dynamic_in(m.start(group) + f.start(1), m.start(group) + f.end(1)):
                    dynamic = True
                    continue
                remote = bool(re.search(r"(?i)\bsnode\b", body)) or (default_remote and not re.search(r"(?i)\bpnode\b", body))
                side(f.group(1).strip("'\""), "cd" if remote else None, P.WRITES_TO if group == 2 else P.READS_FROM)
    else:
        out.construct(construct, cmd.start, cmd.end, False)  # rsync, curl, wget, cloud and hdfs copies: counted
        return []
    if dynamic:
        out.construct("sh:transfer.dynamic", cmd.start, cmd.end, False)
        out.note(f"{base} moves a file named at run time: that dataset is not resolved")
        out.unresolved += 1
    out.construct(construct, cmd.start, cmd.end, bool(reads or writes))
    return [{"op": "platform", "at": out.at(cmd.start, cmd.end), "construct": construct, "detail": {"reads": sorted(set(reads)), "writes": sorted(set(writes))}}]


def _remote_spec(ctx: Context, arg: Arg) -> str | None:
    """The path of a `[user@]host:path` argument (the host is dropped), else None."""
    first = arg.parts[0] if arg.parts else None
    if first is None:
        return None
    if first.kind == "lit":
        m = _REMOTE.match(first.text)
        if m and not re.match(r"^[A-Za-z]:[\\/]", first.text):
            if all(p.kind == "lit" for p in arg.parts[1:]):
                return m.group(2) + "".join(p.text for p in arg.parts[1:])
            return ctx.value(Arg([Part("lit", m.group(2), first.start, first.end)] + arg.parts[1:], arg.start, arg.end))
        return None
    # $HOST:/path or $USER@$HOST:/path: a variable host followed by `:path`
    for i, p in enumerate(arg.parts):
        if p.kind == "lit" and ":" in p.text and not p.text.startswith("/"):
            head, _, tail = p.text.partition(":")
            if "/" in head:
                return None
            rest = Arg([Part("lit", tail, p.start, p.end)] + arg.parts[i + 1:], arg.start, arg.end)
            return ctx.value(rest)
    return None


# ------------------------------------------------------------------ the command


def interpret(ctx: Context, cmd: Cmd) -> list[dict[str, Any]]:
    """The behaviour statements standing for `cmd`, after emitting what it says."""
    out = ctx.out
    name = cmd.name
    base = posixpath.basename(name.replace("\\", "/"))
    key = base.lower() if ctx.windows else base
    if key in SQL_CLIENTS:
        nodes = _sql_client(ctx, cmd, key)
        return nodes or [_generic(ctx, cmd)]
    if key in TOOLS and not (key in ("sh", "bash", "ksh", "zsh") and not cmd.args):
        node = _tool(ctx, cmd, key)
        return [node] if node is not None else [_generic(ctx, cmd)]
    if key in TRANSFERS or key.removesuffix(".exe") in TRANSFERS:
        nodes = _transfer(ctx, cmd, key if key in TRANSFERS else key.removesuffix(".exe"))
        return nodes or [_generic(ctx, cmd)]
    if key.lower() in MAILERS:
        out.construct("sh:mail", cmd.start, cmd.end, True)
        return [{"op": "output", "at": out.at(cmd.start, cmd.end), "channel": "mail"}]
    if "/" in name.replace("\\", "/") or key.lower().endswith(SCRIPT_SUFFIXES):
        if key.lower().endswith(SCRIPT_SUFFIXES):  # ./other.sh or $DIR/other.sh run by path
            return [file_step(ctx, cmd, posixpath.splitext(key)[1].lstrip(".") or "sh", "sh:run.script", cmd.name_arg, lambda p: program_ref(PLATFORM, p, declare=False))]
    return [_generic(ctx, cmd)]


def _generic(ctx: Context, cmd: Cmd) -> dict[str, Any]:
    out = ctx.out
    out.construct("sh:command", cmd.start, cmd.end, True)
    callee = {"e": "name", "name": posixpath.basename(cmd.name.replace("\\", "/")), "kind": "command"}
    sensitive = bool(_SENSITIVE.search(cmd.name)) or cmd.name.lower() in TRANSFERS or cmd.name.lower() in SQL_CLIENTS
    return {"op": "call", "at": out.at(cmd.start, cmd.end), "call": {"e": "call", "callee": callee, "args": [arg_expr(a, sensitive) for a in cmd.args[:20]]}}


__all__ = ["Arg", "Cmd", "Context", "Resolver", "SCRIPT_DIR", "arg_expr", "interpret", "literal", "part_expr", "step", "var_expr"]
