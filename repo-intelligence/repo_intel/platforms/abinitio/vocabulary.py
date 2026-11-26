"""The Ab Initio pack's vocabulary: platform terms and the constructs it looks for.

Components are named by their `mp` command (`reformat`, `sort`, `output-table`); `graph.COMPONENTS`
gives each its family, which says how the pack reads it. Families whose logic lives in an `.xfr`
transform the pack does not read are counted as not modelled.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term
from repo_intel.platforms.abinitio.graph import COMPONENTS

TERMS = (
    Term("abi:Graph", K.JOB, "lossy", "a graph, read from the mp commands of its deployed script; named by `mp job`"),
    Term("abi:Component", K.TASK, "lossy", "a component of a graph, named by its label; its mp command and family in attributes"),
    Term("abi:RecordFormat", K.DATA_SCHEMA, "lossy", "a DML record format: the file's record, or a named type; named by the file path (and #type)"),
    Term("abi:DmlField", K.FIELD, "lossy", "a field of a record format; base type, length, scale, delimiter, format, vector and nullability in attributes"),
)

# family -> what a component of it is modelled as
_FAMILY_MODELLED = {
    "input_file": "entity:Dataset",
    "output_file": "entity:Dataset",
    "input_table": "relation:READS_FROM",
    "output_table": "relation:WRITES_TO",
    "update_table": "relation:WRITES_TO",
    "run_sql": "relation:WRITES_TO",
    "filter": "entity:Task",
    "sort": "entity:Task",
    "dedup": "entity:Task",
    "aggregate": "entity:Task",
    "join": "entity:Task",
    "union": "entity:Task",
    "pass": "entity:Task",
    "xfr": "unmapped",
    "other": "unmapped",
}

_OTHER = (
    ("graph", "entity:Job", "a graph (`mp job`) of a deployed script"),
    ("flow", "relation:DEPENDS_ON", "a flow between two components' ports"),
    ("transform_ref", "unmapped", "a component's `-transform` (.xfr) file: named, not read"),
    ("dynamic_name", "unmapped", "a file or table named by parameters ($AI_SERIAL/...): kept dynamic"),
    ("command_unknown", "unmapped", "an mp command this pack does not know"),
    ("record_format", "entity:DataSchema", "a DML record format with its fields"),
    ("type_definition", "entity:DataSchema", "a named DML type (`type x = record ... end;`)"),
    ("type_alias", "unmapped", "a DML type defined as another named type"),
    ("dml_include", "unmapped", "an `include` of another DML file: not followed"),
    ("dml_unparsed", "unmapped", "a DML statement or field this pack does not read (conditional fields, expressions)"),
    ("graph_binary", "unmapped", "a binary graph (.mp): not read; deploy the graph as a script and commit it under .rkp/inputs/abinitio/"),
    ("transform_file", "unmapped", "a transform (.xfr): its DML functions are not lowered"),
)


def _constructs() -> tuple[Construct, ...]:
    out = [Construct(f"abi:component.{name}", text, _FAMILY_MODELLED[family]) for name, (family, text) in COMPONENTS.items()]
    out += [Construct(f"abi:{name}", text, modelled) for name, modelled, text in _OTHER]
    return tuple(sorted(out, key=lambda c: c.id))


CONSTRUCTS = _constructs()
