"""Journal-derived handoff proof against synthetic, reviewed v4 captures."""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import knowledge_graph as graph
import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture
import knowledge_graph_v4_cli as cli
from platform_support import mode_has_any, owner_is_current_user, set_private_path_mode
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, ProducerEnvelope

from tests.support.graph_cases import PLAN_KEY, V4TestCase, applied, change
from tests.support.paths import GRAPH_SCRIPTS


class HandoffFixture(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        # A missing implementation is a RED assertion, not an import-time error
        # that would prevent the unaffected capture controls from running.
        try:
            self.receipts = importlib.import_module("tedd_knowledge_graph.handoff_receipts")
        except ModuleNotFoundError as error:
            if error.name != "tedd_knowledge_graph.handoff_receipts":
                raise
            self.fail("Journal-derived handoff receipts are not implemented")
        with self.create_store() as store:
            applied(store, [change("entity_project")], fingerprint="fixture")
            store.register_consumer("consumer_alpha", agent_id="agent_alpha", task_id="task_alpha")
            store.register_consumer("consumer_beta", agent_id="agent_beta", task_id="task_beta")
        self.identity = self.receipts.HandoffIdentity(
            "consumer_alpha", "agent_alpha", "task_alpha", "prompt_alpha"
        )

    def request(self, event_id: str, *, prompt: str | None = "prompt_alpha") -> CaptureRequest:
        packet = {
            "source": {
                "kind": "test",
                "role": "verified",
                "title": "Synthetic milestone",
                "observed_at": "2026-01-03T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "events": [
                {
                    "id": event_id,
                    "kind": "milestone",
                    "title": f"Reviewed milestone {event_id}",
                    "summary": "Verified the synthetic deployment.",
                    "occurred_at": "2026-01-03T12:00:00Z",
                    "entity_refs": ["entity_project"],
                }
            ],
        }
        return CaptureRequest(
            input_path="-",
            input_bytes=json.dumps(packet).encode(),
            dry_run=True,
            consumer_id="consumer_alpha",
            envelope=ProducerEnvelope(
                "agent_alpha", "task_alpha", None, "run_original", "milestone", 1
            ),
            authority_ceiling="validated_structured_candidate",
            allow_replace=False,
            allow_redirect=False,
            expected_fingerprint=None,
            expected_global_watermark=None,
            plan_token=None,
            expected_revisions={},
            expected_absent=(),
            handoff_prompt_id=prompt,
        )

    @staticmethod
    def apply(store: v4.KnowledgeGraphV4, request: CaptureRequest) -> dict[str, object]:
        preview = capture.execute_capture(store, request)
        return capture.execute_capture(
            store,
            dataclasses.replace(
                request,
                dry_run=False,
                expected_fingerprint=str(preview["fingerprint"]),
                plan_token=str(preview["plan_token"]),
            ),
        )


class HandoffReceiptTests(HandoffFixture):
    def test_reconstruction_recovers_original_run_and_exact_changed_revisions(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            result = self.apply(store, self.request("event_saved"))
            before = store.state()
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(len(receipt["mutations"]), 1)
            mutation = receipt["mutations"][0]
            self.assertEqual(mutation["mutation_id"], result["mutation_id"])
            self.assertEqual(mutation["watermark"], result["watermark"])
            self.assertEqual(mutation["run_id"], "run_original")
            self.assertEqual(mutation["changed_ids"], result["changed_ids"])
            self.assertEqual(mutation["revisions"]["event_saved"], 1)
            self.assertTrue(self.receipts.verify_receipt(store, self.identity, receipt))
            self.assertEqual(store.state(), before)

    def test_nothing_receipt_is_asserted_not_saved_and_cannot_hide_capture(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(hasattr(self.receipts, "nothing_receipt"), "no-save receipts missing")
            before = store.state()
            receipt = self.receipts.nothing_receipt(store, self.identity, "Nothing durable to save")
            self.assertEqual(receipt["status"], "agent_asserted")
            self.assertEqual(receipt["mutations"], [])
            self.assertFalse(self.receipts.verify_receipt(store, self.identity, receipt))
            self.assertTrue(self.receipts.accepts_nothing(store, self.identity, receipt))
            self.apply(store, self.request("event_after_nothing"))
            self.assertFalse(self.receipts.accepts_nothing(store, self.identity, receipt))
            self.assertEqual(before["watermark"] + 1, store.state()["watermark"])

    def test_reusable_fix_requires_all_five_nonempty_fields(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            request = self.request("event_fix")
            packet = json.loads(request.input_bytes)
            packet["events"][0]["kind"] = "reusable_fix"
            for summary in ("Fixed it", "Symptom: x Cause: y Fix: z Verified by: t Applies when: "):
                packet["events"][0]["summary"] = summary
                with (
                    self.subTest(summary=summary),
                    self.assertRaisesRegex(ValueError, "reusable_fix"),
                ):
                    capture.execute_capture(
                        store, dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
                    )
            packet["events"][0]["summary"] = (
                "Symptom: timeout Cause: limit Fix: bound work Verified by: regression Applies when: parsing"
            )
            self.assertEqual(
                "captured",
                self.apply(
                    store, dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
                )["status"],
            )

    def capture_args(self, *extra: str):
        packet_path = self.root / "packet.json"
        packet_path.write_bytes(self.request("event_cli").input_bytes)
        set_private_path_mode(packet_path, 0o600)
        return graph.build_parser().parse_args(
            [
                "capture",
                "--input",
                str(packet_path),
                "--consumer-id",
                "consumer_alpha",
                "--producer-agent-id",
                "agent_alpha",
                "--task-id",
                "task_alpha",
                "--producer-run-id",
                "run_cli",
                "--discovery-kind",
                "milestone",
                "--based-on-watermark",
                "1",
                "--authority-ceiling",
                "validated_structured_candidate",
                "--mode",
                "operator_full",
                *extra,
            ]
        )

    def run_cli(self, args):
        return cli.handle_v4_command(
            args,
            db_path=self.store_path,
            plan_hmac_key=PLAN_KEY,
            capture_handler=capture.execute_capture,
        )

    def reviewed_cli_apply(self, *extra: str):
        preview = self.run_cli(self.capture_args("--dry-run"))
        return self.run_cli(
            self.capture_args(
                "--expect-fingerprint",
                preview["fingerprint"],
                "--expect-plan-token",
                preview["plan_token"],
                *extra,
            )
        )

    def handoff_args(self, *extra: str):
        return graph.build_parser().parse_args(
            [
                "handoff",
                "--check",
                "--session",
                "task_alpha",
                "--prompt",
                "prompt_alpha",
                "--consumer-id",
                "consumer_alpha",
                "--producer-agent-id",
                "agent_alpha",
                *extra,
                "--mode",
                "operator_full",
            ]
        )

    def test_cli_publishes_and_checks_private_receipt_without_new_graph_writes(self) -> None:
        with mock.patch.object(
            self.receipts, "handoff_directory", return_value=self.root / "handoffs"
        ):
            result = self.reviewed_cli_apply("--handoff", "--handoff-prompt-id", "prompt_alpha")
            self.assertEqual(result["handoff_receipt"]["status"], "verified")
            with v4.open_store(self.store_path, readonly=True) as store:
                before = store.state()
            self.assertEqual(self.run_cli(self.handoff_args())["status"], "verified")
            with v4.open_store(self.store_path, readonly=True) as store:
                self.assertEqual(store.state(), before)
                self.assertTrue(store.validate(full=True)["valid"])
            files = list((self.root / "handoffs").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertTrue(owner_is_current_user(files[0]))
            self.assertFalse(mode_has_any(files[0], 0o077))
            document = json.loads(files[0].read_bytes())
            self.assertEqual(document["mutations"][0]["run_id"], "run_cli")
            self.assertNotIn("Verified the synthetic deployment.", files[0].read_text())

    def test_public_cli_review_safe_roundtrip_uses_only_disposable_state(self) -> None:
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": "v2",
                    "store_path": str(self.store_path),
                    "canonical_store_format": "sqlite_v4_wal",
                }
            )
        )
        set_private_path_mode(config, 0o600)
        # HOME/USERPROFILE redirect ONLY this child test's operational receipts.
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA")
            if key in os.environ
        }
        environment.update({"HOME": str(self.root), "USERPROFILE": str(self.root)})
        packet = self.root / "packet.json"
        packet.write_bytes(self.request("event_cli").input_bytes)
        set_private_path_mode(packet, 0o600)
        base = [
            sys.executable,
            "-B",
            str(GRAPH_SCRIPTS / "knowledge_graph.py"),
            "--config",
            str(config),
        ]
        capture_options = [
            "capture",
            "--input",
            str(packet),
            "--consumer-id",
            "consumer_alpha",
            "--producer-agent-id",
            "agent_alpha",
            "--task-id",
            "task_alpha",
            "--producer-run-id",
            "run_cli",
            "--discovery-kind",
            "milestone",
            "--based-on-watermark",
            "1",
            "--authority-ceiling",
            "validated_structured_candidate",
        ]

        def run(arguments):
            result = subprocess.run(
                [*base, *arguments], env=environment, capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        plan = self.root / "review.plan.json"
        preview = run([*capture_options, "--dry-run", "--plan-output", str(plan)])
        self.assertEqual(preview["plan_token"], "[redacted secret]")
        captured = run(
            [
                *capture_options,
                "--expect-fingerprint",
                preview["fingerprint"],
                "--expect-plan-file",
                str(plan),
                "--handoff",
                "--handoff-prompt-id",
                "prompt_alpha",
            ]
        )
        self.assertEqual(captured["handoff_receipt"]["status"], "verified")
        checked = run(
            [
                "handoff",
                "--check",
                "--session",
                "task_alpha",
                "--prompt",
                "prompt_alpha",
                "--consumer-id",
                "consumer_alpha",
                "--producer-agent-id",
                "agent_alpha",
            ]
        )
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(len(list((self.root / ".codex/tedd/handoffs").glob("*.json"))), 1)

    def test_committed_capture_survives_receipt_failure_then_reconstructs(self) -> None:
        with mock.patch.object(
            self.receipts, "handoff_directory", return_value=self.root / "handoffs"
        ):
            preview = self.run_cli(self.capture_args("--dry-run"))
            options = (
                "--expect-fingerprint",
                preview["fingerprint"],
                "--expect-plan-token",
                preview["plan_token"],
                "--handoff",
                "--handoff-prompt-id",
                "prompt_alpha",
            )
            with mock.patch.object(
                cli, "_write_handoff_file", side_effect=OSError("synthetic full disk")
            ):
                result = self.run_cli(self.capture_args(*options))
            self.assertEqual(result["status"], "captured")
            self.assertEqual(result["handoff_receipt"]["status"], "pending")
            self.assertIsNotNone(result["mutation_id"])
            self.assertEqual(self.run_cli(self.handoff_args())["status"], "pending")
            retry = self.run_cli(self.capture_args(*options, "--producer-run-id", "run_retry"))
            self.assertEqual(retry["status"], "duplicate")
            self.assertEqual(retry["changed_ids"], [])
            self.assertEqual(retry["handoff_receipt"]["mutations"][0]["run_id"], "run_cli")
            restored = self.run_cli(self.handoff_args("--reconstruct"))
            self.assertEqual(restored["status"], "verified")
            self.assertEqual(restored["mutations"][0]["mutation_id"], result["mutation_id"])
            self.assertEqual(restored["mutations"][0]["run_id"], "run_cli")

    def test_strict_no_write_mode_refuses_handoff_apply_before_graph_open(self) -> None:
        with mock.patch.dict("os.environ", {"TEDD_NO_LOCAL_WRITES": "1"}):
            with mock.patch.object(cli, "_open") as opener:
                with self.assertRaisesRegex(cli.V4CLIError, "no-local-writes"):
                    self.run_cli(
                        self.capture_args("--handoff", "--handoff-prompt-id", "prompt_alpha")
                    )
                opener.assert_not_called()

    def test_check_refuses_tampered_receipt_without_echoing_or_repairing_it(self) -> None:
        with mock.patch.object(
            self.receipts, "handoff_directory", return_value=self.root / "handoffs"
        ):
            self.reviewed_cli_apply("--handoff", "--handoff-prompt-id", "prompt_alpha")
            path = next((self.root / "handoffs").glob("*.json"))
            document = json.loads(path.read_bytes())
            document["mutations"][0]["run_id"] = "forged-run"
            path.write_text(json.dumps(document))
            original = path.read_bytes()
            result = self.run_cli(self.handoff_args())
            self.assertEqual(result["status"], "unverified")
            self.assertNotIn("forged-run", json.dumps(result))
            self.assertEqual(path.read_bytes(), original)

    def test_reconstruction_repairs_malformed_private_receipt_without_recapture(self) -> None:
        with mock.patch.object(
            self.receipts, "handoff_directory", return_value=self.root / "handoffs"
        ):
            result = self.reviewed_cli_apply("--handoff", "--handoff-prompt-id", "prompt_alpha")
            path = next((self.root / "handoffs").glob("*.json"))
            path.write_bytes(b"{")
            repaired = self.run_cli(self.handoff_args("--reconstruct"))
            self.assertEqual(repaired["status"], "verified")
            self.assertEqual(repaired["mutations"][0]["mutation_id"], result["mutation_id"])
            with v4.open_store(self.store_path, readonly=True) as store:
                self.assertEqual(store.watermark, result["watermark"])

    def test_private_receipt_keeps_existing_descriptor_and_parent_checks(self) -> None:
        path = self.root / "handoffs" / "synthetic.json"
        cli._write_handoff_file(path, {"version": 1})
        original = path.read_bytes()
        with mock.patch.object(cli, "mode_has_any", return_value=True):
            with self.assertRaises(cli.V4CLIError):
                cli._read_handoff_file(path)
            with self.assertRaises(cli.V4CLIError):
                cli._write_handoff_file(path, {"version": 2})
        self.assertEqual(path.read_bytes(), original)
        with mock.patch.object(
            cli, "is_link_or_reparse_point", side_effect=lambda value: value == path
        ):
            with self.assertRaises(cli.V4CLIError):
                cli._read_handoff_file(path)

    def test_receipt_limits_refuse_partial_success(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, self.request("event_first"))
            self.apply(store, self.request("event_second"))
            with mock.patch.object(self.receipts, "MAX_HANDOFF_MUTATIONS", 1):
                with self.assertRaisesRegex(self.receipts.HandoffError, "mutation limit"):
                    self.receipts.reconstruct_receipt(store, self.identity)
            with mock.patch.object(self.receipts, "MAX_HANDOFF_CHANGES", 1):
                with self.assertRaisesRegex(self.receipts.HandoffError, "changes unavailable"):
                    self.receipts.reconstruct_receipt(store, self.identity)

    def test_partial_receipt_requires_reconstruction_after_another_packet(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, self.request("event_first"))
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            self.apply(store, self.request("event_second"))
            self.assertFalse(self.receipts.verify_receipt(store, self.identity, receipt))
            current = self.receipts.reconstruct_receipt(store, self.identity)
            self.assertTrue(self.receipts.verify_receipt(store, self.identity, current))

    def test_invalid_cli_handoff_combinations_never_capture(self) -> None:
        for options in (("--handoff", "--dry-run"), ("--handoff-prompt-id", "prompt_alpha")):
            with self.subTest(options=options):
                with v4.open_store(self.store_path, readonly=True) as store:
                    before = store.state()
                with self.assertRaises(ValueError):
                    self.run_cli(self.capture_args(*options))
                with v4.open_store(self.store_path, readonly=True) as store:
                    self.assertEqual(store.state(), before)

    def test_receipt_decoder_refuses_duplicate_keys_and_unbounded_numbers(self) -> None:
        path = self.root / "receipt.json"
        for raw in (
            b'{"version":1,"version":1}',
            b'{"version":' + b"9" * 40 + b"}",
            b'{"version":1.0}',
            b'{"version":NaN}',
        ):
            path.write_bytes(raw)
            set_private_path_mode(path, 0o600)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                cli._read_handoff_file(path)

    def test_duplicate_in_new_run_does_not_rewrite_original_saving_provenance(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            request = self.request("event_saved")
            preview = capture.execute_capture(store, request)
            approved = dataclasses.replace(
                request,
                dry_run=False,
                expected_fingerprint=str(preview["fingerprint"]),
                plan_token=str(preview["plan_token"]),
            )
            original = capture.execute_capture(store, approved)
            before = store.state()
            retry = dataclasses.replace(
                approved,
                envelope=dataclasses.replace(
                    request.envelope,
                    producer_run_id="run_retry",
                    based_on_watermark=store.watermark,
                ),
            )
            duplicate = capture.execute_capture(store, retry)
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(duplicate["changed_ids"], [])
            self.assertEqual(duplicate["mutation_id"], original["mutation_id"])
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            self.assertEqual(receipt["mutations"][0]["run_id"], "run_original")
            self.assertEqual(store.state(), before)

    def test_multiple_packets_accumulate_once_and_exclude_other_prompts(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            first = self.apply(store, self.request("event_first"))
            second = self.apply(store, self.request("event_second"))
            self.apply(store, self.request("event_other", prompt="prompt_other"))
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            self.assertEqual(
                [item["mutation_id"] for item in receipt["mutations"]],
                [
                    first["mutation_id"],
                    second["mutation_id"],
                ],
            )

    def test_dry_run_and_ordinary_capture_do_not_invent_a_handoff(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            capture.execute_capture(store, self.request("event_preview"))
            self.assertEqual(store.state(), before)
            self.assertEqual(
                self.receipts.reconstruct_receipt(store, self.identity)["status"], "pending"
            )
            self.apply(store, self.request("event_ordinary", prompt=None))
            self.assertEqual(
                self.receipts.reconstruct_receipt(store, self.identity)["status"], "pending"
            )

    def test_receipt_tampering_and_store_replacement_never_verify(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, self.request("event_saved"))
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            for field, value in (
                ("store_id", "different"),
                ("generation", True),
                ("task_id", "task_beta"),
            ):
                forged = {**receipt, field: value}
                with self.subTest(field=field):
                    self.assertFalse(self.receipts.verify_receipt(store, self.identity, forged))
            for field, value in (
                ("run_id", "run_retry"),
                ("watermark", True),
                ("changed_ids", []),
                ("revisions", {"event_saved": 2}),
            ):
                forged = copy.deepcopy(receipt)
                forged["mutations"][0][field] = value
                with self.subTest(field=field):
                    self.assertFalse(self.receipts.verify_receipt(store, self.identity, forged))
            receipt["mutations"].append(copy.deepcopy(receipt["mutations"][0]))
            self.assertFalse(self.receipts.verify_receipt(store, self.identity, receipt))

    def test_wrong_consumer_owner_is_refused_before_journal_disclosure(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, self.request("event_saved"))
            wrong = dataclasses.replace(self.identity, consumer_id="consumer_beta")
            with self.assertRaises(self.receipts.HandoffError):
                self.receipts.reconstruct_receipt(store, wrong)
            other = self.receipts.HandoffIdentity(
                "consumer_beta", "agent_beta", "task_beta", "prompt_alpha"
            )
            self.assertEqual(self.receipts.reconstruct_receipt(store, other)["status"], "pending")

    def test_malformed_prompt_is_refused_without_committing(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            for prompt in ("", "x" * 201, "a\nforged", True):
                with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                    capture.execute_capture(store, self.request("event_invalid", prompt=prompt))
            self.assertEqual(store.state(), before)


if __name__ == "__main__":
    unittest.main()
