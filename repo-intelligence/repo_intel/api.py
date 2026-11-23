"""HTTP API (spec §10). Exposes snapshot lookup, the knowledge query and evidence excerpts.

Authentication in M0 is a single development bearer token bound to one tenant.
It fails closed: with no token configured, every request is refused. Real
principals, GitHub permission checks and path scopes are GI-022.
"""


import hmac
from typing import Annotated, Any

import asyncio
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import projection
from .contracts import Manifest
from .delta import knowledge_delta
from .pipeline import render_evidence
from .store import ObjectStore
from .github import GitHubError
from .service import PLANES, Service
from .ontology import Predicate


class SeedSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None
    name: str | None = None


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rkp_hash: str
    seed_entities: list[str] | None = Field(default=None, max_length=500)
    seed_selector: SeedSelector | None = None
    allowed_edge_types: list[Predicate] | None = None
    max_depth: int = Field(default=2, ge=0, le=5)
    max_nodes: int = Field(default=250, ge=1, le=1000)

    @model_validator(mode="after")
    def _one_seed(self) -> "QueryRequest":
        if (self.seed_entities is None) == (self.seed_selector is None):
            raise ValueError("give exactly one of seed_entities or seed_selector")
        return self


def _error(status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "retryable": retryable, "details": {}}, status_code=status)


