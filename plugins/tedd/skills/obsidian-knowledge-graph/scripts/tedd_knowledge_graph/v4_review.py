"""Read-only review kernels: current state, waiting work, and notes to fold.

Every function receives the caller's subscription predicate, written against
the ``r`` (records) and ``h`` (current revision header) aliases, together with
its bound parameters.  Scope and privacy filters run before any limit or
count, so a record outside the subscription, or a sensitive one, can neither
appear nor change a total.

The storage facade owns the connection and the read snapshot.  This module
performs no writes, starts no transactions, and takes the current time as an
argument.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Sequence
from typing import TypeAlias, TypedDict, cast

from .v4_review_answers import StoredRecord

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

CURRENT_STATE_LABEL = "current claims, with dates"
CURRENT_STATE_ITEMS = 12
MAX_QUEUE_ITEMS = 50
MAX_FOLD_ITEMS = 50
MAX_FOLD_FAMILIES = 50
MAX_SCOPED_RECORDS = 100
DEFAULT_AGE_DAYS = 30
MAX_AGE_DAYS = 36_500

# Evidence at or above this authority counts as confirming a current claim.
_CONFIRMING_TIERS = ("validated_structured_candidate", "explicit_user_statement")
_VISIBLE = "r.sensitive=0 AND COALESCE(r.lifecycle,'') NOT IN ('redacted','tombstoned')"
_CURRENT_JOIN = (
    "JOIN record_revision_headers h ON h.record_id=r.record_id AND h.revision=r.current_revision "
    "JOIN record_revision_payloads p ON p.record_id=r.record_id AND p.revision=r.current_revision"
)
# Bound parameters: one for the undated-loop age threshold.
_OVERDUE = "((checkpoint IS NOT NULL AND elapsed>0) OR (checkpoint IS NULL AND elapsed>?))"
_PROPOSED = (
    "(json_extract(p.payload_json,'$.authority_state')='candidate' "
    "OR json_extract(p.payload_json,'$.status') IN ('proposed','candidate'))"
)
# Bound parameters: one for the current time.
_PROPOSAL_AGE = (
    "julianday(?) - julianday(COALESCE(json_extract(p.payload_json,'$.decided_at'),"
    "json_extract(p.payload_json,'$.first_observed_at')))"
)


class CurrentClaim(TypedDict):
    """One current claim; stored values keep their saved JSON types."""

    id: str
    revision: int
    surface_ref: JsonValue
    subject_refs: JsonValue
    family: JsonValue
    key: JsonValue
    value: JsonValue
    valid_from: JsonValue
    last_confirmed_at: JsonValue
    source_tier: JsonValue
    lifecycle: JsonValue
    effective_conflict: bool
    cited_observations: int


class CurrentState(TypedDict):
    label: str
    items: list[CurrentClaim]
    truncated: bool
    omitted: bool


class CommitmentCounts(TypedDict):
    unfinished: int
    overdue: int
    unassigned: int
    agent_owned: int
    named_owner: int


class ProposalCounts(TypedDict):
    undecided: int
    older_than_days: int
    older: int


class NoteCounts(TypedDict):
    unfolded: int


class WaitingSummary(TypedDict):
    commitments: CommitmentCounts
    proposals: ProposalCounts
    notes: NoteCounts


class Commitment(TypedDict):
    id: str
    title: JsonValue
    status: JsonValue
    source_tier: JsonValue
    authority_state: JsonValue
    owner_refs: JsonValue
    owner_kind: str
    next_action: JsonValue
    checkpoint_at: JsonValue
    overdue: bool
    days_overdue: int | None


class Proposal(TypedDict):
    id: str
    title: JsonValue
    status: JsonValue
    requested_status: JsonValue
    source_tier: JsonValue
    authority_state: JsonValue
    decided_at: JsonValue
    age_days: int


class SubjectNotes(TypedDict):
    subject_id: str
    unfolded: int


class CommitmentSection(TypedDict):
    items: list[Commitment]
    truncated: bool


class ProposalSection(TypedDict):
    items: list[Proposal]
    truncated: bool


class SubjectNotesSection(TypedDict):
    items: list[SubjectNotes]
    truncated: bool


class ReviewQueue(TypedDict):
    subscription_id: str
    summary: WaitingSummary
    commitments: CommitmentSection
    proposals: ProposalSection
    notes: SubjectNotesSection


class FoldNote(TypedDict):
    id: str
    family: JsonValue
    key: JsonValue
    value: JsonValue
    observed_at: JsonValue
    source_tier: JsonValue
    entity_refs: JsonValue


class FamilyCount(TypedDict):
    family: str
    notes: int


class FamilySection(TypedDict):
    items: list[FamilyCount]
    truncated: bool


class FoldNoteSection(TypedDict):
    items: list[FoldNote]
    truncated: bool


class SlotSection(TypedDict):
    items: list[CurrentClaim]
    truncated: bool


class FoldCandidates(TypedDict):
    subscription_id: str
    subject_id: str | None
    family: str | None
    unfolded: int
    families: FamilySection
    notes: FoldNoteSection
    slots: SlotSection


def _payload(value: object) -> JsonObject:
    """Decode a stored payload; canonical validation guarantees an object."""

    decoded = cast(JsonValue, json.loads(str(value)))
    if not isinstance(decoded, dict):
        raise ValueError("Stored record payload must be an object")
    return decoded


def _instant(value: object) -> dt.datetime | None:
    """Read stored ISO text as a UTC instant; absent or unreadable text is ``None``."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _bounded(value: int, maximum: int, label: str) -> int:
    # An exact type check keeps a JSON boolean from passing as a count.
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} is outside its supported range")
    return value


