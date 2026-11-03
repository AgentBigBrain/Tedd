"""Small, dependency-free JSON-RPC contracts for the graph MCP server."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import TypeAlias, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 8 * 1_048_576
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = 10_000
MAX_STRING_CHARS = 100_000
MAX_IDENTIFIER_CHARS = 512
MAX_NUMBER_CHARS = 512

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_TOO_LARGE = -32001
INITIALIZATION_REQUIRED = -32002


class McpProtocolError(ValueError):
    """A public, compact protocol error with no diagnostic details."""

    def __init__(self, code: int, message: str, request_id: JsonScalar = None) -> None:
        self.code = code
        self.request_id = request_id
        super().__init__(message)


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite number")


def _bounded_integer(value: str) -> int:
    if len(value) > MAX_NUMBER_CHARS:
        raise ValueError("number is too large")
    return int(value)


def _reject_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or non-string key")
        result[key] = value
    return result


def decode_request_line(line: bytes) -> JsonObject:
    """Decode one bounded UTF-8 JSON-RPC request with a closed envelope."""

    if not isinstance(line, bytes) or not line or len(line) > MAX_REQUEST_BYTES:
        raise McpProtocolError(REQUEST_TOO_LARGE, "request exceeds protocol limits")
    if b"\n" in line or b"\r" in line:
        raise McpProtocolError(INVALID_REQUEST, "request must be one line")
    try:
        text = line.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise McpProtocolError(INVALID_REQUEST, "invalid JSON-RPC request") from error
    _validate_tree(decoded, depth=1)
    if not isinstance(decoded, dict):
        raise McpProtocolError(INVALID_REQUEST, "request must be an object")
    request = cast(JsonObject, decoded)
    allowed = {"jsonrpc", "id", "method", "params"}
    if set(request) - allowed or request.get("jsonrpc") != "2.0":
        raise McpProtocolError(INVALID_REQUEST, "invalid JSON-RPC envelope")
    method = request.get("method")
    if not isinstance(method, str) or not method or len(method) > MAX_IDENTIFIER_CHARS:
        raise McpProtocolError(INVALID_REQUEST, "invalid JSON-RPC method")
    if "id" in request and not _valid_id(request["id"]):
        raise McpProtocolError(INVALID_REQUEST, "invalid JSON-RPC request ID")
    return request


def _validate_tree(value: object, *, depth: int) -> None:
    items = 0

    def visit(current: object, current_depth: int) -> None:
        nonlocal items
        if current_depth > MAX_JSON_DEPTH:
            raise McpProtocolError(INVALID_REQUEST, "JSON request is too deeply nested")
        if isinstance(current, str):
            if len(current) > MAX_STRING_CHARS:
                raise McpProtocolError(INVALID_REQUEST, "JSON request string is too large")
            return
        if isinstance(current, float) and not math.isfinite(current):
            raise McpProtocolError(INVALID_REQUEST, "JSON request contains a non-finite number")
        if isinstance(current, list):
            items += len(current)
            if items > MAX_JSON_ITEMS:
                raise McpProtocolError(INVALID_REQUEST, "JSON request has too many items")
            for child in current:
                visit(child, current_depth + 1)
            return
        if isinstance(current, Mapping):
            items += len(current)
            if items > MAX_JSON_ITEMS:
                raise McpProtocolError(INVALID_REQUEST, "JSON request has too many items")
            for key, child in current.items():
                if not isinstance(key, str):
                    raise McpProtocolError(INVALID_REQUEST, "JSON request key is invalid")
                visit(key, current_depth + 1)
                visit(child, current_depth + 1)

    visit(value, depth)


def _valid_id(value: JsonValue) -> bool:
    return (isinstance(value, str) and len(value) <= MAX_IDENTIFIER_CHARS) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _request_id(request: JsonObject) -> JsonScalar:
    value = request.get("id")
    return cast(JsonScalar, value) if _valid_id(value) else None


def error_message(request_id: JsonScalar, code: int, message: str) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def result_message(
    request_id: JsonScalar, payload: JsonObject, *, is_error: bool = False
) -> JsonObject:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise McpProtocolError(INTERNAL_ERROR, "response is invalid", request_id) from error
    if "\n" in text or "\r" in text:
        raise McpProtocolError(INTERNAL_ERROR, "response contains an embedded newline", request_id)
    result: JsonObject = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
    }
    if is_error:
        result["isError"] = True
    message: JsonObject = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }
    # Validate the complete envelope here, before a serve loop has committed a
    # partial line to stdout.
    encode_response(message)
    return message


def parse_content_text(result: JsonValue) -> JsonObject:
    if not isinstance(result, Mapping):
        raise ValueError("result is invalid")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise ValueError("content is invalid")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise ValueError("content text is invalid")
    value = json.loads(text, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("content text is invalid")
    return cast(JsonObject, value)


def encode_response(message: JsonObject) -> bytes:
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise McpProtocolError(INTERNAL_ERROR, "response is invalid") from error
    if b"\n" in encoded or b"\r" in encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise McpProtocolError(INTERNAL_ERROR, "response exceeds protocol limits")
    return encoded


__all__ = [
    "INITIALIZATION_REQUIRED",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "MAX_IDENTIFIER_CHARS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_ITEMS",
    "MAX_NUMBER_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_STRING_CHARS",
    "METHOD_NOT_FOUND",
    "McpProtocolError",
    "REQUEST_TOO_LARGE",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "decode_request_line",
    "encode_response",
    "error_message",
    "parse_content_text",
    "result_message",
]
