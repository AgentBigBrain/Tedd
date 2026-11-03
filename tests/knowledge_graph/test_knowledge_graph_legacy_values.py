#!/usr/bin/env python3
"""Characterize the typed legacy value service and its flat facade."""

from __future__ import annotations

import re
import unittest
from unittest import mock

import knowledge_graph as kg  # noqa: E402
from tedd_knowledge_graph import legacy_values  # noqa: E402


class LegacyValueServiceTests(unittest.TestCase):
    def test_review_paths_preserve_legacy_bare_directory_and_injected_policies(self) -> None:
        """A tokenizer must not separate a legacy pattern's root from its suffix."""
        for raw in ("/tmp/", "/var/", "/private/"):
            for redact in (kg.redact_text, legacy_values.redact_text):
                with self.subTest(raw=raw, redact=redact.__module__):
                    self.assertEqual(redact(raw), ("[redacted local path]", 1))
        with mock.patch.object(
            kg, "PATH_PATTERNS", [*kg.PATH_PATTERNS, re.compile("/confidential")]
        ):
            self.assertEqual(kg.redact_text("/confidential"), ("[redacted local path]", 1))

    def test_review_paths_cover_custom_roots_and_complete_space_containing_paths(self) -> None:
        """A prefix-only or whitespace-only scrubber leaks these synthetic paths."""
        cases = (
            (
                "parent contains untrusted symlink: /custom#private/graphs",
                "parent contains untrusted symlink: [redacted local path]",
            ),
            (
                "parent contains untrusted symlink: /custom?private/graphs",
                "parent contains untrusted symlink: [redacted local path]",
            ),
            (
                "parent contains untrusted symlink: /custom",
                "parent contains untrusted symlink: [redacted local path]",
            ),
            ("/tmp/#private", "[redacted local path]"),
            (
                "parent contains untrusted symlink: /custom/graphs",
                "parent contains untrusted symlink: [redacted local path]",
            ),
            ('Read "/custom/graphs/x.sqlite"; ready.', 'Read "[redacted local path]"; ready.'),
            (
                "/Users/example/Library/Application Support/tedd/graph.sqlite",
                "[redacted local path]",
            ),
            (
                "Read `/custom/Shared Files/graph.sqlite`; ready.",
                "Read `[redacted local path]`; ready.",
            ),
            (r"C:\Custom Root\Shared Files\graph.sqlite", "[redacted local path]"),
            (r"\\server\Private Share\Shared Files\graph.sqlite", "[redacted local path]"),
            ("file:///custom/Shared Files/graph.sqlite", "[redacted local path]"),
            ("~/Shared Files/graph.sqlite", "[redacted local path]"),
            ("Read /custom/graphs/Private File.sqlite and stop", "Read [redacted local path]"),
            (
                'Read "/custom/Bob\'s Files/a.sqlite"; ready.',
                'Read "[redacted local path]"; ready.',
            ),
            (
                'Read "/custom/https://example.test/Private Files/graph.sqlite"; ready.',
                'Read "[redacted local path]"; ready.',
            ),
        )
        for raw, expected in cases:
            for redact in (kg.redact_text, legacy_values.redact_text):
                with self.subTest(raw=raw, redact=redact.__module__):
                    self.assertEqual(redact(raw), (expected, 1))
                    self.assertEqual(redact(expected), (expected, 0))

    def test_review_paths_preserve_web_addresses_and_explicit_routes(self) -> None:
        """Recognizing custom roots must not redact the path part of public URLs."""
        for raw in (
            "https://example.test/custom/graphs/x.sqlite",
            "https://example.test/Users/example/Library/Application%20Support/a",
            "Visit /support and https://example.test/custom/graphs",
            "https://example.test/projects/client-case/ and /projects/?page=2",
            "The route /api/v1/status is available.",
            "endpoint: /custom/graphs",
            "Keep the launch/trial handoff with the customer.",
        ):
            for redact in (kg.redact_text, legacy_values.redact_text):
                with self.subTest(raw=raw, redact=redact.__module__):
                    self.assertEqual(redact(raw), (raw, 0))

    def test_structured_paths_are_withheld_without_losing_field_shape(self) -> None:
        """Known path fields must not rely on guessing path syntax from strings."""
        raw = {
            "path": "opaque-local-location",
            "details": [{"storePath": "/custom"}],
            "file_path": ["/custom/a", "/custom/b"],
        }
        expected = {
            "path": "[redacted local path]",
            "details": [{"storePath": "[redacted local path]"}],
            "file_path": "[redacted local path]",
        }
        for scrub in (kg.scrub_value, legacy_values.scrub_value):
            with self.subTest(scrub=scrub.__module__):
                self.assertEqual(scrub(raw), (expected, 3))
                self.assertEqual(scrub(expected), (expected, 0))
                self.assertEqual(scrub(raw, redact_paths=False), (raw, 0))

    def test_structured_routes_keep_secret_redaction(self) -> None:
        """Explicit route fields may bypass path classification, never secret policy."""
        query_key = "token"
        raw = {
            "url": "/custom/graphs",
            "route": "/api/v1/status",
            "href": f"/custom/graphs?{query_key}=synthetic-private-value",
        }
        for scrub in (kg.scrub_value, legacy_values.scrub_value):
            with self.subTest(scrub=scrub.__module__):
                cleaned, _ = scrub(raw)
                self.assertIsInstance(cleaned, dict)
                assert isinstance(cleaned, dict)
                self.assertEqual(cleaned["url"], raw["url"])
                self.assertEqual(cleaned["route"], raw["route"])
                self.assertNotIn("synthetic-private-value", repr(cleaned))

    def test_route_labels_cannot_override_known_filesystem_path_policy(self) -> None:
        """A route key or prose label must not exempt recognized private paths."""
        for path in (
            "/home/example/private.sqlite",
            "/Users/example/private.sqlite",
            "/var/www/status",
            "/tmp/private.sqlite",
            "/root/private.sqlite",
            "/Volumes/Example/private.sqlite",
            r"C:\Users\Example\private.sqlite",
            r"\\server\share\private.sqlite",
            "file:///home/example/private.sqlite",
        ):
            for key in ("route", "url", "href", "endpoint"):
                for scrub in (kg.scrub_value, legacy_values.scrub_value):
                    with self.subTest(path=path, key=key, scrub=scrub.__module__):
                        self.assertEqual(scrub({key: path}), ({key: "[redacted local path]"}, 1))
                        self.assertEqual(scrub({key: path}, redact_paths=False), ({key: path}, 0))
                for redact in (kg.redact_text, legacy_values.redact_text):
                    with self.subTest(path=path, label=key, redact=redact.__module__):
                        self.assertEqual(
                            redact(f"{key}: {path}"), (f"{key}: [redacted local path]", 1)
                        )
        with mock.patch.object(kg, "PATH_PATTERNS", [re.compile(r"/confidential")]):
            self.assertEqual(
                kg.scrub_value({"route": "/confidential"}), ({"route": "[redacted local path]"}, 1)
            )
            self.assertEqual(
                kg.redact_text("route: /confidential"), ("route: [redacted local path]", 1)
            )

    def test_flat_facade_keeps_exported_policy_objects(self) -> None:
        self.assertIs(kg.SAFE_RECORD_ID, legacy_values.SAFE_RECORD_ID)
        self.assertIs(kg.SENSITIVE_FIELD_KEYS, legacy_values.SENSITIVE_FIELD_KEYS)
        self.assertIs(kg.SECRET_PATTERNS, legacy_values.SECRET_PATTERNS)
        self.assertIs(kg.PATH_PATTERNS, legacy_values.PATH_PATTERNS)

    def test_facade_supplies_clock_at_call_time(self) -> None:
        fixed_now = "2026-08-03T12:34:56+00:00"

        with mock.patch.object(kg, "utc_now", return_value=fixed_now):
            self.assertEqual(kg.parse_time(""), "2026-08-03T12:34:56Z")

    def test_nested_boundary_values_are_bounded_and_redacted(self) -> None:
        value: object = {
            "clientSecret": "do-not-persist",
            "details": [
                "file:///Users/operator/private.txt",
                "ordinary review-safe context",
            ],
        }

        cleaned, count = legacy_values.scrub_value(value)
        facade_cleaned, facade_count = kg.scrub_value(value)

        self.assertEqual((facade_cleaned, facade_count), (cleaned, count))
        self.assertNotIn("do-not-persist", repr(cleaned))
        self.assertIn("[redacted secret]", repr(cleaned))
        self.assertIn("[redacted local path]", repr(cleaned))
        self.assertGreaterEqual(count, 2)
        self.assertEqual(kg.secret_hits(cleaned), legacy_values.secret_hits(cleaned))

    def test_explicit_identifier_contract_is_unchanged(self) -> None:
        self.assertEqual(legacy_values.explicit_record_id(" loop_1 "), "loop_1")
        with self.assertRaisesRegex(ValueError, "Explicit record IDs"):
            legacy_values.explicit_record_id("unsafe id")

    def test_session_suffix_is_structurally_sensitive(self) -> None:
        for field in ("browser_session", "browserSession", "nested-user-session"):
            with self.subTest(field=field):
                self.assertTrue(legacy_values.sensitive_field(field))
                self.assertTrue(kg.sensitive_field(field))

        cleaned, redactions = legacy_values.scrub_value(
            {"browser_session": "synthetic-session-value"}
        )

        self.assertEqual(cleaned, {"browser_session": "[redacted secret]"})
        self.assertEqual(redactions, 1)


if __name__ == "__main__":
    unittest.main()
