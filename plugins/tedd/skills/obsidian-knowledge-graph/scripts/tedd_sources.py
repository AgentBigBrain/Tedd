#!/usr/bin/env python3
"""Explicit, scope-checked source capture and maintenance; never a retrieval hook."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

import knowledge_graph as graph
import knowledge_graph_v4 as v4
from platform_support import is_link_or_reparse_point
from tedd_knowledge_graph import source_files
from tedd_knowledge_graph.source_archive import DEFAULT_QUOTA, Archive
from tedd_knowledge_graph.source_authority import (
    AccessDecision,
    ProviderCheck,
    access_binding,
    authorized_scope,
    decode_access_policy,
)
from tedd_knowledge_graph.source_av1_host import Av1Validator
from tedd_knowledge_graph.source_capture import (
    OFFICE_KINDS,
    PdfReading,
    bind_document_reading,
    capture_chat,
    capture_document,
    capture_pdf,
    run_if_collecting,
    validate_document_capture,
)
from tedd_knowledge_graph.source_contracts import (
    MANIFEST_BYTES,
    OFFICE_IMAGE_READERS,
    READING_BYTES,
    ArchiveLimits,
    SourceArchiveError,
    SourceMetadata,
    SourceRef,
    SourceScope,
    SourceUnit,
    decode_metadata,
    decode_scope,
    decode_units,
    decoded_png,
    digest,
    encode,
    object_fields,
    parse_json,
    parse_source_uri,
    positive,
    sha256,
    text,
)
from tedd_knowledge_graph.source_documents import DocumentReading, read_document
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.source_images import ImageReading
from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable, require_worker_boundary
from tedd_knowledge_graph.source_preview_recovery import IS_LINUX, PreviewRecovery, recovery_report
from tedd_knowledge_graph.source_reader_contracts import ReaderRequest
from tedd_knowledge_graph.source_review import ReviewRendering, SourceProgress
from tedd_knowledge_graph.source_svg_host import SvgRenderer
from tedd_knowledge_graph.v4_application_contracts import (
    GraphReadScope,
    GraphReadSession,
    ReadScopeKind,
)
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, V4CLIError

if TYPE_CHECKING:
    from tedd_knowledge_graph.source_presentation import PreviewPolicy
    from tedd_knowledge_graph.v4_source_evidence import PreparedSourceProjection

# Release qualification, not an operator-configurable permission or test receipt.
# Add a platform only after its source-bound native-viewer acceptance passes.
QUALIFIED_PREVIEW_PLATFORMS: frozenset[str] = frozenset()
QUALIFIED_PRIVATE_VIEWER_PLATFORMS = frozenset({"win32", "linux"})


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SourceArchiveError("source_corrupt")


def parser() -> argparse.ArgumentParser:
    result = Parser(description=__doc__)
    result.add_argument("--config", required=True)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("status")
    commands.add_parser("disable")
    for action in ("disconnect-google", "reconnect-google"):
        control = commands.add_parser(action)
        control.add_argument("--google-client", required=True, type=Path)
        control.add_argument("--google-account", required=True)
        control.add_argument("--google-credentials", required=True, type=Path)
        control.add_argument(
            "--operation-id",
            default=None,
            help="Opaque prior disconnect ID for receipt reconciliation",
        )
    for name in (
        "init",
        "configure",
        "configure-preview",
        "configure-owner-view",
        "owner-view",
        "preview",
        "capture-chat",
        "capture-pdf",
        "capture-file",
        "intake-file",
        "import-exports",
        "capture-google",
        "list-google",
        "lookup",
        "inspect",
        "render-page",
        "render-image",
        "render-document",
        "forget",
        "restrict",
        "recover",
        "backup",
        "restore",
    ):
        command = commands.add_parser(name)
        for flag in (
            "consumer-id",
            "producer-agent-id",
            "task-id",
            "producer-run-id",
            "subscription-id",
        ):
            command.add_argument("--" + flag, required=True)
        command.add_argument("--subject-id", required=True, action="append")
        if name == "import-exports":
            command.add_argument("--input", required=True)
            command.add_argument("--reader-python")
        if name == "list-google":
            command.add_argument("--folder-id", required=True)
            command.add_argument("--page-token", default="")
        if name in ("init", "configure"):
            for field, maximum in asdict(ArchiveLimits()).items():
                command.add_argument(
                    "--max-" + field.replace("_", "-"),
                    type=int,
                    help=f"Acquisition ceiling (1 to {maximum} bytes); omitted values stay unchanged",
                )
        if name == "configure":
            access = command.add_mutually_exclusive_group()
            access.add_argument(
                "--allow-local-documents",
                dest="local_documents",
                action="store_const",
                const=True,
                default=None,
            )
            access.add_argument(
                "--deny-local-documents", dest="local_documents", action="store_const", const=False
            )
        if name == "configure-preview":
            preview = command.add_mutually_exclusive_group(required=True)
            preview.add_argument(
                "--allow-local-previews", dest="allow_previews", action="store_true"
            )
            preview.add_argument("--deny-previews", dest="allow_previews", action="store_false")
            command.add_argument("--exposure", choices=("local_only", "approved_sync"))
            command.add_argument("--approve-exposure", action="store_true")
        if name == "configure-owner-view":
            owner = command.add_mutually_exclusive_group(required=True)
            owner.add_argument("--allow-owner-view", dest="allow_owner_view", action="store_true")
            owner.add_argument("--deny-owner-view", dest="allow_owner_view", action="store_false")
            command.add_argument(
                "--sensitivity",
                action="append",
                choices=("ordinary", "confidential", "medical", "restricted"),
            )
        if name == "preview":
            command.add_argument("--record-id", required=True, action="append")
            command.add_argument("--reader-python")
            command.add_argument("--renderer")
        if name == "init":
            command.add_argument("--enable", required=True, action="store_true")
            command.add_argument("--archive-root", required=True)
            command.add_argument(
                "--max-bytes",
                type=int,
                help=f"Initial archive quota (1 to {DEFAULT_QUOTA} bytes); existing quotas cannot change",
            )
        if name.startswith("capture-") or name == "intake-file":
            if name != "capture-google":
                command.add_argument("--input", required=True)
            command.add_argument("--metadata", required=True)
            command.add_argument("--acquired-sha256", required=name != "capture-google")
            command.add_argument(
                "--workflow-state",
                required=True,
                choices=("active", "waiting", "paused", "closed", "excluded"),
            )
        if name == "capture-pdf":
            command.add_argument("--reader-python", required=True)
            command.add_argument("--visual-review-sha256", required=True)
            command.add_argument("--visual-pages", required=True, type=int)
        if name in ("capture-file", "intake-file"):
            command.add_argument("--reader-python")
            command.add_argument(
                "--visual-review-sha256",
                help="Exact original digest after reviewing every embedded image; never inferred from text extraction",
            )
        if name == "intake-file":
            command.add_argument("--review-only", action="store_true")
            command.add_argument("--owner-review", action="store_true")
            review = command.add_mutually_exclusive_group()
            review.add_argument("--image-unit", type=int)
            review.add_argument("--document-page", type=int)
            command.add_argument("--renderer")
            command.add_argument("--purpose", choices=("fact", "context", "keep"))
            command.add_argument("--use-once", action="store_true")
            command.add_argument("--whole-file-reason")
            command.add_argument("--byte-start", type=int)
            command.add_argument("--byte-end", type=int)
            command.add_argument("--relevant-generated", action="store_true")
            command.add_argument("--input-root", type=Path)
            command.add_argument("--visual-pages", type=int)
        if name == "lookup":
            command.add_argument("--source-id")
            command.add_argument("--source-version")
            command.add_argument("--query")
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--snapshot-sha256")
        if name in (
            "inspect",
            "forget",
            "restrict",
            "render-page",
            "render-image",
            "render-document",
            "owner-view",
        ):
            command.add_argument("--source-uri", required=True)
        if name == "render-image":
            command.add_argument("--reader-python")
        if name in ("intake-file", "render-image", "owner-view"):
            command.add_argument("--av1-validator")
            command.add_argument("--svg-renderer")
            command.add_argument("--svg-font")
            command.add_argument("--image-rectangle", type=int, nargs=4)
            command.add_argument(
                "--image-view", choices=("presentation", "reconstruction"), default="presentation"
            )
        if name == "render-document":
            command.add_argument("--document-page", required=True, type=int)
            command.add_argument("--reader-python")
            command.add_argument("--renderer")
        if name == "render-page":
            command.add_argument("--page", required=True, type=int)
            command.add_argument("--reader-python", required=True)
            command.add_argument("--renderer", required=True)
        if name in ("inspect", "owner-view"):
            command.add_argument("--max-chars", type=int, default=12_000)
        if name == "owner-view":
            command.add_argument(
                "--private-viewer",
                action="store_true",
                help="Open the explicitly approved human-only local viewer, not Obsidian",
            )
            view = command.add_mutually_exclusive_group()
            view.add_argument("--page", type=int)
            view.add_argument("--image", action="store_true")
            view.add_argument("--document-page", type=int)
            command.add_argument("--reader-python")
            command.add_argument("--renderer")
        if name in ("owner-view", "intake-file", "render-document"):
            command.add_argument("--office-converter")
            command.add_argument("--office-work-root")
        if name == "forget":
            command.add_argument("--confirm-forget", action="store_true")
            command.add_argument("--expect-impact")
        if name in ("backup", "restore"):
            command.add_argument("--destination", required=True)
        if name == "restore":
            command.add_argument("--backup", required=True)
    return result


def load_config(path: Path) -> tuple[bytes, dict[str, object]]:
    snapshot = source_files.read_bytes(
        graph.secure_state_path(path, "Source configuration"), MANIFEST_BYTES
    )
    value = parse_json(snapshot)
    if not isinstance(value, dict):
        raise SourceArchiveError()
    return snapshot, cast(dict[str, object], value)


def save_config(path: Path, expected: bytes, config: dict[str, object]) -> None:
    with graph.store_lock(path, 5):
        current, _ = load_config(path)
        if current != expected:
            raise SourceArchiveError("capture_incomplete")
        graph.atomic_write(path, encode(config).decode("utf-8") + "\n")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def archive_path(value: object, store: Path, config: dict[str, object]) -> Path:
    path = Path(text(value))
    if not path.is_absolute():
        raise SourceArchiveError("privacy_rejected")
    path = Path(os.path.abspath(path))
    excluded = [store.parent]
    vault = config.get("vault_path")
    if isinstance(vault, str) and vault:
        excluded.append(Path(vault).resolve())
    if any(_paths_overlap(path, parent) for parent in excluded):
        raise SourceArchiveError("privacy_rejected")
    return path


def open_archive(
    root: Path,
    scope: SourceScope,
    config: Mapping[str, object],
    *,
    readonly: bool = True,
    provider_check: ProviderCheck | None = None,
    audience: str = "model",
) -> Archive:
    key = "source_owner_view_access_policy" if audience == "owner" else "source_access_policy"
    access = decode_access_policy(config.get(key), scope=scope, audience=audience)
    return Archive.open(
        root,
        scope=scope,
        readonly=readonly,
        access=replace(access, provider_check=provider_check) if access is not None else None,
    )


def check_capture_origin(path: Path, archive: Archive, config: Mapping[str, object]) -> None:
    """Do not turn managed output into a new independent primary source.

    This exclusion is separate from read_input's descriptor/link checks. Approved
    owner-authored files elsewhere in a vault or sync folder remain eligible.
    """
    excluded = [archive.root, Path(text(config.get("store_path"))).absolute().parent]
    vault = config.get("vault_path")
    if vault is not None:
        root_name = text(config.get("root_name", graph.LEGACY_IMPLICIT_ROOT_NAME), maximum=80)
        if root_name in (".", "..") or Path(root_name).name != root_name:
            raise SourceArchiveError("scope_denied")
        excluded.append(Path(text(vault)).resolve() / root_name)
    resolved = path.resolve()
    if any(resolved == root or root in resolved.parents for root in excluded):
        raise SourceArchiveError("scope_denied")


def host_reading_response(
    payload: bytes, reader_python: object, worker_name: str, *worker_args: str
) -> dict[str, object]:
    """One shared bounded process handoff for explicitly selected host readers."""
    require_worker_boundary()
    if not isinstance(reader_python, str) or not reader_python:
        raise SourceArchiveError("capability_unavailable")
    reader_path = Path(reader_python)
    if not reader_path.is_absolute() or not reader_path.is_file():
        raise SourceArchiveError("capability_unavailable")
    if worker_name not in ("source_pdf_host.py", "source_office_host.py"):
        raise SourceArchiveError("capability_unavailable")
    worker = Path(__file__).parent / "tedd_knowledge_graph" / worker_name
    try:
        command = [str(reader_path), "-I", "-B", str(worker), *worker_args]
        if worker_name == "source_office_host.py" and worker_args[:1] == ("image",):
            from tedd_knowledge_graph.source_native_host import run_image_worker

            completed = run_image_worker(command, payload)
        else:
            completed = subprocess.run(
                command,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise SourceArchiveError("capability_unavailable") from None
    value = parse_json(completed.stdout, maximum_bytes=READING_BYTES)
    if not isinstance(value, dict):
        raise SourceArchiveError()
    result = cast(dict[str, object], value)
    if completed.returncode != 0 or result.get("status") != "text_screened":
        code = result.get("status")
        if code not in (
            "source_corrupt",
            "privacy_rejected",
            "limit_exceeded",
            "capture_incomplete",
            "capability_unavailable",
        ):
            code = "capability_unavailable"
        raise SourceArchiveError(str(code))
    return result


def pdf_reading(
    payload: bytes, args: argparse.Namespace, *, host: SourceHost | None = None
) -> PdfReading:
    if sha256(args.visual_review_sha256) != digest(payload):
        raise SourceArchiveError("privacy_rejected")
    result = (
        host.reader.read(ReaderRequest("pdf-text"), payload, host.cancelled)
        if host is not None and host.reader is not None
        else host_reading_response(payload, args.reader_python, "source_pdf_host.py")
    )
    count = positive(result.get("page_count"), maximum=256)
    if count != args.visual_pages or result.get("visual_review_required") is not True:
        raise SourceArchiveError("privacy_rejected")
    return PdfReading(
        sha256(result.get("original_sha256")),
        text(result.get("reader_id")),
        count,
        decode_units(encode(result.get("units"))),
        "screened",
        text(result.get("resources")),
    )


def office_reading(
    payload: bytes,
    kind: str,
    args: argparse.Namespace,
    *,
    sheet_excerpt: bool = False,
    host: SourceHost | None = None,
) -> DocumentReading:
    if host is not None and host.reader is not None:
        if kind != "docx" or sheet_excerpt:
            raise SourceArchiveError("capability_unavailable")
        response = host.reader.read(ReaderRequest("docx-text"), payload, host.cancelled)
    else:
        response = host_reading_response(
            payload,
            args.reader_python,
            "source_office_host.py",
            kind,
            *(("--sheet-excerpt",) if sheet_excerpt else ()),
        )
    result = object_fields(
        response,
        {"status", "resources", "kind", "original_sha256", "reader_id", "units", "gaps"},
    )
    if (
        result["resources"] != "bounded"
        or result["kind"] != kind
        or not isinstance(result["gaps"], list)
    ):
        raise SourceArchiveError()
    reading = DocumentReading(
        sha256(result["original_sha256"]),
        decode_units(encode(result["units"])),
        text(result["reader_id"]),
        tuple(text(item) for item in result["gaps"]),
        kind=kind,
    )
    if reading.original_sha256 != digest(payload):
        raise SourceArchiveError()
    return reading


def _svg_renderer(args: argparse.Namespace) -> SvgRenderer | None:
    executable, font = getattr(args, "svg_renderer", None), getattr(args, "svg_font", None)
    if executable is None and font is None:
        return None
    if not isinstance(executable, str) or not executable or not isinstance(font, str) or not font:
        raise SourceArchiveError("capability_unavailable")
    return SvgRenderer(Path(executable), Path(font))


def _av1_validator(args: argparse.Namespace) -> Av1Validator | None:
    executable = getattr(args, "av1_validator", None)
    if executable is None:
        return None
    if not isinstance(executable, str) or not executable:
        raise SourceArchiveError("capability_unavailable")
    return Av1Validator(Path(executable))


def image_reading(
    payload: bytes,
    reader: object,
    *,
    position: int | None = None,
    rectangle: Sequence[int] | None = None,
    view: str = "presentation",
    svg: SvgRenderer | None = None,
    av1: Av1Validator | None = None,
    host: SourceHost | None = None,
) -> ImageReading:
    if (
        view not in ("presentation", "reconstruction")
        or position is None
        and view != "presentation"
    ):
        raise SourceArchiveError()
    selected = None if position is None else positive(position, maximum=256)
    if host is not None and host.reader is not None:
        if rectangle is not None or view != "presentation" or svg is not None or av1 is not None:
            raise SourceArchiveError("capability_unavailable")
        response = host.reader.read(
            ReaderRequest("image-inspect" if selected is None else "image-page", selected),
            payload,
            host.cancelled,
        )
        result = ImageReading.decode(payload, response)
        if result.view != view or result.position != selected or result.rectangle is not None:
            raise SourceArchiveError()
        return result
    arguments = [] if selected is None else ["--render-unit", str(selected)]
    if rectangle is not None:
        if (
            position is None
            or len(rectangle) != 4
            or any(type(value) is not int for value in rectangle)
        ):
            raise SourceArchiveError()
        arguments.extend(("--rectangle", ",".join(str(value) for value in rectangle)))
    if view != "presentation":
        arguments.extend(("--view", view))
    if svg is not None:
        arguments.extend(("--svg-renderer", str(svg.executable), "--svg-font", str(svg.font)))
    if av1 is not None:
        arguments.extend(("--av1-validator", str(av1.executable)))
    response = host_reading_response(payload, reader, "source_office_host.py", "image", *arguments)
    result = ImageReading.decode(payload, response)
    if (
        result.view != view
        or result.position != position
        or result.rectangle != (tuple(rectangle) if rectangle is not None else None)
    ):
        raise SourceArchiveError()
    return result


@contextmanager
def publication_reservation(store_path: Path) -> Iterator[None]:
    """Reserve SQLite's writer without changing graph state.

    A read snapshot alone cannot stop a concurrent owner/scope change. Keep
    extraction outside this short reservation; never write graph rows here.
    """
    # Normal writable open may upgrade a legacy schema or create a plan key.
    # First require a qualified readonly store, then reserve an existing database
    # without either of those setup paths. No SQL mutation is permitted here.
    with v4.open_store(store_path, readonly=True):
        before = store_path.stat()
        with closing(
            sqlite3.connect(store_path.as_uri() + "?mode=rw", uri=True, timeout=5)
        ) as reservation:
            reservation.set_authorizer(
                lambda action, _a, _b, _db, _trigger: (
                    sqlite3.SQLITE_OK
                    if action == sqlite3.SQLITE_TRANSACTION
                    else sqlite3.SQLITE_DENY
                )
            )
            reservation.execute("BEGIN IMMEDIATE")
            try:
                current = store_path.stat()
                if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                    raise SourceArchiveError("scope_denied")
                yield
            finally:
                reservation.rollback()


@contextmanager
def _capture_reservation(
    archive: Archive,
    args: argparse.Namespace,
    scope: SourceScope,
    session: GraphReadSession,
    *,
    check_access: bool,
) -> Iterator[None]:
    """Fresh local authority for a bounded handoff or publication—not owner input."""
    config_path = Path(args.config)
    with graph.store_lock(config_path, 5):
        _, config = load_config(config_path)
        if config.get("source_capture_enabled") is not True:
            raise SourceArchiveError("scope_denied")
        store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
        if archive_path(config.get("source_archive_root"), store_path, config) != archive.root:
            raise SourceArchiveError("scope_denied")
        check_capture_origin(Path(args.input), archive, config)
        if check_access:
            audience = archive.access.audience if archive.access is not None else "model"
            key = (
                "source_owner_view_access_policy" if audience == "owner" else "source_access_policy"
            )
            if (
                decode_access_policy(config.get(key), scope=scope, audience=audience)
                != archive.access
            ):
                raise SourceArchiveError("scope_denied")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as guard:
            graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
            subjects = tuple(args.subject_id)
            with guard.read_snapshot():
                if authorized_scope(guard, session, graph_scope, subjects).key != scope.key:
                    raise SourceArchiveError("scope_denied")
                yield
            with guard.read_snapshot():
                if authorized_scope(guard, session, graph_scope, subjects).key != scope.key:
                    raise SourceArchiveError("scope_denied")


def capture(
    archive: Archive,
    args: argparse.Namespace,
    scope: SourceScope,
    session: GraphReadSession,
    *,
    host: SourceHost | None = None,
) -> dict[str, object]:
    from tedd_knowledge_graph import source_intake

    selection = None
    review_only = args.command == "intake-file" and args.review_only
    owner_review = args.command == "intake-file" and args.owner_review
    if args.command == "intake-file":
        if (args.image_unit is not None or args.document_page is not None) and not review_only:
            raise SourceArchiveError("source_corrupt")
        if args.document_page is None and any(
            (args.renderer, args.office_converter, args.office_work_root)
        ):
            raise SourceArchiveError("scope_denied")
        if (args.byte_start is None) != (args.byte_end is None):
            raise SourceArchiveError("capture_incomplete")
        selection = source_intake.IntakeSelection(
            purpose=args.purpose,
            use_once=args.use_once,
            whole_file_reason=args.whole_file_reason,
            byte_range=(args.byte_start, args.byte_end) if args.byte_start is not None else None,
            relevant_generated=args.relevant_generated,
            root=args.input_root,
        )
        if host is not None:
            if host.authorize_local is None or host.collecting is None:
                raise SourceArchiveError("capability_unavailable")
            if not host.authorize_local(Path(args.input)):
                selection = replace(selection, use_once=True)
        skipped = selection.skip_reason(Path(args.input))
        if skipped is not None:
            return {"status": "not_preserved", "reason": skipped}
    metadata: SourceMetadata = decode_metadata(
        parse_json(source_files.read_bytes(Path(args.metadata), archive.limits.manifest_bytes))
    )
    if (
        metadata.scope.key != scope.key
        or metadata.task_id != session.task_id
        or metadata.run_id != session.run_id
    ):
        raise SourceArchiveError("scope_denied")
    if review_only and metadata.kind not in OFFICE_KINDS | {"image"}:
        raise SourceArchiveError("capability_unavailable")
    if owner_review and (
        metadata.kind != "image"
        or host is None
        or host.review_owner_image is None
        or host.collecting is None
        or sys.platform not in QUALIFIED_PRIVATE_VIEWER_PLATFORMS
        or archive.access is None
        or archive.access.audience != "owner"
    ):
        raise SourceArchiveError("capability_unavailable")
    if (
        args.command == "intake-file"
        and args.image_rectangle is not None
        and metadata.kind != "image"
    ):
        raise SourceArchiveError("capability_unavailable")
    if metadata.kind == "image" and (
        args.command != "intake-file"
        or host is None
        or host.reviews is None
        or args.document_page is not None
    ):
        raise SourceArchiveError("capability_unavailable")
    maximum = archive.limits.for_kind(metadata.kind)
    acquired = sha256(args.acquired_sha256)
    if selection is not None and metadata.kind == "pdf":
        if archive.access is None or not archive.access.local_retention:
            raise SourceArchiveError("capability_unavailable")
        archive.access.authorize(metadata, acquired, "capture")
    elif args.command in ("capture-file", "intake-file"):
        validate_document_capture(archive, metadata, acquired)
    elif metadata.details is not None or metadata.kind != {
        "capture-chat": "chat",
        "capture-pdf": "pdf",
    }.get(args.command):
        raise SourceArchiveError("capability_unavailable")
    _, initial_config = load_config(Path(args.config))
    check_capture_origin(Path(args.input), archive, initial_config)
    document = None
    image = None
    if selection is not None:
        prepared = source_intake.prepare_local(Path(args.input), selection, metadata, maximum)
        if prepared is None:
            raise SourceArchiveError("capture_incomplete")
        payload, metadata, document = prepared
    else:
        payload = source_files.read_input(Path(args.input), maximum)
    if digest(payload) != acquired:
        raise SourceArchiveError()
    reading = pdf_reading(payload, args, host=host) if metadata.kind == "pdf" else None
    if metadata.kind == "image":
        image = image_reading(
            payload,
            args.reader_python,
            position=args.image_unit if review_only else None,
            rectangle=args.image_rectangle,
            view=args.image_view,
            svg=_svg_renderer(args),
            av1=_av1_validator(args),
            host=host,
        )
        if metadata.details is None:
            raise SourceArchiveError()
        image.inventory.require_metadata_permission(metadata.details.sensitivity)
        document = image.document
    if (
        args.command in ("capture-file", "intake-file")
        and metadata.kind != "pdf"
        and document is None
    ):
        document = (
            office_reading(payload, metadata.kind, args, host=host)
            if metadata.kind in OFFICE_KINDS
            else read_document(payload, metadata.kind, maximum_bytes=maximum)
        )
    reviewed: dict[str, object] | None = None
    owner_acknowledgment = None
    if review_only:
        assert document is not None
        reviewed = {
            "status": "source_review",
            "original_sha256": acquired,
            "reader_id": document.reader_id,
            "gaps": document.gaps,
            "untrusted_source_content": True,
            "preserved": False,
        }
        if image is not None:
            assert host is not None and host.reviews is not None
            reviewed.update(stage=SourceProgress("decoded").stage, unit_count=len(document.units))
            if image.png is not None:
                rendering = image.review(
                    metadata.scope.key, metadata.logical_key, "owner" if owner_review else "model"
                )
                book = host.reviews
                token = book.offer(rendering)
                if owner_review:
                    assert host.review_owner_image is not None and host.collecting is not None
                    collecting = host.collecting

                    @contextmanager
                    def owner_handoff() -> Iterator[None]:
                        with (
                            _capture_reservation(archive, args, scope, session, check_access=True),
                            collecting(session, scope),
                        ):
                            book.for_delivery(
                                token, audience="owner", png_sha256=rendering.png_sha256
                            )
                            yield
                            book.delivered(token)

                    if not host.review_owner_image(rendering, image.png, owner_handoff()):
                        raise SourceArchiveError("privacy_rejected")
                    owner_acknowledgment = token
                    reviewed.update(human_only=True, stage=SourceProgress("reviewed").stage)
                else:
                    reviewed.update(
                        stage=SourceProgress("rendered").stage,
                        review_id=token,
                        position=image.position,
                        original_size=list(rendering.original_size),
                        output_size=list(rendering.output_size),
                        orientation=rendering.orientation,
                        rectangle=list(rendering.rectangle)
                        if rendering.rectangle is not None
                        else None,
                        png_sha256=rendering.png_sha256,
                        png_base64=base64.b64encode(image.png).decode("ascii"),
                    )
                    if rendering.view != "presentation":
                        reviewed["view"] = rendering.view
            else:
                reviewed["units"] = [asdict(unit) for unit in document.units]
        elif args.document_page is not None:
            reviewed.update(_office_document_page(archive, args, payload, metadata.kind, host))
            reviewed["status"] = "source_review"
        elif args.image_unit is None:
            reviewed["unit_count"] = len(document.units)
            reviewed["units"] = [asdict(unit) for unit in document.units]
        else:
            index = positive(args.image_unit, maximum=len(document.units))
            if host is not None and host.reader is not None:
                raise SourceArchiveError("capability_unavailable")
            png = _render_office_png(
                payload, metadata.kind, document.units[index - 1], args.reader_python
            )
            reviewed.update(
                {
                    "position": index,
                    "png_sha256": digest(png),
                    "png_base64": base64.b64encode(png).decode("ascii"),
                }
            )
    # Extraction holds no configuration lock. Disable can finish during a slow
    # reader; publication then checks that decision under the same lock as disable.
    # The fresh graph connection deliberately does not reuse acquisition's WAL snapshot.
    reservation = (
        host.collecting(session, scope)
        if host is not None and host.collecting is not None
        else nullcontext()
    )
    # Waiting for configuration/SQLite/archive ownership is not publication.
    # The archive enters this host reservation immediately before durable writes.
    with _capture_reservation(
        archive, args, scope, session, check_access=document is not None or selection is not None
    ):
        if reviewed is not None:
            with reservation:
                if owner_acknowledgment is not None:
                    assert host is not None and host.reviews is not None
                    host.reviews.acknowledge(owner_acknowledgment, audience="owner")
                if host is not None and host.retain_delivery is not None:

                    @contextmanager
                    def review_delivery() -> Iterator[None]:
                        with _capture_reservation(archive, args, scope, session, check_access=True):
                            if host.authorize_local is None or not host.authorize_local(
                                Path(args.input)
                            ):
                                raise SourceArchiveError("scope_denied")
                            yield

                    host.retain_delivery(review_delivery)
                return reviewed
        if document is not None:
            receipt = capture_document(
                archive,
                metadata=metadata,
                payload=payload,
                acquired_sha256=acquired,
                reading=document,
                visual_review_sha256=args.visual_review_sha256,
                image=image,
                reviews=host.reviews if host is not None else None,
                publication=reservation,
            )
        elif reading is None:
            receipt = capture_chat(
                archive,
                metadata=metadata,
                payload=payload,
                acquired_sha256=acquired,
                publication=reservation,
            )
        else:
            receipt = capture_pdf(
                archive,
                metadata=metadata,
                payload=payload,
                reading=reading,
                publication=reservation,
            )
    return {
        **asdict(receipt),
        **(
            {"stage": SourceProgress("preserved", ref=receipt.ref).stage}
            if image is not None
            else {}
        ),
    }


def render_page(
    archive: Archive, args: argparse.Namespace, host: SourceHost | None = None
) -> dict[str, object]:
    """Return an ephemeral bounded PNG to a host image bridge, never a disk cache.

    The host must display the PNG as an image, not paste its encoding into a
    prompt or graph. The text evidence budget is separate from this image route.
    """
    payload, ref = archive.pdf_page(parse_source_uri(args.source_uri), args.page)
    png = _render_pdf_png(
        payload,
        args.page,
        Path(args.reader_python) if args.reader_python is not None else None,
        Path(args.renderer) if args.renderer is not None else None,
        host=host,
    )
    archive.verify_refs((ref,), operation="render")
    return {
        "status": "rendered",
        "source_uri": ref.as_uri(),
        "page": args.page,
        "original_sha256": digest(payload),
        "png_sha256": digest(png),
        "png_base64": base64.b64encode(png).decode("ascii"),
        "untrusted_source_content": True,
    }


def _saved_image(
    archive: Archive,
    ref: SourceRef,
    reader: object,
    rectangle: Sequence[int] | None,
    *,
    operation: str,
    view: str = "presentation",
    svg: SvgRenderer | None = None,
    av1: Av1Validator | None = None,
    host: SourceHost | None = None,
) -> tuple[bytes, bytes, SourceUnit, ReviewRendering | None, tuple[str, ...]]:
    """One immutable-original rendering path for model and private owner audiences."""
    payload, kind, selected = archive.image(ref, operation=operation)
    if kind == "image":
        reading = image_reading(
            payload,
            reader,
            position=selected.position,
            rectangle=rectangle,
            view=view,
            svg=svg,
            av1=av1,
            host=host,
        )
        details = archive._manifest(ref.manifest_sha256).metadata.details
        if details is None:
            raise SourceArchiveError()
        reading.inventory.require_metadata_permission(details.sensitivity)
        if selected != reading.document.units[selected.position - 1] or reading.png is None:
            raise SourceArchiveError()
        preview = reading.review(
            archive.scope.key,
            ref.manifest_sha256,
            "owner" if operation == "owner_view" else "model",
        )
        return payload, reading.png, selected, preview, reading.inventory.gaps
    if rectangle is not None or view != "presentation":
        raise SourceArchiveError("capability_unavailable")
    if host is not None and host.reader is not None:
        raise SourceArchiveError("capability_unavailable")
    return payload, _render_office_png(payload, kind, selected, reader), selected, None, ()


def _image_geometry(preview: ReviewRendering | None) -> dict[str, object]:
    return (
        {}
        if preview is None
        else {
            "original_size": list(preview.original_size),
            "output_size": list(preview.output_size),
            "orientation": preview.orientation,
            "rectangle": list(preview.rectangle) if preview.rectangle is not None else None,
            **({"view": preview.view} if preview.view != "presentation" else {}),
        }
    )


def render_image(
    archive: Archive, args: argparse.Namespace, host: SourceHost | None = None
) -> dict[str, object]:
    ref = parse_source_uri(args.source_uri)
    payload, png, selected, preview, gaps = _saved_image(
        archive,
        ref,
        args.reader_python,
        args.image_rectangle,
        operation="render",
        view=args.image_view,
        svg=_svg_renderer(args),
        av1=_av1_validator(args),
        host=host,
    )
    archive.verify_refs((ref,), operation="render")
    return {
        "status": "rendered",
        "source_uri": ref.as_uri(),
        "position": selected.position,
        "original_sha256": digest(payload),
        "png_sha256": digest(png),
        "png_base64": base64.b64encode(png).decode("ascii"),
        "untrusted_source_content": True,
        **_image_geometry(preview),
        **({"gaps": list(gaps)} if preview is not None else {}),
    }


def _render_office_png(payload: bytes, kind: str, selected: SourceUnit, reader: object) -> bytes:
    if selected.kind != "image_page":
        raise SourceArchiveError("source_unavailable")
    result = object_fields(
        host_reading_response(
            payload, reader, "source_office_host.py", kind, "--render-unit", str(selected.position)
        ),
        {
            "status",
            "resources",
            "kind",
            "original_sha256",
            "position",
            "unit",
            "reader_id",
            "png_sha256",
            "png_base64",
        },
    )
    if (
        result["resources"] != "bounded"
        or result["kind"] != kind
        or result["original_sha256"] != digest(payload)
        or type(result["position"]) is not int
        or result["position"] != selected.position
        or result["reader_id"] not in OFFICE_IMAGE_READERS
        or encode(result["unit"]) != encode(asdict(selected))
    ):
        raise SourceArchiveError()
    return decoded_png(result)


def _office_document_page(
    archive: Archive, args: argparse.Namespace, payload: bytes, kind: str, host: SourceHost | None
) -> dict[str, object]:
    if host is not None and host.reader is not None:
        if kind != "docx":
            raise SourceArchiveError("capability_unavailable")
        return host.reader.read(
            ReaderRequest("docx-page", args.document_page), payload, host.cancelled
        )
    from tedd_knowledge_graph.source_office_host import document_page

    require_worker_boundary()
    if not all((args.reader_python, args.renderer, args.office_converter, args.office_work_root)):
        raise SourceArchiveError("capability_unavailable")
    _, config = load_config(Path(args.config))
    store = graph.secure_state_path(config.get("store_path"), "Canonical store")
    root = archive_path(args.office_work_root, store, config)
    if _paths_overlap(root, archive.root):
        raise SourceArchiveError("privacy_rejected")
    return document_page(
        payload,
        kind,
        args.document_page,
        reader=Path(args.reader_python),
        converter=Path(args.office_converter),
        renderer=Path(args.renderer),
        work_root=root,
        cancelled=host.cancelled if host else None,
    )


def render_document(
    archive: Archive, args: argparse.Namespace, host: SourceHost | None
) -> dict[str, object]:
    ref = parse_source_uri(args.source_uri)
    payload, kind = archive.office_document(ref)
    result = _office_document_page(archive, args, payload, kind, host)
    archive.verify_refs((ref,), operation="render")
    return {
        **result,
        "status": "rendered",
        "source_uri": ref.as_uri(),
        "untrusted_source_content": True,
    }


def _render_pdf_png(
    payload: bytes,
    page: int,
    reader: Path | None,
    renderer: Path | None,
    *,
    host: SourceHost | None = None,
) -> bytes:
    """Shared bounded worker validation; bytes are never a serializable access grant."""
    if host is not None and host.reader is not None:
        return decoded_png(
            host.reader.read(ReaderRequest("pdf-page", page), payload, host.cancelled)
        )
    require_worker_boundary()
    if (
        reader is None
        or renderer is None
        or not reader.is_absolute()
        or not reader.is_file()
        or not renderer.is_absolute()
        or not renderer.is_file()
    ):
        raise SourceArchiveError("capability_unavailable")
    worker = Path(__file__).parent / "tedd_knowledge_graph" / "source_pdf_host.py"
    try:
        completed = subprocess.run(
            [
                str(reader),
                "-I",
                "-B",
                str(worker),
                "--render-page",
                str(page),
                "--renderer",
                str(renderer),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SourceArchiveError("capability_unavailable") from None
    value = parse_json(completed.stdout, maximum_bytes=READING_BYTES)
    if not isinstance(value, dict) or completed.returncode or value.get("status") != "rendered":
        raise SourceArchiveError("capability_unavailable")
    if (
        value.get("original_sha256") != digest(payload)
        or type(value.get("page")) is not int
        or value.get("page") != page
        or value.get("resources") != "bounded"
    ):
        raise SourceArchiveError()
    return decoded_png(value)


def _execute(
    args: argparse.Namespace,
    *,
    provider_check: ProviderCheck | None = None,
    host: SourceHost | None = None,
) -> dict[str, object]:
    config_path = Path(args.config)
    snapshot, config = load_config(config_path)
    enabled = config.get("source_capture_enabled", False)
    if type(enabled) is not bool:
        raise SourceArchiveError()
    if args.command == "status":
        return {
            "enabled": enabled,
            "archive_configured": bool(config.get("source_archive_root")),
            **_preview_recovery_status(config_path),
            **(
                {"export_recovery": recovery_report(config_path.parent / "source-export-recovery")}
                if IS_LINUX
                else {}
            ),
        }
    if args.command == "disable":
        with graph.store_lock(config_path, 5):
            if load_config(config_path)[0] != snapshot:
                raise SourceArchiveError("scope_denied")
            changed = {**config, "source_capture_enabled": False}
            preview = config.get("source_preview_policy")
            if isinstance(preview, dict):
                changed["source_preview_policy"] = {**preview, "enabled": False}
            graph.atomic_write(config_path, encode(changed).decode("utf-8") + "\n")
            cleanup_result = _cleanup_owner_preview_files(changed, config_path=config_path)
        return {"status": "disabled", "history_deleted": False, "preview_cleanup": cleanup_result}
    if (args.command.startswith("capture-") or args.command == "intake-file") and run_if_collecting(
        lambda: True, state=args.workflow_state, enabled=enabled
    ) is None:
        return {"status": "inactive"}
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    with v4.open_store(store_path, readonly=True) as store:
        scope = authorized_scope(store, session, graph_scope, tuple(args.subject_id))
        configured_root = config.get("source_archive_root")
        if args.command == "init":
            maximum = (
                positive(args.max_bytes, maximum=DEFAULT_QUOTA)
                if args.max_bytes is not None
                else None
            )
            root = archive_path(args.archive_root, store_path, config)
            if configured_root and Path(text(configured_root)) != root:
                raise SourceArchiveError("scope_denied")
            if root.exists():
                existing = Archive.open(root, scope=scope)
                if maximum is not None and maximum != existing.max_bytes:
                    raise SourceArchiveError("scope_denied")
                if requested_limits(args, existing.limits) != existing.limits:
                    raise SourceArchiveError("scope_denied")
            else:
                Archive.initialize(
                    root,
                    scope=scope,
                    max_bytes=maximum or DEFAULT_QUOTA,
                    limits=requested_limits(args, ArchiveLimits()),
                )
        else:
            root = archive_path(configured_root, store_path, config)
            writable = args.command in (
                "capture-chat",
                "capture-pdf",
                "capture-file",
                "intake-file",
                "forget",
                "recover",
            ) and not (args.command == "intake-file" and args.review_only)
            archive = open_archive(
                root,
                scope,
                config,
                readonly=not writable,
                provider_check=provider_check,
                audience="owner"
                if args.command == "intake-file" and args.owner_review
                else "model",
            )
            if args.command.startswith("capture-") or args.command == "intake-file":
                return capture(archive, args, scope, session, host=host)
            if args.command == "lookup":
                return {
                    "basis": "fresh_source_discovery",
                    **asdict(
                        archive.discover(
                            source_id=args.source_id,
                            source_version=args.source_version,
                            query=args.query,
                            offset=args.offset,
                            snapshot_sha256=args.snapshot_sha256,
                        )
                    ),
                }
            if args.command == "inspect":
                excerpt = archive.read(parse_source_uri(args.source_uri), max_chars=args.max_chars)
                inspection = {
                    "basis": "fresh_source_check",
                    "untrusted_source_content": True,
                    **asdict(excerpt),
                }
                if len(encode(inspection).decode("utf-8")) + 1 > args.max_chars:
                    raise SourceArchiveError("limit_exceeded")
                return inspection
            if args.command == "render-page":
                return render_page(archive, args, host)
            if args.command == "render-image":
                return render_image(archive, args, host)
            if args.command == "render-document":
                return render_document(archive, args, host)
            if args.command == "recover":
                archive.recover()
                return {"status": "recovered"}
            if args.command == "backup":
                archive.backup(Path(args.destination))
                return {"status": "backup_verified", "active": False}
            if args.command == "restore":
                Archive.restore(Path(args.backup), Path(args.destination), current=archive)
                return {"status": "restored_not_activated", "active": False}
            raise SourceArchiveError()
    save_config(
        config_path,
        snapshot,
        {**config, "source_archive_root": str(root), "source_capture_enabled": True},
    )
    return {"status": "enabled", "collection_started": False}


@contextmanager
def _read_reservation(
    config_path: Path,
    snapshot: bytes,
    store_path: Path,
    archive: Archive,
    session: GraphReadSession,
    graph_scope: GraphReadScope,
    subjects: tuple[str, ...],
    state: Mapping[str, object],
    refs: tuple[SourceRef, ...],
    heads: Mapping[SourceRef, str] | None,
) -> Iterator[None]:
    """Pin exact locally authorized read state through a bounded transport handoff.

    This performs no provider calls; external checks have already authorized the
    prepared response. The actual sender nests provider/turn fences inside it.
    """
    with graph.store_lock(config_path, 5):
        if load_config(config_path)[0] != snapshot:
            raise SourceArchiveError("scope_denied")
        with (
            publication_reservation(store_path),
            v4.open_store(store_path, readonly=True) as current,
        ):
            with current.read_snapshot():
                if (
                    authorized_scope(current, session, graph_scope, subjects).key
                    != archive.scope.key
                    or dict(current.state()) != state
                ):
                    raise SourceArchiveError("scope_denied")
                with archive.hold_verified_refs(refs, expected_heads=heads):
                    yield


def execute(args: argparse.Namespace, *, host: SourceHost | None = None) -> dict[str, object]:
    """Fresh release-boundary checks for all source-discovery/read routes."""
    if args.command == "owner-view":
        return owner_view_command(args, host)
    if args.command == "capture-google":
        return capture_google_command(args, host)
    if args.command == "import-exports":
        return import_exports_command(args, host)
    if args.command == "list-google":
        return list_google_command(args, host)
    if args.command == "forget":
        return forget_command(args)
    if args.command == "restrict":
        return _restrict_source(
            Path(args.config),
            GraphReadSession(
                args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
            ),
            GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id),
            tuple(args.subject_id),
            parse_source_uri(args.source_uri),
        )
    if args.command in ("configure", "configure-owner-view"):
        return configure_command(args)
    if args.command == "configure-preview":
        return configure_preview_command(args)
    if args.command == "preview":
        return preview_command(args)
    if args.command not in ("lookup", "inspect", "render-page", "render-image", "render-document"):
        return _execute(args, host=host)
    config_path = Path(args.config)
    config_bytes, config = load_config(config_path)
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    with v4.open_store(store_path, readonly=True) as store:
        with store.read_snapshot():
            scope = authorized_scope(store, session, graph_scope, tuple(args.subject_id))
            state = dict(store.state())
        result = (
            _execute(args, provider_check=host.provider_check, host=host)
            if host is not None
            else _execute(args)
        )
        archive = open_archive(
            archive_path(config.get("source_archive_root"), store_path, config),
            scope,
            config,
            provider_check=host.provider_check if host is not None else None,
        )
        heads: dict[SourceRef, str] | None = None
        if args.command == "lookup":
            latest = archive.discover(
                source_id=args.source_id,
                source_version=args.source_version,
                query=args.query,
                offset=args.offset,
                snapshot_sha256=args.snapshot_sha256,
            )
            if result.get("snapshot_sha256") != latest.snapshot_sha256:
                raise SourceArchiveError("capture_incomplete")
            # Permission selection is not part of the archive snapshot hash.
            # Release the freshly authorized page, never its precheck metadata.
            result = {"basis": "fresh_source_discovery", **asdict(latest)}
            refs = tuple(parse_source_uri(item.source_uri) for item in latest.sources)
        else:
            ref = parse_source_uri(args.source_uri)
            refs = (ref,)
            if args.command == "inspect":
                related = result.get("later_versions")
                if not isinstance(related, (tuple, list)):
                    raise SourceArchiveError()
                for value in related:
                    item = object_fields(
                        value, {"archive_id", "scope_id", "manifest_sha256", "anchor_id"}
                    )
                    refs += (
                        SourceRef(
                            text(item["archive_id"]),
                            text(item["scope_id"]),
                            text(item["manifest_sha256"]),
                            text(item["anchor_id"]),
                        ),
                    )
                heads = {selected: sha256(result.get("history_head")) for selected in refs}
            archive.verify_refs(
                refs,
                expected_heads=heads,
                operation="render"
                if args.command in ("render-page", "render-image", "render-document")
                else "inspect",
            )

        # Remote checks precede this LOCAL reservation. Re-enter it at actual MCP
        # write, using a fresh graph handle rather than the acquisition snapshot.
        read_delivery = partial(
            _read_reservation,
            config_path,
            config_bytes,
            store_path,
            archive,
            session,
            graph_scope,
            tuple(args.subject_id),
            state,
            refs,
            heads,
        )

        with read_delivery():
            pass
        if host is not None and host.retain_delivery is not None:
            host.retain_delivery(read_delivery)
        return result


def list_google_command(args: argparse.Namespace, host: SourceHost | None) -> dict[str, object]:
    """Read one task-authorized folder page without creating source observations."""
    if host is None or host.list_google is None or host.collecting is None:
        raise SourceArchiveError("capability_unavailable")
    config_path = Path(args.config)
    snapshot, config = load_config(config_path)
    if config.get("source_capture_enabled") is not True:
        raise SourceArchiveError("scope_denied")
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    subjects = tuple(args.subject_id)
    with v4.open_store(store_path, readonly=True) as store:
        with store.read_snapshot():
            scope = authorized_scope(store, session, graph_scope, subjects)
            state = dict(store.state())
        with host.collecting(session, scope):
            pass
        result = host.list_google(scope, args.folder_id, args.page_token)
        with host.collecting(session, scope), store.read_snapshot():
            if (
                authorized_scope(store, session, graph_scope, subjects) != scope
                or dict(store.state()) != state
                or load_config(config_path)[0] != snapshot
            ):
                raise SourceArchiveError("scope_denied")
        return result


def requested_limits(args: argparse.Namespace, existing: ArchiveLimits) -> ArchiveLimits:
    changes = {
        field: positive(value)
        for field in asdict(existing)
        if (value := getattr(args, "max_" + field, None)) is not None
    }
    if args.command == "configure" and not changes:
        raise SourceArchiveError()
    return replace(existing, **changes)


def import_exports_command(args: argparse.Namespace, host: SourceHost | None) -> dict[str, object]:
    """Owner-confirmed retained copies, not an authenticated provider acquisition.

    Validate and read the bounded batch before any publication. Each archive write
    is atomic; a later failure reports earlier receipts, never an atomic-batch claim.
    """
    from tedd_knowledge_graph.source_intake import owner_export_batch, owner_export_description

    snapshot, config = load_config(Path(args.config))
    if config.get("source_capture_enabled") is not True:
        return {"status": "inactive"}
    if (
        host is None
        or host.export_root is None
        or host.confirm_exports is None
        or host.collecting is None
        or host.check_retention is None
        or host.cancelled is None
    ):
        raise SourceArchiveError("capability_unavailable")
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    subjects = tuple(args.subject_id)
    with v4.open_store(store_path, readonly=True) as store:
        scope = authorized_scope(store, session, graph_scope, subjects)
    archive = open_archive(
        archive_path(config.get("source_archive_root"), store_path, config),
        scope,
        config,
        readonly=False,
    )
    if archive.access is None or not archive.access.local_retention:
        raise SourceArchiveError("scope_denied")
    batch = owner_export_batch(
        parse_json(source_files.read_bytes(Path(args.input), MANIFEST_BYTES)), host.export_root
    )
    metadata = tuple(
        item.metadata(scope, text(session.task_id), text(session.run_id), graph.utc_now())
        for item in batch
    )
    for item, source in zip(batch, metadata, strict=True):
        check_capture_origin(item.path, archive, config)
        archive.access.validate_metadata(source, "capture")
        host.check_retention(source)
    with host.collecting(session, scope):
        pass
    # No graph/archive/turn lock spans the human interaction.
    if not host.confirm_exports(owner_export_description(batch)):
        return {"status": "not_preserved", "reason": "confirmation_not_accepted"}

    bindings = frozenset(
        access_binding(source, item.payload_sha256)
        for item, source in zip(batch, metadata, strict=True)
    )

    def confirmed(
        source: SourceMetadata, checksum: str, operation: str, audience: str
    ) -> AccessDecision:
        assert host.check_retention is not None and host.cancelled is not None
        host.check_retention(source)
        binding = access_binding(source, checksum)
        if (
            host.cancelled.is_set()
            or operation != "capture"
            or audience != "model"
            or binding not in bindings
        ):
            raise SourceArchiveError("scope_denied")
        return AccessDecision(binding, operation, audience, "allowed")

    archive.access = replace(archive.access, provider_check=confirmed)

    @contextmanager
    def publication(source: SourceMetadata) -> Iterator[None]:
        assert host.collecting is not None and host.check_retention is not None
        with host.collecting(session, scope):
            # The archive enters this after waiting for its lock. A new denial
            # during that wait must not reuse the earlier retention decision.
            host.check_retention(source)
            yield

    cleanup: list[tuple[Path, tuple[int, int], bytes]] = []
    readings: list[tuple[bytes, SourceMetadata, DocumentReading]] = []
    receipts: list[dict[str, object]] = []
    cleanup_result: dict[str, object] = {}
    failure: str | None = None
    try:
        total = 0
        for item, source in zip(batch, metadata, strict=True):
            with host.collecting(session, scope):
                pass
            host.check_retention(source)
            info = item.path.stat(follow_symlinks=False)
            payload = source_files.read_input(item.path, archive.limits.for_kind(item.kind))
            if digest(payload) != item.payload_sha256:
                raise SourceArchiveError("capture_incomplete")
            cleanup.append((item.path, (info.st_dev, info.st_ino), payload))
            total += len(payload)
            if total > archive.limits.for_kind(item.kind):
                raise SourceArchiveError("limit_exceeded")
            reading = office_reading(
                payload,
                item.kind,
                args,
                sheet_excerpt=item.sheet_excerpt is not None,
                host=host,
            )
            if (
                "embedded_images_require_visual_review" in reading.gaps
                and item.visual_review_sha256 != digest(payload)
            ):
                raise SourceArchiveError("privacy_rejected")
            bound = bind_document_reading(source, payload, reading)
            archive.access.authorize(bound, item.payload_sha256, "capture")
            readings.append((payload, bound, reading))
        with graph.store_lock(Path(args.config), 5):
            if load_config(Path(args.config))[0] != snapshot:
                raise SourceArchiveError("scope_denied")
            with (
                publication_reservation(store_path),
                v4.open_store(store_path, readonly=True) as store,
            ):
                if authorized_scope(store, session, graph_scope, subjects) != scope:
                    raise SourceArchiveError("scope_denied")
                for payload, source, reading in readings:
                    receipt = archive.publish(
                        payload=payload,
                        metadata=source,
                        units=reading.units,
                        publication=publication(source),
                    )
                    receipts.append(asdict(receipt))
    except SourceArchiveError as error:
        failure = error.code
    except OSError:
        failure = "source_unavailable"
    finally:
        cleanup_result = _cleanup_export_copies(
            Path(args.config), config, scope, host.export_root, cleanup
        )
    return {
        "status": "preserved" if failure is None else "partial" if receipts else "not_preserved",
        "receipts": receipts,
        "reason": failure,
        **cleanup_result,
        "unread_or_changed_inputs_retained": len(batch) - len(cleanup),
    }


def _cleanup_export_copies(
    config_path: Path,
    config: dict[str, object],
    scope: SourceScope,
    export_root: Path,
    copies: list[tuple[Path, tuple[int, int], bytes]],
) -> dict[str, object]:
    results: list[dict[str, object]] = [
        {"item": index, "removed": False} for index in range(1, len(copies) + 1)
    ]
    recovery: PreviewRecovery | None = None
    recovery_error = False
    try:
        # No human interaction or provider call under this lock. Shared recovery
        # entries must not be reconciled by two imports concurrently.
        with graph.store_lock(config_path, 5) if IS_LINUX else nullcontext():
            if IS_LINUX and copies:
                if load_config(config_path)[1] != config:
                    raise SourceArchiveError("scope_denied")
                root = _recovery_root(config_path, config, "source-export-recovery")
                if _paths_overlap(root, export_root):
                    raise SourceArchiveError("privacy_rejected")
                key = config.get("source_export_recovery_key")
                if key is None:
                    key = secrets.token_hex(32)
                    graph.atomic_write(
                        config_path,
                        encode({**config, "source_export_recovery_key": key}).decode() + "\n",
                    )
                recovery = PreviewRecovery(
                    root,
                    sha256(key),
                    digest(encode(["owner-export", scope.key, str(export_root)])),
                    export_root=export_root,
                )
            for result, (path, identity, payload) in zip(results, copies, strict=True):
                try:
                    result["removed"] = (
                        recovery.mutate(path, identity, payload, None)
                        if recovery is not None
                        else source_files.remove_confirmed_export(path, identity, payload)
                    )
                except (OSError, SourceArchiveError):
                    result["removed"] = False
    except (OSError, SourceArchiveError):
        recovery_error = True
    output: dict[str, object] = {"cleanup": results}
    if IS_LINUX:
        report = (
            recovery.report()
            if recovery is not None
            else recovery_report(config_path.parent / "source-export-recovery")
        )
        if recovery_error:
            report.update(recovery_status="required", recovery_failed=True)
        output["export_recovery"] = report
    return output


def capture_google_command(args: argparse.Namespace, host: SourceHost | None) -> dict[str, object]:
    """Acquire one authorized provider observation; never accept a JSON access grant."""
    config_path = Path(args.config)
    snapshot, config = load_config(config_path)
    enabled = config.get("source_capture_enabled", False)
    if not isinstance(enabled, bool):
        raise SourceArchiveError()
    if (
        run_if_collecting(
            lambda: True,
            state=args.workflow_state,
            enabled=enabled,
        )
        is None
    ):
        return {"status": "inactive"}
    if (
        host is None
        or host.acquire_google is None
        or host.provider_check is None
        or host.collecting is None
    ):
        raise SourceArchiveError("capability_unavailable")
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    subjects = tuple(args.subject_id)
    with v4.open_store(store_path, readonly=True) as store:
        scope = authorized_scope(store, session, graph_scope, subjects)
    archive = open_archive(
        archive_path(config.get("source_archive_root"), store_path, config),
        scope,
        config,
        readonly=False,
        provider_check=host.provider_check,
    )
    metadata = decode_metadata(
        parse_json(source_files.read_bytes(Path(args.metadata), archive.limits.manifest_bytes))
    )
    if (
        metadata.scope != scope
        or metadata.task_id != session.task_id
        or metadata.run_id != session.run_id
    ):
        raise SourceArchiveError("scope_denied")
    if (
        metadata.kind not in {"google_doc", "google_sheet", "google_slide"}
        or metadata.capture_method != "structured_observation"
        or metadata.details is None
        or metadata.details.provider != "google"
        or metadata.details.representation != "structured_observation"
        or archive.access is None
    ):
        raise SourceArchiveError("capability_unavailable")
    acquired = sha256(args.acquired_sha256) if args.acquired_sha256 is not None else None
    maximum = archive.limits.for_kind(metadata.kind)
    with host.collecting(session, scope):
        pass
    archive.access.validate_metadata(metadata, "capture")
    if host.prepare_google is not None:
        prepared = host.prepare_google(metadata)
        if (
            prepared.scope != metadata.scope
            or prepared.task_id != metadata.task_id
            or prepared.run_id != metadata.run_id
            or prepared.source_id != metadata.source_id
            or prepared.kind != metadata.kind
            or prepared.capture_method != metadata.capture_method
            or prepared.details is None
            or prepared.details.provider != "google"
            or prepared.details.sensitivity != metadata.details.sensitivity
            or prepared.details.retention != metadata.details.retention
        ):
            raise SourceArchiveError("scope_denied")
        metadata = prepared
    elif acquired is None:
        raise SourceArchiveError("capability_unavailable")
    if acquired is not None:
        archive.access.authorize(metadata, acquired, "capture")
    payload = host.acquire_google(metadata, maximum)
    if type(payload) is not bytes or len(payload) > maximum:
        raise SourceArchiveError("limit_exceeded")
    if acquired is not None and digest(payload) != acquired:
        raise SourceArchiveError("capture_incomplete")
    acquired = digest(payload)
    reading = read_document(payload, metadata.kind, maximum_bytes=maximum)
    bound = bind_document_reading(metadata, payload, reading)
    archive.access.authorize(bound, acquired, "capture")
    # Provider calls and parsing have finished. Serialize a concurrent pause,
    # disable, scope change and archive restriction only for local publication.
    with graph.store_lock(config_path, 5):
        if load_config(config_path)[0] != snapshot:
            raise SourceArchiveError("scope_denied")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as store:
            if authorized_scope(store, session, graph_scope, subjects) != scope:
                raise SourceArchiveError("scope_denied")
            receipt = archive.publish(
                payload=payload,
                metadata=bound,
                units=reading.units,
                publication=host.collecting(session, scope),
            )
    return asdict(receipt)


def owner_view_command(args: argparse.Namespace, host: SourceHost | None) -> dict[str, object]:
    """Dispatch one selected reading to a qualified human-only display, not stdout."""
    if (
        sys.platform
        not in (
            QUALIFIED_PRIVATE_VIEWER_PLATFORMS
            if args.private_viewer
            else QUALIFIED_PREVIEW_PLATFORMS
        )
        or host is None
        or (
            host.display_owner_image is None
            if args.page is not None or args.image or args.document_page is not None
            else host.display_owner is None
        )
    ):
        raise SourceArchiveError("capability_unavailable")
    config_path = Path(args.config)
    snapshot, config = load_config(config_path)
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    subjects = tuple(args.subject_id)
    with v4.open_store(store_path, readonly=True) as store:
        scope = authorized_scope(store, session, graph_scope, subjects)
    access = decode_access_policy(
        config.get("source_owner_view_access_policy"), scope=scope, audience="owner"
    )
    if access is None:
        raise SourceArchiveError("scope_denied")
    access = replace(access, provider_check=host.provider_check)
    archive = Archive.open(
        archive_path(config.get("source_archive_root"), store_path, config),
        scope=scope,
        access=access,
    )
    ref = parse_source_uri(args.source_uri)
    excerpt = archive.read(ref, operation="owner_view", max_chars=args.max_chars)
    if not excerpt.units:
        raise SourceArchiveError("limit_exceeded")
    selected = replace(
        excerpt,
        units=excerpt.units[:1],
        remaining_units=excerpt.remaining_units + len(excerpt.units) - 1,
        next_anchor=excerpt.units[1].anchor_id if len(excerpt.units) > 1 else excerpt.next_anchor,
        later_versions=(),
        later_observations=(),
        remaining_versions=excerpt.remaining_versions + len(excerpt.later_versions),
    )
    heads = {ref: excerpt.history_head}
    png = None
    image_preview = None
    document_result: dict[str, object] = {}
    if args.document_page is not None:
        payload, kind = archive.office_document(ref, operation="owner_view")
        rendered = _office_document_page(archive, args, payload, kind, host)
        png = decoded_png(rendered)
        document_result = {
            "page": args.document_page,
            "page_count": rendered["page_count"],
            "representation": "libreoffice_rendering",
        }
    elif args.office_converter is not None or args.office_work_root is not None:
        raise SourceArchiveError("scope_denied")
    elif args.page is not None:
        if (host.reader is None) and (not args.reader_python or not args.renderer):
            raise SourceArchiveError("capability_unavailable")
        payload, page_ref = archive.pdf_page(ref, args.page, operation="owner_view")
        if page_ref != ref:
            raise SourceArchiveError("scope_denied")
        png = _render_pdf_png(
            payload,
            args.page,
            Path(args.reader_python) if args.reader_python is not None else None,
            Path(args.renderer) if args.renderer is not None else None,
            host=host,
        )
    elif args.image:
        if (host.reader is None and not args.reader_python) or args.renderer is not None:
            raise SourceArchiveError("capability_unavailable")
        _, png, _, image_preview, _ = _saved_image(
            archive,
            ref,
            args.reader_python,
            args.image_rectangle,
            operation="owner_view",
            view=args.image_view,
            svg=_svg_renderer(args),
            av1=_av1_validator(args),
            host=host,
        )
        if image_preview is not None and host.display_owner_rendering is None:
            raise SourceArchiveError("capability_unavailable")
        document_result = _image_geometry(image_preview)
    elif args.reader_python is not None or args.renderer is not None:
        raise SourceArchiveError("scope_denied")
    # All external authorization runs before the short local release reservation.
    archive.verify_refs((ref,), expected_heads=heads, operation="owner_view")
    with graph.store_lock(config_path, 5):
        if load_config(config_path)[0] != snapshot:
            raise SourceArchiveError("scope_denied")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as store:
            if authorized_scope(store, session, graph_scope, subjects) != scope:
                raise SourceArchiveError("scope_denied")
            with archive.hold_verified_refs((ref,), expected_heads=heads):
                if png is None:
                    assert host.display_owner is not None
                    host.display_owner(selected)
                elif image_preview is not None:
                    assert host.display_owner_rendering is not None
                    host.display_owner_rendering(image_preview, png)
                else:
                    assert host.display_owner_image is not None
                    host.display_owner_image(ref, png)
    return {"status": "owner_view_dispatched", "human_only": True, **document_result}


def configure_command(args: argparse.Namespace) -> dict[str, object]:
    """Explicit config → graph reservation → archive mutation; never enable capture."""
    config_path = Path(args.config)
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    owner_view = args.command == "configure-owner-view"
    if owner_view and (args.allow_owner_view != bool(args.sensitivity)):
        raise SourceArchiveError("scope_denied")
    with graph.store_lock(config_path, 5):
        _, config = load_config(config_path)
        store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as store:
            with store.read_snapshot():
                scope = authorized_scope(store, session, graph_scope, tuple(args.subject_id))
                policy_change = owner_view or args.local_documents is not None
                limit_change = any(
                    getattr(args, "max_" + field, None) is not None
                    for field in asdict(ArchiveLimits())
                )
                if policy_change:
                    if limit_change:
                        raise SourceArchiveError("scope_denied")
                    allowed = args.allow_owner_view if owner_view else args.local_documents
                    sensitivities = args.sensitivity if owner_view else ["ordinary"]
                    key = (
                        "source_owner_view_access_policy" if owner_view else "source_access_policy"
                    )
                    policy = (
                        {
                            "format": 1,
                            "scope": asdict(scope),
                            "audience": "owner" if owner_view else "model",
                            "sensitivities": sensitivities,
                            "local_retention": True,
                            "retained_bindings": [],
                        }
                        if allowed
                        else None
                    )
                    decode_access_policy(
                        parse_json(encode(policy)),
                        scope=scope,
                        audience="owner" if owner_view else "model",
                    )
                    graph.atomic_write(
                        config_path,
                        encode({**config, key: policy}).decode("utf-8") + "\n",
                    )
                    if owner_view:
                        return {
                            "status": "owner_view_configured" if allowed else "owner_view_disabled",
                            "route_qualified": False,
                            "collection_started": False,
                        }
                    return {
                        "status": "access_configured",
                        "local_documents": args.local_documents,
                        "collection_started": False,
                    }
                archive = Archive.open(
                    archive_path(config.get("source_archive_root"), store_path, config),
                    scope=scope,
                    readonly=False,
                )
                limits = requested_limits(args, archive.limits)
                archive.configure_limits(limits)
    return {"status": "configured", "limits": asdict(limits), "collection_started": False}


def _preview_destination(policy: PreviewPolicy) -> Path:
    """Check the lexical approved vault before helpers resolve its path."""
    vault = Path(policy.vault_path)
    if not vault.is_absolute() or any(
        is_link_or_reparse_point(parent) for parent in (vault, *vault.parents)
    ):
        raise SourceArchiveError("privacy_rejected")
    source_files.check_entry(vault, directory=True)
    if policy.exposure == "local_only" and not source_files.inspect_store_path(vault)["safe"]:
        raise SourceArchiveError("privacy_rejected")
    graph.ensure_projection_target(vault, policy.root_name)
    return vault


def configure_preview_command(args: argparse.Namespace) -> dict[str, object]:
    """Approve one existing destination without granting model or cloud access.

    Unqualified builds may stage a disabled approval, never publish or launch a
    viewer. Denial and changed approvals retire prior managed copies under the
    existing config → graph → projection lock order before any re-enablement.
    """
    from tedd_knowledge_graph.source_presentation import PreviewPolicy, decode_preview_policy

    if args.allow_previews != bool(args.exposure and args.approve_exposure):
        raise SourceArchiveError("scope_denied")
    if not args.allow_previews and (args.exposure is not None or args.approve_exposure):
        raise SourceArchiveError("scope_denied")
    config_path = Path(args.config)
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    route_qualified = sys.platform in QUALIFIED_PREVIEW_PLATFORMS
    with graph.store_lock(config_path, 5):
        _, config = load_config(config_path)
        store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as store:
            scope = authorized_scope(store, session, graph_scope, tuple(args.subject_id))
            previous = decode_preview_policy(config.get("source_preview_policy"), config, scope)
            owner = decode_access_policy(
                config.get("source_owner_access_policy"), scope=scope, audience="owner"
            )
            changed = dict(config)
            policy: PreviewPolicy | None
            if args.allow_previews:
                policy = PreviewPolicy(
                    scope,
                    text(config.get("vault_path")),
                    text(config.get("root_name")),
                    text(config.get("projection_root_id")),
                    False,
                    args.exposure,
                )
                _preview_destination(policy)
                value = {
                    **asdict(policy),
                    "format": 1,
                    "audience": "owner",
                    "sensitivities": ["ordinary"],
                    "exposure_approved": True,
                }
                changed["source_preview_policy"] = value
                changed["source_owner_access_policy"] = {
                    "format": 1,
                    "scope": asdict(scope),
                    "audience": "owner",
                    "sensitivities": ["ordinary"],
                    "local_retention": True,
                    "retained_bindings": list(owner.retained_bindings) if owner else [],
                }
            else:
                policy = replace(previous, enabled=False) if previous else None
                if previous is not None:
                    value = dict(cast(dict[str, object], config["source_preview_policy"]))
                    changed["source_preview_policy"] = {**value, "enabled": False}
                changed["source_owner_access_policy"] = None
            # Persist refusal before fallible cleanup. An interrupted transition
            # cannot leave a previous approval authorizing new publication.
            graph.atomic_write(config_path, encode(changed).decode("utf-8") + "\n")
            cleanup: dict[str, object] = {}
            if previous is not None and (
                not args.allow_previews
                or not previous.enabled
                or replace(previous, enabled=False) != policy
                or not route_qualified
            ):
                cleanup = _cleanup_owner_preview_files(
                    changed, scope=scope, config_path=config_path
                )
            recovery_result = _preview_recovery_status(config_path)
            if recovery_result:
                if recovery_result.get("recovery_unreadable"):
                    recovery_result["recovery_entries"] = cleanup.get("recovery_entries", [])
                if cleanup.get("recovery_failed"):
                    recovery_result["recovery_status"] = "required"
                cleanup.update(recovery_result)
                if recovery_result["recovery_status"] == "required":
                    cleanup["cleanup_status"] = "cleanup_incomplete"
            enabled = bool(
                args.allow_previews
                and route_qualified
                and cleanup.get("cleanup_status") != "cleanup_incomplete"
            )
            if enabled:
                value = cast(dict[str, object], changed["source_preview_policy"])
                changed["source_preview_policy"] = {**value, "enabled": True}
                graph.atomic_write(config_path, encode(changed).decode("utf-8") + "\n")
    return {
        "status": "owner_preview_configured" if args.allow_previews else "owner_preview_disabled",
        "enabled": enabled,
        "route_qualified": route_qualified,
        "collection_started": False,
        **cleanup,
    }


def preview_command(args: argparse.Namespace) -> dict[str, object]:
    """Publish owner-only derivatives; never return source bodies to the caller."""
    from tedd_knowledge_graph.source_presentation import decode_preview_policy
    from tedd_knowledge_graph.v4_source_evidence import EvidenceRequest, prepare_source_views

    if sys.platform not in QUALIFIED_PREVIEW_PLATFORMS:
        raise SourceArchiveError("capability_unavailable")
    if bool(args.reader_python) != bool(args.renderer):
        raise SourceArchiveError("capability_unavailable")
    config_path = Path(args.config)
    snapshot, config = load_config(config_path)
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    with v4.open_store(store_path, readonly=True) as store:
        scope = authorized_scope(store, session, graph_scope, tuple(args.subject_id))
        policy = decode_preview_policy(config.get("source_preview_policy"), config, scope)
        if policy is None or not policy.enabled:
            raise SourceArchiveError("scope_denied")
        _preview_destination(policy)
        owner = decode_access_policy(
            config.get("source_owner_access_policy"), scope=scope, audience="owner"
        )
        if owner is None or owner.sensitivities != ("ordinary",):
            raise SourceArchiveError("scope_denied")
        archive = Archive.open(
            archive_path(config.get("source_archive_root"), store_path, config),
            scope=scope,
            access=owner,
        )
        prepared = prepare_source_views(
            store,
            archive,
            tuple(
                EvidenceRequest(session, graph_scope, scope.subject_ids, record)
                for record in args.record_id
            ),
            policy,
        )
    return _publish_owner_preview(
        config_path,
        snapshot,
        prepared,
        reader_python=Path(args.reader_python) if args.reader_python else None,
        renderer=Path(args.renderer) if args.renderer else None,
    )


def forget_command(args: argparse.Namespace) -> dict[str, object]:
    """Preview exact shared impact; apply only its explicitly approved digest.

    Config → graph writer → archive is the lock order. Denial survives a failed
    graph transaction. Retrying always requires a fresh preview and authority.
    """
    from tedd_knowledge_graph.v4_source_evidence import forget_inventory

    config_path = Path(args.config)
    session = GraphReadSession(
        args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id)
    ref = parse_source_uri(args.source_uri)
    with graph.store_lock(config_path, 5):
        config_bytes, config = load_config(config_path)
        store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
        with v4.open_store(store_path, readonly=True) as reader, reader.read_snapshot():
            scope = authorized_scope(reader, session, graph_scope, tuple(args.subject_id))
            archive = Archive.open(
                archive_path(config.get("source_archive_root"), store_path, config),
                scope=scope,
                readonly=not args.confirm_forget,
            )
            impact = forget_inventory(reader, archive, ref)
        expected = digest(encode([digest(config_bytes), impact["impact_sha256"]]))
        preview: dict[str, object] = {
            "status": "forget_preview",
            "impact_sha256": expected,
            "graph_evidence": impact["graph_evidence"],
            "retained_memories": impact["retained_memories"],
            "source_versions": len(cast(tuple[str, ...], impact["manifests"])),
            "backups_require_separate_erasure": True,
        }
        if not args.confirm_forget:
            if args.expect_impact is not None:
                raise SourceArchiveError("capture_incomplete")
            return preview
        if args.expect_impact != expected:
            raise SourceArchiveError("capture_incomplete")

        def preflight() -> None:
            # Called with SQLite's writer reserved; only nonlocking config reads.
            with v4.open_store(store_path, readonly=True) as guard, guard.read_snapshot():
                current = authorized_scope(guard, session, graph_scope, tuple(args.subject_id))
                if current.key != scope.key or load_config(config_path)[0] != config_bytes:
                    raise SourceArchiveError("scope_denied")
                latest = forget_inventory(guard, archive, ref)
                if latest["impact_sha256"] != impact["impact_sha256"]:
                    raise SourceArchiveError("capture_incomplete")
                archive.deny(
                    ref,
                    graph_records=tuple(cast(list[str], impact["source_records"])),
                    expected_manifests=cast(tuple[str, ...], impact["manifests"]),
                )

        try:
            preview_bindings, preview_manifests = (
                _preview_bindings(archive, ref, config)
                if config.get("source_preview_policy") is not None
                else (frozenset(), ())
            )
            targets = cast(list[dict[str, object]], impact["graph_evidence"])
            if targets:
                with v4.open_store(store_path, readonly=False) as writer:
                    changes: list[v4.RecordChange] = []
                    for target in targets:
                        record_id = str(target["record_id"])
                        record = writer.get_record(record_id)
                        payload = dict(cast(dict[str, object], record["payload"]))
                        graph.redact_payload(payload, include_identity=True)
                        changes.append(
                            v4.RecordChange(
                                record_id,
                                str(target["collection"]),
                                payload,
                                change_kind="redact",
                                expected_revision=int(cast(int, target["revision"])),
                            )
                        )
                    writer.apply_changes(
                        changes,
                        fingerprint=expected,
                        action="redact",
                        discovery_kind="redaction",
                        producer_agent_id=session.agent_id,
                        task_id=session.task_id,
                        run_id=session.run_id,
                        idempotency_key="source_forget:" + expected,
                        redaction_count=len(changes),
                        preflight=preflight,
                    )
            else:
                with publication_reservation(store_path):
                    preflight()
            preview_cleanup = _cleanup_owner_preview_files(
                config,
                bindings=preview_bindings,
                denied_source=(ref, preview_manifests),
                config_path=config_path,
            )
            if preview_cleanup.get("failed_files") or preview_cleanup.get("preserved_files"):
                return {
                    "status": "forget_incomplete",
                    "fresh_impact_required": True,
                    "source_may_be_denied": True,
                    "preview_cleanup": preview_cleanup,
                }
            receipt = archive.forget(ref)
        except (SourceArchiveError, v4.V4Error, OSError, ValueError):
            # Never describe a rolled-back graph transaction as restored source
            # access: its independently durable denial may already be committed.
            return {
                "status": "forget_incomplete",
                "fresh_impact_required": True,
                "source_may_be_denied": True,
                "graph_cleanup_may_be_pending": True,
            }
        return {
            "status": "forgotten",
            "retained_memories": impact["retained_memories"],
            "graph_evidence_redacted": len(targets),
            "preview_cleanup": preview_cleanup,
            **asdict(receipt),
        }


def _publish_owner_preview(
    config_path: Path,
    snapshot: bytes,
    prepared: PreparedSourceProjection,
    *,
    cleanup: bool = False,
    reader_python: Path | None = None,
    renderer: Path | None = None,
) -> dict[str, object]:
    """Publication seam behind the public command's release-qualification gate.

    Rendering and source access precede locks. Final order is configuration →
    SQLite writer reservation → archive shared reservation → projection lock.
    A changed policy or graph invalidates the prepared job, including when only
    preview policy changed. The response intentionally contains no owner source text.
    """
    import knowledge_graph_v4_integration as integration
    from tedd_knowledge_graph.legacy_projection import LegacyProjectionService
    from tedd_knowledge_graph.source_presentation import decode_preview_policy
    from tedd_knowledge_graph.v4_projection import mask_source_uris
    from tedd_knowledge_graph.v4_source_evidence import (
        active_preview_memories,
        prepare_source_views,
        verify_evidence_state,
    )

    current, config = load_config(config_path)
    if current != snapshot:
        raise SourceArchiveError("scope_denied")
    policy = decode_preview_policy(
        config.get("source_preview_policy"), config, prepared.policy.scope
    )
    expected_policy = replace(prepared.policy, enabled=False) if cleanup else prepared.policy
    if policy is None or policy.enabled == cleanup or policy != expected_policy:
        raise SourceArchiveError("scope_denied")
    if cleanup:
        with graph.store_lock(config_path, 5):
            if load_config(config_path)[0] != snapshot:
                raise SourceArchiveError("scope_denied")
            result = _cleanup_owner_preview_files(config, config_path=config_path)
            return {
                "status": (
                    "owner_preview_cleanup_incomplete"
                    if result.get("cleanup_status") == "cleanup_incomplete"
                    else "owner_preview_cleared"
                ),
                **result,
            }
    _preview_destination(policy)
    store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
    views = prepared.views
    refs = tuple(view.excerpt.ref for view in views)
    heads = {view.excerpt.ref: view.excerpt.history_head for view in views}
    access = decode_access_policy(
        config.get("source_owner_access_policy"), scope=policy.scope, audience="owner"
    )
    if access is None or access.sensitivities != ("ordinary",):
        raise SourceArchiveError("scope_denied")
    archive = Archive.open(
        archive_path(config.get("source_archive_root"), store_path, config),
        scope=policy.scope,
        access=access,
    )
    archive.verify_refs(refs, expected_heads=heads, operation="owner_preview")
    if prepared.retained_views or any(view.image_path is not None for view in views):
        # Caller-supplied filenames never stand in for a trusted worker result.
        raise SourceArchiveError("capability_unavailable")
    binaries: dict[str, bytes] = {}
    base = graph._legacy_projection_service()
    inventory = base.source_preview_inventory(
        Path(policy.vault_path),
        policy.root_name,
        text(config.get("projection_manifest_key")),
        policy.projection_root_id,
    )
    memories_inventory, retained_inventory, retained_binaries = inventory
    archive.verify_refs(memories_inventory[2], operation="owner_preview")
    if memories_inventory[0]:
        with v4.open_store(store_path, readonly=True) as reader:
            verify_evidence_state(
                reader, archive, prepared.request, prepared.state_sha256, refs, heads
            )
            memories = tuple(
                dict.fromkeys(
                    (
                        *active_preview_memories(reader, memories_inventory[0]),
                        *(memory for view in views for memory in view.memory_ids),
                    )
                )
            )
            prepared = prepare_source_views(
                reader,
                archive,
                tuple(replace(prepared.request, record_id=memory) for memory in memories),
                policy,
                retained_views=retained_inventory,
                prepared_views=views,
            )
        views = prepared.views
        refs = (
            *tuple(view.excerpt.ref for view in views),
            *tuple(view.ref for view in prepared.retained_views),
        )
        heads = {
            **{view.excerpt.ref: view.excerpt.history_head for view in views},
            **{view.ref: view.history_head for view in prepared.retained_views},
        }
        binaries.update(
            {
                view.image_path: retained_binaries[view.image_path]
                for view in prepared.retained_views
                if view.image_path is not None
            }
        )
    if reader_python is None and renderer is None and memories_inventory[1]:
        # A repeated exact request may reuse its already verified page too. Never
        # silently downgrade an existing image to text when a refresh cannot
        # establish unchanged history (including pre-history-head legacy notes).
        saved = {view.ref: view for view in retained_inventory if view.image_path is not None}
        reused_images = []
        for view in views:
            prior = saved.get(view.excerpt.ref)
            if prior is not None and prior.history_head == view.excerpt.history_head:
                assert prior.image_path is not None
                binaries[prior.image_path] = retained_binaries[prior.image_path]
                view = replace(view, image_path=prior.image_path)
            elif any(
                unit.anchor_id == view.excerpt.ref.anchor_id
                and unit.kind in ("page", "image_page", "ocr_page")
                for unit in view.excerpt.units
            ):
                raise SourceArchiveError("capability_unavailable")
            reused_images.append(view)
        views = tuple(reused_images)
    if reader_python is not None or renderer is not None:
        if reader_python is None or renderer is None:
            raise SourceArchiveError("capability_unavailable")
        rendered_views = []
        for view in views:
            selected = next(
                unit for unit in view.excerpt.units if unit.anchor_id == view.excerpt.ref.anchor_id
            )
            if selected.kind in ("page", "image_page", "ocr_page"):
                payload_bytes, exact_ref = archive.pdf_page(
                    view.excerpt.ref, selected.position, operation="owner_preview"
                )
                if exact_ref != view.excerpt.ref:
                    raise SourceArchiveError("scope_denied")
                png = _render_pdf_png(payload_bytes, selected.position, reader_python, renderer)
                relative = base.binary_path(
                    text(config.get("projection_manifest_key")),
                    policy.projection_root_id,
                    png,
                    binding=view.binding,
                )
                binaries[relative] = png
                base._validated_binaries(
                    binaries, text(config.get("projection_manifest_key")), policy.projection_root_id
                )
                view = replace(view, image_path=relative)
            rendered_views.append(view)
        views = tuple(rendered_views)
        archive.verify_refs(refs, expected_heads=heads, operation="owner_preview")
    expected_state = prepared.state_sha256

    def check_state(store: v4.KnowledgeGraphV4) -> None:
        verify_evidence_state(store, archive, prepared.request, expected_state, refs, heads)

    with v4.open_store(store_path, readonly=True) as store, store.read_snapshot():
        check_state(store)
        payload, _ = integration._projection_graph(store)
    projection_hash = graph.stable_hash(payload, 64)
    mask_source_uris(payload)
    notes = graph._v3_projection.build_projection_notes(
        payload,
        "review_safe",
        policy.root_name,
        dependencies=graph._projection_dependencies(),
        hooks=graph._projection_build_hooks(),
        source_views=(*views, *prepared.retained_views),
    )

    def build_notes(
        graph: dict[str, object], mode: str, root_name: str, *, skip_validation: bool = False
    ) -> dict[str, str]:
        return dict(notes)

    service = LegacyProjectionService(
        replace(base.dependencies, build_projection_notes=build_notes)
    )
    recovery = _preview_recovery(config_path, config, policy.scope)
    with graph.store_lock(config_path, 5), publication_reservation(store_path):
        if load_config(config_path)[0] != snapshot:
            raise SourceArchiveError("scope_denied")
        _preview_destination(policy)
        with v4.open_store(store_path, readonly=True) as store:
            check_state(store)
        with (
            archive.hold_verified_refs(refs, expected_heads=heads),
            service.projection_lock(Path(policy.vault_path), policy.root_name),
        ):
            _preview_destination(policy)
            if (
                base.source_preview_inventory(
                    Path(policy.vault_path),
                    policy.root_name,
                    text(config.get("projection_manifest_key")),
                    policy.projection_root_id,
                )
                != inventory
            ):
                raise SourceArchiveError("scope_denied")
            try:
                if recovery is not None:
                    recovery_result = recovery.reconcile()
                    if recovery_result["recovery_status"] == "required":
                        return {
                            "status": "owner_preview_cleanup_incomplete",
                            "cleanup_status": "cleanup_incomplete",
                            **recovery_result,
                        }
                result = service._project_graph_locked(
                    payload,
                    Path(policy.vault_path),
                    policy.root_name,
                    "review_safe",
                    text(config.get("projection_manifest_key")),
                    policy.projection_root_id,
                    projection_hash,
                    canonical_store_schema_version=v4.SCHEMA_VERSION,
                    projection_payload_schema_version=graph.GRAPH_SCHEMA_VERSION,
                    binaries=binaries,
                    recovery=recovery,
                )
            except source_files.MutationGuardUnavailable:
                # Translate inside the archive reservation: an unavailable
                # mutation guard is not missing/corrupt source evidence.
                raise SourceArchiveError("capability_unavailable") from None
            except (OSError, ValueError):
                if recovery is not None:
                    return {
                        "status": "owner_preview_cleanup_incomplete",
                        "cleanup_status": "cleanup_incomplete",
                        **recovery.report(),
                    }
                raise
    return {
        "status": "owner_preview_published",
        **{key: result[key] for key in ("written_files", "unchanged_files", "deleted_files")},
        "distributed_copies_may_remain": True,
        **(recovery.report() if recovery is not None else {}),
    }


def _recovery_root(config_path: Path, config: dict[str, object], name: str) -> Path:
    root = Path(os.path.abspath(config_path.parent / name))
    # Config may legitimately sit beside the SQLite file. Exclude the actual
    # graph file, archive and whole configured vault, not their shared parent.
    for field_name in ("store_path", "source_archive_root", "vault_path"):
        value = config.get(field_name)
        if isinstance(value, str) and value and _paths_overlap(root, Path(value).resolve()):
            raise SourceArchiveError("privacy_rejected")
    return root


def _preview_recovery(
    config_path: Path, config: dict[str, object], scope: SourceScope
) -> PreviewRecovery | None:
    if not IS_LINUX:
        return None
    return PreviewRecovery(
        _recovery_root(config_path, config, "source-preview-recovery"),
        text(config.get("projection_manifest_key")),
        digest(encode([scope.key, config.get("projection_root_id")])),
    )


def _preview_recovery_status(config_path: Path | None) -> dict[str, object]:
    if config_path is None or not IS_LINUX:
        return {}
    return recovery_report(config_path.parent / "source-preview-recovery")


def _cleanup_owner_preview_files(
    config: dict[str, object],
    *,
    bindings: frozenset[str] | None = None,
    denied_source: tuple[SourceRef, tuple[str, ...]] | None = None,
    scope: SourceScope | None = None,
    config_path: Path | None = None,
) -> dict[str, object]:
    """Local maintenance under the caller's config lock; never authorize new copies."""
    from tedd_knowledge_graph.source_presentation import decode_preview_policy

    value = config.get("source_preview_policy")
    if value is None:
        retained = _preview_recovery_status(config_path)
        if retained.get("recovery_status") == "required":
            return {
                "deleted_files": 0,
                "preserved_files": 0,
                "failed_files": 1,
                "cleanup_status": "cleanup_incomplete",
                **retained,
            }
        return {"deleted_files": 0, "preserved_files": 0, "failed_files": 0}
    recovery = None
    try:
        if not isinstance(value, dict):
            raise SourceArchiveError("scope_denied")
        policy = decode_preview_policy(value, config, scope or decode_scope(value.get("scope")))
        assert policy is not None
        if config_path is not None:
            recovery = _preview_recovery(config_path, config, policy.scope)
        _preview_destination(policy)
        service = graph._legacy_projection_service()
        with service.projection_lock(Path(policy.vault_path), policy.root_name):
            _preview_destination(policy)
            if denied_source is not None:
                ref, manifests = denied_source
                # Presentation recovery is fallible and must follow durable
                # denial. Only already-authorized immutable identities enter
                # this error boundary; no archive/authority calls belong here.
                recovered = service.source_preview_cleanup_refs(
                    Path(policy.vault_path),
                    policy.root_name,
                    text(config.get("projection_manifest_key")),
                    policy.projection_root_id,
                )
                bindings = frozenset(bindings or ()) | frozenset(
                    digest(candidate.as_uri().encode("utf-8"))[:32]
                    for candidate in recovered
                    if candidate.archive_id == ref.archive_id
                    and candidate.scope_id == ref.scope_id
                    and candidate.manifest_sha256 in manifests
                )
            return service.clear_source_previews_locked(
                Path(policy.vault_path),
                policy.root_name,
                text(config.get("projection_manifest_key")),
                policy.projection_root_id,
                bindings=bindings,
                recovery=recovery,
            )
    except (OSError, ValueError, SourceArchiveError):
        return {
            "deleted_files": 0,
            "preserved_files": 0,
            "failed_files": 1,
            "cleanup_status": "cleanup_incomplete",
            "distributed_copies_may_remain": True,
            **(
                recovery.report() if recovery is not None else _preview_recovery_status(config_path)
            ),
        }


