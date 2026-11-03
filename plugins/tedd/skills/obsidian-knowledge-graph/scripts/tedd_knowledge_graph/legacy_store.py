"""Typed persistence service for the legacy JSON graph store.

Decoded JSON is the sole dynamic boundary.  Filesystem publication and clocks
are supplied by the flat compatibility facade so its safety hooks remain
replaceable at call time.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from tedd_knowledge_graph import legacy_migrations, legacy_values

JsonObject = legacy_migrations.JsonObject
PrepareGraph = Callable[[object], tuple[JsonObject, str]]
AtomicWriter = Callable[[Path, str], None]
Clock = Callable[[], str]


def blank_graph(*, clock: Clock = legacy_values.utc_now) -> JsonObject:
    now = clock()
    return {
        "schema_version": legacy_migrations.GRAPH_SCHEMA_VERSION,
        "updated_at": now,
        "next_watermark": 1,
        "journal_anchor": {"watermark": 0, "entry_hash": ""},
        "entities": [],
        "relations": [],
        "observations": [],
        "claim_surfaces": [],
        "claims": [],
        "events": [],
        "decisions": [],
        "open_loops": [],
        "receipts": [],
        "sources": [],
        "journal": [],
    }


def committed_watermark(graph: JsonObject) -> int:
    return max(0, int(cast(int | str, graph.get("next_watermark", 1))) - 1)


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at {path}: {error}") from error
    return decoded


def load_config(config_path: Path) -> JsonObject:
    config = load_json(config_path, {})
    if not isinstance(config, dict):
        raise ValueError(f"Configuration at {config_path} must be a JSON object")
    mapping = cast(dict[object, object], config)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"Configuration at {config_path} must use string keys")
    return cast(JsonObject, config)


def load_graph_with_digest(
    store_path: Path,
    *,
    blank_factory: Callable[[], JsonObject] = blank_graph,
    prepare: PrepareGraph = legacy_migrations.prepare_graph,
) -> tuple[JsonObject, str, str]:
    if not store_path.exists():
        graph = blank_factory()
        content = (
            json.dumps(
                graph,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        return (
            graph,
            hashlib.sha256(content).hexdigest(),
            legacy_migrations.GRAPH_SCHEMA_VERSION,
        )
    try:
        payload = store_path.read_bytes()
        raw_graph: object = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at {store_path}: {error}") from error
    graph, stored_version = prepare(raw_graph)
    return graph, hashlib.sha256(payload).hexdigest(), stored_version


def load_graph(
    store_path: Path,
    *,
    blank_factory: Callable[[], JsonObject] = blank_graph,
    prepare: PrepareGraph = legacy_migrations.prepare_graph,
) -> JsonObject:
    graph, _, _ = load_graph_with_digest(
        store_path,
        blank_factory=blank_factory,
        prepare=prepare,
    )
    return graph


def save_graph(
    store_path: Path,
    graph: JsonObject,
    *,
    clock: Clock,
    writer: AtomicWriter,
) -> str:
    graph["schema_version"] = legacy_migrations.GRAPH_SCHEMA_VERSION
    graph["updated_at"] = clock()
    content = json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    writer(store_path, content)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
