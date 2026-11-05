"""Explicit hosted-dashboard API; local viewer capability tokens never enter it."""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from urllib.parse import unquote
from uuid import UUID

import psycopg

from natsuhat.interface.viewer.hosted_workspace import HostedWorkspace
from natsuhat.interface.viewer.jobs import ACTIVE_STATUSES
from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.remote import remote_limit, remote_timeout, runners
from natsuhat.interface.viewer.supabase_auth import (
    ADMIN_ROLES,
    OPERATOR_ROLES,
    Identity,
    SupabaseAuth,
)
from natsuhat.interface.viewer.supabase_store import HostedSettings, SupabaseStore
from natsuhat.interface.viewer.transcript import primary_target, severity_counts


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)
Reply = tuple[int, Any, dict[str, str]]


class HostedDashboard:
    def __init__(self, root: Path, settings: HostedSettings) -> None:
        self.store = SupabaseStore(settings)
        self.auth = SupabaseAuth(self.store)
        self.root = root / "hosted"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.workspaces: dict[str, HostedWorkspace] = {}
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        # One controller across hosts, not just one per local filesystem.
        self.owner = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10)
        acquired = self.owner.execute("SELECT pg_try_advisory_lock(173528991, 1)").fetchone()
        if not acquired or not acquired[0]:
            self.owner.close()
            raise JobError("Another hosted controller owns this database.", 409)
        try:
            with self.store.transaction() as db:
                db.execute("SELECT 1 FROM natsuhat.workspaces LIMIT 1")
            for path in self.root.iterdir():
                if path.is_dir():
                    with contextlib.suppress(ValueError):
                        UUID(path.name)
                        self.workspace(path.name)
        except BaseException:
            self.owner.close()
            raise
        self.publisher = threading.Thread(
            target=self._publish, name="supabase-publisher", daemon=True
        )
        self.publisher.start()

    def workspace(self, identifier: str) -> HostedWorkspace:
        identifier = str(UUID(identifier))
        with self.lock:
            if self.stopped.is_set():
                raise JobError(
                    "Hosted controller is unavailable. Restart it before managing scans.", 503
                )
            if identifier not in self.workspaces:
                self.workspaces[identifier] = HostedWorkspace(self.root, identifier, self.store)
            return self.workspaces[identifier]

    def _publish(self) -> None:
        while not self.stopped.wait(5):
            try:
                self.owner.execute("SELECT 1")
            except psycopg.Error:
                logger.error(  # noqa: TRY400 - connection errors can include credentials
                    "Hosted controller lost database ownership; stopping its scan workers."
                )
                self.stopped.set()
                for workspace in list(self.workspaces.values()):
                    workspace.close()
                return
            for workspace in list(self.workspaces.values()):
                workspace.flush()
            try:
                with self.lock:
                    self.store.collect_objects()
            except Exception as exc:  # noqa: BLE001 - persistent deletion queue retries
                logger.warning("Private object cleanup pending (%s)", type(exc).__name__)

    def close(self) -> None:
        self.stopped.set()
        self.publisher.join(timeout=70)
        for workspace in list(self.workspaces.values()):
            workspace.close()
        self.owner.close()

    def dispatch(  # noqa: PLR0911, PLR0912, PLR0915 - endpoint authorization stays beside each action
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        cookie: str,
        body: dict[str, Any],
        address: str,
        *,
        stream: BinaryIO | None = None,
        length: int = 0,
        filename: str = "source.zip",
        workspace_id: str | None = None,
    ) -> Reply:
        if path.startswith("/api/auth/"):
            return self._auth_route(
                method, path.removeprefix("/api/auth/"), cookie, body, address, workspace_id
            )
        if path == "/api/capabilities":
            try:
                identity, _ = self.auth.authenticate(cookie)
                allowed = identity.role in OPERATOR_ROLES and identity.aal == "aal2"
                admin = identity.role in ADMIN_ROLES and identity.aal == "aal2"
            except JobError:
                allowed = admin = False
            return (
                200,
                {
                    "hosted": True,
                    "can_steer": False,
                    "can_manage_scans": allowed,
                    "can_manage_settings": admin,
                },
                {},
            )
        identity, _ = self.auth.authenticate(cookie)
        wid = identity.require()
        # OAuth redirects carry a one-time state bound to this session and the
        # workspace's GitHub connector, rather than a browser-supplied header.
        if path != "/api/github/callback" and workspace_id != wid:
            raise JobError("Workspace changed. Refresh this tab before continuing.", 409)
        if method == "GET":
            return self._read(path, query, identity, wid)
        identity.require(operate=True, mfa=True)
        workspace = self.workspace(wid)
        manager = workspace.manager
        if path.startswith(("/api/llm/", "/api/github/", "/api/runners/")) and path not in {
            "/api/runners/check",
            "/api/github/check",
        }:
            identity.require(admin=True)
        if path == "/api/sources/upload":
            if stream is None:
                raise JobError("Provide a ZIP archive.")
            result = manager.sources.upload(stream, length, filename)
            self.store.save_source(
                wid,
                result["upload_id"],
                result,
                manager.sources.directory(result["upload_id"]) / "source.zip",
            )
            self.store.audit(wid, identity.user_id, "source.upload", result["upload_id"])
            return 201, result, {}
        if path == "/api/jobs" or (
            path.startswith("/api/jobs/") and path.endswith(("/resume", "/rerun"))
        ):
            # Check ownership immediately before any provider-side action.
            self.owner.execute("SELECT 1")
            active = sum(
                job["status"] in ACTIVE_STATUSES
                for w in self.workspaces.values()
                for job in w.manager.list()
            )
            if active >= remote_limit():
                raise JobError("Dashboard scan concurrency limit reached.", 409)
        if path == "/api/jobs":
            source = body.get("source")
            if isinstance(source, dict) and cast("dict[str, Any]", source).get("kind") == "zip":
                source = cast("dict[str, Any]", source)
                workspace.restore_source(identity.user_id, str(source.get("upload_id", "")))
            self.store.audit(wid, identity.user_id, "scan.start.requested")
            job = manager.start(body, created_by=identity.user_id)
            return 202, {"job": job}, {}
        if path.startswith("/api/jobs/"):
            name, action = path.removeprefix("/api/jobs/").rsplit("/", 1)
            name = unquote(name)
            with manager.lock:
                workspace.restore(identity.user_id, name)
                self.store.audit(wid, identity.user_id, "scan." + action + ".requested", name)
                if action == "stop":
                    job = manager.stop(name)
                elif action == "resume":
                    job = manager.resume(name, body)
                elif action == "rerun":
                    job = manager.rerun(name, body)
                else:
                    raise JobError("Unknown action.", 404)
            return 200 if action == "stop" else 202, {"job": job}, {}
        if path.startswith("/api/runs/") and path.endswith("/delete"):
            identity.require(admin=True)
            if body.get("confirmed") is not True:
                raise JobError("Confirm deletion.")
            name = unquote(path.removeprefix("/api/runs/").removesuffix("/delete"))
            workspace.delete(identity.user_id, name)
            self.store.audit(wid, identity.user_id, "scan.delete", name)
            return 200, {"ok": True}, {}
        if path.startswith("/api/llm/"):
            action = path.removeprefix("/api/llm/")
            result = (
                {"models": manager.llm_profiles.models(body.get("id"))}
                if action == "models"
                else manager.llm_profiles.update(action, body)
            )
        elif path == "/api/runners/check":
            result = manager.runner_settings.check(body.get("provider"))
        elif path == "/api/runners/settings":
            result = manager.runner_settings.update(body)
        elif path.startswith("/api/github/"):
            action = path.removeprefix("/api/github/")
            result = manager.github.update(action, body, identity.session)
        else:
            raise JobError("Unknown endpoint.", 404)
        self.store.audit(wid, identity.user_id, "settings.update", path)
        return 200, result, {}

    def _read(  # noqa: PLR0912 - read-only endpoint dispatch
        self, path: str, query: dict[str, list[str]], identity: Identity, wid: str
    ) -> Reply:
        result: Any
        if path == "/api/github/callback":
            identity.require(admin=True)
            location = self.workspace(wid).manager.github.callback(query, identity.session)
            return 303, {}, {"Location": location}
        if path in {
            "/api/llm/settings",
            "/api/runners/settings",
            "/api/github",
            "/api/github/refresh",
        }:
            identity.require(operate=True, mfa=True)
            if path == "/api/runners/settings":
                identity.require(admin=True)
            manager = self.workspace(wid).manager
            if path == "/api/llm/settings":
                result = manager.llm_profiles.public()
            elif path == "/api/runners/settings":
                result = manager.runner_settings.public()
            else:
                result = manager.github.public(refresh=path.endswith("/refresh"))
            return 200, result, {}
        rows = self.store.scans(identity.user_id, wid)
        if path == "/api/runs":
            entries: list[dict[str, Any]] = []
            for row in rows:
                payload: dict[str, Any] = row["payload"] or {}
                summary: dict[str, Any] = payload.get("summary", {})
                entries.append(
                    {
                        "name": row["id"],
                        "target": primary_target(summary) or row["job"].get("target"),
                        "scan_mode": summary.get("scan_mode", row["job"].get("scan_mode")),
                        "status": summary.get("status", row["job"].get("status")),
                        "start_time": summary.get("start_time", row["job"].get("start_time")),
                        "end_time": summary.get("end_time", row["job"].get("end_time")),
                        "finished": summary.get(
                            "finished",
                            row["job"]["status"] not in {"starting", "running", "stopping"},
                        ),
                        "severity_counts": severity_counts(payload.get("vulnerabilities", [])),
                    }
                )
            return 200, {"locked": False, "count": len(entries), "runs": entries}, {}
        if path == "/api/jobs":
            workspace = self.workspaces.get(wid)
            local = {job["id"]: job for job in workspace.manager.list()} if workspace else {}
            jobs: list[dict[str, Any]] = []
            for row in rows:
                # A replacement controller has no local checkpoint yet. Offer
                # resume for retained snapshots; restore validates the actual
                # checkpoint before ScanJobManager can launch a paid worker.
                can_resume = bool(
                    row["payload"] is not None
                    and row["job"]["status"] in {"stopped", "interrupted"}
                    and row["job"].get("runner") == "e2b"
                    and row["runner"].get("cleanup") == "paused"
                    and row["runner"].get("sandbox_id")
                    and not row["runner"].get("results_pending")
                )
                jobs.append(
                    {
                        **row["job"],
                        "remote": row["runner"],
                        "has_run": row["payload"] is not None,
                        "can_resume": can_resume,
                        "resume_unavailable_reason": (
                            None if can_resume else "No retained resumable snapshot is available."
                        ),
                        **local.get(row["id"], {}),
                        "sync_pending": bool(
                            workspace
                            and (
                                (workspace.base / row["id"] / ".cloud-final").exists()
                                or (workspace.base / row["id"] / ".cloud-error").exists()
                            )
                        ),
                    }
                )
            return (
                200,
                {
                    "jobs": jobs,
                    "runners": runners(),
                    "default_runner": "e2b",
                    "max_concurrent": remote_limit(),
                    "max_remote_concurrent": remote_limit(),
                    "remote_timeout_seconds": remote_timeout(),
                },
                {},
            )
        if path.startswith("/api/jobs/") and path.endswith("/log"):
            identity.require(operate=True)
            name = unquote(path.removeprefix("/api/jobs/").removesuffix("/log"))
            workspace = self.workspace(wid)
            workspace.restore(identity.user_id, name)
            return 200, {"text": workspace.manager.log(name)}, {}
        name = (query.get("run") or [rows[0]["id"] if rows else ""])[0]
        row = self.store.scan(identity.user_id, wid, name)
        payload = row["payload"] or {}
        if path == "/api/run":
            result = payload.get("summary") or {
                "run_id": name,
                "run_name": name,
                **row["job"],
                "finished": False,
            }
        elif path == "/api/vulnerabilities":
            result = payload.get("vulnerabilities", [])
        elif path == "/api/report":
            result = {"markdown": payload.get("report", "")}
        elif path == "/api/transcript":
            result = payload.get("transcript", {"agents": [], "events": []})
        else:
            raise JobError("Unknown endpoint.", 404)
        return 200, result, {}

    def _auth_route(  # noqa: PLR0911, PLR0912, PLR0915 - account endpoint dispatch
        self,
        method: str,
        action: str,
        cookie: str,
        body: dict[str, Any],
        address: str,
        workspace_id: str | None,
    ) -> Reply:
        if method == "POST" and action in {"login", "verify", "recover"}:
            self.auth.throttle(address)
            if action == "recover":
                with contextlib.suppress(JobError):
                    self.auth.request("POST", "recover", {"email": str(body.get("email", ""))})
                return (
                    200,
                    {
                        "ok": True,
                        "message": "If this account exists, a recovery email has been sent.",
                    },
                    {},
                )
            if action == "login":
                value = self.auth.login(body)
            else:
                if body.get("type") not in {"invite", "recovery"} or not isinstance(
                    body.get("token_hash"), str
                ):
                    raise JobError("Invalid confirmation link.")
                tokens = self.auth.request(
                    "POST", "verify", {"type": body["type"], "token_hash": body["token_hash"]}
                )
                value = self.auth.create_session(tokens)
            return 200, {"ok": True}, {"Set-Cookie": self.auth.cookie(value)}
        try:
            identity, tokens = self.auth.authenticate(cookie)
        except JobError as exc:
            if method == "GET" and action == "session" and exc.status == 401:
                return 200, {"hosted": True, "user": None}, {}
            raise
        if action in {"members", "invite", "member", "transfer", "create-workspace"}:
            wid = identity.require(admin=True)
            if workspace_id != wid:
                raise JobError("Workspace changed. Refresh this tab before continuing.", 409)
        if method == "GET" and action == "session":
            return (
                200,
                {
                    "hosted": True,
                    "user": {"id": identity.user_id, "email": identity.email},
                    "aal": identity.aal,
                    "workspace": identity.workspace,
                    "role": identity.role,
                    "workspaces": identity.workspaces,
                },
                {},
            )
        if method == "GET" and action == "factors":
            user = self.auth.request("GET", "user", token=tokens["access_token"])
            return 200, {"factors": user.get("factors", [])}, {}
        if method == "GET" and action == "members":
            wid = identity.require(admin=True)
            with self.store.transaction(identity.user_id) as db:
                members = db.execute(
                    "SELECT user_id::text,role FROM natsuhat.workspace_members "
                    "WHERE workspace_id=%s",
                    (wid,),
                ).fetchall()
                invitations = db.execute(
                    "SELECT email,role,expires_at FROM natsuhat.workspace_invitations "
                    "WHERE workspace_id=%s",
                    (wid,),
                ).fetchall()
            return 200, {"members": members, "invitations": invitations}, {}
        if method != "POST":
            raise JobError("Unknown endpoint.", 404)
        if action == "logout":
            self.auth.logout(identity, tokens)
            return 200, {"ok": True}, {"Set-Cookie": self.auth.cookie("", clear=True)}
        if action == "workspace":
            wid = str(body.get("workspace", ""))
            if not any(w["id"] == wid for w in identity.workspaces):
                raise JobError("Unknown workspace.", 403)
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE natsuhat.app_sessions SET workspace_id=%s WHERE id=%s",
                    (wid, identity.session),
                )
        elif action == "password":
            password = body.get("password")
            if not isinstance(password, str) or len(password) < 12:
                raise JobError("Use a password with at least 12 characters.")
            user = self.auth.request("GET", "user", token=tokens["access_token"])
            if identity.aal != "aal2" and any(
                f.get("status") == "verified" for f in user.get("factors", [])
            ):
                raise JobError("Verify MFA before changing your password.", 403)
            self.auth.request("PUT", "user", {"password": password}, token=tokens["access_token"])
            with self.store.transaction() as db:
                db.execute(
                    "DELETE FROM natsuhat.app_sessions WHERE user_id=%s AND id<>%s",
                    (identity.user_id, identity.session),
                )
        elif action == "mfa/enroll":
            result = self.auth.request(
                "POST",
                "factors",
                {"factor_type": "totp", "friendly_name": "Natsuhat"},
                token=tokens["access_token"],
            )
            return 200, result, {}
        elif action == "mfa/verify":
            factor = str(UUID(str(body.get("factor_id", ""))))
            challenge = self.auth.request(
                "POST", f"factors/{factor}/challenge", {}, token=tokens["access_token"]
            )
            result = self.auth.request(
                "POST",
                f"factors/{factor}/verify",
                {"challenge_id": challenge["id"], "code": str(body.get("code", ""))},
                token=tokens["access_token"],
            )
            self.auth.update_tokens(identity, result)
        elif action == "mfa/remove":
            if identity.aal != "aal2":
                raise JobError("Verify MFA before removing a factor.", 403)
            factor = str(UUID(str(body.get("factor_id", ""))))
            self.auth.request("DELETE", f"factors/{factor}", token=tokens["access_token"])
            # Reauthentication is mandatory after changing enrolled factors.
            with self.store.transaction() as db:
                db.execute(
                    "DELETE FROM natsuhat.app_sessions WHERE user_id=%s", (identity.user_id,)
                )
            self.auth.logout(identity, tokens)
            return 200, {"ok": True}, {"Set-Cookie": self.auth.cookie("", clear=True)}
        elif action in {"invite", "member", "transfer", "create-workspace"}:
            self._membership(action, identity, body)
        else:
            raise JobError("Unknown endpoint.", 404)
        return 200, {"ok": True}, {}

    def _membership(  # noqa: PLR0912 - explicit role and ownership transitions
        self, action: str, identity: Identity, body: dict[str, Any]
    ) -> None:
        wid = identity.require(admin=True)
        role = body.get("role")
        if action == "invite":
            email = str(body.get("email", "")).strip().lower()
            if "@" not in email or len(email) > 254 or role not in {"admin", "operator", "viewer"}:
                raise JobError("Provide an email and a valid workspace role.")
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO natsuhat.workspace_invitations(workspace_id,email,role) "
                    "VALUES(%s,%s,%s) ON CONFLICT(workspace_id,email) DO UPDATE "
                    "SET role=excluded.role,expires_at=now()+interval '7 days'",
                    (wid, email, role),
                )
            # Existing verified users accept pending invites on their next sign-in.
            # Supabase rejects /invite for existing users; do not fail the DB invitation.
            try:
                self.auth.request("POST", "invite", {"email": email}, admin=True)
            except JobError as exc:
                if exc.status != 422:
                    raise
        elif action == "create-workspace":
            if identity.role != "owner":
                raise JobError("Only workspace owners can create workspaces.", 403)
            name = str(body.get("name", "")).strip()
            if not 1 <= len(name) <= 120:
                raise JobError("Use a workspace name between 1 and 120 characters.")
            with self.store.transaction() as db:
                row = db.execute(
                    "INSERT INTO natsuhat.workspaces(name) VALUES(%s) RETURNING id", (name,)
                ).fetchone()
                assert row is not None
                db.execute(
                    "INSERT INTO natsuhat.workspace_members VALUES(%s,%s,'owner')",
                    (row["id"], identity.user_id),
                )
        else:
            user = str(UUID(str(body.get("user_id", ""))))
            with self.store.transaction() as db:
                db.execute("SELECT id FROM natsuhat.workspaces WHERE id=%s FOR UPDATE", (wid,))
                target = db.execute(
                    "SELECT role FROM natsuhat.workspace_members "
                    "WHERE workspace_id=%s AND user_id=%s",
                    (wid, user),
                ).fetchone()
                if not target or user == identity.user_id or target["role"] == "owner":
                    raise JobError("Cannot change yourself, an owner, or an unknown member.", 403)
                if action == "transfer":
                    if identity.role != "owner":
                        raise JobError("Only the owner can transfer ownership.", 403)
                    db.execute(
                        "UPDATE natsuhat.workspace_members SET role='admin' "
                        "WHERE workspace_id=%s AND user_id=%s",
                        (wid, identity.user_id),
                    )
                    db.execute(
                        "UPDATE natsuhat.workspace_members SET role='owner' "
                        "WHERE workspace_id=%s AND user_id=%s",
                        (wid, user),
                    )
                elif role is None:
                    db.execute(
                        "DELETE FROM natsuhat.workspace_members "
                        "WHERE workspace_id=%s AND user_id=%s",
                        (wid, user),
                    )
                elif role in {"admin", "operator", "viewer"}:
                    db.execute(
                        "UPDATE natsuhat.workspace_members SET role=%s "
                        "WHERE workspace_id=%s AND user_id=%s",
                        (role, wid, user),
                    )
                else:
                    raise JobError("Invalid workspace role.")
        self.store.audit(wid, identity.user_id, "workspace." + action)
