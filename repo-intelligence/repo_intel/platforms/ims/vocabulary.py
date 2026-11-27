"""The IMS pack's vocabulary: platform terms and the constructs it looks for.

Keys follow docs/solution-ir-contract.md, section 11.1:

- a segment is `Table {datastore: "ims:<DBD>", name: <segment>}` (item 8), its parent
  segment CONTAINS it, and its fields are `Field {table: "ims:<DBD>.<segment>", name}`,
  CONTAINed by the segment;
- a PSB's program is `Program {platform: "mainframe", name: <PSBNAME>}` (items 2 and 8),
  never declared here, which READS_FROM and, when PROCOPT allows insert, replace, delete
  or load, WRITES_TO each sensitive segment;
- an MFS message descriptor is `Screen {platform: "ims", name: <MID or MOD name>}`
  (item 7), with a `UIControl {container: <MID or MOD name>, name: <device field>}` for
  each device field one of its message fields names;
- a transaction of the system definition is
  `Endpoint {component_root: "@unit", protocol: "ims", method: "TRANSACTION", route: <code>}`
  (item 6), which the program its APPLCTN names EXPOSES.

IMS names are assembler symbols, so they are upper-cased.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "ims"
PROGRAM_PLATFORM = "mainframe"  # contract 11.1 item 2: one key for every executable program

TERMS = (
    Term("ims:Segment", K.TABLE, "lossy", "a segment of an IMS database (DBD SEGM): hierarchy, length, pointers, logical relationships and indexes in attributes"),
    Term("ims:Field", K.FIELD, "lossy", "a field of a segment (DBD FIELD): start, length, type and sequence (key) in attributes"),
    Term("ims:Message", K.SCREEN, "lossy", "an MFS message descriptor: an input (MID) or output (MOD) message and the format (FMT) it maps to a device"),
    Term("ims:DeviceField", K.UI_CONTROL, "lossy", "a named device field (DFLD) a message field (MFLD) maps; position, length and attributes in attributes"),
    Term("ims:Transaction", K.ENDPOINT, "lossy", "an IMS transaction code (stage-1 TRANSACT): method TRANSACTION, the code as route"),
)

_CONSTRUCTS = (
    # DBD: database descriptions.
    ("dbd", "DBD: the database's name and access method", "attribute:access"),
    ("dataset_group", "DATASET: the data set group (DD name) the segments after it are stored in", "attribute:dd"),
    ("area", "AREA: a Fast Path DEDB area", "unmapped"),
    ("segment", "SEGM: a segment and its physical parent", "entity:Table"),
    ("field", "FIELD: a field of a segment, its sequence (key) field marked", "entity:Field"),
    ("system_field", "FIELD /SX or /CK: a system-related field of a secondary index", "unmapped"),
    ("logical_parent", "SEGM PARENT=(..., (segment, VIRTUAL or PHYSICAL, dbd)): a logical parent", "attribute:logical_parent"),
    ("logical_child", "LCHILD: a logical child, or the index segment of an index relationship", "attribute:lchild"),
    ("secondary_index", "XDFLD: the indexed field of a secondary index", "attribute:secondary_indexes"),
    ("segment_source", "SEGM SOURCE=: a segment of a logical DBD and the physical segments it is built from", "attribute:source"),
    ("exit", "EXIT= on a DBD or SEGM: a data capture exit routine", "unmapped"),
    ("compression", "COMPRTN= on a SEGM: a segment edit or compression routine", "unmapped"),
    ("randomizer", "RMNAME= on a DBD: the HDAM or PHDAM randomizing module", "unmapped"),
    # PSB: program specification blocks.
    ("db_pcb", "PCB TYPE=DB: a database view, its DBD and processing options", "relation:READS_FROM"),
    ("sensitive_segment", "SENSEG: a segment the program may reach, with its own PROCOPT", "relation:READS_FROM"),
    ("key_sensitive_segment", "SENSEG PROCOPT=K: a segment the program only passes through, reading its key", "unmapped"),
    ("sensitive_field", "SENFLD: a field the program sees, recorded on the segment's relations", "attribute:fields"),
    ("secondary_sequence", "PCB PROCSEQ=: the database is reached through a secondary index", "attribute:procseq"),
    ("tp_pcb", "PCB TYPE=TP: an alternate PCB; with a literal NAME, the program sends messages to that transaction or terminal", "relation:CONSUMES"),
    ("modifiable_tp_pcb", "PCB TYPE=TP with MODIFY=YES or no NAME: its destination is set at run time", "unmapped"),
    ("gsam_pcb", "PCB TYPE=GSAM: a sequential data set read or written through DL/I", "unmapped"),
    ("psbgen", "PSBGEN: the program the PSB belongs to (PSBNAME) and its language", "relation:READS_FROM"),
    # MFS: message format services.
    ("format", "FMT: a device format", "attribute:format"),
    ("device", "DEV: a device type a format is written for", "attribute:device"),
    ("division", "DIV: a division of a device format", "unmapped"),
    ("device_page", "DPAGE: a device page", "unmapped"),
    ("device_field", "DFLD with a label: a named device field", "entity:UIControl"),
    ("literal_device_field", "DFLD with a literal and no label: screen text; a short one labels the next named field", "attribute:label"),
    ("pf_keys", "DEV PFK=: literals or functions program function keys send", "unmapped"),
    ("input_message", "MSG TYPE=INPUT: a message input descriptor (MID)", "entity:Screen"),
    ("output_message", "MSG TYPE=OUTPUT: a message output descriptor (MOD)", "entity:Screen"),
    ("logical_page", "LPAGE: a logical page of a message", "unmapped"),
    ("message_segment", "SEG: a segment of a message", "attribute:segments"),
    ("message_field", "MFLD naming a device field", "entity:UIControl"),
    ("literal_message_field", "MFLD with only a literal; at the start of an input message, the transaction code it sends", "relation:NAVIGATES_TO"),
    ("system_message_field", "MFLD of a system literal or control area (DATE2, TIME, LTNAME, SCA) or naming no device field", "unmapped"),
    ("repeated_fields", "DO ... ENDDO: device or message fields repeated with a suffix", "unmapped"),
    ("next_message", "MSG NXT=: the message descriptor used for the next message", "relation:NAVIGATES_TO"),
    ("partition", "PDB or PD: a partition descriptor", "unmapped"),
    ("table_edit", "TABLE, IF and TABLEEND: an operator control table", "unmapped"),
    ("copy", "COPY of another MFS member: not followed", "unmapped"),
    # Stage-1 system definition.
    ("application", "APPLCTN: an application program (by PSB or GPSB) and its type", "attribute:psb"),
    ("transaction", "TRANSACT: a transaction code of the application before it", "entity:Endpoint"),
    ("conversational", "TRANSACT SPA=: a conversational transaction with a scratch pad area", "attribute:conversational"),
    ("fast_path", "APPLCTN or TRANSACT FPATH=: Fast Path", "attribute:fast_path"),
    ("database", "DATABASE: a database the online system opens", "unmapped"),
    ("routing_code", "RTCODE: a Fast Path routing code", "unmapped"),
    ("sysdef_other", "another system definition macro (IMSCTRL, COMM, MSGQUEUE, TERMINAL, NAME, LINEGRP, BUFPOOLS, IMSGEN, ...); its operands are never copied", "unmapped"),
    ("symbolic_parameter", "an operand set by a symbolic parameter: decided when the source is assembled", "unmapped"),
    ("other_statement", "another statement in an IMS source", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"ims:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
