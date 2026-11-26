"""Native SAS inputs placed under `.rkp/inputs/sas/`: metadata exports and configuration.

SAS keeps much of a solution outside its programs: jobs and stored processes live in
the metadata server, environment variables in the `sasv9.cfg` configuration file.
A team exports what it wants modelled into `.rkp/inputs/sas/`. Programs placed there
(`autoexec.sas`, `config.sas`) are read by `sas.source`; this extractor reads:

`*.xml`: a simple metadata export. The shape read (element names are matched without
namespace and case-insensitively; anything else is ignored):

    <Job Name="nightly_sales">                       DI Studio job      -> Job
      <Step Name="extract" Program="sas/extract.sas"> transformation   -> Task, RUNS Program
        <Source Table="rpt.sales"/>                   input table      -> Task READS_FROM Dataset
        <Target Table="rpt.summary"/>                 output table     -> Task WRITES_TO Dataset
      </Step>
      <Step Name="load" DependsOn="extract"/>         explicit order   -> Task DEPENDS_ON Task
    </Job>
    <ProcessFlow Name="...">                          Enterprise Guide flow -> Job
      <Task Name="..." Program="..."/>                program node      -> Task
      <Link From="a" To="b"/>                         flow link         -> b DEPENDS_ON a
    </ProcessFlow>
    <StoredProcess Name="..." SourceFile="stp/x.sas"> stored process
      <Prompt Name="region" Type="Text"/>             prompt            -> UIControl
    </StoredProcess>

A DI job runs its steps in the order listed unless a step names `DependsOn`, so steps
without any `DependsOn` in their job depend on the step before them. An Enterprise Guide
flow runs what its links say, so its tasks are ordered only by `Link` elements.
`Program` and `SourceFile` are repository paths; anything absolute, with a drive or
climbing out of the repository, is not linked. Connection details, server names,
credentials and option values in an export are never read.

`*.cfg`: a SAS configuration file. Each `-SET NAME value` declares the environment
variable NAME as a ConfigKey `sas_env:NAME` (its value is never copied), which
programs reading `%SYSGET(NAME)` reference.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Any

from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity

P = Predicate
E1 = EvidenceClass.E1
PREFIX = ".rkp/inputs/sas/"
MAX_ELEMENTS = 20000
_SET = re.compile(r"^[ \t]*-set[ \t]+([A-Za-z_]\w*)", re.I | re.M)
_OPTION = re.compile(r"^[ \t]*-([A-Za-z_]\w*)", re.M)
_DATASET = re.compile(r"^([A-Za-z_]\w{0,7})\.([A-Za-z_]\w{0,31})$")
_RESERVED_LIBRARIES = {"work", "sashelp", "sasuser"}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _attr(el: ET.Element, *names: str) -> str | None:
    lowered = {k.rsplit("}", 1)[-1].lower(): v for k, v in el.attrib.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v and v.strip():
            return v.strip()
    return None


def _repo_path(value: str | None) -> str | None:
    if not value:
        return None
    v = value.replace("\\", "/").strip()
    if v.startswith("/") or re.match(r"^[A-Za-z]:", v) or "&" in v or "://" in v:
        return None
    norm = posixpath.normpath(v)
    return None if norm.startswith("..") or norm == "." else norm


class SasInputs:
    id = "sas.inputs"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        p = path.lower()
        return p.startswith(PREFIX) and p.endswith((".xml", ".cfg"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        if path.lower().endswith(".cfg"):
            return self._config(path, data, blob, text, base)
        return self._metadata(path, data, blob, text, base)

    # ------------------------------------------------------------------ sasv9.cfg

    def _config(self, path: str, data: bytes, blob: str, text: str, base) -> Any:
        items = []
        seen = set()
        for m in _OPTION.finditer(text):
            at = base.span(data, len(text[: m.start(1)].encode()), len(text[: m.end(1)].encode()))
            items.append(construct_observation(blob, at, "sas:config_option", m.group(1).lower() == "set", evidence_class=E1))
        for m in _SET.finditer(text):
            name = m.group(1).upper()
            if name in seen:
                continue
            seen.add(name)
            a, b = len(text[: m.start(1)].encode()), len(text[: m.end(1)].encode())
            key = {"name": f"sas_env:{name}"}
            items.append(platform_entity(blob, base.span(data, a, b), "sas:EnvironmentVariable", key, key["name"], {"source": "sas_config", "declared_in": posixpath.basename(path)}, E1))
        return base.Extraction(items, [])

    # ------------------------------------------------------------------ metadata XML

    def _metadata(self, path: str, data: bytes, blob: str, text: str, base) -> Any:
        if re.search(r"<!(?:ENTITY|DOCTYPE)", text, re.I):
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "XML with a DOCTYPE or entity declarations is not read")], parsed=False)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not well-formed XML: {exc}")], parsed=False)
        out = _Out(path, data, blob, text, base)
        count = 0
        for el in root.iter():
            count += 1
            if count > MAX_ELEMENTS:
                out.diagnostics.append(base.diagnostic(self.id, "info", f"export has more than {MAX_ELEMENTS} elements: the rest is not read"))
                break
            tag = _local(el.tag)
            if tag in ("job", "jobdefinition"):
                out.job(el, sequential=True)
            elif tag == "processflow":
                out.job(el, sequential=False)
            elif tag == "storedprocess":
                out.stored_process(el)
        return base.Extraction(out.items, out.diagnostics)


class _Out:
    def __init__(self, path: str, data: bytes, blob: str, text: str, base):
        self.path, self.data, self.blob, self.text, self.base = path, data, blob, text, base
        self.items: list = []
        self.diagnostics: list = []
        self.cursor = 0

    def at(self, name: str | None):
        """Span of the next occurrence of `name` in the export (elements are visited in document order)."""
        if name:
            raw = name.encode()
            i = self.data.find(raw, self.cursor)
            if i < 0:
                i = self.data.find(raw)
            if i >= 0:
                self.cursor = i + len(raw)
                return self.base.span(self.data, i, i + len(raw))
        return self.base.whole_file(self.data)

    def construct(self, at, cid: str, modelled: bool = True) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    def relation(self, at, subject: dict, predicate, obj: dict) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, {}, E1))

    def job(self, el: ET.Element, sequential: bool) -> None:
        name = _attr(el, "Name", "Label")
        at = self.at(name)
        if not name:
            self.construct(at, "sas:metadata_job", False)
            return
        self.construct(at, "sas:metadata_job")
        job_key = {"framework": "sas", "name": name}
        job = {"kind": "Job", "natural_key": job_key, "name": name}
        kind = "di_job" if sequential else "eg_process_flow"
        self.items.append(platform_entity(self.blob, at, "sas:MetadataJob", job_key, name, {"type": kind, "source": posixpath.basename(self.path)}, E1))
        steps = [c for c in el if _local(c.tag) in ("step", "transformation", "task", "node", "codetask")]
        explicit = any(_attr(s, "DependsOn") for s in steps)
        refs: dict[str, dict] = {}
        previous = None
        for s in steps:
            sname = _attr(s, "Name", "Label")
            sat = self.at(sname)
            if not sname:
                self.construct(sat, "sas:metadata_step", False)
                continue
            self.construct(sat, "sas:metadata_step")
            key = {"framework": "sas", "job": name, "name": sname}
            me = {"kind": "Task", "natural_key": key, "name": sname}
            attrs: dict[str, Any] = {"type": _local(s.tag)}
            transform = _attr(s, "Transformation", "Type")
            if transform and re.fullmatch(r"[\w .-]{1,80}", transform):
                attrs["transformation"] = transform
            self.items.append(platform_entity(self.blob, sat, "sas:MetadataStep", key, sname, attrs, E1))
            self.relation(sat, job, P.CONTAINS, me)
            refs[sname] = me
            program = _repo_path(_attr(s, "Program", "SourceFile", "Code"))
            if program:
                self.relation(sat, me, P.RUNS, {"kind": "Program", "natural_key": {"platform": "sas", "name": program}, "name": program.rsplit("/", 1)[-1], "declare": False})
            for c in s:
                tag = _local(c.tag)
                if tag in ("source", "input", "target", "output"):
                    self.table(c, me, P.READS_FROM if tag in ("source", "input") else P.WRITES_TO)
            if sequential and not explicit and previous is not None:
                self.relation(sat, me, P.DEPENDS_ON, previous)
            previous = me
        for s in steps:
            upstream = _attr(s, "DependsOn")
            sname = _attr(s, "Name", "Label")
            if explicit and upstream and sname in refs:
                for u in re.split(r"[,\s]+", upstream):
                    if u in refs:
                        self.relation(self.at(u), refs[sname], P.DEPENDS_ON, refs[u])
        for link in (c for c in el if _local(c.tag) == "link"):
            a, b = _attr(link, "From"), _attr(link, "To")
            if a in refs and b in refs:
                self.relation(self.at(b), refs[b], P.DEPENDS_ON, refs[a])

    def table(self, el: ET.Element, task: dict, predicate) -> None:
        raw = _attr(el, "Table", "Name")
        m = _DATASET.match(raw or "")
        if not m or m.group(1).lower() in _RESERVED_LIBRARIES:
            return
        name = raw.lower()
        at = self.at(raw)
        self.items.append(platform_entity(self.blob, at, "sas:Dataset", {"name": name}, name, {}, E1))
        self.relation(at, task, predicate, {"kind": "Dataset", "natural_key": {"name": name}, "name": name})

    def stored_process(self, el: ET.Element) -> None:
        source = _repo_path(_attr(el, "SourceFile", "Program"))
        at = self.at(_attr(el, "Name") or source)
        if not source:
            self.construct(at, "sas:stored_process", False)
            return
        self.construct(at, "sas:stored_process")
        screen = {"platform": "sas", "name": source}
        form = {"screen": source, "name": "prompts"}
        prompts = [p for p in el.iter() if _local(p.tag) == "prompt"]
        if not prompts:
            return
        base_name = source.rsplit("/", 1)[-1]
        self.items.append(platform_entity(self.blob, at, "sas:PromptPage", screen, base_name, {"stored_process": source}, E1))
        self.items.append(platform_entity(self.blob, at, "sas:PromptGroup", form, "prompts", {"stored_process": source}, E1))
        screen_ref = {"kind": "Screen", "natural_key": screen, "name": base_name}
        form_ref = {"kind": "Form", "natural_key": form, "name": "prompts"}
        self.relation(at, screen_ref, P.CONTAINS, form_ref)
        self.relation(at, form_ref, P.SUBMITS_TO, {"kind": "Program", "natural_key": {"platform": "sas", "name": source}, "name": base_name, "declare": False})
        for p in prompts:
            pname = _attr(p, "Name")
            if not pname or not re.fullmatch(r"[A-Za-z_]\w{0,31}", pname):
                continue
            pat = self.at(pname)
            self.construct(pat, "sas:stp_prompt")
            key = {"container": f"{source}#prompts", "name": pname.lower()}
            attrs: dict[str, Any] = {"macro_variable": pname.lower(), "control": "prompt"}
            ptype = _attr(p, "Type")
            if ptype and re.fullmatch(r"[\w -]{1,40}", ptype):
                attrs["prompt_type"] = ptype.lower()
            if (_attr(p, "Required") or "").lower() in ("true", "yes", "1"):
                attrs["required"] = True
            self.items.append(platform_entity(self.blob, pat, "sas:Prompt", key, pname.lower(), attrs, E1))
            self.relation(pat, form_ref, P.CONTAINS, {"kind": "UIControl", "natural_key": key, "name": pname.lower()})
