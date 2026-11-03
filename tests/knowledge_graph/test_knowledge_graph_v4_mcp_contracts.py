"""Protocol limits for the read-only graph MCP transport."""

from __future__ import annotations

import json
import unittest

from tedd_knowledge_graph import v4_mcp_contracts as contracts  # noqa: E402


class McpContractsTests(unittest.TestCase):
    def test_supported_protocol_and_limits_are_frozen(self) -> None:
        self.assertEqual(("2025-11-25", "2025-06-18"), contracts.SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual(1_048_576, contracts.MAX_REQUEST_BYTES)
        self.assertEqual(8 * 1_048_576, contracts.MAX_RESPONSE_BYTES)
        self.assertEqual(32, contracts.MAX_JSON_DEPTH)
        self.assertEqual(10_000, contracts.MAX_JSON_ITEMS)
        self.assertEqual(100_000, contracts.MAX_STRING_CHARS)
        self.assertEqual(512, contracts.MAX_IDENTIFIER_CHARS)
        self.assertEqual(512, contracts.MAX_NUMBER_CHARS)

    def test_decoder_rejects_duplicate_nonfinite_and_invalid_request_ids(self) -> None:
        for line in (
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
            b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
            b'{"jsonrpc":"2.0","id":true,"method":"ping"}',
            b'{"jsonrpc":"2.0","id":[],"method":"ping"}',
        ):
            with self.subTest(line=line), self.assertRaises(contracts.McpProtocolError):
                contracts.decode_request_line(line)

    def test_decoder_bounds_numeric_and_string_request_ids_before_conversion(self) -> None:
        long_number = b"9" * (contracts.MAX_IDENTIFIER_CHARS + 1)
        long_string = "x" * (contracts.MAX_IDENTIFIER_CHARS + 1)
        for line in (
            b'{"jsonrpc":"2.0","id":' + long_number + b',"method":"ping"}',
            json.dumps({"jsonrpc": "2.0", "id": long_string, "method": "ping"}).encode("utf-8"),
        ):
            with self.subTest(line=line[:32]), self.assertRaises(contracts.McpProtocolError):
                contracts.decode_request_line(line)
        decoded = contracts.decode_request_line(b'{"jsonrpc":"2.0","id":-7,"method":"ping"}')
        self.assertEqual(-7, decoded["id"])

    def test_decoder_rejects_size_tree_and_string_limit_violations(self) -> None:
        too_deep = (
            b"{"
            + b'"x":{' * (contracts.MAX_JSON_DEPTH + 1)
            + b'"x":0'
            + b"}" * (contracts.MAX_JSON_DEPTH + 1)
            + b"}"
        )
        for line in (
            b"x" * (contracts.MAX_REQUEST_BYTES + 1),
            too_deep,
            (
                '{"jsonrpc":"2.0","id":1,"method":"' + "x" * (contracts.MAX_STRING_CHARS + 1) + '"}'
            ).encode(),
        ):
            with self.subTest(size=len(line)), self.assertRaises(contracts.McpProtocolError):
                contracts.decode_request_line(line)

    def test_decoder_converts_json_recursion_failure_to_a_stable_protocol_error(self) -> None:
        line = (
            b'{"jsonrpc":"2.0","id":1,"method":"ping","params":'
            + b"[" * 2_000
            + b"0"
            + b"]" * 2_000
            + b"}"
        )
        self.assertLess(len(line), contracts.MAX_REQUEST_BYTES)
        with self.assertRaises(contracts.McpProtocolError) as raised:
            contracts.decode_request_line(line)
        self.assertEqual(contracts.INVALID_REQUEST, raised.exception.code)

    def test_result_encoding_is_single_line_and_structured_text_agree(self) -> None:
        message = contracts.result_message(7, {"answer": ["safe", 3]})
        result = message["result"]
        assert isinstance(result, dict)
        encoded = contracts.encode_response(message)
        self.assertNotIn(b"\n", encoded)
        self.assertLessEqual(len(encoded), contracts.MAX_RESPONSE_BYTES)
        self.assertEqual(
            result["structuredContent"],
            contracts.parse_content_text(result),
        )

    def test_content_parser_rejects_nonfinite_json(self) -> None:
        with self.assertRaises(ValueError):
            contracts.parse_content_text({"content": [{"type": "text", "text": '{"value":NaN}'}]})


if __name__ == "__main__":
    unittest.main()
