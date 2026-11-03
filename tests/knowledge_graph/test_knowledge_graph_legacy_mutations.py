#!/usr/bin/env python3
"""Characterization tests for the typed legacy mutation services."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import unittest
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS

SCRIPT = GRAPH_SCRIPTS / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_legacy_mutations", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


def parity_packet() -> dict[str, object]:
    return {
        "source": {
            "kind": "document",
            "role": "operator",
            "title": "Kernel parity",
            "uri": "https://example.test/report?token=TOPSECRET",
            "observed_at": "2026-08-01T08:00:00-04:00",
            "source_tier": "validated_structured_candidate",
            "excerpt": "safe evidence",
        },
        "entities": [
            {
                "ref": "a",
                "id": "entity_a",
                "type": "site",
                "namespace": "client",
                "name": "Alpha",
                "aliases": ["A"],
                "identity_keys": [{"scheme": "serial", "namespace": "client", "value": "001"}],
            },
            {
                "ref": "b",
                "id": "entity_b",
                "type": "site",
                "namespace": "client",
                "name": "Beta",
            },
        ],
        "relations": [{"source": "a", "target": "b", "type": "depends_on"}],
        "observations": [
            {
                "family": "health",
                "key": "status",
                "value": "green",
                "entity_refs": ["a"],
            }
        ],
        "claims": [
            {
                "family": "deployment",
                "key": "version",
                "value": "1.2.3",
                "entity_refs": ["a"],
                "conflict_policy": "replace",
            }
        ],
        "events": [{"title": "Deployed", "entity_refs": ["a"]}],
        "decisions": [{"title": "Keep release", "entity_refs": ["a"]}],
        "open_loops": [
            {
                "title": "Monitor",
                "entity_refs": ["a"],
                "owner_refs": ["b"],
                "priority": 2,
            }
        ],
        "receipts": [{"title": "CI pass", "entity_refs": ["a"], "verified": True}],
    }


class LegacyMutationServiceTests(unittest.TestCase):
    def test_full_capture_preserves_canonical_bytes_and_hashes(self) -> None:
        with mock.patch.object(kg, "utc_now", return_value="2026-08-03T12:34:56Z"):
            graph = kg.blank_graph()
            result = kg.capture_packet(
                graph,
                parity_packet(),
                authority_ceiling="validated_structured_candidate",
                allow_replace=True,
                producer={"agent_id": "a1", "task_id": "t1"},
                based_on_watermark=0,
                discovery_kind="verification",
            )

        payload = json.dumps(
            {"graph": graph, "result": result},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "0f5534cec6612a1e2d640928916e08d2e97819ff3e8abe696677dc5d316b0b0e",
        )
        self.assertEqual(
            graph["journal"][-1]["entry_hash"],
            "9e6362e203a71fc33225634ddee5e5c1ff48cf5c3e3fe1634816ea4f2d2d33e2",
        )
        self.assertEqual(result["fingerprint"], "a2271e95936579ca2a0c3b31ede389f1")

    def test_flat_signatures_remain_compatible(self) -> None:
        expected = {
            "index_by_id": ("records",),
            "upsert_entity": ("graph", "raw", "source_id", "observed_at", "incoming_tier"),
            "upsert_claim_surface": (
                "graph",
                "raw",
                "local_refs",
                "source",
                "family",
                "key",
                "context_refs",
            ),
            "capture_packet": (
                "graph",
                "raw_packet",
                "authority_ceiling",
                "allow_replace",
                "allow_redirect",
                "producer",
                "based_on_watermark",
                "discovery_kind",
            ),
        }
        for name, parameters in expected.items():
            with self.subTest(name=name):
                signature = inspect.signature(getattr(kg, name))
                self.assertEqual(tuple(signature.parameters), parameters)
        capture = inspect.signature(kg.capture_packet).parameters
        self.assertEqual(capture["authority_ceiling"].default, "assistant_inference")
        self.assertIs(capture["allow_replace"].default, False)
        self.assertIs(capture["allow_redirect"].default, False)
        self.assertIsNone(capture["producer"].default)

    def test_facade_composes_dependencies_at_call_time(self) -> None:
        sentinel = "validated_structured_candidate"
        with (
            mock.patch.object(kg, "normalize_key", return_value=sentinel) as normalize_key,
            mock.patch.object(
                kg._legacy_mutations.LegacyIdentityService,
                "source_tier",
                autospec=True,
                return_value=sentinel,
            ) as delegated,
        ):
            self.assertEqual(kg.source_tier("ignored"), sentinel)

        service = delegated.call_args.args[0]
        self.assertIs(service.dependencies.normalize_key, normalize_key)

    def test_capture_resolves_mutation_and_hash_hooks_at_call_time(self) -> None:
        real_upsert = kg.upsert_entity
        real_hash = kg.stable_hash
        with (
            mock.patch.object(kg, "utc_now", return_value="2026-08-03T12:34:56Z"),
            mock.patch.object(kg, "upsert_entity", wraps=real_upsert) as upsert,
            mock.patch.object(kg, "stable_hash", wraps=real_hash) as stable_hash,
        ):
            kg.capture_packet(
                kg.blank_graph(),
                parity_packet(),
                authority_ceiling="validated_structured_candidate",
                allow_replace=True,
            )

        self.assertGreaterEqual(upsert.call_count, 2)
        self.assertGreater(stable_hash.call_count, 1)

    def test_redaction_and_recursive_scrub_keep_public_hook_seams(self) -> None:
        with mock.patch.object(
            kg,
            "redact_uri",
            side_effect=lambda value: f"{value}#redacted-by-hook",
        ) as redact_uri:
            text, redactions = kg.redact_text("https://example.test/path")
        self.assertEqual(text, "https://example.test/path#redacted-by-hook")
        self.assertEqual(redactions, 1)
        redact_uri.assert_called_once_with("https://example.test/path")

        with mock.patch.object(
            kg,
            "redact_text",
            side_effect=lambda value, **_kwargs: (f"hook:{value}", 1),
        ) as redact_text:
            scrubbed, count = kg.scrub_value({"field": "value"})
        self.assertEqual(scrubbed, {"hook:field": "hook:value"})
        self.assertEqual(count, 2)
        self.assertEqual(redact_text.call_count, 2)

    def test_capture_rejects_non_json_after_dynamic_scrub_boundary(self) -> None:
        with (
            mock.patch.object(
                kg,
                "scrub_value",
                return_value=(
                    {"source": {"observed_at": "2026-08-03T00:00:00Z"}, "bad": object()},
                    0,
                ),
            ),
            self.assertRaisesRegex(ValueError, "contains a non-JSON value"),
        ):
            kg.capture_packet(kg.blank_graph(), {})


if __name__ == "__main__":
    unittest.main()
