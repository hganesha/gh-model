"""JavaScript and TypeScript, read with tree-sitter: JSX elements, imports, functions,
route configuration, theme objects, HTTP calls and navigation calls.

One parse and one walk per script. Nothing is run or imported. Offsets are byte
offsets into the file: a script that is part of a larger file (a Vue `<script>`
block) is parsed on its own and shifted by `base`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Iterator

from repo_intel.platforms.web.markup import TEXT_CAP, Attr, Element

SCRIPT_EXTS = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs")
MODULE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".vue")
STYLE_EXTS = (".css", ".scss", ".sass", ".less")
_WRAPPERS = frozenset({"as_expression", "satisfies_expression", "parenthesized_expression", "non_null_expression", "type_assertion"})
_HTTP_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "del": "DELETE"}
_HTTP_CLIENTS = frozenset({"axios", "http", "api", "client", "httpClient", "$http", "ky", "request", "$axios", "$api"})
_ROUTER_ROOT_CALLS = frozenset({"createBrowserRouter", "createHashRouter", "createMemoryRouter", "createStaticRouter", "createRouter", "provideRouter", "forRoot", "VueRouter"})
_ROUTE_CALLS = _ROUTER_ROOT_CALLS | {"useRoutes", "forChild", "createRoutesFromElements", "RouterModule"}
_ROUTER_ELEMENTS = frozenset({"BrowserRouter", "HashRouter", "MemoryRouter", "StaticRouter", "Router", "RouterProvider"})
_THEME_NAME = re.compile(r"^(theme|tokens?|designTokens|palette|colors|typography|spacing|[a-z]*Theme|[a-z]*Tokens)$")
_THEME_FILE = re.compile(r"(^|[._-])(theme|tokens?)$", re.I)
_THEME_CALLS = frozenset({"createTheme", "extendTheme", "createMuiTheme", "defineTheme", "makeTheme", "createTokens", "defineTokens"})
_NAV_CALLEES = frozenset({"navigate", "navigateTo", "navigateByUrl", "push", "replace", "redirect"})
_NAV_OBJECTS = frozenset({"router", "$router", "this.router", "this.$router", "history", "Router", "useRouter()"})
_GUARD_TAG = re.compile(r"(Require|Protected|Private|Guard|Authori[sz]e|Auth|Role|Permission|Secure)")
_SKIP_WRAPPERS = frozenset({"Suspense", "React.Suspense", "Fragment", "React.Fragment", "ErrorBoundary", "StrictMode", "React.StrictMode"})
ROLE_KEYS = frozenset({"role", "roles", "allowedRoles", "requiredRole", "requiredRoles", "requireRole", "requiresRole", "hasRole", "userRole", "userRoles"})
AUTH_KEYS = frozenset({"requiresAuth", "requireAuth", "authRequired", "auth", "authenticated", "protected", "private", "isProtected"})
GUARD_KEYS = frozenset({"canActivate", "canActivateChild", "canMatch", "canLoad", "beforeEnter", "middleware"})
_SIMPLE_CALL = re.compile(r"^\s*(?:this\.)?([A-Za-z_$][\w$]*)\s*(?:\(.*\))?\s*;?\s*$", re.S)


@cache
def _parser(grammar: str):
    from tree_sitter import Language, Parser

    if grammar == "javascript":
        import tree_sitter_javascript

        return Parser(Language(tree_sitter_javascript.language()))
    import tree_sitter_typescript

    return Parser(Language(tree_sitter_typescript.language_tsx() if grammar == "tsx" else tree_sitter_typescript.language_typescript()))


def grammar_for(path_or_lang: str) -> str:
    p = path_or_lang.lower()
    if p.endswith((".tsx", ".jsx")) or p in ("tsx", "jsx"):
        return "tsx"
    if p.endswith(".ts") or p == "ts":
        return "typescript"
    return "javascript"


def text(node) -> str:
    return node.text.decode("utf-8", "replace")


def unwrap(node):
    while node is not None and node.type in _WRAPPERS:
        node = node.named_children[0] if node.named_children else None
    return node


def string_value(node) -> str | None:
    """The value of a string literal, or a template literal with no substitutions."""
    node = unwrap(node)
    if node is None:
        return None
    if node.type == "string":
        return text(node)[1:-1]
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
        return text(node)[1:-1]
    return None


def template_path(node) -> str | None:
    """A template literal with substitutions as text, each substitution written `{}`."""
    node = unwrap(node)
    if node is None or node.type != "template_string":
        return None
    out = []
    for c in node.children:
        if c.type == "template_substitution":
            out.append("{}")
        elif c.type not in ("`",):
            out.append(text(c))
    return "".join(out)


def string_list(node) -> list[str] | None:
    """A string literal or an array of string literals, else None."""
    node = unwrap(node)
    if node is None:
        return None
    s = string_value(node)
    if s is not None:
        return [s]
    if node.type == "array":
        out = [string_value(c) for c in node.named_children]
        return out if out and all(v is not None for v in out) else None
    return None


def tail_name(node) -> str | None:
    node = unwrap(node)
    if node is None:
        return None
    if node.type in ("identifier", "property_identifier", "type_identifier"):
        return text(node)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return text(prop) if prop is not None else None
    return None


def pairs(obj) -> dict[str, Any]:
    """An object literal's literal keys and their value nodes (shorthand `{ path }` maps to the identifier)."""
    out: dict[str, Any] = {}
    obj = unwrap(obj)
    if obj is None or obj.type != "object":
        return out
    for p in obj.named_children:
        if p.type == "pair":
            k = p.child_by_field_name("key")
            if k is None:
                continue
            key = string_value(k) if k.type == "string" else text(k) if k.type in ("property_identifier", "number") else None
            if key is not None:
                out[key] = p.child_by_field_name("value")
        elif p.type == "shorthand_property_identifier":
            out[text(p)] = p
        elif p.type == "method_definition":
            name = p.child_by_field_name("name")
            if name is not None:
                out[text(name)] = p
    return out


def dynamic_import(node) -> str | None:
    """The literal specifier of the first `import('x')` inside `node`."""
    for n in _walk(node):
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "import":
                args = n.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                return string_value(first) if first is not None else None
    return None


