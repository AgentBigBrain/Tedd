"""Typed, source-bound contracts for an optional isolated reader."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from .source_contracts import (
    READING_BYTES,
    SourceArchiveError,
    decode_units,
    decoded_png,
    digest,
    encode,
    identifier,
    object_fields,
    parse_json,
    positive,
    sha256,
    text,
)
from .source_documents import DocumentReading
from .source_images import ImageReading

_OPERATIONS = frozenset(
    {
        "image-inspect",
        "image-page",
        "pdf-text",
        "pdf-page",
        "docx-text",
        "docx-page",
    }
)
_PAGE_OPERATIONS = frozenset({"image-page", "pdf-page", "docx-page"})
_MAX_INPUT_BYTES = 64 * 1024**2
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SOURCE_ERRORS = frozenset(
    {
        "source_corrupt",
        "privacy_rejected",
        "limit_exceeded",
        "source_unavailable",
        "capture_incomplete",
        "capability_unavailable",
    }
)


@dataclass(frozen=True, repr=False)
class ReaderRequest:
    operation: str
    position: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or self.operation not in _OPERATIONS:
            raise SourceArchiveError()
        if self.operation in _PAGE_OPERATIONS:
            if type(self.position) is not int or not 1 <= self.position <= 256:
                raise SourceArchiveError()
        elif self.position is not None:
            raise SourceArchiveError()

    def worker_arguments(self) -> tuple[str, ...]:
        if self.position is None:
            return (self.operation,)
        return (self.operation, str(self.position))


class SourceReader(Protocol):
    def read(
        self,
        request: ReaderRequest,
        payload: bytes,
        cancelled: threading.Event | None = None,
    ) -> dict[str, object]: ...


def validate_reader_input(request: ReaderRequest, payload: bytes) -> None:
    """Refuse unsupported source bytes before any isolated job is started."""
    if not isinstance(request, ReaderRequest) or not isinstance(payload, bytes) or not payload:
        raise SourceArchiveError()
    if len(payload) > _MAX_INPUT_BYTES:
        raise SourceArchiveError("limit_exceeded")
    if request.operation.startswith("image-"):
        signature = _PNG_SIGNATURE
    elif request.operation.startswith("pdf-"):
        signature = b"%PDF-"
    else:
        signature = b"PK\x03\x04"
    if not payload.startswith(signature):
        raise SourceArchiveError()


def _source_error(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"status"}:
        return
    status = value.get("status")
    if isinstance(status, str) and status in _SOURCE_ERRORS:
        raise SourceArchiveError(status)


def _image_response(request: ReaderRequest, payload: bytes, value: object) -> None:
    reading = ImageReading.decode(payload, value)
    if request.operation == "image-inspect":
        if reading.position is not None:
            raise SourceArchiveError()
    elif (
        reading.position != request.position
        or reading.view != "presentation"
        or reading.rectangle is not None
    ):
        raise SourceArchiveError()


def _pdf_text_response(payload: bytes, value: object) -> None:
    obj = object_fields(
        value,
        {
            "status",
            "original_sha256",
            "reader_id",
            "page_count",
            "units",
            "resources",
            "visual_review_required",
        },
    )
    units = decode_units(encode(obj["units"]))
    page_count = positive(obj["page_count"], maximum=256)
    if (
        obj["status"] != "text_screened"
        or obj["resources"] != "bounded"
        or obj["visual_review_required"] is not True
        or sha256(obj["original_sha256"]) != digest(payload)
        or not text(obj["reader_id"], maximum=256)
        or len(units) != page_count
        or any(
            unit.anchor_id != f"u{position}"
            or unit.position != position
            or unit.kind not in ("page", "image_page")
            for position, unit in enumerate(units, start=1)
        )
    ):
        raise SourceArchiveError()


def _pdf_page_response(request: ReaderRequest, payload: bytes, value: object) -> None:
    obj = object_fields(
        value,
        {
            "status",
            "page",
            "original_sha256",
            "png_sha256",
            "png_base64",
            "resources",
        },
    )
    if (
        obj["status"] != "rendered"
        or obj["resources"] != "bounded"
        or positive(obj["page"], maximum=256) != request.position
        or sha256(obj["original_sha256"]) != digest(payload)
    ):
        raise SourceArchiveError()
    decoded_png(obj)


def _gaps(value: object, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or required and not value:
        raise SourceArchiveError()
    return tuple(identifier(gap) for gap in value)


def _docx_text_response(payload: bytes, value: object) -> None:
    obj = object_fields(
        value,
        {
            "status",
            "resources",
            "kind",
            "original_sha256",
            "reader_id",
            "units",
            "gaps",
        },
    )
    reading = DocumentReading(
        sha256(obj["original_sha256"]),
        decode_units(encode(obj["units"])),
        text(obj["reader_id"], maximum=256),
        _gaps(obj["gaps"]),
        kind=text(obj["kind"]),
    )
    if (
        obj["status"] != "text_screened"
        or obj["resources"] != "bounded"
        or reading.kind != "docx"
        or reading.original_sha256 != digest(payload)
    ):
        raise SourceArchiveError()


def _docx_page_response(request: ReaderRequest, payload: bytes, value: object) -> None:
    obj = object_fields(
        value,
        {
            "status",
            "page",
            "original_sha256",
            "derived_pdf_sha256",
            "png_sha256",
            "png_base64",
            "resources",
            "page_count",
            "kind",
            "reader_id",
            "representation",
            "coverage",
            "gaps",
        },
    )
    page = positive(obj["page"], maximum=256)
    page_count = positive(obj["page_count"], maximum=256)
    gaps = _gaps(obj["gaps"], required=True)
    if (
        obj["status"] != "text_screened"
        or obj["resources"] != "bounded"
        or obj["kind"] != "docx"
        or page != request.position
        or page > page_count
        or sha256(obj["original_sha256"]) != digest(payload)
        or not sha256(obj["derived_pdf_sha256"])
        or not text(obj["reader_id"], maximum=256)
        or obj["representation"] != "libreoffice_rendering"
        or obj["coverage"] != "partial"
        or not gaps
    ):
        raise SourceArchiveError()
    decoded_png(obj)


def validate_reader_response(
    request: ReaderRequest, payload: bytes, output: bytes
) -> dict[str, object]:
    """Validate one bounded worker envelope against its exact input and selection."""
    validate_reader_input(request, payload)
    try:
        value = parse_json(output, maximum_bytes=READING_BYTES)
        _source_error(value)
        if request.operation.startswith("image-"):
            _image_response(request, payload, value)
        elif request.operation == "pdf-text":
            _pdf_text_response(payload, value)
        elif request.operation == "pdf-page":
            _pdf_page_response(request, payload, value)
        elif request.operation == "docx-text":
            _docx_text_response(payload, value)
        else:
            _docx_page_response(request, payload, value)
    except SourceArchiveError as error:
        code = error.code if error.code in _SOURCE_ERRORS else "source_corrupt"
        raise SourceArchiveError(code) from None
    if not isinstance(value, dict):
        raise SourceArchiveError()
    return value
