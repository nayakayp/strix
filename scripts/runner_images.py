# ruff: noqa: INP001
"""Build a scan-ready E2B template or Daytona snapshot from this exact checkout.

Usage: uv run --frozen python scripts/runner_images.py e2b
Requires the selected provider's API key in the environment. Creates persistent,
potentially billable provider resources; never replaces an existing name.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from natsuhat.interface.viewer.remote import (
    RemoteSandbox,
    runner_revision,
    source_bundle,
)


if TYPE_CHECKING:
    from e2b.template.types import BuildInfo


logger = logging.getLogger(__name__)
CONTEXT = Path(__file__).resolve().parents[1] / "containers" / "runner"
STAGE = (
    "mkdir -p /home/runner/natsuhat && "
    "tar --no-same-owner -xzf /home/runner/source.tar.gz -C /home/runner/natsuhat && "
    "rm /home/runner/source.tar.gz && "
    "install -m 0755 /home/runner/natsuhat/containers/runner/start.sh "
    "/usr/local/bin/natsuhat-runner-start && "
    "bash /home/runner/natsuhat/containers/runner/prepare.sh && "
    "chown -R runner:runner /home/runner/natsuhat"
)
READY = (
    "sudo -n docker image inspect natsuhat-sandbox:local >/dev/null 2>&1 && "
    "test -x /home/runner/natsuhat/.venv/bin/python"
)
SMOKE = r"""
set -eu
python3 -c 'import sys; assert sys.version_info >= (3, 12); print(sys.version.split()[0])'
uv --version
timeout 60 sh -c 'until docker info >/dev/null 2>&1; do sleep 1; done'
docker version --format 'Docker server: {{.Server.Version}}'
/home/runner/natsuhat/.venv/bin/python -m natsuhat.interface.main --help >/dev/null
/home/runner/natsuhat/.venv/bin/python -c '
import json, os
from natsuhat.interface.viewer.remote import runner_revision
assert runner_revision() == json.load(open("/opt/natsuhat-runner.json"))["revision"]
assert not any(os.environ.get(key) for key in ("E2B_API_KEY", "DAYTONA_API_KEY", "LLM_API_KEY"))
print("Prepared application revision and credential isolation: passed")
'
container=$(docker run --pull=never -d --cap-add NET_ADMIN --cap-add NET_RAW \
    --add-host host.docker.internal:host-gateway \
    -p 127.0.0.1:48080:48080 --entrypoint python3 natsuhat-sandbox:local \
    -m http.server 48080)
tools=
trap 'docker rm -f "$container" ${tools:+"$tools"} >/dev/null' EXIT
# The tool image defaults to an unprivileged user. Inspect container capabilities
# as root; ordinary tools use their own configured privilege escalation.
docker exec --user root "$container" python -c '
import socket
with open("/proc/self/status") as status:
    caps = int(next(line.split()[1] for line in status if line.startswith("CapEff:")), 16)
assert caps & (1 << 12), "NET_ADMIN unavailable"
assert caps & (1 << 13), "NET_RAW unavailable"
sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
sock.close()
socket.gethostbyname("host.docker.internal")
print("Container network capabilities: passed")
'
python3 -c '
import time, urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:48080", timeout=2) as response:
            assert response.status == 200
        break
    except OSError:
        if attempt == 29:
            raise
        time.sleep(1)
print("Container port forwarding: passed")
'
tools=$(docker run --pull=never -d --cap-add NET_ADMIN --cap-add NET_RAW \
    natsuhat-sandbox:local sleep infinity)
for attempt in $(seq 1 60); do
    if docker logs "$tools" 2>&1 | grep -q 'Container ready'; then break; fi
    if [ "$attempt" = 60 ]; then docker logs "$tools"; exit 1; fi
    sleep 1
done
docker exec "$tools" bash -lc '
for tool in nmap nuclei sqlmap ffuf httpx caido-cli agent-browser semgrep; do
    command -v "$tool" || exit 1
