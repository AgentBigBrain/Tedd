#!/usr/bin/env python3
"""Characterize typed legacy migration/store services behind the flat facade."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge_graph as kg  # noqa: E402
from tedd_knowledge_graph import legacy_migrations, legacy_store  # noqa: E402


class LegacyStoreServiceTests(unittest.TestCase):
    def test_flat_schema_exports_have_one_internal_source(self) -> None:
        self.assertEqual(kg.GRAPH_SCHEMA_VERSION, legacy_migrations.GRAPH_SCHEMA_VERSION)
        self.assertIs(kg.SOURCE_TIERS, legacy_migrations.SOURCE_TIERS)
        self.assertIs(kg.COLLECTIONS, legacy_migrations.COLLECTIONS)

    def test_missing_store_resolves_facade_blank_factory_at_call_time(self) -> None:
        graph = legacy_store.blank_graph(clock=lambda: "2026-08-03T12:00:00Z")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(kg, "blank_graph", return_value=graph) as blank_graph,
        ):
            loaded, digest, stored_version = kg.load_graph_with_digest(
                Path(temporary) / "missing.json"
            )

        expected = (
            json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        )
        self.assertIs(loaded, graph)
        self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
        self.assertEqual(stored_version, kg.GRAPH_SCHEMA_VERSION)
        blank_graph.assert_called_once_with()

    def test_save_resolves_facade_clock_and_writer_at_call_time(self) -> None:
        graph = kg.blank_graph()
        writes: list[tuple[Path, str]] = []
        destination = Path("unused.json")

        with (
            mock.patch.object(
                kg,
                "utc_now",
                return_value="2026-08-03T13:00:00Z",
            ),
            mock.patch.object(
                kg,
                "atomic_write",
                side_effect=lambda path, content: writes.append((path, content)),
            ),
        ):
            digest = kg.save_graph(destination, graph)

        self.assertEqual(graph["updated_at"], "2026-08-03T13:00:00Z")
        self.assertEqual(writes[0][0], destination)
        self.assertEqual(digest, hashlib.sha256(writes[0][1].encode()).hexdigest())

    def test_v2_migration_facade_is_deterministic_and_non_mutating(self) -> None:
        raw: dict[str, object] = {
            "schema_version": "v2",
            "updated_at": "2026-08-03T12:00:00Z",
            "entities": [],
            "claims": [],
            "journal": [],
        }

        first = kg.migrate_graph_v2_to_v3(raw)
        second = legacy_migrations.migrate_graph_v2_to_v3(raw)

        self.assertEqual(first, second)
        self.assertEqual(raw["schema_version"], "v2")


if __name__ == "__main__":
    unittest.main()
