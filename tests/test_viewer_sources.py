"""Source ingestion and GitHub contracts, with no scans or external requests."""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from natsuhat.interface.cli_args import parse_arguments
from natsuhat.interface.viewer import github, job_runner, sources
from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.server import SESSION_COOKIE_PREFIX, serve
from natsuhat.runtime.session_manager import build_bind_mounts


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def archive_bytes(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in entries.items():
            bundle.writestr(name, value)
    return output.getvalue()


def test_source_snapshot_is_filtered_isolated_and_reproducible(tmp_path: Path) -> None:
    store = sources.SourceStore(tmp_path)
    raw = archive_bytes(
        {
            "repo-main/src/auth.py": "ORIGINAL",
            "repo-main/README.md": "Codebase",
            "repo-main/.git/config": "credential=must-not-transfer",
            "__MACOSX/._README.md": "metadata",
            "repo-main/.gitmodules": "[submodule]",
            "repo-main/model.bin": "version https://git-lfs.github.com/spec/v1\n",
        }
    )
    uploaded = store.upload(io.BytesIO(raw), len(raw), "codebase.zip")
    assert uploaded["files"] == 4
    assert len(uploaded["warnings"]) == 2
    original = store.directory(uploaded["upload_id"]) / "source.zip"
    with zipfile.ZipFile(original) as bundle:
        assert set(bundle.namelist()) == {
            "code/src/auth.py",
            "code/README.md",
            "code/.gitmodules",
            "code/model.bin",
        }
        assert "credential=must-not-transfer" not in str(
            [bundle.read(n) for n in bundle.namelist()]
        )
    first = "local-" + "a" * 32
    second = "e2b-" + "b" * 32
    metadata = store.snapshot(uploaded["upload_id"], first)
    assert "upload_id" not in metadata
    assert not original.exists()
    sources.extract_source(store.directory(first) / "source.zip", tmp_path / "working")
    (tmp_path / "working/src/auth.py").write_text("CHANGED BY SCAN")
    assert store.snapshot(first, second) == metadata
    sources.extract_source(store.directory(second) / "source.zip", tmp_path / "rerun")
    assert (tmp_path / "rerun/src/auth.py").read_text() == "ORIGINAL"
    store.remove(first)
    assert not store.directory(first).exists()
    assert store.directory(second).exists()
    unused = store.upload(io.BytesIO(raw), len(raw), "unused.zip")
    old = store.directory(unused["upload_id"])
    os.utime(old, (0, 0))
    with pytest.raises(JobError, match="expired"):
        store.snapshot(unused["upload_id"], "local-" + "c" * 32)
    store.expire_uploads()
    assert not old.exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "/absolute.py", "C:/drive.py", "a\\b.py", "a/../b.py", "a//b.py", "a/./b.py"],
)
def test_zip_rejects_unsafe_paths_without_partial_files(tmp_path: Path, name: str) -> None:
    store = sources.SourceStore(tmp_path)
    raw = archive_bytes({"good.py": "safe", name: "unsafe"})
    with pytest.raises(JobError, match="unsafe"):
        store.upload(io.BytesIO(raw), len(raw), "bad.zip")
    assert list(store.root.iterdir()) == []
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "link",
        "duplicate",
        "collision",
        "empty",
        "truncated",
        "invalid",
        "entry-limit",
        "size-limit",
    ],
)
def test_zip_rejects_invalid_and_over_limit_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    store = sources.SourceStore(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        if kind == "link":
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, "../outside")
        elif kind != "empty":
            bundle.writestr("good.py", "a" * 129)
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                bundle.writestr("good.py", "duplicate")
        if kind == "collision":
            bundle.writestr("good.py/child", "cannot be a directory")
    raw = b"not-a-zip" if kind == "invalid" else output.getvalue()
    if kind == "entry-limit":
        monkeypatch.setattr(sources, "MAX_ENTRIES", 0)
    if kind == "size-limit":
        monkeypatch.setattr(sources, "MAX_EXTRACTED", 128)
    length = len(raw) + 3 if kind == "truncated" else len(raw)
    with pytest.raises(JobError):
        store.upload(io.BytesIO(raw), length, "bad.zip")
    assert list(store.root.iterdir()) == []


