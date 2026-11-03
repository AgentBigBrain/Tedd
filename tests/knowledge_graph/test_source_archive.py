"""Private source storage behavior, with new local synthetic fixtures only."""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Protocol
from unittest import mock

from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_authority import AccessDecision, SourceAccess, access_binding
from tedd_knowledge_graph.source_contracts import (
    ArchiveLimits,
    CaptureReceipt,
    SheetExcerpt,
    SourceArchiveError,
    SourceDetails,
    SourceLocation,
    SourceMetadata,
    SourceScope,
    SourceUnit,
    digest,
    encode,
)

SCOPE = SourceScope("store", "generation", ("entity_task",), "personal_task")


class ProcessBarrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


class ResultSender(Protocol):
    def send(self, value: str) -> None: ...
    def close(self) -> None: ...


def metadata(version: str = "v1", *, source_id: str = "case") -> SourceMetadata:
    return SourceMetadata(
        SCOPE,
        source_id,
        version,
        "chat",
        "structured_export",
        "fixture:1",
        "task",
        "run",
        "interaction",
        "2026-09-05T12:00:00Z",
    )


def wire_capture(
    wire_format: int, source_id: str, content: str = "Not approved. Only if reviewed."
) -> tuple[SourceMetadata, bytes, tuple[SourceUnit, ...], bool]:
    from tedd_knowledge_graph.source_chat import decode_chat

    units: tuple[SourceUnit, ...]
    if wire_format == 1:
        current = metadata(source_id=source_id)
        payload = encode({"text": content})
        units = (SourceUnit("u1", content, 1),)
        reuse_messages = False
    elif wire_format == 2:
        current = metadata(source_id=source_id)
        payload = encode(
            {
                "format": 1,
                "source_id": source_id,
                "source_version": "v1",
                "coverage": "partial",
                "gaps": [],
                "delivery": "sent",
                "messages": [
                    {
                        "id": "message-one",
                        "speaker": "external",
                        "text": content,
                        "time": None,
                        "reply_to": None,
                        "links": [],
                    }
                ],
            }
        )
        units = decode_chat(payload, current)
        reuse_messages = True
    elif wire_format in (3, 4, 5):
        current = replace(
            metadata(source_id=source_id),
            kind="text",
            capture_method="local_file",
            details=SourceDetails(
                "local",
                "owner",
                source_id + "-representation",
                "original",
                "reader:1",
                title="Shared synthetic title",
                sensitivity="ordinary",
                identity_version=2 if wire_format in (4, 5) else 1,
            ),
        )
        payload = content.encode("utf-8")
        units = (
            SourceUnit(
                "u1",
                content,
                1,
                "line",
                location=SourceLocation("line", None, 1, 1),
            ),
        )
        reuse_messages = False
        if wire_format == 5:
            assert current.details is not None
            current = replace(
                current,
                kind="xlsx",
                capture_method="owner_confirmed_export",
                details=replace(
                    current.details,
                    provider="google",
                    representation="provider_export",
                    sheet_excerpt=SheetExcerpt(source_id + "-original", "7"),
                ),
            )
    else:
        raise AssertionError("Unsupported synthetic wire format")
    return current, payload, units, reuse_messages


def race_publish(
    root: str,
    wire_format: int,
    content: str,
    barrier: ProcessBarrier,
    result: ResultSender,
) -> None:
    from tedd_knowledge_graph.source_archive import Archive

    try:
        archive = Archive.open(Path(root), scope=SCOPE, readonly=False)
        barrier.wait(timeout=15)
        current, payload, units, reuse_messages = wire_capture(wire_format, "case", content)
        receipt = archive.publish(
            payload=payload,
            metadata=current,
            units=units,
            reuse_messages=reuse_messages,
        )
        result.send(receipt.status)
    except SourceArchiveError as error:
        result.send(error.code)
    finally:
        result.close()


class SourceArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        temporary = tempfile.TemporaryDirectory(
            prefix="tedd-source-test-", dir=private_temporary_parent()
        )
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        set_private_path_mode(self.parent, 0o700)
        self.root = self.parent / "sources"
        self.archive = Archive.initialize(
            self.root,
            scope=SCOPE,
            access=SourceAccess(SCOPE, "model", ("ordinary",), local_retention=True),
        )
        self.units = (SourceUnit("u1", "Not approved. Only if reviewed.", 1),)
        self.payload = b'{"text":"Not approved. Only if reviewed."}'

    def publish(self, version: str = "v1") -> CaptureReceipt:
        return self.archive.publish(
            payload=self.payload, metadata=metadata(version), units=self.units
        )

    def test_image_backup_keeps_newer_references_unavailable_and_current_denials_intact(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_images import ImageInventory, ImageUnit

        from tests.knowledge_graph.test_source_office import png_image

        original = png_image()
        later_bytes = png_image(pixels=b"\0\0\0")
        survivor_bytes = png_image(pixels=b"\0\0\xff")
        units = ImageInventory(
            digest(original), "png", (ImageUnit("still", 0, (1, 1)),)
        ).source_units()
        current, _, _, _ = wire_capture(4, "selected-image")
        assert current.details is not None
        details = replace(current.details, reader_id="tedd-image:1")
        current = replace(current, kind="image", details=details)
        survivor_metadata = replace(
            current,
            source_id="unrelated-image",
            details=replace(details, representation_id="unrelated-original"),
        )

        # This directly tests archive/reference lifecycle, not acquisition,
        # decoder admission, owner acknowledgment, or old-software activation.
        for disposition in ("retain", "erase"):
            with self.subTest(disposition=disposition):
                archive = Archive.initialize(
                    self.parent / f"image-{disposition}", scope=SCOPE, access=self.archive.access
                )
                first = archive.publish(payload=original, metadata=current, units=units)
                duplicate = archive.publish(payload=original, metadata=current, units=units)
                self.assertEqual(duplicate.ref, first.ref)
                survivor = archive.publish(
                    payload=survivor_bytes, metadata=survivor_metadata, units=units
                )
                backup = self.parent / f"image-{disposition}-backup"
                archive.backup(backup)
                with self.assertRaises(SourceArchiveError):
                    Archive.open(backup, scope=SCOPE, access=archive.access)

                later = archive.publish(
                    payload=later_bytes,
                    metadata=replace(current, source_version="v2", interaction_id="later-image"),
                    units=units,
                )
                restored = Archive.restore(
                    backup, self.parent / f"image-{disposition}-older", current=archive
                )
                restored = Archive.open(restored.root, scope=SCOPE, access=archive.access)
                self.assertEqual(restored.image(first.ref)[0], original)
                with self.assertRaises(SourceArchiveError):
                    restored.image(later.ref)  # Never substitute the older white image.

                if disposition == "retain":
                    archive.restrict(first.ref)
                else:
                    archive.forget(first.ref)
                restored = Archive.restore(
                    backup, self.parent / f"image-{disposition}-denied", current=archive
                )
                restored.recover()
                restored = Archive.open(restored.root, scope=SCOPE, access=archive.access)
                for ref in (first.ref, later.ref):
                    with self.assertRaises(SourceArchiveError):
                        restored.image(ref)
                    with self.assertRaises(SourceArchiveError):
                        restored.original(ref)
                self.assertEqual(restored.image(survivor.ref)[0], survivor_bytes)
                self.assertEqual(archive.image(survivor.ref)[0], survivor_bytes)

    def test_retained_restriction_survives_recovery_backup_restore_and_shared_erasure(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        first = self.publish()
        self.publish("v2")
        history = {
            path.relative_to(self.root): path.read_bytes()
            for folder in ("versions", "heads", "blobs", "readings")
            for path in (self.archive.scoped / folder).iterdir()
        }
        old = self.parent / "old-backup"
        self.archive.backup(old)
        other = self.archive.publish(
            payload=self.payload, metadata=metadata(source_id="other"), units=self.units
        )
        self.archive.restrict(first.ref)
        markers = {path: path.read_bytes() for path in (self.archive.scoped / "denials").iterdir()}
        self.assertTrue(self.archive.forget(other.ref).shared_content_retained)
        self.archive.recover()
        fresh = self.parent / "restricted-backup"
        self.archive.backup(fresh)
        for number, backup in enumerate((old, fresh)):
            restored = Archive.restore(
                backup, self.parent / f"restored-{number}", current=self.archive
            )
            restored.restrict(first.ref)
            restored.recover()
            self.assertEqual(
                {path: (restored.root / path).read_bytes() for path in history}, history
            )
            with self.assertRaises(SourceArchiveError) as denied:
                restored.read(first.ref)
            self.assertEqual(denied.exception.code, "scope_denied")
        self.archive.restrict(first.ref)
        self.assertEqual({path: path.read_bytes() for path in markers}, markers)
        self.assertEqual({path: (self.root / path).read_bytes() for path in history}, history)
        self.archive.forget(first.ref)
        self.archive.restrict(first.ref)
        self.archive.recover()
        self.assertFalse(tuple((self.archive.scoped / "versions").iterdir()))
        self.assertFalse(tuple((self.archive.scoped / "blobs").iterdir()))
        self.assertFalse(tuple((self.archive.scoped / "readings").iterdir()))
        self.assertEqual({path: path.read_bytes() for path in markers}, markers)

    def test_interrupted_restriction_expands_without_erasing_later_versions(self) -> None:
        first = self.publish()
        later = self.publish("v2")
        history = {
            path: path.read_bytes()
            for folder in ("versions", "heads", "blobs", "readings")
            for path in (self.archive.scoped / folder).iterdir()
        }
        with mock.patch.object(self.archive, "_expand_denial", autospec=True, side_effect=OSError):
            with self.assertRaises(SourceArchiveError):
                self.archive.restrict(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(later.ref)
        self.archive.recover()
        self.assertEqual({path: path.read_bytes() for path in history}, history)

    def test_retained_chat_recipe_and_document_survive_shared_source_erasure(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        for wire_format in (2, 3):
            with self.subTest(wire_format=wire_format):
                archive = Archive.initialize(
                    self.parent / f"retained-{wire_format}", scope=SCOPE, access=self.archive.access
                )
                _, _, _, first = self.publish_wire_format(archive, wire_format, "first")
                history = {
                    path: path.read_bytes()
                    for path in archive._all_files()
                    if path.parent.name in ("versions", "heads", "blobs", "readings")
                }
                _, payload, _, other = self.publish_wire_format(archive, wire_format, "other")
                archive.restrict(first.ref)
                self.assertEqual(archive.original(other.ref), payload)
                self.assertTrue(archive.forget(other.ref).shared_content_retained)
                archive.recover()
                archive.backup(self.parent / f"backup-retained-{wire_format}")
                self.assertEqual({path: path.read_bytes() for path in history}, history)
                with self.assertRaises(SourceArchiveError):
                    archive.read(first.ref)

    def test_legacy_erasure_wins_retained_markers_in_either_iteration_order(self) -> None:
        from tedd_knowledge_graph.source_archive import _Denial

        first = self.publish()
        self.archive.restrict(first.ref)
        restricted = self.archive._denials()
        self.archive.deny(first.ref)
        markers = self.archive._denials()
        self.assertTrue(any(item.disposition == "erase" for item in markers))
        self.assertTrue(all(item.disposition == "retain" for item in restricted))
        for ordered in (markers, tuple(reversed(markers))):
            with (
                self.subTest(order=ordered),
                mock.patch.object(self.archive, "_denials", autospec=True, return_value=ordered),
            ):
                for item in ordered:
                    self.assertEqual(
                        self.archive._expand_denial(item, publish=False).disposition, "erase"
                    )
        legacy = encode({"source": "a" * 64, "manifests": [], "payloads": [], "readings": []})
        self.assertEqual(_Denial.decode(legacy).disposition, "erase")
        with self.assertRaises(SourceArchiveError):
            _Denial.decode(
                encode(
                    {
                        "source": "a" * 64,
                        "manifests": [],
                        "payloads": [],
                        "readings": [],
                        "disposition": "unknown",
                    }
                )
            )
        self.archive.recover()
        self.assertFalse(tuple((self.archive.scoped / "versions").iterdir()))

    def publish_wire_format(
        self, archive: object, wire_format: int, source_id: str
    ) -> tuple[SourceMetadata, bytes, tuple[SourceUnit, ...], CaptureReceipt]:
        from tedd_knowledge_graph.source_archive import Archive

        assert isinstance(archive, Archive)
        current, payload, units, reuse_messages = wire_capture(wire_format, source_id)
        receipt = archive.publish(
            payload=payload,
            metadata=current,
            units=units,
            reuse_messages=reuse_messages,
        )
        return current, payload, units, receipt

    def test_owner_preview_requires_separate_access_and_excludes_legacy_unknown_policy(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        legacy = self.publish()
        details = SourceDetails(
            "local", "owner", "file", "original", "reader", sensitivity="ordinary"
        )
        receipt = self.archive.publish(
            payload=self.payload,
            metadata=replace(
                metadata(),
                source_id="document",
                kind="text",
                capture_method="local_file",
                details=details,
            ),
            units=self.units,
        )
        owner = Archive.open(
            self.root,
            scope=SCOPE,
            access=SourceAccess(SCOPE, "owner", ("ordinary",), local_retention=True),
        )
        self.assertEqual(owner.read(receipt.ref, operation="owner_preview").units, self.units)
        for handle, ref in ((self.archive, receipt.ref), (owner, legacy.ref)):
            with self.subTest(ref=ref), self.assertRaises(SourceArchiveError):
                handle.read(ref, operation="owner_preview")
        self.assertEqual(self.archive.read(legacy.ref).units, self.units)

    def test_mismatched_access_is_rejected_before_creating_an_archive(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        target = self.parent / "must-not-be-created"
        access = SourceAccess(replace(SCOPE, generation="other"), "owner", ("ordinary",))
        with self.assertRaises(SourceArchiveError):
            Archive.initialize(target, scope=SCOPE, access=access)
        self.assertFalse(target.exists())

    def test_display_title_does_not_expose_a_local_source_path(self) -> None:
        details = SourceDetails(
            "local",
            "owner",
            "file",
            "original",
            "reader",
            title="C:\\Users\\synthetic-owner\\document.txt",
            sensitivity="ordinary",
        )
        receipt = self.archive.publish(
            payload=self.payload,
            metadata=replace(
                metadata(),
                source_id="local-document",
                kind="text",
                capture_method="local_file",
                details=details,
            ),
            units=self.units,
        )
        self.assertNotIn("synthetic-owner", self.archive.read(receipt.ref).title or "")

    def test_logical_identity_not_raw_id_or_title_scopes_history_and_forgetting(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        def allow(
            current: SourceMetadata, payload_sha256: str, operation: str, audience: str
        ) -> AccessDecision:
            return AccessDecision(
                access_binding(current, payload_sha256), operation, audience, "allowed"
            )

        archive = Archive.open(
            self.root,
            scope=SCOPE,
            readonly=False,
            access=SourceAccess(SCOPE, "model", ("ordinary",), provider_check=allow),
        )
        shared = SourceDetails(
            "provider-one",
            "account-one",
            "representation-one",
            "provider_export",
            "reader:1",
            title="Shared synthetic title",
            sensitivity="ordinary",
        )
        identities = (
            ("base", shared, "text"),
            ("provider", replace(shared, provider="provider-two"), "text"),
            ("account", replace(shared, principal="account-two"), "text"),
            ("kind", shared, "markdown"),
        )
        histories: list[tuple[SourceMetadata, tuple[CaptureReceipt, CaptureReceipt]]] = []
        for number, (case, details, kind) in enumerate(identities, start=1):
            first_metadata = replace(
                metadata(source_id="same-raw-resource-id"),
                kind=kind,
                capture_method="provider_export",
                interaction_id=f"interaction-{number}-one",
                details=details,
            )
            first_payload = f"Neutral body {number}, version one.".encode()
            first_units = (
                SourceUnit(
                    "u1",
                    first_payload.decode(),
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 1),
                ),
            )
            second_metadata = replace(
                first_metadata,
                source_version="v2",
                interaction_id=f"interaction-{number}-two",
                details=replace(
                    details,
                    representation_id="representation-two",
                    title="Renamed synthetic title",
                ),
            )
            second_payload = f"Neutral body {number}, version two.".encode()
            first = archive.publish(
                payload=first_payload, metadata=first_metadata, units=first_units
            )
            second = archive.publish(
                payload=second_payload,
                metadata=second_metadata,
                units=(replace(first_units[0], text=second_payload.decode()),),
            )
            with self.subTest(case=case):
                self.assertEqual(first_metadata.logical_key, second_metadata.logical_key)
                self.assertEqual(archive.lookup(first_metadata), (first.ref, second.ref))
                self.assertEqual(archive.read(first.ref).later_versions, (second.ref,))
            histories.append((first_metadata, (first, second)))

        self.assertEqual(len({item.logical_key for item, _ in histories}), len(identities))
        forgotten_metadata, forgotten = histories[0]
        same_resource = (histories[0], histories[-1])
        expected_manifests = {
            receipt.ref.manifest_sha256 for _, receipts in same_resource for receipt in receipts
        }
        self.assertEqual(set(archive.forget_identity(forgotten[0].ref)[0]), expected_manifests)
        archive.restrict(forgotten[0].ref)
        for current, receipts in same_resource:
            self.assertEqual(archive.lookup(current), ())
            for receipt in receipts:
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    archive.read(receipt.ref)
                self.assertTrue(
                    (archive.scoped / "versions" / receipt.ref.manifest_sha256).exists()
                )
        archive.forget(forgotten[0].ref)
        archive.recover()
        self.assertEqual(archive.lookup(forgotten_metadata), ())
        for current, receipts in same_resource:
            self.assertFalse((archive.scoped / "heads" / current.logical_key).exists())
            for receipt in receipts:
                self.assertFalse(
                    (archive.scoped / "versions" / receipt.ref.manifest_sha256).exists()
                )
                with self.assertRaises(SourceArchiveError):
                    archive.read(receipt.ref)
        # Format-3 history IDs stay kind-specific, but retention exclusions are
        # resource-wide. Only different provider/account resources survive.
        for number, (current, receipts) in enumerate(histories[1:3], start=2):
            with self.subTest(survivor=current.logical_key):
                self.assertEqual(archive.lookup(current), tuple(item.ref for item in receipts))
                self.assertEqual(
                    archive.original(receipts[-1].ref),
                    f"Neutral body {number}, version two.".encode(),
                )

    def test_new_artifacts_and_readers_do_not_replace_an_old_citation(self) -> None:
        details = SourceDetails(
            "local", "owner", "representation-one", "original", "reader:1", sensitivity="ordinary"
        )
        original_meta = replace(
            metadata(), kind="text", capture_method="local_file", details=details
        )
        units = (
            SourceUnit(
                "u1", "Not approved.", 1, "line", location=SourceLocation("line", None, 1, 1)
            ),
        )
        try:
            first = self.archive.publish(
                payload=b"Not approved.", metadata=original_meta, units=units
            )
        except SourceArchiveError as error:
            self.fail(f"Validated new source publication was rejected: {error.code}")
        repeated = self.archive.publish(
            payload=b"Not approved.", metadata=original_meta, units=units
        )
        self.assertEqual(repeated.status, "duplicate")
        second_meta = replace(
            original_meta, details=replace(details, representation_id="representation-two")
        )
        second = self.archive.publish(payload=b"Not approved.\n", metadata=second_meta, units=units)
        reader_meta = replace(original_meta, details=replace(details, reader_id="reader:2"))
        revised_units = (replace(units[0], text="Not approved. [new reader]"),)
        third = self.archive.publish(
            payload=b"Not approved.", metadata=reader_meta, units=revised_units
        )
        self.assertNotEqual(first.ref, second.ref)
        self.assertNotEqual(first.ref, third.ref)
        self.assertEqual(self.archive.read(first.ref).units, units)
        self.assertEqual(self.archive.original(first.ref), b"Not approved.")
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(payload=b"Approved.", metadata=original_meta, units=units)

    def test_new_format_recovery_and_backup_leave_legacy_objects_unchanged(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        old = self.publish()
        immutable = {
            path: path.read_bytes()
            for folder in ("blobs", "readings", "versions")
            for path in (self.archive.scoped / folder).iterdir()
        }
        details = SourceDetails(
            "local", "owner", "plain-text", "original", "reader:1", sensitivity="ordinary"
        )
        new_metadata = replace(
            metadata(), kind="text", capture_method="local_file", details=details
        )
        units = (
            SourceUnit(
                "u1", "Only if approved.", 1, "line", location=SourceLocation("line", None, 1, 1)
            ),
        )
        new = self.archive.publish(payload=b"Only if approved.", metadata=new_metadata, units=units)
        self.archive.recover()
        self.assertEqual({path: path.read_bytes() for path in immutable}, immutable)
        self.assertEqual(self.archive.original(old.ref), self.payload)
        backup, restored = self.parent / "backup", self.parent / "restored"
        self.archive.backup(backup)
        Archive.restore(backup, restored, current=self.archive)
        copy = Archive.open(restored, scope=SCOPE, access=self.archive.access)
        self.assertEqual(copy.original(new.ref), b"Only if approved.")
        self.assertEqual(copy.original(old.ref), self.payload)
        self.archive.forget(new.ref)
        self.assertEqual({path: path.read_bytes() for path in immutable}, immutable)

    def test_uncertain_new_identity_does_not_merge_encounters(self) -> None:
        details = SourceDetails(
            "local", "owner", "plain-text", "original", "reader:1", sensitivity="ordinary"
        )
        first_metadata = replace(
            metadata(),
            kind="text",
            capture_method="local_file",
            details=details,
            identity_known=False,
        )
        second_metadata = replace(first_metadata, interaction_id="other-encounter")
        units = (
            SourceUnit(
                "u1", "Not approved.", 1, "line", location=SourceLocation("line", None, 1, 1)
            ),
        )
        first = self.archive.publish(payload=b"Not approved.", metadata=first_metadata, units=units)
        first_reread_metadata = replace(
            first_metadata, details=replace(details, reader_id="reader:2")
        )
        first_reread = self.archive.publish(
            payload=b"Not approved.",
            metadata=first_reread_metadata,
            units=(replace(units[0], text="Not approved. [reader two]"),),
        )
        second = self.archive.publish(
            payload=b"Not approved.", metadata=second_metadata, units=units
        )
        second_reread = self.archive.publish(
            payload=b"Not approved.",
            metadata=replace(second_metadata, details=replace(details, reader_id="reader:2")),
            units=(replace(units[0], text="Not approved. [reader two]"),),
        )
        self.assertEqual(self.archive.lookup(first_metadata), (first.ref, first_reread.ref))
        self.assertEqual(self.archive.lookup(second_metadata), (second.ref, second_reread.ref))
        self.archive.forget(first.ref)
        self.assertEqual(self.archive.lookup(first_metadata), ())
        self.assertEqual(self.archive.lookup(second_metadata), (second.ref, second_reread.ref))
        for receipt in (first, first_reread):
            with self.assertRaises(SourceArchiveError):
                self.archive.read(receipt.ref)
        self.assertEqual(self.archive.original(second.ref), b"Not approved.")

    def test_manifest_formats_share_recovery_backup_and_deny_first_lifecycle(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import parse_json

        for wire_format in (1, 2, 3, 4, 5):
            with self.subTest(wire_format=wire_format):
                root = self.parent / f"format-{wire_format}"
                archive = Archive.initialize(
                    root,
                    scope=SCOPE,
                    access=SourceAccess(
                        SCOPE,
                        "model",
                        ("ordinary",),
                        local_retention=True,
                        provider_check=lambda metadata, checksum, operation, audience: (
                            AccessDecision(
                                access_binding(metadata, checksum), operation, audience, "allowed"
                            )
                        ),
                    ),
                )
                _, payload, units, first = self.publish_wire_format(
                    archive, wire_format, "format-target"
                )
                before_duplicate = {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                duplicate = self.publish_wire_format(archive, wire_format, "format-target")[3]
                self.assertEqual((duplicate.status, duplicate.ref), ("duplicate", first.ref))
                self.assertEqual(
                    {
                        path.relative_to(root): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    },
                    before_duplicate,
                )
                _, survivor_payload, _, survivor = self.publish_wire_format(
                    archive, wire_format, "format-survivor"
                )
                self.assertEqual(len(tuple((archive.scoped / "blobs").iterdir())), 1)
                manifest_path = archive.scoped / "versions" / first.ref.manifest_sha256
                manifest = archive._manifest(first.ref.manifest_sha256)
                manifest_value = parse_json(manifest_path.read_bytes())
                assert isinstance(manifest_value, dict)
                self.assertEqual(manifest_value["format"], wire_format)
                reading_path = archive.scoped / "readings" / archive._reading_object(manifest)
                reading_value = parse_json(reading_path.read_bytes())
                if wire_format == 1:
                    self.assertIsInstance(reading_value, list)
                else:
                    assert isinstance(reading_value, dict)
                    self.assertEqual(reading_value["format"], 1 if wire_format == 2 else 2)

                committed = {
                    path.relative_to(root): path.read_bytes()
                    for folder in ("blobs", "readings", "versions", "heads")
                    for path in (archive.scoped / folder).iterdir()
                }
                orphan_blob = (
                    archive.scoped / "blobs" / digest(f"orphan-blob-{wire_format}".encode())
                )
                orphan_reading = (
                    archive.scoped / "readings" / digest(f"orphan-reading-{wire_format}".encode())
                )
                pending = archive.scoped / "pending" / digest(f"partial-{wire_format}".encode())
                for path, body in (
                    (orphan_blob, f"orphan-blob-{wire_format}".encode()),
                    (orphan_reading, f"orphan-reading-{wire_format}".encode()),
                    (pending, encode({"next": "0" * 64})),
                ):
                    source_files.write_bytes(path, body)
                archive.recover()
                self.assertFalse(orphan_blob.exists())
                self.assertFalse(orphan_reading.exists())
                self.assertFalse(pending.exists())
                self.assertEqual(
                    {root / path: (root / path).read_bytes() for path in committed},
                    {root / path: body for path, body in committed.items()},
                )
                self.assertEqual(archive.original(first.ref), payload)
                self.assertEqual(archive.read(first.ref).units, units)

                backup = self.parent / f"format-{wire_format}-backup"
                archive.backup(backup)
                for relative, body in committed.items():
                    self.assertEqual((backup / relative).read_bytes(), body)
                broken = self.parent / f"format-{wire_format}-broken"
                archive.backup(broken)
                (broken / manifest_path.relative_to(root)).write_bytes(b"corrupt")
                refused = self.parent / f"format-{wire_format}-refused"
                with self.assertRaises(SourceArchiveError):
                    Archive.restore(broken, refused, current=archive)
                self.assertFalse(refused.exists())

                archive.configure_limits(
                    ArchiveLimits(pdf_bytes=1, chat_bytes=1, manifest_bytes=1, reading_bytes=1)
                )
                archive.recover()
                self.assertEqual(archive.original(first.ref), payload)
                with mock.patch.object(
                    archive, "_erase", side_effect=OSError("synthetic interrupted erasure")
                ):
                    with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                        archive.forget(first.ref)
                with self.assertRaises(SourceArchiveError):
                    archive.read(first.ref)
                self.assertEqual(archive.original(survivor.ref), survivor_payload)

                restored_root = self.parent / f"format-{wire_format}-restored"
                Archive.restore(backup, restored_root, current=archive)
                restored = Archive.open(restored_root, scope=SCOPE, access=archive.access)
                with self.assertRaises(SourceArchiveError):
                    restored.read(first.ref)
                self.assertEqual(
                    restored.original(survivor.ref),
                    survivor_payload,
                )
                archive.recover()
                with self.assertRaises(SourceArchiveError):
                    archive.read(first.ref)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in (archive.scoped / "denials").iterdir()
                    },
                    {
                        path.name: path.read_bytes()
                        for path in (restored.scoped / "denials").iterdir()
                    },
                )

    def test_resource_restriction_survives_identity_format_changes(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        for before, after in ((3, 4), (4, 3)):
            for erase in (False, True):
                with self.subTest(before=before, after=after, erase=erase):
                    archive = Archive.initialize(
                        self.parent / f"identity-{before}-{erase}",
                        scope=SCOPE,
                        access=self.archive.access,
                    )
                    old, payload, units, _ = wire_capture(before, "same-resource")
                    saved = archive.publish(payload=payload, metadata=old, units=units)
                    if erase:
                        archive.forget(saved.ref)
                    else:
                        archive.restrict(saved.ref)
                    changed, payload, units, _ = wire_capture(
                        after, "same-resource", "Changed bytes must remain excluded."
                    )
                    reopened = Archive.open(
                        archive.root, scope=SCOPE, readonly=False, access=archive.access
                    )
                    with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                        reopened.publish(payload=payload, metadata=changed, units=units)

    def test_restrict_then_forget_sibling_is_order_independent_and_recovers(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        real_unlink = Path.unlink
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                archive = Archive.initialize(
                    self.parent / f"sibling-order-{order[0]}",
                    scope=SCOPE,
                    access=self.archive.access,
                )
                receipts = []
                for kind in ("text", "markdown"):
                    current, payload, units, _ = wire_capture(3, "resource", kind + " bytes")
                    receipts.append(
                        archive.publish(
                            payload=payload, metadata=replace(current, kind=kind), units=units
                        )
                    )
                archive.restrict(receipts[order[0]].ref)
                target = archive.scoped / "versions" / receipts[order[1]].ref.manifest_sha256

                def interrupt_erasure(
                    path: Path, missing_ok: bool = False, target: Path = target
                ) -> None:
                    real_unlink(path, missing_ok=missing_ok)
                    if path == target:
                        raise OSError("synthetic interruption after a sibling manifest erasure")

                with (
                    mock.patch.object(Path, "unlink", interrupt_erasure),
                    self.assertRaisesRegex(SourceArchiveError, "source_unavailable"),
                ):
                    archive.forget(receipts[order[1]].ref)
                reopened = Archive.open(
                    archive.root, scope=SCOPE, readonly=False, access=archive.access
                )
                reopened.recover()
                for folder in ("versions", "heads", "pending", "blobs", "readings"):
                    self.assertEqual(list((reopened.scoped / folder).iterdir()), [])
                for receipt in receipts:
                    with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                        reopened.read(receipt.ref)

    def test_older_erasure_marker_blocks_changed_bytes_across_identity_formats(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive

        for before, after in ((3, 4), (4, 3)):
            with self.subTest(before=before, after=after):
                archive = Archive.initialize(
                    self.parent / f"old-marker-{before}", scope=SCOPE, access=self.archive.access
                )
                old, _, _, _ = wire_capture(before, "erased-resource")
                # A pre-repair erasure leaves only the original identity, without
                # newer compatibility aliases or any retained payload to compare.
                marker = encode(
                    {"source": old.logical_key, "manifests": [], "payloads": [], "readings": []}
                )
                source_files.write_bytes(archive.scoped / "denials" / digest(marker), marker)
                reopened = Archive.open(
                    archive.root, scope=SCOPE, readonly=False, access=archive.access
                )
                backup = self.parent / f"old-marker-backup-{before}"
                reopened.backup(backup)
                restored = Archive.restore(
                    backup, self.parent / f"old-marker-restored-{before}", current=reopened
                )
                changed, payload, units, _ = wire_capture(
                    after, "erased-resource", "Changed bytes must remain excluded."
                )
                for handle in (reopened, restored):
                    handle.access = self.archive.access
                    with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                        handle.publish(payload=payload, metadata=changed, units=units)

    def test_excerpt_forget_keeps_copy_excluded_after_restart_without_annotation(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.archive.access = SourceAccess(
            SCOPE,
            "model",
            ("ordinary",),
            local_retention=True,
            provider_check=lambda metadata, checksum, operation, audience: AccessDecision(
                access_binding(metadata, checksum), operation, audience, "allowed"
            ),
        )
        current, payload, units, _ = wire_capture(5, "copy")
        first = self.archive.publish(payload=payload, metadata=current, units=units)
        assert current.details is not None and current.details.sheet_excerpt is not None
        for metadata_change in (
            replace(current, details=replace(current.details, sheet_excerpt=None)),
            replace(
                current,
                details=replace(
                    current.details, sheet_excerpt=SheetExcerpt("different-parent", "7")
                ),
            ),
            replace(
                current,
                source_id="nested-copy",
                details=replace(current.details, sheet_excerpt=SheetExcerpt("copy", "7")),
            ),
        ):
            with (
                self.subTest(metadata=metadata_change),
                self.assertRaisesRegex(SourceArchiveError, "scope_denied"),
            ):
                self.archive.publish(payload=payload, metadata=metadata_change, units=units)
        self.archive.forget(first.ref)
        reopened = Archive.open(self.root, scope=SCOPE, readonly=False, access=self.archive.access)
        self.assertFalse(tuple((reopened.scoped / "versions").iterdir()))
        changed = replace(
            current,
            source_version="later",
            interaction_id="later",
            details=replace(current.details, sheet_excerpt=None),
        )
        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
            reopened.publish(
                payload=b"Changed bytes do not erase a retention exclusion.",
                metadata=changed,
                units=units,
            )

    def test_excerpt_copy_marker_cannot_narrow_shared_resource_erasure(self) -> None:
        assert self.archive.access is not None
        self.archive.access = replace(
            self.archive.access,
            provider_check=lambda metadata, checksum, operation, audience: AccessDecision(
                access_binding(metadata, checksum), operation, audience, "allowed"
            ),
        )
        receipts = []
        copies = []
        for number in (1, 2):
            current, payload, units, _ = wire_capture(5, f"copy-{number}", f"Excerpt {number}.")
            assert current.details is not None
            current = replace(
                current,
                details=replace(
                    current.details, sheet_excerpt=SheetExcerpt("workbook", str(number))
                ),
            )
            copies.append(current)
            receipts.append(self.archive.publish(payload=payload, metadata=current, units=units))
        self.archive.restrict(receipts[0].ref)
        copy_key = copies[0]._identity_key(copies[0].source_id)
        # Earlier recovery could persist a valid copy-specific retained marker.
        # Keep that compatibility case even though new recovery normalizes it.
        manifest = self.archive._manifest(receipts[0].ref.manifest_sha256)
        older = replace(
            self.archive._initial_denial(receipts[0].ref),
            source=copy_key,
            manifests=(receipts[0].ref.manifest_sha256,),
            payloads=(manifest.payload_sha256,),
            readings=(self.archive._reading_object(manifest),),
        ).encoded()
        self.archive._publish(self.archive.scoped / "denials" / digest(older), older)
        self.archive.recover()
        denials = self.archive._denials()
        self.assertTrue(any(d.source == copy_key and d.manifests for d in denials))
        # Force the formerly problematic valid ledger order, not a forged grant.
        denials = tuple(sorted(denials, key=lambda item: item.source != copy_key))
        with mock.patch.object(self.archive, "_denials", return_value=denials):
            for receipt in receipts:
                with self.subTest(source=receipt.ref.manifest_sha256):
                    self.assertEqual(
                        set(self.archive.forget_identity(receipt.ref)[0]),
                        {item.ref.manifest_sha256 for item in receipts},
                    )
        self.archive.forget(receipts[0].ref)
        self.archive.recover()
        for folder in ("versions", "heads", "pending", "blobs", "readings"):
            self.assertEqual(list((self.archive.scoped / folder).iterdir()), [])

    def test_excerpt_refuses_legacy_parent_identity_before_and_after_forgetting(self) -> None:
        self.archive.access = SourceAccess(
            SCOPE,
            "model",
            ("ordinary",),
            local_retention=True,
            provider_check=lambda metadata, checksum, operation, audience: AccessDecision(
                access_binding(metadata, checksum), operation, audience, "allowed"
            ),
        )
        excerpt, _payload, units, _ = wire_capture(5, "copy")
        assert excerpt.details is not None and excerpt.details.sheet_excerpt is not None
        legacy = replace(
            excerpt,
            source_id=excerpt.details.sheet_excerpt.source_id,
            kind="google_sheet",
            capture_method="structured_observation",
            details=replace(
                excerpt.details,
                sheet_excerpt=None,
                identity_version=1,
                representation="structured_observation",
            ),
        )
        saved = self.archive.publish(
            payload=b"Legacy observed bytes.", metadata=legacy, units=units
        )
        for forgotten in (False, True):
            if forgotten:
                self.archive.forget(saved.ref)
            with (
                self.subTest(forgotten=forgotten),
                self.assertRaisesRegex(SourceArchiveError, "scope_denied"),
            ):
                self.archive.publish(
                    payload=b"Different excerpt bytes.", metadata=excerpt, units=units
                )

    def test_new_source_metadata_does_not_grant_low_level_body_access(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        details = SourceDetails(
            "local", "owner", "file", "original", "reader", sensitivity="ordinary"
        )
        new_metadata = replace(
            metadata(), kind="text", capture_method="local_file", details=details
        )
        units = (
            SourceUnit(
                "u1", "Not approved.", 1, "line", location=SourceLocation("line", None, 1, 1)
            ),
        )
        receipt = self.archive.publish(payload=b"Not approved.", metadata=new_metadata, units=units)
        reader = Archive.open(self.root, scope=SCOPE)
        for operation in (
            lambda: reader.original(receipt.ref),
            lambda: reader.read(receipt.ref),
            lambda: reader.verify_refs((receipt.ref,)),
            lambda: reader.lookup(new_metadata),
        ):
            with self.subTest(operation=operation), self.assertRaises(SourceArchiveError):
                operation()
        result = reader.discover()
        self.assertEqual(result.sources, ())
        self.assertTrue(result.access_incomplete)

    def test_authorized_new_titles_are_distinguishable_without_raw_identity(self) -> None:
        for number, title in enumerate(("Decision — proposal", "Decision [reviewed]"), 1):
            current = replace(
                metadata(source_id=f"private-resource-{number}"),
                kind="text",
                capture_method="local_file",
                details=SourceDetails(
                    "local",
                    "owner",
                    "file",
                    "original",
                    "reader",
                    title=title,
                    sensitivity="ordinary",
                ),
            )
            self.archive.publish(payload=b"Neutral", metadata=current, units=self.units)
        result = self.archive.discover(query="Decision")
        self.assertEqual(
            {item.title for item in result.sources}, {"Decision — proposal", "Decision [reviewed]"}
        )
        self.assertNotIn(b"private-resource", encode(asdict(result)))

    def test_historical_read_filters_later_versions_by_exact_authority(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        current, payload, units, _ = wire_capture(3, "history")
        first = self.archive.publish(payload=payload, metadata=current, units=units)
        assert current.details is not None
        writer = Archive.open(
            self.root,
            scope=SCOPE,
            readonly=False,
            access=SourceAccess(SCOPE, "model", ("ordinary", "medical"), local_retention=True),
        )
        later = []
        for version, sensitivity in (("v2", "medical"), ("v3", "ordinary")):
            later.append(
                writer.publish(
                    payload=payload,
                    metadata=replace(
                        current,
                        source_version=version,
                        details=replace(current.details, sensitivity=sensitivity),
                    ),
                    units=units,
                )
            )
        for operation, audience in (
            ("inspect", "model"),
            ("evidence", "model"),
            ("owner_view", "owner"),
            ("owner_preview", "owner"),
        ):
            reader = Archive.open(
                self.root,
                scope=SCOPE,
                access=SourceAccess(SCOPE, audience, ("ordinary",), local_retention=True),
            )
            with self.subTest(operation=operation):
                excerpt = reader.read(first.ref, operation=operation)
                self.assertEqual(excerpt.units, units)
                self.assertEqual(excerpt.later_versions, (later[1].ref,))
                self.assertEqual(
                    tuple(item.ref for item in excerpt.later_observations), (later[1].ref,)
                )
                self.assertEqual(excerpt.remaining_versions, 1)

    def test_read_loads_selected_body_once_with_and_without_later_history(self) -> None:
        from tedd_knowledge_graph import source_files

        for wire_format in (1, 2, 3):
            for has_later in (False, True):
                current, payload, units, reuse = wire_capture(
                    wire_format, f"body-count-{wire_format}-{has_later}"
                )
                first = self.archive.publish(
                    payload=payload, metadata=current, units=units, reuse_messages=reuse
                )
                if has_later:
                    self.archive.publish(
                        payload=payload,
                        metadata=replace(current, interaction_id="later"),
                        units=units,
                        reuse_messages=reuse,
                    )
                with self.subTest(wire_format=wire_format, has_later=has_later):
                    with mock.patch.object(
                        source_files, "read_bytes", wraps=source_files.read_bytes
                    ) as reads:
                        excerpt = self.archive.read(first.ref)
                    self.assertEqual(excerpt.units, units)
                    self.assertEqual(len(excerpt.later_versions), int(has_later))
                    body_reads = [
                        call.args[0]
                        for call in reads.call_args_list
                        if call.args[0].parent.name in ("blobs", "readings")
                    ]
                    # Recipe chat uses one message blob and a reconstruction recipe;
                    # other formats use one original and a saved reading.
                    self.assertCountEqual(
                        [path.parent.name for path in body_reads], ["blobs", "readings"]
                    )

    def test_historical_retained_copy_survives_unavailable_later_provider_access(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        current, payload, units, _ = wire_capture(3, "history")
        assert current.details is not None
        current = replace(current, details=replace(current.details, provider="provider"))
        status = "allowed"
        checked: list[tuple[str, str, str]] = []

        def provide(
            meta: SourceMetadata, sha: str, operation: str, audience: str
        ) -> AccessDecision:
            checked.append((meta.source_version, operation, audience))
            return AccessDecision(access_binding(meta, sha), operation, audience, status)

        writer = Archive.open(
            self.root,
            scope=SCOPE,
            readonly=False,
            access=SourceAccess(SCOPE, "model", ("ordinary",), provider_check=provide),
        )
        first = writer.publish(payload=payload, metadata=current, units=units)
        assert current.details is not None
        second = writer.publish(
            payload=payload,
            metadata=replace(
                current,
                source_version="v2",
                details=replace(current.details, retention="access_dependent"),
            ),
            units=units,
        )
        for status in ("denied", "unknown", "allowed"):
            for operation, audience in (
                ("inspect", "model"),
                ("evidence", "model"),
                ("owner_view", "owner"),
            ):
                checked.clear()
                reader = Archive.open(
                    self.root,
                    scope=SCOPE,
                    access=SourceAccess(
                        SCOPE,
                        audience,
                        ("ordinary",),
                        retained_bindings=(access_binding(current, digest(payload)),),
                        provider_check=provide,
                    ),
                )
                with self.subTest(status=status, operation=operation):
                    excerpt = reader.read(first.ref, operation=operation)
                    self.assertEqual(excerpt.units, units)
                    self.assertEqual(
                        excerpt.later_versions, (second.ref,) if status == "allowed" else ()
                    )
                    self.assertEqual(excerpt.remaining_versions, 0 if status == "allowed" else 1)
                    self.assertEqual(checked, [("v2", operation, audience)])

    def test_local_denial_during_later_provider_check_blocks_historical_release(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        current, payload, units, _ = wire_capture(3, "history")
        first = self.archive.publish(payload=payload, metadata=current, units=units)
        assert current.details is not None
        self.archive.publish(
            payload=payload,
            metadata=replace(current, source_version="v2"),
            units=units,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:

            def provide(
                meta: SourceMetadata, sha: str, operation: str, audience: str
            ) -> AccessDecision:
                # A separate handle can commit local denial while the host check
                # is in progress: no provider callback may hold the archive lock.
                pool.submit(self.archive.deny, first.ref).result(timeout=10)
                return AccessDecision(access_binding(meta, sha), operation, audience, "allowed")

            reader = Archive.open(
                self.root,
                scope=SCOPE,
                access=SourceAccess(
                    SCOPE,
                    "model",
                    ("ordinary",),
                    retained_bindings=(access_binding(current, digest(payload)),),
                    provider_check=provide,
                ),
            )
            with self.assertRaises(SourceArchiveError) as denied:
                reader.read(first.ref)
            self.assertEqual(denied.exception.code, "scope_denied")

    def test_new_read_exposes_capture_and_source_times_without_inventing_revision_change(
        self,
    ) -> None:
        first = replace(
            metadata(),
            kind="text",
            capture_method="local_file",
            observed_at="2026-09-04T10:00:00Z",
            details=SourceDetails(
                "local",
                "owner",
                "file",
                "original",
                "reader:1",
                source_time="2026-09-01T10:00:00Z",
                sensitivity="ordinary",
            ),
        )
        saved = self.archive.publish(payload=b"Neutral", metadata=first, units=self.units)
        assert first.details is not None
        next_meta = replace(
            first, interaction_id="another", details=replace(first.details, reader_id="reader:2")
        )
        self.archive.publish(payload=b"Neutral", metadata=next_meta, units=self.units)
        excerpt = self.archive.read(saved.ref)
        self.assertEqual(excerpt.observed_at, "2026-09-04T10:00:00Z")
        self.assertEqual(excerpt.source_time, "2026-09-01T10:00:00Z")
        self.assertEqual(excerpt.reader_id, "reader:1")
        self.assertEqual(excerpt.later_observations[0].relationship, "new_reading")

    def test_explicit_synced_input_is_read_only_and_does_not_relax_archive_privacy(self) -> None:
        from tedd_knowledge_graph import source_files

        shared = self.parent / "OneDrive"
        shared.mkdir()
        original = shared / "approved.json"
        original.write_bytes(self.payload)
        before = original.stat()
        self.assertEqual(source_files.read_input(original, 1024), self.payload)
        self.assertEqual(original.stat().st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(SourceArchiveError):
            source_files.read_input(original, 2)
        with self.assertRaises(SourceArchiveError):
            source_files.read_input(shared, 1024)
        with self.assertRaises(SourceArchiveError):
            source_files.private_root(shared / "sources", create=True)

    @unittest.skipIf(os.name == "nt", "POSIX metadata-change timestamps")
    def test_shared_input_refuses_same_size_write_with_restored_mtime(self) -> None:
        from tedd_knowledge_graph import source_files

        original = self.parent / "shared-input.txt"
        original.write_bytes(b"First synthetic wording.")
        before = original.stat()
        real_fstat = os.fstat
        observations = 0

        def change_after_read(fd: int) -> os.stat_result:
            nonlocal observations
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
                observations += 1
                # The ancestor guard, opened snapshot, then post-read check.
                if observations == 3:
                    # Advance the filesystem clock; the fstat boundary, not this
                    # delay, fixes the writer/read interleaving deterministically.
                    time.sleep(0.02)
                    original.write_bytes(b"Other synthetic wording.")
                    os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
                    info = real_fstat(fd)
                    self.assertEqual(info.st_size, before.st_size)
                    self.assertEqual(info.st_mtime_ns, before.st_mtime_ns)
                    self.assertNotEqual(info.st_ctime_ns, before.st_ctime_ns)
            return info

        with (
            mock.patch.object(source_files.os, "fstat", side_effect=change_after_read),
            self.assertRaisesRegex(SourceArchiveError, "source_corrupt"),
        ):
            source_files.read_input(original, 1024)

    def test_exact_bytes_replay_and_committed_source_without_graph(self) -> None:
        first = self.publish()
        repeated = self.publish()
        self.assertEqual(repeated.ref, first.ref)
        self.assertEqual(repeated.status, "duplicate")
        self.assertEqual(repeated.new_bytes, 0)
        self.assertEqual(self.archive.original(first.ref), self.payload)
        self.assertEqual(self.archive.read(first.ref).units, self.units)
        self.assertEqual(self.archive.lookup("case"), (first.ref,))

    def test_duplicate_capture_rejects_invalid_scope_marker_without_mutation(self) -> None:
        from tedd_knowledge_graph import source_files

        first = self.publish()
        marker = self.archive.scoped / "scope.json"
        original = marker.read_bytes()
        wrong_scope = replace(SCOPE, subject_ids=("entity_other",))
        for label, value in (
            ("corrupt", b"not JSON"),
            ("wrong scope", encode(asdict(wrong_scope))),
            ("missing", None),
        ):
            with self.subTest(marker=label):
                if value is None:
                    marker.unlink()
                else:
                    source_files.write_bytes(marker, value, overwrite=True)
                before = {
                    path.relative_to(self.root): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                try:
                    with self.assertRaises(SourceArchiveError):
                        self.publish()
                    self.assertEqual(
                        {
                            path.relative_to(self.root): path.read_bytes()
                            for path in self.root.rglob("*")
                            if path.is_file()
                        },
                        before,
                    )
                finally:
                    source_files.write_bytes(marker, original, overwrite=True)
        replay = self.publish()
        self.assertEqual(replay.status, "duplicate")
        self.assertEqual(replay.ref, first.ref)

    def test_denial_retry_must_sync_before_success_or_erasure(self) -> None:
        from tedd_knowledge_graph import source_files

        receipt = self.publish()
        real_sync = source_files.fsync_directory
        denial_directory = self.archive.scoped / "denials"

        def fail_denial_sync(path: Path) -> None:
            if path == denial_directory:
                raise OSError(5, "synthetic denial durability failure")
            real_sync(path)

        with mock.patch.object(source_files, "fsync_directory", side_effect=fail_denial_sync):
            for _ in range(2):
                with self.assertRaises(SourceArchiveError):
                    self.archive.forget(receipt.ref)
                self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 1)
        with mock.patch.object(source_files, "fsync_directory", wraps=real_sync) as synced:
            self.archive.forget(receipt.ref)
        self.assertIn(mock.call(denial_directory), synced.call_args_list)
        self.assertEqual(list((self.archive.scoped / "blobs").iterdir()), [])

    def test_scope_capacity_refuses_new_source_without_disabling_existing_sources(self) -> None:
        from tedd_knowledge_graph import source_archive

        with mock.patch.object(source_archive, "MAX_HISTORY", 2):
            first = self.publish()
            self.archive.publish(
                payload=self.payload, metadata=metadata(source_id="other"), units=self.units
            )
            before = set(self.root.rglob("*"))
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.archive.publish(
                    payload=self.payload, metadata=metadata(source_id="third"), units=self.units
                )
            self.assertEqual(set(self.root.rglob("*")), before)
            self.assertEqual(self.publish().status, "duplicate")
            self.assertEqual(self.archive.read(first.ref).units, self.units)
            self.archive.forget(first.ref)
            self.archive.recover()

    def test_global_capacity_accounts_for_all_new_publication_files(self) -> None:
        from tedd_knowledge_graph import source_archive

        first = self.publish()
        used_files = len(self.archive._all_files())
        with mock.patch.object(source_archive, "MAX_FILES", used_files + 2):
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.archive.publish(
                    payload=b'{"text":"Another original."}',
                    metadata=metadata("v2"),
                    units=(SourceUnit("u1", "Another reading.", 1),),
                )
            self.assertEqual(self.archive.read(first.ref).units, self.units)
            self.archive.forget(first.ref)
            self.archive.recover()

    def test_large_denial_remains_readable_and_preserves_unrelated_source(self) -> None:
        from tedd_knowledge_graph import source_archive

        unrelated = self.archive.publish(
            payload=self.payload, metadata=metadata(source_id="unrelated"), units=self.units
        )
        receipts = [
            self.archive.publish(
                payload=encode({"text": str(index)}),
                metadata=metadata(f"v{index}"),
                units=(SourceUnit("u1", f"Neutral version {index}", 1),),
            )
            for index in range(6)
        ]
        with mock.patch.object(source_archive, "MANIFEST_BYTES", 1100):
            self.archive.forget(receipts[0].ref)
            self.assertEqual(self.archive.read(unrelated.ref).units, self.units)
            self.archive.recover()
            for receipt in receipts:
                with self.assertRaises(SourceArchiveError):
                    self.archive.read(receipt.ref)

    def test_backup_inventory_larger_than_source_manifest_limit_restores(self) -> None:
        from tedd_knowledge_graph import source_archive

        receipts = [
            self.archive.publish(
                payload=self.payload, metadata=metadata(source_id=f"case-{index}"), units=self.units
            )
            for index in range(3)
        ]
        with mock.patch.object(source_archive, "MANIFEST_BYTES", 1600):
            destination = self.parent / "large-inventory"
            self.archive.backup(destination)
            self.assertGreater((destination / "backup.json").stat().st_size, 1600)
            restored = source_archive.Archive.restore(
                destination, self.parent / "restored-inventory", current=self.archive
            )
            self.assertEqual(restored.original(receipts[0].ref), self.payload)

    def test_backup_inventory_bound_is_checked_before_destination_creation(self) -> None:
        from tedd_knowledge_graph import source_archive

        self.publish()
        destination = self.parent / "oversized-inventory"
        with mock.patch.object(source_archive, "BACKUP_INDEX_BYTES", 100, create=True):
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.archive.backup(destination)
        self.assertFalse(destination.exists())

    def test_new_private_root_requires_parent_sync_before_success(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive

        backup = self.parent / "sync-source-backup"
        self.archive.backup(backup)
        real_sync = source_files.fsync_directory

        def refuse_parent(path: Path) -> None:
            if path == self.parent:
                raise OSError("synthetic root-parent sync failure")
            real_sync(path)

        for operation in ("initialize", "backup", "restore"):
            destination = self.parent / f"unsynced-{operation}"
            with self.subTest(operation=operation):
                with (
                    mock.patch.object(source_files, "fsync_directory", side_effect=refuse_parent),
                    self.assertRaisesRegex(SourceArchiveError, "source_unavailable"),
                ):
                    if operation == "initialize":
                        Archive.initialize(destination, scope=SCOPE)
                    elif operation == "backup":
                        self.archive.backup(destination)
                    else:
                        Archive.restore(backup, destination, current=self.archive)
                self.assertFalse((destination / "archive.json").exists())

    def test_explicit_recovery_completes_interrupted_scope_directories(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive

        real_directory = source_files.directory
        for boundary in ("scope", "blobs", "readings", "versions", "heads", "pending", "denials"):
            with self.subTest(boundary=boundary):
                archive = Archive.initialize(self.parent / f"interrupted-{boundary}", scope=SCOPE)
                saved = archive.publish(payload=self.payload, metadata=metadata(), units=self.units)
                scope = replace(SCOPE, subject_ids=("entity_" + boundary,))
                other = Archive.open(archive.root, scope=scope, readonly=False)
                target = other.scoped if boundary == "scope" else other.scoped / boundary

                def interrupt_directory(path: Path, target: Path = target) -> None:
                    real_directory(path)
                    if path == target:
                        raise OSError("synthetic scope-creation interruption")

                with (
                    mock.patch.object(source_files, "directory", side_effect=interrupt_directory),
                    self.assertRaisesRegex(SourceArchiveError, "source_unavailable"),
                ):
                    other.publish(
                        payload=self.payload,
                        metadata=replace(metadata(), scope=scope),
                        units=self.units,
                    )
                reopened = Archive.open(archive.root, scope=scope, readonly=False)
                try:
                    reopened.recover()
                except SourceArchiveError as exc:
                    self.fail(f"Explicit recovery could not repair {boundary}: {exc.code}")
                recovered = reopened.publish(
                    payload=self.payload,
                    metadata=replace(metadata(), scope=scope),
                    units=self.units,
                )
                self.assertEqual(reopened.original(recovered.ref), self.payload)
                self.assertEqual(archive.original(saved.ref), self.payload)
                archive.backup(self.parent / f"recovered-scopes-backup-{boundary}")

    def test_recovery_does_not_enroll_a_missing_scope(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        other = Archive.open(
            self.root, scope=replace(SCOPE, subject_ids=("entity_other",)), readonly=False
        )
        before = set(self.root.rglob("*"))
        other.recover()
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_recovery_cannot_replace_missing_committed_scope_state(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        for missing in ("denials", "versions", "scope.json"):
            with self.subTest(missing=missing):
                archive = Archive.initialize(self.parent / f"missing-{missing}", scope=SCOPE)
                saved = archive.publish(payload=self.payload, metadata=metadata(), units=self.units)
                archive.restrict(saved.ref)
                (archive.scoped / missing).rename(self.parent / f"held-{missing}")
                before = {
                    path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()
                }
                with self.assertRaises(SourceArchiveError):
                    archive.recover()
                self.assertEqual(
                    {path: path.read_bytes() for path in archive.root.rglob("*") if path.is_file()},
                    before,
                )
                with self.assertRaises(SourceArchiveError):
                    archive.read(saved.ref)

    def test_backup_file_count_must_fit_restore_before_copying(self) -> None:
        from tedd_knowledge_graph import source_archive

        self.publish()
        used = len(self.archive._all_files())
        destination = self.parent / "excess-files-backup"
        with (
            mock.patch.object(source_archive, "MAX_FILES", used - 1),
            mock.patch.object(source_archive, "MAINTENANCE_FILE_RESERVE", 0),
        ):
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.archive.backup(destination)
        self.assertFalse(destination.exists())

    def test_maintenance_exhaustion_refuses_new_marker_without_erasing_content(self) -> None:
        from tedd_knowledge_graph import source_archive

        first = self.publish()
        other = self.archive.publish(
            payload=self.payload, metadata=metadata(source_id="other"), units=self.units
        )
        self.archive.deny(first.ref)
        before = set(self.root.rglob("*"))
        used = len(self.archive._all_files())
        with (
            mock.patch.object(source_archive, "MAX_FILES", used - 1),
            mock.patch.object(source_archive, "MAINTENANCE_FILE_RESERVE", 1),
        ):
            with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
                self.archive.forget(other.ref)
            self.assertEqual(set(self.root.rglob("*")), before)
            self.assertEqual(self.archive.read(other.ref).units, self.units)
            self.archive.deny(first.ref)
            self.archive.recover()
            self.assertEqual(self.archive.read(other.ref).units, self.units)

    def test_forget_denies_before_pruning_link_publication_residue(self) -> None:
        receipt = self.publish()
        blob = next((self.archive.scoped / "blobs").iterdir())
        stage = blob.parent / ".stage-neutral"
        os.link(blob, stage)
        self.archive.forget(receipt.ref)
        self.assertFalse(blob.exists())
        self.assertFalse(stage.exists())
        with self.assertRaises(SourceArchiveError):
            self.archive.read(receipt.ref)

    def test_denial_keeps_content_free_graph_cleanup_binding_after_recovery(self) -> None:
        first = self.publish()
        second = self.publish("v2")
        binding = "source_" + "1" * 20
        self.archive.deny(first.ref, graph_records=(binding,))
        with self.assertRaises(SourceArchiveError):
            self.archive.read(second.ref)
        self.archive.recover()
        manifests, records = self.archive.forget_identity(first.ref)
        self.assertEqual(set(manifests), {first.ref.manifest_sha256, second.ref.manifest_sha256})
        self.assertEqual(records, (binding,))
        self.assertEqual(list((self.archive.scoped / "versions").iterdir()), [])

    def test_interrupted_backup_cannot_be_opened_as_an_archive(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        destination = self.parent / "interrupted-backup"
        original = source_files.write_bytes

        def fail_completion(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
            if path == destination / "backup.json" and b'"files"' in payload:
                raise OSError("injected completion failure")
            original(path, payload, overwrite=overwrite)

        with mock.patch.object(source_files, "write_bytes", side_effect=fail_completion):
            with self.assertRaises((OSError, SourceArchiveError)):
                self.archive.backup(destination)
        with self.assertRaises(SourceArchiveError):
            Archive.open(destination, scope=SCOPE)

    def test_failed_restore_validation_leaves_an_inert_destination(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        backup, destination = self.parent / "backup", self.parent / "failed-restore"
        self.archive.backup(backup)
        with mock.patch.object(Archive, "recover", side_effect=SourceArchiveError()):
            with self.assertRaises(SourceArchiveError):
                Archive.restore(backup, destination, current=self.archive)
        with self.assertRaises(SourceArchiveError):
            Archive.open(destination, scope=SCOPE)

    def test_restore_retains_exclusion_in_a_scope_added_after_the_backup(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        backup, destination = self.parent / "older-backup", self.parent / "restored"
        self.archive.backup(backup)
        other_scope = replace(SCOPE, subject_ids=("entity_other",))
        other = Archive.open(self.root, scope=other_scope, readonly=False)
        other_metadata = replace(metadata(), scope=other_scope, source_id="other-case")
        other_ref = other.publish(
            payload=self.payload, metadata=other_metadata, units=self.units
        ).ref
        other.forget(other_ref)
        Archive.restore(backup, destination, current=self.archive)
        restored = Archive.open(destination, scope=other_scope, readonly=False)
        with self.assertRaises(SourceArchiveError):
            restored.publish(payload=self.payload, metadata=other_metadata, units=self.units)

    def test_forgetting_does_not_require_healthy_original_bytes_or_a_complete_head(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_contracts import digest

        receipt = self.publish()
        blob = next((self.archive.scoped / "blobs").iterdir())
        blob.write_bytes(b"corrupt original")
        source = digest(encode(["source", "case"]))
        source_files.write_bytes(
            self.archive.scoped / "pending" / source, encode({"next": "0" * 64})
        )
        self.archive.forget(receipt.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(receipt.ref)
        self.assertFalse(blob.exists())
        self.assertFalse((self.archive.scoped / "pending" / source).exists())

    def test_recovery_expands_an_interrupted_denial_to_every_source_version(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        for selected in (0, 1):
            with self.subTest(selected=selected):
                archive = Archive.initialize(
                    self.parent / f"interrupted-forget-{selected}", scope=SCOPE
                )
                receipts = [
                    archive.publish(
                        payload=encode({"version": version}),
                        metadata=metadata(f"v{version}"),
                        units=self.units,
                    )
                    for version in (1, 2)
                ]
                with mock.patch.object(archive, "_prune_staging", side_effect=SourceArchiveError()):
                    with self.assertRaises(SourceArchiveError):
                        archive.forget(receipts[selected].ref)
                archive.recover()
                self.assertEqual(tuple((archive.scoped / "versions").iterdir()), ())
                for version in (1, 2):
                    with self.assertRaises(SourceArchiveError):
                        archive.publish(
                            payload=encode({"version": version}),
                            metadata=metadata(source_id="renamed"),
                            units=self.units,
                        )

    def test_recovery_handles_link_before_unlink_crash_without_accepting_external_links(
        self,
    ) -> None:
        receipt = self.publish()
        blob = next((self.archive.scoped / "blobs").iterdir())
        stage = blob.parent / ".stage-neutral"
        os.link(blob, stage)
        self.assertEqual(blob.stat().st_nlink, 2)
        self.archive.recover()
        self.assertFalse(stage.exists())
        self.assertEqual(self.archive.original(receipt.ref), self.payload)
        outside = self.parent / "external-link"
        os.link(blob, outside)
        with self.assertRaises(SourceArchiveError):
            self.archive.recover()
        self.assertTrue(outside.exists())

    def test_same_version_cannot_replace_competing_content(self) -> None:
        first = self.publish()
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(payload=b"other", metadata=metadata(), units=self.units)
        self.assertEqual(self.archive.original(first.ref), self.payload)

    def test_versions_are_distinct_and_later_context_is_visible(self) -> None:
        first, second = self.publish(), self.publish("v2")
        self.assertNotEqual(first.ref, second.ref)
        self.assertEqual(self.archive.read(first.ref).later_versions, (second.ref,))
        self.assertEqual(self.archive.original(first.ref), self.payload)

    def test_anchor_read_reports_earlier_context_and_neighbors(self) -> None:
        units = tuple(SourceUnit(f"u{i}", f"Neutral message {i}.", i) for i in range(1, 5))
        ref = self.archive.publish(payload=self.payload, metadata=metadata(), units=units).ref
        anchored = replace(ref, anchor_id="u3")
        direct = self.archive.read(anchored)
        self.assertEqual(direct.previous_units, 2)
        nearby = self.archive.read(anchored, neighbors=True)
        self.assertEqual(nearby.units[0], units[1])
        self.assertEqual(nearby.previous_units, 1)
        self.assertEqual(nearby.remaining_units, 0)

    def test_pdf_page_input_requires_exact_preserved_page_and_live_reference(self) -> None:
        with self.assertRaises(SourceArchiveError):
            self.archive.pdf_page(self.publish().ref, 1)
        pdf = b"%PDF-1.7 neutral synthetic input"
        meta = replace(metadata(), source_id="manual", kind="pdf", capture_method="pdf_original")
        ref = self.archive.publish(
            payload=pdf,
            metadata=meta,
            units=(SourceUnit("u1", "Image needs visual checking.", 1, kind="image_page"),),
        ).ref
        original, page_ref = self.archive.pdf_page(ref, 1)
        self.assertEqual(original, pdf)
        self.assertEqual(page_ref.anchor_id, "u1")
        with self.assertRaises(SourceArchiveError):
            self.archive.pdf_page(ref, 2)
        self.archive.forget(ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.pdf_page(ref, 1)

    def test_metadata_discovery_is_bounded_scoped_and_exact_version_filtered(self) -> None:
        refs = [self.publish(f"v{i}").ref for i in range(1, 8)]
        first = self.archive.discover(query="case", limit=3)
        self.assertEqual(len(first.sources), 3)
        self.assertTrue(first.more_metadata)
        second = self.archive.discover(
            query="case", offset=first.next_offset, limit=5, snapshot_sha256=first.snapshot_sha256
        )
        self.assertEqual(len(second.sources), 4)
        exact = self.archive.discover(source_id="case", source_version="v2")
        self.assertEqual(exact.sources[0].source_uri, refs[1].as_uri())
        self.assertEqual(
            self.archive.discover(source_id="case", source_version="missing").sources, ()
        )
        self.assertNotIn(b"Not approved", encode(asdict(exact)))
        self.archive.forget(refs[0])
        self.assertEqual(self.archive.discover(query="case").sources, ())
        with self.assertRaises(SourceArchiveError):
            self.archive.discover(offset=first.next_offset, snapshot_sha256=first.snapshot_sha256)

    def test_new_interaction_with_unchanged_external_version_preserves_its_own_origin(self) -> None:
        first = self.publish()
        second_metadata = replace(
            metadata(), interaction_id="second-interaction", captured_at="2026-09-05T13:00:00Z"
        )
        second = self.archive.publish(
            payload=self.payload, metadata=second_metadata, units=self.units
        )
        self.assertNotEqual(first.ref, second.ref)
        self.assertEqual(len(list((self.archive.scoped / "blobs").iterdir())), 1)
        self.assertEqual(self.archive.lookup("case"), (first.ref, second.ref))
        replay = self.archive.publish(
            payload=self.payload, metadata=second_metadata, units=self.units
        )
        self.assertEqual(replay.status, "duplicate")
        self.assertEqual(replay.ref, second.ref)

    def test_wrong_scope_store_generation_and_archive_are_refused(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        first = self.publish()
        for scope in (
            replace(SCOPE, subject_ids=("other",)),
            replace(SCOPE, store_id="other"),
            replace(SCOPE, generation="other"),
        ):
            with self.subTest(scope=scope.key), self.assertRaises(SourceArchiveError):
                Archive.open(self.root, scope=scope).read(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(replace(first.ref, archive_id="f" * 32))
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(
                payload=self.payload,
                metadata=replace(metadata(), scope=replace(SCOPE, subject_ids=("other",))),
                units=self.units,
            )

    def test_budget_never_silently_truncates_a_unit(self) -> None:
        first = self.publish()
        excerpt = self.archive.read(first.ref, max_chars=5)
        self.assertEqual(excerpt.units, ())
        self.assertEqual(excerpt.remaining_units, 1)
        self.assertEqual(self.archive.original(first.ref), self.payload)

    def test_source_lookup_reads_only_metadata_before_source_selection(self) -> None:
        from tedd_knowledge_graph import source_files

        first = self.publish()
        real = source_files.read_bytes

        def metadata_only(path: Path, maximum: int) -> bytes:
            self.assertNotIn(path.parent.name, ("blobs", "readings"))
            return real(path, maximum)

        with mock.patch.object(source_files, "read_bytes", side_effect=metadata_only):
            self.assertEqual(self.archive.lookup("case"), (first.ref,))

    def test_corrupt_payload_and_manifest_are_not_returned(self) -> None:
        first = self.publish()
        blob = next((self.root / "scopes" / SCOPE.key / "blobs").iterdir())
        blob.write_bytes(b"corrupt")
        with self.assertRaises(SourceArchiveError) as raised:
            self.archive.read(first.ref)
        self.assertEqual(raised.exception.code, "source_corrupt")

    def test_missing_head_reports_incomplete_until_explicit_recovery(self) -> None:
        first = self.publish()
        head = next((self.root / "scopes" / SCOPE.key / "heads").iterdir())
        head.unlink()
        with self.assertRaises(SourceArchiveError) as raised:
            self.archive.read(first.ref)
        self.assertEqual(raised.exception.code, "capture_incomplete")
        self.archive.recover()
        self.assertEqual(self.archive.read(first.ref).units, self.units)

    def test_publication_failure_has_no_successful_source_and_cleans_staging(self) -> None:
        from tedd_knowledge_graph import source_files

        with mock.patch.object(
            source_files, "publish_staged_file", side_effect=OSError("synthetic storage failure")
        ):
            with self.assertRaises(SourceArchiveError):
                self.publish()
        self.assertEqual(self.archive.lookup("case"), ())
        self.assertEqual(list(self.root.rglob(".stage-*")), [])

    def test_quota_failure_preserves_history(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        small = Archive.initialize(self.parent / "small", scope=SCOPE, max_bytes=2048)
        with self.assertRaises(SourceArchiveError) as raised:
            small.publish(
                payload=encode({"text": "x" * 2048}), metadata=metadata(), units=self.units
            )
        self.assertEqual(raised.exception.code, "limit_exceeded")
        self.assertEqual(small.lookup("case"), ())

    def test_forgetting_denies_before_failed_erasure_and_rejects_recapture(self) -> None:
        first = self.publish()
        with mock.patch.object(Path, "unlink", side_effect=OSError("synthetic erase failure")):
            with self.assertRaises(SourceArchiveError):
                self.archive.forget(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(first.ref)
        with self.assertRaises(SourceArchiveError):
            self.publish("v2")
        with self.assertRaises(SourceArchiveError):
            self.archive.publish(
                payload=self.payload, metadata=metadata(source_id="renamed"), units=self.units
            )
        self.archive.forget(first.ref)
        self.assertEqual(list((self.root / "scopes" / SCOPE.key / "blobs").iterdir()), [])
        self.assertEqual(self.archive.lookup("case"), ())

    def test_backup_restore_cannot_discard_current_forgetting(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        first = self.publish()
        backup = self.parent / "backup"
        self.archive.backup(backup)
        self.assertTrue((backup / "scopes" / SCOPE.key / "pending").is_dir())
        self.archive.forget(first.ref)
        restored = Archive.restore(backup, self.parent / "restored", current=self.archive)
        with self.assertRaises(SourceArchiveError):
            restored.read(first.ref)
        with self.assertRaises(SourceArchiveError):
            restored.publish(payload=self.payload, metadata=metadata("v2"), units=self.units)

    def test_private_permissions_and_git_root_refusal(self) -> None:
        from platform_support import mode_has_any, owner_is_current_user
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        for path in (self.root, *self.root.rglob("*")):
            self.assertTrue(owner_is_current_user(path))
            self.assertFalse(mode_has_any(path, 0o077))
        fake_repo = self.parent / "repo"
        fake_repo.mkdir()
        (fake_repo / ".git").mkdir()
        with self.assertRaises(SourceArchiveError):
            Archive.initialize(fake_repo / "sources", scope=SCOPE)

    def test_readonly_instance_never_creates_or_mutates_archive_entries(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        first = self.publish()
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        readonly = Archive.open(self.root, scope=SCOPE)
        readonly.read(first.ref)
        with self.assertRaises(SourceArchiveError):
            readonly.publish(payload=self.payload, metadata=metadata("v2"), units=self.units)
        after = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(after, before)

    def test_stale_head_cannot_hide_a_later_committed_version(self) -> None:
        self.publish()
        head = next((self.archive.scoped / "heads").iterdir())
        previous = head.read_bytes()
        self.publish("v2")
        head.write_bytes(previous)
        with self.assertRaises(SourceArchiveError) as raised:
            self.archive.lookup("case")
        self.assertEqual(raised.exception.code, "capture_incomplete")
        self.archive.recover()
        self.assertEqual(len(self.archive.lookup("case")), 2)

    def test_raw_chat_credentials_are_refused_even_with_a_safe_reading(self) -> None:
        for payload in (
            encode({"password": "synthetic-value"}),
            encode({"api_session": "synthetic-session"}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(SourceArchiveError) as raised:
                    self.archive.publish(payload=payload, metadata=metadata(), units=self.units)
                self.assertEqual(raised.exception.code, "privacy_rejected")
        self.assertEqual(self.archive.lookup("case"), ())

    def test_cloud_vault_nested_archive_and_network_roots_are_refused(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        for marker in ("OneDrive", "Dropbox", "vault"):
            parent = self.parent / marker
            parent.mkdir()
            set_private_path_mode(parent, 0o700)
            if marker == "vault":
                (parent / ".obsidian").mkdir()
            with self.subTest(marker=marker), self.assertRaises(SourceArchiveError):
                Archive.initialize(parent / "sources", scope=SCOPE)
        with self.assertRaises(SourceArchiveError):
            Archive.initialize(self.root / "nested", scope=SCOPE)
        from tedd_knowledge_graph import source_files

        with (
            mock.patch.object(source_files, "inspect_store_path", return_value={"safe": False}),
            self.assertRaises(SourceArchiveError),
        ):
            Archive.initialize(self.parent / "network", scope=SCOPE)

    def test_hard_link_and_ancestor_reparse_swap_refuse_content(self) -> None:
        from tedd_knowledge_graph import source_files

        first = self.publish()
        blob = next((self.archive.scoped / "blobs").iterdir())
        other = self.parent / "linked"
        os.link(blob, other)
        try:
            with self.assertRaises(SourceArchiveError):
                self.archive.read(first.ref)
        finally:
            other.unlink()
        actual = source_files.is_link_or_reparse_point
        with mock.patch.object(
            source_files,
            "is_link_or_reparse_point",
            side_effect=lambda path: path == blob.parent or actual(path),
        ):
            with self.assertRaises(SourceArchiveError):
                self.archive.read(first.ref)

    def test_every_publication_phase_fails_closed_and_can_recover(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_archive import Archive

        real = source_files.publish_staged_file
        for phase in range(1, 6):
            archive = Archive.initialize(self.parent / f"phase-{phase}", scope=SCOPE)
            calls = 0

            def fail(
                source: Path, target: Path, *, overwrite: bool, fail_at: int = phase
            ) -> tuple[int, int]:
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    raise OSError("synthetic disk failure")
                return real(source, target, overwrite=overwrite)

            with (
                self.subTest(phase=phase),
                mock.patch.object(source_files, "publish_staged_file", side_effect=fail),
            ):
                with self.assertRaises(SourceArchiveError):
                    archive.publish(payload=self.payload, metadata=metadata(), units=self.units)
            self.assertFalse(list(archive.root.rglob(".stage-*")))
            archive.recover()
            found = archive.lookup("case")
            self.assertEqual(len(found), 1 if phase == 5 else 0)
            if found:
                self.assertEqual(archive.original(found[0]), self.payload)

    def test_fsync_failure_is_not_a_success_receipt(self) -> None:
        from tedd_knowledge_graph import source_files

        with mock.patch.object(source_files.os, "fsync", side_effect=OSError("synthetic fsync")):
            with self.assertRaises(SourceArchiveError):
                self.publish()
        self.assertFalse(list(self.root.rglob(".stage-*")))
        self.archive.recover()
        self.assertEqual(self.archive.lookup("case"), ())

    def test_after_publication_sync_failures_preserve_only_committed_versions(self) -> None:
        from tedd_knowledge_graph import source_archive, source_files
        from tedd_knowledge_graph.source_archive import Archive

        real = source_archive.fsync_directory
        for phase in range(1, 7):
            archive = Archive.initialize(self.parent / f"after-sync-{phase}", scope=SCOPE)
            calls = 0

            def fail(path: Path, fail_at: int = phase) -> None:
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    raise OSError(28, "synthetic full disk")
                real(path)

            with (
                self.subTest(phase=phase),
                mock.patch.object(source_archive, "fsync_directory", side_effect=fail),
                mock.patch.object(source_files, "fsync_directory", side_effect=fail),
            ):
                with self.assertRaises(SourceArchiveError):
                    archive.publish(payload=self.payload, metadata=metadata(), units=self.units)
            archive.recover()
            self.assertEqual(len(archive.lookup("case")), 1 if phase >= 4 else 0)

    def test_private_permission_failure_cleans_staging_without_a_source(self) -> None:
        from tedd_knowledge_graph import source_files

        with mock.patch.object(
            source_files,
            "set_private_fd_mode",
            side_effect=PermissionError("synthetic permissions"),
        ):
            with self.assertRaises(SourceArchiveError):
                self.publish()
        self.assertEqual(self.archive.lookup("case"), ())
        self.assertFalse(list(self.root.rglob(".stage-*")))

    def test_actual_directory_symlink_is_refused_when_the_host_allows_creation(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        link = self.parent / "linked-root"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows host does not grant symlink creation")
            raise
        try:
            with self.assertRaises(SourceArchiveError):
                Archive.open(link, scope=SCOPE)
        finally:
            link.unlink()

    def test_shared_blob_forget_retains_other_source_and_reports_shared_retention(self) -> None:
        first = self.publish()
        second = self.archive.publish(
            payload=self.payload, metadata=metadata(source_id="other"), units=self.units
        )
        result = self.archive.forget(first.ref)
        self.assertTrue(result.shared_content_retained)
        self.assertEqual(self.archive.original(second.ref), self.payload)
        with self.assertRaises(SourceArchiveError):
            self.archive.read(first.ref)
        self.archive.forget(second.ref)
        self.assertEqual(list((self.archive.scoped / "blobs").iterdir()), [])

    def test_backup_corruption_and_traversal_refused_before_restore_publication(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        backup = self.parent / "broken-backup"
        self.archive.backup(backup)
        blob = next((backup / "scopes" / SCOPE.key / "blobs").iterdir())
        blob.write_bytes(b"corrupt")
        destination = self.parent / "refused-restore"
        with self.assertRaises(SourceArchiveError):
            Archive.restore(backup, destination, current=self.archive)
        self.assertFalse(destination.exists())
        (backup / "backup.json").write_bytes(encode({"files": {"../outside": "0" * 64}}))
        with self.assertRaises(SourceArchiveError):
            Archive.restore(backup, destination, current=self.archive)
        self.assertFalse(destination.exists())

    def test_backup_is_not_an_alternate_active_archive(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        backup = self.parent / "inert-backup"
        self.archive.backup(backup)
        with self.assertRaises(SourceArchiveError):
            Archive.open(backup, scope=SCOPE)

    def test_backup_refuses_unrecognized_files_before_creating_a_destination(self) -> None:
        self.publish()
        unexpected = self.root / "unrelated"
        unexpected.write_bytes(b"neutral unrelated file")
        set_private_path_mode(unexpected, 0o600)
        with self.assertRaises(SourceArchiveError):
            self.archive.backup(self.parent / "rejected-backup")
        self.assertFalse((self.parent / "rejected-backup").exists())

    def test_backup_checks_other_scopes_and_restore_keeps_their_current_revocations(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        other_scope = replace(SCOPE, subject_ids=("other",))
        other = Archive.open(self.root, scope=other_scope, readonly=False)
        receipt = other.publish(
            payload=self.payload, metadata=replace(metadata(), scope=other_scope), units=self.units
        )
        backup = self.parent / "multi-scope-backup"
        self.archive.backup(backup)
        other.forget(receipt.ref)
        restored = Archive.restore(
            backup, self.parent / "multi-scope-restored", current=self.archive
        )
        restored_other = Archive.open(restored.root, scope=other_scope)
        with self.assertRaises(SourceArchiveError):
            restored_other.read(receipt.ref)
        self.assertEqual(list((restored_other.scoped / "blobs").iterdir()), [])

    def test_restoring_missing_inventory_entries_is_not_accepted(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        self.publish()
        backup = self.parent / "missing-inventory"
        self.archive.backup(backup)
        (backup / "backup.json").write_bytes(encode({"files": {}}))
        with self.assertRaises(SourceArchiveError):
            Archive.restore(backup, self.parent / "incomplete-restore", current=self.archive)
        self.assertFalse((self.parent / "incomplete-restore").exists())

    def test_read_started_after_denial_waits_and_returns_no_content(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive, _Denial

        first = self.publish()
        denied, release = threading.Event(), threading.Event()
        erase = self.archive._erase

        def pause(denial: _Denial) -> None:
            denied.set()
            self.assertTrue(release.wait(10))
            erase(denial)

        reader = Archive.open(self.root, scope=SCOPE)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with mock.patch.object(self.archive, "_erase", side_effect=pause):
                writer = pool.submit(self.archive.forget, first.ref)
                try:
                    self.assertTrue(denied.wait(10))
                    reading = pool.submit(reader.read, first.ref)
                finally:
                    release.set()
                writer.result(timeout=10)
                with self.assertRaises(SourceArchiveError):
                    reading.result(timeout=10)

    def test_process_duplicate_and_conflicting_publish_per_format_use_one_winner(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive

        context = multiprocessing.get_context("spawn")
        for wire_format in (1, 2, 3):
            for conflicting in (False, True):
                with self.subTest(wire_format=wire_format, conflicting=conflicting):
                    archive = Archive.initialize(
                        self.parent / f"race-{wire_format}-{conflicting}",
                        scope=SCOPE,
                        access=SourceAccess(SCOPE, "model", ("ordinary",), local_retention=True),
                    )
                    barrier = context.Barrier(2)
                    receivers, children = [], []
                    try:
                        for index in range(2):
                            receiver, sender = context.Pipe(duplex=False)
                            content = (
                                "Different neutral reading."
                                if conflicting and index
                                else "Neutral shared reading."
                            )
                            process = context.Process(
                                target=race_publish,
                                args=(str(archive.root), wire_format, content, barrier, sender),
                            )
                            process.start()
                            sender.close()
                            receivers.append(receiver)
                            children.append(process)
                        statuses = []
                        for receiver in receivers:
                            self.assertTrue(receiver.poll(20))
                            statuses.append(receiver.recv())
                        self.assertEqual(
                            sorted(statuses),
                            (
                                ["preserved", "source_corrupt"]
                                if conflicting
                                else ["duplicate", "preserved"]
                            ),
                        )
                        current = wire_capture(wire_format, "case")[0]
                        self.assertEqual(len(archive.lookup(current)), 1)
                    finally:
                        for process in children:
                            process.join(timeout=20)
                            if process.is_alive():
                                process.terminate()
                                process.join(timeout=5)
                            self.assertEqual(process.exitcode, 0)
                        for receiver in receivers:
                            receiver.close()


if __name__ == "__main__":
    unittest.main()
