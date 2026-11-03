"""Capture direct, already-authorized inputs; no fetching, watchers or model calls.

The host supplies acquisition bytes and metadata separately from source content.
Its trusted bridge must establish acquisition and PDF screening. A JSON assertion
or a model-generated reconstruction cannot establish those facts.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import TypeVar

from .source_archive import Archive
from .source_chat import decode_chat
from .source_contracts import (
    CaptureReceipt,
    SourceArchiveError,
    SourceMetadata,
    SourceUnit,
    digest,
    positive,
    sha256,
    text,
)
from .source_documents import PLAIN_KINDS, DocumentReading, read_document
from .source_images import ImageReading
from .source_review import ReviewBook

Result = TypeVar("Result")
MAX_PDF_PAGES = 256
OFFICE_KINDS = frozenset({"docx", "xlsx", "pptx"})


def run_if_collecting(
    operation: Callable[[], Result], *, state: str, enabled: bool
) -> Result | None:
    """Consume the host workflow's current state, without maintaining another one."""
    if type(enabled) is not bool or state not in (
        "active",
        "waiting",
        "paused",
        "closed",
        "excluded",
    ):
        raise SourceArchiveError("scope_denied")
    if not enabled or state in ("paused", "closed", "excluded"):
        return None
    return operation()


def capture_chat(
    archive: Archive,
    *,
    metadata: SourceMetadata,
    payload: bytes,
    acquired_sha256: str,
    publication: AbstractContextManager[None] | None = None,
) -> CaptureReceipt:
    """Accept the neutral direct-export/selected-DOM contract, preserving raw bytes."""
    if metadata.kind != "chat" or digest(payload) != sha256(acquired_sha256):
        raise SourceArchiveError()
    units = decode_chat(payload, metadata, maximum_bytes=archive.limits.chat_bytes)
    return archive.publish(
        payload=payload,
        metadata=metadata,
        units=units,
        reuse_messages=True,
        publication=publication,
    )


def validate_document_capture(
    archive: Archive, metadata: SourceMetadata, acquired_sha256: str
) -> None:
    """Reject unqualified acquisition before reading an input or running a parser."""
    details = metadata.details
    if (
        details is None
        or details.provider != "local"
        or details.retention != "retained_copy"
        or metadata.kind not in PLAIN_KINDS | OFFICE_KINDS | {"email", "image"}
        or archive.access is None
        or not archive.access.local_retention
    ):
        raise SourceArchiveError("capability_unavailable")
    if metadata.kind == "tool_result" and metadata.capture_method != "delivered_output":
        raise SourceArchiveError("capture_incomplete")
    if metadata.kind in OFFICE_KINDS | {"image"} and (
        metadata.capture_method != "local_file" or details.representation != "original"
    ):
        raise SourceArchiveError("capability_unavailable")
    archive.access.authorize(metadata, sha256(acquired_sha256), "capture")


def capture_document(
    archive: Archive,
    *,
    metadata: SourceMetadata,
    payload: bytes,
    acquired_sha256: str,
    reading: DocumentReading | None = None,
    visual_review_sha256: str | None = None,
    image: ImageReading | None = None,
    reviews: ReviewBook | None = None,
    publication: AbstractContextManager[None] | None = None,
) -> CaptureReceipt:
    """Publish a direct local reading through the existing archive transaction.

    The CLI prepares its reading before the final config/graph reservation, just
    like PDF extraction. No JSON-supplied reading or cloud access assertion enters
    this path. A real provider-operation boundary must qualify cloud capture first.
    """
    validate_document_capture(archive, metadata, acquired_sha256)
    if metadata.kind in OFFICE_KINDS | {"image"} and reading is None:
        # Untrusted OOXML is parsed by the existing bounded host worker, not here.
        raise SourceArchiveError("capability_unavailable")
    if digest(payload) != sha256(acquired_sha256):
        raise SourceArchiveError()
    result = reading or read_document(
        payload, metadata.kind, maximum_bytes=archive.limits.for_kind(metadata.kind)
    )
    if metadata.kind == "image":
        if image is None or reviews is None or image.document != result or archive.access is None:
            raise SourceArchiveError("capability_unavailable")
        if metadata.details is None:
            raise SourceArchiveError()
        image.inventory.require_metadata_permission(metadata.details.sensitivity)
        image.require_reviews(
            reviews, metadata.scope.key, metadata.logical_key, archive.access.audience
        )
    if "embedded_images_require_visual_review" in result.gaps and (
        visual_review_sha256 is None or sha256(visual_review_sha256) != digest(payload)
    ):
        raise SourceArchiveError("privacy_rejected")
    bound = bind_document_reading(metadata, payload, result)
    return archive.publish(
        payload=payload,
        metadata=bound,
        units=result.units,
        reuse_context=True,
        publication=publication,
    )


def bind_document_reading(
    metadata: SourceMetadata, payload: bytes, reading: DocumentReading
) -> SourceMetadata:
    """Bind a finite reader's exact input and gaps without acquisition or I/O."""
    details = metadata.details
    if (
        details is None
        or reading.original_sha256 != digest(payload)
        or reading.kind != metadata.kind
    ):
        raise SourceArchiveError()
    gaps = tuple(dict.fromkeys((*metadata.gaps, *reading.gaps)))
    return replace(
        metadata,
        details=replace(details, reader_id=reading.reader_id),
        gaps=gaps,
        coverage="partial" if gaps else metadata.coverage,
    )


@dataclass(frozen=True, repr=False)
class PdfReading:
    """Host result, not a self-authenticating credential or source truth verdict."""

    original_sha256: str
    reader_id: str
    page_count: int
    units: tuple[SourceUnit, ...]
    screening: str
    resources: str


def capture_pdf(
    archive: Archive,
    *,
    metadata: SourceMetadata,
    payload: bytes,
    reading: PdfReading | None,
    publication: AbstractContextManager[None] | None = None,
) -> CaptureReceipt:
    if reading is None or reading.resources != "bounded":
        raise SourceArchiveError("capability_unavailable")
    if reading.screening != "screened":
        raise SourceArchiveError("privacy_rejected")
    if (
        metadata.kind != "pdf"
        or not payload.startswith(b"%PDF-")
        or digest(payload) != sha256(reading.original_sha256)
    ):
        raise SourceArchiveError()
    text(reading.reader_id)
    positive(reading.page_count, maximum=MAX_PDF_PAGES)
    if (
        type(reading.units) is not tuple
        or len(reading.units) != reading.page_count
        or any(unit.position != number for number, unit in enumerate(reading.units, start=1))
        or any(unit.kind not in ("page", "ocr_page", "image_page") for unit in reading.units)
    ):
        raise SourceArchiveError("capture_incomplete")
    receipt = archive.publish(
        payload=payload,
        metadata=replace(
            metadata,
            tool_id=reading.reader_id,
            details=replace(metadata.details, reader_id=reading.reader_id)
            if metadata.details is not None
            else None,
        ),
        units=reading.units,
        publication=publication,
    )
    if any(unit.kind in ("ocr_page", "image_page") for unit in reading.units):
        receipt = replace(receipt, warnings=("visual_check_required",))
    return receipt
