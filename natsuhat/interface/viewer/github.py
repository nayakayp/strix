"""Self-hosted, read-only GitHub App connections. No credentials leave the host."""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.sources import MAX_ARCHIVE, SourceStore
from natsuhat.utils.secret_files import dashboard_profiles_path, write_secret_text


class GithubConnection:
    def __init__(self, workspace: Path, sources: SourceStore) -> None:
        self.path = dashboard_profiles_path(workspace).with_name("github.json")
        self.sources = sources
        self._lock = threading.RLock()
        self._states: dict[str, tuple[float, str, str, int | None]] = {}

    @staticmethod
    def configured() -> bool:
        return all(
            os.getenv("NATSUHAT_GITHUB_" + key)
            for key in (
                "APP_ID",
                "PRIVATE_KEY_PATH",
                "CLIENT_ID",
                "CLIENT_SECRET",
                "CALLBACK_URL",
            )
        )

    def _read(self) -> dict[str, Any]:
        return (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"installations": [], "repositories": []}
        )

    @staticmethod
    def _request(method: str, url: str, token: str | None = None, **kwargs: Any) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(
                    method,
                    url,
                    headers={
                        "Accept": "application/json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        **({"Authorization": "Bearer " + token} if token else {}),
                    },
                    **kwargs,
                )
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
                try:
                    details = exc.response.json()
                except ValueError:
                    details = None
                if (
                    isinstance(details, dict)
                    and cast("dict[str, Any]", details).get("message") == "Git Repository is empty."
                ):
                    raise JobError(
                        "This repository is empty. Push your code to GitHub, then check again, "
                        "or choose a different repository.",
                        409,
                    ) from None
            raise JobError(
                "GitHub request failed. Check App permissions, installation access or rate limits.",
                502,
            ) from None
        if not isinstance(result, dict):
            raise JobError("GitHub returned an invalid response.", 502)
        result = cast("dict[str, Any]", result)
        if result.get("error"):
            raise JobError("GitHub authorization failed. Reconnect and approve App access.", 502)
        return result

    def _jwt(self) -> str:
        if not self.configured():
            raise JobError("Configure the GitHub App on the dashboard server first.", 409)

        def encode(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        now = int(time.time())
        message = (
            encode(b'{"alg":"RS256","typ":"JWT"}')
            + "."
            + encode(
                json.dumps(
                    {
                        "iat": now - 60,
                        "exp": now + 540,
                        "iss": os.environ["NATSUHAT_GITHUB_APP_ID"],
                    }
                ).encode()
            )
        )
        try:
            key = serialization.load_pem_private_key(
                Path(os.environ["NATSUHAT_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(), password=None
            )
        except (OSError, ValueError, TypeError):
            raise JobError("Cannot load the GitHub App RSA private key.", 409) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise JobError("The GitHub App private key must be RSA.", 409)
        signature = key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        return message + "." + encode(signature)

    def _token(self, installation: int, repository: int | None = None) -> str:
        result = self._request(
            "POST",
            f"https://api.github.com/app/installations/{installation}/access_tokens",
            self._jwt(),
            json={
                "permissions": {"contents": "read", "metadata": "read"},
                **({"repository_ids": [repository]} if repository else {}),
            },
        )
        return str(result["token"])

    def _pages(self, url: str, token: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 101):
            rows = self._request("GET", url, token, params={"per_page": 100, "page": page})[key]
            items.extend(rows)
            if len(rows) < 100:
                return items
        raise JobError("GitHub returned too many repositories; narrow the App's repository access.")

    def public(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            data = self._read()
        available: list[dict[str, Any]] = []
        if refresh:
            for installation in data["installations"]:
                allowed = data.get("allowed", {}).get(str(installation), [])
                available.extend(r for r in self._repositories(installation) if r["id"] in allowed)
            by_id = {r["id"]: r for r in available}
            with self._lock:
                data = self._read()
                repositories = [by_id.get(r["id"], r) for r in data["repositories"]]
                if repositories != data["repositories"]:
                    data["repositories"] = repositories
                    write_secret_text(self.path, json.dumps(data))
        return {
            "configured": self.configured(),
            "connected": bool(data["installations"]),
            "repositories": data["repositories"],
            "available": available,
        }

    def _repositories(self, installation: int) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"],
                "full_name": r["full_name"],
                "default_branch": r["default_branch"],
                "private": r["private"],
                "installation_id": installation,
            }
            for r in self._pages(
                "https://api.github.com/installation/repositories",
                self._token(installation),
                "repositories",
            )
        ]

    def _state(self, session: str, stage: str, installation: int | None = None) -> str:
        value = secrets.token_urlsafe(32)
        with self._lock:
            self._states = {k: v for k, v in self._states.items() if v[0] > time.monotonic()}
            self._states[value] = (time.monotonic() + 600, session, stage, installation)
        return value

    def connect(self, session: str) -> str:
        app = self._request("GET", "https://api.github.com/app", self._jwt())
        callback = os.environ["NATSUHAT_GITHUB_CALLBACK_URL"]
        parsed = urlsplit(callback)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
            or parsed.username
            or parsed.password
            or parsed.path != "/api/github/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise JobError(
                "Set the GitHub callback URL to the dashboard's /api/github/callback endpoint."
            )
        return (
            f"https://github.com/apps/{quote(app['slug'], safe='')}/installations/new?"
            + urlencode({"state": self._state(session, "install")})
        )

    def callback(self, query: dict[str, list[str]], session: str) -> str:
        state = query.get("state", [""])[0]
        with self._lock:
            saved = self._states.pop(state, None)
        if (
            saved is None
            or saved[0] < time.monotonic()
            or not secrets.compare_digest(saved[1], session)
        ):
            raise JobError(
                "GitHub connection expired or belongs to another session. Connect again.", 403
            )
        callback = os.environ["NATSUHAT_GITHUB_CALLBACK_URL"]
        if saved[2] == "install":
            try:
                installation = int(query.get("installation_id", [""])[0])
            except ValueError:
                raise JobError("GitHub installation was not completed.") from None
            return "https://github.com/login/oauth/authorize?" + urlencode(
                {
                    "client_id": os.environ["NATSUHAT_GITHUB_CLIENT_ID"],
                    "redirect_uri": callback,
                    "state": self._state(session, "oauth", installation),
                }
            )
        code = query.get("code", [""])[0]
        if not code:
            raise JobError("GitHub authorization was cancelled.")
        result = self._request(
            "POST",
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": os.environ["NATSUHAT_GITHUB_CLIENT_ID"],
                "client_secret": os.environ["NATSUHAT_GITHUB_CLIENT_SECRET"],
                "code": code,
                "redirect_uri": callback,
            },
        )
        token = str(result["access_token"])
        try:
            installations = self._pages(
                "https://api.github.com/user/installations", token, "installations"
            )
            if not any(
                i["id"] == saved[3] and str(i["app_id"]) == os.environ["NATSUHAT_GITHUB_APP_ID"]
                for i in installations
            ):
                raise JobError(
                    "This GitHub account cannot access the selected App installation.", 403
                )
            # Intersect installation access with the authorizing user's repository access.
            repos = self._pages(
                f"https://api.github.com/user/installations/{saved[3]}/repositories",
                token,
                "repositories",
            )
            allowed = {r["id"] for r in repos}
            with self._lock:
                data = self._read()
                installation = cast("int", saved[3])
                data["installations"] = [i for i in data["installations"] if i != installation] + [
                    installation
                ]
                data.setdefault("allowed", {})[str(installation)] = sorted(allowed)
                data["repositories"] = [
                    r
                    for r in data["repositories"]
                    if r["installation_id"] != installation or r["id"] in allowed
                ]
                write_secret_text(self.path, json.dumps(data))
        finally:
            # User access tokens are needed only to prove installation ownership.
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(JobError):
                self._request(
                    "DELETE",
                    f"https://api.github.com/applications/{os.environ['NATSUHAT_GITHUB_CLIENT_ID']}/token",
                    auth=(
                        os.environ["NATSUHAT_GITHUB_CLIENT_ID"],
                        os.environ["NATSUHAT_GITHUB_CLIENT_SECRET"],
                    ),
                    json={"access_token": token},
                )
        return callback.removesuffix("/api/github/callback") + "/#page=integrations"

    def update(self, action: str, body: dict[str, Any], session: str) -> dict[str, Any]:
        if action == "connect":
            return {"url": self.connect(session)}
        if action == "check":
            checked_repository, _token, sha = self.resolve(
                body.get("repository_id"), body.get("ref")
            )
            return {
                "repository_id": checked_repository["id"],
                "ref": body["ref"].strip(),
                "commit": sha,
            }
        with self._lock:
            data = self._read()
            if action == "disconnect":
                write_secret_text(self.path, json.dumps({"installations": [], "repositories": []}))
                self._states.clear()
            elif action in {"add", "remove"}:
                identifier = body.get("id")
                data["repositories"] = [r for r in data["repositories"] if r["id"] != identifier]
                if action == "add":
                    available = [r for i in data["installations"] for r in self._repositories(i)]
                    repository = next(
                        (
                            r
                            for r in available
                            if r["id"] == identifier
                            and identifier
                            in data.get("allowed", {}).get(str(r["installation_id"]), [])
                        ),
                        None,
                    )
                    if repository is None:
                        raise JobError("Repository is not available to this connection.", 403)
                    data["repositories"].append(repository)
                write_secret_text(self.path, json.dumps(data))
            else:
                raise JobError("Unknown GitHub action.", 404)
        return self.public(refresh=True)

    def repository_token(self, repository_id: Any) -> tuple[dict[str, Any], str]:
        with self._lock:
            data = self._read()
            repository = next((r for r in data["repositories"] if r["id"] == repository_id), None)
        if repository is None:
            raise JobError("Connect this GitHub repository first.", 403)
        return repository, self._token(repository["installation_id"], repository["id"])

    def resolve(self, repository_id: Any, ref: Any) -> tuple[dict[str, Any], str, str]:
        """Check connected source access and resolve a commit without downloading or scanning."""
        if (
            not isinstance(ref, str)
            or not ref.strip()
            or len(ref) > 300
            or any(ord(c) < 32 for c in ref)
        ):
            raise JobError("Choose a branch, tag, or commit (at most 300 characters).")
        repository, token = self.repository_token(repository_id)
        repo = quote(repository["full_name"], safe="/")
        commit = self._request(
            "GET",
            f"https://api.github.com/repos/{repo}/commits/{quote(ref.strip(), safe='')}",
            token,
        )
        sha = str(commit["sha"])
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise JobError("GitHub returned an invalid commit.", 502)
        return repository, token, sha

    def download(self, repository_id: Any, ref: Any) -> str:
        repository, token, sha = self.resolve(repository_id, ref)
        repo = quote(repository["full_name"], safe="/")
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryFile() as temporary:
            try:
                with httpx.Client(timeout=60, follow_redirects=False) as client:
                    response = client.get(
                        f"https://api.github.com/repos/{repo}/zipball/{sha}",
                        headers={"Authorization": "Bearer " + token},
                    )
                    if response.status_code != 302:
                        response.raise_for_status()
                        raise JobError("GitHub did not return an archive redirect.", 502)
                    location: str = response.headers.get("location", "")
                    parsed = urlsplit(location)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "codeload.github.com"
                        or parsed.username
                        or parsed.password
                        or parsed.port not in {None, 443}
                    ):
                        raise JobError("GitHub returned an unsafe archive redirect.", 502)
                    size = 0
                    with client.stream("GET", location) as download:
                        download.raise_for_status()
                        for chunk in download.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_ARCHIVE:
                                raise JobError("Repository exceeds the 100 MiB archive limit.", 413)
                            temporary.write(chunk)
                temporary.seek(0)
                metadata = self.sources.upload(
                    temporary, size, repository["full_name"].split("/")[-1] + ".zip"
                )
            except (httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, JobError):
                    raise
                raise JobError(
                    "Could not download the repository snapshot from GitHub.", 502
                ) from None
        identifier = metadata["upload_id"]
        metadata.update(
            kind="github",
            repository_id=repository["id"],
            repository=repository["full_name"],
            ref=ref.strip(),
            commit=sha,
        )
        write_secret_text(self.sources.directory(identifier) / "source.json", json.dumps(metadata))
        return str(identifier)
