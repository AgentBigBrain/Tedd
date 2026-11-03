"""Pinned optional SVG rendering inside the existing disposable image worker.

No source bytes are written to disk. Positive XML admission runs before helper
access or execution. Process limits belong to the caller's whole worker/job.
Development artifacts are pinned separately; unknown platform builds refuse.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .source_contracts import SourceArchiveError
from .source_image_svg import SvgInventory, inspect_svg
from .source_native_host import run_native_helper

_RENDERER_SHA256 = {
    "win32": "29278eeac1217ed69c10ff0a978d92efecd19e227074fe71dafb5f6631b73eca",
    "linux": "8d1dbe4d8e56d3d052668afc69d9d93fba7b723b06f1d3425f29418da9a816af",
}.get(sys.platform, "")
_FONT_SHA256 = "30e35dda7a179dadec11c58e89835b5006ee69d3bd43b55a91dd52f1f02a8cac"
_FONT_FAMILY = "Source Sans Pro"


@dataclass(frozen=True, repr=False)
class SvgRenderer:
    """Host-supplied locations, never source/tool-supplied executable choices."""

    executable: Path
    font: Path
    deadline: float | None = None

    def render(self, payload: bytes) -> tuple[SvgInventory, bytes]:
        inventory = inspect_svg(payload)
        if any(family != _FONT_FAMILY for family in inventory.font_families):
            # Never silently substitute an unavailable named font.
            raise SourceArchiveError("capability_unavailable")
        if not _RENDERER_SHA256:
            raise SourceArchiveError("capability_unavailable")
        arguments = [
            str(self.executable),
            "--skip-system-fonts",
            "--use-font-file",
            str(self.font),
            "--width",
            str(inventory.size[0]),
            "--height",
            str(inventory.size[1]),
        ]
        for family in ("font", "serif", "sans-serif", "cursive", "fantasy", "monospace"):
            arguments.extend((f"--{family}-family", _FONT_FAMILY))
        # No environment-based resource discovery. stdin is the admitted original;
        # the allowlist excludes every external-resource construct.
        try:
            completed = run_native_helper(
                [*arguments, "--resources-dir", str(self.executable.parent), "-", "-c"],
                payload,
                (
                    (self.executable, _RENDERER_SHA256),
                    (self.font, _FONT_SHA256),
                ),
                self.deadline,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceArchiveError("capability_unavailable") from error
        # resvg can succeed while dropping text. No warning content leaves this
        # process; any warning is a refusal, never a successful partial preview.
        if completed.returncode or completed.stderr:
            raise SourceArchiveError("capability_unavailable")
        png = completed.stdout
        if (
            not 33 <= len(png) <= inventory.size[0] * inventory.size[1] * 5 + 65536
            or not png.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            or struct.unpack("!2I", png[16:24]) != inventory.size
        ):
            raise SourceArchiveError()
        return inventory, png
