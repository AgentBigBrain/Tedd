"""Deterministic, payload-free aggregation for the project recall lab."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from tedd_memory_eval import reporting as reference_reporting
from tedd_memory_eval.contracts import Budget, StateSnapshot

from .catalog import cases_digest, full_context
from .contracts import (
    ARM_IDS,
    BUDGETS,
    DEFAULT_BUDGET,
    PLACEMENTS,
    PROTOCOL_ID,
    REPETITIONS,
    REPORT_SCHEMA_ID,
    SCENARIO_CATALOG_ID,
    WARMUPS,
    DeliveryObservation,
    FullContext,
    ProjectCase,
)
from .delivery import CONTEXT_ARM_ID

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = REPOSITORY_ROOT / "research/project-recall-lab/PROTOCOL.md"
INSTRUMENT_PATHS = (
    REPOSITORY_ROOT / "scripts/project_recall_lab.py",
    *(REPOSITORY_ROOT / "scripts/tedd_project_recall").glob("*.py"),
    PROTOCOL_PATH,
)
GIT_REPRODUCIBILITY_PATHS = (
    "research/project-recall-lab",
    "scripts/project_recall_lab.py",
    "scripts/tedd_project_recall",
    *reference_reporting.GIT_REPRODUCIBILITY_PATHS,
)
REQUIRED_COLLECTIONS = ("decisions", "open_loops", "observations")


def _rounded(value: float) -> float:
    return round(value, 4)


def _mean(values: Iterable[float]) -> float:
    material = tuple(values)
    return _rounded(statistics.fmean(material)) if material else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
    return round(ordered[index], 2)


def evaluator_digest() -> str:
    """Hash this instrument's code and protocol with the lab code it runs on.

    The memory evaluation lab's own digest covers its package and the graph
    runtime, so a change to either changes this digest too.
    """

    digest = hashlib.sha256()
    for path in sorted(INSTRUMENT_PATHS):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    digest.update(reference_reporting.evaluator_digest().encode("ascii"))
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
        status_result = run("status", "--porcelain", "--", *GIT_REPRODUCIBILITY_PATHS)
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
        "research_scope_dirty": status_result.returncode != 0 or bool(status_result.stdout.strip()),
    }


def reproducibility_metadata(cases: Sequence[ProjectCase]) -> dict[str, object]:
    """Return sanitized code, catalog, environment, and dependency identity."""

    reference = reference_reporting.reproducibility_metadata(tuple(case.scenario for case in cases))
    return {
        **reference,
        "catalog_sha256": cases_digest(cases),
        "evaluator_sha256": evaluator_digest(),
        "memory_eval_evaluator_sha256": reference["evaluator_sha256"],
        "git": _git_metadata(),
    }


def _first(observations: Sequence[DeliveryObservation]) -> tuple[DeliveryObservation, ...]:
    return tuple(item for item in observations if item.records.repetition == 0)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError("Aggregate member must be a mapping")
    return value


def _measured(item: DeliveryObservation) -> dict[str, object]:
    """Return everything one read was measured to deliver, without its timing."""

    records = item.records
    return {
        "arm": records.variant,
        "budget": records.budget,
        "case": records.case_id,
        "ordered_ids": records.ordered_ids,
        "named_ids": item.named_ids,
        "sections_sha256": item.sections_sha256,
        "delivered_recall": item.delivered_recall,
        "listed_recall": item.listed_recall,
        "sufficient": item.sufficient,
        "stale_named": item.stale_named,
        "hard_violations": records.hard_violation_count,
        "section_scope": item.section_scope_exposure,
        "section_items": item.section_items,
        "withheld_sections": item.withheld_sections,
        "limit_violations": item.limit_violations,
        "payload_mismatches": item.payload_mismatches,
        "items": records.item_count,
        "chars": records.payload_chars,
        "response_chars": item.response_chars,
        "response_tokens": item.response_tokens,
        "truncated": records.truncated,
    }


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identical_repetitions(observations: Sequence[DeliveryObservation]) -> bool:
    """Check that every repetition of a case measured exactly the same delivery.

    Record order, section content, and every score must match.  Comparing IDs
    alone would pass a section item whose text changed under the same ID.
    """

    by_case: dict[str, set[str]] = defaultdict(set)
    for item in observations:
        by_case[item.records.case_id].add(_encoded(_measured(item)))
    return all(len(measured) == 1 for measured in by_case.values())


def _semantic_digest(observations: Sequence[DeliveryObservation]) -> str:
    material = [{**_measured(item), "repetition": item.records.repetition} for item in observations]
    return hashlib.sha256(_encoded(material).encode("utf-8")).hexdigest()


def _case_row(item: DeliveryObservation) -> dict[str, object]:
    records = item.records
    return {
        "case_id": records.case_id,
        "family": records.family,
        "placement": item.placement,
        "partition": records.partition,
        "delivered_recall": _rounded(item.delivered_recall),
        "listed_recall": _rounded(item.listed_recall),
        "records_list_recall": _rounded(records.required_recall),
        "sufficient": item.sufficient,
        "stale_named": item.stale_named,
        "hard_violations": records.hard_violation_count,
        "limit_violations": item.limit_violations,
        "section_scope_exposure": item.section_scope_exposure,
        "item_count": records.item_count,
        "section_items": item.section_items,
        "withheld_sections": item.withheld_sections,
        "payload_chars": records.payload_chars,
        "response_chars": item.response_chars,
        "response_evaluator_tokens": item.response_tokens,
        "truncated": records.truncated,
    }


def _aggregate(
    observations: Sequence[DeliveryObservation],
    references: Mapping[str, FullContext],
    *,
    arm_id: str,
    budget: Budget,
    critical_cases: frozenset[str],
) -> dict[str, object]:
    measured = [
        item
        for item in observations
        if item.records.variant == arm_id and item.records.budget == budget.name
    ]
    first = _first(measured)
    order_scores = [
        item.records.current_over_stale_accuracy
        for item in first
        if item.records.current_over_stale_accuracy is not None
    ]
    return {
        "arm": arm_id,
        "budget": budget.name,
        "scenario_count": len(first),
        "measured_sample_count": len(measured),
        "utility": {
            "delivered_recall": _mean(item.delivered_recall for item in first),
            "listed_recall": _mean(item.listed_recall for item in first),
            "records_list_recall": _mean(item.records.required_recall for item in first),
            "sufficiency": _mean(float(item.sufficient) for item in first),
            "sufficiency_by_placement": {
                placement: _mean(
                    float(item.sufficient) for item in first if item.placement == placement
                )
                for placement in PLACEMENTS
            },
            "delivered_recall_by_required_collection": {
                collection: _mean(
                    item.delivered_recall
                    for item in first
                    if collection in item.records.required_collections
                )
                for collection in REQUIRED_COLLECTIONS
            },
            "calibration_sufficiency": _mean(
                float(item.sufficient) for item in first if item.records.partition == "calibration"
            ),
            "confirmation_sufficiency": _mean(
                float(item.sufficient) for item in first if item.records.partition == "confirmation"
            ),
            "critical_insufficient_cases": sorted(
                item.records.case_id
                for item in first
                if item.records.case_id in critical_cases and not item.sufficient
            ),
            "current_over_stale_accuracy": _mean(order_scores),
        },
        "safety": {
            "stale_named_scenarios": sum(item.stale_named > 0 for item in first),
            "scope_exposure_scenarios": sum(
                item.records.scope_exposure > 0 or item.section_scope_exposure > 0 for item in first
            ),
            "hard_violation_scenarios": sum(
                item.records.hard_violation_count > 0 for item in first
            ),
            "limit_violation_scenarios": sum(item.limit_violations > 0 for item in first),
        },
        "size": {
            "mean_items": _mean(float(item.records.item_count) for item in first),
            "mean_section_items": _mean(float(item.section_items) for item in first),
            "mean_payload_chars": _mean(float(item.records.payload_chars) for item in first),
            "mean_response_chars": _mean(float(item.response_chars) for item in first),
            "mean_response_evaluator_tokens": _mean(float(item.response_tokens) for item in first),
            "mean_share_of_project_items": _mean(
                item.records.item_count / references[item.records.case_id].record_count
                for item in first
            ),
            "mean_share_of_full_context_chars": _mean(
                item.records.payload_chars / references[item.records.case_id].payload_chars
                for item in first
            ),
            "response_over_allowance_scenarios": sum(
                item.response_chars > item.allowed_chars for item in first
            ),
            "truncated_scenarios": sum(item.records.truncated for item in first),
            "withheld_section_scenarios": sum(item.withheld_sections > 0 for item in first),
        },
        "determinism": {
            "identical_across_repetitions": _identical_repetitions(measured),
            "semantic_digest_sha256": _semantic_digest(measured),
        },
        "latency_observational_ms": {
            "p50": round(statistics.median(item.records.duration_ms for item in measured), 2),
            "p95": _percentile([item.records.duration_ms for item in measured], 0.95),
        },
        "cases": [_case_row(item) for item in first],
    }


def _matrix_complete(
    observations: Sequence[DeliveryObservation],
    cases: Sequence[ProjectCase],
    repetitions: int,
) -> bool:
    by_key: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for item in observations:
        records = item.records
        by_key[(records.variant, records.budget, records.case_id)].append(records.repetition)
    expected = {
        (arm_id, budget.name, case.case_id)
        for arm_id in ARM_IDS
        for budget in BUDGETS
        for case in cases
    }
    return set(by_key) == expected and all(
        sorted(values) == list(range(repetitions)) for values in by_key.values()
    )


def _full_context_section(references: Sequence[FullContext]) -> dict[str, object]:
    return {
        "basis": "authored_payloads_checked_against_review_safe_reads",
        "mean_record_count": _mean(float(item.record_count) for item in references),
        "mean_payload_chars": _mean(float(item.payload_chars) for item in references),
        "mean_evaluator_tokens": _mean(float(item.evaluation_tokens) for item in references),
        "cases": [
            {
                "case_id": item.case_id,
                "placement": item.placement,
                "record_count": item.record_count,
                "payload_chars": item.payload_chars,
                "evaluator_tokens": item.evaluation_tokens,
                "stale_record_count": item.stale_records,
                "fits_default_budget": (
                    item.record_count <= DEFAULT_BUDGET.max_items
                    and item.payload_chars <= DEFAULT_BUDGET.max_chars
                ),
            }
            for item in references
        ],
    }


def _select(
    observations: Sequence[DeliveryObservation], arm_id: str, budget: Budget
) -> tuple[DeliveryObservation, ...]:
    return _first(
        [
            item
            for item in observations
            if item.records.variant == arm_id and item.records.budget == budget.name
        ]
    )


def _route(arm_id: str, first: Sequence[DeliveryObservation]) -> dict[str, object]:
    """Summarize one arm at the default budget, oversized cases apart from controls."""

    outside = [item for item in first if item.placement == "outside_recall"]
    controls = [item for item in first if item.placement != "outside_recall"]
    return {
        "arm": arm_id,
        "outside_recall_sufficiency": _mean(float(item.sufficient) for item in outside),
        "outside_recall_delivered_recall": _mean(item.delivered_recall for item in outside),
        "controls_sufficient": bool(controls) and all(item.sufficient for item in controls),
        "mean_response_chars": _mean(float(item.response_chars) for item in first),
    }


def build_report(
    observations: Sequence[DeliveryObservation],
    cases: Sequence[ProjectCase],
    *,
    warmups: int,
    repetitions: int,
    state_unchanged: bool,
    initial_state: StateSnapshot,
) -> dict[str, object]:
    """Build the public report without record IDs, payloads, or paths."""

    references = tuple(full_context(case) for case in cases)
    by_case = {item.case_id: item for item in references}
    critical = frozenset(case.case_id for case in cases if case.scenario.critical)
    aggregates = [
        _aggregate(observations, by_case, arm_id=arm_id, budget=budget, critical_cases=critical)
        for arm_id in ARM_IDS
        for budget in BUDGETS
    ]
    oracle = [item for budget in BUDGETS for item in _select(observations, "oracle_v1", budget)]
    no_memory = [
        item for budget in BUDGETS for item in _select(observations, "no_memory_v1", budget)
    ]
    checks = {
        "oracle_sufficient_at_every_budget": bool(oracle)
        and all(item.sufficient and item.stale_named == 0 for item in oracle),
        "no_memory_lower_bound": bool(no_memory)
        and all(item.delivered_recall == 0.0 and not item.sufficient for item in no_memory),
        "returned_payloads_match_authored_catalog": all(
            item.payload_mismatches == 0 for item in observations
        ),
        "identical_measured_repetitions": all(
            bool(_mapping(item["determinism"])["identical_across_repetitions"])
            for item in aggregates
        ),
        "canonical_state_unchanged": state_unchanged,
        "synthetic_disposable_store_only": True,
        "payload_and_path_free_report": True,
        "frozen_warmups_and_repetitions": (warmups, repetitions) == (WARMUPS, REPETITIONS),
        "complete_measurement_matrix": _matrix_complete(observations, cases, repetitions),
    }
    context = _select(observations, CONTEXT_ARM_ID, DEFAULT_BUDGET)
    outside = [item for item in context if item.placement == "outside_recall"]
    controls = [item for item in context if item.placement != "outside_recall"]
    oracle_outside = [
        item
        for item in _select(observations, "oracle_v1", DEFAULT_BUDGET)
        if item.placement == "outside_recall"
    ]
    routes = [_route(arm_id, _select(observations, arm_id, DEFAULT_BUDGET)) for arm_id in ARM_IDS]
    return {
        "schema": REPORT_SCHEMA_ID,
        "protocol": PROTOCOL_ID,
        "scenario_catalog": SCENARIO_CATALOG_ID,
        "reproducibility": reproducibility_metadata(cases),
        "run": {
            "scenario_count": len(cases),
            "warmups_per_arm_budget_case": warmups,
            "measured_repetitions_per_arm_budget_case": repetitions,
            "arm_count": len(ARM_IDS),
            "budgets": [
                {"name": budget.name, "max_items": budget.max_items, "max_chars": budget.max_chars}
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
            "passed": all(checks.values()),
            "checks": checks,
            "repository_quality_gate": "external_to_harness",
        },
        "full_context": _full_context_section(references),
        "arms": aggregates,
        "interpretation": {
            "arm": CONTEXT_ARM_ID,
            "budget": DEFAULT_BUDGET.name,
            "outside_recall_sufficiency": _mean(float(item.sufficient) for item in outside),
            "outside_recall_delivered_recall": _mean(item.delivered_recall for item in outside),
            "outside_recall_listed_recall": _mean(item.listed_recall for item in outside),
            "outside_recall_oracle_sufficiency": _mean(
                float(item.sufficient) for item in oracle_outside
            ),
            "controls_sufficient": bool(controls) and all(item.sufficient for item in controls),
            "critical_insufficient_cases": sorted(
                item.records.case_id
                for item in context
                if item.records.case_id in critical and not item.sufficient
            ),
            "routes_at_default": routes,
        },
        "limitations": [
            {
                "claim": "live_agent_answer_or_action_quality",
                "status": "not_run",
                "reason": "This lab measures what a read delivers, not model behavior.",
            },
            {
                "claim": "agent_search_behavior",
                "status": "not_run",
                "reason": (
                    "Search arms search with the case's question. Whether an agent "
                    "searches, and with which words, is not measured."
                ),
            },
            {
                "claim": "model_token_or_cost_savings",
                "status": "not_run",
                "reason": "Evaluator tokens are comparison units, not model or billed tokens.",
            },
            {
                "claim": "benefit_on_real_projects",
                "status": "not_run",
                "reason": "All projects are synthetic and disposable.",
            },
        ],
    }


__all__ = ["build_report", "evaluator_digest", "reproducibility_metadata"]
