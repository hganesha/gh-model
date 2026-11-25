"""What the web scanners found, as entities, relations, constructs and diagnostics.

`Out` collects one file's observations. `emit_ui` turns a file's elements (from the
markup scanner or from JSX) into forms and controls under the file's screen, with
their bindings, submit targets and navigation. `emit_tokens` and `emit_style_refs`
handle design tokens.

References to things other files declare (a route another file declares, an
Endpoint a server file declares, a token in an imported stylesheet) are made
without declaring them (`declare=False`): the compiler keeps such a relation only
when something in the same unit declares the target, so nothing is guessed into
existence. They carry evidence class E3 (resolved across files).

`repo_intel.extractors.base` is imported inside functions (`_base()`), never at module
level: pack discovery runs while `repo_intel.extractors` loads and refuses a pack that is
still importing (docs/solution-ir-contract.md, section 2, rule 1).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from repo_intel import platforms
from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms.web import styles
from repo_intel.platforms.web.markup import Attr, Element

K = EntityKind
P = Predicate
PLATFORM = "web"
REALM = "web"
_TERM_KIND = {"web:Route": K.SCREEN, "web:Page": K.SCREEN, "web:View": K.SCREEN, "web:Form": K.FORM, "web:Control": K.UI_CONTROL,
              "web:CssVariable": K.STYLE_TOKEN, "web:ScssVariable": K.STYLE_TOKEN, "web:ThemeToken": K.STYLE_TOKEN, "web:Role": K.ROLE}


def _base():
    """`repo_intel.extractors.base`, imported on use (see the module docstring)."""
    from repo_intel.extractors import base

    return base


def screen_ref(name: str, declare: bool = True) -> dict[str, Any]:
    return _base().ref(K.SCREEN, {"platform": PLATFORM, "name": name}, name, declare)


def token_ref(system: str, name: str, declare: bool = True) -> dict[str, Any]:
    return _base().ref(K.STYLE_TOKEN, {"system": system, "name": name}, name, declare)


def endpoint_ref(method: str, route: str) -> dict[str, Any]:
    """The Endpoint a server in this unit would declare for `method route` (the key the
    TypeScript, Python and other source extractors use), never declared from here."""
    base = _base()
    return base.ref(K.ENDPOINT, {"component_root": base.UNIT, "protocol": "http", "method": method, "route": route}, f"{method} {route}", declare=False)


def clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and v != [] and v != ""}


class Out:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str):
        self.id, self.path, self.data, self.blob = extractor_id, path, data, blob
        self._base = _base()
        self.result = self._base.Extraction()
        self._said: set[str] = set()
        self._related: set[str] = set()

    def at(self, a: int, b: int):
        return self._base.span(self.data, a, b)

    def construct(self, construct_id: str, a: int, b: int, modelled: bool) -> None:
        self.result.observations.append(platforms.construct(self.blob, self.at(a, b), construct_id, modelled))

    def entity(self, term: str, key: dict[str, Any], name: str, a: int, b: int, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        self.result.observations.append(platforms.platform_entity(self.blob, self.at(a, b), term, key, name, clean(attrs or {})))
        return self._base.ref(_TERM_KIND[term], key, name)

    def relate(self, subject: dict[str, Any] | str, predicate: Predicate, obj: dict[str, Any] | str, a: int, b: int, qualifiers: dict[str, Any] | None = None, cls: EvidenceClass = EvidenceClass.E2) -> None:
        key = repr((subject, predicate, obj, sorted((qualifiers or {}).items())))
        if key in self._related:  # the same claim again in this file: the first occurrence is its evidence
            return
        self._related.add(key)
        self.result.observations.append(self._base.relation(self.blob, self.at(a, b), subject, predicate, obj, qualifiers, cls))

    def diag(self, message: str, severity: str = "info") -> None:
        if message not in self._said:
            self._said.add(message)
            self.result.diagnostics.append(self._base.diagnostic(self.id, severity, message))


@dataclass
class Owner:
    """The screen a file's forms, controls and file-level references belong to."""

    name: str
    ref: dict[str, Any]


def renders(out: Out, screen: dict[str, Any], a: int, b: int, component: str | None) -> None:
    """The unit's package RENDERS a screen. A component is a Symbol the linker declares
    with a key one file cannot build, so it is named as a qualifier."""
    out.relate(_base().PACKAGE, P.RENDERS, screen, a, b, {"component": component} if component else {})


