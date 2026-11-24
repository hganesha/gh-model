"""Sandboxed step worker (spec §3.3 hermeticity; plan GI-004).

    python -m repo_intel.compile.step_worker --inputs DIR     # serve steps (started by SandboxExecutor)
    python -m repo_intel.compile.step_worker --selfcheck      # prove the sandbox blocks file, env and network reads

The host starts this module inside `sandbox-exec` or `bwrap`. The worker can read
the interpreter, its libraries and the repo_intel package, plus DIR, which
holds only the current step's declared inputs, named by digest. It has no
network, cannot write files, and clears its environment before running any step.

Protocol, one JSON object per line on stdin:
    {"step": name, "version": v, "inputs": key-form inputs, "config": {...}}
and one line back on the original stdout:
    "R..." once ready; then per request "O" + canonical JSON output, or "E" + {"type", "message", "traceback"}.
Anything a step prints goes to stderr, never into the protocol stream.

The `selfcheck.*` steps below try to read an undeclared file, environment
variable and network address. They exist so that the sandbox's guarantees can
be tested and checked on a host, and must fail under the sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repo_intel.compile.buildgraph import Ref, Step, StepInputs, from_key_form, registry
from repo_intel.storage.hashing import canonical


def _probe_read_file(inputs: StepInputs, config: Mapping[str, Any]) -> str:
    return Path(config["path"]).read_text()


def _probe_env(inputs: StepInputs, config: Mapping[str, Any]) -> str:
    return os.environ[config["name"]]


def _probe_network(inputs: StepInputs, config: Mapping[str, Any]) -> str:
    with socket.create_connection((config["host"], int(config["port"])), timeout=3):
        return "connected"


def _probe_write_file(inputs: StepInputs, config: Mapping[str, Any]) -> str:
    Path(config["path"]).write_text("written by a step")
    return "written"


def _probe_echo(inputs: StepInputs, config: Mapping[str, Any]) -> Any:
    return {name: inputs[name] if not isinstance(inputs[name], bytes) else inputs[name].decode() for name in inputs.names()}


def _probe_undeclared_input(inputs: StepInputs, config: Mapping[str, Any]) -> Any:
    return inputs[config["name"]]


def _probe_sleep(inputs: StepInputs, config: Mapping[str, Any]) -> str:
    import time

    time.sleep(float(config["seconds"]))
    return "slept"


PROBE_READ_FILE = Step("selfcheck.read_file", "1", _probe_read_file)
PROBE_ENV = Step("selfcheck.env", "1", _probe_env)
PROBE_NETWORK = Step("selfcheck.network", "1", _probe_network)
PROBE_WRITE_FILE = Step("selfcheck.write_file", "1", _probe_write_file)
PROBE_ECHO = Step("selfcheck.echo", "1", _probe_echo)
PROBE_UNDECLARED_INPUT = Step("selfcheck.undeclared_input", "1", _probe_undeclared_input)
PROBE_SLEEP = Step("selfcheck.sleep", "1", _probe_sleep)


def serve(inputs_dir: str) -> int:
    proto = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # steps that print write to stderr
    sys.stdout = sys.stderr
    steps = registry()
    os.environ.clear()  # a step sees no environment
    base = Path(inputs_dir)

    def resolve(ref: Ref) -> Any:
        data = (base / ref.digest.split(":", 1)[1]).read_bytes()
        return data if ref.kind == "bytes" else json.loads(data)

    proto.write(b"R" + f"{sys.version_info.major}.{sys.version_info.minor}".encode() + b"\n")
    proto.flush()
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            step = steps.get(request["step"])
            if step is None:
                raise LookupError(f"unknown step {request['step']}")
            if step.version != request["version"]:
                raise RuntimeError(f"toolchain skew: host runs {request['step']}@{request['version']}, worker has {step.version}")
            output = canonical(step.fn(StepInputs(resolve, from_key_form(request["inputs"])), request["config"]))
            proto.write(b"O" + output + b"\n")
        except BaseException as exc:  # report every failure; the host decides
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            message = exc.args[0] if type(exc).__name__ == "UndeclaredInputError" and exc.args else str(exc)
            body = {"type": type(exc).__name__, "message": str(message), "traceback": traceback.format_exc(limit=20)}
            proto.write(b"E" + json.dumps(body).encode() + b"\n")
        proto.flush()
    return 0


def selfcheck() -> int:
    """Run the probes under the default sandbox; exit 1 if any of them succeeds."""
    import tempfile

    from repo_intel.compile.buildgraph import SandboxExecutor, StepFailed, StepRunner
    from repo_intel.storage.store import ObjectStore

    # Run as `-m`, this file is `__main__`; the registry holds the probes of the imported module.
    probe = sys.modules.get(f"{__package__}.step_worker") or __import__(f"{__package__}.step_worker", fromlist=["_"])

    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "undeclared.txt"
        secret.write_text("not a declared input")
        os.environ["RKP_SELFCHECK_PROBE"] = "visible"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        runner = StepRunner(ObjectStore(Path(tmp) / "store", "selfcheck"), use_cache=False, executor=SandboxExecutor(timeout=30))
        probes = [
            (probe.PROBE_READ_FILE, {"path": str(secret)}),
            (probe.PROBE_WRITE_FILE, {"path": str(Path(tmp) / "written.txt")}),
            (probe.PROBE_ENV, {"name": "RKP_SELFCHECK_PROBE"}),
            (probe.PROBE_NETWORK, {"host": "127.0.0.1", "port": listener.getsockname()[1]}),
        ]
        failures = 0
        for step, config in probes:
            try:
                runner.run(step, {}, config)
                print(f"FAIL {step.name}: the sandbox allowed it")
                failures += 1
            except StepFailed as exc:
                print(f"ok   {step.name}: blocked ({exc.error_type})")
        listener.close()
    print(f"sandbox: {runner.executor.name}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repo_intel.compile.step_worker")
    parser.add_argument("--inputs")
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if not args.inputs:
        parser.error("--inputs is required")
    return serve(args.inputs)


if __name__ == "__main__":
    raise SystemExit(main())
