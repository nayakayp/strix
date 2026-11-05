"""Execute the CI deploy step against simulated GitHub and Komodo command boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
FAKE_COMMAND = """
import json
import os
import sys
from pathlib import Path

path = Path(os.environ['FAKE_STATE'])
state = json.loads(path.read_text())
command = Path(sys.argv[0]).name
exit_code = 0
if command == 'gh':
    if 'PATCH' in sys.argv:
        assert state.get('idle') is True, 'Release promoted while Stack was busy'
        assert 'force=false' in sys.argv
        state['promoted'] = True
    else:
        print('superseded' if state['scenario'] == 'superseded' else 'tested-commit')
elif command == 'curl':
    headers = [sys.argv[i+1] for i, arg in enumerate(sys.argv[:-1]) if arg == '--header']
    assert 'X-Api-Key: fixture-key' in headers
    assert 'X-Api-Secret: fixture-secret' in headers
    endpoint = sys.argv[-1].removeprefix('https://komodo.example.test/')
    body = json.loads(sys.argv[sys.argv.index('--data')+1])
    if endpoint == 'read/GetStackActionState':
        assert body == {'stack': 'fixture stack'}
        # A pull also makes the Stack busy, even if it is not deploying.
        state['idle'] = state.get('observed_busy', False)
        state['observed_busy'] = True
        print(json.dumps({'pulling': not state['idle'], 'deploying': False}))
    elif endpoint in ('read/GetStack', 'read/GetRepo'):
        config = {
            'repo': 'fixture/repository',
            'git_provider': (
                'untrusted.example.test' if state['scenario'] == 'wrong-provider' else 'github.com'
            ),
            'git_https': state['scenario'] != 'source-http',
            'branch': 'main' if state['scenario'] == 'untested-source' else 'production',
            'commit': 'old-commit' if state['scenario'] == 'pinned-source' else '',
            'webhook_enabled': state['scenario'] == 'repo-webhook',
        }
        if endpoint == 'read/GetStack':
            assert body == {'stack': 'fixture stack'}
            config.update(
                linked_repo='linked-repo' if state['linked_repo'] else '',
                auto_update=state['scenario'] == 'auto-update',
                files_on_host=False, file_contents='', run_build=True,
                extra_args=['--wait', '--wait-timeout', '180'],
            )
            if state['linked_repo']:
                # The linked repository, not these unused fields, owns the source.
                config.update(
                    repo='', branch='main', commit='', webhook_enabled=False,
                    git_provider='unused.example.test', git_https=False,
                )
            if state['scenario'] == 'stack-webhook':
                config['webhook_enabled'] = True
            print(json.dumps({
                'config': config,
                'info': {
                    'missing_files': (
                        ['override.yaml'] if state['scenario'] == 'missing-file' else []
                    ),
                },
            }))
        else:
            assert body == {'repo': 'linked-repo'}
            print(json.dumps({'config': config}))
    elif endpoint == 'execute/DeployStack':
        assert body == {'stack': 'fixture stack'}
        assert state.get('promoted') is True
        state['submissions'] = state.get('submissions', 0) + 1
        state['update'] = {
            '_id': {'$oid': 'deployment-update'}, 'status': 'Queued', 'success': True,
        }
        if state['scenario'] == 'lost-ack':
            exit_code = 28  # The execution happened, but its HTTP acknowledgement timed out.
        else:
            print(json.dumps(state['update']))
    elif endpoint == 'read/GetUpdate':
        assert body == {'id': 'deployment-update'}
        state['polls'] = state.get('polls', 0) + 1
        if state['scenario'] == 'poll-error':
            exit_code = 22
        else:
            state['returned_terminal'] = state['polls'] > 1
            state['update'].update(
                status='Complete' if state['returned_terminal'] else 'InProgress',
                success=state['scenario'] != 'failed-update',
            )
            print(json.dumps(state['update']))
    else:
        raise AssertionError('Unexpected API endpoint: ' + endpoint)
elif command != 'sleep':
    raise AssertionError('Unexpected executable: ' + command)
path.write_text(json.dumps(state))
sys.exit(exit_code)
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "failed-update",
        "lost-ack",
        "poll-error",
        "superseded",
        "untested-source",
        "wrong-provider",
        "source-http",
        "pinned-source",
        "stack-webhook",
        "repo-webhook",
        "auto-update",
        "missing-file",
    ],
)
@pytest.mark.parametrize("linked_repo", [False, True])
def test_deployment_waits_for_komodo_result(
    tmp_path: Path, scenario: str, linked_repo: bool
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = workflow["jobs"]["deploy-vps"]["steps"][0]
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"scenario": scenario, "linked_repo": linked_repo}))
    summary = tmp_path / "summary"
    for command in ("gh", "curl", "sleep"):
        executable = tmp_path / command
        executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        executable.chmod(0o700)
    result = subprocess.run(  # noqa: S603 - run the repository's deploy step with fake commands
        ["bash", "-c", step["run"]],  # noqa: S607 - Bash is a required CI tool
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "FAKE_STATE": str(state_file),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GH_REPO": "fixture/repository",
            "TESTED_SHA": "tested-commit",
            "KOMODO_ADDRESS": "https://komodo.example.test/",
            "KOMODO_API_KEY": "fixture-key",
            "KOMODO_API_SECRET": "fixture-secret",
            "KOMODO_STACK": "fixture stack",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    state = json.loads(state_file.read_text())
    assert state["observed_busy"]
    if scenario in {
        "untested-source",
        "wrong-provider",
        "source-http",
        "pinned-source",
        "stack-webhook",
        "repo-webhook",
        "auto-update",
        "missing-file",
    }:
        assert result.returncode != 0
        assert not state.get("promoted")
        assert state.get("submissions", 0) == 0
        assert not summary.exists()
        return
    if scenario == "superseded":
        assert result.returncode == 0, result.stderr
        assert not state.get("promoted")
        assert state.get("submissions", 0) == 0
        assert not summary.exists()
        return

    # A failed/ambiguous acknowledgement must never create another remote deployment.
    assert state["submissions"] == 1
    if scenario == "lost-ack":
        assert result.returncode == 28
        assert state.get("polls", 0) == 0
    elif scenario == "poll-error":
        assert result.returncode == 22
    else:
        assert state["returned_terminal"], "Job finished before the asynchronous deployment"
        assert (result.returncode == 0) is (scenario == "success"), result.stderr

    if scenario == "success":
        assert "deployment completed" in summary.read_text()
        assert "deployment-update" in summary.read_text()
    else:
        assert not summary.exists()
