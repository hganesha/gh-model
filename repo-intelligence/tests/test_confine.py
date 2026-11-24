"""Landlock/seccomp self-confinement: chosen on Linux without bwrap, and it really confines."""

import os
import subprocess
import sys
import textwrap

import pytest

from repo_intel.compile import buildgraph, confine


def test_linux_without_bwrap_uses_landlock(monkeypatch):
    monkeypatch.setattr(buildgraph.sys, "platform", "linux")
    monkeypatch.setattr(buildgraph.shutil, "which", lambda name: None)
    monkeypatch.setattr(confine, "landlock_abi", lambda: 3)
    assert buildgraph._sandbox_tool() == "landlock"
    monkeypatch.setattr(buildgraph.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert buildgraph._sandbox_tool() == "bwrap", "bwrap stays the first choice where it is installed"
    monkeypatch.setattr(buildgraph.shutil, "which", lambda name: None)
    monkeypatch.setattr(confine, "landlock_abi", lambda: 0)
    with pytest.raises(buildgraph.SandboxUnavailable):
        buildgraph.SandboxExecutor(tool=buildgraph._sandbox_tool())


def test_landlock_read_roots_cover_code_and_inputs(tmp_path):
    roots = buildgraph.landlock_read_roots(str(tmp_path))
    assert str(tmp_path) in roots and "/usr" in roots
    assert any(os.path.realpath(buildgraph.__file__).startswith(r.rstrip("/") + "/") for r in roots)


@pytest.mark.skipif(confine.landlock_abi() < 1, reason="needs a Linux kernel with Landlock")
def test_confined_process_cannot_read_write_or_connect(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "ok.txt").write_text("fine")
    script = textwrap.dedent(f"""
        import socket, sys
        from repo_intel.compile.confine import confine
        confine([sys.prefix, sys.base_prefix, "/usr", "/lib", {str(allowed)!r}, {os.path.dirname(os.path.dirname(confine.__file__))!r}])
        assert open({str(allowed / "ok.txt")!r}).read() == "fine"
        for attempt in (lambda: open({str(secret)!r}).read(), lambda: open({str(tmp_path / "w.txt")!r}, "w"),
                        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM), lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)):
            try:
                attempt()
            except PermissionError:
                continue
            sys.exit("not blocked")
        socket.socketpair()  # AF_UNIX stays available
        print("confined")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "confined", out.stderr
