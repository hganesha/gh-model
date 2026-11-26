"""The JSP pack's vocabulary: platform terms, tag libraries and the constructs it looks for.

Tag libraries are recognised by the URI a `taglib` directive (or a JSP document's
`xmlns:` attribute) maps a prefix to. A prefix used without a mapping in the file is
read by the prefix its library conventionally uses (`c` for JSTL core, `html` for
Struts), since pages commonly get their taglib directives from an included file; the
page's `taglibs_assumed` attribute says when that happened.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("jsp:Page", K.PROGRAM, "exact", "a JSP page, JSP document or fragment (.jsp, .jspx, .jspf): the servlet it compiles to; name is its path"),
    Term("jsp:TagFile", K.PROGRAM, "lossy", "a tag file (.tag, .tagx): a custom tag written in JSP; its attributes in attributes"),
    Term("jsp:View", K.SCREEN, "lossy", "a JSP page that renders, or a fragment or tag file with forms or links; name is its path, the URL it answers in `route`"),
    Term("jsp:Form", K.FORM, "lossy", "an HTML <form>, a Struts <html:form> or a Spring <form:form>"),
    Term("jsp:Control", K.UI_CONTROL, "lossy", "an input, select, text area, button or link, in HTML or a Struts or Spring form tag; binding path in attributes"),
    Term("jsp:PageRequest", K.ENDPOINT, "exact", "a JSP page reachable by URL (not under WEB-INF), at its path below the web root"),
    Term("jsp:Servlet", K.ENDPOINT, "lossy", "a servlet mapping's URL pattern in web.xml; the servlet's name and class in attributes"),
    Term("jsp:Role", K.ROLE, "exact", "a role named by web.xml, request.isUserInRole, a Spring Security or Struts role check; realm 'jsp'"),
    Term("jsp:SecurityConstraint", K.PERMISSION, "lossy", "a web.xml security constraint: access to URL patterns (and methods) that roles are granted"),
    Term("jsp:DataSource", K.CONFIG_KEY, "lossy", "a JNDI data source named by a sql: tag or a web.xml resource-ref; connection details are never read"),
)

# Tag libraries by the URI a taglib directive maps them from (matched by suffix), and the
# prefix each conventionally uses.
LIBRARY_URIS = (
    ("jsp/jstl/core", "c"), ("jstl/core", "c"), ("jakarta.tags.core", "c"),
    ("jsp/jstl/fmt", "fmt"), ("jstl/fmt", "fmt"), ("jakarta.tags.fmt", "fmt"),
    ("jsp/jstl/sql", "sql"), ("jstl/sql", "sql"), ("jakarta.tags.sql", "sql"),
    ("jsp/jstl/functions", "fn"), ("jakarta.tags.functions", "fn"),
    ("jsp/jstl/xml", "x"), ("jstl/xml", "x"), ("jakarta.tags.xml", "x"),
    ("tags-html", "html"), ("struts-html", "html"), ("tags-bean", "bean"), ("struts-bean", "bean"),
    ("tags-logic", "logic"), ("struts-logic", "logic"), ("tags-tiles", "tiles"), ("struts-tiles", "tiles"), ("tiles.apache.org/tags-tiles", "tiles"),
    ("springframework.org/tags/form", "form"), ("springframework.org/tags", "spring"), ("springframework.org/security/tags", "sec"),
    ("struts-tags", "s2"), ("java.sun.com/jsf/html", "jsf"), ("java.sun.com/jsf/core", "jsf"), ("xmlns.jcp.org/jsf/html", "jsf"), ("xmlns.jcp.org/jsf/core", "jsf"),
    ("java.sun.com/jsp/page", "jsp"), ("java.sun.com/jsp/jstl", "c"),
)
CONVENTIONAL_PREFIXES = {"c": "c", "fmt": "fmt", "sql": "sql", "fn": "fn", "x": "x", "html": "html", "bean": "bean", "logic": "logic", "tiles": "tiles",
                         "form": "form", "spring": "spring", "sec": "sec", "s": "s2", "h": "jsf", "f": "jsf", "jsp": "jsp"}

# library -> {tag (lower case): (canonical name, description, modelled_as)}
_TAGS: dict[str, list[tuple[str, str, str]]] = {
    "c": [
        ("out", "write an expression, escaped", "behavior:output"), ("set", "set a scoped variable or bean property", "behavior:assign"),
        ("remove", "remove a scoped variable", "behavior:assign"), ("if", "conditional", "behavior:if"), ("choose", "choice among c:when", "behavior:if"),
        ("when", "branch of c:choose", "behavior:if"), ("otherwise", "fallback branch of c:choose", "behavior:if"),
        ("forEach", "loop over items or a range", "behavior:loop"), ("forTokens", "loop over delimited tokens", "behavior:loop"),
        ("import", "include a URL's content", "behavior:include"), ("url", "build a context-relative URL", "behavior:assign"),
        ("param", "URL parameter of c:url, c:import or c:redirect", "behavior:call"), ("redirect", "redirect the response", "behavior:redirect"),
        ("catch", "catch exceptions of its body", "behavior:try"),
    ],
    "fmt": [
        ("message", "localised message by key", "behavior:output"), ("formatNumber", "format a number", "behavior:output"),
        ("formatDate", "format a date", "behavior:output"), ("parseNumber", "parse a number", "behavior:assign"), ("parseDate", "parse a date", "behavior:assign"),
        ("param", "message parameter", "behavior:call"), ("setBundle", "set the resource bundle", "unmapped"), ("bundle", "resource bundle for the body", "unmapped"),
        ("setLocale", "set the locale", "unmapped"), ("timeZone", "time zone for the body", "unmapped"), ("setTimeZone", "set the time zone", "unmapped"),
        ("requestEncoding", "set the request encoding", "unmapped"),
    ],
    "sql": [
        ("query", "SQL query; tables through lineage", "behavior:query"), ("update", "SQL update; tables through lineage", "behavior:query"),
        ("param", "bound SQL parameter", "behavior:query"), ("dateParam", "bound SQL date parameter", "behavior:query"),
        ("transaction", "database transaction", "behavior:transaction"), ("setDataSource", "data source by JNDI name; URL, user and password are never read", "entity:ConfigKey"),
    ],
    "jsp": [
        ("include", "include a page at request time", "relation:CALLS"), ("forward", "forward the request to another page", "relation:NAVIGATES_TO"),
        ("param", "parameter of jsp:include or jsp:forward", "behavior:call"), ("useBean", "declare or create a bean", "behavior:declare"),
        ("setProperty", "set a bean property", "behavior:assign"), ("getProperty", "write a bean property", "behavior:output"),
        ("doBody", "run a tag file's body", "behavior:platform"), ("invoke", "run a tag file's fragment attribute", "behavior:platform"),
        ("plugin", "embed an applet", "unmapped"), ("params", "parameters of jsp:plugin", "unmapped"), ("fallback", "fallback of jsp:plugin", "unmapped"),
        ("element", "an element built at run time", "unmapped"), ("attribute", "an attribute given as a body", "unmapped"), ("body", "an element body", "unmapped"),
        ("text", "template text in a JSP document", "unmapped"), ("output", "XML declaration control in a JSP document", "unmapped"), ("root", "root of a JSP document", "unmapped"),
    ],
    "html": [
        ("form", "Struts form", "entity:Form"), ("text", "Struts text input", "entity:UIControl"), ("password", "Struts password input", "entity:UIControl"),
        ("textarea", "Struts text area", "entity:UIControl"), ("select", "Struts select", "entity:UIControl"), ("checkbox", "Struts check box", "entity:UIControl"),
        ("multibox", "Struts multiple check box", "entity:UIControl"), ("radio", "Struts radio button", "entity:UIControl"), ("hidden", "Struts hidden field", "entity:UIControl"),
        ("file", "Struts file input", "entity:UIControl"), ("submit", "Struts submit button", "entity:UIControl"), ("button", "Struts button", "entity:UIControl"),
        ("cancel", "Struts cancel button", "entity:UIControl"), ("reset", "Struts reset button", "entity:UIControl"), ("image", "Struts image button", "entity:UIControl"),
        ("link", "Struts link", "entity:UIControl"), ("option", "Struts option", "unmapped"), ("options", "Struts options", "unmapped"),
        ("optionsCollection", "Struts options from a collection", "unmapped"), ("errors", "Struts validation errors", "unmapped"), ("messages", "Struts messages", "unmapped"),
        ("html", "Struts html element", "unmapped"), ("base", "Struts base element", "unmapped"), ("rewrite", "Struts URL rewrite", "unmapped"),
        ("img", "Struts image", "unmapped"), ("javascript", "Struts validator script", "unmapped"), ("frame", "Struts frame", "unmapped"), ("xhtml", "Struts XHTML mode", "unmapped"),
    ],
    "bean": [
        ("write", "write a bean property", "behavior:output"), ("message", "localised message", "behavior:output"), ("define", "define a scripting variable", "behavior:assign"),
        ("size", "collection size into a variable", "behavior:assign"), ("include", "include a resource's content", "unmapped"), ("resource", "load a web resource", "unmapped"),
        ("cookie", "cookie into a variable", "unmapped"), ("header", "header into a variable", "unmapped"), ("parameter", "parameter into a variable", "unmapped"),
        ("page", "page object into a variable", "unmapped"), ("struts", "Struts object into a variable", "unmapped"),
    ],
    "logic": [
        ("iterate", "loop over a collection", "behavior:loop"), ("equal", "conditional: equal", "behavior:if"), ("notEqual", "conditional: not equal", "behavior:if"),
        ("greaterThan", "conditional", "behavior:if"), ("lessThan", "conditional", "behavior:if"), ("greaterEqual", "conditional", "behavior:if"),
        ("lessEqual", "conditional", "behavior:if"), ("present", "conditional: present (or the user has a role)", "behavior:if"),
        ("notPresent", "conditional: absent", "behavior:if"), ("empty", "conditional: empty", "behavior:if"), ("notEmpty", "conditional: not empty", "behavior:if"),
        ("match", "conditional: substring match", "behavior:if"), ("notMatch", "conditional: no substring match", "behavior:if"),
        ("messagesPresent", "conditional: messages present", "behavior:if"), ("messagesNotPresent", "conditional: no messages", "behavior:if"),
        ("redirect", "redirect the response", "behavior:redirect"), ("forward", "forward to a global forward", "unmapped"),
    ],
    "form": [
        ("form", "Spring form bound to a model attribute", "entity:Form"), ("input", "Spring input", "entity:UIControl"), ("password", "Spring password input", "entity:UIControl"),
        ("hidden", "Spring hidden field", "entity:UIControl"), ("textarea", "Spring text area", "entity:UIControl"), ("select", "Spring select", "entity:UIControl"),
        ("checkbox", "Spring check box", "entity:UIControl"), ("checkboxes", "Spring check boxes", "entity:UIControl"), ("radiobutton", "Spring radio button", "entity:UIControl"),
        ("radiobuttons", "Spring radio buttons", "entity:UIControl"), ("button", "Spring button", "entity:UIControl"), ("option", "Spring option", "unmapped"),
        ("options", "Spring options", "unmapped"), ("errors", "Spring binding errors", "unmapped"), ("label", "Spring label", "unmapped"),
    ],
    "spring": [
        ("message", "localised message", "behavior:output"), ("url", "build a URL", "behavior:assign"), ("param", "URL parameter", "behavior:call"),
        ("eval", "evaluate a SpEL expression", "behavior:output"), ("bind", "bind status of a property", "unmapped"), ("hasBindErrors", "conditional on binding errors", "unmapped"),
        ("escapeBody", "escape the body", "unmapped"), ("htmlEscape", "default HTML escaping", "unmapped"), ("nestedPath", "nested bind path", "unmapped"),
        ("theme", "theme message", "unmapped"), ("transform", "transform with a property editor", "unmapped"), ("argument", "message argument", "unmapped"),
    ],
    "sec": [
        ("authorize", "Spring Security: render the body for some roles", "entity:Role"), ("authentication", "write a property of the logged-in user", "behavior:output"),
        ("csrfInput", "CSRF token field", "unmapped"), ("csrfMetaTags", "CSRF meta tags", "unmapped"), ("accesscontrollist", "ACL check", "unmapped"),
    ],
}
TAGS: dict[str, dict[str, tuple[str, str]]] = {lib: {name.lower(): (f"jsp:{lib}.{name}", modelled) for name, _, modelled in tags} for lib, tags in _TAGS.items()}
# Libraries counted as a whole, not tag by tag: none of their tags is modelled yet.
WHOLE_LIBRARIES = {"x": "jsp:x-tag", "s2": "jsp:struts2-tag", "jsf": "jsp:jsf-tag", "tiles": "jsp:tiles-tag"}
MARKUP = {"form": "jsp:markup.form", "input": "jsp:markup.input", "select": "jsp:markup.select", "textarea": "jsp:markup.textarea", "button": "jsp:markup.button", "a": "jsp:markup.link"}

_OTHER = (
    ("directive.page", "page directive: imports, content type, error page, session", "attribute:directives"),
    ("directive.include", "static include of another file", "relation:CALLS"),
    ("directive.taglib", "tag library declaration", "attribute:taglibs"),
    ("directive.tag", "tag file directive", "attribute:directives"),
    ("directive.attribute", "tag file attribute declaration", "attribute:tag_attributes"),
    ("directive.variable", "tag file variable declaration", "attribute:directives"),
    ("scriptlet", "Java code block <% %>: kept as a platform node; redirects, forwards and role checks in it are read", "behavior:platform"),
    ("expression", "Java expression <%= %> written to the response", "behavior:output"),
    ("declaration", "Java declaration <%! %>: fields and methods of the page", "behavior:platform"),
    ("el", "an expression language expression ${...} or #{...}", "behavior:output"),
    ("send-redirect", "response.sendRedirect in Java code", "behavior:redirect"),
    ("request-dispatcher", "forward or include through a RequestDispatcher or pageContext", "behavior:include"),
    ("is-user-in-role", "request.isUserInRole with a literal role", "entity:Role"),
    ("custom-tag", "a custom tag from a tag directory (tag file)", "relation:CALLS"),
    ("tld-tag", "a custom tag from a tag library descriptor this pack does not know", "unmapped"),
    ("unknown-tag", "a tag of a known library that this pack does not know", "unmapped"),
    ("x-tag", "a JSTL XML tag", "unmapped"),
    ("struts2-tag", "a Struts 2 tag", "unmapped"),
    ("jsf-tag", "a JavaServer Faces tag in a JSP", "unmapped"),
    ("tiles-tag", "a Tiles layout tag", "unmapped"),
    ("markup.form", "HTML form", "entity:Form"),
    ("markup.input", "HTML input", "entity:UIControl"),
    ("markup.select", "HTML select", "entity:UIControl"),
    ("markup.textarea", "HTML text area", "entity:UIControl"),
    ("markup.button", "HTML button", "entity:UIControl"),
    ("markup.link", "HTML link; NAVIGATES_TO when its target is a literal page or URL", "entity:UIControl"),
    # web.xml
    ("web.servlet-mapping", "servlet mapping: an Endpoint per URL pattern", "entity:Endpoint"),
    ("web.servlet", "servlet declaration: name, class or JSP file", "attribute:servlet"),
    ("web.security-constraint", "security constraint on URL patterns", "entity:Permission"),
    ("web.security-role", "declared security role", "entity:Role"),
    ("web.resource-ref", "JNDI resource reference (data sources): the name only", "entity:ConfigKey"),
    ("web.login-config", "authentication method and login pages", "unmapped"),
    ("web.filter", "servlet filter declaration", "unmapped"),
    ("web.filter-mapping", "servlet filter mapping", "unmapped"),
    ("web.listener", "application or session listener", "unmapped"),
    ("web.context-param", "context parameter (values are never read)", "unmapped"),
    ("web.env-entry", "environment entry (values are never read)", "unmapped"),
    ("web.error-page", "error page by status or exception", "unmapped"),
    ("web.welcome-file", "welcome file", "unmapped"),
    ("web.session-config", "session configuration", "unmapped"),
    ("web.ejb-ref", "EJB reference", "unmapped"),
    ("web.jsp-config", "JSP configuration (tag libraries, property groups)", "unmapped"),
    ("web.mime-mapping", "MIME type mapping", "unmapped"),
)


def _constructs() -> tuple[Construct, ...]:
    out = []
    descriptions = {lib: {name.lower(): text for name, text, _ in tags} for lib, tags in _TAGS.items()}
    for lib, tags in TAGS.items():
        for low, (cid, modelled) in tags.items():
            out.append(Construct(cid, descriptions[lib][low], modelled))
    out += [Construct(f"jsp:{name}", text, modelled) for name, text, modelled in _OTHER]
    return tuple(out)


CONSTRUCTS = _constructs()
