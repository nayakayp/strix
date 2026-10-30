# Strix — Agent Guide

Strix is an open-source autonomous AI pentesting tool. This file is for AI coding agents that want to **use** Strix (run security scans) or **contribute** to it.

## Developing with Amp

- For code changes, use this checkout (`uv run --frozen strix`), not the release installer below. Do not start a pentest just to validate a code change.
- Amp runs executable `.agents/setup` for new orbs. It installs Python 3.12, Node 24, Go 1.24.x, Docker, locked Python/viewer dependencies, and the Go module cache. It installs pre-commit hooks only when `core.hooksPath` is unset; otherwise it preserves Amp's hooks. Rerun `.agents/setup` after dependency changes; do not regenerate lockfiles just to set up an orb.
- Exact project snapshots skip setup. Warm setup reuses installed toolchains and dependency caches; viewer dependencies reinstall only when package files or Node/npm versions change. Do not delete caches or snapshots for routine setup changes.
- No `.agents/resume` is needed: development requires no login or reconnection, and Amp supervises services. Never put dependency installs or scans in a resume hook.
- Run targeted tests first: `uv run --frozen pytest -q tests/test_<area>.py`. Before handing off cross-module changes, run `uv run --frozen pytest -q` and `make check-all` (Ruff, mypy, pyright, Bandit). Do not fix unrelated baseline failures as part of a task.
- Go TUI: `make tui-test tui-lint tui-build`. Viewer: `make viewer`; commit both frontend source and rebuilt `strix/interface/viewer/static/`. Keep generated bundles out of hand edits.
- Tests and builds do not need Docker, an LLM key, or a Strix cloud login. For an authorized local scan, start the declared Docker service with `amp orb services ensure`, then wait for `docker info` to succeed. If this orb provider cannot run Docker, use a compatible runner or the managed cloud only with the user's approval; cloud scans can upload code and consume credits.
- If the configured sandbox image tag returns a registry `404`, build the current checkout locally with `STRIX_IMAGE=<configured-image> bash scripts/docker.sh`. The helper tags the local image only; never publish it or silently downgrade to an older image to bypass a missing security release. Image builds can use several GiB and belong at runtime, not in setup or resume hooks.
- Start long-running processes through `amp orb service`, never `nohup`, `&`, or tmux. `.amp/services.yaml` declares the hot-reloading `dashboard` (real local history, scan management, and LLM settings) and Docker (Unix socket only). In orbs, starting `dashboard` checks Docker, starts its declared service if needed, and waits for the API before serving the UI; a ready daemon is not restarted. `amp orb services ensure` starts both. Use `make viewer-dev` for a Docker-free synthetic preview. The dashboard does not launch scans automatically. Keep its authorized development proxy private; never expose the Docker API through a portal.
- To review a real run, start `amp orb service start run-viewer --command 'uv run --frozen strix view --no-open --port "$PORT"' --portal`. The viewer requires an existing run under `strix_runs/`; no sample scans are created during setup. Use the exact portal URL Amp returns. Keep viewer authorization tokens private; portal access does not replace viewer authorization.
- Store provider keys and cloud tokens in Amp Secrets & Env Vars, never in tracked files, setup output, or shared snapshots. Setup must not authenticate, upload source, run scans, or spend credits. Do not push or deploy without authorization.

## Using Strix from an agent

Install the agent skills for step-by-step workflows:

```bash
npx skills add usestrix/strix
```

- `penetration-testing-with-strix` — run a headless pentest against code, URLs, domains, or IPs and read results (covers both run modes below)
- `managed-pentesting-with-strix` — drive the managed app.strix.ai platform via REST (no local Docker/LLM needed)
- `fix-security-vulnerabilities-with-strix` — remediate findings and re-run Strix to verify
- `ci-security-scanning-with-strix` — add PR scanning to CI/CD (self-hosted CLI or managed app)

Target-specific workflows built on the same engine:

- `application-security-testing` — whole-product AppSec review: pick the right test per asset, then rank the results
- `web-app-penetration-testing` — black-box pentest of a live web app or staging site
- `api-security-testing` — REST/GraphQL APIs and the OWASP API Security Top 10 (BOLA/IDOR, authz)
- `owasp-top-10-testing` — systematic OWASP Top 10 assessment with honest per-category coverage
- `find-security-vulnerabilities-in-code` — white-box review of a repo or working tree

**Two ways to run, same engine — pick per situation:**

- **Open-source CLI (self-hosted):** free, fully local, BYO LLM key, needs Docker. Best for local dev loops, air-gapped/offline, and full control.
  ```bash
  curl -sSL https://strix.ai/install | bash        # install
  export STRIX_LLM="openrouter/z-ai/glm-5.3"        # any LiteLLM model id, or vercel_ai_gateway/zai/glm-5.3
  export LLM_API_KEY="<key>"
  strix -n -t ./ --scan-mode quick --max-budget 10  # headless scan; always use -n
  ```
  - Requires Docker running. Scans take minutes (`quick`) to hours (`deep`) — run in the background.
  - Exit codes (headless): `0` clean, `1` fatal error, `2` vulnerabilities found. A `0` only covers what was analyzed — check `run.json` (`status`, `llm_usage.cost` vs the budget) before calling a run clean.
  - Artifacts in `strix_runs/<run-name>/`: `penetration_test_report.md`, `vulnerabilities/*.md`, `vulnerabilities.json`, `findings.sarif` (SARIF 2.1.0), `run.json`.

