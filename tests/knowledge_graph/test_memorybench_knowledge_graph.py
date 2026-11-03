#!/usr/bin/env python3
"""Isolated end-to-end tests for the read-only Tedd MemoryBench runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
import memorybench_knowledge_graph as bench  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIR

FIXED_TIME = "2026-01-01T12:00:00Z"
PLAN_KEY = bytes.fromhex("55" * 32)
PRIVATE_SENTINEL = "PRIVATE-MEMORYBENCH-SENTINEL-74f9"


def private_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    set_private_path_mode(path.parent, 0o700)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(payload)
    set_private_path_mode(path, 0o600)
    return payload


def change(
    record_id: str,
    collection: str,
    payload: dict[str, object],
    search_text: str,
    *,
    refs: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "collection": collection,
        "payload": {"id": record_id, **payload},
        "change_kind": "create",
        "refs": [
            {"target_id": target_id, "kind": "entity", "ordinal": index}
            for index, target_id in enumerate(refs)
        ],
        "search_text": search_text,
    }


class MemoryBenchCase(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tedd-memorybench-test-", dir=private_parent
        )
        # macOS exposes its canonical private temp tree through /var. Keep the
        # fixture root canonical so assertions refer to the same path hardened
        # by the report writer.
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)
        self.store_path = self.root / "graph.sqlite3"
        self.config_path = self.root / "config.json"
        self.suite_path = self.root / "cases.json"
        with v4.create_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.apply_changes(
                [
                    change(
                        "entity_alpha",
                        "entities",
                        {
                            "namespace": "client_alpha",
                            "type": "client",
                            "name": "Alpha Financial",
                            "aliases": ["Alpha"],
                            "status": "current",
                            "lifecycle": "active",
                            "sensitive": False,
                        },
                        "entity_alpha Alpha Financial client_alpha",
                    ),
                    change(
                        "entity_beta",
                        "entities",
                        {
                            "namespace": "client_beta",
                            "type": "client",
                            "name": "Beta Financial",
                            "aliases": ["Beta"],
                            "status": "current",
                            "lifecycle": "active",
                            "sensitive": False,
                        },
                        "entity_beta Beta Financial client_beta",
                    ),
                    change(
                        "surface_alpha_status",
                        "claim_surfaces",
                        {
                            "namespace": "client_alpha",
                            "family": "deployment",
                            "key": "alpha.status",
                            "subject_refs": ["entity_alpha"],
                            "cardinality": "single",
                            "created_at": FIXED_TIME,
                            "last_observed_at": FIXED_TIME,
                            "source_tier": "validated_structured_candidate",
                            "lifecycle": "active",
                            "sensitive": False,
                        },
                        "surface_alpha_status Alpha deployment status",
                        refs=("entity_alpha",),
                    ),
                    change(
                        "claim_alpha_current",
                        "claims",
                        {
                            "namespace": "client_alpha",
                            "surface_ref": "surface_alpha_status",
                            "family": "deployment",
                            "key": "alpha.status",
                            "value": "Alpha is current on staging",
                            "valid_from": FIXED_TIME,
                            "valid_to": None,
                            "asserted_at": FIXED_TIME,
                            "active": True,
                            "lifecycle": "current",
                            "source_tier": "validated_structured_candidate",
                            "entity_refs": ["entity_alpha"],
                            "sensitive": False,
                        },
                        "claim_alpha_current Alpha current staging deployment status",
                        refs=("entity_alpha", "surface_alpha_status"),
                    ),
                    change(
                        "claim_alpha_old",
                        "claims",
                        {
                            "namespace": "client_alpha",
                            "surface_ref": "surface_alpha_status",
                            "family": "deployment",
                            "key": "alpha.status",
                            "value": "Alpha historical production state",
                            "valid_from": FIXED_TIME,
                            "valid_to": FIXED_TIME,
                            "asserted_at": FIXED_TIME,
                            "active": False,
                            "lifecycle": "superseded",
                            "source_tier": "validated_structured_candidate",
                            "entity_refs": ["entity_alpha"],
                            "sensitive": False,
                        },
                        "claim_alpha_old Alpha historical production status",
                        refs=("entity_alpha", "surface_alpha_status"),
                    ),
                    change(
                        "receipt_private",
                        "receipts",
                        {
                            "namespace": "client_alpha",
                            "kind": "verification",
                            "title": "Private receipt",
                            "summary": PRIVATE_SENTINEL,
                            "verified_at": FIXED_TIME,
                            "verified": True,
                            "status": "recorded",
                            "lifecycle": "active",
                            "entity_refs": ["entity_alpha"],
                            "sensitive": True,
                        },
                        "receipt_private privacy sentinel",
                        refs=("entity_alpha",),
                    ),
                ],
                fingerprint="memorybench-fixture",
                producer_agent_id="memorybench-test",
                task_id="memorybench-test",
                run_id="memorybench-test-run",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
            self.state = store.state()
        private_json(
            self.config_path,
            {
                "schema_version": "v2",
                "canonical_store_format": "sqlite_v4_wal",
                "store_path": str(self.store_path),
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_report_writer_hardens_a_new_parent_before_validation(self) -> None:
        report = self.root / "new-reports" / "result.json"
        real_harden = bench.set_private_path_mode
        hardened: list[Path] = []

        def harden(path: Path, mode: int) -> None:
            hardened.append(path)
            real_harden(path, mode)

        with mock.patch.object(bench, "set_private_path_mode", harden):
            bench.atomic_private_json(
                report,
                {"schema_version": bench.REPORT_SCHEMA_VERSION},
                force=False,
            )

        self.assertIn(report.parent, hardened)
        self.assertTrue(report.is_file())

    def suite(self, cases: list[dict[str, object]], **updates: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": bench.SCHEMA_VERSION,
            "suite_id": "isolated-memorybench-v1",
            "expected_store_id": self.state["store_id"],
            "expected_generation": self.state["generation"],
            "defaults": {"mode": "review_safe", "limit": 5, "max_chars": 12000},
            "thresholds": {
                "min_macro_precision_at_k": 0.0,
                "min_macro_recall_at_k": 0.0,
                "min_macro_mrr": 0.0,
            },
            "cases": cases,
        }
        value.update(updates)
        return value

    def run_process(
        self, suite: dict[str, object], *extra: str, expected: int = 0
    ) -> dict[str, object]:
        private_json(self.suite_path, suite)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "memorybench_knowledge_graph.py"),
                "--suite",
                str(self.suite_path),
                "--config",
                str(self.config_path),
                *extra,
            ],
            cwd=SCRIPT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def passing_cases(self) -> list[dict[str, object]]:
        return [
            {
                "id": "exact_identity",
                "query": "entity_alpha",
                "relevant_record_ids": ["entity_alpha"],
                "required": [
                    {
                        "record_id": "entity_alpha",
                        "max_rank": 1,
                        "collection": "entities",
                        "payload_equals": {
                            "namespace": "client_alpha",
                            "lifecycle": "active",
                        },
                    }
                ],
                "forbidden_record_ids": ["entity_beta"],
                "forbidden_namespaces": ["client_beta"],
            },
            {
                "id": "current_claim",
                "query": "claim_alpha_current",
                "relevant_record_ids": ["claim_alpha_current"],
                "relevant_surface_ids": ["surface_alpha_status"],
                "required": [
                    {
                        "record_id": "claim_alpha_current",
                        "max_rank": 1,
                        "collection": "claims",
                        "payload_equals": {"lifecycle": "current", "active": True},
                    }
                ],
            },
            {
                "id": "client_scope",
                "query": "Alpha",
                "limit": 10,
                "subject_ids": ["entity_alpha"],
                "relevant_record_ids": ["claim_alpha_current", "claim_alpha_old"],
                "required": [{"record_id": "claim_alpha_current", "max_rank": 10}],
                "forbidden_record_ids": ["entity_beta"],
                "forbidden_reference_ids": ["entity_beta"],
                "forbidden_namespaces": ["client_beta"],
            },
            {
                "id": "review_safe_private",
                "query": "receipt_private",
                "collections": ["receipts"],
                "relevant_record_ids": ["receipt_private"],
                "required": [
                    {
                        "record_id": "receipt_private",
                        "max_rank": 1,
                        "payload_equals": {"summary": "[redacted in review_safe mode]"},
                    }
                ],
                "forbidden_output_substrings": [PRIVATE_SENTINEL],
            },
        ]

    def test_end_to_end_uses_production_cli_and_review_safe_boundary(self) -> None:
        report = self.run_process(self.suite(self.passing_cases()), "--repeat", "2")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["summary"]["case_count"], 4)
        self.assertEqual(report["summary"]["fail_count"], 0)
        self.assertEqual(report["repeat"], 2)
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(report))
        private_case = next(
            item for item in report["cases"] if item["case_id"] == "review_safe_private"
        )
        self.assertEqual(private_case["ordered_record_ids"], ["receipt_private"])

    def test_mcp_search_measures_scoped_matching_without_registering_or_acknowledging(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.register_consumer("bench_alpha", agent_id="bench_agent", task_id="bench_task")
            store.subscribe(
                "bench_alpha",
                {"entity_ids": ["entity_alpha"], "record_ids": ["entity_alpha"]},
                subscription_id="bench_scope",
                start_watermark=0,
            )
            before = store.state(), store.consumer_status("bench_alpha")
        session = {
            "consumer_id": "bench_alpha",
            "agent_id": "bench_agent",
            "task_id": "bench_task",
            "subscription_id": "bench_scope",
        }
        report = self.run_process(
            self.suite(
                [
                    {
                        "id": "all_words",
                        "query": "Alpha staging",
                        "session": session,
                        "required": [{"record_id": "claim_alpha_current", "max_rank": 1}],
                        "forbidden_record_ids": [
                            "entity_beta",
                            "claim_alpha_old",
                            "receipt_private",
                        ],
                    },
                    {
                        "id": "foreign_identity",
                        "query": "entity_beta",
                        "session": session,
                        "max_result_count": 0,
                        "forbidden_record_ids": ["entity_beta"],
                    },
                    {
                        "id": "private_identity",
                        "query": "receipt_private",
                        "session": session,
                        "max_result_count": 0,
                        "forbidden_output_substrings": [PRIVATE_SENTINEL],
                    },
                ],
                schema_version="tedd-memorybench-v2",
                read_path="mcp_search_memory",
            ),
            "--repeat",
            "2",
        )
        self.assertEqual("pass", report["status"])
        self.assertEqual("mcp_search_memory", report["read_path"])
        self.assertEqual(["claim_alpha_current"], report["cases"][0]["ordered_record_ids"])
        self.assertEqual([0, 0], [case["result_count"] for case in report["cases"][1:]])
        self.assertEqual(64, len(report["implementation_sha256"]))
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(before, (store.state(), store.consumer_status("bench_alpha")))

    def test_mcp_reader_refusal_is_invalid_not_an_empty_success(self):
        report = self.run_process(
            self.suite(
                [
                    {
                        "id": "missing_reader",
                        "query": "entity_alpha",
                        "max_result_count": 0,
                        "session": {
                            "consumer_id": "absent",
                            "agent_id": "absent",
                            "task_id": "absent",
                            "subscription_id": "absent",
                        },
                    }
                ],
                schema_version="tedd-memorybench-v2",
                read_path="mcp_search_memory",
            ),
            expected=2,
        )
        self.assertEqual("invalid", report["status"])
        self.assertFalse(report.get("cases"))
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(self.state, store.state())

    def test_mcp_transport_rejects_bad_handshakes_and_protocol_failures(self):
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search_memory"}]}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "structuredContent": {
                        "subscription_id": "bench_scope",
                        "results": [],
                        "result_count": 0,
                    }
                },
            },
        ]
        arguments = {
            "subscription_id": "bench_scope",
            "consumer_id": "reader",
            "agent_id": "agent",
            "task_id": "task",
            "query": "teal",
        }
        variants = [
            [*responses[:1], responses[1], {**responses[2], "id": 2}],
            [{**responses[0], "id": True}, *responses[1:]],
            [{**responses[0], "result": {}}, *responses[1:]],
            [responses[0], {**responses[1], "result": {"tools": []}}, responses[2]],
            [
                *responses[:2],
                {
                    **responses[2],
                    "result": {"isError": True, "structuredContent": {"category": "scope_denied"}},
                },
            ],
            [
                *responses[:2],
                {
                    **responses[2],
                    "result": {
                        "structuredContent": {
                            "subscription_id": "wrong_scope",
                            "result_count": 0,
                            "results": [],
                        }
                    },
                },
            ],
        ]
        for index, variant in enumerate(variants):
            with self.subTest(variant=index):
                data = "".join(json.dumps(item) + "\n" for item in variant).encode()
                process = subprocess.CompletedProcess([], 0, data, b"")
                with mock.patch.object(bench.subprocess, "run", return_value=process):
                    with self.assertRaises(bench.MemoryBenchError):
                        bench.run_mcp(self.config_path, "search_memory", arguments, 10)
        for stdout, stderr, code in ((b"not json\n", b"", 0), (b"", b"fault", 0), (b"", b"", 1)):
            with self.subTest(stdout=stdout, stderr=stderr, code=code):
                with mock.patch.object(
                    bench.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, stdout, stderr),
                ):
                    with self.assertRaises(bench.MemoryBenchError):
                        bench.run_mcp(self.config_path, "search_memory", arguments, 10)
        with mock.patch.object(
            bench.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1)
        ):
            with self.assertRaisesRegex(bench.MemoryBenchError, "timed out"):
                bench.run_mcp(self.config_path, "search_memory", arguments, 1)

    def test_v2_rejects_ambiguous_or_malformed_scope_and_context_options(self):
        session = {
            "consumer_id": "reader",
            "agent_id": "agent",
            "task_id": "task",
            "subscription_id": "scope",
        }
        search = {"id": "search", "query": "teal", "session": session, "max_result_count": 0}
        variants = [
            ("mcp_search_memory", {**search, "session": {}}),
            ("mcp_search_memory", {**search, "subject_ids": ["entity_beta"]}),
            ("mcp_search_memory", {**search, "collections": ["claims"]}),
            ("mcp_search_memory", {**search, "context_section": "overdue"}),
            ("mcp_recall_context", {**search, "context_section": []}),
            ("mcp_recall_context", {**search, "context_section": "preferences"}),
        ]
        for read_path, case in variants:
            with self.subTest(read_path=read_path, case=case):
                raw = self.suite([case], schema_version="tedd-memorybench-v2", read_path=read_path)
                raw["defaults"] = {"max_chars": 12000}
                with self.assertRaises(bench.MemoryBenchError):
                    bench.normalize_suite(raw, json.dumps(raw).encode())

    def test_context_safety_checks_other_sections_not_only_the_scored_section(self):
        raw = self.suite(
            [
                {
                    "id": "preferences",
                    "session": {
                        "consumer_id": "reader",
                        "agent_id": "agent",
                        "task_id": "task",
                        "subscription_id": "scope",
                    },
                    "context_section": "preferences",
                    "max_result_count": 0,
                    "forbidden_record_ids": ["foreign_loop"],
                    "forbidden_reference_ids": ["entity_beta"],
                    "forbidden_namespaces": ["client_beta"],
                }
            ],
            schema_version="tedd-memorybench-v2",
            read_path="mcp_recall_context",
        )
        raw["defaults"] = {"max_chars": 12000}
        case = bench.normalize_suite(raw, json.dumps(raw).encode())["cases"][0]
        response = {
            "command": "recall",
            "status": "ok",
            "results": [],
            "result_count": 0,
            "context_section": "preferences",
            "omitted": False,
            "response_chars": 100,
            "truncated": False,
            "disclosed_records": [
                {
                    "record_id": "foreign_loop",
                    "payload": {"namespace": "client_beta", "entity_refs": ["entity_beta"]},
                }
            ],
        }
        scored = bench.evaluate_case(case, response, json.dumps(response), 1)
        self.assertEqual("fail", scored["status"])
        self.assertEqual(3, len(scored["gates"]))

    def test_existing_reader_with_wrong_task_is_refused_by_the_actual_mcp(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.register_consumer("reader", agent_id="agent", task_id="actual_task")
            store.subscribe("reader", {"record_ids": ["entity_alpha"]}, subscription_id="scope")
            before = store.state(), store.consumer_status("reader")
        report = self.run_process(
            self.suite(
                [
                    {
                        "id": "wrong_owner",
                        "query": "entity_alpha",
                        "max_result_count": 0,
                        "session": {
                            "consumer_id": "reader",
                            "agent_id": "agent",
                            "task_id": "wrong_task",
                            "subscription_id": "scope",
                        },
                    }
                ],
                schema_version="tedd-memorybench-v2",
                read_path="mcp_search_memory",
            ),
            expected=2,
        )
        self.assertEqual("invalid", report["status"])
        self.assertIn("MCP tool refused", report["reason"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(before, (store.state(), store.consumer_status("reader")))

    def test_a_reader_change_during_measurement_is_inconclusive(self):
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe("reader", {"record_ids": ["entity_alpha"]}, subscription_id="scope")
        raw = self.suite(
            [
                {
                    "id": "identity",
                    "query": "entity_alpha",
                    "session": {
                        "consumer_id": "reader",
                        "agent_id": "agent",
                        "task_id": "task",
                        "subscription_id": "scope",
                    },
                    "required": [{"record_id": "entity_alpha", "max_rank": 1}],
                }
            ],
            schema_version="tedd-memorybench-v2",
            read_path="mcp_search_memory",
        )
        actual_read = bench.run_mcp

        def read_and_change_reader(*args, **kwargs):
            response = actual_read(*args, **kwargs)
            with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
                store.subscribe("reader", {"record_ids": ["entity_beta"]}, subscription_id="extra")
            return response

        with mock.patch.object(bench, "run_mcp", side_effect=read_and_change_reader):
            report, code = bench.run_benchmark(
                bench.normalize_suite(raw, json.dumps(raw).encode()), self.config_path
            )
        self.assertEqual(2, code)
        self.assertEqual("inconclusive", report["status"])
        self.assertEqual("pass", report["cases"][0]["status"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(self.state, store.state())

    def test_reader_fingerprint_refuses_an_incomplete_subscription_list(self) -> None:
        suite = {"cases": [{"session": {"consumer_id": "reader"}}]}
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                status = {"active_subscriptions_truncated": truncated}
                with mock.patch.object(bench, "run_cli", return_value=(status, "{}", 0.0)):
                    if truncated:
                        with self.assertRaisesRegex(
                            bench.MemoryBenchError, "subscription.*truncated"
                        ):
                            bench.reader_state_digest(suite, self.config_path, 30.0)
                    else:
                        self.assertEqual(
                            64, len(bench.reader_state_digest(suite, self.config_path, 30.0))
                        )

    def test_required_forbidden_and_threshold_gates_exit_one(self) -> None:
        cases = [
            {
                "id": "deliberate_gate_failure",
                "query": "entity_alpha",
                "relevant_record_ids": ["entity_beta"],
                "required": [{"record_id": "entity_beta", "max_rank": 1}],
                "forbidden_record_ids": ["entity_alpha"],
            }
        ]
        suite = self.suite(cases)
        suite["thresholds"] = {
            "min_macro_precision_at_k": 1.0,
            "min_macro_recall_at_k": 1.0,
            "min_macro_mrr": 1.0,
        }
        report = self.run_process(suite, expected=1)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["cases"][0]["gates"])
        self.assertTrue(report["threshold_failures"])

    def test_v2_cli_records_its_path_and_refuses_implementation_drift(self) -> None:
        raw = self.suite(
            [self.passing_cases()[0]], schema_version="tedd-memorybench-v2", read_path="cli_recall"
        )
        suite = bench.normalize_suite(raw, json.dumps(raw).encode())
        for changed in (False, True):
            with self.subTest(implementation_changed=changed):
                with mock.patch.object(
                    bench,
                    "implementation_digest",
                    side_effect=["a" * 64, ("b" if changed else "a") * 64],
                ):
                    report, code = bench.run_benchmark(suite, self.config_path)
                self.assertEqual(2 if changed else 0, code)
                self.assertEqual("inconclusive" if changed else "pass", report["status"])
                self.assertEqual("pass", report["cases"][0]["status"])
                self.assertEqual("cli_recall", report["cases"][0]["read_path"])
                self.assertEqual(5, report["cases"][0]["precision_k"])
                self.assertIsNone(report["reader_state_sha256"])

    def test_private_report_replacement_accepts_both_versions_not_arbitrary_json(self) -> None:
        target = self.root / "reports" / "result.json"
        for schema in (bench.REPORT_SCHEMA_VERSION, bench.REPORT_SCHEMA_VERSION_V2, []):
            with self.subTest(schema=schema):
                private_json(target, {"schema_version": schema})
                replacement = {"schema_version": bench.REPORT_SCHEMA_VERSION_V2, "status": "pass"}
                if isinstance(schema, list):
                    with self.assertRaisesRegex(
                        bench.MemoryBenchError, "only an existing MemoryBench"
                    ):
                        bench.atomic_private_json(target, replacement, force=True)
                    self.assertEqual({"schema_version": []}, json.loads(target.read_text()))
                else:
                    bench.atomic_private_json(target, replacement, force=True)
                    self.assertEqual(replacement, json.loads(target.read_text()))

    def test_report_contains_no_payload_bodies_and_is_private(self) -> None:
        report_path = self.root / "reports" / "result.json"
        report = self.run_process(self.suite(self.passing_cases()), "--output", str(report_path))
        self.assertEqual(report["status"], "pass")
        self.assertNotIn("cases", report)
        if os.name != "nt":
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(report_path.read_text())
        self.assertEqual(saved["suite_sha256"], report["suite_sha256"])
        self.assertIn("cases", saved)
        self.assertNotIn("payload", json.dumps(saved))
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(saved))

    def test_report_output_is_confined_and_force_replaces_only_reports(self) -> None:
        suite = self.suite(self.passing_cases())
        original_suite = private_json(self.suite_path, suite)
        report = self.run_process(
            suite,
            "--output",
            str(self.suite_path),
            "--force",
            expected=2,
        )
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(self.suite_path.read_bytes(), original_suite)

        unrelated = self.root / "reports" / "unrelated.json"
        unrelated_payload = private_json(unrelated, {"owner": "user"})
        report = self.run_process(
            suite,
            "--output",
            str(unrelated),
            "--force",
            expected=2,
        )
        self.assertEqual(report["status"], "invalid")
        self.assertIn("existing MemoryBench report", report["reason"])
        self.assertEqual(unrelated.read_bytes(), unrelated_payload)

    def test_precision_uses_configured_k_and_required_targets_are_relevant(self) -> None:
        raw = self.suite(
            [
                {
                    "id": "precision_denominator",
                    "query": "entity_alpha",
                    "limit": 5,
                    "required": [{"record_id": "entity_alpha", "max_rank": 1}],
                }
            ]
        )
        case = bench.normalize_suite(raw, json.dumps(raw).encode())["cases"][0]
        self.assertEqual(case["relevant_record_ids"], ["entity_alpha"])
        output = {
            "command": "recall",
            "status": "ok",
            "result_count": 1,
            "results": [
                {
                    "record_id": "entity_alpha",
                    "collection": "entities",
                    "payload": {"id": "entity_alpha"},
                }
            ],
        }
        report = bench.evaluate_case(case, output, json.dumps(output), 1.0)
        self.assertEqual(report["precision_at_k"], 0.2)
        self.assertEqual(report["recall_at_k"], 1.0)

    def test_surface_requirement_accepts_a_later_matching_current_claim(self) -> None:
        raw = self.suite(
            [
                {
                    "id": "surface_later_match",
                    "query": "Alpha deployment status",
                    "required": [
                        {
                            "surface_id": "surface_alpha_status",
                            "max_rank": 2,
                            "collection": "claims",
                            "payload_equals": {"active": True, "lifecycle": "current"},
                        }
                    ],
                }
            ]
        )
        case = bench.normalize_suite(raw, json.dumps(raw).encode())["cases"][0]
        output = {
            "command": "recall",
            "status": "ok",
            "result_count": 2,
            "results": [
                {
                    "record_id": "claim_alpha_old",
                    "collection": "claims",
                    "payload": {
                        "surface_ref": "surface_alpha_status",
                        "active": False,
                        "lifecycle": "superseded",
                    },
                },
                {
                    "record_id": "claim_alpha_current",
                    "collection": "claims",
                    "payload": {
                        "surface_ref": "surface_alpha_status",
                        "active": True,
                        "lifecycle": "current",
                    },
                },
            ],
        }
        report = bench.evaluate_case(case, output, json.dumps(output), 1.0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["gates"], [])

    def test_surface_assertions_must_hold_on_one_matching_result(self) -> None:
        raw = self.suite(
            [
                {
                    "id": "surface_split_assertions",
                    "query": "Alpha deployment status",
                    "required": [
                        {
                            "surface_id": "surface_alpha_status",
                            "max_rank": 2,
                            "collection": "claims",
                            "payload_equals": {"active": True, "lifecycle": "current"},
                        }
                    ],
                }
            ]
        )
        case = bench.normalize_suite(raw, json.dumps(raw).encode())["cases"][0]
        output = {
            "command": "recall",
            "status": "ok",
            "result_count": 2,
            "results": [
                {
                    "record_id": "claim_alpha_old",
                    "collection": "claims",
                    "payload": {
                        "surface_ref": "surface_alpha_status",
                        "active": True,
                        "lifecycle": "superseded",
                    },
                },
                {
                    "record_id": "claim_alpha_current",
                    "collection": "claims",
                    "payload": {
                        "surface_ref": "surface_alpha_status",
                        "active": False,
                        "lifecycle": "current",
                    },
                },
            ],
        }
        report = bench.evaluate_case(case, output, json.dumps(output), 1.0)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("satisfying all" in gate for gate in report["gates"]))

    def test_noop_case_and_vacuous_ranking_pair_are_rejected(self) -> None:
        noop = self.suite([{"id": "noop", "query": "entity_alpha"}])
        self.assertEqual(self.run_process(noop, expected=2)["status"], "invalid")

        ranking = self.suite(
            [
                {
                    "id": "vacuous_ranking",
                    "query": "Alpha status",
                    "ranking_pairs": [
                        {
                            "higher_record_id": "claim_alpha_current",
                            "lower_record_id": "claim_alpha_old",
                        }
                    ],
                }
            ]
        )
        report = self.run_process(ranking, expected=2)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("must also be a required record target", report["reason"])

    def test_missing_higher_rank_target_fails_when_lower_is_present(self) -> None:
        raw = self.suite(
            [
                {
                    "id": "missing_higher",
                    "query": "Alpha status",
                    "required": [{"record_id": "claim_alpha_current", "max_rank": 2}],
                    "ranking_pairs": [
                        {
                            "higher_record_id": "claim_alpha_current",
                            "lower_record_id": "claim_alpha_old",
                        }
                    ],
                }
            ]
        )
        case = bench.normalize_suite(raw, json.dumps(raw).encode())["cases"][0]
        output = {
            "command": "recall",
            "status": "ok",
            "result_count": 1,
            "results": [
                {
                    "record_id": "claim_alpha_old",
                    "collection": "claims",
                    "payload": {},
                }
            ],
        }
        report = bench.evaluate_case(case, output, json.dumps(output), 1.0)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("indeterminate" in gate for gate in report["gates"]))

    def test_positive_threshold_fails_when_metric_is_unavailable(self) -> None:
        suite = self.suite(
            [
                {
                    "id": "hard_negative_only",
                    "query": "entity_alpha",
                    "forbidden_record_ids": ["entity_beta"],
                }
            ]
        )
        suite["thresholds"] = {
            "min_macro_precision_at_k": 0.0,
            "min_macro_recall_at_k": 0.1,
            "min_macro_mrr": 0.1,
        }
        report = self.run_process(suite, expected=1)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["threshold_failures"]), 2)
        self.assertTrue(all("unavailable" in item for item in report["threshold_failures"]))

    def test_non_finite_timeout_is_rejected(self) -> None:
        report = self.run_process(self.suite(self.passing_cases()), "--timeout", "nan", expected=2)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("timeout", report["reason"])

    def test_wrong_store_identity_is_invalid(self) -> None:
        report = self.run_process(
            self.suite(self.passing_cases(), expected_store_id="different-store"),
            expected=2,
        )
        self.assertEqual(report["status"], "invalid")
        self.assertIn("store_id", report["reason"])

    def test_operator_full_duplicate_cases_and_unsafe_ids_are_rejected(self) -> None:
        bad_mode = self.suite(self.passing_cases())
        bad_mode["defaults"] = {"mode": "operator_full"}
        self.assertEqual(self.run_process(bad_mode, expected=2)["status"], "invalid")

        duplicate = self.suite([self.passing_cases()[0], self.passing_cases()[0]])
        self.assertEqual(self.run_process(duplicate, expected=2)["status"], "invalid")

        unsafe = self.suite([{"id": "unsafe id", "query": "entity_alpha"}])
        self.assertEqual(self.run_process(unsafe, expected=2)["status"], "invalid")

    @unittest.skipIf(os.name == "nt", "POSIX mode-bit policy is not a Windows ACL test")
    def test_suite_permissions_are_enforced(self) -> None:
        private_json(self.suite_path, self.suite(self.passing_cases()))
        self.suite_path.chmod(0o644)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "memorybench_knowledge_graph.py"),
                "--suite",
                str(self.suite_path),
                "--config",
                str(self.config_path),
            ],
            cwd=SCRIPT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("must not be accessible", json.loads(completed.stdout)["reason"])

    def test_watermark_change_is_inconclusive(self) -> None:
        raw = self.suite([self.passing_cases()[0]])
        payload = json.dumps(raw, sort_keys=True).encode()
        suite = bench.normalize_suite(raw, payload)
        start = {
            "command": "status",
            "status": "ok",
            "graph": {
                "store_id": self.state["store_id"],
                "generation": 1,
                "watermark": 1,
            },
        }
        recall = {
            "command": "recall",
            "status": "ok",
            "result_count": 1,
            "results": [
                {
                    "record_id": "entity_alpha",
                    "collection": "entities",
                    "payload": {
                        "id": "entity_alpha",
                        "namespace": "client_alpha",
                        "lifecycle": "active",
                    },
                }
            ],
        }
        end = json.loads(json.dumps(start))
        end["graph"]["watermark"] = 2
        with mock.patch.object(
            bench,
            "run_cli",
            side_effect=[(start, "{}", 1.0), (recall, "{}", 1.0), (end, "{}", 1.0)],
        ):
            report, exit_code = bench.run_benchmark(suite, self.config_path)
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "inconclusive")

    def test_cli_failure_is_invalid_without_payload_dump(self) -> None:
        raw = self.suite([self.passing_cases()[0]])
        suite = bench.normalize_suite(raw, json.dumps(raw).encode())
        status = {
            "command": "status",
            "status": "ok",
            "graph": {
                "store_id": self.state["store_id"],
                "generation": 1,
                "watermark": 1,
            },
        }
        with mock.patch.object(
            bench,
            "run_cli",
            side_effect=[
                (status, "{}", 1.0),
                bench.MemoryBenchError("production knowledge-graph CLI failed for recall (exit 1)"),
                (status, "{}", 1.0),
            ],
        ):
            report, exit_code = bench.run_benchmark(suite, self.config_path)
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "invalid")
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
