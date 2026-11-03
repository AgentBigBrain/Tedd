"""Deterministic, payload-free aggregation for memory evaluation results."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .catalog import (
    FIXED_TIME,
    INERT_CANARY,
    INERT_SENSITIVE,
    INERT_UNTRUSTED,
    catalog_digest,
)
from .contracts import (
    BUDGETS,
    EVALUATOR_TOKENIZER_ID,
    PROTOCOL_ID,
    REPORT_SCHEMA_ID,
    SCENARIO_CATALOG_ID,
    Observation,
    Scenario,
    StateSnapshot,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GRAPH_RUNTIME_ROOT = (
    REPOSITORY_ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts"
).resolve()
GRAPH_RUNTIME_PATHS = tuple(
    path for path in GRAPH_RUNTIME_ROOT.rglob("*.py") if not path.name.startswith("test_")
)
EVALUATOR_PATHS = (
    REPOSITORY_ROOT / "scripts/memory_eval_lab.py",
    *(
        path
        for path in (REPOSITORY_ROOT / "scripts/tedd_memory_eval").glob("*.py")
        if not path.name.startswith("test_")
    ),
    REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL.md",
    REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL-V2.md",
    *GRAPH_RUNTIME_PATHS,
)
GIT_REPRODUCIBILITY_PATHS = (
    "research/memory-eval-lab",
    "scripts/memory_eval_lab.py",
    "scripts/tedd_memory_eval",
    "scripts/verify_release.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts",
)
EXPECTED_VARIANTS = (
    "no_memory_v1",
    "context_recency_v1",
    "recall_fts_v1",
    "oracle_v1",
)


def _rounded(value: float) -> float:
    return round(value, 4)


def _mean(values: Iterable[float]) -> float:
    material = tuple(values)
    return _rounded(statistics.fmean(material)) if material else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
    return round(ordered[index], 2)


def _first_repetitions(observations: Sequence[Observation]) -> tuple[Observation, ...]:
    return tuple(item for item in observations if item.repetition == 0)


def _deterministic(observations: Sequence[Observation]) -> bool:
    by_case: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for item in observations:
        by_case[item.case_id].append(item.ordered_ids)
    return all(len(set(orders)) == 1 for orders in by_case.values())


def _semantic_digest(observations: Sequence[Observation]) -> str:
    material = [
        {
            "variant": item.variant,
            "budget": item.budget,
            "case": item.case_id,
            "required_collections": item.required_collections,
            "repetition": item.repetition,
            "ordered_ids": item.ordered_ids,
            "required_recall": item.required_recall,
            "precision": item.precision,
            "sufficient": item.sufficient,
            "stale": item.stale_exposure,
            "untrusted": item.untrusted_exposure,
            "sensitive": item.sensitive_exposure,
            "canary": item.canary_exposure,
            "assertion_failures": item.assertion_failures,
            "untrusted_text": item.untrusted_text_exposure,
            "sensitive_text": item.sensitive_text_exposure,
            "canary_text": item.canary_text_exposure,
            "payload_namespace": item.payload_namespace_exposure,
            "payload_reference_scope": item.payload_reference_scope_exposure,
            "scope": item.scope_exposure,
            "namespace": item.namespace_exposure,
            "overflow": item.budget_overflow,
            "items": item.item_count,
            "chars": item.payload_chars,
            "evaluation_tokens": item.evaluation_tokens,
            "truncated": item.truncated,
        }
        for item in observations
    ]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluator_digest() -> str:
    """Hash evaluator code and protocol independently from Git state."""

    digest = hashlib.sha256()
    for path in sorted(EVALUATOR_PATHS):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _git_metadata() -> dict[str, object]:
    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *arguments),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10.0,
        )

    try:
        revision_result = run("rev-parse", "HEAD")
        status_result = run(
            "status",
            "--porcelain",
            "--",
            *GIT_REPRODUCIBILITY_PATHS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"base_revision": "unavailable", "research_scope_dirty": True}
    revision = revision_result.stdout.strip()
    if (
        revision_result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None
    ):
        revision = "unavailable"
    return {
        "base_revision": revision,
        "research_scope_dirty": (
            status_result.returncode != 0 or bool(status_result.stdout.strip())
        ),
    }


def _fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
    return True


def reproducibility_metadata(catalog: tuple[Scenario, ...]) -> dict[str, object]:
    """Return sanitized code, suite, environment, and dependency identity."""

    return {
        "catalog_sha256": catalog_digest(catalog),
        "evaluator_sha256": evaluator_digest(),
        "evaluator_tokenizer": EVALUATOR_TOKENIZER_ID,
        "fixture_time": FIXED_TIME,
        "git": _git_metadata(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "fts5_available": _fts5_available(),
        "operating_system": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "byte_order": sys.byteorder,
    }


def _exposure_scenarios(observations: Sequence[Observation], field: str) -> int:
    return sum(int(getattr(item, field) > 0) for item in observations)


def _collection_coverage(observations: Sequence[Observation], collection: str) -> float:
    relevant = [
        item.required_recall for item in observations if collection in item.required_collections
    ]
    return _mean(relevant)


def _aggregate(
    observations: Sequence[Observation],
    *,
    variant: str,
    budget: str,
    critical_cases: frozenset[str],
) -> dict[str, object]:
    measured = [item for item in observations if item.variant == variant and item.budget == budget]
    first = _first_repetitions(measured)
    order_scores = [
        item.current_over_stale_accuracy
        for item in first
        if item.current_over_stale_accuracy is not None
    ]
    worst_ranks = [
        item.worst_required_rank for item in first if item.worst_required_rank is not None
    ]
    total_chars = sum(item.payload_chars for item in first)
    total_evaluator_tokens = sum(item.evaluation_tokens for item in first)
    total_hits = sum(item.relevant_hits for item in first)
    critical_insufficient = sorted(
        item.case_id for item in first if item.case_id in critical_cases and not item.sufficient
    )
    return {
        "variant": variant,
        "budget": budget,
        "scenario_count": len(first),
        "measured_sample_count": len(measured),
        "utility": {
            "required_recall": _mean(item.required_recall for item in first),
            "precision": _mean(item.precision for item in first),
            "mean_reciprocal_rank": _mean(item.reciprocal_rank for item in first),
            "worst_required_rank": max(worst_ranks) if worst_ranks else None,
            "missing_required_rank_count": sum(item.worst_required_rank is None for item in first),
            "current_over_stale_accuracy": _mean(order_scores),
            "safe_context_sufficiency": _mean(float(item.sufficient) for item in first),
            "decision_coverage": _collection_coverage(first, "decisions"),
            "open_loop_coverage": _collection_coverage(first, "open_loops"),
            "commitment_coverage": _mean(
                item.required_recall for item in first if item.family == "commitment"
            ),
            "critical_insufficient_cases": critical_insufficient,
            "calibration_sufficiency": _mean(
                float(item.sufficient) for item in first if item.partition == "calibration"
            ),
            "confirmation_sufficiency": _mean(
                float(item.sufficient) for item in first if item.partition == "confirmation"
            ),
        },
        "safety": {
            "stale_record_exposure_scenarios": _exposure_scenarios(first, "stale_exposure"),
            "untrusted_record_exposure_scenarios": _exposure_scenarios(first, "untrusted_exposure"),
            "sensitive_record_exposure_scenarios": _exposure_scenarios(first, "sensitive_exposure"),
            "canary_record_exposure_scenarios": _exposure_scenarios(first, "canary_exposure"),
            "untrusted_text_exposure_scenarios": _exposure_scenarios(
                first, "untrusted_text_exposure"
            ),
            "sensitive_text_exposure_scenarios": _exposure_scenarios(
                first, "sensitive_text_exposure"
            ),
            "canary_text_exposure_scenarios": _exposure_scenarios(first, "canary_text_exposure"),
            "payload_assertion_failure_scenarios": _exposure_scenarios(first, "assertion_failures"),
            "payload_namespace_exposure_scenarios": _exposure_scenarios(
                first, "payload_namespace_exposure"
            ),
            "payload_reference_scope_exposure_scenarios": _exposure_scenarios(
                first, "payload_reference_scope_exposure"
            ),
            "scope_exposure_scenarios": _exposure_scenarios(first, "scope_exposure"),
            "non_synthetic_id_exposure_scenarios": _exposure_scenarios(first, "namespace_exposure"),
        },
        "boundedness": {
            "budget_overflow_scenarios": sum(item.budget_overflow for item in first),
            "truncated_scenarios": sum(item.truncated for item in first),
            "mean_items": _mean(float(item.item_count) for item in first),
            "mean_payload_chars": _mean(float(item.payload_chars) for item in first),
            "mean_evaluator_tokens": _mean(float(item.evaluation_tokens) for item in first),
            "relevant_hits_per_1000_chars": (
                _rounded((total_hits * 1_000) / total_chars) if total_chars else 0.0
            ),
            "relevant_hits_per_1000_evaluator_tokens": (
                _rounded((total_hits * 1_000) / total_evaluator_tokens)
                if total_evaluator_tokens
                else 0.0
            ),
        },
        "determinism": {
            "exact_order_across_repetitions": _deterministic(measured),
            "semantic_digest_sha256": _semantic_digest(measured),
        },
        "latency_observational_ms": {
            "p50": round(statistics.median(item.duration_ms for item in measured), 2),
            "p95": _percentile([item.duration_ms for item in measured], 0.95),
        },
        "cases": [
            {
                "case_id": item.case_id,
                "family": item.family,
                "partition": item.partition,
                "required_recall": _rounded(item.required_recall),
                "precision": _rounded(item.precision),
                "sufficient": item.sufficient,
                "stale_exposure": item.stale_exposure,
                "untrusted_exposure": item.untrusted_exposure,
                "sensitive_exposure": item.sensitive_exposure,
                "canary_exposure": item.canary_exposure,
                "untrusted_text_exposure": item.untrusted_text_exposure,
                "sensitive_text_exposure": item.sensitive_text_exposure,
                "canary_text_exposure": item.canary_text_exposure,
                "payload_assertion_failures": item.assertion_failures,
                "payload_namespace_exposure": item.payload_namespace_exposure,
                "payload_reference_scope_exposure": item.payload_reference_scope_exposure,
                "scope_exposure": item.scope_exposure,
                "namespace_exposure": item.namespace_exposure,
                "budget_overflow": item.budget_overflow,
                "item_count": item.item_count,
                "payload_chars": item.payload_chars,
                "evaluation_tokens": item.evaluation_tokens,
                "truncated": item.truncated,
            }
            for item in first
        ],
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a mapping")
    return value


def _utility(aggregate: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(aggregate.get("utility"), "Aggregate utility")


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{label} must be numeric")
    return float(value)


def _matrix_complete(
    observations: Sequence[Observation],
    catalog: tuple[Scenario, ...],
    repetitions: int,
) -> bool:
    by_key: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for item in observations:
        by_key[(item.variant, item.budget, item.case_id)].append(item.repetition)
    expected_repetitions = list(range(repetitions))
    expected_keys = {
        (variant, budget.name, scenario.case_id)
        for variant in EXPECTED_VARIANTS
        for budget in BUDGETS
        for scenario in catalog
    }
    return set(by_key) == expected_keys and all(
        sorted(values) == expected_repetitions for values in by_key.values()
    )


def build_report(
    observations: Sequence[Observation],
    catalog: tuple[Scenario, ...],
    *,
    warmups: int,
    repetitions: int,
    state_unchanged: bool,
    initial_state: StateSnapshot,
    detector_self_test_passed: bool,
) -> dict[str, object]:
    """Build the stable public report without record IDs, payloads, or paths."""

    variants = tuple(dict.fromkeys(item.variant for item in observations))
    budgets = tuple(dict.fromkeys(item.budget for item in observations))
    critical_cases = frozenset(item.case_id for item in catalog if item.critical)
    aggregates = [
        _aggregate(
            observations,
            variant=variant,
            budget=budget,
            critical_cases=critical_cases,
        )
        for variant in variants
        for budget in budgets
    ]
    by_key = {(str(item["variant"]), str(item["budget"])): item for item in aggregates}
    for aggregate in aggregates:
        budget = str(aggregate["budget"])
        oracle = by_key[("oracle_v1", budget)]
        aggregate["oracle_sufficiency_gap"] = _rounded(
            _number(
                _utility(oracle)["safe_context_sufficiency"],
                "Oracle safe-context sufficiency",
            )
            - _number(
                _utility(aggregate)["safe_context_sufficiency"],
                "Baseline safe-context sufficiency",
            )
        )

    oracle_standard = _first_repetitions(
        [item for item in observations if item.variant == "oracle_v1" and item.budget == "standard"]
    )
    oracle_tight = _first_repetitions(
        [item for item in observations if item.variant == "oracle_v1" and item.budget == "tight"]
    )
    no_memory = _first_repetitions(
        [item for item in observations if item.variant == "no_memory_v1"]
    )
    deterministic = all(
        bool(_mapping(item["determinism"], "Determinism")["exact_order_across_repetitions"])
        for item in aggregates
    )
    oracle_standard_safe = all(
        item.sufficient and item.stale_exposure == 0 and item.hard_violation_count == 0
        for item in oracle_standard
    )
    tight_capable = {item.case_id for item in catalog if item.tight_capable}
    oracle_tight_capable = all(
        item.sufficient for item in oracle_tight if item.case_id in tight_capable
    )
    no_memory_lower_bound = all(
        item.required_recall == 0.0 and not item.sufficient for item in no_memory
    )
    runtime_checks = {
        "oracle_standard_safe_sufficiency": oracle_standard_safe,
        "oracle_tight_capable_cases_fit": oracle_tight_capable,
        "no_memory_lower_bound": no_memory_lower_bound,
        "hard_detector_self_test": detector_self_test_passed,
        "exact_order_determinism": deterministic,
        "canonical_state_unchanged": state_unchanged,
        "synthetic_disposable_store_only": True,
        "payload_and_path_free_report": True,
        "frozen_warmups_and_repetitions": warmups == 2 and repetitions == 5,
        "complete_measurement_matrix": _matrix_complete(observations, catalog, repetitions),
    }
    recency_standard = by_key[("context_recency_v1", "standard")]
    recency_utility = _utility(recency_standard)
    oracle_gap = _number(recency_standard["oracle_sufficiency_gap"], "Oracle sufficiency gap")
    critical_insufficient_value = recency_utility["critical_insufficient_cases"]
    if not isinstance(critical_insufficient_value, list):
        raise RuntimeError("Critical insufficiency cases must be a list")
    critical_insufficient = [str(item) for item in critical_insufficient_value]
    authorize_retrieval_experiment = oracle_gap >= 0.1 or bool(critical_insufficient)

    return {
        "schema": REPORT_SCHEMA_ID,
        "protocol": PROTOCOL_ID,
        "scenario_catalog": SCENARIO_CATALOG_ID,
        "reproducibility": reproducibility_metadata(catalog),
        "run": {
            "scenario_count": len(catalog),
            "warmups_per_provider_budget_case": warmups,
            "measured_repetitions_per_provider_budget_case": repetitions,
            "provider_count": len(variants),
            "budget_count": len(budgets),
            "budgets": [
                {
                    "name": budget.name,
                    "max_items": budget.max_items,
                    "max_chars": budget.max_chars,
                }
                for budget in BUDGETS
            ],
            "synthetic_state": {
                "generation": initial_state.generation,
                "watermark": initial_state.watermark,
                "record_count": initial_state.record_count,
            },
        },
        "qualification": {
            "scope": "runtime_internal",
            "passed": all(runtime_checks.values()),
            "checks": runtime_checks,
            "repository_quality_gate": "external_to_harness",
        },
        "baselines": aggregates,
        "interpretation": {
            "oracle_minus_recency_standard_sufficiency": _rounded(oracle_gap),
            "critical_recency_insufficient_cases": critical_insufficient,
            "retrieval_policy_experiment_authorized": authorize_retrieval_experiment,
            "rule": "gap_at_least_0.10_or_any_critical_recency_insufficiency",
        },
        "limitations": [
            {
                "claim": "live_agent_answer_or_action_quality",
                "status": "not_run",
                "reason": "This branch measures retrieval packets, not model behavior.",
            },
            {
                "claim": "email_quality_or_user_helpfulness",
                "status": "not_run",
                "reason": "Human judgment and privacy controls require a later protocol.",
            },
            {
                "claim": "model_token_or_cost_savings",
                "status": "not_run",
                "reason": "No live model is invoked by this lab.",
            },
            {
                "claim": "longitudinal_real_world_learning",
                "status": "not_run",
                "reason": "All fixtures are synthetic and disposable.",
            },
        ],
    }


def assert_report_private(
    report: Mapping[str, object], *, forbidden_values: Iterable[str] = ()
) -> None:
    """Fail closed if a public report contains payload sentinels or local paths."""

    forbidden_keys = {
        "config",
        "config_path",
        "ordered_ids",
        "path",
        "payload",
        "record_id",
        "record_ids",
        "records",
        "store_id",
        "subscription_id",
    }

    def validate_shape(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key) in forbidden_keys:
                    raise RuntimeError("Memory evaluation report contains a forbidden field")
                validate_shape(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                validate_shape(child)

    validate_shape(report)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    lowered = encoded.casefold()
    normalized = lowered.replace("\\\\", "\\")
    forbidden = [
        INERT_CANARY,
        INERT_SENSITIVE,
        INERT_UNTRUSTED,
        "lab_",
        "c:\\users\\",
        "/users/",
        *forbidden_values,
    ]
    leaked = [
        item
        for item in forbidden
        if item and (item.casefold() in lowered or item.casefold() in normalized)
    ]
    if leaked:
        raise RuntimeError("Memory evaluation report contains protected payload or path material")


__all__ = ["assert_report_private", "build_report"]
