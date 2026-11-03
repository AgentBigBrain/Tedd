#!/usr/bin/env python3
"""Readable, privacy-preserving connection contracts for the Obsidian projection."""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from importlib import import_module
from typing import Protocol, cast
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

SCRIPT = SCRIPT_DIRECTORY / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_connections_target", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)

FIXED_NOW = "2026-09-20T12:30:00+00:00"


class _YamlLoader(Protocol):
    def safe_load(self, stream: str) -> object:
        """Decode one YAML document for structural test validation."""

        ...


def readable_graph(
    *,
    extra_relations: int = 0,
    extra_activity: int = 0,
    extra_observations: int = 0,
    include_claims: bool = True,
) -> dict[str, object]:
    """Build a renderer fixture; this is not reviewed-capture qualification."""

    entities: list[dict[str, object]] = [
        {
            "ref": "mira",
            "type": "person",
            "namespace": "people",
            "name": "Mira",
            "sensitive": False,
        },
        {
            "ref": "harbor",
            "type": "project",
            "namespace": "projects",
            "name": "Harbor",
            "sensitive": False,
        },
        {
            "ref": "other_mira",
            "type": "person",
            "namespace": "vendors",
            "name": "Mira",
            "sensitive": False,
        },
        {
            "ref": "northstar",
            "type": "service",
            "namespace": "services",
            "name": "Northstar",
            "sensitive": False,
        },
    ]
    relations: list[dict[str, object]] = [
        {
            "source": "mira",
            "target": "harbor",
            "type": "contributes_to",
            "status": "current",
            "sensitive": False,
        },
        {
            "source": "harbor",
            "target": "other_mira",
            "type": "unmapped_relation",
            "status": "candidate",
            "sensitive": False,
        },
        {
            "source": "harbor",
            "target": "northstar",
            "type": "depends_on",
            "status": "current",
            "sensitive": False,
        },
        {
            "source": "harbor",
            "target": "northstar",
            "type": "hosted_by",
            "status": "current",
            "sensitive": False,
        },
    ]
    for index in range(extra_relations):
        ref = f"service_{index:02d}"
        entities.append(
            {
                "ref": ref,
                "type": "service",
                "namespace": "services",
                "name": f"Service {index:02d}",
                "sensitive": False,
            }
        )
        relations.append(
            {
                "source": "harbor",
                "target": ref,
                "type": "depends_on",
                "status": "current",
                "sensitive": False,
            }
        )

    with mock.patch.object(kg, "utc_now", autospec=True, return_value=FIXED_NOW):
        graph: dict[str, object] = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "discussion",
                    "role": "user",
                    "title": "Planning discussion",
                    "observed_at": "2026-09-18T14:00:00Z",
                    "source_tier": "explicit_user_statement",
                    "excerpt": "Mira will review the draft for Harbor.",
                    "sensitive": False,
                },
                "entities": entities,
                "relations": relations,
                "observations": [
                    {
                        "family": "planning",
                        "key": "draft_review",
                        "value": "Mira will review the draft",
                        "entity_refs": ["mira", "harbor"],
                        "observed_at": "2026-09-18T14:00:00Z",
                        "sensitive": False,
                    },
                    *[
                        {
                            "family": "planning_history",
                            "key": f"observation_{index:02d}",
                            "value": f"Recorded planning note {index:02d}",
                            "entity_refs": ["harbor"],
                            "observed_at": f"2026-09-17T{index % 24:02d}:30:00Z",
                            "sensitive": False,
                        }
                        for index in range(extra_observations)
                    ],
                ],
                "claims": [
                    {
                        "family": "planning",
                        "key": "draft_review",
                        "value": "Mira will review the draft",
                        "entity_refs": ["mira", "harbor"],
                        "valid_from": "2026-09-18T14:00:00Z",
                        "sensitive": False,
                    }
                ]
                if include_claims
                else [],
                "decisions": [
                    {
                        "id": "decision_harbor_review",
                        "title": "Review the Harbor draft",
                        "summary": "Mira will review before publication.",
                        "rationale": "Catch errors early.",
                        "decided_at": "2026-09-18T14:05:00Z",
                        "status": "accepted",
                        "entity_refs": ["mira", "harbor"],
                        "sensitive": False,
                    },
                    {
                        "id": "decision_harbor_candidate",
                        "title": "Consider another reviewer",
                        "summary": "A second reviewer remains a proposal.",
                        "rationale": "The proposal has not been accepted.",
                        "decided_at": "2026-09-18T14:06:00Z",
                        "status": "candidate",
                        "entity_refs": ["harbor"],
                        "sensitive": False,
                    },
                ],
                "open_loops": [
                    {
                        "id": "loop_harbor_review",
                        "title": "Finish the Harbor review",
                        "summary": "Review remains open.",
                        "created_at": "2026-09-18T14:10:00Z",
                        "status": "open",
                        "priority": 2,
                        "owner_refs": ["mira"],
                        "entity_refs": ["harbor"],
                        "next_action": "Review the draft.",
                        "terminal_condition": "Review is recorded.",
                        "sensitive": False,
                    },
                    {
                        "id": "loop_harbor_archived",
                        "title": "Archive the prior draft",
                        "summary": "The prior draft was archived.",
                        "created_at": "2026-09-17T14:10:00Z",
                        "status": "open",
                        "priority": 1,
                        "owner_refs": ["mira"],
                        "entity_refs": ["harbor"],
                        "next_action": "Send the already archived draft.",
                        "terminal_condition": "Archive is recorded.",
                        "sensitive": False,
                    },
                ],
                "events": [
                    {
                        "kind": "milestone",
                        "title": "Draft prepared",
                        "summary": "Harbor draft is ready for review.",
                        "occurred_at": "2026-09-18T14:15:00Z",
                        "status": "complete",
                        "entity_refs": ["harbor"],
                        "sensitive": False,
                    }
                ],
                "receipts": [
                    {
                        "kind": "verification",
                        "title": "Draft receipt",
                        "summary": "Draft bytes were verified.",
                        "verified_at": "2026-09-18T14:20:00Z",
                        "verified": True,
                        "entity_refs": ["harbor"],
                        "sensitive": False,
                    },
                    *[
                        {
                            "id": f"receipt_activity_{index:02d}",
                            "kind": "verification",
                            "title": f"Activity receipt {index:02d}",
                            "summary": "Synthetic activity evidence.",
                            "verified_at": f"2026-09-17T{index % 24:02d}:00:00Z",
                            "verified": True,
                            "entity_refs": ["harbor"],
                            "sensitive": False,
                        }
                        for index in range(extra_activity)
                    ],
                ],
            },
            authority_ceiling="explicit_user_statement",
        )
    archived_loop = next(
        loop
        for loop in cast(list[dict[str, object]], graph["open_loops"])
        if loop["id"] == "loop_harbor_archived"
    )
    with mock.patch.object(
        kg,
        "utc_now",
        autospec=True,
        return_value="2026-09-18T13:00:00Z",
    ):
        kg.resolve_loop(graph, str(archived_loop["id"]), "Draft review recorded.")
    return graph


def note_for(notes: dict[str, str], record_id: str) -> tuple[str, str]:
    marker = f"kg_id: {json.dumps(record_id)}"
    return next((path, note) for path, note in notes.items() if marker in note)


def internal_link_targets(content: str) -> list[str]:
    """Parse generated wikilinks while honoring escapes in their display labels."""

    targets: list[str] = []
    cursor = 0
    while (start := content.find("[[", cursor)) >= 0:
        position = start + 2
        target: list[str] = []
        in_label = False
        escaped = False
        while position < len(content):
            character = content[position]
            if escaped:
                if not in_label:
                    target.append(character)
                escaped = False
                position += 1
                continue
            if character == "\\":
                escaped = True
                position += 1
                continue
            if not in_label and content.startswith("[[", position):
                raise AssertionError(f"nested wikilink opener at offset {position}")
            if not in_label and character == "|":
                in_label = True
                position += 1
                continue
            if content.startswith("]]", position):
                targets.append("".join(target))
                cursor = position + 2
                break
            if not in_label:
                target.append(character)
            position += 1
        else:
            raise AssertionError(f"unterminated wikilink at offset {start}")
    return targets


