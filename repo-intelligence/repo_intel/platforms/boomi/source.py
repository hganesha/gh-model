"""Extract declarations and native IDs from one Boomi component export."""

from __future__ import annotations

import re
from xml.etree.ElementTree import Element

from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.xmlread import Doc, Place, child, children, head_text, local, parse

_NAMESPACES = ("http://api.platform.boomi.com/", "http://api.boomi.com/")
_COMPONENT_TAGS = {f"{{{uri}}}Component" for uri in _NAMESPACES}
_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_TOKEN = re.compile(r"[\w.-]{1,128}", re.ASCII)
_PROFILES = {"profile.json", "profile.xml", "profile.flatfile", "profile.db", "profile.edi"}
_COMPONENTS = {
    "process": ("Process", "process"),
    "connector-settings": ("Connection", "connection"),
    "connector-action": ("Operation", "operation"),
    "transform.map": ("Map", "map"),
    **{typ: ("Profile", "profile") for typ in _PROFILES},
}


def _component_id(value: str | None) -> str | None:
    return value.lower() if value and _ID.fullmatch(value) else None


def _token(value: str | None) -> str | None:
    return value if value and _TOKEN.fullmatch(value) else None


def _references(node: Element | None, *attrs: str) -> dict[str, str]:
    if node is None:
        return {}
    return {key: value for key in attrs if (value := _component_id(node.get(key)))}


class BoomiComponent:
    id = "boomi.component"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".xml")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        probe = head_text(data, len(data))
        if not any(uri in probe for uri in _NAMESPACES) or "Component" not in probe:
            return base.Extraction(parsed=False)
        doc, problem = parse(data)
        if doc is None:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"Boomi XML: {problem}")], parsed=False)
        if doc.root.tag not in _COMPONENT_TAGS:
            return base.Extraction(parsed=False)
        out = Out(self.id, path, data, blob, base)
        ComponentExport(doc, out, len(data)).run()
        result = out.extraction()
        result.language = "boomi"
        return result


