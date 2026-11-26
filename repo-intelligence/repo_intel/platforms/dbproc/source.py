"""Stored routines of DB2, Netezza and Sybase ASE (`dbproc.routines`).

A file is read when its content says which engine it is for:

- Netezza: `LANGUAGE NZPLSQL` or `BEGIN_PROC`;
- DB2: a routine with DB2 markers (`--#SET TERMINATOR`, `DYNAMIC RESULT SETS`, `BEGIN ATOMIC`,
  `DECLARE ... HANDLER`, `SIGNAL SQLSTATE`, `SPECIFIC`, `MODE DB2SQL`, `GET DIAGNOSTICS`,
  `SYSPROC.`), or a `.db2` file;
- Sybase: a routine with ASE-only markers (`@@sqlstatus`, `@@transtate`, `sp_procxmode`,
  `set chained`, `holdlock`, `raiserror <number>`, `print "..."`, `at isolation`), or one in
  a directory named `sybase`.

Oracle PL/SQL (the `plsql` pack's), MySQL and SQL Server files are left alone; SQL Server
procedures stay with `sql_lineage`.

Each procedure, function and trigger is a `Program {platform: <engine>, name: <lower-case
name>}` with one behaviour unit (kind `function`, or `trigger`), the tables its statements
write and read, and CALLS to the procedures it calls. `sql_lineage` also declares a
procedure in a `.sql` file, under the platform its scanner guesses (`sql` or `tsql`); that
Program is ALIAS_OF this one.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.dbproc.lexer import Tok, name_of, tokens
from repo_intel.platforms.dbproc.lower import Ctx, Lowering, ir_type
from repo_intel.platforms.dbproc.vocabulary import LANGUAGE
from repo_intel.platforms.shell.emit import Out, P, decode, program_ref

_ROUTINE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|PROC|FUNCTION|TRIGGER)\b", re.I)
_NETEZZA = re.compile(r"\bLANGUAGE\s+NZPLSQL\b|\bBEGIN_PROC\b", re.I)
_DB2 = re.compile(r"--#SET\s+TERMINATOR|\bDYNAMIC\s+RESULT\s+SETS\b|\bBEGIN\s+ATOMIC\b|\bDECLARE\s+(?:CONTINUE|EXIT|UNDO)\s+HANDLER\b|\bSIGNAL\s+SQLSTATE\b"
                  r"|^\s*SPECIFIC\s+\w+|\bMODE\s+DB2SQL\b|\bGET\s+DIAGNOSTICS\b|\bSYSPROC\.", re.I | re.M)
_SYBASE = re.compile(r"@@sqlstatus|@@transtate|\bsp_procxmode\b|\bset\s+chained\b|\b(?:no)?holdlock\b|\braiserror\s+\d+|\bprint\s+\"|\bat\s+isolation\b", re.I)
_NOT_OURS = re.compile(r"\bVARCHAR2\b|%ROWTYPE|%TYPE\b|\bDBMS_\w+|\bPRAGMA\b|^\s*DELIMITER\b|\bENGINE\s*=|\bNVARCHAR\s*\(\s*MAX\s*\)|\bTRY_CONVERT\b|\bBEGIN\s+TRY\b", re.I | re.M)


def engine_of(path: str, text: str) -> str | None:
    """The engine a file's routines are for, or None when it is not a file this pack reads."""
    if not _ROUTINE.search(text):
        return None
    if _NETEZZA.search(text):
        return "netezza"
    if _NOT_OURS.search(text):
        return None
    if _DB2.search(text) or path.lower().endswith(".db2"):
        return "db2"
    dirs = [d.lower() for d in posixpath.dirname(path).split("/")]
    if _SYBASE.search(text) or "sybase" in dirs:
        return "sybase"
    return None


class Routines:
    id = "dbproc.routines"
    version = "1.0.0"
    SUFFIXES = (".sql", ".db2", ".nzpl", ".nzsql", ".sp", ".spl", ".proc")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction

        text = decode(data)
        engine = engine_of(path, text)
        if engine is None:
            return Extraction(parsed=False)
        out = Out(self.id, path, data, blob, text)
        toks = tokens(text, double_quoted_strings=engine == "sybase")
        found = 0
        i = 0
        while i < len(toks):
            if toks[i].up == "CREATE":
                j = i + 1
                if toks[j:j + 2] and toks[j].up == "OR" and j + 1 < len(toks) and toks[j + 1].up in ("REPLACE", "ALTER"):
                    j += 2
                if j < len(toks) and toks[j].up in ("PROCEDURE", "PROC", "FUNCTION", "TRIGGER"):
                    end = Routine(out, path, text, engine, toks, i, j).run()
                    found += 1
                    i = max(end, i + 1)
                    continue
            i += 1
        if not found:
            return Extraction(parsed=False)
        return out.done()


