"""The IMS platform pack (docs/solution-ir-contract.md, section 11; W8.3).

`ims.source` reads IMS DBD source (segments as `Table`s, lossy, with their hierarchy, and
their fields as `Field`s), PSB source (what the program a PSB is named for may read and
write, from its PCBs and sensitive segments), MFS source (message input and output
descriptors as `Screen`s with the device fields they map as `UIControl`s), and the
stage-1 system definition committed under `.rkp/inputs/ims/` (transactions as `Endpoint`s
the program of their application EXPOSES).

Modules: `vocabulary` (terms and constructs), one reader per source kind (`dbd`, `psb`,
`mfs`, `sysdef`) and `source` (the extractor, which recognises the kind of a file by its
content). Statements are read with the BMS pack's assembler macro lexer
(`repo_intel.platforms.bms.statements`). None of them imports the `repo_intel.extractors`
package at module level: pack discovery runs while that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.ims.source import ImsSource
from repo_intel.platforms.ims.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="ims",
    name="IMS (DBD, PSB, MFS and the stage-1 system definition)",
    prefix="ims",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(ImsSource(),),
    inputs=(".rkp/inputs/ims/**",),
)
