"""`pli.source`: PL/I programs and %INCLUDE members (`.pli`, `.pl1`).

A file with an external procedure is a program; one with declarations and no procedure is
an %INCLUDE member. What it declares (keys in `vocabulary`):

- each external procedure as `Program {platform: "mainframe", name}` (contract 11.1 item 2),
  with `attributes.language` `pli`, `main`, `options`, secondary `entries`, the `files` it
  opens by DD name and, for DL/I programs, `dli_segments`;
- each internal procedure as a `Symbol` the Program CONTAINS;
- one behaviour unit (kind `function`) per procedure, owned by its Program or Symbol;
- CALLS: to an internal procedure exactly, to an external entry as
  `Program {platform: "mainframe", name}` without declaring it (so it links only when the
  repository defines that program, in any language), and EXEC CICS LINK and XCTL to a
  literal program the same way; a CALL through an ENTRY variable or a parameter stays
  dynamic, with a diagnostic;
- `%INCLUDE` and `EXEC SQL INCLUDE` as `DEPENDS_ON DataSchema {kind: "include", name}`,
  not declared, and an include member's structures as `Field {table: "pli:<member>", name}`
  that its DataSchema CONTAINS (item 3);
- the tables embedded SQL reads and writes (item 12), through the shared SQL scanner;
- one `dd_file` observation per file constant (item 5): its DD name (a literal TITLE, else
  the file name), its mode from OPEN (else from its declaration) and the record it is read
  into or written from;
- `RENDERS Screen {platform: "bms", name: "<mapset>.<map>"}` for EXEC CICS SEND MAP with
  literal names (item 7), not declared.

Data reads and writes and external calls are stated by the external procedure (the load
module), wherever in it they appear; the behaviour units keep where each happens.
"""

from __future__ import annotations

from typing import Any

from repo_intel.platforms.pli import declare as D
from repo_intel.platforms.pli.common import MAINFRAME, Emit, dd_mode, decode, member_name, scrub_credentials
from repo_intel.platforms.pli.lexer import source, tokens
from repo_intel.platforms.pli.parse import Proc, Result, parse

LANGUAGE = "pli"
SUFFIXES = (".pli", ".pl1")
MAX_BYTES = 4 * 1024 * 1024


class PliSource:
    id = "pli.source"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = decode(data)
        src = source(text)
        e = Emit(self.id, data, blob, text)
        for a, b in src.process:
            e.construct(a, b, "pli:process", True)
        if src.column_one:
            e.note(f"{src.column_one} line(s) have text in column 1, outside the margins {src.margins}: it is ignored, as the compiler ignores it", "warning")
        res = parse(tokens(src.code), src.code, e.at)
        for cid, modelled, a, b in res.constructs:
            e.construct(a, b, cid, modelled)
        for n in res.notes:
            e.note(n)
        if res.tops:
            _programs(e, res)
        elif res.top_decls:
            _member(e, res, member_name(path))
        else:
            e.note("no procedure and no declaration found")
        return e.done()


# ------------------------------------------------------------------ programs


def _symbol_names(top: Proc) -> dict[int, str]:
    """Each internal procedure's Symbol name: its own, or qualified by its parents when two
    internal procedures of the program share it."""
    nested: list[Proc] = []

    def walk(p: Proc) -> None:
        for c in p.children:
            nested.append(c)
            walk(c)

    walk(top)
    counts: dict[str, int] = {}
    for p in nested:
        counts[p.name] = counts.get(p.name, 0) + 1
    out: dict[int, str] = {}
    for p in nested:
        if counts[p.name] == 1:
            out[id(p)] = p.name
        else:
            parts, q = [p.name], p.parent
            while q is not None and q is not top:
                parts.append(q.name)
                q = q.parent
            out[id(p)] = ".".join(reversed(parts))
    return out


