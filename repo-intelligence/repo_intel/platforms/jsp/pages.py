"""What the server-page packs share: JSP (`jsp`) and ASP.NET (`aspnet`).

A server page is markup with server code in it: `<% ... %>` blocks, `${...}`
expressions, Razor `@...`. Both packs read a page the same way:

1. find the server code regions (each pack has its own syntax) and replace every
   character of each with `HOLE`, so the rest reads as plain markup and every offset
   stays where it was (`mask`);
2. read the masked text with the web pack's linear markup scanner
   (`repo_intel.platforms.web.markup.scan`), which gives elements with character
   offsets; an attribute value holding a `HOLE` is computed, never literal;
3. describe the page's forms and controls as `UiForm` and `UiControl` and let
   `emit_ui` declare them, named by the contract's UIControl rule (section 1), the
   same rule the ColdFusion and web packs follow (`link_name` is the ColdFusion
   pack's, so a link is named the same way on every platform).

`Page` collects one file's observations, as the ColdFusion pack's emitter does.
Behaviour literals go through `scrub_tree` (the SAS pack's rule-7 scrubber): a URL
keeps its scheme, host, path and parameter names, never values, and no mail address
is copied.

This module imports `repo_intel.extractors` modules only inside functions: pack
discovery runs while that package loads (contract section 2, rule 1).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from repo_intel import platforms
from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms.cfml.source import link_name
from repo_intel.platforms.sas.values import scrub, scrub_tree
from repo_intel.platforms.web.markup import Attr, Element, scan

K = EntityKind
P = Predicate
HOLE = "\x01"
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)
_WS = re.compile(r"\s+")

__all__ = [
    "HOLE", "Page", "Region", "UiControl", "UiForm", "attr_map", "decode", "emit_ui", "is_external", "link_name", "literal", "mask",
    "markup", "orig_value", "scrub", "scrub_tree", "split_target",
]


def _base():
    from repo_intel.extractors import base

    return base


def _sql():
    from repo_intel.extractors import sql_lineage

    return sql_lineage


def decode(data: bytes) -> str:
    """UTF-8 (a byte-order mark is kept as a character so offsets still map), else Windows-1252, else Latin-1."""
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


@dataclass
class Region:
    """One server code region: `kind` names the syntax, `code` is what is inside the delimiters."""

    kind: str
    start: int
    end: int
    code: str
    code_start: int


def mask(text: str, regions: list[Region]) -> str:
    """`text` with every character of every region replaced by `HOLE`: offsets do not move."""
    if not regions:
        return text
    out = []
    cursor = 0
    for r in sorted(regions, key=lambda r: r.start):
        if r.start < cursor:
            continue
        out.append(text[cursor:r.start])
        out.append(HOLE * (r.end - r.start))
        cursor = r.end
    out.append(text[cursor:])
    return "".join(out)


def markup(masked: str) -> list[Element]:
    """Elements of the masked page, tag names lower-cased, offsets in characters."""
    return scan(masked, lower=True)


def attr_map(el: Element) -> dict[str, Attr]:
    """Attributes by their name as written, lower-cased (server pages are case-insensitive
    about attribute names; the web scanner's Vue and Angular normalisation is not wanted here)."""
    out: dict[str, Attr] = {}
    for a in el.attrs.values():
        out.setdefault(a.raw.lower(), a)
    return out


def literal(a: Attr | None) -> str | None:
    """The trimmed literal value of an attribute, or None when it is absent, empty or computed."""
    if a is None or a.value is None or HOLE in a.value:
        return None
    v = a.value.strip()
    return v or None


def computed(a: Attr | None) -> bool:
    return a is not None and a.value is not None and HOLE in a.value


def orig_value(text: str, a: Attr) -> str | None:
    """The attribute's value as written in the original text (server code included)."""
    raw = text[a.start:a.end]
    eq = raw.find("=")
    if eq < 0:
        return None
    v = raw[eq + 1:].strip()
    if v[:1] in ("'", '"') and v.endswith(v[0]) and len(v) >= 2:
        return v[1:-1]
    return v


def is_external(target: str) -> bool:
    t = target.strip()
    return bool(_EXTERNAL.match(t)) or t.startswith("#")


def split_target(value: str | None) -> str | None:
    """The literal path of a URL attribute value: the whole value when it is literal, or the
    literal part before a computed query string (`edit.jsp?id=${o.id}` gives `edit.jsp`).
    None when the path itself is computed."""
    if value is None:
        return None
    if HOLE not in value:
        return value.strip()
    head = value.split(HOLE, 1)[0]
    if re.search(r"[?#]", head):
        return re.split(r"[?#]", head, maxsplit=1)[0].strip()
    return None


def collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


# ------------------------------------------------------------------ emission


class Page:
    """One file's observations: entities through platform terms, relations once each,
    construct counts, diagnostics and a lineage summary."""

    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        self.base = _base()
        self.sql = _sql()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self._related: set[str] = set()
        self._said: set[str] = set()
        self._declared: set[str] = set()

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.to_byte(a), self.to_byte(max(a, b)))

    def whole(self):
        return self.base.whole_file(self.data)

    def term(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = EvidenceClass.E2) -> dict[str, Any]:
        o = platforms.platform_entity(self.blob, at, term, key, name, {k: v for k, v in (attrs or {}).items() if v not in (None, [], {}, "")}, ev)
        ref = self.base.ref(o.payload["kind"], key, name)
        marker = repr((o.payload["kind"], sorted(key.items())))
        if marker not in self._declared:  # the first declaration in the file carries the attributes
            self._declared.add(marker)
            self.items.append(o)
        return ref

    def entity(self, at, kind: EntityKind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, at, kind, key, name, attrs or {}, EvidenceClass.E2))
        return self.base.ref(kind, key, name)

    def rel(self, at, subject: dict[str, Any] | str, predicate: Predicate, obj: dict[str, Any] | str, qualifiers: dict[str, Any] | None = None, ev: EvidenceClass = EvidenceClass.E2) -> None:
        marker = repr((subject, predicate, obj, sorted((qualifiers or {}).items())))
        if marker in self._related:  # the same claim again: the first occurrence is its evidence
            return
        self._related.add(marker)
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, ev))

    def construct(self, construct_id: str, a: int, b: int, modelled: bool, ev: EvidenceClass = EvidenceClass.E2) -> None:
        self.items.append(platforms.construct(self.blob, self.span(a, b), construct_id, modelled, ev))

    def note(self, message: str, severity: str = "info") -> None:
        message = scrub(message)
        if message not in self._said:
            self._said.add(message)
            self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def behavior(self, at, owner: dict[str, Any], language: str, body: list, kind: str) -> None:
        from repo_intel.compile.behavior import behavior_observation

        try:
            self.items.append(behavior_observation(self.blob, at, owner, language, scrub_tree(body), [], kind=kind))
        except ValueError as exc:  # a lowering bug must not lose the file's entities
            self.note(f"behaviour of {owner['name']} not emitted: {exc}", "warning")

    def query_lineage(self, subject: dict[str, Any], sql_text: str, offset: int, dynamic: list[tuple[int, int]]) -> None:
        """READS_FROM and WRITES_TO for the tables of `sql_text` (which starts at character
        `offset` of the file). `dynamic` holds the spans, relative to `sql_text`, of
        server expressions masked into it: a table named through one is not resolved."""
        found = self.sql.lineage(sql_text, None)
        for items in (found.writes, found.reads):
            keep = []
            for name, a, b in items:
                if any(s < b and a < e for s, e in dynamic):
                    self.note("query names a table computed at run time: table not resolved")
                    self.unresolved += 1
                else:
                    keep.append((name, a, b))
            items[:] = keep
        self.resolved += len(found.writes) + len(found.reads)
        self.unresolved += found.unresolved
        self.items += self.sql.lineage_relations(self.blob, self.data, self.text, found, subject, offset=offset, to_byte=self.to_byte)

    def done(self, parsed: bool = True):
        return self.base.Extraction(self.items + self.sql.lineage_summary(self.blob, self.data, self.resolved, self.unresolved), self.diagnostics, parsed)


