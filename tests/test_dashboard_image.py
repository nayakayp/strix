"""Opt-in image contract test; no scans, credentials, Docker socket, or network egress."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    "NATSUHAT_TEST_IMAGE" not in os.environ,
    reason="Build the dashboard image and set NATSUHAT_TEST_IMAGE to test it.",
)


def _docker(*args: str, source: str | None = None) -> str:
    return subprocess.run(  # noqa: S603 - fixed Docker executable; no shell
        ["docker", *args],  # noqa: S607 - Docker CLI on the test host's PATH
        input=source,
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout.strip()


def test_dashboard_image_auth_health_and_persistence() -> None:
    image = os.environ["NATSUHAT_TEST_IMAGE"]
    name = f"natsuhat-image-test-{uuid.uuid4().hex}"
    volume = name + "-data"
    containers: list[str] = []
    previous_token = ""
    _docker("volume", "create", volume)
    try:
        for phase in range(2):
            container = f"{name}-{phase}"
            containers.append(container)
            _docker(
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--cpus=1",
                "--memory=2g",
                "--pids-limit=256",
                "--env",
                "NATSUHAT_VIEWER_SECURE_COOKIES=true",
                "--env",
                "E2B_API_KEY=synthetic-e2b-key",
                "--mount",
                f"type=volume,source={volume},target=/var/lib/natsuhat",
                image,
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = json.loads(_docker("inspect", container))[0]["State"]
                if state["Health"]["Status"] == "healthy":
                    break
                assert state["Running"], "Dashboard exited before becoming healthy"
                time.sleep(1)
            else:
                raise AssertionError("Dashboard did not become healthy within 90 seconds")

            assert _docker("exec", container, "id", "-u") == "10001"
            tokens = re.findall(r"\?token=([A-Za-z0-9_-]+)", _docker("logs", container))
            assert tokens, "Dashboard did not print an operator authorization URL"
            token = tokens[-1]
            assert token != previous_token
            # Exercise the public HTTP contract inside the isolated container.
            _docker(
                "exec",
                "-i",
                container,
                "python",
                "-",
                source=f"""
import json
import urllib.error
import urllib.request
from pathlib import Path

url = 'http://127.0.0.1:8080'
def get(path, cookie=''):
    request = urllib.request.Request(url + path, headers={{'Cookie': cookie}})
    return urllib.request.urlopen(request, timeout=3)

for cookie in ['', 'natsuhat_viewer_session_8080=' + {previous_token!r}]:
    try:
        get('/api/jobs', cookie)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError('Unauthenticated/old session could read jobs')

with get('/?token=' + {token!r}) as response:
    assert b'<html' in response.read()
    assert '; Secure' in response.headers['Set-Cookie']
    cookie = response.headers['Set-Cookie'].split(';')[0]
with get('/api/capabilities', cookie) as response:
    assert json.load(response)['can_manage_scans'] is True
with get('/api/jobs', cookie) as response:
    configuration = json.load(response)
    assert configuration['default_runner'] == 'e2b'
    assert configuration['runners'] == [{{'id': 'e2b', 'name': 'E2B', 'configured': True}}]

record = Path('natsuhat_runs/synthetic/run.json')
private = Path.home() / '.natsuhat/smoke-setting'
if {phase} == 0:
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({{
        'run_name': 'synthetic', 'status': 'completed', 'end_time': '2026-01-01',
    }}))
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text('saved configuration')
else:
    assert json.loads(record.read_text())['status'] == 'completed'
    assert private.read_text() == 'saved configuration'
with get('/api/runs', cookie) as response:
    assert [run['name'] for run in json.load(response)['runs']] == ['synthetic']
""",
            )
            _docker("stop", "--time", "40", container)
            state = json.loads(_docker("inspect", container))[0]["State"]
            assert state["ExitCode"] == 0
            assert not state["OOMKilled"]
            previous_token = token
    finally:
        for container in containers:
            _docker("rm", "-f", container)
        _docker("volume", "rm", volume)
