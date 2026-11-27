"""CA-7 job listings: the output of `LJOB` (with `LIST=ALL`, `RQMT`, `TRIG` or `SCHD`).

The listing is a report, so only parts whose layout is certain are read:

- The job rows under the column heading that names `NAME`, `ID` and `MEMBER`: job
  name, JCL id (three digits), JCL member and system name. Each is a Job
  (`sched:Ca7Job`); CA-7 requires a job's name to be the name on its JOB statement, so
  the job RUNS the JCL job of that name (evidence E1), with the member in `jcl_member`
  and the system in `application`.
- `KEY=VALUE` lines under a dashed section heading, which belong to the job row before
  them:
  - `REQUIREMENTS`: `JOB=` is a predecessor (DEPENDS_ON, `on: completion`); `DSN=` a
    data set the job waits for, kept by name in `requires_datasets`; `USR=` (a manual
    check, free text never copied) and `NWK=` (a network) are counted.
  - `TRIGGERED JOBS`: `JOB=` is a job this one starts on completion, which therefore
    DEPENDS_ON it (`via: trigger`); `TRIGGERED BY` the reverse. A data set trigger is
    counted.
  - `SCHEDULE`: each `SCHID=` starts a schedule; its words (`ROLL=`, `DOTM=`,
    `DAILY`, `WEEKLY DAYS=...`) are the schedule, `SCAL=` names its calendar.

A listing in another layout gives no jobs and a diagnostic.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.scheduler.common import MEMBER, P, is_dataset_name

FRAMEWORK = "ca7"
_HEADING = re.compile(r"^\s*NAME\s+ID\s+MEMBER\b", re.I)
_ROW = re.compile(r"^\s*([A-Z#@$][A-Z0-9#@$]{0,7})\s+(\d{3})\s+([A-Z#@$][A-Z0-9#@$]{0,7})(?:\s+([A-Z0-9#@$-]{1,8})\b)?", re.I)
_SECTION = re.compile(r"^\s*-{3,}\s*([A-Z][A-Z ]*?)\s*-{3,}\s*$", re.I)
_PAIR = re.compile(r"\b([A-Z]{3,7})=(\S*)", re.I)
_SAFE_WORD = re.compile(r"^[A-Z0-9#@$=,+.:/*-]{1,40}$", re.I)


def read(em, text: str) -> None:
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_rows = False
    rows = 0
    section: str | None = None
    pos = 0
    for line in text.split("\n"):
        start, pos = pos, pos + len(line) + 1
        if _HEADING.match(line):
            in_rows, rows, section = True, 0, None
            continue
        sec = _SECTION.match(line)
        if sec:
            section, in_rows = " ".join(sec.group(1).upper().split()), False
            continue
        if not line.strip():
            in_rows = in_rows and not rows  # the rows under a heading end at the first blank line after them
            continue
        if in_rows:
            m = _ROW.match(line)
            if m:
                rows += 1
                current = {"name": m.group(1).upper(), "member": m.group(3).upper(), "system": (m.group(4) or "").upper() or None,
                           "at": em.span(start + m.start(1), start + m.end(1)), "row": em.span(start, start + len(line.rstrip())),
                           "requires": [], "datasets": [], "triggers": [], "triggered_by": [], "schedules": [], "calendars": []}
                jobs.append(current)
            continue
        if current is None or section is None:
            continue
        _section_line(em, current, section, line, start)
    if not jobs:
        em.info("no job rows in the layout this reader knows (a NAME ID MEMBER heading): nothing read")
    for job in jobs:
        _emit(em, job)


def _section_line(em, job: dict[str, Any], section: str, line: str, start: int) -> None:
    pairs = [(m.group(1).upper(), m.group(2), start + m.start(2), start + m.end(2)) for m in _PAIR.finditer(line)]
    if section == "REQUIREMENTS":
        for key, value, a, b in pairs:
            where = em.span(a, b)
            if key == "JOB":
                ok = bool(MEMBER.match(value.upper()))
                em.construct(where, "sched:ca7.requirement-job", ok)
                if ok:
                    job["requires"].append((value.upper(), where))
            elif key == "DSN":
                ok = is_dataset_name(value)
                em.construct(where, "sched:ca7.requirement-dataset", ok)
                if ok:
                    job["datasets"].append(value.upper())
            elif key == "USR":
                em.construct(where, "sched:ca7.user-requirement", False)
                break  # the rest of the line is the requirement's text
            elif key == "NWK":
                em.construct(where, "sched:ca7.network", False)
    elif section in ("TRIGGERED JOBS", "TRIGGERED BY"):
        for key, value, a, b in pairs:
            where = em.span(a, b)
            if key == "JOB":
                ok = bool(MEMBER.match(value.upper()))
                em.construct(where, "sched:ca7.trigger", ok)
                if ok:
                    job["triggers" if section == "TRIGGERED JOBS" else "triggered_by"].append((value.upper(), where))
            elif key == "DSN":
                em.construct(where, "sched:ca7.trigger", False)
    elif section == "SCHEDULE":
        words = line.split()
        if not words:
            return
        if words[0].upper().startswith("SCHID="):
            job["schedules"].append([])
            em.construct(em.span(start, start + len(line.rstrip())), "sched:ca7.schedule", True)
        if not job["schedules"]:
            return
        job["schedules"][-1] += [w.upper() for w in words if _SAFE_WORD.match(w)]
        for key, value, a, b in pairs:
            if key == "SCAL" and re.fullmatch(r"[A-Z0-9#@$]{1,8}", value, re.I):
                em.construct(em.span(a, b), "sched:ca7.calendar", True)
                job["calendars"].append(value.upper())


def _emit(em, job: dict[str, Any]) -> None:
    name = em.unique(FRAMEWORK, job["name"])
    me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
    em.construct(job["at"], "sched:ca7.job", True)
    em.construct(job["row"], "sched:ca7.jcl-member", True)
    em.jcl(job["row"], me, job["name"], certain=True)
    for other, where in job["requires"]:
        em.depends(where, me, FRAMEWORK, other, {"on": "completion"})
    for other, where in job["triggered_by"]:
        em.depends(where, me, FRAMEWORK, other, {"via": "trigger"})
    for other, where in job["triggers"]:
        em.relation(where, em.base.ref("Job", {"framework": FRAMEWORK, "name": other}, other, declare=False), P.DEPENDS_ON, me, {"via": "trigger"})
    em.job(job["at"], "sched:Ca7Job", FRAMEWORK, name, {
        "kind": "job", "jcl_member": job["member"], "application": job["system"],
        "schedule": "; ".join(" ".join(s) for s in job["schedules"] if s) or None,
        "calendar": job["calendars"][0] if job["calendars"] else None, "calendars": sorted(set(job["calendars"])),
        "requires_datasets": sorted(set(job["datasets"])),
    })
