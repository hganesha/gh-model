"""`cfml.source`: ColdFusion components (`.cfc`) and templates (`.cfm`).

Replaces `legacy.cfml` and produces at least what it did, with the same kinds and
natural keys:

- a component is a `Program` named by its dotted path, a template a `Program` named
  by its path; `remote` functions and templates (other than `Application.cfm` and
  `OnRequestEnd.cfm`) EXPOSE an `Endpoint`;
- `cfquery` and `queryExecute` SQL goes through the shared SQL lineage scanner with
  the masks `legacy.cfml` used, and their literal datasources are `ConfigKey`s;
- `cfinvoke`, `createObject`, `new` and `cfinclude` CALL the `Program`s they name,
  with component names tried against every directory the lookup may use.

On top of that: each function is a `Symbol` its `Program` CONTAINS, with its own
behaviour unit, and each template's body a behaviour unit of its `Program`; a
template that renders output is a `Screen` its `Program` RENDERS, holding the
`Form`s and `UIControl`s of its markup; forms SUBMIT_TO and links NAVIGATE_TO
literal targets, and so does a `cflocation` (from the screen, or from the page or
function that runs it when nothing is rendered); a table named by a `#expression#`
is not declared;
`roles=`, `IsUserInRole` and `cfloginuser` give `Role`s; `Application.cfc` records
its lifecycle events and settings; `cfschedule` gives scheduled `Task`s.
Controls are named as the contract's "UIControl names" says, every link included;
URLs in behaviour keep parameter names but never values (`urls`).

Two deliberate differences from `legacy.cfml`: SQL of a query of queries
(`dbtype="query"`) names in-memory result sets, not tables, so it is not read for
lineage; and code inside comments is not read.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.cfml import markup
from repo_intel.platforms.cfml.ir import LIFECYCLE, Ctx, Function
from repo_intel.platforms.cfml.lower import Lowerer
from repo_intel.platforms.cfml.markup import Document, Node
from repo_intel.platforms.cfml.scan import decode
from repo_intel.platforms.cfml.script import ParseError, Parser
from repo_intel.platforms.cfml.urls import scrub_unit

_SCRIPT_COMPONENT = re.compile(r"^(?:\s|//[^\n]*\n|/\*.*?\*/|<!---.*?--->)*(?:import\b|component\b|interface\b|(?:abstract|final)\s+component\b)", re.I | re.S)
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)
SAFE_SETTINGS = ("name", "datasource", "sessionmanagement", "clientmanagement", "loginstorage", "ormenabled", "setclientcookies", "scriptprotect")


def _lib():
    """Extractor helpers, imported when first used: a pack module must not import the
    `repo_intel.extractors` package while it is being discovered."""
    from repo_intel import platforms
    from repo_intel.compile.behavior import behavior_observation
    from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
    from repo_intel.extractors import base, sql_lineage

    return platforms, behavior_observation, EntityKind, EvidenceClass, Predicate, base, sql_lineage


def program_name(path: str) -> str:
    """A component's dotted name, or a template's path: the legacy `Program` names."""
    if path.lower().endswith(".cfc"):
        return posixpath.splitext(path)[0].replace("/", ".").lower()
    return path


def resolve_include(path: str, target: str) -> str:
    """A template path as `cfinclude` resolves it, the way `legacy.cfml` did."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), target)) if not target.startswith("/") else target.lstrip("/")


def resolve_url(path: str, target: str) -> str | None:
    """The repository path a literal URL in `path` points at; None for other sites, mail and script links."""
    t = target.strip()
    if not t or _EXTERNAL.match(t) or t.startswith("#"):
        return None
    t = re.split(r"[?#]", t, maxsplit=1)[0]
    if not t:
        return path  # "?x=1": the page itself
    resolved = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(path), t))
    if resolved.startswith("../") or resolved == "..":
        return None
    return resolved


def looks_script(text: str) -> bool:
    return bool(_SCRIPT_COMPONENT.match(text))