class Routine:
    def __init__(self, out: Out, path: str, text: str, engine: str, toks: list[Tok], create: int, keyword: int):
        self.out, self.path, self.text, self.engine, self.toks = out, path, text, engine, toks
        self.create, self.i = create, keyword
        self.kind = {"PROC": "procedure"}.get(toks[keyword].up, toks[keyword].up.lower())

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def up(self, k: int = 0) -> str:
        t = self.peek(k)
        return t.up if t is not None else ""

    def run(self) -> int:
        """Read the routine; the index of the first token after it."""
        out = self.out
        self.i += 1
        name_start = self.i
        name_toks = []
        while self.peek() is not None and self.peek().kind in ("id", "qid"):
            name_toks.append(self.toks[self.i])
            self.i += 1
            if self.peek() is not None and self.peek().text == ".":
                name_toks.append(self.toks[self.i])
                self.i += 1
                continue
            break
        name = name_of(name_toks)
        if not name:
            return self.i
        a, b = self.toks[name_start].start, self.toks[max(self.i - 1, name_start)].end
        if self.engine == "sybase" and self.peek() is not None and self.peek().text == ";":  # proc;2 (a group number)
            self.i += 2
        attrs: dict[str, Any] = {"kind": self.kind, "language": LANGUAGE[self.engine]}
        params: list[dict[str, Any]] = []
        when: list[Tok] = []
        if self.kind == "trigger":
            on = self.trigger_header(when)
            if on:
                attrs["on"] = on
        else:
            params = self.parameters()
            attrs.update(self.options())
        body_start, body_end, end = self.body()
        if params:
            attrs["params"] = [p["name"] for p in params]
        me = program_ref(self.engine, name, declare=False)
        ctx = Ctx(out, self.engine, me, aliases=self.aliases(body_start, body_end))
        if self.engine == "netezza" and params:  # name ALIAS FOR $n names the positional parameters
            for k, p in enumerate(params):
                p["name"] = ctx.aliases.get(f"${k + 1}", p["name"])
            attrs["params"] = [p["name"] for p in params]
        body: list[dict[str, Any]] = []
        try:
            lowering = Lowering(ctx, self.toks[body_start:body_end])
            body = lowering.block(set())
            if when:  # a DB2 trigger's WHEN (condition): the body runs only when it holds
                cond = Lowering(ctx, when).expr()
                body = [{"op": "if", "at": out.at(when[0].start, when[-1].end), "cond": cond, "then": body}]
        except Exception as exc:  # a reader bug must not lose the routine and its lineage
            out.note(f"the body of {name} was not lowered: {type(exc).__name__}", "warning")
        if ctx.result_sets and "result_sets" not in attrs:
            attrs["result_sets"] = ctx.result_sets
        term = {"procedure": "dbp:Procedure", "function": "dbp:Function", "trigger": "dbp:Trigger"}[self.kind]
        ref = out.term(a, b, term, {"platform": self.engine, "name": name}, me["name"], attrs)
        rstart = self.toks[self.create].start
        rend = self.toks[min(end, len(self.toks)) - 1].end
        out.construct(f"dbp:{self.kind}", a, b, True)
        for _ in params:
            out.construct("dbp:parameter", a, b, True)
        out.behavior(rstart, rend, ref, self.engine, body, "trigger" if self.kind == "trigger" else "function", params)
        self.lineage(ctx, ref)
        self.alias(name, a, b, ref)
        return end

    # -- headers

    def parameters(self) -> list[dict[str, Any]]:
        params: list[dict[str, Any]] = []
        t = self.peek()
        if t is None:
            return params
        if self.engine == "sybase":
            paren = t.text == "("
            if paren:
                self.i += 1
            items: list[list[Tok]] = [[]]
            depth = 0
            while self.peek() is not None:
                t = self.peek()
                if depth == 0 and (t.up in ("AS", "WITH") or (paren and t.text == ")")):
                    break
                if t.text == "(":
                    depth += 1
                elif t.text == ")":
                    depth -= 1
                if t.text == "," and depth == 0:
                    items.append([])
                else:
                    items[-1].append(t)
                self.i += 1
            if paren and self.peek() is not None and self.peek().text == ")":
                self.i += 1
            for item in items:
                if item and item[0].kind == "var":
                    param: dict[str, Any] = {"name": item[0].text.lstrip("@").lower(), "type": ir_type(item[1].text) if len(item) > 1 else "unknown",
                                             "required": not any(x.text == "=" for x in item), "default": None}
                    if any(x.up in ("OUTPUT", "OUT") for x in item):
                        param["direction"] = "out"
                    params.append(param)
            return params
        if t.text != "(":
            return params
        close = self.close(self.i)
        items = _split(self.toks[self.i + 1:close])
        self.i = close + 1
        for k, item in enumerate(items):
            if not item:
                continue
            if self.engine == "netezza":  # types only: the body names them with ALIAS FOR $n
                params.append({"name": f"arg{k + 1}", "type": ir_type(item[0].text), "required": True, "default": None})
                continue
            direction = "in"
            if item[0].up in ("IN", "OUT", "INOUT"):
                direction = item[0].up.lower()
                item = item[1:]
            if not item:
                continue
            param = {"name": item[0].text.lower(), "type": ir_type(item[1].text) if len(item) > 1 else "unknown",
                     "required": not any(x.up == "DEFAULT" for x in item), "default": None}
            if direction != "in":
                param["direction"] = direction
            params.append(param)
        return params

    def options(self) -> dict[str, Any]:
        """Routine options up to the body; the ones kept as attributes."""
        attrs: dict[str, Any] = {}
        start = self.i
        while self.peek() is not None and self.up() not in ("BEGIN", "BEGIN_PROC", "AS", "RETURN") and not (self.peek(1) is not None and self.peek(1).text == ":" and self.up(2) == "BEGIN"):
            if self.up() == "DYNAMIC" and self.up(1) == "RESULT" and self.up(3).isdigit():
                attrs["result_sets"] = int(self.up(3))
            if self.up() == "SPECIFIC" and self.peek(1) is not None:
                attrs["specific"] = self.peek(1).text.lower()
            if self.up() == "RETURNS" and self.peek(1) is not None:
                attrs["returns"] = self.peek(1).text.lower()
            if self.up() in ("@",) or (self.up() == ";" and self.engine != "db2"):
                break
            self.i += 1
        if self.i > start:
            self.out.construct("dbp:routine-option", self.toks[start].start, self.toks[self.i - 1].end, False)
        if attrs.get("result_sets"):
            self.out.construct("dbp:result-sets", self.toks[start].start, self.toks[self.i - 1].end, True)
        return attrs

    def trigger_header(self, when: list[Tok]) -> dict[str, Any] | None:
        """`[NO CASCADE] BEFORE|AFTER|INSTEAD OF INSERT|UPDATE [OF cols]|DELETE ON table ...`
        (DB2) or `ON table FOR|INSTEAD OF insert, update AS` (Sybase): the trigger's `on`."""
        timing = None
        events: list[str] = []
        table = None
        while self.peek() is not None:
            u = self.up()
            if u == "BEGIN" or (u == "AS" and self.engine == "sybase") or (self.engine == "db2" and u in ("INSERT", "UPDATE", "DELETE", "SET", "SIGNAL", "CALL", "VALUES") and table is not None):
                break
            if u in ("BEFORE", "AFTER"):
                timing = u.lower()
            elif u == "INSTEAD":
                timing = "instead of"
            elif u == "FOR" and self.engine == "sybase" and table is not None:
                timing = timing or "after"
            elif u in ("INSERT", "UPDATE", "DELETE") and table is None or (self.engine == "sybase" and u in ("INSERT", "UPDATE", "DELETE")):
                events.append(u.lower())
            elif u == "ON" and table is None:
                self.i += 1
                parts = []
                while self.peek() is not None and self.peek().kind in ("id", "qid"):
                    parts.append(self.toks[self.i])
                    self.i += 1
                    if self.peek() is not None and self.peek().text == ".":
                        parts.append(self.toks[self.i])
                        self.i += 1
                        continue
                    break
                table = name_of(parts)
                continue
            elif u == "WHEN" and self.peek(1) is not None and self.peek(1).text == "(":
                close = self.close(self.i + 1)
                when.extend(self.toks[self.i + 2:close])
                self.i = close + 1
                continue
            self.i += 1
        if table is None:
            return None
        return {"table": table, "events": sorted(set(events)), "timing": timing or "after"}

    def body(self) -> tuple[int, int, int]:
        """(start, end) of the body tokens, and the index after the routine."""
        toks = self.toks
        if self.engine == "netezza":
            start = next((k for k in range(self.i, len(toks)) if toks[k].up == "BEGIN_PROC"), None)
            if start is None:
                return self.i, self.i, self.i
            end = next((k for k in range(start, len(toks)) if toks[k].up == "END_PROC"), len(toks))
            after = end + 1
            if after < len(toks) and toks[after].text == ";":
                after += 1
            return start + 1, end, after
        if self.engine == "sybase":
            while self.peek() is not None and self.up() != "AS":
                self.i += 1
            start = self.i + 1
            end = next((k for k in range(start, len(toks)) if toks[k].up == "GO" or (toks[k].up == "CREATE" and k + 1 < len(toks)
                        and toks[k + 1].up in ("PROCEDURE", "PROC", "TRIGGER", "FUNCTION"))), len(toks))
            return start, end, end
        # DB2: a compound statement, or one statement up to the terminator
        k = self.i
        while k < len(toks) and toks[k].up not in ("BEGIN", "RETURN") and toks[k].text not in ("@",) and not (self.kind == "trigger" and toks[k].up in ("INSERT", "UPDATE", "DELETE", "SET", "SIGNAL", "CALL", "VALUES")):
            k += 1
        if k > 1 and k - 2 >= self.i and toks[k - 1].text == ":" and k < len(toks) and toks[k].up == "BEGIN":
            k -= 2  # a labelled compound
        if k < len(toks) and (toks[k].up == "BEGIN" or (k + 2 < len(toks) and toks[k + 2].up == "BEGIN")):
            begin = k if toks[k].up == "BEGIN" else k + 2
            end = _compound_end(toks, begin)
            after = end
            while after < len(toks) and toks[after].text in (";", "@"):
                after += 1
            return k, end, after
        end = k
        while end < len(toks) and toks[end].text not in ("@",) and not (toks[end].text == ";" and not self.terminator_at()):
            end += 1
        return k, end, end + 1

    def terminator_at(self) -> bool:
        """Whether this file ends statements with `@` (`--#SET TERMINATOR @`), so `;` does not end the routine."""
        return bool(re.search(r"--#SET\s+TERMINATOR\s+@", self.text, re.I))

    def aliases(self, start: int, end: int) -> dict[str, str]:
        out: dict[str, str] = {}
        toks = self.toks[start:end]
        for k in range(len(toks) - 3):
            if toks[k + 1].up == "ALIAS" and toks[k + 2].up == "FOR" and toks[k + 3].text.startswith("$"):
                out[toks[k + 3].text] = toks[k].text.lower()
        return out

    def close(self, i: int) -> int:
        depth = 0
        for k in range(i, len(self.toks)):
            if self.toks[k].text == "(":
                depth += 1
            elif self.toks[k].text == ")":
                depth -= 1
                if depth == 0:
                    return k
        return len(self.toks) - 1

    # -- lineage and the alias

    def lineage(self, ctx: Ctx, me: dict[str, Any]) -> None:
        from repo_intel.extractors.sql_lineage import DIALECT, lineage

        out = self.out
        pseudo = {"inserted", "deleted", "new", "old", "new_table", "old_table"}
        for sql, a, b in ctx.sqls:
            found = lineage(sql, DIALECT[self.engine], views=False)
            for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
                for name, _x, _y in items:
                    if name in pseudo or name.startswith(("session.", "#", ":")) or ":" in name:
                        continue
                    out.table(a, b, me, predicate, name)
            out.unresolved += found.unresolved

    def alias(self, name: str, a: int, b: int, me: dict[str, Any]) -> None:
        """`sql_lineage` declares the procedures of a `.sql` file as Program {sql|tsql, name}."""
        if self.kind != "procedure" or not self.path.lower().endswith(".sql"):
            return
        from repo_intel.extractors.contracts_data import migration_identity
        from repo_intel.extractors.sql_lineage import _PROCEDURE, clean_name, scrub_commands, sql_platform

        if migration_identity(self.path) is not None or "{{" in self.text or "{%" in self.text:
            return
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            return
        platform = sql_platform(self.path, self.text)
        if any(clean_name(m.group(2)) == name for m in _PROCEDURE.finditer(scrub_commands(self.text))):
            self.out.rel(a, b, program_ref(platform, name, declare=False), P.ALIAS_OF, me)


def _compound_end(toks: list[Tok], begin: int) -> int:
    """The index after the END that closes the BEGIN at toks[begin]."""
    depth = 0
    k = begin
    while k < len(toks):
        u = toks[k].up
        nxt = toks[k + 1].up if k + 1 < len(toks) else ""
        if u == "BEGIN" and nxt not in ("TRAN", "TRANSACTION", "WORK"):
            depth += 1
        elif u == "CASE":
            depth += 1
        elif u == "END":
            if nxt in ("IF", "WHILE", "LOOP", "FOR", "REPEAT"):
                k += 2
                continue
            depth -= 1
            if nxt == "CASE":
                k += 1
            if depth == 0:
                return k + 1
        k += 1
    return len(toks)


def _split(toks: list[Tok]) -> list[list[Tok]]:
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
    return out


__all__ = ["Routines", "engine_of"]
