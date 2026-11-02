"""Workspace runner selection and read-only remote image preflight checks."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.remote import default_runner, runner_revision, runners
from natsuhat.utils.secret_files import dashboard_profiles_path, write_secret_text


if TYPE_CHECKING:
    from pathlib import Path


IMAGE_ENV = {"e2b": "NATSUHAT_E2B_TEMPLATE", "daytona": "NATSUHAT_DAYTONA_SNAPSHOT"}


def _e2b_metadata(name: str) -> dict[str, Any] | None:
    from e2b import ConnectionConfig, Template  # noqa: PLC0415 - optional provider SDK
    from e2b.template.types import BuildInfo  # noqa: PLC0415

    family, separator, tag = name.partition(":")
    config = ConnectionConfig()
    response = httpx.get(
        f"{config.api_url}/templates",
        headers={"X-API-Key": str(config.api_key)},
        timeout=15,
    )
    response.raise_for_status()
    for template in response.json():
        aliases = template.get("aliases", []) + template.get("names", [])
        if family not in [template["templateID"], *aliases]:
            continue
        result: dict[str, Any] = {
            "id": template["templateID"],
            "names": aliases,
            "available": template.get("buildStatus") == "ready",
        }
        if not separator:
            return result
        tags = Template.get_tags(template["templateID"], request_timeout=15)
        selected = next((item for item in tags if item.tag == tag), None)
        if selected is None:
            return None
        revisions = {
            item.tag
            for item in tags
            if item.build_id == selected.build_id and re.fullmatch(r"rev-[a-f0-9]{64}", item.tag)
        }
        if len(revisions) != 1:
            raise JobError("Selected E2B tag must have one immutable content-revision tag.")
        release = revisions.pop()
        status = Template.get_build_status(
            BuildInfo(template["templateID"], selected.build_id, name, name), request_timeout=15
        )
        return {
            **result,
            "available": status.status.value == "ready",
            "build_id": selected.build_id,
            "revision": release.removeprefix("rev-"),
            "resolved_template": f"{template['templateID']}:{release}",
            "release": f"{family}:{release}",
        }
    return None


def image_metadata(provider: str, name: str) -> dict[str, Any] | None:
    """Read provider metadata only; never create, resume, or build a VM."""
    if provider == "e2b":
        return _e2b_metadata(name)
    from daytona import Daytona, DaytonaConfig, DaytonaNotFoundError  # noqa: PLC0415

    client = Daytona(DaytonaConfig(otel_enabled=False))
    try:
        snapshot = client.snapshot.get(name)
    except DaytonaNotFoundError:
        return None
    return {
        "id": snapshot.id,
        "names": [snapshot.name],
        "available": snapshot.state == "active",
    }


class RunnerSettings:
    def __init__(self, workspace: Path) -> None:
        self.path = dashboard_profiles_path(workspace).with_name("runner-settings.json")
        self._lock = threading.RLock()
        self._checks: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    def _read(self) -> dict[str, Any]:
        if self.path.exists():
            return dict(json.loads(self.path.read_text()))
        return {"default_runner": None, "e2b_template": "", "daytona_snapshot": ""}

    def default(self) -> str:
        with self._lock:
            return str(self._read()["default_runner"] or default_runner())

    def image(self, provider: str, required: str) -> tuple[str, str]:
        data = self._read()
        field = "e2b_template" if provider == "e2b" else "daytona_snapshot"
        if data[field]:
            return str(data[field]), "dashboard"
        if value := os.getenv(IMAGE_ENV[provider]):
            return value, "environment"
        return required, "automatic"

    def public(self) -> dict[str, Any]:
        with self._lock:
            revision = runner_revision()
            required = "natsuhat-ready-" + revision[:16]
            images: list[dict[str, Any]] = []
            for provider in runners()[1:]:
                name, source = self.image(provider["id"], required)
                cached = self._checks.get((provider["id"], name, revision))
                images.append(
                    {
                        **provider,
                        "template": name,
                        "selection_source": source,
                        "check": cached[1]
                        if cached and time.monotonic() - cached[0] < 60
                        else None,
                    }
                )
            return {
                **self._read(),
                "effective_default_runner": self.default(),
                "required_revision": revision,
                "required_template": required,
                "images": images,
            }

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"default_runner", "e2b_template", "daytona_snapshot"}:
            raise JobError("Provide a default runner, E2B template, and Daytona snapshot.")
        if body["default_runner"] is not None and body["default_runner"] not in (
            "local",
            "e2b",
            "daytona",
        ):
            raise JobError("Choose automatic, Local Docker, E2B, or Daytona.")
        for field in ("e2b_template", "daytona_snapshot"):
            value = body[field]
            if not isinstance(value, str) or (
                value
                and (
                    "://" in value
                    or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}", value)
                )
            ):
                raise JobError("Use a template/snapshot name or ID, not a URL or whitespace.")
        with self._lock:
            write_secret_text(self.path, json.dumps(body, indent=2))
            self._checks.clear()
            return self.public()

    def check(self, provider: Any, *, refresh: bool = True) -> dict[str, Any]:
        if provider not in ("e2b", "daytona"):
            raise JobError("Choose E2B or Daytona to check remote image availability.")
        with self._lock:
            revision = runner_revision()
            required = "natsuhat-ready-" + revision[:16]
            name, _ = self.image(provider, required)
            key = (provider, name, revision)
            cached = self._checks.get(key)
            if not refresh and cached and time.monotonic() - cached[0] < 60:
                return cached[1]
            result: dict[str, Any] = {
                "template": name,
                "checked_at": datetime.now(UTC).isoformat(),
                "availability": "unconfigured",
                "compatibility": "unverified",
                "template_id": None,
                "build_id": None,
                "revision": None,
                "resolved_template": None,
                "release": None,
                "message": f"Set {provider.upper()}_API_KEY on the dashboard server first.",
            }
            if os.getenv(f"{provider.upper()}_API_KEY"):
                self._inspect(provider, name, revision, result)
            self._checks[key] = (time.monotonic(), result)
            return result

    @staticmethod
    def _inspect(provider: str, name: str, revision: str, result: dict[str, Any]) -> None:
        required = "natsuhat-ready-" + revision[:16]
        try:
            metadata = image_metadata(provider, name)
        except JobError as exc:
            result.update(availability="unavailable", message=str(exc))
            return
        except Exception:  # noqa: BLE001 - never expose SDK errors containing credentials
            result.update(
                availability="error",
                message="Could not read provider metadata. "
                "Check credentials, connectivity, and provider access.",
            )
            return
        if metadata is None:
            result.update(
                availability="missing",
                message="Selected template/snapshot was not found in this account.",
            )
            return
        result.update(
            availability="available" if metadata["available"] else "unavailable",
            template_id=metadata["id"],
            **{
                key: metadata.get(key)
                for key in ("build_id", "revision", "resolved_template", "release")
            },
        )
        versions = {
            candidate.rsplit("/", 1)[-1]
            for candidate in [name, *metadata["names"]]
            if re.fullmatch(r"(?:[^/]+/)?natsuhat-ready-[a-f0-9]{16}", candidate)
        }
        if (metadata.get("revision") and metadata["revision"] != revision) or (
            not metadata.get("revision") and versions and required not in versions
        ):
            result.update(
                compatibility="incompatible",
                message="Published version does not match this application. "
                "Build a matching image before scanning.",
            )
        elif not metadata["available"]:
            result["message"] = "The image exists but the provider does not report it ready."
        else:
            result.update(
                compatibility="revision_matches"
                if metadata.get("revision")
                else ("name_matches" if required in versions else "unverified"),
                message=(
                    "Release tag matches the required application revision."
                    if metadata.get("revision")
                    else (
                        "Versioned name matches. Image contents are still unverified."
                        if required in versions
                        else "Image exists, but metadata cannot verify its application version."
                    )
                )
                + " The worker checks the full manifest before sending LLM credentials.",
            )

    def preflight(self, provider: str) -> str | None:
        if provider == "local":
            return None
        result = self.check(provider, refresh=False)
        if result["availability"] != "available" or result["compatibility"] == "incompatible":
            raise JobError(
                f"{result['message']} Open Sandbox runners settings to check the selected image.",
                409,
            )
        return str(result["resolved_template"] or result["template"])