def _walk(root) -> Iterator[Any]:
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def module_candidates(importer: str, spec: str, exts: tuple[str, ...] = MODULE_EXTS) -> list[str]:
    """Repository paths a relative specifier can name. A bare or aliased specifier
    (`react`, `@/views/x`) names nothing this file can resolve."""
    if not spec.startswith("."):
        return []
    path = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec.split("?")[0]))
    if path.startswith(".."):
        return []
    ext = posixpath.splitext(path)[1].lower()
    if ext in MODULE_EXTS or ext in STYLE_EXTS or ext in (".html", ".htm", ".mjs", ".cjs"):
        return [path]
    return [path + e for e in exts] + [path + "/index" + e for e in exts if e != ".vue"]


def framework_exts(framework: str) -> tuple[str, ...]:
    """The module extensions a component import resolves to, by router framework."""
    return {"angular-router": (".ts",), "vue-router": (".vue", ".ts", ".js")}.get(framework, (".tsx", ".jsx", ".ts", ".js"))


def style_path(importer: str, spec: str) -> str | None:
    """The repository path a stylesheet specifier names, without guessing an extension
    or a partial's leading `_`; None for a package, a URL or a root-relative path."""
    if spec.startswith(("http:", "https:", "//", "~", "@", "/")) or "{" in spec or ":" in spec:
        return None
    joined = spec if spec.startswith(".") else "./" + spec
    path = posixpath.normpath(posixpath.join(posixpath.dirname(importer), joined.split("?")[0]))
    return None if path.startswith("..") else path


def style_candidates(importer: str, spec: str, dialect: str | None = None) -> list[str]:
    """Stylesheet paths an `@use`, `@import`, `<link>` or `import` specifier can name.
    Sass resolves `variables` to `variables.scss`, `_variables.scss` or an index file."""
    path = style_path(importer, spec)
    if path is None:
        return []
    head, base = posixpath.split(path)
    stem, ext = posixpath.splitext(base)
    if ext.lower() in STYLE_EXTS:
        out = [path]
        if ext.lower() in (".scss", ".sass") and not stem.startswith("_"):
            out.append(posixpath.join(head, "_" + base))
        return out
    exts = {"scss": (".scss", ".sass", ".css"), "less": (".less", ".css")}.get(dialect or "", (".css",))
    out = []
    for e in exts:
        out.append(path + e)
        if dialect == "scss":
            out.append(posixpath.join(head, "_" + base + e))
    if dialect == "scss":
        out += [path + "/_index.scss", path + "/index.scss"]
    return out


@dataclass
class HttpCall:
    method: str
    path: str | None  # static path; None when the URL is not a literal
    url: str | None  # what was written, with substitutions as {} (None when not a literal)
    start: int
    end: int
    external: bool = False


@dataclass
class RouteScreen:
    name: str
    start: int
    end: int
    construct: str
    framework: str
    attrs: dict[str, Any] = field(default_factory=dict)
    roles: list[tuple[str, int, int]] = field(default_factory=list)
    component: str | None = None
    module: str | None = None  # import specifier of the component, if the file shows it
    router: str | None = None  # the label of the router this file shows the route under (section 8.5)
    root: Any = None  # the router call or element the route is declared under, when the file shows it


@dataclass
class Inherited:
    path: str = "/"
    layouts: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()
    roles: tuple[tuple[str, int, int], ...] = ()
    requires_auth: bool = False