def emit_roles(out: Out, subject: dict[str, Any], roles: list[tuple[str, int, int]]) -> None:
    seen: set[str] = set()
    for name, a, b in roles:
        if name in seen:
            continue
        seen.add(name)
        role = out.entity("web:Role", {"realm": REALM, "name": name}, name, a, b)
        out.relate(subject, P.REQUIRES_ROLE, role, a, b)
        out.construct("web:route-role", a, b, True)


# ------------------------------------------------------------------ forms and controls

FORM_TAGS = frozenset({"Form", "el-form", "ElForm", "v-form", "VForm", "a-form", "AForm", "q-form", "QForm", "b-form", "BForm"})
LIBRARY_CONTROLS = {
    "input": {"Input", "TextField", "TextInput", "Field", "Checkbox", "Radio", "Switch", "DatePicker", "InputNumber", "NumberInput", "PasswordInput",
              "el-input", "ElInput", "el-input-number", "el-checkbox", "el-radio", "el-switch", "el-date-picker", "v-text-field", "VTextField",
              "v-checkbox", "v-radio", "v-switch", "a-input", "q-input", "b-form-input", "mat-checkbox", "ion-input", "Form.Control", "Form.Check"},
    "select": {"Select", "Autocomplete", "Combobox", "NativeSelect", "el-select", "ElSelect", "v-select", "VSelect", "v-autocomplete", "a-select",
               "q-select", "mat-select", "b-form-select", "ion-select", "Form.Select"},
    "textarea": {"Textarea", "TextArea", "v-textarea", "b-form-textarea", "ion-textarea"},
    "button": {"Button", "IconButton", "LoadingButton", "el-button", "ElButton", "v-btn", "VBtn", "a-button", "q-btn", "b-button", "ion-button"},
    "link": {"Link", "NavLink", "RouterLink", "router-link", "nuxt-link", "NuxtLink"},
}
_LIBRARY_KIND = {tag: kind for kind, tags in LIBRARY_CONTROLS.items() for tag in tags}
_NATIVE = {"input": "input", "select": "select", "textarea": "textarea", "button": "button"}
_VALIDATION = ("required", "pattern", "min", "max", "minlength", "maxlength", "minLength", "maxLength", "step", "rules")
_REGISTER = re.compile(r"""\bregister\(\s*['"`]([\w.\[\]-]+)['"`]""")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)


def classify(el: Element) -> tuple[str, str] | None:
    """(control type or "form", construct id) for a form or control element, else None."""
    tag = el.tag
    if tag == "form":
        return "form", "web:form"
    if tag in FORM_TAGS:
        return "form", "web:library-form"
    if tag in _NATIVE:
        kind = _NATIVE[tag]
        if tag == "input" and (el.lit("type") or "").lower() in ("submit", "button", "reset", "image"):
            kind = "button"
        if tag == "input" and (el.lit("type") or "").lower() == "hidden":
            return None
        return kind, "web:button" if kind == "button" else "web:" + tag
    if tag == "a":
        return ("link", "web:link") if el.has("href") or el.has("routerLink") else None
    if tag in _LIBRARY_KIND:
        kind = _LIBRARY_KIND[tag]
        if tag == "Field":
            as_ = el.lit("as", "component") or ""
            kind = "select" if as_ == "select" else "textarea" if as_ == "textarea" else "input"
        return kind, "web:link" if kind == "link" else "web:library-control"
    if el.has("routerLink"):
        return "link", "web:link"
    return None


def static(attr: Attr | None) -> str | None:
    """A literal attribute value, or a bound expression that is only a quoted string."""
    if attr is None or attr.value is None:
        return None
    if attr.literal:
        return attr.value
    v = attr.value.strip()
    m = re.fullmatch(r"""(['"`])([^'"`$]*)\1""", v) or re.fullmatch(r"""\[\s*(['"])([^'"]*)\1\s*\]""", v)
    return m.group(2) if m else None


def nav_target(el: Element) -> tuple[str | None, Attr | None]:
    attr = el.get("to", "href", "routerLink")
    return static(attr), attr


