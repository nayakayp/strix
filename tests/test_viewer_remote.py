"""Remote worker process and result-transfer contracts, without paid services."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from typing import TYPE_CHECKING

import pytest

from natsuhat.interface.viewer import remote, remote_snapshot


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("manifest", ["ready", "outdated", "missing", "recover"])
def test_prepared_runner_scans_without_installing_and_rejects_stale_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str
) -> None:
    controller, vm_root = tmp_path / "controller", tmp_path / "vm"
    controller.mkdir()
    job = {"id": "prepared-scan", "runner": "e2b", "target": "https://authorized.test"}
    (controller / "job.json").write_text(json.dumps(job))
    package = vm_root / "natsuhat/natsuhat/interface/viewer"
    package.mkdir(parents=True)
    for directory in (package, package.parent, package.parent.parent):
        (directory / "__init__.py").touch()
    shutil.copyfile(remote_snapshot.__file__, package / "remote_snapshot.py")
    # A synthetic scan engine isolates model billing, not preparation: the real
    # controller, process supervisor, filesystem permissions and result transfer run.
    (package / "job_runner.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "directory=Path(sys.argv[1])\n"
        "job=json.loads((directory/'job.json').read_text())\n"
        "with (directory/'launches').open('a') as log: log.write('launched\\n')\n"
        "(directory/'run.json').write_text(json.dumps({'status':'completed',"
        "'target':job['target']}))\nprint('PREPARED_SCAN_EXECUTED')\nsys.exit(2)\n"
    )
    python = vm_root / "natsuhat/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setenv(
        "NATSUHAT_DASHBOARD_LLM", json.dumps({"model": "synthetic", "api_key": "private-test-key"})
    )

    class Sandbox:
        id = "prepared-vm"
        available = manifest != "recover"

        def create(self) -> None:
            pass

        def find(self) -> bool:
            return True

        def restore(self) -> None:
            pass

        def retain(self) -> str:
            return "paused"

        def resume(self) -> bool:
            return False

        def upload(self, path: str, data: bytes) -> None:
            (vm_root / path.removeprefix("/home/runner/")).write_bytes(data)

        def download(self, path: str) -> bytes:
            if not self.available:
                raise ConnectionError("Synthetic result transfer outage")
            return (vm_root / path.removeprefix("/home/runner/")).read_bytes()

        # The short fixture launch is synchronous, including background requests.
        def command(self, command: str, *, background: bool = False) -> str:  # noqa: ARG002
            if command == "cat /opt/natsuhat-runner.json":
                if manifest == "missing":
                    raise FileNotFoundError("No prepared image manifest")
                return json.dumps(
                    {
                        "revision": remote.runner_revision()
                        if manifest in {"ready", "recover"}
                        else "old"
                    }
                )
            if "docker image inspect" in command:
                return ""  # Only the provider and Docker daemon are substituted.
            command = command.replace("/home/runner", str(vm_root)).replace(
                "python3 ", f"{sys.executable} -I -S ", 1
            )
            return subprocess.run(  # noqa: S603 - trusted controller shell in disposable fixture
                ["/bin/bash", "-c", command], check=True, capture_output=True, text=True
            ).stdout

    sandbox = Sandbox()
    monkeypatch.setattr(remote, "RemoteSandbox", lambda *_: sandbox)
    worker = remote.RemoteRun(controller)
    if manifest not in {"ready", "recover"}:
        with pytest.raises(remote.JobError, match="No per-scan installation"):
            worker.execute()
        assert not (vm_root / "llm.json").exists()
        assert not list(vm_root.rglob("run.json"))
        return
    if manifest == "recover":
        monkeypatch.setattr(remote.signal, "signal", lambda *_: None)
        monkeypatch.setattr(remote.time, "sleep", lambda _: None)
        monkeypatch.setattr(worker.stop, "wait", lambda _: False)
        assert worker.run() == 1
        state = json.loads((controller / "runner.json").read_text())
        assert state["results_pending"] and state["cleanup"] == "paused"
        assert not (controller / "run.json").exists()
        sandbox.available = True
        assert remote.RemoteRun(controller).run(cleanup_only=True) == 130
        assert not json.loads((controller / "runner.json").read_text())["results_pending"]
    else:
        assert worker.execute() == 2
    assert json.loads((controller / "run.json").read_text()) == {
        "status": "completed",
        "target": "https://authorized.test",
    }
    assert (vm_root / "natsuhat/natsuhat_runs/prepared-scan/launches").read_text() == "launched\n"
    assert (vm_root / "llm.json").stat().st_mode & 0o777 == 0o600
    assert not (vm_root / "source.tar.gz").exists()
    assert not (vm_root / "natsuhat/uv.lock").exists()


def test_runner_revision_ignores_archive_metadata_but_detects_dependency_changes() -> None:
    def bundle(*, modified: bool, reversed_order: bool) -> bytes:
        output = io.BytesIO()
        files = [("app.py", b"application"), ("uv.lock", b"new" if modified else b"original")]
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, data in reversed(files) if reversed_order else files:
                member = tarfile.TarInfo(name)
                member.size, member.mtime = len(data), 100 if reversed_order else 200
                archive.addfile(member, io.BytesIO(data))
        return output.getvalue()

    first = remote.runner_revision(bundle(modified=False, reversed_order=False))
    assert first == remote.runner_revision(bundle(modified=False, reversed_order=True))
    assert first != remote.runner_revision(bundle(modified=True, reversed_order=True))


@pytest.mark.parametrize(
    "reboot,interruption", [(False, None), (True, None), (True, "connect"), (True, "boot_read")]
)
def test_remote_process_bootstraps_without_application_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reboot: bool, interruption: str | None
) -> None:
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "job.json").write_text('{"id": "bootstrap-test"}')
    vm = tmp_path / "vm"
    helper = vm / "natsuhat/natsuhat/interface/viewer/remote_snapshot.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(remote_snapshot.__file__, helper)

    class Sandbox:
        paused = False
        rebooted = False
        interrupted = False

        def resume(self) -> bool:
            paused, self.paused = self.paused, False
            self.rebooted = self.rebooted or (paused and reboot)
            if paused and interruption == "connect" and not self.interrupted:
                self.interrupted = True
                raise ConnectionError("Server resumed but its response was lost")
            return paused

        def command(self, command: str, *, background: bool = False) -> str:
            if self.paused:
                raise ConnectionError("Synthetic VM is paused")
            if self.rebooted and command == "cat /proc/sys/kernel/random/boot_id":
                if interruption == "boot_read" and not self.interrupted:
                    self.interrupted = True
                    raise ConnectionError("First boot-ID read was lost")
                return "filesystem-only-reboot"
            # Execute the real controller's shell command in an isolated VM-shaped
            # filesystem. -I -S makes application dependencies unavailable.
            command = command.replace("/home/runner", str(vm)).replace(
                "python3 ", f"{sys.executable} -I -S ", 1
            )
            output = subprocess.run(  # noqa: S603 - trusted controller commands only
                ["/bin/bash", "-c", command], check=True, capture_output=True, text=True
            ).stdout
            if background:
                self.paused = True
            return output

    worker = remote.RemoteRun(controller)
    monkeypatch.setattr(worker, "sandbox", Sandbox())
    monkeypatch.setattr(worker.stop, "wait", lambda _: False)
    if reboot:
        with pytest.raises(remote.JobError, match="rebooted instead of restoring process memory"):
            worker.phase("Setup", [sys.executable, "-c", "exit(7)"])
        return
    assert (
        worker.phase("Setup", [sys.executable, "-c", "print('quoted input: a; b'); exit(7)"]) == 7
    )
    assert "quoted input: a; b" in (controller / "remote.log").read_text()
    assert worker.state["resume_count"] == 1
    # A launch failure must settle, not poll a missing exit marker for an hour.
    assert worker.phase("Missing executable", ["/nonexistent-runner-executable"]) != 0


def test_remote_snapshot_preserves_live_transcripts_and_excludes_controller_files(
    tmp_path: Path,
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    (source / ".state").mkdir(parents=True)
    (source / "vulnerabilities").mkdir()
    (source / "run.json").write_text('{"status":"running"}')
    (source / "vulnerabilities/issue.md").write_text("# Finding\nReproduction details")
    (source / "job.json").write_text("controller metadata must not be transferred")
    (source / "instructions.txt").write_text("private instructions")
    (source / "vulnerabilities/linked.md").symlink_to(source / "instructions.txt")
    destination.mkdir()
    (destination / "job.json").write_text("keep controller metadata")
    database = source / ".state/agents.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE messages (text TEXT)")
        connection.execute("INSERT INTO messages VALUES ('live transcript from WAL')")
        connection.commit()
        snapshot = tmp_path / "results.tar.gz"
        subprocess.run(  # noqa: S603 - real stdlib-only helper, local fixture paths
            [
                sys.executable,
                "-I",
                "-S",
                remote_snapshot.__file__,
                "snapshot",
                str(source),
                str(snapshot),
            ],
            check=True,
        )
        remote.import_results(snapshot.read_bytes(), destination)
    assert json.loads((destination / "run.json").read_text())["status"] == "running"
    assert (destination / "vulnerabilities/issue.md").read_text().endswith("Reproduction details")
    assert (destination / "job.json").read_text() == "keep controller metadata"
    assert not (destination / "instructions.txt").exists()
    assert not (destination / "vulnerabilities/linked.md").exists()
    with sqlite3.connect(destination / ".state/agents.db") as received:
        assert received.execute("SELECT text FROM messages").fetchall() == [
            ("live transcript from WAL",)
        ]
    (source / "vulnerabilities/issue.md").unlink()
    (source / "vulnerabilities/retained.md").write_text("Still valid")
    remote_snapshot.snapshot(source, snapshot)
    remote.import_results(snapshot.read_bytes(), destination)
    assert not (destination / "vulnerabilities/issue.md").exists()
    assert (destination / "vulnerabilities/retained.md").read_text() == "Still valid"
    assert (destination / "job.json").read_text() == "keep controller metadata"


@pytest.mark.parametrize("name", ["../outside", "/run.json", "job.json", ".state/../../run.json"])
def test_remote_archive_rejects_controller_overwrite_and_traversal(
    tmp_path: Path, name: str
) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for filename in ("run.json", name):
            info = tarfile.TarInfo(filename)
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="Unexpected"):
        remote.import_results(payload.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_remote_archive_rejects_links(tmp_path: Path) -> None:
    for link in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            info = tarfile.TarInfo("run.json")
            info.type, info.linkname = link, "/etc/passwd"
            archive.addfile(info)
        with pytest.raises(ValueError, match="Unexpected"):
            remote.import_results(payload.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []
