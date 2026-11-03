"""Explicit source operations bound to Codex's actual in-memory turn lifecycle.

The qualified stdio host supplies transport metadata separately from tool input.
Only lifecycle-hook calls (not model tool calls) may begin a turn. No prompts,
messages, event ledger or independent workflow database are stored here.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .source_contracts import (
    SourceArchiveError,
    SourceMetadata,
    decoded_png,
    digest,
    encode,
    identifier,
)
from .source_macos_host import MacOSWorkerUnavailable
from .source_review import ReviewBook, SourceProgress
from .v4_application_contracts import JsonObject, JsonValue
from .v4_mcp_contracts import (
    METHOD_NOT_FOUND,
    McpProtocolError,
    decode_request_line,
    encode_response,
    result_message,
)
from .v4_mcp_stdio import McpLifecycle


@dataclass(repr=False)
class SourceTurn:
    cancelled: threading.Event = field(default_factory=threading.Event)
    publication: threading.RLock = field(default_factory=threading.RLock)
    google_files: frozenset[str] = frozenset()
    google_folders: frozenset[str] = frozenset()
    local_files: frozenset[str] = frozenset()
    local_folders: frozenset[str] = frozenset()
    local_denials: frozenset[str] = frozenset()
    google_denials: frozenset[str] = frozenset()
    google_denied_folders: frozenset[str] = frozenset()
    reviews: ReviewBook = field(default_factory=ReviewBook)
    # Set only by the active host operation when it uses provider-backed data.
    # Ordinary operations are serialized; the separate control never resets it.
    provider_hold: Callable[[], AbstractContextManager[None]] | None = None
    local_delivery_hold: Callable[[], AbstractContextManager[None]] | None = None

    def authorize_local(self, path: Path) -> bool:
        if not path.is_absolute() or ".." in path.parts or self.cancelled.is_set():
            raise SourceArchiveError("scope_denied")
        path = Path(os.path.abspath(path))
        if not any(Path(selected) == path for selected in self.local_files) and not any(
            Path(root) in path.parents for root in self.local_folders
        ):
            raise SourceArchiveError("scope_denied")
        return not any(
            Path(denied) == path or Path(denied) in path.parents for denied in self.local_denials
        )

    def check_retention(self, metadata: SourceMetadata) -> None:
        if self.cancelled.is_set():
            raise SourceArchiveError("scope_denied")
        if metadata.details is not None and metadata.details.provider == "google":
            # A saved version cannot establish current or earlier folder ancestry.
            # Folder exclusions therefore refuse Google retention in this task;
            # no extra provider lookup is made to try to weaken that exclusion.
            excerpt = metadata.details.sheet_excerpt
            if (
                metadata.source_id in self.google_denials
                or (excerpt is not None and excerpt.source_id in self.google_denials)
                or self.google_denied_folders
            ):
                raise SourceArchiveError("scope_denied")
            return
        if (
            not self.local_denials
            or metadata.details is None
            or metadata.details.provider != "local"
        ):
            return
        # Old manifests may lack a mapped origin. With an active negative control
        # we cannot establish that such a source is unrelated, so refuse binding.
        origin = urlsplit(metadata.origin or "")
        if origin.scheme != "file" or origin.query or origin.fragment:
            raise SourceArchiveError("scope_denied")
        raw_path = f"//{origin.netloc}{origin.path}" if origin.netloc else origin.path
        path = Path(url2pathname(raw_path))
        if not path.is_absolute() or path.as_uri() != metadata.origin:
            raise SourceArchiveError("scope_denied")
        if any(
            Path(denied) == path or Path(denied) in path.parents for denied in self.local_denials
        ):
            raise SourceArchiveError("scope_denied")

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Serialize cancellation against the existing short publication section."""
        with self.publication:
            if self.cancelled.is_set():
                raise SourceArchiveError("scope_denied")
            yield

    def cancel(self) -> None:
        self.cancelled.set()
        # A publication already holding the reservation finishes before this
        # acknowledgement; no later operation may enter it. Network I/O is never
        # performed in this reservation.
        with self.publication:
            self.reviews.close()

    @contextmanager
    def delivery(self) -> Iterator[None]:
        """Credential generation first, then short turn/publication reservation."""
        with self.provider_hold() if self.provider_hold is not None else nullcontext():
            with self.hold():
                yield


