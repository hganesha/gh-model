"""Sqoop commands as dataset moves between a Table and a path, and `hadoop.sqoop` for options files.

A Sqoop command is read from its tokens: an Oozie sqoop action's `<command>` text or its
`<arg>` elements (`hadoop.oozie`), or the lines of an options file (`hadoop.sqoop`, below).

- `import` reads the source table (`--table`), or the tables a free-form `--query` names,
  and writes a path (`--target-dir`, or `--warehouse-dir` plus the table name), or a Hive
  table (`--hive-import` with `--hive-table` and `--hive-database`, or `--hcatalog-table`);
- `export` reads a path (`--export-dir`) or a Hive table (`--hcatalog-table`) and writes the
  table; `--update-key` with `--update-mode allowinsert` is an upsert (`merge_into`);
- `--columns`, `--where` and `--incremental` on a check column shape the rows moved, as
  transforms (language `hadoop`).

Never kept: the connection string (only its JDBC engine name, `mysql`, `oracle`, ...),
user names, passwords, password files and aliases, and every other option value except
table, column and path names. A path a substitution names keeps it as a `{placeholder}`.
An import with no target directory writes under the running user's home directory, which
the file cannot say: it is counted as unresolved with a diagnostic, never guessed.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.hadoop.common import P, Out, dataset_path, mark_dynamic, program, table_name

EXTRACTOR = "hadoop.sqoop"
TOOLS = ("import", "export", "import-all-tables", "import-mainframe", "eval", "merge", "job", "codegen", "create-hive-table",
         "list-databases", "list-tables", "metastore", "version", "help")
# Options that take no value.
FLAGS = frozenset({
    "--hive-import", "--hive-overwrite", "--create-hive-table", "--direct", "--as-textfile", "--as-avrodatafile", "--as-sequencefile",
    "--as-parquetfile", "-p", "--append", "--delete-target-dir", "--verbose", "--compress", "-z", "--hive-drop-import-delims",
    "--clear-staging-table", "--batch", "--autoreset-to-one-mapper", "--skip-dist-cache", "--relaxed-isolation", "--mysql-delimiters",
    "--create-hcatalog-table", "--hbase-create-table", "--validate", "--help",
})
_SECRET_OPTIONS = ("--password", "--password-file", "--password-alias")
_ENGINES = {"jtds": "sqlserver", "microsoft": "sqlserver", "postgres": "postgresql"}
_DIALECTS = {"oracle": "oracle", "sqlserver": "tsql", "teradata": "teradata", "postgresql": "postgres", "mysql": "mysql", "netezza": "postgres", "sybase": "tsql"}


@dataclass
class Token:
    text: str
    start: int  # byte offsets into the file
    end: int


@dataclass
class Command:
    tool: str | None
    tool_at: Token | None
    options: dict[str, list[Token]] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    seen: list[Token] = field(default_factory=list)  # every option token, for spans

    def value(self, *names: str) -> Token | None:
        for n in names:
            if self.options.get(n):
                return self.options[n][0]
        return None


def split_command(text: str, offset: int, to_byte=lambda i: i) -> list[Token]:
    """Shell-like tokens of a command, with quotes removed and offsets kept."""
    out: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] == "\\" and i + 1 < n and text[i + 1] == "\n":
            i += 2
            continue
        start = i
        buf: list[str] = []
        while i < n and not text[i].isspace():
            c = text[i]
            if c in "'\"":
                end = text.find(c, i + 1)
                end = n if end < 0 else end
                buf.append(text[i + 1:end])
                i = end + 1
            elif c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
            else:
                buf.append(c)
                i += 1
        out.append(Token("".join(buf), to_byte(offset + start), to_byte(offset + min(i, n))))
    return out


def parse(tokens: list[Token]) -> Command:
    """The tool and options of a Sqoop command (a leading `sqoop` word is skipped; `job --create
    name -- import ...` is read as the command after `--`)."""
    toks = list(tokens)
    if toks and toks[0].text.lower() in ("sqoop", "sqoop2"):
        toks = toks[1:]
    if toks and toks[0].text.lower() == "job":
        rest = next((i for i, t in enumerate(toks) if t.text == "--"), None)
        inner = parse(toks[rest + 1:]) if rest is not None else Command(None, None)
        inner.options.setdefault("--job", []).append(toks[0])
        return inner
    tool = toks[0] if toks and not toks[0].text.startswith("-") else None
    cmd = Command(tool.text.lower() if tool else None, tool)
    i = 1 if tool else 0
    while i < len(toks):
        t = toks[i]
        if not t.text.startswith("-"):
            i += 1
            continue
        cmd.seen.append(t)
        name, eq, inline = t.text.partition("=")
        name = name.lower() if name.startswith("--") else name
        if eq:
            cmd.options.setdefault(name, []).append(Token(inline, t.start, t.end))
            i += 1
        elif name.lower() in FLAGS or i + 1 >= len(toks) or name == "--":
            cmd.flags.add(name.lower())
            i += 1
        else:
            cmd.options.setdefault(name, []).append(toks[i + 1])
            i += 2
    return cmd


def engine_of(connect: str | None) -> str | None:
    """The JDBC engine of a connection string (`jdbc:mysql://...` -> `mysql`); nothing else of it."""
    m = re.match(r"\s*jdbc:([A-Za-z0-9]+)", connect or "")
    if not m:
        return None
    name = m.group(1).lower()
    return _ENGINES.get(name, name)


def _cond_free(query: str) -> str:
    return re.sub(r"\\?\$CONDITIONS", "1 = 1", query)


def emit(out: Out, cmd: Command, subject: dict[str, Any], whole) -> list[dict[str, Any]]:
    """The relations and constructs of one Sqoop command, as moves of `subject`; returns the transforms."""
    from repo_intel.compile.transforms import Builder, sql_query

    b = Builder()
    at_of = lambda t: out.span(t.start, t.end) if t is not None else whole  # noqa: E731
    span_of = lambda t: [at_of(t).start_byte, at_of(t).end_byte]  # noqa: E731
    where_all = [whole.start_byte, whole.end_byte]
    tool = cmd.tool
    connect = cmd.value("--connect")
    engine = engine_of(connect.text) if connect else None
    if connect is not None:
        out.construct(at_of(connect), "hdp:sqoop_connect", engine is not None)
    for name in _SECRET_OPTIONS:
        for t in cmd.options.get(name, []):
            out.construct(at_of(t), "hdp:sqoop_password", False)
    if "-p" in cmd.flags:
        out.construct(whole, "hdp:sqoop_password", False)
    for t in cmd.options.get("--options-file", []):
        out.construct(at_of(t), "hdp:sqoop_options_file", False)
        out.note("info", "sqoop options in another file are not read here")
    qual = {"via": "sqoop", **({"engine": engine} if engine else {})}

    if tool in ("import", "export"):
        hcat = cmd.value("--hcatalog-table")
        hive_table = None
        if hcat is not None:
            db = cmd.value("--hcatalog-database")
            raw = f"{db.text}.{hcat.text}" if db is not None and "." not in hcat.text else hcat.text
            hive_table = (table_name(raw), hcat)
        table_tok = cmd.value("--table")
        rdbms = table_name(table_tok.text) if table_tok is not None else None
        if tool == "import":
            _import(out, cmd, subject, b, at_of, span_of, where_all, engine, qual, rdbms, table_tok, hive_table, sql_query)
        else:
            _export(out, cmd, subject, b, at_of, span_of, where_all, qual, rdbms, table_tok, hive_table)
    elif tool == "import-all-tables":
        out.construct(at_of(cmd.tool_at), "hdp:sqoop_import_all_tables", False)
        out.note("info", "sqoop import-all-tables names its tables only at run time")
        out.unresolved += 1
    elif tool == "eval":
        out.construct(at_of(cmd.tool_at), "hdp:sqoop_eval", False)
    elif tool == "merge":
        out.construct(at_of(cmd.tool_at), "hdp:sqoop_merge", False)
    elif tool is not None:
        out.construct(at_of(cmd.tool_at), "hdp:sqoop_other_tool", False)
    for t in cmd.options.get("--job", []):
        out.construct(at_of(t), "hdp:sqoop_job", False)
    return mark_dynamic(b.finish())


def _import(out: Out, cmd: Command, subject, b, at_of, span_of, where_all, engine, qual, rdbms, table_tok, hive_table, sql_query) -> None:
    from repo_intel.extractors.sql_lineage import lineage

    query_tok = cmd.value("--query", "-e", "--e")
    ref: str | None = None
    source_ok = False
    if rdbms is not None:
        name, param = rdbms
        target = out.table(at_of(table_tok), name, param)
        out.moves(at_of(table_tok), subject, P.READS_FROM, target, param, qual)
        ref, source_ok = name, True
    elif query_tok is not None:
        query = _cond_free(query_tok.text)
        found = lineage(query, _DIALECTS.get(engine or ""))
        names = sorted({n for n, _, _ in found.reads})
        for name in names:
            out.moves(at_of(query_tok), subject, P.READS_FROM, out.table(at_of(query_tok), name), False, qual)
        out.construct(at_of(query_tok), "hdp:sqoop_free_form_query", bool(names))
        if not names:
            out.unresolved += 1
        try:
            ref = sql_query(b, query, span_of(query_tok), dialect=_DIALECTS.get(engine or ""))
        except Exception as exc:  # the recognizer's fault: the moves stand, the shapes are left out
            out.note("warning", f"transform shapes not derived for a sqoop query: {type(exc).__name__}")
        source_ok = bool(names)
    ref = _shape_rows(cmd, b, ref, span_of)

    # Where the rows go.
    dest: tuple[dict[str, Any], str, bool, Any] | None = None
    if hive_table is not None and hive_table[0] is not None:
        (name, param), tok = hive_table
        dest = (out.table(at_of(tok), name, param), name, param, tok)
        out.construct(at_of(tok), "hdp:sqoop_hive_import", True)
    elif "--hive-import" in cmd.flags:
        ht, hdb = cmd.value("--hive-table"), cmd.value("--hive-database")
        raw = ht.text if ht is not None else (table_tok.text.rsplit(".", 1)[-1] if table_tok is not None else None)
        if raw is not None and hdb is not None and "." not in raw:
            raw = f"{hdb.text}.{raw}"
        found = table_name(raw) if raw else None
        tok = ht or table_tok
        out.construct(at_of(tok), "hdp:sqoop_hive_import", found is not None)
        if found is not None:
            dest = (out.table(at_of(tok), found[0], found[1]), found[0], found[1], tok)
    else:
        tdir, wdir = cmd.value("--target-dir"), cmd.value("--warehouse-dir")
        found = dataset_path(tdir.text) if tdir is not None else None
        tok = tdir
        if found is None and wdir is not None and table_tok is not None:
            found = dataset_path(posixpath.join(wdir.text, table_tok.text.rsplit(".", 1)[-1]))
            tok = wdir
        if found is not None:
            fmt = next((f[5:-4] for f in sorted(cmd.flags) if f.startswith("--as-") and f.endswith("file")), None)
            dest = (out.dataset(at_of(tok), found[0], found[1], {"format": fmt} if fmt else None), found[0], found[1], tok)
        else:
            out.note("info", "sqoop import with no target directory writes under the user's home directory, which the file does not name")
    if dest is not None:
        target, name, param, tok = dest
        out.moves(at_of(tok), subject, P.WRITES_TO, target, param, qual)
        if ref is not None:
            b.write(ref, name, span_of(tok))
    else:
        out.unresolved += 1
    out.construct(at_of(cmd.tool_at), "hdp:sqoop_import", source_ok and dest is not None)


def _export(out: Out, cmd: Command, subject, b, at_of, span_of, where_all, qual, rdbms, table_tok, hive_table) -> None:
    ref: str | None = None
    edir = cmd.value("--export-dir")
    if hive_table is not None and hive_table[0] is not None:
        (name, param), tok = hive_table
        out.moves(at_of(tok), subject, P.READS_FROM, out.table(at_of(tok), name, param), param, qual)
        out.construct(at_of(tok), "hdp:sqoop_hive_import", True)
        ref = name
    elif edir is not None and (found := dataset_path(edir.text)) is not None:
        name, param = found
        out.moves(at_of(edir), subject, P.READS_FROM, out.dataset(at_of(edir), name, param), param, qual)
        ref = name
    else:
        out.unresolved += 1
    columns = cmd.value("--columns")
    if columns is not None and ref is not None:
        ref = b.add("project", span_of(columns), [ref], primitive="select_columns")
    if rdbms is not None:
        name, param = rdbms
        out.moves(at_of(table_tok), subject, P.WRITES_TO, out.table(at_of(table_tok), name, param), param, qual)
        key = cmd.value("--update-key")
        if key is not None and ref is not None:
            mode = cmd.value("--update-mode")
            keys = [k.strip().lower() for k in key.text.split(",") if k.strip()]
            upsert = mode is not None and mode.text.lower() == "allowinsert"
            b.add("publish", span_of(key), [ref], output=name, keys=keys, primitive="merge_into" if upsert else None)
            out.construct(at_of(key), "hdp:sqoop_update_key", True)
        elif ref is not None:
            b.write(ref, name, span_of(table_tok))
    else:
        out.unresolved += 1
        if cmd.value("--call") is not None:
            out.note("info", "sqoop export --call runs a stored procedure: not modelled")
    out.construct(at_of(cmd.tool_at), "hdp:sqoop_export", ref is not None and rdbms is not None)


def _shape_rows(cmd: Command, b, ref: str | None, span_of) -> str | None:
    """--columns, --where and --incremental on the rows an import reads."""
    if ref is None:
        return None
    columns = cmd.value("--columns")
    if columns is not None:
        ref = b.add("project", span_of(columns), [ref], primitive="select_columns")
    where = cmd.value("--where")
    if where is not None and where.text.strip():
        ref = b.add("filter", span_of(where), [ref], predicate={"sql": where.text.strip()})
    mode, check = cmd.value("--incremental"), cmd.value("--check-column")
    if mode is not None and check is not None and re.fullmatch(r"[\w.]+", check.text):
        # Rows past the last value imported: the value is never read, only its role.
        pred = {"e": "binop", "op": ">", "left": {"e": "var", "name": check.text.lower()}, "right": {"e": "var", "scope": "sqoop", "name": "last_value"}}
        ref = b.add("filter", span_of(mode), [ref], predicate=pred)
    return ref


def incremental_attrs(cmd: Command) -> dict[str, Any]:
    """The names of an import's incremental mode, check column and split column (never the last value)."""
    attrs: dict[str, Any] = {}
    mode, check, split = cmd.value("--incremental"), cmd.value("--check-column"), cmd.value("--split-by")
    if mode is not None and mode.text.lower() in ("append", "lastmodified"):
        attrs["incremental"] = mode.text.lower()
    if check is not None and re.fullmatch(r"[\w.]+", check.text):
        attrs["check_column"] = check.text.lower()
    if split is not None and re.fullmatch(r"[\w.]+", split.text):
        attrs["split_by"] = split.text.lower()
    return attrs


