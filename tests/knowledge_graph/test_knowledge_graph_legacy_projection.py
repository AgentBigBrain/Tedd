#!/usr/bin/env python3
"""Differential and hook tests for the typed legacy projection service."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS

SCRIPT = GRAPH_SCRIPTS / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_legacy_projection", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


class LegacyProjectionServiceTests(unittest.TestCase):
    def test_normal_publication_preserves_final_seam_owner_edits_and_replacements(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.legacy_projection_contracts import AtomicWrite
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        for operation in ("retire", "rewrite", "ordinary", "orphan-claim"):
            for edit in (True, False):
                with self.subTest(operation=operation, edit=edit), self.private_directory() as tmp:
                    vault = Path(tmp)
                    relative = (
                        "11 Claims/neutral-claim.md"
                        if operation == "orphan-claim"
                        else "20 Evidence/source-" + "a" * 32 + ".md"
                    )
                    body = PRESENTATION_MARKER + "\n# Neutral\n\nOriginal condition.\n"
                    self.project_with_binaries(vault, {}, {relative: body})
                    path = vault / "Tedd Knowledge" / relative
                    expected = path.read_bytes() + (b"\nOwner annotation.\n" if edit else b"")
                    replacement = vault / "owner-replacement.md"
                    kg.atomic_write(replacement, expected.decode(), mode=0o600)
                    winner: list[int] = []
                    write, unlink, mutate = (
                        kg.atomic_write,
                        Path.unlink,
                        source_files.mutate_owned_preview,
                    )

                    def compete(
                        candidate: Path,
                        *,
                        path: Path = path,
                        winner: list[int] = winner,
                        edit: bool = edit,
                        expected: bytes = expected,
                        replacement: Path = replacement,
                    ) -> None:
                        if candidate == path and not winner:
                            if edit:
                                path.write_bytes(expected)
                            else:
                                os.replace(replacement, path)
                            winner.append(path.stat().st_ino)

                    def write_gap(
                        candidate: Path,
                        content: str,
                        *,
                        mode: int = 0o600,
                        write: AtomicWrite = write,
                    ) -> None:
                        compete(candidate)
                        write(candidate, content, mode=mode)

                    def unlink_gap(
                        candidate: Path,
                        missing_ok: bool = False,
                        *,
                        unlink: Callable[[Path, bool], None] = unlink,
                    ) -> None:
                        compete(candidate)
                        unlink(candidate, missing_ok)

                    def mutate_gap(
                        candidate: Path,
                        identity: tuple[int, int],
                        expected: bytes,
                        replacement: bytes | None,
                        *,
                        mutate: Callable[
                            [Path, tuple[int, int], bytes, bytes | None], bool
                        ] = mutate,
                    ) -> bool:
                        compete(candidate)
                        return mutate(candidate, identity, expected, replacement)

                    with (
                        mock.patch.object(kg, "atomic_write", side_effect=write_gap),
                        mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink_gap),
                        mock.patch.object(
                            source_files, "mutate_owned_preview", side_effect=mutate_gap
                        ),
                    ):
                        try:
                            self.project_with_binaries(
                                vault,
                                {},
                                {relative: body + "New condition.\n"}
                                if operation == "rewrite"
                                else (None if operation == "ordinary" else {}),
                            )
                        except (OSError, ValueError):
                            pass
                    self.assertTrue(path.exists(), "publication deleted the owner winner")
                    self.assertEqual(path.read_bytes(), expected)
                    self.assertEqual([path.stat().st_ino], winner)

    def test_normal_publication_new_note_destination_is_no_clobber(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        with self.private_directory() as tmp:
            vault = Path(tmp)
            relative = "20 Evidence/source-" + "a" * 32 + ".md"
            path = vault / "Tedd Knowledge" / relative
            write, publish = kg.atomic_write, source_files.publish_staged_file
            winner: list[int] = []

            def compete(candidate: Path) -> None:
                if candidate == path and not winner:
                    write(path, "Owner destination winner.\n", mode=0o600)
                    winner.append(path.stat().st_ino)

            def write_gap(candidate: Path, content: str, *, mode: int = 0o600) -> None:
                compete(candidate)
                write(candidate, content, mode=mode)

            def publish_gap(staged: Path, candidate: Path, *, overwrite: bool) -> tuple[int, int]:
                compete(candidate)
                return publish(staged, candidate, overwrite=overwrite)

            with (
                mock.patch.object(kg, "atomic_write", side_effect=write_gap),
                mock.patch.object(source_files, "publish_staged_file", side_effect=publish_gap),
            ):
                try:
                    self.project_with_binaries(
                        vault, {}, {relative: PRESENTATION_MARKER + "\nNew.\n"}
                    )
                except (OSError, ValueError):
                    pass
            self.assertEqual(path.read_bytes(), b"Owner destination winner.\n")
            self.assertEqual([path.stat().st_ino], winner)

    def test_retained_inventory_does_not_interpret_literal_image_heading_as_metadata(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceRef
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        with self.private_directory() as tmp:
            vault = Path(tmp)
            ref = SourceRef("a" * 32, "b" * 64, "c" * 64, "u1")
            binding = hashlib.sha256(ref.as_uri().encode()).hexdigest()[:32]
            body = (
                PRESENTATION_MARKER + "\n# Neutral\\_title\n"
                f"- Exact archived reference: {ref.as_uri()}\n"
                f"- History head: {'c' * 64}\n- Linked memories: claim_neutral\n\n"
                "## Saved source text\n\n    ## Saved page image\n"
            )
            self.project_with_binaries(vault, {}, {f"20 Evidence/source-{binding}.md": body})
            inventory, retained, binaries = (
                kg._legacy_projection_service().source_preview_inventory(
                    vault, "Tedd Knowledge", "k" * 64, "root_test"
                )
            )
            self.assertFalse(inventory[1])
            self.assertEqual(retained[0].title, "Neutral_title")
            self.assertIn("    ## Saved page image", retained[0].body)
            self.assertEqual(binaries, {})

    def test_normal_publication_guard_excludes_writes_at_exact_delete_boundary(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        for binary in (False, True):
            with self.subTest(binary=binary), self.private_directory() as tmp:
                vault = Path(tmp)
                service = kg._legacy_projection_service()
                payload = b"\x89PNG\r\n\x1a\nneutral boundary"
                relative = (
                    service.binary_path("k" * 64, "root_test", payload, binding="a" * 32)
                    if binary
                    else "20 Evidence/source-" + "a" * 32 + ".md"
                )
                self.project_with_binaries(
                    vault,
                    {relative: payload} if binary else {},
                    None if binary else {relative: PRESENTATION_MARKER + "\nNeutral.\n"},
                )
                path = vault / "Tedd Knowledge" / relative
                before = path.read_bytes(), path.stat().st_ino
                delete = source_files.delete_guarded_file
                attempted: list[bool] = []

                def compete(
                    fd: int,
                    *,
                    path: Path = path,
                    attempted: list[bool] = attempted,
                    delete: Callable[[int], None] = delete,
                ) -> None:
                    attempted.append(True)
                    with self.assertRaises(OSError):
                        path.write_bytes(b"Concurrent in-place edit.")
                    delete(fd)

                with mock.patch.object(source_files, "delete_guarded_file", side_effect=compete):
                    if os.name == "nt":
                        self.project_with_binaries(vault, {})
                        self.assertFalse(path.exists())
                        self.assertTrue(attempted)
                    else:
                        with self.assertRaises(source_files.MutationGuardUnavailable):
                            self.project_with_binaries(vault, {})
                        self.assertEqual((path.read_bytes(), path.stat().st_ino), before)

    def test_normal_rewrite_preserves_post_delete_winner_and_reports_sync_failure(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        for fail_sync in (False, True):
            with self.subTest(fail_sync=fail_sync), self.private_directory() as tmp:
                vault = Path(tmp)
                relative = "20 Evidence/source-" + "a" * 32 + ".md"
                body = PRESENTATION_MARKER + "\nOriginal.\n"
                self.project_with_binaries(vault, {}, {relative: body})
                root = vault / "Tedd Knowledge"
                path = root / relative
                before = path.read_bytes(), path.stat().st_ino
                manifest = (root / kg.PROJECTION_MANIFEST).read_bytes()
                sync = source_files.fsync_directory
                attempted: list[bool] = []

                def after_delete(
                    directory: Path,
                    *,
                    path: Path = path,
                    fail_sync: bool = fail_sync,
                    attempted: list[bool] = attempted,
                    sync: Callable[[Path], None] = sync,
                ) -> None:
                    if directory == path.parent and not path.exists():
                        attempted.append(True)
                        if fail_sync:
                            raise OSError("neutral real directory-sync failure")
                        kg.atomic_write(path, "Owner post-delete winner.\n", mode=0o600)
                    sync(directory)

                with mock.patch.object(source_files, "fsync_directory", side_effect=after_delete):
                    with self.assertRaises(OSError):
                        self.project_with_binaries(vault, {}, {relative: body + "Updated.\n"})
                self.assertEqual((root / kg.PROJECTION_MANIFEST).read_bytes(), manifest)
                if os.name != "nt":
                    self.assertEqual((path.read_bytes(), path.stat().st_ino), before)
                elif fail_sync:
                    self.assertFalse(path.exists())
                    self.assertTrue(attempted)
                else:
                    self.assertEqual(path.read_bytes(), b"Owner post-delete winner.\n")
                    self.assertTrue(attempted)

    def test_preview_cleanup_preserves_boundary_replacement_and_in_place_edits(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        for rewrite in (False, True):
            for edit in (False, True):
                with (
                    self.subTest(rewrite=rewrite, edit=edit),
                    self.private_directory() as temporary,
                ):
                    vault = Path(temporary)
                    relative = (
                        "11 Claims/neutral-claim.md"
                        if rewrite
                        else "20 Evidence/source-" + "a" * 32 + ".md"
                    )
                    body = (
                        "# Neutral claim\n\n"
                        + PRESENTATION_MARKER
                        + "\n\n## Saved sources\n\n[[source-"
                        + "a" * 32
                        + "|Saved source]]\n"
                    )
                    self.project_with_binaries(vault, {}, {relative: body})
                    path = vault / "Tedd Knowledge" / relative
                    original = path.read_bytes()
                    expected = original + b"\nOwner annotation.\n" if edit else original
                    first_identity = path.stat().st_ino
                    reads = 0
                    read = source_files.read_bytes

                    def change_after_final_read(
                        candidate: Path,
                        maximum: int,
                        *,
                        path: Path = path,
                        read: Callable[[Path, int], bytes] = read,
                        edit: bool = edit,
                        expected: bytes = expected,
                    ) -> bytes:
                        nonlocal reads
                        payload = read(candidate, maximum)
                        if candidate == path:
                            reads += 1
                            if reads == 2:
                                if edit:
                                    path.write_bytes(expected)
                                else:
                                    source_files.write_bytes(path, expected, overwrite=True)
                        return payload

                    service = kg._legacy_projection_service()
                    with (
                        service.projection_lock(vault, "Tedd Knowledge"),
                        mock.patch.object(
                            source_files, "read_bytes", side_effect=change_after_final_read
                        ),
                    ):
                        result = service.clear_source_previews_locked(
                            vault, "Tedd Knowledge", "k" * 64, "root_test"
                        )
                    self.assertEqual(reads, 2)
                    self.assertTrue(path.exists(), result)
                    self.assertEqual(path.read_bytes(), expected)
                    self.assertEqual(path.stat().st_ino == first_identity, edit)
                    self.assertEqual(result["deleted_files"], 0)
                    self.assertEqual(result["rewritten_files"], 0)
                    self.assertGreater(result["preserved_files"] + result["failed_files"], 0)

    def test_preview_cleanup_reports_real_directory_sync_failure(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        with self.private_directory() as temporary:
            vault = Path(temporary)
            relative = "20 Evidence/source-" + "a" * 32 + ".md"
            self.project_with_binaries(
                vault, {}, {relative: PRESENTATION_MARKER + "\nSaved condition."}
            )
            service = kg._legacy_projection_service()
            synchronize_directory = source_files.fsync_directory

            def fail_evidence(directory: Path) -> None:
                if directory == vault / "Tedd Knowledge" / "20 Evidence":
                    raise OSError("synthetic directory I/O failure")
                synchronize_directory(directory)

            with (
                service.projection_lock(vault, "Tedd Knowledge"),
                mock.patch.object(
                    source_files,
                    "fsync_directory",
                    side_effect=fail_evidence,
                ) as synchronize,
            ):
                result = service.clear_source_previews_locked(
                    vault, "Tedd Knowledge", "k" * 64, "root_test"
                )
            if os.name == "nt":
                synchronize.assert_any_call(vault / "Tedd Knowledge" / "20 Evidence")
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(result["failed_files"], 1)
            # Even an empty directory must be re-synchronized before retry succeeds.
            with (
                service.projection_lock(vault, "Tedd Knowledge"),
                mock.patch.object(
                    source_files, "fsync_directory", wraps=synchronize_directory
                ) as retry_sync,
            ):
                retry = service.clear_source_previews_locked(
                    vault, "Tedd Knowledge", "k" * 64, "root_test"
                )
            if os.name == "nt":
                retry_sync.assert_any_call(vault / "Tedd Knowledge" / "20 Evidence")
                self.assertEqual(retry["cleanup_status"], "cleaned")
            else:
                self.assertEqual(retry["cleanup_status"], "cleanup_incomplete")

    def test_preview_link_rewrite_preserves_no_clobber_gap_winner(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        with self.private_directory() as temporary:
            vault = Path(temporary)
            relative = "11 Claims/neutral-claim.md"
            self.project_with_binaries(
                vault,
                {},
                {
                    relative: "# Neutral\n\n"
                    + PRESENTATION_MARKER
                    + "\n\n## Saved sources\n\n[[source-"
                    + "a" * 32
                    + "|Source]]"
                },
            )
            path = vault / "Tedd Knowledge" / relative
            original = path.read_bytes()
            publish = source_files.publish_staged_file

            def owner_wins(staged: Path, destination: Path, *, overwrite: bool) -> tuple[int, int]:
                self.assertEqual(destination, path)
                self.assertFalse(overwrite)
                self.assertFalse(path.exists())
                path.write_bytes(b"Owner replacement in publication gap.")
                return publish(staged, destination, overwrite=overwrite)

            service = kg._legacy_projection_service()
            with (
                service.projection_lock(vault, "Tedd Knowledge"),
                mock.patch.object(source_files, "publish_staged_file", side_effect=owner_wins),
            ):
                result = service.clear_source_previews_locked(
                    vault, "Tedd Knowledge", "k" * 64, "root_test"
                )
            self.assertEqual(
                path.read_bytes(),
                b"Owner replacement in publication gap." if os.name == "nt" else original,
            )
            self.assertEqual(result["rewritten_files"], 0)
            self.assertEqual(result["failed_files"], 1)
            self.assertEqual(result["cleanup_status"], "cleanup_incomplete")
            self.assertEqual(list(path.parent.glob(".stage-*")), [])

    def test_preview_cleanup_explicitly_refuses_unsupported_mutation_without_changes(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        with self.private_directory() as temporary:
            vault = Path(temporary)
            relative = "20 Evidence/source-" + "a" * 32 + ".md"
            self.project_with_binaries(
                vault, {}, {relative: PRESENTATION_MARKER + "\nSaved condition."}
            )
            path = vault / "Tedd Knowledge" / relative
            original = path.read_bytes()
            identity = path.stat().st_ino
            service = kg._legacy_projection_service()
            with (
                service.projection_lock(vault, "Tedd Knowledge"),
                mock.patch.object(
                    source_files,
                    "guarded_file_mutation",
                    side_effect=source_files.MutationGuardUnavailable(),
                ),
            ):
                result = service.clear_source_previews_locked(
                    vault, "Tedd Knowledge", "k" * 64, "root_test"
                )
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_ino, identity)
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(result["failed_files"], 1)
            self.assertEqual(result["guarded_mutation_unavailable_files"], 1)
            self.assertEqual(result["cleanup_status"], "cleanup_incomplete")

    def test_preview_delete_and_rewrite_hold_guard_at_final_mutation(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        for rewrite in (False, True):
            with self.subTest(rewrite=rewrite), self.private_directory() as temporary:
                vault = Path(temporary)
                relative = (
                    "11 Claims/neutral-claim.md"
                    if rewrite
                    else "20 Evidence/source-" + "a" * 32 + ".md"
                )
                body = (
                    "# Neutral\n\n"
                    + PRESENTATION_MARKER
                    + "\n\n## Saved sources\n\n[[source-"
                    + "a" * 32
                    + "|Source]]"
                )
                self.project_with_binaries(vault, {}, {relative: body})
                path = vault / "Tedd Knowledge" / relative
                original = path.read_bytes()
                editor = vault / "editor.md"
                editor.write_bytes(b"Owner replacement.")
                delete = source_files.delete_guarded_file
                attempted = False

                def attempt_edit(
                    fd: int,
                    *,
                    path: Path = path,
                    editor: Path = editor,
                    delete: Callable[[int], None] = delete,
                ) -> None:
                    nonlocal attempted
                    attempted = True
                    with self.assertRaises(OSError):
                        path.write_bytes(b"Owner in-place edit.")
                    with self.assertRaises(OSError):
                        os.replace(editor, path)
                    delete(fd)

                service = kg._legacy_projection_service()
                with (
                    service.projection_lock(vault, "Tedd Knowledge"),
                    mock.patch.object(
                        source_files, "delete_guarded_file", side_effect=attempt_edit
                    ),
                ):
                    result = service.clear_source_previews_locked(
                        vault, "Tedd Knowledge", "k" * 64, "root_test"
                    )
                self.assertEqual(editor.read_bytes(), b"Owner replacement.")
                self.assertEqual(attempted, os.name == "nt")
                if os.name == "nt":
                    self.assertEqual(result["failed_files"], 0)
                    self.assertEqual(path.exists(), rewrite)
                    if rewrite:
                        self.assertNotIn(b"## Saved sources", path.read_bytes())
                else:
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(result["guarded_mutation_unavailable_files"], 1)

    def project_with_binaries(
        self,
        vault: Path,
        binaries: Mapping[str, bytes],
        source_notes: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        from tedd_knowledge_graph import legacy_store
        from tedd_knowledge_graph.legacy_projection import LegacyProjectionService

        service = cast(LegacyProjectionService, kg._legacy_projection_service())
        if source_notes is not None:
            original_builder = service.dependencies.build_projection_notes

            def build(
                graph: dict[str, object],
                mode: str,
                root_name: str,
                *,
                skip_validation: bool = False,
            ) -> dict[str, str]:
                return {
                    **original_builder(graph, mode, root_name, skip_validation=skip_validation),
                    **source_notes,
                }

            service = LegacyProjectionService(
                replace(service.dependencies, build_projection_notes=build)
            )
        with service.projection_lock(vault, "Tedd Knowledge"):
            return service._project_graph_locked(
                legacy_store.blank_graph(clock=lambda: "2026-08-03T12:34:56Z"),
                vault,
                "Tedd Knowledge",
                "review_safe",
                "k" * 64,
                "root_test",
                binaries=binaries,
            )

    def test_source_note_edits_are_preserved_with_ownership_marker_intact(self) -> None:
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        relative = "20 Evidence/source-" + "a" * 32 + ".md"
        note = PRESENTATION_MARKER + "\n# Neutral\n\nOnly after review.\n"
        with self.private_directory() as temporary:
            vault = Path(temporary)
            self.project_with_binaries(vault, {}, {relative: note})
            path = vault / "Tedd Knowledge" / relative
            edited = path.read_text(encoding="utf-8").replace(
                "Only after review.", "Owner annotation."
            )
            kg.atomic_write(path, edited, mode=0o600)
            for desired in ({relative: note}, {}):
                with self.subTest(refresh=bool(desired)), self.assertRaises(ValueError):
                    self.project_with_binaries(vault, {}, desired)
                self.assertEqual(path.read_text(encoding="utf-8"), edited)

    def test_binary_copy_is_root_bound_reused_and_removed_without_rewriting_bytes(self) -> None:
        from tedd_knowledge_graph import source_files

        service = kg._legacy_projection_service()
        payload = b"\x89PNG\r\n\x1a\nneutral publication fixture"
        relative = service.binary_path("k" * 64, "root_test", payload, binding="a" * 32)
        self.assertNotEqual(
            relative, service.binary_path("k" * 64, "other_root", payload, binding="a" * 32)
        )
        self.assertNotEqual(
            relative, service.binary_path("k" * 64, "root_test", payload, binding="b" * 32)
        )
        with self.private_directory() as temporary:
            vault = Path(temporary)
            result = self.project_with_binaries(vault, {relative: payload})
            path = vault / "Tedd Knowledge" / relative
            self.assertEqual(path.read_bytes(), payload)
            stamp = path.stat().st_mtime_ns
            again = self.project_with_binaries(vault, {relative: payload})
            self.assertNotEqual(again["unchanged_files"], 0)
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            manifest = json.loads((vault / "Tedd Knowledge" / kg.PROJECTION_MANIFEST).read_text())
            self.assertEqual(
                manifest["content_hashes"][relative], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(result["generated_files"], len(manifest["generated_files"]))
            if os.name == "nt":
                self.project_with_binaries(vault, {})
                self.assertFalse(path.exists())
            else:
                before = path.read_bytes(), path.stat().st_ino
                with self.assertRaises(source_files.MutationGuardUnavailable):
                    self.project_with_binaries(vault, {})
                self.assertEqual((path.read_bytes(), path.stat().st_ino), before)

    def test_edit_after_preview_preflight_is_not_deleted_or_overwritten(self) -> None:
        from tedd_knowledge_graph.legacy_projection import (
            LegacyProjectionService,
            _PriorProjection,
            _ProjectionContext,
            _PublicationPlan,
        )
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        relative = "20 Evidence/source-" + "a" * 32 + ".md"
        note = PRESENTATION_MARKER + "\n# Neutral\n\nOnly after review.\n"
        original_check = LegacyProjectionService._verify_source_publication
        for refresh in (False, True):
            with self.subTest(refresh=refresh), self.private_directory() as temporary:
                vault = Path(temporary)
                self.project_with_binaries(vault, {}, {relative: note})
                path = vault / "Tedd Knowledge" / relative
                edited = path.read_text(encoding="utf-8") + "\nOwner annotation.\n"

                def check_then_edit(
                    service: LegacyProjectionService,
                    context: _ProjectionContext,
                    prior: _PriorProjection,
                    owned: set[str],
                    plan: _PublicationPlan,
                    manifest_key: str,
                    *,
                    edited_path: Path = path,
                    edited_text: str = edited,
                ) -> dict[str, tuple[tuple[int, int], str]]:
                    checked = original_check(service, context, prior, owned, plan, manifest_key)
                    kg.atomic_write(edited_path, edited_text, mode=0o600)
                    return checked

                with (
                    mock.patch.object(
                        LegacyProjectionService,
                        "_verify_source_publication",
                        autospec=True,
                        side_effect=check_then_edit,
                    ),
                    self.assertRaises(ValueError),
                ):
                    self.project_with_binaries(vault, {}, {relative: note} if refresh else {})
                self.assertEqual(path.read_text(encoding="utf-8"), edited)

    def test_binary_copy_rejects_forged_token_and_changed_bytes_without_deleting(self) -> None:
        service = kg._legacy_projection_service()
        payload = b"\x89PNG\r\n\x1a\nneutral publication fixture"
        relative = service.binary_path("k" * 64, "root_test", payload, binding="a" * 32)
        with self.private_directory() as temporary:
            vault = Path(temporary)
            with self.assertRaises(ValueError):
                self.project_with_binaries(vault, {relative.replace("source-", "forged-"): payload})
            self.project_with_binaries(vault, {relative: payload})
            path = vault / "Tedd Knowledge" / relative
            kg.atomic_write(path, "owner edit", mode=0o600)
            for desired in ({relative: payload}, {}):
                with self.subTest(desired=bool(desired)), self.assertRaises(ValueError):
                    self.project_with_binaries(vault, desired)
                self.assertEqual(path.read_bytes(), b"owner edit")

    def test_binary_first_publication_crash_recovers_without_second_inventory(self) -> None:
        from tedd_knowledge_graph import source_files

        service = kg._legacy_projection_service()
        payload = b"\x89PNG\r\n\x1a\nneutral publication fixture"
        relative = service.binary_path("k" * 64, "root_test", payload, binding="a" * 32)
        original = kg.atomic_write
        for retry in (True, False):
            with self.subTest(retry=retry), self.private_directory() as temporary:
                vault = Path(temporary)
                live_clock = mock.Mock(return_value="2026-08-03T12:34:56Z")

                def fail_manifest(path: Path, content: str, *, mode: int = 0o600) -> None:
                    if path.name == kg.PROJECTION_MANIFEST:
                        raise OSError("neutral interruption")
                    original(path, content, mode=mode)

                with mock.patch.object(kg, "utc_now", live_clock):
                    with mock.patch.object(kg, "atomic_write", side_effect=fail_manifest):
                        with self.assertRaises(OSError):
                            self.project_with_binaries(vault, {relative: payload})
                    path = vault / "Tedd Knowledge" / relative
                    self.assertEqual(path.read_bytes(), payload)
                    self.assertFalse((vault / "Tedd Knowledge" / kg.PROJECTION_MANIFEST).exists())
                    live_clock.return_value = "2026-08-03T12:34:57Z"
                    if os.name != "nt" and not retry:
                        before = path.read_bytes(), path.stat().st_ino
                        with self.assertRaises(source_files.MutationGuardUnavailable):
                            self.project_with_binaries(vault, {})
                        self.assertEqual((path.read_bytes(), path.stat().st_ino), before)
                    else:
                        self.project_with_binaries(vault, {relative: payload} if retry else {})
                        self.assertEqual(path.exists(), retry)

    def test_first_preview_crash_cleanup_preserves_an_edited_claim_without_content_trailer(
        self,
    ) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_presentation import PRESENTATION_MARKER

        claim = "11 Claims/neutral-claim.md"
        source = "20 Evidence/source-" + "a" * 32 + ".md"
        notes = {
            claim: "# Neutral claim\n\n" + PRESENTATION_MARKER + "\nSaved source link.\n",
            source: PRESENTATION_MARKER + "\n# Condition\nOnly after review.\n",
        }
        original_write = kg.atomic_write
        original_bytes = source_files.write_bytes
        for failure_target in (Path(source).name, kg.PROJECTION_MANIFEST):
            with self.subTest(failure_target=failure_target), self.private_directory() as temporary:
                vault = Path(temporary)

                def interrupt(
                    path: Path,
                    content: str,
                    *,
                    mode: int = 0o600,
                    target: str = failure_target,
                ) -> None:
                    if path.name == target:
                        raise OSError("neutral interrupted first publication")
                    original_write(path, content, mode=mode)

                def interrupt_bytes(
                    path: Path,
                    payload: bytes,
                    *,
                    overwrite: bool = False,
                    target: str = failure_target,
                ) -> None:
                    if path.name == target:
                        raise OSError("neutral interrupted first publication")
                    original_bytes(path, payload, overwrite=overwrite)

                with (
                    mock.patch.object(kg, "atomic_write", side_effect=interrupt),
                    mock.patch.object(source_files, "write_bytes", side_effect=interrupt_bytes),
                ):
                    with self.assertRaises(OSError):
                        self.project_with_binaries(vault, {}, notes)
                path = vault / "Tedd Knowledge" / claim
                edited = (
                    "\n".join(
                        line
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if "<!-- tedd-source-content:" not in line
                    )
                    + "\nOwner annotation.\n"
                )
                kg.atomic_write(path, edited, mode=0o600)
                with self.assertRaises(ValueError):
                    self.project_with_binaries(vault, {})
                self.assertEqual(path.read_text(encoding="utf-8"), edited)

    def private_directory(self) -> tempfile.TemporaryDirectory[str]:
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA"))
        try:
            kg.set_private_path_mode(Path(temporary.name), 0o700)
        except Exception:
            temporary.cleanup()
            raise
        return temporary

    def test_empty_projection_preserves_authenticated_tree_bytes(self) -> None:
        with self.private_directory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir(mode=0o700)
            kg.set_private_path_mode(vault, 0o700)
            with mock.patch.object(kg, "utc_now", return_value="2026-08-03T12:34:56Z"):
                result = kg.project_graph(
                    kg.blank_graph(),
                    vault,
                    "Codex Knowledge",
                    "review_safe",
                    "k" * 64,
                    root_id="root_test",
                )

            root = vault / "Codex Knowledge"
            file_hashes = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            result["root"] = "<root>"
            payload = json.dumps(
                {"result": result, "files": file_hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                # Reviewed change: the empty view now includes reader-first
                # dashboard routes, the generated-note class, concise Bases,
                # native-stable note-property metadata/order and bounded folder
                # filter serialization.
                "462e1eeda08350c8be0651ebc8c935465e8d0d05af4d56e3116ca0d32f771d45",
            )
            self.assertEqual(len(file_hashes), 9)

    def test_flat_projection_signatures_keep_defaults(self) -> None:
        signature = inspect.signature(kg.projection_manifest_signature)
        self.assertEqual(
            signature.parameters["schema_version"].default,
            kg.PROJECTION_FORMAT_VERSION,
        )
        project = inspect.signature(kg.project_graph)
        self.assertEqual(
            project.parameters["timeout_seconds"].default,
            kg.DEFAULT_LOCK_TIMEOUT_SECONDS,
        )
        self.assertIsNone(project.parameters["force_current_digest"].default)

    def test_facade_composes_projection_dependencies_at_call_time(self) -> None:
        replacement = {"Generated"}
        with (
            mock.patch.object(kg, "GENERATED_PROJECTION_DIRS", replacement),
            mock.patch.object(
                kg._legacy_projection.LegacyProjectionService,
                "generated_projection_path",
                autospec=True,
                return_value=True,
            ) as delegated,
        ):
            self.assertTrue(kg.generated_projection_path("anything"))

        service = delegated.call_args.args[0]
        self.assertIs(service.dependencies.generated_projection_dirs, replacement)

    def test_marking_and_publication_resolve_facade_hooks_dynamically(self) -> None:
        with mock.patch.object(
            kg,
            "projection_file_marker",
            return_value="<!-- hook-marker -->",
        ) as marker:
            content = kg.marked_projection_content(
                "m" * 64,
                "root",
                "00 Dashboard.md",
                "dashboard\n",
            )
        self.assertEqual(content, "dashboard\n\n<!-- hook-marker -->\n")
        marker.assert_called_once_with("m" * 64, "root", "00 Dashboard.md")

        with self.private_directory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir(mode=0o700)
            kg.set_private_path_mode(vault, 0o700)
            real_atomic_write = kg.atomic_write
            with (
                mock.patch.object(kg, "utc_now", return_value="2026-08-03T12:34:56Z"),
                mock.patch.object(kg, "atomic_write", wraps=real_atomic_write) as atomic_write,
            ):
                kg.project_graph(
                    kg.blank_graph(),
                    vault,
                    "Codex Knowledge",
                    "review_safe",
                    "m" * 64,
                    root_id="root_hooks",
                )
        # Eight generated notes/Bases plus the authenticated manifest.
        self.assertEqual(atomic_write.call_count, 9)


if __name__ == "__main__":
    unittest.main()
