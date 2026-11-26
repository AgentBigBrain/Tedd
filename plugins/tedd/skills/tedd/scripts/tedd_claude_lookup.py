#!/usr/bin/env python3
"""Opt-in Claude Code stdio endpoint for scoped local source metadata."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import tedd_setup as setup
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_lookup import LookupRequest
from tedd_knowledge_graph.source_mcp import TOOLS as SOURCE_TOOLS
from tedd_knowledge_graph.source_mcp import SourceTurn, command_arguments
from tedd_knowledge_graph.v4_application_contracts import JsonObject, JsonValue
from tedd_knowledge_graph.v4_mcp_contracts import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    McpProtocolError,
    decode_request_line,
    error_message,
    result_message,
)
from tedd_knowledge_graph.v4_mcp_stdio import McpLifecycle, serve, stdio_streams
from tedd_sources_mcp import SourceOperations

PREPARE: JsonObject = {
    "name": "prepare_lookup",
    "description": "Claude Code PreToolUse hook only. Prepare one native lookup call.",
    "inputSchema": {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("session_id", "cwd", "tool_use_id")},
        "required": ["session_id", "cwd", "tool_use_id"],
        "additionalProperties": False,
    },
}
TOOLS = (next(tool for tool in SOURCE_TOOLS if tool["name"] == "lookup"), PREPARE)
RequestId = str | int
Reply = Callable[[JsonObject], None]


def refusal(request_id: RequestId | None, code: str) -> JsonObject:
    return result_message(request_id, {"status": code}, is_error=True)


class ClaudeLookupServer(McpLifecycle):
    def __init__(self, setup_request: setup.SetupRequest, session_id: str) -> None:
        super().__init__(
            name="tedd-lookup", tools=TOOLS, instructions="Read local source metadata with lookup."
        )
        self.setup = setup_request
        self.session_id = session_id
        self.operations = SourceOperations(setup_request.graph_config)
        self._lock = threading.RLock()
        self._prepared: dict[str, float] = {}
        self._active: tuple[RequestId, SourceTurn] | None = None
        self._closed = False

    def _call_tool(self, request_id: RequestId | None, params: JsonValue | None) -> JsonObject:
        values = self._params(params)
        self._closed_protocol(values, {"name", "arguments"})
        if values.get("name") != "prepare_lookup":
            raise McpProtocolError(METHOD_NOT_FOUND, "tool is not supported")
        meta = values.get("_meta", {})
        args = values.get("arguments")
        if (
            not isinstance(meta, dict)
            or set(meta) - {"progressToken"}
            or not isinstance(args, dict)
            or set(args) != {"session_id", "cwd", "tool_use_id"}
            or args["session_id"] != self.session_id
            or not isinstance(args["cwd"], str)
            or Path(args["cwd"]) != self.setup.project
        ):
            return refusal(request_id, "scope_denied")
        call = args["tool_use_id"]
        if not isinstance(call, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", call):
            return refusal(request_id, "scope_denied")
        with self._lock:
            now = time.monotonic()
            self._prepared = {k: expiry for k, expiry in self._prepared.items() if expiry > now}
            if self._closed or len(self._prepared) >= 32:
                return refusal(request_id, "capability_unavailable")
            self._prepared[call] = now + 120
        return result_message(request_id, {})

    def receive_line(self, line: bytes, reply: Reply) -> None:
        try:
            msg = decode_request_line(line)
        except McpProtocolError:
            return super().receive_line(line, reply)
        params = msg.get("params")
        if not self._initialized or not isinstance(params, dict):
            return super().receive_line(line, reply)
        if msg["method"] == "notifications/cancelled" and "id" not in msg:
            rid = params.get("requestId")
            with self._lock:
                active = self._active
            if active and type(rid) is type(active[0]) and rid == active[0]:
                active[1].cancel()
            return
        rid = msg.get("id")
        if (
            msg["method"] != "tools/call"
            or params.get("name") != "lookup"
            or type(rid) not in (str, int)
        ):
            return super().receive_line(line, reply)
        assert isinstance(rid, (str, int))
        try:
            self._closed_protocol(params, {"name", "arguments"})
        except McpProtocolError as error:
            reply(error_message(rid, error.code, str(error)))
            return
        meta = params.get("_meta")
        call = meta.get("claudecode/toolUseId") if isinstance(meta, dict) else None
        with self._lock:
            if (
                self._closed
                or not isinstance(call, str)
                or self._prepared.pop(call, 0) <= time.monotonic()
            ):
                denied = "scope_denied"
            elif self._active is not None:
                denied = "capability_unavailable"
            else:
                turn = SourceTurn()
                self._active = (rid, turn)
                denied = None
        if denied:
            reply(refusal(rid, denied))
            return
        threading.Thread(
            target=self._lookup, args=(rid, params.get("arguments"), turn, reply), daemon=True
        ).start()

    def check_scope(self, request: LookupRequest) -> None:
        session = request.session
        if (
            session.task_id != self.session_id
            or session.consumer_id != "claude:" + self.session_id
            or not session.run_id
        ):
            raise SourceArchiveError("scope_denied")
        try:
            current = setup.session_binding(self.setup, self.session_id, session.run_id)
        except (ValueError, setup.storage.V4Error) as error:
            raise SourceArchiveError("scope_denied") from error
        if current["agent_id"] != session.agent_id or current["subscription_ids"] != [
            request.subscription_id
        ]:
            raise SourceArchiveError("scope_denied")

    def _lookup(self, rid: RequestId, args: JsonValue, turn: SourceTurn, reply: Reply) -> None:
        def finish(message: JsonObject) -> None:
            # A client can send its next call as soon as it receives these bytes.
            # A failed transport can also close the server from inside reply().
            with self._lock:
                if self._active is not None and self._active[1] is turn:
                    try:
                        reply(message)
                    finally:
                        self._active = None

        try:
            if not isinstance(args, dict):
                raise SourceArchiveError("invalid_request")
            request = self.operations.lookup_request(command_arguments("lookup", args))
            self.check_scope(request)
            payload = self.operations.local_lookup(request, turn)
            if turn.cancelled.is_set() or turn.local_delivery_hold is None:
                raise SourceArchiveError("scope_denied")
            # Serialize a project rebind and a graph/archive revocation with the transport write.
            bindings = self.setup.profile.parent / "project-bindings.json"
            with setup.graph.store_lock(bindings), turn.local_delivery_hold():
                self.check_scope(request)
                with turn.delivery():
                    finish(result_message(rid, payload))
        except SourceArchiveError as error:
            finish(refusal(rid, error.code))
        except (OSError, TimeoutError, sqlite3.Error, setup.storage.V4Error):
            finish(refusal(rid, "source_unavailable"))
        except Exception:
            # This worker is the JSON-RPC boundary. Never echo paths or source metadata.
            finish(error_message(rid, INTERNAL_ERROR, "request could not be completed"))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._prepared.clear()
            active = self._active
        if active:
            active[1].cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=setup.tedd_profile.DEFAULT_PROFILE)
    args = parser.parse_args()
    if not args.config.is_absolute() or not args.profile.is_absolute():
        parser.error("Use absolute graph configuration and profile paths")
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", session) or not project.is_absolute():
        parser.error("Start this server from an approved local Claude Code project")
    request = setup.SetupRequest(
        "claude",
        args.config,
        setup.real_directory(project),
        args.profile.expanduser(),
        Path.home() / ".codex",
        Path(sys.executable),
    )
    return serve(ClaudeLookupServer(request, session), *stdio_streams())


if __name__ == "__main__":
    raise SystemExit(main())