done
test -x /usr/bin/chromium
echo "Preinstalled security tools and Caido startup: passed"
agent-browser open "data:text/html,<title>Natsuhat-ready</title>" >/dev/null
agent-browser get title | grep -Fx "Natsuhat-ready"
agent-browser close >/dev/null
echo "Preinstalled Chromium browser: passed"
'
echo RUNNER_IMAGE_SMOKE_OK
"""


def create_e2b(name: str, context: Path, base: str | None) -> None:
    from e2b import Template  # noqa: PLC0415 - optional, script-only SDK

    if Template.exists(name, request_timeout=30):
        raise SystemExit(f"E2B template {name!r} already exists; choose a new name.")
    builder = Template(file_context_path=context)
    template = (
        builder.from_template(base)
        if base
        else builder.from_dockerfile(str(context / "Dockerfile"))
    )
    template = (
        template.copy("source.tar.gz", "/home/runner/source.tar.gz", user="root")
        .copy("manifest.json", "/opt/natsuhat-runner.json", user="root")
        .run_cmd(STAGE, user="root")
        # E2B resets Dockerfile USER to `user`; persist the application owner for
        # readiness, runtime commands and filesystem operations instead.
        .set_user("runner")
        .set_workdir("/home/runner/natsuhat")
    )
    build = Template.build_in_background(
        template.set_start_cmd("/usr/local/bin/natsuhat-runner-start", READY),
        name,
        cpu_count=2,
        memory_mb=4096,
        min_free_disk_mb=20480,
        on_build_logs=lambda entry: logger.info("%s", entry.message),
        request_timeout=60,
    )
    logger.info("E2B build submitted: template=%s build=%s", build.template_id, build.build_id)
    wait_e2b(build)
    logger.info("E2B template created: %s (ID %s)", name, build.template_id)
    verify_e2b(name)


def wait_e2b(build: BuildInfo) -> None:
    """Retry read-only polling, never resubmit a build after a broken connection."""
    from e2b import Template  # noqa: PLC0415

    offset = 0
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        try:
            status = Template.get_build_status(build, logs_offset=offset, request_timeout=30)
        except httpx.TransportError:
            logger.warning("E2B build connection interrupted; continuing the same build.")
            time.sleep(5)
            continue
        for entry in status.log_entries:
            logger.info("%s", entry.message)
        offset += len(status.log_entries)
        if status.status.value == "ready":
            return
        if status.status.value == "error":
            raise RuntimeError(f"E2B template build failed: {status.reason}")
        time.sleep(2)
    raise TimeoutError(
        f"Check E2B build {build.template_id}/{build.build_id}; it was not resubmitted."
    )


def verify_e2b(name: str) -> None:
    from e2b import Sandbox  # noqa: PLC0415

    started = time.monotonic()
    sandbox = Sandbox.create(
        name,
        timeout=300,
        lifecycle={"on_timeout": "pause", "auto_resume": False},
        request_timeout=60,
    )
    logger.info("Fresh E2B sandbox created in %.1f seconds", time.monotonic() - started)
    try:
        result = sandbox.commands.run(SMOKE, timeout=180, request_timeout=190)
        logger.info("%s", result.stdout)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr)
        logger.info("E2B creation and smoke verification: %.1f seconds", time.monotonic() - started)
    finally:
        sandbox.pause(request_timeout=90)
        logger.info("E2B verification sandbox paused and retained: %s", sandbox.sandbox_id)


def create_daytona(name: str, context: Path, base: str | None) -> None:
    from daytona import (  # noqa: PLC0415 - optional, script-only SDK
        CreateSandboxFromSnapshotParams,
        CreateSnapshotParams,
        Daytona,
        DaytonaBadRequestError,
        DaytonaConfig,
        DaytonaNotFoundError,
        Image,
        Resources,
    )

    client = Daytona(DaytonaConfig(otel_enabled=False))
    try:
        client.snapshot.get(name)
    except DaytonaNotFoundError:
        pass
    else:
        raise SystemExit(f"Daytona snapshot {name!r} already exists; choose a new name.")
    if base is None:
        base = name + "-base"
        client.snapshot.create(
            CreateSnapshotParams(
                name=base,
                image=Image.from_dockerfile(context / "Dockerfile", strict_context=True),
                resources=Resources(cpu=2, memory=4, disk=20),
            ),
            on_logs=lambda chunk: logger.info("%s", chunk),
            timeout=1200,
        )
    job_id = "image-build-" + uuid.uuid4().hex
    try:
        sandbox = client.create(
            CreateSandboxFromSnapshotParams(
                name=job_id,
                snapshot=base,
                os_user="runner",
                public=False,
                labels={"natsuhat_job": job_id},
                auto_stop_interval=60,
                auto_archive_interval=1,
                auto_delete_interval=-1,
                ttl_minutes=0,
            ),
            timeout=90,
        )
        sandbox.fs.upload_file(
            (context / "source.tar.gz").read_bytes(), "/home/runner/source.tar.gz"
        )
        sandbox.fs.upload_file(
            (context / "manifest.json").read_bytes(), "/home/runner/manifest.json"
        )
        result = sandbox.process.exec(
            "sudo -n mv /home/runner/manifest.json /opt/natsuhat-runner.json && "
            "sudo -n bash -c " + shlex.quote(STAGE),
            timeout=3600,
        )
        logger.info("%s", result.result)
        if result.exit_code != 0:
            raise RuntimeError("Daytona image preparation failed.")
        # Cold snapshots preserve a clean Docker data directory, not running processes.
        sandbox.stop(timeout=60)
        sandbox.create_snapshot(name, timeout=1200)
        logger.info("Daytona prepared snapshot created: %s", name)
    except DaytonaBadRequestError as exc:
        if "Disk request" in str(exc) and "non-ephemeral" in str(exc):
            raise SystemExit(
                "Daytona refused the preparation VM: its snapshot exceeds this account's "
                "retained-sandbox disk quota. No ephemeral fallback or account changes were made."
            ) from None
        raise
    finally:
        retained = RemoteSandbox("daytona", job_id)
        for attempt in range(3):
            try:
                retained.retain()
                break
            except Exception:
                if attempt == 2:
                    raise
    verify_daytona(name)


def verify_daytona(name: str) -> None:
    """Check a fresh VM from the final snapshot, not the preparation filesystem."""
    from daytona import (  # noqa: PLC0415
        CreateSandboxFromSnapshotParams,
        Daytona,
        DaytonaConfig,
    )

    client = Daytona(DaytonaConfig(otel_enabled=False))
    job_id = "image-verify-" + uuid.uuid4().hex
    try:
        sandbox = client.create(
            CreateSandboxFromSnapshotParams(
                name=job_id,
                snapshot=name,
                os_user="runner",
                public=False,
                labels={"natsuhat_job": job_id},
                auto_stop_interval=5,
                auto_archive_interval=1,
                auto_delete_interval=-1,
                ttl_minutes=0,
            ),
            timeout=90,
        )
        result = sandbox.process.exec(SMOKE, timeout=180)
        logger.info("%s", result.result)
        if result.exit_code != 0:
            raise RuntimeError("Daytona runner image smoke test failed.")
    finally:
        retained = RemoteSandbox("daytona", job_id)
        for attempt in range(3):
            try:
                state = retained.retain()
                logger.info("Daytona verification sandbox %s: %s", retained.id, state)
                break
            except Exception:
                if attempt == 2:
                    raise


if __name__ == "__main__":
    logging.basicConfig(format="%(message)s")
    logger.setLevel(logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("e2b", "daytona"))
    parser.add_argument("--name", help="Override the content-versioned image name.")
    parser.add_argument("--base", help="Reuse an existing minimal runner template/snapshot.")
    args = parser.parse_args()
    bundle = source_bundle()
    revision = runner_revision(bundle)
    name = args.name or "natsuhat-ready-" + revision[:16]
    logger.info("Building %s for application revision %s", name, revision)
    with tempfile.TemporaryDirectory(prefix="natsuhat-image-") as temporary:
        context = Path(temporary)
        for filename in ("Dockerfile", "start.sh"):
            shutil.copyfile(CONTEXT / filename, context / filename)
        (context / "source.tar.gz").write_bytes(bundle)
        (context / "manifest.json").write_text(json.dumps({"revision": revision}))
        {"e2b": create_e2b, "daytona": create_daytona}[args.provider](name, context, args.base)
