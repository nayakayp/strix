"""Local job HTTP contracts, using real child processes without scans or LLM calls."""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from natsuhat.interface.viewer import jobs, llm_settings, runner_settings
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
    "runner": "e2b",
}
_REAL_IMAGE_METADATA = runner_settings.image_metadata

# Only the scan process is replaced. HTTP auth, persistence, process waiting,
# signals and server shutdown all execute their real implementations.
CHILD = """
import json, os, signal, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
job = json.loads((directory / 'job.json').read_text())
# Match the real worker's atomic publication while the dashboard polls state.
def write_json(name, value):
    path = directory / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)
if '--cleanup' in sys.argv[2:]:
    state = {
        'cleanup': 'paused' if job['runner'] == 'e2b' else 'archived',
        'phase': 'Cleanup finished',
    }
    write_json('runner.json', state)
    sys.exit(130)
if '--resume' in sys.argv[2:]:
    record = json.loads((directory / 'run.json').read_text())
    record.update(status='running', end_time=None)
    write_json('run.json', record)
    print('Resumed checkpoint: ' + job['id'], flush=True)
print('Synthetic child: ' + job['target'], flush=True)
if 'NATSUHAT_DASHBOARD_LLM' in os.environ:
    snapshot = json.loads(os.environ['NATSUHAT_DASHBOARD_LLM'])
    print('LLM configuration: ' + json.dumps({
        'model': snapshot['model'],
        'input': snapshot['input_cost_per_mtok'],
        'output': snapshot['output_cost_per_mtok'],
        'cached_input': snapshot['cached_input_cost_per_mtok'],
        'has_api_key': bool(snapshot['api_key']),
        'extra_header_names': list(snapshot.get('extra_headers') or {}),
        'extra_body': snapshot.get('extra_body'),
    }), flush=True)
if 'hold' in job['target']:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    while True:
        time.sleep(0.02)
time.sleep(0.1)
code = int(job['target'].rsplit('/', 1)[-1])
if '/cleanup-pending/' in job['target']:
    write_json('runner.json', {'cleanup': 'pending', 'phase': 'Scanning'})
if '/results-pending/' in job['target']:
    write_json('runner.json', {
        'cleanup': 'paused' if job['runner'] == 'e2b' else 'archived',
        'phase': 'Failed', 'results_pending': True,
    })
if '/exit/' in job['target']:
    record = {'run_name': job['id'], 'status': 'completed', 'end_time': '2026-01-01T12:00:00Z'}
    write_json('run.json', record)
if not (directory / 'runner.json').exists():
    write_json('runner.json', {'cleanup': 'paused', 'phase': 'Finished'})
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
    monkeypatch.setenv("E2B_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("NATSUHAT_REMOTE_MAX_CONCURRENT", "1")

    monkeypatch.setattr(
        runner_settings,
        "image_metadata",
        lambda _provider, name: {
            "id": "synthetic-image",
            "names": [name],
            "available": True,
            "revision": "b" * 64,
            "resolved_template": "synthetic-image:rev-" + "b" * 64,
        },
    )

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        args = command[-2:] if command[-1] in {"--cleanup", "--resume"} else command[-1:]
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
@pytest.mark.parametrize("runner", ["e2b", "local", "daytona"])
def test_resume_http_preserves_run_and_enforces_confirmation_and_capacity(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("NATSUHAT_REMOTE_MAX_CONCURRENT", "1")
    name = runner + "-saved-checkpoint"
    directory = tmp_path / "natsuhat_runs" / name
    state = directory / ".state"
    state.mkdir(parents=True)
    job = {
        **REQUEST,
        "id": name,
        "runner": runner,
        "target": "https://hold.example.test",
        "status": "stopped",
        "start_time": "2026-01-01T10:00:00Z",
        "end_time": "2026-01-01T10:05:00Z",
        "exit_code": 130,
        "error": None,
    }
    (directory / "job.json").write_text(json.dumps(job))
    record = {"status": "stopped", "llm_usage": {"cost": 1.23}}
    (directory / "run.json").write_text(json.dumps(record))
    (directory / "job.log").write_text("ORIGINAL LOG\n")
    findings = '[{"id":"vuln-0001","title":"Saved finding"}]'
    (directory / "vulnerabilities.json").write_text(findings)
    checkpoint = {"parent_of": {"root": None}, "statuses": {"root": "running"}}
    (state / "agents.json").write_text(json.dumps(checkpoint))
    with sqlite3.connect(state / "agents.db") as database:
        database.execute("CREATE TABLE history (content TEXT)")
        database.execute("INSERT INTO history VALUES ('SAVED CONVERSATION')")
    if runner == "e2b":
        (directory / "runner.json").write_text(
            json.dumps({"cleanup": "paused", "sandbox_id": "same-vm", "phase": "Stopped"})
        )
    elif runner == "daytona":
        (directory / "runner.json").write_text(json.dumps({"cleanup": "failed"}))
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    endpoint = f"/api/jobs/{name}/resume"
    manager = httpd.job_manager
    provider = manager.llm_profiles.update(
        "providers",
        {
            "name": "Resume gateway",
            "kind": "custom",
            "base_url": "https://provider.test/v1",
            "model": "team/resume",
            "extra_headers": {"X-Resume": "resume-routing-value"},
            "extra_body": {"reasoning": {"enabled": False}, "uncensored": True},
        },
    )["providers"][0]
    manager.llm_profiles.update("select", {"id": provider["id"]})
    try:
        if runner != "e2b":
            legacy = request(url, "/api/jobs", cookie)[1]["jobs"][0]
            assert legacy["status"] == "stopped" and not legacy["can_resume"]
            assert request(url, endpoint, cookie, {"authorized": True})[0] == 409
            if runner == "daytona":
                assert request(url, f"/api/jobs/{name}/stop", cookie, {})[0] == 400
            assert (directory / "job.log").read_text() == "ORIGINAL LOG\n"
            assert (directory / "vulnerabilities.json").read_text() == findings
            return
        assert request(url, "/api/jobs", cookie)[1]["jobs"][0]["can_resume"]
        assert request(url, endpoint, body={"authorized": True})[0] == 403
        assert request(url, endpoint, cookie, {})[0] == 400
        assert (
            request(url, endpoint, cookie, {"authorized": True}, Origin="https://evil.test")[0]
            == 403
        )
        assert request(url, "/api/jobs/missing/resume", cookie, {"authorized": True})[0] == 404
        for budget in (0, -2, True, float("inf")):
            assert (
                request(url, endpoint, cookie, {"authorized": True, "max_budget_usd": budget})[0]
                == 400
            )
        for budget in (1.22, 1.23):
            assert (
                request(url, endpoint, cookie, {"authorized": True, "max_budget_usd": budget})[0]
                == 409
            )
        (state / "agents.json").unlink()
        assert not request(url, "/api/jobs", cookie)[1]["jobs"][0]["can_resume"]
        assert request(url, endpoint, cookie, {"authorized": True})[0] == 409
        (state / "agents.json").write_text(json.dumps(checkpoint))
        (directory / "run.json").write_text('{"status":"completed"}')
        assert request(url, endpoint, cookie, {"authorized": True})[0] == 409
        (directory / "run.json").write_text(json.dumps(record))
        if runner == "e2b":
            saved = (directory / "runner.json").read_text()
            (directory / "runner.json").write_text(
                json.dumps({"cleanup": "paused", "results_pending": True, "sandbox_id": "same-vm"})
            )
            assert request(url, endpoint, cookie, {"authorized": True})[0] == 409
            (directory / "runner.json").write_text(saved)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(
                    lambda _: request(
                        url, endpoint, cookie, {"authorized": True, "max_budget_usd": 7.5}
                    ),
                    range(2),
                )
            )
        assert sorted(status for status, _ in replies) == [202, 409]
        resumed = next(body["job"] for status, body in replies if status == 202)
        assert resumed["id"] == name and resumed["start_time"] == job["start_time"]
        assert resumed["end_time"] is None and resumed["max_budget_usd"] == 7.5
        assert request(url, "/api/jobs", cookie)[1]["jobs"][0]["can_resume"] is False
        assert request(url, "/api/jobs", cookie, {**REQUEST, "runner": runner})[0] == 409
        deadline = time.monotonic() + 3
        while "LLM configuration: " not in (directory / "job.log").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        log = (directory / "job.log").read_text()
        assert log.startswith("ORIGINAL LOG\n") and "Resumed checkpoint" in log
        connection = json.loads(log.split("LLM configuration: ", 1)[1].splitlines()[0])
        assert connection["extra_header_names"] == ["X-Resume"]
        assert connection["extra_body"] == {"reasoning": {"enabled": False}, "uncensored": True}
        assert "resume-routing-value" not in log
        assert json.loads((directory / "run.json").read_text())["llm_usage"] == record["llm_usage"]
        assert (directory / "vulnerabilities.json").read_text() == findings
        with sqlite3.connect(state / "agents.db") as database:
            assert database.execute("SELECT content FROM history").fetchall() == [
                ("SAVED CONVERSATION",)
            ]
        assert len(list((tmp_path / "natsuhat_runs").glob("*/job.json"))) == 1
        assert request(url, f"/api/jobs/{name}/stop", cookie, {})[0] == 200
        assert settled(manager, name)["status"] == "stopped"
    finally:
        httpd.shutdown()
        httpd.server_close()


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
            ("/api/runners/settings", None),
            ("/api/runners/settings", {}),
            ("/api/runners/check", {"provider": "e2b"}),
            ("/api/jobs/nope/log", None),
            ("/api/jobs/nope/stop", {}),
            ("/api/jobs/nope/rerun", {"authorized": True}),
            ("/api/jobs/nope/resume", {"authorized": True}),
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
            {"source": {"kind": []}},
            {"source": "not-a-source"},
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


def test_runner_settings_always_use_stable_despite_legacy_and_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "private-provider-key")
    monkeypatch.setenv("NATSUHAT_E2B_TEMPLATE", "environment-template")
    base = tmp_path / "natsuhat_runs"
    httpd, url, token = serve(base / ".dashboard", manage_scans=True, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    saved = {"e2b_template": "nayakas-project/natsuhat-runner:stable"}
    try:
        initial = request(url, "/api/runners/settings", cookie)[1]
        assert initial["images"][0]["template"] == saved["e2b_template"]
        assert initial["images"][0]["selection_source"] == "release"
        assert "private-provider-key" not in json.dumps(initial)
        endpoint = "/api/runners/settings"
        assert request(url, endpoint, cookie, saved, Origin="https://evil.test")[0] == 403
        for invalid in (
            {"default_runner": []},
            {"e2b_template": "https://evil.test"},
            {"e2b_template": "a\nb"},
            {"daytona_snapshot": "obsolete"},
            {"e2b_template": 42},
            {"e2b_template": "another-template:stable"},
        ):
            assert request(url, endpoint, cookie, {**saved, **invalid})[0] == 400
        assert request(url, endpoint, cookie, saved)[0] == 200
        assert request(url, "/api/jobs", cookie)[1]["default_runner"] == "e2b"
        path = dashboard_profiles_path(tmp_path).with_name("runner-settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"e2b_template": "saved-template", "default_runner": "daytona"}))
        assert not list(base.glob("*/job.json"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    restored = jobs.ScanJobManager(base)
    try:
        state = restored.runner_settings.public()
        assert state["effective_default_runner"] == "e2b"
        assert "daytona_snapshot" not in state and "default_runner" not in state
        assert state["images"][0]["template"] == saved["e2b_template"]
        assert state["images"][0]["selection_source"] == "release"
        assert [image["id"] for image in state["images"]] == ["e2b"]
        assert state["images"][0]["check"] is None
        assert restored.configuration()["default_runner"] == "e2b"
        monkeypatch.delenv("NATSUHAT_E2B_TEMPLATE")
        default = restored.runner_settings.public()["images"][0]
        assert default["template"] == "nayakas-project/natsuhat-runner:stable"
        assert default["selection_source"] == "release"
        assert (
            runner_settings.RunnerSettings(tmp_path / "another-workspace").public()["e2b_template"]
            == saved["e2b_template"]
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    "image,availability,compatibility,blocked",
    [
        ("missing", "missing", "unverified", True),
        ("published", "available", "release_pinned", False),
        ("unversioned", "unavailable", "unverified", True),
        ("building", "unavailable", "unverified", True),
        ("error", "error", "unverified", True),
        ("no-key", "unconfigured", "unverified", True),
    ],
)
@pytest.mark.usefixtures("child_process")
def test_runner_preflight_pins_current_stable_without_matching_local_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image: str,
    availability: str,
    compatibility: str,
    blocked: bool,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "private-provider-key")
    monkeypatch.delenv("NATSUHAT_E2B_TEMPLATE", raising=False)
    if image == "no-key":
        monkeypatch.delenv("E2B_API_KEY")
    # Exercise the real metadata parser; replace only the provider's HTTP response.
    monkeypatch.setattr(runner_settings, "image_metadata", _REAL_IMAGE_METADATA)
    from e2b import Template  # noqa: PLC0415

    revision = "b" * 64
    monkeypatch.setattr(
        Template,
        "get_tags",
        lambda *_args, **_kwargs: [
            SimpleNamespace(tag="stable", build_id=revision),
            *(
                []
                if image == "unversioned"
                else [SimpleNamespace(tag="rev-" + revision, build_id=revision)]
            ),
        ],
    )
    monkeypatch.setattr(
        Template,
        "get_build_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=SimpleNamespace(value="building" if image == "building" else "ready")
        ),
    )

    def metadata_response(*_args: Any, **_kwargs: Any) -> httpx.Response:
        if image == "error":
            raise httpx.ConnectError("private-provider-key must never leak")
        rows = (
            []
            if image == "missing"
            else [
                {
                    "templateID": "published-id",
                    "aliases": ["nayakas-project/natsuhat-runner"],
                    "names": [],
                    "buildStatus": "building" if image == "building" else "ready",
                }
            ]
        )
        return httpx.Response(
            200, json=rows, request=httpx.Request("GET", "https://provider.test/templates")
        )

    monkeypatch.setattr(runner_settings.httpx, "get", metadata_response)
    base = tmp_path / "natsuhat_runs"
    manager = jobs.ScanJobManager(base)
    try:
        check = manager.runner_settings.check("e2b")
        assert (check["availability"], check["compatibility"]) == (availability, compatibility)
        assert "private-provider-key" not in json.dumps(check)
        if blocked:
            with pytest.raises(jobs.JobError):
                manager.start({**REQUEST, "runner": "e2b"})
            assert manager.list() == []
            assert not list(base.glob("*/job.json"))
        else:
            job = manager.start(
                {**REQUEST, "runner": "e2b", "target": "https://authorized.test/exit/0"}
            )
            pinned = "published-id:rev-" + revision
            assert job["runner_template"] == pinned
            assert settled(manager, job["id"])["status"] == "completed"
            # Resolve a promotion immediately, even while the settings check is cached.
            revision = "c" * 64
            next_job = manager.start({**REQUEST, "target": "https://authorized.test/exit/0"})
            assert next_job["runner_template"] == "published-id:rev-" + revision
            assert job["runner_template"] == pinned
            assert settled(manager, next_job["id"])["status"] == "completed"
    finally:
        manager.close()


def test_delete_scan_http_contract(tmp_path: Path) -> None:
    base = tmp_path / "natsuhat_runs"
    httpd, url, token = serve(base / ".dashboard", manage_scans=True, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    confirmed = {"confirmed": True}
    try:
        completed = base / "cli-completed"
        completed.mkdir()
        (completed / "run.json").write_text(
            json.dumps({"status": "completed", "end_time": "2026-01-01T12:00:00Z"})
        )
        (completed / "vulnerabilities.json").write_text("[]")
        endpoint = "/api/runs/cli-completed/delete"
        assert request(url, endpoint, body=confirmed)[0] == 403
        assert request(url, endpoint, cookie, confirmed, Origin="https://evil.test")[0] == 403
        assert request(url, endpoint, cookie, {})[0] == 400
        assert completed.is_dir()

        live = base / "cli-live"
        live.mkdir()
        (live / "run.json").write_text('{"status": "running"}')
        assert request(url, "/api/runs/cli-live/delete", cookie, confirmed)[0] == 409
        assert live.is_dir()

        for remote in (
            {"cleanup": "failed"},
            {"cleanup": "paused", "results_pending": True},
        ):
            (completed / "runner.json").write_text(json.dumps(remote))
            assert request(url, endpoint, cookie, confirmed)[0] == 409
            assert completed.is_dir()
        (completed / "runner.json").write_text('{"cleanup": "paused"}')

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "run.json").write_text((completed / "run.json").read_text())
        (base / "linked").symlink_to(outside, target_is_directory=True)
        for name in ("..%2Foutside", "linked", ".dashboard", "missing", "cli-completed%2F.."):
            assert request(url, f"/api/runs/{name}/delete", cookie, confirmed)[0] == 404
        assert outside.is_dir()

        assert request(url, endpoint, cookie, confirmed) == (200, {"ok": True})
        assert not completed.exists()
        names = {run["name"] for run in request(url, "/api/runs", cookie)[1]["runs"]}
        assert "cli-completed" not in names
        assert "cli-live" in names
        assert request(url, "/api/run?run=cli-completed", cookie)[0] == 404
        assert request(url, endpoint, cookie, confirmed)[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()

    httpd, url, token = serve(live, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    try:
        assert request(url, "/api/runs/cli-live/delete", cookie, confirmed)[0] == 403
        assert live.is_dir()
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.usefixtures("child_process")
def test_delete_job_requires_stopped_process(tmp_path: Path) -> None:
    base = tmp_path / "natsuhat_runs"
    httpd, url, token = serve(base / ".dashboard", manage_scans=True, open_browser=False)
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    confirmed = {"confirmed": True}
    try:
        status, body = request(
            url, "/api/jobs", cookie, {**REQUEST, "target": "https://hold.example.test"}
        )
        assert status == 202
        name = body["job"]["id"]
        endpoint = f"/api/runs/{name}/delete"
        assert request(url, endpoint, cookie, confirmed)[0] == 409
        assert request(url, f"/api/jobs/{name}/stop", cookie, {})[0] == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if request(url, "/api/jobs", cookie)[1]["jobs"][0]["status"] == "stopped":
                break
            time.sleep(0.02)
        assert request(url, endpoint, cookie, confirmed)[0] == 200
        assert request(url, "/api/jobs", cookie)[1]["jobs"] == []
        assert not (base / name).exists()
        assert request(url, f"/api/jobs/{name}/log", cookie)[0] == 404
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


@pytest.mark.parametrize("budget", [3.75, -1])
@pytest.mark.usefixtures("child_process")
def test_failed_job_rerun_preserves_inputs_and_original_after_restart(
    tmp_path: Path, budget: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = "e2b"
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
        assert config["max_concurrent"] == 2
        assert {r["id"]: r["configured"] for r in config["runners"]} == {"e2b": True}
        for invalid in (
            {"runner": "local"},
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
        assert code == 409
        assert request(url, "/api/jobs", cookie, REQUEST)[0] == 409
        assert httpd.job_manager is not None
        for name in remote_ids:
            request(url, f"/api/jobs/{name}/stop", cookie, {})
            assert settled(httpd.job_manager, name)["status"] == "stopped"
        code, body = request(
            url, "/api/jobs", cookie, {**REQUEST, "target": "https://authorized.test/exit/2"}
        )
        assert code == 202 and body["job"]["runner"] == "e2b"
        assert settled(httpd.job_manager, body["job"]["id"])["status"] == "completed"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("pending", ["cleanup", "results"])
@pytest.mark.usefixtures("child_process")
def test_unconfirmed_remote_cleanup_can_be_retried_without_rerunning_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending: str
) -> None:
    runner, retained = "e2b", "paused"
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


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.usefixtures("child_process")
def test_default_runner_for_new_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    monkeypatch.delenv("NATSUHAT_REMOTE_MAX_CONCURRENT", raising=False)
    if configured:
        monkeypatch.setenv("E2B_API_KEY", "synthetic-test-key")
    else:
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    manager = jobs.ScanJobManager(tmp_path / "natsuhat_runs")
    try:
        body = {key: value for key, value in REQUEST.items() if key != "runner"}
        assert manager.configuration()["default_runner"] == "e2b"
        assert manager.configuration()["max_remote_concurrent"] == 10
        if not configured:
            with pytest.raises(jobs.JobError, match="E2B_API_KEY"):
                manager.start(body)
            assert manager.list() == []
            return
        job = manager.start({**body, "target": "https://authorized.test/exit/2"})
        assert job["runner"] == "e2b"
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
        json.dumps({**REQUEST, "runner": "local", "id": name, "max_budget_usd": budget})
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
    state = directory / ".state"
    state.mkdir()
    (state / "agents.json").write_text('{"parent_of":{"root":null}}')
    record["llm_usage"] = {"cost": 2.47}
    (directory / "run.json").write_text(json.dumps(record))
    # A resume reuses the persisted instruction; it must not inject the same
    # instructions.txt as a new user message or re-run fresh target setup.
    worker.run_job(directory, resume=True)
    assert json.loads((directory / "run.json").read_text()) == record
    assert "--resume=" + name in worker.sys.argv
    assert not any(arg.startswith("--target") for arg in worker.sys.argv)
    assert "--instruction-file" not in worker.sys.argv


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
        "extra_headers": {"X-Netra-Admin-Target": "private-routing-value"},
        "extra_body": {"reasoning": {"enabled": False}, "uncensored": True},
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
        assert "extra_headers" not in saved and "private-routing-value" not in json.dumps(result)
        assert saved["extra_header_names"] == ["X-Netra-Admin-Target"]
        assert saved["extra_body"] == provider["extra_body"]
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
        assert "private-routing-value" not in metadata
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
            "extra_header_names": ["X-Netra-Admin-Target"],
            "extra_body": provider["extra_body"],
        }
        assert "private-routing-value" not in log
        # Blank replacements preserve credentials; unrelated model rates survive edits.
        update = {
            **provider,
            "id": identifier,
            "api_key": "",
            "model": "team/model-b",
            "extra_headers": {"X-Netra-Admin-Target": None},
        }
        assert request(url, "/api/llm/providers", cookie, update)[0] == 200
        persisted = json.loads(file.read_text())["providers"][0]
        assert persisted["api_key"] == "synthetic-profile-key"
        assert persisted["extra_headers"] == provider["extra_headers"]
        snapshot = httpd.job_manager.llm_profiles.selected()
        assert snapshot["extra_body"] == provider["extra_body"]
        assert snapshot["extra_headers"] == provider["extra_headers"]
        assert persisted["prices"]["team/model-a"]["output"] == 4.6
        assert (
            request(
                url, "/api/llm/providers", cookie, {**update, "base_url": "https://other.test/v1"}
            )[0]
            == 400
        )
        assert (
            request(
                url,
                "/api/llm/providers",
                cookie,
                {**update, "clear_api_key": True, "extra_headers": {}},
            )[1]["providers"][0]["has_api_key"]
            is False
        )
        update["extra_headers"] = {}
        # Keyless local providers can change endpoints without a nonexistent key to clear.
        code, result = request(
            url, "/api/llm/providers", cookie, {**update, "base_url": "https://other.test/v1"}
        )
        assert code == 200
        assert result["providers"][0]["base_url"] == "https://other.test/v1"
        assert result["providers"][0]["has_api_key"] is False
        assert httpd.job_manager.llm_profiles.selected()["extra_headers"] is None
        # Already captured job settings do not change with later provider edits.
        assert snapshot["extra_headers"] == provider["extra_headers"]
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
        assert req.headers.get("X-Tenant") == "discovery-routing-value"
        assert not req.content  # Inference-only body fields never enter discovery.
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
            "extra_headers": {"X-Tenant": "discovery-routing-value"},
            "extra_body": {"uncensored": True},
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


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"extra_headers": {"Authorization": "private-override"}}, "Extra headers:"),
        ({"extra_headers": {"hOsT": "private-override"}}, "Extra headers:"),
        ({"extra_headers": {"Content-Length": "8"}}, "Extra headers:"),
        ({"extra_headers": {"X-Bad\nName": "private-override"}}, "Extra headers:"),
        ({"extra_headers": {"X-A": "private-override\r\nInjected: yes"}}, "Extra headers:"),
        ({"extra_headers": {"X-A": "a", "x-a": "b"}}, "Extra headers:"),
        ({"extra_headers": {"X-A": None}}, "Extra headers:"),
        ({"extra_headers": {"X-A": 12}}, "Extra headers:"),
        ({"extra_headers": []}, "Extra headers:"),
        ({"extra_body": []}, "Extra body:"),
        ({"extra_body": None}, "Extra body:"),
        ({"extra_body": {"uncensored": float("nan")}}, "Extra body:"),
        *[
            ({"extra_body": {key: "private-override"}}, "Extra body:")
            for key in (
                "model",
                "messages",
                "tools",
                "stream",
                "instructions",
                "tool_choice",
            )
        ],
    ],
)
def test_provider_rejects_unsafe_overrides_without_disclosing_values(
    tmp_path: Path,
    overrides: dict[str, Any],
    field: str,
) -> None:
    profiles = LlmProfiles(tmp_path / "providers.json")
    with pytest.raises(jobs.JobError, match=field) as error:
        profiles.update(
            "providers",
            {
                "name": "Invalid",
                "kind": "custom",
                "base_url": "https://provider.test/v1",
                "model": "team/model",
                **overrides,
            },
        )
    assert "private-override" not in str(error.value)
    assert profiles.public() == {"providers": [], "selected_id": None}


def test_provider_header_edits_and_legacy_defaults(tmp_path: Path) -> None:
    profiles = LlmProfiles(tmp_path / "providers.json")
    payload = {
        "name": "Gateway",
        "kind": "custom",
        "base_url": "https://provider.test/v1",
        "model": "team/model",
    }
    saved = profiles.update("providers", payload)["providers"][0]
    identifier = saved["id"]
    profiles.update("select", {"id": identifier})
    assert profiles.selected()["extra_headers"] is None
    assert profiles.selected()["extra_body"] is None
    payload.update(
        id=identifier,
        extra_headers={"X-A": "secret-a", "X-B": "secret-b"},
        extra_body={"uncensored": False, "nested": {"n": 0, "list": [None, True]}},
    )
    profiles.update("providers", payload)
    original = profiles.selected()
    # Older clients omit these fields. They must not silently erase saved overrides.
    edit = {k: v for k, v in payload.items() if k not in {"extra_headers", "extra_body"}}
    profiles.update("providers", edit)
    assert profiles.selected() == original
    for changed in ({"base_url": "https://other.test/v1"}, {"kind": "openai"}):
        with pytest.raises(jobs.JobError, match="Re-enter or remove saved extra header"):
            profiles.update("providers", {**edit, **changed})
    profiles.update("providers", {**edit, "extra_headers": {"x-a": None, "X-B": "replacement"}})
    assert profiles.selected()["extra_headers"] == {"x-a": "secret-a", "X-B": "replacement"}
    profiles.update("providers", {**edit, "extra_headers": {"x-a": None}})
    assert profiles.selected()["extra_headers"] == {"x-a": "secret-a"}
    profiles.update("providers", {**edit, "extra_headers": {}, "extra_body": {}})
    restored = LlmProfiles(profiles.path).selected()
    assert restored["extra_headers"] is None and restored["extra_body"] is None
    assert original["extra_headers"] == {"X-A": "secret-a", "X-B": "secret-b"}


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


@pytest.mark.usefixtures("child_process")
def test_zip_http_upload_job_rerun_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = "e2b"
    monkeypatch.setenv("E2B_API_KEY", "synthetic-provider-key")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("app/main.py", "print('fixture only')")
    raw = output.getvalue()
    httpd, url, token = serve(
        tmp_path / "natsuhat_runs" / ".dashboard", manage_scans=True, open_browser=False
    )
    cookie = f"{SESSION_COOKIE_PREFIX}_{httpd.server_address[1]}={token}"
    manager = httpd.job_manager
    try:
        with httpx.Client(base_url=url, headers={"Cookie": cookie}) as client:
            endpoint = "/api/sources/upload"
            headers = {"Content-Type": "application/zip", "X-Source-Filename": "local-code.zip"}
            assert (
                client.post(endpoint, content=raw, headers={**headers, "Cookie": ""}).status_code
                == 403
            )
            assert (
                client.post(
                    endpoint, content=raw, headers={**headers, "Origin": "https://evil.test"}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    endpoint, content=raw, headers={**headers, "Content-Type": "text/plain"}
                ).status_code
                == 415
            )
            response = client.post(endpoint, content=raw, headers=headers)
            assert response.status_code == 201
            source = response.json()
            assert source["files"] == 1
            assert client.get("/api/jobs").json()["jobs"] == []
            body = {
                **REQUEST,
                "runner": runner,
                "source": {"kind": "zip", "upload_id": source["upload_id"]},
            }
            assert client.post("/api/jobs", json={**body, "authorized": False}).status_code == 400
            created = client.post("/api/jobs", json=body)
            assert created.status_code == 202
            job = created.json()["job"]
            settled(manager, job["id"])
            assert job["source"]["sha256"] == source["sha256"]
            assert job["target"] == "local-code.zip"
            record = client.get("/api/run", params={"run": job["id"]}).json()
            assert record["source"] == job["source"]
            rerun = client.post(f"/api/jobs/{job['id']}/rerun", json={"authorized": True})
            assert rerun.status_code == 202
            second = rerun.json()["job"]
            settled(manager, second["id"])
            assert second["source"] == job["source"]
            assert second["id"] != job["id"]
            assert (
                client.post(f"/api/runs/{job['id']}/delete", json={"confirmed": True}).status_code
                == 200
            )
            assert not manager.sources.directory(job["id"]).exists()
            assert manager.sources.directory(second["id"]).exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
