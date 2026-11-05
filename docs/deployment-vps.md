# Railway-style VPS deployment with Komodo

[Komodo](https://komo.do/) is the Rust-based deployment manager: Core hosts the
dashboard/API, Periphery manages Docker on your VPS, and a database stores Komodo
state. Use its [official setup](https://komo.do/docs/setup) rather than installing
a second deployment platform. This repository supplies the Natsuhat application
stack; it does not duplicate Komodo's installation files.

## Push-to-deploy, with an availability limit

After setup: **push main → CI passes → promote production → Komodo deploys**.
A local commit without a push does not deploy. Automation stays disabled until
you explicitly set GitHub variable `VPS_AUTO_DEPLOY=true`.

**Zero-downtime upgrades are not supported yet.** The dashboard owns scan
subprocesses and holds an exclusive workspace lock. Two managing dashboards
cannot share that workspace, and stopping the dashboard stops active scans.
Compose replaces the single instance with a service interruption. Caddy's HTTPS
proxy and the image health check do not remove this limitation.

**Deploy only while idle.** Auto deployment can stop a paid E2B scan.
Disable `VPS_AUTO_DEPLOY` before long scans and check already queued/running jobs
in GitHub and Komodo. Alternatively, require approval on GitHub's `production`
environment and approve only while idle. The workflow does not detect active
scans or wait for them. Results/checkpoints persist, and eligible interrupted
scans can be resumed, but resume is not uninterrupted execution. Browser tokens
reset after restart.

True uninterrupted upgrades require a durable scan supervisor independent of
the web server, shared authentication, and web replicas that can safely overlap.
One VPS remains a single point of failure even with those changes.

## 1. Prepare the VPS

Install Docker Engine, Compose v2.24.4 or newer, and Komodo Core/Periphery using
the official [MongoDB setup](https://komo.do/docs/setup/mongo). Komodo recommends MongoDB; its
database, Docker images, and logs also consume memory and disk. No measured
Komodo-versus-Coolify resource comparison is claimed here.

The VPS runs the dashboard, Caddy, Komodo, and Komodo's database. **Dashboard scans
run on E2B, not in local Docker sandboxes.** Their VM resources and charges are
separate from the VPS. Size the VPS for controller processes, uploads, retained
results, and image builds; builds can use substantially more memory/disk than
the idle dashboard. No minimum VPS size or scan benchmark is claimed here.

- Use a dedicated, trusted-operator VPS, not a multi-tenant server. Dashboard
  authorization permits paid scans and access to uploaded source and results.
  The dashboard runs as UID/GID 10001 without root or host Docker socket access.
- Serve Komodo administration and its authenticated API over HTTPS. Use strong
  operator authentication and a least-privilege service user for CI. Keep Docker
  and Periphery APIs private; public dashboard access does not require exposing them.
- Point `your-dashboard.example` at the VPS. The included Caddy service needs
  ports 80 and 443 available for automatic HTTPS; do not run a second proxy on
  those ports. If you already have a proxy, remove this stack's proxy service and
  route your existing proxy to the dashboard instead.
- Dashboard access is not restricted by source IP. Authorization relies on its
  private capability token over HTTPS, not merely on reaching the website.
  A VPN or IP allowlist is optional defense in depth, not a deployment requirement.
  E2B sandboxes have network egress and receive the scan inputs and selected LLM
  credentials; only submit systems you are authorized to test.

The dashboard drops all Linux capabilities, prevents privilege escalation, and
limits its controller to 1 CPU, 2 GiB RAM, and 256 processes. These are starting
limits, not a scan-load benchmark; monitor controller usage before increasing
concurrency. Image builds are not constrained by these runtime limits. HTTPS
deployments set `NATSUHAT_VIEWER_SECURE_COOKIES=true` for both dashboard and GitHub
flow cookies; ordinary local HTTP viewers leave it unset. The proxy also sets
HSTS, no-referrer, nosniff, and anti-framing headers.

Create the persistent state directory on the VPS with the image user's ownership:

```bash copy Copy State Directory Command
sudo install -d -m 700 -o 10001 -g 10001 /var/lib/natsuhat
```

If upgrading a root-run installation, stop the dashboard and back up its state
before changing ownership of the existing directory and its contents to UID/GID
10001. Do not delete existing data. No local Kali sandbox image build is needed:
scans use the published `nayakas-project/natsuhat-runner:stable` E2B template,
pinning its release per new scan. Do not launch a scan to validate installation.

## 2. Create a Komodo Stack

In Komodo, create a **Stack**, selecting your connected VPS as its Server.
Use the [Git repository source](https://komo.do/docs/deploy/compose):

| Setting | Value |
| --- | --- |
| Repository | `nayakayp/strix`, or your fork |
| Branch | `production` (create from a passing main commit first) |
| Run directory | Repository root (`.` relative to the cloned repository) |
| Compose files | `deploy/compose.yaml` |
| Project name | `natsuhat` (keep stable across releases) |
| Run build | Enabled |
| Extra args for Compose up | `--build --wait --wait-timeout 120` (rebuild, then wait for readiness) |
| Auto update / direct push webhook | Disabled; CI requests deployment |

For an existing Komodo Caddy proxy on `komodo_default`, use Compose files
`deploy/compose.yaml` then `deploy/compose.komodo.yaml`. This tracked override
removes the bundled proxy and exposes only the internal `natsuhat-dashboard:8080`
network alias. Do not reference `/etc/komodo/overrides/natsuhat.compose.yaml`:
an untracked host-only override may be invisible to Core or lost during recovery.
Publish the tracked override to the release branch **before** switching the
Stack's file paths; confirm Komodo reports no missing files. Apply the HTTPS
headers to the **existing proxy**, since the removed service cannot set them.
Both dashboards can remain publicly reachable, but sensitive application and
Komodo API operations must still require authentication.

If using a linked Komodo Repo, set **that Repo's** branch to `production`, clear
its commit pin, and disable its webhook too. The Stack's own branch field does
not override the linked Repo's source. Do not switch before `production` exists.

Keep the repository files available: Compose builds from the repository root and
mounts `deploy/Caddyfile`. Do not paste just the Compose text into an empty stack.
If running Periphery in Docker, its host paths must match the paths it passes to
Docker; use Komodo's recommended mounts. Do not alter the 20-minute stop grace
period to a shorter timeout. It allows cleanup, not uninterrupted scanning.

Add these values to the Stack's environment (Komodo writes an env file):

| Variable | Value |
| --- | --- |
| `NATSUHAT_DOMAIN` | Bare hostname, e.g. `your-dashboard.example` (required) |
| `E2B_API_KEY` | Secret E2B API key (required; passed into the dashboard container) |
| `NATSUHAT_LLM` | Model ID, default `openai/gpt-5.4` |
| `LLM_API_KEY` | Secret provider API key |
| `LLM_API_BASE` | Optional custom endpoint |

Use Komodo's secret-variable facilities; never put provider keys in Git or build
arguments. The dashboard can also persist LLM profiles. Check the E2B runner in
**Settings → Sandbox → E2B** before scanning; key presence alone does not prove
provider access. Optional GitHub App variables must be added to the Compose
`environment` mapping before that integration works. E2B template overrides and
Local Docker settings do not enable alternative dashboard runners.

Deploy manually once. In private dashboard logs, find the URL ending `?token=…`.
Open your HTTPS dashboard domain with that query string, replacing the printed
`http://0.0.0.0:8080` origin. Treat the URL as a password. Do not paste it in issues,
shared logs, or screenshots. The public homepage alone grants no authorization.
Repeat using the newly printed token after a restart. Caddy access logging is
not enabled; do not add logs that retain authorization query strings.

## 3. Enable deployment after CI

The `production` branch prevents Komodo from building an untested newer tip of
`main`. Only the latest passing main **push** is promoted, fast-forward only.
PR runs, failed CI, manual CI runs, and superseded main commits do not deploy.
Do not push untested code directly to `production`.

1. Protect `main`: require pull requests, the `ci-passed` status check, and no
   force pushes or deletions. Confirm Actions are enabled and a main commit
   passes CI before creating `production` from that exact SHA. Leave
   `VPS_AUTO_DEPLOY` disabled during this bootstrap.
2. Create GitHub environment `production`. Optionally require idle-scan approval.
   Environment branch policies must allow `main`, where CI runs.
3. Allow the deploy job's `GITHUB_TOKEN` to update `production`. Branch protections
   must permit its fast-forward update; retain main protections and prohibit
   force-pushes. If your policy prohibits this, leave automation disabled and
   promote reviewed commits manually.
4. Create a Komodo service user with read and execution access only to this Stack.
   A linked Repo also needs read access for the source validation; no Repo write
   or execution access is required. Do not use your Komodo administrator key.
   Create its API key/secret. In GitHub's `production` environment, configure secrets:
   `KOMODO_ADDRESS` (HTTPS base URL), `KOMODO_API_KEY`, `KOMODO_API_SECRET`.
5. Set repository Actions variable `KOMODO_STACK` to the Stack ID or name, and
   `VPS_AUTO_DEPLOY` to `true`.
6. Push to main. Inspect **CI → Deploy VPS**, then the Stack's updates in Komodo.

The job executes no repository code with its write token. It waits for all Stack
actions to become idle using `POST /read/GetStackActionState`, then verifies the
Stack uses Git source, disables direct webhooks/auto-update, builds and waits for
health, and has no missing files. It checks that the effective source (including
a linked Repo) is this repository on `github.com` over HTTPS, using the
`production` branch without a commit pin or webhook. Unsafe configuration fails
before release promotion. It then rechecks main, promotes the tested commit,
and calls `POST /execute/DeployStack` exactly once.
It keeps its GitHub concurrency lock while polling the returned Update ID with
`POST /read/GetUpdate`. The job succeeds only on `status: Complete` and
`success: true`; an accepted request alone is not success. The Stack's Compose
`--wait` arguments make deployment wait for dashboard health, not merely startup.

Idle waiting and deployment share a 40-minute deadline. Failure, an invalid API
response, or timeout fails CI. Remote work can continue after a timeout or job
cancellation: inspect Komodo before retrying. A manual/external action can still
race the idle check; if Komodo rejects this deployment as busy, CI reports failure
instead of silently losing it. No execution POST is automatically retried, and no
automatic rollback is promised.

## 4. Persistence, storage, and rollback

The host directory `/var/lib/natsuhat` persists dashboard state. Keep the container
workspace path stable across releases because settings are keyed by workspace
path. Ensure UID/GID 10001 can write the directory. It stores:

- `workspace/natsuhat_runs/`: reports, job logs, history, checkpoints.
- `config/.natsuhat/`: credentials, settings, source uploads and snapshots.

Back up this whole directory while scans are stopped. Encrypt backups and test
restores to the same path, since settings are keyed by workspace path. Back up
Caddy's certificate volume and Komodo's database separately. Removing containers
does not delete the bind mount; losing the VPS does.

The multi-stage dashboard image omits Go, uv, build caches, and frontend source
from the final image. Docker may still retain build layers on the VPS. Use
`docker system df` and Komodo's disk monitoring; bound logs are already configured.
Review old images/build cache for removal while idle, retaining rollback images.
Do not blindly prune volumes or use `docker compose down -v`. For a tight VPS,
build/publish images on CI and deploy pinned registry images instead of compiling
on the VPS; that publishing path is not enabled by this source-build stack.

The image health check requests `/api/capabilities` only after initialization. It
does not prove sandbox readiness, provider credentials, or scan success. Before
enabling automation, check: healthy container and HTTPS; `/api/jobs` returns 403
without authorization; idle restart preserves history/settings and rejects the
old token; failed CI does not promote production. No scan is needed for these.

For rollback, disable auto deployment and inspect queued work. Revert the bad
change on main, pass CI, then re-enable and rerun the successful push's jobs. Keep
the production branch fast-forward. For urgent recovery, deploy a retained
known-good release manually in Komodo. A failed Compose replacement may leave
the service unavailable. Code rollback does not undo persistent data changes;
retain backups. Rolling back the dashboard does not change a scan's pinned E2B release.
