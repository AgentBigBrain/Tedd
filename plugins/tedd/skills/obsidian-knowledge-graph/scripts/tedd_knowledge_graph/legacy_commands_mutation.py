"""Capture and maintenance mutation handlers for legacy and v4 stores."""

from __future__ import annotations

import argparse
import contextlib
import copy
import sqlite3
from pathlib import Path
from typing import cast

from .legacy_dispatch_contracts import (
    CommandResponse,
    JSONMap,
    LegacyDispatchDependencies,
    V4Bindings,
    ValidationResult,
)


def _integer_value(value: object, *, label: str) -> int:
    if not isinstance(value, (bool, int, float, str)):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def _list_value(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return cast(list[object], value)


def _string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return cast(list[str], value)


def v4_mutation_response(
    args: argparse.Namespace,
    *,
    config: JSONMap,
    store_path: Path,
    bindings: V4Bindings,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    if args.command == "redact":
        result = bindings.redact(store_path, args)
    else:
        result = bindings.resolve_loop(store_path, args)
    projection = None
    if getattr(args, "project", False):
        vault, root_name = dependencies.resolve_projection(args, config)
        projection = bindings.project(
            store_path,
            vault=vault,
            root_name=root_name,
            mode=args.mode,
            manifest_key=dependencies.normalize_space(config.get("projection_manifest_key")),
            root_id=(dependencies.normalize_space(config.get("projection_root_id")) or None),
            timeout_seconds=args.lock_timeout,
        )
    with bindings.open_store(store_path, readonly=True) as current_store:
        validation = current_store.validate(full=False)
    return CommandResponse(
        {
            **result,
            "validation": validation,
            "projection": projection,
        },
        exit_code=0 if validation["valid"] else 1,
        ensure_ascii=False,
    )


def capture_response(
    args: argparse.Namespace,
    config: JSONMap,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    if getattr(args, "plan_output", None) or getattr(args, "expect_plan_file", None):
        raise ValueError("--plan-output and --expect-plan-file require a v4 store")
    if getattr(args, "handoff", False) or getattr(args, "handoff_prompt_id", None):
        raise ValueError("handoff receipts require a v4 store")
    packet = dependencies.read_packet(args.input)
    has_coverage = packet.get("coverage") is not None
    capture_lock = (
        contextlib.nullcontext()
        if args.dry_run
        else dependencies.store_lock(store_path, args.lock_timeout)
    )
    with capture_lock:
        graph, canonical_digest, stored_version = dependencies.load_graph_with_digest(store_path)
        dependencies.require_current_graph_schema(stored_version)
        if (
            (args.allow_replace or args.allow_redirect or has_coverage)
            and not args.dry_run
            and (
                not dependencies.normalize_space(args.expect_fingerprint)
                or args.expect_watermark is None
            )
        ):
            raise ValueError(
                "Applying coverage, --allow-replace, or --allow-redirect requires "
                "--expect-fingerprint and --expect-watermark from a dry run"
            )
        working = copy.deepcopy(graph)
        result = dependencies.capture_packet(
            working,
            packet,
            authority_ceiling=args.authority_ceiling,
            allow_replace=args.allow_replace,
            allow_redirect=args.allow_redirect,
            producer=cast(JSONMap, dependencies.producer_from_args(args)),
            based_on_watermark=args.based_on_watermark,
            discovery_kind=args.discovery_kind,
        )
        if (
            args.expect_watermark is not None
            and _integer_value(graph.get("next_watermark", 1), label="Graph next_watermark")
            != args.expect_watermark
            and result.get("status") != "duplicate"
        ):
            raise ValueError(
                "Graph watermark changed after the reviewed dry run; rerun review before applying"
            )
        expected_fingerprint = dependencies.normalize_space(args.expect_fingerprint)
        if expected_fingerprint and result.get("fingerprint") != expected_fingerprint:
            raise ValueError(
                "Capture fingerprint does not match the reviewed dry run; inspect the "
                "packet before applying it"
            )
        if (
            result["status"] == "duplicate"
            and dependencies.index_status(
                graph,
                store_path,
                canonical_digest,
            )["current"]
        ):
            validation_result: ValidationResult = {
                "valid": True,
                "errors": [],
                "warnings": [],
                "counts": {
                    collection: len(
                        _list_value(
                            graph[collection],
                            label=f"Graph collection {collection}",
                        )
                    )
                    for collection in dependencies.collections
                },
            }
            validation: JSONMap = {
                **validation_result,
                "cached": True,
                "cache_basis": "digest-bound validated recall index",
            }
        else:
            validation_result = dependencies.validate_graph(working)
            validation = dict(validation_result)
        if not validation_result["valid"]:
            raise ValueError("Capture failed validation: " + "; ".join(validation_result["errors"]))
        committed = False
        if not args.dry_run and result["status"] != "duplicate":
            dependencies.save_graph(store_path, working)
            committed = True
    index_result = None
    projection = None
    if not args.dry_run:
        try:
            index_result = dependencies.ensure_latest_recall_index(
                store_path,
                args.lock_timeout,
            )
        except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
            index_result = {"status": "degraded", "error": str(error)}
        try:
            projection = dependencies.maybe_project_latest(args, config, store_path)
        except (OSError, ValueError) as error:
            committed_text = "Capture was committed" if committed else "Capture was already present"
            raise ValueError(
                f"{committed_text} in {store_path}, but projection failed: {error}. "
                "Fix the projection target and rerun project."
            ) from error
    output_result = dict(result)
    if args.dry_run and output_result.get("status") == "captured":
        output_result["status"] = "would_capture"
    return CommandResponse(
        {
            **output_result,
            "dry_run": bool(args.dry_run),
            "validation": validation,
            "index": index_result,
            "projection": projection,
        },
        ensure_ascii=False,
    )


def mutation_response(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    config: JSONMap,
    store_path: Path,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    with dependencies.store_lock(store_path, args.lock_timeout):
        graph, _, stored_version = dependencies.load_graph_with_digest(store_path)
        dependencies.require_current_graph_schema(stored_version)
        if args.command == "redact":
            result = dependencies.redact_record(graph, args.id)
            action = "redact"
            changed_ids = _string_list(result.get("cascaded_ids"), label="Redaction cascaded_ids")
        elif args.command == "resolve-loop":
            result = dependencies.resolve_loop(graph, args.id, args.resolution)
            action = "resolve_loop"
            changed_ids = [args.id]
        else:
            parser.error(f"Unsupported command: {args.command}")
        changed = bool(result.get("changed", True))
        if changed:
            dependencies.mutation_journal(
                graph,
                action,
                changed_ids,
                producer=dependencies.producer_from_args(args),
                based_on_watermark=args.based_on_watermark,
                discovery_kind=(
                    args.discovery_kind
                    or ("redaction" if action == "redact" else "loop_resolution")
                ),
            )
        validation = dependencies.validate_graph(graph)
        if not validation["valid"]:
            raise ValueError("Mutation failed validation: " + "; ".join(validation["errors"]))
        if changed:
            dependencies.save_graph(store_path, graph)
    index_result = None
    if changed:
        try:
            index_result = dependencies.ensure_latest_recall_index(
                store_path,
                args.lock_timeout,
            )
        except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
            index_result = {"status": "degraded", "error": str(error)}
    try:
        projection = dependencies.maybe_project_latest(args, config, store_path)
    except (OSError, ValueError) as error:
        committed_text = "Mutation was committed" if changed else "Mutation was already applied"
        raise ValueError(
            f"{committed_text}, but projection failed: {error}. Fix the projection "
            "target and rerun project."
        ) from error
    return CommandResponse(
        {
            "status": "updated" if changed else "unchanged",
            "result": result,
            "validation": validation,
            "index": index_result,
            "projection": projection,
        },
        ensure_ascii=False,
    )
