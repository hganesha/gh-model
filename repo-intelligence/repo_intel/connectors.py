"""Repository host adapters. Credentials stay in the server process or Git child environment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import github

if TYPE_CHECKING:
    from .settings import Connection

ConnectorError = github.GitHubError
SHA = re.compile(r"[a-f0-9]{40}")
GIT_SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
REF = re.compile(r"(?![-./])[A-Za-z0-9._/-]{1,200}")
HOST = re.compile(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?")
REMOTE_PATH = re.compile(r"/[A-Za-z0-9_./~-]{1,450}")
KEYCHAIN_SERVICE = "repo-intelligence"


def checked_ref(ref: str) -> str:
    if not REF.fullmatch(ref) or ".." in ref or "//" in ref or ref.endswith(("/", ".")):
        raise ConnectorError("Invalid Git ref.")
    return ref


def validate_git_remote(url: str, host: str, mode: str) -> str:
    p = urlsplit(url)
    expected = "ssh" if mode == "git_ssh" else "https"
    actual_host = p.netloc.rsplit("@", 1)[-1]
    if (
        p.scheme != expected or actual_host != host or not HOST.fullmatch(host)
        or p.password is not None or (p.username is not None and (expected != "ssh" or not re.fullmatch(r"[A-Za-z0-9_-]+", p.username)))
        or not REMOTE_PATH.fullmatch(p.path) or ".." in p.path.split("/") or "." in p.path.split("/")
        or p.query or p.fragment or url != urlunsplit(p)
    ):
        raise ValueError("Use a credential-free remote URL for the configured host and protocol.")
    return url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url: str, headers: dict[str, str] | None = None, form: dict[str, str] | None = None) -> tuple[Any, Any]:
    data = urlencode(form).encode() if form is not None else None
    request = Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ConnectorError("Repository host response was too large.")
            return json.loads(raw), response.headers
    except HTTPError as exc:
        error = ConnectorError(f"Repository host returned HTTP {exc.code}.")
        try:  # OAuth endpoints explain a refusal with an error code in the body
            code = json.loads(exc.read(64 * 1024)).get("error")
            error.oauth_error = code if isinstance(code, str) else None
        except (ValueError, AttributeError, OSError):
            pass
        raise error from None
    except (URLError, ValueError, TimeoutError):
        raise ConnectorError("Repository host could not be reached or returned invalid data.") from None


VAULT_SECRET = re.compile(r"^[0-9a-zA-Z-]{1,127}$")
VAULT_SCOPE = "https://vault.azure.net/.default"


class SecretReferences:
    """Where credentials live: the OS keychain locally (`keychain:`), files mounted by a
    secret manager (`managed:`), or Azure Key Vault (`vault:`) when hosted, reached with
    the app's managed identity. Keychain and Key Vault can also store device sign-in tokens."""

    def __init__(self, vault_url: str | None = None, credential: Any = None, client: Any = None) -> None:
        self.vault_url = (os.environ.get("RKP_KEY_VAULT_URL", "") if vault_url is None else vault_url).rstrip("/")
        self._credential = credential
        self._client = client
        self._auth: Callable[[], dict[str, str]] | None = None

    @property
    def writable(self) -> str:
        """The store device sign-in writes to: Key Vault when hosted, else the OS keychain."""
        return "vault" if self.vault_url else "keychain"

    def device_reference(self, connection_id: str) -> str:
        name = re.sub(r"[^0-9a-zA-Z-]", "-", f"github-device-{connection_id}")[:100]
        return f"{self.writable}:{name}"

    def _vault(self, name: str, method: str, body: dict | None = None) -> Any:
        if not self.vault_url:
            raise ConnectorError("No Key Vault is configured for this server (RKP_KEY_VAULT_URL).")
        if not VAULT_SECRET.fullmatch(name):
            raise ConnectorError("Key Vault secret names may contain only letters, digits and '-'.")
        if self._auth is None:
            from .decisions.gateway import azure_credential, bearer
            self._auth = bearer(self._credential or azure_credential("managed-identity"), VAULT_SCOPE)
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=15)
        try:
            headers = self._auth()
        except Exception:
            raise ConnectorError("The server's identity could not sign in to Key Vault.") from None
        response = self._client.request(method, f"{self.vault_url}/secrets/{name}", params={"api-version": "7.4"}, headers=headers, json=body)
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code >= 400:
            raise ConnectorError(f"Key Vault refused the request (HTTP {response.status_code}).")
        return response.json()

    def get(self, reference: str | None) -> str:
        from .settings import REF
        if not reference or not REF.fullmatch(reference):
            raise ConnectorError("A credential reference is required.")
        kind, key = reference.split(":", 1)
        try:
            if kind == "managed":
                root = os.environ.get("RKP_MANAGED_SECRET_DIR")
                if not root or key in (".", ".."):
                    raise ValueError
                value = (Path(root) / key).read_text(encoding="utf-8").strip()
            elif kind == "vault":
                value = ((self._vault(key, "GET") or {}).get("value") or "").strip()
            else:
                import keyring
                value = keyring.get_password(KEYCHAIN_SERVICE, key)
        except ConnectorError:
            raise
        except Exception:
            raise ConnectorError("Credential reference could not be read.") from None
        if not value:
            raise ConnectorError("Credential reference has no value.")
        return value

    def put_device_token(self, reference: str, value: str) -> None:
        kind, _, key = reference.partition(":")
        if kind == "vault":
            self._vault(key, "PUT", {"value": value, "contentType": "github-oauth-token"})
            return
        if kind != "keychain":
            raise ConnectorError("Device sign-in needs a writable store: an OS keychain or Key Vault reference.")
        try:
            import keyring
            keyring.set_password(KEYCHAIN_SERVICE, key, value)
        except Exception:
            raise ConnectorError("The operating-system keychain could not store the credential.") from None