def _preview_bindings(
    archive: Archive, ref: SourceRef, config: dict[str, object]
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Validate authoritative cleanup identity without reading presentation files."""
    from tedd_knowledge_graph.source_presentation import decode_preview_policy

    bindings = _source_preview_bindings(archive, ref)
    value = config.get("source_preview_policy")
    if not isinstance(value, dict):
        raise SourceArchiveError("scope_denied")
    policy = decode_preview_policy(value, config, decode_scope(value.get("scope")))
    assert policy is not None
    return bindings


def _source_preview_bindings(
    archive: Archive, ref: SourceRef
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Content-free authoritative identity, independent of fallible presentation."""
    manifests, _ = archive.forget_identity(ref)
    refs = {ref}
    for key in manifests:
        try:
            manifest = archive._manifest(key)
        except FileNotFoundError:
            # Durable denial identity survives partial or completed erasure.
            continue
        refs.update(archive._ref(key, anchor) for anchor in manifest.anchor_ids)
    return frozenset(
        digest(candidate.as_uri().encode("utf-8"))[:32] for candidate in refs
    ), manifests


def _restrict_owner_preview(
    config_path: Path, snapshot: bytes, prepared: PreparedSourceProjection, ref: SourceRef
) -> dict[str, object]:
    """Internal local denial/cleanup. No live-provider qualification or graph mutation."""
    if ref not in {view.excerpt.ref for view in prepared.views}:
        raise SourceArchiveError("scope_denied")
    return _restrict_source(
        config_path,
        prepared.request.session,
        prepared.request.scope,
        prepared.request.subject_ids,
        ref,
        snapshot=snapshot,
        expected_scope=prepared.policy.scope,
    )


def _restrict_source(
    config_path: Path,
    session: GraphReadSession,
    graph_scope: GraphReadScope,
    subject_ids: tuple[str, ...],
    ref: SourceRef,
    *,
    snapshot: bytes | None = None,
    expected_scope: SourceScope | None = None,
) -> dict[str, object]:
    """Config → store reservation → archive denial → targeted preview cleanup.

    No provider or source-body reads are required to reduce access. Invalid
    authority fails before writes; presentation failure never reverses denial.
    """
    with graph.store_lock(config_path, 5):
        current, config = load_config(config_path)
        if snapshot is not None and current != snapshot:
            raise SourceArchiveError("scope_denied")
        store_path = graph.secure_state_path(config.get("store_path"), "Canonical store")
        with publication_reservation(store_path), v4.open_store(store_path, readonly=True) as store:
            with store.read_snapshot():
                scope = authorized_scope(store, session, graph_scope, subject_ids)
                if expected_scope is not None and scope != expected_scope:
                    raise SourceArchiveError("scope_denied")
                archive = Archive.open(
                    archive_path(config.get("source_archive_root"), store_path, config),
                    scope=scope,
                    readonly=False,
                )
                bindings, manifests = _source_preview_bindings(archive, ref)
                try:
                    disposition = archive.restrict(ref, expected_manifests=manifests)
                except (SourceArchiveError, OSError):
                    # Marker publication can succeed before expansion fails.
                    # Do not clean copies or imply the denial was rolled back.
                    return {
                        "status": "restriction_incomplete",
                        "source_may_be_denied": True,
                        "cleanup_status": "cleanup_incomplete",
                        "distributed_copies_may_remain": True,
                    }
            return {
                "status": "source_restricted",
                "history_disposition": disposition,
                "distributed_copies_may_remain": True,
                **_cleanup_owner_preview_files(
                    config,
                    bindings=bindings,
                    denied_source=(ref, manifests),
                    scope=scope,
                    config_path=config_path,
                ),
            }


def evidence_command(
    args: argparse.Namespace, store: object, canonical_path: Path, *, host: SourceHost | None = None
) -> dict[str, object]:
    """CLI-only composition; no operator-full switch or arbitrary archive path."""
    from tedd_knowledge_graph.v4_source_evidence import EvidenceRequest, gather

    if not isinstance(store, v4.KnowledgeGraphV4) or not store.readonly:
        raise V4CLIError("scope_denied")
    try:
        maximum = args.max_chars
        if type(maximum) is not int or not 200 < maximum <= 48_000:
            raise SourceArchiveError("limit_exceeded")
        request = EvidenceRequest(
            GraphReadSession(
                args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
            ),
            GraphReadScope(ReadScopeKind.SUBSCRIPTION, args.subscription_id),
            tuple(args.subject_id),
            args.id,
            args.revision,
            maximum - 100,
            args.max_versions,
        )
        config_bytes, config = load_config(Path(args.config))
        if Path(str(config.get("store_path"))).resolve() != canonical_path.resolve():
            raise SourceArchiveError("scope_denied")
        with store.read_snapshot():
            scope = authorized_scope(store, request.session, request.scope, request.subject_ids)
            root = archive_path(config.get("source_archive_root"), canonical_path, config)
            archive = open_archive(
                root,
                scope,
                config,
                provider_check=host.provider_check if host is not None else None,
            )

        def retain_selection(
            state: Mapping[str, object], refs: tuple[SourceRef, ...], heads: Mapping[SourceRef, str]
        ) -> None:
            if host is not None and host.retain_delivery is not None:
                host.retain_delivery(
                    partial(
                        _read_reservation,
                        Path(args.config),
                        config_bytes,
                        canonical_path,
                        archive,
                        request.session,
                        request.scope,
                        request.subject_ids,
                        dict(state),
                        refs,
                        dict(heads),
                    )
                )

        result = gather(store, archive, request, retain_selection=retain_selection)
        if load_config(Path(args.config))[0] != config_bytes:
            raise SourceArchiveError("scope_denied")
        return result
    except SourceArchiveError as error:
        if host is not None:
            raise
        raise V4CLIError(error.code) from None
    except (KeyError, OSError, ValueError):
        if host is not None:
            raise SourceArchiveError("source_unavailable") from None
        raise V4CLIError("source_unavailable") from None


def _validate_capture_binding(
    store: object,
    packet: Mapping[str, object],
    request: CaptureRequest,
    *,
    changes: Sequence[Mapping[str, object]] = (),
    provider_check: ProviderCheck | None = None,
    expected_config: bytes | None = None,
    local_only: bool = False,
    check_retention: Callable[[SourceMetadata], None] | None = None,
) -> bytes:
    """Check durable bindings without holding an archive lock during a graph write."""
    from tedd_knowledge_graph.v4_source_evidence import _mapping, _within

    if not isinstance(store, v4.KnowledgeGraphV4) or request.source_config_path is None:
        raise SourceArchiveError("scope_denied")
    snapshot, config = load_config(request.source_config_path)
    if (expected_config is not None and expected_config != snapshot) or (
        local_only and expected_config is None
    ):
        raise SourceArchiveError("scope_denied")
    if Path(str(config.get("store_path"))).resolve() != store.path.resolve():
        raise SourceArchiveError("scope_denied")
    session = GraphReadSession(
        request.envelope.producer_agent_id,
        request.envelope.task_id,
        request.envelope.producer_run_id,
        request.consumer_id,
    )
    graph_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, request.source_subscription_id)
    with v4.open_store(store.path, readonly=True) as reader:
        scope = authorized_scope(reader, session, graph_scope, request.source_subject_ids)
        root = archive_path(config.get("source_archive_root"), store.path, config)
        uri = _mapping(packet.get("source")).get("uri")
        if not isinstance(uri, str):
            raise SourceArchiveError()
        ref = parse_source_uri(uri)
        archive = open_archive(root, scope, config, provider_check=provider_check)
        if local_only:
            # The request-local external check authorized this immutable packet
            # under these exact config bytes; the graph writer blocks scope edits.
            archive._verify_refs_local((ref,))
        else:
            archive.verify_refs((ref,))
        if check_retention is not None:
            check_retention(archive._manifest(ref.manifest_sha256).metadata)
        for change in changes:
            if change.get("collection") not in ("sources", "journal"):
                _within({**change, "payload_available": True}, scope)
        if load_config(request.source_config_path)[0] != snapshot:
            raise SourceArchiveError("scope_denied")
    return snapshot


