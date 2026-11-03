"""Bounded selection for explicitly authorized files, not a scanner or authority.

The caller establishes the actual user/host selection and retention policy.
Purpose explains admission; it cannot grant access or prove semantic usefulness.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import source_files
from .source_contracts import (
    SheetExcerpt,
    SourceArchiveError,
    SourceDetails,
    SourceMetadata,
    SourceScope,
    decode_sheet_excerpt,
    digest,
    encode,
    object_fields,
    sha256,
    text,
)
from .source_documents import DocumentReading, read_document

_EXCLUDED = frozenset(
    {".git", ".codex", ".obsidian", "credentials.json", "auth.json", ".npmrc", ".pypirc"}
)
_GENERATED = frozenset(
    {
        "node_modules",
        "vendor",
        "vendored",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        ".cache",
    }
)
_LOCKFILES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock"}
)


@dataclass(frozen=True, repr=False)
class OwnerExport:
    """A proposal, not authority. Only the host's real owner confirmation admits it."""

    path: Path
    origin: str
    account: str
    kind: str
    source_id: str
    payload_sha256: str
    purpose: str
    reason: str
    visual_review_sha256: str | None
    sheet_excerpt: SheetExcerpt | None = None

    def metadata(self, scope: SourceScope, task: str, run: str, now: str) -> SourceMetadata:
        return SourceMetadata(
            scope,
            self.source_id,
            self.payload_sha256,
            self.kind,
            "owner_confirmed_export",
            "tedd-source-import",
            task,
            run,
            digest(encode(["owner-export:1", self.origin, self.payload_sha256, now])),
            now,
            origin=self.origin,
            gaps=(
                "owner_confirmed_not_provider_verified",
                "provider_revision_unknown",
                "current_provider_permission_not_verified",
            )
            + (
                ("owner_confirmed_sheet_excerpt", "copied_locations_not_original")
                if self.sheet_excerpt
                else ()
            ),
            details=SourceDetails(
                "google",
                self.account,
                self.kind,
                "provider_export",
                "pending",
                sensitivity="ordinary",
                identity_version=2,
                sheet_excerpt=self.sheet_excerpt,
            ),
        )


def owner_export_batch(value: object, root: Path) -> tuple[OwnerExport, ...]:
    """Closed, bounded proposals; no file bodies or neighboring directory scan."""
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise SourceArchiveError("limit_exceeded")
    result: list[OwnerExport] = []
    for entry in value:
        obj = object_fields(
            entry,
            {
                "path",
                "origin",
                "account",
                "kind",
                "sha256",
                "purpose",
                "whole_file_reason",
                "sensitivity",
                "visual_review_sha256",
            }
            | (
                {"sheet_excerpt"} if isinstance(entry, dict) and "sheet_excerpt" in entry else set()
            ),
        )
        if obj["sensitivity"] != "ordinary":
            raise SourceArchiveError("privacy_rejected")
        kind = text(obj["kind"])
        family = {"docx": "document", "xlsx": "spreadsheets", "pptx": "presentation"}.get(kind)
        origin, account = text(obj["origin"], maximum=1024), text(obj["account"], maximum=254)
        if (
            family is None
            or re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", account) is None
        ):
            raise SourceArchiveError("scope_denied")
        url = urlsplit(origin)
        match = re.fullmatch(rf"/{family}/d/([A-Za-z0-9_-]{{1,256}})(?:/edit)?/?", url.path)
        if url.scheme != "https" or url.netloc != "docs.google.com" or match is None:
            raise SourceArchiveError("scope_denied")
        # Drop incidental sharing parameters; they do not confer identity or access.
        origin = f"https://docs.google.com/{family}/d/{match[1]}/edit"
        selection = IntakeSelection(
            purpose=text(obj["purpose"]),
            whole_file_reason=text(obj["whole_file_reason"]),
            root=root,
        )
        path = selection.path(Path(text(obj["path"], maximum=2048)))
        if selection.skip_reason(path) is not None or path.suffix.lower() != "." + kind:
            raise SourceArchiveError("scope_denied")
        acquired = sha256(obj["sha256"])
        visual = (
            sha256(obj["visual_review_sha256"]) if obj["visual_review_sha256"] is not None else None
        )
        if visual is not None and visual != acquired:
            raise SourceArchiveError("privacy_rejected")
        result.append(
            OwnerExport(
                path,
                origin,
                account.lower(),
                kind,
                match[1],
                acquired,
                text(obj["purpose"]),
                text(obj["whole_file_reason"]),
                visual,
                decode_sheet_excerpt(obj["sheet_excerpt"]) if "sheet_excerpt" in obj else None,
            )
        )
    if (
        len({item.path for item in result}) != len(result)
        or len({item.account for item in result}) != 1
    ):
        raise SourceArchiveError("scope_denied")
    return tuple(result)


