"""IMS stage-1 system definition: APPLCTN and TRANSACT, committed by the owner under
`.rkp/inputs/ims/`.

Each transaction code a TRANSACT names is an `Endpoint {component_root: "@unit",
protocol: "ims", method: "TRANSACTION", route: <code>}` (contract 10.1 item 5 and 11.1
item 6), and the program of the APPLCTN before it (its PSB, or its GPSB, which is the
program's name) EXPOSES it, as `Program {platform: "mainframe", name}` never declared
here. The endpoint's attributes say what the definition states about the transaction:
the PSB, the program type, single or multiple message segments, whether it responds,
the scheduling mode, and whether it is conversational (SPA) or Fast Path. Everything is
read from a declarative export, so evidence is E1.

The other system definition macros (IMSCTRL, COMM, MSGQUEUE, TERMINAL, NAME, BUFPOOLS and
the rest) describe the online system, not the application, and are counted; their
operands, which can hold system names and passwords, are never copied.
"""

from __future__ import annotations

from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.bms.emit import Emitter
from repo_intel.platforms.bms.statements import Statement, dynamic, first, is_name, words
from repo_intel.platforms.ims.psb import endpoint_ref, is_transaction_code
from repo_intel.platforms.ims.vocabulary import PLATFORM, PROGRAM_PLATFORM

LISTING = frozenset({"PRINT", "TITLE", "EJECT", "SPACE", "END", "ICTL", "PUNCH"})


class SysdefReader:
    def __init__(self, em: Emitter):
        self.em = em
        self.app: dict[str, Any] | None = None
        self.read = 0

    def statement(self, s: Statement) -> None:
        em = self.em
        op = s.op
        if op == "APPLCTN":
            self.applctn(s)
        elif op == "TRANSACT":
            self.transact(s)
        elif op == "DATABASE":
            em.construct(s.start, s.end, "ims:database", False)
        elif op == "RTCODE":
            em.construct(s.start, s.end, "ims:routing_code", False)
        elif op not in LISTING:
            em.construct(s.start, s.end, "ims:sysdef_other", False)

    def applctn(self, s: Statement) -> None:
        em = self.em
        psb = first(s.value("PSB"))
        gpsb = first(s.value("GPSB"))
        name = psb if is_name(psb) else gpsb if is_name(gpsb) else None
        pgmtype = first(s.value("PGMTYPE"))
        self.app = {"name": name, "generated": name is not None and name == gpsb and not is_name(psb),
                    "pgmtype": pgmtype if is_name(pgmtype) else None, "fast_path": self.fast_path(s)}
        if name is None:
            em.note(f"line {s.line}: an APPLCTN without a literal PSB or GPSB: its transactions are exposed by no program", "warning")
        em.construct(s.start, s.end, "ims:application", name is not None)
        if s.get("FPATH"):
            em.construct(s.start, s.end, "ims:fast_path", self.app["fast_path"] is not None)

    @staticmethod
    def fast_path(s: Statement) -> bool | None:
        v = first(s.value("FPATH"))
        if v is None:
            return None
        if isinstance(v, str) and not dynamic(v):
            return v != "NO"
        return None

    def transact(self, s: Statement) -> None:
        em = self.em
        raw = s.value("CODE")
        codes = [c for c in (raw if isinstance(raw, list) else [raw]) if c is not None]
        msgtype = words(s.value("MSGTYPE"))
        mode = first(s.value("MODE"))
        fast_path = self.fast_path(s)
        attrs: dict[str, Any] = {
            "psb": self.app["name"] if self.app else None,
            "generated_psb": True if self.app and self.app["generated"] else None,
            "pgmtype": self.app["pgmtype"] if self.app else None,
            "message_segments": "single" if "SNGLSEG" in msgtype else "multiple" if "MULTSEG" in msgtype else None,
            "response": True if "RESPONSE" in msgtype else False if "NONRESPONSE" in msgtype else None,
            "mode": {"SNGL": "single", "MULT": "multiple"}.get(mode) if isinstance(mode, str) else None,
            "conversational": True if s.get("SPA") else None,
            "fast_path": fast_path if fast_path is not None else (self.app["fast_path"] if self.app else None),
        }
        if s.get("SPA"):
            em.construct(s.start, s.end, "ims:conversational", True)
        if s.get("FPATH"):
            em.construct(s.start, s.end, "ims:fast_path", fast_path is not None)
        read = False
        for code in codes:
            if not is_name(code) or not is_transaction_code(code):
                em.note(f"line {s.line}: a TRANSACT CODE that is not a literal transaction code: not read", "warning")
                continue
            code = code.upper()
            ep = em.term(s.start, s.end, "ims:Transaction", endpoint_ref(em, code)["natural_key"], f"TRANSACTION {code}", attrs)
            if self.app and self.app["name"]:
                program = em.ref(K.PROGRAM, {"platform": PROGRAM_PLATFORM, "name": self.app["name"]}, self.app["name"], declare=False)
                em.rel(s.start, s.end, program, P.EXPOSES, ep, {"via": "applctn"})
            elif self.app is None:
                em.note(f"line {s.line}: transaction {code} comes before any APPLCTN: no program exposes it", "warning")
            read = True
            self.read += 1
        em.construct(s.start, s.end, "ims:transaction", read)

    def finish(self) -> None:
        """Nothing is held back: each TRANSACT is emitted as it is read."""


__all__ = ["SysdefReader", "PLATFORM"]
