# Contributing to Natsuhat

Thank you for your interest in contributing to Natsuhat! This guide will help you get started with development and contributions.

## 🚀 Development Setup

### Prerequisites

- Python 3.12+
- Latest Go 1.24.x patch (only for Bubble Tea TUI development and release artifacts)
- Docker (running)
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Git

### Local Development

1. **Clone the repository**
   ```bash
   git clone https://github.com/nayakayp/natsuhat.git
   cd natsuhat
   ```

2. **Install development dependencies**
   ```bash
   make setup-dev

   # or manually:
   uv sync
   uv run pre-commit install
   ```

3. **Configure your LLM provider**
   ```bash
   export NATSUHAT_LLM="openrouter/z-ai/glm-5.3"   # or vercel_ai_gateway/zai/glm-5.3
   export LLM_API_KEY="your-api-key"
   ```

4. **Run Natsuhat in development mode**
   ```bash
   uv run natsuhat --target https://example.com
   ```

### Dashboard UI development (no scan required)

The dashboard is the local run viewer. It uses React, TypeScript, Tailwind CSS
and Vite, with a Python HTTP server reading run artifacts.

With Python 3.12+, uv and Node.js 24 installed, run from the repository root:

```bash
uv sync --frozen
npm --prefix natsuhat/interface/viewer/frontend ci
make viewer-dev
```

Open the local URL printed by Vite. Edit
`natsuhat/interface/viewer/frontend/src/` for hot-reloading UI changes. Restart
`make viewer-dev` after editing Python code or the demo fixtures in
`scripts/viewer_dev.py`. Set `PORT` to choose a port (default: 5173).

The launcher creates disposable synthetic data: three runs, findings with
different severities, and a two-agent graph. It never scans a target, starts
Docker, uses an LLM, or writes to `natsuhat_runs/`. Telemetry is disabled. The Vite server proxies
the demo API with a server-side capability; do not expose this development server
to untrusted users or point its proxy at real scan data.

Past runs lists every run directory containing `run.json` alongside the launched
run, newest activity first. Select a row to open its findings and report; no email
verification is required. In normal use this is the current project's
`natsuhat_runs/`, not a search across the whole machine.

There is no remote report-email or feedback service. Live scan execution and
steering are unavailable in this synthetic preview.

In an Amp orb, dependencies are already prepared. Start a supervised preview:

```bash
amp orb service start viewer-dev --command 'make viewer-dev' --portal --title 'Natsuhat dashboard development'
```

Use the portal URL returned by Amp, not the sandbox-local Vite URL. No viewer
token needs to be copied into the browser for this synthetic preview.

For hot reload with **real local history, scan management, and LLM settings**,
run from the repository root:

```bash
uv run --frozen python scripts/viewer_dev.py --manage-scans
```

This mode reads `natsuhat_runs/` and persists provider profiles in
`~/.natsuhat/dashboard/<workspace-hash>/llm-providers.json`, outside the project;
existing workspace profiles migrate automatically. It works before the first
scan exists. It does not create demo records or launch scans automatically.
In Amp orbs (`AMP_ORB=1`), it checks Docker before serving the UI: a ready daemon
is left running; otherwise it starts the declared `docker` service and waits for
its API. Outside orbs,
start Docker yourself. Launching a scan also requires an LLM provider and explicit
authorization in the UI, and can incur LLM charges. Keep the development server
private: its server-side proxy authorizes API requests without a separate viewer
login. Do not use it as a public or multi-tenant deployment.

In Amp, `.amp/services.yaml` declares this mode as `dashboard`, with a portal and
an API health check. Starting or restarting it also ensures Docker is ready:

```bash
amp orb service restart dashboard
```

`amp orb services ensure` starts all declared services, including Docker. Amp
supervises the dashboard across process restarts and orb pause/resume; no resume
hook or generated portal hostname belongs in the repository. The declaration is
available in future orbs once these changes reach the project's default branch.
Provider credentials remain local and are never committed or installed by setup.
For a Docker-free, read-only preview, keep using `make viewer-dev` without
`--manage-scans`.

Build the sandbox locally from this checkout before starting a scan:

```bash
bash scripts/docker.sh local
```

This builds the default runtime image, `natsuhat-sandbox:local`.
`NATSUHAT_IMAGE` can select a different complete local image tag; it does not
push anything to a registry or launch a scan. Without a positional tag, the
helper builds `natsuhat-sandbox:dev`. The build context contains
only the Dockerfile, entrypoint, and proxy client, not provider keys or run data.
Building the tools takes time and several GiB of disk space; do this at runtime,
not in `.agents/setup` or `.agents/resume`. Each new orb needs the image pulled
or built in its own Docker daemon before a scan can use it.

Before shipping frontend changes, run `make viewer` and include both the source
and rebuilt `natsuhat/interface/viewer/static/` bundle. To inspect real results
instead, use `uv run --frozen natsuhat view <run-name>` with an existing run and keep
its authorization URL private.

### Development in Amp orbs

