"""Evaluation orchestration with no production graph or model side effects."""

from __future__ import annotations

from .contracts import BUDGETS, Observation
from .providers import providers
from .reporting import assert_report_private, build_report
from .sandbox import MemoryEvalSandbox
from .scoring import hard_detector_self_test, score_packet

DEFAULT_WARMUPS = 2
DEFAULT_REPETITIONS = 5


def run_lab(
    *,
    warmups: int = DEFAULT_WARMUPS,
    repetitions: int = DEFAULT_REPETITIONS,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Run all frozen baselines in one private synthetic graph."""

    if warmups < 0:
        raise ValueError("warmups cannot be negative")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    observations: list[Observation] = []
    with MemoryEvalSandbox(timeout_seconds=timeout_seconds) as sandbox:
        protected_paths = tuple(
            str(path)
            for path in (
                sandbox.root,
                sandbox.config_path,
                sandbox.store_path,
                sandbox.vault_path,
            )
            if path is not None
        )
        for provider in providers():
            for budget in BUDGETS:
                for scenario in sandbox.catalog:
                    for _ in range(warmups):
                        provider.retrieve(sandbox, scenario, budget)
                    for repetition in range(repetitions):
                        packet = provider.retrieve(sandbox, scenario, budget)
                        observations.append(
                            score_packet(
                                variant=provider.variant_id,
                                budget=budget,
                                scenario=scenario,
                                repetition=repetition,
                                packet=packet,
                            )
                        )
        state_unchanged = sandbox.initial_state == sandbox.final_state()
        report = build_report(
            observations,
            sandbox.catalog,
            warmups=warmups,
            repetitions=repetitions,
            state_unchanged=state_unchanged,
            initial_state=sandbox.initial_state,
            detector_self_test_passed=hard_detector_self_test(sandbox.catalog, BUDGETS[0]),
        )
        assert_report_private(report, forbidden_values=protected_paths)
        return report


__all__ = ["DEFAULT_REPETITIONS", "DEFAULT_WARMUPS", "run_lab"]
