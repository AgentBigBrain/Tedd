"""Finite Google API reads within trusted user-linked files and folders.

Folder listings return bounded metadata only; no writes or graph mutation.
Authorization is checked on each operation; it is not a promise of instantaneous
remote revocation. The caller still owns cancellation and local publication locks.
"""

from __future__ import annotations

import http.client
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import cast
from urllib.parse import urlencode, urlsplit

from .legacy_values import utc_now
from .source_authority import AccessDecision, access_binding
from .source_contracts import SourceArchiveError, SourceMetadata, SourceScope, parse_json
from .source_google_connection import ConnectionStamp, GoogleConnection

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_FOLDER = "application/vnd.google-apps.folder"
_KINDS = {
    "google_doc": ("document", "docs.googleapis.com/v1/documents", "includeTabsContent=true"),
    "google_sheet": (
        "spreadsheet",
        "sheets.googleapis.com/v4/spreadsheets",
        "includeGridData=true",
    ),
    "google_slide": ("presentation", "slides.googleapis.com/v1/presentations", ""),
}
_HOSTS = frozenset(
    {
        "www.googleapis.com",
        "docs.googleapis.com",
        "sheets.googleapis.com",
        "slides.googleapis.com",
        "oauth2.googleapis.com",
    }
)


def google_request(
    url: str, *, maximum: int, token: str | None = None, form: dict[str, str] | None = None
) -> bytes:
    """One bounded TLS request; errors never include provider bodies or tokens."""
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or target.hostname not in _HOSTS
        or target.netloc != target.hostname
        or target.fragment
        or type(maximum) is not int
        or not 0 < maximum <= 64 * 1024**2
        or (
            form is not None
            and url
            not in ("https://oauth2.googleapis.com/token", "https://oauth2.googleapis.com/revoke")
        )
    ):
        raise SourceArchiveError("scope_denied")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token is not None:
        if not token or len(token) > 8192 or not token.isascii() or any(c.isspace() for c in token):
            raise SourceArchiveError("scope_denied")
        headers["Authorization"] = "Bearer " + token
    body = urlencode(form).encode("ascii") if form is not None else None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection = http.client.HTTPSConnection(target.hostname, timeout=30)
    try:
        connection.request(
            "POST" if body is not None else "GET",
            target.path + ("?" + target.query if target.query else ""),
            body,
            headers,
        )
        response = connection.getresponse()
        if (
            response.status == 400
            and url == "https://oauth2.googleapis.com/token"
            and form is not None
            and form.get("grant_type") == "refresh_token"
            and response.getheader("Content-Encoding", "identity") == "identity"
        ):
            failure = response.read(65537)
            if len(failure) <= 65536 and google_object(failure).get("error") == "invalid_grant":
                raise SourceArchiveError("reauthentication_required")
        if response.status == 401:
            raise SourceArchiveError("reauthentication_required")
        if response.status in (403, 404):
            raise SourceArchiveError("scope_denied")
        if response.status != 200:
            raise SourceArchiveError("source_unavailable")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise SourceArchiveError("source_unavailable")
        payload = response.read(maximum + 1)
        if len(payload) > maximum:
            raise SourceArchiveError("limit_exceeded")
        return payload
    except SourceArchiveError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise SourceArchiveError("source_unavailable") from None
    finally:
        connection.close()


def google_object(payload: bytes) -> dict[str, object]:
    value = parse_json(payload)
    if not isinstance(value, dict):
        raise SourceArchiveError("source_unavailable")
    return cast(dict[str, object], value)


