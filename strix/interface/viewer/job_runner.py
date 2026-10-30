"""Child-only entrypoint for the local scan manager; never imported by the server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from strix.config import load_settings
from strix.interface.main import main


def run_job(run_dir: Path) -> None:
    """Reuse CLI setup, sandbox lifecycle, and exit semantics with a reserved ID."""
    if snapshot := os.environ.pop("STRIX_DASHBOARD_LLM", None):
        settings = load_settings()
        settings.llm = settings.llm.model_copy(update=json.loads(snapshot))
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    sys.argv = [
        "strix",
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
