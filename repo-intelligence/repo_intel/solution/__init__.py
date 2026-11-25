"""The solution layer: one application spread over several repositories and inputs.

    manifest.py   `.rkp/solution.json` (schema `rkp-solution/1`): members and inputs
    loader.py     open each member's bundle without compiling; read the inputs
    view.py       a read-only view across members, entity IDs namespaced by member
    identity.py   exact identity rules (E3 links), candidates (E4) and unmatched references
    commands.py   what `rkp solution check` and `rkp solution links` report

Nothing here compiles a repository or changes a member's bundle.
"""

from repo_intel.solution.identity import Resolution, resolve
from repo_intel.solution.loader import Solution, load_solution
from repo_intel.solution.manifest import MANIFEST, SCHEMA, Member, SolutionError, SolutionManifest, load_manifest, parse_manifest
from repo_intel.solution.view import MemberData, SolutionView, nsid, split

__all__ = [
    "MANIFEST", "SCHEMA", "Member", "MemberData", "Resolution", "Solution", "SolutionError", "SolutionManifest", "SolutionView",
    "load_manifest", "load_solution", "nsid", "parse_manifest", "resolve", "split",
]
