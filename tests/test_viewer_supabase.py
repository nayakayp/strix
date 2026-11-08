"""Hosted HTTP contracts against disposable PostgreSQL and synthetic Auth/S3.

Set NATSUHAT_TEST_DATABASE_URL to a disposable loopback database named natsuhat_test*.
Never uses DATABASE_URL, Supabase credentials, paid workers, or a hosted project.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import jwt
import psycopg
import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.conninfo import conninfo_to_dict

from natsuhat.interface.viewer import jobs, runner_settings
from natsuhat.interface.viewer import server as viewer_server
from natsuhat.interface.viewer.hosted import HostedDashboard
from natsuhat.interface.viewer.hosted_workspace import HostedWorkspace
from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.server import serve
from natsuhat.interface.viewer.supabase_store import HostedSettings, SupabaseStore


if TYPE_CHECKING:
    from collections.abc import Iterator


USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
OWNER = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
FOREIGN = "55555555-5555-4555-8555-555555555555"
FACTOR = "66666666-6666-4666-8666-666666666666"
SCAN = "e2b-" + "a" * 32
EMAILS = {USER: "viewer@example.test", OTHER: "other@example.test", OWNER: "owner@example.test"}


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    url = os.environ.get("NATSUHAT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set NATSUHAT_TEST_DATABASE_URL for disposable PostgreSQL integration tests")
    info = conninfo_to_dict(url)
    if info.get("host") not in {"localhost", "127.0.0.1"} or not info.get("dbname", "").startswith(
        "natsuhat_test"
    ):
        pytest.fail("Refusing to modify a non-disposable or non-loopback database")
    with psycopg.connect(url, autocommit=True) as db:
        for role in ("anon", "authenticated", "natsuhat_test_runtime"):
            if not db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                db.execute(
                    psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(psycopg.sql.Identifier(role))
                )
        db.execute("DROP SCHEMA IF EXISTS natsuhat CASCADE")
        db.execute("DROP SCHEMA IF EXISTS auth CASCADE")
        db.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        db.execute("CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY)")
        db.execute(
            "CREATE SCHEMA storage; "
            "CREATE TABLE storage.buckets(id text PRIMARY KEY, name text, public boolean)"
        )
        db.execute(Path("supabase/migrations/202611050001_hosted_dashboard.sql").read_text())
        db.execute("GRANT natsuhat_api,natsuhat_worker TO natsuhat_test_runtime")
        db.execute("GRANT natsuhat_test_runtime TO CURRENT_USER")
    yield url


class Services:
    """Only external boundaries are synthetic; JWT validation and SQL/RLS are real."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.objects: dict[str, bytes] = {}
        self.fail_storage = False
        self.tokens: dict[str, tuple[str, str]] = {}
        self.factors: dict[str, list[dict[str, str]]] = {}
        self.confirmations = {"synthetic-recovery": USER, "synthetic-invite": OTHER}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def issue(self, user: str, aal: str = "aal1", *, expired: bool = False) -> dict[str, str]:
        token = jwt.encode(
            {
                "sub": user,
                "email": EMAILS[user],
                "role": "authenticated",
                "aal": aal,
                "aud": "authenticated",
                "iss": self.url + "/auth/v1",
                "exp": int(time.time()) + (-60 if expired else 3600),
            },
            self.key,
            algorithm="ES256",
            headers={"kid": "test-key"},
        )
        refresh = str(uuid4())
        self.tokens[refresh] = (user, aal)
        return {"access_token": token, "refresh_token": refresh}

    def handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def reply(self, status: int, value: Any) -> None:
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.endswith("/.well-known/jwks.json"):
                    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(service.key.public_key()))
                    self.reply(200, {"keys": [{**public, "kid": "test-key", "alg": "ES256"}]})
                elif self.path == "/auth/v1/user":
                    claims = jwt.decode(
                        self.headers["Authorization"][7:], options={"verify_signature": False}
                    )
                    self.reply(
                        200,
                        {
                            "id": claims["sub"],
                            "email": claims["email"],
                            "email_confirmed_at": "2026-01-01",
                            "factors": service.factors.get(claims["sub"], []),
                        },
                    )
                else:
                    self.object_response()

            def do_HEAD(self) -> None:
                self.object_response(head=True)

            def object_response(self, *, head: bool = False) -> None:
                key = urlsplit(self.path).path
                if key not in service.objects:
                    self.reply(404, {})
                    return
                data = service.objects[key]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("ETag", '"synthetic"')
                self.end_headers()
                if not head:
                    self.wfile.write(data)

            def do_PUT(self) -> None:
                if self.path == "/auth/v1/user":
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.reply(200, {})
                    return
                if service.fail_storage:
                    self.reply(403, {})
                    return
                service.objects[urlsplit(self.path).path] = self.rfile.read(
                    int(self.headers["Content-Length"])
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.send_header("ETag", '"synthetic"')
                self.end_headers()

            def do_DELETE(self) -> None:
                if service.fail_storage:
                    self.reply(403, {})
                    return
                service.objects.pop(urlsplit(self.path).path, None)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.startswith("/auth/v1/token?grant_type=refresh_token"):
                    self.reply(200, service.issue(*service.tokens[body["refresh_token"]]))
                elif self.path.startswith("/auth/v1/token"):
                    user = next((u for u, email in EMAILS.items() if email == body["email"]), None)
                    if user and body["password"] == "synthetic-password":  # noqa: S105 - synthetic fixture
                        self.reply(200, service.issue(user))
                    else:
                        self.reply(400, {})
                elif self.path.endswith("/challenge"):
                    self.reply(200, {"id": "challenge"})
                elif self.path.endswith("/verify") and "/factors/" in self.path:
                    claims = jwt.decode(
                        self.headers["Authorization"][7:], options={"verify_signature": False}
                    )
                    if body["code"] == "123456":
                        service.factors[claims["sub"]] = [
                            {
                                "id": FACTOR,
                                "status": "verified",
                                "factor_type": "totp",
                                "friendly_name": "Natsuhat",
                            }
                        ]
                    self.reply(
                        200 if body["code"] == "123456" else 400,
                        service.issue(claims["sub"], "aal2") if body["code"] == "123456" else {},
                    )
                elif self.path == "/auth/v1/verify":
                    user = service.confirmations.pop(body.get("token_hash"), None)
                    self.reply(200 if user else 400, service.issue(user) if user else {})
                elif self.path == "/auth/v1/factors":
                    claims = jwt.decode(
                        self.headers["Authorization"][7:], options={"verify_signature": False}
                    )
                    service.factors[claims["sub"]] = [
                        {
                            "id": FACTOR,
                            "status": "unverified",
                            "factor_type": "totp",
                            "friendly_name": "Natsuhat",
                        }
                    ]
                    self.reply(
                        200,
                        {
                            "id": FACTOR,
                            "totp": {
                                "secret": "SYNTHETIC-DEMO-ONLY",
                                "qr_code": '<svg xmlns="http://www.w3.org/2000/svg" '
                                'width="100" height="100"><text y="50">Demo QR</text></svg>',
                            },
                        },
                    )
                elif self.path == "/auth/v1/invite":
                    self.reply(422, {"code": 422, "error_code": "email_exists"})
                elif self.path in {"/auth/v1/recover", "/auth/v1/logout?scope=local"}:
                    self.reply(200, {})
                else:
                    self.reply(404, {})

        return Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def hosted(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SupabaseStore, Services]]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with psycopg.connect(database) as db:
        db.execute("TRUNCATE natsuhat.workspaces,auth.users,natsuhat.storage_objects CASCADE")
        for user in EMAILS:
            db.execute("INSERT INTO auth.users VALUES(%s)", (user,))
        for wid, name in ((WORKSPACE, "Research team"), (FOREIGN, "Other workspace")):
            db.execute("INSERT INTO natsuhat.workspaces(id,name) VALUES(%s,%s)", (wid, name))
        for wid, user, role in (
            (WORKSPACE, USER, "viewer"),
            (WORKSPACE, OWNER, "owner"),
            (FOREIGN, OTHER, "operator"),
        ):
            db.execute("INSERT INTO natsuhat.workspace_members VALUES(%s,%s,%s)", (wid, user, role))
    service = Services()
    settings = HostedSettings(
        service.url,
        "synthetic-publishable",
        "synthetic-secret",
        database,
        service.url,
        "local",
        "synthetic-access",
        "synthetic-s3-secret",
        "synthetic-encryption-key-not-for-production",
        "http://127.0.0.1",
    )
    monkeypatch.setattr(HostedSettings, "from_env", classmethod(lambda _cls: settings))
    store = SupabaseStore(settings)
    # Exercise a non-owner role even though the disposable migration login owns tables.
    original = psycopg.connect

    def runtime(*args: Any, **kwargs: Any) -> Any:
        connection = original(*args, **kwargs)
        connection.execute("SET SESSION ROLE natsuhat_test_runtime")
        if not connection.autocommit:
            connection.commit()
        return connection

    monkeypatch.setattr(psycopg, "connect", runtime)
    yield store, service
    store.close()
    service.close()


