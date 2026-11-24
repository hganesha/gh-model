"""Unprivileged self-confinement for the step worker on Linux: Landlock and seccomp.

`bwrap` needs user namespaces, which containers (including Azure Container Apps)
usually refuse. Landlock and seccomp need no privilege, so the worker can confine
itself after starting and before it reads any step input:

- Landlock: the process may read and execute only beneath the given roots, may
  write nothing except `/dev/null`, and (ABI 4 and later) may not bind or connect
  TCP sockets; from ABI 6, abstract Unix sockets and signals outside the sandbox
  are out of reach too.
- seccomp: `socket()` for any family but AF_UNIX fails with EPERM, so there is no
  network (UDP and raw sockets included), and io_uring is refused, since it could
  open sockets without the `socket()` call.

Both are irreversible and inherited by children. They apply to the calling thread
(and seccomp, with TSYNC, to every thread), so `confine` refuses to run while
other threads exist.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import threading
from collections.abc import Iterable

_SYS = {  # (landlock_create_ruleset, landlock_add_rule, landlock_restrict_self, seccomp, socket, io_uring_setup, audit arch)
    "x86_64": (444, 445, 446, 317, 41, 425, 0xC000003E),
    "aarch64": (444, 445, 446, 277, 198, 425, 0xC00000B7),
}

# Landlock filesystem rights, by the ABI version that introduced them.
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_FS_ABI1 = (1 << 13) - 1
_REFER, _TRUNCATE, _IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
_FILE_RIGHTS = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
_NET_BIND_TCP, _NET_CONNECT_TCP = 1 << 0, 1 << 1
_SCOPE_ABSTRACT_UNIX, _SCOPE_SIGNAL = 1 << 0, 1 << 1
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_O_PATH = 0o10000000


class ConfinementError(OSError):
    pass


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _numbers() -> tuple[int, ...] | None:
    return _SYS.get(platform.machine()) if os.uname().sysname == "Linux" else None


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is unavailable."""
    nums = _numbers()
    if nums is None:
        return 0
    abi = _libc().syscall(nums[0], None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION))
    return max(abi, 0)


def _check(result: int, what: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise ConfinementError(code, f"{what}: {os.strerror(code)}")
    return result


def _landlock(read_roots: Iterable[str], writable_files: Iterable[str]) -> int:
    nums = _numbers()
    abi = landlock_abi()
    if nums is None or abi < 1:
        raise ConfinementError(errno.ENOSYS, "Landlock is not available in this kernel")
    libc = _libc()
    fs = _FS_ABI1 | (_REFER if abi >= 2 else 0) | (_TRUNCATE if abi >= 3 else 0) | (_IOCTL_DEV if abi >= 5 else 0)
    attr = _RulesetAttr(fs, _NET_BIND_TCP | _NET_CONNECT_TCP if abi >= 4 else 0, _SCOPE_ABSTRACT_UNIX | _SCOPE_SIGNAL if abi >= 6 else 0)
    size = 24 if abi >= 6 else 16 if abi >= 4 else 8
    ruleset = _check(libc.syscall(nums[0], ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)), "landlock_create_ruleset")
    try:
        def allow(path: str, access: int) -> None:
            try:
                fd = os.open(path, _O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                return
            try:
                if not os.path.isdir(path):
                    access &= _FILE_RIGHTS
                rule = _PathBeneath(access & fs, fd)
                _check(libc.syscall(nums[1], ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(rule), ctypes.c_uint32(0)), f"landlock_add_rule {path}")
            finally:
                os.close(fd)

        for root in read_roots:
            allow(root, _EXECUTE | _READ_FILE | _READ_DIR)
        for path in writable_files:
            allow(path, _READ_FILE | _WRITE_FILE | _TRUNCATE)
        _check(libc.syscall(nums[2], ctypes.c_int(ruleset), ctypes.c_uint32(0)), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return abi


def _seccomp() -> None:
    nums = _numbers()
    assert nums is not None
    _, _, _, seccomp_nr, socket_nr, io_uring_nr, arch = nums
    LD, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
    ALLOW, KILL, DENY = 0x7FFF0000, 0x80000000, 0x00050000 | errno.EPERM
    # Offsets: seccomp_data.nr at 0, .arch at 4, .args[0] (low half) at 16.
    program = [
        (LD, 0, 0, 4),
        (JEQ, 1, 0, arch),            # 1: right architecture, skip the kill
        (RET, 0, 0, KILL),            # 2
        (LD, 0, 0, 0),                # 3
        (JGE, 5, 0, 0x40000000),      # 4: x32 system calls -> deny (10)
        (JEQ, 4, 0, io_uring_nr),     # 5: io_uring -> deny (10)
        (JEQ, 0, 4, socket_nr),       # 6: not socket() -> allow (11)
        (LD, 0, 0, 16),               # 7: the address family
        (JEQ, 2, 0, 1),               # 8: AF_UNIX -> allow (11)
        (RET, 0, 0, DENY),            # 9
        (RET, 0, 0, DENY),            # 10
        (RET, 0, 0, ALLOW),           # 11
    ]
    filters = (_SockFilter * len(program))(*(_SockFilter(*ins) for ins in program))
    prog = _SockFprog(len(program), filters)
    SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC = 1, 1
    _check(_libc().syscall(seccomp_nr, ctypes.c_uint(SECCOMP_SET_MODE_FILTER), ctypes.c_uint(SECCOMP_FILTER_FLAG_TSYNC), ctypes.byref(prog)), "seccomp")


def confine(read_roots: Iterable[str], writable_files: Iterable[str] = ("/dev/null",)) -> int:
    """Confine this process for good. Returns the Landlock ABI used; raises ConfinementError."""
    if threading.active_count() != 1:
        raise ConfinementError(errno.EBUSY, "confine must run before any other thread starts")
    PR_SET_NO_NEW_PRIVS = 38
    _check(_libc().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(PR_SET_NO_NEW_PRIVS)")
    abi = _landlock(list(read_roots), list(writable_files))
    _seccomp()
    return abi
