"""Capture semantics for stated outcomes: closed loops, replaced decisions, cited notes."""

from __future__ import annotations

import copy
import unittest

import knowledge_graph as graph

from tests.knowledge_graph.outcome_support import (
    EARLY,
    INFERENCE,
    LATER,
    USER,
    VALIDATED,
    OutcomeFixture,
    Packet,
)

MUCH_LATER = "2026-02-01T09:30:00Z"
LATEST = "2026-02-05T09:30:00Z"
# Computed by the 1.2.0-rc.12 release for the packet in ReleasedFingerprintTests.
RELEASED_FINGERPRINT = "af12796ea096f2ce15d2b118fc5f57a3"


class ReleasedFingerprintTests(unittest.TestCase):
    def test_a_packet_without_the_new_fields_keeps_its_released_fingerprint(self) -> None:
        """A replayed older packet must still be recognized as the same capture."""

        packet = OutcomeFixture.packet()
        packet["source"]["title"] = "Synthetic source"
        packet["claims"] = [OutcomeFixture.claim("current_version", "Version two is current")]
        packet["decisions"] = [OutcomeFixture.decision("decision_old")]
        packet["open_loops"] = [
            OutcomeFixture.loop("loop_report", checkpoint_at="2026-03-01T09:00:00Z")
        ]
        blank = graph.blank_graph()
        blank["_capture_fingerprint_only"] = graph._CAPTURE_FINGERPRINT_ONLY

        result = graph.capture_packet(
            blank,
            packet,
            authority_ceiling=VALIDATED,
            producer={"agent_id": "agent_alpha", "task_id": "task_alpha", "run_id": "run_original"},
            based_on_watermark=1,
            discovery_kind="milestone",
        )

        self.assertEqual(result["fingerprint"], RELEASED_FINGERPRINT)


class LoopOutcomeTests(OutcomeFixture):
    def answer(self, *, observed_at: str = LATER, **fields: object) -> Packet:
        packet = self.packet(observed_at=observed_at, tier=USER)
        packet["open_loops"] = [self.loop("loop_report", **fields)]
        return packet

    def setUp(self) -> None:
        super().setUp()
        self.save({**self.packet(), "open_loops": [self.loop("loop_report")]})

    def test_a_user_statement_closes_a_loop_with_its_outcome_and_time(self) -> None:
        answer = self.answer(status="resolved", resolution="Report sent to the client")

        self.save(answer, ceiling=USER)

        saved = self.payload("loop_report")
        self.assertEqual(saved["status"], "resolved")
        self.assertEqual(saved["resolution"], "Report sent to the client")
        self.assertEqual(graph.parse_time(saved["resolved_at"]), graph.parse_time(LATER))

    def test_an_inference_cannot_close_an_accepted_loop(self) -> None:
        guess = self.packet(observed_at=LATER, tier=INFERENCE)
        guess["open_loops"] = [
            self.loop("loop_report", status="resolved", resolution="Probably finished")
        ]

        with self.assertRaisesRegex(ValueError, "authoritative source"):
            self.preview(guess, ceiling=INFERENCE)

        self.assertEqual(self.payload("loop_report")["status"], "open")

    def test_a_later_capture_does_not_rewrite_a_recorded_outcome(self) -> None:
        self.save(
            self.answer(status="resolved", resolution="Report sent to the client"), ceiling=USER
        )
        second = self.answer(
            observed_at=MUCH_LATER, status="resolved", resolution="A different story"
        )

        self.save(second, ceiling=USER)

        saved = self.payload("loop_report")
        self.assertEqual(saved["resolution"], "Report sent to the client")
        self.assertEqual(graph.parse_time(saved["resolved_at"]), graph.parse_time(LATER))

    def test_the_stated_outcome_is_part_of_the_reviewed_fingerprint(self) -> None:
        sent = self.answer(status="resolved", resolution="Sent")
        cancelled = self.answer(status="resolved", resolution="Cancelled")

        first = self.preview(sent, ceiling=USER)
        second = self.preview(cancelled, ceiling=USER)

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


