"""Child-only entrypoint for the local scan manager; never imported by the server."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from natsuhat.config import load_settings
from natsuhat.interface.main import main
from natsuhat.interface.viewer.sources import SourceStore, extract_source
from natsuhat.report.writer import atomic_write_text


def run_job(run_dir: Path, *, resume: bool = False) -> None:
    """Reuse CLI setup, sandbox lifecycle, and exit semantics with a reserved ID."""
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    if job.get("runner", "local") != "local":
        from natsuhat.interface.viewer.remote import RemoteRun  # noqa: PLC0415

        raise SystemExit(
            RemoteRun(run_dir).run(cleanup_only="--cleanup" in sys.argv[2:], resume=resume)
        )
    if connection_path := os.environ.pop("NATSUHAT_DASHBOARD_LLM_PATH", None):
        connection = Path(connection_path)
        os.environ["NATSUHAT_DASHBOARD_LLM"] = connection.read_text(encoding="utf-8")
        connection.unlink()
    if snapshot := os.environ.pop("NATSUHAT_DASHBOARD_LLM", None):
        settings = load_settings()
        settings.llm = settings.llm.model_copy(update=json.loads(snapshot))
    sys.argv = [
        "natsuhat",
        "-n",
        f"--resume={job['id']}" if resume else f"--target={job['target']}",
        "--scan-mode",
        job["scan_mode"],
    ]
    # The engine already represents an uncapped budget as None (flag omitted).
    if job["max_budget_usd"] != -1:
        sys.argv.extend(["--max-budget", str(job["max_budget_usd"])])
    instructions = run_dir / "instructions.txt"
    if instructions.exists() and not resume:
        sys.argv.extend(["--instruction-file", str(instructions)])
    with ExitStack() as stack:
        if job.get("source"):
            archive = (
                Path(os.environ.pop("NATSUHAT_DASHBOARD_SOURCE_PATH"))
                if "NATSUHAT_DASHBOARD_SOURCE_PATH" in os.environ
                else SourceStore(run_dir.parent.parent).directory(job["id"]) / "source.zip"
            )
            with archive.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != job["source"]["sha256"]:
                    raise ValueError("Source archive checksum mismatch.")
            temporary = Path(stack.enter_context(TemporaryDirectory(prefix="natsuhat-source-")))
            working = temporary / "code"
            extract_source(archive, working)
            if resume:
                record = run_dir / "run.json"
                data = json.loads(record.read_text())
                # Source jobs have one local-code target. Its old temporary
                # extraction was removed on stop; mount the same snapshot anew.
                target = data["targets_info"][0]
                target["original"] = str(working)
                target["details"]["target_path"] = str(working)
                atomic_write_text(record, json.dumps(data, indent=2))
            else:
                sys.argv[2] = f"--target={working}"
        try:
            main(run_name=job["id"], save_config=False)
        finally:
            record = run_dir / "run.json"
            if job.get("source") and record.exists():
                data = json.loads(record.read_text())
                data["source"] = job["source"]
                atomic_write_text(record, json.dumps(data, indent=2))


if __name__ == "__main__":
    run_job(Path(sys.argv[1]).resolve(), resume="--resume" in sys.argv[2:])
