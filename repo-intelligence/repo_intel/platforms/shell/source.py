"""The shell pack's extractors: POSIX shell scripts, Windows batch files and PowerShell scripts."""

from __future__ import annotations

import posixpath
import re

from repo_intel.platforms.shell import posix
from repo_intel.platforms.shell.commands import Context
from repo_intel.platforms.shell.emit import Out, decode, pack_input
from repo_intel.platforms.shell.lower import lower, resolver
from repo_intel.platforms.shell.vocabulary import PLATFORM

_SHEBANG = re.compile(r"^#!\s*(?:/usr)?(?:/local)?/bin/(?:env\s+)?(sh|bash|ksh|ksh93|zsh|dash)\b")
_PROBLEMS = 20  # syntax problems reported per file; the rest are counted


def program_key(path: str) -> dict[str, str]:
    return {"platform": PLATFORM, "name": path}


class ShellSource:
    """`.sh`, `.ksh`, `.bash`: the script as a Program, its behaviour, and what its commands do."""

    id = "shell.source"
    version = "1.1.0"
    SUFFIXES = (".sh", ".ksh", ".bash")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES) and not pack_input(path)

    def extract(self, path: str, data: bytes, blob: str):
        text = decode(data)
        out = Out(self.id, path, data, blob, text)
        m = _SHEBANG.match(text)
        shell = m.group(1) if m else posixpath.splitext(path)[1].lstrip(".").lower() or "sh"
        me = out.term(0, len(text), "sh:Script", program_key(path), posixpath.basename(path), {"shell": shell.replace("ksh93", "ksh"), "language": "shell"})
        script = posix.parse(text)
        for i, (a, b, message) in enumerate(script.problems):
            out.construct("sh:syntax-error", a, max(a, b), False)
            if i < _PROBLEMS:
                out.note(f"shell syntax at byte {out.to_byte(a)}: {message}", "warning")
        ctx = Context(out, me, path, resolver(script, text))
        body = lower(ctx, script)
        out.behavior(0, len(text), me, "shell", body, "open_code")
        return out.done()