def _programs(e: Emit, res: Result) -> None:
    K, P = e.K, e.P
    first = res.tops[0]
    top_of = lambda proc: proc.top if proc is not None else first  # noqa: E731

    # What each program says about its files, DL/I segments and maps, before it is declared.
    files: dict[int, dict[str, dict[str, Any]]] = {id(t): {} for t in res.tops}
    for name, decl in sorted(res.files.items()):
        t = top_of(decl.proc)
        opts = decl.options
        attrs = opts.get("attributes", [])
        files[id(t)][name] = {"decl": decl, "modes": set(), "dcl_modes": {m.lower() for m in attrs if m in ("INPUT", "OUTPUT", "UPDATE")},
                              "title": opts.get("title"), "title_dynamic": opts.get("title_dynamic", False), "record": None, "at": (decl.element.start, decl.element.end)}
    segments: dict[int, set[str]] = {id(t): set() for t in res.tops}
    maps: dict[int, set[str]] = {id(t): set() for t in res.tops}
    for f in res.facts:
        t = top_of(f.proc)
        if f.kind in ("open", "io"):
            entry = files[id(t)].setdefault(f.data["file"], {"decl": None, "modes": set(), "dcl_modes": set(), "title": None, "title_dynamic": False, "record": None, "at": (f.start, f.end)})
            if f.kind == "open":
                if f.data.get("mode"):
                    entry["modes"].add(f.data["mode"])
                if f.data.get("title"):
                    entry["title"] = f.data["title"]
                if f.data.get("title_dynamic"):
                    entry["title_dynamic"] = True
            elif entry["record"] is None and f.data.get("record"):
                entry["record"] = f.data["record"]
        elif f.kind == "dli":
            segments[id(t)].update(f.data["segments"])
        elif f.kind == "map":
            maps[id(t)].add(f"{f.data['mapset']}.{f.data['map']}")

    refs: dict[int, dict[str, Any]] = {}
    dd_rows: list[tuple[Proc, str, dict[str, Any], str | None]] = []
    for t in res.tops:
        rows = []
        for fname, info in sorted(files[id(t)].items()):
            if fname in ("SYSPRINT", "SYSIN") and info["decl"] is None:
                continue  # the default stream files, used without being declared
            mode = dd_mode({{"update": "i-o"}.get(m, m) for m in (info["modes"] or info["dcl_modes"])})
            if info["title_dynamic"] and not info["title"]:
                e.note(f"file {fname}: its TITLE is set at run time, so its DD name is not known")
                continue
            dd = (info["title"] or fname)[:8] if info["title"] else (fname if len(fname) <= 8 else None)
            if dd is None:
                e.note(f"file {fname}: a name longer than 8 characters with no TITLE; the DD name is not read")
                continue
            rows.append({"file": fname, "dd": dd, "mode": mode})
            dd_rows.append((t, fname, info, mode))
        attrs: dict[str, Any] = {"language": LANGUAGE, "main": "MAIN" in t.options}
        if t.options:
            attrs["options"] = t.options
        entries = sorted(set(t.entries) | set(t.labels[:-1]))
        if entries:
            attrs["entries"] = entries
        if t.package:
            attrs["package"] = t.package
        if rows:
            attrs["files"] = sorted(rows, key=lambda r: (r["dd"], r["file"]))
        if segments[id(t)]:
            attrs["dli_segments"] = sorted(segments[id(t)])
        if maps[id(t)]:
            attrs["maps"] = sorted(maps[id(t)])
        refs[id(t)] = e.term(t.start, t.head_end, "pli:Program", {"platform": MAINFRAME, "name": t.name}, t.name, attrs)

    for t, fname, info, mode in dd_rows:
        dd = (info["title"] or fname)[:8]
        a, b = info["at"]
        e.dd_file(a, b, refs[id(t)], dd, mode, fname, info["record"])

    # Internal procedures, behaviour, calls.
    owner: dict[int, dict[str, Any]] = {}
    by_name: dict[int, dict[str, dict[str, Any]]] = {}
    for t in res.tops:
        owner[id(t)] = refs[id(t)]
        names = _symbol_names(t)
        by_name[id(t)] = {}
        stack = list(reversed(t.children))  # depth first, in source order: the next is at the end
        while stack:
            p = stack.pop()
            name = names[id(p)]
            attrs = {"language": LANGUAGE}
            if p.params:
                attrs["params"] = p.params
            if p.parent is not t:
                attrs["parent"] = names[id(p.parent)]
            if p.entries:
                attrs["entries"] = sorted(p.entries)
            sym = e.term(p.start, p.head_end, "pli:Procedure", {"platform": MAINFRAME, "program": t.name, "name": name}, name, attrs)
            e.rel(p.start, p.head_end, refs[id(t)], P.CONTAINS, sym)
            owner[id(p)] = sym
            for label in [p.name, *p.labels, *p.entries]:
                by_name[id(t)].setdefault(label, sym)
            stack += reversed(p.children)
    for p in res.procs:
        scrub_credentials(p.body)
        params = [{"name": x} for x in p.params]
        e.behavior(p.start, p.end, owner[id(p)], LANGUAGE, p.body, params, kind="function")

    top_by_name: dict[str, Proc] = {}
    for x in res.tops:
        top_by_name.setdefault(x.name, x)
    for f in res.facts:
        t = top_of(f.proc)
        prog = refs[id(t)]
        subject = owner.get(id(f.proc), prog) if f.proc is not None else prog
        if f.kind == "call":
            name = f.data["name"]
            internal = by_name[id(t)].get(name)
            a, b = f.data.get("stmt", (f.start, f.end))
            if internal is not None and f.data.get("via") is None:
                e.rel(f.start, f.end, subject, P.CALLS, internal)
                e.construct(a, b, "pli:call", True)
            elif f.data.get("via") is None and (name in res.info.entry_vars or name in _params(f.proc) or (name in res.info.data and name not in res.info.entries)):
                e.construct(a, b, "pli:call_dynamic", False)
                e.note(f"CALL {name} at line {e.line(f.start)} goes through a variable or parameter: the program is decided at run time")
            elif name in top_by_name and name != t.name:
                e.rel(f.start, f.end, prog, P.CALLS, refs[id(top_by_name[name])])
                if f.data.get("via") is None:
                    e.construct(a, b, "pli:call", True)
            else:
                q = {"via": f.data["via"]} if f.data.get("via") else {}
                e.rel(f.start, f.end, prog, P.CALLS, e.program(name), q)
                if f.data.get("via") is None:
                    e.construct(a, b, "pli:call", True)
        elif f.kind == "dynamic_call":
            e.note(f"EXEC CICS at line {e.line(f.start)} names its program through a variable: not resolved")
        elif f.kind == "include":
            member = f.data["member"]
            for target in ([t] if f.proc is not None else res.tops):
                e.rel(f.start, f.end, refs[id(target)], P.DEPENDS_ON, e.base.ref(K.DATA_SCHEMA, {"kind": "include", "name": member}, member, declare=False))
        elif f.kind == "sql":
            for target in ([t] if f.proc is not None else res.tops):
                e.embedded_sql(refs[id(target)], f.data["text"], f.data["offset"])
        elif f.kind == "dynamic_sql":
            e.unresolved += 1
            e.note(f"dynamic SQL at line {e.line(f.start)}: its statement is built at run time and is not read for tables")
        elif f.kind == "map" and f.data.get("send"):
            name = f"{f.data['mapset']}.{f.data['map']}"
            e.rel(f.start, f.end, prog, P.RENDERS, e.base.ref(K.SCREEN, {"platform": "bms", "name": name}, name, declare=False))


