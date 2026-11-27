"""PowerCenter parameter files (`.par`, `.prm`, `.parm`), by name only.

A parameter file is sections of `name=value` lines:

    [Global]
    [SALES.WF:wf_daily_sales]
    $$LOAD_DATE=2026-01-01
    [SALES.WF:wf_daily_sales.ST:s_m_load_orders]
    $DBConnection_SRC=ORA_SRC

Each user parameter (`$$NAME`, or a session parameter `$NAME`) in a section naming a
workflow or worklet becomes `ConfigKey parameter:<folder>/<flow>#<name>`, where `<flow>` is
the innermost workflow or worklet the section header names (`[SALES.WF:wf_daily.WT:wl_q]`
gives `SALES/wl_q`). That is the key the export gives a session parameter of a session in
that flow, and the key a session there references for each parameter its mapping declares,
so the file is linked to what it sets and `$$LOAD_DATE` set for one workflow never merges
with `$$LOAD_DATE` set for another. A section naming a workflow or worklet makes that Job
REFERENCES_CONFIG the parameter; one naming a session makes that session's Task do so;
neither is declared here (they are linked only when an export declares them). Parameters in
`[Global]` or in a section that is not read apply to no workflow this file names: they are
counted, not modelled. Built-in parameters (`$PM...`) are counted, not modelled. Values are
never read, so a password a file sets never leaves it.

A file is read only when it has a section header and a `$` parameter line: other `.par`
files are left alone without a diagnostic.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.informatica.vocabulary import FRAMEWORK
from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.xmlread import Place

_SECTION = re.compile(rb"^[ \t]*\[([^\]\r\n]{1,300})\][ \t]*\r?$", re.M)
_ENTRY = re.compile(rb"^[ \t]*(\$\$?[A-Za-z_][\w]*)[ \t]*=", re.M)
_PART = re.compile(r"^(WF|WT|ST|S):(.+)$", re.I)
_NAME = re.compile(r"[A-Za-z_][\w$#]*")
MAX_BYTES = 2_000_000


def subject_of(section: str) -> tuple[dict[str, Any] | None, str | None]:
    """The Job or Task a section header names, or None and why not."""
    if section.strip().lower() == "global":
        return None, None
    folder, dot, rest = section.partition(".")
    if not dot or not _NAME.fullmatch(folder.strip()):
        return None, f"section [{section}] names no folder"
    folder = folder.strip()
    flow: str | None = None
    session: str | None = None
    for part in rest.split("."):
        m = _PART.match(part.strip())
        if m is None:
            if _NAME.fullmatch(part.strip()) and session is None and flow is None:
                return None, f"section [{section}] names a session without its workflow"
            return None, f"section [{section}] is not read"
        kind, name = m.group(1).upper(), m.group(2).strip()
        if not _NAME.fullmatch(name):
            return None, f"section [{section}] is not read"
        if kind in ("WF", "WT"):
            flow = name
        else:
            session = name
    if flow is None:
        return None, f"section [{section}] names a session without its workflow"
    job = f"{folder}/{flow}"
    if session is None:
        return {"kind": "Job", "natural_key": {"framework": FRAMEWORK, "name": job}, "name": flow, "declare": False}, None
    return {"kind": "Task", "natural_key": {"framework": FRAMEWORK, "job": job, "name": session}, "name": session, "declare": False}, None


def read(out: Out, data: bytes) -> bool:
    if len(data) > MAX_BYTES or b"\x00" in data[:4096]:
        return False
    sections = list(_SECTION.finditer(data))
    if not sections or not _ENTRY.search(data):
        return False
    place = Place(0, len(data), True)
    out.construct(out.whole(), "infa:parameter_file", True)
    bounds = [(m.start(), m.group(1).decode("latin-1")) for m in sections] + [(len(data), "")]
    seen: set[tuple[str, str]] = set()
    for (start, header), (end, _) in zip(bounds, bounds[1:]):
        sat = out.span(place, start, start + len(header) + 2)
        subject, why = subject_of(header)
        if why:
            out.note("info", f"{out.path}: {why}")
        out.construct(sat, "infa:parameter_section", subject is not None or header.strip().lower() == "global")
        scope = (subject["natural_key"].get("job") or subject["natural_key"]["name"]) if subject is not None else None
        unscoped = 0
        for m in _ENTRY.finditer(data, start, end):
            name = m.group(1).decode("ascii")
            at = out.span(place, m.start(1), m.end(1))
            if name.upper().startswith("$PM"):
                out.construct(at, "infa:parameter_builtin", False)
                continue
            out.construct(at, "infa:parameter_entry", scope is not None)
            if scope is None:
                unscoped += 1
                continue
            key = f"parameter:{scope}#{name}"
            ref = out.entity(at, "infa:Parameter", {"name": key}, key, {"kind": "mapping_parameter" if name.startswith("$$") else "session_parameter"})
            if (header, name) not in seen:
                seen.add((header, name))
                out.relation(at, subject, P.REFERENCES_CONFIG, ref, {"via": "parameter_file"})
        if unscoped:
            out.note("info", f"{out.path}: {unscoped} parameter(s) in section [{header}] belong to no workflow or worklet the section names: counted, not linked")
    return True
