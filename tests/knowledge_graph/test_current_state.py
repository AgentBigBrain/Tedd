"""The current-state sheet: what is true now inside one task scope."""

from __future__ import annotations

import json

import knowledge_graph as graph
import knowledge_graph_v4 as v4

from tests.knowledge_graph.outcome_support import (
    INFERENCE,
    LATER,
    SCOPE,
    OutcomeFixture,
    Packet,
)
from tests.support.graph_cases import PLAN_KEY

MUCH_LATER = "2026-02-01T09:30:00Z"
LATEST = "2026-02-05T09:30:00Z"
PREFERENCE = "Prefer: short lists. When: writing updates. Because: they scan quickly."


class CurrentStateFixture(OutcomeFixture):
    """Two current project claims beside history, a preference, and hidden records."""

    def setUp(self) -> None:
        super().setUp()
        self.save(
            {
                **self.packet(),
                "entities": [{"id": "entity_other", "type": "project", "name": "Other project"}],
                "claims": [
                    self.claim("current_version", "Version one"),
                    self.claim("provider", "Provider A", family="hosting"),
                    self.claim("status_updates", PREFERENCE, family="preference"),
                    self.claim(
                        "elsewhere",
                        "Other project fact",
                        surface_id="surface_other_release",
                        subject_refs=["entity_other"],
                    ),
                    self.claim("secret_plan", "Hidden project fact", sensitive=True),
                ],
            }
        )
        successor = self.packet(observed_at=LATER)
        successor["claims"] = [
            self.claim(
                "current_version", "Version two", valid_from=LATER, conflict_policy="replace"
            )
        ]
        self.save(successor)
        guess = self.packet(observed_at=LATER, tier=INFERENCE)
        guess["claims"] = [self.claim("owner", "Probably Sam", family="ownership")]
        self.save(guess, ceiling=INFERENCE)
        self.subscribe()

    def sheet(self) -> dict[str, object]:
        return self.read("current_state", SCOPE)

    def values(self) -> list[str]:
        return [item["value"] for item in self.sheet()["items"]]

    def restate(self, observed_at: str, *, tier: str) -> None:
        packet = self.packet(observed_at=observed_at, tier=tier)
        packet["claims"] = [self.claim("provider", "Provider A", family="hosting")]
        self.save(packet, ceiling=tier)

    def provider(self) -> dict[str, object]:
        return next(item for item in self.sheet()["items"] if item["key"] == "provider")


class CurrentStateTests(CurrentStateFixture):
    def test_the_sheet_holds_current_claims_and_leaves_out_everything_else(self) -> None:
        sheet = self.sheet()

        self.assertEqual(sorted(self.values()), ["Provider A", "Version two"])
        self.assertEqual(sheet["label"], "current claims, with dates")
        self.assertFalse(sheet["truncated"])
        self.assertFalse(sheet["omitted"])

    def test_a_claim_carries_its_slot_subject_start_date_and_authority(self) -> None:
        version = next(item for item in self.sheet()["items"] if item["key"] == "current_version")

        self.assertEqual(version["surface_ref"], "surface_project_release_current_version")
        self.assertEqual(version["subject_refs"], ["entity_project"])
        self.assertEqual(graph.parse_time(version["valid_from"]), graph.parse_time(LATER))
        self.assertEqual(version["source_tier"], "validated_structured_candidate")
        self.assertFalse(version["effective_conflict"])

    def test_restating_a_claim_with_validated_evidence_moves_its_confirmed_date(self) -> None:
        self.restate(MUCH_LATER, tier="validated_structured_candidate")

        confirmed = graph.parse_time(self.provider()["last_confirmed_at"])

        self.assertEqual(confirmed, graph.parse_time(MUCH_LATER))

    def test_an_inference_does_not_count_as_confirming_a_claim(self) -> None:
        self.restate(MUCH_LATER, tier="validated_structured_candidate")
        self.restate(LATEST, tier=INFERENCE)

        confirmed = graph.parse_time(self.provider()["last_confirmed_at"])

        self.assertEqual(confirmed, graph.parse_time(MUCH_LATER))

    def test_a_claim_about_the_project_comes_before_one_that_only_mentions_it(self) -> None:
        mention = self.packet(observed_at=LATEST)
        mention["claims"] = [
            self.claim(
                "dependency",
                "Other project depends on this project",
                surface_id="surface_other_dependency",
                subject_refs=["entity_other"],
                entity_refs=["entity_project"],
            )
        ]
        self.save(mention)

        self.assertEqual(self.values()[-1], "Other project depends on this project")

    def test_an_unresolved_conflict_is_flagged_instead_of_picking_a_winner(self) -> None:
        rival = self.packet(observed_at=MUCH_LATER)
        rival["claims"] = [
            self.claim("provider", "Provider B", family="hosting", valid_from=MUCH_LATER)
        ]
        self.save(rival)

        providers = [item for item in self.sheet()["items"] if item["key"] == "provider"]

        self.assertEqual(sorted(item["value"] for item in providers), ["Provider A", "Provider B"])
        self.assertTrue(all(item["effective_conflict"] for item in providers))

    def test_the_sheet_stops_at_twelve_claims_and_says_more_exist(self) -> None:
        many = self.packet(observed_at=LATER)
        many["claims"] = [self.claim(f"slot_{index:02}", f"Fact {index}") for index in range(12)]
        self.save(many)

        sheet = self.sheet()

        self.assertEqual(len(sheet["items"]), 12)
        self.assertTrue(sheet["truncated"])


