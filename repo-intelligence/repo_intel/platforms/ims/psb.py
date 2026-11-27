"""IMS program specification blocks (PSBGEN source): PCB, SENSEG, SENFLD, PSBGEN.

A PSB is named for the program that uses it (`PSBGEN PSBNAME=`), so its PCBs say what that
program may read and write (contract 11.1 item 8). The program is
`Program {platform: "mainframe", name: <PSBNAME>}`, never declared here: the COBOL, PL/I or
assembler source declares it. For each sensitive segment of a database PCB, it READS_FROM
the segment's Table when the processing options allow a get (G, or R, D or A, which
include one), and WRITES_TO it when they allow insert, replace, delete or load (I, R, D,
A, L). A SENSEG's own PROCOPT replaces the PCB's. Both relations carry the qualifiers
`pcb` (the PCB's name, or `pcb<n>` by its position among the PSB's PCBs), `procopt`, and,
when given, `procseq` (the secondary index it is reached through) and `fields` (its
SENFLDs). Segment Tables are never declared here: a segment links when the repository
holds its DBD.

An alternate PCB (`TYPE=TP`) whose destination is literal and not modifiable is a
CONSUMES of the IMS transaction of that name, never declared here, so it links only when
the system definition names that transaction (the destination may be a terminal). PCBs
with no PSBGEN after them, GSAM PCBs and modifiable destinations are counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.bms.emit import Emitter
from repo_intel.platforms.bms.statements import Quoted, Statement, dynamic, first, is_name, words
from repo_intel.platforms.ims.dbd import datastore
from repo_intel.platforms.ims.vocabulary import PLATFORM, PROGRAM_PLATFORM

LISTING = frozenset({"PRINT", "TITLE", "EJECT", "SPACE", "END", "FINISH", "ICTL", "PUNCH"})
READS = frozenset("GRDA")
WRITES = frozenset("IRDAL")
TRANSACTION = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$")


def procopt_letters(v: Any) -> str | None:
    if isinstance(v, str) and not isinstance(v, Quoted) and v and not dynamic(v) and v.isalpha():
        return v
    return None


@dataclass
class _Senseg:
    stmt: Statement
    name: str | None
    procopt: str | None
    fields: list[str] = field(default_factory=list)
    field_stmts: list[Statement] = field(default_factory=list)


@dataclass
class _Pcb:
    stmt: Statement
    type: str
    name: str | None
    dbd: str | None = None
    procopt: str | None = None
    procseq: str | None = None
    destination: str | None = None
    modify: bool = False
    segments: list[_Senseg] = field(default_factory=list)


def endpoint_ref(em: Emitter, code: str, declare: bool = True) -> dict[str, Any]:
    return em.ref(K.ENDPOINT, {"component_root": em.base.UNIT, "protocol": PLATFORM, "method": "TRANSACTION", "route": code}, f"TRANSACTION {code}", declare=declare)


def is_transaction_code(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 8 and v[0].isalpha() and all(c in TRANSACTION for c in v.upper())


class PsbReader:
    def __init__(self, em: Emitter):
        self.em = em
        self.pcbs: list[_Pcb] = []
        self.read = 0

    def statement(self, s: Statement) -> None:
        em = self.em
        for o in s.operands:
            if dynamic(o.value):
                em.construct(o.start, o.end, "ims:symbolic_parameter", False)
        op = s.op
        if op == "PCB":
            self.pcb(s)
        elif op == "SENSEG":
            pcb = self.pcbs[-1] if self.pcbs and self.pcbs[-1].type == "DB" else None
            name = first(s.value("NAME"))
            if pcb is None:
                em.note(f"line {s.line}: a SENSEG outside a database PCB: not read", "warning")
                em.construct(s.start, s.end, "ims:sensitive_segment", False)
                return
            pcb.segments.append(_Senseg(s, name if is_name(name) else None, procopt_letters(s.value("PROCOPT"))))
        elif op == "SENFLD":
            pcb = self.pcbs[-1] if self.pcbs and self.pcbs[-1].type == "DB" else None
            name = first(s.value("NAME"))
            if pcb is None or not pcb.segments or not is_name(name):
                em.construct(s.start, s.end, "ims:sensitive_field", False)
                return
            pcb.segments[-1].fields.append(name)
            pcb.segments[-1].field_stmts.append(s)  # counted when the PSBGEN says whose PSB it is
        elif op == "PSBGEN":
            self.psbgen(s)
        elif op not in LISTING:
            em.construct(s.start, s.end, "ims:other_statement", False)

    def pcb(self, s: Statement) -> None:
        ptype = first(s.value("TYPE"))
        ptype = ptype if isinstance(ptype, str) and not isinstance(ptype, Quoted) else ("DB" if s.get("DBDNAME") else None)
        label = first(s.value("PCBNAME")) or s.name
        pcb = _Pcb(s, ptype or "?", label if is_name(label) else None)
        if pcb.type == "DB":
            dbd = first(s.value("DBDNAME")) or first(s.value("NAME"))
            pcb.dbd = dbd if is_name(dbd) else None
            pcb.procopt = procopt_letters(s.value("PROCOPT")) or ("A" if s.get("PROCOPT") is None else None)
            seq = first(s.value("PROCSEQ"))
            pcb.procseq = seq if is_name(seq) else None
        elif pcb.type == "TP":
            dest = first(s.value("NAME")) or first(s.value("LTERM"))
            pcb.destination = dest if is_name(dest) else None
            pcb.modify = first(s.value("MODIFY")) == "YES"
        self.pcbs.append(pcb)

    def psbgen(self, s: Statement) -> None:
        em = self.em
        name = first(s.value("PSBNAME"))
        pcbs, self.pcbs = self.pcbs, []
        if not is_name(name):
            em.note(f"line {s.line}: a PSBGEN without a literal PSBNAME: the program its PCBs belong to is not known", "warning")
            em.construct(s.start, s.end, "ims:psbgen", False)
            self.count_unread(pcbs)
            return
        em.construct(s.start, s.end, "ims:psbgen", True)
        program = em.ref(K.PROGRAM, {"platform": PROGRAM_PLATFORM, "name": name}, name, declare=False)
        for n, pcb in enumerate(pcbs, start=1):
            label = pcb.name or f"pcb{n}"
            p = pcb.stmt
            if pcb.type == "DB":
                self.db_pcb(program, pcb, label)
            elif pcb.type == "TP":
                if pcb.destination and not pcb.modify:
                    if is_transaction_code(pcb.destination):
                        em.rel(p.start, p.end, program, P.CONSUMES, endpoint_ref(em, pcb.destination, declare=False), {"pcb": label, "via": "alternate_pcb"})
                    em.construct(p.start, p.end, "ims:tp_pcb", is_transaction_code(pcb.destination))
                else:
                    em.construct(p.start, p.end, "ims:modifiable_tp_pcb", False)
            elif pcb.type == "GSAM":
                em.construct(p.start, p.end, "ims:gsam_pcb", False)
            else:
                em.construct(p.start, p.end, "ims:other_statement", False)
        self.read += 1

    def db_pcb(self, program: dict[str, Any], pcb: _Pcb, label: str) -> None:
        em, p = self.em, pcb.stmt
        if pcb.dbd is None:
            em.note(f"line {p.line}: a database PCB without a literal DBDNAME: its segments are not read", "warning")
            em.construct(p.start, p.end, "ims:db_pcb", False)
            for sg in pcb.segments:
                em.construct(sg.stmt.start, sg.stmt.end, "ims:sensitive_segment", False)
                self.fields(sg, False)
            return
        em.construct(p.start, p.end, "ims:db_pcb", True)
        if p.get("PROCSEQ"):
            em.construct(p.start, p.end, "ims:secondary_sequence", pcb.procseq is not None)
        for sg in pcb.segments:
            s = sg.stmt
            procopt = sg.procopt or pcb.procopt
            if sg.name is None or procopt is None:
                em.construct(s.start, s.end, "ims:sensitive_segment", False)
                self.fields(sg, False)
                continue
            letters = set(procopt.upper())
            reads, writes = bool(letters & READS), bool(letters & WRITES)
            if not reads and not writes:
                em.construct(s.start, s.end, "ims:key_sensitive_segment" if "K" in letters else "ims:sensitive_segment", False)
                self.fields(sg, False)
                continue
            table = em.ref(K.TABLE, {"datastore": datastore(pcb.dbd), "name": sg.name}, sg.name, declare=False)
            q = {"pcb": label, "procopt": procopt, "procseq": pcb.procseq, "fields": sg.fields}
            if reads:
                em.rel(s.start, s.end, program, P.READS_FROM, table, q)
            if writes:
                em.rel(s.start, s.end, program, P.WRITES_TO, table, q)
            em.construct(s.start, s.end, "ims:sensitive_segment", True)
            self.fields(sg, True)

    def fields(self, sg: _Senseg, modelled: bool) -> None:
        for f in sg.field_stmts:
            self.em.construct(f.start, f.end, "ims:sensitive_field", modelled)

    def count_unread(self, pcbs: list[_Pcb]) -> None:
        for pcb in pcbs:
            p = pcb.stmt
            cid = {"DB": "ims:db_pcb", "TP": "ims:tp_pcb", "GSAM": "ims:gsam_pcb"}.get(pcb.type, "ims:other_statement")
            self.em.construct(p.start, p.end, cid, False)
            for sg in pcb.segments:
                self.em.construct(sg.stmt.start, sg.stmt.end, "ims:sensitive_segment", False)
                self.fields(sg, False)

    def close(self) -> None:
        if self.pcbs:
            self.em.note(f"{len(self.pcbs)} PCB(s) with no PSBGEN after them: the program they belong to is not known", "warning")
            self.count_unread(self.pcbs)
            self.pcbs = []

    def finish(self) -> None:
        self.close()


__all__ = ["PsbReader", "endpoint_ref", "is_transaction_code", "procopt_letters"]
