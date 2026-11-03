#!/usr/bin/env python3
"""Explicit source endpoint. Requires reviewed Codex begin/end lifecycle hooks.

This entry point does not install hooks, enable capture, or change graph policy.
Optional --google-client and --google-account configure the separate Google API
route. --google-client is a desktop app configuration, not a connector token.
Optional --google-credentials enables explicitly approved OS-protected reuse.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from pathlib import Path
from typing import cast

from tedd_knowledge_graph.source_contracts import (
    MANIFEST_BYTES,
    SourceArchiveError,
    SourceExcerpt,
    SourceMetadata,
    SourceRef,
    SourceScope,
    encode,
    parse_json,
)
from tedd_knowledge_graph.source_credentials import CredentialStore
from tedd_knowledge_graph.source_google import GoogleSourceSession
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.source_mcp import SOURCE_COMMANDS, SourceMcpServer, SourceTurn
from tedd_knowledge_graph.source_reader_cold import load_mac_cold_entitlement
from tedd_knowledge_graph.source_reader_contracts import SourceReader
from tedd_knowledge_graph.source_reader_host import ReaderCoordinator
from tedd_knowledge_graph.source_reader_runtime import load_mac_runtime_binding
from tedd_knowledge_graph.source_review import ReviewRendering, SourceProgress
from tedd_knowledge_graph.v4_application_contracts import GraphReadSession, JsonObject
from tedd_knowledge_graph.v4_mcp_contracts import MAX_RESPONSE_BYTES
from tedd_knowledge_graph.v4_mcp_stdio import serve, stdio_streams


def load_mac_reader(path: Path) -> ReaderCoordinator:
    """Build the source reader from one already-enrolled private runtime."""

    binding = load_mac_runtime_binding(path)
    return ReaderCoordinator(binding.runtime, binding=binding)


def load_mac_cold_reader(path: Path) -> ReaderCoordinator:
    """Build the source reader from one reviewed cold entitlement."""

    cold = load_mac_cold_entitlement(path)
    return ReaderCoordinator(cold.runtime, cold=cold, profile_owners=cold.profile_authority())


class SourceOperations:
    """Adapt existing commands; configuration and Google account are host-owned.

    The endpoint serializes operations, not lifecycle notifications. Only one
    selected-file session is retained; replacing it discards the previous token.
    No provider request or interactive consent runs inside a publication lock.
    """

    def __init__(
        self,
        config: Path,
        client: Path | None = None,
        account: str | None = None,
        *,
        reader_python: Path | None = None,
        renderer: Path | None = None,
        svg_renderer: Path | None = None,
        svg_font: Path | None = None,
        av1_validator: Path | None = None,
        office_converter: Path | None = None,
        office_work_root: Path | None = None,
        credentials: CredentialStore | None = None,
        browser_export_root: Path | None = None,
        reader: SourceReader | None = None,
    ) -> None:
        if (client is None) != (account is None) or (credentials is not None and client is None):
            raise SourceArchiveError("capability_unavailable")
        self._config, self._client, self._account = config, client, account
        self._reader = reader
        self._reader_python, self._renderer = reader_python, renderer
        if (svg_renderer is None) != (svg_font is None) or (
            svg_renderer is not None and reader_python is None
        ):
            raise SourceArchiveError("capability_unavailable")
        self._svg_renderer, self._svg_font = svg_renderer, svg_font
        if av1_validator is not None and reader_python is None:
            raise SourceArchiveError("capability_unavailable")
        self._av1_validator = av1_validator
        if (office_converter is None) != (office_work_root is None) or (
            office_converter is not None and (reader_python is None or renderer is None)
        ):
            raise SourceArchiveError("capability_unavailable")
        self._office_converter, self._office_work_root = office_converter, office_work_root
        self._google: GoogleSourceSession | None = None
        self._credentials = credentials
        self._google_turn: SourceTurn | None = None
        if browser_export_root is not None and (
            not browser_export_root.is_absolute() or (reader_python is None and reader is None)
        ):
            raise SourceArchiveError("capability_unavailable")
        self._browser_export_root = browser_export_root

    def close(self) -> None:
        if self._google is not None:
            self._google.disconnect()
            self._google = None
        self._google_turn = None

    def _credential_root(self) -> CredentialStore:
        """Reuse one privacy check for sign-in and connection-level controls."""
        import tedd_sources

        credentials = self._credentials
        if credentials is None:
            raise SourceArchiveError("capability_unavailable")
        _, config = tedd_sources.load_config(self._config)
        store_path = tedd_sources.graph.secure_state_path(
            config.get("store_path"), "Canonical store"
        )
        root = tedd_sources.archive_path(str(credentials.root), store_path, config)
        if config.get("source_archive_root") is not None:
            archive = tedd_sources.archive_path(
                config.get("source_archive_root"), store_path, config
            )
            if tedd_sources._paths_overlap(root, archive):
                raise SourceArchiveError("privacy_rejected")
        return credentials

    def control(self, action: str, operation_id: str, turn: SourceTurn) -> JsonObject:
        from tedd_knowledge_graph import source_files, source_google_oauth
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import disconnect_google, reconnect_google
        from tedd_knowledge_graph.source_viewer import PrivateViewer

        if (
            self._client is None
            or self._account is None
            or action not in {"disconnect_google", "reconnect_google"}
        ):
            raise SourceArchiveError("capability_unavailable")
        client = source_google_oauth.decode_client(
            parse_json(source_files.read_bytes(self._client, 65536))
        )
        connection = GoogleConnection(
            self._credential_root(), source_google_oauth.credential_key(client, self._account)
        )

        def confirm(description: str, purpose: str) -> bool:
            with closing(PrivateViewer(confirmation=True)) as viewer:
                return viewer.confirm(description, turn.cancelled, action=purpose)

        if action == "disconnect_google":
            result = disconnect_google(
                connection, self._account, client.client_id, operation_id, turn.cancelled, confirm
            )
            if result.get("local_access_disabled") is True:
                self.close()
            return result
        return reconnect_google(
            connection, self._account, client.client_id, turn.cancelled, confirm
        )

    def __call__(self, command: str, argv: list[str], turn: SourceTurn) -> JsonObject:
        import knowledge_graph_v4 as v4
        import knowledge_graph_v4_capture as graph_capture
        import knowledge_graph_v4_cli as graph_cli
        import tedd_sources
        from tedd_knowledge_graph import source_google_oauth
        from tedd_knowledge_graph.source_viewer import PrivateViewer

        turn.local_delivery_hold = None

        if command not in SOURCE_COMMANDS or any(arg in {"--help", "-h"} for arg in argv):
            raise SourceArchiveError("capability_unavailable")
        command_parser = (
            tedd_sources.graph.build_parser()
            if command in {"capture", "source-evidence"}
            else tedd_sources.parser()
        )
        args = command_parser.parse_args(["--config", str(self._config), command, *argv])
        if command in ("intake-file", "render-image", "owner-view"):
            if (
                args.svg_renderer is not None
                or args.svg_font is not None
                or args.av1_validator is not None
            ):
                raise SourceArchiveError("scope_denied")
            args.svg_renderer = str(self._svg_renderer) if self._svg_renderer is not None else None
            args.svg_font = str(self._svg_font) if self._svg_font is not None else None
            args.av1_validator = (
                str(self._av1_validator) if self._av1_validator is not None else None
            )
        if command in ("intake-file", "owner-view", "render-document"):
            if args.office_converter is not None or args.office_work_root is not None:
                raise SourceArchiveError("scope_denied")
            if args.document_page is not None:
                if self._reader is None and (
                    self._office_converter is None or self._office_work_root is None
                ):
                    raise SourceArchiveError("capability_unavailable")
                args.office_converter = (
                    str(self._office_converter) if self._office_converter is not None else None
                )
                args.office_work_root = (
                    str(self._office_work_root) if self._office_work_root is not None else None
                )
            if command in ("intake-file", "render-document"):
                if args.renderer is not None:
                    raise SourceArchiveError("scope_denied")
                if args.document_page is not None:
                    args.renderer = str(self._renderer)
        if command in ("intake-file", "render-image", "render-document", "import-exports"):
            # A model cannot select an executable or substitute its workflow state.
            if args.reader_python is not None:
                raise SourceArchiveError("scope_denied")
            args.reader_python = str(self._reader_python) if self._reader_python else None
        if command == "intake-file":
            args.workflow_state = "active"
            turn.authorize_local(Path(args.input))
        if command in {"capture-google", "list-google"} and self._client is None:
            raise SourceArchiveError("capability_unavailable")
        if command == "owner-view":
            if args.reader_python is not None or args.renderer is not None:
                raise SourceArchiveError("scope_denied")
            if args.page is not None or args.document_page is not None:
                if self._reader is None and (self._reader_python is None or self._renderer is None):
                    raise SourceArchiveError("capability_unavailable")
                args.reader_python = (
                    str(self._reader_python) if self._reader_python is not None else None
                )
                args.renderer = str(self._renderer) if self._renderer is not None else None
            elif args.image:
                if self._reader is None and self._reader_python is None:
                    raise SourceArchiveError("capability_unavailable")
                args.reader_python = (
                    str(self._reader_python) if self._reader_python is not None else None
                )
        expected_session = GraphReadSession(
            args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
        )

        def active() -> None:
            if turn.cancelled.is_set():
                raise SourceArchiveError("scope_denied")

        def google_session(scope: SourceScope, resource: str) -> GoogleSourceSession:
            active()
            # An operation that already used provider bytes keeps that generation
            # through publication and viewing. Recovery authorizes new work, not
            # a paused old operation or a fresh sign-in on its behalf.
            if turn.provider_hold is not None:
                with turn.provider_hold():
                    pass
            client, account = self._client, self._account
            if client is None or account is None:
                raise SourceArchiveError("capability_unavailable")
            persistent = self._credentials is not None
            if persistent and not (turn.google_files or turn.google_folders):
                raise SourceArchiveError("scope_denied")
            if self._credentials is not None:
                self._credential_root()
            session = self._google
            if (
                session is None
                or session.scope != scope
                or (not session.folder_ids and resource not in session.selected_ids)
                or (
                    persistent
                    and (
                        self._google_turn is not turn
                        or session.selected_ids != turn.google_files
                        or session.folder_ids != turn.google_folders
                    )
                )
                or session.expires_at <= time.monotonic()
            ):
                if not persistent and command in {"lookup", "capture", "source-evidence"}:
                    raise SourceArchiveError("scope_denied")
                self.close()
                session = source_google_oauth.sign_in(
                    client,
                    account=account,
                    scope=scope,
                    cancelled=turn.cancelled,
                    credentials=self._credentials,
                    selected_ids=turn.google_files if persistent else frozenset(),
                    folder_ids=turn.google_folders if persistent else frozenset(),
                )
                self._google = session
                self._google_turn = turn
            active()
            with session.hold():
                pass
            if turn.provider_hold is None:
                turn.provider_hold = session.hold
            # Recheck after any unlocked sign-in/refresh, but never replace the
            # first fence with the replacement session's newer generation.
            with turn.provider_hold():
                pass
            if not session.folder_ids and resource not in session.selected_ids:
                self.close()
                raise SourceArchiveError("scope_denied")
            return session

        def google(source: SourceMetadata) -> GoogleSourceSession:
            if command in {"capture-google", "capture"}:
                turn.check_retention(source)
            return google_session(source.scope, source.source_id)

        def list_google(scope: SourceScope, folder: str, page: str) -> dict[str, object]:
            if self._credentials is None or not turn.google_folders:
                raise SourceArchiveError("scope_denied")
            result = google_session(scope, folder).list_folder(folder, page)
            active()
            return result

        @contextmanager
        def collecting(session: GraphReadSession, scope: SourceScope) -> Iterator[None]:
            subjects = args.source_subject_id if command == "capture" else args.subject_id
            if session != expected_session or set(scope.subject_ids) != set(subjects):
                raise SourceArchiveError("scope_denied")
            with turn.delivery():
                yield

        def acquire(source: SourceMetadata, maximum: int) -> bytes:
            payload = google(source).acquire(source, maximum)
            active()
            return payload

        active()
        try:
            with ExitStack() as stack:
                viewer = None
                if command == "owner-view":
                    if not args.private_viewer:
                        raise SourceArchiveError("capability_unavailable")
                    viewer = PrivateViewer()
                    stack.callback(viewer.close)

                def display(excerpt: SourceExcerpt) -> None:
                    assert viewer is not None
                    with turn.delivery():
                        viewer.display(excerpt)

                def display_image(ref: SourceRef, png: bytes) -> None:
                    assert viewer is not None
                    with turn.delivery():
                        viewer.display_image(ref, png)

                def display_rendering(rendering: ReviewRendering, png: bytes) -> None:
                    assert viewer is not None
                    with turn.delivery():
                        viewer.display_rendering(rendering, png)

                def confirm_exports(description: str) -> bool:
                    active()
                    with ExitStack() as confirmation_stack:
                        confirmation = PrivateViewer(confirmation=True)
                        confirmation_stack.callback(confirmation.close)
                        return confirmation.confirm(description, turn.cancelled, exports=True)

                def review_owner_image(
                    rendering: ReviewRendering, png: bytes, handoff: AbstractContextManager[None]
                ) -> bool:
                    active()
                    with closing(PrivateViewer(confirmation=True)) as confirmation:
                        return confirmation.review_image(
                            rendering, png, turn.cancelled, handoff=handoff
                        )

                def retain_delivery(
                    reservation: Callable[[], AbstractContextManager[None]],
                ) -> None:
                    @contextmanager
                    def local_release() -> Iterator[None]:
                        with ExitStack() as ownership:
                            try:
                                ownership.enter_context(reservation())
                            except SourceArchiveError:
                                raise
                            except (OSError, ValueError, v4.V4Error):
                                raise SourceArchiveError("source_unavailable") from None
                            # Transport exceptions propagate to the actual sender;
                            # do not turn a partial write into a second response.
                            yield

                    turn.local_delivery_hold = local_release

                host = SourceHost(
                    reader=self._reader,
                    cancelled=turn.cancelled,
                    reviews=turn.reviews,
                    review_owner_image=review_owner_image,
                    authorize_local=turn.authorize_local,
                    export_root=self._browser_export_root,
                    confirm_exports=confirm_exports if self._browser_export_root else None,
                    check_retention=turn.check_retention,
                    list_google=list_google,
                    prepare_google=lambda source: google(source).prepare(source),
                    acquire_google=acquire,
                    provider_check=lambda source, checksum, operation, audience: google(
                        source
                    ).check(source, checksum, operation, audience),
                    collecting=collecting,
                    retain_delivery=retain_delivery,
                    display_owner=display if viewer else None,
                    display_owner_image=display_image if viewer else None,
                    display_owner_rendering=display_rendering if viewer else None,
                )
                if command in {"capture", "source-evidence"}:
                    _, config = tedd_sources.load_config(self._config)
                    store_path = tedd_sources.graph.secure_state_path(
                        config.get("store_path"), "Canonical store"
                    )
                    if command == "source-evidence":
                        with v4.open_store(store_path, readonly=True) as store:
                            result = tedd_sources.evidence_command(
                                args, store, store_path, host=host
                            )
                    else:

                        def capture(
                            store: object, request: graph_capture.CaptureRequest
                        ) -> dict[str, object]:
                            from tedd_knowledge_graph.source_contracts import parse_source_uri

                            if (
                                request.input_bytes is None
                                or request.source_subscription_id is None
                            ):
                                raise SourceArchiveError("scope_denied")
                            packet = parse_json(request.input_bytes)
                            source = packet.get("source") if isinstance(packet, dict) else None
                            if not isinstance(source, dict):
                                raise SourceArchiveError("scope_denied")
                            uri = source.get("uri")
                            if not isinstance(uri, str):
                                raise SourceArchiveError("scope_denied")
                            reference = parse_source_uri(uri)
                            active()
                            result = graph_capture.execute_capture(store, request, source_host=host)
                            if result.get("status") == "captured" and isinstance(
                                store, v4.KnowledgeGraphV4
                            ):
                                changed = result.get("changed_ids")
                                if isinstance(changed, list):
                                    bound = [
                                        SourceProgress(
                                            "bound",
                                            ref=reference,
                                            memory_id=str(record["record_id"]),
                                        )
                                        for record in store.get_records(
                                            str(item) for item in changed
                                        )
                                        if record.get("collection") == "claims"
                                    ]
                                    if bound:
                                        result = {
                                            **result,
                                            "stage": "bound",
                                            "bound_memories": [item.as_dict() for item in bound],
                                        }
                            return result

                        # The original dry-run/fingerprint/apply contract and review
                        # sanitizer are retained; this is not a general graph-write tool.
                        from knowledge_graph_v4_integration import sanitize_review_safe_value

                        if args.mode != "review_safe" or args.input == "-":
                            raise SourceArchiveError("scope_denied")
                        response = graph_cli.handle_v4_command(
                            args,
                            db_path=store_path,
                            capture_handler=capture,
                            review_sanitizer=sanitize_review_safe_value,
                        )
                        if response is None:
                            raise SourceArchiveError("capability_unavailable")
                        result = dict(response)
                else:
                    result = tedd_sources.execute(args, host=host)
                active()
                # The existing CLI's serializer checks data shapes; MCP additionally
                # bounds the final JSON-RPC response. No arbitrary objects escape.
                return cast(
                    JsonObject,
                    parse_json(
                        encode(result),
                        maximum_bytes=MAX_RESPONSE_BYTES
                        if "png_base64" in result
                        else MANIFEST_BYTES,
                    ),
                )
        finally:
            if turn.cancelled.is_set():
                self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--google-client", type=Path)
    parser.add_argument("--google-account")
    parser.add_argument(
        "--google-credentials",
        type=Path,
        help="Existing private credential root; enables approved read-only reuse",
    )
    parser.add_argument("--reader-python", type=Path, help="Qualified host-owned document reader")
    parser.add_argument(
        "--browser-export-root",
        type=Path,
        help="Approved ordinary-only export root; imports require private owner confirmation",
    )
    parser.add_argument("--renderer", type=Path, help="Qualified host-owned PDF renderer")
    parser.add_argument("--svg-renderer", type=Path, help="Qualified private resvg executable")
    parser.add_argument("--svg-font", type=Path, help="Qualified fixed SVG font")
    parser.add_argument("--av1-validator", type=Path, help="Qualified private strict AV1 helper")
    parser.add_argument(
        "--office-converter", type=Path, help="Approved isolated LibreOffice executable"
    )
    parser.add_argument(
        "--office-work-root", type=Path, help="Existing private disposable-conversion root"
    )
    parser.add_argument(
        "--mac-reader-runtime-config",
        type=Path,
        help="Existing private enrollment for the supported warm Mac reader runtime",
    )
    parser.add_argument(
        "--mac-reader-cold-config",
        type=Path,
        help="Existing private entitlement for the dedicated cold Mac reader runtime",
    )
    args = parser.parse_args(argv)
    if (args.google_client is None) != (args.google_account is None) or (
        args.google_credentials is not None and args.google_client is None
    ):
        parser.error("Google API access requires both --google-client and --google-account")
    if args.renderer is not None and args.reader_python is None:
        parser.error("PDF viewing requires both host-owned reader and renderer")
    legacy_reader_options = (
        args.reader_python,
        args.renderer,
        args.svg_renderer,
        args.svg_font,
        args.av1_validator,
        args.office_converter,
        args.office_work_root,
    )
    if args.mac_reader_runtime_config is not None and args.mac_reader_cold_config is not None:
        parser.error("Warm and cold Mac reader configurations are mutually exclusive")
    if (
        args.mac_reader_runtime_config is not None or args.mac_reader_cold_config is not None
    ) and any(option is not None for option in legacy_reader_options):
        parser.error("Mac reader configuration cannot be combined with legacy readers")
    reader: SourceReader | None = None
    if args.mac_reader_runtime_config is not None:
        try:
            reader = load_mac_reader(args.mac_reader_runtime_config)
        except SourceArchiveError:
            parser.error("Mac reader runtime configuration is invalid")
    elif args.mac_reader_cold_config is not None:
        try:
            reader = load_mac_cold_reader(args.mac_reader_cold_config)
        except SourceArchiveError:
            parser.error("Mac reader cold configuration is invalid")
    operation = SourceOperations(
        args.config,
        args.google_client,
        args.google_account,
        reader_python=args.reader_python,
        browser_export_root=args.browser_export_root,
        renderer=args.renderer,
        svg_renderer=args.svg_renderer,
        svg_font=args.svg_font,
        av1_validator=args.av1_validator,
        office_converter=args.office_converter,
        office_work_root=args.office_work_root,
        credentials=CredentialStore(args.google_credentials) if args.google_credentials else None,
        reader=reader,
    )
    try:
        return serve(SourceMcpServer(operation, control=operation.control), *stdio_streams())
    finally:
        operation.close()


if __name__ == "__main__":
    raise SystemExit(main())
