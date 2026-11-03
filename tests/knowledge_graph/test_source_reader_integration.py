"""Trusted reader injection preserves source authority and publication gates."""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import cast
from unittest import mock

import tedd_sources
from tedd_knowledge_graph.source_archive import Archive
from tedd_knowledge_graph.source_av1_host import Av1Validator
from tedd_knowledge_graph.source_contracts import SourceArchiveError, digest, encode
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.source_mcp import SourceTurn
from tedd_knowledge_graph.source_reader_contracts import (
    ReaderRequest,
    SourceReader,
    validate_reader_response,
)
from tedd_knowledge_graph.source_svg_host import SvgRenderer

from tests.knowledge_graph.test_source_reader_contracts import (
    DOCX,
    PDF,
    PNG,
    _docx_page_response,
    _docx_text_response,
    _image_response,
    _pdf_page_response,
    _pdf_text_response,
)

_SESSION = [
    "--consumer-id",
    "consumer",
    "--producer-agent-id",
    "agent",
    "--task-id",
    "task",
    "--producer-run-id",
    "run",
    "--subscription-id",
    "subscription",
    "--subject-id",
    "entity_task",
]


class ContractReader:
    """Small trusted fake that applies the real request/response contract."""

    def __init__(self, after_read: Callable[[Event | None], None] | None = None) -> None:
        self.calls: list[tuple[ReaderRequest, bytes, Event | None]] = []
        self.after_read = after_read

    def read(
        self, request: ReaderRequest, payload: bytes, cancelled: Event | None = None
    ) -> dict[str, object]:
        self.calls.append((request, payload, cancelled))
        responses: dict[str, Callable[[], dict[str, object]]] = {
            "image-inspect": lambda: _image_response(rendered=False),
            "image-page": lambda: _image_response(rendered=True),
            "pdf-text": _pdf_text_response,
            "pdf-page": _pdf_page_response,
            "docx-text": _docx_text_response,
            "docx-page": _docx_page_response,
        }
        result = validate_reader_response(request, payload, encode(responses[request.operation]()))
        if self.after_read is not None:
            self.after_read(cancelled)
        return result


class FailingReader:
    def read(
        self, request: ReaderRequest, payload: bytes, cancelled: Event | None = None
    ) -> dict[str, object]:
        raise SourceArchiveError("source_unavailable")


class SourceReaderRouteTests(unittest.TestCase):
    def test_source_host_preserves_authorize_local_as_first_positional_argument(self) -> None:
        reader = ContractReader()

        def authorize(_path: Path) -> bool:
            return True

        legacy = SourceHost(authorize)
        explicit = SourceHost(authorize, reader=reader)

        self.assertIs(legacy.authorize_local, authorize)
        self.assertIsNone(legacy.reader)
        self.assertIs(explicit.authorize_local, authorize)
        self.assertIs(explicit.reader, reader)

    def test_exact_six_routes_use_contract_requests_and_the_host_event(self) -> None:
        reader = ContractReader()
        cancelled = Event()
        host = SourceHost(reader=reader, cancelled=cancelled)
        pdf_args = argparse.Namespace(
            visual_review_sha256=digest(PDF), visual_pages=2, reader_python=None
        )
        office_args = argparse.Namespace(reader_python=None)

        pdf = tedd_sources.pdf_reading(PDF, pdf_args, host=host)
        inspected = tedd_sources.image_reading(PNG, None, host=host)
        rendered = tedd_sources.image_reading(PNG, None, position=1, host=host)
        document = tedd_sources.office_reading(DOCX, "docx", office_args, host=host)
        pdf_png = tedd_sources._render_pdf_png(PDF, 2, None, None, host=host)
        docx_page = tedd_sources._office_document_page(
            cast(Archive, object()), argparse.Namespace(document_page=2), DOCX, "docx", host
        )

        self.assertEqual(pdf.original_sha256, digest(PDF))
        self.assertEqual(inspected.position, None)
        self.assertEqual(rendered.position, 1)
        self.assertEqual(document.original_sha256, digest(DOCX))
        self.assertEqual(pdf_png, PNG)
        self.assertEqual(docx_page["page"], 2)
        self.assertEqual(
            [call[0] for call in reader.calls],
            [
                ReaderRequest("pdf-text"),
                ReaderRequest("image-inspect"),
                ReaderRequest("image-page", 1),
                ReaderRequest("docx-text"),
                ReaderRequest("pdf-page", 2),
                ReaderRequest("docx-page", 2),
            ],
        )
        self.assertTrue(all(call[2] is cancelled for call in reader.calls))

    def test_reader_refuses_options_its_closed_contract_cannot_represent(self) -> None:
        reader = ContractReader()
        host = SourceHost(reader=reader, cancelled=Event())
        args = argparse.Namespace(reader_python=None)
        unsupported = (
            lambda: tedd_sources.image_reading(
                PNG, None, position=1, rectangle=(0, 0, 1, 1), host=host
            ),
            lambda: tedd_sources.image_reading(
                PNG, None, position=1, view="reconstruction", host=host
            ),
            lambda: tedd_sources.image_reading(
                PNG, None, svg=SvgRenderer(Path("/renderer"), Path("/font")), host=host
            ),
            lambda: tedd_sources.image_reading(
                PNG, None, av1=Av1Validator(Path("/validator")), host=host
            ),
            lambda: tedd_sources.office_reading(DOCX, "xlsx", args, host=host),
            lambda: tedd_sources.office_reading(DOCX, "docx", args, sheet_excerpt=True, host=host),
            lambda: tedd_sources._office_document_page(
                cast(Archive, object()), argparse.Namespace(document_page=2), DOCX, "xlsx", host
            ),
        )
        for operation in unsupported:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"),
            ):
                operation()
        self.assertEqual(reader.calls, [])

    def test_explicit_reader_failure_never_falls_back_to_a_native_reader(self) -> None:
        host = SourceHost(reader=FailingReader(), cancelled=Event())
        args = argparse.Namespace(
            visual_review_sha256=digest(PDF), visual_pages=2, reader_python="/native"
        )
        with (
            mock.patch.object(
                tedd_sources,
                "host_reading_response",
                side_effect=AssertionError("native fallback attempted"),
            ),
            self.assertRaisesRegex(SourceArchiveError, "source_unavailable"),
        ):
            tedd_sources.pdf_reading(PDF, args, host=host)

    def test_no_capability_keeps_the_native_worker_gate(self) -> None:
        args = argparse.Namespace(
            visual_review_sha256=digest(PDF), visual_pages=2, reader_python="/native"
        )
        with (
            mock.patch.object(
                tedd_sources,
                "require_worker_boundary",
                side_effect=SourceArchiveError("capability_unavailable"),
            ) as boundary,
            self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"),
        ):
            tedd_sources.pdf_reading(PDF, args)
        boundary.assert_called_once_with()