@dataclass(frozen=True)
class RemoteRepository:
    repository_id: str
    selector: str
    full_name: str
    default_branch: str
    private: bool
    description: str = ""
    clone_url: str | None = None

    def row(self) -> dict:
        return asdict(self)


class Connector(Protocol):
    def describe(self, selector: str) -> RemoteRepository: ...
    def repositories(self, page: int = 1) -> list[RemoteRepository]: ...
    def resolve(self, repo: RemoteRepository, ref: str) -> tuple[str, str | None]: ...
    def fetch(self, repo: RemoteRepository, sha: str, mirrors: Path) -> Path: ...


class GitHubCli:
    def describe(self, selector: str) -> RemoteRepository:
        if selector.isdecimal():
            return _github_repo(json.loads(github._gh("api", f"repositories/{selector}")))
        info = github.describe(selector)
        return self._convert(info)

    @staticmethod
    def _convert(info: github.RepoInfo) -> RemoteRepository:
        slug = f"{info.owner}/{info.name}"
        return RemoteRepository(info.repository_id, slug, slug, info.default_branch, info.private, info.description)

    def repositories(self, page: int = 1) -> list[RemoteRepository]:
        rows = json.loads(github._gh("api", f"user/repos?per_page=100&page={page}&sort=full_name"))
        return [_github_repo(row) for row in rows]

    @staticmethod
    def _info(repo: RemoteRepository) -> github.RepoInfo:
        owner, name = github.parse_slug(repo.selector)
        return github.RepoInfo(repo.repository_id, owner, name, repo.default_branch, repo.private, repo.description, 0)

    def resolve(self, repo: RemoteRepository, ref: str) -> tuple[str, str | None]:
        return github.resolve_commit(self._info(repo), checked_ref(ref))

    def fetch(self, repo: RemoteRepository, sha: str, mirrors: Path) -> Path:
        return github.fetch_commit(self._info(repo), sha, mirrors)


def _github_repo(row: dict) -> RemoteRepository:
    owner, name = github.parse_slug(row["full_name"])
    slug = f"{owner}/{name}"
    return RemoteRepository(
        f"github.com/{int(row['id'])}", slug, slug, row.get("default_branch") or "HEAD",
        row.get("private", True), row.get("description") or "", f"https://github.com/{slug}.git",
    )


def _gitlab_repo(row: dict, host: str) -> RemoteRepository:
    slug = row["path_with_namespace"]
    try:
        url = validate_git_remote(row["http_url_to_repo"], host, "git_https")
    except (KeyError, TypeError, ValueError):
        raise ConnectorError("GitLab did not return a clone URL for the configured host.") from None
    project_id = str(int(row["id"]))
    return RemoteRepository(
        f"{host}/{project_id}", project_id, slug, row.get("default_branch") or "HEAD",
        row.get("visibility") != "public", row.get("description") or "", url,
    )