class _Emit:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, ctx: Ctx):
        (self.platforms, self.behavior_observation, self.K, self.EC, self.P, self.base, self.sql) = _lib()
        self.id, self.path, self.data, self.blob, self.ctx = extractor_id, path, data, blob, ctx
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.ctx.to_byte(a), self.ctx.to_byte(max(a, b)))

    def whole(self):
        return self.base.whole_file(self.data)

    def term(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev=None) -> dict[str, Any]:
        o = self.platforms.platform_entity(self.blob, at, term, key, name, attrs or {}, ev or self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, at, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None):
        self.items.append(self.base.entity(self.blob, at, kind, key, name, attrs or {}, self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, ev=None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, ev or self.EC.E2))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))


class CfmlSource:
    id = "cfml.source"
    version = "1.2.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".cfc", ".cfm", ".cfml"))

    def extract(self, path: str, data: bytes, blob: str):
        *_, base, sql = _lib()
        text = decode(data)
        if text is None:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        ctx = Ctx(path, text, data)
        is_cfc = path.lower().endswith(".cfc")
        doc: Document | None = None
        if is_cfc and looks_script(text):
            parser = Parser(ctx, 0, len(text))
            try:
                body = parser.component()
            except ParseError as exc:  # no `component {` header: read the file as plain script, from scratch
                ctx = Ctx(path, text, data)
                ctx.note(f"not read as a script component: {exc}", exc.pos)
                body = Parser(ctx, 0, len(text)).statements(top=True)
        else:
            doc = markup.parse(text)
            for problem in doc.problems:
                ctx.diagnostics.append(f"markup: {problem}")
            body = Lowerer(ctx).nodes(doc.root.children)
        out = _Emit(self.id, path, data, blob, ctx)
        _entities(out, path, is_cfc, doc, body)
        for message in ctx.diagnostics:
            out.note(message)
        return base.Extraction(out.items + sql.lineage_summary(blob, data, out.resolved, out.unresolved), out.diagnostics)


