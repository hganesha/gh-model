import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from repo_intel.fixtures import materialize
from repo_intel.store import ObjectStore

REPO_ID = "github.com/1001"


@pytest.fixture(scope="session")
def f01(tmp_path_factory):
    """Fixture F01 as a Git repository: (path, [commit SHAs])."""
    path = tmp_path_factory.mktemp("f01") / "repo"
    return path, materialize("F01-python-api", path)


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "store", "tenant-a")


@pytest.fixture(scope="session")
def pg_conninfo():
    """A throwaway PostgreSQL cluster on a free local port, removed afterwards."""
    if not shutil.which("initdb"):
        pytest.skip("PostgreSQL binaries not installed")
    data = Path(tempfile.mkdtemp(prefix="rkp-pg-"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "LC_ALL": "C"}
    subprocess.run(["initdb", "-D", str(data), "-U", "rkp", "--auth=trust", "-E", "UTF8"], check=True, capture_output=True, env=env)
    opts = f"-c listen_addresses=127.0.0.1 -c port={port} -c unix_socket_directories='' -c fsync=off"
    subprocess.run(["pg_ctl", "-D", str(data), "-o", opts, "-w", "-l", str(data / "log"), "start"], check=True, capture_output=True, env=env)
    try:
        yield f"host=127.0.0.1 port={port} user=rkp dbname=postgres"
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "stop"], capture_output=True, env=env)
        shutil.rmtree(data, ignore_errors=True)
