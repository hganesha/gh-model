"""Captured screens (contract 8.2): owner-captured DOM attached to the Screen it shows, as E5.

The owner captures screens of the running application outside the engine and commits them
under `.rkp/inputs/screens/`: a `capture.json` listing one entry per capture,

    {"captures": [{"route": "/orders", "image": "orders.png", "dom": "orders.html"},
                  {"template": "orders/list.cfm", "dom": "list.html"}]}

(a bare list is accepted too; `image` and `dom` are relative to `capture.json`), with the
PNG images and the saved DOM. No browser runs inside the engine.

`features.screens`, a pure step, reads `capture.json` and each saved DOM:

- an entry is matched to the one Screen whose name is its `route`, else its `template`
  (Screen names are routes or template paths, section 1). No match, or more than one, is
  a diagnostic, never a guess;
- regions are landmarks (`header`, `nav`, `main`, `aside`, `footer`, `form`, `search`, a
  labelled or headed `section`/`article`, or an explicit landmark `role`), each with its
  first heading, its `aria-label` and the controls inside it, named by the section 1 rule.
  Input values are never read; only an input button's value, as the naming rule says.

Spec `dm.screen-regions` labels each region from a closed set: the landmark settles
`banner`, `navigation`, `contentinfo` and `search`; a model labels the rest, or abstains.
The compiler sets the result on the Screen as `attributes.captured` with the capture's
evidence, class E5.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Evidence, Span, decision_id
from repo_intel.storage.hashing import digest
from repo_intel.compile.ontology import EntityKind
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.runner import Budget, Ledger, RuleResult, evaluate

INPUT_DIR = ".rkp/inputs/screens/"
CAPTURE_FILE = INPUT_DIR + "capture.json"

LABELS = ["header", "navigation", "search", "filters", "form", "results", "detail", "summary", "actions", "messages", "sidebar", "footer", "content"]
SPEC = DecisionSpec.model_validate({
    "id": "dm.screen-regions", "version": "0.1.0", "scope": "screen_region",
    "input_schema": "ScreenRegion.v1", "output_schema": "RegionLabel.v1",
    "labels": LABELS,
    "feature_sources": [CAPTURE_FILE],
    "strategy": [{"kind": "rules"}, {"kind": "llm", "when": "ambiguous", "model_ref": "llm-default", "prompt_ref": "prompts/screen-regions-v1"}],
    "abstain_required_if_missing": ["region.landmark"],
    "verification": "vfy.none",
    "max_input_bytes": 8000,
})
CRITERIA = {
    "@instructions": "You label one region of a captured application screen from its landmark, heading and the controls in it. Choose the option that best describes what the region is for.",
    "header": "The page header: title, logo, account links.",
    "navigation": "Links that move between screens.",
    "search": "A search box and its button.",
    "filters": "Controls that narrow a list or report.",
    "form": "Inputs a user fills in and submits.",
    "results": "A list or table of records.",
    "detail": "One record shown in full.",
    "summary": "Totals, counts or key figures.",
    "actions": "Buttons that act on the screen's data.",
    "messages": "Alerts, errors or notices.",
    "sidebar": "Secondary content beside the main content.",
    "footer": "The page footer.",
    "content": "The main content when nothing more specific fits.",
}
_SETTLED = {"banner": "header", "navigation": "navigation", "contentinfo": "footer", "search": "search"}
_AMBIGUOUS = {
    "form": ["form", "filters", "search"],
    "complementary": ["sidebar", "filters", "summary", "actions"],
}

LANDMARK_TAGS = {"header": "banner", "nav": "navigation", "main": "main", "aside": "complementary", "footer": "contentinfo", "form": "form", "search": "search"}
LANDMARK_ROLES = frozenset({"banner", "navigation", "main", "complementary", "contentinfo", "search", "form", "region"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
MAX_REGIONS = 40
MAX_CONTROLS = 50


def region_rules(features: Mapping[str, Any]) -> RuleResult:
    landmark = features["region"]["landmark"]
    if landmark in _SETTLED:
        return RuleResult("resolved", label=_SETTLED[landmark], reason=f"landmark:{landmark}")
    return RuleResult("ambiguous", candidates=tuple(_AMBIGUOUS.get(landmark, [x for x in LABELS if x not in ("header", "navigation", "footer")])))


# ------------------------------------------------------------------ the saved DOM


class _Node:
    __slots__ = ("tag", "attrs", "start", "end", "parent", "children", "text")

    def __init__(self, tag: str, attrs: dict[str, str], start: int, parent: "_Node | None"):
        self.tag, self.attrs, self.start, self.end, self.parent = tag, attrs, start, start, parent
        self.children: list[_Node] = []
        self.text: list[str] = []

    def ancestors(self) -> list["_Node"]:
        out, p = [], self.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out

    def deep_text(self) -> str:
        parts = list(self.text)
        for c in self.children:
            parts.append(c.deep_text())
        return " ".join(" ".join(parts).split())

    def walk(self) -> list["_Node"]:
        out = [self]
        for c in self.children:
            out += c.walk()
        return out


class _Parser(HTMLParser):
    """Elements with character offsets; unclosed elements end where their parent does."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.lines = [0]
        for m in re.finditer("\n", text):
            self.lines.append(m.end())
        self.root = _Node("#root", {}, 0, None)
        self.open: list[_Node] = [self.root]
        self.size = len(text)

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self._offset(), self.open[-1])
        node.end = node.start + len(self.get_starttag_text() or "")
        self.open[-1].children.append(node)
        if tag not in _VOID:
            self.open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self._offset(), self.open[-1])
        node.end = node.start + len(self.get_starttag_text() or "")
        self.open[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.open) - 1, 0, -1):
            if self.open[i].tag == tag:
                end = self._offset() + len(tag) + 3
                for node in self.open[i:]:
                    node.end = max(node.end, end)
                del self.open[i:]
                return

    def handle_data(self, data: str) -> None:
        self.open[-1].text.append(data)

    def finish(self) -> _Node:
        self.close()
        for node in self.open[1:]:
            node.end = self.size
        self.root.end = self.size
        return self.root


