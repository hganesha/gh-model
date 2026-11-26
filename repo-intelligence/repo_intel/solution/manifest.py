"""`.rkp/solution.json`: the repositories and inputs that make up one application.

    {
      "schema": "rkp-solution/1",
      "name": "shop",
      "members": [
        {"id": "web", "path": "../shop-web", "role": "web"},
        {"id": "orders", "path": "../orders-service", "role": "service", "database": "shopdb"},
        {"id": "reports", "bundle": "delivered/reports/.rkp", "role": "batch"}
      ],
      "inputs": [".rkp/inputs/catalog/*.json", ".rkp/inputs/catalog/*.csv"]
    }

A member is a repository with a compiled bundle (`path`, the repository's directory) or
a bundle copied out of its repository (`bundle`, a `.rkp` directory; evidence text is
not available for it). Paths are relative to the directory holding `.rkp/solution.json`,
so the manifest can be committed; absolute paths are refused.

`role` says what the member is in the application (`web`, `service`, `batch`,
`database`, ...). `database` names the database a member's unqualified table names
refer to: without it, a table the member's code names is matched to another member's
table only as a candidate, because the code does not say which database it means.

`inputs` are files under the solution's own `.rkp/inputs/`, by path or glob, read by the
platform packs whose `inputs` patterns match them (database catalogue dumps, for one).
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA = "rkp-solution/1"
MANIFEST = ".rkp/solution.json"
INPUTS_DIR = ".rkp/inputs/"
INPUTS_MEMBER = "inputs"  # the member id the solution's own inputs are read as
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NAME = re.compile(r"^[^\x00-\x1f]{1,200}$")


class SolutionError(ValueError):
    """The manifest is missing or invalid; the message says why."""


def _relative(value: str, what: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{what} is empty")
    if "\\" in text or re.match(r"^[A-Za-z]:", text) or text.startswith("/") or text.startswith("~"):
        raise ValueError(f"{what} {value!r} must be a relative path with '/' separators, so the manifest works on every machine")
    return str(PurePosixPath(text))


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    role: str
    path: str | None = None
    bundle: str | None = None
    database: str | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"member id {value!r} must be lower-case letters, digits, '-' and '_', starting with a letter")
        if value == INPUTS_MEMBER:
            raise ValueError(f"member id {INPUTS_MEMBER!r} is reserved for the solution's own inputs")
        return value

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"role {value!r} must be a lower-case word such as web, service, batch or database")
        return value

    @field_validator("database")
    @classmethod
    def _database(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_$.-]{0,127}$", value):
            raise ValueError(f"database {value!r} must be a database name")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return None if value is None else _relative(value, "path")

    @field_validator("bundle")
    @classmethod
    def _bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        bundle = _relative(value, "bundle")
        if PurePosixPath(bundle).name != ".rkp":
            raise ValueError(f"bundle {value!r} must name a .rkp directory")
        return bundle

    @model_validator(mode="after")
    def _source(self) -> "Member":
        if (self.path is None) == (self.bundle is None):
            raise ValueError(f"member {self.id}: give exactly one of 'path' (a repository) or 'bundle' (a .rkp directory)")
        return self

    @property
    def source(self) -> str:
        """The member's path as the manifest gives it."""
        return self.path if self.path is not None else str(self.bundle)

    @property
    def kind(self) -> Literal["repository", "bundle"]:
        return "repository" if self.path is not None else "bundle"

    def directory(self, root: Path) -> Path:
        """The directory whose `.rkp/` holds the member's bundle."""
        return (root / self.path) if self.path is not None else (root / str(self.bundle)).parent


class SolutionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["rkp-solution/1"] = Field(alias="schema")
    name: str
    description: str | None = Field(default=None, max_length=2000)
    members: list[Member] = Field(min_length=1)
    inputs: list[str] = []

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.match(value) or not value.strip():
            raise ValueError("name must be 1 to 200 printable characters")
        return value

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, values: list[str]) -> list[str]:
        out = []
        for value in values:
            path = _relative(value, "input")
            if not path.startswith(INPUTS_DIR) or ".." in PurePosixPath(path).parts:
                raise ValueError(f"input {value!r} must be under {INPUTS_DIR}")
            out.append(path)
        if len(out) != len(set(out)):
            raise ValueError("an input is listed twice")
        return out

    @model_validator(mode="after")
    def _unique(self) -> "SolutionManifest":
        ids = [m.id for m in self.members]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"member ids must be unique: {', '.join(duplicates)}")
        sources = [str(PurePosixPath(m.source).parent if m.kind == "bundle" else PurePosixPath(m.source)) for m in self.members]
        repeated = sorted({s for s in sources if sources.count(s) > 1})
        if repeated:
            raise ValueError(f"two members name the same bundle: {', '.join(repeated)}")
        return self

    def member(self, member_id: str) -> Member:
        for m in self.members:
            if m.id == member_id:
                return m
        raise KeyError(member_id)


def parse_manifest(text: str) -> SolutionManifest:
    """Validate a manifest's text. Raises SolutionError with every problem found."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise SolutionError(f"{MANIFEST} is not valid JSON: {error}") from None
    if not isinstance(document, dict):
        raise SolutionError(f"{MANIFEST} must hold a JSON object")
    if document.get("schema") != SCHEMA:
        raise SolutionError(f"{MANIFEST}: schema must be {SCHEMA!r}, not {document.get('schema')!r}")
    try:
        return SolutionManifest.model_validate(document)
    except ValidationError as error:
        problems = []
        for e in error.errors():
            where = ".".join(str(p) for p in e["loc"])
            message = e["msg"].removeprefix("Value error, ")
            problems.append(f"{where}: {message}" if where else message)
        raise SolutionError(f"{MANIFEST} is invalid: " + "; ".join(problems)) from None


def load_manifest(root: str | Path) -> SolutionManifest:
    path = Path(root) / MANIFEST
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SolutionError(f"no {MANIFEST} here; a solution lists its members in that file") from None
    return parse_manifest(text)
