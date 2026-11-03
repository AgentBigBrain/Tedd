"""Pure scoring and detector logic for memory evaluation packets."""

from __future__ import annotations

from .contracts import ALLOWED_RECORD_PREFIX, Budget, ContextPacket, Observation, Scenario


def _exposure(record_ids: tuple[str, ...], judged_ids: tuple[str, ...]) -> int:
    return len(set(record_ids) & set(judged_ids))


def _ordering_accuracy(
    record_ids: tuple[str, ...], pairs: tuple[tuple[str, str], ...]
) -> float | None:
    if not pairs:
        return None
    ranks = {record_id: index for index, record_id in enumerate(record_ids, start=1)}
    correct = 0
    for current_id, stale_id in pairs:
        current_rank = ranks.get(current_id)
        stale_rank = ranks.get(stale_id)
        if current_rank is not None and (stale_rank is None or current_rank < stale_rank):
            correct += 1
    return correct / len(pairs)


def score_packet(
    *,
    variant: str,
    budget: Budget,
    scenario: Scenario,
    repetition: int,
    packet: ContextPacket,
) -> Observation:
    """Score one packet against explicit scenario judgments."""

    if (
        isinstance(packet.evaluation_tokens, bool)
        or not isinstance(packet.evaluation_tokens, int)
        or packet.evaluation_tokens < 0
        or (bool(packet.record_ids) != (packet.evaluation_tokens > 0))
    ):
        raise ValueError(
            "evaluation tokens must be zero for an empty packet and positive otherwise"
        )

    selected = set(packet.record_ids)
    required = set(scenario.required_ids)
    relevant = set(scenario.relevant_ids)
    relevant_hits = len(selected & relevant)
    ranks = {record_id: index for index, record_id in enumerate(packet.record_ids, start=1)}
    required_ranks = [ranks[record_id] for record_id in scenario.required_ids if record_id in ranks]
    all_required_present = len(required_ranks) == len(required)
    first_relevant_rank = min(
        (rank for record_id, rank in ranks.items() if record_id in relevant),
        default=None,
    )
    stale_exposure = _exposure(packet.record_ids, scenario.stale_ids)
    untrusted_exposure = _exposure(packet.record_ids, scenario.untrusted_ids)
    sensitive_exposure = _exposure(packet.record_ids, scenario.sensitive_ids)
    canary_exposure = _exposure(packet.record_ids, scenario.canary_ids)
    scope_exposure = len(
        (selected - scenario.scope_ids) | (selected & set(scenario.scope_forbidden_ids))
    )
    namespace_exposure = sum(
        not record_id.startswith(ALLOWED_RECORD_PREFIX) for record_id in packet.record_ids
    )
    fixture_collections = {fixture.record_id: fixture.collection for fixture in scenario.records}
    budget_overflow = (
        packet.item_count > budget.max_items
        or packet.payload_chars > budget.max_chars
        or packet.reported_chars > budget.max_chars
    )
    hard_violations = (
        packet.assertion_failures
        + packet.untrusted_text_exposure
        + packet.sensitive_text_exposure
        + packet.canary_text_exposure
        + packet.payload_namespace_exposure
        + packet.payload_reference_scope_exposure
        + scope_exposure
        + namespace_exposure
    )
    return Observation(
        variant=variant,
        budget=budget.name,
        case_id=scenario.case_id,
        family=scenario.family,
        partition=scenario.partition,
        required_collections=tuple(
            sorted({fixture_collections[record_id] for record_id in scenario.required_ids})
        ),
        repetition=repetition,
        required_recall=len(selected & required) / len(required),
        precision=relevant_hits / packet.item_count if packet.item_count else 0.0,
        reciprocal_rank=0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank,
        worst_required_rank=max(required_ranks) if all_required_present else None,
        current_over_stale_accuracy=_ordering_accuracy(
            packet.record_ids, scenario.current_before_stale
        ),
        sufficient=all_required_present and hard_violations == 0 and not budget_overflow,
        stale_exposure=stale_exposure,
        untrusted_exposure=untrusted_exposure,
        sensitive_exposure=sensitive_exposure,
        canary_exposure=canary_exposure,
        scope_exposure=scope_exposure,
        namespace_exposure=namespace_exposure,
        assertion_failures=packet.assertion_failures,
        untrusted_text_exposure=packet.untrusted_text_exposure,
        sensitive_text_exposure=packet.sensitive_text_exposure,
        canary_text_exposure=packet.canary_text_exposure,
        payload_namespace_exposure=packet.payload_namespace_exposure,
        payload_reference_scope_exposure=packet.payload_reference_scope_exposure,
        budget_overflow=budget_overflow,
        item_count=packet.item_count,
        payload_chars=packet.payload_chars,
        evaluation_tokens=packet.evaluation_tokens,
        truncated=packet.truncated,
        duration_ms=packet.duration_ms,
        relevant_hits=relevant_hits,
        ordered_ids=packet.record_ids,
    )


