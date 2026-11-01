"""Runner-side process supervision and consistent, allowlisted result snapshots.

This module uses only the standard library, including during dependency setup.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


RESULT_FILES = {
    "run.json",
    "vulnerabilities.json",
    "vulnerabilities.csv",
    "coverage.json",
    "findings.sarif",
    "penetration_test_report.md",
    ".state/agents.json",
    ".state/coverage.json",
    ".state/agents.db",
}


def is_result(name: str) -> bool:
    path = Path(name)
    return name in RESULT_FILES or (
        path.parent.as_posix() == "vulnerabilities"
        and path.suffix == ".md"
        and path.name not in {".", ".."}
    )


def snapshot(run_dir: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary, tarfile.open(output, "w:gz") as archive:
        for path in run_dir.rglob("*"):
            name = path.relative_to(run_dir).as_posix()
            if not is_result(name) or not path.is_file() or path.is_symlink():
                continue
            if name == ".state/agents.db":
                backup = Path(temporary) / "agents.db"
                with (
                    sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source,
                    sqlite3.connect(backup) as destination,
                ):
                    source.backup(destination)
                archive.add(backup, arcname=name)
            else:
                # Atomic writers may replace a JSON file, but cannot truncate
                # the inode tarfile has already opened.
                archive.add(path, arcname=name)


def launch(root: Path, phase: str, command: list[str]) -> None:
    with (root / f"{phase}.log").open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 - trusted controller supplies argv
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        (root / "process.pid").write_text(str(process.pid))
        code = process.wait()
    (root / f"{phase}.exit").write_text(str(code))


def stop(root: Path) -> None:
    path = root / "process.pid"
    if path.exists():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(int(path.read_text()), signal.SIGTERM)


if __name__ == "__main__":
    action, directory, *args = sys.argv[1:]
    base = Path(directory)
    if action == "snapshot":
        snapshot(base, Path(args[0]))
    elif action == "launch":
        launch(base, args[0], args[1:])
    elif action == "stop":
        stop(base)
    else:
        raise SystemExit("Unknown remote operation")
