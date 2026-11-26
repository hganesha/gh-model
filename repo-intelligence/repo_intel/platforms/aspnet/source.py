"""`aspnet.source`: Web Forms pages (`.aspx`, `.ascx`, `.master`) and Razor (`.cshtml`, `.razor`).

For each file:

- a `Program` named by its path: `aspx:Page`, `aspx:UserControl`, `aspx:MasterPage`,
  `aspx:RazorFile` (its `kind`: page, view, partial, layout, view_start, view_imports)
  or `aspx:RazorComponent`. The code-behind file a Web Forms directive names is the
  Artifact the Program DEPENDS_ON, and its base class (`Inherits`) an attribute; the
  master page, layout and registered user controls are Programs it CALLS;
- an `.aspx` page EXPOSES an Endpoint at its path; a Razor Page (`@page`) one at the
  route ASP.NET Core gives it: its path below `Pages/` (`Index` dropped) with the
  `@page` template appended, or the template alone when it starts with `/`;
- a page that renders is a `Screen` (`aspx:WebForm`, `aspx:RazorView`), named by its
  path; a user control, master page, partial or layout is one only when it holds forms
  or links. Forms are server forms (which post back to their page), HTML forms,
  tag-helper forms, `Html.BeginForm` blocks and Blazor `EditForm`s; controls are
  server controls (named by `ID`, the contract's id rule), HTML inputs (`asp-for` is
  the binding path), HTML helpers (`TextBoxFor(m => m.Qty)` binds `Qty`) and Blazor
  inputs (`@bind-Value`). Web Forms validators become the `validation` attribute of the
  control they validate. A button's `PostBackUrl` is a page its form SUBMITS_TO;
- links (`href`, `NavigateUrl`, `asp-page`), `Response.Redirect`, `Server.Transfer` and
  `NavigationManager.NavigateTo` with literal targets NAVIGATE_TO the page or Endpoint
  they name; `~/` paths are tried against every directory the application root could
  be, and only a file the unit has is kept;
- roles from `[Authorize(Roles = "...")]` and `@attribute [Authorize]` in the file,
  `<AuthorizeView Roles>`, LoginView role groups, `User.IsInRole` and
  `Roles.IsUserInRole`;
- `SqlDataSource` commands as queries, their tables through SQL lineage, and connection
  strings and app settings named by `<%$ ... %>` or `ConfigurationManager` as ConfigKeys;
- one behaviour unit (`kind: page`): server code blocks and expressions as `platform`
  and `output` nodes holding only the redirects, transfers and role checks read from
  them, and the data source queries. C# is not lowered and never copied.

Routes of MVC views and `asp-action` targets depend on routing configured in other
files: they stay attributes, never guessed into relations.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from repo_intel.platforms.aspnet.razor import matching, razor_regions, webforms_regions
from repo_intel.platforms.aspnet.vocabulary import BLAZOR_INPUTS, HTML_HELPER_CONTROLS, MARKUP, OTHER_CONTROLS, SERVER_CONTROLS, VALIDATORS
from repo_intel.platforms.jsp.pages import (
    HOLE, K, P, Page, Region, UiControl, UiForm, attr_map, button_label, candidates, decode, emit_ui, endpoint_refs, is_external, literal, markup, mask,
    orig_value, relative,
)
from repo_intel.platforms.web.markup import Attr, Element

PLATFORM = "aspnet"
REALM = "aspnet"
_STR = r"\"((?:[^\"\\\n]|\\.)*)\""
_DIRECTIVE_ATTR = re.compile(r"([\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_REDIRECT = re.compile(r"\bResponse\s*\.\s*Redirect(?:Permanent)?\s*\(\s*(?:" + _STR + r"\s*[,)])?")
_TRANSFER = re.compile(r"\bServer\s*\.\s*(Transfer|Execute)\s*\(\s*(?:" + _STR + r"\s*[,)])?")
_NAVIGATE = re.compile(r"\.\s*NavigateTo\s*\(\s*(?:" + _STR + r"\s*[,)])?")
_REDIRECT_PAGE = re.compile(r"\bRedirectToPage\s*\(\s*" + _STR)
_ROLE = re.compile(r"(?:\b(\w+)\s*\.\s*)?\b(IsInRole|IsUserInRole)\s*\(\s*" + _STR + r"\s*\)")
_AUTHORIZE = re.compile(r"\[\s*Authorize(?:Attribute)?\s*(?:\(([^)\]]*)\))?\s*\]")
_ROLES_ARG = re.compile(r"\bRoles\s*=\s*" + _STR)
_CONFIG = re.compile(r"\bConfigurationManager\s*\.\s*(ConnectionStrings|AppSettings)\s*\[\s*" + _STR + r"\s*\]")
_BUILDER = re.compile(r"^\s*(ConnectionStrings|AppSettings|Resources)\s*:\s*([\w.\-]+)")
_TITLE = re.compile(r"\b(?:ViewData\s*\[\s*\"Title\"\s*\]|ViewBag\s*\.\s*Title)\s*=\s*" + _STR)
_LAYOUT = re.compile(r"\bLayout\s*=\s*" + _STR)
_HELPER = re.compile(r"\bHtml\s*\.\s*([A-Z]\w*)\s*\(")
_BEGIN_FORM = re.compile(r"\bHtml\s*\.\s*Begin(?:Route)?Form\s*\(")
_COMPONENT = re.compile(r"\bComponent\s*\.\s*InvokeAsync\s*\(")
_DIRECTIVE_WORD = re.compile(r"^(\w+)\s*(.*)$", re.S)
_PAGE_LIKE = (".aspx", ".cshtml", ".razor")


def _cs(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def _string_args(text: str, open_paren: int) -> tuple[list[str | None], int]:
    """The arguments of the call whose `(` is at `open_paren`: each a literal string, or None."""
    close = matching(text, open_paren, "(", ")")
    if close < 0:
        return [], open_paren + 1
    inner = text[open_paren + 1:close - 1]
    args: list[str | None] = []
    depth = 0
    start = 0
    quote = False
    for i, c in enumerate(inner):
        if quote:
            if c == "\\":
                continue
            if c == '"' and (i == 0 or inner[i - 1] != "\\"):
                quote = False
            continue
        if c == '"':
            quote = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(inner[start:i])
            start = i + 1
    if inner.strip():
        args.append(inner[start:])
    out: list[str | None] = []
    for a in args:
        m = re.fullmatch(r"\s*" + _STR + r"\s*", a)
        out.append(_cs(m.group(1)) if m else None)
    return out, close


class AspNetSource:
    id = "aspnet.source"
    version = "1.0.0"
    SUFFIXES = (".aspx", ".ascx", ".master", ".cshtml", ".razor")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        return _Reader(self.id, path, data, blob, decode(data)).run()


class _Reader:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        self.page = Page(extractor_id, path, data, blob, text)
        self.path, self.text = path, text
        low = path.lower()
        self.ext = posixpath.splitext(low)[1]
        self.razor = self.ext in (".cshtml", ".razor")
        self.program = self.page.base.ref(K.PROGRAM, {"platform": PLATFORM, "name": path}, posixpath.basename(path))
        self.attrs: dict[str, Any] = {}
        self.statements: list[tuple[int, dict[str, Any]]] = []
        self.calls: list[tuple[list[str], int, int, str]] = []
        self.redirects: list[tuple[str | None, int, int, str]] = []
        self.roles: list[tuple[str, int, int, str]] = []
        self.configs: list[tuple[str, int, int]] = []
        self.forms: list[UiForm] = []
        self.controls: list[UiControl] = []
        self.validation: dict[str, list[dict[str, Any]]] = {}
        self.routes: list[str] = []
        self.authorize = False
        self.title: str | None = None
        self.prefixes: dict[str, tuple[str, str | None]] = {}  # tag prefix -> ("control", src) or ("assembly", None)
        self.user_controls: dict[str, str] = {}  # "prefix:name" -> src

    # ------------------------------------------------------------------ helpers

    def at(self, a: int, b: int) -> list[int]:
        return [self.page.to_byte(a), self.page.to_byte(max(a, b))]

    def add(self, pos: int, stmt: dict[str, Any]) -> None:
        self.statements.append((pos, stmt))

    @property
    def pages_root(self) -> str | None:
        p = "/" + self.path
        i = p.rfind("/Pages/")
        return p[1:i + len("/Pages/")] if i >= 0 else None

    def app_paths(self, rel: str) -> list[str]:
        """Repository paths an application-root path may be: one per ancestor directory."""
        return candidates(self.path, rel)

    def targets(self, value: str, methods: tuple[str, ...]) -> list[dict[str, Any]]:
        """What a literal URL reaches: a page's Program (looked up from every possible
        application root), or the Endpoint at an application-relative route."""
        t = value.strip()
        if not t or is_external(t):
            return []
        t = re.split(r"[?#]", t, maxsplit=1)[0]
        if not t:
            return [self.program]
        sql = self.page.sql
        if t.startswith(("~/", "/")):
            rel = t.lstrip("~").lstrip("/")
            if rel.lower().endswith(_PAGE_LIKE):
                return [sql.program(PLATFORM, p, declare=False) for p in self.app_paths(rel)]
            # A Razor Page or MVC route: the Endpoint a page or controller declares at it.
            return endpoint_refs("/" + rel, methods) if rel else []
        resolved = relative(self.path, t)
        if resolved and resolved.lower().endswith(_PAGE_LIKE):
            return [sql.program(PLATFORM, resolved, declare=False)]
        return []

    def razor_page(self, name: str) -> list[dict[str, Any]]:
        """The Razor Page an `asp-page` or `RedirectToPage` name gives."""
        root = self.pages_root
        if root is None or not name.strip():
            return []
        n = name.strip()
        target = posixpath.normpath(root + n.lstrip("/") + ".cshtml") if n.startswith("/") else relative(self.path, n + ".cshtml")
        return [self.page.sql.program(PLATFORM, target, declare=False)] if target else []

    def view_paths(self, name: str) -> list[str]:
        """Where ASP.NET Core looks for a partial or layout named `name`: this file's folder
        and each parent up to the `Pages` or `Views` folder, then `Pages/Shared` and
        `Views/Shared` beside it. Without such a folder, this file's folder and the shared
        folders below each ancestor. A Blazor `@layout` names a class, which only other
        files resolve, so `.razor` files get none."""
        n = name.strip()
        if not n or HOLE in n or self.ext == ".razor":
            return []
        if n.startswith(("~/", "/")):
            return self.app_paths(n.lstrip("~").lstrip("/"))
        if "/" in n or n.lower().endswith(".cshtml"):
            r = relative(self.path, n)
            return [r] if r else []
        file = n + ".cshtml"
        parts = posixpath.dirname(self.path).split("/") if posixpath.dirname(self.path) else []
        top = next((i for i in range(len(parts) - 1, -1, -1) if parts[i] in ("Pages", "Views")), None)
        out: list[str] = []
        if top is None:
            out.append(posixpath.join(*parts, file) if parts else file)
            for shared in ("Views/Shared", "Pages/Shared"):
                out += self.app_paths(f"{shared}/{file}")
        else:
            for depth in range(len(parts), top, -1):
                out.append(posixpath.join(*parts[:depth], file))
            app = parts[:top]
            for shared in ("Pages/Shared", "Views/Shared"):
                out.append(posixpath.join(*app, shared, file) if app else f"{shared}/{file}")
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ code

    def code(self, r: Region) -> list[dict[str, Any]]:
        """Redirects, transfers, navigation and role checks read from C# or VB code; nothing else is kept."""
        text, base = r.code, r.code_start
        out: list[tuple[int, dict[str, Any]]] = []
        for m in _REDIRECT.finditer(text):
            a, b = base + m.start(), base + m.end()
            self.page.construct("aspx:response-redirect", a, b, True)
            target = _cs(m.group(1)) if m.group(1) is not None else None
            self.redirects.append((target, a, b, "Response.Redirect"))
            out.append((a, {"op": "redirect", "at": self.at(a, b), "target": self.lit(target, "aspx:response-redirect")}))
        for m in _TRANSFER.finditer(text):
            a, b = base + m.start(), base + m.end()
            self.page.construct("aspx:server-transfer", a, b, True)
            target = _cs(m.group(2)) if m.group(2) is not None else None
            if m.group(1) == "Transfer":
                self.redirects.append((target, a, b, "Server.Transfer"))
            elif target:
                found = self.targets(target, ("ANY",))
                self.calls.append(([t["natural_key"]["name"] for t in found if t["kind"] == "Program"], a, b, "Server.Execute"))
            out.append((a, {"op": "include", "at": self.at(a, b), "target": self.lit(target, "aspx:server-transfer"), **({"forward": True} if m.group(1) == "Transfer" else {})}))
        for m in _NAVIGATE.finditer(text):
            a, b = base + m.start(), base + m.end()
            self.page.construct("aspx:blazor.navigate-to", a, b, True)
            target = _cs(m.group(1)) if m.group(1) is not None else None
            self.redirects.append((target, a, b, "NavigateTo"))
            out.append((a, {"op": "redirect", "at": self.at(a, b), "target": self.lit(target, "aspx:blazor.navigate-to")}))
        for m in _REDIRECT_PAGE.finditer(text):
            a, b = base + m.start(), base + m.end()
            self.redirects.append(("page:" + _cs(m.group(1)), a, b, "RedirectToPage"))
            out.append((a, {"op": "redirect", "at": self.at(a, b), "target": {"e": "lit", "value": _cs(m.group(1)), "type": "string"}, "page": True}))
        for m in _ROLE.finditer(text):
            a, b = base + m.start(), base + m.end()
            role = _cs(m.group(3))
            self.page.construct("aspx:is-in-role", a, b, True)
            self.roles.append((role, a, b, "IsInRole"))
            method = {"e": "var", "name": m.group(2)}
            callee = {"e": "member", "object": {"e": "var", "name": m.group(1)}, "name": m.group(2)} if m.group(1) else method
            call = {"e": "call", "callee": callee, "args": [{"e": "lit", "value": role, "type": "string"}]}
            out.append((a, {"op": "call", "at": self.at(a, b), "call": call}))
        for m in _AUTHORIZE.finditer(text):
            a, b = base + m.start(), base + m.end()
            args = m.group(1) or ""
            roles = [x.strip() for rm in _ROLES_ARG.finditer(args) for x in _cs(rm.group(1)).split(",") if x.strip()]
            self.authorize = True
            self.page.construct("aspx:authorize", a, b, bool(roles) or not args.strip())
            for role in roles:
                self.roles.append((role, a, b, "Authorize"))
            policy = re.search(r"\bPolicy\s*=\s*" + _STR, args) or re.match(r"\s*" + _STR, args)
            if policy:
                self.attrs.setdefault("policies", set()).add(_cs(policy.group(1)))
        for m in _CONFIG.finditer(text):
            a, b = base + m.start(), base + m.end()
            kind = "connectionstring" if m.group(1) == "ConnectionStrings" else "appsetting"
            self.configs.append((f"{kind}:{_cs(m.group(2))}", a, b))
        for m in _TITLE.finditer(text):
            self.title = self.title or _cs(m.group(1))
        for m in _LAYOUT.finditer(text):
            a, b = base + m.start(), base + m.end()
            self.page.construct("aspx:razor.layout", a, b, True)
            self.attrs["layout"] = _cs(m.group(1))
            self.calls.append((self.view_paths(_cs(m.group(1))), a, b, "layout"))
        if self.razor:
            self.helpers(r)
        return [s for _, s in sorted(out, key=lambda x: x[0])]

    def lit(self, target: str | None, construct: str) -> dict[str, Any]:
        if target is None:
            return {"e": "platform", "construct": construct, "dynamic": True}
        return {"e": "lit", "value": target, "type": "string"}

    def helpers(self, r: Region) -> None:
        """HTML helpers in Razor code: forms, controls, links, partials."""
        text, base = r.code, r.code_start
        for m in _BEGIN_FORM.finditer(text):
            a, b = base + m.start(), base + m.end()
            args, close = _string_args(text, m.end() - 1)
            attrs: dict[str, Any] = {"kind": "html_helper"}
            if len(args) > 0 and args[0]:
                attrs["action"] = args[0]
            if len(args) > 1 and args[1]:
                attrs["controller"] = args[1]
            tail = text[m.end():close]
            attrs["method"] = "get" if "FormMethod.Get" in tail else "post"
            self.page.note("a form posts to an MVC action whose route is configured elsewhere: not followed")
            # The form holds what is inside the `@using (...) { }` block it opens.
            brace = self.text.find("{", base + close)
            extent_end = matching(self.text, brace, "{", "}") if r.kind == "flow" and brace >= 0 and brace < r.end + 2 else -1
            extent = (r.end, extent_end if extent_end > 0 else r.end)
            self.forms.append(UiForm(a, b, extent, "aspx:html-helper.form", [None, None], False, attrs, []))
        for m in _HELPER.finditer(text):
            name = m.group(1)
            a, b = base + m.start(), base + m.end()
            if name in ("BeginForm", "BeginRouteForm", "EndForm"):
                continue
            if name in HTML_HELPER_CONTROLS:
                close = matching(text, m.end() - 1, "(", ")")
                inner = text[m.end():close - 1 if close > 0 else len(text)]
                lam = re.match(r"\s*(\w+)\s*=>\s*\1\s*\.\s*([\w.]+)", inner)
                args, _ = _string_args(text, m.end() - 1)
                binding = lam.group(2) if lam else None
                cname = None if lam else (args[0] if args else None)
                self.controls.append(UiControl(a, base + (close if close > 0 else m.end()), "aspx:html-helper.control", HTML_HELPER_CONTROLS[name], [cname, None, binding],
                                               False, None, None, None, {"tag": f"Html.{name}", **({"binding": binding} if binding else {})}))
            elif name in ("ActionLink", "RouteLink"):
                args, close = _string_args(text, m.end() - 1)
                attrs = {"tag": f"Html.{name}"}
                if len(args) > 1 and args[1] and name == "ActionLink":
                    attrs["action"] = args[1]
                if len(args) > 2 and args[2] and name == "ActionLink":
                    attrs["controller"] = args[2]
                self.controls.append(UiControl(a, base + close, "aspx:html-helper.link", "link", [None, None, None], False, None, None, None, attrs))
            elif name in ("Partial", "PartialAsync", "RenderPartial", "RenderPartialAsync"):
                args, _ = _string_args(text, m.end() - 1)
                found = self.view_paths(args[0]) if args and args[0] else []
                self.page.construct("aspx:html-helper.partial", a, b, bool(found))
                if found:
                    self.calls.append((found, a, b, "partial"))
                    self.add(a, {"op": "include", "at": self.at(a, b), "target": {"e": "lit", "value": args[0], "type": "string"}})
            elif name.startswith("Validation"):
                self.page.construct("aspx:html-helper.validation", a, b, False)
            else:
                self.page.construct("aspx:html-helper.other", a, b, False)
        for m in _COMPONENT.finditer(text):
            self.page.construct("aspx:view-component", base + m.start(), base + m.end(), False)

    # ------------------------------------------------------------------ directives

    def webforms_directive(self, r: Region) -> None:
        m = re.match(r"\s*(\w+)", r.code)
        if not m:
            return
        name = m.group(1).lower()
        attrs = {k.lower(): v[1:-1] for k, v in _DIRECTIVE_ATTR.findall(r.code)}
        cid = f"aspx:directive.{name}"
        known = name in ("page", "control", "master", "register", "import", "mastertype", "previouspagetype", "outputcache", "assembly", "reference", "implements")
        if not known:
            return
        self.page.construct(cid, r.start, r.end, name in ("page", "control", "master", "register", "import"))
        if name in ("page", "control", "master"):
            lang = (attrs.get("language") or "").lower()
            if lang:
                self.attrs["language"] = {"c#": "csharp", "cs": "csharp", "csharp": "csharp", "vb": "vb", "vbnet": "vb"}.get(lang, lang)
            if attrs.get("inherits") and re.fullmatch(r"[\w.`]+", attrs["inherits"]):
                self.attrs["inherits"] = attrs["inherits"]
            behind = attrs.get("codebehind") or attrs.get("codefile")
            if behind and not is_external(behind):
                resolved = relative(self.path, behind.lstrip("~/")) if not behind.startswith("~/") else None
                if behind.startswith("~/"):
                    self.attrs["code_behind"] = behind
                    self.code_behind = self.app_paths(behind[2:])
                elif resolved:
                    self.attrs["code_behind"] = resolved
                    self.code_behind = [resolved]
            if attrs.get("masterpagefile"):
                master = attrs["masterpagefile"]
                self.attrs["master"] = master
                found = self.app_paths(master[2:]) if master.startswith("~/") else [p for p in [relative(self.path, master)] if p]
                self.calls.append((found, r.start, r.end, "MasterPageFile"))
            if attrs.get("title") and HOLE not in attrs["title"]:
                self.title = attrs["title"]
        elif name == "register":
            prefix = attrs.get("tagprefix")
            if prefix and attrs.get("tagname") and attrs.get("src"):
                self.user_controls[f"{prefix.lower()}:{attrs['tagname'].lower()}"] = attrs["src"]
            elif prefix:
                self.prefixes[prefix.lower()] = ("assembly", attrs.get("namespace"))
        elif name == "import" and attrs.get("namespace"):
            self.attrs.setdefault("imports", set()).add(attrs["namespace"])

    def razor_directive(self, r: Region) -> None:
        m = _DIRECTIVE_WORD.match(r.code.strip())
        if not m:
            return
        word, rest = m.group(1), m.group(2).strip()
        a, b = r.start, r.end
        if word == "page":
            self.page.construct("aspx:razor.page", a, b, True)
            route = re.match(_STR, rest)
            self.routes.append(_cs(route.group(1)) if route else "")
        elif word == "model":
            self.page.construct("aspx:razor.model", a, b, True)
            if re.fullmatch(r"[\w.<>, \[\]?]+", rest):
                self.attrs["model"] = rest
        elif word == "inject":
            self.page.construct("aspx:razor.inject", a, b, True)
            parts = rest.split()
            if parts and re.fullmatch(r"[\w.<>,?]+", parts[0]):
                self.attrs.setdefault("injects", set()).add(parts[0])
        elif word == "inherits":
            self.page.construct("aspx:razor.inherits", a, b, True)
            if re.fullmatch(r"[\w.<>, ]+", rest):
                self.attrs["inherits"] = rest
        elif word == "using":
            self.page.construct("aspx:razor.using", a, b, False)
        elif word == "layout":
            found = self.view_paths(rest)
            self.page.construct("aspx:razor.layout", a, b, bool(found))
            if re.fullmatch(r"[\w.]+", rest):
                self.attrs["layout"] = rest
            self.calls.append((found, a, b, "layout"))
        elif word == "attribute":
            self.page.construct("aspx:razor.attribute", a, b, True)
            self.add(a, {"op": "platform", "at": self.at(a, b), "construct": "aspx:razor.attribute", "body": self.code(r)})
        else:
            self.page.construct("aspx:razor.other-directive", a, b, False)

    # ------------------------------------------------------------------ regions as statements

    def region(self, r: Region) -> None:
        a, b = r.start, r.end
        at = self.at(a, b)
        if r.kind == "directive":
            if self.razor:
                self.razor_directive(r)
            else:
                self.webforms_directive(r)
        elif r.kind in ("code", "code-block", "functions", "flow"):
            cid = {"code": "aspx:razor.code-block", "code-block": "aspx:code-block", "functions": "aspx:razor.functions", "flow": "aspx:razor.control-flow"}[r.kind]
            self.page.construct(cid, a, b, True)
            self.add(a, {"op": "platform", "at": at, "construct": cid, "body": self.code(r)})
        elif r.kind in ("expr", "code-expression", "encoded-expression", "databinding"):
            cid = {"expr": "aspx:razor.expression", "code-expression": "aspx:code-expression", "encoded-expression": "aspx:encoded-expression", "databinding": "aspx:databinding"}[r.kind]
            self.page.construct(cid, a, b, True)
            body = self.code(r)
            self.add(a, {"op": "output", "at": at, "channel": "response", "value": {"e": "platform", "construct": cid}})
            for s in body:
                self.add(a + 1, s)
        elif r.kind == "expression-builder":
            m = _BUILDER.match(r.code)
            modelled = bool(m) and m.group(1) != "Resources"
            self.page.construct("aspx:expression-builder", a, b, modelled)
            if modelled:
                kind = "connectionstring" if m.group(1) == "ConnectionStrings" else "appsetting"
                self.configs.append((f"{kind}:{m.group(2)}", a, b))
        elif r.kind == "section":
            self.page.construct("aspx:razor.section", a, b, False)

    # ------------------------------------------------------------------ markup

    def original_tag(self, el: Element) -> str:
        return self.text[el.start + 1:el.start + 1 + len(el.tag)]

    def element(self, el: Element) -> None:
        tag = el.tag
        attrs = attr_map(el)
        page = self.page
        if self.razor:
            for name in attrs:
                if name.startswith("@on"):
                    page.construct("aspx:blazor.event", attrs[name].start, attrs[name].end, False)
                if name in ("asp-validation-for", "asp-validation-summary"):
                    page.construct("aspx:tag-helper.validation", attrs[name].start, attrs[name].end, False)
        if tag == "script" and (literal(attrs.get("runat")) or "").lower() == "server" and el.raw is not None:
            page.construct("aspx:server-script", el.start, el.end, True)
            r = Region("server-script", el.start, el.close, el.raw, el.raw_start)
            self.add(el.start, {"op": "platform", "at": self.at(el.start, el.close), "construct": "aspx:server-script", "body": self.code(r)})
            return
        if tag == "form":
            self.form(el, attrs)
            return
        if tag in ("input", "select", "textarea", "button", "a"):
            self.html_control(el, attrs)
            return
        if tag.startswith("asp:"):
            self.server_control(el, attrs, tag[4:])
            return
        if ":" in tag:
            prefix = tag.split(":", 1)[0]
            if tag in self.user_controls:
                src = self.user_controls[tag]
                found = self.app_paths(src[2:]) if src.startswith("~/") else [p for p in [relative(self.path, src)] if p]
                page.construct("aspx:user-control", el.start, el.end, bool(found))
                self.calls.append((found, el.start, el.end, "user-control"))
                self.add(el.start, {"op": "include", "at": self.at(el.start, el.close), "target": {"e": "lit", "value": src, "type": "string"}, "module": True})
            elif prefix in self.prefixes:
                page.construct("aspx:third-party-control", el.start, el.end, False)
            elif prefix == "vc":
                page.construct("aspx:view-component", el.start, el.end, False)
            return
        if not self.razor:
            return
        if tag == "partial":
            name = literal(attrs.get("name"))
            found = self.view_paths(name) if name else []
            page.construct("aspx:tag-helper.partial", el.start, el.end, bool(found))
            if found:
                self.calls.append((found, el.start, el.end, "partial"))
                self.add(el.start, {"op": "include", "at": self.at(el.start, el.close), "target": {"e": "lit", "value": name, "type": "string"}})
            return
        if tag == "editform":
            names = [None, literal(attrs.get("id"))]
            fattrs = {"kind": "blazor", "model": literal(attrs.get("model")), "handler": literal(attrs.get("onvalidsubmit")) or literal(attrs.get("onsubmit"))}
            self.forms.append(UiForm(el.start, el.end, (el.end, el.close), "aspx:blazor.edit-form", names, False, fattrs, []))
            return
        if tag in BLAZOR_INPUTS:
            bind = attrs.get("@bind-value") or attrs.get("@bind")
            binding = None
            if bind is not None:
                raw = orig_value(self.text, bind) or ""
                binding = raw.lstrip("@").strip() or None
                if binding and not re.fullmatch(r"[\w.\[\]]+", binding):
                    binding = None
            ctype = BLAZOR_INPUTS[tag]
            self.controls.append(UiControl(el.start, el.end, "aspx:blazor.input", ctype, [literal(attrs.get("name")), literal(attrs.get("id")), binding], False,
                                           None, None, None, {"tag": self.original_tag(el), **({"binding": binding} if binding else {})}))
            return
        if tag == "navlink":
            href = literal(attrs.get("href"))
            navigates = [(t, {"via": "link"}) for t in (self.targets(href, ("GET", "ANY")) if href is not None else [])]
            if href is not None and href and not href.startswith(("/", "~", "#")) and not is_external(href):
                navigates += [(t, {"via": "link"}) for t in self.targets("/" + href, ("GET", "ANY"))]  # NavLink hrefs are relative to the app base
            self.controls.append(UiControl(el.start, el.end, "aspx:blazor.navlink", "link", [None, literal(attrs.get("id")), None], False, href, None, None, {"tag": "NavLink"}, navigates))
            return
        if tag == "authorizeview":
            roles = literal(attrs.get("roles"))
            page.construct("aspx:authorize-view", el.start, el.end, bool(roles))
            for role in (x.strip() for x in (roles or "").split(",") if x.strip()):
                self.roles.append((role, el.start, el.end, "AuthorizeView"))
            return
        original = self.original_tag(el)
        if self.ext == ".razor" and original[:1].isupper():
            page.construct("aspx:blazor.component", el.start, el.end, False)

    def form(self, el: Element, attrs: dict[str, Attr]) -> None:
        server = (literal(attrs.get("runat")) or "").lower() == "server"
        method = (literal(attrs.get("method")) or ("post" if server else "get")).lower()
        fattrs: dict[str, Any] = {"kind": "server" if server else "html", "method": method}
        submits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        helper = [k for k in ("asp-page", "asp-page-handler", "asp-action", "asp-controller", "asp-route") if k in attrs]
        construct = "aspx:server-form" if server else ("aspx:tag-helper.form" if helper else MARKUP["form"])
        action = attrs.get("action")
        methods = (method.upper(), "ANY") if method.upper() in ("GET", "POST") else ("ANY",)
        if helper:
            fattrs["kind"] = "tag_helper"
            for k in helper:
                v = literal(attrs.get(k))
                if v:
                    fattrs[k.replace("asp-", "").replace("-", "_")] = v
            if "asp-page" in attrs and literal(attrs.get("asp-page")):
                submits += [(t, {"via": "asp-page"}) for t in self.razor_page(literal(attrs.get("asp-page")))]
            elif "asp-page-handler" in attrs and "asp-action" not in attrs:
                submits.append((self.program, {"implicit": True, **({"handler": literal(attrs.get("asp-page-handler"))} if literal(attrs.get("asp-page-handler")) else {})}))
            else:
                self.page.note("a form posts to an MVC action whose route is configured elsewhere: not followed")
        elif action is None or (action.value is not None and not action.value.strip()):
            submits.append((self.program, {"implicit": True, **({"via": "postback"} if server else {})}))
        else:
            target = literal(action)
            if target is not None:
                submits += [(t, {}) for t in self.targets(target, methods)]
            else:
                self.page.note("form posts to a computed action: target not resolved")
        names = [literal(attrs.get("name")), literal(attrs.get("id"))]
        self.forms.append(UiForm(el.start, el.end, (el.end, el.close), construct, names, attrs.get("name") is not None and names[0] is None, fattrs, submits))

    def html_control(self, el: Element, attrs: dict[str, Attr]) -> None:
        tag = el.tag
        link = None
        navigates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        button = label = None
        construct = MARKUP[tag]
        cattrs: dict[str, Any] = {"tag": tag}
        if (literal(attrs.get("runat")) or "").lower() == "server":
            cattrs["server"] = True
        binding = literal(attrs.get("asp-for"))
        if binding:
            cattrs["binding"] = binding
            self.page.construct("aspx:tag-helper.for", attrs["asp-for"].start, attrs["asp-for"].end, True)
        if tag == "a":
            href = attrs.get("href")
            page_name = literal(attrs.get("asp-page"))
            if href is None and page_name is None and "asp-action" not in attrs and "asp-controller" not in attrs:
                return
            ctype = "link"
            if href is not None:
                link = literal(href)
                if link is not None:
                    navigates = [(t, {"via": "link"}) for t in self.targets(link, ("GET", "ANY"))]
                elif HOLE in (href.value or ""):
                    head = (href.value or "").split(HOLE, 1)[0]
                    if re.search(r"[?#]", head):
                        link = re.split(r"[?#]", head, maxsplit=1)[0]
                        navigates = [(t, {"via": "link"}) for t in self.targets(link, ("GET", "ANY"))]
                    else:
                        self.page.note("link to a computed URL: target not resolved")
            else:
                construct = "aspx:tag-helper.link"
                if page_name:
                    link = page_name
                    navigates = [(t, {"via": "asp-page"}) for t in self.razor_page(page_name)]
                for k in ("asp-action", "asp-controller"):
                    if literal(attrs.get(k)):
                        cattrs[k.replace("asp-", "")] = literal(attrs.get(k))
        elif tag == "input":
            ctype = (literal(attrs.get("type")) or "text").lower()
            if ctype in ("submit", "button", "reset", "image"):
                button, label = ("submit" if ctype == "submit" else "button"), button_label(el)
        elif tag == "button":
            ctype = "button"
            button = "submit" if (literal(attrs.get("type")) or "").lower() == "submit" else "button"
            label = button_label(el)
        else:
            ctype = tag
        if attrs.get("required") is not None:
            cattrs["required"] = True
        for key in ("pattern", "maxlength", "min", "max"):
            if literal(attrs.get(key)):
                cattrs[key] = literal(attrs.get(key))
        names = [literal(attrs.get("name")), literal(attrs.get("id")), binding]
        self.controls.append(UiControl(el.start, el.end, construct, ctype, names, attrs.get("name") is not None and names[0] is None, link, button, label, cattrs, navigates,
                                       key=literal(attrs.get("id"))))

    def server_control(self, el: Element, attrs: dict[str, Attr], local: str) -> None:
        page = self.page
        cid = literal(attrs.get("id"))
        if local in SERVER_CONTROLS:
            ctype, construct = SERVER_CONTROLS[local]
            if local == "textbox":
                mode = (literal(attrs.get("textmode")) or "").lower()
                ctype = {"multiline": "textarea", "password": "password", "singleline": "text", "": "text"}.get(mode, mode)
            cattrs: dict[str, Any] = {"tag": "asp:" + self.original_tag(el)[4:], "server": True}
            for key in ("maxlength", "datasourceid", "onclick", "ontextchanged", "onselectedindexchanged", "autopostback"):
                v = literal(attrs.get(key))
                if v and re.fullmatch(r"[\w.]+", v):
                    cattrs[{"datasourceid": "data_source", "maxlength": "maxlength", "autopostback": "auto_postback"}.get(key, "handler")] = v
            button = label = link = None
            navigates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            form_submits: list[tuple[dict[str, Any], dict[str, Any]]] = []
            if construct == "aspx:asp.button":
                button = "submit" if local == "button" and (literal(attrs.get("usesubmitbehavior")) or "true").lower() != "false" else "button"
                label = literal(attrs.get("text"))
                pb = attrs.get("postbackurl")
                if pb is not None:
                    target = literal(pb)
                    page.construct("aspx:post-back-url", pb.start, pb.end, target is not None)
                    if target is not None:
                        form_submits = [(t, {"via": "PostBackUrl"}) for t in self.targets(target, ("POST", "ANY"))]
                    else:
                        page.note("PostBackUrl to a computed URL: target not resolved")
            if local == "hyperlink":
                nav = attrs.get("navigateurl")
                link = literal(nav)
                label = literal(attrs.get("text"))
                if link is not None:
                    navigates = [(t, {"via": "NavigateUrl"}) for t in self.targets(link, ("GET", "ANY"))]
                elif nav is not None:
                    page.note("hyperlink to a computed URL: target not resolved")
            self.controls.append(UiControl(el.start, el.end, construct, ctype, [literal(attrs.get("name")), cid, None], False, link, button, label, cattrs, navigates,
                                           form_submits, key=cid))
            return
        if local in VALIDATORS:
            target = literal(attrs.get("controltovalidate"))
            page.construct("aspx:asp.validator", el.start, el.end, bool(target))
            if target:
                rule: dict[str, Any] = {"kind": VALIDATORS[local]}
                for key, out in (("validationexpression", "pattern"), ("minimumvalue", "min"), ("maximumvalue", "max"), ("type", "type"), ("operator", "operator"), ("controltocompare", "compare_to")):
                    v = literal(attrs.get(key))
                    if v:
                        rule[out] = v
                self.validation.setdefault(target, []).append(rule)
            return
        if local == "sqldatasource":
            self.sql_data_source(el, attrs)
            return
        if local == "loginview":
            roles = []
            for child in el.children:
                for g in [child, *child.children]:
                    if g.tag == "asp:rolegroup":
                        roles += [x.strip() for x in (literal(attr_map(g).get("roles")) or "").split(",") if x.strip()]
            page.construct("aspx:asp.login-view", el.start, el.end, bool(roles))
            for role in roles:
                self.roles.append((role, el.start, el.end, "LoginView"))
            return
        if local in ("rolegroup", "rolegroups", "anonymoustemplate", "loggedintemplate", "listitem", "parameter", "controlparameter", "querystringparameter",
                     "sessionparameter", "formparameter", "selectparameters", "insertparameters", "updateparameters", "deleteparameters", "columns",
                     "boundfield", "templatefield", "itemtemplate", "edititemtemplate", "headertemplate", "footertemplate", "commandfield", "hyperlinkfield", "buttonfield",
                     "checkboxfield", "imagefield", "contenttemplate", "triggers", "asyncpostbacktrigger", "postbacktrigger", "layouttemplate", "emptydatatemplate",
                     "alternatingitemtemplate", "separatortemplate", "pagersettings", "rowstyle", "headerstyle", "alternatingrowstyle", "footerstyle", "pagerstyle"):
            return  # parts of another control, counted with it
        page.construct(OTHER_CONTROLS.get(local, "aspx:asp.other"), el.start, el.end, False)

    def sql_data_source(self, el: Element, attrs: dict[str, Attr]) -> None:
        page = self.page
        ds_name = None
        conn = attrs.get("connectionstring")
        if conn is not None and conn.value and HOLE in conn.value:
            raw = orig_value(self.text, conn) or ""
            m = re.search(r"<%\$\s*ConnectionStrings\s*:\s*([\w.\-]+)", raw)
            if m:
                ds_name = m.group(1)
        elif conn is not None:
            page.note("SqlDataSource with an inline connection string: not read")
        stored = (literal(attrs.get("selectcommandtype")) or "").lower() == "storedprocedure"
        found = False
        for key in ("selectcommand", "insertcommand", "updatecommand", "deletecommand"):
            a = attrs.get(key)
            sql = literal(a)
            if not sql:
                continue
            found = True
            raw = orig_value(self.text, a) or ""
            start = self.text.find(raw, a.start, a.end)
            at = self.at(a.start, a.end)
            params = [{"name": p} for p in dict.fromkeys(re.findall(r"@(\w+)", sql))]
            if stored and key == "selectcommand":
                stmt: dict[str, Any] = {"op": "query", "at": at, "sql": f"EXEC {sql}", "procedure": sql, "params": params}
            else:
                stmt = {"op": "query", "at": at, "sql": re.sub(r"\s+", " ", re.sub(r"@(\w+)", r":\1", sql)).strip(), "params": params}
                if start >= 0:
                    page.query_lineage(self.program, re.sub(r"@(\w+)", lambda m: "x" + "_" * len(m.group(1)), raw), start, [])
            if ds_name:
                stmt["datasource"] = ds_name
            sid = literal(attrs.get("id"))
            if sid:
                stmt["target"] = {"e": "var", "name": sid}
            self.add(a.start, stmt)
        page.construct("aspx:asp.sql-data-source", el.start, el.end, found)
        if ds_name:
            self.configs.append((f"connectionstring:{ds_name}", el.start, el.end))

    # ------------------------------------------------------------------ the file

    def kind(self) -> str:
        base = posixpath.basename(self.path).lower()
        if self.ext == ".aspx":
            return "page"
        if self.ext == ".ascx":
            return "user_control"
        if self.ext == ".master":
            return "master"
        if self.ext == ".razor":
            return "component"
        if base == "_viewstart.cshtml":
            return "view_start"
        if base == "_viewimports.cshtml":
            return "view_imports"
        if self.routes:
            return "page"
        if base.startswith("_layout"):
            return "layout"
        if base.startswith("_"):
            return "partial"
        return "view"

    def route(self) -> str | None:
        """The route ASP.NET Core gives a Razor Page."""
        if not self.routes or self.ext != ".cshtml":
            return None
        template = self.routes[0].strip()
        if template.startswith(("/", "~/")):
            return "/" + template.lstrip("~").lstrip("/")
        root = self.pages_root
        if root is None:
            self.page.note("a Razor Page outside a Pages folder with a relative route: route not computed")
            return None
        rel = posixpath.splitext(self.path[len(root):])[0]
        parts = rel.split("/")
        if parts and parts[-1].lower() == "index":
            parts = parts[:-1]
        base = "/" + "/".join(parts)
        if template:
            base = base.rstrip("/") + "/" + template
        return base

    def run(self):
        page = self.page
        regions = razor_regions(self.text) if self.razor else webforms_regions(self.text)
        elements = markup(mask(self.text, regions))
        self.code_behind: list[str] = []
        for r in regions:
            self.region(r)
        for el in elements:
            self.element(el)
        kind = self.kind()
        term = {"page": "aspx:Page", "user_control": "aspx:UserControl", "master": "aspx:MasterPage", "component": "aspx:RazorComponent"}.get(kind, "aspx:RazorFile")
        if self.razor:
            term = "aspx:RazorComponent" if self.ext == ".razor" else "aspx:RazorFile"
        attrs: dict[str, Any] = {"kind": kind, "syntax": "razor" if self.razor else "webforms", "language": self.attrs.pop("language", "csharp")}
        for k, v in self.attrs.items():
            attrs[k] = sorted(v) if isinstance(v, set) else v
        if self.authorize and not any(via == "Authorize" for _, _, _, via in self.roles):
            attrs["requires_authentication"] = True
        prog = page.term(page.whole(), term, self.program["natural_key"], self.program["name"], attrs)
        for path in self.code_behind:
            artifact = page.base.ref(K.ARTIFACT, {"path": path}, posixpath.basename(path), declare=False)
            page.rel(page.whole(), prog, P.DEPENDS_ON, artifact, {"via": "code_behind"})

        # The URL it answers.
        endpoints = []
        route = "/" + self.path if self.ext == ".aspx" else self.route()
        if self.ext == ".razor":  # a routable component: each @page template is its whole route
            served = [r for r in self.routes if r.startswith("/")]
            if len(served) < len(self.routes):
                self.page.note("a component route that does not start with '/': not served")
        else:
            served = [route] if route else []
        framework = {".aspx": "webforms", ".cshtml": "razor_pages", ".razor": "blazor"}.get(self.ext, "razor")
        for r in served:
            key = {"component_root": page.base.UNIT, "protocol": "http", "method": "ANY", "route": r}
            endpoints.append(page.term(page.whole(), "aspx:PageRequest", key, f"ANY {r}", {"framework": framework}))
            page.rel(page.whole(), prog, P.EXPOSES, endpoints[-1])

        # The screen it renders.
        has_ui = bool(self.forms or self.controls)
        renders = kind in ("page", "view") or (kind == "component" and bool(self.routes))
        screen = None
        if renders or has_ui:
            sattrs: dict[str, Any] = {"title": self.title, "route": route if self.ext == ".cshtml" else None, "routes": self.routes if self.ext == ".razor" else None,
                                      "fragment": True if not renders else None}
            screen = page.term(page.whole(), "aspx:RazorView" if self.razor else "aspx:WebForm", {"platform": PLATFORM, "name": self.path}, self.path, sattrs)
            page.rel(page.whole(), prog, P.RENDERS, screen)
            for c in self.controls:
                if c.construct.startswith("aspx:asp."):
                    c.term = "aspx:ServerControl"
            emit_ui(page, screen, self.path, self.forms, self.controls, "aspx:Form", "aspx:Control", self.validation)
        else:
            for f in self.forms:
                page.construct(f.construct, f.start, f.end, False)
            for c in self.controls:
                page.construct(c.construct, c.start, c.end, False)
        if not self.razor:
            for target, _ in self.validation.items():
                if not any(c.key == target for c in self.controls):
                    page.note(f"a validator names control {target}, which this file does not declare")

        # Master pages, layouts, partials, user controls and transfers.
        for found, a, b, via in self.calls:
            for path in found:
                page.rel(page.span(a, b), prog, P.CALLS, page.sql.program(PLATFORM, path, declare=False), {"via": via})
        for target, a, b, via in self.redirects:
            if target is None:
                page.note(f"{via} to a computed URL: target not resolved")
                continue
            refs = self.razor_page(target[5:]) if target.startswith("page:") else self.targets(target, ("GET", "ANY"))
            subject = screen if screen is not None else prog
            for ref in refs:
                if ref != self.program:
                    page.rel(page.span(a, b), subject, P.NAVIGATES_TO, ref, {"via": via})

        # Roles.
        for role, a, b, via in self.roles:
            at = page.span(a, b)
            r = page.term(at, "aspx:Role", {"realm": REALM, "name": role}, role, {"source": via})
            page.rel(at, prog, P.REQUIRES_ROLE, r, {"via": via})
            if screen is not None:
                page.rel(at, screen, P.REQUIRES_ROLE, r, {"via": via})
            if via == "Authorize":
                for endpoint in endpoints:
                    page.rel(at, endpoint, P.REQUIRES_ROLE, r, {"via": via})

        # Connection strings and app settings by name.
        for name, a, b in self.configs:
            at = page.span(a, b)
            term_name = "aspx:ConnectionString" if name.startswith("connectionstring:") else "aspx:AppSetting"
            c = page.term(at, term_name, {"name": name}, name, {"source": "page"})
            page.rel(at, prog, P.REFERENCES_CONFIG, c)

        body = [s for _, s in sorted(self.statements, key=lambda x: x[0])]
        page.behavior(page.whole(), self.program, "razor" if self.razor else "webforms", body, "page")
        return page.done()
