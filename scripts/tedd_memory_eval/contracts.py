"""Typed contracts shared by the memory evaluation lab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

PROTOCOL_ID = "tedd-memory-eval-protocol-v2"
SCENARIO_CATALOG_ID = "tedd-memory-scenarios-v1"
REPORT_SCHEMA_ID = "tedd-memory-eval-report-v2"
EVALUATOR_TOKENIZER_ID = "tedd-memory-eval-tokenizer-v1"
ALLOWED_RECORD_PREFIX = "lab_"


@dataclass(frozen=True)
class Budget:
    """One fixed agent-context allowance."""

    name: str
    max_items: int
    max_chars: int


@dataclass(frozen=True)
class RecordFixture:
    """One synthetic graph record in deterministic mutation order."""

    record_id: str
    collection: str
    payload: Mapping[str, object]
    search_text: str
    subject_id: str
    refs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PayloadAssertion:
    """One required review-safe scalar value for a retrieved record."""

    record_id: str
    field: str
    expected: object


@dataclass(frozen=True)
class Scenario:
    """A versioned retrieval judgment with explicit safety labels."""

    case_id: str
    family: str
    query: str
    subject_id: str
    records: tuple[RecordFixture, ...]
    required_ids: tuple[str, ...]
    relevant_ids: tuple[str, ...]
    oracle_ids: tuple[str, ...]
    required_assertions: tuple[PayloadAssertion, ...]
    partition: str = "calibration"
    stale_ids: tuple[str, ...] = ()
    untrusted_ids: tuple[str, ...] = ()
    sensitive_ids: tuple[str, ...] = ()
    canary_ids: tuple[str, ...] = ()
    scope_forbidden_ids: tuple[str, ...] = ()
    current_before_stale: tuple[tuple[str, str], ...] = ()
    critical: bool = False
    tight_capable: bool = True

    @property
    def forbidden_ids(self) -> frozenset[str]:
        """Return all records that must not appear in a safe packet."""

        return frozenset(
            (*self.stale_ids, *self.untrusted_ids, *self.sensitive_ids, *self.canary_ids)
        )

    @property
    def fixture_ids(self) -> frozenset[str]:
        """Return all records authored inside this scenario's scope."""

        return frozenset(item.record_id for item in self.records)

    @property
    def scope_ids(self) -> frozenset[str]:
        """Include the synthetic subject entity selected by current context semantics."""

        return self.fixture_ids | {self.subject_id}

    @property
    def reference_scope_ids(self) -> frozenset[str]:
        """Return records and the subject that belong to this scenario's actual scope."""

        return frozenset(
            fixture.record_id for fixture in self.records if fixture.subject_id == self.subject_id
        ) | {self.subject_id}


@dataclass(frozen=True)
class ContextPacket:
    """One provider result retained only for in-process scoring."""

    record_ids: tuple[str, ...]
    payload_chars: int
    reported_chars: int
    item_count: int
    truncated: bool
    duration_ms: float
    evaluation_tokens: int
    assertion_failures: int = 0
    untrusted_text_exposure: int = 0
    sensitive_text_exposure: int = 0
    canary_text_exposure: int = 0
    payload_namespace_exposure: int = 0
    payload_reference_scope_exposure: int = 0


@dataclass(frozen=True)
class Observation:
    """One measured provider, budget, and scenario outcome."""

    variant: str
    budget: str
    case_id: str
    family: str
    partition: str
    required_collections: tuple[str, ...]
    repetition: int
    required_recall: float
    precision: float
    reciprocal_rank: float
    worst_required_rank: int | None
    current_over_stale_accuracy: float | None
    sufficient: bool
    stale_exposure: int
    untrusted_exposure: int
    sensitive_exposure: int
    canary_exposure: int
    scope_exposure: int
    namespace_exposure: int
    assertion_failures: int
    untrusted_text_exposure: int
    sensitive_text_exposure: int
    canary_text_exposure: int
    payload_namespace_exposure: int
    payload_reference_scope_exposure: int
    budget_overflow: bool
    item_count: int
    payload_chars: int
    evaluation_tokens: int
    truncated: bool
    duration_ms: float
    relevant_hits: int
    ordered_ids: tuple[str, ...]

    @property
    def hard_violation_count(self) -> int:
        """Count non-stale safety, scope, namespace, and budget violations."""

        return (
            self.assertion_failures
            + self.untrusted_text_exposure
            + self.sensitive_text_exposure
            + self.canary_text_exposure
            + self.payload_namespace_exposure
            + self.payload_reference_scope_exposure
            + self.scope_exposure
            + self.namespace_exposure
            + int(self.budget_overflow)
        )


@dataclass(frozen=True)
class StateSnapshot:
    """Canonical graph state fields that retrieval is forbidden to mutate."""

    generation: int
    watermark: int
    journal_head_hash: str
    record_count: int


BUDGETS = (
    Budget(name="tight", max_items=6, max_chars=6_000),
    Budget(name="standard", max_items=12, max_chars=12_000),
)
