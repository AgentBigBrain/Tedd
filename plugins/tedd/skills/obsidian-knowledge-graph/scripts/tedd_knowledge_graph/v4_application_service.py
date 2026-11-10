"""Private, transport-neutral read application service for the v4 graph.

All filesystem, security, storage, and disclosure policies arrive through
``GraphReadDependencies``.  This module intentionally does not know about a
CLI, MCP, configured graph path, or compatibility facade.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from .v4_application_contracts import (
    MAX_CURSOR_CHARS,
    BrainStatusRequest,
    FoldCandidatesRequest,
    GraphReadError,
    GraphReadErrorCategory,
    GraphReadFailure,
    GraphReadResult,
    GraphReadScope,
    GraphReadSession,
    JsonObject,
    JsonValue,
    LoopsReviewRequest,
    PullChangesRequest,
    ReadScopeKind,
    RecallContextRequest,
    ReviewAnswersRequest,
    ReviewQueueRequest,
    SearchMemoryRequest,
)
from .v4_review_answers import AnswersError, StoredRecord, build_answers_packet

# Disclosed-section ceilings, in characters of sanitized JSON.
CURRENT_STATE_CHARS = 2_400
OVERDUE_CHARS = 1_500
PREFERENCE_CHARS = 2_000
# The current-state sheet never takes more than this share of a context budget.
CURRENT_STATE_BUDGET_DIVISOR = 4


class GraphReadStore(Protocol):
    """The strictly read-only subset of the v4 store facade."""

    readonly: bool

    def read_snapshot(self) -> AbstractContextManager[None]: ...

    def state(self) -> Mapping[str, object]: ...

    def search(
        self, query: str, *, limit: int = 100, collections: Iterable[str] | None = None
    ) -> Mapping[str, object]: ...

    def search_subscription(
        self, subscription_id: str, query: str, *, limit: int = 10, match: str = "all"
    ) -> Mapping[str, object]: ...

    def records_referencing(
        self, target_ids: Iterable[str], *, limit: int = 500
    ) -> list[Mapping[str, object]]: ...

    def mutation_feed(
        self,
        since_watermark: int,
        *,
        through_watermark: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
        selector: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def consumer_status(self, consumer_id: str) -> Mapping[str, object]: ...

    def subscription_identity(self, subscription_id: str) -> Mapping[str, object] | None: ...

    def context_pack(
        self, subscription_id: str, *, max_items: int = 100, max_chars: int = 50_000
    ) -> Mapping[str, object]: ...

    def review_loops(
        self, subscription_id: str, *, overdue_days: int = 0, age_days: int = 30, limit: int = 20
    ) -> Mapping[str, object]: ...

    def preferences(self, subscription_id: str) -> Mapping[str, object]: ...

    def current_state(self, subscription_id: str) -> Mapping[str, object]: ...

    def waiting_summary(self, subscription_id: str) -> Mapping[str, object]: ...

    def review_queue(self, subscription_id: str, *, limit: int = 20) -> Mapping[str, object]: ...

    def fold_candidates(
        self,
        subscription_id: str,
        *,
        subject_id: str | None = None,
        family: str | None = None,
        limit: int = 20,
    ) -> Mapping[str, object]: ...

    def scoped_records(
        self, subscription_id: str, record_ids: Iterable[str]
    ) -> Mapping[str, StoredRecord]: ...

    def create_plan_token(self, record_ids: Iterable[str], *, fingerprint: str) -> str: ...

    def inbox(
        self,
        subscription_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        through_watermark: int | None = None,
    ) -> Mapping[str, object]: ...


class LoadConfig(Protocol):
    def __call__(self) -> object: ...


class ResolveStorePath(Protocol):
    def __call__(self, config: object) -> Path: ...


class ValidateStorePath(Protocol):
    def __call__(self, config: object, path: Path) -> None: ...


class DetectStore(Protocol):
    def __call__(self, path: Path) -> str: ...


class InspectStore(Protocol):
    def __call__(self, path: Path) -> Mapping[str, object]: ...


class LoadPlanKey(Protocol):
    def __call__(self, config: object, path: Path) -> bytes | str | None: ...


class OpenReadStore(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> AbstractContextManager[GraphReadStore]: ...


class ResultSanitizer(Protocol):
    def __call__(self, value: JsonObject) -> object: ...


class ErrorClassifier(Protocol):
    def __call__(self, error: Exception) -> GraphReadFailure: ...


@dataclass(frozen=True)
class GraphReadLimits:
    max_query_chars: int = 2_000
    max_page_size: int = 500
    max_context_chars: int = 100_000
    max_subscription_identity_chars: int = 512
    max_cursor_chars: int = MAX_CURSOR_CHARS


class ReadAuthorityMode(str, Enum):
    """Whether a subscription must carry an exact runtime owner session."""

    EXACT_SESSION = "exact_session"
    TRUSTED_LOCAL_CLI = "trusted_local_cli"


@dataclass(frozen=True)
class GraphReadDependencies:
    load_config: LoadConfig
    resolve_store_path: ResolveStorePath
    validate_store_path: ValidateStorePath
    detect_store: DetectStore
    inspect_store: InspectStore
    load_plan_key: LoadPlanKey
    open_store: OpenReadStore
    sanitize: ResultSanitizer
    limits: GraphReadLimits = GraphReadLimits()
    classify_error: ErrorClassifier | None = None
    authority_mode: ReadAuthorityMode = ReadAuthorityMode.EXACT_SESSION


def _public_store_health(assessment: Mapping[str, object]) -> JsonObject:
    """Project a verified store assessment onto the public health contract."""

    safe = assessment.get("safe")
    scope = assessment.get("coordination_scope")
    if not isinstance(safe, bool) or scope != "same-machine-only":
        raise ValueError("store health is invalid")
    return {"safe": safe, "coordination_scope": "same-machine-only"}


# The item-list sections that grow one item at a time inside their own ceilings.
_SectionName = Literal["current_state", "overdue", "preferences"]


@dataclass(frozen=True)
class _ContextSections:
    """The parts of one subscription context that are budgeted independently."""

    records: list[JsonValue]
    current_state: JsonObject
    waiting: JsonObject
    overdue: JsonObject
    preferences: JsonObject

    def section(self, name: _SectionName) -> JsonObject:
        if name == "current_state":
            return self.current_state
        if name == "overdue":
            return self.overdue
        return self.preferences

    def with_section(self, name: _SectionName, section: JsonObject) -> _ContextSections:
        if name == "current_state":
            return replace(self, current_state=section)
        if name == "overdue":
            return replace(self, overdue=section)
        return replace(self, preferences=section)


class GraphReadService:
    """Execute scope-safe graph reads without persisting any state."""

    def __init__(self, dependencies: GraphReadDependencies) -> None:
        self._dependencies = dependencies

    def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
        self._validate_scope(request.session, request.scope)
        if request.scope.kind is not ReadScopeKind.GLOBAL_CLI:
            self._raise(GraphReadErrorCategory.SCOPE_DENIED, "brain status requires global scope")

        def operation(store: GraphReadStore, path: Path) -> JsonObject:
            payload: JsonObject = {
                "store": _public_store_health(
                    self._json_object(self._dependencies.inspect_store(path))
                ),
                "graph": self._json_object(store.state()),
            }
            if request.consumer_id is not None:
                payload["consumer"] = self._json_object(store.consumer_status(request.consumer_id))
            return payload

        return self._execute("brain_status", operation)

    def recall_context(self, request: RecallContextRequest) -> GraphReadResult:
        self._validate_scope(request.session, request.scope)
        if request.scope.kind is ReadScopeKind.SUBSCRIPTION:
            subscription_id = self._require_subscription(request.session, request.scope)

            def subscription_operation(store: GraphReadStore, _path: Path) -> JsonObject:
                self._validate_subscription_owner(store, subscription_id, request.session)
                payload = self._json_object(
                    store.context_pack(
                        subscription_id,
                        max_items=self._bounded_positive(request.max_items, "context item budget"),
                        max_chars=self._bounded_context_chars(request.max_chars),
                    )
                )
                overdue = self._json_object(store.review_loops(subscription_id, limit=5))
                payload["overdue"] = {
                    "items": overdue["items"],
                    "truncated": overdue["truncated"],
                    "omitted": False,
                }
                return {
                    "preferences": self._json_object(store.preferences(subscription_id)),
                    "current_state": self._json_object(store.current_state(subscription_id)),
                    "waiting": self._json_object(store.waiting_summary(subscription_id)),
                    **payload,
                }

            return self._execute(
                "recall_context",
                subscription_operation,
                disclose=lambda store, payload: self._subscription_context_disclosure(
                    store, payload, request
                ),
            )

        def global_operation(store: GraphReadStore, _path: Path) -> JsonObject:
            if request.at_watermark is not None:
                state = self._json_object(store.state())
                if request.at_watermark != state.get("watermark"):
                    self._raise(
                        GraphReadErrorCategory.STALE_STATE, "recall requires current graph state"
                    )
            query = request.query.strip()
            if len(query) > self._dependencies.limits.max_query_chars:
                self._raise(GraphReadErrorCategory.INVALID_REQUEST, "recall query is too large")
            value = self._json_object(
                store.search(
                    query,
                    limit=self._bounded_positive(request.limit, "recall limit"),
                    collections=request.collections or None,
                )
            )
            if request.subject_ids:
                allowed = {
                    str(record.get("record_id"))
                    for record in store.records_referencing(request.subject_ids, limit=2_000)
                }
                results = self._json_list(value.get("results"))
                filtered = [
                    record
                    for record in results
                    if self._json_object(record).get("record_id") in allowed
                ]
                value["results"] = filtered
                value["result_count"] = len(filtered)
                value["subject_ids"] = list(request.subject_ids)
            self._truncate_search(value, self._bounded_context_chars(request.max_chars))
            return value

        return self._execute("recall_context", global_operation)

    def loops_review(self, request: LoopsReviewRequest) -> GraphReadResult:
        self._validate_scope(request.session, request.scope)
        if request.scope.kind is not ReadScopeKind.SUBSCRIPTION:
            self._raise(
                GraphReadErrorCategory.SCOPE_DENIED, "loop review requires subscription scope"
            )
        subscription_id = self._require_subscription(request.session, request.scope)

        def operation(store: GraphReadStore, _path: Path) -> JsonObject:
            self._validate_subscription_owner(store, subscription_id, request.session)
            return self._json_object(
                store.review_loops(
                    subscription_id,
                    overdue_days=request.overdue_days,
                    age_days=request.age_days,
                    limit=request.limit,
                )
            )

        return self._execute("loops_review", operation)

    def _subscription_context_disclosure(
        self, store: GraphReadStore, payload: JsonObject, request: RecallContextRequest
    ) -> JsonObject:
        """Fit one context inside the caller's budget, most durable sections first.

        The waiting counts and a bounded current-state sheet are reserved before
        recent records.  Overdue details and stated preferences use what remains.
        """

        fingerprint = request.plan_fingerprint
        if fingerprint is not None:
            fingerprint = fingerprint.strip()
            if not fingerprint or len(fingerprint) > 256:
                self._raise(GraphReadErrorCategory.INVALID_REQUEST, "plan fingerprint is invalid")
        exhausted: JsonObject = {"truncated": True, "omitted": True, "reason": "budget_exhausted"}
        full = _ContextSections(
            records=self._json_list(payload.get("records")),
            current_state=self._json_object(payload["current_state"]),
            waiting=self._json_object(payload["waiting"]),
            overdue=self._json_object(payload["overdue"]),
            preferences=self._json_object(payload["preferences"]),
        )

        def withheld(section: JsonObject) -> JsonObject:
            # An empty section is a true "nothing here"; only withheld items are omissions.
            return {**section, "items": [], **exhausted} if section["items"] else section

        def portion(section: JsonObject, count: int) -> JsonObject:
            items = self._json_list(section["items"])
            return {
                **section,
                "items": items[:count],
                "truncated": bool(section["truncated"]) or count < len(items),
                "omitted": False,
            }

        def disclose(sections: _ContextSections) -> JsonObject:
            candidate = {
                **payload,
                "records": sections.records,
                "current_state": sections.current_state,
                "waiting": sections.waiting,
                "overdue": sections.overdue,
                "preferences": sections.preferences,
            }
            if len(sections.records) < len(full.records):
                candidate["item_count"] = len(sections.records)
                candidate["char_count"] = sum(
                    len(
                        json.dumps(
                            self._json_object(item).get("payload"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                    for item in sections.records
                )
                candidate["truncated"] = True
            if fingerprint is not None:
                record_ids = [
                    str(self._json_object(item)["record_id"]) for item in sections.records
                ]
                for section in (sections.current_state, sections.overdue, sections.preferences):
                    record_ids.extend(
                        str(self._json_object(item)["id"])
                        for item in self._json_list(section["items"])
                    )
                candidate["plan_token"] = store.create_plan_token(
                    record_ids, fingerprint=fingerprint
                )
            # Always start from canonical values, never re-sanitize a disclosure.
            return self._json_object(self._dependencies.sanitize(candidate))

        def size(value: JsonValue) -> int:
            return len(json.dumps(value, ensure_ascii=False))

        def fill(
            chosen: _ContextSections, safe: JsonObject, name: _SectionName, ceiling: int
        ) -> tuple[_ContextSections, JsonObject]:
            """Grow one section an item at a time while it and the whole context fit."""

            section = full.section(name)
            for count in range(1, len(self._json_list(section["items"])) + 1):
                trial = chosen.with_section(name, portion(section, count))
                candidate = disclose(trial)
                if size(candidate[name]) > ceiling or size(candidate) > request.max_chars:
                    break
                chosen, safe = trial, candidate
            return chosen, safe

        chosen = _ContextSections(
            records=[],
            current_state=withheld(full.current_state),
            waiting=dict(exhausted),
            overdue=withheld(full.overdue),
            preferences=withheld(full.preferences),
        )
        safe = disclose(chosen)
        if size(safe) > request.max_chars:
            self._raise(
                GraphReadErrorCategory.INVALID_REQUEST, "context budget cannot fit its metadata"
            )
        counted = replace(chosen, waiting=full.waiting)
        candidate = disclose(counted)
        if size(candidate) <= request.max_chars:
            chosen, safe = counted, candidate
        chosen, safe = fill(
            chosen,
            safe,
            "current_state",
            min(CURRENT_STATE_CHARS, request.max_chars // CURRENT_STATE_BUDGET_DIVISOR),
        )
        records = full.records
        while records:
            candidate = disclose(replace(chosen, records=records))
            if size(candidate) <= request.max_chars:
                chosen, safe = replace(chosen, records=records), candidate
                break
            records = records[:-1]
        chosen, safe = fill(chosen, safe, "overdue", OVERDUE_CHARS)
        chosen, safe = fill(chosen, safe, "preferences", PREFERENCE_CHARS)
        return safe

    def review_queue(self, request: ReviewQueueRequest) -> GraphReadResult:
        """List what waits on a person in one existing subscription."""

        return self._subscription_read(
            "review_queue",
            request.session,
            request.scope,
            lambda store, subscription_id: self._json_object(
                store.review_queue(subscription_id, limit=request.limit)
            ),
        )

    def fold_candidates(self, request: FoldCandidatesRequest) -> GraphReadResult:
        """List unfolded notes and the claim slots that could absorb them."""

        return self._subscription_read(
            "fold_candidates",
            request.session,
            request.scope,
            lambda store, subscription_id: self._json_object(
                store.fold_candidates(
                    subscription_id,
                    subject_id=request.subject_id,
                    family=request.family,
                    limit=request.limit,
                )
            ),
        )

    def review_answers(self, request: ReviewAnswersRequest) -> GraphReadResult:
        """Turn reviewed answers into one capture packet without saving anything.

        Records outside the subscription, sensitive records, and answers that do
        not fit a record's current status are refused as ``INVALID_REQUEST``.
        The packet repeats stored titles exactly, so compose this service
        without a review sanitizer and keep the result in private storage.
        """

        def read(store: GraphReadStore, subscription_id: str) -> JsonObject:
            records = store.scoped_records(subscription_id, request.document.record_ids())
            try:
                built = build_answers_packet(request.document, records)
            except AnswersError as error:
                self._raise(GraphReadErrorCategory.INVALID_REQUEST, str(error))
            return {
                "packet": built["packet"],
                "answers": [
                    {"id": item["id"], "collection": item["collection"], "answer": item["answer"]}
                    for item in built["answers"]
                ],
            }

        return self._subscription_read("review_answers", request.session, request.scope, read)

    def _subscription_read(
        self,
        operation: str,
        session: GraphReadSession,
        scope: GraphReadScope,
        read: Callable[[GraphReadStore, str], JsonObject],
    ) -> GraphReadResult:
        """Run one read that only an exact, owned subscription may perform."""

        self._validate_scope(session, scope)
        if scope.kind is not ReadScopeKind.SUBSCRIPTION:
            self._raise(
                GraphReadErrorCategory.SCOPE_DENIED,
                f"{operation.replace('_', ' ')} requires subscription scope",
            )
        subscription_id = self._require_subscription(session, scope)

        def callback(store: GraphReadStore, _path: Path) -> JsonObject:
            self._validate_subscription_owner(store, subscription_id, session)
            return read(store, subscription_id)

        return self._execute(operation, callback)

    def search_memory(self, request: SearchMemoryRequest) -> GraphReadResult:
        self._validate_scope(request.session, request.scope)
        if request.scope.kind is not ReadScopeKind.SUBSCRIPTION:
            self._raise(GraphReadErrorCategory.SCOPE_DENIED, "search requires subscription scope")
        subscription_id = self._require_subscription(request.session, request.scope)
        if (
            not isinstance(request.query, str)
            or not request.query.strip()
            or len(request.query) > self._dependencies.limits.max_query_chars
        ):
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "search query is invalid")
        limit = self._bounded_positive(request.limit, "search limit")
        if limit > 100:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "search limit is too large")
        max_chars = self._bounded_context_chars(request.max_chars)
        if max_chars < 2:
            self._raise(
                GraphReadErrorCategory.INVALID_REQUEST, "search character budget is too small"
            )
        if request.match not in {"all", "any", "auto"}:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "search match is invalid")

        def operation(store: GraphReadStore, _path: Path) -> JsonObject:
            self._validate_subscription_owner(store, subscription_id, request.session)
            return self._json_object(
                store.search_subscription(
                    subscription_id, request.query, limit=limit, match=request.match
                )
            )

        result = self._execute("search_memory", operation)
        # Budget the disclosed JSON, not raw payloads, and never let the first
        # oversized result escape the cap. Metadata is outside the result budget.
        results = self._json_list(result.payload.get("results"))
        bounded: list[JsonValue] = []
        used = 2  # enclosing list brackets
        for item in results:
            size = len(json.dumps(item, ensure_ascii=False)) + (2 if bounded else 0)
            if used + size > max_chars:
                break
            bounded.append(item)
            used += size
        result.payload["results"] = bounded
        result.payload["result_count"] = len(bounded)
        result.payload["truncated"] = bool(result.payload.get("truncated")) or len(bounded) < len(
            results
        )
        return result

    def pull_changes(self, request: PullChangesRequest) -> GraphReadResult:
        self._validate_scope(request.session, request.scope)
        cursor = self._optional_cursor(request.cursor)
        if request.scope.kind is ReadScopeKind.SUBSCRIPTION:
            subscription_id = self._require_subscription(request.session, request.scope)

            def subscription_operation(store: GraphReadStore, _path: Path) -> JsonObject:
                self._validate_subscription_owner(store, subscription_id, request.session)
                return self._json_object(
                    store.inbox(
                        subscription_id,
                        limit=self._bounded_positive(request.limit, "inbox limit"),
                        cursor=cursor,
                        through_watermark=request.through_watermark,
                    )
                )

            return self._execute("pull_changes", subscription_operation)

        def global_operation(store: GraphReadStore, _path: Path) -> JsonObject:
            if request.since_watermark < 0:
                self._raise(
                    GraphReadErrorCategory.INVALID_REQUEST, "watermark must not be negative"
                )
            if (
                request.through_watermark is not None
                and request.through_watermark < request.since_watermark
            ):
                self._raise(GraphReadErrorCategory.INVALID_REQUEST, "watermark range is invalid")
            return self._json_object(
                store.mutation_feed(
                    request.since_watermark,
                    through_watermark=request.through_watermark,
                    cursor=cursor,
                    limit=self._bounded_positive(request.limit, "change limit"),
                    selector={
                        "all": not bool(request.subject_ids or request.discovery_kinds),
                        "entity_ids": list(request.subject_ids),
                        "record_ids": [],
                        "collections": [],
                        "discovery_kinds": list(request.discovery_kinds),
                    },
                )
            )

        return self._execute("pull_changes", global_operation)

    def _execute(
        self,
        operation: str,
        callback: Callable[[GraphReadStore, Path], JsonObject],
        *,
        disclose: Callable[[GraphReadStore, JsonObject], JsonObject] | None = None,
    ) -> GraphReadResult:
        try:
            config = self._dependencies.load_config()
            path = self._dependencies.resolve_store_path(config)
            self._dependencies.validate_store_path(config, path)
            if self._dependencies.detect_store(path) != "sqlite_v4":
                self._raise(GraphReadErrorCategory.UNAVAILABLE_CAPABILITY, "graph is unavailable")
            key = self._dependencies.load_plan_key(config, path)
            with self._dependencies.open_store(path, readonly=True, plan_hmac_key=key) as store:
                if getattr(store, "readonly", False) is not True:
                    self._raise(
                        GraphReadErrorCategory.UNAVAILABLE_CAPABILITY,
                        "read-only graph access is unavailable",
                    )
                with store.read_snapshot():
                    payload = callback(store, path)
                    # Context must sign its final read set in this same snapshot.
                    safe_payload = (
                        disclose(store, payload)
                        if disclose is not None
                        else self._json_object(self._dependencies.sanitize(payload))
                    )
            return GraphReadResult(
                operation=operation,
                payload=safe_payload,
                warnings=self._warnings(safe_payload),
            )
        except GraphReadError:
            raise
        except Exception as error:
            failure = self._classify_error(error)
            self._raise(failure.category, failure.message)
        raise AssertionError("unreachable")

    def _validate_scope(self, session: GraphReadSession, scope: GraphReadScope) -> None:
        if not isinstance(scope.kind, ReadScopeKind):
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "read scope is invalid")
        if scope.kind is ReadScopeKind.GLOBAL_CLI:
            if scope.subscription_id is not None or session.consumer_id is not None:
                self._raise(
                    GraphReadErrorCategory.SCOPE_DENIED,
                    "global scope cannot use subscription state",
                )
            return
        if scope.kind is ReadScopeKind.SUBSCRIPTION:
            if not isinstance(scope.subscription_id, str) or not scope.subscription_id.strip():
                self._raise(GraphReadErrorCategory.INVALID_REQUEST, "subscription ID is required")
            if self._dependencies.authority_mode is ReadAuthorityMode.TRUSTED_LOCAL_CLI:
                return
            if self._dependencies.authority_mode is not ReadAuthorityMode.EXACT_SESSION:
                self._raise(
                    GraphReadErrorCategory.INVALID_REQUEST, "read authority mode is invalid"
                )
            if not self._nonblank_text(session.consumer_id):
                self._raise(
                    GraphReadErrorCategory.AUTHORITY_DENIED, "subscription consumer is required"
                )
            if not self._nonblank_text(session.agent_id) or not self._nonblank_text(
                session.task_id
            ):
                self._raise(
                    GraphReadErrorCategory.AUTHORITY_DENIED, "subscription owner is required"
                )
            return

    def _require_subscription(self, session: GraphReadSession, scope: GraphReadScope) -> str:
        self._validate_scope(session, scope)
        subscription_id = scope.subscription_id
        if subscription_id is None:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "subscription ID is required")
        if len(subscription_id) > self._dependencies.limits.max_subscription_identity_chars:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "subscription ID is too large")
        return subscription_id.strip()

    def _validate_subscription_owner(
        self,
        store: GraphReadStore,
        subscription_id: str,
        session: GraphReadSession,
    ) -> None:
        try:
            identity = store.subscription_identity(subscription_id)
        except GraphReadError:
            raise
        except ValueError:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "subscription ID is invalid")
        # Backend failures retain their safe category through _execute; a failed
        # lookup does not establish that an active subscription is missing.
        if not isinstance(identity, Mapping) or not bool(identity.get("active")):
            self._raise(GraphReadErrorCategory.SCOPE_DENIED, "subscription scope is unavailable")
        if identity.get("subscription_id") != subscription_id:
            self._raise(GraphReadErrorCategory.SCOPE_DENIED, "subscription scope is unavailable")
        if session.consumer_id is not None and identity.get("consumer_id") != session.consumer_id:
            self._raise(
                GraphReadErrorCategory.SCOPE_DENIED, "subscription scope does not match consumer"
            )
        if self._dependencies.authority_mode is ReadAuthorityMode.TRUSTED_LOCAL_CLI:
            return
        if (
            identity.get("agent_id") != session.agent_id
            or identity.get("task_id") != session.task_id
        ):
            self._raise(
                GraphReadErrorCategory.AUTHORITY_DENIED, "subscription owner does not match session"
            )

    def _bounded_positive(self, value: int, label: str) -> int:
        number = self._positive(value, label)
        if number > self._dependencies.limits.max_page_size:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, f"{label} is too large")
        return number

    def _positive(self, value: int, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, f"{label} must be positive")
        return value

    def _bounded_context_chars(self, value: int) -> int:
        number = self._positive(value, "context character budget")
        if number > self._dependencies.limits.max_context_chars:
            self._raise(
                GraphReadErrorCategory.INVALID_REQUEST,
                "context character budget is too large",
            )
        return number

    def _optional_cursor(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "cursor is invalid")
        cursor = value.strip()
        if not cursor:
            return None
        if len(cursor) > self._dependencies.limits.max_cursor_chars:
            self._raise(GraphReadErrorCategory.INVALID_REQUEST, "cursor is too large")
        return cursor

    @staticmethod
    def _nonblank_text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _truncate_search(self, value: JsonObject, max_chars: int) -> None:
        results = self._json_list(value.get("results"))
        if len(json.dumps(results, ensure_ascii=False)) <= max_chars:
            return
        bounded: list[JsonValue] = []
        used = 2
        for item in results:
            size = len(json.dumps(item, ensure_ascii=False)) + 1
            if bounded and used + size > max_chars:
                break
            bounded.append(item)
            used += size
        value["results"] = bounded
        value["result_count"] = len(bounded)
        value["truncated_by_char_budget"] = True

    @staticmethod
    def _json_object(value: object) -> JsonObject:
        if not isinstance(value, Mapping):
            raise ValueError("graph result is not an object")
        return cast(JsonObject, dict(value))

    @staticmethod
    def _json_list(value: JsonValue | None) -> list[JsonValue]:
        if not isinstance(value, list):
            raise ValueError("graph result list is invalid")
        return value

    def _classify_error(self, error: Exception) -> GraphReadFailure:
        if self._dependencies.classify_error is not None:
            return self._dependencies.classify_error(error)
        if isinstance(error, (OSError, FileNotFoundError)):
            return GraphReadFailure(
                GraphReadErrorCategory.UNAVAILABLE_CAPABILITY, "graph is unavailable"
            )
        if isinstance(error, ValueError):
            return GraphReadFailure(
                GraphReadErrorCategory.INVALID_REQUEST, "graph request is invalid"
            )
        return GraphReadFailure(
            GraphReadErrorCategory.INTERNAL_ERROR, "graph request could not be completed"
        )

    @staticmethod
    def _warnings(payload: JsonObject) -> tuple[str, ...]:
        raw_warnings = payload.get("warnings")
        if not isinstance(raw_warnings, list) or not all(
            isinstance(item, str) for item in raw_warnings
        ):
            return ()
        return tuple(cast(list[str], raw_warnings))

    @staticmethod
    def _raise(category: GraphReadErrorCategory, message: str) -> NoReturn:
        raise GraphReadError(category, message) from None


__all__ = [
    "GraphReadDependencies",
    "GraphReadLimits",
    "GraphReadService",
    "GraphReadStore",
    "ReadAuthorityMode",
]
