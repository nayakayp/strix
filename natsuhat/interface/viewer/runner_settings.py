"""Resolve the authoritative E2B stable release without provisioning a sandbox."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from natsuhat.interface.viewer.llm_settings import JobError
from natsuhat.interface.viewer.remote import DEFAULT_E2B_TEMPLATE, runners


if TYPE_CHECKING:
    from pathlib import Path


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
    if provider != "e2b":
        raise JobError("Dashboard scans use E2B only.")
    return _e2b_metadata(name)


class RunnerSettings:
    def __init__(self, _workspace: Path) -> None:
        self._lock = threading.RLock()
        self._checks: dict[str, tuple[float, dict[str, Any]]] = {}

    def default(self) -> str:
        return "e2b"

    def public(self) -> dict[str, Any]:
        with self._lock:
            images: list[dict[str, Any]] = []
            for provider in runners():
                cached = self._checks.get(provider["id"])
                images.append(
                    {
                        **provider,
                        "template": DEFAULT_E2B_TEMPLATE,
                        "selection_source": "release",
                        "check": cached[1]
                        if cached and time.monotonic() - cached[0] < 60
                        else None,
                    }
                )
            return {
                "e2b_template": DEFAULT_E2B_TEMPLATE,
                "effective_default_runner": self.default(),
                "images": images,
            }

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        if body != {"e2b_template": DEFAULT_E2B_TEMPLATE}:
            raise JobError(f"The E2B template is fixed to {DEFAULT_E2B_TEMPLATE}.")
        return self.public()

    def check(self, provider: Any, *, refresh: bool = True) -> dict[str, Any]:
        if provider != "e2b":
            raise JobError("Only E2B template checks are supported.")
        with self._lock:
            name = DEFAULT_E2B_TEMPLATE
            cached = self._checks.get(provider)
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
                self._inspect(provider, name, result)
            self._checks[provider] = (time.monotonic(), result)
            return result

    @staticmethod
    def _inspect(provider: str, name: str, result: dict[str, Any]) -> None:
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
                message="Selected E2B template was not found in this account.",
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
        if not metadata["available"]:
            result["message"] = "The stable release exists but is not ready."
        elif not metadata.get("revision") or not metadata.get("resolved_template"):
            result.update(
                compatibility="incompatible",
                message="The stable release does not identify a pinned application revision.",
            )
        else:
            result.update(
                compatibility="release_pinned",
                message="Stable release is ready. "
                "The worker verifies its manifest before sending LLM credentials.",
            )

    def preflight(self, provider: str) -> str | None:
        # Every new scan resolves stable afresh; existing jobs keep their pinned release.
        result = self.check(provider)
        if result["availability"] != "available" or result["compatibility"] == "incompatible":
            raise JobError(
                f"{result['message']} Open E2B settings to check the selected template.",
                409,
            )
        return str(result["resolved_template"] or result["template"])