def link_targets(path: str, target: str) -> tuple[str, list[str]]:
    """(kind, screen names it can reach): route, page, external, anchor or relative."""
    if _EXTERNAL.match(target):
        return "external", []
    if target.startswith("#") or not target:
        return "anchor", []
    clean_target = target.split("#")[0].split("?")[0]
    if clean_target.startswith("/"):
        return "route", [clean_target.rstrip("/") or "/"]
    if clean_target.lower().endswith((".html", ".htm")):
        page = posixpath.normpath(posixpath.join(posixpath.dirname(path), clean_target))
        return ("page", [page]) if not page.startswith("..") else ("relative", [])
    return "relative", []


def has_ui(elements: list[Element], path: str) -> bool:
    """A file holds UI worth a View: a form, or a link to a screen."""
    for el in elements:
        c = classify(el)
        if c is None:
            continue
        if c[0] == "form":
            return True
        target, _ = nav_target(el)
        if target is not None and link_targets(path, target)[0] in ("route", "page"):
            return True
    return False


SubmitResolver = Callable[[Element, Attr], tuple[str | None, list[Any] | None, str | None]]


def emit_ui(out: Out, owner: Owner | None, elements: list[Element], resolve: SubmitResolver | None = None, library: str | None = None) -> None:
    """Forms and controls under `owner` (counted but not modelled when there is none)."""
    forms: dict[int, tuple[str, dict[str, Any]]] = {}
    counters: dict[tuple[str, str], int] = {}
    order = 0
    for el in elements:
        _element_constructs(out, el)
        c = classify(el)
        if c is None:
            continue
        kind, cid = c
        out.construct(cid, el.start, el.end, owner is not None)
        if kind == "form":
            order += 1
            if owner is not None:
                forms[id(el)] = _form(out, owner, el, order, resolve, library)
            continue
        form = next((forms[id(a)] for a in el.ancestors() if id(a) in forms), None)
        binds = _binding(el)
        validation = _validation(el)
        if binds is not None:
            out.construct("web:binding", el.start, el.end, owner is not None)
        if validation:
            out.construct("web:client-validation", el.start, el.end, owner is not None)
        target, target_attr = nav_target(el)
        if owner is None:
            continue
        container = f"{owner.name}#{form[0]}" if form else owner.name
        # n is the control's position among every control of its type in the container, named
        # or not, the way a form's `form<n>` counts every form on the screen.
        n = counters[(container, kind)] = counters.get((container, kind), 0) + 1
        name = _control_name(el, kind, binds, target, n)
        attrs = {
            "control_type": kind, "tag": el.tag, "input_type": el.lit("type") if el.tag in ("input", "Input", "TextField", "Field") else None,
            "binds": binds, "validation": validation or None, "label": _label(el, kind),
            "role": el.lit("role"),
        }
        nav_kind, screens = link_targets(out.path, target) if target is not None else (None, [])
        if target is not None:
            attrs["target"] = target.split("?")[0].split("#")[0] if nav_kind != "external" else _host(target)
            attrs["external"] = True if nav_kind == "external" else None
        elif target_attr is not None:
            attrs["target"] = "dynamic"
            out.diag(f"{el.tag} at byte {target_attr.start} navigates to {str(target_attr.value)[:60]!r}, which is not a literal")
        control = out.entity("web:Control", {"container": container, "name": name}, name, el.start, el.end, attrs)
        out.relate(form[1] if form else owner.ref, P.CONTAINS, control, el.start, el.end)
        for screen in screens:
            out.relate(control, P.NAVIGATES_TO, screen_ref(screen, declare=False), el.start, el.end, cls=EvidenceClass.E3)


