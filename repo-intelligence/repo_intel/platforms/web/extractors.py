"""The web pack's extractors: one each for markup (HTML, Vue), scripts (JS, TS, JSX, TSX)
and stylesheets (CSS, SCSS, LESS). Each reads one file's bytes and nothing else.

What a file contributes:

- A router file: one Screen (`web:Route`) per routed path, named by the full path
  (`/orders/:id`); the unit's package RENDERS it (the component is a qualifier); it
  REQUIRES_ROLE each literal role its guards name; it CONTAINS the View its component
  module is, when that module is a relative import and declares a View.
- A Next.js app/ or pages/ file, a Nuxt page: a `web:Route` Screen named by the file route.
- A static HTML document: a `web:Page` Screen named by its path.
- Any other component module or template with a form or a link to a screen: a
  `web:View` Screen named by its path.
- Under the file's Screen: Forms and UIControls (CONTAINS), SUBMITS_TO, NAVIGATES_TO,
  bindings as attributes, STYLED_BY for the tokens it references.
- Stylesheets, style blocks and theme modules: StyleTokens.

Test files, stories and type declarations are not read.

Nothing here imports `repo_intel.extractors` at module level (see `experience.py`):
`extract` returns the `repo_intel.extractors.base.Extraction` that `Out` builds.
"""

from __future__ import annotations

import posixpath
import re

from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms.web import markup, script, styles
from repo_intel.platforms.web.experience import Out, Owner, emit_roles, emit_style_refs, emit_tokens, emit_ui, has_ui, link_targets, renders, screen_ref

P = Predicate
_SKIP = re.compile(r"(^|/)(node_modules|dist|build|coverage|\.next|\.nuxt|out|vendor|__tests__|__mocks__|e2e|cypress)/|\.(test|spec|stories|story|d)\.[a-z]+$|\.min\.(js|css)$", re.I)
_DOCUMENT = re.compile(r"<!doctype\s+html|<html[\s>]|<body[\s>]", re.I)
_TEMPLATE_I18N = re.compile(r"\$t\(\s*['\"]|\|\s*translate\b")
_THEME_IMPORT = re.compile(r"(^|[./_-])(theme|tokens?|design-tokens)$", re.I)


def _skipped(path: str) -> bool:
    return bool(_SKIP.search(path))


def _file_route(path: str, s: script.Script | None) -> tuple[str, str] | None:
    """(route, construct) for a Next.js app/ or pages/ file."""
    parts = path.split("/")
    stem, ext = posixpath.splitext(parts[-1])
    if ext not in (".tsx", ".jsx", ".js", ".ts"):
        return None
    if stem == "page" and "app" in parts[:-1]:
        last = len(parts) - 2 - parts[-2::-1].index("app")
        segments = [p for p in parts[last + 1:-1] if not (p.startswith("(") and p.endswith(")")) and not p.startswith("@")]
        return "/" + "/".join(segments), "web:nextjs-app-page"
    if "pages" in parts[:-1] and s is not None and not stem.startswith("_"):
        last = len(parts) - 2 - parts[-2::-1].index("pages")
        rest = parts[last + 1:-1] + ([] if stem == "index" else [stem])
        if rest[:1] == ["api"]:
            return None
        next_signal = s.imports_from("next") or any(name in s.exports for name in ("getServerSideProps", "getStaticProps", "getStaticPaths")) or "getInitialProps" in s.functions
        if next_signal:
            return "/" + "/".join(rest), "web:nextjs-pages-route"
    return None


def _nuxt_route(path: str) -> str | None:
    parts = path.split("/")
    if "pages" not in parts[:-1]:
        return None
    last = len(parts) - 2 - parts[-2::-1].index("pages")
    stem = posixpath.splitext(parts[-1])[0]
    rest = parts[last + 1:-1] + ([] if stem == "index" else [stem])
    return "/" + "/".join(rest)


def _route_screens(out: Out, s: script.Script) -> None:
    for r in s.routes():
        screen = out.entity("web:Route", {"platform": "web", "name": r.name}, r.name, r.start, r.end, r.attrs)
        out.construct(r.construct, r.start, r.end, True)
        renders(out, screen, r.start, r.end, r.component or r.module)
        emit_roles(out, screen, r.roles)
        for candidate in script.module_candidates(out.path, r.module, script.framework_exts(r.framework)) if r.module else []:
            out.relate(screen, P.CONTAINS, screen_ref(candidate, declare=False), r.start, r.end, cls=EvidenceClass.E3)
    for cid, a, b, modelled in s.constructs:
        out.construct(cid, a, b, modelled)
    for message in s.diagnostics:
        out.diag(message)
    s.constructs.clear()
    s.diagnostics.clear()


