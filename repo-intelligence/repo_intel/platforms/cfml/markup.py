"""CFML tag syntax: a tree of CFML tags, the HTML elements that make up forms and links, and text.

One forward pass over the file. `<!--- --->` comments (which nest) are skipped;
`<cfscript>` bodies are kept as raw regions for the script parser. A CFML tag that
may or may not have a body (`cfinvoke`, `cftransaction`, a custom tag) is opened
when it is seen and, if no closing tag follows before its parent closes, it is
taken to have had no body: its children move up to its parent. Tags that never
have a body (`cfset`, `cfinclude`) are leaves.

HTML is not built into a tree: browsers forgive too much for that to mean
anything. The elements that matter here (forms, inputs, selects, text areas,
buttons, links, the title) are recorded as leaves and, in document order, as UI
events, so a later pass can tell which controls sit inside which form even when
`<form>` opens inside one `cfif` and closes outside it. HTML inside
`<!-- -->`, `<script>` and `<style>` is skipped; CFML tags there still run, so
they are parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repo_intel.platforms.cfml.scan import Part, cf_comment_end, scan_string

# Tags whose bodies are always closed by an end tag.
REQUIRED_BODY = frozenset({
    "cfif", "cfloop", "cfquery", "cfoutput", "cffunction", "cfcomponent", "cfinterface", "cfscript", "cftry", "cfcatch", "cffinally",
    "cfswitch", "cfcase", "cfdefaultcase", "cfsavecontent", "cflock", "cfmail", "cfform", "cftextarea", "cflogin", "cfsilent", "cfstoredproc",
    "cfdocument", "cfdocumentsection", "cfdocumentitem", "cfchart", "cfxml", "cftimer", "cflayout", "cflayoutarea", "cfdiv", "cfwindow",
    "cfpod", "cfmenu", "cftree", "cftooltip", "cfmailpart",
})
# Tags that never have a body.
NEVER_BODY = frozenset({
    "cfset", "cfparam", "cfreturn", "cfargument", "cfinclude", "cflocation", "cfabort", "cfexit", "cfelse", "cfelseif", "cfbreak", "cfcontinue",
    "cfthrow", "cfrethrow", "cfdump", "cflog", "cfheader", "cfcookie", "cfcontent", "cfsetting", "cfflush", "cfimport", "cfinvokeargument",
    "cfmailparam", "cfhttpparam", "cfprocparam", "cfprocresult", "cfqueryparam", "cfloginuser", "cflogout", "cfinput", "cfschedule",
    "cfdirectory", "cfobject", "cfregistry", "cfapplication", "cfproperty", "cffile", "cfftp", "cfwddx", "cfassociate", "cfdbinfo", "cfimage",
    "cfajaxproxy", "cfwebsocket", "cfspreadsheet", "cfgridcolumn", "cfslider", "cfhtmlhead", "cfcollection", "cfindex", "cfsearch", "cfldap",
    "cfpop", "cfimap", "cfntauthenticate", "cfreportparam", "cfpdfparam", "cfzipparam", "cfchartdata", "cftrace", "cfinsert", "cfupdate",
})
# Tags whose single "attribute" is a CFML expression up to the closing `>`.
EXPRESSION_TAGS = frozenset({"cfset", "cfif", "cfelseif", "cfreturn"})
RAW_BODY = frozenset({"cfscript"})
HTML_UI = frozenset({"form", "input", "select", "textarea", "button", "a"})

_TAG = re.compile(r"<!---|<!--|<(/?)(cf[A-Za-z_][\w.:-]*|form|input|select|textarea|button|a|script|style|title)(?=[\s/>])", re.I)
_ATTR_NAME = re.compile(r"[^\s=>/\"']+")
_UNQUOTED = re.compile(r"[^\s>]+")
_WS = re.compile(r"\s*")
_EXPR_STOP = re.compile(r"[\"'>]")


@dataclass
class Attr:
    name: str  # lower-case
    start: int  # value span, quotes included; equal to the name's end when the attribute has no value
    end: int
    quoted: bool
    parts: list[Part]  # literal and `#expression#` parts of the value
    raw: str  # the value as written, without quotes

    def literal(self) -> str | None:
        """The value when it holds no `#expression#`, else None."""
        if any(p[0] == "expr" for p in self.parts):
            return None
        return "".join(p[3] or "" for p in self.parts)


@dataclass
class Node:
    kind: str  # "root", "tag", "html", "text"
    name: str  # lower-case tag name; "#text" for text
    start: int
    end: int  # end of the whole element (its end tag when it has one)
    open_end: int = 0  # end of the start tag
    attrs: dict[str, Attr] = field(default_factory=dict)
    expr: tuple[int, int] | None = None  # the expression of an expression tag
    children: list["Node"] = field(default_factory=list)
    body: tuple[int, int] | None = None  # raw body (cfscript), or the span between start and end tags
    closed: bool = False
    in_output: bool = False  # inside a cfoutput, where `#...#` is evaluated

    def attr(self, name: str) -> Attr | None:
        return self.attrs.get(name)

    def literal(self, name: str) -> str | None:
        a = self.attrs.get(name)
        return a.literal() if a else None


@dataclass
class UIEvent:
    kind: str  # "form_open", "form_close", "control"
    node: Node | None
    pos: int


@dataclass
class Document:
    root: Node
    ui: list[UIEvent]
    title: tuple[int, int] | None
    problems: list[str]
    has_document: bool  # an <html> or <body>, or a <title>: a page rather than a fragment


def _parse_attrs(text: str, i: int, end: int, hashes: bool) -> tuple[dict[str, Attr], int, bool]:
    """Attributes from `i` to the tag's `>`. Returns (attributes, index after `>`, self-closed)."""
    attrs: dict[str, Attr] = {}
    j = i
    while j < end:
        j = _WS.match(text, j, end).end()
        if j >= end:
            break
        c = text[j]
        if c == ">":
            return attrs, j + 1, False
        if text.startswith("/>", j):
            return attrs, j + 2, True
        m = _ATTR_NAME.match(text, j, end)
        if m is None:
            j += 1  # a stray quote or "/": skip it
            continue
        name = m.group().lower()
        j = m.end()
        k = _WS.match(text, j, end).end()
        if k < end and text[k] == "=":
            k = _WS.match(text, k + 1, end).end()
            if k < end and text[k] in "\"'":
                close, parts, _ = scan_string(text, k, end, hashes)
                attrs.setdefault(name, Attr(name, k, close, True, parts, text[k + 1: max(k + 1, close - 1)]))
                j = close
            else:
                u = _UNQUOTED.match(text, k, end)
                stop = u.end() if u else k
                if stop > k and text[stop - 1] == "/" and text.startswith(">", stop):
                    stop -= 1
                raw = text[k:stop]
                parts = _unquoted_parts(text, k, stop) if hashes else [("lit", k, stop, raw)]
                attrs.setdefault(name, Attr(name, k, stop, False, parts, raw))
                j = stop
        else:
            attrs.setdefault(name, Attr(name, j, j, False, [], ""))
    return attrs, end, False


