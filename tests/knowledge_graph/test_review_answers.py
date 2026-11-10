"""Reviewed queue answers: decoding the file, building the packet, and saving it."""

from __future__ import annotations

import json
import re
import unittest
from typing import Any

import knowledge_graph as graph
from tedd_knowledge_graph.v4_review_answers import (
    MAX_ANSWERS,
    MAX_ANSWERS_BYTES,
    Answer,
    AnswersDocument,
    AnswersError,
    StoredRecord,
    build_answers_packet,
    decode_answers,
)

from tests.knowledge_graph.outcome_support import (
    INFERENCE,
    NOW,
    SCOPE,
    USER,
    OutcomeFixture,
)

ANSWERED_AT = "2026-02-10T09:00:00-05:00"
ANSWERED_AT_UTC = "2026-02-10T14:00:00+00:00"
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def record_id(value: object) -> str:
    """Stand in for the command boundary's record-ID validator."""

    if not isinstance(value, str) or not _RECORD_ID.fullmatch(value):
        raise ValueError("invalid record ID")
    return value


def document(*answers: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"version": "v1", "observed_at": ANSWERED_AT, "answers": list(answers), **fields}


def decode(value: object) -> AnswersDocument:
    return decode_answers(json.dumps(value).encode(), record_id=record_id)


def stored(collection: str, identifier: str, **payload: Any) -> StoredRecord:
    time_field = "created_at" if collection == "open_loops" else "decided_at"
    return {
        "collection": collection,
        "revision": 1,
        "payload": {
            "id": identifier,
            "title": f"Title of {identifier}",
            time_field: "2026-01-03T12:00:00+00:00",
            "entity_refs": ["entity_project"],
            **payload,
        },
    }


def answers(*items: Answer) -> AnswersDocument:
    return AnswersDocument(observed_at=ANSWERED_AT_UTC, title="Synthetic answers", answers=items)


