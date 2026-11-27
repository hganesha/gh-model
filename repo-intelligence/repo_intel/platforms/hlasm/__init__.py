"""The High Level Assembler pack (docs/solution-ir-contract.md, section 11; W8.5).

`hlasm.source` reads assembler source at construct level: the program (its first control
section) with its entry points, the programs it calls, links to, transfers to and loads by
literal name, its DCB and ACB files by DD name, EXEC CICS LINK and SEND MAP, and static
EXEC SQL. Machine instructions, assembler instructions and macros are counted, not lowered:
the pack gives no behaviour units.

Modules: `reader` (fixed-format statements), `vocabulary` (terms, constructs, the tables of
mnemonics, directives and listed macros) and `source` (the extractor). The `bms` pack
names `hlasm.source` in `supersedes_when_parsed`, so a BMS map set is read once.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.hlasm.source import HlasmSource
from repo_intel.platforms.hlasm.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="hlasm",
    name="High Level Assembler (programs, calls, files and macros, at construct level)",
    prefix="asm",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(HlasmSource(),),
)
