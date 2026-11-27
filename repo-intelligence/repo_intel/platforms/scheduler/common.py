"""What the scheduler readers share: decoding, spans, emission and what a command starts.

A reader works on the decoded text and gives spans as character offsets; `Emitter`
turns them into byte spans of the file, so a UTF-16 Task Scheduler export is cited as
exactly as a UTF-8 crontab.

**What a job starts.** A scheduler names the JCL member, script or program a job runs.
None of them is declared here: each is a reference that links only when the repository
defines it.

- A JCL member is the JCL `Job` of that name (`{framework: "jcl", name}`, as
  `legacy.jcl` keys it by the JOB statement). Only CA-7 requires the job name to be the
  JOB statement's; for a member named by Control-M or AutoSys the link rests on the
  usual convention that a member's JOB statement carries the member's name, so it is
  evidence E4 with `via: "jcl_member"`.
- A script is named by the path it has on the scheduler's machine
  (`/opt/batch/scripts/load_orders.sh`), which is not a repository path. The relation
  points at the `Artifact` of every suffix of that path (`opt/batch/scripts/load_orders.sh`,
  `batch/scripts/load_orders.sh`, `scripts/load_orders.sh`, `load_orders.sh`), each a
  non-declaring reference, so only a file the repository holds at one of those paths is
  linked, at evidence E4 with `via: "path_suffix"`. When the extension is one another pack
  reads (`.sh`, `.ksh`, `.bash`, `.bat`, `.cmd`, `.ps1` as `shell`; `.hql`, `.q` as `hive`;
  `.pig` as `pig`; `.sas` as `sas`), each candidate also gets a RUNS to the non-declaring
  `Program {platform, name: <candidate>}`, at the same evidence. A path with a variable in it
  (`$HOME`, `%%LIB`, `%TEMP%`) is dynamic: nothing is linked and a diagnostic says so.
- A bare program name (`dsjob`, `pmcmd`, `mail`) is a tool on the machine's path: it is
  kept in `attributes.programs` and not linked.

Only the program and the script it runs are read from a command, never its arguments,
which may hold dates, user names or passwords. A UNC path's host is dropped.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1
E4 = EvidenceClass.E4

NAME_SAFE = re.compile(r"^[\w .#@$&+:/\\^~%!-]{1,200}$")
_DYNAMIC = re.compile(r"[$%{}`*?]")
_DSN = re.compile(r"^[A-Za-z#@$][A-Za-z0-9#@$-]{0,7}(\.[A-Za-z#@$][A-Za-z0-9#@$-]{0,7})+$")
MEMBER = re.compile(r"^[A-Z#@$][A-Z0-9#@$]{0,7}$")

SCRIPT_EXTENSIONS = frozenset({
    ".sh", ".ksh", ".bash", ".csh", ".zsh", ".pl", ".py", ".rb", ".php", ".js", ".sas", ".ps1", ".bat", ".cmd",
    ".vbs", ".jar", ".r", ".sql", ".hql", ".pig", ".scala", ".jcl", ".exe", ".com", ".awk",
})
# The Program platform the pack that reads a script declares it under, by extension: `shell`
# for shell, batch and PowerShell scripts, `hive` and `pig` for the Hadoop pack's scripts
# (`.q` is read as HiveQL only when that pack recognises it, so the link may not resolve),
# `sas` for SAS programs. Each is named by its repository path.
SCRIPT_PLATFORMS = {
    ".sh": "shell", ".ksh": "shell", ".bash": "shell", ".bat": "shell", ".cmd": "shell", ".ps1": "shell",
    ".hql": "hive", ".q": "hive", ".pig": "pig", ".sas": "sas",
}
INTERPRETERS = frozenset({
    "sh", "bash", "ksh", "zsh", "csh", "tcsh", "dash", "perl", "python", "python2", "python3", "ruby", "php", "node",
    "rscript", "sas", "powershell", "pwsh", "cscript", "wscript", "java", "sqlplus", "awk", "hive", "pig", "beeline",
})
# The option after which an interpreter names its script.
_SCRIPT_OPTION = {"powershell": ("-file", "-f"), "pwsh": ("-file", "-f"), "sas": ("-sysin",), "java": ("-jar",), "hive": ("-f",), "beeline": ("-f",), "pig": ("-f",)}
_WRAPPERS = frozenset({"nohup", "exec", "time", "nice", "env", "timeout", "sudo", "ionice", "start", "call"})
_WRAPPER_ARGS = {"nice": ("-n",), "timeout": (), "sudo": ("-u", "-g", "-h", "-p"), "ionice": ("-c", "-n"), "start": ("/d",)}
_BUILTINS = frozenset({
    "cd", ".", "source", "export", "set", "unset", "umask", "ulimit", "echo", "[", "[[", "test", "true", "false", ":", "sleep",
    "mkdir", "rm", "touch", "trap", "if", "then", "fi", "else", "for", "do", "done", "while", "exit", "wait", "rem", "@echo", "setlocal", "endlocal",
})
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")


def decode(data: bytes) -> tuple[str, str, int]:
    """(text, codec, length of the byte order mark)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8", 3
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le", 2
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be", 2
    try:
        return data.decode("utf-8"), "utf-8", 0
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", 0


def clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {})}


def short(text: str | None, limit: int = 200) -> str | None:
    if not text:
        return None
    one = " ".join(text.split())
    return one[:limit] if one else None


# ---------------------------------------------------------------------- commands


def words(command: str) -> list[str]:
    """Shell-like words: quotes group (and are removed), no escapes, so Windows paths survive;
    runs of `;|&<>` are operator words of their own."""
    out: list[str] = []
    cur: list[str] = []
    started = False
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
        elif c in "\"'":
            quote, started = c, True
        elif c.isspace():
            if started:
                out.append("".join(cur))
                cur, started = [], False
        elif c in ";|&<>":
            if started:
                out.append("".join(cur))
                cur, started = [], False
            j = i
            while j < n and command[j] in ";|&<>":
                j += 1
            out.append(command[i:j])
            i = j
            continue
        else:
            cur.append(c)
            started = True
        i += 1
    if started:
        out.append("".join(cur))
    return out


def _operator(word: str) -> bool:
    return bool(word) and all(c in ";|&<>" for c in word)


def _segments(command: str) -> list[list[str]]:
    """Simple commands of a command line, redirections removed."""
    segments: list[list[str]] = [[]]
    ws = words(command)
    i = 0
    while i < len(ws):
        w = ws[i]
        if _operator(w):
            if "<" in w or ">" in w:
                i += 2  # a redirection and its target
                continue
            segments.append([])
        elif re.fullmatch(r"\d", w) and i + 1 < len(ws) and _operator(ws[i + 1]) and (">" in ws[i + 1] or "<" in ws[i + 1]):
            pass  # the file descriptor of a redirection (2>&1)
        else:
            segments[-1].append(w)
        i += 1
    return [s for s in segments if s]


def _program_of(segment: list[str]) -> tuple[str | None, list[str]]:
    """The program a simple command runs and its arguments, wrappers and assignments skipped."""
    i = 0
    while i < len(segment):
        w = segment[i]
        low = w.lower()
        if _ASSIGNMENT.match(w):
            i += 1
        elif low in _WRAPPERS:
            i += 1
            while i < len(segment) and (segment[i].startswith("-") or (low in ("start", "call") and re.fullmatch(r"/[A-Za-z]{1,8}", segment[i]))):
                takes = segment[i].lower() in _WRAPPER_ARGS.get(low, ())
                i += 2 if takes else 1
            if low == "timeout" and i < len(segment) and re.fullmatch(r"\d+[smhd]?", segment[i]):
                i += 1
            if low == "start" and i < len(segment) and segment[i] == "":
                i += 1  # start "" program: an empty window title
        else:
            return w, segment[i + 1:]
    return None, []


def _path_like(word: str) -> bool:
    return "/" in word or "\\" in word


def _base(program: str) -> str:
    name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith((".exe", ".com")) else name


def _extension(word: str) -> str:
    return posixpath.splitext(word.replace("\\", "/"))[1].lower()