@pytest.fixture
def dashboard(
    hosted: tuple[SupabaseStore, Services], tmp_path: Path
) -> Iterator[tuple[httpx.Client, Any]]:
    store, _service = hosted
    server, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", port=0, open_browser=False, hosted=True
    )
    assert token == ""
    store.settings.public_url = url
    with httpx.Client(base_url=url, headers={"Origin": url}) as client:
        yield client, server.hosted
    server.shutdown()
    server.server_close()


def login(client: httpx.Client, user: str) -> None:
    response = client.post(
        "/api/auth/login", json={"email": EMAILS[user], "password": "synthetic-password"}
    )
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Lax" in response.headers["set-cookie"]
    client.headers["X-Natsuhat-Workspace"] = client.get("/api/auth/session").json()["workspace"]


def mfa(client: httpx.Client) -> None:
    response = client.post("/api/auth/mfa/verify", json={"factor_id": FACTOR, "code": "123456"})
    assert response.status_code == 200, response.text


def test_reused_transactions_preserve_workspace_isolation(
    hosted: tuple[SupabaseStore, Services], database: str
) -> None:
    store, _service = hosted
    store.pool.resize(1, 1)
    for user, expected in ((USER, [WORKSPACE]), (OTHER, [FOREIGN]), (None, [WORKSPACE, FOREIGN])):
        with store.transaction(user) as db:
            rows = db.execute("SELECT id::text FROM natsuhat.workspaces ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == expected

    with pytest.raises(psycopg.errors.DivisionByZero), store.transaction(USER) as db:
        db.execute("SELECT 1/0")
    assert [row["id"] for row in store.workspaces(OTHER)] == [FOREIGN]

    # An idle socket can disappear when the database or its proxy restarts.
    with store.transaction() as db:
        pid = db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    with psycopg.Connection.connect(database, autocommit=True) as db:
        db.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert [row["id"] for row in store.workspaces(USER)] == [WORKSPACE]


def test_http_auth_workspace_isolation_and_revocation(
    dashboard: tuple[httpx.Client, HostedDashboard],
) -> None:
    client, app = dashboard
    assert client.get("/api/auth/session").json() == {"hosted": True, "user": None}
    assert client.get("/api/runs?token=local-viewer-token").status_code == 401
    login(client, USER)
    app.store.save_job(
        WORKSPACE, {"id": SCAN, "status": "completed", "target": "authorized.example.test"}, {}, ""
    )
    app.store.save_job(FOREIGN, {"id": "foreign", "status": "completed"}, {}, "")
    assert [r["name"] for r in client.get("/api/runs").json()["runs"]] == [SCAN]
    assert client.get("/api/run?run=foreign").status_code == 404
    assert client.post("/api/auth/workspace", json={"workspace": FOREIGN}).status_code == 403
    assert client.post("/api/jobs", json={}).status_code == 403
    assert (
        client.post(
            "/api/auth/invite", json={"email": "x@example.test", "role": "admin"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/logout", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/auth/logout", json={}, headers={"Origin": ""}).status_code == 403
    with app.store.transaction() as db:
        row = db.execute("SELECT encrypted_tokens FROM natsuhat.app_sessions").fetchone()
        assert row and "access_token" not in row["encrypted_tokens"]
    old = dict(client.cookies)
    assert client.post("/api/auth/logout", json={}).status_code == 200
    client.cookies.update(old)
    assert client.get("/api/runs").status_code == 401


def test_compact_catalog_and_selected_run_sections(
    dashboard: tuple[httpx.Client, HostedDashboard], tmp_path: Path
) -> None:
    client, app = dashboard
    login(client, USER)
    summary = {
        "run_id": SCAN,
        "targets_info": [{"original": "https://authorized.example.test"}],
        "scan_mode": "deep",
        "status": "completed",
        "finished": True,
        "start_time": "2026-01-01T10:00:00Z",
        "end_time": None,
        "scan_results": {"executive_summary": "summary detail " * 10000},
    }
    findings = [
        {"id": "high", "title": "High", "severity": " HIGH ", "description": "a" * 10000},
        {"id": "info", "title": "Informational", "severity": "info"},
        {"id": "missing", "title": "No severity"},
    ]
    payload = {
        "summary": summary,
        "vulnerabilities": findings,
        "transcript": {
            "agents": [{"id": "root", "name": "Coordinator"}],
            "events": [
                {
                    "type": "tool",
                    "data": {
                        "mcp_connection": "active",
                        "status": "running",
                        "args": {"text": "argument " * 10000},
                    },
                },
                {
                    "type": "tool",
                    "data": {
                        "mcp_connection": "idle",
                        "status": "completed",
                        "result": "result " * 10000,
                    },
                },
                {"type": "chat", "data": {"mcp_connection": "not-a-tool", "status": "running"}},
            ],
        },
        "report": "# Full report\n" + "report detail " * 10000,
    }
    archive = tmp_path / "results.tar.gz"
    archive.write_bytes(b"synthetic-archive")
    app.store.save_job(WORKSPACE, {"id": SCAN, "status": "completed"}, {}, "")
    app.store.publish(WORKSPACE, SCAN, "00000000000000000001", payload, archive)
    app.store.save_job(WORKSPACE, {"id": "no-snapshot", "status": "failed"}, {}, "")
    app.store.save_job(FOREIGN, {"id": "foreign", "status": "completed"}, {}, "")

    # The catalog must stay small even when each snapshot contains large details.
    catalog = app.store.scans(USER, WORKSPACE)
    assert len(json.dumps(catalog, default=str)) < 4000
    metadata = app.store.scans(USER, WORKSPACE, include_summary=False)
    assert {row["id"]: row["payload"] for row in metadata} == {SCAN: {}, "no-snapshot": None}
    runs = client.get("/api/runs").json()["runs"]
    assert [run["name"] for run in runs] == ["no-snapshot", SCAN]
    assert runs[1] == {
        "name": SCAN,
        "target": "https://authorized.example.test",
        "scan_mode": "deep",
        "status": "completed",
        "start_time": "2026-01-01T10:00:00Z",
        "end_time": None,
        "finished": True,
        "severity_counts": {"critical": 0, "high": 1, "medium": 0, "low": 2},
    }
    jobs_by_id = {job["id"]: job for job in client.get("/api/jobs").json()["jobs"]}
    assert jobs_by_id[SCAN]["has_run"] is True
    assert jobs_by_id["no-snapshot"]["has_run"] is False
    for endpoint, section in (
        ("run", "summary"),
        ("vulnerabilities", "vulnerabilities"),
        ("report", "report"),
        ("transcript", "transcript"),
    ):
        selected = app.store.scan(USER, WORKSPACE, SCAN, section=section)
        assert selected["payload"] == payload[section]
        expected = {"markdown": payload[section]} if section == "report" else payload[section]
        assert client.get(f"/api/{endpoint}?run={SCAN}").json() == expected
        assert client.get(f"/api/{endpoint}?run=foreign").status_code == 404
    activity = {"agents": payload["transcript"]["agents"], "mcp_in_use": ["active"]}
    assert client.get(f"/api/transcript?run={SCAN}&events=0").json() == activity
    compact = app.store.scan(USER, WORKSPACE, SCAN, section="transcript", include_events=False)
    assert compact["payload"] == activity
    assert len(json.dumps(compact, default=str)) < 2000
    assert client.get("/api/transcript?run=foreign&events=0").status_code == 404
    assert client.get("/api/run").json()["run_id"] == "no-snapshot"
    assert client.get("/api/report").json() == {"markdown": ""}
    assert client.get("/api/vulnerabilities").json() == []
    assert client.get("/api/transcript").json() == {"agents": [], "events": []}
    assert client.get("/api/transcript?events=0").json() == {"agents": [], "mcp_in_use": []}
    # Checkpoint restoration still needs the full archive metadata and payload.
    restored = app.store.scan(USER, WORKSPACE, SCAN)
    assert restored["payload"] == payload
    assert restored["object_key"]
    assert restored["sha256"]


def test_admin_mfa_and_existing_user_invitation(
    dashboard: tuple[httpx.Client, HostedDashboard],
) -> None:
    client, app = dashboard
    login(client, OWNER)
    assert client.get("/api/runs").status_code == 403
    assert client.get("/api/auth/members").status_code == 403
    assert (
        client.post(
            "/api/auth/mfa/verify", json={"factor_id": FACTOR, "code": "000000"}
        ).status_code
        == 401
    )
    mfa(client)
    assert client.get("/api/auth/session").json()["aal"] == "aal2"
    assert client.get("/api/auth/members").status_code == 200
    assert (
        client.post(
            "/api/auth/invite", json={"email": EMAILS[OTHER], "role": "operator"}
        ).status_code
        == 200
    )
    login(client, OTHER)
    session = client.get("/api/auth/session").json()
    assert {w["id"] for w in session["workspaces"]} == {WORKSPACE, FOREIGN}
    assert client.post("/api/auth/workspace", json={"workspace": WORKSPACE}).status_code == 200
    client.headers["X-Natsuhat-Workspace"] = WORKSPACE
    assert client.post("/api/jobs", json={}).status_code == 403
    mfa(client)
    assert client.get("/api/llm/settings").status_code == 200
    assert client.post("/api/llm/providers", json={}).status_code == 403
    with app.store.transaction() as db:
        db.execute("DELETE FROM natsuhat.workspace_members WHERE user_id=%s", (OTHER,))
    assert client.get("/api/runs").status_code == 403


def test_postgres_rls_and_publication_retries(
    hosted: tuple[SupabaseStore, Services], tmp_path: Path
) -> None:
    store, service = hosted
    archive = tmp_path / "results.tar.gz"
    archive.write_bytes(b"synthetic-archive-v1")
    store.save_job(WORKSPACE, {"id": SCAN, "status": "completed"}, {}, "")
    first = {
        "vulnerabilities": [
            {"id": "withdrawn", "title": "Withdraw me", "severity": "high"},
            {"id": "keep", "title": "Keep me", "severity": "low"},
        ]
    }
    store.publish(WORKSPACE, SCAN, "00000000000000000002", first, archive)
    with store.transaction(USER) as db:
        assert len(db.execute("SELECT * FROM natsuhat.scans").fetchall()) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("DELETE FROM natsuhat.scans")
    with store.transaction(OTHER) as db:
        assert db.execute("SELECT * FROM natsuhat.scans").fetchall() == []
    with store.transaction() as db:
        db.execute(
            "UPDATE natsuhat.findings SET triage='{\"status\":\"accepted\"}' WHERE id='keep'"
        )
    second = {"vulnerabilities": [{"id": "keep", "title": "Updated", "severity": "medium"}]}
    service.fail_storage = True
    with pytest.raises(S3UploadFailedError):
        store.publish(WORKSPACE, SCAN, "00000000000000000003", second, archive)
    assert store.scan(USER, WORKSPACE, SCAN)["payload"] == first
    service.fail_storage = False
    store.publish(WORKSPACE, SCAN, "00000000000000000003", second, archive)
    store.publish(WORKSPACE, SCAN, "00000000000000000001", first, archive)
    store.publish(WORKSPACE, SCAN, "00000000000000000003", second, archive)
    assert store.scan(USER, WORKSPACE, SCAN)["payload"] == second
    with store.transaction() as db:
        findings = db.execute("SELECT id,title,triage FROM natsuhat.findings").fetchall()
        assert findings == [{"id": "keep", "title": "Updated", "triage": {"status": "accepted"}}]
    store.delete_scan(WORKSPACE, SCAN)
    service.fail_storage = True
    with pytest.raises(ClientError):
        store.collect_objects()
    assert store.scans(USER, WORKSPACE) == []
    service.fail_storage = False
    store.collect_objects()
    assert service.objects == {}
    with pytest.raises(JobError, match="deleted"):
        store.save_job(WORKSPACE, {"id": SCAN}, {}, "")
    store.publish(WORKSPACE, SCAN, "00000000000000000004", first, archive)
    assert store.scans(USER, WORKSPACE) == []


def test_private_sources_restore_expiry_and_checksums(
    hosted: tuple[SupabaseStore, Services], tmp_path: Path
) -> None:
    store, service = hosted
    workspace = HostedWorkspace(tmp_path / "one", FOREIGN, store)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("project/app.py", "print('synthetic')")
    raw = content.getvalue()
    metadata = workspace.sources.upload(io.BytesIO(raw), len(raw), "source.zip")
    identifier = metadata["upload_id"]
    archive = workspace.sources.directory(identifier) / "source.zip"
    store.save_source(FOREIGN, identifier, metadata, archive)
    assert store.source(USER, FOREIGN, identifier) is None
    restored = HostedWorkspace(tmp_path / "two", FOREIGN, store)
    restored.restore_source(OTHER, identifier)
    assert (
        restored.sources.directory(identifier) / "source.zip"
    ).read_bytes() == archive.read_bytes()
    with store.transaction() as db:
        db.execute("UPDATE natsuhat.source_snapshots SET expires_at=now()-interval '1 second'")
    with pytest.raises(JobError, match="expired"):
        restored.restore_source(OTHER, identifier)
    store.collect_objects()
    assert service.objects == {}
    key = f"{FOREIGN}/corrupt"
    service.objects["/scan-sources/" + key] = b"wrong"
    with pytest.raises(JobError, match="checksum"):
        store.get("scan-sources", key, tmp_path / "bad.zip", hashlib.sha256(b"right").hexdigest())
    assert not (tmp_path / "bad.zip").exists()
    restored.close()
    workspace.close()


def test_session_refresh_and_signature_validation(
    dashboard: tuple[httpx.Client, HostedDashboard],
    hosted: tuple[SupabaseStore, Services],
    tmp_path: Path,
) -> None:
    client, app = dashboard
    store, service = hosted
    login(client, USER)
    expired = service.issue(USER, expired=True)
    with store.transaction() as db:
        db.execute(
            "UPDATE natsuhat.app_sessions SET encrypted_tokens=%s", (app.auth.encrypt(expired),)
        )
    assert client.get("/api/runs").status_code == 200
    forged = jwt.encode(
        {"sub": OWNER, "exp": int(time.time()) + 3600},
        "synthetic-wrong-signing-key-long-enough",
        algorithm="HS256",
    )
    with pytest.raises(JobError, match="Invalid"):
        app.auth.claims(forged)
    with pytest.raises(JobError, match="Another hosted controller"):
        HostedDashboard(tmp_path / "second-controller", store.settings)


def test_recovery_mfa_password_and_throttling(
    dashboard: tuple[httpx.Client, HostedDashboard], hosted: tuple[SupabaseStore, Services]
) -> None:
    client, _app = dashboard
    _store, service = hosted
    known = client.post("/api/auth/recover", json={"email": EMAILS[USER]})
    unknown = client.post("/api/auth/recover", json={"email": "missing@example.test"})
    assert known.json() == unknown.json() and known.status_code == unknown.status_code == 200
    login(client, USER)
    old = dict(client.cookies)
    service.factors[USER] = [{"id": FACTOR, "status": "verified", "factor_type": "totp"}]
    assert (
        client.post(
            "/api/auth/verify", json={"type": "recovery", "token_hash": "synthetic-recovery"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/verify", json={"type": "recovery", "token_hash": "synthetic-recovery"}
        ).status_code
        == 401
    )
    assert (
        client.post("/api/auth/password", json={"password": "new-synthetic-password"}).status_code
        == 403
    )
    mfa(client)
    assert (
        client.post("/api/auth/password", json={"password": "new-synthetic-password"}).status_code
        == 200
    )
    current = dict(client.cookies)
    client.cookies.update(old)
    assert client.get("/api/runs").status_code == 401
    client.cookies.update(current)
    assert client.post("/api/auth/mfa/remove", json={"factor_id": FACTOR}).status_code == 200
    client.cookies.update(current)
    assert client.get("/api/runs").status_code == 401
    statuses = [
        client.post(
            "/api/auth/login", json={"email": EMAILS[USER], "password": "wrong"}
        ).status_code
        for _ in range(11)
    ]
    assert statuses[-1] == 429


def test_scan_persists_before_worker_and_recovers_results(
    dashboard: tuple[httpx.Client, HostedDashboard],
    hosted: tuple[SupabaseStore, Services],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = dashboard
    store, service = hosted
    monkeypatch.setenv("E2B_API_KEY", "synthetic-provider")
    monkeypatch.setattr(
        runner_settings,
        "image_metadata",
        lambda _provider, name: {
            "id": "synthetic",
            "names": [name],
            "available": True,
            "revision": "b" * 64,
            "resolved_template": "synthetic:rev-" + "b" * 64,
        },
    )
    popen = subprocess.Popen
    script = """
import json, os, sys
from pathlib import Path
assert not any(k.startswith('SUPABASE_') or k in {
    'DATABASE_URL', 'NATSUHAT_SESSION_ENCRYPTION_KEY'} for k in os.environ)
directory = Path(sys.argv[1])
(directory / 'runner.json').write_text(json.dumps({'cleanup': 'paused', 'phase': 'Finished'}))
(directory / 'run.json').write_text(json.dumps({
    'run_name': directory.name, 'status': 'completed', 'end_time': '2026-01-01T12:00:00Z'}))
(directory / 'penetration_test_report.md').write_text('# Synthetic completed scan')
"""

    def launch(command: list[str], **kwargs: Any) -> Any:
        # The catalog must already contain the source before any process is spawned.
        records = store.scans(OTHER, FOREIGN)
        assert records and store.source(OTHER, FOREIGN, records[0]["id"])
        return popen([sys.executable, "-c", script, command[-1]], **kwargs)

    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    login(client, OTHER)
    mfa(client)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        bundle.writestr("app.py", "print('synthetic')")
    upload = client.post(
        "/api/sources/upload",
        content=content.getvalue(),
        headers={"Content-Type": "application/zip"},
    )
    assert upload.status_code == 201, upload.text
    body = {
        "source": {"kind": "zip", "upload_id": upload.json()["upload_id"]},
        "authorized": True,
        "runner": "e2b",
        "max_budget_usd": 1,
    }
    service.fail_storage = True
    assert client.post("/api/jobs", json=body).status_code == 503
    assert store.scans(OTHER, FOREIGN) == []
    service.fail_storage = False
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202, response.text
    name = response.json()["job"]["id"]
    workspace = app.workspace(FOREIGN)
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline
        and workspace.manager.list()[0]["status"] in jobs.ACTIVE_STATUSES
    ):
        time.sleep(0.02)
    assert workspace.manager.list()[0]["status"] == "completed"
    workspace.flush()
    assert client.get(f"/api/report?run={name}").json()["markdown"] == "# Synthetic completed scan"
    assert store.scan(OTHER, FOREIGN, name)["object_key"]
    # A new local controller directory restores artifacts and immutable source.
    recovered = HostedWorkspace(app.root.parent / "recovery", FOREIGN, store)
    try:
        path = recovered.restore(OTHER, name)
        assert (path / "penetration_test_report.md").read_text() == "# Synthetic completed scan"
        assert (recovered.sources.directory(name) / "source.zip").is_file()
    finally:
        recovered.close()
    # Listing on a replacement controller must not hide resume solely because
    # checkpoints have not been downloaded yet. Retention still gates eligibility.
    workspace.close()
    app.workspaces.pop(FOREIGN)
    job = store.scan(OTHER, FOREIGN, name)["job"]
    runner = {"cleanup": "paused", "sandbox_id": "synthetic-retained"}
    for status in ("failed", "interrupted", "stopped"):
        job["status"] = status
        store.save_job(FOREIGN, job, runner, "")
        assert client.get("/api/jobs").json()["jobs"][0]["can_resume"] is True
    store.save_job(FOREIGN, job, {**runner, "cleanup": "archived"}, "")
    assert client.get("/api/jobs").json()["jobs"][0]["can_resume"] is False


def body_request(client: httpx.Client, path: str, content_type: str) -> socket.socket:
    connection = socket.create_connection((client.base_url.host, client.base_url.port), timeout=2)
    headers = {
        "Host": client.base_url.netloc.decode(),
        "Content-Type": content_type,
        "Content-Length": "1024",
        "Origin": str(client.base_url).rstrip("/"),
        "Cookie": "; ".join(f"{key}={value}" for key, value in client.cookies.items()),
        "X-Natsuhat-Workspace": client.headers.get("X-Natsuhat-Workspace", ""),
    }
    request = f"POST {path} HTTP/1.1\r\n"
    request += "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    connection.sendall(request.encode())
    return connection


@pytest.mark.parametrize("upload", [False, True], ids=["json", "zip"])
@pytest.mark.parametrize("trickle", [False, True], ids=["stalled", "trickled"])
def test_request_body_has_total_deadline(
    dashboard: tuple[httpx.Client, HostedDashboard],
    monkeypatch: pytest.MonkeyPatch,
    *,
    upload: bool,
    trickle: bool,
) -> None:
    client, _app = dashboard
    if upload:
        login(client, OTHER)
        mfa(client)
    monkeypatch.setattr(viewer_server, "HOSTED_JSON_TIMEOUT", 0.25)
    monkeypatch.setattr(viewer_server, "HOSTED_UPLOAD_TIMEOUT", 0.25)
    stopped = threading.Event()
    with body_request(
        client,
        "/api/sources/upload" if upload else "/api/auth/login",
        "application/zip" if upload else "application/json",
    ) as connection:

        def send() -> None:
            while not stopped.wait(0.03):
                if trickle:
                    try:
                        connection.sendall(b" ")
                    except OSError:
                        return

        sender = threading.Thread(target=send)
        sender.start()
        try:
            response = HTTPResponse(connection)
            response.begin()
            assert response.status == 408
            assert json.loads(response.read())["error"] == "Request body timed out."
        finally:
            stopped.set()
            sender.join(timeout=2)


def test_slow_upload_does_not_block_another_workspaces_logout(
    dashboard: tuple[httpx.Client, HostedDashboard],
) -> None:
    client, app = dashboard
    login(client, OTHER)
    mfa(client)
    sources = app.workspace(FOREIGN).sources.root
    before = set(sources.iterdir())
    with (
        httpx.Client(
            base_url=client.base_url, headers={"Origin": str(client.base_url).rstrip("/")}
        ) as other,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        login(other, OWNER)
        with body_request(client, "/api/sources/upload", "application/zip") as connection:
            deadline = time.monotonic() + 2
            # Wait until the authenticated upload is actually receiving its body.
            while set(sources.iterdir()) == before and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                assert set(sources.iterdir()) != before
                result = executor.submit(other.post, "/api/auth/logout", json={})
                assert result.result(timeout=1).status_code == 200
            finally:
                connection.shutdown(socket.SHUT_WR)
                response = HTTPResponse(connection)
                response.begin()
                response.read()


def test_stale_tab_cannot_retarget_workspace_operations(
    dashboard: tuple[httpx.Client, HostedDashboard],
) -> None:
    client, app = dashboard
    with app.store.transaction() as db:
        db.execute("INSERT INTO natsuhat.workspace_members VALUES(%s,%s,'admin')", (FOREIGN, OWNER))
    login(client, OWNER)
    mfa(client)
    # The cookie is shared across tabs, but the first tab still displays WORKSPACE.
    assert client.post("/api/auth/workspace", json={"workspace": FOREIGN}).status_code == 200
    assert client.get("/api/runs").status_code == 409
    assert client.post("/api/jobs", json={}).status_code == 409
    assert client.post("/api/llm/providers", json={}).status_code == 409
    assert (
        client.post("/api/auth/invite", json={"email": EMAILS[USER], "role": "viewer"}).status_code
        == 409
    )
    assert client.get("/api/auth/members").status_code == 409
    assert (
        client.post(
            "/api/sources/upload", content=b"not-a-zip", headers={"Content-Type": "application/zip"}
        ).status_code
        == 409
    )
    client.headers["X-Natsuhat-Workspace"] = FOREIGN
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/auth/members").status_code == 200
    del client.headers["X-Natsuhat-Workspace"]
    assert client.post("/api/jobs", json={}).status_code == 409
    with app.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM natsuhat.scans").fetchone()
        assert not db.execute("SELECT 1 FROM natsuhat.source_snapshots").fetchone()
        assert not db.execute("SELECT 1 FROM natsuhat.workspace_invitations").fetchone()


def test_cold_delete_needs_no_storage_but_preserves_lifecycle_guards(
    dashboard: tuple[httpx.Client, HostedDashboard],
    hosted: tuple[SupabaseStore, Services],
    tmp_path: Path,
) -> None:
    client, app = dashboard
    store, service = hosted
    login(client, OWNER)
    mfa(client)
    archive = tmp_path / "missing-results.tar.gz"
    archive.write_bytes(b"unreadable-synthetic-archive")
    job = {"id": SCAN, "status": "running"}
    store.save_job(WORKSPACE, job, {}, "")
    store.publish(WORKSPACE, SCAN, "00000000000000000001", {"vulnerabilities": []}, archive)
    service.fail_storage = True
    assert client.post(f"/api/runs/{SCAN}/delete", json={"confirmed": True}).status_code == 409
    job["status"] = "completed"
    store.save_job(WORKSPACE, job, {"cleanup": "failed"}, "")
    assert client.post(f"/api/runs/{SCAN}/delete", json={"confirmed": True}).status_code == 409
    store.save_job(WORKSPACE, job, {"cleanup": "paused", "results_pending": True}, "")
    assert client.post(f"/api/runs/{SCAN}/delete", json={"confirmed": True}).status_code == 409
    store.save_job(WORKSPACE, job, {"cleanup": "paused"}, "")
    assert client.post(f"/api/runs/{SCAN}/delete", json={"confirmed": True}).status_code == 200
    assert store.scans(OWNER, WORKSPACE) == []
    assert not (app.workspace(WORKSPACE).base / SCAN).exists()
    with pytest.raises(ClientError):
        store.collect_objects()
    service.fail_storage = False
    store.collect_objects()
    assert service.objects == {}