def _unfinished_loops(predicate: str) -> str:
    """Select visible unfinished loops in scope.

    Bound parameters, in order: the current time, then the predicate's own.
    """

    return f"""
        SELECT r.record_id,p.payload_json,
               json_extract(p.payload_json,'$.checkpoint_at') AS checkpoint,
               julianday(?) - julianday(COALESCE(
                   json_extract(p.payload_json,'$.checkpoint_at'),
                   json_extract(p.payload_json,'$.first_observed_at'))) AS elapsed,
               (SELECT COUNT(*) FROM revision_refs o
                 WHERE o.record_id=r.record_id AND o.revision=r.current_revision
                   AND o.ref_kind='owner') AS owners,
               (SELECT COUNT(*) FROM revision_refs o
                  JOIN records e ON e.record_id=o.target_record_id
                  JOIN record_revision_payloads ep
                    ON ep.record_id=e.record_id AND ep.revision=e.current_revision
                 WHERE o.record_id=r.record_id AND o.revision=r.current_revision
                   AND o.ref_kind='owner' AND e.collection='entities'
                   AND json_extract(ep.payload_json,'$.type')='agent') AS agent_owners
        FROM records r {_CURRENT_JOIN}
        WHERE ({predicate}) AND r.collection='open_loops' AND {_VISIBLE}
          AND json_extract(p.payload_json,'$.status') IN ('open','candidate')
    """


def _last_confirmed(connection: sqlite3.Connection, record_id: str, revision: int) -> str | None:
    """Latest observation time among a claim's validated evidence sources."""

    placeholders = ",".join("?" for _ in _CONFIRMING_TIERS)
    rows = connection.execute(
        f"""
        SELECT json_extract(sp.payload_json,'$.observed_at')
        FROM revision_refs e
        JOIN records s ON s.record_id=e.target_record_id AND s.collection='sources'
        JOIN record_revision_payloads sp
          ON sp.record_id=s.record_id AND sp.revision=s.current_revision
        WHERE e.record_id=? AND e.revision=? AND e.ref_kind='evidence'
          AND s.source_tier IN ({placeholders})
        """,
        [record_id, revision, *_CONFIRMING_TIERS],
    ).fetchall()
    instants = [instant for row in rows if (instant := _instant(row[0])) is not None]
    if not instants:
        return None
    return max(instants).isoformat(timespec="seconds")


