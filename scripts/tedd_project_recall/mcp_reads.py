"""Read one synthetic project through Tedd's packaged read-only MCP server.

The memory evaluation lab's sandbox allows only its frozen CLI reads.  This
module adds the other public read route, the one an agent uses, under the same
limits: the sandbox's own synthetic configuration, synthetic identifiers, an
isolated child environment, a time limit, and no tool that can write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from tedd_memory_eval.contracts import ALLOWED_RECORD_PREFIX, Scenario
from tedd_memory_eval.sandbox import (
    GRAPH_SCRIPTS,
    REPOSITORY_ROOT,
    MemoryEvalSandbox,
    consumer_id_for,
    subscription_id_for,
)

GRAPH_MCP = GRAPH_SCRIPTS / "tedd_graph_mcp.py"
MCP_PROTOCOL_VERSION = "2025-06-18"
# The agent the sandbox registers for every scenario's consumer.
LAB_AGENT_ID = "lab_agent_memory_eval"
TOOL_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "recall_context": frozenset({"max_items", "max_chars"}),
    "search_memory": frozenset({"query", "limit", "max_chars"}),
}

ToolCall = tuple[str, Mapping[str, object]]


class McpReader(Protocol):
    """The one operation an MCP arm needs from its transport."""

    def call(self, scenario: Scenario, calls: Sequence[ToolCall]) -> list[dict[str, object]]: ...


def _child_environment(root: Path) -> dict[str, str]:
    """Mirror the sandbox's isolated child environment for another read process."""

    environment = dict(os.environ)
    environment.update(
        {
            "APPDATA": str(root),
            "HOME": str(root),
            "LOCALAPPDATA": str(root),
            "TEMP": str(root),
            "TMP": str(root),
            "USERPROFILE": str(root),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment.pop("PYTHONPATH", None)
    return environment


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a JSON object")
    raw = cast(Mapping[object, object], value)
    return {str(key): item for key, item in raw.items()}


def _replies(output: bytes) -> dict[object, dict[str, object]]:
    replies: dict[object, dict[str, object]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            decoded = cast(object, json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Synthetic MCP server returned invalid JSON") from error
        reply = _object(decoded, "Synthetic MCP reply")
        replies[reply.get("id")] = reply
    return replies


def _tool_result(reply: dict[str, object] | None) -> dict[str, object]:
    if reply is None:
        raise RuntimeError("Synthetic MCP server did not answer every read")
    result = _object(reply.get("result"), "Synthetic MCP result")
    body = _object(result.get("structuredContent"), "Synthetic MCP tool result")
    if result.get("isError"):
        # The category is a fixed enumeration; the message may quote a path.
        raise RuntimeError(f"Synthetic MCP read was refused: {body.get('category')}")
    return body


class McpReads:
    """Run read-only MCP tool calls against one sandbox's synthetic graph."""

    def __init__(self, sandbox: MemoryEvalSandbox) -> None:
        self._sandbox = sandbox

    def call(self, scenario: Scenario, calls: Sequence[ToolCall]) -> list[dict[str, object]]:
        """Run the calls in order in one server process and return each tool result.

        The scenario supplies the subscription, consumer and task the sandbox
        registered for it, so a call can read only that scenario's project.

        Raises:
            RuntimeError: If a call is outside the frozen read allowlist, the
                sandbox is not prepared, or the server fails, times out,
                refuses a call, or returns malformed output.
        """

        root, config = self._sandbox.root, self._sandbox.config_path
        if root is None or config is None:
            raise RuntimeError("Memory evaluation sandbox is not prepared")
        identity = {
            "subscription_id": subscription_id_for(scenario),
            "consumer_id": consumer_id_for(scenario),
            "agent_id": LAB_AGENT_ID,
            "task_id": f"lab_task_{scenario.case_id}",
        }
        if not all(value.startswith(ALLOWED_RECORD_PREFIX) for value in identity.values()):
            raise RuntimeError("Project recall MCP reads require synthetic lab identifiers")
        messages: list[dict[str, object]] = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "tedd-project-recall-lab", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        for index, (tool, arguments) in enumerate(calls, start=1):
            allowed = TOOL_ARGUMENTS.get(tool)
            if allowed is None or not set(arguments) <= allowed:
                raise RuntimeError("Project recall permits only its frozen MCP reads")
            messages.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {**arguments, **identity}},
                }
            )
        try:
            completed = subprocess.run(
                [sys.executable, "-B", str(GRAPH_MCP), "--config", str(config)],
                # One LF-terminated request per line, on every platform.
                input=b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in messages),
                cwd=REPOSITORY_ROOT,
                env=_child_environment(root),
                check=False,
                capture_output=True,
                timeout=self._sandbox.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Synthetic MCP read exceeded its time limit") from error
        if completed.returncode != 0:
            raise RuntimeError("Synthetic MCP server failed")
        replies = _replies(completed.stdout)
        return [_tool_result(replies.get(index)) for index in range(1, len(calls) + 1)]


__all__ = ["McpReader", "McpReads", "ToolCall"]
