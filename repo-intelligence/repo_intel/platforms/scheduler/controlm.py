"""Control-M XML exports: a `DEFTABLE` of folders and the jobs they hold.

- Each `FOLDER`, `SMART_FOLDER`, `TABLE`, `SMART_TABLE` and `SUB_FOLDER` is a Job
  (`sched:ControlMFolder`, named by its folder name, a sub-folder by
  `<folder>/<sub-folder>`) that CONTAINS its jobs and sub-folders.
- Each `JOB` is a Job (`sched:ControlMJob`, `<folder>/<job name>`) with
  `attributes.schedule` (the scheduling fields as written, `DAYS=... WEEKDAYS=...
  TIMEFROM=...`, cyclic runs included), `calendar` (DAYSCAL, else WEEKSCAL, else the
  first rule-based calendar) and `calendars` (every calendar it names).
- An `INCOND` makes the job DEPEND_ON every job of the same file whose `OUTCOND` (or a
  `DOCOND` of an ON statement) adds that condition. A condition no job in the file adds
  is kept in `unresolved_conditions` with a diagnostic: the producer may be in another
  export, and the link is never guessed from the condition's name.
- What a job runs: a z/OS member (`MEMNAME` in a `MEMLIB` that is a data set name) is
  the JCL job of that name; a script (`MEMNAME` in a directory `MEMLIB`) and a
  `CMDLINE` are read as `common` describes. AutoEdit variables (`%%NAME`) make the
  target dynamic.

Values that may be secret or name a machine are never read: `NODEID`, `RUN_AS`,
`OWNER`, AutoEdit values, shout destinations. Resources, ON/DO actions, shouts and
plug-in job types are counted.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from repo_intel.platforms.scheduler.common import MEMBER, NAME_SAFE, P, display_path, is_dataset_name, is_dynamic, short

FRAMEWORK = "control-m"
MAX_ELEMENTS = 100000
FOLDERS = {"FOLDER": "folder", "SMART_FOLDER": "smart_folder", "TABLE": "table", "SMART_TABLE": "smart_table", "SUB_FOLDER": "sub_folder"}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_CALENDAR_DAYS = ("DAYS", "WEEKDAYS", "DATE", "DAYS_AND_OR", "MONTHS", "TIMEFROM", "TIMETO", "TIMEZONE")
_CYCLIC = ("CYCLIC_TYPE", "INTERVAL", "CYCLIC_INTERVAL_SEQUENCE", "CYCLIC_TIMES_SEQUENCE", "CYCLIC_TOLERANCE")
_COUNTED = {"QUANTITATIVE": "sched:ctm.quantitative", "CONTROL": "sched:ctm.control", "SHOUT": "sched:ctm.shout", "VARIABLE": "sched:ctm.variable"}
_PLAIN_TYPES = {None, "", "OS", "Z/OS", "ZOS"}


def _name(el: ET.Element, *keys: str) -> str | None:
    for k in keys:
        v = (el.get(k) or "").strip()
        if v:
            return v
    return None


class _Reader:
    def __init__(self, em):
        self.em = em
        self.records: list[dict[str, Any]] = []
        self.producers: dict[str, list[str]] = {}
        self.pos = 0

    def tag(self, name: str):
        """Span of the next `<name ...>` start tag (elements are visited in document order)."""
        m = re.compile(rf"<{re.escape(name)}\b[^>]*>?").search(self.em.text, self.pos)
        if not m:
            return self.em.whole()
        self.pos = m.end()
        return self.em.span(m.start(), m.end())

    # ---------------------------------------------------------------- walk

    def folder(self, el: ET.Element, parent: str | None) -> None:
        at = self.tag(el.tag)
        own = _name(el, "FOLDER_NAME", "TABLE_NAME", "JOBNAME", "PARENT_FOLDER")
        if own is None or not NAME_SAFE.match(own):
            self.em.construct(at, "sched:ctm.folder", False)
            self.em.info("a folder with no readable name was skipped")
            self.skip(el)
            return
        name = self.em.unique(FRAMEWORK, f"{parent}/{own}" if parent else own)
        self.em.construct(at, "sched:ctm.folder", True)
        record = self.record(el, at, "sched:ControlMFolder", name, parent, FOLDERS[el.tag])
        for child in el:
            if child.tag == "JOB":
                self.job(child, name)
            elif child.tag in FOLDERS:
                self.folder(child, name)
            elif child.tag == "RULE_BASED_CALENDAR":
                self.em.construct(self.tag(child.tag), "sched:ctm.rule-based-calendar-definition", False)
                self.skip(child)
            else:
                self.child(child, record)

    def job(self, el: ET.Element, folder: str) -> None:
        at = self.tag("JOB")
        own = _name(el, "JOBNAME", "MEMNAME")
        if own is None or not NAME_SAFE.match(own):
            self.em.construct(at, "sched:ctm.job", False)
            self.em.info(f"a job in folder {folder} with no readable name was skipped")
            self.skip(el)
            return
        name = self.em.unique(FRAMEWORK, f"{folder}/{own}")
        self.em.construct(at, "sched:ctm.job", True)
        record = self.record(el, at, "sched:ControlMJob", name, folder, (el.get("TASKTYPE") or "Job").strip().lower() or "job")
        for child in el:
            self.child(child, record)
        self.runs(el, record)

    def skip(self, el: ET.Element) -> None:
        """Move past the start tags of an element that is not read."""
        for sub in list(el.iter())[1:]:
            self.tag(sub.tag)

    def record(self, el: ET.Element, at, term: str, name: str, parent: str | None, kind: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "kind": kind, "application": _safe(el.get("APPLICATION")), "sub_application": _safe(el.get("SUB_APPLICATION")),
            "description": short(el.get("DESCRIPTION")),
        }
        schedule = _schedule(el)
        if schedule:
            cyclic = (el.get("CYCLIC") or "").strip() == "1"
            if any((el.get(k) or "").strip() for k in _CALENDAR_DAYS) or any((el.get(m) or "") == "1" for m in _MONTHS):
                self.em.construct(at, "sched:ctm.schedule", True)
            if cyclic:
                self.em.construct(at, "sched:ctm.cyclic", True)
            attrs["schedule"] = schedule
        calendars = []
        for key in ("DAYSCAL", "WEEKSCAL", "CONFCAL"):
            cal = _safe(el.get(key))
            if cal:
                self.em.construct(at, "sched:ctm.calendar", True)
                calendars.append(cal)
        record = {"el": el, "at": at, "term": term, "name": name, "parent": parent, "attrs": attrs, "calendars": calendars, "rule_calendars": [], "inconds": [], "outconds": []}
        self.records.append(record)
        return record

    def child(self, el: ET.Element, record: dict[str, Any]) -> None:
        tag = el.tag
        if tag == "INCOND":
            at = self.tag(tag)
            cond = _safe(el.get("NAME"))
            if cond:
                record["inconds"].append((at, cond, (el.get("AND_OR") or "A").upper(), (el.get("ODATE") or "").upper()))
            else:
                self.em.construct(at, "sched:ctm.incond", False)
        elif tag == "OUTCOND":
            at = self.tag(tag)
            cond = _safe(el.get("NAME"))
            if (el.get("SIGN") or "+").upper() in ("+", "ADD") and cond:
                self.em.construct(at, "sched:ctm.outcond", True)
                record["outconds"].append(cond)
                self.producers.setdefault(cond, []).append(record["name"])
            else:
                self.em.construct(at, "sched:ctm.outcond-delete", False)
        elif tag == "RULE_BASED_CALENDARS":
            at = self.tag(tag)
            cal = _safe(el.get("NAME"))
            self.em.construct(at, "sched:ctm.rule-based-calendar", bool(cal))
            if cal:
                record["rule_calendars"].append(cal)
        elif tag == "ON":
            self.em.construct(self.tag(tag), "sched:ctm.on-do", False)
            for sub in el:
                at = self.tag(sub.tag)
                if sub.tag == "DOFORCEJOB":
                    self.em.construct(at, "sched:ctm.doforcejob", False)
                elif sub.tag == "DOCOND" and (sub.get("SIGN") or "+").upper() in ("+", "ADD") and _safe(sub.get("NAME")):
                    self.producers.setdefault(sub.get("NAME").strip(), []).append(record["name"])
                for deeper in list(sub.iter())[1:]:
                    self.tag(deeper.tag)
        elif tag in _COUNTED:
            self.em.construct(self.tag(tag), _COUNTED[tag], False)
            self.skip(el)
        else:
            self.em.construct(self.tag(tag), "sched:ctm.other", False)
            self.skip(el)

    def runs(self, el: ET.Element, record: dict[str, Any]) -> None:
        em, at, attrs, name = self.em, record["at"], record["attrs"], record["name"]
        me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
        appl = (el.get("APPL_TYPE") or "").strip()
        if appl.upper() not in _PLAIN_TYPES:
            em.construct(at, "sched:ctm.application-type", False)
            attrs["application_type"] = _safe(appl)
        kind = attrs["kind"]
        cmdline = (el.get("CMDLINE") or "").strip()
        memname, memlib = (el.get("MEMNAME") or "").strip(), (el.get("MEMLIB") or "").strip()
        if kind == "dummy":
            em.construct(at, "sched:ctm.dummy", True)
        elif cmdline:
            em.command(at, me, f"job {name}", cmdline, attrs, "sched:ctm.cmdline")
        elif memname:
            if is_dynamic(memname) or is_dynamic(memlib):
                em.info(f"job {name}: its member or library is set at run time (AutoEdit): not linked")
                attrs["runs_dynamic"] = True
                em.construct(at, "sched:ctm.memname", False)
            elif is_dataset_name(memlib) or (not memlib and MEMBER.match(memname)):
                attrs["jcl_member"], attrs["jcl_library"] = memname.upper(), memlib.upper() or None
                em.jcl(at, me, memname, certain=False)
                em.construct(at, "sched:ctm.memname", True)
            else:
                path = f"{memlib.rstrip('/').rstrip(chr(92))}/{memname}" if memlib else memname
                shown = display_path(path)
                linked = em.script(at, me, path)
                if linked and shown:
                    attrs["scripts"] = [shown]
                em.construct(at, "sched:ctm.memname", bool(linked and shown))

    # ---------------------------------------------------------------- emit

    def emit(self) -> None:
        em = self.em
        for r in self.records:
            me = em.base.ref("Job", {"framework": FRAMEWORK, "name": r["name"]}, r["name"])
            unresolved = []
            for at, cond, and_or, odate in r["inconds"]:
                producers = sorted(set(p for p in self.producers.get(cond, []) if p != r["name"]))
                em.construct(at, "sched:ctm.incond", bool(producers))
                if not producers:
                    unresolved.append(cond)
                    em.info(f"{r['name']} waits for condition {cond}, which no job in this export adds: not linked")
                for p in producers:
                    em.relation(at, me, P.DEPENDS_ON, em.base.ref("Job", {"framework": FRAMEWORK, "name": p}, p),
                                {"condition": cond, "logic": "or" if and_or in ("O", "OR") else None, "odate": odate if odate not in ("", "ODAT") else None})
            calendars = r["calendars"] + r["rule_calendars"]
            attrs = dict(r["attrs"])
            primary = _safe(r["el"].get("DAYSCAL")) or _safe(r["el"].get("WEEKSCAL")) or (r["rule_calendars"][0] if r["rule_calendars"] else None)
            attrs.update(
                calendar=primary, calendars=sorted(set(calendars)), in_conditions=sorted({c for _, c, _, _ in r["inconds"]}), out_conditions=sorted(set(r["outconds"])),
                unresolved_conditions=sorted(set(unresolved)),
            )
            em.job(r["at"], r["term"], FRAMEWORK, r["name"], attrs)
            if r["parent"]:
                parent = em.base.ref("Job", {"framework": FRAMEWORK, "name": r["parent"]}, r["parent"])
                em.relation(r["at"], parent, P.CONTAINS, me)


def _safe(value: str | None) -> str | None:
    v = (value or "").strip()
    return v if v and NAME_SAFE.match(v) else None


def _schedule(el: ET.Element) -> str | None:
    parts = []
    for key in _CALENDAR_DAYS:
        if key == "MONTHS":
            months = [m for m in _MONTHS if (el.get(m) or "").strip() == "1"]
            if months and len(months) < 12:
                parts.append("MONTHS=" + ",".join(months))
            continue
        v = (el.get(key) or "").strip()
        if v and re.fullmatch(r"[\w,.:+/ -]{1,120}", v):
            parts.append(f"{key}={v}")
    if (el.get("CYCLIC") or "").strip() == "1":
        parts.append("CYCLIC=1")
        for key in _CYCLIC:
            v = (el.get(key) or "").strip()
            if v and re.fullmatch(r"[\w,.:+/ -]{1,120}", v):
                parts.append(f"{key}={v}")
    return " ".join(parts) or None


def read(em, root: ET.Element) -> None:
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        em.info(f"export has more than {MAX_ELEMENTS} elements: not read")
        return
    reader = _Reader(em)
    reader.tag(root.tag)
    for child in root:
        if child.tag in FOLDERS:
            reader.folder(child, None)
        else:
            reader.em.construct(reader.tag(child.tag), "sched:ctm.other", False)
            reader.skip(child)
    reader.emit()
