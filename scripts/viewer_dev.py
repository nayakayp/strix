"""Hot-reloading viewer: synthetic preview, or opt-in local scan management."""  # noqa: INP001

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from rich.console import Console

from strix.core.paths import latest_run_dir, runs_base_dir
from strix.interface.viewer import auth
from strix.interface.viewer.server import SESSION_COOKIE_PREFIX, serve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manage-scans",
        action="store_true",
        help="Use real local history and LLM settings; allow explicitly authorized scans.",
    )
    args = parser.parse_args()
    # Supervised service stops must clean up the child process and demo directory too.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    os.environ["STRIX_TELEMETRY"] = "false"
    if args.manage_scans:
        if os.environ.get("AMP_ORB") == "1":
            _ensure_orb_docker()
        _serve(latest_run_dir() or runs_base_dir() / ".dashboard", manage_scans=True)
        return

    # Never write demo records among real scan results or start a scan.
    with TemporaryDirectory(prefix="strix-viewer-demo-") as temporary:
        run_dir = Path(temporary) / "demo-ui-only"
        state_dir = run_dir / ".state"
        state_dir.mkdir(parents=True)
        record = {
            "run_id": "demo-ui-only",
            "run_name": "Demo — UI development only",
            "status": "completed",
            "scan_mode": "quick",
            "start_time": "2026-01-01T10:00:00Z",
            "end_time": "2026-01-01T10:05:00Z",
            "targets_info": [{"original": "https://demo.example.test", "type": "url"}],
            "instructions": "Synthetic UI fixture. No target was scanned.",
            "scan_results": {
                "executive_summary": (
                    "This is synthetic development data, not a security assessment. "
                    "Use the issues and agent graph to develop the dashboard locally."
                ),
                "recommendations": "Replace these fixtures with real run data for scan review.",
            },
        }
        findings = [
            {
                "id": "demo-authorization",
                "title": "[DEMO] Missing object-level authorization",
                "severity": "high",
                "timestamp": record["end_time"],
                "description": "Synthetic finding for developing the issue detail view.",
                "impact": "In this fictional example, a user could read another user's record.",
                "endpoint": "/api/projects/42",
                "method": "GET",
                "cwe": "CWE-639",
                "remediation_steps": "Check record ownership before returning project data.",
            },
            {
                "id": "demo-header",
                "title": "[DEMO] Missing Content-Security-Policy header",
                "severity": "low",
                "timestamp": record["end_time"],
                "description": "Synthetic finding for exercising severity filters.",
                "remediation_steps": "Define an application-specific Content-Security-Policy.",
            },
        ]
        agents = {
            "statuses": {"demo-root": "completed", "demo-api": "completed"},
            "names": {"demo-root": "Demo coordinator", "demo-api": "Demo API reviewer"},
            "parent_of": {"demo-root": None, "demo-api": "demo-root"},
        }
        for path, value in (
            (run_dir / "run.json", record),
            (run_dir / "vulnerabilities.json", findings),
            (state_dir / "agents.json", agents),
        ):
            path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        (run_dir / "penetration_test_report.md").write_text(
            "# Demo report\n\nSynthetic UI data only. No scan was performed.\n",
            encoding="utf-8",
        )

        for days_ago, name, target, status, previous_findings in (
            (1, "demo-api", "https://api.example.test", "completed", findings[:1]),
            (2, "demo-shop", "https://shop.example.test", "stopped", []),
        ):
            previous = Path(temporary) / name
            previous.mkdir()
            previous_record = {
                **record,
                "run_id": name,
                "run_name": f"[DEMO] {name}",
                "status": status,
                "start_time": f"2025-12-{32 - days_ago}T10:00:00Z",
                "end_time": f"2025-12-{32 - days_ago}T10:05:00Z",
                "targets_info": [{"original": target, "type": "url"}],
            }
            (previous / "run.json").write_text(json.dumps(previous_record), encoding="utf-8")
            (previous / "vulnerabilities.json").write_text(
                json.dumps(previous_findings), encoding="utf-8"
            )
            modified = (run_dir / "run.json").stat().st_mtime - days_ago * 86400
            os.utime(previous / "run.json", (modified, modified))

        auth.AUTH_PATH = Path(temporary) / "viewer-auth.json"
        _serve(run_dir)


def _docker_ready(docker: str) -> bool:
    try:
        return (
            subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
                [docker, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def _ensure_orb_docker() -> None:
    docker, amp = shutil.which("docker"), shutil.which("amp")
    if docker is None or amp is None:
        raise SystemExit(
            "Docker and Amp must be in PATH. Rerun .agents/setup if Docker is missing."
        )
    if _docker_ready(docker):
        return
    Console().print("Starting the supervised Docker service; waiting for its API…")
    subprocess.run(  # noqa: S603 - resolved Amp executable, fixed service name
        [amp, "orb", "service", "restart", "docker"], check=True, timeout=15
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _docker_ready(docker):
            return
        time.sleep(0.5)
    raise SystemExit(
        "Docker did not become ready. Check `amp orb service logs docker` and DOCKER_HOST. "
        "This orb provider must support running Docker containers."
    )


def _serve(run_dir: Path, *, manage_scans: bool = False) -> None:
    root = Path(__file__).resolve().parents[1]
    frontend = root / "strix" / "interface" / "viewer" / "frontend"
    npm = shutil.which("npm")
    if npm is None or not (frontend / "node_modules").is_dir():
        raise SystemExit(
            "Install Node.js 24 and run: npm --prefix strix/interface/viewer/frontend ci"
        )
    httpd, backend_url, token = serve(run_dir, open_browser=False, manage_scans=manage_scans)
    try:
        env = os.environ.copy()
        env["STRIX_VIEWER_DEV_BACKEND"] = backend_url
        # Only the Vite server receives the capability; it never enters browser JS or logs.
        port = httpd.server_address[1]
        env["STRIX_VIEWER_DEV_COOKIE"] = f"{SESSION_COOKIE_PREFIX}_{port}={token}"
        if public_url := env.get("PUBLIC_URL"):
            env["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"] = urlsplit(public_url).hostname or ""
        Console().print(
            "Dashboard development: real local data; scan management enabled. "
            "Starting scans can incur LLM charges. No scan starts automatically."
            if manage_scans
            else "Viewer development: synthetic data only; no scan or LLM required."
        )
        subprocess.run(  # noqa: S603 - fixed npm command, resolved executable
            [
                npm,
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                env.get("PORT", "5173"),
                "--strictPort",
            ],
            cwd=frontend,
            env=env,
            check=True,
        )
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
