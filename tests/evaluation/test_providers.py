"""Tests for provider command construction and packet parsing."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from typing import cast

from tedd_memory_eval.catalog import INERT_CANARY, NAMESPACE, scenarios
from tedd_memory_eval.contracts import BUDGETS
from tedd_memory_eval.providers import (
    ContextRecencyProvider,
    OracleProvider,
    RecallFtsProvider,
    canonical_payload_json,
    evaluation_token_count,
)
from tedd_memory_eval.sandbox import MemoryEvalSandbox


class FakeSandbox:
    def __init__(
        self,
        *,
        record_id: str = "lab_record",
        summary: str = "synthetic",
        truncated: object = False,
        payload_updates: dict[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.record_id = record_id
        self.summary = summary
        self.truncated = truncated
        self.payload_updates = payload_updates or {}

    def call(self, arguments: Sequence[str]) -> dict[str, object]:
        command = tuple(arguments)
        self.calls.append(command)
        record_id = command[command.index("--id") + 1] if "--id" in command else self.record_id
        payload: dict[str, object] = {
            "id": record_id,
            "namespace": NAMESPACE,
            "summary": self.summary,
        }
        payload.update(self.payload_updates)
        record: dict[str, object] = {
            "record_id": record_id,
            "collection": "observations",
            "payload": payload,
        }
        if command[0] == "context":
            return {
                "records": [record],
                "char_count": 41,
                "item_count": 1,
                "subscription_id": command[command.index("--subscription-id") + 1],
                "truncated": self.truncated,
            }
        if command[0] == "recall":
            return {
                "results": [record],
                "result_count": 1,
                "subject_ids": [command[command.index("--subject-id") + 1]],
                "truncated_by_char_budget": self.truncated,
            }
        return record


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = scenarios()[0]
        self.budget = BUDGETS[0]
        self.fake = FakeSandbox()
        self.sandbox = cast(MemoryEvalSandbox, self.fake)

    def test_recency_uses_exact_subscription_and_review_safe_mode(self) -> None:
        packet = ContextRecencyProvider().retrieve(self.sandbox, self.scenario, self.budget)

        command = self.fake.calls[0]
        self.assertEqual(command[0], "context")
        self.assertIn("--subscription-id", command)
        self.assertEqual(command[-2:], ("--mode", "review_safe"))
        self.assertEqual(packet.record_ids, ("lab_record",))
        self.assertGreater(packet.evaluation_tokens, 0)

    def test_evaluation_token_count_is_zero_only_for_an_empty_packet(self) -> None:
        self.assertEqual(evaluation_token_count(()), 0)
        self.assertGreater(evaluation_token_count((canonical_payload_json({}),)), 0)
        self.assertEqual(evaluation_token_count(('{"value":"alpha beta"}',)), 10)

    def test_canonical_payload_json_rejects_non_finite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            canonical_payload_json({"value": float("nan")})

    def test_recall_is_subject_scoped_and_bounded(self) -> None:
        RecallFtsProvider().retrieve(self.sandbox, self.scenario, self.budget)

        command = self.fake.calls[0]
        self.assertEqual(command[0], "recall")
        self.assertIn(self.scenario.subject_id, command)
        self.assertIn(str(self.budget.max_items), command)
        self.assertIn(str(self.budget.max_chars), command)

    def test_oracle_fetches_only_authored_exact_ids(self) -> None:
        packet = OracleProvider().retrieve(self.sandbox, self.scenario, self.budget)

        fetched_ids = [command[command.index("--id") + 1] for command in self.fake.calls]
        self.assertEqual(fetched_ids, list(self.scenario.oracle_ids))
        self.assertEqual(packet.record_ids, self.scenario.oracle_ids)

    def test_canary_detector_inspects_review_safe_text_not_record_id(self) -> None:
        scenario = next(item for item in scenarios() if item.case_id == "review_safe_sensitive")
        canary_id = scenario.canary_ids[0]
        leaking = cast(
            MemoryEvalSandbox,
            FakeSandbox(record_id=canary_id, summary=INERT_CANARY),
        )
        redacted = cast(
            MemoryEvalSandbox,
            FakeSandbox(record_id=canary_id, summary="[redacted]"),
        )

        leaking_packet = ContextRecencyProvider().retrieve(leaking, scenario, self.budget)
        redacted_packet = ContextRecencyProvider().retrieve(redacted, scenario, self.budget)

        self.assertEqual(leaking_packet.canary_text_exposure, 1)
        self.assertEqual(redacted_packet.canary_text_exposure, 0)

    def test_provider_rejects_non_boolean_truncation_marker(self) -> None:
        malformed = cast(MemoryEvalSandbox, FakeSandbox(truncated="false"))

        with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
            ContextRecencyProvider().retrieve(malformed, self.scenario, self.budget)

    def test_reference_detector_rejects_foreign_nested_reference(self) -> None:
        foreign = cast(
            MemoryEvalSandbox,
            FakeSandbox(payload_updates={"owner_refs": ["lab_subject_elsewhere"]}),
        )
        redacted = cast(
            MemoryEvalSandbox,
            FakeSandbox(payload_updates={"owner_refs": ["[withheld in review_safe mode]"]}),
        )

        foreign_packet = ContextRecencyProvider().retrieve(foreign, self.scenario, self.budget)
        redacted_packet = ContextRecencyProvider().retrieve(redacted, self.scenario, self.budget)

        self.assertEqual(foreign_packet.payload_reference_scope_exposure, 1)
        self.assertEqual(redacted_packet.payload_reference_scope_exposure, 0)

    def test_reference_detector_rejects_forbidden_fixture_and_fake_redaction(self) -> None:
        scope_case = next(item for item in scenarios() if item.case_id == "same_name_scopes")
        forbidden_id = scope_case.scope_forbidden_ids[0]
        sandbox = cast(
            MemoryEvalSandbox,
            FakeSandbox(payload_updates={"owner_refs": [forbidden_id, "[withheld:subject]"]}),
        )

        packet = ContextRecencyProvider().retrieve(sandbox, scope_case, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 2)

    def test_reference_detector_rejects_unmaterialized_same_scope_target(self) -> None:
        scope_case = next(item for item in scenarios() if item.case_id == "email_commitment")
        returned_id, unmaterialized_id = scope_case.required_ids[:2]
        sandbox = cast(
            MemoryEvalSandbox,
            FakeSandbox(
                record_id=returned_id,
                payload_updates={"evidence_refs": [unmaterialized_id]},
            ),
        )

        packet = ContextRecencyProvider().retrieve(sandbox, scope_case, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 1)

    def test_reference_detector_accepts_materialized_explicit_reference(self) -> None:
        record_id = self.scenario.required_ids[0]
        sandbox = cast(
            MemoryEvalSandbox,
            FakeSandbox(
                record_id=record_id,
                payload_updates={"refs": [{"target_id": record_id, "kind": "supporting_evidence"}]},
            ),
        )

        packet = ContextRecencyProvider().retrieve(sandbox, self.scenario, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 0)

    def test_reference_detector_fails_closed_on_malformed_explicit_reference(self) -> None:
        record_id = self.scenario.required_ids[0]
        malformed = cast(
            MemoryEvalSandbox,
            FakeSandbox(record_id=record_id, payload_updates={"refs": [{"kind": "missing"}]}),
        )

        packet = ContextRecencyProvider().retrieve(malformed, self.scenario, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 1)

    def test_reference_detector_covers_scalar_and_identity_evidence_refs(self) -> None:
        foreign_id = "lab_subject_elsewhere"
        sandbox = cast(
            MemoryEvalSandbox,
            FakeSandbox(
                payload_updates={
                    "target_entity_id": foreign_id,
                    "identity_keys": [
                        {
                            "scheme": "synthetic_scheme",
                            "namespace": "scheme_specific_namespace",
                            "value": "scheme-specific-value",
                            "evidence_refs": [foreign_id],
                        }
                    ],
                }
            ),
        )

        packet = ContextRecencyProvider().retrieve(sandbox, self.scenario, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 2)

    def test_reference_detector_fails_closed_on_malformed_reference(self) -> None:
        malformed = cast(
            MemoryEvalSandbox,
            FakeSandbox(payload_updates={"owner_refs": {"unexpected": "shape"}}),
        )

        packet = ContextRecencyProvider().retrieve(malformed, self.scenario, self.budget)

        self.assertEqual(packet.payload_reference_scope_exposure, 1)

    def test_reference_detector_fails_closed_on_null_reference_fields(self) -> None:
        malformed_payloads: tuple[dict[str, object], ...] = (
            {"evidence_refs": None},
            {"entity_refs": None},
            {"target_entity_id": None},
            {"refs": [{"target_id": None}]},
        )
        for payload_updates in malformed_payloads:
            with self.subTest(payload_updates=payload_updates):
                malformed = cast(
                    MemoryEvalSandbox,
                    FakeSandbox(payload_updates=payload_updates),
                )

                packet = ContextRecencyProvider().retrieve(
                    malformed,
                    self.scenario,
                    self.budget,
                )

                self.assertEqual(packet.payload_reference_scope_exposure, 1)


if __name__ == "__main__":
    unittest.main()
