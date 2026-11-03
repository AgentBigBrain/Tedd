"""New synthetic graph/source boundaries; no installed configuration is used."""

from __future__ import annotations

import argparse
import base64
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import mock

import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture_adapter
from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_archive import Archive
from tedd_knowledge_graph.source_capture import capture_chat
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceExcerpt,
    SourceMetadata,
    SourceRef,
    SourceScope,
    SourceUnit,
    digest,
    encode,
)
from tedd_knowledge_graph.v4_application_contracts import (
    GraphReadScope,
    GraphReadSession,
    ReadScopeKind,
)
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, ProducerEnvelope

from tests.support.paths import GRAPH_SCRIPTS

if TYPE_CHECKING:
    from tedd_knowledge_graph.v4_source_evidence import PreparedSourceProjection

_PREVIEW_MUTATION = os.name == "nt" or sys.platform == "linux"


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="tedd-evidence-test-", dir=private_temporary_parent()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        graph_root = self.root / "graph"
        graph_root.mkdir()
        set_private_path_mode(graph_root, 0o700)
        self.path = graph_root / "graph.sqlite3"
        self.session = GraphReadSession("agent", "task", "run", "consumer")
        self.graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription")
        with v4.create_store(self.path) as store:
            store.apply_changes(
                [
                    v4.RecordChange(
                        "entity_task",
                        "entities",
                        {
                            "id": "entity_task",
                            "type": "task",
                            "namespace": "neutral_task",
                            "name": "Neutral task",
                            "aliases": [],
                            "identity_keys": [
                                {
                                    "scheme": "fixture_task",
                                    "namespace": "neutral_task",
                                    "value": "task",
                                }
                            ],
                            "lifecycle": "active",
                            "status": "current",
                            "sensitive": False,
                        },
                    )
                ],
                fingerprint="entity",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=0,
            )
            store.register_consumer("consumer", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer", {"entity_ids": ["entity_task"]}, subscription_id="subscription"
            )
            state = store.state()
        self.scope = SourceScope(
            state["store_id"], str(state["generation"]), ("entity_task",), "neutral_task"
        )
        self.archive = Archive.initialize(self.root / "sources", scope=self.scope)
        self.config = graph_root / "config.json"
        self.config.write_bytes(
            encode(
                {
                    "schema_version": "v2",
                    "store_path": str(self.path),
                    "canonical_store_format": "sqlite_v4_wal",
                    "source_archive_root": str(self.archive.root),
                    "source_capture_enabled": False,
                }
            )
        )
        set_private_path_mode(self.config, 0o600)
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
        self.units = (
            SourceUnit(
                "u1",
                "We can deploy Friday if security approves.",
                1,
                source_id="m1",
                speaker="support",
            ),
            SourceUnit(
                "u2",
                "This is not approval yet.",
                2,
                source_id="m2",
                speaker="support",
                reply_to="m1",
            ),
            SourceUnit("u3", "Use staging only.", 3, source_id="m3", speaker=None, reply_to="m2"),
        )
        payload = encode(
            {
                "format": 1,
                "source_id": "case",
                "source_version": "v1",
                "coverage": "complete",
                "gaps": [],
                "delivery": "sent",
                "messages": [
                    {
                        "id": unit.source_id,
                        "speaker": unit.speaker,
                        "text": unit.text,
                        "time": unit.source_time,
                        "reply_to": unit.reply_to,
                        "links": [],
                    }
                    for unit in self.units
                ],
            }
        )
        self.ref = capture_chat(
            self.archive, payload=payload, metadata=self.metadata, acquired_sha256=digest(payload)
        ).ref
        # An ordinary existing capture provides the historical graph boundary.
        # Binding validation tests below use the future reserved-URI path separately.
        self.packet: dict[str, object] = {
            "source": {
                "kind": "support",
                "role": "external",
                "title": "Neutral exchange",
                "uri": self.ref.as_uri(),
                "observed_at": "2026-09-05T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "claims": [
                {
                    "surface_id": "surface_approval",
                    "family": "deployment",
                    "key": "approval",
                    "value": "Deployment approved",
                    "subject_refs": ["entity_task"],
                    "entity_refs": ["entity_task"],
                    "valid_from": "2026-09-05T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                }
            ],
        }
        request = CaptureRequest(
            input_path="-",
            input_bytes=encode(self.packet),
            dry_run=True,
            consumer_id="consumer",
            envelope=ProducerEnvelope("agent", "task", None, "run", "verification", 1),
            authority_ceiling="validated_structured_candidate",
            allow_replace=False,
            allow_redirect=False,
            expected_fingerprint=None,
            expected_global_watermark=None,
            plan_token=None,
            expected_revisions={},
            expected_absent=(),
            source_config_path=self.config,
            source_subscription_id="subscription",
            source_subject_ids=self.scope.subject_ids,
        )
        self.capture_request = request
        with v4.open_store(self.path) as store:
            plan = capture_adapter.execute_capture(store, request)
            capture_adapter.execute_capture(
                store,
                replace(
                    request,
                    dry_run=False,
                    expected_fingerprint=str(plan["fingerprint"]),
                    plan_token=str(plan["plan_token"]),
                ),
            )
            changed_ids = plan["changed_ids"]
            assert isinstance(changed_ids, list)
            records = store.get_records(str(item) for item in changed_ids)
            self.claim_id = next(r["record_id"] for r in records if r["collection"] == "claims")
            self.source_id = next(r["record_id"] for r in records if r["collection"] == "sources")

    def gather(self, **overrides: object) -> dict[str, object]:
        module = importlib.import_module("tedd_knowledge_graph.v4_source_evidence")
        request = module.EvidenceRequest(
            session=self.session,
            scope=self.graph_scope,
            subject_ids=self.scope.subject_ids,
            record_id=self.claim_id,
            **overrides,
        )
        with v4.open_store(self.path, readonly=True) as store:
            return cast(dict[str, object], module.gather(store, self.archive, request))

    def _owner_preview_fixture(self) -> tuple[Archive, PreparedSourceProjection, bytes]:
        from tedd_knowledge_graph.source_authority import SourceAccess
        from tedd_knowledge_graph.source_contracts import SourceDetails
        from tedd_knowledge_graph.source_presentation import PreviewPolicy
        from tedd_knowledge_graph.v4_source_evidence import EvidenceRequest, prepare_source_views

        details = SourceDetails(
            "local",
            "owner",
            "file",
            "original",
            "reader",
            title="Neutral saved note",
            sensitivity="ordinary",
        )
        self.archive.access = SourceAccess(self.scope, "model", ("ordinary",), local_retention=True)
        ref = self.archive.publish(
            payload=b"Only after review.",
            metadata=replace(
                self.metadata,
                source_id="saved-file",
                kind="text",
                capture_method="local_file",
                details=details,
            ),
            units=(SourceUnit("u1", "Only after review.", 1),),
        ).ref
        with v4.open_store(self.path) as writer:
            source = writer.get_record(self.source_id)
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.source_id,
                        "sources",
                        {**cast(dict[str, object], source["payload"]), "uri": ref.as_uri()},
                        expected_revision=source["revision"],
                    ),
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "value": "Only after review.",
                        },
                        expected_revision=claim["revision"],
                    ),
                ],
                fingerprint="neutral-owner-view",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
            )
        owner = Archive.open(
            self.archive.root,
            scope=self.scope,
            access=SourceAccess(self.scope, "owner", ("ordinary",), local_retention=True),
        )
        policy = PreviewPolicy(
            self.scope, str(self.root / "vault"), "Tedd Knowledge", "root_test", True, "local_only"
        )
        request = EvidenceRequest(
            self.session, self.graph_scope, self.scope.subject_ids, self.claim_id
        )
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(reader, owner, request, policy)
        from tedd_knowledge_graph import source_files

        vault = Path(policy.vault_path)
        vault.mkdir()
        set_private_path_mode(vault, 0o700)
        config = json.loads(self.config.read_bytes())
        config.update(
            {
                "vault_path": policy.vault_path,
                "root_name": policy.root_name,
                "projection_root_id": policy.projection_root_id,
                "projection_manifest_key": "k" * 64,
                "source_preview_policy": {
                    **asdict(policy),
                    "format": 1,
                    "audience": "owner",
                    "sensitivities": ["ordinary"],
                    "exposure_approved": True,
                },
                "source_owner_access_policy": {
                    "format": 1,
                    "scope": asdict(self.scope),
                    "audience": "owner",
                    "sensitivities": ["ordinary"],
                    "local_retention": True,
                    "retained_bindings": [],
                },
            }
        )
        snapshot = encode(config)
        source_files.write_bytes(self.config, snapshot, overwrite=True)
        return owner, prepared, snapshot

    def _publish_refresh_or_verify_posix_refusal(
        self, snapshot: bytes, prepared: PreparedSourceProjection
    ) -> dict[str, object] | None:
        """Exercise supported mutation; other platforms still preserve copies."""
        import tedd_sources

        if _PREVIEW_MUTATION:
            return tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        before = {
            path: (path.read_bytes(), path.stat().st_ino)
            for path in root.rglob("*")
            if path.is_file()
        }
        with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
            tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.assertEqual(
            {
                path: (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            },
            before,
        )
        return None

    def _source_command(self, command: str, *options: str) -> argparse.Namespace:
        import tedd_sources

        assert self.session.agent_id is not None
        assert self.session.task_id is not None
        assert self.session.run_id is not None
        return tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.config),
                command,
                "--producer-agent-id",
                self.session.agent_id,
                "--task-id",
                self.session.task_id,
                "--producer-run-id",
                self.session.run_id,
                "--consumer-id",
                "consumer",
                "--subscription-id",
                "subscription",
                "--subject-id",
                "entity_task",
                *options,
            ]
        )

    def test_preview_approval_is_staged_without_model_access_or_host_activation(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, _, snapshot = self._owner_preview_fixture()
        config = json.loads(snapshot)
        config.pop("source_preview_policy")
        config.pop("source_owner_access_policy")
        config["unrelated_setting"] = "preserve"
        source_files.write_bytes(self.config, encode(config), overwrite=True)
        graph_before = self.path.read_bytes()
        result = tedd_sources.execute(
            self._source_command(
                "configure-preview",
                "--allow-local-previews",
                "--exposure",
                "local_only",
                "--approve-exposure",
            )
        )
        self.assertEqual(result["status"], "owner_preview_configured")
        self.assertFalse(result["enabled"])
        self.assertFalse(result["route_qualified"])
        updated = json.loads(self.config.read_bytes())
        self.assertEqual(updated["unrelated_setting"], "preserve")
        self.assertFalse(updated["source_capture_enabled"])
        self.assertNotIn("source_access_policy", updated)
        self.assertEqual(updated["source_owner_access_policy"]["audience"], "owner")
        self.assertEqual(
            updated["source_preview_policy"]["scope"],
            {
                "store_id": self.scope.store_id,
                "generation": self.scope.generation,
                "subject_ids": ["entity_task"],
                "namespace": "neutral_task",
            },
        )
        self.assertEqual(updated["source_preview_policy"]["sensitivities"], ["ordinary"])
        self.assertEqual(list((self.root / "vault").iterdir()), [])
        self.assertEqual(self.path.read_bytes(), graph_before)
        with mock.patch.object(Archive, "open", side_effect=AssertionError("no source I/O")):
            with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
                tedd_sources.execute(self._source_command("preview", "--record-id", self.claim_id))

    def test_preview_approval_requires_explicit_exposure_and_matching_scope(self) -> None:
        import tedd_sources

        _, _, snapshot = self._owner_preview_fixture()
        for options in (
            ("--allow-local-previews",),
            ("--allow-local-previews", "--exposure", "local_only"),
            ("--allow-local-previews", "--approve-exposure"),
            (
                "--allow-local-previews",
                "--approve-exposure",
                "--exposure",
                "local_only",
                "--subject-id",
                "entity_outside",
            ),
        ):
            with self.subTest(options=options), self.assertRaises(SourceArchiveError):
                tedd_sources.execute(self._source_command("configure-preview", *options))
            self.assertEqual(self.config.read_bytes(), snapshot)
            self.assertEqual(list((self.root / "vault").iterdir()), [])

    def test_public_preview_publishes_owner_notes_without_model_source_output(self) -> None:
        import tedd_sources

        _, prepared, _ = self._owner_preview_fixture()
        graph_before = self.path.read_bytes()
        # Exercise the real command/publisher with a synthetic qualified-build
        # decision. This is not native viewer qualification or a runtime override.
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", frozenset({sys.platform})
        ):
            configured = tedd_sources.execute(
                self._source_command(
                    "configure-preview",
                    "--allow-local-previews",
                    "--exposure",
                    "local_only",
                    "--approve-exposure",
                )
            )
            self.assertTrue(configured["enabled"])
            result = tedd_sources.execute(
                self._source_command("preview", "--record-id", self.claim_id)
            )
            self.assertEqual(result["status"], "owner_preview_published")
            self.assertNotIn(b"Only after review", encode(result))
            self.assertNotIn(b"Neutral saved note", encode(result))
            self.assertNotIn(str(self.root).encode(), encode(result))
            note = (
                self.root
                / "vault"
                / "Tedd Knowledge"
                / "20 Evidence"
                / f"source-{prepared.views[0].binding}.md"
            )
            self.assertIn("Only after review.", note.read_text(encoding="utf-8"))
            claim_notes = list((self.root / "vault" / "Tedd Knowledge" / "11 Claims").glob("*.md"))
            self.assertTrue(
                any(
                    f"source-{prepared.views[0].binding}" in p.read_text(encoding="utf-8")
                    for p in claim_notes
                )
            )
            with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
                tedd_sources.execute(
                    self._source_command(
                        "inspect",
                        "--source-uri",
                        prepared.views[0].excerpt.ref.as_uri(),
                    )
                )
            denied = tedd_sources.execute(
                self._source_command("configure-preview", "--deny-previews")
            )
            self.assertFalse(denied["enabled"])
            if _PREVIEW_MUTATION:
                self.assertFalse(note.exists())
            else:
                self.assertEqual(denied["cleanup_status"], "cleanup_incomplete")
                self.assertIn("Only after review.", note.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                tedd_sources.execute(self._source_command("preview", "--record-id", self.claim_id))
        self.assertEqual(self.path.read_bytes(), graph_before)

    @unittest.skipUnless(sys.platform == "linux", "Linux recovery composition")
    def test_linux_cleanup_reports_relocated_edit_and_blocks_reapproval(self) -> None:
        import tedd_sources

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        note = (
            self.root
            / "vault"
            / "Tedd Knowledge"
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        graph_before = self.path.read_bytes()
        real_replace = os.replace
        edited = note.read_bytes() + b"\nOwner's concurrent correction.\n"

        def edit_then_move(
            src: str | Path,
            dst: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if src == note.name:
                note.write_bytes(edited)
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=edit_then_move):
            denied = tedd_sources.execute(
                self._source_command("configure-preview", "--deny-previews")
            )
        self.assertEqual(denied["cleanup_status"], "cleanup_incomplete")
        self.assertEqual(denied["recovery_status"], "required")
        self.assertEqual(denied["relocated_files"], 1)
        self.assertTrue(denied["ordinary_refresh_required"])
        self.assertFalse(note.exists())
        recovery = self.config.parent / "source-preview-recovery"
        payloads = list(recovery.glob("*/payload"))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].read_bytes(), edited)
        self.assertNotIn(b"Owner's", encode(denied))
        self.assertNotIn(str(self.root).encode(), encode(denied))
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", frozenset({sys.platform})
        ):
            retried = tedd_sources.execute(
                self._source_command(
                    "configure-preview",
                    "--allow-local-previews",
                    "--exposure",
                    "local_only",
                    "--approve-exposure",
                )
            )
        self.assertFalse(retried["enabled"])
        self.assertEqual(retried["recovery_entries"], denied["recovery_entries"])
        self.assertEqual(payloads[0].read_bytes(), edited)
        self.assertEqual(self.path.read_bytes(), graph_before)
        status = tedd_sources.execute(argparse.Namespace(config=str(self.config), command="status"))
        self.assertEqual(status["recovery_entries"], denied["recovery_entries"])
        self.assertTrue(status["recovery_may_contain_relocated_files"])
        # Removing/changing the policy cannot hide old private recovery entries.
        config = json.loads(self.config.read_bytes())
        config.pop("source_preview_policy")
        without_policy = tedd_sources._cleanup_owner_preview_files(config, config_path=self.config)
        self.assertEqual(without_policy["cleanup_status"], "cleanup_incomplete")
        self.assertEqual(without_policy["recovery_entries"], denied["recovery_entries"])

    def test_public_preview_cannot_publish_after_approval_is_withdrawn(self) -> None:
        import tedd_knowledge_graph.v4_source_evidence as evidence
        import tedd_sources
        from tedd_knowledge_graph.source_presentation import PreviewPolicy

        self._owner_preview_fixture()
        prepare = evidence.prepare_source_views

        def withdraw(
            store: evidence.EvidenceStore,
            archive: Archive,
            request: evidence.EvidenceRequest | tuple[evidence.EvidenceRequest, ...],
            policy: PreviewPolicy,
        ) -> evidence.PreparedSourceProjection:
            prepared = prepare(store, archive, request, policy)
            tedd_sources.execute(self._source_command("configure-preview", "--deny-previews"))
            return prepared

        with (
            mock.patch.object(
                tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", frozenset({sys.platform})
            ),
            mock.patch.object(
                evidence, "prepare_source_views", autospec=True, side_effect=withdraw
            ),
        ):
            with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                tedd_sources.execute(self._source_command("preview", "--record-id", self.claim_id))
        self.assertFalse(json.loads(self.config.read_bytes())["source_preview_policy"]["enabled"])
        self.assertEqual(list((self.root / "vault").rglob("*.md")), [])

    @unittest.skipUnless(sys.platform == "linux", "Linux recovery composition")
    def test_recovery_refuses_config_inside_a_marker_free_vault(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, prepared, snapshot = self._owner_preview_fixture()
        vault = Path(prepared.policy.vault_path)
        inside = vault / "private-config.json"
        source_files.write_bytes(inside, snapshot)
        self.assertFalse((vault / ".obsidian").exists())
        with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
            tedd_sources._publish_owner_preview(inside, snapshot, prepared)
        self.assertFalse((vault / "source-preview-recovery").exists())
        self.assertEqual(list(vault.iterdir()), [inside])

    @unittest.skipUnless(sys.platform == "linux", "Linux recovery composition")
    def test_public_cleanup_keeps_move_receipt_when_recovery_inventory_becomes_unreadable(
        self,
    ) -> None:
        import tedd_sources

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        note = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        root = self.config.parent / "source-preview-recovery"
        real_replace, real_scandir = os.replace, os.scandir
        relocated = False

        def edit_then_move(
            src: str | Path,
            dst: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal relocated
            if src == note.name:
                note.write_bytes(b"Owner edit retained despite a later inventory error.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            if src == note.name:
                relocated = True

        def fail_inventory(
            path: str | Path | int = ".",
        ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if not isinstance(path, int) and Path(path) == root and relocated:
                raise PermissionError("recovery inventory unavailable")
            return real_scandir(path)

        with (
            mock.patch("os.replace", side_effect=edit_then_move),
            mock.patch("os.scandir", side_effect=fail_inventory),
        ):
            denied = tedd_sources.execute(
                self._source_command("configure-preview", "--deny-previews")
            )
        payloads = list(root.glob("*/payload"))
        self.assertEqual(denied["cleanup_status"], "cleanup_incomplete")
        self.assertTrue(denied["recovery_unreadable"])
        self.assertEqual(denied["relocated_files"], 1)
        self.assertEqual(denied["recovery_entries"], [payloads[0].parent.name])
        self.assertFalse(note.exists())
        self.assertEqual(
            payloads[0].read_bytes(), b"Owner edit retained despite a later inventory error."
        )

    def test_preview_reapproval_cannot_skip_incomplete_cleanup_of_an_edited_copy(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        note = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        edited = note.read_bytes() + b"\nOwner annotation.\n"
        source_files.write_bytes(note, edited, overwrite=True)
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", frozenset({sys.platform})
        ):
            denied = tedd_sources.execute(
                self._source_command("configure-preview", "--deny-previews")
            )
            self.assertEqual(denied["cleanup_status"], "cleanup_incomplete")
            for _ in range(2):
                retried = tedd_sources.execute(
                    self._source_command(
                        "configure-preview",
                        "--allow-local-previews",
                        "--exposure",
                        "local_only",
                        "--approve-exposure",
                    )
                )
                self.assertFalse(retried["enabled"])
                self.assertEqual(retried["cleanup_status"], "cleanup_incomplete")
                self.assertEqual(note.read_bytes(), edited)
                self.assertFalse(
                    json.loads(self.config.read_bytes())["source_preview_policy"]["enabled"]
                )

    def test_public_preview_refuses_a_vault_replaced_by_a_directory_link(self) -> None:
        import tedd_sources

        _, prepared, _ = self._owner_preview_fixture()
        vault = Path(prepared.policy.vault_path)
        destination = self.root / "not-approved-vault"
        destination.mkdir()
        set_private_path_mode(destination, 0o700)
        vault.rmdir()  # Only this test's newly created, still-empty vault.
        if os.name == "nt":
            made = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(vault), str(destination)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            self.addCleanup(vault.rmdir)  # Remove the junction, never its target.
        else:
            vault.symlink_to(destination, target_is_directory=True)
            self.addCleanup(vault.unlink)
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", frozenset({sys.platform})
        ):
            with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
                tedd_sources.execute(self._source_command("preview", "--record-id", self.claim_id))
        self.assertEqual(list(destination.iterdir()), [])
        denied = tedd_sources.execute(self._source_command("configure-preview", "--deny-previews"))
        self.assertFalse(denied["enabled"])
        self.assertEqual(denied["cleanup_status"], "cleanup_incomplete")
        cleanup = tedd_sources._publish_owner_preview(
            self.config, self.config.read_bytes(), prepared, cleanup=True
        )
        self.assertEqual(cleanup["status"], "owner_preview_cleanup_incomplete")
        self.assertEqual(list(destination.iterdir()), [])

    def test_owner_preparation_uses_exact_claim_lineage_and_separate_audience(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        ref, policy, request = prepared.views[0].excerpt.ref, prepared.policy, prepared.request
        before_graph = self.path.read_bytes()
        source_key = owner._manifest(ref.manifest_sha256).metadata.logical_key
        before_history = owner._history(source_key)
        with v4.open_store(self.path, readonly=True) as reader:
            again = prepare_source_views(reader, owner, request, policy)
            self.assertEqual(again, prepared)
            self.assertEqual(prepared.views[0].excerpt.units[0].text, "Only after review.")
            self.assertEqual(prepared.views[0].title, "Neutral saved note")
            self.assertEqual(prepared.views[0].memory_ids, (self.claim_id,))
            for handle, selected in (
                (self.archive, policy),
                (owner, replace(policy, enabled=False)),
                (owner, replace(policy, scope=replace(self.scope, generation="other"))),
            ):
                with self.subTest(handle=handle), self.assertRaises(SourceArchiveError):
                    prepare_source_views(reader, handle, request, selected)
        result = tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.assertEqual(result["status"], "owner_preview_published")
        self.assertNotIn(b"Only after review", encode(result))
        self.assertNotIn(b"Neutral saved note", encode(result))
        note = (
            Path(policy.vault_path)
            / policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        self.assertIn("Only after review.", note.read_text(encoding="utf-8"))
        original_note = note.read_bytes(), note.stat().st_ino
        config = json.loads(snapshot)
        config["source_preview_policy"]["enabled"] = False
        source_files.write_bytes(self.config, encode(config), overwrite=True)
        with self.assertRaises(SourceArchiveError):
            tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        cleared = tedd_sources._publish_owner_preview(
            self.config, encode(config), prepared, cleanup=True
        )
        if _PREVIEW_MUTATION:
            self.assertEqual(cleared["status"], "owner_preview_cleared")
            self.assertEqual(cleared["cleanup_status"], "cleaned")
            self.assertFalse(note.exists())
        else:
            self.assertEqual(cleared["status"], "owner_preview_cleanup_incomplete")
            self.assertEqual(cleared["cleanup_status"], "cleanup_incomplete")
            self.assertGreater(cast(int, cleared["guarded_mutation_unavailable_files"]), 0)
            self.assertEqual((note.read_bytes(), note.stat().st_ino), original_note)
        with self.assertRaises(SourceArchiveError):
            tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.assertEqual(self.path.read_bytes(), before_graph)
        self.assertEqual(
            owner._history(source_key),
            before_history,
        )

    def test_owner_batch_coalesces_exact_sources_and_links_every_memory(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        with v4.open_store(self.path) as writer:
            claim = writer.get_record(self.claim_id)
            surface = writer.get_record(
                str(cast(dict[str, object], claim["payload"])["surface_ref"])
            )
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "surface_second",
                        str(surface["collection"]),
                        {
                            **cast(dict[str, object], surface["payload"]),
                            "id": "surface_second",
                            "key": "second",
                        },
                    ),
                    v4.RecordChange(
                        "claim_second",
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "id": "claim_second",
                            "surface_ref": "surface_second",
                            "key": "second",
                        },
                    ),
                ],
                fingerprint="second-preview-claim",
            )
        requests = (prepared.request, replace(prepared.request, record_id="claim_second"))
        with v4.open_store(self.path, readonly=True) as reader:
            batch = prepare_source_views(reader, owner, requests, prepared.policy)
        self.assertEqual(len(batch.views), 1)
        self.assertEqual(batch.views[0].memory_ids, (self.claim_id, "claim_second"))
        result = tedd_sources._publish_owner_preview(self.config, snapshot, batch)
        self.assertEqual(result["status"], "owner_preview_published")
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        source_notes = list((root / "20 Evidence").glob("source-*.md"))
        self.assertEqual(len(source_notes), 1)
        memories = [
            p.read_text(encoding="utf-8") for p in root.rglob("*.md") if p not in source_notes
        ]
        self.assertGreaterEqual(
            sum(f"source-{batch.views[0].binding}|" in body for body in memories), 2
        )
        self.assertNotIn(b"Only after review", encode(result))

    def test_owner_batch_refuses_mixed_identity_and_exhausted_shared_budget(self) -> None:
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, _ = self._owner_preview_fixture()
        with v4.open_store(self.path, readonly=True) as reader:
            for requests in (
                (),
                (prepared.request,) * 65,
                (
                    prepared.request,
                    replace(prepared.request, session=replace(self.session, run_id="other")),
                ),
                (replace(prepared.request, max_chars=1),),
            ):
                with self.subTest(requests=len(requests)), self.assertRaises(SourceArchiveError):
                    prepare_source_views(reader, owner, requests, prepared.policy)

    def test_owner_preparation_refuses_unavailable_required_lineage(self) -> None:
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, _ = self._owner_preview_fixture()
        with v4.open_store(self.path) as writer:
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "source_missing",
                        "sources",
                        {"id": "source_missing", "uri": "fixture:missing", "lifecycle": "active"},
                    ),
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "evidence_refs": [self.source_id, "source_missing"],
                        },
                        expected_revision=claim["revision"],
                    ),
                ],
                fingerprint="missing-required-source",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                prepare_source_views(reader, owner, prepared.request, prepared.policy)

    def test_owner_pdf_publication_copies_only_the_exact_rendered_page(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_contracts import SourceDetails
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        original = b"%PDF-1.7 synthetic approved page plus UNAPPROVED-PAGE-MARKER"
        ref = self.archive.publish(
            payload=original,
            metadata=replace(
                self.metadata,
                source_id="classified-pdf",
                kind="pdf",
                capture_method="local_file",
                coverage="partial",
                details=SourceDetails(
                    "local",
                    "owner",
                    "file",
                    "original",
                    "reader",
                    sensitivity="ordinary",
                ),
            ),
            units=(SourceUnit("u1", "Approved saved page", 1, kind="page"),),
        ).ref
        with v4.open_store(self.path) as writer:
            source, claim = writer.get_record(self.source_id), writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.source_id,
                        "sources",
                        {**cast(dict[str, object], source["payload"]), "uri": ref.as_uri()},
                        expected_revision=source["revision"],
                    ),
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        cast(dict[str, object], claim["payload"]),
                        expected_revision=claim["revision"],
                    ),
                ],
                fingerprint="classified-page",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(reader, owner, prepared.request, prepared.policy)
        png = b"\x89PNG\r\n\x1a\napproved-page-only"
        response = {
            "status": "rendered",
            "page": 1,
            "original_sha256": digest(original),
            "png_sha256": digest(png),
            "png_base64": base64.b64encode(png).decode(),
            "resources": "bounded",
        }

        def worker(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(kwargs["input"], original)
            return subprocess.CompletedProcess([], 0, encode(response))

        renderer_process = SimpleNamespace(
            run=worker,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        # This synthetic worker checks projection validation only; bypassing the
        # host gate here does not qualify native worker execution on macOS.
        with (
            mock.patch.object(tedd_sources, "require_worker_boundary", return_value=None),
            mock.patch.object(tedd_sources, "subprocess", renderer_process),
        ):
            result = tedd_sources._publish_owner_preview(
                self.config,
                snapshot,
                prepared,
                reader_python=Path(sys.executable),
                renderer=Path(sys.executable),
            )
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        images = list(root.rglob("*.png"))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].read_bytes(), png)
        self.assertEqual(list(root.rglob("*.pdf")), [])
        for path in root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"UNAPPROVED-PAGE-MARKER", path.read_bytes())
        self.assertNotIn(b"approved-page-only", encode(result))
        self.assertNotIn(b"png_base64", encode(result))
        note = next((root / "20 Evidence").glob("source-*.md")).read_text(encoding="utf-8")
        self.assertIn(images[0].name, note)
        self.assertIn("Coverage: partial", note)
        self.assertIn("not the whole document", note)
        response["page"] = 2
        with (
            mock.patch.object(tedd_sources, "require_worker_boundary", return_value=None),
            mock.patch.object(tedd_sources, "subprocess", renderer_process),
        ):
            with self.assertRaises(SourceArchiveError):
                tedd_sources._publish_owner_preview(
                    self.config,
                    snapshot,
                    prepared,
                    reader_python=Path(sys.executable),
                    renderer=Path(sys.executable),
                )
        self.assertEqual(images[0].read_bytes(), png)

        # A repeated exact request also cannot silently retire its saved image.
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.assertEqual(images[0].read_bytes(), png)

        # An unrelated refresh reuses the exact authenticated PNG without a worker.
        image_before = images[0].read_bytes(), images[0].stat().st_ino
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            unrelated = prepare_source_views(
                reader,
                owner,
                replace(prepared.request, record_id="claim_second", max_versions=1),
                prepared.policy,
            )
        with mock.patch.object(
            tedd_sources,
            "_render_pdf_png",
            side_effect=AssertionError("unchanged PDF was rerendered"),
        ):
            self._publish_refresh_or_verify_posix_refusal(snapshot, unrelated)
        self.assertEqual((images[0].read_bytes(), images[0].stat().st_ino), image_before)

    def _second_owner_source(
        self, owner: Archive, prepared: PreparedSourceProjection, *, number: int = 2
    ) -> SourceRef:
        label = "second" if number == 2 else f"number_{number}"
        source_id = "source_aaaaaaaaaaaaaaaaaaaa" if number == 2 else f"source_number_{number}"
        metadata = owner._manifest(prepared.views[0].excerpt.ref.manifest_sha256).metadata
        ref = self.archive.publish(
            payload=b"Other independent condition.",
            metadata=replace(metadata, source_id=f"{label}-file", kind="text"),
            units=(SourceUnit("u1", "Other independent condition.", 1),),
        ).ref
        with v4.open_store(self.path) as writer:
            claim = cast(dict[str, object], writer.get_record(self.claim_id)["payload"])
            source = cast(dict[str, object], writer.get_record(self.source_id)["payload"])
            surface = writer.get_record(str(claim["surface_ref"]))
            writer.apply_changes(
                [
                    v4.RecordChange(
                        source_id,
                        "sources",
                        {**source, "id": source_id, "uri": ref.as_uri()},
                    ),
                    v4.RecordChange(
                        f"surface_{label}",
                        str(surface["collection"]),
                        {
                            **cast(dict[str, object], surface["payload"]),
                            "id": f"surface_{label}",
                            "key": label,
                        },
                    ),
                    v4.RecordChange(
                        f"claim_{label}",
                        "claims",
                        {
                            **claim,
                            "id": f"claim_{label}",
                            "surface_ref": f"surface_{label}",
                            "key": label,
                            "evidence_refs": [source_id],
                            "value": "Other independent condition.",
                        },
                    ),
                ],
                fingerprint=f"{label}-independent-source",
            )
        return ref

    def test_successive_disjoint_refreshes_keep_six_versions_and_shared_links(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        primary = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        before = primary.read_bytes(), primary.stat().st_ino
        for number in range(2, 7):
            self._second_owner_source(owner, prepared, number=number)
            label = "second" if number == 2 else f"number_{number}"
            with v4.open_store(self.path, readonly=True) as reader:
                current = prepare_source_views(
                    reader,
                    owner,
                    replace(prepared.request, record_id=f"claim_{label}", max_versions=1),
                    prepared.policy,
                )
            if self._publish_refresh_or_verify_posix_refusal(snapshot, current) is None:
                return
            self.assertEqual(len(list((root / "20 Evidence").glob("source-*.md"))), number)
            self.assertEqual((primary.read_bytes(), primary.stat().st_ino), before)
        with v4.open_store(self.path) as writer:
            record = writer.get_record("claim_second")
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "claim_second",
                        "claims",
                        {
                            **cast(dict[str, object], record["payload"]),
                            "evidence_refs": ["source_aaaaaaaaaaaaaaaaaaaa", self.source_id],
                        },
                        expected_revision=record["revision"],
                    )
                ],
                fingerprint="shared-retained-link",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            current = prepare_source_views(
                reader,
                owner,
                replace(prepared.request, record_id="claim_number_6", max_versions=1),
                prepared.policy,
            )
        if self._publish_refresh_or_verify_posix_refusal(snapshot, current) is None:
            return
        self.assertEqual(len(list((root / "20 Evidence").glob("source-*.md"))), 6)
        links = next(
            line
            for line in primary.read_text(encoding="utf-8").splitlines()
            if line.startswith("- Linked memories: ")
        )
        self.assertEqual(
            set(links.removeprefix("- Linked memories: ").split(",")),
            {self.claim_id, "claim_second"},
        )

    def test_disjoint_owner_refresh_preserves_existing_source_links(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            second = prepare_source_views(
                reader, owner, replace(prepared.request, record_id="claim_second"), prepared.policy
            )
        if self._publish_refresh_or_verify_posix_refusal(snapshot, second) is None:
            return
        evidence = Path(prepared.policy.vault_path) / prepared.policy.root_name / "20 Evidence"
        self.assertEqual(len(list(evidence.glob("source-*.md"))), 2)
        self.assertTrue((evidence / f"source-{prepared.views[0].binding}.md").is_file())

    def test_restriction_cleans_only_selected_unmodified_copies_and_stops_stale_jobs(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        second_ref = self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(
                reader,
                owner,
                (prepared.request, replace(prepared.request, record_id="claim_second")),
                prepared.policy,
            )
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        first_note = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        second_note = root / "20 Evidence" / f"source-{prepared.views[1].binding}.md"
        second_bytes = second_note.read_bytes()
        before = self.path.read_bytes()
        history = {
            path: path.read_bytes()
            for folder in ("versions", "heads", "blobs", "readings")
            for path in (self.archive.scoped / folder).iterdir()
        }
        result = tedd_sources._restrict_owner_preview(
            self.config, snapshot, prepared, prepared.views[0].excerpt.ref
        )
        self.assertEqual(first_note.exists(), not _PREVIEW_MUTATION)
        self.assertEqual(
            result["cleanup_status"], "cleaned" if _PREVIEW_MUTATION else "cleanup_incomplete"
        )
        self.assertEqual(second_note.read_bytes(), second_bytes)
        self.assertEqual(
            owner.read(second_ref, operation="owner_preview").units[0].text,
            "Other independent condition.",
        )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(b"condition", encode(result))
        with self.assertRaises(SourceArchiveError):
            tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.archive.recover()
        self.assertEqual({path: path.read_bytes() for path in history if path.exists()}, history)

    def test_disable_cleans_other_copies_even_when_owner_edited_one_note(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        note = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        edited = note.read_bytes() + b"\nOwner annotation.\n"
        source_files.write_bytes(note, edited, overwrite=True)
        result = tedd_sources._execute(
            argparse.Namespace(config=str(self.config), command="disable")
        )
        self.assertEqual(note.read_bytes(), edited)
        self.assertGreater(cast(dict[str, int], result["preview_cleanup"])["preserved_files"], 0)
        remaining = b"".join(p.read_bytes() for p in root.rglob("*.md") if p != note)
        self.assertEqual(b"## Saved sources" in remaining, not _PREVIEW_MUTATION)
        self.assertEqual(
            cast(dict[str, object], result["preview_cleanup"])["cleanup_status"],
            "cleanup_incomplete",
        )
        self.assertFalse(json.loads(self.config.read_bytes())["source_preview_policy"]["enabled"])
        with self.assertRaises(SourceArchiveError):
            tedd_sources._publish_owner_preview(self.config, snapshot, prepared)

    def test_forget_cleans_selected_previews_without_erasing_other_source_copies(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        self.ref = self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(
                reader,
                owner,
                (prepared.request, replace(prepared.request, record_id="claim_second")),
                prepared.policy,
            )
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name / "20 Evidence"
        first = root / f"source-{prepared.views[0].binding}.md"
        second = root / f"source-{prepared.views[1].binding}.md"
        first_bytes = first.read_bytes()
        impact = self.forget()
        result = self.forget(approve=str(impact["impact_sha256"]))
        self.assertEqual(
            result["status"], "forgotten" if _PREVIEW_MUTATION else "forget_incomplete"
        )
        self.assertEqual(second.exists(), not _PREVIEW_MUTATION)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(
            cast(dict[str, int], result["preview_cleanup"])["failed_files"] == 0, _PREVIEW_MUTATION
        )

    def test_owner_byte_accessors_do_not_admit_unknown_legacy_sensitivity(self) -> None:
        from tedd_knowledge_graph.source_authority import SourceAccess

        owner = Archive.open(
            self.archive.root,
            scope=self.scope,
            access=SourceAccess(self.scope, "owner", ("ordinary",), local_retention=True),
        )
        for read in (lambda: owner.original(self.ref), lambda: owner.read(self.ref)):
            with self.subTest(read=read), self.assertRaises(SourceArchiveError):
                read()

    def _assert_damaged_preview_cannot_block_denial(self, operation: str, damage: str) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.legacy_projection import MAX_PREVIEW_FILE_BYTES
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        self.ref = self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(
                reader,
                owner,
                (prepared.request, replace(prepared.request, record_id="claim_second")),
                prepared.policy,
            )
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        damaged = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        original = damaged.read_bytes()
        expected = {
            "utf8": b"\xffOwner annotation.",
            "size": b"x" * (MAX_PREVIEW_FILE_BYTES + 1),
            "read": original,
        }[damage]
        source_files.write_bytes(damaged, expected, overwrite=True)
        identity = damaged.stat().st_ino
        approval = str(self.forget()["impact_sha256"]) if operation == "forget" else None
        before_graph = self.path.read_bytes()
        read_bytes = source_files.read_bytes

        def unreadable(path: Path, maximum: int) -> bytes:
            if path == damaged:
                # Assert the actual state at the first presentation read, not
                # merely its eventual state after an exception is caught.
                with self.assertRaises(SourceArchiveError) as already_denied:
                    owner.read(self.ref, operation="owner_preview")
                self.assertEqual(already_denied.exception.code, "scope_denied")
            if damage == "read" and path == damaged:
                raise PermissionError("private preview path must not escape")
            return read_bytes(path, maximum)

        with mock.patch.object(source_files, "read_bytes", side_effect=unreadable):
            try:
                result = (
                    self.forget(approve=approval)
                    if operation == "forget"
                    else tedd_sources._restrict_owner_preview(
                        self.config, snapshot, prepared, self.ref
                    )
                )
            except (OSError, ValueError) as error:
                result = {"unexpected_error": type(error).__name__}
        reopened = Archive.open(self.archive.root, scope=self.scope, access=owner.access)
        with self.assertRaises(SourceArchiveError) as denied:
            reopened.read(self.ref, operation="owner_preview")
        self.assertEqual(denied.exception.code, "scope_denied")
        self.assertEqual(
            reopened.read(prepared.views[0].excerpt.ref, operation="owner_preview").units[0].text,
            "Only after review.",
        )
        self.assertEqual(damaged.read_bytes(), expected)
        self.assertEqual(damaged.stat().st_ino, identity)
        cleanup = (
            cast(dict[str, object], result["preview_cleanup"]) if operation == "forget" else result
        )
        self.assertEqual(cleanup["cleanup_status"], "cleanup_incomplete")
        self.assertGreater(cast(int, cleanup["failed_files"]), 0)
        self.assertEqual(cleanup["deleted_files"], 0)
        self.assertNotIn(b"Owner annotation", encode(result))
        self.assertNotIn(b"private preview path", encode(result))
        self.assertNotIn(str(damaged).encode(), encode(result))
        self.assertEqual(self.config.read_bytes(), snapshot)
        if operation == "forget":
            self.assertEqual(result["status"], "forget_incomplete")
            with v4.open_store(self.path, readonly=True) as reader:
                self.assertEqual(
                    cast(
                        dict[str, object],
                        reader.get_record("source_aaaaaaaaaaaaaaaaaaaa")["payload"],
                    )["lifecycle"],
                    "redacted",
                )
        else:
            self.assertEqual(result["status"], "source_restricted")
            self.assertEqual(self.path.read_bytes(), before_graph)

    def test_restriction_denies_before_unrelated_invalid_utf8_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("restrict", "utf8")

    def test_restriction_denies_before_unrelated_oversized_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("restrict", "size")

    def test_restriction_denies_before_unrelated_unreadable_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("restrict", "read")

    def test_forget_denies_before_unrelated_invalid_utf8_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("forget", "utf8")

    def test_forget_denies_before_unrelated_oversized_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("forget", "size")

    def test_forget_denies_before_unrelated_unreadable_inventory(self) -> None:
        self._assert_damaged_preview_cannot_block_denial("forget", "read")

    def test_restriction_does_not_classify_archive_corruption_as_preview_failure(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        owner, prepared, snapshot = self._owner_preview_fixture()
        ref = prepared.views[0].excerpt.ref
        path = self.archive.scoped / "versions" / ref.manifest_sha256
        original = path.read_bytes()
        source_files.write_bytes(path, b"invalid private archive metadata", overwrite=True)
        with self.assertRaises(SourceArchiveError):
            tedd_sources._restrict_owner_preview(self.config, snapshot, prepared, ref)
        source_files.write_bytes(path, original, overwrite=True)
        self.assertEqual(
            owner.read(ref, operation="owner_preview").units[0].text, "Only after review."
        )
        self.assertEqual(self.config.read_bytes(), snapshot)

    def test_restriction_denies_with_invalid_policy_and_reports_incomplete_cleanup(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        owner, prepared, snapshot = self._owner_preview_fixture()
        config = json.loads(snapshot)
        config["source_preview_policy"]["audience"] = "model"
        changed = encode(config)
        source_files.write_bytes(self.config, changed, overwrite=True)
        ref = prepared.views[0].excerpt.ref
        result = tedd_sources._restrict_owner_preview(self.config, changed, prepared, ref)
        self.assertEqual(result["status"], "source_restricted")
        self.assertEqual(result["cleanup_status"], "cleanup_incomplete")
        with self.assertRaises(SourceArchiveError):
            owner.read(ref, operation="owner_preview")
        self.assertEqual(self.config.read_bytes(), changed)

    def test_forget_retries_after_manifest_erasure_and_repeated_success_preserve_other_copies(
        self,
    ) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        self.ref = self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(
                reader,
                owner,
                (prepared.request, replace(prepared.request, record_id="claim_second")),
                prepared.policy,
            )
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        unrelated = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        unrelated_bytes = unrelated.read_bytes()
        if not _PREVIEW_MUTATION:
            # POSIX guarded cleanup is explicitly unavailable. Simulate the
            # owner's separate removal of these disposable selected copies so
            # manifest-erasure retry is still exercised on this platform.
            selected = "source-" + prepared.views[1].binding
            for path in unrelated.parent.parent.rglob("*.md"):
                if path.stem == selected or (selected + "|").encode() in path.read_bytes():
                    path.unlink()
        manifest_path = self.archive.scoped / "versions" / self.ref.manifest_sha256
        blob = (
            self.archive.scoped
            / "blobs"
            / self.archive._manifest(self.ref.manifest_sha256).payload_sha256
        )
        unlink = Path.unlink

        def fail_blob(path: Path, *, missing_ok: bool = False) -> None:
            if path == blob:
                self.assertFalse(manifest_path.exists())
                raise PermissionError("synthetic blob removal failure")
            unlink(path, missing_ok=missing_ok)

        approval = str(self.forget()["impact_sha256"])
        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_blob):
            failed = self.forget(approve=approval)
        self.assertEqual(failed["status"], "forget_incomplete")
        self.assertFalse(manifest_path.exists())
        self.assertTrue(blob.exists())
        fresh = str(self.forget()["impact_sha256"])
        self.assertNotEqual(fresh, approval)
        with self.assertRaises(SourceArchiveError):
            self.forget(approve=approval)
        self.assertEqual(self.forget(approve=fresh)["status"], "forgotten")
        self.assertFalse(blob.exists())
        repeated = str(self.forget()["impact_sha256"])
        self.assertEqual(self.forget(approve=repeated)["status"], "forgotten")
        self.assertEqual(unrelated.read_bytes(), unrelated_bytes)

    def test_restriction_preserves_other_links_on_the_same_memory_note(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path) as writer:
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "evidence_refs": [self.source_id, "source_aaaaaaaaaaaaaaaaaaaa"],
                        },
                        expected_revision=claim["revision"],
                    )
                ],
                fingerprint="shared-memory-links",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(reader, owner, prepared.request, prepared.policy)
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        notes = [path for path in root.rglob("*.md") if b"## Saved sources" in path.read_bytes()]
        self.assertEqual(len(notes), 1)
        result = tedd_sources._restrict_owner_preview(
            self.config, snapshot, prepared, prepared.views[0].excerpt.ref
        )
        body = notes[0].read_text(encoding="utf-8")
        self.assertEqual(f"source-{prepared.views[0].binding}|" in body, not _PREVIEW_MUTATION)
        self.assertIn(f"source-{prepared.views[1].binding}|", body)
        self.assertEqual(
            result["cleanup_status"], "cleaned" if _PREVIEW_MUTATION else "cleanup_incomplete"
        )

    def test_cleanup_identity_uses_signed_other_anchor_after_archive_erasure(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        metadata = owner._manifest(prepared.views[0].excerpt.ref.manifest_sha256).metadata
        first = self.archive.publish(
            payload=b"Heading.\nOnly after review.",
            metadata=replace(metadata, source_id="two-unit-file"),
            units=(SourceUnit("u1", "Heading.", 1), SourceUnit("u2", "Only after review.", 2)),
        ).ref
        second = replace(first, anchor_id="u2")
        with v4.open_store(self.path) as writer:
            source = writer.get_record(self.source_id)
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.source_id,
                        "sources",
                        {
                            **cast(dict[str, object], source["payload"]),
                            "uri": second.as_uri(),
                        },
                        expected_revision=source["revision"],
                    ),
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "value": "The review condition.",
                        },
                        expected_revision=claim["revision"],
                    ),
                ],
                fingerprint="second-anchor",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(reader, owner, prepared.request, prepared.policy)
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self.archive.forget(first)
        self.assertEqual(prepared.views[0].excerpt.ref, second)
        self.assertFalse((self.archive.scoped / "versions" / first.manifest_sha256).exists())
        _, config = tedd_sources.load_config(self.config)
        bindings, manifests = tedd_sources._preview_bindings(self.archive, first, config)
        result = tedd_sources._cleanup_owner_preview_files(
            config, bindings=bindings, denied_source=(first, manifests), config_path=self.config
        )
        note = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        self.assertEqual(note.exists(), not _PREVIEW_MUTATION)
        self.assertEqual(
            result["cleanup_status"], "cleaned" if _PREVIEW_MUTATION else "cleanup_incomplete"
        )

    def test_disable_reports_cleanup_failure_and_retry_cleans_remaining_copy(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_preview_recovery import PreviewRecovery

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        note = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        mutate = source_files.mutate_owned_preview
        linux_mutate = PreviewRecovery.mutate

        def fail_one(
            path: Path, identity: tuple[int, int], expected: bytes, replacement: bytes | None
        ) -> bool:
            if path == note:
                raise PermissionError("synthetic locked note")
            return mutate(path, identity, expected, replacement)

        def fail_linux(
            recovery: PreviewRecovery,
            path: Path,
            identity: tuple[int, int],
            expected: bytes,
            replacement: bytes | None,
        ) -> bool:
            if path == note:
                raise PermissionError("synthetic locked note")
            return linux_mutate(recovery, path, identity, expected, replacement)

        with (
            mock.patch.object(source_files, "mutate_owned_preview", side_effect=fail_one),
            mock.patch.object(PreviewRecovery, "mutate", autospec=True, side_effect=fail_linux),
        ):
            result = tedd_sources._execute(
                argparse.Namespace(config=str(self.config), command="disable")
            )
        self.assertGreaterEqual(cast(dict[str, int], result["preview_cleanup"])["failed_files"], 1)
        self.assertTrue(note.exists())
        self.assertFalse(json.loads(self.config.read_bytes())["source_preview_policy"]["enabled"])
        result = tedd_sources._execute(
            argparse.Namespace(config=str(self.config), command="disable")
        )
        self.assertEqual(
            cast(dict[str, int], result["preview_cleanup"])["failed_files"] == 0, _PREVIEW_MUTATION
        )
        self.assertEqual(note.exists(), not _PREVIEW_MUTATION)

    def test_disjoint_refresh_refuses_to_evade_a_denied_prior_source(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path) as writer:
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "evidence_refs": ["source_aaaaaaaaaaaaaaaaaaaa"],
                        },
                        expected_revision=claim["revision"],
                    )
                ],
                fingerprint="changed-basis",
            )
        self.archive.deny(prepared.views[0].excerpt.ref)
        with v4.open_store(self.path, readonly=True) as reader:
            second = prepare_source_views(
                reader, owner, replace(prepared.request, record_id="claim_second"), prepared.policy
            )
        with self.assertRaises(SourceArchiveError):
            tedd_sources._publish_owner_preview(self.config, snapshot, second)

    def test_refresh_request_limit_does_not_evict_or_charge_unchanged_prior_notes(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        prior_note = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        before = prior_note.read_bytes(), prior_note.stat().st_ino
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            second = prepare_source_views(
                reader,
                owner,
                replace(prepared.request, record_id="claim_second", max_versions=1),
                prepared.policy,
            )
        result = self._publish_refresh_or_verify_posix_refusal(snapshot, second)
        if result is None:
            return
        self.assertEqual(result["status"], "owner_preview_published")
        self.assertEqual((prior_note.read_bytes(), prior_note.stat().st_ino), before)
        self.assertEqual(len(list((root / "20 Evidence").glob("source-*.md"))), 2)

    def test_changed_retained_history_consumes_current_request_budget(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        before = {
            path: (path.read_bytes(), path.stat().st_ino)
            for path in root.rglob("*")
            if path.is_file()
        }
        metadata = owner._manifest(prepared.views[0].excerpt.ref.manifest_sha256).metadata
        self.archive.publish(
            payload=b"Later observed qualification.",
            metadata=replace(metadata, source_version="v2", captured_at="2026-09-05T13:00:00Z"),
            units=(SourceUnit("u1", "Later observed qualification.", 1),),
        )
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            current = prepare_source_views(
                reader,
                owner,
                replace(prepared.request, record_id="claim_second", max_versions=1),
                prepared.policy,
            )
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            tedd_sources._publish_owner_preview(self.config, snapshot, current)
        self.assertEqual(
            {
                path: (path.read_bytes(), path.stat().st_ino)
                for path in root.rglob("*")
                if path.is_file()
            },
            before,
        )

    def test_legacy_signed_note_requires_bounded_refresh_without_rewriting_archive(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        relative = f"20 Evidence/source-{prepared.views[0].binding}.md"
        note = root / relative
        service = tedd_sources.graph._legacy_projection_service()
        body = service._source_text_body(note.read_bytes(), "k" * 64, "root_test", relative)
        assert body is not None
        legacy = "\n".join(
            line for line in body.split("\n") if not line.startswith("- History head: ")
        )
        tedd_sources.graph.atomic_write(
            note,
            service.marked_projection_content("k" * 64, "root_test", relative, legacy),
            mode=0o600,
        )
        before = note.read_bytes(), note.stat().st_ino
        archived = {path: path.read_bytes() for path in owner.scoped.rglob("*") if path.is_file()}
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path, readonly=True) as reader:
            current = prepare_source_views(
                reader,
                owner,
                replace(prepared.request, record_id="claim_second", max_versions=1),
                prepared.policy,
            )
        with self.assertRaisesRegex(SourceArchiveError, "limit_exceeded"):
            tedd_sources._publish_owner_preview(self.config, snapshot, current)
        self.assertEqual((note.read_bytes(), note.stat().st_ino), before)
        for path, payload in archived.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_cleanup_status_does_not_claim_edited_copy_was_cleared(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        note = (
            Path(prepared.policy.vault_path)
            / prepared.policy.root_name
            / "20 Evidence"
            / f"source-{prepared.views[0].binding}.md"
        )
        edited = note.read_bytes() + b"\nOwner annotation.\n"
        note.write_bytes(edited)
        config = json.loads(snapshot)
        config["source_preview_policy"]["enabled"] = False
        source_files.write_bytes(self.config, encode(config), overwrite=True)
        result = tedd_sources._publish_owner_preview(
            self.config, encode(config), prepared, cleanup=True
        )
        self.assertEqual(result["status"], "owner_preview_cleanup_incomplete")
        self.assertEqual(result["cleanup_status"], "cleanup_incomplete")
        self.assertGreater(cast(int, result["preserved_files"]), 0)
        self.assertTrue(result["distributed_copies_may_remain"])
        self.assertEqual(note.read_bytes(), edited)

    def test_owner_publication_reports_guard_unavailability_as_capability(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.legacy_projection import LegacyProjectionService

        _, prepared, snapshot = self._owner_preview_fixture()
        with mock.patch.object(
            LegacyProjectionService,
            "_project_graph_locked",
            side_effect=source_files.MutationGuardUnavailable("unavailable"),
        ):
            with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
                tedd_sources._publish_owner_preview(self.config, snapshot, prepared)

    def test_even_a_root_signed_image_filename_is_not_trusted_rendering_evidence(self) -> None:
        import tedd_sources

        _, prepared, snapshot = self._owner_preview_fixture()
        base = tedd_sources.graph._legacy_projection_service()
        filename = base.binary_path(
            "k" * 64,
            prepared.policy.projection_root_id,
            b"\x89PNG\r\n\x1a\nnot-a-worker-result",
            binding=prepared.views[0].binding,
        )
        forged = replace(prepared, views=(replace(prepared.views[0], image_path=filename),))
        with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
            tedd_sources._publish_owner_preview(self.config, snapshot, forged)
        self.assertFalse((Path(prepared.policy.vault_path) / prepared.policy.root_name).exists())

    def test_refresh_retires_sensitive_memory_link_but_preserves_its_shared_source(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, snapshot = self._owner_preview_fixture()
        self._second_owner_source(owner, prepared)
        with v4.open_store(self.path) as writer:
            claim = writer.get_record("claim_second")
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "claim_second",
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "evidence_refs": [self.source_id],
                        },
                        expected_revision=claim["revision"],
                    )
                ],
                fingerprint="shared-primary",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            prepared = prepare_source_views(
                reader,
                owner,
                (prepared.request, replace(prepared.request, record_id="claim_second")),
                prepared.policy,
            )
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        with v4.open_store(self.path) as writer:
            claim = writer.get_record(self.claim_id)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **cast(dict[str, object], claim["payload"]),
                            "sensitive": True,
                        },
                        expected_revision=claim["revision"],
                    )
                ],
                fingerprint="private-memory",
            )
        with v4.open_store(self.path, readonly=True) as reader:
            current = prepare_source_views(
                reader, owner, replace(prepared.request, record_id="claim_second"), prepared.policy
            )
        if self._publish_refresh_or_verify_posix_refusal(snapshot, current) is None:
            return
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        source_notes = list((root / "20 Evidence").glob("source-*.md"))
        self.assertEqual(len(source_notes), 1)
        self.assertIn(
            "- Linked memories: claim_second", source_notes[0].read_text(encoding="utf-8")
        )
        self.assertNotIn(self.claim_id, source_notes[0].read_text(encoding="utf-8"))
        memory_notes = [
            path.read_text(encoding="utf-8") for path in (root / "11 Claims").glob("*.md")
        ]
        self.assertEqual(sum("## Saved sources" in body for body in memory_notes), 1)

    def test_restriction_wins_during_owner_preview_preparation(self) -> None:
        from tedd_knowledge_graph.v4_source_evidence import prepare_source_views

        owner, prepared, _ = self._owner_preview_fixture()
        entered, release = threading.Event(), threading.Event()
        original = owner.read

        def pause_after_read(
            ref: SourceRef, *, max_chars: int, neighbors: bool, operation: str
        ) -> SourceExcerpt:
            excerpt = original(ref, max_chars=max_chars, neighbors=neighbors, operation=operation)
            entered.set()
            if not release.wait(5):
                raise AssertionError("neutral source preparation barrier timed out")
            return excerpt

        def prepare() -> PreparedSourceProjection:
            with v4.open_store(self.path, readonly=True) as reader:
                return prepare_source_views(reader, owner, prepared.request, prepared.policy)

        with (
            mock.patch.object(owner, "read", side_effect=pause_after_read),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(prepare)
            try:
                self.assertTrue(entered.wait(5))
                self.archive.deny(prepared.views[0].excerpt.ref)
            finally:
                release.set()
            with self.assertRaises(SourceArchiveError):
                pending.result(timeout=5)

    def test_policy_restriction_wins_against_an_inflight_prepared_preview(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.legacy_projection import LegacyProjectionService

        _, prepared, snapshot = self._owner_preview_fixture()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        before = {
            path: (path.read_bytes(), path.stat().st_ino)
            for path in root.rglob("*")
            if path.is_file()
        }
        entered, release = threading.Event(), threading.Event()
        original = tedd_sources.graph._legacy_projection_service

        def pause_before_publication() -> LegacyProjectionService:
            service = original()
            if not entered.is_set():
                entered.set()
                # The coordinator releases in finally; cleanup is not a latency gate.
                # Keep the worker paused until that actual interleaving has completed.
                release.wait()
            return service

        with (
            mock.patch.object(
                tedd_sources.graph,
                "_legacy_projection_service",
                side_effect=pause_before_publication,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(
                tedd_sources._publish_owner_preview, self.config, snapshot, prepared
            )
            try:
                self.assertTrue(entered.wait(5))
                config = json.loads(snapshot)
                config["source_preview_policy"]["enabled"] = False
                changed = encode(config)
                with tedd_sources.graph.store_lock(self.config, 5):
                    source_files.write_bytes(self.config, changed, overwrite=True)
                cleanup_result = tedd_sources._publish_owner_preview(
                    self.config, changed, prepared, cleanup=True
                )
            finally:
                release.set()
            with self.assertRaises(SourceArchiveError):
                pending.result(timeout=5)
        if _PREVIEW_MUTATION:
            self.assertEqual(cleanup_result["cleanup_status"], "cleaned")
            self.assertEqual(list((root / "20 Evidence").glob("source-*")), [])
        else:
            self.assertEqual(cleanup_result["status"], "owner_preview_cleanup_incomplete")
            self.assertEqual(cleanup_result["cleanup_status"], "cleanup_incomplete")
            self.assertGreater(cast(int, cleanup_result["guarded_mutation_unavailable_files"]), 0)
            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )
        self.assertFalse(json.loads(self.config.read_bytes())["source_preview_policy"]["enabled"])

    def test_source_access_checks_run_outside_the_graph_snapshot(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceManifest
        from tedd_knowledge_graph.v4_source_evidence import EvidenceRequest, gather

        request = EvidenceRequest(
            self.session, self.graph_scope, self.scope.subject_ids, self.claim_id
        )
        authorize = self.archive._authorize_manifest
        checked: list[str] = []
        with v4.open_store(self.path, readonly=True) as store:

            def outside_snapshot(manifest: SourceManifest, operation: str) -> None:
                self.assertFalse(
                    store.connection.in_transaction, "Provider check held a graph snapshot"
                )
                checked.append(operation)
                authorize(manifest, operation)

            with mock.patch.object(
                self.archive, "_authorize_manifest", side_effect=outside_snapshot
            ):
                result = gather(store, self.archive, request)
        self.assertTrue(checked)
        self.assertEqual(set(checked), {"evidence"})
        self.assertIn(b"This is not approval yet.", encode(result))

    def test_review_safe_projection_withholds_archive_binding_without_changing_evidence(
        self,
    ) -> None:
        integration = importlib.import_module("knowledge_graph_v4_integration")
        before = self.path.read_bytes()
        for mode in ("review_safe", "operator_full"):
            with self.subTest(mode=mode):
                vault = self.root / mode
                vault.mkdir()
                set_private_path_mode(vault, 0o700)
                integration.project_v4(
                    self.path,
                    vault=vault,
                    root_name="Tedd Knowledge",
                    mode=mode,
                    manifest_key="a" * 64,
                    root_id="b" * 32,
                    timeout_seconds=5,
                )
                notes = "\n".join(path.read_text(encoding="utf-8") for path in vault.rglob("*.md"))
                if mode == "review_safe":
                    self.assertNotIn("tedd-source://", notes)
                    self.assertIn(r"\[withheld in review\_safe mode\]", notes)
                else:
                    self.assertIn(self.ref.as_uri().replace("#", r"\#"), notes)
        self.assertEqual(self.path.read_bytes(), before)
        sources = cast(list[dict[str, object]], self.gather()["sources"])
        self.assertEqual(sources[0]["ref"], asdict(self.ref))

    def test_exact_memory_and_source_are_read_without_graph_or_source_mutation(self) -> None:
        before = self.path.read_bytes()
        source_before = {
            p.relative_to(self.archive.root): p.read_bytes()
            for p in self.archive.root.rglob("*")
            if p.is_file()
        }
        result = self.gather()
        body = encode(result)
        self.assertIn(b"Deployment approved", body)
        self.assertIn(b"This is not approval yet.", body)
        self.assertNotIn(b"verdict", body)
        self.assertEqual(result["record_id"], self.claim_id)
        self.assertTrue(result["untrusted_source_content"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            source_before,
            {
                p.relative_to(self.archive.root): p.read_bytes()
                for p in self.archive.root.rglob("*")
                if p.is_file()
            },
        )

    def test_wrong_owner_and_namespace_return_no_source_content(self) -> None:
        self.session = replace(self.session, agent_id="other")
        with self.assertRaises(SourceArchiveError):
            self.gather()
        self.session = replace(self.session, agent_id="agent")
        with v4.open_store(self.path) as store:
            entity = store.get_record("entity_task")
            payload = entity["payload"]
            assert isinstance(payload, dict)
            store.apply_changes(
                [
                    v4.RecordChange(
                        "entity_task",
                        "entities",
                        {**payload, "namespace": "other"},
                        expected_revision=entity["revision"],
                    )
                ],
                fingerprint="namespace",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=store.watermark,
            )
        with self.assertRaises(SourceArchiveError):
            self.gather()

    def test_corrupt_or_revoked_source_has_no_excerpt(self) -> None:
        self.archive.forget(self.ref)
        with self.assertRaises(SourceArchiveError):
            self.gather()

    def test_changed_source_record_does_not_substitute_a_later_uri(self) -> None:
        with v4.open_store(self.path) as store:
            source = store.get_record(self.source_id)
            payload = source["payload"]
            assert isinstance(payload, dict)
            store.apply_changes(
                [
                    v4.RecordChange(
                        self.source_id,
                        "sources",
                        {**payload, "uri": "fixture:missing"},
                        expected_revision=source["revision"],
                    )
                ],
                fingerprint="missing",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=store.watermark,
            )
        result = self.gather()
        self.assertIn(b"This is not approval yet.", encode(result))

    def test_link_only_evidence_reports_no_saved_body_with_or_without_a_claim(self) -> None:
        packet = copy.deepcopy(self.packet)
        source, claims = packet["source"], packet["claims"]
        assert isinstance(source, dict)
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        source["uri"] = "https://example.invalid/neutral-contract"
        claims[0].update(surface_id="link_only_approval", key="link_only_approval")
        with v4.open_store(self.path) as store:
            request = replace(
                self.capture_request,
                input_bytes=encode(packet),
                envelope=replace(self.capture_request.envelope, based_on_watermark=store.watermark),
            )
            plan = capture_adapter.execute_capture(store, request)
            capture_adapter.execute_capture(
                store,
                replace(
                    request,
                    dry_run=False,
                    expected_fingerprint=str(plan["fingerprint"]),
                    plan_token=str(plan["plan_token"]),
                ),
            )
            changed = plan["changed_ids"]
            assert isinstance(changed, list)
            records = store.get_records(str(value) for value in changed)
            self.claim_id = next(
                record["record_id"] for record in records if record["collection"] == "claims"
            )
        graph_before = self.path.read_bytes()
        archive_before = {path: path.read_bytes() for path in self.archive._all_files()}
        with mock.patch.object(
            self.archive, "read", side_effect=AssertionError("Unrelated body read")
        ):
            result = self.gather()
            self.assertFalse(result["complete"])
            self.assertEqual(result["unavailable_links"], 1)
            self.assertEqual(result["sources"], [])
            self.assertNotIn(b"This is not approval yet.", encode(result))
        # With no memory to follow, scoped source lookup is the supported route;
        # a bare graph source ID grants no subject ownership or archive receipt.
        self.assertEqual(self.archive.lookup("https://example.invalid/neutral-contract"), ())
        self.assertEqual(self.path.read_bytes(), graph_before)
        self.assertEqual(
            {path: path.read_bytes() for path in self.archive._all_files()}, archive_before
        )

    def test_old_claim_revision_cannot_bypass_current_redaction(self) -> None:
        with v4.open_store(self.path) as store:
            record = store.get_record(self.claim_id)
            payload = record["payload"]
            assert isinstance(payload, dict)
            store.apply_changes(
                [
                    v4.RecordChange(
                        self.claim_id,
                        "claims",
                        {
                            **payload,
                            "lifecycle": "redacted",
                            "active": False,
                            "value": "[redacted]",
                        },
                        change_kind="redact",
                        expected_revision=record["revision"],
                    )
                ],
                fingerprint="redact",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=store.watermark,
            )
        with self.assertRaises(SourceArchiveError):
            self.gather(revision=1)

    def test_public_capture_and_graph_redaction_preserve_another_claims_shared_source(self) -> None:
        packet = copy.deepcopy(self.packet)
        claims = packet["claims"]
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        claims[0].update(surface_id="surface_environment", key="environment", value="Use staging")
        cli = GRAPH_SCRIPTS / "knowledge_graph.py"
        with v4.open_store(self.path, readonly=True) as reader:
            watermark = reader.watermark
        args = [
            sys.executable,
            "-B",
            str(cli),
            "--config",
            str(self.config),
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
            "--mode",
            "operator_full",
        ]
        preview = subprocess.run(
            args + ["--dry-run"], input=encode(packet), capture_output=True, check=False
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        plan = json.loads(preview.stdout)
        applied = subprocess.run(
            args
            + [
                "--expect-fingerprint",
                plan["fingerprint"],
                "--expect-plan-token",
                plan["plan_token"],
            ],
            input=encode(packet),
            capture_output=True,
            check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        with v4.open_store(self.path, readonly=True) as reader:
            other = reader.current_claims(("surface_environment",))[0]["record_id"]
            watermark = reader.watermark
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cli),
                "--config",
                str(self.config),
                "redact",
                "--id",
                self.claim_id,
                "--producer-agent-id",
                "agent",
                "--task-id",
                "task",
                "--producer-run-id",
                "run",
                "--based-on-watermark",
                str(watermark),
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with self.assertRaises(SourceArchiveError):
            self.gather(revision=1)
        self.claim_id = str(other)
        self.assertIn(b"This is not approval yet.", encode(self.gather()))
        self.assertIn(b"shared_source_retained", result.stdout)

    def test_budget_is_for_the_whole_packet_and_no_source_truth_is_inferred(self) -> None:
        complete = self.gather()
        sources = cast(list[dict[str, object]], complete["sources"])
        self.assertEqual(sources[0]["source_uri"], self.ref.as_uri())
        full_size = len(encode(complete).decode("utf-8"))
        for budget in (1200, full_size - 1):
            with self.subTest(budget=budget):
                result = self.gather(max_chars=budget)
                self.assertLessEqual(len(encode(result).decode("utf-8")), budget)
                self.assertFalse(result["complete"])

    def test_ordinary_reads_do_not_open_sources(self) -> None:
        with v4.open_store(self.path, readonly=True) as store:

            def reads() -> bytes:
                return encode(
                    [
                        store.search("approval"),
                        store.get_record(self.claim_id),
                        store.history(self.claim_id),
                        store.context_pack("subscription"),
                    ]
                )

            baseline = reads()
            with mock.patch.object(Archive, "open", side_effect=AssertionError("archive opened")):
                self.assertEqual(reads(), baseline)

    def test_configured_owner_copies_and_archive_failures_do_not_change_retrieval(self) -> None:
        import tedd_sources

        archive, prepared, snapshot = self._owner_preview_fixture()

        def ordinary() -> bytes:
            with v4.open_store(self.path, readonly=True) as store:
                return encode(
                    [
                        store.search("approval"),
                        store.get_record(self.claim_id),
                        store.history(self.claim_id),
                        store.context_pack("subscription"),
                        dict(store.state()),
                    ]
                )

        baseline, graph_bytes = ordinary(), self.path.read_bytes()
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        ref = prepared.views[0].excerpt.ref
        writer = Archive.open(archive.root, scope=archive.scope, readonly=False)
        manifest_path = archive.scoped / "versions" / ref.manifest_sha256
        manifest_bytes = manifest_path.read_bytes()
        with mock.patch.object(Archive, "open", side_effect=AssertionError("archive opened")):
            for state in ("present", "corrupt", "absent", "denied"):
                with self.subTest(state=state):
                    if state == "corrupt":
                        manifest_path.write_bytes(b"neutral malformed manifest")
                    elif state == "absent":
                        manifest_path.unlink()
                    elif state == "denied":
                        manifest_path.write_bytes(manifest_bytes)
                        set_private_path_mode(manifest_path, 0o600)
                        writer.forget(ref)
                    self.assertEqual(ordinary(), baseline)
                    self.assertEqual(self.path.read_bytes(), graph_bytes)

    def test_source_failure_workflow_leaves_ordinary_graph_outputs_and_state_unchanged(
        self,
    ) -> None:
        """Source lifecycle must not become an implicit ordinary-recall write path."""
        session = [
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

        def invoke(status: str, *arguments: str) -> dict[str, object]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(GRAPH_SCRIPTS / "tedd_sources.py"),
                    "--config",
                    str(self.config),
                    *arguments,
                    *session,
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.stderr, b"")
            self.assertEqual(
                result.returncode, 1 if status in ("limit_exceeded", "source_corrupt") else 0
            )
            value = cast(dict[str, object], json.loads(result.stdout))
            self.assertEqual(value["status"], status)
            return value

        def ordinary() -> bytes:
            with v4.open_store(self.path, readonly=True) as store:
                return encode(
                    [
                        store.search("approval"),
                        store.get_record(self.claim_id),
                        store.history(self.claim_id),
                        store.context_pack("subscription"),
                        dict(store.state()),
                    ]
                )

        baseline, graph_bytes = ordinary(), self.path.read_bytes()
        acquisition = self.root / "workflow-input.json"
        metadata_path = self.root / "workflow-metadata.json"
        metadata = replace(
            self.metadata,
            source_id="workflow_case",
            coverage="partial",
            gaps=("earlier viewport unavailable",),
        )
        payload = encode(
            {
                "format": 1,
                "source_id": "workflow_case",
                "source_version": "v1",
                "coverage": "partial",
                "gaps": ["earlier viewport unavailable"],
                "delivery": "sent",
                "messages": [
                    {
                        "id": "workflow_message",
                        "speaker": None,
                        "text": "Observed only. Not full context.",
                        "time": None,
                        "reply_to": None,
                        "links": [],
                    }
                ],
            }
        )
        capture_args = (
            "capture-chat",
            "--input",
            str(acquisition),
            "--metadata",
            str(metadata_path),
            "--acquired-sha256",
            digest(payload),
            "--workflow-state",
            "active",
        )
        # Disabled capture cannot even inspect the deliberately absent inputs.
        invoke("inactive", *capture_args)
        self.assertEqual(ordinary(), baseline)
        self.assertEqual(self.path.read_bytes(), graph_bytes)
        acquisition.write_bytes(payload)
        metadata_path.write_bytes(encode(asdict(metadata)))
        set_private_path_mode(acquisition, 0o600)
        set_private_path_mode(metadata_path, 0o600)
        invoke("configured", "configure", "--max-chat-bytes", "10")
        invoke("enabled", "init", "--enable", "--archive-root", str(self.archive.root))
        invoke("limit_exceeded", *capture_args)
        self.assertEqual(ordinary(), baseline)
        self.assertEqual(self.path.read_bytes(), graph_bytes)
        invoke("configured", "configure", "--max-chat-bytes", "8388608")
        receipt = invoke("preserved", *capture_args)
        self.assertEqual(receipt["coverage"], "partial")
        self.assertEqual(ordinary(), baseline)
        self.assertEqual(self.path.read_bytes(), graph_bytes)
        reference = cast(dict[str, str], receipt["ref"])
        ref = SourceRef(**reference)
        excerpt = Archive.open(self.archive.root, scope=self.scope).read(ref)
        self.assertEqual(excerpt.gaps, ("earlier viewport unavailable",))
        self.assertEqual(excerpt.units[0].text, "Observed only. Not full context.")
        manifest = json.loads((self.archive.scoped / "versions" / ref.manifest_sha256).read_bytes())
        recipe = json.loads(
            (self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]).read_bytes()
        )
        (self.archive.scoped / "blobs" / recipe["messages"][0]).write_bytes(b"synthetic corruption")
        invoke("source_corrupt", "inspect", "--source-uri", ref.as_uri())
        self.assertEqual(ordinary(), baseline)
        self.assertEqual(self.path.read_bytes(), graph_bytes)

    def test_capture_rejects_an_unvalidated_source_binding_even_on_replay(self) -> None:
        with v4.open_store(self.path) as store:
            with self.assertRaises(SourceArchiveError):
                capture_adapter.execute_capture(
                    store, replace(self.capture_request, source_config_path=None)
                )
        self.archive.forget(self.ref)
        with v4.open_store(self.path) as store:
            with self.assertRaises(SourceArchiveError):
                capture_adapter.execute_capture(store, self.capture_request)

    def test_whitespace_cannot_bypass_reserved_source_binding_on_replay(self) -> None:
        for prefix in (" ", "\t", "\n", "\u00a0"):
            with self.subTest(prefix=repr(prefix)), v4.open_store(self.path) as store:
                packet = copy.deepcopy(self.packet)
                source = packet["source"]
                assert isinstance(source, dict)
                source["uri"] = prefix + self.ref.as_uri()
                with self.assertRaises(SourceArchiveError):
                    capture_adapter.execute_capture(
                        store,
                        replace(
                            self.capture_request,
                            input_bytes=encode(packet),
                            source_config_path=None,
                        ),
                    )

    def test_scope_checks_include_owners_and_relation_endpoints(self) -> None:
        module = importlib.import_module("tedd_knowledge_graph.v4_source_evidence")
        for collection, extra in (
            ("open_loops", {"owner_refs": ["entity_other"]}),
            ("relations", {"source": "entity_task", "target": "entity_other"}),
        ):
            with self.subTest(collection=collection), self.assertRaises(SourceArchiveError):
                module._within(
                    {
                        "collection": collection,
                        "payload_available": True,
                        "payload": {"entity_refs": ["entity_task"], **extra},
                    },
                    self.scope,
                )

    def test_capture_binding_errors_never_include_private_configuration_paths(self) -> None:
        with mock.patch(
            "tedd_sources.load_config", side_effect=ValueError("C:/private/secret-path")
        ):
            with v4.open_store(self.path) as store:
                with self.assertRaises(SourceArchiveError) as caught:
                    capture_adapter.execute_capture(store, self.capture_request)
                self.assertNotIn("private", str(caught.exception))

    def test_source_evidence_public_cli_requires_exact_owner_and_returns_passages(self) -> None:
        cli = GRAPH_SCRIPTS / "knowledge_graph.py"
        command = [
            sys.executable,
            "-B",
            str(cli),
            "--config",
            str(self.config),
            "source-evidence",
            "--id",
            self.claim_id,
            "--consumer-id",
            "consumer",
            "--subscription-id",
            "subscription",
            "--producer-agent-id",
            "agent",
            "--task-id",
            "task",
            "--producer-run-id",
            "run",
            "--subject-id",
            "entity_task",
        ]
        result = subprocess.run(command, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(result.stderr)
        packet = json.loads(result.stdout)
        self.assertEqual(packet["record_id"], self.claim_id)
        self.assertIn(b"This is not approval yet.", result.stdout)
        limited = subprocess.run(
            command + ["--max-chars", "1200"], capture_output=True, timeout=15, check=False
        )
        self.assertEqual(limited.returncode, 0, limited.stdout + limited.stderr)
        self.assertLessEqual(len(limited.stdout.decode("utf-8")), 1200)
        self.assertFalse(json.loads(limited.stdout)["complete"])
        for flag, value in (
            ("--max-chars", "0"),
            ("--max-chars", "50000"),
            ("--max-versions", "10"),
        ):
            invalid = subprocess.run(
                command + [flag, value], capture_output=True, timeout=15, check=False
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn(b"limit_exceeded", invalid.stdout + invalid.stderr)
            self.assertNotIn(b"source_corrupt", invalid.stdout + invalid.stderr)

    def test_owner_change_at_graph_commit_cannot_publish_a_bound_capture(self) -> None:
        packet = copy.deepcopy(self.packet)
        claims = packet["claims"]
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        claims[0].update(surface_id="surface_new", key="new", value="Neutral new claim")
        with v4.open_store(self.path) as store:
            request = replace(self.capture_request, input_bytes=encode(packet))
            preview = capture_adapter.execute_capture(store, request)
            apply = mock.Mock(wraps=store.apply_changes)

            def changed(*args: object, **kwargs: object) -> object:
                with v4.open_store(self.path) as writer:
                    writer.rebind_consumer(
                        "consumer",
                        task_id="task",
                        expected_old_agent_id="agent",
                        new_agent_id="other",
                    )
                applied: object = apply(*args, **kwargs)
                return applied

            watermark = store.watermark
            with mock.patch.object(store, "apply_changes", side_effect=changed):
                with self.assertRaises(SourceArchiveError):
                    capture_adapter.execute_capture(
                        store,
                        replace(
                            request,
                            dry_run=False,
                            expected_fingerprint=str(preview["fingerprint"]),
                            plan_token=str(preview["plan_token"]),
                        ),
                    )
            self.assertEqual(store.watermark, watermark)

    def test_permission_change_during_source_read_releases_no_excerpt(self) -> None:
        read = self.archive.read

        def changed(
            ref: SourceRef, *, max_chars: int, max_versions: int, neighbors: bool, operation: str
        ) -> SourceExcerpt:
            result = read(
                ref,
                max_chars=max_chars,
                max_versions=max_versions,
                neighbors=neighbors,
                operation=operation,
            )
            with v4.open_store(self.path) as writer:
                writer.rebind_consumer(
                    "consumer",
                    task_id="task",
                    expected_old_agent_id="agent",
                    new_agent_id="replacement",
                )
            return result

        with mock.patch.object(self.archive, "read", side_effect=changed):
            with self.assertRaises(SourceArchiveError):
                self.gather()

    def test_revocation_after_excerpt_read_is_checked_before_return(self) -> None:
        read = self.archive.read

        def revoked(
            ref: SourceRef, *, max_chars: int, max_versions: int, neighbors: bool, operation: str
        ) -> SourceExcerpt:
            result = read(
                ref,
                max_chars=max_chars,
                max_versions=max_versions,
                neighbors=neighbors,
                operation=operation,
            )
            self.archive.forget(self.ref)
            return result

        with mock.patch.object(self.archive, "read", side_effect=revoked):
            with self.assertRaises(SourceArchiveError):
                self.gather()

    def test_corrupt_bytes_never_become_a_confident_missing_source_result(self) -> None:
        blob = next((self.archive.scoped / "blobs").iterdir())
        blob.write_bytes(b"corruption")
        with self.assertRaises(SourceArchiveError):
            self.gather()

    def test_later_captured_retraction_is_read_and_not_only_listed(self) -> None:
        self.archive.publish(
            payload=b'{"later":"retraction"}',
            metadata=replace(self.metadata, source_version="v2", interaction_id="later"),
            units=(SourceUnit("u1", "The earlier proposal is withdrawn.", 1),),
        )
        result = self.gather()
        self.assertIn(b"The earlier proposal is withdrawn.", encode(result))
        self.assertIn(b"later_capture", encode(result))

    def test_retraction_published_during_evidence_read_requires_a_fresh_read(self) -> None:
        self.assertTrue(self.gather()["complete"])
        read = self.archive.read

        def publish_after_read(
            ref: SourceRef, *, max_chars: int, max_versions: int, neighbors: bool, operation: str
        ) -> SourceExcerpt:
            result = read(
                ref,
                max_chars=max_chars,
                max_versions=max_versions,
                neighbors=neighbors,
                operation=operation,
            )
            self.archive.publish(
                payload=b'{"later":"withdrawn"}',
                metadata=replace(self.metadata, source_version="v2", interaction_id="later"),
                units=(SourceUnit("u1", "The earlier proposal is withdrawn.", 1),),
            )
            return result

        with mock.patch.object(self.archive, "read", side_effect=publish_after_read):
            with self.assertRaises(SourceArchiveError) as caught:
                self.gather()
        self.assertEqual(caught.exception.code, "capture_incomplete")
        refreshed = self.gather()
        self.assertTrue(refreshed["complete"])
        self.assertIn(b"The earlier proposal is withdrawn.", encode(refreshed))

    def test_retraction_published_during_inspect_requires_fresh_navigation(self) -> None:
        import tedd_sources

        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.config),
                "inspect",
                "--source-uri",
                self.ref.as_uri(),
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
        )
        self.assertEqual(tedd_sources.execute(args)["later_versions"], ())
        execute = tedd_sources._execute

        def publish_after_read(selected: argparse.Namespace) -> dict[str, object]:
            result = execute(selected)
            self.archive.publish(
                payload=b'{"later":"withdrawn"}',
                metadata=replace(self.metadata, source_version="v2", interaction_id="later"),
                units=(SourceUnit("u1", "The earlier proposal is withdrawn.", 1),),
            )
            return result

        with mock.patch.object(tedd_sources, "_execute", side_effect=publish_after_read):
            with self.assertRaises(SourceArchiveError) as caught:
                tedd_sources.execute(args)
        self.assertEqual(caught.exception.code, "capture_incomplete")
        self.assertEqual(
            len(cast(tuple[object, ...], tedd_sources.execute(args)["later_versions"])), 1
        )

    def test_reviewed_replacement_is_shown_when_checking_the_old_claim(self) -> None:
        packet = copy.deepcopy(self.packet)
        claims = packet["claims"]
        assert isinstance(claims, list) and isinstance(claims[0], dict)
        claims[0].update(
            value="Security approval remains required",
            conflict_policy="replace",
            valid_from="2026-09-05T12:01:00Z",
        )
        with v4.open_store(self.path) as store:
            request = replace(
                self.capture_request,
                input_bytes=encode(packet),
                allow_replace=True,
                envelope=replace(self.capture_request.envelope, based_on_watermark=store.watermark),
            )
            preview = capture_adapter.execute_capture(store, request)
            capture_adapter.execute_capture(
                store,
                replace(
                    request,
                    dry_run=False,
                    expected_fingerprint=str(preview["fingerprint"]),
                    plan_token=str(preview["plan_token"]),
                ),
            )
        result = self.gather(revision=1)
        self.assertIn(b"Security approval remains required", encode(result["memory"]))
        self.assertIn(b"Deployment approved", encode(result["memory"]))

    def test_unread_attachment_and_unknown_reply_are_explicit_remaining_context(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceLink

        self.archive.publish(
            payload=b'{"later":"linked context"}',
            metadata=replace(self.metadata, source_version="v2", interaction_id="linked"),
            units=(
                SourceUnit(
                    "u1",
                    "The second option.",
                    1,
                    reply_to="missing-message",
                    links=(SourceLink("attachment", "manual", "v2", None, None),),
                ),
            ),
        )
        result = self.gather()
        self.assertFalse(result["complete"])
        count = result["remaining_context_links"]
        assert isinstance(count, int)
        self.assertGreater(count, 0)

    def test_partial_source_remains_explicit_with_or_without_a_memory_reference(self) -> None:
        partial = self.archive.publish(
            payload=b'{"later":"partial context"}',
            metadata=replace(
                self.metadata,
                source_version="v2",
                interaction_id="partial",
                coverage="partial",
                gaps=("earlier page unavailable",),
            ),
            units=(SourceUnit("u1", "Observed only. Not full context.", 1),),
        )
        graph_before = self.path.read_bytes()
        archive_before = {path: path.read_bytes() for path in self.archive._all_files()}
        self.assertEqual(self.archive.read(partial.ref).gaps, ("earlier page unavailable",))
        result = self.gather()
        self.assertFalse(result["complete"])
        self.assertIn(b"earlier page unavailable", encode(result))
        self.assertIn(b"Observed only. Not full context.", encode(result))
        self.assertEqual(self.path.read_bytes(), graph_before)
        self.assertEqual(
            {path: path.read_bytes() for path in self.archive._all_files()}, archive_before
        )

    def test_assistant_authorship_survives_linked_source_read(self) -> None:
        self.archive.publish(
            payload=b'{"later":"assistant report"}',
            metadata=replace(
                self.metadata,
                source_version="v2",
                interaction_id="authorship",
                authorship="assistant",
            ),
            units=(
                SourceUnit("u1", "An assistant's interpretation, not independent confirmation.", 1),
            ),
        )
        self.assertIn(b'"authorship":"assistant"', encode(self.gather()))

    def test_invalid_request_budget_is_not_misreported_as_source_corruption(self) -> None:
        with self.assertRaises(SourceArchiveError) as caught:
            self.gather(max_versions=10)
        self.assertEqual(caught.exception.code, "limit_exceeded")

    def forget(self, *, approve: str | None = None) -> dict[str, object]:
        import tedd_sources

        command = [
            "--config",
            str(self.config),
            "forget",
            "--source-uri",
            self.ref.as_uri(),
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
        if approve is not None:
            command += ["--confirm-forget", "--expect-impact", approve]
        return tedd_sources.execute(tedd_sources.parser().parse_args(command))

    def restrict(self, **changes: object) -> dict[str, object]:
        import tedd_sources

        arguments = {
            "config": str(self.config),
            "command": "restrict",
            "source_uri": self.ref.as_uri(),
            "consumer_id": "consumer",
            "producer_agent_id": "agent",
            "task_id": "task",
            "producer_run_id": "run",
            "subscription_id": "subscription",
            "subject_id": ["entity_task"],
        }
        return tedd_sources.execute(argparse.Namespace(**{**arguments, **changes}))

    def test_public_restriction_needs_no_body_reads_and_blocks_evidence_without_graph_mutation(
        self,
    ) -> None:
        from tedd_knowledge_graph import source_files

        before = self.path.read_bytes(), self.config.read_bytes()
        history = {path: path.read_bytes() for path in self.archive._all_files()}
        read_bytes = source_files.read_bytes

        def reject_body(path: Path, maximum: int) -> bytes:
            if path.parent.name in ("blobs", "readings"):
                raise AssertionError("Restriction attempted a source-body read")
            return read_bytes(path, maximum)

        with mock.patch.object(source_files, "read_bytes", autospec=True, side_effect=reject_body):
            self.assertEqual(self.restrict()["status"], "source_restricted")
            self.assertEqual(self.restrict()["failed_files"], 0)
        self.assertEqual({path: path.read_bytes() for path in history}, history)
        self.assertEqual((self.path.read_bytes(), self.config.read_bytes()), before)
        with self.assertRaises(SourceArchiveError) as denied:
            self.gather()
        self.assertEqual(denied.exception.code, "scope_denied")

    def test_public_restriction_rejects_wrong_authority_and_identity_before_writes(self) -> None:
        before = {path: path.read_bytes() for path in self.archive._all_files()}
        graph_before = self.path.read_bytes()
        for changes in (
            {"consumer_id": "other"},
            {"producer_agent_id": "other"},
            {"task_id": "other"},
            {"subscription_id": "other"},
            {"subject_id": ["entity_other"]},
            {"source_uri": replace(self.ref, scope_id="0" * 64).as_uri()},
            {"source_uri": replace(self.ref, archive_id="0" * 32).as_uri()},
            {"source_uri": replace(self.ref, anchor_id="u999").as_uri()},
        ):
            with self.subTest(changes=changes), self.assertRaises((SourceArchiveError, v4.V4Error)):
                self.restrict(**changes)
            self.assertEqual(
                {path: path.read_bytes() for path in self.archive._all_files()}, before
            )
            self.assertEqual(self.path.read_bytes(), graph_before)

    def test_public_restriction_preserves_copies_when_policy_is_malformed(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph import source_files

        _, prepared, snapshot = self._owner_preview_fixture()
        self.ref = prepared.views[0].excerpt.ref
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        copies = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        config = json.loads(snapshot)
        config["source_preview_policy"] = "malformed"
        source_files.write_bytes(self.config, encode(config), overwrite=True)
        result = self.restrict()
        self.assertEqual(result["status"], "source_restricted")
        self.assertEqual(result["cleanup_status"], "cleanup_incomplete")
        self.assertGreater(cast(int, result["failed_files"]), 0)
        self.assertEqual({path: path.read_bytes() for path in copies}, copies)

    def test_public_restriction_rejects_archive_store_or_generation_mismatch(self) -> None:
        from tedd_knowledge_graph import source_files

        config = json.loads(self.config.read_bytes())
        for field, scope in (
            ("store_id", replace(self.scope, store_id="other")),
            ("generation", replace(self.scope, generation="other")),
        ):
            with self.subTest(field=field):
                other = Archive.initialize(self.root / ("other-" + field), scope=scope)
                source_files.write_bytes(
                    self.config,
                    encode({**config, "source_archive_root": str(other.root)}),
                    overwrite=True,
                )
                before = {path: path.read_bytes() for path in other._all_files()}
                with self.assertRaises(SourceArchiveError) as denied:
                    self.restrict()
                self.assertEqual(denied.exception.code, "scope_denied")
                self.assertEqual({path: path.read_bytes() for path in other._all_files()}, before)
        self.assertFalse(tuple((self.archive.scoped / "denials").iterdir()))

    def test_internal_restriction_checks_prepared_source_snapshot_and_scope_before_writes(
        self,
    ) -> None:
        import tedd_sources

        _, prepared, snapshot = self._owner_preview_fixture()
        ref = prepared.views[0].excerpt.ref
        history = {path: path.read_bytes() for path in self.archive._all_files()}
        for current_snapshot, current_prepared, current_ref in (
            (snapshot + b" ", prepared, ref),
            (snapshot, prepared, self.ref),
            (
                snapshot,
                replace(
                    prepared,
                    policy=replace(prepared.policy, scope=replace(self.scope, generation="other")),
                ),
                ref,
            ),
        ):
            with self.subTest(ref=current_ref), self.assertRaises(SourceArchiveError):
                tedd_sources._restrict_owner_preview(
                    self.config, current_snapshot, current_prepared, current_ref
                )
            self.assertEqual(
                {path: path.read_bytes() for path in self.archive._all_files()}, history
            )

    def test_public_restriction_cleans_valid_policy_and_retains_later_history(self) -> None:
        import tedd_sources

        owner, prepared, snapshot = self._owner_preview_fixture()
        self.ref = prepared.views[0].excerpt.ref
        tedd_sources._publish_owner_preview(self.config, snapshot, prepared)
        root = Path(prepared.policy.vault_path) / prepared.policy.root_name
        note = root / "20 Evidence" / f"source-{prepared.views[0].binding}.md"
        graph_before = self.path.read_bytes()
        history = {path: path.read_bytes() for path in self.archive._all_files()}
        result = self.restrict()
        self.assertEqual(result["status"], "source_restricted")
        self.assertEqual(note.exists(), not _PREVIEW_MUTATION)
        self.assertEqual(self.path.read_bytes(), graph_before)
        self.archive.recover()
        self.assertEqual({path: path.read_bytes() for path in history}, history)
        with self.assertRaises(SourceArchiveError):
            owner.read(self.ref, operation="owner_preview")

    def test_restrict_after_forget_reports_erasure_without_restoring_history(self) -> None:
        self.archive.forget(self.ref)
        result = self.restrict()
        self.assertEqual(result["status"], "source_restricted")
        self.assertEqual(result["history_disposition"], "erase")
        self.assertFalse(tuple((self.archive.scoped / "versions").iterdir()))

    def test_restriction_expansion_failure_reports_possible_denial_without_cleanup(self) -> None:
        from tedd_knowledge_graph.source_archive import _Denial

        expand = Archive._expand_denial

        def interrupted(archive: Archive, denial: _Denial, *, publish: bool = True) -> _Denial:
            if publish:
                raise SourceArchiveError("source_unavailable")
            return expand(archive, denial, publish=publish)

        with mock.patch.object(Archive, "_expand_denial", autospec=True, side_effect=interrupted):
            result = self.restrict()
        self.assertEqual(result["status"], "restriction_incomplete")
        self.assertTrue(result["source_may_be_denied"])
        with self.assertRaises(SourceArchiveError) as denied:
            self.archive.read(self.ref)
        self.assertEqual(denied.exception.code, "scope_denied")
        self.archive.recover()
        self.assertEqual(self.restrict()["history_disposition"], "retain")

    def test_whole_source_forget_requires_reviewed_impact_and_keeps_interpretations(self) -> None:
        before = self.path.read_bytes()
        impact = self.forget()
        self.assertEqual(impact["status"], "forget_preview")
        self.assertIn(self.claim_id, str(impact["retained_memories"]))
        self.assertEqual(self.path.read_bytes(), before)
        result = self.forget(approve=str(impact["impact_sha256"]))
        self.assertEqual(result["status"], "forgotten")
        with v4.open_store(self.path, readonly=True) as reader:
            source = reader.get_record(self.source_id)
            source_payload = source["payload"]
            assert isinstance(source_payload, dict)
            self.assertEqual(source_payload["lifecycle"], "redacted")
            self.assertFalse(reader.get_record(self.source_id, revision=1)["payload_available"])
            claim = reader.get_record(self.claim_id)["payload"]
            assert isinstance(claim, dict)
            self.assertEqual(claim["value"], "Deployment approved")
        with self.assertRaises(SourceArchiveError):
            self.gather()

    def test_forget_cli_reports_unknown_sharing_when_target_recipe_is_missing(self) -> None:
        import tedd_sources

        raw = self.archive.original(self.ref)
        parsed = json.loads(raw)
        parsed["source_id"] = "survivor"
        payload = encode(parsed)
        survivor = capture_chat(
            self.archive,
            payload=payload,
            metadata=replace(self.metadata, source_id="survivor"),
            acquired_sha256=digest(payload),
        )
        manifest = json.loads(
            (self.archive.scoped / "versions" / self.ref.manifest_sha256).read_bytes()
        )
        (self.archive.scoped / "readings" / manifest["chat_recipe_sha256"]).unlink()
        preview = self.forget()
        command = [
            "--config",
            str(self.config),
            "forget",
            "--source-uri",
            self.ref.as_uri(),
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
            "--confirm-forget",
            "--expect-impact",
            str(preview["impact_sha256"]),
        ]
        captured = io.BytesIO()
        output = io.TextIOWrapper(captured, encoding="utf-8")
        self.addCleanup(output.close)
        with mock.patch.object(tedd_sources.sys, "stdout", output):
            status = tedd_sources.main(command)
        receipt = json.loads(captured.getvalue())
        self.assertEqual(status, 0, receipt)
        self.assertEqual(receipt["status"], "forgotten")
        self.assertIsNone(receipt["shared_content_retained"])
        self.assertTrue(receipt["backup_copies_may_remain"])
        self.assertEqual(self.archive.original(survivor.ref), payload)
        with self.assertRaises(SourceArchiveError):
            self.archive.original(self.ref)

    def test_forget_rejects_stale_approval_without_denying_source(self) -> None:
        preview = self.forget()
        self.archive.publish(
            payload=b'{"later":"source"}',
            metadata=replace(self.metadata, source_version="v2"),
            units=self.units,
        )
        with self.assertRaises(SourceArchiveError):
            self.forget(approve=str(preview["impact_sha256"]))
        self.assertEqual(self.archive.read(self.ref).units, self.units)

    def test_forget_cli_returns_failure_with_resumable_receipt_after_erasure_error(self) -> None:
        import tedd_sources

        preview = self.forget()
        args = [
            "--config",
            str(self.config),
            "forget",
            "--source-uri",
            self.ref.as_uri(),
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
            "--confirm-forget",
            "--expect-impact",
            str(preview["impact_sha256"]),
        ]
        captured = io.BytesIO()
        output = io.TextIOWrapper(captured, encoding="utf-8")
        self.addCleanup(output.close)
        with (
            mock.patch.object(Archive, "forget", side_effect=OSError("synthetic erase failure")),
            mock.patch.object(tedd_sources.sys, "stdout", output),
        ):
            status = tedd_sources.main(args)
        receipt = json.loads(captured.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(receipt["status"], "forget_incomplete")
        self.assertTrue(receipt["fresh_impact_required"])
        self.assertTrue(receipt["source_may_be_denied"])
        with self.assertRaises(SourceArchiveError):
            self.archive.read(self.ref)

    def test_forget_refuses_mixed_source_history_without_deleting_either(self) -> None:
        with v4.open_store(self.path) as writer:
            item = writer.get_record(self.source_id)
            payload = item["payload"]
            assert isinstance(payload, dict)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        self.source_id,
                        "sources",
                        {**payload, "uri": "fixture:unrelated"},
                        expected_revision=item["revision"],
                    )
                ],
                fingerprint="mixed",
                based_on_watermark=writer.watermark,
            )
        with self.assertRaises(SourceArchiveError):
            self.forget()
        self.assertEqual(self.archive.read(self.ref).units, self.units)

    def test_denied_forget_resumes_after_graph_commit_failure_and_archive_recovery(self) -> None:
        from tedd_knowledge_graph import v4_transactions

        with v4.open_store(self.path) as writer:
            for start in (0, 1000, 2000):
                writer.apply_changes(
                    [
                        v4.RecordChange(
                            f"source_unrelated_{index}",
                            "sources",
                            {
                                "id": f"source_unrelated_{index}",
                                "uri": "fixture:unrelated",
                                "lifecycle": "active",
                            },
                        )
                        for index in range(start, min(start + 1000, 2001))
                    ],
                    fingerprint=f"unrelated-{start}",
                    based_on_watermark=writer.watermark,
                )
        preview = self.forget()
        with mock.patch.object(
            v4_transactions, "_prepare_mutation", side_effect=OSError("neutral failure")
        ):
            result = self.forget(approve=str(preview["impact_sha256"]))
        self.assertEqual(result["status"], "forget_incomplete")
        with self.assertRaises(SourceArchiveError):
            self.archive.read(self.ref)
        self.archive.recover()
        preview = self.forget()
        self.assertIn(self.source_id, str(preview["graph_evidence"]))
        result = self.forget(approve=str(preview["impact_sha256"]))
        self.assertEqual(result["status"], "forgotten")

    def test_forget_refuses_observation_history_from_an_unrelated_source(self) -> None:
        self._assert_observation_history_is_preserved("source_other")

    def test_forget_refuses_independent_observation_history(self) -> None:
        self._assert_observation_history_is_preserved(None)

    def _assert_observation_history_is_preserved(self, prior_link: str | None) -> None:
        with v4.open_store(self.path) as writer:
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "source_other",
                        "sources",
                        {"id": "source_other", "uri": "fixture:other", "lifecycle": "active"},
                    ),
                    v4.RecordChange(
                        "observation_mixed",
                        "observations",
                        {
                            "id": "observation_mixed",
                            "source_ref": prior_link,
                            "entity_refs": ["entity_task"],
                            "summary": "Other evidence",
                            "lifecycle": "active",
                            "sensitive": False,
                        },
                    ),
                ],
                fingerprint="observation-before",
                based_on_watermark=writer.watermark,
            )
            old = writer.get_record("observation_mixed")
            payload = old["payload"]
            assert isinstance(payload, dict)
            writer.apply_changes(
                [
                    v4.RecordChange(
                        "observation_mixed",
                        "observations",
                        {**payload, "source_ref": self.source_id, "summary": "Selected evidence"},
                        expected_revision=old["revision"],
                    )
                ],
                fingerprint="observation-after",
                based_on_watermark=writer.watermark,
            )
        with self.assertRaises(SourceArchiveError):
            self.forget()
        self.assertEqual(self.archive.read(self.ref).units, self.units)

    def test_new_shared_claim_after_preview_is_not_silently_included(self) -> None:
        preview = self.forget()
        with v4.open_store(self.path) as store:
            claim = store.get_record(self.claim_id)["payload"]
            assert isinstance(claim, dict)
            store.apply_changes(
                [
                    v4.RecordChange(
                        "observation_new",
                        "observations",
                        {
                            "id": "observation_new",
                            "source_ref": self.source_id,
                            "entity_refs": ["entity_task"],
                            "summary": "Neutral additional evidence",
                            "lifecycle": "active",
                            "sensitive": False,
                        },
                    )
                ],
                fingerprint="new-link",
                based_on_watermark=store.watermark,
            )
        with self.assertRaises(SourceArchiveError):
            self.forget(approve=str(preview["impact_sha256"]))
        self.assertEqual(self.archive.read(self.ref).units, self.units)


if __name__ == "__main__":
    unittest.main()