Amp automatically runs the executable `.agents/setup` on a fresh orb. It prepares
Python 3.12, checksum-verified Node 24 and Go 1.24.x toolchains, system build tools,
ShellCheck, Docker, and dependencies from the Python, npm, and Go lockfiles. It does
not start scans, download the large Natsuhat sandbox image, authenticate accounts, or
require API keys. It preserves any existing `core.hooksPath` (including Amp's hooks);
otherwise it installs the project's pre-commit hook.

Rerun `.agents/setup` after changing dependencies. It is safe to run repeatedly.
Amp snapshots the prepared filesystem; an exact snapshot skips setup entirely.
When setup runs on a stale snapshot, it reuses installed toolchains and Python/Go
caches. The viewer's `node_modules` is reused when its package files and Node/npm
versions match the last successful install; changed inputs trigger `npm ci`.
Toolchains are available to new login shells and supervised services without
activating a virtualenv. Use `uv run --frozen` for Python commands. No resume hook
is necessary; dependency installation belongs in setup and services are supervised.

Useful validation commands:

```bash
bash -n .agents/setup && shellcheck .agents/setup
uv run --frozen pytest -q
make check-all
make tui-test tui-lint tui-build
make viewer
```

Python tests, TUI checks, and viewer builds need no Docker daemon or paid services.
Setup installs Docker, but does not start a daemon inside the shared snapshot.
The managed dashboard starts it at runtime in each orb before accepting requests.
For a CLI scan without that dashboard, run `amp orb services ensure` to start the
Docker daemon declared in `.amp/services.yaml`, then wait for `docker info` to succeed.
Docker is available only over its local Unix socket, never through a portal.
Provider restrictions can still prevent containers from running; verify Docker
before starting a scan and do not silently substitute a paid cloud scan.

For a viewer portal after a run exists, follow **Developing with Amp** in
[`AGENTS.md`](AGENTS.md). Keep viewer authorization tokens private. Store LLM keys
in Amp **Secrets & Env Vars**, not repository files or setup scripts. Development
setup does not authenticate with providers or start scans.

These files configure future orbs once they reach the Amp project's default branch.
Until then, they apply only to the checkout where you run them.

## 📚 Contributing Skills

Skills are specialized knowledge packages that enhance agent capabilities. See [natsuhat/skills/README.md](natsuhat/skills/README.md) for detailed guidelines.

### Quick Guide

1. **Choose the right category** (`/vulnerabilities`, `/frameworks`, `/technologies`, etc.)
2. **Create a** `.md` file with your skill content
3. **Include practical examples** - Working payloads, commands, or test cases
4. **Provide validation methods** - How to confirm findings and avoid false positives
5. **Submit via PR** with clear description

## 🔧 Contributing Code

### Pull Request Process

1. **Create an issue first** - Describe the problem or feature
2. **Fork and branch** - Work from the `main` branch
3. **Make your changes** - Follow existing code style
4. **Write/update tests** - Ensure coverage for new features
5. **Run quality checks** - `make check-all` should pass
6. **Submit PR** - Link to issue and provide context

### PR Guidelines

- **Clear description** - Explain what and why
- **Small, focused changes** - One feature/fix per PR
- **Include examples** - Show before/after behavior
- **Update documentation** - If adding features
- **Pass all checks** - Tests, linting, type checking

### Code Style

- Follow PEP 8 with 100-character line limit
- Use type hints for all functions
- Write docstrings for public methods
- Keep functions focused and small
- Use meaningful variable names

## 🐛 Reporting Issues

When reporting bugs, please include:

- Python version and OS
- Natsuhat version
- LLMs being used
- Full error traceback
- Steps to reproduce
- Expected vs actual behavior

## 💡 Feature Requests

We welcome feature ideas! Please:

- Check existing issues first
- Describe the use case clearly
- Explain why it would benefit users
- Consider implementation approach
- Be open to discussion

## 🖥️ Local viewer SPA

`natsuhat view` serves a prebuilt web UI whose source lives in
`natsuhat/interface/viewer/frontend/` (a Vite + React project) and whose built output is
committed to `natsuhat/interface/viewer/static/` and shipped in the package. End users never
run a JS build. If you change anything under `natsuhat/interface/viewer/frontend/`, rebuild
and commit the output:

```bash
make viewer   # or: cd natsuhat/interface/viewer/frontend && npm ci && npm run build
```

Commit both the source change and the regenerated `natsuhat/interface/viewer/static/`.

## Package builds

Editable installs do not need Go; they run the TUI from source (`go run`).

Wheels always bundle the matching Go sidecar and are platform-specific:

```bash
make wheel
```

The build hook (`scripts/tui_sidecar_hook.py`) compiles the sidecar, embeds it as
`natsuhat/bin/natsuhat-tui`, and assigns the current platform tag. It requires Go
1.24.x or newer and fails rather than producing a wheel without the sidecar.
`scripts/build.sh` and `natsuhat.spec` are likewise strict for frozen PyInstaller
releases.

## 🤝 Community

- **Issues**: [GitHub Issues](https://github.com/nayakayp/natsuhat/issues)

## ✨ Recognition

We value all contributions! Contributors will be:
- Listed in release notes
- Thanked in our Discord
- Added to contributors list (coming soon)

---

**Questions?** Reach out on the repository issue tracker or create an issue. We're here to help!
