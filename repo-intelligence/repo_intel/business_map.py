"""Versioned tenant business maps. All writes compare the map revision."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

import psycopg
from psycopg.types.json import Jsonb
from pydantic import Field, StringConstraints, model_validator

from .contracts import Model


ACTIVE_MAP = "default"
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class StaleVersion(ValueError):
    pass


class Capability(Model):
    id: Key
    name: Name
    description: str = Field("", max_length=500)
    # Position on the free-form canvas; None until the card is moved there.
    x: int | None = Field(None, ge=0, le=20000)
    y: int | None = Field(None, ge=0, le=20000)

    def meaning(self) -> tuple[str, str, str]:
        """What placement reads: moving a card on the canvas changes nothing here."""
        return self.id, self.name, self.description


class Stage(Model):
    id: Key
    name: Name
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=100)


class Placement(Model):
    lineage_id: Key
    capability_id: Key | None = None
    status: Literal["confirmed", "ai", "suggested", "unplaced", "declared"]
    score: float | None = Field(None, ge=0, le=1)
    candidates: tuple[Key, ...] = ()
    rejected: tuple[Key, ...] = ()
    author: str
    decision_ref: str | None = None
    source_rkp_hash: str | None = None
    evidence_ids: tuple[str, ...] = ()
    reason: str | None = None


class MapDefinition(Model):
    title: Name = "Business map"
    layout: Literal["lanes", "canvas"] = "lanes"
    stages: tuple[Stage, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        stage_ids = [stage.id for stage in self.stages]
        capabilities = [cap.id for stage in self.stages for cap in stage.capabilities]
        if len(stage_ids) != len(set(stage_ids)) or len(capabilities) != len(set(capabilities)):
            raise ValueError("stage and capability IDs must be unique")
        if any(key in ("unknown", "none_of_these") for key in capabilities):
            raise ValueError("Use an unreserved capability ID.")
        if len(capabilities) > 300:
            raise ValueError("a map can hold up to 300 capabilities")
        return self

    def capabilities(self) -> dict[str, Capability]:
        return {cap.id: cap for stage in self.stages for cap in stage.capabilities}


class BusinessMap(MapDefinition):
    id: Key = ACTIVE_MAP
    version: int = 0
    author: str | None = None
    updated_at: datetime | None = None
    placements: tuple[Placement, ...] = ()


class SaveMap(MapDefinition):
    expected_version: int = Field(ge=0)


class Position(Model):
    x: int = Field(ge=0, le=20000)
    y: int = Field(ge=0, le=20000)
    stage_id: Key | None = None  # dropping a card in another stage column moves it to that stage


class ArrangeMap(Model):
    """Canvas layout only: no names, descriptions or placements change."""
    expected_version: int = Field(ge=0)
    layout: Literal["lanes", "canvas"]
    positions: dict[Key, Position] = Field(default_factory=dict, max_length=300)


class ReviewPlacement(Model):
    expected_version: int = Field(ge=0)
    lineage_id: Key
    action: Literal["confirm", "move", "remove", "reject"]
    capability_id: Key | None = None


def read(conn: psycopg.Connection, tenant: str, map_id: str = ACTIVE_MAP) -> BusinessMap:
    row = conn.execute(
        """SELECT r.document, r.author, r.created_at
           FROM business_maps m JOIN business_map_revisions r USING (tenant_id, map_id, version)
           WHERE m.tenant_id=%s AND m.map_id=%s""",
        (tenant, map_id),
    ).fetchone()
    return BusinessMap.model_validate(row[0] | {"author": row[1], "updated_at": row[2]}) if row else BusinessMap(id=map_id)


def revisions(conn: psycopg.Connection, tenant: str, map_id: str = ACTIVE_MAP) -> list[dict[str, Any]]:
    return [
        {"version": version, "author": author, "updated_at": at}
        for version, author, at in conn.execute(
            """SELECT version, author, created_at FROM business_map_revisions
               WHERE tenant_id=%s AND map_id=%s ORDER BY version DESC LIMIT 100""",
            (tenant, map_id),
        )
    ]


def save(conn: psycopg.Connection, tenant: str, current: BusinessMap, author: str) -> BusinessMap:
    """Persist a whole map and its revision in one transaction."""
    target = (tenant, current.id)
    version = current.version + 1
    with conn.transaction():
        if current.version == 0:
            changed = conn.execute(
                "INSERT INTO business_maps VALUES (%s,%s,1) ON CONFLICT DO NOTHING RETURNING version", target,
            ).fetchone()
        else:
            changed = conn.execute(
                "UPDATE business_maps SET version=%s WHERE tenant_id=%s AND map_id=%s AND version=%s RETURNING version",
                (version, *target, current.version),
            ).fetchone()
        if not changed:
            raise StaleVersion("This map changed. Reload it before saving.")
        for table in ("business_map_placements", "business_map_capabilities", "business_map_stages"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id=%s AND map_id=%s", target)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO business_map_stages VALUES (%s,%s,%s,%s,%s)", [
                (*target, stage.id, position, stage.name) for position, stage in enumerate(current.stages)
            ])
            cur.executemany("INSERT INTO business_map_capabilities VALUES (%s,%s,%s,%s,%s,%s,%s)", [
                (*target, stage.id, cap.id, position, cap.name, cap.description)
                for stage in current.stages for position, cap in enumerate(stage.capabilities)
            ])
            cur.executemany("INSERT INTO business_map_placements VALUES (%s,%s,%s,%s,%s)", [
                (*target, row.lineage_id, row.capability_id, Jsonb(row.row())) for row in current.placements
            ])
        updated = current.model_copy(update={"version": version, "author": author, "updated_at": None})
        at, = conn.execute(
            """INSERT INTO business_map_revisions (tenant_id,map_id,version,author,document)
               VALUES (%s,%s,%s,%s,%s) RETURNING created_at""",
            (*target, version, author, Jsonb(updated.row())),
        ).fetchone()
    return updated.model_copy(update={"updated_at": at})


def edit(current: BusinessMap, body: SaveMap) -> BusinessMap:
    if current.version != body.expected_version:
        raise StaleVersion("This map changed. Reload it before saving.")
    valid = body.capabilities()
    placements = []
    for row in current.placements:
        candidates = tuple(c for c in row.candidates if c in valid)
        if row.capability_id and row.capability_id not in valid:
            row = row.model_copy(update={"capability_id": None, "status": "unplaced", "score": None, "decision_ref": None})
        placements.append(row.model_copy(update={"candidates": candidates}))
    return current.model_copy(update={"title": body.title, "layout": body.layout if "layout" in body.model_fields_set else current.layout, "stages": body.stages, "placements": tuple(placements)})


def arrange(current: BusinessMap, body: ArrangeMap) -> BusinessMap:
    if current.version != body.expected_version:
        raise StaleVersion("This map changed. Reload it before saving.")
    stage_ids = {stage.id for stage in current.stages}
    known = current.capabilities()
    if set(body.positions) - set(known) or any(p.stage_id and p.stage_id not in stage_ids for p in body.positions.values()):
        raise ValueError("Unknown capability or stage.")
    moved = {cap_id: p for cap_id, p in body.positions.items()}
    stages = []
    for stage in current.stages:
        kept = [cap for cap in stage.capabilities if not (cap.id in moved and moved[cap.id].stage_id not in (None, stage.id))]
        arriving = [known[cap_id] for cap_id, p in moved.items() if p.stage_id == stage.id and cap_id not in {c.id for c in stage.capabilities}]
        stages.append(stage.model_copy(update={"capabilities": tuple(
            cap.model_copy(update={"x": moved[cap.id].x, "y": moved[cap.id].y}) if cap.id in moved else cap for cap in [*kept, *arriving]
        )}))
    return current.model_copy(update={"layout": body.layout, "stages": tuple(stages)})


def review(current: BusinessMap, body: ReviewPlacement, author: str) -> BusinessMap:
    if current.version != body.expected_version:
        raise StaleVersion("This map changed. Reload it before saving.")
    old = next((p for p in current.placements if p.lineage_id == body.lineage_id), None)
    rejected = set(old.rejected if old else ())
    capability = body.capability_id or (old.capability_id if old else None)
    if body.action in ("confirm", "move"):
        if capability not in current.capabilities():
            raise ValueError("Choose a capability on this map.")
        if old and old.capability_id and old.capability_id != capability:
            rejected.add(old.capability_id)
        rejected.discard(capability)
        status = "confirmed"
    else:
        if not old:
            raise ValueError("This subject has no placement to remove.")
        if capability:
            rejected.add(capability)
        capability, status = None, "unplaced"
    row = Placement(
        lineage_id=body.lineage_id, capability_id=capability, status=status, author=author,
        rejected=tuple(sorted(rejected)),
        candidates=tuple(c for c in (old.candidates if old else ()) if c not in rejected),
        reason="removed by owner" if body.action == "remove" else None,
    )
    return current.model_copy(update={"placements": tuple(p for p in current.placements if p.lineage_id != body.lineage_id) + (row,)})
