"""Project recall orchestration with no production graph or model side effects."""

from __future__ import annotations

from tedd_memory_eval.reporting import assert_report_private
from tedd_memory_eval.sandbox import MemoryEvalSandbox

from .catalog import project_cases
from .contracts import BUDGETS, PROTOCOL_ID, REPETITIONS, WARMUPS, DeliveryObservation
from .delivery import arms, score_delivery
from .reporting import build_report


def run_lab(
    *,
    warmups: int = WARMUPS,
    repetitions: int = REPETITIONS,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Read every frozen project through every arm in one private synthetic graph.

    The graph, its configuration, and its vault exist only inside a private
    temporary directory that is removed on exit, including on failure.  The
    returned report holds aggregates only and is checked for payload, path,
    and identifier leaks before it is returned.

    Raises:
        ValueError: If a count or the timeout is outside its allowed range.
        RuntimeError: If a synthetic graph read fails or the report would
            expose protected material.
    """

    if warmups < 0:
        raise ValueError("warmups cannot be negative")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    cases = project_cases()
    observations: list[DeliveryObservation] = []
    with MemoryEvalSandbox(
        timeout_seconds=timeout_seconds,
        catalog=tuple(case.scenario for case in cases),
        protocol_id=PROTOCOL_ID,
    ) as sandbox:
        protected_paths = tuple(
            str(path)
            for path in (sandbox.root, sandbox.config_path, sandbox.store_path, sandbox.vault_path)
            if path is not None
        )
        for arm in arms():
            for budget in BUDGETS:
                for case in cases:
                    for _ in range(warmups):
                        arm.read(sandbox, case, budget)
                    observations.extend(
                        score_delivery(
                            arm_id=arm.arm_id,
                            budget=arm.allowance(budget),
                            case=case,
                            repetition=repetition,
                            delivery=arm.read(sandbox, case, budget),
                        )
                        for repetition in range(repetitions)
                    )
        report = build_report(
            observations,
            cases,
            warmups=warmups,
            repetitions=repetitions,
            state_unchanged=sandbox.initial_state == sandbox.final_state(),
            initial_state=sandbox.initial_state,
        )
        assert_report_private(report, forbidden_values=protected_paths)
        return report


__all__ = ["run_lab"]
