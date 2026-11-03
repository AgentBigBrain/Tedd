"""Pure validation policy for the legacy v3 knowledge-graph representation."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypedDict, cast

JsonObject = dict[object, object]
RecordIndex = dict[str, JsonObject]


class ValidationResult(TypedDict):
    """Stable public result produced by legacy graph validation."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    counts: dict[str, int]


@dataclass(frozen=True)
class ValidationDependencies:
    """Legacy helpers and schema policy required to validate a v3 graph."""

    collections: Sequence[str]
    discovery_kinds: Collection[str]
    graph_schema_version: str
    safe_record_id: re.Pattern[str]
    source_tiers: Mapping[str, int]
    canonical_entity_id: Callable[[JsonObject, str], str]
    canonical_entity_refs: Callable[[JsonObject, Iterable[object]], list[str]]
    identity_key_signature: Callable[[JsonObject, str], tuple[str, str, str]]
    index_by_id: Callable[[list[JsonObject]], RecordIndex]
    journal_entry_hash: Callable[[JsonObject, str], str]
    normalize_key: Callable[[object], str]
    normalize_space: Callable[[object], str]
    parse_time: Callable[[object], str]
    secret_hits: Callable[[object], list[str]]
    time_value: Callable[[str], dt.datetime]


@dataclass
class _ValidationState:
    graph: JsonObject
    dependencies: ValidationDependencies
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    known_ids: set[str] = field(default_factory=set)
    collection_ids: dict[str, set[str]] = field(default_factory=dict)
    collection_records: dict[str, list[JsonObject]] = field(default_factory=dict)
    invalid_list_fields: set[tuple[str, str]] = field(default_factory=set)
    invalid_string_fields: set[tuple[str, str]] = field(default_factory=set)
    validation_graph: JsonObject = field(default_factory=dict)

    def collect_records(self) -> None:
        for collection in self.dependencies.collections:
            if collection not in self.graph:
                self.errors.append(f"missing required top-level collection: {collection}")
            raw_records = self.graph.get(collection, [])
            if not isinstance(raw_records, list):
                self.errors.append(f"{collection} must be a list")
                raw_records = []
            records = [
                cast(JsonObject, record) for record in raw_records if isinstance(record, dict)
            ]
            if len(records) != len(raw_records):
                self.errors.append(f"{collection} contains a non-object record")
            self.collection_records[collection] = records
            ids: set[str] = set()
            for record in records:
                record_id = record.get("id")
                if not isinstance(record_id, str) or not self.dependencies.safe_record_id.fullmatch(
                    record_id
                ):
                    self.errors.append(f"{collection} contains an invalid or missing opaque id")
                    continue
                if record_id in self.known_ids:
                    self.errors.append(f"duplicate id: {record_id}")
                self.known_ids.add(record_id)
                ids.add(record_id)
            self.collection_ids[collection] = ids
        self.validation_graph = {
            **self.graph,
            **{
                collection: self.collection_records[collection]
                for collection in self.dependencies.collections
            },
        }

    def record_list(self, record: JsonObject, field_name: str) -> list[object]:
        value = record.get(field_name, [])
        if isinstance(value, list):
            return cast(list[object], value)
        signature = (str(record.get("id")), field_name)
        if signature not in self.invalid_list_fields:
            self.errors.append(f"{record.get('id')} {field_name} must be a list")
            self.invalid_list_fields.add(signature)
        return []

    def record_string(self, record: JsonObject, field_name: str) -> str:
        value = record.get(field_name)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        signature = (str(record.get("id")), field_name)
        if signature not in self.invalid_string_fields:
            self.errors.append(f"{record.get('id')} {field_name} must be a string")
            self.invalid_string_fields.add(signature)
        return ""

    def record_string_list(self, record: JsonObject, field_name: str) -> list[str]:
        values = self.record_list(record, field_name)
        strings: list[str] = []
        for value in values:
            if not isinstance(value, str):
                self.errors.append(f"{record.get('id')} {field_name} must contain only strings")
                continue
            strings.append(value)
        return strings

    def validate_scalar_fields(self) -> None:
        textual_fields = {
            "action",
            "authority_state",
            "asserted_at",
            "captured_at",
            "checkpoint_at",
            "created_at",
            "decided_at",
            "excerpt",
            "ended_at",
            "family",
            "first_observed_at",
            "first_seen_at",
            "fingerprint",
            "fingerprint_version",
            "key",
            "kind",
            "last_observed_at",
            "last_seen_at",
            "lifecycle",
            "name",
            "namespace",
            "next_action",
            "occurred_at",
            "observed_at",
            "previous_entry_hash",
            "entry_hash",
            "redirect_reason",
            "redirect_source_ref",
            "redirect_to",
            "redirected_at",
            "resolution",
            "resolved_at",
            "source_entity_id",
            "source_ref",
            "source_tier",
            "status",
            "status_source_tier",
            "summary",
            "superseded_by",
            "surface_ref",
            "target_entity_id",
            "terminal_condition",
            "title",
            "type",
            "uri",
            "updated_at",
            "valid_from",
            "valid_to",
            "value",
            "verified_at",
        }
        boolean_fields = {"active", "sensitive", "verified"}
        for records in self.collection_records.values():
            for record in records:
                for field_name, value in record.items():
                    if field_name in textual_fields and value is not None:
                        self.record_string(record, field_name)
                    if field_name in boolean_fields and not isinstance(value, bool):
                        self.errors.append(f"{record.get('id')} {field_name} must be a boolean")
                tier = record.get("source_tier")
                if isinstance(tier, str) and tier and tier not in self.dependencies.source_tiers:
                    self.errors.append(f"{record.get('id')} has invalid source_tier {tier!r}")

    def validate_entities(self) -> None:
        entity_ids = self.collection_ids.get("entities", set())
        entity_by_id = self.dependencies.index_by_id(self.collection_records["entities"])
        for entity in self.collection_records["entities"]:
            lifecycle = self.record_string(entity, "lifecycle") or "active"
            if lifecycle not in {"active", "redirected", "redacted"}:
                self.errors.append(f"{entity.get('id')} has invalid entity lifecycle {lifecycle!r}")
            if lifecycle == "redirected":
                target = self.record_string(entity, "redirect_to")
                if target not in entity_ids:
                    self.errors.append(f"{entity.get('id')} redirects to missing entity {target}")
                elif target == entity.get("id"):
                    self.errors.append(f"{entity.get('id')} redirects to itself")
                elif entity_by_id.get(target, {}).get("lifecycle") == "redirected":
                    self.errors.append(
                        f"{entity.get('id')} redirects through a chain instead of "
                        "a canonical target"
                    )
                elif entity_by_id.get(target, {}).get("lifecycle") == "redacted":
                    self.errors.append(f"{entity.get('id')} redirects to a redacted target")
                else:
                    entity_id = entity.get("id")
                    if isinstance(entity_id, str):
                        try:
                            self.dependencies.canonical_entity_id(self.validation_graph, entity_id)
                        except ValueError as error:
                            self.errors.append(str(error))
            self.record_string_list(entity, "aliases")

        entity_identities: dict[tuple[str, str, str], object] = {}
        identity_keys: dict[tuple[str, str, str], object] = {}
        for entity in self.collection_records["entities"]:
            if self.record_string(entity, "lifecycle") in {"redacted", "redirected"}:
                continue
            identity = (
                self.dependencies.normalize_key(self.record_string(entity, "namespace"))
                or "global",
                self.dependencies.normalize_key(self.record_string(entity, "type")) or "thing",
                self.dependencies.normalize_key(self.record_string(entity, "name")),
            )
            prior = entity_identities.get(identity)
            if prior and prior != entity.get("id"):
                self.errors.append(
                    f"duplicate semantic entity identity {identity}: {prior}, {entity.get('id')}"
                )
            entity_identities[identity] = entity.get("id")
            raw_identity_keys = entity.get("identity_keys", [])
            if not isinstance(raw_identity_keys, list) or not raw_identity_keys:
                self.errors.append(
                    f"{entity.get('id')} must have at least one immutable identity key"
                )
                continue
            for raw_key in raw_identity_keys:
                if not isinstance(raw_key, dict):
                    self.errors.append(f"{entity.get('id')} contains a non-object identity key")
                    continue
                identity_key = cast(JsonObject, raw_key)
                try:
                    for key_field in ("scheme", "namespace", "value"):
                        self.record_string(identity_key, key_field)
                    self.record_string_list(identity_key, "evidence_refs")
                    signature = self.dependencies.identity_key_signature(
                        identity_key,
                        self.dependencies.normalize_key(entity.get("namespace")) or "global",
                    )
                except ValueError as error:
                    self.errors.append(f"{entity.get('id')}: {error}")
                    continue
                prior_key = identity_keys.get(signature)
                if prior_key and prior_key != entity.get("id"):
                    self.errors.append(
                        "duplicate canonical entity identity key "
                        f"{signature}: {prior_key}, {entity.get('id')}"
                    )
                identity_keys[signature] = entity.get("id")

    def validate_generic_identities(self) -> None:
        generic_timestamps = {
            "events": "occurred_at",
            "decisions": "decided_at",
            "open_loops": "created_at",
            "receipts": "verified_at",
        }
        for collection, timestamp_key in generic_timestamps.items():
            identities: dict[tuple[str, str, str, tuple[str, ...]], object] = {}
            for record in self.collection_records[collection]:
                if record.get("lifecycle") == "redacted":
                    continue
                identity = (
                    self.dependencies.normalize_key(self.record_string(record, "kind")),
                    self.dependencies.normalize_key(self.record_string(record, "title")),
                    self.dependencies.normalize_space(self.record_string(record, timestamp_key)),
                    tuple(sorted(self.record_string_list(record, "entity_refs"))),
                )
                prior = identities.get(identity)
                if prior and prior != record.get("id"):
                    self.errors.append(
                        f"duplicate semantic {collection} identity {identity}: "
                        f"{prior}, {record.get('id')}"
                    )
                identities[identity] = record.get("id")

    def validate_record_references(self) -> None:
        entity_ids = self.collection_ids.get("entities", set())
        source_ids = self.collection_ids.get("sources", set())
        for collection in (
            "entities",
            "claim_surfaces",
            "claims",
            "events",
            "decisions",
            "open_loops",
            "receipts",
            "observations",
        ):
            for record in self.collection_records[collection]:
                for ref in self.record_string_list(record, "entity_refs"):
                    if ref not in entity_ids:
                        self.errors.append(f"{record.get('id')} references missing entity {ref}")
                for ref in self.record_string_list(record, "evidence_refs"):
                    if ref not in source_ids:
                        self.errors.append(f"{record.get('id')} references missing source {ref}")
                for ref in self.record_string_list(record, "subject_refs"):
                    if ref not in entity_ids:
                        self.errors.append(
                            f"{record.get('id')} references missing subject entity {ref}"
                        )

    def validate_open_loops(self) -> None:
        entity_ids = self.collection_ids.get("entities", set())
        for loop in self.collection_records["open_loops"]:
            raw_owner_refs = self.record_list(loop, "owner_refs")
            owner_refs = self.record_string_list(loop, "owner_refs")
            if len(owner_refs) != len(raw_owner_refs):
                self.errors.append(f"{loop.get('id')} contains an invalid owner entity reference")
            for ref in owner_refs:
                if not self.dependencies.safe_record_id.fullmatch(ref):
                    self.errors.append(
                        f"{loop.get('id')} contains an invalid owner entity reference"
                    )
                    continue
                if ref not in entity_ids:
                    self.errors.append(f"{loop.get('id')} references missing owner entity {ref}")
            checkpoint_at = loop.get("checkpoint_at")
            if checkpoint_at:
                try:
                    self.dependencies.parse_time(checkpoint_at)
                except (TypeError, ValueError):
                    self.errors.append(f"{loop.get('id')} has an invalid checkpoint timestamp")

    def validate_relations_and_observations(self) -> None:
        entity_ids = self.collection_ids.get("entities", set())
        source_ids = self.collection_ids.get("sources", set())
        for relation in self.collection_records["relations"]:
            for key in ("source_entity_id", "target_entity_id"):
                endpoint = self.record_string(relation, key)
                if endpoint not in entity_ids:
                    self.errors.append(f"{relation.get('id')} references missing entity {endpoint}")
            for ref in self.record_string_list(relation, "evidence_refs"):
                if ref not in source_ids:
                    self.errors.append(f"{relation.get('id')} references missing source {ref}")
        for observation in self.collection_records["observations"]:
            observation_source = self.record_string(observation, "source_ref")
            if observation_source not in source_ids:
                self.errors.append(
                    f"{observation.get('id')} references missing source {observation_source}"
                )

    def validate_journal(self) -> None:
        if "journal_anchor" not in self.graph:
            self.errors.append("missing required top-level journal_anchor")
        anchor = self.graph.get("journal_anchor", {})
        if isinstance(anchor, dict):
            if "watermark" not in anchor:
                self.errors.append("journal_anchor is missing watermark")
            if "entry_hash" not in anchor:
                self.errors.append("journal_anchor is missing entry_hash")
        anchor_watermark = anchor.get("watermark", 0) if isinstance(anchor, dict) else None
        if not isinstance(anchor_watermark, int) or anchor_watermark < 0:
            self.errors.append("journal_anchor.watermark must be a nonnegative integer")
            anchor_watermark = 0
        prior_watermark = anchor_watermark
        prior_entry_hash = (
            self.dependencies.normalize_space(anchor.get("entry_hash"))
            if isinstance(anchor, dict)
            else ""
        )
        journal_fingerprints: set[str] = set()
        journal_watermarks: set[int] = set()
        source_ids = self.collection_ids.get("sources", set())
        for journal in self.collection_records["journal"]:
            watermark = journal.get("watermark")
            if not isinstance(watermark, int) or watermark <= 0:
                self.errors.append(f"{journal.get('id')} has an invalid positive integer watermark")
            else:
                if watermark in journal_watermarks:
                    self.errors.append(f"duplicate journal watermark: {watermark}")
                journal_watermarks.add(watermark)
                if watermark != prior_watermark + 1:
                    self.errors.append(
                        "journal watermark sequence is not contiguous at "
                        f"{journal.get('id')}: expected {prior_watermark + 1}, "
                        f"got {watermark}"
                    )
                prior_watermark = watermark
            fingerprint = self.dependencies.normalize_space(journal.get("fingerprint"))
            if not fingerprint:
                self.errors.append(f"{journal.get('id')} is missing a fingerprint")
            elif fingerprint in journal_fingerprints:
                self.errors.append(f"duplicate journal fingerprint: {fingerprint}")
            journal_fingerprints.add(fingerprint)
            if (
                self.dependencies.normalize_space(journal.get("previous_entry_hash"))
                != prior_entry_hash
            ):
                self.errors.append(
                    f"{journal.get('id')} has a broken journal hash-chain predecessor"
                )
            expected_entry_hash = self.dependencies.journal_entry_hash(journal, prior_entry_hash)
            if self.dependencies.normalize_space(journal.get("entry_hash")) != expected_entry_hash:
                self.errors.append(f"{journal.get('id')} has an invalid journal entry hash")
            prior_entry_hash = self.dependencies.normalize_space(journal.get("entry_hash"))
            source_ref = self.record_string(journal, "source_ref")
            if source_ref and source_ref not in source_ids:
                self.errors.append(f"{journal.get('id')} references missing source {source_ref}")
            changed_ids = self.record_string_list(journal, "changed_ids")
            for changed_id in changed_ids:
                if changed_id not in self.known_ids:
                    self.errors.append(
                        f"{journal.get('id')} references missing changed record {changed_id}"
                    )
            based_on = journal.get("based_on_watermark")
            if based_on is not None and (
                not isinstance(based_on, int)
                or based_on < 0
                or (isinstance(watermark, int) and based_on >= watermark)
            ):
                self.errors.append(f"{journal.get('id')} has an invalid based_on_watermark")
            producer = journal.get("producer", {})
            if producer and not isinstance(producer, dict):
                self.errors.append(f"{journal.get('id')} producer must be an object")
            elif isinstance(producer, dict):
                for key in ("agent_id", "task_id", "parent_task_id", "run_id"):
                    producer_value = producer.get(key)
                    if producer_value is not None and not isinstance(producer_value, str):
                        self.errors.append(f"{journal.get('id')} producer {key} must be a string")
                        continue
                    if len(self.dependencies.normalize_space(producer_value)) > 200:
                        self.errors.append(
                            f"{journal.get('id')} producer {key} exceeds 200 characters"
                        )
            discovery_kind = self.dependencies.normalize_key(journal.get("discovery_kind"))
            if discovery_kind and discovery_kind not in self.dependencies.discovery_kinds:
                self.errors.append(
                    f"{journal.get('id')} has invalid discovery_kind {discovery_kind!r}"
                )
        if "next_watermark" not in self.graph:
            self.errors.append("missing required top-level next_watermark")
        expected_next_watermark = prior_watermark + 1
        if self.graph.get("next_watermark") != expected_next_watermark:
            self.errors.append(
                f"next_watermark must be {expected_next_watermark}, "
                f"got {self.graph.get('next_watermark')!r}"
            )

    def validate_authority_states(self) -> None:
        for collection in ("decisions", "open_loops"):
            for record in self.collection_records[collection]:
                if record.get("authority_state") != "candidate":
                    continue
                record_tier = self.record_string(record, "source_tier")
                if (
                    self.dependencies.source_tiers.get(record_tier, 0)
                    >= self.dependencies.source_tiers["validated_structured_candidate"]
                ):
                    self.errors.append(
                        f"{record.get('id')} is still a candidate despite validated authority"
                    )
                forbidden_status = "open" if collection == "open_loops" else "accepted"
                if record.get("status") == forbidden_status:
                    self.errors.append(
                        f"{record.get('id')} is a candidate with authoritative status "
                        f"{forbidden_status}"
                    )

    def validate_claims(self) -> None:
        surface_ids = self.collection_ids.get("claim_surfaces", set())
        observation_ids = self.collection_ids.get("observations", set())
        surface_by_id = self.dependencies.index_by_id(self.collection_records["claim_surfaces"])
        surface_signatures: dict[tuple[str, str, tuple[str, ...]], object] = {}
        for surface in self.collection_records["claim_surfaces"]:
            try:
                canonical_subject_refs = self.dependencies.canonical_entity_refs(
                    self.validation_graph,
                    self.record_string_list(surface, "subject_refs"),
                )
            except (TypeError, ValueError) as error:
                self.errors.append(f"{surface.get('id')} has invalid canonical subjects: {error}")
                canonical_subject_refs = []
            signature = (
                self.dependencies.normalize_key(surface.get("family")),
                self.dependencies.normalize_key(surface.get("key")),
                tuple(sorted(canonical_subject_refs)),
            )
            if not signature[0] or not signature[1]:
                self.errors.append(f"{surface.get('id')} has an empty claim-surface family or key")
            prior_surface = surface_signatures.get(signature)
            if prior_surface and prior_surface != surface.get("id"):
                self.errors.append(
                    f"duplicate claim surface definition {signature}: "
                    f"{prior_surface}, {surface.get('id')}"
                )
            surface_signatures[signature] = surface.get("id")

        current_surfaces: dict[str, list[str]] = {}
        for claim in self.collection_records["claims"]:
            surface_ref = self.record_string(claim, "surface_ref")
            if surface_ref not in surface_ids:
                self.errors.append(
                    f"{claim.get('id')} references missing claim surface {surface_ref}"
                )
            else:
                surface = surface_by_id[surface_ref]
                # A redaction deliberately tombstones the claim's semantic fields.
                # Keep validating that its immutable surface still exists, but do
                # not require scrubbed family/key/subjects to reproduce that surface.
                if claim.get("lifecycle") != "redacted":
                    if self.dependencies.normalize_key(
                        surface.get("family")
                    ) != self.dependencies.normalize_key(claim.get("family")):
                        self.errors.append(
                            f"{claim.get('id')} family differs from claim surface {surface_ref}"
                        )
                    if self.dependencies.normalize_key(
                        surface.get("key")
                    ) != self.dependencies.normalize_key(claim.get("key")):
                        self.errors.append(
                            f"{claim.get('id')} key differs from claim surface {surface_ref}"
                        )
                    try:
                        canonical_subjects = set(
                            self.dependencies.canonical_entity_refs(
                                self.validation_graph,
                                self.record_string_list(surface, "subject_refs"),
                            )
                        )
                        canonical_claim_refs = set(
                            self.dependencies.canonical_entity_refs(
                                self.validation_graph,
                                self.record_string_list(claim, "entity_refs"),
                            )
                        )
                    except (TypeError, ValueError) as error:
                        self.errors.append(
                            f"{claim.get('id')} has invalid canonical claim-surface references: "
                            f"{error}"
                        )
                        canonical_subjects = set()
                        canonical_claim_refs = set()
                    missing_subjects = canonical_subjects - canonical_claim_refs
                    if missing_subjects:
                        self.errors.append(
                            f"{claim.get('id')} omits claim-surface subjects: "
                            f"{sorted(missing_subjects)}"
                        )
            for ref in self.record_string_list(claim, "observation_refs"):
                if ref not in observation_ids:
                    self.errors.append(f"{claim.get('id')} references missing observation {ref}")
            successor = self.record_string(claim, "superseded_by")
            if successor and successor not in self.collection_ids.get("claims", set()):
                self.errors.append(f"{claim.get('id')} references missing successor {successor}")
            if claim.get("lifecycle") == "superseded" and claim.get("active") is True:
                self.errors.append(f"{claim.get('id')} is both superseded and active")
            if claim.get("lifecycle") == "quarantined" and claim.get("active") is True:
                self.errors.append(f"{claim.get('id')} is both quarantined and active")
            claim_tier = self.record_string(claim, "source_tier")
            if self.dependencies.source_tiers.get(claim_tier, 0) < self.dependencies.source_tiers[
                "validated_structured_candidate"
            ] and (claim.get("active") is True or claim.get("lifecycle") == "current"):
                self.errors.append(f"{claim.get('id')} exposes a low-authority claim as current")
            valid_from = self.record_string(claim, "valid_from")
            valid_to = self.record_string(claim, "valid_to")
            if valid_from and valid_to:
                try:
                    if self.dependencies.time_value(valid_to) < self.dependencies.time_value(
                        valid_from
                    ):
                        self.errors.append(f"{claim.get('id')} has an inverted validity interval")
                except (TypeError, ValueError):
                    self.errors.append(f"{claim.get('id')} has an invalid validity timestamp")
            if claim.get("active") and claim.get("lifecycle") == "current":
                current_surfaces.setdefault(str(surface_ref), []).append(str(claim.get("id")))
        for surface_key, claim_ids in current_surfaces.items():
            if len(claim_ids) > 1:
                self.errors.append(
                    f"multiple current claims on surface {surface_key}: {', '.join(claim_ids)}"
                )

    def result(self) -> ValidationResult:
        hits = self.dependencies.secret_hits(self.graph)
        if hits:
            self.errors.append(f"potential secret material detected: {', '.join(hits)}")
        if len(self.collection_records["journal"]) > 5000:
            self.warnings.append("journal exceeds 5000 entries; consider archival compaction")
        return {
            "valid": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": {
                collection: len(self.collection_records[collection])
                for collection in self.dependencies.collections
            },
        }


def validate_graph(graph: object, dependencies: ValidationDependencies) -> ValidationResult:
    """Validate a v3 graph without importing or mutating the legacy module."""
    if not isinstance(graph, dict):
        return {
            "valid": False,
            "errors": ["canonical graph must be an object"],
            "warnings": [],
            "counts": {collection: 0 for collection in dependencies.collections},
        }

    state = _ValidationState(graph=cast(JsonObject, graph), dependencies=dependencies)
    if graph.get("schema_version") != dependencies.graph_schema_version:
        state.errors.append(f"unsupported schema version: {graph.get('schema_version')!r}")
    state.collect_records()
    state.validate_scalar_fields()
    state.validate_entities()
    state.validate_generic_identities()
    state.validate_record_references()
    state.validate_open_loops()
    state.validate_relations_and_observations()
    state.validate_journal()
    state.validate_authority_states()
    state.validate_claims()
    return state.result()
