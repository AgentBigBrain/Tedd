"""Arms that read one project, and how a read is scored.

Every records list is measured by the memory evaluation lab's frozen packet
rules, so budget, scope, namespace, reference, and payload checks are that
lab's own.  This module adds what those rules do not read: the reserved
sections of a context response, which can name or carry a record that the
records list leaves out, and the reads an agent makes through MCP.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast

from tedd_memory_eval.contracts import Budget, ContextPacket, Scenario
from tedd_memory_eval.providers import (
    Provider,
    canonical_payload_json,
    evaluation_token_count,
    providers,
)
from tedd_memory_eval.sandbox import MemoryEvalSandbox, subscription_id_for
from tedd_memory_eval.scoring import score_packet

from .contracts import (
    ARM_IDS,
    SEARCH_STEP_BUDGET,
    Delivery,
    DeliveryObservation,
    ProjectCase,
)
from .mcp_reads import McpReader, McpReads

CONTEXT_ARM_ID = "context_response_v1"
SEARCH_ARM_ID = "mcp_search_v1"
WORKFLOW_ARM_ID = "mcp_context_search_v1"
CONTEXT_PROVIDER_ID = "context_recency_v1"
# The replayed provider builds a command from its budget and nothing else.
_REPLAY_BUDGET = Budget(name="replay", max_items=1, max_chars=1)

Connect = Callable[[MemoryEvalSandbox], McpReader]


def _object(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[object, object], value)
    return {str(key): item for key, item in raw.items()}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    decoded = (_object(item) for item in cast(list[object], value))
    return [item for item in decoded if item is not None]


def _flag(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{label} must be a boolean")
    return value


def _sections(response: Mapping[str, object]) -> list[tuple[str, dict[str, object]]]:
    """Return each reserved section with its name, in response order."""

    named = ((name, _object(member)) for name, member in response.items())
    return [
        (name, section)
        for name, section in named
        if section is not None and isinstance(section.get("items"), list)
    ]


def section_items(response: Mapping[str, object]) -> list[dict[str, object]]:
    """Return the identified items of every reserved section, in response order.

    A reserved section is any top-level member that is an object with an
    ``items`` array.  The rule names no section, so a section added by a later
    Tedd change is read without changing this instrument.  Items without a
    text ``id`` identify no record and are ignored.
    """

    return [
        item
        for _name, section in _sections(response)
        for item in _objects(section.get("items"))
        if isinstance(item.get("id"), str)
    ]


def withheld_sections(response: Mapping[str, object]) -> int:
    """Count reserved sections whose items were left out for lack of budget.

    Tedd marks such a section ``omitted``.  An empty section that is not
    marked had nothing to show and is not counted.
    """

    return sum(section.get("omitted") is True for _name, section in _sections(response))


def sections_digest(responses: Sequence[Mapping[str, object]]) -> str:
    """Hash the complete content of every reserved section, in response order.

    Two reads have the same digest only when every section has the same name,
    flags, and items with the same field values in the same order.
    """

    material = [[[name, section] for name, section in _sections(item)] for item in responses]
    return hashlib.sha256(canonical_payload_json(material).encode("utf-8")).hexdigest()


def _returned_records(response: Mapping[str, object]) -> list[dict[str, object]]:
    """Return every full record a context, search, or exact-ID response holds."""

    if "record_id" in response and "payload" in response:
        return [dict(response)]
    return [*_objects(response.get("records")), *_objects(response.get("results"))]


def _payload_mismatches(responses: Sequence[Mapping[str, object]], scenario: Scenario) -> int:
    """Count returned payloads that differ from the payload the catalog authored."""

    authored = {fixture.record_id: fixture.payload for fixture in scenario.records}
    mismatches = 0
    for response in responses:
        for record in _returned_records(response):
            expected = authored.get(str(record.get("record_id")))
            if expected is not None and canonical_payload_json(
                record.get("payload")
            ) != canonical_payload_json(expected):
                mismatches += 1
    return mismatches


def over_limits(
    response: Mapping[str, object], limits: Budget, *, cli_envelope: bool = False
) -> bool:
    """Tell whether one call returned more than the limits it was given.

    A context's character limit covers its service payload; a search's covers
    its results list. Use Tedd's budget encoding, including separator spaces
    and unescaped Unicode. Only CLI reads exclude the command/status envelope
    added after the service budgets a context. Exact-ID reads have no limit.
    """

    if isinstance(response.get("records"), list):
        returned = _objects(response.get("records"))
        bounded: object = (
            {key: value for key, value in response.items() if key not in ("command", "status")}
            if cli_envelope
            else dict(response)
        )
    elif isinstance(response.get("results"), list):
        returned, bounded = _objects(response.get("results")), response.get("results")
    else:
        return False
    return (
        len(returned) > limits.max_items
        or len(json.dumps(bounded, ensure_ascii=False)) > limits.max_chars
    )


def deliver(
    packet: ContextPacket,
    responses: Sequence[Mapping[str, object]],
    scenario: Scenario,
    *,
    sections: bool,
    limits: Sequence[Budget],
    cli_envelope: bool = False,
) -> Delivery:
    """Describe one read from its scored records list and its decoded responses.

    With ``sections`` true, items in reserved sections count as listed, and a
    required record counts as carried when one item with its ID holds every
    asserted field with the asserted value.  ``limits`` gives, for each
    response in order, the limits that call was made with. ``cli_envelope``
    excludes the CLI-added fields from context limit checks only; response
    cost and section digests still describe the complete responses.

    Raises:
        ValueError: If ``limits`` does not give one entry per response.
    """

    if len(limits) != len(responses):
        raise ValueError("Every response needs the limits its call was given")

    items = [item for response in responses for item in section_items(response)] if sections else []
    carried = tuple(
        record_id
        for record_id in scenario.required_ids
        if any(
            item.get("id") == record_id
            and all(
                assertion.field in item and item[assertion.field] == assertion.expected
                for assertion in scenario.required_assertions
                if assertion.record_id == record_id
            )
            for item in items
        )
    )
    texts = tuple(canonical_payload_json(response) for response in responses)
    return Delivery(
        packet=packet,
        listed_ids=tuple(dict.fromkeys(str(item["id"]) for item in items)),
        carried_ids=carried,
        response_chars=sum(len(text) for text in texts),
        response_tokens=evaluation_token_count(texts),
        payload_mismatches=_payload_mismatches(responses, scenario),
        withheld_sections=(
            sum(withheld_sections(response) for response in responses) if sections else 0
        ),
        sections_sha256=sections_digest(responses) if sections else "",
        limit_violations=sum(
            over_limits(response, limit, cli_envelope=cli_envelope)
            for response, limit in zip(responses, limits, strict=True)
        ),
    )


class _RecordingClient:
    """Forward allowlisted reads to the sandbox and keep each decoded response."""

    def __init__(self, sandbox: MemoryEvalSandbox) -> None:
        self._sandbox = sandbox
        self.responses: list[dict[str, object]] = []

    def call(self, arguments: Sequence[str]) -> dict[str, object]:
        response = self._sandbox.call(arguments)
        self.responses.append(response)
        return response


class _Replay:
    """Answer a frozen provider's one read with a prepared response."""

    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def call(self, arguments: Sequence[str]) -> dict[str, object]:
        del arguments
        return self._response


