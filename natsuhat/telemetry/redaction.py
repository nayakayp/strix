"""Keep provider credentials and custom request values out of diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

from natsuhat.config import loader


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        return [
            text
            for item in cast("Mapping[object, object]", value).values()
            for text in _strings(item)
        ]
    if isinstance(value, list):
        items = cast("list[object]", value)
        return [text for item in items for text in _strings(item)]
    return []


def provider_secrets(connection: Mapping[str, Any]) -> list[str]:
    return _strings(
        [
            connection.get("api_key"),
            connection.get("extra_headers"),
            connection.get("extra_body"),
        ]
    )


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    if secrets is None:
        try:
            settings = loader.load_settings()
        except (ValueError, OSError):
            # Invalid configuration must still be reportable.
            return text
        secrets = provider_secrets(settings.llm.model_dump()) + provider_secrets(
            settings.dedupe.model_dump()
        )
    patterns = {
        value for secret in secrets if secret for value in (secret, json.dumps(secret)[1:-1])
    }
    if not patterns:
        return text
    # One pass avoids redacting the replacement itself for short header values.
    return re.sub(
        "|".join(re.escape(value) for value in sorted(patterns, key=len, reverse=True)),
        "[redacted]",
        text,
    )