def _form(out: Out, owner: Owner, el: Element, order: int, resolve: SubmitResolver | None, library: str | None) -> tuple[str, dict[str, Any]]:
    name = el.lit("name") or el.lit("id") or f"form{order}"
    method = (el.lit("method") or "").upper() or None
    attrs: dict[str, Any] = {"tag": el.tag, "method": method, "form_library": _form_library(el, library), "binds": _form_binding(el)}
    if attrs["form_library"]:
        out.construct("web:form-library", el.start, el.end, True)
    targets: list[tuple[str, str, int, int, str]] = []  # (method, path, start, end, how)
    action = el.get("action")
    if action is not None:
        value = static(action)
        if value is None:
            attrs["action"] = "dynamic"
            out.diag(f"form {name} at byte {el.start}: action {str(action.value)[:60]!r} is not a literal")
            out.construct("web:form-action", action.start, action.end, False)
        else:
            kind, screens = link_targets(out.path, value) if value else ("anchor", [])
            attrs["action"] = _host(value) if kind == "external" else value.split("?")[0]
            if kind in ("route", "page"):
                targets.append((method or "GET", screens[0], action.start, action.end, "action"))
            else:
                out.diag(f"form {name} at byte {el.start}: action {attrs['action']!r} is {'on another host' if kind == 'external' else 'relative to a URL this file does not show'}")
                out.construct("web:form-action", action.start, action.end, False)
    handler_attr = el.get("on:submit", "on:ngSubmit", "on:finish")
    if handler_attr is None:
        for a in el.ancestors():  # Formik holds the submit handler around its <Form>
            if a.tag == "Formik" and a.get("on:submit") is not None:
                handler_attr = a.get("on:submit")
                attrs["form_library"] = attrs["form_library"] or "formik"
                break
    if handler_attr is not None:
        handler, calls, lib = resolve(el, handler_attr) if resolve is not None else (None, None, None)
        handler = handler or _simple_name(handler_attr.value)
        attrs["submit_handler"] = handler
        attrs["form_library"] = attrs["form_library"] or lib
        found = False
        for call in calls or []:
            if call.path is None:
                out.diag(f"form {name} submits through {handler or 'a handler'} to a URL that is not a literal ({call.url or 'an expression'}) at byte {call.start}")
            elif call.external:
                out.diag(f"form {name} submits through {handler or 'a handler'} to another host at byte {call.start}")
            elif call.method == "DYNAMIC":
                out.diag(f"form {name} submits to {call.path} with a method that is not a literal at byte {call.start}")
            else:
                targets.append((call.method, call.path, call.start, call.end, "handler"))
                found = True
        if not found and calls is not None and not calls:
            out.diag(f"form {name} at byte {el.start}: handler {handler or '(inline)'} makes no HTTP call this file shows")
        elif calls is None:
            out.diag(f"form {name} at byte {el.start}: handler {handler or str(handler_attr.value)[:40]!r} is not defined in this file")
        out.construct("web:submit-handler", handler_attr.start, handler_attr.end, found)
    attrs["submits"] = sorted({f"{m} {p}" for m, p, *_ in targets}) or None
    form = out.entity("web:Form", {"screen": owner.name, "name": name}, name, el.start, el.end, attrs)
    out.relate(owner.ref, P.CONTAINS, form, el.start, el.end)
    for method, path, a, b, how in targets:
        out.relate(form, P.SUBMITS_TO, endpoint_ref(method, path), a, b, cls=EvidenceClass.E3)
        if how == "action" or method == "GET":
            out.relate(form, P.SUBMITS_TO, screen_ref(path, declare=False), a, b, cls=EvidenceClass.E3)
        if how == "action":
            out.construct("web:form-action", a, b, True)
        out.diag(f"form {name} submits {method} {path}: it links to an Endpoint only when this unit declares one; calls to other services are left to the HTTP linker")
    return name, form


def _form_library(el: Element, library: str | None) -> str | None:
    if el.has("formGroup"):
        return "angular-reactive-forms"
    if el.tag == "form" and any(a.raw.startswith("#") and a.value == "ngForm" for a in el.attrs.values()):
        return "angular-template-forms"
    if el.tag in FORM_TAGS and el.tag != "form":
        return library or el.tag
    return None


def _form_binding(el: Element) -> str | None:
    a = el.get("formGroup", "model", ":model")
    return _name_of(a.value) if a is not None and a.value else None


def _binding(el: Element) -> str | None:
    for key in ("v-model", "ngModel", "formControl", "value-binding"):
        a = el.get(key)
        if a is not None and a.value:
            return _name_of(a.value)
    v = el.lit("formControlName")
    if v:
        return v
    for s in el.spreads:
        m = _REGISTER.search(s.value or "")
        if m:
            return m.group(1)
    return el.lit("name")


def _validation(el: Element) -> list[str]:
    out = [k.lower() for k in _VALIDATION if el.has(k) and k != "rules"]
    rules = el.get("rules")
    if rules is not None:
        if rules.literal and rules.value:
            out += [r.split(":")[0].strip() for r in rules.value.split("|") if r.strip()]
        else:
            out.append("rules")
    for s in el.spreads:
        m = re.search(r"register\([^,)]*,\s*\{(.*)\}\s*\)", s.value or "", re.S)
        if m:
            out += re.findall(r"(\w+)\s*:", m.group(1))
    return sorted(set(out))


