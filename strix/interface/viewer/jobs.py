"""Single-host scan jobs owned by an explicitly enabled local viewer.

Each child runs the normal headless CLI in a separate process. Job metadata and
logs live with the run; shutdown stops children rather than leaving scans behind.
The owner lock prevents two managing viewers from scanning the same workspace.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from strix.interface.viewer.llm_settings import JobError, LlmProfiles
from strix.report.writer import atomic_write_text


if TYPE_CHECKING:
    from pathlib import Path


__all__ = ["JobError", "ScanJobManager"]

ACTIVE_STATUSES = {"starting", "running", "stopping"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ScanJobManager:
    """Run at most one scan at a time, retaining job state across viewer restarts."""

    def __init__(self, base_dir: Path) -> None:
        if os.name != "posix":
            raise JobError("Local scan management currently requires Linux or macOS.")
        if getattr(sys, "frozen", False):
            raise JobError("Use a Python installation (uv/pip) for local scan management.")
        import fcntl  # noqa: PLC0415 - unavailable on Windows; read-only viewers still work there

        self.base_dir = base_dir.resolve()
        if self.base_dir.name != "strix_runs":
            raise JobError("Scan management requires a strix_runs directory.")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._owner = (self.base_dir / ".manager.lock").open("a")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._owner.close()
            raise JobError("Another viewer already manages scans in this directory.", 409) from exc
        try:
            self.llm_profiles = LlmProfiles.for_workspace(self.base_dir.parent)
        except BaseException:
            self._owner.close()
            raise
        self._lock = threading.RLock()
        self._closed = False
        self._jobs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._watchers: list[threading.Thread] = []
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

    def _save(self, job: dict[str, Any]) -> None:
        run_dir = self.base_dir / job["id"]
        atomic_write_text(run_dir / "job.json", json.dumps(job, indent=2))
        # The CLI owns run.json while alive; finalize only after it has exited.
        if job["status"] in ACTIVE_STATUSES:
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

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**job, "has_run": (self.base_dir / job["id"] / "run.json").is_file()}
                for job in sorted(self._jobs.values(), key=lambda j: j["start_time"], reverse=True)
            ]

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        target = body.get("target")
        if not isinstance(target, str) or not target.strip() or len(target) > 4096:
            raise JobError("Provide a target URL, domain, IP, repository, or local path.")
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
        with self._lock:
            if self._closed:
                raise JobError("The scan manager is shutting down.", 503)
            if self._processes:
                raise JobError("A scan is already active. Stop it or wait for it to finish.", 409)
            llm = self.llm_profiles.selected()
            env = os.environ.copy()
            if llm is not None:
                env["STRIX_DASHBOARD_LLM"] = json.dumps(llm)
            name = f"local-{uuid.uuid4().hex}"
            run_dir = self.base_dir / name
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
            }
            self._jobs[name] = job
            self._save(job)
            if instruction.strip():
                (run_dir / "instructions.txt").write_text(instruction, encoding="utf-8")
            command = [sys.executable, "-m", "strix.interface.viewer.job_runner", str(run_dir)]
            try:
                with (run_dir / "job.log").open("wb") as log:
                    process = subprocess.Popen(  # noqa: S603 - fixed Python module, no shell
                        command,
                        cwd=self.base_dir.parent,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        # Keep ownership locked even if the viewer crashes while
                        # its child still runs. Never adopt arbitrary saved PIDs.
                        pass_fds=(self._owner.fileno(),),
                    )
            except OSError:
                job.update(
                    status="failed", end_time=_now(), error="Could not start the scan process."
                )
                self._save(job)
                raise JobError(str(job["error"]), 503) from None
            self._processes[name] = process
            job["status"] = "running"
            self._save(job)
            watcher = threading.Thread(target=self._wait, args=(name, process), daemon=True)
            self._watchers.append(watcher)
            watcher.start()
            return dict(job)

    def rerun(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(name)
            if job is None:
                raise JobError("Unknown local scan job.", 404)
            if job["status"] != "failed":
                raise JobError("Only failed scan jobs can be rerun.", 409)
            instructions = self.base_dir / name / "instructions.txt"
            return self.start(
                {
                    "target": job["target"],
                    "scan_mode": job["scan_mode"],
                    "max_budget_usd": job["max_budget_usd"],
                    "instruction": instructions.read_text(encoding="utf-8")
                    if instructions.exists()
                    else "",
                    "authorized": body.get("authorized"),
                }
            )

    def _wait(self, name: str, process: subprocess.Popen[bytes]) -> None:
        code = process.wait()
        with self._lock:
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
            job.update(status=status, exit_code=code, end_time=_now())
            if status == "failed":
                job["error"] = "Scan exited with an error. Check the local job log."
            if code == -signal.SIGKILL:
                job["error"] = "Forced stop after 30s. Check Docker for leftover scan containers."
            try:
                self._save(job)
            finally:
                self._processes.pop(name, None)

    def stop(self, name: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(name)
            if job is None:
                raise JobError("Unknown local scan job.", 404)
            process = self._processes.get(name)
            if process is not None and process.poll() is None and job["status"] != "stopping":
                job["status"] = "stopping"
                self._save(job)
                # Signal only the Python parent first, letting CLI cleanup tear
                # down Docker. Escalate the process group only if it cannot exit.
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                threading.Thread(target=self._escalate, args=(process,), daemon=True).start()
            return dict(job)

    @staticmethod
    def _escalate(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    def log(self, name: str) -> str:
        with self._lock:
            if name not in self._jobs:
                raise JobError("Unknown local scan job.", 404)
        path = self.base_dir / name / "job.log"
        if not path.exists():
            return ""
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            return stream.read(32768).decode("utf-8", errors="replace")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for name in list(self._processes):
                self.stop(name)
        for watcher in self._watchers:
            watcher.join(timeout=35)
        self._owner.close()
