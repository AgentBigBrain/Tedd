"""Decode reviewed queue answers and turn them into one capture packet.

The answers file is caller-supplied data.  ``decode_answers`` validates it once
at that boundary and returns frozen values; ``build_answers_packet`` is a pure
function over those values and the stored records the task may read.  Nothing
here touches storage, the clock, or the filesystem, and nothing is saved: the
packet still goes through the reviewed capture dry run and apply.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn, TypeAlias, TypedDict, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

ANSWERS_VERSION = "v1"
MAX_ANSWERS_BYTES = 256 * 1024
MAX_ANSWERS = 50
MAX_ANSWER_TEXT = 600
MAX_TITLE = 200
MAX_OWNERS = 8
MAX_EXCERPT = 1_200
ANSWER_SOURCE_TIER = "explicit_user_statement"
DEFAULT_TITLE = "Operator answers to the review queue"
WITHDRAWN_PREFIX = "Withdrawn: "

LOOP_ANSWERS = ("accept", "done", "drop", "hand_off", "later")
DECISION_ANSWERS = ("accept", "reject", "replace")
# Optional fields each answer may carry; a field outside its row is refused
# rather than silently ignored.
_ANSWER_FIELDS: Mapping[str, frozenset[str]] = {
    "accept": frozenset(),
    "done": frozenset({"outcome"}),
    "drop": frozenset({"reason"}),
    "later": frozenset({"checkpoint_at", "next_action"}),
    "hand_off": frozenset({"owner_refs", "checkpoint_at", "next_action"}),
    "reject": frozenset({"reason"}),
    "replace": frozenset({"superseded_by", "reason"}),
}
_REQUIRED_FIELD: Mapping[str, str] = {
    "done": "outcome",
    "drop": "reason",
    "later": "checkpoint_at",
    "hand_off": "owner_refs",
    "replace": "superseded_by",
}
_DOCUMENT_FIELDS = frozenset({"version", "observed_at", "title", "answers"})
_UNFINISHED = frozenset({"open", "candidate"})
_SETTLED_DECISIONS = frozenset({"rejected", "superseded"})
_PROPOSED = frozenset({"proposed", "candidate"})
# Answers that leave a proposal standing as a decision someone ruled on.
_SETTLING_ANSWERS = frozenset({"accept", "replace"})


class AnswersError(ValueError):
    """The answers document, or one answer against its stored record, is not acceptable."""


class StoredRecord(TypedDict):
    """A decision or loop the task may read, as loaded for answering."""

    collection: str
    revision: int
    payload: JsonObject


class AnswerSummary(TypedDict):
    id: str
    collection: str
    answer: str


class AnswersPacket(TypedDict):
    """A capture packet and a disclosure-safe list of what it settles."""

    packet: JsonObject
    answers: list[AnswerSummary]


@dataclass(frozen=True)
class Answer:
    """One reviewed answer.  Text is trimmed with inner whitespace collapsed."""

    record_id: str
    answer: str
    outcome: str = ""
    reason: str = ""
    checkpoint_at: str = ""
    next_action: str = ""
    owner_refs: tuple[str, ...] = ()
    superseded_by: str = ""


@dataclass(frozen=True)
class AnswersDocument:
    """A decoded answers file.  Times are UTC ISO 8601 text."""

    observed_at: str
    title: str
    answers: tuple[Answer, ...]

    def record_ids(self) -> tuple[str, ...]:
        """Every record the answers name, successors included, without repeats."""

        wanted: dict[str, None] = {}
        for answer in self.answers:
            wanted[answer.record_id] = None
            if answer.superseded_by:
                wanted[answer.superseded_by] = None
        return tuple(wanted)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AnswersError("answers repeat a JSON key")
        result[key] = value
    return result


def _reject_number(_raw: str) -> NoReturn:
    raise AnswersError("answers do not accept fractional or non-finite numbers")


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AnswersError(f"{label} must be text")
    text = " ".join(value.split())
    if len(text) > maximum:
        raise AnswersError(f"{label} exceeds {maximum} characters")
    return text


def _utc_time(value: object, label: str) -> str:
    """Accept only a timezone-aware ISO 8601 time and return it in UTC."""

    text = _text(value, label, 64)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AnswersError(f"{label} must be an ISO 8601 time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnswersError(f"{label} must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _owners(value: object, label: str, record_id: Callable[[object], str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OWNERS:
        raise AnswersError(f"{label} must list 1 to {MAX_OWNERS} entity IDs")
    owners = tuple(record_id(item) for item in cast(list[object], value))
    if len(set(owners)) != len(owners):
        raise AnswersError(f"{label} repeats an entity ID")
    return owners


def _decode_answer(raw: object, index: int, record_id: Callable[[object], str]) -> Answer:
    label = f"answers[{index}]"
    if not isinstance(raw, dict):
        raise AnswersError(f"{label} must be an object")
    entry = cast(dict[str, object], raw)
    answer = entry.get("answer")
    if not isinstance(answer, str) or answer not in _ANSWER_FIELDS:
        raise AnswersError(f"{label}.answer must be one of: " + ", ".join(sorted(_ANSWER_FIELDS)))
    extra = sorted(set(entry) - {"id", "answer"} - _ANSWER_FIELDS[answer])
    if extra:
        raise AnswersError(f"{label} does not use these fields with {answer}: " + ", ".join(extra))
    required = _REQUIRED_FIELD.get(answer)
    if required is not None and required not in entry:
        raise AnswersError(f"{label} requires {required} with {answer}")
    decoded = Answer(
        record_id=record_id(entry.get("id")),
        answer=answer,
        outcome=_text(entry.get("outcome", ""), f"{label}.outcome", MAX_ANSWER_TEXT),
        reason=_text(entry.get("reason", ""), f"{label}.reason", MAX_ANSWER_TEXT),
        checkpoint_at=(
            _utc_time(entry["checkpoint_at"], f"{label}.checkpoint_at")
            if "checkpoint_at" in entry
            else ""
        ),
        next_action=_text(entry.get("next_action", ""), f"{label}.next_action", MAX_ANSWER_TEXT),
        owner_refs=(
            _owners(entry["owner_refs"], f"{label}.owner_refs", record_id)
            if "owner_refs" in entry
            else ()
        ),
        superseded_by=record_id(entry["superseded_by"]) if "superseded_by" in entry else "",
    )
    if answer == "done" and not decoded.outcome:
        raise AnswersError(f"{label}.outcome must not be empty")
    if answer == "drop" and not decoded.reason:
        raise AnswersError(f"{label}.reason must not be empty")
    if decoded.superseded_by == decoded.record_id:
        raise AnswersError(f"{label} cannot replace a decision with itself")
    return decoded


def decode_answers(content: bytes, *, record_id: Callable[[object], str]) -> AnswersDocument:
    """Decode a caller-supplied answers file into validated, typed answers.

    The document is ``{"version": "v1", "observed_at": <time>, "title": <text>,
    "answers": [...]}``.  ``observed_at`` is when the person answered and must
    carry a UTC offset.  Each answer names one record ``id`` and one ``answer``
    with only the fields that answer uses.  Unknown fields, repeated keys or
    record IDs, missing required fields, naive times, and oversized text are
    refused; nothing is coerced.

    Args:
        content: The file's bytes, at most ``MAX_ANSWERS_BYTES``.
        record_id: Returns a validated record ID or raises ``ValueError``.

    Raises:
        AnswersError: If the document does not meet this contract.
    """

    if len(content) > MAX_ANSWERS_BYTES:
        raise AnswersError(f"answers exceed {MAX_ANSWERS_BYTES} bytes")
    try:
        document = cast(
            object,
            json.loads(
                content,
                object_pairs_hook=_unique_object,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            ),
        )
    except AnswersError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AnswersError("answers are not valid JSON") from error
    if not isinstance(document, dict):
        raise AnswersError("answers must be a JSON object")
    fields = cast(dict[str, object], document)
    unknown = sorted(set(fields) - _DOCUMENT_FIELDS)
    if unknown:
        raise AnswersError("answers have unknown fields: " + ", ".join(unknown))
    if fields.get("version") != ANSWERS_VERSION:
        raise AnswersError(f"answers version must be {ANSWERS_VERSION}")
    entries = fields.get("answers")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ANSWERS:
        raise AnswersError(f"answers must hold 1 to {MAX_ANSWERS} entries")
    try:
        answers = tuple(
            _decode_answer(raw, index, record_id)
            for index, raw in enumerate(cast(list[object], entries))
        )
    except AnswersError:
        raise
    except ValueError as error:
        raise AnswersError("answers name an invalid record ID") from error
    identifiers = [answer.record_id for answer in answers]
    if len(set(identifiers)) != len(identifiers):
        raise AnswersError("answers name a record more than once")
    return AnswersDocument(
        observed_at=_utc_time(fields.get("observed_at"), "observed_at"),
        title=_text(fields.get("title", ""), "title", MAX_TITLE) or DEFAULT_TITLE,
        answers=answers,
    )


def _require_newer_answer(
    record_id: str, payload: Mapping[str, JsonValue], answered_at: dt.datetime
) -> None:
    """Refuse an answer older than the record's latest observation.

    Capture leaves a record untouched when the incoming source predates what it
    already holds.  Refusing here turns that silent no-op into a clear error.
    """

    observed = payload.get("last_observed_at")
    if not isinstance(observed, str) or not observed:
        return
    try:
        latest = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        return
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=dt.timezone.utc)
    if answered_at < latest:
        raise AnswersError(f"{record_id}: the record changed after these answers were given")


def _identity(record_id: str, payload: Mapping[str, JsonValue], time_field: str) -> JsonObject:
    """Repeat the stored episode identity so capture updates the record in place."""

    title = payload.get("title")
    timestamp = payload.get(time_field)
    entities = payload.get("entity_refs")
    if (
        not isinstance(title, str)
        or not isinstance(timestamp, str)
        or not isinstance(entities, list)
    ):
        raise AnswersError(f"{record_id}: the stored record cannot be answered")
    return {"id": record_id, "title": title, time_field: timestamp, "entity_refs": list(entities)}


def _loop_record(answer: Answer, payload: Mapping[str, JsonValue]) -> JsonObject:
    if answer.answer not in LOOP_ANSWERS:
        raise AnswersError(f"{answer.record_id}: a commitment accepts " + ", ".join(LOOP_ANSWERS))
    status = payload.get("status")
    if status not in _UNFINISHED:
        raise AnswersError(f"{answer.record_id}: only an unfinished commitment can be answered")
    if answer.answer == "accept" and status != "candidate":
        raise AnswersError(f"{answer.record_id}: accept applies to a proposed commitment")
    record = _identity(answer.record_id, payload, "created_at")
    if answer.answer == "done":
        record.update({"status": "resolved", "resolution": answer.outcome})
    elif answer.answer == "drop":
        record.update({"status": "resolved", "resolution": WITHDRAWN_PREFIX + answer.reason})
    if answer.checkpoint_at:
        record["checkpoint_at"] = answer.checkpoint_at
    if answer.owner_refs:
        record["owner_refs"] = list(answer.owner_refs)
    if answer.next_action:
        record["next_action"] = answer.next_action
    return record


def _is_proposal(payload: Mapping[str, JsonValue]) -> bool:
    return payload.get("authority_state") == "candidate" or payload.get("status") in _PROPOSED


def _require_standing_successor(
    answer: Answer, successor: Mapping[str, JsonValue], rulings: Mapping[str, str]
) -> None:
    """Refuse to replace a decision with one that will not stand after this batch.

    ``rulings`` maps each answered record to its answer.  Capture applies the
    same rule to the finished packet; refusing here names the answer at fault
    before a packet is written.
    """

    ruling = rulings.get(answer.superseded_by)
    if ruling == "reject" or successor.get("status") == "rejected":
        raise AnswersError(f"{answer.record_id}: a rejected decision cannot replace another")
    if _is_proposal(successor) and ruling not in _SETTLING_ANSWERS:
        raise AnswersError(
            f"{answer.record_id}: accept {answer.superseded_by} first or in the same answers"
        )


def _decision_record(
    answer: Answer,
    payload: Mapping[str, JsonValue],
    records: Mapping[str, StoredRecord],
    rulings: Mapping[str, str],
) -> JsonObject:
    if answer.answer not in DECISION_ANSWERS:
        raise AnswersError(f"{answer.record_id}: a decision accepts " + ", ".join(DECISION_ANSWERS))
    status = payload.get("status")
    record = _identity(answer.record_id, payload, "decided_at")
    if answer.answer == "replace":
        successor = records.get(answer.superseded_by)
        if successor is None or successor["collection"] != "decisions":
            raise AnswersError(
                f"{answer.record_id}: replace requires another decision in this scope"
            )
        if status in _SETTLED_DECISIONS:
            raise AnswersError(f"{answer.record_id}: the decision is already settled")
        _require_standing_successor(answer, successor["payload"], rulings)
        record.update({"status": "superseded", "superseded_by": answer.superseded_by})
        return record
    if not _is_proposal(payload):
        raise AnswersError(f"{answer.record_id}: only a proposal can be accepted or rejected")
    record["status"] = "accepted" if answer.answer == "accept" else "rejected"
    return record


def build_answers_packet(
    document: AnswersDocument, records: Mapping[str, StoredRecord]
) -> AnswersPacket:
    """Build one capture packet that applies every answer, or refuse the batch.

    ``records`` holds the decisions and loops the task may read, keyed by ID.
    Each packet record repeats the stored title, identity time and entity list
    exactly, so capture revises that record instead of creating another.  The
    packet's source is the person's statement at ``document.observed_at``; it
    must be captured with the ``explicit_user_statement`` authority ceiling.

    A dropped commitment closes with its reason behind ``WITHDRAWN_PREFIX``.
    Rescheduling or handing off a proposed commitment accepts it as real.

    Raises:
        AnswersError: If an answered record is absent from ``records``, the
            answer does not fit that record's collection or current status, a
            replacement would not stand once the batch is applied, or the
            record was observed after ``document.observed_at``.
    """

    answered_at = dt.datetime.fromisoformat(document.observed_at)
    rulings = {answer.record_id: answer.answer for answer in document.answers}
    decisions: list[JsonValue] = []
    loops: list[JsonValue] = []
    summary: list[AnswerSummary] = []
    notes: list[str] = []
    for answer in document.answers:
        stored = records.get(answer.record_id)
        if stored is None:
            raise AnswersError(f"{answer.record_id}: not an answerable record in this scope")
        collection = stored["collection"]
        _require_newer_answer(answer.record_id, stored["payload"], answered_at)
        if collection == "open_loops":
            loops.append(_loop_record(answer, stored["payload"]))
        else:
            decisions.append(_decision_record(answer, stored["payload"], records, rulings))
        summary.append({"id": answer.record_id, "collection": collection, "answer": answer.answer})
        notes.append(
            f"{answer.record_id}: {answer.answer}"
            + (f" ({answer.reason})" if answer.reason else "")
        )
    packet: JsonObject = {
        "source": {
            "kind": "chat",
            "role": "user",
            "title": document.title,
            "observed_at": document.observed_at,
            "source_tier": ANSWER_SOURCE_TIER,
            "excerpt": "; ".join(notes)[:MAX_EXCERPT],
        }
    }
    if decisions:
        packet["decisions"] = decisions
    if loops:
        packet["open_loops"] = loops
    return {"packet": packet, "answers": summary}


__all__ = [
    "ANSWER_SOURCE_TIER",
    "DECISION_ANSWERS",
    "LOOP_ANSWERS",
    "MAX_ANSWERS",
    "MAX_ANSWERS_BYTES",
    "WITHDRAWN_PREFIX",
    "Answer",
    "AnswerSummary",
    "AnswersDocument",
    "AnswersError",
    "AnswersPacket",
    "StoredRecord",
    "build_answers_packet",
    "decode_answers",
]
