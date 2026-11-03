"""In-process lifecycle and typed-dispatch contracts for graph MCP."""

from __future__ import annotations

import io
import json
import unittest

from tedd_knowledge_graph.v4_application_contracts import (  # noqa: E402
    BrainStatusRequest,
    GraphReadResult,
    JsonObject,
    JsonValue,
    PullChangesRequest,
    RecallContextRequest,
    SearchMemoryRequest,
)
from tedd_knowledge_graph.v4_mcp_stdio import McpServer  # noqa: E402


class _FakeService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
        self.requests.append(request)
        return GraphReadResult("brain_status", {"healthy": True})

    def recall_context(self, request: RecallContextRequest) -> GraphReadResult:
        self.requests.append(request)
        return GraphReadResult(
            "recall_context", {"records": [], "subscription_id": request.scope.subscription_id}
        )

    def pull_changes(self, request: PullChangesRequest) -> GraphReadResult:
        self.requests.append(request)
        return GraphReadResult("pull_changes", {"changes": [], "complete": True})

    def search_memory(self, request: SearchMemoryRequest) -> GraphReadResult:
        self.requests.append(request)
        return GraphReadResult(
            "search_memory", {"query": request.query, "results": [], "truncated": False}
        )


def _request(identifier: int | None, method: str, params: object | None = None) -> bytes:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if identifier is not None:
        message["id"] = identifier
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def _text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _result(response: JsonObject) -> JsonObject:
    return _object(response["result"])


def _error_code(response: JsonObject) -> JsonValue:
    return _object(response["error"])["code"]


class McpStdioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.server = McpServer(self.service)

    def _dispatch(
        self, identifier: int | None, method: str, params: object | None = None
    ) -> JsonObject | None:
        return self.server.dispatch_line(_request(identifier, method, params))

    def _initialize(self) -> JsonObject:
        response = self._dispatch(
            1,
            "initialize",
            {"protocolVersion": "2025-11-25", "clientInfo": {"name": "test"}},
        )
        assert response is not None
        return response

    def test_initialize_is_first_and_negotiates_supported_versions(self) -> None:
        before = self._dispatch(1, "ping")
        assert before is not None
        self.assertEqual(-32002, _error_code(before))
        initialized = self._initialize()
        result = _result(initialized)
        self.assertEqual("2025-11-25", result["protocolVersion"])
        self.assertFalse(_object(_object(result["capabilities"])["tools"])["listChanged"])
        self.assertIn("read-only", _text(result["instructions"]))

    def test_initialize_falls_back_to_the_newest_supported_version(self) -> None:
        response = self._dispatch(
            1,
            "initialize",
            {"protocolVersion": "unsupported-version", "clientInfo": {"name": "test"}},
        )
        assert response is not None
        self.assertEqual("2025-11-25", _result(response)["protocolVersion"])

    def test_initialize_rejects_extra_or_non_object_metadata_as_invalid_params(self) -> None:
        for params in (
            {"protocolVersion": "2025-11-25", "extra": True},
            {"protocolVersion": "2025-11-25", "clientInfo": "not-an-object"},
            {"protocolVersion": "2025-11-25", "capabilities": []},
        ):
            with self.subTest(params=params):
                response = McpServer(_FakeService()).dispatch_line(
                    _request(1, "initialize", params)
                )
                assert response is not None
                self.assertEqual(-32602, _error_code(response))

    def test_initialized_notification_has_no_response_and_tools_are_closed_read_only(self) -> None:
        self._initialize()
        self.assertIsNone(self._dispatch(None, "notifications/initialized"))
        response = self._dispatch(2, "tools/list")
        assert response is not None
        tools = _items(_result(response)["tools"])
        self.assertEqual(
            ["brain_status", "recall_context", "search_memory", "pull_changes"],
            [_object(tool)["name"] for tool in tools],
        )
        for tool in tools:
            tool_data = _object(tool)
            self.assertFalse(_object(tool_data["inputSchema"]).get("additionalProperties", True))
            self.assertEqual(
                {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                    "idempotentHint": True,
                },
                _object(tool_data["annotations"]),
            )

    def test_search_has_typed_scoped_defaults_and_rejects_scope_overrides(self) -> None:
        self._initialize()
        arguments: JsonObject = {
            "subscription_id": "scope",
            "consumer_id": "consumer",
            "agent_id": "agent",
            "task_id": "task",
            "query": "logo approved",
        }
        response = self._dispatch(
            2, "tools/call", {"name": "search_memory", "arguments": arguments}
        )
        assert response is not None
        self.assertFalse(_result(response).get("isError", False))
        request = self.service.requests[-1]
        assert isinstance(request, SearchMemoryRequest)
        self.assertEqual("subscription", request.scope.kind.value)
        self.assertEqual("scope", request.scope.subscription_id)
        self.assertEqual(10, request.limit)
        self.assertEqual(12_000, request.max_chars)
        for key, value in (
            ("query", ""),
            ("query", " " * 20),
            ("query", "X" * 2001),
            ("query", None),
            ("query", False),
            ("limit", True),
            ("limit", 101),
            ("limit", 0),
            ("max_chars", 1),
            ("max_chars", 100001),
            ("collections", ["decisions"]),
            ("subject_ids", ["other-client"]),
            ("selector", {"all": True}),
            ("mode", "operator_full"),
            ("consumer_id", None),
        ):
            with self.subTest(key=key, value=value):
                before = len(self.service.requests)
                response = self._dispatch(
                    3,
                    "tools/call",
                    {
                        "name": "search_memory",
                        "arguments": {**arguments, key: value},
                    },
                )
                assert response is not None
                self.assertTrue(_result(response)["isError"])
                self.assertEqual(
                    "invalid_request", _object(_result(response)["structuredContent"])["category"]
                )
                self.assertEqual(before, len(self.service.requests))

    def test_exact_subscription_translation_never_uses_global_cli_or_acknowledges(self) -> None:
        self._initialize()
        response = self._dispatch(
            2,
            "tools/call",
            {
                "name": "pull_changes",
                "arguments": {
                    "subscription_id": "subscription-alpha",
                    "consumer_id": "consumer-alpha",
                    "agent_id": "agent-alpha",
                    "task_id": "task-alpha",
                },
            },
        )
        assert response is not None
        self.assertNotIn("error", response)
        request = self.service.requests[-1]
        self.assertIsInstance(request, PullChangesRequest)
        assert isinstance(request, PullChangesRequest)
        self.assertEqual("subscription", request.scope.kind.value)
        self.assertEqual("subscription-alpha", request.scope.subscription_id)
        self.assertFalse(hasattr(self.service, "ack"))

    def test_subscription_pull_rejects_legacy_since_watermark_instead_of_ignoring_it(self) -> None:
        """The MCP inbox contract is cursor-based; a legacy bound must not lie."""

        self._initialize()
        response = self._dispatch(
            2,
            "tools/call",
            {
                "name": "pull_changes",
                "arguments": {
                    "subscription_id": "subscription-alpha",
                    "consumer_id": "consumer-alpha",
                    "agent_id": "agent-alpha",
                    "task_id": "task-alpha",
                    "since_watermark": 4,
                },
            },
        )
        assert response is not None
        tool_result = _result(response)
        self.assertTrue(tool_result["isError"])
        self.assertEqual("invalid_request", _object(tool_result["structuredContent"])["category"])
        self.assertEqual([], self.service.requests)

    def test_brain_status_text_and_structured_content_match(self) -> None:
        """This fails if a health result differs between MCP's two public surfaces."""

        class HealthService(_FakeService):
            def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
                self.requests.append(request)
                return GraphReadResult(
                    "brain_status",
                    {"store": {"safe": True, "coordination_scope": "same-machine-only"}},
                )

        server = McpServer(HealthService())
        response = server.dispatch_line(
            _request(1, "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {}})
        )
        assert response is not None
        response = server.dispatch_line(
            _request(2, "tools/call", {"name": "brain_status", "arguments": {}})
        )
        assert response is not None

        result = _result(response)
        self.assertEqual(
            result["structuredContent"],
            json.loads(_text(_object(_items(result["content"])[0])["text"])),
        )

    def test_standard_request_metadata_does_not_block_discovery_or_calls(self) -> None:
        # rmcp adds a progressToken to outgoing requests, including tools/list.
        cases: tuple[tuple[str, JsonObject], ...] = (
            ("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {}}),
            ("ping", {}),
            ("tools/list", {}),
            ("tools/call", {"name": "brain_status", "arguments": {}}),
        )
        for method, params in cases:
            with self.subTest(method=method):
                server = McpServer(_FakeService())
                if method != "initialize":
                    server.dispatch_line(_request(1, "initialize", cases[0][1]))
                response = server.dispatch_line(
                    _request(2, method, {**params, "_meta": {"progressToken": 0}})
                )
                assert response is not None
                self.assertNotIn("error", response)
                self.assertIn("result", response)

    def test_request_metadata_is_non_authoritative_and_not_echoed(self) -> None:
        self._initialize()
        response = self._dispatch(
            2,
            "tools/call",
            {
                "name": "brain_status",
                "arguments": {},
                "_meta": {
                    "consumer_id": "metadata-only-consumer",
                    "agent_id": "metadata-only-agent",
                    "example.com/nested": {"value": ["metadata-only-value"]},
                },
            },
        )
        assert response is not None
        self.assertNotIn("error", response)
        self.assertNotIn("metadata-only", json.dumps(response))
        request = self.service.requests[-1]
        assert isinstance(request, BrainStatusRequest)
        self.assertIsNone(request.consumer_id)
        self.assertIsNone(request.session.agent_id)
        denied = self._dispatch(
            3,
            "tools/call",
            {
                "name": "recall_context",
                "arguments": {},
                "_meta": {
                    "subscription_id": "subscription-alpha",
                    "consumer_id": "consumer-alpha",
                    "agent_id": "agent-alpha",
                    "task_id": "task-alpha",
                },
            },
        )
        assert denied is not None
        self.assertTrue(_result(denied)["isError"])
        self.assertEqual(1, len(self.service.requests))

    def test_metadata_inside_tool_arguments_remains_an_invalid_argument(self) -> None:
        self._initialize()
        response = self._dispatch(
            2, "tools/call", {"name": "brain_status", "arguments": {"_meta": {}}}
        )
        assert response is not None
        self.assertTrue(_result(response)["isError"])
        self.assertEqual([], self.service.requests)

    def test_request_metadata_shape_and_progress_token_are_validated(self) -> None:
        self._initialize()
        invalid: tuple[tuple[str, JsonValue], ...] = (
            ("null-meta", None),
            ("array-meta", []),
            ("string-meta", "invalid"),
            ("null-token", {"progressToken": None}),
            ("boolean-token", {"progressToken": True}),
            ("array-token", {"progressToken": []}),
            ("object-token", {"progressToken": {}}),
        )
        for case, metadata in invalid:
            with self.subTest(case=case):
                response = self._dispatch(2, "tools/list", {"_meta": metadata})
                assert response is not None
                self.assertEqual(-32602, _error_code(response))
        for case, token in (("string", "progress-alpha"), ("integer", 0), ("number", 1.5)):
            with self.subTest(case=case):
                response = self._dispatch(3, "tools/list", {"_meta": {"progressToken": token}})
                assert response is not None
                self.assertNotIn("error", response)
                self.assertEqual(4, len(_items(_result(response)["tools"])))

    def test_request_metadata_remains_subject_to_protocol_size_limits(self) -> None:
        self._initialize()
        response = self._dispatch(2, "tools/list", {"_meta": {"example.com/value": "x" * 100_001}})
        assert response is not None
        self.assertEqual(-32600, _error_code(response))

    def test_protocol_errors_are_not_tool_errors_and_tool_errors_are_results(self) -> None:
        self._initialize()
        unknown = self._dispatch(2, "tools/call", {"name": "unknown", "arguments": {}})
        assert unknown is not None
        self.assertEqual(-32601, _error_code(unknown))
        invalid_tool = self._dispatch(3, "tools/call", {"name": "recall_context", "arguments": {}})
        assert invalid_tool is not None
        self.assertTrue(_result(invalid_tool)["isError"])
        self.assertNotIn("error", invalid_tool)

    def test_tool_argument_errors_are_results_but_envelope_errors_are_protocol_errors(self) -> None:
        self._initialize()
        tool_error = self._dispatch(
            2,
            "tools/call",
            {"name": "brain_status", "arguments": {"unknown": True}},
        )
        assert tool_error is not None
        result = _result(tool_error)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"],
            json.loads(_text(_object(_items(result["content"])[0])["text"])),
        )
        envelope_error = self._dispatch(3, "tools/list", {"unknown": True})
        assert envelope_error is not None
        self.assertEqual(-32602, _error_code(envelope_error))

    def test_oversize_service_payload_becomes_a_bounded_tool_error(self) -> None:
        class OversizeService(_FakeService):
            def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
                self.requests.append(request)
                return GraphReadResult("brain_status", {"value": "x" * (8 * 1_048_576)})

        server = McpServer(OversizeService())
        server.dispatch_line(
            _request(1, "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {}})
        )
        response = server.dispatch_line(
            _request(2, "tools/call", {"name": "brain_status", "arguments": {}})
        )
        assert response is not None
        self.assertTrue(_result(response)["isError"])

    def test_nonfinite_service_payload_becomes_a_bounded_tool_error(self) -> None:
        class NonfiniteService(_FakeService):
            def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
                self.requests.append(request)
                return GraphReadResult("brain_status", {"value": float("nan")})

        server = McpServer(NonfiniteService())
        server.dispatch_line(
            _request(1, "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {}})
        )
        response = server.dispatch_line(
            _request(2, "tools/call", {"name": "brain_status", "arguments": {}})
        )
        assert response is not None
        result = _result(response)
        self.assertTrue(result["isError"])
        self.assertNotIn("NaN", _text(_object(_items(result["content"])[0])["text"]))

    def test_tool_limits_and_subscription_identity_are_exact(self) -> None:
        self._initialize()
        shared = {
            "subscription_id": "subscription-alpha",
            "consumer_id": "consumer-alpha",
            "agent_id": "agent-alpha",
            "task_id": "task-alpha",
        }
        cases = (
            ("recall_context", {**shared, "subscription_id": " subscription-alpha"}),
            ("recall_context", {**shared, "max_items": 501}),
            ("recall_context", {**shared, "max_chars": 100_001}),
            ("pull_changes", {**shared, "limit": 501}),
        )
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                response = self._dispatch(2, "tools/call", {"name": name, "arguments": arguments})
                assert response is not None
                self.assertTrue(_result(response)["isError"])

    def test_idless_notifications_are_ignored_without_reading_or_writing_stdout(self) -> None:
        self._initialize()
        for method, params in (
            ("ping", ["unexpected"]),
            ("method/unsupported", {}),
            ("tools/call", {"name": "brain_status", "arguments": {}}),
        ):
            with self.subTest(method=method):
                self.assertIsNone(self._dispatch(None, method, params))
        self.assertEqual([], self.service.requests)

        input_stream = io.BytesIO(
            _request(None, "tools/call", {"name": "brain_status", "arguments": {}})
        )
        output_stream = io.BytesIO()
        self.assertEqual(
            0,
            __import__("tedd_knowledge_graph.v4_mcp_stdio", fromlist=["serve"]).serve(
                self.server, input_stream, output_stream, io.StringIO()
            ),
        )
        self.assertEqual(b"", output_stream.getvalue())

    def test_pull_cursor_schema_and_runtime_limit_match_the_service(self) -> None:
        self._initialize()
        list_response = self._dispatch(1, "tools/list")
        assert list_response is not None
        tools = _items(_result(list_response)["tools"])
        pull_tool = next(_object(tool) for tool in tools if _object(tool)["name"] == "pull_changes")
        cursor_schema = _object(_object(pull_tool["inputSchema"])["properties"])["cursor"]
        self.assertEqual(16_384, _object(cursor_schema)["maxLength"])
        common = {
            "subscription_id": "subscription-alpha",
            "consumer_id": "consumer-alpha",
            "agent_id": "agent-alpha",
            "task_id": "task-alpha",
        }
        accepted = self._dispatch(
            2,
            "tools/call",
            {"name": "pull_changes", "arguments": {**common, "cursor": "x" * 16_384}},
        )
        assert accepted is not None
        self.assertNotIn("isError", _result(accepted))
        rejected = self._dispatch(
            3,
            "tools/call",
            {"name": "pull_changes", "arguments": {**common, "cursor": "x" * 16_385}},
        )
        assert rejected is not None
        self.assertTrue(_result(rejected)["isError"])
        self.assertEqual(
            "invalid_request", _object(_result(rejected)["structuredContent"])["category"]
        )


if __name__ == "__main__":
    unittest.main()
