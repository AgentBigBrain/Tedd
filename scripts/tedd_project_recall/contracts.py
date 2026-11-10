"""Typed contracts for the project recall lab.

The lab reuses the memory evaluation lab's scenario, packet, and observation
types unchanged and adds only what a project-sized question needs: where the
needed records sit in the project's history, and what a whole context response
delivered beyond its records list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tedd_memory_eval.contracts import Budget, ContextPacket, Observation, Scenario

PROTOCOL_ID = "tedd-project-recall-protocol-v1"
SCENARIO_CATALOG_ID = "tedd-project-recall-scenarios-v1"
REPORT_SCHEMA_ID = "tedd-project-recall-report-v1"
WARMUPS = 1
REPETITIONS = 3

STANDARD_BUDGET = Budget(name="standard", max_items=12, max_chars=12_000)
DEFAULT_BUDGET = Budget(name="default", max_items=100, max_chars=50_000)
BUDGETS = (STANDARD_BUDGET, DEFAULT_BUDGET)
# The shipped ``search_memory`` defaults, used for the search that follows a context.
SEARCH_STEP_BUDGET = Budget(name="search_step", max_items=10, max_chars=12_000)

ARM_IDS = (
    "no_memory_v1",
    "context_response_v1",
    "recall_fts_v1",
    "mcp_search_v1",
    "mcp_context_search_v1",
    "oracle_v1",
)

Placement = Literal["outside_recall", "recent_work", "fits_one_recall"]
PLACEMENTS: tuple[Placement, ...] = ("outside_recall", "recent_work", "fits_one_recall")


@dataclass(frozen=True)
class ProjectCase:
    """One question asked of one synthetic project.

    ``placement`` states where the required records sit by design.  Only
    ``outside_recall`` cases put them beyond what the default budget can hold;
    the other two are controls that today's recency order should satisfy.
    """

    scenario: Scenario
    placement: Placement

    @property
    def case_id(self) -> str:
        return self.scenario.case_id

    @property
    def control(self) -> bool:
        return self.placement != "outside_recall"


@dataclass(frozen=True)
class Delivery:
    """Everything one arm put in front of an agent for one question.

    ``packet`` is the records list, measured by the memory evaluation lab's
    frozen rules.  ``listed_ids`` are records that a reserved section of a
    context response names by ID, in response order.  ``carried_ids`` are the
    required records for which such a section also carries every asserted
    field.  ``withheld_sections`` counts reserved sections whose items were left
    out for lack of budget, and ``sections_sha256`` binds the exact content of
    every reserved section read.  Sizes cover the complete decoded responses,
    not only record payloads.
    """

    packet: ContextPacket
    listed_ids: tuple[str, ...]
    carried_ids: tuple[str, ...]
    response_chars: int
    response_tokens: int
    payload_mismatches: int
    withheld_sections: int = 0
    sections_sha256: str = ""


@dataclass(frozen=True)
class DeliveryObservation:
    """One scored arm, budget, case, and repetition.

    ``records`` is the memory evaluation lab's observation for the records
    list alone.  ``listed_recall`` counts a required record that is named
    anywhere in the response.  ``delivered_recall`` counts it only when its full
    payload or every asserted field arrived, so a list of titles earns the
    first measure and not the second.  ``allowed_chars`` is the character
    allowance the arm had for this read.
    """

    records: Observation
    placement: Placement
    listed_recall: float
    delivered_recall: float
    sufficient: bool
    stale_named: int
    section_items: int
    withheld_sections: int
    section_scope_exposure: int
    response_chars: int
    response_tokens: int
    payload_mismatches: int
    named_ids: tuple[str, ...]
    sections_sha256: str
    allowed_chars: int


@dataclass(frozen=True)
class FullContext:
    """The size of handing an agent every record in one project."""

    case_id: str
    placement: Placement
    record_count: int
    payload_chars: int
    evaluation_tokens: int
    stale_records: int


__all__ = [
    "ARM_IDS",
    "BUDGETS",
    "DEFAULT_BUDGET",
    "PLACEMENTS",
    "PROTOCOL_ID",
    "REPETITIONS",
    "REPORT_SCHEMA_ID",
    "SCENARIO_CATALOG_ID",
    "SEARCH_STEP_BUDGET",
    "STANDARD_BUDGET",
    "WARMUPS",
    "Delivery",
    "DeliveryObservation",
    "FullContext",
    "Placement",
    "ProjectCase",
]