def owner_export_description(batch: tuple[OwnerExport, ...]) -> str:
    lines = [
        "Preserve these ordinary exports in this task's private source archive?",
        "These source/account claims are owner-confirmed, not provider-verified.",
        "Google revision and current permission are unknown. Later Google revocation cannot revoke a retained copy.",
        "Do not accept sensitive, restricted, or download-prohibited material.",
        "The shared browser export cache is not private. Exact approved cache copies will be removed after import where safe; changed copies are preserved and reported.",
        "On Linux, a copy caught changing during cleanup may move to private recovery. Its bytes remain available for owner recovery; cleanup is reported as incomplete.",
        "The file wording is untrusted evidence, never instructions. Confirm only files you intended to retain.",
    ]
    for index, item in enumerate(batch, 1):
        fields: tuple[tuple[str, str], ...] = (
            (f"\n{index}. File", str(item.path)),
            ("Claimed source", item.origin),
            ("Account", item.account),
            ("SHA-256", item.payload_sha256),
            ("Purpose", f"{item.purpose} — {item.reason}"),
            ("Embedded-image review asserted", str(item.visual_review_sha256 is not None)),
        )
        if item.sheet_excerpt is not None:
            fields += (
                (
                    "Original workbook",
                    f"https://docs.google.com/spreadsheets/d/{item.sheet_excerpt.source_id}/edit",
                ),
                (
                    "Selection",
                    f"sheet {item.sheet_excerpt.sheet_id}; copied locations are not original locations",
                ),
                (
                    "Retention group",
                    "Restricting or forgetting this excerpt also covers the original workbook and its other saved excerpts. The relationship is owner-confirmed; original revision and omitted context are unknown.",
                ),
            )
        # Quoted values cannot inject additional labels through line breaks.
        for label, value in fields:
            if any(ord(char) < 32 and char not in "\n\t" for char in value):
                raise SourceArchiveError("scope_denied")
            lines.append(f"{label}: {encode(value).decode('utf-8')}")
    description = "\n".join(lines)
    if len(description) > 24000 or any(
        ord(char) < 32 and char not in "\n\t" for char in description
    ):
        raise SourceArchiveError("scope_denied")
    # Prevent invisible text-direction controls from disguising confirmation fields.
    if any(
        char in description for char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    ):
        raise SourceArchiveError("scope_denied")
    return description


