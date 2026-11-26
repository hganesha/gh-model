"""Memoized build-graph runtime (spec §3.3, §14.2; plan GI-004).

A step is a pure function of its declared inputs. Its cache key is the digest
of (step name, code version, config, input digests). A compile evaluates the
graph; steps whose key is cached are not re-run. Incremental and cold compiles
therefore run the same graph and differ only in cache hits.

Step versions are derived from code: `Step.version` is the declared version
plus a digest of the step function's source and of every repo_intel function,
class and constant it reaches. Changing step code invalidates its cache
without a manual bump.

Hermeticity is enforced by the executor. The default `SandboxExecutor` writes
only the step's declared inputs, by digest, into a directory that the worker
can read but not write, and runs `python -m repo_intel.compile.step_worker` under
`sandbox-exec` (macOS) or `bwrap` (Linux) with no network, no store path, an
empty environment and a time limit. A read of anything else fails the step.
`InProcessExecutor` is for development and tests only: select it explicitly,
or set `RKP_STEP_EXECUTOR=inprocess`.

Every step evaluation is counted per step (hits, executed, seconds), and the
counts are written to the store index `step_metrics` under the runner's run id.
"""

from __future__ import annotations

import ast
import atexit
import dataclasses
import functools
import hashlib
import importlib
import inspect
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import weakref
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from repo_intel.compile import confine
from repo_intel.storage.hashing import canonical, digest
from repo_intel.storage.store import ObjectStore

PACKAGE = __name__.split(".")[0]  # the top-level package: steps live anywhere under it
EXECUTOR_ENV = "RKP_STEP_EXECUTOR"
METRICS_INDEX = "step_metrics"


class UndeclaredInputError(KeyError):
    """A step asked for an input it did not declare."""


class StepFailed(RuntimeError):
    """A step raised inside the sandbox worker."""

    def __init__(self, step: str, error_type: str, message: str, trace: str = ""):
        super().__init__(f"step {step} failed: {error_type}: {message}")
        self.step, self.error_type, self.error_message, self.trace = step, error_type, message, trace


class StepTimeout(StepFailed):
    """A step exceeded the executor's time limit and its worker was killed."""


class SandboxUnavailable(RuntimeError):
    """The OS sandbox cannot run here. Install bubblewrap (Linux) or use the dev flag."""


class UnregisteredStepError(LookupError):
    """Only registered steps with importable top-level functions run in the sandbox."""


@dataclass(frozen=True)
class Ref:
    """A declared input held in the object store, identified by digest."""

    digest: str
    kind: str = "json"  # "json" or "bytes"


class StepInputs:
    """What a step sees: its declared inputs, resolved on demand, and nothing else."""

    def __init__(self, resolve: Callable[[Ref], Any], declared: Mapping[str, Any]):
        self.__resolve = resolve
        self.__declared = dict(declared)

    def __getitem__(self, name: str) -> Any:
        if name not in self.__declared:
            raise UndeclaredInputError(name)
        return self._resolve(self.__declared[name])

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, Ref):
            return self.__resolve(value)
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve(v) for k, v in value.items()}
        return value

    def names(self) -> list[str]:
        return sorted(self.__declared)


StepFn = Callable[[StepInputs, Mapping[str, Any]], Any]


# ------------------------------------------------------------------ code versions


def _ours(obj: Any) -> bool:
    module = getattr(obj, "__module__", None) or ""
    return module == PACKAGE or module.startswith(PACKAGE + ".")


def _qualname(obj: Any) -> str:
    return f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', type(obj).__qualname__)}"


def _code_objects(code: types.CodeType) -> Iterator[types.CodeType]:
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _callables_of(cls: type) -> Iterator[types.FunctionType]:
    for value in vars(cls).values():
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, property):
            yield from (f for f in (value.fget, value.fset, value.fdel) if isinstance(f, types.FunctionType))
        elif isinstance(value, types.FunctionType):
            yield value


