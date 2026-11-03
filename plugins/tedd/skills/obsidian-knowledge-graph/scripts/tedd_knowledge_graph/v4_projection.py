"""Bounded v4-to-legacy projection kernels.

This module is a leaf: policy and facade callbacks are supplied by the flat
compatibility adapter for each call.  It therefore has no import edge back to
``knowledge_graph`` or ``knowledge_graph_v4``.
"""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast


class ProjectionStore(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    def state(self) -> Mapping[str, object]: ...

    def get_records(self, record_ids: Sequence[str]) -> Sequence[Mapping[str, object]]: ...


class ProjectGraph(Protocol):
    def __call__(
        self,
        graph: dict[str, object],
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None,
        digest: str,
        timeout_seconds: float,
        force_current_digest: str | None,
        *,
        canonical_store_schema_version: str,
        projection_payload_schema_version: str,
    ) -> object: ...


@dataclass(frozen=True)
class ProjectionDependencies:
    collections: tuple[str, ...]
    max_root_records: int
    max_records: int
    blank_graph: Callable[[], object]
    validate_graph: Callable[[Mapping[str, object]], object]
    stable_hash: Callable[[object, int], str]
    project_graph: ProjectGraph
    canonical_schema_version: str
    projection_schema_version: str


def object_dict(value: object, *, label: str) -> dict[str, object]:
    """Narrow one validated JSON boundary to a string-keyed object."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{label} keys must be strings")
    return {str(key): child for key, child in raw.items()}


def graph_records(graph: Mapping[str, object], collection: str) -> list[dict[str, object]]:
    """Return one validated mutable legacy graph collection."""

    value = graph.get(collection, [])
    if not isinstance(value, list):
        raise ValueError(f"Graph collection {collection!r} must be a list")
    records = cast(list[object], value)
    if not all(isinstance(item, dict) for item in records):
        raise ValueError(f"Graph collection {collection!r} must contain objects")
    return cast(list[dict[str, object]], records)


def forward_closure(store: ProjectionStore, roots: Sequence[str]) -> set[str]:
    """Return the current forward-reference closure for bounded roots."""

    selected = set(roots)
    frontier = set(roots)
    while frontier:
        targets: set[str] = set()
        ordered = sorted(frontier)
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = store.connection.execute(
                f"""
                SELECT DISTINCT rr.target_record_id
                FROM revision_refs rr
                JOIN records r ON r.record_id=rr.record_id
                              AND r.current_revision=rr.revision
                WHERE rr.record_id IN ({placeholders})
                """,
                batch,
            )
            targets.update(str(row[0]) for row in rows)
        frontier = targets - selected
        selected.update(frontier)
    return selected


def records_graph(
    store: ProjectionStore,
    record_ids: set[str],
    *,
    projection_metadata: Mapping[str, object] | None = None,
    dependencies: ProjectionDependencies,
) -> dict[str, object]:
    """Convert selected current records to the validated legacy graph shape."""

    graph = object_dict(dependencies.blank_graph(), label="Legacy graph")
    state = store.state()
    graph["updated_at"] = state["updated_at"]
    graph["next_watermark"] = int(cast(int, state["watermark"])) + 1
    graph["journal_anchor"] = {
        "watermark": int(cast(int, state["watermark"])),
        "entry_hash": state["journal_head_hash"],
    }
    ordered = sorted(record_ids)
    for start in range(0, len(ordered), 2_000):
        for item in store.get_records(ordered[start : start + 2_000]):
            payload = item.get("payload")
            if not isinstance(payload, dict):
                raise ValueError(f"Current payload is unavailable for {item['record_id']}")
            payload_object = object_dict(copy.deepcopy(payload), label="Current projection payload")
            graph_records(graph, str(item["collection"])).append(payload_object)
    for collection in dependencies.collections:
        graph_records(graph, collection).sort(key=lambda item: str(item.get("id", "")))
    if projection_metadata:
        graph["projection_hot_set"] = dict(projection_metadata)
    validation = object_dict(
        dependencies.validate_graph(graph), label="Projection graph validation"
    )
    if not bool(validation.get("valid")):
        errors_value = validation.get("errors", [])
        if not isinstance(errors_value, list):
            raise ValueError("Projection validation errors must be a list")
        errors = [str(item) for item in cast(list[object], errors_value)]
        raise ValueError("Generated bounded v4 graph is invalid: " + "; ".join(errors))
    return graph


def projection_graph(
    store: ProjectionStore,
    *,
    dependencies: ProjectionDependencies,
) -> tuple[dict[str, object], dict[str, object]]:
    """Select and materialize the bounded current-state projection."""

    total = int(store.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    if total <= dependencies.max_records:
        roots = [
            str(row[0])
            for row in store.connection.execute("SELECT record_id FROM records ORDER BY record_id")
        ]
        selected = set(roots)
        strategy = "complete_current_state"
    else:
        roots = [
            str(row[0])
            for row in store.connection.execute(
                """
                SELECT record_id
                FROM records
                ORDER BY
                  CASE collection
                    WHEN 'open_loops' THEN 0
                    WHEN 'claims' THEN 1
                    WHEN 'decisions' THEN 2
                    WHEN 'receipts' THEN 3
                    WHEN 'events' THEN 4
                    WHEN 'claim_surfaces' THEN 5
                    WHEN 'entities' THEN 6
                    WHEN 'observations' THEN 7
                    WHEN 'relations' THEN 8
                    ELSE 9
                  END,
                  CASE WHEN lifecycle='conflicting' THEN 0 ELSE 1 END,
                  last_watermark DESC,
                  record_id
                LIMIT ?
                """,
                (dependencies.max_root_records,),
            )
        ]
        selected = forward_closure(store, roots)
        while len(selected) > dependencies.max_records and len(roots) > 1:
            roots = roots[: max(1, len(roots) // 2)]
            selected = forward_closure(store, roots)
        if len(selected) > dependencies.max_records:
            raise ValueError("One projection root expands beyond the bounded hot-set limit")
        strategy = "priority_hot_set_with_reference_closure"
    metadata: dict[str, object] = {
        "strategy": strategy,
        "canonical_record_count": total,
        "root_record_count": len(roots),
        "projected_record_count": len(selected),
        "record_limit": dependencies.max_records,
        "complete": len(selected) == total,
    }
    return (
        records_graph(
            store,
            selected,
            projection_metadata=metadata,
            dependencies=dependencies,
        ),
        metadata,
    )


def mask_source_uris(graph: dict[str, object]) -> None:
    """Keep archived source identifiers out of ordinary review-safe projection."""
    for source in graph_records(graph, "sources"):
        uri = source.get("uri")
        if isinstance(uri, str) and uri.strip().casefold().startswith("tedd-source:"):
            source["uri"] = "[withheld in review_safe mode]"


def project_v4(
    store: ProjectionStore,
    *,
    vault: Path,
    root_name: str,
    mode: str,
    manifest_key: str,
    root_id: str | None,
    timeout_seconds: float,
    force_current_digest: str | None,
    dependencies: ProjectionDependencies,
) -> dict[str, object]:
    """Project one already-open snapshot using injected legacy policy."""

    graph, hot_set = projection_graph(store, dependencies=dependencies)
    digest = dependencies.stable_hash(graph, 64)
    if mode == "review_safe":
        mask_source_uris(graph)
    result = object_dict(
        dependencies.project_graph(
            graph,
            vault,
            root_name,
            mode,
            manifest_key,
            root_id,
            digest,
            timeout_seconds,
            force_current_digest,
            canonical_store_schema_version=dependencies.canonical_schema_version,
            projection_payload_schema_version=dependencies.projection_schema_version,
        ),
        label="Projection result",
    )
    return {**result, "hot_set": hot_set, "projection_digest": digest}


__all__ = [
    "ProjectionDependencies",
    "ProjectionStore",
    "forward_closure",
    "graph_records",
    "object_dict",
    "project_v4",
    "projection_graph",
    "records_graph",
]
