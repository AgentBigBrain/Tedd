"""Advisory hints for a capture dry run: an existing claim slot, or a similar label.

A hint never changes what a packet saves.  It is computed after the reviewed
plan, stays out of the capture fingerprint, and is returned only by a dry run.
Every hint is drawn from records about the same entities the new record names,
so it cannot surface another subject's vocabulary.

This module only reads.  The caller owns the connection and its snapshot.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import TypedDict

MAX_SUGGESTIONS = 10
MAX_SIMILAR_LABELS = 3
MAX_SLOTS_PER_NOTE = 2
MAX_LABELS_READ = 200
MAX_SLOTS_READ = 200
MAX_ENTITIES = 20
MAX_LABEL_CHARS = 120
# A note resembles a slot when it shares this many label words with it and
# those words cover at least half of the slot's own.
MIN_SHARED_SLOT_WORDS = 2

# The free-text label each collection carries; a fixed allowlist, never caller input.
_LABEL_FIELDS: Mapping[str, str] = {"events": "kind", "receipts": "kind", "observations": "family"}
_WORD = re.compile(r"[a-z0-9]+")


class SimilarLabel(TypedDict):
    label: str
    uses: int


class LabelSuggestion(TypedDict):
    """A new record uses a label its entities have not seen, near ones they have."""

    kind: str
    record_id: str
    collection: str
    field: str
    label: str
    similar: list[SimilarLabel]


class SlotSuggestion(TypedDict):
    """A new observation resembles a claim slot that already exists for its subject."""

    kind: str
    record_id: str
    family: str
    key: str
    surface_id: str
    slot_family: str
    slot_key: str
    shared_words: int


class Suggestions(TypedDict):
    items: list[LabelSuggestion | SlotSuggestion]
    truncated: bool


def label_words(*labels: object, minimum: int = 1) -> frozenset[str]:
    """Split labels into lower-case words of at least ``minimum`` characters."""

    return frozenset(
        word
        for label in labels
        if isinstance(label, str)
        for word in _WORD.findall(label.lower())
        if len(word) >= minimum
    )


def labels_are_similar(first: frozenset[str], second: frozenset[str]) -> bool:
    """Two labels are similar when one's words contain the other's, or half overlap."""

    shared = len(first & second)
    if not shared:
        return False
    return first <= second or second <= first or shared * 2 >= len(first | second)


def _text(value: object) -> str:
    return value[:MAX_LABEL_CHARS] if isinstance(value, str) else ""


def _entities(payload: Mapping[str, object]) -> tuple[str, ...]:
    refs = payload.get("entity_refs")
    if not isinstance(refs, list):
        return ()
    return tuple(sorted({ref for ref in refs if isinstance(ref, str)}))[:MAX_ENTITIES]


def _known_labels(
    connection: sqlite3.Connection, collection: str, entities: Sequence[str]
) -> list[tuple[str, int]]:
    """Labels that visible records about these entities already use, most used first."""

    placeholders = ",".join("?" for _ in entities)
    rows = connection.execute(
        f"""
        SELECT json_extract(p.payload_json,?) AS label,COUNT(*) AS uses
        FROM records r
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE r.collection=? AND r.sensitive=0
          AND COALESCE(r.lifecycle,'') NOT IN ('redacted','tombstoned')
          AND EXISTS (SELECT 1 FROM revision_refs rr
                      WHERE rr.record_id=r.record_id AND rr.revision=r.current_revision
                        AND rr.ref_kind='entity' AND rr.target_record_id IN ({placeholders}))
        GROUP BY label ORDER BY uses DESC,label LIMIT ?
        """,
        [f"$.{_LABEL_FIELDS[collection]}", collection, *entities, MAX_LABELS_READ],
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows if isinstance(row[0], str) and row[0]]


def _subject_slots(
    connection: sqlite3.Connection, entities: Sequence[str]
) -> list[tuple[str, str, str]]:
    """Active, non-sensitive, non-preference claim slots whose subject is one of the entities."""

    placeholders = ",".join("?" for _ in entities)
    rows = connection.execute(
        f"""
        SELECT DISTINCT c.surface_id,c.family,c.key_name
        FROM claim_surface_constraints c
        JOIN claim_surface_subjects s ON s.surface_id=c.surface_id
        JOIN records r ON r.record_id=c.surface_id
        WHERE s.entity_id IN ({placeholders}) AND r.sensitive=0 AND r.lifecycle='active'
          AND c.family<>'preference'
        ORDER BY c.surface_id LIMIT ?
        """,
        [*entities, MAX_SLOTS_READ],
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def _label_suggestion(
    record_id: str, collection: str, label: str, known: Sequence[tuple[str, int]]
) -> LabelSuggestion | None:
    if not label or any(label == existing for existing, _ in known):
        return None
    words = label_words(label)
    similar = [
        (len(words & label_words(existing)), uses, existing)
        for existing, uses in known
        if labels_are_similar(words, label_words(existing))
    ]
    if not similar:
        return None
    similar.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return {
        "kind": "similar_label",
        "record_id": record_id,
        "collection": collection,
        "field": _LABEL_FIELDS[collection],
        "label": _text(label),
        "similar": [
            {"label": _text(existing), "uses": uses}
            for _, uses, existing in similar[:MAX_SIMILAR_LABELS]
        ],
    }


def _slot_suggestions(
    record_id: str, payload: Mapping[str, object], slots: Sequence[tuple[str, str, str]]
) -> list[SlotSuggestion]:
    family = _text(payload.get("family"))
    key = _text(payload.get("key"))
    words = label_words(family, key, minimum=3)
    matches: list[tuple[int, str, str, str]] = []
    for surface_id, slot_family, slot_key in slots:
        slot_words = label_words(slot_family, slot_key, minimum=3)
        shared = len(words & slot_words)
        if shared >= MIN_SHARED_SLOT_WORDS and shared * 2 >= len(slot_words):
            matches.append((shared, surface_id, slot_family, slot_key))
    matches.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "kind": "existing_claim_slot",
            "record_id": record_id,
            "family": family,
            "key": key,
            "surface_id": surface_id,
            "slot_family": _text(slot_family),
            "slot_key": _text(slot_key),
            "shared_words": shared,
        }
        for shared, surface_id, slot_family, slot_key in matches[:MAX_SLOTS_PER_NOTE]
    ]


def capture_suggestions(
    connection: sqlite3.Connection, changes: Sequence[Mapping[str, object]]
) -> Suggestions:
    """Suggest existing slots and labels for the records one capture would create.

    ``changes`` is the reviewed plan's record list.  Only records the capture
    would create are considered, and only against stored records about the same
    entities.  At most ``MAX_SUGGESTIONS`` items are returned, in plan order;
    ``truncated`` reports more.  The result never affects what is saved.
    """

    items: list[LabelSuggestion | SlotSuggestion] = []
    labels: dict[tuple[str, tuple[str, ...]], list[tuple[str, int]]] = {}
    slots: dict[tuple[str, ...], list[tuple[str, str, str]]] = {}
    for change in changes:
        collection = change.get("collection")
        payload = change.get("payload")
        record_id = change.get("record_id")
        if (
            change.get("change_kind") != "create"
            or not isinstance(collection, str)
            or collection not in _LABEL_FIELDS
            or not isinstance(payload, Mapping)
            or not isinstance(record_id, str)
        ):
            continue
        entities = _entities(payload)
        if not entities:
            continue
        if collection == "observations":
            if entities not in slots:
                slots[entities] = _subject_slots(connection, entities)
            items.extend(_slot_suggestions(record_id, payload, slots[entities]))
        if (collection, entities) not in labels:
            labels[(collection, entities)] = _known_labels(connection, collection, entities)
        label = payload.get(_LABEL_FIELDS[collection])
        suggestion = (
            _label_suggestion(record_id, collection, label, labels[(collection, entities)])
            if isinstance(label, str)
            else None
        )
        if suggestion is not None:
            items.append(suggestion)
        if len(items) > MAX_SUGGESTIONS:
            break
    return {"items": items[:MAX_SUGGESTIONS], "truncated": len(items) > MAX_SUGGESTIONS}


__all__ = [
    "MAX_SUGGESTIONS",
    "Suggestions",
    "capture_suggestions",
    "label_words",
    "labels_are_similar",
]