def _landmark(node: _Node) -> str | None:
    role = node.attrs.get("role", "").strip().lower()
    if role in LANDMARK_ROLES:
        return role
    if node.tag in ("header", "footer") and any(a.tag in ("main", "article", "section", "aside", "nav") for a in node.ancestors()):
        return None  # a section's own header or footer is not the page's
    if node.tag in LANDMARK_TAGS:
        return LANDMARK_TAGS[node.tag]
    if node.tag in ("section", "article") and (node.attrs.get("aria-label", "").strip() or any(c.tag in _HEADINGS for c in node.walk()[1:])):
        return "region"
    return None


def _control_kind(node: _Node) -> str | None:
    if node.tag == "input":
        t = node.attrs.get("type", "").strip().lower()
        if t == "hidden":
            return None
        return "button" if t in ("submit", "button", "reset", "image") else "input"
    if node.tag in ("select", "textarea", "button"):
        return node.tag
    if node.tag == "a" and "href" in node.attrs:
        return "link"
    return None


def _trimmed(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _link_name(href: str) -> str | None:
    href = href.strip()
    if _EXTERNAL.match(href):  # another host: its origin only, never its path, query or user information
        m = re.match(r"^([a-z][a-z0-9+.-]*://)(?:[^@/?#]*@)?([^/?#]*)", href, re.I)
        return "link:" + m.group(1) + m.group(2) if m else None
    if _SCHEME.match(href):  # mailto:, javascript:, tel: carry no screen and may carry an address
        return None
    path = href.split("#")[0].split("?")[0]
    return "link:" + path if path else None


def _named(node: _Node, kind: str) -> str | None:
    """Rules 1 to 3 of the section 1 naming rule; None leaves rule 4, `<type><n>`."""
    name = _trimmed(node.attrs.get("name")) or _trimmed(node.attrs.get("id")) or _trimmed(node.attrs.get("v-model")) or _trimmed(node.attrs.get("formcontrolname"))
    if name:
        return name
    if kind == "link":
        return _link_name(node.attrs.get("href", ""))
    if kind == "button":
        if node.attrs.get("type", "").strip().lower() == "submit":
            return "submit"
        text = " ".join((node.attrs.get("value", "") if node.tag == "input" else node.deep_text()).split())
        if text and len(text) <= 40:
            return "button:" + text
    return None


def _fallback_type(node: _Node, kind: str) -> str:
    if node.tag == "input":
        t = node.attrs.get("type", "").strip().lower()
        return t if re.fullmatch(r"[a-z][a-z-]*", t) else "text"
    return kind


def dom_regions(data: bytes, screen: str) -> list[dict[str, Any]]:
    """Regions of a saved DOM in document order: landmark, tag, heading, aria-label, byte span,
    and the controls inside (innermost region only), named by the section 1 rule with the
    screen, or `<screen>#<form>`, as container."""
    text = data.decode("utf-8", "replace")
    parser = _Parser(text)
    parser.feed(text)
    root = parser.finish()
    nodes = root.walk()[1:]
    byte_at = _byte_offsets(text)
    forms: dict[int, str] = {}
    order = 0
    for node in nodes:
        if node.tag == "form":
            order += 1
            forms[id(node)] = _trimmed(node.attrs.get("name")) or _trimmed(node.attrs.get("id")) or f"form{order}"
    regions: list[tuple[_Node, dict[str, Any]]] = []
    for node in nodes:
        landmark = _landmark(node)
        if landmark is None or len(regions) >= MAX_REGIONS:
            continue
        heading = next((h.deep_text() for h in node.walk()[1:] if h.tag in _HEADINGS and h.deep_text()), None)
        regions.append((node, {
            "id": f"r{len(regions) + 1}", "landmark": landmark, "tag": node.tag,
            "heading": heading[:80] if heading else None, "aria_label": (_trimmed(node.attrs.get("aria-label")) or "")[:80] or None,
            "at": [byte_at(node.start), byte_at(node.end)], "controls": [],
            **({"form": forms[id(node)]} if node.tag == "form" else {}),
        }))
    region_of = {id(n): r for n, r in regions}
    fallbacks: dict[tuple[str, str], int] = {}
    taken: dict[tuple[str, str], int] = {}
    for node in nodes:
        kind = _control_kind(node)
        if kind is None:
            continue
        form = next((forms[id(a)] for a in node.ancestors() if id(a) in forms), None)
        container = f"{screen}#{form}" if form else screen
        name = _named(node, kind)
        if name is None:
            ctype = _fallback_type(node, kind)
            n = fallbacks[(container, ctype)] = fallbacks.get((container, ctype), 0) + 1
            name = f"{ctype}{n}"
        seen = taken[(container, name)] = taken.get((container, name), 0) + 1
        if seen > 1:
            name = f"{name}~{seen}"
        region = next((region_of[id(a)] for a in node.ancestors() if id(a) in region_of), None)
        if region is not None and len(region["controls"]) < MAX_CONTROLS:
            region["controls"].append({"container": container, "name": name, "kind": kind})
    return [r for _, r in regions]


def _byte_offsets(text: str) -> Any:
    """Character offset -> byte offset in the UTF-8 encoding of `text`."""
    if text.isascii():
        return lambda i: i
    cumulative = [0]
    for ch in text:
        cumulative.append(cumulative[-1] + len(ch.encode("utf-8")))
    return lambda i: cumulative[min(i, len(text))]


def _span(data: bytes, start: int, end: int) -> Span:
    end = max(start, min(end, len(data)))
    return Span(start_byte=start, end_byte=end, start_line=data.count(b"\n", 0, start) + 1, end_line=data.count(b"\n", 0, max(start, end - 1)) + 1)


# ------------------------------------------------------------------ the step


def _entries(data: bytes) -> tuple[list[Any], str | None]:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], f"{CAPTURE_FILE} is not JSON: {exc.__class__.__name__}"
    items = doc.get("captures") if isinstance(doc, Mapping) else doc
    if not isinstance(items, list):
        return [], f"{CAPTURE_FILE} lists no captures"
    return items, None