def validate_capture_binding(
    store: object,
    packet: Mapping[str, object],
    request: CaptureRequest,
    *,
    changes: Sequence[Mapping[str, object]] = (),
    provider_check: ProviderCheck | None = None,
    expected_config: bytes | None = None,
    local_only: bool = False,
    check_retention: Callable[[SourceMetadata], None] | None = None,
) -> bytes:
    """Keep local configuration/path exceptions out of public capture errors."""
    try:
        return _validate_capture_binding(
            store,
            packet,
            request,
            changes=changes,
            provider_check=provider_check,
            expected_config=expected_config,
            local_only=local_only,
            check_retention=check_retention,
        )
    except SourceArchiveError:
        raise
    except (KeyError, OSError, ValueError):
        raise SourceArchiveError("source_unavailable") from None


def execute_cli(args: argparse.Namespace) -> dict[str, object]:
    """Set up an explicit local capability before the command takes release locks."""
    if args.command in {"disconnect-google", "reconnect-google"}:
        from tedd_knowledge_graph.source_credentials import CredentialStore
        from tedd_knowledge_graph.source_mcp import SourceTurn
        from tedd_sources_mcp import SourceOperations

        with closing(
            SourceOperations(
                Path(args.config),
                args.google_client,
                args.google_account,
                credentials=CredentialStore(args.google_credentials),
            )
        ) as operations:
            return dict(
                operations.control(
                    args.command.replace("-", "_"),
                    sha256(args.operation_id) if args.operation_id else secrets.token_hex(32),
                    SourceTurn(),
                )
            )
    if args.command == "owner-view" and args.private_viewer:
        if sys.platform not in QUALIFIED_PRIVATE_VIEWER_PLATFORMS:
            raise SourceArchiveError("capability_unavailable")
        from tedd_knowledge_graph.source_viewer import PrivateViewer

        with closing(PrivateViewer()) as viewer:
            return execute(
                args,
                host=SourceHost(
                    display_owner=viewer.display,
                    display_owner_image=viewer.display_image,
                    display_owner_rendering=viewer.display_rendering,
                ),
            )
    return execute(args)


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute_cli(parser().parse_args(argv))
    except MacOSWorkerUnavailable as error:
        result = error.response()
        status = 1
    except SourceArchiveError as error:
        result = {"status": error.code}
        status = 1
    except (OSError, v4.V4Error, ValueError):
        result = {"status": "source_unavailable"}
        status = 1
    else:
        status = (
            1
            if (
                result.get("status") in {"forget_incomplete", "google_disconnect_incomplete"}
                or result.get("cleanup_status") == "cleanup_incomplete"
            )
            else 0
        )
    sys.stdout.buffer.write(encode(result) + b"\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
