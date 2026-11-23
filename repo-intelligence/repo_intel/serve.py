"""Run the whole backend on this machine with one command.

    python -m repo_intel.serve

Starts (and on first run creates) a private PostgreSQL cluster under the data
directory, applies the schema, and serves the API on 127.0.0.1 only. A random
development token is created in <data dir>/dev-token unless RKP_DEV_TOKEN is set.
Environment: RKP_DATA_DIR (default ./.rkp-store), RKP_API_PORT (8787),
RKP_PG_PORT (54329), RKP_ALLOW_LOCAL=1 to allow compiling local repositories.
Decision models are used only if their credentials are set AND the provider is
listed in RKP_ALLOW_PROVIDERS (comma-separated); otherwise ambiguous decisions abstain.

Provider settings that are not secrets (endpoints, deployments, `azure-cli`
sign-in, the allow list and the default alias) can be kept in
<data dir>/providers.env as KEY=value lines so a workspace keeps its model
across restarts. API keys are refused there; set them in the environment.
The process environment wins over the file.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
from pathlib import Path

import psycopg
import uvicorn

from . import projection
from .api import create_app
from .decisions.gateway import Gateway
from .service import Service
from .store import ObjectStore

TENANT = "local"
PROVIDER_FILE_KEYS = frozenset({
    "RKP_ALLOW_PROVIDERS", "RKP_DECISION_DEFAULT", "RKP_JEV_MODEL",
    "RKP_MSDECISION_ENDPOINT", "RKP_MSDECISION_DEPLOYMENT", "RKP_MSDECISION_AUTH",
    "RKP_COHERE_RERANK_ENDPOINT", "RKP_COHERE_RERANK_DEPLOYMENT", "RKP_COHERE_RERANK_AUTH",
    "RKP_AZURE_OPENAI_ENDPOINT", "RKP_AZURE_OPENAI_DEPLOYMENT", "RKP_AZURE_OPENAI_AUTH",
    "RKP_EMBEDDING_URL", "RKP_EMBEDDING_MODEL", "RKP_EMBEDDING_AUTH",
})


def load_provider_file(data_dir: Path, env: dict | None = None) -> list[str]:
    """Apply <data dir>/providers.env to `env` without overriding what is set. Returns the keys applied."""
    env = os.environ if env is None else env
    path = data_dir / "providers.env"
    if not path.is_file():
        return []
    applied = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not sep or key not in PROVIDER_FILE_KEYS:
            # Never echo the line: a pasted key must not reach the log.
            raise SystemExit(f"{path}:{number}: only non-secret provider settings belong here ({', '.join(sorted(PROVIDER_FILE_KEYS))}); set API keys in the environment.")
        if key not in env:
            env[key] = value
            applied.append(key)
    return applied


def start_postgres(data_dir: Path, port: int) -> str:
    cluster = data_dir / "postgres"
    env = {**os.environ, "LC_ALL": "C"}
    if not (cluster / "PG_VERSION").exists():
        cluster.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["initdb", "-D", str(cluster), "-U", "rkp", "--auth=trust", "-E", "UTF8"], check=True, capture_output=True, env=env)
    running = subprocess.run(["pg_ctl", "-D", str(cluster), "status"], capture_output=True, env=env).returncode == 0
    if not running:
        opts = f"-c listen_addresses=127.0.0.1 -c port={port} -c unix_socket_directories=''"
        subprocess.run(["pg_ctl", "-D", str(cluster), "-o", opts, "-w", "-l", str(cluster / "server.log"), "start"], check=True, capture_output=True, env=env)
        atexit.register(lambda: subprocess.run(["pg_ctl", "-D", str(cluster), "-m", "fast", "stop"], capture_output=True, env=env))
    admin = f"host=127.0.0.1 port={port} user=rkp dbname=postgres"
    with psycopg.connect(admin) as conn:
        projection.ensure_schema(conn)
    return f"host=127.0.0.1 port={port} user={projection.APP_ROLE} dbname=postgres"  # the API connects as the restricted role


def dev_token(data_dir: Path) -> str:
    """A random local token, created once and readable only by this user. The Explorer's
    server side reads the same file, so the token never has to be typed or shown."""
    path = data_dir / "dev-token"
    if not path.exists():
        import secrets as _secrets
        data_dir.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(_secrets.token_urlsafe(32))
    return path.read_text().strip()


def main() -> int:
    data_dir = Path(os.environ.get("RKP_DATA_DIR", ".rkp-store")).resolve()
    token = os.environ.get("RKP_DEV_TOKEN") or dev_token(data_dir)
    if len(token) < 16:
        print("RKP_DEV_TOKEN must be at least 16 characters; the API refuses every request without it.")
        return 2
    load_provider_file(data_dir)
    conninfo = start_postgres(data_dir, int(os.environ.get("RKP_PG_PORT", "54329")))
    store = ObjectStore(data_dir / "objects", TENANT)
    allowed = frozenset(p for p in os.environ.get("RKP_ALLOW_PROVIDERS", "").split(",") if p)
    gateway = Gateway.from_env(allowed)
    service = Service(store, data_dir / "objects", conninfo, gateway=gateway, allow_local_paths=os.environ.get("RKP_ALLOW_LOCAL") == "1")
    app = create_app(conninfo, TENANT, token, store, service)
    threading.Thread(target=service.poll_repositories, daemon=True, name="repository-polling").start()
    port = int(os.environ.get("RKP_API_PORT", "8787"))
    print(f"data: {data_dir}\ndecision models: {gateway.describe() if gateway else 'none (ambiguous decisions abstain)'}\nAPI: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
