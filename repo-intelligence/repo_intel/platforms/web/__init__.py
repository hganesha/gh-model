"""The web experience pack: what users see and do in a web front end.

Screens come from router configuration (React Router `<Route>` elements and route
objects, Vue Router, Angular `Routes`), file routes (Next.js `app/` and `pages/`,
Nuxt pages) and static HTML documents. Forms, controls, submit targets, navigation and
bindings come from HTML, JSX/TSX, Vue templates and Angular templates. Style tokens
come from CSS custom properties, SCSS and LESS variables and exported theme objects,
and screens are STYLED_BY the tokens their files reference. Literal roles in route
guards become Roles a screen REQUIRES_ROLE.

Version 1.3.0 (section 8.5) adds: RENDERS from the component Symbol a file exports
(falling back to the package), stylesheet imports as Artifact DEPENDS_ON Artifact, token aliases as
StyleToken ALIAS_OF StyleToken, Fields (`web:ModelField`) from declared form schemas
(TypeScript interfaces and object types, Zod and Yup object schemas) that controls
BIND_TO, and accessibility facts on controls, screens and colour tokens
(`accessibility.py`, with the `accessibility` coverage entry it computes).

Version 1.4.0 puts `router` (`<path>#<label>`) on each Screen found under a router its
file shows, and `<app dir>#nextjs` or `<app dir>#nuxt` on file routes (a Next.js route's `app/` or
`pages/` directory moves to `route_dir`); the compiler adds
the `app` key part to the routed Screens of a unit holding more than one router
(`compiler._screen_apps`), replacing the earlier rule that looked at one file only.
The pack's `coverage` hook adds the `accessibility` domain to the repository's coverage,
and nothing under `.rkp/` (owner inputs such as saved DOMs) is read as a page.

The pack adds only this experience layer. Endpoints, symbols, imports and HTTP calls
stay with `typescript.source`: a form that posts to a literal URL points at the
Endpoint key that extractor declares, and is kept only when the same unit declares it.
It supersedes nothing.

The vocabulary is in `vocabulary.py`, the scanners in `markup.py`, `script.py` and
`styles.py`, form models in `models.py`, accessibility facts in `accessibility.py`,
and the shared emission rules in `experience.py`.
"""

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.web.accessibility import accessibility_coverage
from repo_intel.platforms.web.extractors import WebMarkup, WebScript, WebStyles
from repo_intel.platforms.web.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="web",
    name="Web front end (HTML, React, Vue, Angular, CSS)",
    prefix="web",
    version="1.4.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(WebMarkup(), WebScript(), WebStyles()),
    coverage=accessibility_coverage,
)
