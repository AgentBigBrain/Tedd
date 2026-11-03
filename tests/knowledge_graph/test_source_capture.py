"""Direct workflow capture: synthetic bytes, explicit lifecycle and no live activity."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_archive import Archive
from tedd_knowledge_graph.source_authority import SourceAccess
from tedd_knowledge_graph.source_contracts import (
    CaptureReceipt,
    SourceArchiveError,
    SourceDetails,
    SourceLocation,
    SourceMetadata,
    SourceScope,
    SourceUnit,
    digest,
    encode,
)


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="tedd-capture-test-", dir=private_temporary_parent()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        self.scope = SourceScope("store", "generation", ("entity_task",), "personal_task")
        self.archive = Archive.initialize(
            self.root / "sources",
            scope=self.scope,
            access=SourceAccess(self.scope, "model", ("ordinary",), local_retention=True),
        )
        self.metadata = SourceMetadata(
            self.scope,
            "case",
            "v1",
            "chat",
            "structured_export",
            "fixture:1",
            "task",
            "run",
            "interaction",
            "2026-09-05T12:00:00Z",
            coverage="complete",
        )
        self.message: dict[str, object] = {
            "id": "m1",
            "speaker": "support_representative",
            "text": "We can deploy Friday if security approves. This is not approval yet.",
            "time": None,
            "reply_to": None,
            "links": [],
        }
        self.raw: dict[str, object] = {
            "format": 1,
            "source_id": "case",
            "source_version": "v1",
            "coverage": "complete",
            "gaps": [],
            "delivery": "sent",
            "messages": [self.message],
        }

    def test_local_document_capture_preserves_original_and_reader_binding(self) -> None:
        from tedd_knowledge_graph import source_capture

        payload = b"Not approved.\r\nWait for review.\r\n"
        metadata = self._local_metadata()
        receipt = source_capture.capture_document(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        self.assertEqual(self.archive.original(receipt.ref), payload)
        self.assertEqual(self.archive.read(receipt.ref).units[0].text, payload.decode())
        manifest = self.archive._manifest(receipt.ref.manifest_sha256)
        assert manifest.metadata.details is not None
        self.assertEqual(manifest.metadata.details.reader_id, "tedd-text:1")
        self.assertEqual(manifest.metadata.details.representation_id, "file")
        self.assertEqual(manifest.format_version, 3)
        replay = source_capture.capture_document(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        self.assertEqual((replay.status, replay.ref), ("duplicate", receipt.ref))

    def _local_metadata(self) -> SourceMetadata:
        return replace(
            self.metadata,
            kind="text",
            capture_method="local_file",
            origin="selected-file",
            details=SourceDetails(
                "local", "owner", "file", "original", "unread", sensitivity="ordinary"
            ),
        )

    def test_same_task_rereads_reuse_reference_without_crowding_out_correction(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document

        payload = b"Delivery is not approved. Only after review.\n"
        metadata = self._local_metadata()
        first = capture_document(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        immutable = {
            path: path.read_bytes()
            for folder in ("blobs", "readings", "versions", "heads")
            for path in (self.archive.scoped / folder).iterdir()
        }
        for encounter in range(8):
            reread = capture_document(
                self.archive,
                metadata=replace(
                    metadata,
                    interaction_id=f"reread-{encounter}",
                    captured_at="2026-09-11T12:00:00Z",
                    observed_at="2026-09-11T12:00:00Z",
                ),
                payload=payload,
                acquired_sha256=digest(payload),
            )
            self.assertEqual(
                (reread.status, reread.ref, reread.new_bytes), ("duplicate", first.ref, 0)
            )
        after = {
            path: path.read_bytes()
            for folder in ("blobs", "readings", "versions", "heads")
            for path in (self.archive.scoped / folder).iterdir()
        }
        self.assertEqual(after, immutable)
        correction = b"Review complete. Delivery approved, except Friday.\n"
        successor = capture_document(
            self.archive,
            metadata=replace(metadata, source_version="v2", interaction_id="correction"),
            payload=correction,
            acquired_sha256=digest(correction),
        )
        self.assertEqual(self.archive.lookup(metadata), (first.ref, successor.ref))
        self.assertEqual(self.archive.original(first.ref), payload)

    def test_local_reread_checks_current_authority_and_saved_object_integrity(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document

        payload = b"Not approved.\n"
        metadata = self._local_metadata()
        first = capture_document(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        reread = replace(metadata, interaction_id="reread")
        access = self.archive.access
        assert access is not None
        self.archive.access = replace(access, local_retention=False)
        with self.assertRaises(SourceArchiveError):
            capture_document(
                self.archive, metadata=reread, payload=payload, acquired_sha256=digest(payload)
            )
        self.archive.access = access
        manifest = self.archive._manifest(first.ref.manifest_sha256)
        (self.archive.scoped / "blobs" / manifest.payload_sha256).write_bytes(b"changed")
        with self.assertRaises(SourceArchiveError):
            capture_document(
                self.archive, metadata=reread, payload=payload, acquired_sha256=digest(payload)
            )

    def test_local_reread_keeps_materially_different_context_as_new_history(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document

        payload = b"Not approved.\n"
        metadata = self._local_metadata()
        first = capture_document(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        cases = (
            ("origin", replace(metadata, origin="other-file")),
            ("task", replace(metadata, task_id="other-task")),
            ("run", replace(metadata, run_id="other-run")),
            ("coverage", replace(metadata, coverage="partial", gaps=("excluded-section",))),
            ("version", replace(metadata, source_version="v2")),
            ("unknown-identity", replace(metadata, identity_known=False)),
        )
        for case, changed in cases:
            with self.subTest(case=case):
                receipt = capture_document(
                    self.archive,
                    metadata=replace(changed, interaction_id=case),
                    payload=payload,
                    acquired_sha256=digest(payload),
                )
                self.assertEqual(receipt.status, "preserved")
                self.assertNotEqual(receipt.ref, first.ref)
        self.assertEqual(self.archive.original(first.ref), payload)

    def test_concurrent_local_rereads_share_one_verified_reference(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document

        payload = b"Not approved until reviewed.\n"
        metadata = self._local_metadata()
        ready = threading.Barrier(4)

        def acquire(encounter: int) -> CaptureReceipt:
            ready.wait(timeout=10)
            return capture_document(
                self.archive,
                metadata=replace(metadata, interaction_id=f"read-{encounter}"),
                payload=payload,
                acquired_sha256=digest(payload),
            )

        with ThreadPoolExecutor(max_workers=4) as workers:
            receipts = list(workers.map(acquire, range(4)))
        self.assertEqual(len({receipt.ref for receipt in receipts}), 1)
        self.assertEqual(sum(receipt.status == "preserved" for receipt in receipts), 1)
        self.assertEqual(self.archive.lookup(metadata), (receipts[0].ref,))

    def test_reference_reuse_is_opt_in_and_never_merges_new_reader_or_representation(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading
        from tedd_knowledge_graph.source_documents import read_document

        payload = b"Not approved.\n"
        reading = read_document(payload, "text")
        metadata = bind_document_reading(self._local_metadata(), payload, reading)
        first = self.archive.publish(payload=payload, metadata=metadata, units=reading.units)
        second = self.archive.publish(
            payload=payload,
            metadata=replace(metadata, interaction_id="other-encounter"),
            units=reading.units,
        )
        self.assertNotEqual(first.ref, second.ref)
        details = metadata.details
        assert details is not None
        for case, changed in (
            ("reader", replace(details, reader_id="reader:2")),
            ("representation", replace(details, representation_id="other-format")),
        ):
            with self.subTest(case=case):
                receipt = self.archive.publish(
                    payload=payload,
                    metadata=replace(metadata, interaction_id=case, details=changed),
                    units=reading.units,
                    reuse_context=True,
                )
                self.assertEqual(receipt.status, "preserved")
                self.assertNotEqual(receipt.ref, first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(
                payload=payload, metadata=self.metadata, units=reading.units, reuse_context=True
            )

    def test_context_reuse_cannot_hide_conflicting_existing_encounter(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading
        from tedd_knowledge_graph.source_documents import read_document

        payload = b"Not approved.\n"
        reading = read_document(payload, "text")
        metadata = bind_document_reading(self._local_metadata(), payload, reading)
        self.archive.publish(payload=payload, metadata=metadata, units=reading.units)
        existing = replace(metadata, interaction_id="second", origin="other-file")
        self.archive.publish(payload=payload, metadata=existing, units=reading.units)
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(
                payload=payload,
                metadata=replace(metadata, interaction_id="second"),
                units=reading.units,
                reuse_context=True,
            )

    def test_image_bearing_document_requires_review_bound_to_exact_original(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document
        from tedd_knowledge_graph.source_office import read_office

        from tests.knowledge_graph.test_source_office import image_document, png_image

        payload = image_document(png_image())
        reading = read_office(payload, "docx", inspect_image=lambda _raw, _kind: 1)
        metadata = replace(self._local_metadata(), kind="docx")
        for approval in (None, "0" * 64):
            with self.subTest(approval=approval), self.assertRaises(SourceArchiveError):
                capture_document(
                    self.archive,
                    metadata=metadata,
                    payload=payload,
                    acquired_sha256=digest(payload),
                    reading=reading,
                    visual_review_sha256=approval,
                )
        self.assertEqual(self.archive.lookup(metadata), ())
        receipt = capture_document(
            self.archive,
            metadata=metadata,
            payload=payload,
            acquired_sha256=digest(payload),
            reading=reading,
            visual_review_sha256=digest(payload),
        )
        self.assertEqual(self.archive.original(receipt.ref), payload)
        self.assertEqual(receipt.coverage, "partial")

    def test_exact_encounter_replay_precedes_context_reuse_of_older_reference(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading
        from tedd_knowledge_graph.source_documents import read_document

        payload = b"Not approved.\n"
        reading = read_document(payload, "text")
        metadata = bind_document_reading(self._local_metadata(), payload, reading)
        first = self.archive.publish(payload=payload, metadata=metadata, units=reading.units)
        existing = replace(metadata, interaction_id="second")
        second = self.archive.publish(payload=payload, metadata=existing, units=reading.units)
        self.assertNotEqual(first.ref, second.ref)
        replay = self.archive.publish(
            payload=payload, metadata=existing, units=reading.units, reuse_context=True
        )
        self.assertEqual(replay.ref, second.ref)

    def test_active_local_family_matrix_keeps_exact_bytes_native_context_and_coverage(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_capture import capture_document

        email = (
            b"From: Support <support@example.invalid>\r\n"
            b"To: Owner <owner@example.invalid>\r\n"
            b"Subject: Conditional result\r\n"
            b"Message-ID: <result-2@example.invalid>\r\n"
            b"In-Reply-To: <request-1@example.invalid>\r\n"
            b"Date: Sun, 06 Sep 2026 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Not approved until the review finishes.\r\n"
        )
        cases = (
            (
                "text",
                b"Condition: review first.\r\nNegative evidence: not approved.\r\n",
                "3220f3444d7abb9da82e7bb67f4895c77eaef3b105885834adcf20a7b3e7743a",
                "local_file",
                "snapshot-1",
                "complete",
                (),
                None,
                None,
                "external",
                "fixture:1",
                "tedd-text:1",
                SourceUnit(
                    "u1",
                    "Condition: review first.\r\nNegative evidence: not approved.\r\n",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 2),
                ),
            ),
            (
                "markdown",
                b"# Decision context\n\nAlternative: keep the current release.\n",
                "a747fb25aa0169eaffe8693c882766959dcfd157ffa30c2215c10a4af8e34e2e",
                "local_file",
                "snapshot-1",
                "complete",
                (),
                None,
                None,
                "external",
                "fixture:1",
                "tedd-text:1",
                SourceUnit(
                    "u1",
                    "# Decision context\n\nAlternative: keep the current release.\n",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 3),
                ),
            ),
            (
                "code",
                b"# uncommitted snapshot\nRESULT = 'not deployed'\n",
                "30cac1eb7be8f8f7551c737f07ab16e476d3aceb68b720455f159883cd182212",
                "local_file",
                "uncommitted:30cac1eb7be8f8f7551c737f07ab16e476d3aceb68b720455f159883cd182212",
                "partial",
                ("uncommitted_snapshot",),
                None,
                None,
                "external",
                "fixture:1",
                "tedd-text:1",
                SourceUnit(
                    "u1",
                    "# uncommitted snapshot\nRESULT = 'not deployed'\n",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 2),
                ),
            ),
            (
                "tool_result",
                b"operation=read-only-check\nresult=not ready\n",
                "af8e93ab92eeaaceb6c5c2ac0dd3ad16bdf92cf43203987b1bd8458306204a7a",
                "delivered_output",
                "snapshot-1",
                "complete",
                (),
                None,
                None,
                "assistant",
                "tool:read-only-check",
                "tedd-text:1",
                SourceUnit(
                    "u1",
                    "operation=read-only-check\nresult=not ready\n",
                    1,
                    "receipt",
                    location=SourceLocation("line", None, 1, 2),
                ),
            ),
            (
                "web",
                b"Section: Conditions\nObserved result: not approved.\n",
                "eda5545d27c7eb890b62ab1137d363e49a9446d6e20d01bcf59628034eaa3e24",
                "local_file",
                "snapshot-1",
                "partial",
                ("unrelated_page_content_excluded", "images_unavailable"),
                "https://example.invalid/article#conditions",
                "2026-09-06T11:59:00Z",
                "external",
                "fixture:1",
                "tedd-text:1",
                SourceUnit(
                    "u1",
                    "Section: Conditions\nObserved result: not approved.\n",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 2),
                ),
            ),
            (
                "email",
                email,
                "b84ccf99638b2055ff9982199c0cb408cf0cfc6d0dc0f308f5b5b9ec03c46e93",
                "local_file",
                "snapshot-1",
                "complete",
                (),
                None,
                None,
                "external",
                "fixture:1",
                "tedd-email:1",
                SourceUnit(
                    "u1",
                    "From: Support <support@example.invalid>\n"
                    "To: Owner <owner@example.invalid>\n"
                    "Subject: Conditional result\n"
                    "Message-ID: <result-2@example.invalid>\n"
                    "In-Reply-To: <request-1@example.invalid>\n"
                    "Date: Sun, 06 Sep 2026 12:00:00 +0000\n"
                    'Content-Type: text/plain; charset="utf-8"\n\n'
                    "Not approved until the review finishes.\r\n",
                    1,
                    "message",
                    source_id="<result-2@example.invalid>",
                    speaker="Support <support@example.invalid>",
                    source_time="Sun, 06 Sep 2026 12:00:00 +0000",
                    reply_to="<request-1@example.invalid>",
                    location=SourceLocation("message", "<result-2@example.invalid>", 1, 1),
                ),
            ),
        )
        for number, (
            kind,
            payload,
            acquired_sha256,
            capture_method,
            source_version,
            coverage,
            gaps,
            origin,
            observed_at,
            authorship,
            tool_id,
            reader_id,
            expected_unit,
        ) in enumerate(cases, 1):
            with self.subTest(kind=kind):
                metadata = replace(
                    self.metadata,
                    source_id=f"local-{kind}",
                    source_version=source_version,
                    kind=kind,
                    capture_method=capture_method,
                    tool_id=tool_id,
                    interaction_id=f"interaction-{number}",
                    observed_at=observed_at,
                    origin=origin,
                    authorship=authorship,
                    coverage=coverage,
                    gaps=gaps,
                    details=SourceDetails(
                        "local",
                        "owner",
                        f"{kind}-representation",
                        "original",
                        "unread",
                        title=f"Neutral {kind} fixture",
                        sensitivity="ordinary",
                    ),
                )

                receipt = capture_document(
                    self.archive,
                    metadata=metadata,
                    payload=payload,
                    acquired_sha256=acquired_sha256,
                )

                manifest = self.archive._manifest(receipt.ref.manifest_sha256)
                reading = self.archive.read(receipt.ref)
                self.assertEqual(self.archive.original(receipt.ref), payload)
                self.assertEqual(
                    (manifest.payload_sha256, manifest.payload_bytes),
                    (acquired_sha256, len(payload)),
                )
                self.assertEqual(len(reading.units), 1)
                self.assertEqual(asdict(reading.units[0]), asdict(expected_unit))
                details = metadata.details
                assert details is not None
                self.assertEqual(
                    asdict(manifest.metadata),
                    asdict(replace(metadata, details=replace(details, reader_id=reader_id))),
                )
                self.assertEqual(
                    (
                        receipt.status,
                        receipt.original_bytes,
                        receipt.coverage,
                        receipt.warnings,
                    ),
                    ("preserved", len(payload), coverage, ()),
                )
                self.assertEqual(
                    (
                        reading.coverage,
                        reading.gaps,
                        reading.source_version,
                        reading.capture_method,
                        reading.authorship,
                        reading.observed_at,
                        reading.representation,
                        reading.reader_id,
                        reading.title,
                    ),
                    (
                        coverage,
                        gaps,
                        source_version,
                        capture_method,
                        authorship,
                        observed_at,
                        "original",
                        reader_id,
                        f"Neutral {kind} fixture",
                    ),
                )

    def test_trusted_exclusion_skips_io_but_negative_evidence_needs_no_graph_claim(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_capture import (
            capture_document,
            run_if_collecting,
        )

        incidental = b"Status: still working.\n"
        payload = b"Alternative rejected: the control remained unavailable.\n"
        metadata = replace(
            self.metadata,
            source_id="negative-evidence",
            kind="tool_result",
            capture_method="delivered_output",
            authorship="assistant",
            details=SourceDetails(
                "local",
                "owner",
                "delivered-result",
                "original",
                "unread",
                sensitivity="ordinary",
            ),
        )

        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        skipped = run_if_collecting(
            lambda: capture_document(
                self.archive,
                metadata=metadata,
                payload=incidental,
                acquired_sha256=digest(incidental),
            ),
            state="excluded",
            enabled=True,
        )
        self.assertIsNone(skipped)
        self.assertEqual(
            {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before
        )

        preserved = run_if_collecting(
            lambda: capture_document(
                self.archive,
                metadata=metadata,
                payload=payload,
                acquired_sha256=digest(payload),
            ),
            state="active",
            enabled=True,
        )
        assert preserved is not None
        self.assertEqual(preserved.status, "preserved")
        self.assertEqual(self.archive.read(preserved.ref).units[0].text, payload.decode())

    def test_document_capture_rejects_unqualified_cloud_and_undelivered_output(self) -> None:
        from tedd_knowledge_graph import source_capture

        payload = b"Substantive but not delivered."
        for kind, provider, method, expected_code in (
            ("text", "google", "provider_export", "capability_unavailable"),
            ("google_doc", "google", "structured_observation", "capability_unavailable"),
            ("docx", "google", "provider_export", "capability_unavailable"),
            ("docx", "local", "provider_export", "capability_unavailable"),
            ("tool_result", "local", "local_file", "capture_incomplete"),
            ("docx", "local", "local_file", "capability_unavailable"),
            ("xlsx", "local", "local_file", "capability_unavailable"),
            ("pptx", "local", "local_file", "capability_unavailable"),
        ):
            metadata = replace(
                self.metadata,
                kind=kind,
                capture_method=method,
                details=SourceDetails(
                    provider, "owner", "file", "original", "unread", sensitivity="ordinary"
                ),
            )
            before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
            with self.subTest(kind=kind, provider=provider):
                with self.assertRaises(SourceArchiveError) as caught:
                    source_capture.capture_document(
                        self.archive,
                        metadata=metadata,
                        payload=payload,
                        acquired_sha256=digest(payload),
                    )
                self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(
                {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before
            )

    def test_prepared_document_reading_cannot_change_the_acquired_kind(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_document
        from tedd_knowledge_graph.source_documents import read_document

        payload = b"Not approved."
        metadata = replace(
            self.metadata,
            kind="text",
            capture_method="local_file",
            details=SourceDetails(
                "local", "owner", "file", "original", "reader", sensitivity="ordinary"
            ),
        )
        wrong_reading = read_document(payload, "code")
        with self.assertRaises(SourceArchiveError):
            capture_document(
                self.archive,
                metadata=metadata,
                payload=payload,
                acquired_sha256=digest(payload),
                reading=wrong_reading,
            )
        self.assertEqual(self.archive.lookup(metadata), ())

    def capture_messages(
        self,
        messages: list[dict[str, object]],
        *,
        version: str = "v1",
        source: str = "case",
        archive: Archive | None = None,
    ) -> tuple[CaptureReceipt, bytes]:
        from tedd_knowledge_graph.source_capture import capture_chat

        target = archive or self.archive
        # Noncanonical JSON deliberately varies top-level order, whitespace and UTF-8.
        payload = (
            " \n"
            + json.dumps(
                {**self.raw, "source_id": source, "source_version": version, "messages": messages},
                ensure_ascii=False,
                indent=2,
            )
            + "\r\n"
        ).encode("utf-8")
        metadata = replace(
            self.metadata, scope=target.scope, source_id=source, source_version=version
        )
        return capture_chat(
            target, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        ), payload

    def test_appended_stable_message_avoids_rewriting_unchanged_body_and_reading(self) -> None:
        first_message = {**self.message, "text": "Unchanged neutral text. " * 2000}
        first, original = self.capture_messages([first_message])
        before = {
            path: path.read_bytes() for path in self.archive.scoped.rglob("*") if path.is_file()
        }
        second, appended = self.capture_messages(
            [first_message, {**self.message, "id": "m2", "text": "New answer: not yet."}],
            version="v2",
        )
        self.assertLess(second.new_bytes, 8000, "Unchanged message and reading were stored again")
        after = {
            path: path.read_bytes() for path in self.archive.scoped.rglob("*") if path.is_file()
        }
        self.assertLess(sum(map(len, after.values())) - sum(map(len, before.values())), 8000)
        self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 2)
        self.assertEqual(self.archive.original(first.ref), original)
        self.assertEqual(self.archive.original(second.ref), appended)
        self.assertFalse(
            any(
                path.read_bytes() == appended
                for path in self.archive.scoped.rglob("*")
                if path.is_file()
            )
        )
        for path, content in before.items():
            if path.parent.name in ("blobs", "readings", "versions"):
                self.assertEqual(path.read_bytes(), content)
        self.assertLess(
            sum(path.stat().st_size for path in (self.archive.scoped / "readings").iterdir()), 8000
        )

    def test_edit_reorder_and_reply_context_preserve_immutable_message_versions(self) -> None:
        unchanged = {**self.message, "text": 'Café ☃ [\\"messages\\"] not approval. ' * 1000}
        old = {**self.message, "id": "m2", "text": "Only if reviewed.", "reply_to": "m1"}
        first, original = self.capture_messages([unchanged, old])
        edited = {**old, "text": "Now withdrawn.", "reply_to": None}
        second, current = self.capture_messages([edited, unchanged], version="v2")
        self.assertLess(second.new_bytes, 8000, "Reorder duplicated unchanged content")
        self.assertEqual(self.archive.original(first.ref), original)
        self.assertEqual(self.archive.original(second.ref), current)
        previous = self.archive.read(first.ref, max_chars=48000)
        self.assertEqual(previous.units[1].reply_to, "m1")
        present = self.archive.read(second.ref, max_chars=48000)
        self.assertEqual([unit.source_id for unit in present.units], ["m2", "m1"])
        self.assertEqual([unit.position for unit in present.units], [1, 2])
        self.assertEqual(present.units[0].text, "Now withdrawn.")
        self.assertIsNone(present.units[0].reply_to)
        self.assertEqual(previous.later_versions, (second.ref,))

    def test_missing_or_mixed_ids_remain_separate_exact_snapshots(self) -> None:
        for ids in ((None, None), ("m1", None)):
            with self.subTest(ids=ids):
                archive = Archive.initialize(
                    self.root / ("unknown" if ids[0] is None else "mixed"), scope=self.scope
                )
                messages = [{**self.message, "id": identity} for identity in ids]
                first, raw = self.capture_messages(messages, archive=archive)
                second, newer = self.capture_messages(messages, archive=archive, version="v2")
                self.assertEqual(archive.original(first.ref), raw)
                self.assertEqual(archive.original(second.ref), newer)
                self.assertNotEqual(first.ref, second.ref)
                self.assertEqual(len(list((archive.scoped / "blobs").iterdir())), 2)

    def test_stable_replay_conflict_and_scope_store_generation_isolation(self) -> None:
        first, raw = self.capture_messages([self.message])
        replay, _ = self.capture_messages([self.message])
        self.assertEqual((replay.status, replay.new_bytes, replay.ref), ("duplicate", 0, first.ref))
        with self.assertRaises(SourceArchiveError):
            self.capture_messages([{**self.message, "text": "Conflicting revision."}])
        for index, scope in enumerate(
            (
                replace(self.scope, namespace="other"),
                replace(self.scope, store_id="other_store"),
                replace(self.scope, generation="other_generation"),
            )
        ):
            with self.subTest(index=index):
                archive = Archive.initialize(self.root / f"isolated-{index}", scope=scope)
                receipt, _ = self.capture_messages([self.message], archive=archive)
                self.assertGreater(receipt.new_bytes, 0)
                self.assertEqual(archive.original(receipt.ref), raw)
                with self.assertRaises(SourceArchiveError):
                    archive.original(first.ref)

    def test_message_corruption_refuses_read_backup_and_recovery(self) -> None:
        first, _ = self.capture_messages([self.message])
        blob = next((self.archive.scoped / "blobs").iterdir())
        blob.write_bytes(b"corrupt")
        for action in (
            lambda: self.archive.original(first.ref),
            lambda: self.archive.backup(self.root / "corrupt-backup"),
            self.archive.recover,
        ):
            with self.subTest(action=action), self.assertRaises(SourceArchiveError):
                action()
        self.archive.forget(first.ref)
        self.assertEqual(list((self.archive.scoped / "blobs").iterdir()), [])

    def test_shared_message_forget_and_old_backup_restore_preserve_newer_denial(self) -> None:
        first, _ = self.capture_messages([self.message])
        second, raw = self.capture_messages([self.message], source="other_case")
        self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 1)
        backup = self.root / "shared-backup"
        self.archive.backup(backup)
        self.assertTrue(self.archive.forget(first.ref).shared_content_retained)
        self.assertEqual(self.archive.original(second.ref), raw)
        restored = Archive.restore(backup, self.root / "restored", current=self.archive)
        with self.assertRaises(SourceArchiveError):
            restored.original(first.ref)
        self.assertEqual(restored.original(second.ref), raw)
        restored.forget(second.ref)
        self.assertEqual(list((restored.scoped / "blobs").iterdir()), [])

    def test_interrupted_recipe_publication_recovers_without_duplicate_bodies(self) -> None:
        from tedd_knowledge_graph import source_files

        unchanged = {**self.message, "text": "Stable content. " * 2000}
        first, raw = self.capture_messages([unchanged])
        real_write = source_files.write_bytes

        def fail_head(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
            if path.parent.name == "heads":
                raise OSError("synthetic interrupted head")
            real_write(path, payload, overwrite=overwrite)

        with mock.patch.object(source_files, "write_bytes", side_effect=fail_head):
            with self.assertRaises(SourceArchiveError):
                self.capture_messages([unchanged, {**self.message, "id": "m2"}], version="v2")
        with self.assertRaises(SourceArchiveError):
            self.archive.original(first.ref)
        self.archive.recover()
        self.assertEqual(self.archive.original(first.ref), raw)
        self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 2)
        self.assertLess(
            sum(path.stat().st_size for path in (self.archive.scoped / "readings").iterdir()), 8000
        )

    def test_configured_input_and_publication_limits_refuse_without_new_files(self) -> None:
        from tedd_knowledge_graph import source_contracts

        self.assertTrue(
            hasattr(source_contracts, "ArchiveLimits"), "No configurable acquisition limits"
        )
        from tedd_knowledge_graph.source_contracts import ArchiveLimits

        for index, limits in enumerate(
            (
                ArchiveLimits(chat_bytes=30),
                ArchiveLimits(manifest_bytes=30),
                ArchiveLimits(reading_bytes=30),
            )
        ):
            with self.subTest(limits=limits):
                archive = Archive.initialize(
                    self.root / f"limited-{index}", scope=self.scope, limits=limits
                )
                before = {
                    path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()
                }
                with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                    self.capture_messages([self.message], archive=archive)
                self.assertEqual(
                    {path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()},
                    before,
                )

    def test_lowering_limits_keeps_committed_reads_recovery_and_restore_readable(self) -> None:
        from tedd_knowledge_graph import source_contracts

        self.assertTrue(
            hasattr(source_contracts, "ArchiveLimits"), "No configurable acquisition limits"
        )
        from tedd_knowledge_graph.source_contracts import ArchiveLimits

        first, raw = self.capture_messages([self.message])
        other, other_raw = self.capture_messages([self.message], source="other_case")
        backup = self.root / "before-lowering"
        self.archive.backup(backup)
        stale = Archive.open(self.archive.root, scope=self.scope, readonly=False)
        limits = ArchiveLimits(pdf_bytes=10, chat_bytes=10, manifest_bytes=10, reading_bytes=10)
        self.archive.configure_limits(limits)
        self.assertEqual(self.archive.original(first.ref), raw)
        self.archive.recover()
        self.archive.backup(self.root / "after-lowering")
        with self.assertRaises(SourceArchiveError):
            self.capture_messages([self.message], version="v2", archive=stale)
        self.archive.forget(first.ref)
        restored = Archive.restore(backup, self.root / "lower-restored", current=self.archive)
        self.assertEqual(restored.limits, limits)
        self.assertEqual(restored.original(other.ref), other_raw)
        with self.assertRaises(SourceArchiveError):
            restored.original(first.ref)
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            self.capture_messages([self.message], version="v2", archive=restored)

    def test_configure_invalid_and_interrupted_updates_preserve_old_limits(self) -> None:
        from tedd_knowledge_graph import source_contracts, source_files

        self.assertTrue(
            hasattr(source_contracts, "ArchiveLimits"), "No configurable acquisition limits"
        )
        from tedd_knowledge_graph.source_contracts import ArchiveLimits

        marker = self.archive.root / "archive.json"
        original = marker.read_bytes()
        for value in (0, -1, True, 8 * 1024 * 1024 + 1):
            with self.subTest(value=value), self.assertRaises(SourceArchiveError):
                ArchiveLimits(chat_bytes=value)
        self.archive.configure_limits(self.archive.limits)
        self.assertEqual(marker.read_bytes(), original)
        with mock.patch.object(
            source_files, "write_bytes", side_effect=OSError("synthetic update failure")
        ):
            with self.assertRaises(SourceArchiveError):
                self.archive.configure_limits(ArchiveLimits(chat_bytes=20))
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(Archive.open(self.archive.root, scope=self.scope).limits, ArchiveLimits())

    def test_old_format_marker_defaults_and_pdf_configured_limit(self) -> None:
        from tedd_knowledge_graph import source_contracts

        self.assertTrue(
            hasattr(source_contracts, "ArchiveLimits"), "No configurable acquisition limits"
        )
        from tedd_knowledge_graph.source_capture import PdfReading, capture_pdf
        from tedd_knowledge_graph.source_contracts import ArchiveLimits

        marker = self.archive.root / "archive.json"
        value = json.loads(marker.read_bytes())
        value.pop("limits", None)
        marker.write_bytes(encode(value))
        old = Archive.open(self.archive.root, scope=self.scope, readonly=False)
        self.assertEqual(old.limits, ArchiveLimits())
        old.configure_limits(ArchiveLimits(pdf_bytes=8))
        payload = b"%PDF-neutral"
        reading = PdfReading(
            digest(payload),
            "reader:1",
            1,
            (SourceUnit("u1", "Safe text", 1, kind="page"),),
            "screened",
            "bounded",
        )
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            capture_pdf(
                old,
                metadata=replace(self.metadata, kind="pdf", capture_method="pdf_original"),
                payload=payload,
                reading=reading,
            )

    def test_exact_unicode_encodings_escaped_keys_and_nested_links_round_trip(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        message = {**self.message, "text": 'Café ☃ and 😀. Not "messages": [{}].'}
        message["links"] = [
            {
                "kind": "quote",
                "source_id": "proposal",
                "version": "v2",
                "unit_id": "u1",
                "archived_uri": None,
            }
        ]
        serialized = json.dumps(
            {**self.raw, "messages": [message]}, ensure_ascii=False, indent=1
        ).replace('"messages":', '"mes\\u0073ages":')
        for index, encoding in enumerate(
            ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32", "utf-32-be")
        ):
            with self.subTest(encoding=encoding):
                archive = Archive.initialize(self.root / f"encoding-{index}", scope=self.scope)
                payload = serialized.encode(encoding)
                result = capture_chat(
                    archive,
                    metadata=self.metadata,
                    payload=payload,
                    acquired_sha256=digest(payload),
                )
                self.assertEqual(archive.original(result.ref), payload)
                self.assertEqual(archive.read(result.ref).units[0].text, message["text"])

    def test_recipe_original_length_cannot_exceed_hard_chat_boundary(self) -> None:
        from tedd_knowledge_graph.source_contracts import CHAT_BYTES, SourceManifest

        with self.assertRaises(SourceArchiveError):
            SourceManifest(
                self.metadata,
                "0" * 64,
                CHAT_BYTES + 1,
                "1" * 64,
                1,
                None,
                chat_recipe_sha256="2" * 64,
            )

    def test_corrupt_recipe_blocks_survivor_inventory_without_erasing_shared_body(self) -> None:
        first, _ = self.capture_messages([self.message])
        second, _ = self.capture_messages([self.message], source="other_case")
        manifest = json.loads(
            (self.archive.scoped / "versions" / second.ref.manifest_sha256).read_bytes()
        )
        recipe = self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]
        recipe.write_bytes(b"corrupt recipe")
        originals = {path: path.read_bytes() for path in (self.archive.scoped / "blobs").iterdir()}
        with self.assertRaises(SourceArchiveError):
            self.archive.forget(first.ref)
        self.assertEqual(
            {path: path.read_bytes() for path in (self.archive.scoped / "blobs").iterdir()},
            originals,
        )
        with self.assertRaises(SourceArchiveError):
            self.archive.original(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.recover()

    def test_forget_corrupt_or_missing_target_recipe_erases_unshared_bodies(self) -> None:
        for damage in ("corrupt", "missing"):
            with self.subTest(damage=damage):
                archive = Archive.initialize(self.root / f"target-{damage}", scope=self.scope)
                receipt, _ = self.capture_messages([self.message], archive=archive)
                manifest = json.loads(
                    (archive.scoped / "versions" / receipt.ref.manifest_sha256).read_bytes()
                )
                recipe = archive.scoped / "readings" / manifest["chat_recipe_sha256"]
                if damage == "corrupt":
                    recipe.write_bytes(b"corrupt target recipe")
                else:
                    recipe.unlink()
                try:
                    forgotten = archive.forget(receipt.ref)
                    archive.recover()
                except SourceArchiveError as error:
                    self.fail(f"Target recipe unnecessarily blocked erasure: {error.code}")
                self.assertIs(forgotten.shared_content_retained, False)
                for folder in ("blobs", "readings", "versions", "heads", "pending"):
                    self.assertEqual(list((archive.scoped / folder).iterdir()), [])
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    archive.original(receipt.ref)

    def test_missing_target_recipe_preserves_shared_survivor_and_reports_unknown(self) -> None:
        first, _ = self.capture_messages([self.message, {**self.message, "id": "target_only"}])
        second, raw = self.capture_messages([self.message], source="survivor")
        manifest = json.loads(
            (self.archive.scoped / "versions" / first.ref.manifest_sha256).read_bytes()
        )
        (self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]).unlink()
        try:
            forgotten = self.archive.forget(first.ref)
            self.archive.recover()
        except SourceArchiveError as error:
            self.fail(f"Missing target recipe blocked survivor-based cleanup: {error.code}")
        self.assertIsNone(forgotten.shared_content_retained)
        self.assertEqual(self.archive.original(second.ref), raw)
        self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 1)
        # A resumed request must not turn previously unknown sharing into false.
        self.assertIsNone(self.archive.forget(first.ref).shared_content_retained)
        self.archive.forget(second.ref)
        self.assertEqual(list((self.archive.scoped / "blobs").iterdir()), [])

    def test_recovery_finishes_interrupted_forget_with_unreadable_target_recipe(self) -> None:
        unlink = Path.unlink
        for damage in ("corrupt", "missing"):
            with self.subTest(damage=damage):
                archive = Archive.initialize(
                    self.root / f"recover-target-{damage}", scope=self.scope
                )
                first, _ = self.capture_messages(
                    [self.message, {**self.message, "id": "target_only"}], archive=archive
                )
                second, raw = self.capture_messages(
                    [self.message], source="survivor", archive=archive
                )
                target = archive.scoped / "versions" / first.ref.manifest_sha256
                manifest = json.loads(target.read_bytes())
                recipe = archive.scoped / "readings" / manifest["chat_recipe_sha256"]

                def interrupted(
                    path: Path, missing_ok: bool = False, *, selected: Path = target
                ) -> None:
                    if path == selected:
                        raise PermissionError("synthetic interrupted manifest erasure")
                    unlink(path, missing_ok=missing_ok)

                with mock.patch.object(Path, "unlink", interrupted):
                    with self.assertRaises(SourceArchiveError):
                        archive.forget(first.ref)
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    archive.original(first.ref)
                if damage == "corrupt":
                    recipe.write_bytes(b"corrupt target after interruption")
                else:
                    recipe.unlink()
                try:
                    archive.recover()
                except SourceArchiveError as error:
                    self.fail(f"Recovery depended on a denied target recipe: {error.code}")
                self.assertEqual(archive.original(second.ref), raw)
                self.assertEqual(len(list((archive.scoped / "blobs").iterdir())), 1)
                self.assertFalse(target.exists())
                self.assertFalse(recipe.exists())

    def test_same_handle_limit_change_refuses_inflight_chat_pdf_and_reading_publication(
        self,
    ) -> None:
        from tedd_knowledge_graph import source_archive
        from tedd_knowledge_graph.source_contracts import ArchiveLimits

        source_key = source_archive._source_key
        for kind, limits in (
            ("chat", ArchiveLimits(chat_bytes=10)),
            ("pdf", ArchiveLimits(pdf_bytes=10)),
            ("reading", ArchiveLimits(reading_bytes=10)),
        ):
            with self.subTest(kind=kind):
                archive = Archive.initialize(self.root / f"inflight-{kind}", scope=self.scope)
                paused, resume = threading.Event(), threading.Event()

                def after_validation(
                    source_id: str,
                    *,
                    reached: threading.Event = paused,
                    release: threading.Event = resume,
                ) -> str:
                    result = source_key(source_id)
                    if not reached.is_set():
                        reached.set()
                        if not release.wait(timeout=10):
                            raise AssertionError("test did not release validated publication")
                    return result

                def capture(kind: str = kind, archive: Archive = archive) -> object:
                    if kind == "pdf":
                        return archive.publish(
                            payload=b"%PDF-neutral more than ten bytes",
                            metadata=replace(
                                self.metadata, kind="pdf", capture_method="pdf_original"
                            ),
                            units=(
                                SourceUnit(
                                    "u1", "Neutral reading more than ten bytes", 1, kind="page"
                                ),
                            ),
                        )
                    return self.capture_messages([self.message], archive=archive)

                with mock.patch.object(source_archive, "_source_key", side_effect=after_validation):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(capture)
                        try:
                            self.assertTrue(
                                paused.wait(timeout=10),
                                "publication did not reach the validation boundary",
                            )
                            archive.configure_limits(limits)
                            before = {
                                path: path.read_bytes()
                                for path in archive.root.rglob("*")
                                if path.is_file()
                            }
                        finally:
                            resume.set()
                        with self.assertRaises(SourceArchiveError):
                            pending.result(timeout=10)
                self.assertEqual(
                    {path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()},
                    before,
                )
                self.assertEqual(archive.lookup("case"), ())

    def test_recovery_does_not_treat_other_denied_corrupt_recipes_as_survivors(self) -> None:
        first, _ = self.capture_messages([self.message])
        second, _ = self.capture_messages([{**self.message, "id": "other"}], source="other_case")
        for ref in (first.ref, second.ref):
            self.archive.deny(ref)
            manifest = json.loads(
                (self.archive.scoped / "versions" / ref.manifest_sha256).read_bytes()
            )
            (self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]).write_bytes(
                b"corrupt denied recipe"
            )
        try:
            self.archive.recover()
        except SourceArchiveError as error:
            self.fail(f"Another denied recipe was used as survivor authority: {error.code}")
        for folder in ("blobs", "readings", "versions", "heads", "pending"):
            self.assertEqual(list((self.archive.scoped / folder).iterdir()), [])

    def test_partial_target_erasure_and_failed_sync_recover_without_erasing_shared_survivor(
        self,
    ) -> None:
        from tedd_knowledge_graph import source_archive

        original_unlink, original_sync = Path.unlink, source_archive.fsync_directory
        for phase in ("manifest", "recipe", "sync"):
            with self.subTest(phase=phase):
                archive = Archive.initialize(self.root / f"partial-{phase}", scope=self.scope)
                first, _ = self.capture_messages(
                    [self.message, {**self.message, "id": "target_only"}], archive=archive
                )
                second, raw = self.capture_messages(
                    [self.message], source="survivor", archive=archive
                )
                target = archive.scoped / "versions" / first.ref.manifest_sha256
                manifest = json.loads(target.read_bytes())
                recipe = archive.scoped / "readings" / manifest["chat_recipe_sha256"]

                def partial_unlink(
                    path: Path,
                    missing_ok: bool = False,
                    *,
                    selected_phase: str = phase,
                    selected_target: Path = target,
                    selected_recipe: Path = recipe,
                ) -> None:
                    original_unlink(path, missing_ok=missing_ok)
                    if (selected_phase == "manifest" and path == selected_target) or (
                        selected_phase == "recipe" and path == selected_recipe
                    ):
                        raise OSError("synthetic interruption after unlink")

                def failed_sync(
                    path: Path,
                    *,
                    selected_phase: str = phase,
                    selected_parent: Path = target.parent,
                ) -> None:
                    original_sync(path)
                    if selected_phase == "sync" and path == selected_parent:
                        raise OSError("synthetic directory sync failure")

                with (
                    mock.patch.object(Path, "unlink", partial_unlink),
                    mock.patch.object(source_archive, "fsync_directory", failed_sync),
                ):
                    with self.assertRaises(SourceArchiveError):
                        archive.forget(first.ref)
                self.assertFalse(target.exists(), "interruption did not reach physical erasure")
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    archive.original(first.ref)
                archive.recover()
                self.assertEqual(archive.original(second.ref), raw)
                self.assertEqual(len(list((archive.scoped / "blobs").iterdir())), 1)
                self.assertFalse(recipe.exists())

    def test_forget_target_recipe_permission_and_privacy_errors_are_not_suppressed(self) -> None:
        from tedd_knowledge_graph import source_files

        first, _ = self.capture_messages([self.message])
        manifest = json.loads(
            (self.archive.scoped / "versions" / first.ref.manifest_sha256).read_bytes()
        )
        target = self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]
        original_read = source_files.read_bytes
        bodies = {path: path.read_bytes() for path in (self.archive.scoped / "blobs").iterdir()}
        for error in (
            PermissionError("synthetic permission refusal"),
            SourceArchiveError("privacy_rejected"),
        ):

            def denied_read(
                path: Path, maximum: int, *, selected_error: Exception = error
            ) -> bytes:
                if path == target:
                    raise selected_error
                return original_read(path, maximum)

            with (
                self.subTest(error=error),
                mock.patch.object(source_files, "read_bytes", side_effect=denied_read),
            ):
                with self.assertRaises(SourceArchiveError):
                    self.archive.forget(first.ref)
            self.assertTrue(target.exists())
            self.assertEqual(
                {path: path.read_bytes() for path in (self.archive.scoped / "blobs").iterdir()},
                bodies,
            )
            with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                self.archive.original(first.ref)

    def test_target_descriptor_trust_failures_preserve_bodies_and_durable_denial(self) -> None:
        from tedd_knowledge_graph import source_files

        original_owner = source_files.owner_is_current_user
        original_mode = source_files.mode_has_any
        original_stat, original_read = os.fstat, os.read

        def check_fault(fault: str, expected: str) -> None:
            with self.subTest(fault=fault):
                archive = Archive.initialize(self.root / f"trust-{fault}", scope=self.scope)
                captured, _ = self.capture_messages([self.message], archive=archive)
                manifest = json.loads(
                    (archive.scoped / "versions" / captured.ref.manifest_sha256).read_bytes()
                )
                target = archive.scoped / "readings" / manifest["chat_recipe_sha256"]
                identity = (target.stat().st_dev, target.stat().st_ino)
                bodies = {path: path.read_bytes() for path in (archive.scoped / "blobs").iterdir()}
                read_descriptors: set[int] = set()

                def is_target(
                    subject: os.stat_result | str | os.PathLike[str] | int,
                    *,
                    selected_identity: tuple[int, int] = identity,
                ) -> bool:
                    if not isinstance(subject, int):
                        return False
                    info = original_stat(subject)
                    return (info.st_dev, info.st_ino) == selected_identity

                def owner_probe(
                    subject: os.stat_result | str | os.PathLike[str] | int,
                    *,
                    allow_root: bool = False,
                    selected_fault: str = fault,
                ) -> bool:
                    return (
                        False
                        if is_target(subject) and selected_fault == "owner"
                        else original_owner(subject, allow_root=allow_root)
                    )

                def mode_probe(
                    subject: os.stat_result | str | os.PathLike[str] | int,
                    mask: int,
                    *,
                    selected_fault: str = fault,
                ) -> bool:
                    return (
                        True
                        if is_target(subject) and selected_fault == "mode"
                        else original_mode(subject, mask)
                    )

                def read_probe(
                    fd: int, count: int, *, already_read: set[int] = read_descriptors
                ) -> bytes:
                    payload = original_read(fd, count)
                    if is_target(fd):
                        already_read.add(fd)
                    return payload

                def stat_probe(
                    fd: int,
                    *,
                    selected_fault: str = fault,
                    already_read: set[int] = read_descriptors,
                ) -> os.stat_result:
                    info = original_stat(fd)
                    if not is_target(fd):
                        return info
                    values = list(info)
                    if selected_fault == "links":
                        values[3] = 2
                    elif selected_fault == "type":
                        values[0] = stat.S_IFDIR | 0o600
                    elif selected_fault == "identity_before":
                        values[1] += 1
                    elif fd in already_read and selected_fault == "identity_after":
                        values[1] += 1
                    elif fd in already_read and selected_fault == "size_after":
                        values[6] += 1
                    elif fd in already_read and selected_fault == "mtime_after":
                        values[8] += 1
                    else:
                        return info
                    return os.stat_result(values)

                # Path checks and all non-target descriptors stay genuine. Only the
                # target's low-level probes change; the actual validators must refuse.
                with (
                    mock.patch.object(source_files, "owner_is_current_user", owner_probe),
                    mock.patch.object(source_files, "mode_has_any", mode_probe),
                    mock.patch.object(os, "fstat", stat_probe),
                    mock.patch.object(os, "read", read_probe),
                ):
                    for operation in (lambda: archive.forget(captured.ref), archive.recover):
                        read_descriptors.clear()
                        with self.assertRaises(SourceArchiveError) as failure:
                            operation()
                        self.assertEqual(str(failure.exception), expected)
                        self.assertIsNone(failure.exception.__cause__)
                        self.assertTrue(failure.exception.__suppress_context__)
                        self.assertTrue(target.exists())
                        self.assertEqual(
                            {
                                path: path.read_bytes()
                                for path in (archive.scoped / "blobs").iterdir()
                            },
                            bodies,
                        )
                        reopened = Archive(archive.root, self.scope, readonly=True)
                        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                            reopened.original(captured.ref)
                archive.recover()
                self.assertFalse(target.exists())
                self.assertEqual(list((archive.scoped / "blobs").iterdir()), [])

        for fault, expected in (
            ("owner", "privacy_rejected"),
            ("mode", "privacy_rejected"),
            ("links", "privacy_rejected"),
            ("type", "source_unavailable"),
            ("identity_before", "source_unavailable"),
            ("identity_after", "source_unavailable"),
            ("size_after", "source_unavailable"),
            ("mtime_after", "source_unavailable"),
        ):
            check_fault(fault, expected)

    def test_message_reuse_quota_refusal_and_count_reserve_do_not_leave_pending(self) -> None:
        from tedd_knowledge_graph import source_archive

        archive = Archive.initialize(self.root / "quota-reuse", scope=self.scope, max_bytes=105000)
        unchanged = {**self.message, "text": "Stable neutral. " * 3000}
        first, raw = self.capture_messages([unchanged], archive=archive)
        second, _ = self.capture_messages(
            [unchanged, {**self.message, "id": "m2"}], archive=archive, version="v2"
        )
        self.assertLess(second.new_bytes, 8000)
        before = {path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            self.capture_messages(
                [unchanged, {**unchanged, "id": "m3"}], archive=archive, version="v3"
            )
        self.assertEqual(
            {path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()}, before
        )
        self.assertEqual(archive.original(first.ref), raw)
        with mock.patch.object(source_archive, "MAX_FILES", len(before) + 3):
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.capture_messages(
                    [unchanged, {**self.message, "id": "m3"}], archive=archive, version="v3"
                )
            archive.forget(first.ref)
            archive.recover()
        self.assertEqual(list((archive.scoped / "pending").iterdir()), [])

    def test_chat_preserves_not_and_if_and_original_bytes(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        payload = encode(self.raw)
        receipt = capture_chat(
            self.archive, metadata=self.metadata, payload=payload, acquired_sha256=digest(payload)
        )
        self.assertEqual(self.archive.original(receipt.ref), payload)
        self.assertEqual(self.archive.read(receipt.ref).units[0].text, self.message["text"])

    def test_unknown_speaker_and_observed_order_are_not_inferred(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        self.message.update(id=None, speaker=None)
        self.raw["messages"] = [
            self.message,
            {**self.message, "text": "Older message displayed later."},
        ]
        payload = encode(self.raw)
        receipt = capture_chat(
            self.archive, metadata=self.metadata, payload=payload, acquired_sha256=digest(payload)
        )
        units = self.archive.read(receipt.ref).units
        self.assertEqual([unit.position for unit in units], [1, 2])
        self.assertIsNone(units[0].speaker)
        self.assertIsNone(units[0].source_id)
        self.assertEqual(units[1].text, "Older message displayed later.")

    def test_approved_redacted_copy_preserves_transformation_and_omission_label(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        original_text = "Not approved. Reference: neutral-personal-detail."
        redacted_text = original_text.replace("neutral-personal-detail", "[removed]")
        self.message["text"] = redacted_text
        self.raw.update(coverage="partial", gaps=["Personal reference removed before capture."])
        payload = encode(self.raw)
        metadata = replace(
            self.metadata,
            coverage="partial",
            privacy="redacted",
            gaps=("Personal reference removed before capture.",),
        )

        receipt = capture_chat(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )

        self.assertEqual(self.archive.original(receipt.ref), payload)
        excerpt = self.archive.read(receipt.ref)
        self.assertEqual(excerpt.units[0].text, "Not approved. Reference: [removed].")
        self.assertEqual(excerpt.privacy, "redacted")
        self.assertEqual(excerpt.coverage, "partial")
        self.assertEqual(excerpt.gaps, ("Personal reference removed before capture.",))
        for path in self.archive.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"neutral-personal-detail", path.read_bytes())

    def test_partial_viewport_is_not_complete(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        self.raw.update(coverage="partial", gaps=["earlier messages not loaded"])
        payload = encode(self.raw)
        with self.assertRaises(SourceArchiveError):
            capture_chat(
                self.archive,
                metadata=self.metadata,
                payload=payload,
                acquired_sha256=digest(payload),
            )
        metadata = replace(
            self.metadata,
            capture_method="observed_dom_snapshot",
            coverage="partial",
            gaps=("earlier messages not loaded",),
        )
        result = capture_chat(
            self.archive, metadata=metadata, payload=payload, acquired_sha256=digest(payload)
        )
        self.assertEqual(result.coverage, "partial")

    def test_unsent_draft_is_not_provider_delivery(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        self.raw["delivery"] = "draft"
        payload = encode(self.raw)
        with self.assertRaises(SourceArchiveError):
            capture_chat(
                self.archive,
                metadata=replace(self.metadata, authorship="assistant"),
                payload=payload,
                acquired_sha256=digest(payload),
            )
        self.assertEqual(self.archive.lookup("case"), ())

    def test_reconstruction_or_mismatched_input_is_not_an_original(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        for invalid in (
            {**self.raw, "reconstructed": True},
            {**self.raw, "source_id": "other"},
            {**self.raw, "source_version": "other"},
        ):
            payload = encode(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(SourceArchiveError):
                capture_chat(
                    self.archive,
                    metadata=self.metadata,
                    payload=payload,
                    acquired_sha256=digest(payload),
                )
        with self.assertRaises(SourceArchiveError):
            capture_chat(
                self.archive,
                metadata=self.metadata,
                payload=encode(self.raw),
                acquired_sha256="0" * 64,
            )

    def test_conflicting_message_ids_and_unrelated_fields_are_refused(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        for messages in (
            [self.message, {**self.message, "text": "Competing content"}],
            [{**self.message, "unrelated": "Do not collect this field"}],
        ):
            payload = encode({**self.raw, "messages": messages})
            with self.subTest(messages=messages), self.assertRaises(SourceArchiveError):
                capture_chat(
                    self.archive,
                    metadata=self.metadata,
                    payload=payload,
                    acquired_sha256=digest(payload),
                )

    def test_reply_attachment_version_and_quotation_relationships_survive(self) -> None:
        from tedd_knowledge_graph.source_capture import capture_chat

        self.message["reply_to"] = "m0"
        self.message["links"] = [
            {
                "kind": "attachment",
                "source_id": "manual",
                "version": "v2",
                "unit_id": None,
                "archived_uri": None,
            },
            {
                "kind": "quote",
                "source_id": "proposal",
                "version": "v1",
                "unit_id": "p1",
                "archived_uri": None,
            },
        ]
        payload = encode(self.raw)
        receipt = capture_chat(
            self.archive, metadata=self.metadata, payload=payload, acquired_sha256=digest(payload)
        )
        unit = self.archive.read(receipt.ref).units[0]
        self.assertEqual(unit.reply_to, "m0")
        self.assertEqual(unit.links[0].kind, "attachment")
        self.assertEqual(unit.links[0].version, "v2")
        self.assertEqual(unit.links[1].kind, "quote")

    def test_task_closed_or_paused_rejects_capture_without_archive_io(self) -> None:
        from tedd_knowledge_graph.source_capture import run_if_collecting

        operation = mock.Mock(return_value="preserved")
        for state, enabled in (
            ("closed", True),
            ("paused", True),
            ("excluded", True),
            ("active", False),
        ):
            self.assertIsNone(run_if_collecting(operation, state=state, enabled=enabled))
        operation.assert_not_called()
        self.assertEqual(run_if_collecting(operation, state="waiting", enabled=True), "preserved")
        self.assertEqual(run_if_collecting(operation, state="active", enabled=True), "preserved")

    def test_pdf_original_digest_matches_input_and_reader_binding(self) -> None:
        from tedd_knowledge_graph.source_capture import PdfReading, capture_pdf

        payload = b"%PDF-1.4\nneutral contract fixture; not a parser qualification"
        metadata = replace(self.metadata, kind="pdf", capture_method="pdf_original")
        reading = PdfReading(
            digest(payload),
            "qualified-reader:1",
            1,
            (SourceUnit("u1", "Not approved.", 1, kind="page"),),
            "screened",
            "bounded",
        )
        result = capture_pdf(self.archive, metadata=metadata, payload=payload, reading=reading)
        self.assertEqual(self.archive.original(result.ref), payload)
        self.assertEqual(self.archive.read(result.ref).units, reading.units)
        current = replace(
            metadata,
            capture_method="local_file",
            details=SourceDetails(
                "local",
                "owner",
                "pdf",
                "original",
                "unread",
                sensitivity="ordinary",
                identity_version=2,
            ),
        )
        selected = capture_pdf(self.archive, metadata=current, payload=payload, reading=reading)
        saved = self.archive._manifest(selected.ref.manifest_sha256)
        assert saved.metadata.details is not None
        self.assertEqual(saved.metadata.details.reader_id, reading.reader_id)
        self.assertEqual(saved.format_version, 4)
        with self.assertRaises(SourceArchiveError):
            capture_pdf(
                self.archive, metadata=metadata, payload=payload + b"changed", reading=reading
            )

    def test_unavailable_reader_or_unscreened_pdf_is_not_verification_success(self) -> None:
        from tedd_knowledge_graph.source_capture import PdfReading, capture_pdf

        payload = b"%PDF-neutral"
        metadata = replace(self.metadata, kind="pdf", capture_method="pdf_original")
        with self.assertRaises(SourceArchiveError):
            capture_pdf(self.archive, metadata=metadata, payload=payload, reading=None)
        for privacy, limits in (("unknown", "bounded"), ("screened", "unbounded")):
            reading = PdfReading(
                digest(payload),
                "reader:1",
                1,
                (SourceUnit("u1", "neutral", 1, kind="page"),),
                privacy,
                limits,
            )
            with self.assertRaises(SourceArchiveError):
                capture_pdf(self.archive, metadata=metadata, payload=payload, reading=reading)

    def test_ocr_is_a_derivative_and_image_only_is_not_empty_success(self) -> None:
        from tedd_knowledge_graph.source_capture import PdfReading, capture_pdf

        payload = b"%PDF-neutral"
        metadata = replace(self.metadata, kind="pdf", capture_method="pdf_original")
        units = (
            SourceUnit("u1", "Possibly NOT approved", 1, kind="ocr_page"),
            SourceUnit("u2", "", 2, kind="image_page"),
        )
        reading = PdfReading(digest(payload), "reader:1", 2, units, "screened", "bounded")
        result = capture_pdf(self.archive, metadata=metadata, payload=payload, reading=reading)
        self.assertIn("visual_check_required", result.warnings)
        self.assertEqual(self.archive.read(result.ref).units[0].kind, "ocr_page")


if __name__ == "__main__":
    unittest.main()
