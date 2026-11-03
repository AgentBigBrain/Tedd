"""Frozen retrieval providers evaluated by the memory lab."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from .catalog import INERT_CANARY, INERT_SENSITIVE, INERT_UNTRUSTED, NAMESPACE
from .contracts import Budget, ContextPacket, PayloadAssertion, Scenario
from .sandbox import MemoryEvalSandbox, subscription_id_for

VARIANT_IDS = (
    "no_memory_v1",
    "context_recency_v1",
    "recall_fts_v1",
    "oracle_v1",
)
REVIEW_SAFE_WITHHELD = "[withheld in review_safe mode]"
LIST_REFERENCE_FIELDS = frozenset(
    {
        "entity_refs",
        "evidence_refs",
        "observation_refs",
        "owner_refs",
        "subject_refs",
    }
)
SCALAR_REFERENCE_FIELDS = frozenset(
    {
        "redirect_source_ref",
        "redirect_to",
        "source_entity_id",
        "source_ref",
        "superseded_by",
        "surface_ref",
        "target_entity_id",
    }
)
EXPLICIT_REFERENCE_CONTAINERS = frozenset({"refs", "references"})
EVALUATION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


class Provider(Protocol):
    """One immutable retrieval strategy."""

    variant_id: str

    def retrieve(
        self,
        sandbox: MemoryEvalSandbox,
        scenario: Scenario,
        budget: Budget,
    ) -> ContextPacket: ...


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise RuntimeError(f"{label} keys must be strings")
    return {str(key): item for key, item in raw.items()}


def _records(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"{label} must be an array")
    return [_object(item, f"{label} item") for item in cast(Sequence[object], value)]


def _record_id(record: Mapping[str, object]) -> str:
    value = record.get("record_id")
    if not isinstance(value, str) or not value:
        raise RuntimeError("Graph record is missing an opaque record ID")
    return value


def canonical_payload_json(payload: object) -> str:
    """Serialize one review-safe payload for budgets and evaluation tokens."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def evaluation_token_count(payload_texts: Sequence[str]) -> int:
    """Count frozen dependency-free comparison tokens over ordered payloads."""

    return sum(1 for _ in EVALUATION_TOKEN_PATTERN.finditer("\n".join(payload_texts)))


def _payload_chars(record: Mapping[str, object]) -> int:
    return len(canonical_payload_json(record.get("payload")))


def _payload(record: Mapping[str, object]) -> Mapping[str, object]:
    return _object(record.get("payload"), "Graph record payload")


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{label} must be a boolean")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"{label} must be an array")
    result = list(cast(Sequence[object], value))
    if not all(isinstance(item, str) for item in result):
        raise RuntimeError(f"{label} must contain text")
    return [str(item) for item in result]


def _assertion_failed(
    records_by_id: Mapping[str, Mapping[str, object]], assertion: PayloadAssertion
) -> bool:
    record = records_by_id.get(assertion.record_id)
    if record is None:
        return False
    payload = _payload(record)
    return payload.get(assertion.field) != assertion.expected


def _is_reference_field(field: str) -> bool:
    return (
        field in LIST_REFERENCE_FIELDS
        or field in SCALAR_REFERENCE_FIELDS
        or field.endswith("_ref")
        or field.endswith("_refs")
    )