def _script_resolver(scripts: list[script.Script]):
    """Submit handlers, looked up in the file's own scripts: a JSX handler by its
    expression node, a template's handler by its text (`save`, `save($event)`)."""
    def resolve(el, attr):
        if attr.node is not None and scripts:
            s = scripts[0]
            name, body, library = s.handler(attr.node)
            return name, (s.http_calls(body) if body is not None else None), library
        for s in scripts:
            name, body = s.handler_by_text(attr.value or "")
            if body is not None:
                return name, s.http_calls(body), None
        return None, None, None
    return resolve


def _in_route_config(el: markup.Element) -> bool:
    """An element that is a route's element (`element={<X/>}`, `element: <X/>`), not part of a view."""
    if any(a.tag == "Route" for a in el.ancestors()):
        return True
    node = getattr(el, "node", None)
    while node is not None:
        if node.type == "pair":
            key = node.child_by_field_name("key")
            if key is not None and script.text(key) in ("element", "component", "errorElement"):
                return True
        node = node.parent
    return False


def _navigation(out: Out, owner: Owner | None, scripts: list[script.Script]) -> None:
    for s in scripts:
        for target, a, b, how in s.navigation_calls():
            screens = link_targets(out.path, target)[1] if target is not None else []
            if owner is not None and screens:
                for name in screens:
                    out.relate(owner.ref, P.NAVIGATES_TO, screen_ref(name, declare=False), a, b, cls=EvidenceClass.E3)
            elif target is None:
                out.diag(f"{how}() at byte {a} navigates to a target that is not a literal")
            out.construct("web:programmatic-navigation", a, b, owner is not None and bool(screens))


_ENV_READ = re.compile(r"(process\.env|import\.meta\.env|window\.)")


def _theme_tokens(out: Out, s: script.Script) -> set[str]:
    """Tokens from exported theme objects; returns the export names that hold them."""
    names = set()
    for export, obj, cid in s.theme_objects(out.path):
        names.add(export)
        for name, value, node in s.flatten(obj, export):
            a, b = s.at(node)
            if value is None and (node.type in ("call_expression", "arrow_function", "function_expression", "new_expression") or _ENV_READ.match(script.text(node))):
                out.construct(cid, a, b, False)  # computed or read from the environment: not a design value
                continue
            safe = styles.safe_value(value) if value is not None else None
            out.entity("web:ThemeToken", {"system": out.path, "name": name}, name, a, b, {"category": styles.category(name, safe), "value": safe})
            out.construct(cid, a, b, True)
    return names


def _script_style_refs(out: Out, owner: Owner | None, s: script.Script, stylesheets: list[str], theme_exports: set[str]) -> None:
    """STYLED_BY for var(--x) in strings and for theme.x member chains."""
    refs = []
    for n in s.nodes:
        if n.type in ("string", "template_string") and (n.parent is None or n.parent.type not in ("import_statement", "template_string")):
            raw = script.text(n)
            if "var(" in raw:
                for m in styles._VAR_REF.finditer(raw):
                    a = s.base + n.start_byte + len(raw[:m.start()].encode())
                    refs.append((m.group(1), a, a + len(m.group(0).encode())))
    emit_style_refs(out, owner, refs, stylesheets, None, set())
    for chain, node in s.member_chains():
        root = chain[0]
        a, b = s.at(node)
        if root in theme_exports:  # the theme this file declares
            emit_style_refs(out, owner, [(".".join(chain), a, b)], [], out.path, {".".join(chain)})
            continue
        bound = s.imports.get(root)
        if bound is not None and bound[0].startswith("."):
            spec, imported = bound
            stem = posixpath.basename(spec).split(".")[0]
            themed = script._THEME_NAME.match(imported) or (imported in ("default", "*") and _THEME_IMPORT.search(stem))
            if not themed:
                continue
            name = ".".join(chain[1:]) if imported == "*" else ".".join([imported, *chain[1:]])
            candidates = script.module_candidates(out.path, spec, (".ts", ".tsx", ".js", ".jsx", ".mjs"))
            emit_style_refs(out, owner, [(name, a, b)], candidates, None, set())
        elif "theme" in (root, chain[1] if len(chain) > 1 else ""):
            out.construct("web:token-reference", a, b, False)  # a theme passed in at run time (props.theme, ({ theme }) => ...)


