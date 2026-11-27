# Order desk (ASP.NET)

Three ASP.NET front ends with no project files: `desk/`, a Web Forms site (pages, a
master page, a user control, server controls with validators, a SqlDataSource and
`Web.config` authorization), `kiosk/`, a second Web Forms site with its own
`Web.config` and a page at the same route as the desk's, and `portal/`, a Razor Pages
and Blazor site (`@page` routes, tag helpers, `[Authorize(Roles = ...)]`, partials and a
layout).

`desk/Admin/Web.config` only sets who may read the Admin folder, so it does not start
an application of its own: the desk's pages are all served below `desk/`.
