"""Transactional capture command family for the v4 CLI."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from . import handoff_receipts
from .v4_cli_contracts import (
    MAX_CAPTURE_PLAN_BYTES,
    CaptureHandler,
    CaptureRequest,
    CommandServices,
    V4CLIError,
)
from .v4_cli_values import (
    argument,
    optional_text,
    sequence_value,
    trimmed_if_truthy,
)

CAPTURE_COMMANDS = frozenset({"capture", "handoff"})


def _publish_receipt(
    store: handoff_receipts.ReceiptStore,
    identity: handoff_receipts.HandoffIdentity,
    services: CommandServices,
) -> dict[str, object]:
    receipt = handoff_receipts.reconstruct_receipt(store, identity)
    if receipt["status"] != "verified":
        return receipt
    path = handoff_receipts.receipt_path(receipt, identity)
    services.write_handoff_file(path, receipt)
    if not handoff_receipts.verify_receipt(store, identity, services.read_handoff_file(path)):
        return {"status": "pending", "reason": "receipt_not_verified"}
    return receipt


def _handle_handoff(
    args: argparse.Namespace,
    store: object,
    services: CommandServices,
) -> dict[str, object]:
    identity = handoff_receipts.HandoffIdentity(
        services.opaque_id(argument(args, "consumer_id"), "consumer ID", record=True),
        services.opaque_id(argument(args, "producer_agent_id"), "producer agent ID"),
        services.opaque_id(argument(args, "task_id"), "task ID"),
        handoff_receipts.validate_prompt_id(argument(args, "prompt")),
    )
    receipt_store = cast(handoff_receipts.ReceiptStore, store)
    if argument(args, "nothing"):
        if argument(args, "reconstruct"):
            raise V4CLIError("--nothing cannot reconstruct a saved receipt")
        assertion = handoff_receipts.nothing_receipt(
            receipt_store, identity, argument(args, "reason")
        )
        services.write_handoff_file(handoff_receipts.receipt_path(assertion, identity), assertion)
        return assertion
    if argument(args, "reason") is not None:
        raise V4CLIError("--reason requires --nothing")
    if argument(args, "reconstruct"):
        return _publish_receipt(receipt_store, identity, services)
    expected = handoff_receipts.reconstruct_receipt(receipt_store, identity)
    path = handoff_receipts.receipt_path(expected, identity)
    receipt = services.read_handoff_file(path)
    if receipt is None:
        return {"status": "pending", "reason": "receipt_missing"}
    if handoff_receipts.accepts_nothing(receipt_store, identity, receipt):
        return cast(dict[str, object], receipt)
    if not handoff_receipts.verify_receipt(receipt_store, identity, receipt):
        return {"status": "unverified", "reason": "receipt_not_verified"}
    # Verification may observe a newer committed snapshot than the path lookup.
    # Return the proof actually verified, not an older reconstruction.
    return cast(dict[str, object], receipt)


def _plan_integer(value: str) -> int:
    # Early Python 3.10 has no integer digit cap. This schema needs only version 1.
    if value != "1":
        raise ValueError("capture plan accepts only the version 1 integer")
    return 1


def _plan_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("capture plan contains duplicate fields")
        result[key] = value
    return result


def _read_plan_file(value: str, fingerprint: str | None, services: CommandServices) -> str:
    snapshot = services.snapshot_plan_input(services.bounded_text(value, "capture plan path", 4096))
    if snapshot.content is None:
        raise V4CLIError("capture plan must be a private file")
    try:
        plan = json.loads(
            snapshot.content,
            object_pairs_hook=_plan_object,
            parse_int=_plan_integer,
            parse_float=_plan_integer,
            parse_constant=_plan_integer,
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise V4CLIError("capture plan is not valid bounded JSON") from error
    if (
        not isinstance(plan, dict)
        or set(plan) != {"version", "fingerprint", "plan_token"}
        or type(plan["version"]) is not int
        or plan["version"] != 1
        or not isinstance(plan["fingerprint"], str)
        or not isinstance(plan["plan_token"], str)
    ):
        raise V4CLIError("capture plan has an unsupported shape or version")
    if plan["fingerprint"] != fingerprint:
        raise V4CLIError("capture plan differs from the reviewed --expect-fingerprint")
    return services.bounded_text(plan["plan_token"], "expected plan token", MAX_CAPTURE_PLAN_BYTES)


def _plan_document(result: Mapping[str, object], services: CommandServices) -> dict[str, object]:
    """Project only control-plane data, never raw capture content, into the handoff."""
    return {
        "version": 1,
        "fingerprint": services.bounded_text(result.get("fingerprint"), "fingerprint", 128),
        "plan_token": services.bounded_text(
            result.get("plan_token"), "plan token", MAX_CAPTURE_PLAN_BYTES
        ),
    }


def handle_capture_command(
    command: str,
    args: argparse.Namespace,
    store: object,
    services: CommandServices,
    capture_handler: CaptureHandler | None,
) -> object:
    """Validate and dispatch a native capture without owning storage logic."""

    if command not in CAPTURE_COMMANDS:
        raise V4CLIError(f"unsupported v4 capture command: {command}")
    if command == "handoff":
        return _handle_handoff(args, store, services)
    if capture_handler is None:
        raise V4CLIError(
            "v4 capture requires a native capture_handler(store, CaptureRequest) adapter"
        )

    expected_plan_token = optional_text(argument(args, "expect_plan_token"))
    legacy_plan_token = optional_text(argument(args, "plan_token"))
    plan_output = optional_text(argument(args, "plan_output"))
    plan_input = optional_text(argument(args, "expect_plan_file"))
    dry_run = bool(argument(args, "dry_run"))
    handoff = bool(argument(args, "handoff"))
    prompt = argument(args, "handoff_prompt_id")
    if handoff and dry_run:
        raise V4CLIError("--handoff is for apply, not --dry-run")
    if prompt is not None and not handoff:
        raise V4CLIError("--handoff-prompt-id requires --handoff")
    handoff_prompt = handoff_receipts.validate_prompt_id(prompt) if handoff else None
    fingerprint = trimmed_if_truthy(argument(args, "expect_fingerprint"))
    if plan_output and not dry_run:
        raise V4CLIError("--plan-output requires --dry-run")
    if plan_input and dry_run:
        raise V4CLIError("--expect-plan-file is for apply, not --dry-run")
    if plan_input and (expected_plan_token or legacy_plan_token):
        raise V4CLIError("--expect-plan-file cannot be combined with a plan token option")
    if not dry_run and not fingerprint:
        raise V4CLIError("capture apply requires --expect-fingerprint from dry-run")
    if expected_plan_token and legacy_plan_token and expected_plan_token != legacy_plan_token:
        raise V4CLIError("--expect-plan-token and --plan-token disagree")
    plan_token = expected_plan_token or legacy_plan_token
    if plan_token is not None:
        plan_token = services.bounded_text(plan_token, "expected plan token", 16_384)
        if plan_token == "[redacted secret]":
            raise V4CLIError(
                "A redacted placeholder is not a plan token; review a dry run with "
                "--plan-output, then apply with --expect-plan-file and --expect-fingerprint"
            )
    if plan_input:
        plan_token = _read_plan_file(plan_input, fingerprint, services)

    input_value = services.bounded_text(argument(args, "input"), "capture input", 4_096)
    capture_input = services.snapshot_capture_input(input_value)
    expected_watermark = argument(args, "expect_watermark")
    revision_values = tuple(
        str(item)
        for item in sequence_value(argument(args, "expect_revision"), "expected revisions")
    )
    request = CaptureRequest(
        input_path=capture_input.path,
        dry_run=dry_run,
        consumer_id=services.opaque_id(argument(args, "consumer_id"), "consumer ID", record=True),
        envelope=services.producer_envelope(args, require_discovery=True),
        authority_ceiling=services.bounded_text(
            argument(args, "authority_ceiling"), "authority ceiling", 80
        ),
        allow_replace=bool(argument(args, "allow_replace")),
        allow_redirect=bool(argument(args, "allow_redirect")),
        expected_fingerprint=fingerprint,
        expected_global_watermark=(
            None
            if expected_watermark is None
            else services.nonnegative_int(expected_watermark, "expected global watermark")
        ),
        plan_token=plan_token,
        expected_revisions=services.expected_revisions(revision_values),
        expected_absent=tuple(
            services.bounded_text(item, "expected-absent key", 300)
            for item in sequence_value(argument(args, "expect_absent"), "expected-absent keys")
        ),
        input_bytes=capture_input.content,
        source_config_path=(
            Path(str(argument(args, "config"))) if argument(args, "config") else None
        ),
        source_subscription_id=(
            str(argument(args, "source_subscription_id"))
            if argument(args, "source_subscription_id")
            else None
        ),
        source_subject_ids=services.unique_ids(
            sequence_value(argument(args, "source_subject_id"), "source subject IDs"),
            "source subject ID",
        ),
        handoff_prompt_id=handoff_prompt,
    )
    if bool(argument(args, "project")):
        raise V4CLIError(
            "v4 worker capture does not project; the coordinating task projects after the batch"
        )
    result = capture_handler(store, request)
    if handoff_prompt is not None and result.get("status") in {"captured", "duplicate"}:
        identity = handoff_receipts.HandoffIdentity(
            request.consumer_id,
            request.envelope.producer_agent_id,
            request.envelope.task_id,
            handoff_prompt,
        )
        try:
            receipt = _publish_receipt(
                cast(handoff_receipts.ReceiptStore, store), identity, services
            )
        except (OSError, V4CLIError, handoff_receipts.HandoffError):
            # Capture has already committed. Repair only the operational receipt;
            # never turn this into a failed capture or encourage replaying the write.
            receipt = {"status": "pending", "reason": "receipt_publication_failed"}
        return {**result, "handoff_receipt": receipt}
    if plan_output:
        if result.get("status") == "duplicate":
            return {**result, "plan_handoff": "not_needed"}
        # The dry run remains read-only for graph/coordination state. This explicit
        # local artifact is private, no-clobber, and never a substitute for review.
        services.write_plan_file(plan_output, _plan_document(result, services))
        return {**result, "plan_handoff": "saved"}
    return result
