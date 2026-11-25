"""The web experience pack: what users see and do in a web front end.

Screens come from router configuration (React Router `<Route>` elements and route
objects, Vue Router, Angular `Routes`), file routes (Next.js `app/` and `pages/`,
Nuxt pages) and static HTML documents. Forms, controls, submit targets, navigation and
bindings come from HTML, JSX/TSX, Vue templates and Angular templates. Style tokens
come from CSS custom properties, SCSS and LESS variables and exported theme objects,
and screens are STYLED_BY the tokens their files reference. Literal roles in route
guards become Roles a screen REQUIRES_ROLE.

The pack adds only this experience layer. Endpoints, symbols, imports and HTTP calls
stay with `typescript.source`: a form that posts to a literal URL points at the
Endpoint key that extractor declares, and is kept only when the same unit declares it.
It supersedes nothing.

The vocabulary is in `vocabulary.py`, the scanners in `markup.py`, `script.py` and
`styles.py`, and the shared emission rules in `experience.py`.
"""

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.web.extractors import WebMarkup, WebScript, WebStyles
from repo_intel.platforms.web.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="web",
    name="Web front end (HTML, React, Vue, Angular, CSS)",
    prefix="web",
    version="1.2.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(WebMarkup(), WebScript(), WebStyles()),
)