def code_digest(root: Any, skip: frozenset[str] = frozenset()) -> str:
    blob = json.dumps(code_parts(root, skip), sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


@functools.cache
def _module_index(module_name: str) -> tuple[dict[str, list[tuple[str, frozenset[str]]]], dict[str, tuple[str, str]]]:
    """Top-level assignments (source text and the names they use) and `from` imports of a module."""
    module = sys.modules.get(module_name)
    try:
        source = inspect.getsource(module) if module else ""
    except (OSError, TypeError):
        source = ""
    assigns: dict[str, list[tuple[str, frozenset[str]]]] = {}
    imports: dict[str, tuple[str, str]] = {}
    package = module_name if getattr(module, "__path__", None) else module_name.rpartition(".")[0]
    for node in ast.parse(source).body if source else []:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            used = frozenset(n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)) if node.value else frozenset()
            text = ast.get_source_segment(source, node) or ""
            for target in targets:
                for name in (n.id for n in ast.walk(target) if isinstance(n, ast.Name)):
                    assigns.setdefault(name, []).append((text, used))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            for alias in node.names:
                imports[alias.asname or alias.name] = (base, alias.name)
    return assigns, imports


def code_parts(root: Any, skip: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Source text reachable from `root`, keyed by qualified name (see `code_digest`).

    Only source is recorded, never runtime values: a module-level constant
    contributes the text of its top-level assignment, and the names that
    assignment uses are followed. A cache that fills up at runtime therefore
    cannot change a version.
    """
    parts: dict[str, Any] = {}
    pending: list[Any] = [root]

    def code_in(value: Any, depth: int = 0) -> None:
        """Queue the repo_intel code held by a value (an instance's class, functions in containers)."""
        if depth > 4:
            return
        if isinstance(value, (types.FunctionType, type)):
            if _ours(value):
                pending.append(value)
        elif isinstance(value, Mapping):
            for v in value.values():
                code_in(v, depth + 1)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for v in value:
                code_in(v, depth + 1)
        elif _ours(type(value)):
            pending.append(type(value))
            if dataclasses.is_dataclass(value):
                for f in dataclasses.fields(value):
                    code_in(getattr(value, f.name), depth + 1)

    def constant(module_name: str, name: str, depth: int = 0) -> None:
        """Record the assignment of `module_name.name`, following imports and the names it uses."""
        label = f"{module_name}.{name}"
        if label in parts or depth > 8 or name in skip:
            return
        if not (module_name == PACKAGE or module_name.startswith(PACKAGE + ".")):
            return
        assigns, imports = _module_index(module_name)
        scope = vars(sys.modules[module_name]) if module_name in sys.modules else {}
        if name in assigns:
            parts[label] = [text for text, _ in assigns[name]]
            for _, used in assigns[name]:
                for other in used:
                    if other != name:
                        note(scope, other, depth + 1)
        elif name in imports:
            source_module, original = imports[name]
            constant(source_module, original, depth + 1)
        if name in scope:
            code_in(scope[name])

    def note(scope: Mapping[str, Any], name: str, depth: int = 0) -> None:
        if name in skip or name not in scope:
            return
        value = scope[name]
        if isinstance(value, (types.FunctionType, type)):
            if _ours(value):
                pending.append(value)
        elif isinstance(value, types.ModuleType):
            pass
        elif not isinstance(value, types.BuiltinFunctionType):
            constant(scope.get("__name__", ""), name, depth)

    def follow(code: types.CodeType, scope: Mapping[str, Any]) -> None:
        for name in code.co_names:
            if name in skip or name not in scope:
                continue
            value = scope[name]
            if isinstance(value, types.ModuleType):
                if value.__name__ == PACKAGE or value.__name__.startswith(PACKAGE + "."):
                    for attr in code.co_names:  # `module.attr` puts both names in co_names
                        if attr not in skip and attr in vars(value):
                            note(vars(value), attr)
                continue
            note(scope, name)

    while pending:
        obj = pending.pop()
        if isinstance(obj, types.MethodType):
            obj = obj.__func__
        label = _qualname(obj)
        if label in parts:
            continue
        try:
            parts[label] = inspect.getsource(obj)
        except (OSError, TypeError):
            parts[label] = label
        if isinstance(obj, types.FunctionType):
            for code in _code_objects(obj.__code__):
                follow(code, obj.__globals__)
            for cell in obj.__closure__ or ():
                try:
                    code_in(cell.cell_contents)
                except ValueError:
                    pass
        elif isinstance(obj, type):
            for fn in _callables_of(obj):
                for code in _code_objects(fn.__code__):
                    follow(code, fn.__globals__)
            for base in obj.__mro__[1:]:
                if _ours(base):
                    pending.append(base)
    return parts


_VERSION_CACHE: dict[Any, str] = {}


def _cached_code_digest(root: Any, skip: frozenset[str]) -> str:
    key = (root, skip)
    if key not in _VERSION_CACHE:
        _VERSION_CACHE[key] = code_digest(root, skip)
    return _VERSION_CACHE[key]


@dataclass(frozen=True)
class Step:
    """A build-graph step. `declared_version` is the human version; `version` adds a code digest.

    `toolchain_extra` folds versions that travel in the step config (for example
    per-extractor versions) into the reported version, so the toolchain hash moves
    with them, without putting them in every cache key.
    """

    name: str
    declared_version: str
    fn: StepFn
    toolchain_extra: Callable[[], Any] | None = field(default=None, compare=False)
    version_skip: frozenset[str] = frozenset()

    @property
    def code_version(self) -> str:
        """The version in the cache key: declared version plus the code digest."""
        return f"{self.declared_version}+{_cached_code_digest(self.fn, self.version_skip)[:12]}"

    @property
    def version(self) -> str:
        """The version reported in the toolchain manifest."""
        if self.toolchain_extra is None:
            return self.code_version
        return f"{self.declared_version}+{digest([self.code_version, self.toolchain_extra()]).split(':')[1][:12]}"


# ------------------------------------------------------------------ registry

_STEP_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*(?::[^=\n]+)?=\s*Step\(", re.MULTILINE)


@functools.cache
def step_modules() -> tuple[str, ...]:
    """Every repo_intel module that defines a module-level `NAME = Step(...)`.

    Found from source text, so a new step module is registered without editing a
    list, and modules without steps (the API, the service) are never imported.
    """
    root = Path(importlib.import_module(PACKAGE).__file__ or "").parent
    found = []
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _STEP_ASSIGNMENT.search(text):
            parts = path.relative_to(root).with_suffix("").parts
            parts = parts[:-1] if parts[-1] == "__init__" else parts
            found.append(".".join((PACKAGE, *parts)))
    return tuple(found)

_REGISTRY: dict[str, Step] | None = None
_REGISTRY_LOCK = threading.Lock()


def registry() -> dict[str, Step]:
    """Every registered step, by name: the module-level `Step` objects of `step_modules()`.

    A registered step's function must be an importable top-level function, so a
    worker process resolves the same code from the step name alone.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            found: dict[str, Step] = {}
            for module_name in step_modules():
                module = importlib.import_module(module_name)
                for value in vars(module).values():
                    if not isinstance(value, Step):
                        continue
                    fn = value.fn
                    home = sys.modules.get(getattr(fn, "__module__", ""))
                    if fn.__qualname__ != fn.__name__ or getattr(home, fn.__name__, None) is not fn:
                        raise UnregisteredStepError(f"step {value.name}: {fn.__qualname__} is not an importable top-level function")
                    if value.name in found and found[value.name] is not value:
                        raise UnregisteredStepError(f"step name {value.name} is registered twice")
                    found[value.name] = value
            _REGISTRY = dict(sorted(found.items()))
        return _REGISTRY


def registered(step: Step) -> bool:
    return registry().get(step.name) is step


# ------------------------------------------------------------------ executors


def _key_form(value: Any) -> Any:
    """Inputs as they enter the cache key and the worker request: refs collapse to their digest."""
    if isinstance(value, Ref):
        return {"$ref": value.digest, "kind": value.kind}
    if isinstance(value, (list, tuple)):
        return [_key_form(v) for v in value]
    if isinstance(value, dict):
        return {k: _key_form(v) for k, v in value.items()}
    return value


def from_key_form(value: Any) -> Any:
    """Inverse of `_key_form`, used by the worker."""
    if isinstance(value, dict):
        if set(value) == {"$ref", "kind"}:
            return Ref(value["$ref"], value["kind"])
        return {k: from_key_form(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_key_form(v) for v in value]
    return value


def _refs(value: Any) -> Iterator[Ref]:
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _refs(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _refs(v)


class Executor(Protocol):
    name: str

    def execute(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any], store: ObjectStore) -> bytes:
        """Run one step; return its output as canonical JSON bytes."""
        ...


class InProcessExecutor:
    """Runs steps in this process. API-level isolation only: for development and tests."""

    name = "inprocess"

    def execute(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any], store: ObjectStore) -> bytes:
        def resolve(ref: Ref) -> Any:
            return store.get_bytes(ref.digest) if ref.kind == "bytes" else store.get_json(ref.digest)

        return canonical(step.fn(StepInputs(resolve, inputs), config))


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(path)


def _code_roots() -> list[str]:
    """What the worker may read: the interpreter, its libraries and the repo_intel package."""
    import site

    candidates = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(os.path.dirname(_real(sys.executable)))}
    # A frozen engine (PyInstaller) has no site-packages: its libraries sit beside the executable.
    candidates |= set(getattr(site, "getsitepackages", list)())
    candidates.add(getattr(sys, "_MEIPASS", ""))
    candidates.add(str(Path(importlib.import_module(PACKAGE).__file__ or "").parent))
    roots = sorted({_real(c) for c in candidates if c and os.path.isdir(c)})
    # Drop roots nested inside another root.
    return [r for r in roots if not any(r != o and r.startswith(o.rstrip("/") + "/") for o in roots)]


def _sbpl(value: str) -> str:
    return json.dumps(value)  # SBPL string literals share JSON's escaping


def _seatbelt_profile(inputs_dir: str) -> str:
    readable = ["/usr/lib", "/usr/share", "/System", "/private/var/db/dyld", *_code_roots(), inputs_dir]
    allow_read = " ".join(f"(subpath {_sbpl(p)})" for p in readable)
    devices = " ".join(f"(literal {_sbpl(p)})" for p in ("/", "/dev/null", "/dev/urandom", "/dev/random"))
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        '(deny file-read-data (subpath "/"))',
        f"(allow file-read-data {devices} {allow_read})",
    ])


_SYSTEM_READ = ("/bin", "/lib", "/lib64", "/lib32", "/sbin", "/etc/ld.so.cache", "/etc/alternatives")


def landlock_read_roots(inputs_dir: str) -> list[str]:
    """What a self-confined worker may read: the same as under bwrap."""
    return ["/usr", *_SYSTEM_READ, *_code_roots(), inputs_dir]


def _bwrap_command(bwrap: str, inputs_dir: str) -> list[str]:
    cmd = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr"]
    for path in _SYSTEM_READ:
        cmd += ["--ro-bind-try", path, path]
    for root in _code_roots():
        cmd += ["--ro-bind", root, root]
    cmd += ["--ro-bind", inputs_dir, inputs_dir, "--chdir", inputs_dir]
    return cmd


FROZEN_WORKER_ARG = "__step_worker__"


def worker_command() -> list[str]:
    """How to start a step worker with this interpreter, or with this frozen engine."""
    if getattr(sys, "frozen", False):
        # The frozen `rkp` executable dispatches this first argument to step_worker.main.
        return [_real(sys.executable), FROZEN_WORKER_ARG]
    # Load the engine package from the directory this host imported it from, not from
    # whatever the interpreter's site-packages points at: a venv shared between checkouts
    # (an editable install of another worktree) would otherwise run other code, outside
    # the sandbox's read roots. Only the package directory is read, which _code_roots allows.
    package_dir = str(Path(importlib.import_module(PACKAGE).__file__ or "").parent)
    bootstrap = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location({PACKAGE!r}, {os.path.join(package_dir, '__init__.py')!r}, submodule_search_locations=[{package_dir!r}])\n"
        "module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)\n"
        f"from {__name__.rsplit('.', 1)[0]}.step_worker import main\n"
        "raise SystemExit(main())\n"
    )
    return [os.path.normpath(os.path.abspath(sys.executable)), "-I", "-B", "-c", bootstrap]


class _Worker:
    """One sandboxed `repo_intel.compile.step_worker` process, reused across steps.

    Steps run one at a time. Before each step the host writes that step's
    declared inputs into the worker's input directory, and removes them
    afterwards, so a step never sees another step's inputs.
    """

    STARTUP_SECONDS = 60.0

    def __init__(self, tool: str, tool_path: str):
        self.root = Path(tempfile.mkdtemp(prefix="rkp-step-"))
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        inputs = _real(self.inputs)
        # Normalised: started as `../.venv/bin/python`, the interpreter path runs through a
        # directory bwrap does not mount. Not resolved: a venv's python is a symlink and
        # must keep its venv path to find its site-packages.
        python = [*worker_command(), "--inputs", inputs]
        if tool == "sandbox-exec":
            cmd = [tool_path, "-p", _seatbelt_profile(inputs), *python]
        elif tool == "landlock":
            cmd = [*python, "--confine"]
        else:
            cmd = [*_bwrap_command(tool_path, inputs), *python]
        self.log_path = self.root / "worker.log"
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log, env={"PATH": "/usr/bin:/bin"}, cwd=inputs, close_fds=True)
        self._buffer = b""
        ready = self._read_line(time.monotonic() + self.STARTUP_SECONDS)
        if ready is None or not ready.startswith(b"R"):
            log = self.log_tail()
            self.close()
            raise SandboxUnavailable(f"{tool} worker did not start: {log}")

    def log_tail(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-2000:].strip() or "(no output)"
        except OSError:
            return "(no log)"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _read_line(self, deadline: float) -> bytes | None:
        """One protocol line, or None on EOF. Raises TimeoutError at the deadline."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if not sel.select(remaining):
                    continue
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    return None
                self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    def call(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any], store: ObjectStore, timeout: float) -> bytes:
        written: list[Path] = []
        try:
            for ref in _refs(dict(inputs)):
                target = self.inputs / ref.digest.split(":", 1)[1]
                if not target.exists():
                    target.write_bytes(store.get_bytes(ref.digest))
                    target.chmod(0o444)
                    written.append(target)
            request = {"step": step.name, "version": step.version, "inputs": _key_form(dict(inputs)), "config": dict(config)}
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            self.proc.stdin.flush()
            try:
                line = self._read_line(time.monotonic() + timeout)
            except TimeoutError:
                self.close()
                raise StepTimeout(step.name, "StepTimeout", f"exceeded {timeout:g}s") from None
            if line is None:
                log = self.log_tail()
                self.close()
                raise StepFailed(step.name, "WorkerExited", f"worker exited: {log}")
            if line.startswith(b"O"):
                return line[1:]
            error = json.loads(line[1:])
            if error["type"] == "UndeclaredInputError":
                raise UndeclaredInputError(error["message"])
            raise StepFailed(step.name, error["type"], error["message"], error.get("traceback", ""))
        finally:
            for path in written:
                path.unlink(missing_ok=True)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        self._log.close()
        shutil.rmtree(self.root, ignore_errors=True)


class SandboxExecutor:
    """Runs each step in an OS sandbox with only its declared inputs readable.

    Workers are pooled per process and reused; a worker that fails or times out
    is discarded.
    """

    _pool: dict[str, list[_Worker]] = {}
    _lock = threading.Lock()

    def __init__(self, timeout: float = 600.0, tool: str | None = None):
        self.timeout = timeout
        self.tool = tool or _sandbox_tool()
        if self.tool == "sandbox-exec":
            found = "/usr/bin/sandbox-exec" if os.path.exists("/usr/bin/sandbox-exec") else None
        elif self.tool == "landlock":
            found = sys.executable if confine.landlock_abi() >= 1 else None
        else:
            found = shutil.which(self.tool) if self.tool == "bwrap" else None
        if not found:
            raise SandboxUnavailable(f"no step sandbox on {sys.platform}: install bubblewrap (bwrap) or use a kernel with Landlock, or set {EXECUTOR_ENV}=inprocess for development")
        self.tool_path = found
        self.name = self.tool

    def _acquire(self) -> _Worker:
        with self._lock:
            idle = self._pool.setdefault(self.tool, [])
            while idle:
                worker = idle.pop()
                if worker.alive():
                    return worker
                worker.close()
        return _Worker(self.tool, self.tool_path)

    def _release(self, worker: _Worker) -> None:
        with self._lock:
            if worker.alive():
                self._pool.setdefault(self.tool, []).append(worker)

    def execute(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any], store: ObjectStore) -> bytes:
        if not registered(step):
            raise UnregisteredStepError(f"step {step.name} is not in the step registry; the sandbox runs only registered steps")
        worker = self._acquire()
        try:
            return worker.call(step, inputs, config, store, self.timeout)
        finally:
            self._release(worker)

    @classmethod
    def shutdown(cls) -> None:
        with cls._lock:
            for workers in cls._pool.values():
                for worker in workers:
                    worker.close()
            cls._pool.clear()


atexit.register(SandboxExecutor.shutdown)


def default_executor_name() -> str:
    choice = os.environ.get(EXECUTOR_ENV, "sandbox")
    if choice == "inprocess":
        return InProcessExecutor.name
    return _sandbox_tool()


def _sandbox_tool() -> str:
    """sandbox-exec on macOS; on Linux bwrap where it is installed, else Landlock self-confinement."""
    if sys.platform == "darwin":
        return "sandbox-exec"
    if shutil.which("bwrap") is None and confine.landlock_abi() >= 1:
        return "landlock"
    return "bwrap"


def default_executor() -> Executor:
    """The sandbox, unless the dev/test flag `RKP_STEP_EXECUTOR=inprocess` is set."""
    choice = os.environ.get(EXECUTOR_ENV, "sandbox")
    if choice == "inprocess":
        return InProcessExecutor()
    if choice != "sandbox":
        raise ValueError(f"{EXECUTOR_ENV} must be 'sandbox' or 'inprocess', not {choice!r}")
    return SandboxExecutor()


# ------------------------------------------------------------------ runner and metrics


@dataclass
class RunStats:
    executed: Counter = field(default_factory=Counter)
    hits: Counter = field(default_factory=Counter)
    seconds: Counter = field(default_factory=Counter)

    @property
    def total_executed(self) -> int:
        return sum(self.executed.values())

    def row(self) -> dict[str, Any]:
        names = sorted(set(self.executed) | set(self.hits))
        return {n: {"hits": self.hits[n], "executed": self.executed[n], "seconds": round(self.seconds[n], 6)} for n in names}


def _write_metrics(store: ObjectStore, run_id: str, executor: str, started: float, stats: RunStats) -> None:
    row = {
        "run_id": run_id, "tenant_id": store.tenant_id, "executor": executor,
        "started_at": started, "updated_at": time.time(), "steps": stats.row(),
        "totals": {"hits": sum(stats.hits.values()), "executed": stats.total_executed, "seconds": round(sum(stats.seconds.values()), 6)},
    }
    try:
        store.index_put(METRICS_INDEX, run_id, json.dumps(row, sort_keys=True))
        store.index_put(METRICS_INDEX, "latest", run_id)
    except OSError:
        pass  # metrics must never fail a compile


def read_metrics(store: ObjectStore, run_id: str | None = None) -> dict[str, Any] | None:
    """Per-step cache metrics for one run; the latest run when `run_id` is None."""
    run_id = run_id or store.index_get(METRICS_INDEX, "latest")
    if not run_id or run_id == "latest":
        return None
    try:
        text = store.index_get(METRICS_INDEX, run_id)
    except ValueError:
        return None
    return json.loads(text) if text else None


class StepRunner:
    METRICS_INTERVAL = 0.5  # seconds between metric writes while running

    def __init__(self, store: ObjectStore, use_cache: bool = True, executor: Executor | None = None, run_id: str | None = None):
        self.store = store
        self.use_cache = use_cache
        self.executor = executor or default_executor()
        self.run_id = run_id or f"run_{uuid.uuid4().hex}"
        self.stats = RunStats()
        self._started = time.time()
        self._last_write = 0.0
        self._finalizer = weakref.finalize(self, _write_metrics, store, self.run_id, self.executor.name, self._started, self.stats)

    def cache_key(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any]) -> str:
        return digest({"step": step.name, "version": step.code_version, "config": dict(config), "inputs": _key_form(dict(inputs))})

    def run(self, step: Step, inputs: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> Ref:
        """Evaluate a step and return a ref to its JSON output."""
        config = config or {}
        key = self.cache_key(step, inputs, config)
        if self.use_cache:
            cached = self.store.index_get("step_cache", key)
            if cached is not None and self.store.has(cached):
                self.stats.hits[step.name] += 1
                self._maybe_flush()
                return Ref(cached)
        started = time.perf_counter()
        output = self.executor.execute(step, inputs, config, self.store)
        self.stats.seconds[step.name] += time.perf_counter() - started
        out_digest = self.store.put_bytes(output)
        self.store.index_put("step_cache", key, out_digest)
        self.stats.executed[step.name] += 1
        self._maybe_flush()
        return Ref(out_digest)

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._last_write >= self.METRICS_INTERVAL:
            self.flush_metrics()

    def flush_metrics(self) -> None:
        """Write this run's per-step metrics to the store now."""
        self._last_write = time.monotonic()
        _write_metrics(self.store, self.run_id, self.executor.name, self._started, self.stats)

    def load(self, ref: Ref) -> Any:
        return self.store.get_json(ref.digest)


__all__ = [
    "EXECUTOR_ENV", "Executor", "InProcessExecutor", "Ref", "RunStats", "SandboxExecutor", "SandboxUnavailable", "Step",
    "StepFailed", "StepInputs", "StepRunner", "StepTimeout", "UndeclaredInputError", "UnregisteredStepError",
    "code_digest", "default_executor", "default_executor_name", "read_metrics", "registry",
]
