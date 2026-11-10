"""Folding notes into claims: what leaves recent context and what stays reachable."""

from __future__ import annotations

from tests.knowledge_graph.outcome_support import (
    INFERENCE,
    LATER,
    SCOPE,
    OutcomeFixture,
    Packet,
)

OTHER_SCOPE = "scope_beta"


class FoldFixture(OutcomeFixture):
    """Four project notes, one of them shared with another project, and one foreign note."""

    def setUp(self) -> None:
        super().setUp()
        saved = self.save(
            {
                **self.packet(),
                "entities": [{"id": "entity_other", "type": "project", "name": "Other project"}],
                "observations": [
                    self.note("state_one", "Version one shipped"),
                    self.note("state_two", "Version two shipped"),
                    self.note("provider_note", "Hosted on Provider A", family="hosting"),
                    self.note(
                        "shared_note",
                        "Both projects share a pipeline",
                        entity_refs=["entity_project", "entity_other"],
                    ),
                    self.note("elsewhere", "Another project shipped", entity_refs=["entity_other"]),
                ],
            }
        )
        self.notes = {self.payload(note)["key"]: note for note in self.note_ids(saved)}
        self.subscribe()
        self.subscribe("entity_other", subscription_id=OTHER_SCOPE, consumer_id="consumer_beta")

    def fold(self, *keys: str, tier: str = "validated_structured_candidate") -> None:
        """Capture one release claim that cites the named notes."""

        packet: Packet = self.packet(observed_at=LATER, tier=tier)
        packet["claims"] = [
            self.claim(
                "current_version",
                "Version two is current",
                valid_from=LATER,
                observation_refs=[self.notes[key] for key in keys],
            )
        ]
        self.save(packet, ceiling=tier)

    def recent_notes(self, subscription_id: str = SCOPE) -> set[str]:
        records = self.read("context_pack", subscription_id)["records"]
        return {
            record["payload"]["key"] for record in records if record["collection"] == "observations"
        }

    def candidates(self, **options: object) -> dict[str, object]:
        return self.read("fold_candidates", SCOPE, **options)


class FoldedContextTests(FoldFixture):
    def test_a_folded_note_leaves_recent_context_and_the_rest_stay(self) -> None:
        self.fold("state_one", "state_two")

        self.assertEqual(self.recent_notes(), {"provider_note", "shared_note"})

    def test_nothing_leaves_recent_context_before_a_claim_cites_it(self) -> None:
        self.assertEqual(
            self.recent_notes(), {"state_one", "state_two", "provider_note", "shared_note"}
        )

    def test_a_folded_note_is_still_found_by_search(self) -> None:
        self.fold("state_one", "state_two")

        found = self.read("search_subscription", SCOPE, "Version one shipped")

        self.assertEqual(
            [item["record_id"] for item in found["results"]], [self.notes["state_one"]]
        )

    def test_a_scope_that_cannot_read_the_claim_keeps_seeing_the_shared_note(self) -> None:
        self.fold("shared_note")

        self.assertNotIn("shared_note", self.recent_notes())
        self.assertIn("shared_note", self.recent_notes(OTHER_SCOPE))

    def test_an_inference_cannot_fold_notes_into_a_claim_that_is_already_current(self) -> None:
        self.fold("state_one")

        with self.assertRaisesRegex(ValueError, "validated_structured_candidate or above"):
            self.fold("state_two", tier=INFERENCE)

        self.assertEqual(self.recent_notes(), {"state_two", "provider_note", "shared_note"})

    def test_a_quarantined_claim_does_not_fold_the_note_saved_with_it(self) -> None:
        guess: Packet = self.packet(observed_at=LATER, tier=INFERENCE)
        guess["observations"] = [self.note("current_version", "Version two is current")]
        guess["claims"] = [self.claim("current_version", "Version two is current")]

        saved = self.save(guess, ceiling=INFERENCE)

        claim = self.payload(next(i for i in saved["changed_ids"] if i.startswith("claim_")))
        self.assertEqual((claim["lifecycle"], len(claim["observation_refs"])), ("quarantined", 1))
        self.assertIn("current_version", self.recent_notes())


