"""The PL/I platform pack (docs/solution-ir-contract.md, section 11; W8.5).

`pli.source` reads PL/I programs and %INCLUDE members (`.pli`, `.pl1`): margins and
`*PROCESS` options, external procedures as the load-module Programs every caller names,
internal procedures as Symbols with behaviour units, `%INCLUDE` members as DataSchemas with
their structures as Fields, calls, files by DD name, embedded SQL and CICS, and DL/I calls.

Modules: `lexer` (margins and tokens), `declare` (DECLARE elements and record layouts),
`parse` (the parser, which lowers bodies into the behavioural IR as it reads them),
`vocabulary` (terms and constructs), `source` (the extractor) and `common`, the helpers the
other W8.5 packs (`easytrieve`, `rexx`, `hlasm`) share with this one. None of them imports
the `repo_intel.extractors` package at module level: pack discovery runs while that package
loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.pli.source import PliSource
from repo_intel.platforms.pli.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="pli",
    name="PL/I (programs, internal procedures and %INCLUDE members)",
    prefix="pli",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(PliSource(),),
)
