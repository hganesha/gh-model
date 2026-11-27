"""The BMS pack's vocabulary: platform terms and the constructs it looks for.

Keys follow docs/solution-ir-contract.md, section 11.1 item 7: a map is
`Screen {platform: "bms", name: "<mapset>.<map>"}` with one
`Form {screen: "<mapset>.<map>", name: <map>}`, and each named field is
`UIControl {container: "<mapset>.<map>", name: <field label>}`. Mapset, map and field
names are assembler symbols, so they are upper-cased.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "bms"

TERMS = (
    Term("bms:Map", K.SCREEN, "lossy", "a BMS map (DFHMDI) of a mapset (DFHMSD): name `<mapset>.<map>`; size, position and the mapset's options in attributes"),
    Term("bms:MapForm", K.FORM, "lossy", "the input of a map: what RECEIVE MAP reads; one per map, named by the map"),
    Term("bms:Field", K.UI_CONTROL, "lossy", "a named field (DFHMDF with a label); position, length, ATTRB, PICIN, PICOUT and INITIAL in attributes"),
)

_CONSTRUCTS = (
    ("mapset", "DFHMSD: a mapset, its TYPE, MODE, LANG, CTRL, STORAGE and TIOAPFX", "attribute:mapset"),
    ("map", "DFHMDI: a map of the mapset", "entity:Screen"),
    ("named_field", "DFHMDF with a label: a field the program reads or writes", "entity:UIControl"),
    ("literal_field", "DFHMDF with no label and an INITIAL text: screen text; a short one labels the next named field", "attribute:label"),
    ("unnamed_field", "DFHMDF with no label and no INITIAL (a stopper or spacer field)", "unmapped"),
    ("occurs", "OCCURS=: a repeated field", "attribute:occurs"),
    ("group_field", "GRPNAME=: a field that is part of a group field", "attribute:group"),
    ("picture", "PICIN= or PICOUT=: the field's COBOL or PL/I picture", "attribute:picin"),
    ("validation", "VALIDN=: MUSTFILL, MUSTENTER or TRIGGER", "attribute:validation"),
    ("extended_attributes", "COLOR=, HILIGHT=, PS=, OUTLINE=, SOSI=, TRANSP=, CASE=, EXTATT=, MAPATTS=, DSATTS=", "attribute:extended"),
    ("hex_or_graphic_initial", "XINIT= or GINIT=: initial text given in hexadecimal or as DBCS; its presence is recorded, not its bytes", "attribute:initial_encoded"),
    ("cursor_location", "CURSLOC=YES on a map", "attribute:cursloc"),
    ("header_trailer", "HEADER= or TRAILER= on a map (cumulative paging with ACCUM)", "attribute:header"),
    ("symbolic_parameter", "an operand set by a symbolic parameter (TYPE=&SYSPARM): its value is decided when the map is assembled", "unmapped"),
    ("partition_set", "DFHPSD or DFHPDI: a partition set", "unmapped"),
    ("map_without_mapset", "a DFHMDI or DFHMDF outside a mapset: not read", "unmapped"),
    ("copy", "COPY of another source member: not followed", "unmapped"),
    ("other_statement", "another assembler statement in a map source", "unmapped"),
)
CONSTRUCTS = tuple(Construct(f"bms:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS)