def _entities(o: _Emit, path: str, is_cfc: bool, doc: Document | None, body: list) -> None:
    K, P, ctx, sql = o.K, o.P, o.ctx, o.sql
    base_name = posixpath.basename(path).lower()
    name = program_name(path)
    me = sql.program("cfml", name)
    attrs: dict[str, Any] = {"kind": "component" if is_cfc else "page", "language": "cfml"}
    comp = ctx.component
    if is_cfc:
        for key in ("extends", "implements", "displayname", "accessors", "persistent", "table", "output"):
            if comp.get(key) is not None:
                attrs[key] = comp[key]
        if comp.get("kind") == "interface":
            attrs["interface"] = True
    if ctx.properties:
        attrs["properties"] = [p["name"] for p in ctx.properties]
    is_app = base_name in ("application.cfc", "application.cfm")
    events = sorted({LIFECYCLE[f.name.lower()] for f in ctx.functions if f.name.lower() in LIFECYCLE}) if base_name == "application.cfc" else []
    if is_app and (events or ctx.settings or ctx.mappings):
        app: dict[str, Any] = {}
        if events:
            app["events"] = events
        settings = {k: ctx.settings[k] for k in SAFE_SETTINGS if k in ctx.settings}
        if settings:
            app["settings"] = settings
        others = sorted(k for k in ctx.settings if k not in SAFE_SETTINGS)
        if others:
            app["configured"] = others  # set, values not copied (paths, timeouts, connection details)
        if ctx.mappings:
            app["mappings"] = sorted(set(ctx.mappings))
        attrs["application"] = app
    prog = o.term(o.whole(), "cf:Component" if is_cfc else "cf:Page", me["natural_key"], name if is_cfc else me["name"], attrs)

    def sym(fn: Function) -> dict[str, Any]:
        return o.base.ref(K.SYMBOL, {"platform": "cfml", "program": name, "name": fn.name.lower()}, fn.name)

    def subjects(owner: Function | None) -> list[dict[str, Any]]:
        return [prog] if owner is None else [prog, sym(owner)]

    # Endpoints, as legacy.cfml declared them.
    route_base = "/" + path

    def endpoint(at, route: str, extra: dict[str, Any], term: str) -> dict[str, Any]:
        key = {"component_root": "@unit", "protocol": "http", "method": "ANY", "route": route}
        ep = o.term(at, term, key, f"ANY {route}", {"framework": "coldfusion", **extra})
        o.rel(at, prog, P.EXPOSES, ep)
        return ep

    endpoints: dict[str, dict[str, Any]] = {}
    if not is_cfc and not base_name.startswith(("application.", "onrequestend.")):
        endpoints[""] = endpoint(o.whole(), route_base, {}, "cf:PageRequest")

    # Functions: a Symbol each, with its behaviour.
    seen_fn: set[str] = set()
    for fn in ctx.functions:
        at = o.span(fn.name_start, fn.name_end)
        fattrs: dict[str, Any] = {"access": fn.access, "program": name, "syntax": "script" if fn.script else "tag", "params": [p["name"] for p in fn.params]}
        if fn.returntype:
            fattrs["returntype"] = fn.returntype
        if fn.output:
            fattrs["output"] = fn.output
        if fn.roles:
            fattrs["roles"] = fn.roles
        event = LIFECYCLE.get(fn.name.lower()) if base_name == "application.cfc" else None
        if event:
            fattrs["lifecycle_event"] = event
            ctx.construct(f"cf:{event}", fn.start, fn.end)
        s = o.term(at, "cf:Function", sym(fn)["natural_key"], fn.name, fattrs)
        if fn.access == "remote" and is_cfc:
            ep = endpoint(at, f"{route_base}?method={fn.name}", {"handler": fn.name}, "cf:RemoteMethod")
            endpoints[fn.name.lower()] = ep
            o.rel(at, s, P.IMPLEMENTS, ep)
            ctx.construct("cf:remote-function", fn.start, fn.end)
        if fn.roles:
            rat = o.span(*fn.roles_at) if fn.roles_at else at
            for role in fn.roles:
                r = _role(o, rat, role, "roles")
                o.rel(rat, s, P.REQUIRES_ROLE, r, {"via": "roles"})
                if fn.name.lower() in endpoints and fn.access == "remote":
                    o.rel(rat, endpoints[fn.name.lower()], P.REQUIRES_ROLE, r, {"via": "roles"})
        if fn.name.lower() in seen_fn:
            o.note(f"function {fn.name} is defined twice")
        seen_fn.add(fn.name.lower())
        o.rel(at, prog, P.CONTAINS, s)
        _behavior(o, o.span(fn.start, fn.end), s, fn.body, fn.params, "function")

    # Queries: lineage and datasources.
    for q in ctx.queries:
        if q.masked is not None and q.region is not None and q.dbtype != "query":
            found = sql.lineage(q.masked, None)
            dynamic_writes = _drop_dynamic_tables(o, q, found)
            o.resolved += len(found.writes) + len(found.reads)
            o.unresolved += dynamic_writes  # data written to a table no name in the source gives
            o.unresolved += found.unresolved
            for subject in subjects(q.owner):
                o.items += sql.lineage_relations(o.blob, o.data, ctx.text, found, subject, offset=q.region[0], to_byte=ctx.to_byte)
        if q.datasource and "#" not in q.datasource:
            at = o.span(*(q.ds_at or q.at))
            _datasource(o, at, q.datasource, subjects(q.owner))
    for ds, (a, b), owner in ctx.datasources:
        _datasource(o, o.span(a, b), ds, subjects(owner))
    if isinstance(ctx.settings.get("datasource"), str) and is_app:
        _datasource(o, o.whole(), ctx.settings["datasource"], [prog])
    for table_name, (a, b), owner, _ in ctx.table_writes:
        at = o.span(a, b)
        t = sql.table(sql.clean_name(table_name))
        o.entity(at, K.TABLE, t["natural_key"], t["name"])
        for subject in subjects(owner):
            o.rel(at, subject, P.WRITES_TO, t)
    if is_cfc and str(comp.get("persistent", "")).lower() in ("true", "yes"):
        table_name = sql.clean_name(comp.get("table") or posixpath.splitext(posixpath.basename(path))[0])
        t = sql.table(table_name)
        o.entity(o.whole(), K.TABLE, t["natural_key"], t["name"], {"orm": "cfml"})
        ctx.construct("cf:persistent-component", 0, len(ctx.text))

    # References to other files and to this file's functions.
    dirs = posixpath.dirname(path).split("/") if posixpath.dirname(path) else []
    local = {f.name.lower(): f for f in ctx.functions}
    for r in ctx.refs:
        at = o.span(*r.at)
        if r.kind == "component" and r.target and "#" not in r.target and re.fullmatch(r"[\w.$-]+", r.target):
            for depth in range(len(dirs), -1, -1):
                candidate = ".".join(dirs[:depth] + [r.target]).lower()
                target = sql.program("cfml", candidate, declare=False)
                for subject in subjects(r.owner):
                    o.rel(at, subject, P.CALLS, target)
                if r.method:
                    method = o.base.ref(K.SYMBOL, {"platform": "cfml", "program": candidate, "name": r.method.lower()}, r.method, declare=False)
                    o.rel(at, subjects(r.owner)[-1], P.CALLS, method)
        elif r.kind in ("include", "module") and r.target:
            target = sql.program("cfml", resolve_include(path, r.target), declare=False)
            for subject in subjects(r.owner):
                o.rel(at, subject, P.CALLS, target)
        elif r.kind == "custom_tag" and r.target:
            # A custom tag is looked up first beside the calling template.
            target = sql.program("cfml", posixpath.join(posixpath.dirname(path), r.target + ".cfm"), declare=False)
            for subject in subjects(r.owner):
                o.rel(at, subject, P.CALLS, target)
        elif r.kind == "function" and r.target and r.target.lower() in local:
            callee = local[r.target.lower()]
            o.rel(at, subjects(r.owner)[-1], P.CALLS, sym(callee))
        elif r.kind == "module_name" and r.target:
            # `cfmodule name="a.b"` is looked up under the server's custom tag paths, which no file here gives.
            ctx.note(f"cfmodule name={r.target}: resolved against the server's custom tag paths, not followed", r.at[0])

    # Screens, forms and controls.
    screen = None
    renders = not is_cfc and not base_name.startswith(("application.", "onrequestend.")) and doc is not None
    has_forms = doc is not None and any(e.kind == "form_open" for e in doc.ui)
    if renders and (ctx.renders or has_forms):
        title = None
        if doc.title:
            raw = ctx.text[doc.title[0]: doc.title[1]].strip()
            title = raw if raw and "#" not in raw and "<" not in raw else None
        sattrs: dict[str, Any] = {"document": doc.has_document}
        if title:
            sattrs["title"] = title
        screen = o.term(o.whole(), "cf:Template", {"platform": "cfml", "name": path}, path, sattrs)
        o.rel(o.whole(), prog, P.RENDERS, screen)
        _ui(o, path, doc, screen)
    elif doc is not None:
        for e in doc.ui:
            if e.node is not None:
                ctx.construct(_ui_construct(e.node), e.node.start, e.node.open_end, False)

    # Redirects are navigation: from the screen when the page renders one, else from
    # the code that runs them (the page or component Program, or the function's Symbol).
    for target, (a, b), owner in ctx.redirects:
        if target is None:
            continue  # a computed URL: noted where it was lowered
        obj = _target(o, path, target, target)
        if obj is None:
            continue
        subject = sym(owner) if owner is not None else screen if screen is not None else prog
        o.rel(o.span(a, b), subject, P.NAVIGATES_TO, obj, {"via": "cflocation"})

    # Roles checked in code, and roles granted at login.
    for role, (a, b), owner, via in ctx.role_checks:
        at = o.span(a, b)
        r = _role(o, at, role, via)
        if owner is None:
            o.rel(at, prog, P.REQUIRES_ROLE, r, {"via": via})
            if screen is not None:
                o.rel(at, screen, P.REQUIRES_ROLE, r, {"via": via})
        else:
            o.rel(at, sym(owner), P.REQUIRES_ROLE, r, {"via": via})
    for role, (a, b) in ctx.roles_granted:
        _role(o, o.span(a, b), role, "cfloginuser")

    # Scheduled tasks declared in code.
    for item in ctx.schedules:
        if item["task"] and item["action"] in ("", "update", "add", "create"):
            _scheduled_task(o, o.span(*item["at"]), item["task"], item["group"], item["path"], {"interval": item["interval"]} if item["interval"] else {}, "cfschedule")

    # Behaviour of the page (or of the component's pseudo-constructor, code outside its functions).
    if not is_cfc or body:
        _behavior(o, o.whole(), prog, body, [], "open_code" if is_cfc else "page")

    for cid, a, b, modelled in ctx.constructs:
        o.items.append(o.platforms.construct(o.blob, o.span(a, b), cid, modelled))


