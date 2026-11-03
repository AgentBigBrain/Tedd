"""Status and read-only command handlers for the legacy v3 graph."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import cast

from .legacy_configuration import OwnerPreviewTransition
from .legacy_dispatch_contracts import (
    CommandResponse,
    JSONMap,
    LegacyDispatchDependencies,
)


def _object_records(value: object, *, label: str) -> list[JSONMap]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return cast(list[JSONMap], value)


def _string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return cast(list[str], value)


def configured_response(
    args: argparse.Namespace,
    config_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    result = dependencies.configure(
        args,
        config_path,
        lock_timeout=args.lock_timeout,
    )
    public_result = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "projection_manifest_key",
            "source_export_recovery_key",
            "source_owner_access_policy",
            "source_owner_view_access_policy",
        }
    }
    public_result["projection_manifest_authentication"] = "configured"
    payload: JSONMap = {"status": "configured", "config": public_result}
    transition = getattr(result, "owner_preview", None)
    if transition is not None:
        if not isinstance(transition, OwnerPreviewTransition):
            raise ValueError("Configuration returned an invalid owner preview transition")
        if transition.policy == "invalidated":
            payload["owner_preview"] = transition.public_mapping()
    return CommandResponse(
        payload,
        ensure_ascii=False,
    )


def doctor_response(
    config_path: Path,
    config: JSONMap,
    store_path: Path,
    privacy_warnings: list[str],
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    graph, canonical_digest, stored_version = dependencies.load_graph_with_digest(store_path)
    validation = dependencies.validate_graph(graph)
    vault = config.get("vault_path")
    root_name = config.get("root_name", "Codex Knowledge")
    projection_status: JSONMap = {"configured": bool(vault)}
    if vault:
        if not isinstance(vault, str) or not isinstance(root_name, str):
            raise ValueError("Projection vault_path and root_name must be strings")
        try:
            projection_status["root"] = str(
                dependencies.ensure_projection_target(Path(vault), root_name)
            )
            projection_status["healthy"] = True
        except ValueError as error:
            projection_status.update({"healthy": False, "error": str(error)})
    payload = {
        "store": str(store_path),
        "store_exists": store_path.exists(),
        "stored_schema_version": stored_version,
        "graph_schema_version": graph.get("schema_version"),
        "migration_required": stored_version != dependencies.graph_schema_version,
        "canonical_sha256": canonical_digest,
        "graph_watermark": dependencies.committed_watermark(graph),
        "config": str(config_path),
        "validation": validation,
        "recall_index": dependencies.index_status(
            graph,
            store_path,
            canonical_digest,
        ),
        "projection": projection_status,
        "privacy_warnings": privacy_warnings,
    }
    return CommandResponse(payload, exit_code=0 if validation["valid"] else 1)


def validate_response(
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    graph, _, stored_version = dependencies.load_graph_with_digest(store_path)
    validation = dependencies.validate_graph(graph)
    result: JSONMap = dict(validation)
    result["stored_schema_version"] = stored_version
    result["migration_required"] = stored_version != dependencies.graph_schema_version
    return CommandResponse(result, exit_code=0 if validation["valid"] else 1)


def index_status_response(
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    graph, canonical_digest, stored_version = dependencies.load_graph_with_digest(store_path)
    return CommandResponse(
        {
            **dependencies.index_status(graph, store_path, canonical_digest),
            "stored_schema_version": stored_version,
        }
    )


def reindex_response(
    args: argparse.Namespace,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    graph, canonical_digest, stored_version = dependencies.load_graph_with_digest(store_path)
    result = dependencies.ensure_recall_index(
        graph,
        store_path,
        canonical_digest,
        args.lock_timeout,
        force=args.force,
    )
    return CommandResponse({**result, "stored_schema_version": stored_version})


def recall_response(
    args: argparse.Namespace,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    query = dependencies.normalize_space(args.query)
    if not query and args.since_watermark is None:
        raise ValueError("Recall requires a query or --since-watermark")
    if args.since_watermark is None and (
        args.through_watermark is not None
        or args.change_cursor is not None
        or args.change_limit != 50
    ):
        raise ValueError(
            "--through-watermark, --change-cursor, and --change-limit require --since-watermark"
        )
    graph, canonical_digest, _ = dependencies.load_graph_with_digest(store_path)
    limit = max(1, min(args.limit, 50))
    max_chars = max(1000, min(args.max_chars, 50000))
    index_result: JSONMap | None = None
    index_warning: str | None = None
    change_feed: JSONMap | None = None

    def feed_candidate_ids(feed: JSONMap) -> set[str]:
        changes = _object_records(feed.get("changes", []), label="Change feed changes")
        record_ids: set[str] = set()
        for change in changes:
            record_id = change.get("record_id")
            if not isinstance(record_id, str):
                raise ValueError("Change feed record_id must be a string")
            record_ids.add(record_id)
        return record_ids

    def build_change_feed(query_candidates: set[str] | None) -> JSONMap | None:
        if args.since_watermark is None:
            return None
        return dependencies.change_feed_since(
            graph,
            args.since_watermark,
            through_watermark=args.through_watermark,
            limit=args.change_limit,
            candidate_ids=query_candidates if query else None,
            mode=args.mode,
            cursor=args.change_cursor,
        )

    if args.no_index:
        validation = dependencies.validate_graph(graph)
        if not validation["valid"]:
            raise ValueError(
                "Refusing to recall from an invalid graph: " + "; ".join(validation["errors"])
            )
        query_candidates = (
            dependencies.scan_query_candidate_ids(graph, query, args.mode) if query else None
        )
        change_feed = build_change_feed(query_candidates)
        result = dependencies.recall_graph(
            graph,
            query,
            limit,
            max_chars,
            args.mode,
            candidate_ids=(feed_candidate_ids(change_feed) if change_feed is not None else None),
            skip_validation=True,
            backend="scan",
            since_watermark=args.since_watermark,
        )
    else:
        try:
            index_result = dependencies.ensure_recall_index(
                graph,
                store_path,
                canonical_digest,
                args.lock_timeout,
            )
            query_candidates = (
                dependencies.indexed_recall_candidates(
                    dependencies.recall_index_path(store_path),
                    query,
                    limit,
                    canonical_digest,
                    exhaustive=args.since_watermark is not None,
                    mode=args.mode,
                )
                if query
                else None
            )
            if query_candidates is not None:
                entity_records = _object_records(graph.get("entities"), label="Graph entities")
                entities = dependencies.index_by_id(entity_records)
                for entity_id in list(query_candidates):
                    entity = entities.get(entity_id, {})
                    query_candidates.update(
                        _string_list(
                            entity.get("evidence_refs", []),
                            label="Entity evidence_refs",
                        )
                    )
            change_feed = build_change_feed(query_candidates)
            candidates = (
                feed_candidate_ids(change_feed)
                if change_feed is not None
                else set(query_candidates or set())
            )
            result = dependencies.recall_graph(
                graph,
                query,
                limit,
                max_chars,
                args.mode,
                candidate_ids=candidates,
                skip_validation=True,
                backend="sqlite_fts5",
                since_watermark=args.since_watermark,
            )
        except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
            index_warning = str(error)
            validation = dependencies.validate_graph(graph)
            if not validation["valid"]:
                raise ValueError(
                    "Refusing to recall from an invalid graph: " + "; ".join(validation["errors"])
                ) from error
            query_candidates = (
                dependencies.scan_query_candidate_ids(graph, query, args.mode) if query else None
            )
            change_feed = build_change_feed(query_candidates)
            result = dependencies.recall_graph(
                graph,
                query,
                limit,
                max_chars,
                args.mode,
                candidate_ids=(
                    feed_candidate_ids(change_feed) if change_feed is not None else None
                ),
                skip_validation=True,
                backend="scan_degraded",
                since_watermark=args.since_watermark,
            )
    if index_result is not None:
        result["index"] = index_result
    if index_warning:
        result["index_warning"] = index_warning
    if change_feed is not None:
        result["change_feed"] = change_feed
    return CommandResponse(result, ensure_ascii=False)


def get_response(
    args: argparse.Namespace,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    graph, canonical_digest, _ = dependencies.load_graph_with_digest(store_path)
    try:
        index_result = dependencies.ensure_recall_index(
            graph,
            store_path,
            canonical_digest,
            args.lock_timeout,
        )
        result = dependencies.get_indexed_graph_record(
            graph,
            dependencies.recall_index_path(store_path),
            args.id,
            args.mode,
            canonical_digest,
        )
        result["index"] = index_result
    except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
        result = dependencies.get_graph_record(graph, args.id, args.mode)
        result["backend"] = "scan_degraded"
        result["index_warning"] = str(error)
    return CommandResponse(result, ensure_ascii=False)