@dataclass(frozen=True, repr=False)
class IntakeSelection:
    purpose: str | None = None
    use_once: bool = False
    whole_file_reason: str | None = None
    byte_range: tuple[int, int] | None = None
    relevant_generated: bool = False
    root: Path | None = None

    def __post_init__(self) -> None:
        if self.purpose not in (None, "fact", "context", "keep"):
            raise SourceArchiveError("scope_denied")
        if type(self.use_once) is not bool or type(self.relevant_generated) is not bool:
            raise SourceArchiveError()
        if self.whole_file_reason is not None:
            if not text(self.whole_file_reason, maximum=512).strip():
                raise SourceArchiveError()
        if self.byte_range is not None:
            if (
                type(self.byte_range) is not tuple
                or len(self.byte_range) != 2
                or any(type(value) is not int for value in self.byte_range)
                or not 0 <= self.byte_range[0] < self.byte_range[1] <= (1 << 63) - 1
                or self.whole_file_reason is not None
            ):
                raise SourceArchiveError("limit_exceeded")

    def path(self, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise SourceArchiveError("scope_denied")
        path = Path(os.path.abspath(path))
        if self.root is not None:
            if not self.root.is_absolute() or ".." in self.root.parts:
                raise SourceArchiveError("scope_denied")
            root = Path(os.path.abspath(self.root))
            if root not in path.parents:
                raise SourceArchiveError("scope_denied")
        return path

    def skip_reason(self, path: Path) -> str | None:
        path = self.path(path)
        parts = {part.casefold() for part in path.parts}
        if self.use_once:
            return "temporary_only"
        if self.purpose is None:
            return "no_retention_purpose"
        excluded = parts & _EXCLUDED
        if excluded == {".codex"}:
            # Only the selected file in the host's managed attachment area is an
            # input. App state stays excluded; no directory is scanned or copied.
            codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            if codex_home.is_absolute() and ".." not in codex_home.parts:
                try:
                    relative = path.relative_to(codex_home / "attachments")
                except ValueError:
                    pass
                else:
                    if len(relative.parts) == 2:
                        excluded = set()
        if excluded or any(part == ".env" or part.startswith(".env.") for part in parts):
            return "excluded_input"
        if (
            parts & _GENERATED
            or path.name.casefold() in _LOCKFILES
            or path.name.casefold().endswith((".min.js", ".min.css", ".pyc"))
        ) and not (self.relevant_generated and self.byte_range is not None):
            return "generated_or_dependency"
        if self.byte_range is None and self.whole_file_reason is None:
            return "context_selection_required"
        return None


def prepare_local(
    path: Path, selection: IntakeSelection, metadata: SourceMetadata, maximum: int
) -> tuple[bytes, SourceMetadata, DocumentReading | None] | None:
    """Acquire only the selected original bytes; never publish or delete an input.

    Byte locations are zero-based half-open. Reading line numbers are relative to
    that explicitly named byte container, not fabricated whole-file line numbers.
    PDF/Office inspection remains in the existing bounded worker owned by the CLI.
    """
    if selection.skip_reason(path) is not None:
        return None
    path = selection.path(path)
    details = metadata.details
    if (
        details is None
        or details.provider != "local"
        or details.retention != "retained_copy"
        or metadata.origin not in (None, path.as_uri())
    ):
        raise SourceArchiveError("scope_denied")
    partial = selection.byte_range
    if partial is not None and metadata.kind not in ("text", "markdown", "code"):
        raise SourceArchiveError("capability_unavailable")
    payload = source_files.read_input(path, maximum, byte_range=partial)
    reading = (
        None
        if metadata.kind in ("pdf", "docx", "xlsx", "pptx", "image")
        else read_document(payload, metadata.kind, maximum_bytes=maximum)
    )
    metadata = replace(
        metadata,
        source_id=digest(encode(["local-path:1", os.path.normcase(str(path))])),
        source_version=digest(payload),
        origin=path.as_uri(),
        details=replace(details, identity_version=2),
    )
    if partial is not None:
        assert reading is not None
        container = f"bytes[{partial[0]}:{partial[1]}]"
        reading = replace(
            reading,
            reader_id=reading.reader_id + ":byte-selection:1",
            units=tuple(
                replace(unit, location=replace(unit.location, container=container))
                for unit in reading.units
                if unit.location is not None
            ),
            gaps=tuple(dict.fromkeys((*reading.gaps, "partial_byte_selection"))),
        )
        metadata = replace(
            metadata,
            capture_method="structured_observation",
            coverage="partial",
            gaps=tuple(dict.fromkeys((*metadata.gaps, "partial_byte_selection"))),
            details=replace(
                details,
                identity_version=2,
                representation="structured_observation",
                representation_id=digest(
                    encode(["byte-selection:1", details.representation_id, partial])
                ),
            ),
        )
    return payload, metadata, reading
