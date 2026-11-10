"""Shared synthetic project and fake CLI and MCP responses for project recall tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from tedd_memory_eval.contracts import RecordFixture, Scenario
from tedd_memory_eval.sandbox import MemoryEvalSandbox
from tedd_project_recall.catalog import Placed, SyntheticProject, project_case
from tedd_project_recall.contracts import ProjectCase
from tedd_project_recall.mcp_reads import McpReader, ToolCall


class MiniProject:
    """A 24-record project with an old decision, its replacement, and an open loop."""

    def __init__(self) -> None:
        self.project = SyntheticProject("mini", 24)
        self.loop = self.project.loop(
            3,
            "promised",
            title="Send the reviewer the summary",
            summary="The reviewer asked for a written summary.",
            next_action="Write the summary and send it.",
            resolved=False,
        )
        self.current = self.project.decision(
            10,
            "current",
            title="The cache refreshes every fifteen minutes",
            summary="The cache refreshes every fifteen minutes.",
            rationale="The hourly refresh left tiles out of date.",
        )
        self.replaced = self.project.decision(
            11,
            "replaced",
            title="The cache refreshes hourly",
            summary="The cache refreshes every sixty minutes.",
            rationale="An hourly refresh kept the load low.",
            status="superseded",
            decided_position=2,
            superseded_by=self.current.record_id,
        )

    def case(self, *required: Placed) -> ProjectCase:
        """Build the project around the given required records."""

        planted = (self.loop, self.current)
        return project_case(
            self.project,
            query="What is still owed, and how often does the cache refresh?",
            placement="outside_recall",
            required=required,
            family="synthetic",
            other_planted=tuple(item for item in planted if item not in required),
            stale=(self.replaced,),
            current_before_stale=(
                ((self.current.record_id, self.replaced.record_id),)
                if self.current in required
                else ()
            ),
        )


def record(fixture: RecordFixture, **payload_updates: object) -> dict[str, object]:
    """Return one record as a CLI read returns it."""

    return {
        "record_id": fixture.record_id,
        "collection": fixture.collection,
        "payload": {**fixture.payload, **payload_updates},
    }


class FakeSandbox:
    """Answer the three allowlisted reads from prepared synthetic records."""

    def __init__(
        self,
        case: ProjectCase,
        *,
        context_records: Sequence[Mapping[str, object]] = (),
        sections: Mapping[str, object] | None = None,
        recall_records: Sequence[Mapping[str, object]] = (),
        recall_extra: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fixtures = {item.record_id: item for item in case.scenario.records}
        self._context_records = list(context_records)
        self._sections = dict(sections or {})
        self._recall_records = list(recall_records)
        self._recall_extra = dict(recall_extra or {})

    def call(self, arguments: Sequence[str]) -> dict[str, object]:
        command = tuple(arguments)
        self.calls.append(command)
        if command[0] == "context":
            return {
                "command": "context",
                "status": "ok",
                **self._sections,
                "subscription_id": command[command.index("--subscription-id") + 1],
                "item_count": len(self._context_records),
                "char_count": 1,
                "truncated": True,
                "records": self._context_records,
            }
        if command[0] == "recall":
            return {
                **self._recall_extra,
                "results": self._recall_records,
                "result_count": len(self._recall_records),
                "subject_ids": [command[command.index("--subject-id") + 1]],
                "truncated_by_char_budget": False,
            }
        return record(self._fixtures[command[command.index("--id") + 1]])

    def as_sandbox(self) -> MemoryEvalSandbox:
        """Present this fake where an arm expects the sandbox's read method."""

        return cast(MemoryEvalSandbox, self)


class FakeMcp:
    """Answer MCP tool calls from prepared bodies and remember what was asked."""

    def __init__(
        self,
        *,
        context: Mapping[str, object] | None = None,
        search: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._bodies = {"recall_context": dict(context or {}), "search_memory": dict(search or {})}

    def call(self, scenario: Scenario, calls: Sequence[ToolCall]) -> list[dict[str, object]]:
        del scenario
        # Copy first: a caller may pass this fake's own record of earlier calls.
        asked = [(tool, dict(arguments)) for tool, arguments in calls]
        self.calls.extend(asked)
        return [self._bodies[tool] for tool, _arguments in asked]

    def connect(self, sandbox: MemoryEvalSandbox) -> McpReader:
        """Stand in for opening MCP reads on a sandbox."""

        del sandbox
        return self


def context_body(records: Sequence[Mapping[str, object]], **sections: object) -> dict[str, object]:
    """Return a context tool result holding the given records and sections."""

    return {**sections, "records": list(records), "truncated": True}


def search_body(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Return a search tool result holding the given records."""

    return {"results": list(records), "result_count": len(records), "truncated": False}
