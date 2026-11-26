"""A PL/SQL parser that lowers what it reads into the behavioural IR.

`Parser(text, to_byte).script()` reads one file: the units it creates (packages and their
bodies, procedures, functions, triggers, types), anonymous blocks, GRANTs, synonyms and
database links, and the SQL*Plus commands around them. Each procedure, function, trigger
body, package initialization and anonymous block is lowered as it is parsed into a list of
IR statements (repo_intel.compile.behavior), with the facts the extractor turns into
relations collected beside it: the SQL it runs, the procedures it calls and the scheduler
jobs it creates.

How PL/SQL maps onto the IR:

- SQL statements are `query` nodes. Their text is the statement as written, less the
  PL/SQL INTO clause (`SELECT ... INTO v`, `BULK COLLECT INTO`, `RETURNING ... INTO`),
  whose targets become the node's `target`. PL/SQL variables appear in SQL by name, as
  PL/SQL writes them; `params` lists the declared variables and parameters a statement
  names, so a reader can tell them from columns. A cursor's query is lowered where the
  cursor is opened or looped over, with the cursor's declaration as its span.
- `EXECUTE IMMEDIATE` and `DBMS_SQL.PARSE` are `query` nodes too. Text that is literal is
  read like any other SQL; text built at run time keeps its literal parts and writes each
  computed part as `:name` with an `inline` parameter, so consumers treat it as dynamic,
  and a diagnostic counts it. Nothing is resolved by guesswork.
- `:NEW` and `:OLD` in a trigger are variables of scope `new` and `old`;
  `INSERTING`, `UPDATING` and `DELETING` are variables of scope `trigger`.
- FORALL, CLOSE, GOTO and compound trigger sections have no neutral form and are
  `platform` nodes named by the pack's construct ids.

The parser is forgiving: a statement it cannot read becomes an
`ora:unrecognised_statement` node and parsing goes on at the next `;`; a unit whose
structure cannot be read raises `ParseError`, and the extractor falls back to reading
its SQL statements for lineage only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.plsql.lexer import BIND, IDENT, NUMBER, OP, QUOTED, SLASH, STRING, Token, tokens


class ParseError(Exception):
    pass


# Functions of the SQL and PL/SQL standard packages: called by name, never a CALLS relation.
BUILTINS = frozenset("""
abs acos add_months ascii asciistr asin atan atan2 bfilename bin_to_num bitand cardinality cast ceil chartorowid chr coalesce compose
concat convert corr cos cosh count cume_dist current_date current_timestamp cv dbtimezone decode decompose dense_rank deref dump
empty_blob empty_clob exp extract extractvalue first_value floor from_tz greatest group_id grouping hextoraw initcap instr instrb
lag last_day last_value lead least length lengthb listagg ln lnnvl localtimestamp log lower lpad ltrim max median min mod
months_between nanvl new_time next_day nls_charset_id nls_initcap nls_lower nls_upper nlssort ntile nullif numtodsinterval
numtoyminterval nvl nvl2 ora_hash percent_rank power rank rawtohex ref regexp_count regexp_instr regexp_like regexp_replace
regexp_substr remainder replace round row_number rowidtochar rpad rtrim sessiontimezone sign sin sinh soundex sqlcode sqlerrm sqrt
standard_hash stddev substr substrb sum sys_context sys_extract_utc sys_guid sysdate systimestamp tan tanh to_binary_double
to_binary_float to_blob to_char to_clob to_date to_dsinterval to_lob to_multi_byte to_nchar to_nclob to_number to_single_byte
to_timestamp to_timestamp_tz to_yminterval translate treat trim trunc tz_offset uid unistr upper user userenv variance vsize
xmlagg xmlelement xmltype json_value json_query json_object json_array json_table raise_application_error inserting updating deleting
""".split())
# Built-in functions that are called without parentheses.
_NILADIC = frozenset({"sysdate", "systimestamp", "current_date", "current_timestamp", "localtimestamp", "user", "uid", "sqlcode", "sqlerrm", "dbtimezone", "sessiontimezone"})
_SYSTEM_PACKAGES = frozenset({"sys", "standard", "htp", "htf", "owa", "owa_util", "owa_cookie", "xmltype", "json_object_t", "json_array_t", "json_element_t", "wpg_docload"})
_SYSTEM_PREFIXES = ("dbms_", "utl_", "apex_", "ctx_", "sdo_", "ord_", "owa_", "wwv_")
# Words that end an expression: the clause keywords that may follow one.
_EXPR_STOP = frozenset({"THEN", "LOOP", "INTO", "USING", "RETURNING", "RETURN", "BULK", "LIMIT", "FROM", "WHEN", "ELSE", "ELSIF", "END", "IS", "AS",
                        "DEFAULT", "SAVE", "BEGIN", "EXCEPTION", "DECLARE", "ESCAPE", "ON", "FOR", "WHERE", "ORDER", "GROUP"})
_SQL_START = frozenset({"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "LOCK"})
_SQLPLUS = frozenset({"SET", "PROMPT", "SPOOL", "SHOW", "WHENEVER", "DEFINE", "UNDEFINE", "REM", "REMARK", "EXIT", "QUIT", "CONNECT", "CONN",
                      "COLUMN", "COL", "TTITLE", "BTITLE", "BREAK", "COMPUTE", "VARIABLE", "VAR", "PRINT", "ACCEPT", "PAUSE", "HOST", "CLEAR", "START", "DISCONNECT", "EXEC", "EXECUTE"})
_TYPES = (
    ("struct", ("%ROWTYPE", "RECORD")),
    ("any", ("%TYPE",)),
    ("query", ("SYS_REFCURSOR", "REF CURSOR")),
    ("integer", ("PLS_INTEGER", "BINARY_INTEGER", "SIMPLE_INTEGER", "INTEGER", "INT", "SMALLINT", "NATURAL", "POSITIVE", "SIGNTYPE")),
    ("number", ("NUMBER", "NUMERIC", "DECIMAL", "DEC", "FLOAT", "REAL", "DOUBLE", "BINARY_FLOAT", "BINARY_DOUBLE")),
    ("binary", ("BLOB", "RAW", "LONG RAW", "BFILE")),
    ("string", ("VARCHAR2", "VARCHAR", "NVARCHAR2", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG", "STRING", "ROWID", "UROWID")),
    ("datetime", ("TIMESTAMP",)),
    ("date", ("DATE",)),
    ("boolean", ("BOOLEAN",)),
)
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_SECRET_SQL = re.compile(r"(\bidentified\s+by\s+)(\"[^\"]*\"|[^\s;]+)|(\busing\s+)('(?:[^']|'')*')", re.I)


def ir_type(text: str) -> str:
    up = " ".join(text.upper().split())
    for name, words in _TYPES:
        if any(up.startswith(w) or w.startswith("%") and w in up for w in words):
            return name
    return "any"


def system_name(parts: list[str]) -> bool:
    """A call into a package Oracle supplies (DBMS_*, UTL_*, HTP, ...) or a standard function."""
    if not parts:
        return False
    first = parts[0]
    if len(parts) == 1:
        return first in BUILTINS
    if first == "sys" and len(parts) > 1:
        return True
    return first in _SYSTEM_PACKAGES or first.startswith(_SYSTEM_PREFIXES)


def scrub_text(value: str) -> str:
    """A literal or SQL text without mail addresses, passwords and connect strings."""
    value = _MAIL.sub("<mail address>", value)
    return _SECRET_SQL.sub(lambda m: (m.group(1) + "<password>") if m.group(1) else (m.group(3) + "'<connect string>'"), value)


# ------------------------------------------------------------------ what the parser returns


@dataclass
class Sql:
    """One SQL statement a unit runs, for lineage: `text` keeps the statement's character
    offsets (removed clauses are blanked), `start` is its offset in the file."""

    kind: str
    text: str
    start: int
    end: int
    dynamic: bool = False  # literal text given to EXECUTE IMMEDIATE or DBMS_SQL: its offsets are not the file's


@dataclass
class Call:
    parts: list[str]
    start: int
    end: int
    remote: bool = False


@dataclass
class Job:
    framework: str  # dbms_scheduler | dbms_job
    name: str | None
    start: int
    end: int
    attrs: dict[str, Any] = field(default_factory=dict)
    runs: list[list[str]] = field(default_factory=list)  # procedure names, as dotted parts
    runs_program: str | None = None  # a scheduler program it names, resolved in the file when it can be


@dataclass
class Body:
    """A unit of behaviour and the facts read from it."""

    statements: list[dict[str, Any]] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    sql: list[Sql] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    links: list[tuple[str, int, int]] = field(default_factory=list)  # database links named in SQL


@dataclass
class Subprogram:
    kind: str  # procedure | function
    name: str
    start: int
    end: int
    name_at: tuple[int, int]
    params: list[dict[str, Any]] = field(default_factory=list)
    returns: str | None = None
    options: list[str] = field(default_factory=list)
    forward: bool = False  # a declaration only: a package specification's, or a forward declaration
    call_spec: bool = False
    autonomous: bool = False
    body: Body | None = None


@dataclass
class Unit:
    kind: str  # package | package_body | procedure | function | trigger
    name: str
    start: int
    end: int
    name_at: tuple[int, int]
    subprograms: list[Subprogram] = field(default_factory=list)
    init: Body | None = None
    body: Body | None = None  # a stand-alone subprogram's or a trigger's
    attrs: dict[str, Any] = field(default_factory=dict)
    wrapped: bool = False
    error: str | None = None  # why the structure could not be read (the extractor falls back)


@dataclass
class Grant:
    privileges: list[tuple[str, list[str]]]  # (privilege, the columns it is limited to)
    object: str | None
    object_kind: str | None  # directory, java source, ... when the GRANT says so
    grantees: list[str]
    grant_option: bool
    start: int
    end: int


@dataclass
class Synonym:
    name: str
    target: str
    link: str | None
    public: bool
    start: int
    end: int


@dataclass
class DbLink:
    name: str
    public: bool
    shared: bool
    authentication: str
    start: int
    end: int


@dataclass
class Script:
    units: list[Unit] = field(default_factory=list)
    blocks: list[tuple[Body, int, int]] = field(default_factory=list)
    grants: list[Grant] = field(default_factory=list)
    synonyms: list[Synonym] = field(default_factory=list)
    dblinks: list[DbLink] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    programs: dict[str, tuple[str, str]] = field(default_factory=dict)  # scheduler program -> (type, action)
    schedules: dict[str, str] = field(default_factory=dict)  # scheduler schedule -> repeat interval
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)  # (severity, message)


_EOF = Token("eof", "", 0, 0, True)


class Parser:
    def __init__(self, text: str, to_byte: Callable[[int], int] = lambda i: i):
        self.text = text
        self.to_byte = to_byte
        self.toks = tokens(text)
        self.i = 0
        self.stop = len(self.toks)
        self.out = Script()
        self.scopes: list[dict[str, str]] = []
        self.body: Body | None = None
        self.cursors: list[dict[str, dict[str, Any]]] = []
        self.package: str | None = None
        self.members: set[str] = set()
        self.trigger_names: dict[str, str] = {}  # REFERENCING names -> new | old
        self.in_trigger = False
        self._bare = False  # in a trigger's WHEN clause, where NEW and OLD are written without a colon
        self.last_sql: dict[str, Any] = {}
        self.last_kind = ""

    # -------------------------------------------------------------- token access

    def peek(self, k: int = 0) -> Token:
        j = self.i + k
        return self.toks[j] if j < self.stop else _EOF

    def next(self) -> Token:
        t = self.peek()
        if t is _EOF:
            raise ParseError("unexpected end of text")
        self.i += 1
        return t

    def at_word(self, *words: str) -> bool:
        return self.peek().is_(*words)

    def at_op(self, *ops: str) -> bool:
        t = self.peek()
        return t.kind == OP and t.text in ops

    def accept(self, *words: str) -> Token | None:
        return self.next() if self.at_word(*words) else None

    def accept_op(self, *ops: str) -> Token | None:
        return self.next() if self.at_op(*ops) else None

    def expect(self, *words: str) -> Token:
        if not self.at_word(*words):
            raise ParseError(f"expected {' or '.join(words)} at line {self.line(self.peek().start)}")
        return self.next()

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            raise ParseError(f"expected {op!r} at line {self.line(self.peek().start)}")
        return self.next()

    def prev(self) -> Token:
        return self.toks[self.i - 1]

    def line(self, char: int) -> int:
        return self.text.count("\n", 0, char) + 1

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.out.constructs.append((cid, a, b, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.out.diagnostics.append((severity, message))

    def skip_to_semicolon(self) -> None:
        """Past the next `;` outside parentheses (or to a `/` line, which also ends a statement)."""
        depth = 0
        while self.peek() is not _EOF:
            t = self.peek()
            if t.kind == SLASH:
                return
            self.i += 1
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth = max(0, depth - 1)
            elif t.kind == OP and t.text == ";" and depth == 0:
                return

    def skip_parens(self) -> None:
        self.expect_op("(")
        depth = 1
        while depth:
            t = self.next()
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1

    def name_token(self) -> Token:
        t = self.peek()
        if t.kind not in (IDENT, QUOTED):
            raise ParseError(f"expected a name at line {self.line(t.start)}")
        return self.next()

    def qualified(self) -> tuple[str, int, int, str | None]:
        """`[schema.]name[@link]`: (dotted lower-case name, start, end, link)."""
        first = self.name_token()
        parts, end = [first.name], first.end
        while self.at_op(".") and self.peek(1).kind in (IDENT, QUOTED):
            self.next()
            t = self.next()
            parts.append(t.name)
            end = t.end
        link = None
        if self.at_op("@") and self.peek(1).kind in (IDENT, QUOTED):
            self.next()
            lparts = [self.next().name]
            while self.at_op(".") and self.peek(1).kind in (IDENT, QUOTED):
                self.next()
                lparts.append(self.next().name)
            link = ".".join(lparts)
            end = self.prev().end
        return ".".join(parts), first.start, end, link

    # -------------------------------------------------------------- scopes

    def declare(self, name: str, scope: str) -> None:
        if self.scopes:
            self.scopes[-1][name] = scope

    def scope_of(self, name: str) -> str | None:
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return None

    def cursor(self, name: str) -> dict[str, Any] | None:
        for c in reversed(self.cursors):
            if name in c:
                return c[name]
        return None

    # -------------------------------------------------------------- the file

    def script(self) -> Script:
        while self.peek() is not _EOF:
            t = self.peek()
            if t.kind == SLASH or (t.kind == OP and t.text == ";"):
                self.i += 1
                continue
            if t.bol and t.kind == OP and t.text in ("@",):
                self.construct("ora:sqlplus", t.start, self.line_end(t.start))
                self.skip_line()
                continue
            if t.bol and t.kind == IDENT and t.up in _SQLPLUS and not self.sql_set(t):
                self.construct("ora:sqlplus", t.start, self.line_end(t.start), t.up in ("EXEC", "EXECUTE"))
                if t.up in ("EXEC", "EXECUTE"):
                    self.exec_command()
                else:
                    self.skip_line()
                continue
            start = self.i
            try:
                self.top_level()
            except ParseError as exc:
                self.i = start
                self.note(f"statement at line {self.line(t.start)} not read: {exc}", "warning")
                self.skip_unit()
        return self.out

    def sql_set(self, t: Token) -> bool:
        """`SET TRANSACTION` and `SET ROLE` are SQL; other SETs at a line start are SQL*Plus."""
        return t.up == "SET" and self.peek(1).is_("TRANSACTION", "ROLE", "CONSTRAINT", "CONSTRAINTS")

    def line_end(self, char: int) -> int:
        j = self.text.find("\n", char)
        return len(self.text) if j < 0 else j

    def skip_line(self) -> None:
        end = self.line_end(self.peek().start)
        while self.peek() is not _EOF and self.peek().start < end:
            self.i += 1

    def skip_unit(self) -> None:
        """Past the unit that failed: to the next `/` line, or the next CREATE at a line start."""
        self.i += 1
        while self.peek() is not _EOF:
            t = self.peek()
            if t.kind == SLASH or (t.bol and t.is_("CREATE", "GRANT", "BEGIN", "DECLARE")):
                return
            self.i += 1

    def exec_command(self) -> None:
        """SQL*Plus `EXEC proc(args)`: one call, as an anonymous block of one statement."""
        start_tok = self.next()
        end = self.line_end(start_tok.start)
        stop = self.i
        while stop < len(self.toks) and self.toks[stop].start < end:
            stop += 1
        saved = self.stop
        self.stop = stop
        try:
            self.block_body(lambda: self.simple_statement(), start_tok.start)
        except ParseError as exc:
            self.note(f"EXEC at line {self.line(start_tok.start)} not read: {exc}", "warning")
        finally:
            self.stop = saved
            self.i = stop

    def block_body(self, parse: Callable[[], list[dict[str, Any]]], start: int) -> None:
        body = Body()
        outer = self.body
        self.body = body
        self.scopes.append({})
        self.cursors.append({})
        try:
            body.statements = parse()
        finally:
            self.body = outer
            self.scopes.pop()
            self.cursors.pop()
        end = self.prev().end
        if body.statements:
            self.out.blocks.append((body, start, end))

    def top_level(self) -> None:
        t = self.peek()
        if t.is_("CREATE"):
            self.create()
        elif t.is_("BEGIN", "DECLARE"):
            self.construct("ora:anonymous_block", t.start, t.end)
            self.block_body(lambda: self.block(label=None), t.start)
            self.accept_op(";")
        elif t.is_("GRANT"):
            self.grant()
        elif t.is_("REVOKE"):
            self.construct("ora:revoke", t.start, t.end, False)
            self.skip_to_semicolon()
        elif t.is_("ALTER", "DROP", "COMMENT", "TRUNCATE", "ANALYZE", "AUDIT", "RENAME", "PURGE", "FLASHBACK"):
            self.construct("ora:other_ddl", t.start, t.end, False)
            self.skip_to_semicolon()
        else:
            # DML and queries in a script are the SQL lineage extractor's; anything else is skipped.
            self.skip_to_semicolon()

    def create(self) -> None:
        create = self.expect("CREATE")
        if self.accept("OR"):
            self.expect("REPLACE", "ALTER")
        while self.accept("EDITIONABLE", "NONEDITIONABLE", "EDITIONING", "FORCE", "NOFORCE"):
            pass
        public = bool(self.accept("PUBLIC"))
        shared = bool(self.accept("SHARED"))
        public = public or bool(self.accept("PUBLIC"))
        t = self.peek()
        if t.is_("PACKAGE"):
            self.next()
            if self.accept("BODY"):
                self.package_unit(create.start, body=True)
            else:
                self.package_unit(create.start, body=False)
        elif t.is_("PROCEDURE", "FUNCTION"):
            self.standalone(create.start)
        elif t.is_("TRIGGER"):
            self.next()
            self.trigger(create.start)
        elif t.is_("TYPE"):
            self.next()
            cid = "ora:create_type_body" if self.accept("BODY") else "ora:create_type"
            self.construct(cid, create.start, t.end, False)
            self.skip_unit_to_slash()
        elif t.is_("JAVA", "AND"):
            self.construct("ora:create_java", create.start, t.end, False)
            self.skip_unit_to_slash()
        elif t.is_("SYNONYM"):
            self.next()
            name, _, _, _ = self.qualified()
            self.expect("FOR")
            target, _, _, link = self.qualified()
            self.out.synonyms.append(Synonym(name, target, link, public, create.start, self.prev().end))
            self.construct("ora:create_synonym", create.start, self.prev().end)
            self.skip_to_semicolon()
        elif t.is_("DATABASE"):
            self.next()
            self.expect("LINK")
            name, _, _, _ = self.qualified()
            auth = "default"
            # Only the kind of authentication is read: users, passwords and connect strings are values.
            j = self.i
            while j < self.stop and not (self.toks[j].kind == OP and self.toks[j].text == ";") and self.toks[j].kind != SLASH:
                if self.toks[j].is_("CONNECT") and j + 2 < self.stop and self.toks[j + 1].is_("TO"):
                    auth = "current_user" if self.toks[j + 2].is_("CURRENT_USER") else "fixed_user"
                j += 1
            self.out.dblinks.append(DbLink(name, public, shared, auth, create.start, t.end))
            self.construct("ora:create_database_link", create.start, t.end)
            self.skip_to_semicolon()
        else:
            self.construct("ora:other_ddl", create.start, t.end, False)
            self.skip_to_semicolon()

    def skip_unit_to_slash(self) -> None:
        while self.peek() is not _EOF and self.peek().kind != SLASH and not (self.peek().bol and self.peek().is_("CREATE", "GRANT")):
            self.i += 1

    # -------------------------------------------------------------- units

    def wrapped(self, unit: Unit) -> bool:
        if self.at_word("WRAPPED"):
            unit.wrapped = True
            self.construct("ora:wrapped", unit.start, self.peek().end, False)
            self.note(f"{unit.kind.replace('_', ' ')} {unit.name} is wrapped: commit its unwrapped source to read it", "warning")
            self.skip_unit_to_slash()
            return True
        return False

    def unit_options(self, until: tuple[str, ...]) -> list[str]:
        """AUTHID, ACCESSIBLE BY, DETERMINISTIC, ...: up to IS, AS or `;`."""
        options: list[str] = []
        while not self.at_word(*until) and not self.at_op(";") and self.peek() is not _EOF:
            t = self.next()
            if t.is_("AUTHID"):
                options.append("authid " + self.next().name)
            elif t.is_("ACCESSIBLE"):
                self.expect("BY")
                options.append("accessible_by")
                if self.at_op("("):
                    self.skip_parens()
            elif t.is_("DETERMINISTIC", "PIPELINED", "PARALLEL_ENABLE", "RESULT_CACHE", "SQL_MACRO", "AGGREGATE", "SHARING", "DEFAULT"):
                options.append(t.name)
                if self.at_op("("):
                    self.skip_parens()
            elif t.kind == OP and t.text == "(":
                self.i -= 1
                self.skip_parens()
        if options:
            self.construct("ora:subprogram_option", self.prev().start, self.prev().end)
        return options

    def package_unit(self, start: int, body: bool) -> None:
        name, a, b, _ = self.qualified()
        unit = Unit("package_body" if body else "package", name, start, b, (a, b))
        self.out.units.append(unit)
        self.construct("ora:create_package_body" if body else "ora:create_package", start, b)
        if self.wrapped(unit):
            unit.end = self.prev().end
            return
        options = self.unit_options(("IS", "AS"))
        if options:
            unit.attrs["options"] = options
        self.expect("IS", "AS")
        outer_package, outer_members = self.package, self.members
        self.package, self.members = name, set()
        self.scopes.append({})
        self.cursors.append({})
        saved = self.i
        try:
            if body:
                self.members = self.member_names()
                self.i = saved
            decls = self.declarations(unit=unit, stops=("BEGIN", "END"))
            if body and self.at_word("BEGIN"):
                begin = self.next()
                self.construct("ora:initialization_section", begin.start, begin.end)
                init = Body()
                self.body = init
                init.statements = decls + self.statements(("END", "EXCEPTION"))
                init.statements = self.handlers(init.statements, begin.start)
                self.body = None
                unit.init = init
            self.expect("END")
            if self.peek().kind in (IDENT, QUOTED) and not self.at_op(";"):
                self.next()
            self.accept_op(";")
        except ParseError as exc:
            unit.error = str(exc)
            self.note(f"{'package body' if body else 'package'} {name}: structure not read ({exc}); its SQL is read for lineage only", "warning")
            self.i = saved
            self.skip_unit_to_slash()
        finally:
            self.package, self.members = outer_package, outer_members
            self.scopes.pop()
            self.cursors.pop()
            self.body = None
        unit.end = self.prev().end

    def member_names(self) -> set[str]:
        """The names of a package body's procedures and functions, read ahead so calls between them resolve."""
        names: set[str] = set()
        depth = 0
        for j in range(self.i, self.stop):
            t = self.toks[j]
            if t.kind == SLASH:
                break
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1
            elif depth == 0 and t.is_("PROCEDURE", "FUNCTION") and j + 1 < self.stop and self.toks[j + 1].kind in (IDENT, QUOTED):
                names.add(self.toks[j + 1].name)
        return names

    def standalone(self, start: int) -> None:
        kw = self.next()
        name, a, b, _ = self.qualified()
        unit = Unit(kw.name, name, start, b, (a, b))
        self.out.units.append(unit)
        self.construct(f"ora:create_{kw.name}", start, b)
        if self.wrapped(unit):
            unit.end = self.prev().end
            return
        saved = self.i
        self.package, self.members = None, set()
        try:
            sub = self.subprogram_rest(kw, name, a, b, start, member=False)
            unit.body = sub.body
            unit.attrs.update({k: v for k, v in (("returns", sub.returns), ("options", sub.options or None), ("autonomous_transaction", sub.autonomous or None), ("call_spec", sub.call_spec or None)) if v})
            unit.attrs["params"] = [p["name"] for p in sub.params]
        except ParseError as exc:
            unit.error = str(exc)
            self.note(f"{kw.name} {name}: structure not read ({exc}); its SQL is read for lineage only", "warning")
            self.i = saved
            self.skip_unit_to_slash()
        unit.end = self.prev().end

    def parameters(self) -> list[dict[str, Any]]:
        params: list[dict[str, Any]] = []
        if not self.accept_op("("):
            return params
        while not self.at_op(")"):
            name = self.name_token().name
            mode = "in"
            if self.accept("IN"):
                mode = "in_out" if self.accept("OUT") else "in"
            elif self.accept("OUT"):
                mode = "out"
            self.accept("NOCOPY")
            type_start = self.peek().start
            depth = 0
            while self.peek() is not _EOF:
                t = self.peek()
                if depth == 0 and ((t.kind == OP and t.text in (",", ")", ":=")) or t.is_("DEFAULT")):
                    break
                if t.kind == OP and t.text == "(":
                    depth += 1
                elif t.kind == OP and t.text == ")":
                    depth -= 1
                self.i += 1
            declared = " ".join(self.text[type_start : self.prev().end].split())
            default = None
            if self.accept_op(":=") or self.accept("DEFAULT"):
                default = self.expression()
            p: dict[str, Any] = {"name": name, "type": ir_type(declared), "required": default is None and mode != "out", "mode": mode, "declared_type": declared}
            if default is not None:
                p["default"] = default
            params.append(p)
            if not self.accept_op(","):
                break
        self.expect_op(")")
        return params

    def subprogram_rest(self, kw: Token, name: str, a: int, b: int, start: int, member: bool) -> Subprogram:
        """After `PROCEDURE name` or `FUNCTION name`: parameters, return type, options, then a
        declaration (`;`), a call specification, or a body, which is lowered."""
        sub = Subprogram(kw.name, name, start, b, (a, b))
        sub.params = self.parameters()
        if kw.is_("FUNCTION"):
            self.expect("RETURN")
            type_start = self.peek().start
            while not self.at_word("IS", "AS", "DETERMINISTIC", "PIPELINED", "PARALLEL_ENABLE", "RESULT_CACHE", "AUTHID", "ACCESSIBLE", "AGGREGATE", "SQL_MACRO", "USING") and not self.at_op(";") and self.peek() is not _EOF:
                self.i += 1
            sub.returns = " ".join(self.text[type_start : self.prev().end].split())
        sub.options = self.unit_options(("IS", "AS"))
        if self.accept_op(";"):
            sub.forward = True
            sub.end = self.prev().end
            return sub
        self.expect("IS", "AS")
        if self.at_word("LANGUAGE", "EXTERNAL"):
            sub.call_spec = True
            self.construct("ora:call_spec", self.peek().start, self.peek().end, False)
            self.skip_to_semicolon()
            sub.end = self.prev().end
            return sub
        body = Body()
        outer_body = self.body
        self.body = body
        self.scopes.append({p["name"]: "arguments" for p in sub.params})
        self.cursors.append({})
        try:
            decls = self.declarations(unit=None, stops=("BEGIN",), sub=sub)
            begin = self.expect("BEGIN")
            stmts = self.statements(("END", "EXCEPTION"))
            stmts = self.handlers(decls + stmts, begin.start)
            self.expect("END")
            if self.peek().kind in (IDENT, QUOTED):
                self.next()
            self.expect_op(";")
        finally:
            self.body = outer_body
            self.scopes.pop()
            self.cursors.pop()
        body.statements = stmts
        body.params = sub.params
        sub.body = body
        sub.end = self.prev().end
        return sub

    def handlers(self, stmts: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
        """An EXCEPTION section, if one follows: the statements from `start` (the BEGIN) to it become a `try`."""
        if not self.at_word("EXCEPTION"):
            return stmts
        self.next()
        catches = []
        while self.at_word("WHEN"):
            when = self.next()
            names = [self.qualified()[0]]
            while self.accept("OR"):
                names.append(self.qualified()[0])
            self.expect("THEN")
            body = self.statements(("WHEN", "END"))
            self.construct("ora:exception_handler", when.start, when.end)
            catches.append({"type": "|".join(names), "body": body})
        return [{"op": "try", "at": self.at(start, self.prev().end), "body": stmts, "catches": catches}]

    # -------------------------------------------------------------- declarations

    def declarations(self, unit: Unit | None, stops: tuple[str, ...], sub: Subprogram | None = None) -> list[dict[str, Any]]:
        """Declarations up to one of `stops`. A package's procedures and functions go to `unit`;
        variables and constants become `declare` statements of the enclosing body."""
        out: list[dict[str, Any]] = []
        while not self.at_word(*stops):
            t = self.peek()
            if t is _EOF or t.kind == SLASH:
                raise ParseError("declarations do not end")
            if t.is_("PROCEDURE", "FUNCTION"):
                kw = self.next()
                name_tok = self.name_token()
                if unit is not None:
                    member = self.subprogram_rest(kw, name_tok.name, name_tok.start, name_tok.end, kw.start, member=True)
                    unit.subprograms.append(member)
                    self.construct(f"ora:package_{kw.name}", kw.start, name_tok.end)
                else:
                    self.declare(name_tok.name, "local")
                    local = self.subprogram_rest(kw, name_tok.name, name_tok.start, name_tok.end, kw.start, member=False)
                    self.construct("ora:local_subprogram", kw.start, name_tok.end)
                    if local.body is not None and self.body is not None:
                        # What a local subprogram runs is run by the unit that declares it.
                        self.body.sql += local.body.sql
                        self.body.calls += local.body.calls
                        self.body.links += local.body.links
                    if local.body is not None:
                        out.append({"op": "declare", "at": self.at(kw.start, local.end), "name": name_tok.name, "scope": "local",
                                    "value": {"e": "lambda", "body": local.body.statements, "params": [p["name"] for p in local.params]}})
                continue
            if t.is_("CURSOR"):
                self.cursor_declaration()
                continue
            if t.is_("TYPE", "SUBTYPE"):
                self.construct("ora:type_declaration", t.start, t.end, False)
                self.skip_to_semicolon()
                continue
            if t.is_("PRAGMA"):
                self.next()
                p = self.name_token()
                if p.is_("AUTONOMOUS_TRANSACTION"):
                    self.construct("ora:pragma_autonomous_transaction", t.start, p.end, sub is not None)
                    if sub is not None:
                        sub.autonomous = True
                elif p.is_("EXCEPTION_INIT"):
                    self.construct("ora:pragma_exception_init", t.start, p.end, False)
                else:
                    self.construct("ora:pragma_other", t.start, p.end, False)
                self.skip_to_semicolon()
                continue
            name_tok = self.name_token()
            if self.at_word("EXCEPTION"):
                self.next()
                self.expect_op(";")
                self.declare(name_tok.name, "exception")
                self.construct("ora:exception_declaration", name_tok.start, self.prev().end, unit is None)
                if unit is not None:
                    continue
                out.append({"op": "declare", "at": self.at(name_tok.start, self.prev().end), "name": name_tok.name, "scope": "exception"})
                continue
            constant = bool(self.accept("CONSTANT"))
            type_start = self.peek().start
            depth = 0
            while self.peek() is not _EOF:
                x = self.peek()
                if depth == 0 and ((x.kind == OP and x.text in (";", ":=")) or x.is_("DEFAULT")):
                    break
                if x.kind == OP and x.text == "(":
                    depth += 1
                elif x.kind == OP and x.text == ")":
                    depth -= 1
                self.i += 1
            declared = " ".join(self.text[type_start : self.prev().end].split())
            value = None
            if self.accept_op(":=") or self.accept("DEFAULT"):
                value = self.expression()
            self.expect_op(";")
            scope = "package" if unit is not None else "local"
            self.declare(name_tok.name, scope)
            self.construct("ora:constant" if constant else "ora:variable", name_tok.start, self.prev().end, unit is None)
            if unit is None:
                node: dict[str, Any] = {"op": "declare", "at": self.at(name_tok.start, self.prev().end), "name": name_tok.name, "scope": scope, "type": ir_type(declared)}
                if value is not None:
                    node["value"] = value
                if constant:
                    node["constant"] = True
                out.append(node)
        return out

    def cursor_declaration(self) -> None:
        kw = self.next()
        name_tok = self.name_token()
        params = self.parameters()
        if self.accept("RETURN"):
            while not self.at_word("IS") and not self.at_op(";"):
                self.next()
        if self.accept_op(";"):
            self.construct("ora:cursor", kw.start, self.prev().end, False)
            return
        self.expect("IS")
        self.scopes.append({p["name"]: "arguments" for p in params})
        outer, self.body = self.body, None  # the query runs where the cursor is opened, not here
        try:
            self.capture_sql()
        finally:
            self.scopes.pop()
            self.body = outer
        self.expect_op(";")
        self.construct("ora:cursor", kw.start, self.prev().end)
        if self.cursors:
            self.cursors[-1][name_tok.name] = {**self.last_sql, "params": [p["name"] for p in params]}
        self.declare(name_tok.name, "cursor")

    # -------------------------------------------------------------- statements

    def statements(self, stops: tuple[str, ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.at_word(*stops):
            t = self.peek()
            if t is _EOF or t.kind == SLASH:
                raise ParseError("block does not end")
            if t.kind == OP and t.text == "<<":
                self.next()
                label = self.name_token()
                self.expect_op(">>")
                self.construct("ora:label", t.start, self.prev().end, False)
                continue
            if t.kind == OP and t.text == ";":
                self.next()
                continue
            start = self.i
            try:
                out += self.statement()
            except ParseError as exc:
                if self.compound(self.toks[start]):
                    raise
                self.i = start
                self.skip_to_semicolon()
                end = self.prev().end
                self.construct("ora:unrecognised_statement", t.start, end, False)
                self.note(f"statement at line {self.line(t.start)} not read: {exc}")
                out.append({"op": "platform", "construct": "ora:unrecognised_statement", "at": self.at(t.start, end)})
        return out

    @staticmethod
    def compound(t: Token) -> bool:
        return t.is_("IF", "CASE", "LOOP", "WHILE", "FOR", "BEGIN", "DECLARE")

    def statement(self) -> list[dict[str, Any]]:
        t = self.peek()
        up = t.up if t.kind == IDENT else ""
        if up == "NULL" and self.peek(1).kind == OP and self.peek(1).text == ";":
            self.next()
            self.next()
            return []
        if up == "IF":
            return [self.if_statement()]
        if up == "CASE":
            return [self.case_statement()]
        if up in ("LOOP", "WHILE", "FOR"):
            return self.loop_statement()
        if up == "FORALL":
            return [self.forall()]
        if up in ("EXIT", "CONTINUE"):
            return [self.exit_statement()]
        if up == "RETURN":
            self.next()
            value = None if self.at_op(";") else self.expression()
            self.expect_op(";")
            self.construct("ora:return", t.start, self.prev().end)
            node: dict[str, Any] = {"op": "return", "at": self.at(t.start, self.prev().end)}
            if value is not None:
                node["value"] = value
            return [node]
        if up == "RAISE":
            self.next()
            node = {"op": "throw", "at": self.at(t.start, t.end)}
            if not self.at_op(";"):
                name = self.qualified()[0]
                node["value"] = {"e": "name", "name": name, "kind": "exception"}
            self.expect_op(";")
            node["at"] = self.at(t.start, self.prev().end)
            self.construct("ora:raise", t.start, self.prev().end)
            return [node]
        if up == "GOTO":
            self.next()
            label = self.name_token().name
            self.expect_op(";")
            self.construct("ora:goto", t.start, self.prev().end)
            return [{"op": "platform", "construct": "ora:goto", "at": self.at(t.start, self.prev().end), "detail": {"label": label}}]
        if up in ("COMMIT", "ROLLBACK", "SAVEPOINT"):
            return [self.transaction()]
        if up == "SET" and self.peek(1).is_("TRANSACTION"):
            self.skip_to_semicolon()
            self.construct("ora:set_transaction", t.start, self.prev().end)
            return [{"op": "transaction", "action": "begin", "at": self.at(t.start, self.prev().end), "body": []}]
        if up in ("BEGIN", "DECLARE"):
            return self.nested_block()
        if up in _SQL_START and not (self.peek(1).kind == OP and self.peek(1).text in (":=", ".", "(")):
            return [self.sql_statement()]
        if up == "OPEN":
            return self.open_statement()
        if up == "FETCH":
            return [self.fetch()]
        if up == "CLOSE":
            self.next()
            name = self.qualified()[0]
            self.expect_op(";")
            self.construct("ora:close_cursor", t.start, self.prev().end)
            return [{"op": "platform", "construct": "ora:close_cursor", "at": self.at(t.start, self.prev().end), "detail": {"cursor": name}}]
        if up == "EXECUTE" and self.peek(1).is_("IMMEDIATE"):
            return [self.execute_immediate()]
        if up == "PIPE" and self.peek(1).is_("ROW"):
            self.next()
            self.next()
            self.expect_op("(")
            value = self.expression()
            self.expect_op(")")
            self.expect_op(";")
            self.construct("ora:pipe_row", t.start, self.prev().end)
            return [{"op": "output", "channel": "dataset", "at": self.at(t.start, self.prev().end), "value": value}]
        return self.simple_statement()

    def simple_statement(self) -> list[dict[str, Any]]:
        """An assignment or a procedure call."""
        t = self.peek()
        target = self.primary(statement=True)
        if self.accept_op(":="):
            value = self.expression()
            self.expect_op(";")
            self.construct("ora:assignment", t.start, self.prev().end)
            return [{"op": "assign", "at": self.at(t.start, self.prev().end), "target": target, "value": value}]
        if not (self.at_op(";") or self.peek() is _EOF):
            raise ParseError(f"unexpected {self.peek().text!r} at line {self.line(self.peek().start)}")
        self.accept_op(";")
        return [self.call_statement(target, t.start, self.prev().end)]

    def call_statement(self, node: dict[str, Any], a: int, b: int) -> dict[str, Any]:
        """A call, or what a call to a supplied package means: output, a raised error, dynamic SQL, a job."""
        at = self.at(a, b)
        callee = node.get("callee", {}) if node.get("e") == "call" else node
        name = callee.get("name") if callee.get("e") in ("name", "var") else None
        args = node.get("args", []) if node.get("e") == "call" else []
        named = node.get("named", {}) if node.get("e") == "call" else {}
        if node.get("e") == "var" and name and self.scope_of(name.split(".")[0]) is None:
            node = {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": []}
            self.record_call(name.split("."), a, b)
        lname = (name or "").lower()
        pkg = lname.split(".")[0] if "." in lname else ""
        if lname == "raise_application_error":
            self.construct("ora:raise_application_error", a, b)
            return {"op": "throw", "at": at, "value": node}
        if pkg == "dbms_output":
            self.construct("ora:dbms_output", a, b)
            out: dict[str, Any] = {"op": "output", "channel": "log", "at": at}
            if args:
                out["value"] = args[0]
            return out
        if pkg == "utl_file":
            self.construct("ora:utl_file", a, b)
            if lname.split(".")[-1] in ("put", "put_line", "putf", "put_raw", "new_line", "put_nchar", "put_line_nchar"):
                out = {"op": "output", "channel": "file", "at": at}
                if len(args) > 1:
                    out["value"] = args[1]
                return out
        if pkg in ("htp", "htf"):
            self.construct("ora:htp", a, b)
            out = {"op": "output", "channel": "response", "at": at}
            if args:
                out["value"] = args[0]
            return out
        if pkg in ("utl_mail", "utl_smtp"):
            self.construct("ora:mail", a, b)
            return {"op": "output", "channel": "mail", "at": at}
        if pkg == "utl_http":
            self.construct("ora:utl_http", a, b, False)
        if pkg in ("dbms_aq", "dbms_aqadm"):
            self.construct("ora:dbms_aq", a, b, False)
        if pkg in ("dbms_pipe", "dbms_alert"):
            self.construct("ora:dbms_pipe", a, b, False)
        if pkg == "dbms_lock":
            self.construct("ora:dbms_lock", a, b, False)
        if pkg == "dbms_scheduler":
            self.scheduler(lname.split(".")[-1], args, named, a, b)
        if pkg == "dbms_job":
            self.dbms_job(lname.split(".")[-1], args, named, a, b)
        if pkg == "dbms_sql":
            if lname.endswith(".parse") and len(args) > 1:
                return self.dynamic_query(args[1], [], a, b, "ora:dbms_sql")
            self.construct("ora:dbms_sql", a, b, False)
        if node.get("e") != "call":
            node = {"e": "call", "callee": node, "args": []}
        return {"op": "call", "at": at, "call": node}

    def transaction(self) -> dict[str, Any]:
        t = self.next()
        node: dict[str, Any] = {"op": "transaction", "action": t.name, "at": self.at(t.start, t.end), "body": []}
        if t.is_("ROLLBACK"):
            self.accept("WORK")
            if self.accept("TO"):
                self.accept("SAVEPOINT")
                node["savepoint"] = self.name_token().name
        elif t.is_("SAVEPOINT"):
            node["savepoint"] = self.name_token().name
        elif t.is_("COMMIT"):
            self.accept("WORK")
        while not self.at_op(";"):
            self.next()
        self.expect_op(";")
        node["at"] = self.at(t.start, self.prev().end)
        self.construct(f"ora:{t.name}", t.start, self.prev().end)
        return node

    def nested_block(self) -> list[dict[str, Any]]:
        t = self.peek()
        self.scopes.append({})
        self.cursors.append({})
        try:
            stmts = self.block(label=None)
        finally:
            self.scopes.pop()
            self.cursors.pop()
        self.accept_op(";")
        return stmts

    def block(self, label: str | None) -> list[dict[str, Any]]:
        """[DECLARE ...] BEGIN ... [EXCEPTION ...] END [label]"""
        decls: list[dict[str, Any]] = []
        if self.accept("DECLARE"):
            decls = self.declarations(unit=None, stops=("BEGIN",))
        begin = self.expect("BEGIN")
        stmts = self.handlers(decls + self.statements(("END", "EXCEPTION")), begin.start)
        self.expect("END")
        if self.peek().kind in (IDENT, QUOTED) and not self.peek().is_("IF", "LOOP", "CASE"):
            self.next()
        return stmts

    def if_statement(self) -> dict[str, Any]:
        t = self.expect("IF")
        cond = self.expression()
        self.expect("THEN")
        then = self.statements(("ELSIF", "ELSE", "END"))
        node: dict[str, Any] = {"op": "if", "at": [0, 0], "cond": cond, "then": then}
        tail = node
        while self.at_word("ELSIF"):
            e = self.next()
            c = self.expression()
            self.expect("THEN")
            body = self.statements(("ELSIF", "ELSE", "END"))
            nested = {"op": "if", "at": self.at(e.start, self.prev().end), "cond": c, "then": body}
            tail["else"] = [nested]
            tail = nested
        if self.accept("ELSE"):
            tail["else"] = self.statements(("END",))
        self.expect("END")
        self.expect("IF")
        self.expect_op(";")
        node["at"] = self.at(t.start, self.prev().end)
        self.construct("ora:if", t.start, self.prev().end)
        return node

    def case_statement(self) -> dict[str, Any]:
        t = self.expect("CASE")
        selector = None if self.at_word("WHEN") else self.expression()
        cases = []
        default = None
        while self.accept("WHEN"):
            match = self.expression()
            self.expect("THEN")
            cases.append((match, self.statements(("WHEN", "ELSE", "END"))))
        if self.accept("ELSE"):
            default = self.statements(("END",))
        self.expect("END")
        self.expect("CASE")
        if self.peek().kind in (IDENT, QUOTED):
            self.next()
        self.expect_op(";")
        at = self.at(t.start, self.prev().end)
        self.construct("ora:case_statement", t.start, self.prev().end)
        if selector is not None:
            node: dict[str, Any] = {"op": "switch", "at": at, "on": selector, "cases": [{"match": [m], "body": b} for m, b in cases]}
            if default is not None:
                node["default"] = default
            return node
        # A searched CASE is an IF chain.
        root: dict[str, Any] | None = None
        tail: dict[str, Any] | None = None
        for cond, body in cases:
            nested = {"op": "if", "at": at, "cond": cond, "then": body}
            if tail is None:
                root = nested
            else:
                tail["else"] = [nested]
            tail = nested
        if root is None:
            raise ParseError("CASE without WHEN")
        if default is not None and tail is not None:
            tail["else"] = default
        return root

    def loop_body(self) -> list[dict[str, Any]]:
        self.expect("LOOP")
        body = self.statements(("END",))
        self.expect("END")
        self.expect("LOOP")
        if self.peek().kind in (IDENT, QUOTED):
            self.next()
        self.expect_op(";")
        return body

    def loop_statement(self) -> list[dict[str, Any]]:
        t = self.peek()
        if t.is_("LOOP"):
            body = self.loop_body()
            self.construct("ora:basic_loop", t.start, self.prev().end)
            return [{"op": "loop", "kind": "while", "at": self.at(t.start, self.prev().end), "cond": {"e": "lit", "value": True, "type": "boolean"}, "body": body}]
        if t.is_("WHILE"):
            self.next()
            cond = self.expression()
            body = self.loop_body()
            self.construct("ora:while_loop", t.start, self.prev().end)
            return [{"op": "loop", "kind": "while", "at": self.at(t.start, self.prev().end), "cond": cond, "body": body}]
        self.expect("FOR")
        var = self.name_token()
        self.expect("IN")
        reverse = bool(self.accept("REVERSE"))
        self.scopes.append({var.name: "local"})
        try:
            if self.at_op("(") and self.peek(1).is_("SELECT", "WITH"):
                open_paren = self.next()
                query = self.capture_sql(until_paren=True)
                self.expect_op(")")
                body = self.loop_body()
                self.construct("ora:cursor_for_loop", t.start, self.prev().end)
                query["target"] = {"e": "var", "scope": "cursor", "name": var.name}
                loop = {"op": "loop", "kind": "each", "at": self.at(t.start, self.prev().end), "var": {"e": "var", "scope": "local", "name": var.name},
                        "over": {"e": "var", "scope": "cursor", "name": var.name}, "body": body}
                return [query, loop]
            start = self.i
            low = self.expression()
            if self.accept_op(".."):
                high = self.expression()
                body = self.loop_body()
                self.construct("ora:for_loop", t.start, self.prev().end)
                node: dict[str, Any] = {"op": "loop", "kind": "range", "at": self.at(t.start, self.prev().end), "var": {"e": "var", "scope": "local", "name": var.name},
                                        "from": high if reverse else low, "to": low if reverse else high, "body": body}
                if reverse:
                    node["step"] = {"e": "lit", "value": -1, "type": "integer"}
                return [node]
            # A cursor FOR loop over a declared cursor, with or without arguments.
            self.i = start
            name_tok = self.peek()
            cname = self.qualified()[0]
            args = self.arguments()[0] if self.at_op("(") else []
            body = self.loop_body()
            self.construct("ora:cursor_for_loop", t.start, self.prev().end)
            out = []
            query = self.open_cursor(cname, args, name_tok.start)
            if query is not None:
                out.append(query)
            out.append({"op": "loop", "kind": "each", "at": self.at(t.start, self.prev().end), "var": {"e": "var", "scope": "local", "name": var.name},
                        "over": {"e": "var", "scope": "cursor", "name": cname}, "body": body})
            return out
        finally:
            self.scopes.pop()

    def exit_statement(self) -> dict[str, Any]:
        t = self.next()
        label = self.next().name if self.peek().kind in (IDENT, QUOTED) and not self.at_word("WHEN") else None
        node: dict[str, Any] = {"op": "break" if t.is_("EXIT") else "continue", "at": self.at(t.start, t.end)}
        if label:
            node["label"] = label
        if self.accept("WHEN"):
            cond = self.expression()
            self.expect_op(";")
            self.construct("ora:exit", t.start, self.prev().end)
            return {"op": "if", "at": self.at(t.start, self.prev().end), "cond": cond, "then": [node]}
        self.expect_op(";")
        self.construct("ora:exit", t.start, self.prev().end)
        return node

    def forall(self) -> dict[str, Any]:
        t = self.expect("FORALL")
        index = self.name_token().name
        self.expect("IN")
        detail: dict[str, Any] = {"index": index}
        self.scopes.append({index: "local"})
        try:
            if self.accept("INDICES"):
                self.expect("OF")
                detail["bounds"] = "indices_of"
                detail["collection"] = self.expression()
                if self.accept("BETWEEN"):
                    self.expression()
                    self.expect("AND")
                    self.expression()
            elif self.accept("VALUES"):
                self.expect("OF")
                detail["bounds"] = "values_of"
                detail["collection"] = self.expression()
            else:
                detail["bounds"] = "range"
                detail["from"] = self.expression()
                self.expect_op("..")
                detail["to"] = self.expression()
            if self.accept("SAVE"):
                self.expect("EXCEPTIONS")
                detail["save_exceptions"] = True
            if self.at_word("EXECUTE"):
                inner = self.execute_immediate()
            else:
                inner = self.sql_statement()
        finally:
            self.scopes.pop()
        self.construct("ora:forall", t.start, self.prev().end)
        return {"op": "platform", "construct": "ora:forall", "at": self.at(t.start, self.prev().end), "detail": detail, "body": [inner]}

    # -------------------------------------------------------------- cursors

    def open_cursor(self, name: str, args: list[dict[str, Any]], at_char: int) -> dict[str, Any] | None:
        """The query a declared cursor runs, with its arguments as parameters; None when the
        cursor is declared somewhere this file does not show."""
        cur = self.cursor(name)
        if cur is None:
            return None
        node = dict(cur["node"])
        params = list(node.get("params", []))
        for pname, value in zip(cur["params"], args):
            params = [p for p in params if p["name"] != pname] + [{"name": pname, "value": value}]
        if params:
            node["params"] = params
        node["target"] = {"e": "var", "scope": "cursor", "name": name}
        if self.body is not None:
            self.body.sql.append(cur["sql"])
            self.body.links += cur["links"]
        return node

    def open_statement(self) -> list[dict[str, Any]]:
        t = self.expect("OPEN")
        name_tok = self.peek()
        name = self.qualified()[0]
        if self.accept("FOR"):
            self.construct("ora:open_for", t.start, self.peek().end)
            if self.at_word("SELECT", "WITH"):
                query = self.capture_sql()
                self.expect_op(";")
                query["target"] = {"e": "var", "scope": self.scope_of(name) or "local", "name": name}
                return [query]
            text = self.expression()
            binds = self.using_clause()
            self.expect_op(";")
            node = self.dynamic_query(text, binds, t.start, self.prev().end, None)
            node["target"] = {"e": "var", "scope": self.scope_of(name) or "local", "name": name}
            return [node]
        args = self.arguments()[0] if self.at_op("(") else []
        self.expect_op(";")
        self.construct("ora:open_cursor", t.start, self.prev().end, self.cursor(name) is not None)
        query = self.open_cursor(name, args, name_tok.start)
        return [query] if query is not None else [{"op": "platform", "construct": "ora:open_cursor", "at": self.at(t.start, self.prev().end), "detail": {"cursor": name}}]

    def targets(self) -> dict[str, Any]:
        items = [self.primary()]
        while self.accept_op(","):
            items.append(self.primary())
        return items[0] if len(items) == 1 else {"e": "array", "items": items}

    def fetch(self) -> dict[str, Any]:
        t = self.expect("FETCH")
        name = self.qualified()[0]
        bulk = False
        if self.accept("BULK"):
            self.expect("COLLECT")
            bulk = True
            self.construct("ora:bulk_collect", t.start, self.prev().end)
        self.expect("INTO")
        target = self.targets()
        detail: dict[str, Any] = {"cursor": name}
        if bulk:
            detail["bulk"] = True
        if self.accept("LIMIT"):
            detail["limit"] = self.expression()
        self.expect_op(";")
        self.construct("ora:fetch", t.start, self.prev().end)
        return {"op": "assign", "at": self.at(t.start, self.prev().end), "target": target, "value": {"e": "platform", "construct": "ora:fetch", "detail": detail}}

    def using_clause(self) -> list[dict[str, Any]]:
        binds: list[dict[str, Any]] = []
        if self.accept("USING"):
            while True:
                mode = "in"
                if self.accept("IN"):
                    mode = "in_out" if self.accept("OUT") else "in"
                elif self.accept("OUT"):
                    mode = "out"
                value = self.expression()
                binds.append({"name": str(len(binds) + 1), "value": value, **({"mode": mode} if mode != "in" else {})})
                if not self.accept_op(","):
                    break
        return binds

    # -------------------------------------------------------------- SQL

    def sql_statement(self) -> dict[str, Any]:
        t = self.peek()
        node = self.capture_sql()
        self.expect_op(";")
        kind = self.last_kind
        cid ={"select": "ora:select_into", "with": "ora:select_into", "insert": "ora:insert", "update": "ora:update", "delete": "ora:delete", "merge": "ora:merge", "lock": "ora:lock_table"}[kind]
        self.construct(cid, t.start, self.prev().end)
        return node

    def capture_sql(self, until_paren: bool = False) -> dict[str, Any]:
        """A SQL statement from here to its `;` (or, inside `( ... )`, to the closing
        parenthesis), as a `query` node; the SQL is also recorded for lineage."""
        first = self.peek()
        j, depth = self.i, 0
        into: tuple[int, int] | None = None  # token indexes [INTO or BULK, FROM)
        returning: int | None = None
        while j < self.stop:
            t = self.toks[j]
            if t.kind == SLASH:
                break
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                if depth == 0 and until_paren:
                    break
                depth -= 1
            elif t.kind == OP and t.text == ";" and depth == 0:
                break
            elif depth == 0 and t.is_("INTO") and first.is_("SELECT", "WITH") and into is None:
                k = j - 2 if j >= 2 and self.toks[j - 1].is_("COLLECT") and self.toks[j - 2].is_("BULK") else j
                f = j + 1
                while f < self.stop and not self.toks[f].is_("FROM") and not (self.toks[f].kind == OP and self.toks[f].text == ";"):
                    f += 1
                into = (k, f)
            elif depth == 0 and t.is_("RETURNING", "RETURN") and first.is_("INSERT", "UPDATE", "DELETE", "MERGE") and returning is None:
                returning = j
            j += 1
        if j == self.i:
            raise ParseError(f"empty SQL statement at line {self.line(first.start)}")
        last = self.toks[j - 1]
        a, b = first.start, last.end
        kind = first.name
        node: dict[str, Any] = {"op": "query", "at": self.at(a, b)}
        blanks: list[tuple[int, int]] = []
        target = None
        bulk = False
        if into is not None:
            k, f = into
            bulk = self.toks[k].is_("BULK")
            saved_i, saved_stop = self.i, self.stop
            self.i, self.stop = (k + 3 if bulk else k + 1), f
            try:
                target = self.targets()
            finally:
                self.i, self.stop = saved_i, saved_stop
            blanks.append((self.toks[k].start, self.toks[f].start if f < len(self.toks) else b))
        if returning is not None:
            r = returning
            n = next((x for x in range(r, j) if self.toks[x].is_("INTO")), None)
            if n is not None:
                bulk = bulk or self.toks[n - 1].is_("COLLECT")
                saved_i, saved_stop = self.i, self.stop
                self.i, self.stop = n + 1, j
                try:
                    target = self.targets()
                finally:
                    self.i, self.stop = saved_i, saved_stop
                self.construct("ora:returning_into", self.toks[r].start, b)
            blanks.append((self.toks[r].start, b))
        if bulk:
            self.construct("ora:bulk_collect", first.start, b)
            node["bulk"] = True
        lineage_text = self.text[a:b]
        sql_text = lineage_text
        for x, y in sorted(blanks, reverse=True):
            lineage_text = lineage_text[: x - a] + " " * (y - x) + lineage_text[y - a :]
            sql_text = sql_text[: x - a].rstrip() + " " + sql_text[y - a :].lstrip()
        node["sql"] = scrub_text(sql_text.strip())
        if target is not None:
            node["target"] = target
        params: list[dict[str, Any]] = []
        seen: set[str] = set()
        links: list[tuple[str, int, int]] = []
        for x in range(self.i, j):
            t = self.toks[x]
            if any(p <= t.start < q for p, q in blanks):
                continue
            if t.kind == OP and t.text == "@" and x + 1 < j and self.toks[x + 1].kind in (IDENT, QUOTED):
                links.append((self.toks[x + 1].name, self.toks[x + 1].start, self.toks[x + 1].end))
            if x > self.i and self.toks[x - 1].kind == OP and self.toks[x - 1].text in (".", "@"):
                continue
            if t.kind == BIND and t.text[1:].lower() in ("new", "old") or (t.kind == BIND and self.trigger_names.get(t.text[1:].lower())):
                scope = self.trigger_names.get(t.text[1:].lower(), t.text[1:].lower())
                if x + 2 < j and self.toks[x + 1].text == "." and self.toks[x + 2].kind in (IDENT, QUOTED):
                    col = self.toks[x + 2].name
                    pname = f"{t.text[1:].lower()}.{col}"  # the placeholder as the SQL writes it
                    if pname not in seen:
                        seen.add(pname)
                        params.append({"name": pname, "value": {"e": "var", "scope": scope, "name": col}})
                continue
            if t.kind in (IDENT, QUOTED) and t.name in self.members and x + 1 < j and self.toks[x + 1].text == "(" and self.body is not None:
                # A function of this package called from SQL. Other names before `(` in SQL may be
                # tables with a column list, so only the package's own functions are read.
                self.body.calls.append(Call([t.name], t.start, t.end))
                self.construct("ora:procedure_call", t.start, t.end)
            if t.kind in (IDENT, QUOTED):
                scope = self.scope_of(t.name)
                if scope in ("arguments", "local", "package") and t.name not in seen:
                    seen.add(t.name)
                    params.append({"name": t.name, "value": {"e": "var", "scope": scope, "name": t.name}})
        if params:
            node["params"] = params
        self.i = j
        sql = Sql(kind, lineage_text, a, b)
        if self.body is not None:
            self.body.sql.append(sql)
            self.body.links += links
        # Remembered so that a cursor's query is lowered where the cursor is opened.
        self.last_sql = {"node": dict(node), "sql": sql, "links": links}
        self.last_kind = kind
        return node

    def execute_immediate(self) -> dict[str, Any]:
        t = self.expect("EXECUTE")
        self.expect("IMMEDIATE")
        text = self.expression()
        target = None
        if self.accept("BULK"):
            self.expect("COLLECT")
            self.expect("INTO")
            target = self.targets()
        elif self.accept("INTO"):
            target = self.targets()
        binds = self.using_clause()
        if self.accept("RETURNING", "RETURN"):
            self.accept("BULK") and self.expect("COLLECT")
            self.expect("INTO")
            target = self.targets()
        self.expect_op(";")
        node = self.dynamic_query(text, binds, t.start, self.prev().end, "ora:execute_immediate")
        if target is not None:
            node["target"] = target
        return node

    def dynamic_query(self, text: dict[str, Any], binds: list[dict[str, Any]], a: int, b: int, cid: str | None) -> dict[str, Any]:
        """SQL whose text is an expression: literal text is SQL like any other; text built at
        run time keeps its literal parts, with each computed part as an inline parameter."""
        parts = _concat_parts(text)
        node: dict[str, Any] = {"op": "query", "at": self.at(a, b), "dynamic": True}
        literal = all(isinstance(p, str) for p in parts)
        if literal:
            sql = "".join(parts).strip().rstrip(";")
            node["sql"] = scrub_text(sql)
            if binds:
                node["params"] = binds
            if self.body is not None and sql:
                # Offsets inside a string literal are not the file's: lineage cites the whole statement.
                self.body.sql.append(Sql(sql.split(None, 1)[0].lower(), sql, a, b, dynamic=True))
        else:
            pieces: list[str] = []
            params: list[dict[str, Any]] = []
            used: set[str] = set()
            for p in parts:
                if isinstance(p, str):
                    pieces.append(p)
                    continue
                base = p.get("name") if p.get("e") == "var" and isinstance(p.get("name"), str) else f"expr{len(params) + 1}"
                pname = re.sub(r"\W", "_", base)
                while pname in used:
                    pname += "_"
                used.add(pname)
                pieces.append(f":{pname}")
                params.append({"name": pname, "value": p, "inline": True})
            node["sql"] = scrub_text("".join(pieces).strip())
            node["params"] = params + binds
            self.note(f"dynamic SQL at line {self.line(a)}: its text is built at run time, so the tables it names are not read")
        if cid:
            self.construct(cid, a, b, literal)
        return node

    # -------------------------------------------------------------- scheduler jobs

    @staticmethod
    def _arg(args: list[dict[str, Any]], named: dict[str, Any], position: int, name: str) -> dict[str, Any] | None:
        if name in named:
            return named[name]
        return args[position] if position < len(args) else None

    @staticmethod
    def _literal(node: dict[str, Any] | None) -> Any:
        if isinstance(node, dict) and node.get("e") == "lit":
            return node.get("value")
        return None

    def scheduler(self, method: str, args: list[dict[str, Any]], named: dict[str, Any], a: int, b: int) -> None:
        named = {k.lower(): v for k, v in named.items()}
        if method == "create_job":
            name = self._literal(self._arg(args, named, 0, "job_name"))
            job = Job("dbms_scheduler", name.lower() if isinstance(name, str) else None, a, b)
            program = self._literal(named.get("program_name"))
            if program is not None:
                job.runs_program = str(program).lower()
                job.attrs["program_name"] = str(program).lower()
                schedule_name = self._literal(named.get("schedule_name"))
            else:
                job_type = self._literal(self._arg(args, named, 1, "job_type"))
                action = self._literal(self._arg(args, named, 2, "job_action"))
                if isinstance(job_type, str):
                    job.attrs["job_type"] = job_type.lower()
                job.runs = self.action_calls(job_type, action)
                schedule_name = self._literal(named.get("schedule_name"))
            interval = self._literal(named.get("repeat_interval", args[5] if len(args) > 5 and program is None else None))
            if isinstance(interval, str):
                job.attrs["schedule"] = interval
            if isinstance(schedule_name, str):
                job.attrs["schedule_name"] = schedule_name.lower()
            enabled = self._literal(named.get("enabled"))
            if isinstance(enabled, bool):
                job.attrs["enabled"] = enabled
            job_class = self._literal(named.get("job_class"))
            if isinstance(job_class, str):
                job.attrs["job_class"] = job_class.lower()
            if job.name is None:
                self.note(f"DBMS_SCHEDULER.CREATE_JOB at line {self.line(a)}: the job name is not literal, so no Job is declared")
            self.out.jobs.append(job)
            self.construct("ora:dbms_scheduler_job", a, b, job.name is not None)
        elif method == "create_program":
            name = self._literal(self._arg(args, named, 0, "program_name"))
            ptype = self._literal(self._arg(args, named, 1, "program_type"))
            action = self._literal(self._arg(args, named, 2, "program_action"))
            if isinstance(name, str) and isinstance(ptype, str):
                self.out.programs[name.lower()] = (ptype, action if isinstance(action, str) else "")
            self.construct("ora:dbms_scheduler_program", a, b, isinstance(name, str))
        elif method == "create_schedule":
            name = self._literal(self._arg(args, named, 0, "schedule_name"))
            interval = self._literal(named.get("repeat_interval", args[2] if len(args) > 2 else None))
            if isinstance(name, str) and isinstance(interval, str):
                self.out.schedules[name.lower()] = interval
            self.construct("ora:dbms_scheduler_schedule", a, b, isinstance(name, str))
        elif method in ("create_chain", "define_chain_step", "define_chain_rule", "define_chain_event_step", "run_chain"):
            self.construct("ora:dbms_scheduler_chain", a, b, False)
        else:
            self.construct("ora:dbms_scheduler_other", a, b, False)

    def action_calls(self, job_type: Any, action: Any) -> list[list[str]]:
        """The procedures a job's literal action runs: a stored procedure's name, or the calls of a PL/SQL block."""
        if not isinstance(job_type, str) or not isinstance(action, str):
            return []
        kind = job_type.upper()
        if kind == "STORED_PROCEDURE":
            parts = [p.strip().strip('"').lower() for p in action.split(".") if p.strip()]
            return [parts] if parts and all(re.fullmatch(r"[\w$#]+", p) for p in parts) else []
        if kind == "PLSQL_BLOCK":
            return block_calls(action)
        return []

    def dbms_job(self, method: str, args: list[dict[str, Any]], named: dict[str, Any], a: int, b: int) -> None:
        named = {k.lower(): v for k, v in named.items()}
        if method != "submit":
            self.construct("ora:dbms_scheduler_other", a, b, False)
            return
        what = self._literal(self._arg(args, named, 1, "what"))
        runs = block_calls(what) if isinstance(what, str) else []
        name = "; ".join(".".join(r) for r in runs) or None
        job = Job("dbms_job", name, a, b, runs=runs)
        interval = self._literal(self._arg(args, named, 3, "interval"))
        if isinstance(interval, str):
            job.attrs["schedule"] = interval
        if name is None:
            self.note(f"DBMS_JOB.SUBMIT at line {self.line(a)}: its WHAT is not a literal block of calls, so no Job is declared")
        self.out.jobs.append(job)
        self.construct("ora:dbms_job", a, b, name is not None)

    # -------------------------------------------------------------- triggers

    def trigger(self, start: int) -> None:
        name, a, b, _ = self.qualified()
        unit = Unit("trigger", name, start, b, (a, b))
        self.out.units.append(unit)
        self.construct("ora:create_trigger", start, b)
        if self.wrapped(unit):
            unit.end = self.prev().end
            return
        saved = self.i
        self.package, self.members = None, set()
        self.trigger_names = {}
        self.in_trigger = True
        try:
            self.trigger_rest(unit)
        except ParseError as exc:
            unit.error = str(exc)
            self.note(f"trigger {name}: structure not read ({exc}); its SQL is read for lineage only", "warning")
            self.i = saved
            self.skip_unit_to_slash()
        finally:
            self.trigger_names = {}
            self.in_trigger = False
            self.body = None
        unit.end = self.prev().end

    def trigger_rest(self, unit: Unit) -> None:
        timing_tok = self.next()
        timing = timing_tok.name
        if timing_tok.is_("INSTEAD"):
            self.expect("OF")
            timing = "instead of"
        elif not timing_tok.is_("BEFORE", "AFTER", "FOR"):
            raise ParseError(f"trigger timing expected at line {self.line(timing_tok.start)}")
        events: list[str] = []
        columns: list[str] = []
        while True:
            ev = self.name_token()
            events.append(ev.name)
            if ev.is_("UPDATE") and self.accept("OF"):
                columns.append(self.name_token().name)
                while self.accept_op(","):
                    columns.append(self.name_token().name)
            if not self.accept("OR"):
                break
        self.expect("ON")
        on: dict[str, Any] = {"events": events, "timing": timing}
        if self.accept("SCHEMA"):
            unit.attrs["scope"] = "schema"
        elif self.at_word("DATABASE", "PLUGGABLE"):
            self.accept("PLUGGABLE")
            self.expect("DATABASE")
            unit.attrs["scope"] = "database"
        else:
            if self.accept("NESTED"):
                self.expect("TABLE")
                self.name_token()
                self.expect("OF")
            on = {"table": self.qualified()[0], **on}
        if columns:
            on["columns"] = columns
        unit.attrs["on"] = on
        level = "statement"
        when = None
        compound = False
        while True:
            if self.accept("REFERENCING"):
                while self.at_word("NEW", "OLD", "PARENT"):
                    which = self.next().name
                    self.accept("AS")
                    self.trigger_names[self.name_token().name] = "new" if which == "new" else "old" if which == "old" else "parent"
            elif self.at_word("FOR") and self.peek(1).is_("EACH"):
                self.next()
                self.next()
                self.expect("ROW")
                level = "row"
            elif self.accept("FOLLOWS", "PRECEDES"):
                order = self.prev().name
                unit.attrs.setdefault(order, []).append(self.qualified()[0])
                while self.accept_op(","):
                    unit.attrs[order].append(self.qualified()[0])
            elif self.accept("ENABLE", "DISABLE"):
                if self.prev().is_("DISABLE"):
                    unit.attrs["enabled"] = False
            elif self.accept("FORWARD", "REVERSE", "CROSSEDITION"):
                pass
            elif self.at_word("WHEN"):
                self.next()
                self.expect_op("(")
                self.trigger_names.setdefault("new", "new")
                self.trigger_names.setdefault("old", "old")
                when = self.expression(bare_correlation=True)
                self.expect_op(")")
            elif self.at_word("COMPOUND"):
                self.next()
                self.expect("TRIGGER")
                compound = True
                break
            else:
                break
        unit.attrs["level"] = level
        body = Body()
        self.body = body
        self.scopes.append({})
        self.cursors.append({})
        try:
            if compound:
                self.construct("ora:compound_trigger", unit.start, self.prev().end)
                stmts = self.compound_sections()
                # A compound trigger's sections say when each part runs, at statement or row level.
                on["timing"] = "compound"
                unit.attrs["sections"] = [s["detail"]["timing"] for s in stmts if s.get("op") == "platform"]
                del unit.attrs["level"]
            elif self.at_word("CALL"):
                call = self.next()
                target = self.primary(statement=True)
                self.accept_op(";")
                stmts = [self.call_statement(target, call.start, self.prev().end)]
            else:
                stmts = self.block(label=None)
                self.expect_op(";")
        finally:
            self.scopes.pop()
            self.cursors.pop()
        if when is not None:
            stmts = [{"op": "if", "at": self.at(unit.start, self.prev().end), "cond": when, "then": stmts}]
        body.statements = stmts
        unit.body = body

    def compound_sections(self) -> list[dict[str, Any]]:
        out = self.declarations(unit=None, stops=("BEFORE", "AFTER", "INSTEAD", "END"))
        while self.at_word("BEFORE", "AFTER", "INSTEAD"):
            t = self.next()
            words = [t.name]
            while not self.at_word("IS"):
                words.append(self.next().name)
            self.expect("IS")
            stmts = self.block(label=None)
            while not self.at_op(";"):
                self.next()
            self.expect_op(";")
            out.append({"op": "platform", "construct": "ora:compound_trigger", "at": self.at(t.start, self.prev().end), "detail": {"timing": " ".join(words)}, "body": stmts})
        self.expect("END")
        if self.peek().kind in (IDENT, QUOTED):
            self.next()
        self.expect_op(";")
        return out

    # -------------------------------------------------------------- GRANT

    def grant(self) -> None:
        t = self.expect("GRANT")
        privileges: list[tuple[str, list[str]]] = []
        words: list[str] = []
        columns: list[str] = []
        while not self.at_word("ON", "TO") and not self.at_op(";") and self.peek() is not _EOF:
            x = self.next()
            if x.kind == OP and x.text == ",":
                privileges.append((" ".join(words), columns))
                words, columns = [], []
            elif x.kind == OP and x.text == "(":
                while not self.at_op(")"):
                    c = self.next()
                    if c.kind in (IDENT, QUOTED):
                        columns.append(c.name)
                self.expect_op(")")
            else:
                words.append(x.text.upper() if x.kind == IDENT else x.value)
        if words:
            privileges.append((" ".join(words), columns))
        obj = kind = None
        if self.accept("ON"):
            if self.at_word("DIRECTORY", "JAVA", "EDITION", "MINING") and self.peek(1).kind in (IDENT, QUOTED) and not self.peek(1).is_("TO"):
                kind = self.next().name
                if kind == "java":
                    self.accept("SOURCE", "RESOURCE", "CLASS")
                    kind = "java " + self.prev().name if self.prev().name != "java" else kind
            obj = self.qualified()[0]
        self.expect("TO")
        grantees = [self.name_token().name]
        while self.accept_op(","):
            grantees.append(self.name_token().name)
        option = False
        while not self.at_op(";") and self.peek() is not _EOF and self.peek().kind != SLASH:
            x = self.next()
            if x.is_("GRANT", "ADMIN") and self.at_word("OPTION"):
                option = True
        self.accept_op(";")
        self.out.grants.append(Grant(privileges, obj, kind, grantees, option, t.start, self.prev().end))

    # -------------------------------------------------------------- expressions

    def expression(self, bare_correlation: bool = False) -> dict[str, Any]:
        saved = getattr(self, "_bare", False)
        self._bare = bare_correlation
        try:
            return self.or_expr()
        finally:
            self._bare = saved

    def _bin(self, op: str, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        node = {"e": "binop", "op": op, "left": left, "right": right}
        if "at" in left and "at" in right:
            node["at"] = [left["at"][0], right["at"][1]]
        return node

    def or_expr(self) -> dict[str, Any]:
        left = self.and_expr()
        while self.accept("OR"):
            left = self._bin("or", left, self.and_expr())
        return left

    def and_expr(self) -> dict[str, Any]:
        left = self.not_expr()
        while self.at_word("AND"):
            self.next()
            left = self._bin("and", left, self.not_expr())
        return left

    def not_expr(self) -> dict[str, Any]:
        if self.accept("NOT"):
            return {"e": "unop", "op": "not", "operand": self.not_expr()}
        return self.comparison()

    def comparison(self) -> dict[str, Any]:
        left = self.additive()
        ops = {"=": "==", "<>": "!=", "!=": "!=", "~=": "!=", "^=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
        if self.peek().kind == OP and self.peek().text in ops:
            op = ops[self.next().text]
            return self._bin(op, left, self.additive())
        if self.at_word("IS"):
            self.next()
            negate = bool(self.accept("NOT"))
            if self.accept("NULL"):
                node: dict[str, Any] = {"e": "unop", "op": "missing", "operand": left}
            elif self.accept("EMPTY"):
                node = {"e": "unop", "op": "missing", "operand": left}
            else:
                raise ParseError(f"IS {self.peek().text} not read at line {self.line(self.peek().start)}")
            return {"e": "unop", "op": "not", "operand": node} if negate else node
        negate = False
        if self.at_word("NOT") and self.peek(1).is_("LIKE", "BETWEEN", "IN"):
            self.next()
            negate = True
        if self.accept("LIKE"):
            node = self._bin("like", left, self.additive())
            if self.accept("ESCAPE"):
                self.additive()
        elif self.accept("BETWEEN"):
            low = self.additive()
            self.expect("AND")
            high = self.additive()
            node = self._bin("and", self._bin(">=", left, low), self._bin("<=", left, high))
        elif self.accept("IN"):
            self.expect_op("(")
            items = [self.expression()]
            while self.accept_op(","):
                items.append(self.expression())
            self.expect_op(")")
            node = self._bin("in", left, {"e": "array", "items": items})
        else:
            return left
        return {"e": "unop", "op": "not", "operand": node} if negate else node

    def additive(self) -> dict[str, Any]:
        left = self.multiplicative()
        while self.at_op("+", "-", "||"):
            op = self.next().text
            left = self._bin("&" if op == "||" else op, left, self.multiplicative())
        return left

    def multiplicative(self) -> dict[str, Any]:
        left = self.power()
        while self.at_op("*", "/") or self.at_word("MOD"):
            t = self.next()
            left = self._bin("%" if t.is_("MOD") else t.text, left, self.power())
        return left

    def power(self) -> dict[str, Any]:
        left = self.unary()
        while self.at_op("**"):
            self.next()
            left = self._bin("^", left, self.unary())
        return left

    def unary(self) -> dict[str, Any]:
        if self.at_op("-", "+"):
            t = self.next()
            operand = self.unary()
            if t.text == "-" and operand.get("e") == "lit" and isinstance(operand.get("value"), (int, float)) and not isinstance(operand.get("value"), bool):
                return {**operand, "value": -operand["value"], "at": self.at(t.start, self.prev().end)}
            return {"e": "unop", "op": t.text, "operand": operand}
        return self.primary()

    def arguments(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        self.expect_op("(")
        args: list[dict[str, Any]] = []
        named: dict[str, dict[str, Any]] = {}
        while not self.at_op(")"):
            if self.peek().kind in (IDENT, QUOTED) and self.peek(1).kind == OP and self.peek(1).text == "=>":
                key = self.next().name
                self.next()
                named[key] = self.argument()
            else:
                args.append(self.argument())
            if not self.accept_op(","):
                break
        self.expect_op(")")
        return args, named

    def argument(self) -> dict[str, Any]:
        """One argument; special forms (CAST(x AS t), EXTRACT(y FROM d), TRIM(... FROM x),
        subqueries) are skipped to the next `,` or `)` and kept as a platform node."""
        start = self.i
        first = self.peek()
        if not first.is_("SELECT", "WITH"):
            try:
                value = self.expression()
                if self.at_op(",", ")"):
                    return value
            except ParseError:
                pass
        self.i = start
        depth = 0
        while self.peek() is not _EOF:
            t = self.peek()
            if depth == 0 and t.kind == OP and t.text in (",", ")"):
                break
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1
            self.i += 1
        if self.i == start:
            raise ParseError(f"empty argument at line {self.line(first.start)}")
        self.construct("ora:unrecognised_expression", first.start, self.prev().end, False)
        return {"e": "platform", "construct": "ora:unrecognised_expression", "at": self.at(first.start, self.prev().end)}

    def literal_of(self, t: Token) -> dict[str, Any]:
        if t.kind == NUMBER:
            raw = t.text.rstrip("fFdD")
            value: Any = int(raw) if re.fullmatch(r"\d+", raw) else float(raw)
            return {"e": "lit", "value": value, "type": "integer" if isinstance(value, int) else "number", "at": self.at(t.start, t.end)}
        return {"e": "lit", "value": scrub_text(t.value), "type": "string", "at": self.at(t.start, t.end)}

    def primary(self, statement: bool = False) -> dict[str, Any]:
        t = self.peek()
        if t.kind in (NUMBER, STRING):
            self.next()
            return self.literal_of(t)
        if t.kind == OP and t.text == "(":
            self.next()
            if self.at_word("SELECT", "WITH"):
                self.i -= 1
                return self.argument_skip_parens(t)
            inner = self.expression()
            if self.at_op(","):  # a row value: (a, b)
                items = [inner]
                while self.accept_op(","):
                    items.append(self.expression())
                inner = {"e": "array", "items": items}
            self.expect_op(")")
            return inner
        if t.kind == BIND:
            self.next()
            bind = t.text[1:].lower()
            scope = self.trigger_names.get(bind) or (bind if bind in ("new", "old") else None)
            if scope and self.at_op(".") and self.peek(1).kind in (IDENT, QUOTED):
                self.next()
                col = self.next()
                node: dict[str, Any] = {"e": "var", "scope": scope, "name": col.name, "at": self.at(t.start, col.end)}
                return self.postfix(node)
            return self.postfix({"e": "var", "scope": "bind", "name": bind, "at": self.at(t.start, t.end)})
        if t.kind not in (IDENT, QUOTED):
            raise ParseError(f"unexpected {t.text!r} at line {self.line(t.start)}")
        if t.kind == IDENT:
            up = t.up
            if up in ("TRUE", "FALSE"):
                self.next()
                return {"e": "lit", "value": up == "TRUE", "type": "boolean", "at": self.at(t.start, t.end)}
            if up == "NULL":
                self.next()
                return {"e": "lit", "value": None, "type": "null", "at": self.at(t.start, t.end)}
            if up in ("DATE", "TIMESTAMP") and self.peek(1).kind == STRING:
                self.next()
                s = self.next()
                return {"e": "lit", "value": s.value, "type": "date" if up == "DATE" else "datetime", "at": self.at(t.start, s.end)}
            if up == "INTERVAL" and self.peek(1).kind == STRING:
                self.next()
                s = self.next()
                while self.at_word("DAY", "HOUR", "MINUTE", "SECOND", "YEAR", "MONTH", "TO") or self.at_op("("):
                    if self.at_op("("):
                        self.skip_parens()
                    else:
                        self.next()
                return {"e": "lit", "value": s.value, "type": "string", "at": self.at(t.start, self.prev().end)}
            if up == "CASE":
                return self.case_expression()
            if up == "NEW" and self.peek(1).kind in (IDENT, QUOTED) and not self.peek(1).is_("IS"):
                self.next()
                type_name = self.qualified()[0]
                args = self.arguments()[0] if self.at_op("(") else []
                return {"e": "new", "type": type_name, "args": args, "at": self.at(t.start, self.prev().end)}
            if up in ("INSERTING", "UPDATING", "DELETING") and self.in_trigger:
                self.next()
                if up == "UPDATING" and self.at_op("("):
                    args = self.arguments()[0]
                    return {"e": "call", "callee": {"e": "name", "name": "updating", "kind": "builtin"}, "args": args, "at": self.at(t.start, self.prev().end)}
                return {"e": "var", "scope": "trigger", "name": up.lower(), "at": self.at(t.start, t.end)}
            if up == "SQL" and self.peek(1).kind == OP and self.peek(1).text == "%":
                self.next()
                return self.postfix({"e": "var", "scope": "cursor", "name": "sql", "at": self.at(t.start, t.end)})
        # A name, a dotted name, a call or a variable with members and indexes.
        first = self.next()
        if getattr(self, "_bare", False) and first.name in self.trigger_names and self.at_op("."):
            self.next()
            col = self.name_token()
            return self.postfix({"e": "var", "scope": self.trigger_names[first.name], "name": col.name, "at": self.at(first.start, col.end)})
        scope = self.scope_of(first.name)
        if scope is not None:
            return self.postfix({"e": "var", "scope": scope, "name": first.name, "at": self.at(first.start, first.end)})
        parts = [first.name]
        end = first.end
        while self.at_op(".") and self.peek(1).kind in (IDENT, QUOTED):
            self.next()
            nxt = self.next()
            parts.append(nxt.name)
            end = nxt.end
        remote = False
        if self.at_op("@") and self.peek(1).kind in (IDENT, QUOTED):
            self.next()
            self.qualified()
            remote = True
            end = self.prev().end
            self.construct("ora:remote_call", first.start, end, False)
        dotted = ".".join(parts)
        if parts[-1] in ("nextval", "currval") and len(parts) > 1:
            self.construct("ora:sequence", first.start, end, False)
            return self.postfix({"e": "member", "object": {"e": "name", "name": ".".join(parts[:-1]), "kind": "sequence"}, "name": parts[-1], "at": self.at(first.start, end)})
        if self.at_op("("):
            args, named = self.arguments()
            kind = "builtin" if system_name(parts) else ("procedure" if statement else "function")
            node = {"e": "call", "callee": {"e": "name", "name": dotted, "kind": kind}, "args": args, "at": self.at(first.start, self.prev().end)}
            if named:
                node["named"] = named
            if not remote:
                self.record_call(parts, first.start, end)
            return self.postfix(node)
        if self.at_op("%"):
            return self.postfix({"e": "var", "name": dotted, "at": self.at(first.start, end)})
        if len(parts) == 1 and parts[0] in _NILADIC:
            return {"e": "call", "callee": {"e": "name", "name": dotted, "kind": "builtin"}, "args": [], "at": self.at(first.start, end)}
        if statement and not self.at_op(":="):
            # `pkg.proc;` with no arguments
            self.record_call(parts, first.start, end)
            return {"e": "call", "callee": {"e": "name", "name": dotted, "kind": "procedure"}, "args": [], "at": self.at(first.start, end)}
        return {"e": "var", "name": dotted, "at": self.at(first.start, end)}

    def argument_skip_parens(self, t: Token) -> dict[str, Any]:
        self.skip_parens()
        self.construct("ora:unrecognised_expression", t.start, self.prev().end, False)
        return {"e": "platform", "construct": "ora:unrecognised_expression", "at": self.at(t.start, self.prev().end)}

    def postfix(self, node: dict[str, Any]) -> dict[str, Any]:
        """Members, indexes, method calls and cursor attributes after a variable."""
        while True:
            if self.at_op(".") and self.peek(1).kind in (IDENT, QUOTED):
                self.next()
                m = self.next()
                node = {"e": "member", "object": node, "name": m.name}
            elif self.at_op("("):
                args, named = self.arguments()
                if node.get("e") == "member":
                    node = {"e": "call", "callee": node, "args": args, **({"named": named} if named else {})}
                elif len(args) == 1 and not named:
                    node = {"e": "index", "object": node, "index": args[0]}
                else:
                    node = {"e": "call", "callee": node, "args": args, **({"named": named} if named else {})}
            elif self.at_op("%") and self.peek(1).kind == IDENT:
                pct = self.next()
                attr = self.next()
                self.construct("ora:cursor_attribute", pct.start, attr.end)
                node = {"e": "member", "object": node, "name": "%" + attr.name}
                if self.at_op("("):  # SQL%BULK_ROWCOUNT(i)
                    args, _ = self.arguments()
                    node = {"e": "index", "object": node, "index": args[0] if args else {"e": "lit", "value": None, "type": "null"}}
            else:
                return node

    def case_expression(self) -> dict[str, Any]:
        t = self.expect("CASE")
        selector = None if self.at_word("WHEN") else self.expression()
        arms: list[tuple[dict[str, Any], dict[str, Any]]] = []
        while self.accept("WHEN"):
            test = self.expression()
            self.expect("THEN")
            arms.append((self._bin("==", selector, test) if selector is not None else test, self.expression()))
        otherwise: dict[str, Any] = {"e": "lit", "value": None, "type": "null"}
        if self.accept("ELSE"):
            otherwise = self.expression()
        self.expect("END")
        node = otherwise
        for test, value in reversed(arms):
            node = {"e": "cond", "test": test, "then": value, "else": node}
        node = dict(node)
        node["at"] = self.at(t.start, self.prev().end)
        return node

    def record_call(self, parts: list[str], a: int, b: int) -> None:
        if self.body is None or system_name(parts):
            return
        if self.scope_of(parts[0]) is not None:
            return  # a method of a variable: t.extend, c.close
        self.body.calls.append(Call(parts, a, b))
        self.construct("ora:procedure_call", a, b)


def _concat_parts(node: dict[str, Any]) -> list[Any]:
    """A concatenation as its parts: literal text, and expressions for everything else."""
    if node.get("e") == "binop" and node.get("op") == "&":
        return _concat_parts(node["left"]) + _concat_parts(node["right"])
    if node.get("e") == "lit" and isinstance(node.get("value"), str):
        return [node["value"]]
    return [node]


def block_calls(text: str) -> list[list[str]]:
    """The procedures a literal PL/SQL block calls (a job's action): dotted names, in order."""
    parser = Parser(text if re.match(r"\s*(begin|declare)\b", text, re.I) else f"BEGIN {text} END;")
    try:
        parser.block_body(lambda: parser.block(label=None), 0)
    except ParseError:
        return []
    out: list[list[str]] = []
    for body, _, _ in parser.out.blocks:
        for call in body.calls:
            if call.parts not in out:
                out.append(call.parts)
    return out
