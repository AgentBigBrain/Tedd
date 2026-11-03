"""Legacy redaction, loop-resolution, and mutation-journal policy."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

JsonObject = dict[str, object]


class RedactPayload(Protocol):
    def __call__(
        self,
        record: JsonObject,
        *,
        include_identity: bool,
    ) -> None: ...


@dataclass(frozen=True)
class MaintenanceDependencies:
    """Live policy callbacks needed by legacy maintenance mutations."""

    collections: Sequence[str]
    discovery_kinds: Collection[str]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    integer_value: Callable[[object], int]
    stable_hash: Callable[[object, int], str]
    unique_strings: Callable[[Iterable[object]], list[str]]
    utc_now: Callable[[], str]
    committed_watermark: Callable[[JsonObject], int]
    append_journal_entry: Callable[[JsonObject, JsonObject], int]
    normalize_producer: Callable[[JsonObject | None], dict[str, str]]
    index_by_id: Callable[[list[JsonObject]], dict[str, JsonObject]]
    redact_payload: RedactPayload


def _records(graph: JsonObject, collection: str) -> list[JsonObject]:
    return cast(list[JsonObject], graph[collection])


def redact_payload(
    record: JsonObject,
    *,
    include_identity: bool,
    utc_now: Callable[[], str],
) -> None:
    del include_identity  # v1 tombstones all user-controlled text, including identity text.
    text_fields = {
        "excerpt",
        "family",
        "key",
        "kind",
        "name",
        "namespace",
        "next_action",
        "quarantine_reason",
        "redirect_reason",
        "rationale",
        "requested_status",
        "resolution",
        "role",
        "summary",
        "terminal_condition",
        "title",
        "type",
        "uri",
        "value",
    }
    for key in text_fields:
        if key in record and record[key] not in (None, "", "[redacted]"):
            record[key] = "[redacted]"
    if record.get("aliases"):
        record["aliases"] = []
    if record.get("identity_keys"):
        record["identity_keys"] = []
    record.pop("redirect_to", None)
    record.pop("redirect_source_ref", None)
    if "status" in record:
        record["status"] = "redacted"
    if "authority_state" in record:
        record["authority_state"] = "redacted"
    record["lifecycle"] = "redacted"
    record["active"] = False
    record["redacted_at"] = utc_now()


def redact_record(
    graph: JsonObject,
    record_id: str,
    dependencies: MaintenanceDependencies,
) -> JsonObject:
    for collection in dependencies.collections:
        for record in _records(graph, collection):
            if record.get("id") != record_id:
                continue
            if record.get("lifecycle") == "redacted":
                return {
                    "id": record_id,
                    "collection": collection,
                    "status": "redacted",
                    "cascaded_ids": [record_id],
                    "changed": False,
                }
            source_ids = set(cast(Iterable[object], record.get("evidence_refs", [])))
            if record.get("source_ref"):
                source_ids.add(record["source_ref"])
            observation_ids = set(cast(Iterable[object], record.get("observation_refs", [])))
            for observation in _records(graph, "observations"):
                if observation.get("id") in observation_ids and observation.get("source_ref"):
                    source_ids.add(observation["source_ref"])
            cascaded_ids = [record_id]
            dependencies.redact_payload(record, include_identity=True)
            if collection == "entities":
                for entity in _records(graph, "entities"):
                    if entity.get("redirect_to") == record_id:
                        dependencies.redact_payload(entity, include_identity=True)
                        cascaded_ids.append(cast(str, entity["id"]))
            for observation in _records(graph, "observations"):
                if observation.get("id") in observation_ids:
                    dependencies.redact_payload(observation, include_identity=False)
                    cascaded_ids.append(cast(str, observation["id"]))
            for source in _records(graph, "sources"):
                if source.get("id") in source_ids:
                    dependencies.redact_payload(source, include_identity=True)
                    cascaded_ids.append(cast(str, source["id"]))
            return {
                "id": record_id,
                "collection": collection,
                "status": "redacted",
                "cascaded_ids": dependencies.unique_strings(cascaded_ids),
                "changed": True,
            }
    raise ValueError(f"Unknown record id: {record_id}")


def resolve_loop(
    graph: JsonObject,
    loop_id: str,
    resolution: str,
    dependencies: MaintenanceDependencies,
) -> JsonObject:
    loop = dependencies.index_by_id(_records(graph, "open_loops")).get(loop_id)
    if not loop:
        raise ValueError(f"Unknown open-loop id: {loop_id}")
    if loop.get("lifecycle") == "redacted":
        raise ValueError("A redacted open loop cannot be resolved or restored")
    normalized_resolution = dependencies.normalize_space(resolution)
    if loop.get("status") == "resolved" and loop.get("resolution") == normalized_resolution:
        return {"id": loop_id, "status": "resolved", "changed": False}
    loop["status"] = "resolved"
    loop["resolved_at"] = dependencies.utc_now()
    loop["resolution"] = normalized_resolution
    return {"id": loop_id, "status": "resolved", "changed": True}


def mutation_journal(
    graph: JsonObject,
    action: str,
    changed_ids: list[str],
    *,
    producer: JsonObject | None,
    based_on_watermark: int | None,
    discovery_kind: str | None,
    details: JsonObject | None,
    dependencies: MaintenanceDependencies,
) -> int:
    watermark = dependencies.integer_value(graph.get("next_watermark", 1))
    entry: JsonObject = {
        "fingerprint": dependencies.stable_hash(
            {"action": action, "ids": changed_ids, "watermark": watermark},
            32,
        ),
        "fingerprint_version": "v2",
        "source_ref": None,
        "changed_ids": changed_ids,
        "redaction_count": 0,
        "action": action,
    }
    producer_record = dependencies.normalize_producer(producer)
    if producer_record:
        entry["producer"] = producer_record
    normalized_discovery_kind = dependencies.normalize_key(discovery_kind)
    if normalized_discovery_kind:
        if normalized_discovery_kind not in dependencies.discovery_kinds:
            raise ValueError(f"Unsupported discovery kind: {normalized_discovery_kind}")
        entry["discovery_kind"] = normalized_discovery_kind
    if based_on_watermark is not None:
        if based_on_watermark < 0 or based_on_watermark > dependencies.committed_watermark(graph):
            raise ValueError("Invalid based_on_watermark for mutation journal")
        entry["based_on_watermark"] = based_on_watermark
    if details:
        entry["details"] = copy.deepcopy(details)
    return dependencies.append_journal_entry(graph, entry)


__all__ = [
    "MaintenanceDependencies",
    "mutation_journal",
    "redact_payload",
    "redact_record",
    "resolve_loop",
]
