"""Hosted catalog and private object storage. Never imported by scan workers."""

from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import boto3  # type: ignore[import-untyped]
import psycopg
from botocore.config import Config  # type: ignore[import-untyped]
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from natsuhat.interface.viewer.llm_settings import JobError


if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(repr=False)
class HostedSettings:
    url: str
    publishable_key: str
    secret_key: str
    database_url: str
    s3_endpoint: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    encryption_key: str
    public_url: str

    @classmethod
    def from_env(cls) -> HostedSettings:
        names = (
            "SUPABASE_URL",
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_SECRET_KEY",
            "DATABASE_URL",
            "SUPABASE_S3_ENDPOINT",
            "SUPABASE_S3_REGION",
            "SUPABASE_S3_ACCESS_KEY_ID",
            "SUPABASE_S3_SECRET_ACCESS_KEY",
            "NATSUHAT_SESSION_ENCRYPTION_KEY",
            "NATSUHAT_PUBLIC_URL",
        )
        values = [os.environ.get(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        if missing:
            raise JobError("Hosted mode requires: " + ", ".join(missing))
        settings = cls(*values)
        for url in (settings.url, settings.s3_endpoint, settings.public_url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
            ):
                raise JobError("Hosted service URLs must use HTTPS (except local development).")
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise JobError("Hosted service URLs must not contain credentials or query strings.")
        if len(settings.encryption_key) < 32:
            raise JobError(
                "NATSUHAT_SESSION_ENCRYPTION_KEY must contain at least 32 random characters."
            )
        connection = conninfo_to_dict(settings.database_url)
        sslmode = connection.get("sslmode", "require")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise JobError("Hosted PostgreSQL requires TLS.")
        settings.database_url = make_conninfo(settings.database_url, sslmode=sslmode)
        settings.url = settings.url.rstrip("/")
        settings.public_url = settings.public_url.rstrip("/")
        if urlsplit(settings.public_url).path:
            raise JobError("NATSUHAT_PUBLIC_URL must be an origin without a path.")
        return settings


class SupabaseStore:
    def __init__(self, settings: HostedSettings) -> None:
        self.settings = settings
        self.objects: Any = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3},
            ),
        )

    @contextlib.contextmanager
    def transaction(
        self, user_id: str | None = None
    ) -> Generator[psycopg.Connection[dict[str, Any]]]:
        # Context and SET ROLE are transaction-local; no identity survives reuse.
        with psycopg.Connection[dict[str, Any]].connect(
            self.settings.database_url, row_factory=dict_row, connect_timeout=10
        ) as connection:
            connection.execute("SET LOCAL statement_timeout = '30s'")
            connection.execute(
                "SET LOCAL ROLE natsuhat_worker"
                if user_id is None
                else "SET LOCAL ROLE natsuhat_api"
            )
            if user_id:
                connection.execute(
                    "SELECT set_config('request.jwt.claim.sub', %s, true)", (user_id,)
                )
            yield connection

    def workspaces(self, user_id: str) -> list[dict[str, Any]]:
        with self.transaction(user_id) as db:
            return db.execute(
                "SELECT w.id::text, w.name, m.role FROM natsuhat.workspaces w "
                "JOIN natsuhat.workspace_members m ON m.workspace_id=w.id "
                "WHERE m.user_id=%s ORDER BY w.created_at",
                (user_id,),
            ).fetchall()

    def scans(self, user_id: str, workspace: str) -> list[dict[str, Any]]:
        # Polling lists need metadata and severity counts, never full scan output.
        with self.transaction(user_id) as db:
            return db.execute(
                "SELECT s.id, s.job, s.runner, "
                "CASE WHEN p.scan_id IS NULL THEN NULL ELSE jsonb_build_object("
                "'summary', COALESCE((SELECT jsonb_object_agg(key,value) "
                "FROM jsonb_each(p.payload->'summary') WHERE key IN "
                "('targets_info','scan_mode','status','start_time','end_time','finished')), "
                "'{}'::jsonb), "
                "'vulnerabilities', COALESCE((SELECT jsonb_agg("
                "jsonb_build_object('severity',item->'severity')) "
                "FROM jsonb_array_elements(p.payload->'vulnerabilities') item), '[]'::jsonb)) "
                "END AS payload FROM natsuhat.scans s "
                "LEFT JOIN natsuhat.scan_snapshots p "
                "ON p.workspace_id=s.workspace_id AND p.scan_id=s.id "
                "WHERE s.workspace_id=%s AND s.deleted_at IS NULL ORDER BY s.created_at DESC",
                (workspace,),
            ).fetchall()

    def scan(
        self, user_id: str, workspace: str, name: str | None, *, section: str | None = None
    ) -> dict[str, Any]:
        # Restoration needs the whole snapshot; HTTP reads select just one section.
        payload = sql.SQL("p.payload") if section is None else sql.SQL("p.payload -> %s")
        parameters: list[Any] = [] if section is None else [section]
        parameters.append(workspace)
        selected = sql.SQL("")
        if name is not None:
            selected = sql.SQL("AND s.id=%s ")
            parameters.append(name)
        with self.transaction(user_id) as db:
            row = db.execute(
                sql.SQL(
                    "SELECT s.*, {} AS payload, p.object_key, p.sha256 FROM natsuhat.scans s "
                    "LEFT JOIN natsuhat.scan_snapshots p "
                    "ON p.workspace_id=s.workspace_id AND p.scan_id=s.id "
                    "WHERE s.workspace_id=%s AND s.deleted_at IS NULL {}"
                    "ORDER BY s.created_at DESC LIMIT 1"
                ).format(payload, selected),
                parameters,
            ).fetchone()
        if row is None:
            raise JobError("Unknown scan.", 404)
        return row

    def save_job(
        self,
        workspace: str,
        job: dict[str, Any],
        runner: dict[str, Any],
        instructions: str,
        user_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            row = db.execute(
                "INSERT INTO natsuhat.scans(workspace_id,id,job,runner,instructions,created_by) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,id) DO UPDATE "
                "SET job=excluded.job, runner=excluded.runner "
                "WHERE natsuhat.scans.deleted_at IS NULL RETURNING id",
                (workspace, job["id"], Jsonb(job), Jsonb(runner), instructions, user_id),
            ).fetchone()
            if row is None:
                raise JobError("Scan has been deleted.", 410)
            db.execute(
                "UPDATE natsuhat.source_snapshots SET expires_at=NULL "
                "WHERE workspace_id=%s AND scan_id=%s",
                (workspace, job["id"]),
            )

    def put(self, bucket: str, key: str, path: Any) -> tuple[str, int]:
        # upload_file uses multipart for large sources; never sends public ACLs.
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO natsuhat.storage_objects(bucket,object_key) VALUES(%s,%s) "
                "ON CONFLICT(bucket,object_key) DO UPDATE "
                "SET delete_after=now()+interval '24 hours' "
                "WHERE natsuhat.storage_objects.delete_after IS NOT NULL",
                (bucket, key),
            )
        self.objects.upload_file(str(path), bucket, key)
        return digest, path.stat().st_size

    def get(self, bucket: str, key: str, path: Any, digest: str) -> None:
        temporary = path.with_suffix(path.suffix + ".download")
        try:
            self.objects.download_file(bucket, key, str(temporary))
            with temporary.open("rb") as stream:
                received = hashlib.file_digest(stream, "sha256").hexdigest()
            if received != digest:
                raise JobError("Stored artifact checksum mismatch.", 502)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def source(self, user_id: str, workspace: str, name: str) -> dict[str, Any] | None:
        with self.transaction(user_id) as db:
            return db.execute(
                "SELECT * FROM natsuhat.source_snapshots WHERE workspace_id=%s AND scan_id=%s "
                "AND (expires_at IS NULL OR expires_at>now())",
                (workspace, name),
            ).fetchone()

    def save_source(self, workspace: str, name: str, metadata: dict[str, Any], path: Any) -> None:
        key = f"{workspace}/{name}/{metadata['sha256']}.zip"
        digest, size = self.put("scan-sources", key, path)
        if digest != metadata["sha256"]:
            raise JobError("Source archive checksum mismatch.")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO natsuhat.source_snapshots "
                "(workspace_id,scan_id,object_key,sha256,size,metadata) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(workspace_id,scan_id) DO UPDATE SET object_key=excluded.object_key, "
                "sha256=excluded.sha256,size=excluded.size,metadata=excluded.metadata",
                (workspace, name, key, digest, size, Jsonb(metadata)),
            )
            db.execute(
                "UPDATE natsuhat.storage_objects SET delete_after=NULL "
                "WHERE bucket='scan-sources' AND object_key=%s",
                (key,),
            )

    def publish(
        self, workspace: str, name: str, generation: str, payload: dict[str, Any], archive: Any
    ) -> None:
        key = f"{workspace}/{name}/{generation}.tar.gz"
        with self.transaction() as db:
            current = db.execute(
                "SELECT s.deleted_at,p.generation FROM natsuhat.scans s "
                "LEFT JOIN natsuhat.scan_snapshots p "
                "ON p.workspace_id=s.workspace_id AND p.scan_id=s.id "
                "WHERE s.workspace_id=%s AND s.id=%s",
                (workspace, name),
            ).fetchone()
            if (
                not current
                or current["deleted_at"]
                or (current["generation"] and current["generation"] >= generation)
            ):
                return
        digest, size = self.put("scan-artifacts", key, archive)
        with self.transaction() as db:
            # Serialize publication with deletion; old retries cannot resurrect a scan.
            row = db.execute(
                "SELECT deleted_at FROM natsuhat.scans WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (workspace, name),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return
            previous = db.execute(
                "SELECT generation,object_key FROM natsuhat.scan_snapshots "
                "WHERE workspace_id=%s AND scan_id=%s",
                (workspace, name),
            ).fetchone()
            if previous and previous["generation"] >= generation:
                return
            if previous:
                db.execute(
                    "UPDATE natsuhat.storage_objects SET delete_after=now() "
                    "WHERE bucket='scan-artifacts' AND object_key=%s",
                    (previous["object_key"],),
                )
            db.execute(
                "INSERT INTO natsuhat.scan_snapshots"
                "(workspace_id,scan_id,generation,payload,object_key,sha256,size) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,scan_id) DO UPDATE SET "
                "generation=excluded.generation,payload=excluded.payload,object_key=excluded.object_key,"
                "sha256=excluded.sha256,size=excluded.size,published_at=now()",
                (workspace, name, generation, Jsonb(payload), key, digest, size),
            )
            db.execute(
                "UPDATE natsuhat.storage_objects SET delete_after=NULL "
                "WHERE bucket='scan-artifacts' AND object_key=%s",
                (key,),
            )
            ids: list[str] = []
            for finding in payload["vulnerabilities"]:
                ids.append(finding["id"])
                db.execute(
                    "INSERT INTO natsuhat.findings(workspace_id,scan_id,id,title,severity,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,scan_id,id) DO UPDATE SET "
                    "title=excluded.title,severity=excluded.severity,payload=excluded.payload",
                    (
                        workspace,
                        name,
                        finding["id"],
                        finding.get("title", ""),
                        finding.get("severity", "low"),
                        Jsonb(finding),
                    ),
                )
            db.execute(
                "DELETE FROM natsuhat.findings WHERE workspace_id=%s AND scan_id=%s "
                "AND NOT(id=ANY(%s))",
                (workspace, name, ids),
            )

    def delete_scan(self, workspace: str, name: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE natsuhat.scans SET deleted_at=now(),job='{}',runner='{}',instructions='' "
                "WHERE workspace_id=%s AND id=%s",
                (workspace, name),
            )
            db.execute(
                "UPDATE natsuhat.storage_objects SET delete_after=now() "
                "WHERE starts_with(object_key,%s)",
                (f"{workspace}/{name}/",),
            )
            for table in ("scan_snapshots", "findings", "source_snapshots"):
                db.execute(
                    sql.SQL("DELETE FROM natsuhat.{} WHERE workspace_id=%s AND scan_id=%s").format(
                        sql.Identifier(table)
                    ),
                    (workspace, name),
                )

    def collect_objects(self) -> None:
        # Retryable deletion queue; references and queue changes commit atomically.
        with self.transaction() as db:
            expired = db.execute(
                "DELETE FROM natsuhat.source_snapshots WHERE expires_at<=now() RETURNING object_key"
            ).fetchall()
            for row in expired:
                db.execute(
                    "UPDATE natsuhat.storage_objects SET delete_after=now() "
                    "WHERE bucket='scan-sources' AND object_key=%s",
                    (row["object_key"],),
                )
            pending = db.execute(
                "SELECT bucket,object_key FROM natsuhat.storage_objects "
                "WHERE delete_after<=now() ORDER BY delete_after LIMIT 100 FOR UPDATE"
            ).fetchall()
            for row in pending:
                self.objects.delete_object(Bucket=row["bucket"], Key=row["object_key"])
                db.execute(
                    "DELETE FROM natsuhat.storage_objects WHERE bucket=%s AND object_key=%s",
                    (row["bucket"], row["object_key"]),
                )

    def audit(self, workspace: str, user_id: str, action: str, resource: str = "") -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO natsuhat.audit_events(workspace_id,user_id,action,resource) "
                "VALUES(%s,%s,%s,%s)",
                (workspace, user_id, action, resource),
            )
