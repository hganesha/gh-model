"""The CA IDMS platform pack (docs/solution-ir-contract.md, section 11; W8.6).

`idms.source` reads IDMS schema and subschema source (schema compiler and IDD syntax, and
the older schema DDL), recognised by content, under `.rkp/inputs/idms/` or with a suffix
IDMS source is kept under: records as Tables keyed by their schema, their elements as
Fields with picture, usage, offset and length, sets on their owner and member records,
88-level conditions as code lists, and subschemas as the DataSchemas a program binds.
COBOL programs with IDMS DML are the COBOL pack's.

Modules: `ddl` (the statement reader), `vocabulary` (terms and constructs) and `source`
(the extractor). None of them imports the `repo_intel.extractors` package at module level.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.idms.source import IdmsSource
from repo_intel.platforms.idms.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="idms",
    name="CA IDMS (schema and subschema source: records, elements, sets)",
    prefix="idms",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(IdmsSource(),),
    inputs=(".rkp/inputs/idms/**",),
)
