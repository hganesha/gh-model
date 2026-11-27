"""Embedded SQL in RPG (`EXEC SQL ... ;`, `C/EXEC SQL` ... `C/END-EXEC`) and CL (`RUNSQL`).

`analyse(body)` reads one statement's text, given with every column that is not SQL already
blanked, so character offsets stay the file's. It returns what the statement is (its verb and
construct), the text the shared SQL lineage scanner reads (contract 11.1, item 12: the `db2`
dialect, host variables blanked to keep offsets, the INTO list of a SELECT, VALUES or FETCH
blanked, a cursor declaration's prefix blanked, and system naming `LIB/FILE` read as
`LIB.FILE`), and what the behaviour `query` node carries: the SQL as written without its
INTO list, the host variables read as `:name` bind parameters, `host_vars` listing the host
variables read and set, and the variables an INTO list sets as the `target`.

Nothing here runs SQL or reads another file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_WORD = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_#@$")
DATA_VERBS = frozenset({"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "VALUES", "DECLARE"})


@dataclass
class _Tok:
    kind: str  # word, str, host, op
    text: str
    start: int
    end: int

    @property
    def up(self) -> str:
        return self.text.upper()


def _tokens(body: str) -> list[_Tok]:
    out: list[_Tok] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c in " \t\r\n":
            i += 1
        elif c == "-" and body.startswith("--", i):
            j = body.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and body.startswith("/*", i):
            j = body.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j = i + 1
            while j < n:
                if body[j] == c:
                    if j + 1 < n and body[j + 1] == c:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(_Tok("str", body[i:j], i, j))
            i = j
        elif c == ":" and i + 1 < n and body[i + 1] in _WORD:
            j = i + 1
            while j < n and (body[j] in _WORD or (body[j] == "." and j + 1 < n and body[j + 1] in _WORD)):
                j += 1
            out.append(_Tok("host", body[i + 1 : j], i, j))
            i = j
        elif c in _WORD:
            j = i
            while j < n and body[j] in _WORD:
                j += 1
            out.append(_Tok("word", body[i:j], i, j))
            i = j
        else:
            out.append(_Tok("op", c, i, i + 1))
            i += 1
    return out


@dataclass
class Statement:
    verb: str  # first keyword, upper case: SELECT, INSERT, DECLARE, FETCH, ...
    construct: str  # an ibmi:exec_sql_* construct id
    lineage: str | None = None  # same length as the body, for the lineage scanner; None when it moves no data
    sql: str = ""  # for the query node
    reads: list[str] = field(default_factory=list)  # host variables read, lower case, in order
    sets: list[str] = field(default_factory=list)  # host variables set
    cursor: str | None = None  # DECLARE, OPEN, FETCH, CLOSE: the cursor
    statement: str | None = None  # PREPARE, EXECUTE, DECLARE ... FOR s: the prepared statement name
    dynamic_from: str | None = None  # PREPARE ... FROM :v, EXECUTE IMMEDIATE :v: the variable
    literal_from: str | None = None  # PREPARE ... FROM 'text'


def _collapse(text: str) -> str:
    return " ".join(text.split())


def analyse(body: str) -> Statement:
    toks = _tokens(body)
    words = [t for t in toks if t.kind == "word"]
    if not words:
        return Statement("", "ibmi:exec_sql_other")
    verb = words[0].up
    blanks: list[tuple[int, int]] = []  # removed from the query text and blanked for lineage
    into_span: tuple[int, int] | None = None
    if verb in ("SELECT", "WITH", "VALUES", "FETCH", "SET"):
        depth = 0
        for k, t in enumerate(toks):
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                depth -= 1
            elif depth == 0 and t.kind == "word" and t.up == "INTO":
                end = len(body)
                if verb in ("SELECT", "WITH"):
                    stop = next((u for u in toks[k + 1 :] if u.kind == "word" and u.up == "FROM"), None)
                    end = stop.start if stop is not None else end
                into_span = (t.start, end)
                break
        if verb == "SET" and into_span is None and len(toks) > 1 and toks[1].kind == "host":
            # SET :x = expression: the variable before `=` is set.
            eq = next((k for k, t in enumerate(toks) if t.kind == "op" and t.text == "="), None)
            if eq is not None:
                into_span = (toks[1].start, toks[eq].start)
    reads: list[str] = []
    sets: list[str] = []
    for t in toks:
        if t.kind != "host":
            continue
        name = t.text.lower()
        bucket = sets if into_span and into_span[0] <= t.start < into_span[1] else reads
        if name not in bucket:
            bucket.append(name)
    out = Statement(verb, "ibmi:exec_sql_other", reads=reads, sets=sets)
    second = words[1].up if len(words) > 1 else ""
    if verb in ("SELECT", "WITH", "VALUES") and into_span is not None:
        out.construct = "ibmi:exec_sql_select_into"
    elif verb in ("SELECT", "WITH"):
        out.construct = "ibmi:exec_sql_select_into"
    elif verb in ("INSERT", "UPDATE", "DELETE", "MERGE"):
        out.construct = "ibmi:exec_sql_dml"
    elif verb == "DECLARE" and len(words) > 2 and words[2].up in ("CURSOR", "SCROLL", "NO", "INSENSITIVE", "SENSITIVE", "ASENSITIVE", "DYNAMIC"):
        out.construct = "ibmi:exec_sql_cursor"
        out.cursor = words[1].text.lower()
        fr = next((k for k, t in enumerate(toks) if t.kind == "word" and t.up == "FOR"), None)
        if fr is None or fr + 1 >= len(toks):
            return out
        rest = [t for t in toks[fr + 1 :] if t.kind == "word"]
        if rest and rest[0].up not in ("SELECT", "WITH", "VALUES"):
            out.statement = rest[0].text.lower()  # FOR a prepared statement: its text is not here
            return out
        blanks.append((0, toks[fr].end))
    elif verb in ("OPEN", "FETCH", "CLOSE"):
        out.construct = "ibmi:exec_sql_cursor"
        names = [w for w in words[1:] if w.up not in ("NEXT", "PRIOR", "FIRST", "LAST", "BEFORE", "AFTER", "CURRENT", "RELATIVE", "ABSOLUTE", "FROM", "ROWS", "FOR")]
        out.cursor = names[0].text.lower() if names else None
        return out
    elif verb == "PREPARE":
        out.construct = "ibmi:exec_sql_dynamic"
        out.statement = second.lower() or None
        fr = next((k for k, t in enumerate(toks) if t.kind == "word" and t.up == "FROM"), None)
        if fr is not None and fr + 1 < len(toks):
            src = toks[fr + 1]
            if src.kind == "host":
                out.dynamic_from = src.text.lower()
            elif src.kind == "str" and src.text.startswith("'"):
                out.literal_from = src.text[1:-1].replace("''", "'")
            else:
                out.dynamic_from = src.text.lower()
        return out
    elif verb == "EXECUTE":
        out.construct = "ibmi:exec_sql_dynamic"
        if second == "IMMEDIATE" and len(toks) > 2:
            src = toks[2]
            if src.kind == "str" and src.text.startswith("'"):
                out.literal_from = src.text[1:-1].replace("''", "'")
            else:
                out.dynamic_from = src.text.lower()
        else:
            out.statement = second.lower() or None
        return out
    elif verb == "CALL":
        out.construct = "ibmi:exec_sql_call"
    elif verb in ("COMMIT", "ROLLBACK"):
        out.construct = "ibmi:exec_sql_transaction"
        return out
    else:
        return out
    if into_span is not None:
        blanks.append(into_span)
    # The query text: as written, without the INTO list or a cursor declaration's prefix.
    text = body
    for a, b in sorted(blanks, reverse=True):
        text = text[:a] + " " + text[b:]
    out.sql = _collapse(text).rstrip(";").strip()
    if verb in DATA_VERBS or verb == "CALL":
        out.lineage = lineage_text(body, toks, blanks) if verb != "CALL" else None
    return out


def lineage_text(body: str, toks: list[_Tok] | None = None, blanks: list[tuple[int, int]] = ()) -> str:
    """`body` for the lineage scanner, character for character: host variables become `0`
    padded with blanks, the given ranges are blanked, and `LIB/FILE` becomes `LIB.FILE`."""
    toks = _tokens(body) if toks is None else toks
    chars = list(body)
    for t in toks:
        if t.kind == "host":
            chars[t.start] = "0"
            for k in range(t.start + 1, t.end):
                chars[k] = " "
    for k, t in enumerate(toks):
        if t.kind == "op" and t.text == "/" and 0 < k < len(toks) - 1 and toks[k - 1].kind == "word" and toks[k + 1].kind == "word" and toks[k - 1].end == t.start and toks[k + 1].start == t.end:
            chars[t.start] = "."
    for a, b in blanks:
        for k in range(a, min(b, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)


def host_expr(name: str) -> dict[str, Any]:
    """A host variable as an expression: `ds.field` is a member of `ds`."""
    parts = name.split(".")
    node: dict[str, Any] = {"e": "var", "name": parts[0]}
    for p in parts[1:]:
        node = {"e": "member", "object": node, "name": p}
    return node
