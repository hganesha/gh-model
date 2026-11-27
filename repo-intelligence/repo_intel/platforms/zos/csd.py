"""CICS system definitions: the `DFHCSDUP` commands of a CSD export.

An owner exports the CSD with `DFHCSDUP EXTRACT` or keeps the `DEFINE` commands that
build it, and commits them under `.rkp/inputs/zos/` (or they sit in-stream in a JCL step
that runs `DFHCSDUP`). Each command is a verb, a resource type with its name, and
keywords:

    DEFINE TRANSACTION(CINQ) GROUP(CUSTGRP) PROGRAM(CUSTINQ) PROFILE(DFHCICST)
    DEFINE FILE(CUSTMAST) GROUP(CUSTGRP) DSNAME(PROD.CUST.MASTER) READ(YES) UPDATE(YES)

`definitions` returns one `Definition` per DEFINE, with the keywords it reads; `source`
turns transactions into Endpoints and files into data set aliases. Descriptions, user
data and security keys are never read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from repo_intel.platforms.zos.cards import Command, commands, value_text

# CSD resource types, by the construct each counts as.
RESOURCES = {
    "TRANSACTION": "csd_transaction", "PROGRAM": "csd_program", "FILE": "csd_file", "MAPSET": "csd_mapset",
    "DB2ENTRY": "csd_db2", "DB2TRAN": "csd_db2",
    "TDQUEUE": "csd_queue", "TSMODEL": "csd_queue", "MQCONN": "csd_queue", "MQQUEUE": "csd_queue", "MQMONITOR": "csd_queue",
    "URIMAP": "csd_web", "WEBSERVICE": "csd_web", "PIPELINE": "csd_web", "TCPIPSERVICE": "csd_web", "ATOMSERVICE": "csd_web",
    "CONNECTION": "csd_connection", "SESSIONS": "csd_connection", "IPCONN": "csd_connection", "TERMINAL": "csd_connection", "TYPETERM": "csd_connection",
    "PROFILE": "csd_other", "TRANCLASS": "csd_other", "LIBRARY": "csd_other", "DB2CONN": "csd_other", "JVMSERVER": "csd_other", "BUNDLE": "csd_other",
    "PARTITIONSET": "csd_other", "LSRPOOL": "csd_other", "JOURNALMODEL": "csd_other", "ENQMODEL": "csd_other", "PROCESSTYPE": "csd_other",
    "DOCTEMPLATE": "csd_other", "REQUESTMODEL": "csd_other", "CORBASERVER": "csd_other", "DJAR": "csd_other", "FENODE": "csd_other",
    "FEPOOL": "csd_other", "FEPROPSET": "csd_other", "FETARGET": "csd_other", "PARTNER": "csd_other", "EVENTBINDING": "csd_other",
}
# Keywords whose values are names `source` may keep (never descriptions, user data or keys).
_KEPT = ("GROUP", "PROGRAM", "DSNAME", "TRANSID", "PLAN", "ENTRY", "LANGUAGE", "PROFILE", "TRANCLASS", "REMOTESYSTEM", "REMOTENAME", "MAPSET",
         "ADD", "BROWSE", "DELETE", "READ", "UPDATE", "RECORDFORMAT", "STRINGS", "LSRPOOLNUM", "EXECKEY", "CONCURRENCY", "DATALOCATION", "TASKDATALOC")
_ABBREVIATIONS = {"TRANS": "TRANSACTION", "PROG": "PROGRAM", "DSN": "DSNAME", "LANG": "LANGUAGE", "PROF": "PROFILE"}


@dataclass
class Definition:
    resource: str  # TRANSACTION, PROGRAM, FILE, ...
    name: str  # upper case
    start: int
    end: int
    name_at: tuple[int, int]
    keywords: dict[str, tuple[str, int, int]]  # kept keyword -> (value, start, end)


def looks_like_csd(text: str) -> bool:
    return bool(re.search(r"(?im)^\s*DEFINE\s+(?:TRANSACTION|PROGRAM|FILE|MAPSET|TDQUEUE|DB2ENTRY|URIMAP|PROFILE|TRANCLASS|LIBRARY|TERMINAL|CONNECTION)\s*\(", text)) \
        and bool(re.search(r"\bGROUP\s*\(", text, re.I))


def definitions(lines: list[tuple[int, str]]) -> tuple[list[Definition], list[Command]]:
    """DEFINE commands, and the other DFHCSDUP commands (ADD, LIST, ...) to count."""
    out: list[Definition] = []
    other: list[Command] = []
    for c in commands(lines, "csd"):
        if c.verb != "DEFINE":
            other.append(c)
            continue
        found = c.items()
        typed = next((i for i in found if i.kind == "keyword"), None)
        if typed is None:
            other.append(c)
            continue
        resource = _ABBREVIATIONS.get(typed.text, typed.text)
        name = (value_text(typed) or "").upper()
        kws: dict[str, tuple[str, int, int]] = {}
        for it in found:
            if it is typed or it.kind != "keyword":
                continue
            key = _ABBREVIATIONS.get(it.text, it.text)
            v = value_text(it)
            if key in _KEPT and v is not None and key not in kws and it.value is not None:
                s, e = it.value.span(0, len(it.value.text))
                kws[key] = (v, s, e)
        name_at = typed.value.span(0, len(typed.value.text)) if typed.value is not None and typed.value.text else (typed.start, typed.end)
        out.append(Definition(resource, name, c.start, c.end, name_at, kws))
    return out, other

