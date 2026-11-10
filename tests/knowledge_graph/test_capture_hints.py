"""Advisory hints in a capture dry run: an existing claim slot or a similar label."""

from __future__ import annotations

import unittest
from typing import Any

from tedd_knowledge_graph.v4_capture_hints import (
    MAX_SUGGESTIONS,
    label_words,
    labels_are_similar,
)

from tests.knowledge_graph.outcome_support import LATER, OutcomeFixture, Packet


class LabelSimilarityTests(unittest.TestCase):
    def test_labels_split_into_lower_case_words(self) -> None:
        self.assertEqual(label_words("Published_Verified"), {"published", "verified"})
        self.assertEqual(label_words("qa.receipt-v2"), {"qa", "receipt", "v2"})
        self.assertEqual(label_words("qa.receipt-v2", minimum=3), {"receipt"})
        self.assertEqual(label_words(None, 7, "release"), {"release"})

    def test_similarity_needs_containment_or_half_of_the_words_in_common(self) -> None:
        cases = (
            ("one contains the other", "published", "published_verified", True),
            ("half in common", "release_notes", "release_plan", False),
            ("two of three in common", "site_release_check", "site_release_plan", True),
            ("nothing in common", "incident", "workshop", False),
            ("identical", "release", "release", True),
        )
        for label, first, second, expected in cases:
            with self.subTest(label):
                self.assertIs(labels_are_similar(label_words(first), label_words(second)), expected)


class CaptureHintFixture(OutcomeFixture):
    """A project with three release events, one receipt kind, and two claim slots."""

    def setUp(self) -> None:
        super().setUp()
        self.save(
            {
                **self.packet(),
                "entities": [{"id": "entity_other", "type": "project", "name": "Other project"}],
                "events": [
                    self.event("event_release_1", "release"),
                    self.event("event_release_2", "release"),
                    self.event("event_release_3", "release"),
                    self.event("event_incident", "incident"),
                    self.event("event_foreign", "handover_review", entity_refs=["entity_other"]),
                    self.event("event_hidden", "audit_review", sensitive=True),
                ],
                "claims": [
                    self.claim("current_version", "Version one"),
                    self.claim("provider", "Provider A", family="hosting"),
                ],
            }
        )

    @staticmethod
    def event(identifier: str, kind: str, **fields: Any) -> Packet:
        return {
            "id": identifier,
            "kind": kind,
            "title": identifier,
            "summary": "Synthetic event",
            "occurred_at": LATER,
            "entity_refs": ["entity_project"],
            **fields,
        }

    def hints(self, **collections: list[Packet]) -> list[dict[str, Any]]:
        """Dry-run one later packet and return its suggestions, if any."""

        preview = self.preview({**self.packet(observed_at=LATER), **collections})
        return preview.get("suggestions", {}).get("items", [])


class SimilarLabelHintTests(CaptureHintFixture):
    def test_a_new_label_near_an_existing_one_names_the_existing_label(self) -> None:
        hints = self.hints(events=[self.event("event_new", "release_published")])

        self.assertEqual(
            hints,
            [
                {
                    "kind": "similar_label",
                    "record_id": "event_new",
                    "collection": "events",
                    "field": "kind",
                    "label": "release_published",
                    "similar": [{"label": "release", "uses": 3}],
                }
            ],
        )

    def test_a_label_the_project_already_uses_needs_no_hint(self) -> None:
        self.assertEqual(self.hints(events=[self.event("event_new", "release")]), [])

    def test_a_new_label_with_nothing_similar_needs_no_hint(self) -> None:
        self.assertEqual(self.hints(events=[self.event("event_new", "workshop")]), [])

    def test_a_dry_run_without_hints_carries_no_suggestions_field(self) -> None:
        preview = self.preview(
            {**self.packet(observed_at=LATER), "events": [self.event("event_new", "release")]}
        )

        self.assertNotIn("suggestions", preview)

    def test_labels_from_another_project_or_a_sensitive_record_are_never_suggested(self) -> None:
        for kind in ("handover", "audit"):
            with self.subTest(kind=kind):
                self.assertEqual(self.hints(events=[self.event("event_new", kind)]), [])

    def test_updating_an_existing_record_gets_no_hint(self) -> None:
        update = self.event("event_incident", "incident", summary="A longer account")

        self.assertEqual(self.hints(events=[update]), [])

    def test_hints_stop_at_the_limit_and_report_more(self) -> None:
        events = [
            self.event(f"event_new_{index:02}", f"release_step_{index:02}") for index in range(12)
        ]

        preview = self.preview({**self.packet(observed_at=LATER), "events": events})

        self.assertEqual(len(preview["suggestions"]["items"]), MAX_SUGGESTIONS)
        self.assertTrue(preview["suggestions"]["truncated"])


class ExistingSlotHintTests(CaptureHintFixture):
    def test_a_note_that_resembles_a_claim_slot_names_that_slot(self) -> None:
        hints = self.hints(observations=[self.note("current_version_check", "Version two shipped")])

        self.assertEqual(
            hints,
            [
                {
                    "kind": "existing_claim_slot",
                    "record_id": hints[0]["record_id"],
                    "family": "release",
                    "key": "current_version_check",
                    "surface_id": "surface_project_release_current_version",
                    "slot_family": "release",
                    "slot_key": "current_version",
                    "shared_words": 3,
                }
            ],
        )
        self.assertTrue(hints[0]["record_id"].startswith("observation_"))

    def test_a_note_sharing_one_word_with_a_slot_gets_no_hint(self) -> None:
        self.assertEqual(self.hints(observations=[self.note("team_size", "Three people")]), [])

    def test_a_note_about_another_project_is_not_matched_to_this_projects_slots(self) -> None:
        foreign = self.note("current_version_check", "Shipped", entity_refs=["entity_other"])

        self.assertEqual(self.hints(observations=[foreign]), [])


class HintIsAdvisoryTests(CaptureHintFixture):
    def test_a_hinted_packet_saves_exactly_what_it_says(self) -> None:
        packet = {
            **self.packet(observed_at=LATER),
            "events": [self.event("event_new", "release_published")],
        }
        preview = self.preview(packet)

        saved = self.save(packet)

        self.assertIn("suggestions", preview)
        self.assertNotIn("suggestions", saved)
        self.assertEqual(saved["fingerprint"], preview["fingerprint"])
        self.assertEqual(self.payload("event_new")["kind"], "release_published")
