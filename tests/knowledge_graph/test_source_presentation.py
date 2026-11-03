"""Neutral policy and pure-rendering tests; no native viewer qualification."""

from __future__ import annotations

import unittest
from dataclasses import asdict, replace
from pathlib import Path

import knowledge_graph as kg
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceExcerpt,
    SourceLink,
    SourceRef,
    SourceScope,
    SourceUnit,
    encode,
    parse_json,
)


class SourcePresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = SourceScope("store", "generation", ("subject",), "personal_task")
        self.config: dict[str, object] = {
            "vault_path": str(Path.cwd() / "neutral-vault-not-created"),
            "root_name": "Tedd Knowledge",
            "projection_root_id": "root_test",
        }
        self.policy: dict[str, object] = {
            "format": 1,
            "scope": parse_json(encode(asdict(self.scope))),
            "enabled": True,
            "audience": "owner",
            "sensitivities": ["ordinary"],
            "exposure": "local_only",
            "exposure_approved": True,
            **self.config,
        }
        self.ref = SourceRef("a" * 32, self.scope.key, "c" * 64, "u1")
        self.excerpt = SourceExcerpt(
            self.ref,
            (SourceUnit("u1", "Only after review.\n![remote](https://example.test/image)", 1),),
            0,
            (),
            0,
            "complete",
            "original",
            "d" * 64,
            source_version="v1",
            captured_at="2026-09-06T12:00:00Z",
            capture_method="local_file",
        )

    def test_absent_disabled_and_exact_scope_target_policy(self) -> None:
        from tedd_knowledge_graph.source_presentation import decode_preview_policy

        self.assertIsNone(decode_preview_policy(None, self.config, self.scope))
        disabled = decode_preview_policy({**self.policy, "enabled": False}, self.config, self.scope)
        self.assertIsNotNone(disabled)
        assert disabled is not None
        self.assertFalse(disabled.enabled)
        for change in (
            {"audience": "model"},
            {"exposure_approved": False},
            {"exposure": "unknown"},
            {"root_name": "Other"},
            {"projection_root_id": "another_root"},
            {"vault_path": "relative"},
            {"scope": parse_json(encode(asdict(replace(self.scope, generation="other"))))},
            {"enabled": 1},
            {"sensitivities": ["medical"]},
            {"extra": True},
        ):
            with self.subTest(change=change), self.assertRaises(SourceArchiveError):
                decode_preview_policy({**self.policy, **change}, self.config, self.scope)
        allowed = decode_preview_policy(
            {**self.policy, "exposure": "approved_sync"}, self.config, self.scope
        )
        self.assertIsNotNone(allowed)

    def test_prepared_note_is_literal_versioned_and_bound_to_exact_reference(self) -> None:
        from tedd_knowledge_graph.source_presentation import PreparedSourceView, render_source_view

        view = PreparedSourceView(("claim_neutral",), self.excerpt, "Résumé [one] ![image]")
        path, note = render_source_view(view, escape=kg.markdown_literal)
        self.assertTrue(path.startswith("20 Evidence/source-"))
        self.assertNotIn("Résumé", path)
        self.assertIn("Résumé \\[one\\]", note)
        self.assertIn("    Only after review.\n    ![remote]", note)
        self.assertIn("Source version: v1", note)
        self.assertIn("Derivative", note)
        self.assertIn("Source time: unknown", note)
        changed = replace(
            view, excerpt=replace(self.excerpt, ref=replace(self.ref, manifest_sha256="e" * 64))
        )
        other_path, _ = render_source_view(changed, escape=kg.markdown_literal)
        self.assertNotEqual(path, other_path)

    def test_prepared_views_refuse_unbound_image_and_invalid_or_oversized_readings(self) -> None:
        from tedd_knowledge_graph.source_presentation import PreparedSourceView

        valid = PreparedSourceView(("claim",), self.excerpt, "Neutral")
        for change in (
            {"memory_ids": ()},
            {"memory_ids": ("claim", "claim")},
            {"image_path": "https://example.test/image.png"},
            {"image_path": "20 Evidence/unbound.png"},
            {"title": "x" * 257},
            {"excerpt": replace(self.excerpt, integrity="unverified")},
            {"excerpt": replace(self.excerpt, units=())},
            {"excerpt": replace(self.excerpt, ref=replace(self.ref, anchor_id="u2"))},
            {"excerpt": replace(self.excerpt, previous_units=-1)},
            {"excerpt": replace(self.excerpt, remaining_units=True)},
        ):
            with self.subTest(change=change), self.assertRaises(SourceArchiveError):
                replace(valid, **change)

    def test_preview_uses_character_budget_and_accepts_the_archived_title_ceiling(self) -> None:
        from tedd_knowledge_graph.source_presentation import PreparedSourceView

        for body, title in (
            ("界" * 6000, "Neutral"),
            ("🟦" * 6000, "Neutral"),
            ("Neutral", "界" * 256),
        ):
            with self.subTest(character=body[0], title_length=len(title)):
                excerpt = replace(self.excerpt, units=(SourceUnit("u1", body, 1),))
                view = PreparedSourceView(("claim",), excerpt, title)
                self.assertEqual(view.excerpt.units[0].text, body)
                self.assertEqual(view.title, title)

    def test_source_note_preserves_message_attribution_and_unresolved_context(self) -> None:
        from tedd_knowledge_graph.source_presentation import PreparedSourceView, render_source_view

        unit = replace(
            self.excerpt.units[0],
            source_id="message-2",
            speaker="Support [operator]",
            source_time="2026-09-06T12:00:00Z",
            reply_to="message-1",
            links=(SourceLink("attachment", "checklist", "v2", None, None),),
        )
        view = PreparedSourceView(("claim",), replace(self.excerpt, units=(unit,)), "Condition")
        _, note = render_source_view(view, escape=kg.markdown_literal)
        for expected in (
            "Speaker: Support \\[operator\\]",
            "Message time: 2026-09-06T12:00:00Z",
            "Reply to: message-1 (not included)",
            "attachment: checklist; version: v2; unit: unknown; not included",
            "Only after review.",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, note)


if __name__ == "__main__":
    unittest.main()
