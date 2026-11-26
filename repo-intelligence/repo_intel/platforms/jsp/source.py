"""`jsp.source`: JSP pages, JSP documents, fragments and tag files.

Reads `.jsp`, `.jspx` and `.jspf` files, and `.tag` and `.tagx` files below a
`WEB-INF/tags/` or `META-INF/tags/` directory (elsewhere that suffix is usually another
tool's, such as a Riot.js component):

- the file is a `Program` (`jsp:Page`, or `jsp:TagFile`) named by its path; a page or
  document outside `WEB-INF` and `META-INF` is reachable at its path below the web
  root and EXPOSES a `jsp:PageRequest` Endpoint. The web root is the directory
  holding `WEB-INF`, or `src/main/webapp/`, `WebContent/` or `WebRoot/` by the usual
  project layouts, else the repository root;
- a page that renders markup is a `Screen` (`jsp:View`) its Program RENDERS, named by
  its path; a fragment or tag file is one only when it holds forms or links. Forms
  (`<form>`, Struts `<html:form>`, Spring `<form:form>`) and controls are named by the
  contract's UIControl rule; Struts `property` and Spring `path` are binding paths;
- forms SUBMIT_TO, and links and redirects NAVIGATE_TO, the page or servlet a literal
  URL names (declared by another file, so kept only when the unit has it);
  `<%@ include %>`, `<jsp:include>`, `<c:import>` of a literal page and custom tags
  from a tag directory CALL the Program they name;
- `request.isUserInRole`, Spring Security `<sec:authorize>` and Struts
  `<logic:present role=...>` give Roles the Program and its Screen REQUIRE;
- JSTL `sql:` tags give queries with their tables through SQL lineage and the
  data source they name as a ConfigKey;
- one behaviour unit (`kind: page`) per file: the JSTL core, fmt and sql tags, the
  standard actions, Struts logic and bean tags and EL expressions lowered to the
  behavioural IR; Java scriptlets are `platform` nodes holding only the redirects,
  forwards and role checks read from them. No Java is copied.

Dynamic targets (an EL expression or scriptlet in a URL) stay dynamic, with a
diagnostic. Query values, passwords and connection details are never copied.
"""

from __future__ import annotations

import posixpath
import re
from bisect import bisect_left
from typing import Any

from repo_intel.platforms.jsp import el as EL
from repo_intel.platforms.jsp.pages import (
    HOLE, K, P, Page, Region, UiControl, UiForm, attr_map, button_label, decode, emit_ui, endpoint_refs, is_external, literal, markup, mask, orig_value, relative,
    split_target,
)
from repo_intel.platforms.jsp.vocabulary import CONVENTIONAL_PREFIXES, LIBRARY_URIS, MARKUP, TAGS, WHOLE_LIBRARIES
from repo_intel.platforms.web.markup import Attr, Element