class Script:
    def __init__(self, data: bytes, grammar: str, base: int = 0):
        self.data = data
        self.base = base
        self.tree = _parser(grammar).parse(data)
        self.root = self.tree.root_node
        self.nodes = list(_walk(self.root))
        self.imports: dict[str, tuple[str, str]] = {}  # local name -> (specifier, imported name or "default" or "*")
        self.import_nodes: list[tuple[str, Any]] = []
        self.functions: dict[str, Any] = {}  # name -> function node
        self.top: dict[str, Any] = {}  # top-level const name -> value node
        self.exports: dict[str, Any] = {}  # exported name -> value node ("default" for the default export)
        self.calls = [n for n in self.nodes if n.type == "call_expression"]
        self._read_declarations()
        self.elements: list[Element] = []
        self._element_of: dict[int, Element] = {}
        self._read_jsx()
        self.diagnostics: list[str] = []
        self.constructs: list[tuple[str, int, int, bool]] = []

    # -- positions

    def at(self, node) -> tuple[int, int]:
        return self.base + node.start_byte, self.base + node.end_byte

    # -- declarations

    def _read_declarations(self) -> None:
        for n in self.nodes:
            t = n.type
            if t == "import_statement":
                src = n.child_by_field_name("source")
                spec = string_value(src) if src is not None else None
                if spec is None:
                    continue
                self.import_nodes.append((spec, n))
                clause = next((c for c in n.children if c.type == "import_clause"), None)
                for c in _walk(clause) if clause is not None else ():
                    if c.type == "identifier" and c.parent is not None and c.parent.type == "import_clause":
                        self.imports[text(c)] = (spec, "default")
                    elif c.type == "namespace_import":
                        ident = next((x for x in c.named_children if x.type == "identifier"), None)
                        if ident is not None:
                            self.imports[text(ident)] = (spec, "*")
                    elif c.type == "import_specifier":
                        name = c.child_by_field_name("name")
                        alias = c.child_by_field_name("alias")
                        if name is not None:
                            self.imports[text(alias or name)] = (spec, text(name))
            elif t == "function_declaration" or t == "generator_function_declaration":
                name = n.child_by_field_name("name")
                if name is not None:
                    self.functions.setdefault(text(name), n)
            elif t == "variable_declarator":
                name, value = n.child_by_field_name("name"), unwrap(n.child_by_field_name("value"))
                if name is None or name.type != "identifier" or value is None:
                    continue
                if value.type == "call_expression" and tail_name(value.child_by_field_name("function")) in ("useCallback", "useMemo") and value.child_by_field_name("arguments") is not None:
                    args = value.child_by_field_name("arguments").named_children
                    value = args[0] if args else value
                if value.type in ("arrow_function", "function_expression", "function"):
                    self.functions.setdefault(text(name), value)
                if n.parent is not None and n.parent.parent is not None and n.parent.parent.type in ("program", "export_statement"):
                    self.top.setdefault(text(name), unwrap(n.child_by_field_name("value")))
                    if n.parent.parent.type == "export_statement":
                        self.exports.setdefault(text(name), unwrap(n.child_by_field_name("value")))
            elif t == "method_definition":
                name = n.child_by_field_name("name")
                if name is not None:
                    self.functions.setdefault(text(name), n)
            elif t == "pair":
                key, value = n.child_by_field_name("key"), unwrap(n.child_by_field_name("value"))
                if key is not None and value is not None and value.type in ("arrow_function", "function_expression", "function"):
                    self.functions.setdefault(text(key), value)
            elif t == "export_statement":
                value = n.child_by_field_name("value")
                if value is not None and any(c.type == "default" for c in n.children):
                    self.exports["default"] = unwrap(value)
                decl = n.child_by_field_name("declaration")
                if decl is not None and decl.type in ("function_declaration", "class_declaration") and any(c.type == "default" for c in n.children):
                    self.exports["default"] = decl
                if n.child_by_field_name("source") is None:  # export { X as default }: the default is the local X
                    for clause in (c for c in n.children if c.type == "export_clause"):
                        for spec in (s for s in clause.named_children if s.type == "export_specifier"):
                            alias, local = spec.child_by_field_name("alias"), spec.child_by_field_name("name")
                            if alias is not None and text(alias) == "default" and local is not None and local.type == "identifier":
                                self.exports["default"] = local
            elif t == "assignment_expression":  # module.exports = {...}
                left = n.child_by_field_name("left")
                if left is not None and text(left) == "module.exports":
                    self.exports["default"] = unwrap(n.child_by_field_name("right"))

    def imports_from(self, *packages: str) -> bool:
        return any(spec == p or spec.startswith(p + "/") for spec, _ in self.import_nodes for p in packages)

    def default_export_name(self) -> str | None:
        node = self.exports.get("default")
        if node is None:
            return None
        if node.type in ("function_declaration", "class_declaration"):
            name = node.child_by_field_name("name")
            return text(name) if name is not None else None
        if node.type == "identifier":
            return text(node)
        return None

    def stylesheet_imports(self) -> list[tuple[str, bool, int, int]]:
        """(specifier, is a CSS module, start, end) for each stylesheet the script imports."""
        out = []
        for spec, n in self.import_nodes:
            low = spec.lower().split("?")[0]
            if low.endswith(STYLE_EXTS):
                out.append((spec, ".module." in low, *self.at(n)))
        return out

    # -- JSX

    def _read_jsx(self) -> None:
        for n in self.nodes:
            if n.type not in ("jsx_element", "jsx_self_closing_element"):
                continue
            opening = n.child_by_field_name("open_tag") if n.type == "jsx_element" else n
            if opening is None:
                continue
            name = opening.child_by_field_name("name")
            tag = text(name) if name is not None else ""
            el = Element(tag=tag, attrs={}, start=self.base + n.start_byte, end=self.base + opening.end_byte, close=self.base + n.end_byte)
            for a in opening.children:
                if a.type == "jsx_attribute":
                    attr = self._jsx_attr(a)
                    if attr is not None:
                        el.attrs.setdefault(attr.key, attr)
                elif a.type == "jsx_expression":
                    inner = a.named_children[0] if a.named_children else None
                    if inner is not None and inner.type == "spread_element":
                        el.spreads.append(Attr(key="...", raw=text(inner), value=text(inner), literal=False, start=self.base + a.start_byte, end=self.base + a.end_byte, node=inner))
            if n.type == "jsx_element":
                el.text = " ".join(" ".join(text(c).split()) for c in n.children if c.type == "jsx_text").strip()[:120]
            p = n.parent
            while p is not None and p.type not in ("jsx_element", "jsx_self_closing_element"):
                p = p.parent
            el.parent = self._element_of.get(p.id) if p is not None else None
            if el.parent is not None:
                el.parent.children.append(el)
            el.node = n  # type: ignore[attr-defined]
            self._element_of[n.id] = el
            self.elements.append(el)
        self._read_jsx_deep_text()

    def _read_jsx_deep_text(self) -> None:
        """Each element's descendant JSX text in document order. Text inside an
        expression (`{ok && <b>Done</b>}`) is computed, so it stops at the expression."""
        for n in self.nodes:
            if n.type != "jsx_text":
                continue
            chunk = " ".join(text(n).split())
            if not chunk:
                continue
            p = n.parent
            while p is not None and p.type == "jsx_element":
                el = self._element_of.get(p.id)
                if el is not None:
                    if len(el.deep_text) >= TEXT_CAP:
                        break  # an outer element holds all of this one's text: it is full too
                    el.deep_text = (el.deep_text + " " + chunk).strip()[:TEXT_CAP]
                p = p.parent

    def _jsx_attr(self, a) -> Attr | None:
        name = a.children[0] if a.children else None
        if name is None:
            return None
        raw = text(name)
        key = "on:" + raw[2].lower() + raw[3:] if re.match(r"^on[A-Z]", raw) else raw
        value_node = a.children[2] if len(a.children) >= 3 else None
        start, end = self.base + a.start_byte, self.base + a.end_byte
        if value_node is None:
            return Attr(key=key, raw=raw, value=None, literal=True, start=start, end=end)
        if value_node.type == "string":
            return Attr(key=key, raw=raw, value=text(value_node)[1:-1], literal=True, start=start, end=end)
        if value_node.type == "jsx_expression":
            inner = value_node.named_children[0] if value_node.named_children else None
            s = string_value(inner) if inner is not None else None
            if s is not None:
                return Attr(key=key, raw=raw, value=s, literal=True, start=start, end=end, node=inner)
            return Attr(key=key, raw=raw, value=text(inner) if inner is not None else "", literal=False, start=start, end=end, node=inner)
        return Attr(key=key, raw=raw, value=text(value_node), literal=False, start=start, end=end, node=value_node)

    def element_of(self, node) -> Element | None:
        return self._element_of.get(node.id)

    # -- functions and HTTP calls

    def function_body(self, name: str):
        fn = self.functions.get(name)
        if fn is None:
            return None
        return fn.child_by_field_name("body") or fn

    def handler(self, node) -> tuple[str | None, Any | None, str | None]:
        """(handler name, the node to search for calls, form library) for an event-handler expression node."""
        node = unwrap(node)
        if node is None:
            return None, None, None
        if node.type == "identifier":
            name = text(node)
            return name, self.function_body(name), None
        if node.type == "member_expression":
            name = tail_name(node)
            return name, self.function_body(name) if name else None, None
        if node.type in ("arrow_function", "function_expression", "function"):
            return None, node, None
        if node.type == "call_expression":
            callee = tail_name(node.child_by_field_name("function"))
            library = "react-hook-form" if callee == "handleSubmit" else None
            args = node.child_by_field_name("arguments")
            for arg in (args.named_children if args is not None else []):
                name, body, _ = self.handler(arg)
                if body is not None:
                    return name, body, library
            if callee and callee in self.functions:
                return callee, self.function_body(callee), library
            return callee, None, library
        return None, None, None

    def handler_by_text(self, expr: str) -> tuple[str | None, Any | None]:
        """For a template's handler text (`save`, `save($event)`, `this.save()`): its name and body."""
        m = _SIMPLE_CALL.match(expr or "")
        if not m:
            return None, None
        return m.group(1), self.function_body(m.group(1))

    def http_calls(self, node, depth: int = 0, seen: set[int] | None = None) -> list[HttpCall]:
        """HTTP calls made in `node` (fetch, axios and common client objects), following
        calls to functions this file defines one level deep."""
        if node is None:
            return []
        seen = seen if seen is not None else set()
        if node.id in seen:
            return []
        seen.add(node.id)
        out: list[HttpCall] = []
        local: list[str] = []
        for n in _walk(node):
            if n.type != "call_expression":
                continue
            fn = unwrap(n.child_by_field_name("function"))
            args_node = n.child_by_field_name("arguments")
            args = args_node.named_children if args_node is not None else []
            if fn is None:
                continue
            call = None
            if fn.type == "identifier" and text(fn) in ("fetch", "$fetch", "ofetch"):
                method = "GET"
                if len(args) > 1:
                    m = pairs(args[1]).get("method")
                    method = (string_value(m) or "DYNAMIC").upper() if m is not None else "GET"
                call = (method, args[0] if args else None)
            elif fn.type == "member_expression":
                obj, prop = fn.child_by_field_name("object"), tail_name(fn)
                obj_text = text(obj) if obj is not None else ""
                root = obj_text.removeprefix("this.")
                if prop in _HTTP_METHODS and (root in _HTTP_CLIENTS or obj_text in ("window", "globalThis")):
                    call = (_HTTP_METHODS[prop], args[0] if args else None)
            elif fn.type == "identifier" and text(fn) in ("axios", "request") and args and unwrap(args[0]).type == "object":
                p = pairs(args[0])
                method = (string_value(p["method"]) or "DYNAMIC").upper() if "method" in p else "GET"
                call = (method, p.get("url"))
            if call is not None:
                out.append(self._http(call[0], call[1], n))
            elif fn.type == "identifier" and text(fn) in self.functions:
                local.append(text(fn))
            elif fn.type == "member_expression" and text(fn.child_by_field_name("object") or fn) == "this" and tail_name(fn) in self.functions:
                local.append(tail_name(fn))
        if depth < 2 and not out:
            for name in local:
                out += self.http_calls(self.function_body(name), depth + 1, seen)
        return out

    def _http(self, method: str, url_node, call) -> HttpCall:
        start, end = self.at(call)
        url = string_value(url_node) if url_node is not None else None
        if url is None and url_node is not None:
            url = template_path(url_node)
        if url is None:
            return HttpCall(method, None, None, start, end)
        external = bool(re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I)) or url.startswith("//")
        path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", url, flags=re.I) if external else url
        path = path.split("?")[0].split("#")[0] or "/"
        return HttpCall(method, path if "{}" not in path else None, url.split("?")[0], start, end, external)

    # -- navigation in code

    def navigation_calls(self) -> list[tuple[str | None, int, int, str]]:
        """(literal target path or None, start, end, how) for navigate(), router.push(), ..."""
        out = []
        for n in self.calls:
            fn = unwrap(n.child_by_field_name("function"))
            if fn is None:
                continue
            name = tail_name(fn)
            if name not in _NAV_CALLEES:
                continue
            if fn.type == "member_expression":
                obj = text(fn.child_by_field_name("object"))
                if obj not in _NAV_OBJECTS and not obj.endswith(("router", "Router", "history")):
                    continue
            elif name not in ("navigate", "navigateTo", "redirect"):
                continue
            args = n.child_by_field_name("arguments")
            first = unwrap(args.named_children[0]) if args is not None and args.named_children else None
            target = None
            if first is not None:
                target = string_value(first)
                if target is None and first.type == "array":
                    parts = [string_value(c) for c in first.named_children]
                    target = parts[0] if len(parts) == 1 and parts[0] is not None else None
                if target is None and first.type == "object":
                    p = pairs(first)
                    target = string_value(p["path"]) if "path" in p else None
            out.append((target, *self.at(n), name))
        for el in self.elements:
            node = getattr(el, "node", None)
            in_route = node is not None and node.parent is not None and (node.parent.type == "pair" or (node.parent.type == "jsx_expression" and node.parent.parent is not None and node.parent.parent.type == "jsx_attribute"))
            if el.tag in ("Navigate", "Redirect") and not in_route:  # a redirect route is counted with the routes
                out.append((el.lit("to"), el.start, el.end, el.tag))
        return out

    # -- theme objects

    def theme_objects(self, path: str) -> list[tuple[str, Any, str]]:
        """(export name, object node, construct) for exported theme and token objects."""
        out = []
        stem = posixpath.basename(path).split(".")[0]
        for name, node in self.exports.items():
            obj = self._theme_object(node)
            if obj is None:
                continue
            if name == "default":
                if stem.startswith("tailwind"):
                    theme = pairs(obj).get("theme")
                    if unwrap(theme) is not None and unwrap(theme).type == "object":
                        out.append(("theme", unwrap(theme), "web:tailwind-config"))
                elif _THEME_FILE.search(stem):
                    out.append(("default", obj, "web:theme-object"))
            elif _THEME_NAME.match(name):
                out.append((name, obj, "web:theme-object"))
        return out

    @staticmethod
    def _theme_object(node):
        node = unwrap(node)
        if node is None:
            return None
        if node.type == "object":
            return node
        if node.type == "call_expression" and tail_name(node.child_by_field_name("function")) in _THEME_CALLS:
            args = node.child_by_field_name("arguments")
            first = unwrap(args.named_children[0]) if args is not None and args.named_children else None
            return first if first is not None and first.type == "object" else None
        return None

    def flatten(self, obj, prefix: str, limit: int = 1000) -> list[tuple[str, str | None, Any]]:
        """(dotted name, literal value or None, node) for each leaf of an object literal."""
        out: list[tuple[str, str | None, Any]] = []
        stack = [(prefix, unwrap(obj), 0)]
        while stack and len(out) < limit:
            name, node, depth = stack.pop()
            if node is None:
                continue
            if node.type == "object" and depth < 8:
                items = []
                for key, value in pairs(node).items():
                    if value is None or value.type == "method_definition":
                        continue
                    items.append((f"{name}.{key}", unwrap(value) if value.type != "shorthand_property_identifier" else value, depth + 1))
                stack.extend(reversed(items))
            elif node.type == "array" and depth < 8:
                stack.extend(reversed([(f"{name}.{i}", unwrap(c), depth + 1) for i, c in enumerate(node.named_children)]))
            else:
                value = string_value(node)
                if value is None and node.type == "number":
                    value = text(node)
                out.append((name, value, node))
        return out

    def member_chains(self) -> list[tuple[list[str], Any]]:
        """Static member chains (`theme.colors.primary`, `tokens.space[2]`), outermost only."""
        out = []
        for n in self.nodes:
            if n.type not in ("member_expression", "subscript_expression"):
                continue
            p = n.parent
            if p is not None and p.type in ("member_expression", "subscript_expression") and p.child_by_field_name("object") == n:
                continue
            chain = _chain(n)
            if chain and len(chain) > 1:
                out.append((chain, n))
        return out

    # -- routes

    def framework(self) -> str:
        if self.imports_from("react-router", "react-router-dom", "@remix-run/react", "@tanstack/react-router"):
            return "react-router"
        if self.imports_from("vue-router"):
            return "vue-router"
        if self.imports_from("@angular/router"):
            return "angular-router"
        return "unknown"

    def routes(self) -> list[RouteScreen]:
        framework = self.framework()
        out: list[RouteScreen] = []
        done: set[int] = set()
        root_arrays: dict[int, bool] = {}  # array node id -> whether its call says it is the root
        router_of: dict[int, Any] = {}  # array node id -> the router call it is given to
        for n in self.calls:
            fn = unwrap(n.child_by_field_name("function"))
            name = tail_name(fn)
            if name not in _ROUTE_CALLS:
                continue
            args = n.child_by_field_name("arguments")
            for arg in (args.named_children if args is not None else []):
                arr = self._route_array(arg)
                if arr is None and unwrap(arg) is not None and unwrap(arg).type == "object":
                    arr = self._route_array(pairs(arg).get("routes"))
                if arr is not None:
                    root_arrays[arr.id] = name in _ROUTER_ROOT_CALLS
                    if name in _ROUTER_ROOT_CALLS:
                        router_of.setdefault(arr.id, n)
                    if name == "forChild":
                        root_arrays[arr.id] = False
                        self.diagnostics.append(f"RouterModule.forChild routes at byte {self.at(arr)[0]}: their prefix is set by the parent module, so their paths are not screens here")
                        done.add(arr.id)
                        for obj in arr.named_children:
                            self.constructs.append(("web:dynamic-route", *self.at(obj), False))
            if framework == "unknown":
                framework = {"createRouter": "vue-router", "VueRouter": "vue-router", "forRoot": "angular-router", "provideRouter": "angular-router"}.get(name, "react-router" if name.startswith("create") or name == "useRoutes" else framework)
        for n in self.nodes:
            if n.type == "variable_declarator":
                value = unwrap(n.child_by_field_name("value"))
                if value is None or value.type != "array" or value.id in root_arrays:
                    continue
                name = n.child_by_field_name("name")
                typ = n.child_by_field_name("type")
                if (name is not None and re.search(r"routes$", text(name), re.I)) or (typ is not None and re.search(r"Routes|RouteRecordRaw|RouteObject|RouteConfig", text(typ))):
                    if any(o.type == "object" and "path" in pairs(o) for o in value.named_children):
                        root_arrays[value.id] = False
            elif n.type == "pair":
                key = n.child_by_field_name("key")
                value = unwrap(n.child_by_field_name("value"))
                if key is not None and text(key) == "routes" and value is not None and value.type == "array" and value.id not in root_arrays:
                    root_arrays[value.id] = framework == "vue-router"
        arrays = {n.id: n for n in self.nodes if n.type == "array" and n.id in root_arrays}
        for aid, arr in sorted(arrays.items(), key=lambda kv: kv[1].start_byte):
            if aid in done:
                continue
            before = len(out)
            self._route_objects(arr, Inherited(), framework, root_arrays[aid], out, done)
            for r in out[before:]:
                r.root = router_of.get(aid)
        out += self._jsx_routes(framework)
        self._name_routers(out)
        return out

    # -- routers (docs/solution-ir-contract.md, section 8.5)

    def _name_routers(self, out: list[RouteScreen]) -> None:
        """Each route this file shows under a router (a createBrowserRouter call, a
        RouterModule.forRoot, a <BrowserRouter> tree) gets `router`, that router's label
        (`router_label`; `router<n>` when nothing names it, `<label>~<n>` when two routers
        of the file share a label). The extractor stores it as the Screen's `router`
        attribute, `<path>#<label>`, and the compiler adds `app` to the Screen keys of a
        unit that holds more than one router. A route under no router this file shows
        (a <Routes> block in a component, an exported routes array) has no `router`."""
        roots = sorted({r.root.id: r.root for r in out if r.root is not None}.values(), key=lambda n: n.start_byte)
        labels: dict[int, str] = {}
        taken: dict[str, int] = {}
        for i, node in enumerate(roots, start=1):
            label = self.router_label(node) or f"router{i}"
            seen = taken[label] = taken.get(label, 0) + 1
            labels[node.id] = label if seen == 1 else f"{label}~{seen}"
        for r in out:
            r.router = labels.get(r.root.id) if r.root is not None else None
            r.root = None
        if len(roots) > 1:
            self.diagnostics.append(f"this file declares {len(roots)} routers ({', '.join(labels[n.id] for n in roots)})")

    def router_label(self, node) -> str | None:
        """A router's label: the variable it is assigned to (`const adminRouter = createBrowserRouter(...)`);
        else, for a call, the routes variable it is given (`RouterModule.forRoot(adminRoutes)`,
        `createRouter({ routes: shopRoutes })`); else the function, class or variable that
        encloses it (`function AdminApp() { return <BrowserRouter>...`). None when nothing names it."""
        p = node.parent
        while p is not None and (p.type in _WRAPPERS or p.type == "await_expression"):
            p = p.parent
        if p is not None and p.type == "variable_declarator":
            name = p.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                return text(name)
        if node.type == "call_expression":
            args = node.child_by_field_name("arguments")
            for arg in (args.named_children if args is not None else []):
                arg = unwrap(arg)
                if arg is not None and arg.type == "identifier":
                    return text(arg)
                if arg is not None and arg.type == "object":
                    routes = unwrap(pairs(arg).get("routes"))
                    if routes is not None and routes.type in ("identifier", "shorthand_property_identifier"):
                        return text(routes)
        p = node.parent
        while p is not None:
            if p.type in ("function_declaration", "generator_function_declaration", "class_declaration", "method_definition"):
                name = p.child_by_field_name("name")
                return text(name) if name is not None else None
            if p.type == "variable_declarator":
                name = p.child_by_field_name("name")
                return text(name) if name is not None and name.type == "identifier" else None
            p = p.parent
        return None

    def _jsx_root(self, el: Element):
        """The router a top-level <Route> sits under: a router element around it, or the
        router call that wraps its createRoutesFromElements(...). None when the file shows none."""
        for a in el.ancestors():
            if a.tag in _ROUTER_ELEMENTS and a.tag != "RouterProvider":
                return getattr(a, "node", None)
        node = getattr(el, "node", None)
        while node is not None:
            if node.type == "call_expression" and tail_name(node.child_by_field_name("function")) in _ROUTER_ROOT_CALLS:
                return node
            node = node.parent
        return None

    def _route_array(self, node):
        node = unwrap(node)
        if node is None:
            return None
        if node.type == "array":
            return node
        if node.type in ("identifier", "shorthand_property_identifier"):
            value = unwrap(self.top.get(text(node)))
            return value if value is not None and value.type == "array" else None
        if node.type == "call_expression" and tail_name(node.child_by_field_name("function")) == "createRoutesFromElements":
            return None
        return None

    def _route_objects(self, arr, parent: Inherited, framework: str, root: bool, out: list[RouteScreen], done: set[int]) -> list[str]:
        """Screens from one routes array; returns the full paths it declared at this level."""
        done.add(arr.id)
        declared: list[str] = []
        for obj in arr.named_children:
            obj = unwrap(obj)
            if obj.type == "spread_element":
                self.constructs.append(("web:route-spread", *self.at(obj), False))
                self.diagnostics.append(f"routes spread in from {text(obj)[3:][:60]} at byte {self.at(obj)[0]}: they are read where they are declared")
                continue
            if obj.type != "object":
                continue
            p = pairs(obj)
            start, end = self.at(obj)
            index = unwrap(p.get("index")) is not None and text(unwrap(p["index"])) == "true"
            raw_path = string_value(p["path"]) if "path" in p else ("" if index else None)
            if raw_path is None:
                if "path" in p:
                    self.constructs.append(("web:dynamic-route", start, end, False))
                    self.diagnostics.append(f"route at byte {start}: path {text(p['path'])[:60]} is not a literal")
                continue
            if not root and parent.path == "/" and raw_path and not raw_path.startswith("/"):
                self.constructs.append(("web:dynamic-route", start, end, False))
                self.diagnostics.append(f"route '{raw_path}' at byte {start} is relative to a prefix this file does not show")
                continue
            full = join_route(parent.path, raw_path)
            here = Inherited(full, parent.layouts, parent.guards, parent.roles, parent.requires_auth)
            guards, roles, auth = self._object_guards(p)
            here.guards += tuple(g for g in guards if g not in here.guards)
            # A route that names its own roles narrows its parent's: the nearest list is the one that applies.
            here.roles = tuple(roles) or here.roles
            here.requires_auth = here.requires_auth or auth
            component, module, lazy = self._object_component(p)
            redirect = "redirect" in p or "redirectTo" in p
            children = unwrap(p.get("children"))
            if component is None and module is None and "loadChildren" in p:
                module, lazy = dynamic_import(p["loadChildren"]), True
            renders = component is not None or module is not None
            child_paths: list[str] = []
            if children is not None and children.type == "array":
                child = Inherited(full, here.layouts + ((component or module or "",) if renders else ()), here.guards, here.roles, here.requires_auth)
                child_paths = self._route_objects(children, child, framework, True, out, done)
                if renders:
                    self.constructs.append(("web:layout-route", start, end, True))
            if lazy:
                self.constructs.append(("web:lazy-route", start, end, renders))
            if redirect and not renders:
                self.constructs.append(("web:redirect-route", start, end, False))
                continue
            if not renders or full in child_paths:
                continue
            attrs: dict[str, Any] = {"framework": framework, "component": component, "component_module": module, "lazy": lazy or None,
                                     "layouts": list(here.layouts) or None, "guards": list(here.guards) or None,
                                     "requires_auth": here.requires_auth or None, "route_name": string_value(p["name"]) if "name" in p else None,
                                     "title": string_value(p["title"]) if "title" in p else None, "loads_children": "loadChildren" in p or None}
            out.append(RouteScreen(full, start, end, "web:route-object", framework, attrs, list(here.roles), component, module))
            declared.append(full)
        return declared

    def _object_component(self, p: dict[str, Any]) -> tuple[str | None, str | None, bool]:
        """(component name, module specifier, lazy) from a route object's renderer keys."""
        for key in ("element", "component", "Component", "loadComponent", "lazy", "components"):
            node = unwrap(p.get(key))
            if node is None:
                continue
            if key == "components" and node.type == "object":
                node = unwrap(pairs(node).get("default"))
                if node is None:
                    continue
            if node.type in ("jsx_element", "jsx_self_closing_element"):
                el = self.element_of(node)
                name = self.element_component(el)[0] if el is not None else None
                return name, *self._module_of(name)
            if node.type in ("identifier", "shorthand_property_identifier"):
                name = text(node)
                return name, *self._module_of(name)
            if node.type in ("arrow_function", "function_expression", "call_expression"):
                spec = dynamic_import(node)
                if spec is not None:
                    then = re.search(r"\.then\(\s*\(?\s*(\w+)\s*\)?\s*=>\s*\1\.(\w+)", text(node))
                    return (then.group(2) if then else None), spec, True
        return None, None, False

    def _module_of(self, name: str | None) -> tuple[str | None, bool]:
        """(specifier, lazy) for a component name bound by an import or a lazy() declaration."""
        if name is None:
            return None, False
        if name in self.imports:
            return self.imports[name][0], False
        value = unwrap(self.top.get(name))
        if value is not None and value.type == "call_expression" and tail_name(value.child_by_field_name("function")) in ("lazy", "defineAsyncComponent", "loadable"):
            spec = dynamic_import(value)
            return spec, spec is not None
        return None, False

    def _object_guards(self, p: dict[str, Any]) -> tuple[list[str], list[tuple[str, int, int]], bool]:
        guards: list[str] = []
        roles: list[tuple[str, int, int]] = []
        auth = False
        holders = [p] + [pairs(p[k]) for k in ("meta", "data", "handle") if k in p]
        for holder in holders:
            for key, value in holder.items():
                value = unwrap(value)
                if value is None:
                    continue
                if key in ROLE_KEYS:
                    names = string_list(value)
                    if names is None:
                        self.constructs.append(("web:route-role", *self.at(value), False))
                        self.diagnostics.append(f"route roles at byte {self.at(value)[0]} are not literal: {text(value)[:60]}")
                    else:
                        roles += [(r, *self.at(value)) for r in names]
                elif key in AUTH_KEYS and text(value) == "true":
                    auth = True
                    self.constructs.append(("web:route-guard", *self.at(value), True))
        for key in GUARD_KEYS:
            value = unwrap(p.get(key))
            if value is None:
                continue
            items = value.named_children if value.type == "array" else [value]
            for item in items:
                item = unwrap(item)
                g, r = self._guard_item(item)
                if g:
                    guards.append(g)
                roles += r
                self.constructs.append(("web:route-guard", *self.at(item), bool(g)))
        return guards, roles, auth

    def _guard_item(self, item) -> tuple[str | None, list[tuple[str, int, int]]]:
        if item.type in ("identifier", "member_expression"):
            return text(item)[:60], []
        s = string_value(item)
        if s is not None:  # Nuxt middleware by name
            return s, []
        if item.type == "call_expression":
            callee = tail_name(item.child_by_field_name("function")) or ""
            roles = []
            if re.search(r"role", callee, re.I):
                args = item.child_by_field_name("arguments")
                for arg in (args.named_children if args is not None else []):
                    names = string_list(arg)
                    if names is None:
                        self.diagnostics.append(f"guard {callee} at byte {self.at(item)[0]} takes a role that is not literal")
                    else:
                        roles += [(r, *self.at(arg)) for r in names]
            return callee[:60] or None, roles
        return None, []

    def element_component(self, el: Element | None) -> tuple[str | None, list[str], list[tuple[str, int, int]], bool]:
        """For a route's element tree: (the component it renders, guard wrappers, literal
        roles they require, whether a wrapper requires authentication)."""
        guards: list[str] = []
        roles: list[tuple[str, int, int]] = []
        auth = False
        seen = 0
        while el is not None and seen < 16:
            seen += 1
            nested = el.children[0] if el.children else None
            if el.tag in _SKIP_WRAPPERS and nested is not None:
                el = nested
                continue
            role_attr = el.get(*ROLE_KEYS)
            if _GUARD_TAG.search(el.tag) and (nested is not None or role_attr is not None):
                guards.append(el.tag)
                auth = True
                if role_attr is not None:
                    names = [role_attr.value] if role_attr.literal and role_attr.value else string_list(role_attr.node) if role_attr.node is not None else None
                    if names:
                        roles += [(r, role_attr.start, role_attr.end) for r in names]
                    else:
                        self.diagnostics.append(f"<{el.tag}> at byte {el.start} names roles that are not literal")
                if nested is None:
                    return None, guards, roles, auth
                el = nested
                continue
            return el.tag, guards, roles, auth
        return None, guards, roles, auth

    def _jsx_routes(self, framework: str) -> list[RouteScreen]:
        out: list[RouteScreen] = []
        routes = [el for el in self.elements if el.tag in ("Route", "ReactRouter.Route")]
        if not routes:
            return out
        root = any(el.tag in _ROUTER_ELEMENTS for el in self.elements) or any(tail_name(c.child_by_field_name("function")) == "createRoutesFromElements" for c in self.calls)
        top = [el for el in routes if not any(a.tag in ("Route", "ReactRouter.Route") for a in el.ancestors())]
        for el in top:
            before = len(out)
            self._jsx_route(el, Inherited(), "react-router" if framework == "unknown" else framework, root, out)
            for r in out[before:]:
                r.root = self._jsx_root(el)
        return out

    def _jsx_route(self, el: Element, parent: Inherited, framework: str, root: bool, out: list[RouteScreen]) -> list[str]:
        index = el.has("index")
        raw = el.lit("path")
        if raw is None and el.has("path"):
            self.constructs.append(("web:dynamic-route", el.start, el.end, False))
            self.diagnostics.append(f"<Route> at byte {el.start}: path {el.attrs['path'].value[:60] if el.attrs['path'].value else ''} is not a literal")
            return []
        raw = raw if raw is not None else ""
        if not root and parent.path == "/" and raw and not raw.startswith("/"):
            self.constructs.append(("web:dynamic-route", el.start, el.end, False))
            self.diagnostics.append(f"<Route path=\"{raw}\"> at byte {el.start} is relative to a prefix this file does not show")
            return []
        full = join_route(parent.path, raw)
        component = module = None
        lazy = False
        here = Inherited(full, parent.layouts, parent.guards, parent.roles, parent.requires_auth)
        element = el.get("element")
        redirect = False
        if element is not None and element.node is not None and unwrap(element.node).type in ("jsx_element", "jsx_self_closing_element"):
            inner = self.element_of(unwrap(element.node))
            component, guards, roles, auth = self.element_component(inner)
            if component in ("Navigate", "Redirect"):
                redirect, component = True, None
            for g in guards:
                self.constructs.append(("web:route-guard", el.start, el.end, True))
            here.guards += tuple(g for g in guards if g not in here.guards)
            here.roles = tuple(roles) or here.roles
            here.requires_auth = here.requires_auth or auth
        else:
            for key in ("component", "Component"):
                a = el.get(key)
                if a is not None and a.node is not None and unwrap(a.node).type == "identifier":
                    component = text(unwrap(a.node))
        if component is not None:
            module, lazy = self._module_of(component)
        lazy_attr = el.get("lazy")
        if lazy_attr is not None and lazy_attr.node is not None:
            module, lazy = dynamic_import(lazy_attr.node), True
        renders = component is not None or module is not None
        nested = [c for c in el.children if c.tag in ("Route", "ReactRouter.Route")]
        child_paths: list[str] = []
        if nested:
            child = Inherited(full, here.layouts + ((component or module or "",) if renders else ()), here.guards, here.roles, here.requires_auth)
            for c in nested:
                child_paths += self._jsx_route(c, child, framework, True, out)
            if renders:
                self.constructs.append(("web:layout-route", el.start, el.end, True))
        if lazy:
            self.constructs.append(("web:lazy-route", el.start, el.end, renders))
        if redirect:
            self.constructs.append(("web:redirect-route", el.start, el.end, False))
            return []
        if not renders or full in child_paths:
            return child_paths
        attrs = {"framework": framework, "component": component, "component_module": module, "lazy": lazy or None,
                 "layouts": list(here.layouts) or None, "guards": list(here.guards) or None, "requires_auth": here.requires_auth or None}
        out.append(RouteScreen(full, el.start, el.end, "web:react-router-route", framework, attrs, list(here.roles), component, module))
        return child_paths + [full]


