"""Typed filesystem and publication service for legacy projections."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from . import source_files
from .legacy_projection_contracts import (
    JsonObject,
    JsonValue,
    ProjectionDependencies,
    ValidationResult,
    require_json_object,
)
from .source_contracts import SourceRef, identifier, parse_source_uri, sha256
from .source_presentation import (
    MAX_PREVIEW_BATCH_BYTES,
    MAX_PREVIEW_FILE_BYTES,
    MAX_PREVIEW_FILES,
    PRESENTATION_MARKER,
    RetainedSourceView,
)
from .source_preview_recovery import PreviewRecovery

_BINARY_PATH = re.compile(
    r"20 Evidence/source-([0-9a-f]{32})-([0-9a-f]{64})-([1-9a-f][0-9a-f]{0,6})-([0-9a-f]{64})\.png"
)


@dataclass(frozen=True)
class _ProjectionContext:
    root: Path
    root_id: str
    graph_digest: str
    force_digest: str
    canonical_store_schema_version: str
    projection_payload_schema_version: str
    manifest_path: Path


@dataclass(frozen=True)
class _PriorProjection:
    generated_files: list[str]
    stale_result: JsonObject | None
    content_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _PublicationPlan:
    notes: dict[str, str]
    desired_contents: dict[str, str]
    desired_hashes: dict[str, str]
    manifest_content: str
    binaries: dict[str, bytes]


@dataclass(frozen=True)
class _PublicationCounts:
    written: int
    unchanged: int
    deleted: int


class LegacyProjectionService:
    """Coordinate safe legacy projection publication and recovery."""

    def __init__(self, dependencies: ProjectionDependencies) -> None:
        self.dependencies = dependencies

    def assert_trusted_directory_chain(self, path: Path, label: str) -> None:
        resolved = path.resolve()
        for candidate in (resolved, *resolved.parents):
            candidate_stat = candidate.stat()
            if not stat.S_ISDIR(candidate_stat.st_mode):
                raise ValueError(f"{label} ancestry contains a non-directory: {candidate}")
            if not self.dependencies.owner_is_current_user(candidate, allow_root=True):
                raise ValueError(f"{label} ancestry is owned by another user: {candidate}")
            sticky_system_directory = bool(
                self.dependencies.uses_posix_permissions()
                and candidate_stat.st_uid == 0
                and self.dependencies.mode_has_any(candidate, stat.S_ISVTX)
            )
            trusted_windows_root = self.dependencies.is_trusted_windows_volume_root(candidate)
            if (
                self.dependencies.mode_has_any(candidate, 0o022)
                and not sticky_system_directory
                and not trusted_windows_root
            ):
                raise ValueError(f"{label} or its ancestry is group/world writable: {candidate}")

    def ensure_projection_target(self, vault: Path, root_name: str) -> Path:
        if (
            not root_name
            or root_name in {".", ".."}
            or Path(root_name).name != root_name
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}", root_name)
        ):
            raise ValueError("Projection root name must be one safe directory name")
        vault = vault.expanduser().resolve()
        if not vault.exists() or not vault.is_dir():
            raise ValueError(f"Obsidian vault directory does not exist: {vault}")
        if not self.dependencies.owner_is_current_user(vault):
            raise ValueError(f"Obsidian vault is not owned by the current user: {vault}")
        self.dependencies.assert_trusted_directory_chain(vault, "Obsidian vault")
        lexical_root = vault / root_name
        if self.dependencies.is_link_or_reparse_point(lexical_root):
            raise ValueError(f"Projection root cannot be a symlink: {lexical_root}")
        root = lexical_root.resolve()
        if root == vault or vault not in root.parents:
            raise ValueError("Projection root must be a child of the configured vault")
        if root.exists() and not root.is_dir():
            raise ValueError(f"Projection root exists but is not a directory: {root}")
        if root.exists() and not self.dependencies.owner_is_current_user(root):
            raise ValueError(f"Projection root is not owned by the current user: {root}")
        return root

    def safe_projection_path(self, root: Path, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise ValueError(f"Unsafe projection path: {relative}")
        current = root
        for part in relative_path.parts:
            current = current / part
            if self.dependencies.is_link_or_reparse_point(current):
                raise ValueError(f"Projection path contains a symlink: {relative}")
        resolved = current.resolve()
        if root not in resolved.parents:
            raise ValueError(f"Projection path escaped root: {relative}")
        return resolved

    def generated_projection_path(self, relative: str) -> bool:
        path = Path(relative)
        if relative == "00 Dashboard.md":
            return True
        if len(path.parts) != 2 or path.parts[0] not in self.dependencies.generated_projection_dirs:
            return False
        return path.suffix in {".md", ".base"} or _BINARY_PATH.fullmatch(relative) is not None

    def binary_path(self, manifest_key: str, root_id: str, payload: bytes, *, binding: str) -> str:
        """Authenticate exact prepared PNG bytes without changing the file itself.

        This is ownership, not image decoding or permission to export a source.
        Only the separately authorized preparation path may supply preview bytes.
        """
        if not root_id or len(manifest_key) < 32 or not re.fullmatch(r"[0-9a-f]{32}", binding):
            raise ValueError("Preview requires an exact source and configured root binding")
        if type(payload) is not bytes or not 8 <= len(payload) <= MAX_PREVIEW_FILE_BYTES:
            raise ValueError("Preview exceeds the file limit")
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Only prepared PNG previews are supported")
        stem = (
            f"20 Evidence/source-{binding}-{hashlib.sha256(payload).hexdigest()}-{len(payload):x}"
        )
        marker = self.dependencies.projection_file_marker(manifest_key, root_id, stem + ".png")
        token = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        return f"{stem}-{token}.png"

    def _binary_is_owned(self, path: Path, manifest_key: str, root_id: str, relative: str) -> bool:
        match = _BINARY_PATH.fullmatch(relative)
        if match is None:
            return False
        try:
            size = int(match[3], 16)
            if not 8 <= size <= MAX_PREVIEW_FILE_BYTES:
                return False
            payload = source_files.read_bytes(path, size)
            return len(payload) == size and hmac.compare_digest(
                relative, self.binary_path(manifest_key, root_id, payload, binding=match[1])
            )
        except (OSError, ValueError):
            return False

    def _validated_binaries(
        self, binaries: Mapping[str, bytes] | None, manifest_key: str, root_id: str
    ) -> dict[str, bytes]:
        prepared = dict(binaries or {})
        if len(prepared) > MAX_PREVIEW_FILES:
            raise ValueError("Preview batch exceeds the file limit")
        total = 0
        for relative, payload in prepared.items():
            match = _BINARY_PATH.fullmatch(relative)
            if match is None or relative != self.binary_path(
                manifest_key, root_id, payload, binding=match[1]
            ):
                raise ValueError("Preview path does not match its root-bound content identity")
            total += len(payload)
        if total > MAX_PREVIEW_BATCH_BYTES:
            raise ValueError("Preview batch exceeds the byte limit")
        return prepared

    def projection_manifest_signature(
        self,
        manifest_key: str,
        root_id: str,
        mode: str,
        generated_files: list[str],
        *,
        schema_version: str,
        graph_watermark: int | None = None,
        graph_sha256: str | None = None,
        content_hashes: dict[str, str] | None = None,
        canonical_store_schema_version: str | None = None,
        projection_payload_schema_version: str | None = None,
    ) -> str:
        payload_data: JsonObject = {
            "schema_version": schema_version,
            "root_id": root_id,
            "mode": mode,
            "generated_files": sorted(generated_files),
        }
        if schema_version != "v2":
            payload_data.update(
                {
                    "graph_schema_version": (
                        self.dependencies.graph_schema_version
                        if schema_version == "v3"
                        else canonical_store_schema_version
                        or self.dependencies.graph_schema_version
                    ),
                    "graph_watermark": graph_watermark,
                    "graph_sha256": graph_sha256,
                    "content_hashes": {
                        key: (content_hashes or {})[key] for key in sorted(content_hashes or {})
                    },
                }
            )
            if schema_version != "v3":
                payload_data.update(
                    {
                        "canonical_store_schema_version": (
                            canonical_store_schema_version or self.dependencies.graph_schema_version
                        ),
                        "projection_payload_schema_version": (
                            projection_payload_schema_version
                            or self.dependencies.graph_schema_version
                        ),
                    }
                )
        payload = json.dumps(
            payload_data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(manifest_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def projection_file_marker(self, manifest_key: str, root_id: str, relative: str) -> str:
        payload = json.dumps(
            {
                "schema_version": self.dependencies.projection_marker_version,
                "root_id": root_id,
                "path": relative,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(manifest_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if Path(relative).suffix == ".base":
            return f"x-codex-generated: {self.dependencies.projection_marker_version}:{signature}"
        return f"<!-- codex-generated:{self.dependencies.projection_marker_version}:{signature} -->"

    def marked_projection_content(
        self, manifest_key: str, root_id: str, relative: str, content: str
    ) -> str:
        marker = self.dependencies.projection_file_marker(manifest_key, root_id, relative)
        if Path(relative).suffix == ".base":
            # Obsidian's Bases serializer places unknown top-level properties first.
            # Emitting the ownership marker there, together with its scalar style and
            # property references below, makes an open/save cycle byte-stable.
            return marker + "\n" + content.rstrip() + "\n"
        body = content.rstrip()
        if PRESENTATION_MARKER in body:
            # Removing the content trailer must not downgrade a source-presented
            # claim to ordinary legacy ownership during pre-manifest recovery.
            marker = self.dependencies.projection_file_marker(
                manifest_key, root_id, relative + ":source-content"
            )
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            bound_marker = self.dependencies.projection_file_marker(
                manifest_key, root_id, relative + ":" + fingerprint
            )
            token = hashlib.sha256(bound_marker.encode("utf-8")).hexdigest()
            body += f"\n\n<!-- tedd-source-content:1:{fingerprint}:{token} -->"
        return body + "\n\n" + marker + "\n"

    def _source_text_is_owned(
        self, path: Path, manifest_key: str, root_id: str, relative: str
    ) -> bool:
        try:
            payload = source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES)
            return self._source_text_body(payload, manifest_key, root_id, relative) is not None
        except (OSError, ValueError):
            return False

    def _source_text_body(
        self, payload: bytes, manifest_key: str, root_id: str, relative: str
    ) -> str | None:
        body, separator, _ = payload.decode("utf-8").rpartition("\n\n<!-- tedd-source-content:1:")
        if (
            separator
            and PRESENTATION_MARKER in body
            and hmac.compare_digest(
                payload,
                self.marked_projection_content(manifest_key, root_id, relative, body).encode(
                    "utf-8"
                ),
            )
        ):
            return body
        return None

    def _source_preview_paths(self, vault: Path, root_name: str) -> Iterator[tuple[str, Path]]:
        """Bounded existing publication inventory, including interrupted first writes."""
        root = self.ensure_projection_target(vault, root_name)
        if not root.exists():
            return
        self.assert_trusted_directory_chain(root, "Projection root")
        for folder in self.dependencies.generated_projection_dirs:
            directory = root / folder
            if self.dependencies.is_link_or_reparse_point(directory):
                raise ValueError("Projection directory is a link")
            if not directory.exists():
                continue
            self.assert_trusted_directory_chain(directory, "Projection directory")
            for count, path in enumerate(directory.iterdir(), 1):
                if count > self.dependencies.max_projection_dir_entries:
                    raise ValueError("Projection directory exceeds the entry limit")
                relative = path.relative_to(root).as_posix()
                if self.generated_projection_path(relative):
                    yield relative, self.safe_projection_path(root, relative)

    def source_preview_memories(
        self, vault: Path, root_name: str, manifest_key: str, root_id: str
    ) -> tuple[tuple[str, ...], bool, tuple[SourceRef, ...]]:
        """Read only authenticated opaque links, not an authority or another index."""
        return self.source_preview_inventory(vault, root_name, manifest_key, root_id)[0]

    def source_preview_inventory(
        self, vault: Path, root_name: str, manifest_key: str, root_id: str
    ) -> tuple[
        tuple[tuple[str, ...], bool, tuple[SourceRef, ...]],
        tuple[RetainedSourceView, ...],
        dict[str, bytes],
    ]:
        """Recover bounded copies through the existing root/content authentication.

        Old notes without a history head still supply links, but require bounded
        fresh preparation. No archive/source body or persisted second index here.
        """
        memories: dict[str, None] = {}
        refs: list[SourceRef] = []
        retained: list[RetainedSourceView] = []
        binaries: dict[str, bytes] = {}
        count = 0
        images = False
        for relative, path in self._source_preview_paths(vault, root_name):
            if not re.fullmatch(r"20 Evidence/source-[0-9a-f]{32}\.md", relative):
                continue
            count += 1
            if count > MAX_PREVIEW_FILES:
                raise ValueError("Source preview inventory exceeds the limit")
            body = self._source_text_body(
                source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES),
                manifest_key,
                root_id,
                relative,
            )
            if body is None:
                raise ValueError("Edited source preview requires owner review")
            has_image = "## Saved page image" in body.splitlines()
            images |= has_image
            refs.append(self._source_preview_reference(body))
            lines = [
                line.removeprefix("- Linked memories: ")
                for line in body.splitlines()
                if line.startswith("- Linked memories: ")
            ]
            if len(lines) != 1:
                raise ValueError("Source preview has no exact memory inventory")
            memory_ids = tuple(identifier(value) for value in lines[0].split(","))
            memories.update(dict.fromkeys(memory_ids))
            if len(memories) > MAX_PREVIEW_FILES:
                raise ValueError("Source preview inventory exceeds the limit")
            heads = re.findall(r"(?m)^- History head: ([0-9a-f]{64})$", body)
            if not heads:
                continue
            titles = re.findall(r"(?m)^# (.*)$", body)
            if len(heads) != 1 or len(titles) != 1:
                raise ValueError("Source preview metadata is incomplete")
            ref = refs[-1]
            binding = hashlib.sha256(ref.as_uri().encode("utf-8")).hexdigest()[:32]
            if relative != f"20 Evidence/source-{binding}.md":
                raise ValueError("Source preview reference does not match its binding")
            image_path = None
            if has_image:
                embeds = re.findall(r"(?m)^!\[\[(.*)\]\]$", body)
                prefix = root_name + "/"
                if len(embeds) != 1 or not embeds[0].startswith(prefix):
                    raise ValueError("Source preview image has no exact inventory")
                image_path = embeds[0][len(prefix) :]
                match = _BINARY_PATH.fullmatch(image_path)
                if match is None or match[1] != binding:
                    raise ValueError("Source preview image binding is invalid")
                image = self.safe_projection_path(vault / root_name, image_path)
                binaries[image_path] = source_files.read_bytes(image, MAX_PREVIEW_FILE_BYTES)
                self._validated_binaries(binaries, manifest_key, root_id)
            retained.append(
                RetainedSourceView(
                    memory_ids,
                    ref,
                    sha256(heads[0]),
                    re.sub(r"\\([\\`*_\[\]<>#!|])", r"\1", titles[0]),
                    body,
                    image_path,
                )
            )
        return (tuple(memories), images, tuple(refs)), tuple(retained), binaries

    @staticmethod
    def _source_preview_reference(body: str) -> SourceRef:
        exact = [
            line.removeprefix("- Exact archived reference: ")
            for line in body.splitlines()
            if line.startswith("- Exact archived reference: ")
        ]
        if len(exact) != 1:
            raise ValueError("Source preview has no exact source inventory")
        # Undo only URI-valid Markdown escapes, then validate the whole identity.
        return parse_source_uri(re.sub(r"\\([#_])", r"\1", exact[0]))

    def source_preview_cleanup_refs(
        self, vault: Path, root_name: str, manifest_key: str, root_id: str
    ) -> tuple[SourceRef, ...]:
        """Recover cleanup identity from existing authenticated notes, never authority.

        Unlike refresh, cleanup skips decodable owner edits. Read/size/decoding
        failures propagate to the caller's post-denial incomplete-cleanup result.
        This inventory survives archive manifest erasure without a second index.
        """
        refs: list[SourceRef] = []
        for relative, path in self._source_preview_paths(vault, root_name):
            if not re.fullmatch(r"20 Evidence/source-[0-9a-f]{32}\.md", relative):
                continue
            body = self._source_text_body(
                source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES),
                manifest_key,
                root_id,
                relative,
            )
            if body is not None:
                refs.append(self._source_preview_reference(body))
            if len(refs) > MAX_PREVIEW_FILES:
                raise ValueError("Source preview inventory exceeds the limit")
        return tuple(refs)

    def clear_source_previews_locked(
        self,
        vault: Path,
        root_name: str,
        manifest_key: str,
        root_id: str,
        *,
        bindings: frozenset[str] | None = None,
        recovery: PreviewRecovery | None = None,
    ) -> dict[str, object]:
        """Remove only unchanged authenticated copies under the projection lock.

        Strip affected memory links too, without removing another source's link.
        The sole signed manifest deliberately retains its prior hashes until the
        next refresh; content-bound ownership authenticates these cleaned notes
        in the meantime, including crash recovery, without another inventory.
        Edited/unowned files are conservatively reported, never overwritten.
        """
        if bindings is not None and any(
            not re.fullmatch(r"[0-9a-f]{32}", value) for value in bindings
        ):
            raise ValueError("Invalid preview binding")
        deleted = preserved = failed = rewritten = unavailable = 0
        recovery_result = recovery.reconcile() if recovery is not None else {}
        blocked = recovery_result.get("recovery_status") == "required"
        if blocked:
            failed += 1
        mutation_directories: set[Path] = set()
        candidates = [] if blocked else list(self._source_preview_paths(vault, root_name))
        mutate = recovery.mutate if recovery is not None else source_files.mutate_owned_preview
        if len(candidates) > self.dependencies.max_projection_files:
            raise ValueError("Projection inventory exceeds the limit")
        for relative, path in candidates:
            try:
                initial = source_files.check_entry(path)
                payload = source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES)
                replacement: str | None = None
                if relative.endswith(".png"):
                    match = _BINARY_PATH.fullmatch(relative)
                    affected = match is not None and (bindings is None or match[1] in bindings)
                    owned = match is not None and relative == self.binary_path(
                        manifest_key, root_id, payload, binding=match[1]
                    )
                else:
                    body = self._source_text_body(payload, manifest_key, root_id, relative)
                    owned = body is not None
                    if relative.startswith("20 Evidence/source-"):
                        affected = body is not None and (
                            bindings is None
                            or any(
                                relative == f"20 Evidence/source-{binding}.md"
                                for binding in bindings
                            )
                        )
                    else:
                        replacement = (
                            self._without_source_links(body, bindings) if body is not None else None
                        )
                        affected = replacement is not None
                if not owned:
                    # Ordinary authenticated notes need no source cleanup.
                    if not self.projection_file_is_owned(path, manifest_key, root_id, relative):
                        preserved += 1
                    continue
                if affected:
                    if source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES) != payload:
                        preserved += 1
                        continue
                    rewritten_bytes = (
                        self.marked_projection_content(
                            manifest_key, root_id, relative, replacement
                        ).encode("utf-8")
                        if replacement is not None
                        else None
                    )
                    mutation_directories.add(path.parent)
                    if not mutate(path, (initial.st_dev, initial.st_ino), payload, rewritten_bytes):
                        preserved += 1
                    elif replacement is not None:
                        rewritten += 1
                    else:
                        deleted += 1
            except source_files.MutationGuardUnavailable:
                unavailable += 1
                failed += 1
            except (OSError, ValueError):
                failed += 1
        # A previous attempt may have removed the last file and then failed its
        # directory sync. Re-sync existing inventory directories on retry even
        # when there is no remaining file to mutate; no tombstone index needed.
        root = self.ensure_projection_target(vault, root_name)
        if root.exists():
            for folder in self.dependencies.generated_projection_dirs:
                directory = root / folder
                if directory not in mutation_directories and directory.exists():
                    try:
                        self.assert_trusted_directory_chain(directory, "Projection directory")
                        source_files.fsync_directory(directory)
                    except (OSError, ValueError):
                        failed += 1
        if recovery is not None:
            recovery_result = {**recovery_result, **recovery.report()}
            if recovery_result.get("recovery_failed"):
                recovery_result["recovery_status"] = "required"
        return {
            "deleted_files": deleted,
            "preserved_files": preserved,
            "failed_files": failed,
            "rewritten_files": rewritten,
            "cleanup_status": (
                "cleanup_incomplete"
                if failed or preserved or recovery_result.get("recovery_status") == "required"
                else "cleaned"
            ),
            "guarded_mutation_unavailable_files": unavailable,
            "distributed_copies_may_remain": True,
            "ordinary_refresh_required": bool(
                deleted
                or rewritten
                or failed
                or recovery_result.get("recovery_status") == "required"
            ),
            **recovery_result,
        }

    @staticmethod
    def _without_source_links(body: str, bindings: frozenset[str] | None) -> str | None:
        separator = f"\n\n{PRESENTATION_MARKER}\n\n## Saved sources\n\n"
        ordinary, found, links = body.rpartition(separator)
        if not found:
            return None
        retained = [
            line
            for line in links.splitlines()
            if line
            and bindings is not None
            and not any(
                f"source-{binding}|" in line or f"source-{binding}.md" in line
                for binding in bindings
            )
        ]
        if retained == [line for line in links.splitlines() if line]:
            return None
        # Keep content authentication on the cleaned graph note until refresh.
        return ordinary + (
            separator + "\n".join(retained) if retained else f"\n\n{PRESENTATION_MARKER}"
        )

    def projection_file_is_owned(
        self, path: Path, manifest_key: str, root_id: str, relative: str
    ) -> bool:
        if Path(relative).suffix == ".png":
            return self._binary_is_owned(path, manifest_key, root_id, relative)
        try:
            with path.open("rb") as handle:
                head = handle.read(self.dependencies.projection_marker_tail_bytes)
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(
                    max(0, size - self.dependencies.projection_marker_tail_bytes), os.SEEK_SET
                )
                tail = handle.read(self.dependencies.projection_marker_tail_bytes)
        except OSError:
            return False
        if b"<!-- tedd-source-content:" in tail or relative.startswith("20 Evidence/source-"):
            return self._source_text_is_owned(path, manifest_key, root_id, relative)
        lines = [line.strip() for line in (head + b"\n" + tail).splitlines() if line.strip()]
        if not lines:
            return False
        expected = self.dependencies.projection_file_marker(manifest_key, root_id, relative).encode(
            "utf-8"
        )
        accepted_markers = [expected]
        if Path(relative).suffix == ".base":
            marker_value = expected.decode("utf-8").split(": ", 1)[1]
            accepted_markers.append(f"# codex-generated:{marker_value}".encode("utf-8"))
        return any(
            hmac.compare_digest(line, marker) for line in lines for marker in accepted_markers
        )

    def load_projection_manifest(self, path: Path) -> JsonObject | None:
        if not path.exists():
            return None
        with path.open("rb") as handle:
            payload = handle.read(self.dependencies.max_manifest_bytes + 1)
        if len(payload) > self.dependencies.max_manifest_bytes:
            raise ValueError("Projection manifest exceeds the safety size limit")
        try:
            decoded: object = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid projection manifest: {error}") from error
        return require_json_object(decoded, "Projection manifest")

    def recoverable_unmanifested_projection(
        self, root: Path, manifest_key: str, root_id: str
    ) -> bool:
        partial_artifact = False
        entry_count = 0
        for entry in root.iterdir():
            entry_count += 1
            if entry_count > len(self.dependencies.generated_projection_dirs) + 2:
                return False
            if self.dependencies.is_link_or_reparse_point(entry):
                return False
            if entry.name == "00 Dashboard.md":
                relative = entry.name
                if not entry.is_file() or not self.dependencies.projection_file_is_owned(
                    entry, manifest_key, root_id, relative
                ):
                    return False
                partial_artifact = True
                continue
            if entry.name in self.dependencies.generated_projection_dirs:
                if not entry.is_dir():
                    return False
                partial_artifact = True
                child_count = 0
                for child in entry.iterdir():
                    child_count += 1
                    if (
                        child_count > self.dependencies.max_projection_dir_entries
                        or self.dependencies.is_link_or_reparse_point(child)
                    ):
                        return False
                    relative = child.relative_to(root).as_posix()
                    if (
                        not child.is_file()
                        or not self.dependencies.generated_projection_path(relative)
                        or not self.dependencies.projection_file_is_owned(
                            child, manifest_key, root_id, relative
                        )
                    ):
                        return False
                continue
            if entry.name == "40 Review Actions" and entry.is_dir():
                continue
            return False
        return partial_artifact

    @contextlib.contextmanager
    def projection_lock(
        self,
        vault: Path,
        root_name: str,
        timeout_seconds: float = 120.0,
    ) -> Iterator[None]:
        vault = vault.expanduser().resolve()
        lock_path = vault / f".{root_name}.codex-projection.lock"
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        descriptor = self.dependencies._open_private_lock_descriptor(
            lock_path, "Projection", deadline
        )
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            while True:
                try:
                    self.dependencies.try_file_lock(handle)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for Obsidian projection lock after {timeout_seconds:g}s: {lock_path}"
                        ) from None
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            try:
                yield
            finally:
                self.dependencies.unlock_file(handle)

    def _prepare_projection_context(
        self,
        graph: JsonObject,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None,
        graph_sha256: str | None,
        force_current_digest: str | None,
        canonical_store_schema_version: str | None,
        projection_payload_schema_version: str | None,
    ) -> _ProjectionContext:
        if mode not in {"review_safe", "operator_full"}:
            raise ValueError("Projection mode must be review_safe or operator_full")
        if len(manifest_key) < 32:
            raise ValueError("Projection manifest key is missing or invalid; run configure again")
        root = self.dependencies.ensure_projection_target(vault, root_name)
        effective_root_id = self.dependencies.normalize_space(
            root_id
        ) or self.dependencies.stable_id("projection_root", str(root))
        effective_graph_digest = graph_sha256 or self.dependencies.stable_hash(graph, 64)
        effective_projection_payload_schema_version = (
            self.dependencies.normalize_space(projection_payload_schema_version)
            or self.dependencies.normalize_space(graph.get("schema_version"))
            or self.dependencies.graph_schema_version
        )
        effective_canonical_store_schema_version = (
            self.dependencies.normalize_space(canonical_store_schema_version)
            or effective_projection_payload_schema_version
        )
        requested_force_digest = self.dependencies.normalize_space(force_current_digest)
        if requested_force_digest and not hmac.compare_digest(
            requested_force_digest, effective_graph_digest
        ):
            raise ValueError("--force-current-digest does not match the current projection payload")
        if (
            root.exists()
            and any(root.iterdir())
            and not (root / self.dependencies.projection_manifest).is_file()
            and not self.dependencies.recoverable_unmanifested_projection(
                root, manifest_key, effective_root_id
            )
        ):
            raise ValueError(
                "Projection root is nonempty and has no authenticated manifest or recoverable root-bound partial projection. "
                "Choose a new root or review and clear the intended generated root explicitly."
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return _ProjectionContext(
            root=root,
            root_id=effective_root_id,
            graph_digest=effective_graph_digest,
            force_digest=requested_force_digest,
            canonical_store_schema_version=effective_canonical_store_schema_version,
            projection_payload_schema_version=effective_projection_payload_schema_version,
            manifest_path=self.dependencies.safe_projection_path(
                root, self.dependencies.projection_manifest
            ),
        )

    def _authenticate_prior_manifest(
        self,
        graph: JsonObject,
        mode: str,
        manifest_key: str,
        context: _ProjectionContext,
    ) -> _PriorProjection:
        prior_manifest = self.dependencies.load_projection_manifest(context.manifest_path)
        if prior_manifest is None:
            return _PriorProjection([], None)
        prior_files_raw = prior_manifest.get("generated_files", [])
        if not isinstance(prior_files_raw, list) or not all(
            isinstance(item, str) for item in prior_files_raw
        ):
            raise ValueError("Projection manifest generated_files must be a string list")
        prior_files = cast(list[str], prior_files_raw)
        if len(prior_files) > self.dependencies.max_projection_files:
            raise ValueError("Projection manifest exceeds the generated-file limit")
        if len(set(prior_files)) != len(prior_files):
            raise ValueError("Projection manifest contains duplicate generated paths")
        if any(len(item) > self.dependencies.max_projection_path_length for item in prior_files):
            raise ValueError("Projection manifest contains an oversized path")
        prior_mode = prior_manifest.get("mode")
        if not isinstance(prior_mode, str):
            raise ValueError("Projection manifest mode must be a string")
        if prior_manifest.get("root_id") != context.root_id:
            raise ValueError(
                "Projection manifest belongs to a different configured root; no files were deleted"
            )
        prior_signature = prior_manifest.get("signature")
        prior_schema_version = (
            self.dependencies.normalize_space(prior_manifest.get("schema_version")) or "v2"
        )
        prior_content_hashes = prior_manifest.get("content_hashes", {})
        if not isinstance(prior_content_hashes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in prior_content_hashes.items()
        ):
            raise ValueError("Projection manifest content_hashes must be a string map")
        typed_prior_content_hashes = cast(dict[str, str], prior_content_hashes)
        prior_watermark_field = prior_manifest.get("graph_watermark")
        if prior_watermark_field is not None and not isinstance(prior_watermark_field, int):
            raise ValueError("Projection manifest graph_watermark must be an integer")
        prior_digest = prior_manifest.get("graph_sha256")
        if prior_digest is not None and not isinstance(prior_digest, str):
            raise ValueError("Projection manifest graph_sha256 must be a string")
        prior_store_schema = prior_manifest.get("canonical_store_schema_version")
        if prior_store_schema is not None and not isinstance(prior_store_schema, str):
            raise ValueError("Projection manifest canonical_store_schema_version must be a string")
        prior_projection_schema = prior_manifest.get("projection_payload_schema_version")
        if prior_projection_schema is not None and not isinstance(prior_projection_schema, str):
            raise ValueError(
                "Projection manifest projection_payload_schema_version must be a string"
            )
        expected_signature = self.dependencies.projection_manifest_signature(
            manifest_key,
            context.root_id,
            prior_mode,
            prior_files,
            schema_version=prior_schema_version,
            graph_watermark=prior_watermark_field,
            graph_sha256=prior_digest,
            content_hashes=typed_prior_content_hashes,
            canonical_store_schema_version=prior_store_schema,
            projection_payload_schema_version=prior_projection_schema,
        )
        if not isinstance(prior_signature, str) or not hmac.compare_digest(
            prior_signature, expected_signature
        ):
            raise ValueError(
                "Projection manifest authentication failed; no files were deleted. "
                "Inspect the projection root before rebuilding."
            )
        prior_watermark = prior_manifest.get("graph_watermark", -1)
        if isinstance(
            prior_watermark, int
        ) and prior_watermark > self.dependencies.committed_watermark(graph):
            if not context.force_digest:
                return _PriorProjection(
                    prior_files,
                    {
                        "root": str(context.root),
                        "mode": mode,
                        "status": "stale_projection_skipped",
                        "graph_watermark": self.dependencies.committed_watermark(graph),
                        "projected_watermark": prior_watermark,
                        "projection_digest": context.graph_digest,
                        "generated_files": len(prior_files),
                        "written_files": 0,
                        "deleted_files": 0,
                        "recovery_hint": (
                            "After reviewing an intentional canonical restore, rerun project "
                            "with --force-current-digest equal to this result's projection_digest"
                        ),
                    },
                )
        return _PriorProjection(prior_files, None, typed_prior_content_hashes)

    def _prepare_projection_notes(
        self,
        graph: JsonObject,
        root_name: str,
        mode: str,
        root: Path,
    ) -> dict[str, str]:
        notes = self.dependencies.build_projection_notes(
            graph, mode, root_name, skip_validation=True
        )
        self.dependencies.set_private_path_mode(root, 0o700)
        if self.dependencies.mode_has_any(root, 0o077):
            raise ValueError(f"Projection root permissions are not private: {root}")
        if len(notes) > self.dependencies.max_projection_files:
            raise ValueError("Projection exceeds the generated-file safety limit")
        return notes

    def _scan_owned_files(
        self,
        root: Path,
        prior_files: list[str],
        manifest_key: str,
        root_id: str,
    ) -> set[str]:
        owned_files: set[str] = set()
        scan_paths = [
            root / "00 Dashboard.md",
            *(root / folder for folder in self.dependencies.generated_projection_dirs),
        ]
        for scan_path in scan_paths:
            if self.dependencies.is_link_or_reparse_point(scan_path):
                raise ValueError(f"Projection path contains a symlink: {scan_path}")
            if scan_path.is_dir():
                self.dependencies.set_private_path_mode(scan_path, 0o700)
                if self.dependencies.mode_has_any(scan_path, 0o077):
                    raise ValueError(
                        f"Projection directory permissions are not private: {scan_path}"
                    )
            if (
                scan_path == root / "00 Dashboard.md"
                and scan_path.exists()
                and not scan_path.is_file()
            ):
                raise ValueError("Dashboard projection path is not a regular file")
            candidates: Iterable[Path] = (
                [scan_path]
                if scan_path.is_file()
                else (scan_path.iterdir() if scan_path.is_dir() else ())
            )
            for entry_count, candidate in enumerate(candidates, start=1):
                if entry_count > self.dependencies.max_projection_dir_entries:
                    raise ValueError(f"Projection directory exceeds the entry limit: {scan_path}")
                if self.dependencies.is_link_or_reparse_point(candidate):
                    raise ValueError(f"Projection path contains a symlink: {candidate}")
                if not candidate.is_file():
                    raise ValueError(
                        f"Managed projection directory contains an unowned entry: {candidate}"
                    )
                relative = candidate.relative_to(root).as_posix()
                if not self.dependencies.generated_projection_path(relative):
                    raise ValueError(
                        f"Managed projection directory contains an unexpected file: {relative}"
                    )
                if not self.dependencies.projection_file_is_owned(
                    candidate, manifest_key, root_id, relative
                ):
                    raise ValueError(
                        f"Managed projection directory contains an unowned file; move it to 40 Review Actions: {relative}"
                    )
                owned_files.add(relative)
        for relative in prior_files:
            if not self.dependencies.generated_projection_path(relative):
                raise ValueError(f"Manifest contains a non-generated path: {relative}")
            candidate = self.dependencies.safe_projection_path(root, str(relative))
            if candidate.is_file():
                if not self.dependencies.projection_file_is_owned(
                    candidate, manifest_key, root_id, relative
                ):
                    raise ValueError(
                        f"Manifest-owned projection file was modified or lost its ownership marker: {relative}"
                    )
                owned_files.add(relative)
        return owned_files

    def _build_publication_plan(
        self,
        graph: JsonObject,
        mode: str,
        manifest_key: str,
        context: _ProjectionContext,
        notes: dict[str, str],
        binaries: dict[str, bytes],
    ) -> _PublicationPlan:
        desired_contents = {
            relative: self.dependencies.marked_projection_content(
                manifest_key, context.root_id, relative, content
            )
            for relative, content in notes.items()
        }
        desired_hashes = {
            relative: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for relative, content in desired_contents.items()
        }
        desired_hashes.update(
            {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in binaries.items()
            }
        )
        generated_files = sorted(desired_hashes)
        if len(generated_files) > self.dependencies.max_projection_files:
            raise ValueError("Projection exceeds the generated-file safety limit")
        manifest_watermark = self.dependencies.committed_watermark(graph)
        manifest: dict[str, object] = {
            "schema_version": self.dependencies.projection_format_version,
            "generated_at": self.dependencies.utc_now(),
            "root_id": context.root_id,
            "mode": mode,
            "graph_schema_version": context.canonical_store_schema_version,
            "canonical_store_schema_version": context.canonical_store_schema_version,
            "projection_payload_schema_version": context.projection_payload_schema_version,
            "graph_watermark": manifest_watermark,
            "graph_sha256": context.graph_digest,
            "generated_files": generated_files,
            "content_hashes": desired_hashes,
        }
        manifest["signature"] = self.dependencies.projection_manifest_signature(
            manifest_key,
            context.root_id,
            mode,
            generated_files,
            schema_version=self.dependencies.projection_format_version,
            graph_watermark=manifest_watermark,
            graph_sha256=context.graph_digest,
            content_hashes=desired_hashes,
            canonical_store_schema_version=context.canonical_store_schema_version,
            projection_payload_schema_version=context.projection_payload_schema_version,
        )
        manifest_content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if len(manifest_content.encode("utf-8")) > self.dependencies.max_manifest_bytes:
            raise ValueError(
                f"Projection manifest would exceed the {self.dependencies.max_manifest_bytes}-byte safety limit"
            )
        return _PublicationPlan(notes, desired_contents, desired_hashes, manifest_content, binaries)

    def _publish_projection(
        self,
        context: _ProjectionContext,
        owned_files: set[str],
        plan: _PublicationPlan,
        manifest_key: str,
        guarded_hashes: Mapping[str, tuple[tuple[int, int], str]],
        recovery: PreviewRecovery | None = None,
    ) -> _PublicationCounts:
        source_affected = (
            bool(guarded_hashes)
            or any(relative.startswith("20 Evidence/source-") for relative in plan.desired_hashes)
            or any(PRESENTATION_MARKER in body for body in plan.notes.values())
        )
        mutate = recovery.mutate if recovery is not None else source_files.mutate_owned_preview

        def check_unchanged(destination: Path, relative: str) -> bytes | None:
            if relative in owned_files and not self.dependencies.projection_file_is_owned(
                destination, manifest_key, context.root_id, relative
            ):
                raise ValueError(
                    "Projection ownership changed before publication; no replacement was changed"
                )
            expected = guarded_hashes.get(relative)
            if expected is not None:
                payload = source_files.read_bytes(destination, MAX_PREVIEW_FILE_BYTES)
                if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected[1]):
                    raise ValueError(
                        "Source projection changed before publication; owner edits were kept"
                    )
                return payload
            return None

        deleted_files = 0
        for relative in sorted(owned_files - set(plan.desired_hashes)):
            destination = self.dependencies.safe_projection_path(context.root, relative)
            payload = check_unchanged(destination, relative)
            if Path(relative).suffix == ".png" and not self._binary_is_owned(
                destination, manifest_key, context.root_id, relative
            ):
                raise ValueError("Preview copy changed before cleanup; no replacement was deleted")
            if source_affected:
                identity, _ = guarded_hashes[relative]
                assert payload is not None
                if not mutate(destination, identity, payload, None):
                    raise ValueError("Source projection changed; inspect cleanup/recovery result")
            else:
                destination.unlink()
            deleted_files += 1
        written_files = 0
        unchanged_files = 0
        for relative, _content in sorted(plan.notes.items()):
            destination = self.dependencies.safe_projection_path(context.root, relative)
            payload = check_unchanged(destination, relative)
            if destination.exists() and relative not in owned_files:
                raise ValueError(
                    f"Refusing to overwrite an unowned file in the generated projection area: {relative}"
                )
            desired_content = plan.desired_contents[relative]
            if destination.is_file():
                actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                if hmac.compare_digest(actual_hash, plan.desired_hashes[relative]):
                    unchanged_files += 1
                    continue
            if source_affected:
                if relative in guarded_hashes:
                    identity, _ = guarded_hashes[relative]
                    assert payload is not None
                    if not mutate(destination, identity, payload, desired_content.encode("utf-8")):
                        raise ValueError(
                            "Source projection changed; inspect cleanup/recovery result"
                        )
                else:
                    source_files.directory(destination.parent)
                    source_files.write_bytes(destination, desired_content.encode("utf-8"))
            else:
                self.dependencies.atomic_write(destination, desired_content, mode=0o600)
            written_files += 1
            if self.dependencies.mode_has_any(destination, 0o077):
                raise ValueError(f"Projection file permissions are not private: {destination}")
        for relative, payload in sorted(plan.binaries.items()):
            destination = self.dependencies.safe_projection_path(context.root, relative)
            if destination.exists():
                if not self._binary_is_owned(destination, manifest_key, context.root_id, relative):
                    raise ValueError("Refusing to replace an unowned or edited preview copy")
                unchanged_files += 1
                continue
            source_files.directory(destination.parent)
            source_files.write_bytes(destination, payload)
            written_files += 1
        guide_path = self.dependencies.safe_projection_path(
            context.root, "40 Review Actions/README.md"
        )
        if not guide_path.exists():
            guide = "# Review Actions\n\nThis folder is human-authored and preserved across rebuilds. Notes here are never executed automatically. Ask Codex to inspect a proposed correction, verify its evidence, and apply an explicit canonical mutation.\n"
            if source_affected:
                source_files.directory(guide_path.parent)
                source_files.write_bytes(guide_path, guide.encode("utf-8"))
            else:
                self.dependencies.atomic_write(guide_path, guide, mode=0o600)
        if self.dependencies.mode_has_any(guide_path, 0o077):
            raise ValueError(f"Review guide permissions are not private: {guide_path}")
        self.dependencies.atomic_write(context.manifest_path, plan.manifest_content, mode=0o600)
        if self.dependencies.mode_has_any(context.manifest_path, 0o077):
            raise ValueError(
                f"Projection manifest permissions are not private: {context.manifest_path}"
            )
        return _PublicationCounts(written_files, unchanged_files, deleted_files)

    def _verify_source_publication(
        self,
        context: _ProjectionContext,
        prior: _PriorProjection,
        owned: set[str],
        plan: _PublicationPlan,
        manifest_key: str,
    ) -> dict[str, tuple[tuple[int, int], str]]:
        """Preserve edits during preview refresh/cleanup, including removed markers.

        The existing manifest is the inventory. Content-bound text markers prove
        an interrupted new publication; ordinary legacy files need the prior or
        desired digest. Unconfigured legacy projections keep their old bytes.
        """
        if (
            not any(
                path.startswith("20 Evidence/source-")
                for path in (*prior.generated_files, *plan.desired_hashes, *owned)
            )
            and not any(
                self._source_text_is_owned(
                    self.dependencies.safe_projection_path(context.root, relative),
                    manifest_key,
                    context.root_id,
                    relative,
                )
                for relative in owned
                if Path(relative).suffix == ".md"
            )
            and not any(PRESENTATION_MARKER in body for body in plan.notes.values())
        ):
            return {}
        verified: dict[str, tuple[tuple[int, int], str]] = {}
        for relative in owned:
            path = self.dependencies.safe_projection_path(context.root, relative)
            initial = source_files.check_entry(path)
            payload = source_files.read_bytes(path, MAX_PREVIEW_FILE_BYTES)
            actual = hashlib.sha256(payload).hexdigest()
            verified[relative] = ((initial.st_dev, initial.st_ino), actual)
            if actual in (prior.content_hashes.get(relative), plan.desired_hashes.get(relative)):
                continue
            if Path(relative).suffix == ".png" and self._binary_is_owned(
                path, manifest_key, context.root_id, relative
            ):
                continue
            if self._source_text_is_owned(path, manifest_key, context.root_id, relative):
                continue
            raise ValueError(
                "Source projection contains an edited file; no preview files were changed"
            )
        return verified

    def _project_graph_locked(
        self,
        graph: JsonObject,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None = None,
        graph_sha256: str | None = None,
        force_current_digest: str | None = None,
        canonical_store_schema_version: str | None = None,
        projection_payload_schema_version: str | None = None,
        *,
        binaries: Mapping[str, bytes] | None = None,
        recovery: PreviewRecovery | None = None,
    ) -> JsonObject:
        prepared = self._validated_binaries(binaries, manifest_key, root_id or "")
        context = self._prepare_projection_context(
            graph,
            vault,
            root_name,
            mode,
            manifest_key,
            root_id,
            graph_sha256,
            force_current_digest,
            canonical_store_schema_version,
            projection_payload_schema_version,
        )
        prior = self._authenticate_prior_manifest(graph, mode, manifest_key, context)
        if prior.stale_result is not None:
            return prior.stale_result
        notes = self._prepare_projection_notes(graph, root_name, mode, context.root)
        owned_files = self._scan_owned_files(
            context.root,
            prior.generated_files,
            manifest_key,
            context.root_id,
        )
        plan = self._build_publication_plan(graph, mode, manifest_key, context, notes, prepared)
        guarded = self._verify_source_publication(context, prior, owned_files, plan, manifest_key)
        counts = self._publish_projection(
            context, owned_files, plan, manifest_key, guarded, recovery
        )
        return {
            "root": str(context.root),
            "mode": mode,
            "status": "projected",
            "graph_watermark": self.dependencies.committed_watermark(graph),
            "generated_files": len(plan.desired_hashes),
            "written_files": counts.written,
            "unchanged_files": counts.unchanged,
            "deleted_files": counts.deleted,
            "canonical_store_schema_version": context.canonical_store_schema_version,
            "projection_payload_schema_version": context.projection_payload_schema_version,
        }

    def project_graph(
        self,
        graph: JsonObject,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None = None,
        graph_sha256: str | None = None,
        timeout_seconds: float = 120.0,
        force_current_digest: str | None = None,
        canonical_store_schema_version: str | None = None,
        projection_payload_schema_version: str | None = None,
    ) -> JsonObject:
        validation = self.dependencies.validate_graph(graph)
        if not validation["valid"]:
            raise ValueError(
                "Refusing to project an invalid graph: " + "; ".join(validation["errors"])
            )
        self.dependencies.ensure_projection_target(vault, root_name)
        with self.dependencies.projection_lock(vault, root_name, timeout_seconds):
            return self.dependencies._project_graph_locked(
                graph,
                vault,
                root_name,
                mode,
                manifest_key,
                root_id,
                graph_sha256,
                force_current_digest,
                canonical_store_schema_version,
                projection_payload_schema_version,
            )

    def project_latest_from_store(
        self,
        store_path: Path,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None = None,
        timeout_seconds: float = 120.0,
        force_current_digest: str | None = None,
    ) -> JsonObject:
        """Project the newest atomic canonical snapshot without holding the writer lock."""
        self.dependencies.ensure_projection_target(vault, root_name)
        with self.dependencies.projection_lock(vault, root_name, timeout_seconds):
            graph, canonical_digest, _ = self.dependencies.load_graph_with_digest(store_path)
            validation = self.dependencies.validate_graph(graph)
            if not validation["valid"]:
                raise ValueError(
                    "Refusing to project an invalid graph: " + "; ".join(validation["errors"])
                )
            return self.dependencies._project_graph_locked(
                graph,
                vault,
                root_name,
                mode,
                manifest_key,
                root_id,
                canonical_digest,
                force_current_digest,
            )


__all__ = [
    "JsonObject",
    "JsonValue",
    "LegacyProjectionService",
    "ProjectionDependencies",
    "ValidationResult",
]
