"""Webhook events that move tracked refs (spec §4.5, GI-018).

`verify_event` authenticates the raw delivery exactly as `connectors.verify_push`
does and then accepts three kinds of event:

- a branch push (`refs/heads/x`), tracked as ref `x`;
- a tag push (`refs/tags/v1`), tracked as ref `refs/tags/v1`, compiled eagerly;
- a pull or merge request opened, reopened, updated or closed, tracked as
  `refs/pull/<n>/head` (GitHub) or `refs/merge-requests/<n>/head` (GitLab).

Deleted refs and other events are ignored. Payload content beyond IDs, refs
and SHAs is never read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from .connectors import SHA, ConnectorError, SecretReferences, checked_ref
from .settings import Connection

PR_OPEN = {"opened", "reopened", "synchronize", "ready_for_review"}
MR_OPEN = {"open", "reopen", "update"}


@dataclass(frozen=True)
class RefEvent:
    delivery: str
    repository_id: str
    ref: str
    sha: str | None  # None when the ref closed
    kind: Literal["branch", "tag", "pull_request"]
    action: Literal["moved", "closed"] = "moved"


def _authenticate(config: Connection, headers: Any, raw: bytes, secrets: SecretReferences) -> tuple[bool, str | None, str | None]:
    github_app = config.mode == "github_app"
    if not github_app and config.mode != "gitlab_token":
        raise PermissionError("This connection does not accept webhooks.")
    secret = secrets.get(config.webhook_ref)
    if github_app:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        supplied = headers.get("x-hub-signature-256", "")
        event, delivery = headers.get("x-github-event"), headers.get("x-github-delivery")
    else:
        expected = secret
        supplied = headers.get("x-gitlab-token", "")
        event, delivery = headers.get("x-gitlab-event"), headers.get("x-gitlab-event-uuid") or headers.get("x-request-id")
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise PermissionError("Invalid webhook signature.")
    return github_app, event, delivery


def verify_event(config: Connection, headers: Any, raw: bytes, secrets: SecretReferences) -> RefEvent | None:
    github_app, event, delivery = _authenticate(config, headers, raw, secrets)
    known = {"push", "pull_request"} if github_app else {"Push Hook", "Tag Push Hook", "Merge Request Hook"}
    if event not in known:
        return None
    try:
        data = json.loads(raw)
        if github_app and str(data["installation"]["id"]) != config.installation_id:
            raise PermissionError("This push belongs to another installation.")
        if not delivery or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery):
            raise ValueError
        project = str(int(data["repository" if github_app else "project"]["id"]))
        repository_id = f"{config.host}/{project}"
        if event in ("push", "Push Hook", "Tag Push Hook"):
            ref, sha = data["ref"], data["after"]
            if sha == "0" * 40 or sha == "0" * 64:
                return None  # the ref was deleted
            if not SHA.fullmatch(sha):
                raise ValueError
            if ref.startswith("refs/heads/"):
                return RefEvent(delivery, repository_id, checked_ref(ref.removeprefix("refs/heads/")), sha, "branch")
            if ref.startswith("refs/tags/"):
                checked_ref(ref.removeprefix("refs/tags/"))
                return RefEvent(delivery, repository_id, ref, sha, "tag")
            return None
        if github_app:
            action, pr = data["action"], data["pull_request"]
            number, sha = int(pr["number"]), pr["head"]["sha"]
            ref = f"refs/pull/{number}/head"
            closed = action == "closed"
            if not closed and action not in PR_OPEN:
                return None
        else:
            attrs = data["object_attributes"]
            action, number, sha = attrs.get("action"), int(attrs["iid"]), attrs["last_commit"]["id"]
            ref = f"refs/merge-requests/{number}/head"
            closed = action in ("close", "merge")
            if not closed and action not in MR_OPEN:
                return None
        if number < 1 or not SHA.fullmatch(sha):
            raise ValueError
        return RefEvent(delivery, repository_id, ref, None if closed else sha, "pull_request", "closed" if closed else "moved")
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ConnectorError("Invalid webhook payload.") from None
