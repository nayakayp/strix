"""Run the hot-reloading viewer against disposable, synthetic scan output."""  # noqa: INP001

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from rich.console import Console

from strix.interface.viewer import auth
from strix.interface.viewer.server import SESSION_COOKIE_PREFIX, serve


def main() -> None:
    # Supervised service stops must clean up the child process and demo directory too.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    root = Path(__file__).resolve().parents[1]
    frontend = root / "strix" / "interface" / "viewer" / "frontend"
    npm = shutil.which("npm")
    if npm is None or not (frontend / "node_modules").is_dir():
        raise SystemExit(
            "Install Node.js 24 and run: npm --prefix strix/interface/viewer/frontend ci"
        )

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

        os.environ["STRIX_TELEMETRY"] = "false"
        auth.AUTH_PATH = Path(temporary) / "viewer-auth.json"
        httpd, backend_url, token = serve(run_dir, open_browser=False)
        try:
            env = os.environ.copy()
            env["STRIX_VIEWER_DEV_BACKEND"] = backend_url
            # Only the Vite server receives the capability; it never enters browser JS or logs.
            port = httpd.server_address[1]
            env["STRIX_VIEWER_DEV_COOKIE"] = f"{SESSION_COOKIE_PREFIX}_{port}={token}"
            if public_url := env.get("PUBLIC_URL"):
                env["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"] = urlsplit(public_url).hostname or ""
            Console().print("Viewer development: synthetic data only; no scan or LLM required.")
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
