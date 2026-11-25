"""`cfml.admin`: ColdFusion Administrator exports committed under `.rkp/inputs/cfml/`.

`neo-datasource.xml` lists the server's datasources and `neo-cron.xml` its scheduled
tasks, both as WDDX packets. These files hold connection URLs, user names and
passwords, so only names are read: a datasource's name and driver, a task's name,
group and the path of the URL it requests (no scheme, host, port, query or
credentials). Everything else in the file is ignored.

The XML is parsed with the standard library parser, and a file that declares a
DOCTYPE or an entity is refused rather than parsed.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Any

from repo_intel.platforms.cfml.ir import url_path

INPUT_DIR = ".rkp/inputs/cfml/"
_FILES = ("neo-datasource.xml", "neo-cron.xml")
_DOCTYPE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.I)
_NAME_SAFE = re.compile(r"^[\w .$@-]{1,128}$")


def _lib():
    from repo_intel import platforms
    from repo_intel.compile.ontology import EvidenceClass, Predicate
    from repo_intel.extractors import base, sql_lineage

    return platforms, EvidenceClass, Predicate, base, sql_lineage


def _struct(node: ET.Element | None) -> dict[str, ET.Element]:
    """The members of a WDDX struct, by name, each the member's value element."""
    out: dict[str, ET.Element] = {}
    if node is None or node.tag != "struct":
        return out
    for member in node.findall("var"):
        name = member.get("name")
        children = list(member)
        if name is not None and children:
            out[name] = children[0]
    return out


def _string(members: dict[str, ET.Element], key: str) -> str | None:
    for k, v in members.items():
        if k.lower() == key and v.tag == "string":
            text = "".join(v.itertext()).strip()
            return text or None
    return None


def _entries(root: ET.Element) -> list[tuple[str, dict[str, ET.Element]]]:
    """(name, members) for each struct-valued member of the packet's top-level struct(s).

    Both exports wrap one struct keyed by datasource or task name, sometimes inside a
    one-element array."""
    data = root.find("data")
    if data is None:
        return []
    tops: list[ET.Element] = []
    for child in data:
        if child.tag == "struct":
            tops.append(child)
        elif child.tag == "array":
            tops += [c for c in child if c.tag == "struct"]
    out = []
    for top in tops:
        for name, value in _struct(top).items():
            if value.tag == "struct":
                out.append((name, _struct(value)))
    return out


class CfmlAdmin:
    id = "cfml.admin"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        low = path.lower()
        return low.startswith(INPUT_DIR) and posixpath.basename(low) in _FILES

    def extract(self, path: str, data: bytes, blob: str):
        platforms, EC, P, base, sql = _lib()
        me = self.id

        def fail(message: str):
            return base.Extraction(diagnostics=[base.diagnostic(me, "warning", message)], parsed=False)

        if _DOCTYPE.search(data):
            return fail("refused: the file declares a DOCTYPE or an entity")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            return fail(f"not well-formed XML: {exc}")
        if root.tag != "wddxPacket":
            return fail("not a WDDX packet: nothing read")
        items: list = []
        diagnostics: list = []
        cursor = 0

        def locate(name: str):
            """The span of the member that names `name`, searched forward from the last one."""
            nonlocal cursor
            for quote in ("'", '"'):
                needle = f"name={quote}{name}{quote}".encode()
                at = data.find(needle, cursor)
                if at >= 0:
                    cursor = at + len(needle)
                    return base.span(data, at, at + len(needle))
            return base.whole_file(data)

        def term(at, name: str, key: dict[str, Any], label: str, attrs: dict[str, Any]):
            o = platforms.platform_entity(blob, at, name, key, label, attrs, EC.E1)
            items.append(o)
            return base.ref(o.payload["kind"], key, label)

        def construct(at, cid: str) -> None:
            items.append(platforms.construct(blob, at, cid, True))

        kind = posixpath.basename(path.lower())
        for name, members in _entries(root):
            if not _NAME_SAFE.match(name):
                diagnostics.append(base.diagnostic(me, "info", "an entry with an unusual name was skipped"))
                continue
            at = locate(name)
            if kind == "neo-datasource.xml":
                ds = _string(members, "name") or name
                if not _NAME_SAFE.match(ds):
                    ds = name
                attrs: dict[str, Any] = {"administered": True}
                driver = _string(members, "driver")
                if driver and _NAME_SAFE.match(driver):
                    attrs["driver"] = driver
                key = {"name": f"datasource:{ds}"}
                term(at, "cf:Datasource", key, key["name"], attrs)
                construct(at, "cf:neo-datasource")
            else:
                task = _string(members, "task") or name
                if not _NAME_SAFE.match(task):
                    task = name
                group = _string(members, "group") or "default"
                if not _NAME_SAFE.match(group):
                    group = "default"
                url = _string(members, "url")
                path_only = url_path(url) if url else None
                job = term(at, "cf:ScheduleGroup", {"framework": "cfschedule", "name": group}, group, {"source": "neo-cron"})
                tattrs: dict[str, Any] = {"source": "neo-cron"}
                if path_only:
                    tattrs["url_path"] = path_only
                t = term(at, "cf:ScheduledTask", {"framework": "cfschedule", "job": group, "name": task}, task, tattrs)
                items.append(base.relation(blob, at, job, P.CONTAINS, t, {}, EC.E1))
                target = (path_only or "").lstrip("/")
                if target.lower().endswith((".cfm", ".cfml")):
                    program = sql.program("cfml", target, declare=False)
                    items.append(base.relation(blob, at, t, P.RUNS, program, {}, EC.E1))
                elif target.lower().endswith(".cfc"):
                    program = sql.program("cfml", posixpath.splitext(target)[0].replace("/", ".").lower(), declare=False)
                    items.append(base.relation(blob, at, t, P.RUNS, program, {}, EC.E1))
                elif url and not path_only:
                    diagnostics.append(base.diagnostic(me, "info", f"task {task}: no URL path to resolve"))
                construct(at, "cf:neo-cron-task")
        return base.Extraction(items, diagnostics)
