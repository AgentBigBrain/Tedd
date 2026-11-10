"""Frozen synthetic projects that are larger than one recall.

Every project is generated from fixed text tables and SHA-256 choices, so the
catalog is identical on every supported interpreter and platform.  Records are
authored oldest first; the sandbox writes one mutation per record, so position
in a project is its recency order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from tedd_memory_eval.catalog import NAMESPACE
from tedd_memory_eval.contracts import (
    ALLOWED_RECORD_PREFIX,
    PayloadAssertion,
    RecordFixture,
    Scenario,
)
from tedd_memory_eval.providers import canonical_payload_json, evaluation_token_count

from .contracts import (
    DEFAULT_BUDGET,
    STANDARD_BUDGET,
    FullContext,
    Placement,
    ProjectCase,
)

VALIDATED = "validated_structured_candidate"
INFERRED = "assistant_inference"
# One filler cycle: nine notes, five receipts, three decisions, two events, one loop.
FILLER_CYCLE = "NRNDNRNENRNDNRNLNRDE"
PROJECT_START = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
PROJECT_MINUTES = 120 * 24 * 60
# Characters a context response adds around each record payload, with margin.
ENVELOPE_CHARS_PER_RECORD = 260
ENVELOPE_CHARS_FIXED = 3_000
NEEDED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "decisions": ("summary", "status"),
    "observations": ("value",),
    "open_loops": ("next_action", "status", "checkpoint_at"),
}


@dataclass(frozen=True)
class _Topic:
    slug: str
    terms: tuple[str, ...]


TOPICS = (
    _Topic(
        "report_export",
        ("export job", "CSV writer", "column order", "header row", "date format", "download link"),
    ),
    _Topic(
        "sign_in",
        ("login form", "session length", "reset link", "lockout rule", "redirect target"),
    ),
    _Topic(
        "cache_refresh",
        ("cache entry", "refresh interval", "stale tile", "warm-up task", "eviction rule"),
    ),
    _Topic(
        "data_import",
        ("import job", "staging table", "row validator", "duplicate check", "batch size"),
    ),
    _Topic(
        "search_index",
        ("index build", "ranking rule", "synonym list", "query parser", "result page"),
    ),
    _Topic(
        "notification_digest",
        ("digest email", "send window", "unsubscribe link", "template block", "retry queue"),
    ),
    _Topic(
        "invoice_run",
        ("invoice total", "tax rule", "rounding step", "payment status", "credit note"),
    ),
    _Topic(
        "access_roles",
        ("role matrix", "admin group", "audit log", "invite link", "permission check"),
    ),
    _Topic(
        "page_layout",
        ("side panel", "grid column", "empty state", "print style", "contrast check"),
    ),
    _Topic(
        "release_process",
        ("release branch", "rollback step", "smoke test", "feature flag", "change window"),
    ),
    _Topic(
        "partner_api",
        ("rate limit", "request schema", "error code", "page cursor", "version header"),
    ),
    _Topic(
        "usage_metrics",
        ("event name", "sampling rate", "weekly chart", "cohort filter", "tracking plan"),
    ),
    _Topic(
        "language_files",
        ("string table", "plural rule", "fallback locale", "date pattern", "glossary entry"),
    ),
    _Topic(
        "page_speed",
        ("load time", "image size", "lazy load", "bundle split", "cache header"),
    ),
)
SENTENCES = (
    "The {a} now runs before the {b}, and the {c} was left as it was.",
    "Checked the {a} against the {b}; the {c} matched the expected output.",
    "The {a} failed once on the sample data because the {b} was out of date.",
    "After the change to the {a}, the {b} needed one more pass over the {c}.",
    "Compared two runs of the {a}; only the {b} differed, and the {c} explains why.",
    "The {a} is slower with the larger sample, so the {b} is measured separately.",
    "Nothing else in the {a} depends on the {b}, which keeps the {c} simple.",
    "The reviewer asked how the {a} treats the {b}; the answer is in the {c}.",
    "Kept the {a} unchanged while the {b} was rebuilt from the {c}.",
    "The {a} and the {b} disagree on one edge case that the {c} does not cover.",
)
TITLES = (
    "Checked the {a} against the {b}",
    "Rebuilt the {a} after the {b} change",
    "Measured the {a} with the {b}",
    "Reviewed the {a} and the {b}",
    "Compared the {a} with the {b}",
)
DECISION_TITLES = (
    "Keep the {a} separate from the {b}",
    "Run the {a} before the {b}",
    "Use the {a} for the {b}",
)
NOTE_FAMILIES = ("project_state", "finding", "environment")
EVENT_KINDS = ("milestone", "handoff", "review")


def _pick(seed: str, count: int) -> int:
    """Choose a stable index below ``count`` from text alone."""

    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big") % count


def _terms(seed: str, topic: _Topic) -> dict[str, str]:
    remaining = list(topic.terms)
    chosen: dict[str, str] = {}
    for slot in "abc":
        chosen[slot] = remaining.pop(_pick(f"{seed}:{slot}", len(remaining)))
    return chosen


def _prose(seed: str, topic: _Topic, length: int) -> str:
    sentences: list[str] = []
    used = 0
    while used < length:
        part = f"{seed}:{len(sentences)}"
        sentence = SENTENCES[_pick(part, len(SENTENCES))].format(**_terms(part, topic))
        sentences.append(sentence)
        used += len(sentence) + 1
    return " ".join(sentences)


def _phrase(seed: str, topic: _Topic, templates: Sequence[str]) -> str:
    return templates[_pick(f"{seed}:title", len(templates))].format(**_terms(seed, topic))


@dataclass(frozen=True)
class Placed:
    """One authored record and its position in a project, oldest first."""

    position: int
    fixture: RecordFixture

    @property
    def record_id(self) -> str:
        return self.fixture.record_id


@dataclass(frozen=True)
class SyntheticProject:
    """Identity, size, and timeline of one synthetic project.

    Builder methods author one production-shaped record at a position between
    zero (oldest) and ``size - 1`` (newest).  Dates follow position across a
    fixed 120-day span, so a record's dates agree with its recency.
    """

    case: str
    size: int

    @property
    def subject(self) -> str:
        return f"lab_subject_project_{self.case}"

    def position(self, percent: int) -> int:
        """Return the position that many percent of the way through the project."""

        return (self.size * percent) // 100

    def moment(self, position: int, *, plus_days: int = 0) -> str:
        minutes = (PROJECT_MINUTES * position) // self.size
        when = PROJECT_START + timedelta(minutes=minutes, days=plus_days)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _place(
        self,
        position: int,
        label: str,
        collection: str,
        fields: Mapping[str, object],
        *,
        text: Sequence[str],
        refs: tuple[tuple[str, str], ...] = (),
    ) -> Placed:
        record_id = f"lab_project_{self.case}_{position:04d}_{label}"
        payload: dict[str, object] = {
            "id": record_id,
            "namespace": NAMESPACE,
            "lifecycle": "active",
            "sensitive": False,
            **fields,
        }
        fixture = RecordFixture(
            record_id=record_id,
            collection=collection,
            payload=payload,
            search_text=" ".join(text),
            subject_id=self.subject,
            refs=refs,
        )
        return Placed(position, fixture)

    def note(self, position: int, label: str, *, family: str, key: str, value: str) -> Placed:
        when = self.moment(position)
        return self._place(
            position,
            label,
            "observations",
            {
                "family": family,
                "key": key,
                "value": value,
                "observed_at": when,
                "asserted_at": when,
                "source_tier": VALIDATED,
                "entity_refs": [self.subject],
            },
            text=(family.replace("_", " "), key.replace("_", " "), value),
        )

    def receipt(self, position: int, label: str, *, title: str, summary: str) -> Placed:
        return self._place(
            position,
            label,
            "receipts",
            {
                "kind": "verification",
                "title": title,
                "summary": summary,
                "verified_at": self.moment(position),
                "verified": True,
                "status": "recorded",
                "entity_refs": [self.subject],
            },
            text=(title, summary),
        )

    def event(self, position: int, label: str, *, kind: str, title: str, summary: str) -> Placed:
        return self._place(
            position,
            label,
            "events",
            {
                "kind": kind,
                "title": title,
                "summary": summary,
                "occurred_at": self.moment(position),
                "status": "recorded",
                "entity_refs": [self.subject],
            },
            text=(title, summary),
        )

    def decision(
        self,
        position: int,
        label: str,
        *,
        title: str,
        summary: str,
        rationale: str,
        status: str = "accepted",
        decided_position: int | None = None,
        superseded_by: str | None = None,
    ) -> Placed:
        """Author one decision as its current revision reads.

        A replaced decision keeps its original ``decided_at`` and sits at the
        position of the save that replaced it, as a real replacement revises it.
        """

        undecided = status == "proposed"
        fields: dict[str, object] = {
            "kind": "decision",
            "title": title,
            "summary": summary,
            "rationale": rationale,
            "decided_at": self.moment(position if decided_position is None else decided_position),
            "status": status,
            "authority_state": "candidate" if undecided else "accepted",
            "source_tier": INFERRED if undecided else VALIDATED,
            "entity_refs": [self.subject],
        }
        refs: tuple[tuple[str, str], ...] = ()
        if superseded_by is not None:
            fields["superseded_by"] = superseded_by
            refs = ((superseded_by, "superseded_by"),)
        return self._place(
            position, label, "decisions", fields, text=(title, summary, rationale), refs=refs
        )

    def loop(
        self,
        position: int,
        label: str,
        *,
        title: str,
        summary: str,
        next_action: str,
        resolved: bool,
    ) -> Placed:
        fields: dict[str, object] = {
            "kind": "open_loop",
            "title": title,
            "summary": summary,
            "created_at": self.moment(position),
            "status": "resolved" if resolved else "open",
            "priority": 2,
            "owner_refs": [self.subject],
            "entity_refs": [self.subject],
            "next_action": next_action,
            "checkpoint_at": self.moment(position, plus_days=7),
            "terminal_condition": "The next action is done and verified.",
            "source_tier": VALIDATED,
            "authority_state": "accepted",
        }
        if resolved:
            fields["resolved_at"] = self.moment(position)
            fields["resolution"] = "Done and verified in the same session."
        return self._place(
            position, label, "open_loops", fields, text=(title, summary, next_action)
        )


def _filler(project: SyntheticProject, position: int, decisions_before: int) -> Placed:
    """Return the routine record a project holds at one position."""

    seed = f"{project.case}:{position}"
    topic = TOPICS[_pick(f"{seed}:topic", len(TOPICS))]
    kind = FILLER_CYCLE[position % len(FILLER_CYCLE)]
    title = _phrase(seed, topic, TITLES)
    if kind == "N":
        return project.note(
            position,
            "note",
            family=NOTE_FAMILIES[_pick(f"{seed}:family", len(NOTE_FAMILIES))],
            key=f"{topic.slug}_{position:04d}",
            value=_prose(seed, topic, 520 + _pick(f"{seed}:length", 240)),
        )
    summary = _prose(seed, topic, 260 + _pick(f"{seed}:length", 180))
    if kind == "R":
        return project.receipt(position, "receipt", title=title, summary=summary)
    if kind == "E":
        return project.event(
            position,
            "event",
            kind=EVENT_KINDS[_pick(f"{seed}:kind", len(EVENT_KINDS))],
            title=title,
            summary=summary,
        )
    if kind == "L":
        return project.loop(
            position,
            "loop",
            title=title,
            summary=summary,
            next_action=_prose(f"{seed}:next", topic, 60),
            resolved=True,
        )
    return project.decision(
        position,
        "decision",
        title=_phrase(seed, topic, DECISION_TITLES),
        summary=summary,
        rationale=_prose(f"{seed}:why", topic, 90),
        status="proposed" if decisions_before % 4 == 3 else "accepted",
    )


def _records(project: SyntheticProject, planted: Sequence[Placed]) -> tuple[RecordFixture, ...]:
    """Fill every position, using a planted record wherever one claims the position."""

    by_position: dict[int, RecordFixture] = {}
    for placed in planted:
        if placed.position in by_position or not 0 <= placed.position < project.size:
            raise ValueError(f"Project {project.case} plants an invalid position")
        if placed.fixture.subject_id != project.subject:
            raise ValueError(f"Project {project.case} plants a record from another project")
        by_position[placed.position] = placed.fixture
    records: list[RecordFixture] = []
    decisions = 0
    for position in range(project.size):
        fixture = by_position.get(position) or _filler(project, position, decisions).fixture
        if fixture.collection == "decisions":
            decisions += 1
        records.append(fixture)
    return tuple(records)


def _needed(required: Sequence[Placed]) -> tuple[PayloadAssertion, ...]:
    """Assert the fields an agent needs from each required record."""

    return tuple(
        PayloadAssertion(placed.record_id, field, placed.fixture.payload[field])
        for placed in required
        for field in NEEDED_FIELDS[placed.fixture.collection]
    )


def project_case(
    project: SyntheticProject,
    *,
    query: str,
    placement: Placement,
    required: Sequence[Placed],
    family: str,
    other_planted: Sequence[Placed] = (),
    stale: Sequence[Placed] = (),
    current_before_stale: tuple[tuple[str, str], ...] = (),
    partition: str = "calibration",
    critical: bool = False,
) -> ProjectCase:
    """Build one project around the records a question needs.

    ``required``, ``stale`` and ``other_planted`` must be authored by
    ``project``.  Every other position holds a generated routine record, in a
    fixed mix of notes, receipts, decisions, events and resolved loops.  The
    exact-ID arm fetches exactly the required records.

    Raises:
        ValueError: If two planted records share a position, or one is outside
            the project or belongs to another project.
    """

    required_ids = tuple(placed.record_id for placed in required)
    return ProjectCase(
        scenario=Scenario(
            case_id=project.case,
            family=family,
            query=query,
            subject_id=project.subject,
            records=_records(project, (*required, *stale, *other_planted)),
            required_ids=required_ids,
            relevant_ids=required_ids,
            oracle_ids=required_ids,
            required_assertions=_needed(required),
            partition=partition,
            stale_ids=tuple(placed.record_id for placed in stale),
            current_before_stale=current_before_stale,
            critical=critical,
        ),
        placement=placement,
    )


def _old_decision() -> ProjectCase:
    project = SyntheticProject("old_decision", 300)
    decision = project.decision(
        project.position(8),
        "format",
        title="Exported reports use CSV",
        summary=(
            "Exported reports are written as CSV in UTF-8 with a header row. The spreadsheet "
            "format was declined because the partner's loader reads plain text only."
        ),
        rationale="The partner confirmed that their loader cannot open spreadsheet files.",
    )
    return project_case(
        project,
        query="Which file format did we agree on for exported reports?",
        placement="outside_recall",
        required=(decision,),
        family="standing_decision",
        critical=True,
    )


def _decisions_over_time() -> ProjectCase:
    project = SyntheticProject("decisions_over_time", 650)
    sessions = project.decision(
        project.position(6),
        "sessions",
        title="Sessions end after twelve hours",
        summary=(
            "A signed-in session ends after twelve hours without activity. Longer sessions "
            "were declined after the access review."
        ),
        rationale="The access review found shared machines left signed in overnight.",
    )
    resets = project.decision(
        project.position(34),
        "resets",
        title="Reset links work once",
        summary="A reset link works once and stops working after thirty minutes.",
        rationale="A forwarded message must not let a second person change the account.",
    )
    rollout = project.decision(
        project.position(63),
        "rollout",
        title="Sign-in changes roll out in stages",
        summary=(
            "Any change to the sign-in flow goes to one tenth of accounts for two days "
            "before everyone gets it."
        ),
        rationale="An earlier change locked out every account at once.",
    )
    return project_case(
        project,
        query="What rules must a change to the sign-in flow respect?",
        placement="outside_recall",
        required=(sessions, resets, rollout),
        family="standing_decision",
        partition="confirmation",
    )


def _replaced_decision() -> ProjectCase:
    project = SyntheticProject("replaced_decision", 300)
    replaced_at = project.position(42)
    current = project.decision(
        replaced_at,
        "current",
        title="The cache refreshes every fifteen minutes",
        summary=(
            "The cache refreshes every fifteen minutes. This replaces the hourly refresh, "
            "which left dashboard tiles out of date."
        ),
        rationale="Two reports of stale dashboard tiles traced back to the hourly refresh.",
    )
    replaced = project.decision(
        replaced_at + 1,
        "replaced",
        title="The cache refreshes hourly",
        summary="The cache refreshes every sixty minutes.",
        rationale="An hourly refresh kept the load on the reporting database low.",
        status="superseded",
        decided_position=project.position(10),
        superseded_by=current.record_id,
    )
    return project_case(
        project,
        query="How often does the cache refresh now?",
        placement="outside_recall",
        required=(current,),
        family="replaced_decision",
        stale=(replaced,),
        current_before_stale=((current.record_id, replaced.record_id),),
    )


def _open_commitment() -> ProjectCase:
    project = SyntheticProject("open_commitment", 250)
    promised = project.loop(
        project.position(22),
        "promised",
        title="Send the reviewer the migration summary",
        summary="The reviewer asked for a written summary of the data migration before sign-off.",
        next_action="Write the migration summary and send it to the reviewer.",
        resolved=False,
    )
    others = (
        project.loop(
            project.position(50),
            "designer",
            title="Confirm the print style with the designer",
            summary="The print style was changed without the designer seeing the result.",
            next_action="Share two printed pages with the designer and record the answer.",
            resolved=False,
        ),
        project.loop(
            project.position(80),
            "partner",
            title="Ask the partner for the new rate limit",
            summary="The partner announced a new rate limit without giving the number.",
            next_action="Ask the partner contact for the new rate limit in writing.",
            resolved=False,
        ),
    )
    return project_case(
        project,
        query="What do we still owe the reviewer, and by when?",
        placement="outside_recall",
        required=(promised,),
        family="open_commitment",
        other_planted=others,
        critical=True,
    )


def _old_note() -> ProjectCase:
    project = SyntheticProject("old_note", 250)
    note = project.note(
        project.position(15),
        "staging",
        family="environment",
        key="import_staging_table",
        value=(
            "The import job writes to the staging table named intake_rows_v2. The older "
            "intake_rows table is read-only and kept for the audit."
        ),
    )
    return project_case(
        project,
        query="Which staging table does the import job write to?",
        placement="outside_recall",
        required=(note,),
        family="unfolded_note",
        partition="confirmation",
    )


def _recent_work() -> ProjectCase:
    project = SyntheticProject("recent_work", 300)
    stopped = project.note(
        project.size - 4,
        "stopped",
        family="project_state",
        key="last_session_stop",
        value=(
            "Stopped after the row validator passed on the sample file. The duplicate "
            "check is the next step."
        ),
    )
    decided = project.decision(
        project.size - 2,
        "batches",
        title="Import batches hold five hundred rows",
        summary="Each import batch holds five hundred rows so one bad file cannot block the queue.",
        rationale="A single oversized file stalled the queue for an afternoon.",
    )
    return project_case(
        project,
        query="Where did the last session stop, and what was decided?",
        placement="recent_work",
        required=(stopped, decided),
        family="resumption",
    )


def _small_project() -> ProjectCase:
    project = SyntheticProject("small_project", 30)
    decision = project.decision(
        project.position(10),
        "window",
        title="The digest goes out at seven in the morning",
        summary="The digest email is sent at seven in the morning in the reader's own time zone.",
        rationale="Evening sends were opened half as often in the trial.",
    )
    return project_case(
        project,
        query="When is the digest email sent?",
        placement="fits_one_recall",
        required=(decision,),
        family="standing_decision",
        partition="confirmation",
    )


def project_cases() -> tuple[ProjectCase, ...]:
    """Return the frozen project recall catalog in seed order."""

    cases = (
        _old_decision(),
        _decisions_over_time(),
        _replaced_decision(),
        _open_commitment(),
        _old_note(),
        _recent_work(),
        _small_project(),
    )
    validate_cases(cases)
    return cases


def _through_newest(scenario: Scenario, record_id: str) -> tuple[int, int]:
    """Count the records at least as recent as one record, and their payload size."""

    index = next(i for i, item in enumerate(scenario.records) if item.record_id == record_id)
    newer = scenario.records[index:]
    return len(newer), sum(len(canonical_payload_json(item.payload)) for item in newer)


def _placement_holds(case: ProjectCase) -> bool:
    """Check that required records sit where the case's placement says they do."""

    reach = [_through_newest(case.scenario, item) for item in case.scenario.required_ids]
    if case.placement == "outside_recall":
        return all(
            count > DEFAULT_BUDGET.max_items or chars > DEFAULT_BUDGET.max_chars
            for count, chars in reach
        )
    if case.placement == "recent_work":
        return all(
            count <= STANDARD_BUDGET.max_items // 2 and chars <= STANDARD_BUDGET.max_chars // 2
            for count, chars in reach
        )
    reference = full_context(case)
    whole = (
        reference.payload_chars
        + ENVELOPE_CHARS_PER_RECORD * reference.record_count
        + ENVELOPE_CHARS_FIXED
    )
    return (
        reference.record_count < DEFAULT_BUDGET.max_items
        and whole <= DEFAULT_BUDGET.max_chars
        and all(count > STANDARD_BUDGET.max_items for count, _chars in reach)
    )