def _drop_dynamic_tables(o: _Emit, q, found) -> int:
    """Take out of `found` every table whose name holds a `#expression#` of the SQL.

    `FROM orders_#url.region#` or `FROM #tbl#` names a table only at run time: the
    masked text (`orders_x___________`) is not a table. The query stays in the
    behaviour with its inline parameters; a diagnostic says the table is not
    resolved. Returns how many writes were dropped."""
    if not q.inline:
        return 0

    def dynamic(name: str, a: int, b: int) -> bool:
        # The name overlaps a masked expression and carries its mask (the span of a
        # name lineage could not locate is the whole statement, so both are checked).
        return any(s < b and a < e and q.masked[s:e].lower() in name for s, e in q.inline)

    dropped = 0
    for items in (found.writes, found.reads):
        keep = []
        for name, a, b in items:
            if dynamic(name, a, b):
                dropped += items is found.writes
                o.ctx.note("query names a table computed at run time: table not resolved", q.region[0] + a)
            else:
                keep.append((name, a, b))
        items[:] = keep
    return dropped


def _behavior(o: _Emit, at, owner: dict[str, Any], body: list, params: list, kind: str) -> None:
    scrub_unit(body, params)  # URLs keep parameter names, never values (contract rule 7)
    try:
        o.items.append(o.behavior_observation(o.blob, at, owner, "cfml", body, params, kind=kind))
    except ValueError as exc:  # a lowering bug must not lose the file's entities
        o.note(f"behaviour of {owner['name']} not emitted: {exc}", "warning")


