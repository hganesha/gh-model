"""HTTP API (spec §10). Exposes snapshot lookup, the knowledge query and evidence excerpts.

Authentication in M0 is a single development bearer token bound to one tenant.
It fails closed: with no token configured, every request is refused. Real
principals, GitHub permission checks and path scopes are GI-022.
"""


import hmac
from typing import Annotated, Any, Literal

import asyncio
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import business_map, lineage, projection, settings
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


def _error(status: int, code: str, message: str, retryable: bool = False, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "retryable": retryable, "details": details or {}}, status_code=status)


class Retracted(Exception):
    """A read of a retracted snapshot: refused, with a pointer to its replacement (spec §4.4)."""

    def __init__(self, record: dict[str, Any]):
        self.record = record


class RetractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["compiler_defect", "leaked_secret", "policy_violation", "other"]
    replacement: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


def create_app(conninfo: str, tenant_id: str, dev_token: str | None, store: ObjectStore | None = None, service: Service | None = None) -> FastAPI:
    app = FastAPI(title="Repository Intelligence API", version="0.1.0")

    def principal_tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not dev_token or not supplied or not hmac.compare_digest(supplied, dev_token):
            raise HTTPException(401, "unauthenticated")
        return tenant_id

    Tenant = Annotated[str, Depends(principal_tenant)]

    # The only configured principal in the local deployment owns this workspace.
    def administrator(tenant: Tenant) -> tuple[str, str]:
        return tenant, "local-admin"

    Admin = Annotated[tuple[str, str], Depends(administrator)]

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        return _error(422, "invalid_request", errors[0]["msg"] if errors else "Invalid request.")

    @app.exception_handler(Retracted)
    async def _retracted(_: Request, exc: Retracted) -> JSONResponse:
        return _error(410, "retracted", "This snapshot was retracted.", details={"reason": exc.record.get("reason"), "replacement": exc.record.get("replacement")})

    def readable(conn: Any, tenant: str, manifest: dict[str, Any] | None) -> dict[str, Any] | None:
        """Every read goes through here: a retracted snapshot is never served."""
        if manifest is not None and manifest["status"] == "retracted":
            raise Retracted(projection.retraction(conn, tenant, manifest["rkp_hash"]) or {})
        return manifest

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, {401: "unauthenticated", 404: "not_found"}.get(exc.status_code, "error"), str(exc.detail))

    @app.get("/v1/repositories/{repository_id:path}/rkp")
    def get_rkp(repository_id: str, tenant: Tenant, commit_sha: str | None = None, ref: Annotated[str | None, Query(max_length=200)] = None, toolchain_hash: str | None = None):
        """By commit, or by tracked ref; with `ref`, also says whether the ref view is `current` or `stale`.
        Asking for a stale pull request's ref starts its (debounced) compile."""
        if (commit_sha is None) == (ref is None):
            raise HTTPException(422, "give exactly one of commit_sha or ref")
        if ref is None:
            with projection.connect(conninfo, tenant) as conn:
                return readable(conn, tenant, projection.get_snapshot(conn, tenant, repository_id=repository_id, commit_sha=commit_sha, toolchain_hash=toolchain_hash)) or _missing()
        from . import registry
        if service is not None:
            view = service.request_ref(repository_id, ref)
        else:
            with projection.connect(conninfo, tenant) as conn:
                view = registry.tracked_ref(conn, tenant, repository_id, ref)
        if view is None:
            raise HTTPException(404, "not found")
        ref_view = {"ref": ref, "kind": view["kind"], "state": view["state"], "head_sha": view["head_sha"], "commit_sha": view["published_sha"], "closed": view["closed_at"] is not None}
        if not view["rkp_hash"]:
            return JSONResponse({"status": "pending", "ref_view": ref_view}, status_code=202)
        with projection.connect(conninfo, tenant) as conn:
            manifest = readable(conn, tenant, projection.get_snapshot(conn, tenant, rkp_hash=view["rkp_hash"]))
        if manifest is None:
            raise HTTPException(404, "not found")
        return manifest | {"ref_view": ref_view}

    def _missing():
        raise HTTPException(404, "not found")

    @app.get("/v1/entities/{entity_id}/lineage")
    def entity_lineage(entity_id: str, tenant: Tenant) -> dict[str, Any]:
        """Cross-revision history of the logical entity this snapshot ID belongs to."""
        with projection.connect(conninfo, tenant) as conn:
            found = lineage.history(conn, tenant, entity_id)
        if found is None:
            raise HTTPException(404, "not found")
        return found

    @app.post("/v1/knowledge/query")
    def query(body: QueryRequest, tenant: Tenant) -> dict[str, Any]:
        with projection.connect(conninfo, tenant) as conn:
            manifest = readable(conn, tenant, projection.get_snapshot(conn, tenant, rkp_hash=body.rkp_hash))
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
            manifest = readable(conn, tenant, projection.get_snapshot(conn, tenant, rkp_hash=rkp_hash))
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
            manifest = readable(conn, tenant, projection.get_snapshot(conn, tenant, rkp_hash=rkp_hash))
        if manifest is None:
            raise HTTPException(404, "not found")
        return manifest

    class ConnectRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        url: str | None = Field(default=None, max_length=300)
        ref: str | None = Field(default=None, max_length=200)
        local_path: str | None = Field(default=None, max_length=1000)
        connection_id: business_map.Key = "github-cli"

    def public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "events"} | {"event_count": len(job["events"])}

    @app.get("/v1/repositories")
    def list_repositories(tenant: Tenant) -> dict[str, Any]:
        return {"repositories": svc().repositories(), "planes": PLANES}

    @app.post("/v1/repositories")
    def connect_repository(body: ConnectRequest, tenant: Tenant) -> dict[str, Any]:
        try:
            return public(svc().connect(body.url, body.ref, body.local_path, body.connection_id))
        except KeyError as exc:
            raise HTTPException(404, "connection not found") from exc
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
                if job["status"] in ("complete", "failed", "cancelled", "superseded"):
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
            result = knowledge_delta(svc().store, older, newer)
            claimed = svc().map_for_snapshot(older)["components"] | svc().map_for_snapshot(newer)["components"]
            entity_ids = {e["id"] for e in result["entities"]} | {e["entity"]["id"] for e in result["impact"]}
            result["capabilities_touched"] = sorted({
                row["capability"] for key, row in claimed.items()
                if key in entity_ids and row["status"] in ("confirmed", "ai") and row["capability"]
            })
            return result
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/snapshots/{rkp_hash}/decisions")
    def snapshot_decisions(rkp_hash: str, tenant: Tenant) -> dict[str, Any]:
        manifest = snapshot(rkp_hash, tenant)
        overlay = svc().map_for_snapshot(Manifest.model_validate(manifest))["components"]
        system = svc()
        decisions = []
        for row in overlay.values():
            ref = row["decision_ref"]
            if ref and row["source_rkp_hash"] == rkp_hash:
                doc = system.store.index_get("ledger", ref) or system.store.index_get("placement_attempts", ref)
                if doc:
                    decisions.append(system.store.get_json(doc))
        return {"decisions": [*system.decisions(manifest), *decisions]}

    @app.get("/v1/snapshots/{rkp_hash}/dependencies")
    def dependencies(rkp_hash: str, tenant: Tenant):
        return svc().dependency_groups(Manifest.model_validate(snapshot(rkp_hash, tenant)))

    @app.get("/v1/snapshots/{rkp_hash}/placements")
    def placements(rkp_hash: str, tenant: Tenant):
        return svc().map_for_snapshot(Manifest.model_validate(snapshot(rkp_hash, tenant)))

    @app.get("/v1/business-maps")
    def map_view(tenant: Tenant):
        return svc().map_view()

    @app.get("/v1/business-maps/revisions")
    def map_revisions(tenant: Tenant):
        with projection.connect(conninfo, tenant) as conn:
            return {"revisions": business_map.revisions(conn, tenant)}

    @app.post("/v1/business-maps")
    def save_map(body: business_map.SaveMap, admin: Admin):
        tenant, actor = admin
        system = svc()
        try:
            with projection.connect(conninfo, tenant) as conn:
                current_map = business_map.read(conn, tenant)
                edited = business_map.edit(current_map, body)
                business_map.save(conn, tenant, edited, actor)
        except business_map.StaleVersion as exc:
            raise HTTPException(409, str(exc)) from exc
        meaning = lambda m: sorted(cap.meaning() for cap in m.capabilities().values())
        if current_map.capabilities() and meaning(edited) != meaning(current_map):
            system.queue_placement()
        return system.map_view()

    @app.post("/v1/business-maps/layout")
    def arrange_map(body: business_map.ArrangeMap, admin: Admin):
        tenant, actor = admin
        try:
            with projection.connect(conninfo, tenant) as conn:
                business_map.save(conn, tenant, business_map.arrange(business_map.read(conn, tenant), body), actor)
        except business_map.StaleVersion as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return svc().map_view()

    @app.post("/v1/business-maps/placements")
    def review_placement(body: business_map.ReviewPlacement, admin: Admin):
        tenant, actor = admin
        system = svc()
        view = system.map_view()
        if not (any(s["lineage_id"] == body.lineage_id for s in view["subjects"]) or
                body.action in ("remove", "reject") and any(p["lineage_id"] == body.lineage_id for p in view["map"]["placements"])):
            raise HTTPException(404, "placement subject not found")
        try:
            with projection.connect(conninfo, tenant) as conn:
                updated = business_map.review(business_map.read(conn, tenant), body, actor)
                business_map.save(conn, tenant, updated, actor)
        except business_map.StaleVersion as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return system.map_view()

    @app.post("/v1/business-maps/run-placement")
    def place_components(admin: Admin):
        try:
            svc().run_placement(attempts=3)  # a save may have started a background run moments ago
        except business_map.StaleVersion as exc:
            raise HTTPException(409, str(exc)) from exc
        return svc().map_view()

    @app.get("/v1/settings")
    def read_settings(admin: Admin):
        return svc().settings_view()

    @app.post("/v1/settings")
    def save_settings(body: settings.SaveSettings, admin: Admin):
        tenant, actor = admin
        system = svc()
        repos = {r["repository_id"]: r for r in system.repositories()}
        connections = {c.id: c for c in body.connections}
        for repo_id, tracking in body.repositories.items():
            if tracking.trigger != "push":
                continue
            repo = repos.get(repo_id)
            connection = connections.get(repo.get("connection_id")) if repo else None
            if not connection or not connection.webhook_ref or connection.mode not in ("github_app", "gitlab_token"):
                raise HTTPException(422, "Push tracking requires a GitHub App or GitLab connection with a webhook reference.")
        try:
            with projection.connect(conninfo, tenant) as conn:
                _, old = settings.read(conn, tenant)
                if any(getattr(body.compile, key) != getattr(old.compile, key) for key in settings.environment_settings()):
                    raise HTTPException(422, "Environment-owned settings are read-only.")
                settings.save(conn, tenant, body, actor)
        except business_map.StaleVersion as exc:
            raise HTTPException(409, str(exc)) from exc
        if old.business_map != body.business_map or old.compile != body.compile:
            system.queue_placement()
        return system.settings_view()

    @app.get("/v1/settings/connections/{connection_id}/repositories")
    def browse_repositories(connection_id: str, admin: Admin, page: Annotated[int, Query(ge=1, le=100)] = 1):
        try:
            return svc().browse(connection_id, page)
        except KeyError:
            raise HTTPException(404, "connection not found") from None
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/settings/connections/{connection_id}/test")
    def test_connection(connection_id: str, admin: Admin):
        try:
            return svc().connection_test(connection_id)
        except KeyError:
            raise HTTPException(404, "connection not found") from None

    @app.post("/v1/settings/connections/{connection_id}/device")
    def start_device(connection_id: str, admin: Admin):
        try:
            return svc().start_device(connection_id)
        except KeyError:
            raise HTTPException(404, "connection not found") from None
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/settings/connections/{connection_id}/device/poll")
    def poll_device(connection_id: str, admin: Admin):
        try:
            return svc().poll_device(connection_id)
        except KeyError:
            raise HTTPException(404, "connection not found") from None
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/settings/models/test")
    def check_model(admin: Admin, alias: Annotated[str, Query(max_length=80)]):
        from .decisions.live_check import run_check
        system = svc()
        config = settings.effective_compile(system.workspace_settings()[1])
        gateway = config.gateway(system.gateway)
        if gateway is None or alias not in gateway.aliases:
            raise HTTPException(404, "model alias is not configured")
        if gateway.aliases[alias].provider not in gateway.allowed_providers:
            raise HTTPException(403, "provider egress is disabled")
        return {"results": run_check(gateway, alias)}

    # ---- lifecycle: retraction, promotion, retention and deletion (GI-019, GI-021)

    @app.post("/v1/repositories/{repository_id:path}/snapshots/{rkp_hash}/retract")
    def retract_snapshot(repository_id: str, rkp_hash: str, body: RetractRequest, admin: Admin):
        _, actor = admin
        try:
            return svc().retract(repository_id, rkp_hash, body.reason, actor, body.replacement)
        except KeyError:
            raise HTTPException(404, "not found") from None

    @app.post("/v1/repositories/{repository_id:path}/snapshots/{rkp_hash}/promote")
    def promote_snapshot(repository_id: str, rkp_hash: str, admin: Admin):
        _, actor = admin
        try:
            return svc().promote(repository_id, rkp_hash, actor)
        except KeyError:
            raise HTTPException(404, "not found") from None

    @app.delete("/v1/repositories/{repository_id:path}")
    def delete_repository(repository_id: str, admin: Admin):
        _, actor = admin
        try:
            return svc().delete_repository(repository_id, actor)
        except KeyError:
            raise HTTPException(404, "not found") from None

    @app.delete("/v1/workspace")
    def delete_workspace(admin: Admin, confirm: Annotated[str, Query(max_length=200)]):
        tenant, actor = admin
        if confirm != tenant:
            raise HTTPException(422, "confirm with the workspace's tenant ID")
        return svc().delete_tenant(actor)

    @app.post("/v1/maintenance/gc")
    def collect_garbage(admin: Admin):
        _, actor = admin
        return svc().collect_garbage(actor)

    @app.post("/v1/maintenance/secret-rescan")
    def rescan_secrets(admin: Admin):
        _, actor = admin
        return {"retracted": svc().rescan_secrets(actor)}

    @app.post("/v1/webhooks/{connection_id}", status_code=202)
    async def push_webhook(connection_id: str, request: Request):
        from starlette.concurrency import run_in_threadpool
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 1024 * 1024:
                raise HTTPException(413, "webhook payload too large")
        try:
            return await run_in_threadpool(svc().webhook, connection_id, request.headers, bytes(payload))
        except KeyError:
            raise HTTPException(404, "connection not found") from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app
