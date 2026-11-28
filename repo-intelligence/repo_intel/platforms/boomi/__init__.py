"""Boomi Integration component XML exported from Build or the Platform API."""

from repo_intel.platforms import Construct, PlatformPack, Term
from repo_intel.platforms.boomi.source import BoomiComponent
from repo_intel.compile.ontology import EntityKind as K

PACK = PlatformPack(
    id="boomi",
    name="Boomi AtomSphere Integration",
    prefix="boomi",
    version="1.0.0",
    terms=(
        Term("boomi:Process", K.JOB, "lossy", "an integration process"),
        Term("boomi:Shape", K.TASK, "lossy", "a process canvas step"),
        Term("boomi:Connection", K.CONFIG_KEY, "lossy", "a reusable connector connection"),
        Term("boomi:Operation", K.PROGRAM, "lossy", "a reusable connector operation"),
        Term("boomi:Map", K.PROGRAM, "lossy", "a data map"),
        Term("boomi:Profile", K.DATA_SCHEMA, "lossy", "a document profile"),
        Term("boomi:Field", K.FIELD, "lossy", "a keyed profile element"),
    ),
    constructs=(
        Construct("boomi:process", "a process definition", "entity:Job"),
        Construct("boomi:shape", "a process canvas step", "entity:Task"),
        Construct("boomi:dragpoint", "an outgoing canvas path", "relation:DEPENDS_ON"),
        Construct("boomi:connection", "a connector connection", "entity:ConfigKey"),
        Construct("boomi:operation", "a connector operation", "entity:Program"),
        Construct("boomi:map", "a data map", "entity:Program"),
        Construct("boomi:mapping", "a direct or computed map binding", "attribute:field_mappings"),
        Construct("boomi:profile", "a document profile", "entity:DataSchema"),
        Construct("boomi:profile_element", "a keyed profile element", "entity:Field"),
        Construct("boomi:other_component", "a component type outside the supported set", "unmapped"),
    ),
    extractors=(BoomiComponent(),),
    inputs=(".rkp/inputs/boomi/**",),
)
