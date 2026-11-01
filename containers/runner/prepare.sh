#!/bin/bash
# Build-time only: nothing in this script runs when a scan is submitted.
set -euo pipefail
cd /home/runner/natsuhat

uv sync --frozen --no-dev --python 3.12

# Template build steps have their own process lifetime. Stop the daemon we start
# before snapshotting its filesystem; the image's start command starts it again.
daemon_pid=
cleanup() {
  if [[ -n "$daemon_pid" ]]; then
    kill "$daemon_pid"
    wait "$daemon_pid" || true
  fi
}
trap cleanup EXIT
if ! docker info >/dev/null 2>&1; then
  dockerd --host=unix:///var/run/docker.sock --log-level=error >/tmp/runner-docker-build.log 2>&1 &
  daemon_pid=$!
fi
timeout 60 sh -c 'until docker info >/dev/null 2>&1; do sleep 1; done'
bash scripts/docker.sh local
# Only disposable build caches are removed; the prepared runtime image remains.
docker builder prune --force
.venv/bin/python -c 'import natsuhat.interface.viewer.job_runner'
docker image inspect natsuhat-sandbox:local >/dev/null
echo RUNNER_PREPARE_OK