class DecodeAnswersTests(unittest.TestCase):
    def test_a_valid_file_becomes_typed_answers_with_utc_times(self) -> None:
        decoded = decode(
            document(
                {"id": "loop_report", "answer": "done", "outcome": "  Report   sent  "},
                {
                    "id": "loop_review",
                    "answer": "later",
                    "checkpoint_at": "2026-02-14T09:00:00-05:00",
                },
                {"id": "decision_old", "answer": "replace", "superseded_by": "decision_new"},
            )
        )

        self.assertEqual(decoded.observed_at, ANSWERED_AT_UTC)
        self.assertEqual(decoded.title, "Operator answers to the review queue")
        self.assertEqual(decoded.answers[0], Answer("loop_report", "done", outcome="Report sent"))
        self.assertEqual(decoded.answers[1].checkpoint_at, "2026-02-14T14:00:00+00:00")
        self.assertEqual(
            decoded.record_ids(), ("loop_report", "loop_review", "decision_old", "decision_new")
        )

    def test_the_largest_allowed_batch_is_accepted(self) -> None:
        batch = [{"id": f"loop_{index:02}", "answer": "accept"} for index in range(MAX_ANSWERS)]

        self.assertEqual(len(decode(document(*batch)).answers), MAX_ANSWERS)

    def test_a_malformed_document_is_refused(self) -> None:
        accept = {"id": "loop_report", "answer": "accept"}
        cases: dict[str, tuple[object, str]] = {
            "not an object": ([accept], "must be a JSON object"),
            "unknown field": (document(accept, note="extra"), "unknown fields: note"),
            "wrong version": ({**document(accept), "version": "v2"}, "version must be v1"),
            "missing time": (
                {"version": "v1", "answers": [accept]},
                "observed_at must be text",
            ),
            "naive time": (
                {**document(accept), "observed_at": "2026-02-10T09:00:00"},
                "observed_at must include a UTC offset",
            ),
            "date only": (
                {**document(accept), "observed_at": "2026-02-10"},
                "observed_at must include a UTC offset",
            ),
            "not a time": (
                {**document(accept), "observed_at": "next Tuesday"},
                "observed_at must be an ISO 8601 time",
            ),
            "no answers": (document(), "1 to 50 entries"),
            "too many answers": (
                document(*[{"id": f"loop_{i}", "answer": "accept"} for i in range(51)]),
                "1 to 50 entries",
            ),
            "long title": (document(accept, title="t" * 201), "title exceeds 200"),
        }
        for label, (value, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(AnswersError, message):
                decode(value)

    def test_a_malformed_answer_is_refused(self) -> None:
        cases: dict[str, tuple[object, str]] = {
            "not an object": ("loop_report", r"answers\[0\] must be an object"),
            "unknown answer": ({"id": "loop_report", "answer": "maybe"}, "answer must be one of"),
            "answer not text": ({"id": "loop_report", "answer": True}, "answer must be one of"),
            "field from another answer": (
                {"id": "loop_report", "answer": "drop", "reason": "No", "outcome": "Sent"},
                "does not use these fields with drop: outcome",
            ),
            "unknown field": (
                {"id": "loop_report", "answer": "accept", "note": "extra"},
                "does not use these fields with accept: note",
            ),
            "missing outcome": ({"id": "loop_report", "answer": "done"}, "requires outcome"),
            "blank outcome": (
                {"id": "loop_report", "answer": "done", "outcome": "   "},
                "outcome must not be empty",
            ),
            "blank reason": (
                {"id": "loop_report", "answer": "drop", "reason": ""},
                "reason must not be empty",
            ),
            "outcome not text": (
                {"id": "loop_report", "answer": "done", "outcome": 7},
                "outcome must be text",
            ),
            "long outcome": (
                {"id": "loop_report", "answer": "done", "outcome": "x" * 601},
                "outcome exceeds 600",
            ),
            "missing checkpoint": ({"id": "loop_report", "answer": "later"}, "requires checkpoint"),
            "naive checkpoint": (
                {"id": "loop_report", "answer": "later", "checkpoint_at": "2026-02-14T09:00:00"},
                "checkpoint_at must include a UTC offset",
            ),
            "missing owners": ({"id": "loop_report", "answer": "hand_off"}, "requires owner_refs"),
            "owners not a list": (
                {"id": "loop_report", "answer": "hand_off", "owner_refs": "entity_person"},
                "owner_refs must list 1 to 8",
            ),
            "no owners": (
                {"id": "loop_report", "answer": "hand_off", "owner_refs": []},
                "owner_refs must list 1 to 8",
            ),
            "repeated owner": (
                {"id": "loop_report", "answer": "hand_off", "owner_refs": ["entity_a", "entity_a"]},
                "owner_refs repeats",
            ),
            "invalid owner": (
                {"id": "loop_report", "answer": "hand_off", "owner_refs": ["not an id"]},
                "invalid record ID",
            ),
            "invalid id": ({"id": "not an id", "answer": "accept"}, "invalid record ID"),
            "missing id": ({"answer": "accept"}, "invalid record ID"),
            "missing successor": (
                {"id": "decision_old", "answer": "replace"},
                "requires superseded_by",
            ),
            "replaces itself": (
                {"id": "decision_old", "answer": "replace", "superseded_by": "decision_old"},
                "cannot replace a decision with itself",
            ),
        }
        for label, (entry, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(AnswersError, message):
                decode(document(entry))

    def test_one_record_cannot_be_answered_twice(self) -> None:
        twice = document(
            {"id": "loop_report", "answer": "accept"},
            {"id": "loop_report", "answer": "done", "outcome": "Sent"},
        )

        with self.assertRaisesRegex(AnswersError, "more than once"):
            decode(twice)

    def test_malformed_bytes_are_refused_before_any_field_is_read(self) -> None:
        valid = json.dumps(document({"id": "loop_report", "answer": "accept"}))
        cases: dict[str, tuple[bytes, str]] = {
            "not JSON": (b"{not json", "not valid JSON"),
            "not UTF-8": (b"\xff\xfe", "not valid JSON"),
            "repeated key": (
                b'{"version": "v1", "version": "v1", "observed_at": "x", "answers": []}',
                "repeat a JSON key",
            ),
            "fraction": (valid.replace('"v1"', "1.5").encode(), "fractional or non-finite"),
            "not a number": (valid.replace('"v1"', "NaN").encode(), "fractional or non-finite"),
            "too large": (b" " * (MAX_ANSWERS_BYTES + 1), "exceed"),
            "nested too deeply": (b"[" * 100_000, "not valid JSON"),
        }
        for label, (content, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(AnswersError, message):
                decode_answers(content, record_id=record_id)


class BuildAnswersPacketTests(unittest.TestCase):
    def test_done_closes_the_commitment_with_its_outcome_and_exact_identity(self) -> None:
        records = {"loop_report": stored("open_loops", "loop_report", status="open")}

        built = build_answers_packet(
            answers(Answer("loop_report", "done", outcome="Report sent")), records
        )

        self.assertEqual(
            built["packet"]["open_loops"],
            [
                {
                    "id": "loop_report",
                    "title": "Title of loop_report",
                    "created_at": "2026-01-03T12:00:00+00:00",
                    "entity_refs": ["entity_project"],
                    "status": "resolved",
                    "resolution": "Report sent",
                }
            ],
        )
        self.assertNotIn("decisions", built["packet"])
        self.assertEqual(
            built["answers"], [{"id": "loop_report", "collection": "open_loops", "answer": "done"}]
        )

    def test_the_packet_source_is_the_persons_statement_at_the_answer_time(self) -> None:
        records = {"loop_report": stored("open_loops", "loop_report", status="open")}

        source = build_answers_packet(
            answers(Answer("loop_report", "drop", reason="No longer needed")), records
        )["packet"]["source"]

        self.assertEqual(
            source,
            {
                "kind": "chat",
                "role": "user",
                "title": "Synthetic answers",
                "observed_at": ANSWERED_AT_UTC,
                "source_tier": "explicit_user_statement",
                "excerpt": "loop_report: drop (No longer needed)",
            },
        )

    def test_each_commitment_answer_changes_only_its_own_fields(self) -> None:
        records = {
            "loop_open": stored("open_loops", "loop_open", status="open"),
            "loop_idea": stored("open_loops", "loop_idea", status="candidate"),
        }
        cases: dict[str, tuple[Answer, dict[str, Any]]] = {
            "drop": (
                Answer("loop_open", "drop", reason="No longer needed"),
                {"status": "resolved", "resolution": "Withdrawn: No longer needed"},
            ),
            "later": (
                Answer("loop_open", "later", checkpoint_at="2026-02-14T14:00:00+00:00"),
                {"checkpoint_at": "2026-02-14T14:00:00+00:00"},
            ),
            "later with action": (
                Answer(
                    "loop_open",
                    "later",
                    checkpoint_at="2026-02-14T14:00:00+00:00",
                    next_action="Ask again",
                ),
                {"checkpoint_at": "2026-02-14T14:00:00+00:00", "next_action": "Ask again"},
            ),
            "hand off": (
                Answer("loop_open", "hand_off", owner_refs=("entity_person",)),
                {"owner_refs": ["entity_person"]},
            ),
            "accept a proposal": (Answer("loop_idea", "accept"), {}),
        }
        for label, (answer, changed) in cases.items():
            with self.subTest(label):
                record = build_answers_packet(answers(answer), records)["packet"]["open_loops"][0]

                extra = {
                    key: value
                    for key, value in record.items()
                    if key not in {"id", "title", "created_at", "entity_refs"}
                }
                self.assertEqual(extra, changed)

    def test_each_decision_answer_sets_the_matching_status(self) -> None:
        records = {
            "decision_idea": stored(
                "decisions", "decision_idea", status="proposed", authority_state="candidate"
            ),
            "decision_old": stored(
                "decisions", "decision_old", status="accepted", authority_state="accepted"
            ),
            "decision_new": stored(
                "decisions", "decision_new", status="accepted", authority_state="accepted"
            ),
        }
        cases: dict[str, tuple[Answer, dict[str, Any]]] = {
            "accept": (Answer("decision_idea", "accept"), {"status": "accepted"}),
            "reject": (
                Answer("decision_idea", "reject", reason="Too costly"),
                {"status": "rejected"},
            ),
            "replace an accepted decision": (
                Answer("decision_old", "replace", superseded_by="decision_new"),
                {"status": "superseded", "superseded_by": "decision_new"},
            ),
            "replace a proposal": (
                Answer("decision_idea", "replace", superseded_by="decision_new"),
                {"status": "superseded", "superseded_by": "decision_new"},
            ),
        }
        for label, (answer, changed) in cases.items():
            with self.subTest(label):
                record = build_answers_packet(answers(answer), records)["packet"]["decisions"][0]

                extra = {
                    key: value
                    for key, value in record.items()
                    if key not in {"id", "title", "decided_at", "entity_refs"}
                }
                self.assertEqual(extra, changed)

    def test_an_answer_that_does_not_fit_its_record_refuses_the_whole_batch(self) -> None:
        records = {
            "loop_valid": stored("open_loops", "loop_valid", status="open"),
            "loop_open": stored("open_loops", "loop_open", status="open"),
            "loop_closed": stored("open_loops", "loop_closed", status="resolved"),
            "decision_idea": stored(
                "decisions", "decision_idea", status="proposed", authority_state="candidate"
            ),
            "decision_old": stored(
                "decisions", "decision_old", status="accepted", authority_state="accepted"
            ),
            "decision_gone": stored(
                "decisions", "decision_gone", status="rejected", authority_state="accepted"
            ),
        }
        cases: dict[str, tuple[Answer, str]] = {
            "absent record": (Answer("loop_missing", "accept"), "not an answerable record"),
            "decision answer on a commitment": (
                Answer("loop_open", "reject"),
                "a commitment accepts",
            ),
            "commitment answer on a decision": (
                Answer("decision_idea", "done", outcome="Sent"),
                "a decision accepts",
            ),
            "finished commitment": (
                Answer("loop_closed", "done", outcome="Sent"),
                "only an unfinished commitment",
            ),
            "accept a commitment that is already open": (
                Answer("loop_open", "accept"),
                "accept applies to a proposed commitment",
            ),
            "accept an accepted decision": (
                Answer("decision_old", "accept"),
                "only a proposal can be accepted or rejected",
            ),
            "replace with a missing decision": (
                Answer("decision_old", "replace", superseded_by="decision_absent"),
                "replace requires another decision in this scope",
            ),
            "replace with a commitment": (
                Answer("decision_old", "replace", superseded_by="loop_open"),
                "replace requires another decision in this scope",
            ),
            "replace a settled decision": (
                Answer("decision_gone", "replace", superseded_by="decision_old"),
                "already settled",
            ),
            "replace with an undecided proposal": (
                Answer("decision_old", "replace", superseded_by="decision_idea"),
                "accept decision_idea first or in the same answers",
            ),
            "replace with a rejected decision": (
                Answer("decision_old", "replace", superseded_by="decision_gone"),
                "a rejected decision cannot replace another",
            ),
        }
        valid = Answer("loop_valid", "later", checkpoint_at=ANSWERED_AT_UTC)
        for label, (answer, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(AnswersError, message):
                build_answers_packet(answers(valid, answer), records)

    @staticmethod
    def proposal_and_decision() -> dict[str, StoredRecord]:
        return {
            "decision_idea": stored(
                "decisions", "decision_idea", status="proposed", authority_state="candidate"
            ),
            "decision_old": stored(
                "decisions", "decision_old", status="accepted", authority_state="accepted"
            ),
        }

    def test_a_proposal_accepted_in_the_same_answers_can_be_the_successor(self) -> None:
        replace = Answer("decision_old", "replace", superseded_by="decision_idea")
        accept = Answer("decision_idea", "accept")
        for label, batch in (
            ("accept listed first", answers(accept, replace)),
            ("replace listed first", answers(replace, accept)),
        ):
            with self.subTest(label):
                built = build_answers_packet(batch, self.proposal_and_decision())

                statuses = {item["id"]: item["status"] for item in built["packet"]["decisions"]}
                self.assertEqual(
                    statuses, {"decision_idea": "accepted", "decision_old": "superseded"}
                )

    def test_a_proposal_rejected_in_the_same_answers_cannot_be_the_successor(self) -> None:
        batch = answers(
            Answer("decision_idea", "reject"),
            Answer("decision_old", "replace", superseded_by="decision_idea"),
        )

        with self.assertRaisesRegex(AnswersError, "a rejected decision cannot replace another"):
            build_answers_packet(batch, self.proposal_and_decision())

    @staticmethod
    def observed_loop(observed: str) -> dict[str, StoredRecord]:
        return {
            "loop_open": stored("open_loops", "loop_open", status="open", last_observed_at=observed)
        }

    def test_an_answer_older_than_the_records_latest_observation_is_refused(self) -> None:
        batch = answers(Answer("loop_open", "later", checkpoint_at=ANSWERED_AT_UTC))

        with self.assertRaisesRegex(AnswersError, "changed after these answers"):
            build_answers_packet(batch, self.observed_loop("2026-02-10T14:00:01+00:00"))

    def test_an_answer_at_or_after_the_latest_observation_is_accepted(self) -> None:
        batch = answers(Answer("loop_open", "later", checkpoint_at=ANSWERED_AT_UTC))
        for label, observed in (
            ("the same instant", ANSWERED_AT_UTC),
            ("a day earlier", "2026-02-09T14:00:00Z"),
        ):
            with self.subTest(label):
                built = build_answers_packet(batch, self.observed_loop(observed))

                self.assertEqual(len(built["packet"]["open_loops"]), 1)

    def test_the_source_excerpt_stays_within_its_bound(self) -> None:
        records = {
            f"loop_{index:02}": stored("open_loops", f"loop_{index:02}", status="open")
            for index in range(MAX_ANSWERS)
        }
        batch = answers(*(Answer(identifier, "drop", reason="r" * 600) for identifier in records))

        excerpt = build_answers_packet(batch, records)["packet"]["source"]["excerpt"]

        self.assertEqual(len(excerpt), 1_200)


class ReviewAnswersCommandTests(OutcomeFixture):
    """Answers travel from a private file to a private packet and through capture."""

    def setUp(self) -> None:
        super().setUp()
        self.save(
            {
                **self.packet(),
                "entities": [
                    {"id": "entity_other", "type": "project", "name": "Other project"},
                    {
                        "id": "entity_person",
                        "type": "person",
                        "namespace": "entity_project",
                        "name": "Sam Example",
                    },
                ],
                "open_loops": [
                    self.loop("loop_report", checkpoint_at="2026-01-05T12:00:00Z"),
                    self.loop("loop_review", checkpoint_at="2026-01-05T12:00:00Z"),
                    self.loop("loop_audit", checkpoint_at="2026-01-05T12:00:00Z"),
                    self.loop("loop_handover", checkpoint_at="2026-01-05T12:00:00Z"),
                    self.loop(
                        "loop_other",
                        checkpoint_at="2026-01-05T12:00:00Z",
                        entity_refs=["entity_other"],
                    ),
                    self.loop("loop_hidden", sensitive=True),
                ],
                "decisions": [self.decision("decision_old"), self.decision("decision_new")],
            }
        )
        ideas = self.packet(tier=INFERENCE)
        ideas["decisions"] = [self.decision("decision_idea"), self.decision("decision_spare")]
        ideas["open_loops"] = [self.loop("loop_idea")]
        self.save(ideas, ceiling=INFERENCE)
        self.subscribe()
        self.output = self.root / "answers.packet.json"

    def command(self, answers_file: object, *, consumer: str = "consumer_alpha") -> dict[str, Any]:
        return self.run_cli(
            "review-answers",
            "--consumer-id",
            consumer,
            "--subscription-id",
            SCOPE,
            "--input",
            str(answers_file),
            "--packet-output",
            str(self.output),
        )

    def answer(self, *entries: dict[str, Any]) -> dict[str, Any]:
        return self.command(self.private_json("answers.json", document(*entries)))

    def test_one_batch_settles_commitments_and_proposals_through_a_single_capture(self) -> None:
        result = self.answer(
            {"id": "loop_report", "answer": "done", "outcome": "Report sent to the client"},
            {"id": "loop_review", "answer": "drop", "reason": "No longer needed"},
            {"id": "loop_audit", "answer": "later", "checkpoint_at": "2026-03-01T09:00:00-05:00"},
            {"id": "loop_handover", "answer": "hand_off", "owner_refs": ["entity_person"]},
            {"id": "loop_idea", "answer": "accept"},
            {"id": "decision_idea", "answer": "accept"},
            {"id": "decision_spare", "answer": "reject", "reason": "Too costly"},
            {"id": "decision_old", "answer": "replace", "superseded_by": "decision_new"},
        )

        saved = self.save(json.loads(self.output.read_text(encoding="utf-8")), ceiling=USER)

        self.assertEqual(saved["status"], "captured")
        self.assertEqual(len(result["answers"]), 8)
        statuses = {
            name: self.payload(name)["status"]
            for name in (
                "loop_report",
                "loop_review",
                "loop_audit",
                "loop_handover",
                "loop_idea",
                "decision_idea",
                "decision_spare",
                "decision_old",
            )
        }
        self.assertEqual(
            statuses,
            {
                "loop_report": "resolved",
                "loop_review": "resolved",
                "loop_audit": "open",
                "loop_handover": "open",
                "loop_idea": "open",
                "decision_idea": "accepted",
                "decision_spare": "rejected",
                "decision_old": "superseded",
            },
        )
        self.assertEqual(self.payload("loop_report")["resolution"], "Report sent to the client")
        self.assertEqual(self.payload("loop_review")["resolution"], "Withdrawn: No longer needed")
        self.assertEqual(self.payload("loop_handover")["owner_refs"], ["entity_person"])
        self.assertEqual(self.payload("decision_old")["superseded_by"], "decision_new")
        self.assertTrue(self.read("validate", full=True)["valid"])

    def test_one_batch_accepts_a_proposal_and_replaces_a_decision_with_it(self) -> None:
        self.answer(
            {"id": "decision_old", "answer": "replace", "superseded_by": "decision_idea"},
            {"id": "decision_idea", "answer": "accept"},
        )

        self.save(json.loads(self.output.read_text(encoding="utf-8")), ceiling=USER)

        self.assertEqual(self.payload("decision_idea")["status"], "accepted")
        self.assertEqual(self.payload("decision_old")["superseded_by"], "decision_idea")

    def test_replacing_a_decision_with_an_undecided_proposal_writes_no_packet(self) -> None:
        with self.assertRaisesRegex(ValueError, "accept decision_idea first"):
            self.answer(
                {"id": "decision_old", "answer": "replace", "superseded_by": "decision_idea"}
            )

        self.assertFalse(self.output.exists())

    def test_rescheduling_a_proposed_commitment_accepts_it(self) -> None:
        self.answer(
            {"id": "loop_idea", "answer": "later", "checkpoint_at": "2026-03-01T09:00:00-05:00"}
        )

        self.save(json.loads(self.output.read_text(encoding="utf-8")), ceiling=USER)

        saved = self.payload("loop_idea")
        self.assertEqual((saved["status"], saved["authority_state"]), ("open", "accepted"))
        self.assertEqual(
            graph.parse_time(saved["checkpoint_at"]), graph.parse_time("2026-03-01T14:00:00Z")
        )

    def test_answering_the_queue_empties_it(self) -> None:
        self.answer(
            {"id": "loop_report", "answer": "done", "outcome": "Sent"},
            {"id": "loop_review", "answer": "done", "outcome": "Reviewed"},
            {"id": "loop_audit", "answer": "drop", "reason": "Not needed"},
            {"id": "loop_handover", "answer": "drop", "reason": "Not needed"},
            {"id": "loop_idea", "answer": "drop", "reason": "Not needed"},
            {"id": "decision_idea", "answer": "accept"},
            {"id": "decision_spare", "answer": "reject"},
        )
        self.save(json.loads(self.output.read_text(encoding="utf-8")), ceiling=USER)

        summary = self.read("waiting_summary", SCOPE, now=NOW)

        self.assertEqual(summary["commitments"]["unfinished"], 0)
        self.assertEqual(summary["proposals"]["undecided"], 0)

    def test_building_the_packet_saves_nothing_and_says_what_capture_needs(self) -> None:
        before = self.read("state")

        result = self.answer({"id": "loop_report", "answer": "done", "outcome": "Sent"})

        self.assertEqual(self.read("state"), before)
        self.assertFalse(result["saved"])
        self.assertEqual(result["authority_ceiling"], "explicit_user_statement")
        self.assertEqual(
            result["answers"],
            [{"id": "loop_report", "collection": "open_loops", "answer": "done"}],
        )
        self.assertNotIn("packet", result)

    def test_a_record_outside_the_scope_or_hidden_refuses_the_batch(self) -> None:
        for label, identifier in (
            ("another project", "loop_other"),
            ("sensitive", "loop_hidden"),
            ("missing", "loop_absent"),
        ):
            with self.subTest(label), self.assertRaisesRegex(ValueError, "not an answerable"):
                self.answer(
                    {"id": "loop_report", "answer": "done", "outcome": "Sent"},
                    {"id": identifier, "answer": "done", "outcome": "Sent"},
                )

        self.assertFalse(self.output.exists())

    def test_another_consumer_cannot_answer_this_scope(self) -> None:
        answers_file = self.private_json(
            "answers.json", document({"id": "loop_report", "answer": "done", "outcome": "Sent"})
        )

        with self.assertRaisesRegex(ValueError, "consumer"):
            self.command(answers_file, consumer="consumer_beta")

    def test_a_malformed_answers_file_is_refused_at_the_command_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires outcome"):
            self.answer({"id": "loop_report", "answer": "done"})

        self.assertFalse(self.output.exists())

    def test_answers_must_come_from_a_file_and_the_packet_must_be_a_new_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "private file, not stdin"):
            self.command("-")

        self.answer({"id": "loop_report", "answer": "done", "outcome": "Sent"})
        with self.assertRaisesRegex(ValueError, "answers packet output already exists"):
            self.answer({"id": "loop_report", "answer": "done", "outcome": "Sent"})
