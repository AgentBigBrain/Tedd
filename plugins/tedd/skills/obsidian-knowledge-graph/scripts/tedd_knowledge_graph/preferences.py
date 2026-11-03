"""Shared preference admission rules, independent of storage and host setup."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

PREFERENCE_SCOPE_PREFIX = "entity_scope_operator_preferences_"
_LABELS = re.compile(r"\b(Prefer|When|Because):")
_REFERENCE_FIELDS = {
    "entity_refs",
    "owner_refs",
    "subject_refs",
    "evidence_refs",
    "observation_refs",
    "source_ref",
    "surface_ref",
    "superseded_by",
    "redirect_to",
    "redirect_source_ref",
    "source_entity_id",
    "target_entity_id",
    "source",
    "target",
    "from",
    "to",
}


def preference_scope_id(operator_id: str) -> str:
    """An opaque, stable scope for the explicitly selected private operator."""
    return (
        PREFERENCE_SCOPE_PREFIX
        + hashlib.sha256(("tedd:operator-preferences:" + operator_id).encode("utf-8")).hexdigest()[
            :32
        ]
    )


def is_preference_scope(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFERENCE_SCOPE_PREFIX)


def validate_preference_value(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("A preference value must be text")
    labels = list(_LABELS.finditer(value))
    if [item.group(1) for item in labels] != ["Prefer", "When", "Because"]:
        raise ValueError("A preference requires Prefer: ... When: ... Because: ...")
    ends = [item.start() for item in labels[1:]] + [len(value)]
    if value[: labels[0].start()].strip() or any(
        not value[item.end() : end].strip() for item, end in zip(labels, ends, strict=True)
    ):
        raise ValueError("Each preference label needs a nonempty value")


def validate_preference_record(collection: str, payload: Mapping[str, object]) -> None:
    """Only preference claims/surfaces may reference the reserved global scope.

    Inspect reference fields, not prose. Claims carry their surface's subject in
    entity_refs; that redundant canonical reference is permitted, extra context is not.
    """
    references: set[str] = set()
    for field in _REFERENCE_FIELDS:
        value = payload.get(field)
        values = value if isinstance(value, list) else [value]
        references.update(item for item in values if isinstance(item, str))
    identities = payload.get("identity_keys")
    if isinstance(identities, list):
        for item in identities:
            if isinstance(item, Mapping):
                evidence = item.get("evidence_refs")
                if isinstance(evidence, list):
                    references.update(value for value in evidence if isinstance(value, str))
    scopes = {ref for ref in references if is_preference_scope(ref)}
    if collection == "entities" and is_preference_scope(payload.get("id")):
        if payload.get("type") != "scope" or payload.get("redirect_to"):
            raise ValueError("The operator preference scope must remain a scope entity")
    preference = (
        collection in {"claims", "claim_surfaces"} and payload.get("family") == "preference"
    )
    if preference:
        if collection == "claims":
            validate_preference_value(payload.get("value"))
        else:
            subjects = payload.get("subject_refs")
            if not isinstance(subjects, list) or len(subjects) != 1:
                raise ValueError("A preference surface requires exactly one subject")
        if scopes:
            subject_field = "entity_refs" if collection == "claims" else "subject_refs"
            if len(scopes) != 1 or payload.get(subject_field) != sorted(scopes):
                raise ValueError("A global preference cannot include client context")
            for field in _REFERENCE_FIELDS - {subject_field}:
                value = payload.get(field)
                if any(
                    is_preference_scope(ref)
                    for ref in (value if isinstance(value, list) else [value])
                ):
                    raise ValueError(
                        "The preference scope is only a subject, not evidence or context"
                    )
    elif scopes:
        raise ValueError(
            "Only preference claims and their surfaces may reference the preference scope"
        )