# ------------------------------------------------------------------ forms and controls


@dataclass
class UiForm:
    start: int  # the opening tag, for evidence
    end: int
    extent: tuple[int, int]  # the controls whose start falls inside belong to the form
    construct: str
    names: list[str | None]  # literal name, then id; None where absent or computed
    dynamic_name: bool = False
    attrs: dict[str, Any] = field(default_factory=dict)
    submits: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)


@dataclass
class UiControl:
    start: int
    end: int
    construct: str
    ctype: str  # input type, or select, textarea, button, link
    names: list[str | None]  # literal name, id, then binding path
    dynamic_name: bool = False
    link: str | None = None  # a link's literal target, for its `link:<target>` name
    button: str | None = None  # "submit" for a submit button, "button" for another button
    label: str | None = None  # a button's literal text
    attrs: dict[str, Any] = field(default_factory=dict)
    navigates: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    form_submits: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # targets this control posts its form to
    key: str | None = None  # an id other parts of the page refer to it by (a Web Forms validator's ControlToValidate)
    term: str | None = None  # the platform term to declare it with, when not the page's default


def _control_name(c: UiControl, container: str, counts: dict[tuple[str, str], int]) -> str:
    for candidate in c.names:
        if candidate and candidate.strip():
            return candidate.strip()
    if c.ctype == "link" and c.link is not None:
        target = link_name(c.link)
        if target:
            return "link:" + target
    if c.button == "submit":
        return "submit"
    if c.button == "button" and c.label and HOLE not in c.label and len(c.label) <= 40:
        return "button:" + c.label
    counts[(container, c.ctype)] = counts.get((container, c.ctype), 0) + 1
    return f"{c.ctype}{counts[(container, c.ctype)]}"


