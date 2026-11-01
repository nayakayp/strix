# Natsuhat — Agent Guide

Natsuhat is a self-hosted autonomous AI pentesting tool. Only scan targets the
user owns or is explicitly authorized to test.

## Development

- Use this checkout: `uv run --frozen natsuhat`. Do not start a pentest to validate code changes.
- `.agents/setup` prepares Python 3.12, Node 24, Go 1.24.x, and locked dependencies. Rerun it after dependency changes; do not regenerate lockfiles for routine setup.
- Run targeted tests first: `uv run --frozen pytest -q tests/test_<area>.py`. For cross-module changes, run `uv run --frozen pytest -q` and `make check-all` (Ruff, mypy, pyright, Bandit). Do not fix unrelated baseline failures.
- Go TUI: `make tui-test tui-lint tui-build`.
- Viewer: `make viewer`; include frontend source and rebuilt `natsuhat/interface/viewer/static/`. Never hand-edit generated bundles.
- Dashboard development: `make viewer-dev` uses disposable synthetic data without Docker or LLM requests. The declared `dashboard` service opts into local scan management and LLM settings; starting scans requires explicit authorization and can incur provider charges. The development proxy authorizes API requests, so keep it private.
- Tests and builds do not need Docker, model keys, or a hosted account.
- For an authorized scan, start Docker with `amp orb services ensure`, wait for `docker info`, then build the local sandbox with `bash scripts/docker.sh local`. If Docker is unavailable, use a compatible runner; there is no managed-cloud fallback.
- The sandbox helper sends only Dockerfile inputs, never the whole checkout, as the build context. Build images at runtime, not during setup or resume.
- Run long-lived services with `amp orb service`, never `nohup`, `&`, or tmux. Do not expose Docker through a portal.
- To review an existing run: `amp orb service start run-viewer --command 'uv run --frozen natsuhat view --no-open --port "$PORT"' --portal`. Keep the viewer session token private; portal access does not replace viewer authorization.
- Keep provider keys in Amp Secrets & Env Vars. Setup must not authenticate, upload source, run scans, or spend credits. Do not push or deploy without authorization.
- No resume hook is needed. Preserve caches, snapshots, and existing Git hooks.

## Running an authorized scan

Natsuhat runs locally with Docker and your chosen model provider. There is no
cloud login, billing flow, telemetry collector, or remote report-email service.
Configured model, search, and MCP services still receive their requests.

```bash
uv sync --frozen
bash scripts/docker.sh local
export NATSUHAT_LLM="openai/gpt-5.4"
export LLM_API_KEY="<key>"
uv run --frozen natsuhat -n -t ./ --scan-mode quick --max-budget 10
```

Always use `-n` for agent-driven scans. Runs take minutes to hours; start them as
supervised services. Exit codes: `0` no findings, `1` fatal error, `2` findings.
Check `run.json` status, coverage, and budget before calling a result clean.
Artifacts live in `natsuhat_runs/<run-name>/`.

## Agent workflows

The `skills/` directory contains eight workflows for local scans, remediation,
CI/CD, application security, web apps, APIs, code review, and OWASP Top 10 testing.
Start with `skills/penetration-testing-with-natsuhat/SKILL.md`.

## Layout

Python 3.12+, managed with uv. `natsuhat/agents` owns agent orchestration and
prompts; `natsuhat/tools` owns tools; `natsuhat/runtime` owns the sandbox;
`natsuhat/report` owns findings and SARIF; `natsuhat/interface` owns the CLI,
Go TUI, and viewer. `natsuhat/skills` contains internal agent knowledge packs,
which are separate from the consumer workflows in `skills/`.