class ComponentExport:
    def __init__(self, doc: Doc, out: Out, size: int):
        self.doc, self.out = doc, out
        self.place = Place(0, size, True)

    def span(self, node: Element):
        bounds = self.doc.span(node)
        return self.out.span(self.place, *bounds) if bounds else self.out.whole()

    def run(self) -> None:
        root = self.doc.root
        component_type = root.get("type", "")
        if component_type not in _COMPONENTS:
            self.out.construct(self.span(root), "boomi:other_component", False)
            self.out.note("info", "Boomi component type has no structural extractor")
            return
        term, construct = _COMPONENTS[component_type]
        component_id = _component_id(root.get("componentId"))
        wrapper = child(root, "object")
        contents = next(iter(wrapper), None) if wrapper is not None else None
        if not component_id or contents is None:
            self.out.construct(self.span(root), f"boomi:{construct}", False)
            self.out.note("warning", "Boomi component is missing its component ID or definition")
            return

        version = _token(root.get("version"))
        branch = _token(root.get("branchId"))
        identity = "~".join((component_id, branch or "", version or ""))
        name = root.get("name") or component_id
        attributes = {
            "component_id": component_id,
            "component_type": component_type,
            "version": version,
            "branch_id": branch,
            "current_version": {"true": True, "false": False}.get(root.get("currentVersion", "").lower()),
            "subtype": _token(root.get("subType")),
        }
        at = self.span(root)
        self.out.construct(at, f"boomi:{construct}", True)
        if component_type == "process":
            job = self.out.entity(at, "boomi:Process", {"framework": "boomi", "name": identity}, name, attributes)
            self.process(contents, job, identity, branch)
        elif component_type == "connector-settings":
            fields = {n for el in children(contents, "field") if (n := _token(el.get("id")))}
            fields.update(local(key) for key in contents.attrib)
            self.out.entity(at, "boomi:Connection", {"name": f"boomi:connection:{identity}"}, name, {**attributes, "setting_names": sorted(fields)})
        elif component_type == "connector-action":
            config = contents
            wrapper = child(contents, "Configuration")
            generic = child(wrapper, "GenericOperationConfig")
            if generic is None:
                generic = child(contents, "GenericOperationConfig")
            if generic is not None:
                config = generic
            attributes.update(
                operation_type=_token(config.get("operationType")),
                action_type=_token(config.get("customOperationType")),
                component_references=_references(config, "requestProfile", "responseProfile"),
            )
            self.out.entity(at, "boomi:Operation", {"platform": "boomi", "name": f"operation:{identity}"}, name, attributes)
        elif component_type == "transform.map":
            self.mapping(contents, identity, name, attributes)
        else:
            self.profile(contents, identity, name, attributes)

    def process(self, contents: Element, job: dict, identity: str, branch: str | None) -> None:
        shapes = children(child(contents, "shapes"), "shape")
        by_name = {}
        for shape in shapes:
            shape_id = _token(shape.get("name"))
            at = self.span(shape)
            modelled = bool(shape_id and shape_id not in by_name)
            self.out.construct(at, "boomi:shape", modelled)
            if not modelled:
                self.out.note("warning", "Boomi process contains a step with a missing or repeated name")
                continue
            config = child(shape, "configuration")
            connector = child(config, "connectoraction")
            refs = {
                **_references(connector, "connectionId", "operationId"),
                **_references(child(config, "map"), "mapId"),
                **_references(child(config, "processcall"), "processId"),
            }
            shape_type = _token(shape.get("shapetype"))
            task = self.out.entity(
                at, "boomi:Shape", {"framework": "boomi", "job": identity, "name": shape_id},
                shape.get("userlabel") or shape_id,
                {
                    "type": shape_type, "shape_id": shape_id, "entry": True if shape_type == "start" else None,
                    "branch_id": branch, "component_references": refs,
                    "connector_type": _token(connector.get("connectorType")) if connector is not None else None,
                    "action_type": _token(connector.get("actionType")) if connector is not None else None,
                },
            )
            self.out.relation(at, job, P.CONTAINS, task)
            by_name[shape_id] = (shape, task)

        for shape, source in by_name.values():
            for point in children(child(shape, "dragpoints"), "dragpoint"):
                target = by_name.get(point.get("toShape"))
                at = self.span(point)
                self.out.construct(at, "boomi:dragpoint", target is not None)
                if target is None:
                    self.out.note("warning", "Boomi dragpoint has an unresolved target")
                    continue
                self.out.relation(at, target[1], P.DEPENDS_ON, source, {
                    "via": "boomi_dragpoint", "identifier": _token(point.get("identifier")), "dragpoint": _token(point.get("name")),
                })

    def mapping(self, contents: Element, identity: str, name: str, attributes: dict) -> None:
        direct = []
        for mapping in children(child(contents, "Mappings"), "Mapping"):
            source, target = _token(mapping.get("fromKey")), _token(mapping.get("toKey"))
            modelled = bool(source and target and mapping.get("fromType") == mapping.get("toType") == "profile"
                            and mapping.get("fromCacheJoinKey") in (None, "", "0"))
            self.out.construct(self.span(mapping), "boomi:mapping", modelled)
            if modelled:
                direct.append({"from_key": source, "to_key": target})
        self.out.entity(self.span(contents), "boomi:Map", {"platform": "boomi", "name": f"map:{identity}"}, name, {
            **attributes, "component_references": _references(contents, "fromProfile", "toProfile"), "field_mappings": direct,
        })

    def profile(self, contents: Element, identity: str, name: str, attributes: dict) -> None:
        key = f"boomi:profile:{identity}"
        profile = self.out.entity(self.span(contents), "boomi:Profile", {"kind": "boomi_profile", "name": key}, name, attributes)
        elements = child(contents, "DataElements")
        if elements is None:
            return
        seen = set()
        for element in elements.iter():
            element_key = _token(element.get("key"))
            field_name = element.get("name")
            if not element_key or not field_name:
                continue
            field_at = self.span(element)
            modelled = element_key not in seen and element.get("isMappable") != "false"
            self.out.construct(field_at, "boomi:profile_element", modelled)
            if modelled:
                field = self.out.entity(field_at, "boomi:Field", {"table": key, "name": element_key}, field_name, {
                    "element_type": local(element.tag), "data_type": _token(element.get("dataType")),
                })
                self.out.relation(field_at, profile, P.CONTAINS, field)
                seen.add(element_key)
