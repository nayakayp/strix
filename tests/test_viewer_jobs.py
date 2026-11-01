"""Local job HTTP contracts, using real child processes without scans or LLM calls."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from natsuhat.interface.viewer import jobs, llm_settings
from natsuhat.interface.viewer.llm_settings import LlmProfiles
from natsuhat.interface.viewer.server import SESSION_COOKIE_PREFIX, serve
from natsuhat.utils.secret_files import dashboard_profiles_path, dashboard_secrets_dir


if TYPE_CHECKING:
    from pathlib import Path


REQUEST = {
    "target": "https://authorized.example.test",
    "scan_mode": "standard",
    "max_budget_usd": 3.75,
    "authorized": True,
    "runner": "local",
}

# Only the scan process is replaced. HTTP auth, persistence, process waiting,
# signals and server shutdown all execute their real implementations.
CHILD = """
import json, os, signal, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
job = json.loads((directory / 'job.json').read_text())
if '--cleanup' in sys.argv[2:]:
    state = {
        'cleanup': 'paused' if job['runner'] == 'e2b' else 'archived',
        'phase': 'Cleanup finished',
    }
    (directory / 'runner.json').write_text(json.dumps(state))
    sys.exit(130)
print('Synthetic child: ' + job['target'], flush=True)
if 'NATSUHAT_DASHBOARD_LLM' in os.environ:
    snapshot = json.loads(os.environ['NATSUHAT_DASHBOARD_LLM'])
    print('LLM configuration: ' + json.dumps({
        'model': snapshot['model'],
        'input': snapshot['input_cost_per_mtok'],
        'output': snapshot['output_cost_per_mtok'],
        'cached_input': snapshot['cached_input_cost_per_mtok'],
        'has_api_key': bool(snapshot['api_key']),
    }), flush=True)
if 'hold' in job['target']:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    while True:
        time.sleep(0.02)
time.sleep(0.1)
code = int(job['target'].rsplit('/', 1)[-1])
if '/cleanup-pending/' in job['target']:
    (directory / 'runner.json').write_text(json.dumps({'cleanup': 'pending', 'phase': 'Scanning'}))
if '/results-pending/' in job['target']:
    (directory / 'runner.json').write_text(json.dumps({
        'cleanup': 'paused' if job['runner'] == 'e2b' else 'archived',
        'phase': 'Failed', 'results_pending': True,
    }))
if '/exit/' in job['target']:
    record = {'run_name': job['id'], 'status': 'completed', 'end_time': '2026-01-01T12:00:00Z'}
    (directory / 'run.json').write_text(json.dumps(record))