def _unquoted_parts(text: str, a: int, b: int) -> list[Part]:
    from repo_intel.platforms.cfml.scan import hash_parts

    return hash_parts(text, a, b) or [("lit", a, b, "")]


def _expression_end(text: str, i: int, end: int) -> int:
    """Index of the `>` that closes an expression tag; quoted strings are skipped whole."""
    j = i
    while j < end:
        m = _EXPR_STOP.search(text, j, end)
        if m is None:
            return end
        k = m.start()
        if text[k] == ">":
            return k
        k, _, ok = scan_string(text, k, end, True)
        if not ok:
            return end
        j = k
    return end


def parse(text: str) -> Document:
    end = len(text)
    root = Node("root", "#root", 0, end, open_end=0)
    stack: list[Node] = [root]
    ui: list[UIEvent] = []
    problems: list[str] = []
    title: tuple[int, int] | None = None
    html_off_until = -1  # inside <!-- -->, <script> or <style>: HTML is not looked at
    outputs = 0  # open cfoutput tags
    has_document = False
    pos = text_start = 0

    def add_text(a: int, b: int) -> None:
        if b > a:
            stack[-1].children.append(Node("text", "#text", a, b, open_end=a, in_output=outputs > 0))

    def splice(node: Node) -> None:
        """An optional-body tag never closed: it had no body after all."""
        parent = stack[-1]
        parent.children.extend(node.children)
        node.children = []
        node.end = node.open_end

    def close_to(name: str, at: int, close_end: int) -> bool:
        nonlocal outputs
        for depth in range(len(stack) - 1, 0, -1):
            if stack[depth].name == name:
                break
        else:
            return False
        while len(stack) - 1 > depth:
            node = stack.pop()
            if node.name == "cfoutput":
                outputs -= 1
            if node.name in REQUIRED_BODY:
                problems.append(f"<{node.name}> at offset {node.start} is not closed before </{name}>")
            splice(node)
        node = stack.pop()
        if node.name == "cfoutput":
            outputs -= 1
        if node.name == "cfform":
            ui.append(UIEvent("form_close", None, at))
        node.end = close_end
        node.body = (node.open_end, at)
        node.closed = True
        return True

    while True:
        m = _TAG.search(text, pos)
        if m is None:
            break
        s = m.start()
        token = m.group()
        if token == "<!---":
            add_text(text_start, s)
            pos = text_start = cf_comment_end(text, s)
            continue
        if token == "<!--":
            if s >= html_off_until:
                close = text.find("-->", s + 4)
                html_off_until = close + 3 if close >= 0 else end
            pos = s + 4
            continue
        closing = m.group(1) == "/"
        raw_name = m.group(2)
        name = raw_name.lower()
        is_cf = name.startswith("cf")
        if not is_cf and s < html_off_until:
            pos = m.end()
            continue
        if closing:
            gt = text.find(">", m.end())
            close_end = gt + 1 if gt >= 0 else end
            if is_cf:
                add_text(text_start, s)
                if not close_to(name, s, close_end):
                    problems.append(f"</{name}> at offset {s} closes nothing")
                pos = text_start = close_end
            else:
                if name == "form":
                    ui.append(UIEvent("form_close", None, s))
                pos = close_end  # an HTML end tag stays part of the surrounding text
            continue
        hashes = is_cf or outputs > 0
        if is_cf and name in EXPRESSION_TAGS:
            gt = _expression_end(text, m.end(), end)
            expr_end = gt
            if gt > m.end() and text[gt - 1] == "/" and gt < end:
                expr_end = gt - 1  # <cfset x = 1 />
            node = Node("tag", name, s, min(gt + 1, end), open_end=min(gt + 1, end), expr=(m.end(), expr_end), in_output=outputs > 0)
            self_closed = name != "cfif"
        else:
            attrs, after, self_closed = _parse_attrs(text, m.end(), end, hashes)
            node = Node("tag" if is_cf else "html", name, s, after, open_end=after, attrs=attrs, in_output=outputs > 0)
        if not is_cf:
            if name in ("script", "style"):
                close = re.compile(rf"</{name}\b", re.I).search(text, node.open_end)
                html_off_until = close.start() if close else end
                pos = node.open_end
                continue
            if name == "title":
                close = re.compile(r"</title\b", re.I).search(text, node.open_end)
                if close and title is None:
                    title = (node.open_end, close.start())
                has_document = True
                pos = node.open_end
                continue
            add_text(text_start, s)
            stack[-1].children.append(node)
            if name == "form":
                ui.append(UIEvent("form_open", node, s))
            else:
                ui.append(UIEvent("control", node, s))
            pos = text_start = node.open_end
            continue
        add_text(text_start, s)
        stack[-1].children.append(node)
        if name == "cfform":
            ui.append(UIEvent("form_open", node, s))
        elif name in ("cfinput", "cfselect", "cftextarea"):
            ui.append(UIEvent("control", node, s))
        if name in RAW_BODY and not self_closed:
            close = re.compile(rf"</{name}\s*>", re.I).search(text, node.open_end)
            if close is None:
                problems.append(f"<{name}> at offset {s} is not closed")
                node.body = (node.open_end, end)
                node.end = end
            else:
                node.body = (node.open_end, close.start())
                node.end = close.end()
                node.closed = True
            pos = text_start = node.end
            continue
        if not self_closed and name not in NEVER_BODY:
            stack.append(node)
            if name == "cfoutput":
                outputs += 1
        pos = text_start = node.open_end
    add_text(text_start, end)
    while len(stack) > 1:
        node = stack.pop()
        if node.name == "cfoutput":
            outputs -= 1
        if node.name in REQUIRED_BODY:
            problems.append(f"<{node.name}> at offset {node.start} is not closed")
            node.body = (node.open_end, end)
            node.end = end
            if node.name == "cfform":
                ui.append(UIEvent("form_close", None, end))
        else:
            splice(node)
    if re.search(r"<(?:html|body)\b", text, re.I):
        has_document = True
    return Document(root, ui, title, problems, has_document)
