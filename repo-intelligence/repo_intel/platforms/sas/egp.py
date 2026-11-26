"""`sas.egp`: SAS Enterprise Guide projects (`.egp`), read from the file's bytes.

An `.egp` file is a zip archive. It is opened in memory (`zipfile` over `BytesIO`),
never extracted to disk, and read within bounds: at most `MAX_ENTRIES` members,
`MAX_MEMBER` bytes from any one member and `MAX_TOTAL` bytes in all, so a crafted
archive cannot exhaust memory. Each member read is scanned for secrets and masked
before it is parsed, as the snapshot does for ordinary files (it cannot see inside a
compressed archive).

`project.xml` (UTF-16 or UTF-8) lists the project's elements. The shape read (element
types are matched by the last part of their `Type`, case-insensitively; anything else
is counted as `sas:eg_unknown_element`):

    <Element Type="SAS.EG.ProjectElements.PFD">              a process flow  -> Job
      <Element><Label>Nightly</Label><ID>PFD-1</ID></Element>
    <Element Type="SAS.EG.ProjectElements.CodeTask">         a program node  -> Task, RUNS Program
      <Element><Label>Load</Label><ID>CodeTask-1</ID><Container>PFD-1</Container></Element>
      <CodeTask><Embedded>True</Embedded><DNA>...</DNA></CodeTask>
    <Element Type="SAS.EG.ProjectElements.ShortCutToData">   a data node     -> Dataset
      <Element>...</Element><DNA>... <Library>RPT</Library><Member>SALES</Member> ...</DNA>
    <Element Type="SAS.EG.ProjectElements.EGTask">           another task (Query Builder, a task wizard) -> Task
    <Element Type="SAS.EG.ProjectElements.Link">             a flow link     -> DEPENDS_ON
      <Element>...</Element><LinkFrom>CodeTask-1</LinkFrom><LinkTo>CodeTask-2</LinkTo>

What it declares (E1, a declarative file):

- the project as a Program (`sas:EgProject`, named by its path), which CONTAINS each
  process flow, a Job (`sas:EgProcessFlow`, framework `sas_eg`, named
  `<project path>#<flow label>`), which CONTAINS its tasks (`sas:EgTask`);
- links give the flow order: a task DEPENDS_ON the task a link comes from, and also
  through a data node between them (`via: data`); a data node linked into a task is a
  Dataset the task READS_FROM, one a task links to a Dataset it WRITES_TO (WORK,
  SASHELP and SASUSER datasets are left out, as elsewhere in the SAS pack);
- an embedded code task's program (the member `<task id>/code.sas`) is lowered by the
  SAS pack's own analysis as a Program `sas:EgCode` named
  `<project path>#<flow label>/<task label>`; the task RUNS it, and the datasets the
  program reads and writes are also the task's. An external code task names a file by
  an absolute path on the author's machine: only its base name is kept, and nothing is
  linked to it.

Evidence spans point into the `.egp` file: exact offsets for a member stored without
compression, the member's whole byte range for a compressed one. Server names, paths,
credentials and option values in the project are never read.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import Construct, Term
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity

P = Predicate
E1 = EvidenceClass.E1
EXTRACTOR = "sas.egp"
FRAMEWORK = "sas_eg"
MAX_ENTRIES = 5000
MAX_MEMBER = 1_000_000
MAX_TOTAL = 8_000_000
MAX_ELEMENTS = 50000
_RESERVED_LIBRARIES = {"work", "sashelp", "sasuser"}
_SAS_NAME = re.compile(r"[A-Za-z_]\w{0,31}")
_LIBREF = re.compile(r"[A-Za-z_]\w{0,7}")


def _t(name: str, core: K, fidelity: str, description: str) -> Term:
    return Term(f"sas:{name}", core, fidelity, description)  # type: ignore[arg-type]


def _c(name: str, modelled_as: str, description: str) -> Construct:
    return Construct(f"sas:{name}", description, modelled_as)


TERMS = (
    _t("EgProject", K.PROGRAM, "lossy", "a SAS Enterprise Guide project (.egp); named by its path"),
    _t("EgProcessFlow", K.JOB, "lossy", "a process flow of an Enterprise Guide project; named <project path>#<flow label>"),
    _t("EgTask", K.TASK, "lossy", "a node of a process flow: a code task, a query or another task"),
    _t("EgCode", K.PROGRAM, "lossy", "the program of an embedded code task, lowered like a .sas file; named <project path>#<flow>/<task>"),
)

CONSTRUCTS = (
    _c("eg_project", "entity:Program", "an Enterprise Guide project file"),
    _c("eg_process_flow", "entity:Job", "a process flow"),
    _c("eg_code_task", "entity:Task", "a code task (program node)"),
    _c("eg_embedded_code", "entity:Program", "a program embedded in the project, lowered by the SAS analysis"),
    _c("eg_external_code", "unmapped", "a code task whose program is a file outside the project, named by an absolute path"),
    _c("eg_query_task", "entity:Task", "a Query Builder task; its SQL is generated when it runs and is not stored as code"),
    _c("eg_task", "entity:Task", "another task (a wizard such as Summary Statistics or a chart)"),
    _c("eg_data", "entity:Dataset", "a data node (a shortcut to a dataset)"),
    _c("eg_link", "relation:DEPENDS_ON", "a link between two nodes of a flow"),
    _c("eg_note", "unmapped", "a note"),
    _c("eg_ordered_list", "unmapped", "an ordered list of tasks to run"),
    _c("eg_condition", "unmapped", "a condition on a branch of a flow"),
    _c("eg_prompt", "unmapped", "a prompt the project defines"),
    _c("eg_file", "unmapped", "a shortcut to a file other than data (Excel, text, HTML)"),
    _c("eg_report", "unmapped", "a report or result"),
    _c("eg_unknown_element", "unmapped", "an element type this reader does not know"),
)

_TASK_TYPES = {"codetask": "code", "egtask": "task", "query": "query", "task": "task"}
_OTHER_TYPES = {
    "note": "sas:eg_note", "orderedlist": "sas:eg_ordered_list", "condition": "sas:eg_condition", "conditionset": "sas:eg_condition",
    "prompt": "sas:eg_prompt", "promptdefinition": "sas:eg_prompt", "shortcuttofile": "sas:eg_file", "report": "sas:eg_report",
    "reportsection": "sas:eg_report", "result": "sas:eg_report", "egtreenode": None, "projectcollection": None, "container": None,
    "processflowcontainer": None, "taskgraph": None,
}


def _short(type_name: str | None) -> str:
    return (type_name or "").rsplit(".", 1)[-1].lower()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if isinstance(c.tag, str) and c.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return c
    return None


def _text(el: ET.Element | None, *path: str) -> str | None:
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = _child(cur, name)
    if cur is None or cur.text is None:
        return None
    return cur.text.strip() or None


def _find_text(el: ET.Element, name: str) -> str | None:
    for c in el.iter():
        if isinstance(c.tag, str) and c.tag.rsplit("}", 1)[-1].lower() == name.lower() and c.text and c.text.strip():
            return c.text.strip()
    return None


def _decode_xml(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if len(raw) > 1 and raw[1:2] == b"\x00":  # UTF-16 LE without a byte-order mark
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


class _Member:
    """One archive member read within bounds, with where its bytes are in the `.egp` file."""

    def __init__(self, info: zipfile.ZipInfo, data: bytes, start: int, end: int, stored: bool):
        self.info, self.data, self.start, self.end, self.stored = info, data, start, end, stored


class EgpProject:
    id = EXTRACTOR
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".egp")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        return _Reader(path, data, blob, base).run()


class _Reader:
    def __init__(self, path: str, data: bytes, blob: str, base):
        self.path, self.data, self.blob, self.base = path, data, blob, base
        self.items: list = []
        self.diagnostics: list = []
        self.total = 0
        self.resolved = self.unresolved = 0
        self.project = {"kind": "Program", "natural_key": {"platform": "sas", "name": path}, "name": posixpath.basename(path)}

    # ------------------------------------------------------------------ archive

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(EXTRACTOR, severity, message))

    def member_range(self, info: zipfile.ZipInfo) -> tuple[int, int]:
        """The bytes of a member's (possibly compressed) data in the archive, from its local header."""
        h = info.header_offset
        header = self.data[h:h + 30]
        if len(header) < 30 or header[:4] != b"PK\x03\x04":
            return 0, len(self.data)
        name_len = int.from_bytes(header[26:28], "little")
        extra_len = int.from_bytes(header[28:30], "little")
        start = h + 30 + name_len + extra_len
        return start, min(len(self.data), start + info.compress_size)

    def read(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> _Member | None:
        from repo_intel.ingest import secrets

        if info.is_dir():
            return None
        if info.flag_bits & 0x1:
            self.note("info", f"member {info.filename} is encrypted: not read")
            return None
        start, end = self.member_range(info)
        stored = info.compress_type == zipfile.ZIP_STORED
        if stored:
            # Read the stored bytes directly: secret masking at admission may have changed
            # them (keeping their length), which a CRC check would reject.
            raw = self.data[start:min(end, start + MAX_MEMBER + 1)]
        else:
            try:
                with zf.open(info) as f:
                    raw = f.read(MAX_MEMBER + 1)
            except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError) as exc:
                self.note("warning", f"member {info.filename} cannot be read: {type(exc).__name__}")
                return None
        if len(raw) > MAX_MEMBER:
            self.note("info", f"member {info.filename} is larger than {MAX_MEMBER} bytes: not read")
            return None
        self.total += len(raw)
        if self.total > MAX_TOTAL:
            self.note("info", f"project holds more than {MAX_TOTAL} bytes of members: the rest is not read")
            return None
        raw = secrets.mask_for_extraction(raw, secrets.scan(raw))
        return _Member(info, raw, start, end, stored)

    # ------------------------------------------------------------------ positions

    def file_span(self, a: int, b: int):
        return self.base.span(self.data, a, b)

    def at(self, member: _Member | None, needle: str | None = None):
        """Where `needle` is in a stored UTF-8 member, else the member's bytes, else the whole file."""
        if member is None:
            return self.base.whole_file(self.data)
        if needle and member.stored:
            for raw in (needle.encode("utf-8"), needle.encode("utf-16-le")):
                i = member.data.find(raw)
                if i >= 0:
                    return self.file_span(member.start + i, member.start + i + len(raw))
        return self.file_span(member.start, member.end)

    # ------------------------------------------------------------------ emission

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any]) -> dict[str, Any]:
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in attrs.items() if v not in (None, [], "")}, E1)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, qualifiers or {}, E1))

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    # ------------------------------------------------------------------ the project

    def run(self):
        try:
            zf = zipfile.ZipFile(io.BytesIO(self.data))
        except (zipfile.BadZipFile, OSError, ValueError):
            return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "warning", "not a zip archive")], parsed=False)
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "info", f"archive has more than {MAX_ENTRIES} members: not read")], parsed=False)
            names = {i.filename.lower(): i for i in infos}
            info = names.get("project.xml")
            if info is None:
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "warning", "no project.xml in the archive")], parsed=False)
            project_member = self.read(zf, info)
            if project_member is None:
                return self.base.Extraction(diagnostics=self.diagnostics, parsed=False)
            text = _decode_xml(project_member.data)
            if text is None:
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "warning", "project.xml is not decodable")], parsed=False)
            if re.search(r"<!(?:ENTITY|DOCTYPE)", text, re.I):
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "warning", "project.xml with a DOCTYPE or entity declarations is not read")], parsed=False)
            try:
                root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip("﻿").lstrip()))
            except ET.ParseError as exc:
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "warning", f"project.xml is not well-formed: {exc}")], parsed=False)
            if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
                return self.base.Extraction(diagnostics=[self.base.diagnostic(EXTRACTOR, "info", f"project.xml has more than {MAX_ELEMENTS} elements: not read")], parsed=False)
            self.project_member = project_member
            self.elements(zf, names, root)
        self.items += self.base_lineage_summary()
        return self.base.Extraction(self.items, self.diagnostics)

    def base_lineage_summary(self) -> list:
        from repo_intel.extractors import sql_lineage

        return sql_lineage.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)

    def elements(self, zf: zipfile.ZipFile, names: dict[str, zipfile.ZipInfo], root: ET.Element) -> None:
        pm = self.project_member
        whole = self.base.whole_file(self.data)
        flows: dict[str, dict[str, Any]] = {}  # flow id -> {label, ref, job name}
        nodes: dict[str, dict[str, Any]] = {}  # element id -> {kind: task|data, ...}
        links: list[tuple[str, str, Any]] = []
        all_elements = [el for el in root.iter() if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "Element" and el.get("Type")]
        attrs: dict[str, Any] = {"kind": "project", "language": "sas", "format": "egp"}
        version = root.get("EGVersion") or _text(root, "EGVersion")
        if version and re.fullmatch(r"[\d.]{1,20}", version):
            attrs["eg_version"] = version
        self.entity(whole, "sas:EgProject", self.project["natural_key"], self.project["name"], attrs)
        self.construct(whole, "sas:eg_project", True)

        # Process flows first: tasks name the flow that holds them.
        labels_seen: dict[str, int] = {}
        for el in all_elements:
            if _short(el.get("Type")) not in ("pfd", "processflow"):
                continue
            head = _child(el, "Element")
            fid, label = _text(head, "ID"), _text(head, "Label")
            at = self.at(pm, label)
            if not fid or not label:
                self.construct(at, "sas:eg_process_flow", False)
                continue
            n = labels_seen[label] = labels_seen.get(label, 0) + 1
            label = label if n == 1 else f"{label}~{n}"
            self.construct(at, "sas:eg_process_flow", True)
            job_name = f"{self.path}#{label}"
            ref = self.entity(at, "sas:EgProcessFlow", {"framework": FRAMEWORK, "name": job_name}, job_name, {"type": "eg_process_flow", "label": label, "project": self.path})
            self.relation(at, self.project, P.CONTAINS, ref)
            flows[fid] = {"label": label, "ref": ref, "job": job_name, "tasks": {}}

        for el in all_elements:
            kind = _short(el.get("Type"))
            if kind in ("pfd", "processflow"):
                continue
            head = _child(el, "Element")
            eid, label, container = _text(head, "ID"), _text(head, "Label"), _text(head, "Container")
            at = self.at(pm, label or eid)
            if kind in _TASK_TYPES or kind.endswith("task"):
                entry = self.task(zf, names, el, kind, eid, label, container, flows, at)
                if entry is not None and eid:
                    nodes[eid] = entry
            elif kind in ("shortcuttodata", "data", "dataset"):
                entry = self.data_node(el, label, at)
                if entry is not None and eid:
                    nodes[eid] = entry
            elif kind == "link":
                src = _find_text(el, "LinkFrom") or _find_text(el, "From")
                dst = _find_text(el, "LinkTo") or _find_text(el, "To")
                links.append((src or "", dst or "", at))
            elif kind in _OTHER_TYPES:
                if _OTHER_TYPES[kind]:
                    self.construct(at, _OTHER_TYPES[kind], False)
            else:
                self.construct(at, "sas:eg_unknown_element", False)

        self.order(nodes, links)

    def task(self, zf, names, el: ET.Element, kind: str, eid: str | None, label: str | None, container: str | None, flows, at) -> dict[str, Any] | None:
        code_type = kind == "codetask"
        query = kind == "query" or "query" in (_find_text(el, "TaskID") or "").lower() or "query" in (el.get("Type") or "").lower()
        cid = "sas:eg_code_task" if code_type else "sas:eg_query_task" if query else "sas:eg_task"
        flow = flows.get(container or "")
        if not eid or not label:
            self.construct(at, cid, False)
            return None
        if flow is None:
            self.construct(at, cid, False)
            self.note("info", f"task {label} is in no process flow this project lists: not modelled")
            return None
        seen = flow["tasks"]
        n = seen[label] = seen.get(label, 0) + 1
        name = label if n == 1 else f"{label}~{n}"
        self.construct(at, cid, True)
        key = {"framework": FRAMEWORK, "job": flow["job"], "name": name}
        tattrs: dict[str, Any] = {"type": "code" if code_type else "query" if query else "task", "label": label}
        task_id = _find_text(el, "TaskID")
        if task_id and re.fullmatch(r"[\w.:-]{1,80}", task_id) and not code_type:
            tattrs["task_id"] = task_id
        info = names.get(f"{eid}/code.sas".lower()) if code_type else None
        if code_type and info is None:
            if (_find_text(el, "Embedded") or "").lower() == "true":
                self.note("warning", f"embedded code of task {name} is missing from the archive")
            else:
                self.construct(at, "sas:eg_external_code", False)
                full = _find_text(el, "FullPath") or ""
                if not full:
                    m = re.search(r"<FullPath>([^<]+)</FullPath>", _find_text(el, "DNA") or "")
                    full = m.group(1) if m else ""
                base = re.split(r"[\\/]", full.strip())[-1] if full.strip() else None
                if base and re.fullmatch(r"[\w .-]{1,120}", base):
                    tattrs["external_file"] = base
                self.note("info", f"code task {name} runs a file outside the project: not linked")
        ref = self.entity(at, "sas:EgTask", key, name, tattrs)
        self.relation(at, flow["ref"], P.CONTAINS, ref)
        if info is not None:
            self.code(zf, info, flow, name, ref)
        return {"kind": "task", "ref": ref, "at": at}

    def code(self, zf, info: zipfile.ZipInfo, flow, task_name: str, task_ref) -> None:
        member = self.read(zf, info)
        if member is None:
            return
        self.construct(self.at(member), "sas:eg_embedded_code", True)
        program_name = f"{self.path}#{flow['label']}/{task_name}"
        self.lower(member, program_name, flow, task_name, task_ref)

    def lower(self, member: _Member, program_name: str, flow, task_name: str, task_ref) -> None:
        """Run the SAS analysis on one embedded program and move what it found into this file."""
        from repo_intel.compile.contracts import Evidence, Observation
        from repo_intel.platforms.sas.analysis import Analysis, decode

        text = decode(member.data)
        if text is None:
            self.note("warning", f"code of task {task_name} is not decodable")
            return
        try:
            out = Analysis(program_name, member.data, self.blob, text).run()
        except Exception as exc:  # the parser's failure on one task must not lose the project
            self.note("warning", f"code of task {task_name} not read: {type(exc).__name__}")
            return
        program = {"kind": "Program", "natural_key": {"platform": "sas", "name": program_name}, "name": program_name.rsplit("/", 1)[-1]}

        def move(o):
            s = o.evidence.span
            if member.stored:
                a, b = member.start + s.start_byte, member.start + s.end_byte
            else:
                a, b = member.start, member.end
            span = self.file_span(a, min(b, len(self.data)))
            ev = Evidence.make(self.blob, span, o.evidence.observation_kind, ast_node_kind=o.evidence.ast_node_kind)
            return Observation(kind=o.kind, evidence=ev, payload=o.payload, evidence_class=o.evidence_class)

        declared = False
        for o in out.observations:
            p = o.payload
            if o.kind == "lineage_summary":
                self.resolved += int(p.get("resolved", 0))
                self.unresolved += int(p.get("unresolved", 0))
                continue
            if o.kind == "entity" and p.get("kind") == "Program" and p.get("natural_key") == program["natural_key"]:
                attrs = {k: v for k, v in p.get("attributes", {}).items() if k not in ("platform", "platform_kind", "fidelity")}
                attrs.update({"embedded": True, "project": self.path, "process_flow": flow["label"], "task": task_name})
                moved = move(o)
                self.items.append(platform_entity(self.blob, moved.evidence.span, "sas:EgCode", program["natural_key"], program["name"], attrs, E1))
                declared = True
                continue
            if o.kind == "behavior":
                p = {**p, "body": _shift(p.get("body", []), member)}
                o = Observation(kind=o.kind, evidence=o.evidence, payload=p, evidence_class=o.evidence_class)
            moved = move(o)
            self.items.append(moved)
            if o.kind == "relation" and p.get("subject") == program and p.get("predicate") in (str(P.READS_FROM), str(P.WRITES_TO)):
                self.relation(moved.evidence.span, task_ref, p["predicate"], p["object"], {"via": "code"})
        if not declared:  # a program with no statements is still what the task runs
            self.items.append(platform_entity(self.blob, self.at(member), "sas:EgCode", program["natural_key"], program["name"],
                                              {"kind": "program", "language": "sas", "embedded": True, "project": self.path, "process_flow": flow["label"], "task": task_name}, E1))
        self.relation(self.at(member), task_ref, P.RUNS, program)
        for d in out.diagnostics:
            self.note(d.severity, f"task {task_name}: {d.message}")

    def data_node(self, el: ET.Element, label: str | None, at) -> dict[str, Any] | None:
        dna = _find_text(el, "DNA") or ""
        lib = _find_text(el, "Library") or (re.search(r"<Library>\s*([^<\s]+)\s*</Library>", dna) or [None, None])[1]
        mem = _find_text(el, "Member") or (re.search(r"<Member>\s*([^<\s]+)\s*</Member>", dna) or [None, None])[1]
        if lib and mem and _LIBREF.fullmatch(lib) and _SAS_NAME.fullmatch(mem):
            name = f"{lib}.{mem}".lower()
            if lib.lower() in _RESERVED_LIBRARIES:
                self.construct(at, "sas:eg_data", False)
                return {"kind": "data", "ref": None, "label": label, "at": at}
            self.construct(at, "sas:eg_data", True)
            ref = self.entity(at, "sas:Dataset", {"name": name}, name, {"source": "egp"})
            return {"kind": "data", "ref": ref, "label": label, "at": at}
        self.construct(at, "sas:eg_data", False)
        return {"kind": "data", "ref": None, "label": label, "at": at}

    def order(self, nodes: dict[str, dict[str, Any]], links: list[tuple[str, str, Any]]) -> None:
        """DEPENDS_ON in flow order, through data nodes, and the datasets tasks read and write."""
        into: dict[str, list[str]] = {}
        for src, dst, at in links:
            a, b = nodes.get(src), nodes.get(dst)
            self.construct(at, "sas:eg_link", a is not None and b is not None)
            if a is None or b is None:
                continue
            into.setdefault(dst, []).append(src)
            if a["kind"] == "task" and b["kind"] == "task":
                self.relation(at, b["ref"], P.DEPENDS_ON, a["ref"], {"via": "link"})
            elif a["kind"] == "data" and b["kind"] == "task" and a["ref"] is not None:
                self.relation(at, b["ref"], P.READS_FROM, a["ref"], {"via": "link"})
            elif a["kind"] == "task" and b["kind"] == "data" and b["ref"] is not None:
                self.relation(at, a["ref"], P.WRITES_TO, b["ref"], {"via": "link"})
        # A task fed by a data node that another task produced runs after that task.
        for dst, sources in sorted(into.items()):
            b = nodes[dst]
            if b["kind"] != "task":
                continue
            for src in sorted(sources):
                if nodes[src]["kind"] != "data":
                    continue
                for upstream in sorted(into.get(src, [])):
                    u = nodes[upstream]
                    if u["kind"] == "task" and upstream != dst:
                        self.relation(b["at"], b["ref"], P.DEPENDS_ON, u["ref"], {"via": "data"})


def _shift(node: Any, member: _Member) -> Any:
    """A behaviour body with each `at` moved from the member's bytes to the archive's."""
    if isinstance(node, list):
        return [_shift(x, member) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "at" and isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v):
            out[k] = [member.start + v[0], member.start + v[1]] if member.stored else [member.start, member.end]
        else:
            out[k] = _shift(v, member)
    return out
