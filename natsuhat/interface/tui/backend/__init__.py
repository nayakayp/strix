"""Backend bridge for external TUI clients."""

from natsuhat.interface.tui.backend.controller import TuiController
from natsuhat.interface.tui.backend.server import TuiBackendServer


__all__ = ["TuiBackendServer", "TuiController"]
