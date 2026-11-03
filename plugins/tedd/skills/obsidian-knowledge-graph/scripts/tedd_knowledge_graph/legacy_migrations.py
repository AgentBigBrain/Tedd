"""Pure schema migration service for legacy JSON graph stores.

The flat ``knowledge_graph`` module remains the compatibility facade.  This
module owns deterministic schema preparation and treats decoded JSON as its
validated dynamic boundary; it performs no file or process I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import cast

from tedd_knowledge_graph import legacy_values

GRAPH_SCHEMA_VERSION = "v3"
LEGACY_GRAPH_SCHEMA_VERSIONS = frozenset({"v2"})
MIGRATION_TRANSFORM_VERSION = "v2-to-v3.identity-surfaces-journal.1"
COLLECTIONS = (
    "entities",
    "relations",
    "observations",
    "claim_surfaces",
    "claims",
    "events",
    "decisions",
    "open_loops",
    "receipts",
    "sources",
    "journal",
)
SOURCE_TIERS = {
    "assistant_inference": 1,
    "reconciliation_or_projection": 2,
    "validated_structured_candidate": 3,
    "explicit_user_statement": 4,
}

JsonObject = dict[str, object]


def _object_iterable(value: object) -> Iterable[object]:
    """Narrow a JSON collection after its enclosing store was decoded."""

    return cast(Iterable[object], value)


def _record_list(graph: JsonObject, collection: str) -> list[JsonObject]:
    """Return one migration collection at the validated JSON boundary."""

    return cast(list[JsonObject], graph[collection])


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{legacy_values.stable_hash(value)}"


def journal_entry_hash(entry: JsonObject, previous_hash: str) -> str:
    payload = {key: value for key, value in entry.items() if key != "entry_hash"}
    return legacy_values.stable_hash(
        {"previous_hash": previous_hash, "entry": payload},
        64,
    )


def legacy_surface_id(
    family: object,
    key: object,
    refs: Iterable[object],
) -> str:
    return _stable_id(
        "surface",
        {
            "family": legacy_values.normalize_key(family),
            "key": legacy_values.normalize_key(key),
            "subject_refs": sorted(legacy_values.unique_strings(refs)),
        },
    )


def migrate_graph_v2_to_v3(raw_graph: JsonObject) -> JsonObject:
    """Return a deterministic compatibility-preserving v3 copy of a v2 graph."""

    if raw_graph.get("schema_version") != "v2":
        raise ValueError(f"Cannot migrate graph schema {raw_graph.get('schema_version')!r} as v2")
    graph = copy.deepcopy(raw_graph)
    graph["schema_version"] = GRAPH_SCHEMA_VERSION
    graph.setdefault("claim_surfaces", [])
    graph.setdefault("journal_anchor", {"watermark": 0, "entry_hash": ""})
    for collection in COLLECTIONS:
        graph.setdefault(collection, [])
    graph.setdefault("next_watermark", 1)

    for entity in _record_list(graph, "entities"):
        entity.setdefault("lifecycle", "active")
        if "identity_keys" not in entity:
            entity["identity_keys"] = [
                {
                    "scheme": "legacy_entity_id",
                    "namespace": legacy_values.normalize_key(entity.get("namespace")) or "global",
                    "value": entity.get("id"),
                    "first_seen_at": entity.get("first_seen_at") or graph.get("updated_at"),
                    "last_seen_at": entity.get("last_seen_at") or graph.get("updated_at"),
                    "evidence_refs": list(_object_iterable(entity.get("evidence_refs", []))),
                }
            ]

    surfaces: dict[tuple[str, str, tuple[str, ...]], JsonObject] = {}
    for claim in _record_list(graph, "claims"):
        signature = (
            legacy_values.normalize_key(claim.get("family")),
            legacy_values.normalize_key(claim.get("key")),
            tuple(
                sorted(legacy_values.unique_strings(_object_iterable(claim.get("entity_refs", []))))
            ),
        )
        surface = surfaces.get(signature)
        if surface is None:
            surface = {
                "id": legacy_surface_id(*signature),
                "family": signature[0],
                "key": signature[1],
                "subject_refs": list(signature[2]),
                "cardinality": "single",
                "lifecycle": "active",
                "created_at": claim.get("valid_from") or graph.get("updated_at"),
                "last_observed_at": claim.get("asserted_at")
                or claim.get("valid_from")
                or graph.get("updated_at"),
                "evidence_refs": list(_object_iterable(claim.get("evidence_refs", []))),
                "source_tier": claim.get(
                    "source_tier",
                    "assistant_inference",
                ),
                "sensitive": bool(claim.get("sensitive", False)),
                "legacy_derived": True,
            }
            surfaces[signature] = surface
        else:
            surface["evidence_refs"] = legacy_values.unique_strings(
                [
                    *_object_iterable(surface.get("evidence_refs", [])),
                    *_object_iterable(claim.get("evidence_refs", [])),
                ]
            )
            surface["sensitive"] = bool(surface.get("sensitive")) or bool(claim.get("sensitive"))
            claim_tier = cast(str, claim.get("source_tier"))
            surface_tier = cast(str, surface.get("source_tier"))
            if SOURCE_TIERS.get(claim_tier, 0) > SOURCE_TIERS.get(
                surface_tier,
                0,
            ):
                surface["source_tier"] = claim.get("source_tier")
        claim["surface_ref"] = surface["id"]
    graph["claim_surfaces"] = sorted(
        surfaces.values(),
        key=lambda item: cast(str, item["id"]),
    )

    journal_anchor = cast(JsonObject, graph["journal_anchor"])
    previous_hash = legacy_values.normalize_space(journal_anchor.get("entry_hash"))
    for entry in _record_list(graph, "journal"):
        entry.setdefault("action", "capture")
        entry.setdefault("fingerprint_version", "v1")
        entry["previous_entry_hash"] = previous_hash
        entry["entry_hash"] = journal_entry_hash(entry, previous_hash)
        previous_hash = cast(str, entry["entry_hash"])
    return graph


def prepare_graph(raw_graph: object) -> tuple[JsonObject, str]:
    """Validate the decoded root and migrate a supported historical schema."""

    if not isinstance(raw_graph, dict):
        raise ValueError("Canonical graph must be a JSON object")
    graph_object = cast(dict[object, object], raw_graph)
    if not all(isinstance(key, str) for key in graph_object):
        raise ValueError("Canonical graph keys must be strings")
    graph = cast(JsonObject, raw_graph)
    stored_version = legacy_values.normalize_space(graph.get("schema_version"))
    if stored_version == GRAPH_SCHEMA_VERSION:
        prepared = graph
    elif stored_version in LEGACY_GRAPH_SCHEMA_VERSIONS:
        prepared = migrate_graph_v2_to_v3(graph)
    else:
        raise ValueError(f"Unsupported graph schema: {stored_version!r}")
    return prepared, stored_version
