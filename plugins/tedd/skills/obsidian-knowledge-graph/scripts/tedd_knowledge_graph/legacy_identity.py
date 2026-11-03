"""Legacy identity and entity-registry mutation semantics."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

from .legacy_mutation_contracts import (
    IdentityDependencies,
    JsonObject,
    _mutable_object_records,
    _object_records,
    _objects,
    _optional_string,
    _records,
    _string,
    _strings,
)


class LegacyIdentityService:
    """Legacy identity and entity-registry mutation semantics."""

    def __init__(self, dependencies: IdentityDependencies) -> None:
        self.dependencies = dependencies

    def index_by_id(self, records: list[JsonObject]) -> dict[str, JsonObject]:
        return {
            record_id: record
            for record in records
            if isinstance(record, dict)
            and isinstance((record_id := record.get("id")), str)
            and record_id
        }

    def identity_key_signature(
        self, raw: JsonObject, default_namespace: str
    ) -> tuple[str, str, str]:
        scheme = self.dependencies.normalize_key(raw.get("scheme"))
        namespace = self.dependencies.normalize_key(raw.get("namespace")) or default_namespace
        value = unicodedata.normalize("NFC", str(raw.get("value") or "").strip())
        if not scheme or not value:
            raise ValueError("Entity identity keys require nonempty scheme and value fields")
        return scheme, namespace, value

    def normalized_identity_keys(
        self, raw: JsonObject, default_namespace: str
    ) -> list[tuple[str, str, str]]:
        values = raw.get("identity_keys", [])
        if not isinstance(values, list):
            raise ValueError("Entity identity_keys must be a list")
        signatures: list[tuple[str, str, str]] = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("Each entity identity key must be an object")
            signature = self.dependencies.identity_key_signature(item, default_namespace)
            if signature not in signatures:
                signatures.append(signature)
        return signatures

    def canonical_entity_id(self, graph: JsonObject, entity_id: str) -> str:
        entities = self.dependencies.index_by_id(_records(graph, "entities"))
        current = entity_id
        seen: set[str] = set()
        while True:
            if current in seen:
                raise ValueError(f"Entity redirect cycle includes {current}")
            seen.add(current)
            entity = entities.get(current)
            if entity is None:
                raise ValueError(f"Unknown entity id: {current}")
            target = self.dependencies.normalize_space(entity.get("redirect_to"))
            if entity.get("lifecycle") != "redirected" or not target:
                return current
            current = target

    def canonical_entity_refs(self, graph: JsonObject, refs: Iterable[object]) -> list[str]:
        return self.dependencies.unique_strings(
            self.dependencies.canonical_entity_id(graph, self.dependencies.normalize_space(ref))
            for ref in refs
        )

    def resolve_entity_ref(self, ref: object, local_refs: dict[str, str], graph: JsonObject) -> str:
        text = self.dependencies.normalize_space(ref)
        if text in local_refs:
            return self.dependencies.canonical_entity_id(graph, local_refs[text])
        entity_ids = self.dependencies.index_by_id(_records(graph, "entities"))
        if text in entity_ids:
            return self.dependencies.canonical_entity_id(graph, text)
        key = self.dependencies.normalize_key(text)
        matches = [
            self.dependencies.canonical_entity_id(graph, _string(entity["id"]))
            for entity in _records(graph, "entities")
            if entity.get("lifecycle") != "redacted"
            if (
                self.dependencies.normalize_key(entity.get("name")) == key
                or key
                in {
                    self.dependencies.normalize_key(alias)
                    for alias in _objects(entity.get("aliases", []))
                }
            )
        ]
        matches = sorted(set(matches))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous entity reference {text!r}; use a stable entity ID")
        raise ValueError(f"Unknown entity reference {text!r}")

    def source_tier(self, value: object, fallback: str = "assistant_inference") -> str:
        tier = self.dependencies.normalize_key(value) or fallback
        if tier not in self.dependencies.source_tiers:
            raise ValueError(f"Unsupported source tier: {tier}")
        return tier

    def source_tier_rank(self, value: object) -> int:
        """Return the authority rank after narrowing a dynamic record field."""

        return self.dependencies.source_tiers.get(self.dependencies.normalize_space(value), 0)

    def capped_source_tier(
        self, value: object, ceiling: str, fallback: str = "assistant_inference"
    ) -> tuple[str, bool]:
        requested = self.dependencies.source_tier(value, fallback)
        effective = (
            requested
            if self.dependencies.source_tiers[requested] <= self.dependencies.source_tiers[ceiling]
            else ceiling
        )
        return effective, effective != requested

    def upsert_entity(
        self,
        graph: JsonObject,
        raw: JsonObject,
        source_id: str,
        observed_at: str,
        incoming_tier: str,
    ) -> tuple[str, bool]:
        name = self.dependencies.normalize_space(raw.get("name"))
        entity_type = self.dependencies.normalize_key(raw.get("type")) or "thing"
        namespace = self.dependencies.normalize_key(raw.get("namespace")) or "global"
        if not name:
            raise ValueError("Entity name is required")
        if entity_type in self.dependencies.scoped_entity_types and namespace == "global":
            raise ValueError(
                f"Entity type {entity_type!r} requires a stable namespace, such as the client or site ID"
            )
        explicit_id = self.dependencies.explicit_record_id(raw.get("id"))
        requested_keys = self.dependencies.normalized_identity_keys(raw, namespace)
        derived_identity: JsonObject
        if requested_keys:
            derived_identity = {
                "namespace": namespace,
                "type": entity_type,
                "identity_key": list(sorted(requested_keys))[0],
            }
        else:
            derived_identity = {
                "namespace": namespace,
                "type": entity_type,
                "name": self.dependencies.normalize_key(name),
            }
        derived_id = self.dependencies.stable_id("entity", derived_identity)
        by_id = self.dependencies.index_by_id(_records(graph, "entities"))
        key_matches = {
            self.dependencies.canonical_entity_id(graph, _string(item["id"]))
            for item in _records(graph, "entities")
            if item.get("lifecycle") != "redacted"
            for identity_key in _object_records(item.get("identity_keys", []))
            if self.dependencies.identity_key_signature(
                identity_key,
                self.dependencies.normalize_key(item.get("namespace")) or "global",
            )
            in requested_keys
        }
        if len(key_matches) > 1:
            raise ValueError(
                f"Entity identity keys resolve to more than one canonical entity: {sorted(key_matches)}"
            )
        identity_matches = {
            self.dependencies.canonical_entity_id(graph, _string(item["id"]))
            for item in _records(graph, "entities")
            if item.get("lifecycle") != "redacted"
            and (self.dependencies.normalize_key(item.get("namespace")) or "global") == namespace
            and (self.dependencies.normalize_key(item.get("type")) or "thing") == entity_type
            and self.dependencies.normalize_key(item.get("name"))
            == self.dependencies.normalize_key(name)
        }
        if len(identity_matches) > 1:
            raise ValueError(
                f"Stored entity identity is ambiguous for {namespace}:{entity_type}:{name}"
            )
        explicit_canonical = (
            self.dependencies.canonical_entity_id(graph, explicit_id)
            if explicit_id in by_id
            else explicit_id
        )
        matched_id = next(iter(key_matches or identity_matches), None)
        if explicit_id and matched_id and matched_id != explicit_canonical:
            raise ValueError(
                f"Entity identity already belongs to {matched_id}; cannot assign {explicit_id}"
            )
        candidate_id = explicit_canonical or matched_id or derived_id
        existing = by_id.get(candidate_id)
        if existing and existing.get("lifecycle") == "redacted":
            return candidate_id, False
        if existing:
            existing_identity = (
                self.dependencies.normalize_key(existing.get("namespace")) or "global",
                self.dependencies.normalize_key(existing.get("type")) or "thing",
            )
            requested_identity = (namespace, entity_type)
            if existing_identity != requested_identity:
                raise ValueError(
                    f"Canonical entity {candidate_id!r} does not match its immutable namespace and type"
                )
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            raise ValueError("Entity aliases must be a list")
        if existing:
            existing["sensitive"] = bool(existing.get("sensitive")) or bool(
                raw.get("sensitive", False)
            )
            prior_last_seen = _string(existing.get("last_seen_at") or observed_at)
            if self.dependencies.source_tiers[incoming_tier] >= self.dependencies.source_tier_rank(
                existing.get("source_tier")
            ):
                if self.dependencies.normalize_key(
                    existing.get("name")
                ) != self.dependencies.normalize_key(name) and self.dependencies.time_value(
                    observed_at
                ) >= self.dependencies.time_value(prior_last_seen):
                    prior_name = self.dependencies.normalize_space(existing.get("name"))
                    existing["name"] = name
                    existing["aliases"] = self.dependencies.unique_strings(
                        [*_objects(existing.get("aliases", [])), prior_name]
                    )
                existing["aliases"] = self.dependencies.unique_strings(
                    [*_objects(existing.get("aliases", [])), name, *aliases]
                )
                existing["source_tier"] = incoming_tier
                stored_keys = {
                    self.dependencies.identity_key_signature(item, namespace): item
                    for item in _object_records(existing.get("identity_keys", []))
                }
                for signature in requested_keys:
                    item = stored_keys.get(signature)
                    if item is None:
                        item = {
                            "scheme": signature[0],
                            "namespace": signature[1],
                            "value": signature[2],
                            "first_seen_at": observed_at,
                            "last_seen_at": observed_at,
                            "evidence_refs": [source_id],
                        }
                        _mutable_object_records(existing.setdefault("identity_keys", [])).append(
                            item
                        )
                        stored_keys[signature] = item
                    else:
                        item["last_seen_at"] = self.dependencies.latest_time(
                            _optional_string(item.get("last_seen_at")), observed_at
                        )
                        item["evidence_refs"] = self.dependencies.unique_strings(
                            [*_objects(item.get("evidence_refs", [])), source_id]
                        )
            if (
                raw.get("status")
                and self.dependencies.time_value(observed_at)
                >= self.dependencies.time_value(prior_last_seen)
                and self.dependencies.source_tiers[incoming_tier]
                >= self.dependencies.source_tier_rank(
                    existing.get("status_source_tier", existing.get("source_tier"))
                )
            ):
                existing["status"] = self.dependencies.normalize_key(raw["status"])
                existing["status_source_tier"] = incoming_tier
            existing["first_seen_at"] = self.dependencies.earliest_time(
                _optional_string(existing.get("first_seen_at")), observed_at
            )
            existing["last_seen_at"] = self.dependencies.latest_time(
                _optional_string(existing.get("last_seen_at")), observed_at
            )
            existing["evidence_refs"] = self.dependencies.unique_strings(
                [*_objects(existing.get("evidence_refs", [])), source_id]
            )
            return candidate_id, True
        identity_keys = [
            {
                "scheme": signature[0],
                "namespace": signature[1],
                "value": signature[2],
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "evidence_refs": [source_id],
            }
            for signature in requested_keys
        ]
        if not identity_keys:
            identity_keys = [
                {
                    "scheme": "legacy_name",
                    "namespace": namespace,
                    "value": self.dependencies.normalize_key(name),
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "evidence_refs": [source_id],
                }
            ]
        record = {
            "id": candidate_id,
            "namespace": namespace,
            "type": entity_type,
            "name": name,
            "aliases": self.dependencies.unique_strings([name, *aliases]),
            "identity_keys": identity_keys,
            "lifecycle": "active",
            "status": self.dependencies.normalize_key(raw.get("status")) or "current",
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
            "evidence_refs": [source_id],
            "source_tier": incoming_tier,
            "status_source_tier": incoming_tier,
            "sensitive": bool(raw.get("sensitive", False)),
        }
        _records(graph, "entities").append(record)
        return candidate_id, True

    def upsert_relation(
        self,
        graph: JsonObject,
        raw: JsonObject,
        local_refs: dict[str, str],
        source_id: str,
        observed_at: str,
        incoming_tier: str,
    ) -> tuple[str, bool]:
        source = self.dependencies.resolve_entity_ref(raw.get("source"), local_refs, graph)
        target = self.dependencies.resolve_entity_ref(raw.get("target"), local_refs, graph)
        relation_type = self.dependencies.normalize_key(raw.get("type"))
        if not relation_type:
            raise ValueError("Relation type is required")
        relation_id = self.dependencies.stable_id(
            "relation", {"source": source, "target": target, "type": relation_type}
        )
        existing = self.dependencies.index_by_id(_records(graph, "relations")).get(relation_id)
        if existing:
            if existing.get("lifecycle") == "redacted":
                return relation_id, False
            existing["sensitive"] = bool(existing.get("sensitive")) or bool(
                raw.get("sensitive", False)
            )
            prior_last_observed = _string(existing.get("last_observed_at") or observed_at)
            existing["first_observed_at"] = self.dependencies.earliest_time(
                _optional_string(existing.get("first_observed_at")), observed_at
            )
            existing["last_observed_at"] = self.dependencies.latest_time(
                _optional_string(existing.get("last_observed_at")), observed_at
            )
            existing["evidence_refs"] = self.dependencies.unique_strings(
                [*_objects(existing.get("evidence_refs", [])), source_id]
            )
            if (
                raw.get("status")
                and self.dependencies.time_value(observed_at)
                >= self.dependencies.time_value(prior_last_observed)
                and self.dependencies.source_tiers[incoming_tier]
                >= self.dependencies.source_tier_rank(
                    existing.get("status_source_tier", existing.get("source_tier"))
                )
            ):
                existing["status"] = self.dependencies.normalize_key(raw["status"])
                existing["status_source_tier"] = incoming_tier
                existing["source_tier"] = incoming_tier
            return relation_id, True
        _records(graph, "relations").append(
            {
                "id": relation_id,
                "source_entity_id": source,
                "target_entity_id": target,
                "type": relation_type,
                "status": self.dependencies.normalize_key(raw.get("status")) or "current",
                "first_observed_at": observed_at,
                "last_observed_at": observed_at,
                "evidence_refs": [source_id],
                "source_tier": incoming_tier,
                "status_source_tier": incoming_tier,
                "sensitive": bool(raw.get("sensitive", False)),
            }
        )
        return relation_id, True

    def resolved_entity_refs(
        self,
        raw: JsonObject,
        field: str,
        local_refs: dict[str, str],
        graph: JsonObject,
    ) -> list[str]:
        refs = raw.get(field, [])
        if not isinstance(refs, list):
            raise ValueError(f"{field} must be a list")
        return self.dependencies.unique_strings(
            self.dependencies.resolve_entity_ref(ref, local_refs, graph) for ref in refs
        )

    def record_entity_refs(
        self, raw: JsonObject, local_refs: dict[str, str], graph: JsonObject
    ) -> list[str]:
        return self.dependencies.resolved_entity_refs(raw, "entity_refs", local_refs, graph)

    def add_entity_redirect(
        self,
        graph: JsonObject,
        raw: JsonObject,
        local_refs: dict[str, str],
        source: JsonObject,
    ) -> tuple[str, bool]:
        if (
            self.dependencies.source_tier_rank(source.get("source_tier"))
            < self.dependencies.source_tiers["validated_structured_candidate"]
        ):
            raise ValueError("Entity redirects require validated evidence authority")
        entities = self.dependencies.index_by_id(_records(graph, "entities"))

        def direct_ref(value: object, label: str) -> str:
            text = self.dependencies.normalize_space(value)
            candidate = local_refs.get(text, text)
            if candidate not in entities:
                raise ValueError(
                    f"Entity redirect {label} must be a stable entity ID or packet-local ref"
                )
            return candidate

        source_entity_id = direct_ref(raw.get("from"), "from")
        requested_target_id = direct_ref(raw.get("to"), "to")
        target_entity_id = self.dependencies.canonical_entity_id(graph, requested_target_id)
        if source_entity_id == target_entity_id:
            raise ValueError("Entity redirect cannot target itself")
        source_entity = entities[source_entity_id]
        target_entity = entities[target_entity_id]
        if (
            source_entity.get("lifecycle") == "redacted"
            or target_entity.get("lifecycle") == "redacted"
        ):
            raise ValueError("Redacted entities cannot participate in redirects")
        if source_entity.get("lifecycle") == "redirected":
            if self.dependencies.canonical_entity_id(graph, source_entity_id) != target_entity_id:
                raise ValueError("Entity is already redirected to a different canonical entity")
            return source_entity_id, False
        source_identity = (
            self.dependencies.normalize_key(source_entity.get("namespace")) or "global",
            self.dependencies.normalize_key(source_entity.get("type")) or "thing",
        )
        target_identity = (
            self.dependencies.normalize_key(target_entity.get("namespace")) or "global",
            self.dependencies.normalize_key(target_entity.get("type")) or "thing",
        )
        if source_identity != target_identity:
            raise ValueError("Entity redirects require matching immutable namespace and type")
        resulting_surfaces: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for surface in _records(graph, "claim_surfaces"):
            subjects = [
                target_entity_id
                if self.dependencies.canonical_entity_id(graph, subject_id) == source_entity_id
                else self.dependencies.canonical_entity_id(graph, subject_id)
                for subject_id in _strings(surface.get("subject_refs", []))
            ]
            signature = (
                self.dependencies.normalize_key(surface.get("family")),
                self.dependencies.normalize_key(surface.get("key")),
                tuple(sorted(set(subjects))),
            )
            prior_surface = resulting_surfaces.get(signature)
            if prior_surface and prior_surface != surface.get("id"):
                raise ValueError(
                    "Entity redirect would collapse distinct claim surfaces; reconcile "
                    f"{prior_surface} and {surface.get('id')} explicitly first"
                )
            resulting_surfaces[signature] = _string(surface.get("id"))
        reason = self.dependencies.normalize_space(raw.get("reason"))
        if not reason:
            raise ValueError("Entity redirect reason is required")
        source_entity["lifecycle"] = "redirected"
        source_entity["redirect_to"] = target_entity_id
        source_entity["redirected_at"] = source["captured_at"]
        source_entity["redirect_source_ref"] = source["id"]
        source_entity["redirect_reason"] = reason
        source_entity["evidence_refs"] = self.dependencies.unique_strings(
            [*_objects(source_entity.get("evidence_refs", [])), source["id"]]
        )
        return source_entity_id, True


__all__ = ["LegacyIdentityService"]