def _context_provider() -> Provider:
    return next(item for item in providers() if item.variant_id == CONTEXT_PROVIDER_ID)


def frozen_packet(
    records: Sequence[Mapping[str, object]],
    scenario: Scenario,
    *,
    truncated: bool,
    duration_ms: float,
) -> ContextPacket:
    """Measure a records list with the memory evaluation lab's frozen packet rules.

    The records are replayed through that lab's context provider, so payload
    size, evaluator tokens, payload assertions, sentinel text, namespace, and
    reference checks are exactly its own.

    Raises:
        RuntimeError: If a record is malformed or an ID repeats.
    """

    response: dict[str, object] = {
        "records": [dict(record) for record in records],
        "item_count": len(records),
        "char_count": sum(len(canonical_payload_json(item.get("payload"))) for item in records),
        "subscription_id": subscription_id_for(scenario),
        "truncated": truncated,
    }
    # Providers use only ``call``, which the replay answers from memory.
    packet = _context_provider().retrieve(
        cast(MemoryEvalSandbox, _Replay(response)), scenario, _REPLAY_BUDGET
    )
    return replace(packet, duration_ms=duration_ms)


def _merge(first: ContextPacket, second: ContextPacket) -> ContextPacket:
    """Combine two reads made in sequence; a record returned twice is paid for twice."""

    return ContextPacket(
        record_ids=tuple(dict.fromkeys((*first.record_ids, *second.record_ids))),
        payload_chars=first.payload_chars + second.payload_chars,
        reported_chars=first.reported_chars + second.reported_chars,
        item_count=first.item_count + second.item_count,
        truncated=first.truncated or second.truncated,
        duration_ms=first.duration_ms + second.duration_ms,
        evaluation_tokens=first.evaluation_tokens + second.evaluation_tokens,
        assertion_failures=first.assertion_failures + second.assertion_failures,
        untrusted_text_exposure=first.untrusted_text_exposure + second.untrusted_text_exposure,
        sensitive_text_exposure=first.sensitive_text_exposure + second.sensitive_text_exposure,
        canary_text_exposure=first.canary_text_exposure + second.canary_text_exposure,
        payload_namespace_exposure=(
            first.payload_namespace_exposure + second.payload_namespace_exposure
        ),
        payload_reference_scope_exposure=(
            first.payload_reference_scope_exposure + second.payload_reference_scope_exposure
        ),
    )


