"""`ims.source`: IMS database descriptions, program specification blocks, MFS formats and
the stage-1 system definition.

Which reader a file gets is decided from its content (`detect`), so a file is read only
when it is surely IMS source:

- `.dbd`: a DBD (`dbd`) when it has DBD or SEGM statements;
- `.psb`: a PSB (`psb`) when it has PCB or PSBGEN statements;
- `.mfs`: MFS (`mfs`) when it has FMT, MSG, DFLD or MFLD statements;
- `.asm` and `.mac`: a DBD when it has both DBDGEN and SEGM, a PSB when it has PSBGEN;
  anything else in them (a BMS map, other assembler) is left to the other readers;
- any file under `.rkp/inputs/ims/`: the stage-1 system definition (`sysdef`) when it has
  APPLCTN or TRANSACT statements, else a DBD, PSB or MFS source as above.

A DBD or PSB in `.asm` or `.mac` is not also read as assembler (`supersedes_when_parsed`,
contract 10.3 item 1). The sources are declarative: they state neither procedural logic
nor row-level data movement, so the pack emits no behaviour units and no transform shapes.
"""

from __future__ import annotations

import posixpath

from repo_intel.compile.ontology import EvidenceClass
from repo_intel.platforms.bms.emit import Emitter, decode
from repo_intel.platforms.bms.statements import find_op, lex
from repo_intel.platforms.ims.dbd import DbdReader
from repo_intel.platforms.ims.mfs import MfsReader
from repo_intel.platforms.ims.psb import PsbReader
from repo_intel.platforms.ims.sysdef import SysdefReader

INPUT_DIR = ".rkp/inputs/ims/"
SUFFIXES = (".dbd", ".psb", ".mfs", ".asm", ".mac")


def _dbd(text: str) -> bool:
    return find_op(text, ("DBD",)) and find_op(text, ("SEGM",))


def _psb(text: str) -> bool:
    return find_op(text, ("PSBGEN",)) or find_op(text, ("PCB",))


def _mfs(text: str) -> bool:
    return find_op(text, ("FMT", "MSG")) and find_op(text, ("DFLD", "MFLD"))


def detect(path: str, text: str) -> str | None:
    """Which IMS source a file is: `dbd`, `psb`, `mfs`, `sysdef`, or None."""
    lower = path.lower().replace("\\", "/")
    ext = posixpath.splitext(lower)[1]
    if lower.startswith(INPUT_DIR):
        if find_op(text, ("APPLCTN", "TRANSACT")):
            return "sysdef"
    elif ext not in SUFFIXES:
        return None
    if ext == ".dbd":
        return "dbd" if find_op(text, ("DBD", "SEGM")) else None
    if ext == ".psb":
        return "psb" if _psb(text) else None
    if ext == ".mfs":
        return "mfs" if find_op(text, ("FMT", "MSG", "DFLD", "MFLD")) else None
    if ext in (".asm", ".mac"):
        if find_op(text, ("DBDGEN",)) and find_op(text, ("SEGM",)):
            return "dbd"
        if find_op(text, ("PSBGEN",)):
            return "psb"
        return None
    if _dbd(text):
        return "dbd"
    if _psb(text):
        return "psb"
    if _mfs(text):
        return "mfs"
    return None


_READERS = {"dbd": DbdReader, "psb": PsbReader, "mfs": MfsReader, "sysdef": SysdefReader}
_WHAT = {"dbd": "database descriptions", "psb": "program specification blocks", "mfs": "message descriptors", "sysdef": "transactions"}


class ImsSource:
    id = "ims.source"
    version = "1.0.0"
    # A DBD or PSB in `.asm` or `.mac` is not also read as assembler (contract 11.1 item 1).
    supersedes_when_parsed = ("hlasm.source",)

    def matches(self, path: str) -> bool:
        lower = path.lower().replace("\\", "/")
        if lower.startswith(INPUT_DIR):
            return not posixpath.basename(lower).startswith(".")
        return lower.endswith(SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        text = decode(data)
        kind = detect(path, text)
        if kind is None:
            ext = posixpath.splitext(path.lower())[1]
            if ext in (".dbd", ".psb", ".mfs") or path.lower().startswith(INPUT_DIR):
                return Extraction(diagnostics=[diagnostic(self.id, "info", "not an IMS source this pack reads (DBD, PSB, MFS or stage-1 APPLCTN and TRANSACT)")], parsed=False)
            return Extraction(parsed=False)
        em = Emitter(self.id, path, data, blob, text, EvidenceClass.E1 if kind == "sysdef" else EvidenceClass.E2)
        lexed = lex(text)
        for message in lexed.notes:
            em.note(message, "warning")
        reader = _READERS[kind](em)
        for s in lexed.statements:
            reader.statement(s)
        reader.finish()
        if not reader.read:
            em.note(f"no {_WHAT[kind]} read", "info")
        return em.done()


__all__ = ["ImsSource", "detect"]
