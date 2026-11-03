"""Explicit qualification gate for private source workers on macOS.

Darwin can reject RLIMIT_AS below the interpreter's existing Mach mappings.
Adding that startup size to the budget would bound additional mappings, not the
documented total worker/helper memory. RLIMIT_RSS aliases RLIMIT_AS, and a zero
RLIMIT_CORE does not establish suppression of macOS crash diagnostics.

Until an execution boundary enforces both contracts, refuse before reading input
or launching a native helper. Do not probe privileged APIs, alter system crash
policy, or silently replace this gate with a weaker memory/privacy contract.
Ordinary graph and non-worker source operations do not use this gate.
"""

from __future__ import annotations

import sys
from typing import Literal

from .source_contracts import SourceArchiveError


class MacOSWorkerUnavailable(SourceArchiveError):
    """A fixed, content-free capability diagnostic, never a parser rejection."""

    reason: Literal["macos_worker_boundary_unqualified"] = "macos_worker_boundary_unqualified"

    def __init__(self) -> None:
        super().__init__("capability_unavailable")

    def response(self) -> dict[str, object]:
        return {"status": self.code, "reason": self.reason}


def require_worker_boundary() -> None:
    """Preserve existing non-macOS paths; macOS private workers are unqualified."""
    if sys.platform == "darwin":
        raise MacOSWorkerUnavailable()
