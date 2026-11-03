"""Typed contracts and validated JSON boundary for legacy graph mutation."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
# Mutable JSON objects use ``object`` at field reads because Python containers
# are invariant. ``require_json_object`` establishes the recursive boundary.
JsonObject: TypeAlias = dict[str, object]


class ParseTime(Protocol):
    def __call__(self, value: object, fallback: str | None = None) -> str: ...


class SourceTier(Protocol):
    def __call__(self, value: object, fallback: str = "assistant_inference") -> str: ...


class CappedSourceTier(Protocol):
    def __call__(
        self,
        value: object,
        ceiling: str,
        fallback: str = "assistant_inference",
    ) -> tuple[str, bool]: ...


class StableHash(Protocol):
    def __call__(self, value: object, length: int = 20) -> str: ...


class ScrubValue(Protocol):
    def __call__(
        self,
        value: object,
        *,
        redact_paths: bool = True,
        depth: int = 0,
    ) -> tuple[object, int]: ...


class SupportsInt(Protocol):
    def __int__(self) -> int: ...


@dataclass(frozen=True)
class IdentityDependencies:
    """Call-time hooks used by identity and entity-registry mutation."""

    source_tiers: Mapping[str, int]
    scoped_entity_types: AbstractSet[str]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    explicit_record_id: Callable[[object], str]
    time_value: Callable[[str], dt.datetime]
    latest_time: Callable[[str | None, str], str]
    earliest_time: Callable[[str | None, str], str]
    stable_id: Callable[[str, object], str]
    unique_strings: Callable[[Iterable[object]], list[str]]
    index_by_id: Callable[[list[JsonObject]], dict[str, JsonObject]]
    identity_key_signature: Callable[[JsonObject, str], tuple[str, str, str]]
    normalized_identity_keys: Callable[[JsonObject, str], list[tuple[str, str, str]]]
    canonical_entity_id: Callable[[JsonObject, str], str]
    resolve_entity_ref: Callable[[object, dict[str, str], JsonObject], str]
    source_tier: SourceTier
    source_tier_rank: Callable[[object], int]
    resolved_entity_refs: Callable[
        [JsonObject, str, dict[str, str], JsonObject],
        list[str],
    ]


@dataclass(frozen=True)
class RecordDependencies:
    """Call-time hooks used by claim and generic-record mutation."""

    source_tiers: Mapping[str, int]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    explicit_record_id: Callable[[object], str]
    parse_time: ParseTime
    time_value: Callable[[str], dt.datetime]
    latest_time: Callable[[str | None, str], str]
    earliest_time: Callable[[str | None, str], str]
    stable_id: Callable[[str, object], str]
    unique_strings: Callable[[Iterable[object]], list[str]]
    _legacy_surface_id: Callable[[object, object, Iterable[object]], str]
    index_by_id: Callable[[list[JsonObject]], dict[str, JsonObject]]
    canonical_entity_refs: Callable[[JsonObject, Iterable[object]], list[str]]
    resolved_entity_refs: Callable[
        [JsonObject, str, dict[str, str], JsonObject],
        list[str],
    ]
    record_entity_refs: Callable[[JsonObject, dict[str, str], JsonObject], list[str]]
    source_tier: SourceTier
    source_tier_rank: Callable[[object], int]
    upsert_claim_surface: Callable[
        [JsonObject, JsonObject, dict[str, str], JsonObject, str, str, list[str]],
        tuple[str, bool, bool],
    ]


@dataclass(frozen=True)
class CaptureDependencies:
    """Call-time hooks used by packet fingerprint and capture coordination."""

    source_tiers: Mapping[str, int]
    discovery_kinds: Collection[str]
    fingerprint_only_marker: object
    blank_graph: Callable[[], JsonObject]
    utc_now: Callable[[], str]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    explicit_record_id: Callable[[object], str]
    sensitive_field: Callable[[object], bool]
    parse_time: ParseTime
    stable_hash: StableHash
    stable_id: Callable[[str, object], str]
    unique_strings: Callable[[Iterable[object]], list[str]]
    scrub_value: ScrubValue
    committed_watermark: Callable[[JsonObject], int]
    journal_entry_hash: Callable[[JsonObject, str], str]
    index_by_id: Callable[[list[JsonObject]], dict[str, JsonObject]]
    normalized_identity_keys: Callable[[JsonObject, str], list[tuple[str, str, str]]]
    source_tier: SourceTier
    capped_source_tier: CappedSourceTier
    upsert_entity: Callable[[JsonObject, JsonObject, str, str, str], tuple[str, bool]]
    upsert_relation: Callable[
        [JsonObject, JsonObject, dict[str, str], str, str, str],
        tuple[str, bool],
    ]
    add_entity_redirect: Callable[
        [JsonObject, JsonObject, dict[str, str], JsonObject],
        tuple[str, bool],
    ]
    add_observation: Callable[
        [JsonObject, JsonObject, dict[str, str], JsonObject],
        tuple[str, bool],
    ]
    add_claim: Callable[
        [JsonObject, JsonObject, dict[str, str], JsonObject],
        tuple[str, str],
    ]
    add_generic_record: Callable[
        [JsonObject, str, JsonObject, dict[str, str], JsonObject],
        tuple[str, bool],
    ]
    packet_records: Callable[[JsonObject, str], list[JsonObject]]
    canonicalize_packet_timestamps: Callable[
        [JsonObject, dict[str, list[JsonObject]]],
        None,
    ]
    canonical_fingerprint_packet: Callable[
        [JsonObject, dict[str, list[JsonObject]]],
        JsonObject,
    ]
    normalize_producer: Callable[[JsonObject | None], dict[str, str]]
    append_journal_entry: Callable[[JsonObject, JsonObject], int]


def _validate_json(value: object, *, label: str, depth: int = 0) -> None:
    if depth > 24:
        raise ValueError(f"{label} exceeds the JSON nesting limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, (list, tuple)):
        for item in cast(Sequence[object], value):
            _validate_json(item, label=label, depth=depth + 1)
        return
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} object keys must be strings")
            _validate_json(item, label=label, depth=depth + 1)
        return
    raise ValueError(f"{label} contains a non-JSON value")


def require_json_object(value: object, label: str) -> JsonObject:
    """Validate and narrow one dynamic recursive JSON object."""

    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    _validate_json(value, label=label)
    return cast(JsonObject, value)


def _records(graph: JsonObject, collection: str) -> list[JsonObject]:
    value = graph.get(collection)
    if not isinstance(value, list):
        raise ValueError(f"{collection} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{collection}[{index}] must be an object")
    return cast(list[JsonObject], value)


def _string(value: object) -> str:
    return cast(str, value)


def _optional_string(value: object) -> str | None:
    return cast(str | None, value)


def _objects(value: object) -> Iterable[object]:
    return cast(Iterable[object], value)


def _strings(value: object) -> Iterable[str]:
    return cast(Iterable[str], value)


def _object_records(value: object) -> Iterable[JsonObject]:
    return cast(Iterable[JsonObject], value)


def _mutable_object_records(value: object) -> list[JsonObject]:
    return cast(list[JsonObject], value)


def _to_int(value: object) -> int:
    return int(cast(str | bytes | bytearray | SupportsInt, value))


__all__ = [
    "CaptureDependencies",
    "IdentityDependencies",
    "JsonObject",
    "JsonValue",
    "RecordDependencies",
    "require_json_object",
]