def join_route(parent: str, path: str) -> str:
    """The full route path: an absolute child path stands alone, a relative one joins its parent's."""
    if path.startswith("/"):
        full = path
    elif path in ("", "/"):
        full = parent
    else:
        full = parent.rstrip("/") + "/" + path
    full = re.sub(r"/{2,}", "/", "/" + full.lstrip("/"))
    return full.rstrip("/") or "/"


def _chain(node) -> list[str] | None:
    parts: list[str] = []
    n = node
    while n is not None:
        if n.type == "member_expression":
            prop = n.child_by_field_name("property")
            if prop is None or prop.type != "property_identifier":
                return None
            parts.append(text(prop))
            n = n.child_by_field_name("object")
        elif n.type == "subscript_expression":
            idx = unwrap(n.child_by_field_name("index"))
            v = string_value(idx) if idx is not None else None
            if v is None and idx is not None and idx.type == "number":
                v = text(idx)
            if v is None:
                return None
            parts.append(v)
            n = n.child_by_field_name("object")
        elif n.type in ("identifier", "this"):
            parts.append(text(n))
            return list(reversed(parts))
        else:
            return None
    return None


_STORES = frozenset({"createSlice", "configureStore", "createStore", "defineStore", "createReducer", "createFeatureSelector"})
_FETCH_HOOKS = frozenset({"useQuery", "useSWR", "useMutation", "useInfiniteQuery", "useSuspenseQuery", "useFetch", "useAsyncData"})
_ROLE_CHECKS = frozenset({"hasRole", "hasAnyRole", "isInRole", "checkRole", "hasAuthority", "hasAnyAuthority", "userHasRole"})
_I18N_CALLEES = frozenset({"t", "$t", "i18n.t", "this.$t", "i18next.t", "$i18n.t", "intl.formatMessage", "translate", "this.translate.instant"})
_CSS_IN_JS = frozenset({"css", "keyframes", "createGlobalStyle", "injectGlobal"})
_STORAGE = frozenset({"localStorage", "sessionStorage", "window.localStorage", "window.sessionStorage"})