class _Sheets:
    """The stylesheets a file uses: candidate paths for token references, the paths as
    written (for the screen's `stylesheets` attribute), and the constructs to count once
    the file's screen is known."""

    def __init__(self, importer: str):
        self.importer = importer
        self.paths: list[str] = []
        self.names: list[str] = []
        self.seen: list[tuple[str, int, int, bool]] = []

    def add(self, spec: str, construct: str, a: int, b: int, dialect: str | None = None) -> None:
        found = script.style_candidates(self.importer, spec, dialect)
        shown = script.style_path(self.importer, spec)
        self.paths += [p for p in found if p not in self.paths]
        if shown and shown not in self.names:
            self.names.append(shown)
        self.seen.append((construct, a, b, bool(found)))

    def attribute(self) -> list[str] | None:
        return sorted(self.names) or None

    def emit(self, out: Out, owner: Owner | None) -> None:
        for cid, a, b, found in self.seen:
            out.construct(cid, a, b, owner is not None and found)


def _script_sheets(sheets: _Sheets, s: script.Script) -> None:
    for spec, is_module, a, b in s.stylesheet_imports():
        sheets.add(spec, "web:css-module" if is_module else "web:stylesheet-import", a, b, styles.dialect_of(spec.split("?")[0]))


def _style_scan(out: Out, a: int, b: int, dialect: str) -> styles.StyleScan:
    """A style block's bytes `a:b`, scanned the way a stylesheet file is (one character per
    byte), so the offsets the scan reports are byte offsets into the file."""
    return styles.scan(out.data[a:b].decode("latin-1"), dialect, a)


def _misc(out: Out, s: script.Script) -> None:
    for cid, a, b in script.misc_constructs(s):
        out.construct(cid, a, b, False)


class WebScript:
    """JavaScript and TypeScript modules: routers, file routes, views, theme objects."""

    id = "web.script"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(script.SCRIPT_EXTS) and not _skipped(path)

    def extract(self, path: str, data: bytes, blob: str):
        out = Out(self.id, path, data, blob)
        s = script.Script(data, script.grammar_for(path))
        _route_screens(out, s)
        templates = self._angular_templates(out, s)
        elements = [el for el in s.elements + templates if not _in_route_config(el)]
        sheets = _Sheets(path)
        _script_sheets(sheets, s)
        # Guard components inside the file's own markup (`<RequireRole role="admin">`): the
        # roles they list become Roles the screen REQUIRES_ROLE, the guards' names its `guards`.
        guard_els = [el for el in s.elements if script._GUARD_TAG.search(el.tag) and el.get(*script.ROLE_KEYS) is not None and not _in_route_config(el)]
        guards = list(dict.fromkeys(el.tag for el in guard_els)) or None
        owner = None
        route = _file_route(path, s)
        component = s.default_export_name()
        if route is not None:
            name, cid = route
            ref = out.entity("web:Route", {"platform": "web", "name": name}, name, 0, len(data), {"framework": "nextjs", "router": "app" if cid == "web:nextjs-app-page" else "pages", "component": component, "stylesheets": sheets.attribute(), "guards": guards})
            out.construct(cid, 0, len(data), True)
            owner = Owner(name, ref)
        elif has_ui(elements, path):
            ref = out.entity("web:View", {"platform": "web", "name": path}, path, 0, len(data), {"framework": self._framework(s, templates), "component": component, "stylesheets": sheets.attribute(), "guards": guards})
            out.construct("web:view", 0, len(data), True)
            owner = Owner(path, ref)
        if owner is not None:
            renders(out, owner.ref, 0, len(data), component)
        if path.split("/")[-1].startswith("layout.") and "app" in path.split("/")[:-1]:
            out.construct("web:nextjs-layout", 0, len(data), False)
        sheets.emit(out, owner)
        emit_ui(out, owner, elements, _script_resolver([s]))
        for el in guard_els if owner is not None else []:
            emit_roles(out, owner.ref, s.element_component(el)[2])
        for message in s.diagnostics:
            out.diag(message)
        _navigation(out, owner, [s])
        theme_exports = _theme_tokens(out, s)
        _script_style_refs(out, owner, s, sheets.paths, theme_exports)
        _misc(out, s)
        return out.result

    @staticmethod
    def _framework(s: script.Script, templates: list[markup.Element]) -> str:
        if templates or s.imports_from("@angular/core"):
            return "angular"
        if s.imports_from("vue"):
            return "vue"
        if s.imports_from("react", "react-dom", "next", "preact", "solid-js"):
            return "react"
        return "jsx"

    @staticmethod
    def _angular_templates(out: Out, s: script.Script) -> list[markup.Element]:
        """Elements of inline Angular templates (`@Component({ template: `...` })`)."""
        found: list[markup.Element] = []
        for n in s.calls:
            if script.tail_name(n.child_by_field_name("function")) != "Component":
                continue
            args = n.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            p = script.pairs(first) if first is not None else {}
            if "templateUrl" in p:
                out.construct("web:angular-template-url", *s.at(p["templateUrl"]), False)
            template = script.unwrap(p.get("template"))
            if template is not None and script.string_value(template) is not None:
                content, byte_of = markup.decode(template.text[1:-1])
                found += markup.scan(content, s.base + template.start_byte + 1, byte_of=byte_of)
        return found