def test_zip_limits_accept_exact_boundary_and_reject_next_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = sources.SourceStore(tmp_path)
    monkeypatch.setattr(sources, "MAX_EXTRACTED", 128)
    monkeypatch.setattr(sources, "MAX_ENTRIES", 1)
    raw = archive_bytes({"single.py": "x" * 128})
    assert store.upload(io.BytesIO(raw), len(raw), "exact.zip")["size"] == 128
    with pytest.raises(JobError, match="100 MiB"):
        store.upload(io.BytesIO(), sources.MAX_ARCHIVE + 1, "too-large.zip")


def test_source_worker_mounts_only_working_copy_and_checks_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = sources.SourceStore(tmp_path)
    raw = archive_bytes({"download/src/app.py": "ORIGINAL", "download/README": "project"})
    upload = store.upload(io.BytesIO(raw), len(raw), "code.zip")
    name = "local-" + "d" * 32
    metadata = store.snapshot(upload["upload_id"], name)
    run = tmp_path / "natsuhat_runs" / name
    run.mkdir(parents=True)
    (run / "job.json").write_text(
        json.dumps(
            {
                "id": name,
                "target": "code.zip",
                "runner": "local",
                "scan_mode": "quick",
                "max_budget_usd": 3,
                "source": metadata,
            }
        )
    )
    working_paths: list[Path] = []

    def engine(**_kwargs: Any) -> None:
        if job_runner.sys.argv[2].startswith("--resume="):
            args = parse_arguments()
            assert args.resume == name
            working = Path(args.targets_info[0]["details"]["target_path"])
        else:
            working = Path(job_runner.sys.argv[2].removeprefix("--target="))
        working_paths.append(working)
        assert (working / "src/app.py").read_text() == "ORIGINAL"
        assert not store.root.is_relative_to(working)
        assert build_bind_mounts([{"source_path": str(working), "workspace_subdir": "app"}])
        (working / "src/app.py").write_text("MODIFIED")
        (run / "run.json").write_text(
            json.dumps(
                {
                    "status": "stopped",
                    "targets_info": [
                        {
                            "type": "local_code",
                            "original": str(working),
                            "details": {"target_path": str(working), "workspace_subdir": "code"},
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(job_runner, "main", engine)
    monkeypatch.setattr(job_runner.sys, "argv", ["worker"])
    job_runner.run_job(run)
    job_runner.run_job(run)
    assert len(working_paths) == 2
    assert all(not path.exists() for path in working_paths)
    assert json.loads((run / "run.json").read_text())["source"] == metadata
    (run / ".state").mkdir()
    (run / ".state/agents.json").write_text('{"parent_of":{"root":null}}')
    monkeypatch.chdir(tmp_path)
    job_runner.run_job(run, resume=True)
    assert len(working_paths) == 3 and not working_paths[-1].exists()
    assert json.loads((run / "run.json").read_text())["source"] == metadata
    (store.directory(name) / "source.zip").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        job_runner.run_job(run, resume=True)
    assert len(working_paths) == 3


@pytest.fixture
def github_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[github.GithubConnection, dict[str, Any]]:
    key = tmp_path / "app.pem"
    key.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for name, value in {
        "APP_ID": "42",
        "CLIENT_ID": "client-test",
        "CLIENT_SECRET": "server-secret",
        "PRIVATE_KEY_PATH": str(key),
        "CALLBACK_URL": "https://dashboard.example.test/api/github/callback",
    }.items():
        monkeypatch.setenv("NATSUHAT_GITHUB_" + name, value)
    state: dict[str, Any] = {
        "revoked": False,
        "unsafe": False,
        "user_installation": 71,
        "requests": [],
    }
    raw = archive_bytes({"repo/src/app.py": "pinned commit", "repo/README": "fixture"})
    repo = {"id": 83, "full_name": "owner/private-repo", "default_branch": "main", "private": True}
    state["repository"] = repo

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - synthetic API endpoints
        state["requests"].append(request)
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"slug": "test-scanner"})
        if path.endswith("/access_tokens"):
            if state["revoked"]:
                return httpx.Response(403, json={"message": "server-secret must not leak"})
            return httpx.Response(201, json={"token": "installation-secret"})
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "user-secret"})
        if path == "/user/installations":
            return httpx.Response(
                200, json={"installations": [{"id": state["user_installation"], "app_id": 42}]}
            )
        if path == "/user/installations/71/repositories":
            return httpx.Response(200, json={"repositories": [repo]})
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [repo, {**repo, "id": 99, "full_name": "owner/not-authorized"}]
                },
            )
        if path == "/applications/client-test/token" and request.method == "DELETE":
            return httpx.Response(204)
        if "/commits/" in path:
            if state.get("empty"):
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"sha": "0123456789" * 4})
        if "/zipball/" in path:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://evil.example.test/secret"
                    if state["unsafe"]
                    else "https://codeload.github.com/owner/private-repo/zip/commit"
                },
            )
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=raw)
        raise AssertionError(f"Unexpected GitHub request: {request.method} {path}")

    client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        github.httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs)
    )
    return github.GithubConnection(tmp_path, sources.SourceStore(tmp_path)), state


