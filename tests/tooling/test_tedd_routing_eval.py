"""Behavioral contract tests for the private routing evaluation CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scripts import tedd_routing_eval as evaluator
from tests.support.paths import REPOSITORY_ROOT as ROOT
from tests.support.paths import REPOSITORY_SCRIPTS as SCRIPTS

EXPECTED_ROUTES = {
    "tiny_sequential_edit": "local",
    "parallel_read_only_investigations": "bounded_subagent",
    "tightly_coupled_writer_refactor": "local",
    "long_work_with_status_question": "bounded_subagent",
    "rapid_five_message_burst": "local",
    "fresh_context_restrictions": "bounded_subagent",
    "peer_dependency_message": "bounded_subagent",
    "capacity_exhausted_fallback": "local",
    "explicit_persistent_task": "persistent_task",
    "unrequested_persistent_task": "local",
    "independent_read_only_reviewer": "bounded_subagent",
    "interrupted_worker_partial_write": "bounded_subagent",
}


def passing_observations() -> dict[str, object]:
    """Return literal, sanitized observations that satisfy the frozen corpus."""

    observations: list[dict[str, object]] = []
    for scenario_id, route in EXPECTED_ROUTES.items():
        observations.append(
            {
                "scenario_id": scenario_id,
                "route": route,
                "persistent_task_created": scenario_id == "explicit_persistent_task",
                "heartbeat_created": False,
                "prohibited_side_effects": False,
                "writer_ownership": True,
                "handoff_complete": True,
                "status_verified": True,
                "bounded_lane_available": scenario_id != "capacity_exhausted_fallback",
                "fresh_context_restrictions_included": scenario_id == "fresh_context_restrictions",
                "dependency_update_reached_owner": scenario_id == "peer_dependency_message",
                "peer_messaging_available": scenario_id == "peer_dependency_message",
                "dependency_update_delivered_directly": scenario_id == "peer_dependency_message",
                "initially_delegated": scenario_id == "interrupted_worker_partial_write",
                "partial_shared_state_inspected": scenario_id == "interrupted_worker_partial_write",
                "fallback_used": scenario_id
                in {"capacity_exhausted_fallback", "interrupted_worker_partial_write"},
            }
        )
    return {"schema": "tedd-routing-observations-v1", "observations": observations}


class TeddRoutingEvaluationTests(unittest.TestCase):
    def test_heartbeat_creation_must_match_explicit_scenario_authorization(self) -> None:
        """Reject both missed authorized monitoring and unsolicited scheduling."""

        for authorized, created, failed in (
            (False, False, 0),
            (False, True, 1),
            (True, False, 1),
            (True, True, 0),
        ):
            with self.subTest(authorized=authorized, created=created):
                route = evaluator.Route.SCHEDULED_HEARTBEAT if authorized else evaluator.Route.LOCAL
                # A neutral scorer fixture, not a change to the frozen public catalog.
                scenario = evaluator.Scenario(
                    "synthetic_monitoring", (route,), heartbeat_authorized=authorized
                )
                observation = cast(list[dict[str, object]], passing_observations()["observations"])[
                    0
                ]
                observation.update(
                    scenario_id="synthetic_monitoring",
                    route=route.value,
                    heartbeat_created=created,
                )
                with mock.patch.object(evaluator, "SCENARIOS", (scenario,)):
                    validated = evaluator.validate_observations(
                        cast(
                            evaluator.JsonValue,
                            {
                                "schema": "tedd-routing-observations-v1",
                                "observations": [observation],
                            },
                        )
                    )
                    report = evaluator.score_observations(validated)
                self.assertEqual(report["failed_count"], failed)
                self.assertEqual(
                    report["failures"],
                    [
                        {
                            "scenario_id": "synthetic_monitoring",
                            "reasons": ["heartbeat authorization"],
                        }
                    ]
                    if failed
                    else [],
                )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the real evaluator without accessing a graph or agent runtime."""

        return subprocess.run(
            [sys.executable, str(SCRIPTS / "tedd_routing_eval.py"), *arguments],
            capture_output=True,
            check=False,
            text=True,
            cwd=ROOT,
        )

    def test_catalog_is_stable_json_with_all_sanitized_scenarios(self) -> None:
        result = self.run_cli("catalog", "--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        catalog = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(catalog["schema"], "tedd-routing-catalog-v1")
        scenarios = cast(list[dict[str, object]], catalog["scenarios"])
        self.assertEqual([item["scenario_id"] for item in scenarios], list(EXPECTED_ROUTES))
        self.assertNotIn("prompt", result.stdout.lower())
        self.assertNotIn("codex://", result.stdout)

    def test_score_accepts_complete_passing_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observations_path = Path(temporary) / "observations.json"
            observations_path.write_text(json.dumps(passing_observations()), encoding="utf-8")

            result = self.run_cli("score", "--observations", str(observations_path), "--pretty")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(report["passed_count"], 12)
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["schema"], "tedd-routing-score-v1")

    def test_score_reports_per_scenario_safety_failure_without_schema_error(self) -> None:
        payload = passing_observations()
        observations = cast(list[dict[str, object]], payload["observations"])
        observations[0]["writer_ownership"] = False
        with tempfile.TemporaryDirectory() as temporary:
            observations_path = Path(temporary) / "observations.json"
            observations_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("score", "--observations", str(observations_path))

        self.assertEqual(result.returncode, 1, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        failures = cast(list[dict[str, object]], report["failures"])
        self.assertEqual(failures[0]["scenario_id"], "tiny_sequential_edit")
        self.assertIn("writer ownership", cast(list[str], failures[0]["reasons"]))

    def test_score_rejects_duplicate_missing_unknown_fields_and_non_boolean_safety(self) -> None:
        malformed_payloads = {
            "duplicate": lambda payload: cast(
                list[dict[str, object]], payload["observations"]
            ).append(dict(cast(list[dict[str, object]], payload["observations"])[0])),
            "missing": lambda payload: cast(list[dict[str, object]], payload["observations"]).pop(),
            "unknown_field": lambda payload: cast(list[dict[str, object]], payload["observations"])[
                0
            ].update({"unexpected": True}),
            "non_boolean": lambda payload: cast(list[dict[str, object]], payload["observations"])[
                0
            ].update({"status_verified": "yes"}),
        }
        for name, mutate in malformed_payloads.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                payload = passing_observations()
                mutate(payload)
                observations_path = Path(temporary) / "observations.json"
                observations_path.write_text(json.dumps(payload), encoding="utf-8")

                result = self.run_cli("score", "--observations", str(observations_path))

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid observations", result.stderr)

    def test_score_rejects_unknown_scenario_and_invalid_route(self) -> None:
        for name, field, value in (
            ("unknown_scenario", "scenario_id", "unrecognized_scenario"),
            ("invalid_route", "route", "automatic"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                payload = passing_observations()
                observations = cast(list[dict[str, object]], payload["observations"])
                observations[0][field] = value
                observations_path = Path(temporary) / "observations.json"
                observations_path.write_text(json.dumps(payload), encoding="utf-8")

                result = self.run_cli("score", "--observations", str(observations_path))

                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid observations", result.stderr)

    def test_score_rejects_duplicate_json_members_without_echoing_input(self) -> None:
        duplicate_documents = {
            "document": (
                '{"schema":"tedd-routing-observations-v1",'
                '"schema":"secret-document-value","observations":[]}'
            ),
            "observation": json.dumps(passing_observations()).replace(
                '"route": "local",',
                '"route": "local", "route": "secret-observation-value",',
                1,
            ),
        }
        for level, contents in duplicate_documents.items():
            with self.subTest(level=level), tempfile.TemporaryDirectory() as temporary:
                observations_path = Path(temporary) / "observations.json"
                observations_path.write_text(contents, encoding="utf-8")

                result = self.run_cli("score", "--observations", str(observations_path))

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid observations", result.stderr)
                self.assertNotIn("secret-", result.stderr)

    def test_capability_aware_routes_require_preferred_or_fallback_evidence(self) -> None:
        payload = passing_observations()
        observations = cast(list[dict[str, object]], payload["observations"])
        by_id = {cast(str, item["scenario_id"]): item for item in observations}
        parallel = by_id["parallel_read_only_investigations"]
        parallel["bounded_lane_available"] = False
        parallel["route"] = "local"
        parallel["fallback_used"] = True
        fresh_context = by_id["fresh_context_restrictions"]
        fresh_context["route"] = "local"
        peer_dependency = by_id["peer_dependency_message"]
        peer_dependency["dependency_update_reached_owner"] = False
        peer_dependency["dependency_update_delivered_directly"] = False
        interrupted = by_id["interrupted_worker_partial_write"]
        interrupted["initially_delegated"] = False
        interrupted["partial_shared_state_inspected"] = False
        with tempfile.TemporaryDirectory() as temporary:
            observations_path = Path(temporary) / "observations.json"
            observations_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("score", "--observations", str(observations_path))

        self.assertEqual(result.returncode, 1, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        failures = {
            cast(str, item["scenario_id"]): cast(list[str], item["reasons"])
            for item in cast(list[dict[str, object]], report["failures"])
        }
        self.assertNotIn("parallel_read_only_investigations", failures)
        self.assertIn(
            "bounded lane required when available", failures["fresh_context_restrictions"]
        )
        self.assertIn(
            "dependency update did not reach owning lane", failures["peer_dependency_message"]
        )
        self.assertIn(
            "peer messaging delivery did not match availability",
            failures["peer_dependency_message"],
        )
        self.assertIn(
            "worker was not initially delegated", failures["interrupted_worker_partial_write"]
        )
        self.assertIn(
            "partial shared state was not inspected before fallback",
            failures["interrupted_worker_partial_write"],
        )

    def test_capacity_exhaustion_cannot_claim_a_bounded_lane_was_available(self) -> None:
        payload = passing_observations()
        observations = cast(list[dict[str, object]], payload["observations"])
        capacity = next(
            item for item in observations if item["scenario_id"] == "capacity_exhausted_fallback"
        )
        capacity["bounded_lane_available"] = True
        capacity["route"] = "bounded_subagent"
        capacity["fallback_used"] = False
        with tempfile.TemporaryDirectory() as temporary:
            observations_path = Path(temporary) / "observations.json"
            observations_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_cli("score", "--observations", str(observations_path))

        self.assertEqual(result.returncode, 1, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        failures = {
            cast(str, item["scenario_id"]): cast(list[str], item["reasons"])
            for item in cast(list[dict[str, object]], report["failures"])
        }
        self.assertEqual(
            failures["capacity_exhausted_fallback"],
            ["capacity exhaustion requires no bounded lane"],
        )


if __name__ == "__main__":
    unittest.main()