class DecisionOutcomeTests(OutcomeFixture):
    def answer(self, *records: Packet, tier: str = USER, observed_at: str = LATER) -> Packet:
        packet = self.packet(observed_at=observed_at, tier=tier)
        packet["decisions"] = list(records)
        return packet

    def seed_pair(self) -> None:
        self.save(
            {
                **self.packet(),
                "decisions": [self.decision("decision_old"), self.decision("decision_new")],
            }
        )

    def test_a_user_statement_rejects_a_proposal(self) -> None:
        proposal = self.answer(self.decision("decision_idea"), tier=INFERENCE, observed_at=LATER)
        proposal["source"]["observed_at"] = "2026-01-03T12:00:00Z"
        self.save(proposal, ceiling=INFERENCE)
        self.assertEqual(self.payload("decision_idea")["status"], "proposed")

        self.save(self.answer(self.decision("decision_idea", status="rejected")), ceiling=USER)

        saved = self.payload("decision_idea")
        self.assertEqual(saved["status"], "rejected")
        self.assertEqual(saved["authority_state"], "accepted")

    def test_a_replaced_decision_links_to_its_successor(self) -> None:
        self.seed_pair()
        replaced = self.decision("decision_old", status="superseded", superseded_by="decision_new")

        self.save(self.answer(replaced), ceiling=USER)

        saved = self.payload("decision_old")
        self.assertEqual(saved["status"], "superseded")
        self.assertEqual(saved["superseded_by"], "decision_new")
        self.assertEqual(self.references("decision_old", "superseded_by"), ["decision_new"])
        self.assertTrue(self.read("validate", full=True)["valid"])

    def test_a_successor_link_needs_an_existing_decision_and_authority(self) -> None:
        self.seed_pair()
        cases = (
            ("missing target", {"status": "superseded", "superseded_by": "decision_gone"}, USER),
            ("itself", {"status": "superseded", "superseded_by": "decision_old"}, USER),
            ("still accepted", {"status": "accepted", "superseded_by": "decision_new"}, USER),
            ("inference", {"status": "superseded", "superseded_by": "decision_new"}, INFERENCE),
        )
        expected = {
            "missing target": "existing decision",
            "itself": "cycle",
            "still accepted": "status superseded",
            "inference": "authoritative source",
        }
        for label, fields, tier in cases:
            with self.subTest(label):
                packet = self.answer(self.decision("decision_old", **fields), tier=tier)

                with self.assertRaisesRegex(ValueError, expected[label]):
                    self.preview(packet, ceiling=tier)

        self.assertEqual(self.payload("decision_old")["status"], "accepted")

    def propose(self, identifier: str) -> None:
        """Store one decision that no authoritative source has ruled on."""

        self.save(
            self.answer(self.decision(identifier), tier=INFERENCE, observed_at=EARLY),
            ceiling=INFERENCE,
        )

    def replacing(self, replaced: str, successor: str, **source: str) -> Packet:
        """A packet that marks one decision as superseded by another."""

        return self.answer(
            self.decision(replaced, status="superseded", superseded_by=successor), **source
        )

    def seed_replaced(self) -> None:
        """Three accepted decisions, the old one already replaced by the new one."""

        self.seed_pair()
        self.save({**self.packet(), "decisions": [self.decision("decision_other")]})
        self.save(self.replacing("decision_old", "decision_new"), ceiling=USER)

    def test_a_decision_is_not_replaced_by_one_that_does_not_stand(self) -> None:
        self.seed_pair()
        self.propose("decision_idea")
        self.propose("decision_declined")
        self.save(self.answer(self.decision("decision_declined", status="rejected")), ceiling=USER)
        cases = (
            ("undecided proposal", "decision_idea", "accept that proposal first"),
            ("rejected decision", "decision_declined", "was rejected"),
        )
        for label, successor, message in cases:
            packet = self.replacing("decision_old", successor, observed_at=MUCH_LATER)
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                self.preview(packet, ceiling=USER)

        self.assertEqual(self.payload("decision_old")["status"], "accepted")

    def test_one_capture_accepts_a_proposal_and_names_it_as_the_successor(self) -> None:
        self.seed_pair()
        cases = (
            ("accept listed first", "decision_old", "decision_idea", False),
            ("replace listed first", "decision_new", "decision_spare", True),
        )
        for label, replaced, proposal, replace_first in cases:
            with self.subTest(label):
                self.propose(proposal)
                records = [
                    self.decision(proposal, status="accepted"),
                    self.decision(replaced, status="superseded", superseded_by=proposal),
                ]
                if replace_first:
                    records.reverse()

                self.save(self.answer(*records), ceiling=USER)

                self.assertEqual(self.payload(proposal)["status"], "accepted")
                self.assertEqual(self.payload(replaced)["superseded_by"], proposal)

    def test_a_superseded_decision_can_still_be_named_as_a_successor(self) -> None:
        self.seed_replaced()

        self.save(
            self.replacing("decision_other", "decision_old", observed_at=MUCH_LATER), ceiling=USER
        )

        self.assertEqual(self.payload("decision_other")["superseded_by"], "decision_old")
        self.assertTrue(self.read("validate", full=True)["valid"])

    def test_only_a_source_that_outranks_the_record_moves_a_stored_link(self) -> None:
        self.seed_replaced()
        cases = (
            ("inference", INFERENCE, MUCH_LATER),
            ("validated source after a user statement", VALIDATED, MUCH_LATER),
            ("older user statement", USER, EARLY),
        )
        for label, tier, observed_at in cases:
            packet = self.replacing(
                "decision_old", "decision_other", tier=tier, observed_at=observed_at
            )
            with self.subTest(label), self.assertRaisesRegex(ValueError, "authoritative source"):
                self.preview(packet, ceiling=tier)

        self.assertEqual(self.payload("decision_old")["superseded_by"], "decision_new")

    def test_a_newer_user_statement_corrects_the_successor(self) -> None:
        self.seed_replaced()

        self.save(
            self.replacing("decision_old", "decision_other", observed_at=MUCH_LATER), ceiling=USER
        )

        self.assertEqual(self.references("decision_old", "superseded_by"), ["decision_other"])

    def test_restating_a_stored_link_is_not_a_change_that_needs_authority(self) -> None:
        self.seed_replaced()
        restated = self.replacing(
            "decision_old", "decision_new", tier=INFERENCE, observed_at=MUCH_LATER
        )

        self.save(restated, ceiling=INFERENCE)

        self.assertEqual(self.payload("decision_old")["superseded_by"], "decision_new")

    def test_a_stored_link_survives_a_later_ruling_on_its_successor(self) -> None:
        self.seed_replaced()
        self.save(
            self.answer(self.decision("decision_new", status="rejected"), observed_at=MUCH_LATER),
            ceiling=USER,
        )

        self.save(self.replacing("decision_old", "decision_new", observed_at=LATEST), ceiling=USER)

        self.assertEqual(self.payload("decision_old")["superseded_by"], "decision_new")
        self.assertTrue(self.read("validate", full=True)["valid"])

    def test_two_decisions_cannot_replace_each_other(self) -> None:
        self.seed_pair()
        self.save(
            self.answer(
                self.decision("decision_old", status="superseded", superseded_by="decision_new")
            ),
            ceiling=USER,
        )
        reverse = self.answer(
            self.decision("decision_new", status="superseded", superseded_by="decision_old"),
            observed_at=MUCH_LATER,
        )

        with self.assertRaisesRegex(ValueError, "cycle"):
            self.preview(reverse, ceiling=USER)


