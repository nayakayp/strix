"""Scan jobs owned by an explicitly enabled dashboard.

Each child runs the normal headless CLI in a separate process. Job metadata and
logs live with the run; shutdown stops children rather than leaving scans behind.
The owner lock prevents two managing viewers from scanning the same workspace.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from natsuhat.interface.viewer.github import GithubConnection
from natsuhat.interface.viewer.llm_settings import JobError, LlmProfiles
from natsuhat.interface.viewer.remote import (
    RETAINED,
    remote_limit,
    remote_timeout,
    runners,
    validate_runner,
)
from natsuhat.interface.viewer.runner_settings import RunnerSettings
from natsuhat.interface.viewer.sources import SourceStore
from natsuhat.interface.viewer.transcript import read_run_summary
from natsuhat.report.writer import atomic_write_text


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


__all__ = ["JobError", "ScanJobManager"]

ACTIVE_STATUSES = {"starting", "running", "stopping"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ScanJobManager:
    """Bounded, independently isolated E2B scans."""

    def __init__(
        self, base_dir: Path, *, on_save: Callable[[dict[str, Any]], None] | None = None
    ) -> None:
        if os.name != "posix":
            raise JobError("Local scan management currently requires Linux or macOS.")
        if getattr(sys, "frozen", False):
            raise JobError("Use a Python installation (uv/pip) for local scan management.")
        import fcntl  # noqa: PLC0415 - unavailable on Windows; read-only viewers still work there

        self.base_dir = base_dir.resolve()
        if self.base_dir.name != "natsuhat_runs":
            raise JobError("Scan management requires a natsuhat_runs directory.")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._owner = (self.base_dir / ".manager.lock").open("a")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._owner.close()
            raise JobError("Another viewer already manages scans in this directory.", 409) from exc
        try:
            self.llm_profiles = LlmProfiles.for_workspace(self.base_dir.parent)
            self.runner_settings = RunnerSettings(self.base_dir.parent)
            self.sources = SourceStore(self.base_dir.parent)
            self.github = GithubConnection(self.base_dir.parent, self.sources)
        except BaseException:
            self._owner.close()
            raise
        # Hosted publication/restore share this lock with job lifecycle changes.
        self.lock = threading.RLock()
        self._closed = False
        self._jobs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._watchers: list[threading.Thread] = []
        self.on_save = on_save
        for path in self.base_dir.glob("*/job.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("id") != path.parent.name:
                continue
            self._jobs[job["id"]] = job
            if job["status"] in ACTIVE_STATUSES:
                # Never signal a PID read from disk: it could have been reused.
                job.update(
                    status="interrupted", end_time=_now(), error="Viewer exited unexpectedly."
                )
                self._save(job)
        for job in self._jobs.values():
            remote = self._runner_state(job["id"]).get("remote", {})
            if job.get("runner") == "e2b" and remote.get("cleanup") not in RETAINED:
                self._launch(job, cleanup=True)

    def _save(self, job: dict[str, Any]) -> None:
        run_dir = self.base_dir / job["id"]
        atomic_write_text(run_dir / "job.json", json.dumps(job, indent=2))
        # The CLI owns run.json while alive; finalize only after it has exited.
        if job["status"] in ACTIVE_STATUSES:
            if self.on_save:
                self.on_save(job)
            return
        record_path = run_dir / "run.json"
        record = (
            json.loads(record_path.read_text())
            if record_path.exists()
            else {
                "run_id": job["id"],
                "run_name": job["id"],
                "targets_info": [{"original": job["target"]}],
                "scan_mode": job["scan_mode"],
                "start_time": job["start_time"],
            }
        )
        if record.get("status") != "completed":
            record.update(status=job["status"], end_time=job["end_time"])
        atomic_write_text(record_path, json.dumps(record, indent=2))
        if self.on_save:
            self.on_save(job)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            result: list[dict[str, Any]] = []
            for job in sorted(self._jobs.values(), key=lambda j: j["start_time"], reverse=True):
                reason = self._resume_error(job)
                result.append(
                    {
                        **job,
                        "has_run": (self.base_dir / job["id"] / "run.json").is_file(),
                        "can_resume": reason is None,
                        "resume_unavailable_reason": reason,
                        **self._runner_state(job["id"]),
                    }
                )
            return result

    def load_restored_job(self, name: str) -> None:
        """Register a durable job after its files have been restored by the host."""
        with self.lock:
            job = json.loads((self.base_dir / name / "job.json").read_text())
            if job["id"] != name:
                raise JobError("Stored job identity does not match its directory.")
            self._jobs[name] = job

    def _runner_state(self, name: str) -> dict[str, Any]:
        path = self.base_dir / name / "runner.json"
        return {"remote": json.loads(path.read_text())} if path.exists() else {}

    def _resume_error(self, job: dict[str, Any]) -> str | None:  # noqa: PLR0911 - explicit eligibility reasons
        if job.get("runner") != "e2b":
            return "This runner is no longer supported. Start a new E2B scan instead."
        if job["status"] not in {"failed", "stopped", "interrupted"}:
            return "Only failed, stopped, or interrupted scans can be resumed."
        directory = self.base_dir / job["id"]
        try:
            record = json.loads((directory / "run.json").read_text())
            checkpoint = json.loads((directory / ".state" / "agents.json").read_text())
        except (OSError, ValueError):
            return "No readable agent checkpoint is available. Start a new scan instead."
        if not isinstance(record, dict) or not isinstance(checkpoint, dict):
            return "No readable agent checkpoint is available. Start a new scan instead."
        record = cast("dict[str, Any]", record)
        checkpoint = cast("dict[str, Any]", checkpoint)
        if record.get("status") == "completed":
            return "This scan is already completed."
        parents = checkpoint.get("parent_of")
        if (
            not (directory / ".state" / "agents.db").is_file()
            or not isinstance(parents, dict)
            or not any(parent is None for parent in cast("dict[str, Any]", parents).values())
        ):
            return "No saved agent session is available. Start a new scan instead."
        remote = self._runner_state(job["id"]).get("remote", {})
        if remote.get("results_pending") or remote.get("cleanup") == "failed":
            return "Retry results or sandbox cleanup before resuming this scan."
        return (
            "The retained sandbox is unavailable. Start a new scan instead."
            if remote.get("cleanup") != "paused" or not remote.get("sandbox_id")
            else None
        )

    def configuration(self) -> dict[str, Any]:
        return {
            "runners": runners(),
            "default_runner": self.runner_settings.default(),
            "max_concurrent": remote_limit(),
            "max_remote_concurrent": remote_limit(),
            "remote_timeout_seconds": remote_timeout(),
        }

    def start(  # noqa: PLR0912 - validate and persist before launching the paid worker
        self, body: dict[str, Any], *, snapshot: str | None = None, created_by: str | None = None
    ) -> dict[str, Any]:
        source = body.get("source")
        has_source = source is not None or snapshot is not None
        target = "source snapshot" if has_source else body.get("target")
        if not isinstance(target, str) or not target.strip() or len(target) > 4096:
            raise JobError("Provide a target URL, domain, IP, or source upload.")
        target = target.strip()
        if target.startswith("-") or any(ord(c) < 32 for c in target):
            raise JobError("Invalid target.")
        if body.get("authorized") is not True:
            raise JobError("Confirm that you are authorized to test this target.")
        mode = body.get("scan_mode", "quick")
        if mode not in ("quick", "standard", "deep"):
            raise JobError("Scan mode must be quick, standard, or deep.")
        budget = body.get("max_budget_usd")
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or (budget != -1 and budget <= 0)
        ):
            raise JobError("Set an LLM budget greater than zero (USD), or -1 for unlimited.")
        instruction = body.get("instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 16000:
            raise JobError("Instructions must be text, at most 16,000 characters.")
        runner = validate_runner(
            body.get("runner", self.runner_settings.default()),
            "https://source.invalid" if has_source else target,
        )
        template = self.runner_settings.preflight(runner)
        if source is not None:
            snapshot = self._prepare_source(source)
        with self.lock:
            if self._closed:
                raise JobError("The scan manager is shutting down.", 503)
            if len(self._processes) >= remote_limit():
                raise JobError(
                    "E2B scan concurrency limit reached. Stop a scan or wait for a slot.", 409
                )
            llm = self.llm_profiles.selected()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("NATSUHAT_GITHUB_")
            }
            env.update({"NATSUHAT_DASHBOARD_LLM": json.dumps(llm)} if llm is not None else {})
            name = f"{runner}-{uuid.uuid4().hex}"
            run_dir = self.base_dir / name
            metadata = self.sources.snapshot(snapshot, name) if snapshot else None
            if metadata:
                target = (
                    f"{metadata['repository']}@{metadata['commit']}"
                    if metadata["kind"] == "github"
                    else metadata["filename"]
                )
            run_dir.mkdir(mode=0o700)
            job: dict[str, Any] = {
                "id": name,
                "target": target,
                "scan_mode": mode,
                "max_budget_usd": budget,
                "status": "starting",
                "start_time": _now(),
                "end_time": None,
                "exit_code": None,
                "error": None,
                "llm_model": llm["model"] if llm is not None else None,
                "runner": runner,
                **({"created_by": created_by} if created_by else {}),
                **({"runner_template": template} if template else {}),
                **({"source": metadata} if metadata else {}),
            }
            self._jobs[name] = job
            if instruction.strip():
                (run_dir / "instructions.txt").write_text(instruction, encoding="utf-8")
            try:
                self._save(job)
            except Exception:
                # Persistence failed before launch: do not spend provider credits.
                self._jobs.pop(name, None)
                shutil.rmtree(run_dir)
                if metadata:
                    self.sources.remove(name)
                raise
            self._launch(job, env=env)
            return dict(job)

    def _prepare_source(self, source: Any) -> str:
        if not isinstance(source, dict):
            raise JobError("Choose a GitHub repository or ZIP upload.")
        source = cast("dict[str, Any]", source)
        if source.get("kind") not in ("zip", "github"):
            raise JobError("Choose a GitHub repository or ZIP upload.")
        if source["kind"] == "github":
            return self.github.download(source.get("repository_id"), source.get("ref"))
        identifier = source.get("upload_id")
        if not isinstance(identifier, str) or not identifier.startswith("upload-"):
            raise JobError("Upload a ZIP before starting the scan.")
        return identifier

    def _launch(
        self,
        job: dict[str, Any],
        *,
        env: dict[str, str] | None = None,
        cleanup: bool = False,
        resume: bool = False,
    ) -> None:
        name = job["id"]
        run_dir = self.base_dir / name
        command = [sys.executable, "-m", "natsuhat.interface.viewer.job_runner", str(run_dir)]
        if cleanup:
            command.append("--cleanup")
            job["cleanup_only"] = True
        elif resume:
            command.append("--resume")
        if self.on_save is not None:
            env = dict(os.environ if env is None else env)
            for key in list(env):
                if key.startswith("SUPABASE_") or key in {
                    "DATABASE_URL",
                    "NATSUHAT_SESSION_ENCRYPTION_KEY",
                }:
                    env.pop(key)
            env["NATSUHAT_PUBLISH_RESULTS"] = "1"
        try:
            with (run_dir / "job.log").open("ab" if cleanup or resume else "wb") as log:
                process = subprocess.Popen(  # noqa: S603 - fixed Python module, no shell
                    command,
                    cwd=self.base_dir.parent,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    # Children retain ownership if the viewer crashes; never adopt saved PIDs.
                    pass_fds=(self._owner.fileno(),),
                )
        except OSError:
            job.update(status="failed", end_time=_now(), error="Could not start the scan process.")
            self._save(job)
            raise JobError(str(job["error"]), 503) from None
        self._processes[name] = process
        job["status"] = "stopping" if cleanup else "running"
        self._save(job)
        watcher = threading.Thread(target=self._wait, args=(name, process), daemon=True)
        self._watchers.append(watcher)
        watcher.start()

    def resume(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("authorized") is not True:
            raise JobError("Confirm that you are authorized to resume testing this target.")
        with self.lock:
            job = self._jobs.get(name)
            if job is None:
                raise JobError("Unknown local scan job.", 404)
            if reason := self._resume_error(job):
                raise JobError(reason, 409)
            if self._closed:
                raise JobError("The scan manager is shutting down.", 503)
            validate_runner(
                job.get("runner", "local"),
                "https://source.invalid" if job.get("source") else job["target"],
            )
            if len(self._processes) >= remote_limit():
                raise JobError("E2B scan concurrency limit reached. Wait for a slot.", 409)
            budget = body.get("max_budget_usd", job["max_budget_usd"])
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not math.isfinite(budget)
                or (budget != -1 and budget <= 0)
            ):
                raise JobError("Set an LLM budget greater than zero (USD), or -1 for unlimited.")
            record = json.loads((self.base_dir / name / "run.json").read_text())
            spent = record.get("llm_usage", {}).get("cost", 0)
            if budget != -1 and budget <= spent:
                raise JobError(
                    "This total LLM budget is already spent. Increase it before resuming.", 409
                )
            if job.get("source", {}).get("kind") == "github":
                self.github.repository_token(job["source"]["repository_id"])
            llm = self.llm_profiles.selected()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("NATSUHAT_GITHUB_")
            }
            env.update({"NATSUHAT_DASHBOARD_LLM": json.dumps(llm)} if llm is not None else {})
            job.update(
                status="starting",
                end_time=None,
                exit_code=None,
                error=None,
                max_budget_usd=budget,
                llm_model=llm["model"] if llm is not None else None,
                resumed_at=_now(),
            )
            self._save(job)
            self._launch(job, env=env, resume=True)
            return dict(job)

    def _wait(self, name: str, process: subprocess.Popen[bytes]) -> None:
        code = process.wait()
        with self.lock:
            job = self._jobs[name]
            record_path = self.base_dir / name / "run.json"
            record: dict[str, Any] = (
                json.loads(record_path.read_text()) if record_path.exists() else {}
            )
            status = "stopped" if job["status"] == "stopping" else "failed"
            # Exit 2 means findings only after a scan produced a terminal run
            # record. Argument/target validation also exits 2, with no run.
            if (
                job["status"] != "stopping"
                and (code in (0, 2) or record.get("status") == "interrupted")
                and record.get("status")
                in {
                    "completed",
                    "stopped",
                    "failed",
                    "interrupted",
                }
            ):
                status = record["status"]
            remote = self._runner_state(name).get("remote", {})
            if job.pop("cleanup_only", False):
                status = record.get("status", "interrupted")
                if status in ACTIVE_STATUSES:
                    status = "interrupted"
            if remote and remote.get("cleanup") not in RETAINED | {"failed"}:
                remote.update(
                    cleanup="failed",
                    phase="Cleanup unconfirmed",
                    error="Worker exited before sandbox retention was confirmed. Retry cleanup.",
                )
                atomic_write_text(self.base_dir / name / "runner.json", json.dumps(remote))
            if remote.get("cleanup") == "failed" or remote.get("results_pending"):
                status = "failed"
            job.update(status=status, exit_code=code, end_time=_now())
            if status != "failed":
                job["error"] = None
            if status == "failed":
                job["error"] = (
                    remote.get("error") or "Scan exited with an error. Check the job log."
                )
            if code == -signal.SIGKILL:
                job["error"] = (
                    "Remote worker force-stopped. Retry sandbox pause/archive cleanup."
                    if job.get("runner", "local") != "local"
                    else "Forced stop after 30s. Check Docker for leftover scan containers."
                )
            try:
                self._save(job)
            finally:
                self._processes.pop(name, None)

    def stop(self, name: str) -> dict[str, Any]:
        with self.lock:
            job = self._jobs.get(name)
            if job is None:
                raise JobError("Unknown local scan job.", 404)
            process = self._processes.get(name)
            remote = self._runner_state(name).get("remote", {})
            if process is None and (
                remote.get("cleanup") == "failed" or remote.get("results_pending")
            ):
                if job.get("runner") != "e2b":
                    raise JobError(
                        "This runner is no longer supported. Recover it with its provider."
                    )
                self._launch(job, cleanup=True)
                return dict(job)
            if process is not None and process.poll() is None and job["status"] != "stopping":
                job["status"] = "stopping"
                self._save(job)
                # Signal only the Python parent first, letting CLI cleanup tear
                # down Docker. Escalate the process group only if it cannot exit.
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                grace = 1080 if job.get("runner", "local") != "local" else 30
                threading.Thread(target=self._escalate, args=(process, grace), daemon=True).start()
            return dict(job)

    @staticmethod
    def _escalate(process: subprocess.Popen[bytes], grace: int) -> None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    def delete(self, name: str, *, before_remove: Callable[[], None] | None = None) -> None:
        """Remove terminal local results, never a retained provider sandbox."""
        with self.lock:
            directory = self.base_dir / name
            if (
                not name
                or name.startswith(".")
                or directory.name != name
                or directory.is_symlink()
                or directory.resolve().parent != self.base_dir
                or not directory.is_dir()
            ):
                raise JobError("Unknown scan.", 404)
            job = self._jobs.get(name)
            if name in self._processes or (job and job["status"] in ACTIVE_STATUSES):
                raise JobError("Stop the scan and wait for it to finish before deleting it.", 409)
            record = read_run_summary(directory)
            if not job and not record.get("finished"):
                raise JobError("Only finished scans can be deleted.", 409)
            remote = self._runner_state(name).get("remote", {})
            if remote and (remote.get("cleanup") not in RETAINED or remote.get("results_pending")):
                raise JobError("Retrieve results and finish sandbox cleanup before deleting.", 409)
            if before_remove:
                before_remove()
            shutil.rmtree(directory)
            if job and job.get("source"):
                self.sources.remove(name)
            self._jobs.pop(name, None)

    def log(self, name: str) -> str:
        with self.lock:
            if name not in self._jobs:
                raise JobError("Unknown local scan job.", 404)
        directory = self.base_dir / name
        output = ""
        for path in (directory / "job.log", directory / "remote.log"):
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 32768))
                    output += stream.read(32768).decode("utf-8", errors="replace")
        return output

    def close(self) -> None:
        with self.lock:
            self._closed = True
            for name in list(self._processes):
                self.stop(name)
        deadline = time.monotonic() + 1085
        for watcher in self._watchers:
            watcher.join(timeout=max(0, deadline - time.monotonic()))
        self._owner.close()
