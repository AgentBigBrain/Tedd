#!/usr/bin/env python3
"""Characterize the extracted legacy privacy and redaction policy."""

from __future__ import annotations

import re
import unittest
from collections.abc import Callable, Iterable

from tedd_knowledge_graph import legacy_privacy  # noqa: E402


def _identity_uri(value: str) -> str:
    return value


def _never_sensitive(value: object) -> bool:
    del value
    return False


def _dependencies(
    *,
    secret_patterns: Iterable[re.Pattern[str]] = (),
    path_patterns: Iterable[re.Pattern[str]] = (),
    redact_uri: Callable[[str], str] = _identity_uri,
    sensitive_field: Callable[[object], bool] = _never_sensitive,
) -> legacy_privacy.PrivacyDependencies:
    return legacy_privacy.PrivacyDependencies(
        secret_patterns=secret_patterns,
        path_patterns=path_patterns,
        redact_uri=redact_uri,
        sensitive_field=sensitive_field,
    )


class LegacyPrivacyTests(unittest.TestCase):
    def test_redact_text_uses_injected_policy_in_legacy_order(self) -> None:
        uri_inputs: list[str] = []

        def redact_uri(value: str) -> str:
            uri_inputs.append(value)
            return value.replace("uri-token", "uri-safe")

        dependencies = _dependencies(
            secret_patterns=(re.compile("secret"),),
            path_patterns=(re.compile("path"),),
            redact_uri=redact_uri,
        )

        cleaned, count = legacy_privacy.redact_text(
            "secret path uri-token",
            dependencies,
        )

        self.assertEqual(cleaned, "[redacted secret] [redacted local path] uri-safe")
        self.assertEqual(count, 3)
        self.assertEqual(
            uri_inputs,
            ["[redacted secret] [redacted local path] uri-token"],
        )

    def test_redact_paths_false_skips_only_injected_path_patterns(self) -> None:
        dependencies = _dependencies(
            secret_patterns=(re.compile("secret"),),
            path_patterns=(re.compile("path"),),
            redact_uri=lambda value: value.replace("uri-token", "uri-safe"),
        )

        cleaned, count = legacy_privacy.redact_text(
            "secret path uri-token",
            dependencies,
            redact_paths=False,
        )

        self.assertEqual(cleaned, "[redacted secret] path uri-safe")
        self.assertEqual(count, 2)

    def test_scrub_value_threads_one_dependency_snapshot_through_recursion(self) -> None:
        checked_fields: list[object] = []

        def sensitive_field(value: object) -> bool:
            checked_fields.append(value)
            return value == "credential"

        dependencies = _dependencies(
            secret_patterns=(re.compile("secret-value"),),
            path_patterns=(re.compile("local-path"),),
            sensitive_field=sensitive_field,
        )
        value: object = {
            "credential": "must-not-survive",
            "payload": ["secret-value", {"location": "local-path"}],
        }

        cleaned, count = legacy_privacy.scrub_value(value, dependencies)

        self.assertEqual(
            cleaned,
            {
                "credential": "[redacted secret]",
                "payload": [
                    "[redacted secret]",
                    {"location": "[redacted local path]"},
                ],
            },
        )
        self.assertEqual(count, 3)
        self.assertEqual(checked_fields, ["credential", "payload", "location"])

    def test_sensitive_empty_and_already_redacted_values_keep_legacy_counts(self) -> None:
        dependencies = _dependencies(sensitive_field=lambda value: value == "credential")
        value: object = {
            "credential": "[redacted secret]",
            "other": None,
        }

        cleaned, count = legacy_privacy.scrub_value(value, dependencies)

        self.assertEqual(cleaned, value)
        self.assertEqual(count, 0)

    def test_truncation_preserves_legacy_utf8_bytes_and_count(self) -> None:
        dependencies = _dependencies()

        cleaned, count = legacy_privacy.redact_text("x" * 4001, dependencies)

        expected_bytes = b"x" * (4000 - 18) + b" \xe2\x80\xa6 [truncated]"
        self.assertEqual(cleaned.encode("utf-8"), expected_bytes)
        self.assertEqual(count, 0)

    def test_depth_limit_allows_twenty_levels_and_rejects_twenty_one(self) -> None:
        dependencies = _dependencies()
        within_limit: object = "leaf"
        for _ in range(20):
            within_limit = {"next": within_limit}

        cleaned, count = legacy_privacy.scrub_value(within_limit, dependencies)

        self.assertEqual(cleaned, within_limit)
        self.assertEqual(count, 0)
        beyond_limit: object = {"next": within_limit}
        with self.assertRaisesRegex(
            ValueError,
            "JSON nesting exceeds the 20-level safety limit",
        ):
            legacy_privacy.scrub_value(beyond_limit, dependencies)

    def test_collection_limits_preserve_exact_boundaries_and_errors(self) -> None:
        dependencies = _dependencies()
        list_at_limit: object = [None] * 500
        object_at_limit: object = {str(index): None for index in range(300)}

        self.assertEqual(
            legacy_privacy.scrub_value(list_at_limit, dependencies),
            (list_at_limit, 0),
        )
        self.assertEqual(
            legacy_privacy.scrub_value(object_at_limit, dependencies),
            (object_at_limit, 0),
        )
        with self.assertRaisesRegex(
            ValueError,
            "JSON list exceeds the 500-item safety limit",
        ):
            legacy_privacy.scrub_value([None] * 501, dependencies)
        with self.assertRaisesRegex(
            ValueError,
            "JSON object exceeds the 300-field safety limit",
        ):
            legacy_privacy.scrub_value(
                {str(index): None for index in range(301)},
                dependencies,
            )


if __name__ == "__main__":
    unittest.main()
