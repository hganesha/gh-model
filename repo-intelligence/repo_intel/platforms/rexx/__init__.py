"""The REXX and CLIST pack (docs/solution-ir-contract.md, section 11; W8.5).

`rexx.source` reads TSO REXX execs and CLISTs: each is a `Program {platform: "tso"}` with
the programs and execs it runs (CALLS), the datasets it allocates and reads or writes, its
files by DD name and the tables of its static SQL.

No behaviour units: a REXX exec's logic is mostly the commands it builds and sends to host
environments, whose text is often assembled at run time, and a CLIST's control flow is
GOTO-based. The commands are read for what they name; the logic around them is counted as
constructs (`rexx:control`, `rexx:assignment` ...), not lowered.

Modules: `tso` (TSO commands and what they name), `vocabulary` (terms and constructs) and
`source` (the readers and the extractor). They import the `repo_intel.extractors` package
only inside functions.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.rexx.source import RexxSource
from repo_intel.platforms.rexx.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="rexx",
    name="TSO REXX and CLIST (execs, the programs they run and the datasets they allocate)",
    prefix="rexx",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(RexxSource(),),
)
