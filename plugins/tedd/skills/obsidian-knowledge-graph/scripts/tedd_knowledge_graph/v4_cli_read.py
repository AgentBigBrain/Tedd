"""Read and query command family for the v4 CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from .v4_application_contracts import (
    BrainStatusRequest,
    GraphReadError,
    GraphReadErrorCategory,
    GraphReadScope,
    GraphReadSession,
    PullChangesRequest,
    ReadScopeKind,
    RecallContextRequest,
)
from .v4_cli_contracts import CommandServices, GraphReadOperations, V4CLIError
from .v4_cli_values import (
    argument,
    object_mapping,
    sequence_value,
    trimmed_if_truthy,
)

READ_COMMANDS = frozenset(
    {
        "changes",
        "doctor",
        "get",
        "history",
        "index-status",
        "recall",
        "status",
        "source-evidence",
        "validate",
    }
)
READONLY_COMMANDS = READ_COMMANDS
SERVICE_READ_COMMANDS = frozenset({"status", "recall", "changes"})


def _optional_nonnegative(value: object, label: str, services: CommandServices) -> int | None:
    if value is None:
        return None
    return services.nonnegative_int(value, label)


def _local_session() -> GraphReadSession:
    """CLI subscriptions use explicit trusted-local authority, never inferred identity."""

    return GraphReadSession(agent_id=None, task_id=None, run_id=None, consumer_id=None)


def _service_payload(
    call: Callable[[], object],
    *,
    stale_message: str | None = None,
) -> object:
    try:
        result = call()
    except GraphReadError as error:
        if stale_message is not None and error.category is GraphReadErrorCategory.STALE_STATE:
            raise V4CLIError(stale_message) from None
        raise V4CLIError(str(error)) from None
    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        raise V4CLIError("graph read service returned an invalid payload")
    return payload


def _recall(
    args: argparse.Namespace,
    services: CommandServices,
    *,
    read_service: GraphReadOperations,
    max_query_chars: int,
    max_page_size: int,
    max_context_chars: int,
) -> object:
    if argument(args, "since_watermark") is not None:
        raise V4CLIError("use the v4 changes command for mutation-feed catch-up")
    if argument(args, "change_cursor") or argument(args, "through_watermark") is not None:
        raise V4CLIError("legacy change-feed options belong to the v4 changes command")
    if argument(args, "consumer_id"):
        raise V4CLIError("use context with an exact subscription ID for task context")

    at_watermark = argument(args, "at_watermark")
    if at_watermark is not None:
        at_watermark = services.nonnegative_int(at_watermark, "at watermark")

    query = str(argument(args, "query") or "").strip()
    if len(query) > max_query_chars:
        raise V4CLIError(f"recall query exceeds {max_query_chars} characters")
    collections = tuple(
        str(item) for item in sequence_value(argument(args, "section"), "recall sections")
    )
    requested_subjects = services.unique_ids(
        sequence_value(argument(args, "subject_id"), "subject IDs"),
        "subject ID",
    )
    max_chars = services.positive_int(
        argument(args, "max_chars"),
        "recall character budget",
        max_context_chars,
    )
    return _service_payload(
        lambda: read_service.recall_context(
            RecallContextRequest(
                session=_local_session(),
                scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                query=query,
                limit=services.positive_int(argument(args, "limit"), "recall limit", max_page_size),
                collections=collections,
                subject_ids=requested_subjects,
                max_chars=max_chars,
                at_watermark=at_watermark,
            )
        ),
        stale_message="recall searches current state; use history for prior revisions",
    )


def handle_read_command(
    command: str,
    args: argparse.Namespace,
    store: object,
    canonical_path: Path,
    services: CommandServices,
    *,
    read_service: GraphReadOperations | None = None,
    max_query_chars: int,
    max_page_size: int,
    max_context_chars: int,
) -> object:
    """Dispatch one validated read/query command."""

    if command == "doctor":
        return {
            "store": services.inspect_store(canonical_path),
            "graph": services.state(store),
            "validation": services.invoke(store, "validate", full=False),
        }
    if command == "validate":
        return services.invoke(store, "validate", full=bool(argument(args, "full")))
    if command == "status":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        return _service_payload(
            lambda: read_service.brain_status(
                BrainStatusRequest(
                    session=_local_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                )
            )
        )
    if command == "index-status":
        validation = object_mapping(
            services.invoke(store, "validate", full=False), "validation result"
        )
        counts = object_mapping(validation.get("counts"), "validation counts", none_as_empty=True)
        return {
            "healthy": bool(validation.get("valid")),
            "record_count": counts.get("records"),
            "validation_errors": validation.get("errors", []),
        }
    if command == "get":
        return services.invoke(
            store,
            "get_record",
            record_id=services.opaque_id(argument(args, "id"), "record ID", record=True),
            revision=_optional_nonnegative(argument(args, "revision"), "revision", services),
        )
    if command == "history":
        return services.invoke(
            store,
            "history",
            record_id=services.opaque_id(argument(args, "id"), "record ID", record=True),
            before_revision=_optional_nonnegative(
                argument(args, "before_revision"), "before revision", services
            ),
            limit=services.positive_int(argument(args, "limit"), "history limit", max_page_size),
        )
    if command == "recall":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        return _recall(
            args,
            services,
            read_service=read_service,
            max_query_chars=max_query_chars,
            max_page_size=max_page_size,
            max_context_chars=max_context_chars,
        )
    if command == "changes":
        if read_service is None:
            raise V4CLIError("graph read service is unavailable")
        since = services.nonnegative_int(argument(args, "since_watermark"), "since watermark")
        through = _optional_nonnegative(
            argument(args, "through_watermark"), "through watermark", services
        )
        if through is not None and through < since:
            raise V4CLIError("through watermark must not precede since watermark")
        subject_values = sequence_value(argument(args, "subject_id"), "subject IDs")
        discovery_kinds = [
            str(item)
            for item in sequence_value(argument(args, "discovery_kind"), "discovery kinds")
        ]
        return _service_payload(
            lambda: read_service.pull_changes(
                PullChangesRequest(
                    session=_local_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    since_watermark=since,
                    through_watermark=through,
                    cursor=trimmed_if_truthy(argument(args, "cursor")),
                    limit=services.positive_int(
                        argument(args, "limit"), "change limit", max_page_size
                    ),
                    subject_ids=services.unique_ids(subject_values, "subject ID"),
                    discovery_kinds=tuple(discovery_kinds),
                )
            )
        )
    raise V4CLIError(f"unsupported v4 read command: {command}")
