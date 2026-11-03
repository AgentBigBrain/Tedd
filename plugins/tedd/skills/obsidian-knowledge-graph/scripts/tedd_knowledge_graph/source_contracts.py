"""Closed, content-conscious values for the private source archive.

These values bind inputs; they do not authenticate the caller or decide whether
a natural-language statement is true. The trusted workflow supplies authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import NoReturn, cast

from .legacy_values import scrub_value, secret_hits
from .v4_contracts import canonical_json

MANIFEST_BYTES = 1024 * 1024
READING_BYTES = 32 * 1024 * 1024
PDF_BYTES = 64 * 1024 * 1024
CHAT_BYTES = 8 * 1024 * 1024
MAX_UNITS = 4096
SOURCE_TITLE_CHARS = 256
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HASH = re.compile(r"[a-f0-9]{64}")
_ANCHOR = re.compile(r"u[1-9][0-9]{0,8}")
_URI = re.compile(r"tedd-source://([a-f0-9]{32})/([a-f0-9]{64})/([a-f0-9]{64})#(u[1-9][0-9]{0,8})")
# Persisted field sets are pinned independently of evolving Python values.
_LIMIT_FIELDS = {"pdf_bytes", "chat_bytes", "manifest_bytes", "reading_bytes"}
_METADATA_FIELDS = {
    "scope",
    "source_id",
    "source_version",
    "kind",
    "capture_method",
    "tool_id",
    "task_id",
    "run_id",
    "interaction_id",
    "captured_at",
    "observed_at",
    "origin",
    "authorship",
    "coverage",
    "gaps",
    "privacy",
    "identity_known",
}
_MANIFEST_FIELDS = {
    "format",
    "metadata",
    "payload_sha256",
    "payload_bytes",
    "reading_sha256",
    "sequence",
    "predecessor",
    "anchor_ids",
}
_UNIT_FIELDS = {
    "anchor_id",
    "text",
    "position",
    "kind",
    "source_id",
    "speaker",
    "source_time",
    "reply_to",
    "links",
}
TEXT_KINDS = frozenset(
    {
        "chat",
        "text",
        "markdown",
        "email",
        "code",
        "tool_result",
        "web",
        "google_doc",
        "google_sheet",
        "google_slide",
    }
)
FILE_KINDS = frozenset({"pdf", "docx", "xlsx", "pptx", "image"})
# Persisted source-v3 hash vocabulary: append only, even if a reader is retired.
# Admission sets above are not a migration or authority to forget old exclusions.
_LEGACY_IDENTITY_KINDS = (
    "chat",
    "code",
    "docx",
    "email",
    "google_doc",
    "google_sheet",
    "google_slide",
    "markdown",
    "pdf",
    "pptx",
    "text",
    "tool_result",
    "web",
    "xlsx",
)
OFFICE_IMAGE_READERS = (
    "tedd-ooxml:4",
    "tedd-ooxml:5",
    "tedd-ooxml:6",
    "tedd-ooxml:7",
    "tedd-ooxml:8",
)


class SourceArchiveError(ValueError):
    """A stable public error; never put source text or local paths in it."""

    def __init__(self, code: str = "source_corrupt") -> None:
        self.code = code
        super().__init__(code)


def decoded_png(value: Mapping[str, object]) -> bytes:
    """Check a bounded trusted-worker image handoff, not source-content safety."""
    encoded = value.get("png_base64")
    if not isinstance(encoded, str) or len(encoded) > READING_BYTES:
        raise SourceArchiveError()
    try:
        png = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise SourceArchiveError() from None
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or digest(png) != value.get("png_sha256"):
        raise SourceArchiveError()
    return png


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def safe_display(value: object) -> object:
    """Apply the existing path/secret scrubber to selected display-only fields."""
    return scrub_value(value, redact_paths=True)[0]


def encode(value: object) -> bytes:
    try:
        return canonical_json(value).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise SourceArchiveError() from error


def _reject_number(_raw: str) -> NoReturn:
    raise SourceArchiveError()


def _integer(raw: str) -> int:
    if len(raw.lstrip("-")) > 19:
        raise SourceArchiveError()
    return int(raw)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceArchiveError()
        result[key] = value
    return result


def check_json_bounds(payload: bytes, maximum_bytes: int) -> None:
    """Shared pre-parse bounds; provider numeric parsing remains a separate boundary."""
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise SourceArchiveError("limit_exceeded")
    quoted = escaped = False
    depth = 0
    for char in payload:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > 20:
                raise SourceArchiveError("limit_exceeded")
        elif char in (93, 125):
            depth -= 1


def parse_json(payload: bytes, *, maximum_bytes: int = MANIFEST_BYTES) -> object:
    """Decode strict internal JSON; provider number handling must not change this."""
    check_json_bounds(payload, maximum_bytes)
    try:
        return cast(
            object,
            json.loads(
                payload,
                object_pairs_hook=unique_object,
                parse_int=_integer,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, SourceArchiveError):
            raise
        raise SourceArchiveError() from error


def object_fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise SourceArchiveError()
    return cast(dict[str, object], value)


def text(value: object, *, maximum: int = 4096, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise SourceArchiveError()
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise SourceArchiveError() from error
    if secret_hits(value):
        raise SourceArchiveError("privacy_rejected")
    return value


def identifier(value: object) -> str:
    result = text(value, maximum=128)
    if not _ID.fullmatch(result) or result in (".", ".."):
        raise SourceArchiveError()
    return result


def legacy_source_key(source_id: str) -> str:
    return digest(encode(["source", text(source_id)]))


def sha256(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SourceArchiveError()
    return value


def positive(value: object, *, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SourceArchiveError()
    return value


def optional_text(value: object) -> str | None:
    return None if value is None else text(value)


@dataclass(frozen=True)
class ArchiveLimits:
    """Acquisition ceilings; hard format/read limits remain independent."""

    pdf_bytes: int = PDF_BYTES
    chat_bytes: int = CHAT_BYTES
    manifest_bytes: int = MANIFEST_BYTES
    reading_bytes: int = READING_BYTES

    def __post_init__(self) -> None:
        positive(self.pdf_bytes, maximum=PDF_BYTES)
        positive(self.chat_bytes, maximum=CHAT_BYTES)
        positive(self.manifest_bytes, maximum=MANIFEST_BYTES)
        positive(self.reading_bytes, maximum=READING_BYTES)

    def for_kind(self, kind: str) -> int:
        """Reject unknown kinds rather than assigning them the binary ceiling."""
        if kind in TEXT_KINDS:
            return self.chat_bytes
        if kind in FILE_KINDS:
            return self.pdf_bytes
        raise SourceArchiveError("capability_unavailable")


def decode_limits(value: object) -> ArchiveLimits:
    obj = object_fields(value, _LIMIT_FIELDS)
    return ArchiveLimits(
        positive(obj["pdf_bytes"]),
        positive(obj["chat_bytes"]),
        positive(obj["manifest_bytes"]),
        positive(obj["reading_bytes"]),
    )


def timestamp(value: object) -> str:
    result = text(value)
    try:
        instant = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceArchiveError() from error
    if instant.tzinfo is None:
        raise SourceArchiveError()
    return result


@dataclass(frozen=True, repr=False)
class SourceScope:
    store_id: str
    generation: str
    subject_ids: tuple[str, ...]
    namespace: str

    def __post_init__(self) -> None:
        identifier(self.store_id)
        identifier(self.generation)
        text(self.namespace, maximum=128)
        if (
            type(self.subject_ids) is not tuple
            or not self.subject_ids
            or len(self.subject_ids) > 128
            or len(set(self.subject_ids)) != len(self.subject_ids)
        ):
            raise SourceArchiveError("scope_denied")
        for subject in self.subject_ids:
            identifier(subject)

    @property
    def key(self) -> str:
        return digest(
            encode([self.store_id, self.generation, self.namespace, sorted(self.subject_ids)])
        )


@dataclass(frozen=True)
class SourceRef:
    archive_id: str
    scope_id: str
    manifest_sha256: str
    anchor_id: str

    def __post_init__(self) -> None:
        if not _URI.fullmatch(self.as_uri()):
            raise SourceArchiveError()

    def as_uri(self) -> str:
        return f"tedd-source://{self.archive_id}/{self.scope_id}/{self.manifest_sha256}#{self.anchor_id}"


def parse_source_uri(value: str) -> SourceRef:
    if not isinstance(value, str):
        raise SourceArchiveError()
    match = _URI.fullmatch(value)
    if match is None:
        raise SourceArchiveError()
    return SourceRef(*match.groups())


@dataclass(frozen=True, repr=False)
class SourceLink:
    kind: str
    source_id: str | None
    version: str | None
    unit_id: str | None
    archived_uri: str | None

    def __post_init__(self) -> None:
        if self.kind not in ("reply", "quote", "forward", "attachment"):
            raise SourceArchiveError()
        for value in (self.source_id, self.version, self.unit_id):
            optional_text(value)
        if self.archived_uri is not None:
            parse_source_uri(self.archived_uri)


def decode_link(value: object) -> SourceLink:
    obj = object_fields(value, {"kind", "source_id", "version", "unit_id", "archived_uri"})
    return SourceLink(
        text(obj["kind"]),
        optional_text(obj["source_id"]),
        optional_text(obj["version"]),
        optional_text(obj["unit_id"]),
        optional_text(obj["archived_uri"]),
    )


@dataclass(frozen=True, repr=False)
class SourceLocation:
    """One-based inclusive native range in an immutable representation.

    ``utf16`` converts a provider's zero-based half-open text range by adding one
    to its start only. It is distinct from paragraph ordinals in a saved document.
    A location never grants access.
    """

    kind: str
    container: str | None
    start: int
    end: int
    column_start: int | None = None
    column_end: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("line", "paragraph", "utf16", "cell", "slide", "page", "message"):
            raise SourceArchiveError()
        if self.container is not None:
            text(self.container, maximum=256)
        positive(self.start, maximum=1_000_000_000)
        positive(self.end, maximum=1_000_000_000)
        if self.end < self.start:
            raise SourceArchiveError()
        if self.kind == "cell":
            if self.container is None:
                raise SourceArchiveError()
            first = positive(self.column_start, maximum=16384)
            last = positive(self.column_end, maximum=16384)
            if last < first:
                raise SourceArchiveError()
        elif self.column_start is not None or self.column_end is not None:
            raise SourceArchiveError()


def decode_location(value: object) -> SourceLocation:
    obj = object_fields(value, {"kind", "container", "start", "end", "column_start", "column_end"})
    return SourceLocation(
        text(obj["kind"]),
        optional_text(obj["container"]),
        positive(obj["start"]),
        positive(obj["end"]),
        None if obj["column_start"] is None else positive(obj["column_start"]),
        None if obj["column_end"] is None else positive(obj["column_end"]),
    )


@dataclass(frozen=True, repr=False)
class SheetExcerpt:
    """Owner-asserted origin of a copied Google sheet, not provider proof."""

    source_id: str
    sheet_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.source_id) is None
            or not isinstance(self.sheet_id, str)
            or re.fullmatch(r"0|[1-9][0-9]{0,19}", self.sheet_id) is None
        ):
            raise SourceArchiveError("scope_denied")


def decode_sheet_excerpt(value: object) -> SheetExcerpt:
    obj = object_fields(value, {"source_id", "sheet_id"})
    return SheetExcerpt(text(obj["source_id"]), text(obj["sheet_id"]))


@dataclass(frozen=True, repr=False)
class SourceDetails:
    """Source facts supplied by acquisition; none authenticates provider access."""

    provider: str
    principal: str
    representation_id: str
    representation: str
    reader_id: str
    provider_revision: str | None = None
    title: str | None = None
    source_time: str | None = None
    retention: str = "retained_copy"
    sensitivity: str = "unknown"
    identity_version: int = 1
    sheet_excerpt: SheetExcerpt | None = None

    def __post_init__(self) -> None:
        if type(self.identity_version) is not int or self.identity_version not in (1, 2):
            raise SourceArchiveError()
        if self.sheet_excerpt is not None and (
            not isinstance(self.sheet_excerpt, SheetExcerpt) or self.identity_version != 2
        ):
            raise SourceArchiveError()
        identifier(self.provider)
        for value in (self.principal, self.representation_id, self.reader_id):
            text(value, maximum=256)
        if self.representation not in (
            "original",
            "provider_export",
            "structured_observation",
            "derivative",
        ):
            raise SourceArchiveError()
        optional_text(self.provider_revision)
        if self.title is not None:
            text(self.title, maximum=SOURCE_TITLE_CHARS)
        if self.source_time is not None:
            timestamp(self.source_time)
        if self.retention not in ("retained_copy", "access_dependent") or self.sensitivity not in (
            "ordinary",
            "confidential",
            "medical",
            "restricted",
            "unknown",
        ):
            raise SourceArchiveError()


def decode_details(value: object) -> SourceDetails:
    versioned = isinstance(value, dict) and "identity_version" in value
    excerpt = isinstance(value, dict) and "sheet_excerpt" in value
    obj = object_fields(
        value,
        {
            "provider",
            "principal",
            "representation_id",
            "representation",
            "reader_id",
            "provider_revision",
            "title",
            "source_time",
            "retention",
            "sensitivity",
        }
        | ({"identity_version"} if versioned else set())
        | ({"sheet_excerpt"} if excerpt else set()),
    )
    return SourceDetails(
        text(obj["provider"]),
        text(obj["principal"]),
        text(obj["representation_id"]),
        text(obj["representation"]),
        text(obj["reader_id"]),
        optional_text(obj["provider_revision"]),
        optional_text(obj["title"]),
        optional_text(obj["source_time"]),
        text(obj["retention"]),
        text(obj["sensitivity"]),
        positive(obj["identity_version"], maximum=2) if versioned else 1,
        decode_sheet_excerpt(obj["sheet_excerpt"])
        if excerpt and obj["sheet_excerpt"] is not None
        else None,
    )


@dataclass(frozen=True, repr=False)
class SourceUnit:
    anchor_id: str
    text: str
    position: int
    kind: str = "message"
    source_id: str | None = None
    speaker: str | None = None
    source_time: str | None = None
    reply_to: str | None = None
    links: tuple[SourceLink, ...] = ()
    location: SourceLocation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_id, str) or not _ANCHOR.fullmatch(self.anchor_id):
            raise SourceArchiveError()
        text(self.text, maximum=128 * 1024, empty=True)
        positive(self.position)
        if self.kind not in (
            "message",
            "page",
            "ocr_page",
            "image_page",
            "line",
            "section",
            "row",
            "slide",
            "receipt",
        ):
            raise SourceArchiveError()
        if self.location is not None and not isinstance(self.location, SourceLocation):
            raise SourceArchiveError()
        if self.kind in ("line", "section", "row", "slide", "receipt") and self.location is None:
            raise SourceArchiveError()
        if self.location is not None:
            expected = {
                "message": "message",
                "page": "page",
                "ocr_page": "page",
                "image_page": "page",
                "line": "line",
                "section": self.location.kind
                if self.location.kind in ("paragraph", "utf16")
                else "paragraph",
                "row": "cell",
                "slide": "slide",
                "receipt": "line",
            }[self.kind]
            if self.location.kind != expected:
                raise SourceArchiveError()
        for value in (self.source_id, self.speaker, self.source_time, self.reply_to):
            optional_text(value)
        if type(self.links) is not tuple or len(self.links) > 32:
            raise SourceArchiveError()
        for link in self.links:
            if not isinstance(link, SourceLink):
                raise SourceArchiveError()


@dataclass(frozen=True, repr=False)
class SourceMetadata:
    scope: SourceScope
    source_id: str
    source_version: str
    kind: str
    capture_method: str
    tool_id: str
    task_id: str
    run_id: str
    interaction_id: str
    captured_at: str
    observed_at: str | None = None
    origin: str | None = None
    authorship: str = "unknown"
    coverage: str = "partial"
    gaps: tuple[str, ...] = ()
    privacy: str = "original"
    identity_known: bool = True
    details: SourceDetails | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SourceScope):
            raise SourceArchiveError("scope_denied")
        for value in (self.source_id, self.source_version, self.tool_id):
            text(value)
        for value in (self.task_id, self.run_id, self.interaction_id):
            identifier(value)
        timestamp(self.captured_at)
        if self.observed_at is not None:
            timestamp(self.observed_at)
        optional_text(self.origin)
        if self.details is None:
            if self.kind not in ("chat", "pdf") or self.capture_method not in (
                "structured_export",
                "observed_dom_snapshot",
                "pdf_original",
            ):
                raise SourceArchiveError()
            if (self.kind == "pdf") != (self.capture_method == "pdf_original"):
                raise SourceArchiveError()
        else:
            if not isinstance(self.details, SourceDetails):
                raise SourceArchiveError()
            ArchiveLimits().for_kind(self.kind)
            if self.capture_method not in (
                "local_file",
                "provider_export",
                "owner_confirmed_export",
                "structured_observation",
                "delivered_output",
            ):
                raise SourceArchiveError()
        if self.authorship not in ("unknown", "external", "operator", "assistant"):
            raise SourceArchiveError()
        if (
            self.details is not None
            and self.details.sheet_excerpt is not None
            and (
                self.capture_method != "owner_confirmed_export"
                or self.kind != "xlsx"
                or self.source_id == self.details.sheet_excerpt.source_id
                or not self.identity_known
                or self.coverage != "partial"
            )
        ):
            raise SourceArchiveError("scope_denied")
        if self.capture_method == "owner_confirmed_export" and (
            self.kind not in ("docx", "xlsx", "pptx")
            or self.details is None
            or self.details.provider != "google"
            or self.details.sensitivity != "ordinary"
            or self.details.representation != "provider_export"
            or self.details.retention != "retained_copy"
            or self.details.provider_revision is not None
            or self.details.identity_version != 2
        ):
            # Do not let a local capture's serialized metadata claim that it
            # passed the distinct owner-confirmation route. This is a format
            # invariant, not authority to capture even a well-formed export.
            raise SourceArchiveError("scope_denied")
        if self.coverage not in ("complete", "partial") or self.privacy not in (
            "original",
            "redacted",
        ):
            raise SourceArchiveError()
        if (
            type(self.identity_known) is not bool
            or type(self.gaps) is not tuple
            or len(self.gaps) > 128
        ):
            raise SourceArchiveError()
        for gap in self.gaps:
            text(gap)
        if self.coverage == "complete" and self.gaps:
            raise SourceArchiveError("capture_incomplete")

    @property
    def logical_key(self) -> str:
        # Excerpts remain distinct copied resources, but share the original's
        # history/retention group. Restricting or forgetting either covers both.
        source_id = (
            self.details.sheet_excerpt.source_id
            if self.details is not None and self.details.sheet_excerpt is not None
            else self.source_id
        )
        return self._identity_key(source_id)

    def _identity_key(
        self, source_id: str, *, legacy_kind: str | None = None, resource_identity: bool = False
    ) -> str:
        if self.details is None:
            return legacy_source_key(source_id)
        identity: list[object] = [
            "source-v3",
            self.details.provider,
            self.details.principal,
            self.kind if legacy_kind is None else legacy_kind,
            source_id,
        ]
        if (self.details.identity_version == 2 or resource_identity) and legacy_kind is None:
            # New writes separate resource identity from its export representation.
            # Scope still belongs to the archive namespace; unknown identity stays
            # encounter-bound. Format 3 always retains the earlier kind-based key.
            identity = [
                "source-resource:1",
                self.details.provider,
                self.details.principal,
                source_id,
            ]
        if not self.identity_known:
            identity.extend((self.task_id, self.run_id, self.interaction_id, self.source_version))
        return digest(encode(identity))

    @property
    def retention_keys(self) -> tuple[str, ...]:
        """Pure exclusion closure; historical identities and permission grants stay separate.

        A known provider/principal/resource has the same alias set across kind,
        version and representation changes. Excerpt copies additionally carry
        their approved original's aliases; arbitrary copy chains are not inferred.
        Unknown resources remain encounter-bound. Archive scope is the namespace,
        not an alias: shared titles, payloads and representation IDs grant no
        cross-resource identity or permission.
        """
        keys: tuple[str, ...] = (self.logical_key, self._identity_key(self.source_id))
        if self.details is not None:
            source_ids: tuple[str, ...] = (self.source_id,)
            if self.details.sheet_excerpt is not None:
                source_ids += (self.details.sheet_excerpt.source_id,)
            # Format 3 used kind-dependent keys; format 4 separates resource
            # identity from representation. Old erasures may retain only either
            # encoding, so check both without rewriting their saved identities.
            keys += tuple(
                self._identity_key(source_id, resource_identity=True) for source_id in source_ids
            )
            keys += tuple(
                self._identity_key(source_id, legacy_kind=kind)
                for source_id in source_ids
                for kind in _LEGACY_IDENTITY_KINDS
            )
        return tuple(dict.fromkeys(keys))

    @property
    def artifact_key(self) -> tuple[str, ...]:
        key = (
            (self.source_version,)
            if self.details is None
            else (self.source_version, self.details.representation_id)
        )
        if self.details is not None and self.details.sheet_excerpt is not None:
            return (*key, "sheet_excerpt", self.source_id, self.details.sheet_excerpt.sheet_id)
        return key

    @property
    def encounter_key(self) -> tuple[str, ...]:
        return (
            self.task_id,
            self.run_id,
            self.interaction_id,
            *self.artifact_key,
            *((self.details.reader_id,) if self.details else ()),
        )


@dataclass(frozen=True, repr=False)
class SourceManifest:
    metadata: SourceMetadata
    payload_sha256: str
    payload_bytes: int
    reading_sha256: str
    sequence: int
    predecessor: str | None
    anchor_ids: tuple[str, ...] = ("u1",)
    chat_recipe_sha256: str | None = None
    format_version: int | None = None

    def __post_init__(self) -> None:
        if self.metadata.kind == "image" and self.format_version != 4:
            raise SourceArchiveError()
        if self.format_version is not None and (
            type(self.format_version) is not int or self.format_version not in (3, 4, 5)
        ):
            raise SourceArchiveError()
        if (self.format_version in (3, 4, 5)) != (self.metadata.details is not None):
            raise SourceArchiveError()
        if self.metadata.details is not None and (
            (self.format_version in (4, 5)) != (self.metadata.details.identity_version == 2)
            or (self.format_version == 5) != (self.metadata.details.sheet_excerpt is not None)
        ):
            raise SourceArchiveError()
        if self.format_version in (3, 4, 5) and self.chat_recipe_sha256 is not None:
            raise SourceArchiveError()
        sha256(self.payload_sha256)
        sha256(self.reading_sha256)
        if self.chat_recipe_sha256 is not None:
            sha256(self.chat_recipe_sha256)
            positive(self.payload_bytes, maximum=CHAT_BYTES)
            if self.metadata.kind != "chat":
                raise SourceArchiveError()
        positive(self.payload_bytes, maximum=64 * 1024 * 1024)
        positive(self.sequence)
        if self.predecessor is not None:
            sha256(self.predecessor)
        if (self.sequence == 1) != (self.predecessor is None):
            raise SourceArchiveError()
        if (
            type(self.anchor_ids) is not tuple
            or not 1 <= len(self.anchor_ids) <= MAX_UNITS
            or len(set(self.anchor_ids)) != len(self.anchor_ids)
            or any(
                not isinstance(anchor, str) or not _ANCHOR.fullmatch(anchor)
                for anchor in self.anchor_ids
            )
        ):
            raise SourceArchiveError()


def encode_manifest(manifest: SourceManifest) -> bytes:
    value = asdict(manifest)
    value.pop("format_version")
    if manifest.format_version in (3, 4, 5):
        if manifest.format_version != 5:
            value["metadata"]["details"].pop("sheet_excerpt")
        if manifest.format_version == 3:
            value["metadata"]["details"].pop("identity_version")
        return encode({"format": manifest.format_version, **value})
    value["metadata"].pop("details")
    if manifest.chat_recipe_sha256 is None:
        value.pop("chat_recipe_sha256")
    return encode({"format": 1 if manifest.chat_recipe_sha256 is None else 2, **value})


def decode_scope(value: object) -> SourceScope:
    obj = object_fields(value, {"store_id", "generation", "subject_ids", "namespace"})
    subjects = obj["subject_ids"]
    if not isinstance(subjects, list):
        raise SourceArchiveError("scope_denied")
    return SourceScope(
        identifier(obj["store_id"]),
        identifier(obj["generation"]),
        tuple(identifier(item) for item in subjects),
        text(obj["namespace"], maximum=128),
    )


def decode_metadata(value: object, *, legacy: bool = False) -> SourceMetadata:
    extended = not legacy and isinstance(value, dict) and "details" in value
    obj = object_fields(value, _METADATA_FIELDS | ({"details"} if extended else set()))
    gaps, known = obj["gaps"], obj["identity_known"]
    if not isinstance(gaps, list) or type(known) is not bool:
        raise SourceArchiveError()
    return SourceMetadata(
        scope=decode_scope(obj["scope"]),
        source_id=text(obj["source_id"]),
        source_version=text(obj["source_version"]),
        kind=text(obj["kind"]),
        capture_method=text(obj["capture_method"]),
        tool_id=text(obj["tool_id"]),
        task_id=identifier(obj["task_id"]),
        run_id=identifier(obj["run_id"]),
        interaction_id=identifier(obj["interaction_id"]),
        captured_at=text(obj["captured_at"]),
        observed_at=optional_text(obj["observed_at"]),
        origin=optional_text(obj["origin"]),
        authorship=text(obj["authorship"]),
        coverage=text(obj["coverage"]),
        gaps=tuple(text(item) for item in gaps),
        privacy=text(obj["privacy"]),
        identity_known=known,
        details=decode_details(obj["details"]) if extended and obj["details"] is not None else None,
    )


def decode_manifest(payload: bytes) -> SourceManifest:
    value = parse_json(payload)
    if (
        not isinstance(value, dict)
        or type(value.get("format")) is not int
        or value["format"] not in (1, 2, 3, 4, 5)
    ):
        raise SourceArchiveError()
    version = value["format"]
    metadata_value = value.get("metadata")
    details_value = metadata_value.get("details") if isinstance(metadata_value, dict) else None
    if isinstance(details_value, dict) and ((version == 5) != ("sheet_excerpt" in details_value)):
        raise SourceArchiveError()
    obj = object_fields(
        value, _MANIFEST_FIELDS | ({"chat_recipe_sha256"} if version in (2, 3, 4, 5) else set())
    )
    recipe = obj.get("chat_recipe_sha256")
    if (version == 2) != (recipe is not None):
        raise SourceArchiveError()
    anchors = obj["anchor_ids"]
    if not isinstance(anchors, list):
        raise SourceArchiveError()
    return SourceManifest(
        decode_metadata(obj["metadata"], legacy=version not in (3, 4, 5)),
        sha256(obj["payload_sha256"]),
        positive(obj["payload_bytes"]),
        sha256(obj["reading_sha256"]),
        positive(obj["sequence"]),
        None if obj["predecessor"] is None else sha256(obj["predecessor"]),
        tuple(text(anchor) for anchor in anchors),
        None if recipe is None else sha256(recipe),
        version if version in (3, 4, 5) else None,
    )


def encode_units(units: tuple[SourceUnit, ...]) -> bytes:
    if type(units) is not tuple or not 1 <= len(units) <= MAX_UNITS:
        raise SourceArchiveError("limit_exceeded")
    if len({unit.anchor_id for unit in units}) != len(units):
        raise SourceArchiveError()
    extended = any(unit.location is not None for unit in units)
    values = [asdict(unit) for unit in units]
    if not extended:
        for value in values:
            value.pop("location")
    payload = encode({"format": 2, "units": values} if extended else values)
    if len(payload) > READING_BYTES:
        raise SourceArchiveError("limit_exceeded")
    return payload


def decode_units(payload: bytes) -> tuple[SourceUnit, ...]:
    value = parse_json(payload, maximum_bytes=READING_BYTES)
    extended = isinstance(value, dict)
    if extended:
        wrapper = object_fields(value, {"format", "units"})
        if type(wrapper["format"]) is not int or wrapper["format"] != 2:
            raise SourceArchiveError()
        value = wrapper["units"]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_UNITS:
        raise SourceArchiveError()
    units: list[SourceUnit] = []
    for item in value:
        obj = object_fields(item, _UNIT_FIELDS | ({"location"} if extended else set()))
        links = obj["links"]
        if not isinstance(links, list):
            raise SourceArchiveError()
        units.append(
            SourceUnit(
                text(obj["anchor_id"]),
                text(obj["text"], maximum=128 * 1024, empty=True),
                positive(obj["position"]),
                text(obj["kind"]),
                optional_text(obj["source_id"]),
                optional_text(obj["speaker"]),
                optional_text(obj["source_time"]),
                optional_text(obj["reply_to"]),
                tuple(decode_link(link) for link in links),
                decode_location(obj["location"])
                if extended and obj["location"] is not None
                else None,
            )
        )
    result = tuple(units)
    encode_units(result)
    return result


@dataclass(frozen=True)
class CaptureReceipt:
    status: str
    ref: SourceRef
    original_bytes: int
    new_bytes: int
    coverage: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class SourceObservation:
    ref: SourceRef
    relationship: str


@dataclass(frozen=True, repr=False)
class SourceExcerpt:
    ref: SourceRef
    units: tuple[SourceUnit, ...]
    remaining_units: int
    later_versions: tuple[SourceRef, ...]
    remaining_versions: int
    coverage: str
    privacy: str
    history_head: str
    integrity: str = "verified"
    source_version: str = "unknown"
    captured_at: str = "unknown"
    capture_method: str = "unknown"
    gaps: tuple[str, ...] = ()
    previous_units: int = 0
    next_anchor: str | None = None
    authorship: str = "unknown"
    observed_at: str | None = None
    source_time: str | None = None
    provider_revision: str | None = None
    representation: str | None = None
    reader_id: str | None = None
    later_observations: tuple[SourceObservation, ...] = ()
    title: str | None = None
    sheet_excerpt: SheetExcerpt | None = None


@dataclass(frozen=True)
class ForgetReceipt:
    # None means target sharing cannot be established from damaged/erased
    # recipes while validated survivor bodies remain; not an erasure assurance.
    shared_content_retained: bool | None
    backup_copies_may_remain: bool = True


@dataclass(frozen=True, repr=False)
class SourceMatch:
    source_uri: str
    source_version: str
    captured_at: str
    kind: str
    coverage: str
    authorship: str
    title: str | None = None
    observed_at: str | None = None
    source_time: str | None = None
    representation: str | None = None


@dataclass(frozen=True)
class SourceDiscovery:
    sources: tuple[SourceMatch, ...]
    more_metadata: bool
    next_offset: int
    snapshot_sha256: str
    access_incomplete: bool = False
