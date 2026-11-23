"""Workspace API, compiler settings and overlay behavior against local snapshots."""

import hashlib
import hmac
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from repo_intel import business_map, projection, registry
from repo_intel.api import create_app
from repo_intel.decisions.gateway import Gateway, ScriptedModel
from repo_intel.fixtures import materialize
from repo_intel.service import Service
from repo_intel.store import ObjectStore
from test_service_api import AUTH, TOKEN, _finish


@pytest.fixture
def workspace(tmp_path, pg_conninfo):
    with psycopg.connect(pg_conninfo) as admin:
        projection.ensure_schema(admin)
        projection.truncate_all(admin)
    conninfo = pg_conninfo.replace("user=rkp ", f"user={projection.APP_ROLE} ")
    store = ObjectStore(tmp_path / "objects", "tenant-a")
    model = ScriptedModel(lambda state, labels: "sales" if "sales" in labels else "none_of_these")
    gateway = Gateway({"decision-default": model}, frozenset({"scripted"}))
    service = Service(store, tmp_path / "objects", conninfo, allow_local_paths=True, gateway=gateway)
    return TestClient(create_app(conninfo, store.tenant_id, TOKEN, store, service)), service, model


def _get(client, path):
    response = client.get("/v1/" + path, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def _post(client, path, body):
    response = client.post("/v1/" + path, headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _create_map(client):
    return _post(client, "business-maps", {"expected_version": 0, "title": "Value chain", "stages": [{
        "id": "sell", "name": "Sell", "capabilities": [
            {"id": "sales", "name": "Sales", "description": "Process orders and manage customers."},
            {"id": "inventory", "name": "Inventory", "description": "Manage warehouse items."},
        ],
    }]})


def _local(client, path, ref):
    job = _post(client, "repositories", {"local_path": str(path), "ref": ref})
    done = _finish(client, job["id"])
    assert done["status"] == "complete", done
    return done


def _commit(path: Path, files: dict[str, str], message: str) -> str:
    for name, value in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.com", "commit", "-qm", message], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_optimistic_map_write_and_tenant_isolation(workspace):
    client, service, _ = workspace
    result = _create_map(client)["map"]
    assert result["version"] == 1 and result["author"] == "local-admin"
    conflict = client.post("/v1/business-maps", headers=AUTH, json={"expected_version": 0, "title": "Stale"})
    assert conflict.status_code == 409
    assert client.post("/v1/business-maps", json={"expected_version": 1, "title": "Anonymous"}).status_code == 401
    assert [r["version"] for r in _get(client, "business-maps/revisions")["revisions"]] == [1]
    with projection.connect(service.conninfo, "tenant-b") as conn:
        assert business_map.read(conn, "tenant-a").version == 0
        assert conn.execute("SELECT count(*) FROM business_map_capabilities").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            business_map.save(conn, "tenant-a", business_map.BusinessMap(id="second"), "intruder")


def test_placement_confirmation_and_rkp_independence(workspace, tmp_path):
    client, service, model = workspace
    repo = tmp_path / "fixture-repo"
    shas = materialize("F01-python-api", repo)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/tracked", shas[0]], check=True)
    first = _local(client, repo, "tracked")
    _create_map(client)
    first_result = _post(client, "business-maps/run-placement", {})["map"]
    assert len(first_result["placements"]) == 1
    ai, = first_result["placements"]
    assert ai["status"] == "ai" and ai["capability_id"] == "sales"
    calls = len(model.calls)
    repeat = _post(client, "business-maps/run-placement", {})["map"]
    assert len(model.calls) == calls
    assert repeat["version"] == first_result["version"]
    reviewed = _post(client, "business-maps/placements", {
        "expected_version": repeat["version"], "lineage_id": ai["lineage_id"], "action": "move",
        "capability_id": "inventory",
    })["map"]
    assert reviewed["placements"][0]["status"] == "confirmed"
    assert reviewed["placements"][0]["rejected"] == ["sales"]
    lookup = _get(client, f"snapshots/{first['rkp_hash']}/placements")
    assert lookup["components"][ai["lineage_id"]]["capability"] == "Inventory"

    second = _local(client, repo, "tracked")
    assert second["rkp_hash"] == first["rkp_hash"]
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/tracked", shas[1]], check=True)
    third = _local(client, repo, "tracked")
    assert third["rkp_hash"] != first["rkp_hash"]
    assert _post(client, "business-maps/run-placement", {})["map"]["placements"][0]["status"] == "confirmed"
    comparison = client.get("/v1/knowledge/diff", headers=AUTH, params={"from": first["rkp_hash"], "to": third["rkp_hash"]})
    assert comparison.status_code == 200


def test_settings_pin_compiler_inputs_and_offer_suggestions(workspace, f01):
    client, _, _ = workspace
    repo, shas = f01
    original = _local(client, repo, shas[0])
    current = _get(client, "settings")
    document = current["settings"]
    document["compile"]["model_functions"]["dm.component-kind"] = {"threshold": 0.99}
    document["business_map"]["auto_place"] = False
    updated = _post(client, "settings", document | {"expected_version": current["version"]})
    assert updated["version"] == 1 and updated["repositories"][0]["snapshots"][0]["stale"]
    assert client.post("/v1/settings", headers=AUTH, json=document | {"expected_version": 0}).status_code == 409
    fresh = _local(client, repo, shas[0])
    assert original["rkp_hash"] != fresh["rkp_hash"]
    assert not _get(client, "settings")["repositories"][0]["snapshots"][-1]["stale"]

    _create_map(client)
    row, = _post(client, "business-maps/run-placement", {})["map"]["placements"]
    assert row["status"] == "suggested" and row["capability_id"] == "sales"


def test_legacy_registry_import_keeps_history(workspace, tmp_path):
    _, service, _ = workspace
    repo_id = "github.com/908"
    path = tmp_path / "old-registry.json"
    original = {"repository_id": repo_id, "full_name": "example/repo", "snapshots": [{"rkp_hash": "old"}]}
    path.write_text(json.dumps({"repositories": {repo_id: original}}))
    with projection.connect(service.conninfo, "tenant-a") as conn:
        registry.import_file(conn, "tenant-a", path)
        assert registry.read(conn, "tenant-a", repo_id) == original
        registry.update(conn, "tenant-a", repo_id, full_name="example/renamed")
        registry.import_file(conn, "tenant-a", path)
        row = registry.read(conn, "tenant-a", repo_id)
        assert row["full_name"] == "example/renamed" and row["snapshots"] == original["snapshots"]


def test_signed_github_push_compiles_registered_repo_once(workspace, f01):
    client, service, _ = workspace
    path, shas = f01
    document = _get(client, "settings")["settings"]
    document["connections"] = [{
        "id": "app", "name": "App", "mode": "github_app", "host": "github.com", "app_id": "101", "installation_id": "202",
        "credential_ref": "keychain:app-key", "webhook_ref": "keychain:hook-secret",
    }]
    doc = _post(client, "settings", document | {"expected_version": 0})
    repo = "github.com/303"
    service._update(repo, full_name="example/repo", source="github_app", connection_id="app", default_branch="main", private=True)
    document["repositories"] = {repo: {"refs": ["main"], "trigger": "push"}}
    _post(client, "settings", document | {"expected_version": doc["version"]})
    data = json.dumps({"ref": "refs/heads/main", "after": shas[0], "installation": {"id": 202}, "repository": {"id": 303}}).encode()
    key = "webhook-secret-for-tests"
    headers = {"X-GitHub-Delivery": "delivery-1", "X-GitHub-Event": "push"}
    with patch.object(service.secrets, "get", return_value=key), patch.object(service, "_remote") as remote:
        connector, _ = remote.return_value = (type("MockConnector", (), {
            "resolve": lambda self, info, ref: (shas[0], None), "fetch": lambda self, *args: path,
        })(), None)
        denied = client.post("/v1/webhooks/app", headers=headers, content=data)
        assert denied.status_code == 403
        remote.assert_not_called()
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(key.encode(), data, hashlib.sha256).hexdigest()
        push = client.post("/v1/webhooks/app", headers=headers, content=data)
        assert push.status_code == 202 and push.json()["status"] == "queued", push.text
        assert _finish(client, push.json()["job_id"])["status"] == "complete"
        duplicate = client.post("/v1/webhooks/app", headers=headers, content=data)
        assert duplicate.json()["status"] == "duplicate"
    assert key not in json.dumps(_get(client, "settings"))


def test_cross_repository_candidates_and_declared_suggestion(workspace, tmp_path):
    client, _, _ = workspace
    provider, consumer = tmp_path / "provider", tmp_path / "consumer"
    for repo in (provider, consumer):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    provider_sha = _commit(provider, {
        "package.json": '{"name":"@test/provider","version":"1.0.0","dependencies":{"express":"4"}}',
        "index.ts": "import express from 'express'; const app = express(); app.get('/catalog', (req, res) => res.send('ok'));",
        "openapi.yaml": "openapi: 3.0.0\ninfo:\n  title: Catalog\n  version: 1\npaths:\n  /catalog:\n    get:\n      operationId: listCatalog\n      responses:\n        '200':\n          description: OK\n",
    }, "provider")
    consumer_sha = _commit(consumer, {
        "package.json": '{"name":"consumer","private":true,"dependencies":{"express":"4","@test/provider":"^1"}}',
        "index.ts": "import express from 'express'; const app = express(); app.get('/orders', async (req, res) => { await fetch('/catalog'); await fetch('/unresolved'); res.send('ok'); });",
        ".rkp/business.yaml": "placements:\n  consumer: sales\n",
    }, "consumer")
    _local(client, provider, provider_sha)
    _local(client, consumer, consumer_sha)
    _create_map(client)
    data = _post(client, "business-maps/run-placement", {})
    assert {edge["predicate"] for edge in data["cross_repository_links"]} == {"DEPENDS_ON", "CONSUMES"}
    assert {edge["resolution"] for edge in data["cross_repository_links"]} == {"candidate"}
    assert [call["path"] for call in data["cross_repository_unknowns"]] == ["/unresolved"]
    sources = {row["lineage_id"]: row["name"] for row in data["subjects"]}
    found = {sources[row["lineage_id"]]: row for row in data["map"]["placements"]}
    assert found["consumer"]["status"] == "declared" and found["consumer"]["evidence_ids"]


def test_gitlab_rest_browse_connect_and_push(workspace, f01, monkeypatch):
    from repo_intel import connectors

    client, service, _ = workspace
    fixture, shas = f01
    token = "sample-gitlab-token-for-tests"
    project = {
        "id": 55, "path_with_namespace": "tools/fixture", "default_branch": "main",
        "visibility": "private", "http_url_to_repo": "https://gitlab.example/tools/fixture.git",
    }

    def get(url, headers=None, form=None):
        assert headers == {"PRIVATE-TOKEN": token}
        url = url.removeprefix("https://gitlab.example/api/v4")
        if url.startswith("/projects?"):
            return [project], {}
        if "/repository/commits/" in url:
            return {"id": shas[0], "committed_date": "2026-01-01T00:00:00Z"}, {}
        assert url == "/projects/55"
        return project, {}

    monkeypatch.setattr(connectors, "request_json", get)
    monkeypatch.setattr(service.secrets, "get", lambda ref: token)
    monkeypatch.setattr(connectors.AuthenticatedGit, "fetch", lambda self, *args: fixture)
    original = _get(client, "settings")
    document = original["settings"]
    document["connections"] = [{
        "id": "gitlab", "name": "GitLab", "mode": "gitlab_token", "host": "gitlab.example",
        "credential_ref": "keychain:gitlab", "webhook_ref": "keychain:gitlab-hook",
    }]
    _post(client, "settings", document | {"expected_version": 0})
    discovered, = _get(client, "settings/connections/gitlab/repositories")["repositories"]
    assert discovered == {"selector": "55", "full_name": "tools/fixture", "default_branch": "main", "private": True}
    job = _post(client, "repositories", {"url": discovered["selector"], "connection_id": "gitlab"})
    assert job["repository_id"] == "gitlab.example/55"
    assert _finish(client, job["id"])["status"] == "complete"

    document["repositories"] = {job["repository_id"]: {"refs": ["main"], "trigger": "push"}}
    _post(client, "settings", document | {"expected_version": 1})
    headers = {"X-Gitlab-Event": "Push Hook", "X-Gitlab-Event-UUID": "fixture-gitlab-event", "X-Gitlab-Token": token}
    event = {"project": {"id": 55}, "ref": "refs/heads/main", "after": shas[0]}
    receipt = client.post("/v1/webhooks/gitlab", headers=headers, json=event)
    assert receipt.status_code == 202
    assert _finish(client, receipt.json()["job_id"])["status"] == "complete"
    assert token not in json.dumps(_get(client, "settings"))


def test_plain_git_fetches_pinned_commit_and_poll_discovers_new_tip(workspace, tmp_path, monkeypatch):
    """Git runs its real ls-remote and shallow fetch using a fixture SSH transport."""
    import os
    import shlex

    client, service, _ = workspace
    repo = tmp_path / "remote fixture"
    shas = materialize("F01-python-api", repo)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/tracked", shas[0]], check=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/tracked"], check=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text(f'#!/bin/sh\nif [ "$1" = "-G" ]; then exit 0; fi\nexec git upload-pack {shlex.quote(str(repo))}\n')
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(service.secrets, "get", lambda ref: "fixture private key placeholder")
    document = _get(client, "settings")["settings"]
    document["connections"] = [{
        "id": "plain", "name": "Plain Git", "mode": "git_ssh", "host": "forge.example",
        "credential_ref": "keychain:deploy", "remote": "ssh://git@forge.example/payments.git",
    }]
    _post(client, "settings", document | {"expected_version": 0})
    discovered, = _get(client, "settings/connections/plain/repositories")["repositories"]
    assert discovered["default_branch"] == "tracked"
    started = _post(client, "repositories", {"url": discovered["selector"], "connection_id": "plain"})
    initial = _finish(client, started["id"])
    assert initial["status"] == "complete" and initial["commit_sha"] == shas[0], initial
    mirror, = service._mirrors.glob("*.git")
    assert subprocess.check_output(["git", "-C", str(mirror), "rev-parse", "--is-bare-repository"], text=True).strip() == "true"
    assert subprocess.check_output(["git", "-C", str(mirror), "rev-parse", "--is-shallow-repository"], text=True).strip() == "true"

    document["repositories"] = {started["repository_id"]: {"refs": ["tracked"], "trigger": "poll", "interval_minutes": 1}}
    _post(client, "settings", document | {"expected_version": 1})
    service.poll_once()
    assert len(service.jobs) == 1
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/tracked", shas[1]], check=True)
    service.poll_due.clear()
    service.poll_once()
    next_id, = set(service.jobs) - {started["id"]}
    final = _finish(client, next_id)
    assert final["status"] == "complete" and final["commit_sha"] == shas[1], final


def test_placement_is_inherited_after_a_component_folder_move(workspace, tmp_path):
    client, _, _ = workspace
    repo = tmp_path / "moving-service"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _commit(repo, {
        "old/pyproject.toml": '[project]\nname = "orders-api"\nversion = "1.0.0"\ndependencies = ["fastapi"]\n',
        "old/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/orders')\ndef orders(): return []\n",
    }, "initial service")
    first = _local(client, repo, "HEAD")
    _create_map(client)
    view = _post(client, "business-maps/run-placement", {})
    component, = [row for row in view["subjects"] if row["kind"] == "Component"]
    original = component["entity_id"]
    _post(client, "business-maps/placements", {
        "expected_version": view["map"]["version"], "lineage_id": component["lineage_id"], "action": "confirm",
    })
    subprocess.run(["git", "-C", str(repo), "mv", "old", "new"], check=True)
    _commit(repo, {}, "move the service")
    second = _local(client, repo, "HEAD")
    assert second["rkp_hash"] != first["rkp_hash"]
    moved = _get(client, "business-maps")
    component, = [row for row in moved["subjects"] if row["kind"] == "Component"]
    row, = moved["map"]["placements"]
    assert component["entity_id"] != original and component["lineage_id"] == original == row["lineage_id"]
    assert row["status"] == "confirmed"


def test_placement_none_and_model_policy_are_cached_separately(store):
    from repo_intel.decisions import placement
    from repo_intel.decisions.runner import Budget
    from repo_intel.settings import ModelSelection, PlacementPolicy

    draft = business_map.BusinessMap(stages=(business_map.Stage(
        id="stage", name="Stage", capabilities=(business_map.Capability(id="sales", name="Sales"),),
    ),))
    subjects = [{
        "lineage_id": "ent_test", "kind": "Component", "component_kind": "service", "rkp_hash": "snapshot-1",
        "features": {"name": "service"}, "evidence_ids": ["ev_test"],
    }]
    decline = ScriptedModel(lambda state, labels: "none_of_these")
    gateway = Gateway({"decision-default": decline}, frozenset({"scripted"}))
    first, = placement.place(draft, subjects, {}, PlacementPolicy(), ModelSelection(), store, gateway, (), Budget())
    repeat, = placement.place(draft, subjects, {}, PlacementPolicy(), ModelSelection(), store, gateway, (), Budget())
    assert first == repeat and first.reason == "none_of_these" and len(decline.calls) == 3

    choose = ScriptedModel(lambda state, labels: {"sales": 0.95, "none_of_these": 0.05}, provider="other")
    gateway = Gateway({"decision-default": choose}, frozenset({"other"}))
    accepted, = placement.place(draft, subjects, {}, PlacementPolicy(), ModelSelection(), store, gateway, (), Budget())
    strict, = placement.place(
        draft, subjects, {}, PlacementPolicy(), ModelSelection(threshold=0.99), store, gateway, (), Budget(),
    )
    assert accepted.status == "ai" and strict.reason == "below_threshold"
    assert len(choose.calls) == 6 and strict.decision_ref != accepted.decision_ref


@pytest.mark.parametrize("mode", ["github_pat", "github_device"])
def test_github_token_connection_browse_and_compile(workspace, f01, monkeypatch, mode):
    from repo_intel import connectors

    client, service, _ = workspace
    fixture, shas = f01
    token = "github-access-token-for-fixture"
    state = {"keychain:fixture": token} if mode == "github_pat" else {}
    remote = {"id": 88, "full_name": "tests/fixture", "default_branch": "main", "private": True}

    def request(url, headers=None, form=None):
        if url.endswith("/login/device/code"):
            assert form["client_id"] == "fixtureOAuthId"
            return {"device_code": "server-only-code", "user_code": "ABCD-1234", "interval": 5, "expires_in": 300}, {}
        if url.endswith("/login/oauth/access_token"):
            assert form["device_code"] == "server-only-code"
            return {"access_token": token}, {}
        assert headers["Authorization"] == f"Bearer {token}"
        if "/user/repos?" in url:
            return [remote], {}
        if "/commits/" in url:
            return {"sha": shas[0]}, {}
        assert url.endswith("/repos/tests/fixture")
        return remote, {}

    monkeypatch.setattr(connectors, "request_json", request)
    monkeypatch.setattr(service.secrets, "get", lambda ref: state[ref])
    monkeypatch.setattr(service.secrets, "put_device_token", state.__setitem__)
    monkeypatch.setattr(connectors.AuthenticatedGit, "fetch", lambda self, *args: fixture)
    settings = _get(client, "settings")["settings"]
    settings["connections"] = [{
        "id": "github", "name": "GitHub fixture", "mode": mode, "host": "github.com", "credential_ref": "keychain:fixture",
        "client_id": "fixtureOAuthId" if mode == "github_device" else None,
    }]
    _post(client, "settings", settings | {"expected_version": 0})
    if mode == "github_device":
        response = _post(client, "settings/connections/github/device", {})
        assert response["user_code"] == "ABCD-1234" and "device_code" not in response
        assert _post(client, "settings/connections/github/device/poll", {})["status"] == "pending"
        service.device_attempts["github"]["next_poll"] = 0
        assert _post(client, "settings/connections/github/device/poll", {})["status"] == "complete"
        assert state["keychain:fixture"] == token
    listed, = _get(client, "settings/connections/github/repositories")["repositories"]
    job = _post(client, "repositories", {"connection_id": "github", "url": listed["selector"], "ref": "main"})
    assert job["repository_id"] == "github.com/88" and _finish(client, job["id"])["status"] == "complete"
    assert token not in json.dumps(_get(client, "settings"))


def test_installation_token_signs_app_jwt_and_caches_credential(monkeypatch):
    import base64
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from repo_intel import connectors
    from repo_intel.settings import Connection

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    cfg = Connection(id="app", name="App", mode="github_app", host="github.com", credential_ref="keychain:app", app_id="42", installation_id="77")
    calls = []
    secrets = connectors.SecretReferences()
    monkeypatch.setattr(secrets, "get", lambda ref: key)

    def request(url, headers, form):
        calls.append(url)
        parts = headers["Authorization"].removeprefix("Bearer ").split(".")
        data = [base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)) for part in parts]
        private.public_key().verify(data[2], ".".join(parts[:2]).encode(), padding.PKCS1v15(), hashes.SHA256())
        assert json.loads(data[1])["iss"] == "42"
        assert url.endswith("/app/installations/77/access_tokens")
        return {
            "token": "fake-installation-credential",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            "permissions": {"contents": "read"},
        }, {}

    monkeypatch.setattr(connectors, "request_json", request)
    client = connectors.InstallationTokens(secrets)
    assert client.get(cfg) == client.get(cfg) == "fake-installation-credential"
    assert len(calls) == 1 and client.permissions["app"] == ["contents:read"]
