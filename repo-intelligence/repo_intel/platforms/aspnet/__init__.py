"""The ASP.NET platform pack: Web Forms and Razor pages, and `web.config`.

`aspnet.source` reads `.aspx`, `.ascx` and `.master` (Web Forms) and `.cshtml` and
`.razor` (Razor views, Razor Pages and Blazor components): the page as a Program, its
URL, the screen it renders with forms and controls (server controls, HTML, tag helpers,
HTML helpers, Blazor inputs), code-behind, master pages, layouts, partials and user
controls, redirects and transfers, roles, `SqlDataSource` queries and the connection
strings and app settings it names. C# and VB code is not lowered: only the redirects,
transfers, navigation and role checks in it are read.

`aspnet.config` reads `web.config`: authorization rules as Permissions that Roles are
granted, and connection strings and app settings by name.

Modules: `razor` (where server code is in Razor and Web Forms markup), `vocabulary`,
`source` and `config` (the extractors). The forms, controls and emission helpers are
shared with the JSP pack (`repo_intel.platforms.jsp.pages`).
"""

from __future__ import annotations

from repo_intel.platforms import PlatformPack
from repo_intel.platforms.aspnet.config import AspNetConfig
from repo_intel.platforms.aspnet.source import AspNetSource
from repo_intel.platforms.aspnet.vocabulary import CONSTRUCTS, TERMS

PACK = PlatformPack(
    id="aspnet",
    name="ASP.NET (Web Forms, Razor, Blazor, web.config)",
    prefix="aspx",
    version="1.0.0",
    terms=TERMS,
    constructs=CONSTRUCTS,
    extractors=(AspNetSource(), AspNetConfig()),
)
