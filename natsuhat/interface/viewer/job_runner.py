"""Child-only entrypoint for the local scan manager; never imported by the server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from natsuhat.config import load_settings
from natsuhat.interface.main import main


def run_job(run_dir: Path) -> None:
    """Reuse CLI setup, sandbox lifecycle, and exit semantics with a reserved ID."""
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    if job.get("runner", "local") != "local":
        from natsuhat.interface.viewer.remote import RemoteRun  # noqa: PLC0415

        raise SystemExit(RemoteRun(run_dir).run(cleanup_only="--cleanup" in sys.argv[2:]))
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
        f"--target={job['target']}",
        "--scan-mode",
        job["scan_mode"],
    ]
    # The engine already represents an uncapped budget as None (flag omitted).
    if job["max_budget_usd"] != -1:
        sys.argv.extend(["--max-budget", str(job["max_budget_usd"])])
    instructions = run_dir / "instructions.txt"
    if instructions.exists():
        sys.argv.extend(["--instruction-file", str(instructions)])
    main(run_name=job["id"], save_config=False)


if __name__ == "__main__":
    run_job(Path(sys.argv[1]).resolve())
