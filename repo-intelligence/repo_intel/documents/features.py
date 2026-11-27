"""What each feature reads from a document's blocks (contract 9.1). All deterministic.

- `requirements`: an item or paragraph that starts with an identifier (`REQ-12`, `FR-3`:
  two to six capitals, a hyphen, digits; brackets, bold marks and a following `:`, `.`,
  `)` or dash allowed) or carries a modal (`shall`, `must`, `must not`, `should`). An
  identifier mentioned inside a sentence is a reference, not a definition, so it does
  not name the block. The name is the identifier lower-cased, or a slug of the first
  eight words; a later block with a name already taken adds its evidence to the first.
- `schema`: a table whose header has a field-name column and a type column, under a
  heading or with an entity column, and Mermaid `erDiagram` blocks.
- `code_lists`: a two-column table whose header is a code column and a meaning column.
- `rules`: candidate blocks with a rule signal, for `dm.document-rules` to name.

Each feature returns what it found and the indexes of the blocks it used, so coverage can
say how much of a document was read.
"""

from __future__ import annotations

import re
from typing import Any

MAX_STATEMENT = 280
MAX_NAME = 60
MAX_VALUES = 500
MAX_REQUIREMENTS = 2000
MAX_CANDIDATES = 200  # per document; the rest are counted, not sent

