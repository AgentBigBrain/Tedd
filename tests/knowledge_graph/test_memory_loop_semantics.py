"""D1 capture semantics, separate from the required native model challenge run."""

from __future__ import annotations

import dataclasses
import json

import knowledge_graph_v4 as v4

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.graph_cases import PLAN_KEY


class MemoryLoopSemanticsTests(HandoffFixture):
    def packet_request(self, **records):
        request = self.request("event_unused")
        packet = json.loads(request.input_bytes)
        packet.pop("events")
        packet.update(records)
        return dataclasses.replace(request, input_bytes=json.dumps(packet).encode())

    def test_one_milestone_preserves_decision_approval_proposal_fix_and_obligation(self):
        request = self.packet_request(
            entities=[
                {
                    "id": "entity_mira",
                    "ref": "mira",
                    "type": "person",
                    "namespace": "client_alpha",
                    "name": "Mira",
                }
            ],
            decisions=[
                {
                    "id": "decision_palette",
                    "title": "Use the teal palette",
                    "summary": "Mira approved the teal palette.",
                    "rationale": "It meets the contrast requirement.",
                    "decided_at": "2026-01-03T12:00:00Z",
                    "status": "accepted",
                    "entity_refs": ["entity_project", "mira"],
                },
                {
                    "id": "decision_draft",
                    "title": "Consider amber palette",
                    "summary": "Unaccepted proposal.",
                    "decided_at": "2026-01-03T12:00:00Z",
                    "status": "proposed",
                    "entity_refs": ["entity_project"],
                },
            ],
            events=[
                {
                    "id": "event_reusable",
                    "kind": "reusable_fix",
                    "title": "Timeout when thumbnailing",
                    "summary": "Symptom: thumbnail timeout Cause: unbounded work Fix: cap batches Verified by: synthetic regression Applies when: thumbnail workers",
                    "occurred_at": "2026-01-03T12:00:00Z",
                    "entity_refs": ["entity_project"],
                }
            ],
            open_loops=[
                {
                    "id": "loop_preview",
                    "title": "Send the preview",
                    "summary": "Mira will send the preview.",
                    "created_at": "2026-01-03T12:00:00Z",
                    "checkpoint_at": "2026-01-05T12:00:00Z",
                    "owner_refs": ["mira"],
                    "entity_refs": ["entity_project"],
                    "next_action": "Send preview",
                    "terminal_condition": "Preview received",
                    "status": "open",
                }
            ],
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, request)
            receipt = self.receipts.reconstruct_receipt(store, self.identity)
            self.assertEqual(1, len(receipt["mutations"]))
            self.assertTrue(store.validate(full=True)["valid"])
        # A different task reads the saved data after the saving connection closes.
        with v4.open_store(self.store_path) as store:
            store.subscribe(
                "consumer_beta", {"entity_ids": ["entity_project"]}, subscription_id="scope_fresh"
            )
            results = store.search_subscription("scope_fresh", "palette")
            found = {record["record_id"]: record["payload"] for record in results["results"]}
            self.assertEqual("accepted", found["decision_palette"]["status"])
            self.assertEqual("proposed", found["decision_draft"]["status"])
            self.assertIn("Mira", found["decision_palette"]["summary"])
            self.assertIn("contrast", found["decision_palette"]["rationale"])
            self.assertEqual(
                ["event_reusable"],
                [
                    r["record_id"]
                    for r in store.search_subscription("scope_fresh", "time*")["results"]
                ],
            )
            loop = store.get_record("loop_preview")["payload"]
            self.assertEqual(["entity_mira"], loop["owner_refs"])
            self.assertEqual("2026-01-05T12:00:00Z", loop["checkpoint_at"])

    def test_correction_supersedes_same_surface_and_preserves_history(self):
        claim = {
            "surface_id": "surface_palette",
            "family": "design",
            "key": "palette",
            "value": "blue",
            "valid_from": "2026-01-03T12:00:00Z",
            "source_tier": "validated_structured_candidate",
            "subject_refs": ["entity_project"],
            "entity_refs": ["entity_project"],
        }
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            first = self.apply(store, self.packet_request(claims=[claim]))
            old_id = next(record for record in first["changed_ids"] if record.startswith("claim_"))
            replacement = {
                **claim,
                "value": "teal",
                "valid_from": "2026-01-04T12:00:00Z",
                "conflict_policy": "replace",
            }
            request = self.packet_request(claims=[replacement])
            packet = json.loads(request.input_bytes)
            packet["source"]["observed_at"] = "2026-01-04T12:00:00Z"
            second = self.apply(
                store,
                dataclasses.replace(
                    request, input_bytes=json.dumps(packet).encode(), allow_replace=True
                ),
            )
            new_id = next(
                record
                for record in second["changed_ids"]
                if record.startswith("claim_") and record != old_id
            )
            old = store.get_record(old_id)["payload"]
            new = store.get_record(new_id)["payload"]
            self.assertEqual("surface_palette", new["surface_ref"])
            self.assertEqual("teal", new["value"])
            self.assertEqual(new_id, old["superseded_by"])
            self.assertFalse(old["active"])
            self.assertEqual("2026-01-04T12:00:00Z", old["valid_to"])
            self.assertEqual("blue", store.get_record(old_id, revision=1)["payload"]["value"])
            self.assertTrue(store.validate(full=True)["valid"])