def _current_claims(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    subject_ids: Sequence[str],
    all_subjects: bool,
    only_subject: str | None,
    limit: int,
) -> tuple[list[CurrentClaim], bool]:
    """Read current non-preference claims, approved subjects first, newest first."""

    subject_filter = ""
    subject_parameters: list[object] = []
    if only_subject is not None:
        subject_filter = (
            "AND EXISTS (SELECT 1 FROM json_each(sp.payload_json,'$.subject_refs') only_s "
            "WHERE only_s.value=?)"
        )
        subject_parameters.append(only_subject)
    rows = connection.execute(
        f"""
        SELECT r.record_id,r.current_revision,p.payload_json,sp.payload_json AS surface_json,
               CASE WHEN ? OR EXISTS (
                   SELECT 1 FROM json_each(sp.payload_json,'$.subject_refs') s
                   WHERE s.value IN (SELECT value FROM json_each(?))) THEN 0 ELSE 1 END AS distance
        FROM records r {_CURRENT_JOIN}
        JOIN records sr ON sr.record_id=json_extract(p.payload_json,'$.surface_ref')
        JOIN record_revision_payloads sp
          ON sp.record_id=sr.record_id AND sp.revision=sr.current_revision
        WHERE ({predicate}) AND r.collection='claims' AND r.sensitive=0
          AND r.lifecycle IN ('current','conflicting')
          AND json_extract(p.payload_json,'$.active')=1
          AND COALESCE(json_extract(p.payload_json,'$.family'),'')<>'preference'
          AND sr.collection='claim_surfaces' AND sr.sensitive=0 AND sr.lifecycle='active'
          {subject_filter}
        ORDER BY distance,r.last_watermark DESC,r.record_id
        LIMIT ?
        """,
        [
            int(all_subjects),
            json.dumps(list(subject_ids)),
            *parameters,
            *subject_parameters,
            limit + 1,
        ],
    ).fetchall()
    items: list[CurrentClaim] = []
    for row in rows[:limit]:
        record_id = str(row["record_id"])
        revision = int(row["current_revision"])
        payload = _payload(row["payload_json"])
        surface = _payload(row["surface_json"])
        cited = payload.get("observation_refs")
        lifecycle = payload.get("lifecycle")
        items.append(
            {
                "id": record_id,
                "revision": revision,
                "surface_ref": payload.get("surface_ref"),
                "subject_refs": surface.get("subject_refs", []),
                "family": payload.get("family"),
                "key": payload.get("key"),
                "value": payload.get("value"),
                "valid_from": payload.get("valid_from"),
                "last_confirmed_at": _last_confirmed(connection, record_id, revision)
                or payload.get("asserted_at"),
                "source_tier": payload.get("source_tier"),
                "lifecycle": lifecycle,
                "effective_conflict": lifecycle == "conflicting",
                "cited_observations": len(cited) if isinstance(cited, list) else 0,
            }
        )
    return items, len(rows) > limit


def current_state(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    subject_ids: Sequence[str],
    all_subjects: bool,
) -> CurrentState:
    """Return the current claims a task may read, as a dated fact sheet.

    Preferences have their own section and are left out.  Claims whose subject
    the subscription names explicitly come first, then the most recently
    changed.  ``last_confirmed_at`` is the latest validated evidence time, so a
    restated claim shows when it was last checked.  At most
    ``CURRENT_STATE_ITEMS`` items are returned; ``truncated`` reports more.
    """

    items, truncated = _current_claims(
        connection,
        predicate,
        parameters,
        subject_ids=subject_ids,
        all_subjects=all_subjects,
        only_subject=None,
        limit=CURRENT_STATE_ITEMS,
    )
    return {
        "label": CURRENT_STATE_LABEL,
        "items": items,
        "truncated": truncated,
        "omitted": False,
    }