class FoldCandidateTests(FoldFixture):
    def test_candidates_list_unfolded_notes_grouped_by_family(self) -> None:
        candidates = self.candidates()

        self.assertEqual(candidates["unfolded"], 4)
        self.assertEqual(
            candidates["families"]["items"],
            [{"family": "release", "notes": 3}, {"family": "hosting", "notes": 1}],
        )
        self.assertEqual(
            [item["key"] for item in candidates["notes"]["items"]],
            ["provider_note", "shared_note", "state_one", "state_two"],
        )

    def test_folding_removes_notes_from_the_candidates_and_adds_the_slot(self) -> None:
        self.fold("state_one", "state_two")

        candidates = self.candidates()

        self.assertEqual(candidates["unfolded"], 2)
        self.assertEqual(
            [item["value"] for item in candidates["slots"]["items"]], ["Version two is current"]
        )
        self.assertEqual(candidates["slots"]["items"][0]["cited_observations"], 2)

    def test_a_family_narrows_the_notes_but_not_the_totals(self) -> None:
        candidates = self.candidates(family="Hosting")

        self.assertEqual([item["key"] for item in candidates["notes"]["items"]], ["provider_note"])
        self.assertEqual(candidates["unfolded"], 4)
        self.assertEqual(candidates["family"], "hosting")

    def test_a_subject_narrows_notes_to_that_entity(self) -> None:
        candidates = self.candidates(subject_id="entity_other")

        self.assertEqual([item["key"] for item in candidates["notes"]["items"]], ["shared_note"])
        self.assertEqual(candidates["unfolded"], 1)

    def test_the_note_limit_is_bounded_and_reports_more(self) -> None:
        candidates = self.candidates(limit=1)

        self.assertEqual(len(candidates["notes"]["items"]), 1)
        self.assertTrue(candidates["notes"]["truncated"])
        for limit in (0, 51, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.candidates(limit=limit)

    def test_a_foreign_note_is_never_a_candidate(self) -> None:
        keys = {item["key"] for item in self.candidates(limit=50)["notes"]["items"]}

        self.assertNotIn("elsewhere", keys)


class FoldQueueTests(FoldFixture):
    def test_the_queue_counts_unfolded_notes_for_the_named_subject(self) -> None:
        queue = self.read("review_queue", SCOPE)

        self.assertEqual(queue["notes"]["items"], [{"subject_id": "entity_project", "unfolded": 4}])
        self.assertEqual(queue["summary"]["notes"], {"unfolded": 4})

    def test_folding_lowers_the_count_the_queue_reports(self) -> None:
        self.fold("state_one", "state_two")

        queue = self.read("review_queue", SCOPE)

        self.assertEqual(queue["notes"]["items"], [{"subject_id": "entity_project", "unfolded": 2}])


class FoldCandidatesCommandTests(FoldFixture):
    def command(self, *extra: str, consumer: str = "consumer_alpha") -> dict[str, object]:
        return self.run_cli(
            "fold-candidates", "--consumer-id", consumer, "--subscription-id", SCOPE, *extra
        )

    def test_the_command_lists_one_family_for_the_owning_consumer(self) -> None:
        result = self.command("--family", "release", "--limit", "2")

        self.assertEqual(
            [item["key"] for item in result["notes"]["items"]], ["shared_note", "state_one"]
        )
        self.assertTrue(result["notes"]["truncated"])

    def test_another_consumer_cannot_list_this_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "consumer"):
            self.command(consumer="consumer_beta")

    def test_the_command_refuses_a_limit_outside_its_range(self) -> None:
        with self.assertRaises(ValueError):
            self.command("--limit", "51")