def _reference_values(field: str, value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    expects_list = field in LIST_REFERENCE_FIELDS or field.endswith("_refs")
    if not expects_list and isinstance(value, str) and value:
        return (value,)
    if (
        expects_list
        and isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        material = tuple(cast(Sequence[object], value))
        if all(isinstance(item, str) and item for item in material):
            return tuple(str(item) for item in material)
    return None


def _reference_scope_exposure(
    records: Sequence[Mapping[str, object]],
    scenario: Scenario,
) -> int:
    exposure = 0
    allowed_ids = frozenset(_record_id(record) for record in records) | {
        scenario.subject_id,
        REVIEW_SAFE_WITHHELD,
    }

    def walk(value: object, *, field: str | None = None, explicit: bool = False) -> None:
        nonlocal exposure
        if field is not None and (
            _is_reference_field(field) or (explicit and field == "target_id")
        ):
            references = _reference_values(field, value)
            if references is None:
                exposure += 1
                return
            exposure += sum(
                reference not in scenario.reference_scope_ids or reference not in allowed_ids
                for reference in references
                if reference and reference != REVIEW_SAFE_WITHHELD
            )
            return
        if isinstance(value, Mapping):
            raw = cast(Mapping[object, object], value)
            for key, child in raw.items():
                key_text = str(key)
                if key_text in EXPLICIT_REFERENCE_CONTAINERS:
                    if not isinstance(child, list):
                        exposure += 1
                        continue
                    entries = cast(list[object], child)
                    for entry in entries:
                        if not isinstance(entry, Mapping) or "target_id" not in entry:
                            exposure += 1
                            continue
                        walk(entry, explicit=True)
                    continue
                walk(child, field=key_text, explicit=explicit)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in cast(Sequence[object], value):
                walk(child, explicit=explicit)

    for record in records:
        record_id = _record_id(record)
        payload = _payload(record)
        exposure += payload.get("id") != record_id
        walk(payload)
    return exposure


def _packet(
    records: Sequence[Mapping[str, object]],
    *,
    scenario: Scenario,
    reported_chars: int | None,
    truncated: bool,
    duration_ms: float,
) -> ContextPacket:
    payload_texts = tuple(canonical_payload_json(record.get("payload")) for record in records)
    payload_chars = sum(len(payload_text) for payload_text in payload_texts)
    records_by_id = {_record_id(record): record for record in records}
    if len(records_by_id) != len(records):
        raise RuntimeError("Graph response contains duplicate record IDs")
    review_safe_text = json.dumps(
        list(records),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ContextPacket(
        record_ids=tuple(_record_id(record) for record in records),
        payload_chars=payload_chars,
        reported_chars=payload_chars if reported_chars is None else reported_chars,
        item_count=len(records),
        truncated=truncated,
        duration_ms=duration_ms,
        evaluation_tokens=evaluation_token_count(payload_texts),
        assertion_failures=sum(
            _assertion_failed(records_by_id, assertion)
            for assertion in scenario.required_assertions
        ),
        untrusted_text_exposure=review_safe_text.count(INERT_UNTRUSTED),
        sensitive_text_exposure=review_safe_text.count(INERT_SENSITIVE),
        canary_text_exposure=review_safe_text.count(INERT_CANARY),
        payload_namespace_exposure=sum(
            _payload(record).get("namespace") != NAMESPACE for record in records
        ),
        payload_reference_scope_exposure=_reference_scope_exposure(records, scenario),
    )


class NoMemoryProvider:
    variant_id = "no_memory_v1"

    def retrieve(
        self,
        sandbox: MemoryEvalSandbox,
        scenario: Scenario,
        budget: Budget,
    ) -> ContextPacket:
        del sandbox, scenario, budget
        return ContextPacket(
            record_ids=(),
            payload_chars=0,
            reported_chars=0,
            item_count=0,
            truncated=False,
            duration_ms=0.0,
            evaluation_tokens=0,
        )


class ContextRecencyProvider:
    variant_id = "context_recency_v1"

    def retrieve(
        self,
        sandbox: MemoryEvalSandbox,
        scenario: Scenario,
        budget: Budget,
    ) -> ContextPacket:
        started = time.perf_counter()
        response = sandbox.call(
            (
                "context",
                "--subscription-id",
                subscription_id_for(scenario),
                "--max-items",
                str(budget.max_items),
                "--max-chars",
                str(budget.max_chars),
                "--mode",
                "review_safe",
            )
        )
        duration_ms = (time.perf_counter() - started) * 1_000
        records = _records(response.get("records", []), "Context records")
        expected_subscription = subscription_id_for(scenario)
        if response.get("subscription_id") != expected_subscription:
            raise RuntimeError("Context response does not match the requested subscription")
        if _integer(response.get("item_count"), "Context item count") != len(records):
            raise RuntimeError("Context response item count does not match its records")
        return _packet(
            records,
            scenario=scenario,
            reported_chars=_integer(response.get("char_count"), "Context character count"),
            truncated=_boolean(response.get("truncated"), "Context truncation marker"),
            duration_ms=duration_ms,
        )


class RecallFtsProvider:
    variant_id = "recall_fts_v1"

    def retrieve(
        self,
        sandbox: MemoryEvalSandbox,
        scenario: Scenario,
        budget: Budget,
    ) -> ContextPacket:
        started = time.perf_counter()
        response = sandbox.call(
            (
                "recall",
                scenario.query,
                "--subject-id",
                scenario.subject_id,
                "--limit",
                str(budget.max_items),
                "--max-chars",
                str(budget.max_chars),
                "--mode",
                "review_safe",
            )
        )
        duration_ms = (time.perf_counter() - started) * 1_000
        records = _records(response.get("results", []), "Recall records")
        if _integer(response.get("result_count"), "Recall result count") != len(records):
            raise RuntimeError("Recall response result count does not match its records")
        if _string_list(response.get("subject_ids"), "Recall subject IDs") != [scenario.subject_id]:
            raise RuntimeError("Recall response does not match the requested subject scope")
        truncated_value = response.get("truncated_by_char_budget", False)
        return _packet(
            records,
            scenario=scenario,
            reported_chars=None,
            truncated=_boolean(truncated_value, "Recall truncation marker"),
            duration_ms=duration_ms,
        )


def _bounded_oracle(
    records: Sequence[Mapping[str, object]], budget: Budget
) -> tuple[list[Mapping[str, object]], bool]:
    selected: list[Mapping[str, object]] = []
    used_chars = 0
    truncated = len(records) > budget.max_items
    for record in records[: budget.max_items]:
        size = _payload_chars(record)
        if used_chars + size > budget.max_chars:
            truncated = True
            break
        selected.append(record)
        used_chars += size
    return selected, truncated


class OracleProvider:
    variant_id = "oracle_v1"

    def retrieve(
        self,
        sandbox: MemoryEvalSandbox,
        scenario: Scenario,
        budget: Budget,
    ) -> ContextPacket:
        started = time.perf_counter()
        records = [
            sandbox.call(("get", "--id", record_id, "--mode", "review_safe"))
            for record_id in scenario.oracle_ids
        ]
        selected, truncated = _bounded_oracle(records, budget)
        if tuple(_record_id(record) for record in records) != scenario.oracle_ids:
            raise RuntimeError("Oracle exact-ID response drifted from the authored request")
        duration_ms = (time.perf_counter() - started) * 1_000
        return _packet(
            selected,
            scenario=scenario,
            reported_chars=None,
            truncated=truncated,
            duration_ms=duration_ms,
        )


def providers() -> tuple[Provider, ...]:
    """Return providers in stable report order."""

    result: tuple[Provider, ...] = (
        NoMemoryProvider(),
        ContextRecencyProvider(),
        RecallFtsProvider(),
        OracleProvider(),
    )
    if tuple(provider.variant_id for provider in result) != VARIANT_IDS:
        raise RuntimeError("Memory evaluation provider order drifted")
    return result


__all__ = [
    "Provider",
    "VARIANT_IDS",
    "canonical_payload_json",
    "evaluation_token_count",
    "providers",
]
