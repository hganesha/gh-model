"""The JSP platform pack (JavaServer Pages and the servlet deployment descriptor).

`jsp.source` reads JSP pages, documents, fragments and tag files: the page as a
Program, its URL, the screen it renders with forms and controls (HTML, Struts
`html:` and Spring `form:` tags), includes, redirects and forwards, role checks, JSTL
`sql:` queries, and a behaviour unit lowered from JSTL, the standard actions and EL.
`jsp.webxml` reads `WEB-INF/web.xml`: servlet mappings as Endpoints and security
constraints as Permissions that Roles are granted.

Modules: `pages` (what this pack and the ASP.NET pack share: server-code masking,
emission and the contract's control naming), `el` (the Expression Language parser),
`vocabulary` (terms, tag libraries and constructs), `source` and `webxml` (the
extractors). None of them imports the `repo_intel.extractors` package at module
level: pack discovery runs while that package loads.
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.jsp.source import JspSource
from repo_intel.platforms.jsp.vocabulary import CONSTRUCTS, TERMS
from repo_intel.platforms.jsp.webxml import JspWebXml

PACK = PlatformPack(
    id="jsp",
    name="JavaServer Pages (JSP, JSTL, Struts and Spring tags, web.xml)",
    prefix="jsp",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(JspSource(), JspWebXml()),
)
