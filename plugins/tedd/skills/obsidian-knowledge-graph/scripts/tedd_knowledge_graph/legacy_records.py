"""Legacy claim, observation, and generic-record mutation semantics."""

from __future__ import annotations

from dataclasses import dataclass

from .legacy_mutation_contracts import (
    JsonObject,
    RecordDependencies,
    _objects,
    _optional_string,
    _records,
    _string,
    _strings,
    _to_int,
)
from .preferences import validate_preference_value


@dataclass(frozen=True)
class _ClaimContext:
    family: str
    key: str
    value: str
    refs: list[str]
    surface_id: str
    tier: str
    tier_strength: int
    validated_claim: bool
    valid_from: str
    observations: list[str]
    conflict_policy: str
    same_active: list[JsonObject]
    current_conflicts: list[JsonObject]
    backdated_replace: bool


@dataclass(frozen=True)
class _GenericShape:
    kind: str
    title: str
    timestamp_key: str
    timestamp: str
    prefix: str


@dataclass(frozen=True)
class _GenericRecordContext:
    collection: str
    kind: str
    title: str
    timestamp_key: str
    timestamp: str
    refs: list[str]
    owner_refs: list[str]
    identity: JsonObject
    explicit_id: str
    record_id: str
    source_tier: str
    authority_gated: bool
    accepted_authority: bool
    requested_status_raw: str
    requested_status: str
    candidate_status: str