def _prompt_parts(prompt: str) -> tuple[str, tuple[str, ...]]:
    """Separate direct user prose and attachment paths; neither is source content."""
    lines = []
    attachments: list[str] = []
    file_list = False
    fence = ""
    for line in prompt.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
            continue
        if marker:
            fence = marker[1]
        elif not line.lstrip().startswith(">") and not line.startswith(("    ", "\t")):
            if line in ("# Files mentioned by the user:", "# Files pasted by the user:"):
                file_list = True
                continue
            if file_list and not line.strip():
                continue
            if (
                file_list
                and line.startswith("## ")
                and line
                not in (
                    "## My request:",
                    "## My request for Codex:",
                )
            ):
                # Labels can contain quoted paths or URLs. Only the actual path
                # after the label is a selection; never mine the label for grants.
                if len(line) > 8192:
                    raise SourceArchiveError("limit_exceeded")
                paths = []
                for delimiter in re.finditer(": ", line):
                    candidate = line[delimiter.end() :].strip()
                    if len(candidate) <= 4096 and Path(candidate).is_absolute():
                        paths.append(candidate)
                if len(paths) == 1:
                    attachments.append(paths[0])
                continue
            file_list = False
            lines.append(line)
    return "\n".join(lines), tuple(attachments)


def _direct_prompt(prompt: str, *, keep_inline: bool = False) -> str:
    """Exclude source/code quotations and attachment labels, not classify intent."""
    direct, _ = _prompt_parts(prompt)
    return (
        direct
        if keep_inline
        else re.sub(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", "", direct, flags=re.DOTALL)
    )


def _local_paths(prompt: str) -> tuple[frozenset[str], frozenset[str]]:
    """Direct quoted paths or a standalone path line, not paths in source blocks.

    This identifies the supplied object, not natural-language retention intent.
    It neither opens file bodies nor resolves links. Retention still needs the
    configured scope policy and an explicit intake purpose; use-once wins.
    """
    direct, attachments = _prompt_parts(prompt)
    quoted = re.findall(r'(?<!`)`([^`\r\n]+)`(?!`)|"([^"\r\n]+)"', direct)
    candidates = [
        *((next(value for value in pair if value), False) for pair in quoted),
        *((line, False) for line in direct.splitlines()),
        *((path, True) for path in attachments),
    ]
    files: set[str] = set()
    folders: set[str] = set()
    for candidate, attachment in candidates:
        raw = candidate.strip()
        if len(raw) > 4096 or "\x00" in raw:
            continue
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts or path in (Path(path.anchor), Path.home()):
            continue
        normalized = str(Path(os.path.abspath(path)))
        if path.is_dir():
            if attachment:
                continue  # A file attachment can never enroll a folder's contents.
            folders.add(normalized)
        else:
            files.add(normalized)
        if len(files) + len(folders) > 256:
            raise SourceArchiveError("limit_exceeded")
    return frozenset(files), frozenset(folders)


class CodexSourceLifecycle:
    def __init__(self) -> None:
        self._active: dict[tuple[str, str], SourceTurn] = {}
        self._retired: set[tuple[str, str]] = set()
        self._lock = threading.RLock()
        self._closed = False
        self._links: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._local_links: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._local_denials: dict[str, frozenset[str]] = {}
        self._google_denials: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def link_sources(self, arguments: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        """Accept direct user links from UserPromptSubmit, never a tool argument."""
        if set(arguments) != {"session_id", "turn_id", "prompt"}:
            raise SourceArchiveError("scope_denied")
        binding = {key: arguments[key] for key in ("session_id", "turn_id")}
        key = self._hook(binding, metadata)
        prompt = arguments["prompt"]
        if not isinstance(prompt, str) or len(prompt) > 131072:
            raise SourceArchiveError("limit_exceeded")
        # Do not treat quoted source blocks as user resource selection. Prompts
        # are reduced to IDs here and are never retained by the endpoint.
        direct = _direct_prompt(prompt)
        local_files, local_folders = _local_paths(prompt)
        # Recognize explicit negative controls, never infer permission from prose.
        # Conservative turn-wide refusal also covers a bare "do not retain that".
        # Source/code quotations have already been removed. Ambiguous usefulness
        # remains the caller's job; a positive purpose cannot clear these denials.
        deny_retention = (
            re.search(
                r"\b(?:do not|don't|don’t|never)\s+(?:save|retain|remember|archive|store)\b|"
                r"\b(?:use[ -]once|temporary[ -]only)\b|\buse\b[^\n]{0,4096}\bonce\b",
                _direct_prompt(prompt, keep_inline=True),
                re.IGNORECASE,
            )
            is not None
        )
        files: set[str] = set()
        folders: set[str] = set()
        for raw in re.findall(r"https://[^\s<>\"'`]+", direct):
            try:
                url = urlsplit(raw.rstrip(".,;:!?)]}"))
            except ValueError:
                continue
            if url.netloc == "drive.google.com":
                match = re.fullmatch(
                    r"/drive/(?:u/[0-9]+/)?folders/([A-Za-z0-9_-]{1,256})/?", url.path
                )
                if match:
                    folders.add(match[1])
                else:
                    match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]{1,256})(?:/[^/]*)?", url.path)
                    if match:
                        files.add(match[1])
            elif url.netloc == "docs.google.com":
                match = re.fullmatch(
                    r"/(?:document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{1,256})(?:/[^/]*)?",
                    url.path,
                )
                if match:
                    files.add(match[1])
        self.begin(binding, metadata)
        with self._lock:
            previous_files, previous_folders = self._links.get(key[0], (frozenset(), frozenset()))
            combined = (previous_files | files, previous_folders | folders)
            previous_local = self._local_links.get(key[0], (frozenset(), frozenset()))
            combined_local = (previous_local[0] | local_files, previous_local[1] | local_folders)
            if sum(map(len, (*combined, *combined_local))) > 256 or (
                key[0] not in self._links and len(self._links) >= 1024
            ):
                raise SourceArchiveError("limit_exceeded")
            turn = self._active.get(key)
            if turn is None or turn.cancelled.is_set():
                raise SourceArchiveError("scope_denied")
            self._links[key[0]] = combined
            self._local_links[key[0]] = combined_local
            denials = self._local_denials.get(key[0], frozenset())
            if deny_retention:
                denials |= combined_local[0] | combined_local[1]
            self._local_denials[key[0]] = denials
            google_denials = self._google_denials.get(key[0], (frozenset(), frozenset()))
            if deny_retention:
                google_denials = (google_denials[0] | combined[0], google_denials[1] | combined[1])
            self._google_denials[key[0]] = google_denials
            turn.google_files, turn.google_folders = combined
            turn.local_files, turn.local_folders = combined_local
            turn.local_denials = denials
            turn.google_denials, turn.google_denied_folders = google_denials

    @staticmethod
    def _hook(arguments: Mapping[str, object], metadata: Mapping[str, object]) -> tuple[str, str]:
        if (
            set(arguments) != {"session_id", "turn_id"}
            or "callId" in metadata
            or "x-codex-turn-metadata" in metadata
        ):
            raise SourceArchiveError("scope_denied")
        thread = identifier(arguments["session_id"])
        turn = identifier(arguments["turn_id"])
        if metadata.get("threadId") != thread:
            raise SourceArchiveError("scope_denied")
        return thread, turn

    def begin(self, arguments: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        key = self._hook(arguments, metadata)
        retired: list[SourceTurn] = []
        with self._lock:
            if (
                self._closed
                or key in self._retired
                or len(self._active) >= 64
                or len(self._retired) >= 1024
            ):
                raise SourceArchiveError("capability_unavailable")
            for previous in tuple(self._active):
                if previous[0] == key[0] and previous != key:
                    old = self._active.pop(previous)
                    old.cancelled.set()
                    retired.append(old)
                    self._retired.add(previous)
            self._active.setdefault(key, SourceTurn())
        for old in retired:
            old.cancel()

    def end(self, arguments: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        key = self._hook(arguments, metadata)
        with self._lock:
            active = self._active.pop(key, None)
            full = len(self._retired) >= 1024
            if not full:
                self._retired.add(key)
        if active is not None:
            active.cancel()
        if full:
            self.close()

    def acquire(self, metadata: Mapping[str, object]) -> SourceTurn:
        context = metadata.get("x-codex-turn-metadata")
        if not isinstance(context, dict) or not isinstance(metadata.get("callId"), str):
            raise SourceArchiveError("capability_unavailable")
        thread = identifier(context.get("thread_id"))
        turn = identifier(context.get("turn_id"))
        if metadata.get("threadId") != thread or context.get("session_id") != thread:
            raise SourceArchiveError("scope_denied")
        with self._lock:
            active = self._active.get((thread, turn))
            if self._closed or active is None or active.cancelled.is_set():
                raise SourceArchiveError("scope_denied")
            return active

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active = tuple(self._active.values())
            self._active.clear()
            self._links.clear()
            self._local_links.clear()
            self._local_denials.clear()
            self._google_denials.clear()
        for turn in active:
            turn.cancel()


_COMMANDS = {
    "intake_file": "intake-file",
    "import_exports": "import-exports",
    "list_google": "list-google",
    "capture_google": "capture-google",
    "inspect": "inspect",
    "render_image": "render-image",
    "render_document": "render-document",
    "lookup": "lookup",
    "owner_view": "owner-view",
    "bind_memory": "capture",
    "check_memory": "source-evidence",
}
SOURCE_COMMANDS = frozenset(_COMMANDS.values())
_HOOKS = {"begin_turn", "end_turn", "link_sources"}
_CONTROLS = {"disconnect_google", "reconnect_google"}
_HOOK_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"session_id": {"type": "string"}, "turn_id": {"type": "string"}},
    "required": ["session_id", "turn_id"],
}
_ARGV_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "argv": {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 8192}}
    },
    "required": ["argv"],
}
_LINK_SCHEMA: JsonObject = {
    **_HOOK_SCHEMA,
    "properties": {
        "session_id": {"type": "string"},
        "turn_id": {"type": "string"},
        "prompt": {"type": "string", "maxLength": 131072},
    },
    "required": ["session_id", "turn_id", "prompt"],
}
_IMAGE_SELECTOR_DESCRIPTION = (
    " For image detail, --image-rectangle takes four separate argv strings: left top right bottom. "
    'Example: ["--image-rectangle", "10", "20", "110", "220"]. '
    "Use zero-based, orientation-corrected full-resolution coordinates, not resized overview "
    "coordinates or x/y/width/height; right and bottom are exclusive. Each crop side must be "
    "at most 2048 pixels. An overview does not establish readable inspection of small text."
)
_TOOLS: tuple[JsonObject, ...] = tuple(
    {
        "name": name,
        "description": (
            "Codex lifecycle hook only; model calls cannot grant or revoke authority."
            if name in _HOOKS
            else "Explicit scoped source operation. argv contains this tedd_sources subcommand's "
            "flags, never --config or a command name. Google access may open browser consent: "
            "the host discloses either selected-file drive.file access or an explicitly configured "
            "reusable drive.readonly connection. Folder access requires a direct user link."
        )
        + (
            _IMAGE_SELECTOR_DESCRIPTION
            if name in {"intake_file", "render_image", "owner_view"}
            else ""
        )
        + (
            " For standalone images, first use --review-only --image-unit N (1-based), inspect "
            "the returned image, then acknowledge_review with its exact review_id. Request and "
            "acknowledge detail before relying on small wording. Only then may a separately "
            "authorized intake without review-only preserve the original. --owner-review uses "
            "the private human viewer; model acknowledgment cannot confirm owner-only review."
            if name == "intake_file"
            else ""
        ),
        "inputSchema": _LINK_SCHEMA
        if name == "link_sources"
        else _HOOK_SCHEMA
        if name in _HOOKS
        else _ARGV_SCHEMA,
    }
    for name in ("begin_turn", "end_turn", "link_sources", *_COMMANDS)
)
_TOOLS += (
    {
        "name": "acknowledge_review",
        "description": "Acknowledge the exact image rendering you reviewed in this turn. "
        "An overview does not establish readable inspection of small text. "
        "This never confirms owner-only viewing or grants retention permission.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"review_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}},
            "required": ["review_id"],
        },
    },
)
_TOOLS += tuple(
    {
        "name": name,
        "description": "Request the host's separate Google disconnect or recovery confirmation. "
        "Model arguments cannot approve the action or choose credentials. "
        "Disconnection remains available while a source operation is running. "
        "Recovery never silently opens consent or declares old revocation resolved.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    for name in sorted(_CONTROLS)
)


class SourceMcpServer(McpLifecycle):
    """One bounded operation at a time; lifecycle hooks remain responsive."""

    def __init__(
        self,
        operation: Callable[[str, list[str], SourceTurn], JsonObject],
        *,
        control: Callable[[str, str, SourceTurn], JsonObject] | None = None,
    ) -> None:
        super().__init__(
            name="tedd-sources",
            tools=_TOOLS,
            instructions="Explicit source access only. Never use retrieval exposure "
            "as capture permission. Human-only viewing returns no evidence text.",
        )
        self.lifecycle = CodexSourceLifecycle()
        self._operation = operation
        self._busy = threading.Lock()
        self._control = control
        self._control_busy = threading.Lock()

    def close(self) -> None:
        self.lifecycle.close()

    def receive_line(self, line: bytes, reply: Callable[[JsonObject], None]) -> None:
        try:
            request = decode_request_line(line)
        except McpProtocolError:
            return super().receive_line(line, reply)
        params = request.get("params")
        if (
            request.get("method") != "tools/call"
            or "id" not in request
            or not isinstance(params, dict)
            or (isinstance(params.get("name"), str) and params.get("name") in _HOOKS)
        ):
            return super().receive_line(line, reply)
        is_control = (
            params.get("name") in _CONTROLS if isinstance(params.get("name"), str) else False
        )
        busy = self._control_busy if is_control else self._busy
        if not busy.acquire(blocking=False):
            reply(
                result_message(
                    cast(str | int | None, request.get("id")),
                    {"status": "capability_unavailable"},
                    is_error=True,
                )
            )
            return

        def guarded_reply(response: JsonObject) -> None:
            result = response.get("result")
            if isinstance(result, dict) and not result.get("isError"):
                try:
                    metadata = params.get("_meta")
                    if not isinstance(metadata, dict):
                        raise SourceArchiveError("scope_denied")
                    turn = self.lifecycle.acquire(metadata)
                    # Include serialization and the transport write in the
                    # reservation: cancellation must also stop late read output.
                    # Match publication ordering: config/store/archive, then
                    # credential generation and the short turn reservation.
                    # A control reply must never inherit ordinary-operation locks.
                    with (
                        turn.local_delivery_hold()
                        if not is_control and turn.local_delivery_hold is not None
                        else nullcontext()
                    ):
                        with turn.hold() if is_control else turn.delivery():
                            reply(response)
                            content = result.get("structuredContent")
                            token = content.get("review_id") if isinstance(content, dict) else None
                            if isinstance(token, str):
                                turn.reviews.delivered(token)
                    return
                except SourceArchiveError as error:
                    response = result_message(
                        cast(str | int | None, request.get("id")),
                        {"status": error.code, "reason": error.reason}
                        if isinstance(error, MacOSWorkerUnavailable)
                        else {"status": error.code},
                        is_error=True,
                    )
            reply(response)

        def run() -> None:
            try:
                super(SourceMcpServer, self).receive_line(line, guarded_reply)
            finally:
                busy.release()

        threading.Thread(target=run, name="tedd-source-operation", daemon=True).start()

    def _call_tool(self, request_id: str | int | None, params: JsonValue | None) -> JsonObject:
        values = self._params(params)
        self._closed_protocol(values, {"name", "arguments"})
        name, arguments, metadata = (values.get(key) for key in ("name", "arguments", "_meta"))
        if not isinstance(name, str) or name not in _HOOKS | _CONTROLS | _COMMANDS.keys() | {
            "acknowledge_review"
        }:
            raise McpProtocolError(METHOD_NOT_FOUND, "tool is not supported")
        try:
            if not isinstance(arguments, dict) or not isinstance(metadata, dict):
                raise SourceArchiveError("scope_denied")
            if name in _HOOKS:
                hook = {
                    "begin_turn": self.lifecycle.begin,
                    "end_turn": self.lifecycle.end,
                    "link_sources": self.lifecycle.link_sources,
                }[name]
                hook(arguments, metadata)
                payload: JsonObject = {}
            elif name in _CONTROLS:
                if arguments or self._control is None:
                    raise SourceArchiveError("capability_unavailable")
                turn = self.lifecycle.acquire(metadata)
                # Host transport identity, not a model-provided operation ID or
                # newly invented ID on each retry. It reveals no task IDs on disk.
                operation_id = digest(
                    encode(
                        [
                            "tedd-google-control-v1",
                            name,
                            metadata.get("threadId"),
                            metadata.get("callId"),
                        ]
                    )
                )
                payload = self._control(name, operation_id, turn)
            else:
                turn = self.lifecycle.acquire(metadata)
                # Every new ordinary operation gets its own provider binding,
                # including a local review acknowledgment. Controls never clear
                # the fence protecting an ordinary operation still in flight.
                turn.provider_hold = None
                turn.local_delivery_hold = None
                if name == "acknowledge_review":
                    token = arguments.get("review_id")
                    if set(arguments) != {"review_id"} or not isinstance(token, str):
                        raise SourceArchiveError("scope_denied")
                    with turn.hold():
                        rendering = turn.reviews.acknowledge(token, audience="model")
                    payload = {
                        "status": "source_review",
                        "stage": SourceProgress("reviewed").stage,
                        "unit_id": rendering.unit_id,
                        "coverage": "overview" if rendering.rectangle is None else "native_detail",
                        "rectangle": list(rendering.rectangle)
                        if rendering.rectangle is not None
                        else None,
                    }
                else:
                    argv = arguments.get("argv")
                    if (
                        set(arguments) != {"argv"}
                        or not isinstance(argv, list)
                        or len(argv) > 64
                        or any(not isinstance(item, str) or len(item) > 8192 for item in argv)
                    ):
                        raise SourceArchiveError("source_corrupt")
                    payload = self._operation(_COMMANDS[name], cast(list[str], argv), turn)
            if "review_id" in payload and "png_base64" not in payload:
                raise SourceArchiveError()
            if "png_base64" in payload:
                if payload.get("status") not in ("source_review", "rendered"):
                    raise SourceArchiveError()
                decoded_png(payload)
                if "review_id" in payload:
                    token, checksum = payload["review_id"], payload.get("png_sha256")
                    if not isinstance(token, str) or not isinstance(checksum, str):
                        raise SourceArchiveError()
                    self.lifecycle.acquire(metadata).reviews.for_delivery(
                        token,
                        audience="model",
                        png_sha256=checksum,
                    )
                metadata_only = {
                    key: value for key, value in payload.items() if key != "png_base64"
                }
                response = result_message(request_id, metadata_only)
                result = cast(JsonObject, response["result"])
                content = cast(list[JsonValue], result["content"])
                content.append(
                    {"type": "image", "mimeType": "image/png", "data": payload["png_base64"]}
                )
                try:
                    encode_response(response)
                except McpProtocolError:
                    raise SourceArchiveError("limit_exceeded") from None
                return response
            return result_message(request_id, payload)
        except SourceArchiveError as error:
            return result_message(
                request_id,
                {"status": error.code, "reason": error.reason}
                if isinstance(error, MacOSWorkerUnavailable)
                else {"status": error.code},
                is_error=True,
            )
