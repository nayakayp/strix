FROM golang:1.24.13-bookworm AS go
FROM ghcr.io/astral-sh/uv:0.9.26 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=go /usr/local/go /usr/local/go
COPY --from=uv /uv /usr/local/bin/uv
ENV PATH="/usr/local/go/bin:$PATH" UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY scripts/tui_sidecar_hook.py scripts/docker.sh ./scripts/
COPY containers ./containers
COPY natsuhat ./natsuhat
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --uid 10001 --create-home natsuhat && \
    mkdir -p /var/lib/natsuhat/config /var/lib/natsuhat/workspace && \
    chown -R natsuhat:natsuhat /var/lib/natsuhat
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/var/lib/natsuhat/config \
    PYTHONUNBUFFERED=1
USER natsuhat
WORKDIR /var/lib/natsuhat/workspace
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/capabilities', timeout=3).close()"
CMD ["natsuhat", "view", "--manage-scans", "--no-open", "--host", "0.0.0.0", "--port", "8080"]
