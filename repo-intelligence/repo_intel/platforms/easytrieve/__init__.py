"""The Easytrieve Plus platform pack (docs/solution-ir-contract.md, section 11; W8.5).

`easytrieve.source` reads Easytrieve programs (`.ezt`): the program as the load-module
Program every caller names, FILE statements and their fields as DataSchemas and Fields, files
by DD name, JOB activities and procedures as behaviour units, SORT activities as transform
shapes, REPORT sections as Reports the program produces, CALL and embedded SQL.

Modules: `reader` (statements and tokens), `vocabulary` (terms and constructs) and `source`
(the extractor). They share `repo_intel.platforms.pli.common` with the other W8.5 packs and
import the `repo_intel.extractors` package only inside functions.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.easytrieve.source import EasytrieveSource
from repo_intel.platforms.easytrieve.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="easytrieve",
    name="Easytrieve Plus (programs, files, jobs, sorts and reports)",
    prefix="ezt",
    version="1.1.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(EasytrieveSource(),),
)
