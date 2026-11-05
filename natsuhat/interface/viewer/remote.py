"""One scan per remote VM; pull results, never expose a dashboard callback.

Provider credentials stay on the controller. Application dependencies and tools
are baked into a versioned image; only the job and selected LLM connection are
staged. Idle E2B sandboxes are paused, never deleted.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shlex
import signal
import tarfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import natsuhat
from natsuhat.config import load_settings
from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.remote_snapshot import is_result
from natsuhat.interface.viewer.sources import SourceStore
from natsuhat.report.writer import atomic_write_text
from natsuhat.telemetry.redaction import provider_secrets, redact_text


if TYPE_CHECKING:
    from types import FrameType


logger = logging.getLogger(__name__)
REMOTE_ROOT = "/home/runner/natsuhat"
SDK_TIMEOUT = 30
RETAINED = {"deleted", "not_created", "paused", "archived"}
DEFAULT_E2B_TEMPLATE = "nayakas-project/natsuhat-runner:stable"


def remote_limit() -> int:
    return max(1, min(16, int(os.getenv("NATSUHAT_REMOTE_MAX_CONCURRENT", "10"))))


def remote_timeout() -> int:
    return max(300, min(86400, int(os.getenv("NATSUHAT_REMOTE_TIMEOUT_SECONDS", "14400"))))


def runners() -> list[dict[str, Any]]:
    return [
        {"id": "e2b", "name": "E2B", "configured": bool(os.getenv("E2B_API_KEY"))},
    ]


def validate_runner(provider: Any, target: str) -> str:
    if not isinstance(provider, str) or provider != "e2b":
        raise JobError("Dashboard scans use E2B only.")
    if not os.getenv("E2B_API_KEY"):
        raise JobError("Set E2B_API_KEY on the dashboard server first.")
    try:
        parsed = urlsplit(target if "://" in target else "//" + target)
    except ValueError:
        raise JobError("Invalid remote target URL.") from None
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or (parsed.scheme and parsed.scheme not in {"http", "https"})
        or target.startswith(("/", ".", "~"))
    ):
        raise JobError("E2B scans accept URLs, domains, IPs, or source uploads, not local paths.")
    return provider


class RemoteSandbox:
    """An E2B sandbox owned by one scan controller."""

    def __init__(self, provider: str, job_id: str, template: str | None = None) -> None:
        if provider != "e2b":
            raise JobError("This sandbox runner is no longer supported. Dashboard scans use E2B.")
        self.job_id = job_id
        self.template = template
        self.resource: Any = None
        self.id: str | None = None

    def create(self) -> None:
        from e2b import Sandbox  # noqa: PLC0415 - SDK only in remote worker

        self.resource = Sandbox.create(
            self.template or DEFAULT_E2B_TEMPLATE,
            timeout=min(3600, remote_timeout()),
            lifecycle={"on_timeout": "pause", "auto_resume": False},
            metadata={"natsuhat_job": self.job_id},
            idempotency_key=self.job_id,
            request_timeout=90,
        )
        self.id = str(self.resource.sandbox_id)

    def find(self) -> bool:
        """Recover only our labelled VM, including an ambiguous create response."""
        from e2b import Sandbox, SandboxQuery  # noqa: PLC0415

        matches = Sandbox.list(
            query=SandboxQuery(metadata={"natsuhat_job": self.job_id}),
            request_timeout=SDK_TIMEOUT,
        ).next_items(request_timeout=SDK_TIMEOUT)
        if not matches:
            return False
        self.id = matches[0].sandbox_id
        return True

    def resume(self) -> bool:
        """Resume the same VM, without replaying its launch or renewing running sessions."""
        from e2b import Sandbox  # noqa: PLC0415

        assert self.id is not None
        if Sandbox.get_info(self.id, request_timeout=SDK_TIMEOUT).state != "paused":
            return False
        self.resource = Sandbox.connect(
            self.id, timeout=min(3600, remote_timeout()), request_timeout=90
        )
        return True

    def restore(self) -> None:
        """Reconnect a retained VM for file recovery, never create another VM."""
        if not self.find():
            raise JobError("The retained sandbox could not be found; results remain pending.")
        from e2b import Sandbox  # noqa: PLC0415

        assert self.id is not None
        self.resource = Sandbox.connect(self.id, timeout=300, request_timeout=90)

    def command(self, command: str, *, background: bool = False) -> str:
        result = self.resource.commands.run(
            command,
            background=background,
            timeout=remote_timeout() if background else SDK_TIMEOUT,
            request_timeout=SDK_TIMEOUT + 5,
        )
        return "" if background else str(result.stdout)

    def upload(self, path: str, data: bytes) -> None:
        self.resource.files.write(path, data, request_timeout=SDK_TIMEOUT)

    def download(self, path: str) -> bytes:
        return bytes(self.resource.files.read(path, format="bytes", request_timeout=SDK_TIMEOUT))

    def retain(self) -> str:
        # Look up by our unique label/name; never wake a VM merely to stop it.
        if not self.find():
            return "not_created"
        assert self.id is not None
        from e2b import Sandbox  # noqa: PLC0415

        Sandbox.pause(self.id, request_timeout=90)
        if Sandbox.get_info(self.id, request_timeout=SDK_TIMEOUT).state != "paused":
            raise RuntimeError("Sandbox pause is unconfirmed.")
        return "paused"


def source_bundle() -> bytes:
    """Build inputs for the image, never uploaded when submitting a scan.

    Wheels carry the same locked build inputs under _runner; editable installs
    use the checkout. No Go binary or frontend toolchain is needed headlessly.
    """
    package = Path(natsuhat.__file__).resolve().parent
    checkout = package.parent
    inputs = checkout if (checkout / "uv.lock").is_file() else package / "_runner"
    required = (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "scripts/tui_sidecar_hook.py",
        "scripts/docker.sh",
        "containers/Dockerfile",
        "containers/docker-entrypoint.sh",
        "containers/runner/Dockerfile",
        "containers/runner/start.sh",
        "containers/runner/prepare.sh",
    )
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name in required:
            archive.add(inputs / name, arcname=name, recursive=False)
        excluded = {"frontend", "static", "bin", "_runner", "__pycache__"}
        for directory, directories, files in package.walk():
            directories[:] = [name for name in directories if name not in excluded]
            for name in files:
                path = directory / name
                if (
                    name not in excluded
                    and path.is_file()
                    and not path.is_symlink()
                    and path.suffix
                    in {".py", ".md", ".jinja", ".j2", ".json", ".yaml", ".yml", ".txt"}
                ):
                    relative = path.relative_to(package)
                    archive.add(path, arcname="natsuhat/" + relative.as_posix(), recursive=False)
    return result.getvalue()


def runner_revision(bundle: bytes | None = None) -> str:
    """Identify application/build contents, independent of tar timestamps and owners."""
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(bundle or source_bundle()), mode="r:gz") as archive:
        contents: list[tuple[str, bytes]] = []
        for member in archive:
            stream = archive.extractfile(member)
            assert stream is not None
            contents.append((member.name, hashlib.sha256(stream.read()).digest()))
        for name, checksum in sorted(contents):
            digest.update(name.encode() + b"\0" + checksum)
    return digest.hexdigest()


def import_results(payload: bytes, run_dir: Path) -> None:
    """Never allow a remote archive to overwrite controller state or follow links."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 256 * 1024 * 1024:
            raise ValueError("Remote results exceed the 256 MiB snapshot limit.")
        for member in members:
            if not member.isfile() or not is_result(member.name) or ".." in Path(member.name).parts:
                raise ValueError("Unexpected remote result archive member.")
        for member in members:
            stream = archive.extractfile(member)
            assert stream is not None
            path = run_dir / member.name
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Result destination must not be a symbolic link.")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".sync")
            temporary.write_bytes(stream.read())
            temporary.replace(path)
        received = {member.name for member in members}
        # Snapshots are complete: omitted results were withdrawn on the worker.
        # Reconcile only after every member was validated and imported.
        for path in run_dir.rglob("*"):
            name = path.relative_to(run_dir).as_posix()
            if (
                is_result(name)
                and name not in received
                and path.is_file()
                and not path.is_symlink()
                and not path.parent.is_symlink()
            ):
                path.unlink()


