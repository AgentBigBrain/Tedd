#!/usr/bin/env python3
"""Argparse registration and bounded JSON dispatch for the v4 graph store.

This module deliberately owns no canonical storage logic.  It validates the
command boundary and delegates to :mod:`knowledge_graph_v4`, an upgrade
callback, or a capture adapter.  Callers decide whether a compatibility
command (for example ``recall``) targets v3 or v4 before dispatching here.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import re
import stat
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Protocol, Sequence, SupportsInt, TypeVar, cast

from knowledge_graph_file_io import DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS, PublicationOperations
from knowledge_graph_file_io import fsync_directory as _fsync_directory
from knowledge_graph_file_io import publish_staged_file as _publish_staged_file
from platform_support import (
    is_link_or_reparse_point,
    mode_has_any,
    owner_is_current_user,
    set_private_fd_mode,
    set_private_path_mode,
)
from tedd_knowledge_graph.handoff_receipts import decode_receipt
from tedd_knowledge_graph.v4_application_service import (
    GraphReadDependencies,
    GraphReadLimits,
    GraphReadService,
    GraphReadStore,
    ReadAuthorityMode,
)
from tedd_knowledge_graph.v4_backend_error_mapping import classify_graph_backend_error
from tedd_knowledge_graph.v4_cli_capture import (
    CAPTURE_COMMANDS,
    handle_capture_command,
)
from tedd_knowledge_graph.v4_cli_contracts import (
    MAX_CAPTURE_PLAN_BYTES,
    CaptureHandler,
    CaptureInputSnapshot,
    CaptureRequest,
    CommandServices,
    InitializeHandler,
    InitializeRequest,
    JsonObject,
    JsonValue,
    ProducerEnvelope,
    ReviewSanitizer,
    StorageModule,
    UpgradeHandler,
    UpgradeRequest,
    V4CLIError,
)
from tedd_knowledge_graph.v4_cli_coordination import (
    COORDINATION_COMMANDS,
    SERVICE_COORDINATION_COMMANDS,
    TRUSTED_LOCAL_SUBSCRIPTION_COMMANDS,
    handle_coordination_command,
)
from tedd_knowledge_graph.v4_cli_coordination import (
    READONLY_COMMANDS as READONLY_COORDINATION_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_maintenance import (
    MAINTENANCE_COMMANDS,
    OPEN_MAINTENANCE_COMMANDS,
    handle_maintenance_command,
    handle_preopen_maintenance_command,
)
from tedd_knowledge_graph.v4_cli_maintenance import (
    READONLY_COMMANDS as READONLY_MAINTENANCE_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_read import (
    READ_COMMANDS,
    SERVICE_READ_COMMANDS,
    handle_read_command,
)
from tedd_knowledge_graph.v4_cli_read import (
    READONLY_COMMANDS as READONLY_READ_COMMANDS,
)
from tedd_knowledge_graph.v4_contracts import DISCOVERY_KINDS, validate_discovery_kind

MAX_IDENTIFIER_LENGTH = 200
MAX_CONTEXT_CHARS = 100_000
MAX_QUERY_CHARS = 2_000
MAX_PAGE_SIZE = 500
# Keep the CLI boundary aligned with the legacy semantic adapter's effective
# limit until capture normalization no longer depends on knowledge_graph.py.
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024

SAFE_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
SAFE_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

MODES = ("review_safe", "operator_full")
SERVICE_COMMANDS = SERVICE_READ_COMMANDS | SERVICE_COORDINATION_COMMANDS

NEW_V4_COMMANDS = frozenset(
    {
        "init-v4",
        "upgrade-v4",
        "history",
        "changes",
        "consumer-status",
        "consumer-register",
        "consumer-rebind",
        "subscribe",
        "inbox",
        "inbox-ack",
        "context",
        "loops-review",
        "backup",
        "export",
        "checkpoint",
        "status",
        "source-evidence",
        "handoff",
    }
)
COMPATIBLE_COMMANDS = frozenset(
    {"doctor", "validate", "get", "recall", "capture", "reindex", "index-status"}
)

_COMMAND_FAMILIES = (
    READ_COMMANDS,
    CAPTURE_COMMANDS,
    COORDINATION_COMMANDS,
    MAINTENANCE_COMMANDS,
)
_FAMILY_COMMANDS = frozenset().union(*_COMMAND_FAMILIES)
if _FAMILY_COMMANDS != NEW_V4_COMMANDS | COMPATIBLE_COMMANDS:
    raise RuntimeError("v4 CLI command families do not cover the public command surface")
if sum(len(family) for family in _COMMAND_FAMILIES) != len(_FAMILY_COMMANDS):
    raise RuntimeError("v4 CLI command families overlap")


class BackendMethod(Protocol):
    """Dynamically selected storage method after the callable check."""

    def __call__(self, *args: object, **kwargs: object) -> object: ...


class ArgumentRegistrar(Protocol):
    """Narrow forwarding surface used by the shared argparse helper."""

    def add_argument(self, *flags: str, **kwargs: object) -> argparse.Action: ...


def _storage_module() -> StorageModule:
    try:
        module = importlib.import_module("knowledge_graph_v4")
    except ModuleNotFoundError as error:
        if error.name != "knowledge_graph_v4":
            raise
        package = __package__
        if package:
            module = importlib.import_module(f"{package}.knowledge_graph_v4")
        else:
            raise V4CLIError("knowledge_graph_v4 storage module is unavailable") from error
    _validate_storage_module(module)
    return cast(StorageModule, module)


def _validate_storage_module(module: ModuleType) -> None:
    for name in ("inspect_store_path", "open_store"):
        if not callable(getattr(module, name, None)):
            raise V4CLIError(f"knowledge_graph_v4 storage module does not implement {name}()")


def _bounded_text(value: object, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise V4CLIError(f"{label} is required")
    if len(text) > maximum:
        raise V4CLIError(f"{label} exceeds {maximum} characters")
    return text


def _opaque_id(value: object, label: str, *, record: bool = False) -> str:
    text = _bounded_text(value, label, MAX_IDENTIFIER_LENGTH)
    pattern = SAFE_RECORD_ID if record else SAFE_OPAQUE_ID
    if not pattern.fullmatch(text):
        raise V4CLIError(f"{label} must be an opaque ASCII identifier")
    return text


def _optional_opaque_id(value: object, label: str, *, record: bool = False) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _opaque_id(value, label, record=record)


def _nonnegative_int(value: object, label: str) -> int:
    try:
        if isinstance(value, (str, bytes, bytearray)):
            number = int(value)
        elif isinstance(value, SupportsInt):
            number = int(value)
        else:
            raise TypeError
    except (TypeError, ValueError) as error:
        raise V4CLIError(f"{label} must be an integer") from error
    if number < 0:
        raise V4CLIError(f"{label} must be nonnegative")
    return number


def _positive_int(value: object, label: str, maximum: int | None = None) -> int:
    number = _nonnegative_int(value, label)
    if number < 1:
        raise V4CLIError(f"{label} must be positive")
    if maximum is not None and number > maximum:
        raise V4CLIError(f"{label} must not exceed {maximum}")
    return number


def _unique_ids(
    values: Sequence[object] | None, label: str, *, record: bool = True
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        item = _opaque_id(value, label, record=record)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _expected_revisions(values: Sequence[str] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values or ():
        text = _bounded_text(value, "expected revision", 256)
        if "=" not in text:
            raise V4CLIError("--expect-revision must use RECORD_ID=REVISION")
        record_id, revision_text = text.rsplit("=", 1)
        record_id = _opaque_id(record_id, "expected revision record ID", record=True)
        revision = _nonnegative_int(revision_text, "expected revision")
        if record_id in result and result[record_id] != revision:
            raise V4CLIError(f"conflicting expected revisions for {record_id}")
        result[record_id] = revision
    return result


def _producer_envelope(args: argparse.Namespace, *, require_discovery: bool) -> ProducerEnvelope:
    agent_id = _opaque_id(getattr(args, "producer_agent_id", None), "producer agent ID")
    task_id = _opaque_id(getattr(args, "task_id", None), "task ID")
    run_id = _opaque_id(getattr(args, "producer_run_id", None), "producer run ID")
    parent_id = _optional_opaque_id(getattr(args, "parent_task_id", None), "parent task ID")
    discovery_value = getattr(args, "discovery_kind", "")
    discovery = str(discovery_value or "").strip()
    if require_discovery:
        try:
            discovery = validate_discovery_kind(discovery)
        except ValueError as error:
            raise V4CLIError("a controlled --discovery-kind is required") from error
    if not discovery:
        discovery = "milestone"
    based_on = _nonnegative_int(getattr(args, "based_on_watermark", None), "based-on watermark")
    return ProducerEnvelope(agent_id, task_id, parent_id, run_id, discovery, based_on)


def _snapshot_capture_input(value: str) -> CaptureInputSnapshot:
    return _snapshot_private_input(value, maximum=MAX_CAPTURE_BYTES)


def _snapshot_private_input(value: str, *, maximum: int) -> CaptureInputSnapshot:
    """Read once, with descriptor-bound ownership, privacy, and size checks."""
    text = _bounded_text(value, "capture input", 4096)
    if text == "-":
        return CaptureInputSnapshot(text, None)
    lexical_path = Path(text).expanduser()
    if is_link_or_reparse_point(lexical_path):
        raise V4CLIError(f"capture input must be a regular non-symlink file: {lexical_path}")
    path = lexical_path.absolute()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(lexical_path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise V4CLIError(f"capture input must be a regular non-symlink file: {path}")
        if before.st_size > maximum:
            raise V4CLIError(f"capture input exceeds {maximum} bytes")
        if not owner_is_current_user(descriptor):
            raise V4CLIError("capture input must be owned by the current user")
        if mode_has_any(descriptor, stat.S_IRWXG | stat.S_IRWXO):
            raise V4CLIError("capture input must not be accessible by group or other users")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise V4CLIError(f"capture input exceeds {maximum} bytes")
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise V4CLIError("capture input changed while it was being read")
        if is_link_or_reparse_point(path):
            raise V4CLIError(f"capture input must be a regular non-symlink file: {path}")
        named = path.stat()
        if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise V4CLIError("capture input pathname changed while it was being read")
        return CaptureInputSnapshot(str(path), content)
    except V4CLIError:
        raise
    except OSError as error:
        raise V4CLIError(
            f"capture input must be a readable regular non-symlink file: {path}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_redirecting_path_components(path: Path, label: str) -> Path:
    """Reject redirects, then return an absolute lexical normalization."""

    if path.drive and not path.root:
        raise V4CLIError(f"{label} must not use a drive-relative path")
    candidate = path if path.is_absolute() else Path.cwd() / path
    if not candidate.is_absolute():
        raise V4CLIError(f"{label} must not use a drive-relative path")
    current = Path(candidate.anchor)
    if is_link_or_reparse_point(current):
        raise V4CLIError(f"{label} must not contain symlinks or reparse points")
    for component in candidate.parts[1:]:
        current = current / component
        if is_link_or_reparse_point(current):
            raise V4CLIError(f"{label} must not contain symlinks or reparse points")
    return Path(os.path.abspath(os.fspath(path)))


def _safe_output_path(
    value: object,
    label: str,
    *,
    force: bool = False,
    replacement_supported: bool = True,
) -> Path:
    if force and not replacement_supported:
        raise V4CLIError(f"{label} does not support replacement")
    lexical_path = Path(_bounded_text(value, label, 4096)).expanduser()
    path = _reject_redirecting_path_components(lexical_path, label)
    if path.exists():
        if is_link_or_reparse_point(path) or not path.is_file():
            raise V4CLIError(f"{label} must be a regular non-symlink file")
        if not force:
            if replacement_supported:
                raise V4CLIError(f"{label} already exists; pass --force to replace it")
            raise V4CLIError(f"{label} already exists; choose a new destination")
    try:
        path.parent.mkdir(mode=0o700, parents=True)
    except FileExistsError as error:
        if not path.parent.is_dir():
            raise V4CLIError(f"{label} parent must be a directory") from error
    else:
        set_private_path_mode(path.parent, 0o700)
    _reject_redirecting_path_components(lexical_path, label)
    if not owner_is_current_user(path.parent):
        raise V4CLIError(f"{label} parent must be owned by the current user")
    if mode_has_any(path.parent, stat.S_IWOTH):
        raise V4CLIError(f"{label} parent must not be world-writable")
    return path


def _require_private_plan_parent(path: Path) -> None:
    if not owner_is_current_user(path.parent) or mode_has_any(path.parent, 0o077):
        raise V4CLIError("capture plan requires a private current-user directory")


def _safe_plan_output(value: str) -> Path:
    path = _safe_output_path(value, "capture plan output", replacement_supported=False)
    _require_private_plan_parent(path)
    return path


def _plan_parent_guard(path: Path) -> Callable[[], None]:
    """Detect redirects or replacement of the trusted private parent during I/O.

    File bytes still use one verified descriptor. This does not establish an
    isolation boundary against a malicious process with the same OS identity.
    """
    _reject_redirecting_path_components(path, "capture plan")
    _require_private_plan_parent(path)
    parent = path.parent.stat()

    def check() -> None:
        _reject_redirecting_path_components(path, "capture plan")
        _require_private_plan_parent(path)
        current = path.parent.stat()
        if (current.st_dev, current.st_ino) != (parent.st_dev, parent.st_ino):
            raise V4CLIError("capture plan parent changed during I/O")

    return check


def _snapshot_plan_input(value: str) -> CaptureInputSnapshot:
    if value.strip() == "-":
        raise V4CLIError("capture plan must be a private file, not stdin")
    path = _reject_redirecting_path_components(Path(value).expanduser(), "capture plan")
    guard = _plan_parent_guard(path)
    snapshot = _snapshot_private_input(str(path), maximum=MAX_CAPTURE_PLAN_BYTES)
    guard()
    return snapshot


def _write_plan_file(value: str, payload: Mapping[str, object]) -> None:
    expected = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(expected) > MAX_CAPTURE_PLAN_BYTES:
        raise V4CLIError("capture plan exceeds the private-file limit; narrow the capture")
    path = _safe_plan_output(value)
    guard = _plan_parent_guard(path)
    _write_json_atomic(path, payload, overwrite=False, path_guard=guard)
    guard()
    snapshot = _snapshot_plan_input(str(path))
    if snapshot.content != expected:
        raise V4CLIError("capture plan changed after publication")
    guard()


def _snapshot_handoff_file(path: Path) -> bytes | None:
    """Read bounded owner-private control data through the existing descriptor boundary."""
    _reject_redirecting_path_components(path, "handoff receipt")
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    guard = _plan_parent_guard(path)
    snapshot = _snapshot_private_input(str(path), maximum=MAX_CAPTURE_PLAN_BYTES)
    guard()
    if snapshot.content is None:
        raise V4CLIError("handoff receipt requires a private file")
    return snapshot.content


def _read_handoff_file(path: Path) -> object:
    content = _snapshot_handoff_file(path)
    if content is None:
        return None
    try:
        return decode_receipt(content)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise V4CLIError("handoff receipt is not valid bounded JSON") from error


def _write_handoff_file(path: Path, payload: Mapping[str, object]) -> None:
    path = _safe_output_path(str(path), "handoff receipt", force=True)
    guard = _plan_parent_guard(path)
    # Refuse to claim a preexisting nonprivate file while allowing atomic replacement
    # of our own prompt receipt. Same-user tampering is detected by journal verification.
    _snapshot_handoff_file(path)
    if len(json.dumps(payload).encode("utf-8")) > MAX_CAPTURE_PLAN_BYTES:
        raise V4CLIError("handoff receipt exceeds the private-file limit")
    _write_json_atomic(path, payload, path_guard=guard)
    guard()


def _json_safe(value: object) -> JsonValue:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_safe(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in cast(Sequence[object], value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise V4CLIError(f"backend returned a non-JSON value: {type(value).__name__}")


def _result(command: str, value: object) -> JsonObject:
    safe = _json_safe(value)
    if isinstance(safe, dict):
        result: JsonObject = {"command": command}
        result.update(safe)
    else:
        result = {"command": command, "result": safe}
    result.setdefault("status", "ok")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JSON_RESPONSE_BYTES:
        raise V4CLIError(
            "command response exceeds the bounded JSON limit; narrow the query or export to a file"
        )
    return result


def _apply_review_sanitizer(value: object, sanitizer: ReviewSanitizer | None) -> object:
    """Cross the review-safe disclosure boundary exactly through the caller policy.

    Storage methods intentionally receive no presentation mode.  They return
    canonical operator-shaped values, and the integrating v3 policy sanitizes
    the *complete* JSON-shaped result here before it can be serialized or
    written to an export file.  A record-only fallback would be unsafe because
    receipts, feed entries, and context wrappers can also carry private data.
    """

    if sanitizer is None:
        raise V4CLIError("review_safe output requires a review_sanitizer(value) callback")
    try:
        return _json_safe(sanitizer(_json_safe(value)))
    except V4CLIError:
        raise
    except Exception as error:
        raise V4CLIError("review-safe output sanitization failed") from error


def _method(store: object, name: str) -> BackendMethod:
    method = getattr(store, name, None)
    if not callable(method):
        raise V4CLIError(f"v4 storage backend does not implement {name}()")
    return cast(BackendMethod, method)


def _invoke(store: object, name: str, /, **kwargs: object) -> object:
    """Invoke one backend method without silently dropping requested semantics."""
    method = _method(store, name)
    signature = inspect.signature(method)
    accepts_extra = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if not accepts_extra:
        unsupported = [
            key
            for key, value in kwargs.items()
            if value is not None and key not in signature.parameters
        ]
        if unsupported:
            raise V4CLIError(
                f"{name}() cannot honor requested option(s): {', '.join(sorted(unsupported))}"
            )
        kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return method(**kwargs)


def _state(store: object) -> object:
    value = getattr(store, "state", None)
    if callable(value):
        return cast(object, value())
    return cast(object, value)


def _fsync_parent_directory(path: Path) -> None:
    _fsync_directory(
        path,
        os_module=os,
        unsupported_errnos=DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
    )


def _write_json_atomic(
    path: Path,
    payload: object,
    *,
    overwrite: bool = True,
    path_guard: Callable[[], None] | None = None,
) -> int:
    content = (json.dumps(_json_safe(payload), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            set_private_fd_mode(handle.fileno(), 0o600)
            if path_guard is not None:
                path_guard()
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path_guard is not None:
            path_guard()
        _publish_staged_file(
            temp_path,
            path,
            overwrite=overwrite,
            os_module=cast(PublicationOperations, os),
        )
        _fsync_parent_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return len(content)


def _maintenance_fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reindex_store(store: object) -> dict[str, object]:
    connection = getattr(store, "connection", None)
    if connection is None:
        raise V4CLIError("v4 storage backend does not expose its maintenance connection")
    with connection:
        connection.execute("DELETE FROM records_fts")
        connection.execute(
            "INSERT INTO records_fts(rowid,record_id,collection,search_text) "
            "SELECT rowid,record_id,collection,search_text FROM records"
        )
    validation_value = _invoke(store, "validate", full=False)
    if not isinstance(validation_value, Mapping):
        raise V4CLIError("v4 storage backend returned an invalid validation result")
    validation = cast(Mapping[str, object], validation_value)
    if not validation.get("valid"):
        raise V4CLIError("FTS rebuild completed but canonical validation failed")
    counts_value = validation.get("counts")
    counts = cast(Mapping[str, object], counts_value) if isinstance(counts_value, Mapping) else {}
    return {
        "status": "rebuilt",
        "record_count": counts.get("records"),
        "validation": dict(validation),
    }


def _has_dest(parser: argparse.ArgumentParser, dest: str) -> bool:
    return any(action.dest == dest for action in parser._actions)


def _has_option(parser: argparse.ArgumentParser, option: str) -> bool:
    return any(option in action.option_strings for action in parser._actions)


def _add(parser: argparse.ArgumentParser, *flags: str, **kwargs: object) -> None:
    raw_dest = kwargs.get("dest")
    if raw_dest is None:
        long_flag = next((flag for flag in flags if flag.startswith("--")), None)
        dest = long_flag[2:].replace("-", "_") if long_flag else flags[0]
    elif isinstance(raw_dest, str):
        dest = raw_dest
    else:
        raise TypeError("argparse destination must be a string")
    if not _has_dest(parser, dest):
        cast(ArgumentRegistrar, parser).add_argument(*flags, **kwargs)


def _command_parser(
    subparsers: argparse._SubParsersAction[ParserT],
    name: str,
    help_text: str,
) -> ParserT:
    existing = subparsers.choices.get(name)
    return existing if existing is not None else subparsers.add_parser(name, help=help_text)


def _add_mode(parser: argparse.ArgumentParser) -> None:
    _add(parser, "--mode", choices=MODES, default="review_safe")


def _add_provenance(parser: argparse.ArgumentParser, *, discovery: bool = False) -> None:
    _add(parser, "--producer-agent-id")
    _add(parser, "--task-id")
    _add(parser, "--parent-task-id")
    _add(parser, "--producer-run-id")
    _add(parser, "--based-on-watermark", type=int)
    if discovery:
        _add(parser, "--discovery-kind", choices=DISCOVERY_KINDS)


def register_v4_subcommands(
    subparsers: argparse._SubParsersAction[ParserT],
) -> None:
    """Idempotently register new v4 commands and augment v3-compatible ones."""

    parser = _command_parser(
        subparsers,
        "init-v4",
        "Explicitly initialize a private empty v4 store for a fresh installation",
    )
    _add(
        parser,
        "--store",
        help=(
            "Fresh-install target to bind in configuration; runtime v4 commands "
            "do not accept store overrides"
        ),
    )

    parser = _command_parser(
        subparsers, "upgrade-v4", "Plan or apply a digest-gated v3 to v4 import"
    )
    if not _has_dest(parser, "dry_run") and not _has_dest(parser, "apply"):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--dry-run", action="store_true")
        group.add_argument("--apply", action="store_true")
    _add(parser, "--expect-source-sha256")
    _add(parser, "--expect-plan-fingerprint")
    _add(parser, "--backup-output")
    _add_provenance(parser)

    parser = _command_parser(subparsers, "doctor", "Inspect the configured graph")
    parser = _command_parser(subparsers, "validate", "Validate canonical v4 state")
    _add(parser, "--full", action="store_true")

    parser = _command_parser(subparsers, "get", "Retrieve an exact record")
    _add(parser, "--id", required=True)
    _add(parser, "--revision", type=int)
    _add_mode(parser)

    parser = _command_parser(
        subparsers, "history", "Retrieve bounded immutable revisions for one record"
    )
    _add(parser, "--id", required=True)
    _add(parser, "--before-revision", type=int)
    _add(parser, "--limit", type=int, default=50)
    _add_mode(parser)

    parser = _command_parser(subparsers, "recall", "Search bounded current graph context")
    if not _has_dest(parser, "query"):
        parser.add_argument("query", nargs="?", default="")
    _add(parser, "--subject-id", action="append", default=[])
    _add(parser, "--section", action="append", default=[])
    _add(parser, "--consumer-id")
    _add(parser, "--limit", type=int, default=10)
    _add(parser, "--max-chars", type=int, default=12000)
    _add(parser, "--at-watermark", type=int)
    _add_mode(parser)

    parser = _command_parser(subparsers, "changes", "Read a snapshot-pinned mutation feed")
    _add(parser, "--since-watermark", type=int, required=True)
    _add(parser, "--through-watermark", type=int)
    _add(parser, "--cursor")
    _add(parser, "--limit", type=int, default=100)
    _add(parser, "--subject-id", action="append", default=[])
    _add(parser, "--discovery-kind", action="append", choices=DISCOVERY_KINDS, default=[])
    _add_mode(parser)

    parser = _command_parser(subparsers, "capture", "Capture a transactional v4 discovery")
    _add(
        parser,
        "--handoff",
        action="store_true",
        help="Save a journal-verified prompt receipt after apply",
    )
    _add(parser, "--handoff-prompt-id", help="Current native host prompt identifier")
    _add(parser, "--input", required=True)
    _add(parser, "--dry-run", action="store_true")
    _add(parser, "--consumer-id")
    _add(parser, "--source-subscription-id")
    _add(parser, "--source-subject-id", action="append", default=[])
    if not _has_dest(parser, "expect_plan_token"):
        token_flags = ["--expect-plan-token"]
        if not _has_option(parser, "--plan-token"):
            token_flags.append("--plan-token")
        parser.add_argument(*token_flags, dest="expect_plan_token")
    _add(
        parser,
        "--plan-output",
        help="v4 dry-run only: save a private plan file without disclosing its token",
    )
    _add(
        parser,
        "--expect-plan-file",
        help="v4 apply only: use a saved private plan with --expect-fingerprint",
    )
    _add(parser, "--expect-revision", action="append", default=[])
    _add(parser, "--expect-absent", action="append", default=[])
    _add(parser, "--expect-fingerprint")
    _add(parser, "--expect-watermark", type=int)
    _add(parser, "--authority-ceiling", default="assistant_inference")
    _add(parser, "--allow-replace", action="store_true")
    _add(parser, "--allow-redirect", action="store_true")
    _add_mode(parser)
    _add_provenance(parser, discovery=True)

    parser = _command_parser(subparsers, "handoff", "Check or reconstruct a private prompt receipt")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--nothing", action="store_true")
    _add(parser, "--reason", help="One-line agent assertion explaining why nothing was saved")
    _add(parser, "--reconstruct", action="store_true")
    _add(parser, "--session", "--task-id", dest="task_id", required=True)
    _add(parser, "--prompt", required=True)
    _add(parser, "--consumer-id", required=True)
    _add(parser, "--producer-agent-id", required=True)
    _add_mode(parser)

    parser = _command_parser(
        subparsers, "source-evidence", "Check one memory against its archived evidence"
    )
    _add(parser, "--id", required=True)
    _add(parser, "--revision", type=int)
    _add(parser, "--consumer-id", required=True)
    _add(parser, "--subscription-id", required=True)
    _add(parser, "--subject-id", required=True, action="append")
    for flag in ("--producer-agent-id", "--task-id", "--producer-run-id"):
        _add(parser, flag, required=True)
    _add(parser, "--max-chars", type=int, default=12_000)
    _add(parser, "--max-versions", type=int, default=5)

    parser = _command_parser(
        subparsers,
        "consumer-status",
        "Inspect one persisted task consumer without changing ownership",
    )
    _add(parser, "--consumer-id", required=True)

    parser = _command_parser(subparsers, "consumer-register", "Register one task-local consumer")
    _add(parser, "--consumer-id", required=True)
    _add_provenance(parser)

    parser = _command_parser(
        subparsers,
        "consumer-rebind",
        "Rebind an existing task consumer to a new logical agent",
    )
    _add(parser, "--consumer-id", required=True)
    _add(parser, "--existing-task-id", required=True)
    _add(parser, "--expected-old-agent-id", required=True)
    _add_provenance(parser)

    parser = _command_parser(subparsers, "subscribe", "Set exact versioned consumer subscriptions")
    _add(parser, "--consumer-id", required=True)
    _add(parser, "--subscription-id")
    _add(parser, "--subject-id", action="append", default=[])
    _add(parser, "--record-id", action="append", default=[])
    _add(parser, "--collection", action="append", default=[])
    _add(parser, "--all", action="store_true")
    _add(parser, "--discovery-kind", action="append", choices=DISCOVERY_KINDS, default=[])
    _add(parser, "--start-watermark", type=int)
    _add(parser, "--backfill-from-watermark", type=int)

    parser = _command_parser(subparsers, "inbox", "Pull bounded relevant task changes")
    _add(parser, "--subscription-id", required=True)
    _add(parser, "--cursor")
    _add(parser, "--through-watermark", type=int)
    _add(parser, "--limit", type=int, default=100)
    _add_mode(parser)

    parser = _command_parser(
        subparsers, "inbox-ack", "Acknowledge a complete authenticated inbox delivery"
    )
    _add(parser, "--subscription-id", required=True)
    _add(parser, "--through-watermark", type=int, required=True)
    _add(parser, "--ack-token", required=True)

    parser = _command_parser(subparsers, "context", "Build a relevance-bounded task context pack")
    _add(parser, "--subscription-id", required=True)
    _add(parser, "--max-items", type=int, default=100)
    _add(parser, "--max-chars", type=int, default=12000)
    _add(parser, "--plan", action="store_true")
    _add(parser, "--fingerprint")
    _add_mode(parser)

    parser = _command_parser(
        subparsers, "loops-review", "Review overdue loops in an existing task scope"
    )
    _add(parser, "--consumer-id", required=True)
    _add(parser, "--subscription-id", required=True)
    _add(parser, "--overdue-days", type=int, default=0)
    _add(parser, "--age-days", type=int, default=30)
    _add(parser, "--limit", type=int, default=20)
    _add_mode(parser)

    parser = _command_parser(subparsers, "backup", "Create a transactional SQLite backup")
    _add(parser, "--output", required=True)

    parser = _command_parser(
        subparsers, "export", "Create a deterministic current-state logical graph export"
    )
    _add(parser, "--output", required=True)
    _add(parser, "--force", action="store_true")
    _add_mode(parser)

    parser = _command_parser(
        subparsers, "checkpoint", "Checkpoint WAL or create an audit checkpoint"
    )
    _add(parser, "--audit", action="store_true")
    _add(parser, "--dry-run", action="store_true")
    _add(parser, "--apply", action="store_true")
    _add(parser, "--expect-watermark", type=int)
    _add(parser, "--expect-plan-fingerprint")
    _add_provenance(parser)

    _command_parser(subparsers, "status", "Inspect compact v4 store state")
    parser = _command_parser(subparsers, "reindex", "Rebuild canonical FTS read structures")
    _add(parser, "--force", action="store_true")
    _command_parser(subparsers, "index-status", "Inspect canonical FTS state")


def should_handle_v4(args: argparse.Namespace, *, v4_enabled: bool) -> bool:
    command = getattr(args, "command", None)
    return command in NEW_V4_COMMANDS or (v4_enabled and command in COMPATIBLE_COMMANDS)


def _reject_custom_store(args: argparse.Namespace) -> None:
    if getattr(args, "store", None):
        raise V4CLIError(
            "production v4 commands use the configured shared store; --store is not allowed"
        )


def _open(
    storage: StorageModule,
    db_path: Path,
    *,
    readonly: bool,
    plan_hmac_key: bytes | str | None,
) -> AbstractContextManager[object]:
    return storage.open_store(db_path, readonly=readonly, plan_hmac_key=plan_hmac_key)


def _command_services(storage: StorageModule) -> CommandServices:
    """Bind helpers at call time so existing monkeypatch seams remain effective."""

    return CommandServices(
        inspect_store=storage.inspect_store_path,
        invoke=_invoke,
        state=_state,
        bounded_text=_bounded_text,
        opaque_id=_opaque_id,
        optional_opaque_id=_optional_opaque_id,
        nonnegative_int=_nonnegative_int,
        positive_int=_positive_int,
        unique_ids=_unique_ids,
        expected_revisions=_expected_revisions,
        producer_envelope=_producer_envelope,
        snapshot_capture_input=_snapshot_capture_input,
        snapshot_plan_input=_snapshot_plan_input,
        write_plan_file=_write_plan_file,
        read_handoff_file=_read_handoff_file,
        write_handoff_file=_write_handoff_file,
        safe_output_path=_safe_output_path,
        write_json_atomic=_write_json_atomic,
        apply_review_sanitizer=_apply_review_sanitizer,
        maintenance_fingerprint=_maintenance_fingerprint,
        reindex_store=_reindex_store,
    )


def _make_graph_read_service(
    storage: StorageModule,
    canonical_path: Path,
    plan_hmac_key: bytes | str | None,
    command: str,
    *,
    review_sanitizer: ReviewSanitizer | None = None,
) -> GraphReadService:
    """Compose one read-only application service from already-secured CLI state."""

    def open_read_store(
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> AbstractContextManager[GraphReadStore]:
        return cast(
            AbstractContextManager[GraphReadStore],
            _open(storage, path, readonly=readonly, plan_hmac_key=plan_hmac_key),
        )

    authority_mode = (
        ReadAuthorityMode.TRUSTED_LOCAL_CLI
        if command in TRUSTED_LOCAL_SUBSCRIPTION_COMMANDS
        else ReadAuthorityMode.EXACT_SESSION
    )
    return GraphReadService(
        GraphReadDependencies(
            load_config=lambda: canonical_path,
            resolve_store_path=lambda configured_path: cast(Path, configured_path),
            validate_store_path=lambda _configured_path, path: _validate_service_path(
                path, canonical_path
            ),
            detect_store=storage.detect_store,
            inspect_store=lambda path: cast(Mapping[str, object], storage.inspect_store_path(path)),
            load_plan_key=lambda _configured_path, _path: plan_hmac_key,
            open_store=open_read_store,
            sanitize=(
                (lambda value: _apply_review_sanitizer(value, review_sanitizer))
                if review_sanitizer is not None
                else (lambda value: value)
            ),
            limits=GraphReadLimits(
                max_query_chars=MAX_QUERY_CHARS,
                max_page_size=MAX_PAGE_SIZE,
                max_context_chars=MAX_CONTEXT_CHARS,
            ),
            classify_error=lambda error: classify_graph_backend_error(error, storage),
            authority_mode=authority_mode,
        )
    )


def _validate_service_path(path: Path, canonical_path: Path) -> None:
    if path != canonical_path:
        raise V4CLIError("graph service path does not match the configured store")


def handle_v4_command(
    args: argparse.Namespace,
    *,
    db_path: str | Path,
    plan_hmac_key: bytes | str | None = None,
    v3_store_path: str | Path | None = None,
    capture_handler: CaptureHandler | None = None,
    initialize_handler: InitializeHandler | None = None,
    upgrade_handler: UpgradeHandler | None = None,
    review_sanitizer: ReviewSanitizer | None = None,
) -> JsonObject | None:
    """Validate and dispatch one v4 command, returning a bounded JSON object.

    Return ``None`` for commands outside the v4 command set.  The caller must
    invoke :func:`should_handle_v4` first for names shared with v3.
    """

    command_value = getattr(args, "command", None)
    if not isinstance(command_value, str):
        return None
    command = command_value
    if command not in NEW_V4_COMMANDS | COMPATIBLE_COMMANDS:
        return None

    if os.environ.get("TEDD_NO_LOCAL_WRITES") == "1" and (
        command == "handoff" or (command == "capture" and getattr(args, "handoff", False))
    ):
        # Do not even open SQLite: an ordinary read may coordinate through -shm.
        raise V4CLIError("handoff is disabled in strict no-local-writes mode")

    storage = _storage_module()
    services = _command_services(storage)
    canonical_path = Path(db_path).expanduser().resolve()

    if command == "init-v4":
        value = handle_preopen_maintenance_command(
            command,
            args,
            canonical_path,
            services,
            v3_store_path=v3_store_path,
            initialize_handler=initialize_handler,
            upgrade_handler=upgrade_handler,
        )
        return _result(command, value)

    _reject_custom_store(args)

    if command == "upgrade-v4":
        value = handle_preopen_maintenance_command(
            command,
            args,
            canonical_path,
            services,
            v3_store_path=v3_store_path,
            initialize_handler=initialize_handler,
            upgrade_handler=upgrade_handler,
        )
        return _result(command, value)

    if command in SERVICE_COMMANDS:
        output_mode = getattr(args, "mode", None)
        if output_mode is not None and output_mode not in MODES:
            raise V4CLIError(f"unsupported output mode: {output_mode}")
        context_review = command == "context" and output_mode == "review_safe"
        if context_review and review_sanitizer is None:
            raise V4CLIError("review_safe output requires a review_sanitizer(value) callback")
        read_service = _make_graph_read_service(
            storage,
            canonical_path,
            plan_hmac_key,
            command,
            review_sanitizer=review_sanitizer if context_review else None,
        )
        if command in READ_COMMANDS:
            value = handle_read_command(
                command,
                args,
                object(),
                canonical_path,
                services,
                read_service=read_service,
                max_query_chars=MAX_QUERY_CHARS,
                max_page_size=MAX_PAGE_SIZE,
                max_context_chars=MAX_CONTEXT_CHARS,
            )
        else:
            value = handle_coordination_command(
                command,
                args,
                object(),
                services,
                read_service=read_service,
                max_page_size=MAX_PAGE_SIZE,
                max_context_chars=MAX_CONTEXT_CHARS,
            )
        if output_mode == "review_safe" and not context_review:
            value = _apply_review_sanitizer(value, review_sanitizer)
        return _result(command, value)

    readonly_commands = (
        READONLY_READ_COMMANDS | READONLY_COORDINATION_COMMANDS | READONLY_MAINTENANCE_COMMANDS
    ) | {"handoff"}
    with _open(
        storage,
        canonical_path,
        readonly=command in readonly_commands,
        plan_hmac_key=plan_hmac_key,
    ) as store:
        if command == "source-evidence":
            from tedd_sources import evidence_command

            value = evidence_command(args, store, canonical_path)
        elif command in READ_COMMANDS:
            value = handle_read_command(
                command,
                args,
                store,
                canonical_path,
                services,
                max_query_chars=MAX_QUERY_CHARS,
                max_page_size=MAX_PAGE_SIZE,
                max_context_chars=MAX_CONTEXT_CHARS,
            )
        elif command in CAPTURE_COMMANDS:
            value = handle_capture_command(command, args, store, services, capture_handler)
        elif command in COORDINATION_COMMANDS:
            value = handle_coordination_command(
                command,
                args,
                store,
                services,
                max_page_size=MAX_PAGE_SIZE,
                max_context_chars=MAX_CONTEXT_CHARS,
            )
        elif command in OPEN_MAINTENANCE_COMMANDS:
            value = handle_maintenance_command(command, args, store, services, review_sanitizer)
        else:
            raise V4CLIError(f"unsupported v4 command: {command}")

    output_mode = getattr(args, "mode", None)
    if output_mode is not None and output_mode not in MODES:
        raise V4CLIError(f"unsupported output mode: {output_mode}")
    if output_mode == "review_safe":
        value = _apply_review_sanitizer(value, review_sanitizer)
    return _result(command, value)


def error_json(command: str | None, error: BaseException) -> JsonObject:
    """Return a stable JSON error shape without exposing a traceback."""
    details: JsonObject = {
        "status": "error",
        "command": command,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    for field in ("record_id", "expected_revision", "current_revision", "candidates"):
        if hasattr(error, field):
            details[field] = _json_safe(getattr(error, field))
    return details


__all__ = [
    "CaptureHandler",
    "CaptureRequest",
    "DISCOVERY_KINDS",
    "InitializeHandler",
    "InitializeRequest",
    "ProducerEnvelope",
    "ReviewSanitizer",
    "UpgradeHandler",
    "UpgradeRequest",
    "V4CLIError",
    "error_json",
    "handle_v4_command",
    "register_v4_subcommands",
    "should_handle_v4",
]
