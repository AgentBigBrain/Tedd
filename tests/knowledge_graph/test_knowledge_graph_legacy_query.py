#!/usr/bin/env python3
"""Characterize the typed legacy query service and its flat facade."""

from __future__ import annotations

import hashlib
import json
import unittest
from typing import cast
from unittest import mock

import knowledge_graph as kg  # noqa: E402
from tedd_knowledge_graph import legacy_query  # noqa: E402


def object_records(value: object, *, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError(f"{label} must be a list of objects")
    return cast(list[dict[str, object]], value)


def graph_with_changes() -> dict[str, object]:
    graph = kg.blank_graph()
    object_records(graph.get("entities"), label="Graph entities").extend(
        [
            {
                "id": "entity_alpha",
                "name": "Alpha Service",
                "aliases": ["Alpha"],
                "namespace": "global",
                "lifecycle": "current",
                "evidence_refs": [],
                "sensitive": False,
            },
            {
                "id": "entity_beta",
                "name": "Beta Worker",
                "aliases": [],
                "namespace": "global",
                "lifecycle": "current",
                "evidence_refs": [],
                "sensitive": False,
            },
        ]
    )
    kg.append_journal_entry(
        graph,
        {
            "id": "journal_alpha",
            "action": "capture",
            "changed_ids": ["entity_alpha"],
        },
    )
    kg.append_journal_entry(
        graph,
        {
            "id": "journal_beta",
            "action": "capture",
            "changed_ids": ["entity_beta"],
        },
    )
    return graph


class LegacyQueryServiceTests(unittest.TestCase):
    def test_recall_facade_matches_typed_service(self) -> None:
        graph = graph_with_changes()
        dependencies = kg._legacy_query_dependencies()

        facade = kg.recall_graph(
            graph,
            "Alpha",
            10,
            12_000,
            skip_validation=True,
        )
        service = legacy_query.recall_graph(
            graph,
            "Alpha",
            10,
            12_000,
            "review_safe",
            candidate_ids=None,
            skip_validation=True,
            backend="scan",
            since_watermark=None,
            dependencies=dependencies,
        )

        self.assertEqual(facade, service)
        self.assertEqual(
            [
                result["id"]
                for result in object_records(facade.get("results"), label="Recall results")
            ],
            ["entity_alpha"],
        )
        self.assertEqual(
            [
                (item["id"], item["score"], item["reasons"])
                for item in object_records(service.get("results"), label="Recall results")
            ],
            [
                (
                    "entity_alpha",
                    14.5,
                    ["exact_phrase", "token_overlap:2", "seed_entity"],
                )
            ],
        )
        serialized = json.dumps(
            service,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(serialized).hexdigest(),
            "5b778b979c03d34c35140bb6446667554cffcc6d96bcafeed9adba4cf8a3aca7",
        )

    def test_recall_character_budget_preserves_exact_empty_response(self) -> None:
        graph = graph_with_changes()
        result = legacy_query.recall_graph(
            graph,
            "",
            10,
            200,
            "review_safe",
            candidate_ids={"entity_alpha", "entity_beta"},
            skip_validation=True,
            backend="scan",
            since_watermark=0,
            dependencies=kg._legacy_query_dependencies(),
        )

        self.assertEqual(result["result_count"], 0)
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(serialized).hexdigest(),
            "bb54bd3f6c91331a52c8b25c9ddf2de2d491a516f5573718e39af6d874cc15ea",
        )

    def test_change_feed_and_cursor_match_typed_service(self) -> None:
        graph = graph_with_changes()
        dependencies = kg._legacy_query_dependencies()

        facade = kg.change_feed_since(graph, 0, limit=1)
        service = legacy_query.change_feed_since(
            graph,
            0,
            through_watermark=None,
            offset=0,
            limit=1,
            candidate_ids=None,
            mode="review_safe",
            cursor=None,
            dependencies=dependencies,
        )

        self.assertEqual(facade, service)
        next_cursor = facade.get("next_cursor")
        self.assertIsInstance(next_cursor, str)
        if not isinstance(next_cursor, str):
            raise AssertionError("Change-feed cursor must be a string")
        continuation = kg.change_feed_since(
            graph,
            0,
            limit=1,
            cursor=next_cursor,
        )
        self.assertTrue(continuation["complete"])
        changes = object_records(continuation.get("changes"), label="Change-feed changes")
        self.assertEqual(changes[0]["record_id"], "entity_beta")

    def test_query_hooks_resolve_from_facade_at_call_time(self) -> None:
        graph = graph_with_changes()
        original = kg.canonical_entity_id
        with mock.patch.object(
            kg,
            "canonical_entity_id",
            wraps=original,
        ) as canonical_entity_id:
            kg.recall_graph(
                graph,
                "entity_alpha",
                10,
                12_000,
                skip_validation=True,
            )

        canonical_entity_id.assert_called_with(graph, "entity_alpha")

    def test_validation_result_is_narrowed_at_dynamic_boundary(self) -> None:
        graph = graph_with_changes()
        with mock.patch.object(
            kg,
            "validate_graph",
            return_value={"valid": "yes", "errors": []},
        ):
            with self.assertRaisesRegex(TypeError, "Graph validation must return"):
                kg.recall_graph(graph, "Alpha", 10, 12_000)

    def test_redaction_hook_resolves_from_facade_at_call_time(self) -> None:
        record: dict[str, object] = {"id": "entity_alpha", "name": "Alpha"}
        replacement = {"id": "entity_alpha", "name": "hooked"}
        with mock.patch.object(
            kg,
            "scrub_value",
            return_value=(replacement, 0),
        ) as scrub_value:
            recalled = kg.recall_record(record, "review_safe")

        scrub_value.assert_called_once_with(record, redact_paths=True)
        self.assertEqual(recalled, replacement)


if __name__ == "__main__":
    unittest.main()
