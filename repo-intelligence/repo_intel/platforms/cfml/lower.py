"""Lowering CFML tag syntax into the behavioural IR.

`Lowerer.nodes` walks a markup tree (`markup.parse`) in document order. Text and
HTML become `output` statements: static text as a span, text inside `cfoutput` as
a template whose `#...#` parts are parsed expressions. Each CFML tag lowers to its
neutral statement where one exists (`cfset` -> assign, `cfif` -> if, `cfquery` ->
query with `cfqueryparam`s as `:name` parameters) and to a `platform` node named by
its construct id otherwise. A `cffunction` is lowered into its own unit and leaves
nothing in the enclosing body.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.cfml.ir import (
    Ctx, Expr, Function, QueryFact, Stmt, assign_target, cf_type, clean_attrs, custom_tag, invoke, lit, lit_str, platform, roles_list, sql_type,
    tag_statements, var,
)
from repo_intel.platforms.cfml.markup import Attr, Node
from repo_intel.platforms.cfml.scan import hash_parts
from repo_intel.platforms.cfml.script import param_name, parse_statements

_VAR_DECL = re.compile(r"\bvar\s+([A-Za-z_]\w*)", re.I)
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
# Tags that sit in the flow of HTML output: they render a control.
_INLINE_UI = frozenset({"cfinput"})
# Where output inside a tag's body goes.
_SINKS = {"cfsavecontent": "capture", "cfsilent": "silent", "cfxml": "capture", "cfdocument": "report", "cfmail": "mail", "cfmailpart": "mail"}


_INLINE_EXPR = re.compile(r"#[^#\n]*#")


def mask_sql(raw: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """SQL with parameters, tags and `#expressions#` masked, offsets kept: what table lineage reads.

    The masks are the ones `legacy.cfml` used, so lineage finds the same tables. Also
    returns the spans (offsets into `raw`) of the `#expressions#` substituted into the
    SQL text: a table name that overlaps one is computed at run time."""
    raw = re.sub(r"<!---.*?--->", lambda m: re.sub(r"[^\n]", " ", m.group()), raw, flags=re.S)
    raw = re.sub(r"<cfqueryparam\b[^>]*>", lambda m: "0".ljust(len(m.group())), raw, flags=re.I)
    raw = re.sub(r"</?cf\w+\b[^>]*>", lambda m: " " * len(m.group()), raw, flags=re.I)
    spans = tuple(m.span() for m in _INLINE_EXPR.finditer(raw) if m.end() - m.start() > 2)  # `##` is an escaped `#`
    return _INLINE_EXPR.sub(lambda m: "x".ljust(len(m.group()), "_"), raw), spans


class Lowerer:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.text = ctx.text

    # -- attribute values

    def attr_expr(self, a: Attr) -> Expr:
        at = self.ctx.at(a.start, a.end)
        if a.start == a.end and not a.quoted:
            return lit(True, at)  # a bare attribute: <input required>
        if not any(p[0] == "expr" for p in a.parts):
            return lit("".join(p[3] or "" for p in a.parts), at)
        if len(a.parts) == 1:
            _, pa, pb, _ = a.parts[0]
            return self.ctx.parse_expr(pa, pb)
        parts: list[Any] = []
        for kind, pa, pb, value in a.parts:
            parts.append(value if kind == "lit" else self.ctx.parse_expr(pa, pb))
        return {"e": "template", "at": at, "parts": parts}

    def attrs(self, node: Node) -> dict[str, Expr]:
        return {k: self.attr_expr(a) for k, a in node.attrs.items()}

    def number(self, e: Expr | None) -> Expr | None:
        s = lit_str(e)
        if s is not None and _NUMBER.match(s.strip()):
            value = float(s) if "." in s else int(s)
            return {**e, "value": value, "type": "number"}
        return e

    def condition(self, a: Attr) -> Expr:
        """A `condition="i LTE 10"` attribute: the string is itself an expression."""
        if a.quoted:
            return self.ctx.parse_expr(a.start + 1, max(a.start + 1, a.end - 1))
        return self.ctx.parse_expr(a.start, a.end)

    # -- bodies

    def nodes(self, children: list[Node]) -> list[Stmt]:
        out: list[Stmt] = []
        run: list[Node] = []
        for n in children:
            if n.kind in ("text", "html") or (n.kind == "tag" and n.name in _INLINE_UI):
                if run and n.start != run[-1].end:  # not adjacent (a cfcatch was taken out between): its own run
                    out.extend(self.flush(run))
                    run = []
                run.append(n)
                continue
            if run:
                out.extend(self.flush(run))
                run = []
            out.extend(self.tag(n))
        if run:
            out.extend(self.flush(run))
        return out

    def flush(self, run: list[Node]) -> list[Stmt]:
        a, b = run[0].start, run[-1].end
        if not self.text[a:b].strip():
            return []
        if not any(n.in_output for n in run) or self.ctx.sinks[-1] == "silent":
            if self.ctx.sinks[-1] == "silent":
                return []
            return [self.ctx.output(self.ctx.at(a, b), static=True)]
        parts = hash_parts(self.text, a, b)
        if not any(p[0] == "expr" for p in parts):
            return [self.ctx.output(self.ctx.at(a, b), static=True)]
        values: list[Any] = []
        for kind, pa, pb, value in parts:
            values.append(value if kind == "lit" else self.ctx.parse_expr(pa, pb))
        value = values[0] if len(values) == 1 and not isinstance(values[0], str) else {"e": "template", "at": self.ctx.at(a, b), "parts": values}
        return [self.ctx.output(self.ctx.at(a, b), value)]

    def body(self, node: Node) -> list[Stmt] | None:
        if not node.children and not node.closed:
            return None
        sink = _SINKS.get(node.name)
        if sink:
            self.ctx.sinks.append(sink)
        try:
            return self.nodes(node.children)
        finally:
            if sink:
                self.ctx.sinks.pop()

    # -- tags

    def tag(self, n: Node) -> list[Stmt]:
        name = n.name
        method = getattr(self, "t_" + name, None)
        if method is not None:
            return method(n)
        if name.startswith("cf_"):
            return custom_tag(self.ctx, name, self.attrs(n), self.body(n), n.start, n.end)
        short = name[2:]
        if short in ("invoke",):
            return invoke(self.ctx, self.attrs(n), self.body(n) or [], n.start, n.end)
        return tag_statements(self.ctx, short, self.attrs(n), self.body(n), n.start, n.end)

    def t_cfset(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfset", n.start, n.end)
        a, b = n.expr or (n.open_end, n.open_end)
        return parse_statements(self.ctx, a, b)

    def t_cfif(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfif", n.start, n.end)
        segments: list[tuple[Node, Expr | None, list[Node]]] = [(n, self.ctx.parse_expr(*n.expr) if n.expr else lit(True), [])]
        for child in n.children:
            if child.kind == "tag" and child.name == "cfelseif":
                self.ctx.construct("cf:cfelseif", child.start, child.end)
                segments.append((child, self.ctx.parse_expr(*child.expr) if child.expr else lit(True), []))
            elif child.kind == "tag" and child.name == "cfelse":
                self.ctx.construct("cf:cfelse", child.start, child.end)
                segments.append((child, None, []))
            else:
                segments[-1][2].append(child)
        tail: list[Stmt] | None = None
        for start_node, cond, children in reversed(segments):
            body = self.nodes(children)
            if cond is None:
                tail = body
                continue
            stmt: Stmt = {"op": "if", "at": self.ctx.at(start_node.start, n.end), "cond": cond, "then": body}
            if tail is not None:
                stmt["else"] = tail
            tail = [stmt]
        return tail or []

    def t_cfloop(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfloop", n.start, n.end)
        at = self.ctx.at(n.start, n.end)
        attrs = self.attrs(n)
        body = self.nodes(n.children)
        name = lambda k: lit_str(attrs.get(k))
        loop_var = lambda k: assign_target(self.ctx, var(name(k))) if name(k) else None
        if "condition" in n.attrs:
            return [{"op": "loop", "at": at, "kind": "while", "cond": self.condition(n.attrs["condition"]), "body": body}]
        if "from" in attrs and "to" in attrs:
            stmt: Stmt = {"op": "loop", "at": at, "kind": "range", "from": self.number(attrs["from"]), "to": self.number(attrs["to"]), "body": body}
            if loop_var("index"):
                stmt["var"] = loop_var("index")
            if "step" in attrs:
                stmt["step"] = self.number(attrs["step"])
            return [stmt]
        for key in ("query", "array", "collection", "list", "file"):
            if key in attrs:
                over = attrs[key]
                if key == "query" and lit_str(over):
                    over = var(lit_str(over), self.ctx.scope_of(lit_str(over)))
                stmt = {"op": "loop", "at": at, "kind": "each", "over": over, "source": key, "body": body}
                item = loop_var("item") or loop_var("index")
                if item is not None:
                    stmt["var"] = item
                if key == "list" and "delimiters" in attrs:
                    stmt["delimiters"] = attrs["delimiters"]
                if key == "query" and "group" in attrs:
                    stmt["group"] = attrs["group"]
                return [stmt]
        return [platform(self.ctx, "cf:cfloop", at, clean_attrs(attrs), body)]

    def t_cfoutput(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfoutput", n.start, n.end)
        body = self.nodes(n.children)
        query = n.literal("query")
        if query:
            stmt: Stmt = {"op": "loop", "at": self.ctx.at(n.start, n.end), "kind": "each", "over": var(query, self.ctx.scope_of(query)), "source": "query", "body": body}
            if n.attr("group"):
                stmt["group"] = self.attr_expr(n.attrs["group"])
            if self.ctx.func is None and self.ctx.sinks[-1] == "response":
                self.ctx.renders = True
            return [stmt]
        if query is None and n.attr("query"):
            return [{"op": "loop", "at": self.ctx.at(n.start, n.end), "kind": "each", "over": self.attr_expr(n.attrs["query"]), "source": "query", "body": body}]
        return body

    def t_cfquery(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfquery", n.start, n.end)
        at = self.ctx.at(n.start, n.end)
        pieces: list[str] = []
        params: list[dict[str, Any]] = []
        taken: set[str] = set()
        dynamic = [False]

        def walk(children: list[Node]) -> None:
            for c in children:
                if c.kind in ("text", "html"):
                    for kind, pa, pb, value in hash_parts(self.text, c.start, c.end):
                        if kind == "lit":
                            pieces.append(value or "")
                        else:
                            e = self.ctx.parse_expr(pa, pb)
                            pname = param_name(e, taken, len(params))
                            pieces.append(f":{pname}")
                            params.append({"name": pname, "value": e, "type": "any", "inline": True})
                elif c.kind == "tag" and c.name == "cfqueryparam":
                    self.ctx.construct("cf:cfqueryparam", c.start, c.end)
                    value = self.attr_expr(c.attrs["value"]) if "value" in c.attrs else lit(None)
                    pname = param_name(value, taken, len(params))
                    pieces.append(f":{pname}")
                    param: dict[str, Any] = {"name": pname, "value": value, "type": sql_type(c.literal("cfsqltype") or c.literal("sqltype"))}
                    if c.literal("list") and c.literal("list").lower() in ("yes", "true"):
                        param["list"] = True
                    if "null" in c.attrs:
                        param["null"] = self.attr_expr(c.attrs["null"])
                    params.append(param)
                elif c.kind == "tag":
                    dynamic[0] = True
                    if c.expr is not None and c.name in ("cfif", "cfelseif"):
                        self.ctx.parse_expr(*c.expr)  # facts and constructs inside the condition
                    if c.name in ("cfif", "cfelseif", "cfelse"):
                        self.ctx.construct(f"cf:{c.name}", c.start, c.end)
                    elif c.name == "cfloop":
                        self.ctx.construct("cf:cfloop", c.start, c.end)
                    walk(c.children)

        walk(n.children)
        stmt: Stmt = {"op": "query", "at": at, "sql": re.sub(r"\s+", " ", "".join(pieces)).strip(), "params": params}
        if dynamic[0]:
            stmt["dynamic"] = True
        ds_attr = n.attr("datasource")
        ds = ds_attr.literal() if ds_attr else None
        if ds:
            stmt["datasource"] = ds
        elif ds_attr is not None:
            stmt["datasource_dynamic"] = True
        dbtype = (n.literal("dbtype") or "").lower() or None
        if dbtype:
            stmt["dbtype"] = dbtype
        target = n.literal("name")
        if target:
            stmt["target"] = assign_target(self.ctx, var(target))
        if n.literal("result"):
            stmt["result"] = n.literal("result")
        region = n.body if n.body else (n.open_end, n.open_end)
        masked, inline = mask_sql(self.text[region[0]: region[1]])
        self.ctx.queries.append(QueryFact(self.ctx.func, region, masked, ds, (ds_attr.start, ds_attr.end) if ds and ds_attr else None, (n.start, n.end), dbtype, inline))
        return [stmt]

    def t_cfscript(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfscript", n.start, n.end)
        a, b = n.body or (n.open_end, n.open_end)
        return parse_statements(self.ctx, a, b)

    def t_cftry(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cftry", n.start, n.end)
        body_nodes: list[Node] = []
        catches = []
        final: list[Stmt] | None = None
        for c in n.children:
            if c.kind == "tag" and c.name == "cfcatch":
                self.ctx.construct("cf:cfcatch", c.start, c.end)
                self.ctx.frames.append(({"cfcatch"}, set()))
                try:
                    cbody = self.nodes(c.children)
                finally:
                    self.ctx.frames.pop()
                catches.append({"type": (c.literal("type") or "any").lower(), "var": "cfcatch", "body": cbody})
            elif c.kind == "tag" and c.name == "cffinally":
                self.ctx.construct("cf:cffinally", c.start, c.end)
                final = self.nodes(c.children)
            else:
                body_nodes.append(c)
        stmt: Stmt = {"op": "try", "at": self.ctx.at(n.start, n.end), "body": self.nodes(body_nodes), "catches": catches}
        if final is not None:
            stmt["finally"] = final
        return [stmt]

    def t_cfswitch(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfswitch", n.start, n.end)
        on = self.attr_expr(n.attrs["expression"]) if "expression" in n.attrs else lit("")
        cases = []
        default: list[Stmt] | None = None
        for c in n.children:
            if c.kind == "tag" and c.name == "cfcase":
                self.ctx.construct("cf:cfcase", c.start, c.end)
                value = c.attr("value")
                delims = c.literal("delimiters") or ","
                if value is None:
                    match: list[Expr] = []
                elif value.literal() is not None:
                    match = [lit(v) for v in re.split("|".join(re.escape(d) for d in delims), value.literal())] if delims else [lit(value.literal())]
                else:
                    match = [self.attr_expr(value)]
                cases.append({"match": match, "body": self.nodes(c.children)})
            elif c.kind == "tag" and c.name == "cfdefaultcase":
                self.ctx.construct("cf:cfdefaultcase", c.start, c.end)
                default = self.nodes(c.children)
        stmt: Stmt = {"op": "switch", "at": self.ctx.at(n.start, n.end), "on": on, "cases": cases}
        if default is not None:
            stmt["default"] = default
        return [stmt]

    def t_cffunction(self, n: Node) -> list[Stmt]:
        name_attr = n.attr("name")
        fname = name_attr.literal() if name_attr else None
        if not fname:
            self.ctx.note("cffunction without a literal name: not modelled", n.start)
            self.ctx.construct("cf:cffunction", n.start, n.end, False)
            return []
        self.ctx.construct("cf:cffunction", n.start, n.end)
        fn = Function(fname, n.start, n.end, name_attr.start + (1 if name_attr.quoted else 0), name_attr.end - (1 if name_attr.quoted else 0), script=False)
        fn.access = (n.literal("access") or "public").lower()
        fn.returntype = n.literal("returntype")
        fn.output = (n.literal("output") or "").lower() or None
        if n.attr("roles") is not None:
            fn.roles = roles_list(n.literal("roles"))
            fn.roles_at = (n.attrs["roles"].start, n.attrs["roles"].end)
            if n.literal("roles") is None:
                self.ctx.note(f"cffunction {fname} with computed roles: roles not resolved", n.start)
        body_nodes = []
        for c in n.children:
            if c.kind == "tag" and c.name == "cfargument":
                self.ctx.construct("cf:cfargument", c.start, c.end)
                pname = c.literal("name")
                if not pname:
                    continue
                required = (c.literal("required") or "no").lower() in ("yes", "true")
                param: dict[str, Any] = {"name": pname, "type": cf_type(c.literal("type")), "required": required}
                raw_type = c.literal("type")
                if raw_type and cf_type(raw_type) == "any" and raw_type.lower() != "any":
                    param["declared_type"] = raw_type
                if "default" in c.attrs:
                    param["default"] = self.number(self.attr_expr(c.attrs["default"]))
                fn.params.append(param)
                fn.args.add(pname.lower())
            else:
                body_nodes.append(c)
        fn.locals = self._locals(n)
        saved, frames, sinks = self.ctx.func, self.ctx.frames, self.ctx.sinks
        self.ctx.func, self.ctx.frames, self.ctx.sinks = fn, [], ["response"]
        try:
            fn.body = self.nodes(body_nodes)
        finally:
            self.ctx.func, self.ctx.frames, self.ctx.sinks = saved, frames, sinks
        self.ctx.functions.append(fn)
        return []

    def _locals(self, n: Node) -> set[str]:
        """Names a tag function declares with `var`, anywhere in its body."""
        names: set[str] = set()
        stack = list(n.children)
        while stack:
            c = stack.pop()
            if c.kind != "tag":
                continue
            if c.name == "cfset" and c.expr:
                m = re.match(r"\s*var\s+([A-Za-z_]\w*)", self.text[c.expr[0]: c.expr[1]], re.I)
                if m:
                    names.add(m.group(1).lower())
            elif c.name == "cfscript" and c.body:
                names.update(m.group(1).lower() for m in _VAR_DECL.finditer(self.text, c.body[0], c.body[1]))
            elif c.name != "cffunction":
                stack.extend(c.children)
        return names

    def t_cfcomponent(self, n: Node) -> list[Stmt]:
        self.ctx.construct(f"cf:{n.name}", n.start, n.end)
        self.ctx.component = {k: v for k, v in ((k, a.literal()) for k, a in n.attrs.items()) if v is not None}
        self.ctx.component["kind"] = "component" if n.name == "cfcomponent" else "interface"
        return self.nodes(n.children)

    t_cfinterface = t_cfcomponent

    def t_cfreturn(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfreturn", n.start, n.end)
        at = self.ctx.at(n.start, n.end)
        if not n.expr or not self.text[n.expr[0]: n.expr[1]].strip():
            return [{"op": "return", "at": at}]
        value = self.ctx.parse_expr(*n.expr)
        query = self.ctx.query_ops.get(id(value))
        if query is not None:
            return [{**query, "at": at, "returned": True}]
        return [{"op": "return", "at": at, "value": value}]

    def t_cfthrow(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfthrow", n.start, n.end)
        attrs = clean_attrs(self.attrs(n))
        stmt: Stmt = {"op": "throw", "at": self.ctx.at(n.start, n.end)}
        if "object" in attrs:
            stmt["value"] = attrs["object"]
        elif attrs:
            stmt["value"] = {"e": "struct", "fields": attrs}
        return [stmt]

    def t_cfrethrow(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfrethrow", n.start, n.end)
        return [{"op": "throw", "at": self.ctx.at(n.start, n.end), "rethrow": True}]

    def t_cfbreak(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfbreak", n.start, n.end)
        return [{"op": "break", "at": self.ctx.at(n.start, n.end)}]

    def t_cfcontinue(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfcontinue", n.start, n.end)
        return [{"op": "continue", "at": self.ctx.at(n.start, n.end)}]

    def t_cfargument(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfargument", n.start, n.end, False)  # outside a function: it declares nothing
        return []

    def t_cfproperty(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfproperty", n.start, n.end)
        if n.literal("name"):
            self.ctx.properties.append({"name": n.literal("name"), "type": n.literal("type"), "column": n.literal("column"), "fieldtype": n.literal("fieldtype")})
        return []

    def _stray(self, n: Node) -> list[Stmt]:
        self.ctx.note(f"<{n.name}> outside the tag it belongs to", n.start)
        self.ctx.construct(f"cf:{n.name}", n.start, n.end, False)
        return self.nodes(n.children)

    t_cfelse = t_cfelseif = t_cfcatch = t_cffinally = t_cfcase = t_cfdefaultcase = _stray

    def t_cfqueryparam(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfqueryparam", n.start, n.end, False)  # outside a query
        return []

    def t_cfmail(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfmail", n.start, n.end)
        fields = clean_attrs(self.attrs(n))
        body = self.body(n) or []
        return [{"op": "output", "at": self.ctx.at(n.start, n.end), "channel": "mail", "value": {"e": "struct", "fields": fields}}, *body]

    def t_cfmailpart(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfmailpart", n.start, n.end)
        return self.body(n) or []

    def _ui_container(self, n: Node) -> list[Stmt]:
        out = [self.ctx.output(self.ctx.at(n.start, n.open_end), static=True)]
        return out + self.nodes(n.children)

    t_cfform = t_cfselect = t_cftextarea = _ui_container

    def t_cfsavecontent(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfsavecontent", n.start, n.end)
        body = self.body(n) or []
        detail: dict[str, Any] = {}
        if n.attr("variable"):
            detail["variable"] = self.attr_expr(n.attrs["variable"])
        return [platform(self.ctx, "cf:cfsavecontent", self.ctx.at(n.start, n.end), {}, body, **detail)]

    def t_cfsilent(self, n: Node) -> list[Stmt]:
        self.ctx.construct("cf:cfsilent", n.start, n.end)
        return [platform(self.ctx, "cf:cfsilent", self.ctx.at(n.start, n.end), {}, self.body(n) or [])]