def constructs_of_incremental(out: Out, cmd: Command) -> None:
    mode = cmd.value("--incremental")
    if mode is not None:
        out.construct(out.span(mode.start, mode.end), "hdp:sqoop_incremental", cmd.value("--check-column") is not None)


# ------------------------------------------------------------------ options files

_OPTION_SUFFIXES = ("", ".txt", ".opts", ".opt", ".options", ".par", ".sqoop", ".conf", ".cfg", ".args")


class SqoopOptionsFiles:
    """An options file (`sqoop --options-file f`) that states the tool: a Program that moves data.

    Recognised when a directory or the file name says `sqoop` and the first option line is a
    Sqoop tool or the file holds `--connect`; a file of shared options with no tool is counted
    as an options file and declares nothing."""

    id = EXTRACTOR
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        low = path.lower()
        base = posixpath.basename(low)
        ext = posixpath.splitext(base)[1]
        return "sqoop" in low and ext in _OPTION_SUFFIXES and not base.startswith(".")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        ext = posixpath.splitext(path.lower())[1]
        if ext not in _OPTION_SUFFIXES:
            return base.Extraction(parsed=False)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return base.Extraction(parsed=False)
        from repo_intel.extractors.sql_lineage import byte_offsets

        to_byte = byte_offsets(text, data)
        tokens: list[Token] = []
        pos = 0
        for line in text.splitlines(keepends=True):
            body = line.strip()
            if body and not body.startswith("#"):
                tokens += split_command(line, pos, to_byte)
            pos += len(line)
        if not tokens:
            return base.Extraction(parsed=False)
        first = tokens[0].text.lower()
        if first in ("sqoop", "sqoop2") and len(tokens) > 1:
            first = tokens[1].text.lower()
        has_connect = any(t.text.lower().startswith("--connect") for t in tokens)
        if first not in TOOLS and not has_connect:
            return base.Extraction(parsed=False)
        out = Out(EXTRACTOR, path, data, blob)
        cmd = parse(tokens)
        if cmd.tool not in TOOLS:
            out.construct(out.whole(), "hdp:sqoop_options_file", False)
            return out.finish()
        me = program("sqoop", path)
        attrs = {"kind": "options_file", "tool": cmd.tool, **incremental_attrs(cmd)}
        out.term(out.whole(), "hdp:SqoopJob", me["natural_key"], me["name"], attrs)
        constructs_of_incremental(out, cmd)
        transforms = emit(out, cmd, me, out.whole())
        out.transforms(out.whole(), me, transforms)
        return out.finish()