class RemoteRun:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.job: dict[str, Any] = json.loads((run_dir / "job.json").read_text())
        self.stop = threading.Event()
        self.sandbox: RemoteSandbox | None = None
        state_file = run_dir / "runner.json"
        self.state: dict[str, Any] = (
            json.loads(state_file.read_text())
            if state_file.exists()
            else {
                "phase": "Provisioning sandbox",
                "cleanup": "pending",
            }
        )
        self.redactions: list[str] = []
        self.deadline = time.monotonic() + remote_timeout()
        self.root = REMOTE_ROOT
        self.helper = f"python3 {self.root}/natsuhat/interface/viewer/remote_snapshot.py"

    def save(self, **fields: Any) -> None:
        self.state.update(fields)
        atomic_write_text(self.run_dir / "runner.json", json.dumps(self.state))

    def sync(self) -> None:
        assert self.sandbox is not None
        remote_dir = f"{self.root}/natsuhat_runs/{self.job['id']}"
        output = "/home/runner/results.tar.gz"
        self.sandbox.command(f"{self.helper} snapshot {remote_dir} {output}")
        archive = self.sandbox.download(output)
        import_results(archive, self.run_dir)
        if os.environ.get("NATSUHAT_PUBLISH_RESULTS") == "1":
            # The parent publishes these complete, validated generations. The
            # worker has no database or bucket credentials, even during retries.
            pending = self.run_dir / ".cloud-pending"
            pending.mkdir(mode=0o700, exist_ok=True)
            temporary = pending / "receiving.tmp"
            temporary.write_bytes(archive)
            temporary.replace(pending / f"{time.time_ns():020}.tar.gz")

    def resume(self) -> None:
        assert self.sandbox is not None
        if self.sandbox.resume():
            self.save(resume_count=self.state.get("resume_count", 0) + 1)
        # A connect response or first boot-ID read can be lost after server-side
        # resume. Verify on every poll, including when the VM is already running.
        if (
            self.state.get("boot_id")
            and self.state["boot_id"]
            != self.sandbox.command("cat /proc/sys/kernel/random/boot_id").strip()
        ):
            raise JobError(
                "E2B restored files but rebooted instead of restoring process memory. "
                "The scan was not restarted; existing results and the sandbox are retained."
            )

    def phase(self, name: str, command: list[str], *, results: bool = False) -> int:
        assert self.sandbox is not None
        self.save(phase=name)
        phase_id = "scan" if results else "setup"
        self.resume()
        self.save(boot_id=self.sandbox.command("cat /proc/sys/kernel/random/boot_id").strip())
        self.sandbox.command(
            f"rm -f /home/runner/{phase_id}.exit; (cd {self.root} && {self.helper} "
            f"launch /home/runner {phase_id} {shlex.join(command)}) "
            f">/home/runner/{phase_id}.bootstrap.log 2>&1; code=$?; "
            f"test -f /home/runner/{phase_id}.exit || echo $code >/home/runner/{phase_id}.exit",
            background=True,
        )
        errors = 0
        while True:
            if self.stop.is_set() or time.monotonic() >= self.deadline:
                self.resume()
                self.sandbox.command(f"{self.helper} stop /home/runner")
                if results:
                    grace = time.monotonic() + 30
                    while time.monotonic() < grace:
                        if self.sandbox.command(
                            "cat /home/runner/scan.exit 2>/dev/null || true"
                        ).strip():
                            break
                        time.sleep(2)
                    self.sync()
                if not self.stop.is_set():
                    self.save(error="Remote time limit reached, including setup time.")
                raise InterruptedError("Scan stopped or its remote time limit was reached.")
            try:
                self.resume()
                code = self.sandbox.command(
                    f"cat /home/runner/{phase_id}.exit 2>/dev/null || true"
                ).strip()
                tail = self.sandbox.command(
                    f"tail -c 16384 /home/runner/{phase_id}.log "
                    f"/home/runner/{phase_id}.bootstrap.log 2>/dev/null || true"
                )
                tail = redact_text(tail, self.redactions)
                atomic_write_text(self.run_dir / "remote.log", tail)
                if results:
                    self.sync()
                self.save(sync_error=None, last_sync=datetime.now(UTC).isoformat())
                errors = 0
                if code:
                    return int(code)
            except (JobError, InterruptedError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001 - retry read-only status/snapshot requests
                errors += 1
                self.save(sync_error=f"Connection interrupted ({type(exc).__name__}); retrying.")
                if errors >= 6:
                    raise RuntimeError(
                        "Remote runner remained unreachable; sandbox retention attempted."
                    ) from None
            self.stop.wait(5)

    def check_prepared_runner(self) -> None:
        assert self.sandbox is not None
        self.save(phase="Checking prepared runner")
        try:
            manifest = json.loads(self.sandbox.command("cat /opt/natsuhat-runner.json"))
            release = re.fullmatch(r"[^:]+:rev-([a-f0-9]{64})", self.job.get("runner_template", ""))
            ready = bool(release and manifest.get("revision") == release.group(1))
            if ready:
                self.sandbox.command(
                    f"test -x {self.root}/.venv/bin/python && "
                    f"test -f {self.root}/natsuhat/interface/viewer/job_runner.py && "
                    f"test -f {self.root}/natsuhat/interface/viewer/remote_snapshot.py && "
                    "docker image inspect natsuhat-sandbox:local >/dev/null"
                )
        except Exception:  # noqa: BLE001 - reject stale images before uploading credentials
            ready = False
        if not ready:
            raise JobError(
                "The sandbox is incomplete or does not match the pinned stable release. "
                "Verify the published E2B template. No per-scan installation is performed."
            ) from None

    def execute(self, *, resume: bool = False) -> int:
        settings = load_settings()
        override = json.loads(os.environ.pop("NATSUHAT_DASHBOARD_LLM", "{}"))
        connection = settings.llm.model_copy(update=override).model_dump()
        if not connection.get("api_key") or not connection.get("model"):
            raise JobError("Remote scans require a configured API-key LLM connection.")
        self.redactions = provider_secrets(connection)
        self.sandbox = RemoteSandbox(
            self.job["runner"], self.job["id"], self.job.get("runner_template")
        )
        if resume:
            self.save(phase="Restoring retained sandbox")
            self.sandbox.restore()
            if self.sandbox.id != self.state.get("sandbox_id"):
                raise JobError("The retained sandbox does not match this scan. Resume refused.")
            if not self.sandbox.command("cat /home/runner/scan.exit 2>/dev/null || true").strip():
                raise JobError(
                    "The previous scan process has no confirmed exit. "
                    "Retrieve results and stop it before resuming."
                )
            # A checkpoint restart does not require the old process memory.
            # Subsequent automatic VM resumes must preserve this new boot.
            self.save(boot_id=self.sandbox.command("cat /proc/sys/kernel/random/boot_id").strip())
        else:
            self.sandbox.create()
            self.save(sandbox_id=self.sandbox.id)
        if self.stop.is_set():
            raise InterruptedError("Scan cancelled during provisioning.")
        if not resume:
            self.check_prepared_runner()
        remote_dir = f"{self.root}/natsuhat_runs/{self.job['id']}"
        self.sandbox.command(f"mkdir -p {remote_dir}")
        self.sandbox.upload(
            remote_dir + "/job.json", json.dumps({**self.job, "runner": "local"}).encode()
        )
        instructions = self.run_dir / "instructions.txt"
        if instructions.exists():
            self.sandbox.upload(remote_dir + "/instructions.txt", instructions.read_bytes())
        source_env: list[str] = []
        if self.job.get("source"):
            archive = (
                SourceStore(self.run_dir.parent.parent).directory(self.job["id"]) / "source.zip"
            )
            self.sandbox.upload("/home/runner/source.zip", archive.read_bytes())
            self.sandbox.command("chmod 600 /home/runner/source.zip")
            source_env = ["NATSUHAT_DASHBOARD_SOURCE_PATH=/home/runner/source.zip"]
        self.sandbox.upload("/home/runner/llm.json", json.dumps(connection).encode())
        self.sandbox.command("chmod 600 /home/runner/llm.json")
        worker = ["-m", "natsuhat.interface.viewer.job_runner", remote_dir]
        if resume:
            # Older retained images already support CLI checkpoints but their
            # dashboard entrypoint lacks --resume. Stage only this adapter;
            # retain the VM's engine, dependencies, checkpoints and tool data.
            self.sandbox.upload(
                "/home/runner/resume_job.py", Path(__file__).with_name("job_runner.py").read_bytes()
            )
            worker = ["/home/runner/resume_job.py", remote_dir, "--resume"]
        self.save(results_pending=True, cleanup="pending", error=None)
        code = self.phase(
            "Resuming scan" if resume else "Scanning",
            [
                "env",
                "NATSUHAT_IMAGE=natsuhat-sandbox:local",
                "NATSUHAT_TELEMETRY=false",
                "NATSUHAT_DASHBOARD_LLM_PATH=/home/runner/llm.json",
                *source_env,
                f"{self.root}/.venv/bin/python",
                *worker,
            ],
            results=True,
        )
        self.save(phase="Receiving results")
        self.sync()
        self.save(results_pending=False, sync_error=None)
        return code

    def collect_results(self, *, restore: bool = False) -> None:
        """Retrieve this job's files without ever replaying its scan launch."""
        assert self.sandbox is not None
        if restore:
            self.sandbox.restore()
        self.sandbox.resume()
        # A paused E2B VM may still contain an unfinished scan. Stop that existing
        # process before collecting; never signal its saved PID after a reboot.
        boot = self.sandbox.command("cat /proc/sys/kernel/random/boot_id").strip()
        if (
            boot == self.state.get("boot_id")
            and not self.sandbox.command("cat /home/runner/scan.exit 2>/dev/null || true").strip()
        ):
            self.sandbox.command(f"{self.helper} stop /home/runner")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.sandbox.command("cat /home/runner/scan.exit 2>/dev/null || true").strip():
                    break
                time.sleep(1)
        self.sync()
        self.save(results_pending=False, sync_error=None, last_sync=datetime.now(UTC).isoformat())

    def cleanup(self) -> bool:
        if self.sandbox is None:
            self.save(cleanup="not_created")
            return True
        if self.state.get("results_pending"):
            for _ in range(3):
                try:
                    self.collect_results()
                except Exception as exc:  # noqa: BLE001 - retention must still run
                    self.save(sync_error=f"Results pending ({type(exc).__name__}). Retry results.")
                    time.sleep(2)
                else:
                    break
        self.save(phase="Pausing sandbox")
        for _ in range(3):
            try:
                retained = self.sandbox.retain()
            except Exception:  # noqa: BLE001 - retry retention, never retry scan creation
                time.sleep(2)
            else:
                self.save(cleanup=retained, sandbox_id=self.sandbox.id)
                return True
        self.save(
            cleanup="failed",
            phase="Cleanup failed",
            error="Sandbox pause unconfirmed. Retry cleanup or check E2B.",
        )
        return False

    def cancel(self, _number: int, _frame: FrameType | None) -> None:
        self.stop.set()

    def run(self, *, cleanup_only: bool = False, resume: bool = False) -> int:
        signal.signal(signal.SIGTERM, self.cancel)
        signal.signal(signal.SIGINT, self.cancel)
        code = 130 if cleanup_only else 1
        self.save()
        try:
            if cleanup_only:
                self.sandbox = RemoteSandbox(self.job["runner"], self.job["id"])
                if self.state.get("results_pending"):
                    self.collect_results(restore=True)
            else:
                code = self.execute(resume=resume)
        except InterruptedError:
            code = 130
        except Exception as exc:  # noqa: BLE001 - sanitize SDK errors and always stop the VM
            logger.error("Remote scan failed (%s)", type(exc).__name__)  # noqa: TRY400 - no credentials in tracebacks
            self.save(
                error=str(exc)
                if isinstance(exc, JobError)
                else (
                    f"Remote execution failed during {self.state['phase']} ({type(exc).__name__}). "
                    "Check the runner log and provider availability."
                )
            )
            code = 1
        finally:
            if self.cleanup():
                phase = {130: "Stopped", 1: "Failed"}.get(code, "Finished")
                if cleanup_only:
                    if not self.state.get("results_pending"):
                        self.save(phase="Recovery finished", error=None)
                else:
                    self.save(phase=phase)
            else:
                code = 1
        return code