class InstallationTokens:
    def __init__(self, secrets: SecretReferences):
        import threading
        self.secrets = secrets
        self.lock = threading.Lock()
        self.cached: dict[tuple[str, str, str, str], tuple[str, float]] = {}
        self.permissions: dict[str, list[str]] = {}

    def get(self, connection: Connection) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key_value = self.secrets.get(connection.credential_ref)
        key = (connection.id, connection.app_id, connection.installation_id, hashlib.sha256(key_value.encode()).hexdigest())
        with self.lock:
            now = int(time.time())
            cached = self.cached.get(key)
            if cached and cached[1] > now + 60:
                return cached[0]
            def encode(value: bytes) -> bytes:
                return base64.urlsafe_b64encode(value).rstrip(b"=")
            claims = json.dumps({"iat": now - 60, "exp": now + 540, "iss": connection.app_id}).encode()
            unsigned = encode(b'{"alg":"RS256","typ":"JWT"}') + b"." + encode(claims)
            try:
                private = serialization.load_pem_private_key(key_value.encode(), password=None)
                signature = private.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
            except (TypeError, ValueError, AttributeError):
                raise ConnectorError("The GitHub App key is not a valid RSA private key.") from None
            jwt = (unsigned + b"." + encode(signature)).decode()
            result, _ = request_json(
                f"https://api.github.com/app/installations/{connection.installation_id}/access_tokens",
                {"Authorization": f"Bearer {jwt}", "X-GitHub-Api-Version": "2026-03-10", "Content-Type": "application/json"}, {},
            )
            from datetime import datetime
            expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
            self.cached = {k: token for k, token in self.cached.items() if k[0] != connection.id and token[1] > now}
            self.cached[key] = result["token"], expires
            self.permissions[connection.id] = [f"{k}:{v}" for k, v in sorted(result.get("permissions", {}).items())]
            return result["token"]