def hard_detector_self_test(catalog: tuple[Scenario, ...], budget: Budget) -> bool:
    """Prove every hard detector changes state when given a violating packet."""

    by_case = {scenario.case_id: scenario for scenario in catalog}

    def observe(
        case_id: str,
        record_ids: tuple[str, ...],
        *,
        chars: int = 1,
        assertion_failures: int = 0,
        untrusted_text: int = 0,
        sensitive_text: int = 0,
        canary_text: int = 0,
        payload_namespace: int = 0,
        payload_reference_scope: int = 0,
    ) -> Observation:
        return score_packet(
            variant="detector_self_test",
            budget=budget,
            scenario=by_case[case_id],
            repetition=0,
            packet=ContextPacket(
                record_ids=record_ids,
                payload_chars=chars,
                reported_chars=chars,
                item_count=len(record_ids),
                truncated=False,
                duration_ms=0.0,
                evaluation_tokens=len(record_ids),
                assertion_failures=assertion_failures,
                untrusted_text_exposure=untrusted_text,
                sensitive_text_exposure=sensitive_text,
                canary_text_exposure=canary_text,
                payload_namespace_exposure=payload_namespace,
                payload_reference_scope_exposure=payload_reference_scope,
            ),
        )

    stale = by_case["current_correction"].stale_ids[0]
    untrusted = by_case["untrusted_instruction"].untrusted_ids[0]
    sensitive = by_case["review_safe_sensitive"].sensitive_ids[0]
    canary = by_case["review_safe_sensitive"].canary_ids[0]
    scope = by_case["same_name_scopes"].scope_forbidden_ids[0]
    foreign = by_case["changed_preference"].required_ids[0]
    return all(
        (
            observe("current_correction", (stale,)).stale_exposure == 1,
            observe("untrusted_instruction", (untrusted,)).untrusted_exposure == 1,
            observe("review_safe_sensitive", (sensitive,)).sensitive_exposure == 1,
            observe("review_safe_sensitive", (canary,)).canary_exposure == 1,
            observe("same_name_scopes", (scope,)).scope_exposure == 1,
            observe("project_resumption", (foreign,)).scope_exposure == 1,
            observe("project_resumption", ("production_record",)).namespace_exposure == 1,
            observe(
                "project_resumption",
                by_case["project_resumption"].required_ids,
                assertion_failures=1,
            ).assertion_failures
            == 1,
            observe(
                "untrusted_instruction",
                by_case["untrusted_instruction"].required_ids,
                untrusted_text=1,
            ).untrusted_text_exposure
            == 1,
            observe(
                "review_safe_sensitive",
                by_case["review_safe_sensitive"].required_ids,
                sensitive_text=1,
            ).sensitive_text_exposure
            == 1,
            observe(
                "review_safe_sensitive",
                by_case["review_safe_sensitive"].required_ids,
                canary_text=1,
            ).canary_text_exposure
            == 1,
            observe(
                "project_resumption",
                by_case["project_resumption"].required_ids,
                payload_namespace=1,
            ).payload_namespace_exposure
            == 1,
            observe(
                "project_resumption",
                by_case["project_resumption"].required_ids,
                payload_reference_scope=1,
            ).payload_reference_scope_exposure
            == 1,
            observe(
                "project_resumption",
                by_case["project_resumption"].required_ids,
                chars=budget.max_chars + 1,
            ).budget_overflow,
        )
    )


__all__ = ["hard_detector_self_test", "score_packet"]
