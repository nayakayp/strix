"""Tests for LLM_EXTRA_HEADERS: custom default headers on OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import litellm
import pytest
from agents.models import _openai_shared
from agents.models.interface import ModelTracing

from natsuhat.config import loader
from natsuhat.config.loader import load_settings
from natsuhat.config.models import NatsuhatProvider, configure_sdk_model_defaults
from natsuhat.config.settings import DedupeSettings
from natsuhat.core.inputs import make_model_settings
from natsuhat.interface.viewer.llm_settings import LlmProfiles
from natsuhat.report.dedupe import _dedupe_model_settings, resolve_dedupe_model


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_ENV_KEYS = [
    "NATSUHAT_LLM",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "LLM_EXTRA_HEADERS",
    "LLM_EXTRA_BODY",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
]


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # SDK configuration assigns these directly, even when initially absent.
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(loader, "_cached", None)
    monkeypatch.setattr(loader, "_override", None)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "api_base", None)
    monkeypatch.setattr(_openai_shared, "_default_openai_key", None)
    monkeypatch.setattr(_openai_shared, "_use_responses_by_default", True)

    saved_headers = litellm.headers
    saved_client = _openai_shared.get_default_openai_client()
    _openai_shared._default_openai_client = None
    litellm.headers = None
    try:
        yield
    finally:
        litellm.headers = saved_headers
        _openai_shared.set_default_openai_client(saved_client)  # type: ignore[arg-type]


def test_extra_headers_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EXTRA_HEADERS", json.dumps({"X-A": "1", "X-B": "2"}))
    settings = load_settings()
    assert settings.llm.extra_headers == {"X-A": "1", "X-B": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["chat_completions", "responses", "litellm"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_profile_overrides_reach_http_without_leaking_to_dedupe(
    tmp_path: Path,
    route: str,
    streaming: bool,
) -> None:
    captured: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, dict(self.headers.items()), body))
            # An intentional rejection captures the real SDK HTTP request without
            # needing a model or token generation, for both streaming modes.
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"fixture captured","type":"invalid_request_error"}}'
            )

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    profiles = LlmProfiles(tmp_path / "providers.json")
    saved = profiles.update(
        "providers",
        {
            "name": "Netra fixture",
            "kind": "custom",
            "base_url": base,
            "model": "deepseek/deepseek-v4.1-flash",
            "api_key": "main-fixture-key",
            "extra_headers": {"X-Netra-Admin-Target": "amd-cloudrift-v41"},
            "extra_body": {"reasoning": {"enabled": False}, "uncensored": True},
        },
    )
    profiles.update("select", {"id": saved["providers"][0]["id"]})
    snapshot = profiles.selected()
    settings = load_settings()
    settings.llm = settings.llm.model_copy(update=snapshot)
    if route == "litellm":
        settings.llm.model = "litellm/" + settings.llm.model
    elif route == "responses":
        settings.llm.api_type = "responses"
    configure_sdk_model_defaults(settings)

    async def send(model: Any, model_settings: Any, *, stream: bool) -> None:
        kwargs = {
            "system_instructions": None,
            "input": "Hello!",
            "model_settings": model_settings,
            "tools": [],
            "output_schema": None,
            "handoffs": [],
            "tracing": ModelTracing.DISABLED,
            "previous_response_id": None,
            "conversation_id": None,
            "prompt": None,
        }
        with pytest.raises(Exception, match="fixture captured"):
            if stream:
                async for _ in model.stream_response(**kwargs):
                    pass
            else:
                await model.get_response(**kwargs)

    try:
        await send(
            NatsuhatProvider().get_model(settings.llm.model),
            make_model_settings(
                "max" if streaming else "high",
                model_name=settings.llm.model,
                extra_headers=settings.llm.extra_headers,
                extra_body=settings.llm.extra_body,
                has_tools=False,
            ),
            stream=streaming,
        )
        dedupe_name = "litellm/openai/judge" if route == "litellm" else "openai/judge"
        dedicated = DedupeSettings(
            NATSUHAT_DEDUPE_MODEL=dedupe_name,
            DEDUPE_LLM_API_KEY="judge-fixture-key",
            DEDUPE_LLM_API_BASE=base,
            DEDUPE_LLM_EXTRA_HEADERS={"X-Judge": "judge-only"},
            DEDUPE_LLM_EXTRA_BODY={"judge_flag": {"enabled": True}},
        )
        await send(
            resolve_dedupe_model(dedicated, dedupe_name),
            _dedupe_model_settings(dedicated, dedupe_name, 10),
            stream=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(captured) == 2
    path, headers, body = captured[0]
    headers = {k.lower(): v for k, v in headers.items()}
    assert path == ("/v1/responses" if route == "responses" else "/v1/chat/completions")
    assert headers["authorization"] == "Bearer main-fixture-key"
    assert headers["x-netra-admin-target"] == "amd-cloudrift-v41"
    assert body["model"] == "deepseek/deepseek-v4.1-flash"
    assert body["reasoning"] == {"enabled": False} and body["uncensored"] is True
    assert "reasoning_effort" not in body and "max_tokens" not in body
    assert body.get("stream", False) is streaming
    _, headers, body = captured[1]
    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["authorization"] == "Bearer judge-fixture-key"
    assert headers["x-judge"] == "judge-only" and "x-netra-admin-target" not in headers
    assert body["model"] == "judge" and body["judge_flag"] == {"enabled": True}
    assert "uncensored" not in body and "reasoning" not in body