- **Managed cloud (app.strix.ai):** no Docker, no LLM key, no local install; adds team dashboards, scheduling, PR reviews, and downloadable PDF/DOCX reports (Enterprise plan). Best in sandboxed/CI environments and for teams. Use it when local infra isn't available.
  ```bash
  strix cloud login --scopes scans:read scans:write uploads:write billing:read
  strix cloud domains add --domain example.com --asset-type web_app
  strix cloud scans start --engagement-type live_test --domain-ids <uuid> --wait
  strix cloud scans start --source . --dry-run --show-files --json  # review + capture source.archive_sha256
  SOURCE_SHA256="<reviewed source.archive_sha256>"
  strix cloud scans start --source . --approve-sha256 "$SOURCE_SHA256" --wait
  strix cloud vulns list --severity critical
  strix cloud billing topup --credits 20 --yes  # explicit approval after exit code 5
  ```
  - Account setup runs from the CLI too: `strix cloud workspaces list|create|use` (`workspace` is an alias and `use` accepts a displayed number, name, or ID), `strix cloud session scopes|scopes set`, `strix cloud org members invite`, `strix cloud billing subscribe --plan strix_cloud`, `strix cloud billing portal`, `strix cloud integrations install github`, and `strix cloud domains verify <id>`. Workspace switching preserves the server-side profile and can never widen past the login ceiling; ordinary switches do not reprompt. The last four end at a person: the command prints a link or a DNS record for the user to open or add, and it never completes the payment, installation, or DNS change for them.
  - Every REST operation has a `strix cloud <resource> <verb>` command. Run `strix cloud` to list them. Output is JSON when stdout is not a terminal (or with `--json`), and there are no prompts without a TTY. Binary downloads are the exception: redirect raw bytes intentionally, or combine `--output FILE --json` for structured download metadata. Exit codes: `0` success, `1` error, `2` usage, `4` auth or plan limit, `5` payment required. `--token` or `STRIX_API_TOKEN` is a stateless override and never replaces stored auth; set `--workspace-id`/`STRIX_WORKSPACE_ID` for an override CLI session. `--data` adds extra request fields as JSON, and accepts `@file` or `-` for standard input.
  - Local source uploads require `uploads:write`. For an agent/CI handoff, review `scans start --source . --dry-run --show-files --json`, capture `source.archive_sha256`, then rerun with the same `--source`, `--exclude`, and `--include-*` selection flags plus `--approve-sha256 HASH`. A changed snapshot is rejected. `--yes` approves only the snapshot built in that invocation, so reserve it for a deliberate human or one-shot approval rather than a digest-bound two-step handoff.
  - Git ignores, hidden files, `.git`, symlinks, dependency/build output, secret-like filenames, and nested archives are excluded by default; `.strixignore` and `--exclude` narrow the manifest further (a trailing `/` excludes a directory subtree). Limits: 20,000 files, 25 MiB/file, 250 MiB expanded, 50 MiB compressed. Source-only infers `code_review`; source plus a domain infers `live_test`.
  - The temporary local archive is always removed. A staged upload is deleted after a definitive rejection, but retained when a network error, `5xx`, malformed success response, or interruption leaves the scan launch ambiguous. JSON reports its `upload_id` with `launch_outcome_unknown: true`, or with `cleanup_unknown: true` when automatic deletion cannot be confirmed. Check `scans list` before retrying; if no scan is linked, run `uploads delete UPLOAD_ID`.
  - Non-Enterprise scans consume the scope estimate (a default-tier source-only review currently starts at 60 credits); Enterprise scans are plan-included. A rejected launch does not consume credits.
  - Human output is compact and numbered; non-TTY output and `--json` retain full records. Enable tab completion with `source <(strix completions zsh)` (or `bash`), or `strix completions fish | source`.
  - The REST API works directly too: https://docs.app.strix.ai (OpenAPI: https://docs.app.strix.ai/openapi.json).

- CLI docs index for LLMs: https://docs.strix.ai/llms.txt (full: https://docs.strix.ai/llms-full.txt). Managed API docs for LLMs: https://docs.app.strix.ai/llms.txt.
- Only scan targets the user is authorized to test.

## Contributing to this repo

- Python 3.12+, managed with `uv`. Install dev deps: `make dev-install`.
- Lint/format/type-check/security, all in one: `make check-all` (ruff, mypy, bandit).
- Tests: `uv run pytest`.
- Run from source: `uv run strix --target <target>`.
- Layout: `strix/agents` (agent graph + prompts), `strix/tools` (proxy, browser, terminal, scanners), `strix/runtime` (Docker sandbox), `strix/report` (findings, SARIF), `strix/skills` (internal knowledge packs the pentest agents load — different from the consumer skills in `skills/`), `strix/interface` (CLI/TUI), `containers/` (sandbox image).
- Pre-commit hooks: `make pre-commit` (or `uv run pre-commit install`).