@dataclass(repr=False)
class GoogleSourceSession:
    """Construct only from verified OAuth and Picker output, not model arguments."""

    _token: str = field(repr=False)
    principal: str
    selected_ids: frozenset[str]
    scope: SourceScope
    expires_at: float
    folder_ids: frozenset[str] = frozenset()
    _connection: GoogleConnection | None = None
    _stamp: ConnectionStamp | None = None

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Short local generation fence; never keep it over provider I/O."""
        if self._connection is None:
            if self._stamp is not None:
                raise SourceArchiveError()
            yield
        else:
            if self._stamp is None:
                raise SourceArchiveError()
            with self._connection.hold(self._stamp):
                yield

    def _get(self, url: str, maximum: int) -> bytes:
        with self.hold():
            if not self._token or time.monotonic() >= self.expires_at:
                raise SourceArchiveError("scope_denied")
            token = self._token
        result = google_request(url, maximum=maximum, token=token)
        with self.hold():
            return result

    def disconnect(self) -> None:
        """Discard this bearer and task grant; an optional OS connection is separate."""
        self._token = ""
        self.selected_ids = frozenset()
        self.folder_ids = frozenset()

    def _metadata(self, resource: str) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", resource):
            raise SourceArchiveError("scope_denied")
        fields = (
            "id,mimeType,version,trashed,parents,"
            "capabilities(canCopy,canDownload,canAccessViaGenAi,canListChildren)"
        )
        value = google_object(
            self._get(
                "https://www.googleapis.com/drive/v3/files/"
                + resource
                + "?"
                + urlencode({"fields": fields, "supportsAllDrives": "true"}),
                65536,
            )
        )
        if value.get("id") != resource or value.get("trashed") is not False:
            raise SourceArchiveError("scope_denied")
        return value

    def _within_folder(self, value: dict[str, object]) -> None:
        """Prove current ancestry without following shortcuts or returning names."""
        seen: set[str] = set()
        current = value
        for _ in range(64):
            resource = current.get("id")
            if not isinstance(resource, str) or resource in seen:
                raise SourceArchiveError("scope_denied")
            seen.add(resource)
            if resource in self.folder_ids:
                capabilities = current.get("capabilities")
                if (
                    current.get("mimeType") != _FOLDER
                    or not isinstance(capabilities, dict)
                    or capabilities.get("canListChildren") is not True
                ):
                    raise SourceArchiveError("scope_denied")
                return
            parents = current.get("parents")
            if not isinstance(parents, list) or len(parents) != 1:
                raise SourceArchiveError("scope_denied")
            parent = parents[0]
            if not isinstance(parent, str) or parent in seen:
                raise SourceArchiveError("scope_denied")
            current = self._metadata(parent)
            if current.get("mimeType") != _FOLDER:
                raise SourceArchiveError("scope_denied")
        raise SourceArchiveError("limit_exceeded")

    def list_folder(self, resource: str, page: str = "") -> dict[str, object]:
        """One page of in-scope names/IDs, never source content or recursive capture."""
        if not self.folder_ids or len(page) > 8192:
            raise SourceArchiveError("scope_denied")
        before = self._metadata(resource)
        self._within_folder(before)
        capabilities = before.get("capabilities")
        if (
            before.get("mimeType") != _FOLDER
            or not isinstance(capabilities, dict)
            or capabilities.get("canListChildren") is not True
        ):
            raise SourceArchiveError("scope_denied")
        query = {
            "q": f"'{resource}' in parents and trashed = false",
            "pageSize": "100",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "nextPageToken,files(id,name,mimeType,parents,trashed)",
        }
        if page:
            query["pageToken"] = page
        result = google_object(
            self._get(
                "https://www.googleapis.com/drive/v3/files?" + urlencode(query),
                262144,
            )
        )
        files, next_page = result.get("files"), result.get("nextPageToken", "")
        if (
            not isinstance(files, list)
            or len(files) > 100
            or not isinstance(next_page, str)
            or len(next_page) > 8192
        ):
            raise SourceArchiveError("source_unavailable")
        rows = []
        kinds = {"application/vnd.google-apps." + value[0]: key for key, value in _KINDS.items()}
        kinds[_FOLDER] = "folder"
        for item in files:
            if (
                not isinstance(item, dict)
                or item.get("parents") != [resource]
                or item.get("trashed") is not False
                or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", item["id"])
                or not isinstance(item.get("name"), str)
                or len(item["name"]) > 1024
                or not isinstance(item.get("mimeType"), str)
            ):
                raise SourceArchiveError("source_unavailable")
            rows.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "kind": kinds.get(item["mimeType"], "unsupported"),
                }
            )
        after = self._metadata(resource)
        self._within_folder(after)
        if before != after:
            raise SourceArchiveError("scope_denied")
        return {"files": rows, "next_page": next_page}

    def prepare(self, source: SourceMetadata) -> SourceMetadata:
        if source.details is None:
            raise SourceArchiveError("scope_denied")
        bound = replace(source, details=replace(source.details, principal=self.principal))
        value = self._file(bound, capture=False, audience="model")
        version = value["version"]
        assert isinstance(version, str)
        now = utc_now()
        return replace(
            bound,
            source_version="google-drive-version:" + version,
            captured_at=now,
            observed_at=now,
            details=replace(source.details, principal=self.principal, provider_revision=version),
        )

    def _file(self, source: SourceMetadata, *, capture: bool, audience: str) -> dict[str, object]:
        details = source.details
        if (
            not self._token
            or time.monotonic() >= self.expires_at
            or source.scope != self.scope
            or (source.source_id not in self.selected_ids and not self.folder_ids)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", source.source_id)
            or source.kind not in _KINDS
            or details is None
            or details.provider != "google"
            or details.principal != self.principal
            or details.representation != "structured_observation"
        ):
            raise SourceArchiveError("scope_denied")
        value = self._metadata(source.source_id)
        if source.source_id not in self.selected_ids:
            self._within_folder(value)
        capabilities = value.get("capabilities")
        if (
            value.get("id") != source.source_id
            or value.get("trashed") is not False
            or value.get("mimeType") != "application/vnd.google-apps." + _KINDS[source.kind][0]
            or not isinstance(capabilities, dict)
            or any(capabilities.get(key) is not True for key in ("canCopy", "canDownload"))
            or (
                (capture or audience == "model")
                and capabilities.get("canAccessViaGenAi") is not True
            )
            or not isinstance(value.get("version"), str)
            or (capture and value["version"] != details.provider_revision)
        ):
            raise SourceArchiveError("scope_denied")
        version = value["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,32}", version):
            raise SourceArchiveError("scope_denied")
        return value

    def check(
        self, source: SourceMetadata, fingerprint: str, operation: str, audience: str
    ) -> AccessDecision:
        binding = access_binding(source, fingerprint)
        try:
            self._file(source, capture=operation == "capture", audience=audience)
        except SourceArchiveError as error:
            if error.code == "reauthentication_required":
                raise
            status = "denied" if error.code == "scope_denied" else "unknown"
        else:
            status = "allowed"
        return AccessDecision(binding, operation, audience, status)

    def acquire(self, source: SourceMetadata, maximum: int) -> bytes:
        before = self._file(source, capture=True, audience="model")
        _, endpoint, query = _KINDS[source.kind]
        payload = self._get(
            "https://" + endpoint + "/" + source.source_id + ("?" + query if query else ""), maximum
        )
        after = self._file(source, capture=True, audience="model")
        if before != after:
            raise SourceArchiveError("source_unavailable")
        return payload
