"""Explicit, private, immutable source storage. Never imported by ordinary recall.

One archive lock serializes publication, revocation and reads. Callers must finish
graph writes outside this lock; this module has no graph-writing capability.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from knowledge_graph_file_io import fsync_directory

from . import source_files as files
from .legacy_values import secret_hits
from .source_authority import SourceAccess
from .source_chat import RECIPE_BYTES, ChatRecipe, decode_chat, message_recipe
from .source_contracts import (
    MANIFEST_BYTES,
    OFFICE_IMAGE_READERS,
    READING_BYTES,
    ArchiveLimits,
    CaptureReceipt,
    ForgetReceipt,
    SourceArchiveError,
    SourceDiscovery,
    SourceExcerpt,
    SourceManifest,
    SourceMatch,
    SourceMetadata,
    SourceObservation,
    SourceRef,
    SourceScope,
    SourceUnit,
    decode_limits,
    decode_manifest,
    decode_scope,
    decode_units,
    digest,
    encode,
    encode_manifest,
    encode_units,
    legacy_source_key,
    object_fields,
    parse_json,
    positive,
    safe_display,
    sha256,
    text,
)

MAX_FILES = 100_000
MAX_HISTORY = 10_000
DEFAULT_QUOTA = 5 * 1024**3
# Capture counts existing denials against MAX_FILES. Maintenance may add an
# initial and expanded denial per admitted source/version even when capture is
# full. Repeated marker variants can exhaust this reserve; refuse before writing
# then, preserving existing denials and content. This is not additional capture
# space or a byte-quota increase. One extra file permits staging or an inert
# backup/restore marker.
MAINTENANCE_FILE_RESERVE = 2 * MAX_FILES
# Three lists of <= MAX_HISTORY hashes (67 JSON bytes each), <=500 opaque graph
# IDs (32 bytes each), and fixed keys/source identity. Individual manifests keep
# their independent 1 MiB limit.
DENIAL_BYTES = 3 * MAX_HISTORY * 67 + 500 * 32 + 512
# A closed relative layout path plus its digest uses <256 JSON bytes per entry.
BACKUP_INDEX_BYTES = 256 * (MAX_FILES + MAINTENANCE_FILE_RESERVE) + 32


def _bounded_encode(value: object, maximum: int) -> bytes:
    payload = encode(value)
    if len(payload) > maximum:
        raise SourceArchiveError("limit_exceeded")
    return payload


def _source_key(source: str | SourceMetadata) -> str:
    """Keep the archive identity seam while contracts own identity encoding."""
    return source.logical_key if isinstance(source, SourceMetadata) else legacy_source_key(source)


def _observation_relationship(previous: SourceManifest, current: SourceManifest) -> str:
    before, after = previous.metadata.details, current.metadata.details
    if before is None or after is None:
        return "later_capture"
    if before.provider_revision is not None and after.provider_revision is not None:
        if before.provider_revision != after.provider_revision:
            return "provider_revision_changed"
        if previous.payload_sha256 != current.payload_sha256:
            return "new_representation"
    if previous.payload_sha256 != current.payload_sha256:
        return "different_observed_bytes"
    if before.reader_id != after.reader_id or previous.reading_sha256 != current.reading_sha256:
        return "new_reading"
    if before.representation_id != after.representation_id:
        return "new_representation"
    return "observed_again"


def _hashes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY:
        raise SourceArchiveError()
    result = tuple(sha256(item) for item in value)
    if len(set(result)) != len(result):
        raise SourceArchiveError()
    return result


def _graph_ids(value: object) -> tuple[str, ...]:
    """Only generated opaque graph-source IDs belong in cleanup markers."""
    if not isinstance(value, (list, tuple)) or len(value) > 500:
        raise SourceArchiveError("limit_exceeded")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or re.fullmatch(r"source_[0-9a-f]{20}", item) is None:
            raise SourceArchiveError("scope_denied")
        result.append(item)
    if len(result) != len(set(result)):
        raise SourceArchiveError()
    return tuple(result)


@dataclass(frozen=True)
class _Denial:
    source: str
    manifests: tuple[str, ...]
    payloads: tuple[str, ...]
    readings: tuple[str, ...]
    graph_records: tuple[str, ...] = ()
    disposition: str = "erase"

    def encoded(self) -> bytes:
        _hashes(list(self.manifests))
        _hashes(list(self.payloads))
        _hashes(list(self.readings))
        _graph_ids(self.graph_records)
        if self.disposition not in ("erase", "retain"):
            raise SourceArchiveError()
        return _bounded_encode(
            {
                "source": self.source,
                "manifests": self.manifests,
                "payloads": self.payloads,
                "readings": self.readings,
                "graph_records": self.graph_records,
                **({"disposition": "retain"} if self.disposition == "retain" else {}),
            },
            DENIAL_BYTES,
        )

    @classmethod
    def decode(cls, payload: bytes) -> _Denial:
        value = parse_json(payload, maximum_bytes=DENIAL_BYTES)
        disposition = "erase"
        if isinstance(value, dict) and "disposition" in value:
            # Additive retained-history format; absent means legacy erasure.
            if value["disposition"] != "retain":
                raise SourceArchiveError()
            disposition = "retain"
            value = {key: item for key, item in value.items() if key != "disposition"}
        if isinstance(value, dict) and "graph_records" not in value:
            value = {**value, "graph_records": []}
        obj = object_fields(value, {"source", "manifests", "payloads", "readings", "graph_records"})
        return cls(
            sha256(obj["source"]),
            _hashes(obj["manifests"]),
            _hashes(obj["payloads"]),
            _hashes(obj["readings"]),
            _graph_ids(obj["graph_records"]),
            disposition,
        )


class Archive:
    """A scope-bound handle, not an authorization grant.

    Only the trusted, opted-in workflow may construct a writable handle. Metadata
    supplied by source content is not session authority. Opening defaults to read
    only and never repairs, initializes or updates an index.
    """

    def __init__(
        self, root: Path, scope: SourceScope, *, readonly: bool, access: SourceAccess | None = None
    ) -> None:
        self.root = root
        self.scope = scope
        self.readonly = readonly
        self._validate_access(scope, access)
        self.access = access
        marker = object_fields(
            self._marker(root),
            {"format", "archive_id", "store", "generation", "max_bytes", "limits"},
        )
        if type(marker["format"]) is not int or marker["format"] != 1:
            raise SourceArchiveError()
        self.archive_id = text(marker["archive_id"], maximum=32)
        # Reuse the URI's exact opaque archive-ID validation.
        SourceRef(self.archive_id, scope.key, "0" * 64, "u1")
        if marker["store"] != digest(encode(scope.store_id)) or (
            marker["generation"] != digest(encode(scope.generation))
        ):
            raise SourceArchiveError("scope_denied")
        self.max_bytes = positive(marker["max_bytes"])
        self.limits = decode_limits(marker["limits"])
        self.scoped = root / "scopes" / scope.key

    @staticmethod
    def _validate_access(scope: SourceScope, access: SourceAccess | None) -> None:
        if access is not None and (not isinstance(access, SourceAccess) or access.scope != scope):
            raise SourceArchiveError("scope_denied")

    @staticmethod
    def _marker(root: Path) -> dict[str, object]:
        value = parse_json(files.read_bytes(root / "archive.json", MANIFEST_BYTES))
        if isinstance(value, dict) and "limits" not in value:
            value = {**value, "limits": asdict(ArchiveLimits())}
        return object_fields(
            value, {"format", "archive_id", "store", "generation", "max_bytes", "limits"}
        )

    @classmethod
    def initialize(
        cls,
        root: Path,
        *,
        scope: SourceScope,
        max_bytes: int = DEFAULT_QUOTA,
        limits: ArchiveLimits | None = None,
        access: SourceAccess | None = None,
    ) -> Archive:
        positive(max_bytes)
        cls._validate_access(scope, access)
        if limits is None:
            limits = ArchiveLimits()
        if not isinstance(limits, ArchiveLimits):
            raise SourceArchiveError()
        try:
            root = files.private_root(root, create=True)
            files.write_bytes(root / "archive.lock", b"")
            files.directory(root / "scopes")
            files.write_bytes(
                root / "archive.json",
                encode(
                    {
                        "format": 1,
                        "archive_id": uuid.uuid4().hex,
                        "max_bytes": max_bytes,
                        "store": digest(encode(scope.store_id)),
                        "generation": digest(encode(scope.generation)),
                        "limits": asdict(limits),
                    }
                ),
            )
            result = cls(root, scope, readonly=False, access=access)
            with result._lock(write=True):
                result._ensure_scope()
            return result
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        scope: SourceScope,
        readonly: bool = True,
        access: SourceAccess | None = None,
    ) -> Archive:
        try:
            root = files.private_root(root)
            if (root / "backup.json").exists():
                raise SourceArchiveError("scope_denied")
            with files.locked(root, shared=True):
                return cls(root, scope, readonly=readonly, access=access)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    @contextmanager
    def _lock(self, *, write: bool = False, repair_scope: bool = False) -> Iterator[None]:
        if (write and self.readonly) or (repair_scope and not write):
            raise SourceArchiveError("scope_denied")
        try:
            with files.locked(self.root, shared=not write):
                # Detect replaced configuration before using a previously opened handle.
                current = Archive(self.root, self.scope, readonly=True)
                if current.archive_id != self.archive_id or current.max_bytes != self.max_bytes:
                    raise SourceArchiveError()
                if write and current.limits != self.limits:
                    raise SourceArchiveError("capture_incomplete")
                if self.scoped.exists():
                    for path in (self.root / "scopes", self.scoped):
                        files.check_entry(path, directory=True)
                    initializing = repair_scope and not (self.scoped / "scope.json").exists()
                    if initializing:
                        # The marker is published after every directory, before
                        # source objects. Only that empty, unfinished layout can
                        # be completed; lost committed state is not initialization.
                        for entry in self.scoped.iterdir():
                            if entry.name not in (
                                "blobs",
                                "readings",
                                "versions",
                                "heads",
                                "pending",
                                "denials",
                            ):
                                raise SourceArchiveError()
                            files.check_entry(entry, directory=True)
                            if next(entry.iterdir(), None) is not None:
                                raise SourceArchiveError()
                    elif repair_scope:
                        self._check_scope()
                    for name in ("blobs", "readings", "versions", "heads", "pending", "denials"):
                        if initializing and not (self.scoped / name).exists():
                            continue
                        files.check_entry(self.scoped / name, directory=True)
                yield
        except FileNotFoundError:
            raise SourceArchiveError("source_unavailable") from None
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    def configure_limits(self, limits: ArchiveLimits) -> None:
        """Explicit atomic acquisition-only update; stale writers must reopen."""
        if not isinstance(limits, ArchiveLimits):
            raise SourceArchiveError()
        with self._lock(write=True):
            if self.limits == limits:
                return
            marker = {**self._marker(self.root), "limits": asdict(limits)}
            self._publish(self.root / "archive.json", encode(marker), overwrite=True)
            self.limits = limits

    def _check_scope(self) -> None:
        marker = self.scoped / "scope.json"
        if decode_scope(parse_json(files.read_bytes(marker, MANIFEST_BYTES))).key != self.scope.key:
            raise SourceArchiveError("scope_denied")

    def _ensure_scope(self) -> None:
        files.directory(self.scoped)
        for name in ("blobs", "readings", "versions", "heads", "pending", "denials"):
            files.directory(self.scoped / name)
        marker = self.scoped / "scope.json"
        payload = encode(asdict(self.scope))
        if marker.exists():
            self._check_scope()
        else:
            files.write_bytes(marker, payload)

    def _scopes(self) -> tuple[Archive, ...]:
        result: list[Archive] = []
        for directory in (self.root / "scopes").iterdir():
            if len(result) >= MAX_HISTORY:
                raise SourceArchiveError("limit_exceeded")
            files.check_entry(directory, directory=True)
            scope = decode_scope(
                parse_json(files.read_bytes(directory / "scope.json", MANIFEST_BYTES))
            )
            if scope.key != directory.name:
                raise SourceArchiveError("scope_denied")
            result.append(Archive(self.root, scope, readonly=self.readonly))
        return tuple(result)

    @staticmethod
    def _valid_relative(path: Path) -> None:
        if path.as_posix() in ("archive.json", "archive.lock"):
            return
        parts = path.parts
        if len(parts) not in (3, 4) or parts[0] != "scopes":
            raise SourceArchiveError()
        sha256(parts[1])
        if len(parts) == 3 and parts[2] == "scope.json":
            return
        if len(parts) != 4 or parts[2] not in (
            "blobs",
            "readings",
            "versions",
            "heads",
            "pending",
            "denials",
        ):
            raise SourceArchiveError()
        sha256(parts[3])

    def _verified_backup_files(self) -> tuple[Path, ...]:
        paths = self._all_files()
        for path in paths:
            self._valid_relative(path.relative_to(self.root))
            if path.parent.name == "pending":
                raise SourceArchiveError("capture_incomplete")
        for archive in self._scopes():
            revoked = {
                key for d in archive._denials() if d.disposition == "erase" for key in d.manifests
            }
            for path in archive._entries("versions"):
                if path.name in revoked:
                    raise SourceArchiveError("capture_incomplete")
                manifest = archive._manifest(path.name)
                archive._objects(manifest)
                archive._history(manifest.metadata.logical_key)
        return paths

    def _entries(self, name: str) -> tuple[Path, ...]:
        path = self.scoped / name
        if not self.scoped.exists():
            return ()
        files.check_entry(path, directory=True)
        entries: list[Path] = []
        maximum = MAX_FILES + MAINTENANCE_FILE_RESERVE if name == "denials" else MAX_HISTORY
        with os.scandir(path) as listing:
            for item in listing:
                if len(entries) == maximum:
                    raise SourceArchiveError("limit_exceeded")
                candidate = Path(item.path)
                files.check_entry(candidate)
                if candidate.name.startswith(".stage-"):
                    raise SourceArchiveError("capture_incomplete")
                sha256(candidate.name)
                entries.append(candidate)
        return tuple(sorted(entries))

    def _denials(self) -> tuple[_Denial, ...]:
        result: list[_Denial] = []
        for path in self._entries("denials"):
            payload = files.read_bytes(path, DENIAL_BYTES)
            if digest(payload) != path.name:
                raise SourceArchiveError()
            result.append(_Denial.decode(payload))
        return tuple(result)

    def _check_ref(self, ref: SourceRef) -> None:
        if ref.archive_id != self.archive_id or ref.scope_id != self.scope.key:
            raise SourceArchiveError("scope_denied")
        denials = self._denials()
        if any(ref.manifest_sha256 in denial.manifests for denial in denials):
            raise SourceArchiveError("scope_denied")
        if denials:
            keys = self._manifest(ref.manifest_sha256).metadata.retention_keys
            if any(denial.source in keys for denial in denials):
                raise SourceArchiveError("scope_denied")

    def _manifest(self, fingerprint: str) -> SourceManifest:
        payload = files.read_bytes(self.scoped / "versions" / sha256(fingerprint), MANIFEST_BYTES)
        if digest(payload) != fingerprint:
            raise SourceArchiveError()
        manifest = decode_manifest(payload)
        if manifest.metadata.scope.key != self.scope.key:
            raise SourceArchiveError("scope_denied")
        return manifest

    def _ref(self, fingerprint: str, anchor: str = "u1") -> SourceRef:
        return SourceRef(self.archive_id, self.scope.key, fingerprint, anchor)

    def _authorize_manifest(self, manifest: SourceManifest, operation: str) -> None:
        # Legacy reads retain their existing graph-authorized contract. New formats
        # require separate, explicit release policy; stored facts cannot grant it.
        if (
            manifest.metadata.details is None
            and not operation.startswith("owner_")
            and (self.access is None or self.access.audience != "owner")
        ):
            return
        if self.access is None:
            raise SourceArchiveError("capability_unavailable")
        self.access.authorize(manifest.metadata, manifest.payload_sha256, operation)

    def _authorize_ref(self, ref: SourceRef, operation: str) -> None:
        with self._lock():
            self._check_ref(ref)
            manifest = self._manifest(ref.manifest_sha256)
        # Provider callbacks must never run under the archive lock. The caller
        # reopens the immutable ref under that lock and rechecks local denials.
        self._authorize_manifest(manifest, operation)

    def _history(self, source: str) -> tuple[tuple[str, SourceManifest], ...]:
        if not self.scoped.exists():
            return ()
        if (self.scoped / "pending" / source).exists():
            raise SourceArchiveError("capture_incomplete")
        path = self.scoped / "heads" / source
        if not path.exists():
            # Missing heads cannot silently conceal a committed, later version.
            if any(
                self._manifest(p.name).metadata.logical_key == source
                for p in self._entries("versions")
            ):
                raise SourceArchiveError("capture_incomplete")
            return ()
        obj = object_fields(parse_json(files.read_bytes(path, MANIFEST_BYTES)), {"versions"})
        fingerprints = _hashes(obj["versions"])
        committed = {
            p.name
            for p in self._entries("versions")
            if self._manifest(p.name).metadata.logical_key == source
        }
        if set(fingerprints) != committed:
            raise SourceArchiveError("capture_incomplete")
        return self._validate_chain(source, fingerprints)

    def _validate_chain(
        self, source: str, fingerprints: tuple[str, ...]
    ) -> tuple[tuple[str, SourceManifest], ...]:
        result: list[tuple[str, SourceManifest]] = []
        previous: str | None = None
        versions: dict[tuple[str, ...], str] = {}
        interactions: set[tuple[str, ...]] = set()
        for sequence, fingerprint in enumerate(fingerprints, start=1):
            manifest = self._manifest(fingerprint)
            if (
                manifest.sequence != sequence
                or manifest.predecessor != previous
                or manifest.metadata.logical_key != source
                or versions.get(manifest.metadata.artifact_key, manifest.payload_sha256)
                != manifest.payload_sha256
            ):
                raise SourceArchiveError()
            key = manifest.metadata.encounter_key
            if key in interactions:
                raise SourceArchiveError()
            interactions.add(key)
            versions[manifest.metadata.artifact_key] = manifest.payload_sha256
            result.append((fingerprint, manifest))
            previous = fingerprint
        return tuple(result)

    def _objects(self, manifest: SourceManifest) -> tuple[bytes, tuple[SourceUnit, ...]]:
        if manifest.chat_recipe_sha256 is not None:
            recipe = self._recipe(manifest)
            payload = recipe.original(
                lambda key: files.read_bytes(self.scoped / "blobs" / key, manifest.payload_bytes),
                manifest.payload_bytes,
            )
            units = decode_chat(payload, manifest.metadata)
            if any(unit.source_id is None for unit in units):
                raise SourceArchiveError()
            reading = encode_units(units)
        else:
            payload = files.read_bytes(
                self.scoped / "blobs" / manifest.payload_sha256, 64 * 1024 * 1024
            )
            reading = files.read_bytes(
                self.scoped / "readings" / manifest.reading_sha256, READING_BYTES
            )
            units = decode_units(reading)
        if (
            digest(payload) != manifest.payload_sha256
            or len(payload) != manifest.payload_bytes
            or digest(reading) != manifest.reading_sha256
        ):
            raise SourceArchiveError()
        if tuple(unit.anchor_id for unit in units) != manifest.anchor_ids:
            raise SourceArchiveError()
        return payload, units

    @staticmethod
    def _reading_object(manifest: SourceManifest) -> str:
        return manifest.chat_recipe_sha256 or manifest.reading_sha256

    def _recipe(self, manifest: SourceManifest) -> ChatRecipe:
        key = manifest.chat_recipe_sha256
        if key is None:
            raise SourceArchiveError()
        payload = files.read_bytes(self.scoped / "readings" / key, RECIPE_BYTES)
        if digest(payload) != key:
            raise SourceArchiveError()
        return ChatRecipe.decode(payload)

    def _storage_objects(self, manifests: list[SourceManifest]) -> tuple[set[str], set[str]]:
        blobs: set[str] = set()
        readings: set[str] = set()
        for manifest in manifests:
            readings.add(self._reading_object(manifest))
            if manifest.chat_recipe_sha256 is None:
                blobs.add(manifest.payload_sha256)
            else:
                blobs.update(self._recipe(manifest).messages)
            if len(blobs) > MAX_HISTORY or len(readings) > MAX_HISTORY:
                raise SourceArchiveError("limit_exceeded")
        return blobs, readings

    def _publish(self, path: Path, payload: bytes, *, overwrite: bool = False) -> int:
        if not overwrite and path.exists():
            if files.read_bytes(path, len(payload)) != payload:
                raise SourceArchiveError()
            # A previous link may have succeeded before directory fsync failed.
            # Equality proves identity, not durable publication of the name.
            files.fsync_directory(path.parent)
            return 0
        # Count recovery aliases without consuming their bytes; denial must
        # precede the separate, stricter staging-alias validation and cleanup.
        if path.parent.name == "denials" and (
            len(self._all_files(recovery_links=True)) >= MAX_FILES + MAINTENANCE_FILE_RESERVE
        ):
            raise SourceArchiveError("limit_exceeded")
        files.write_bytes(path, payload, overwrite=overwrite)
        return len(payload)

    def _all_files(self, *, recovery_links: bool = False) -> tuple[Path, ...]:
        result: list[Path] = []
        pending = [self.root]
        directories = 0
        while pending:
            directory = pending.pop()
            directories += 1
            if directories > 7 * MAX_HISTORY + 2:
                raise SourceArchiveError("limit_exceeded")
            files.check_entry(directory, directory=True)
            with os.scandir(directory) as listing:
                for item in listing:
                    path = Path(item.path)
                    if item.is_dir(follow_symlinks=False):
                        files.check_entry(path, directory=True)
                        if directories + len(pending) >= 7 * MAX_HISTORY + 2:
                            raise SourceArchiveError("limit_exceeded")
                        pending.append(path)
                    else:
                        if len(result) >= MAX_FILES + MAINTENANCE_FILE_RESERVE + 1:
                            raise SourceArchiveError("limit_exceeded")
                        files.check_entry(path, recovery_links=recovery_links)
                        result.append(path)
        return tuple(result)

    def _capture_capacity(self, targets: tuple[Path, ...]) -> tuple[Path, ...]:
        """Reserve new immutable/index files and one simultaneous staging file.

        Readers and maintenance have bounded reserved headroom; new capture does
        not. Check all target directories before creating pending publication.
        """
        existing = self._all_files()
        additions = tuple(path for path in targets if not path.exists())
        new_scope = not self.scoped.exists()
        if new_scope and len(self._scopes()) >= MAX_HISTORY:
            raise SourceArchiveError("limit_exceeded")
        if len(existing) + len(additions) + int(new_scope) + 1 > MAX_FILES:
            raise SourceArchiveError("limit_exceeded")
        for folder in {path.parent.name for path in additions}:
            if (
                len(self._entries(folder)) + sum(path.parent.name == folder for path in additions)
                > MAX_HISTORY
            ):
                raise SourceArchiveError("limit_exceeded")
        return existing

    def publish(
        self,
        *,
        payload: bytes,
        metadata: SourceMetadata,
        units: tuple[SourceUnit, ...],
        reuse_messages: bool = False,
        reuse_context: bool = False,
        publication: AbstractContextManager[None] | None = None,
    ) -> CaptureReceipt:
        # This opt-in is for already-authorized local rereads. Legacy capture and
        # provider observations keep their existing encounter history semantics.
        if reuse_context and (metadata.details is None or metadata.details.provider != "local"):
            raise SourceArchiveError("capability_unavailable")
        if metadata.scope.key != self.scope.key:
            raise SourceArchiveError("scope_denied")
        checked_limits = self.limits
        maximum = checked_limits.for_kind(metadata.kind)
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= maximum:
            raise SourceArchiveError("limit_exceeded")
        if metadata.kind == "chat":
            value = parse_json(payload, maximum_bytes=8 * 1024 * 1024)
            if secret_hits(value):
                raise SourceArchiveError("privacy_rejected")
        reading = encode_units(units)
        if len(reading) > checked_limits.reading_bytes:
            raise SourceArchiveError("limit_exceeded")
        source, payload_hash = _source_key(metadata), digest(payload)
        recipe = message_recipe(payload, metadata, units) if reuse_messages else None
        if recipe is not None and metadata.kind != "chat":
            raise SourceArchiveError()
        stored_reading, blobs = recipe if recipe is not None else (reading, {payload_hash: payload})
        reading_key = digest(stored_reading)
        with self._lock(write=True):
            # The same handle may have been explicitly reconfigured while the
            # acquisition bytes were being validated outside this lock.
            if self.limits != checked_limits:
                raise SourceArchiveError("capture_incomplete")
            if self.scoped.exists():
                self._check_scope()
            denials = self._denials()
            if any(
                d.source in metadata.retention_keys or payload_hash in d.payloads for d in denials
            ):
                raise SourceArchiveError("scope_denied")
            if denials and not metadata.identity_known:
                raise SourceArchiveError("scope_denied")
            if metadata.details is not None and metadata.details.provider == "google":
                for path in self._entries("versions"):
                    prior = self._manifest(path.name).metadata
                    if prior.details is None or (
                        prior.details.provider,
                        prior.details.principal,
                    ) != (
                        metadata.details.provider,
                        metadata.details.principal,
                    ):
                        continue
                    # No relabeling an existing copy into a different retention
                    # group, or nested excerpt chains. These need an explicit
                    # maintenance operation, not an import-side identity guess.
                    if set(prior.retention_keys).intersection(metadata.retention_keys) and (
                        prior.logical_key != metadata.logical_key
                    ):
                        raise SourceArchiveError("scope_denied")
            history = self._history(source)
            duplicate: str | None = None
            for fingerprint, old in history:
                if old.metadata.artifact_key == metadata.artifact_key:
                    self._objects(old)
                    if old.payload_sha256 != payload_hash:
                        raise SourceArchiveError()
                    if old.metadata.encounter_key != metadata.encounter_key:
                        # Reuse only the exact same task, origin, version, reader,
                        # scope and coverage. New observation times alone are not
                        # evidence of changed content. Never rewrite the old manifest.
                        if (
                            reuse_context
                            and metadata.identity_known
                            and replace(
                                old.metadata,
                                interaction_id=metadata.interaction_id,
                                captured_at=metadata.captured_at,
                                observed_at=metadata.observed_at,
                            )
                            == metadata
                            and old.reading_sha256 == digest(reading)
                        ):
                            duplicate = duplicate or fingerprint
                        # An exact encounter later in history takes precedence,
                        # including its conflict check. Do not hide it by reuse.
                        continue
                    if old.metadata != metadata or old.reading_sha256 != digest(reading):
                        raise SourceArchiveError()
                    duplicate = fingerprint
                    break
            if duplicate is not None:
                with publication if publication is not None else nullcontext():
                    return CaptureReceipt(
                        "duplicate",
                        self._ref(duplicate, units[0].anchor_id),
                        len(payload),
                        0,
                        metadata.coverage,
                    )
            manifest = SourceManifest(
                metadata,
                payload_hash,
                len(payload),
                digest(reading),
                len(history) + 1,
                history[-1][0] if history else None,
                tuple(unit.anchor_id for unit in units),
                reading_key if recipe is not None else None,
                format_version=(
                    (
                        5
                        if metadata.details.sheet_excerpt
                        else 4
                        if metadata.details.identity_version == 2
                        else 3
                    )
                    if metadata.details is not None
                    else None
                ),
            )
            serialized = encode_manifest(manifest)
            if len(serialized) > self.limits.manifest_bytes:
                raise SourceArchiveError("limit_exceeded")
            fingerprint = digest(serialized)
            head = encode({"versions": [*(key for key, _ in history), fingerprint]})
            if len(head) > MANIFEST_BYTES or len(history) >= MAX_HISTORY:
                raise SourceArchiveError("limit_exceeded")
            pending = self.scoped / "pending" / source
            immutable = {
                **{self.scoped / "blobs" / key: value for key, value in blobs.items()},
                self.scoped / "readings" / reading_key: stored_reading,
                self.scoped / "versions" / fingerprint: serialized,
            }
            existing = self._capture_capacity(
                (
                    pending,
                    *immutable,
                    self.scoped / "heads" / source,
                )
            )
            used = sum(path.stat().st_size for path in existing)
            if not self.scoped.exists():
                used += len(encode(asdict(self.scope)))
            # Charge new immutable objects only, plus head/pending and one largest
            # staging object. Reused bodies are verified by _publish before commit.
            additions = [len(value) for path, value in immutable.items() if not path.exists()]
            if (
                used + sum(additions) + len(head) + 256 + max([len(head), 256, *additions])
                > self.max_bytes
            ):
                raise SourceArchiveError("limit_exceeded")
            # No host reservation during lock waits, history scans or capacity
            # checks. Cancellation may retire the operation until this exact
            # existing transaction starts; no second filesystem lock is taken.
            with publication if publication is not None else nullcontext():
                self._ensure_scope()
                self._publish(pending, encode({"next": fingerprint}))
                new_bytes = sum(self._publish(path, value) for path, value in immutable.items())
                self._publish(self.scoped / "heads" / source, head, overwrite=True)
                pending.unlink()
                fsync_directory(pending.parent)
                return CaptureReceipt(
                    "preserved",
                    self._ref(fingerprint, units[0].anchor_id),
                    len(payload),
                    new_bytes,
                    metadata.coverage,
                )

    def lookup(
        self, source_id: str | SourceMetadata, *, max_versions: int = 5
    ) -> tuple[SourceRef, ...]:
        positive(max_versions, maximum=5)
        if isinstance(source_id, SourceMetadata):
            if source_id.scope.key != self.scope.key:
                raise SourceArchiveError("scope_denied")
            source = source_id.logical_key
        else:
            source = _source_key(source_id)
        with self._lock():
            if any(source == denial.source for denial in self._denials()):
                return ()
            history = self._history(source)
            if len(history) > max_versions:
                raise SourceArchiveError("limit_exceeded")
        for _, manifest in history:
            self._authorize_manifest(manifest, "discover")
        with self._lock():
            if self._history(source) != history or any(
                source == denial.source for denial in self._denials()
            ):
                raise SourceArchiveError("capture_incomplete")
            return tuple(self._ref(key, manifest.anchor_ids[0]) for key, manifest in history)

    def discover(
        self,
        *,
        query: str | None = None,
        source_id: str | None = None,
        source_version: str | None = None,
        offset: int = 0,
        limit: int = 5,
        snapshot_sha256: str | None = None,
    ) -> SourceDiscovery:
        """Search only exact-scope metadata, never source bodies or other scopes.

        Each page considers at most 100 manifests and returns at most five. A
        continuation must bind the same listing; new capture requires restarting.
        Missing matches with more_metadata=True are not evidence of absence.
        """
        positive(limit, maximum=5)
        if type(offset) is not int or not 0 <= offset <= MAX_HISTORY:
            raise SourceArchiveError("limit_exceeded")
        for value in (query, source_id, source_version):
            if value is not None:
                text(value)
        with self._lock():
            entries = self._entries("versions")
            denials = self._denials()
            identity = digest(encode([[p.name for p in entries], [d.source for d in denials]]))
            if snapshot_sha256 is not None and sha256(snapshot_sha256) != identity:
                raise SourceArchiveError("capture_incomplete")
            if offset and snapshot_sha256 is None:
                raise SourceArchiveError("capture_incomplete")
            denied = {d.source for d in denials}
            candidates: list[tuple[str, SourceManifest]] = []
            end = min(len(entries), offset + 100)
            cursor = offset
            while cursor < end and len(candidates) < limit:
                path = entries[cursor]
                cursor += 1
                manifest = self._manifest(path.name)
                metadata = manifest.metadata
                if denied.intersection(metadata.retention_keys):
                    continue
                if source_id is not None and metadata.source_id != source_id:
                    continue
                if source_version is not None and metadata.source_version != source_version:
                    continue
                haystack = " ".join(
                    (
                        metadata.source_id,
                        metadata.source_version,
                        metadata.captured_at,
                        metadata.origin or "",
                        metadata.details.title or "" if metadata.details else "",
                    )
                ).casefold()
                if query is not None and query.casefold() not in haystack:
                    continue
                candidates.append((path.name, manifest))
        permitted: list[tuple[str, SourceManifest]] = []
        incomplete = False
        for key, manifest in candidates:
            try:
                self._authorize_manifest(manifest, "discover")
            except SourceArchiveError as error:
                if error.code not in (
                    "scope_denied",
                    "capability_unavailable",
                    "source_unavailable",
                ):
                    raise
                incomplete = True
            else:
                permitted.append((key, manifest))
        with self._lock():
            current = digest(
                encode(
                    [
                        [p.name for p in self._entries("versions")],
                        [d.source for d in self._denials()],
                    ]
                )
            )
            if current != identity:
                raise SourceArchiveError("capture_incomplete")
            matches: list[SourceMatch] = []
            for key, manifest in permitted:
                metadata = manifest.metadata
                details = metadata.details
                self._history(metadata.logical_key)
                matches.append(
                    SourceMatch(
                        self._ref(key, manifest.anchor_ids[0]).as_uri(),
                        str(safe_display(metadata.source_version)),
                        metadata.captured_at,
                        metadata.kind,
                        metadata.coverage,
                        metadata.authorship,
                        title=str(safe_display(details.title))
                        if details and details.title
                        else None,
                        observed_at=metadata.observed_at,
                        source_time=details.source_time if details else None,
                        representation=details.representation if details else None,
                    )
                )
            return SourceDiscovery(
                tuple(matches), cursor < len(entries), cursor, identity, incomplete
            )

    def _selected_history(
        self, ref: SourceRef
    ) -> tuple[SourceManifest, tuple[tuple[str, SourceManifest], ...]]:
        """Validate selected metadata under the caller's lock without loading bodies."""
        self._check_ref(ref)
        manifest = self._manifest(ref.manifest_sha256)
        if any(d.source in manifest.metadata.retention_keys for d in self._denials()):
            raise SourceArchiveError("scope_denied")
        history = self._history(manifest.metadata.logical_key)
        if ref.manifest_sha256 not in {key for key, _ in history}:
            raise SourceArchiveError("capture_incomplete")
        if ref.anchor_id not in manifest.anchor_ids:
            raise SourceArchiveError()
        return manifest, history

    def _selected(
        self, ref: SourceRef
    ) -> tuple[SourceManifest, tuple[SourceUnit, ...], tuple[tuple[str, SourceManifest], ...]]:
        manifest, history = self._selected_history(ref)
        # _objects verifies the decoded anchors exactly match the manifest.
        _, units = self._objects(manifest)
        return manifest, units, history

    def read(
        self,
        ref: SourceRef,
        *,
        max_chars: int = 12_000,
        max_versions: int = 5,
        neighbors: bool = False,
        operation: str = "inspect",
    ) -> SourceExcerpt:
        positive(max_chars, maximum=48_000)
        positive(max_versions, maximum=5)
        if operation not in ("inspect", "evidence", "owner_view", "owner_preview"):
            raise SourceArchiveError("scope_denied")
        self._authorize_ref(ref, operation)
        with self._lock():
            manifest, history = self._selected_history(ref)
            history_head = history[-1][0]
            later = history[manifest.sequence :]
        permitted: list[tuple[str, SourceManifest]] = []
        # Related refs and observation relationships reveal source metadata too.
        # Check each bounded candidate under the same operation/audience, outside
        # the archive lock, while preserving independently authorized history.
        for key, item in later[:max_versions]:
            try:
                self._authorize_manifest(item, operation)
            except SourceArchiveError as error:
                if error.code not in (
                    "scope_denied",
                    "capability_unavailable",
                    "source_unavailable",
                ):
                    raise
            else:
                permitted.append((key, item))
        with self._lock():
            manifest, units, history = self._selected(ref)
            if history[-1][0] != history_head:
                raise SourceArchiveError("capture_incomplete")
            selected: list[SourceUnit] = []
            used = 0
            start = next(i for i, unit in enumerate(units) if unit.anchor_id == ref.anchor_id)
            if neighbors and start:
                pair_size = len(encode_units(units[start - 1 : start + 1]).decode("utf-8"))
                if pair_size <= max_chars:
                    start -= 1
            # Adjacent whole units, in captured order. No inferred chronology.
            for unit in units[start:]:
                size = len(encode_units((unit,)).decode("utf-8"))
                if used + size > max_chars:
                    break
                selected.append(unit)
                used += size
            references = tuple(self._ref(key, item.anchor_ids[0]) for key, item in permitted)
            details = manifest.metadata.details
            return SourceExcerpt(
                ref,
                tuple(selected),
                len(units) - start - len(selected),
                references,
                len(later) - len(permitted),
                manifest.metadata.coverage,
                manifest.metadata.privacy,
                source_version=manifest.metadata.source_version,
                captured_at=manifest.metadata.captured_at,
                capture_method=manifest.metadata.capture_method,
                gaps=manifest.metadata.gaps,
                authorship=manifest.metadata.authorship,
                observed_at=manifest.metadata.observed_at,
                source_time=details.source_time if details else None,
                provider_revision=details.provider_revision if details else None,
                representation=details.representation if details else None,
                reader_id=details.reader_id if details else None,
                sheet_excerpt=details.sheet_excerpt if details else None,
                title=str(safe_display(details.title))
                if details and details.title is not None
                else None,
                later_observations=tuple(
                    SourceObservation(
                        self._ref(key, item.anchor_ids[0]),
                        _observation_relationship(manifest, item),
                    )
                    for key, item in permitted
                )
                if details
                else (),
                previous_units=start,
                history_head=history[-1][0],
                next_anchor=(
                    units[start + len(selected)].anchor_id
                    if start + len(selected) < len(units)
                    else None
                ),
            )

    def verify_refs(
        self,
        refs: tuple[SourceRef, ...],
        *,
        expected_heads: Mapping[SourceRef, str] | None = None,
        operation: str = "inspect",
    ) -> None:
        """Final serialized integrity/revocation check; does not repair or write."""
        for ref in refs:
            self._authorize_ref(ref, operation)
        self._verify_refs_local(refs, expected_heads=expected_heads)

    def _verify_refs_local(
        self, refs: tuple[SourceRef, ...], *, expected_heads: Mapping[SourceRef, str] | None = None
    ) -> None:
        """Repeat only local integrity/denial checks after the external access check."""
        with self.hold_verified_refs(refs, expected_heads=expected_heads):
            pass

    @contextmanager
    def hold_verified_refs(
        self, refs: tuple[SourceRef, ...], *, expected_heads: Mapping[SourceRef, str] | None = None
    ) -> Iterator[None]:
        """Hold local denials stable during final publication; this grants no access."""
        with self._lock():
            for ref in refs:
                _, _, history = self._selected(ref)
                if expected_heads is not None and expected_heads.get(ref) != history[-1][0]:
                    raise SourceArchiveError("capture_incomplete")
            yield

    def original(self, ref: SourceRef) -> bytes:
        """Internal acquisition verification only; never an unrestricted public CLI."""
        self._authorize_ref(ref, "original")
        with self._lock():
            manifest, _, _ = self._selected(ref)
            return self._objects(manifest)[0]

    def pdf_page(
        self, ref: SourceRef, page: int, *, operation: str = "render"
    ) -> tuple[bytes, SourceRef]:
        """Supply one selected original to the bounded, explicitly approved renderer."""
        positive(page, maximum=256)
        if operation not in ("render", "owner_preview", "owner_view"):
            raise SourceArchiveError("scope_denied")
        self._authorize_ref(ref, operation)
        with self._lock():
            manifest, units, _ = self._selected(ref)
            if manifest.metadata.kind != "pdf":
                raise SourceArchiveError("source_unavailable")
            selected = next((u for u in units if u.position == page), None)
            if selected is None or selected.kind not in ("page", "image_page", "ocr_page"):
                raise SourceArchiveError("source_unavailable")
            return self._objects(manifest)[0], self._ref(ref.manifest_sha256, selected.anchor_id)

    def office_document(self, ref: SourceRef, *, operation: str = "render") -> tuple[bytes, str]:
        """Authorize the saved Office original, not an inferred page or live file."""
        if operation not in ("render", "owner_view"):
            raise SourceArchiveError("scope_denied")
        self._authorize_ref(ref, operation)
        with self._lock():
            manifest, _, _ = self._selected(ref)
            if manifest.metadata.kind not in ("docx", "xlsx", "pptx"):
                raise SourceArchiveError("source_unavailable")
            return self._objects(manifest)[0], manifest.metadata.kind

    def office_image(
        self, ref: SourceRef, *, operation: str = "render"
    ) -> tuple[bytes, str, SourceUnit]:
        result = self.image(ref, operation=operation)
        if result[1] == "image":
            raise SourceArchiveError("source_unavailable")
        return result

    def image(self, ref: SourceRef, *, operation: str = "render") -> tuple[bytes, str, SourceUnit]:
        """Supply only an exact saved embedded-frame anchor to the bounded worker."""
        if operation not in ("render", "owner_view"):
            raise SourceArchiveError("scope_denied")
        self._authorize_ref(ref, operation)
        with self._lock():
            manifest, units, _ = self._selected(ref)
            details = manifest.metadata.details
            selected = next((unit for unit in units if unit.anchor_id == ref.anchor_id), None)
            if (
                manifest.metadata.kind not in ("docx", "xlsx", "pptx", "image")
                or details is None
                or details.reader_id
                not in (
                    ("tedd-image:1",) if manifest.metadata.kind == "image" else OFFICE_IMAGE_READERS
                )
                or selected is None
                or selected.kind != "image_page"
                or selected.location is None
                or selected.location.container is None
            ):
                raise SourceArchiveError("source_unavailable")
            return self._objects(manifest)[0], manifest.metadata.kind, selected

    def recover(self) -> None:
        """Explicit recovery: validate committed objects, rebuild heads, prune staging."""
        with self._lock(write=True, repair_scope=True):
            if not self.scoped.exists():
                return
            self._ensure_scope()
            # A prior mkdir may have succeeded before its parent sync failed.
            # Recovery acknowledges both the scope entry and its child entries.
            fsync_directory(self.scoped.parent)
            fsync_directory(self.scoped)
            self._prune_staging()
            # A crash may leave only the initial source-identity denial. Expand
            # every exclusion before rebuilding any surviving history or erasing
            # objects, including when the selected version was the predecessor.
            denials = tuple(
                expanded
                for denial in self._denials()
                if (expanded := self._expand_denial(denial)).disposition == "erase"
            )
            revoked = {key for denial in denials for key in denial.manifests}
            histories: dict[str, list[tuple[str, SourceManifest]]] = {}
            for path in self._entries("versions"):
                if path.name in revoked:
                    continue
                manifest = self._manifest(path.name)
                self._objects(manifest)
                histories.setdefault(manifest.metadata.logical_key, []).append(
                    (path.name, manifest)
                )
            for source, history in histories.items():
                history.sort(key=lambda pair: pair[1].sequence)
                keys = tuple(key for key, _ in history)
                self._validate_chain(source, keys)
                self._publish(
                    self.scoped / "heads" / source, encode({"versions": keys}), overwrite=True
                )
            for path in self._entries("heads"):
                if path.name not in histories:
                    path.unlink()
            for path in self._entries("pending"):
                path.unlink()
            for denial in denials:
                self._erase(denial)
            self._prune_unreferenced()
            for name in ("heads", "pending", "versions", "blobs", "readings"):
                fsync_directory(self.scoped / name)

    def _prune_staging(self) -> None:
        """Recognize only proven local link-before-unlink publication residue."""
        groups: dict[tuple[int, int], list[Path]] = {}
        for path in self._all_files(recovery_links=True):
            info = path.lstat()
            groups.setdefault((info.st_dev, info.st_ino), []).append(path)
        stages: list[Path] = []
        for paths in groups.values():
            count = paths[0].lstat().st_nlink
            temporary = [path for path in paths if path.name.startswith(".stage-")]
            if count != len(paths) or count not in (1, 2):
                raise SourceArchiveError("privacy_rejected")
            if count == 2:
                if len(temporary) != 1 or paths[0].parent != paths[1].parent:
                    raise SourceArchiveError("privacy_rejected")
                target = next(path for path in paths if path not in temporary)
                sha256(target.name)
            stages.extend(temporary)
        # Validate every alias before removing any; external links never qualify.
        for path in stages:
            path.unlink()
            fsync_directory(path.parent)

    def _prune_unreferenced(self) -> None:
        manifests = self._surviving_manifests()
        blobs, readings = self._storage_objects(manifests)
        for folder, referenced in (
            ("blobs", blobs),
            ("readings", readings),
        ):
            for path in self._entries(folder):
                if path.name not in referenced:
                    path.unlink()

    def _surviving_manifests(self) -> list[SourceManifest]:
        denials = tuple(item for item in self._denials() if item.disposition == "erase")
        denied = {key for item in denials for key in item.manifests}
        sources = {item.source for item in denials}
        result: list[SourceManifest] = []
        for path in self._entries("versions"):
            if path.name in denied:
                continue
            manifest = self._manifest(path.name)
            if manifest.metadata.logical_key not in sources:
                result.append(manifest)
        return result

    def _erase(self, denial: _Denial) -> bool | None:
        # Only erasure targets are excluded; restricted histories still own bytes.
        others = self._surviving_manifests()
        shared_payloads, shared_readings = self._storage_objects(others)
        removed = [
            self._manifest(p.name) for p in self._entries("versions") if p.name in denial.manifests
        ]
        removed_payloads: set[str] = set()
        complete = len(removed) == len(denial.manifests)
        for manifest in removed:
            if manifest.chat_recipe_sha256 is None:
                removed_payloads.add(manifest.payload_sha256)
            else:
                # Target recipes establish sharing information, not permission
                # to erase. Only the strict survivor inventory above protects
                # live objects. A denied target can be damaged or already gone.
                try:
                    removed_payloads.update(self._recipe(manifest).messages)
                except FileNotFoundError:
                    complete = False
                except SourceArchiveError as error:
                    if error.code not in ("source_corrupt", "limit_exceeded"):
                        raise
                    complete = False
            if len(removed_payloads) > MAX_HISTORY:
                raise SourceArchiveError("limit_exceeded")
        retained: bool | None
        if (removed_payloads | set(denial.payloads)) & shared_payloads:
            retained = True
        else:
            retained = False if complete or not shared_payloads else None
        # Different historical stream IDs can now belong to the same restricted
        # resource. Select their heads before erasing manifests, including an
        # interrupted erasure whose manifests are already absent.
        sources = {denial.source, *(item.metadata.logical_key for item in removed)}
        denied_manifests = set(denial.manifests)
        for path in self._entries("heads"):
            head = object_fields(parse_json(files.read_bytes(path, MANIFEST_BYTES)), {"versions"})
            versions = set(_hashes(head["versions"]))
            if versions & denied_manifests:
                if not versions <= denied_manifests:
                    raise SourceArchiveError("capture_incomplete")
                sources.add(path.name)
        for source in sources:
            (self.scoped / "pending" / source).unlink(missing_ok=True)
            (self.scoped / "heads" / source).unlink(missing_ok=True)
        for name in ("pending", "heads"):
            fsync_directory(self.scoped / name)
        for fingerprint in denial.manifests:
            (self.scoped / "versions" / fingerprint).unlink(missing_ok=True)
        for folder, fingerprints in (
            ("blobs", set(denial.payloads) - shared_payloads),
            ("readings", set(denial.readings) - shared_readings),
        ):
            for fingerprint in fingerprints:
                (self.scoped / folder / fingerprint).unlink(missing_ok=True)
        for name in ("versions", "blobs", "readings", "heads", "pending"):
            fsync_directory(self.scoped / name)
        return retained

    def _initial_denial(self, ref: SourceRef) -> _Denial:
        if ref.archive_id != self.archive_id or ref.scope_id != self.scope.key:
            raise SourceArchiveError("scope_denied")
        matches = [d for d in self._denials() if ref.manifest_sha256 in d.manifests]
        denial = next((d for d in matches if d.disposition == "erase"), next(iter(matches), None))
        if denial is not None:
            if denial.disposition == "retain":
                manifest = self._manifest(ref.manifest_sha256)
                if (
                    ref.anchor_id not in manifest.anchor_ids
                    or denial.source not in manifest.metadata.retention_keys
                ):
                    raise SourceArchiveError()
                # A retained copy-ID marker is an exclusion, not a narrower
                # deletion scope. Resolve an existing ref to its approved group.
                return replace(denial, source=manifest.metadata.logical_key)
            return denial
        manifest = self._manifest(ref.manifest_sha256)
        if ref.anchor_id not in manifest.anchor_ids:
            raise SourceArchiveError()
        return _Denial(
            manifest.metadata.logical_key,
            (ref.manifest_sha256,),
            (manifest.payload_sha256,),
            (self._reading_object(manifest),),
        )

    def forget_identity(self, ref: SourceRef) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Content-free cleanup identity, including after denial and erasure."""
        with self._lock():
            denial = self._expand_denial(self._initial_denial(ref), publish=False)
            return denial.manifests, denial.graph_records

    def deny(
        self,
        ref: SourceRef,
        *,
        graph_records: tuple[str, ...] = (),
        expected_manifests: tuple[str, ...] | None = None,
    ) -> None:
        """Legacy erasure intent: block now; forget/recovery completes deletion."""
        self._deny(ref, graph_records=graph_records, expected_manifests=expected_manifests)

    def restrict(self, ref: SourceRef, *, expected_manifests: tuple[str, ...] | None = None) -> str:
        """Deny every version while retaining history; never downgrade erasure."""
        return self._deny(ref, disposition="retain", expected_manifests=expected_manifests)

    def _deny(
        self,
        ref: SourceRef,
        *,
        disposition: str = "erase",
        graph_records: tuple[str, ...] = (),
        expected_manifests: tuple[str, ...] | None = None,
    ) -> str:
        with self._lock(write=True):
            denial = self._initial_denial(ref)
            if (
                expected_manifests is not None
                and self._expand_denial(denial, publish=False).manifests != expected_manifests
            ):
                raise SourceArchiveError("capture_incomplete")
            denial = _Denial(
                denial.source,
                denial.manifests,
                denial.payloads,
                denial.readings,
                tuple(sorted(set(denial.graph_records) | set(_graph_ids(graph_records)))),
                disposition,
            )
            payload = denial.encoded()
            self._publish(self.scoped / "denials" / digest(payload), payload)
            return self._expand_denial(denial).disposition

    def forget(self, ref: SourceRef) -> ForgetReceipt:
        with self._lock(write=True):
            denial = replace(self._initial_denial(ref), disposition="erase")
            payload = denial.encoded()
            # Durably deny first; failed physical erasure never restores access.
            self._publish(self.scoped / "denials" / digest(payload), payload)
            self._prune_staging()
            denial = self._expand_denial(denial)
            retained = self._erase(denial)
            self._prune_unreferenced()
            for name in ("blobs", "readings"):
                fsync_directory(self.scoped / name)
            return ForgetReceipt(retained)

    def _expand_denial(self, denial: _Denial, *, publish: bool = True) -> _Denial:
        inventory = [(path.name, self._manifest(path.name)) for path in self._entries("versions")]
        groups = {
            item.metadata.logical_key
            for _, item in inventory
            if denial.source in item.metadata.retention_keys
        }
        # An alias-seeded recovery must include the whole approved history, not
        # only versions bearing that copy ID. No arbitrary copy chain is inferred.
        related = [(key, item) for key, item in inventory if item.metadata.logical_key in groups]
        previous = [item for item in self._denials() if item.source in groups | {denial.source}]
        previous.append(denial)
        # Keep a copy-specific exclusion as an alias, not a misleading marker
        # whose source ID would fail validation for its sibling's references.
        source = next(iter(groups)) if len(groups) == 1 else denial.source
        expanded = _Denial(
            source,
            tuple(
                sorted(
                    {key for item in previous for key in item.manifests}
                    | {key for key, _ in related}
                )
            ),
            tuple(
                sorted(
                    {key for item in previous for key in item.payloads}
                    | {item.payload_sha256 for _, item in related}
                )
            ),
            tuple(
                sorted(
                    {key for item in previous for key in item.readings}
                    | {self._reading_object(item) for _, item in related}
                )
            ),
            tuple(sorted({key for item in previous for key in item.graph_records})),
            "erase" if any(item.disposition == "erase" for item in previous) else "retain",
        )
        payload = expanded.encoded()
        if publish:
            self._publish(self.scoped / "denials" / digest(payload), payload)
            # Preserve copy-ID exclusions before any grouped manifests can be
            # erased. Otherwise a changed copy could be reimported without its
            # excerpt annotation after forgetting the original workbook.
            for alias in sorted(
                {key for _, item in related for key in item.metadata.retention_keys} - {source}
            ):
                marker = _Denial(alias, (), (), (), disposition="retain").encoded()
                self._publish(self.scoped / "denials" / digest(marker), marker)
        return expanded

    def backup(self, destination: Path) -> None:
        """Create an inert, verified private backup; it cannot be opened for reads."""
        with self._lock():
            source_files = self._verified_backup_files()
            if len(source_files) > MAX_FILES + MAINTENANCE_FILE_RESERVE:
                raise SourceArchiveError("limit_exceeded")
            inventory = {
                path.relative_to(self.root).as_posix(): digest(
                    files.read_bytes(path, 64 * 1024 * 1024)
                )
                for path in source_files
            }
            serialized = _bounded_encode({"files": inventory}, BACKUP_INDEX_BYTES)
            destination = files.private_root(destination, create=True)
            # The refusal marker precedes every active-layout file. Completion
            # replaces its contents, never removes the backup's inert identity.
            files.write_bytes(destination / "backup.json", encode({"state": "incomplete"}))
            self._copy_layout(self.root, destination)
            for path in sorted(source_files, key=lambda p: len(p.parts)):
                if path.name.startswith(".stage-") or path.parent.name == "pending":
                    raise SourceArchiveError("capture_incomplete")
                relative = path.relative_to(self.root)
                self._copy_file(path, destination / relative)
            files.write_bytes(destination / "backup.json", serialized, overwrite=True)

    @staticmethod
    def _copy_layout(source: Path, destination: Path) -> None:
        files.directory(destination / "scopes")
        files.check_entry(source / "scopes", directory=True)
        for scope in (source / "scopes").iterdir():
            sha256(scope.name)
            files.check_entry(scope, directory=True)
            target = destination / "scopes" / scope.name
            files.directory(target)
            for name in ("blobs", "readings", "versions", "heads", "pending", "denials"):
                files.check_entry(scope / name, directory=True)
                files.directory(target / name)

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        # Only layout objects pass here; the separately bounded backup inventory
        # is generated/read directly and is never copied as an archive object.
        missing: list[Path] = []
        parent = destination.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            files.directory(parent)
        files.write_bytes(destination, files.read_bytes(source, 64 * 1024 * 1024))

    @classmethod
    def restore(cls, backup: Path, destination: Path, *, current: Archive) -> Archive:
        """Require the current trusted archive as the revocation authority.

        An old backup alone cannot authorize reactivation. Restore never changes
        the current archive or its graph, and never changes store/generation.
        """
        with current._lock():
            backup = files.private_root(backup)
            obj = object_fields(
                parse_json(
                    files.read_bytes(backup / "backup.json", BACKUP_INDEX_BYTES),
                    maximum_bytes=BACKUP_INDEX_BYTES,
                ),
                {"files"},
            )
            inventory = obj["files"]
            if (
                not isinstance(inventory, dict)
                or len(inventory) > MAX_FILES + MAINTENANCE_FILE_RESERVE
            ):
                raise SourceArchiveError()
            backup_marker, current_marker = cls._marker(backup), cls._marker(current.root)
            if {key: value for key, value in backup_marker.items() if key != "limits"} != {
                key: value for key, value in current_marker.items() if key != "limits"
            }:
                raise SourceArchiveError("scope_denied")
            snapshot = Archive(backup, current.scope, readonly=True)
            actual = {
                path.relative_to(backup).as_posix()
                for path in snapshot._all_files()
                if path != backup / "backup.json"
            }
            if set(inventory) != actual or not {"archive.json", "archive.lock"} <= actual:
                raise SourceArchiveError()
            approved: list[tuple[Path, str]] = []
            for raw, expected in inventory.items():
                relative = text(raw)
                path = Path(relative)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in relative
                    or path.as_posix() != relative
                    or path.name == "backup.json"
                ):
                    raise SourceArchiveError()
                cls._valid_relative(path)
                for parent in (backup / path).parents:
                    files.check_entry(parent, directory=True)
                    if parent == backup:
                        break
                payload = files.read_bytes(backup / path, 64 * 1024 * 1024)
                if digest(payload) != sha256(expected):
                    raise SourceArchiveError()
                approved.append((path, relative))
            destination = files.private_root(destination, create=True)
            files.write_bytes(destination / "backup.json", encode({"state": "restoring"}))
            cls._copy_layout(backup, destination)
            # Public open remains disabled through copying and recovery.
            for path, _ in approved:
                if path.name != "archive.json":
                    cls._copy_file(backup / path, destination / path)
            # A scope may have been created and forgotten after this backup.
            # Its exclusions need the same validated identity and directory layout.
            cls._copy_layout(current.root, destination)
            for scope_archive in current._scopes():
                source = scope_archive.scoped / "scope.json"
                target = destination / source.relative_to(current.root)
                if not target.exists():
                    cls._copy_file(source, target)
                elif files.read_bytes(target, MANIFEST_BYTES) != files.read_bytes(
                    source, MANIFEST_BYTES
                ):
                    raise SourceArchiveError("scope_denied")
            for source in current._all_files():
                if source.parent.name == "denials":
                    target = destination / source.relative_to(current.root)
                    if not target.exists():
                        cls._copy_file(source, target)
                    elif files.read_bytes(target, DENIAL_BYTES) != files.read_bytes(
                        source, DENIAL_BYTES
                    ):
                        raise SourceArchiveError()
            cls._copy_file(current.root / "archive.json", destination / "archive.json")
            result = cls(destination, current.scope, readonly=False)
            for archive in result._scopes():
                archive.recover()
            # Failure above leaves an inert destination, never a readable partial
            # restore. Hold current's read lock until its exclusions are installed.
            (destination / "backup.json").unlink()
            fsync_directory(destination)
        return result