def _inside(path: Any) -> str | None:
    """A path relative to the capture directory, as a repository path, if it stays inside it."""
    if not isinstance(path, str) or not path.strip() or path.startswith("/"):
        return None
    joined = posixpath.normpath(INPUT_DIR + path.strip())
    return joined if joined.startswith(INPUT_DIR) else None


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """Each capture matched to its Screen, with the regions of its DOM and evidence rows."""
    from repo_intel.ingest.secrets import mask_for_extraction
    blobs: dict[str, str] = config["blobs"]  # path -> blob sha256, for the files under INPUT_DIR
    findings: dict[str, list[dict[str, Any]]] = config.get("secret_findings", {})
    # Detected secret values are masked before reading, offsets kept, as extractors do.
    capture: bytes = mask_for_extraction(inputs["capture"], findings.get(CAPTURE_FILE, []))
    doms: dict[str, bytes] = {p: mask_for_extraction(b, findings.get(p, [])) for p, b in inputs["doms"].items()}
    screens: dict[str, list[dict[str, Any]]] = {}
    for s in inputs["summaries"]:
        if s["path"].startswith(INPUT_DIR):
            continue  # a saved DOM read as markup by a web extractor is not the screen it shows
        for o in s["observations"]:
            p = o["payload"]
            if o["kind"] == "entity" and p.get("kind") == EntityKind.SCREEN and isinstance(p["natural_key"].get("name"), str):
                screens.setdefault(p["natural_key"]["name"], []).append({"natural_key": p["natural_key"], "name": p["name"], "path": s["path"]})
    items, problem = _entries(capture)
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        record: dict[str, Any] = {"entry": i, "screen": None, "problem": None, "reason": None, "regions": [], "evidence": []}
        item = item if isinstance(item, Mapping) else {}
        wanted = [(k, _no_values(item[k].strip())) for k in ("route", "template") if isinstance(item.get(k), str) and item[k].strip()]
        record["wanted"] = {k: v for k, v in wanted}
        dom = _inside(item.get("dom"))
        record["dom"], record["image"] = dom, _inside(item.get("image"))
        anchor = json.dumps(item.get("dom")).encode() if item.get("dom") else b""
        at = capture.find(anchor) if anchor else -1
        span = _span(capture, at, at + len(anchor)) if at >= 0 else _span(capture, 0, len(capture))
        record["evidence"].append(Evidence.make(blobs[CAPTURE_FILE], span, "screen_capture").row())
        hits: dict[str, dict[str, Any]] = {}
        for _, value in wanted:
            for s in screens.get(value, []):
                hits[json.dumps(s["natural_key"], sort_keys=True)] = s
            if hits:
                break  # the route first, then the template
        named = ", ".join(f"{k} {v}" for k, v in wanted)
        if not wanted:
            record["reason"], record["problem"] = "capture_names_no_screen", f"capture {i} names no route or template"
        elif not hits:
            record["reason"], record["problem"] = "capture_matches_no_screen", f"capture {i} ({named}) matches no Screen"
        elif len(hits) > 1:
            record["reason"], record["problem"] = "capture_matches_several_screens", f"capture {i} ({named}) matches {len(hits)} Screens"
        elif dom is None or dom not in doms:
            record["reason"], record["problem"] = "capture_dom_missing", f"capture {i} ({named}): its DOM file is not a readable file under {INPUT_DIR} in this commit"
        else:
            screen = next(iter(hits.values()))
            record["screen"] = screen
            data = doms[dom]
            for region in dom_regions(data, screen["natural_key"]["name"]):
                ev = Evidence.make(blobs[dom], _span(data, region["at"][0], region["at"][1]), "screen_region").row()
                record["evidence"].append(ev)
                region["evidence_id"] = ev["id"]
                record["regions"].append(region)
        out.append(record)
    return {"captures": out, "problem": problem}