def validate_cases(cases: Sequence[ProjectCase]) -> None:
    """Reject ambiguous judgments and projects outside their declared size regime.

    Raises:
        ValueError: If IDs collide or leave the synthetic namespace, a label
            names an unknown record, an assertion disagrees with its fixture,
            or required records do not sit where the case's placement says.
    """

    seen: set[str] = set()
    for case in cases:
        scenario = case.scenario
        fixtures = {fixture.record_id: fixture for fixture in scenario.records}
        identifiers = [scenario.case_id, scenario.subject_id, *fixtures]
        if len(fixtures) != len(scenario.records) or seen & set(identifiers):
            raise ValueError(f"Project case {case.case_id} repeats an identifier")
        seen.update(identifiers)
        if any(not item.startswith(ALLOWED_RECORD_PREFIX) for item in identifiers[1:]):
            raise ValueError(f"Project case {case.case_id} leaves the synthetic namespace")
        if any(fixture.subject_id != scenario.subject_id for fixture in scenario.records):
            raise ValueError(f"Project case {case.case_id} mixes subjects")
        required = set(scenario.required_ids)
        stale = set(scenario.stale_ids)
        if not scenario.query.strip() or not required:
            raise ValueError(f"Project case {case.case_id} requires a query and judged records")
        if scenario.oracle_ids != scenario.required_ids or not required <= set(
            scenario.relevant_ids
        ):
            raise ValueError(f"Project case {case.case_id} has incomplete labels")
        if not (set(scenario.relevant_ids) | stale) <= set(fixtures) or required & stale:
            raise ValueError(f"Project case {case.case_id} labels an unknown or stale record")
        if any(
            current not in required or old not in stale
            for current, old in scenario.current_before_stale
        ):
            raise ValueError(f"Project case {case.case_id} has an invalid replacement pair")
        if scenario.partition not in {"calibration", "confirmation"}:
            raise ValueError(f"Project case {case.case_id} has an unknown partition")
        asserted = {assertion.record_id for assertion in scenario.required_assertions}
        if asserted != required or any(
            fixtures[assertion.record_id].payload.get(assertion.field) != assertion.expected
            for assertion in scenario.required_assertions
        ):
            raise ValueError(f"Project case {case.case_id} has invalid payload assertions")
        if not _placement_holds(case):
            raise ValueError(f"Project case {case.case_id} does not match its placement")


def full_context(case: ProjectCase) -> FullContext:
    """Measure handing over every authored record of one project.

    The size is computed from the authored payloads with the lab's canonical
    serialization.  The run checks that every payload a read returns matches
    its authored payload, so this equals what full review-safe reads would
    return for these non-sensitive records.  The project's own entity record
    is not counted.
    """

    payloads = tuple(canonical_payload_json(item.payload) for item in case.scenario.records)
    return FullContext(
        case_id=case.case_id,
        placement=case.placement,
        record_count=len(payloads),
        payload_chars=sum(len(payload) for payload in payloads),
        evaluation_tokens=evaluation_token_count(payloads),
        stale_records=len(case.scenario.stale_ids),
    )


def cases_digest(cases: Sequence[ProjectCase]) -> str:
    """Hash every semantic catalog field so the catalog cannot drift silently."""

    encoded = json.dumps(
        [asdict(case) for case in cases],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "Placed",
    "SyntheticProject",
    "cases_digest",
    "full_context",
    "project_case",
    "project_cases",
    "validate_cases",
]
