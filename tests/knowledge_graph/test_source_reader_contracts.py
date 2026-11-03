"""Closed contracts for the optional isolated source reader."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import traceback
import unittest
from collections.abc import Callable

from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_contracts import (
    ReaderRequest,
    validate_reader_input,
    validate_reader_response,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _wire(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR" + struct.pack("!II", 1, 1) + b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
)
PDF = b"%PDF-neutral-reader-contract"
DOCX = b"PK\x03\x04neutral-reader-contract"


def _plain_unit(position: int, kind: str, text: str = "") -> dict[str, object]:
    return {
        "anchor_id": f"u{position}",
        "text": text,
        "position": position,
        "kind": kind,
        "source_id": None,
        "speaker": None,
        "source_time": None,
        "reply_to": None,
        "links": [],
    }


def _image_unit() -> dict[str, object]:
    return {
        **_plain_unit(1, "image_page"),
        "location": {
            "kind": "page",
            "container": "image:1:png:still:root:0",
            "start": 1,
            "end": 1,
            "column_start": None,
            "column_end": None,
        },
    }


def _office_unit() -> dict[str, object]:
    return {
        **_plain_unit(1, "section", "Neutral paragraph."),
        "location": {
            "kind": "paragraph",
            "container": "word/document.xml",
            "start": 1,
            "end": 1,
            "column_start": None,
            "column_end": None,
        },
    }


def _image_response(*, rendered: bool) -> dict[str, object]:
    checksum = _sha256(PNG)
    result: dict[str, object] = {
        "status": "text_screened",
        "resources": "bounded",
        "kind": "image",
        "original_sha256": checksum,
        "reader_id": "tedd-image:1",
        "units": [_image_unit()],
        "gaps": [
            "image_pixels_require_viewing",
            "image_preview_8bit_color_not_calibrated",
            "image_preview_hdr_not_reproduced",
        ],
        "inventory": {
            "original_sha256": checksum,
            "format": "png",
            "units": [
                {
                    "role": "still",
                    "native_index": 0,
                    "size": [1, 1],
                    "orientation": 1,
                    "parent": None,
                }
            ],
            "metadata_profile": 1,
            "metadata_categories": [],
        },
    }
    if rendered:
        result.update(
            position=1,
            original_size=[1, 1],
            output_size=[1, 1],
            orientation=1,
            rectangle=None,
            png_sha256=checksum,
            png_base64=base64.b64encode(PNG).decode("ascii"),
        )
    return result


def _pdf_text_response() -> dict[str, object]:
    return {
        "status": "text_screened",
        "original_sha256": _sha256(PDF),
        "reader_id": "neutral-pdf-reader:1",
        "page_count": 2,
        "units": [
            _plain_unit(1, "page", "Neutral page."),
            _plain_unit(2, "image_page"),
        ],
        "resources": "bounded",
        "visual_review_required": True,
    }


def _pdf_page_response() -> dict[str, object]:
    return {
        "status": "rendered",
        "page": 2,
        "original_sha256": _sha256(PDF),
        "png_sha256": _sha256(PNG),
        "png_base64": base64.b64encode(PNG).decode("ascii"),
        "resources": "bounded",
    }


def _docx_text_response() -> dict[str, object]:
    return {
        "status": "text_screened",
        "resources": "bounded",
        "kind": "docx",
        "original_sha256": _sha256(DOCX),
        "reader_id": "tedd-ooxml:8",
        "units": {"format": 2, "units": [_office_unit()]},
        "gaps": ["office_layout_not_rendered"],
    }


def _docx_page_response() -> dict[str, object]:
    return {
        "status": "text_screened",
        "page": 2,
        "original_sha256": _sha256(DOCX),
        "derived_pdf_sha256": "d" * 64,
        "png_sha256": _sha256(PNG),
        "png_base64": base64.b64encode(PNG).decode("ascii"),
        "resources": "bounded",
        "page_count": 2,
        "kind": "docx",
        "reader_id": "tedd-ooxml:8",
        "representation": "libreoffice_rendering",
        "coverage": "partial",
        "gaps": ["office_layout_not_rendered"],
    }


class ReaderRequestTests(unittest.TestCase):
    def test_page_operations_require_an_exact_bounded_integer_position(self) -> None:
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("pdf-page", True)
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("pdf-page")
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("pdf-page", 0)
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("pdf-page", 257)
        self.assertEqual(
            ReaderRequest("pdf-page", 2).worker_arguments(),
            ("pdf-page", "2"),
        )

    def test_non_page_operations_reject_positions_and_unknown_names(self) -> None:
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("pdf-text", 1)
        with self.assertRaises(SourceArchiveError):
            ReaderRequest("unknown")
        with self.assertRaises(SourceArchiveError):
            ReaderRequest(1)  # type: ignore[arg-type]
        self.assertEqual(ReaderRequest("docx-text").worker_arguments(), ("docx-text",))


class ReaderInputTests(unittest.TestCase):
    def test_supported_routes_accept_only_their_source_signature(self) -> None:
        for operation, payload in (
            ("image-inspect", b"\x89PNG\r\n\x1a\nfixture"),
            ("image-page", b"\x89PNG\r\n\x1a\nfixture"),
            ("pdf-text", b"%PDF-fixture"),
            ("pdf-page", b"%PDF-fixture"),
            ("docx-text", b"PK\x03\x04fixture"),
            ("docx-page", b"PK\x03\x04fixture"),
        ):
            with self.subTest(operation=operation):
                position = 1 if operation.endswith("-page") else None
                validate_reader_input(ReaderRequest(operation, position), payload)

        for operation, payload in (
            ("image-inspect", b"GIF89a"),
            ("pdf-text", b"not-pdf"),
            ("docx-text", b"not-zip"),
        ):
            with self.subTest(operation=operation), self.assertRaises(SourceArchiveError):
                validate_reader_input(ReaderRequest(operation), payload)

    def test_input_is_nonempty_exact_bytes_with_a_sixty_four_mib_ceiling(self) -> None:
        request = ReaderRequest("pdf-text")
        for payload in (b"", bytearray(b"%PDF-fixture")):
            with self.subTest(payload_type=type(payload)), self.assertRaises(SourceArchiveError):
                validate_reader_input(request, payload)
        validate_reader_input(request, b"%PDF-" + bytes(64 * 1024**2 - 5))
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            validate_reader_input(request, b"%PDF-" + bytes(64 * 1024**2 - 4))


class ReaderResponseTests(unittest.TestCase):
    def cases(
        self,
    ) -> tuple[tuple[ReaderRequest, bytes, Callable[[], dict[str, object]]], ...]:
        return (
            (ReaderRequest("image-inspect"), PNG, lambda: _image_response(rendered=False)),
            (ReaderRequest("image-page", 1), PNG, lambda: _image_response(rendered=True)),
            (ReaderRequest("pdf-text"), PDF, _pdf_text_response),
            (ReaderRequest("pdf-page", 2), PDF, _pdf_page_response),
            (ReaderRequest("docx-text"), DOCX, _docx_text_response),
            (ReaderRequest("docx-page", 2), DOCX, _docx_page_response),
        )

    def test_all_six_operations_accept_closed_source_bound_successes(self) -> None:
        for request, payload, response in self.cases():
            expected = response()
            with self.subTest(operation=request.operation):
                self.assertEqual(
                    validate_reader_response(request, payload, _wire(expected)), expected
                )

    def test_all_successes_reject_unknown_status_digest_and_resource_mutations(self) -> None:
        for request, payload, response in self.cases():
            for field, value in (
                ("status", "unknown"),
                ("original_sha256", "0" * 64),
                ("resources", "unbounded"),
            ):
                changed = response()
                changed[field] = value
                with (
                    self.subTest(operation=request.operation, field=field),
                    self.assertRaises(SourceArchiveError),
                ):
                    validate_reader_response(request, payload, _wire(changed))

    def test_selected_page_successes_bind_the_exact_requested_position(self) -> None:
        for request, payload, response in self.cases():
            if request.position is None:
                continue
            changed = response()
            changed["page" if request.operation != "image-page" else "position"] = 1 + (
                request.position or 0
            )
            with self.subTest(operation=request.operation), self.assertRaises(SourceArchiveError):
                validate_reader_response(request, payload, _wire(changed))

    def test_image_contract_rejects_inspection_rendering_and_non_presentation_details(self) -> None:
        inspected = _image_response(rendered=False)
        inspected["position"] = 1
        rendered = _image_response(rendered=True)
        rendered["rectangle"] = [0, 0, 1, 1]
        reconstructed = _image_response(rendered=True)
        reconstructed["view"] = "reconstruction"
        wrong_kind = _image_response(rendered=False)
        wrong_kind["kind"] = "docx"
        for request, value in (
            (ReaderRequest("image-inspect"), inspected),
            (ReaderRequest("image-page", 1), rendered),
            (ReaderRequest("image-page", 1), reconstructed),
            (ReaderRequest("image-inspect"), wrong_kind),
        ):
            with self.subTest(value=value), self.assertRaises(SourceArchiveError):
                validate_reader_response(request, PNG, _wire(value))

    def test_pdf_text_requires_visual_review_and_one_contiguous_unit_per_page(self) -> None:
        cases: list[dict[str, object]] = []
        for field, value in (
            ("visual_review_required", False),
            ("page_count", 1),
            ("page_count", 257),
        ):
            changed = _pdf_text_response()
            changed[field] = value
            cases.append(changed)
        duplicate_position = _pdf_text_response()
        units = copy.deepcopy(duplicate_position["units"])
        assert isinstance(units, list) and isinstance(units[1], dict)
        units[1]["position"] = 1
        duplicate_position["units"] = units
        cases.append(duplicate_position)
        wrong_kind = _pdf_text_response()
        units = copy.deepcopy(wrong_kind["units"])
        assert isinstance(units, list) and isinstance(units[0], dict)
        units[0]["kind"] = "section"
        wrong_kind["units"] = units
        cases.append(wrong_kind)
        for invalid_response in cases:
            with self.subTest(value=invalid_response), self.assertRaises(SourceArchiveError):
                validate_reader_response(ReaderRequest("pdf-text"), PDF, _wire(invalid_response))

    def test_rendered_pages_require_bound_png_bytes_and_operation_specific_fields(self) -> None:
        pdf = _pdf_page_response()
        pdf["png_sha256"] = "0" * 64
        docx_mutations: list[dict[str, object]] = []
        for field, value in (
            ("kind", "xlsx"),
            ("derived_pdf_sha256", "not-a-digest"),
            ("page_count", 1),
            ("representation", "native"),
            ("coverage", "complete"),
            ("gaps", []),
        ):
            changed = _docx_page_response()
            changed[field] = value
            docx_mutations.append(changed)
        for request, payload, invalid_response in (
            (ReaderRequest("pdf-page", 2), PDF, pdf),
            *((ReaderRequest("docx-page", 2), DOCX, changed) for changed in docx_mutations),
        ):
            with (
                self.subTest(operation=request.operation, value=invalid_response),
                self.assertRaises(SourceArchiveError),
            ):
                validate_reader_response(request, payload, _wire(invalid_response))

    def test_docx_text_uses_the_existing_document_reading_schema(self) -> None:
        mutations: list[dict[str, object]] = []
        for field, value in (
            ("kind", "xlsx"),
            ("reader_id", ""),
            ("gaps", ["/private/source.docx"]),
        ):
            changed = _docx_text_response()
            changed[field] = value
            mutations.append(changed)
        unknown_field = _docx_text_response()
        unknown_field["path"] = "/private/source.docx"
        mutations.append(unknown_field)
        for invalid_response in mutations:
            with self.subTest(value=invalid_response), self.assertRaises(SourceArchiveError):
                validate_reader_response(ReaderRequest("docx-text"), DOCX, _wire(invalid_response))

    def test_output_json_is_strict_bounded_and_closed(self) -> None:
        request = ReaderRequest("pdf-text")
        for output in (
            b"not-json",
            b'{"status":"source_corrupt"} trailing',
            b'{"status":"source_corrupt","status":"capability_unavailable"}',
            b'{"status":"source_corrupt","message":"/private/source.pdf"}',
        ):
            with self.subTest(output=output), self.assertRaises(SourceArchiveError) as caught:
                validate_reader_response(request, PDF, output)
            self.assertNotIn("/private", str(caught.exception))
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            validate_reader_response(request, PDF, b"{" + bytes(32 * 1024**2))

    def test_malformed_output_suppresses_the_decoder_exception_chain(self) -> None:
        with self.assertRaises(SourceArchiveError) as caught:
            validate_reader_response(
                ReaderRequest("pdf-text"),
                PDF,
                b'{"text":"PRIVATE_SENTINEL",',
            )
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("PRIVATE_SENTINEL", rendered)
        self.assertNotIn("JSONDecodeError", rendered)

    def test_only_closed_one_field_source_errors_are_propagated(self) -> None:
        for status in (
            "source_corrupt",
            "privacy_rejected",
            "limit_exceeded",
            "source_unavailable",
            "capture_incomplete",
            "capability_unavailable",
        ):
            with self.subTest(status=status), self.assertRaises(SourceArchiveError) as caught:
                validate_reader_response(
                    ReaderRequest("docx-text"), DOCX, _wire({"status": status})
                )
            self.assertEqual(caught.exception.code, status)
        with self.assertRaises(SourceArchiveError) as caught:
            validate_reader_response(
                ReaderRequest("docx-text"),
                DOCX,
                _wire({"status": "scope_denied", "traceback": "/private/source.docx"}),
            )
        self.assertEqual(caught.exception.code, "source_corrupt")
        self.assertNotIn("/private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
