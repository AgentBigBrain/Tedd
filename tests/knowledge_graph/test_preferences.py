"""Scoped preferences through the reviewed v4 capture and read boundaries."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import subprocess
import sys
from unittest import mock

import knowledge_graph as graph
import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture
import knowledge_graph_v4_cli as cli
from knowledge_graph_v4_integration import sanitize_review_safe_value

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.graph_cases import PLAN_KEY
from tests.support.paths import GRAPH_SCRIPTS

PREFERENCE_SCOPE = "entity_scope_operator_preferences_fixture"
VALID_PREFERENCE = (
    "Prefer: short commit titles. When: committing code. Because: they are easier to scan."
)


class PreferenceFixture(HandoffFixture):
    def packet_request(self, **records):
        request = self.request("unused", prompt=None)
        packet = json.loads(request.input_bytes)
        packet.pop("events")
        packet.update(records)
        return dataclasses.replace(request, input_bytes=json.dumps(packet).encode())

    @staticmethod
    def preference(*, subjects=None, value=VALID_PREFERENCE, **fields):
        return {
            "surface_id": "surface_preference_commit_titles",
            "family": "preference",
            "key": "commit_titles",
            "value": value,
            "subject_refs": ["entity_project"] if subjects is None else subjects,
            **fields,
        }

    def seed_preference_scope(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    entities=[
                        {
                            "id": PREFERENCE_SCOPE,
                            "type": "scope",
                            "namespace": "operator_fixture",
                            "name": "Operator preferences",
                        }
                    ]
                ),
            )


class PreferenceCaptureTests(PreferenceFixture):
    def test_preference_requires_three_nonempty_labels_in_order(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            for value in (
                "Use short titles",
                "Prefer: short titles. Because: readable. When: committing.",
                "Prefer: When: committing. Because: readable.",
                "Prefer: short. When: Because: readable.",
                "Prefer: short. When: committing. Because:",
                "Prefer: short. When: committing. Because: Prefer: something else.",
                42,
                {"Prefer": "short"},
            ):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "preference"):
                    capture.execute_capture(
                        store, self.packet_request(claims=[self.preference(value=value)])
                    )
            self.assertEqual(before, store.state())

    def test_preference_requires_exactly_one_canonical_subject(self):
        self.seed_preference_scope()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            for subjects in ([], ["entity_project", PREFERENCE_SCOPE]):
                with (
                    self.subTest(subjects=subjects),
                    self.assertRaisesRegex(ValueError, "preference"),
                ):
                    capture.execute_capture(
                        store, self.packet_request(claims=[self.preference(subjects=subjects)])
                    )

    def test_global_scope_accepts_only_preference_claims_and_their_surfaces(self):
        self.seed_preference_scope()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            result = self.apply(
                store,
                self.packet_request(claims=[self.preference(subjects=[PREFERENCE_SCOPE])]),
            )
            self.assertEqual("captured", result["status"])
            store.subscribe(
                "consumer_alpha", {"entity_ids": [PREFERENCE_SCOPE]}, subscription_id="prefs"
            )
            records = store.context_pack("prefs")["records"]
            self.assertEqual(
                {"entities", "claims", "claim_surfaces"}, {r["collection"] for r in records}
            )
            self.assertEqual(
                [PREFERENCE_SCOPE],
                [r["record_id"] for r in records if r["collection"] == "entities"],
            )
            self.assertTrue(store.validate(full=True)["valid"])

    def test_client_preference_cannot_cite_global_scope_as_extra_context(self):
        self.seed_preference_scope()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            with self.assertRaisesRegex(ValueError, "preference"):
                capture.execute_capture(
                    store,
                    self.packet_request(claims=[self.preference(entity_refs=[PREFERENCE_SCOPE])]),
                )

    def test_other_record_collections_cannot_reference_global_scope(self):
        self.seed_preference_scope()
        cases = {
            "observations": {
                "family": "work",
                "key": "result",
                "value": "Client detail",
            },
            "events": {"kind": "work", "title": "Client incident"},
            "decisions": {"title": "Client decision", "summary": "Client detail"},
            "open_loops": {"title": "Client follow-up", "summary": "Client detail"},
            "receipts": {"kind": "work", "title": "Client evidence"},
        }
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            for collection, record in cases.items():
                with (
                    self.subTest(collection=collection),
                    self.assertRaisesRegex(ValueError, "preference"),
                ):
                    capture.execute_capture(
                        store,
                        self.packet_request(
                            **{collection: [{**record, "entity_refs": [PREFERENCE_SCOPE]}]}
                        ),
                    )
            self.assertEqual(before, store.state())

    def test_non_preference_claim_cannot_use_global_scope(self):
        self.seed_preference_scope()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            with self.assertRaisesRegex(ValueError, "preference"):
                capture.execute_capture(
                    store,
                    self.packet_request(
                        claims=[self.preference(subjects=[PREFERENCE_SCOPE], family="client_fact")]
                    ),
                )

    def test_scope_cannot_redirect_or_become_relation_context(self):
        self.seed_preference_scope()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            packets = (
                {
                    "relations": [
                        {"source": "entity_project", "target": PREFERENCE_SCOPE, "type": "uses"}
                    ]
                },
                {"entity_redirects": [{"from": "entity_project", "to": PREFERENCE_SCOPE}]},
                {"entity_redirects": [{"from": PREFERENCE_SCOPE, "to": "entity_project"}]},
            )
            for packet in packets:
                with self.subTest(packet=packet), self.assertRaisesRegex(ValueError, "preference"):
                    capture.execute_capture(
                        store,
                        dataclasses.replace(self.packet_request(**packet), allow_redirect=True),
                    )
            self.assertEqual(before, store.state())

    def test_source_reference_and_wrong_scope_type_are_refused(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            request = self.packet_request()
            packet = json.loads(request.input_bytes)
            packet["source"]["entity_refs"] = [PREFERENCE_SCOPE]
            with self.assertRaisesRegex(ValueError, "preference"):
                capture.execute_capture(
                    store, dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
                )
            with self.assertRaisesRegex(ValueError, "preference"):
                capture.execute_capture(
                    store,
                    self.packet_request(
                        entities=[{"id": PREFERENCE_SCOPE, "type": "client", "name": "Not a scope"}]
                    ),
                )


class PreferenceRecallTests(PreferenceFixture):
    def test_real_mcp_endpoint_discloses_preferences_without_changing_the_graph(self):
        self.seed_preferences()
        config = self.root / "reader-config.json"
        graph.atomic_write(config, json.dumps({"store_path": str(self.store_path)}))
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_context",
                    "arguments": {
                        "agent_id": "agent_alpha",
                        "task_id": "task_alpha",
                        "consumer_id": "consumer_alpha",
                        "subscription_id": "prefs_alpha",
                        "max_items": 1,
                        "max_chars": 12000,
                    },
                },
            },
        ]
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            before, consumer = store.state(), store.consumer_status("consumer_alpha")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "tedd_graph_mcp.py"),
                "--config",
                str(config),
            ],
            input=("\n".join(json.dumps(item) for item in requests) + "\n").encode(),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        context = responses[1]["result"]["structuredContent"]
        self.assertEqual("preferences", next(iter(context)))
        self.assertEqual(3, len(context["preferences"]["items"]))
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 12000)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(before, store.state())
            self.assertEqual(consumer, store.consumer_status("consumer_alpha"))

    def seed_preferences(self, *, extra=0, hidden=0, value=VALID_PREFERENCE):
        self.seed_preference_scope()
        claims = [
            self.preference(
                surface_id="surface_project",
                key="project",
                entity_refs=["entity_project"],
                value=value,
            ),
            self.preference(subjects=["entity_client"], surface_id="surface_client", key="client"),
            self.preference(subjects=[PREFERENCE_SCOPE], surface_id="surface_global", key="global"),
            self.preference(
                subjects=["entity_other"],
                surface_id="surface_other",
                key="other",
                entity_refs=["entity_project"],
            ),
        ]
        claims.extend(
            self.preference(surface_id=f"surface_extra_{i:02}", key=f"extra_{i:02}")
            for i in range(extra)
        )
        claims.extend(
            self.preference(
                surface_id=f"surface_hidden_{i:02}", key=f"hidden_{i:02}", sensitive=True
            )
            for i in range(hidden)
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    entities=[
                        {"id": "entity_client", "type": "client", "name": "Client"},
                        {"id": "entity_other", "type": "project", "name": "Other"},
                    ],
                    claims=claims,
                ),
            )
            store.subscribe(
                "consumer_alpha",
                {"entity_ids": ["entity_project", "entity_client", PREFERENCE_SCOPE]},
                subscription_id="prefs_alpha",
            )
            store.subscribe(
                "consumer_beta", {"entity_ids": ["entity_other"]}, subscription_id="prefs_beta"
            )

    def context(self, budget=50_000, *, plan=False):
        arguments = [
            "context",
            "--subscription-id",
            "prefs_alpha",
            "--max-items",
            "1",
            "--max-chars",
            str(budget),
            "--mode",
            "operator_full",
        ]
        if plan:
            arguments += ["--plan", "--fingerprint", "preference-read"]
        with mock.patch.object(v4, "utc_now", return_value="2026-02-10T12:00:00Z"):
            result = cli.handle_v4_command(
                graph.build_parser().parse_args(arguments),
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            )
        return {key: value for key, value in result.items() if key not in {"command", "status"}}

    def test_scoped_subject_order_and_read_only_selection(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(
                callable(getattr(store, "preferences", None)), "D3 preference selection missing"
            )
            before = store.state()
            writes = []
            # SQLite registers the built-in virtual table lazily in its in-memory
            # schema. Warm it before trapping durable write opcodes.
            store.connection.execute("SELECT value FROM json_each('[]')").fetchall()

            def refuse_writes(action, table, column, database, trigger):
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    writes.append(table)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store.connection.set_authorizer(refuse_writes)
            result = store.preferences("prefs_alpha")
            self.assertEqual(
                ["entity_project", "entity_client", PREFERENCE_SCOPE],
                [item["subject_id"] for item in result["items"]],
            )
            self.assertEqual(
                ["entity_other"],
                [item["subject_id"] for item in store.preferences("prefs_beta")["items"]],
            )
            self.assertEqual(store.state(), before)
            self.assertEqual([], writes)

    def test_hidden_preferences_do_not_displace_visible_results_before_the_cap(self):
        self.seed_preferences(extra=15)
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.preferences("prefs_alpha")
            self.assertEqual(12, len(before["items"]))
            self.assertTrue(before["truncated"])
            self.apply(
                store,
                self.packet_request(
                    claims=[
                        self.preference(
                            key=f"private_{i}",
                            surface_id=f"surface_private_{i}",
                            sensitive=True,
                            valid_from="2026-02-01T00:00:00Z",
                        )
                        for i in range(20)
                    ]
                ),
            )
            self.assertEqual(before, store.preferences("prefs_alpha"))

    def test_future_and_quarantined_values_do_not_become_current_preferences(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    claims=[
                        self.preference(
                            key="future",
                            surface_id="surface_future",
                            valid_from="2099-01-01T00:00:00Z",
                        ),
                        self.preference(
                            key="guess",
                            surface_id="surface_guess",
                            source_tier="assistant_inference",
                        ),
                    ]
                ),
            )
            self.assertEqual(3, len(store.preferences("prefs_alpha")["items"]))

    def test_current_conflicts_keep_their_status_instead_of_a_silent_winner(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    claims=[
                        self.preference(
                            key="project",
                            surface_id="surface_project",
                            value="Prefer: detailed titles. When: committing. Because: context helps.",
                        )
                    ]
                ),
            )
            items = [
                item
                for item in store.preferences("prefs_alpha")["items"]
                if item["subject_id"] == "entity_project"
            ]
            self.assertEqual(2, len(items))
            self.assertEqual({"conflicting"}, {item["lifecycle"] for item in items})

    def test_review_safe_disclosure_preserves_recent_records_and_sanitizes_before_caps(self):
        self.seed_preferences(
            value="Prefer: " + '"/a" ' * 40 + "When: committing. Because: context."
        )
        args = graph.build_parser().parse_args(
            [
                "context",
                "--subscription-id",
                "prefs_alpha",
                "--max-items",
                "1",
                "--max-chars",
                "50000",
            ]
        )
        result = cli.handle_v4_command(
            args,
            db_path=self.store_path,
            plan_hmac_key=PLAN_KEY,
            review_sanitizer=sanitize_review_safe_value,
        )
        section = result["preferences"]
        self.assertNotIn('"/a"', json.dumps(section))
        self.assertLessEqual(len(json.dumps(section, ensure_ascii=False)), 2000)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            baseline = sanitize_review_safe_value(
                store.context_pack("prefs_alpha", max_items=1, max_chars=50000)
            )
        self.assertEqual(baseline["records"], result["records"])

    def test_context_preferences_are_first_bounded_and_in_the_read_set(self):
        self.seed_preferences(extra=15, hidden=20)
        result = self.context(plan=True)
        self.assertEqual("preferences", next(iter(result)))
        section = result["preferences"]
        self.assertEqual("stated preferences, with dates", section["label"])
        self.assertLessEqual(len(section["items"]), 12)
        self.assertLessEqual(len(json.dumps(section, ensure_ascii=False)), 2000)
        self.assertTrue(section["truncated"])
        self.assertNotIn("hidden_", json.dumps(section))
        covered = {
            item["record_id"]
            for item in v4.verify_plan_token(result["plan_token"], PLAN_KEY)["read_set"]
        }
        self.assertLessEqual({item["id"] for item in section["items"]}, covered)

    def test_budget_exhaustion_is_not_reported_as_no_preferences(self):
        self.seed_preferences(
            value="Prefer: "
            + "long synthetic explanation " * 100
            + "When: committing. Because: context."
        )
        result = self.context(1200)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 1200)
        self.assertTrue(result["preferences"]["omitted"])
        self.assertEqual("budget_exhausted", result["preferences"]["reason"])

    def test_supersession_retains_history_but_recalls_only_current_value(self):
        self.seed_preferences()
        replacement = "Prefer: descriptive titles. When: committing. Because: context helps."
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.preferences("prefs_alpha")["items"][0]
            request = self.packet_request(
                claims=[
                    self.preference(
                        surface_id="surface_project",
                        key="project",
                        value=replacement,
                        valid_from="2026-02-01T00:00:00Z",
                        conflict_policy="replace",
                    )
                ]
            )
            self.apply(store, dataclasses.replace(request, allow_replace=True))
            current = store.preferences("prefs_alpha")["items"][0]
            self.assertEqual(replacement, current["value"])
            old = store.get_record(before["id"])["payload"]
            self.assertFalse(old["active"])
            self.assertEqual("2026-02-01T00:00:00Z", old["valid_to"])
            self.assertTrue(store.validate(full=True)["valid"])

    def test_scheduled_replacement_keeps_the_preference_valid_until_its_change_date(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            previous = store.preferences("prefs_alpha")["items"][0]
            replacement = (
                "Prefer: scheduled detail. When: committing. Because: the policy changes later."
            )
            request = self.packet_request(
                claims=[
                    self.preference(
                        surface_id="surface_project",
                        key="project",
                        value=replacement,
                        valid_from="2099-01-01T00:00:00Z",
                        conflict_policy="replace",
                    )
                ]
            )
            self.apply(store, dataclasses.replace(request, allow_replace=True))
            with mock.patch.object(v4, "utc_now", return_value="2098-12-31T23:59:59Z"):
                items = store.preferences("prefs_alpha")["items"]
                self.assertEqual(
                    [previous["id"]],
                    [item["id"] for item in items if item["subject_id"] == "entity_project"],
                )
            with mock.patch.object(v4, "utc_now", return_value="2099-01-01T00:00:00Z"):
                items = store.preferences("prefs_alpha")["items"]
                self.assertEqual(
                    [replacement],
                    [item["value"] for item in items if item["subject_id"] == "entity_project"],
                )

    def test_fractional_timestamps_are_ordered_and_filtered_as_instants(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    claims=[
                        self.preference(
                            key="whole_second",
                            surface_id="surface_whole",
                            valid_from="2026-02-10T12:00:00Z",
                        ),
                        self.preference(
                            key="microsecond",
                            surface_id="surface_micro",
                            valid_from="2026-02-10T12:00:00.000001Z",
                        ),
                        self.preference(
                            key="half_second",
                            surface_id="surface_half",
                            valid_from="2026-02-10T12:00:00.500000Z",
                        ),
                    ]
                ),
            )
            with mock.patch.object(v4, "utc_now", return_value="2026-02-10T12:00:00+00:00"):
                surfaces = [
                    item["surface_ref"] for item in store.preferences("prefs_alpha")["items"]
                ]
                self.assertIn("surface_whole", surfaces)
                self.assertNotIn("surface_micro", surfaces)
                self.assertNotIn("surface_half", surfaces)
            with mock.patch.object(v4, "utc_now", return_value="2026-02-10T12:00:01Z"):
                surfaces = [
                    item["surface_ref"] for item in store.preferences("prefs_alpha")["items"]
                ]
                self.assertEqual(["surface_half", "surface_micro", "surface_whole"], surfaces[:3])

    def test_scheduled_replacement_keeps_effective_conflicts_visible_before_caps(self):
        self.seed_preferences()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(
                store,
                self.packet_request(
                    claims=[
                        self.preference(
                            key="project",
                            surface_id="surface_project",
                            value="Prefer: disputed titles. When: committing. Because: context helps.",
                        )
                    ]
                ),
            )
            request = self.packet_request(
                claims=[
                    self.preference(
                        key="project",
                        surface_id="surface_project",
                        value="Prefer: future settled titles. When: committing. Because: the owner confirmed a new default.",
                        valid_from="2099-01-01T00:00:00Z",
                        conflict_policy="replace",
                    )
                ]
            )
            self.apply(store, dataclasses.replace(request, allow_replace=True))
            with mock.patch.object(v4, "utc_now", return_value="2026-02-10T12:00:00Z"):
                items = [
                    item
                    for item in store.preferences("prefs_alpha")["items"]
                    if item["surface_ref"] == "surface_project"
                ]
                self.assertEqual(2, len(items))
                self.assertTrue(all(item.get("effective_conflict") for item in items))
            with mock.patch.object(v4, "utc_now", return_value="2099-01-01T00:00:00Z"):
                items = [
                    item
                    for item in store.preferences("prefs_alpha")["items"]
                    if item["surface_ref"] == "surface_project"
                ]
                self.assertEqual(1, len(items))
                self.assertFalse(items[0]["effective_conflict"])