def connect_github(connection: github.GithubConnection) -> None:
    installation = parse_qs(urlsplit(connection.connect("session-one")).query)["state"]
    oauth = connection.callback({"state": installation, "installation_id": ["71"]}, "session-one")
    oauth_state = parse_qs(urlsplit(oauth).query)["state"]
    assert (
        connection.callback({"state": oauth_state, "code": ["one-time-code"]}, "session-one")
        == "https://dashboard.example.test/#page=integrations"
    )


def test_github_connection_pins_commit_limits_access_and_keeps_credentials_host_only(
    github_app: tuple[github.GithubConnection, dict[str, Any]],
) -> None:
    connection, state = github_app
    connect_github(connection)
    assert [r["id"] for r in connection.public(refresh=True)["available"]] == [83]
    with pytest.raises(JobError, match="not available"):
        connection.update("add", {"id": 99}, "")
    connection.update("add", {"id": 83}, "")
    upload = connection.download(83, "feature/auth")
    metadata = json.loads((connection.sources.directory(upload) / "source.json").read_text())
    assert metadata["repository"] == "owner/private-repo"
    assert metadata["ref"] == "feature/auth"
    assert metadata["commit"] == "0123456789" * 4
    assert metadata["files"] == 2
    assert not any(
        secret in connection.path.read_text()
        for secret in ("server-secret", "installation-secret", "user-secret")
    )
    requests = state["requests"]
    assert any(b"feature%2Fauth" in r.url.raw_path for r in requests)
    assert all(
        "authorization" not in r.headers for r in requests if r.url.host == "codeload.github.com"
    )
    scoped = [json.loads(r.content) for r in requests if r.url.path.endswith("/access_tokens")]
    assert all(body["permissions"] == {"contents": "read", "metadata": "read"} for body in scoped)
    assert any(body.get("repository_ids") == [83] for body in scoped)
    state["repository"].update(full_name="owner/renamed-repo", default_branch="develop")
    refreshed = connection.public(refresh=True)["repositories"][0]
    assert (refreshed["full_name"], refreshed["default_branch"]) == (
        "owner/renamed-repo",
        "develop",
    )
    renamed = connection.download(83, "develop")
    assert (
        json.loads((connection.sources.directory(renamed) / "source.json").read_text())[
            "repository"
        ]
        == "owner/renamed-repo"
    )
    state["unsafe"] = True
    with pytest.raises(JobError, match="unsafe archive redirect"):
        connection.download(83, "main")
    state["revoked"] = True
    with pytest.raises(JobError, match="GitHub request failed") as error:
        connection.download(83, "main")
    assert "server-secret" not in str(error.value)
    connection.update("disconnect", {}, "")
    with pytest.raises(JobError, match="Connect this GitHub repository"):
        connection.download(83, "main")