def misc_constructs(s: Script) -> list[tuple[str, int, int]]:
    """Constructs a front end uses that the pack counts without modelling."""
    out: list[tuple[str, int, int]] = []
    for n in s.calls:
        fn = unwrap(n.child_by_field_name("function"))
        if fn is None:
            continue
        ftext = text(fn) if fn.type in ("identifier", "member_expression") else ""
        name = tail_name(fn)
        args = n.child_by_field_name("arguments")
        tagged = args is not None and args.type == "template_string"
        first = unwrap(args.named_children[0]) if args is not None and args.type == "arguments" and args.named_children else None
        cid = None
        if ftext.endswith("customElements.define"):
            cid = "web:custom-element"
        elif ftext.endswith("serviceWorker.register"):
            cid = "web:service-worker"
        elif name == "createPortal":
            cid = "web:portal"
        elif name in _STORES or (ftext in s.imports and s.imports[ftext][0] in ("zustand", "zustand/vanilla", "jotai", "valtio")):
            cid = "web:state-store"
        elif name in _FETCH_HOOKS:
            cid = "web:data-fetching-hook"
        elif name in _ROLE_CHECKS:
            cid = "web:role-check"
        elif tagged and (ftext.startswith("styled.") or (fn.type == "call_expression" and tail_name(fn.child_by_field_name("function")) == "styled")):
            cid = "web:styled-component"
        elif (tagged and name in _CSS_IN_JS) or (not tagged and name in ("makeStyles", "createStyles")):
            cid = "web:css-in-js"
        elif ftext in _I18N_CALLEES and first is not None and string_value(first) is not None:
            cid = "web:i18n-key"
        if cid:
            out.append((cid, *s.at(n)))
    for n in s.nodes:
        if n.type == "new_expression":
            ctor = tail_name(n.child_by_field_name("constructor"))
            cid = {"Worker": "web:web-worker", "SharedWorker": "web:web-worker", "WebSocket": "web:websocket", "EventSource": "web:websocket"}.get(ctor or "")
            if cid:
                out.append((cid, *s.at(n)))
        elif n.type == "member_expression":
            obj = n.child_by_field_name("object")
            if obj is not None and (text(obj) in _STORAGE or text(n) == "document.cookie"):
                out.append(("web:browser-storage", *s.at(n)))
    return out
