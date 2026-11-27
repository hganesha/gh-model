"""crontab files: one Job per entry.

An entry is five time fields (or an `@daily`-style special) and a command. A system
crontab (a file under a `cron.d` or `etc` directory) has a user field after the time
fields, which is skipped and never copied. A `%` that is not escaped ends the command
(the rest is the command's input). Environment assignments (`MAILTO=...`) are counted
and their values never read.

Each entry is a Job (`sched:CronEntry`) named `<crontab path>#<program>`, the program
being the first script or program its command starts (`entry` when there is none), with
`~2`, `~3`, ... for later entries of the same name; `attributes.schedule` is the time
fields as written. What the command starts is read as `common` describes.
"""

from __future__ import annotations

import posixpath
import re

from repo_intel.platforms.scheduler.common import starts

FRAMEWORK = "cron"
_FIELD = r"[\d*,/A-Za-z?LW#-]+"
_ENTRY = re.compile(rf"^\s*({_FIELD}\s+{_FIELD}\s+{_FIELD}\s+{_FIELD}\s+{_FIELD})\s+(\S.*)$")
_SPECIAL = re.compile(r"^\s*(@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly))\s+(\S.*)$", re.I)
_ASSIGNMENT = re.compile(r"^\s*[A-Za-z_]\w*\s*=")


def system_format(path: str) -> bool:
    parts = path.lower().split("/")[:-1]
    return "cron.d" in parts or "etc" in parts


def _command_end(command: str) -> int:
    i = 0
    while i < len(command):
        if command[i] == "\\":
            i += 2
            continue
        if command[i] == "%":
            return i
        i += 1
    return len(command)


def read(em, text: str) -> None:
    system = system_format(em.path)
    pos = 0
    found = 0
    for line in text.split("\n"):
        start, pos = pos, pos + len(line) + 1
        body = line.rstrip("\r")
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        m = _SPECIAL.match(body) or _ENTRY.match(body)
        if not m:
            if _ASSIGNMENT.match(body):
                em.construct(em.span(start, start + len(body)), "sched:cron.variable", False)
            continue
        schedule = " ".join(m.group(1).split())
        command = m.group(2)
        offset = start + m.start(2)
        if system:
            user = re.match(r"\S+\s+", command)
            if not user:
                continue
            offset += user.end()
            command = command[user.end():]
        command = command[:_command_end(command)]
        at = em.span(start, start + len(body))
        scripts, programs, _ = starts(command)
        first = scripts[0] if scripts else programs[0] if programs else None
        label = posixpath.basename(first.replace("\\", "/")) if first else "entry"
        if not re.fullmatch(r"[\w.@+-]{1,80}", label):
            label = "entry"
        name = em.unique(FRAMEWORK, f"{em.path}#{label}")
        me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
        attrs = {"kind": "entry", "schedule": schedule}
        em.command(em.span(offset, offset + len(command)), me, f"crontab entry {name}", command, attrs, "sched:cron.command")
        em.construct(at, "sched:cron.entry", True)
        em.job(at, "sched:CronEntry", FRAMEWORK, name, attrs)
        found += 1
    if not found:
        em.info("no crontab entries")
