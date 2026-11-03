"""Projection and migration command handlers for legacy and v4 stores."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import cast

from .legacy_dispatch_contracts import (
    CommandResponse,
    JSONMap,
    LegacyDispatchDependencies,
    V4Bindings,
)


def _object_records(value: object, *, label: str) -> list[JSONMap]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return cast(list[JSONMap], value)


def v4_upgrade_response(
    args: argparse.Namespace,
    *,
    config_path: Path,
    store_path: Path,
    bindings: V4Bindings,
    dependencies: LegacyDispatchDependencies,
    recovery_source: Path | None = None,
) -> CommandResponse:
    source_path = recovery_source or store_path
    target_path = (
        store_path
        if recovery_source is not None
        else dependencies.secure_state_path(
            store_path.with_name("graph.sqlite3"),
            "Canonical v4 target",
        )
    )
    result = bindings.handle_command(
        args,
        db_path=target_path,
        v3_store_path=source_path,
        capture_handler=bindings.capture,
        upgrade_handler=lambda request: bindings.upgrade(
            request,
            config_path=config_path,
            lock_timeout=args.lock_timeout,
        ),
        review_sanitizer=bindings.sanitize_review_safe,
    )
    return CommandResponse(result, ensure_ascii=False)


def v4_project_response(
    args: argparse.Namespace,
    *,
    config: JSONMap,
    store_path: Path,
    bindings: V4Bindings,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    vault, root_name = dependencies.resolve_projection(args, config)
    result = bindings.project(
        store_path,
        vault=vault,
        root_name=root_name,
        mode=args.mode,
        manifest_key=dependencies.normalize_space(config.get("projection_manifest_key")),
        root_id=(dependencies.normalize_space(config.get("projection_root_id")) or None),
        timeout_seconds=args.lock_timeout,
        force_current_digest=args.force_current_digest,
    )
    return CommandResponse(result, ensure_ascii=False)


def project_response(
    args: argparse.Namespace,
    config: JSONMap,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    if not store_path.is_file():
        raise ValueError(
            f"Canonical store does not exist: {store_path}. Refusing to replace a "
            "projection with an empty graph."
        )
    vault, root_name = dependencies.resolve_projection(args, config)
    result = dependencies.project_latest_from_store(
        store_path,
        vault,
        root_name,
        args.mode,
        dependencies.normalize_space(config.get("projection_manifest_key")),
        dependencies.normalize_space(config.get("projection_root_id")) or None,
        args.lock_timeout,
        args.force_current_digest,
    )
    return CommandResponse(result)


def migrate_response(
    args: argparse.Namespace,
    config: JSONMap,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    if not store_path.is_file():
        raise ValueError(f"Canonical store does not exist: {store_path}")
    with dependencies.store_lock(store_path, args.lock_timeout):
        payload = store_path.read_bytes()
        canonical_digest = hashlib.sha256(payload).hexdigest()
        raw_graph = json.loads(payload.decode("utf-8"))
        if not isinstance(raw_graph, dict):
            raise ValueError("Canonical graph must be a JSON object")
        stored_version = dependencies.normalize_space(raw_graph.get("schema_version"))
        if stored_version == dependencies.graph_schema_version:
            graph = raw_graph
            validation = dependencies.validate_graph(graph)
            migration_result: JSONMap = {
                "status": "already_current",
                "stored_schema_version": stored_version,
                "canonical_sha256": canonical_digest,
                "graph_watermark": dependencies.committed_watermark(graph),
                "validation": validation,
            }
            migration_committed = False
        elif stored_version in dependencies.legacy_graph_schema_versions:
            graph = dependencies.migrate_graph_v2_to_v3(raw_graph)
            validation = dependencies.validate_graph(graph)
            if not validation["valid"]:
                raise ValueError(
                    "Migration plan failed validation: " + "; ".join(validation["errors"])
                )
            migrated_core_sha256 = dependencies.stable_hash(graph, 64)
            plan_fingerprint = dependencies.stable_hash(
                {
                    "from": stored_version,
                    "to": dependencies.graph_schema_version,
                    "canonical_sha256": canonical_digest,
                    "migration_transform_version": dependencies.migration_transform_version,
                    "migrated_core_sha256": migrated_core_sha256,
                },
                32,
            )
            migration_result = {
                "status": "would_migrate" if args.dry_run else "migrated",
                "from_schema_version": stored_version,
                "to_schema_version": dependencies.graph_schema_version,
                "canonical_sha256": canonical_digest,
                "migration_transform_version": dependencies.migration_transform_version,
                "migrated_core_sha256": migrated_core_sha256,
                "migration_fingerprint": plan_fingerprint,
                "source_watermark": dependencies.committed_watermark(graph),
                "target_watermark": dependencies.committed_watermark(graph) + 1,
                "counts": validation["counts"],
                "validation": validation,
            }
            migration_committed = False
            if not args.dry_run:
                expected_digest = dependencies.normalize_space(args.expect_sha256)
                expected_migration = dependencies.normalize_space(args.expect_migration_fingerprint)
                if not expected_digest or not expected_migration:
                    raise ValueError(
                        "Applying a migration requires --expect-sha256 and "
                        "--expect-migration-fingerprint from migrate --dry-run"
                    )
                if not hmac.compare_digest(expected_digest, canonical_digest):
                    raise ValueError("Canonical graph changed after the reviewed migration dry run")
                if not hmac.compare_digest(expected_migration, plan_fingerprint):
                    raise ValueError("Migration transform differs from the reviewed dry run")
                backup_path = dependencies.migration_backup_path(
                    store_path,
                    stored_version,
                    dependencies.committed_watermark(graph),
                    canonical_digest,
                )
                if backup_path.exists():
                    if backup_path.read_bytes() != payload:
                        raise ValueError(
                            f"Migration backup exists with different contents: {backup_path}"
                        )
                else:
                    dependencies.atomic_write(
                        backup_path,
                        payload.decode("utf-8"),
                        mode=0o600,
                    )
                changed_ids = dependencies.unique_strings(
                    [
                        *[
                            item["id"]
                            for item in _object_records(
                                graph.get("entities"), label="Graph entities"
                            )
                        ],
                        *[
                            item["id"]
                            for item in _object_records(
                                graph.get("claim_surfaces"),
                                label="Graph claim_surfaces",
                            )
                        ],
                        *[
                            item["id"]
                            for item in _object_records(graph.get("claims"), label="Graph claims")
                        ],
                    ]
                )
                dependencies.mutation_journal(
                    graph,
                    "migrate_schema",
                    changed_ids,
                    producer=dependencies.producer_from_args(args),
                    based_on_watermark=args.based_on_watermark,
                    discovery_kind=args.discovery_kind or "migration",
                    details={
                        "from_schema_version": stored_version,
                        "to_schema_version": dependencies.graph_schema_version,
                        "source_sha256": canonical_digest,
                        "migration_transform_version": (dependencies.migration_transform_version),
                        "migrated_core_sha256": migrated_core_sha256,
                        "migration_fingerprint": plan_fingerprint,
                    },
                )
                validation = dependencies.validate_graph(graph)
                if not validation["valid"]:
                    raise ValueError(
                        "Migrated graph failed validation: " + "; ".join(validation["errors"])
                    )
                saved_digest = dependencies.save_graph(store_path, graph)
                migration_result.update(
                    {
                        "backup": str(backup_path),
                        "canonical_sha256_after": saved_digest,
                        "graph_watermark": dependencies.committed_watermark(graph),
                        "validation": validation,
                    }
                )
                migration_committed = True
        else:
            raise ValueError(f"Unsupported graph schema: {stored_version!r}")
    index_result = None
    projection = None
    if migration_committed:
        try:
            index_result = dependencies.ensure_latest_recall_index(
                store_path,
                args.lock_timeout,
            )
        except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
            index_result = {"status": "degraded", "error": str(error)}
        try:
            projection = dependencies.maybe_project_latest(args, config, store_path)
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Migration was committed to {store_path}, but projection failed: {error}. "
                "Fix the projection target and rerun project."
            ) from error
    return CommandResponse(
        {
            **migration_result,
            "dry_run": bool(args.dry_run),
            "index": index_result,
            "projection": projection,
        },
        ensure_ascii=False,
    )
