"""Argument schema and bounded packet input for the legacy CLI facade."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

JsonObject = dict[str, object]


@dataclass(frozen=True)
class ParserPolicy:
    description: str | None
    default_config_path: Path
    default_lock_timeout_seconds: float
    default_root_name: str
    discovery_kinds: Collection[str]
    source_tiers: Iterable[str]


def _add_provenance_arguments(
    parser: argparse.ArgumentParser,
    discovery_kinds: Collection[str],
) -> None:
    parser.add_argument("--producer-agent-id")
    parser.add_argument("--task-id")
    parser.add_argument("--parent-task-id")
    parser.add_argument("--producer-run-id")
    parser.add_argument(
        "--discovery-kind",
        choices=sorted(discovery_kinds),
        help="Primary durable discovery classification for this mutation",
    )
    parser.add_argument(
        "--based-on-watermark",
        type=int,
        help="Committed graph watermark used as the producer's read basis",
    )


def build_parser(
    parser_type: type[argparse.ArgumentParser],
    policy: ParserPolicy,
    register_v4_subcommands: Callable[[argparse._SubParsersAction[argparse.ArgumentParser]], None],
) -> argparse.ArgumentParser:
    """Build the stable v3 surface, then register additive v4 commands."""

    parser = parser_type(description=policy.description)
    parser.add_argument(
        "--config",
        default=str(policy.default_config_path),
        help="Configuration JSON path",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=policy.default_lock_timeout_seconds,
        help="Maximum seconds to wait for canonical, index, or projection locks",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=parser_type)

    configure_parser = sub.add_parser("configure", help="Configure the Obsidian projection")
    configure_parser.add_argument("--vault", required=True)
    configure_parser.add_argument(
        "--root-name",
        help="Generated folder (new default: Tedd Knowledge; preserves existing roots)",
    )
    configure_parser.add_argument("--store")

    for name in ("doctor", "validate"):
        sub.add_parser(name).add_argument("--store")

    migrate_parser = sub.add_parser("migrate", help="Review or apply a canonical schema migration")
    migrate_parser.add_argument("--store")
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.add_argument(
        "--expect-sha256",
        help="Require the exact canonical digest returned by a prior migration dry run",
    )
    migrate_parser.add_argument(
        "--expect-migration-fingerprint",
        help="Require the deterministic transform fingerprint returned by a prior migration dry run",
    )
    migrate_parser.add_argument("--project", action="store_true")
    migrate_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )
    _add_provenance_arguments(migrate_parser, policy.discovery_kinds)

    for name, help_text in (
        ("reindex", "Build or refresh the disposable SQLite/FTS recall index"),
        ("index-status", "Inspect recall-index freshness without rebuilding it"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--store")
        if name == "reindex":
            item.add_argument(
                "--force",
                action="store_true",
                help="Atomically rebuild even when digest-bound metadata appears current",
            )

    get_parser = sub.add_parser("get", help="Retrieve one exact opaque record ID")
    get_parser.add_argument("--store")
    get_parser.add_argument("--id", required=True)
    get_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )

    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--input", required=True, help="JSON packet path or - for stdin")
    capture_parser.add_argument("--store")
    capture_parser.add_argument("--dry-run", action="store_true")
    capture_parser.add_argument(
        "--expect-fingerprint",
        help="Require the normalized packet fingerprint returned by a prior dry run",
    )
    capture_parser.add_argument(
        "--expect-watermark",
        type=int,
        help=(
            "Strict whole-store guard: v4 requires the dry run's base_watermark; "
            "legacy v3 requires its next watermark. Ordinary v4 capture uses the "
            "reviewed plan instead; --based-on-watermark records provenance"
        ),
    )
    capture_parser.add_argument(
        "--expect-plan-token",
        help=(
            "Require the v4 revision/read-set plan returned by a reviewed dry run; "
            "ignored by legacy v3 stores"
        ),
    )
    capture_parser.add_argument("--project", action="store_true")
    capture_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )
    capture_parser.add_argument(
        "--authority-ceiling",
        default="assistant_inference",
        choices=list(policy.source_tiers),
        help="Maximum evidence authority granted by the caller",
    )
    capture_parser.add_argument(
        "--allow-replace",
        action="store_true",
        help="Allow verified successor claims to supersede a current claim",
    )
    capture_parser.add_argument(
        "--allow-redirect",
        action="store_true",
        help="Allow an evidence-backed entity redirect in this packet",
    )
    _add_provenance_arguments(capture_parser, policy.discovery_kinds)

    recall_parser = sub.add_parser("recall")
    recall_parser.add_argument("query", nargs="?", default="")
    recall_parser.add_argument("--store")
    recall_parser.add_argument("--limit", type=int, default=10)
    recall_parser.add_argument("--max-chars", type=int, default=12000)
    recall_parser.add_argument(
        "--since-watermark",
        type=int,
        help="Return records changed after this committed journal watermark",
    )
    recall_parser.add_argument(
        "--through-watermark",
        type=int,
        help="Pin a paginated change feed to the snapshot watermark returned by its first page",
    )
    recall_parser.add_argument(
        "--change-cursor",
        help="Opaque continuation cursor returned by the previous mutation-feed page",
    )
    recall_parser.add_argument(
        "--change-limit",
        type=int,
        default=50,
        help="Mutation-feed page size (1-500); independent of ranked recall --limit",
    )
    recall_parser.add_argument(
        "--no-index",
        action="store_true",
        help="Use the validated JSON scan backend for diagnostics",
    )
    recall_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )

    project_parser = sub.add_parser("project")
    project_parser.add_argument("--store")
    project_parser.add_argument("--vault")
    project_parser.add_argument("--root-name")
    project_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )
    project_parser.add_argument(
        "--force-current-digest",
        help=(
            "After reviewing an intentional canonical rollback, allow projection "
            "watermark regression only when this equals the current projection payload digest"
        ),
    )

    redact_parser = sub.add_parser("redact")
    redact_parser.add_argument("--store")
    redact_parser.add_argument("--id", required=True)
    redact_parser.add_argument("--project", action="store_true")
    redact_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )
    _add_provenance_arguments(redact_parser, policy.discovery_kinds)

    loop_parser = sub.add_parser("resolve-loop")
    loop_parser.add_argument("--store")
    loop_parser.add_argument("--id", required=True)
    loop_parser.add_argument("--resolution", required=True)
    loop_parser.add_argument("--project", action="store_true")
    loop_parser.add_argument(
        "--mode", default="review_safe", choices=["review_safe", "operator_full"]
    )
    _add_provenance_arguments(loop_parser, policy.discovery_kinds)

    register_v4_subcommands(sub)
    return parser


def read_packet(path: str, *, stdin: BinaryIO, max_capture_bytes: int) -> JsonObject:
    """Read one bounded UTF-8 JSON-object packet from a file or stdin."""

    if path == "-":
        payload = stdin.read(max_capture_bytes + 1)
    else:
        with Path(path).open("rb") as handle:
            payload = handle.read(max_capture_bytes + 1)
    if len(payload) > max_capture_bytes:
        raise ValueError(f"Capture packet exceeds {max_capture_bytes} bytes")
    packet: object = json.loads(payload.decode("utf-8"))
    if not isinstance(packet, dict):
        raise ValueError("Capture packet must be a JSON object")
    return cast(JsonObject, packet)


def require_current_graph_schema(stored_version: str, *, current_version: str) -> None:
    if stored_version != current_version:
        raise ValueError(
            f"Canonical graph is stored as {stored_version!r}; run migrate --dry-run "
            "and then migrate --expect-sha256 before writing"
        )


__all__ = [
    "ParserPolicy",
    "build_parser",
    "read_packet",
    "require_current_graph_schema",
]
