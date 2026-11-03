"""Transport-neutral, read-only application contracts for the v4 graph."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

# A continuation token is opaque transport data, but it must remain bounded
# consistently at every public read boundary.
MAX_CURSOR_CHARS = 16_384


class ReadScopeKind(str, Enum):
    """The only authority scopes accepted by graph read operations."""

    GLOBAL_CLI = "global_cli"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class GraphReadSession:
    """Explicit runtime identity; ``run_id`` is provenance, not authority."""

    agent_id: str | None
    task_id: str | None
    run_id: str | None
    consumer_id: str | None


@dataclass(frozen=True)
class GraphReadScope:
    kind: ReadScopeKind
    subscription_id: str | None = None


@dataclass(frozen=True)
class BrainStatusRequest:
    session: GraphReadSession
    scope: GraphReadScope
    consumer_id: str | None = None


@dataclass(frozen=True)
class RecallContextRequest:
    session: GraphReadSession
    scope: GraphReadScope
    query: str = ""
    limit: int = 100
    collections: tuple[str, ...] = ()
    subject_ids: tuple[str, ...] = ()
    max_chars: int = 50_000
    at_watermark: int | None = None
    max_items: int = 100
    plan_fingerprint: str | None = None


@dataclass(frozen=True)
class LoopsReviewRequest:
    session: GraphReadSession
    scope: GraphReadScope
    overdue_days: int = 0
    age_days: int = 30
    limit: int = 20


@dataclass(frozen=True)
class SearchMemoryRequest:
    session: GraphReadSession
    scope: GraphReadScope
    query: str
    limit: int = 10
    max_chars: int = 12_000


@dataclass(frozen=True)
class PullChangesRequest:
    session: GraphReadSession
    scope: GraphReadScope
    since_watermark: int = 0
    through_watermark: int | None = None
    cursor: str | None = None
    limit: int = 100
    subject_ids: tuple[str, ...] = ()
    discovery_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphReadResult:
    operation: str
    payload: JsonObject
    warnings: tuple[str, ...] = ()


class GraphReadErrorCategory(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    AUTHORITY_DENIED = "authority_denied"
    SCOPE_DENIED = "scope_denied"
    STALE_STATE = "stale_state"
    CONFLICT = "conflict"
    UNHEALTHY_GRAPH = "unhealthy_graph"
    INTERNAL_ERROR = "internal_error"


class GraphReadError(RuntimeError):
    """A typed failure whose public text is safe for every transport."""

    def __init__(self, category: GraphReadErrorCategory, message: str) -> None:
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class GraphReadFailure:
    """A composition-root-classified backend failure with safe public text."""

    category: GraphReadErrorCategory
    message: str


__all__ = [
    "BrainStatusRequest",
    "GraphReadError",
    "GraphReadErrorCategory",
    "GraphReadFailure",
    "GraphReadResult",
    "GraphReadScope",
    "GraphReadSession",
    "JsonObject",
    "MAX_CURSOR_CHARS",
    "LoopsReviewRequest",
    "JsonScalar",
    "JsonValue",
    "PullChangesRequest",
    "ReadScopeKind",
    "RecallContextRequest",
    "SearchMemoryRequest",
]
