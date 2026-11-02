from __future__ import annotations

import io
import logging
import sys
from typing import TYPE_CHECKING

import pytest

from natsuhat.config import loader
from natsuhat.telemetry import logging as natsuhat_logging


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def clean_loggers() -> Iterator[None]:
    tracked = [logging.getLogger(name) for name in natsuhat_logging._TRACKED_ROOTS]
    saved = [(logger.handlers[:], logger.level, logger.propagate) for logger in tracked]
    for logger in tracked:
        logger.handlers = []
    try:
        yield
    finally:
        for logger, (handlers, level, propagate) in zip(tracked, saved, strict=True):
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def _stderr_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, natsuhat_logging._STREAM_TAG, False)]


@pytest.mark.usefixtures("clean_loggers")
def test_console_logging_hides_warnings_and_shows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NATSUHAT_DEBUG", raising=False)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    natsuhat_logging.setup_console_logging()

    logger = logging.getLogger("natsuhat.llm.request_log")
    logger.warning("llm_request outcome=error")
    logger.error("boom")

    out = err.getvalue()
    assert "llm_request" not in out
    assert "ERROR" in out
    assert "natsuhat.llm.request_log: boom" in out


@pytest.mark.usefixtures("clean_loggers")
def test_console_logging_follows_the_current_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NATSUHAT_DEBUG", raising=False)
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    natsuhat_logging.setup_console_logging()
    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)

    logging.getLogger("natsuhat.test").error("after swap")

    assert first.getvalue() == ""
    assert "after swap" in second.getvalue()


@pytest.mark.usefixtures("clean_loggers")
def test_scan_logs_redact_request_overrides_in_arguments_and_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = loader.load_settings().model_copy(deep=True)
    settings.llm.extra_headers = {"X-Private": "header-credential"}
    settings.llm.extra_body = {"nested": {"token": "body-credential"}}
    monkeypatch.setattr(loader, "_cached", settings)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    teardown = natsuhat_logging.setup_scan_logging(tmp_path)
    try:
        try:
            raise ValueError("Gateway echoed body-credential")  # noqa: TRY301 - exercise traceback redaction
        except ValueError:
            logging.getLogger("natsuhat.test").exception("Failed with %s", "header-credential")
    finally:
        teardown()
    for text in (err.getvalue(), (tmp_path / "natsuhat.log").read_text()):
        assert "header-credential" not in text and "body-credential" not in text
        assert "Failed with [redacted]" in text
        assert "ValueError: Gateway echoed [redacted]" in text


@pytest.mark.usefixtures("clean_loggers")
def test_console_logging_is_idempotent() -> None:
    natsuhat_logging.setup_console_logging()
    natsuhat_logging.setup_console_logging()

    for name in natsuhat_logging._TRACKED_ROOTS:
        assert len(_stderr_handlers(logging.getLogger(name))) == 1


@pytest.mark.usefixtures("clean_loggers")
def test_scan_logging_reuses_the_console_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NATSUHAT_DEBUG", raising=False)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    natsuhat_logging.setup_console_logging()
    teardown = natsuhat_logging.setup_scan_logging(tmp_path)

    natsuhat_logger = logging.getLogger("natsuhat")
    assert len(_stderr_handlers(natsuhat_logger)) == 1
    logging.getLogger("natsuhat.test").error("once")
    assert err.getvalue().count("once") == 1
    assert "once" in (tmp_path / "natsuhat.log").read_text(encoding="utf-8")

    teardown()
    assert len(_stderr_handlers(natsuhat_logger)) == 1
    assert not any(isinstance(h, logging.FileHandler) for h in natsuhat_logger.handlers)


@pytest.mark.usefixtures("clean_loggers")
def test_scan_logging_alone_still_attaches_a_stderr_handler(tmp_path: Path) -> None:
    teardown = natsuhat_logging.setup_scan_logging(tmp_path)
    natsuhat_logger = logging.getLogger("natsuhat")
    assert len(_stderr_handlers(natsuhat_logger)) == 1
    teardown()
    assert _stderr_handlers(natsuhat_logger) == []