def _script_argument(interpreter: str, args: list[str]) -> str | None:
    """The script file an interpreter is given: after its script option, else its first
    argument with a script's extension. None for a module (`python -m`), an inline
    command (`sh -c`) or an argument with no extension, which may be an option's value."""
    lows = [a.lower() for a in args]
    for i, a in enumerate(lows):
        if a in _SCRIPT_OPTION.get(interpreter, ()):
            return args[i + 1] if i + 1 < len(args) else None
    if (interpreter.startswith("python") and "-m" in lows) or (interpreter in ("sh", "bash", "ksh", "zsh", "dash", "csh", "tcsh") and "-c" in lows):
        return None
    for a in args:
        if interpreter == "sqlplus" and a.startswith("@") and len(a) > 1:
            return a[1:]
        if not a.startswith("-") and _extension(a) in SCRIPT_EXTENSIONS:
            return a
    return None


def starts(command: str) -> tuple[list[str], list[str], list[str]]:
    """(scripts, programs, dynamic) a command line starts: script paths as written, bare
    program names, and the targets that are not literal."""
    return _starts(_segments(command))


def _starts(segments: list[list[str]]) -> tuple[list[str], list[str], list[str]]:
    scripts: list[str] = []
    programs: list[str] = []
    dynamic: list[str] = []
    for segment in segments:
        program, args = _program_of(segment)
        if program is None:
            continue
        base = _base(program)
        lows = [a.lower() for a in args]
        if base == "cmd" and ("/c" in lows or "/k" in lows):
            flag = "/c" if "/c" in lows else "/k"
            s, p, d = _starts([args[lows.index(flag) + 1:]])
            scripts += s
            programs += p
            dynamic += d
            continue
        if base in _BUILTINS or program.lower() in _BUILTINS:
            continue
        if _DYNAMIC.search(program):
            dynamic.append(program)
            continue
        if base in INTERPRETERS:
            script = _script_argument(base, args)
            if script is None:
                programs.append(base)
            elif _DYNAMIC.search(script):
                dynamic.append(script)
            else:
                scripts.append(script)
            continue
        if _path_like(program) or _extension(program) in SCRIPT_EXTENSIONS:
            scripts.append(program)
        elif NAME_SAFE.match(program):
            programs.append(base)
    return scripts, programs, dynamic


def display_path(path: str) -> str | None:
    """A script path as kept in attributes: forward slashes, a UNC path's host and share dropped."""
    p = path.replace("\\", "/")
    if p.startswith("//"):
        parts = p[2:].split("/")
        p = "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
    return p if p and NAME_SAFE.match(p) else None


def path_suffixes(path: str) -> list[str]:
    """Repository paths a script's path may be at: every suffix of it, longest first."""
    p = display_path(path)
    if not p or _DYNAMIC.search(p) or p.startswith("~"):
        return []
    p = re.sub(r"^[A-Za-z]:", "", p)
    parts = [x for x in posixpath.normpath("/" + p.lstrip("/")).split("/") if x not in ("", ".", "..")]
    return ["/".join(parts[i:]) for i in range(len(parts))]


def is_dataset_name(text: str | None) -> bool:
    return bool(text) and bool(_DSN.match(text or ""))


def is_dynamic(text: str | None) -> bool:
    return bool(text) and bool(_DYNAMIC.search(text or ""))


# ---------------------------------------------------------------------- emission


