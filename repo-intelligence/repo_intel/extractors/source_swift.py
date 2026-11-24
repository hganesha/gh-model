"""Swift source: Vapor routes and Fluent models.

Routes: `app.get("users", ":id", use: show)` and `app.post("login") { req in ...}`,
with prefixes from `let api = app.grouped("api")` and from `.group("x") { r in
... }` blocks (carried to the closure's parameter). A call counts as a route only
when it registers a handler (`use:` or a trailing closure), so `dict.get(...)`
is not read. Path components are literal strings; `:id` stays a parameter.
Fluent: `static let schema = "todos"` in a `Model` is the table `todos`.
"""

from __future__ import annotations

import re

from repo_intel.compile.ontology import EntityKind
from repo_intel.extractors.base import Extraction
from repo_intel.extractors.textscan import Doc, decode, join_route, matching, string_args, without_comments

K = EntityKind
_GROUPED = re.compile(r"""\blet\s+(\w+)\s*=\s*(\w+)\s*\.\s*grouped\(([^)]*)\)""")
_GROUP_BLOCK = re.compile(r"""\b(\w+)\s*\.\s*group\(([^)]*)\)\s*\{\s*(\w+)\s+in\b""")
_VERB = re.compile(r"""\b(\w+)\s*\.\s*(get|post|put|patch|delete|on)\s*(?:\(([^()]*)\))?\s*(\{)?""")
_SCHEMA = re.compile(r"""class\s+(\w+)\s*:\s*[^{]*\bModel\b[^{]*\{[^}]*?static\s+let\s+schema\s*=\s*"([\w.]+)\"""")


def _components(args: str) -> str:
    return join_route(*(v for v, *_ in string_args(args.split("use:", 1)[0])))


class SwiftSource:
    id = "swift.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.endswith(".swift") and not path.endswith("Package.swift")

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None or ("Vapor" not in raw and "Fluent" not in raw):
            return Extraction(parsed=False)
        text = without_comments(raw, "swift")
        d = Doc(path, data, blob, text)
        prefixes: dict[str, str] = {}
        for m in _GROUPED.finditer(text):
            prefixes[m.group(1)] = join_route(prefixes.get(m.group(2), ""), _components(m.group(3)))
        blocks: list[tuple[int, int, str, str]] = []
        for m in _GROUP_BLOCK.finditer(text):
            parent = next((p for a, b, v, p in reversed(blocks) if a <= m.start() < b and v == m.group(1)), prefixes.get(m.group(1), ""))
            blocks.append((m.end(), matching(text, text.rfind("{", 0, m.end())), m.group(3), join_route(parent, _components(m.group(2)))))
        for m in _VERB.finditer(text):
            var, verb, args, closure = m.group(1), m.group(2), m.group(3) or "", m.group(4)
            if "use:" not in args and not closure:
                continue
            method = verb.upper()
            if verb == "on":
                found = re.match(r"\s*\.(\w+)", args)
                if not found:
                    continue
                method, args = found.group(1).upper(), args[found.end():]
            prefix = next((p for a, b, v, p in reversed(blocks) if a <= m.start() < b and v == var), prefixes.get(var, ""))
            handler = re.search(r"use:\s*([\w.]+)", args)
            d.endpoint(d.at(m.start(), m.end()), method, join_route(prefix, _components(args)), "vapor", handler.group(1) if handler else None)
        for m in _SCHEMA.finditer(text):
            d.table(d.at(m.start(2), m.end(2)), m.group(2).lower(), None, {"orm": "fluent", "model": m.group(1)})
        return Extraction(d.out) if d.out else Extraction(parsed=False)


EXTRACTORS = [SwiftSource()]
