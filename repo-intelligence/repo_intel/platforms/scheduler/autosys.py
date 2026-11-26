"""AutoSys JIL: `insert_job` statements and their attributes.

- Each `insert_job` is a Job: `sched:AutosysBox` for `job_type: BOX`, otherwise
  `sched:AutosysJob`, named as written. `box_name` makes the box CONTAIN the job (the
  box may be defined in another file). `update_job`, `delete_job`, `override_job` and
  machine or instance statements change definitions made elsewhere: they are counted,
  not applied.
- `condition:` is parsed (`s()`, `f()`, `d()`, `t()`, `n()`, `e()` and their long
  names, `&`, `|`, `and`, `or`, parentheses, look-back times): each job it names is a
  DEPENDS_ON with qualifier `on` (`success`, `failure`, `done`, ...), and `optional`
  when the job sits under an `or`. Global variables (`v()`) are counted, their values
  never read; a job of another instance (`JOB^INS`) is counted and kept by name in
  `external_conditions`.
- With `date_conditions` set, `days_of_week`, `start_times`, `start_mins`, `run_window`
  and `timezone` are the schedule; `run_calendar` and `exclude_calendar` are the
  calendars. Without it AutoSys does not start the job by time, and no schedule is kept.
- What a job runs: `command:` (CMD jobs) as `common` describes; `jcl_member` (z/OS
  jobs) is the JCL job of that name; `watch_file` (file watchers) is kept as a path; a
  CICS job's `cics_tran_id` is an Endpoint (`sched:EntryPoint`, method TRANSACTION, the
  transaction id as route) named in the job's `transaction` attribute.

`machine`, `owner`, `permission`, `profile`, `envvars` and notification addresses are
never copied, only counted.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.scheduler.common import MEMBER, NAME_SAFE, P, display_path, is_dynamic, short

FRAMEWORK = "autosys"
_STATEMENT = re.compile(r"^(insert|update|delete|override)_[a-z_]+$")
_TYPES = {"c": "CMD", "cmd": "CMD", "b": "BOX", "box": "BOX", "f": "FW", "fw": "FW"}
_ZOS = {"ZOS", "ZOSM", "ZOSD", "ZOSDST"}
_SCHEDULE = ("days_of_week", "start_times", "start_mins", "run_window", "timezone", "must_start_times", "must_complete_times")
_COUNTED = {
    "machine": "sched:jil.machine", "owner": "sched:jil.owner", "permission": "sched:jil.owner", "profile": "sched:jil.profile", "envvars": "sched:jil.profile",
    "alarm_if_fail": "sched:jil.notification", "alarm_if_terminated": "sched:jil.notification", "send_notification": "sched:jil.notification",
    "notification_msg": "sched:jil.notification", "notification_emailaddress": "sched:jil.notification", "notification_emailaddress_on_failure": "sched:jil.notification",
    "notification_emailaddress_on_success": "sched:jil.notification", "notification_emailaddress_on_alarm": "sched:jil.notification",
    "notification_emailaddress_on_terminated": "sched:jil.notification", "resources": "sched:jil.resources",
    "max_run_alarm": "sched:jil.run-limits", "min_run_alarm": "sched:jil.run-limits", "term_run_time": "sched:jil.run-limits", "n_retrys": "sched:jil.run-limits",
}
_STATUS = {"s": "success", "success": "success", "f": "failure", "failure": "failure", "d": "done", "done": "done", "t": "terminated", "terminated": "terminated",
           "n": "notrunning", "notrunning": "notrunning", "e": "exitcode", "exitcode": "exitcode", "v": "variable", "value": "variable"}
_TRAN = re.compile(r"^[A-Za-z0-9#@$]{1,4}$")
_TOKEN = re.compile(
    r"\s+|(?P<open>\()|(?P<close>\))|(?P<and>&|\band\b)|(?P<or>\||\bor\b)"
    r"|(?P<fn>[A-Za-z]+)\s*\(\s*(?P<arg>[^(),\s]+)\s*(?:,\s*(?P<look>[^()]*?)\s*)?\)(?:\s*(?:!=|<>|>=|<=|=|>|<)\s*(?:\"[^\"]*\"|[^\s&|()]+))?",
    re.I,
)


def _strip_comments(text: str) -> str:
    """Comments blanked to spaces, so offsets are kept."""
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"(?m)^[ \t]*#.*$", lambda m: " " * len(m.group(0)), text)


def _unquote(value: str) -> str:
    v = value.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def statements(text: str) -> list[dict[str, Any]]:
    """Each statement: {verb, name, at: (start, end), attrs: {key: (value, start, end)}}."""
    out: list[dict[str, Any]] = []
    pos = 0
    for line in _strip_comments(text).split("\n"):
        start, pos = pos, pos + len(line) + 1
        m = re.match(r"\s*([a-z_][a-z0-9_]*)\s*:\s*(.*?)\s*$", line, re.I)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2)
        vstart = start + m.start(2)
        if _STATEMENT.match(key):
            name, _, rest = value.partition(" ")
            out.append({"verb": key, "name": name.strip(), "at": (start + m.start(2), start + m.start(2) + len(name.strip())), "attrs": {}})
            t = re.search(r"\bjob_type\s*:\s*(\S+)", rest, re.I)
            if t:
                offset = vstart + len(name) + 1 + t.start(1)
                out[-1]["attrs"]["job_type"] = (t.group(1), offset, offset + len(t.group(1)))
        elif out:
            out[-1]["attrs"].setdefault(key, (value, vstart, vstart + len(value)))
    return out


class _Bad(Exception):
    pass


def _tokens(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise _Bad
        pos = m.end()
        kind = next((k for k in ("open", "close", "and", "or", "fn") if m.group(k)), None)
        if kind == "fn":
            fn = m.group("fn").lower()
            if fn not in _STATUS:
                raise _Bad
            tokens.append(("atom", (_STATUS[fn], m.group("arg"), (m.group("look") or "").strip() or None, m.start("arg"), m.end("arg"))))
        elif kind:
            tokens.append((kind, None))
    return tokens


def _parse_condition(text: str) -> list[tuple[str, str, str | None, int, int, bool]] | None:
    """Each job or variable a condition names: (status, name, look-back, start, end, required),
    where `required` is false under an `or`. None if the condition does not parse."""
    try:
        tokens = _tokens(text)
    except _Bad:
        return None
    i = 0

    def peek() -> str | None:
        return tokens[i][0] if i < len(tokens) else None

    def expr() -> Any:
        nonlocal i
        nodes = [term()]
        while peek() == "or":
            i += 1
            nodes.append(term())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    def term() -> Any:
        nonlocal i
        nodes = [factor()]
        while peek() == "and":
            i += 1
            nodes.append(factor())
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def factor() -> Any:
        nonlocal i
        kind = peek()
        if kind == "open":
            i += 1
            node = expr()
            if peek() != "close":
                raise _Bad
            i += 1
            return node
        if kind == "atom":
            i += 1
            return ("atom", tokens[i - 1][1])
        raise _Bad

    try:
        tree = expr()
        if i != len(tokens):
            raise _Bad
    except _Bad:
        return None
    atoms: list[tuple[str, str, str | None, int, int, bool]] = []

    def walk(node: Any, required: bool) -> None:
        if node[0] == "atom":
            atoms.append((*node[1], required))
        else:
            for child in node[1]:
                walk(child, required and node[0] == "and")

    walk(tree, True)
    return atoms


def read(em, text: str) -> None:
    for st in statements(text):
        at = em.span(*st["at"])
        if st["verb"] != "insert_job":
            em.construct(at, "sched:jil.other-statement", False)
            continue
        _job(em, st, at)


def _job(em, st: dict[str, Any], at) -> None:
    name = st["name"]
    attrs_in: dict[str, tuple[str, int, int]] = st["attrs"]
    if not name or not NAME_SAFE.match(name) or is_dynamic(name):
        em.construct(at, "sched:jil.insert-job", False)
        em.info("an insert_job with no readable name was skipped")
        return
    name = em.unique(FRAMEWORK, name)
    raw_type = _unquote(attrs_in.get("job_type", ("CMD", 0, 0))[0])
    job_type = _TYPES.get(raw_type.lower(), raw_type.upper())
    em.construct(at, "sched:jil.insert-job", True)
    me = em.base.ref("Job", {"framework": FRAMEWORK, "name": name}, name)
    attrs: dict[str, Any] = {"kind": job_type.lower(), "description": short(_unquote(attrs_in.get("description", ("", 0, 0))[0]))}

    def span(key: str):
        _, a, b = attrs_in[key]
        return em.span(a, b)

    if job_type == "BOX":
        em.construct(at, "sched:jil.box", True)
    if "box_name" in attrs_in:
        box = _unquote(attrs_in["box_name"][0])
        ok = bool(box) and bool(NAME_SAFE.match(box)) and not is_dynamic(box)
        em.construct(span("box_name"), "sched:jil.box-name", ok)
        if ok:
            attrs["box"] = box
            em.relation(span("box_name"), em.base.ref("Job", {"framework": FRAMEWORK, "name": box}, box, declare=False), P.CONTAINS, me)
    if "condition" in attrs_in:
        _condition(em, me, name, attrs_in["condition"], attrs)
    # schedule and calendars
    dated = _unquote(attrs_in.get("date_conditions", ("0", 0, 0))[0]).lower() in ("1", "y", "yes", "true")
    parts = []
    for key in _SCHEDULE:
        if key in attrs_in:
            value = " ".join(_unquote(attrs_in[key][0]).split())
            em.construct(span(key), "sched:jil.start-times", dated)
            if dated and value and re.fullmatch(r"[\w,.:+/ \"-]{1,160}", value):
                parts.append(f"{key}={value}")
    if dated and parts:
        attrs["schedule"] = " ".join(parts)
    calendars = []
    for key in ("run_calendar", "exclude_calendar"):
        if key in attrs_in:
            cal = _unquote(attrs_in[key][0])
            ok = bool(cal) and bool(NAME_SAFE.match(cal))
            em.construct(span(key), "sched:jil.calendar", ok)
            if ok:
                calendars.append(cal)
                attrs["calendar" if key == "run_calendar" else "exclude_calendar"] = cal
    attrs["calendars"] = sorted(set(calendars))
    # what it runs
    if job_type == "CMD" and "command" in attrs_in:
        em.command(span("command"), me, f"job {name}", attrs_in["command"][0], attrs, "sched:jil.command")
    elif job_type == "FW":
        watched = _unquote(attrs_in.get("watch_file", ("", 0, 0))[0])
        shown = display_path(watched) if watched and not is_dynamic(watched) else None
        em.construct(span("watch_file") if "watch_file" in attrs_in else at, "sched:jil.file-watcher", bool(shown))
        attrs["watch_file"] = shown
    elif job_type in _ZOS:
        member = _unquote(attrs_in.get("jcl_member", ("", 0, 0))[0]).upper()
        where = span("jcl_member") if "jcl_member" in attrs_in else at
        if member and MEMBER.match(member):
            library = _unquote(attrs_in.get("jcl_library", ("", 0, 0))[0]).upper()
            attrs["jcl_member"], attrs["jcl_library"] = member, library if library and NAME_SAFE.match(library) and not is_dynamic(library) else None
            em.jcl(where, me, member, certain=False)
            em.construct(where, "sched:jil.zos", True)
        else:
            em.construct(where, "sched:jil.zos", False)
            if member:
                em.info(f"job {name}: its JCL member is not literal: not linked")
    elif job_type == "CICS":
        key = "cics_tran_id" if "cics_tran_id" in attrs_in else "tran_id" if "tran_id" in attrs_in else None
        tran = _unquote(attrs_in[key][0]).upper() if key else ""
        where = span(key) if key else at
        if _TRAN.match(tran):
            region = _unquote(attrs_in.get("cics_region_name", ("", 0, 0))[0])
            em.entity(where, "sched:EntryPoint", {"component_root": em.base.UNIT, "protocol": "cics", "method": "TRANSACTION", "route": tran}, f"TRANSACTION {tran}",
                      {"scheduler": FRAMEWORK, "job": name, "region": region if region and NAME_SAFE.match(region) and not is_dynamic(region) else None})
            attrs["transaction"] = tran
            em.construct(where, "sched:jil.cics", True)
        else:
            em.construct(where, "sched:jil.cics", False)
    elif job_type not in ("BOX", "CMD"):
        em.construct(at, "sched:jil.other-job-type", False)
    for key in sorted(attrs_in, key=lambda k: attrs_in[k][1]):
        if key in _COUNTED:
            em.construct(span(key), _COUNTED[key], False)
    em.job(at, "sched:AutosysBox" if job_type == "BOX" else "sched:AutosysJob", FRAMEWORK, name, attrs)


def _condition(em, me: dict[str, Any], name: str, value: tuple[str, int, int], attrs: dict[str, Any]) -> None:
    text, start, end = value
    parsed = _parse_condition(text)
    if parsed is None:
        em.construct(em.span(start, end), "sched:jil.condition", False)
        em.info(f"job {name}: its condition does not parse: not linked")
        return
    external: list[str] = []
    for status, other, look, a, b, required in parsed:
        where = em.span(start + a, start + b)
        if status == "variable":
            em.construct(where, "sched:jil.global-variable", False)
            continue
        if "^" in other:
            em.construct(where, "sched:jil.external-condition", False)
            if NAME_SAFE.match(other):
                external.append(other)
            continue
        ok = bool(NAME_SAFE.match(other)) and not is_dynamic(other)
        em.construct(where, "sched:jil.condition", ok)
        if ok:
            em.depends(where, me, FRAMEWORK, other, {"on": status, "optional": True if not required else None, "lookback": look if look and re.fullmatch(r"[\d.:]{1,8}", look) else None})
    attrs["external_conditions"] = sorted(set(external))
