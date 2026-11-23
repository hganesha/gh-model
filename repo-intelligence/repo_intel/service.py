"""Local compile service: repository registry and background compile jobs.

Single-process and single-user, for running the whole thing on one machine.
The registry is projected in PostgreSQL. Compile jobs run on threads and keep
their progress events in memory. The durable route (Temporal workflow, tracked
refs, webhooks) is `workflows.py` plus M3 work and replaces this later.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import business_graph, business_map, connectors, cross_repository, github, projection, registry, settings
from .contracts import Manifest
from .decisions import placement
from .decisions.gateway import Gateway
from .decisions.runner import Budget
from .pipeline import PublishGateError, load_manifest, stage_compile, stage_snapshot, toolchain_hash
from .store import ObjectStore

PLANES = {
    "architecture": ["Repository", "Package", "Component", "Symbol", "ExternalDependency"],
    "contracts": ["Interface", "Endpoint", "EventTopic"],
    "data": ["DataSchema", "Table", "Field", "Migration", "StorageResource"],
    "ai": ["AIComponent", "ModelEndpoint", "PromptArtifact"],
    "tests": ["Test"],
    "deployment": ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
}


class Service:
    def __init__(self, store: ObjectStore, store_root: Path, conninfo: str, gateway: Gateway | None = None, allow_local_paths: bool = False):
        self.store, self.conninfo, self.gateway, self.allow_local_paths = store, conninfo, gateway, allow_local_paths
        self._mirrors = Path(store_root) / store.tenant_id / "mirrors"
        self.jobs: dict[str, dict[str, Any]] = {}
        self.mirror_lock = threading.Lock()
        self.mirror_locks: dict[str, Any] = {}
        self.secrets = connectors.SecretReferences()
        self.app_tokens = connectors.InstallationTokens(self.secrets)
        self.device_attempts: dict[str, dict[str, Any]] = {}
        self.device_lock = threading.Lock()
        self.poll_due: dict[str, float] = {}
        self.placement_lock = threading.Lock()
        self.pending_placements: set[str | None] = set()
        self.placement_worker: threading.Thread | None = None
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            registry.import_file(conn, self.store.tenant_id, Path(store_root) / store.tenant_id / "registry.json")

    # -- registry

    def _update(self, repository_id: str, **fields: Any) -> dict[str, Any]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.update(conn, self.store.tenant_id, repository_id, **fields)

    def repositories(self) -> list[dict[str, Any]]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.all_repositories(conn, self.store.tenant_id)

    def repository(self, repository_id: str) -> dict[str, Any] | None:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return registry.read(conn, self.store.tenant_id, repository_id)

    # -- connecting and compiling

    def workspace_settings(self) -> tuple[int, settings.Settings]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return settings.read(conn, self.store.tenant_id)

    def compile_configuration(self, document: settings.Settings):
        effective = settings.effective_compile(document)
        policy = effective.model_copy(update={
            "model_functions": {k: v for k, v in effective.model_functions.items() if k == "dm.component-kind"},
            "allowed_providers": tuple(sorted(set(effective.allowed_providers) - {"openai-embeddings"})),
            "disabled_extractors": tuple(sorted(set(effective.disabled_extractors))),
        })
        return policy.gateway(self.gateway), policy

    def _connection(self, connection_id: str) -> tuple[settings.Connection | None, connectors.Connector]:
        cfg = None
        if connection_id != "github-cli":
            cfg = next((c for c in self.workspace_settings()[1].connections if c.id == connection_id), None)
            if cfg is None:
                raise KeyError(connection_id)
        return cfg, connectors.build(cfg, self.secrets, self.app_tokens)

    def browse(self, connection_id: str, page: int) -> dict[str, Any]:
        _, connector = self._connection(connection_id)
        rows = connector.repositories(page)
        return {"repositories": [
            {"selector": r.selector, "full_name": r.full_name, "default_branch": r.default_branch, "private": r.private}
            for r in rows
        ], "has_more": len(rows) == 100}

    def connection_test(self, connection_id: str) -> dict[str, Any]:
        cfg, connector = self._connection(connection_id)
        try:
            visible = connector.repositories()
            scopes = self.app_tokens.permissions.get(connection_id) if cfg and cfg.mode == "github_app" else getattr(connector, "scopes", None)
            status = {"status": "connected", "repositories_sampled": len(visible), "scopes": scopes}
        except github.GitHubError as exc:
            status = {"status": "failed", "message": str(exc)}
        status["checked_at"] = time.time()
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            conn.execute(
                """INSERT INTO connection_checks VALUES (%s,%s,%s)
                   ON CONFLICT (tenant_id,connection_id) DO UPDATE SET document=excluded.document""",
                (self.store.tenant_id, connection_id, Jsonb(status)),
            )
        return status

    def start_device(self, connection_id: str) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        if cfg is None or cfg.mode != "github_device":
            raise KeyError(connection_id)
        if cfg.credential_ref and not cfg.credential_ref.startswith("keychain:"):
            raise github.GitHubError("Device sign-in requires an OS keychain reference.")
        state = connectors.begin_device(cfg)
        state["configuration"] = cfg
        with self.device_lock:
            self.device_attempts[connection_id] = state
        return {"user_code": state["user_code"], "verification_uri": "https://github.com/login/device", "interval": state["interval"]}

    def poll_device(self, connection_id: str) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        with self.device_lock:
            state = self.device_attempts.get(connection_id)
            if state is None or state["configuration"] != cfg:
                return {"status": "expired"}
            status = connectors.finish_device(cfg, state, self.secrets)
            if status != "pending":
                self.device_attempts.pop(connection_id, None)
            return {"status": status, "interval": state["interval"]}

    def connect(self, url: str | None = None, ref: str | None = None, local_path: str | None = None, connection_id: str = "github-cli") -> dict[str, Any]:
        """Register a repository and compile one commit."""
        if local_path:
            if not self.allow_local_paths:
                raise github.GitHubError("local repositories are disabled on this server")
            return self._start_local(Path(local_path), ref)
        cfg, connector = self._connection(connection_id)
        info = connector.describe(url or "")
        tracked_ref = ref or info.default_branch
        sha, committed_at = connector.resolve(info, tracked_ref)
        self._update(
            info.repository_id, full_name=info.full_name, selector=info.selector, connection_id=connection_id,
            source=cfg.mode if cfg else "github", default_branch=info.default_branch,
            private=info.private, description=info.description,
        )
        return self._start(info.repository_id, tracked_ref, sha, lambda: connector.fetch(info, sha, self._mirrors), committed_at)

    def _remote(self, repository: dict[str, Any]) -> tuple[connectors.Connector, connectors.RemoteRepository]:
        _, connector = self._connection(repository.get("connection_id", "github-cli"))
        selector = repository.get("selector", repository["full_name"])
        if repository["repository_id"].startswith("github.com/") and not isinstance(connector, connectors.PlainGit):
            selector = repository["repository_id"].rsplit("/", 1)[-1]
        info = connector.describe(selector)
        if info.repository_id != repository["repository_id"]:
            raise github.GitHubError("The remote identity changed. Connect it as a new repository.")
        return connector, info

    def recompile(self, repository_id: str, ref: str | None = None, *, only_if_changed: bool = False) -> dict[str, Any] | None:
        repo = self.repository(repository_id)
        if repo is None:
            raise KeyError(repository_id)
        if repo["source"] == "local":
            if not self.allow_local_paths:
                raise github.GitHubError("local repositories are disabled on this server")
            return self._start_local(Path(repo["local_path"]), ref, only_if_changed=only_if_changed)
        connector, info = self._remote(repo)
        target = ref or info.default_branch
        sha, date = connector.resolve(info, target)
        if only_if_changed and self._is_current(repo, target, sha):
            return None
        return self._start(repository_id, target, sha, lambda: connector.fetch(info, sha, self._mirrors), date)

    def _is_current(self, repo: dict, ref: str, sha: str) -> bool:
        latest = max((s for s in repo["snapshots"] if s["ref"] == ref), default=None,
                     key=lambda s: s.get("requested_at", s["compiled_at"]))
        gateway, policy = self.compile_configuration(self.workspace_settings()[1])
        return bool(latest and latest["commit_sha"] == sha and latest.get("toolchain_hash") == toolchain_hash(gateway, policy))

    def poll_repositories(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                # PostgreSQL or a host may be temporarily unavailable. The next iteration retries.
                pass
            time.sleep(15)

    def poll_once(self) -> None:
        _, document = self.workspace_settings()
        for repo_id, policy in document.repositories.items():
            if policy.trigger != "poll" or self.poll_due.get(repo_id, 0) > time.monotonic():
                continue
            self.poll_due[repo_id] = time.monotonic() + 60 * policy.interval_minutes
            for ref in policy.refs:
                try:
                    self.recompile(repo_id, ref, only_if_changed=True)
                except (KeyError, github.GitHubError):
                    continue

    def webhook(self, connection_id: str, headers: Any, raw: bytes) -> dict[str, Any]:
        cfg, _ = self._connection(connection_id)
        if cfg is None:
            raise KeyError(connection_id)
        event = connectors.verify_push(cfg, headers, raw, self.secrets)
        if not event:
            return {"status": "ignored"}
        delivery, repository_id, ref, sha = event
        _, document = self.workspace_settings()
        policy, repo = document.repositories.get(repository_id), self.repository(repository_id)
        if not repo or repo.get("connection_id") != connection_id or not policy or policy.trigger != "push" or ref not in policy.refs:
            return {"status": "ignored"}
        connector, info = self._remote(repo)
        # Check the signed commit against the credential's repository visibility before fetching.
        _, date = connector.resolve(info, sha)
        key = (self.store.tenant_id, connection_id, delivery)
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            claimed = conn.execute(
                """INSERT INTO webhook_deliveries (tenant_id,connection_id,delivery_id)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING delivery_id""",
                key,
            ).fetchone()
        if not claimed:
            return {"status": "duplicate"}
        try:
            job = self._start(repository_id, ref, sha, lambda: connector.fetch(info, sha, self._mirrors), date)
        except Exception:
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                conn.execute("DELETE FROM webhook_deliveries WHERE tenant_id=%s AND connection_id=%s AND delivery_id=%s", key)
            raise
        return {"status": "queued", "job_id": job["id"]}

    def _start_local(self, path: Path, ref: str | None, *, only_if_changed: bool = False) -> dict[str, Any] | None:
        import subprocess
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            raise github.GitHubError("not a Git repository")
        try:
            sha = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--verify", "--end-of-options", f"{connectors.checked_ref(ref or 'HEAD')}^{{commit}}"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise github.GitHubError("Local Git ref was not found.") from None
        repository_id = "local/" + uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve())).hex[:12]
        previous = self.repository(repository_id)
        if only_if_changed and self._is_current(previous or {"snapshots": []}, ref or "HEAD", sha):
            return None
        self._update(repository_id, full_name=path.name, source="local", local_path=str(path.resolve()),
                     default_branch=(previous or {}).get("default_branch", ref or "HEAD"), private=True, description="Local repository")
        committed_at = subprocess.run(["git", "-C", str(path), "show", "-s", "--format=%cI", sha], check=True, capture_output=True, text=True).stdout.strip() or None
        return self._start(repository_id, ref or "HEAD", sha, lambda: path, committed_at)

    def _start(self, repository_id: str, ref: str, sha: str, obtain: Any, committed_at: str | None = None) -> dict[str, Any]:
        configuration = self.compile_configuration(self.workspace_settings()[1])
        job = {"id": "cmp_" + uuid.uuid4().hex[:16], "repository_id": repository_id, "ref": ref, "commit_sha": sha, "committed_at": committed_at, "status": "queued", "rkp_hash": None, "error": None, "events": [], "started_at": time.time(), "finished_at": None}
        self.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job, obtain, configuration), daemon=True, name=job["id"]).start()
        return job

    def _run(self, job: dict[str, Any], obtain: Any, configuration: Any) -> None:
        def tell(stage: str, detail: dict[str, Any] | None = None) -> None:
            job["events"].append({"seq": len(job["events"]), "stage": stage, "at": time.time(), **(detail or {})})

        try:
            job["status"] = "running"
            tell("fetch")
            with self.mirror_lock:
                lock = self.mirror_locks.setdefault(job["repository_id"], threading.Lock())
            with lock:
                repo_path = obtain()
            pm = stage_snapshot(self.store, repo_path, job["commit_sha"])
            gateway, policy = configuration
            result = stage_compile(self.store, job["repository_id"], pm, gateway=gateway, progress=tell, policy=policy)
            tell("project")
            repository = self.repository(job["repository_id"])
            old = next((s for s in reversed(repository["snapshots"]) if s["ref"] == job["ref"]), None)
            previous = load_manifest(self.store, rkp_hash=old["rkp_hash"]) if old else None
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                projection.project_snapshot(conn, self.store, result.manifest)
                business_graph.record_lineage(conn, self.store.tenant_id, self.store, result.manifest, previous)
            job["rkp_hash"] = result.manifest.rkp_hash
            self._update(job["repository_id"], snapshot={"rkp_hash": result.manifest.rkp_hash, "commit_sha": job["commit_sha"], "ref": job["ref"], "committed_at": job["committed_at"], "compiled_at": time.time(), "requested_at": job["started_at"], "toolchain_hash": result.manifest.toolchain_hash})
            tell("complete", {"rkp_hash": result.manifest.rkp_hash})
            job["status"] = "complete"
            self.queue_placement(job["repository_id"])
        except PublishGateError as exc:
            job["status"], job["error"] = "failed", "the snapshot did not pass the publish gate"
            tell("failed", {"problems": exc.problems[:20]})
        except github.GitHubError as exc:
            job["status"], job["error"] = "failed", str(exc)
            tell("failed")
        except Exception as exc:  # report the kind of failure, not repository content
            job["status"], job["error"] = "failed", f"compile failed: {type(exc).__name__}"
            tell("failed")
        finally:
            job["finished_at"] = time.time()

    def settings_view(self) -> dict[str, Any]:
        version, document = self.workspace_settings()
        gateway, compiler = self.compile_configuration(document)
        current_toolchain = toolchain_hash(gateway, compiler)
        repositories = self.repositories()
        for repo in repositories:
            repo["snapshots"] = [
                snap | {"stale": snap.get("toolchain_hash") != current_toolchain}
                for snap in repo["snapshots"]
            ]
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            checks = dict(conn.execute(
                "SELECT connection_id,document FROM connection_checks WHERE tenant_id=%s",
                (self.store.tenant_id,),
            ))
            hooks = dict(conn.execute(
                "SELECT connection_id,extract(epoch from max(received_at)) FROM webhook_deliveries WHERE tenant_id=%s GROUP BY connection_id",
                (self.store.tenant_id,),
            ))
            author = conn.execute(
                "SELECT author,updated_at FROM workspace_settings_revisions WHERE tenant_id=%s AND version=%s",
                (self.store.tenant_id, version),
            ).fetchone()
        from .extractors import EXTRACTORS
        return {
            "version": version, "settings": document.row(), "environment": settings.environment_settings(),
            "available_models": gateway.describe() if gateway else {},
            "repositories": repositories, "checks": checks, "last_webhook": hooks,
            "author": author[0] if author else None, "updated_at": author[1] if author else None,
            "extractors": [{
                "id": e.id, "version": e.version,
                "tier": "inventory" if e.id in {"manifest.maven", "manifest.go"} else ("A" if e.id in {"python.source", "typescript.source", "contract.openapi"} else "B"),
            } for e in EXTRACTORS],
        }

    def map_document(self) -> business_map.BusinessMap:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return business_map.read(conn, self.store.tenant_id)

    def _business_snapshots(self, repository_id: str | None = None):
        rows = [r for r in self.repositories() if repository_id is None or r["repository_id"] == repository_id]
        for repo, snap in business_graph.latest(rows):
            manifest = load_manifest(self.store, rkp_hash=snap["rkp_hash"])
            if manifest is None:
                continue
            entities, edges = business_graph.graph(self.store, manifest)
            with projection.connect(self.conninfo, self.store.tenant_id) as conn:
                aliases = business_graph.identities(conn, self.store.tenant_id, manifest.rkp_hash)
            subjects = business_graph.describe_subjects(repo, manifest, entities, edges, aliases)
            yield manifest, entities, edges, aliases, subjects

    def map_view(self) -> dict[str, Any]:
        current = self.map_document()
        assigned = {row.lineage_id: row.capability_id for row in current.placements if row.status in ("confirmed", "ai") and row.capability_id}
        subjects, dependencies = [], {}
        snapshots = list(self._business_snapshots())
        for manifest, entities, edges, aliases, found in snapshots:
            subjects.extend({k: v for k, v in s.items() if k not in ("features", "evidence_ids")} for s in found)
            for edge in business_graph.capability_dependencies(assigned, entities, edges, aliases, manifest.rkp_hash):
                key = edge["from"], edge["to"]
                if key not in dependencies:
                    dependencies[key] = edge
                else:
                    dependencies[key]["evidence"].extend(edge["evidence"])
                    if edge["resolution"] != "exact":
                        dependencies[key]["resolution"] = "candidate"
        entries = {s["entity_id"]: s["lineage_id"] for s in subjects}
        links, unknowns = cross_repository.candidates(self.store, snapshots)
        for link in links:
            start = assigned.get(entries.get(link["from"], link["from"]))
            end = assigned.get(entries.get(link["to"], link["to"]))
            if start and end and start != end:
                edge = dependencies.setdefault((start, end), {"from": start, "to": end, "resolution": "candidate", "evidence": []})
                edge["resolution"] = "candidate"
                edge["evidence"].extend(item | {"assertion_id": link["id"], "predicate": link["predicate"]} for item in link["evidence"])
        _, config = self.workspace_settings()
        return {
            "cross_repository_links": links, "cross_repository_unknowns": unknowns,
            "map": current.row(), "subjects": subjects,
            "dependencies": list(dependencies.values()),
            "eligible_kinds": config.business_map.component_kinds,
        }

    def queue_placement(self, repository_id: str | None = None) -> None:
        with self.placement_lock:
            self.pending_placements.add(repository_id)
            if self.placement_worker is None:
                self.placement_worker = threading.Thread(target=self._drain_placement, daemon=True, name="business-placement")
                self.placement_worker.start()

    def _drain_placement(self) -> None:
        while True:
            with self.placement_lock:
                if not self.pending_placements:
                    self.placement_worker = None
                    return
                if None in self.pending_placements:
                    target = None
                    self.pending_placements.clear()
                else:
                    target = self.pending_placements.pop()
            for _ in range(2):
                try:
                    self.run_placement(repository_id=target)
                except business_map.StaleVersion:
                    continue
                except Exception:
                    # A placement outage must leave the published RKP usable.
                    break
                else:
                    break

    def run_placement(self, *, repository_id: str | None = None) -> business_map.BusinessMap:
        current = self.map_document()
        if not current.capabilities():
            return current
        _, document = self.workspace_settings()
        config = settings.effective_compile(document)
        gateway = config.gateway(self.gateway)
        subjects, hints = [], {}
        for manifest, _, _, _, found in self._business_snapshots(repository_id):
            subjects.extend(found)
            declared = business_graph.declared_hints(self.store, manifest)
            by_name: dict[str, list[dict]] = {}
            for subject in found:
                if subject["kind"] == "Component":
                    by_name.setdefault(subject["name"], []).append(subject)
            for name, value in declared.items():
                if len(by_name.get(name, ())) == 1:
                    hints[by_name[name][0]["lineage_id"]] = value
        rows = placement.place(
            current, subjects, hints, document.business_map,
            config.model_functions.get(placement.SPEC_ID, settings.ModelSelection()),
            self.store, gateway, config.allowed_providers, Budget(max_calls=config.decision_budget),
        )
        if rows == current.placements:
            return current
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            return business_map.save(conn, self.store.tenant_id, current.model_copy(update={"placements": rows}), "placement")

    def map_for_snapshot(self, manifest: Manifest) -> dict[str, Any]:
        current = self.map_document()
        if not current.placements:
            return {"components": {}}
        capabilities = current.capabilities()
        entities, _ = business_graph.graph(self.store, manifest)
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            aliases = business_graph.identities(conn, self.store.tenant_id, manifest.rkp_hash)
        rows = {row.lineage_id: row for row in current.placements}
        return {
            "components": {
                entity_id: row.row() | {"capability": capabilities[row.capability_id].name if row.capability_id in capabilities else None}
                for entity_id, entity in entities.items() if entity["kind"] in business_graph.SUBJECT_KINDS
                if (row := rows.get(aliases.get(entity_id, entity_id))) is not None
            },
            "version": current.version,
            "capabilities": [{"id": cap.id, "name": cap.name} for cap in capabilities.values()],
        }

    def dependency_groups(self, manifest: Manifest) -> dict[str, Any]:
        entities, edges = business_graph.graph(self.store, manifest)
        memberships: dict[str, list] = {}
        for edge in edges.values():
            if edge["predicate"] == "PROVIDES":
                memberships.setdefault(edge["object"], []).append(entities[edge["subject"]])
        classified = {row["id"] for rows in memberships.values() for row in rows}
        groups = [{
            "capability": entities[key], "dependencies": sorted(rows, key=lambda r: (r["name"], r["id"])),
        } for key, rows in memberships.items()]
        other = sorted((row for key, row in entities.items() if row["kind"] == "ExternalDependency" and key not in classified), key=lambda r: (r["name"], r["id"]))
        return {"groups": sorted(groups, key=lambda g: g["capability"]["name"]), "unclassified": other}

    # -- reading a snapshot

    def summary(self, manifest: dict[str, Any]) -> dict[str, Any]:
        with projection.connect(self.conninfo, self.store.tenant_id) as conn:
            counts = projection.count_by_kind(conn, self.store.tenant_id, manifest["rkp_hash"])
            repo_entity = projection.list_entities(conn, self.store.tenant_id, manifest["rkp_hash"], ["Repository"], None, 1)
        coverage = self.store.get_json(manifest["coverage_ref"])
        unknowns = self.store.get_json(manifest["unknowns_ref"])
        conflicts = self.store.get_json(manifest["conflicts_ref"])
        totals: dict[str, list[int]] = {}
        for c in coverage:
            t = totals.setdefault(c["domain"], [0, 0])
            t[0] += c["numerator"]
            t[1] += c["denominator"]
        return {
            "manifest": manifest,
            "topology": (repo_entity[0]["attributes"].get("topology") if repo_entity else None),
            "counts_by_kind": counts,
            "planes": {plane: {"kinds": {k: counts.get(k, 0) for k in kinds}, "total": sum(counts.get(k, 0) for k in kinds)} for plane, kinds in PLANES.items()},
            "files": counts.get("Artifact", 0),
            "coverage_totals": {d: {"numerator": n, "denominator": m} for d, (n, m) in sorted(totals.items())},
            "coverage": coverage,
            "unknowns": unknowns,
            "conflicts": conflicts,
        }

    def decisions(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        entries = self.store.get_json(manifest["ledger_entries_ref"])
        return [self.store.get_json(e["digest"]) for e in entries[:1000]]
