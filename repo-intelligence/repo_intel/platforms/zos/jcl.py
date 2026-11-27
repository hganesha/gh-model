"""JCL: statements, operands, in-stream data, and the jobs and procedures they make.

`statements(text)` reads the file line by line, once:

- A statement starts with `//` in columns 1 and 2; the name field starts in column 3,
  then the operation and the operand field, which ends at the first blank outside quotes
  (the rest is a comment). The statement field ends at column 71: column 72 marks a
  continuation and columns 73 to 80 are sequence numbers, so both are ignored.
- An operand field that ends with a comma continues on the next `//` line whose column 3
  is blank. A quoted value open at the end of a line continues at column 16 of the next.
  An IF statement's expression may hold blanks and runs to its THEN keyword.
- `//*` is a comment (JES3 control statements and scheduler directives are told apart
  and counted), `/*` alone is a delimiter, `/*JOBPARM` and the like are JES2 statements,
  and `//` alone ends the job.
- `DD *` and `DD DATA` start in-stream data, which ends at the delimiter (`/*`, or the
  `DLM=` value), or for `DD *` also at the next `//` line. Data lines that follow a step
  with no DD statement before them are an implied `SYSIN DD *`.

Every operand character keeps its offset in the file, so a parameter's span is exact.

`parse(text, member)` groups the statements into units: each JOB, each cataloged PROC
(named by its PROC statement, else by the member), and each in-stream PROC inside a job.
A unit holds its steps in order, the IF/THEN/ELSE tree around them, its SET statements,
INCLUDE members and JCLLIB libraries. A step holds its DD statements; a DD named
`procstep.ddname` is an override of a procedure step, and an unnamed DD continues the
concatenation of the DD before it.

Nothing here resolves symbolic parameters or reads control cards; `source` does that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_KEYWORD = re.compile(r"^([A-Z@#$][A-Z0-9@#$]*(?:\.[A-Z@#$][A-Z0-9@#$]*)?)=", re.I)
_THEN = re.compile(r"(?<![A-Za-z0-9@#$.])THEN(?![A-Za-z0-9@#$])", re.I)
_JES3 = ("MAIN", "NET", "NETACCT", "FORMAT", "PROCESS", "ENDPROCESS", "DATASET", "ENDDATASET", "OPERATOR", "PAUSE", "ROUTE", "SIGNOFF", "SIGNON")
_JES2 = ("JOBPARM", "ROUTE", "OUTPUT", "PRIORITY", "XEQ", "MESSAGE", "NOTIFY", "SETUP", "SIGNON", "SIGNOFF", "XMIT", "NETACCT")


@dataclass
class Param:
    key: str | None  # upper-cased keyword; None for a positional parameter
    value: str  # as written
    start: int  # character offsets of the value in the file
    end: int


@dataclass
class InStream:
    lines: list[tuple[int, str]]  # (offset of the line in the file, the line as written)
    start: int
    end: int
    implied: bool = False

    @property
    def text(self) -> str:
        return "\n".join(line for _, line in self.lines)


@dataclass
class Statement:
    name: str | None
    op: str
    operands: str
    pos: list[int]  # file offset of each operand character
    start: int
    end: int
    index: int  # how many JOB, EXEC, DD and PROC statements came before it (legacy.jcl's step numbering)
    data: InStream | None = None

    def span(self, i: int, j: int) -> tuple[int, int]:
        """File offsets of operand characters i..j (exclusive)."""
        if not self.pos or i >= j:
            return self.start, self.end
        return self.pos[i], self.pos[j - 1] + 1

    def params(self) -> list[Param]:
        return split_params(self)

    def param(self, key: str) -> Param | None:
        return next((p for p in self.params() if p.key == key), None)


@dataclass
class Special:
    """A line that is not a statement but is counted: JES2 or JES3 control, a scheduler directive."""

    kind: str  # "jes2" | "jes3" | "scheduler_directive"
    start: int
    end: int


# ------------------------------------------------------------------------------ the lexer


def _lines(text: str) -> list[tuple[int, str]]:
    out = []
    offset = 0
    for raw in text.split("\n"):
        out.append((offset, raw[:-1] if raw.endswith("\r") else raw))
        offset += len(raw) + 1
    return out


def _scan(body: str, k: int, off: int, chars: list[str], pos: list[int], quote: bool) -> tuple[bool, int]:
    """Operand characters of one line from column k, until a blank outside quotes.
    Returns (quote still open, index after the last operand character)."""
    j, n = k, len(body)
    while j < n:
        c = body[j]
        if quote:
            chars.append(c)
            pos.append(off + j)
            if c == "'":
                if j + 1 < n and body[j + 1] == "'":
                    chars.append("'")
                    pos.append(off + j + 1)
                    j += 2
                    continue
                quote = False
            j += 1
            continue
        if c == " ":
            break
        if c == "'":
            quote = True
        chars.append(c)
        pos.append(off + j)
        j += 1
    return quote, j


def _continuation(line: str) -> bool:
    return line.startswith("//") and not line.startswith("//*") and line[2:3] in (" ", "") and line[2:72].strip() != ""


def statements(text: str) -> tuple[list[Statement], list[Special]]:
    lines = _lines(text)
    out: list[Statement] = []
    specials: list[Special] = []
    counted = 0
    i, n = 0, len(lines)
    data_for: Statement | None = None
    data_lines: list[tuple[int, str]] = []
    delimiter, ends_at_slashes = "/*", True

    def finish_data(end: int) -> None:
        nonlocal data_for, data_lines
        if data_for is not None:
            start = data_lines[0][0] if data_lines else end
            last = data_lines[-1][0] + len(data_lines[-1][1]) if data_lines else end
            data_for.data = InStream(list(data_lines), start, last, data_for.data.implied if data_for.data else False)
        data_for, data_lines = None, []

    while i < n:
        off, line = lines[i]
        if data_for is not None:
            if line.startswith(delimiter):
                finish_data(off)
                i += 1
                continue
            if ends_at_slashes and line.startswith("//"):
                finish_data(off)
            else:
                data_lines.append((off, line[:80]))
                i += 1
                continue
        if line.startswith("//*"):
            rest = line[3:]
            word = re.match(r"[A-Z]+", rest)
            if rest.startswith("%OPC") or rest.startswith("%ACC"):
                specials.append(Special("scheduler_directive", off, off + len(line.rstrip())))
            elif word and word.group() in _JES3 and (len(rest) == len(word.group()) or rest[len(word.group())] in " ,"):
                specials.append(Special("jes3", off, off + len(line.rstrip())))
            i += 1
            continue
        if line.startswith("/*"):
            word = re.match(r"[A-Z]+", line[2:])
            if word and word.group() in _JES2:
                specials.append(Special("jes2", off, off + len(line.rstrip())))
            i += 1
            continue
        if not line.startswith("//"):
            if line.strip() and out and any(s.op == "EXEC" for s in out):
                # Data with no DD * before it: JES reads it as SYSIN DD * of the step.
                implied = Statement("SYSIN", "DD", "*", [off], off, off, counted)
                implied.data = InStream([], off, off, implied=True)
                out.append(implied)
                data_for, data_lines, delimiter, ends_at_slashes = implied, [(off, line[:80])], "/*", True
            i += 1
            continue
        body = line[:71].rstrip()
        if body.strip() == "//":
            out.append(Statement(None, "NULL", "", [], off, off + len(body), counted))
            i += 1
            continue
        j = 2
        name = None
        if body[2:3] not in (" ", ""):
            k = body.find(" ", 2)
            k = len(body) if k < 0 else k
            name = body[2:k]
            j = k
        while j < len(body) and body[j] == " ":
            j += 1
        k = j
        while k < len(body) and body[k] != " ":
            k += 1
        op = body[j:k].upper()
        j = k
        while j < len(body) and body[j] == " ":
            j += 1
        chars: list[str] = []
        pos: list[int] = []
        end = off + len(body)
        if op == "IF":
            # The relational expression may hold blanks: it runs to THEN, over continuation lines.
            segment = body[j:]
            for c_i, c in enumerate(segment):
                chars.append(c)
                pos.append(off + j + c_i)
            while not _THEN.search("".join(chars)) and i + 1 < n and _continuation(lines[i + 1][1]):
                i += 1
                off2, nxt = lines[i]
                body2 = nxt[:71].rstrip()
                k2 = 2
                while k2 < len(body2) and body2[k2] == " ":
                    k2 += 1
                chars.append(" ")
                pos.append(off2 + k2 - 1)
                for c_i, c in enumerate(body2[k2:]):
                    chars.append(c)
                    pos.append(off2 + k2 + c_i)
                end = off2 + len(body2)
            m = _THEN.search("".join(chars))
            if m:
                chars, pos = chars[: m.start()], pos[: m.start()]
                while chars and chars[-1] == " ":
                    chars.pop()
                    pos.pop()
        else:
            quote, _ = _scan(body, j, off, chars, pos, False)
            while (quote or (chars and chars[-1] == ",")) and i + 1 < n:
                # Comment statements may sit between a statement and its continuation.
                peek = i + 1
                while peek < n and lines[peek][1].startswith("//*"):
                    peek += 1
                if peek >= n:
                    break
                nxt = lines[peek][1]
                if quote:
                    if not (nxt.startswith("//") and nxt[2:3] in (" ", "")):
                        break
                    i = peek
                    body2 = nxt[:71].rstrip()
                    quote, _ = _scan(body2, min(15, len(body2)), lines[i][0], chars, pos, True)
                    end = lines[i][0] + len(body2)
                    continue
                if not _continuation(nxt):
                    break
                i = peek
                off2 = lines[i][0]
                body2 = nxt[:71].rstrip()
                k2 = 2
                while k2 < len(body2) and body2[k2] == " ":
                    k2 += 1
                quote, _ = _scan(body2, k2, off2, chars, pos, False)
                end = off2 + len(body2)
        st = Statement(name, op, "".join(chars), pos, off, end, counted)
        if op in ("JOB", "EXEC", "DD", "PROC"):
            counted += 1
        out.append(st)
        if op == "DD":
            params = split_params(st)
            first = next((p for p in params if p.key is None), None)
            if first is not None and first.value.upper() in ("*", "DATA"):
                dlm = next((p for p in params if p.key == "DLM"), None)
                data_for, data_lines = st, []
                delimiter = (dlm.value.strip("'") if dlm and dlm.value.strip("'") else "/*")
                ends_at_slashes = first.value.upper() == "*" and dlm is None
        i += 1
    finish_data(len(text))
    return out, specials


def split_params(st: Statement) -> list[Param]:
    """The operand field split at commas outside parentheses and quotes."""
    text = st.operands
    out: list[Param] = []
    depth, quote, start = 0, False, 0
    pieces: list[tuple[int, int]] = []
    for i, c in enumerate(text):
        if quote:
            if c == "'":
                quote = False  # a doubled quote closes and reopens, which leaves the state unchanged
            continue
        if c == "'":
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0:
            pieces.append((start, i))
            start = i + 1
    pieces.append((start, len(text)))
    for a, b in pieces:
        piece = text[a:b]
        if not piece and (a, b) == (0, 0) and not text:
            continue
        m = _KEYWORD.match(piece)
        if m:
            va = a + m.end()
            s, e = st.span(va, b) if va < b else st.span(a, b)
            out.append(Param(m.group(1).upper(), text[va:b], s, e))
        else:
            s, e = st.span(a, b)
            out.append(Param(None, piece, s, e))
    return out


def subparams(value: str) -> list[str]:
    """`(A,B,(C,D))` -> ["A", "B", "(C,D)"]; a value without parentheses is one item."""
    v = value.strip()
    if not (v.startswith("(") and v.endswith(")")):
        return [v]
    inner = v[1:-1]
    out, depth, quote, start = [], 0, False, 0
    for i, c in enumerate(inner):
        if quote:
            if c == "'":
                quote = False
            continue
        if c == "'":
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(inner[start:i].strip())
            start = i + 1
    out.append(inner[start:].strip())
    return out


def unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    return v


# ------------------------------------------------------------------------------ the units


@dataclass
class DD:
    stmt: Statement
    ddname: str  # the DD name it binds; for a concatenation, the name of the DD it continues
    procstep: str | None  # for an override //procstep.ddname
    concat: int  # 0 for a named DD, 1, 2, ... for the DDs concatenated to it
    params: list[Param]

    def param(self, key: str) -> Param | None:
        return next((p for p in self.params if p.key == key), None)

    def positional(self) -> list[Param]:
        return [p for p in self.params if p.key is None and p.value]


@dataclass
class Step:
    stmt: Statement
    name: str  # as keyed: the step name, or STEP<n> for an unnamed step
    params: list[Param]
    dds: list[DD] = field(default_factory=list)
    overrides: list[DD] = field(default_factory=list)  # //procstep.ddname DDs of a procedure call
    conditions: list[dict] = field(default_factory=list)  # the IF constructs around the step, outermost first

    def param(self, key: str) -> Param | None:
        return next((p for p in self.params if p.key == key), None)

    @property
    def pgm(self) -> Param | None:
        return self.param("PGM")

    @property
    def proc(self) -> Param | None:
        """The procedure an EXEC calls: PROC=NAME, or a first positional parameter."""
        p = self.param("PROC")
        if p is not None:
            return p
        first = self.params[0] if self.params else None
        return first if first is not None and first.key is None and first.value else None


@dataclass
class IfNode:
    stmt: Statement
    expr: str
    expr_at: tuple[int, int]
    then: list = field(default_factory=list)
    els: list | None = None
    else_stmt: Statement | None = None
    end: int = 0


@dataclass
class Unit:
    kind: str  # "job" or "proc"
    name: str
    stmt: Statement
    params: list[Param]
    in_stream: bool = False
    body: list = field(default_factory=list)  # Step and IfNode, in order
    steps: list[Step] = field(default_factory=list)
    sets: list[tuple[Statement, Param]] = field(default_factory=list)
    includes: list[tuple[Statement, str]] = field(default_factory=list)
    jcllib: list[tuple[Statement, list[str]]] = field(default_factory=list)
    procs: dict[str, "Unit"] = field(default_factory=dict)  # in-stream procedures of a job
    others: list[Statement] = field(default_factory=list)  # OUTPUT, CNTL and the like
    end: int = 0

    def param(self, key: str) -> Param | None:
        return next((p for p in self.params if p.key == key), None)


@dataclass
class Parsed:
    units: list[Unit]  # jobs and cataloged procedures, in file order
    specials: list[Special]
    orphans: list[Statement]  # statements outside any job or procedure


def parse(text: str, member: str | None) -> Parsed:
    stmts, specials = statements(text)
    units: list[Unit] = []
    orphans: list[Statement] = []
    job: Unit | None = None
    proc: Unit | None = None
    step: Step | None = None
    stack: list[list] = []
    ifs: list[tuple[IfNode, str]] = []
    saved: tuple[list[list], list[tuple[IfNode, str]], Step | None] | None = None
    last_dd: DD | None = None

    def owner() -> Unit | None:
        return proc or job

    for st in stmts:
        op = st.op
        if op == "JOB":
            job = Unit("job", (st.name or member or "JOB").upper(), st, split_params(st))
            units.append(job)
            proc, step, last_dd, saved = None, None, None, None
            stack, ifs = [job.body], []
        elif op == "PROC":
            name = (st.name or member or "PROC").upper()
            if job is not None and proc is None:
                proc = Unit("proc", name, st, split_params(st), in_stream=True)
                job.procs.setdefault(name, proc)
                saved = (stack, ifs, step)
                stack, ifs, step, last_dd = [proc.body], [], None, None
            elif proc is None:
                proc = Unit("proc", name, st, split_params(st))
                units.append(proc)
                stack, ifs, step, last_dd = [proc.body], [], None, None
            else:
                orphans.append(st)
        elif op == "PEND":
            if proc is not None:
                proc.end = st.end
            if proc is not None and proc.in_stream and saved is not None:
                stack, ifs, step = saved
                saved = None
                proc, last_dd = None, None
                step = None
            elif proc is not None:
                step, last_dd = None, None
        elif op == "EXEC":
            o = owner()
            if o is None:
                orphans.append(st)
                continue
            step = Step(st, (st.name or f"STEP{st.index}").upper(), split_params(st))
            step.conditions = [{"if": node.expr, "branch": branch} for node, branch in ifs]
            stack[-1].append(step)
            o.steps.append(step)
            last_dd = None
        elif op == "DD":
            if step is None:
                orphans.append(st)  # JOBLIB and other DDs before the first step
                continue
            params = split_params(st)
            if st.name and "." in st.name:
                procstep, _, ddname = st.name.upper().partition(".")
                dd = DD(st, ddname, procstep, 0, params)
                step.overrides.append(dd)
            elif st.name:
                dd = DD(st, st.name.upper(), None, 0, params)
                step.dds.append(dd)
            elif last_dd is not None:
                dd = DD(st, last_dd.ddname, last_dd.procstep, last_dd.concat + 1, params)
                (step.overrides if last_dd.procstep else step.dds).append(dd)
            else:
                orphans.append(st)
                continue
            last_dd = dd
        elif op == "SET":
            o = owner()
            if o is None:
                orphans.append(st)
                continue
            for p in split_params(st):
                if p.key:
                    o.sets.append((st, p))
        elif op == "IF":
            o = owner()
            if o is None:
                orphans.append(st)
                continue
            expr = re.sub(r"\s+", " ", st.operands).strip()
            node = IfNode(st, expr, (st.pos[0], st.pos[-1] + 1) if st.pos else (st.start, st.end))
            stack[-1].append(node)
            stack.append(node.then)
            ifs.append((node, "then"))
        elif op == "ELSE":
            if ifs:
                node, _ = ifs[-1]
                node.els, node.else_stmt = [], st
                stack.pop()
                stack.append(node.els)
                ifs[-1] = (node, "else")
        elif op == "ENDIF":
            if ifs:
                node, _ = ifs.pop()
                node.end = st.end
                stack.pop()
        elif op == "INCLUDE":
            o = owner()
            member_p = next((p for p in split_params(st) if p.key == "MEMBER"), None)
            if o is not None and member_p is not None:
                o.includes.append((st, unquote(member_p.value).upper()))
            elif o is None:
                orphans.append(st)
        elif op == "JCLLIB":
            o = owner()
            order = next((p for p in split_params(st) if p.key == "ORDER"), None)
            if o is not None and order is not None:
                o.jcllib.append((st, [unquote(x).upper() for x in subparams(order.value) if x]))
            elif o is None:
                orphans.append(st)
        elif op == "NULL":
            job, proc, step, last_dd, saved = None, None, None, None, None
            stack, ifs = [], []
        else:
            o = owner()
            if o is not None:
                o.others.append(st)
            else:
                orphans.append(st)
        o = owner()
        if o is not None:
            o.end = max(o.end, st.end)
            if proc is not None and proc.in_stream and job is not None:
                job.end = max(job.end, st.end)
    return Parsed(units, specials, orphans)

