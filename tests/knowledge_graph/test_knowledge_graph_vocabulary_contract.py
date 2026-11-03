#!/usr/bin/env python3
"""Behavioral contracts for the shared discovery-kind vocabulary."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest

import knowledge_graph as kg  # noqa: E402
import knowledge_graph_v4_cli as v4_cli  # noqa: E402
from tedd_knowledge_graph.v4_contracts import DISCOVERY_KINDS  # noqa: E402

CANONICAL_DISCOVERY_KINDS = (
    "diagnosis",
    "decision",
    "verification",
    "release",
    "support_outcome",
    "stakeholder_outcome",
    "identity_assertion",
    "identity_resolution",
    "open_loop",
    "loop_resolution",
    "correction",
    "reusable_lesson",
    "milestone",
    "substantial_analysis",
    "migration",
    "redaction",
)


class DiscoveryKindVocabularyContractTests(unittest.TestCase):
    def test_public_compatibility_exports_share_the_contract_tuple(self) -> None:
        self.assertIs(kg.DISCOVERY_KINDS, DISCOVERY_KINDS)
        self.assertIs(v4_cli.DISCOVERY_KINDS, DISCOVERY_KINDS)

    def test_public_parser_accepts_every_canonical_discovery_kind(self) -> None:
        parser = kg.build_parser()
        capture_arguments = [
            "capture",
            "--input",
            "packet.json",
            "--producer-agent-id",
            "agent-1",
            "--task-id",
            "task-1",
            "--producer-run-id",
            "run-1",
            "--based-on-watermark",
            "0",
            "--discovery-kind",
        ]

        for discovery_kind in CANONICAL_DISCOVERY_KINDS:
            with self.subTest(discovery_kind=discovery_kind):
                args = parser.parse_args([*capture_arguments, discovery_kind])
                self.assertEqual(args.discovery_kind, discovery_kind)

    def test_native_v4_parser_and_producer_accept_every_canonical_discovery_kind(self) -> None:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command", required=True)
        v4_cli.register_v4_subcommands(subparsers)
        capture_arguments = [
            "capture",
            "--input",
            "packet.json",
            "--producer-agent-id",
            "agent-1",
            "--task-id",
            "task-1",
            "--producer-run-id",
            "run-1",
            "--based-on-watermark",
            "0",
            "--discovery-kind",
        ]

        for discovery_kind in CANONICAL_DISCOVERY_KINDS:
            with self.subTest(discovery_kind=discovery_kind):
                args = parser.parse_args([*capture_arguments, discovery_kind])
                envelope = v4_cli._producer_envelope(args, require_discovery=True)
                self.assertEqual(envelope.discovery_kind, discovery_kind)

    def test_native_v4_parser_rejects_discovery_kind_outside_the_compatibility_contract(
        self,
    ) -> None:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command", required=True)
        v4_cli.register_v4_subcommands(subparsers)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "capture",
                    "--input",
                    "packet.json",
                    "--producer-agent-id",
                    "agent-1",
                    "--task-id",
                    "task-1",
                    "--producer-run-id",
                    "run-1",
                    "--based-on-watermark",
                    "0",
                    "--discovery-kind",
                    "discovery",
                ]
            )


if __name__ == "__main__":
    unittest.main()
