"""`natsuhat view [<run>]` command: serve a run's viewer UI locally."""

from __future__ import annotations

import argparse
import logging
import signal
import time
from typing import TYPE_CHECKING

from rich.console import Console

from natsuhat.core.paths import (
    RUNS_DIR_NAME,
    latest_run_dir,
    run_dir_for,
    run_record_path,
    runs_base_dir,
)
from natsuhat.interface.viewer.jobs import JobError
from natsuhat.interface.viewer.server import authorized_url, bundle_is_built, serve
from natsuhat.interface.viewer.transcript import read_run_summary


if TYPE_CHECKING:
    from pathlib import Path
    from typing import NoReturn


logger = logging.getLogger(__name__)


def run_view(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="natsuhat view",
        description="Open a local web view of a Natsuhat run (live or finished).",
    )
    parser.add_argument(
        "run",
        nargs="?",
        default=None,
        help=f"Run name under ./{RUNS_DIR_NAME} (defaults to the most recent run).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to serve on (default: an available ephemeral port).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1; use 0.0.0.0 for all IPv4 interfaces).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    parser.add_argument(
        "--manage-scans",
        action="store_true",
        help="Allow the authorized dashboard to start/stop local scans (one at a time).",
    )
    args = parser.parse_args(argv)

    console = Console()

    if not bundle_is_built():
        console.print(
            "[bold red]Viewer UI is not built.[/]\n"
            "Build it with: [cyan]make viewer[/] from a Natsuhat checkout."
        )
        raise SystemExit(1)

    # A managing dashboard must also work before the first scan exists.
    run_dir = (
        latest_run_dir() or runs_base_dir() / ".dashboard"
        if args.manage_scans and not args.run
        else _resolve_run_dir(args.run, console)
    )

    try:
        httpd, url, token = serve(
            run_dir,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            manage_scans=args.manage_scans,
        )
    except JobError as exc:
        console.print(str(exc), style="bold red", markup=False)
        raise SystemExit(1) from exc
    # The tokened URL is what authorizes the browser (steering, report sending,
    # history). Print it rather than the bare URL so the operator -- and only
    # the operator -- can open or share an authorized link.
    open_url = authorized_url(url, token)

    run_name = run_dir.name
    summary = read_run_summary(run_dir)
    live = bool(summary.get("run_id") or summary.get("run_name")) and not summary.get(
        "finished", False
    )

    from natsuhat.telemetry import posthog

    posthog.viewer_opened(source="cli", live=live)

    state_label = _state_label(summary)
    console.print()
    if not run_record_path(run_dir).is_file():
        console.print("Serving the local scan dashboard at:")
    else:
        console.print(f"Serving [bold white]{run_name}[/] ({state_label}) at:")
    # Print the URL alone on its own line with soft_wrap so Rich never inserts a
    # wrap into the (long, tokened) link -- that keeps it selectable/copyable.
    console.print(f"  [#60a5fa]{open_url}[/]", soft_wrap=True)
    console.print("[dim]This link authorizes the browser; anyone you share it with can steer[/]")
    console.print("[dim]a live scan and browse history. Press Ctrl-C to stop the viewer.[/]")
    if args.manage_scans:
        console.print("[yellow]Scan management enabled: this link can launch paid local scans.[/]")
        console.print("[dim]Stopping this viewer also stops its active scan.[/]")
    console.print()

    previous_sigterm = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]Viewer stopped.[/]")
    finally:
        httpd.shutdown()
        httpd.server_close()
        signal.signal(signal.SIGTERM, previous_sigterm)


def _state_label(summary: dict[str, object]) -> str:
    if not summary.get("finished", False):
        return "[#eab308]live[/]"

    status = summary.get("status")
    if status == "failed":
        return "[#ef4444]failed[/]"
    if status in {"stopped", "interrupted"}:
        return f"[#eab308]{status}[/]"
    return "[#22c55e]finished[/]"


def _resolve_run_dir(run: str | None, console: Console) -> Path:
    if run:
        run_dir = run_dir_for(run)
        if not run_record_path(run_dir).is_file():
            _fail_no_run(console, requested=run)
        return run_dir

    latest = latest_run_dir()
    if latest is None:
        _fail_no_run(console, requested=None)
    return latest


def _fail_no_run(console: Console, *, requested: str | None) -> NoReturn:
    base = runs_base_dir()
    available = (
        sorted(
            (child.name for child in base.iterdir() if run_record_path(child).is_file()),
            reverse=True,
        )
        if base.is_dir()
        else []
    )

    if requested:
        console.print(f"[bold red]No run named '{requested}' under ./{RUNS_DIR_NAME}.[/]")
    else:
        console.print(f"[bold red]No runs found under ./{RUNS_DIR_NAME}.[/]")

    if available:
        console.print("Available runs:")
        for name in available[:20]:
            console.print(f"  [cyan]{name}[/]")
    raise SystemExit(1)


__all__ = ["run_view"]
