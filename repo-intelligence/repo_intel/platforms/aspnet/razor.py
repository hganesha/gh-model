"""Server code in Razor (`.cshtml`, `.razor`) and Web Forms (`.aspx`, `.ascx`, `.master`) pages.

`razor_regions` finds what Razor treats as code, in one pass:

    @* ... *@                       comment
    @{ ... }                        code block (to its matching brace)
    @( ... )                        explicit expression
    @page "/x", @model T, ...       a directive, to the end of its line
    @functions { }, @code { }       a block of members
    @section Name {                 the header of a section (the body is markup)
    @if (...) {, @foreach (...) {   the header of a control-flow block (the body is markup)
    } else {, } else if (...) {     the joint between two branches
    @name.member(...)[...]          an implicit expression
    @@, a@b                         literal text, not code
    @bind-Value=, @onclick=         Blazor directive attributes: markup, not code

A switch block is masked whole, since its cases are code. Markup inside a code block
is masked with it: a `@{ }` block that writes markup loses that markup (rare in
practice). `webforms_regions` finds `<%@ %>`, `<% %>`, `<%= %>`, `<%: %>`, `<%# %>`,
`<%$ %>` and `<%-- --%>`.

Nothing here runs code; regions only say where code is, so the markup scanner can read
what is around it.
"""

from __future__ import annotations

import re

from repo_intel.platforms.jsp.pages import Region

