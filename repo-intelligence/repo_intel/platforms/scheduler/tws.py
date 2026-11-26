"""IBM Workload Scheduler (TWS) composer text: `$JOBS` definitions and `SCHEDULE` job streams.

Composer text is free-form and keyword driven, so it is read as a stream of words
(quoted strings are one word); a line whose first character is `#` or `*` is a comment.

- Each job in `$JOBS` is a Job (`sched:TwsJob`, `<workstation>#<job>` as written). Its
  `SCRIPTNAME` or `DOCOMMAND` is what it runs, read as `common` describes; its
  `STREAMLOGON` user is never copied.
- Each `SCHEDULE` is a Job (`sched:TwsJobStream`). Its `ON`, `EXCEPT`, `AT`,
  `SCHEDTIME`, `EVERY`, `UNTIL` and `DEADLINE` clauses, as written, are the schedule; a
  name in `ON` that is not a day, a date, a frequency or a keyword is a calendar, one in
  `EXCEPT` the excluded calendar, and `FREEDAYS` names the free-days calendar.
- The jobs after the stream's `:` are CONTAINED by it. They refer to job definitions,
  declared only when the stream defines the job in line (with `SCRIPTNAME` or
  `DOCOMMAND`), so a stream links to definitions the repository's exports hold.
- `FOLLOWS` is a DEPENDS_ON: on a job of the same stream (by name or `AS` alias), on a
  job of another stream of this file, or else on the other job stream itself
  (`ws#stream.job` with qualifier `job`), since which definition a job of a stream
  defined elsewhere stands for is not known here. A job's dependency carries the
  qualifier `stream`. Internetwork dependencies (`net::`) are counted.

`NEEDS`, `OPENS`, `PROMPT`, `RECOVERY`, JSDL `TASK` definitions and the other
definitions (`$CALENDAR`, `$RESOURCE`, `$PROMPT`, `CPUNAME`, ...) are counted.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.scheduler.common import NAME_SAFE, P, short

FRAMEWORK = "tws"
_IDENT = re.compile(r"^(?:[A-Za-z0-9_.-]+#)?[A-Za-z0-9_@.-]+$")
_JOB_KEYWORDS = {
    "SCRIPTNAME", "DOCOMMAND", "STREAMLOGON", "DESCRIPTION", "TASKTYPE", "INTERACTIVE", "RCCONDSUCC", "SUCCOUTPUTCOND", "OUTPUTCOND",
    "RECOVERY", "ABENDPROMPT", "AFTER", "STOP", "CONTINUE", "RERUN", "REPEATEVERY", "FOR", "ATTEMPTS", "TASK", "STATISTICTYPE", "WORKSTATION",
}
_STREAM_KEYWORDS = {
    "DESCRIPTION", "DRAFT", "VALIDFROM", "VALIDTO", "TIMEZONE", "TZ", "ON", "EXCEPT", "AT", "SCHEDTIME", "UNTIL", "ONUNTIL", "DEADLINE", "EVERY",
    "CARRYFORWARD", "FOLLOWS", "NEEDS", "OPENS", "PROMPT", "PRIORITY", "LIMIT", "KEYSCHED", "MATCHING", "FREEDAYS", "VARTABLE", "JOBPRIORITY",
    "STARTCOND", "ONOVERLAP", "LATESTART", "ONLATE", "SA", "SU", "RESOURCE",
}
_IN_STREAM_KEYWORDS = {
    "FOLLOWS", "AT", "SCHEDTIME", "UNTIL", "ONUNTIL", "DEADLINE", "EVERY", "NEEDS", "OPENS", "PROMPT", "PRIORITY", "CONFIRMED", "KEYJOB", "NOP",
    "MAXDUR", "MINDUR", "ONLATE", "AS", "TIMEZONE", "TZ", "JOBPRIORITY",
} | _JOB_KEYWORDS
_SCHEDULE_CLAUSES = ("ON", "EXCEPT", "AT", "SCHEDTIME", "EVERY", "UNTIL", "DEADLINE")
_NOT_CALENDARS = {
    "REQUEST", "EVERYDAY", "WORKDAYS", "WORKDAY", "WEEKDAYS", "WEEKDAY", "FREEDAYS", "DAY", "DAYS", "RUNCYCLE", "VALIDFROM", "VALIDTO", "DESCRIPTION",
    "SU", "MO", "TU", "WE", "TH", "FR", "SA", "SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "FDIGNORE", "FDNEXT", "FDPREV",
    "SUBSET", "AND", "OR", "AT", "TIMEZONE", "TZ", "DAILY", "WEEKLY", "MONTHLY", "YEARLY", "ONLY",
}
_SECTION = re.compile(r"^\$[A-Z]+$")
_OTHER_STARTS = {"CPUNAME", "CPUCLASS", "DOMAIN", "WORKSTATION", "WORKSTATIONCLASS", "VARTABLE", "EVENTRULE", "RUNCYCLEGROUP"}


def _words(text: str) -> list[tuple[str, int, int, bool]]:
    """(word, start, end, quoted): comment lines blanked first; a comma is a word of its own."""
    text = re.sub(r"(?m)^[ \t]*[#*].*$", lambda m: " " * len(m.group(0)), text)
    out: list[tuple[str, int, int, bool]] = []
    for m in re.finditer(r'"[^"]*"|,|[^\s",]+', text):
        w = m.group(0)
        quoted = w.startswith('"')
        out.append((w[1:-1] if quoted else w, m.start(), m.end(), quoted))
    return out


class _Reader:
    def __init__(self, em, text: str):
        self.em = em
        self.w = _words(text)
        self.i = 0
        self.streams: list[dict[str, Any]] = []

    def peek(self, k: int = 0) -> tuple[str, int, int, bool] | None:
        return self.w[self.i + k] if self.i + k < len(self.w) else None

    def up(self, k: int = 0) -> str:
        x = self.peek(k)
        return x[0].upper() if x and not x[3] else ""

    def take(self) -> tuple[str, int, int, bool]:
        x = self.w[self.i]
        self.i += 1
        return x

    def span(self, x: tuple[str, int, int, bool]):
        return self.em.span(x[1], x[2])

    def at_section_start(self) -> bool:
        u = self.up()
        return bool(_SECTION.match(u)) or u in ("SCHEDULE", "JOBSTREAM") or u in _OTHER_STARTS

    def run(self) -> None:
        while self.peek():
            u = self.up()
            if u == "$JOBS":
                self.take()
                self.jobs_section()
            elif u in ("SCHEDULE", "JOBSTREAM"):
                self.stream()
            elif _SECTION.match(u) or u in _OTHER_STARTS:
                start = self.take()
                self.em.construct(self.span(start), "sched:tws.other-definition", False)
                while self.peek() and not self.at_section_start():
                    if self.up() == "END":
                        self.take()
                        break
                    self.take()
            else:
                self.take()
        self.emit_streams()

    # ------------------------------------------------------------------ $JOBS

    def jobs_section(self) -> None:
        while self.peek() and not self.at_section_start():
            x = self.take()
            if x[3] or x[0] == "," or x[0].upper() in _JOB_KEYWORDS or not _IDENT.match(x[0]):
                continue
            self.job_definition(x, None)

    def job_definition(self, ident: tuple[str, int, int, bool], stream: dict[str, Any] | None) -> dict[str, Any] | None:
        """Read the definition keywords after a job's name; declare the job if it runs something or is in $JOBS."""
        name = ident[0]
        if stream is not None and "#" not in name and stream["workstation"]:
            name = f"{stream['workstation']}#{name}"
        attrs: dict[str, Any] = {"kind": "job"}
        command: tuple[str, str, Any] | None = None
        while self.peek():
            u = self.up()
            if u in ("SCRIPTNAME", "DOCOMMAND"):
                kw = self.take()
                value = self.take() if self.peek() else None
                if value:
                    command = (value[0], "sched:tws.scriptname" if u == "SCRIPTNAME" else "sched:tws.docommand", self.span(value))
                    attrs["kind"] = "script" if u == "SCRIPTNAME" else "command"
                else:
                    self.em.construct(self.span(kw), "sched:tws.scriptname", False)
            elif u == "DESCRIPTION":
                self.take()
                if self.peek():
                    attrs["description"] = short(self.take()[0])
            elif u in ("STREAMLOGON", "TASKTYPE", "RCCONDSUCC", "SUCCOUTPUTCOND", "OUTPUTCOND", "STATISTICTYPE", "WORKSTATION"):
                self.take()
                if self.peek():
                    value = self.take()
                    if u == "TASKTYPE" and NAME_SAFE.match(value[0]):
                        attrs["task_type"] = value[0].upper()
            elif u == "RECOVERY":
                self.em.construct(self.span(self.take()), "sched:tws.recovery", False)
                while self.up() in ("STOP", "CONTINUE", "RERUN", "AFTER", "ABENDPROMPT", "REPEATEVERY", "FOR", "ATTEMPTS"):
                    kw = self.take()[0].upper()
                    if kw in ("AFTER", "ABENDPROMPT", "REPEATEVERY", "FOR") and self.peek():
                        self.take()
            elif u == "TASK":
                self.em.construct(self.span(self.take()), "sched:tws.task", False)
                while self.peek() and "jobDefinition>" not in self.peek()[0]:
                    self.take()
                if self.peek():
                    self.take()
            elif u == "INTERACTIVE":
                self.take()
            else:
                break
        if stream is not None and command is None:
            return None  # a job of a stream: a reference to its definition
        if not NAME_SAFE.match(name):
            self.em.construct(self.span(ident), "sched:tws.job", False)
            return None
        name = self.em.unique(FRAMEWORK, name)
        me = self.em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
        if command:
            self.em.command(command[2], me, f"job {name}", command[0], attrs, command[1])
        self.em.construct(self.span(ident), "sched:tws.job", True)
        self.em.job(self.span(ident), "sched:TwsJob", FRAMEWORK, name, attrs)
        return me

    # ------------------------------------------------------------------ SCHEDULE

    def clause(self, stop: set[str]) -> list[tuple[str, int, int, bool]]:
        """Words up to the next keyword in `stop`, `:` or `END`."""
        out = []
        while self.peek() and self.up() not in stop and self.peek()[0] not in (":",) and self.up() != "END" and not self.at_section_start():
            out.append(self.take())
        return out

    def stream(self) -> None:
        start = self.take()
        ident = self.take() if self.peek() else None
        if ident is None or ident[3] or not _IDENT.match(ident[0]) or not NAME_SAFE.match(ident[0]):
            self.em.construct(self.span(start), "sched:tws.jobstream", False)
            return
        workstation = ident[0].split("#", 1)[0] if "#" in ident[0] else None
        s: dict[str, Any] = {"ident": ident, "name": ident[0], "workstation": workstation, "clauses": [], "calendars": [], "calendar": None, "exclude": None,
                             "follows": [], "jobs": [], "aliases": {}, "description": None}
        # header, up to ":"
        while self.peek() and self.peek()[0] != ":" and self.up() != "END" and not (self.up() in ("SCHEDULE", "JOBSTREAM") or _SECTION.match(self.up())):
            kw = self.take()
            u = kw[0].upper()
            args = self.clause(_STREAM_KEYWORDS)
            if u == "DESCRIPTION" and args:
                s["description"] = short(args[0][0])
            elif u in _SCHEDULE_CLAUSES:
                self.em.construct(self.span(kw), "sched:tws.runcycle", True)
                s["clauses"].append(" ".join([u] + [f'"{a[0]}"' if a[3] else a[0] for a in args if a[0] != ","]))
                if u in ("ON", "EXCEPT"):
                    for cal in _calendars(args):
                        self.em.construct(self.span(kw), "sched:tws.calendar", True)
                        s["calendars"].append(cal)
                        if u == "ON" and s["calendar"] is None:
                            s["calendar"] = cal
                        if u == "EXCEPT" and s["exclude"] is None:
                            s["exclude"] = cal
            elif u == "FREEDAYS" and args and _IDENT.match(args[0][0]):
                self.em.construct(self.span(kw), "sched:tws.calendar", True)
                s["calendars"].append(args[0][0])
            elif u == "FOLLOWS":
                s["follows"] += self.follows_items(kw, args)
            elif u in ("NEEDS", "OPENS", "PROMPT"):
                self.em.construct(self.span(kw), f"sched:tws.{u.lower()}", False)
        if self.peek() and self.peek()[0] == ":":
            self.take()
        # jobs, up to END
        while self.peek() and self.up() != "END" and not (self.up() in ("SCHEDULE", "JOBSTREAM") or _SECTION.match(self.up())):
            x = self.take()
            if x[3] or x[0] == "," or x[0].upper() in _IN_STREAM_KEYWORDS or not _IDENT.match(x[0]):
                continue
            job_name = x[0] if "#" in x[0] or not workstation else f"{workstation}#{x[0]}"
            alias = None
            if self.up() == "AS" and self.peek(1):
                self.take()
                alias = self.take()[0]
            defined = self.job_definition(x, s)
            entry: dict[str, Any] = {"ident": x, "name": job_name, "alias": alias, "follows": [], "defined": defined is not None}
            s["jobs"].append(entry)
            s["aliases"][(alias or x[0].split("#")[-1]).upper()] = job_name
            while self.peek() and self.up() in _IN_STREAM_KEYWORDS - _JOB_KEYWORDS:
                kw = self.take()
                u = kw[0].upper()
                args = self.clause(_IN_STREAM_KEYWORDS)
                if u == "FOLLOWS":
                    entry["follows"] += self.follows_items(kw, args)
                elif u in ("NEEDS", "OPENS", "PROMPT"):
                    self.em.construct(self.span(kw), f"sched:tws.{u.lower()}", False)
                elif u in ("AT", "EVERY", "UNTIL", "DEADLINE", "SCHEDTIME"):
                    self.em.construct(self.span(kw), "sched:tws.runcycle", False)
        if self.up() == "END":
            self.take()
        self.streams.append(s)

    def follows_items(self, kw, args: list[tuple[str, int, int, bool]]) -> list[tuple[str, Any]]:
        """The references of a FOLLOWS clause: the first word of each comma-separated item."""
        out = []
        expect = True
        for a in args:
            if a[0] == ",":
                expect = True
            elif expect:
                out.append((a[0], self.span(a)))
                expect = False
        return out

    def emit_streams(self) -> None:
        em = self.em
        by_name = {s["name"].upper(): s for s in self.streams}
        for s in self.streams:
            name = em.unique(FRAMEWORK, s["name"])
            me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
            at = self.span(s["ident"])
            em.construct(at, "sched:tws.jobstream", True)
            em.job(at, "sched:TwsJobStream", FRAMEWORK, name, {
                "kind": "job_stream", "description": s["description"], "schedule": "; ".join(c for c in s["clauses"] if re.fullmatch(r"[\w\s,.:;=+/\"#@-]{1,300}", c)) or None,
                "calendar": s["calendar"], "exclude_calendar": s["exclude"], "calendars": sorted(set(s["calendars"])), "jobs": sorted({j["name"] for j in s["jobs"]}),
            })
            for j in s["jobs"]:
                if NAME_SAFE.match(j["name"]):
                    em.relation(self.span(j["ident"]), me, P.CONTAINS, em.base.ref("Job", {"framework": FRAMEWORK, "name": j["name"]}, j["name"], declare=j["defined"]))
            for ref_text, where in s["follows"]:
                self.follow(where, me, None, s, ref_text, by_name, stream_level=True)
            for j in s["jobs"]:
                if not NAME_SAFE.match(j["name"]):
                    continue
                subject = em.base.ref("Job", {"framework": FRAMEWORK, "name": j["name"]}, j["name"], declare=False)
                for ref_text, where in j["follows"]:
                    self.follow(where, subject, s["name"], s, ref_text, by_name, stream_level=False)

    def follow(self, where, subject, stream_name: str | None, s: dict[str, Any], text: str, by_name: dict[str, dict[str, Any]], stream_level: bool) -> None:
        em = self.em
        if "::" in text:
            em.construct(where, "sched:tws.follows", False)
            em.info(f"{s['name']} follows {text.split('::')[-1]} of another network: not linked")
            return
        target, job = None, None
        if "." in text:
            stream_part, job_part = text.rsplit(".", 1)
            other = by_name.get((stream_part if "#" in stream_part or not s["workstation"] else f"{s['workstation']}#{stream_part}").upper())
            if job_part != "@" and other is not None and job_part.upper() in other["aliases"]:
                target = other["aliases"][job_part.upper()]
            else:
                target = stream_part if "#" in stream_part or not s["workstation"] else f"{s['workstation']}#{stream_part}"
                job = None if job_part == "@" else job_part
        elif not stream_level and text.split("#")[-1].upper() in s["aliases"] and ("#" not in text or text.upper() in {j["name"].upper() for j in s["jobs"]}):
            target = s["aliases"][text.split("#")[-1].upper()]
        else:
            target = text if "#" in text or not s["workstation"] else f"{s['workstation']}#{text}"
        if not NAME_SAFE.match(target) or not _IDENT.match(target):
            em.construct(where, "sched:tws.follows", False)
            return
        em.construct(where, "sched:tws.follows", True)
        em.depends(where, subject, FRAMEWORK, target, {"stream": stream_name, "job": job})


def _calendars(args: list[tuple[str, int, int, bool]]) -> list[str]:
    """Calendar names in the words of an ON or EXCEPT clause."""
    out = []
    skip_next = False
    for k, a in enumerate(args):
        word = a[0]
        if skip_next:
            skip_next = False
            continue
        u = word.upper()
        if u in ("RUNCYCLE", "VALIDFROM", "VALIDTO", "DESCRIPTION", "TIMEZONE", "TZ"):
            skip_next = True
            continue
        if a[3] or word == "," or u in _NOT_CALENDARS or re.search(r"[\d/+=;()]", word) or not re.fullmatch(r"[A-Za-z_][\w-]{0,15}", word):
            continue
        out.append(word)
    return out


def read(em, text: str) -> None:
    _Reader(em, text).run()
