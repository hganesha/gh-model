"""The Oracle PL/SQL platform pack (docs/solution-ir-contract.md, section 10; W7.1).

`plsql.source` reads packages, package bodies, procedures, functions and triggers
(`.pks`, `.pkb`, `.pls`, `.plb`, `.fnc`, `.trg`, and `.prc` and `.sql` files whose content
is Oracle's) and the deployment scripts around them: packages as Programs that CONTAIN
Symbols for their procedures and functions, triggers as Programs with the table and
events they fire on, a behaviour unit for each body, table reads and writes through the
shared SQL lineage scanner in the `oracle` dialect, calls, database links, synonyms,
grants and DBMS_SCHEDULER and DBMS_JOB jobs.

Modules: `lexer` (tokens), `parse` (the parser, which lowers bodies into the behavioural
IR as it reads them), `vocabulary` (terms and constructs) and `source` (the extractor).
None of them imports the `repo_intel.extractors` package at module level: pack discovery
runs while that package loads. The parser is importable on its own, for packs that meet
PL/SQL elsewhere (Oracle Forms triggers).
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.plsql.source import PlsqlSource
from repo_intel.platforms.plsql.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="plsql",
    name="Oracle PL/SQL (packages, procedures, functions, triggers, deployment scripts)",
    prefix="ora",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(PlsqlSource(),),
)