def waiting_summary(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    fold_clause: str,
    fold_parameters: Sequence[object],
    now: str,
    age_days: int = DEFAULT_AGE_DAYS,
) -> WaitingSummary:
    """Count what waits in scope: commitments, undecided proposals, unfolded notes.

    Counts are exact because no limit applies.  A commitment is overdue when its
    checkpoint has passed, or when it has no checkpoint and was first observed
    more than ``age_days`` ago.  ``fold_clause`` must be true for an observation
    that a visible claim already cites.

    Raises:
        ValueError: If ``age_days`` is not an integer from 1 to ``MAX_AGE_DAYS``.
    """

    age_days = _bounded(age_days, MAX_AGE_DAYS, "review age")
    loops = connection.execute(
        f"""
        WITH scoped AS ({_unfinished_loops(predicate)})
        SELECT COUNT(*) AS unfinished,
               COALESCE(SUM(CASE WHEN {_OVERDUE} THEN 1 ELSE 0 END),0) AS overdue,
               COALESCE(SUM(owners=0),0) AS unassigned,
               COALESCE(SUM(owners>0 AND agent_owners=owners),0) AS agent_owned,
               COALESCE(SUM(owners>agent_owners),0) AS named_owner
        FROM scoped
        """,
        [now, *parameters, age_days],
    ).fetchone()
    proposals = connection.execute(
        f"""
        SELECT COUNT(*) AS undecided,COALESCE(SUM({_PROPOSAL_AGE}>?),0) AS older
        FROM records r {_CURRENT_JOIN}
        WHERE ({predicate}) AND r.collection='decisions' AND {_VISIBLE} AND {_PROPOSED}
        """,
        [now, age_days, *parameters],
    ).fetchone()
    notes = connection.execute(
        f"""
        SELECT COUNT(*) AS unfolded FROM records r {_CURRENT_JOIN}
        WHERE ({predicate}) AND r.collection='observations' AND {_VISIBLE}
          AND NOT {fold_clause}
        """,
        [*parameters, *fold_parameters],
    ).fetchone()
    return {
        "commitments": {
            "unfinished": int(loops["unfinished"]),
            "overdue": int(loops["overdue"]),
            "unassigned": int(loops["unassigned"]),
            "agent_owned": int(loops["agent_owned"]),
            "named_owner": int(loops["named_owner"]),
        },
        "proposals": {
            "undecided": int(proposals["undecided"]),
            "older_than_days": age_days,
            "older": int(proposals["older"]),
        },
        "notes": {"unfolded": int(notes["unfolded"])},
    }


def _owner_kind(owners: int, agent_owners: int) -> str:
    if owners == 0:
        return "none"
    return "agent" if agent_owners == owners else "named"


def _commitments(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    now: str,
    age_days: int,
    limit: int,
) -> CommitmentSection:
    rows = connection.execute(
        f"""
        WITH scoped AS ({_unfinished_loops(predicate)}),
        ranked AS (
            SELECT scoped.*,
                   CASE WHEN {_OVERDUE} THEN 1 ELSE 0 END AS overdue,
                   CAST(elapsed - CASE WHEN checkpoint IS NULL THEN ? ELSE 0 END AS INTEGER)
                       AS days_overdue
            FROM scoped
        )
        SELECT record_id,payload_json,owners,agent_owners,overdue,days_overdue
        FROM ranked
        ORDER BY overdue DESC,
                 CASE WHEN overdue=1 THEN days_overdue END DESC,
                 (checkpoint IS NULL),elapsed DESC,record_id
        LIMIT ?
        """,
        [now, *parameters, age_days, age_days, limit + 1],
    ).fetchall()
    items: list[Commitment] = []
    for row in rows[:limit]:
        payload = _payload(row["payload_json"])
        overdue = bool(row["overdue"])
        items.append(
            {
                "id": str(row["record_id"]),
                "title": payload.get("title"),
                "status": payload.get("status"),
                "source_tier": payload.get("source_tier"),
                "authority_state": payload.get("authority_state"),
                "owner_refs": payload.get("owner_refs", []),
                "owner_kind": _owner_kind(int(row["owners"]), int(row["agent_owners"])),
                "next_action": payload.get("next_action"),
                "checkpoint_at": payload.get("checkpoint_at"),
                "overdue": overdue,
                "days_overdue": int(row["days_overdue"]) if overdue else None,
            }
        )
    return {"items": items, "truncated": len(rows) > limit}


def _proposals(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    now: str,
    limit: int,
) -> ProposalSection:
    rows = connection.execute(
        f"""
        SELECT r.record_id,p.payload_json,CAST({_PROPOSAL_AGE} AS INTEGER) AS age_days
        FROM records r {_CURRENT_JOIN}
        WHERE ({predicate}) AND r.collection='decisions' AND {_VISIBLE} AND {_PROPOSED}
        ORDER BY age_days DESC,r.record_id
        LIMIT ?
        """,
        [now, *parameters, limit + 1],
    ).fetchall()
    items: list[Proposal] = []
    for row in rows[:limit]:
        payload = _payload(row["payload_json"])
        items.append(
            {
                "id": str(row["record_id"]),
                "title": payload.get("title"),
                "status": payload.get("status"),
                "requested_status": payload.get("requested_status"),
                "source_tier": payload.get("source_tier"),
                "authority_state": payload.get("authority_state"),
                "decided_at": payload.get("decided_at"),
                "age_days": int(row["age_days"]),
            }
        )
    return {"items": items, "truncated": len(rows) > limit}