def _role(o: _Emit, at, role: str, via: str) -> dict[str, Any]:
    return o.term(at, "cf:Role", {"realm": "cfml", "name": role.lower()}, role, {"source": via})


def _datasource(o: _Emit, at, ds: str, subjects: list[dict[str, Any]]) -> None:
    key = {"name": f"datasource:{ds}"}
    c = o.term(at, "cf:Datasource", key, key["name"], {"source": "cfml_datasource"})
    for subject in subjects:
        o.rel(at, subject, o.P.REFERENCES_CONFIG, c)


def _scheduled_task(o: _Emit, at, task: str, group: str, path: str | None, attrs: dict[str, Any], source: str, ev=None) -> None:
    P = o.P
    job = o.term(at, "cf:ScheduleGroup", {"framework": "cfschedule", "name": group}, group, {"source": source}, ev)
    tattrs = {"source": source, **attrs}
    if path:
        tattrs["url_path"] = path
    t = o.term(at, "cf:ScheduledTask", {"framework": "cfschedule", "job": group, "name": task}, task, tattrs, ev)
    o.rel(at, job, P.CONTAINS, t, ev=ev)
    if path:
        target = path.lstrip("/")
        if target.lower().endswith((".cfm", ".cfml")):
            o.rel(at, t, P.RUNS, o.sql.program("cfml", target, declare=False), ev=ev)
        elif target.lower().endswith(".cfc"):
            o.rel(at, t, P.RUNS, o.sql.program("cfml", program_name(target), declare=False), ev=ev)


def _target(o: _Emit, path: str, literal: str, full: str | None) -> dict[str, Any] | None:
    """The Program or Endpoint a literal URL reaches, declared only if the unit has it."""
    resolved = resolve_url(path, literal)
    if resolved is None:
        return None
    low = resolved.lower()
    if low.endswith((".cfm", ".cfml")):
        return o.sql.program("cfml", resolved, declare=False)
    if low.endswith(".cfc"):
        m = re.search(r"[?&]method=([A-Za-z_]\w*)", full or "")
        if m:
            key = {"component_root": "@unit", "protocol": "http", "method": "ANY", "route": f"/{resolved}?method={m.group(1)}"}
            return o.base.ref(o.K.ENDPOINT, key, f"ANY {key['route']}", declare=False)
        return o.sql.program("cfml", program_name(resolved), declare=False)
    return None


