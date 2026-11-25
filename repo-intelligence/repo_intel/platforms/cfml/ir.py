"""The parse context for one CFML file, IR builders, and the lowering of tags shared by
tag syntax (`<cflock name="x">`) and script syntax (`lock name="x" { }`, `cflock(...)`).

Lowering writes the behavioural IR of `repo_intel.compile.behavior` and, as it goes,
records the facts the extractor turns into entities and relations: functions,
queries, component and template references, role checks, redirects, scheduled
tasks and `Application.cfc` settings. It also records every construct occurrence for
coverage. Nothing is resolved by guessing: a target that is not a literal stays an
expression, and a diagnostic says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.cfml.scan import byte_offsets

Expr = dict[str, Any]
Stmt = dict[str, Any]

SCOPES = frozenset({"form", "url", "session", "application", "request", "cgi", "variables", "arguments", "local", "this", "client", "server", "cookie", "thread", "attributes", "caller"})
# Scopes whose use is counted as a construct: shared state and request input.
COUNTED_SCOPES = frozenset({"session", "application", "client", "server", "cookie", "request", "form", "url", "cgi"})
LIFECYCLE = {e.lower(): e for e in ("onApplicationStart", "onApplicationEnd", "onSessionStart", "onSessionEnd", "onRequestStart", "onRequest", "onRequestEnd", "onCFCRequest", "onError", "onMissingTemplate", "onAbort")}
# Attribute names whose values are credentials: never copied, in any tag.
SECRET_ATTRS = frozenset({"password", "username", "user", "passwd", "pwd", "proxypassword", "proxyuser", "clientcertpassword", "apikey", "secret", "token", "key", "authpassword", "authusername"})
# Mail address attributes: literal addresses are personal data and are not copied.
ADDRESS_ATTRS = frozenset({"to", "from", "cc", "bcc", "replyto", "failto"})
CF_TYPES = {
    "numeric": "number", "number": "number", "integer": "integer", "string": "string", "boolean": "boolean", "date": "date", "datetime": "datetime",
    "array": "array", "struct": "struct", "query": "query", "any": "any", "binary": "binary", "uuid": "string", "guid": "string", "email": "string",
    "variablename": "string", "xml": "any", "function": "any", "component": "any", "void": "null",
}
SQL_TYPES = {
    "integer": "integer", "bigint": "integer", "smallint": "integer", "tinyint": "integer", "varchar": "string", "char": "string", "longvarchar": "string",
    "nvarchar": "string", "nchar": "string", "clob": "string", "date": "date", "time": "datetime", "timestamp": "datetime", "bit": "boolean",
    "decimal": "number", "numeric": "number", "float": "number", "double": "number", "real": "number", "money": "number", "money4": "number",
    "blob": "binary", "varbinary": "binary", "binary": "binary",
}


def cf_type(raw: str | None) -> str:
    return CF_TYPES.get((raw or "any").lower(), "any")


def sql_type(raw: str | None) -> str:
    name = (raw or "").lower().removeprefix("cf_sql_").removeprefix("cf_")
    return SQL_TYPES.get(name, "any")


@dataclass
class Function:
    name: str
    start: int
    end: int
    name_start: int
    name_end: int
    access: str = "public"
    returntype: str | None = None
    roles: list[str] = field(default_factory=list)
    output: str | None = None
    params: list[dict[str, Any]] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    script: bool = False
    locals: set[str] = field(default_factory=set)
    args: set[str] = field(default_factory=set)
    roles_at: tuple[int, int] | None = None


@dataclass
class QueryFact:
    owner: Function | None
    region: tuple[int, int] | None  # SQL text in the file, for table lineage
    masked: str | None  # that region with parameters and expressions masked, offsets kept
    datasource: str | None
    ds_at: tuple[int, int] | None
    at: tuple[int, int]
    dbtype: str | None = None


@dataclass
class Ref:
    """A reference by name to something another file defines."""

    kind: str  # "component", "include", "module", "custom_tag", "method", "function"
    target: str | None  # literal name or path; None when dynamic
    at: tuple[int, int]
    owner: Function | None
    method: str | None = None


class Ctx:
    def __init__(self, path: str, text: str, data: bytes):
        self.path, self.text, self.data = path, text, data
        self.to_byte = byte_offsets(text, data)
        self.func: Function | None = None
        self.frames: list[tuple[set[str], set[str]]] = []  # closures' own (locals, args)
        self.functions: list[Function] = []
        self.queries: list[QueryFact] = []
        self.refs: list[Ref] = []
        self.role_checks: list[tuple[str, tuple[int, int], Function | None, str]] = []
        self.roles_granted: list[tuple[str, tuple[int, int]]] = []
        self.redirects: list[tuple[str | None, tuple[int, int], Function | None]] = []
        self.schedules: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = {}  # Application.cfc `this.*` and cfapplication
        self.mappings: list[str] = []
        self.properties: list[dict[str, Any]] = []
        self.table_writes: list[tuple[str, tuple[int, int], Function | None, str | None]] = []
        self.datasources: list[tuple[str, tuple[int, int], Function | None]] = []  # other than queries
        self.constructs: list[tuple[str, int, int, bool]] = []
        self.diagnostics: list[str] = []
        self.renders = False
        self.sinks: list[str] = ["response"]
        self.query_ops: dict[int, Stmt] = {}  # id(queryExecute call expression) -> its query statement
        self.component: dict[str, Any] = {}
        self.depth = 0

    # -- positions and reports

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def line(self, i: int) -> int:
        return self.text.count("\n", 0, i) + 1

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.constructs.append((cid, a, b, modelled))

    def note(self, message: str, at: int) -> None:
        self.diagnostics.append(f"line {self.line(at)}: {message}")

    # -- scopes

    def scope_of(self, name: str) -> str | None:
        low = name.lower()
        for locals_, args in reversed(self.frames):
            if low in args:
                return "arguments"
            if low in locals_:
                return "local"
        if self.func is not None:
            if low in self.func.args:
                return "arguments"
            if low in self.func.locals:
                return "local"
        return None

    def write_scope(self, name: str) -> str:
        """Where an unscoped assignment writes: a declared local or argument, else `variables`."""
        return self.scope_of(name) or "variables"

    def output(self, at: list[int], value: Expr | None = None, **extra: Any) -> Stmt:
        sink = self.sinks[-1]
        stmt: Stmt = {"op": "output", "at": at, "channel": "response" if sink in ("response", "capture", "silent") else sink}
        if value is not None:
            stmt["value"] = value
        if sink == "capture":
            stmt["captured"] = True
        stmt.update(extra)
        if sink == "response" and self.func is None:
            self.renders = True
        return stmt

    def parse_expr(self, a: int, b: int) -> Expr:
        from repo_intel.platforms.cfml.script import parse_expression

        return parse_expression(self, a, b)


# ------------------------------------------------------------------ IR builders


def lit(value: Any, at: list[int] | None = None) -> Expr:
    if isinstance(value, bool):
        t = "boolean"
    elif isinstance(value, (int, float)):
        t = "number"
    elif value is None:
        t = "null"
    else:
        t = "string"
    e: Expr = {"e": "lit", "value": value, "type": t}
    if at:
        e["at"] = at
    return e


def var(name: str, scope: str | None = None, at: list[int] | None = None) -> Expr:
    e: Expr = {"e": "var", "name": name}
    if scope:
        e["scope"] = scope
    if at:
        e["at"] = at
    return e


def lit_str(e: Expr | None) -> str | None:
    if isinstance(e, dict) and e.get("e") == "lit" and isinstance(e.get("value"), str):
        return e["value"]
    return None


def literal_prefix(e: Expr | None) -> tuple[str | None, bool]:
    """The literal path a URL expression starts with, and whether the path itself is literal.

    `"save.cfm?id=#id#"` gives ("save.cfm", True): only the query string is dynamic."""
    s = lit_str(e)
    if s is not None:
        return re.split(r"[?#]", s, 1)[0], True
    if isinstance(e, dict) and e.get("e") == "template" and e["parts"] and isinstance(e["parts"][0], str):
        head = e["parts"][0]
        if "?" in head:
            return head.split("?", 1)[0], True
    return None, False


def assign_target(ctx: Ctx, e: Expr) -> Expr:
    """An unscoped variable written to lands in `variables` unless it is a local or an argument."""
    if e.get("e") == "var" and "scope" not in e:
        return {**e, "scope": ctx.write_scope(e["name"])}
    return e


def name_parts(raw: str) -> tuple[str | None, str]:
    """`form.qty` -> ("form", "qty"); `qty` -> (None, "qty")."""
    head, _, rest = raw.partition(".")
    if rest and head.lower() in SCOPES:
        return head.lower(), rest
    return None, raw


def clean_attrs(attrs: dict[str, Expr], drop: tuple[str, ...] = ()) -> dict[str, Expr]:
    """Attributes safe to keep: credentials and literal mail addresses are left out."""
    out = {}
    for k, v in attrs.items():
        if k in SECRET_ATTRS or k in drop:
            continue
        if k in ADDRESS_ATTRS and lit_str(v) is not None:
            continue
        out[k] = v
    return out


def roles_list(raw: str | None) -> list[str]:
    return [r.strip() for r in (raw or "").split(",") if r.strip()]


# ------------------------------------------------------------------ tags shared by both syntaxes

# Tags (by name without the "cf" prefix) with no neutral form: kept as platform nodes.
PLATFORM_TAGS = frozenset({
    "savecontent", "silent", "thread", "document", "documentitem", "documentsection", "pdf", "spreadsheet", "ftp", "directory", "execute", "registry",
    "login", "logout", "header", "content", "cookie", "setting", "flush", "processingdirective", "xml", "wddx", "image", "zip", "cache", "ldap",
    "pop", "imap", "search", "index", "collection", "ntauthenticate", "dbinfo", "timer", "associate", "storedproc", "procparam", "procresult",
    "insert", "update", "schedule", "loginuser", "htmlhead", "trace", "mailparam", "pdfparam", "zipparam", "queryparam",
})
UNMAPPED_TAGS = frozenset({
    "ajaxproxy", "websocket", "grid", "gridcolumn", "layout", "layoutarea", "div", "window", "pod", "report", "reportparam", "chart", "chartseries",
    "chartdata", "import", "menu", "menuitem", "tree", "treeitem", "tooltip", "slider",
})


def tag_statements(ctx: Ctx, name: str, attrs: dict[str, Expr], body: list[Stmt] | None, a: int, b: int) -> list[Stmt]:
    """Lower one attribute-driven tag (`name` without "cf", lower-case) given its attributes as expressions."""
    at = ctx.at(a, b)
    cid = f"cf:cf{name}"
    get = lambda k: attrs.get(k)
    s = lambda k: lit_str(attrs.get(k))

    if name == "location":
        target = get("url") or lit("")
        ctx.construct(cid, a, b)
        path, literal = literal_prefix(target)
        ctx.redirects.append((path if literal else None, (a, b), ctx.func))
        if not literal:
            ctx.note("cflocation to a computed URL: target not resolved", a)
        stmt = {"op": "redirect", "at": at, "target": target}
        if get("statuscode"):
            stmt["status"] = get("statuscode")
        return [stmt]
    if name == "include":
        target = get("template") or lit("")
        ctx.construct(cid, a, b)
        path = lit_str(target)
        ctx.refs.append(Ref("include", path if path and "#" not in path else None, (a, b), ctx.func))
        if path is None:
            ctx.note("cfinclude of a computed template: target not resolved", a)
        return [{"op": "include", "at": at, "target": target}]
    if name == "module":
        ctx.construct(cid, a, b)
        template, dotted = s("template"), s("name")
        target = get("template") or get("name") or lit("")
        if template:
            ctx.refs.append(Ref("module", template, (a, b), ctx.func))
        elif dotted:
            ctx.refs.append(Ref("module_name", dotted, (a, b), ctx.func))
        else:
            ctx.refs.append(Ref("module", None, (a, b), ctx.func))
            ctx.note("cfmodule with a computed template: target not resolved", a)
        stmt = {"op": "include", "at": at, "target": target, "module": True, "args": clean_attrs(attrs, ("template", "name"))}
        if body:
            stmt["body_statements"] = len(body)
            return [stmt, *body]
        return [stmt]
    if name in ("abort", "exit"):
        ctx.construct(cid, a, b)
        stmt = {"op": "exit", "at": at, "kind": name}
        if s("method"):
            stmt["method"] = s("method")
        return [stmt]
    if name == "lock":
        ctx.construct(cid, a, b)
        stmt = {"op": "lock", "at": at, "body": body or [], "kind": "readonly" if (s("type") or "").lower() == "readonly" else "exclusive"}
        if get("name") is not None:
            stmt["name"] = s("name") if s("name") is not None else get("name")
        if s("scope"):
            stmt["scope"] = s("scope").lower()
        return [stmt]
    if name == "transaction":
        ctx.construct(cid, a, b)
        stmt = {"op": "transaction", "at": at, "body": body or []}
        if s("action"):
            stmt["action"] = s("action").lower()
        if s("isolation"):
            stmt["isolation"] = s("isolation").lower()
        return [stmt]
    if name == "param":
        ctx.construct(cid, a, b)
        raw = s("name") or ""
        scope, short = name_parts(raw)
        stmt = {"op": "declare", "at": at, "name": short or raw, "param": True}
        if scope:
            stmt["scope"] = scope
        if get("default") is not None:
            stmt["value"] = get("default")
        if s("type"):
            stmt["type"] = cf_type(s("type"))
        return [stmt]
    if name in ("dump", "log", "trace", "htmlhead"):
        ctx.construct(cid, a, b)
        if name == "log":
            return [{"op": "output", "at": at, "channel": "log", **({"value": get("text")} if get("text") else {}), **({"log": s("file") or s("log")} if (s("file") or s("log")) else {})}]
        if name == "dump":
            return [ctx.output(at, get("var"), dump=True)]
        return [ctx.output(at, get("text"))]
    if name == "file":
        action = (s("action") or "").lower()
        ctx.construct(cid, a, b)
        if action in ("write", "append"):
            return [{"op": "output", "at": at, "channel": "file", "action": action, **({"value": get("output")} if get("output") else {}), **({"file": get("file")} if get("file") else {})}]
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name == "http":
        ctx.construct(cid, a, b)
        named = clean_attrs(attrs, ("result",))
        call: Expr = {"e": "call", "callee": {"e": "name", "name": "cfhttp", "kind": "builtin"}, "args": [], "named": named, "at": at}
        if body:
            call["named"] = {**named, "params": {"e": "array", "items": [st["call"] for st in body if st.get("op") == "call" and st.get("httpparam")]}}
        target = var(s("result") or "cfhttp", None)
        return [{"op": "assign", "at": at, "target": assign_target(ctx, target), "value": call}]
    if name == "httpparam":
        ctx.construct(cid, a, b)
        return [{"op": "call", "at": at, "httpparam": True, "call": {"e": "struct", "fields": clean_attrs(attrs)}}]
    if name == "invoke":
        return invoke(ctx, attrs, body or [], a, b)
    if name == "invokeargument":
        ctx.construct(cid, a, b)
        return [{"op": "call", "at": at, "argument": s("name") or "", "call": get("value") or lit(None)}]
    if name == "object":
        ctx.construct(cid, a, b)
        kind = (s("type") or ("component" if get("component") is not None else "java")).lower()
        type_expr = get("component") or get("class") or get("webservice")
        type_name = lit_str(type_expr)
        if kind == "component":
            ctx.refs.append(Ref("component", type_name, (a, b), ctx.func))
        if type_name is None:
            ctx.note("cfobject of a computed type: target not resolved", a)
        new = {"e": "new", "type": type_name or "?", "kind": kind, "at": at}
        if type_name is None and type_expr is not None:
            new["type_expr"] = type_expr
        return [{"op": "assign", "at": at, "target": assign_target(ctx, var(s("name") or "object")), "value": new}]
    if name == "schedule":
        ctx.construct(cid, a, b)
        task = s("task")
        url = s("url")
        path = None
        if url:
            path = _url_path(url)
        ctx.schedules.append({"task": task, "group": s("group") or "default", "path": path, "interval": s("interval"), "action": (s("action") or "").lower(), "at": (a, b)})
        if task is None:
            ctx.note("cfschedule with a computed task name: not modelled as a task", a)
        return [platform(ctx, cid, at, clean_attrs(attrs, ("url",)), body, url_path=path)]
    if name == "loginuser":
        ctx.construct(cid, a, b)
        roles = s("roles")
        for role in roles_list(roles):
            ctx.roles_granted.append((role, (a, b)))
        if get("roles") is not None and roles is None:
            ctx.note("cfloginuser with computed roles: roles not resolved", a)
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name in ("insert", "update"):
        ctx.construct(cid, a, b)
        table = s("tablename")
        if table:
            ctx.table_writes.append((table, (a, b), ctx.func, s("datasource")))
        if s("datasource"):
            ctx.datasources.append((s("datasource"), (a, b), ctx.func))
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name == "application":
        ctx.construct(cid, a, b)
        for key in ("name", "sessionmanagement", "clientmanagement", "loginstorage", "setclientcookies"):
            if s(key) is not None:
                ctx.settings[key] = s(key)
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name == "dbinfo" or name == "storedproc":
        if s("datasource"):
            ctx.datasources.append((s("datasource"), (a, b), ctx.func))
    if name in PLATFORM_TAGS:
        ctx.construct(cid, a, b)
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name in UNMAPPED_TAGS:
        ctx.construct(cid, a, b, False)
        return [platform(ctx, cid, at, clean_attrs(attrs), body)]
    if name.startswith("_"):
        return custom_tag(ctx, "cf" + name, attrs, body, a, b)
    ctx.construct("cf:unknown-tag", a, b, False)
    return [platform(ctx, "cf:unknown-tag", at, clean_attrs(attrs), body, tag="cf" + name)]


def platform(ctx: Ctx, cid: str, at: list[int], attrs: dict[str, Expr], body: list[Stmt] | None, **detail: Any) -> Stmt:
    stmt: Stmt = {"op": "platform", "at": at, "construct": cid}
    d = {**detail}
    if attrs:
        d["attributes"] = attrs
    if d:
        stmt["detail"] = d
    if body is not None:
        stmt["body"] = body
    return stmt


def custom_tag(ctx: Ctx, tag: str, attrs: dict[str, Expr], body: list[Stmt] | None, a: int, b: int) -> list[Stmt]:
    ctx.construct("cf:custom-tag", a, b)
    ctx.refs.append(Ref("custom_tag", tag[3:].lower(), (a, b), ctx.func))
    return [platform(ctx, "cf:custom-tag", ctx.at(a, b), clean_attrs(attrs), body, tag=tag.lower())]


def invoke(ctx: Ctx, attrs: dict[str, Expr], body: list[Stmt], a: int, b: int) -> list[Stmt]:
    at = ctx.at(a, b)
    ctx.construct("cf:cfinvoke", a, b)
    comp, method = attrs.get("component"), attrs.get("method")
    comp_name, method_name = lit_str(comp), lit_str(method)
    if attrs.get("webservice") is not None:
        obj: Expr = {"e": "new", "type": "webservice", "kind": "webservice", "args": [attrs["webservice"]]}
    elif comp_name is not None:
        obj = {"e": "name", "name": comp_name, "kind": "component"}
        ctx.refs.append(Ref("component", comp_name, (a, b), ctx.func, method_name))
    elif comp is not None:
        obj = comp
        ctx.note("cfinvoke on a computed component: target not resolved", a)
    else:
        obj = {"e": "name", "name": "this", "kind": "scope"}  # no component: a method of the same component
        if method_name:
            ctx.refs.append(Ref("function", method_name, (a, b), ctx.func))
    named = {k: v for k, v in clean_attrs(attrs, ("component", "method", "returnvariable", "webservice", "argumentcollection")).items()}
    for st in body:
        if st.get("argument"):
            named[st["argument"]] = st["call"]
    call: Expr = {"e": "call", "at": at, "callee": {"e": "member", "object": obj, "name": method_name or "?"}, "args": [], "named": named}
    if method_name is None and method is not None:
        call["method_expr"] = method
    if attrs.get("argumentcollection") is not None:
        call["argumentcollection"] = attrs["argumentcollection"]
    rv = lit_str(attrs.get("returnvariable"))
    if rv:
        scope, short = name_parts(rv)
        return [{"op": "assign", "at": at, "target": var(short, scope) if scope else assign_target(ctx, var(rv)), "value": call}]
    return [{"op": "call", "at": at, "call": call}]


def _url_path(url: str) -> str | None:
    """The path of a URL, without scheme, host, query or fragment: never credentials or parameters."""
    m = re.match(r"^(?:[a-z][a-z0-9+.-]*://[^/?#]*)?([^?#]*)", url.strip(), re.I)
    path = m.group(1) if m else ""
    return path or None


url_path = _url_path