class ReadArm(Protocol):
    """One way of reading a project for a question."""

    @property
    def arm_id(self) -> str: ...

    @property
    def reads_sections(self) -> bool: ...

    def allowance(self, budget: Budget) -> Budget:
        """Return the items and characters this arm may use at ``budget``."""
        ...

    def read(self, sandbox: MemoryEvalSandbox, case: ProjectCase, budget: Budget) -> Delivery:
        """Run the arm's reads once and describe everything they returned."""
        ...


@dataclass(frozen=True)
class ProviderArm:
    """One frozen memory-lab provider reading through the CLI."""

    arm_id: str
    provider: Provider
    reads_sections: bool = False

    def allowance(self, budget: Budget) -> Budget:
        return budget

    def read(self, sandbox: MemoryEvalSandbox, case: ProjectCase, budget: Budget) -> Delivery:
        client = _RecordingClient(sandbox)
        # Providers use only ``call``, which the recorder forwards unchanged.
        packet = self.provider.retrieve(cast(MemoryEvalSandbox, client), case.scenario, budget)
        return deliver(
            packet,
            client.responses,
            case.scenario,
            sections=self.reads_sections,
            limits=[budget] * len(client.responses),
            cli_envelope=True,
        )


@dataclass(frozen=True)
class McpSearchArm:
    """One scoped ``search_memory`` call with the case's question, at the budget."""

    arm_id: str = SEARCH_ARM_ID
    connect: Connect = McpReads
    reads_sections: bool = False

    def allowance(self, budget: Budget) -> Budget:
        return budget

    def read(self, sandbox: MemoryEvalSandbox, case: ProjectCase, budget: Budget) -> Delivery:
        scenario = case.scenario
        started = time.perf_counter()
        (search,) = self.connect(sandbox).call(
            scenario,
            [
                (
                    "search_memory",
                    {
                        "query": scenario.query,
                        "limit": budget.max_items,
                        "max_chars": budget.max_chars,
                    },
                )
            ],
        )
        packet = frozen_packet(
            _objects(search.get("results")),
            scenario,
            truncated=_flag(search.get("truncated"), "Search truncation marker"),
            duration_ms=(time.perf_counter() - started) * 1_000,
        )
        return deliver(packet, [search], scenario, sections=False, limits=[budget])


