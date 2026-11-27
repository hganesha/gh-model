"""The Talend pack's extractors: job design `.item` files and repository `.properties` files.

`talend.item` reads an `.item` file whose root is a Talend process (`talendfile:ProcessType`:
a job or a joblet) and hands it to `item.ItemReader`; a context group item is counted, and
any other `.item` (the source of a routine, a metadata item) is counted without being read.

`talend.properties` reads the `.properties` file Talend writes next to each item (an XMI
document, `TalendProperties:Property`), only below a Talend project folder (`process/`,
`joblets/`, `context/`, `code/`, `metadata/`, ...) and only when it is one, since Java
`.properties` files share the extension. A job's properties declare its `Job` with its
version, purpose and status; a routine's declare its `Program`. Authors, dates and the
settings of metadata items (database connections) are never read.
"""

from __future__ import annotations

import re

from repo_intel.compile.ontology import EvidenceClass
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.datastage import xmlspan
from repo_intel.platforms.talend.item import FRAMEWORK, ItemReader, folder, job_identity

E1 = EvidenceClass.E1
_FOLDERS = ("process", "process_mr", "process_storm", "joblets", "context", "code", "metadata", "routines", "sqlPatterns", "documentations", "businessProcess")
_METADATA_ITEMS = re.compile(r"(?i)(connection|schema|file\w*|ldap|salesforce|sap|wsdl|xml|edifact|hl7|rules|validation)item$")


def _talend(data: bytes) -> bool:
    return b"talend" in data[:4096].lower()


def _first_line(text: str | None, limit: int = 200) -> str | None:
    if not text or not text.strip():
        return None
    return text.strip().splitlines()[0].strip()[:limit] or None


class TalendItem:
    id = "talend.item"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".item")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base, sql_lineage

        if not _talend(data):
            return base.Extraction(parsed=False)
        try:
            root, exact = xmlspan.parse(data)
        except xmlspan.XmlError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: {exc}")], parsed=False)
        whole = base.whole_file(data)

        def span(node):
            if not exact:
                return whole
            return base.span(data, max(0, min(node.start, len(data))), max(0, min(node.end, len(data))))

        local = root.local
        if local == "ProcessType":
            reader = ItemReader(path, blob, base, span, whole, self.id)
            reader.process(root)
            return base.Extraction(reader.items + sql_lineage.lineage_summary(blob, data, reader.resolved, reader.unresolved), reader.diagnostics)
        if local == "ContextType":
            return base.Extraction([construct_observation(blob, whole, "tal:context_item", False, evidence_class=E1)])
        where = "/" + path.replace("\\", "/")
        cid = "tal:code_item" if "/code/" in where else "tal:metadata_item" if "/metadata/" in where else "tal:item_other"
        return base.Extraction([construct_observation(blob, whole, cid, False, evidence_class=E1)])


class TalendProperties:
    id = "talend.properties"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        if not path.lower().endswith(".properties"):
            return False
        parts = path.replace("\\", "/").split("/")[:-1]
        return any(p in _FOLDERS for p in parts)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if not (_talend(data) and b"Property" in data[:8192]):
            return base.Extraction(parsed=False)
        try:
            root, exact = xmlspan.parse(data)
        except xmlspan.XmlError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: {exc}")], parsed=False)
        whole = base.whole_file(data)
        span = (lambda n: base.span(data, n.start, n.end)) if exact else (lambda n: whole)
        prop = next((c for c in root.iter() if c.local == "Property"), None)
        if prop is None:
            return base.Extraction(parsed=False)
        state = next((c for c in root.iter() if c.local == "ItemState"), None)
        kinds = [c.local for c in root.children if c.local.endswith("Item")]
        kind = kinds[0] if kinds else ""
        label = (prop.get("label") or "").strip() or job_identity(path)[0]
        version = (prop.get("version") or "").strip() or None
        where = (state.get("path") if state is not None else None) or folder(path)
        at = span(prop)
        items = [construct_observation(blob, at, "tal:properties_item", True, evidence_class=E1)]
        parts = path.replace("\\", "/").split("/")
        if kind in ("ProcessItem", "JobletProcessItem") and re.fullmatch(r"[A-Za-z_][\w.$-]{0,254}", label):
            framework = next((a.get("value") for a in prop.find("additionalProperties") if (a.get("key") or "").upper() == "FRAMEWORK"), None)
            term = "tal:Joblet" if kind == "JobletProcessItem" or "joblets" in parts else (
                "tal:BigDataJob" if framework or "process_mr" in parts or "process_storm" in parts else "tal:StandardJob")
            attrs = {"version": version, "purpose": _first_line(prop.get("purpose")), "status": (prop.get("statusCode") or "").strip() or None,
                     "folder": (where or "").strip("/") or None}
            items.append(platform_entity(blob, at, term, {"framework": FRAMEWORK, "name": label}, label, {k: v for k, v in attrs.items() if v}, E1))
        elif kind == "RoutineItem" and re.fullmatch(r"[A-Za-z_]\w{0,254}", label):
            items.append(construct_observation(blob, at, "tal:routine", True, evidence_class=E1))
            attrs = {"version": version, "folder": (where or "").strip("/") or None}
            items.append(platform_entity(blob, at, "tal:Routine", {"platform": "talend", "name": label}, label, {k: v for k, v in attrs.items() if v}, E1))
        elif kind == "ContextItem":
            items.append(construct_observation(blob, at, "tal:context_item", False, evidence_class=E1))
        elif _METADATA_ITEMS.search(kind):
            items.append(construct_observation(blob, at, "tal:metadata_item", False, evidence_class=E1))
        else:
            items.append(construct_observation(blob, at, "tal:item_other", False, evidence_class=E1))
        return base.Extraction(items)
