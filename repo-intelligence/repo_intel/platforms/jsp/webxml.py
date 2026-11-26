"""`jsp.webxml`: the servlet deployment descriptor (`WEB-INF/web.xml`, `web-fragment.xml`).

What it declares, as evidence E1 (a declarative file):

- each URL pattern of a `<servlet-mapping>` is an Endpoint (`jsp:Servlet`, method
  `ANY`, route the pattern as written) with the servlet's name and class in
  attributes; a servlet that is a JSP file (`<jsp-file>`) is that page's Program,
  which EXPOSES the Endpoint;
- each `<security-constraint>` is a Permission (`jsp:SecurityConstraint`) named by its
  URL patterns and HTTP methods; each role of its `<auth-constraint>` is a Role that
  GRANTS it; a servlet Endpoint whose pattern the constraint covers, and a JSP page
  the constraint names exactly, REQUIRE the role. `*` (any authenticated user) is
  kept as an attribute, not a role, and an empty auth-constraint as `denied`;
- `<security-role>` names are Roles; `<resource-ref>` names are data sources
  (`jsp:DataSource`), the key the `sql:` tags use.

Filters, listeners, context parameters, error pages and the rest are counted as
constructs, not modelled. Values of context parameters, environment entries and
login configuration are never read. A descriptor with entity declarations is not read.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1
MAX_ELEMENTS = 50000
_UNMAPPED = {
    "login-config": "jsp:web.login-config", "filter": "jsp:web.filter", "filter-mapping": "jsp:web.filter-mapping", "listener": "jsp:web.listener",
    "context-param": "jsp:web.context-param", "env-entry": "jsp:web.env-entry", "error-page": "jsp:web.error-page", "welcome-file": "jsp:web.welcome-file",
    "session-config": "jsp:web.session-config", "ejb-ref": "jsp:web.ejb-ref", "ejb-local-ref": "jsp:web.ejb-ref", "jsp-config": "jsp:web.jsp-config",
    "mime-mapping": "jsp:web.mime-mapping",
}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name and c.text and c.text.strip():
            return c.text.strip()
    return None


def _texts(el: ET.Element, name: str) -> list[str]:
    return [c.text.strip() for c in el if _local(c.tag) == name and c.text and c.text.strip()]


def covers(pattern: str, route: str) -> bool:
    """Whether a security constraint's URL pattern covers a servlet mapping's pattern (servlet specification matching)."""
    if pattern == "/" or pattern == "/*":
        return True
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        return route == prefix or route.startswith(prefix + "/")
    if pattern.startswith("*."):
        return route.endswith(pattern[1:]) or route == pattern
    return route == pattern


class JspWebXml:
    id = "jsp.webxml"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path).lower() in ("web.xml", "web-fragment.xml")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        if re.search(r"<!ENTITY", text, re.I):
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "XML with entity declarations is not read")], parsed=False)
        try:
            root = ET.fromstring(re.sub(r"<!DOCTYPE[^>\[]*>", "", text, count=1))  # an external DTD reference is never fetched
        except ET.ParseError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not well-formed XML: {exc}")], parsed=False)
        if _local(root.tag) not in ("web-app", "web-fragment"):
            return base.Extraction(parsed=False)
        return _Descriptor(path, data, blob, text, base, root).run()


class _Descriptor:
    def __init__(self, path: str, data: bytes, blob: str, text: str, base, root: ET.Element):
        self.path, self.data, self.blob, self.text, self.base, self.root = path, data, blob, text, base, root
        self.items: list = []
        self.diagnostics: list = []
        self.cursor = 0
        self.roles: set[str] = set()
        # web.xml sits in <web root>/WEB-INF/.
        here = posixpath.dirname(path)
        self.webroot = posixpath.dirname(here) + "/" if posixpath.basename(here).upper() == "WEB-INF" and posixpath.dirname(here) else ""

    def at(self, needle: str | None):
        """Span of the next occurrence of `needle` (elements are visited in document order)."""
        if needle:
            raw = needle.encode()
            i = self.data.find(raw, self.cursor)
            if i < 0:
                i = self.data.find(raw)
            if i >= 0:
                self.cursor = i + len(raw)
                return self.base.span(self.data, i, i + len(raw))
        return self.base.whole_file(self.data)

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any]) -> dict[str, Any]:
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in attrs.items() if v not in (None, [], "")}, E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def role(self, at, name: str, source: str) -> dict[str, Any]:
        """A Role, declared once per file (by its first mention)."""
        key = {"realm": "jsp", "name": name}
        if name in self.roles:
            return self.base.ref(K.ROLE, key, name)
        self.roles.add(name)
        return self.entity(at, "jsp:Role", key, name, {"source": source})

    def run(self):
        count = sum(1 for _ in self.root.iter())
        if count > MAX_ELEMENTS:
            return self.base.Extraction(diagnostics=[self.base.diagnostic("jsp.webxml", "info", f"descriptor has more than {MAX_ELEMENTS} elements: not read")], parsed=False)
        servlets: dict[str, dict[str, str | None]] = {}
        for s in _children(self.root, "servlet"):
            name = _text(s, "servlet-name")
            if name:
                servlets[name] = {"class": _text(s, "servlet-class"), "jsp_file": _text(s, "jsp-file")}
        endpoints: list[tuple[str, dict[str, Any]]] = []
        for child in self.root:
            tag = _local(child.tag)
            if tag == "servlet":
                name = _text(child, "servlet-name")
                mapped = any(_text(m, "servlet-name") == name for m in _children(self.root, "servlet-mapping"))
                self.construct(self.at(name), "jsp:web.servlet", bool(name) and mapped)
            elif tag == "servlet-mapping":
                endpoints += self.mapping(child, servlets)
            elif tag == "security-role":
                role = _text(child, "role-name")
                at = self.at(role)
                self.construct(at, "jsp:web.security-role", bool(role))
                if role and role != "*":
                    self.role(at, role, "security-role")
            elif tag == "resource-ref" or tag == "resource-env-ref":
                name = _text(child, "res-ref-name") or _text(child, "resource-env-ref-name")
                at = self.at(name)
                rtype = _text(child, "res-type") or _text(child, "resource-env-ref-type") or ""
                is_ds = "datasource" in rtype.lower() or (name or "").lower().startswith("jdbc/")
                self.construct(at, "jsp:web.resource-ref", bool(name) and is_ds)
                if name and is_ds:
                    key = {"name": f"datasource:{name}"}
                    self.entity(at, "jsp:DataSource", key, key["name"], {"source": "web.xml", "resource_type": rtype if re.fullmatch(r"[\w.$]+", rtype) else None})
            elif tag == "welcome-file-list":
                for w in _children(child, "welcome-file"):
                    self.construct(self.at(w.text.strip() if w.text else None), "jsp:web.welcome-file", False)
            elif tag in _UNMAPPED:
                self.construct(self.at("<" + tag), _UNMAPPED[tag], False)
        for c in _children(self.root, "security-constraint"):
            self.constraint(c, endpoints)
        return self.base.Extraction(self.items, self.diagnostics)

    def mapping(self, el: ET.Element, servlets: dict[str, dict[str, str | None]]) -> list[tuple[str, dict[str, Any]]]:
        name = _text(el, "servlet-name")
        out = []
        for pattern in _texts(el, "url-pattern"):
            at = self.at(pattern)
            self.construct(at, "jsp:web.servlet-mapping", bool(name))
            if not name:
                continue
            info = servlets.get(name, {})
            key = {"component_root": self.base.UNIT, "protocol": "http", "method": "ANY", "route": pattern}
            attrs = {"framework": "servlet", "servlet": name, "servlet_class": info.get("class") if info.get("class") and re.fullmatch(r"[\w.$]+", info["class"]) else None}
            ep = self.entity(at, "jsp:Servlet", key, f"ANY {pattern}", attrs)
            jsp_file = info.get("jsp_file")
            if jsp_file:
                target = posixpath.normpath(self.webroot + jsp_file.lstrip("/"))
                self.relation(at, {"kind": "Program", "natural_key": {"platform": "jsp", "name": target}, "name": posixpath.basename(target), "declare": False}, P.EXPOSES, ep)
            out.append((pattern, ep))
        return out

    def constraint(self, el: ET.Element, endpoints: list[tuple[str, dict[str, Any]]]) -> None:
        collections = _children(el, "web-resource-collection")
        patterns = sorted({p for c in collections for p in _texts(c, "url-pattern")})
        methods = sorted({m.upper() for c in collections for m in _texts(c, "http-method")})
        omitted = sorted({m.upper() for c in collections for m in _texts(c, "http-method-omission")})
        title = next((t for c in collections for t in _texts(c, "web-resource-name")), None)
        at = self.at(title or (patterns[0] if patterns else None))
        if not patterns:
            self.construct(at, "jsp:web.security-constraint", False)
            return
        self.construct(at, "jsp:web.security-constraint", True)
        auth = _children(el, "auth-constraint")
        roles = sorted({r for a in auth for r in _texts(a, "role-name")})
        transport = next((t for u in _children(el, "user-data-constraint") for t in _texts(u, "transport-guarantee")), None)
        name = " ".join(patterns) if not methods else f"{','.join(methods)} {' '.join(patterns)}"
        attrs: dict[str, Any] = {"url_patterns": patterns, "methods": methods, "methods_omitted": omitted, "resource": title,
                                 "transport": transport if transport in ("NONE", "INTEGRAL", "CONFIDENTIAL") else None, "source": "web.xml"}
        if auth and not roles:
            attrs["denied"] = True
        if "*" in roles or "**" in roles:
            attrs["any_authenticated"] = True
        perm = self.entity(at, "jsp:SecurityConstraint", {"realm": "jsp", "name": name}, name, attrs)
        for role_name in (r for r in roles if r not in ("*", "**")):
            rat = self.at(role_name)
            role = self.role(rat, role_name, "security-constraint")
            self.relation(rat, role, P.GRANTS, perm)
            for route, ep in endpoints:
                if any(covers(p, route) for p in patterns):
                    self.relation(rat, ep, P.REQUIRES_ROLE, role, {"via": "security-constraint"})
            for p in patterns:
                if p.lower().endswith((".jsp", ".jspx")) and "*" not in p:
                    target = posixpath.normpath(self.webroot + p.lstrip("/"))
                    page = {"kind": "Program", "natural_key": {"platform": "jsp", "name": target}, "name": posixpath.basename(target), "declare": False}
                    self.relation(rat, page, P.REQUIRES_ROLE, role, {"via": "security-constraint"})
