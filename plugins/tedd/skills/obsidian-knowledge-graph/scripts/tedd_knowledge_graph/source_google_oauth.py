"""Desktop PKCE sign-in; optional OS-protected reuse for trusted linked resources."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import source_files
from .source_contracts import SourceArchiveError, SourceScope, digest, encode, parse_json
from .source_credentials import CredentialStore
from .source_google import (
    DRIVE_FILE_SCOPE,
    DRIVE_READONLY_SCOPE,
    GoogleSourceSession,
    google_object,
    google_request,
)
from .source_google_connection import CredentialSnapshot, GoogleConnection

CONSENT_DESCRIPTION = (
    "Tedd requests drive.file: this credential can read, create, edit and permanently "
    "delete files selected for this app, within your Google permissions. "
    "Tedd's source adapter only reads. It does not request "
    "whole-Drive access. Sign-in credentials stay in memory until this session ends."
)
PERSISTENT_CONSENT_DESCRIPTION = (
    "Tedd requests drive.readonly: Google grants view/download access across Drive, "
    "not only one folder. This credential cannot edit or delete files. Tedd limits "
    "its use to files and folders you link for the task, including folder descendants. "
    "Only sources actually used are preserved. Sign-in is retained with OS protection "
    "outside your graph and vault; no background collection is enabled."
)


@dataclass(frozen=True, repr=False)
class DesktopClient:
    client_id: str
    client_secret: str


def decode_client(value: object) -> DesktopClient:
    installed = value.get("installed") if isinstance(value, dict) else None
    if not isinstance(installed, dict):
        raise SourceArchiveError("capability_unavailable")
    client_id, secret = installed.get("client_id"), installed.get("client_secret", "")
    if (
        not isinstance(client_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id)
        or not isinstance(secret, str)
        or len(secret) > 4096
        or installed.get("auth_uri")
        not in (
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        )
        or installed.get("token_uri") != "https://oauth2.googleapis.com/token"
    ):
        raise SourceArchiveError("capability_unavailable")
    return DesktopClient(client_id, secret)


def authorization_url(
    client: DesktopClient,
    port: int,
    state: str,
    challenge: str,
    account: str,
    *,
    persistent: bool = False,
) -> str:
    query = {
        "client_id": client.client_id,
        "redirect_uri": f"http://127.0.0.1:{port}/oauth/callback",
        "response_type": "code",
        "scope": DRIVE_READONLY_SCOPE if persistent else DRIVE_FILE_SCOPE,
        "access_type": "offline" if persistent else "online",
        "prompt": "consent",
        "trigger_onepick": "true",
        "include_granted_scopes": "false",
        "allow_multiple": "false",
        "allow_folder_selection": "false",
        "mimetypes": ",".join(
            "application/vnd.google-apps." + kind
            for kind in ("document", "spreadsheet", "presentation")
        ),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": account,
    }
    if persistent:
        for name in ("trigger_onepick", "allow_multiple", "allow_folder_selection", "mimetypes"):
            query.pop(name)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query)


def callback_result(target: str, state: str, *, persistent: bool = False) -> tuple[str, str]:
    url = urlsplit(target)
    if len(target) > 16384 or url.path != "/oauth/callback" or url.netloc or url.fragment:
        raise SourceArchiveError("scope_denied")
    values = parse_qs(url.query, keep_blank_values=True, max_num_fields=12)
    if (
        any(len(items) != 1 for items in values.values())
        or not hmac.compare_digest(
            values.get("state", [""])[0].encode("utf-8"), state.encode("utf-8")
        )
        or values.get("scope") != [DRIVE_READONLY_SCOPE if persistent else DRIVE_FILE_SCOPE]
        or "error" in values
    ):
        raise SourceArchiveError("scope_denied")
    code, selected = values.get("code", [""])[0], values.get("picked_file_ids", [""])[0]
    if (
        not code
        or len(code) > 8192
        or (not persistent and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", selected))
        or (persistent and selected)
    ):
        raise SourceArchiveError("scope_denied")
    return code, selected


def verified_identity(value: Mapping[str, object], account: str) -> str:
    user = value.get("user")
    if (
        not isinstance(user, dict)
        or user.get("me") is not True
        or not isinstance(user.get("emailAddress"), str)
        or user["emailAddress"].casefold() != account.casefold()
        or not isinstance(user.get("permissionId"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user["permissionId"])
    ):
        raise SourceArchiveError("scope_denied")
    principal = user["permissionId"]
    assert isinstance(principal, str)
    return "google:" + principal


def _authorization_form(
    client: DesktopClient, account: str, cancelled: threading.Event, *, persistent: bool
) -> tuple[dict[str, str], str]:
    """One state/PKCE-bound browser exchange; no tokens or prompt content logged."""

    def check_active() -> None:
        if cancelled.is_set():
            raise SourceArchiveError("scope_denied")

    check_active()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    result: list[tuple[str, str]] = []
    refused = False
    consent_started = False
    nonce = secrets.token_urlsafe(32)

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(3)
            super().setup()

        def log_message(self, format: str, *args: object) -> None:
            # OAuth query parameters contain a code; never use HTTP access logs.
            pass

        def reply(self, status: int, page: bytes, *, location: str | None = None) -> None:
            nonlocal refused
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            # Chrome uses an opaque Origin for a no-referrer form POST. Preserve
            # same-origin form validation without sending a referrer to Google.
            self.send_header(
                "Referrer-Policy", "same-origin" if self.path == "/oauth/review" else "no-referrer"
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com",
            )
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            try:
                self.wfile.write(page)
            except (BrokenPipeError, ConnectionResetError):
                refused = True

        def valid_host(self) -> bool:
            assert isinstance(self.server, HTTPServer)
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [expected_host]:
                self.reply(400, b"Invalid local host.")
                return False
            return True

        def do_POST(self) -> None:
            nonlocal refused, consent_started
            if not self.valid_host():
                return
            if self.path != "/oauth/start":
                self.reply(404, b"Unknown sign-in action.")
                return
            assert isinstance(self.server, HTTPServer)
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (
                consent_started
                or cancelled.is_set()
                or self.headers.get_all("Origin") not in (None, [origin])
            ):
                self.reply(403, b"Sign-in action refused.")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
                or not 0 < int(lengths[0]) <= 1024
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type") != "application/x-www-form-urlencoded"
            ):
                self.reply(400, b"Invalid sign-in form.")
                return
            try:
                fields = parse_qs(
                    self.rfile.read(int(lengths[0])).decode("ascii"),
                    keep_blank_values=True,
                    max_num_fields=2,
                )
            except (OSError, ValueError):
                self.reply(400, b"Invalid sign-in form.")
                return
            if (
                set(fields) != {"nonce", "decision"}
                or len(fields["nonce"]) != 1
                or not hmac.compare_digest(
                    fields["nonce"][0].encode("utf-8"), nonce.encode("ascii")
                )
                or fields["decision"] not in (["continue"], ["cancel"])
            ):
                self.reply(403, b"Sign-in action refused.")
                return
            if fields["decision"] == ["cancel"] or cancelled.is_set():
                refused = True
                self.reply(
                    200, b"Sign-in cancelled. No Google access was requested. Return to Codex."
                )
                return
            consent_started = True
            self.reply(
                303,
                b"Continue with Google.",
                location=authorization_url(
                    client,
                    self.server.server_port,
                    state,
                    challenge,
                    account,
                    persistent=persistent,
                ),
            )

        def do_GET(self) -> None:
            nonlocal refused
            if not self.valid_host():
                return
            if self.path == "/oauth/review":
                description = PERSISTENT_CONSENT_DESCRIPTION if persistent else CONSENT_DESCRIPTION
                self.reply(
                    200,
                    (
                        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Tedd — Google source access</title>'
                        "<h1>Connect Google sources to Tedd</h1>"
                        f"<p>Account: <strong>{html.escape(account)}</strong></p><p>{html.escape(description)}</p>"
                        "<p>This attempt expires after five minutes. If it expires, return to Codex for a new sign-in; do not refresh an old callback page.</p>"
                        '<form method="post" action="/oauth/start">'
                        f'<input type="hidden" name="nonce" value="{nonce}">'
                        '<button name="decision" value="continue">Continue to Google</button> '
                        '<button name="decision" value="cancel">Cancel</button></form></html>'
                    ).encode("utf-8"),
                )
                return
            if not self.path.startswith("/oauth/callback"):
                self.reply(404, b"Unknown sign-in page.")
                return
            if not consent_started or cancelled.is_set():
                self.reply(403, b"Start with Tedd's permission review. Return to Codex.")
                return
            try:
                result.append(callback_result(self.path, state, persistent=persistent))
            except (SourceArchiveError, ValueError):
                refused = True
            self.reply(
                400 if refused else 200,
                b"Sign-in refused. Return to Codex for a new attempt."
                if refused
                else b"Sign-in response received. Return to Codex. Tedd will verify the account and permissions before using sources.",
            )

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        port = server.server_port
        check_active()
        if not webbrowser.open(f"http://127.0.0.1:{port}/oauth/review"):
            raise SourceArchiveError("capability_unavailable")
        deadline = time.monotonic() + 300
        while not result and not refused and time.monotonic() < deadline:
            check_active()
            server.handle_request()
        if refused or not result:
            raise SourceArchiveError("scope_denied")
    check_active()
    code, selected = result[0]
    return {
        "code": code,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "redirect_uri": f"http://127.0.0.1:{port}/oauth/callback",
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, selected


def credential_key(client: DesktopClient, account: str) -> str:
    return digest(encode(["tedd-google-credential-v1", client.client_id, account.casefold()]))


def sign_in(
    client_path: Path,
    *,
    account: str,
    scope: SourceScope,
    cancelled: threading.Event,
    credentials: CredentialStore | None = None,
    selected_ids: frozenset[str] = frozenset(),
    folder_ids: frozenset[str] = frozenset(),
) -> GoogleSourceSession:
    """Reuse verified credentials, never prior task grants or connector tokens."""

    connection: GoogleConnection | None = None
    snapshot: CredentialSnapshot | None = None

    def check_active() -> None:
        if cancelled.is_set():
            raise SourceArchiveError("scope_denied")
        if connection is not None and snapshot is not None:
            with connection.hold(snapshot.stamp):
                pass

    check_active()
    persistent = credentials is not None
    if persistent and (
        not (selected_ids or folder_ids)
        or len(selected_ids) + len(folder_ids) > 256
        or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", item) for item in selected_ids | folder_ids
        )
    ):
        raise SourceArchiveError("scope_denied")
    check_active()
    client = decode_client(parse_json(source_files.read_bytes(client_path, 65536)))
    key = credential_key(client, account)
    if credentials is not None:
        connection = GoogleConnection(credentials, key)
        snapshot = connection.snapshot()
        check_active()
    saved_bytes = snapshot.payload if snapshot is not None else None
    saved = google_object(saved_bytes) if saved_bytes is not None else None
    requested_scope = DRIVE_READONLY_SCOPE if persistent else DRIVE_FILE_SCOPE
    expected = {
        "schema": "tedd-google-credential-v1",
        "client_id": client.client_id,
        "account": account.casefold(),
        "scope": requested_scope,
    }
    selected = ""
    if saved is not None:
        refresh = saved.get("refresh_token")
        if (
            set(saved) != set(expected) | {"principal", "refresh_token"}
            or any(saved.get(name) != value for name, value in expected.items())
            or not isinstance(saved.get("principal"), str)
            or not isinstance(refresh, str)
            or not refresh
            or len(refresh) > 8192
        ):
            raise SourceArchiveError("privacy_rejected")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        }
    else:
        check_active()
        form, selected = _authorization_form(client, account, cancelled, persistent=persistent)
    check_active()
    issued_at = time.monotonic()
    try:
        token = google_object(
            google_request(
                "https://oauth2.googleapis.com/token",
                maximum=65536,
                form=form,
            )
        )
    except SourceArchiveError as error:
        if (
            error.code == "reauthentication_required"
            and saved is not None
            and connection is not None
            and snapshot is not None
        ):
            connection.reject(snapshot)
        raise
    bearer, lifetime = token.get("access_token"), token.get("expires_in")
    if (
        not isinstance(bearer, str)
        or token.get("token_type") != "Bearer"
        or token.get("scope") != requested_scope
        or type(lifetime) is not int
        or not 0 < lifetime <= 3600
    ):
        raise SourceArchiveError("scope_denied")
    check_active()
    identity = google_object(
        google_request(
            "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress,permissionId,me)",
            maximum=65536,
            token=bearer,
        )
    )
    check_active()
    principal = verified_identity(identity, account)
    if saved is not None and principal != saved["principal"]:
        raise SourceArchiveError("scope_denied")
    if connection is not None and snapshot is not None:
        refresh = token.get("refresh_token", saved.get("refresh_token") if saved else None)
        if not isinstance(refresh, str) or not refresh or len(refresh) > 8192:
            raise SourceArchiveError("scope_denied")
        updated = encode({**expected, "principal": principal, "refresh_token": refresh})
        check_active()
        snapshot = connection.replace(snapshot, updated, new_grant=saved is None)
    return GoogleSourceSession(
        bearer,
        principal,
        selected_ids if persistent else frozenset({selected}),
        scope,
        issued_at + lifetime,
        folder_ids=folder_ids if persistent else frozenset(),
        _connection=connection,
        _stamp=snapshot.stamp if snapshot is not None else None,
    )