sys.exit(code)
"""


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("dashboard-home")))


@pytest.fixture
def child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    popen = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        args = command[-2:] if command[-1] == "--cleanup" else command[-1:]
        return popen([sys.executable, "-u", "-c", CHILD, *args], **kwargs)

    monkeypatch.setattr(jobs.subprocess, "Popen", launch)


def request(
    url: str, path: str, cookie: str = "", body: Any = None, **headers: str
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - loopback test server only
        url + path,
        data=data,
        headers={"Cookie": cookie, "Content-Type": "application/json", **headers},
    )
    try:
        response = urllib.request.urlopen(req, timeout=3)  # noqa: S310
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def settled(manager: jobs.ScanJobManager, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = next(j for j in manager.list() if j["id"] == name)
        if job["status"] not in jobs.ACTIVE_STATUSES:
            return job
        time.sleep(0.02)
    raise AssertionError("Child did not settle")


@pytest.mark.usefixtures("child_process")
def test_job_http_lifecycle_and_guards(tmp_path: Path) -> None:
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        assert request(url, "/api/capabilities")[1]["can_manage_scans"] is False
        assert request(url, "/api/capabilities", cookie)[1]["can_manage_scans"] is True
        for path, body in (
            ("/api/jobs", None),
            ("/api/jobs", REQUEST),
            ("/api/jobs/nope/log", None),
            ("/api/jobs/nope/stop", {}),
            ("/api/jobs/nope/rerun", {"authorized": True}),
        ):
            assert request(url, path, body=body)[0] == 403
        assert request(url, "/api/jobs", cookie, REQUEST, Origin="https://evil.test")[0] == 403
        assert (
            request(url, "/api/jobs", cookie, REQUEST, **{"Content-Type": "text/plain"})[0] == 415
        )
        assert request(url, "/api/jobs", cookie, [REQUEST])[0] == 400
        assert request(url, "/api/jobs", cookie, {**REQUEST, "instruction": "x" * 33000})[0] == 413
        for invalid in (
            {"authorized": False},
            {"authorized": "true"},
            {"max_budget_usd": 0},
            {"max_budget_usd": -2},
            {"max_budget_usd": -0.5},
            {"max_budget_usd": float("inf")},
            {"max_budget_usd": float("nan")},
            {"max_budget_usd": True},
            {"scan_mode": "arbitrary"},
            {"target": "--config=/tmp/x"},
        ):
            assert request(url, "/api/jobs", cookie, {**REQUEST, **invalid})[0] == 400
        assert request(url, "/api/jobs/nope/stop", cookie, {})[0] == 404
        assert request(url, "/api/jobs/nope/rerun", cookie, {"authorized": True})[0] == 404
        assert request(url, "/api/jobs/../job.json/log", cookie)[0] == 404

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(
                    lambda _: request(
                        url,
                        "/api/jobs",
                        cookie,
                        {**REQUEST, "target": "https://hold.example.test", "max_budget_usd": -1},
                    ),
                    range(2),
                )
            )
        assert sorted(status for status, _ in replies) == [202, 409]
        job = next(body["job"] for status, body in replies if status == 202)
        name = job["id"]
        assert job["max_budget_usd"] == -1
        assert (
            json.loads((tmp_path / "natsuhat_runs" / name / "job.json").read_text())[
                "max_budget_usd"
            ]
            == -1
        )
        assert request(url, "/api/jobs", cookie)[1]["jobs"][0]["id"] == name
        assert request(url, f"/api/jobs/{name}/rerun", cookie, {"authorized": True})[0] == 409
        assert request(url, f"/api/jobs/{name}/stop", cookie, {})[0] == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = request(url, "/api/jobs", cookie)[1]["jobs"][0]
            if job["status"] == "stopped":
                break
            time.sleep(0.02)
        assert job["status"] == "stopped"
        assert job["end_time"] is not None
        assert request(url, f"/api/jobs/{name}/stop", cookie, {})[1]["job"]["status"] == "stopped"
        assert request(url, f"/api/jobs/{name}/rerun", cookie, {"authorized": True})[0] == 409
        assert request(url, f"/api/run?run={name}", cookie)[1]["finished"] is True
        assert request(url, "/api/runs", cookie)[1]["runs"][0]["name"] == name
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    ("exit_code", "has_report", "status"),
    [(0, True, "completed"), (2, True, "completed"), (1, False, "failed"), (2, False, "failed")],
)
@pytest.mark.usefixtures("child_process")
def test_job_exit_history_and_logs(
    tmp_path: Path, exit_code: int, has_report: bool, status: str
) -> None:
    base = tmp_path / "natsuhat_runs"
    manager = jobs.ScanJobManager(base)
    try:
        phase = "exit" if has_report else "usage"
        job = manager.start(
            {**REQUEST, "target": f"https://authorized.example.test/{phase}/{exit_code}"}
        )
        result = settled(manager, job["id"])
        assert result["status"] == status
        assert result["exit_code"] == exit_code
        assert result["has_run"] is True
        assert f"/{phase}/{exit_code}" in manager.log(job["id"])
        record = json.loads((base / job["id"] / "run.json").read_text())
        assert record["status"] == status
        if status == "completed":
            with pytest.raises(jobs.JobError, match="Only failed") as error:
                manager.rerun(job["id"], {"authorized": True})
            assert error.value.status == 409
    finally:
        manager.close()
    restored = jobs.ScanJobManager(base)
    try:
        assert restored.list() == [result]
    finally:
        restored.close()


@pytest.mark.parametrize("runner", ["local", "e2b"])
@pytest.mark.parametrize("budget", [3.75, -1])
@pytest.mark.usefixtures("child_process")
def test_failed_job_rerun_preserves_inputs_and_original_after_restart(
    tmp_path: Path, budget: float, runner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("NATSUHAT_REMOTE_MAX_CONCURRENT", "1")
    base = tmp_path / "natsuhat_runs"
    instruction = "Test object ownership.\nDo not test payments."
    manager = jobs.ScanJobManager(base)
    try:
        original = manager.start(
            {
                **REQUEST,
                "target": "https://authorized.example.test/usage/1",
                "max_budget_usd": budget,
                "instruction": instruction,
                "runner": runner,
            }
        )
        assert settled(manager, original["id"])["status"] == "failed"
        if runner != "local":
            (base / original["id"] / "runner.json").write_text('{"cleanup": "deleted"}')
    finally:
        manager.close()
    original_files = {p.name: p.read_bytes() for p in (base / original["id"]).iterdir()}

    httpd, url, token = serve(base / ".dashboard", manage_scans=True, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    path = f"/api/jobs/{original['id']}/rerun"
    try:
        for body in ({}, {"authorized": False}, {"authorized": "true"}):
            assert request(url, path, cookie, body)[0] == 400
        assert (
            request(url, path, cookie, {"authorized": True}, Origin="https://evil.test")[0] == 403
        )
        assert request(url, "/api/jobs", cookie)[1]["jobs"][0]["id"] == original["id"]
        assert httpd.job_manager is not None
        held = httpd.job_manager.start(
            {**REQUEST, "target": "https://hold.example.test", "runner": runner}
        )
        assert request(url, path, cookie, {"authorized": True})[0] == 409
        httpd.job_manager.stop(held["id"])
        assert settled(httpd.job_manager, held["id"])["status"] == "stopped"
        code, result = request(
            url,
            path,
            cookie,
            {"authorized": True, "target": "https://other.test", "max_budget_usd": 99},
        )
        assert code == 202
        rerun = result["job"]
        assert rerun["id"] != original["id"]
        assert rerun["target"] == "https://authorized.example.test/usage/1"
        assert rerun["scan_mode"] == "standard"
        assert rerun["max_budget_usd"] == budget
        assert rerun["runner"] == runner
        assert (base / rerun["id"] / "instructions.txt").read_text() == instruction
        assert settled(httpd.job_manager, rerun["id"])["status"] == "failed"
        assert {p.name: p.read_bytes() for p in (base / original["id"]).iterdir()} == original_files
        assert len(request(url, "/api/jobs", cookie)[1]["jobs"]) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.usefixtures("child_process")
def test_remote_http_guards_and_independent_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "synthetic-e2b-key")
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.setenv("NATSUHAT_REMOTE_MAX_CONCURRENT", "2")
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        config = request(url, "/api/jobs", cookie)[1]
        assert config["default_runner"] == "e2b"
        assert config["max_remote_concurrent"] == 2
        assert config["max_concurrent"] == 3
        assert {r["id"]: r["configured"] for r in config["runners"]} == {
            "local": True,
            "e2b": True,
            "daytona": False,
        }
        for invalid in (
            {"runner": "daytona"},
            {"runner": "unknown"},
            {"runner": []},
            {"runner": "e2b", "target": "/server/private"},
            {"runner": "e2b", "target": "./source"},
            {"runner": "e2b", "target": "https://user:secret@host.test"},
            {"runner": "e2b", "target": "file:///etc/passwd"},
            {"runner": "e2b", "target": "https://[invalid"},
        ):
            assert request(url, "/api/jobs", cookie, {**REQUEST, **invalid})[0] == 400
        with ThreadPoolExecutor(max_workers=3) as pool:
            replies = list(
                pool.map(
                    lambda _: request(
                        url,
                        "/api/jobs",
                        cookie,
                        {
                            **REQUEST,
                            "runner": "e2b",
                            "target": "https://hold.example.test",
                        },
                    ),
                    range(3),
                )
            )
        assert sorted(code for code, _ in replies) == [202, 202, 409]
        remote_ids = {body["job"]["id"] for code, body in replies if code == 202}
        assert len(remote_ids) == 2
        code, body = request(
            url,
            "/api/jobs",
            cookie,
            {
                **REQUEST,
                "target": "https://hold.example.test",
            },
        )
        assert code == 202 and body["job"]["runner"] == "local"
        assert request(url, "/api/jobs", cookie, REQUEST)[0] == 409
        assert httpd.job_manager is not None
        for name in remote_ids:
            request(url, f"/api/jobs/{name}/stop", cookie, {})
            assert settled(httpd.job_manager, name)["status"] == "stopped"
        assert (
            next(j for j in httpd.job_manager.list() if j["id"] == body["job"]["id"])["status"]
            == "running"
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("runner,retained", [("e2b", "paused"), ("daytona", "archived")])
@pytest.mark.parametrize("pending", ["cleanup", "results"])
@pytest.mark.usefixtures("child_process")
def test_unconfirmed_remote_cleanup_can_be_retried_without_rerunning_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str, retained: str, pending: str
) -> None:
    monkeypatch.setenv(f"{runner.upper()}_API_KEY", "synthetic-test-key")
    base = tmp_path / "natsuhat_runs"
    httpd, url, token = serve(base / ".dashboard", manage_scans=True, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        code, body = request(
            url,
            "/api/jobs",
            cookie,
            {
                **REQUEST,
                "runner": runner,
                "target": f"https://authorized.test/exit/{pending}-pending/2",
            },
        )
        assert code == 202
        name = body["job"]["id"]
        assert httpd.job_manager is not None
        result = settled(httpd.job_manager, name)
        assert result["status"] == "failed"
        assert result["remote"]["cleanup"] == ("failed" if pending == "cleanup" else retained)
        if pending == "results":
            assert result["remote"]["results_pending"]
        assert request(url, f"/api/jobs/{name}/rerun", cookie, {"authorized": True})[0] == 409
        assert request(url, f"/api/jobs/{name}/stop", cookie, {})[0] == 200
        result = settled(httpd.job_manager, name)
        assert result["status"] == "completed"
        assert result["remote"]["cleanup"] == retained
        assert result["error"] is None
        assert len(request(url, "/api/jobs", cookie)[1]["jobs"]) == 1
        assert request(url, f"/api/run?run={name}", cookie)[1]["finished"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
    restored = jobs.ScanJobManager(base)
    try:
        assert restored.list()[0]["status"] == "completed"
        assert restored.list()[0]["remote"]["cleanup"] == retained
        assert restored.log(name).count("Synthetic child:") == 1
    finally:
        restored.close()


@pytest.mark.parametrize("configured,expected", [(False, "local"), (True, "e2b")])
@pytest.mark.usefixtures("child_process")
def test_default_runner_for_new_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool, expected: str
) -> None:
    monkeypatch.delenv("NATSUHAT_REMOTE_MAX_CONCURRENT", raising=False)
    if configured:
        monkeypatch.setenv("E2B_API_KEY", "synthetic-test-key")
    else:
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    manager = jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    try:
        body = {key: value for key, value in REQUEST.items() if key != "runner"}
        job = manager.start({**body, "target": "https://authorized.test/exit/2"})
        assert manager.configuration()["default_runner"] == expected
        assert manager.configuration()["max_remote_concurrent"] == 10
        assert job["runner"] == expected
        assert settled(manager, job["id"])["status"] == "completed"
    finally:
        manager.close()


@pytest.mark.usefixtures("child_process")
def test_job_shutdown_stops_child_and_owner_lock(tmp_path: Path) -> None:
    base = tmp_path / "natsuhat_runs"
    manager = jobs.ScanJobManager(base)
    try:
        with pytest.raises(jobs.JobError, match="already manages"):
            jobs.ScanJobManager(base)
        job = manager.start({**REQUEST, "target": "https://hold.example.test"})
        # Ensure the child actually started before exercising shutdown.
        deadline = time.monotonic() + 5
        while not manager.log(job["id"]) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        manager.close()
    assert manager.list()[0]["status"] == "stopped"
    assert manager.list()[0]["exit_code"] is not None
    with pytest.raises(jobs.JobError, match="shutting down"):
        manager.start(REQUEST)


def test_job_management_disabled_by_default(tmp_path: Path) -> None:
    httpd, url, token = serve(tmp_path / "natsuhat_runs" / ".dashboard", open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        assert request(url, "/api/capabilities", cookie)[1]["can_manage_scans"] is False
        assert request(url, "/api/jobs", cookie)[0] == 403
        assert request(url, "/api/jobs", cookie, REQUEST)[0] == 403
        assert request(url, "/api/llm/settings", cookie)[0] == 403
        assert request(url, "/api/llm/providers", cookie, {})[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_managing_viewer_starts_before_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    viewer_cli = importlib.import_module("natsuhat.interface.viewer.cli")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NATSUHAT_TELEMETRY", "false")

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(viewer_cli.time, "sleep", interrupt)
    viewer_cli.run_view(["--manage-scans", "--no-open"])
    assert "Serving the local scan dashboard" in capsys.readouterr().out
    assert list((tmp_path / "natsuhat_runs").glob("*/run.json")) == []
    # Shutdown released ownership, even though there was no run to open.
    manager = jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    manager.close()


@pytest.mark.parametrize(
    ("budget", "engine_budget", "rates"),
    [(3.75, 3.75, (2.75, 13.5)), (-1, None, (None, None))],
)
def test_job_worker_preserves_cli_options_and_run_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
    engine_budget: float | None,
    rates: tuple[float | None, float | None],
) -> None:
    cli_main = importlib.import_module("natsuhat.interface.main")
    cli = importlib.import_module("natsuhat.interface.cli")
    loader = importlib.import_module("natsuhat.config.loader")
    worker = importlib.import_module("natsuhat.interface.viewer.job_runner")
    config = tmp_path / "cli-config.json"
    config.write_text('{"env": {"NATSUHAT_LLM_INPUT_COST_PER_MTOK": "99"}}')
    monkeypatch.setattr(loader, "_DEFAULT_PATH", config)
    monkeypatch.setattr(loader, "_override", None)
    monkeypatch.setattr(loader, "_cached", None)
    monkeypatch.setenv(
        "NATSUHAT_DASHBOARD_LLM",
        json.dumps(
            {
                "model": "openai/test-model",
                "api_key": "synthetic-worker-key",
                "api_base": "https://provider.example.test/v1",
                "input_cost_per_mtok": rates[0],
                "output_cost_per_mtok": rates[1],
                "cached_input_cost_per_mtok": None,
            }
        ),
    )
    name = "local-worker-test"
    directory = tmp_path / "natsuhat_runs" / name
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(
        json.dumps({**REQUEST, "id": name, "max_budget_usd": budget})
    )
    (directory / "instructions.txt").write_text("Focus on object ownership.")
    monkeypatch.chdir(tmp_path)

    async def finish(args: Any) -> None:
        assert args.non_interactive is True
        assert args.max_budget_usd == engine_budget
        llm = loader.load_settings().llm
        assert llm.model == "openai/test-model"
        assert llm.api_key == "synthetic-worker-key"
        assert llm.api_base == "https://provider.example.test/v1"
        assert (llm.input_cost_per_mtok, llm.output_cost_per_mtok) == rates
        assert llm.cached_input_cost_per_mtok is None

    async def no_warmup() -> None:
        pass

    for function in (
        "setup_console_logging",
        "start_import_warmup",
        "start_background_check",
        "check_docker_installed",
        "pull_docker_image",
        "validate_environment",
        "wait_for_import_warmup",
        "telemetry_start",
    ):
        monkeypatch.setattr(cli_main, function, lambda *_: None)
    monkeypatch.setattr(cli_main, "warm_up_llm", no_warmup)
    monkeypatch.setattr(cli_main, "display_completion_message", lambda *_: None)
    monkeypatch.setattr(cli, "run_cli", finish)
    monkeypatch.setattr("natsuhat.report.state.get_global_report_state", lambda: None)
    monkeypatch.setattr(sys, "argv", ["worker"])
    worker.run_job(directory)
    record = json.loads((directory / "run.json").read_text())
    assert record["run_name"] == name
    assert record["scan_mode"] == "standard"
    assert record["targets_info"][0]["original"] == REQUEST["target"]
    assert record["instruction"] == "Focus on object ownership."
    assert config.read_text() == '{"env": {"NATSUHAT_LLM_INPUT_COST_PER_MTOK": "99"}}'


@pytest.mark.usefixtures("child_process")
def test_llm_settings_http_persistence_credentials_and_job_selection(  # noqa: PLR0915 - one HTTP lifecycle
    tmp_path: Path,
) -> None:
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    provider = {
        "name": "Local gateway",
        "kind": "custom",
        "base_url": "https://gateway.example.test/v1",
        "model": "team/model-a",
        "api_key": "synthetic-profile-key",
        "prices": {"team/model-a": {"input": 0.23, "output": 4.6, "cached_input": 0.017}},
    }
    try:
        for path, body in (
            ("/api/llm/settings", None),
            ("/api/llm/providers", provider),
            ("/api/llm/models", {"id": "nope"}),
        ):
            assert request(url, path, body=body)[0] == 403
        for headers in (
            {"Origin": "https://evil.test"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Content-Type": "text/plain"},
        ):
            assert request(url, "/api/llm/providers", cookie, provider, **headers)[0] in (403, 415)
        for invalid in (
            {"base_url": "file:///etc/passwd"},
            {"base_url": "https://user:pass@provider.test"},
            {"base_url": "https://provider.test?key=private"},
            {"name": " "},
            {"prices": {"team/model-a": {"input": -1, "output": 2}}},
            {"prices": {"team/model-a": {"input": 1}}},
            {"prices": {"team/model-a": {"input": True, "output": 2}}},
            {"prices": {"team/model-a": {"input": 1, "output": float("nan")}}},
        ):
            code, result = request(url, "/api/llm/providers", cookie, {**provider, **invalid})
            assert code == 400
            assert "synthetic-profile-key" not in json.dumps(result)
        code, result = request(url, "/api/llm/providers", cookie, provider)
        assert code == 200
        assert "synthetic-profile-key" not in json.dumps(result)
        saved = result["providers"][0]
        assert saved["has_api_key"] is True and "api_key" not in saved
        identifier = saved["id"]
        file = dashboard_profiles_path(tmp_path)
        assert not file.is_relative_to(tmp_path)
        assert not (tmp_path / ".natsuhat" / "llm-providers.json").exists()
        assert dashboard_secrets_dir().stat().st_mode & 0o777 == 0o700
        assert file.parent.stat().st_mode & 0o777 == 0o700
        assert file.stat().st_mode & 0o777 == 0o600
        assert (
            request(url, "/api/llm/select", cookie, {"id": identifier})[1]["selected_id"]
            == identifier
        )
        code, result = request(
            url,
            "/api/jobs",
            cookie,
            {**REQUEST, "target": "https://authorized.example.test/exit/0"},
        )
        assert code == 202
        assert result["job"]["llm_model"] == "openai/team/model-a"
        assert "synthetic-profile-key" not in json.dumps(result)
        metadata = (tmp_path / "natsuhat_runs" / result["job"]["id"] / "job.json").read_text()
        assert "synthetic-profile-key" not in metadata
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            log = request(url, f"/api/jobs/{result['job']['id']}/log", cookie)[1]["text"]
            if "LLM configuration: " in log:
                break
            time.sleep(0.02)
        assert json.loads(log.split("LLM configuration: ", 1)[1].splitlines()[0]) == {
            "model": "openai/team/model-a",
            "input": 0.23,
            "output": 4.6,
            "cached_input": 0.017,
            "has_api_key": True,
        }
        # Blank replacements preserve credentials; unrelated model rates survive edits.
        update = {**provider, "id": identifier, "api_key": "", "model": "team/model-b"}
        assert request(url, "/api/llm/providers", cookie, update)[0] == 200
        persisted = json.loads(file.read_text())["providers"][0]
        assert persisted["api_key"] == "synthetic-profile-key"
        assert persisted["prices"]["team/model-a"]["output"] == 4.6
        assert (
            request(
                url, "/api/llm/providers", cookie, {**update, "base_url": "https://other.test/v1"}
            )[0]
            == 400
        )
        assert (
            request(url, "/api/llm/providers", cookie, {**update, "clear_api_key": True})[1][
                "providers"
            ][0]["has_api_key"]
            is False
        )
        # Keyless local providers can change endpoints without a nonexistent key to clear.
        code, result = request(
            url, "/api/llm/providers", cookie, {**update, "base_url": "https://other.test/v1"}
        )
        assert code == 200
        assert result["providers"][0]["base_url"] == "https://other.test/v1"
        assert result["providers"][0]["has_api_key"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        restored = request(url, "/api/llm/settings", cookie)[1]
        assert restored["selected_id"] == identifier
        assert restored["providers"][0]["model"] == "team/model-b"
        assert request(url, "/api/llm/delete", cookie, {"id": identifier})[1] == {
            "providers": [],
            "selected_id": None,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("kind", ["openai", "custom", "openrouter", "anthropic", "ollama"])
def test_provider_model_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    def catalog(req: httpx.Request) -> httpx.Response:
        if kind == "anthropic":
            if (
                req.url.path != "/v1/models"
                or req.headers.get("x-api-key") != "synthetic-discovery-key"
                or req.headers.get("anthropic-version") != "2023-06-01"
            ):
                return httpx.Response(401)
            if req.url.params.get("after_id") == "model-z":
                return httpx.Response(200, json={"data": [{"id": "model-a"}], "has_more": False})
            return httpx.Response(
                200, json={"data": [{"id": "model-z"}], "has_more": True, "last_id": "model-z"}
            )
        if req.headers.get("Authorization") != "Bearer synthetic-discovery-key":
            return httpx.Response(401)
        if kind == "ollama":
            return httpx.Response(
                200 if req.url.path == "/api/tags" else 404,
                json={"models": [{"name": "model-z:latest"}, {"name": "model-a:latest"}]},
            )
        return httpx.Response(
            200 if req.url.path == "/models" else 404,
            json={
                "data": [
                    {
                        "id": "model-z",
                        "pricing": {
                            "prompt": "0.00000023",
                            "completion": "0.0000046",
                            "input_cache_read": "0.000000017",
                        },
                    },
                    {"id": "model-a"},
                ]
            },
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(catalog), **kwargs)
    )
    profiles = LlmProfiles(tmp_path / "providers.json")
    saved = profiles.update(
        "providers",
        {
            "name": "Test",
            "kind": kind,
            "base_url": "https://provider.example.test",
            "api_key": "synthetic-discovery-key",
            "model": "model-a",
        },
    )
    models = profiles.models(saved["providers"][0]["id"])
    assert [m["id"] for m in models] == (
        ["model-a:latest", "model-z:latest"] if kind == "ollama" else ["model-a", "model-z"]
    )
    if kind == "openrouter":
        assert models[1]["prices"] == pytest.approx(
            {"input": 0.23, "output": 4.6, "cached_input": 0.017}
        )
    else:
        assert all("prices" not in model for model in models)


@pytest.mark.parametrize("status", [200, 302, 401])
def test_discovery_errors_do_not_disclose_credentials_or_erase_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200 if req.url.host == "other-host.test" else status,
                    json={"data": [{"id": "redirected-model"}]}
                    if req.url.host == "other-host.test"
                    else {"error": "synthetic-private-key"},
                    headers={"Location": "https://other-host.test/models"},
                )
            ),
            **kwargs,
        ),
    )
    profiles = LlmProfiles(tmp_path / "providers.json")
    saved = profiles.update(
        "providers",
        {
            "name": "Gateway",
            "kind": "custom",
            "base_url": "https://provider.test/v1",
            "model": "manual-model",
            "api_key": "synthetic-private-key",
        },
    )
    with pytest.raises(jobs.JobError) as error:
        profiles.models(saved["providers"][0]["id"])
    assert error.value.status == 502
    assert "synthetic-private-key" not in str(error.value)
    assert profiles.public()["providers"][0]["model"] == "manual-model"


@pytest.mark.parametrize("destination", ["missing", "identical", "conflicting", "orphaned-temp"])
def test_profiles_migrate_without_losing_or_overwriting_settings(
    tmp_path: Path, destination: str
) -> None:
    legacy = tmp_path / ".natsuhat" / "llm-providers.json"
    legacy.parent.mkdir()
    data = {
        "selected_id": "b" * 32,
        "providers": [
            {
                "id": letter * 32,
                "name": f"Provider {letter}",
                "kind": "custom",
                "base_url": "https://gateway.example.test/v1",
                "api_key": f"synthetic-key-{letter}",
                "model": f"model-{letter}",
                "prices": {f"model-{letter}": {"input": 0.25, "output": 3.75}},
            }
            for letter in ("a", "b")
        ],
    }
    legacy.write_text(json.dumps(data))
    temporary = legacy.with_suffix(".json.tmp")
    if destination == "orphaned-temp":
        legacy.replace(temporary)
    else:
        temporary.write_text("synthetic-stale-key")
    host = dashboard_profiles_path(tmp_path)
    existing = {**data, "selected_id": "a" * 32} if destination == "conflicting" else data
    if destination in {"identical", "conflicting"}:
        host.parent.mkdir(parents=True)
        host.write_text(json.dumps(existing))

    if destination == "conflicting":
        with pytest.raises(jobs.JobError, match="Conflicting"):
            jobs.ScanJobManager(tmp_path / "natsuhat_runs")
        assert json.loads(host.read_text()) == existing
        assert json.loads(legacy.read_text()) == data
        assert temporary.read_text() == "synthetic-stale-key"
        # Reconciliation must be retryable: failed startup releases the owner lock.
        host.unlink()

    manager = jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    try:
        assert json.loads(host.read_text()) == data
        assert not legacy.exists()
        assert not temporary.exists()
        assert manager.llm_profiles.public()["selected_id"] == "b" * 32
        assert manager.llm_profiles.selected()["model"] == "openai/model-b"
        assert host.stat().st_mode & 0o777 == 0o600
        assert host.parent.stat().st_mode & 0o777 == 0o700
        assert dashboard_secrets_dir().stat().st_mode & 0o777 == 0o700
    finally:
        manager.close()
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    restored = jobs.ScanJobManager(alias / "natsuhat_runs")
    other = jobs.ScanJobManager(tmp_path / "other-workspace" / "natsuhat_runs")
    try:
        assert restored.llm_profiles.public()["selected_id"] == "b" * 32
        assert other.llm_profiles.public() == {"selected_id": None, "providers": []}
    finally:
        restored.close()
        other.close()


def test_failed_profile_migration_retains_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".natsuhat" / "llm-providers.json"
    legacy.parent.mkdir()
    original = '{"providers": [], "selected_id": null}'
    legacy.write_text(original)

    def disk_full(*_args: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(llm_settings, "write_secret_text", disk_full)
        with pytest.raises(OSError, match="disk full"):
            jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    assert legacy.read_text() == original
    assert not dashboard_profiles_path(tmp_path).exists()
    manager = jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    manager.close()
    assert not legacy.exists()
    assert json.loads(dashboard_profiles_path(tmp_path).read_text()) == json.loads(original)
