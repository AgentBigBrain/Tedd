"""Host-dependent commands use real disposable stores, not JSON access grants."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import threading
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture_adapter
import tedd_sources
from tedd_knowledge_graph import source_files
from tedd_knowledge_graph.source_archive import Archive
from tedd_knowledge_graph.source_authority import AccessDecision, ProviderCheck, access_binding
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceExcerpt,
    SourceLocation,
    SourceMetadata,
    SourceRef,
    SourceScope,
    SourceUnit,
    decode_metadata,
    digest,
    encode,
    parse_json,
)
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.v4_application_contracts import GraphReadSession, JsonObject
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, ProducerEnvelope

from tests.knowledge_graph import test_source_cli


class SourceHostTests(unittest.TestCase):
    def test_real_command_adapter_disconnect_blocks_disposable_graph_publication(self) -> None:
        self._adapter_disconnect(cancel_confirmation=False)

    def test_real_command_adapter_cancelled_disconnect_preserves_disposable_capture(self) -> None:
        self._adapter_disconnect(cancel_confirmation=True)

    def test_recovery_cannot_reauthorize_an_old_capture_after_parsing(self) -> None:
        self._adapter_disconnect(cancel_confirmation=False, recovery_boundary="capture")

    def test_recovery_cannot_reauthorize_an_old_owner_handoff_after_reading(self) -> None:
        self._adapter_disconnect(cancel_confirmation=False, recovery_boundary="owner")

    def _adapter_disconnect(
        self, *, cancel_confirmation: bool, recovery_boundary: str | None = None
    ) -> None:
        from tedd_knowledge_graph import source_google, source_google_oauth
        from tedd_knowledge_graph.source_credentials import CredentialStore
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_sources_mcp import SourceOperations

        _, metadata, payload = self._google_capture()
        fixture = self.fixture
        credential_root = source_files.private_root(fixture.root / "credentials", create=True)
        credentials = CredentialStore(credential_root)
        client = source_google_oauth.DesktopClient("neutral.apps.googleusercontent.com", "")
        client_path = fixture.root / "client.json"
        source_files.write_bytes(
            client_path,
            encode(
                {
                    "installed": {
                        "client_id": client.client_id,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
        )
        key = source_google_oauth.credential_key(client, "owner@example.com")
        saved: dict[str, bytes] = {}
        for patcher in (
            mock.patch.object(credentials, "get", side_effect=saved.get),
            mock.patch.object(credentials, "put", side_effect=saved.__setitem__),
            mock.patch.object(
                credentials, "delete", side_effect=lambda name: saved.pop(name, None)
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        connection = GoogleConnection(credentials, key)
        connection.replace(connection.snapshot(), b'{"refresh_token":"neutral"}', new_grant=True)

        def signed_in(*_args: object, **_kwargs: object) -> source_google.GoogleSourceSession:
            return source_google.GoogleSourceSession(
                "neutral",
                "google:123",
                frozenset({metadata.source_id}),
                metadata.scope,
                10**12,
                _connection=connection,
                _stamp=connection.snapshot().stamp,
            )

        if recovery_boundary == "owner":
            fixture.invoke(
                "configure-owner-view",
                "--allow-owner-view",
                "--sensitivity",
                "ordinary",
                *fixture.session,
            )
        operations = SourceOperations(
            fixture.config, client_path, "owner@example.com", credentials=credentials
        )
        self.addCleanup(operations.close)
        server = SourceMcpServer(operations, control=operations.control)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
        )
        server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, {"threadId": "thread"})
        call_metadata = {
            "threadId": "thread",
            "callId": "capture",
            "x-codex-turn-metadata": {
                "thread_id": "thread",
                "session_id": "thread",
                "turn_id": "turn",
            },
        }
        server.lifecycle.acquire(call_metadata).google_files = frozenset({metadata.source_id})
        entered, release = threading.Event(), threading.Event()
        replies: queue.Queue[JsonObject] = queue.Queue()

        def pause() -> None:
            entered.set()
            if not release.wait(10):
                raise RuntimeError("synthetic operation did not resume")

        read_document = tedd_sources.read_document
        read_archive = Archive.read

        def after_parsing(
            payload: bytes, kind: str, *, maximum_bytes: int
        ) -> tedd_sources.DocumentReading:
            reading = read_document(payload, kind, maximum_bytes=maximum_bytes)
            if recovery_boundary == "capture" and not release.is_set():
                pause()
            return reading

        def after_reading(
            archive: Archive, ref: SourceRef, *, operation: str, max_chars: int
        ) -> SourceExcerpt:
            excerpt = read_archive(archive, ref, operation=operation, max_chars=max_chars)
            if recovery_boundary == "owner" and not release.is_set():
                pause()
            return excerpt

        def transport(url: str, **_kwargs: object) -> bytes:
            if "/drive/v3/files/" in url:
                return encode(
                    {
                        "id": metadata.source_id,
                        "version": "7",
                        "trashed": False,
                        "mimeType": "application/vnd.google-apps.document",
                        "capabilities": {
                            "canCopy": True,
                            "canDownload": True,
                            "canAccessViaGenAi": True,
                        },
                    }
                )
            if recovery_boundary is None:
                pause()
            return payload

        def send(name: str, request_id: int, arguments: JsonObject) -> None:
            gate = (
                server._control_busy
                if name in {"disconnect_google", "reconnect_google"}
                else server._busy
            )
            self.assertTrue(gate.acquire(timeout=5))
            gate.release()
            server.receive_line(
                encode(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": name,
                            "arguments": arguments,
                            "_meta": {**call_metadata, "callId": str(request_id)},
                        },
                    }
                ),
                replies.put,
            )

        graph_before = fixture.store.read_bytes()
        capture_arguments: JsonObject = {
            "argv": [
                "--metadata",
                str(fixture.root / "document-metadata.json"),
                "--workflow-state",
                "active",
                *fixture.session,
            ]
        }
        operation_name, operation_arguments = "capture_google", capture_arguments
        with (
            mock.patch.object(source_google_oauth, "sign_in", side_effect=signed_in) as sign_in,
            mock.patch.object(source_google, "google_request", side_effect=transport),
            mock.patch.object(tedd_sources, "read_document", side_effect=after_parsing),
            mock.patch.object(Archive, "read", autospec=True, side_effect=after_reading),
            mock.patch(
                "tedd_knowledge_graph.source_google_control.google_request", return_value=b""
            ),
            mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer,
            mock.patch.object(
                tedd_sources,
                "QUALIFIED_PRIVATE_VIEWER_PLATFORMS",
                {tedd_sources.sys.platform},
            ),
        ):
            viewer.return_value.confirm.return_value = not cancel_confirmation
            try:
                if recovery_boundary == "owner":
                    send("capture_google", 0, capture_arguments)
                    saved_result = replies.get(timeout=5)["result"]
                    assert isinstance(saved_result, dict)
                    saved_content = saved_result["structuredContent"]
                    assert isinstance(saved_content, dict)
                    saved_ref = saved_content["ref"]
                    assert isinstance(saved_ref, dict)
                    reference = SourceRef(
                        *(
                            str(saved_ref[key])
                            for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                        )
                    )
                    operation_name = "owner_view"
                    operation_arguments = {
                        "argv": [
                            "--source-uri",
                            reference.as_uri(),
                            "--private-viewer",
                            *fixture.session,
                        ]
                    }
                send(operation_name, 1, operation_arguments)
                if not entered.wait(5):
                    self.fail(f"synthetic operation missed pause: {replies.get_nowait()!r}")
                send("disconnect_google", 2, {})
                control_result = replies.get(timeout=5)
                self.assertEqual(control_result["id"], 2)
                self.assertIn(
                    "google_disconnect_cancelled" if cancel_confirmation else "google_disconnected",
                    json.dumps(control_result),
                )
                self.assertEqual(
                    viewer.return_value.confirm.call_args.kwargs["action"], "disconnect"
                )
                if recovery_boundary is not None:
                    send("reconnect_google", 3, {})
                    recovered = replies.get(timeout=5)
                    self.assertEqual(recovered["id"], 3)
                    self.assertIn("google_recovery_awaiting_consent", json.dumps(recovered))
                    # A separate actor obtains a replacement grant while the old
                    # ordinary operation remains paused on this same endpoint.
                    connection.replace(
                        connection.snapshot(), b'{"refresh_token":"replacement"}', new_grant=True
                    )
                release.set()
                capture_result = replies.get(timeout=5)
                self.assertEqual(capture_result["id"], 1)
                self.assertEqual('"preserved"' in json.dumps(capture_result), cancel_confirmation)
                if recovery_boundary is not None:
                    old_result = capture_result["result"]
                    assert isinstance(old_result, dict)
                    self.assertTrue(old_result.get("isError", False))
                    viewer.return_value.display.assert_not_called()
                    # The failed old operation must not even start fresh consent.
                    self.assertEqual(sign_in.call_count, 1)
                    send(operation_name, 4, operation_arguments)
                    fresh_result = replies.get(timeout=5)["result"]
                    assert isinstance(fresh_result, dict)
                    self.assertFalse(fresh_result.get("isError", False))
                    self.assertEqual(sign_in.call_count, 2)
                    if recovery_boundary == "owner":
                        viewer.return_value.display.assert_called_once()
            finally:
                release.set()
                self.assertTrue(server._busy.acquire(timeout=5))
                server._busy.release()
        _, config = tedd_sources.load_config(fixture.config)
        archive = tedd_sources.open_archive(
            fixture.archive_root,
            metadata.scope,
            config,
            readonly=True,
            provider_check=self.permitted,
        )
        # Preparation binds the verified principal, so use the actual receipt
        # rather than the proposal's pre-verification logical identity.
        result = capture_result["result"]
        assert isinstance(result, dict)
        content = result["structuredContent"]
        assert isinstance(content, dict)
        if cancel_confirmation:
            ref = content["ref"]
            assert isinstance(ref, dict)
            reference = SourceRef(
                *(
                    str(ref[key])
                    for key in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                )
            )
            self.assertEqual(archive.original(reference), payload)
        elif recovery_boundary is None:
            self.assertNotIn("ref", content)
            assert metadata.details is not None
            self.assertEqual(
                archive._history(
                    replace(
                        metadata, details=replace(metadata.details, principal="google:123")
                    ).logical_key
                ),
                (),
            )
        self.assertEqual(fixture.store.read_bytes(), graph_before)

    def test_document_render_cannot_bypass_model_denial(self) -> None:
        _, ref = self._saved_medical_source()
        self.fixture.invoke("configure", "--allow-local-documents", *self.fixture.session)
        try:
            args = tedd_sources.parser().parse_args(
                [
                    "--config",
                    str(self.fixture.config),
                    "render-document",
                    "--source-uri",
                    ref.as_uri(),
                    "--document-page",
                    "1",
                    *self.fixture.session,
                ]
            )
        except SourceArchiveError:
            self.fail("Saved document-page model command is missing")
        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
            tedd_sources.execute(args)

    def test_document_page_view_preserves_owner_only_permissions_and_exact_bytes(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading
        from tedd_knowledge_graph.source_office import read_office

        from tests.knowledge_graph.test_source_office import png_image, word_document

        archive, prior = self._saved_medical_source()
        payload = word_document()
        metadata = replace(
            archive._manifest(prior.manifest_sha256).metadata,
            kind="docx",
            source_id="medical-layout",
            capture_method="local_file",
        )
        reading = read_office(payload, "docx")
        ref = archive.publish(
            payload=payload,
            metadata=bind_document_reading(metadata, payload, reading),
            units=reading.units,
        ).ref
        self.fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *self.fixture.session,
        )
        command = tedd_sources.parser()
        # A missing page-view command is the expected pre-implementation failure.
        try:
            args = command.parse_args(
                [
                    "--config",
                    str(self.fixture.config),
                    "owner-view",
                    "--private-viewer",
                    "--source-uri",
                    ref.as_uri(),
                    "--document-page",
                    "2",
                    "--reader-python",
                    str(Path(tedd_sources.sys.executable)),
                    "--renderer",
                    str(Path(tedd_sources.sys.executable)),
                    "--office-converter",
                    str(Path(tedd_sources.sys.executable)),
                    "--office-work-root",
                    str(self.fixture.root / "conversion"),
                    *self.fixture.session,
                ]
            )
        except SourceArchiveError:
            self.fail("Owner-only whole-document page command is missing")
        source_files.directory(self.fixture.root / "conversion")
        before = self.fixture.store.read_bytes()
        archive_before = {
            path.relative_to(archive.root): path.read_bytes()
            for path in archive.root.rglob("*")
            if path.is_file()
        }
        seen: list[bytes] = []

        def render(raw: bytes, kind: str, page: int, **options: object) -> dict[str, object]:
            self.assertEqual((raw, kind, page), (payload, "docx", 2))
            return {
                "png_base64": __import__("base64").b64encode(png_image()).decode(),
                "png_sha256": digest(png_image()),
                "page": 2,
                "page_count": 2,
            }

        renderer = mock.Mock(side_effect=render)
        if tedd_sources.sys.platform == "darwin":
            with self.assertRaises(SourceArchiveError) as refused:
                tedd_sources.execute(
                    args, host=SourceHost(display_owner_image=lambda _ref, png: seen.append(png))
                )
            self.assertEqual(refused.exception.code, "capability_unavailable")
            renderer.assert_not_called()
            self.assertEqual(seen, [])
            self.assertEqual(self.fixture.store.read_bytes(), before)
            self.assertEqual(
                {
                    path.relative_to(archive.root): path.read_bytes()
                    for path in archive.root.rglob("*")
                    if path.is_file()
                },
                archive_before,
            )

        with (
            mock.patch.object(
                tedd_sources,
                "QUALIFIED_PRIVATE_VIEWER_PLATFORMS",
                {tedd_sources.sys.platform},
            ),
            # The renderer is already synthetic; bypassing this gate exercises
            # dispatch logic and does not qualify native macOS worker execution.
            mock.patch.object(tedd_sources, "require_worker_boundary", return_value=None),
            mock.patch("tedd_knowledge_graph.source_office_host.document_page", renderer),
        ):
            result = tedd_sources.execute(
                args, host=SourceHost(display_owner_image=lambda _ref, png: seen.append(png))
            )
        self.assertTrue(result["human_only"])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(seen, [png_image()])
        self.assertNotIn("png_base64", result)
        self.assertEqual(self.fixture.store.read_bytes(), before)
        with self.assertRaises(SourceArchiveError):
            archive.read(ref)

    def setUp(self) -> None:
        self.fixture = test_source_cli.SourceCliTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @staticmethod
    @contextmanager
    def collecting(_session: GraphReadSession, _scope: SourceScope) -> Iterator[None]:
        """An active synthetic workflow; never a live host qualification."""
        yield

    @staticmethod
    def permitted(
        source: SourceMetadata, fingerprint: str, operation: str, audience: str
    ) -> AccessDecision:
        return AccessDecision(access_binding(source, fingerprint), operation, audience, "allowed")

    def test_owner_view_approval_does_not_grant_model_or_ordinary_preview_access(self) -> None:
        fixture = self.fixture
        fixture.invoke(
            "init", "--enable", "--archive-root", str(fixture.archive_root), *fixture.session
        )
        before = json.loads(fixture.config.read_bytes())
        graph_bytes = fixture.store.read_bytes()

        result = fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )

        self.assertEqual(result["status"], "owner_view_configured")
        self.assertFalse(result["route_qualified"])
        current = json.loads(fixture.config.read_bytes())
        self.assertEqual(
            current.pop("source_owner_view_access_policy")["sensitivities"], ["medical"]
        )
        self.assertEqual(current, before)
        self.assertEqual(fixture.store.read_bytes(), graph_bytes)
        fixture.invoke("configure-owner-view", "--deny-owner-view", *fixture.session)
        self.assertIsNone(
            json.loads(fixture.config.read_bytes())["source_owner_view_access_policy"]
        )

    def test_owner_view_revocation_does_not_open_unavailable_archive(self) -> None:
        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "configure-owner-view",
                "--deny-owner-view",
                *fixture.session,
            ]
        )
        with mock.patch.object(
            Archive, "open", side_effect=AssertionError("Revocation must not read archive")
        ):
            self.assertEqual(tedd_sources.execute(args)["status"], "owner_view_disabled")
        self.assertIsNone(
            json.loads(fixture.config.read_bytes())["source_owner_view_access_policy"]
        )

    def _saved_medical_source(self) -> tuple[Archive, SourceRef]:
        fixture = self.fixture
        fixture._document_args()
        metadata = decode_metadata(
            parse_json((fixture.root / "document-metadata.json").read_bytes())
        )
        assert metadata.details is not None
        metadata = replace(metadata, details=replace(metadata.details, sensitivity="medical"))
        archive = Archive.open(fixture.archive_root, scope=metadata.scope, readonly=False)
        receipt = archive.publish(
            payload=b"Not approved.\nUnselected neutral section.",
            metadata=metadata,
            units=(
                SourceUnit(
                    "u1", "Not approved.", 1, "line", location=SourceLocation("line", None, 1, 1)
                ),
                SourceUnit(
                    "u2",
                    "Unselected neutral section.",
                    2,
                    "line",
                    location=SourceLocation("line", None, 2, 2),
                ),
            ),
        )
        return archive, receipt.ref

    def test_owner_view_without_a_qualified_host_refuses_before_source_access(self) -> None:
        fixture = self.fixture
        result = fixture.invoke(
            "owner-view",
            "--source-uri",
            "not-a-source",
            *fixture.session,
            success=False,
        )
        self.assertEqual(result, {"status": "capability_unavailable"})
        self.assertFalse(fixture.archive_root.exists())

    def test_explicit_private_viewer_cli_uses_owner_policy_and_content_free_result(self) -> None:
        _, ref = self._saved_medical_source()
        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "owner-view",
                "--private-viewer",
                "--source-uri",
                ref.as_uri(),
                *fixture.session,
            ]
        )
        graph_before = fixture.store.read_bytes()
        archive_before = {
            path.relative_to(fixture.archive_root): path.read_bytes()
            for path in fixture.archive_root.rglob("*")
            if path.is_file()
        }
        with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
            if tedd_sources.sys.platform == "darwin":
                with self.assertRaises(SourceArchiveError) as refused:
                    tedd_sources.execute_cli(args)
                self.assertEqual(refused.exception.code, "capability_unavailable")
                viewer.assert_not_called()
                self.assertEqual(fixture.store.read_bytes(), graph_before)
                self.assertEqual(
                    {
                        path.relative_to(fixture.archive_root): path.read_bytes()
                        for path in fixture.archive_root.rglob("*")
                        if path.is_file()
                    },
                    archive_before,
                )
            with mock.patch.object(
                tedd_sources,
                "QUALIFIED_PRIVATE_VIEWER_PLATFORMS",
                {tedd_sources.sys.platform},
            ):
                result = tedd_sources.execute_cli(args)
        self.assertEqual(result, {"status": "owner_view_dispatched", "human_only": True})
        displayed = viewer.return_value.display.call_args.args[0]
        self.assertEqual([unit.text for unit in displayed.units], ["Not approved."])
        viewer.return_value.close.assert_called_once()

    def test_human_only_display_selects_one_unit_and_returns_no_source_content(self) -> None:
        archive, ref = self._saved_medical_source()
        earlier = archive._manifest(ref.manifest_sha256)
        archive.publish(
            payload=b"Later neutral version.",
            metadata=replace(earlier.metadata, source_version="later"),
            units=(
                SourceUnit(
                    "u1",
                    "Later neutral version.",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 1),
                ),
            ),
        )
        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        graph_bytes = fixture.store.read_bytes()
        displayed: list[SourceExcerpt] = []
        try:
            args = tedd_sources.parser().parse_args(
                [
                    "--config",
                    str(fixture.config),
                    "owner-view",
                    "--source-uri",
                    ref.as_uri(),
                    *fixture.session,
                ]
            )
        except SourceArchiveError as error:
            self.fail(f"Owner-view dispatch is missing: {error.code}")
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", {tedd_sources.sys.platform}
        ):
            result = tedd_sources.execute(args, host=SourceHost(display_owner=displayed.append))
        self.assertEqual(result, {"status": "owner_view_dispatched", "human_only": True})
        self.assertEqual(len(displayed), 1)
        self.assertEqual([unit.text for unit in displayed[0].units], ["Not approved."])
        self.assertEqual(displayed[0].remaining_units, 1)
        self.assertEqual(displayed[0].later_versions, ())
        self.assertEqual(displayed[0].later_observations, ())
        self.assertEqual(displayed[0].remaining_versions, 1)
        self.assertEqual(fixture.store.read_bytes(), graph_bytes)
        self.assertEqual(
            fixture.invoke(
                "inspect", "--source-uri", ref.as_uri(), *fixture.session, success=False
            ),
            {"status": "scope_denied"},
        )
        self.assertEqual(
            len(archive._history(archive._manifest(ref.manifest_sha256).metadata.logical_key)), 2
        )

    def _google_capture(
        self, kind: str = "google_doc", payload: bytes | None = None
    ) -> tuple[argparse.Namespace, SourceMetadata, bytes]:
        fixture = self.fixture
        payload = (
            payload
            if payload is not None
            else encode(
                {
                    "tabs": [
                        {
                            "tabId": "tab",
                            "footnotes": {},
                            "body": {
                                "content": [
                                    {
                                        "startIndex": 0,
                                        "endIndex": 14,
                                        "paragraph": {
                                            "elements": [
                                                {"textRun": {"content": "Not approved.\n"}}
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        )
        fixture._document_args(kind, payload)
        path = fixture.root / "document-metadata.json"
        metadata = decode_metadata(parse_json(path.read_bytes()))
        assert metadata.details is not None
        metadata = replace(
            metadata,
            capture_method="structured_observation",
            details=replace(
                metadata.details,
                provider="google",
                principal="fixture-principal",
                representation="structured_observation",
                retention="access_dependent",
            ),
        )
        path.write_bytes(encode(asdict(metadata)))
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        try:
            args = tedd_sources.parser().parse_args(
                [
                    "--config",
                    str(fixture.config),
                    "capture-google",
                    "--metadata",
                    str(path),
                    "--acquired-sha256",
                    digest(payload),
                    "--workflow-state",
                    "active",
                    *fixture.session,
                ]
            )
        except SourceArchiveError as error:
            self.fail(f"Google capture dispatch is missing: {error.code}")
        return args, metadata, payload

    def test_google_transfer_uses_host_checks_and_preserves_exact_input(self) -> None:
        args, metadata, payload = self._google_capture()
        phase: list[str] = []

        @contextmanager
        def collecting(session: GraphReadSession, scope: SourceScope) -> Iterator[None]:
            self.assertEqual(
                (session.task_id, session.run_id, scope), ("task", "run", metadata.scope)
            )
            phase.append("guard")
            yield
            phase.pop()

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            self.assertEqual(phase, [])
            assert source.details is not None
            self.assertEqual(source.details.principal, "fixture-principal")
            return AccessDecision(
                access_binding(source, fingerprint), operation, audience, "allowed"
            )

        def acquire(source: SourceMetadata, maximum: int) -> bytes:
            self.assertEqual(phase, [])
            self.assertEqual(source, metadata)
            self.assertGreaterEqual(maximum, len(payload))
            return payload

        host = SourceHost(provider_check=provider, acquire_google=acquire, collecting=collecting)
        before = self.fixture.store.read_bytes()
        result = tedd_sources.execute(args, host=host)
        self.assertEqual(result["status"], "preserved")
        config = json.loads(self.fixture.config.read_bytes())
        archive = tedd_sources.open_archive(
            self.fixture.archive_root, metadata.scope, config, provider_check=provider
        )
        value = result["ref"]
        assert isinstance(value, dict)
        ref = SourceRef(**value)
        self.assertEqual(archive.original(ref), payload)
        self.assertEqual(archive.read(ref).units[0].text, "Not approved.\n")
        self.assertEqual(self.fixture.store.read_bytes(), before)

    def test_google_live_preparation_binds_checksum_after_direct_transfer(self) -> None:
        args, metadata, payload = self._google_capture()
        args.acquired_sha256 = None
        host = SourceHost(
            prepare_google=lambda source: replace(source, source_version="provider-version-7"),
            provider_check=self.permitted,
            acquire_google=lambda source, maximum: payload,
            collecting=self.collecting,
        )
        result = tedd_sources.execute(args, host=host)
        self.assertEqual(result["status"], "preserved")
        reference = result["ref"]
        assert isinstance(reference, dict)
        archive = Archive.open(self.fixture.archive_root, scope=metadata.scope)
        saved = archive._manifest(SourceRef(**reference).manifest_sha256)
        self.assertEqual(saved.payload_sha256, digest(payload))
        self.assertEqual(saved.metadata.source_version, "provider-version-7")

    def test_mcp_operations_reuse_capture_and_do_not_authenticate_inactive_work(self) -> None:
        from tedd_knowledge_graph.source_google import GoogleSourceSession
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        _, metadata, payload = self._google_capture()
        fixture = self.fixture
        argv = [
            "--metadata",
            str(fixture.root / "document-metadata.json"),
            "--workflow-state",
            "paused",
            *fixture.session,
        ]
        operation = SourceOperations(
            fixture.config, fixture.root / "client.json", "owner@example.com"
        )
        self.addCleanup(operation.close)
        session = GoogleSourceSession(
            "neutral", "google:123", frozenset({metadata.source_id}), metadata.scope, 10**12
        )
        with mock.patch(
            "tedd_knowledge_graph.source_google_oauth.sign_in", return_value=session
        ) as login:
            self.assertEqual(
                operation("capture-google", argv, SourceTurn()), {"status": "inactive"}
            )
            login.assert_not_called()
            argv[3] = "active"
            with (
                mock.patch.object(session, "prepare", return_value=metadata),
                mock.patch.object(session, "acquire", return_value=payload),
                mock.patch.object(session, "check", side_effect=self.permitted),
            ):
                result = operation("capture-google", argv, SourceTurn())
        self.assertEqual(result["status"], "preserved")
        self.assertEqual(login.call_args.kwargs["account"], "owner@example.com")

    def test_mcp_historical_local_read_needs_no_google_configuration(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        fixture = self.fixture
        arguments = fixture._document_args()
        fixture.invoke("configure", "--allow-local-documents", *fixture.session)
        receipt = fixture.invoke(*arguments)
        reference = receipt["ref"]
        assert isinstance(reference, dict)
        uri = SourceRef(**reference).as_uri()
        operation = SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        before = fixture.store.read_bytes()
        with mock.patch(
            "tedd_knowledge_graph.source_google_oauth.sign_in",
            side_effect=AssertionError("Local evidence must not open Google sign-in"),
        ):
            result = operation("inspect", ["--source-uri", uri, *fixture.session], SourceTurn())
        self.assertIn(b"Not approved. Only if reviewed.", encode(result))
        self.assertEqual(fixture.store.read_bytes(), before)

    def test_mcp_explicit_google_without_configuration_does_not_open_sign_in(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        fixture = self.fixture
        operation = SourceOperations(fixture.config)
        self.addCleanup(operation.close)
        with mock.patch(
            "tedd_knowledge_graph.source_google_oauth.sign_in",
            side_effect=AssertionError("Missing configuration must not open Google sign-in"),
        ):
            with self.assertRaises(SourceArchiveError) as failure:
                operation("list-google", ["--folder-id", "unused", *fixture.session], SourceTurn())
        self.assertEqual(failure.exception.code, "capability_unavailable")

    def test_mcp_folder_listing_is_task_bound_read_only_and_does_not_capture(self) -> None:
        from tedd_knowledge_graph.source_credentials import CredentialStore
        from tedd_knowledge_graph.source_google import GoogleSourceSession
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        _, metadata, _ = self._google_capture()
        fixture = self.fixture
        operation = SourceOperations(
            fixture.config,
            fixture.root / "client.json",
            "owner@example.com",
            credentials=mock.Mock(spec=CredentialStore, root=fixture.root / "credentials"),
        )
        self.addCleanup(operation.close)
        session = GoogleSourceSession(
            "neutral",
            "google:123",
            frozenset(),
            metadata.scope,
            10**12,
            folder_ids=frozenset({"linked-root"}),
        )
        before = fixture.store.read_bytes()
        argv = ["--folder-id", "linked-root", *fixture.session]
        with mock.patch(
            "tedd_knowledge_graph.source_google_oauth.sign_in", return_value=session
        ) as login:
            with self.assertRaises(SourceArchiveError):
                operation("list-google", argv, SourceTurn())
            login.assert_not_called()
            turn = SourceTurn(google_folders=frozenset({"linked-root"}))
            with mock.patch.object(
                session, "list_folder", return_value={"files": [], "next_page": ""}
            ) as listing:
                self.assertEqual(
                    operation("list-google", argv, turn), {"files": [], "next_page": ""}
                )
                self.assertEqual(
                    operation("list-google", argv, turn), {"files": [], "next_page": ""}
                )
                self.assertEqual(login.call_count, 1)
                self.assertEqual(listing.call_count, 2)
                self.assertEqual(login.call_args.kwargs["folder_ids"], frozenset({"linked-root"}))
                with self.assertRaises(SourceArchiveError):
                    operation("list-google", argv, SourceTurn())
                self.assertEqual(listing.call_count, 2)
        self.assertEqual(fixture.store.read_bytes(), before)
        self.assertEqual(
            Archive.open(fixture.archive_root, scope=metadata.scope)._history(metadata.logical_key),
            (),
        )

    def test_reusable_credentials_cannot_overlap_source_or_graph_storage(self) -> None:
        from tedd_knowledge_graph.source_credentials import CredentialStore
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        self._google_capture()
        fixture = self.fixture
        with self.assertRaises(SourceArchiveError):
            CredentialStore(fixture.archive_root)
        for root in (fixture.archive_root, fixture.graph_root, fixture.root):
            with self.subTest(root=root.name):
                operation = SourceOperations(
                    fixture.config,
                    fixture.root / "client.json",
                    "owner@example.com",
                    credentials=mock.Mock(spec=CredentialStore, root=root),
                )
                self.addCleanup(operation.close)
                with (
                    mock.patch("tedd_knowledge_graph.source_google_oauth.sign_in") as login,
                    self.assertRaises(SourceArchiveError),
                ):
                    operation(
                        "list-google",
                        ["--folder-id", "root", *fixture.session],
                        SourceTurn(google_folders=frozenset({"root"})),
                    )
                login.assert_not_called()

    def test_folder_listing_cancelled_or_scope_changed_during_request_releases_nothing(
        self,
    ) -> None:
        self._google_capture()
        fixture = self.fixture
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "list-google",
                "--folder-id",
                "linked-root",
                *fixture.session,
            ]
        )

        def listing(_scope: SourceScope, _folder: str, _page: str) -> dict[str, object]:
            fixture.invoke("disable")
            return {"files": [{"name": "must not escape"}], "next_page": ""}

        with self.assertRaises(SourceArchiveError):
            tedd_sources.execute(
                args, host=SourceHost(list_google=listing, collecting=self.collecting)
            )

    def test_mcp_operation_cancellation_discards_transfer_before_publication(self) -> None:
        from tedd_knowledge_graph.source_google import GoogleSourceSession
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        _, metadata, payload = self._google_capture()
        fixture = self.fixture
        operation = SourceOperations(
            fixture.config, fixture.root / "client.json", "owner@example.com"
        )
        self.addCleanup(operation.close)
        turn = SourceTurn()
        session = GoogleSourceSession(
            "neutral", "google:123", frozenset({metadata.source_id}), metadata.scope, 10**12
        )

        def acquire(source: SourceMetadata, maximum: int) -> bytes:
            turn.cancel()
            return payload

        with (
            mock.patch("tedd_knowledge_graph.source_google_oauth.sign_in", return_value=session),
            mock.patch.object(session, "prepare", return_value=metadata),
            mock.patch.object(session, "acquire", side_effect=acquire),
            mock.patch.object(session, "check", side_effect=self.permitted),
            self.assertRaises(SourceArchiveError),
        ):
            operation(
                "capture-google",
                [
                    "--metadata",
                    str(fixture.root / "document-metadata.json"),
                    "--workflow-state",
                    "active",
                    *fixture.session,
                ],
                turn,
            )
        archive = Archive.open(fixture.archive_root, scope=metadata.scope)
        self.assertEqual(archive._history(metadata.logical_key), ())
        self.assertEqual(session.selected_ids, frozenset())

    def test_mcp_google_to_memory_dispute_later_version_and_denial(self) -> None:
        from tedd_knowledge_graph.source_google import GoogleSourceSession
        from tedd_knowledge_graph.source_mcp import CodexSourceLifecycle, SourceTurn
        from tedd_sources_mcp import SourceOperations

        _, metadata, payload = self._google_capture()
        fixture = self.fixture
        operation = SourceOperations(
            fixture.config, fixture.root / "client.json", "owner@example.com"
        )
        self.addCleanup(operation.close)
        session = GoogleSourceSession(
            "neutral", "google:123", frozenset({metadata.source_id}), metadata.scope, 10**12
        )
        current = {
            "id": metadata.source_id,
            "version": "7",
            "trashed": False,
            "mimeType": "application/vnd.google-apps.document",
            "capabilities": {"canCopy": True, "canDownload": True, "canAccessViaGenAi": True},
        }

        def fetch(url: str, maximum: int) -> bytes:
            return encode(current) if "/drive/v3/files/" in url else payload

        capture = [
            "--metadata",
            str(fixture.root / "document-metadata.json"),
            "--workflow-state",
            "active",
            *fixture.session,
        ]
        with (
            mock.patch("tedd_knowledge_graph.source_google_oauth.sign_in", return_value=session),
            mock.patch.object(session, "_get", side_effect=fetch),
        ):
            saved = operation("capture-google", capture, SourceTurn())
            reference = saved["ref"]
            assert isinstance(reference, dict)
            ref = SourceRef(
                *(
                    str(reference[field])
                    for field in ("archive_id", "scope_id", "manifest_sha256", "anchor_id")
                )
            )
            packet = fixture.root / "memory-packet.json"
            source_files.write_bytes(packet, encode(self._memory_packet(ref)))
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
            plan_file = fixture.root / "memory-plan.json"
            planned = operation(
                "capture", [*memory, "--dry-run", "--plan-output", str(plan_file)], SourceTurn()
            )
            apply_args = [
                *memory,
                "--expect-fingerprint",
                str(planned["fingerprint"]),
                "--expect-plan-file",
                str(plan_file),
            ]
            lifecycle = CodexSourceLifecycle()
            self.addCleanup(lifecycle.close)
            lifecycle.link_sources(
                {
                    "session_id": "thread",
                    "turn_id": "turn",
                    "prompt": f"Use https://docs.google.com/document/d/{metadata.source_id}/edit once.",
                },
                {"threadId": "thread"},
            )
            denied = lifecycle.acquire(
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
            before_denial = fixture.store.read_bytes()
            versions = tuple((fixture.archive_root / "scopes").rglob("*.json"))
            self.assertTrue(versions)
            archived = {path: path.read_bytes() for path in versions}
            with mock.patch.object(
                session, "_get", side_effect=AssertionError("Unneeded Google read")
            ):
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    operation("capture-google", capture, denied)
                with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                    operation("capture", apply_args, denied)
            self.assertEqual(fixture.store.read_bytes(), before_denial)
            self.assertEqual({path: path.read_bytes() for path in versions}, archived)
            self.assertEqual(tuple((fixture.archive_root / "scopes").rglob("*.json")), versions)
            historical = operation(
                "inspect", ["--source-uri", ref.as_uri(), *fixture.session], denied
            )
            self.assertIn(b"Not approved.", encode(historical))
            cancelled = SourceTurn()
            check_provider = session.check

            def cancel_after_permission(
                source: SourceMetadata, checksum: str, action: str, audience: str
            ) -> AccessDecision:
                decision = check_provider(source, checksum, action, audience)
                cancelled.cancel()
                return decision

            before_apply = fixture.store.read_bytes()
            with (
                mock.patch.object(session, "check", side_effect=cancel_after_permission),
                self.assertRaises(SourceArchiveError),
            ):
                operation("capture", apply_args, cancelled)
            self.assertEqual(fixture.store.read_bytes(), before_apply)
            self.assertEqual(session.selected_ids, frozenset())
            # A fresh verified-session fixture stands in for explicit reselection;
            # never revive the cancelled turn or its discarded token.
            session = GoogleSourceSession(
                "neutral", "google:123", frozenset({metadata.source_id}), metadata.scope, 10**12
            )
            operation._google = session
            patch_get = mock.patch.object(session, "_get", side_effect=fetch)
            patch_get.start()
            self.addCleanup(patch_get.stop)
            applied = operation(
                "capture",
                apply_args,
                SourceTurn(),
            )
            self.assertEqual(applied["status"], "captured")
            with v4.open_store(fixture.store, readonly=True) as store:
                changed = planned["changed_ids"]
                assert isinstance(changed, list)
                claim = next(
                    record
                    for record in store.get_records(str(item) for item in changed)
                    if record["collection"] == "claims"
                )
                retrieval = encode(
                    store.context_pack("subscription", max_items=10, max_chars=12000)
                )
            check = ["--id", str(claim["record_id"]), *fixture.session]
            before = fixture.store.read_bytes()
            evidence = operation("source-evidence", check, SourceTurn())
            self.assertIn(b"Not approved.", encode(evidence))
            payload = payload.replace(b"Not approved.", b"Approval withdrawn.")
            current["version"] = "8"
            operation("capture-google", capture, SourceTurn())
            evidence = operation("source-evidence", check, SourceTurn())
            self.assertIn(b"Not approved.", encode(evidence))
            self.assertIn(b"Approval withdrawn.", encode(evidence))
            current["capabilities"] = {
                "canCopy": False,
                "canDownload": False,
                "canAccessViaGenAi": False,
            }
            with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                operation("source-evidence", check, SourceTurn())
            with v4.open_store(fixture.store, readonly=True) as store:
                self.assertEqual(
                    retrieval,
                    encode(store.context_pack("subscription", max_items=10, max_chars=12000)),
                )
            self.assertEqual(fixture.store.read_bytes(), before)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_private_owner_command_displays_medical_source_without_model_access(
        self,
    ) -> None:
        _, ref = self._saved_medical_source()
        self._native_owner_view(ref)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1"
        and os.environ.get("TEDD_TEST_PDF_RENDERER"),
        "explicit native PDF renderer and display check",
    )
    def test_native_private_owner_page_uses_host_renderer_without_model_access(self) -> None:
        archive, prior = self._saved_medical_source()
        metadata = replace(
            archive._manifest(prior.manifest_sha256).metadata,
            kind="pdf",
            source_id="medical-page",
            capture_method="local_file",
        )
        # A minimal, neutral one-page PDF; no optional authoring dependency in tests.
        content = b"BT /F1 18 Tf 40 200 Td (Not approved - review first.) Tj ET"
        objects = (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream",
        )
        pdf = b"%PDF-1.4\n"
        offsets = [0]
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n"
        pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
        pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        ref = archive.publish(
            payload=pdf,
            metadata=metadata,
            units=(SourceUnit("u1", "Not approved - review first.", 1, "page"),),
        ).ref
        self._native_owner_view(ref, page=True)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1" and importlib.util.find_spec("PIL"),
        "explicit native embedded-image display check with optional Pillow",
    )
    def test_native_private_office_image_uses_saved_frame_without_model_access(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading

        from tests.knowledge_graph.test_source_office import image_document, png_image

        archive, prior = self._saved_medical_source()
        raw = image_document(png_image(), "word/media/image.png")
        reading = tedd_sources.office_reading(
            raw, "docx", argparse.Namespace(reader_python=tedd_sources.sys.executable)
        )
        metadata = replace(
            archive._manifest(prior.manifest_sha256).metadata,
            kind="docx",
            source_id="medical-image",
            capture_method="local_file",
        )
        ref = archive.publish(
            payload=raw, metadata=bind_document_reading(metadata, raw, reading), units=reading.units
        ).ref
        self._native_owner_view(replace(ref, anchor_id="u4"), image=True)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1"
        and os.environ.get("TEDD_TEST_OFFICE_CONVERTER")
        and os.environ.get("TEDD_TEST_PDF_RENDERER")
        and importlib.util.find_spec("docx")
        and importlib.util.find_spec("pdfplumber")
        and importlib.util.find_spec("pypdf"),
        "explicit private whole-document display check",
    )
    def test_native_private_document_page_never_grants_model_access(self) -> None:
        from tedd_knowledge_graph.source_capture import bind_document_reading

        from tests.knowledge_graph.test_source_office import two_page_word_document

        raw = two_page_word_document()
        archive, prior = self._saved_medical_source()
        reading = tedd_sources.office_reading(
            raw, "docx", argparse.Namespace(reader_python=tedd_sources.sys.executable)
        )
        metadata = replace(
            archive._manifest(prior.manifest_sha256).metadata,
            kind="docx",
            source_id="medical-layout",
            capture_method="local_file",
        )
        ref = archive.publish(
            payload=raw, metadata=bind_document_reading(metadata, raw, reading), units=reading.units
        ).ref
        self._native_owner_view(ref, document_page=2)

    def _native_owner_view(
        self,
        ref: SourceRef,
        *,
        page: bool = False,
        image: bool = False,
        document_page: int | None = None,
    ) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_knowledge_graph.source_viewer import PrivateViewer
        from tedd_sources_mcp import SourceOperations

        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        before = fixture.store.read_bytes()
        work_root = fixture.root / "conversion"
        if document_page is not None:
            source_files.directory(work_root)
        operation = SourceOperations(
            fixture.config,
            fixture.root / "unused.json",
            "owner@example.com",
            reader_python=Path(tedd_sources.sys.executable)
            if page or image or document_page
            else None,
            renderer=Path(os.environ["TEDD_TEST_PDF_RENDERER"]) if page or document_page else None,
            office_converter=Path(os.environ["TEDD_TEST_OFFICE_CONVERTER"])
            if document_page
            else None,
            office_work_root=work_root if document_page else None,
        )
        self.addCleanup(operation.close)
        with self.assertRaises(SourceArchiveError):
            operation("inspect", ["--source-uri", ref.as_uri(), *fixture.session], SourceTurn())
        viewer = PrivateViewer()
        try:
            with mock.patch(
                "tedd_knowledge_graph.source_viewer.PrivateViewer", return_value=viewer
            ):
                result = operation(
                    "owner-view",
                    [
                        "--private-viewer",
                        "--source-uri",
                        ref.as_uri(),
                        *fixture.session,
                        *(["--page", "1"] if page else []),
                        *(["--image"] if image else []),
                        *(["--document-page", str(document_page)] if document_page else []),
                    ],
                    SourceTurn(),
                )
            self.assertEqual(
                result,
                {
                    "status": "owner_view_dispatched",
                    "human_only": True,
                    **(
                        {"page": 2, "page_count": 2, "representation": "libreoffice_rendering"}
                        if document_page
                        else {}
                    ),
                },
            )
            if document_page:
                self.assertEqual(list(work_root.iterdir()), [])
            self.assertIsNone(viewer._process.poll())
            self.assertEqual(fixture.store.read_bytes(), before)
        finally:
            # This exact process belongs to the neutral fixture, not the user's viewer.
            if viewer._process.poll() is None:
                viewer._process.terminate()
            viewer._process.wait(timeout=5)
            viewer.close()

    def test_mcp_owner_view_rejects_caller_chosen_executables_before_private_handoff(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        _, ref = self._saved_medical_source()
        fixture = self.fixture
        operation = SourceOperations(
            fixture.config, fixture.root / "unused.json", "owner@example.com"
        )
        self.addCleanup(operation.close)
        with mock.patch("tedd_knowledge_graph.source_viewer.PrivateViewer") as viewer:
            for flag in (
                "--reader-python",
                "--renderer",
                "--office-converter",
                "--office-work-root",
            ):
                with self.subTest(flag=flag), self.assertRaises(SourceArchiveError):
                    operation(
                        "owner-view",
                        [
                            "--private-viewer",
                            "--source-uri",
                            ref.as_uri(),
                            flag,
                            str(fixture.root / "untrusted-executable"),
                            *fixture.session,
                        ],
                        SourceTurn(),
                    )
            viewer.assert_not_called()

    def test_google_sheet_and_slide_routes_reuse_native_readers_without_changing_input(
        self,
    ) -> None:
        families = {
            "google_sheet": b'{"sheets":[{"properties":{"sheetId":2,"title":"Decision","hidden":false},"data":[{"startRow":0,"startColumn":0,"rowData":[{"values":[{"formattedValue":"Not approved."}]}]}]}]}',
            "google_slide": b'{"slides":[{"objectId":"slide","pageElements":[{"shape":{"text":{"textElements":[{"textRun":{"content":"Not approved."}}]}}}]}]}',
        }
        for kind, payload in families.items():
            with self.subTest(kind=kind):
                args, metadata, _ = self._google_capture(kind, payload)
                # A Sheet and a Slide are separate Google resources, not two
                # incompatible kind-based histories of the same file ID.
                metadata = replace(metadata, source_id=kind)
                Path(args.metadata).write_bytes(encode(asdict(metadata)))

                def acquire(
                    _source: SourceMetadata, _limit: int, *, saved: bytes = payload
                ) -> bytes:
                    return saved

                host = SourceHost(
                    acquire_google=acquire,
                    provider_check=self.permitted,
                    collecting=self.collecting,
                )
                receipt = tedd_sources.execute(args, host=host)
                reference = receipt["ref"]
                assert isinstance(reference, dict)
                ref = SourceRef(**reference)
                archive = tedd_sources.open_archive(
                    self.fixture.archive_root,
                    metadata.scope,
                    json.loads(self.fixture.config.read_bytes()),
                    provider_check=self.permitted,
                )
                self.assertEqual(archive.original(ref), payload)
                self.assertIn("Not approved.", archive.read(ref).units[0].text)

    def test_absent_host_paused_workflow_and_denied_provider_never_acquire(self) -> None:
        args, metadata, payload = self._google_capture()
        acquire = mock.Mock(side_effect=AssertionError("Acquisition must not start"))
        host = SourceHost(
            acquire_google=acquire, provider_check=self.permitted, collecting=self.collecting
        )
        for incomplete in (
            None,
            replace(host, acquire_google=None),
            replace(host, provider_check=None),
            replace(host, collecting=None),
        ):
            with self.subTest(host=incomplete), self.assertRaises(SourceArchiveError) as caught:
                tedd_sources.execute(args, host=incomplete)
            self.assertEqual(caught.exception.code, "capability_unavailable")
        for state in ("paused", "closed", "excluded"):
            args.workflow_state = state
            with (
                self.subTest(state=state),
                mock.patch.object(
                    Archive, "open", side_effect=AssertionError("Inactive archive I/O")
                ),
            ):
                self.assertEqual(tedd_sources.execute(args, host=host), {"status": "inactive"})
        args.workflow_state = "active"
        for decision, status in (
            (
                AccessDecision(
                    access_binding(metadata, digest(payload)), "capture", "model", "denied"
                ),
                "scope_denied",
            ),
            (
                AccessDecision(
                    access_binding(metadata, digest(payload)), "capture", "model", "unknown"
                ),
                "source_unavailable",
            ),
            (AccessDecision("0" * 64, "capture", "model", "allowed"), "scope_denied"),
            (
                AccessDecision(
                    access_binding(metadata, digest(payload)), "inspect", "model", "allowed"
                ),
                "scope_denied",
            ),
            (
                AccessDecision(
                    access_binding(metadata, digest(payload)), "capture", "owner", "allowed"
                ),
                "scope_denied",
            ),
        ):
            with self.subTest(decision=decision), self.assertRaises(SourceArchiveError) as caught:
                tedd_sources.execute(
                    args, host=replace(host, provider_check=lambda *_args, saved=decision: saved)
                )
            self.assertEqual(caught.exception.code, status)
        acquire.assert_not_called()
        self.assertEqual(
            Archive.open(self.fixture.archive_root, scope=metadata.scope)._history(
                metadata.logical_key
            ),
            (),
        )

    def test_transfer_mismatch_or_permission_revocation_cannot_publish(self) -> None:
        args, metadata, payload = self._google_capture()
        calls = 0

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            nonlocal calls
            calls += 1
            return AccessDecision(
                access_binding(source, fingerprint),
                operation,
                audience,
                "allowed" if calls == 1 else "denied",
            )

        for case in ("mismatch", "revocation", "disable", "rebind"):
            with self.subTest(case=case):
                calls = 0

                def acquire(_source: SourceMetadata, _maximum: int, case: str = case) -> bytes:
                    if case == "disable":
                        snapshot, config = tedd_sources.load_config(self.fixture.config)
                        tedd_sources.save_config(
                            self.fixture.config,
                            snapshot,
                            {**config, "source_capture_enabled": False},
                        )
                    elif case == "rebind":
                        with v4.open_store(self.fixture.store) as writer:
                            writer.rebind_consumer(
                                "consumer",
                                task_id="task",
                                expected_old_agent_id="agent",
                                new_agent_id="other",
                            )
                    return b"different" if case == "mismatch" else payload

                host = SourceHost(
                    acquire_google=acquire,
                    provider_check=provider if case == "revocation" else self.permitted,
                    collecting=self.collecting,
                )
                with self.assertRaises(SourceArchiveError) as caught:
                    tedd_sources.execute(args, host=host)
                self.assertEqual(
                    caught.exception.code,
                    "capture_incomplete" if case == "mismatch" else "scope_denied",
                )
                if case == "disable":
                    snapshot, config = tedd_sources.load_config(self.fixture.config)
                    tedd_sources.save_config(
                        self.fixture.config, snapshot, {**config, "source_capture_enabled": True}
                    )
        self.assertEqual(
            Archive.open(self.fixture.archive_root, scope=metadata.scope)._history(
                metadata.logical_key
            ),
            (),
        )

    def test_changed_scope_run_revision_or_principal_cannot_reuse_authority(self) -> None:
        args, metadata, payload = self._google_capture()
        assert metadata.details is not None
        acquire = mock.Mock(side_effect=AssertionError("Mismatched authority reached transfer"))
        decision = AccessDecision(
            access_binding(metadata, digest(payload)), "capture", "model", "allowed"
        )
        host = SourceHost(
            acquire_google=acquire,
            provider_check=lambda *_args: decision,
            collecting=self.collecting,
        )
        variants = (
            replace(metadata, scope=replace(metadata.scope, store_id="other_store")),
            replace(metadata, scope=replace(metadata.scope, generation="other_generation")),
            replace(metadata, scope=replace(metadata.scope, namespace="other_namespace")),
            replace(metadata, scope=replace(metadata.scope, subject_ids=("other_subject",))),
            replace(metadata, task_id="other_task"),
            replace(metadata, run_id="other_run"),
            replace(metadata, source_version="other_revision"),
            replace(metadata, details=replace(metadata.details, principal="other_principal")),
        )
        for changed in variants:
            with self.subTest(binding=changed.logical_key, version=changed.source_version):
                (self.fixture.root / "document-metadata.json").write_bytes(encode(asdict(changed)))
                with self.assertRaises(SourceArchiveError) as caught:
                    tedd_sources.execute(args, host=host)
                self.assertEqual(caught.exception.code, "scope_denied")
        acquire.assert_not_called()

    def test_transfer_secret_screening_and_configured_limit_prevent_publication(self) -> None:
        args, metadata, payload = self._google_capture()
        for raw, status in (
            (
                payload.replace(b"Not approved.", b"password=example-secret-value"),
                "privacy_rejected",
            ),
            (b"x" * 101, "limit_exceeded"),
        ):
            with self.subTest(status=status):
                if status == "limit_exceeded":
                    self.fixture.invoke(
                        "configure", "--max-chat-bytes", "100", *self.fixture.session
                    )
                args.acquired_sha256 = digest(raw)

                def acquire(_source: SourceMetadata, _limit: int, *, saved: bytes = raw) -> bytes:
                    return saved

                with self.assertRaises(SourceArchiveError) as caught:
                    tedd_sources.execute(
                        args,
                        host=SourceHost(
                            acquire_google=acquire,
                            provider_check=self.permitted,
                            collecting=self.collecting,
                        ),
                    )
                self.assertEqual(caught.exception.code, status)
        self.assertEqual(
            Archive.open(self.fixture.archive_root, scope=metadata.scope)._history(
                metadata.logical_key
            ),
            (),
        )

    def test_owner_approval_revoked_during_read_prevents_display(self) -> None:
        archive, ref = self._saved_medical_source()
        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "owner-view",
                "--source-uri",
                ref.as_uri(),
                *fixture.session,
            ]
        )
        read = Archive.read

        def revoked(selected: Archive, reference: SourceRef, **kwargs: object) -> SourceExcerpt:
            # The real read and independent config writer run before dispatch.
            result = read(selected, reference, operation="owner_view")
            fixture.invoke("configure-owner-view", "--deny-owner-view", *fixture.session)
            return result

        display = mock.Mock(side_effect=AssertionError("Revoked evidence escaped"))
        with (
            mock.patch.object(
                tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", {tedd_sources.sys.platform}
            ),
            mock.patch.object(Archive, "read", revoked),
        ):
            with self.assertRaises(SourceArchiveError) as caught:
                tedd_sources.execute(args, host=SourceHost(display_owner=display))
        self.assertEqual(caught.exception.code, "scope_denied")
        display.assert_not_called()

    def test_discovery_permission_loss_does_not_return_precheck_metadata(self) -> None:
        args, metadata, payload = self._google_capture()
        host = SourceHost(
            acquire_google=lambda _source, _limit: payload,
            provider_check=self.permitted,
            collecting=self.collecting,
        )
        tedd_sources.execute(args, host=host)
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.fixture.config),
                "lookup",
                "--source-id",
                metadata.source_id,
                *self.fixture.session,
            ]
        )
        calls = 0

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            nonlocal calls
            calls += 1
            return AccessDecision(
                access_binding(source, fingerprint),
                operation,
                audience,
                "allowed" if calls == 1 else "denied",
            )

        result = tedd_sources.execute(args, host=replace(host, provider_check=provider))
        self.assertEqual(result["sources"], ())
        self.assertTrue(result["access_incomplete"])

    def test_pause_during_google_transfer_prevents_archive_publication(self) -> None:
        args, metadata, payload = self._google_capture()
        active = True

        @contextmanager
        def collecting(_session: GraphReadSession, _scope: SourceScope) -> Iterator[None]:
            if not active:
                raise SourceArchiveError("scope_denied")
            yield

        def acquire(_source: SourceMetadata, _maximum: int) -> bytes:
            nonlocal active
            active = False
            return payload

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            return AccessDecision(
                access_binding(source, fingerprint), operation, audience, "allowed"
            )

        host = SourceHost(provider_check=provider, acquire_google=acquire, collecting=collecting)
        with self.assertRaises(SourceArchiveError) as caught:
            tedd_sources.execute(args, host=host)
        self.assertEqual(caught.exception.code, "scope_denied")
        archive = Archive.open(self.fixture.archive_root, scope=metadata.scope)
        self.assertEqual(archive._history(metadata.logical_key), ())

    def test_cloud_read_requires_current_provider_checks_at_public_release(self) -> None:
        args, metadata, payload = self._google_capture()
        archive = Archive.open(self.fixture.archive_root, scope=metadata.scope, readonly=False)
        from tedd_knowledge_graph.source_documents import read_document

        reading = read_document(payload, metadata.kind)
        ref = archive.publish(payload=payload, metadata=metadata, units=reading.units).ref
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.fixture.config),
                "inspect",
                "--source-uri",
                ref.as_uri(),
                *self.fixture.session,
            ]
        )
        calls: list[str] = []

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            calls.append(operation)
            return AccessDecision(
                access_binding(source, fingerprint), operation, audience, "allowed"
            )

        result = tedd_sources.execute(args, host=SourceHost(provider_check=provider))
        self.assertIn(b"Not approved.", encode(result))
        self.assertEqual(calls, ["inspect", "inspect"])
        with self.assertRaises(SourceArchiveError) as caught:
            tedd_sources.execute(args)
        self.assertEqual(caught.exception.code, "capability_unavailable")

    def test_sensitive_page_handoff_never_returns_image_bytes_or_ordinary_preview(self) -> None:
        archive, prior = self._saved_medical_source()
        metadata = replace(
            archive._manifest(prior.manifest_sha256).metadata,
            kind="pdf",
            source_id="medical-page",
            capture_method="local_file",
        )
        ref = archive.publish(
            payload=b"%PDF-1.7 neutral",
            metadata=metadata,
            units=(SourceUnit("u1", "Neutral page", 1, "page"),),
        ).ref
        fixture = self.fixture
        fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "medical",
            *fixture.session,
        )
        args = tedd_sources.parser().parse_args(
            [
                "--config",
                str(fixture.config),
                "owner-view",
                "--source-uri",
                ref.as_uri(),
                "--page",
                "1",
                "--reader-python",
                tedd_sources.sys.executable,
                "--renderer",
                tedd_sources.sys.executable,
                *fixture.session,
            ]
        )
        png = b"\x89PNG\r\n\x1a\nneutral test page"
        displayed: list[tuple[SourceRef, bytes]] = []

        def display(selected: SourceRef, image: bytes) -> None:
            displayed.append((selected, image))

        before = {
            p.relative_to(archive.root): p.read_bytes()
            for p in archive.root.rglob("*")
            if p.is_file()
        }
        with (
            mock.patch.object(
                tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", {tedd_sources.sys.platform}
            ),
            mock.patch.object(tedd_sources, "_render_pdf_png", autospec=True, return_value=png),
        ):
            result = tedd_sources.execute(args, host=SourceHost(display_owner_image=display))
        self.assertEqual(result, {"status": "owner_view_dispatched", "human_only": True})
        self.assertEqual(displayed, [(ref, png)])
        self.assertEqual(
            before,
            {
                p.relative_to(archive.root): p.read_bytes()
                for p in archive.root.rglob("*")
                if p.is_file()
            },
        )

    @staticmethod
    def _memory_packet(ref: SourceRef) -> dict[str, object]:
        return {
            "source": {
                "kind": "document",
                "role": "external",
                "title": "Neutral decision",
                "uri": ref.as_uri(),
                "observed_at": "2026-09-05T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "claims": [
                {
                    "surface_id": "approval",
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

    def test_google_memory_binding_and_dispute_use_provider_checks_outside_graph_transactions(
        self,
    ) -> None:
        args, metadata, payload = self._google_capture()

        @contextmanager
        def collecting(_session: GraphReadSession, _scope: SourceScope) -> Iterator[None]:
            yield

        checks: list[str] = []
        allowed = True

        def provider(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            checks.append(operation)
            # A separate writer can reserve the graph while permission is checked.
            # This fails if a network callback accidentally runs in graph preflight.
            with tedd_sources.publication_reservation(self.fixture.store):
                pass
            return AccessDecision(
                access_binding(source, fingerprint),
                operation,
                audience,
                "allowed" if allowed else "denied",
            )

        host = SourceHost(
            acquire_google=lambda _source, _limit: payload,
            provider_check=provider,
            collecting=collecting,
        )
        captured = tedd_sources.execute(args, host=host)
        reference = captured["ref"]
        assert isinstance(reference, dict)
        ref = SourceRef(**reference)
        packet = self._memory_packet(ref)
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
            source_config_path=self.fixture.config,
            source_subscription_id="subscription",
            source_subject_ids=metadata.scope.subject_ids,
        )
        with v4.open_store(self.fixture.store) as store:
            plan = capture_adapter.execute_capture(store, request, source_host=host)
            validate = tedd_sources.validate_capture_binding
            checked_commit: list[bool] = []

            def validate_at_commit(
                selected_store: object,
                packet: Mapping[str, object],
                selected_request: CaptureRequest,
                *,
                changes: Sequence[Mapping[str, object]] = (),
                provider_check: ProviderCheck | None = None,
                expected_config: bytes | None = None,
                local_only: bool = False,
                check_retention: Callable[[SourceMetadata], None] | None = None,
            ) -> bytes:
                snapshot = validate(
                    selected_store,
                    packet,
                    selected_request,
                    changes=changes,
                    provider_check=provider_check,
                    expected_config=expected_config,
                    local_only=local_only,
                    check_retention=check_retention,
                )
                if local_only:
                    with (
                        self.assertRaises(TimeoutError),
                        tedd_sources.graph.store_lock(self.fixture.config, 0),
                    ):
                        pass
                    checked_commit.append(True)
                return snapshot

            with mock.patch.object(tedd_sources, "validate_capture_binding", validate_at_commit):
                result = capture_adapter.execute_capture(
                    store,
                    replace(
                        request,
                        dry_run=False,
                        expected_fingerprint=str(plan["fingerprint"]),
                        plan_token=str(plan["plan_token"]),
                    ),
                    source_host=host,
                )
            self.assertEqual(checked_commit, [True])
            self.assertEqual(result["status"], "captured")
            changed = plan["changed_ids"]
            assert isinstance(changed, list)
            records = store.get_records(str(item) for item in changed)
            claim = next(record for record in records if record["collection"] == "claims")
        before = self.fixture.store.read_bytes()
        evidence_args = argparse.Namespace(
            config=str(self.fixture.config),
            max_chars=12_000,
            max_versions=5,
            producer_agent_id="agent",
            task_id="task",
            producer_run_id="run",
            consumer_id="consumer",
            subscription_id="subscription",
            subject_id=["entity_task"],
            id=claim["record_id"],
            revision=None,
        )
        with v4.open_store(self.fixture.store, readonly=True) as store:
            result = tedd_sources.evidence_command(
                evidence_args, store, self.fixture.store, host=host
            )
        self.assertIn(b"Not approved.", encode(result))
        self.assertIn("evidence", checks)
        self.assertEqual(self.fixture.store.read_bytes(), before)
        with v4.open_store(self.fixture.store, readonly=True) as store:
            retrieval_before = encode(
                store.context_pack("subscription", max_items=10, max_chars=12000)
            )
        # The same explicit host route preserves a later observation, not a rewrite.
        original = payload
        payload = payload.replace(b"Not approved.", b"Approval withdrawn.")
        later = replace(metadata, source_version="v2")
        (self.fixture.root / "document-metadata.json").write_bytes(encode(asdict(later)))
        args.acquired_sha256 = digest(payload)
        self.assertEqual(tedd_sources.execute(args, host=host)["status"], "preserved")
        with v4.open_store(self.fixture.store, readonly=True) as store:
            result = tedd_sources.evidence_command(
                evidence_args, store, self.fixture.store, host=host
            )
        self.assertIn(b"Approval withdrawn.", encode(result))
        self.assertIn(b"Not approved.", encode(result))
        archive = tedd_sources.open_archive(
            self.fixture.archive_root,
            metadata.scope,
            json.loads(self.fixture.config.read_bytes()),
            provider_check=provider,
        )
        self.assertEqual(archive.original(ref), original)
        related_checks = 0

        def deny_metadata_only(
            source: SourceMetadata, fingerprint: str, operation: str, audience: str
        ) -> AccessDecision:
            nonlocal related_checks
            if source.source_version == "v2":
                related_checks += 1
            return AccessDecision(
                access_binding(source, fingerprint),
                operation,
                audience,
                "denied" if source.source_version == "v2" and related_checks > 1 else "allowed",
            )

        evidence_args.max_versions = 1
        with (
            v4.open_store(self.fixture.store, readonly=True) as store,
            self.assertRaises(SourceArchiveError) as related_denied,
        ):
            tedd_sources.evidence_command(
                evidence_args,
                store,
                self.fixture.store,
                host=replace(host, provider_check=deny_metadata_only),
            )
        self.assertEqual(str(related_denied.exception), "scope_denied")
        self.assertEqual(related_checks, 2)
        evidence_args.max_versions = 5
        allowed = False
        with (
            v4.open_store(self.fixture.store, readonly=True) as store,
            self.assertRaises(SourceArchiveError) as denied,
        ):
            tedd_sources.evidence_command(evidence_args, store, self.fixture.store, host=host)
        self.assertEqual(str(denied.exception), "scope_denied")
        self.fixture.invoke(
            "configure-owner-view",
            "--allow-owner-view",
            "--sensitivity",
            "ordinary",
            *self.fixture.session,
        )
        viewing = tedd_sources.parser().parse_args(
            [
                "--config",
                str(self.fixture.config),
                "owner-view",
                "--source-uri",
                ref.as_uri(),
                *self.fixture.session,
            ]
        )
        displayed: list[SourceExcerpt] = []
        with mock.patch.object(
            tedd_sources, "QUALIFIED_PREVIEW_PLATFORMS", {tedd_sources.sys.platform}
        ):
            display_host = replace(host, display_owner=displayed.append)
            with self.assertRaises(SourceArchiveError):
                tedd_sources.execute(viewing, host=display_host)
            self.assertEqual(displayed, [])
            allowed = True
            self.assertEqual(
                tedd_sources.execute(viewing, host=display_host),
                {"status": "owner_view_dispatched", "human_only": True},
            )
        self.assertIn("Not approved.", displayed[0].units[0].text)
        self.assertEqual(self.fixture.store.read_bytes(), before)
        with v4.open_store(self.fixture.store, readonly=True) as store:
            self.assertEqual(
                retrieval_before,
                encode(store.context_pack("subscription", max_items=10, max_chars=12000)),
            )
        root = self.fixture.root
        self.fixture.doCleanups()
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
