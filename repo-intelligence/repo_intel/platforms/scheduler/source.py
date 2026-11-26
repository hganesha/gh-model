"""`scheduler.exports`: scheduler definitions an owner commits under `.rkp/inputs/scheduler/`.

The format is told from the file, not from where it sits below that directory:

    XML whose root is DEFTABLE                   Control-M         (controlm)
    XML whose root is Task                       Task Scheduler    (wintask)
    a .jil file, or insert_job: statements       AutoSys JIL       (autosys)
    a NAME ID MEMBER heading or an LJOB command  CA-7 listing      (ca7)
    $JOBS or SCHEDULE ... END                    TWS composer      (tws)
    a crontab, .cron or cron.d file              crontab           (cron)

Anything else is inventoried and reported with a diagnostic. XML that declares a
DOCTYPE or an entity is refused, never parsed.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET

from repo_intel.platforms.scheduler import autosys, ca7, controlm, cron, tws, wintask
from repo_intel.platforms.scheduler.common import Emitter, decode

INPUT_DIR = ".rkp/inputs/scheduler/"
_DOCTYPE = re.compile(r"<!DOCTYPE|<!ENTITY", re.I)
_JIL = re.compile(r"(?m)^\s*insert_job\s*:")
_CA7 = re.compile(r"(?m)^\s*NAME\s+ID\s+MEMBER\b|^\s*LJOB\b", re.I)
_TWS = re.compile(r"(?m)^\s*\$JOBS\b|^\s*SCHEDULE\s+\S+", re.I)
_TWS_END = re.compile(r"(?m)^\s*END\s*$", re.I)


def _strip_namespaces(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.rsplit("}", 1)[1]
    return root


def is_cron(path: str) -> bool:
    low = path.lower()
    base = posixpath.basename(low)
    parent = posixpath.basename(posixpath.dirname(low))
    return base == "crontab" or base.endswith((".cron", ".crontab")) or parent in ("cron", "cron.d", "crontabs", "crontab")


def detect(path: str, text: str) -> str | None:
    head = text.lstrip()
    if head.startswith("<"):
        m = re.search(r"<(?!\?|!)([A-Za-z_][\w.:-]*)", head)
        root = m.group(1).split(":")[-1] if m else ""
        return {"DEFTABLE": "controlm", "Task": "wintask"}.get(root)
    if path.lower().endswith(".jil") or _JIL.search(text):
        return "autosys"
    if _CA7.search(text):
        return "ca7"
    if _TWS.search(text) and (_TWS_END.search(text) or re.search(r"(?m)^\s*\$JOBS\b", text, re.I)):
        return "tws"
    if is_cron(path):
        return "cron"
    return None


class SchedulerExports:
    id = "scheduler.exports"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().startswith(INPUT_DIR) and not posixpath.basename(path).startswith(".")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        text, codec, bom = decode(data)
        kind = detect(path, text)
        if kind is None:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "info", "not a scheduler export this pack reads (Control-M XML, AutoSys JIL, TWS composer, CA-7 listing, crontab, Task Scheduler XML)")], parsed=False)
        em = Emitter(self.id, path, data, blob, text, codec, bom)
        if kind in ("controlm", "wintask"):
            if _DOCTYPE.search(text):
                return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "refused: the file declares a DOCTYPE or an entity")], parsed=False)
            try:
                root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1))
            except ET.ParseError as exc:
                return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not well-formed XML: {exc}")], parsed=False)
            (controlm if kind == "controlm" else wintask).read(em, _strip_namespaces(root))
        else:
            {"autosys": autosys, "ca7": ca7, "tws": tws, "cron": cron}[kind].read(em, text)
        return em.result()