def emit_ui(page: Page, screen: dict[str, Any], screen_name: str, forms: list[UiForm], controls: list[UiControl], form_term: str, control_term: str,
            validation: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, str]:
    """Declare forms and controls under `screen`, in document order. A control belongs to
    the innermost form whose extent holds its start. Returns each control's `key` mapped
    to the name it was given."""
    forms = sorted(forms, key=lambda f: f.start)
    form_refs: list[tuple[UiForm, dict[str, Any], str]] = []
    for n, f in enumerate(forms, start=1):
        fname = next((x.strip() for x in f.names if x and x.strip()), None) or f"form{n}"
        at = page.span(f.start, f.end)
        attrs = dict(f.attrs)
        if f.dynamic_name:
            attrs["name_dynamic"] = True
        ref = page.term(at, form_term, {"screen": screen_name, "name": fname}, fname, attrs)
        page.rel(at, screen, P.CONTAINS, ref)
        for target, qualifiers in f.submits:
            page.rel(at, ref, P.SUBMITS_TO, target, qualifiers)
        page.construct(f.construct, f.start, f.end, True)
        form_refs.append((f, ref, f"{screen_name}#{fname}"))
    counts: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], int] = {}
    named: dict[str, str] = {}
    for c in sorted(controls, key=lambda c: c.start):
        holder = None
        for f, ref, container in form_refs:
            if f.extent[0] <= c.start < f.extent[1] and (holder is None or f.extent[0] >= holder[0].extent[0]):
                holder = (f, ref, container)
        container = holder[2] if holder else screen_name
        base_name = _control_name(c, container, counts)
        n = seen[(container, base_name)] = seen.get((container, base_name), 0) + 1
        cname = base_name if n == 1 else f"{base_name}~{n}"
        attrs = {"control": c.ctype, **c.attrs}
        if c.dynamic_name:
            attrs["name_dynamic"] = True
        if validation and c.key and c.key in validation:
            attrs["validation"] = validation[c.key]
        at = page.span(c.start, c.end)
        ref = page.term(at, c.term or control_term, {"container": container, "name": cname}, cname, attrs)
        page.rel(at, holder[1] if holder else screen, P.CONTAINS, ref)
        for target, qualifiers in c.navigates:
            page.rel(at, ref, P.NAVIGATES_TO, target, qualifiers)
        if holder:
            for target, qualifiers in c.form_submits:
                page.rel(at, holder[1], P.SUBMITS_TO, target, {**qualifiers, "control": cname})
        page.construct(c.construct, c.start, c.end, True)
        if c.key:
            named.setdefault(c.key, cname)
    return named


def button_label(el: Element) -> str | None:
    """A `<button>`'s literal text (all its descendant text, collapsed), or an input's literal value."""
    if el.tag == "button":
        text = collapse(el.deep_text)
        return None if not text or HOLE in text else text
    return literal(attr_map(el).get("value"))


def candidates(path: str, rel: str) -> list[str]:
    """`rel` joined to every ancestor directory of `path`, deepest first: an application-root
    path (`~/x`, `/x`) is looked up under each directory the application root could be, and
    only a file the unit has is kept (non-declaring references)."""
    rel = rel.lstrip("/")
    parts = posixpath.dirname(path).split("/") if posixpath.dirname(path) else []
    out = []
    for depth in range(len(parts), -1, -1):
        out.append(posixpath.normpath(posixpath.join(*parts[:depth], rel)) if depth else posixpath.normpath(rel))
    return list(dict.fromkeys(p for p in out if not p.startswith("..")))


def relative(path: str, target: str) -> str | None:
    """A relative URL or file reference resolved against the directory of `path`; None when it climbs out."""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return None if resolved.startswith("..") or resolved == "." else resolved


def endpoint_refs(route: str, methods: tuple[str, ...]) -> list[dict[str, Any]]:
    """Endpoints a literal URL may reach, by the key server extractors declare, never declared from here."""
    base = _base()
    return [base.ref(K.ENDPOINT, {"component_root": base.UNIT, "protocol": "http", "method": m, "route": route}, f"{m} {route}", declare=False) for m in methods]


Lowered = list[dict[str, Any]]
ExprOf = Callable[[str], dict[str, Any]]
