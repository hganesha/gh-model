"""Ruby source: Rails routes, Sinatra routes, ActiveRecord tables, SQL and Kafka.

- Rails `config/routes.rb` is read as Rails expands it: `resources :orders` is
  the seven REST routes (narrowed by `only:` / `except:`), `resource :profile`
  the singular six, `member` and `collection` blocks add to a resource,
  `namespace :admin` and `scope "/api"` add a prefix, and nested resources sit
  under `/orders/:order_id`. Verbs with an explicit path (`get "/health", to:
  "status#show"`) are routes as written. Anything computed (a loop that draws
  routes, `draw(:admin)`) is not read.
- Sinatra: `get "/x" do` in a file that requires sinatra or subclasses
  `Sinatra::Base`.
- ActiveRecord: `class Order < ApplicationRecord` is the table `orders` (Rails'
  pluralisation of the simple cases, E4) unless `self.table_name = "x"` names it.
  SQL literals given to `find_by_sql`, `execute`, `exec_query` and `select_all`.
- Kafka: Karafka routes (`topic :orders do consumer ... end`) subscribe; `produce`
  / `produce_sync` / `produce_async` / `deliver_message` with `topic: "x"` publish.
"""

from __future__ import annotations

import posixpath
import re

from repo_intel.compile.ontology import EntityKind, EvidenceClass
from repo_intel.extractors.base import Extraction
from repo_intel.extractors.textscan import Doc, blank, decode, join_route, looks_like_sql, queue_name, snake, sql_in, unquote

K = EntityKind
E4 = EvidenceClass.E4

_OPENERS = re.compile(r"(?m)(\bdo\b(?:\s*\|[^|\n]*\|)?\s*$)|^\s*(?:if|unless|case|begin|def|class|module|while|until)\b|(\bend\b)")
_REST = {"index": ("GET", ""), "create": ("POST", ""), "new": ("GET", "/new"), "edit": ("GET", "/:id/edit"), "show": ("GET", "/:id"), "update": ("PATCH", "/:id"), "update_put": ("PUT", "/:id"), "destroy": ("DELETE", "/:id")}
_SINGULAR = {"create": ("POST", ""), "new": ("GET", "/new"), "edit": ("GET", "/edit"), "show": ("GET", ""), "update": ("PATCH", ""), "update_put": ("PUT", ""), "destroy": ("DELETE", "")}
_SYM_LIST = re.compile(r"[:\"']([\w]+)")


def block_end(text: str, do_end: int) -> int:
    """Index of the `end` that closes the block opened just before `do_end`."""
    depth = 1
    for m in _OPENERS.finditer(text, do_end):
        depth += -1 if m.group(2) else 1
        if depth == 0:
            return m.start()
    return len(text)


def _strip(text: str) -> str:
    """Comments blanked (a `#` outside strings to the end of the line)."""
    out, i, quote = list(text), 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i + 1 >= len(text) or text[i + 1] != "{"):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return blank("".join(out), re.compile(r"(?ms)^=begin.*?^=end"))


