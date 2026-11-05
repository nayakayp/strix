"""One workspace's scan controller and durable result-publication journal."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from natsuhat.interface.viewer.jobs import ACTIVE_STATUSES, ScanJobManager
from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.remote import RETAINED, import_results
from natsuhat.interface.viewer.remote_snapshot import snapshot
from natsuhat.interface.viewer.sources import SourceStore
from natsuhat.interface.viewer.transcript import (
    build_run_state,
    read_report_markdown,
    read_run_summary,
    read_vulnerabilities,
)
from natsuhat.report.writer import atomic_write_text
from natsuhat.utils.secret_files import write_secret_text


if TYPE_CHECKING:
    from natsuhat.interface.viewer.supabase_store import SupabaseStore


logger = logging.getLogger(__name__)


class HostedWorkspace:
    def __init__(self, root: Path, workspace: str, store: SupabaseStore) -> None:
        self.id = workspace
        self.store = store
        self.base = root / workspace / "natsuhat_runs"
        self.base.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.sources = SourceStore(self.base.parent)
        # A crash after the DB tombstone but before local cleanup must not reload a job.
        with self.store.transaction() as db:
            deleted = db.execute(
                "SELECT id FROM natsuhat.scans WHERE workspace_id=%s AND deleted_at IS NOT NULL",
                (workspace,),
            ).fetchall()
        for row in deleted:
            shutil.rmtree(self.base / row["id"], ignore_errors=True)
            self.sources.remove(row["id"])
        self.manager = ScanJobManager(self.base, on_save=self.save_job)

    def save_job(self, job: dict[str, Any]) -> None:
        directory = self.base / job["id"]
        pending = directory / ".cloud-jobs"
        pending.mkdir(mode=0o700, exist_ok=True)
        instructions = directory / "instructions.txt"
        runner = directory / "runner.json"
        value: dict[str, Any] = {
            "job": dict(job),
            "runner": json.loads(runner.read_text()) if runner.exists() else {},
            "instructions": instructions.read_text() if instructions.exists() else "",
        }
        generation = f"{time.time_ns():020}"
        write_secret_text(pending / f"{generation}.json", json.dumps(value))
        created = directory / ".cloud-created"
        if not created.exists():
            # Initial durability is mandatory before ScanJobManager launches E2B.
            try:
                if job.get("source"):
                    source = self.sources.directory(job["id"]) / "source.zip"
                    self.store.save_source(self.id, job["id"], job["source"], source)
                self.store.save_job(
                    self.id, job, value["runner"], value["instructions"], job.get("created_by")
                )
                created.touch(mode=0o600)
            except Exception:  # noqa: BLE001 - persistence fails closed before provider launch
                raise JobError(
                    "Could not persist this scan and its source. No sandbox was launched.", 503
                ) from None
        if job["status"] not in ACTIVE_STATUSES:
            atomic_write_text(directory / ".cloud-final", generation)

    def flush(self) -> None:
        # Same lock as job saves, resume and deletion: publication cannot regress job state.
        with self.manager.lock:
            for directory in self.base.iterdir():
                if not directory.is_dir() or not (directory / ".cloud-created").exists():
                    continue
                try:
                    self._flush_run(directory)
                except Exception as exc:  # noqa: BLE001 - durable journal retries without SDK details
                    logger.warning("Cloud publication pending (%s)", type(exc).__name__)
                    atomic_write_text(
                        directory / ".cloud-error", "Publication pending; retrying automatically."
                    )

    def _flush_run(self, directory: Path) -> None:
        jobs = sorted((directory / ".cloud-jobs").glob("*.json"))
        if jobs:
            value = json.loads(jobs[-1].read_text())
            self.store.save_job(self.id, value["job"], value["runner"], value["instructions"])
            for path in jobs:
                path.unlink(missing_ok=True)
        archives = sorted((directory / ".cloud-pending").glob("*.tar.gz"))
        final = directory / ".cloud-final"
        if archives:
            self._publish(directory, archives[-1], archives[-1].name.removesuffix(".tar.gz"))
            for archive in archives:
                archive.unlink(missing_ok=True)
        job = json.loads((directory / "job.json").read_text())
        if final.exists() and job["status"] not in ACTIVE_STATUSES:
            generation = final.read_text()
            # Final job status is written by the parent, after the worker exits.
            # Snapshot SQLite via backup(), not a raw copy of its live/WAL files.
            with tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "results.tar.gz"
                snapshot(directory, archive)
                self._publish(directory, archive, generation)
            if final.read_text() == generation:
                final.unlink(missing_ok=True)
        (directory / ".cloud-error").unlink(missing_ok=True)

    def _publish(self, directory: Path, archive: Path, generation: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            import_results(archive.read_bytes(), staging)
            job = json.loads((directory / "job.json").read_text())
            write_secret_text(staging / "job.json", json.dumps(job))
            runner_path = directory / "runner.json"
            runner: dict[str, Any] = (
                json.loads(runner_path.read_text()) if runner_path.exists() else {}
            )
            instructions = directory / "instructions.txt"
            self.store.save_job(
                self.id, job, runner, instructions.read_text() if instructions.exists() else ""
            )
            payload = {
                "summary": read_run_summary(staging),
                "vulnerabilities": read_vulnerabilities(staging),
                "report": read_report_markdown(staging),
                "transcript": build_run_state(staging),
            }
            self.store.publish(self.id, directory.name, generation, payload, archive)

    def restore(self, user_id: str, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", name):
            raise JobError("Unknown scan.", 404)
        row = self.store.scan(user_id, self.id, name)
        directory = self.base / name
        if not directory.exists():
            if row["job"]["status"] in ACTIVE_STATUSES:
                raise JobError(
                    "This scan needs operator recovery; "
                    "its previous controller may still be active.",
                    409,
                )
            directory.mkdir(mode=0o700)
            try:
                if row.get("object_key"):
                    with tempfile.TemporaryDirectory() as temporary:
                        archive = Path(temporary) / "results.tar.gz"
                        self.store.get("scan-artifacts", row["object_key"], archive, row["sha256"])
                        import_results(archive.read_bytes(), directory)
                write_secret_text(directory / "job.json", json.dumps(row["job"]))
                write_secret_text(directory / "runner.json", json.dumps(row["runner"]))
                write_secret_text(directory / "instructions.txt", row["instructions"])
                (directory / ".cloud-created").touch(mode=0o600)
                self.manager.load_restored_job(name)
            except BaseException:
                shutil.rmtree(directory)
                raise
        if row["job"].get("source"):
            self.restore_source(user_id, name)
        return directory

    def restore_source(self, user_id: str, name: str) -> None:
        source = self.store.source(user_id, self.id, name)
        if not source:
            raise JobError("Source upload expired or is unavailable.", 410)
        target = self.sources.directory(name)
        target.mkdir(mode=0o700, exist_ok=True)
        if not (target / "source.zip").exists():
            self.store.get(
                "scan-sources", source["object_key"], target / "source.zip", source["sha256"]
            )
            write_secret_text(target / "source.json", json.dumps(source["metadata"]))
        if source["expires_at"]:
            # Preserve expiry through controller restarts, not a fresh 24-hour lease.
            timestamp = source["expires_at"].timestamp() - 24 * 3600
            os.utime(target, (timestamp, timestamp))

    def delete(self, user_id: str, name: str) -> None:
        with self.manager.lock:
            row = self.store.scan(user_id, self.id, name)
            directory = self.base / name
            if directory.exists() or directory.is_symlink():
                # Reuse local process/retention guards without restoring bytes.
                self.manager.delete(
                    name, before_remove=lambda: self.store.delete_scan(self.id, name)
                )
                return
            local = next((job for job in self.manager.list() if job["id"] == name), row["job"])
            if local["status"] in ACTIVE_STATUSES or row["job"]["status"] in ACTIVE_STATUSES:
                raise JobError("Stop the scan and wait for it to finish before deleting it.", 409)
            remote = row["runner"]
            if remote and (remote.get("cleanup") not in RETAINED or remote.get("results_pending")):
                raise JobError("Retrieve results and finish sandbox cleanup before deleting.", 409)
            # Cold deletion depends only on the catalog, not the objects being
            # deleted. The durable queue handles Storage outages or missing files.
            self.store.delete_scan(self.id, name)
            if row["job"].get("source"):
                self.sources.remove(name)

    def close(self) -> None:
        self.manager.close()
        self.flush()
