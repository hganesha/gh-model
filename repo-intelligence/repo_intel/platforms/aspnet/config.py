"""`aspnet.config`: ASP.NET `web.config` files, as evidence E1 (a declarative file).

What it declares:

- the `<authorization>` rules of `<system.web>` (for the application, or for the path
  of an enclosing `<location path>`), as one Permission (`aspx:AuthorizationRule`)
  named by that path: `/` + the path in the application when the file roots one, else
  (a folder's file that only sets authorization) the folder's repository path, joined
  with the location path, since the application root is another file. Each role an
  `<allow roles>` names is a Role that GRANTS it. A
  page the path names exactly (an `.aspx` file) REQUIRES the role. `deny users="?"`
  (anonymous users denied) and `allow users="*"` are kept as attributes, never roles;
  user names are not recorded;
- `<connectionStrings>` entries by name (`aspx:ConnectionString`, `connectionstring:<name>`,
  the key the pages use) with the provider; the connection string is never read;
- `<appSettings>` keys (`aspx:AppSetting`, `appsetting:<key>`); values are never read;
- whether the file marks a Web Forms application root (an `app_root` observation,
  `{"root": true | false}`): it does unless it only sets authorization
  (`application_root`). The unit linker keys a page's Endpoint below the nearest root.

Authentication, custom errors, handlers, modules and session state are counted as
constructs, not modelled. A file with entity declarations is not read.
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
REALM = "aspnet"
MAX_ELEMENTS = 50000
_UNMAPPED = {
    "authentication": "aspx:config.authentication", "customErrors": "aspx:config.custom-errors", "httpHandlers": "aspx:config.http-handler",
    "handlers": "aspx:config.http-handler", "httpModules": "aspx:config.http-module", "modules": "aspx:config.http-module",
    "sessionState": "aspx:config.session-state",
}
_NAME = re.compile(r"[\w.\-:/ ]+")


def _split(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def application_root(root: ET.Element) -> bool:
    """Whether a `web.config` marks an application root (contract section 9.2).

    It does unless it only sets authorization: its top-level elements are `system.web`
    and `location` alone, each `location` holds only `system.web`, every `system.web`
    holds only `authorization`, and at least one `authorization` is there. That is the
    file a folder of a site carries to restrict who may read it; anything else (settings,
    connection strings, compilation, handlers, an empty file) is read as an application's
    own configuration. Comments are not elements here (ElementTree drops them)."""
    authorizations = 0

    def web_only(el: ET.Element) -> bool:
        nonlocal authorizations
        if any(part.tag != "authorization" for part in el):
            return False
        authorizations += len(el)
        return True

    for child in root:
        if child.tag == "system.web":
            if not web_only(child):
                return True
        elif child.tag == "location":
            if any(part.tag != "system.web" or not web_only(part) for part in child):
                return True
        else:
            return True
    return authorizations == 0


class AspNetConfig:
    id = "aspnet.config"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        base = posixpath.basename(path).lower()
        return base == "web.config" or (base.startswith("web.") and base.endswith(".config"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        if re.search(r"<!ENTITY", text, re.I):
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "XML with entity declarations is not read")], parsed=False)
        try:
            root = ET.fromstring(re.sub(r"<!DOCTYPE[^>\[]*>", "", text, count=1))
        except ET.ParseError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not well-formed XML: {exc}")], parsed=False)
        if root.tag != "configuration":
            return base.Extraction(parsed=False)
        if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "info", f"configuration has more than {MAX_ELEMENTS} elements: not read")], parsed=False)
        return _Config(path, data, blob, base, root).run()


class _Config:
    def __init__(self, path: str, data: bytes, blob: str, base, root: ET.Element):
        self.path, self.data, self.blob, self.base, self.root = path, data, blob, base, root
        self.items: list = []
        self.cursor = 0
        self.roles: set[str] = set()
        self.declared: set[str] = set()
        self.here = posixpath.dirname(path)
        self.app_root = application_root(root)

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
        marker = repr((term, sorted(key.items())))
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in attrs.items() if v not in (None, [], "")}, E1)
        if marker not in self.declared:
            self.declared.add(marker)
            self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def role(self, at, name: str) -> dict[str, Any]:
        return self.entity(at, "aspx:Role", {"realm": REALM, "name": name}, name, {"source": "web.config"})

    def run(self):
        self.section(self.root, None)
        for loc in (c for c in self.root if c.tag == "location"):
            path = (loc.get("path") or "").strip()
            self.section(loc, path or None)
        # Whether this file marks a Web Forms application root (contract section 9.2); the
        # unit linker reads it in `link_app_relative_routes`.
        self.items.append(self.base.obs("app_root", self.blob, self.base.whole_file(self.data), {"root": application_root(self.root)}, E1))
        return self.base.Extraction(self.items, [])

    def section(self, holder: ET.Element, location: str | None) -> None:
        for child in holder:
            if child.tag == "connectionStrings" and location is None:
                for add in (c for c in child if c.tag == "add"):
                    self.setting(add.get("name"), "connectionstring", "aspx:ConnectionString", "aspx:config.connection-string", add.get("providerName"))
            elif child.tag == "appSettings" and location is None:
                for add in (c for c in child if c.tag == "add"):
                    self.setting(add.get("key"), "appsetting", "aspx:AppSetting", "aspx:config.app-setting", None)
            elif child.tag in ("system.web", "system.webServer"):
                for part in child:
                    if part.tag == "authorization" and child.tag == "system.web":
                        self.authorization(part, location)
                    elif part.tag in _UNMAPPED:
                        self.construct(self.at("<" + part.tag), _UNMAPPED[part.tag], False)
                    elif part.tag == "security":
                        for sub in part:
                            if sub.tag == "authorization":
                                self.construct(self.at("<authorization"), "aspx:config.authorization", False)

    def setting(self, name: str | None, prefix: str, term: str, cid: str, provider: str | None) -> None:
        at = self.at(f'"{name}"' if name else None)
        ok = bool(name) and bool(_NAME.fullmatch(name or ""))
        self.construct(at, cid, ok)
        if ok:
            key = {"name": f"{prefix}:{name}"}
            self.entity(at, term, key, key["name"], {"source": "web.config", "provider": provider if provider and re.fullmatch(r"[\w.]+", provider) else None})

    def authorization(self, el: ET.Element, location: str | None) -> None:
        at = self.at("<authorization")
        rules = [c for c in el if c.tag in ("allow", "deny")]
        if not rules:
            self.construct(at, "aspx:config.authorization", False)
            return
        self.construct(at, "aspx:config.authorization", True)
        if self.app_root:  # a path in the application this file roots
            target = "/" + location.lstrip("~/") if location else "/"
        else:  # a folder's own rules: the root is another file, so the path is the repository's
            target = posixpath.normpath(posixpath.join(self.here, location.lstrip("~/") if location else ""))
        allowed = sorted({r for c in rules if c.tag == "allow" for r in _split(c.get("roles"))})
        denied = sorted({r for c in rules if c.tag == "deny" for r in _split(c.get("roles"))})
        users = [(c.tag, u) for c in rules for u in _split(c.get("users"))]
        attrs: dict[str, Any] = {
            "path": target, "source": "web.config", "roles_denied": denied,
            "anonymous_denied": True if ("deny", "?") in users else None,
            "all_denied": True if ("deny", "*") in users else None,
            "all_allowed": True if ("allow", "*") in users else None,
            "named_users": True if any(u not in ("?", "*") for _, u in users) else None,
            "verbs": sorted({v.upper() for c in rules for v in _split(c.get("verbs"))}),
        }
        perm = self.entity(at, "aspx:AuthorizationRule", {"realm": REALM, "name": target}, target, attrs)
        page = None
        if location and location.lower().endswith(".aspx"):
            full = posixpath.normpath(posixpath.join(self.here, location.lstrip("~/"))) if self.here else posixpath.normpath(location.lstrip("~/"))
            page = {"kind": "Program", "natural_key": {"platform": "aspnet", "name": full}, "name": posixpath.basename(full), "declare": False}
        for name in allowed:
            rat = self.at(name)
            role = self.role(rat, name)
            self.relation(rat, role, P.GRANTS, perm)
            if page is not None:
                self.relation(rat, page, P.REQUIRES_ROLE, role, {"via": "web.config"})
