"""Typed command dispatch for the public ``knowledge_graph.py`` facade.

The legacy module remains the compatibility surface and owns graph semantics.
This module owns routing and error translation; cohesive command behavior lives
in typed family modules. Every dependency is still assembled by the facade at
call time so tests and embedders retain the historical replacement seams.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import legacy_commands_mutation as _mutation_commands
from . import legacy_commands_projection as _projection_commands
from . import legacy_commands_read as _read_commands
from .legacy_dispatch_contracts import (
    CommandResponse,
    JSONMap,
    LegacyDispatchDependencies,
    V4Bindings,
)

_capture_response = _mutation_commands.capture_response
_mutation_response = _mutation_commands.mutation_response
_v4_mutation_response = _mutation_commands.v4_mutation_response
_migrate_response = _projection_commands.migrate_response
_project_response = _projection_commands.project_response
_v4_project_response = _projection_commands.v4_project_response
_v4_upgrade_response = _projection_commands.v4_upgrade_response
_configured_response = _read_commands.configured_response
_doctor_response = _read_commands.doctor_response
_get_response = _read_commands.get_response
_index_status_response = _read_commands.index_status_response
_recall_response = _read_commands.recall_response
_reindex_response = _read_commands.reindex_response
_validate_response = _read_commands.validate_response


def _render(response: CommandResponse) -> None:
    if isinstance(response.payload, dict) and response.payload.get("command") == "source-evidence":
        # This new bounded packet has a UTF-8 wire contract. Preserve historical
        # formatting/encoding of every existing command for read parity.
        wire = json.dumps(response.payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(wire.encode("utf-8"))
        else:
            sys.stdout.write(wire)
        return
    print(
        json.dumps(
            response.payload,
            indent=2,
            ensure_ascii=response.ensure_ascii,
        )
    )


def _v4_response(
    args: argparse.Namespace,
    *,
    config_path: Path,
    store_path: Path,
    detected_store: str,
    bindings: V4Bindings,
) -> CommandResponse | None:
    if args.command == "init-v4":
        if detected_store not in {"missing", "sqlite_v4"}:
            if detected_store == "json_v3":
                raise ValueError(
                    "init-v4 is only for a fresh installation; run upgrade-v4 for the "
                    "configured v3 JSON graph"
                )
            raise ValueError(
                f"init-v4 refuses existing {detected_store} state at the configured target"
            )
        result = bindings.handle_command(
            args,
            db_path=store_path,
            initialize_handler=lambda request: bindings.initialize(
                request,
                config_path=config_path,
                lock_timeout=args.lock_timeout,
            ),
            review_sanitizer=bindings.sanitize_review_safe,
        )
        return CommandResponse(result, ensure_ascii=False)

    if not bindings.should_handle(
        args,
        v4_enabled=detected_store == "sqlite_v4",
    ):
        return None
    if detected_store != "sqlite_v4":
        raise ValueError(
            f"{args.command} requires an activated v4 SQLite store; run init-v4 for a "
            "fresh installation or upgrade-v4 for v3 migration"
        )
    result = bindings.handle_command(
        args,
        db_path=store_path,
        capture_handler=bindings.capture,
        review_sanitizer=bindings.sanitize_review_safe,
    )
    exit_code = 0
    if args.command == "validate":
        exit_code = 0 if result and result.get("valid") else 1
    return CommandResponse(result, exit_code=exit_code, ensure_ascii=False)


def _v4_projection_or_mutation_response(
    args: argparse.Namespace,
    *,
    config: JSONMap,
    store_path: Path,
    bindings: V4Bindings,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse | None:
    if args.command == "project":
        return _v4_project_response(
            args,
            config=config,
            store_path=store_path,
            bindings=bindings,
            dependencies=dependencies,
        )
    if args.command in {"redact", "resolve-loop"}:
        return _v4_mutation_response(
            args,
            config=config,
            store_path=store_path,
            bindings=bindings,
            dependencies=dependencies,
        )
    return None


def _dispatch(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    dependencies: LegacyDispatchDependencies,
) -> CommandResponse:
    if args.lock_timeout < 0 or args.lock_timeout > 300:
        raise ValueError("--lock-timeout must be between 0 and 300 seconds")
    config_path = dependencies.secure_state_path(args.config, "Configuration")
    if args.command == "configure":
        return _configured_response(args, config_path, dependencies)

    config = dependencies.load_config(config_path)
    store_path = dependencies.resolve_store(args, config)
    privacy_warnings = dependencies.state_privacy_warnings(
        config_path,
        store_path,
        config,
    )
    if privacy_warnings and args.command != "doctor":
        raise ValueError(
            "Canonical graph state must remain outside the Obsidian vault: "
            + "; ".join(privacy_warnings)
        )

    bindings = dependencies.load_v4_bindings()
    detected_store = bindings.detect_store(store_path)
    if args.command == "upgrade-v4":
        if detected_store == "json_v3":
            return _v4_upgrade_response(
                args,
                config_path=config_path,
                store_path=store_path,
                bindings=bindings,
                dependencies=dependencies,
            )
        if detected_store != "sqlite_v4":
            raise ValueError(
                "upgrade-v4 requires the configured canonical store to be a valid v3 JSON graph"
            )
        if not bool(getattr(args, "apply", False)) or bool(getattr(args, "dry_run", False)):
            raise ValueError("upgrade-v4 recovery on an activated v4 store requires --apply")
        manifest_value = dependencies.normalize_space(config.get("v4_migration_manifest"))
        if not manifest_value:
            raise ValueError("upgrade-v4 recovery requires the configured migration manifest")
        expected_source_sha256 = dependencies.normalize_space(
            getattr(args, "expect_source_sha256", None)
        )
        expected_plan_fingerprint = dependencies.normalize_space(
            getattr(args, "expect_plan_fingerprint", None)
        )
        if not expected_source_sha256 or not expected_plan_fingerprint:
            raise ValueError(
                "upgrade-v4 recovery requires the reviewed source digest and plan fingerprint"
            )
        manifest_path = dependencies.secure_state_path(
            manifest_value,
            "v4 migration manifest",
        )
        recovery_source = bindings.migration_recovery_source(
            manifest_path,
            config_path=config_path,
            target_path=store_path,
            expected_source_sha256=expected_source_sha256,
            expected_plan_fingerprint=expected_plan_fingerprint,
            transform_version=bindings.upgrade_transform_version,
        )
        return _v4_upgrade_response(
            args,
            config_path=config_path,
            store_path=store_path,
            bindings=bindings,
            dependencies=dependencies,
            recovery_source=recovery_source,
        )
    if args.command == "migrate" and detected_store == "sqlite_v4":
        raise ValueError(
            "migrate supports legacy JSON stores only; the configured canonical "
            "store is an activated v4 SQLite store"
        )
    v4_response = _v4_response(
        args,
        config_path=config_path,
        store_path=store_path,
        detected_store=detected_store,
        bindings=bindings,
    )
    if v4_response is not None:
        return v4_response

    projection_requested = args.command == "project" or bool(getattr(args, "project", False))
    configured_store_raw = dependencies.normalize_space(config.get("store_path"))
    if projection_requested and configured_store_raw:
        configured_store = dependencies.secure_state_path(
            configured_store_raw,
            "Configured canonical store",
        )
        if store_path != configured_store:
            raise ValueError(
                "Refusing to project from a canonical store other than the one bound "
                "to this vault configuration"
            )
    if detected_store == "sqlite_v4":
        response = _v4_projection_or_mutation_response(
            args,
            config=config,
            store_path=store_path,
            bindings=bindings,
            dependencies=dependencies,
        )
        if response is not None:
            return response

    if args.command == "doctor":
        return _doctor_response(
            config_path,
            config,
            store_path,
            privacy_warnings,
            dependencies,
        )
    if args.command == "validate":
        return _validate_response(store_path, dependencies)
    if args.command == "index-status":
        return _index_status_response(store_path, dependencies)
    if args.command == "reindex":
        return _reindex_response(args, store_path, dependencies)
    if args.command == "recall":
        return _recall_response(args, store_path, dependencies)
    if args.command == "get":
        return _get_response(args, store_path, dependencies)
    if args.command == "project":
        return _project_response(args, config, store_path, dependencies)
    if args.command == "migrate":
        return _migrate_response(args, config, store_path, dependencies)
    if args.command == "capture":
        return _capture_response(args, config, store_path, dependencies)
    return _mutation_response(args, parser, config, store_path, dependencies)


def run_cli(
    parser: argparse.ArgumentParser,
    dependencies: LegacyDispatchDependencies,
) -> int:
    """Parse, dispatch, and preserve the facade's structured error contract."""

    argument_error = dependencies.argument_error
    try:
        args = parser.parse_args()
        response = _dispatch(args, parser, dependencies)
        _render(response)
        return int(response.exit_code)
    except argument_error as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "invalid_arguments",
                    "error": str(error),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except (
        OSError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
        RecursionError,
        RuntimeError,
    ) as error:
        print(
            json.dumps({"status": "error", "error": str(error)}, indent=2),
            file=sys.stderr,
        )
        return 1