class CurrentStateContextTests(CurrentStateFixture):
    def fill_scope_with_events(self, count: int) -> None:
        packet = self.packet(observed_at=MUCH_LATER)
        packet["events"] = [
            {
                "id": f"event_bulk_{index:02}",
                "kind": "milestone",
                "title": f"Bulk milestone {index}",
                "summary": "Synthetic detail. " * 40,
                "occurred_at": MUCH_LATER,
                "entity_refs": ["entity_project"],
            }
            for index in range(count)
        ]
        self.save(packet)

    def test_task_context_shows_the_sheet_after_preferences_and_before_records(self) -> None:
        context = self.context()

        sections = list(context)
        self.assertEqual(sections[:3], ["preferences", "current_state", "waiting"])
        self.assertLess(sections.index("current_state"), sections.index("records"))
        self.assertEqual(context["current_state"], self.sheet())

    def test_the_sheet_keeps_its_share_when_recent_records_would_fill_the_budget(self) -> None:
        self.fill_scope_with_events(30)

        context = self.context(max_chars=12_000)

        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 12_000)
        self.assertEqual(
            sorted(item["value"] for item in context["current_state"]["items"]),
            ["Provider A", "Version two"],
        )
        self.assertTrue(context["records"])
        self.assertTrue(context["truncated"])

    def test_the_sheet_never_takes_more_than_its_ceiling(self) -> None:
        wide = self.packet(observed_at=LATER)
        wide["claims"] = [self.claim(f"slot_{index:02}", "Long fact. " * 60) for index in range(10)]
        self.save(wide)

        section = self.context(max_chars=50_000)["current_state"]

        self.assertLessEqual(len(json.dumps(section, ensure_ascii=False)), 2_400)
        self.assertTrue(section["items"])
        self.assertTrue(section["truncated"])

    def test_a_small_budget_gives_the_sheet_at_most_a_quarter(self) -> None:
        section = self.context(max_chars=2_000)["current_state"]

        self.assertLessEqual(len(json.dumps(section, ensure_ascii=False)), 500)

    def test_a_withheld_sheet_is_reported_as_omitted_not_as_empty(self) -> None:
        section = self.context(max_chars=1_200)["current_state"]

        self.assertEqual(section["items"], [])
        self.assertTrue(section["omitted"])
        self.assertEqual(section["reason"], "budget_exhausted")

    def test_an_optional_plan_covers_the_claims_the_sheet_disclosed(self) -> None:
        result = self.run_cli(
            "context",
            "--subscription-id",
            SCOPE,
            "--max-items",
            "1",
            "--plan",
            "--fingerprint",
            "synthetic-sheet-read",
            "--mode",
            "operator_full",
        )

        plan = v4.verify_plan_token(result["plan_token"], PLAN_KEY)
        covered = {item["record_id"] for item in plan["read_set"]}
        disclosed = {item["id"] for item in result["current_state"]["items"]}
        self.assertTrue(disclosed)
        self.assertLessEqual(disclosed, covered)

    def test_review_safe_context_redacts_a_path_inside_a_claim_value(self) -> None:
        packet: Packet = self.packet(observed_at=LATER)
        packet["claims"] = [self.claim("location", 'Stored at "/Users/example/private/plan"')]
        self.save(packet)

        section = self.context()["current_state"]

        self.assertNotIn("/Users/example/private/plan", json.dumps(section))
