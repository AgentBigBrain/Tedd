"""Digest-gated, side-by-side v3-to-v4 migration service.

The legacy graph is a validated heterogeneous JSON document.  Mutable graph
objects use ``dict[str, object]`` at that compatibility boundary; decoded JSON
is recursively validated before it reaches migration policy or storage code.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Literal, Protocol, TypeAlias, TypedDict, cast, overload

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
JSONMap: TypeAlias = dict[str, object]


class LegacyValidationResult(TypedDict):
    valid: bool
    errors: list[str]
    warnings: list[str]
    counts: dict[str, int]


class StoreValidationResult(TypedDict):
    valid: bool
    errors: list[str]


class StoreState(TypedDict):
    watermark: int
    store_id: str


class UpgradePlan(dict[str, object]):
    """Mutable public plan with exact fields used by migration coordination."""

    @overload
    def __getitem__(self, key: Literal["source_sha256"]) -> str: ...

    @overload
    def __getitem__(
        self,
        key: Literal["source_watermark", "target_watermark"],
    ) -> int: ...

    @overload
    def __getitem__(self, key: str) -> object: ...

    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class UpgradeEnvelope(Protocol):
    @property
    def producer_agent_id(self) -> str: ...

    @property
    def task_id(self) -> str: ...

    @property
    def parent_task_id(self) -> str | None: ...

    @property
    def based_on_watermark(self) -> int: ...

    @property
    def producer_run_id(self) -> str: ...

    @property
    def discovery_kind(self) -> str: ...


class UpgradeRequest(Protocol):
    @property
    def source_path(self) -> Path: ...

    @property
    def target_path(self) -> Path: ...

    @property
    def dry_run(self) -> bool: ...

    @property
    def expected_source_sha256(self) -> str | None: ...

    @property
    def expected_plan_fingerprint(self) -> str | None: ...

    @property
    def backup_output(self) -> Path | None: ...

    @property
    def envelope(self) -> UpgradeEnvelope | None: ...


class MigrationStore(Protocol):
    def __enter__(self) -> MigrationStore: ...

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None: ...

    def validate(self, *, full: bool) -> StoreValidationResult: ...

    def export_current_graph(self, *, schema_version: str) -> JSONMap: ...

    def backup(self, output_path: Path) -> JSONMap: ...

    def checkpoint(self, mode: str) -> JSONMap: ...

    def state(self) -> StoreState: ...

    def close(self) -> None: ...


class AtomicWrite(Protocol):
    def __call__(self, path: Path, content: str, mode: int = 0o600) -> None: ...


class ImportV3Graph(Protocol):
    def __call__(
        self,
        graph: Mapping[str, object],
        target_path: Path,
        source_sha256: str,
        *,
        producer_agent_id: str,
        task_id: str,
        parent_task_id: str | None,
        discovery_kind: str,
    ) -> MigrationStore: ...


class OpenStore(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None = None,
    ) -> MigrationStore: ...


class StoreLock(Protocol):
    def __call__(self, store_path: Path, timeout_seconds: float) -> ContextManager[None]: ...


class PublishStagedFile(Protocol):
    def __call__(
        self,
        source: Path,
        destination: Path,
        *,
        overwrite: bool,
    ) -> tuple[int, int]: ...


class ReadVerifiedPublishedBytes(Protocol):
    def __call__(
        self,
        path: Path,
        expected_identity: tuple[int, int],
        *,
        maximum_bytes: int,
    ) -> bytes: ...


class FinalizePublicationLink(Protocol):
    def __call__(
        self,
        staged: Path,
        destination: Path,
        expected_identity: tuple[int, int],
        expected_sha256: str,
    ) -> None: ...


@dataclass(frozen=True)
class MigrationDependencies:
    """Call-time policy and effect boundaries needed for one migration."""

    collections: Sequence[str]
    ddl: str
    schema_user_version: int
    transform_version: str
    validate_legacy: Callable[[JSONMap], LegacyValidationResult]
    committed_watermark: Callable[[JSONMap], int]
    stable_hash: Callable[[object, int], str]
    canonical_json: Callable[[object], str]
    store_lock: StoreLock
    load_config: Callable[[Path], JSONMap]
    atomic_write: AtomicWrite
    utc_now: Callable[[], str]
    import_v3_graph: ImportV3Graph
    open_store: OpenStore
    atomic_bytes: Callable[[Path, bytes], None]
    sha256_bytes: Callable[[bytes], str]
    sha256_file: Callable[[Path], str]
    fsync_parent: Callable[[Path], None]
    publish_staged_file: PublishStagedFile
    read_verified_published_bytes: ReadVerifiedPublishedBytes
    sha256_verified_published_file: Callable[[Path, tuple[int, int]], tuple[str, int]]
    finalize_publication_link: FinalizePublicationLink
    uuid_hex: Callable[[], str]


def _decode_graph(payload: bytes) -> JSONMap:
    value: object = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Canonical v3 source must be a JSON object")
    _require_json_value(value, label="Canonical v3 source")
    return cast(JSONMap, value)


def _require_json_value(value: object, *, label: str, depth: int = 0) -> JsonValue:
    """Recursively prove that a decoded value belongs to the JSON domain."""

    if depth > 64:
        raise ValueError(f"{label} exceeds the JSON nesting limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        for item in cast(Sequence[object], value):
            _require_json_value(item, label=label, depth=depth + 1)
        return cast(Sequence[JsonValue], value)
    if isinstance(value, dict):
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"{label} object keys must be strings")
            _require_json_value(item, label=label, depth=depth + 1)
        return cast(Mapping[str, JsonValue], value)
    raise ValueError(f"{label} contains a non-JSON value")


def record_count(graph: Mapping[str, object], dependencies: MigrationDependencies) -> int:
    count = 0
    for collection in dependencies.collections:
        records = graph.get(collection, [])
        if not isinstance(records, list):
            raise ValueError(f"Canonical v3 collection must be a list: {collection}")
        count += len(records)
    return count


def upgrade_plan(
    source_path: Path,
    target_path: Path,
    payload: bytes,
    dependencies: MigrationDependencies,
) -> tuple[JSONMap, UpgradePlan, str]:
    source_sha256 = dependencies.sha256_bytes(payload)
    graph = _decode_graph(payload)
    validation = dependencies.validate_legacy(graph)
    if not validation["valid"]:
        raise ValueError(
            "Refusing to migrate an invalid v3 graph: " + "; ".join(validation["errors"])
        )
    state = UpgradePlan(
        {
            "operation": "upgrade_canonical_store",
            "from_schema": "v3-json",
            "to_schema": "v4-sqlite-wal",
            "transform_version": dependencies.transform_version,
            "ddl_sha256": hashlib.sha256(dependencies.ddl.encode("utf-8")).hexdigest(),
            "equivalence_policy": "unordered-record-collections+journal-prefix+one-migration",
            "source_path": str(source_path),
            "target_path": str(target_path),
            "source_sha256": source_sha256,
            "source_watermark": dependencies.committed_watermark(graph),
            "target_watermark": dependencies.committed_watermark(graph) + 1,
            "record_count": record_count(graph, dependencies),
            "collection_counts": validation["counts"],
            "sqlite_schema_user_version": dependencies.schema_user_version,
        }
    )
    fingerprint = dependencies.stable_hash(state, 64)
    return graph, state, fingerprint


def collections_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
    dependencies: MigrationDependencies,
) -> bool:
    for collection in dependencies.collections:
        left_records = left.get(collection, [])
        right_records = right.get(collection, [])
        if not isinstance(left_records, list) or not isinstance(right_records, list):
            return False
        left_by_id = {
            str(item.get("id")): item for item in left_records if isinstance(item, Mapping)
        }
        right_by_id = {
            str(item.get("id")): item for item in right_records if isinstance(item, Mapping)
        }
        if len(left_by_id) != len(left_records) or len(right_by_id) != len(right_records):
            return False
        if dependencies.canonical_json(left_by_id) != dependencies.canonical_json(right_by_id):
            return False
    return True


@dataclass(frozen=True)
class MigrationPaths:
    """All reviewed publication and recovery paths for one migration."""

    backup: Path
    staging: Path
    staging_key: Path
    target_key: Path
    verification_backup: Path
    verification_manifest: Path
    rollback_export: Path
    migration_manifest: Path


@dataclass(frozen=True)
class PreparedCutover:
    """Validated, durable intent for one roll-forward-only cutover."""

    plan: UpgradePlan
    paths: MigrationPaths
    fingerprint: str
    activation_timestamp: str
    current_config_sha256: str
    activated_config_sha256: str
    source_tombstone_sha256: str
    target_sha256: str
    target_key_sha256: str
    installed_validation: StoreValidationResult
    installed_state: StoreState
    restore_validation: StoreValidationResult


def _path_alias_key(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Migration path cannot be resolved safely: {path}") from error
    return os.path.normcase(str(resolved))


def _require_distinct_paths(
    named_paths: Mapping[str, Path],
    *,
    allowed_file_aliases: frozenset[frozenset[str]] = frozenset(),
) -> None:
    seen_paths: dict[str, str] = {}
    seen_files: dict[tuple[int, int], str] = {}
    for label, path in named_paths.items():
        path_key = _path_alias_key(path)
        prior_label = seen_paths.get(path_key)
        if prior_label is not None:
            raise ValueError(f"Migration paths must be distinct: {prior_label} and {label}")
        seen_paths[path_key] = label
        try:
            entry = path.stat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(f"Migration path cannot be inspected safely: {path}") from error
        identity = entry.st_dev, entry.st_ino
        prior_label = seen_files.get(identity)
        if prior_label is not None:
            if frozenset((prior_label, label)) not in allowed_file_aliases:
                raise ValueError(f"Migration files must not alias: {prior_label} and {label}")
        seen_files[identity] = label


def _require_distinct_migration_paths(
    *,
    source: Path,
    target: Path,
    config_path: Path,
    paths: MigrationPaths,
) -> None:
    _require_distinct_paths(
        {
            "v3 source": source,
            "v4 target": target,
            "configuration": config_path,
            "source backup": paths.backup,
            "staged database": paths.staging,
            "staged plan key": paths.staging_key,
            "published plan key": paths.target_key,
            "verification backup": paths.verification_backup,
            "verification manifest": paths.verification_manifest,
            "rollback export": paths.rollback_export,
            "cutover manifest": paths.migration_manifest,
        },
        allowed_file_aliases=frozenset(
            {
                frozenset(("v4 target", "staged database")),
                frozenset(("staged plan key", "published plan key")),
            }
        ),
    )


def _require_apply_envelope(
    request: UpgradeRequest,
    target: Path,
    plan: UpgradePlan,
    fingerprint: str,
) -> UpgradeEnvelope:
    if target.exists():
        raise FileExistsError(f"Reviewed v4 target already exists: {target}")
    if request.expected_source_sha256 != plan["source_sha256"]:
        raise ValueError("Canonical v3 digest differs from the reviewed upgrade plan")
    if request.expected_plan_fingerprint != fingerprint:
        raise ValueError("Upgrade transform differs from the reviewed plan")
    envelope = request.envelope
    if envelope is None:
        raise ValueError("Upgrade apply requires producer/task provenance")
    if int(envelope.based_on_watermark) != plan["source_watermark"]:
        raise ValueError("Upgrade provenance must be based on the current v3 watermark")
    return envelope


def _migration_paths(
    request: UpgradeRequest,
    source: Path,
    target: Path,
    plan: UpgradePlan,
    fingerprint: str,
    dependencies: MigrationDependencies,
) -> MigrationPaths:
    default_backup = (
        source.parent
        / "backups"
        / f"graph-v3-wm{plan['source_watermark']}-{plan['source_sha256'][:12]}.json"
    )
    backup = request.backup_output or default_backup
    staging_token = _private_staging_token(dependencies)
    staging = target.with_name(f".{target.name}.{staging_token}.migration-staging")
    verification_backup = staging.with_name(f"{staging.name}.verification-backup")
    rollback_export = _rollback_export_path(source, plan, fingerprint, staging_token)
    return MigrationPaths(
        backup=backup,
        staging=staging,
        staging_key=staging.with_name(f"{staging.name}.plan.key"),
        target_key=target.with_name(f"{target.name}.plan.key"),
        verification_backup=verification_backup,
        verification_manifest=verification_backup.with_name(
            f"{verification_backup.name}.manifest.json"
        ),
        rollback_export=rollback_export,
        migration_manifest=(
            source.parent / "backups" / f"graph-v4-migration-{fingerprint[:24]}.manifest.json"
        ),
    )


def _manifest_paths(
    request: UpgradeRequest,
    *,
    source: Path,
    target: Path,
    plan: UpgradePlan,
    fingerprint: str,
    manifest: Mapping[str, object],
) -> MigrationPaths:
    default_backup = (
        source.parent
        / "backups"
        / f"graph-v3-wm{plan['source_watermark']}-{plan['source_sha256'][:12]}.json"
    )
    backup = request.backup_output or default_backup
    staging = Path(_manifest_text(manifest, "staging_path"))
    staging_key = Path(_manifest_text(manifest, "staging_key_path"))
    verification_backup = Path(_manifest_text(manifest, "verification_backup"))
    verification_manifest = Path(_manifest_text(manifest, "verification_manifest"))
    target_key = target.with_name(f"{target.name}.plan.key")
    migration_manifest = _recovery_manifest_path(source, fingerprint)
    if migration_manifest is None:
        raise ValueError("Migration cutover manifest fingerprint is invalid")

    prefix = f".{target.name}."
    suffix = ".migration-staging"
    staging_name = staging.name
    token = (
        staging_name[len(prefix) : -len(suffix)]
        if staging_name.startswith(prefix) and staging_name.endswith(suffix)
        else ""
    )
    if (
        staging.parent != target.parent
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
        or staging_key != staging.with_name(f"{staging.name}.plan.key")
        or verification_backup != staging.with_name(f"{staging.name}.verification-backup")
        or verification_manifest
        != verification_backup.with_name(f"{verification_backup.name}.manifest.json")
    ):
        raise ValueError("Migration cutover manifest contains invalid private staging paths")
    rollback_export = _rollback_export_path(source, plan, fingerprint, token)

    paths = MigrationPaths(
        backup=backup,
        staging=staging,
        staging_key=staging_key,
        target_key=target_key,
        verification_backup=verification_backup,
        verification_manifest=verification_manifest,
        rollback_export=rollback_export,
        migration_manifest=migration_manifest,
    )
    expected_paths = {
        "source_backup": paths.backup,
        "target_key_path": paths.target_key,
        "rollback_export": paths.rollback_export,
    }
    for field, expected_path in expected_paths.items():
        if _manifest_text(manifest, field) != str(expected_path):
            raise ValueError(
                f"Migration cutover manifest path differs from the reviewed plan: {field}"
            )
    return paths


def _private_staging_token(dependencies: MigrationDependencies) -> str:
    token = dependencies.uuid_hex()
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("Migration staging identity must be 32 lowercase hexadecimal characters")
    return token


def _rollback_export_path(
    source: Path,
    plan: UpgradePlan,
    fingerprint: str,
    staging_token: str,
) -> Path:
    return (
        source.parent
        / "backups"
        / (
            f"graph-v4-baseline-wm{plan['target_watermark']}-{fingerprint[:24]}-"
            f"{staging_token}.rollback-v3.json"
        )
    )


def _recovery_manifest_path(source: Path, fingerprint: str | None) -> Path | None:
    if (
        fingerprint is None
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return None
    # A 96-bit filename prefix keeps Windows state paths bounded. The manifest
    # stores and verifies all 256 bits; a prefix collision therefore fails closed
    # at no-clobber publication or recovery validation.
    return source.parent / "backups" / f"graph-v4-migration-{fingerprint[:24]}.manifest.json"


def _load_locked_upgrade(
    source: Path,
    target: Path,
    config_path: Path,
    reviewed_plan: UpgradePlan,
    reviewed_fingerprint: str,
    dependencies: MigrationDependencies,
) -> tuple[bytes, JSONMap, UpgradePlan, JSONMap]:
    payload = source.read_bytes()
    graph, plan, fingerprint = upgrade_plan(source, target, payload, dependencies)
    if plan["source_sha256"] != reviewed_plan["source_sha256"]:
        raise ValueError("Canonical v3 graph changed while waiting for the writer lock")
    if fingerprint != reviewed_fingerprint:
        raise ValueError("Locked upgrade plan differs from the reviewed plan")
    config = dependencies.load_config(config_path)
    configured = Path(str(config.get("store_path", ""))).expanduser().resolve()
    if configured != source:
        raise ValueError("Configuration no longer points at the reviewed v3 source")
    return payload, graph, plan, config


def _ensure_source_backup(
    backup_path: Path,
    locked_payload: bytes,
    dependencies: MigrationDependencies,
) -> None:
    _publish_exact_bytes(
        backup_path,
        locked_payload,
        label="migration source backup",
        dependencies=dependencies,
    )


def _published_identity(path: Path) -> tuple[int, int] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    return entry.st_dev, entry.st_ino


def _sha256_published_path(
    path: Path,
    *,
    label: str,
    dependencies: MigrationDependencies,
) -> tuple[str, int, tuple[int, int]]:
    identity = _published_identity(path)
    if identity is None:
        raise ValueError(f"{label} is missing: {path}")
    digest, size = dependencies.sha256_verified_published_file(path, identity)
    return digest, size, identity


_EXACT_PUBLICATION_PREFIX = ".tedd-migration-publish-"


def _exact_publication_stage(destination: Path, payload_sha256: str) -> Path:
    destination_binding = hashlib.sha256(
        os.fsencode(os.path.normcase(str(destination.expanduser().resolve(strict=False))))
    ).hexdigest()[:24]
    return destination.with_name(
        f"{_EXACT_PUBLICATION_PREFIX}{destination_binding}-{payload_sha256}"
    )


def _exact_publication_stage_digest(destination: Path, staged: Path) -> str | None:
    prefix = _exact_publication_stage(destination, "0" * 64).name[:-64]
    if staged.parent != destination.parent or not staged.name.startswith(prefix):
        return None
    digest = staged.name[len(prefix) :]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _exact_publication_stages(destination: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    try:
        for candidate in destination.parent.iterdir():
            digest = _exact_publication_stage_digest(destination, candidate)
            if digest is not None:
                candidates.append((candidate, digest))
                if len(candidates) > 64:
                    raise ValueError("Too many staged migration publication artifacts")
    except FileNotFoundError:
        return candidates
    return candidates


def _finalize_exact_publication_if_linked(
    destination: Path,
    *,
    expected_sha256: str | None,
    label: str,
    finalize_publication_link: FinalizePublicationLink,
) -> None:
    candidates = _exact_publication_stages(destination)
    destination_identity = _published_identity(destination)
    if destination_identity is None:
        return
    linked_candidates = [
        (staged, staged_digest)
        for staged, staged_digest in candidates
        if _published_identity(staged) == destination_identity
    ]
    if not linked_candidates:
        return
    if len(linked_candidates) != 1:
        raise ValueError(f"Ambiguous staged {label} publication evidence")
    staged, staged_digest = linked_candidates[0]
    if expected_sha256 is not None and staged_digest != expected_sha256:
        raise ValueError(f"Staged {label} is bound to different content")
    staged_identity = _published_identity(staged)
    if staged_identity is None or staged_identity != destination_identity:
        raise ValueError(f"Staged {label} does not match its published destination")
    finalize_publication_link(
        staged,
        destination,
        destination_identity,
        staged_digest,
    )


def _publish_exact_bytes(
    destination: Path,
    payload: bytes,
    *,
    label: str,
    dependencies: MigrationDependencies,
) -> None:
    payload_sha256 = dependencies.sha256_bytes(payload)
    _finalize_exact_publication_if_linked(
        destination,
        expected_sha256=payload_sha256,
        label=label,
        finalize_publication_link=dependencies.finalize_publication_link,
    )
    identity = _published_identity(destination)
    if identity is not None:
        if (
            dependencies.read_verified_published_bytes(
                destination,
                identity,
                maximum_bytes=len(payload),
            )
            != payload
        ):
            raise ValueError(f"Existing {label} differs: {destination}")
        return
    staged = _exact_publication_stage(destination, payload_sha256)
    created_stage = False
    try:
        staged_identity = _published_identity(staged)
        if staged_identity is None:
            dependencies.atomic_bytes(staged, payload)
            created_stage = True
        elif (
            dependencies.read_verified_published_bytes(
                staged,
                staged_identity,
                maximum_bytes=len(payload),
            )
            != payload
        ):
            raise ValueError(f"Staged {label} differs: {staged}")
        try:
            published_identity = dependencies.publish_staged_file(
                staged,
                destination,
                overwrite=False,
            )
        except FileExistsError:
            _finalize_exact_publication_if_linked(
                destination,
                expected_sha256=payload_sha256,
                label=label,
                finalize_publication_link=dependencies.finalize_publication_link,
            )
            competing_identity = _published_identity(destination)
            if competing_identity is None:
                raise ValueError(f"Competing {label} disappeared: {destination}") from None
            if (
                dependencies.read_verified_published_bytes(
                    destination,
                    competing_identity,
                    maximum_bytes=len(payload),
                )
                != payload
            ):
                raise ValueError(f"Existing {label} differs: {destination}") from None
            return
        dependencies.fsync_parent(destination.parent)
        if (
            dependencies.read_verified_published_bytes(
                destination,
                published_identity,
                maximum_bytes=len(payload),
            )
            != payload
        ):
            raise ValueError(f"Published {label} differs: {destination}")
    finally:
        staged_identity = _published_identity(staged)
        destination_identity = _published_identity(destination)
        if (
            created_stage
            and staged_identity is not None
            and (destination_identity is None or staged_identity != destination_identity)
        ):
            staged.unlink()
            dependencies.fsync_parent(staged.parent)


def _validate_rollback_graph(
    source_graph: JSONMap,
    rollback_graph: JSONMap,
    dependencies: MigrationDependencies,
) -> None:
    rollback_validation = dependencies.validate_legacy(rollback_graph)
    if not rollback_validation["valid"]:
        raise ValueError(
            "Generated v3 rollback export is invalid: " + "; ".join(rollback_validation["errors"])
        )
    if not collections_equal(source_graph, rollback_graph, dependencies):
        raise ValueError("v4 current records differ from the exact v3 source")
    source_journal = source_graph.get("journal", [])
    rollback_journal = rollback_graph.get("journal", [])
    rollback_tail = rollback_journal[-1] if isinstance(rollback_journal, list) else None
    if (
        not isinstance(source_journal, list)
        or not isinstance(rollback_journal, list)
        or rollback_journal[: len(source_journal)] != source_journal
        or len(rollback_journal) != len(source_journal) + 1
        or not isinstance(rollback_tail, Mapping)
        or rollback_tail.get("action") != "migrate_storage"
    ):
        raise ValueError(
            "v3 rollback journal does not preserve the source prefix plus one migration entry"
        )


def _stage_and_verify(
    graph: JSONMap,
    plan: UpgradePlan,
    envelope: UpgradeEnvelope,
    paths: MigrationPaths,
    dependencies: MigrationDependencies,
) -> StoreValidationResult:
    store: MigrationStore | None = None
    try:
        store = dependencies.import_v3_graph(
            graph,
            paths.staging,
            plan["source_sha256"],
            producer_agent_id=envelope.producer_agent_id,
            task_id=envelope.task_id,
            parent_task_id=envelope.parent_task_id,
            discovery_kind="migration",
        )
        validation = store.validate(full=True)
        if not validation["valid"]:
            raise ValueError(
                "Side-by-side v4 import failed validation: " + "; ".join(validation["errors"])
            )
        rollback_graph = store.export_current_graph(schema_version="v3")
        _validate_rollback_graph(graph, rollback_graph, dependencies)
        store.backup(paths.verification_backup)
        plan_key = paths.staging_key.read_bytes()
        with dependencies.open_store(
            paths.verification_backup,
            readonly=True,
            plan_hmac_key=plan_key,
        ) as restored:
            restore_validation = restored.validate(full=True)
        if not restore_validation["valid"]:
            raise ValueError("Online backup/restore verification failed")
        _publish_exact_bytes(
            paths.rollback_export,
            (json.dumps(rollback_graph, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            label="migration rollback export",
            dependencies=dependencies,
        )
        store.checkpoint("TRUNCATE")
        store.close()
        store = None
        return restore_validation
    finally:
        if store is not None:
            store.close()


def _inspect_staged_target(
    paths: MigrationPaths,
    dependencies: MigrationDependencies,
) -> tuple[str, str, StoreValidationResult, StoreState]:
    staged_identity = _published_identity(paths.staging)
    staged_key_identity = _published_identity(paths.staging_key)
    if staged_identity is None or staged_key_identity is None:
        raise ValueError("Verified migration staging files are incomplete")
    staged_digest, _ = dependencies.sha256_verified_published_file(
        paths.staging,
        staged_identity,
    )
    staged_key_digest, _ = dependencies.sha256_verified_published_file(
        paths.staging_key,
        staged_key_identity,
    )
    with dependencies.open_store(paths.staging, readonly=True) as staged:
        validation = staged.validate(full=True)
        state = staged.state()
    if not validation["valid"]:
        raise ValueError("Verified migration staging database failed final validation")
    return staged_digest, staged_key_digest, validation, state


def _activated_config_document(
    *,
    target: Path,
    current_config: JSONMap,
    paths: MigrationPaths,
    activation_timestamp: str,
) -> str:
    activated_config: JSONMap = dict(current_config)
    activated_config.update(
        {
            "store_path": str(target),
            "canonical_store_format": "sqlite_v4_wal",
            "legacy_v3_backup_path": str(paths.backup),
            "v4_migration_manifest": str(paths.migration_manifest),
            "updated_at": activation_timestamp,
        }
    )
    return json.dumps(activated_config, indent=2, sort_keys=True) + "\n"


def _source_tombstone_document(
    *,
    target: Path,
    plan: UpgradePlan,
    paths: MigrationPaths,
    activation_timestamp: str,
) -> str:
    tombstone: JSONMap = {
        "schema_version": "v4_redirect",
        "canonical_store": str(target),
        "migration_manifest": str(paths.migration_manifest),
        "recoverable_v3_backup": str(paths.backup),
        "source_sha256": plan["source_sha256"],
        "migrated_at": activation_timestamp,
    }
    return json.dumps(tombstone, indent=2, sort_keys=True) + "\n"


def _manifest_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _manifest_text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Migration cutover manifest field must be text: {key}")
    return result


def _manifest_integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise ValueError(f"Migration cutover manifest field must be a nonnegative integer: {key}")
    return result


def _manifest_digest(value: Mapping[str, object], key: str) -> str:
    digest = _manifest_text(value, key)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Migration cutover manifest field must be a SHA-256 digest: {key}")
    return digest


def _manifest_validation(value: object, *, label: str) -> StoreValidationResult:
    mapping = _manifest_mapping(value, label=label)
    valid = mapping.get("valid")
    errors = mapping.get("errors")
    if (
        not isinstance(valid, bool)
        or not isinstance(errors, list)
        or not all(isinstance(item, str) for item in errors)
    ):
        raise ValueError(f"{label} is malformed")
    return cast(StoreValidationResult, dict(mapping))


def _manifest_state(value: object) -> StoreState:
    mapping = _manifest_mapping(value, label="Target state receipt")
    watermark = mapping.get("watermark")
    store_id = mapping.get("store_id")
    if (
        not isinstance(watermark, int)
        or isinstance(watermark, bool)
        or watermark < 0
        or not isinstance(store_id, str)
        or not store_id
    ):
        raise ValueError("Target state receipt is malformed")
    return cast(StoreState, dict(mapping))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Migration cutover manifest contains a duplicate key: {key}")
        result[key] = value
    return result


def _read_cutover_manifest(
    path: Path,
    *,
    read_verified_published_bytes: ReadVerifiedPublishedBytes,
    finalize_publication_link: FinalizePublicationLink,
) -> Mapping[str, object]:
    _finalize_exact_publication_if_linked(
        path,
        expected_sha256=None,
        label="migration cutover manifest",
        finalize_publication_link=finalize_publication_link,
    )
    entry = path.lstat()
    payload = read_verified_published_bytes(
        path,
        (entry.st_dev, entry.st_ino),
        maximum_bytes=1024 * 1024,
    )
    decoded: object = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    _require_json_value(decoded, label="Migration cutover manifest")
    canonical = (json.dumps(decoded, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if canonical != payload:
        raise ValueError("Migration cutover manifest bytes are not canonical")
    manifest = _manifest_mapping(decoded, label="Migration cutover manifest")
    if (
        manifest.get("format") != "knowledge-graph-v4-migration-manifest"
        or manifest.get("cutover_protocol") != "verified-roll-forward-v1"
    ):
        raise ValueError("Migration cutover manifest format is incompatible")
    return manifest


def migration_recovery_source(
    manifest_path: Path,
    *,
    config_path: Path,
    target_path: Path,
    expected_source_sha256: str,
    expected_plan_fingerprint: str,
    transform_version: str,
    read_verified_published_bytes: ReadVerifiedPublishedBytes,
    finalize_publication_link: FinalizePublicationLink,
) -> Path:
    """Return the manifest-bound v3 source for a narrowly authorized CLI retry.

    The caller must first resolve ``manifest_path`` through the normal private-state
    path policy. The full migration service independently revalidates the backup,
    plan, paths, provenance, and every published artifact before changing state.
    """

    manifest = _read_cutover_manifest(
        manifest_path,
        read_verified_published_bytes=read_verified_published_bytes,
        finalize_publication_link=finalize_publication_link,
    )
    if (
        _manifest_digest(manifest, "source_sha256") != expected_source_sha256
        or _manifest_digest(manifest, "plan_fingerprint") != expected_plan_fingerprint
        or _manifest_text(manifest, "transform_version") != transform_version
    ):
        raise ValueError("Migration recovery arguments differ from the durable cutover intent")
    configured_target = target_path.expanduser().resolve()
    manifest_target = Path(_manifest_text(manifest, "target_path")).expanduser().resolve()
    manifest_config = Path(_manifest_text(manifest, "config_path")).expanduser().resolve()
    if (
        manifest_target != configured_target
        or manifest_config != config_path.expanduser().resolve()
    ):
        raise ValueError("Migration recovery manifest is not bound to this configuration and store")
    source = Path(_manifest_text(manifest, "source_path")).expanduser().resolve()
    expected_manifest = _recovery_manifest_path(source, expected_plan_fingerprint)
    if (
        expected_manifest is None
        or manifest_path.expanduser().resolve() != expected_manifest.resolve()
    ):
        raise ValueError("Migration recovery manifest is not at its deterministic reviewed path")
    return source


def _write_cutover_manifest(
    *,
    source: Path,
    target: Path,
    config_path: Path,
    current_config: JSONMap,
    plan: UpgradePlan,
    fingerprint: str,
    envelope: UpgradeEnvelope,
    paths: MigrationPaths,
    target_sha256: str,
    target_key_sha256: str,
    installed_validation: StoreValidationResult,
    installed_state: StoreState,
    restore_validation: StoreValidationResult,
    dependencies: MigrationDependencies,
) -> None:
    activation_timestamp = dependencies.utc_now()
    activated_config = _activated_config_document(
        target=target,
        current_config=current_config,
        paths=paths,
        activation_timestamp=activation_timestamp,
    )
    source_tombstone = _source_tombstone_document(
        target=target,
        plan=plan,
        paths=paths,
        activation_timestamp=activation_timestamp,
    )
    manifest: JSONMap = {
        "format": "knowledge-graph-v4-migration-manifest",
        "cutover_protocol": "verified-roll-forward-v1",
        "transform_version": dependencies.transform_version,
        "source_path": str(source),
        "source_backup": str(paths.backup),
        "source_sha256": plan["source_sha256"],
        "source_watermark": plan["source_watermark"],
        "target_path": str(target),
        "target_key_path": str(paths.target_key),
        "staging_path": str(paths.staging),
        "staging_key_path": str(paths.staging_key),
        "verification_backup": str(paths.verification_backup),
        "verification_manifest": str(paths.verification_manifest),
        "target_sha256_at_cutover": target_sha256,
        "target_key_sha256_at_cutover": target_key_sha256,
        "target_watermark": installed_state["watermark"],
        "target_store_id": installed_state["store_id"],
        "target_state": installed_state,
        "rollback_export": str(paths.rollback_export),
        "rollback_export_sha256": _sha256_published_path(
            paths.rollback_export,
            label="Migration rollback export",
            dependencies=dependencies,
        )[0],
        "plan_fingerprint": fingerprint,
        "plan": dict(plan),
        "config_path": str(config_path),
        "current_config_sha256": dependencies.sha256_file(config_path),
        "activated_config_sha256": dependencies.sha256_bytes(activated_config.encode("utf-8")),
        "source_tombstone_sha256": dependencies.sha256_bytes(source_tombstone.encode("utf-8")),
        "activation_timestamp": activation_timestamp,
        "producer": {
            "agent_id": envelope.producer_agent_id,
            "task_id": envelope.task_id,
            "parent_task_id": envelope.parent_task_id,
            "based_on_watermark": envelope.based_on_watermark,
            "producer_run_id": envelope.producer_run_id,
            "discovery_kind": envelope.discovery_kind,
        },
        "backup_restore_validation": restore_validation,
        "target_validation": installed_validation,
    }
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    _publish_exact_bytes(
        paths.migration_manifest,
        content.encode("utf-8"),
        label="migration cutover manifest",
        dependencies=dependencies,
    )


def _load_prepared_cutover(
    request: UpgradeRequest,
    *,
    source: Path,
    target: Path,
    config_path: Path,
    dependencies: MigrationDependencies,
) -> tuple[PreparedCutover, JSONMap]:
    fingerprint = request.expected_plan_fingerprint
    manifest_path = _recovery_manifest_path(source, fingerprint)
    if manifest_path is None or _published_identity(manifest_path) is None:
        raise ValueError("No reviewed migration cutover manifest is available for recovery")
    manifest = _read_cutover_manifest(
        manifest_path,
        read_verified_published_bytes=dependencies.read_verified_published_bytes,
        finalize_publication_link=dependencies.finalize_publication_link,
    )
    manifest_fingerprint = _manifest_digest(manifest, "plan_fingerprint")
    source_sha256 = _manifest_digest(manifest, "source_sha256")
    if fingerprint != manifest_fingerprint or request.expected_source_sha256 != source_sha256:
        raise ValueError("Migration recovery does not match the reviewed digest and plan")
    if (
        _manifest_text(manifest, "source_path") != str(source)
        or _manifest_text(manifest, "target_path") != str(target)
        or _manifest_text(manifest, "config_path") != str(config_path)
        or _manifest_text(manifest, "transform_version") != dependencies.transform_version
    ):
        raise ValueError("Migration cutover manifest is bound to different source paths or code")

    plan_mapping = _manifest_mapping(manifest.get("plan"), label="Migration cutover plan")
    plan = UpgradePlan(dict(plan_mapping))
    if dependencies.stable_hash(plan, 64) != manifest_fingerprint:
        raise ValueError("Migration cutover plan fingerprint is invalid")
    paths = _manifest_paths(
        request,
        source=source,
        target=target,
        plan=plan,
        fingerprint=manifest_fingerprint,
        manifest=manifest,
    )
    _require_distinct_migration_paths(
        source=source,
        target=target,
        config_path=config_path,
        paths=paths,
    )
    if paths.migration_manifest != manifest_path:
        raise ValueError("Migration cutover manifest path is inconsistent")

    backup_digest, backup_size, backup_identity = _sha256_published_path(
        paths.backup,
        label="Recoverable v3 migration backup",
        dependencies=dependencies,
    )
    if backup_digest != source_sha256:
        raise ValueError("Recoverable v3 backup differs from the reviewed migration plan")
    backup_payload = dependencies.read_verified_published_bytes(
        paths.backup,
        backup_identity,
        maximum_bytes=backup_size,
    )
    graph, backup_plan, backup_fingerprint = upgrade_plan(
        source,
        target,
        backup_payload,
        dependencies,
    )
    if backup_fingerprint != manifest_fingerprint or dependencies.canonical_json(
        backup_plan
    ) != dependencies.canonical_json(plan):
        raise ValueError("Recoverable v3 backup differs from the reviewed migration plan")
    rollback_digest, _, _ = _sha256_published_path(
        paths.rollback_export,
        label="v3 rollback export",
        dependencies=dependencies,
    )
    if rollback_digest != _manifest_digest(manifest, "rollback_export_sha256"):
        raise ValueError("v3 rollback export differs from the reviewed migration plan")

    envelope = request.envelope
    if envelope is None:
        raise ValueError("Migration recovery requires the original producer/task provenance")
    producer = _manifest_mapping(manifest.get("producer"), label="Migration producer binding")
    expected_producer: tuple[object, ...] = (
        producer.get("agent_id"),
        producer.get("task_id"),
        producer.get("parent_task_id"),
        producer.get("based_on_watermark"),
        producer.get("producer_run_id"),
        producer.get("discovery_kind"),
    )
    actual_producer: tuple[object, ...] = (
        envelope.producer_agent_id,
        envelope.task_id,
        envelope.parent_task_id,
        envelope.based_on_watermark,
        envelope.producer_run_id,
        envelope.discovery_kind,
    )
    if (
        expected_producer != actual_producer
        or envelope.based_on_watermark != plan["source_watermark"]
    ):
        raise ValueError("Migration recovery producer/task provenance differs from cutover intent")

    installed_state = _manifest_state(manifest.get("target_state"))
    installed_validation = _manifest_validation(
        manifest.get("target_validation"), label="Target validation receipt"
    )
    restore_validation = _manifest_validation(
        manifest.get("backup_restore_validation"), label="Backup validation receipt"
    )
    if not installed_validation["valid"] or not restore_validation["valid"]:
        raise ValueError("Migration cutover manifest contains a failed validation receipt")
    if (
        _manifest_integer(manifest, "source_watermark") != plan["source_watermark"]
        or installed_state["watermark"] != plan["target_watermark"]
        or _manifest_integer(manifest, "target_watermark") != installed_state["watermark"]
        or _manifest_text(manifest, "target_store_id") != installed_state["store_id"]
    ):
        raise ValueError("Migration cutover watermarks differ from the reviewed plan")
    prepared = PreparedCutover(
        plan=plan,
        paths=paths,
        fingerprint=manifest_fingerprint,
        activation_timestamp=_manifest_text(manifest, "activation_timestamp"),
        current_config_sha256=_manifest_digest(manifest, "current_config_sha256"),
        activated_config_sha256=_manifest_digest(manifest, "activated_config_sha256"),
        source_tombstone_sha256=_manifest_digest(manifest, "source_tombstone_sha256"),
        target_sha256=_manifest_digest(manifest, "target_sha256_at_cutover"),
        target_key_sha256=_manifest_digest(manifest, "target_key_sha256_at_cutover"),
        installed_validation=installed_validation,
        installed_state=installed_state,
        restore_validation=restore_validation,
    )
    return prepared, graph


def _publish_staged_component(
    staged: Path,
    destination: Path,
    expected_sha256: str,
    *,
    label: str,
    dependencies: MigrationDependencies,
) -> None:
    staged_identity = _published_identity(staged)
    destination_identity = _published_identity(destination)
    staged_exists = staged_identity is not None
    destination_exists = destination_identity is not None
    if staged_exists and destination_exists:
        if staged_identity != destination_identity or destination_identity is None:
            raise ValueError(f"Migration {label} exists in conflicting locations")
        dependencies.finalize_publication_link(
            staged,
            destination,
            destination_identity,
            expected_sha256,
        )
        staged_identity = None
        destination_identity = _published_identity(destination)
        destination_exists = destination_identity is not None
    if destination_exists:
        if destination_identity is None:
            raise AssertionError("Published destination identity unexpectedly disappeared")
        existing_digest, _ = dependencies.sha256_verified_published_file(
            destination,
            destination_identity,
        )
        if existing_digest != expected_sha256:
            raise ValueError(f"Published migration {label} differs from cutover intent")
        return
    if not staged_exists:
        raise ValueError(f"Migration {label} is missing from staged and published locations")
    if staged_identity is None:
        raise AssertionError("Staged migration identity unexpectedly disappeared")
    staged_digest, _ = dependencies.sha256_verified_published_file(
        staged,
        staged_identity,
    )
    if staged_digest != expected_sha256:
        raise ValueError(f"Staged migration {label} differs from cutover intent")
    published_identity = dependencies.publish_staged_file(staged, destination, overwrite=False)
    published_digest, _ = dependencies.sha256_verified_published_file(
        destination,
        published_identity,
    )
    dependencies.fsync_parent(destination.parent)
    if published_digest != expected_sha256:
        raise ValueError(f"Published migration {label} failed post-publication verification")


def _publish_config_activation(
    *,
    source: Path,
    target: Path,
    config_path: Path,
    prepared: PreparedCutover,
    dependencies: MigrationDependencies,
) -> None:
    current_digest = dependencies.sha256_file(config_path)
    if current_digest == prepared.activated_config_sha256:
        return
    if current_digest != prepared.current_config_sha256:
        raise ValueError("Configuration is neither the reviewed pre-cutover nor activated version")
    current_config = dependencies.load_config(config_path)
    configured = Path(str(current_config.get("store_path", ""))).expanduser().resolve()
    if configured != source:
        raise ValueError("Reviewed pre-cutover configuration no longer points at the v3 source")
    activated_config = _activated_config_document(
        target=target,
        current_config=current_config,
        paths=prepared.paths,
        activation_timestamp=prepared.activation_timestamp,
    )
    if (
        dependencies.sha256_bytes(activated_config.encode("utf-8"))
        != prepared.activated_config_sha256
    ):
        raise ValueError("Activated configuration cannot be reproduced from cutover intent")
    dependencies.atomic_write(config_path, activated_config, mode=0o600)
    if dependencies.sha256_file(config_path) != prepared.activated_config_sha256:
        raise ValueError("Activated configuration failed post-publication verification")


def _publish_source_tombstone(
    *,
    source: Path,
    target: Path,
    prepared: PreparedCutover,
    dependencies: MigrationDependencies,
) -> None:
    current_digest = dependencies.sha256_file(source)
    if current_digest == prepared.source_tombstone_sha256:
        return
    if current_digest != prepared.plan["source_sha256"]:
        raise ValueError("v3 source is neither the reviewed graph nor its cutover tombstone")
    source_tombstone = _source_tombstone_document(
        target=target,
        plan=prepared.plan,
        paths=prepared.paths,
        activation_timestamp=prepared.activation_timestamp,
    )
    if (
        dependencies.sha256_bytes(source_tombstone.encode("utf-8"))
        != prepared.source_tombstone_sha256
    ):
        raise ValueError("Source tombstone cannot be reproduced from cutover intent")
    dependencies.atomic_write(source, source_tombstone, mode=0o600)
    if dependencies.sha256_file(source) != prepared.source_tombstone_sha256:
        raise ValueError("Source tombstone failed post-publication verification")


def _complete_cutover(
    *,
    source: Path,
    target: Path,
    config_path: Path,
    prepared: PreparedCutover,
    dependencies: MigrationDependencies,
) -> None:
    # The database is safe to publish while configuration still points to v3.
    # Publishing its key second avoids an apparently complete key-only target.
    _publish_staged_component(
        prepared.paths.staging,
        target,
        prepared.target_sha256,
        label="database",
        dependencies=dependencies,
    )
    _publish_staged_component(
        prepared.paths.staging_key,
        prepared.paths.target_key,
        prepared.target_key_sha256,
        label="plan key",
        dependencies=dependencies,
    )
    with dependencies.open_store(target, readonly=True) as installed:
        validation = installed.validate(full=True)
        state = installed.state()
    if (
        not validation["valid"]
        or state["watermark"] != prepared.installed_state["watermark"]
        or state["store_id"] != prepared.installed_state["store_id"]
    ):
        raise ValueError("Published v4 target differs from the verified cutover state")

    _publish_config_activation(
        source=source,
        target=target,
        config_path=config_path,
        prepared=prepared,
        dependencies=dependencies,
    )
    _publish_source_tombstone(
        source=source,
        target=target,
        prepared=prepared,
        dependencies=dependencies,
    )


def _cleanup_staging(paths: MigrationPaths) -> None:
    for candidate in (
        paths.staging,
        Path(f"{paths.staging}-wal"),
        Path(f"{paths.staging}-shm"),
        paths.staging_key,
        paths.verification_backup,
        paths.verification_manifest,
    ):
        if candidate.exists():
            candidate.unlink()


def _completed_result(
    graph: JSONMap,
    prepared: PreparedCutover,
    *,
    target: Path,
    recovered: bool,
    dependencies: MigrationDependencies,
) -> JSONMap:
    return {
        "status": "upgraded_and_activated",
        "source_sha256": prepared.plan["source_sha256"],
        "plan_fingerprint": prepared.fingerprint,
        "plan": prepared.plan,
        "source_validation": dependencies.validate_legacy(graph),
        "target_exists": target.exists(),
        "target": str(target),
        "source_backup": str(prepared.paths.backup),
        "source_tombstone": str(prepared.plan["source_path"]),
        "rollback_export": str(prepared.paths.rollback_export),
        "migration_manifest": str(prepared.paths.migration_manifest),
        "config_switched": True,
        "cutover_recovered": recovered,
        "target_validation": prepared.installed_validation,
        "backup_restore_validation": prepared.restore_validation,
        "target_state": prepared.installed_state,
    }


def _locked_recovery(
    request: UpgradeRequest,
    *,
    source: Path,
    target: Path,
    config_path: Path,
    dependencies: MigrationDependencies,
) -> JSONMap:
    prepared, graph = _load_prepared_cutover(
        request,
        source=source,
        target=target,
        config_path=config_path,
        dependencies=dependencies,
    )
    _complete_cutover(
        source=source,
        target=target,
        config_path=config_path,
        prepared=prepared,
        dependencies=dependencies,
    )
    _cleanup_staging(prepared.paths)
    return _completed_result(
        graph,
        prepared,
        target=target,
        recovered=True,
        dependencies=dependencies,
    )


def upgrade_v3_to_v4(
    request: UpgradeRequest,
    *,
    config_path: Path,
    lock_timeout: float,
    dependencies: MigrationDependencies,
) -> JSONMap:
    """Plan or atomically activate an exact, recoverable side-by-side upgrade."""

    source = request.source_path
    target = request.target_path
    _require_distinct_paths(
        {
            "v3 source": source,
            "v4 target": target,
            "configuration": config_path,
        }
    )
    recovery_manifest = _recovery_manifest_path(source, request.expected_plan_fingerprint)
    if (
        not request.dry_run
        and recovery_manifest is not None
        and _published_identity(recovery_manifest) is not None
    ):
        with contextlib.ExitStack() as locks:
            locks.enter_context(dependencies.store_lock(config_path, lock_timeout))
            locks.enter_context(dependencies.store_lock(source, lock_timeout))
            locks.enter_context(dependencies.store_lock(target, lock_timeout))
            return _locked_recovery(
                request,
                source=source,
                target=target,
                config_path=config_path,
                dependencies=dependencies,
            )
    if not source.is_file():
        raise ValueError(f"Canonical v3 source does not exist: {source}")
    payload = source.read_bytes()
    graph, plan, fingerprint = upgrade_plan(source, target, payload, dependencies)
    base_result: JSONMap = {
        "status": "would_upgrade" if request.dry_run else "upgraded",
        "source_sha256": plan["source_sha256"],
        "plan_fingerprint": fingerprint,
        "plan": plan,
        "source_validation": dependencies.validate_legacy(graph),
        "target_exists": target.exists(),
    }
    if request.dry_run:
        if target.exists():
            base_result["status"] = "blocked_target_exists"
        return base_result

    envelope = _require_apply_envelope(request, target, plan, fingerprint)
    paths = _migration_paths(request, source, target, plan, fingerprint, dependencies)
    _require_distinct_migration_paths(
        source=source,
        target=target,
        config_path=config_path,
        paths=paths,
    )

    # Configuration is always locked before either canonical store. This is the
    # shared order used by fresh initialization and migration cutover.
    with contextlib.ExitStack() as locks:
        locks.enter_context(dependencies.store_lock(config_path, lock_timeout))
        locks.enter_context(dependencies.store_lock(source, lock_timeout))
        locks.enter_context(dependencies.store_lock(target, lock_timeout))
        if _published_identity(paths.migration_manifest) is not None:
            return _locked_recovery(
                request,
                source=source,
                target=target,
                config_path=config_path,
                dependencies=dependencies,
            )
        if target.exists() or paths.target_key.exists():
            raise FileExistsError("v4 target appeared while waiting for migration locks")
        locked_payload, locked_graph, locked_plan, current_config = _load_locked_upgrade(
            source,
            target,
            config_path,
            plan,
            fingerprint,
            dependencies,
        )
        _ensure_source_backup(paths.backup, locked_payload, dependencies)
        try:
            restore_validation = _stage_and_verify(
                locked_graph,
                locked_plan,
                envelope,
                paths,
                dependencies,
            )
            (
                installed_digest,
                target_key_digest,
                installed_validation,
                installed_state,
            ) = _inspect_staged_target(
                paths,
                dependencies,
            )
            _write_cutover_manifest(
                source=source,
                target=target,
                config_path=config_path,
                current_config=current_config,
                plan=locked_plan,
                fingerprint=fingerprint,
                envelope=envelope,
                paths=paths,
                target_sha256=installed_digest,
                target_key_sha256=target_key_digest,
                installed_validation=installed_validation,
                installed_state=installed_state,
                restore_validation=restore_validation,
                dependencies=dependencies,
            )
            prepared, recovery_graph = _load_prepared_cutover(
                request,
                source=source,
                target=target,
                config_path=config_path,
                dependencies=dependencies,
            )
            _complete_cutover(
                source=source,
                target=target,
                config_path=config_path,
                prepared=prepared,
                dependencies=dependencies,
            )
        finally:
            if not paths.migration_manifest.exists():
                _cleanup_staging(paths)
        _cleanup_staging(paths)

    return _completed_result(
        recovery_graph,
        prepared,
        target=target,
        recovered=False,
        dependencies=dependencies,
    )
