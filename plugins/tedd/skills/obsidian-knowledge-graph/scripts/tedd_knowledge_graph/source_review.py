"""Finite, turn-owned image review receipts and content-free capture progress.

Values describe verified host actions; constructing one supplies no authority.
The host must record delivery only after the actual transport/viewer succeeds,
and owner acknowledgment only after its private confirmation UI accepts.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import asdict, dataclass

from .source_contracts import SourceArchiveError, SourceRef, identifier, positive, sha256


def overview_size(size: tuple[int, int], maximum: int = 2048) -> tuple[int, int]:
    """One disclosed aspect-preserving geometry for the worker and review receipt."""
    if type(size) is not tuple or len(size) != 2:
        raise SourceArchiveError()
    width, height = (positive(value, maximum=131072) for value in size)
    if width * height > 128_000_000:
        raise SourceArchiveError("limit_exceeded")
    edge = min(positive(maximum, maximum=2048), max(width, height))
    return (
        (edge, max(1, (height * edge + width // 2) // width))
        if width >= height
        else (max(1, (width * edge + height // 2) // height), edge)
    )


@dataclass(frozen=True, repr=False)
class ReviewRendering:
    """Exact source/unit and orientation-corrected coverage of one delivered PNG."""

    scope_key: str
    source_key: str
    original_sha256: str
    unit_id: str
    audience: str
    png_sha256: str
    original_size: tuple[int, int]
    output_size: tuple[int, int]
    orientation: int = 1
    rectangle: tuple[int, int, int, int] | None = None
    view: str = "presentation"

    def __post_init__(self) -> None:
        for checksum in (self.scope_key, self.source_key, self.original_sha256, self.png_sha256):
            sha256(checksum)
        if (
            not isinstance(self.unit_id, str)
            or re.fullmatch(r"u[1-9][0-9]{0,8}", self.unit_id) is None
            or self.audience not in ("model", "owner")
            or self.view not in ("presentation", "reconstruction")
        ):
            raise SourceArchiveError("scope_denied")
        positive(self.orientation, maximum=8)
        for size, maximum in ((self.original_size, 131072), (self.output_size, 2048)):
            if type(size) is not tuple or len(size) != 2:
                raise SourceArchiveError()
            for dimension in size:
                positive(dimension, maximum=maximum)
        if self.original_size[0] * self.original_size[1] > 128_000_000:
            raise SourceArchiveError("limit_exceeded")
        width, height = self.oriented_size
        out_width, out_height = self.output_size
        if self.rectangle is None:
            # Fix the long axis, round the short axis to the nearest pixel, and
            # retain a one-pixel floor for extremely thin originals. A generic
            # cross-product tolerance can otherwise admit a halved short axis.
            expected = overview_size(self.oriented_size, max(self.output_size))
            if out_width > width or out_height > height or self.output_size != expected:
                raise SourceArchiveError()
        else:
            if (
                type(self.rectangle) is not tuple
                or len(self.rectangle) != 4
                or any(type(value) is not int for value in self.rectangle)
            ):
                raise SourceArchiveError()
            left, top, right, bottom = self.rectangle
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise SourceArchiveError()
            if self.output_size != (right - left, bottom - top):
                raise SourceArchiveError("limit_exceeded")

    @property
    def oriented_size(self) -> tuple[int, int]:
        if self.orientation >= 5:
            return self.original_size[1], self.original_size[0]
        return self.original_size

    @property
    def binding(self) -> tuple[object, ...]:
        return (
            self.scope_key,
            self.source_key,
            self.original_sha256,
            self.unit_id,
            self.audience,
            self.original_size,
            self.orientation,
            self.view,
        )


@dataclass
class _ReviewEntry:
    rendering: ReviewRendering
    delivered: bool = False
    acknowledged: bool = False


class ReviewBook:
    """Bounded in-memory state owned and retired by the existing source turn.

    No source bytes, filenames, durable ledger, independent timers, or policy
    grants live here. Model and owner acknowledgment use distinct host routes.
    """

    def __init__(self, *, maximum: int = 512) -> None:
        self._maximum = positive(maximum, maximum=4096)
        self._entries: dict[str, _ReviewEntry] = {}
        self._closed = False
        self._lock = threading.RLock()

    def _active(self) -> None:
        if self._closed:
            raise SourceArchiveError("scope_denied")

    def _entry(self, token: str) -> _ReviewEntry:
        self._active()
        if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{32}", token) is None:
            raise SourceArchiveError("scope_denied")
        entry = self._entries.get(token)
        if entry is None:
            raise SourceArchiveError("scope_denied")
        return entry

    def offer(self, rendering: ReviewRendering) -> str:
        with self._lock:
            self._active()
            if not isinstance(rendering, ReviewRendering):
                raise SourceArchiveError()
            if len(self._entries) >= self._maximum:
                raise SourceArchiveError("limit_exceeded")
            token = secrets.token_hex(16)
            if token in self._entries:
                raise SourceArchiveError("source_unavailable")
            self._entries[token] = _ReviewEntry(rendering)
            return token

    def delivered(self, token: str) -> None:
        with self._lock:
            self._entry(token).delivered = True

    def for_delivery(self, token: str, *, audience: str, png_sha256: str) -> ReviewRendering:
        """Validate audience and exact derivative before exposing any image bytes."""
        with self._lock:
            rendering = self._entry(token).rendering
            if rendering.audience != audience or rendering.png_sha256 != png_sha256:
                raise SourceArchiveError("scope_denied")
            return rendering

    def acknowledge(self, token: str, *, audience: str) -> ReviewRendering:
        with self._lock:
            entry = self._entry(token)
            if not entry.delivered or entry.rendering.audience != audience:
                raise SourceArchiveError("scope_denied")
            entry.acknowledged = True
            return entry.rendering

    def acknowledged(self, rendering: ReviewRendering) -> tuple[ReviewRendering, ...]:
        with self._lock:
            self._active()
            return tuple(
                dict.fromkeys(
                    entry.rendering
                    for entry in self._entries.values()
                    if entry.acknowledged and entry.rendering.binding == rendering.binding
                )
            )

    def covers(self, rendering: ReviewRendering) -> bool:
        for reviewed in self.acknowledged(rendering):
            if rendering.rectangle is None:
                if rendering == reviewed:
                    return True
            elif reviewed.rectangle is not None:
                left, top, right, bottom = rendering.rectangle
                old_left, old_top, old_right, old_bottom = reviewed.rectangle
                if old_left <= left < right <= old_right and old_top <= top < bottom <= old_bottom:
                    return True
        return False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()


@dataclass(frozen=True, repr=False)
class SourceProgress:
    """Additive progress fields; never replaces an operation's public status.

    A publication reference is supplied only from a verified archive receipt;
    a memory ID only from a successful graph application. This value performs
    neither operation and is not an authority token.
    """

    stage: str
    reason: str | None = None
    ref: SourceRef | None = None
    memory_id: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in ("acquired", "decoded", "rendered", "reviewed", "preserved", "bound"):
            raise SourceArchiveError()
        if self.reason not in (
            None,
            "review_required",
            "metadata_denied",
            "cancelled",
            "binding_failed",
            "access_denied",
            "limit_exceeded",
            "delivery_failed",
            "reconciliation_required",
        ):
            raise SourceArchiveError()
        if (self.stage in ("preserved", "bound")) != (self.ref is not None):
            raise SourceArchiveError()
        if self.ref is not None and not isinstance(self.ref, SourceRef):
            raise SourceArchiveError()
        if (self.stage == "bound") != (self.memory_id is not None):
            raise SourceArchiveError()
        if self.memory_id is not None:
            identifier(self.memory_id)

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}