class CitedObservationTests(OutcomeFixture):
    def setUp(self) -> None:
        super().setUp()
        saved = self.save(
            {
                **self.packet(),
                "entities": [{"id": "entity_other", "type": "project", "name": "Other project"}],
                "observations": [
                    self.note("state_one", "Version one shipped"),
                    self.note("state_two", "Version two shipped"),
                    self.note("elsewhere", "Another project shipped", entity_refs=["entity_other"]),
                ],
            }
        )
        notes = {note: self.payload(note)["entity_refs"] for note in self.note_ids(saved)}
        self.project_notes = sorted(n for n, refs in notes.items() if refs == ["entity_project"])
        self.foreign_notes = sorted(n for n, refs in notes.items() if refs == ["entity_other"])

    def citing(self, cited: object, *, observed_at: str = LATER, tier: str = VALIDATED) -> Packet:
        packet = self.packet(observed_at=observed_at, tier=tier)
        packet["claims"] = [
            self.claim(
                "current_version",
                "Version two is current",
                valid_from=LATER,
                observation_refs=cited,
            )
        ]
        return packet

    def current_claim(self) -> dict[str, object]:
        return self.read("current_claims", ["surface_project_release_current_version"])[0]

    def test_a_claim_cites_the_existing_notes_it_replaces(self) -> None:
        self.save(self.citing(self.project_notes))

        claim = self.current_claim()
        self.assertEqual(sorted(claim["payload"]["observation_refs"]), self.project_notes)
        self.assertEqual(
            sorted(self.references(str(claim["record_id"]), "observation")), self.project_notes
        )
        self.assertTrue(self.read("validate", full=True)["valid"])

    def test_restating_a_current_claim_folds_more_notes_into_it(self) -> None:
        self.save(self.citing(self.project_notes[:1]))

        self.save(self.citing(self.project_notes[1:], observed_at=MUCH_LATER))

        self.assertEqual(
            sorted(self.current_claim()["payload"]["observation_refs"]), self.project_notes
        )

    def test_a_claim_cannot_cite_unknown_unrelated_or_malformed_notes(self) -> None:
        cases = (
            ("unknown", ["observation_missing"], "existing observations"),
            ("another entity", self.foreign_notes, "share an entity"),
            ("not a list", "observation_missing", "must be a list"),
        )
        for label, cited, message in cases:
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                self.preview(self.citing(cited))

    def test_a_source_below_the_validated_tier_cannot_cite_notes(self) -> None:
        cases = (
            ("an inference", INFERENCE),
            ("a validated source capped by the caller", VALIDATED),
        )
        for label, tier in cases:
            packet = self.citing(self.project_notes, tier=tier)
            with (
                self.subTest(label),
                self.assertRaisesRegex(ValueError, "validated_structured_candidate or above"),
            ):
                self.preview(packet, ceiling=INFERENCE)

    def test_an_inference_restating_a_current_claim_adds_no_notes_to_it(self) -> None:
        self.save(self.citing(self.project_notes[:1]))
        guess = self.citing(self.project_notes[1:], observed_at=MUCH_LATER, tier=INFERENCE)

        with self.assertRaisesRegex(ValueError, "validated_structured_candidate or above"):
            self.preview(guess, ceiling=INFERENCE)

        self.assertEqual(
            self.current_claim()["payload"]["observation_refs"], self.project_notes[:1]
        )

    def test_citing_notes_changes_the_reviewed_fingerprint(self) -> None:
        citing = self.citing(self.project_notes)
        plain = copy.deepcopy(citing)
        del plain["claims"][0]["observation_refs"]

        self.assertNotEqual(self.preview(plain)["fingerprint"], self.preview(citing)["fingerprint"])
