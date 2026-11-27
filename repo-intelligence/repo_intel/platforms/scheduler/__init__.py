"""The batch scheduler platform pack: job definitions exported from the scheduler.

`scheduler.exports` reads what an owner commits under `.rkp/inputs/scheduler/`:
Control-M XML (`controlm`), AutoSys JIL (`autosys`), IBM Workload Scheduler composer
text (`tws`), CA-7 job listings (`ca7`), crontab files (`cron`) and Windows Task
Scheduler XML (`wintask`). Every definition is a `Job` with its schedule and calendars
in attributes; the dependencies the scheduler states (in-conditions, `condition:`,
`FOLLOWS`, requirements and triggers) are `DEPENDS_ON` between Jobs; what a job starts
(a JCL member, a script, a program) is a `RUNS` to a reference that is never declared
here, so it links only when the repository defines the target (`common`). A CICS
transaction an AutoSys job starts is an `Endpoint` with method `TRANSACTION`.

Scheduler definitions are declarative and state neither procedural logic nor data
movement, so the pack emits no behaviour units and no transform shapes. Evidence is E1,
except the links that rest on a convention or a path suffix (E4).

Modules: `vocabulary` (terms and constructs), `common` (decoding, emission, commands),
one reader per format, and `source` (the extractor). None imports the
`repo_intel.extractors` package at module level: pack discovery runs while it loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.scheduler.source import SchedulerExports
from repo_intel.platforms.scheduler.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="scheduler",
    name="Batch schedulers (Control-M, AutoSys, IBM Workload Scheduler, CA-7, cron, Windows Task Scheduler)",
    prefix="sched",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(SchedulerExports(),),
    inputs=(".rkp/inputs/scheduler/**",),
)
