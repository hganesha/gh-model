"""The scheduler pack's vocabulary: platform terms and the constructs it looks for.

Every scheduler definition becomes a `Job` keyed `{framework, name}`, the key shape the
JCL, Airflow and Databricks extractors already use. `framework` names the product:

    control-m       Control-M XML (DEFTABLE); a folder is `<folder>`, a job `<folder>/<job>`
    autosys         AutoSys JIL; jobs and boxes by name
    tws             IBM Workload Scheduler composer text; `<workstation>#<name>`
    ca7             CA-7 job listings; jobs by name
    cron            crontab files; `<crontab path>#<program>`
    windows-task    Windows Task Scheduler XML; the task's URI

Constructs are counted on every occurrence. Those marked `unmapped` are read only far
enough to count them: resources, notifications, post-processing actions and the like.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("sched:ControlMFolder", K.JOB, "lossy", "a Control-M folder, smart folder, table or sub-folder; the jobs it holds are CONTAINED"),
    Term("sched:ControlMJob", K.JOB, "lossy", "a Control-M job definition; schedule, calendars and conditions in attributes"),
    Term("sched:AutosysBox", K.JOB, "lossy", "an AutoSys box job; the jobs whose box_name names it are CONTAINED"),
    Term("sched:AutosysJob", K.JOB, "lossy", "an AutoSys job (command, file watcher, z/OS, CICS or another job type)"),
    Term("sched:TwsJobStream", K.JOB, "lossy", "an IBM Workload Scheduler job stream (SCHEDULE); its jobs are CONTAINED"),
    Term("sched:TwsJob", K.JOB, "lossy", "an IBM Workload Scheduler job definition"),
    Term("sched:Ca7Job", K.JOB, "lossy", "a CA-7 job from a job listing (LJOB)"),
    Term("sched:CronEntry", K.JOB, "lossy", "one crontab entry: a schedule and the command it starts"),
    Term("sched:WindowsTask", K.JOB, "lossy", "a Windows Task Scheduler task; its triggers as the schedule"),
    Term("sched:EntryPoint", K.ENDPOINT, "lossy", "an entry point a scheduler starts by name (a CICS transaction): method TRANSACTION, the id as route"),
)

_CONSTRUCTS = (
    # Control-M
    ("ctm.folder", "folder, smart folder, table or sub-folder", "entity:Job"),
    ("ctm.job", "job definition", "entity:Job"),
    ("ctm.incond", "in-condition: the job waits for a condition another job adds", "relation:DEPENDS_ON"),
    ("ctm.outcond", "out-condition that adds a condition", "attribute:out_conditions"),
    ("ctm.outcond-delete", "out-condition that deletes a condition", "unmapped"),
    ("ctm.schedule", "scheduling criteria: days, week days, months, dates, time window", "attribute:schedule"),
    ("ctm.cyclic", "cyclic run: interval or time sequence", "attribute:schedule"),
    ("ctm.calendar", "named calendar: DAYSCAL, WEEKSCAL or CONFCAL", "attribute:calendar"),
    ("ctm.rule-based-calendar", "rule-based calendar a job or folder uses", "attribute:calendar"),
    ("ctm.rule-based-calendar-definition", "rule-based calendar defined in a folder", "unmapped"),
    ("ctm.memname", "member or script the job runs (MEMNAME, MEMLIB)", "relation:RUNS"),
    ("ctm.cmdline", "command line the job runs", "relation:RUNS"),
    ("ctm.dummy", "dummy job: runs nothing", "attribute:kind"),
    ("ctm.application-type", "application plug-in job (SAP, file transfer, database, ETL)", "unmapped"),
    ("ctm.quantitative", "quantitative resource", "unmapped"),
    ("ctm.control", "control resource", "unmapped"),
    ("ctm.on-do", "post-processing: ON statement or code with DO actions", "unmapped"),
    ("ctm.doforcejob", "DO FORCEJOB: order another job on a result", "unmapped"),
    ("ctm.shout", "notification", "unmapped"),
    ("ctm.variable", "AutoEdit variable; values are never read", "unmapped"),
    ("ctm.other", "another element of a job or folder", "unmapped"),
    # AutoSys
    ("jil.insert-job", "insert_job: a job definition", "entity:Job"),
    ("jil.box", "a box job", "entity:Job"),
    ("jil.box-name", "box_name: the box holding the job", "relation:CONTAINS"),
    ("jil.condition", "a job named in condition: s(), f(), d(), t(), n(), e()", "relation:DEPENDS_ON"),
    ("jil.global-variable", "a global variable in condition: v()", "unmapped"),
    ("jil.external-condition", "a job of another instance in condition: JOB^INS", "unmapped"),
    ("jil.command", "command: what a CMD job runs", "relation:RUNS"),
    ("jil.start-times", "date_conditions, days_of_week, start_times, start_mins, run_window", "attribute:schedule"),
    ("jil.calendar", "run_calendar or exclude_calendar", "attribute:calendar"),
    ("jil.file-watcher", "a file watcher job: the file it waits for", "attribute:watch_file"),
    ("jil.zos", "a z/OS job: the JCL member it submits", "relation:RUNS"),
    ("jil.cics", "a CICS job: the transaction it starts", "entity:Endpoint"),
    ("jil.other-job-type", "another job type (SAP, PeopleSoft, database, file transfer, web service)", "unmapped"),
    ("jil.machine", "machine the job runs on; never copied", "unmapped"),
    ("jil.owner", "owner and permission; never copied", "unmapped"),
    ("jil.profile", "profile or envvars; values are never read", "unmapped"),
    ("jil.notification", "alarm or notification", "unmapped"),
    ("jil.resources", "virtual resources", "unmapped"),
    ("jil.run-limits", "run time alarms, termination and retries", "unmapped"),
    ("jil.other-statement", "update_job, delete_job, override_job, machine and instance statements", "unmapped"),
    # IBM Workload Scheduler (TWS) composer
    ("tws.jobstream", "SCHEDULE: a job stream", "entity:Job"),
    ("tws.job", "a job definition in $JOBS or in a job stream", "entity:Job"),
    ("tws.follows", "FOLLOWS: a dependency on a job or job stream", "relation:DEPENDS_ON"),
    ("tws.scriptname", "SCRIPTNAME: the script a job runs", "relation:RUNS"),
    ("tws.docommand", "DOCOMMAND: the command a job runs", "relation:RUNS"),
    ("tws.runcycle", "ON, AT, EVERY, UNTIL, DEADLINE: when a stream or job runs", "attribute:schedule"),
    ("tws.calendar", "a calendar named in ON, EXCEPT or FREEDAYS", "attribute:calendar"),
    ("tws.needs", "NEEDS: a resource", "unmapped"),
    ("tws.opens", "OPENS: a file dependency", "unmapped"),
    ("tws.prompt", "PROMPT: a prompt dependency", "unmapped"),
    ("tws.recovery", "RECOVERY: what to do when a job fails", "unmapped"),
    ("tws.task", "TASK: an advanced job type in JSDL", "unmapped"),
    ("tws.other-definition", "$CALENDAR, $RESOURCE, $PROMPT, CPUNAME and other definitions", "unmapped"),
    # CA-7
    ("ca7.job", "a job in an LJOB listing", "entity:Job"),
    ("ca7.jcl-member", "the JCL the job submits", "relation:RUNS"),
    ("ca7.requirement-job", "a job requirement: a predecessor job", "relation:DEPENDS_ON"),
    ("ca7.trigger", "a job trigger: a job that starts another on completion", "relation:DEPENDS_ON"),
    ("ca7.requirement-dataset", "a data set requirement", "attribute:requires_datasets"),
    ("ca7.schedule", "a schedule id with its days and times", "attribute:schedule"),
    ("ca7.calendar", "the calendar a schedule uses (SCAL)", "attribute:calendar"),
    ("ca7.user-requirement", "a user requirement: a manual check", "unmapped"),
    ("ca7.network", "an input or output network", "unmapped"),
    # cron
    ("cron.entry", "a crontab entry", "entity:Job"),
    ("cron.command", "the command an entry runs", "relation:RUNS"),
    ("cron.variable", "an environment assignment; values are never read", "unmapped"),
    # Windows Task Scheduler
    ("win.task", "a task", "entity:Job"),
    ("win.exec", "an Exec action: the program or script it runs", "relation:RUNS"),
    ("win.calendar-trigger", "a calendar trigger: daily, weekly or monthly", "attribute:schedule"),
    ("win.time-trigger", "a one-time trigger", "attribute:schedule"),
    ("win.event-trigger", "an event log trigger", "attribute:schedule"),
    ("win.boot-trigger", "a boot or logon trigger", "attribute:schedule"),
    ("win.other-trigger", "an idle, registration or session trigger", "attribute:schedule"),
    ("win.com-handler", "a COM handler action", "unmapped"),
    ("win.message-action", "a send e-mail or show message action", "unmapped"),
    ("win.principal", "the principal a task runs as; never copied", "unmapped"),
)

CONSTRUCTS = tuple(Construct(f"sched:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