def dashboard_section(content: str, heading: str) -> str:
    """Return one second-level dashboard section without parsing record prose."""

    start = content.index(f"## {heading}")
    end = content.find("\n## ", start + 3)
    return content[start:] if end < 0 else content[start:end]


class ReadableConnectionsTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_every_generated_markdown_note_has_constant_reading_class(self) -> None:
        notes = kg.build_projection_notes(readable_graph(), "review_safe", "Tedd Knowledge")
        yaml = cast(_YamlLoader, import_module("yaml"))

        for path, content in notes.items():
            if not path.endswith(".md"):
                continue
            with self.subTest(path=path):
                self.assertTrue(content.startswith("---\n"))
                metadata = cast(dict[str, object], yaml.safe_load(content.split("---", 2)[1]))
                self.assertEqual(metadata["cssclasses"], ["tedd-reading"])
                self.assertNotIn("[[", " ".join(cast(list[str], metadata["cssclasses"])))

    def test_observation_folder_is_an_owned_generated_projection_boundary(self) -> None:
        self.assertTrue(kg.generated_projection_path("22 Observations/example.md"))
        self.assertFalse(kg.generated_projection_path("22 Observations/nested/example.md"))

    def test_claim_observation_and_evidence_are_navigable_without_raw_refs(self) -> None:
        graph = readable_graph()
        claims = cast(list[dict[str, object]], graph["claims"])
        observations = cast(list[dict[str, object]], graph["observations"])
        sources = cast(list[dict[str, object]], graph["sources"])

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")

        claim_path, claim_note = note_for(notes, str(claims[0]["id"]))
        observation_path, observation_note = note_for(notes, str(observations[0]["id"]))
        source_path = next(path for path in notes if path.startswith("20 Evidence/"))
        self.assertIn("22 Observations/", observation_path)
        self.assertIn(f"[[Tedd Knowledge/{observation_path.removesuffix('.md')}|", claim_note)
        self.assertIn(f"[[Tedd Knowledge/{source_path.removesuffix('.md')}|", observation_note)
        self.assertNotIn(str(observations[0]["id"]), claim_note.replace(claim_path, ""))
        self.assertNotIn(str(sources[0]["id"]), observation_note)

    def test_entity_links_observations_even_without_current_claims(self) -> None:
        graph = readable_graph(include_claims=False)
        observation = cast(list[dict[str, object]], graph["observations"])[0]
        entity_id = str(cast(list[str], observation["entity_refs"])[0])

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")

        observation_path, _ = note_for(notes, str(observation["id"]))
        _, entity_note = note_for(notes, entity_id)
        self.assertIn("## Recorded observations", entity_note)
        self.assertIn(
            "Tedd Knowledge/" + observation_path.removesuffix(".md"),
            internal_link_targets(entity_note),
        )
        self.assertIn("No current claims are shown here", entity_note)

    def test_entity_connections_precede_recorded_observations(self) -> None:
        graph = readable_graph(include_claims=False, extra_observations=26)
        entities = cast(list[dict[str, object]], graph["entities"])
        harbor = next(item for item in entities if item["namespace"] == "projects")

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, page = note_for(notes, str(harbor["id"]))

        for heading in ("Relationships", "Decisions", "Open commitments"):
            self.assertLess(page.index(f"## {heading}"), page.index("## Recorded observations"))
        self.assertIn("No current claims are shown here", page)

    def test_observation_folds_have_five_visible_rows_and_preserve_selection_bound(self) -> None:
        for count in (1, 5, 6, 27):
            graph = readable_graph(extra_observations=count - 1)
            harbor = next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )
            notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
            _, page = note_for(notes, str(harbor["id"]))
            section = page.split("## Recorded observations", 1)[1].split("\n## ", 1)[0]
            visible = [line for line in section.splitlines() if line.startswith("- [[")]
            folded = [line for line in section.splitlines() if line.startswith("> - [[")]
            with self.subTest(count=count):
                self.assertEqual(len(visible), min(count, 5))
                self.assertEqual(len(folded), max(0, min(count, 25) - 5))
                self.assertEqual("> [!note]- More recorded observations" in section, count > 5)
                self.assertEqual("Summary shortened:" in section, count > 25)
                if count > 25:
                    self.assertGreater(
                        section.index("Summary shortened:"),
                        section.index("> [!note]- More recorded observations"),
                    )

        empty_graph = readable_graph()
        northstar = next(
            item
            for item in cast(list[dict[str, object]], empty_graph["entities"])
            if item["namespace"] == "services"
        )
        empty_notes = kg.build_projection_notes(empty_graph, "review_safe", "Tedd Knowledge")
        _, empty_page = note_for(empty_notes, str(northstar["id"]))
        empty_section = empty_page.split("## Recorded observations", 1)[1].split("\n## ", 1)[0]
        self.assertIn("No recorded observations are shown here", empty_section)
        self.assertNotIn("[!note]", empty_section)

    def test_observation_summaries_are_bounded_without_changing_paths(self) -> None:
        graph = readable_graph(extra_observations=26)
        observations = cast(list[dict[str, object]], graph["observations"])
        observation = observations[0]
        original_notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        original_paths = {
            str(item["id"]): note_for(original_notes, str(item["id"]))[0] for item in observations
        }
        observation["value"] = (
            "A long recorded statement with a newline\n"
            + "[brackets] # heading ^ block %% comment | pipe "
            + "é" * 220
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")

        observation_path, observation_note = note_for(notes, str(observation["id"]))
        self.assertEqual(observation_path, original_paths[str(observation["id"])])
        self.assertIn("display_title:", observation_note)
        self.assertIn("display_summary:", observation_note)
        for line in observation_note.splitlines():
            if line.startswith(("display_title: ", "display_summary: ")):
                value = json.loads(line.split(": ", 1)[1])
                self.assertNotIn("\n", value)
                self.assertLessEqual(len(value), 160)
                self.assertTrue(value.endswith("…"))
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )
        _, harbor_note = note_for(notes, harbor_id)
        observations_section = harbor_note.split("## Recorded observations", 1)[1].split(
            "\n## ", 1
        )[0]
        self.assertIn("Summary shortened:", observations_section)
        for item in observations:
            path, _ = note_for(notes, str(item["id"]))
            self.assertIn(str(item["id"]), original_paths)
            self.assertEqual(path, original_paths[str(item["id"])])

    def test_entity_note_explains_connections_and_preserves_every_relationship_link(self) -> None:
        graph = readable_graph(extra_relations=30)
        entities = cast(list[dict[str, object]], graph["entities"])
        harbor = next(item for item in entities if item["namespace"] == "projects")

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, harbor_note = note_for(notes, str(harbor["id"]))

        self.assertIn("## What Tedd remembers", harbor_note)
        self.assertIn("## Decisions", harbor_note)
        self.assertIn("## Open commitments", harbor_note)
        self.assertIn("## Recent activity", harbor_note)
        self.assertIn("|Mira]] contributes to [[10 Entities/Harbor", harbor_note)
        self.assertIn("|Mira]]", harbor_note)
        self.assertIn("|Harbor]] depends on [[10 Entities/Northstar", harbor_note)
        self.assertIn("|Harbor]] is hosted by [[10 Entities/Northstar", harbor_note)
        self.assertIn("unmapped\\_relation", harbor_note)
        self.assertIn("### Proposed or needs checking", harbor_note)
        self.assertIn("### Resolved", harbor_note)
        self.assertIn("recorded owner: [[Tedd Knowledge/10 Entities/Mira", harbor_note)
        self.assertIn("next action: Review the draft.", harbor_note)
        for index in range(30):
            self.assertIn(f"Service {index:02d}", harbor_note)
        relationship_section = harbor_note.split("## Relationships", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(
            len([line for line in relationship_section.splitlines() if line.startswith("- [[")]),
            5,
        )
        self.assertEqual(
            len([line for line in relationship_section.splitlines() if line.startswith("> - [[")]),
            29,
        )
        self.assertEqual(relationship_section.count("> [!note]- More relationships"), 1)

    def test_entity_activity_status_uses_the_same_sanitizer_as_the_event_note(self) -> None:
        graph = readable_graph()
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )
        private_path = "C:/Users/example/private-review/report.txt"

        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "discussion",
                    "role": "user",
                    "title": "Synthetic status report",
                    "observed_at": "2026-09-20T13:00:00Z",
                    "source_tier": "explicit_user_statement",
                    "sensitive": False,
                },
                "events": [
                    {
                        "kind": "milestone",
                        "title": "Status sanitizer fixture",
                        "summary": "Synthetic event used to verify projection sanitization.",
                        "occurred_at": "2026-09-20T13:00:00Z",
                        "status": private_path,
                        "entity_refs": [harbor_id],
                        "sensitive": False,
                    }
                ],
            },
            authority_ceiling="explicit_user_statement",
        )
        event = next(
            item
            for item in cast(list[dict[str, object]], graph["events"])
            if item.get("title") == "Status sanitizer fixture"
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, harbor_note = note_for(notes, harbor_id)
        _, event_note = note_for(notes, str(event["id"]))

        self.assertNotIn(private_path, harbor_note)
        self.assertIn("redacted local path", harbor_note)
        self.assertNotIn(private_path, event_note)
        self.assertIn("redacted local path", event_note)

    def test_candidate_authority_decisions_and_commitments_need_checking(self) -> None:
        graph = readable_graph()
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )

        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "chat",
                    "role": "assistant",
                    "title": "Assistant-only suggestions",
                    "observed_at": "2026-09-20T13:10:00Z",
                    "sensitive": False,
                },
                "decisions": [
                    {
                        "id": "decision_assistant_suggestion",
                        "title": "Assistant-only proposal",
                        "summary": "This suggestion has not been accepted.",
                        "rationale": "Synthetic authority fixture.",
                        "decided_at": "2026-09-20T13:10:00Z",
                        "status": "accepted",
                        "entity_refs": [harbor_id],
                        "sensitive": False,
                    }
                ],
                "open_loops": [
                    {
                        "id": "loop_assistant_suggestion",
                        "title": "Assistant-only commitment",
                        "summary": "This obligation has not been accepted.",
                        "created_at": "2026-09-20T13:10:00Z",
                        "status": "open",
                        "priority": 1,
                        "entity_refs": [harbor_id],
                        "next_action": "Wait for explicit acceptance.",
                        "terminal_condition": "The owner accepts or declines it.",
                        "sensitive": False,
                    }
                ],
            },
        )
        decision = next(
            item
            for item in cast(list[dict[str, object]], graph["decisions"])
            if item.get("id") == "decision_assistant_suggestion"
        )
        commitment = next(
            item
            for item in cast(list[dict[str, object]], graph["open_loops"])
            if item.get("id") == "loop_assistant_suggestion"
        )
        self.assertEqual(
            (decision["authority_state"], decision["status"]), ("candidate", "proposed")
        )
        self.assertEqual(
            (commitment["authority_state"], commitment["status"]),
            ("candidate", "candidate"),
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, harbor_note = note_for(notes, harbor_id)
        decisions = harbor_note.split("## Decisions", 1)[1].split("## Open commitments", 1)[0]
        commitments = harbor_note.split("## Open commitments", 1)[1].split("## Recent activity", 1)[
            0
        ]

        self.assertIn("### Proposed or needs checking", decisions)
        self.assertIn(
            "Assistant-only proposal", decisions.split("### Proposed or needs checking", 1)[1]
        )
        self.assertNotIn("### Historical\n- [[Tedd Knowledge/14 Decisions/", decisions)
        self.assertIn("### Proposed or needs checking", commitments)
        self.assertIn(
            "Assistant-only commitment",
            commitments.split("### Proposed or needs checking", 1)[1],
        )

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_candidate_conflict_and_quarantine_keep_both_warnings_visible(self) -> None:
        cases = (
            ("conflicting", "Proposed conflict — needs checking"),
            ("quarantined", "Proposed — needs checking"),
        )
        for lifecycle, expected_state in cases:
            with self.subTest(lifecycle=lifecycle):
                graph = readable_graph()
                decision = next(
                    item
                    for item in cast(list[dict[str, object]], graph["decisions"])
                    if item.get("id") == "decision_harbor_candidate"
                )
                decision["authority_state"] = "candidate"
                decision["lifecycle"] = lifecycle

                notes = kg.build_projection_notes(
                    graph,
                    "review_safe",
                    "Tedd Knowledge",
                    skip_validation=True,
                )
                _, decision_note = note_for(notes, str(decision["id"]))
                self.assertIn(f"State: {expected_state}", decision_note)

                yaml = cast(_YamlLoader, import_module("yaml"))
                formulas = cast(
                    dict[str, str],
                    cast(
                        dict[str, object],
                        yaml.safe_load(notes["30 Bases/decisions.base"]),
                    )["formulas"],
                )
                state_formula = formulas["readable_state"]
                self.assertIn(
                    f'if(note.lifecycle == "{lifecycle}", '
                    f'if(note.authority_state == "candidate", "{expected_state}"',
                    state_formula,
                )

    def test_resolved_commitment_shows_outcome_not_an_active_next_action(self) -> None:
        graph = readable_graph()
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")

        _, entity_note = note_for(notes, harbor_id)
        resolved = entity_note.split("### Resolved", 1)[1].split("\n## ", 1)[0]
        self.assertIn("outcome: Draft review recorded.", resolved)
        self.assertNotIn("next action:", resolved)
        self.assertIn("completed: September 18, 2026 at 13:00 UTC", resolved)

    def test_decision_and_commitment_pages_lead_with_status_and_action_or_outcome(self) -> None:
        graph = readable_graph()
        decisions = cast(list[dict[str, object]], graph["decisions"])
        loops = cast(list[dict[str, object]], graph["open_loops"])
        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")

        _, decision = note_for(notes, str(decisions[0]["id"]))
        self.assertLess(decision.index("## Summary"), decision.index("## Status"))
        self.assertLess(decision.index("## Status"), decision.index("## Entities"))
        self.assertLess(decision.index("## Evidence used"), decision.index("## Details"))

        active_record = next(item for item in loops if item["status"] == "open")
        resolved_record = next(item for item in loops if item["status"] == "resolved")
        _, active = note_for(notes, str(active_record["id"]))
        _, resolved_note = note_for(notes, str(resolved_record["id"]))
        self.assertLess(active.index("## Status"), active.index("## Next step"))
        self.assertLess(active.index("## Next step"), active.index("## Details"))
        self.assertLess(resolved_note.index("## Status"), resolved_note.index("## Outcome"))
        self.assertLess(resolved_note.index("## Outcome"), resolved_note.index("## Details"))
        self.assertNotIn("## Next step", resolved_note)

    def test_display_dates_are_deterministic_and_missing_values_are_honest(self) -> None:
        cases = (
            ("2026-09-18T13:00:00Z", "September 18, 2026 at 13:00 UTC"),
            ("2026-09-18T09:00:00-04:00", "September 18, 2026 at 13:00 UTC"),
            ("2026-09-18", "September 18, 2026"),
            (None, "Date not recorded"),
            ("not-a-date", "Date not recorded"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    kg._v3_projection._display_timestamp(value, "Date not recorded"),
                    expected,
                )

    def test_provenance_describes_source_without_claiming_current_operator_identity(self) -> None:
        graph = readable_graph()

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        observation = cast(list[dict[str, object]], graph["observations"])[0]
        _, observation_note = note_for(notes, str(observation["id"]))

        self.assertIn("Recorded from a user", observation_note)
        self.assertNotIn("You told Tedd", observation_note)

    def test_collaborates_on_is_readable_without_changing_direction_or_status(self) -> None:
        graph = readable_graph()
        relation = cast(list[dict[str, object]], graph["relations"])[0]
        relation["type"] = "collaborates_on"
        relation["status"] = "candidate"

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        mira_id = str(relation["source_entity_id"])
        _, mira_note = note_for(notes, mira_id)

        self.assertIn("Mira]] collaborates on [[", mira_note)
        self.assertIn("status: candidate", mira_note)

    def test_obsidian_heading_separator_does_not_enter_generated_paths(self) -> None:
        graph = readable_graph()
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )
        family = "planning #42"
        key = "draft #review"

        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "discussion",
                    "role": "user",
                    "title": "Planning #42",
                    "observed_at": "2026-09-20T13:20:00Z",
                    "source_tier": "explicit_user_statement",
                    "sensitive": False,
                },
                "observations": [
                    {
                        "family": family,
                        "key": key,
                        "value": "The generated links must remain navigable.",
                        "entity_refs": [harbor_id],
                        "observed_at": "2026-09-20T13:20:00Z",
                        "sensitive": False,
                    }
                ],
                "claims": [
                    {
                        "family": family,
                        "key": key,
                        "value": "The generated links must remain navigable.",
                        "entity_refs": [harbor_id],
                        "valid_from": "2026-09-20T13:20:00Z",
                        "sensitive": False,
                    }
                ],
            },
            authority_ceiling="explicit_user_statement",
        )
        observation = next(
            item
            for item in cast(list[dict[str, object]], graph["observations"])
            if item.get("family") == family
        )
        claim = next(
            item
            for item in cast(list[dict[str, object]], graph["claims"])
            if item.get("family") == family
        )
        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        observation_path, observation_note = note_for(notes, str(observation["id"]))
        claim_path, claim_note = note_for(notes, str(claim["id"]))
        source_path = next(
            path
            for path, note in notes.items()
            if path.startswith("20 Evidence/") and "# Planning \\#42" in note
        )

        for path in (observation_path, claim_path, source_path):
            self.assertNotIn("#", path)
        self.assertIn(source_path.removesuffix(".md"), observation_note)
        self.assertIn(observation_path.removesuffix(".md"), claim_note)
        self.assertIn("Planning \\#42", observation_note)

    def test_obsidian_structural_sequences_do_not_break_generated_links(self) -> None:
        cases = (
            ("Notes [[draft]]", ("[[", "]]")),
            ("Notes ^draft", ("^",)),
            ("Notes %%draft%%", ("%%",)),
            ("Notes: draft", (":",)),
            ("Notes | draft", ("|",)),
        )
        for title, forbidden in cases:
            with self.subTest(title=title):
                graph = readable_graph()
                harbor_id = str(
                    next(
                        item
                        for item in cast(list[dict[str, object]], graph["entities"])
                        if item["namespace"] == "projects"
                    )["id"]
                )
                family = title.casefold()
                kg.capture_packet(
                    graph,
                    {
                        "source": {
                            "kind": "discussion",
                            "role": "user",
                            "title": title,
                            "observed_at": "2026-09-20T13:25:00Z",
                            "source_tier": "explicit_user_statement",
                            "sensitive": False,
                        },
                        "observations": [
                            {
                                "family": family,
                                "key": "linked",
                                "value": "The generated evidence link must parse completely.",
                                "entity_refs": [harbor_id],
                                "observed_at": "2026-09-20T13:25:00Z",
                                "sensitive": False,
                            }
                        ],
                    },
                    authority_ceiling="explicit_user_statement",
                )
                validation = kg.validate_graph(graph)
                self.assertTrue(validation["valid"], validation["errors"])
                observation = next(
                    item
                    for item in cast(list[dict[str, object]], graph["observations"])
                    if item.get("family") == family
                )

                notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
                _, observation_note = note_for(notes, str(observation["id"]))
                source_path = next(
                    path
                    for path, note in notes.items()
                    if path.startswith("20 Evidence/") and f"# {kg.markdown_literal(title)}" in note
                )
                for token in forbidden:
                    self.assertNotIn(token, source_path)
                expected_target = f"Tedd Knowledge/{source_path.removesuffix('.md')}"
                self.assertIn(kg.wiki(expected_target, title), observation_note)
                body = observation_note.split("---\n", 2)[-1]
                targets = internal_link_targets(body)
                self.assertEqual(body.count("[["), len(targets))
                self.assertEqual(body.count("]]"), len(targets))
                self.assertIn(expected_target, targets)

    def test_folded_adversarial_wording_cannot_create_links_or_mutate_graph(self) -> None:
        graph = readable_graph(extra_observations=5)
        observations = cast(list[dict[str, object]], graph["observations"])
        observations[-1]["value"] = (
            "> [!warning]- [[unexpected target]] ![[unexpected.png]]\n"
            "Bearer SYNTHETIC_TOKEN at C:/Users/example/private/condition.txt"
        )
        expected = copy.deepcopy(graph)
        harbor = next(
            item
            for item in cast(list[dict[str, object]], graph["entities"])
            if item["namespace"] == "projects"
        )

        for mode in ("review_safe", "operator_full"):
            with self.subTest(mode=mode):
                notes = kg.build_projection_notes(graph, mode, "Tedd Knowledge")
                _, page = note_for(notes, str(harbor["id"]))
                section = page.split("## Recorded observations", 1)[1].split("\n## ", 1)[0]
                targets = internal_link_targets(section)
                self.assertNotIn("unexpected target", targets)
                self.assertNotIn("unexpected.png", targets)
                self.assertEqual(section.count("> [!note]-"), 1)
                if mode == "review_safe":
                    self.assertNotIn("C:/Users/example/private", section)
        self.assertEqual(graph, expected)

    def test_display_properties_cannot_create_obsidian_links(self) -> None:
        cases = (
            (
                "complete",
                "Property source [[draft]]",
                "Review [[draft]] and ![[preview.png]] with Mira.",
            ),
            (
                "embed",
                "Property source ![[scan.png]]",
                "Review ![[preview.png]] before approval.",
            ),
            ("malformed-open", "Property source [[draft", "Review [[draft before approval."),
            ("malformed-close", "Property source draft]]", "Review draft]] before approval."),
            (
                "repeated-brackets",
                "Property source [[[draft]]]",
                "Review [[[draft]]] before approval.",
            ),
            (
                "truncated-delimiters",
                "Property source " + "S" * 145 + "[[draft]]",
                "O" * 151 + "![[preview.png]]",
            ),
        )

        def property_value(note: str, name: str) -> str:
            frontmatter = note.split("---\n", 2)[1]
            line = next(line for line in frontmatter.splitlines() if line.startswith(f"{name}: "))
            value = json.loads(line.split(": ", 1)[1])
            self.assertIsInstance(value, str)
            return cast(str, value)

        for case, source_title, observation_value in cases:
            with self.subTest(case=case):
                graph = readable_graph()
                harbor_id = str(
                    next(
                        item
                        for item in cast(list[dict[str, object]], graph["entities"])
                        if item["namespace"] == "projects"
                    )["id"]
                )
                kg.capture_packet(
                    graph,
                    {
                        "source": {
                            "kind": "discussion",
                            "role": "user",
                            "title": source_title,
                            "observed_at": "2026-09-20T15:00:00Z",
                            "source_tier": "explicit_user_statement",
                            "sensitive": False,
                        },
                        "observations": [
                            {
                                "family": "display_literal",
                                "key": "captured",
                                "value": observation_value,
                                "entity_refs": [harbor_id],
                                "observed_at": "2026-09-20T15:00:00Z",
                                "sensitive": False,
                            }
                        ],
                    },
                    authority_ceiling="explicit_user_statement",
                )
                validation = kg.validate_graph(graph)
                self.assertTrue(validation["valid"], validation["errors"])
                observation = next(
                    item
                    for item in cast(list[dict[str, object]], graph["observations"])
                    if item.get("family") == "display_literal"
                )
                notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
                observation_path, observation_note = note_for(notes, str(observation["id"]))
                source_path, source_note = next(
                    (path, note)
                    for path, note in notes.items()
                    if path.startswith("20 Evidence/")
                    and f"# {kg.markdown_literal(source_title)}" in note
                )
                entity_path, _ = note_for(notes, harbor_id)

                source_display = property_value(source_note, "display_title")
                observation_title = property_value(observation_note, "display_title")
                observation_summary = property_value(observation_note, "display_summary")
                for value in (source_display, observation_title, observation_summary):
                    self.assertNotIn("[[", value)
                    self.assertNotIn("]]", value)
                    self.assertLessEqual(len(value), 160)
                if case == "truncated-delimiters":
                    self.assertTrue(source_display.endswith("…"))
                    self.assertTrue(observation_title.endswith("…"))
                    self.assertTrue(observation_summary.endswith("…"))

                targets = internal_link_targets(observation_note)
                self.assertIn(
                    f"Tedd Knowledge/{source_path.removesuffix('.md')}",
                    targets,
                )
                self.assertIn(
                    entity_path.removesuffix(".md"),
                    targets,
                )
                for target in targets:
                    normalized = target.removeprefix("Tedd Knowledge/").split("#", 1)[0]
                    self.assertTrue(
                        normalized in notes or f"{normalized}.md" in notes,
                        (observation_path, target),
                    )

    def test_hidden_observation_adds_no_artifact_or_reverse_link(self) -> None:
        graph = readable_graph()
        observations = cast(list[dict[str, object]], graph["observations"])
        observation_id = str(observations[0]["id"])
        hidden = copy.deepcopy(graph)
        cast(list[dict[str, object]], hidden["observations"])[0]["sensitive"] = True
        claim = cast(list[dict[str, object]], hidden["claims"])[0]
        claim["observation_refs"] = []

        visible_notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        hidden_notes = kg.build_projection_notes(hidden, "review_safe", "Tedd Knowledge")

        self.assertTrue(any(path.startswith("22 Observations/") for path in visible_notes))
        self.assertFalse(any(path.startswith("22 Observations/") for path in hidden_notes))
        self.assertFalse(any(observation_id in note for note in hidden_notes.values()))

        for mutation in ("missing", "redacted"):
            unavailable = copy.deepcopy(graph)
            unavailable_observation = cast(list[dict[str, object]], unavailable["observations"])[0]
            if mutation == "missing":
                unavailable_observation.pop("sensitive")
            else:
                unavailable_observation["lifecycle"] = "redacted"
                unavailable_observation["active"] = False
            unavailable_notes = kg.build_projection_notes(
                unavailable,
                "review_safe",
                "Tedd Knowledge",
                skip_validation=mutation == "missing",
            )
            with self.subTest(mutation=mutation):
                self.assertFalse(
                    any(path.startswith("22 Observations/") for path in unavailable_notes)
                )

    def test_disconnected_hidden_observation_does_not_change_visible_connections(self) -> None:
        graph = readable_graph()
        hidden = copy.deepcopy(graph)
        source_id = str(cast(list[dict[str, object]], hidden["sources"])[0]["id"])
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], hidden["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )
        cast(list[dict[str, object]], hidden["observations"]).append(
            {
                "id": "observation_private_counterpart",
                "family": "private",
                "key": "counterpart",
                "value": "PRIVATE_COUNTERPART",
                "entity_refs": [harbor_id],
                "source_ref": source_id,
                "observed_at": "2026-09-18T14:01:00Z",
                "source_tier": "explicit_user_statement",
                "lifecycle": "current",
                "sensitive": True,
            }
        )

        visible_notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        hidden_notes = kg.build_projection_notes(hidden, "review_safe", "Tedd Knowledge")
        _, visible_harbor = note_for(visible_notes, harbor_id)
        _, hidden_harbor = note_for(hidden_notes, harbor_id)

        self.assertEqual(hidden_harbor, visible_harbor)
        self.assertEqual(
            hidden_notes["30 Bases/observations.base"],
            visible_notes["30 Bases/observations.base"],
        )
        self.assertNotIn("PRIVATE_COUNTERPART", "\n".join(hidden_notes.values()))

    def test_relationship_requires_ordinary_edge_and_both_ordinary_endpoints(self) -> None:
        graph = readable_graph()
        entities = cast(list[dict[str, object]], graph["entities"])
        relations = cast(list[dict[str, object]], graph["relations"])
        harbor = next(item for item in entities if item["namespace"] == "projects")

        sensitive_edge = copy.deepcopy(graph)
        cast(list[dict[str, object]], sensitive_edge["relations"])[0]["sensitive"] = True
        _, edge_note = note_for(
            kg.build_projection_notes(sensitive_edge, "review_safe", "Tedd Knowledge"),
            str(harbor["id"]),
        )
        self.assertNotIn("contributes to", edge_note)

        sensitive_endpoint = copy.deepcopy(graph)
        endpoint_id = str(relations[1]["target_entity_id"])
        endpoint = next(
            item
            for item in cast(list[dict[str, object]], sensitive_endpoint["entities"])
            if item["id"] == endpoint_id
        )
        endpoint["sensitive"] = True
        _, endpoint_note = note_for(
            kg.build_projection_notes(sensitive_endpoint, "review_safe", "Tedd Knowledge"),
            str(harbor["id"]),
        )
        self.assertNotIn("unmapped\\_relation", endpoint_note)

        missing_sensitivity = copy.deepcopy(graph)
        cast(list[dict[str, object]], missing_sensitivity["relations"])[0].pop("sensitive")
        _, missing_note = note_for(
            kg.build_projection_notes(missing_sensitivity, "review_safe", "Tedd Knowledge"),
            str(harbor["id"]),
        )
        self.assertNotIn("contributes to", missing_note)

    def test_hidden_owner_and_sensitive_records_gain_no_new_navigation(self) -> None:
        graph = readable_graph()
        hidden_owner = copy.deepcopy(graph)
        mira = next(
            item
            for item in cast(list[dict[str, object]], hidden_owner["entities"])
            if item["namespace"] == "people"
        )
        mira["sensitive"] = True
        harbor = next(
            item
            for item in cast(list[dict[str, object]], hidden_owner["entities"])
            if item["namespace"] == "projects"
        )
        hidden_notes = kg.build_projection_notes(hidden_owner, "review_safe", "Tedd Knowledge")
        _, harbor_note = note_for(hidden_notes, str(harbor["id"]))
        self.assertIn("recorded owner: not available in this view", harbor_note)
        self.assertNotIn("contributes to", harbor_note)
        sensitive_path, _ = note_for(hidden_notes, str(mira["id"]))
        _, claim_note = note_for(
            hidden_notes,
            str(cast(list[dict[str, object]], hidden_owner["claims"])[0]["id"]),
        )
        self.assertNotIn(f"[[{sensitive_path.removesuffix('.md')}|", claim_note)

        sensitive = copy.deepcopy(graph)
        claim = cast(list[dict[str, object]], sensitive["claims"])[0]
        decision = cast(list[dict[str, object]], sensitive["decisions"])[0]
        source = cast(list[dict[str, object]], sensitive["sources"])[0]
        for record in (claim, decision, source):
            record["sensitive"] = True
        notes = kg.build_projection_notes(sensitive, "review_safe", "Tedd Knowledge")
        for record_id in (str(claim["id"]), str(decision["id"]), "withheld"):
            _, note = note_for(notes, record_id)
            self.assertNotIn("How to read this view", note)
        _, decision_note = note_for(notes, str(decision["id"]))
        self.assertIn("## Evidence Refs", decision_note)
        self.assertNotIn("## Evidence used", decision_note)

    def test_unclassified_claim_gains_no_readable_display_metadata(self) -> None:
        graph = readable_graph()
        claim = cast(list[dict[str, object]], graph["claims"])[0]
        claim["value"] = "UNCLASSIFIED_PRIVATE_WORDING"
        claim.pop("sensitive")

        notes = kg.build_projection_notes(
            graph,
            "review_safe",
            "Tedd Knowledge",
            skip_validation=True,
        )
        _, claim_note = note_for(notes, str(claim["id"]))
        frontmatter = claim_note.split("---\n", 2)[1]

        self.assertIn('display_title: "Saved record"', frontmatter)
        self.assertIn('display_summary: ""', frontmatter)
        self.assertNotIn("UNCLASSIFIED_PRIVATE_WORDING", frontmatter)

    def test_operator_full_does_not_expand_new_navigation_for_unclassified_records(self) -> None:
        graph = readable_graph()
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )

        for classification in ("sensitive", "missing"):
            hidden = copy.deepcopy(graph)
            observation = cast(list[dict[str, object]], hidden["observations"])[0]
            relation = cast(list[dict[str, object]], hidden["relations"])[0]
            source = cast(list[dict[str, object]], hidden["sources"])[0]
            owner = next(
                item
                for item in cast(list[dict[str, object]], hidden["entities"])
                if item["namespace"] == "people"
            )
            for record in (observation, relation, source, owner):
                if classification == "sensitive":
                    record["sensitive"] = True
                else:
                    record.pop("sensitive")

            notes = kg.build_projection_notes(
                hidden,
                "operator_full",
                "Tedd Knowledge",
                skip_validation=classification == "missing",
            )
            _, harbor_note = note_for(notes, harbor_id)
            _, claim_note = note_for(
                notes,
                str(cast(list[dict[str, object]], hidden["claims"])[0]["id"]),
            )

            with self.subTest(classification=classification):
                self.assertFalse(any(path.startswith("22 Observations/") for path in notes))
                self.assertNotIn("22 Observations/", claim_note)
                self.assertNotIn("contributes to", harbor_note)
                self.assertIn("recorded owner: not available in this view", harbor_note)
                self.assertNotIn("20 Evidence/", harbor_note)

    def test_unavailable_source_never_creates_a_dangling_evidence_link(self) -> None:
        graph = readable_graph()
        observation = cast(list[dict[str, object]], graph["observations"])[0]
        graph["sources"] = []

        notes = kg.build_projection_notes(
            graph,
            "review_safe",
            "Tedd Knowledge",
            skip_validation=True,
        )
        _, observation_note = note_for(notes, str(observation["id"]))

        self.assertIn("Evidence is not available in this view", observation_note)
        self.assertNotIn("20 Evidence/", observation_note)

    def test_same_named_entities_have_distinct_paths_and_markdown_is_escaped(self) -> None:
        graph = readable_graph()
        relations = cast(list[dict[str, object]], graph["relations"])
        relations[1]["type"] = "untrusted_[label]"

        notes = kg.build_projection_notes(
            graph,
            "review_safe",
            "Tedd Knowledge",
            skip_validation=True,
        )
        mira_paths = sorted(path for path in notes if path.startswith("10 Entities/Mira "))
        self.assertEqual(len(mira_paths), 2)
        self.assertNotEqual(mira_paths[0].casefold(), mira_paths[1].casefold())
        harbor_note = next(
            note for path, note in notes.items() if path.startswith("10 Entities/Harbor ")
        )
        self.assertIn(r"untrusted\_\[label\]", harbor_note)

    def test_replacement_links_history_and_redaction_removes_connections(self) -> None:
        graph = readable_graph()
        harbor = next(
            item
            for item in cast(list[dict[str, object]], graph["entities"])
            if item["namespace"] == "projects"
        )
        mira = next(
            item
            for item in cast(list[dict[str, object]], graph["entities"])
            if item["namespace"] == "people"
        )
        with mock.patch.object(kg, "utc_now", autospec=True, return_value=FIXED_NOW):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "discussion",
                        "role": "user",
                        "title": "Updated planning discussion",
                        "observed_at": "2026-09-19T14:00:00Z",
                        "source_tier": "explicit_user_statement",
                        "sensitive": False,
                    },
                    "claims": [
                        {
                            "family": "planning",
                            "key": "draft_review",
                            "value": "Mira completed the draft review",
                            "entity_refs": [mira["id"], harbor["id"]],
                            "subject_refs": [mira["id"], harbor["id"]],
                            "valid_from": "2026-09-19T14:00:00Z",
                            "conflict_policy": "replace",
                            "sensitive": False,
                        }
                    ],
                },
                authority_ceiling="explicit_user_statement",
                allow_replace=True,
            )
        claims = cast(list[dict[str, object]], graph["claims"])
        historical = next(item for item in claims if item["lifecycle"] == "superseded")
        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, historical_note = note_for(notes, str(historical["id"]))
        self.assertIn("Replaced by: [[", historical_note)
        _, harbor_note = note_for(notes, str(harbor["id"]))
        self.assertIn("### Historical or replaced", harbor_note)

        kg.redact_record(graph, str(historical["id"]))
        redacted_notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, redacted_harbor_note = note_for(redacted_notes, str(harbor["id"]))
        self.assertNotIn("Mira will review the draft", redacted_harbor_note)

    def test_only_redacted_claims_are_exempt_from_surface_semantic_matching(self) -> None:
        malformed = readable_graph()
        cast(list[dict[str, object]], malformed["claims"])[0]["family"] = "different"
        self.assertIn(
            "family differs from claim surface",
            "\n".join(cast(list[str], kg.validate_graph(malformed)["errors"])),
        )

        redacted = readable_graph()
        claim_id = str(cast(list[dict[str, object]], redacted["claims"])[0]["id"])
        kg.redact_record(redacted, claim_id)
        self.assertEqual(kg.validate_graph(redacted)["errors"], [])

    def test_projection_never_invents_a_person_mentioned_only_in_evidence(self) -> None:
        graph = readable_graph()
        source = cast(list[dict[str, object]], graph["sources"])[0]
        source["excerpt"] = "Nora attended, but no reviewed person record was captured."

        before = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        self.assertFalse(any("Nora" in path for path in before))

        with mock.patch.object(kg, "utc_now", autospec=True, return_value=FIXED_NOW):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "discussion",
                        "role": "user",
                        "title": "Corrected stakeholder capture",
                        "observed_at": "2026-09-20T12:00:00Z",
                        "source_tier": "explicit_user_statement",
                        "sensitive": False,
                    },
                    "entities": [
                        {
                            "ref": "nora",
                            "type": "person",
                            "namespace": "people",
                            "name": "Nora",
                            "sensitive": False,
                        }
                    ],
                },
                authority_ceiling="explicit_user_statement",
            )
        after = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        self.assertTrue(any(path.startswith("10 Entities/Nora ") for path in after))

    def test_note_backed_summary_is_bounded_but_child_notes_remain_reachable(self) -> None:
        graph = readable_graph(extra_activity=30)
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        _, harbor_note = note_for(notes, harbor_id)

        self.assertIn("Summary shortened:", harbor_note)
        for index in range(30):
            _, receipt_note = note_for(notes, f"receipt_activity_{index:02d}")
            self.assertIn("Harbor", receipt_note)

    def test_dashboard_reports_selected_snapshot_metadata_and_keeps_help_anchor(self) -> None:
        graph = readable_graph()
        anchor = cast(dict[str, object], graph["journal_anchor"])
        graph["projection_hot_set"] = {
            "strategy": "priority_hot_set_with_reference_closure",
            "canonical_record_count": 8000,
            "root_record_count": 100,
            "projected_record_count": 7500,
            "record_limit": 7500,
            "complete": False,
        }

        notes = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        dashboard = notes["00 Dashboard.md"]

        self.assertIn("# Tedd Knowledge", dashboard)
        self.assertIn(f"Saved database revision: {anchor['watermark']}", dashboard)
        self.assertIn("Selection: bounded", dashboard)
        self.assertIn("## How to read this view", dashboard)
        entity_note = next(note for path, note in notes.items() if path.startswith("10 Entities/"))
        self.assertNotIn("[[Tedd Knowledge/00 Dashboard#How to read this view|", entity_note)

        complete = copy.deepcopy(graph)
        cast(dict[str, object], complete["projection_hot_set"])["complete"] = True
        complete_dashboard = kg.build_projection_notes(complete, "review_safe", "Tedd Knowledge")[
            "00 Dashboard.md"
        ]
        self.assertIn("Selection: complete", complete_dashboard)

        legacy = copy.deepcopy(graph)
        legacy.pop("projection_hot_set")
        legacy_dashboard = kg.build_projection_notes(legacy, "review_safe", "Tedd Knowledge")[
            "00 Dashboard.md"
        ]
        self.assertIn("Selection: not reported", legacy_dashboard)

    def test_dashboard_starts_with_reader_tasks_before_diagnostics(self) -> None:
        dashboard = kg.build_projection_notes(readable_graph(), "review_safe", "Tedd Knowledge")[
            "00 Dashboard.md"
        ]

        headings = [
            "## People and projects",
            "## Needs attention",
            "## Recent decisions",
            "## Recently recorded",
            "## Snapshot details",
        ]
        positions = [dashboard.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(dashboard.index("## People and projects"), dashboard.index("## Tables"))
        snapshot = dashboard_section(dashboard, "Snapshot details")
        self.assertIn("> [!info]- Technical snapshot details", snapshot)
        active = dashboard_section(dashboard, "Active Open Loops")
        self.assertIn("[[30 Bases/open_loops.base|Open commitments table]]", active)
        self.assertNotIn("[[Tedd Knowledge/13 Open Loops/", active)
        resolved = dashboard_section(dashboard, "Recently Resolved Loops")
        self.assertIn("[[30 Bases/open_loops.base|Completed work table]]", resolved)
        self.assertNotIn("[[Tedd Knowledge/13 Open Loops/", resolved)

    def test_dashboard_separates_active_obligations_from_proposals(self) -> None:
        dashboard = kg.build_projection_notes(readable_graph(), "review_safe", "Tedd Knowledge")[
            "00 Dashboard.md"
        ]

        attention = dashboard_section(dashboard, "Needs attention")
        active = attention.split("### Active obligations", 1)[1].split(
            "### Proposals and conflicts", 1
        )[0]
        proposals = attention.split("### Proposals and conflicts", 1)[1]
        self.assertIn("Finish the Harbor review", active)
        self.assertNotIn("Consider another reviewer", active)
        self.assertIn("Consider another reviewer", proposals)

    def test_hidden_newer_records_do_not_displace_visible_dashboard_navigation(self) -> None:
        graph = readable_graph()
        before = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")[
            "00 Dashboard.md"
        ]
        headings = ("Needs attention", "Recent decisions", "Recently recorded")
        before_sections = {heading: dashboard_section(before, heading) for heading in headings}
        harbor_id = str(
            next(
                item
                for item in cast(list[dict[str, object]], graph["entities"])
                if item["namespace"] == "projects"
            )["id"]
        )
        with mock.patch.object(kg, "utc_now", autospec=True, return_value=FIXED_NOW):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "discussion",
                        "role": "user",
                        "title": "Private dashboard noninterference fixture",
                        "observed_at": "2026-09-20T14:00:00Z",
                        "source_tier": "explicit_user_statement",
                        "sensitive": True,
                    },
                    "observations": [
                        {
                            "family": "privacy_control",
                            "key": f"hidden_{index}",
                            "value": f"Private observation {index}",
                            "entity_refs": [harbor_id],
                            "observed_at": f"2026-09-20T14:{index:02d}:00Z",
                            "sensitive": True,
                        }
                        for index in range(12)
                    ],
                    "decisions": [
                        {
                            "id": f"decision_hidden_{index}",
                            "title": f"Private proposal {index}",
                            "summary": "Private proposal.",
                            "decided_at": f"2026-09-20T15:{index:02d}:00Z",
                            "status": "candidate",
                            "entity_refs": [harbor_id],
                            "sensitive": True,
                        }
                        for index in range(12)
                    ],
                    "open_loops": [
                        {
                            "id": f"loop_hidden_{index}",
                            "title": f"Private obligation {index}",
                            "summary": "Private obligation.",
                            "created_at": f"2026-09-20T16:{index:02d}:00Z",
                            "status": "open",
                            "priority": 1,
                            "entity_refs": [harbor_id],
                            "next_action": "Keep private.",
                            "sensitive": True,
                        }
                        for index in range(12)
                    ],
                },
                authority_ceiling="explicit_user_statement",
            )

        after = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")["00 Dashboard.md"]
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertEqual(before_sections[heading], dashboard_section(after, heading))

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_tables_show_meaning_before_internal_keys(self) -> None:
        root_name = 'Tedd "Knowledge" #1'
        notes = kg.build_projection_notes(readable_graph(), "review_safe", root_name)
        expression = "link(file.path, if(note.display_title, note.display_title, file.name))"

        for path, content in notes.items():
            if path.endswith(".base"):
                with self.subTest(path=path):
                    self.assertIn(f"  readable_note: {expression}\n", content)
                    self.assertNotIn(f"  readable_note: '{expression}'", content)

        expected_folders = {
            "entities.base": f"{root_name}/10 Entities",
            "claims.base": f"{root_name}/11 Claims",
            "observations.base": f"{root_name}/22 Observations",
            "events.base": f"{root_name}/12 Incidents and Events",
            "open_loops.base": f"{root_name}/13 Open Loops",
            "decisions.base": f"{root_name}/14 Decisions",
        }
        yaml = cast(_YamlLoader, import_module("yaml"))
        for filename, folder in expected_folders.items():
            with self.subTest(filename=filename):
                document = cast(dict[str, object], yaml.safe_load(notes[f"30 Bases/{filename}"]))
                filters = cast(dict[str, list[str]], document["filters"])
                self.assertEqual(
                    filters["and"][0],
                    f"file.inFolder({json.dumps(folder)})",
                )

        observations = notes["30 Bases/observations.base"]
        self.assertNotIn("      - display_summary", observations)
        self.assertNotIn("      - observation_key", observations)

        expected = {
            "entities.base": (
                [
                    "formula.readable_note",
                    "entity_type",
                    "display_context",
                    "formula.readable_state",
                ],
                ["Name", "Kind", "Context", "State"],
            ),
            "claims.base": (
                [
                    "formula.readable_note",
                    "about",
                    "formula.readable_state",
                    "valid_from",
                    "formula.readable_source",
                ],
                ["Statement", "About", "State", "Since", "Source"],
            ),
            "observations.base": (
                [
                    "formula.readable_note",
                    "about",
                    "observed_at",
                    "formula.readable_source",
                ],
                ["Recorded wording", "About", "Observed", "Source"],
            ),
            "decisions.base": (
                [
                    "formula.readable_note",
                    "about",
                    "formula.readable_state",
                    "decided_at",
                ],
                ["Decision", "About", "State", "Decided"],
            ),
            "events.base": (
                [
                    "formula.readable_note",
                    "about",
                    "formula.readable_state",
                    "occurred_at",
                ],
                ["Event", "About", "State", "When"],
            ),
        }
        for filename, (order, labels) in expected.items():
            path = f"30 Bases/{filename}"
            with self.subTest(path=path):
                document = cast(dict[str, object], yaml.safe_load(notes[path]))
                views = cast(list[dict[str, object]], document["views"])
                properties = cast(dict[str, dict[str, str]], document["properties"])
                self.assertEqual(views[0]["order"], order)
                metadata_order = [
                    property_name
                    if property_name.startswith("formula.")
                    else f"note.{property_name}"
                    for property_name in order
                ]
                self.assertEqual(set(properties), set(metadata_order))
                self.assertEqual(
                    [properties[property_name]["displayName"] for property_name in metadata_order],
                    labels,
                )

        commitment_document = cast(
            dict[str, object], yaml.safe_load(notes["30 Bases/open_loops.base"])
        )
        commitment_views = cast(list[dict[str, object]], commitment_document["views"])
        commitment_properties = cast(dict[str, dict[str, str]], commitment_document["properties"])
        commitment_expected = (
            (
                "Active",
                [
                    "formula.active_title",
                    "owners",
                    "next_action",
                    "checkpoint_at",
                    "due_at",
                ],
                ["Commitment", "Owner", "Next step", "Check-in", "Due"],
            ),
            (
                "Candidates",
                [
                    "formula.candidate_title",
                    "owners",
                    "formula.candidate_next_action",
                    "checkpoint_at",
                ],
                ["Proposal", "Owner", "Proposed step", "Check-in"],
            ),
            (
                "Resolved",
                [
                    "formula.resolved_title",
                    "owners",
                    "resolution",
                    "resolved_at",
                ],
                ["Completed work", "Owner", "Outcome", "Completed"],
            ),
        )
        for view, (name, order, labels) in zip(commitment_views, commitment_expected, strict=True):
            with self.subTest(view=name):
                self.assertEqual(view["name"], name)
                self.assertEqual(view["order"], order)
                metadata_order = [
                    property_name
                    if property_name.startswith("formula.")
                    else f"note.{property_name}"
                    for property_name in order
                ]
                self.assertEqual(
                    [
                        commitment_properties[property_name]["displayName"]
                        for property_name in metadata_order
                    ],
                    labels,
                )
        self.assertEqual(
            set(commitment_properties),
            {
                property_name if property_name.startswith("formula.") else f"note.{property_name}"
                for _, order, _ in commitment_expected
                for property_name in order
            },
        )

        formulas = cast(
            dict[str, str],
            cast(dict[str, object], yaml.safe_load(notes["30 Bases/decisions.base"]))["formulas"],
        )
        self.assertLess(
            formulas["readable_state"].index('note.authority_state == "candidate"'),
            formulas["readable_state"].index("note.status"),
        )
        source_formulas = cast(
            dict[str, str],
            cast(dict[str, object], yaml.safe_load(observations))["formulas"],
        )
        self.assertIn(
            'if(note.source_tier, note.source_tier, "Unknown source")',
            source_formulas["readable_source"],
        )
        self.assertNotIn("verified", source_formulas["readable_source"].casefold())
        commitment_formulas = cast(dict[str, str], commitment_document["formulas"])
        self.assertEqual(commitment_formulas["candidate_next_action"], "note.next_action")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_base_filter_serialization_is_native_stable_and_special_root_safe(self) -> None:
        yaml = cast(_YamlLoader, import_module("yaml"))
        cases = (
            ("Tedd Knowledge/11 Claims", False),
            ("Tedd #1/11 Claims", True),
            ("Tedd: Reading/11 Claims", True),
            ('Tedd "Quoted"/11 Claims', True),
        )

        for folder, needs_yaml_quotes in cases:
            expression = f"file.inFolder({json.dumps(folder)})"
            expected_scalar = (
                json.dumps(expression, ensure_ascii=False) if needs_yaml_quotes else expression
            )
            for name, content in (
                ("ordinary", kg.base_file("Claims", folder, "claim", ["about"])),
                ("commitment", kg.loop_base_file(folder)),
            ):
                with self.subTest(folder=folder, kind=name):
                    self.assertIn(f"    - {expected_scalar}\n", content)
                    document = cast(dict[str, object], yaml.safe_load(content))
                    filters = cast(dict[str, list[str]], document["filters"])
                    self.assertEqual(filters["and"][0], expression)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_native_state_expression_is_balanced(self) -> None:
        notes = kg.build_projection_notes(readable_graph(), "review_safe", "Tedd Knowledge")
        yaml = cast(_YamlLoader, import_module("yaml"))
        document = cast(dict[str, object], yaml.safe_load(notes["30 Bases/decisions.base"]))
        formulas = cast(dict[str, str], document["formulas"])
        expected_state = (
            'if(note.lifecycle == "conflicting", '
            'if(note.authority_state == "candidate", "Proposed conflict — needs checking", '
            '"Conflict — needs checking"), '
            'if(note.lifecycle == "quarantined", '
            'if(note.authority_state == "candidate", "Proposed — needs checking", '
            '"Needs checking"), '
            'if(note.authority_state == "candidate", "Proposed or needs checking", '
            'if(note.lifecycle, note.lifecycle, if(note.status, note.status, "Unknown")))))'
        )

        self.assertEqual(formulas["readable_state"], expected_state)
        self.assertEqual(expected_state.count("("), expected_state.count(")"))

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_entity_table_context_is_selected_readable_and_nonidentifying(self) -> None:
        graph = readable_graph()
        entities = cast(list[dict[str, object]], graph["entities"])
        harbor = next(item for item in entities if item["namespace"] == "projects")
        literal_mira = next(item for item in entities if item["namespace"] == "people")
        contextual_mira = next(item for item in entities if item["namespace"] == "vendors")
        northstar = next(item for item in entities if item["namespace"] == "services")
        hidden_context = {
            "id": "entity_hidden_context_7c4ea1",
            "type": "project",
            "namespace": "projects",
            "name": "Restricted program",
            "aliases": [],
            "identity_keys": [],
            "status": "active",
            "lifecycle": "active",
            "source_tier": "explicit_user_statement",
            "sensitive": True,
            "evidence_refs": [],
        }
        hidden_member = {
            "id": "entity_visible_member_2f126b",
            "type": "person",
            "namespace": hidden_context["id"],
            "name": "Taylor",
            "aliases": [],
            "identity_keys": [],
            "status": "active",
            "lifecycle": "active",
            "source_tier": "explicit_user_statement",
            "sensitive": False,
            "evidence_refs": [],
        }
        entities.extend([hidden_context, hidden_member])
        contextual_mira["namespace"] = harbor["id"]
        northstar["namespace"] = "project_omitted_context_381ceb"

        notes = kg.build_projection_notes(
            graph,
            "review_safe",
            "Tedd Knowledge",
            skip_validation=True,
        )
        yaml = cast(_YamlLoader, import_module("yaml"))

        def context_for(record: dict[str, object]) -> str:
            _, note = note_for(notes, str(record["id"]))
            frontmatter_text = note.split("---", 2)[1]
            metadata = cast(dict[str, object], yaml.safe_load(frontmatter_text))
            return cast(str, metadata["display_context"])

        self.assertEqual(context_for(literal_mira), "Context not shown")
        self.assertEqual(context_for(contextual_mira), "Harbor")
        self.assertEqual(context_for(northstar), "Context not shown")
        self.assertEqual(context_for(hidden_member), "Context not shown")
        hidden_contexts = " ".join(context_for(item) for item in (northstar, hidden_member))
        self.assertNotIn("project_omitted_context_381ceb", hidden_contexts)
        self.assertNotIn("entity_", hidden_contexts)
        mira_paths = {
            note_for(notes, str(item["id"]))[0] for item in (literal_mira, contextual_mira)
        }
        self.assertEqual(len(mira_paths), 2)

    def test_record_notes_do_not_create_a_universal_help_hub(self) -> None:
        notes = kg.build_projection_notes(readable_graph(), "review_safe", "Tedd Knowledge")
        self.assertIn("## How to read this view", notes["00 Dashboard.md"])
        for path, content in notes.items():
            if path == "00 Dashboard.md" or path.startswith("30 Bases/"):
                continue
            with self.subTest(path=path):
                self.assertNotIn("00 Dashboard#How to read this view", content)

    def test_reordered_input_produces_identical_output(self) -> None:
        graph = readable_graph(extra_relations=3)
        reordered = copy.deepcopy(graph)
        for collection in kg.COLLECTIONS:
            value = reordered.get(collection)
            if isinstance(value, list):
                value.reverse()

        self.assertEqual(
            kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge"),
            kg.build_projection_notes(reordered, "review_safe", "Tedd Knowledge"),
        )

    def test_every_emitted_internal_link_resolves_in_the_selected_snapshot(self) -> None:
        notes = kg.build_projection_notes(
            readable_graph(extra_relations=3), "review_safe", "Tedd Knowledge"
        )
        self.assertEqual(len({path.casefold() for path in notes}), len(notes))

        missing: list[tuple[str, str]] = []
        for source_path, content in notes.items():
            for target in internal_link_targets(content):
                original_target = target
                if target.startswith("Tedd Knowledge/"):
                    target = target.removeprefix("Tedd Knowledge/")
                target_path, _, heading = target.partition("#")
                candidates = (target_path, f"{target_path}.md")
                if not any(candidate in notes for candidate in candidates):
                    missing.append((source_path, original_target))
                    continue
                if heading:
                    resolved = notes.get(target_path, notes.get(f"{target_path}.md", ""))
                    if f"## {heading}" not in resolved:
                        missing.append((source_path, original_target))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