def create_app(conninfo: str, tenant_id: str, dev_token: str | None, store: ObjectStore | None = None, service: Service | None = None) -> FastAPI:
    app = FastAPI(title="Repository Intelligence API", version="0.1.0")

    def principal_tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not dev_token or not supplied or not hmac.compare_digest(supplied, dev_token):
            raise HTTPException(401, "unauthenticated")
        return tenant_id

    Tenant = Annotated[str, Depends(principal_tenant)]

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, {401: "unauthenticated", 404: "not_found"}.get(exc.status_code, "error"), str(exc.detail))

    @app.get("/v1/repositories/{repository_id:path}/rkp")
    def get_rkp(repository_id: str, commit_sha: str, tenant: Tenant) -> dict[str, Any]:
        with projection.connect(conninfo, tenant) as conn:
            manifest = projection.get_snapshot(conn, tenant, repository_id=repository_id, commit_sha=commit_sha)
        if manifest is None:
            raise HTTPException(404, "not found")
        return manifest

    @app.post("/v1/knowledge/query")
    def query(body: QueryRequest, tenant: Tenant) -> dict[str, Any]:
        with projection.connect(conninfo, tenant) as conn:
            manifest = projection.get_snapshot(conn, tenant, rkp_hash=body.rkp_hash)
            if manifest is None or manifest["status"] != "complete":
                raise HTTPException(404, "not found")
            seeds = body.seed_entities if body.seed_entities is not None else projection.find_entities(conn, tenant, body.rkp_hash, body.seed_selector.kind, body.seed_selector.name)
            edges = [str(e) for e in body.allowed_edge_types] if body.allowed_edge_types is not None else None
            result = projection.query_graph(conn, tenant, body.rkp_hash, seeds, edges, body.max_depth, body.max_nodes)
        return {"rkp_hash": body.rkp_hash, "commit_sha": manifest["commit_sha"], "coverage_ref": manifest["coverage_ref"], **result}

    @app.get("/v1/evidence/{evidence_id}")
    def get_evidence(evidence_id: str, rkp_hash: str, tenant: Tenant) -> dict[str, Any]:
        """Render the secret-masked excerpt for one evidence row. Source is never stored in the graph."""
        if store is None or store.tenant_id != tenant:
            raise HTTPException(404, "not found")
        with projection.connect(conninfo, tenant) as conn:
            manifest = projection.get_snapshot(conn, tenant, rkp_hash=rkp_hash)
            row = projection.get_evidence(conn, tenant, rkp_hash, evidence_id) if manifest else None
        if row is None:
            raise HTTPException(404, "not found")
        return render_evidence(store, Manifest.model_validate(manifest), row)

    # ---- repositories, compiles and snapshot reads (used by the Explorer)

    def svc() -> Service:
        if service is None:
            raise HTTPException(404, "not found")
        return service

    def snapshot(rkp_hash: str, tenant: str) -> dict[str, Any]:
        with projection.connect(conninfo, tenant) as conn:
            manifest = projection.get_snapshot(conn, tenant, rkp_hash=rkp_hash)
        if manifest is None:
            raise HTTPException(404, "not found")
        return manifest

    class ConnectRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        url: str | None = Field(default=None, max_length=300)
        ref: str | None = Field(default=None, max_length=200)
        local_path: str | None = Field(default=None, max_length=1000)

    def public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "events"} | {"event_count": len(job["events"])}

    @app.get("/v1/repositories")
    def list_repositories(tenant: Tenant) -> dict[str, Any]:
        return {"repositories": svc().repositories(), "planes": PLANES}

    @app.post("/v1/repositories")
    def connect_repository(body: ConnectRequest, tenant: Tenant) -> dict[str, Any]:
        try:
            return public(svc().connect(body.url, body.ref, body.local_path))
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/repositories/{repository_id:path}/compilations")
    def compile_repository(repository_id: str, body: ConnectRequest, tenant: Tenant) -> dict[str, Any]:
        try:
            return public(svc().recompile(repository_id, body.ref))
        except KeyError as exc:
            raise HTTPException(404, "not found") from exc
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/compilations/{job_id}")
    def get_compilation(job_id: str, tenant: Tenant) -> dict[str, Any]:
        job = svc().jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "not found")
        return public(job) | {"events": job["events"][-50:]}

    @app.get("/v1/compilations/{job_id}/events")
    async def compilation_events(job_id: str, tenant: Tenant, after: int = -1) -> StreamingResponse:
        """Server-sent progress: only events after `after` are sent, never the whole state."""
        job = svc().jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "not found")

        async def stream():
            sent = after + 1
            while True:
                while sent < len(job["events"]):
                    yield f"data: {json.dumps(job['events'][sent])}\n\n"
                    sent += 1
                if job["status"] in ("complete", "failed"):
                    yield f"event: done\ndata: {json.dumps(public(job))}\n\n"
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/v1/snapshots/{rkp_hash}/summary")
    def snapshot_summary(rkp_hash: str, tenant: Tenant) -> dict[str, Any]:
        return svc().summary(snapshot(rkp_hash, tenant))

    @app.get("/v1/snapshots/{rkp_hash}/entities")
    def snapshot_entities(rkp_hash: str, tenant: Tenant, plane: str | None = None, kind: str | None = None, q: str | None = None, limit: int = 200) -> dict[str, Any]:
        snapshot(rkp_hash, tenant)
        if plane is not None and plane not in PLANES and plane != "files":
            raise HTTPException(404, "not found")
        kinds = [kind] if kind else (["Artifact"] if plane == "files" else PLANES.get(plane or "") or None)
        limit = max(1, min(limit, 500))
        with projection.connect(conninfo, tenant) as conn:
            rows = projection.list_entities(conn, tenant, rkp_hash, kinds, (q or "")[:100] or None, limit + 1)
        return {"entities": rows[:limit], "truncated": len(rows) > limit}

    @app.get("/v1/snapshots/{rkp_hash}/entities/{entity_id}")
    def snapshot_entity(rkp_hash: str, entity_id: str, tenant: Tenant) -> dict[str, Any]:
        manifest = snapshot(rkp_hash, tenant)
        with projection.connect(conninfo, tenant) as conn:
            detail = projection.entity_detail(conn, tenant, rkp_hash, entity_id)
        if detail is None:
            raise HTTPException(404, "not found")
        s = svc()
        detail["unknowns"] = [u for u in s.store.get_json(manifest["unknowns_ref"]) if u["subject"] == entity_id]
        detail["conflicts"] = [c for c in s.store.get_json(manifest["conflicts_ref"]) if entity_id in c["subject_ids"]]
        return detail

    @app.get("/v1/knowledge/diff")
    def knowledge_diff(tenant: Tenant, to: str, from_: Annotated[str, Query(alias="from")]) -> dict[str, Any]:
        """What changed between two snapshots of one repository (`from` is the older one)."""
        older, newer = Manifest.model_validate(snapshot(from_, tenant)), Manifest.model_validate(snapshot(to, tenant))
        try:
            return knowledge_delta(svc().store, older, newer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/snapshots/{rkp_hash}/decisions")
    def snapshot_decisions(rkp_hash: str, tenant: Tenant) -> dict[str, Any]:
        return {"decisions": svc().decisions(snapshot(rkp_hash, tenant))}

    return app