def _subject_notes(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    fold_clause: str,
    fold_parameters: Sequence[object],
    subject_ids: Sequence[str],
    limit: int,
) -> SubjectNotesSection:
    if not subject_ids:
        return {"items": [], "truncated": False}
    placeholders = ",".join("?" for _ in subject_ids)
    rows = connection.execute(
        f"""
        SELECT subj.target_record_id AS subject_id,COUNT(DISTINCT r.record_id) AS unfolded
        FROM records r {_CURRENT_JOIN}
        JOIN revision_refs subj
          ON subj.record_id=r.record_id AND subj.revision=r.current_revision
         AND subj.ref_kind='entity'
        WHERE ({predicate}) AND r.collection='observations' AND {_VISIBLE}
          AND NOT {fold_clause} AND subj.target_record_id IN ({placeholders})
        GROUP BY subj.target_record_id
        ORDER BY unfolded DESC,subject_id
        LIMIT ?
        """,
        [*parameters, *fold_parameters, *subject_ids, limit + 1],
    ).fetchall()
    return {
        "items": [
            {"subject_id": str(row["subject_id"]), "unfolded": int(row["unfolded"])}
            for row in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }


def review_queue(
    connection: sqlite3.Connection,
    subscription_id: str,
    predicate: str,
    parameters: Sequence[object],
    *,
    fold_clause: str,
    fold_parameters: Sequence[object],
    subject_ids: Sequence[str],
    now: str,
    limit: int = 20,
    age_days: int = DEFAULT_AGE_DAYS,
) -> ReviewQueue:
    """List what waits on a person in one subscription.

    Commitments are unfinished loops, overdue first and then by nearest
    checkpoint.  ``owner_kind`` is ``none``, ``agent`` when every owner is an
    agent entity, or ``named`` otherwise.  Proposals are decisions without an
    authoritative verdict, oldest first.  Notes count unfolded observations for
    each subject the subscription names.  Each section holds at most ``limit``
    items and reports ``truncated``; ``summary`` carries the exact totals.

    Raises:
        ValueError: If ``limit`` or ``age_days`` is outside its supported range.
    """

    limit = _bounded(limit, MAX_QUEUE_ITEMS, "queue limit")
    age_days = _bounded(age_days, MAX_AGE_DAYS, "review age")
    return {
        "subscription_id": subscription_id,
        "summary": waiting_summary(
            connection,
            predicate,
            parameters,
            fold_clause=fold_clause,
            fold_parameters=fold_parameters,
            now=now,
            age_days=age_days,
        ),
        "commitments": _commitments(
            connection, predicate, parameters, now=now, age_days=age_days, limit=limit
        ),
        "proposals": _proposals(connection, predicate, parameters, now=now, limit=limit),
        "notes": _subject_notes(
            connection,
            predicate,
            parameters,
            fold_clause=fold_clause,
            fold_parameters=fold_parameters,
            subject_ids=subject_ids,
            limit=limit,
        ),
    }


def fold_candidates(
    connection: sqlite3.Connection,
    subscription_id: str,
    predicate: str,
    parameters: Sequence[object],
    *,
    fold_clause: str,
    fold_parameters: Sequence[object],
    subject_ids: Sequence[str],
    all_subjects: bool,
    subject_id: str | None = None,
    family: str | None = None,
    limit: int = 20,
) -> FoldCandidates:
    """List unfolded observations beside the claim slots that could absorb them.

    ``subject_id`` narrows both notes and slots to one entity; ``family``
    narrows the notes to one already-normalized family.  Notes are ordered by
    family, key, then ID so related ones are adjacent.  ``families`` and
    ``unfolded`` describe every unfolded note for the subject, before the
    family filter and the note limit.

    Raises:
        ValueError: If ``limit`` is not an integer from 1 to ``MAX_FOLD_ITEMS``.
    """

    limit = _bounded(limit, MAX_FOLD_ITEMS, "fold limit")
    where = f"({predicate}) AND r.collection='observations' AND {_VISIBLE} AND NOT {fold_clause}"
    base: list[object] = [*parameters, *fold_parameters]
    if subject_id is not None:
        where += (
            " AND EXISTS (SELECT 1 FROM revision_refs subj WHERE subj.record_id=r.record_id "
            "AND subj.revision=r.current_revision AND subj.ref_kind='entity' "
            "AND subj.target_record_id=?)"
        )
        base.append(subject_id)
    family_rows = connection.execute(
        f"""
        SELECT COALESCE(json_extract(p.payload_json,'$.family'),'') AS family,COUNT(*) AS notes
        FROM records r {_CURRENT_JOIN}
        WHERE {where}
        GROUP BY family ORDER BY notes DESC,family LIMIT ?
        """,
        [*base, MAX_FOLD_FAMILIES + 1],
    ).fetchall()
    total = connection.execute(
        f"SELECT COUNT(*) AS unfolded FROM records r {_CURRENT_JOIN} WHERE {where}", base
    ).fetchone()
    note_where = where
    note_parameters = list(base)
    if family is not None:
        note_where += " AND json_extract(p.payload_json,'$.family')=?"
        note_parameters.append(family)
    note_rows = connection.execute(
        f"""
        SELECT r.record_id,p.payload_json
        FROM records r {_CURRENT_JOIN}
        WHERE {note_where}
        ORDER BY json_extract(p.payload_json,'$.family'),
                 json_extract(p.payload_json,'$.key'),r.record_id
        LIMIT ?
        """,
        [*note_parameters, limit + 1],
    ).fetchall()
    notes: list[FoldNote] = []
    for row in note_rows[:limit]:
        payload = _payload(row["payload_json"])
        notes.append(
            {
                "id": str(row["record_id"]),
                "family": payload.get("family"),
                "key": payload.get("key"),
                "value": payload.get("value"),
                "observed_at": payload.get("observed_at"),
                "source_tier": payload.get("source_tier"),
                "entity_refs": payload.get("entity_refs", []),
            }
        )
    slots, slots_truncated = _current_claims(
        connection,
        predicate,
        parameters,
        subject_ids=subject_ids,
        all_subjects=all_subjects,
        only_subject=subject_id,
        limit=MAX_FOLD_ITEMS,
    )
    return {
        "subscription_id": subscription_id,
        "subject_id": subject_id,
        "family": family,
        "unfolded": int(total["unfolded"]),
        "families": {
            "items": [
                {"family": str(row["family"]), "notes": int(row["notes"])}
                for row in family_rows[:MAX_FOLD_FAMILIES]
            ],
            "truncated": len(family_rows) > MAX_FOLD_FAMILIES,
        },
        "notes": {"items": notes, "truncated": len(note_rows) > limit},
        "slots": {"items": slots, "truncated": slots_truncated},
    }


def scoped_records(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    record_ids: Sequence[str],
) -> dict[str, StoredRecord]:
    """Load the named decisions and loops that the subscription may read.

    A record that is missing, outside the scope, sensitive, redacted, or in
    another collection is simply absent from the result, so a caller cannot
    tell those cases apart.

    Raises:
        ValueError: If more than ``MAX_SCOPED_RECORDS`` IDs are requested.
    """

    if not record_ids:
        return {}
    if len(record_ids) > MAX_SCOPED_RECORDS:
        raise ValueError("too many records were requested")
    placeholders = ",".join("?" for _ in record_ids)
    rows = connection.execute(
        f"""
        SELECT r.record_id,r.collection,r.current_revision,p.payload_json
        FROM records r {_CURRENT_JOIN}
        WHERE ({predicate}) AND r.collection IN ('decisions','open_loops') AND {_VISIBLE}
          AND r.record_id IN ({placeholders})
        """,
        [*parameters, *record_ids],
    ).fetchall()
    return {
        str(row["record_id"]): {
            "collection": str(row["collection"]),
            "revision": int(row["current_revision"]),
            "payload": _payload(row["payload_json"]),
        }
        for row in rows
    }


__all__ = [
    "CURRENT_STATE_ITEMS",
    "CURRENT_STATE_LABEL",
    "DEFAULT_AGE_DAYS",
    "MAX_FOLD_ITEMS",
    "MAX_QUEUE_ITEMS",
    "MAX_SCOPED_RECORDS",
    "CurrentState",
    "FoldCandidates",
    "ReviewQueue",
    "WaitingSummary",
    "current_state",
    "fold_candidates",
    "review_queue",
    "scoped_records",
    "waiting_summary",
]
