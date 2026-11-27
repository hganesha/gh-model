"""The IDMS pack's vocabulary: platform terms and the constructs it looks for.

Keys (docs/solution-ir-contract.md, section 11.1 items 4 and 8): a record is
`Table {datastore: "idms:<schema>", name: <record>}` with its sets as `attributes.sets` on
the owner record (`[{set, member, order}]`); an element is
`Field {table: "idms:<schema>.<record>", name}`; an 88-level is
`CodeList {platform: "idms", name: "<schema>.<record>.<field>"}`. Where 11.1 says nothing:
a schema is `DataSchema {kind: "idms-schema", name}` and a subschema
`DataSchema {kind: "idms-subschema", name}`, as data areas are `natural-lda` and copybooks
`copybook`. Names are upper case, as IDMS keeps them.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "idms"

TERMS = (
    Term("idms:Schema", K.DATA_SCHEMA, "lossy", "an IDMS schema: its areas in attributes, its records CONTAINed"),
    Term("idms:Subschema", K.DATA_SCHEMA, "lossy", "a subschema: the view of a schema a program binds, its areas, records and sets"),
    Term("idms:Record", K.TABLE, "lossy", "a record type: location mode, area and the sets it owns and belongs to in attributes"),
    Term("idms:Element", K.FIELD, "lossy", "an element of a record: level, picture, usage, occurrences, redefinition, offset and length"),
    Term("idms:Condition", K.CODE_LIST, "lossy", "the 88-level condition names of an element and their values"),
)

_CONSTRUCTS = (
    ("schema", "entity:DataSchema", "ADD SCHEMA, or SCHEMA NAME IS in the older DDL"),
    ("subschema", "entity:DataSchema", "ADD SUBSCHEMA ... OF SCHEMA"),
    ("area", "attribute:areas", "an AREA of the schema; a record's area is its WITHIN AREA"),
    ("record", "entity:Table", "a RECORD of the schema"),
    ("element", "entity:Field", "an element (level-numbered field) of a record; FILLER is counted, not declared"),
    ("condition", "entity:CodeList", "an 88-level condition name under an element"),
    ("redefines", "attribute:redefines", "REDEFINES"),
    ("occurs", "attribute:occurs", "OCCURS [DEPENDING ON]"),
    ("set", "attribute:sets", "a SET: order, mode, owner and members, on the owner record"),
    ("subschema_area", "attribute:areas", "an AREA a subschema includes"),
    ("subschema_record", "relation:CONTAINS", "a RECORD a subschema includes, with the elements it views"),
    ("subschema_set", "attribute:sets", "a SET a subschema includes"),
    ("logical_record", "unmapped", "a LOGICAL RECORD or PATH-GROUP of a subschema"),
    ("copy_elements", "unmapped", "COPY ELEMENTS FROM another record: the elements are that record's"),
    ("delete", "unmapped", "DELETE of a schema object"),
    ("physical", "unmapped", "a FILE, SEGMENT, DMCL, BUFFER or JOURNAL statement: storage, not the logical database"),
    ("directive", "unmapped", "a compiler directive: VALIDATE, GENERATE, SET OPTIONS, SIGNON, DISPLAY, PUNCH"),
    ("unrecognised_statement", "unmapped", "a statement outside any object this reader knows"),
)
CONSTRUCTS = tuple(sorted((Construct(f"idms:{name}", text, modelled) for name, modelled, text in _CONSTRUCTS), key=lambda c: c.id))
