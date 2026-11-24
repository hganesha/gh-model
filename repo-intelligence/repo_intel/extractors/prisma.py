"""Prisma schema (`*.prisma`): models as Tables, scalar fields as Fields, and
the names of environment variables read with `env("NAME")`.

A hand-written line parser: Prisma's schema language is line oriented (one
field or attribute per line inside a block).
"""

from __future__ import annotations

import re

from repo_intel.compile.ontology import EntityKind, Predicate
from repo_intel.extractors.base import PACKAGE, Extraction, diagnostic, entity, ref, relation, span

SCALARS = {"String", "Boolean", "Int", "BigInt", "Float", "Decimal", "DateTime", "Json", "Bytes", "Unsupported"}
DATASTORE = "default"

_BLOCK = re.compile(rb"^\s*(model|view|enum|type|datasource|generator)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(\}?)\s*$")
_FIELD = re.compile(rb"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*(\[\])?\s*([?!])?(?=\s|$)(.*)$")
_MAP = re.compile(rb"@map\(\s*(?:name\s*:\s*)?\"([^\"]*)\"")
_BLOCK_MAP = re.compile(rb"^\s*@@map\(\s*(?:name\s*:\s*)?\"([^\"]*)\"")
_ENV = re.compile(rb"\benv\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)")


def _strip_comment(line: bytes) -> bytes:
    """Remove a trailing `//` comment, ignoring `//` inside a string."""
    quoted = False
    i = 0
    while i < len(line):
        c = line[i : i + 1]
        if c == b"\\" and quoted:
            i += 2
            continue
        if c == b'"':
            quoted = not quoted
        elif not quoted and line[i : i + 2] == b"//":
            return line[:i]
        i += 1
    return line


class PrismaSchema:
    id = "prisma.schema"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".prisma")

    def extract(self, path: str, data: bytes, blob: str) -> Extraction:
        out = Extraction()
        # Pass 1: split into blocks of (offset, code) lines.
        blocks: list[dict] = []
        current: dict | None = None
        offset = 0
        problems = 0
        for raw in data.splitlines(keepends=True):
            start, offset = offset, offset + len(raw)
            code = _strip_comment(raw.rstrip(b"\r\n")).rstrip()
            lead = len(code) - len(code.lstrip())
            if not code.strip():
                continue
            if current is None:
                m = _BLOCK.match(code)
                if m:
                    current = {"kind": m.group(1).decode(), "name": m.group(2).decode(), "at": span(data, start + lead, start + m.end(2)), "lines": []}
                    if m.group(3):
                        blocks.append(current)
                        current = None
                else:
                    problems += 1
            elif code.strip() == b"}":
                blocks.append(current)
                current = None
            elif _BLOCK.match(code):
                problems += 1  # previous block was never closed
                blocks.append(current)
                m = _BLOCK.match(code)
                current = {"kind": m.group(1).decode(), "name": m.group(2).decode(), "at": span(data, start + lead, start + m.end(2)), "lines": []}
            else:
                current["lines"].append((start + lead, code.strip()))
        if current is not None:
            problems += 1
            blocks.append(current)
        if problems:
            out.diagnostics.append(diagnostic(self.id, "warning", f"syntax errors ({problems}): extraction may be incomplete"))
            out.parsed = False

        models = {b["name"] for b in blocks if b["kind"] in ("model", "view")}
        composites = {b["name"] for b in blocks if b["kind"] == "type"}
        enums = {b["name"] for b in blocks if b["kind"] == "enum"}

        seen_env: set[str] = set()
        for b in blocks:
            if b["kind"] in ("datasource", "generator"):
                for at, code in b["lines"]:
                    for m in _ENV.finditer(code):
                        name = m.group(1).decode()
                        if name in seen_env:
                            continue
                        seen_env.add(name)
                        where = span(data, at + m.start(), at + m.end())
                        out.observations.append(entity(blob, where, EntityKind.CONFIG_KEY, {"name": name}, name))
                        out.observations.append(relation(blob, where, PACKAGE, Predicate.REFERENCES_CONFIG, ref(EntityKind.CONFIG_KEY, {"name": name}, name)))
            elif b["kind"] == "model":
                self._model(b, data, blob, models, composites, enums, out)
        return out

    def _model(self, b, data, blob, models, composites, enums, out):
        model = b["name"]
        table = model
        for _, code in b["lines"]:
            m = _BLOCK_MAP.match(code)
            if m:
                table = m.group(1).decode("utf-8", "replace")
        table_key = {"datastore": DATASTORE, "name": table}
        table_ref = ref(EntityKind.TABLE, table_key, table)
        out.observations.append(entity(blob, b["at"], EntityKind.TABLE, table_key, table, {"orm": "prisma", "model": model}))
        seen: set[str] = set()
        for at, code in b["lines"]:
            if code.startswith(b"@@"):
                continue
            m = _FIELD.match(code)
            if not m:
                out.diagnostics.append(diagnostic(self.id, "warning", f"model {model}: unrecognised line skipped"))
                continue
            field, type_name = m.group(1).decode(), m.group(2).decode()
            if type_name in models or type_name in composites:
                continue  # relation or embedded field, not a column
            if type_name not in SCALARS and type_name not in enums:
                out.diagnostics.append(diagnostic(self.id, "info", f"model {model}: field {field} has a type not declared in this file; skipped"))
                continue
            mapped = _MAP.search(m.group(6))
            column = mapped.group(1).decode("utf-8", "replace") if mapped else field
            if column in seen:
                continue
            seen.add(column)
            where = span(data, at, at + len(code))
            key = {"table": table, "name": column}
            name = f"{table}.{column}"
            out.observations.append(entity(blob, where, EntityKind.FIELD, key, name, {"orm": "prisma", "field": field, "type": type_name}))
            out.observations.append(relation(blob, where, table_ref, Predicate.CONTAINS, ref(EntityKind.FIELD, key, name)))


EXTRACTORS = [PrismaSchema()]
