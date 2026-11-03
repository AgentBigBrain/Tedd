"""Legacy packet fingerprinting, journaling, and capture coordination."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import cast

from .legacy_mutation_contracts import (
    CaptureDependencies,
    JsonObject,
    _records,
    _string,
    _to_int,
    require_json_object,
)

_CAPTURE_COLLECTIONS = (
    "entities",
    "entity_redirects",
    "relations",
    "observations",
    "claims",
    "events",
    "decisions",
    "open_loops",
    "receipts",
)
_CAPTURE_CANONICAL_COLLECTIONS = tuple(
    collection for collection in _CAPTURE_COLLECTIONS if collection != "entity_redirects"
)

_CAPTURE_TOP_LEVEL_FIELDS = frozenset({"source", "coverage", *_CAPTURE_COLLECTIONS})
_COVERAGE_VERSION = "v1"
_COVERAGE_CATEGORIES = (
    "identities_relationships_status",
    "verified_current_state",
    "conclusions_risks_unknowns",
    "recommendations_decisions",
    "obligations_next_actions",
    "artifacts_events_receipts",
)
_COVERAGE_COMPATIBLE_COLLECTIONS = {
    "identities_relationships_status": frozenset(
        {"entities", "relations", "observations", "claims"}
    ),
    "verified_current_state": frozenset({"observations", "claims"}),
    "conclusions_risks_unknowns": frozenset({"observations", "claims"}),
    "recommendations_decisions": frozenset({"decisions"}),
    "obligations_next_actions": frozenset({"open_loops"}),
    "artifacts_events_receipts": frozenset({"events", "receipts"}),
}
_COVERAGE_DISPOSITIONS = frozenset({"captured", "existing", "omitted", "not_applicable"})
_COVERAGE_OMISSION_REASONS = frozenset(
    {
        "unsupported_inference",
        "insufficient_authority",
        "transient",
        "no_actionable_owner_or_checkpoint",
        "out_of_scope",
        "privacy_or_security",
    }
)
_COVERAGE_CAPTURE_PATH = re.compile(
    r"^(entities|relations|observations|claims|events|decisions|open_loops|receipts)\[(\d+)\]$"
)
_COVERAGE_INELIGIBLE_LIFECYCLES = frozenset(
    {"conflicting", "quarantined", "redacted", "redirected", "superseded"}
)
_PROPOSAL_REQUIRED_CATEGORIES = frozenset(
    {
        "identities_relationships_status",
        "verified_current_state",
        "conclusions_risks_unknowns",
        "artifacts_events_receipts",
    }
)
_FINGERPRINT_ONLY_GRAPH_KEY = "_capture_fingerprint_only"


@dataclass(frozen=True)
class _CaptureRequest:
    authority_ceiling: str
    allow_replace: bool
    allow_redirect: bool
    producer_record: dict[str, str]
    based_on_watermark: int | None
    discovery_kind: str
    source_raw: JsonObject
    records: dict[str, list[JsonObject]]
    redaction_count: int
    policy_adjustments: list[str]
    capture_policy: JsonObject
    coverage: JsonObject | None


@dataclass(frozen=True)
class _CaptureFingerprint:
    value: str
    uses_extended_format: bool


@dataclass(frozen=True)
class _CaptureSource:
    captured_at: str
    observed_at: str
    source_id: str
    record: JsonObject


class LegacyCaptureService:
    """Legacy packet fingerprinting, journaling, and capture coordination."""

    def __init__(self, dependencies: CaptureDependencies) -> None:
        self.dependencies = dependencies

    def packet_records(self, packet: JsonObject, collection: str) -> list[JsonObject]:
        value = packet.get(collection, [])
        if not isinstance(value, list):
            raise ValueError(f"{collection} must be a list")
        if len(value) > 500:
            raise ValueError(f"{collection} exceeds the 500-record packet limit")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{collection}[{index}] must be an object")
        return cast(list[JsonObject], value)

    def _coverage_is_required(
        self, records: dict[str, list[JsonObject]], discovery_kind: str
    ) -> bool:
        if discovery_kind == "substantial_analysis":
            return True
        return self._is_substantive_review(records)

    def _classification_tokens(self, value: object) -> set[str]:
        text = self.dependencies.normalize_space(value)
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
        return set(re.findall(r"[a-z0-9]+", self.dependencies.normalize_key(text)))

    def _is_substantive_review(self, records: dict[str, list[JsonObject]]) -> bool:
        for event in records["events"]:
            kind_tokens = self._classification_tokens(event.get("kind"))
            status_tokens = self._classification_tokens(event.get("status"))
            tokens = kind_tokens | status_tokens
            pending_tokens = {
                "requested",
                "scheduled",
                "started",
                "failed",
                "inprogress",
                "canceled",
                "cancelled",
                "pending",
            }
            in_progress = {"in", "progress"}.issubset(kind_tokens) or {
                "in",
                "progress",
            }.issubset(status_tokens)
            if tokens.intersection(pending_tokens) or in_progress:
                continue

            terminal_tokens = {
                "completed",
                "complete",
                "produced",
                "finalized",
                "result",
                "reviewed",
                "analyzed",
                "assessed",
                "audited",
            }
            semantic_stems = {
                "reviewed": "review",
                "analyzed": "analysis",
                "assessed": "assessment",
                "audited": "audit",
            }
            stemmed_kind_tokens = kind_tokens | {
                semantic_stems[token] for token in kind_tokens if token in semantic_stems
            }
            semantic_kind_tokens = stemmed_kind_tokens.intersection(
                {"analysis", "assessment", "audit", "review", "report"}
            )
            if {"proposal", "review"}.issubset(stemmed_kind_tokens) or (
                semantic_kind_tokens
                and (
                    tokens.intersection(terminal_tokens)
                    or (
                        "report" in kind_tokens
                        and stemmed_kind_tokens.intersection(
                            {"analysis", "assessment", "audit", "review"}
                        )
                    )
                )
            ):
                return True
        return False

    def _validate_coverage_dispositions(
        self,
        items: list[JsonObject],
        records: dict[str, list[JsonObject]],
        discovery_kind: str,
    ) -> None:
        if all(item.get("disposition") == "not_applicable" for item in items):
            raise ValueError(
                "A substantive coverage ledger cannot mark all categories not_applicable"
            )
        category_dispositions: dict[str, set[str]] = {
            category: set() for category in _COVERAGE_CATEGORIES
        }
        for item in items:
            category_dispositions[_string(item["category"])].add(_string(item["disposition"]))
        if discovery_kind == "substantial_analysis" or self._is_substantive_review(records):
            missing = sorted(
                category
                for category in _PROPOSAL_REQUIRED_CATEGORIES
                if not category_dispositions[category].intersection({"captured", "existing"})
            )
            if missing:
                raise ValueError(
                    "Substantive coverage requires captured or existing records for: "
                    + ", ".join(missing)
                )
        for category, dispositions in category_dispositions.items():
            if "not_applicable" in dispositions and len(dispositions) > 1:
                raise ValueError(
                    f"Coverage category {category!r} cannot combine not_applicable with "
                    "another disposition"
                )
        exact_packet_categories = {
            "recommendations_decisions": bool(records["decisions"]),
            "obligations_next_actions": bool(records["open_loops"]),
            "artifacts_events_receipts": bool(records["events"] or records["receipts"]),
        }
        for category, has_records in exact_packet_categories.items():
            dispositions = category_dispositions[category]
            if has_records and not dispositions.intersection({"captured", "existing"}):
                raise ValueError(
                    f"Coverage category {category!r} requires captured or existing coverage "
                    "when its compatible packet records exist"
                )

    def _coverage_record_is_eligible(
        self, category: str, collection: str, record: JsonObject
    ) -> bool:
        lifecycle = self.dependencies.normalize_key(record.get("lifecycle"))
        if lifecycle in _COVERAGE_INELIGIBLE_LIFECYCLES:
            return False
        if category == "obligations_next_actions":
            status = self.dependencies.normalize_key(record.get("status"))
            if status == "resolved" or record.get("resolved_at"):
                return False
        if category == "verified_current_state":
            source_tier = self.dependencies.source_tier(record.get("source_tier"))
            if (
                self.dependencies.source_tiers[source_tier]
                < self.dependencies.source_tiers["validated_structured_candidate"]
            ):
                return False
            if collection == "claims" and (
                record.get("active") is not True or lifecycle != "current"
            ):
                return False
        return collection in _COVERAGE_COMPATIBLE_COLLECTIONS[category]

    def _normalize_coverage(
        self,
        packet: JsonObject,
        records: dict[str, list[JsonObject]],
        discovery_kind: str,
    ) -> JsonObject | None:
        raw_coverage = packet.get("coverage")
        required = self._coverage_is_required(records, discovery_kind)
        if raw_coverage is None:
            if required:
                raise ValueError(
                    "A v1 semantic coverage ledger is required for substantial analysis "
                    "and proposal-review captures"
                )
            return None
        if not isinstance(raw_coverage, dict):
            raise ValueError("coverage must be an object")
        coverage = cast(JsonObject, raw_coverage)
        unknown_coverage = sorted(set(coverage) - {"version", "items"})
        if unknown_coverage:
            raise ValueError("Unknown coverage fields: " + ", ".join(unknown_coverage))
        version = self.dependencies.normalize_key(coverage.get("version"))
        if version != _COVERAGE_VERSION:
            raise ValueError(f"coverage.version must be {_COVERAGE_VERSION!r}")
        raw_items = coverage.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("coverage.items must be a list")
        if not 6 <= len(raw_items) <= 120:
            raise ValueError("coverage.items must contain between 6 and 120 items")

        normalized_items: list[JsonObject] = []
        seen_categories: set[str] = set()
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise ValueError(f"coverage.items[{index}] must be an object")
            item = cast(JsonObject, raw_item)
            category = self.dependencies.normalize_key(item.get("category"))
            if category not in _COVERAGE_CATEGORIES:
                raise ValueError(f"coverage.items[{index}].category is unsupported: {category!r}")
            summary = self.dependencies.normalize_space(item.get("summary"))
            if not summary or len(summary) > 240:
                raise ValueError(f"coverage.items[{index}].summary must contain 1-240 characters")
            disposition = self.dependencies.normalize_key(item.get("disposition"))
            if disposition not in _COVERAGE_DISPOSITIONS:
                raise ValueError(
                    f"coverage.items[{index}].disposition is unsupported: {disposition!r}"
                )
            normalized: JsonObject = {
                "category": category,
                "summary": summary,
                "disposition": disposition,
            }
            allowed_fields = {"category", "summary", "disposition"}
            if disposition == "captured":
                allowed_fields.add("path")
                path = self.dependencies.normalize_space(item.get("path"))
                match = _COVERAGE_CAPTURE_PATH.fullmatch(path)
                if match is None:
                    raise ValueError(
                        f"coverage.items[{index}].path must identify one packet record"
                    )
                collection, raw_record_index = match.groups()
                record_index = int(raw_record_index)
                if record_index >= len(records[collection]):
                    raise ValueError(f"coverage.items[{index}].path does not exist in this packet")
                if collection not in _COVERAGE_COMPATIBLE_COLLECTIONS[category]:
                    raise ValueError(
                        f"coverage category {category!r} cannot be satisfied by {collection}"
                    )
                normalized["path"] = f"{collection}[{record_index}]"
            elif disposition == "existing":
                allowed_fields.add("record_id")
                record_id = self.dependencies.explicit_record_id(item.get("record_id"))
                if not record_id:
                    raise ValueError(
                        f"coverage.items[{index}].record_id must be an exact canonical ID"
                    )
                normalized["record_id"] = record_id
            else:
                allowed_fields.update({"reason", "explanation"})
                reason = self.dependencies.normalize_key(item.get("reason"))
                explanation = self.dependencies.normalize_space(item.get("explanation"))
                if disposition == "not_applicable":
                    if reason != "no_material_outcome":
                        raise ValueError(
                            f"coverage.items[{index}] not_applicable requires "
                            "reason 'no_material_outcome'"
                        )
                elif reason not in _COVERAGE_OMISSION_REASONS:
                    raise ValueError(f"coverage.items[{index}].reason is unsupported: {reason!r}")
                if not explanation or len(explanation) > 400:
                    raise ValueError(
                        f"coverage.items[{index}].explanation must contain 1-400 characters"
                    )
                normalized["reason"] = reason
                normalized["explanation"] = explanation
            unknown_fields = sorted(set(item) - allowed_fields)
            if unknown_fields:
                raise ValueError(
                    f"Unknown coverage.items[{index}] fields: " + ", ".join(unknown_fields)
                )
            normalized_items.append(normalized)
            seen_categories.add(category)
        missing_categories = sorted(set(_COVERAGE_CATEGORIES) - seen_categories)
        if missing_categories:
            raise ValueError(
                "coverage.items must account for every semantic category; missing: "
                + ", ".join(missing_categories)
            )
        self._validate_coverage_dispositions(normalized_items, records, discovery_kind)
        return {"version": _COVERAGE_VERSION, "items": normalized_items}

    def _resolve_coverage(
        self,
        coverage: JsonObject | None,
        graph: JsonObject,
        record_ids_by_path: dict[str, list[str]],
    ) -> JsonObject | None:
        if coverage is None:
            return None
        record_by_id: dict[str, tuple[str, JsonObject]] = {}
        for collection in _CAPTURE_CANONICAL_COLLECTIONS:
            for record in _records(graph, collection):
                record_id = self.dependencies.normalize_space(record.get("id"))
                if record_id:
                    record_by_id[record_id] = (collection, record)
        resolved_items: list[JsonObject] = []
        disposition_counts = {disposition: 0 for disposition in _COVERAGE_DISPOSITIONS}
        category_counts = {category: 0 for category in _COVERAGE_CATEGORIES}
        for raw_item in cast(list[object], coverage["items"]):
            item = cast(JsonObject, raw_item)
            category = _string(item["category"])
            disposition = _string(item["disposition"])
            resolved: JsonObject = {
                "category": category,
                "disposition": disposition,
            }
            if disposition == "captured":
                path = _string(item["path"])
                resolved["path"] = path
                record_ids = record_ids_by_path.get(path, [])
                if not record_ids:
                    raise ValueError(
                        f"Coverage path {path!r} did not resolve to a canonical record"
                    )
                for record_id in record_ids:
                    resolved_record = record_by_id.get(record_id)
                    if resolved_record is None or not self._coverage_record_is_eligible(
                        category, resolved_record[0], resolved_record[1]
                    ):
                        raise ValueError(
                            f"Coverage path {path!r} resolved to semantically ineligible "
                            f"record {record_id!r}"
                        )
                resolved["record_ids"] = self.dependencies.unique_strings(record_ids)
            elif disposition == "existing":
                record_id = _string(item["record_id"])
                existing_record = record_by_id.get(record_id)
                if existing_record is None or not self._coverage_record_is_eligible(
                    category, existing_record[0], existing_record[1]
                ):
                    raise ValueError(
                        f"Coverage existing record {record_id!r} is not a semantically eligible "
                        "current canonical record for this category"
                    )
                resolved["record_ids"] = [record_id]
            elif "reason" in item:
                resolved["reason"] = item["reason"]
            resolved_items.append(resolved)
            disposition_counts[disposition] += 1
            category_counts[category] += 1
        return {
            "version": _COVERAGE_VERSION,
            "items": resolved_items,
            "disposition_counts": disposition_counts,
            "category_counts": category_counts,
        }

    def _validate_existing_coverage(self, coverage: JsonObject | None, graph: JsonObject) -> None:
        if coverage is None:
            return
        record_by_id: dict[str, tuple[str, JsonObject]] = {}
        for collection in _CAPTURE_CANONICAL_COLLECTIONS:
            for record in _records(graph, collection):
                record_id = self.dependencies.normalize_space(record.get("id"))
                if record_id:
                    record_by_id[record_id] = (collection, record)
        for raw_item in cast(list[object], coverage["items"]):
            item = cast(JsonObject, raw_item)
            if item.get("disposition") != "existing":
                continue
            record_id = _string(item["record_id"])
            category = _string(item["category"])
            existing_record = record_by_id.get(record_id)
            if existing_record is None or not self._coverage_record_is_eligible(
                category, existing_record[0], existing_record[1]
            ):
                raise ValueError(
                    f"Coverage existing record {record_id!r} is not a semantically eligible "
                    "current canonical record for this category"
                )

    def canonicalize_packet_timestamps(
        self, source_raw: JsonObject, records: dict[str, list[JsonObject]]
    ) -> None:
        source_observed_at = self.dependencies.parse_time(source_raw.get("observed_at"))
        source_raw["observed_at"] = source_observed_at
        identity_fields = {
            "observations": "observed_at",
            "claims": "valid_from",
            "events": "occurred_at",
            "decisions": "decided_at",
            "open_loops": "created_at",
            "receipts": "verified_at",
        }
        for collection, field in identity_fields.items():
            for record in records[collection]:
                record[field] = self.dependencies.parse_time(record.get(field), source_observed_at)
        optional_fields = {
            "observations": ("asserted_at",),
            "claims": ("asserted_at",),
            "open_loops": ("due_at", "checkpoint_at"),
        }
        for collection, fields in optional_fields.items():
            for record in records[collection]:
                for field in fields:
                    if record.get(field):
                        record[field] = self.dependencies.parse_time(record[field])

    def canonical_fingerprint_packet(
        self, source_raw: JsonObject, records: dict[str, list[JsonObject]]
    ) -> JsonObject:
        def reference_list(raw: JsonObject, field: str) -> list[str]:
            refs = raw.get(field, [])
            if not isinstance(refs, list):
                raise ValueError(f"{field} must be a list")
            return sorted(set(self.dependencies.unique_strings(refs)))

        def entity_refs(raw: JsonObject) -> list[str]:
            return reference_list(raw, "entity_refs")

        def aliases(raw: JsonObject) -> list[str]:
            values = raw.get("aliases", [])
            if not isinstance(values, list):
                raise ValueError("Entity aliases must be a list")
            return self.dependencies.unique_strings(values)

        normalized: JsonObject = {
            "source": {
                "kind": self.dependencies.normalize_key(source_raw.get("kind")) or "other",
                "role": self.dependencies.normalize_key(source_raw.get("role")) or "unknown",
                "title": self.dependencies.normalize_space(source_raw.get("title"))
                or "Captured source",
                "uri": self.dependencies.normalize_space(source_raw.get("uri")),
                "observed_at": source_raw["observed_at"],
                "source_tier": self.dependencies.source_tier(source_raw.get("source_tier")),
                "excerpt": self.dependencies.normalize_space(source_raw.get("excerpt"))[:1200],
                "sensitive": bool(source_raw.get("sensitive", False)),
            },
            "entities": [],
            "relations": [],
            "observations": [],
            "claims": [],
            "events": [],
            "decisions": [],
            "open_loops": [],
            "receipts": [],
        }
        for raw in records["entities"]:
            normalized_entity: JsonObject = {
                "id": self.dependencies.explicit_record_id(raw.get("id")),
                "ref": self.dependencies.normalize_space(raw.get("ref")),
                "type": self.dependencies.normalize_key(raw.get("type")) or "thing",
                "namespace": self.dependencies.normalize_key(raw.get("namespace")) or "global",
                "name": self.dependencies.normalize_space(raw.get("name")),
                "aliases": aliases(raw),
                "status": self.dependencies.normalize_key(raw.get("status")) or "current",
                "sensitive": bool(raw.get("sensitive", False)),
            }
            if "identity_keys" in raw:
                normalized_entity["identity_keys"] = [
                    {
                        "scheme": signature[0],
                        "namespace": signature[1],
                        "value": signature[2],
                    }
                    for signature in self.dependencies.normalized_identity_keys(
                        raw, _string(normalized_entity["namespace"])
                    )
                ]
            _records(normalized, "entities").append(normalized_entity)
        if records.get("entity_redirects"):
            normalized["entity_redirects"] = [
                {
                    "from": self.dependencies.normalize_space(raw.get("from")),
                    "to": self.dependencies.normalize_space(raw.get("to")),
                    "reason": self.dependencies.normalize_space(raw.get("reason")),
                }
                for raw in records["entity_redirects"]
            ]
        for raw in records["relations"]:
            _records(normalized, "relations").append(
                {
                    "source": self.dependencies.normalize_space(raw.get("source")),
                    "target": self.dependencies.normalize_space(raw.get("target")),
                    "type": self.dependencies.normalize_key(raw.get("type")),
                    "status": self.dependencies.normalize_key(raw.get("status")) or "current",
                    "sensitive": bool(raw.get("sensitive", False)),
                }
            )
        for raw in records["observations"]:
            _records(normalized, "observations").append(
                {
                    "family": self.dependencies.normalize_key(raw.get("family")),
                    "key": self.dependencies.normalize_key(raw.get("key")),
                    "value": self.dependencies.normalize_space(raw.get("value")),
                    "observed_at": raw["observed_at"],
                    "asserted_at": self.dependencies.normalize_space(raw.get("asserted_at"))
                    or None,
                    "source_tier": self.dependencies.source_tier(raw.get("source_tier")),
                    "entity_refs": entity_refs(raw),
                    "sensitive": bool(raw.get("sensitive", False)),
                }
            )
        for raw in records["claims"]:
            normalized_claim = {
                "family": self.dependencies.normalize_key(raw.get("family")),
                "key": self.dependencies.normalize_key(raw.get("key")),
                "value": self.dependencies.normalize_space(raw.get("value")),
                "valid_from": raw["valid_from"],
                "asserted_at": self.dependencies.normalize_space(raw.get("asserted_at")) or None,
                "conflict_policy": self.dependencies.normalize_key(raw.get("conflict_policy"))
                or "preserve",
                "source_tier": self.dependencies.source_tier(raw.get("source_tier")),
                "entity_refs": entity_refs(raw),
                "sensitive": bool(raw.get("sensitive", False)),
            }
            if "surface_id" in raw:
                normalized_claim["surface_id"] = self.dependencies.explicit_record_id(
                    raw.get("surface_id")
                )
            if "subject_refs" in raw:
                normalized_claim["subject_refs"] = reference_list(raw, "subject_refs")
            _records(normalized, "claims").append(normalized_claim)
        for raw in records["events"]:
            _records(normalized, "events").append(
                {
                    "id": self.dependencies.explicit_record_id(raw.get("id")),
                    "kind": self.dependencies.normalize_key(raw.get("kind")) or "event",
                    "title": self.dependencies.normalize_space(raw.get("title")),
                    "summary": self.dependencies.normalize_space(raw.get("summary")),
                    "occurred_at": raw["occurred_at"],
                    "status": self.dependencies.normalize_key(raw.get("status")) or "recorded",
                    "entity_refs": entity_refs(raw),
                    "sensitive": bool(raw.get("sensitive", False)),
                }
            )
        for raw in records["decisions"]:
            normalized_decision: JsonObject = {
                "id": self.dependencies.explicit_record_id(raw.get("id")),
                "title": self.dependencies.normalize_space(raw.get("title")),
                "summary": self.dependencies.normalize_space(raw.get("summary")),
                "rationale": self.dependencies.normalize_space(raw.get("rationale")),
                "decided_at": raw["decided_at"],
                "status": self.dependencies.normalize_key(raw.get("status")) or "recorded",
                "entity_refs": entity_refs(raw),
                "sensitive": bool(raw.get("sensitive", False)),
            }
            if "source_tier" in raw:
                normalized_decision["source_tier"] = self.dependencies.source_tier(
                    raw.get("source_tier")
                )
            _records(normalized, "decisions").append(normalized_decision)
        for raw in records["open_loops"]:
            normalized_loop: JsonObject = {
                "id": self.dependencies.explicit_record_id(raw.get("id")),
                "title": self.dependencies.normalize_space(raw.get("title")),
                "summary": self.dependencies.normalize_space(raw.get("summary")),
                "created_at": raw["created_at"],
                "status": self.dependencies.normalize_key(raw.get("status")) or "open",
                "priority": _to_int(raw.get("priority", 1)),
                "due_at": self.dependencies.normalize_space(raw.get("due_at")) or None,
                "owner_refs_present": "owner_refs" in raw,
                "owner_refs": reference_list(raw, "owner_refs"),
                "next_action_present": "next_action" in raw,
                "next_action": self.dependencies.normalize_space(raw.get("next_action")),
                "checkpoint_at_present": "checkpoint_at" in raw,
                "checkpoint_at": self.dependencies.normalize_space(raw.get("checkpoint_at"))
                or None,
                "terminal_condition_present": "terminal_condition" in raw,
                "terminal_condition": self.dependencies.normalize_space(
                    raw.get("terminal_condition")
                ),
                "entity_refs": entity_refs(raw),
                "sensitive": bool(raw.get("sensitive", False)),
            }
            if "source_tier" in raw:
                normalized_loop["source_tier"] = self.dependencies.source_tier(
                    raw.get("source_tier")
                )
            _records(normalized, "open_loops").append(normalized_loop)
        for raw in records["receipts"]:
            _records(normalized, "receipts").append(
                {
                    "id": self.dependencies.explicit_record_id(raw.get("id")),
                    "kind": self.dependencies.normalize_key(raw.get("kind")) or "receipt",
                    "title": self.dependencies.normalize_space(raw.get("title")),
                    "summary": self.dependencies.normalize_space(raw.get("summary")),
                    "verified_at": raw["verified_at"],
                    "status": self.dependencies.normalize_key(raw.get("status")) or "recorded",
                    "uri": self.dependencies.normalize_space(raw.get("uri")),
                    "verified": raw.get("verified", False),
                    "entity_refs": entity_refs(raw),
                    "sensitive": bool(raw.get("sensitive", False)),
                }
            )
        return normalized

    def normalize_producer(self, value: JsonObject | None) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Producer provenance must be an object")
        result: dict[str, str] = {}
        for key in ("agent_id", "task_id", "parent_task_id", "run_id"):
            text = self.dependencies.normalize_space(value.get(key))
            if len(text) > 200:
                raise ValueError(f"Producer {key} exceeds 200 characters")
            if text:
                result[key] = text
        return result

    def append_journal_entry(self, graph: JsonObject, entry: JsonObject) -> int:
        watermark = _to_int(graph.get("next_watermark", 1))
        graph["next_watermark"] = watermark + 1
        prior_hash = (
            self.dependencies.normalize_space(_records(graph, "journal")[-1].get("entry_hash"))
            if graph.get("journal")
            else self.dependencies.normalize_space(
                cast(JsonObject, graph.get("journal_anchor", {})).get("entry_hash")
            )
        )
        record = {
            **entry,
            "watermark": watermark,
            "captured_at": entry.get("captured_at") or self.dependencies.utc_now(),
        }
        record.setdefault(
            "id",
            self.dependencies.stable_id(
                "journal",
                {
                    "watermark": watermark,
                    "action": record.get("action"),
                    "fingerprint": record.get("fingerprint"),
                },
            ),
        )
        record["previous_entry_hash"] = prior_hash
        record["entry_hash"] = self.dependencies.journal_entry_hash(record, prior_hash)
        _records(graph, "journal").append(record)
        return watermark

    def _apply_capture_policy(
        self,
        source_raw: JsonObject,
        records: dict[str, list[JsonObject]],
        authority_ceiling: str,
        allow_replace: bool,
        allow_redirect: bool,
        redaction_count: int,
    ) -> tuple[int, list[str]]:
        policy_adjustments: list[str] = []
        effective_source_tier, source_was_capped = self.dependencies.capped_source_tier(
            source_raw.get("source_tier"), authority_ceiling
        )
        if source_was_capped:
            policy_adjustments.append("source_tier_capped")
        source_raw["source_tier"] = effective_source_tier
        for collection in ("observations", "claims"):
            for index, record in enumerate(records[collection]):
                effective_tier, was_capped = self.dependencies.capped_source_tier(
                    record.get("source_tier"), effective_source_tier, effective_source_tier
                )
                if was_capped:
                    policy_adjustments.append(f"{collection}[{index}].source_tier_capped")
                record["source_tier"] = effective_tier
                is_sensitive = self.dependencies.sensitive_field(
                    record.get("key")
                ) or self.dependencies.sensitive_field(record.get("family"))
                if is_sensitive and record.get("value") not in (None, "", "[redacted secret]"):
                    record["value"] = "[redacted secret]"
                    redaction_count += 1
                    policy_adjustments.append(
                        f"{collection}[{index}].semantic_secret_value_redacted"
                    )
        for collection in ("decisions", "open_loops"):
            for index, record in enumerate(records[collection]):
                if "source_tier" not in record:
                    continue
                effective_tier, was_capped = self.dependencies.capped_source_tier(
                    record.get("source_tier"), effective_source_tier, effective_source_tier
                )
                if was_capped:
                    policy_adjustments.append(f"{collection}[{index}].source_tier_capped")
                record["source_tier"] = effective_tier
        if self.dependencies.normalize_key(source_raw.get("role")) == "assistant":
            for collection in ("decisions", "open_loops"):
                for index, record in enumerate(records[collection]):
                    if record.get("source_tier") != "assistant_inference":
                        record["source_tier"] = "assistant_inference"
                        policy_adjustments.append(
                            f"{collection}[{index}].assistant_role_tier_capped"
                        )
        if not allow_replace:
            for index, record in enumerate(records["claims"]):
                if self.dependencies.normalize_key(record.get("conflict_policy")) == "replace":
                    record["conflict_policy"] = "preserve"
                    policy_adjustments.append(f"claims[{index}].replace_downgraded_to_preserve")
        if records["entity_redirects"] and not allow_redirect:
            raise ValueError("Entity redirects require caller-controlled --allow-redirect")
        for index, record in enumerate(records["claims"]):
            if not self.dependencies.normalize_space(record.get("surface_id")):
                policy_adjustments.append(f"claims[{index}].legacy_surface_derived")
        if (
            self.dependencies.source_tiers[effective_source_tier]
            < self.dependencies.source_tiers["validated_structured_candidate"]
        ):
            for index, record in enumerate(records["receipts"]):
                if record.get("verified") is True:
                    record["verified"] = False
                    policy_adjustments.append(f"receipts[{index}].verified_downgraded_to_false")
        return redaction_count, policy_adjustments

    def _prepare_capture_request(
        self,
        graph: JsonObject,
        raw_packet: JsonObject,
        authority_ceiling: str = "assistant_inference",
        allow_replace: bool = False,
        allow_redirect: bool = False,
        producer: JsonObject | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
        validate_existing_coverage: bool = True,
        validate_graph_state: bool = True,
    ) -> _CaptureRequest:
        authority_ceiling = self.dependencies.source_tier(authority_ceiling)
        scrubbed_packet, redaction_count = self.dependencies.scrub_value(
            raw_packet, redact_paths=False
        )
        packet = require_json_object(scrubbed_packet, "Capture packet")
        unknown_top_level = sorted(set(packet) - _CAPTURE_TOP_LEVEL_FIELDS)
        if unknown_top_level:
            raise ValueError("Unknown capture packet fields: " + ", ".join(unknown_top_level))
        records = {
            collection: self.dependencies.packet_records(packet, collection)
            for collection in _CAPTURE_COLLECTIONS
        }
        producer_record = self.dependencies.normalize_producer(producer)
        normalized_discovery_kind = self.dependencies.normalize_key(discovery_kind)
        if (
            normalized_discovery_kind
            and normalized_discovery_kind not in self.dependencies.discovery_kinds
        ):
            raise ValueError(f"Unsupported discovery kind: {normalized_discovery_kind}")
        coverage = self._normalize_coverage(packet, records, normalized_discovery_kind)
        if validate_existing_coverage:
            self._validate_existing_coverage(coverage, graph)
        if validate_graph_state and based_on_watermark is not None:
            if based_on_watermark < 0 or based_on_watermark > self.dependencies.committed_watermark(
                graph
            ):
                raise ValueError(
                    "based_on_watermark must be between zero and the current committed watermark"
                )
        source_raw = packet.get("source") or {}
        if not isinstance(source_raw, dict):
            raise ValueError("source must be an object")
        source_raw = cast(JsonObject, source_raw)
        if not self.dependencies.normalize_space(source_raw.get("observed_at")):
            raise ValueError("source.observed_at is required for deterministic capture")
        if coverage is not None:
            for collection in ("decisions", "open_loops"):
                missing_tiers = [
                    index
                    for index, record in enumerate(records[collection])
                    if "source_tier" not in record
                ]
                if missing_tiers:
                    raise ValueError(
                        f"Coverage-bearing {collection} records must explicitly declare "
                        f"source_tier; missing at indices {missing_tiers}"
                    )
        self.dependencies.canonicalize_packet_timestamps(source_raw, records)
        redaction_count, policy_adjustments = self._apply_capture_policy(
            source_raw,
            records,
            authority_ceiling,
            allow_replace,
            allow_redirect,
            redaction_count,
        )
        capture_policy: JsonObject = {
            "authority_ceiling": authority_ceiling,
            "allow_replace": bool(allow_replace),
        }
        if allow_redirect:
            capture_policy["allow_redirect"] = True
        return _CaptureRequest(
            authority_ceiling=authority_ceiling,
            allow_replace=allow_replace,
            allow_redirect=allow_redirect,
            producer_record=producer_record,
            based_on_watermark=based_on_watermark,
            discovery_kind=normalized_discovery_kind,
            source_raw=source_raw,
            records=records,
            redaction_count=redaction_count,
            policy_adjustments=policy_adjustments,
            capture_policy=capture_policy,
            coverage=coverage,
        )

    def _capture_fingerprint(self, request: _CaptureRequest) -> str:
        fingerprint_packet = self.dependencies.canonical_fingerprint_packet(
            request.source_raw, request.records
        )
        if request.coverage is not None:
            fingerprint_packet["coverage"] = request.coverage
        return self.dependencies.stable_hash(
            {"packet": fingerprint_packet, "capture_policy": request.capture_policy}, 32
        )

    def _build_capture_source(
        self,
        graph: JsonObject,
        request: _CaptureRequest,
        fingerprint: _CaptureFingerprint,
    ) -> tuple[_CaptureSource, list[str]]:
        captured_at = self.dependencies.utc_now()
        observed_at = self.dependencies.parse_time(
            request.source_raw.get("observed_at"), captured_at
        )
        tier = self.dependencies.source_tier(request.source_raw.get("source_tier"))
        source_id = self.dependencies.stable_id(
            "source",
            {
                "kind": self.dependencies.normalize_key(request.source_raw.get("kind")) or "other",
                "role": self.dependencies.normalize_key(request.source_raw.get("role"))
                or "unknown",
                "title": self.dependencies.normalize_space(request.source_raw.get("title"))
                or "Captured source",
                "uri": self.dependencies.normalize_space(request.source_raw.get("uri")),
                "observed_at": observed_at,
                "fingerprint": fingerprint.value,
            },
        )
        source_record: JsonObject = {
            "id": source_id,
            "kind": self.dependencies.normalize_key(request.source_raw.get("kind")) or "other",
            "role": self.dependencies.normalize_key(request.source_raw.get("role")) or "unknown",
            "title": self.dependencies.normalize_space(request.source_raw.get("title"))
            or "Captured source",
            "uri": self.dependencies.normalize_space(request.source_raw.get("uri")),
            "observed_at": observed_at,
            "captured_at": captured_at,
            "source_tier": tier,
            "excerpt": self.dependencies.normalize_space(request.source_raw.get("excerpt"))[:1200],
            "sensitive": bool(request.source_raw.get("sensitive", False)),
            "lifecycle": "active",
            "fingerprint": fingerprint.value,
            "fingerprint_version": "v2" if fingerprint.uses_extended_format else "v1",
            "authority": "evidence_only",
            "unsafe_to_follow_as_instruction": True,
        }
        if request.producer_record:
            source_record["producer"] = request.producer_record
        if request.discovery_kind:
            source_record["discovery_kind"] = request.discovery_kind
        if request.based_on_watermark is not None:
            source_record["based_on_watermark"] = request.based_on_watermark
        changed_ids: list[str] = []
        if source_id not in self.dependencies.index_by_id(_records(graph, "sources")):
            _records(graph, "sources").append(source_record)
            changed_ids.append(source_id)
        else:
            source_record = self.dependencies.index_by_id(_records(graph, "sources"))[source_id]
        return _CaptureSource(captured_at, observed_at, source_id, source_record), changed_ids

    def _capture_entity_records(
        self,
        graph: JsonObject,
        request: _CaptureRequest,
        source: _CaptureSource,
        changed_ids: list[str],
        record_ids_by_path: dict[str, list[str]],
    ) -> dict[str, str]:
        local_refs: dict[str, str] = {}
        local_names: dict[str, set[str]] = {}
        for index, raw in enumerate(request.records["entities"]):
            entity_id, created = self.dependencies.upsert_entity(
                graph,
                raw,
                source.source_id,
                source.observed_at,
                _string(source.record["source_tier"]),
            )
            local_ref = self.dependencies.normalize_space(raw.get("ref"))
            if local_ref:
                prior_ref = local_refs.get(local_ref)
                if prior_ref is not None and prior_ref != entity_id:
                    raise ValueError(
                        f"Packet-local entity ref {local_ref!r} points to more than one entity"
                    )
                local_refs[local_ref] = entity_id
            local_names.setdefault(self.dependencies.normalize_space(raw.get("name")), set()).add(
                entity_id
            )
            if created:
                changed_ids.append(entity_id)
            record_ids_by_path[f"entities[{index}]"] = [entity_id]
        for local_name, entity_ids in local_names.items():
            if len(entity_ids) == 1:
                local_refs.setdefault(local_name, next(iter(entity_ids)))
        for index, raw in enumerate(request.records["entity_redirects"]):
            entity_id, changed = self.dependencies.add_entity_redirect(
                graph, raw, local_refs, source.record
            )
            if changed:
                changed_ids.append(entity_id)
            record_ids_by_path[f"entity_redirects[{index}]"] = [entity_id]
        for index, raw in enumerate(request.records["relations"]):
            relation_id, created = self.dependencies.upsert_relation(
                graph,
                raw,
                local_refs,
                source.source_id,
                source.observed_at,
                _string(source.record["source_tier"]),
            )
            if created:
                changed_ids.append(relation_id)
            record_ids_by_path[f"relations[{index}]"] = [relation_id]
        for index, raw in enumerate(request.records["observations"]):
            observation_id, created = self.dependencies.add_observation(
                graph, raw, local_refs, source.record
            )
            if created:
                changed_ids.append(observation_id)
            record_ids_by_path[f"observations[{index}]"] = [observation_id]
        return local_refs

    def _capture_claim_records(
        self,
        graph: JsonObject,
        records: list[JsonObject],
        local_refs: dict[str, str],
        source_record: JsonObject,
        changed_ids: list[str],
        record_ids_by_path: dict[str, list[str]],
    ) -> dict[str, str]:
        claim_outcomes: dict[str, str] = {}
        prior_claim_hashes = {
            _string(item["id"]): self.dependencies.stable_hash(item, 64)
            for item in _records(graph, "claims")
        }
        prior_surface_hashes = {
            _string(item["id"]): self.dependencies.stable_hash(item, 64)
            for item in _records(graph, "claim_surfaces")
        }
        for index, raw in enumerate(records):
            claim_id, outcome = self.dependencies.add_claim(graph, raw, local_refs, source_record)
            claim_outcomes[claim_id] = outcome
            record_ids_by_path[f"claims[{index}]"] = [claim_id]
        changed_ids.extend(
            _string(item["id"])
            for item in _records(graph, "claims")
            if prior_claim_hashes.get(_string(item["id"]))
            != self.dependencies.stable_hash(item, 64)
        )
        changed_ids.extend(
            _string(item["id"])
            for item in _records(graph, "claim_surfaces")
            if prior_surface_hashes.get(_string(item["id"]))
            != self.dependencies.stable_hash(item, 64)
        )
        return claim_outcomes

    def _capture_generic_records(
        self,
        graph: JsonObject,
        records: dict[str, list[JsonObject]],
        local_refs: dict[str, str],
        source_record: JsonObject,
        changed_ids: list[str],
        record_ids_by_path: dict[str, list[str]],
    ) -> None:
        for collection in ("events", "decisions", "open_loops", "receipts"):
            for index, raw in enumerate(records[collection]):
                record_id, created = self.dependencies.add_generic_record(
                    graph, collection, raw, local_refs, source_record
                )
                if created:
                    changed_ids.append(record_id)
                record_ids_by_path[f"{collection}[{index}]"] = [record_id]

    def _append_capture_journal(
        self,
        graph: JsonObject,
        request: _CaptureRequest,
        fingerprint: _CaptureFingerprint,
        source: _CaptureSource,
        changed_ids: list[str],
        coverage: JsonObject | None,
    ) -> int:
        journal_entry: JsonObject = {
            "action": "capture",
            "captured_at": source.captured_at,
            "fingerprint": fingerprint.value,
            "fingerprint_version": "v2" if fingerprint.uses_extended_format else "v1",
            "source_ref": source.source_id,
            "changed_ids": self.dependencies.unique_strings(changed_ids),
            "redaction_count": request.redaction_count,
            "capture_policy": request.capture_policy,
            "policy_adjustments": request.policy_adjustments,
        }
        if coverage is not None:
            journal_entry["coverage"] = coverage
        if request.producer_record:
            journal_entry["producer"] = request.producer_record
        if request.discovery_kind:
            journal_entry["discovery_kind"] = request.discovery_kind
        if request.based_on_watermark is not None:
            journal_entry["based_on_watermark"] = request.based_on_watermark
        return self.dependencies.append_journal_entry(graph, journal_entry)

    def _capture_packet_once(
        self,
        graph: JsonObject,
        raw_packet: JsonObject,
        authority_ceiling: str = "assistant_inference",
        allow_replace: bool = False,
        allow_redirect: bool = False,
        producer: JsonObject | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
        *,
        fingerprint_only: bool = False,
    ) -> JsonObject:
        fingerprint_request = self._prepare_capture_request(
            graph,
            raw_packet,
            authority_ceiling,
            allow_replace,
            allow_redirect,
            producer,
            based_on_watermark,
            discovery_kind,
            validate_existing_coverage=False,
            validate_graph_state=not fingerprint_only,
        )
        fingerprint_value = self._capture_fingerprint(fingerprint_request)
        if fingerprint_only:
            fingerprint_result: JsonObject = {
                "status": "fingerprint",
                "fingerprint": fingerprint_value,
                "redactions": fingerprint_request.redaction_count,
                "changed_ids": [],
                "policy_adjustments": fingerprint_request.policy_adjustments,
            }
            if fingerprint_request.coverage is not None:
                fingerprint_result["coverage"] = {
                    "version": _COVERAGE_VERSION,
                    "coverage_digest": self.dependencies.stable_hash(
                        fingerprint_request.coverage, 32
                    ),
                }
            return fingerprint_result
        duplicate = next(
            (
                item
                for item in _records(graph, "journal")
                if item.get("fingerprint") == fingerprint_value
            ),
            None,
        )
        if duplicate:
            duplicate_result: JsonObject = {
                "status": "duplicate",
                "fingerprint": fingerprint_value,
                "watermark": duplicate.get("watermark"),
                "redactions": fingerprint_request.redaction_count,
                "changed_ids": [],
                "policy_adjustments": fingerprint_request.policy_adjustments,
            }
            if isinstance(duplicate.get("coverage"), dict):
                duplicate_result["coverage"] = duplicate["coverage"]
            return duplicate_result
        request = self._prepare_capture_request(
            graph,
            raw_packet,
            authority_ceiling,
            allow_replace,
            allow_redirect,
            producer,
            based_on_watermark,
            discovery_kind,
        )
        uses_extended_format = bool(
            request.coverage is not None
            or request.allow_redirect
            or any("identity_keys" in item for item in request.records["entities"])
            or any(
                "surface_id" in item or "subject_refs" in item for item in request.records["claims"]
            )
            or any("source_tier" in item for item in request.records["decisions"])
            or any("source_tier" in item for item in request.records["open_loops"])
        )
        fingerprint = _CaptureFingerprint(fingerprint_value, uses_extended_format)
        source, changed_ids = self._build_capture_source(graph, request, fingerprint)
        record_ids_by_path: dict[str, list[str]] = {}
        local_refs = self._capture_entity_records(
            graph, request, source, changed_ids, record_ids_by_path
        )
        claim_outcomes = self._capture_claim_records(
            graph,
            request.records["claims"],
            local_refs,
            source.record,
            changed_ids,
            record_ids_by_path,
        )
        self._capture_generic_records(
            graph,
            request.records,
            local_refs,
            source.record,
            changed_ids,
            record_ids_by_path,
        )
        coverage = self._resolve_coverage(request.coverage, graph, record_ids_by_path)
        if coverage is not None:
            coverage["coverage_digest"] = self.dependencies.stable_hash(request.coverage, 32)
        watermark = self._append_capture_journal(
            graph, request, fingerprint, source, changed_ids, coverage
        )
        return {
            "status": "captured",
            "fingerprint": fingerprint.value,
            "watermark": watermark,
            "redactions": request.redaction_count,
            "changed_ids": self.dependencies.unique_strings(changed_ids),
            "claim_outcomes": claim_outcomes,
            "policy_adjustments": request.policy_adjustments,
            **({"coverage": coverage} if coverage is not None else {}),
        }

    def capture_packet(
        self,
        graph: JsonObject,
        raw_packet: JsonObject,
        authority_ceiling: str = "assistant_inference",
        allow_replace: bool = False,
        allow_redirect: bool = False,
        producer: JsonObject | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
    ) -> JsonObject:
        marker_present = _FINGERPRINT_ONLY_GRAPH_KEY in graph
        fingerprint_only = marker_present and (
            graph[_FINGERPRINT_ONLY_GRAPH_KEY] is self.dependencies.fingerprint_only_marker
        )
        if marker_present and not fingerprint_only:
            raise ValueError("Invalid private fingerprint-only graph marker")
        if fingerprint_only:
            expected_blank = self.dependencies.blank_graph()
            candidate = {
                key: value for key, value in graph.items() if key != _FINGERPRINT_ONLY_GRAPH_KEY
            }
            candidate_updated_at = candidate.get("updated_at")
            if isinstance(candidate_updated_at, str) and candidate_updated_at:
                expected_blank["updated_at"] = candidate_updated_at
            if candidate != expected_blank:
                raise ValueError(
                    "Fingerprint-only capture requires an otherwise structurally blank graph"
                )
            return self._capture_packet_once(
                expected_blank,
                raw_packet,
                authority_ceiling,
                allow_replace,
                allow_redirect,
                producer,
                based_on_watermark,
                discovery_kind,
                fingerprint_only=True,
            )
        preflight = self._capture_packet_once(
            copy.deepcopy(graph),
            raw_packet,
            authority_ceiling,
            allow_replace,
            allow_redirect,
            producer,
            based_on_watermark,
            discovery_kind,
        )
        if preflight.get("status") == "duplicate":
            return preflight
        return self._capture_packet_once(
            graph,
            raw_packet,
            authority_ceiling,
            allow_replace,
            allow_redirect,
            producer,
            based_on_watermark,
            discovery_kind,
        )


__all__ = ["LegacyCaptureService"]
