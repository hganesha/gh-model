"""The IBM i platform pack (docs/legacy-platform-packs-plan.md, wave W8; contract section 11).

Reads RPG III and RPG IV source (`.rpg`, `.rpgle`, `.sqlrpgle`: fixed form by column, free
form, fully free `**FREE` and mixed, with embedded SQL), CL (`.clp`, `.clle`) and DDS
(`.pf`, `.lf`, `.dspf`, `.prtf`). RPG and CL members become Programs with behaviour units
for their main procedure, subroutines and procedures; physical files become Tables and
Fields, logical files DataSchemas, display files Screens, Forms and UIControls, printer
files Reports; F-specs, DCLF and embedded SQL become reads and writes; CALL, CALLP,
SBMJOB and bound calls become CALLS; OVRDBF and data areas are recorded as attributes.

Modules: `rpglex` (RPG tokens and expressions), `rpg` (the RPG reader and its lowering),
`sql` (embedded SQL statements), `cl` (the CL reader), `dds` (the DDS reader),
`vocabulary` (terms and constructs) and `source` (the extractors). None of them imports
the `repo_intel.extractors` package at module level: pack discovery runs while that
package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.ibmi.source import IbmiCl, IbmiDds, IbmiRpg
from repo_intel.platforms.ibmi.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="ibmi",
    name="IBM i (RPG III and IV, CL, DDS physical, logical, display and printer files)",
    prefix="ibmi",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(IbmiRpg(), IbmiCl(), IbmiDds()),
)