def pluralize(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def _symbols(raw: str | None) -> list[str]:
    return _SYM_LIST.findall(raw or "")


class _Routes:
    _LINE = re.compile(r"""(?m)^[ \t]*(resources|resource|namespace|scope|member|collection|get|post|put|patch|delete|match|root|concern|concerns)\b([^\n]*)""")

    def __init__(self, d: Doc, text: str):
        self.d, self.text = d, text

    def run(self, start: int, end: int, prefix: str, resource: str | None = None, singular: bool = False, name: str | None = None) -> None:
        i = start
        while True:
            m = self._LINE.search(self.text, i, end)
            if not m:
                return
            word, rest = m.group(1), m.group(2)
            line_end = m.end()
            block = re.search(r"\bdo\b(\s*\|[^|\n]*\|)?\s*$", rest)
            body_end = block_end(self.text, line_end) if block else None
            at = self.d.at(m.start(1), line_end)
            args = rest[: block.start()] if block else rest
            if word in ("resources", "resource"):
                names = re.findall(r"^\s*\(?\s*:(\w+)", args) or re.findall(r"^\s*\(?\s*[\"'](\w+)[\"']", args)
                path_opt = re.search(r"path:\s*[\"']([^\"']+)[\"']", args)
                only = _symbols(re.search(r"only:\s*(\[[^\]]*\]|:\w+)", args).group(1)) if re.search(r"only:", args) else None
                except_ = _symbols(re.search(r"except:\s*(\[[^\]]*\]|:\w+)", args).group(1)) if re.search(r"except:", args) else []
                for name in names[:1]:
                    one = word == "resource"
                    base = join_route(prefix, path_opt.group(1) if path_opt else name)
                    table = _SINGULAR if one else _REST
                    for action, (method, suffix) in table.items():
                        act = action.replace("_put", "")
                        if (only is not None and act not in only) or act in except_:
                            continue
                        self.d.endpoint(at, method, base + suffix if suffix else base, "rails", f"{name}#{act}")
                    if block:
                        nested = base if one else join_route(base, f":{name[:-1] if name.endswith('s') else name}_id")
                        self.run(line_end, body_end, nested, base, one, name)
            elif word in ("namespace", "scope"):
                sym = re.match(r"^\s*\(?\s*:(\w+)", args)
                path = re.search(r"(?:path:\s*)?[\"'](/?[\w/:.-]*)[\"']", args)
                piece = sym.group(1) if word == "namespace" and sym else path.group(1) if path and not re.search(r"module:\s*[\"']", args[: path.start()] if path else "") else ""
                if word == "scope" and re.match(r"^\s*\(?\s*module:", args):
                    piece = ""
                if block:
                    self.run(line_end, body_end, join_route(prefix, piece))
            elif word in ("member", "collection") and block and resource:
                self.run(line_end, body_end, resource + ("" if singular or word == "collection" else "/:id"), name=name)
            elif word == "root":
                target = re.search(r"[\"'](\w+#\w+)[\"']", args)
                self.d.endpoint(at, "GET", join_route(prefix), "rails", target.group(1) if target else None)
            elif word in ("get", "post", "put", "patch", "delete", "match"):
                path = re.match(r"""^\s*\(?\s*(?:[\"']([^\"'\n]*)[\"']|:(\w+))""", args)
                if path:
                    target = re.search(r"""(?:to:\s*|=>\s*)[\"'](\w+(?:/\w+)*#\w+)[\"']""", args)
                    methods = [word.upper()] if word != "match" else [v.upper() for v in _symbols((re.search(r"via:\s*(\[[^\]]*\]|:\w+)", args) or re.match("()", "")).group(1))] or ["ANY"]
                    handler = target.group(1) if target else f"{name}#{path.group(2)}" if name and path.group(2) else None
                    for method in methods:
                        self.d.endpoint(at, method, join_route(prefix, path.group(1) if path.group(1) is not None else path.group(2)), "rails", handler)
            i = body_end + 3 if block and body_end is not None else line_end


_SINATRA = re.compile(r"""(?m)^[ \t]*(get|post|put|patch|delete|options|head)\s*\(?\s*[\"'](/[^\"'\n]*)[\"']\s*\)?\s*do\b""")
_MODEL = re.compile(r"""(?m)^\s*class\s+(\w+)\s*<\s*(?:ApplicationRecord|ActiveRecord::Base)\b""")
_TABLE_NAME = re.compile(r"""self\.table_name\s*=\s*[\"']([\w.]+)[\"']""")
_SQL = re.compile(r"""\b(?:find_by_sql|execute|exec_query|select_all|select_value|select_values|exec_update|exec_insert|exec_delete)\s*\(?\s*(<<[~-]?SQL\b|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")
_KARAFKA = re.compile(r"""(?m)^[ \t]*topic\s+[:\"']([\w.-]+)[\"']?\s+do\b""")
_PRODUCE = re.compile(r"""\b(?:produce|produce_sync|produce_async|deliver_message|produce_many_sync|produce_many_async)\s*\(?[^\n]*?\btopic:\s*([\"'][\w.-]+[\"']|\w+)""")


class RubySource:
    id = "ruby.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.endswith(".rb") or posixpath.basename(path) == "config.ru"

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(parsed=False)
        text = _strip(raw)
        d = Doc(path, data, blob, text)
        if path.endswith("config/routes.rb") or "routes.draw" in text:
            m = re.search(r"routes\.draw\s+do\b", text)
            if m:
                _Routes(d, text).run(m.end(), block_end(text, m.end()), "")
        if "sinatra" in text.lower():
            for m in _SINATRA.finditer(text):
                d.endpoint(d.at(m.start(1), m.end(2) + 1), m.group(1).upper(), m.group(2), "sinatra")
        for m in _MODEL.finditer(text):
            override = _TABLE_NAME.search(text, m.end())
            nxt = _MODEL.search(text, m.end())
            if override and (nxt is None or override.start() < nxt.start()):
                d.table(d.at(override.start(1), override.end(1)), override.group(1).lower(), None, {"orm": "activerecord", "model": m.group(1)})
            else:
                d.table(d.at(m.start(1), m.end(1)), pluralize(snake(m.group(1))), None, {"orm": "activerecord", "model": m.group(1), "name_from": "rails_convention"}, E4)
        for m in _SQL.finditer(text):
            token = m.group(1)
            if token.startswith("<<"):
                body_start = text.find("\n", m.end()) + 1
                end = re.compile(r"(?m)^\s*SQL\b").search(text, body_start)
                sql, offset = (text[body_start: end.start()], body_start) if end else (None, 0)
                if sql and "#{" in sql:
                    d.unresolved += 1
                    continue
            else:
                sql, offset = unquote(token), m.start(1) + 1
            if sql and looks_like_sql(sql):
                sql_in(d, sql, offset)
            elif token.startswith('"') and "#{" in token:
                d.unresolved += 1
        if "karafka" in text.lower() or "Karafka" in raw:
            for m in _KARAFKA.finditer(text):
                d.topic(d.at(m.start(1), m.end(1)), "subscribe", "kafka", queue_name(m.group(1)))
        for m in _PRODUCE.finditer(text):
            lit = unquote(m.group(1))
            d.topic(d.at(m.start(), m.end()), "publish", "kafka", queue_name(lit) if lit else None, None if lit else m.group(1) if m.group(1)[:1].isupper() else None)
        if not d.out:
            return Extraction(parsed=False)
        return d.done() if d.resolved or d.unresolved else Extraction(d.out)


EXTRACTORS = [RubySource()]
