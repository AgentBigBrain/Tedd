"""Initialization, migration, backup, export, and maintenance v4 CLI commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from .v4_cli_contracts import (
    CommandServices,
    InitializeHandler,
    InitializeRequest,
    ReviewSanitizer,
    UpgradeHandler,
    UpgradeRequest,
    V4CLIError,
)
from .v4_cli_values import argument, integer_value, object_mapping, optional_text

PREOPEN_MAINTENANCE_COMMANDS = frozenset({"init-v4", "upgrade-v4"})
OPEN_MAINTENANCE_COMMANDS = frozenset({"backup", "checkpoint", "export", "reindex"})
MAINTENANCE_COMMANDS = PREOPEN_MAINTENANCE_COMMANDS | OPEN_MAINTENANCE_COMMANDS
READONLY_COMMANDS = frozenset({"export"})


def handle_preopen_maintenance_command(
    command: str,
    args: argparse.Namespace,
    canonical_path: Path,
    services: CommandServices,
    *,
    v3_store_path: str | Path | None,
    initialize_handler: InitializeHandler | None,
    upgrade_handler: UpgradeHandler | None,
) -> object:
    """Dispatch initialization or migration before opening the target store."""

    if command == "init-v4":
        if initialize_handler is None:
            raise V4CLIError("init-v4 requires an explicit initialize_handler callback")
        return initialize_handler(InitializeRequest(canonical_path))
    if command == "upgrade-v4":
        if upgrade_handler is None:
            raise V4CLIError("upgrade-v4 requires an explicit upgrade_handler callback")
        dry_run = bool(argument(args, "dry_run"))
        apply = bool(argument(args, "apply"))
        if dry_run == apply:
            raise V4CLIError("choose exactly one of --dry-run or --apply")
        if v3_store_path is None:
            raise V4CLIError("upgrade-v4 requires the configured v3 source path")
        expected_sha = optional_text(argument(args, "expect_source_sha256"))
        expected_plan = optional_text(argument(args, "expect_plan_fingerprint"))
        if apply and (not expected_sha or not expected_plan):
            raise V4CLIError("--apply requires both dry-run digest and plan fingerprint")
        backup_value = argument(args, "backup_output")
        backup_path = (
            services.safe_output_path(backup_value, "upgrade backup") if backup_value else None
        )
        envelope = None if dry_run else services.producer_envelope(args, require_discovery=False)
        request = UpgradeRequest(
            source_path=Path(v3_store_path).expanduser().resolve(),
            target_path=canonical_path,
            dry_run=dry_run,
            expected_source_sha256=expected_sha,
            expected_plan_fingerprint=expected_plan,
            backup_output=backup_path,
            envelope=envelope,
        )
        return upgrade_handler(request)
    raise V4CLIError(f"unsupported pre-open v4 maintenance command: {command}")


def _backup(args: argparse.Namespace, store: object, services: CommandServices) -> object:
    output = services.safe_output_path(
        argument(args, "output"),
        "backup output",
        replacement_supported=False,
    )
    value = object_mapping(
        services.invoke(store, "backup", destination=output, overwrite=False),
        "backup result",
    )
    value["operational_state_included"] = True
    return value


def _export(
    args: argparse.Namespace,
    store: object,
    services: CommandServices,
    review_sanitizer: ReviewSanitizer | None,
) -> object:
    force = bool(argument(args, "force"))
    output = services.safe_output_path(argument(args, "output"), "export output", force=force)
    exported = services.invoke(store, "export_current_graph")
    if argument(args, "mode") == "review_safe":
        exported = services.apply_review_sanitizer(exported, review_sanitizer)
    try:
        exported_mapping = object_mapping(exported, "export result")
    except V4CLIError:
        exported_mapping = None
    if exported_mapping is not None and exported_mapping.get("output_path"):
        return exported_mapping
    bytes_written = services.write_json_atomic(output, exported, overwrite=force)
    return {"output_path": str(output), "bytes_written": bytes_written}


def _checkpoint(args: argparse.Namespace, store: object, services: CommandServices) -> object:
    if bool(argument(args, "audit")):
        dry_run = bool(argument(args, "dry_run"))
        apply = bool(argument(args, "apply"))
        if dry_run == apply:
            raise V4CLIError("audit checkpoint requires exactly one of --dry-run or --apply")
        state = object_mapping(services.state(store), "store state", none_as_empty=True)
        plan: dict[str, object] = {
            "operation": "create_audit_checkpoint",
            "store_id": state.get("store_id"),
            "generation": state.get("generation"),
            "watermark": state.get("watermark"),
            "journal_head_hash": state.get("journal_head_hash"),
        }
        fingerprint = services.maintenance_fingerprint(plan)
        if dry_run:
            return {
                "status": "would_checkpoint",
                "plan": plan,
                "plan_fingerprint": fingerprint,
            }

        expected_watermark = services.nonnegative_int(
            argument(args, "expect_watermark"), "expected watermark"
        )
        expected_fingerprint = services.bounded_text(
            argument(args, "expect_plan_fingerprint"),
            "expected plan fingerprint",
            128,
        )
        current_watermark = integer_value(state.get("watermark", -1), "store watermark")
        if expected_watermark != current_watermark:
            raise V4CLIError("store watermark changed after checkpoint dry-run")
        if expected_fingerprint != fingerprint:
            raise V4CLIError("audit checkpoint plan differs from dry-run")
        services.producer_envelope(args, require_discovery=False)
        value = object_mapping(
            services.invoke(store, "create_audit_checkpoint"),
            "audit checkpoint result",
        )
        value["plan_fingerprint"] = fingerprint
        return value

    if (
        bool(argument(args, "apply"))
        or bool(argument(args, "expect_plan_fingerprint"))
        or argument(args, "expect_watermark") is not None
    ):
        raise V4CLIError("audit-only options require --audit")
    return services.invoke(store, "checkpoint", mode="passive")


def handle_maintenance_command(
    command: str,
    args: argparse.Namespace,
    store: object,
    services: CommandServices,
    review_sanitizer: ReviewSanitizer | None,
) -> object:
    """Dispatch one maintenance command against an open store."""

    if command == "backup":
        return _backup(args, store, services)
    if command == "export":
        return _export(args, store, services, review_sanitizer)
    if command == "checkpoint":
        return _checkpoint(args, store, services)
    if command == "reindex":
        return services.reindex_store(store)
    raise V4CLIError(f"unsupported open v4 maintenance command: {command}")
