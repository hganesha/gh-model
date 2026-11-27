"""The three pure steps of the documents compile step (contract 9.1).

- `documents.designations` reads `.rkp/documents.json` against the commit's inventory:
  which files are designated for which features, which designated files cannot be read
  (excluded binaries such as PDF and images, oversize files, formats not read) and the
  export each needs, and a problem for every entry that is ignored.
- `documents.read` reads one designated document (its bytes, secret-masked) into blocks
  and runs the features designated for it. It is cached on the document's bytes and
  features, so an unchanged document is not read again.
- `documents.link` matches what the documents name against the compiled shards: Tables,
  Fields, Screens and Endpoints named exactly, and the Table a DataSchema describes.

Nothing here calls a model; `repo_intel.documents.stage` runs the steps and the rule
decisions, and `compile.compiler` writes the results into the global shard.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import Evidence, Span
from repo_intel.compile.ontology import EntityKind
from repo_intel.documents import features as feats
from repo_intel.documents import format as fmt
from repo_intel.documents import readers

OBSERVATION_KIND = "document_block"


def _span(data: bytes, start: int, end: int) -> Span:
    end = max(start, min(end, len(data)))
    return Span(start_byte=start, end_byte=end, start_line=data.count(b"\n", 0, start) + 1, end_line=data.count(b"\n", 0, max(start, end - 1)) + 1)


# ------------------------------------------------------------------ documents.designations

def _designations(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    from repo_intel.extractors import detect_language
    from repo_intel.ingest.secrets import mask_for_extraction

    data = mask_for_extraction(inputs["designation"], config.get("secret_findings", []))
    entries, problems = fmt.load(data)
    files = {path: parse for path, parse in config["files"] if path != fmt.DESIGNATION_FILE}
    excluded = {path: reason for path, reason in config["excluded"]}
    resolved, unmatched = fmt.resolve(entries, [*files, *excluded])
    documents, unreadable = [], []
    for path, features in resolved.items():
        language = detect_language(path)
        export = readers.export_needed(language)
        if path in excluded:
            unreadable.append({"path": path, "features": features, "reason": "excluded from the snapshot as " + excluded[path].replace("_", " "), "export": readers.EXPORT_NEEDED, "artifact": False})
        elif export is not None:
            unreadable.append({"path": path, "features": features, "reason": f"{language or posixpath.splitext(path)[1] or 'this format'} is not read", "export": export, "artifact": True})
        elif not files[path]:
            unreadable.append({"path": path, "features": features, "reason": "larger than the parse limit", "export": "a smaller export, as " + readers.EXPORT_NEEDED, "artifact": True})
        else:
            documents.append({"path": path, "features": features, "language": language})
    return {"entries": entries, "documents": documents, "unreadable": unreadable, "problems": problems + unmatched}


DESIGNATIONS = Step("documents.designations", "0.1.0", _designations)


# ------------------------------------------------------------------ documents.read

def _read(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    from repo_intel.ingest.secrets import mask_for_extraction

    path, language, wanted, blob = config["path"], config["language"], set(config["features"]), config["blob"]
    data = mask_for_extraction(inputs["data"], config.get("secret_findings", []))
    out: dict[str, Any] = {"path": path, "blocks": 0, "requirements": [], "schemas": [], "code_lists": [], "candidates": [], "omitted_candidates": 0, "used": {}, "evidence": [], "counts": {}, "problem": None}
    try:
        blocks, counts = readers.read(path, language, data)
    except readers.Unreadable as exc:
        out["problem"] = str(exc)
        return out
    out["blocks"], out["counts"] = len(blocks), counts
    evidence: dict[str, dict[str, Any]] = {}

    def cite(*indexes: int) -> list[str]:
        """One evidence row per block, or one spanning a run of blocks (a table)."""
        if not indexes:
            return []
        start, end = min(blocks[i]["at"][0] for i in indexes), max(blocks[i]["at"][1] for i in indexes)
        ev = Evidence.make(blob, _span(data, start, end), OBSERVATION_KIND).row()
        evidence[ev["id"]] = ev
        return [ev["id"]]

    if "requirements" in wanted:
        found, used = feats.requirements(blocks)
        out["used"]["requirements"] = sorted(used)
        for r in found:
            ids = sorted({e for i in r.pop("blocks") for e in cite(i)})
            out["requirements"].append({**r, "evidence_ids": ids})
    if "schema" in wanted:
        found, used = feats.schemas(blocks, path)
        out["used"]["schema"] = sorted(used)
        for s in found:
            fields = [{**{k: v for k, v in f.items() if k != "block"}, "evidence_ids": cite(f["block"])} for f in s["fields"]]
            out["schemas"].append({"name": s["name"], "entity": s["entity"], "fields": fields, "evidence_ids": cite(*s["blocks"])})
    if "code_lists" in wanted:
        found, used = feats.code_lists(blocks, path)
        out["used"]["code_lists"] = sorted(used)
        for c in found:
            out["code_lists"].append({"name": c["name"], "heading": c["heading"], "values": c["values"], "omitted": c["omitted"], "evidence_ids": cite(*c["blocks"])})
    if "rules" in wanted:
        found, omitted = feats.rule_candidates(blocks)
        out["omitted_candidates"] = omitted
        for c in found:
            out["candidates"].append({**c, "at": blocks[c["block"]]["at"], "evidence_id": cite(c["block"])[0]})
    out["evidence"] = [evidence[k] for k in sorted(evidence)]
    return out


READ = Step("documents.read", "0.1.0", _read)


# ------------------------------------------------------------------ documents.link

class _Names:
    """Entities a document may name exactly, from the compiled shards."""

    def __init__(self, shards: Mapping[str, Mapping[str, Any]]):
        self.tables: dict[str, set[str]] = {}
        self.fields: dict[tuple[str, str], set[str]] = {}
        self.screens: dict[str, set[str]] = {}
        self.endpoints: dict[str, set[str]] = {}
        self.unit_tables: dict[str, dict[str, set[str]]] = {}
        for unit in sorted(shards):
            for e in shards[unit]["entities"]:
                key = e["natural_key"]
                if e["kind"] == EntityKind.TABLE and isinstance(key.get("name"), str):
                    name = key["name"].lower()
                    self.tables.setdefault(name, set()).add(e["id"])
                    by_name = self.unit_tables.setdefault(unit, {})
                    by_name.setdefault(name, set()).add(e["id"])
                    if "." in name:
                        by_name.setdefault("@last:" + name.rsplit(".", 1)[1], set()).add(e["id"])
                elif e["kind"] == EntityKind.FIELD and isinstance(key.get("table"), str) and isinstance(key.get("name"), str):
                    self.fields.setdefault((key["table"].lower(), key["name"].lower()), set()).add(e["id"])
                elif e["kind"] == EntityKind.SCREEN and isinstance(key.get("name"), str):
                    self.screens.setdefault(key["name"], set()).add(e["id"])
                elif e["kind"] == EntityKind.ENDPOINT and isinstance(key.get("route"), str):
                    self.endpoints.setdefault(key["route"], set()).add(e["id"])
                    if isinstance(key.get("method"), str):
                        self.endpoints.setdefault(f"{key['method'].upper()} {key['route']}", set()).add(e["id"])

    @staticmethod
    def _one(hits: set[str] | None) -> list[str]:
        return sorted(hits) if hits and len(hits) == 1 else []

    def _endpoint(self, q: str) -> list[str]:
        """An Endpoint by its route, or by `METHOD route`."""
        hit = self._one(self.endpoints.get(q))
        if not hit and " " in q:
            method, route = q.split(None, 1)
            hit = self._one(self.endpoints.get(f"{method.upper()} {route.strip()}"))
        return hit

    def named(self, mentions: Mapping[str, list[str]]) -> list[str]:
        """Entities named exactly once each: a quoted Table, `table.field`, Screen or Endpoint
        route, and an unquoted `schema.table`."""
        found: set[str] = set()
        for q in mentions.get("quoted", []):
            hit = self._one(self.tables.get(q.lower()))
            if not hit and "." in q:
                table, _, column = q.rpartition(".")
                hit = self._one(self.fields.get((table.lower(), column.lower())))
            hit = hit or self._one(self.screens.get(q)) or self._endpoint(q)
            found.update(hit)
        for q in mentions.get("qualified", []):
            found.update(self._one(self.tables.get(q.lower())))
        return sorted(found)

    def table_in_unit(self, unit: str, entity: str) -> str | None:
        """The one Table of the unit with the entity's name (case-insensitive; an unqualified
        entity also matches a schema-qualified table of that name)."""
        by_name = self.unit_tables.get(unit, {})
        name = entity.lower()
        hits = by_name.get(name, set()) | (by_name.get("@last:" + name, set()) if "." not in name else set())
        return next(iter(hits)) if len(hits) == 1 else None


def _link(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    names = _Names(inputs["shards"])
    reads: dict[str, dict[str, Any]] = inputs["reads"]
    units: Mapping[str, str] = config["units"]
    documents = []
    for path in sorted(reads):
        r = dict(reads[path])
        unit = units.get(path, "")
        r["unit"] = unit
        r["requirements"] = [{**{k: v for k, v in q.items() if k != "mentions"}, "applies_to": names.named(q["mentions"])} for q in r["requirements"]]
        r["candidates"] = [{**{k: v for k, v in c.items() if k != "mentions"}, "applies_to": names.named(c["mentions"])} for c in r["candidates"]]
        schemas = []
        for s in r["schemas"]:
            table = names.table_in_unit(unit, s["entity"])
            schemas.append({**s, **({"documents_table": table} if table else {})})
        r["schemas"] = schemas
        documents.append(r)
    return {"documents": documents}


LINK = Step("documents.link", "0.1.0", _link)
