"""Selective intake checks use real files; no live attachment/download claim."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import io
import os
import queue
import random
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from tedd_knowledge_graph import source_files
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceDetails,
    SourceMetadata,
    SourceScope,
    decode_metadata,
    digest,
    encode,
    parse_json,
    parse_source_uri,
)
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.source_mcp import SourceTurn
from tedd_knowledge_graph.v4_application_contracts import JsonObject


class SourceIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tedd-intake-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "condition.txt"
        self.path.write_bytes(b"Not approved. Only if reviewed.\r\n")
        self.metadata = SourceMetadata(
            SourceScope("store", "1", ("entity_task",), "personal_task"),
            "untrusted-name",
            "observation",
            "text",
            "local_file",
            "neutral-fixture",
            "task",
            "run",
            "encounter",
            "2026-09-11T12:00:00Z",
            coverage="complete",
            details=SourceDetails(
                "local", "owner", "text", "original", "unread", sensitivity="ordinary"
            ),
        )

    def test_owner_confirmation_label_cannot_be_attached_to_an_ordinary_local_capture(self) -> None:
        with self.assertRaises(SourceArchiveError):
            replace(self.metadata, capture_method="owner_confirmed_export")

    def test_sheet_excerpt_keeps_copy_identity_and_original_retention_boundary(self) -> None:
        from tedd_knowledge_graph.source_intake import owner_export_batch, owner_export_description

        proposal = {
            "path": str(self.root / "selected.xlsx"),
            "origin": "https://docs.google.com/spreadsheets/d/copied-sheet/edit",
            "account": "owner@example.com",
            "kind": "xlsx",
            "sha256": "a" * 64,
            "purpose": "context",
            "whole_file_reason": "Only the selected sheet was copied; its conditions are needed",
            "sensitivity": "ordinary",
            "visual_review_sha256": None,
            "sheet_excerpt": {"source_id": "original-book", "sheet_id": "7"},
        }
        item = owner_export_batch([proposal], self.root)[0]
        excerpt = item.metadata(self.metadata.scope, "task", "run", "2026-09-11T12:00:00Z")
        whole = owner_export_batch(
            [{key: value for key, value in proposal.items() if key != "sheet_excerpt"}], self.root
        )[0].metadata(self.metadata.scope, "task", "run", "2026-09-11T12:00:00Z")
        original = replace(whole, source_id="original-book")
        self.assertEqual(excerpt.source_id, "copied-sheet")
        self.assertEqual(excerpt.origin, proposal["origin"])
        self.assertEqual(excerpt.logical_key, original.logical_key)
        self.assertNotEqual(excerpt.artifact_key, whole.artifact_key)
        self.assertEqual(excerpt.coverage, "partial")
        assert excerpt.details is not None
        self.assertIsNone(excerpt.details.provider_revision)
        self.assertIn("original-book", owner_export_description((item,)))
        self.assertIn("sheet 7", owner_export_description((item,)))
        for denied in ("original-book", "copied-sheet"):
            with self.subTest(denied=denied), self.assertRaises(SourceArchiveError):
                SourceTurn(google_denials=frozenset({denied})).check_retention(excerpt)
        SourceTurn(google_denials=frozenset({"unrelated"})).check_retention(excerpt)

    def test_owner_export_confirmation_keeps_untrusted_values_on_their_own_lines(self) -> None:
        from tedd_knowledge_graph.source_intake import OwnerExport, owner_export_description

        item = OwnerExport(
            self.root / "condition\nAccount: misleading.docx",
            "https://docs.google.com/document/d/neutral-source/edit",
            "owner@example.com",
            "docx",
            "neutral-source",
            "a" * 64,
            "context",
            "Keep the condition.\n2. File: invented.docx\tClaimed source: misleading",
            None,
        )
        description = owner_export_description((item,))
        self.assertNotIn("\nAccount: misleading", description)
        self.assertNotIn("\n2. File: invented.docx", description)
        self.assertIn(r"\nAccount: misleading", description)
        self.assertIn(r"\n2. File: invented.docx\tClaimed source: misleading", description)
        self.assertEqual(sum(line.startswith("Account: ") for line in description.splitlines()), 1)

    def test_temporary_and_purposeless_inputs_are_not_read(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        for selection, reason in (
            (IntakeSelection(), "no_retention_purpose"),
            (IntakeSelection(purpose="keep", use_once=True), "temporary_only"),
            (IntakeSelection(purpose="fact"), "context_selection_required"),
        ):
            with (
                self.subTest(reason=reason),
                mock.patch.object(
                    source_files,
                    "read_input",
                    side_effect=AssertionError("Must not read skipped input"),
                ),
            ):
                self.assertEqual(selection.skip_reason(self.path), reason)
                self.assertIsNone(prepare_local(self.path, selection, self.metadata, 1024))

    def test_generated_and_credential_paths_do_not_gain_a_bulk_exception(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection

        whole = IntakeSelection(purpose="fact", whole_file_reason="The full condition is needed")
        for name in ("node_modules/a.js", "dist/app.js", "package-lock.json", "out.min.js"):
            with self.subTest(name=name):
                self.assertEqual(whole.skip_reason(self.root / name), "generated_or_dependency")
                self.assertEqual(
                    replace(whole, relevant_generated=True).skip_reason(self.root / name),
                    "generated_or_dependency",
                )
        selected = IntakeSelection(purpose="context", byte_range=(0, 8), relevant_generated=True)
        self.assertIsNone(selected.skip_reason(self.root / "node_modules/a.js"))
        for name in (
            ".env",
            ".env.local",
            ".git/config",
            ".codex/auth.json",
            "credentials.json",
            ".obsidian/config",
        ):
            with self.subTest(name=name):
                self.assertEqual(selected.skip_reason(self.root / name), "excluded_input")

    def test_partial_acquisition_reads_only_selected_bytes_and_keeps_native_offsets(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        condition = b"Not approved.\r\nOnly if reviewed.\r\n"
        # Non-text, unselected bytes must not enter the reading or archive payload.
        self.path.write_bytes(b"\xff" * 19 + condition + b"\xff" * 5000)
        selection = IntakeSelection(purpose="context", byte_range=(19, 19 + len(condition)))
        result = prepare_local(self.path, selection, self.metadata, 1024)
        self.assertIsNotNone(result)
        assert result is not None
        payload, metadata, reading = result
        assert reading is not None
        self.assertEqual(payload, condition)
        self.assertEqual(reading.original_sha256, digest(condition))
        self.assertEqual(metadata.coverage, "partial")
        self.assertIn("partial_byte_selection", metadata.gaps)
        self.assertEqual(metadata.origin, self.path.as_uri())
        self.assertNotEqual(metadata.source_id, "untrusted-name")
        self.assertEqual(
            [unit.text for unit in reading.units], ["Not approved.\r\nOnly if reviewed.\r\n"]
        )
        location = reading.units[0].location
        assert location is not None
        self.assertEqual(location.container, f"bytes[19:{19 + len(condition)}]")

    def test_selected_codex_attachment_uses_normal_reader_without_admitting_app_state(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        codex_root = self.root / ".codex"
        selected = codex_root / "attachments" / "ad673b11-96ce-4f45-b30e-1e4f2dfb3551" / "brief.txt"
        for directory in (codex_root, codex_root / "attachments", selected.parent):
            source_files.directory(directory)
        payload = b"The worker must not stop while jobs remain.\r\n"
        source_files.write_bytes(selected, payload)
        selection = IntakeSelection(purpose="context", whole_file_reason="Keep the full condition")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_root)}):
            result = prepare_local(selected, selection, self.metadata, 1024)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result[0], payload)
            self.assertEqual(result[1].origin, selected.as_uri())
            self.assertEqual(selected.read_bytes(), payload)
            for relative in (
                "auth.json",
                "config.toml",
                "sessions/brief.txt",
                "attachments/brief.txt",
                "attachments/ad673b11-96ce-4f45-b30e-1e4f2dfb3551/auth.json",
                "attachments/ad673b11-96ce-4f45-b30e-1e4f2dfb3551/nested/brief.txt",
            ):
                with self.subTest(relative=relative):
                    self.assertEqual(selection.skip_reason(codex_root / relative), "excluded_input")
            elsewhere = (
                self.root
                / "unrelated"
                / ".codex"
                / "attachments"
                / selected.parent.name
                / selected.name
            )
            self.assertEqual(selection.skip_reason(elsewhere), "excluded_input")

    def test_same_name_elsewhere_is_not_same_source_and_ranges_are_distinct_artifacts(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        other = self.root / "other"
        other.mkdir()
        other_path = other / self.path.name
        other_path.write_bytes(self.path.read_bytes())
        whole = IntakeSelection(purpose="keep", whole_file_reason="Both conditions are needed")
        first = prepare_local(self.path, whole, self.metadata, 1024)
        second = prepare_local(other_path, whole, self.metadata, 1024)
        partial = prepare_local(
            self.path, IntakeSelection(purpose="context", byte_range=(0, 13)), self.metadata, 1024
        )
        assert first is not None and second is not None and partial is not None
        self.assertNotEqual(first[1].logical_key, second[1].logical_key)
        self.assertEqual(first[1].logical_key, partial[1].logical_key)
        self.assertNotEqual(first[1].artifact_key, partial[1].artifact_key)

    def test_folder_bounds_and_traversal_are_checked_before_reading(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        selected = self.root / "selected"
        selected.mkdir()
        selection = IntakeSelection(
            purpose="keep", whole_file_reason="Required condition", root=selected
        )
        for path in (self.path, selected / ".." / self.path.name):
            with (
                self.subTest(path=path),
                mock.patch.object(
                    source_files,
                    "read_input",
                    side_effect=AssertionError("Outside selection was read"),
                ),
                self.assertRaises(SourceArchiveError),
            ):
                prepare_local(path, selection, self.metadata, 1024)

    def test_selection_limits_and_changed_or_malformed_source_are_not_silently_trimmed(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        for bounds in ((-1, 2), (2, 2), (True, 3), (0, 2048)):
            with self.subTest(bounds=bounds), self.assertRaises(SourceArchiveError):
                prepare_local(
                    self.path,
                    IntakeSelection(purpose="fact", byte_range=bounds),
                    self.metadata,
                    1024,
                )
        for payload in (b"\xff\xfe", b"password=not-a-real-password"):
            self.path.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaises(SourceArchiveError):
                prepare_local(
                    self.path,
                    IntakeSelection(purpose="fact", byte_range=(0, len(payload))),
                    self.metadata,
                    1024,
                )

    def test_known_cloud_origin_cannot_be_relabelled_by_local_intake(self) -> None:
        from tedd_knowledge_graph.source_intake import IntakeSelection, prepare_local

        metadata = replace(self.metadata, origin="https://docs.google.com/document/d/neutral/edit")
        with self.assertRaises(SourceArchiveError):
            prepare_local(
                self.path,
                IntakeSelection(purpose="keep", whole_file_reason="Needed"),
                metadata,
                1024,
            )

    def test_parent_swap_cannot_read_an_outside_target(self) -> None:
        parent, outside, moved = (self.root / name for name in ("selected", "outside", "moved"))
        parent.mkdir()
        outside.mkdir()
        (parent / "item.txt").write_bytes(b"Allowed original")
        (outside / "item.txt").write_bytes(b"Outside original")
        trial = self.root / "trial"
        try:
            trial.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit a disposable symlink")
        trial.unlink()
        original = source_files.is_link_or_reparse_point
        swapped = False

        def swap(path: Path) -> bool:
            nonlocal swapped
            result = original(path)
            if path == parent and not swapped:
                swapped = True
                parent.rename(moved)
                parent.symlink_to(outside, target_is_directory=True)
            return result

        with mock.patch.object(source_files, "is_link_or_reparse_point", side_effect=swap):
            try:
                payload = source_files.read_input(parent / "item.txt", 1024)
            except SourceArchiveError:
                pass
            else:
                self.assertEqual(payload, b"Allowed original")
        self.assertTrue(swapped)

    def test_new_identity_groups_formats_but_preserves_legacy_identity_and_manifest_bytes(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import (
            SourceManifest,
            decode_manifest,
            encode_manifest,
        )

        details = self.metadata.details
        assert details is not None
        first = replace(
            self.metadata, details=replace(details, provider="google", identity_version=2)
        )
        assert first.details is not None
        second = replace(
            first, kind="docx", details=replace(first.details, representation_id="docx")
        )
        legacy = self.metadata
        legacy_other = replace(legacy, kind="docx")
        self.assertEqual(first.logical_key, second.logical_key)
        self.assertNotEqual(first.artifact_key, second.artifact_key)
        self.assertNotEqual(legacy.logical_key, legacy_other.logical_key)
        self.assertNotEqual(first.logical_key, legacy.logical_key)
        for metadata, version in ((legacy, 3), (first, 4)):
            with self.subTest(version=version):
                manifest = SourceManifest(
                    metadata, "1" * 64, 1, "2" * 64, 1, None, format_version=version
                )
                raw = encode_manifest(manifest)
                self.assertEqual(encode_manifest(decode_manifest(raw)), raw)
                self.assertEqual(b'"identity_version"' in raw, version == 4)

    def test_saved_reference_denial_preserves_unrestricted_legacy_and_other_origins(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn

        SourceTurn().check_retention(self.metadata)  # Legacy origin=None remains usable.
        denied = SourceTurn(local_denials=frozenset({str(self.path)}))
        with self.assertRaises(SourceArchiveError):
            denied.check_retention(self.metadata)  # Ambiguous mapping cannot defeat a refusal.
        with self.assertRaises(SourceArchiveError):
            denied.check_retention(replace(self.metadata, origin=self.path.as_uri()))
        denied.check_retention(replace(self.metadata, origin=(self.root / "other.txt").as_uri()))
        if os.name == "nt":
            remote = Path(r"\\neutral-host\selected\condition.txt")
            remote_metadata = replace(self.metadata, origin=remote.as_uri())
            denied.check_retention(
                remote_metadata
            )  # No network access; canonical URI parsing only.
            with self.assertRaises(SourceArchiveError):
                SourceTurn(local_denials=frozenset({str(remote.parent)})).check_retention(
                    remote_metadata
                )

    def test_identity_isolates_unknown_observations_principals_and_scopes(self) -> None:
        details = self.metadata.details
        assert details is not None
        first = replace(self.metadata, details=replace(details, identity_version=2))
        assert first.details is not None
        for other in (
            replace(first, details=replace(first.details, principal="other-owner")),
            replace(first, scope=replace(first.scope, store_id="other-store")),
            replace(first, scope=replace(first.scope, generation="2")),
            replace(first, scope=replace(first.scope, subject_ids=("entity_other",))),
        ):
            self.assertNotEqual(
                (first.scope.key, first.logical_key), (other.scope.key, other.logical_key)
            )
        unknown = replace(first, identity_known=False)
        self.assertNotEqual(
            unknown.logical_key, replace(unknown, interaction_id="other").logical_key
        )


class SourceIntakeCommandTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
    def test_location_metadata_requires_restricted_owner_authority_before_review_and_preservation(
        self,
    ) -> None:
        from tests.knowledge_graph.test_source_images import exif_location
        from tests.knowledge_graph.test_source_office import png_chunk, png_image

        plain = png_image(width=2, height=2)
        original = plain[:33] + png_chunk(b"eXIf", exif_location()[6:]) + plain[33:]
        self._owner_metadata_source_flow(original, "restricted")

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_AV1_VALIDATOR"),
        "optional native AV1 qualification not configured",
    )
    def test_avif_description_metadata_requires_confidential_owner_review(self) -> None:
        from tests.knowledge_graph.test_source_bmff import AVIF_SIX_COLORS

        self._owner_metadata_source_flow(AVIF_SIX_COLORS, "confidential", av1=True)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_AV1_VALIDATOR"),
        "optional native AV1 qualification not configured",
    )
    def test_avif_sequence_requires_every_owner_review_and_reopens_exact_saved_frames(self) -> None:
        from tests.knowledge_graph.test_source_bmff import AVIF_SEQUENCE

        self._owner_metadata_source_flow(AVIF_SEQUENCE, "confidential", av1=True, units=4)

    def _owner_metadata_source_flow(
        self, original: bytes, classification: str, *, av1: bool = False, units: int = 1
    ) -> None:
        from contextlib import AbstractContextManager

        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_review import ReviewRendering

        fixture = self.fixture
        capture = fixture._document_args("image", original)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [
            *capture[1:],
            "--purpose",
            "keep",
            "--whole-file-reason",
            "Selected synthetic image",
        ]
        descriptor = Path(argv[argv.index("--metadata") + 1])
        source = decode_metadata(parse_json(descriptor.read_bytes()))
        assert source.details is not None
        self._start_source_endpoint(av1=av1)
        call, content = self._source_call, self._source_content
        before = self.archive_bytes(), fixture.store.read_bytes()
        refused = call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]})
        self.assertTrue(refused.get("isError"), refused)
        self.assertNotIn(b'"type":"image"', encode(refused))
        self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)

        source_files.write_bytes(
            descriptor,
            encode(
                asdict(replace(source, details=replace(source.details, sensitivity=classification)))
            ),
            overwrite=True,
        )
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            classification,
            *fixture.session,
        )
        viewed: list[str] = []

        def owner_review(
            rendering: ReviewRendering,
            png: bytes,
            _cancelled: object,
            *,
            handoff: AbstractContextManager[None],
        ) -> bool:
            with handoff:
                self.assertEqual(rendering.audience, "owner")
                viewed.append(digest(png))
            return True

        owner = [*argv, "--owner-review"]
        if sys.platform == "darwin":
            self.assert_macos_reader_refusal(
                call(
                    "intake_file",
                    {"argv": [*owner, "--review-only", "--image-unit", "1"]},
                ),
                before,
                includes_boundary_reason=False,
            )
            return
        reviewed_responses: list[JsonObject] = []
        with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
            viewer.return_value.review_image.side_effect = owner_review
            for position in range(1, units + 1):
                reviewed_responses.append(
                    content(
                        call(
                            "intake_file",
                            {"argv": [*owner, "--review-only", "--image-unit", str(position)]},
                        )
                    )
                )
                if position < units:
                    refused = call("intake_file", {"argv": [*owner]})
                    self.assertTrue(refused.get("isError"), refused)
                    self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
        self.assertEqual(len(viewed), units)
        saved = content(call("intake_file", {"argv": [*owner]}))
        self.assertIn(original, self.archive_bytes().values())
        value = saved["ref"]
        assert isinstance(value, dict)
        ref = SourceRef(
            *(str(value[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        )
        self._begin_source_turn("saved-owner-metadata")
        shown_responses: list[JsonObject] = []
        with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
            with mock.patch.object(
                source_files, "read_input", side_effect=AssertionError("reacquired original")
            ):
                for position in range(1, units + 1):
                    shown_responses.append(
                        content(
                            call(
                                "owner_view",
                                {
                                    "argv": [
                                        "--source-uri",
                                        replace(ref, anchor_id=f"u{position}").as_uri(),
                                        "--private-viewer",
                                        "--image",
                                        *fixture.session,
                                    ]
                                },
                            )
                        )
                    )
            self.assertEqual(viewer.return_value.display_rendering.call_count, units)
        for response in (*reviewed_responses, saved, *shown_responses):
            self.assertNotIn(b"metadata_categories", encode(response))
            self.assertNotIn(b"png_base64", encode(response))
        self.assertTrue(
            call("render_image", {"argv": ["--source-uri", ref.as_uri(), *fixture.session]}).get(
                "isError"
            )
        )
        self.assertEqual(fixture.store.read_bytes(), before[1])
        self.assertEqual((fixture.root / "neutral.txt").read_bytes(), original)

    def _start_source_endpoint(self, *, svg: bool = False, av1: bool = False) -> None:
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_sources_mcp import SourceOperations

        operations = SourceOperations(
            self.fixture.config,
            reader_python=Path(sys.executable),
            svg_renderer=Path(os.environ["TEDD_TEST_SVG_RENDERER"]) if svg else None,
            svg_font=Path(os.environ["TEDD_TEST_SVG_FONT"]) if svg else None,
            av1_validator=Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]) if av1 else None,
        )
        self.addCleanup(operations.close)
        self._source_server = SourceMcpServer(operations)
        self.addCleanup(self._source_server.close)
        self._source_server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
        )
        self._begin_source_turn("first")

    def _begin_source_turn(self, turn_id: str) -> None:
        self._source_turn_id = turn_id
        self._source_server.lifecycle.begin(
            {"session_id": "thread", "turn_id": turn_id}, {"threadId": "thread"}
        )

    def _source_call(self, name: str, arguments: JsonObject) -> JsonObject:
        metadata = {
            "threadId": "thread",
            "callId": name,
            "x-codex-turn-metadata": {
                "thread_id": "thread",
                "session_id": "thread",
                "turn_id": self._source_turn_id,
            },
        }
        self._source_server.lifecycle.acquire(metadata).local_files = frozenset(
            str(self.fixture.root / name) for name in ("neutral.txt", "owner-image.png")
        )
        replies: queue.Queue[JsonObject] = queue.Queue()
        self._source_server.receive_line(
            encode(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments, "_meta": metadata},
                }
            ),
            replies.put,
        )
        response = replies.get(timeout=10)
        self.assertTrue(self._source_server._busy.acquire(timeout=5))
        self._source_server._busy.release()
        result = response["result"]
        assert isinstance(result, dict)
        return result

    def _source_content(self, result: JsonObject) -> JsonObject:
        self.assertFalse(result.get("isError", False), result)
        value = result["structuredContent"]
        assert isinstance(value, dict)
        return value

    def assert_macos_reader_refusal(
        self,
        result: JsonObject,
        before: tuple[dict[str, bytes], bytes],
        *,
        includes_boundary_reason: bool = True,
    ) -> None:
        expected: JsonObject = {"status": "capability_unavailable"}
        if includes_boundary_reason:
            expected["reason"] = "macos_worker_boundary_unqualified"
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(result["structuredContent"], expected)
        self.assertNotIn(b'"type":"image"', encode(result))
        self.assertEqual((self.archive_bytes(), self.fixture.store.read_bytes()), before)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
    def test_png_cancel_after_decode_and_use_once_leave_no_durable_observation(self) -> None:
        import tedd_sources

        from tests.knowledge_graph.test_source_office import png_image

        fixture = self.fixture
        raw = png_image()
        capture = fixture._document_args("image", raw)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [
            *capture[1:],
            "--purpose",
            "keep",
            "--whole-file-reason",
            "Selected synthetic color",
        ]
        self._start_source_endpoint()
        call, content = self._source_call, self._source_content
        before = self.archive_bytes(), fixture.store.read_bytes()
        if sys.platform == "darwin":
            self.assert_macos_reader_refusal(
                call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}),
                before,
            )
            return
        reviewed = content(
            call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]})
        )
        content(call("acknowledge_review", {"review_id": reviewed["review_id"]}))
        read_image = tedd_sources.image_reading

        def cancel_after_decode(
            payload: bytes,
            reader: object,
            *,
            position: int | None = None,
            rectangle: Sequence[int] | None = None,
            view: str = "presentation",
            svg: tedd_sources.SvgRenderer | None = None,
            av1: tedd_sources.Av1Validator | None = None,
            host: SourceHost | None = None,
        ) -> tedd_sources.ImageReading:
            result = read_image(
                payload,
                reader,
                position=position,
                rectangle=rectangle,
                view=view,
                svg=svg,
                av1=av1,
                host=host,
            )
            self._source_server.lifecycle.end(
                {"session_id": "thread", "turn_id": "first"}, {"threadId": "thread"}
            )
            return result

        with mock.patch.object(tedd_sources, "image_reading", cancel_after_decode):
            self.assertTrue(call("intake_file", {"argv": [*argv]}).get("isError"))
        self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
        self._begin_source_turn("next")
        self.assertTrue(
            call("acknowledge_review", {"review_id": reviewed["review_id"]}).get("isError")
        )
        self.assertTrue(call("intake_file", {"argv": [*argv]}).get("isError"))
        skipped = content(call("intake_file", {"argv": [*argv, "--use-once"]}))
        self.assertEqual(skipped["status"], "not_preserved")
        self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
        self.assertEqual((fixture.root / "neutral.txt").read_bytes(), raw)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
    def test_png_memory_binding_dispute_and_later_version_keep_exact_evidence(self) -> None:
        import knowledge_graph_v4 as v4
        import tedd_sources
        from tedd_knowledge_graph.source_contracts import SourceRef

        from tests.knowledge_graph.test_source_host import SourceHostTests
        from tests.knowledge_graph.test_source_office import png_image

        fixture = self.fixture
        original = png_image()
        capture = fixture._document_args("image", original)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [
            *capture[1:],
            "--purpose",
            "context",
            "--whole-file-reason",
            "The selected color is evidence",
        ]
        self._start_source_endpoint()
        call, content = self._source_call, self._source_content
        before = self.archive_bytes(), fixture.store.read_bytes()
        if sys.platform == "darwin":
            self.assert_macos_reader_refusal(
                call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}),
                before,
            )
            return
        reviewed = content(
            call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]})
        )
        content(call("acknowledge_review", {"review_id": reviewed["review_id"]}))
        saved = content(call("intake_file", {"argv": [*argv]}))["ref"]
        assert isinstance(saved, dict)
        ref = SourceRef(
            *(str(saved[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        )
        archived = self.archive_bytes()
        graph_before = fixture.store.read_bytes()

        # Deliberately wrong wording is a neutral fixture, not a model conclusion.
        packet = SourceHostTests._memory_packet(ref)
        claims = packet["claims"]
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        claims[0]["value"] = "The square is black."
        packet_path, plan_path = fixture.root / "memory.json", fixture.root / "memory-plan.json"
        source_files.write_bytes(packet_path, encode(packet))
        memory = [
            "--input",
            str(packet_path),
            "--consumer-id",
            "consumer",
            "--producer-agent-id",
            "agent",
            "--task-id",
            "task",
            "--producer-run-id",
            "run",
            "--discovery-kind",
            "verification",
            "--based-on-watermark",
            "1",
            "--source-subscription-id",
            "subscription",
            "--source-subject-id",
            "entity_task",
            "--authority-ceiling",
            "validated_structured_candidate",
        ]
        invalid = [*memory]
        invalid[invalid.index("--source-subject-id") + 1] = "entity_unselected"
        self.assertTrue(call("bind_memory", {"argv": [*invalid, "--dry-run"]}).get("isError"))
        self.assertEqual(
            (self.archive_bytes(), fixture.store.read_bytes()), (archived, graph_before)
        )
        planned = content(
            call("bind_memory", {"argv": [*memory, "--dry-run", "--plan-output", str(plan_path)]})
        )
        self.assertNotIn("stage", planned)
        applied = content(
            call(
                "bind_memory",
                {
                    "argv": [
                        *memory,
                        "--expect-fingerprint",
                        str(planned["fingerprint"]),
                        "--expect-plan-file",
                        str(plan_path),
                    ]
                },
            )
        )
        self.assertEqual(applied["status"], "captured")
        self.assertEqual(applied["stage"], "bound")
        self.assertEqual(self.archive_bytes(), archived)
        with v4.open_store(fixture.store, readonly=True) as store:
            claim = str(store.current_claims(("approval",))[0]["record_id"])
            retrieval_before = encode(
                store.context_pack("subscription", max_items=10, max_chars=12000)
            )
        self._begin_source_turn("dispute")
        evidence = content(call("check_memory", {"argv": ["--id", claim, *fixture.session]}))
        self.assertIn(b"The square is black.", encode(evidence["memory"]))
        self.assertIn(b"image_page", encode(evidence["sources"]))
        self.assertFalse(evidence["complete"])  # No OCR and no image truth judgment.
        sources = evidence["sources"]
        assert isinstance(sources, list) and isinstance(sources[0], dict)
        evidence_uri = sources[0]["source_uri"]
        assert isinstance(evidence_uri, str)
        self.assertEqual(parse_source_uri(evidence_uri), ref)
        self.assertEqual(sources[0]["ref"], asdict(ref))
        detail = content(
            call(
                "render_image",
                {
                    "argv": [
                        "--source-uri",
                        evidence_uri,
                        "--image-rectangle",
                        "0",
                        "0",
                        "1",
                        "1",
                        *fixture.session,
                    ]
                },
            )
        )
        self.assertEqual(detail["original_sha256"], digest(original))
        self.assertEqual(detail["source_uri"], evidence_uri)

        later = png_image(pixels=b"\0\0\0")
        source_files.write_bytes(fixture.root / "neutral.txt", later, overwrite=True)
        descriptor = Path(argv[argv.index("--metadata") + 1])
        metadata = decode_metadata(parse_json(descriptor.read_bytes()))
        source_files.write_bytes(
            descriptor,
            encode(
                asdict(
                    replace(
                        metadata, interaction_id="later-image", observed_at="2026-09-06T12:00:00Z"
                    )
                )
            ),
            overwrite=True,
        )
        argv[argv.index("--acquired-sha256") + 1] = digest(later)
        self.assertTrue(call("intake_file", {"argv": [*argv]}).get("isError"))
        later_review = content(
            call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]})
        )
        content(call("acknowledge_review", {"review_id": later_review["review_id"]}))
        content(call("intake_file", {"argv": [*argv]}))
        self._begin_source_turn("later-dispute")
        old = content(
            call("render_image", {"argv": ["--source-uri", ref.as_uri(), *fixture.session]})
        )
        self.assertEqual(old["original_sha256"], digest(original))
        history = content(call("check_memory", {"argv": ["--id", claim, *fixture.session]}))
        self.assertIn(b"later_capture", encode(history))
        with v4.open_store(fixture.store, readonly=True) as store:
            self.assertEqual(
                encode(store.context_pack("subscription", max_items=10, max_chars=12000)),
                retrieval_before,
            )
        evidence_command = tedd_sources.evidence_command
        for denial in ("configuration", "archive"):
            with self.subTest(evidence_delivery=denial):

                def revoke_prepared(
                    args: argparse.Namespace,
                    store: object,
                    canonical_path: Path,
                    *,
                    host: object = None,
                    denial: str = denial,
                ) -> dict[str, object]:
                    from tedd_knowledge_graph.source_host import SourceHost

                    assert isinstance(host, SourceHost)
                    result = evidence_command(args, store, canonical_path, host=host)
                    self.assertIn(b"The square is black.", encode(result))
                    # A separate process completes denial after actual evidence
                    # preparation; the endpoint has not written its response yet.
                    if denial == "configuration":
                        fixture.invoke("configure", "--deny-local-documents", *fixture.session)
                    else:
                        fixture.invoke("restrict", "--source-uri", ref.as_uri(), *fixture.session)
                    return result

                with mock.patch.object(tedd_sources, "evidence_command", revoke_prepared):
                    refused = call("check_memory", {"argv": ["--id", claim, *fixture.session]})
                repeated = call("check_memory", {"argv": ["--id", claim, *fixture.session]})
                self.assertTrue(repeated.get("isError", False), repeated)
                if denial == "configuration":
                    fixture.invoke("configure", "--allow-local-documents", *fixture.session)
                self.assertTrue(refused.get("isError", False), refused)
                self.assertEqual(refused["structuredContent"], {"status": "scope_denied"})
                self.assertNotIn(b"The square is black.", encode(refused))
                self.assertNotIn(b"image_page", encode(refused))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
    def test_png_source_mcp_requires_delivered_review_and_renders_saved_bytes_in_new_turn(
        self,
    ) -> None:
        from tests.knowledge_graph.test_source_office import png_image

        self._reviewed_image_source_flow(png_image(width=6, height=3))

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_SVG_RENDERER"), "optional SVG qualification not configured"
    )
    def test_svg_uses_same_mcp_review_preservation_fresh_turn_and_private_owner_path(self) -> None:
        from tests.knowledge_graph.test_source_svg import SVG

        self._reviewed_image_source_flow(SVG, svg=True)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_AV1_VALIDATOR"),
        "optional native AV1 qualification not configured",
    )
    def test_avif_uses_same_mcp_review_preservation_fresh_turn_and_private_owner_path(self) -> None:
        from tests.knowledge_graph.test_source_bmff import AVIF_SIX_COLORS_UNNAMED

        self.assertEqual(
            digest(AVIF_SIX_COLORS_UNNAMED),
            "60827c215b20f0314a4b0abe79135f85f2df604d6632e95813126e00713c58c2",
        )
        self._reviewed_image_source_flow(AVIF_SIX_COLORS_UNNAMED, av1=True)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
    def test_apng_mcp_requires_poster_and_every_frame_before_exact_preservation(self) -> None:
        import struct

        from tests.knowledge_graph.test_source_images import animation_png
        from tests.knowledge_graph.test_source_office import png_chunk

        raw = animation_png()

        # A valid outer chunk with a broken LAST frame must refuse even a request
        # for the intact poster. The changed hash is accurate, not the refusal.
        offset = raw.rfind(b"fdAT") - 4
        length = struct.unpack("!I", raw[offset : offset + 4])[0]
        damaged = (
            raw[:offset]
            + png_chunk(b"fdAT", raw[offset + 8 : offset + 12] + b"not-zlib")
            + raw[offset + length + 12 :]
        )
        red, green, blue, yellow = (
            bytes((255, 0, 0, 255)),
            bytes((0, 255, 0, 255)),
            bytes((0, 0, 255, 255)),
            bytes((255, 255, 0, 255)),
        )
        expected = (red * 2, blue * 2, blue + yellow, green + blue)
        self._reviewed_multiunit_source_flow(raw, expected, (2, 1), damaged=damaged)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_AV1_VALIDATOR"),
        "optional native AV1 qualification not configured",
    )
    def test_avif_movie_only_uses_shared_mcp_frame_review_and_saved_rendering(self) -> None:
        from tests.knowledge_graph.test_source_bmff import avif_movie_only

        expected = tuple(
            bytes((*color, 255)) * 128 for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))
        )
        self._reviewed_multiunit_source_flow(
            avif_movie_only(), expected, (16, 8), av1=True, tolerance=2
        )

    @unittest.skipUnless(
        importlib.util.find_spec("pillow_heif"), "optional HEIF helper is unavailable"
    )
    def test_heif_mcp_requires_every_item_and_reconstruction_before_exact_preservation(
        self,
    ) -> None:
        from tests.knowledge_graph.test_source_bmff import hevc_ignored_tail

        # Top-level red/blue items, then the red item's separate alpha plane.
        pixels = ((254, 0, 0, 128), (0, 0, 254, 255), (128, 128, 128, 255))
        original = hevc_ignored_tail()
        self._reviewed_multiunit_source_flow(
            original,
            tuple(bytes(color) * 64 for color in pixels),
            (8, 8),
            damaged=original[:-1],
            gaps=("encoded_bytes_not_exhaustively_inspected",),
            reconstruction=(tuple(bytes(color) * 4096 for color in pixels), (64, 64)),
        )

    def _reviewed_multiunit_source_flow(
        self,
        raw: bytes,
        expected: tuple[bytes, ...],
        dimensions: tuple[int, int],
        *,
        av1: bool = False,
        damaged: bytes | None = None,
        tolerance: int = 0,
        gaps: tuple[str, ...] = ("animation_timing_not_reproduced",),
        reconstruction: tuple[tuple[bytes, ...], tuple[int, int]] | None = None,
    ) -> None:
        from contextlib import closing

        from PIL import Image
        from tedd_knowledge_graph.source_contracts import SourceRef

        fixture = self.fixture
        capture = fixture._document_args("image", raw)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [*capture[1:], "--purpose", "keep", "--whole-file-reason", "Selected image evidence"]
        self._start_source_endpoint(av1=av1)
        call, content = self._source_call, self._source_content
        before = self.archive_bytes(), fixture.store.read_bytes()
        original = fixture.root / "neutral.txt"
        if sys.platform == "darwin":
            self.assert_macos_reader_refusal(
                call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}),
                before,
            )
            return
        if damaged is not None:
            source_files.write_bytes(original, damaged, overwrite=True)
            invalid = list(argv)
            invalid[invalid.index("--acquired-sha256") + 1] = digest(damaged)
            refused = call(
                "intake_file", {"argv": [*invalid, "--review-only", "--image-unit", "1"]}
            )
            self.assertEqual(refused["structuredContent"], {"status": "source_corrupt"})
            self.assertNotIn(b'"type":"image"', encode(refused))
            source_files.write_bytes(original, raw, overwrite=True)
            self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)

        def assert_pixels(
            response: JsonObject, pixels: bytes, size: tuple[int, int] = dimensions
        ) -> None:
            blocks = response["content"]
            assert isinstance(blocks, list)
            images = [
                item for item in blocks if isinstance(item, dict) and item.get("type") == "image"
            ]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]["mimeType"], "image/png")
            data = images[0]["data"]
            assert isinstance(data, str)
            with io.BytesIO(base64.b64decode(data, validate=True)) as stream:
                with Image.open(stream, formats=["PNG"]) as image, closing(image):
                    self.assertEqual(image.size, size)
                    self.assertEqual(image.info, {})
                    actual = image.tobytes()
                    self.assertEqual(len(actual), len(pixels))
                    self.assertLessEqual(
                        max(abs(a - b) for a, b in zip(actual, pixels, strict=True)), tolerance
                    )

        for position, pixels in enumerate(expected, 1):
            response = call(
                "intake_file", {"argv": [*argv, "--review-only", "--image-unit", str(position)]}
            )
            reviewed = content(response)
            self.assertEqual(reviewed["unit_count"], len(expected))
            self.assertEqual(reviewed["original_sha256"], digest(raw))
            disclosed = reviewed["gaps"]
            assert isinstance(disclosed, list)
            for gap in (
                "image_preview_8bit_color_not_calibrated",
                "image_preview_hdr_not_reproduced",
                *gaps,
            ):
                self.assertIn(gap, disclosed)
            assert_pixels(response, pixels)
            # Delivered-but-unacknowledged last-frame pixels still cannot publish.
            refused = call("intake_file", {"argv": [*argv]})
            self.assertEqual(refused["structuredContent"], {"status": "privacy_rejected"})
            self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
            content(call("acknowledge_review", {"review_id": reviewed["review_id"]}))
            if reconstruction is not None:
                refused = call("intake_file", {"argv": [*argv]})
                self.assertEqual(refused["structuredContent"], {"status": "privacy_rejected"})
                self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
                response = call(
                    "intake_file",
                    {
                        "argv": [
                            *argv,
                            "--review-only",
                            "--image-unit",
                            str(position),
                            "--image-view",
                            "reconstruction",
                        ]
                    },
                )
                reviewed = content(response)
                assert_pixels(response, reconstruction[0][position - 1], reconstruction[1])
                content(call("acknowledge_review", {"review_id": reviewed["review_id"]}))
        saved = content(call("intake_file", {"argv": [*argv]}))
        self.assertEqual(saved["status"], "preserved")
        self.assertIn(raw, self.archive_bytes().values())
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(fixture.store.read_bytes(), before[1])
        ref = saved["ref"]
        assert isinstance(ref, dict)
        first = SourceRef(
            *(str(ref[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        )
        archived = self.archive_bytes()
        self._begin_source_turn("saved-animation")
        with mock.patch.object(
            source_files, "read_input", side_effect=AssertionError("reacquired source")
        ):
            for position, pixels in enumerate(expected, 1):
                reference = replace(first, anchor_id=f"u{position}")
                rendered = call(
                    "render_image", {"argv": ["--source-uri", reference.as_uri(), *fixture.session]}
                )
                self.assertEqual(content(rendered)["original_sha256"], digest(raw))
                self.assertEqual(content(rendered)["gaps"], reviewed["gaps"])
                assert_pixels(rendered, pixels)
        self.assertEqual(self.archive_bytes(), archived)
        self.assertEqual(fixture.store.read_bytes(), before[1])

    def _reviewed_image_source_flow(
        self, raw: bytes, *, svg: bool = False, av1: bool = False
    ) -> None:
        import tedd_sources

        fixture = self.fixture
        capture = fixture._document_args("image", raw)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [
            *capture[1:],
            "--purpose",
            "keep",
            "--whole-file-reason",
            "Keep the selected diagram",
        ]
        self._start_source_endpoint(svg=svg, av1=av1)
        server = self._source_server
        call, content = self._source_call, self._source_content

        original_archive, original_graph = self.archive_bytes(), fixture.store.read_bytes()
        for flag in ("--svg-renderer", "--svg-font", "--av1-validator"):
            refused = call("intake_file", {"argv": [*argv, flag, "model-chosen-helper"]})
            self.assertEqual(refused["structuredContent"], {"status": "scope_denied"})
        denied = call("intake_file", {"argv": [*argv, "--visual-review-sha256", digest(raw)]})
        self.assertTrue(denied.get("isError", False))
        if sys.platform == "darwin":
            self.assert_macos_reader_refusal(
                call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}),
                (original_archive, original_graph),
            )
            return
        reviewed = content(
            call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]})
        )
        self.assertEqual(reviewed["stage"], "rendered")
        self.assertFalse(reviewed["preserved"])
        self.assertNotIn("png_base64", reviewed)  # Actual MCP image block, not prompt text.
        token = reviewed["review_id"]
        self.assertIsInstance(token, str)
        self.assertTrue(call("intake_file", {"argv": [*argv]}).get("isError", False))
        self.assertEqual(
            (self.archive_bytes(), fixture.store.read_bytes()), (original_archive, original_graph)
        )
        content(call("acknowledge_review", {"review_id": token}))
        preserved = content(call("intake_file", {"argv": [*argv]}))
        self.assertEqual(preserved["status"], "preserved")
        self.assertEqual(preserved["stage"], "preserved")
        saved = preserved["ref"]
        assert isinstance(saved, dict)
        from tedd_knowledge_graph.source_contracts import SourceRef

        reference = SourceRef(
            *(str(saved[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        )
        self.assertEqual(fixture.store.read_bytes(), original_graph)
        self.assertEqual((fixture.root / "neutral.txt").read_bytes(), raw)
        saved_archive = self.archive_bytes()
        # A genuinely new source turn has no review receipts. Historical rendering
        # uses the immutable original, never the still-present selected input.
        self._begin_source_turn("second")
        with mock.patch.object(
            source_files, "read_input", side_effect=AssertionError("reacquired original")
        ):
            rendered = content(
                call(
                    "render_image",
                    {
                        "argv": [
                            "--source-uri",
                            reference.as_uri(),
                            "--image-rectangle",
                            "2",
                            "1",
                            "6",
                            "3",
                            *fixture.session,
                        ]
                    },
                )
            )
        self.assertEqual(rendered["original_sha256"], digest(raw))
        self.assertEqual(rendered["output_size"], [4, 2])
        self.assertEqual(rendered["rectangle"], [2, 1, 6, 3])
        self.assertEqual(self.archive_bytes(), saved_archive)

        # Revoke after real preparation but before the transport callback. A
        # fresh-call refusal alone cannot prove the prepared image was fenced.
        execute_source = tedd_sources.execute
        for name, arguments in (
            ("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}),
            ("render_image", {"argv": ["--source-uri", reference.as_uri(), *fixture.session]}),
        ):
            with self.subTest(delivery=name):
                prepared, release = threading.Event(), threading.Event()
                failures: queue.Queue[BaseException] = queue.Queue()

                def revoke(
                    prepared: threading.Event = prepared,
                    release: threading.Event = release,
                    failures: queue.Queue[BaseException] = failures,
                ) -> None:
                    try:
                        if not prepared.wait(10):
                            raise AssertionError("image preparation did not reach the barrier")
                        fixture.invoke("configure", "--deny-local-documents", *fixture.session)
                    except BaseException as error:
                        failures.put(error)
                    finally:
                        release.set()

                def paused_execute(
                    args: argparse.Namespace,
                    *,
                    host: object = None,
                    prepared: threading.Event = prepared,
                    release: threading.Event = release,
                ) -> dict[str, object]:
                    from tedd_knowledge_graph.source_host import SourceHost

                    assert isinstance(host, SourceHost)
                    result = execute_source(args, host=host)
                    self.assertIn("png_base64", result)
                    prepared.set()
                    self.assertTrue(release.wait(10))
                    return result

                worker = threading.Thread(target=revoke)
                worker.start()
                try:
                    with mock.patch.object(tedd_sources, "execute", paused_execute):
                        denied = call(name, {"argv": [*arguments["argv"]]})
                finally:
                    release.set()
                    worker.join(15)
                self.assertFalse(worker.is_alive())
                if not failures.empty():
                    raise failures.get_nowait()
                fixture.invoke("configure", "--allow-local-documents", *fixture.session)
                self.assertTrue(denied.get("isError", False), denied)
                self.assertNotIn(b'"type":"image"', encode(denied))
                content(call(name, {"argv": [*arguments["argv"]]}))

        # Owner-only review uses the same actual endpoint but a separate audience.
        # This adapter test controls the UI response; native pixels/button behavior
        # is independently exercised by the explicit native-viewer suite.
        descriptor = Path(argv[argv.index("--metadata") + 1])
        metadata = decode_metadata(parse_json(descriptor.read_bytes()))
        assert metadata.details is not None
        source_files.write_bytes(
            descriptor,
            encode(
                asdict(replace(metadata, details=replace(metadata.details, sensitivity="medical")))
            ),
            overwrite=True,
        )
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        with mock.patch.object(
            source_files, "read_input", side_effect=AssertionError("model read")
        ):
            self.assertTrue(
                call("intake_file", {"argv": [*argv, "--review-only", "--image-unit", "1"]}).get(
                    "isError"
                )
            )
        owner_argv = [*argv, "--owner-review"]
        owner_path = fixture.root / "owner-image.png"
        source_files.write_bytes(owner_path, raw)
        owner_argv[owner_argv.index("--input") + 1] = str(owner_path)
        from contextlib import AbstractContextManager

        from tedd_knowledge_graph.source_review import ReviewRendering

        for outcome in ("declined", "cancel-before", "cancel-after", "revoked", "accepted"):
            with self.subTest(owner=outcome):
                turn_id = outcome
                self._begin_source_turn(turn_id)
                observed: list[str] = []

                def owner_review(
                    rendering: ReviewRendering,
                    png: bytes,
                    _cancelled: object,
                    *,
                    handoff: AbstractContextManager[None],
                    outcome: str = outcome,
                    turn_id: str = turn_id,
                    observed: list[str] = observed,
                ) -> bool:
                    self.assertEqual(rendering.audience, "owner")
                    if outcome == "cancel-before":
                        server.lifecycle.end(
                            {"session_id": "thread", "turn_id": turn_id}, {"threadId": "thread"}
                        )
                    with handoff:
                        observed.append(digest(png))
                    # No publication/configuration lock may span owner input.
                    with (
                        tedd_sources.graph.store_lock(fixture.config, 0),
                        tedd_sources.publication_reservation(fixture.store),
                    ):
                        pass
                    if outcome == "cancel-after":
                        server.lifecycle.end(
                            {"session_id": "thread", "turn_id": turn_id}, {"threadId": "thread"}
                        )
                    if outcome == "revoked":
                        fixture.invoke(
                            "configure-owner-view", "--deny-owner-view", *fixture.session
                        )
                    return outcome != "declined"

                with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
                    viewer.return_value.review_image.side_effect = owner_review
                    response = call(
                        "intake_file", {"argv": [*owner_argv, "--review-only", "--image-unit", "1"]}
                    )
                self.assertNotIn(b'"type":"image"', encode(response))
                self.assertNotIn(b"png_base64", encode(response))
                if outcome != "accepted":
                    self.assertTrue(response.get("isError", False), response)
                    self.assertEqual(self.archive_bytes(), saved_archive)
                    if outcome == "revoked":
                        fixture.invoke(
                            "configure-owner-view",
                            "--allow-owner-view",
                            "--sensitivity",
                            "medical",
                            *fixture.session,
                        )
                    continue
                owner_result = content(response)
                self.assertEqual(owner_result["stage"], "reviewed")
                self.assertTrue(owner_result["human_only"])
                self.assertNotIn("review_id", owner_result)
                self.assertEqual(len(observed), 1)
                owner_saved = content(call("intake_file", {"argv": [*owner_argv]}))
                self.assertEqual(owner_saved["stage"], "preserved")
                value = owner_saved["ref"]
                assert isinstance(value, dict)
                owner_ref = SourceRef(
                    *(
                        str(value[key])
                        for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                    )
                )
                with mock.patch.object(
                    tedd_sources,
                    "image_reading",
                    side_effect=AssertionError("model decoded owner source"),
                ):
                    self.assertTrue(
                        call(
                            "render_image",
                            {"argv": ["--source-uri", owner_ref.as_uri(), *fixture.session]},
                        ).get("isError")
                    )
                with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
                    shown = content(
                        call(
                            "owner_view",
                            {
                                "argv": [
                                    "--source-uri",
                                    owner_ref.as_uri(),
                                    "--private-viewer",
                                    "--image",
                                    "--image-rectangle",
                                    "1",
                                    "1",
                                    "6",
                                    "3",
                                    *fixture.session,
                                ]
                            },
                        )
                    )
                    viewer.return_value.display_rendering.assert_called_once()
                    rendering, pixels = viewer.return_value.display_rendering.call_args.args
                    self.assertEqual(
                        (rendering.rectangle, rendering.output_size, rendering.audience),
                        ((1, 1, 6, 3), (5, 2), "owner"),
                    )
                    self.assertEqual(digest(pixels), rendering.png_sha256)
                    self.assertEqual(shown["status"], "owner_view_dispatched")
                    self.assertNotIn(b"png_base64", encode(shown))
        self.assertEqual(fixture.store.read_bytes(), original_graph)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_OFFICE_CONVERTER")
        and os.environ.get("TEDD_TEST_PDF_RENDERER")
        and importlib.util.find_spec("docx")
        and importlib.util.find_spec("pdfplumber")
        and importlib.util.find_spec("pypdf"),
        "explicit isolated Office layout helper check",
    )
    def test_real_document_pages_review_capture_and_offline_read(self) -> None:
        import knowledge_graph_v4 as v4
        import knowledge_graph_v4_capture as graph_capture
        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, ProducerEnvelope
        from tedd_sources_mcp import SourceOperations

        from tests.knowledge_graph.test_source_host import SourceHostTests
        from tests.knowledge_graph.test_source_office import two_page_word_document

        condition = "Reviewed changes may ship. See page two for the exception."
        exception = "Do not ship if backup verification failed."
        raw = two_page_word_document(first=condition, second=exception)
        fixture = self.fixture
        capture = fixture._document_args("docx", raw)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        root = fixture.root / "conversion"
        source_files.directory(root)
        operations = SourceOperations(
            fixture.config,
            reader_python=Path(sys.executable),
            renderer=Path(os.environ["TEDD_TEST_PDF_RENDERER"]),
            office_converter=Path(os.environ["TEDD_TEST_OFFICE_CONVERTER"]),
            office_work_root=root,
        )
        self.addCleanup(operations.close)
        path = fixture.root / "neutral.txt"
        turn = SourceTurn(local_files=frozenset({str(path)}))
        argv = [
            *capture[1:],
            "--purpose",
            "context",
            "--whole-file-reason",
            "Both pages qualify the condition",
        ]
        before = self.archive_bytes(), fixture.store.read_bytes()
        reviewed = operations("intake-file", [*argv, "--review-only", "--document-page", "2"], turn)
        self.assertEqual(reviewed["status"], "source_review")
        self.assertEqual(reviewed["original_sha256"], digest(raw))
        self.assertEqual(reviewed["page_count"], 2)
        self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), before)
        with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
            operations("intake-file", argv, turn)  # Rendering alone is not visual approval.
        saved = operations("intake-file", [*argv, "--visual-review-sha256", digest(raw)], turn)
        value = saved["ref"]
        assert isinstance(value, dict)
        ref = SourceRef(
            *(str(value[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        )
        path.unlink()
        archived = self.archive_bytes()
        rendered = operations(
            "render-document",
            ["--source-uri", ref.as_uri(), "--document-page", "2", *fixture.session],
            turn,
        )
        self.assertEqual(rendered["original_sha256"], digest(raw))
        self.assertEqual(rendered["page_count"], 2)
        self.assertEqual(rendered["png_sha256"], reviewed["png_sha256"])
        self.assertEqual(self.archive_bytes(), archived)
        self.assertEqual(fixture.store.read_bytes(), before[1])
        self.assertEqual(list(root.iterdir()), [])

        # A misleading summary must not hide the separately located exception.
        # Capture/review use real graph boundaries; no model judgment is simulated.
        packet = SourceHostTests._memory_packet(ref)
        request = CaptureRequest(
            "-",
            True,
            "consumer",
            ProducerEnvelope("agent", "task", None, "run", "verification", 1),
            "validated_structured_candidate",
            False,
            False,
            None,
            None,
            None,
            {},
            (),
            input_bytes=encode(packet),
            source_config_path=fixture.config,
            source_subscription_id="subscription",
            source_subject_ids=("entity_task",),
        )
        with v4.open_store(fixture.store) as store:
            planned = graph_capture.execute_capture(store, request)
            graph_capture.execute_capture(
                store,
                replace(
                    request,
                    dry_run=False,
                    expected_fingerprint=str(planned["fingerprint"]),
                    plan_token=str(planned["plan_token"]),
                ),
            )
            claim = str(store.current_claims(("approval",))[0]["record_id"])
        before_check = fixture.store.read_bytes()
        evidence = operations("source-evidence", ["--id", claim, *fixture.session], turn)
        for wording in (condition, exception):
            self.assertIn(wording.encode(), encode(evidence))
        self.assertEqual(fixture.store.read_bytes(), before_check)
        self.assertEqual(self.archive_bytes(), archived)

        # Only an independently authorized reviewed successor changes the memory.
        claims = packet["claims"]
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        correction = "Reviewed changes may ship only if backup verification did not fail."
        claims[0].update(
            value=correction, conflict_policy="replace", valid_from="2026-09-06T12:01:00Z"
        )
        with v4.open_store(fixture.store) as store:
            corrected = replace(
                request,
                input_bytes=encode(packet),
                allow_replace=True,
                envelope=replace(request.envelope, based_on_watermark=store.watermark),
            )
            proposed = graph_capture.execute_capture(store, corrected)
        self.assertEqual(fixture.store.read_bytes(), before_check)
        self.assertEqual(self.archive_bytes(), archived)
        with v4.open_store(fixture.store) as store:
            graph_capture.execute_capture(
                store,
                replace(
                    corrected,
                    dry_run=False,
                    expected_fingerprint=str(proposed["fingerprint"]),
                    plan_token=str(proposed["plan_token"]),
                ),
            )
        history = operations(
            "source-evidence", ["--id", claim, "--revision", "1", *fixture.session], turn
        )
        self.assertIn(b"Deployment approved", encode(history["memory"]))
        self.assertIn(correction.encode(), encode(history["memory"]))
        self.assertIn(exception.encode(), encode(history))
        self.assertEqual(self.archive_bytes(), archived)

        # A different supplied copy genuinely lacks its exception; do not borrow
        # the first source's text or imply that the absence is approval.
        incomplete = fixture.root / "incomplete.docx"
        missing = two_page_word_document(first=condition, second="Exception page unavailable.")
        incomplete.write_bytes(missing)
        descriptor = fixture.root / "document-metadata.json"
        metadata = decode_metadata(parse_json(descriptor.read_bytes()))
        source_files.write_bytes(
            descriptor,
            encode(asdict(replace(metadata, coverage="partial", gaps=("missing_exception_page",)))),
            overwrite=True,
        )
        missing_args = list(argv)
        missing_args[missing_args.index("--input") + 1] = str(incomplete)
        missing_args[missing_args.index("--acquired-sha256") + 1] = digest(missing)
        missing_turn = SourceTurn(local_files=frozenset({str(incomplete)}))
        missing_saved = operations(
            "intake-file", [*missing_args, "--visual-review-sha256", digest(missing)], missing_turn
        )
        missing_value = missing_saved["ref"]
        assert isinstance(missing_value, dict)
        missing_ref = SourceRef(
            *(
                str(missing_value[key])
                for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
            )
        )
        incomplete.unlink()
        after_missing = self.archive_bytes(), fixture.store.read_bytes()
        limited = operations(
            "inspect", ["--source-uri", missing_ref.as_uri(), *fixture.session], missing_turn
        )
        self.assertEqual(limited["coverage"], "partial")
        self.assertIn(b"missing_exception_page", encode(limited))
        self.assertNotIn(exception.encode(), encode(limited))
        self.assertEqual((self.archive_bytes(), fixture.store.read_bytes()), after_missing)
        self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Optional Pillow reader is unavailable")
    def test_image_review_capture_and_private_view_use_the_same_exact_frame(self) -> None:
        from tests.knowledge_graph.test_source_office import word_document

        self._assert_image_review_capture_and_view(word_document())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Optional Pillow reader is unavailable")
    def test_word_template_review_capture_and_view_keep_exact_source_binding(self) -> None:
        from tests.knowledge_graph.test_source_office import bibliography_document

        self._assert_image_review_capture_and_view(bibliography_document())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Optional Pillow reader is unavailable")
    def test_word_note_reader_keeps_image_review_and_offline_view_compatible(self) -> None:
        from tests.knowledge_graph.test_source_office import noted_word_document

        self._assert_image_review_capture_and_view(
            noted_word_document("footnote"), image_position=5, expected_text=b"[footnote:7]"
        )

    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("docx"),
        "Optional document fixture/reader unavailable",
    )
    def test_ordinary_word_metadata_survives_review_capture_and_offline_private_view(self) -> None:
        module = __import__("docx", fromlist=["Document"])
        document = module.Document()
        for text in ("Not approved.", "Review first.", "Only then."):
            document.add_paragraph(text)
        output = io.BytesIO()
        document.save(output)
        self._assert_image_review_capture_and_view(output.getvalue(), image_position=5)

    def _assert_image_review_capture_and_view(
        self, document: bytes, *, image_position: int = 4, expected_text: bytes = b"Not approved."
    ) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_host import SourceHost
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        from tests.knowledge_graph.test_source_office import image_document, png_image

        fixture = self.fixture
        raw = image_document(
            png_image(width=512, height=768, pixels=random.Random(0).randbytes(512 * 768 * 3)),
            "word/media/image.png",
            document=document,
        )
        capture_args = fixture._document_args("docx", raw)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        argv = [
            *capture_args[1:],
            "--purpose",
            "context",
            "--whole-file-reason",
            "The image qualifies the condition",
        ]
        path = fixture.root / "neutral.txt"
        turn = SourceTurn(local_files=frozenset({str(path)}))
        operations = SourceOperations(
            fixture.config, None, None, reader_python=Path(sys.executable)
        )
        self.addCleanup(operations.close)
        before, graph_before = self.archive_bytes(), fixture.store.read_bytes()
        if sys.platform == "darwin":
            from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable

            with self.assertRaises(MacOSWorkerUnavailable) as raised:
                operations("intake-file", [*argv, "--review-only"], turn)
            self.assertEqual(raised.exception.code, "capability_unavailable")
            self.assertEqual(raised.exception.reason, "macos_worker_boundary_unqualified")
            self.assertEqual(self.archive_bytes(), before)
            self.assertEqual(fixture.store.read_bytes(), graph_before)
            return
        reviewed = operations("intake-file", [*argv, "--review-only"], turn)
        self.assertEqual(reviewed["status"], "source_review")
        self.assertEqual(reviewed["unit_count"], image_position)
        self.assertEqual(reviewed["original_sha256"], digest(raw))
        self.assertIn(expected_text, encode(reviewed))
        interrupted = SourceTurn(local_files=turn.local_files)
        read_office = tedd_sources.office_reading

        def stop_after_read(
            payload: bytes, kind: str, args: argparse.Namespace, *, host: SourceHost | None = None
        ) -> object:
            result = read_office(payload, kind, args, host=host)
            interrupted.cancel()
            return result

        with (
            mock.patch.object(tedd_sources, "office_reading", side_effect=stop_after_read),
            self.assertRaises(SourceArchiveError),
        ):
            operations("intake-file", [*argv, "--review-only"], interrupted)
        image = operations(
            "intake-file", [*argv, "--review-only", "--image-unit", str(image_position)], turn
        )
        png = base64.b64decode(str(image["png_base64"]), validate=True)
        self.assertGreater(len(png), 1024 * 1024)  # Realistic PNG, not just a one-pixel response.
        self.assertEqual(image["png_sha256"], digest(png))
        self.assertEqual(self.archive_bytes(), before)
        self.assertEqual(fixture.store.read_bytes(), graph_before)
        with self.assertRaises(SourceArchiveError):
            operations("intake-file", argv, turn)  # Decoding is not visual approval.
        self.assertEqual(self.archive_bytes(), before)
        saved = operations("intake-file", [*argv, "--visual-review-sha256", digest(raw)], turn)
        ref = saved["ref"]
        assert isinstance(ref, dict)
        selected = SourceRef(
            str(ref["archive_id"]),
            str(ref["scope_id"]),
            str(ref["manifest_sha256"]),
            f"u{image_position}",
        )
        path.unlink()  # Rendering is from the saved original, not reacquisition.
        archived = self.archive_bytes()
        rendered = operations(
            "render-image", ["--source-uri", selected.as_uri(), *fixture.session], turn
        )
        self.assertEqual(rendered["png_sha256"], digest(png))
        self.assertEqual(rendered["source_uri"], selected.as_uri())
        for bad in ("--reader-python", "--reader-p"):
            with self.assertRaises(SourceArchiveError):
                operations(
                    "render-image",
                    ["--source-uri", selected.as_uri(), *fixture.session, bad, sys.executable],
                    turn,
                )
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "ordinary",
            *fixture.session,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "owner-view",
                "--private-viewer",
                "--image",
                "--reader-python",
                sys.executable,
                "--source-uri",
                selected.as_uri(),
                *fixture.session,
            ]
        )
        display = mock.Mock()
        result = tedd_sources.execute(args, host=SourceHost(display_owner_image=display))
        self.assertEqual(result, {"status": "owner_view_dispatched", "human_only": True})
        display.assert_called_once_with(selected, png)
        self.assertEqual(self.archive_bytes(), archived)
        self.assertEqual(fixture.store.read_bytes(), graph_before)
        fixture.invoke("configure", "--deny-local-documents", *fixture.session)
        with self.assertRaises(SourceArchiveError):
            operations("render-image", ["--source-uri", selected.as_uri(), *fixture.session], turn)
        # Owner permission is separate from model permission.
        display.reset_mock()
        tedd_sources.execute(args, host=SourceHost(display_owner_image=display))
        display.assert_called_once_with(selected, png)

    def test_office_render_rejects_wrong_input_frame_and_worker_reading(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_office import read_office

        from tests.knowledge_graph.test_source_office import image_document, png_image

        raw = image_document(png_image())
        unit = read_office(raw, "docx", inspect_image=lambda _payload, _kind: 1).units[-1]
        response = {
            "status": "text_screened",
            "resources": "bounded",
            "kind": "docx",
            "original_sha256": digest(raw),
            "position": unit.position,
            "reader_id": "tedd-ooxml:4",
            "unit": asdict(unit),
            "png_sha256": digest(png_image()),
            "png_base64": base64.b64encode(png_image()).decode("ascii"),
        }
        for changed in (
            {"original_sha256": "0" * 64},
            {"position": True},
            {"position": 1},
            {"unit": asdict(replace(unit, text="Wrong frame"))},
            {"reader_id": "tedd-ooxml:3"},
            {"reader_id": ["tedd-ooxml:5"]},
            {"kind": "xlsx"},
            {"png_sha256": "0" * 64},
        ):
            with (
                self.subTest(changed=list(changed)),
                mock.patch.object(
                    tedd_sources, "host_reading_response", return_value={**response, **changed}
                ),
                self.assertRaises(SourceArchiveError),
            ):
                tedd_sources._render_office_png(raw, "docx", unit, sys.executable)

    def setUp(self) -> None:
        from tests.knowledge_graph.test_source_cli import SourceCliTests

        self.fixture = SourceCliTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def arguments(self, payload: bytes = b"Not approved. Only if reviewed.\r\n") -> list[str]:
        fixture = self.fixture
        args = fixture._document_args(payload=payload)
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        return ["intake-file", *args[1:]]

    def archive_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.fixture.archive_root)): path.read_bytes()
            for path in self.fixture.archive_root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def in_process_office_reading(
        payload: bytes,
        kind: str,
        _args: argparse.Namespace,
        *,
        sheet_excerpt: bool = False,
        host: SourceHost | None = None,
    ) -> object:
        from tedd_knowledge_graph.source_office import read_office

        return read_office(payload, kind, sheet_excerpt=sheet_excerpt)

    def assert_macos_export_refusal(
        self,
        result: dict[str, object],
        archive_before: dict[str, bytes],
        graph_before: bytes,
    ) -> None:
        self.assertEqual(result["status"], "not_preserved", result)
        self.assertEqual(result["reason"], "capability_unavailable", result)
        self.assertEqual(result["receipts"], [], result)
        self.assertEqual(self.archive_bytes(), archive_before)
        self.assertEqual(self.fixture.store.read_bytes(), graph_before)

    def export_batch(self) -> tuple[Path, Path, bytes]:
        from tests.knowledge_graph.test_source_office import word_document

        fixture = self.fixture
        fixture.invoke(
            "init", "--enable", "--archive-root", str(fixture.archive_root), *fixture.session
        )
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        root = fixture.root / "exports"
        source_files.directory(root)
        path = root / "condition.docx"
        payload = word_document()
        source_files.write_bytes(path, payload)
        batch = fixture.root / "export-batch.json"
        source_files.write_bytes(
            batch,
            encode(
                [
                    {
                        "path": str(path),
                        "origin": "https://docs.google.com/document/d/neutral-source/edit",
                        "account": "owner@example.com",
                        "kind": "docx",
                        "sha256": digest(payload),
                        "purpose": "context",
                        "whole_file_reason": "Both the refusal and its condition are needed",
                        "sensitivity": "ordinary",
                        "visual_review_sha256": None,
                    }
                ]
            ),
        )
        return batch, path, payload

    def test_owner_export_import_preserves_bytes_and_offline_provenance(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_host import SourceHost
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, payload = self.export_batch()
        fixture = self.fixture
        turn = SourceTurn()
        descriptions: list[str] = []

        def confirm(description: str) -> bool:
            descriptions.append(description)
            return True

        host = SourceHost(
            export_root=path.parent,
            confirm_exports=confirm,
            check_retention=turn.check_retention,
            collecting=lambda _session, _scope: turn.hold(),
            cancelled=turn.cancelled,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "import-exports",
                "--input",
                str(batch),
                "--reader-python",
                sys.executable,
                *fixture.session,
            ]
        )
        graph_before = fixture.store.read_bytes()
        archive_before = self.archive_bytes()
        result = tedd_sources.execute(args, host=host)
        if sys.platform == "darwin":
            self.assert_macos_export_refusal(result, archive_before, graph_before)
            return
        self.assertEqual(result["status"], "preserved", result)
        receipts = result["receipts"]
        assert isinstance(receipts, list) and isinstance(receipts[0], dict)
        ref = receipts[0]["ref"]
        assert isinstance(ref, dict)
        from tedd_knowledge_graph.source_contracts import SourceRef

        uri = SourceRef(
            *(str(ref[key]) for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id"))
        ).as_uri()
        inspected = fixture.invoke("inspect", "--source-uri", uri, *fixture.session)
        self.assertIn("Not approved.", encode(inspected).decode())
        self.assertIn("owner_confirmed_export", encode(inspected).decode())
        self.assertIn(payload, self.archive_bytes().values())
        self.assertEqual(fixture.store.read_bytes(), graph_before)
        self.assertEqual(len(descriptions), 1)
        self.assertIn("owner@example.com", descriptions[0])
        self.assertIn(digest(payload), descriptions[0])
        self.assertIn("not provider-verified", descriptions[0])

    def test_sheet_excerpt_import_keeps_exact_copy_and_original_restriction_group(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceRef, decode_manifest

        from tests.knowledge_graph.test_source_office import literal_workbook

        batch, original_path, _ = self.export_batch()
        value = parse_json(batch.read_bytes())
        assert isinstance(value, list) and isinstance(value[0], dict)
        path = original_path.with_suffix(".xlsx")
        payload = literal_workbook()
        graph = self.fixture.store.read_bytes()
        archive_before = self.archive_bytes()
        references = []
        for source_id, selection in (
            ("original-book", {}),
            ("copied-sheet", {"sheet_excerpt": {"source_id": "original-book", "sheet_id": "7"}}),
        ):
            source_files.write_bytes(path, payload, overwrite=path.exists())
            source_files.write_bytes(
                batch,
                encode(
                    [
                        {
                            **value[0],
                            "path": str(path),
                            "kind": "xlsx",
                            "sha256": digest(payload),
                            "origin": f"https://docs.google.com/spreadsheets/d/{source_id}/edit",
                            **selection,
                        }
                    ]
                ),
                overwrite=True,
            )
            result = self.run_export_batch(batch, path, SourceTurn(), lambda _text: True)
            if sys.platform == "darwin":
                self.assert_macos_export_refusal(result, archive_before, graph)
                continue
            self.assertEqual(result["status"], "preserved", result)
            receipts = result["receipts"]
            assert isinstance(receipts, list) and isinstance(receipts[0], dict)
            ref = receipts[0]["ref"]
            assert isinstance(ref, dict)
            references.append(
                SourceRef(
                    *(
                        str(ref[key])
                        for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                    )
                )
            )
            self.assertFalse(path.exists())
        if sys.platform == "darwin":
            return
        stored = self.archive_bytes()
        original_manifest = next(
            data for data in stored.values() if digest(data) == references[0].manifest_sha256
        )
        copied_manifest = next(
            data for data in stored.values() if digest(data) == references[1].manifest_sha256
        )
        self.assertEqual(decode_manifest(original_manifest).format_version, 4)
        copied = decode_manifest(copied_manifest)
        self.assertEqual(copied.format_version, 5)
        self.assertEqual(copied.metadata.source_id, "copied-sheet")
        self.assertIn(payload, stored.values())
        result = self.fixture.invoke(
            "inspect", "--source-uri", references[1].as_uri(), *self.fixture.session
        )
        self.assertEqual(result["sheet_excerpt"], {"source_id": "original-book", "sheet_id": "7"})
        self.assertIn(b"Do not stop if jobs remain.", encode(result["units"]))
        self.assertIn(b"copied_locations_not_original", encode(result["gaps"]))
        self.fixture.invoke(
            "restrict", "--source-uri", references[1].as_uri(), *self.fixture.session
        )
        for ref in references:
            with self.subTest(ref=ref):
                denied = self.fixture.invoke(
                    "inspect", "--source-uri", ref.as_uri(), *self.fixture.session, success=False
                )
                self.assertEqual(denied["status"], "scope_denied")
        self.assertIn(original_manifest, self.archive_bytes().values())
        self.assertEqual(self.fixture.store.read_bytes(), graph)

    def test_sheet_excerpt_refuses_dependencies_or_an_extra_empty_sheet_before_publication(
        self,
    ) -> None:
        from tests.knowledge_graph.test_source_office import literal_workbook

        batch, original_path, _ = self.export_batch()
        value = parse_json(batch.read_bytes())
        assert isinstance(value, list) and isinstance(value[0], dict)
        path = original_path.with_suffix(".xlsx")
        before = self.archive_bytes()
        graph_before = self.fixture.store.read_bytes()
        for changes in ({"formula": True}, {"extra_sheet": True}):
            with self.subTest(changes=changes):
                payload = literal_workbook(**changes)
                source_files.write_bytes(path, payload, overwrite=path.exists())
                source_files.write_bytes(
                    batch,
                    encode(
                        [
                            {
                                **value[0],
                                "path": str(path),
                                "kind": "xlsx",
                                "sha256": digest(payload),
                                "origin": "https://docs.google.com/spreadsheets/d/copied-sheet/edit",
                                "sheet_excerpt": {"source_id": "original-book", "sheet_id": "7"},
                            }
                        ]
                    ),
                    overwrite=True,
                )
                with mock.patch(
                    "tedd_sources.office_reading", side_effect=self.in_process_office_reading
                ):
                    result = self.run_export_batch(batch, path, SourceTurn(), lambda _text: True)
                self.assertEqual(result["status"], "not_preserved", result)
                self.assertEqual(result["reason"], "capture_incomplete")
                self.assertEqual(self.archive_bytes(), before)
                self.assertEqual(self.fixture.store.read_bytes(), graph_before)

    def test_owner_export_later_observation_preserves_old_citation_and_exact_replay(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceRef

        from tests.knowledge_graph.test_source_office import W, package

        batch, path, original = self.export_batch()
        proposal = parse_json(source_files.read_bytes(batch, 65536))
        assert isinstance(proposal, list) and isinstance(proposal[0], dict)
        later = package(
            {
                "word/document.xml": (
                    f'<w:document xmlns:w="{W}"><w:body><w:p><w:r>'
                    "<w:t>Now approved, but only if the review passed.</w:t>"
                    "</w:r></w:p></w:body></w:document>"
                ).encode()
            }
        )
        references = []
        graph = self.fixture.store.read_bytes()
        archive_before = self.archive_bytes()
        for payload, timestamp in (
            (original, "2026-09-12T12:00:00Z"),
            (later, "2026-09-12T12:01:00Z"),
        ):
            source_files.write_bytes(path, payload, overwrite=True)
            source_files.write_bytes(
                batch, encode([{**proposal[0], "sha256": digest(payload)}]), overwrite=True
            )
            # Freeze the encounter clock only for exact replay. This is a
            # synthetic owner decision, not live Chrome or human qualification.
            with mock.patch("tedd_sources.graph.utc_now", return_value=timestamp):
                saved = self.run_export_batch(batch, path, SourceTurn(), lambda _text: True)
                if sys.platform == "darwin":
                    self.assert_macos_export_refusal(saved, archive_before, graph)
                    return
                self.assertEqual(saved["status"], "preserved", saved)
                receipts = saved["receipts"]
                assert isinstance(receipts, list) and isinstance(receipts[0], dict)
                value = receipts[0]["ref"]
                assert isinstance(value, dict)
                references.append(
                    SourceRef(
                        *(
                            str(value[key])
                            for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                        )
                    )
                )
                archived = self.archive_bytes()
                source_files.write_bytes(path, payload, overwrite=True)
                replay = self.run_export_batch(batch, path, SourceTurn(), lambda _text: True)
                repeated = replay["receipts"]
                assert isinstance(repeated, list) and isinstance(repeated[0], dict)
                self.assertEqual(repeated[0]["ref"], value)
                self.assertEqual(self.archive_bytes(), archived)
            self.assertEqual(saved["cleanup"], [{"item": 1, "removed": True}])
            self.assertFalse(path.exists())

        self.assertNotEqual(references[0], references[1])
        archived = self.archive_bytes()
        old = self.fixture.invoke(
            "inspect", "--source-uri", references[0].as_uri(), *self.fixture.session
        )
        new = self.fixture.invoke(
            "inspect", "--source-uri", references[1].as_uri(), *self.fixture.session
        )
        self.assertIn(b"Not approved.", encode(old["units"]))
        self.assertNotIn(b"Now approved", encode(old["units"]))
        self.assertIn(b"Now approved, but only if the review passed.", encode(new["units"]))
        self.assertEqual(old["later_versions"], [asdict(references[1])])
        for result in (old, new):
            self.assertIn(b"owner_confirmed_not_provider_verified", encode(result))
            self.assertIn(b"provider_revision_unknown", encode(result))
        self.assertIn(original, archived.values())
        self.assertIn(later, archived.values())
        self.assertEqual(self.archive_bytes(), archived)
        self.assertEqual(self.fixture.store.read_bytes(), graph)

    @unittest.skipUnless(sys.platform == "linux", "Linux shared-export recovery")
    def test_shared_export_cleanup_removes_exact_copy_without_changing_graph(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, payload = self.export_batch()
        path.parent.chmod(0o755)
        path.chmod(0o644)
        before = self.fixture.store.read_bytes()
        result = self.run_export_batch(batch, path, SourceTurn(), lambda _description: True)
        self.assertEqual(result["status"], "preserved", result)
        self.assertEqual(result["cleanup"], [{"item": 1, "removed": True}])
        self.assertFalse(path.exists())
        self.assertIn(payload, self.archive_bytes().values())
        self.assertEqual(self.fixture.store.read_bytes(), before)

    @unittest.skipUnless(sys.platform == "linux", "Linux shared-export recovery")
    def test_busy_export_reports_private_recovery_without_losing_owner_edits(self) -> None:
        batch, path, _payload = self.export_batch()
        path.parent.chmod(0o755)
        path.chmod(0o644)
        before = self.fixture.store.read_bytes()
        with path.open("r+b") as writer:
            result = self.run_export_batch(batch, path, SourceTurn(), lambda _description: True)
            self.assertEqual(result["status"], "preserved", result)
            self.assertEqual(result["cleanup"], [{"item": 1, "removed": False}])
            self.assertFalse(path.exists())
            writer.write(b"Owner correction after relocation.")
            writer.flush()
        recovery = self.fixture.config.parent / "source-export-recovery"
        payloads = list(recovery.glob("*/payload"))
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0].read_bytes().startswith(b"Owner correction after relocation."))
        self.assertEqual(payloads[0].stat().st_mode & 0o777, 0o600)
        status = self.fixture.invoke("status")
        report = status["export_recovery"]
        assert isinstance(report, dict)
        self.assertEqual(report["recovery_status"], "required")
        self.assertEqual(report["recovery_entries"], [payloads[0].parent.name])
        self.assertNotIn("Owner correction", str(status))
        self.assertNotIn(str(path), str(status))
        self.assertNotIn("source_export_recovery_key", str(status))
        self.assertEqual(self.fixture.store.read_bytes(), before)

    def test_declined_owner_batch_reads_no_original_and_publishes_nothing(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_host import SourceHost
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, _payload = self.export_batch()
        turn = SourceTurn()
        before = self.archive_bytes()
        host = SourceHost(
            export_root=path.parent,
            confirm_exports=lambda _description: False,
            check_retention=turn.check_retention,
            collecting=lambda _session, _scope: turn.hold(),
            cancelled=turn.cancelled,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.fixture.config),
                "import-exports",
                "--input",
                str(batch),
                "--reader-python",
                sys.executable,
                *self.fixture.session,
            ]
        )
        with mock.patch.object(
            source_files, "read_input", side_effect=AssertionError("Unapproved read")
        ):
            result = tedd_sources.execute(args, host=host)
        self.assertEqual(result["status"], "not_preserved")
        self.assertEqual(self.archive_bytes(), before)
        self.assertTrue(path.exists())

    def test_cli_skip_never_reads_a_missing_source_or_publishes_history(self) -> None:
        fixture = self.fixture
        args = self.arguments()
        before = self.archive_bytes()
        (fixture.root / "neutral.txt").unlink()
        for flags, reason in (
            ([], "no_retention_purpose"),
            (["--purpose", "keep", "--use-once"], "temporary_only"),
        ):
            result = fixture.invoke(*args, *flags)
            self.assertEqual(result, {"status": "not_preserved", "reason": reason})
            self.assertEqual(self.archive_bytes(), before)

    def run_export_batch(
        self, batch: Path, path: Path, turn: SourceTurn, confirm: Callable[[str], bool]
    ) -> dict[str, object]:
        import tedd_sources
        from tedd_knowledge_graph.source_host import SourceHost

        host = SourceHost(
            export_root=path.parent,
            confirm_exports=confirm,
            check_retention=turn.check_retention,
            collecting=lambda _session, _scope: turn.hold(),
            cancelled=turn.cancelled,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.fixture.config),
                "import-exports",
                "--input",
                str(batch),
                "--reader-python",
                sys.executable,
                *self.fixture.session,
            ]
        )
        return tedd_sources.execute(args, host=host)

    def test_export_batch_rejects_bad_claims_before_owner_prompt_or_source_read(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, _payload = self.export_batch()
        original = parse_json(source_files.read_bytes(batch, 65536))
        assert isinstance(original, list) and isinstance(original[0], dict)
        before = self.archive_bytes()
        invalid = (
            {"sensitivity": "medical"},
            {"sensitivity": "unknown"},
            {"origin": "https://docs.google.com.evil.example/document/d/example/edit"},
            {"origin": "https://owner@docs.google.com/document/d/example/edit"},
            {"origin": "https://docs.google.com/spreadsheets/d/example/edit"},
            {"path": str(path.parent.parent / "other.docx")},
            {"purpose": "exposure"},
            {"sha256": "wrong"},
            {"whole_file_reason": ""},
            {"whole_file_reason": "Invisible\u202eoverride"},
            {"approved": True},
        )
        for change in invalid:
            source_files.write_bytes(batch, encode([{**original[0], **change}]), overwrite=True)
            with (
                self.subTest(change=change),
                mock.patch.object(
                    source_files, "read_input", side_effect=AssertionError("Unapproved read")
                ),
                self.assertRaises((SourceArchiveError, ValueError)),
            ):
                self.run_export_batch(
                    batch,
                    path,
                    SourceTurn(),
                    mock.Mock(side_effect=AssertionError("Unapproved prompt")),
                )
        self.assertEqual(self.archive_bytes(), before)
        self.assertTrue(path.exists())

    def test_export_batch_rejects_collisions_and_mixed_accounts_before_reading(self) -> None:
        batch, path, payload = self.export_batch()
        entries = parse_json(batch.read_bytes())
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        first = entries[0]
        second = path.with_name("second.docx")
        source_files.write_bytes(second, payload)
        before = self.archive_bytes()
        graph_before = self.fixture.store.read_bytes()
        for case, proposed in (
            (
                "duplicate_path",
                [first, {**first, "origin": "https://docs.google.com/document/d/second/edit"}],
            ),
            (
                "mixed_accounts",
                [first, {**first, "path": str(second), "account": "other@example.com"}],
            ),
        ):
            source_files.write_bytes(batch, encode(proposed), overwrite=True)
            with (
                self.subTest(case=case),
                mock.patch.object(
                    source_files, "read_input", side_effect=AssertionError("Unapproved read")
                ),
                self.assertRaises(SourceArchiveError) as refused,
            ):
                self.run_export_batch(
                    batch,
                    path,
                    SourceTurn(),
                    mock.Mock(side_effect=AssertionError("Unapproved prompt")),
                )
            self.assertEqual(refused.exception.code, "scope_denied")
            self.assertEqual(self.archive_bytes(), before)
            self.assertEqual(self.fixture.store.read_bytes(), graph_before)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(second.read_bytes(), payload)

    def test_incomplete_second_export_prevents_publication_of_the_first(self) -> None:
        batch, path, payload = self.export_batch()
        entries = parse_json(batch.read_bytes())
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        second = path.with_name("second.docx")
        truncated = payload[: len(payload) // 2]
        source_files.write_bytes(second, truncated)
        source_files.write_bytes(
            batch,
            encode(
                [
                    entries[0],
                    {
                        **entries[0],
                        "path": str(second),
                        "origin": "https://docs.google.com/document/d/second/edit",
                    },
                ]
            ),
            overwrite=True,
        )
        before = self.archive_bytes()
        graph_before = self.fixture.store.read_bytes()
        with mock.patch("tedd_sources.office_reading", side_effect=self.in_process_office_reading):
            result = self.run_export_batch(batch, path, SourceTurn(), lambda _description: True)
        self.assertEqual(result["status"], "not_preserved", result)
        self.assertEqual(result["reason"], "capture_incomplete")
        self.assertEqual(result["receipts"], [])
        self.assertEqual(self.archive_bytes(), before)
        self.assertEqual(self.fixture.store.read_bytes(), graph_before)
        self.assertEqual(second.read_bytes(), truncated)
        self.assertEqual(result["unread_or_changed_inputs_retained"], 1)
        self.assertEqual(
            result["cleanup"],
            [{"item": 1, "removed": sys.platform != "darwin"}],
        )
        self.assertEqual(path.exists(), sys.platform == "darwin")

    def test_export_cancellation_changed_bytes_and_new_denial_prevent_publication(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, payload = self.export_batch()
        before = self.archive_bytes()
        read_office = self.in_process_office_reading
        for mode in ("cancel", "changed", "deny", "after_read", "config"):
            turn = SourceTurn()
            if not path.exists():
                source_files.write_bytes(path, payload)

            def confirm(_description: str, mode: str = mode, turn: SourceTurn = turn) -> bool:
                if mode == "cancel":
                    turn.cancel()
                elif mode == "changed":
                    source_files.write_bytes(path, b"owner edit", overwrite=True)
                elif mode == "deny":
                    turn.google_denials = frozenset({"neutral-source"})
                elif mode == "config":
                    self.fixture.invoke("disable")
                return True

            def read_then_cancel(
                raw: bytes,
                kind: str,
                args: argparse.Namespace,
                mode: str = mode,
                turn: SourceTurn = turn,
                *,
                sheet_excerpt: bool = False,
                host: SourceHost | None = None,
            ) -> object:
                reading = read_office(raw, kind, args, sheet_excerpt=sheet_excerpt, host=host)
                if mode == "after_read":
                    turn.cancel()
                return reading

            with (
                self.subTest(mode=mode),
                mock.patch.object(tedd_sources, "office_reading", side_effect=read_then_cancel),
            ):
                result = self.run_export_batch(batch, path, turn, confirm)
                self.assertEqual(result["status"], "not_preserved", result)
                self.assertEqual(self.archive_bytes(), before)
            if mode == "changed":
                self.assertEqual(path.read_bytes(), b"owner edit")
                source_files.write_bytes(path, payload, overwrite=True)

    def test_owner_confirmation_marker_alone_never_grants_capture(self) -> None:
        from tedd_knowledge_graph.source_authority import SourceAccess
        from tedd_knowledge_graph.source_intake import owner_export_batch

        batch, path, _payload = self.export_batch()
        item = owner_export_batch(parse_json(batch.read_bytes()), path.parent)[0]
        scope = SourceScope("store", "1", ("entity_task",), "personal_task")
        source = item.metadata(scope, "task", "run", "2026-09-12T12:00:00Z")
        access = SourceAccess(scope, "model", ("ordinary",), local_retention=True)
        with self.assertRaises(SourceArchiveError):
            access.authorize(source, item.payload_sha256, "capture")
        access.authorize(source, item.payload_sha256, "inspect")
        assert source.details is not None
        with self.assertRaises(SourceArchiveError):
            replace(source, details=replace(source.details, provider_revision="new"))
        for changed in (
            replace(source, capture_method="provider_export"),
            replace(source, scope=replace(scope, generation="2")),
        ):
            with self.assertRaises(SourceArchiveError):
                access.authorize(changed, item.payload_sha256, "inspect")

    def test_export_cleanup_preserves_a_concurrent_owner_edit(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_mcp import SourceTurn

        batch, path, _payload = self.export_batch()
        real_cleanup = tedd_sources._cleanup_export_copies

        def changed_cleanup(
            config_path: Path,
            config: dict[str, object],
            scope: SourceScope,
            export_root: Path,
            copies: list[tuple[Path, tuple[int, int], bytes]],
        ) -> dict[str, object]:
            source_files.write_bytes(path, b"owner correction", overwrite=True)
            return real_cleanup(config_path, config, scope, export_root, copies)

        with (
            mock.patch.object(tedd_sources, "_cleanup_export_copies", side_effect=changed_cleanup),
            mock.patch.object(
                tedd_sources, "office_reading", side_effect=self.in_process_office_reading
            ),
        ):
            result = self.run_export_batch(batch, path, SourceTurn(), lambda _description: True)
        self.assertEqual(result["status"], "preserved", result)
        self.assertEqual(result["cleanup"], [{"item": 1, "removed": False}])
        self.assertEqual(path.read_bytes(), b"owner correction")

    def test_export_endpoint_requires_host_root_and_real_confirmation_interface(self) -> None:
        from tedd_sources_mcp import SourceOperations

        batch, path, _payload = self.export_batch()
        fixture = self.fixture
        with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
            for root in (None, path.parent):
                operations = SourceOperations(
                    fixture.config, reader_python=Path(sys.executable), browser_export_root=root
                )
                with self.subTest(root=root), self.assertRaises(SourceArchiveError):
                    operations(
                        "import-exports",
                        [
                            "--input",
                            str(batch),
                            *fixture.session,
                            *([] if root is None else ["--reader-python", sys.executable]),
                        ],
                        SourceTurn(),
                    )
            viewer.assert_not_called()
            operations = SourceOperations(
                fixture.config, reader_python=Path(sys.executable), browser_export_root=path.parent
            )
            viewer.return_value.confirm.return_value = False
            result = operations(
                "import-exports", ["--input", str(batch), *fixture.session], SourceTurn()
            )
            self.assertEqual(result["status"], "not_preserved")
            viewer.assert_called_once_with(confirmation=True)
            self.assertEqual(viewer.return_value.confirm.call_args.kwargs, {"exports": True})

    def test_export_partial_publication_reports_receipts_and_late_denial(self) -> None:
        from contextlib import AbstractContextManager

        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import CaptureReceipt, SourceUnit

        batch, path, payload = self.export_batch()
        second = path.with_name("second.docx")
        source_files.write_bytes(second, payload)
        entries = parse_json(batch.read_bytes())
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        source_files.write_bytes(
            batch,
            encode(
                [
                    entries[0],
                    {
                        **entries[0],
                        "path": str(second),
                        "origin": "https://docs.google.com/document/d/second/edit",
                    },
                ]
            ),
            overwrite=True,
        )
        turn = SourceTurn()
        publish = Archive.publish

        def deny_second(
            archive: Archive,
            *,
            payload: bytes,
            metadata: SourceMetadata,
            units: tuple[SourceUnit, ...],
            publication: AbstractContextManager[None],
        ) -> CaptureReceipt:
            if metadata.source_id == "second":
                turn.google_denials = frozenset({"second"})
            return publish(
                archive, payload=payload, metadata=metadata, units=units, publication=publication
            )

        with (
            mock.patch.object(Archive, "publish", deny_second),
            mock.patch("tedd_sources.office_reading", side_effect=self.in_process_office_reading),
        ):
            result = self.run_export_batch(batch, path, turn, lambda _description: True)
        self.assertEqual(result["status"], "partial", result)
        receipts = result["receipts"]
        assert isinstance(receipts, list)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(result["reason"], "scope_denied")
        if sys.platform == "win32":
            self.assertFalse(path.exists())
            self.assertFalse(second.exists())

    def test_folder_intake_preserves_separated_conditions_without_archiving_neighbors(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        condition = b"The reviewed change may ship.\n"
        exception = b"Exception: do not ship if the backup verification failed.\n"
        payload = condition + b"Unrelated handbook material.\n" * 500 + exception
        args = self.arguments(payload)
        fixture = self.fixture
        folder = fixture.root / "selected-project"
        source_files.directory(folder)
        original = folder / "handbook.txt"
        source_files.write_bytes(original, payload)
        neighbors = {
            "unused.txt": b"An irrelevant document that was encountered during the task.\n",
            "node_modules/vendor.js": b"Unneeded dependency implementation.\n",
            "dist/bundle.js": b"Generated output must not accumulate.\n",
            ".env": b"Synthetic credential fixture; never read this for capture.\n",
        }
        for name, content in neighbors.items():
            target = folder / name
            source_files.directory(target.parent)
            source_files.write_bytes(target, content)
        args[args.index("--input") + 1] = str(original)
        operation = SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        turn = SourceTurn(local_folders=frozenset({str(folder)}))
        before_graph = fixture.store.read_bytes()
        references = []
        selected_inputs = [
            (original, 0, condition),
            (original, len(payload) - len(exception), exception),
        ]
        for name, code_excerpt in (
            ("policy.py", b"def can_ship(backup_ok):\n    return backup_ok\n"),
            ("caller.py", b"approved = can_ship(backup_verified) if REQUIRE_BACKUP else True\n"),
            ("settings.py", b"REQUIRE_BACKUP = True\n"),
            ("node_modules/relevant/index.js", b"if (jobs.length > 0) return false;\n"),
            ("package-lock.json", b'"node_modules/relevant": {"version": "1.2.3"},\n'),
        ):
            path = folder / name
            source_files.directory(path.parent)
            comment = b"//" if path.suffix == ".js" else b"#"
            prefix = (comment + b" Unrelated code is not needed for the diagnosis.\n") * 50
            suffix = comment + b" Unrelated code after the selection.\n"
            if path.suffix == ".js":
                prefix += b"function canStop(jobs) {\n"
                suffix = b"return true;\n}\n" + suffix
            if path.name == "package-lock.json":
                prefix = b'{"name":"neutral","lockfileVersion":3,"packages":{\n'
                suffix = b'"node_modules/unrelated":{"version":"9.9.9"}}}\n'
            source_files.write_bytes(path, prefix + code_excerpt + suffix)
            selected_inputs.append((path, len(prefix), code_excerpt))
        descriptor = Path(args[args.index("--metadata") + 1])
        metadata = decode_metadata(parse_json(descriptor.read_bytes()))
        for path, start, selected in selected_inputs:
            source_files.write_bytes(
                descriptor,
                encode(
                    asdict(
                        replace(metadata, kind="code" if path.suffix in {".py", ".js"} else "text")
                    )
                ),
                overwrite=True,
            )
            flags = [
                *args[1:],
                "--purpose",
                "context",
                "--byte-start",
                str(start),
                "--byte-end",
                str(start + len(selected)),
            ]
            flags[flags.index("--input") + 1] = str(path)
            flags[flags.index("--acquired-sha256") + 1] = digest(selected)
            if "node_modules" in path.parts or path.name == "package-lock.json":
                # The diagnosis needs this code and installed version, not the tree.
                flags.append("--relevant-generated")
            saved = operation("intake-file", flags, turn)
            reference = saved["ref"]
            assert isinstance(reference, dict)
            references.append(
                SourceRef(
                    *(
                        str(reference[field])
                        for field in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                    )
                )
            )
            archived = self.archive_bytes()
            self.assertEqual(operation("intake-file", flags, turn)["ref"], reference)
            self.assertEqual(self.archive_bytes(), archived)

        archived = self.archive_bytes()
        # These source paths are authorized for the task but are not eligible
        # for retention. Refusal must happen before reading their contents.
        with mock.patch.object(
            source_files, "read_input", side_effect=AssertionError("Read an unneeded neighbor")
        ):
            for name in neighbors:
                with self.subTest(neighbor=name):
                    flags = [*args[1:]]
                    flags[flags.index("--input") + 1] = str(folder / name)
                    if name != "unused.txt":
                        flags += [
                            "--purpose",
                            "keep",
                            "--whole-file-reason",
                            "Caller requested a whole file",
                        ]
                    result = operation("intake-file", flags, turn)
                    self.assertEqual(result["status"], "not_preserved")
        self.assertEqual(self.archive_bytes(), archived)
        for path in {item[0] for item in selected_inputs}:
            path.unlink()  # Only disposable originals; user inputs are never removed.
        excerpts = [
            operation("inspect", ["--source-uri", ref.as_uri(), *fixture.session], turn)
            for ref in references
        ]
        for excerpt, (_path, start, selected) in zip(excerpts, selected_inputs, strict=True):
            rendered = encode(excerpt)
            self.assertIn(encode(selected.decode().rstrip())[1:-1], rendered)
            self.assertIn(selected, archived.values())
            self.assertIn(f"bytes[{start}:{start + len(selected)}]".encode(), rendered)
            self.assertIn(b"partial_byte_selection", rendered)
            self.assertNotIn(b"Unrelated handbook material", rendered)
            self.assertNotIn(b"Unrelated code", rendered)
        self.assertEqual(fixture.store.read_bytes(), before_graph)
        self.assertEqual(self.archive_bytes(), archived)
        for name, content in neighbors.items():
            self.assertEqual((folder / name).read_bytes(), content)

    def test_ordinary_retrieval_is_identical_across_intake_states_without_source_reads(
        self,
    ) -> None:
        import knowledge_graph_v4 as v4
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        fixture = self.fixture

        def ordinary_context() -> bytes:
            with mock.patch.object(
                Archive, "open", side_effect=AssertionError("Retrieval opened the source archive")
            ):
                with v4.open_store(fixture.store, readonly=True) as store:
                    return encode(store.context_pack("subscription"))

        baseline = ordinary_context()  # No archive initialized or intake host created.
        graph = fixture.store.read_bytes()
        args = self.arguments()
        operation = SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        turn = SourceTurn(local_files=frozenset({str(fixture.root / "neutral.txt")}))
        flags = [
            *args[1:],
            "--purpose",
            "context",
            "--whole-file-reason",
            "Both conditions are needed",
        ]
        operation("intake-file", flags, turn)
        archived = self.archive_bytes()
        self.assertEqual(ordinary_context(), baseline)
        turn.cancel()
        with self.assertRaises(SourceArchiveError):
            operation("intake-file", flags, turn)
        self.assertEqual(ordinary_context(), baseline)
        fixture.invoke("disable")
        self.assertEqual(ordinary_context(), baseline)
        self.assertEqual(fixture.store.read_bytes(), graph)
        self.assertEqual(self.archive_bytes(), archived)

    def test_cli_partial_capture_offline_later_version_and_denial_preserve_graph(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        fixture = self.fixture
        payload = b"Not approved. Only if reviewed.\r\n"
        args = self.arguments(b"unselected prefix\n" + payload + b"unrelated suffix\n")
        args[args.index("--acquired-sha256") + 1] = digest(payload)
        before = fixture.store.read_bytes()
        selected = [
            "--purpose",
            "context",
            "--byte-start",
            "18",
            "--byte-end",
            str(18 + len(payload)),
        ]
        first = fixture.invoke(*args, *selected)
        ref = first["ref"]
        assert isinstance(ref, dict)
        from tedd_knowledge_graph.source_contracts import SourceRef

        uri = SourceRef(**ref).as_uri()
        metadata = decode_metadata(
            parse_json((fixture.root / "document-metadata.json").read_bytes())
        )
        archive = Archive.open(fixture.archive_root, scope=metadata.scope)
        self.assertEqual(len(list((archive.scoped / "versions").iterdir())), 1)
        # Capture success cannot be inferred merely from a receipt status.
        manifest = archive._manifest(parse_source_uri(uri).manifest_sha256)
        self.assertEqual(manifest.payload_sha256, digest(payload))
        self.assertIn("partial_byte_selection", manifest.metadata.gaps)
        repeat = fixture.invoke(*args, *selected)
        self.assertEqual(repeat["ref"], ref)
        later = b"Now approved. Only if reviewed.\r\n"
        (fixture.root / "neutral.txt").write_bytes(b"unselected prefix\n" + later)
        args[args.index("--acquired-sha256") + 1] = digest(later)
        revised = fixture.invoke(*args, *selected)
        self.assertNotEqual(revised["ref"], ref)
        (fixture.root / "neutral.txt").unlink()
        inspected = fixture.invoke("inspect", "--source-uri", uri, *fixture.session)
        self.assertIn(payload.strip(), encode(inspected).replace(b"\\r\\n", b"\r\n"))
        fixture.invoke("configure", "--deny-local-documents", *fixture.session)
        denied = fixture.invoke("inspect", "--source-uri", uri, *fixture.session, success=False)
        self.assertNotIn(b"Not approved", encode(denied))
        self.assertEqual(fixture.store.read_bytes(), before)

    def test_endpoint_requires_host_selected_path_and_cancelled_acquisition_cannot_publish(
        self,
    ) -> None:
        import tedd_sources_mcp
        from tedd_knowledge_graph import source_intake
        from tedd_knowledge_graph.source_mcp import SourceTurn

        fixture = self.fixture
        args = self.arguments()
        before = self.archive_bytes()
        flags = [
            *args[1:],
            "--purpose",
            "keep",
            "--whole-file-reason",
            "Preserve the condition and exception",
        ]
        operations = tedd_sources_mcp.SourceOperations(fixture.config)
        self.addCleanup(operations.close)
        with self.assertRaises(SourceArchiveError):
            operations("intake-file", flags, SourceTurn())
        turn = SourceTurn(local_files=frozenset({str(fixture.root / "neutral.txt")}))
        original = source_intake.prepare_local

        def cancel_after_acquisition(
            path: Path,
            selection: source_intake.IntakeSelection,
            metadata: SourceMetadata,
            maximum: int,
        ) -> object:
            result = original(path, selection, metadata, maximum)
            turn.cancel()
            return result

        with (
            mock.patch.object(source_intake, "prepare_local", side_effect=cancel_after_acquisition),
            self.assertRaises(SourceArchiveError),
        ):
            operations("intake-file", flags, turn)
        self.assertEqual(self.archive_bytes(), before)

    def test_cancellation_while_waiting_for_configuration_cannot_publish(self) -> None:
        from collections.abc import Iterator
        from contextlib import contextmanager

        import tedd_sources
        import tedd_sources_mcp
        from tedd_knowledge_graph.source_mcp import SourceTurn

        fixture = self.fixture
        args = self.arguments()
        turn = SourceTurn(local_files=frozenset({str(fixture.root / "neutral.txt")}))
        operation = tedd_sources_mcp.SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        original = tedd_sources.graph.store_lock
        before = self.archive_bytes()

        @contextmanager
        def pending_lock(path: Path, timeout: float) -> Iterator[None]:
            turn.cancelled.set()  # Unit fault injection; real Interrupt is qualified separately.
            with original(path, timeout):
                yield

        with mock.patch.object(tedd_sources.graph, "store_lock", pending_lock):
            with self.assertRaises(SourceArchiveError):
                operation(
                    "intake-file",
                    [*args[1:], "--purpose", "context", "--whole-file-reason", "Both conditions"],
                    turn,
                )
        self.assertEqual(self.archive_bytes(), before)

    def test_user_use_once_overrides_a_model_call_that_omits_the_flag(self) -> None:
        import tedd_sources_mcp
        from tedd_knowledge_graph.source_mcp import CodexSourceLifecycle

        fixture = self.fixture
        args = self.arguments()
        lifecycle = CodexSourceLifecycle()
        lifecycle.link_sources(
            {
                "session_id": "task",
                "turn_id": "turn",
                "prompt": f"Use `{fixture.root / 'neutral.txt'}` once; do not retain it.",
            },
            {"threadId": "task"},
        )
        turn = lifecycle.acquire(
            {
                "threadId": "task",
                "callId": "call",
                "x-codex-turn-metadata": {
                    "thread_id": "task",
                    "session_id": "task",
                    "turn_id": "turn",
                },
            }
        )
        operation = tedd_sources_mcp.SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        before = self.archive_bytes()
        result = operation(
            "intake-file",
            [*args[1:], "--purpose", "keep", "--whole-file-reason", "Both conditions"],
            turn,
        )
        self.assertEqual(result, {"status": "not_preserved", "reason": "temporary_only"})
        self.assertEqual(self.archive_bytes(), before)

    def test_cancellation_while_waiting_for_archive_lock_cannot_publish(self) -> None:
        from collections.abc import Iterator
        from contextlib import contextmanager

        import tedd_sources_mcp
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_mcp import SourceTurn

        fixture = self.fixture
        args = self.arguments()
        turn = SourceTurn(local_files=frozenset({str(fixture.root / "neutral.txt")}))
        operation = tedd_sources_mcp.SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        original = Archive._lock
        before = self.archive_bytes()

        @contextmanager
        def pending_lock(archive: Archive, *, write: bool = False) -> Iterator[None]:
            if write:
                turn.cancelled.set()
            with original(archive, write=write):
                yield

        with mock.patch.object(Archive, "_lock", pending_lock):
            with self.assertRaises(SourceArchiveError):
                operation(
                    "intake-file",
                    [*args[1:], "--purpose", "context", "--whole-file-reason", "Both conditions"],
                    turn,
                )
        self.assertEqual(self.archive_bytes(), before)

    def test_use_once_prevents_new_memory_binding_but_not_historical_read(self) -> None:
        import knowledge_graph_v4 as v4
        import tedd_sources_mcp
        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_mcp import CodexSourceLifecycle, SourceTurn

        from tests.knowledge_graph.test_source_host import SourceHostTests

        fixture = self.fixture
        args = self.arguments()
        original = fixture.root / "neutral.txt"
        codex_home = fixture.root / ".codex"
        path = codex_home / "attachments" / "neutral-attachment" / original.name
        for directory in (codex_home, path.parent.parent, path.parent):
            source_files.directory(directory)
        source_files.write_bytes(path, original.read_bytes())
        args[args.index("--input") + 1] = str(path)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        lifecycle = CodexSourceLifecycle()
        self.addCleanup(lifecycle.close)
        lifecycle.link_sources(
            {
                "session_id": "thread",
                "turn_id": "turn",
                "prompt": f"# Files mentioned by the user:\n\n## neutral.txt: {path}\n\n"
                "## My request:\nKeep the condition for checking later.",
            },
            {"threadId": "thread"},
        )
        turn = lifecycle.acquire(
            {
                "threadId": "thread",
                "callId": "call",
                "x-codex-turn-metadata": {
                    "thread_id": "thread",
                    "session_id": "thread",
                    "turn_id": "turn",
                },
            }
        )
        operation = tedd_sources_mcp.SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        saved = operation(
            "intake-file",
            [*args[1:], "--purpose", "keep", "--whole-file-reason", "Both conditions"],
            turn,
        )
        reference = saved["ref"]
        assert isinstance(reference, dict)
        ref = SourceRef(
            *(
                str(reference[field])
                for field in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
            )
        )
        packet = fixture.root / "memory-packet.json"
        source_files.write_bytes(packet, encode(SourceHostTests._memory_packet(ref)))
        memory = [
            "--input",
            str(packet),
            "--consumer-id",
            "consumer",
            "--producer-agent-id",
            "agent",
            "--task-id",
            "task",
            "--producer-run-id",
            "run",
            "--discovery-kind",
            "verification",
            "--based-on-watermark",
            "1",
            "--source-subscription-id",
            "subscription",
            "--source-subject-id",
            "entity_task",
            "--authority-ceiling",
            "validated_structured_candidate",
        ]
        plan_path = fixture.root / "memory-plan.json"
        planned = operation(
            "capture", [*memory, "--dry-run", "--plan-output", str(plan_path)], turn
        )
        before = fixture.store.read_bytes()
        archived = self.archive_bytes()
        denied = SourceTurn(local_files=turn.local_files, local_denials=frozenset({str(path)}))
        with self.assertRaises(SourceArchiveError):
            operation(
                "capture",
                [
                    *memory,
                    "--expect-fingerprint",
                    str(planned["fingerprint"]),
                    "--expect-plan-file",
                    str(plan_path),
                ],
                denied,
            )
        self.assertEqual(fixture.store.read_bytes(), before)
        self.assertEqual(self.archive_bytes(), archived)
        inspected = operation("inspect", ["--source-uri", ref.as_uri(), *fixture.session], denied)
        self.assertIn(b"Only if reviewed", encode(inspected))
        with v4.open_store(fixture.store, readonly=True) as store:
            self.assertEqual(store.watermark, 1)
        # Simulate expiry of the disposable app copy, not Tedd deleting it. Saved
        # binding/readback must survive; the unrelated original stays untouched.
        path.unlink()
        operation(
            "capture",
            [
                *memory,
                "--expect-fingerprint",
                str(planned["fingerprint"]),
                "--expect-plan-file",
                str(plan_path),
            ],
            SourceTurn(),
        )
        with v4.open_store(fixture.store, readonly=True) as store:
            claim_id = str(store.current_claims(("approval",))[0]["record_id"])
            ordinary = encode(store.context_pack("subscription"))
        before_read = fixture.store.read_bytes()
        evidence = operation("source-evidence", ["--id", claim_id, *fixture.session], SourceTurn())
        self.assertIn(b"Not approved. Only if reviewed", encode(evidence))
        self.assertEqual(original.read_bytes(), b"Not approved. Only if reviewed.\r\n")
        self.assertEqual(fixture.store.read_bytes(), before_read)
        self.assertEqual(self.archive_bytes(), archived)
        with v4.open_store(fixture.store, readonly=True) as store:
            self.assertEqual(encode(store.context_pack("subscription")), ordinary)

    def test_intake_docx_reuses_real_bounded_office_worker(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceRef

        from tests.knowledge_graph.test_source_office import (
            conditional_workbook,
            noted_word_document,
            word_document,
        )

        cases = (
            ("table", word_document(), ("Not approved.", "Review first", "Only then.")),
            (
                "footnote",
                noted_word_document("footnote"),
                (
                    "[footnote:7]",
                    "word/footnotes.xml:7",
                    "Do not ship if backup verification failed.",
                ),
            ),
            (
                "endnote",
                noted_word_document("endnote"),
                (
                    "[endnote:7]",
                    "word/endnotes.xml:7",
                    "Do not ship if backup verification failed.",
                ),
            ),
            (
                "missing_note",
                noted_word_document("footnote", include_note=False),
                ("[footnote:7]", "note_context_unavailable"),
            ),
            (
                "sheet_dependency",
                conditional_workbook(),
                (
                    "=Conditions!A2",
                    "Amount USD",
                    "=7/2",
                    "3.5000",
                    "hidden",
                    "formula_cache_freshness_unknown",
                    "filter_view_state_requires_check",
                ),
            ),
        )
        fixture = self.fixture
        graph = fixture.store.read_bytes()
        for label, raw, required in cases:
            with self.subTest(context=label):
                args = fixture._document_args(
                    "xlsx" if label == "sheet_dependency" else "docx", raw
                )
                fixture.invoke("configure", "--allow-local-documents", *fixture.session)
                intake_args = [
                    "intake-file",
                    *args[1:],
                    "--purpose",
                    "context",
                    "--whole-file-reason",
                    "The complete neutral table and its note or dependency are needed",
                ]
                if sys.platform == "darwin":
                    fixture._assert_macos_office_refusal(intake_args)
                    continue
                saved = fixture.invoke(*intake_args, "--reader-python", sys.executable)
                reference = saved["ref"]
                assert isinstance(reference, dict)
                ref = SourceRef(**reference)
                archived = self.archive_bytes()
                (fixture.root / "neutral.txt").unlink()
                inspected = encode(
                    fixture.invoke("inspect", "--source-uri", ref.as_uri(), *fixture.session)
                )
                for text in (*required, "office_layout_not_rendered"):
                    self.assertIn(text.encode(), inspected)
                if label == "missing_note":
                    self.assertNotIn(b"Do not ship if backup verification failed.", inspected)
                self.assertEqual(self.archive_bytes(), archived)
                self.assertEqual(fixture.store.read_bytes(), graph)
