"""Consumer, subscription, inbox, and context commands for the v4 CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from .v4_application_contracts import (
    BrainStatusRequest,
    GraphReadError,
    GraphReadErrorCategory,
    GraphReadScope,
    GraphReadSession,
    LoopsReviewRequest,
    PullChangesRequest,
    ReadScopeKind,
    RecallContextRequest,
)
from .v4_cli_contracts import CommandServices, GraphReadOperations, V4CLIError
from .v4_cli_values import (
    argument,
    sequence_value,
    trimmed_if_truthy,
)

COORDINATION_COMMANDS = frozenset(
    {
        "consumer-rebind",
        "consumer-register",
        "consumer-status",
        "context",
        "inbox",
        "inbox-ack",
        "loops-review",
        "subscribe",
    }
)
READONLY_COMMANDS = frozenset({"consumer-status", "context", "inbox", "loops-review"})
SERVICE_COORDINATION_COMMANDS = READONLY_COMMANDS
TRUSTED_LOCAL_SUBSCRIPTION_COMMANDS = frozenset({"context", "inbox", "loops-review"})


def _optional_nonnegative(value: object, label: str, services: CommandServices) -> int | None:
    if value is None:
        return None
    return services.nonnegative_int(value, label)


def _local_session() -> GraphReadSession:
    return GraphReadSession(agent_id=None, task_id=None, run_id=None, consumer_id=None)


def _service_payload(
    call: Callable[[], object],
    *,
    historical_subscription_id: str | None = None,
) -> dict[str, object]:
    try:
        result = call()
    except GraphReadError as error:
        if (
            historical_subscription_id is not None
            and error.category is GraphReadErrorCategory.SCOPE_DENIED
        ):
            raise V4CLIError(f"Unknown active subscription: {historical_subscription_id}") from None
        raise V4CLIError(str(error)) from None
    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        raise V4CLIError("graph read service returned an invalid payload")
    return payload


def handle_coordination_command(
    command: str,
    args: argparse.Namespace,
    store: object,
    services: CommandServices,
    *,
    read_service: GraphReadOperations | None = None,
    max_page_size: int,
    max_context_chars: int,
) -> object:
    """Dispatch one coordination command against an open store."""

    if command == "consumer-status":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        consumer_id = services.opaque_id(argument(args, "consumer_id"), "consumer ID", record=True)
        payload = _service_payload(
            lambda: read_service.brain_status(
                BrainStatusRequest(
                    session=_local_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    consumer_id=consumer_id,
                )
            )
        )
        consumer = payload.get("consumer")
        if not isinstance(consumer, dict):
            raise V4CLIError("graph read service returned an invalid consumer payload")
        return consumer
    if command == "consumer-register":
        envelope = services.producer_envelope(args, require_discovery=False)
        return services.invoke(
            store,
            "register_consumer",
            consumer_id=services.opaque_id(
                argument(args, "consumer_id"), "consumer ID", record=True
            ),
            agent_id=envelope.producer_agent_id,
            task_id=envelope.task_id,
            parent_task_id=envelope.parent_task_id,
            metadata={
                "producer_run_id": envelope.producer_run_id,
                "based_on_watermark": envelope.based_on_watermark,
            },
        )
    if command == "consumer-rebind":
        envelope = services.producer_envelope(args, require_discovery=False)
        existing_task_id = services.opaque_id(
            argument(args, "existing_task_id"), "existing task ID"
        )
        if envelope.task_id != existing_task_id:
            raise V4CLIError("--task-id must match --existing-task-id for consumer rebind")
        return services.invoke(
            store,
            "rebind_consumer",
            consumer_id=services.opaque_id(
                argument(args, "consumer_id"), "consumer ID", record=True
            ),
            task_id=existing_task_id,
            expected_old_agent_id=services.opaque_id(
                argument(args, "expected_old_agent_id"), "expected old agent ID"
            ),
            new_agent_id=envelope.producer_agent_id,
            parent_task_id=envelope.parent_task_id,
            metadata={
                "producer_run_id": envelope.producer_run_id,
                "based_on_watermark": envelope.based_on_watermark,
            },
        )
    if command == "subscribe":
        start_values = [
            services.nonnegative_int(value, "subscription start watermark")
            for value in (
                argument(args, "start_watermark"),
                argument(args, "backfill_from_watermark"),
            )
            if value is not None
        ]
        if len(set(start_values)) > 1:
            raise V4CLIError("start and backfill watermarks disagree")
        selector: dict[str, object] = {
            "all": bool(argument(args, "all")),
            "record_ids": list(
                services.unique_ids(
                    sequence_value(argument(args, "record_id"), "subscription record IDs"),
                    "subscription record ID",
                )
            ),
            "entity_ids": list(
                services.unique_ids(
                    sequence_value(argument(args, "subject_id"), "subscription subject IDs"),
                    "subscription subject ID",
                )
            ),
            "collections": [
                str(item)
                for item in sequence_value(argument(args, "collection"), "subscription collections")
            ],
            "discovery_kinds": [
                str(item)
                for item in sequence_value(
                    argument(args, "discovery_kind"), "subscription discovery kinds"
                )
            ],
        }
        if bool(selector["all"]) and any(
            bool(selector[key])
            for key in ("record_ids", "entity_ids", "collections", "discovery_kinds")
        ):
            raise V4CLIError("--all cannot be combined with scoped subscription selectors")
        return services.invoke(
            store,
            "subscribe",
            consumer_id=services.opaque_id(
                argument(args, "consumer_id"), "consumer ID", record=True
            ),
            selector=selector,
            subscription_id=services.optional_opaque_id(
                argument(args, "subscription_id"),
                "subscription ID",
                record=True,
            ),
            start_watermark=None if not start_values else start_values[0],
        )
    if command == "inbox":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        subscription_id = services.opaque_id(
            argument(args, "subscription_id"), "subscription ID", record=True
        )
        return _service_payload(
            lambda: read_service.pull_changes(
                PullChangesRequest(
                    session=_local_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                    cursor=trimmed_if_truthy(argument(args, "cursor")),
                    limit=services.positive_int(
                        argument(args, "limit"), "inbox limit", max_page_size
                    ),
                    through_watermark=_optional_nonnegative(
                        argument(args, "through_watermark"), "through watermark", services
                    ),
                )
            ),
            historical_subscription_id=subscription_id,
        )
    if command == "inbox-ack":
        return services.invoke(
            store,
            "ack",
            subscription_id=services.opaque_id(
                argument(args, "subscription_id"), "subscription ID", record=True
            ),
            through_watermark=services.nonnegative_int(
                argument(args, "through_watermark"), "through watermark"
            ),
            ack_token=services.bounded_text(argument(args, "ack_token"), "ack token", 16_384),
        )
    if command == "context":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        subscription_id = services.opaque_id(
            argument(args, "subscription_id"), "subscription ID", record=True
        )
        max_items = services.positive_int(
            argument(args, "max_items"), "context item budget", max_page_size
        )
        max_chars = services.positive_int(
            argument(args, "max_chars"), "context character budget", max_context_chars
        )
        fingerprint = None
        if bool(argument(args, "plan")):
            fingerprint = services.bounded_text(
                argument(args, "fingerprint"), "plan fingerprint", 256
            )
        return _service_payload(
            lambda: read_service.recall_context(
                RecallContextRequest(
                    session=_local_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                    max_items=max_items,
                    max_chars=max_chars,
                    plan_fingerprint=fingerprint,
                )
            ),
            historical_subscription_id=subscription_id,
        )
    if command == "loops-review":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        subscription_id = services.opaque_id(
            argument(args, "subscription_id"), "subscription ID", record=True
        )
        consumer_id = services.opaque_id(argument(args, "consumer_id"), "consumer ID", record=True)
        request = LoopsReviewRequest(
            session=GraphReadSession(None, None, None, consumer_id),
            scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
            overdue_days=services.nonnegative_int(argument(args, "overdue_days"), "overdue days"),
            age_days=services.positive_int(argument(args, "age_days"), "loop age", 36_500),
            limit=services.positive_int(argument(args, "limit"), "loop limit", 20),
        )
        return _service_payload(lambda: read_service.loops_review(request))
    raise V4CLIError(f"unsupported v4 coordination command: {command}")