def test_github_callback_rejects_other_sessions_replays_and_unowned_installations(
    github_app: tuple[github.GithubConnection, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, state = github_app
    value = parse_qs(urlsplit(connection.connect("session-one")).query)["state"]
    with pytest.raises(JobError, match="another session"):
        connection.callback({"state": value, "installation_id": ["71"]}, "session-two")
    with pytest.raises(JobError, match="expired"):
        connection.callback({"state": value, "installation_id": ["71"]}, "session-one")
    state["user_installation"] = 999
    with pytest.raises(JobError, match="cannot access"):
        connect_github(connection)
    assert connection.public()["connected"] is False
    for callback in (
        "http://dashboard.example.test/api/github/callback",
        "https://user:password@dashboard.example.test/api/github/callback",
    ):
        monkeypatch.setenv("NATSUHAT_GITHUB_CALLBACK_URL", callback)
        with pytest.raises(JobError, match="callback URL"):
            connection.connect("session-one")


@pytest.mark.usefixtures("github_app")
def test_github_http_callback_uses_lax_flow_without_weakening_dashboard_session(
    tmp_path: Path,
) -> None:
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie_name = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}"
    connection = HTTPConnection(urlsplit(url).netloc)

    def call(
        path: str, *, cookie: str = "", body: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection.request(
            "POST" if body is not None else "GET",
            path,
            body=body,
            headers={"Cookie": cookie, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    try:
        assert call("/api/github")[0] == 403
        assert call("/api/github/callback?state=forged")[0] == 403
        bootstrap = call("/?" + urlencode({"token": token}))
        assert "SameSite=Strict" in bootstrap[1]["Set-Cookie"]
        begin = call("/api/github/connect", cookie=f"{cookie_name}={token}", body="{}")
        assert begin[0] == 200
        assert "SameSite=Lax" in begin[1]["Set-Cookie"]
        flow = begin[1]["Set-Cookie"].split(";", 1)[0]
        state = parse_qs(urlsplit(json.loads(begin[2])["url"]).query)["state"][0]
        installation = call(
            "/api/github/callback?" + urlencode({"state": state, "installation_id": 71}),
            cookie=flow,
        )
        assert installation[0] == 303
        oauth_state = parse_qs(urlsplit(installation[1]["Location"]).query)["state"][0]
        callback = "/api/github/callback?" + urlencode({"state": oauth_state, "code": "test-code"})
        success = call(callback, cookie=flow)
        assert success[0] == 303
        assert success[1]["Location"] == "https://dashboard.example.test/#page=integrations"
        assert call(callback, cookie=flow)[0] == 403
        connected = call("/api/github", cookie=f"{cookie_name}={token}")
        assert json.loads(connected[2])["connected"] is True
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()


def test_github_source_check_rejects_empty_and_unconnected_repositories_without_scanning(
    github_app: tuple[github.GithubConnection, dict[str, Any]], tmp_path: Path
) -> None:
    github_connection, state = github_app
    connect_github(github_connection)
    github_connection.update("add", {"id": 83}, "")
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    connection = HTTPConnection(urlsplit(url).netloc)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"

    def check(identifier: int, *, authorized: bool = True) -> tuple[int, dict[str, Any]]:
        connection.request(
            "POST",
            "/api/github/check",
            json.dumps({"repository_id": identifier, "ref": " feature/auth "}),
            headers={"Cookie": cookie if authorized else "", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        assert check(83, authorized=False)[0] == 403
        assert check(99)[0] == 403
        state["empty"] = True
        status, result = check(83)
        assert status == 409
        assert "repository is empty" in result["error"]
        assert "check again" in result["error"]
        # A push can make the same selection ready without reconnecting.
        state["empty"] = False
        status, result = check(83)
        assert status == 200
        assert result == {"repository_id": 83, "ref": "feature/auth", "commit": "0123456789" * 4}
        assert "secret" not in json.dumps(result)
        assert not list(tmp_path.rglob("source.json"))
        assert not list((tmp_path / "natsuhat_runs").glob("*/job.json"))
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
