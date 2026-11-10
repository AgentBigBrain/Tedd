"""Read-only MCP lifecycle and stdio dispatch over ``GraphReadService``."""

from __future__ import annotations

import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import BinaryIO, Protocol, TextIO, cast

from .v4_application_contracts import (
    MAX_CURSOR_CHARS,
    BrainStatusRequest,
    GraphReadError,
    GraphReadErrorCategory,
    GraphReadResult,
    GraphReadScope,
    GraphReadSession,
    JsonObject,
    JsonValue,
    PullChangesRequest,
    ReadScopeKind,
    RecallContextRequest,
    SearchMemoryRequest,
)
from .v4_mcp_contracts import (
    INITIALIZATION_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpProtocolError,
    decode_request_line,
    encode_response,
    error_message,
    result_message,
)


class GraphReadOperations(Protocol):
    def brain_status(self, request: BrainStatusRequest) -> GraphReadResult: ...

    def recall_context(self, request: RecallContextRequest) -> GraphReadResult: ...

    def search_memory(self, request: SearchMemoryRequest) -> GraphReadResult: ...

    def pull_changes(self, request: PullChangesRequest) -> GraphReadResult: ...


_ANNOTATIONS: JsonObject = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
    "idempotentHint": True,
}


def _schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": cast(list[JsonValue], list(required)),
    }


