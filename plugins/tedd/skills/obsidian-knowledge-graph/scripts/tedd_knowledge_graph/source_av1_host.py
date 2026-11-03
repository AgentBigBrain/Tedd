"""Optional strict AV1 validation before admission to the shared image path.

One native child validates every coded item, including grid tiles and alpha.
Container/metadata admission stays pure; no unqualified decoder is a fallback.
The enclosing disposable worker owns the whole process-tree budget.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .source_contracts import SourceArchiveError
from .source_image_bmff import BmffInventory, inspect_bmff
from .source_native_host import run_native_helper

_VALIDATOR_SHA256 = {
    "win32": "91a5fbf0056d2da6face1f40415c2c015d9b77e7770413d9069915d55fa7cecd",
    "linux": "60c95ed97bdae7d9acd7db576951d66c483b48732ea8e1ce19e8c1d19f44c2bb",
}.get(sys.platform, "")
_MAGIC = b"TEDDAV1\x02"


@dataclass(frozen=True, repr=False)
class Av1Validator:
    """Host-bound helper, never an executable selected by a source or model."""

    executable: Path
    deadline: float | None = None

    def inspect(self, payload: bytes) -> BmffInventory:
        inventory = inspect_bmff(payload)
        if inventory.format != "avif" or not _VALIDATOR_SHA256:
            raise SourceArchiveError("capability_unavailable")
        if any(item.kind == b"hvc1" for item in inventory.items):
            raise SourceArchiveError("capability_unavailable")
        coded = tuple(item for item in inventory.items if item.kind == b"av01")
        if not coded and not inventory.tracks:
            raise SourceArchiveError("capture_incomplete")
        # Independent items are one-sample sync streams. Carry their av1C just
        # like tracks so still images, tiles and auxiliary planes cannot bypass
        # the native public-header comparison.
        streams = tuple(((item, True),) for item in coded) + tuple(
            tuple(zip(track.samples, track.sync, strict=True)) for track in inventory.tracks
        )
        packet = (
            _MAGIC
            + struct.pack("!I", len(streams))
            + b"".join(
                struct.pack("!I", len(stream))
                + dict(stream[0][0].properties)[b"av1C"]
                + b"".join(
                    struct.pack("!4I", len(item.data), *item.coded_size, int(sync)) + item.data
                    for item, sync in stream
                )
                for stream in streams
            )
        )
        coded += tuple(sample for track in inventory.tracks for sample in track.samples)
        expected = _MAGIC + struct.pack(
            "!2I", len(coded), sum(item.coded_size[0] * item.coded_size[1] for item in coded)
        )
        try:
            completed = run_native_helper(
                [str(self.executable)],
                packet,
                ((self.executable, _VALIDATOR_SHA256),),
                self.deadline,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceArchiveError("capability_unavailable") from error
        if completed.returncode or completed.stderr or completed.stdout != expected:
            raise SourceArchiveError()
        return inventory
