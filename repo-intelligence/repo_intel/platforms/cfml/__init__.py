"""The ColdFusion (CFML) platform pack.

`cfml.source` reads `.cfc` components and `.cfm` templates in both tag and script
syntax; `cfml.admin` reads ColdFusion Administrator exports committed under
`.rkp/inputs/cfml/`. Together they replace `legacy.cfml`.

Modules: `scan` (strings, `#...#` and comments), `markup` (the tag tree), `script`
(the CFScript and expression parser), `ir` (parse context and tags shared by both
syntaxes), `lower` (tag syntax into the behavioural IR), `source` and `admin` (the
extractors). None of them imports the `repo_intel.extractors` package at module
level: pack discovery runs while that package loads.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, PlatformPack, Term
from repo_intel.platforms.cfml.admin import CfmlAdmin
from repo_intel.platforms.cfml.ir import COUNTED_SCOPES, LIFECYCLE, PLATFORM_TAGS, UNMAPPED_TAGS
from repo_intel.platforms.cfml.source import CfmlSource

TERMS = (
    Term("cf:Component", K.PROGRAM, "lossy", "a .cfc component (or interface); named by its dotted path as legacy.cfml named it"),
    Term("cf:Page", K.PROGRAM, "exact", "a .cfm template as code: the program a request runs"),
    Term("cf:Function", K.SYMBOL, "lossy", "a function of a component or template (cffunction or script); access, roles and output in attributes"),
    Term("cf:RemoteMethod", K.ENDPOINT, "exact", "a remote function, reachable at /<component path>?method=<name>"),
    Term("cf:PageRequest", K.ENDPOINT, "exact", "a template reachable by URL"),
    Term("cf:Template", K.SCREEN, "lossy", "a .cfm template that renders output; markup semantics live only in its behaviour"),
    Term("cf:Form", K.FORM, "exact", "an HTML <form> or <cfform> on a template"),
    Term("cf:Input", K.UI_CONTROL, "lossy", "an input, select, text area, button or link; cfinput validation in attributes"),
    Term("cf:Role", K.ROLE, "exact", "a role named by roles=, IsUserInRole or cfloginuser"),
    Term("cf:Datasource", K.CONFIG_KEY, "lossy", "a named datasource; connection details are never read"),
    Term("cf:ScheduleGroup", K.JOB, "lossy", "a scheduler group of tasks (cfschedule or neo-cron.xml)"),
    Term("cf:ScheduledTask", K.TASK, "lossy", "a scheduled task that requests a URL"),
)

# Constructs modelled the same way in both syntaxes, by tag name without "cf".
_TAGS: dict[str, tuple[str, str]] = {
    "set": ("variable assignment", "behavior:assign"),
    "if": ("conditional", "behavior:if"),
    "elseif": ("conditional branch", "behavior:if"),
    "else": ("conditional fallback branch", "behavior:if"),
    "loop": ("loop: index, condition, query, array, collection, list or file", "behavior:loop"),
    "output": ("output block, evaluating #expressions#", "behavior:output"),
    "query": ("SQL query; tables through lineage", "behavior:query"),
    "queryparam": ("bound SQL parameter", "behavior:query"),
    "script": ("CFScript block", "behavior:statements"),
    "try": ("exception handling block", "behavior:try"),
    "catch": ("exception handler", "behavior:try"),
    "finally": ("finally block", "behavior:try"),
    "switch": ("switch", "behavior:switch"),
    "case": ("switch case", "behavior:switch"),
    "defaultcase": ("switch default case", "behavior:switch"),
    "function": ("function definition", "entity:Symbol"),
    "argument": ("function parameter", "attribute:params"),
    "component": ("tag component", "entity:Program"),
    "interface": ("tag interface", "entity:Program"),
    "return": ("return", "behavior:return"),
    "throw": ("raise an exception", "behavior:throw"),
    "rethrow": ("re-raise the caught exception", "behavior:throw"),
    "break": ("leave a loop or switch", "behavior:break"),
    "continue": ("next loop iteration", "behavior:continue"),
    "property": ("component property", "attribute:properties"),
    "mail": ("send mail; addresses are not copied", "behavior:output"),
    "mailpart": ("part of a mail body", "behavior:output"),
    "location": ("redirect", "behavior:redirect"),
    "include": ("include a template", "relation:CALLS"),
    "module": ("call a template as a custom tag", "relation:CALLS"),
    "abort": ("stop the request", "behavior:exit"),
    "exit": ("leave a custom tag or template", "behavior:exit"),
    "lock": ("named or scope lock", "behavior:lock"),
    "transaction": ("database transaction", "behavior:transaction"),
    "param": ("declare a variable with a default and type", "behavior:declare"),
    "dump": ("debug dump to the response", "behavior:output"),
    "log": ("write a log entry", "behavior:output"),
    "file": ("file operation; write and append are output", "behavior:output"),
    "http": ("outbound HTTP request", "behavior:call"),
    "httpparam": ("outbound HTTP request parameter", "behavior:call"),
    "invoke": ("call a component method", "relation:CALLS"),
    "invokeargument": ("argument of cfinvoke", "behavior:call"),
    "object": ("instantiate a component or object", "relation:CALLS"),
    "schedule": ("register a scheduled task", "entity:Task"),
    "loginuser": ("log a user in with roles", "entity:Role"),
    "insert": ("insert a form into a table", "relation:WRITES_TO"),
    "update": ("update a table from a form", "relation:WRITES_TO"),
    "application": ("application settings (tag form)", "attribute:application"),
    "form": ("ColdFusion form", "entity:Form"),
    "input": ("ColdFusion form input with validation", "entity:UIControl"),
    "select": ("ColdFusion select", "entity:UIControl"),
    "textarea": ("ColdFusion text area", "entity:UIControl"),
    "savecontent": ("capture output into a variable", "behavior:platform"),
    "silent": ("suppress output", "behavior:platform"),
}
_PLATFORM_DESCRIPTIONS = {
    "thread": "run code in a thread", "document": "render a PDF or FlashPaper document", "documentitem": "header or footer of a document",
    "documentsection": "section of a document", "pdf": "PDF manipulation", "spreadsheet": "spreadsheet read or write", "ftp": "FTP operation",
    "directory": "directory listing or change", "execute": "run an operating system command", "registry": "Windows registry access",
    "login": "login block", "logout": "log the user out", "header": "set an HTTP response header", "content": "set content type or send a file",
    "cookie": "set a cookie", "setting": "request settings", "flush": "flush the response", "processingdirective": "page encoding and whitespace",
    "xml": "build an XML document", "wddx": "WDDX serialisation", "image": "image manipulation", "zip": "zip archive operation",
    "cache": "page or fragment cache", "ldap": "LDAP query", "pop": "read mail over POP", "imap": "read mail over IMAP",
    "search": "full-text search", "index": "full-text index update", "collection": "full-text collection management",
    "ntauthenticate": "Windows authentication", "dbinfo": "database metadata", "timer": "timed block", "associate": "associate a custom tag with its parent",
    "storedproc": "stored procedure call", "procparam": "stored procedure parameter", "procresult": "stored procedure result set",
    "htmlhead": "write into the page head", "trace": "debug trace", "mailparam": "mail header or attachment", "pdfparam": "PDF merge parameter",
    "zipparam": "zip entry parameter",
}
_UNMAPPED_DESCRIPTIONS = {
    "ajaxproxy": "JavaScript proxy for a component", "websocket": "WebSocket channel", "grid": "Ajax data grid", "gridcolumn": "grid column",
    "layout": "Ajax layout", "layoutarea": "Ajax layout area", "div": "Ajax div", "window": "Ajax window", "pod": "Ajax pod",
    "report": "Report Builder report", "reportparam": "report parameter", "chart": "chart", "chartseries": "chart series", "chartdata": "chart data point",
    "import": "import a tag library or package", "menu": "Ajax menu", "menuitem": "menu item", "tree": "Ajax tree", "treeitem": "tree item",
    "tooltip": "tooltip", "slider": "slider control",
}
_SCRIPT = (
    ("cf:script-component", "script component (component { ... })", "entity:Program"),
    ("cf:script-function", "script function", "entity:Symbol"),
    ("cf:remote-function", "remote function exposed as an endpoint", "entity:Endpoint"),
    ("cf:closure", "closure or arrow function", "behavior:lambda"),
    ("cf:var", "var declaration of a function local", "behavior:declare"),
    ("cf:increment", "increment or decrement statement", "behavior:assign"),
    ("cf:writeOutput", "writeOutput or echo", "behavior:output"),
    ("cf:queryExecute", "queryExecute; tables through lineage", "behavior:query"),
    ("cf:createObject", "createObject of a component", "relation:CALLS"),
    ("cf:new", "new of a component", "relation:CALLS"),
    ("cf:invoke", "invoke() of a component method", "behavior:call"),
    ("cf:isUserInRole", "IsUserInRole or IsUserInAnyRole check", "entity:Role"),
    ("cf:getAuthUser", "read the logged-in user", "behavior:call"),
    ("cf:orm-function", "ORM entity function (EntityLoad, EntitySave, ...)", "behavior:call"),
    ("cf:persistent-component", "ORM persistent component mapped to a table", "entity:Table"),
    ("cf:application-setting", "Application.cfc this.* setting (names only for paths and credentials)", "attribute:application"),
    ("cf:custom-tag", "custom tag call (<cf_name>)", "relation:CALLS"),
    ("cf:evaluate", "Evaluate, IIf, SetVariable or DE: code built from strings", "unmapped"),
    ("cf:unparsed", "code the parser could not read", "unmapped"),
    ("cf:unknown-tag", "a cf tag this pack does not know", "unmapped"),
    ("cf:neo-datasource", "Administrator datasource (neo-datasource.xml)", "entity:ConfigKey"),
    ("cf:neo-cron-task", "Administrator scheduled task (neo-cron.xml)", "entity:Task"),
    ("cf:html-form", "HTML form", "entity:Form"),
    ("cf:html-input", "HTML input", "entity:UIControl"),
    ("cf:html-select", "HTML select", "entity:UIControl"),
    ("cf:html-textarea", "HTML text area", "entity:UIControl"),
    ("cf:html-button", "HTML button", "entity:UIControl"),
    ("cf:html-link", "HTML link to a literal page", "relation:NAVIGATES_TO"),
)


def _constructs() -> tuple[Construct, ...]:
    out = [Construct(f"cf:cf{name}", description, modelled) for name, (description, modelled) in _TAGS.items()]
    for name in sorted(PLATFORM_TAGS - set(_TAGS)):
        out.append(Construct(f"cf:cf{name}", _PLATFORM_DESCRIPTIONS[name], "behavior:platform"))
    for name in sorted(UNMAPPED_TAGS):
        out.append(Construct(f"cf:cf{name}", _UNMAPPED_DESCRIPTIONS[name], "unmapped"))
    out += [Construct(cid, description, modelled) for cid, description, modelled in _SCRIPT]
    out += [Construct(f"cf:scope.{scope}", f"use of the {scope} scope", "behavior:var") for scope in sorted(COUNTED_SCOPES)]
    out += [Construct(f"cf:{event}", f"Application.cfc {event} handler", "attribute:lifecycle_event") for event in sorted(LIFECYCLE.values())]
    return tuple(out)


PACK = PlatformPack(
    id="cfml",
    name="ColdFusion (CFML)",
    prefix="cf",
    version="1.1.0",
    terms=TERMS,
    constructs=_constructs(),
    extractors=(CfmlSource(), CfmlAdmin()),
    supersedes=("legacy.cfml",),
    inputs=(".rkp/inputs/cfml/**",),
)
