"""Recall-index and projection coordination for legacy CLI operations."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

JsonObject = dict[str, object]


class RecallIndexHealthy(Protocol):
    def __call__(self, path: Path, /, *, full: bool = False) -> bool: ...


class EnsureRecallIndex(Protocol):
    def __call__(
        self,
        graph: JsonObject,
        store_path: Path,
        canonical_digest: str,
        timeout_seconds: float,
        *,
        force: bool = False,
    ) -> JsonObject: ...


class ProjectLatestFromStore(Protocol):
    def __call__(
        self,
        store_path: Path,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None,
        timeout_seconds: float,
        force_current_digest: str | None = None,
    ) -> JsonObject: ...


@dataclass(frozen=True)
class CoordinationDependencies:
    index_schema_version: str
    graph_schema_version: str
    default_lock_timeout_seconds: float
    recall_index_path: Callable[[Path], Path]
    read_recall_index_meta: Callable[[Path], dict[str, str]]
    recall_index_healthy: RecallIndexHealthy
    committed_watermark: Callable[[JsonObject], int]
    load_graph_with_digest: Callable[[Path], tuple[JsonObject, str, str]]
    ensure_recall_index: EnsureRecallIndex
    resolve_projection: Callable[[argparse.Namespace, JsonObject], tuple[Path, str]]
    project_latest_from_store: ProjectLatestFromStore
    normalize_space: Callable[[object], str]


def index_status(
    graph: JsonObject,
    store_path: Path,
    canonical_digest: str,
    dependencies: CoordinationDependencies,
) -> JsonObject:
    path = dependencies.recall_index_path(store_path)
    metadata = dependencies.read_recall_index_meta(path)
    healthy = dependencies.recall_index_healthy(path, full=True)
    current = bool(
        healthy
        and metadata.get("index_schema_version") == dependencies.index_schema_version
        and metadata.get("graph_schema_version") == dependencies.graph_schema_version
        and metadata.get("canonical_sha256") == canonical_digest
        and metadata.get("canonical_watermark") == str(dependencies.committed_watermark(graph))
    )
    return {
        "path": str(path),
        "exists": path.is_file(),
        "healthy": healthy,
        "current": current,
        "canonical_sha256": canonical_digest,
        "canonical_watermark": dependencies.committed_watermark(graph),
        "metadata": metadata,
    }


def ensure_latest_recall_index(
    store_path: Path,
    timeout_seconds: float,
    dependencies: CoordinationDependencies,
) -> JsonObject:
    result: JsonObject = {}
    for _ in range(3):
        graph, digest, _ = dependencies.load_graph_with_digest(store_path)
        result = dependencies.ensure_recall_index(graph, store_path, digest, timeout_seconds)
        _, latest_digest, _ = dependencies.load_graph_with_digest(store_path)
        if latest_digest == digest:
            return result
    return {
        **result,
        "status": "canonical_advanced_during_reindex",
        "current": False,
    }


def maybe_project_latest(
    args: argparse.Namespace,
    config: JsonObject,
    store_path: Path,
    dependencies: CoordinationDependencies,
) -> JsonObject | None:
    if not getattr(args, "project", False):
        return None
    vault, root_name = dependencies.resolve_projection(args, config)
    return dependencies.project_latest_from_store(
        store_path,
        vault,
        root_name,
        getattr(args, "mode", "review_safe"),
        dependencies.normalize_space(config.get("projection_manifest_key")),
        dependencies.normalize_space(config.get("projection_root_id")) or None,
        getattr(args, "lock_timeout", dependencies.default_lock_timeout_seconds),
    )


def migration_backup_path(
    store_path: Path,
    stored_version: str,
    watermark: int,
    digest: str,
) -> Path:
    return store_path.with_name(
        f"{store_path.name}.{stored_version}.watermark-{watermark}.{digest[:12]}.backup"
    )


__all__ = [
    "CoordinationDependencies",
    "ensure_latest_recall_index",
    "index_status",
    "maybe_project_latest",
    "migration_backup_path",
]
