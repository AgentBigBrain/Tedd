"""The explicit source CLI never enables collection implicitly."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import knowledge_graph_v4 as v4
from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_contracts import (
    SourceDetails,
    SourceMetadata,
    SourceScope,
    digest,
    encode,
)
from tedd_knowledge_graph.source_host import SourceHost

from tests.support.paths import GRAPH_SCRIPTS


class SourceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="tedd-source-cli-", dir=private_temporary_parent()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        self.graph_root = self.root / "graph"
        self.graph_root.mkdir()
        set_private_path_mode(self.graph_root, 0o700)
        self.store = self.graph_root / "graph.sqlite3"
        with v4.create_store(self.store) as store:
            store.apply_changes(
                [
                    v4.RecordChange(
                        "entity_task",
                        "entities",
                        {
                            "id": "entity_task",
                            "namespace": "personal_task",
                            "type": "task",
                            "name": "Neutral support task",
                            "aliases": [],
                            "identity_keys": [
                                {
                                    "scheme": "fixture_task",
                                    "namespace": "personal_task",
                                    "value": "task",
                                }
                            ],
                            "lifecycle": "active",
                            "status": "current",
                            "sensitive": False,
                        },
                    )
                ],
                fingerprint="scope-fixture",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=0,
            )
            store.register_consumer("consumer", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer", {"entity_ids": ["entity_task"]}, subscription_id="subscription"
            )
        self.config = self.graph_root / "config.json"
        self.config.write_bytes(
            encode(
                {
                    "schema_version": "v2",
                    "store_path": str(self.store),
                    "canonical_store_format": "sqlite_v4_wal",
                }
            )
        )
        set_private_path_mode(self.config, 0o600)
        self.archive_root = self.root / "sources"
        self.cli = GRAPH_SCRIPTS / "tedd_sources.py"
        self.session = [
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

    def invoke(
        self,
        *args: str,
        success: bool = True,
        script: Path | None = None,
        input_data: str | None = None,
    ) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-B", str(script or self.cli), "--config", str(self.config), *args],
            capture_output=True,
            text=True,
            input=input_data,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        value: object = json.loads(result.stdout)
        self.assertIsInstance(value, dict)
        assert isinstance(value, dict)
        return value

    def test_disabled_capture_never_reads_input_or_creates_an_archive(self) -> None:
        missing = self.root / "does-not-exist"
        result = self.invoke(
            "capture-chat",
            "--input",
            str(missing),
            "--metadata",
            str(missing),
            "--acquired-sha256",
            "0" * 64,
            "--workflow-state",
            "active",
            *self.session,
        )
        self.assertEqual(result["status"], "inactive")
        self.assertFalse(self.archive_root.exists())

    def test_explicit_enable_and_disable_preserve_graph_bytes(self) -> None:
        graph_bytes = self.store.read_bytes()
        result = self.invoke(
            "init", "--enable", "--archive-root", str(self.archive_root), *self.session
        )
        self.assertEqual(result["status"], "enabled")
        self.assertTrue(self.archive_root.is_dir())
        self.assertEqual(self.store.read_bytes(), graph_bytes)
        self.assertEqual(self.invoke("disable")["status"], "disabled")
        self.assertTrue(self.archive_root.is_dir())
        self.assertEqual(self.store.read_bytes(), graph_bytes)

    def test_init_accepts_a_smaller_quota_and_reenable_preserves_it(self) -> None:
        result = self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.archive_root),
            "--max-bytes",
            "1048576",
            *self.session,
        )
        self.assertEqual(result["status"], "enabled")
        marker = self.archive_root / "archive.json"
        self.assertEqual(json.loads(marker.read_bytes())["max_bytes"], 1048576)
        before = marker.read_bytes()
        self.invoke("disable")
        self.assertEqual(
            self.invoke(
                "init",
                "--enable",
                "--archive-root",
                str(self.archive_root),
                *self.session,
            )["status"],
            "enabled",
        )
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(
            self.invoke(
                "init",
                "--enable",
                "--archive-root",
                str(self.archive_root),
                "--max-bytes",
                "1048576",
                *self.session,
            )["status"],
            "enabled",
        )
        self.assertEqual(marker.read_bytes(), before)

    def test_reenable_rejects_a_changed_quota_without_mutation(self) -> None:
        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        self.invoke("disable")
        config_before = self.config.read_bytes()
        marker = self.archive_root / "archive.json"
        marker_before = marker.read_bytes()
        result = self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.archive_root),
            "--max-bytes",
            "1048576",
            *self.session,
            success=False,
        )
        self.assertEqual(result["status"], "scope_denied")
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(marker.read_bytes(), marker_before)

    def test_init_refuses_invalid_quota_without_creating_archive(self) -> None:
        before = self.config.read_bytes()
        for quota in ("0", "-1", "5368709121"):
            with self.subTest(quota=quota):
                self.invoke(
                    "init",
                    "--enable",
                    "--archive-root",
                    str(self.archive_root),
                    "--max-bytes",
                    quota,
                    *self.session,
                    success=False,
                )
                self.assertFalse(self.archive_root.exists())
                self.assertEqual(self.config.read_bytes(), before)

    def test_init_limits_and_reenable_preserve_explicit_archive_policy(self) -> None:
        graph_bytes = self.store.read_bytes()
        self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.archive_root),
            "--max-pdf-bytes",
            "1000000",
            "--max-chat-bytes",
            "200000",
            "--max-manifest-bytes",
            "10000",
            "--max-reading-bytes",
            "100000",
            *self.session,
        )
        marker = self.archive_root / "archive.json"
        before = marker.read_bytes()
        self.assertEqual(
            json.loads(before)["limits"],
            {
                "pdf_bytes": 1000000,
                "chat_bytes": 200000,
                "manifest_bytes": 10000,
                "reading_bytes": 100000,
            },
        )
        self.invoke("disable")
        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        self.assertEqual(marker.read_bytes(), before)
        self.invoke("disable")
        config_before = self.config.read_bytes()
        self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.archive_root),
            "--max-chat-bytes",
            "300000",
            *self.session,
            success=False,
        )
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.store.read_bytes(), graph_bytes)

    def test_capture_dispatch_applies_shared_limits_and_rejects_unknown_kind(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import SourceArchiveError
        from tedd_knowledge_graph.v4_application_contracts import GraphReadSession

        self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.archive_root),
            "--max-chat-bytes",
            "100",
            "--max-pdf-bytes",
            "1000",
            *self.session,
        )
        self.invoke("configure", "--allow-local-documents", *self.session)
        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "personal_task"
        )
        _, config = tedd_sources.load_config(self.config)
        archive = tedd_sources.open_archive(self.archive_root, scope, config, readonly=False)
        session = GraphReadSession("agent", "task", "run", "consumer")
        input_path = self.root / "boundary-input"
        descriptor = self.root / "boundary-metadata.json"
        payload = b"x" * 1001
        input_path.write_bytes(payload)
        set_private_path_mode(input_path, 0o600)
        acquired = digest(payload)
        cases = (
            ("chat", "capture-chat", 100),
            ("text", "capture-file", 100),
            ("markdown", "capture-file", 100),
            ("email", "capture-file", 100),
            ("code", "capture-file", 100),
            ("tool_result", "capture-file", 100),
            ("web", "capture-file", 100),
            ("google_doc", "capture-file", 100),
            ("google_sheet", "capture-file", 100),
            ("google_slide", "capture-file", 100),
            ("pdf", "capture-pdf", 1000),
            ("docx", "capture-file", 1000),
            ("xlsx", "capture-file", 1000),
            ("pptx", "capture-file", 1000),
        )
        disabled_routes = frozenset({"google_doc", "google_sheet", "google_slide"})
        validate_document_capture = tedd_sources.validate_document_capture
        read_input_boundary = tedd_sources.source_files.read_input
        bypassed: list[str] = []

        def qualify_for_limit(
            current_archive: Archive, current_metadata: SourceMetadata, checksum: str
        ) -> None:
            if current_metadata.kind in disabled_routes:
                bypassed.append(current_metadata.kind)
                return
            validate_document_capture(current_archive, current_metadata, checksum)

        last_metadata: SourceMetadata | None = None
        with mock.patch.object(
            tedd_sources, "validate_document_capture", side_effect=qualify_for_limit
        ) as qualify:
            for kind, command, expected in cases:
                with self.subTest(kind=kind):
                    details = (
                        None
                        if command != "capture-file"
                        else SourceDetails(
                            "local",
                            "owner",
                            kind + "-representation",
                            "original",
                            "reader:1",
                            retention="retained_copy",
                            sensitivity="ordinary",
                        )
                    )
                    capture_method = (
                        "structured_export"
                        if kind == "chat"
                        else "pdf_original"
                        if kind == "pdf"
                        else "delivered_output"
                        if kind == "tool_result"
                        else "local_file"
                    )
                    current = SourceMetadata(
                        scope,
                        "boundary-" + kind,
                        "v1",
                        kind,
                        capture_method,
                        "fixture",
                        "task",
                        "run",
                        "encounter",
                        "2026-09-07T12:00:00Z",
                        details=details,
                    )
                    last_metadata = current
                    descriptor.write_bytes(encode(asdict(current)))
                    set_private_path_mode(descriptor, 0o600)
                    args = argparse.Namespace(
                        command=command,
                        input=str(input_path),
                        metadata=str(descriptor),
                        acquired_sha256=acquired,
                        config=str(self.config),
                    )
                    qualify.reset_mock()
                    with mock.patch.object(
                        tedd_sources.source_files,
                        "read_input",
                        wraps=read_input_boundary,
                    ) as read_input:
                        with self.assertRaises(SourceArchiveError) as raised:
                            tedd_sources.capture(archive, args, scope, session)
                    self.assertEqual(raised.exception.code, "limit_exceeded")
                    read_input.assert_called_once_with(input_path, expected)
                    if command == "capture-file":
                        qualify.assert_called_once_with(archive, current, acquired)
                    else:
                        qualify.assert_not_called()

            self.assertEqual(frozenset(bypassed), disabled_routes)
            assert last_metadata is not None
            unknown = asdict(last_metadata)
            unknown["kind"] = "unknown_binary"
            descriptor.write_bytes(encode(unknown))
            qualify.reset_mock()
            args.metadata = str(descriptor)
            with mock.patch.object(
                tedd_sources.source_files, "read_input", wraps=read_input_boundary
            ) as read_input:
                with self.assertRaises(SourceArchiveError) as raised:
                    tedd_sources.capture(archive, args, scope, session)
            self.assertEqual(raised.exception.code, "capability_unavailable")
            qualify.assert_not_called()
            read_input.assert_not_called()

    def test_configure_changes_only_explicit_limits_and_does_not_enable_collection(self) -> None:
        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        self.invoke("disable")
        config_before = self.config.read_bytes()
        graph_before = self.store.read_bytes()
        marker = self.archive_root / "archive.json"
        original = json.loads(marker.read_bytes())
        result = self.invoke("configure", "--max-chat-bytes", "1000", *self.session)
        self.assertEqual(result["status"], "configured")
        current = json.loads(marker.read_bytes())
        original["limits"]["chat_bytes"] = 1000
        self.assertEqual(current, original)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.store.read_bytes(), graph_before)
        before = marker.read_bytes()
        for flags in ((), ("--max-chat-bytes", "0"), ("--max-reading-bytes", "33554433")):
            with self.subTest(flags=flags):
                self.invoke("configure", *flags, *self.session, success=False)
                self.assertEqual(marker.read_bytes(), before)
        self.invoke("configure", "--max-chat-bytes", "1000", *self.session)
        self.assertEqual(marker.read_bytes(), before)
        wrong = [("other" if value == "agent" else value) for value in self.session]
        self.invoke("configure", "--max-chat-bytes", "10", *wrong, success=False)
        self.assertEqual(marker.read_bytes(), before)

    def test_lowered_cli_input_limits_refuse_before_acquisition_and_keep_lookup(self) -> None:
        self._capture_chat_fixture()
        before = self.invoke("lookup", "--source-id", "case", *self.session)
        self.invoke("configure", "--max-chat-bytes", "10", *self.session)
        self.assertEqual(self.invoke("lookup", "--source-id", "case", *self.session), before)
        payload = (self.root / "input.json").read_bytes()
        result = self.invoke(
            "capture-chat",
            "--input",
            str(self.root / "input.json"),
            "--metadata",
            str(self.root / "metadata.json"),
            "--acquired-sha256",
            digest(payload),
            "--workflow-state",
            "active",
            *self.session,
            success=False,
        )
        self.assertEqual(result["status"], "limit_exceeded")

    def test_paused_closed_and_excluded_capture_never_open_graph_archive_or_input(self) -> None:
        self.config.write_bytes(
            encode(
                {
                    "source_capture_enabled": True,
                    "source_archive_root": str(self.root / "absent"),
                    "store_path": str(self.root / "absent.sqlite3"),
                }
            )
        )
        for state in ("paused", "closed", "excluded"):
            with self.subTest(state=state):
                result = self.invoke(
                    "capture-chat",
                    "--input",
                    "missing",
                    "--metadata",
                    "missing",
                    "--acquired-sha256",
                    "0" * 64,
                    "--workflow-state",
                    state,
                    *self.session,
                )
                self.assertEqual(result, {"status": "inactive"})

    def test_capture_binds_real_scope_and_returns_only_an_opaque_receipt(self) -> None:
        self._capture_chat_fixture()

    def test_restrict_cli_retains_history_without_preview_policy_then_allows_explicit_forget(
        self,
    ) -> None:
        self._capture_chat_fixture()
        found = self.invoke("lookup", "--source-id", "case", *self.session)
        sources = found["sources"]
        assert isinstance(sources, list)
        uri = sources[0]["source_uri"]
        history = {
            path: path.read_bytes()
            for path in self.archive_root.rglob("*")
            if path.is_file() and path.parent.name in ("versions", "heads", "blobs", "readings")
        }
        before = self.store.read_bytes(), self.config.read_bytes()
        result = self.invoke("restrict", "--source-uri", uri, *self.session)
        self.assertEqual(result["status"], "source_restricted")
        self.assertEqual(result["failed_files"], 0)
        self.invoke("recover", *self.session)
        self.invoke("restrict", "--source-uri", uri, *self.session)
        self.assertEqual({path: path.read_bytes() for path in history}, history)
        self.assertEqual((self.store.read_bytes(), self.config.read_bytes()), before)
        self.assertEqual(self.invoke("lookup", "--source-id", "case", *self.session)["sources"], [])
        self.assertEqual(
            self.invoke("inspect", "--source-uri", uri, *self.session, success=False)["status"],
            "scope_denied",
        )
        from tedd_knowledge_graph import source_files

        config = json.loads(self.config.read_bytes())
        source_files.write_bytes(
            self.config, encode({**config, "source_preview_policy": "malformed"}), overwrite=True
        )
        incomplete = self.invoke("restrict", "--source-uri", uri, *self.session, success=False)
        self.assertEqual(incomplete["status"], "source_restricted")
        self.assertEqual(incomplete["cleanup_status"], "cleanup_incomplete")
        source_files.write_bytes(self.config, before[1], overwrite=True)
        approval = self.invoke("forget", "--source-uri", uri, *self.session)
        self.assertEqual(
            self.invoke(
                "forget",
                "--source-uri",
                uri,
                "--confirm-forget",
                "--expect-impact",
                str(approval["impact_sha256"]),
                *self.session,
            )["status"],
            "forgotten",
        )
        self.assertFalse(any(path.exists() for path in history))

    def _document_args(
        self, kind: str = "text", payload: bytes = b"Not approved. Only if reviewed.\r\n"
    ) -> list[str]:
        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "personal_task"
        )
        metadata = SourceMetadata(
            scope,
            "local-file",
            "snapshot",
            kind,
            "delivered_output" if kind == "tool_result" else "local_file",
            "fixture",
            "task",
            "run",
            "encounter",
            "2026-09-06T12:00:00Z",
            coverage="complete",
            details=SourceDetails(
                "local",
                "owner",
                "file",
                "original",
                "unread",
                title="Neutral condition",
                sensitivity="ordinary",
            ),
        )
        original, descriptor = self.root / "neutral.txt", self.root / "document-metadata.json"
        original.write_bytes(payload)
        descriptor.write_bytes(encode(asdict(metadata)))
        for path in (original, descriptor):
            set_private_path_mode(path, 0o600)
        return [
            "capture-file",
            "--input",
            str(original),
            "--metadata",
            str(descriptor),
            "--acquired-sha256",
            digest(payload),
            "--workflow-state",
            "active",
            *self.session,
        ]

    def _assert_macos_office_refusal(self, arguments: list[str]) -> None:
        graph_before = self.store.read_bytes()
        archive_before = {
            path.relative_to(self.archive_root): path.read_bytes()
            for path in self.archive_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            self.invoke(*arguments, "--reader-python", sys.executable, success=False),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )
        self.assertEqual(self.store.read_bytes(), graph_before)
        self.assertEqual(
            {
                path.relative_to(self.archive_root): path.read_bytes()
                for path in self.archive_root.rglob("*")
                if path.is_file()
            },
            archive_before,
        )

    def test_local_documents_need_one_explicit_scope_policy_and_survive_original_removal(
        self,
    ) -> None:
        arguments = self._document_args()
        graph_before = self.store.read_bytes()
        denied = self.invoke(*arguments, success=False)
        self.assertEqual(denied["status"], "capability_unavailable")
        allowed = self.invoke("configure", "--allow-local-documents", *self.session)
        self.assertEqual(allowed["status"], "access_configured")
        receipt = self.invoke(*arguments)
        self.assertEqual(receipt["status"], "preserved")
        (self.root / "neutral.txt").unlink()
        found = self.invoke("lookup", "--query", "Neutral condition", *self.session)
        sources = found["sources"]
        assert isinstance(sources, list)
        uri = sources[0]["source_uri"]
        inspected = self.invoke("inspect", "--source-uri", uri, *self.session)
        self.assertIn(b"Not approved. Only if reviewed.", encode(inspected))
        self.invoke("configure", "--deny-local-documents", *self.session)
        denied = self.invoke("inspect", "--source-uri", uri, *self.session, success=False)
        self.assertEqual(denied["status"], "capability_unavailable")
        self.assertNotIn(b"Not approved", encode(denied))
        self.assertEqual(self.store.read_bytes(), graph_before)

    def test_document_policy_configuration_does_not_enable_capture_or_change_other_settings(
        self,
    ) -> None:
        self._document_args()
        self.invoke("disable")
        before = json.loads(self.config.read_bytes())
        self.invoke("configure", "--allow-local-documents", *self.session)
        after = json.loads(self.config.read_bytes())
        self.assertEqual(
            {key: value for key, value in after.items() if key != "source_access_policy"}, before
        )
        self.assertFalse(after["source_capture_enabled"])

    def test_all_enabled_document_families_use_real_capture_and_exact_saved_bytes(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import SourceRef

        for kind in ("text", "markdown", "code", "web", "tool_result", "email"):
            with self.subTest(kind=kind):
                body = b"Not approved.\r\nOnly after review.\r\n"
                payload = (
                    b"From: Support <support@example.test>\r\n"
                    b"Subject: Conditions\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body
                    if kind == "email"
                    else body
                )
                arguments = self._document_args(kind, payload)
                self.invoke("configure", "--allow-local-documents", *self.session)
                graph_before = self.store.read_bytes()
                receipt = self.invoke(*arguments)
                reference = receipt["ref"]
                assert isinstance(reference, dict)
                ref = SourceRef(**reference)
                metadata = json.loads((self.root / "document-metadata.json").read_bytes())
                from tedd_knowledge_graph.source_contracts import decode_scope

                scope = decode_scope(metadata["scope"])
                archive = Archive.open(self.archive_root, scope=scope)
                # This private test access is not a public authority route.
                manifest = archive._manifest(ref.manifest_sha256)
                original, units = archive._objects(manifest)
                self.assertEqual(original, payload)
                expected = (
                    "From: Support <support@example.test>\nSubject: Conditions\n"
                    'Content-Type: text/plain; charset="utf-8"\n\n' + body.decode("utf-8")
                    if kind == "email"
                    else body.decode("utf-8")
                )
                self.assertEqual(units[0].text, expected)
                self.assertIsNotNone(units[0].location)
                self.assertEqual(manifest.metadata.kind, kind)
                (self.root / "neutral.txt").unlink()
                self.invoke("inspect", "--source-uri", ref.as_uri(), *self.session)
                self.assertEqual(self.store.read_bytes(), graph_before)

    def test_local_office_capture_preserves_exact_version_and_denies_later_access(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import SourceRef, decode_scope

        from tests.knowledge_graph.test_source_office import W, package, word_document

        payload = word_document()
        arguments = self._document_args("docx", payload)
        self.invoke("configure", "--allow-local-documents", *self.session)
        graph_before = self.store.read_bytes()
        if sys.platform == "darwin":
            self._assert_macos_office_refusal(arguments)
            return
        receipt = self.invoke(*arguments, "--reader-python", sys.executable)
        reference = receipt["ref"]
        assert isinstance(reference, dict)
        ref = SourceRef(**reference)
        descriptor = self.root / "document-metadata.json"
        metadata = json.loads(descriptor.read_bytes())
        scope = decode_scope(metadata["scope"])
        archive = Archive.open(self.archive_root, scope=scope)
        manifest = archive._manifest(ref.manifest_sha256)
        original, units = archive._objects(manifest)
        self.assertEqual(original, payload)
        self.assertEqual(units[0].text, "Not approved.")
        self.assertEqual(units[2].text, "Only then.")
        self.assertEqual(manifest.metadata.coverage, "partial")
        self.assertIn("office_layout_not_rendered", manifest.metadata.gaps)

        # Build a second real package, not an edit to compressed ZIP bytes.
        later = package(
            {
                "word/document.xml": (
                    f'<w:document xmlns:w="{W}"><w:body><w:p><w:r>'
                    "<w:t>Approved after review.</w:t></w:r></w:p></w:body></w:document>"
                ).encode()
            }
        )
        (self.root / "neutral.txt").write_bytes(later)
        metadata["source_version"] = "later"
        descriptor.write_bytes(encode(metadata))
        later_args = arguments.copy()
        later_args[later_args.index("--acquired-sha256") + 1] = digest(later)
        later_receipt = self.invoke(*later_args, "--reader-python", sys.executable)
        self.assertNotEqual(later_receipt["ref"], receipt["ref"])
        (self.root / "neutral.txt").unlink()
        evidence = self.invoke("inspect", "--source-uri", ref.as_uri(), *self.session)
        self.assertIn("Not approved.", json.dumps(evidence))
        self.assertNotIn("Approved after review.", json.dumps(evidence))
        self.invoke("restrict", "--source-uri", ref.as_uri(), *self.session)
        denied = self.invoke("inspect", "--source-uri", ref.as_uri(), *self.session, success=False)
        self.assertEqual(denied["status"], "scope_denied")
        self.assertEqual(archive._objects(manifest)[0], payload)
        self.assertEqual(self.store.read_bytes(), graph_before)

    def test_local_spreadsheet_and_slides_use_bounded_worker_and_native_locations(self) -> None:
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import SourceLocation, SourceRef, decode_scope

        from tests.knowledge_graph.test_source_office import REL, A, P, R, S, package

        fixtures = (
            (
                "xlsx",
                {
                    "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Conditions" sheetId="7" r:id="r1"/></sheets></workbook>',
                    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Amount USD</t></is></c></row><row r="2"><c r="A2"><f>7/2</f><v>3.5</v></c></row></sheetData></worksheet>',
                },
                1,
                SourceLocation("cell", "7", 2, 2, 1, 1),
                "A2: formula: =7/2; saved n: 3.5",
            ),
            (
                "pptx",
                {
                    "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="257" r:id="r2"/><p:sldId id="258" r:id="r1"/></p:sldIdLst></p:presentation>',
                    "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/slide" Target="slides/slide1.xml"/><Relationship Id="r2" Type="{R}/slide" Target="slides/slide2.xml"/></Relationships>',
                    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Next: review the condition.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
                    "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Not approved. Only after review.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
                },
                0,
                SourceLocation("slide", "257", 1, 1),
                "Not approved. Only after review.",
            ),
        )
        for kind, members, selected, location, expected in fixtures:
            with self.subTest(kind=kind):
                payload = package({name: value.encode() for name, value in members.items()})
                arguments = self._document_args(kind, payload)
                self.invoke("configure", "--allow-local-documents", *self.session)
                graph_before = self.store.read_bytes()
                if sys.platform == "darwin":
                    self._assert_macos_office_refusal(arguments)
                    continue
                receipt = self.invoke(*arguments, "--reader-python", sys.executable)
                reference = receipt["ref"]
                assert isinstance(reference, dict)
                ref = SourceRef(**reference)
                metadata = json.loads((self.root / "document-metadata.json").read_bytes())
                archive = Archive.open(self.archive_root, scope=decode_scope(metadata["scope"]))
                manifest = archive._manifest(ref.manifest_sha256)
                original, units = archive._objects(manifest)
                self.assertEqual(original, payload)
                self.assertEqual(units[selected].location, location)
                self.assertIn(expected, units[selected].text)
                self.assertEqual(manifest.metadata.coverage, "partial")
                self.assertIn("office_layout_not_rendered", manifest.metadata.gaps)
                self.assertEqual(self.store.read_bytes(), graph_before)

                # Expected challenge: the saved amount is 3.5 USD, not 7 USD;
                # the first presented slide is conditional, not approval. The
                # partial XML reading must not claim a visual-layout check.
                graph_cli = GRAPH_SCRIPTS / "knowledge_graph.py"
                with v4.open_store(self.store, readonly=True) as store:
                    watermark = store.watermark
                packet = {
                    "source": {
                        "kind": "document",
                        "role": "external",
                        "title": "Neutral saved document",
                        "uri": ref.as_uri(),
                        "observed_at": "2026-09-05T12:00:00Z",
                        "source_tier": "validated_structured_candidate",
                    },
                    "claims": [
                        {
                            "surface_id": f"surface_{kind}",
                            "family": "document",
                            "key": f"condition_{kind}",
                            "value": "Amount is 7 USD"
                            if kind == "xlsx"
                            else "First slide approves",
                            "subject_refs": ["entity_task"],
                            "entity_refs": ["entity_task"],
                            "valid_from": "2026-09-05T12:00:00Z",
                            "source_tier": "validated_structured_candidate",
                        }
                    ],
                }
                capture_args = [
                    "capture",
                    "--input",
                    "-",
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
                    str(watermark),
                    "--source-subscription-id",
                    "subscription",
                    "--source-subject-id",
                    "entity_task",
                    "--authority-ceiling",
                    "validated_structured_candidate",
                ]
                plan_path = self.graph_root / f"{kind}-plan.json"
                plan = self.invoke(
                    *capture_args,
                    "--dry-run",
                    "--plan-output",
                    str(plan_path),
                    script=graph_cli,
                    input_data=json.dumps(packet),
                )
                self.invoke(
                    *capture_args,
                    "--expect-fingerprint",
                    str(plan["fingerprint"]),
                    "--expect-plan-file",
                    str(plan_path),
                    script=graph_cli,
                    input_data=json.dumps(packet),
                )
                with v4.open_store(self.store, readonly=True) as store:
                    claim_id = str(store.current_claims((f"surface_{kind}",))[0]["record_id"])
                    ordinary_before = encode(store.context_pack("subscription"))
                graph_before = self.store.read_bytes()
                history_before = {p: p.read_bytes() for p in archive._all_files()}
                (self.root / "neutral.txt").unlink()
                evidence = self.invoke(
                    "source-evidence", "--id", claim_id, *self.session, script=graph_cli
                )
                sources = evidence["sources"]
                assert isinstance(sources, list) and isinstance(sources[0], dict)
                self.assertEqual(sources[0]["ref"], reference)
                passages = sources[0]["units"]
                assert isinstance(passages, list) and isinstance(passages[selected], dict)
                self.assertEqual(passages[selected]["location"], asdict(location))
                self.assertIn(expected, passages[selected]["text"])
                if kind == "xlsx":
                    self.assertIn("Amount USD", json.dumps(passages))
                else:
                    self.assertEqual(
                        passages[1]["location"], asdict(SourceLocation("slide", "258", 2, 2))
                    )
                self.assertFalse(evidence["complete"])
                self.assertEqual(self.store.read_bytes(), graph_before)
                self.assertEqual({p: p.read_bytes() for p in archive._all_files()}, history_before)
                with v4.open_store(self.store, readonly=True) as store:
                    self.assertEqual(encode(store.context_pack("subscription")), ordinary_before)

    def test_office_without_a_reader_or_with_opaque_parts_never_publishes(self) -> None:
        from tests.knowledge_graph.test_source_office import W, package, word_document

        for payload, reader, expected_status in (
            (word_document(), [], "capability_unavailable"),
            (
                package({"word/media/image.png": b"not an inspectable XML part"}),
                ["--reader-python", sys.executable],
                "source_corrupt",
            ),
            (
                package(
                    {
                        "word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>password=neutral-test-secret</w:t></w:r></w:p></w:body></w:document>'.encode()
                    }
                ),
                ["--reader-python", sys.executable],
                "privacy_rejected",
            ),
        ):
            with self.subTest(reader=bool(reader), checksum=digest(payload)):
                arguments = self._document_args("docx", payload)
                self.invoke("configure", "--allow-local-documents", *self.session)
                graph_before = self.store.read_bytes()
                before = {p: p.read_bytes() for p in self.archive_root.rglob("*") if p.is_file()}
                refused = self.invoke(*arguments, *reader, success=False)
                if sys.platform == "darwin" and reader:
                    self.assertEqual(
                        refused,
                        {
                            "status": "capability_unavailable",
                            "reason": "macos_worker_boundary_unqualified",
                        },
                    )
                else:
                    self.assertEqual(refused["status"], expected_status)
                self.assertEqual(self.store.read_bytes(), graph_before)
                self.assertEqual(
                    {p: p.read_bytes() for p in self.archive_root.rglob("*") if p.is_file()}, before
                )

    def test_document_policy_change_during_extraction_prevents_late_publication(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_contracts import SourceArchiveError
        from tedd_knowledge_graph.source_documents import DocumentReading

        arguments = self._document_args("markdown")
        self.invoke("configure", "--allow-local-documents", *self.session)
        args = tedd_sources.parser().parse_args(["--config", str(self.config), *arguments])
        entered, release = threading.Event(), threading.Event()
        original_reader = tedd_sources.read_document

        def read(payload: bytes, kind: str, *, maximum_bytes: int) -> DocumentReading:
            result = original_reader(payload, kind, maximum_bytes=maximum_bytes)
            entered.set()
            if not release.wait(10):
                raise AssertionError("neutral document extraction barrier timed out")
            return result

        before = self.store.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with mock.patch.object(tedd_sources, "read_document", side_effect=read):
                pending = pool.submit(tedd_sources.execute, args)
                try:
                    self.assertTrue(entered.wait(10))
                    self.invoke("configure", "--deny-local-documents", *self.session)
                finally:
                    release.set()
                with self.assertRaises(SourceArchiveError):
                    pending.result(timeout=10)
        self.assertEqual(list(self.archive_root.rglob("versions/*")), [])
        self.assertEqual(self.store.read_bytes(), before)

    def test_managed_projection_is_not_captured_as_an_independent_document(self) -> None:
        import tedd_sources

        arguments = self._document_args()
        self.invoke("configure", "--allow-local-documents", *self.session)
        vault = self.root / "vault"
        managed = vault / "Tedd Knowledge"
        managed.mkdir(parents=True)
        copied = managed / "memory.md"
        copied.write_bytes((self.root / "neutral.txt").read_bytes())
        snapshot, config = tedd_sources.load_config(self.config)
        tedd_sources.save_config(
            self.config, snapshot, {**config, "vault_path": str(vault), "root_name": managed.name}
        )
        arguments[arguments.index("--input") + 1] = str(copied)
        result = self.invoke(*arguments, success=False)
        self.assertEqual(result["status"], "scope_denied")
        # Owner-authored notes elsewhere in the same vault remain eligible.
        original = vault / "authored.md"
        original.write_bytes(copied.read_bytes())
        arguments[arguments.index("--input") + 1] = str(original)
        self.assertEqual(self.invoke(*arguments)["status"], "preserved")

    def _capture_chat_fixture(self) -> None:
        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        message: dict[str, object] = {
            "id": "m1",
            "speaker": None,
            "text": "Not approved. Only if reviewed.",
            "time": None,
            "reply_to": None,
            "links": [],
        }
        payload = encode(
            {
                "format": 1,
                "source_id": "case",
                "source_version": "v1",
                "coverage": "complete",
                "gaps": [],
                "delivery": "sent",
                "messages": [message],
            }
        )
        input_path = self.root / "input.json"
        input_path.write_bytes(payload)
        set_private_path_mode(input_path, 0o600)
        metadata: dict[str, object] = {
            "scope": {
                "store_id": state["store_id"],
                "generation": str(state["generation"]),
                "subject_ids": ["entity_task"],
                "namespace": "personal_task",
            },
            "source_id": "case",
            "source_version": "v1",
            "kind": "chat",
            "capture_method": "structured_export",
            "tool_id": "fixture:1",
            "task_id": "task",
            "run_id": "run",
            "interaction_id": "interaction",
            "captured_at": "2026-09-05T12:00:00Z",
            "observed_at": None,
            "origin": None,
            "authorship": "unknown",
            "coverage": "complete",
            "gaps": [],
            "privacy": "original",
            "identity_known": True,
        }
        metadata_path = self.root / "metadata.json"
        metadata_path.write_bytes(encode(metadata))
        set_private_path_mode(metadata_path, 0o600)
        result = self.invoke(
            "capture-chat",
            "--input",
            str(input_path),
            "--metadata",
            str(metadata_path),
            "--acquired-sha256",
            digest(payload),
            "--workflow-state",
            "active",
            *self.session,
        )
        serialized = encode(result)
        self.assertNotIn(b"Not approved", serialized)
        self.assertNotIn(str(self.root).encode(), serialized)
        self.assertEqual(result["status"], "preserved")
        found = self.invoke("lookup", "--source-id", "case", *self.session)
        self.assertEqual(found["basis"], "fresh_source_discovery")
        sources = found["sources"]
        self.assertIsInstance(sources, list)
        assert isinstance(sources, list)
        self.assertEqual(len(sources), 1)

    def test_wrong_owner_or_archive_inside_graph_storage_is_refused(self) -> None:
        wrong = [("other" if value == "agent" else value) for value in self.session]
        self.invoke(
            "init", "--enable", "--archive-root", str(self.archive_root), *wrong, success=False
        )
        self.assertFalse(self.archive_root.exists())
        self.invoke(
            "init",
            "--enable",
            "--archive-root",
            str(self.graph_root / "sources"),
            *self.session,
            success=False,
        )
        self.assertFalse((self.graph_root / "sources").exists())

    def test_page_render_checks_original_binding_and_does_not_cache_a_copy(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import (
            SourceArchiveError,
            SourceMetadata,
            SourceScope,
            SourceUnit,
        )

        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "personal_task"
        )
        archive = Archive.initialize(self.archive_root, scope=scope)
        original = b"%PDF-1.7 neutral"
        meta = SourceMetadata(
            scope,
            "manual",
            "v2",
            "pdf",
            "pdf_original",
            "fixture",
            "task",
            "run",
            "interaction",
            "2026-09-05T12:00:00Z",
        )
        ref = archive.publish(
            payload=original,
            metadata=meta,
            units=(SourceUnit("u1", "Visual page", 1, kind="image_page"),),
        ).ref
        png = b"\x89PNG\r\n\x1a\nneutral-test-render"
        rendered = {
            "status": "rendered",
            "page": 1,
            "original_sha256": digest(original),
            "png_sha256": digest(png),
            "png_base64": base64.b64encode(png).decode(),
            "resources": "bounded",
        }
        args = argparse.Namespace(
            source_uri=ref.as_uri(), page=1, reader_python=sys.executable, renderer=sys.executable
        )
        before = {
            p.relative_to(self.archive_root): p.read_bytes()
            for p in self.archive_root.rglob("*")
            if p.is_file()
        }

        def worker(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(kwargs["input"], original)
            return subprocess.CompletedProcess([], 0, encode(rendered))

        renderer_process = SimpleNamespace(
            run=worker,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        # This synthetic worker checks response validation only; bypassing the
        # host gate here does not qualify native worker execution on macOS.
        with (
            mock.patch.object(tedd_sources, "require_worker_boundary", return_value=None),
            mock.patch.object(tedd_sources, "subprocess", renderer_process),
        ):
            result = tedd_sources.render_page(archive, args)
        self.assertEqual(result["png_sha256"], digest(png))
        self.assertEqual(
            before,
            {
                p.relative_to(self.archive_root): p.read_bytes()
                for p in self.archive_root.rglob("*")
                if p.is_file()
            },
        )
        rendered["original_sha256"] = "0" * 64
        with (
            mock.patch.object(tedd_sources, "require_worker_boundary", return_value=None),
            mock.patch.object(tedd_sources, "subprocess", renderer_process),
        ):
            with self.assertRaises(SourceArchiveError):
                tedd_sources.render_page(archive, args)

    def test_discovery_release_rechecks_consumer_ownership(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_contracts import SourceArchiveError

        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        args = tedd_sources.parser().parse_args(
            ["--config", str(self.config), "lookup", "--query", "neutral", *self.session]
        )
        execute = tedd_sources._execute

        def rebind(selected: argparse.Namespace) -> dict[str, object]:
            result = execute(selected)
            with v4.open_store(self.store) as writer:
                writer.rebind_consumer(
                    "consumer", task_id="task", expected_old_agent_id="agent", new_agent_id="other"
                )
            return result

        with mock.patch.object(tedd_sources, "_execute", side_effect=rebind):
            with self.assertRaises(SourceArchiveError):
                tedd_sources.execute(args)

    def _assert_render_authority(self, *, execute: bool, allowed_checks: int | None) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_authority import (
            AccessDecision,
            SourceAccess,
            access_binding,
        )
        from tedd_knowledge_graph.source_contracts import SourceArchiveError, SourceUnit

        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "personal_task"
        )
        checks: list[tuple[str, str]] = []

        def provide(
            meta: SourceMetadata, sha: str, operation: str, audience: str
        ) -> AccessDecision:
            if operation != "capture":
                checks.append((operation, audience))
            # For denial regressions, inspect remains allowed: it cannot stand in
            # for render permission. The success control permits render only.
            denied = (
                operation == "render" and checks.count(("render", "model")) > allowed_checks
                if allowed_checks is not None
                else operation == "inspect"
            )
            return AccessDecision(
                access_binding(meta, sha), operation, audience, "denied" if denied else "allowed"
            )

        archive = Archive.open(
            self.archive_root,
            scope=scope,
            readonly=False,
            access=SourceAccess(scope, "model", ("ordinary",), provider_check=provide),
        )
        ref = archive.publish(
            payload=b"%PDF-1.7 neutral",
            metadata=SourceMetadata(
                scope,
                "manual",
                "v1",
                "pdf",
                "provider_export",
                "fixture",
                "task",
                "run",
                "interaction",
                "2026-09-05T12:00:00Z",
                details=SourceDetails(
                    "provider",
                    "account",
                    "pdf",
                    "provider_export",
                    "reader",
                    sensitivity="ordinary",
                    retention="access_dependent",
                ),
            ),
            units=(SourceUnit("u1", "Visual page", 1, kind="image_page"),),
        ).ref
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.config),
                "render-page",
                "--source-uri",
                ref.as_uri(),
                "--page",
                "1",
                "--reader-python",
                sys.executable,
                "--renderer",
                sys.executable,
                *self.session,
            ]
        )
        png = b"\x89PNG\r\n\x1a\nneutral-test-render"
        # The renderer is external. The injected archive supplies a synthetic,
        # exact-bound trusted host callback, not a serialized provider grant.
        with (
            mock.patch.object(tedd_sources, "_render_pdf_png", autospec=True, return_value=png),
            mock.patch.object(tedd_sources, "open_archive", autospec=True, return_value=archive),
        ):
            if allowed_checks is None:
                result = (
                    tedd_sources.execute(args)
                    if execute
                    else tedd_sources.render_page(archive, args)
                )
                self.assertEqual(result["png_sha256"], digest(png))
            else:
                with self.assertRaises(SourceArchiveError) as denied:
                    if execute:
                        tedd_sources.execute(args)
                    else:
                        tedd_sources.render_page(archive, args)
                self.assertEqual(denied.exception.code, "scope_denied")
        self.assertEqual(checks, [("render", "model")] * (3 if execute else 2))

    def test_post_render_release_requires_render_not_inspect_authority(self) -> None:
        self._assert_render_authority(execute=False, allowed_checks=1)

    def test_command_release_requires_render_not_inspect_authority(self) -> None:
        self._assert_render_authority(execute=True, allowed_checks=2)

    def test_render_permission_does_not_require_inspect_permission(self) -> None:
        self._assert_render_authority(execute=True, allowed_checks=None)

    def _assert_capture_change_during_extraction(self, change: str) -> None:
        from dataclasses import asdict

        import tedd_sources
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_capture import PdfReading
        from tedd_knowledge_graph.source_contracts import (
            SourceArchiveError,
            SourceMetadata,
            SourceScope,
            SourceUnit,
        )

        self.invoke("init", "--enable", "--archive-root", str(self.archive_root), *self.session)
        with v4.open_store(self.store, readonly=True) as store:
            state = store.state()
        scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "personal_task"
        )
        meta = SourceMetadata(
            scope,
            "manual",
            "v1",
            "pdf",
            "pdf_original",
            "fixture",
            "task",
            "run",
            "interaction",
            "2026-09-05T12:00:00Z",
        )
        payload = b"%PDF-1.7 neutral extraction input"
        original, metadata = self.root / "manual.pdf", self.root / "pdf-metadata.json"
        original.write_bytes(payload)
        metadata.write_bytes(encode(asdict(meta)))
        set_private_path_mode(original, 0o600)
        set_private_path_mode(metadata, 0o600)
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.config),
                "capture-pdf",
                "--input",
                str(original),
                "--metadata",
                str(metadata),
                "--acquired-sha256",
                digest(payload),
                "--workflow-state",
                "active",
                "--reader-python",
                sys.executable,
                "--visual-review-sha256",
                digest(payload),
                "--visual-pages",
                "1",
                *self.session,
            ]
        )
        changed: list[str] = []

        def extract(
            _payload: bytes, _args: argparse.Namespace, *, host: SourceHost | None = None
        ) -> PdfReading:
            if change == "disable":
                snapshot, config = tedd_sources.load_config(self.config)
                tedd_sources.save_config(
                    self.config, snapshot, {**config, "source_capture_enabled": False}
                )
            else:
                with v4.open_store(self.store) as writer:
                    writer.rebind_consumer(
                        "consumer",
                        task_id="task",
                        expected_old_agent_id="agent",
                        new_agent_id="other",
                    )
            changed.append(change)
            return PdfReading(
                digest(payload),
                "fixture",
                1,
                (SourceUnit("u1", "Neutral text", 1, kind="page"),),
                "screened",
                "bounded",
            )

        with mock.patch.object(tedd_sources, "pdf_reading", side_effect=extract):
            with self.assertRaises(SourceArchiveError):
                tedd_sources.execute(args)
        self.assertEqual(changed, [change])
        self.assertEqual(Archive.open(self.archive_root, scope=scope).lookup("manual"), ())

    def test_disable_during_extraction_prevents_publication(self) -> None:
        self._assert_capture_change_during_extraction("disable")

    def test_rebind_during_extraction_prevents_publication(self) -> None:
        self._assert_capture_change_during_extraction("rebind")

    def test_publication_holds_graph_writer_reservation_without_graph_mutation(self) -> None:
        import sqlite3
        from contextlib import AbstractContextManager, closing

        import tedd_sources
        from tedd_knowledge_graph.source_archive import Archive
        from tedd_knowledge_graph.source_contracts import CaptureReceipt, SourceUnit

        publish = Archive.publish
        attempted: list[bool] = []
        before = self.store.read_bytes()

        def guarded(
            archive: Archive,
            *,
            payload: bytes,
            metadata: SourceMetadata,
            units: tuple[SourceUnit, ...],
            reuse_messages: bool = False,
            publication: AbstractContextManager[None] | None = None,
        ) -> CaptureReceipt:
            # A second native writer cannot rebind after the authority precheck.
            with closing(sqlite3.connect(self.store, timeout=0)) as competing:
                with self.assertRaises(sqlite3.OperationalError):
                    competing.execute("BEGIN IMMEDIATE")
            attempted.append(True)
            return publish(
                archive,
                payload=payload,
                metadata=metadata,
                units=units,
                reuse_messages=reuse_messages,
                publication=publication,
            )

        invoke = self.invoke

        def local_cli(*args: str, success: bool = True) -> dict[str, object]:
            if args[0] != "capture-chat":
                return invoke(*args, success=success)
            return tedd_sources.execute(
                tedd_sources.parser().parse_args(["--config", str(self.config), *args])
            )

        with (
            mock.patch.object(Archive, "publish", guarded),
            mock.patch.object(self, "invoke", side_effect=local_cli),
        ):
            self._capture_chat_fixture()
        self.assertEqual(attempted, [True])
        self.assertEqual(self.store.read_bytes(), before)

    def test_lock_only_publication_never_creates_a_missing_plan_key(self) -> None:
        import tedd_sources

        key = self.store.with_name(self.store.name + ".plan.key")
        key.unlink()
        before = self.store.read_bytes()
        with tedd_sources.publication_reservation(self.store):
            self.assertFalse(key.exists())
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse(key.exists())

    def test_lock_only_publication_refuses_schema_upgrade(self) -> None:
        import sqlite3
        from contextlib import closing

        import tedd_sources

        with closing(sqlite3.connect(self.store)) as connection:
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        before = self.store.read_bytes()
        with self.assertRaises(v4.StoreFormatError):
            with tedd_sources.publication_reservation(self.store):
                self.fail("Legacy schema must not be upgraded implicitly")
        self.assertEqual(self.store.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