PLATFORM = "jsp"
LANGUAGE = "jsp"
_ROOT_MARKERS = ("src/main/webapp/", "WebContent/", "WebRoot/", "web-app/", "webapp/")
_SERVER = re.compile(r"<%--|<%|[$#]\{|<jsp:(scriptlet|expression|declaration)\b", re.I)
_NESTED_TAG = re.compile(r"=\s*([\"'])(<[A-Za-z][\w-]*:[A-Za-z][\w.-]*\b[^<>]*?/>)\1")
_DIRECTIVE = re.compile(r"^\s*([\w.]+)")
_LOGIC_CONDITIONS = frozenset({
    "equal", "notEqual", "greaterThan", "lessThan", "greaterEqual", "lessEqual", "present", "notPresent", "empty", "notEmpty",
    "match", "notMatch", "messagesPresent", "messagesNotPresent",
})
_DIRECTIVE_ATTR = re.compile(r"([\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_JAVA_STR = r"\"((?:[^\"\\\n]|\\.)*)\""
_REDIRECT = re.compile(r"\bresponse\s*\.\s*sendRedirect\s*\(\s*(?:" + _JAVA_STR + r"\s*\))?")
_DISPATCH = re.compile(r"\bgetRequestDispatcher\s*\(\s*(?:" + _JAVA_STR + r"\s*)?\)\s*\.\s*(forward|include)\b|\bpageContext\s*\.\s*(forward|include)\s*\(\s*(?:" + _JAVA_STR + r")?")
_ROLE = re.compile(r"\bisUserInRole\s*\(\s*" + _JAVA_STR + r"\s*\)")
_URL_TAG = re.compile(r"^<(?:c|spring):url\b[^>]*?\bvalue\s*=\s*([\"'])([^\"'<>$#%]*)\1[^>]*/>$", re.I)
_SEC_ROLE = re.compile(r"has(?:Any)?(?:Role|Authority)\s*\(([^)]*)\)")
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_MAX_STATEMENTS = 20000


def webroot(path: str) -> str:
    """The web application's root directory, with a trailing slash ("" for the repository root)."""
    p = "/" + path
    i = p.find("/WEB-INF/")
    if i >= 0:
        return p[1:i + 1]
    for marker in _ROOT_MARKERS:
        j = p.find("/" + marker)
        if j >= 0:
            return p[1:j + 1 + len(marker)]
    return ""


def url_of(path: str) -> str | None:
    root = webroot(path)
    rel = path[len(root):] if path.startswith(root) else path
    if rel.upper().startswith(("WEB-INF/", "META-INF/")) or "/WEB-INF/" in "/" + rel:
        return None
    return "/" + rel


def _java_string(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


class JspSource:
    id = "jsp.source"
    version = "1.1.0"
    SUFFIXES = (".jsp", ".jspx", ".jspf", ".tag", ".tagx")
    TAG_SUFFIXES = (".tag", ".tagx")
    TAG_DIRECTORIES = ("/web-inf/tags/", "/meta-inf/tags/")

    def matches(self, path: str) -> bool:
        """JSP pages, documents and fragments anywhere; tag files only where the JSP
        specification puts them, below a `WEB-INF/tags/` or `META-INF/tags/` directory,
        because other tools use the same suffix (Riot.js components are `.tag` files)."""
        low = path.lower()
        if low.endswith(self.TAG_SUFFIXES):
            return any(d in "/" + low for d in self.TAG_DIRECTORIES)
        return low.endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        return _Reader(self.id, path, data, blob, decode(data)).run()


class _Reader:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        self.page = Page(extractor_id, path, data, blob, text)
        self.path, self.text = path, text
        low = path.lower()
        self.kind = "tag" if low.endswith((".tag", ".tagx")) else "fragment" if low.endswith(".jspf") else "document" if low.endswith(".jspx") else "page"
        self.root = webroot(path)
        self.program = self.page.base.ref(K.PROGRAM, {"platform": PLATFORM, "name": path}, posixpath.basename(path))
        self.regions: list[Region] = []
        self.used: set[int] = set()
        self.prefixes: dict[str, str] = {}  # prefix -> library ("c", "html", "tagdir:<dir>", "tld")
        self.assumed: set[str] = set()
        self.directives: dict[str, Any] = {}
        self.calls: list[tuple[str, int, int]] = []  # (repository path of a Program, start, end)
        self.redirects: list[tuple[str | None, int, int, str]] = []  # (literal target or None, start, end, via)
        self.roles: list[tuple[str, int, int, str]] = []
        self.datasources: list[tuple[str, int, int]] = []
        self.beans: list[str] = []
        self.outputs = 0
        self.statements = 0

    # ------------------------------------------------------------------ server regions

    def find_regions(self) -> None:
        text = self.text
        found: list[Region] = []
        pos = 0
        n = len(text)
        while pos < n:
            m = _SERVER.search(text, pos)
            if not m:
                break
            a = m.start()
            token = m.group(0)
            if token == "<%--":
                end = text.find("--%>", a + 4)
                end = n if end < 0 else end + 4
                found.append(Region("comment", a, end, "", a))
            elif token == "<%":
                end = text.find("%>", a + 2)
                end = n if end < 0 else end + 2
                body_start = a + 2
                kind = "scriptlet"
                if text.startswith("@", body_start):
                    kind, body_start = "directive", body_start + 1
                elif text.startswith("=", body_start):
                    kind, body_start = "expression", body_start + 1
                elif text.startswith("!", body_start):
                    kind, body_start = "declaration", body_start + 1
                found.append(Region(kind, a, end, text[body_start:max(body_start, end - 2)], body_start))
            elif token in ("${", "#{"):
                if a > 0 and text[a - 1] == "\\":
                    pos = a + 2
                    continue
                end = _brace_end(text, a + 2)
                if end < 0:
                    pos = a + 2
                    continue
                found.append(Region("el", a, end, text[a + 2:end - 1], a + 2))
            else:  # <jsp:scriptlet> and friends in a JSP document
                kind = m.group(1).lower()
                close = re.compile(r"</jsp:" + kind + r"\s*>", re.I).search(text, m.end())
                gt = text.find(">", m.end())
                if gt < 0:
                    break
                end = n if close is None else close.end()
                body_end = n if close is None else close.start()
                found.append(Region(kind, a, end, text[gt + 1:body_end], gt + 1))
            pos = found[-1].end if found and found[-1].start == a else a + 2
        # Tags written inside an attribute value (`href="<c:url value='/x'/>"`) are masked too,
        # so the markup scanner reads the outer tag; `url_value` reads them back.
        starts = [r.start for r in found]
        for m in _NESTED_TAG.finditer(text):
            a, b = m.start(2), m.end(2)
            i = bisect_left(starts, a)
            if (i < len(found) and found[i].start < b) or (i and found[i - 1].end > a):
                continue
            found.insert(i, Region("tag", a, b, m.group(2), a))
            starts.insert(i, a)
        self.regions = found
        self.region_starts = [r.start for r in found]

    def regions_in(self, a: int, b: int) -> list[int]:
        i = bisect_left(self.region_starts, a)
        out = []
        while i < len(self.regions) and self.regions[i].start < b:
            out.append(i)
            i += 1
        return out

    # ------------------------------------------------------------------ directives and tag libraries

    def read_directives(self, elements: list[Element]) -> None:
        for i, r in enumerate(self.regions):
            if r.kind != "directive":
                continue
            m = _DIRECTIVE.match(r.code)
            if not m:
                continue
            attrs = {k.lower(): v[1:-1] for k, v in _DIRECTIVE_ATTR.findall(r.code)}
            self.directive(m.group(1).lower(), attrs, r.start, r.end)
            self.used.add(i)
        for e in elements:  # <jsp:directive.page .../> and xmlns:prefix="uri" in a JSP document
            if e.tag.startswith("jsp:directive."):
                self.directive(e.tag[len("jsp:directive."):], {k: (v.value or "") for k, v in attr_map(e).items() if v.value and HOLE not in v.value}, e.start, e.end)
            for name, a in attr_map(e).items():
                if name.startswith("xmlns:") and a.value:
                    lib = _library(a.value)
                    if lib:
                        self.prefixes.setdefault(name[6:], lib)

    def directive(self, name: str, attrs: dict[str, str], a: int, b: int) -> None:
        page = self.page
        if name not in ("page", "include", "taglib", "tag", "attribute", "variable"):
            return
        page.construct(f"jsp:directive.{name}", a, b, True)
        if name == "taglib":
            prefix = attrs.get("prefix")
            if not prefix:
                return
            if attrs.get("tagdir"):
                self.prefixes[prefix] = "tagdir:" + attrs["tagdir"]
            elif attrs.get("uri"):
                self.prefixes[prefix] = _library(attrs["uri"]) or "tld"
        elif name == "include":
            target = attrs.get("file")
            resolved = self.resolve_file(target) if target else None
            if resolved:
                self.calls.append((resolved, a, b))
        elif name == "page":
            if attrs.get("import"):
                imports = sorted({x.strip() for x in attrs["import"].split(",") if re.fullmatch(r"[\w.]+(?:\.\*)?", x.strip())})
                self.directives.setdefault("imports", [])
                self.directives["imports"] = sorted(set(self.directives["imports"]) | set(imports))
            if attrs.get("errorpage"):
                resolved = self.resolve_file(attrs["errorpage"])
                if resolved:
                    self.directives["error_page"] = resolved
            if attrs.get("iserrorpage", "").lower() == "true":
                self.directives["is_error_page"] = True
            if attrs.get("session", "").lower() == "false":
                self.directives["session"] = False
        elif name == "attribute" and attrs.get("name"):
            self.directives.setdefault("tag_attributes", []).append(attrs["name"])

    def library(self, prefix: str) -> str | None:
        lib = self.prefixes.get(prefix)
        if lib is None and prefix in CONVENTIONAL_PREFIXES:
            lib = CONVENTIONAL_PREFIXES[prefix]
            if prefix != "jsp":
                self.assumed.add(prefix)
        return lib

    def resolve_file(self, target: str) -> str | None:
        """A file reference of a directive or action: absolute from the web root, else relative to this file."""
        t = target.strip()
        if not t or HOLE in t or is_external(t):
            return None
        t = re.split(r"[?#]", t, maxsplit=1)[0]
        if t.startswith("/"):
            return posixpath.normpath(self.root + t.lstrip("/"))
        return relative(self.path, t)

    def url_refs(self, target: str, methods: tuple[str, ...]) -> list[dict[str, Any]]:
        """What a literal URL reaches: a JSP page's Program, or the servlet or controller Endpoint at its route."""
        t = target.strip()
        if is_external(t):
            return []
        t = re.split(r"[?#]", t, maxsplit=1)[0]
        if not t:
            return [self.program]
        resolved = posixpath.normpath(self.root + t.lstrip("/")) if t.startswith("/") else relative(self.path, t)
        if resolved is None:
            return []
        if resolved.lower().endswith((".jsp", ".jspx")):
            return [self.page.sql.program(PLATFORM, resolved, declare=False)]
        if not resolved.startswith(self.root):
            return []
        return endpoint_refs("/" + resolved[len(self.root):], methods)

    def url_value(self, a: Attr | None) -> tuple[str | None, bool]:
        """(literal URL path, present) of a URL attribute; a `<c:url value="/x"/>` written in
        the value gives its context-relative literal."""
        if a is None or a.value is None:
            return None, False
        if a.value and set(a.value) == {HOLE}:
            i = bisect_left(self.region_starts, a.start)
            for j in range(max(0, i - 1), min(len(self.regions), i + 2)):
                r = self.regions[j]
                if r.kind == "tag" and a.start <= r.start < a.end:
                    m = _URL_TAG.match(r.code.strip())
                    self.used.add(j)
                    if m:
                        return m.group(2).strip(), True
        return split_target(a.value), True

    # ------------------------------------------------------------------ expressions

    def expr_of(self, raw: str | None) -> dict[str, Any] | None:
        """An attribute value as an expression: a literal, an EL expression, or a template of both."""
        if raw is None:
            return None
        parts: list[Any] = []
        pos = 0
        for m in re.finditer(r"[$#]\{|<%=", raw):
            if m.start() < pos:
                continue
            if m.group(0) == "<%=":
                end = raw.find("%>", m.end())
                end = len(raw) if end < 0 else end + 2
                node: dict[str, Any] = {"e": "platform", "construct": "jsp:expression"}
            else:
                end = _brace_end(raw, m.end())
                if end < 0:
                    continue
                node, _ = EL.parse(raw[m.end():end - 1])
            if m.start() > pos:
                parts.append(raw[pos:m.start()])
            parts.append(node)
            pos = end
        if pos < len(raw):
            parts.append(raw[pos:])
        if not parts:
            return {"e": "lit", "value": "", "type": "string"}
        if len(parts) == 1:
            return parts[0] if isinstance(parts[0], dict) else {"e": "lit", "value": parts[0], "type": "string"}
        return {"e": "template", "parts": parts}

    def attr_expr(self, el: Element, name: str) -> dict[str, Any] | None:
        a = attr_map(el).get(name.lower())
        if a is None:
            return None
        for i in self.regions_in(a.start, a.end):
            self.use(i)
        expr = self.expr_of(orig_value(self.text, a))
        if expr is not None:
            for role in EL.role_checks(expr):
                self.roles.append((role, a.start, a.end, "isUserInRole"))
        return expr

    def use(self, i: int) -> None:
        if i in self.used:
            return
        self.used.add(i)
        r = self.regions[i]
        if r.kind == "el":
            _, ok = EL.parse(r.code)
            self.page.construct("jsp:el", r.start, r.end, ok)
            if not ok:
                self.page.note(f"EL expression at line {self.text.count(chr(10), 0, r.start) + 1} not read: kept as a platform node")
        elif r.kind in ("scriptlet", "expression", "declaration"):
            self.page.construct(f"jsp:{r.kind}", r.start, r.end, True)

    def at(self, a: int, b: int) -> list[int]:
        return [self.page.to_byte(a), self.page.to_byte(max(a, b))]

    # ------------------------------------------------------------------ lowering

    def region_statements(self, i: int) -> list[dict[str, Any]]:
        """Statements for a server region met in template text or a plain markup tag."""
        r = self.regions[i]
        if i in self.used and r.kind not in ("scriptlet", "declaration", "expression", "el"):
            return []
        first = i not in self.used
        self.use(i)
        at = self.at(r.start, r.end)
        if r.kind == "el":
            node, _ = EL.parse(r.code)
            for role in EL.role_checks(node):
                self.roles.append((role, r.start, r.end, "isUserInRole"))
            self.outputs += 1
            return [{"op": "output", "at": at, "channel": "response", "value": node}]
        if r.kind == "expression":
            self.outputs += 1
            body = self.java(r) if first else []
            return [{"op": "output", "at": at, "channel": "response", "value": {"e": "platform", "construct": "jsp:expression"}}, *body]
        if r.kind in ("scriptlet", "declaration"):
            return [{"op": "platform", "at": at, "construct": f"jsp:{r.kind}", "body": self.java(r) if first else []}]
        return []

    def java(self, r: Region) -> list[dict[str, Any]]:
        """Redirects, forwards and role checks read from Java code; nothing else of it is kept."""
        out: list[tuple[int, dict[str, Any]]] = []
        base = r.code_start
        for m in _REDIRECT.finditer(r.code):
            a, b = base + m.start(), base + m.end()
            self.page.construct("jsp:send-redirect", a, b, True)
            if m.group(1) is not None:
                target = _java_string(m.group(1))
                self.redirects.append((target, a, b, "sendRedirect"))
                value: dict[str, Any] = {"e": "lit", "value": target, "type": "string"}
            else:
                self.redirects.append((None, a, b, "sendRedirect"))
                value = {"e": "platform", "construct": "jsp:send-redirect", "dynamic": True}
            out.append((a, {"op": "redirect", "at": self.at(a, b), "target": value}))
        for m in _DISPATCH.finditer(r.code):
            a, b = base + m.start(), base + m.end()
            self.page.construct("jsp:request-dispatcher", a, b, True)
            literal_target = m.group(1) if m.group(1) is not None else m.group(4)
            mode = m.group(2) or m.group(3)
            if literal_target is not None:
                target = _java_string(literal_target)
                value = {"e": "lit", "value": target, "type": "string"}
                resolved = self.resolve_file(target)
                if mode == "forward":
                    self.redirects.append((target, a, b, "forward"))
                elif resolved:
                    self.calls.append((resolved, a, b))
            else:
                value = {"e": "platform", "construct": "jsp:request-dispatcher", "dynamic": True}
                self.page.note(f"{mode} to a computed path: target not resolved")
            out.append((a, {"op": "include", "at": self.at(a, b), "target": value, **({"forward": True} if mode == "forward" else {})}))
        for m in _ROLE.finditer(r.code):
            a, b = base + m.start(), base + m.end()
            role = _java_string(m.group(1))
            self.page.construct("jsp:is-user-in-role", a, b, True)
            self.roles.append((role, a, b, "isUserInRole"))
            call = {"e": "call", "callee": {"e": "member", "object": {"e": "var", "name": "request"}, "name": "isUserInRole"}, "args": [{"e": "lit", "value": role, "type": "string"}]}
            out.append((a, {"op": "call", "at": self.at(a, b), "call": call}))
        return [s for _, s in sorted(out, key=lambda x: x[0])]

    def content_end(self, el: Element) -> int:
        if el.close <= el.end:
            return el.end
        j = self.text.rfind("</", el.end, el.close)
        return el.close if j < 0 else j

    def block(self, children: list[Element], a: int, b: int) -> list[dict[str, Any]]:
        """Statements of the template text and elements between `a` and `b`, in document order."""
        out: list[dict[str, Any]] = []
        cursor = a
        for child in children:
            for i in self.regions_in(cursor, child.start):
                out += self.region_statements(i)
            out += self.element(child)
            cursor = max(cursor, child.close)
            if self.statements > _MAX_STATEMENTS:
                return out
        for i in self.regions_in(cursor, b):
            out += self.region_statements(i)
        self.statements += len(out)
        return out

    def body(self, el: Element) -> list[dict[str, Any]]:
        return self.block(el.children, el.end, self.content_end(el))

    def tag_regions(self, el: Element) -> list[dict[str, Any]]:
        """Server code in a plain markup tag's attributes (`<a href="${url}">`): written to the response."""
        out = []
        for i in self.regions_in(el.start, el.end):
            if self.regions[i].kind == "tag":
                self.use(i)
                continue
            out += self.region_statements(i)
        return out

    def element(self, el: Element) -> list[dict[str, Any]]:
        tag = el.tag
        if ":" not in tag:
            return self.tag_regions(el) + self.body(el)
        prefix, local = tag.split(":", 1)
        lib = self.library(prefix)
        if lib is None:
            return self.tag_regions(el) + self.body(el)  # an XML namespace, not a tag library
        if lib.startswith("tagdir:"):
            return self.custom_tag(el, lib[len("tagdir:"):], local)
        if lib in WHOLE_LIBRARIES or lib == "tld":
            cid = WHOLE_LIBRARIES.get(lib, "jsp:tld-tag")
            self.page.construct(cid, el.start, el.end, False)
            self.consume(el)
            return self.body(el)
        if lib == "fn":
            return self.body(el)
        if lib == "jsp" and local.startswith("directive."):
            return []
        spec = TAGS.get(lib, {}).get(local)
        if spec is None:
            self.page.construct("jsp:unknown-tag", el.start, el.end, False)
            self.consume(el)
            return self.body(el)
        cid, modelled_as = spec
        handler = getattr(self, "_" + cid.split(":", 1)[1].replace(".", "_"), None)  # by the tag's canonical name: _c_forEach
        canonical = cid.split(".", 1)[-1]
        if handler is None and lib == "logic" and canonical in _LOGIC_CONDITIONS:
            handler = lambda el, at: self._logic_condition(el, at, canonical)  # noqa: E731
        at = self.at(el.start, el.close)
        if handler is not None:
            stmts = handler(el, at)
            self.page.construct(cid, el.start, el.end, True)
            return stmts
        if modelled_as == "unmapped" or modelled_as.startswith("entity:") or modelled_as.startswith("attribute:"):
            # Forms and controls are declared from the markup pass; unmodelled tags are counted.
            modelled = not modelled_as == "unmapped" and lib in ("html", "form")
            if lib in ("html", "form"):
                stmts = self.tag_regions_consumed(el)
            else:
                self.consume(el)
                stmts = []
            if not modelled:
                self.page.construct(cid, el.start, el.end, False)
            return stmts + self.body(el)
        self.consume(el)
        self.page.construct(cid, el.start, el.end, modelled_as != "unmapped")
        return [{"op": "platform", "at": at, "construct": cid, "body": self.body(el)}]

    def consume(self, el: Element) -> None:
        for i in self.regions_in(el.start, el.end):
            self.use(i)

    def tag_regions_consumed(self, el: Element) -> list[dict[str, Any]]:
        self.consume(el)
        return []

    def custom_tag(self, el: Element, tagdir: str, local: str) -> list[dict[str, Any]]:
        base = tagdir.strip("/")
        root = self.root if tagdir.startswith("/") else posixpath.dirname(self.path) + "/"
        found = False
        for ext in (".tag", ".tagx"):
            target = posixpath.normpath(root + base + "/" + local + ext)
            self.calls.append((target, el.start, el.end))
            found = True
        self.page.construct("jsp:custom-tag", el.start, el.end, found)
        args = {name: self.attr_expr(el, name) for name in sorted(attr_map(el))}
        target = {"e": "lit", "value": posixpath.normpath(base + "/" + local), "type": "string"}
        return [{"op": "include", "at": self.at(el.start, el.close), "target": target, "module": True, "args": {k: v for k, v in args.items() if v is not None}}, *self.body(el)]

    # ------------------------------------------------------------------ JSTL core

    def _var(self, el: Element, name: str = "var") -> dict[str, Any] | None:
        v = literal(attr_map(el).get(name))
        if not v:
            return None
        scope = literal(attr_map(el).get("scope"))
        node: dict[str, Any] = {"e": "var", "name": v}
        if scope:
            node["scope"] = scope.lower()
        return node

    def _c_out(self, el, at):
        self.outputs += 1
        return [{"op": "output", "at": at, "channel": "response", "value": self.attr_expr(el, "value") or {"e": "lit", "value": "", "type": "string"}}]

    def _c_set(self, el, at):
        target = self._var(el)
        if target is None:
            obj, prop = self.attr_expr(el, "target"), literal(attr_map(el).get("property"))
            target = {"e": "member", "object": obj, "name": prop} if obj is not None and prop else {"e": "platform", "construct": "jsp:c.set"}
        value = self.attr_expr(el, "value") or {"e": "platform", "construct": "jsp:c.set", "body_value": True}
        return [{"op": "assign", "at": at, "target": target, "value": value}, *self.body(el)]

    def _c_remove(self, el, at):
        target = self._var(el) or {"e": "platform", "construct": "jsp:c.remove"}
        return [{"op": "assign", "at": at, "target": target, "value": {"e": "lit", "value": None, "type": "null"}}]

    def _c_if(self, el, at):
        stmt = {"op": "if", "at": at, "cond": self.attr_expr(el, "test") or {"e": "lit", "value": False, "type": "boolean"}, "then": self.body(el)}
        var = self._var(el)
        return [stmt] if var is None else [{"op": "assign", "at": at, "target": var, "value": stmt["cond"]}, stmt]

    def _c_choose(self, el, at):
        branches: list[tuple[Element, dict[str, Any] | None]] = []
        for child in el.children:
            if child.tag.endswith(":when"):
                branches.append((child, self.attr_expr(child, "test")))
                self.page.construct("jsp:c.when", child.start, child.end, True)
            elif child.tag.endswith(":otherwise"):
                branches.append((child, None))
                self.page.construct("jsp:c.otherwise", child.start, child.end, True)
        chain: list[dict[str, Any]] = []
        for child, cond in reversed(branches):
            body = self.body(child)
            if cond is None:
                chain = body
            else:
                chain = [{"op": "if", "at": self.at(child.start, child.close), "cond": cond, "then": body, **({"else": chain} if chain else {})}]
        return chain

    def _c_forEach(self, el, at):
        body = self.body(el)
        var = self._var(el)
        over = self.attr_expr(el, "items")
        if over is not None:
            stmt: dict[str, Any] = {"op": "loop", "at": at, "kind": "each", "over": over, "body": body}
        else:
            stmt = {"op": "loop", "at": at, "kind": "range", "from": self.attr_expr(el, "begin") or {"e": "lit", "value": 0, "type": "integer"},
                    "to": self.attr_expr(el, "end") or {"e": "lit", "value": None, "type": "null"}, "body": body}
        if var is not None:
            stmt["var"] = var
        if attr_map(el).get("step") is not None:
            stmt["step"] = self.attr_expr(el, "step")
        return [stmt]

    def _c_forTokens(self, el, at):
        stmt: dict[str, Any] = {"op": "loop", "at": at, "kind": "each", "over": self.attr_expr(el, "items") or {"e": "lit", "value": "", "type": "string"}, "body": self.body(el)}
        var = self._var(el)
        if var is not None:
            stmt["var"] = var
        delims = literal(attr_map(el).get("delims"))
        if delims:
            stmt["delimiters"] = delims
        return [stmt]

    def _params(self, el: Element) -> list[str]:
        names = []
        for child in el.children:
            if child.tag.endswith(":param"):
                name = literal(attr_map(child).get("name"))
                if name:
                    names.append(name)
                lib = child.tag.split(":", 1)[0]
                self.page.construct(TAGS.get(self.library(lib) or "", {}).get("param", ("jsp:c.param",))[0], child.start, child.end, True)
                self.consume(child)
        return names

    def _url_expr(self, el: Element, name: str) -> tuple[dict[str, Any], str | None]:
        a = attr_map(el).get(name)
        expr = self.attr_expr(el, name) or {"e": "lit", "value": "", "type": "string"}
        target, _ = self.url_value(a)
        return expr, target

    def _c_url(self, el, at):
        expr, _ = self._url_expr(el, "value")
        params = self._params(el)
        if params:
            expr = {"e": "template", "parts": [expr, "?" + "&".join(p + "=" for p in params)]}
        var = self._var(el)
        if var is None:
            self.outputs += 1
            return [{"op": "output", "at": at, "channel": "response", "value": expr}]
        return [{"op": "assign", "at": at, "target": var, "value": expr}]

    def _c_import(self, el, at):
        expr, target = self._url_expr(el, "url")
        params = self._params(el)
        if target is not None:
            resolved = self.resolve_file(target)
            if resolved and resolved.lower().endswith((".jsp", ".jspx", ".jspf")):
                self.calls.append((resolved, el.start, el.end))
        else:
            self.page.note("c:import of a computed URL: target not resolved")
        stmt: dict[str, Any] = {"op": "include", "at": at, "target": expr}
        if params:
            stmt["params"] = params
        var = self._var(el)
        return [stmt] if var is None else [stmt, {"op": "assign", "at": at, "target": var, "value": {"e": "platform", "construct": "jsp:c.import"}}]

    def _c_redirect(self, el, at):
        expr, target = self._url_expr(el, "url")
        params = self._params(el)
        self.redirects.append((target, el.start, el.end, "c:redirect"))
        stmt: dict[str, Any] = {"op": "redirect", "at": at, "target": expr}
        if params:
            stmt["params"] = params
        return [stmt]

    def _c_catch(self, el, at):
        var = literal(attr_map(el).get("var"))
        return [{"op": "try", "at": at, "body": self.body(el), "catches": [{"type": "any", **({"var": var} if var else {}), "body": []}]}]

    def _c_param(self, el, at):
        self.consume(el)
        return []

    def _c_when(self, el, at):  # outside c:choose: read as a conditional
        return self._c_if(el, at)

    def _c_otherwise(self, el, at):
        return self.body(el)

    # ------------------------------------------------------------------ JSTL fmt

    def _fmt_call(self, el: Element, at, name: str, arg: str) -> list[dict[str, Any]]:
        args = [x for x in (self.attr_expr(el, arg),) if x is not None]
        call = {"e": "call", "callee": {"e": "name", "name": f"fmt:{name}", "kind": "builtin"}, "args": args}
        var = self._var(el)
        if var is not None:
            return [{"op": "assign", "at": at, "target": var, "value": call}]
        self.outputs += 1
        return [{"op": "output", "at": at, "channel": "response", "value": call}]

    def _fmt_message(self, el, at):
        self._params(el)
        return self._fmt_call(el, at, "message", "key")

    def _fmt_formatNumber(self, el, at):  # noqa: N802
        return self._fmt_call(el, at, "formatNumber", "value")

    def _fmt_formatDate(self, el, at):  # noqa: N802
        return self._fmt_call(el, at, "formatDate", "value")

    def _fmt_parseNumber(self, el, at):  # noqa: N802
        return self._fmt_call(el, at, "parseNumber", "value")

    def _fmt_parseDate(self, el, at):  # noqa: N802
        return self._fmt_call(el, at, "parseDate", "value")

    # ------------------------------------------------------------------ JSTL sql

    def _datasource(self, el: Element) -> str | None:
        a = attr_map(el).get("datasource")
        name = literal(a)
        if name and not re.search(r"[;=]|://", name):  # a JNDI name or a variable, never a connection string
            self.datasources.append((name, el.start, el.end))
            return name
        if a is not None and name is None:
            self.consume(el)
        return None

    def _sql_query(self, el, at):
        return self._sql_statement(el, at)

    def _sql_update(self, el, at):
        return self._sql_statement(el, at)

    def _sql_statement(self, el: Element, at) -> list[dict[str, Any]]:
        """sql:query or sql:update: the SQL is the `sql` attribute or the element's text with its
        sql:param children taken out (they bind the `?` marks in order). A server expression
        inside the SQL is text substituted before it runs: an inline parameter."""
        a, b = el.end, self.content_end(el)
        children = el.children
        attr = attr_map(el).get("sql")
        if attr is not None:
            raw = orig_value(self.text, attr) or ""
            start = self.text.find(raw, attr.start, attr.end) if raw else -1
            if start >= 0:
                a, b = start, start + len(raw)
            children = []
        chars = list(self.text[a:b])
        bound: list[dict[str, Any]] = []
        taken: list[tuple[int, int]] = []
        for child in children:
            local = child.tag.split(":", 1)[-1]
            if local in ("param", "dateparam"):
                bound.append(self.attr_expr(child, "value") or {"e": "lit", "value": None, "type": "null"})
                self.page.construct("jsp:sql.dateParam" if local == "dateparam" else "jsp:sql.param", child.start, child.end, True)
                for k in range(child.start - a, min(child.close, b) - a):
                    chars[k] = " "
                taken.append((child.start, child.close))
        params: list[dict[str, Any]] = []
        dynamic: list[tuple[int, int]] = []
        pieces: list[str] = []
        pos = 0
        for i in self.regions_in(a, b):
            r = self.regions[i]
            if any(s <= r.start < e for s, e in taken):
                continue
            s, e = r.start - a, min(r.end, b) - a
            if s < pos:
                continue
            self.use(i)
            name = f"el{len(dynamic) + 1}"
            pieces += ["".join(chars[pos:s]), ":" + name]
            value = EL.parse(r.code)[0] if r.kind == "el" else {"e": "platform", "construct": f"jsp:{r.kind}"}
            params.append({"name": name, "value": value, "inline": True})
            dynamic.append((s, e))
            chars[s:e] = list("x" + "_" * (e - s - 1))
            pos = e
        pieces.append("".join(chars[pos:]))
        count = 0

        def bind(m: re.Match) -> str:
            nonlocal count
            if m.group(0) != "?":
                return m.group(0)
            count += 1
            params.append({"name": f"p{count}", "value": bound[count - 1] if count <= len(bound) else {"e": "lit", "value": None, "type": "null"}})
            return f":p{count}"

        sql = re.sub(r"'(?:[^']|'')*'|\?", bind, "".join(pieces))
        stmt: dict[str, Any] = {"op": "query", "at": at, "sql": re.sub(r"\s+", " ", sql).strip(), "params": params}
        ds = self._datasource(el)
        if ds:
            stmt["datasource"] = ds
        var = self._var(el)
        if var is not None:
            stmt["target"] = var
        self.page.query_lineage(self.program, "".join(chars), a, dynamic)
        return [stmt]

    def _sql_transaction(self, el, at):
        ds = self._datasource(el)
        return [{"op": "transaction", "at": at, "body": self.body(el), **({"datasource": ds} if ds else {})}]

    def _sql_setDataSource(self, el, at):  # noqa: N802
        a = attr_map(el).get("datasource")
        name = literal(a)
        self.consume(el)
        if name and not re.search(r"[;=]|://", name):
            self.datasources.append((name, el.start, el.end))
            return [{"op": "platform", "at": at, "construct": "jsp:sql.setDataSource", "datasource": name}]
        if attr_map(el).get("url") is not None:
            self.page.note("sql:setDataSource with a JDBC URL: connection details are not read")
        return [{"op": "platform", "at": at, "construct": "jsp:sql.setDataSource"}]

    # ------------------------------------------------------------------ standard actions

    def _jsp_include(self, el, at):
        a = attr_map(el).get("page")
        target = literal(a)
        params = self._params(el)
        if target:
            resolved = self.resolve_file(target)
            if resolved:
                self.calls.append((resolved, el.start, el.end))
        else:
            self.page.note("jsp:include of a computed page: target not resolved")
        stmt: dict[str, Any] = {"op": "include", "at": at, "target": self.attr_expr(el, "page") or {"e": "lit", "value": "", "type": "string"}}
        if params:
            stmt["params"] = params
        return [stmt]

    def _jsp_forward(self, el, at):
        target = literal(attr_map(el).get("page"))
        params = self._params(el)
        self.redirects.append((target, el.start, el.end, "jsp:forward"))
        stmt: dict[str, Any] = {"op": "include", "at": at, "target": self.attr_expr(el, "page") or {"e": "lit", "value": "", "type": "string"}, "forward": True}
        if params:
            stmt["params"] = params
        return [stmt, {"op": "exit", "at": at}]

    def _jsp_param(self, el, at):
        self.consume(el)
        return []

    def _jsp_useBean(self, el, at):  # noqa: N802
        attrs = attr_map(el)
        name = literal(attrs.get("id"))
        cls = literal(attrs.get("class")) or literal(attrs.get("type")) or literal(attrs.get("beanname"))
        if name:
            self.beans.append(name)
        stmt: dict[str, Any] = {"op": "declare", "at": at, "name": name or "bean"}
        scope = literal(attrs.get("scope"))
        if scope:
            stmt["scope"] = scope.lower()
        if cls:
            stmt["value"] = {"e": "new", "type": cls}
        return [stmt, *self.body(el)]

    def _jsp_setProperty(self, el, at):  # noqa: N802
        attrs = attr_map(el)
        bean, prop = literal(attrs.get("name")), literal(attrs.get("property"))
        target = {"e": "member", "object": {"e": "var", "name": bean or "bean"}, "name": prop or "*"}
        if attrs.get("value") is not None:
            value = self.attr_expr(el, "value")
        else:
            param = literal(attrs.get("param")) or (prop if prop and prop != "*" else None)
            value = {"e": "var", "scope": "param", "name": param} if param else {"e": "platform", "construct": "jsp:jsp.setProperty", "all_parameters": True}
        return [{"op": "assign", "at": at, "target": target, "value": value}]

    def _jsp_getProperty(self, el, at):  # noqa: N802
        attrs = attr_map(el)
        self.outputs += 1
        value = {"e": "member", "object": {"e": "var", "name": literal(attrs.get("name")) or "bean"}, "name": literal(attrs.get("property")) or ""}
        return [{"op": "output", "at": at, "channel": "response", "value": value}]

    # ------------------------------------------------------------------ Struts bean and logic, Spring, Spring Security

    def _bean_write(self, el, at):
        attrs = attr_map(el)
        obj: dict[str, Any] = {"e": "var", "name": literal(attrs.get("name")) or "bean"}
        prop = literal(attrs.get("property"))
        self.outputs += 1
        return [{"op": "output", "at": at, "channel": "response", "value": {"e": "member", "object": obj, "name": prop} if prop else obj}]

    def _bean_message(self, el, at):
        self.outputs += 1
        key = literal(attr_map(el).get("key"))
        call = {"e": "call", "callee": {"e": "name", "name": "bean:message", "kind": "builtin"}, "args": [{"e": "lit", "value": key or "", "type": "string"}]}
        return [{"op": "output", "at": at, "channel": "response", "value": call}]

    def _bean_define(self, el, at):
        attrs = attr_map(el)
        name = literal(attrs.get("id")) or "bean"
        source = literal(attrs.get("name"))
        prop = literal(attrs.get("property"))
        value: dict[str, Any] = self.attr_expr(el, "value") or ({"e": "var", "name": source} if source else {"e": "platform", "construct": "jsp:bean.define"})
        if prop and source:
            value = {"e": "member", "object": value, "name": prop}
        return [{"op": "assign", "at": at, "target": {"e": "var", "name": name}, "value": value}]

    def _bean_size(self, el, at):
        attrs = attr_map(el)
        name = literal(attrs.get("id")) or "size"
        source = literal(attrs.get("name"))
        value = {"e": "call", "callee": {"e": "name", "name": "size", "kind": "builtin"}, "args": [{"e": "var", "name": source}] if source else []}
        return [{"op": "assign", "at": at, "target": {"e": "var", "name": name}, "value": value}]

    def _logic_iterate(self, el, at):
        attrs = attr_map(el)
        source = literal(attrs.get("name"))
        over: dict[str, Any] = {"e": "var", "name": source} if source else (self.attr_expr(el, "collection") or {"e": "platform", "construct": "jsp:logic.iterate"})
        prop = literal(attrs.get("property"))
        if prop and source:
            over = {"e": "member", "object": over, "name": prop}
        stmt: dict[str, Any] = {"op": "loop", "at": at, "kind": "each", "over": over, "body": self.body(el)}
        var = literal(attrs.get("id"))
        if var:
            stmt["var"] = {"e": "var", "name": var}
        return [stmt]

    def _logic_condition(self, el: Element, at, name: str) -> list[dict[str, Any]]:
        attrs = attr_map(el)
        role = literal(attrs.get("role"))
        if role and name in ("present", "notPresent"):
            roles = [r.strip() for r in role.split(",") if r.strip()]
            for r in roles:
                self.roles.append((r, el.start, el.end, "logic:present"))
            cond: dict[str, Any] = {"e": "call", "callee": {"e": "member", "object": {"e": "var", "name": "request"}, "name": "isUserInRole"}, "args": [{"e": "lit", "value": r, "type": "string"} for r in roles]}
            if name == "notPresent":
                cond = {"e": "unop", "op": "not", "operand": cond}
        else:
            subject: dict[str, Any] = {"e": "var", "name": literal(attrs.get("name")) or literal(attrs.get("parameter")) or "bean"}
            if literal(attrs.get("parameter")):
                subject = {"e": "var", "scope": "param", "name": literal(attrs.get("parameter"))}
            if literal(attrs.get("property")):
                subject = {"e": "member", "object": subject, "name": literal(attrs.get("property"))}
            ops = {"equal": "==", "notEqual": "!=", "greaterThan": ">", "lessThan": "<", "greaterEqual": ">=", "lessEqual": "<="}
            if name in ops:
                cond = {"e": "binop", "op": ops[name], "left": subject, "right": self.attr_expr(el, "value") or {"e": "lit", "value": None, "type": "null"}}
            elif name in ("empty", "notEmpty"):
                cond = {"e": "call", "callee": {"e": "name", "name": "empty", "kind": "builtin"}, "args": [subject]}
                if name == "notEmpty":
                    cond = {"e": "unop", "op": "not", "operand": cond}
            elif name in ("present", "notPresent"):
                cond = {"e": "unop", "op": "exists" if name == "present" else "missing", "operand": subject}
            else:
                cond = {"e": "platform", "construct": f"jsp:logic.{name}"}
        self.consume(el)
        return [{"op": "if", "at": at, "cond": cond, "then": self.body(el)}]

    def _logic_redirect(self, el, at):
        attrs = attr_map(el)
        a = attrs.get("href") or attrs.get("page")
        target = literal(a)
        self.redirects.append((target if attrs.get("forward") is None else None, el.start, el.end, "logic:redirect"))
        expr = self.expr_of(orig_value(self.text, a)) if a is not None else {"e": "platform", "construct": "jsp:logic.redirect"}
        self.consume(el)
        return [{"op": "redirect", "at": at, "target": expr}]

    def _spring_message(self, el, at):
        self.outputs += 1
        args = [x for x in (self.attr_expr(el, "code"),) if x is not None]
        call = {"e": "call", "callee": {"e": "name", "name": "spring:message", "kind": "builtin"}, "args": args}
        var = self._var(el)
        if var is not None:
            return [{"op": "assign", "at": at, "target": var, "value": call}]
        return [{"op": "output", "at": at, "channel": "response", "value": call}]

    def _spring_url(self, el, at):
        return self._c_url(el, at)

    def _spring_eval(self, el, at):
        self.outputs += 1
        self.consume(el)
        return [{"op": "output", "at": at, "channel": "response", "value": {"e": "platform", "construct": "jsp:spring.eval"}}]

    def _spring_param(self, el, at):
        self.consume(el)
        return []

    def _sec_authorize(self, el, at):
        attrs = attr_map(el)
        roles: list[str] = []
        access = orig_value(self.text, attrs["access"]) if attrs.get("access") is not None else None
        if access:
            for m in _SEC_ROLE.finditer(access):
                roles += [q1 or q2 for q1, q2 in _QUOTED.findall(m.group(1))]
        for key in ("ifanygranted", "ifallgranted"):
            v = literal(attrs.get(key))
            if v:
                roles += [r.strip() for r in v.split(",") if r.strip()]
        for r in roles:
            self.roles.append((r, el.start, el.end, "sec:authorize"))
        cond: dict[str, Any] = {"e": "call", "callee": {"e": "name", "name": "hasAnyRole", "kind": "builtin"}, "args": [{"e": "lit", "value": r, "type": "string"} for r in roles]} if roles else {"e": "platform", "construct": "jsp:sec.authorize"}
        if literal(attrs.get("ifnotgranted")):
            cond = {"e": "platform", "construct": "jsp:sec.authorize", "not_granted": True}
        self.consume(el)
        return [{"op": "if", "at": at, "cond": cond, "then": self.body(el)}]

    def _sec_authentication(self, el, at):
        self.outputs += 1
        prop = literal(attr_map(el).get("property")) or "principal"
        return [{"op": "output", "at": at, "channel": "response", "value": {"e": "member", "object": {"e": "var", "name": "authentication"}, "name": prop}}]

    # ------------------------------------------------------------------ forms and controls

    def ui(self, elements: list[Element]) -> tuple[list[UiForm], list[UiControl]]:
        forms: list[UiForm] = []
        controls: list[UiControl] = []
        for el in elements:
            tag = el.tag
            lib, local = None, tag
            if ":" in tag:
                prefix, local = tag.split(":", 1)
                lib = self.library(prefix)
                if lib not in ("html", "form"):
                    continue
            attrs = attr_map(el)
            extent = (el.end, el.close)
            if local == "form" and (lib is None or lib in ("html", "form")):
                forms.append(self.form(el, attrs, lib, extent))
                continue
            control = self.control(el, attrs, lib, local)
            if control is not None:
                controls.append(control)
        return forms, controls

    def form(self, el: Element, attrs: dict[str, Attr], lib: str | None, extent: tuple[int, int]) -> UiForm:
        construct = MARKUP["form"] if lib is None else TAGS[lib]["form"][0]
        method = (literal(attrs.get("method")) or ("post" if lib else "get")).lower()
        fattrs: dict[str, Any] = {"kind": {"html": "struts", "form": "spring"}.get(lib or "", "html"), "method": method}
        names: list[str | None] = [literal(attrs.get("name")), literal(attrs.get("id"))]
        if lib == "form":
            model = literal(attrs.get("modelattribute")) or literal(attrs.get("commandname"))
            if model:
                fattrs["model"] = model
        if lib == "html":
            action_path = literal(attrs.get("action"))
            if action_path:
                fattrs["struts_action"] = action_path
        dynamic_name = attrs.get("name") is not None and literal(attrs.get("name")) is None
        submits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        action = attrs.get("action")
        methods = (method.upper(), "ANY") if method.upper() in ("GET", "POST") else ("ANY",)
        if action is None or (action.value is not None and not action.value.strip()):
            if lib != "html":
                submits.append((self.program, {"implicit": True}))
        elif lib == "html":
            pass  # a Struts action path, mapped to a URL by struts-config.xml, which this file does not show
        else:
            target, _ = self.url_value(action)
            if target is not None:
                submits += [(ref, {}) for ref in self.url_refs(target, methods)]
            else:
                self.page.note(f"form {names[0] or names[1] or ''} posts to a computed action: target not resolved".replace("form  ", "form "))
        if lib == "html" and attrs.get("action") is not None:
            self.page.note("a Struts form posts to an action mapped in struts-config.xml: not followed")
        return UiForm(el.start, el.end, extent, construct, names, dynamic_name, fattrs, submits)

    def control(self, el: Element, attrs: dict[str, Attr], lib: str | None, local: str) -> UiControl | None:
        binding = None
        navigates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        link = None
        button = None
        label = None
        if lib is None:
            if local not in ("input", "select", "textarea", "button", "a"):
                return None
            construct = MARKUP[local]
            if local == "input":
                ctype = (literal(attrs.get("type")) or "text").lower()
            elif local == "a":
                href = attrs.get("href")
                if href is None:
                    return None
                ctype = "link"
                link, _ = self.url_value(href)
                if link is not None:
                    navigates = [(ref, {"via": "link"}) for ref in self.url_refs(link, ("GET", "ANY"))]
                else:
                    self.page.note("link to a computed URL: target not resolved")
            else:
                ctype = local
            if local == "button":
                button = "submit" if (literal(attrs.get("type")) or "").lower() == "submit" else "button"
                label = button_label(el)
            elif local == "input" and ctype in ("submit", "button", "reset", "image"):
                button = "submit" if ctype == "submit" else "button"
                label = button_label(el)
        else:
            spec = TAGS[lib].get(local)
            if spec is None or not spec[1].startswith("entity:UIControl"):
                return None
            construct = spec[0]
            binding = literal(attrs.get("property")) if lib == "html" else literal(attrs.get("path"))
            kinds = {"text": "text", "input": "text", "password": "password", "hidden": "hidden", "textarea": "textarea", "select": "select", "checkbox": "checkbox",
                     "checkboxes": "checkbox", "multibox": "checkbox", "radio": "radio", "radiobutton": "radio", "radiobuttons": "radio", "file": "file",
                     "submit": "submit", "button": "button", "cancel": "button", "reset": "reset", "image": "image", "link": "link"}
            ctype = kinds.get(local, local)
            if lib == "form" and local == "input":
                ctype = (literal(attrs.get("type")) or "text").lower()
            if local in ("submit", "button", "cancel", "reset", "image"):
                button = "submit" if local == "submit" or (lib == "form" and (literal(attrs.get("type")) or "submit").lower() == "submit") else "button"
                label = literal(attrs.get("value")) or (None if HOLE in el.deep_text else (el.deep_text.strip() or None))
            if local == "link":
                target = literal(attrs.get("page")) or literal(attrs.get("href"))
                link = target
                if target:
                    navigates = [(ref, {"via": "link"}) for ref in self.url_refs(target, ("GET", "ANY"))]
        names = [literal(attrs.get("name")), literal(attrs.get("id") or attrs.get("styleid")), binding]
        cattrs: dict[str, Any] = {"tag": el.tag}
        if binding:
            cattrs["binding"] = binding
        if attrs.get("required") is not None:
            cattrs["required"] = True
        for key in ("pattern", "maxlength", "min", "max"):
            if literal(attrs.get(key)):
                cattrs[key] = literal(attrs.get(key))
        dynamic_name = attrs.get("name") is not None and literal(attrs.get("name")) is None
        return UiControl(el.start, el.end, construct, ctype, names, dynamic_name, link, button, label, cattrs, navigates)

    # ------------------------------------------------------------------ the file

    def run(self):
        page = self.page
        self.find_regions()
        elements = markup(mask(self.text, self.regions))
        self.read_directives(elements)
        top = [e for e in elements if e.parent is None]
        body = self.block(top, 0, len(self.text))
        forms, controls = self.ui(elements)
        for i, r in enumerate(self.regions):  # regions no statement took (inside unmodelled tags) are counted as seen
            if i not in self.used and r.kind in ("el", "scriptlet", "expression", "declaration"):
                self.used.add(i)
                page.construct("jsp:el" if r.kind == "el" else f"jsp:{r.kind}", r.start, r.end, False)
        renders = self.outputs > 0 or any(":" not in e.tag for e in elements) or bool(forms or controls)
        is_page = self.kind in ("page", "document")
        has_ui = bool(forms or controls)

        # The program, and the URL it answers.
        attrs: dict[str, Any] = {"kind": self.kind, "language": LANGUAGE, **self.directives}
        taglibs = sorted({lib.split(":", 1)[0] for lib in self.prefixes.values()} | {CONVENTIONAL_PREFIXES[p] for p in self.assumed})
        if taglibs:
            attrs["taglibs"] = taglibs
        if self.assumed:
            attrs["taglibs_assumed"] = sorted(self.assumed)
        if self.beans:
            attrs["beans"] = sorted(set(self.beans))
        if self.root:
            attrs["web_root"] = self.root.rstrip("/")
        term = "jsp:TagFile" if self.kind == "tag" else "jsp:Page"
        prog = page.term(page.whole(), term, self.program["natural_key"], self.program["name"], attrs)
        route = url_of(self.path) if is_page else None
        endpoint = None
        if route:
            key = {"component_root": page.base.UNIT, "protocol": "http", "method": "ANY", "route": route}
            endpoint = page.term(page.whole(), "jsp:PageRequest", key, f"ANY {route}", {"framework": "jsp"})
            page.rel(page.whole(), prog, P.EXPOSES, endpoint)
        if self.directives.get("error_page"):
            page.rel(page.whole(), prog, P.NAVIGATES_TO, page.sql.program(PLATFORM, self.directives["error_page"], declare=False), {"via": "errorPage"})

        # The screen it renders.
        screen = None
        if (is_page and renders) or has_ui:
            title = _title(elements)
            sattrs: dict[str, Any] = {"route": route, "title": title, "fragment": True if not is_page else None}
            screen = page.term(page.whole(), "jsp:View", {"platform": PLATFORM, "name": self.path}, self.path, sattrs)
            page.rel(page.whole(), prog, P.RENDERS, screen)
            emit_ui(page, screen, self.path, forms, controls, "jsp:Form", "jsp:Control")
        else:
            for f in forms:
                page.construct(f.construct, f.start, f.end, False)
            for c in controls:
                page.construct(c.construct, c.start, c.end, False)

        # Includes, custom tags, redirects and forwards.
        for target, a, b in self.calls:
            page.rel(page.span(a, b), prog, P.CALLS, page.sql.program(PLATFORM, target, declare=False))
        for target, a, b, via in self.redirects:
            if target is None:
                page.note(f"{via} to a computed URL: target not resolved")
                continue
            if via == "jsp:forward" or via == "forward":
                resolved = self.resolve_file(target)
                refs = [page.sql.program(PLATFORM, resolved, declare=False)] if resolved and resolved.lower().endswith((".jsp", ".jspx")) else self.url_refs(target, ("GET", "ANY"))
            else:
                refs = self.url_refs(target, ("GET", "ANY"))
            subject = screen if screen is not None else prog
            for ref in refs:
                if ref != self.program:
                    page.rel(page.span(a, b), subject, P.NAVIGATES_TO, ref, {"via": via})

        # Roles checked on the page.
        for role, a, b, via in self.roles:
            at = page.span(a, b)
            r = page.term(at, "jsp:Role", {"realm": PLATFORM, "name": role}, role, {"source": via})
            page.rel(at, prog, P.REQUIRES_ROLE, r, {"via": via})
            if screen is not None:
                page.rel(at, screen, P.REQUIRES_ROLE, r, {"via": via})

        # Data sources the sql: tags name.
        for name, a, b in self.datasources:
            at = page.span(a, b)
            key = {"name": f"datasource:{name}"}
            ds = page.term(at, "jsp:DataSource", key, key["name"], {"source": "jstl"})
            page.rel(at, prog, P.REFERENCES_CONFIG, ds)

        if self.assumed:
            page.note(f"tag prefixes {', '.join(sorted(self.assumed))} used without a taglib directive in this file: read by their conventional libraries")
        page.behavior(page.whole(), self.program, LANGUAGE, body, "page")
        return page.done()


def _library(uri: str) -> str | None:
    u = uri.strip().lower().rstrip("/")
    if u.startswith("urn:jsptagdir:"):
        return "tagdir:" + uri.strip()[len("urn:jsptagdir:"):]
    for suffix, lib in LIBRARY_URIS:
        if u.endswith(suffix.lower()):
            return lib
    return None


def _brace_end(text: str, i: int) -> int:
    """The position just past the `}` closing an expression whose body starts at `i`, or -1."""
    depth = 1
    n = len(text)
    quote = None
    limit = min(n, i + 20000)
    while i < limit:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _title(elements: list[Element]) -> str | None:
    for e in elements:
        if e.tag == "title":
            t = " ".join(e.text.split())
            return t if t and HOLE not in t else None
    return None


__all__ = ["JspSource", "url_of", "webroot"]