class LegacyRecordService:
    """Legacy claim, observation, and generic-record mutation semantics."""

    def __init__(self, dependencies: RecordDependencies) -> None:
        self.dependencies = dependencies

    def upsert_claim_surface(
        self,
        graph: JsonObject,
        raw: JsonObject,
        local_refs: dict[str, str],
        source: JsonObject,
        family: str,
        key: str,
        context_refs: list[str],
    ) -> tuple[str, bool, bool]:
        explicit_surface_id = self.dependencies.explicit_record_id(raw.get("surface_id"))
        subjects_present = "subject_refs" in raw
        subject_refs = (
            self.dependencies.resolved_entity_refs(raw, "subject_refs", local_refs, graph)
            if subjects_present
            else []
        )
        surfaces = self.dependencies.index_by_id(_records(graph, "claim_surfaces"))
        if explicit_surface_id and explicit_surface_id in surfaces and not subjects_present:
            subject_refs = list(_strings(surfaces[explicit_surface_id].get("subject_refs", [])))
        elif not subjects_present:
            subject_refs = list(context_refs)
        subject_refs = self.dependencies.canonical_entity_refs(graph, subject_refs)
        if family == "preference" and len(subject_refs) != 1:
            raise ValueError("A preference requires exactly one canonical subject")
        requested_signature = (family, key, tuple(sorted(subject_refs)))
        signature_matches = [
            surface
            for surface in _records(graph, "claim_surfaces")
            if surface.get("lifecycle") != "redacted"
            and (
                self.dependencies.normalize_key(surface.get("family")),
                self.dependencies.normalize_key(surface.get("key")),
                tuple(
                    sorted(
                        self.dependencies.canonical_entity_refs(
                            graph, _objects(surface.get("subject_refs", []))
                        )
                    )
                ),
            )
            == requested_signature
        ]
        if len(signature_matches) > 1:
            raise ValueError(
                "Multiple canonical claim surfaces match this request; reconcile them before capturing"
            )
        if explicit_surface_id:
            if explicit_surface_id not in surfaces and signature_matches:
                raise ValueError(
                    "Explicit claim-surface ID would duplicate an existing canonical surface; "
                    f"use {signature_matches[0]['id']}"
                )
            surface_id = explicit_surface_id
        elif signature_matches:
            surface_id = _string(signature_matches[0]["id"])
        else:
            surface_id = self.dependencies._legacy_surface_id(family, key, subject_refs)
        existing = surfaces.get(surface_id)
        if existing:
            if existing.get("lifecycle") == "redacted":
                raise ValueError("A redacted claim surface cannot be reused")
            signature = (
                self.dependencies.normalize_key(existing.get("family")),
                self.dependencies.normalize_key(existing.get("key")),
                tuple(
                    sorted(
                        self.dependencies.canonical_entity_refs(
                            graph, _objects(existing.get("subject_refs", []))
                        )
                    )
                ),
            )
            if signature != requested_signature:
                raise ValueError(
                    f"Claim surface {surface_id!r} is immutable and does not match family, key, or subjects"
                )
            existing["last_observed_at"] = self.dependencies.latest_time(
                _optional_string(existing.get("last_observed_at")),
                _string(source["observed_at"]),
            )
            existing["evidence_refs"] = self.dependencies.unique_strings(
                [*_objects(existing.get("evidence_refs", [])), source["id"]]
            )
            existing["sensitive"] = bool(existing.get("sensitive")) or bool(
                raw.get("sensitive", False)
            )
            return surface_id, False, not bool(explicit_surface_id)
        _records(graph, "claim_surfaces").append(
            {
                "id": surface_id,
                "family": family,
                "key": key,
                "subject_refs": subject_refs,
                "cardinality": "single",
                "lifecycle": "active",
                "created_at": source["observed_at"],
                "last_observed_at": source["observed_at"],
                "evidence_refs": [source["id"]],
                "source_tier": source["source_tier"],
                "sensitive": bool(raw.get("sensitive", False)),
                "legacy_derived": not bool(explicit_surface_id),
            }
        )
        return surface_id, True, not bool(explicit_surface_id)

    def add_observation(
        self, graph: JsonObject, raw: JsonObject, local_refs: dict[str, str], source: JsonObject
    ) -> tuple[str, bool]:
        family = self.dependencies.normalize_key(raw.get("family"))
        key = self.dependencies.normalize_key(raw.get("key"))
        value = self.dependencies.normalize_space(raw.get("value"))
        if not family or not key:
            raise ValueError("Observation family and key are required")
        observed_at = self.dependencies.parse_time(
            raw.get("observed_at"), _string(source["observed_at"])
        )
        refs = self.dependencies.record_entity_refs(raw, local_refs, graph)
        observation_id = self.dependencies.stable_id(
            "observation",
            {
                "family": family,
                "key": key,
                "value": value,
                "observed_at": observed_at,
                "source": source["id"],
                "entity_refs": sorted(refs),
            },
        )
        existing = self.dependencies.index_by_id(_records(graph, "observations")).get(
            observation_id
        )
        if existing:
            existing["sensitive"] = bool(existing.get("sensitive")) or bool(
                raw.get("sensitive", False)
            )
            return observation_id, False
        _records(graph, "observations").append(
            {
                "id": observation_id,
                "family": family,
                "key": key,
                "value": value,
                "observed_at": observed_at,
                "asserted_at": self.dependencies.parse_time(
                    raw.get("asserted_at"), _string(source["captured_at"])
                ),
                "source_tier": self.dependencies.source_tier(
                    raw.get("source_tier"), _string(source["source_tier"])
                ),
                "source_ref": source["id"],
                "entity_refs": refs,
                "sensitive": bool(raw.get("sensitive", False)),
                "lifecycle": "active",
            }
        )
        return observation_id, True

    def _cited_observations(
        self, graph: JsonObject, raw: JsonObject, claim_refs: list[str]
    ) -> list[str]:
        """Resolve existing observations a claim explicitly folds into its slot.

        A cited observation must already exist, remain readable, and share an
        entity with the claim, so one subject cannot fold another's notes.
        """

        values = raw.get("observation_refs")
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("Claim observation_refs must be a list")
        stored = self.dependencies.index_by_id(_records(graph, "observations"))
        claim_entities = set(claim_refs)
        cited: list[str] = []
        for value in values:
            observation_id = self.dependencies.explicit_record_id(value)
            observation = stored.get(observation_id) if observation_id else None
            if observation is None:
                raise ValueError("Claim observation_refs must name existing observations")
            if observation.get("lifecycle") == "redacted":
                raise ValueError("A claim cannot cite a redacted observation")
            observation_entities = set(
                self.dependencies.canonical_entity_refs(
                    graph, _objects(observation.get("entity_refs", []))
                )
            )
            if not observation_entities & claim_entities:
                raise ValueError("A cited observation must share an entity with the claim")
            cited.append(observation_id)
        return cited

    def _prepare_claim_context(
        self,
        graph: JsonObject,
        raw: JsonObject,
        local_refs: dict[str, str],
        source: JsonObject,
    ) -> _ClaimContext:
        family = self.dependencies.normalize_key(raw.get("family"))
        key = self.dependencies.normalize_key(raw.get("key"))
        value = self.dependencies.normalize_space(raw.get("value"))
        if not family or not key:
            raise ValueError("Claim family and key are required")
        if family == "preference":
            validate_preference_value(raw.get("value"))
        refs = self.dependencies.record_entity_refs(raw, local_refs, graph)
        surface_id, _, _ = self.dependencies.upsert_claim_surface(
            graph, raw, local_refs, source, family, key, refs
        )
        surface = self.dependencies.index_by_id(_records(graph, "claim_surfaces"))[surface_id]
        refs = self.dependencies.unique_strings(
            [
                *refs,
                *self.dependencies.canonical_entity_refs(
                    graph, _objects(surface.get("subject_refs", []))
                ),
            ]
        )
        scope = tuple(sorted(refs))
        tier = self.dependencies.source_tier(raw.get("source_tier"), _string(source["source_tier"]))
        tier_strength = self.dependencies.source_tiers[tier]
        validated_claim = (
            tier_strength >= self.dependencies.source_tiers["validated_structured_candidate"]
        )
        valid_from = self.dependencies.parse_time(
            raw.get("valid_from"), _string(source["observed_at"])
        )
        observations = [
            _string(item["id"])
            for item in _records(graph, "observations")
            if item.get("family") == family
            and item.get("key") == key
            and item.get("value") == value
            and item.get("source_ref") == source["id"]
            and tuple(sorted(_strings(item.get("entity_refs", [])))) == scope
        ]
        cited = self._cited_observations(graph, raw, refs)
        if cited and not validated_claim:
            # Citing a note takes it out of task context, including when this
            # packet only restates a claim that is already current.
            raise ValueError(
                "Claim observation_refs require a source at validated_structured_candidate or above"
            )
        observations = self.dependencies.unique_strings([*observations, *cited])
        conflict_policy = self.dependencies.normalize_key(raw.get("conflict_policy")) or "preserve"
        if conflict_policy not in {"preserve", "replace"}:
            raise ValueError("Claim conflict_policy must be preserve or replace")
        same_active = [
            claim
            for claim in _records(graph, "claims")
            if claim.get("surface_ref") == surface_id
            and claim.get("value") == value
            and claim.get("active") is True
        ]
        current_conflicts = [
            claim
            for claim in _records(graph, "claims")
            if claim.get("surface_ref") == surface_id
            and claim.get("active") is True
            and claim.get("value") != value
        ]
        backdated_replace = conflict_policy == "replace" and any(
            self.dependencies.time_value(valid_from)
            < self.dependencies.time_value(_string(claim.get("valid_from") or valid_from))
            for claim in current_conflicts
        )
        return _ClaimContext(
            family=family,
            key=key,
            value=value,
            refs=refs,
            surface_id=surface_id,
            tier=tier,
            tier_strength=tier_strength,
            validated_claim=validated_claim,
            valid_from=valid_from,
            observations=observations,
            conflict_policy=conflict_policy,
            same_active=same_active,
            current_conflicts=current_conflicts,
            backdated_replace=backdated_replace,
        )

    def _merge_claim_evidence(
        self,
        existing: JsonObject,
        raw: JsonObject,
        source: JsonObject,
        context: _ClaimContext,
    ) -> None:
        existing["sensitive"] = bool(existing.get("sensitive")) or bool(raw.get("sensitive", False))
        existing["evidence_refs"] = self.dependencies.unique_strings(
            [*_objects(existing.get("evidence_refs", [])), source["id"]]
        )
        existing["observation_refs"] = self.dependencies.unique_strings(
            [*_objects(existing.get("observation_refs", [])), *context.observations]
        )
        if context.tier_strength > self.dependencies.source_tier_rank(existing.get("source_tier")):
            existing["source_tier"] = context.tier

    def _strongest_claim_conflict(self, conflicts: list[JsonObject]) -> int:
        return max(
            (self.dependencies.source_tier_rank(item.get("source_tier")) for item in conflicts),
            default=0,
        )

    @staticmethod
    def _supersede_claims(
        conflicts: list[JsonObject],
        valid_from: str,
        captured_at: object,
        superseded_by: object,
    ) -> None:
        for old_claim in conflicts:
            old_claim["active"] = False
            old_claim["lifecycle"] = "superseded"
            old_claim["valid_to"] = valid_from
            old_claim["ended_at"] = captured_at
            old_claim["superseded_by"] = superseded_by

    def _corroborate_active_claim(
        self,
        raw: JsonObject,
        source: JsonObject,
        context: _ClaimContext,
    ) -> tuple[str, str]:
        existing = sorted(
            context.same_active,
            key=lambda item: (
                item.get("lifecycle") != "current",
                -self.dependencies.source_tier_rank(item.get("source_tier")),
                _string(item.get("id", "")),
            ),
        )[0]
        self._merge_claim_evidence(existing, raw, source, context)
        strongest_conflict = self._strongest_claim_conflict(context.current_conflicts)
        if context.backdated_replace:
            return _string(existing["id"]), "backdated_replace_not_applied"
        if (
            context.conflict_policy == "replace"
            and context.current_conflicts
            and context.validated_claim
            and context.tier_strength >= strongest_conflict
        ):
            self._supersede_claims(
                context.current_conflicts,
                context.valid_from,
                source["captured_at"],
                existing["id"],
            )
            existing["lifecycle"] = "current"
            return _string(existing["id"]), "reconciled"
        if (
            context.current_conflicts
            and context.validated_claim
            and context.tier_strength >= strongest_conflict
        ):
            existing["lifecycle"] = "conflicting"
        return _string(existing["id"]), "corroborated"

    def _update_existing_claim(
        self,
        existing: JsonObject,
        claim_id: str,
        raw: JsonObject,
        source: JsonObject,
        context: _ClaimContext,
    ) -> tuple[str, str]:
        if existing.get("lifecycle") == "redacted":
            return claim_id, "unchanged_redacted"
        self._merge_claim_evidence(existing, raw, source, context)
        if context.validated_claim and existing.get("lifecycle") == "quarantined":
            strongest_conflict = self._strongest_claim_conflict(context.current_conflicts)
            can_promote = not context.backdated_replace and (
                not context.current_conflicts or context.tier_strength >= strongest_conflict
            )
            if can_promote:
                if context.current_conflicts and context.conflict_policy == "replace":
                    self._supersede_claims(
                        context.current_conflicts,
                        context.valid_from,
                        source["captured_at"],
                        claim_id,
                    )
                    existing["lifecycle"] = "current"
                elif context.current_conflicts:
                    existing["lifecycle"] = "conflicting"
                    for old_claim in context.current_conflicts:
                        if old_claim.get("lifecycle") == "current":
                            old_claim["lifecycle"] = "conflicting"
                else:
                    existing["lifecycle"] = "current"
                existing["active"] = True
                existing.pop("quarantine_reason", None)
                return claim_id, "promoted"
        return claim_id, "corroborated"

    def _new_claim_state(
        self,
        context: _ClaimContext,
        claim_id: str,
        source: JsonObject,
    ) -> tuple[str, bool, str | None]:
        lifecycle = "current"
        active = True
        quarantine_reason = None
        if not context.validated_claim:
            return (
                "quarantined",
                False,
                "claim authority is below validated_structured_candidate",
            )
        if not context.current_conflicts:
            return lifecycle, active, quarantine_reason
        strongest = self._strongest_claim_conflict(context.current_conflicts)
        if context.backdated_replace:
            return (
                "quarantined",
                False,
                "backdated replacement would invert an active claim interval",
            )
        if context.conflict_policy == "replace" and context.tier_strength >= strongest:
            self._supersede_claims(
                context.current_conflicts,
                context.valid_from,
                source["captured_at"],
                claim_id,
            )
        elif context.conflict_policy == "replace":
            return (
                "quarantined",
                False,
                "replacement authority is lower than the active claim",
            )
        elif context.tier_strength < strongest:
            return (
                "quarantined",
                False,
                "lower-authority contradiction preserved without destabilizing the active claim",
            )
        else:
            lifecycle = "conflicting"
            for old_claim in context.current_conflicts:
                if old_claim.get("lifecycle") == "current":
                    old_claim["lifecycle"] = "conflicting"
        return lifecycle, active, quarantine_reason

    def add_claim(
        self, graph: JsonObject, raw: JsonObject, local_refs: dict[str, str], source: JsonObject
    ) -> tuple[str, str]:
        context = self._prepare_claim_context(graph, raw, local_refs, source)
        if context.same_active:
            return self._corroborate_active_claim(raw, source, context)
        claim_id = self.dependencies.stable_id(
            "claim",
            {
                "family": context.family,
                "key": context.key,
                "value": context.value,
                "surface_ref": context.surface_id,
                "valid_from": context.valid_from,
            },
        )
        existing = self.dependencies.index_by_id(_records(graph, "claims")).get(claim_id)
        if existing:
            return self._update_existing_claim(existing, claim_id, raw, source, context)
        lifecycle, active, quarantine_reason = self._new_claim_state(context, claim_id, source)
        record = {
            "id": claim_id,
            "family": context.family,
            "key": context.key,
            "surface_ref": context.surface_id,
            "value": context.value,
            "active": active,
            "lifecycle": lifecycle,
            "valid_from": context.valid_from,
            "valid_to": None,
            "asserted_at": self.dependencies.parse_time(
                raw.get("asserted_at"), _string(source["captured_at"])
            ),
            "ended_at": None,
            "superseded_by": None,
            "source_tier": context.tier,
            "evidence_refs": [source["id"]],
            "observation_refs": context.observations,
            "entity_refs": context.refs,
            "sensitive": bool(raw.get("sensitive", False)),
        }
        if quarantine_reason:
            record["quarantine_reason"] = quarantine_reason
        _records(graph, "claims").append(record)
        return claim_id, lifecycle

    def _generic_shape(self, collection: str, raw: JsonObject, source: JsonObject) -> _GenericShape:
        if collection == "events":
            kind = self.dependencies.normalize_key(raw.get("kind")) or "event"
            title = self.dependencies.normalize_space(raw.get("title"))
            timestamp_key = "occurred_at"
            timestamp = self.dependencies.parse_time(
                raw.get(timestamp_key), _string(source["observed_at"])
            )
            prefix = "event"
        elif collection == "decisions":
            kind = "decision"
            title = self.dependencies.normalize_space(raw.get("title"))
            timestamp_key = "decided_at"
            timestamp = self.dependencies.parse_time(
                raw.get(timestamp_key), _string(source["observed_at"])
            )
            prefix = "decision"
        elif collection == "open_loops":
            kind = "open_loop"
            title = self.dependencies.normalize_space(raw.get("title"))
            timestamp_key = "created_at"
            timestamp = self.dependencies.parse_time(
                raw.get(timestamp_key), _string(source["observed_at"])
            )
            prefix = "loop"
        elif collection == "receipts":
            kind = self.dependencies.normalize_key(raw.get("kind")) or "receipt"
            title = self.dependencies.normalize_space(raw.get("title"))
            timestamp_key = "verified_at"
            timestamp = self.dependencies.parse_time(
                raw.get(timestamp_key), _string(source["observed_at"])
            )
            prefix = "receipt"
        else:
            raise ValueError(f"Unsupported collection: {collection}")
        if not title:
            raise ValueError(f"{collection} title is required")
        return _GenericShape(kind, title, timestamp_key, timestamp, prefix)

    def _prepare_generic_record(
        self,
        graph: JsonObject,
        collection: str,
        raw: JsonObject,
        local_refs: dict[str, str],
        source: JsonObject,
    ) -> tuple[_GenericRecordContext, JsonObject | None]:
        shape = self._generic_shape(collection, raw, source)
        refs = self.dependencies.record_entity_refs(raw, local_refs, graph)
        owner_refs = (
            self.dependencies.resolved_entity_refs(raw, "owner_refs", local_refs, graph)
            if collection == "open_loops"
            else []
        )
        identity: JsonObject = {
            "kind": shape.kind,
            "title": self.dependencies.normalize_key(shape.title),
            "timestamp": shape.timestamp,
            "entities": sorted(refs),
        }
        explicit_id = self.dependencies.explicit_record_id(raw.get("id"))
        derived_id = self.dependencies.stable_id(shape.prefix, identity)
        identity_matches: list[JsonObject] = []
        for item in _records(graph, collection):
            if item.get("lifecycle") == "redacted":
                continue
            item_identity = {
                "kind": item.get("kind"),
                "title": self.dependencies.normalize_key(item.get("title")),
                "timestamp": item.get(shape.timestamp_key),
                "entities": sorted(_strings(item.get("entity_refs", []))),
            }
            if item_identity == identity:
                identity_matches.append(item)
        if len(identity_matches) > 1:
            raise ValueError(f"Stored {collection} identity is ambiguous: {shape.title}")
        if explicit_id and identity_matches and identity_matches[0]["id"] != explicit_id:
            raise ValueError(
                f"{collection} identity already belongs to {identity_matches[0]['id']}; cannot assign {explicit_id}"
            )
        record_id = explicit_id or (
            _string(identity_matches[0]["id"]) if identity_matches else derived_id
        )
        existing = self.dependencies.index_by_id(_records(graph, collection)).get(record_id)
        authority_gated = collection in {"decisions", "open_loops"}
        record_source_tier = (
            self.dependencies.source_tier(raw.get("source_tier"), _string(source["source_tier"]))
            if authority_gated
            else self.dependencies.source_tier(source.get("source_tier"))
        )
        accepted_authority = (
            self.dependencies.source_tiers[record_source_tier]
            >= self.dependencies.source_tiers["validated_structured_candidate"]
        )
        requested_status_raw = self.dependencies.normalize_key(raw.get("status"))
        requested_status = requested_status_raw or (
            "open" if collection == "open_loops" else "recorded"
        )
        candidate_status = "candidate" if collection == "open_loops" else "proposed"
        context = _GenericRecordContext(
            collection=collection,
            kind=shape.kind,
            title=shape.title,
            timestamp_key=shape.timestamp_key,
            timestamp=shape.timestamp,
            refs=refs,
            owner_refs=owner_refs,
            identity=identity,
            explicit_id=explicit_id,
            record_id=record_id,
            source_tier=record_source_tier,
            authority_gated=authority_gated,
            accepted_authority=accepted_authority,
            requested_status_raw=requested_status_raw,
            requested_status=requested_status,
            candidate_status=candidate_status,
        )
        return context, existing

    def _apply_generic_field_updates(
        self,
        existing: JsonObject,
        raw: JsonObject,
        context: _GenericRecordContext,
    ) -> None:
        for field in (
            "summary",
            "rationale",
            "priority",
            "due_at",
            "verified",
            "uri",
        ):
            if field in raw and raw[field] is not None:
                if field in {"summary", "rationale", "uri"}:
                    existing[field] = self.dependencies.normalize_space(raw[field])
                elif field == "priority":
                    existing[field] = _to_int(raw[field])
                elif field == "due_at":
                    existing[field] = (
                        self.dependencies.parse_time(raw[field]) if raw[field] else None
                    )
                elif field == "verified":
                    if not isinstance(raw[field], bool):
                        raise ValueError("receipts verified must be a boolean")
                    existing[field] = raw[field]
        if context.collection == "open_loops":
            if "owner_refs" in raw:
                existing["owner_refs"] = context.owner_refs
            if "next_action" in raw:
                existing["next_action"] = self.dependencies.normalize_space(raw.get("next_action"))
            if "checkpoint_at" in raw:
                existing["checkpoint_at"] = (
                    self.dependencies.parse_time(raw["checkpoint_at"])
                    if raw.get("checkpoint_at")
                    else None
                )
            if "terminal_condition" in raw:
                existing["terminal_condition"] = self.dependencies.normalize_space(
                    raw.get("terminal_condition")
                )

    def _apply_generic_authority_update(
        self,
        existing: JsonObject,
        raw: JsonObject,
        source: JsonObject,
        context: _GenericRecordContext,
    ) -> None:
        if context.authority_gated:
            if context.accepted_authority:
                was_candidate = existing.get("authority_state") == "candidate"
                existing["authority_state"] = "accepted"
                if context.requested_status_raw:
                    existing["status"] = context.requested_status_raw
                elif was_candidate:
                    existing["status"] = self.dependencies.normalize_key(
                        existing.get("requested_status")
                    ) or ("open" if context.collection == "open_loops" else "recorded")
                existing.pop("requested_status", None)
            elif existing.get("authority_state", "accepted") == "candidate":
                existing["authority_state"] = "candidate"
                if context.requested_status_raw:
                    existing["requested_status"] = context.requested_status_raw
                existing["status"] = context.candidate_status
        elif context.requested_status_raw:
            existing["status"] = context.requested_status_raw
        self._apply_generic_field_updates(existing, raw, context)
        existing["source_tier"] = context.source_tier

    def _source_outranks(
        self,
        existing: JsonObject,
        source: JsonObject,
        context: _GenericRecordContext,
    ) -> bool:
        """Whether this source is at least as recent and as strong as the stored record.

        Read this before the record's observation times are updated.
        """

        observed_at = _string(source["observed_at"])
        stored_at = _string(
            existing.get("last_observed_at") or existing.get(context.timestamp_key) or observed_at
        )
        if self.dependencies.time_value(observed_at) < self.dependencies.time_value(stored_at):
            return False
        return self.dependencies.source_tiers[
            context.source_tier
        ] >= self.dependencies.source_tier_rank(existing.get("source_tier"))

    def _update_existing_generic_record(
        self,
        existing: JsonObject,
        raw: JsonObject,
        source: JsonObject,
        context: _GenericRecordContext,
        outranks: bool,
    ) -> tuple[str, bool]:
        if existing:
            if existing.get("lifecycle") == "redacted":
                return context.record_id, False
            existing["sensitive"] = bool(existing.get("sensitive")) or bool(
                raw.get("sensitive", False)
            )
            if context.explicit_id:
                existing_identity = {
                    "kind": existing.get("kind"),
                    "title": self.dependencies.normalize_key(existing.get("title")),
                    "timestamp": existing.get(context.timestamp_key),
                    "entities": sorted(_strings(existing.get("entity_refs", []))),
                }
                if existing_identity != context.identity:
                    raise ValueError(
                        f"Explicit {context.collection} ID {context.explicit_id!r} does not match its stored identity"
                    )
            if (
                context.collection == "open_loops"
                and existing.get("status") == "resolved"
                and context.requested_status_raw == "open"
            ):
                raise ValueError(
                    "Resolved open loops cannot be reopened in place; capture a new episode with a new created_at"
                )
            existing["evidence_refs"] = self.dependencies.unique_strings(
                [*_objects(existing.get("evidence_refs", [])), source["id"]]
            )
            existing["entity_refs"] = self.dependencies.unique_strings(
                [*_objects(existing.get("entity_refs", [])), *context.refs]
            )
            prior_last_observed = _string(
                existing.get("last_observed_at")
                or existing.get(context.timestamp_key)
                or source["observed_at"]
            )
            existing["first_observed_at"] = self.dependencies.earliest_time(
                _optional_string(existing.get("first_observed_at") or prior_last_observed),
                _string(source["observed_at"]),
            )
            existing["last_observed_at"] = self.dependencies.latest_time(
                _optional_string(existing.get("last_observed_at") or prior_last_observed),
                _string(source["observed_at"]),
            )
            if outranks:
                self._apply_generic_authority_update(existing, raw, source, context)
            return context.record_id, True
        raise AssertionError("existing generic record is required")

    def _create_generic_record(
        self,
        graph: JsonObject,
        raw: JsonObject,
        source: JsonObject,
        context: _GenericRecordContext,
    ) -> tuple[str, bool]:
        record: JsonObject = {
            "id": context.record_id,
            "kind": context.kind,
            "title": context.title,
            "summary": self.dependencies.normalize_space(raw.get("summary")),
            context.timestamp_key: context.timestamp,
            "status": context.requested_status,
            "entity_refs": context.refs,
            "evidence_refs": [source["id"]],
            "source_tier": context.source_tier,
            "sensitive": bool(raw.get("sensitive", False)),
            "first_observed_at": source["observed_at"],
            "last_observed_at": source["observed_at"],
        }
        if context.authority_gated:
            record["authority_state"] = "accepted" if context.accepted_authority else "candidate"
            if not context.accepted_authority:
                record["requested_status"] = context.requested_status
                record["status"] = context.candidate_status
        if context.collection == "decisions":
            record["rationale"] = self.dependencies.normalize_space(raw.get("rationale"))
        if context.collection == "open_loops":
            record["priority"] = _to_int(raw.get("priority", 1))
            record["due_at"] = (
                self.dependencies.parse_time(raw["due_at"]) if raw.get("due_at") else None
            )
            record["owner_refs"] = context.owner_refs
            record["next_action"] = self.dependencies.normalize_space(raw.get("next_action"))
            record["checkpoint_at"] = (
                self.dependencies.parse_time(raw["checkpoint_at"])
                if raw.get("checkpoint_at")
                else None
            )
            record["terminal_condition"] = self.dependencies.normalize_space(
                raw.get("terminal_condition")
            )
            record["resolved_at"] = None
            record["resolution"] = None
        if context.collection == "receipts":
            record["uri"] = self.dependencies.normalize_space(raw.get("uri"))
            if "verified" in raw and not isinstance(raw["verified"], bool):
                raise ValueError("receipts verified must be a boolean")
            record["verified"] = bool(raw.get("verified", False))
        _records(graph, context.collection).append(record)
        return context.record_id, True

    def add_generic_record(
        self,
        graph: JsonObject,
        collection: str,
        raw: JsonObject,
        local_refs: dict[str, str],
        source: JsonObject,
    ) -> tuple[str, bool]:
        context, existing = self._prepare_generic_record(graph, collection, raw, local_refs, source)
        previous_status = existing.get("status") if existing else None
        if existing:
            outranks = existing.get("lifecycle") != "redacted" and self._source_outranks(
                existing, source, context
            )
            result = self._update_existing_generic_record(existing, raw, source, context, outranks)
        else:
            outranks = True
            result = self._create_generic_record(graph, raw, source, context)
        record = self.dependencies.index_by_id(_records(graph, collection))[context.record_id]
        if collection == "decisions":
            self._apply_decision_successor(
                graph, record, raw, context, context.accepted_authority and outranks
            )
        elif collection == "open_loops":
            self._apply_loop_resolution(record, raw, source, previous_status)
        return result

    def _apply_decision_successor(
        self,
        graph: JsonObject,
        record: JsonObject,
        raw: JsonObject,
        context: _GenericRecordContext,
        authoritative: bool,
    ) -> None:
        """Link a replaced decision to the decision that replaced it.

        ``authoritative`` says this capture's source is validated and outranks
        what is stored.  Only such a source may add or move the link; repeating
        the stored link changes nothing.  Whether the successor itself stands
        is judged once the whole packet is applied, by the capture service.
        """

        if raw.get("superseded_by") in (None, ""):
            return
        if record.get("lifecycle") == "redacted":
            return
        successor_id = self.dependencies.explicit_record_id(raw.get("superseded_by"))
        replaced = record.get("status") == "superseded" and (
            record.get("authority_state") == "accepted"
        )
        unchanged = record.get("superseded_by") == successor_id
        if not replaced or not (unchanged or authoritative):
            raise ValueError(
                "Decision superseded_by requires status superseded from an authoritative source"
            )
        if unchanged:
            return
        decisions = self.dependencies.index_by_id(_records(graph, "decisions"))
        successor = decisions.get(successor_id)
        if successor is None or successor.get("lifecycle") == "redacted":
            raise ValueError("Decision superseded_by must name an existing decision")
        seen = {context.record_id}
        cursor: JsonObject | None = successor
        while cursor is not None:
            cursor_id = _string(cursor.get("id", ""))
            if cursor_id in seen:
                raise ValueError("Decision superseded_by would form a cycle")
            seen.add(cursor_id)
            cursor = decisions.get(_optional_string(cursor.get("superseded_by")) or "")
        record["superseded_by"] = successor_id

    def _apply_loop_resolution(
        self,
        record: JsonObject,
        raw: JsonObject,
        source: JsonObject,
        previous_status: object,
    ) -> None:
        """Record the stated outcome when a capture closes an unfinished loop."""

        resolution = self.dependencies.normalize_space(raw.get("resolution"))
        if not resolution or record.get("lifecycle") == "redacted":
            return
        if previous_status == "resolved":
            return
        if record.get("status") != "resolved":
            raise ValueError(
                "Open-loop resolution requires status resolved from an authoritative source"
            )
        record["resolution"] = resolution
        record["resolved_at"] = _string(source["observed_at"])


__all__ = ["LegacyRecordService"]