@dataclass(frozen=True)
class McpContextSearchArm:
    """A context at the budget, then one search at the search tool's default limits.

    This is the route an agent takes when it reads its context and then
    searches with its question.  Its allowance is the budget plus the search
    step's limits, and its sizes are the sum of both responses.  Each response
    is still checked against the limits of its own call, so room left by one
    step cannot hide an overrun in the other.
    """

    arm_id: str = WORKFLOW_ARM_ID
    connect: Connect = McpReads
    reads_sections: bool = True

    def allowance(self, budget: Budget) -> Budget:
        return Budget(
            name=budget.name,
            max_items=budget.max_items + SEARCH_STEP_BUDGET.max_items,
            max_chars=budget.max_chars + SEARCH_STEP_BUDGET.max_chars,
        )

    def read(self, sandbox: MemoryEvalSandbox, case: ProjectCase, budget: Budget) -> Delivery:
        scenario = case.scenario
        started = time.perf_counter()
        context, search = self.connect(sandbox).call(
            scenario,
            [
                (
                    "recall_context",
                    {"max_items": budget.max_items, "max_chars": budget.max_chars},
                ),
                (
                    "search_memory",
                    {
                        "query": scenario.query,
                        "limit": SEARCH_STEP_BUDGET.max_items,
                        "max_chars": SEARCH_STEP_BUDGET.max_chars,
                    },
                ),
            ],
        )
        duration_ms = (time.perf_counter() - started) * 1_000
        packet = _merge(
            frozen_packet(
                _objects(context.get("records")),
                scenario,
                truncated=_flag(context.get("truncated"), "Context truncation marker"),
                duration_ms=duration_ms,
            ),
            frozen_packet(
                _objects(search.get("results")),
                scenario,
                truncated=_flag(search.get("truncated"), "Search truncation marker"),
                duration_ms=0.0,
            ),
        )
        return deliver(
            packet,
            [context, search],
            scenario,
            sections=True,
            limits=[budget, SEARCH_STEP_BUDGET],
        )


def arms() -> tuple[ReadArm, ...]:
    """Return the lab's arms in stable report order."""

    by_variant = {provider.variant_id: provider for provider in providers()}
    built: dict[str, ReadArm] = {
        "no_memory_v1": ProviderArm("no_memory_v1", by_variant["no_memory_v1"]),
        CONTEXT_ARM_ID: ProviderArm(
            CONTEXT_ARM_ID, by_variant[CONTEXT_PROVIDER_ID], reads_sections=True
        ),
        "recall_fts_v1": ProviderArm("recall_fts_v1", by_variant["recall_fts_v1"]),
        SEARCH_ARM_ID: McpSearchArm(),
        WORKFLOW_ARM_ID: McpContextSearchArm(),
        "oracle_v1": ProviderArm("oracle_v1", by_variant["oracle_v1"]),
    }
    return tuple(built[arm_id] for arm_id in ARM_IDS)


def score_delivery(
    *,
    arm_id: str,
    budget: Budget,
    case: ProjectCase,
    repetition: int,
    delivery: Delivery,
) -> DeliveryObservation:
    """Score one read against the case's judgments.

    ``budget`` is the arm's allowance for this read.  The records list is
    scored by the memory evaluation lab's frozen rules.  A read is sufficient
    when every required record was delivered, in a records list or carried by a
    reserved section, with no hard violation in a records list, no call over
    its own limits, and no reserved section naming a record outside the
    project.
    """

    scenario = case.scenario
    records = score_packet(
        variant=arm_id,
        budget=budget,
        scenario=scenario,
        repetition=repetition,
        packet=delivery.packet,
    )
    named = tuple(dict.fromkeys((*delivery.listed_ids, *delivery.packet.record_ids)))
    delivered = {*delivery.packet.record_ids, *delivery.carried_ids}
    required = scenario.required_ids
    scope_exposure = sum(item not in scenario.scope_ids for item in delivery.listed_ids)
    delivered_recall = sum(item in delivered for item in required) / len(required)
    return DeliveryObservation(
        records=records,
        placement=case.placement,
        listed_recall=sum(item in named for item in required) / len(required),
        delivered_recall=delivered_recall,
        sufficient=(
            delivered_recall == 1.0
            and records.hard_violation_count == 0
            and delivery.limit_violations == 0
            and scope_exposure == 0
        ),
        stale_named=len(set(named) & set(scenario.stale_ids)),
        section_items=len(delivery.listed_ids),
        withheld_sections=delivery.withheld_sections,
        section_scope_exposure=scope_exposure,
        response_chars=delivery.response_chars,
        response_tokens=delivery.response_tokens,
        payload_mismatches=delivery.payload_mismatches,
        named_ids=named,
        sections_sha256=delivery.sections_sha256,
        allowed_chars=budget.max_chars,
        limit_violations=delivery.limit_violations,
    )


__all__ = [
    "CONTEXT_ARM_ID",
    "SEARCH_ARM_ID",
    "WORKFLOW_ARM_ID",
    "McpContextSearchArm",
    "McpSearchArm",
    "ProviderArm",
    "ReadArm",
    "arms",
    "deliver",
    "frozen_packet",
    "over_limits",
    "score_delivery",
    "section_items",
    "sections_digest",
    "withheld_sections",
]
