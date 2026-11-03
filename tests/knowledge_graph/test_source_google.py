"""Google reads use exact provider bytes and effective, current capabilities."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceDetails,
    SourceMetadata,
    SourceScope,
    digest,
    encode,
)
from tedd_knowledge_graph.source_google import GoogleSourceSession


class GoogleSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = SourceScope("store", "generation", ("subject",), "personal_task")
        self.metadata = SourceMetadata(
            self.scope,
            "selected",
            "observation",
            "google_doc",
            "structured_observation",
            "google-rest:1",
            "task",
            "run",
            "encounter",
            "2026-09-08T12:00:00Z",
            details=SourceDetails(
                "google",
                "principal",
                "response",
                "structured_observation",
                "pending",
                provider_revision="7",
                retention="access_dependent",
                sensitivity="ordinary",
            ),
        )
        self.session = GoogleSourceSession(
            "neutral-bearer",
            "principal",
            frozenset({"selected"}),
            self.scope,
            expires_at=10**12,
        )
        self.file = {
            "id": "selected",
            "version": "7",
            "trashed": False,
            "mimeType": "application/vnd.google-apps.document",
            "capabilities": {"canCopy": True, "canDownload": True, "canAccessViaGenAi": True},
        }

    def test_raw_response_is_preserved_between_matching_version_checks(self) -> None:
        payload = b'{"documentId":"selected", "body":{"content":[]}}\n'
        with mock.patch.object(
            self.session, "_get", side_effect=[encode(self.file), payload, encode(self.file)]
        ) as fetch:
            self.assertEqual(self.session.acquire(self.metadata, 1000), payload)
        self.assertIn("includeTabsContent=true", fetch.call_args_list[1].args[0])

    def test_linked_folder_allows_nested_file_without_individual_selection(self) -> None:
        session = GoogleSourceSession(
            "neutral",
            "principal",
            frozenset(),
            self.scope,
            10**12,
            folder_ids=frozenset({"root"}),
        )
        child = {**self.file, "parents": ["nested"]}
        nested = {
            "id": "nested",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "parents": ["root"],
        }
        root = {
            "id": "root",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "capabilities": {"canListChildren": True},
        }
        payload = b'{"documentId":"selected"}'
        with mock.patch.object(
            session,
            "_get",
            side_effect=[
                encode(child),
                encode(nested),
                encode(root),
                payload,
                encode(child),
                encode(nested),
                encode(root),
            ],
        ) as fetch:
            self.assertEqual(session.acquire(self.metadata, 1024), payload)
        self.assertEqual(
            sum("docs.googleapis.com" in call.args[0] for call in fetch.call_args_list), 1
        )

    def test_folder_escape_cycle_and_shortcut_never_read_document_content(self) -> None:
        for parent in (
            {
                "id": "outside",
                "mimeType": "application/vnd.google-apps.folder",
                "trashed": False,
                "parents": [],
            },
            {
                "id": "outside",
                "mimeType": "application/vnd.google-apps.folder",
                "trashed": False,
                "parents": ["outside"],
            },
            {
                "id": "outside",
                "mimeType": "application/vnd.google-apps.shortcut",
                "trashed": False,
                "parents": ["root"],
            },
        ):
            session = GoogleSourceSession(
                "neutral",
                "principal",
                frozenset(),
                self.scope,
                10**12,
                folder_ids=frozenset({"root"}),
            )
            with (
                self.subTest(parent=parent),
                mock.patch.object(
                    session,
                    "_get",
                    side_effect=[
                        encode({**self.file, "parents": ["outside"]}),
                        encode(parent),
                    ],
                ) as fetch,
            ):
                with self.assertRaises(SourceArchiveError):
                    session.acquire(self.metadata, 1024)
                self.assertFalse(
                    any("docs.googleapis.com" in call.args[0] for call in fetch.call_args_list)
                )

    def test_folder_move_during_transfer_prevents_returning_acquired_bytes(self) -> None:
        session = GoogleSourceSession(
            "neutral",
            "principal",
            frozenset(),
            self.scope,
            10**12,
            folder_ids=frozenset({"root"}),
        )
        root = {
            "id": "root",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "capabilities": {"canListChildren": True},
        }
        with mock.patch.object(
            session,
            "_get",
            side_effect=[
                encode({**self.file, "parents": ["root"]}),
                encode(root),
                b"not released",
                encode({**self.file, "parents": []}),
            ],
        ):
            with self.assertRaises(SourceArchiveError):
                session.acquire(self.metadata, 1024)

    def test_folder_listing_is_bounded_metadata_not_document_download(self) -> None:
        session = GoogleSourceSession(
            "neutral",
            "principal",
            frozenset(),
            self.scope,
            10**12,
            folder_ids=frozenset({"root"}),
        )
        root = {
            "id": "root",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "capabilities": {"canListChildren": True},
        }
        listing = {
            "files": [{**self.file, "name": "Neutral", "parents": ["root"]}],
            "nextPageToken": "next",
        }
        with mock.patch.object(
            session,
            "_get",
            side_effect=[
                encode(root),
                encode(listing),
                encode(root),
            ],
        ) as fetch:
            result = session.list_folder("root")
        self.assertEqual(
            result,
            {
                "files": [{"id": "selected", "name": "Neutral", "kind": "google_doc"}],
                "next_page": "next",
            },
        )
        self.assertFalse(
            any("docs.googleapis.com" in call.args[0] for call in fetch.call_args_list)
        )

    def test_preparation_uses_verified_identity_and_version_not_model_claims(self) -> None:
        assert self.metadata.details is not None
        proposed = replace(
            self.metadata,
            details=replace(self.metadata.details, principal="claimed", provider_revision=None),
        )
        with mock.patch.object(self.session, "_get", return_value=encode(self.file)):
            prepared = self.session.prepare(proposed)
        assert prepared.details is not None
        self.assertEqual(prepared.details.principal, self.session.principal)
        self.assertEqual(prepared.details.provider_revision, "7")
        self.assertEqual(prepared.source_version, "google-drive-version:7")
        self.assertEqual(prepared.scope, self.scope)

    def test_changed_version_denies_transfer_without_retry(self) -> None:
        with mock.patch.object(
            self.session,
            "_get",
            side_effect=[encode(self.file), b"{}", encode({**self.file, "version": "8"})],
        ) as fetch:
            with self.assertRaises(SourceArchiveError):
                self.session.acquire(self.metadata, 1000)
            self.assertEqual(fetch.call_count, 3)

    def test_current_capability_loss_and_unknown_fields_fail_closed(self) -> None:
        capabilities = self.file["capabilities"]
        assert isinstance(capabilities, dict)
        for name in ("canCopy", "canDownload", "canAccessViaGenAi"):
            for value in (False, None, "true"):
                with self.subTest(name=name, value=value):
                    restricted = {
                        **self.file,
                        "capabilities": {**capabilities, name: value},
                    }
                    with mock.patch.object(self.session, "_get", return_value=encode(restricted)):
                        decision = self.session.check(
                            self.metadata, digest(b"body"), "capture", "model"
                        )
                        self.assertNotEqual(decision.status, "allowed")

    def test_old_source_can_be_read_with_current_permission_but_not_recaptured(self) -> None:
        with mock.patch.object(
            self.session, "_get", return_value=encode({**self.file, "version": "8"})
        ):
            self.assertEqual(
                self.session.check(self.metadata, "a" * 64, "inspect", "model").status, "allowed"
            )
            self.assertEqual(
                self.session.check(self.metadata, "a" * 64, "capture", "model").status, "denied"
            )

    def test_selection_principal_scope_and_kind_cannot_be_substituted(self) -> None:
        assert self.metadata.details is not None
        for metadata in (
            replace(self.metadata, source_id="unselected"),
            replace(self.metadata, scope=replace(self.scope, generation="other")),
            replace(self.metadata, details=replace(self.metadata.details, principal="other")),
            replace(self.metadata, kind="google_sheet"),
        ):
            with (
                self.subTest(metadata=metadata),
                mock.patch.object(self.session, "_get", return_value=encode(self.file)),
            ):
                self.assertNotEqual(
                    self.session.check(metadata, "a" * 64, "inspect", "model").status, "allowed"
                )

    def test_expiry_disconnect_and_remote_error_release_no_content(self) -> None:
        with mock.patch.object(
            self.session, "_get", side_effect=SourceArchiveError("source_unavailable")
        ):
            self.assertEqual(
                self.session.check(self.metadata, "a" * 64, "inspect", "model").status, "unknown"
            )
        self.session.disconnect()
        with self.assertRaises(SourceArchiveError):
            self.session.acquire(self.metadata, 1000)
        self.assertNotIn("neutral-bearer", repr(self.session))

    def test_http_boundary_refuses_redirects_and_non_google_destinations(self) -> None:
        with mock.patch("http.client.HTTPSConnection") as connection:
            with self.assertRaises(SourceArchiveError):
                self.session._get("https://example.com/selected", 1000)
            connection.assert_not_called()
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.getheader.return_value = "application/json"
            with self.assertRaises(SourceArchiveError):
                self.session._get("https://www.googleapis.com/drive/v3/files/selected", 1000)
            response.read.assert_not_called()
            connection.return_value.close.assert_called_once()

    def test_http_denial_remains_distinct_from_provider_unavailability(self) -> None:
        for status, expected in (
            (403, "denied"),
            (404, "denied"),
            (503, "unknown"),
        ):
            with (
                self.subTest(status=status),
                mock.patch("http.client.HTTPSConnection") as connection,
            ):
                response = connection.return_value.getresponse.return_value
                response.status = status
                decision = self.session.check(self.metadata, "a" * 64, "evidence", "model")
                self.assertEqual(decision.status, expected)
                response.read.assert_not_called()
                connection.return_value.close.assert_called_once()

    def test_authentication_failure_is_not_reported_as_resource_permission_denial(self) -> None:
        with mock.patch("http.client.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 401
            with self.assertRaises(SourceArchiveError) as refused:
                self.session.check(self.metadata, "a" * 64, "evidence", "model")
            self.assertEqual(refused.exception.code, "reauthentication_required")
            response.read.assert_not_called()

    def test_http_payload_limit_keeps_its_public_error_code(self) -> None:
        with mock.patch("http.client.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "identity"
            response.read.return_value = b"12345"
            with self.assertRaises(SourceArchiveError) as refused:
                self.session._get("https://www.googleapis.com/drive/v3/files/selected", 4)
            self.assertEqual(refused.exception.code, "limit_exceeded")
            connection.return_value.close.assert_called_once()

    def test_expired_refresh_grant_has_actionable_error_without_provider_content(self) -> None:
        from tedd_knowledge_graph.source_google import google_request

        with mock.patch("http.client.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 400
            response.getheader.return_value = "identity"
            response.read.return_value = (
                b'{"error":"invalid_grant","error_description":"private detail"}'
            )
            with self.assertRaises(SourceArchiveError) as refused:
                google_request(
                    "https://oauth2.googleapis.com/token",
                    maximum=65536,
                    form={"grant_type": "refresh_token", "refresh_token": "neutral"},
                )
            self.assertEqual(str(refused.exception), "reauthentication_required")
            connection.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
