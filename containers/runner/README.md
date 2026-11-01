# Scan-ready remote runners

This image provides Ubuntu 24.04, Python 3.12, uv 0.9.26, Git, and a local Docker
daemon for an E2B or Daytona runner. Docker listens only on its Unix socket.
The `runner` user has Docker access and passwordless sudo; it is the trusted
orchestrator, not the isolation boundary for untrusted tool execution.

The Dockerfile is the minimal build host. `scripts/runner_images.py` layers in
Natsuhat's application, frozen Python dependencies and the complete Kali/Caido
Docker image built from `containers/Dockerfile`, then saves a reusable prepared
template/snapshot. No targets, findings, provider credentials or model credentials
are included. Only scan explicitly authorized targets.

## Scan-ready resources

Built and verified on 2026-11-01:

| Provider | Default name | Resource ID |
| --- | --- | --- |
| E2B template | `natsuhat-ready-eecbc2bb9339ce02` | `kuln9bececpr65dq31l5` |

The E2B default tag selects build `066065d9-c031-4f7c-adf2-e3bb5f92a769`.
Its fresh verification VM passed `RUNNER_IMAGE_SMOKE_OK` without installing or
building anything and remains paused and retained. E2B explicitly persists
`runner` as the default execution/filesystem user and the application directory
as its working directory; template imports otherwise reset these defaults.
Retained-VM result recovery also passed against the real E2B SDK: the same VM
was restored, its report retrieved, withdrawn findings removed, and the VM
paused again. This check used synthetic artifacts without launching a pentest
or sending a model connection.

A prepared Daytona snapshot has **not** been created. The account refused its
20 GiB preparation VM because it exceeds the retained-sandbox disk quota.
No ephemeral fallback, billing change or quota change was made. The existing
bootstrap snapshot below is not scan-ready.

## Existing bootstrap resources (not scan-ready)

Created and smoke-tested on 2026-11-01 in the configured provider accounts:

| Provider | Name | Resource ID |
| --- | --- | --- |
| E2B template | `natsuhat-runner-v1` | `67nbvn8b6oku1ohps6xt` |
| Daytona snapshot | `natsuhat-runner-v1` | `35baa366-29ae-4d35-868c-2ae5873049c3` |

Both verification sandboxes passed `RUNNER_IMAGE_SMOKE_OK` with Python 3.12.3,
uv 0.9.26, and Docker 29.1.3. The verification sandboxes were deleted; only the
reusable template and snapshot remain.

## Create provider resources

Set `E2B_API_KEY` or `DAYTONA_API_KEY` through your secret manager, then run from
the repository root:

```bash
uv run --frozen python scripts/runner_images.py e2b
uv run --frozen python scripts/runner_images.py daytona
```

These commands create persistent, potentially billable templates/snapshots in
your provider account. Names default to `natsuhat-ready-<content revision>`;
the dashboard selects that same name and verifies the revision before sending
model credentials. The revision covers application files, locked dependencies
and image build inputs, independent of timestamps. Rebuild after those change.
The commands refuse to replace existing names. `--name` selects an explicit
version; configure `NATSUHAT_E2B_TEMPLATE`/`NATSUHAT_DAYTONA_SNAPSHOT` to use it.
`--base natsuhat-runner-v1` reuses the bootstrap resources above. Without a base,
E2B builds the host Dockerfile and Daytona first creates a minimal base snapshot.
Only allowlisted application/build files are staged; Git, local targets, run
artifacts, environment variables, and secret files are never build inputs.

Each build uses 2 CPUs and 4 GiB RAM. Daytona requests a 20 GiB disk; E2B requests
20 GiB of free disk space. Resource limits and pricing depend on your account.

`prepare.sh` installs dependencies and builds the tool image **at image-build
time only**, then stops its temporary Docker daemon cleanly. E2B's start/ready
commands capture Docker running with the image already loaded. Daytona prepares
a dedicated VM, stops it, and snapshots its filesystem; its entrypoint starts
Docker on boot. Scans never run `uv sync`, pull tool images, or rebuild tools.
An incompatible or incomplete prepared image is an error, not a cold-build fallback.

After building, the script launches a fresh verification sandbox. It checks Python,
uv, the installed CLI, the content revision, credential isolation, Docker, a
preinstalled tool container with `--pull=never`, `NET_ADMIN` and
`NET_RAW`, raw socket creation (without sending packets), host-gateway resolution,
loopback port forwarding, Caido startup and Chromium rendering a local data URL.
In `finally` it pauses E2B sandboxes and stops and
archives Daytona sandboxes, preserving their data rather than deleting them.
The template/snapshot also remains even if validation fails so it can be inspected.
E2B has a five-minute auto-pause timeout with traffic-triggered resume disabled;
Daytona verification has five-minute idle stop and one-minute stopped auto-archive as fallbacks.
Its preparation VM uses a one-hour idle stop while the long build runs.
Daytona auto-delete and destructive TTL are disabled.
It does not start a pentest or make model requests.

Use the generated name with `Sandbox.create(...)` or Daytona's
`CreateSandboxFromSnapshotParams(snapshot=..., os_user="runner")`.
Daytona preparation still requires sufficient non-ephemeral disk quota and
outbound package access; prebuilding does not bypass provider account policies.