_CONTROL_TAGS = {"input": "cf:html-input", "select": "cf:html-select", "textarea": "cf:html-textarea", "button": "cf:html-button", "a": "cf:html-link",
                 "cfinput": "cf:cfinput", "cfselect": "cf:cfselect", "cftextarea": "cf:cftextarea", "form": "cf:html-form", "cfform": "cf:cfform"}


def _ui_construct(node: Node) -> str:
    return _CONTROL_TAGS[node.name]


def _plain(node: Node, attr: str) -> str | None:
    value = node.literal(attr)
    return value.strip() if value and value.strip() else None


_BUTTON_INPUTS = ("submit", "button", "reset", "image")
_END_BUTTON = re.compile(r"</button\s*>", re.I)


_HOST = re.compile(r"^([a-z][a-z0-9+.-]*:)?//(?:[^/?#@]*@)?([^/?#]*)", re.I)
_OPAQUE_LINK = re.compile(r"^([a-z][a-z0-9+.-]*:)(?!//)", re.I)


def _button_text(text: str, node: Node) -> str | None:
    """The literal label of a button: an input's `value`, or all the text inside a
    `<button>`, tags removed and whitespace collapsed. None when any of it is computed:
    a `#expression#` evaluated in a `cfoutput`, or a CFML tag."""
    if node.name != "button":
        return _plain(node, "value")
    close = _END_BUTTON.search(text, node.open_end)
    if close is None:
        return None
    a, b = node.open_end, close.start()
    if re.search(r"</?cf", text[a:b], re.I):
        return None
    if node.in_output:
        from repo_intel.platforms.cfml.scan import hash_parts

        parts = hash_parts(text, a, b)
        if any(p[0] == "expr" for p in parts):
            return None
        raw = "".join(p[3] or "" for p in parts)  # `##` is a literal `#`
    else:
        raw = text[a:b]
    label = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", raw)).strip()
    return label or None


def link_name(target: str | None) -> str | None:
    """The `<target>` of a link's `link:<target>` name: the literal target, trimmed,
    without its query string or fragment; a link to another host keeps only
    `<scheme>://<host>` (never user information), and a link whose scheme has no host
    (`mailto:`, `javascript:`) only its scheme. None when nothing is left."""
    t = (target or "").strip()
    m = _HOST.match(t)
    if m:
        host = m.group(2).strip()
        return f"{m.group(1) or ''}//{host}" if host else None
    m = _OPAQUE_LINK.match(t)
    if m and not re.match(r"^[a-z]:[\\/]", t, re.I):
        return m.group(1).lower()
    path = re.split(r"[?#]", t, maxsplit=1)[0].strip()
    return path or None


def _control_name(text: str, node: Node, ctype: str, link: str | None, counts: dict[tuple[str, str], int], container: str) -> str:
    """A control's base name as the contract orders it: its name, its id, `link:<target>`
    for a link with a literal target, `submit` or `button:<text>` for a button, else
    `<type><n>` with `n` counted in document order among the controls of its container
    that fall back to `<type>`. CFML has no binding attribute apart from the field
    name, so no binding path is looked for. Values are trimmed; an empty one does not
    count. Repeats in one container are told apart by the caller."""
    name = _plain(node, "name") or _plain(node, "id")
    if name:
        return name
    if ctype == "link":
        target = link_name(link)
        if target:
            return "link:" + target
    if node.name == "button" or ctype in _BUTTON_INPUTS:
        kind = (_plain(node, "type") or "").lower() if node.name == "button" else ctype
        if kind == "submit":
            return "submit"
        label = _button_text(text, node)
        if label and len(label) <= 40:
            return "button:" + label
    counts[(container, ctype)] = counts.get((container, ctype), 0) + 1
    return f"{ctype}{counts[(container, ctype)]}"


def _unique(name: str, container: str, seen: dict[tuple[str, str], int]) -> str:
    """The second and later controls of one name in one container get `~2`, `~3`, ..."""
    n = seen[(container, name)] = seen.get((container, name), 0) + 1
    return name if n == 1 else f"{name}~{n}"


