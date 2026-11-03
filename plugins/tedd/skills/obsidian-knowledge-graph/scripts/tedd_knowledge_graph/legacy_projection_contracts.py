"""Typed dependency contracts for legacy projection publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence, Set
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, TypeAlias, TypedDict, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, object]


class ValidationResult(TypedDict):
    valid: bool
    errors: list[str]


def _validate_json(value: object, *, label: str, depth: int = 0) -> None:
    if depth > 24:
        raise ValueError(f"{label} exceeds the JSON nesting limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in cast(Sequence[object], value):
            _validate_json(item, label=label, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"{label} object keys must be strings")
            _validate_json(item, label=label, depth=depth + 1)
        return
    raise ValueError(f"{label} contains a non-JSON value")


def require_json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _validate_json(value, label=label)
    return cast(JsonObject, value)


class OwnerCheck(Protocol):
    def __call__(self, subject: Path, *, allow_root: bool = False) -> bool: ...


class AtomicWrite(Protocol):
    def __call__(self, path: Path, content: str, *, mode: int = 0o600) -> None: ...


class BuildProjectionNotes(Protocol):
    def __call__(
        self,
        graph: JsonObject,
        mode: str,
        root_name: str,
        *,
        skip_validation: bool = False,
    ) -> dict[str, str]: ...


class ProjectionManifestSignature(Protocol):
    def __call__(
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
    ) -> str: ...


class ProjectLocked(Protocol):
    def __call__(
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
    ) -> JsonObject: ...


@dataclass(frozen=True)
class ProjectionDependencies:
    generated_projection_dirs: Set[str]
    projection_format_version: str
    projection_marker_version: str
    graph_schema_version: str
    projection_manifest: str
    projection_marker_tail_bytes: int
    max_manifest_bytes: int
    max_projection_files: int
    max_projection_dir_entries: int
    max_projection_path_length: int
    owner_is_current_user: OwnerCheck
    uses_posix_permissions: Callable[[], bool]
    mode_has_any: Callable[[Path, int], bool]
    is_trusted_windows_volume_root: Callable[[Path], bool]
    is_link_or_reparse_point: Callable[[Path], bool]
    _open_private_lock_descriptor: Callable[[Path, str, float], int]
    try_file_lock: Callable[[TextIO], None]
    unlock_file: Callable[[TextIO], None]
    normalize_space: Callable[[object], str]
    stable_id: Callable[[str, object], str]
    stable_hash: Callable[[object, int], str]
    committed_watermark: Callable[[JsonObject], int]
    build_projection_notes: BuildProjectionNotes
    set_private_path_mode: Callable[[Path, int], None]
    atomic_write: AtomicWrite
    utc_now: Callable[[], str]
    validate_graph: Callable[[JsonObject], ValidationResult]
    load_graph_with_digest: Callable[[Path], tuple[JsonObject, str, str]]
    assert_trusted_directory_chain: Callable[[Path, str], None]
    ensure_projection_target: Callable[[Path, str], Path]
    safe_projection_path: Callable[[Path, str], Path]
    generated_projection_path: Callable[[str], bool]
    projection_manifest_signature: ProjectionManifestSignature
    projection_file_marker: Callable[[str, str, str], str]
    marked_projection_content: Callable[[str, str, str, str], str]
    projection_file_is_owned: Callable[[Path, str, str, str], bool]
    load_projection_manifest: Callable[[Path], JsonObject | None]
    recoverable_unmanifested_projection: Callable[[Path, str, str], bool]
    projection_lock: Callable[[Path, str, float], AbstractContextManager[None]]
    _project_graph_locked: ProjectLocked


__all__ = [
    "JsonObject",
    "JsonValue",
    "ProjectionDependencies",
    "ValidationResult",
    "require_json_object",
]