def _params(proc: Proc | None) -> set[str]:
    out: set[str] = set()
    while proc is not None:
        out.update(proc.params)
        proc = proc.parent
    return out


# ------------------------------------------------------------------ include members


def _member(e: Emit, res: Result, member: str) -> None:
    P = e.P
    table = f"pli:{member}"
    rows: list[D.FieldRow] = []
    for items in res.top_decls:
        for root in D.tree(items):
            if D.kind_of(root) != "data":
                continue
            e.construct(root.start, root.end, "pli:member_declare", True)
            rows += D.layout(root)
    if not rows:
        e.note("an include member with no data declarations")
        return
    named = [r for r in rows if r.path[-1] != "*"]
    for r in rows:
        if r.path[-1] == "*":
            e.construct(r.element.start, r.element.end, "pli:unnamed_element", False)
    first = min(r.element.start for r in rows)
    last = max(r.element.end for r in rows)
    schema = e.term(first, last, "pli:IncludeMember", {"kind": "include", "name": member}, member, {"language": LANGUAGE, "fields": len(named), "layouts": sorted({r.path[0] for r in named if len(r.path) == 1})})
    names = _field_names([r.path for r in named])
    seen: set[str] = set()
    for r, name in zip(named, names):
        if name in seen:
            e.note(f"{'.'.join(r.path)} is declared twice in the member: the second is not read", "warning")
            continue
        seen.add(name)
        attrs = dict(r.attrs)
        if len(r.path) > 1:
            attrs["parent"] = r.path[-2]
        f = e.term(r.element.start, r.element.end, "pli:Field", {"table": table, "name": name}, name, attrs)
        e.rel(r.element.start, r.element.end, schema, P.CONTAINS, f)


def _field_names(paths: list[list[str]]) -> list[str]:
    """A field's name is its data name; a name repeated in the member is qualified by its
    parent (`<parent>.<name>`), and by more of its path while that is still repeated."""
    out = [p[-1] for p in paths]
    for depth in range(2, max((len(p) for p in paths), default=1) + 1):
        counts: dict[str, int] = {}
        for n in out:
            counts[n] = counts.get(n, 0) + 1
        if all(c == 1 for c in counts.values()):
            break
        out = [".".join(p[-depth:]) if counts[n] > 1 and len(p) >= depth else n for p, n in zip(paths, out)]
    return out