TOOLS: tuple[JsonObject, ...] = (
    {
        "name": "brain_status",
        "description": "Inspect read-only graph health; no graph records are returned.",
        "inputSchema": _schema(
            {"consumer_id": {"type": "string", "maxLength": 512}},
            [],
        ),
        "annotations": _ANNOTATIONS,
    },
    {
        "name": "recall_context",
        "description": (
            "Read the existing exact subscription context without creating state: stated "
            "preferences, current claims with the date each was last confirmed, counts of "
            "waiting commitments and proposals, recent records and overdue items. A section "
            "marked omitted is missing information, not an empty result. Read-only."
        ),
        "inputSchema": _schema(
            {
                "subscription_id": {"type": "string", "maxLength": 512},
                "consumer_id": {"type": "string", "maxLength": 512},
                "agent_id": {"type": "string", "maxLength": 512},
                "task_id": {"type": "string", "maxLength": 512},
                "run_id": {"type": "string", "maxLength": 512},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            ["subscription_id", "consumer_id", "agent_id", "task_id"],
        ),
        "annotations": _ANNOTATIONS,
    },
    {
        "name": "search_memory",
        "description": (
            "Find saved memories in an existing exact subscription. Supplement recent context; "
            "use specific words, not FTS operators. An unquoted ASCII word "
            "of at least four letters/digits may end with one * for explicit prefix matching. "
            "Records containing every term come first: exact IDs lead, then matched recency. "
            "If none contains every term, match_mode is any_terms and results are partial: "
            "ranked by distinct terms matched, each listing matched_terms; a partial match "
            "does not establish the unmatched words. Set match to all to refuse partial results. "
            "Results retain authority/lifecycle; no matches do not prove no memory. "
            "Read-only: no source bytes, acknowledgement or scope expansion."
        ),
        "inputSchema": _schema(
            {
                "subscription_id": {"type": "string", "maxLength": 512},
                "consumer_id": {"type": "string", "maxLength": 512},
                "agent_id": {"type": "string", "maxLength": 512},
                "task_id": {"type": "string", "maxLength": 512},
                "run_id": {"type": "string", "maxLength": 512},
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_chars": {"type": "integer", "minimum": 2, "maximum": 100000},
                "match": {"type": "string", "enum": ["auto", "all", "any"]},
            },
            ["subscription_id", "consumer_id", "agent_id", "task_id", "query"],
        ),
        "annotations": _ANNOTATIONS,
    },
    {
        "name": "pull_changes",
        "description": "Read an existing subscription inbox without acknowledgement.",
        "inputSchema": _schema(
            {
                "subscription_id": {"type": "string", "maxLength": 512},
                "consumer_id": {"type": "string", "maxLength": 512},
                "agent_id": {"type": "string", "maxLength": 512},
                "task_id": {"type": "string", "maxLength": 512},
                "run_id": {"type": "string", "maxLength": 512},
                "through_watermark": {"type": "integer", "minimum": 0},
                "cursor": {"type": "string", "maxLength": MAX_CURSOR_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["subscription_id", "consumer_id", "agent_id", "task_id"],
        ),
        "annotations": _ANNOTATIONS,
    },
)


class McpLifecycle(ABC):
    """Shared bounded lifecycle; each endpoint supplies its own finite tools."""

    def __init__(self, *, name: str, instructions: str, tools: tuple[JsonObject, ...]) -> None:
        self._name = name
        self._instructions = instructions
        self._tools = tools
        self._initialized = False

    def dispatch_line(self, line: bytes) -> JsonObject | None:
        try:
            request = decode_request_line(line)
        except McpProtocolError as error:
            return error_message(error.request_id, error.code, str(error))
        if "id" not in request:
            # JSON-RPC notifications are deliberately fire-and-forget.  The
            # lifecycle notification is the sole notification with server
            # state to advance; all other methods are ignored before service
            # dispatch, even when their params would otherwise be invalid.
            if cast(str, request["method"]) == "notifications/initialized":
                try:
                    self._dispatch(None, "notifications/initialized", request.get("params"))
                except (McpProtocolError, ValueError):
                    pass
            return None
        identifier = request.get("id")
        request_id = (
            identifier
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
            else None
        )
        method = cast(str, request["method"])
        try:
            response = self._dispatch(request_id, method, request.get("params"))
        except McpProtocolError as error:
            return error_message(request_id, error.code, str(error))
        except Exception:
            return error_message(request_id, INTERNAL_ERROR, "request could not be completed")
        return response

    def receive_line(self, line: bytes, reply: Callable[[JsonObject], None]) -> None:
        response = self.dispatch_line(line)
        if response is not None:
            reply(response)

    def close(self) -> None:
        """Release endpoint-specific resources when the transport closes."""
        return None

    def _dispatch(
        self, request_id: str | int | None, method: str, params: JsonValue | None
    ) -> JsonObject | None:
        if not self._initialized:
            if method != "initialize":
                raise McpProtocolError(INITIALIZATION_REQUIRED, "initialization is required")
            return self._initialize(request_id, params)
        if method == "notifications/initialized":
            if request_id is not None:
                raise McpProtocolError(INVALID_PARAMS, "initialized must be a notification")
            return None
        if method == "ping":
            self._require_empty_params(params)
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            self._require_empty_params(params)
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": list(self._tools)}}
        if method == "tools/call":
            return self._call_tool(request_id, params)
        raise McpProtocolError(METHOD_NOT_FOUND, "method is not supported")

    def _initialize(self, request_id: str | int | None, params: JsonValue | None) -> JsonObject:
        values = self._params(params)
        self._closed_protocol(values, {"protocolVersion", "clientInfo", "capabilities"})
        version = values.get("protocolVersion")
        if not isinstance(version, str):
            raise McpProtocolError(INVALID_PARAMS, "protocolVersion is required")
        client_info = values.get("clientInfo")
        if client_info is not None and not isinstance(client_info, dict):
            raise McpProtocolError(INVALID_PARAMS, "clientInfo must be an object")
        capabilities = values.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise McpProtocolError(INVALID_PARAMS, "capabilities must be an object")
        negotiated = (
            version if version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self._initialized = True
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiated,
                "serverInfo": {"name": self._name, "version": "1.2.0-rc.13"},
                "instructions": self._instructions,
                "capabilities": {"tools": {"listChanged": False}},
            },
        }

    @staticmethod
    def _params(params: JsonValue | None) -> JsonObject:
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise McpProtocolError(INVALID_PARAMS, "params must be an object")
        return params

    def _require_empty_params(self, params: JsonValue | None) -> None:
        self._closed_protocol(self._params(params), set())

    @staticmethod
    def _closed_protocol(values: JsonObject, allowed: set[str]) -> None:
        if set(values) - (allowed | {"_meta"}):
            raise McpProtocolError(INVALID_PARAMS, "request contains unsupported fields")
        if "_meta" not in values:
            return
        metadata = values["_meta"]
        if not isinstance(metadata, dict):
            raise McpProtocolError(INVALID_PARAMS, "request metadata must be an object")
        if "progressToken" in metadata:
            token = metadata["progressToken"]
            if isinstance(token, bool) or not isinstance(token, (str, int, float)):
                raise McpProtocolError(INVALID_PARAMS, "progressToken must be a string or number")
        # Transport metadata is never forwarded as graph arguments or echoed.
        # A separately qualified source endpoint validates its host binding.

    @abstractmethod
    def _call_tool(self, request_id: str | int | None, params: JsonValue | None) -> JsonObject:
        """Dispatch only the endpoint's declared tools."""
        raise NotImplementedError


class McpServer(McpLifecycle):
    """The read-only graph endpoint over the shared MCP lifecycle."""

    def __init__(self, service: GraphReadOperations) -> None:
        super().__init__(
            name="tedd-graph",
            instructions="read-only local graph access; tools never acknowledge or modify graph state.",
            tools=TOOLS,
        )
        self._service = service

    def _call_tool(self, request_id: str | int | None, params: JsonValue | None) -> JsonObject:
        values = self._params(params)
        self._closed_protocol(values, {"name", "arguments"})
        name = values.get("name")
        arguments = values.get("arguments", {})
        if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
            raise McpProtocolError(METHOD_NOT_FOUND, "tool is not supported")
        if not isinstance(arguments, dict):
            raise McpProtocolError(INVALID_PARAMS, "tool arguments must be an object")
        try:
            payload = self._tool_payload(name, arguments)
        except GraphReadError as error:
            return self._tool_error(request_id, str(error), error.category)
        except ValueError as error:
            return self._tool_error(request_id, str(error), GraphReadErrorCategory.INVALID_REQUEST)
        try:
            return result_message(request_id, payload)
        except McpProtocolError:
            return self._tool_error(
                request_id,
                "tool response exceeds protocol limits",
                GraphReadErrorCategory.INTERNAL_ERROR,
            )

    def _tool_payload(self, name: str, values: JsonObject) -> JsonObject:
        if name == "brain_status":
            self._closed(values, {"consumer_id"})
            consumer_id = self._optional_identifier(values.get("consumer_id"), "consumer_id")
            result = self._service.brain_status(
                BrainStatusRequest(
                    session=GraphReadSession(None, None, None, None),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    consumer_id=consumer_id,
                )
            )
            return result.payload
        if name == "recall_context":
            self._closed(
                values,
                {
                    "subscription_id",
                    "consumer_id",
                    "agent_id",
                    "task_id",
                    "run_id",
                    "max_items",
                    "max_chars",
                },
            )
            session, subscription_id = self._subscription(values)
            result = self._service.recall_context(
                RecallContextRequest(
                    session=session,
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                    max_items=self._integer(
                        values.get("max_items", 100), "max_items", minimum=1, maximum=500
                    ),
                    max_chars=self._integer(
                        values.get("max_chars", 50_000), "max_chars", minimum=1, maximum=100_000
                    ),
                )
            )
            return result.payload
        if name == "search_memory":
            self._closed(
                values,
                {
                    "subscription_id",
                    "consumer_id",
                    "agent_id",
                    "task_id",
                    "run_id",
                    "query",
                    "limit",
                    "max_chars",
                    "match",
                },
            )
            session, subscription_id = self._subscription(values)
            query = self._optional_string(values.get("query"), "query", maximum=2000)
            match = values.get("match", "auto")
            if match not in ("auto", "all", "any"):
                raise ValueError("match must be auto, all or any")
            if query is None or not query.strip():
                raise ValueError("query is required")
            return self._service.search_memory(
                SearchMemoryRequest(
                    session=session,
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                    query=query,
                    limit=self._integer(values.get("limit", 10), "limit", minimum=1, maximum=100),
                    max_chars=self._integer(
                        values.get("max_chars", 12_000), "max_chars", minimum=2, maximum=100_000
                    ),
                    match=str(match),
                )
            ).payload
        self._closed(
            values,
            {
                "subscription_id",
                "consumer_id",
                "agent_id",
                "task_id",
                "run_id",
                "through_watermark",
                "cursor",
                "limit",
            },
        )
        session, subscription_id = self._subscription(values)
        result = self._service.pull_changes(
            PullChangesRequest(
                session=session,
                scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                since_watermark=0,
                through_watermark=self._optional_integer(
                    values.get("through_watermark"), "through_watermark", minimum=0
                ),
                cursor=self._optional_string(
                    values.get("cursor"), "cursor", maximum=MAX_CURSOR_CHARS
                ),
                limit=self._integer(values.get("limit", 100), "limit", minimum=1, maximum=500),
            )
        )
        return result.payload

    def _subscription(self, values: JsonObject) -> tuple[GraphReadSession, str]:
        subscription_id = self._identifier(values.get("subscription_id"), "subscription_id")
        return (
            GraphReadSession(
                self._identifier(values.get("agent_id"), "agent_id"),
                self._identifier(values.get("task_id"), "task_id"),
                self._optional_identifier(values.get("run_id"), "run_id"),
                self._identifier(values.get("consumer_id"), "consumer_id"),
            ),
            subscription_id,
        )

    @staticmethod
    def _closed(values: JsonObject, allowed: set[str]) -> None:
        if set(values) - allowed:
            raise ValueError("request contains unsupported fields")

    @staticmethod
    def _integer(value: JsonValue, label: str, *, minimum: int, maximum: int | None = None) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or maximum is not None
            and value > maximum
        ):
            raise ValueError(f"{label} is invalid")
        return value

    @classmethod
    def _optional_integer(cls, value: JsonValue | None, label: str, *, minimum: int) -> int | None:
        if value is None:
            return None
        return cls._integer(value, label, minimum=minimum)

    @staticmethod
    def _optional_string(value: JsonValue | None, label: str, *, maximum: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"{label} is invalid")
        return value

    @classmethod
    def _identifier(cls, value: JsonValue | None, label: str) -> str:
        result = cls._optional_identifier(value, label)
        if result is None:
            raise ValueError(f"{label} is required")
        return result

    @staticmethod
    def _optional_identifier(value: JsonValue | None, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
            raise ValueError(f"{label} is invalid")
        return value

    @staticmethod
    def _tool_error(
        request_id: str | int | None,
        message: str,
        category: GraphReadErrorCategory,
    ) -> JsonObject:
        safe_message = (
            message if len(message) <= 160 and "\n" not in message else "tool request was denied"
        )
        return result_message(
            request_id,
            {"category": category.value, "error": safe_message, "isError": True},
            is_error=True,
        )


def serve(
    server: McpLifecycle, input_stream: BinaryIO, output_stream: BinaryIO, error_stream: TextIO
) -> int:
    """Serve newline-delimited bytes. Diagnostics are intentionally category-only."""

    output_lock = threading.Lock()
    failed = threading.Event()

    def reply(response: JsonObject) -> None:
        with output_lock:
            if failed.is_set():
                return
            try:
                packet = encode_response(response) + b"\n"
                output_stream.write(packet)
                output_stream.flush()
            except McpProtocolError:
                failed.set()
                error_stream.write("mcp response rejected\n")
                error_stream.flush()
            except OSError:
                failed.set()
        if failed.is_set():
            # An asynchronous operation may fail while the reader waits for its
            # next line. Revoke authority now, not only after input EOF. Never
            # wait for publication while holding the transport output lock.
            server.close()

    try:
        while not failed.is_set():
            line = input_stream.readline(1_048_578)
            if not line:
                return 0
            if len(line) > 1_048_577 and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = input_stream.readline(65_536)
                reply(error_message(None, -32001, "request exceeds protocol limits"))
            else:
                server.receive_line(line.rstrip(b"\n"), reply)
        return 1
    finally:
        server.close()


def stdio_streams() -> tuple[BinaryIO, BinaryIO, TextIO]:
    return sys.stdin.buffer, sys.stdout.buffer, sys.stderr


__all__ = ["McpServer", "TOOLS", "serve", "stdio_streams"]