class WebMarkup:
    """HTML documents and fragments, and Vue single-file components."""

    id = "web.markup"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".html", ".htm", ".vue")) and not _skipped(path)

    def extract(self, path: str, data: bytes, blob: str):
        out = Out(self.id, path, data, blob)
        text, byte_of = markup.decode(data)
        if path.lower().endswith(".vue"):
            self._vue(out, text, byte_of)
        else:
            self._html(out, text, byte_of)
        return out.result

    def _html(self, out: Out, text: str, byte_of: markup.ByteOf) -> None:
        path = out.path
        elements = markup.scan(text, 0, lower=True, byte_of=byte_of)
        sheets = _Sheets(path)
        style_scans = []
        for el in elements:
            if el.tag == "link" and "stylesheet" in (el.lit("rel") or "").lower() and el.lit("href"):
                sheets.add(el.lit("href"), "web:stylesheet-import", el.start, el.end)
            elif el.tag == "style" and el.raw is not None:
                style_scans.append(_style_scan(out, el.raw_start, el.raw_end, "css"))
            elif el.tag == "script":
                out.construct("web:external-script" if el.has("src") else "web:inline-script", el.start, el.end, False)
        owner = None
        if _DOCUMENT.search(text):
            title = next((el.text for el in elements if el.tag == "title" and el.text), None)
            ref = out.entity("web:Page", {"platform": "web", "name": path}, path, 0, len(out.data), {"title": title[:120] if title else None, "stylesheets": sheets.attribute()})
            out.construct("web:static-page", 0, len(out.data), True)
            owner = Owner(path, ref)
        elif has_ui(elements, path):
            framework = "angular" if any(a.raw.startswith(("(", "[", "*ng")) for el in elements for a in el.attrs.values()) else "html"
            ref = out.entity("web:View", {"platform": "web", "name": path}, path, 0, len(out.data), {"framework": framework, "stylesheets": sheets.attribute()})
            out.construct("web:view", 0, len(out.data), True)
            owner = Owner(path, ref)
        if owner is not None:
            renders(out, owner.ref, 0, len(out.data), None)
        sheets.emit(out, owner)
        emit_ui(out, owner, elements, None)
        self._template_misc(out, text, byte_of)
        self._styles(out, owner, style_scans, elements, sheets.paths)

    def _vue(self, out: Out, text: str, byte_of: markup.ByteOf) -> None:
        path = out.path
        scripts: list[script.Script] = []
        elements: list[markup.Element] = []
        style_scans: list[styles.StyleScan] = []
        sheets = _Sheets(path)
        template_at = (0, 0)
        for tag, attrs, a, b in markup.sfc_blocks(text):  # a and b are text positions
            if tag == "template":
                elements = markup.scan(text[a:b], 0, byte_of=lambda i, a=a: byte_of(a + i))
                template_at = (a, b)
            elif tag == "script":
                lang = attrs.get("lang", "js")
                scripts.append(script.Script(out.data[byte_of(a):byte_of(b)], script.grammar_for(lang if lang in ("ts", "tsx", "jsx") else "js"), byte_of(a)))
            elif tag == "style":
                dialect = styles.dialect_of(attrs.get("lang", "css"))
                scan = _style_scan(out, byte_of(a), byte_of(b), dialect)
                style_scans.append(scan)
                for rule, spec, x, y in scan.imports:
                    sheets.add(spec, "web:stylesheet-import", x, y, dialect)
                if attrs.get("src"):
                    sheets.add(attrs["src"], "web:stylesheet-import", byte_of(a), byte_of(a), dialect)
        for s in scripts:
            _route_screens(out, s)
            _script_sheets(sheets, s)
        owner = None
        page_meta = next(((s, c) for s in scripts for c in s.calls if script.tail_name(c.child_by_field_name("function")) == "definePageMeta"), None)
        route = _nuxt_route(path) if page_meta is not None else None
        if route is not None:
            s, call = page_meta
            args = call.child_by_field_name("arguments")
            p = script.pairs(args.named_children[0]) if args is not None and args.named_children else {}
            guards, roles, auth = s._object_guards(p)
            ref = out.entity("web:Route", {"platform": "web", "name": route}, route, *s.at(call), {"framework": "nuxt", "guards": guards or None, "requires_auth": auth or None, "stylesheets": sheets.attribute()})
            out.construct("web:nuxt-page", *s.at(call), True)
            owner = Owner(route, ref)
            emit_roles(out, ref, roles)
            for cid, x, y, modelled in s.constructs:
                out.construct(cid, x, y, modelled)
            s.constructs.clear()
        elif has_ui(elements, path):
            ref = out.entity("web:View", {"platform": "web", "name": path}, path, 0, len(out.data), {"framework": "vue", "stylesheets": sheets.attribute()})
            out.construct("web:view", 0, len(out.data), True)
            owner = Owner(path, ref)
        if owner is not None:
            renders(out, owner.ref, 0, len(out.data), posixpath.splitext(posixpath.basename(path))[0])
        sheets.emit(out, owner)
        library = "vee-validate" if any(s.imports_from("vee-validate") for s in scripts) else None
        emit_ui(out, owner, elements, _script_resolver(scripts), library)
        _navigation(out, owner, scripts)
        for s in scripts:
            _script_style_refs(out, owner, s, sheets.paths, set())
            _misc(out, s)
        ta = template_at[0]
        self._template_misc(out, text[ta:template_at[1]], lambda i: byte_of(ta + i))
        self._styles(out, owner, style_scans, elements, sheets.paths)

    @staticmethod
    def _template_misc(out: Out, text: str, byte_of: markup.ByteOf) -> None:
        for m in _TEMPLATE_I18N.finditer(text):
            out.construct("web:i18n-key", byte_of(m.start()), byte_of(m.end()), False)

    @staticmethod
    def _styles(out: Out, owner: Owner | None, scans: list[styles.StyleScan], elements: list[markup.Element], stylesheets: list[str]) -> None:
        local: set[str] = set()
        refs: list[tuple[str, int, int]] = []
        for scan in scans:
            local |= emit_tokens(out, out.path, scan)
            refs += scan.refs
            for cid, a, b in scan.constructs:
                out.construct(cid, a, b, cid in ("web:css-custom-property", "web:scss-variable", "web:less-variable"))
        for el in elements:  # style="color: var(--brand)"
            attr = el.get("style")
            if attr is not None and attr.value and "var(" in attr.value:
                for m in styles._VAR_REF.finditer(attr.value):
                    refs.append((m.group(1), attr.start, attr.end))
        emit_style_refs(out, owner, refs, stylesheets, out.path, local)


class WebStyles:
    """Stylesheets: design tokens and the at-rules a stylesheet uses."""

    id = "web.styles"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(script.STYLE_EXTS) and not _skipped(path)

    def extract(self, path: str, data: bytes, blob: str):
        out = Out(self.id, path, data, blob)
        scan = styles.scan(data.decode("latin-1"), styles.dialect_of(path))
        emit_tokens(out, path, scan)
        for cid, a, b in scan.constructs:
            out.construct(cid, a, b, cid in ("web:css-custom-property", "web:scss-variable", "web:less-variable"))
        for rule, spec, a, b in scan.imports:
            out.construct("web:stylesheet-import", a, b, False)  # one stylesheet using another has no relation kind yet
        for name, a, b in scan.refs:
            out.construct("web:token-reference", a, b, False)  # a stylesheet is not a screen: nothing to hang STYLED_BY on
        return out.result