def _control_name(el: Element, kind: str, binds: str | None, target: str | None, n: int) -> str:
    """A control's name, in the contract's order of preference: its name, its id, its
    binding path, `link:<target>` for a link with a literal target, `submit` or
    `button:<text>` for a button, else `<type><n>` with `n` its position among the
    container's controls of that type in document order."""
    name = (el.lit("name") or "").strip() or (el.lit("id") or "").strip() or el.lit("formControlName") or binds
    if name:
        return name
    if kind == "link" and target:
        if _EXTERNAL.match(target):
            return "link:" + _host(target)  # the host only: a path or query on another host can carry identifiers
        path = target.split("?")[0].split("#")[0]
        if path:
            return "link:" + path
        if target.startswith("#") and len(target) > 1:
            return "link:" + target  # an anchor on the same page
    if kind == "button":
        if (el.lit("type") or "").lower() == "submit":
            return "submit"
        text = el.text if el.tag not in ("input", "Input") else (el.lit("value") or "").strip()
        if text and len(text) <= 40 and "{" not in text:
            return "button:" + text
    return f"{kind}{n}"


def _label(el: Element, kind: str) -> str | None:
    label = el.lit("aria-label", "label", "placeholder")
    if label is None and kind in ("button", "link") and el.text and "{" not in el.text:
        label = el.text
    return label[:60] if label else None


def _name_of(expr: str) -> str | None:
    v = expr.strip().removeprefix("this.")
    return v if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", v) else None


def _simple_name(expr: str | None) -> str | None:
    m = re.match(r"^\s*(?:this\.)?([A-Za-z_$][\w$]*)", expr or "")
    return m.group(1) if m else None


def _host(url: str) -> str:
    m = re.match(r"^([a-z][a-z0-9+.-]*:)?//([^/?#@]*@)?([^/?#]*)", url, re.I)
    return (m.group(1) or "") + "//" + m.group(3) if m else url.split(":")[0] + ":"


def _element_constructs(out: Out, el: Element) -> None:
    tag = el.tag
    if tag == "iframe":
        out.construct("web:iframe", el.start, el.end, False)
    elif tag in ("Teleport", "teleport", "Portal"):
        out.construct("web:portal", el.start, el.end, False)
    if el.has("dangerouslySetInnerHTML") or el.has("v-html") or el.has("innerHTML"):
        out.construct("web:raw-html", el.start, el.end, False)
    if el.has("role") or any(k.startswith("aria-") for k in el.attrs):
        out.construct("web:aria", el.start, el.end, False)
    if el.has("i18nKey") or el.has("i18n") or el.has("v-t"):
        out.construct("web:i18n-key", el.start, el.end, False)
    if el.has("sx") or el.has("css"):
        out.construct("web:css-in-js", el.start, el.end, False)


# ------------------------------------------------------------------ style tokens


def emit_tokens(out: Out, system: str, scan: styles.StyleScan) -> set[str]:
    """StyleTokens a stylesheet (or a style block) declares; returns their names."""
    for name, t in scan.tokens.items():
        term = "web:CssVariable" if t.dialect == "css" else "web:ScssVariable"
        value = styles.safe_value(t.value)
        attrs = {"category": styles.category(name, value), "value": value, "dialect": t.dialect,
                 "selectors": t.selectors or None, "declarations": t.declarations if t.declarations > 1 else None}
        out.entity(term, {"system": system, "name": name}, name, t.start, t.end, attrs)
    return set(scan.tokens)


def emit_style_refs(out: Out, owner: Owner | None, refs: list[tuple[str, int, int]], stylesheets: list[str], local_system: str | None, local: set[str]) -> None:
    """STYLED_BY from the file's screen to each token it references: a token this file
    declares, else the same name in each stylesheet the file imports."""
    for name, a, b in refs:
        if owner is None:
            out.construct("web:token-reference", a, b, False)
            continue
        if local_system is not None and name in local:
            out.relate(owner.ref, P.STYLED_BY, token_ref(local_system, name, declare=False), a, b)
            out.construct("web:token-reference", a, b, True)
            continue
        for system in stylesheets:
            out.relate(owner.ref, P.STYLED_BY, token_ref(system, name, declare=False), a, b, cls=EvidenceClass.E3)
        out.construct("web:token-reference", a, b, bool(stylesheets))
