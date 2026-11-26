"""A routine body as behavioural IR (rkp-behavior/1).

One reader for the three languages, which share most of their statements. DB2 and Netezza
end every statement with `;`; Transact-SQL (Sybase) need not, so a Sybase statement ends
where the next one starts. SQL statements become `query` nodes: Sybase `@name` variables
are `:name` bind parameters, Netezza's positional `$n` too; DB2 and Netezza variables
named in SQL are kept as written (DB2 resolves a column first, so a rename could be wrong).
`SELECT ... INTO v` and `SELECT @v = ...` lose the assignment in the SQL text and name the
variable as the node's `target`. Text run with EXECUTE IMMEDIATE is a query when it is a
string literal and dynamic otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.dbproc.lexer import Tok, name_of, sql_text
from repo_intel.platforms.shell.emit import Out, P, program_ref

_DML = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "WITH", "UPSERT", "SEL", "INS", "UPD", "DEL"}
_DDL = {"CREATE", "DROP", "ALTER", "GRANT", "REVOKE", "LOCK", "COMMENT", "RENAME", "REFRESH", "GENERATE", "GROOM"}
# Transact-SQL statements: where one of these starts at the top level, the statement before ends.
_TSQL_START = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "DECLARE", "SET", "IF", "ELSE", "WHILE", "BEGIN", "END", "RETURN",
               "EXEC", "EXECUTE", "PRINT", "RAISERROR", "COMMIT", "ROLLBACK", "SAVE", "GOTO", "BREAK", "CONTINUE", "OPEN", "FETCH",
               "CLOSE", "DEALLOCATE", "WAITFOR", "CREATE", "DROP", "ALTER", "GRANT", "REVOKE", "GO", "USE", "DUMP", "READTEXT", "WRITETEXT"}
_GLOBALS = {"SQLSTATE", "SQLCODE", "ROW_COUNT", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "CURRENT_USER", "SESSION_USER", "USER", "SYSDATE", "NOW"}
_SYSTEM_SCHEMAS = ("sysproc.", "sysibm.", "sysfun.", "systools.", "sys.", "sysibmadm.", "admin.")
_TYPES = (("integer", ("INT", "INTEGER", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT", "SERIAL")),
          ("number", ("DECIMAL", "DEC", "NUMERIC", "NUMBER", "FLOAT", "DOUBLE", "REAL", "MONEY", "SMALLMONEY", "DECFLOAT")),
          ("string", ("CHAR", "CHARACTER", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "CLOB", "DBCLOB", "GRAPHIC", "VARGRAPHIC", "UNICHAR", "UNIVARCHAR", "LONG", "SYSNAME", "NATIONAL", "UNITEXT")),
          ("date", ("DATE",)),
          ("datetime", ("TIMESTAMP", "DATETIME", "SMALLDATETIME", "TIME", "BIGDATETIME", "BIGTIME")),
          ("boolean", ("BOOLEAN", "BOOL", "BIT")),
          ("binary", ("BLOB", "BINARY", "VARBINARY", "IMAGE")))


def ir_type(word: str) -> str:
    w = word.upper()
    return next((t for t, words in _TYPES if w in words), "unknown")


@dataclass
class Ctx:
    out: Out
    engine: str  # db2 | netezza | sybase
    me: dict[str, Any]
    aliases: dict[str, str] = field(default_factory=dict)  # Netezza `$n` -> name
    cursors: dict[str, tuple[str, list]] = field(default_factory=dict)  # name -> (sql, params)
    sqls: list[tuple[str, int, int]] = field(default_factory=list)  # every SQL text, for lineage
    result_sets: int = 0


class Lowering:
    """Statements of tokens toks[i:j] as behaviour nodes."""

    def __init__(self, ctx: Ctx, toks: list[Tok]):
        self.ctx, self.out, self.toks = ctx, ctx.out, toks
        self.i = 0
        self.sybase = ctx.engine == "sybase"

    # ------------------------------------------------------------ token helpers

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def up(self, k: int = 0) -> str:
        t = self.peek(k)
        return t.up if t is not None else ""

    def is_(self, *words: str) -> bool:
        return all(self.up(k) == w for k, w in enumerate(words))

    def eat(self, *words: str) -> bool:
        if self.is_(*words):
            self.i += len(words)
            return True
        return False

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def done(self) -> bool:
        return self.i >= len(self.toks)

    def at(self, i: int, j: int | None = None) -> list[int]:
        j = self.i if j is None else j
        a = self.toks[min(i, len(self.toks) - 1)].start if self.toks else 0
        b = self.toks[max(min(j, len(self.toks)) - 1, i)].end if self.toks else 0
        return self.out.at(a, b)

    def span(self, i: int, j: int) -> tuple[int, int]:
        if not self.toks:
            return 0, 0
        return self.toks[min(i, len(self.toks) - 1)].start, self.toks[max(min(j, len(self.toks)) - 1, i)].end

    def construct(self, name: str, i: int, modelled: bool = True) -> None:
        a, b = self.span(i, max(self.i, i + 1))
        self.out.construct(f"dbp:{name}", a, b, modelled)

    def semi(self) -> None:
        self.eat(";")

    def statement_end(self, i: int) -> int:
        """The index after the statement starting at toks[i]: its `;` (DB2, Netezza), or where
        the next Transact-SQL statement starts."""
        depth = case = 0
        first = self.toks[i].up if i < len(self.toks) else ""
        seen_set = seen_select = False
        j = i + 1
        while j < len(self.toks):
            t = self.toks[j]
            u = t.up
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                if depth == 0:
                    return j
                depth -= 1
            elif depth == 0 and t.text == ";":
                return j
            elif depth == 0 and t.kind == "id":
                if u == "CASE":
                    case += 1
                elif u == "END" and case:
                    case -= 1
                elif u == "END_PROC":
                    return j
                elif self.sybase and not case and u in _TSQL_START:
                    prev = self.toks[j - 1].up
                    if u == "SELECT" and (prev in ("UNION", "ALL", "EXCEPT", "INTERSECT", "FOR") or (first == "INSERT" and not seen_select)):
                        seen_select = True
                    elif u == "SET" and first == "UPDATE" and not seen_set:
                        seen_set = True
                    elif u in ("UPDATE", "DELETE", "INSERT") and prev in ("FOR", ",", "OF", "ON"):
                        pass
                    else:
                        return j
            j += 1
        return j

    # ------------------------------------------------------------ blocks

    def block(self, stops: set[str]) -> list[dict[str, Any]]:
        """Statements up to (not including) a token in `stops`, with DB2 handlers applied."""
        body: list[dict[str, Any]] = []
        exits: list[tuple[int, dict[str, Any]]] = []
        while not self.done() and self.up() not in stops:
            before = self.i
            for node in self.statement():
                if node.get("op") == "_exit_handler":
                    exits.append((len(body), node["catch"]))
                else:
                    body.append(node)
            if self.i == before:  # never stall on a token no statement takes
                self.i += 1
        for index, catch in reversed(exits):
            body = body[:index] + [{"op": "try", "body": body[index:], "catches": [catch]}]
        return body

    def compound(self) -> list[dict[str, Any]]:
        """BEGIN [ATOMIC] ... END (the BEGIN is current)."""
        start = self.i
        self.eat("BEGIN")
        self.eat("NOT", "ATOMIC") or self.eat("ATOMIC")
        body = self.block({"END", "EXCEPTION"})
        if self.up() == "EXCEPTION":
            body = [self.exception_block(body, start)]
        self.eat("END")
        if self.peek() is not None and self.peek().kind == "id" and self.up() not in _TSQL_START | {"ELSE", "WHEN", "END", "UNTIL", "EXCEPTION"} and not self.sybase:
            self.i += 1  # END label
        self.semi()
        return body

    def exception_block(self, body: list[dict[str, Any]], start: int) -> dict[str, Any]:
        """Netezza `EXCEPTION WHEN cond [OR cond] THEN stmts ...`."""
        self.eat("EXCEPTION")
        catches = []
        while self.eat("WHEN"):
            names = []
            while not self.done() and self.up() != "THEN":
                t = self.next()
                if t.kind == "id" and t.up != "OR":
                    names.append(t.text.lower())
            self.eat("THEN")
            catches.append({"type": " or ".join(names) or "others", "body": self.block({"WHEN", "END"})})
        self.construct("exception-block", start)
        return {"op": "try", "at": self.at(start), "body": body, "catches": catches}

    # ------------------------------------------------------------ statements

    def statement(self) -> list[dict[str, Any]]:
        t = self.peek()
        if t is None:
            return []
        if t.text in (";", "@") or t.up == "GO":
            self.i += 1
            return []
        start = self.i
        u = t.up
        # A label (`name:` before a statement; Transact-SQL `name:` is a GOTO target)
        if t.kind == "id" and self.peek(1) is not None and self.peek(1).text == ":":
            self.i += 2
            if self.sybase:
                self.construct("goto", start, False)
            nodes = self.statement()
            for n in nodes[:1]:
                if n.get("op") == "loop":
                    n["label"] = t.text.lower()
            return nodes
        handler = getattr(self, "s_" + u.lower(), None) if t.kind == "id" else None
        if handler is not None:
            return handler(start)
        if t.kind in ("id", "var") and self.peek(1) is not None and self.peek(1).text == ":=":
            return self.s_assign_netezza(start)
        return self.unparsed(start)

    def unparsed(self, start: int) -> list[dict[str, Any]]:
        end = self.statement_end(start)
        self.i = max(end, start + 1)
        self.semi()
        self.construct("unparsed", start, False)
        word = self.toks[start].text
        self.out.note(f"a statement starting with {word[:30]!r} was not understood; it is kept as a platform node")
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:unparsed"}]

    # -- compound and declarations

    def s_begin(self, start: int) -> list[dict[str, Any]]:
        if self.is_("BEGIN", "TRAN") or self.is_("BEGIN", "TRANSACTION") or self.is_("BEGIN", "WORK"):
            self.i += 2
            if self.peek() is not None and self.peek().kind == "id" and self.up() not in _TSQL_START:
                self.i += 1
            self.semi()
            self.construct("transaction", start)
            return [{"op": "transaction", "at": self.at(start), "action": "begin", "body": []}]
        return self.compound()

    def s_declare(self, start: int) -> list[dict[str, Any]]:
        self.eat("DECLARE")
        if self.ctx.engine == "netezza":
            return self.netezza_declarations(start)
        if self.is_("GLOBAL", "TEMPORARY") or self.is_("GLOBAL", "TEMP"):
            self.i = start
            return self.sql_statement(start, construct="temp-table")
        if self.up() in ("CONTINUE", "EXIT", "UNDO") and self.up(1) == "HANDLER":
            return self.handler(start)
        nodes: list[dict[str, Any]] = []
        names: list[str] = []
        while not self.done():
            t = self.next()
            names.append(t.text.lstrip("@").lower() if t.kind == "var" else t.text.lower())
            if self.up() == "CONDITION":
                end = self.statement_end(start)
                self.i = end
                self.semi()
                self.construct("declare-condition", start)
                return [{"op": "declare", "at": self.at(start), "name": names[0], "value": {"e": "platform", "construct": "dbp:condition"}}]
            if self.up() in ("CURSOR", "SCROLL", "INSENSITIVE", "ASENSITIVE", "SENSITIVE", "NO"):
                return self.cursor(start, names[0])
            if self.eat(","):
                continue
            break
        # the type, then an optional DEFAULT (DB2) or nothing (Transact-SQL)
        type_start = self.i
        value = None
        while not self.done():
            if self.up() in ("DEFAULT",) or (self.peek().text == "=" and not self.sybase):
                self.i += 1
                value = self.expr()
                break
            if self.peek().text == ";" or (self.sybase and self.peek().text == ","):
                break
            if self.sybase and self.peek().kind == "id" and self.up() in _TSQL_START:
                break
            if self.peek().text == "(":
                self.skip_parens()
                continue
            self.i += 1
        type_word = self.toks[type_start].up if type_start < len(self.toks) else ""
        for n in names:
            node: dict[str, Any] = {"op": "declare", "at": self.at(start), "name": n, "type": ir_type(type_word)}
            if value is not None:
                node["value"] = value
            nodes.append(node)
        if self.sybase and self.eat(","):  # DECLARE @a int, @b int
            more = self.s_declare_rest(start)
            nodes += more
        self.semi()
        self.construct("declare", start)
        return nodes

    def s_declare_rest(self, start: int) -> list[dict[str, Any]]:
        """The rest of a Transact-SQL `DECLARE @a type, @b type`."""
        fake = self.i
        out: list[dict[str, Any]] = []
        while not self.done() and self.peek().kind == "var":
            name = self.next().text.lstrip("@").lower()
            type_word = self.up()
            while not self.done() and self.peek().text not in (",", ";") and not (self.peek().kind == "id" and self.up() in _TSQL_START) and self.peek().kind != "var":
                if self.peek().text == "(":
                    self.skip_parens()
                else:
                    self.i += 1
            out.append({"op": "declare", "at": self.at(fake), "name": name, "type": ir_type(type_word)})
            if not self.eat(","):
                break
        return out

    def netezza_declarations(self, start: int) -> list[dict[str, Any]]:
        """`DECLARE name type [:= expr]; name ALIAS FOR $1; ...` up to BEGIN."""
        nodes: list[dict[str, Any]] = []
        while not self.done() and self.up() not in ("BEGIN", "END_PROC"):
            s = self.i
            name_tok = self.next()
            name = name_tok.text.lower()
            if self.eat("ALIAS", "FOR"):
                pos = self.next().text
                self.ctx.aliases[pos] = name
                self.semi()
                self.out.construct("dbp:alias", *self.span(s, self.i), True)
                nodes.append({"op": "declare", "at": self.at(s), "name": name, "value": {"e": "var", "scope": "arguments", "name": _positional(pos)}})
                continue
            self.eat("CONSTANT")
            type_word = self.up()
            value = None
            while not self.done() and self.peek().text != ";":
                if self.peek().text in (":=", "=") or self.up() == "DEFAULT":
                    self.i += 1
                    value = self.expr()
                    continue
                if self.peek().text == "(":
                    self.skip_parens()
                    continue
                self.i += 1
            self.semi()
            node: dict[str, Any] = {"op": "declare", "at": self.at(s), "name": name, "type": ir_type(type_word)}
            if value is not None:
                node["value"] = value
            nodes.append(node)
            self.out.construct("dbp:declare", *self.span(s, self.i), True)
        return nodes

    def cursor(self, start: int, name: str) -> list[dict[str, Any]]:
        while not self.done() and not self.is_("FOR") and self.peek().text != ";":
            if self.is_("WITH", "RETURN"):
                self.ctx.result_sets += 1
            self.i += 1
        self.eat("FOR")
        if self.up() in _DML:
            s = self.i
            end = self.statement_end(s)
            toks = self.toks[s:end]
            sql, params = self.sql_of(_without_for_clause(toks))
            self.ctx.cursors[name] = (sql, params)
            self.ctx.sqls.append((sql, *self.span(s, end)))
            self.i = end
        else:  # FOR a prepared statement: the text is built at run time
            self.i = self.statement_end(self.i)
            self.ctx.cursors[name] = ("", [])
        self.semi()
        self.construct("declare-cursor", start)
        return [{"op": "declare", "at": self.at(start), "name": name, "value": {"e": "platform", "construct": "dbp:cursor"}}]

    def handler(self, start: int) -> list[dict[str, Any]]:
        kind = self.next().up.lower()
        self.eat("HANDLER")
        self.eat("FOR")
        conditions = []
        while not self.done():
            if self.eat("NOT", "FOUND"):
                conditions.append("not found")
            elif self.eat("SQLSTATE"):
                self.eat("VALUE")
                conditions.append("sqlstate " + self.next().value())
            else:
                conditions.append(self.next().text.lower())
            if not self.eat(","):
                break
        body = self.statement() if self.up() != "BEGIN" else self.compound()
        self.construct("handler", start)
        catch = {"type": " or ".join(conditions), "body": body}
        if kind in ("exit", "undo"):
            return [{"op": "_exit_handler", "catch": catch}]
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:handler", "detail": {"kind": kind, "conditions": conditions}, "body": body}]

    # -- assignment

    def s_set(self, start: int) -> list[dict[str, Any]]:
        self.eat("SET")
        t = self.peek()
        if t is None:
            return []
        if self.sybase and t.kind == "id":  # SET NOCOUNT ON, SET ROWCOUNT n, SET CHAINED OFF
            end = self.statement_end(start)
            self.i = end
            self.semi()
            self.construct("session-option", start, False)
            return [{"op": "platform", "at": self.at(start), "construct": "dbp:session-option", "detail": {"option": t.text.lower()}}]
        if t.text == "(":  # SET (a, b) = (SELECT ...) or (x, y)
            close = self.matching(self.i)
            targets = [self.var_of(x) for x in self.toks[self.i + 1:close] if x.text != ","]
            self.i = close + 1
            self.eat("=")
            node = self.assign_value(start, {"e": "array", "items": targets})
            return [node]
        nodes = []
        while True:
            target = self.var_of(self.next())
            while self.peek() is not None and self.peek().text == ".":  # SET rec.field (rare)
                self.i += 2
            self.eat("=")
            nodes.append(self.assign_value(start, target))
            if not self.eat(","):
                break
        self.semi()
        return nodes

    def s_assign_netezza(self, start: int) -> list[dict[str, Any]]:
        target = self.var_of(self.next())
        self.eat(":=")
        node = self.assign_value(start, target)
        self.semi()
        return [node]

    def assign_value(self, start: int, target: dict[str, Any]) -> dict[str, Any]:
        """`target = <expr>`; a parenthesised SELECT on the right is a query into the target."""
        if self.peek() is not None and self.peek().text == "(" and self.up(1) in ("SELECT", "WITH", "VALUES"):
            close = self.matching(self.i)
            toks = self.toks[self.i + 1:close]
            self.i = close + 1
            self.construct("select-into", start)
            return self.query_node(start, toks, target=target)
        value = self.expr()
        self.construct("assign", start)
        return {"op": "assign", "at": self.at(start), "target": target, "value": value}

    def s_values(self, start: int) -> list[dict[str, Any]]:
        """DB2 `VALUES expr INTO v`."""
        end = self.statement_end(start)
        into = next((k for k in range(start, end) if self.toks[k].up == "INTO"), None)
        if into is None:
            return self.sql_statement(start)
        self.i += 1
        value = self.expr()
        self.i = into + 1
        target = self.var_of(self.next())
        self.i = end
        self.semi()
        self.construct("assign", start)
        return [{"op": "assign", "at": self.at(start), "target": target, "value": value}]

    # -- control flow

    def s_if(self, start: int) -> list[dict[str, Any]]:
        self.eat("IF")
        cond = self.expr()
        if self.sybase:
            then = self.single()
            other = None
            if self.eat("ELSE"):
                other = self.single()
            self.construct("if", start)
            node = {"op": "if", "at": self.at(start), "cond": cond, "then": then}
            if other:
                node["else"] = other
            return [node]
        self.eat("THEN")
        then = self.block({"ELSEIF", "ELSIF", "ELSE", "END"})
        node = {"op": "if", "at": self.at(start), "cond": cond, "then": then}
        tail = node
        while self.up() in ("ELSEIF", "ELSIF"):
            s = self.i
            self.i += 1
            c = self.expr()
            self.eat("THEN")
            branch = {"op": "if", "at": self.at(s), "cond": c, "then": self.block({"ELSEIF", "ELSIF", "ELSE", "END"})}
            tail["else"] = [branch]
            tail = branch
        if self.eat("ELSE"):
            tail["else"] = self.block({"END"})
        self.eat("END", "IF")
        self.semi()
        self.construct("if", start)
        return [node]

    def single(self) -> list[dict[str, Any]]:
        """One Transact-SQL statement or a BEGIN ... END block (the body of IF or WHILE)."""
        if self.is_("BEGIN") and self.up(1) not in ("TRAN", "TRANSACTION", "WORK"):
            return self.compound()
        before = self.i
        nodes = self.statement()
        if self.i == before:
            self.i += 1
        return nodes

    def s_case(self, start: int) -> list[dict[str, Any]]:
        self.eat("CASE")
        on = None
        if self.up() != "WHEN":
            on = self.expr()
        cases, ifs = [], []
        while self.eat("WHEN"):
            match = self.expr()
            self.eat("THEN")
            body = self.block({"WHEN", "ELSE", "END"})
            (cases if on is not None else ifs).append((match, body))
        default = self.block({"END"}) if self.eat("ELSE") else []
        self.eat("END", "CASE") or self.eat("END")
        self.semi()
        self.construct("case", start)
        if on is not None:
            node: dict[str, Any] = {"op": "switch", "at": self.at(start), "on": on, "cases": [{"match": [m], "body": b} for m, b in cases]}
            if default:
                node["default"] = default
            return [node]
        chain: list[dict[str, Any]] = default
        for cond, body in reversed(ifs):
            n = {"op": "if", "at": self.at(start), "cond": cond, "then": body}
            if chain:
                n["else"] = chain
            chain = [n]
        return chain

    def s_while(self, start: int) -> list[dict[str, Any]]:
        self.eat("WHILE")
        cond = self.expr()
        if self.sybase:
            body = self.single()
        elif self.eat("DO"):
            body = self.block({"END"})
            self.eat("END", "WHILE")
            self.label_after()
        else:
            self.eat("LOOP")
            body = self.block({"END"})
            self.eat("END", "LOOP")
        self.semi()
        self.construct("while", start)
        return [{"op": "loop", "at": self.at(start), "kind": "while", "cond": cond, "body": body}]

    def label_after(self) -> None:
        if self.peek() is not None and self.peek().kind == "id" and self.peek().text != ";":
            self.i += 1

    def s_loop(self, start: int) -> list[dict[str, Any]]:
        self.eat("LOOP")
        body = self.block({"END"})
        self.eat("END", "LOOP")
        self.label_after()
        self.semi()
        self.construct("loop", start)
        return [{"op": "loop", "at": self.at(start), "kind": "while", "cond": {"e": "lit", "value": True, "type": "boolean"}, "body": body}]

    def s_repeat(self, start: int) -> list[dict[str, Any]]:
        self.eat("REPEAT")
        body = self.block({"UNTIL"})
        self.eat("UNTIL")
        cond = self.expr()
        self.eat("END", "REPEAT")
        self.label_after()
        self.semi()
        self.construct("repeat", start)
        return [{"op": "loop", "at": self.at(start), "kind": "until", "cond": cond, "body": body}]

    def s_for(self, start: int) -> list[dict[str, Any]]:
        self.eat("FOR")
        var = self.next().text.lower()
        nodes: list[dict[str, Any]] = []
        if self.eat("AS"):  # DB2: FOR v AS [c CURSOR [WITH HOLD] FOR] select DO ... END FOR
            cursor = var
            if self.up(1) == "CURSOR" or self.up(1) == "INSENSITIVE":
                cursor = self.next().text.lower()
                while not self.done() and self.up() != "FOR":
                    self.i += 1
                self.eat("FOR")
            s = self.i
            end = next((k for k in range(s, len(self.toks)) if self.toks[k].up == "DO"), len(self.toks))
            nodes.append(self.query_node(s, self.toks[s:end], target={"e": "var", "scope": "local", "name": cursor}))
            self.i = end
            self.eat("DO")
            body = self.block({"END"})
            self.eat("END", "FOR")
            self.label_after()
            self.semi()
            self.construct("for", start)
            nodes.append({"op": "loop", "at": self.at(start), "kind": "each", "var": {"e": "var", "scope": "local", "name": var},
                          "over": {"e": "var", "scope": "local", "name": cursor}, "body": body})
            return nodes
        self.eat("IN")
        self.eat("REVERSE")
        if self.up() in _DML or self.up() == "EXECUTE":  # Netezza: FOR rec IN select LOOP
            dynamic = self.up() == "EXECUTE"
            s = self.i
            end = next((k for k in range(s, len(self.toks)) if self.toks[k].up == "LOOP"), len(self.toks))
            if dynamic:
                self.construct("dynamic-sql", s, False)
                self.out.note("a FOR loop runs a query built at run time: its tables are not resolved")
                over: dict[str, Any] = {"e": "platform", "construct": "dbp:dynamic-sql"}
            else:
                nodes.append(self.query_node(s, self.toks[s:end], target={"e": "var", "scope": "local", "name": var + "_rows"}))
                over = {"e": "var", "scope": "local", "name": var + "_rows"}
            self.i = end
            self.eat("LOOP")
            body = self.block({"END"})
            self.eat("END", "LOOP")
            self.semi()
            self.construct("for", start)
            nodes.append({"op": "loop", "at": self.at(start), "kind": "each", "var": {"e": "var", "scope": "local", "name": var}, "over": over, "body": body})
            return nodes
        low = self.expr()
        self.eat("..")
        high = self.expr()
        self.eat("LOOP")
        body = self.block({"END"})
        self.eat("END", "LOOP")
        self.semi()
        self.construct("for", start)
        return [{"op": "loop", "at": self.at(start), "kind": "range", "var": {"e": "var", "scope": "local", "name": var}, "from": low, "to": high, "body": body}]

    def jump(self, start: int, op: str, construct: str) -> list[dict[str, Any]]:
        self.i += 1
        label = None
        if self.peek() is not None and self.peek().kind == "id" and self.up() not in ("WHEN",) and self.peek().text != ";" and not (self.sybase and self.up() in _TSQL_START):
            label = self.next().text.lower()
        node: dict[str, Any] = {"op": op, "at": self.at(start)}
        if label:
            node["label"] = label
        result = [node]
        if self.eat("WHEN"):  # Netezza: EXIT WHEN cond
            cond = self.expr()
            result = [{"op": "if", "at": self.at(start), "cond": cond, "then": [node]}]
        self.semi()
        self.construct(construct, start)
        return result

    def s_leave(self, start: int) -> list[dict[str, Any]]:
        return self.jump(start, "break", "leave")

    def s_exit(self, start: int) -> list[dict[str, Any]]:
        return self.jump(start, "break", "leave")

    def s_break(self, start: int) -> list[dict[str, Any]]:
        return self.jump(start, "break", "leave")

    def s_iterate(self, start: int) -> list[dict[str, Any]]:
        return self.jump(start, "continue", "iterate")

    def s_continue(self, start: int) -> list[dict[str, Any]]:
        return self.jump(start, "continue", "iterate")

    def s_goto(self, start: int) -> list[dict[str, Any]]:
        self.i += 2
        self.semi()
        self.construct("goto", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:goto", "detail": {"label": self.toks[start + 1].text.lower() if start + 1 < len(self.toks) else ""}}]

    def s_return(self, start: int) -> list[dict[str, Any]]:
        self.eat("RETURN")
        node: dict[str, Any] = {"op": "return", "at": self.at(start)}
        t = self.peek()
        if t is not None and t.text != ";" and not (t.kind == "id" and t.up in _TSQL_START | {"END", "ELSE", "END_PROC"}):
            if t.text == "(" and self.up(1) in ("SELECT", "WITH"):
                close = self.matching(self.i)
                node["value"] = self.subquery(self.toks[self.i + 1:close], self.i)
                self.i = close + 1
            elif t.up in ("SELECT", "WITH"):  # DB2 table function: RETURN SELECT ...
                end = self.statement_end(self.i)
                node["value"] = self.subquery(self.toks[self.i:end], self.i)
                self.i = end
            else:
                node["value"] = self.expr()
        self.semi()
        self.construct("return", start)
        return [node]

    # -- calls and dynamic SQL

    def s_call(self, start: int) -> list[dict[str, Any]]:
        self.eat("CALL")
        name_toks = self.dotted()
        name = name_of(name_toks)
        args = []
        if self.peek() is not None and self.peek().text == "(":
            args = self.args()
        self.semi()
        return [self.call_node(start, name, args)]

    def call_node(self, start: int, name: str, args: list) -> dict[str, Any]:
        if name and not name.startswith(_SYSTEM_SCHEMAS) and name not in ("sp_executesql",):
            a, b = self.span(start, self.i)
            self.out.rel(a, b, self.ctx.me, P.CALLS, program_ref(self.ctx.engine, name, declare=False))
        self.construct("call", start)
        return {"op": "call", "at": self.at(start), "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": args}}

    def s_exec(self, start: int) -> list[dict[str, Any]]:
        return self.s_execute(start)

    def s_execute(self, start: int) -> list[dict[str, Any]]:
        self.i += 1
        if self.eat("IMMEDIATE") or (self.peek() is not None and self.peek().text == "("):
            return self.dynamic_sql(start)
        if not self.sybase:  # DB2: EXECUTE prepared_statement [INTO ...] [USING ...]
            self.i = self.statement_end(start)
            self.semi()
            self.construct("dynamic-sql", start, False)
            self.out.note("EXECUTE runs a statement prepared from text built at run time: its tables are not resolved")
            return [{"op": "platform", "at": self.at(start), "construct": "dbp:dynamic-sql"}]
        target = None
        if self.peek() is not None and self.peek().kind == "var" and self.peek(1) is not None and self.peek(1).text == "=":
            target = self.var_of(self.next())
            self.i += 1
        if self.peek() is not None and self.peek().kind == "var":  # EXEC @proc_name: the procedure is chosen at run time
            self.i = self.statement_end(start)
            self.construct("dynamic-sql", start, False)
            self.out.note("EXEC of a procedure named in a variable: the procedure is not resolved")
            return [{"op": "platform", "at": self.at(start), "construct": "dbp:dynamic-sql"}]
        name = name_of(self.dotted())
        end = self.statement_end(start)
        args = []
        while self.i < end:
            if self.peek().kind == "var" and self.peek(1) is not None and self.peek(1).text == "=":
                self.i += 2
            args.append(self.expr())
            self.eat("OUTPUT") or self.eat("OUT")
            if not self.eat(","):
                break
        self.i = max(self.i, end)
        self.semi()
        node = self.call_node(start, name, args)
        if target is not None:
            return [{"op": "assign", "at": self.at(start), "target": target, "value": node["call"]}]
        return [node]

    def dynamic_sql(self, start: int) -> list[dict[str, Any]]:
        """EXECUTE IMMEDIATE <expr> / EXEC (<expr>): a literal is a query, anything else dynamic."""
        paren = self.peek() is not None and self.peek().text == "("
        if paren:
            close = self.matching(self.i)
            inner = self.toks[self.i + 1:close]
            self.i = close + 1
        else:
            end = self.statement_end(self.i)
            inner = self.toks[self.i:end]
            self.i = end
        self.semi()
        if len(inner) == 1 and inner[0].kind == "str":
            from repo_intel.platforms.dbproc.lexer import tokens

            text = inner[0].value()
            toks = tokens(text, double_quoted_strings=self.sybase)
            sql, params = self.sql_of(toks)
            self.ctx.sqls.append((sql, inner[0].start, inner[0].end))
            self.construct("dml", start)
            return [{"op": "query", "at": self.at(start), "sql": sql, **({"params": params} if params else {})}]
        sub = Lowering(self.ctx, inner)
        value = sub.expr() if inner else {"e": "lit", "value": None, "type": "null"}
        self.construct("dynamic-sql", start, False)
        self.out.note("EXECUTE IMMEDIATE runs text built at run time: its tables are not resolved")
        self.out.unresolved += 1
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:dynamic-sql", "detail": {"text": value}}]

    def s_prepare(self, start: int) -> list[dict[str, Any]]:
        self.i = self.statement_end(start)
        self.semi()
        self.construct("dynamic-sql", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:dynamic-sql", "detail": {"prepare": True}}]

    # -- errors and output

    def s_signal(self, start: int) -> list[dict[str, Any]]:
        self.i += 1
        fields: dict[str, Any] = {}
        if self.eat("SQLSTATE"):
            self.eat("VALUE")
            if self.peek() is not None and self.peek().kind == "str":
                fields["sqlstate"] = {"e": "lit", "value": self.next().value(), "type": "string"}
        elif self.peek() is not None and self.peek().kind == "id" and self.up() != "SET":
            fields["condition"] = {"e": "lit", "value": self.next().text.lower(), "type": "string"}
        if self.eat("SET"):
            while not self.done() and self.peek().text != ";":
                key = self.next().text.lower()
                self.eat("=")
                fields[key] = self.expr()
                if not self.eat(","):
                    break
        elif self.peek() is not None and self.peek().text == "(":  # DB2 v7 style SIGNAL SQLSTATE 'x' ('text')
            self.i += 1
            fields["message_text"] = self.expr()
            self.eat(")")
        self.semi()
        self.construct("signal", start)
        node: dict[str, Any] = {"op": "throw", "at": self.at(start)}
        if fields:
            node["value"] = {"e": "struct", "fields": fields}
        return [node]

    def s_resignal(self, start: int) -> list[dict[str, Any]]:
        return self.s_signal(start)

    def s_raise(self, start: int) -> list[dict[str, Any]]:
        """Netezza `RAISE level 'format', args`."""
        self.eat("RAISE")
        level = self.next().up if self.peek() is not None and self.peek().kind == "id" else "EXCEPTION"
        args = []
        while not self.done() and self.peek().text != ";":
            args.append(self.expr())
            if not self.eat(","):
                break
        self.semi()
        message = args[0] if len(args) == 1 else {"e": "call", "callee": {"e": "name", "name": "format", "kind": "builtin"}, "args": args}
        if level == "EXCEPTION":
            self.construct("signal", start)
            return [{"op": "throw", "at": self.at(start), "value": message}]
        self.construct("print", start)
        return [{"op": "output", "at": self.at(start), "channel": "log", "value": message}]

    def s_raiserror(self, start: int) -> list[dict[str, Any]]:
        self.eat("RAISERROR")
        end = self.statement_end(start)
        args = []
        if self.peek() is not None and self.peek().text == "(":
            args = self.args()
        else:  # raiserror number [message] [, args]
            while self.i < end:
                args.append(self.expr())
                if self.eat(","):
                    continue
                if len(args) == 1 and self.peek() is not None and self.peek().kind in ("str", "var") and self.i < end:
                    continue
                break
        self.i = max(self.i, end)
        self.semi()
        self.construct("signal", start)
        return [{"op": "throw", "at": self.at(start), "value": {"e": "call", "callee": {"e": "name", "name": "raiserror", "kind": "builtin"}, "args": args}}]

    def s_print(self, start: int) -> list[dict[str, Any]]:
        self.eat("PRINT")
        end = self.statement_end(start)
        args = []
        while self.i < end:
            args.append(self.expr())
            if not self.eat(","):
                break
        self.i = max(self.i, end)
        self.semi()
        self.construct("print", start)
        value = args[0] if len(args) == 1 else {"e": "call", "callee": {"e": "name", "name": "format", "kind": "builtin"}, "args": args}
        return [{"op": "output", "at": self.at(start), "channel": "log", "value": value}]

    # -- transactions and cursors

    def transaction(self, start: int, action: str) -> list[dict[str, Any]]:
        self.i = self.statement_end(start)
        self.semi()
        self.construct("transaction", start)
        return [{"op": "transaction", "at": self.at(start), "action": action, "body": []}]

    def s_commit(self, start: int) -> list[dict[str, Any]]:
        return self.transaction(start, "commit")

    def s_rollback(self, start: int) -> list[dict[str, Any]]:
        return self.transaction(start, "rollback")

    def s_savepoint(self, start: int) -> list[dict[str, Any]]:
        return self.transaction(start, "savepoint")

    def s_save(self, start: int) -> list[dict[str, Any]]:
        return self.transaction(start, "savepoint")

    def s_open(self, start: int) -> list[dict[str, Any]]:
        self.eat("OPEN")
        name = self.next().text.lower() if not self.done() else ""
        self.i = self.statement_end(start)
        self.semi()
        self.construct("open", start)
        sql, params = self.ctx.cursors.get(name, ("", []))
        if not sql:
            self.out.note(f"cursor {name} runs a query that is not a literal SELECT here: not resolved")
            return [{"op": "platform", "at": self.at(start), "construct": "dbp:open", "detail": {"cursor": name}}]
        node: dict[str, Any] = {"op": "query", "at": self.at(start), "sql": sql, "target": {"e": "var", "scope": "local", "name": name}}
        if params:
            node["params"] = params
        return [node]

    def s_fetch(self, start: int) -> list[dict[str, Any]]:
        end = self.statement_end(start)
        words = self.toks[start:end]
        into = next((k for k, t in enumerate(words) if t.up == "INTO"), None)
        cursor = next((t.text.lower() for t in words[1:into] if t.kind == "id" and t.up not in ("NEXT", "FROM", "PRIOR", "FIRST", "LAST")), "")
        targets = [self.var_of(t) for t in words[into + 1:] if t.text != ","] if into is not None else []
        self.i = end
        self.semi()
        self.construct("fetch", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:fetch", "detail": {"cursor": cursor, "into": targets}}]

    def s_close(self, start: int) -> list[dict[str, Any]]:
        self.i = self.statement_end(start)
        self.semi()
        self.construct("close", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:close"}]

    def s_deallocate(self, start: int) -> list[dict[str, Any]]:
        return self.s_close(start)

    def s_get(self, start: int) -> list[dict[str, Any]]:
        self.i = self.statement_end(start)
        self.semi()
        self.construct("get-diagnostics", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:get-diagnostics"}]

    def s_waitfor(self, start: int) -> list[dict[str, Any]]:
        self.i = self.statement_end(start)
        self.semi()
        self.construct("waitfor", start, False)
        return [{"op": "platform", "at": self.at(start), "construct": "dbp:waitfor"}]

    # -- SQL statements

    def sql_statement(self, start: int, construct: str | None = None) -> list[dict[str, Any]]:
        end = self.statement_end(start)
        toks = self.toks[start:end]
        self.i = end
        self.semi()
        word = toks[0].up if toks else ""
        construct = construct or ("ddl" if word in _DDL else "dml")
        if word in ("CREATE",) and any(t.up in ("TEMP", "TEMPORARY") for t in toks[:4]):
            construct = "temp-table"
        target = None
        if word in ("SELECT", "SEL"):
            toks, target = self.select_into(toks)
            if target is None and not self.sybase_assign(toks):
                self.ctx.result_sets += 1
                self.construct("result-sets", start)
            elif target is None:
                return self.select_assign(start, toks)
        self.construct("select-into" if target is not None else construct, start)
        return [self.query_node(start, toks, target=target, record=True)]

    s_select = s_insert = s_update = s_delete = s_merge = s_truncate = s_with = s_upsert = sql_statement
    s_sel = s_ins = s_upd = s_del = sql_statement
    s_create = s_drop = s_alter = s_grant = s_revoke = s_lock = s_comment = s_rename = s_refresh = s_groom = sql_statement

    def sybase_assign(self, toks: list[Tok]) -> bool:
        return self.sybase and len(toks) > 2 and toks[1].kind == "var" and toks[2].text == "="

    def select_assign(self, start: int, toks: list[Tok]) -> list[dict[str, Any]]:
        """Transact-SQL `SELECT @a = expr, @b = expr [FROM ...]`: assignments, or a query into
        the variables when it reads a table."""
        from_at = next((k for k, t in enumerate(toks) if t.up == "FROM"), None)
        items = _split_commas(toks[1:from_at] if from_at is not None else toks[1:])
        targets = []
        exprs = []
        for item in items:
            if len(item) > 2 and item[0].kind == "var" and item[1].text == "=":
                targets.append(self.var_of(item[0]))
                exprs.append(item[2:])
        if from_at is None:
            nodes = []
            for t, e in zip(targets, exprs):
                sub = Lowering(self.ctx, e)
                nodes.append({"op": "assign", "at": self.at(start), "target": t, "value": sub.expr()})
            self.construct("assign", start)
            return nodes
        select = [toks[0]] + [x for k, e in enumerate(exprs) for x in ([Tok("op", ",", e[0].start, e[0].start)] if k else []) + e] + toks[from_at:]
        target = targets[0] if len(targets) == 1 else {"e": "array", "items": targets}
        self.construct("select-into", start)
        return [self.query_node(start, select, target=target, record=True)]

    def select_into(self, toks: list[Tok]) -> tuple[list[Tok], dict[str, Any] | None]:
        """`SELECT cols INTO v1, v2 FROM ...` (DB2) or `SELECT INTO v cols FROM` (Netezza): the
        tokens without the INTO clause, and the variables. A Transact-SQL `SELECT ... INTO
        table` makes a table and is left as it is."""
        if self.sybase:
            return toks, None
        if len(toks) > 2 and toks[1].up == "INTO":  # Netezza SELECT INTO target cols FROM ...
            return toks[:1] + toks[3:], self.var_of(toks[2])
        depth = 0
        for k, t in enumerate(toks):
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
            elif depth == 0 and t.up == "INTO":
                end = k + 1
                while end < len(toks) and toks[end].up not in ("FROM", "WHERE", "GROUP", "ORDER", "FETCH", "WITH", ";"):
                    end += 1
                targets = [self.var_of(n) for n in toks[k + 1:end] if n.text != ","]
                if not targets:
                    return toks, None
                return toks[:k] + toks[end:], (targets[0] if len(targets) == 1 else {"e": "array", "items": targets})
        return toks, None

    def query_node(self, start: int, toks: list[Tok], target: dict[str, Any] | None = None, record: bool = True) -> dict[str, Any]:
        sql, params = self.sql_of(toks)
        if record and toks:
            self.ctx.sqls.append((sql, toks[0].start, toks[-1].end))
        at = self.at(start)
        if toks:
            at = self.out.at(min(self.toks[start].start, toks[0].start), max(toks[-1].end, self.span(start, self.i)[1]))
        node: dict[str, Any] = {"op": "query", "at": at, "sql": sql}
        if params:
            node["params"] = params
        if target is not None:
            node["target"] = target
        return node

    def sql_of(self, toks: list[Tok]) -> tuple[str, list[dict[str, Any]]]:
        """SQL text with Sybase `@v` and Netezza `$n` as `:name` bind parameters."""
        params: list[dict[str, Any]] = []
        seen: set[str] = set()

        def rename(t: Tok) -> str | None:
            if t.kind != "var" or t.text.startswith(("@@", ":")):
                return None
            name = self.ctx.aliases.get(t.text) or (_positional(t.text) if t.text.startswith("$") else t.text.lstrip("@").lower())
            if name not in seen:
                seen.add(name)
                params.append({"name": name, "value": self.var_of(t), "inline": False})
            return ":" + name

        return sql_text(toks, rename), params

    def subquery(self, toks: list[Tok], i: int) -> dict[str, Any]:
        sql, _ = self.sql_of(toks)
        if toks:
            self.ctx.sqls.append((sql, toks[0].start, toks[-1].end))
        return {"e": "platform", "construct": "dbp:subquery", "detail": {"sql": sql}}

    # ------------------------------------------------------------ expressions

    def matching(self, i: int) -> int:
        depth = 0
        for k in range(i, len(self.toks)):
            if self.toks[k].text == "(":
                depth += 1
            elif self.toks[k].text == ")":
                depth -= 1
                if depth == 0:
                    return k
        return len(self.toks) - 1

    def skip_parens(self) -> None:
        self.i = self.matching(self.i) + 1

    def dotted(self) -> list[Tok]:
        out = []
        while not self.done() and self.peek().kind in ("id", "qid"):
            out.append(self.next())
            if self.peek() is not None and self.peek().text in (".", ".."):  # Sybase db..proc
                out.append(self.next())
                continue
            break
        return out

    def args(self) -> list[dict[str, Any]]:
        self.eat("(")
        out = []
        while not self.done() and self.peek().text != ")":
            if self.peek().kind == "var" and self.peek(1) is not None and self.peek(1).text == "=":
                self.i += 2
            if self.peek().kind == "id" and self.peek(1) is not None and self.peek(1).text == "=>":
                self.i += 2
            out.append(self.expr())
            if not self.eat(","):
                break
        self.eat(")")
        return out

    def var_of(self, t: Tok) -> dict[str, Any]:
        if t.kind == "var":
            if t.text.startswith("@@"):
                return {"e": "var", "scope": "global", "name": t.text[2:].lower()}
            if t.text.startswith("$"):
                return {"e": "var", "scope": "arguments", "name": self.ctx.aliases.get(t.text) or _positional(t.text)}
            return {"e": "var", "scope": "local", "name": t.text.lstrip("@:").lower()}
        if t.up in _GLOBALS:
            return {"e": "var", "scope": "global", "name": t.text.lower()}
        return {"e": "var", "scope": "local", "name": t.value().lower() if t.kind == "qid" else t.text.lower()}

    _BINARY = (("OR",), ("AND",), ("=", "==", "<>", "!=", "^=", "<", "<=", ">", ">=", "LIKE", "IN", "IS", "BETWEEN", "NOT"), ("||", "+", "-"), ("*", "/", "%"))
    _OP = {"=": "==", "==": "==", "<>": "!=", "!=": "!=", "^=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "||": "&", "+": "+", "-": "-",
           "*": "*", "/": "/", "%": "%", "OR": "or", "AND": "and", "LIKE": "like"}

    def expr(self, level: int = 0) -> dict[str, Any]:
        if level >= len(self._BINARY):
            return self.unary()
        left = self.expr(level + 1)
        while not self.done():
            u = self.up()
            if u not in self._BINARY[level]:
                break
            if level == 2:
                negate = False
                if u == "NOT":
                    if self.up(1) not in ("LIKE", "IN", "BETWEEN"):
                        break
                    self.i += 1
                    negate = True
                    u = self.up()
                if u == "IS":
                    self.i += 1
                    neg = self.eat("NOT")
                    self.eat("NULL")
                    left = {"e": "unop", "op": "missing", "operand": left}
                    left = {"e": "unop", "op": "not", "operand": left} if neg else left
                    continue
                if u == "IN":
                    self.i += 1
                    if self.peek() is not None and self.peek().text == "(" and self.up(1) in ("SELECT", "WITH"):
                        close = self.matching(self.i)
                        right = self.subquery(self.toks[self.i + 1:close], self.i)
                        self.i = close + 1
                    else:
                        right = {"e": "array", "items": self.args()}
                    node = {"e": "binop", "op": "in", "left": left, "right": right}
                elif u == "BETWEEN":
                    self.i += 1
                    low = self.expr(3)
                    self.eat("AND")
                    high = self.expr(3)
                    node = {"e": "binop", "op": "and", "left": {"e": "binop", "op": ">=", "left": left, "right": low}, "right": {"e": "binop", "op": "<=", "left": left, "right": high}}
                else:
                    self.i += 1
                    node = {"e": "binop", "op": self._OP[u], "left": left, "right": self.expr(level + 1)}
                left = {"e": "unop", "op": "not", "operand": node} if negate else node
                continue
            self.i += 1
            left = {"e": "binop", "op": self._OP[u], "left": left, "right": self.expr(level + 1)}
        return left

    def unary(self) -> dict[str, Any]:
        if self.eat("NOT"):
            return {"e": "unop", "op": "not", "operand": self.expr(2)}
        if self.peek() is not None and self.peek().text in ("-", "+"):
            op = self.next().text
            return {"e": "unop", "op": op, "operand": self.unary()}
        return self.primary()

    def primary(self) -> dict[str, Any]:
        t = self.peek()
        if t is None:
            return {"e": "lit", "value": None, "type": "null"}
        if t.kind == "str":
            self.i += 1
            return {"e": "lit", "value": t.value(), "type": "string"}
        if t.kind == "num":
            self.i += 1
            return {"e": "lit", "value": int(t.text) if t.text.isdigit() else float(t.text), "type": "integer" if t.text.isdigit() else "number"}
        if t.kind == "var":
            self.i += 1
            return self.var_of(t)
        if t.text == "(":
            if self.up(1) in ("SELECT", "WITH"):
                close = self.matching(self.i)
                node = self.subquery(self.toks[self.i + 1:close], self.i)
                self.i = close + 1
                return node
            self.i += 1
            inner = self.expr()
            self.eat(")")
            return inner
        if t.kind in ("id", "qid"):
            u = t.up
            if u == "NULL":
                self.i += 1
                return {"e": "lit", "value": None, "type": "null"}
            if u in ("TRUE", "FALSE"):
                self.i += 1
                return {"e": "lit", "value": u == "TRUE", "type": "boolean"}
            if u == "EXISTS" and self.peek(1) is not None and self.peek(1).text == "(":
                self.i += 1
                close = self.matching(self.i)
                node = self.subquery(self.toks[self.i + 1:close], self.i)
                self.i = close + 1
                return {"e": "unop", "op": "exists", "operand": node}
            if u == "CASE":
                return self.case_expr()
            if u == "CURRENT" and self.peek(1) is not None and self.peek(1).kind == "id":  # DB2 CURRENT DATE, CURRENT TIMESTAMP
                self.i += 2
                return {"e": "var", "scope": "global", "name": "current_" + self.toks[self.i - 1].text.lower()}
            if u == "CAST" and self.peek(1) is not None and self.peek(1).text == "(":
                self.i += 2
                value = self.expr()
                self.eat("AS")
                type_word = self.next().text if not self.done() else ""
                if self.peek() is not None and self.peek().text == "(":
                    self.skip_parens()
                self.eat(")")
                return {"e": "call", "callee": {"e": "name", "name": "cast", "kind": "builtin"}, "args": [value, {"e": "lit", "value": type_word.lower(), "type": "string"}]}
            if t.kind == "id" and u in _TSQL_START | {"THEN", "DO", "LOOP", "ELSE", "ELSEIF", "ELSIF", "END", "WHEN", "INTO", "FROM", "UNTIL", "END_PROC"}:
                return {"e": "platform", "construct": "dbp:unparsed"}  # nothing taken: the caller moves on
            name_toks = self.dotted()
            if self.peek() is not None and self.peek().text == "(":
                return {"e": "call", "callee": {"e": "name", "name": name_of(name_toks), "kind": "builtin" if len(name_toks) == 1 else "function"}, "args": self.args()}
            parts = [x for x in name_toks if x.text != "."]
            node = self.var_of(parts[0])
            for p in parts[1:]:
                node = {"e": "member", "object": node, "name": p.text.lower()}
            return node
        self.i += 1
        return {"e": "platform", "construct": "dbp:unparsed"}

    def case_expr(self) -> dict[str, Any]:
        self.eat("CASE")
        on = None if self.up() == "WHEN" else self.expr()
        branches = []
        while self.eat("WHEN"):
            test = self.expr()
            self.eat("THEN")
            branches.append((test if on is None else {"e": "binop", "op": "==", "left": on, "right": test}, self.expr()))
        other = self.expr() if self.eat("ELSE") else {"e": "lit", "value": None, "type": "null"}
        self.eat("END")
        for test, value in reversed(branches):
            other = {"e": "cond", "test": test, "then": value, "else": other}
        return other


def _positional(text: str) -> str:
    return "arg" + text.lstrip("$")


def _split_commas(toks: list[Tok]) -> list[list[Tok]]:
    out: list[list[Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        if t.text == "," and depth == 0:
            out.append([])
        else:
            out[-1].append(t)
    return [o for o in out if o]


def _without_for_clause(toks: list[Tok]) -> list[Tok]:
    """A cursor's SELECT without its trailing FOR READ ONLY / FOR UPDATE [OF ...] clause."""
    depth = 0
    for k, t in enumerate(toks):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif depth == 0 and t.up == "FOR" and k + 1 < len(toks) and toks[k + 1].up in ("READ", "UPDATE", "FETCH"):
            return toks[:k]
    return toks
