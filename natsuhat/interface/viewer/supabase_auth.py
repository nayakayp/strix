"""Supabase Auth with encrypted, revocable server-side dashboard sessions."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from natsuhat.interface.viewer.llm_settings import JobError


if TYPE_CHECKING:
    from natsuhat.interface.viewer.supabase_store import SupabaseStore


COOKIE = "natsuhat_hosted_session"
SESSION_SECONDS = 7 * 24 * 3600
ADMIN_ROLES = {"owner", "admin"}
OPERATOR_ROLES = ADMIN_ROLES | {"operator"}


@dataclass
class Identity:
    session: str
    user_id: str
    email: str
    aal: str
    workspace: str | None
    role: str | None
    workspaces: list[dict[str, Any]]

    def require(self, *, admin: bool = False, operate: bool = False, mfa: bool = False) -> str:
        if not self.workspace or not self.role:
            raise JobError("Select a workspace you belong to.", 403)
        if admin and self.role not in ADMIN_ROLES:
            raise JobError("Workspace administrator access is required.", 403)
        if operate and self.role not in OPERATOR_ROLES:
            raise JobError("Workspace operator access is required.", 403)
        if (mfa or self.role in ADMIN_ROLES) and self.aal != "aal2":
            raise JobError("Verify an authenticator code before this operation.", 403)
        return self.workspace


class SupabaseAuth:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store
        self.settings = store.settings
        key = base64.urlsafe_b64encode(
            hashlib.sha256(self.settings.encryption_key.encode()).digest()
        )
        self.cipher = Fernet(key)
        self.keys = jwt.PyJWKClient(
            self.settings.url + "/auth/v1/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=300,
            timeout=10,
        )
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def cookie(self, value: str, *, clear: bool = False) -> str:
        secure = "; Secure" if self.settings.public_url.startswith("https:") else ""
        return (
            f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={0 if clear else SESSION_SECONDS}{secure}"
        )

    def throttle(self, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._attempts = {
                key: [t for t in values if now - t < 60]
                for key, values in self._attempts.items()
                if values and now - values[-1] < 60
            }
            attempts = self._attempts.setdefault(address, [])
            if len(attempts) >= 10:
                raise JobError("Too many authentication attempts. Try again in one minute.", 429)
            attempts.append(now)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        admin: bool = False,
    ) -> Any:
        key = self.settings.secret_key if admin else self.settings.publishable_key
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.request(
                    method,
                    self.settings.url + "/auth/v1/" + path,
                    headers={
                        "apikey": key,
                        **({"Authorization": "Bearer " + token} if token else {}),
                    },
                    json=body,
                )
            if response.status_code == 429:
                raise JobError("Authentication rate limit reached. Try again later.", 429)
            if (
                admin
                and path == "invite"
                and response.status_code == 422
                and response.json().get("error_code") == "email_exists"
            ):
                raise JobError("Existing account can accept this invitation on sign-in.", 422)
            if response.is_error:
                raise JobError(
                    "Authentication request failed. Check your credentials or try again.", 401
                )
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, JobError):
                raise
            raise JobError("Authentication service unavailable. Try again later.", 503) from None

    def claims(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self.keys.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience="authenticated",
                issuer=self.settings.url + "/auth/v1",
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
            UUID(claims["sub"])
            if claims.get("role") != "authenticated":
                raise JobError("Invalid user session.", 401)
        except jwt.ExpiredSignatureError:
            raise
        except (jwt.PyJWTError, ValueError, KeyError):
            raise JobError("Invalid user session.", 401) from None
        else:
            return claims

    def login(self, body: dict[str, Any]) -> str:
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
            raise JobError("Enter your email and password.")
        tokens = self.request(
            "POST", "token?grant_type=password", {"email": email, "password": password}
        )
        return self.create_session(tokens)

    def create_session(self, tokens: dict[str, Any]) -> str:
        claims = self.claims(tokens["access_token"])
        user = self.request("GET", "user", token=tokens["access_token"])
        if user.get("id") != claims["sub"] or not user.get("email_confirmed_at"):
            raise JobError("Verify your email before signing in.", 403)
        value = secrets.token_urlsafe(32)
        identifier = hashlib.sha256(value.encode()).hexdigest()
        with self.store.transaction() as db:
            # Only a verified Auth email can consume an unexpired workspace invitation.
            db.execute(
                "INSERT INTO natsuhat.workspace_members(workspace_id,user_id,role) "
                "SELECT workspace_id,%s,role FROM natsuhat.workspace_invitations "
                "WHERE email=%s AND expires_at>now() ON CONFLICT DO NOTHING",
                (user["id"], user["email"].lower()),
            )
            db.execute(
                "DELETE FROM natsuhat.workspace_invitations WHERE email=%s AND expires_at>now()",
                (user["email"].lower(),),
            )
            db.execute("DELETE FROM natsuhat.app_sessions WHERE expires_at<=now()")
            db.execute(
                "INSERT INTO natsuhat.app_sessions(id,user_id,encrypted_tokens) VALUES(%s,%s,%s)",
                (identifier, user["id"], self.encrypt(tokens)),
            )
        return value

    def encrypt(self, tokens: dict[str, Any]) -> str:
        # Do not persist provider response metadata or passwords.
        value = {key: tokens[key] for key in ("access_token", "refresh_token")}
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def authenticate(self, value: str) -> tuple[Identity, dict[str, Any]]:
        if not value or len(value) > 128:
            raise JobError("Sign in to continue.", 401)
        identifier = hashlib.sha256(value.encode()).hexdigest()
        with self.store.transaction() as db:
            # Row lock coordinates rotation across concurrent browser polls.
            row = db.execute(
                "SELECT * FROM natsuhat.app_sessions WHERE id=%s AND expires_at>now() FOR UPDATE",
                (identifier,),
            ).fetchone()
            if row is None:
                raise JobError("Your session expired. Sign in again.", 401)
            try:
                tokens = json.loads(self.cipher.decrypt(row["encrypted_tokens"].encode()))
            except (InvalidToken, ValueError):
                raise JobError("Your session is unavailable. Sign in again.", 401) from None
            try:
                claims = self.claims(tokens["access_token"])
            except jwt.ExpiredSignatureError:
                tokens = self.request(
                    "POST",
                    "token?grant_type=refresh_token",
                    {"refresh_token": tokens["refresh_token"]},
                )
                claims = self.claims(tokens["access_token"])
                db.execute(
                    "UPDATE natsuhat.app_sessions SET encrypted_tokens=%s WHERE id=%s",
                    (self.encrypt(tokens), identifier),
                )
            if claims["sub"] != str(row["user_id"]):
                raise JobError("Invalid user session.", 401)
        workspaces = self.store.workspaces(claims["sub"])
        selected = next((w for w in workspaces if w["id"] == str(row["workspace_id"])), None)
        if selected is None and workspaces:
            selected = workspaces[0]
        identity = Identity(
            identifier,
            claims["sub"],
            claims.get("email", ""),
            claims.get("aal", "aal1"),
            selected["id"] if selected else None,
            selected["role"] if selected else None,
            workspaces,
        )
        return identity, tokens

    def update_tokens(self, identity: Identity, tokens: dict[str, Any]) -> None:
        claims = self.claims(tokens["access_token"])
        if claims["sub"] != identity.user_id:
            raise JobError("Invalid user session.", 401)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE natsuhat.app_sessions SET encrypted_tokens=%s "
                "WHERE id=%s AND expires_at>now()",
                (self.encrypt(tokens), identity.session),
            )

    def logout(self, identity: Identity, tokens: dict[str, Any]) -> None:
        # Local revocation commits first even if the Auth provider is unavailable.
        with self.store.transaction() as db:
            db.execute("DELETE FROM natsuhat.app_sessions WHERE id=%s", (identity.session,))
        with contextlib.suppress(JobError):
            self.request("POST", "logout?scope=local", token=tokens["access_token"])