FEATURES_BUILD = Step("features.screens", "0.1.0", _features)


def label_regions(record: Mapping[str, Any], screen_entity: str, *, ledger: Ledger, gateway: Gateway | None, budget: Budget | None) -> list[dict[str, Any]]:
    """One `dm.screen-regions` decision per region. Overrides do not apply: a region label is
    not something an owner confirms."""
    out = []
    for region in record["regions"]:
        features = {"screen": record["screen"]["natural_key"]["name"], "region": {k: region[k] for k in ("landmark", "tag", "heading", "aria_label") if region.get(k)} | {"controls": [c["name"] for c in region["controls"][:20]]}}
        out.append(evaluate(SPEC, screen_entity, features, region_rules, ledger=ledger, evidence_ids=(region["evidence_id"],), gateway=gateway, budget=budget, criteria=CRITERIA).row())
    return out


def _no_values(target: str) -> str:
    """A route as the contract keeps URLs: query parameter names without values, no fragment."""
    path, _, query = target.split("#")[0].partition("?")
    if not query:
        return path
    return path + "?" + "&".join(p.split("=")[0] + "=" for p in query.split("&") if p)


def unmatched(record: Mapping[str, Any] | None, scope_entity: str, problem: str, *, ledger: Ledger) -> DecisionResult:
    """A capture whose regions cannot be labelled, because it shows no single Screen or its DOM
    is missing (or `capture.json` cannot be read: `record` is None), recorded as an abstained
    decision so `decisions` coverage counts it."""
    reason = (record or {}).get("reason") or "capture_unreadable"
    evidence = sorted(ev["id"] for ev in (record or {}).get("evidence", []))
    name = f"{SPEC.id}@{SPEC.version}"
    fsh = digest({"scope": scope_entity, "entry": (record or {}).get("entry"), "wanted": (record or {}).get("wanted"), "problem": problem, "evidence_ids": evidence})
    return ledger.put(DecisionResult(
        decision_id=decision_id(name, fsh, ""), spec=name, scope_entity=scope_entity, feature_set_hash=fsh,
        rule_outcome="insufficient_evidence", selected_label=None, evidence_ids=evidence, abstained=True,
        abstain_reason=reason, decision_method="rules",
    ))


def label_status(decision: Mapping[str, Any]) -> str:
    if decision["abstained"]:
        return "unknown"
    return "supported" if decision["decision_method"] == "rules" else "inferred"


def captured_attribute(record: Mapping[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """`attributes.captured` entry for one capture of a Screen."""
    regions = []
    for region, d in zip(record["regions"], decisions):
        regions.append({**{k: v for k, v in region.items() if v is not None}, "label": d["selected_label"], "label_status": label_status(d), "decision_ref": d["decision_id"]})
    return {
        "capture": CAPTURE_FILE, "entry": record["entry"], "dom": record["dom"], **({"image": record["image"]} if record["image"] else {}),
        "evidence_class": "E5", "evidence_ids": [ev["id"] for ev in record["evidence"]], "regions": regions,
    }