def _ui(o: _Emit, path: str, doc: Document, screen: dict[str, Any]) -> None:
    P, ctx = o.P, o.ctx
    screen_name = path
    form = None
    form_count = 0
    counts: dict[tuple[str, str], int] = {}  # (container, type) -> controls named <type><n> so far
    seen: dict[tuple[str, str], int] = {}  # (container, name) -> controls given that name so far
    for e in doc.ui:
        node = e.node
        if e.kind == "form_close":
            form = None
            continue
        at = o.span(node.start, node.open_end)
        if e.kind == "form_open":
            form_count += 1
            dynamic_name = node.attr("name") is not None and _plain(node, "name") is None
            fname = _plain(node, "name") or _plain(node, "id") or f"form{form_count}"
            is_cf = node.name == "cfform"
            fattrs: dict[str, Any] = {"kind": "cfform" if is_cf else "html", "method": (_plain(node, "method") or ("post" if is_cf else "get")).lower()}
            if dynamic_name:
                fattrs["name_dynamic"] = True
            key = {"screen": screen_name, "name": fname}
            form_ref = o.term(at, "cf:Form", key, fname, fattrs)
            o.rel(at, screen, P.CONTAINS, form_ref)
            action = node.attr("action")
            if action is None or (action.literal() is not None and not action.literal().strip()):
                prog = o.sql.program("cfml", program_name(path) if path.lower().endswith(".cfc") else path, declare=False)
                o.rel(at, form_ref, P.SUBMITS_TO, prog, {"implicit": True})
            else:
                literal = action.literal()
                head = literal
                if head is None and action.parts and action.parts[0][0] == "lit" and "?" in (action.parts[0][3] or ""):
                    head = action.parts[0][3].split("?", 1)[0]
                target = _target(o, path, head, literal) if head is not None else None
                if target is not None:
                    o.rel(at, form_ref, P.SUBMITS_TO, target)
                elif head is None:
                    ctx.note(f"form {fname} posts to a computed action: target not resolved", node.start)
            ctx.construct(_ui_construct(node), node.start, node.open_end)
            form = (form_ref, f"{screen_name}#{fname}")
            continue
        tag = node.name
        head = None
        if tag == "a":
            href = node.attr("href")
            if href is None:
                continue  # an anchor, not a link
            head = href.literal()
            if head is None and href.parts and href.parts[0][0] == "lit" and re.search(r"[?#]", href.parts[0][3] or ""):
                head = re.split(r"[?#]", href.parts[0][3], maxsplit=1)[0]  # literal up to a computed query
            target = None
            if head is None:
                ctx.note("link to a computed URL: target not resolved", node.start)
            else:
                target = _target(o, path, head, href.literal())
            ctype = "link"
        elif tag in ("input", "cfinput"):
            ctype = (_plain(node, "type") or "text").lower()
            target = None
        elif tag in ("select", "cfselect"):
            ctype, target = "select", None
        elif tag in ("textarea", "cftextarea"):
            ctype, target = "textarea", None
        else:  # <button>
            ctype, target = "button", None
        container = form[1] if form else screen_name
        cname = _unique(_control_name(ctx.text, node, ctype, head, counts, container), container, seen)
        cattrs: dict[str, Any] = {"control": ctype, "tag": tag}
        if node.attr("name") is not None and _plain(node, "name") is None:
            cattrs["name_dynamic"] = True
        req = node.attr("required")
        if req is not None and (req.literal() is None or req.literal().lower() in ("", "yes", "true", "required")):
            cattrs["required"] = True
        for key in ("validate", "pattern", "maxlength"):
            if _plain(node, key):
                cattrs[key] = _plain(node, key)
        if tag == "cfselect" and _plain(node, "query"):
            cattrs["query"] = _plain(node, "query")
        control = o.term(at, "cf:Input", {"container": container, "name": cname}, cname, cattrs)
        o.rel(at, form[0] if form else screen, P.CONTAINS, control)
        if target is not None:
            o.rel(at, control, P.NAVIGATES_TO, target, {"via": "link"})
        ctx.construct(_ui_construct(node), node.start, node.open_end)