class SourceReaderEndpointTests(unittest.TestCase):
    def test_operations_forward_only_the_injected_reader_and_actual_turn_event(self) -> None:
        from tedd_sources_mcp import SourceOperations

        reader = ContractReader()
        turn = SourceTurn()
        operations = SourceOperations(Path("/unused/config.json"), reader=reader)
        self.addCleanup(operations.close)
        observed: list[SourceHost] = []

        def execute(
            _args: argparse.Namespace, *, host: SourceHost | None = None
        ) -> dict[str, object]:
            assert host is not None
            observed.append(host)
            return {"status": "ok"}

        with mock.patch.object(tedd_sources, "execute", side_effect=execute):
            result = operations("inspect", ["--source-uri", "opaque", *_SESSION], turn)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0].reader, reader)
        self.assertIs(observed[0].cancelled, turn.cancelled)

    def test_cancelled_or_unselected_turns_never_reach_the_reader(self) -> None:
        from tedd_sources_mcp import SourceOperations

        reader = ContractReader()
        operations = SourceOperations(Path("/unused/config.json"), reader=reader)
        self.addCleanup(operations.close)
        cancelled = SourceTurn()
        cancelled.cancel()
        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
            operations("inspect", ["--source-uri", "opaque", *_SESSION], cancelled)
        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
            operations(
                "intake-file",
                [
                    "--input",
                    "/unselected/source.docx",
                    "--metadata",
                    "/unselected/metadata.json",
                    "--acquired-sha256",
                    "0" * 64,
                    "--workflow-state",
                    "active",
                    "--purpose",
                    "keep",
                    "--whole-file-reason",
                    "Synthetic denied source",
                    *_SESSION,
                ],
                SourceTurn(),
            )
        self.assertEqual(reader.calls, [])

    def test_cancellation_after_reader_success_cannot_publish(self) -> None:
        from tedd_sources_mcp import SourceOperations

        from tests.knowledge_graph.test_source_cli import SourceCliTests

        fixture = SourceCliTests(methodName="runTest")
        with mock.patch.dict(
            os.environ,
            {"TEDD_PRIVATE_TEMP_PARENT": str(Path(tempfile.gettempdir()).resolve())},
        ):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        capture = fixture._document_args("docx", DOCX)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        source = fixture.root / "neutral.txt"
        turn = SourceTurn(local_files=frozenset({str(source)}))

        def cancel(event: Event | None) -> None:
            assert event is turn.cancelled
            event.set()

        reader = ContractReader(cancel)
        operations = SourceOperations(fixture.config, reader=reader)
        self.addCleanup(operations.close)
        before_archive = {
            path.relative_to(fixture.archive_root): path.read_bytes()
            for path in fixture.archive_root.rglob("*")
            if path.is_file()
        }
        graph_before = fixture.store.read_bytes()
        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
            operations(
                "intake-file",
                [
                    *capture[1:],
                    "--purpose",
                    "keep",
                    "--whole-file-reason",
                    "Synthetic reader cancellation fixture",
                ],
                turn,
            )
        after_archive = {
            path.relative_to(fixture.archive_root): path.read_bytes()
            for path in fixture.archive_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual([call[0] for call in reader.calls], [ReaderRequest("docx-text")])
        self.assertEqual(after_archive, before_archive)
        self.assertEqual(fixture.store.read_bytes(), graph_before)


class SourceReaderTypingTests(unittest.TestCase):
    def test_contract_fake_satisfies_the_reader_protocol(self) -> None:
        reader: SourceReader = ContractReader()
        self.assertIsInstance(reader.read(ReaderRequest("pdf-text"), PDF), dict)


if __name__ == "__main__":
    unittest.main()