class AuthenticatedGit:
    def __init__(self, config: Connection, credential: Callable[[], str]):
        self.config, self.credential = config, credential

    def run(self, args: list[str], *, timeout: int = 60, authenticate: bool = True) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "ssh" if self.config.mode == "git_ssh" else "https", "LC_ALL": "C",
        }
        flags = ["-c", f"core.hooksPath={os.devnull}", "-c", "credential.helper=", "-c", "http.followRedirects=false"]
        with TemporaryDirectory(prefix="rkp-git-") as directory:
            if authenticate and self.config.mode == "git_ssh":
                key = Path(directory) / "key"
                key.touch(mode=0o600)
                key.write_text(self.credential() + "\n")
                env["GIT_SSH_COMMAND"] = shlex.join([
                    "ssh", "-F", os.devnull, "-i", str(key), "-o", "IdentitiesOnly=yes", "-o",
                    "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                ])
            elif authenticate:
                helper = Path(directory) / "askpass"
                helper.write_text('#!/bin/sh\ncase "$1" in Username*) printf "%s\\n" "$RKP_GIT_USERNAME";; *) printf "%s\\n" "$RKP_GIT_PASSWORD";; esac\n')
                helper.chmod(0o700)
                env.update(
                    GIT_ASKPASS=str(helper), RKP_GIT_USERNAME="oauth2" if self.config.mode == "gitlab_token" else "x-access-token",
                    RKP_GIT_PASSWORD=self.credential(),
                )
            try:
                return subprocess.run(["git", *flags, *args], env=env, capture_output=True, check=True, text=True, timeout=timeout).stdout
            except (subprocess.SubprocessError, OSError):
                raise ConnectorError("Git could not read this ref with the configured connection.") from None

    def fetch(self, repo: RemoteRepository, sha: str, mirrors: Path) -> Path:
        if not GIT_SHA.fullmatch(sha):
            raise ConnectorError("Expected a full commit SHA.")
        mode = "git_ssh" if self.config.mode == "git_ssh" else "git_https"
        url = validate_git_remote(repo.clone_url or "", self.config.host, mode)
        name = hashlib.sha256(repo.repository_id.encode()).hexdigest()
        mirror = mirrors / f"{name}.git"
        mirror.mkdir(parents=True, exist_ok=True)
        self.run(["init", "-q", "--bare", f"--object-format=sha{256 if len(sha) == 64 else 1}", str(mirror)], authenticate=False)
        # Recheck access through ls-remote/REST at resolve time, even when the object is cached.
        try:
            self.run(["-C", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"], authenticate=False)
        except ConnectorError:
            self.run(["-C", str(mirror), "fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules", url, sha], timeout=900)
        return mirror


class HostedConnector(AuthenticatedGit):
    def __init__(self, config: Connection, credential: Callable[[], str]):
        super().__init__(config, credential)
        self.gitlab = config.mode == "gitlab_token"
        self.base = f"https://{config.host}/api/v4" if self.gitlab else "https://api.github.com"
        self.scopes: list[str] = []

    def _get(self, endpoint: str) -> Any:
        value = self.credential()
        headers = {"PRIVATE-TOKEN": value} if self.gitlab else {
            "Authorization": f"Bearer {value}", "X-GitHub-Api-Version": "2026-03-10",
        }
        data, response_headers = request_json(self.base + endpoint, headers)
        scopes = response_headers.get("X-OAuth-Scopes")
        if scopes:
            self.scopes = sorted(s.strip() for s in scopes.split(",") if s.strip())
        return data

    def _selector(self, selector: str) -> str:
        if self.gitlab:
            if "://" in selector:
                p = urlsplit(selector)
                if p.netloc != self.config.host or p.scheme != "https" or p.query or p.fragment:
                    raise ConnectorError("Choose a project on the configured GitLab host.")
                selector = p.path.strip("/").removesuffix(".git")
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*", selector) or ".." in selector.split("/"):
                raise ConnectorError("Enter a GitLab project path or ID.")
            return "/projects/" + quote(selector, safe="")
        if selector.isdecimal():
            return f"/repositories/{selector}"
        owner, name = github.parse_slug(selector)
        return f"/repos/{owner}/{name}"

    def describe(self, selector: str) -> RemoteRepository:
        data = self._get(self._selector(selector))
        return _gitlab_repo(data, self.config.host) if self.gitlab else _github_repo(data)

    def repositories(self, page: int = 1) -> list[RemoteRepository]:
        if self.gitlab:
            rows = self._get(f"/projects?membership=true&order_by=id&per_page=100&page={page}")
            return [_gitlab_repo(row, self.config.host) for row in rows]
        if self.config.mode == "github_app":
            rows = self._get(f"/installation/repositories?per_page=100&page={page}")["repositories"]
        else:
            rows = self._get(f"/user/repos?per_page=100&page={page}&sort=full_name")
        return [_github_repo(row) for row in rows]

    def resolve(self, repo: RemoteRepository, ref: str) -> tuple[str, str | None]:
        key = quote(checked_ref(ref), safe="")
        base = self._selector(repo.selector)
        if self.gitlab:
            row = self._get(f"{base}/repository/commits/{key}")
            sha, date = row["id"], row.get("committed_date")
        else:
            row = self._get(f"{base}/commits/{key}")
            sha, date = row["sha"], (row.get("commit", {}).get("committer") or {}).get("date")
        if not SHA.fullmatch(sha):
            raise ConnectorError("Repository host returned an unexpected commit id.")
        return sha, date


class PlainGit(AuthenticatedGit):
    def _remote(self) -> str:
        return validate_git_remote(self.config.remote or "", self.config.host, self.config.mode)

    def describe(self, selector: str) -> RemoteRepository:
        remote = self._remote()
        if selector and selector != remote:
            raise ConnectorError("Choose the remote configured for this connection.")
        head = self.run(["ls-remote", "--symref", "--", remote, "HEAD"])
        branch = next((line.split("\t", 1)[0][len("ref: refs/heads/"):] for line in head.splitlines()
                       if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")), "HEAD")
        url = urlsplit(remote)
        path = url.path.rstrip("/").removesuffix(".git")
        stable = hashlib.sha256(f"{self.config.host}{path}".encode()).hexdigest()[:24]
        return RemoteRepository(f"{self.config.host}/git-{stable}", remote, path.strip("/"), branch, True, clone_url=remote)

    def repositories(self, page: int = 1) -> list[RemoteRepository]:
        return [self.describe("")] if page == 1 else []

    def resolve(self, repo: RemoteRepository, ref: str) -> tuple[str, str | None]:
        ref = checked_ref(ref)
        if GIT_SHA.fullmatch(ref):
            return ref, None
        refs = [ref] if ref == "HEAD" or ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
        output = self.run(["ls-remote", "--", self._remote(), *refs])
        entries = dict((name, sha) for sha, name in (line.split("\t", 1) for line in output.splitlines()))
        order = list(refs)
        if len(refs) == 3:
            order = [refs[0], refs[2], refs[1]]
        elif ref.startswith("refs/tags/"):
            peeled = self.run(["ls-remote", "--", self._remote(), ref + "^{}"])
            if peeled:
                entries[ref] = peeled.split("\t", 1)[0]
        sha = next((entries[name] for name in order if name in entries), "")
        if not GIT_SHA.fullmatch(sha):
            raise ConnectorError("Git ref was not found.")
        return sha, None


def build(config: Connection | None, secrets: SecretReferences, apps: InstallationTokens) -> Connector:
    if config is None:
        return GitHubCli()
    credential = (
        (lambda: apps.get(config)) if config.mode == "github_app" else
        (lambda: secrets.get(config.credential_ref or secrets.device_reference(config.id)))
    )
    return PlainGit(config, credential) if config.mode.startswith("git_") else HostedConnector(config, credential)


# GitHub answers a refused device-code request with HTTP 400 and an OAuth error code; say what to fix.
DEVICE_ERRORS = {
    "device_flow_disabled": "Device sign-in is not enabled for this GitHub OAuth App. Tick “Enable Device Flow” in the app's settings on GitHub, then try again.",
    "incorrect_client_credentials": "GitHub does not recognise this OAuth client ID. Copy the Client ID from the OAuth App's page on GitHub.",
    "unauthorized_client": "This GitHub app cannot use device sign-in. Use an OAuth App with “Enable Device Flow” ticked.",
}


def begin_device(config: Connection) -> dict[str, Any]:
    try:
        data, _ = request_json(
            "https://github.com/login/device/code", form={"client_id": config.client_id, "scope": "repo"},
        )
    except ConnectorError as exc:
        if exc.oauth_error in DEVICE_ERRORS:
            raise ConnectorError(DEVICE_ERRORS[exc.oauth_error]) from None
        raise
    if not isinstance(data, dict) or "device_code" not in data:
        code = data.get("error") if isinstance(data, dict) else None
        raise ConnectorError(DEVICE_ERRORS.get(code, "GitHub refused device sign-in."))
    now = time.monotonic()
    interval = max(5, int(data["interval"]))
    return {
        "device_code": data["device_code"], "user_code": data["user_code"], "interval": interval,
        "expires": now + int(data["expires_in"]), "next_poll": now + interval,
    }


def finish_device(config: Connection, state: dict[str, Any], secrets: SecretReferences) -> str:
    now = time.monotonic()
    if now >= state["expires"]:
        return "expired"
    if now < state["next_poll"]:
        return "pending"
    state["next_poll"] = now + state["interval"]
    data, _ = request_json(
        "https://github.com/login/oauth/access_token",
        form={"client_id": config.client_id, "device_code": state["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
    )
    if "access_token" in data:
        secrets.put_device_token(config.credential_ref or secrets.device_reference(config.id), data["access_token"])
        return "complete"
    if data.get("error") == "slow_down":
        state["interval"] += 5
        state["next_poll"] = now + state["interval"]
    if data.get("error") in ("authorization_pending", "slow_down"):
        return "pending"
    return "expired" if data.get("error") == "expired_token" else "denied"


def verify_push(config: Connection, headers: Any, raw: bytes, secrets: SecretReferences) -> tuple[str, str, str, str] | None:
    """Authenticate the raw bytes before accepting a registered push delivery."""
    import hmac

    github_app = config.mode == "github_app"
    if not github_app and config.mode != "gitlab_token":
        raise PermissionError("This connection does not accept webhooks.")
    secret = secrets.get(config.webhook_ref)
    if github_app:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        supplied = headers.get("x-hub-signature-256", "")
        event = headers.get("x-github-event")
        delivery = headers.get("x-github-delivery")
    else:
        expected = secret
        supplied = headers.get("x-gitlab-token", "")
        event = headers.get("x-gitlab-event")
        delivery = headers.get("x-gitlab-event-uuid") or headers.get("x-request-id")
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise PermissionError("Invalid webhook signature.")
    if event != ("push" if github_app else "Push Hook"):
        return None
    try:
        data = json.loads(raw)
        if github_app and str(data["installation"]["id"]) != config.installation_id:
            raise PermissionError("This push belongs to another installation.")
        project = str(int(data["repository" if github_app else "project"]["id"]))
        ref, sha = data["ref"], data["after"]
        if not ref.startswith("refs/heads/") or sha == "0" * 40:
            return None
        ref = checked_ref(ref.removeprefix("refs/heads/"))
        if not SHA.fullmatch(sha) or not delivery or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delivery):
            raise ValueError
        return delivery, f"{config.host}/{project}", ref, sha
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ConnectorError("Invalid push payload.") from None
