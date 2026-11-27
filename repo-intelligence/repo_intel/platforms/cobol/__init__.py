"""The COBOL platform pack: programs, copybooks, paragraphs, files, embedded SQL and CICS.

It replaces `legacy.cobol` and emits at least what that extractor emitted, with the same
kinds and natural keys (see `source`). Keys follow docs/solution-ir-contract.md, 11.1.

Modules:

- `lexer`: fixed and free reference format to tokens in one linear pass (sequence and
  indicator areas, comment and debug lines, continued literals, `*>` comments, `>>`
  directives, PICTURE strings as one token);
- `parse`: COPY and REPLACE directing statements (recorded, never expanded), programs
  (nested and batch), the environment division's SELECTs, the data division's items
  (levels, PIC, USAGE, OCCURS, REDEFINES, 88-levels, layout offsets) and the procedure
  division's sections and paragraphs;
- `lower`: procedure statements lowered to the behavioural IR, with the facts the
  extractor turns into relations (PERFORM and GO TO targets, CALLs, OPEN modes, EXEC SQL
  and EXEC CICS commands);
- `source`: the extractor, one file at a time;
- `vocabulary`: terms and constructs.

Pack modules import `repo_intel.extractors` modules only inside functions: pack
discovery runs while that package is loading.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.cobol.source import CobolSource
from repo_intel.platforms.cobol.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="cobol",
    name="COBOL",
    prefix="cbl",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(CobolSource(),),
    supersedes=("legacy.cobol",),
)

__all__ = ["PACK", "CobolSource"]
