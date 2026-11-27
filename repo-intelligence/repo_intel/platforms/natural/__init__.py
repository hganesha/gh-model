"""The Software AG Natural and Adabas platform pack (docs/solution-ir-contract.md, section 10; W8.6).

`natural.source` reads Natural source exports (`.nsp`, `.nsn`, `.nss`, `.nsh`, `.nsc`,
`.nsm`, `.nsl`, `.nsa`, `.nsg`, `.nsd`) in structured and reporting mode: programs,
subprograms, external subroutines, helproutines and copycode as Programs with a behaviour
unit for their main code and each inline subroutine; data areas as DataSchemas with their
Fields; maps as Screens with their controls; DDMs as Adabas Tables with their Fields. What
the code does to Adabas (READ, FIND, HISTOGRAM, GET, STORE, UPDATE, DELETE through a view
of a DDM), the objects it runs (CALLNAT, FETCH, RUN, PERFORM, CALL), the maps it shows, the
data areas and copycode it uses, and its Natural SQL, are relations.

Modules: `lexer` (tokens, comments and editor line numbers), `data` (DEFINE DATA), `parse`
(statements, lowered into the behavioural IR as they are read), `maps`, `ddm`,
`vocabulary` (terms and constructs) and `source` (the extractor). None of them imports the
`repo_intel.extractors` package at module level: pack discovery runs while that package
loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.natural.source import NaturalSource
from repo_intel.platforms.natural.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="natural",
    name="Software AG Natural and Adabas (programs, subprograms, maps, data areas, DDMs)",
    prefix="nat",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(NaturalSource(),),
)
