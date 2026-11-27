"""The CICS BMS platform pack (docs/solution-ir-contract.md, section 11; W8.3).

`bms.maps` reads BMS map source: a mapset (`DFHMSD`) and its maps (`DFHMDI`) as
`Screen`s, each with a `Form`, and their named fields (`DFHMDF`) as `UIControl`s, with
position, length, attributes, pictures and initial text as attributes (`source`).

Modules: `statements` (the assembler macro statement lexer, which the IMS pack also
uses), `emit` (emission helpers shared with the IMS pack), `vocabulary` (terms and
constructs) and `source` (the extractor). None of them imports the `repo_intel.extractors`
package at module level: pack discovery runs while that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.bms.source import BmsMaps
from repo_intel.platforms.bms.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="bms",
    name="CICS Basic Mapping Support (BMS map source)",
    prefix="bms",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(BmsMaps(),),
)
