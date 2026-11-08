# Natsuhat

Self-hosted AI penetration testing for applications, APIs, and source code.
Natsuhat coordinates security agents, runs tools in a Docker sandbox, and writes
findings with reproduction steps and remediation guidance.

**Local by default:** no hosted account or cloud sign-up is required. Scan
artifacts stay on your machine. Configured LLM, search, and MCP providers still
receive the data needed for their requests. An explicitly enabled
[Supabase dashboard](docs/supabase.md) adds individual sign-in, workspace roles,
PostgreSQL records, and private Storage in your own Supabase project.

## Run from source

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and a running Docker daemon.
The sandbox is built locally; no published Natsuhat image or package is required.

```bash
uv sync --frozen
bash scripts/docker.sh local

export NATSUHAT_LLM="openai/gpt-5.4"
export LLM_API_KEY="your-api-key"
uv run --frozen natsuhat -n --target ./app-directory --scan-mode quick --max-budget 10
```

Run without `-n` in a terminal for the interactive interface. See
`uv run --frozen natsuhat --help` for all options. To install the command into a
dedicated environment from this checkout, use `uv tool install .`.

## Targets and results

```bash
# A deployed application
uv run --frozen natsuhat -n --target https://your-app.example

# Source and a deployed application together
uv run --frozen natsuhat -n --target ./app --target https://your-app.example

# Focused instructions
uv run --frozen natsuhat -n --target ./app --instruction "Review authorization boundaries"

# Browse an existing run locally
uv run --frozen natsuhat view
```

Results are written under `natsuhat_runs/<run-name>/`, including `run.json`,
`penetration_test_report.md`, `vulnerabilities.json`, per-finding Markdown, and
`findings.sarif`. The viewer supports findings, agent transcripts, and local run
history without email verification.

Headless exit codes: `0` no findings, `1` fatal error, `2` findings found. A clean
exit does not guarantee complete coverage: check the run status, scope, and budget.

## Dashboard codebase scans

Start `uv run --frozen natsuhat view --manage-scans` and use **Pentest → New scan**.
Dashboard scans use **E2B only**. Set `E2B_API_KEY` on the server and check
availability in **Settings → Sandbox → E2B**. The only template source is
`nayakas-project/natsuhat-runner:stable`; environment and saved template overrides
are ignored. Each new scan pins the published release, independent of local
dashboard changes, and verifies its manifest before sending LLM credentials.
Docker is preinstalled inside E2B, not required on the dashboard server.
Choose a target URL, domain, IP, a connected GitHub repository, or a ZIP codebase.
Every scan still requires authorization and an LLM budget. Uploading or connecting
a repository never starts a scan.

ZIPs need no Git history. Limits are 100 MiB compressed, 500 MiB extracted, and
20,000 entries. Unsafe paths, links, encrypted archives, and duplicate destinations
are rejected. Git metadata and macOS archive metadata are removed before storage
or transfer. Submodules and Git LFS pointers produce warnings; their contents are
not fetched automatically. Review archives for secrets before uploading.

For GitHub, register a self-hosted GitHub App with **Contents: read-only** and
**Metadata: read-only**, no webhooks, and automatic user authorization during
installation disabled. Set both its **Setup URL** and OAuth **Callback URL** to
`https://your-dashboard.example/api/github/callback`. Enable **Redirect on update**
for the Setup URL so existing installations can reconnect. Configure these on the
dashboard server through your secret manager, then restart it:

- `NATSUHAT_GITHUB_APP_ID`: the App ID, not the client ID.
- `NATSUHAT_GITHUB_PRIVATE_KEY_PATH`: an RSA PEM key file outside scan sources;
  protect it with owner-only permissions.
- `NATSUHAT_GITHUB_CLIENT_ID` and `NATSUHAT_GITHUB_CLIENT_SECRET`: App OAuth credentials.
- `NATSUHAT_GITHUB_CALLBACK_URL`: the same stable HTTPS callback URL configured above.

Open **Integrations → GitHub**, install the App on selected repositories, authorize
your GitHub account, then refresh and connect repositories. GitHub installation
access and user repository access must both permit the connection. Branches/tags
resolve to exact commits before scanning. Disconnecting locally blocks new GitHub
scans; uninstall the App on GitHub to revoke its installation access.

Selecting a repository and ref checks commit availability before submission.
Empty repositories show a warning and disable **Start scan**. After pushing code,
use **Check again**. Checks do not download source or start scans; source access
and the current commit are checked again when submitting.

Sources are stored per workspace under `~/.natsuhat/dashboard/`, outside results
and scan mounts. Each scan gets an immutable snapshot and a disposable writable
copy; resumed scans use the same snapshot. Unused uploads expire after 24
hours and are removed on the next upload or server start. Deleting a scan removes
its local source snapshot. E2B scans explicitly transfer code to the
provider's retained sandbox; deleting local results does not delete that sandbox.
GitHub credentials never go to runners. Code excerpts can reach the configured LLM.

Stopped or interrupted dashboard scans with saved agent checkpoints show
**Resume scan**. Confirm authorization and the total LLM budget, including prior
spending, to continue the same run and conversation history. Remote resumes use
the retained E2B sandbox, not a new VM; provider charges resume. Source
scans use the original snapshot. Completed scans and scans without checkpoints
cannot be resumed. Resuming does not restore terminated commands or network sessions.

## Configuration

Use `NATSUHAT_*` environment variables for application settings. Configuration is
stored under `~/.natsuhat/`; previous product configuration is not imported.

- `NATSUHAT_LLM`: model provider and model ID.
- `LLM_API_KEY`: provider API key.
- `LLM_API_BASE`: optional custom model endpoint.
- `NATSUHAT_IMAGE`: sandbox image, default `natsuhat-sandbox:local`.
- `~/.natsuhat/mcp-servers.json`: optional MCP server configuration.

ChatGPT subscription authentication is available through
`uv run --frozen natsuhat auth login chatgpt`. This authenticates with the model
provider, not a hosted pentesting platform.

## Deploy on a VPS

For a Railway-style workflow on your own server, use
[Komodo with the included Docker deployment](docs/deployment-vps.md). It supports
CI-gated deployments after pushes to `main`, persistent results/settings, Caddy HTTPS,
and container health checks. Automation is opt-in. Upgrades currently restart the
single dashboard and stop active scans; this is **not zero-downtime deployment**.

## Documentation and development

Documentation lives in [`docs/`](docs/). Coding-agent workflows live in
[`skills/`](skills/); start with [`AGENTS.md`](AGENTS.md).

```bash
uv run --frozen pytest -q
make check-all
make tui-test tui-lint tui-build
make viewer
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidance.

**Authorized use only.** Only test systems you own or have explicit permission to
test, within the agreed scope. Natsuhat is provided as-is under the
[Apache License 2.0](LICENSE). Existing copyright and license notices are retained.
