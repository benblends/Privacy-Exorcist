"""
Signal handler for graceful Ctrl+C shutdown.

SPEC-004 §3.4 + §5 Phase 3: Installs a SIGINT handler that sets
a shutdown flag on first press and force-exits on second press.
"""

from __future__ import annotations

import os
import signal
from typing import Optional


class SignalHandler:
    """Double-Ctrl+C detection with graceful shutdown.

    First Ctrl+C:  Sets shutdown flag, prints message.
    Second Ctrl+C: Force exits via os._exit(1).

    Usage:
        handler = SignalHandler()
        handler.install(orchestrator)
        try:
            ... main loop ...
        finally:
            handler.restore()
    """

    def __init__(self) -> None:
        self._shutdown_requested: bool = False
        self._original_sigint: Optional[signal.Handlers] = None
        self._orchestrator: Optional[object] = None

    def install(self, orchestrator: object) -> None:
        """Install the SIGINT handler.

        Args:
            orchestrator: An object with a shutdown() method.
        """
        self._orchestrator = orchestrator
        self._original_sigint = signal.getsignal(signal.SIGINT)

        def handler(signum: int, frame) -> None:
            if self._shutdown_requested:
                # Second Ctrl+C — force quit
                print("\n💀 Force quitting...")
                os._exit(1)

            self._shutdown_requested = True
            print("\n🛑 Shutting down gracefully...")
            print("   (Press Ctrl+C again to force quit)")

            if self._orchestrator and hasattr(self._orchestrator, "shutdown"):
                self._orchestrator.shutdown()

        signal.signal(signal.SIGINT, handler)

    def restore(self) -> None:
        """Restore the original SIGINT handler."""
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
            self._original_sigint = None

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested
