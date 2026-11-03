"""PKCE, exact account/scope binding and protected reuse; no live Google requests."""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from tedd_knowledge_graph import source_google_oauth as oauth
from tedd_knowledge_graph.source_contracts import SourceArchiveError, SourceScope
from tedd_knowledge_graph.source_google import DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE
from tedd_knowledge_graph.source_google_oauth import (
    DesktopClient,
    authorization_url,
    callback_result,
    decode_client,
    verified_identity,
)


class GoogleOAuthTests(unittest.TestCase):
    def _credential_store(self, payload: bytes | None = None) -> mock.Mock:
        from benchmark_support import private_temporary_parent
        from platform_support import set_private_path_mode
        from tedd_knowledge_graph.source_credentials import CredentialStore

        temporary = tempfile.TemporaryDirectory(dir=private_temporary_parent())
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        set_private_path_mode(root, 0o700)
        actual = CredentialStore(root)
        store = mock.Mock(spec=CredentialStore, wraps=actual)
        store.root = root

        def put(_key: str, value: bytes) -> None:
            nonlocal payload
            payload = value

        def delete(_key: str) -> None:
            nonlocal payload
            payload = None

        store.get.side_effect = lambda _key: payload
        store.put.side_effect = put
        store.delete.side_effect = delete
        return store

    @staticmethod
    def _client_read(client: object) -> Callable[[Path, int], bytes]:
        read = oauth.source_files.read_bytes
        return lambda path, maximum: (
            json.dumps(client).encode()
            if path.name == "neutral-client.json"
            else read(path, maximum)
        )

    def test_refresh_rejects_changed_authority_cancellation_and_expired_grants(self) -> None:
        from tedd_knowledge_graph.source_contracts import encode

        saved = {
            "schema": "tedd-google-credential-v1",
            "client_id": "neutral.apps.googleusercontent.com",
            "account": "owner@example.com",
            "principal": "google:123",
            "scope": DRIVE_READONLY_SCOPE,
            "refresh_token": "neutral",
        }
        client = {
            "installed": {
                "client_id": saved["client_id"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        scope = SourceScope("store", "generation", ("subject",), "personal_task")
        for case in (
            "wrong_account",
            "wrong_scope",
            "wrong_principal",
            "cancelled",
            "expired",
            "unavailable",
        ):
            with self.subTest(case=case):
                stop = threading.Event()
                store = self._credential_store(encode(saved))
                calls = 0

                def request(
                    url: str, *, case: str = case, stop: threading.Event = stop, **_kwargs: object
                ) -> bytes:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if case in {"expired", "unavailable"}:
                            raise SourceArchiveError(
                                "reauthentication_required"
                                if case == "expired"
                                else "source_unavailable"
                            )
                        if case == "cancelled":
                            stop.set()
                        return encode(
                            {
                                "access_token": "neutral-access",
                                "token_type": "Bearer",
                                "scope": DRIVE_FILE_SCOPE
                                if case == "wrong_scope"
                                else DRIVE_READONLY_SCOPE,
                                "expires_in": 3600,
                                "refresh_token": "neutral-rotated",
                            }
                        )
                    return encode(
                        {
                            "user": {
                                "me": True,
                                "emailAddress": "other@example.com"
                                if case == "wrong_account"
                                else "owner@example.com",
                                "permissionId": "456" if case == "wrong_principal" else "123",
                            }
                        }
                    )

                with (
                    mock.patch.object(
                        oauth.source_files, "read_bytes", side_effect=self._client_read(client)
                    ),
                    mock.patch.object(oauth, "google_request", side_effect=request),
                    mock.patch.object(
                        oauth,
                        "_authorization_form",
                        side_effect=AssertionError("unexpected consent"),
                    ),
                    self.assertRaises(SourceArchiveError),
                ):
                    oauth.sign_in(
                        Path("neutral-client.json"),
                        account="owner@example.com",
                        scope=scope,
                        cancelled=stop,
                        credentials=store,
                        folder_ids=frozenset({"folder"}),
                    )
                store.put.assert_not_called()
                self.assertEqual(store.delete.call_count, 1 if case == "expired" else 0)
                self.assertEqual(calls, 2 if case in {"wrong_account", "wrong_principal"} else 1)

    def test_linux_keyring_uses_stdin_and_opaque_attributes_without_plaintext_files(self) -> None:
        from benchmark_support import private_temporary_parent
        from platform_support import set_private_path_mode
        from tedd_knowledge_graph import source_credentials as credentials

        with tempfile.TemporaryDirectory(dir=private_temporary_parent()) as folder:
            root = Path(folder)
            set_private_path_mode(root, 0o700)
            store = credentials.CredentialStore(root)
            with mock.patch.object(credentials, "_secret_tool", return_value=None) as secret:
                with mock.patch.object(credentials.sys, "platform", "linux"):
                    store.put("a" * 64, b"neutral-refresh")
                    self.assertIsNone(store.get("a" * 64))
                    store.delete("a" * 64)
            self.assertEqual(
                [call.args[0] for call in secret.call_args_list], ["store", "lookup", "clear"]
            )
            self.assertEqual(secret.call_args_list[0].kwargs["payload"], b"neutral-refresh")
            self.assertTrue(all(len(call.args[1]) == 64 for call in secret.call_args_list))
            self.assertEqual(list(root.iterdir()), [])

    def test_reusable_authorization_is_read_only_offline_without_file_picker(self) -> None:
        query = parse_qs(
            urlsplit(
                authorization_url(
                    DesktopClient("neutral.apps.googleusercontent.com", ""),
                    49152,
                    "state",
                    "challenge",
                    "owner@example.com",
                    persistent=True,
                )
            ).query
        )
        self.assertEqual(query["scope"], [DRIVE_READONLY_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertNotIn("trigger_onepick", query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(
            callback_result(
                "/oauth/callback?"
                + urlencode(
                    {
                        "state": "state",
                        "scope": DRIVE_READONLY_SCOPE,
                        "code": "neutral",
                    }
                ),
                "state",
                persistent=True,
            ),
            ("neutral", ""),
        )

    def test_saved_refresh_token_reuses_sign_in_but_not_task_authority(self) -> None:
        store = self._credential_store(
            json.dumps(
                {
                    "schema": "tedd-google-credential-v1",
                    "client_id": "neutral.apps.googleusercontent.com",
                    "account": "owner@example.com",
                    "principal": "google:123",
                    "scope": DRIVE_READONLY_SCOPE,
                    "refresh_token": "neutral-refresh",
                }
            ).encode()
        )
        client = {
            "installed": {
                "client_id": "neutral.apps.googleusercontent.com",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        scope = SourceScope("store", "generation", ("subject",), "personal_task")
        with (
            mock.patch.object(
                oauth.source_files, "read_bytes", side_effect=self._client_read(client)
            ),
            mock.patch.object(
                oauth, "_authorization_form", side_effect=AssertionError("repeated consent")
            ),
            mock.patch.object(
                oauth.webbrowser, "open", side_effect=AssertionError("repeated browser")
            ),
            mock.patch.object(
                oauth,
                "google_request",
                side_effect=[
                    json.dumps(
                        {
                            "access_token": "neutral-new",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                            "scope": DRIVE_READONLY_SCOPE,
                        }
                    ).encode(),
                    json.dumps(
                        {
                            "user": {
                                "me": True,
                                "emailAddress": "owner@example.com",
                                "permissionId": "123",
                            }
                        }
                    ).encode(),
                ],
            ) as request,
        ):
            session = oauth.sign_in(
                Path("neutral-client.json"),
                account="owner@example.com",
                scope=scope,
                cancelled=threading.Event(),
                credentials=store,
                selected_ids=frozenset({"new-file"}),
                folder_ids=frozenset({"folder"}),
            )
        self.assertEqual(session.selected_ids, frozenset({"new-file"}))
        self.assertEqual(session.folder_ids, frozenset({"folder"}))
        self.assertEqual(session.principal, "google:123")
        self.assertEqual(request.call_args_list[0].kwargs["form"]["grant_type"], "refresh_token")
        self.assertNotIn("neutral-new", repr(session))

    @unittest.skipUnless(sys.platform == "win32", "Windows native credential protection")
    def test_protected_credential_survives_reopen_without_plaintext_file(self) -> None:
        from benchmark_support import private_temporary_parent
        from platform_support import set_private_path_mode
        from tedd_knowledge_graph.source_credentials import CredentialStore

        with tempfile.TemporaryDirectory(dir=private_temporary_parent()) as folder:
            root = Path(folder)
            set_private_path_mode(root, 0o700)
            store = CredentialStore(root)
            key = "a" * 64
            self.assertIsNone(store.get(key))
            secret = b"neutral-refresh-token"
            store.put(key, secret)
            self.assertEqual(CredentialStore(root).get(key), secret)
            stored = list(root.iterdir())
            self.assertTrue(stored)
            self.assertTrue(all(secret not in path.read_bytes() for path in stored))
            store.delete(key)
            self.assertIsNone(CredentialStore(root).get(key))

    @unittest.skipUnless(sys.platform == "win32", "Windows native credential protection")
    def test_corrupt_or_rebound_credential_fails_closed(self) -> None:
        from benchmark_support import private_temporary_parent
        from platform_support import set_private_path_mode
        from tedd_knowledge_graph.source_credentials import CredentialStore

        with tempfile.TemporaryDirectory(dir=private_temporary_parent()) as folder:
            root = Path(folder)
            set_private_path_mode(root, 0o700)
            store = CredentialStore(root)
            key, other = "a" * 64, "b" * 64
            store.put(key, b"neutral-refresh-token")
            os.rename(root / (key + ".credential"), root / (other + ".credential"))
            with self.assertRaises(SourceArchiveError):
                store.get(other)
            path = root / (other + ".credential")
            data = path.read_bytes()
            path.write_bytes(data[:-1])
            with self.assertRaises(SourceArchiveError):
                store.get(other)

    def test_real_loopback_handoff_verifies_account_and_keeps_tokens_in_memory(self) -> None:
        self._loopback_sign_in(persistent=False)

    def test_real_loopback_readonly_handoff_saves_only_verified_refresh_credential(self) -> None:
        self._loopback_sign_in(persistent=True)

    def _loopback_sign_in(self, *, persistent: bool) -> None:
        credentials = self._credential_store()
        requested = DRIVE_READONLY_SCOPE if persistent else DRIVE_FILE_SCOPE
        client = {
            "installed": {
                "client_id": "neutral.apps.googleusercontent.com",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        stop = threading.Event()
        scope = SourceScope("store", "generation", ("subject",), "personal_task")
        responses = []
        callback_threads = []
        failures: list[BaseException] = []

        def browser(url: str) -> bool:
            redirect = urlsplit(url)
            callback_host = redirect.hostname
            self.assertEqual(callback_host, "127.0.0.1")
            self.assertEqual(redirect.path, "/oauth/review")

            def callback() -> None:
                assert callback_host is not None
                connection = http.client.HTTPConnection(callback_host, redirect.port, timeout=3)
                try:
                    nonce = self._read_browser_disclosure(connection, redirect.path, persistent)
                    connection.request(
                        "POST",
                        "/oauth/start",
                        urlencode({"nonce": nonce, "decision": "continue"}),
                        {
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": f"http://127.0.0.1:{redirect.port}",
                        },
                    )
                    started = connection.getresponse()
                    self.assertEqual(started.status, 303)
                    location = started.getheader("Location")
                    assert location is not None
                    self.assertEqual(urlsplit(location).hostname, "accounts.google.com")
                    query = parse_qs(urlsplit(location).query)
                    self.assertEqual(query["login_hint"], ["owner@example.com"])
                    self.assertEqual(query["scope"], [requested])
                    started.read()
                    connection.request(
                        "GET",
                        "/oauth/callback"
                        + "?"
                        + urlencode(
                            {
                                "state": query["state"][0],
                                "scope": requested,
                                "code": "neutral-code",
                                **({} if persistent else {"picked_file_ids": "selected"}),
                            }
                        ),
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
                    responses.append(
                        (response.status, response.getheader("Cache-Control"), response.read())
                    )
                except BaseException as error:
                    failures.append(error)
                    stop.set()
                finally:
                    connection.close()

            thread = threading.Thread(target=callback)
            callback_threads.append(thread)
            thread.start()
            return True

        with (
            mock.patch(
                "tedd_knowledge_graph.source_viewer.PrivateViewer",
                side_effect=AssertionError("unexpected native consent"),
            ),
            mock.patch.object(
                oauth.source_files, "read_bytes", side_effect=self._client_read(client)
            ),
            mock.patch.object(oauth.webbrowser, "open", side_effect=browser),
            mock.patch.object(
                oauth,
                "google_request",
                side_effect=[
                    json.dumps(
                        {
                            "access_token": "neutral-token",
                            "expires_in": 60,
                            "token_type": "Bearer",
                            "scope": requested,
                            **({"refresh_token": "neutral-refresh"} if persistent else {}),
                        }
                    ).encode(),
                    json.dumps(
                        {
                            "user": {
                                "me": True,
                                "emailAddress": "owner@example.com",
                                "permissionId": "123",
                            }
                        }
                    ).encode(),
                ],
            ) as request,
        ):
            session = oauth.sign_in(
                Path("neutral-client.json"),
                account="owner@example.com",
                scope=scope,
                cancelled=stop,
                credentials=credentials if persistent else None,
                selected_ids=frozenset({"selected"}) if persistent else frozenset(),
            )
        for thread in callback_threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(responses[0][:2], (200, "no-store"))
        self.assertEqual(session.selected_ids, frozenset({"selected"}))
        self.assertEqual(session.principal, "google:123")
        self.assertNotIn("neutral-token", repr(session))
        self.assertEqual(len(request.call_args_list), 2)
        self.assertIn("code_verifier", request.call_args_list[0].kwargs["form"])
        if persistent:
            credentials.put.assert_called_once()
            saved = json.loads(credentials.put.call_args.args[1])
            self.assertEqual(saved["scope"], DRIVE_READONLY_SCOPE)
            self.assertEqual(saved["principal"], "google:123")
            self.assertNotIn("access_token", saved)
        else:
            credentials.put.assert_not_called()
        session.disconnect()

    def _read_browser_disclosure(
        self, connection: http.client.HTTPConnection, path: str, persistent: bool
    ) -> str:
        connection.request("GET", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy") or "")
        page = response.read().decode("utf-8")
        self.assertIn("owner@example.com", page)
        self.assertIn("drive.readonly" if persistent else "drive.file", page)
        self.assertIn("Continue to Google", page)
        self.assertNotIn("<script", page)
        match = re.search(r'name="nonce" value="([A-Za-z0-9_-]+)"', page)
        assert match is not None
        return match[1]

    def test_cancelled_consent_never_exchanges_a_code(self) -> None:
        stop = threading.Event()
        stop.set()
        with (
            mock.patch.object(oauth.webbrowser, "open") as browser,
            mock.patch.object(oauth, "google_request") as request,
            self.assertRaises(SourceArchiveError),
        ):
            oauth.sign_in(
                Path("not-read.json"),
                account="owner@example.com",
                scope=SourceScope("store", "generation", ("subject",), "personal_task"),
                cancelled=stop,
            )
        browser.assert_not_called()
        request.assert_not_called()

    def test_browser_review_refuses_cancellation_and_unapproved_requests(self) -> None:
        for case, expected_status in (
            ("cancel", 200),
            ("wrong_nonce", 403),
            ("foreign_origin", 403),
            ("null_origin", 403),
            ("wrong_host", 400),
            ("oversized", 400),
            ("callback_first", 403),
            ("get_start", 404),
            ("replay", 403),
        ):
            with self.subTest(case=case):
                stop = threading.Event()
                failures: list[BaseException] = []
                workers: list[threading.Thread] = []

                def browser(
                    url: str,
                    *,
                    case: str = case,
                    expected_status: int = expected_status,
                    stop: threading.Event = stop,
                    failures: list[BaseException] = failures,
                    workers: list[threading.Thread] = workers,
                ) -> bool:
                    target = urlsplit(url)
                    self.assertEqual(target.hostname, "127.0.0.1")

                    def interact() -> None:
                        connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=3)
                        try:
                            nonce = self._read_browser_disclosure(connection, target.path, True)
                            fields = {
                                "nonce": "wrong" if case == "wrong_nonce" else nonce,
                                "decision": "cancel" if case == "cancel" else "continue",
                            }
                            headers = {"Content-Type": "application/x-www-form-urlencoded"}
                            if case == "foreign_origin":
                                headers["Origin"] = "https://example.com"
                            if case == "null_origin":
                                headers["Origin"] = "null"
                            if case == "wrong_host":
                                headers["Host"] = "example.com"
                            body = urlencode(fields)
                            if case == "replay":
                                connection.request("POST", "/oauth/start", body, headers)
                                accepted = connection.getresponse()
                                self.assertEqual(accepted.status, 303)
                                accepted.read()
                            if case in {
                                "oversized",
                                "wrong_host",
                                "foreign_origin",
                                "null_origin",
                                "replay",
                            }:
                                # The server must reject these headers before reading a body.
                                headers["Content-Length"] = (
                                    "1025" if case == "oversized" else str(len(body))
                                )
                                body = ""
                            if case in {"callback_first", "get_start"}:
                                connection.request(
                                    "GET",
                                    "/oauth/callback"
                                    if case == "callback_first"
                                    else "/oauth/start",
                                )
                            else:
                                connection.request("POST", "/oauth/start", body, headers)
                            response = connection.getresponse()
                            self.assertEqual(response.status, expected_status)
                            self.assertIsNone(response.getheader("Location"))
                            response.read()
                        except BaseException as error:
                            failures.append(error)
                        finally:
                            connection.close()
                            stop.set()

                    worker = threading.Thread(target=interact)
                    workers.append(worker)
                    worker.start()
                    return True

                with (
                    mock.patch.object(oauth.webbrowser, "open", side_effect=browser),
                    self.assertRaises(SourceArchiveError),
                ):
                    oauth._authorization_form(
                        DesktopClient("neutral.apps.googleusercontent.com", ""),
                        "owner@example.com",
                        stop,
                        persistent=True,
                    )
                for worker in workers:
                    worker.join(4)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])

    def test_disclosure_includes_credential_deletion_without_claiming_adapter_writes(self) -> None:
        self.assertIn("delete", oauth.CONSENT_DESCRIPTION)
        self.assertIn("only reads", oauth.CONSENT_DESCRIPTION)

    def test_authorization_requests_only_selected_file_scope_and_pkce(self) -> None:
        url = authorization_url(
            DesktopClient("neutral.apps.googleusercontent.com", ""),
            49152,
            "state",
            "challenge",
            "owner@example.com",
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["scope"], [DRIVE_FILE_SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["trigger_onepick"], ["true"])
        self.assertEqual(query["include_granted_scopes"], ["false"])
        self.assertEqual(query["access_type"], ["online"])
        self.assertNotIn("client_secret", query)
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:49152/oauth/callback"])

    def test_callback_requires_exact_state_scope_and_one_selected_file(self) -> None:
        fields = {
            "state": "state",
            "scope": DRIVE_FILE_SCOPE,
            "code": "neutral-code",
            "picked_file_ids": "selected",
        }
        self.assertEqual(
            callback_result("/oauth/callback?" + urlencode(fields), "state"),
            ("neutral-code", "selected"),
        )
        for changed in (
            {**fields, "state": "other"},
            {**fields, "scope": DRIVE_FILE_SCOPE + " https://www.googleapis.com/auth/drive"},
            {**fields, "picked_file_ids": ""},
            {**fields, "picked_file_ids": "one,two"},
            {**fields, "error": "access_denied"},
        ):
            with self.subTest(fields=changed), self.assertRaises(SourceArchiveError):
                callback_result("/oauth/callback?" + urlencode(changed), "state")
        with self.assertRaises(SourceArchiveError):
            callback_result("/oauth/callback?" + urlencode(fields) + "&state=state", "state")

    def test_app_configuration_cannot_substitute_endpoints_or_web_client(self) -> None:
        installed = {
            "client_id": "neutral.apps.googleusercontent.com",
            "client_secret": "neutral-client",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        client = decode_client({"installed": installed})
        self.assertNotIn("neutral-client", repr(client))
        for value in (
            {"web": installed},
            {"installed": {**installed, "token_uri": "https://example.com/token"}},
        ):
            with self.assertRaises(SourceArchiveError):
                decode_client(value)

    def test_provider_identity_is_not_taken_from_login_hint(self) -> None:
        value = {
            "user": {
                "emailAddress": "owner@example.com",
                "permissionId": "1234",
                "me": True,
            }
        }
        self.assertEqual(verified_identity(value, "owner@example.com"), "google:1234")
        for user in (
            {**value["user"], "emailAddress": "different@example.com"},
            {**value["user"], "me": False},
            {},
        ):
            with self.assertRaises(SourceArchiveError):
                verified_identity({"user": user}, "owner@example.com")


if __name__ == "__main__":
    unittest.main()