LINE_DIRECTIVES = frozenset({
    "page", "model", "inject", "using", "inherits", "layout", "namespace", "implements", "attribute", "addTagHelper", "removeTagHelper",
    "tagHelperPrefix", "preservewhitespace", "rendermode", "typeparam",
})
BLOCK_DIRECTIVES = frozenset({"functions", "code"})
FLOW = frozenset({"if", "for", "foreach", "while", "switch", "do", "try", "lock", "using"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_JOINT = re.compile(r"\}\s*(?:else\s+if\s*(?=\()|else\s*(?=\{)|catch\b\s*(?=[({])|finally\s*(?=\{)|while\s*(?=\())")
_WEBFORMS = re.compile(r"<%--|<%")
_DIRECTIVE_ATTR = re.compile(r"(?:bind|on[a-z]\w*|ref|key|attributes|formname|rendermode)(?:[-:][\w:-]+)?(?=\s*=)")
MAX_SCAN = 200000


def _string_end(text: str, i: int) -> int:
    """Past the end of the C# string or character literal starting at `i`."""
    n = len(text)
    verbatim = text.startswith('@"', i) or text.startswith('$@"', i) or text.startswith('@$"', i)
    q = text.find('"', i) if text[i] in "@$" else i
    if q < 0:
        return n
    quote = text[q]
    j = q + 1
    while j < n:
        c = text[j]
        if verbatim and c == '"':
            if text.startswith('""', j):
                j += 2
                continue
            return j + 1
        if not verbatim and c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "\n" and not verbatim:
            return j
        j += 1
    return n


def matching(text: str, i: int, open_: str, close: str) -> int:
    """Past the bracket matching the one at `i`, skipping C# strings and comments; -1 if unclosed."""
    depth = 0
    n = min(len(text), i + MAX_SCAN)
    j = i
    while j < n:
        c = text[j]
        if c == '"' or (c in "@$" and j + 1 < n and text[j + 1] in '"$@'):
            j = _string_end(text, j)
            continue
        if c == "'":  # a character literal ('a', '\n'); any other apostrophe is text
            k = j + 2 if j + 1 < n and text[j + 1] == "\\" else j + 1
            if k + 1 < n and text[k + 1] == "'":
                j = k + 2
                continue
        if text.startswith("//", j) and open_ == "{":
            k = text.find("\n", j)
            j = n if k < 0 else k
            continue
        if text.startswith("/*", j):
            k = text.find("*/", j + 2)
            j = n if k < 0 else k + 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def razor_regions(text: str) -> list[Region]:
    out: list[Region] = []
    n = len(text)
    i = 0
    while i < n:
        at = text.find("@", i)
        if at < 0:
            break
        nxt = text[at + 1] if at + 1 < n else ""
        if nxt == "@":
            i = at + 2
            continue
        if at > 0 and (text[at - 1].isalnum() or text[at - 1] in "._") and nxt != "(":
            i = at + 1  # an address (a@b.c), not code
            continue
        if nxt == "*":
            end = text.find("*@", at + 2)
            end = n if end < 0 else end + 2
            out.append(Region("comment", at, end, "", at))
            i = end
            continue
        if nxt == ":":
            i = at + 2  # @: a line of literal text inside code
            continue
        if nxt == "{":
            end = matching(text, at + 1, "{", "}")
            end = n if end < 0 else end
            out.append(Region("code", at, end, text[at + 2:max(at + 2, end - 1)], at + 2))
            i = end
            continue
        if nxt == "(":
            end = matching(text, at + 1, "(", ")")
            end = n if end < 0 else end
            out.append(Region("expr", at, end, text[at + 2:max(at + 2, end - 1)], at + 2))
            i = end
            continue
        attr = _DIRECTIVE_ATTR.match(text, at + 1)
        if attr and at > 0 and text[at - 1] in " \t\r\n":
            i = attr.end()  # a Blazor directive attribute (@bind-Value=, @onclick=): markup, not code
            continue
        m = _IDENT.match(text, at + 1)
        if not m:
            i = at + 1
            continue
        word = m.group(0)
        j = m.end()
        if word == "using":  # `@using Namespace` is a directive, `@using (...) { }` a block
            k = _skip_space(text, j)
            word_kind = "flow" if k < n and text[k] == "(" else "directive"
        elif word in LINE_DIRECTIVES:
            word_kind = "directive"
        elif word in BLOCK_DIRECTIVES:
            word_kind = "functions"
        elif word == "section":
            word_kind = "section"
        elif word in FLOW:
            word_kind = "flow"
        else:
            word_kind = "expr"
        if word_kind == "directive":
            end = text.find("\n", j)
            end = n if end < 0 else end
            out.append(Region("directive", at, end, text[at + 1:end].rstrip("\r"), at + 1))
            i = end
            continue
        if word_kind == "functions":
            k = _skip_space(text, j)
            end = matching(text, k, "{", "}") if k < n and text[k] == "{" else -1
            end = (k if k < n else n) if end < 0 else end
            out.append(Region("functions", at, end, text[at + 1:end], at + 1))
            i = end
            continue
        if word_kind == "section":
            k = text.find("{", j)
            end = n if k < 0 else k + 1
            out.append(Region("section", at, end, text[at + 1:end - 1], at + 1))
            i = end
            continue
        if word_kind == "flow":
            k = _skip_space(text, j)
            if k < n and text[k] == "(":
                k = matching(text, k, "(", ")")
                k = n if k < 0 else k
            k = _skip_space(text, k)
            if word == "switch" and k < n and text[k] == "{":
                end = matching(text, k, "{", "}")
                end = n if end < 0 else end
            else:
                end = k + 1 if k < n and text[k] == "{" else k
            out.append(Region("flow", at, end, text[at + 1:end], at + 1))
            i = end
            continue
        # An implicit expression: a name, then members, calls and indexes.
        end = j
        if word == "await":
            k = _skip_space(text, j)
            m2 = _IDENT.match(text, k)
            end = m2.end() if m2 else j
        while end < n:
            if text[end] == "." and end + 1 < n and (text[end + 1].isalpha() or text[end + 1] == "_"):
                m2 = _IDENT.match(text, end + 1)
                end = m2.end()
            elif text.startswith("?.", end) and end + 2 < n and text[end + 2].isalpha():
                end = _IDENT.match(text, end + 2).end()
            elif text[end] in "([":
                close = matching(text, end, text[end], ")" if text[end] == "(" else "]")
                if close < 0:
                    break
                end = close
            else:
                break
        out.append(Region("expr", at, end, text[at + 1:end], at + 1))
        i = end
    # Joints between branches of a control-flow block (`} else {`), outside every region found.
    starts = [(r.start, r.end) for r in out]
    for m in _JOINT.finditer(text):
        a = m.start()
        if any(s <= a < e for s, e in starts):
            continue
        k = m.end()
        if k < n and text[k] == "(":
            k = matching(text, k, "(", ")")
            if k < 0:
                continue
        k = _skip_space(text, k)
        end = k + 1 if k < n and text[k] == "{" else k
        if k < n and text[k] == ";":
            end = k + 1
        out.append(Region("flow", a, end, text[a:end], a))
    out.sort(key=lambda r: r.start)
    return out


def webforms_regions(text: str) -> list[Region]:
    out: list[Region] = []
    n = len(text)
    pos = 0
    while pos < n:
        m = _WEBFORMS.search(text, pos)
        if not m:
            break
        a = m.start()
        if m.group(0) == "<%--":
            end = text.find("--%>", a + 4)
            end = n if end < 0 else end + 4
            out.append(Region("comment", a, end, "", a))
            pos = end
            continue
        end = text.find("%>", a + 2)
        end = n if end < 0 else end + 2
        mark = text[a + 2:a + 3]
        kinds = {"@": "directive", "=": "code-expression", ":": "encoded-expression", "#": "databinding", "$": "expression-builder"}
        kind = kinds.get(mark, "code-block")
        body = a + 3 if kind != "code-block" else a + 2
        out.append(Region(kind, a, end, text[body:max(body, end - 2)], body))
        pos = end
    return out