_IDENT = r"[A-Z]{2,6}-\d+"
_LEADING_ID = re.compile(r"^[\s>*_\[(]*(" + _IDENT + r")\b[\])*_]*\s*(?:[:.)\-–—]\s*)?")
_MODAL = re.compile(r"\b(must\s+not|shall\s+not|must|shall|should\s+not|should)\b", re.I)
_SIGNALS = [
    ("must_not", re.compile(r"\bmust\s+not\b", re.I)),
    ("must", re.compile(r"\bmust\b", re.I)),
    ("only_if", re.compile(r"\bonly\s+if\b", re.I)),
    ("at_least", re.compile(r"\bat\s+least\b", re.I)),
    ("no_more_than", re.compile(r"\bno\s+more\s+than\b", re.I)),
    ("cannot", re.compile(r"\b(?:cannot|can\s+not|can't)\b", re.I)),
    ("if_then", re.compile(r"\bif\b.{1,200}?\bthen\b", re.I | re.S)),
    ("percentage", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("threshold", re.compile(r"(?:[<>]=?|≤|≥)\s*\$?\d|\b(?:over|under|above|below|exceeds?|exceeding|more\s+than|less\s+than|greater\s+than|fewer\s+than|up\s+to|at\s+most|maximum(?:\s+of)?|minimum(?:\s+of)?)\s+[$€£]?\d", re.I)),
]

_NAME_COLUMNS = ("field", "field name", "column", "column name", "attribute", "attribute name", "element", "data element", "name")
_TYPE_COLUMNS = ("type", "data type", "datatype", "sql type", "field type", "column type")
_ENTITY_COLUMNS = ("entity", "entity name", "table", "table name", "record", "object")
_NULL_COLUMNS = {"nullable": False, "null": False, "null?": False, "optional": False, "required": True, "mandatory": True, "not null": True}
_KEY_COLUMNS = ("key", "keys", "pk", "primary key", "key type", "constraint", "constraints")
_DESC_COLUMNS = ("description", "meaning", "definition", "notes", "comment", "comments")
_CODE_COLUMNS = ("code", "value", "key", "id", "abbreviation", "short code")
_LABEL_COLUMNS = ("meaning", "description", "label", "name", "text", "definition", "title")


def _norm(header: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9?]+", " ", header.lower()).split())


def slug(text: str, words: int = 8, limit: int = MAX_NAME) -> str:
    """Lower-case words joined by hyphens: the first `words` words, cut at a word to `limit`."""
    parts = re.findall(r"[a-z0-9]+", text.lower())[:words]
    out = ""
    for p in parts:
        nxt = f"{out}-{p}" if out else p
        if len(nxt) > limit:
            break
        out = nxt
    return out or (parts[0][:limit] if parts else "")


def clip(text: str, limit: int = MAX_STATEMENT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def plain(text: str) -> str:
    """Block text without emphasis marks; backticks are kept, they name things."""
    return re.sub(r"(\*\*|__)", "", text)


# ------------------------------------------------------------------ names a block mentions

_BACKTICK = re.compile(r"`([^`\n]{1,120})`")
_QUALIFIED = re.compile(r"(?<![\w./-])([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)(?![\w/-]|\.\w)")


def mentions(text: str) -> dict[str, list[str]]:
    """What a block names exactly: `quoted` text in backticks, and `qualified` names
    (`schema.table`) written without them. Matched against entities later."""
    quoted = sorted({m.group(1).strip() for m in _BACKTICK.finditer(text) if m.group(1).strip()})
    bare = _BACKTICK.sub(" ", text)
    qualified = sorted({m.group(1) for m in _QUALIFIED.finditer(bare)})
    return {"quoted": quoted, "qualified": qualified}


# ------------------------------------------------------------------ requirements

def requirements(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[int]]:
    found: dict[str, dict[str, Any]] = {}
    used: set[int] = set()
    for i, b in enumerate(blocks):
        if b["kind"] not in ("item", "paragraph"):
            continue
        text = plain(b["text"])
        lead = _LEADING_ID.match(text)
        modal = _MODAL.search(text)
        if not lead and not modal:
            continue
        identifier = lead.group(1) if lead else None
        body = text[lead.end():] if lead else text
        name = identifier.lower() if identifier else slug(body)
        if not name:
            continue
        used.add(i)
        if name in found:
            found[name]["blocks"].append(i)
            continue
        if len(found) >= MAX_REQUIREMENTS:
            continue
        found[name] = {
            "name": name, "statement": clip(body if identifier and body.strip() else text),
            **({"identifier": identifier} if identifier else {}),
            **({"modality": " ".join(modal.group(1).lower().split())} if modal else {}),
            "section": list(b["heading_path"]), "blocks": [i], "mentions": mentions(b["text"]),
        }
    return [found[n] for n in sorted(found)], used


# ------------------------------------------------------------------ tables

def tables(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive rows of one table: header cells, data rows (block index and cells), heading."""
    out: dict[Any, dict[str, Any]] = {}
    for i, b in enumerate(blocks):
        if b["kind"] != "table_row" or "table" not in b:
            continue
        t = out.get(b["table"])
        if t is None:
            out[b["table"]] = {"header": [c.strip() for c in b["cells"]], "header_block": i, "rows": [], "heading_path": list(b["heading_path"])}
        else:
            t["rows"].append((i, b["cells"]))
    return list(out.values())


def _find(header: list[str], names: tuple[str, ...] | list[str]) -> int | None:
    normed = [_norm(h) for h in header]
    for want in names:
        if want in normed:
            return normed.index(want)
    return None


def _cell(cells: list[str], i: int | None) -> str:
    return cells[i].strip() if i is not None and i < len(cells) else ""


def _nullable(value: str, required: bool) -> bool | None:
    """Whether a field may be null, from a nullable column (`required` False) or a required
    column (`required` True); None when the cell says neither."""
    v = value.strip().lower()
    if v in ("null", "nullable", "optional"):
        return True
    if v in ("not null", "required", "mandatory"):
        return False
    if v in ("y", "yes", "true", "x", "1"):
        return not required
    if v in ("n", "no", "false", "0", "-"):
        return required
    return None


def schemas(blocks: list[dict[str, Any]], doc: str) -> tuple[list[dict[str, Any]], set[int]]:
    """DataSchemas `{entity, fields: [{name, type?, nullable?, key?, description?, block}], blocks}`."""
    found: dict[str, dict[str, Any]] = {}
    used: set[int] = set()

    def schema(entity: str, block: int) -> dict[str, Any]:
        s = found.setdefault(entity, {"entity": entity, "name": f"{doc}#{entity}", "fields": {}, "blocks": []})
        s["blocks"].append(block)
        return s

    for t in tables(blocks):
        header = t["header"]
        name_col, type_col = _find(header, _NAME_COLUMNS), _find(header, _TYPE_COLUMNS)
        if name_col is None or type_col is None or name_col == type_col:
            continue
        entity_col = _find(header, _ENTITY_COLUMNS)
        if entity_col in (name_col, type_col):
            entity_col = None
        heading = t["heading_path"][-1] if t["heading_path"] else None
        if entity_col is None and not heading:
            continue
        null_col = next((i for i, h in enumerate(header) if _norm(h) in _NULL_COLUMNS), None)
        required = _NULL_COLUMNS.get(_norm(header[null_col]), False) if null_col is not None else False
        key_col, desc_col = _find(header, _KEY_COLUMNS), _find(header, _DESC_COLUMNS)
        for i, cells in t["rows"]:
            field, ftype = _cell(cells, name_col), _cell(cells, type_col)
            entity = _cell(cells, entity_col) if entity_col is not None else heading
            if not field or not entity:
                continue
            s = schema(entity, i)
            used.update((i, t["header_block"]))
            if field in s["fields"]:
                continue
            nullable = _nullable(_cell(cells, null_col), required) if null_col is not None else None
            s["fields"][field] = {"name": field, "block": i, **({"type": ftype} if ftype else {}),
                                  **({"nullable": nullable} if nullable is not None else {}),
                                  **({"key": _cell(cells, key_col)} if _cell(cells, key_col) else {}),
                                  **({"description": clip(_cell(cells, desc_col))} if _cell(cells, desc_col) else {})}
    for i, b in enumerate(blocks):
        if b["kind"] == "code" and re.match(r"\s*erDiagram\b", b["text"]):
            for entity, fields in _er_diagram(b["text"]):
                s = schema(entity, i)
                used.add(i)
                for f in fields:
                    s["fields"].setdefault(f["name"], {**f, "block": i})
    out = []
    for entity in sorted(found):
        s = found[entity]
        out.append({**s, "fields": [s["fields"][k] for k in sorted(s["fields"])], "blocks": sorted(set(s["blocks"]))})
    return out, used


_ER_ENTITY = re.compile(r'^\s*("?[\w.-]+"?)\s*\{\s*$')
_ER_FIELD = re.compile(r'^\s*([\w\[\]()<>,.-]+)\s+([\w-]+)((?:\s+(?:PK|FK|UK))(?:\s*,\s*(?:PK|FK|UK))*)?\s*(?:"([^"]*)")?\s*$')


def _er_diagram(text: str) -> list[tuple[str, list[dict[str, Any]]]]:
    out: list[tuple[str, list[dict[str, Any]]]] = []
    current: tuple[str, list[dict[str, Any]]] | None = None
    for line in text.splitlines():
        if current is None:
            m = _ER_ENTITY.match(line)
            if m:
                current = (m.group(1).strip('"'), [])
            continue
        if line.strip() == "}":
            out.append(current)
            current = None
            continue
        m = _ER_FIELD.match(line)
        if m:
            keys = ",".join(k.strip() for k in re.split(r"[\s,]+", (m.group(3) or "").strip()) if k.strip())
            current[1].append({"name": m.group(2), "type": m.group(1), **({"key": keys} if keys else {}), **({"description": clip(m.group(4))} if m.group(4) else {})})
    return out


# ------------------------------------------------------------------ code lists

def code_lists(blocks: list[dict[str, Any]], doc: str) -> tuple[list[dict[str, Any]], set[int]]:
    out: list[dict[str, Any]] = []
    used: set[int] = set()
    taken: dict[str, int] = {}
    for t in tables(blocks):
        header = t["header"]
        if len([h for h in header if h.strip()]) != 2 or len(header) != 2:
            continue
        if _norm(header[0]) not in _CODE_COLUMNS or _norm(header[1]) not in _LABEL_COLUMNS:
            continue
        values = []
        rows = []
        for i, cells in t["rows"]:
            code, label = _cell(cells, 0), _cell(cells, 1)
            if not code:
                continue
            rows.append(i)
            if len(values) < MAX_VALUES:
                values.append({"code": code, "label": label})
        if not values:
            continue
        base = slug(t["heading_path"][-1], words=12) if t["heading_path"] else ""
        base = base or "codes"
        n = taken[base] = taken.get(base, 0) + 1
        name = f"{doc}#{base}" + (f"~{n}" if n > 1 else "")
        used.update([t["header_block"], *rows])
        out.append({"name": name, "heading": list(t["heading_path"]), "values": values, "omitted": max(0, len(rows) - MAX_VALUES), "blocks": [t["header_block"], *rows]})
    return out, used


# ------------------------------------------------------------------ rule candidates

def rule_candidates(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Blocks with a rule signal, numbered d1, d2, ... in document order, and how many were
    left out past MAX_CANDIDATES."""
    out: list[dict[str, Any]] = []
    seen = 0
    for i, b in enumerate(blocks):
        if b["kind"] not in ("item", "paragraph", "table_row"):
            continue
        text = plain(b["text"])
        signals = [name for name, pattern in _SIGNALS if pattern.search(text)]
        if "must_not" in signals:
            signals.remove("must")
        if not signals:
            continue
        seen += 1
        if len(out) >= MAX_CANDIDATES:
            continue
        out.append({"id": f"d{len(out) + 1}", "block": i, "kind": b["kind"], "section": list(b["heading_path"]), "text": clip(text), "signals": signals, "mentions": mentions(b["text"])})
    return out, seen - len(out)
