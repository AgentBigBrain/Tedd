#!/usr/bin/env python3
"""Contract and byte-parity tests for the legacy v3 projection adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from typing import cast
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

SCRIPT = SCRIPT_DIRECTORY / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_projection_target", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)

FIXED_NOW = "2026-08-03T12:00:00+00:00"
EXPECTED_PROJECTION_SHA256 = {
    # Reviewed snapshots include navigable ordinary connections, observations,
    # status-grouped entity summaries and the dashboard reading contract. The
    # review-safe fixture still withholds the ambiguous Windows path.
    # Operator-full preserves permitted legacy full-note values and paths, while
    # every new navigation surface still requires explicit non-sensitive status.
    "review_safe": "ab2a90eab0aa35c46267e5d13e024378f239cf075bd8ef5632ff2c707171eb5a",
    "operator_full": "e4a4b1404c6910a3ab3475e74b09c4d9ab6e4b5a67a6530f1e47c238234665c1",
}


def representative_graph() -> dict[str, object]:
    with mock.patch.object(kg, "utc_now", autospec=True, return_value=FIXED_NOW):
        graph: dict[str, object] = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Primary evidence",
                    "observed_at": "2026-08-01T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                    "uri": "https://example.test/evidence",
                    "excerpt": "Verified C:\\Private\\artifact and `markup`.",
                },
                "entities": [
                    {
                        "ref": "service",
                        "type": "service",
                        "namespace": "example",
                        "name": "Service [One]",
                        "aliases": ["Service One"],
                    },
                    {
                        "ref": "secret",
                        "type": "component",
                        "namespace": "example",
                        "name": "Secret Component",
                        "sensitive": True,
                    },
                ],
                "relations": [
                    {
                        "source": "service",
                        "target": "secret",
                        "type": "contains",
                        "status": "current",
                    }
                ],
                "claims": [
                    {
                        "family": "deployment",
                        "key": "version",
                        "value": "2.0",
                        "entity_refs": ["service"],
                        "valid_from": "2026-08-01T12:00:00Z",
                    }
                ],
                "events": [
                    {
                        "kind": "release",
                        "title": "Release `two`",
                        "summary": "Shipped safely.",
                        "occurred_at": "2026-08-01T13:00:00Z",
                        "status": "complete",
                        "entity_refs": ["service"],
                    }
                ],
                "decisions": [
                    {
                        "id": "decision_projection",
                        "title": "Keep projection stable",
                        "summary": "Preserve exact bytes.",
                        "rationale": "Compatibility.",
                        "decided_at": "2026-08-01T14:00:00Z",
                        "status": "accepted",
                        "entity_refs": ["service"],
                    }
                ],
                "open_loops": [
                    {
                        "id": "loop_projection",
                        "title": "Verify projection",
                        "summary": "Run focused tests.",
                        "created_at": "2026-08-01T15:00:00Z",
                        "status": "open",
                        "priority": 2,
                        "owner_refs": ["service"],
                        "next_action": "Compare output.",
                        "checkpoint_at": "2026-08-02T12:00:00Z",
                        "terminal_condition": "Hashes match.",
                        "entity_refs": ["service"],
                    }
                ],
                "receipts": [
                    {
                        "kind": "verification",
                        "title": "Projection receipt",
                        "summary": "Known bytes verified.",
                        "verified_at": "2026-08-01T16:00:00Z",
                        "verified": True,
                        "uri": "https://example.test/receipt",
                        "entity_refs": ["service"],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
    return graph


class KnowledgeGraphV3ProjectionAdapterTests(unittest.TestCase):
    def test_owner_source_notes_add_real_links_without_unmasking_ordinary_notes(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceExcerpt, SourceRef, SourceUnit
        from tedd_knowledge_graph.source_presentation import PreparedSourceView

        graph = representative_graph()
        claims = cast(list[dict[str, object]], graph["claims"])
        ref = SourceRef("a" * 32, "b" * 64, "c" * 64, "u1")
        excerpt = SourceExcerpt(
            ref,
            (SourceUnit("u1", "Only after review.", 1),),
            0,
            (),
            0,
            "complete",
            "original",
            "d" * 64,
        )
        view = PreparedSourceView((str(claims[0]["id"]),), excerpt, "Résumé [source]")
        before = json.dumps(graph, sort_keys=True)
        ordinary = kg.build_projection_notes(graph, "review_safe", "Tedd Knowledge")
        with mock.patch(
            "tedd_knowledge_graph.source_files.read_bytes",
            side_effect=AssertionError("renderer must not read sources"),
        ):
            enhanced = kg._v3_projection.build_projection_notes(
                graph,
                "review_safe",
                "Tedd Knowledge",
                dependencies=kg._projection_dependencies(),
                hooks=kg._projection_build_hooks(),
                source_views=(view,),
            )
        claim_path = next(path for path in ordinary if path.startswith("11 Claims/"))
        self.assertIn(f"[[Tedd Knowledge/20 Evidence/source-{view.binding}|", enhanced[claim_path])
        self.assertIn("Only after review.", enhanced[f"20 Evidence/source-{view.binding}.md"])
        for path in ordinary:
            if path != claim_path:
                self.assertEqual(ordinary[path], enhanced[path])
        self.assertEqual(json.dumps(graph, sort_keys=True), before)

    def test_public_builder_preserves_graph_identity_and_arguments(self) -> None:
        graph = kg.blank_graph()
        expected = {"sentinel.md": "sentinel"}
        dependencies = kg._projection_dependencies()
        hooks = kg._projection_build_hooks()

        with (
            mock.patch.object(
                kg,
                "_projection_dependencies",
                autospec=True,
                return_value=dependencies,
            ),
            mock.patch.object(
                kg,
                "_projection_build_hooks",
                autospec=True,
                return_value=hooks,
            ),
            mock.patch.object(
                kg._v3_projection,
                "build_projection_notes",
                autospec=True,
                return_value=expected,
            ) as build,
        ):
            result = kg.build_projection_notes(
                graph,
                "review_safe",
                "Codex Knowledge",
                skip_validation=True,
            )

        self.assertIs(result, expected)
        build.assert_called_once_with(
            graph,
            "review_safe",
            "Codex Knowledge",
            dependencies=dependencies,
            hooks=hooks,
            skip_validation=True,
        )

    def test_builder_resolves_legacy_hooks_at_call_time(self) -> None:
        graph = representative_graph()

        with (
            mock.patch.object(kg, "note_path", wraps=kg.note_path) as note_path,
            mock.patch.object(
                kg, "render_record_note", wraps=kg.render_record_note
            ) as render_record_note,
            mock.patch.object(kg, "frontmatter", wraps=kg.frontmatter) as frontmatter,
        ):
            notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")

        self.assertIn("00 Dashboard.md", notes)
        self.assertGreater(note_path.call_count, 0)
        self.assertGreater(render_record_note.call_count, 0)
        self.assertGreater(frontmatter.call_count, 0)

    def test_nested_text_helpers_remain_patchable(self) -> None:
        with mock.patch.object(
            kg, "yaml_scalar", autospec=True, return_value="patched"
        ) as render_scalar:
            content = kg.frontmatter({"value": "original"})

        self.assertEqual(content, "---\nvalue: patched\n---\n")
        render_scalar.assert_called_once_with("original")

    def test_representative_projection_remains_byte_identical(self) -> None:
        graph = representative_graph()
        validation = kg.validate_graph(graph)
        self.assertTrue(validation["valid"], validation["errors"])

        for mode, expected_digest in EXPECTED_PROJECTION_SHA256.items():
            with self.subTest(mode=mode):
                notes = kg.build_projection_notes(graph, mode, "Codex Knowledge")
                payload = json.dumps(
                    notes,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_digest)


if __name__ == "__main__":
    unittest.main()
