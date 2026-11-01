# Natsuhat

Self-hosted AI penetration testing for applications, APIs, and source code.
Natsuhat coordinates security agents, runs tools in a Docker sandbox, and writes
findings with reproduction steps and remediation guidance.

**Local-only:** no hosted account, cloud sign-up, billing integration, email relay,
or upstream usage analytics. Scan artifacts stay on your machine. Configured LLM,
search, and MCP providers still receive the data needed for their requests.

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