class Emitter:
    """Observations of one export file, spans given as character offsets of the decoded text."""

    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str, codec: str, bom: int):
        from repo_intel.extractors import base

        self.base = base
        self.id, self.path, self.data, self.blob, self.text, self.codec, self.bom = extractor_id, path, data, blob, text, codec, bom
        self.items: list = []
        self.diagnostics: list = []
        self.cursor = 0
        self.declared: dict[tuple[str, str], int] = {}
        self._line_chars = [0] + [m.end() for m in re.finditer("\n", text)]
        self._line_bytes: list[int] | None = None

    # spans
    def _byte(self, char: int) -> int:
        if self.codec == "latin-1":
            return char
        if self._line_bytes is None:
            out, total, prev = [], self.bom, 0
            for start in self._line_chars:
                total += len(self.text[prev:start].encode(self.codec))
                out.append(total)
                prev = start
            self._line_bytes = out
        line = bisect.bisect_right(self._line_chars, char) - 1
        return self._line_bytes[line] + len(self.text[self._line_chars[line]:char].encode(self.codec))

    def span(self, start: int, end: int):
        return self.base.span(self.data, self._byte(start), self._byte(end))

    def whole(self):
        return self.base.whole_file(self.data)

    def at(self, needle: str | None, start: int | None = None):
        """Span of the next occurrence of `needle` from `start` (or the cursor); the whole file if absent."""
        if needle:
            i = self.text.find(needle, self.cursor if start is None else start)
            if i < 0 and start is None:
                i = self.text.find(needle)
            if i >= 0:
                if start is None:
                    self.cursor = i + len(needle)
                return self.span(i, i + len(needle))
        return self.whole()

    # observations
    def construct(self, at, cid: str, modelled: bool) -> None:
        from repo_intel.platforms import construct

        self.items.append(construct(self.blob, at, cid, modelled, evidence_class=E1))

    def unique(self, framework: str, name: str) -> str:
        """`name`, or `name~2`, `name~3`, ... when the file already declared that job."""
        n = self.declared.get((framework, name), 0) + 1
        self.declared[(framework, name)] = n
        return name if n == 1 else f"{name}~{n}"

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any]) -> dict[str, Any]:
        from repo_intel.platforms import platform_entity

        o = platform_entity(self.blob, at, term, key, name, clean(attrs), E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def job(self, at, term: str, framework: str, name: str, attrs: dict[str, Any]) -> dict[str, Any]:
        return self.entity(at, term, {"framework": framework, "name": name}, name, attrs)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, ev=E1) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, clean(qualifiers or {}), ev))

    def depends(self, at, job: dict[str, Any], framework: str, name: str, qualifiers: dict[str, Any] | None = None) -> None:
        """`job` DEPENDS_ON the job `name`, declared here or anywhere else in the unit."""
        self.relation(at, job, P.DEPENDS_ON, self.base.ref(K.JOB, {"framework": framework, "name": name}, name, declare=False), qualifiers)

    def jcl(self, at, job: dict[str, Any], member: str, certain: bool) -> None:
        """`job` RUNS the JCL job of that name, if the repository holds it."""
        name = member.upper()
        target = self.base.ref(K.JOB, {"framework": "jcl", "name": name}, name, declare=False)
        self.relation(at, job, P.RUNS, target, None if certain else {"via": "jcl_member"}, E1 if certain else E4)

    def script(self, at, job: dict[str, Any], path: str) -> bool:
        """`job` RUNS the repository file at a suffix of `path`, and the Program another pack
        declares for that file when its extension names one; False if the path is dynamic."""
        suffixes = path_suffixes(path)
        platform = SCRIPT_PLATFORMS.get(_extension(path))
        for candidate in suffixes:
            target = self.base.ref(K.ARTIFACT, {"path": candidate}, posixpath.basename(candidate), declare=False)
            self.relation(at, job, P.RUNS, target, {"via": "path_suffix"}, E4)
            if platform:
                program = self.base.ref(K.PROGRAM, {"platform": platform, "name": candidate}, posixpath.basename(candidate), declare=False)
                self.relation(at, job, P.RUNS, program, {"via": "path_suffix"}, E4)
        return bool(suffixes)

    def command(self, at, job: dict[str, Any], label: str, command: str | None, attrs: dict[str, Any], construct_id: str, base_dir: str | None = None) -> None:
        """What a command line starts: RUNS for each script, the rest in `attrs`."""
        if not command or not command.strip():
            return
        scripts, programs, dynamic = starts(command)
        kept: list[str] = []
        for s in scripts:
            if base_dir and not _path_like(s) and not is_dynamic(base_dir):
                s = base_dir.rstrip("/\\") + "/" + s
            if self.script(at, job, s):
                shown = display_path(s)
                if shown:
                    kept.append(shown)
            else:
                dynamic.append(s)
        for d in dynamic:
            self.info(f"{label}: the program or script it runs is not literal ({_safe_shape(d)}): not linked")
        attrs["scripts"] = sorted(set(attrs.get("scripts", [])) | set(kept))
        attrs["programs"] = sorted(set(attrs.get("programs", [])) | set(programs))
        if dynamic and not kept and not programs:
            attrs["runs_dynamic"] = True
        self.construct(at, construct_id, bool(kept or programs))

    def info(self, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, "info", message))

    def warning(self, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, "warning", message))

    def result(self):
        return self.base.Extraction(self.items, self.diagnostics)


def _safe_shape(text: str) -> str:
    """A dynamic target as a diagnostic may show it: its variables kept, nothing past 60 characters."""
    return re.sub(r"\s+", " ", text)[:60]
