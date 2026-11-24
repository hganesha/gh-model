"""GitHub connection for local use (first slice of GI-005).

Uses the GitHub CLI session already present on this machine: metadata through
`gh api`, objects through `git fetch` with `gh` as the credential helper. No
token is read, stored or logged here. Only the one requested commit is
fetched, into a bare per-tenant mirror; nothing is checked out and no hook runs.

A GitHub App with webhooks is the production route and is not built yet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from collections.abc import Callable
from pathlib import Path

_SLUG = re.compile(r"^(?:https?://github\.com/|git@github\.com:)?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubError(RuntimeError):
    oauth_error: str | None = None  # the OAuth `error` code a refusal carried, when there was one


@dataclass(frozen=True)
class RepoInfo:
    repository_id: str  # github.com/<numeric id>: stable across rename and transfer
    owner: str
    name: str
    default_branch: str
    private: bool
    description: str
    size_kb: int

    def row(self) -> dict:
        return asdict(self)


def parse_slug(url_or_slug: str) -> tuple[str, str]:
    m = _SLUG.match(url_or_slug.strip())
    if not m or ".." in m.group(2):
        raise GitHubError("enter a GitHub repository such as https://github.com/owner/name")
    return m.group(1), m.group(2)


def _gh(*args: str) -> str:
    try:
        out = subprocess.run(["gh", "api", "--hostname", "github.com", *args[1:]], check=True, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise GitHubError("the GitHub CLI (gh) is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("GitHub did not answer in time") from exc
    except subprocess.CalledProcessError as exc:
        text = (exc.stderr or "").lower()
        if "404" in text or "not found" in text:
            raise GitHubError("repository or ref not found, or this account cannot see it") from exc
        if "auth" in text or "401" in text:
            raise GitHubError("the GitHub CLI is not signed in; run `gh auth login`") from exc
        raise GitHubError("GitHub request failed") from exc
    return out.stdout


def describe(url_or_slug: str) -> RepoInfo:
    owner, name = parse_slug(url_or_slug)
    d = json.loads(_gh("api", f"repos/{owner}/{name}"))
    return RepoInfo(f"github.com/{d['id']}", d["owner"]["login"], d["name"], d["default_branch"], bool(d["private"]), d.get("description") or "", int(d.get("size") or 0))


def resolve_commit(info: RepoInfo, ref: str | None) -> tuple[str, str | None]:
    """Resolve a branch, tag or SHA to (full commit SHA, commit date in ISO 8601)."""
    ref = ref or info.default_branch
    if not _REF.match(ref) or ".." in ref:
        raise GitHubError("invalid ref")
    d = json.loads(_gh("api", f"repos/{info.owner}/{info.name}/commits/{ref}"))
    sha = d["sha"]
    if not _SHA.match(sha):
        raise GitHubError("GitHub returned an unexpected commit id")
    date = ((d.get("commit") or {}).get("committer") or {}).get("date")
    return sha, date if isinstance(date, str) and len(date) <= 40 else None


def partial_fetch(git: Callable[[list[str], int, bytes | None], object], mirror: Path, url: str, sha: str) -> None:
    """Fetch one commit as a partial clone: its trees first, then only the blobs a snapshot reads.

    Vendored, generated and binary files, and whatever the repository's own policy excludes,
    are never downloaded. `git(args, timeout, stdin)` runs Git with the connection's credentials.
    """
    from repo_intel.ingest.snapshot import missing_objects, wanted_blobs

    for key, value in (
        ("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"), ("remote.origin.url", url),
        ("remote.origin.promisor", "true"), ("remote.origin.partialclonefilter", "blob:none"),
    ):
        git(["-C", str(mirror), "config", key, value], 60, None)
    if missing_objects(mirror, [sha]):
        git(["-C", str(mirror), "fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules", "--filter=blob:none", "origin", sha], 900, None)
    def fetch_blobs(oids: list[str]) -> None:
        if missing := missing_objects(mirror, oids):
            git(["-C", str(mirror), "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", "--filter=blob:none", "--stdin", "origin"], 900, ("\n".join(missing) + "\n").encode())

    fetch_blobs(wanted_blobs(mirror, sha)[0])  # the policy first: it decides what else is read
    fetch_blobs(wanted_blobs(mirror, sha)[1])
    if missing_objects(mirror, sum(wanted_blobs(mirror, sha), [])):
        raise GitHubError("the host did not send every file of the commit")


def fetch_commit(info: RepoInfo, sha: str, mirrors_root: Path) -> Path:
    """Fetch exactly one commit into the repository's bare mirror; return the mirror path."""
    if not _SHA.match(sha):
        raise GitHubError("a full commit SHA is required")
    mirror = mirrors_root / (info.repository_id.replace("/", "_") + ".git")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    base = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    try:
        if not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([*base, "init", "-q", "--bare", str(mirror)], check=True, capture_output=True, env=env, timeout=60)
        partial_fetch(
            lambda args, timeout, stdin: subprocess.run([*base, *args], input=stdin, check=True, capture_output=True, env=env, timeout=timeout),
            mirror, f"https://github.com/{info.owner}/{info.name}.git", sha,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("fetching the repository took too long") from exc
    except subprocess.CalledProcessError as exc:
        raise GitHubError("could not fetch the commit from GitHub") from exc
    return mirror
