"""Host-only, workspace-scoped provider profiles. Keys are write-only to clients."""

from __future__ import annotations

import json
import math
import threading
import uuid
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from strix.utils.secret_files import (
    dashboard_profiles_path,
    dashboard_secrets_dir,
    write_secret_text,
)


if TYPE_CHECKING:
    from pathlib import Path


class JobError(ValueError):
    """A local management request cannot be fulfilled, with a public status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _catalog(payload: Any, kind: str) -> dict[str, dict[str, Any]]:
    rows = payload["models" if kind == "ollama" else "data"]
    if not isinstance(rows, list):
        raise TypeError("Invalid model list")
    models: dict[str, dict[str, Any]] = {}
    for row in cast("list[dict[str, Any]]", rows):
        identifier = row.get("name" if kind == "ollama" else "id")
        if not isinstance(identifier, str) or not identifier:
            continue
        entry: dict[str, Any] = {"id": identifier}
        if kind == "openrouter" and isinstance(row.get("pricing"), dict):
            rates = cast("dict[str, Any]", row["pricing"])
            converted = {
                name: float(Decimal(str(rates[key])) * 1_000_000)
                for name, key in (
                    ("input", "prompt"),
                    ("output", "completion"),
                    ("cached_input", "input_cache_read"),
                )
                if rates.get(key) is not None
            }
            if all(math.isfinite(v) and v >= 0 for v in converted.values()):
                entry["prices"] = converted
        models[identifier] = entry
    return models


class Prices(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cached_input: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def paired_rates(self) -> Prices:
        if (self.input is None) != (self.output is None) or (
            self.cached_input is not None and self.input is None
        ):
            raise ValueError("Set both input and output prices, or leave all prices blank.")
        return self


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["openai", "openrouter", "anthropic", "ollama", "custom"]
    base_url: str = Field(max_length=2048)
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(default="", max_length=300)
    prices: dict[str, Prices] = Field(default_factory=dict, max_length=1000)

    @field_validator("name", "model", "api_key")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed.")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials, query, or fragment.")
        return value


def _parse_provider(body: dict[str, Any], existing: dict[str, Any] | None) -> Provider:
    payload = dict(body)
    clear_key = payload.pop("clear_api_key", False)
    if payload.get("id") and existing is None:
        raise JobError("Unknown provider.", 404)
    if existing and existing["api_key"] and not payload.get("api_key") and not clear_key:
        if (
            payload.get("kind") != existing["kind"]
            or payload.get("base_url") != existing["base_url"]
        ):
            raise JobError(
                "Connection changed. Re-enter the API key or choose Remove saved API key."
            )
        payload["api_key"] = existing["api_key"]
    try:
        provider = Provider.model_validate(payload)
    except ValidationError:
        # Validation errors can contain submitted secrets; never return them.
        raise JobError(
            "Invalid provider. Check name, URL, model, and nonnegative prices. "
            "Set both input and output prices, or leave all prices blank."
        ) from None
    if not provider.name:
        raise JobError("Provide a connection name.")
    return provider


class LlmProfiles:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @classmethod
    def for_workspace(cls, workspace: Path) -> LlmProfiles:
        """Migrate only while holding the workspace's scan-manager owner lock."""
        root = dashboard_secrets_dir()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        profiles = cls(dashboard_profiles_path(workspace))
        profiles.path.parent.mkdir(mode=0o700, exist_ok=True)
        profiles.path.parent.chmod(0o700)
        if profiles.path.exists():
            profiles.path.chmod(0o600)
        legacy = workspace / ".strix" / "llm-providers.json"
        temporary = legacy.with_suffix(".json.tmp")
        source = legacy if legacy.exists() else temporary
        if source.exists():
            data = cls(source)._read()
            if profiles.path.exists() and profiles._read() != data:
                raise JobError(
                    "Conflicting dashboard LLM profiles. Neither copy was removed; "
                    "reconcile the host and workspace profiles before restarting.",
                    409,
                )
            if not profiles.path.exists():
                profiles._write(data)
            if profiles._read() != data:
                raise JobError("Could not verify migrated LLM profiles; workspace copy retained.")
            legacy.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
        return profiles

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"providers": [], "selected_id": None}
        return dict(json.loads(self.path.read_text(encoding="utf-8")))

    def _write(self, data: dict[str, Any]) -> None:
        write_secret_text(self.path, json.dumps(data, indent=2))

    def public(self) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            data["providers"] = [
                {
                    **{k: v for k, v in p.items() if k != "api_key"},
                    "has_api_key": bool(p["api_key"]),
                }
                for p in data["providers"]
            ]
            return data

    def update(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            identifier = body.get("id")
            existing = next((p for p in data["providers"] if p["id"] == identifier), None)
            if action == "providers":
                provider = _parse_provider(body, existing)
                data["providers"] = [p for p in data["providers"] if p["id"] != provider.id]
                data["providers"].append(provider.model_dump())
                if data["selected_id"] == provider.id and not provider.model:
                    raise JobError("The default provider must have a selected model.")
            elif action == "select":
                if identifier is not None and (existing is None or not existing["model"]):
                    raise JobError("Save a provider with a model before selecting it.")
                if (
                    existing
                    and existing["kind"] not in {"ollama", "custom"}
                    and not existing["api_key"]
                ):
                    raise JobError("Save an API key before using this provider for scans.")
                data["selected_id"] = identifier
            elif action == "delete":
                if existing is None:
                    raise JobError("Unknown provider.", 404)
                data["providers"] = [p for p in data["providers"] if p["id"] != identifier]
                if data["selected_id"] == identifier:
                    data["selected_id"] = None
            else:
                raise JobError("Unknown settings operation.", 404)
            self._write(data)
            return self.public()

    def selected(self) -> dict[str, Any] | None:
        """A complete, immutable-for-the-child LLM override for the next scan."""
        with self._lock:
            data = self._read()
            saved = next((p for p in data["providers"] if p["id"] == data["selected_id"]), None)
            if saved is None:
                return None
            provider = Provider.model_validate(saved)
            if not provider.api_key and provider.kind not in {"ollama", "custom"}:
                raise JobError("The default provider needs an API key. Update LLM settings first.")
            prefix = "openai" if provider.kind == "custom" else provider.kind
            prices = provider.prices.get(provider.model, Prices())
            return {
                "model": f"{prefix}/{provider.model}",
                "api_key": provider.api_key or "not-required",
                "api_base": provider.base_url,
                "api_type": "chat_completions" if provider.kind == "custom" else None,
                "extra_headers": None,
                "extra_body": None,
                "input_cost_per_mtok": prices.input,
                "output_cost_per_mtok": prices.output,
                "cached_input_cost_per_mtok": prices.cached_input,
            }

    def models(self, identifier: Any) -> list[dict[str, Any]]:
        with self._lock:
            provider = next((p for p in self._read()["providers"] if p["id"] == identifier), None)
            if provider is None:
                raise JobError("Save the provider connection before fetching models.", 404)
        headers = {"Accept": "application/json"}
        if provider["api_key"]:
            headers["Authorization"] = f"Bearer {provider['api_key']}"
        kind = provider["kind"]
        suffix = "/models"
        if kind == "anthropic":
            headers = {"x-api-key": provider["api_key"], "anthropic-version": "2023-06-01"}
            suffix = "/v1/models"
        elif kind == "ollama":
            suffix = "/api/tags"
        models: dict[str, dict[str, Any]] = {}
        params: dict[str, str | int] = {"limit": 1000} if kind == "anthropic" else {}
        try:
            # No redirects: a provider must never forward its credential to another host.
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                for _ in range(20):
                    response = client.get(
                        provider["base_url"] + suffix, headers=headers, params=params
                    )
                    response.raise_for_status()
                    payload = response.json()
                    models.update(_catalog(payload, kind))
                    if kind != "anthropic" or not payload.get("has_more"):
                        return sorted(models.values(), key=lambda m: m["id"])
                    cursor = payload.get("last_id")
                    if (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor == params.get("after_id")
                    ):
                        raise ValueError("Invalid pagination")  # noqa: TRY301 - protocol guard
                    params["after_id"] = cursor
        except httpx.HTTPStatusError as exc:
            raise JobError(
                f"Provider model request failed (HTTP {exc.response.status_code}).", 502
            ) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            raise JobError(
                "Could not fetch models. Check the base URL, API key, and provider response.", 502
            ) from None
        raise JobError("Provider returned too many model pages. Enter a model ID manually.", 502)
