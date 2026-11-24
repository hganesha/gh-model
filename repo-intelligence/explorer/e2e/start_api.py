"""Run the actual local API and frontend fixture repository in isolation."""

import atexit
import os
import signal
import socket
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from repo_intel.fixtures import materialize
from repo_intel.serve import main

data = TemporaryDirectory(prefix="rkp-browser-test-")
atexit.register(data.cleanup)
root = Path(data.name)
fixture = root / "Orders fixture"
orders_commits = materialize("F01-python-api", fixture)
# A second ref, so the browser suite can compile and then retract a snapshot that nothing else uses.
subprocess.run(["git", "-C", str(fixture), "tag", "v0", orders_commits[0]], check=True)
monorepo = root / "Storefront fixture"
materialize("F02-ts-monorepo", monorepo)
config = root / "data"
if "RKP_PG_PORT" not in os.environ:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        os.environ["RKP_PG_PORT"] = str(listener.getsockname()[1])
os.environ.update(RKP_DATA_DIR=str(config), RKP_ALLOW_LOCAL="1", RKP_ALLOW_PROVIDERS="")

def finish(signum, frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
# Playwright can exchange this temporary fixture path without exposing the API token.
path_file = Path(__file__).parent.parent / "test-results" / "fixture-path"
path_file.parent.mkdir(exist_ok=True)
path_file.write_text(str(fixture), encoding="utf-8")
(path_file.parent / "fixture-path-monorepo").write_text(str(monorepo), encoding="utf-8")
raise SystemExit(main())
