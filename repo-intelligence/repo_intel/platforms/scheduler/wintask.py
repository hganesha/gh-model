"""Windows Task Scheduler XML (the `schtasks /query /xml` or Task Scheduler export format).

The task is a Job (`sched:WindowsTask`) named by its `RegistrationInfo/URI`
(`\\Batch\\NightlyInvoices`), or `\\<file name>` when the export has none. Its triggers
are the schedule: each trigger's kind with its start boundary, its day, week or month
pattern and its repetition interval, as written, joined with `; `. An event trigger's
subscription query is not copied. Each `Exec` action is read as `common` describes,
its `Arguments` searched only for the script an interpreter is given and a relative
script resolved against `WorkingDirectory`. The principal it runs as is counted and
never copied; COM handler and message actions are counted.

Exports are usually UTF-16; `common.decode` reads the byte order mark.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET

from repo_intel.platforms.scheduler.common import NAME_SAFE, short

FRAMEWORK = "windows-task"
_TRIGGERS = {
    "CalendarTrigger": "sched:win.calendar-trigger", "TimeTrigger": "sched:win.time-trigger", "EventTrigger": "sched:win.event-trigger",
    "BootTrigger": "sched:win.boot-trigger", "LogonTrigger": "sched:win.boot-trigger", "IdleTrigger": "sched:win.other-trigger",
    "RegistrationTrigger": "sched:win.other-trigger", "SessionStateChangeTrigger": "sched:win.other-trigger",
}
_SAFE = re.compile(r"^[\w:.+-]{1,40}$")


def _text(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    child = el.find(name)
    return child.text.strip() if child is not None and child.text and child.text.strip() else None


def _trigger(t: ET.Element) -> str:
    parts = [t.tag]
    for key in ("StartBoundary", "EndBoundary", "Delay"):
        v = _text(t, key)
        if v and _SAFE.match(v):
            parts.append(f"{key}={v}")
    if (_text(t, "Enabled") or "true").lower() == "false":
        parts.append("Enabled=false")
    for by in ("ScheduleByDay", "ScheduleByWeek", "ScheduleByMonth", "ScheduleByMonthDayOfWeek"):
        el = t.find(by)
        if el is None:
            continue
        parts.append(by)
        for interval in ("DaysInterval", "WeeksInterval"):
            v = _text(el, interval)
            if v and _SAFE.match(v):
                parts.append(f"{interval}={v}")
        for group in ("DaysOfWeek", "Months", "DaysOfMonth", "Weeks"):
            g = el.find(group)
            if g is not None:
                values = [c.text.strip() if c.text and c.text.strip() else c.tag for c in g]
                values = [v for v in values if _SAFE.match(v)]
                if values:
                    parts.append(f"{group}={','.join(values)}")
    rep = t.find("Repetition")
    if rep is not None:
        for key in ("Interval", "Duration"):
            v = _text(rep, key)
            if v and _SAFE.match(v):
                parts.append(f"Repetition{key}={v}")
    return " ".join(parts)


def read(em, root: ET.Element) -> None:
    reg = root.find("RegistrationInfo")
    uri = _text(reg, "URI")
    if uri and NAME_SAFE.match(uri):
        name, at = uri, em.at(f"<URI>{uri}</URI>") if f"<URI>{uri}</URI>" in em.text else em.at("<URI>")
    else:
        stem = posixpath.splitext(posixpath.basename(em.path))[0]
        name, at = "\\" + (stem if NAME_SAFE.match(stem) else "task"), em.at("<Task")
    name = em.unique(FRAMEWORK, name)
    me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
    attrs: dict = {"kind": "task", "description": short(_text(reg, "Description"))}
    em.construct(at, "sched:win.task", True)
    parts = []
    triggers = root.find("Triggers")
    for t in list(triggers) if triggers is not None else []:
        where = em.at(f"<{t.tag}")
        em.construct(where, _TRIGGERS.get(t.tag, "sched:win.other-trigger"), True)
        parts.append(_trigger(t))
    attrs["schedule"] = "; ".join(parts) or None
    principals = root.find("Principals")
    for _ in list(principals) if principals is not None else []:
        em.construct(em.at("<Principal"), "sched:win.principal", False)
    actions = root.find("Actions")
    for action in list(actions) if actions is not None else []:
        where = em.at(f"<{action.tag}")
        if action.tag == "Exec":
            command = _text(action, "Command") or ""
            arguments = _text(action, "Arguments") or ""
            quoted = f'"{command}"' if " " in command else command
            em.command(where, me, f"task {name}", f"{quoted} {arguments}".strip(), attrs, "sched:win.exec", base_dir=_text(action, "WorkingDirectory"))
        elif action.tag == "ComHandler":
            em.construct(where, "sched:win.com-handler", False)
        else:
            em.construct(where, "sched:win.message-action", False)
    em.job(at, "sched:WindowsTask", FRAMEWORK, name, attrs)
