#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n /usr/local/bin/natsuhat-runner-start
fi

# E2B may already have started Docker through its init system.
if docker info >/dev/null 2>&1; then
    exec sleep infinity
fi

# Keep Docker in the provider-managed foreground process. Never expose its API
# over TCP; only the trusted runner can access the local Unix socket.
exec dockerd --host=unix:///var/run/docker.sock --log-level=error
