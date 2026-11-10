#!/usr/bin/env python3
"""Behavioral contracts for the typed, read-only graph application service."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NoReturn, cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from tedd_knowledge_graph.v4_application_contracts import (  # noqa: E402
    BrainStatusRequest,
    GraphReadError,
    GraphReadErrorCategory,
    GraphReadScope,
    GraphReadSession,
    JsonObject,
    PullChangesRequest,
    ReadScopeKind,
    RecallContextRequest,
    SearchMemoryRequest,
)
from tedd_knowledge_graph.v4_application_service import (  # noqa: E402
    ErrorClassifier,
    GraphReadDependencies,
    GraphReadService,
    GraphReadStore,
    OpenReadStore,
    ReadAuthorityMode,
    ResultSanitizer,
)
from tedd_knowledge_graph.v4_backend_error_mapping import classify_graph_backend_error  # noqa: E402

from tests.support.graph_cases import (  # noqa: E402
    PLAN_KEY,
    V4TestCase,
    applied,
    change,
    scheduled_commit,  # noqa: E402
)

PATH_CASES = (
    "/Users/example/Library/Application Support/tedd/graph.sqlite",
    "/custom/graphs/client-brain.sqlite",
    r"C:\Users\Example\Application Data\tedd\graph.sqlite",
    r"\\example-host\private share\tedd\graph.sqlite",
)


class _ReadOnlyOpener:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[Path, bool, bytes | str | None]] = []

    def __call__(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> contextlib.AbstractContextManager[GraphReadStore]:
        self.calls.append((path, readonly, plan_hmac_key))
        return cast(
            contextlib.AbstractContextManager[GraphReadStore],
            v4.open_store(path, readonly=readonly, plan_hmac_key=plan_hmac_key),
        )


class _WriteFailingReadStore:
    """A read surface that makes accidental write dispatch immediately observable."""

    readonly = True

    def __init__(self) -> None:
        self.read_calls: list[str] = []

    def read_snapshot(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def state(self) -> Mapping[str, object]:
        self.read_calls.append("state")
        return {"watermark": 4, "store_id": "fixture"}

    def search(
        self, query: str, *, limit: int = 100, collections: Iterable[str] | None = None
    ) -> Mapping[str, object]:
        self.read_calls.append("search")
        return {"query": query, "watermark": 4, "result_count": 0, "results": []}

    def records_referencing(
        self, target_ids: Iterable[str], *, limit: int = 500
    ) -> list[Mapping[str, object]]:
        self.read_calls.append("records_referencing")
        return []

    def mutation_feed(
        self,
        since_watermark: int,
        *,
        through_watermark: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
        selector: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        self.read_calls.append("mutation_feed")
        return {
            "since_watermark": since_watermark,
            "snapshot_watermark": 4,
            "returned_count": 0,
            "complete": True,
            "next_cursor": None,
            "changes": [],
        }

    def consumer_status(self, consumer_id: str) -> Mapping[str, object]:
        self.read_calls.append("consumer_status")
        return {"consumer_id": consumer_id, "found": True}

    def subscription_identity(self, subscription_id: str) -> Mapping[str, object] | None:
        self.read_calls.append("subscription_identity")
        return {
            "subscription_id": subscription_id,
            "consumer_id": "consumer_alpha",
            "agent_id": "agent_alpha",
            "task_id": "task_alpha",
            "active": True,
        }

    def context_pack(
        self, subscription_id: str, *, max_items: int = 100, max_chars: int = 50_000
    ) -> Mapping[str, object]:
        self.read_calls.append("context_pack")
        return {"subscription_id": subscription_id, "records": []}

    def search_subscription(
        self, subscription_id: str, query: str, *, limit: int = 10, match: str = "all"
    ) -> Mapping[str, object]:
        self.read_calls.append("search_subscription")
        return {"subscription_id": subscription_id, "results": [], "truncated": False}

    def review_loops(self, subscription_id: str, **_options: object) -> Mapping[str, object]:
        self.read_calls.append("review_loops")
        return {"subscription_id": subscription_id, "items": [], "truncated": False}

    def preferences(self, subscription_id: str) -> Mapping[str, object]:
        self.read_calls.append("preferences")
        return {
            "label": "stated preferences, with dates",
            "items": [],
            "truncated": False,
            "omitted": False,
        }

    def create_plan_token(self, record_ids: Iterable[str], *, fingerprint: str) -> str:
        self.read_calls.append("create_plan_token")
        return "read-only-token"

    def inbox(
        self,
        subscription_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        through_watermark: int | None = None,
    ) -> Mapping[str, object]:
        self.read_calls.append("inbox")
        return {"subscription_id": subscription_id, "changes": [], "ack_token": "read-only-token"}

    def ack(self, *_: object, **__: object) -> NoReturn:
        raise AssertionError("read service must never acknowledge")

    def register_consumer(self, *_: object, **__: object) -> NoReturn:
        raise AssertionError("read service must never register a consumer")

    def subscribe(self, *_: object, **__: object) -> NoReturn:
        raise AssertionError("read service must never create a subscription")

    def apply_changes(self, *_: object, **__: object) -> NoReturn:
        raise AssertionError("read service must never mutate records")


class _SingleStoreOpener:
    def __init__(self, store: GraphReadStore) -> None:
        self.store = store
        self.calls: list[tuple[Path, bool, bytes | str | None]] = []

    def __call__(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> contextlib.AbstractContextManager[GraphReadStore]:
        self.calls.append((path, readonly, plan_hmac_key))
        return cast(
            contextlib.AbstractContextManager[GraphReadStore], contextlib.nullcontext(self.store)
        )


class GraphReadServiceTests(V4TestCase):
    def test_subscription_context_reports_overdue_selection_even_when_empty(self) -> None:
        self._seed()
        result = self._service().recall_context(
            RecallContextRequest(
                session=self._subscription_session(),
                scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
            )
        )
        self.assertIn("overdue", result.payload)
        overdue = result.payload["overdue"]
        assert isinstance(overdue, dict)
        self.assertEqual(overdue["items"], [])

    def test_subscription_context_never_returns_an_oversized_first_record(self) -> None:
        self._seed()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            applied(
                store,
                [change("record_alpha", name="Synthetic description " * 100, expected_revision=1)],
                fingerprint="oversized-context-record",
            )
        result = self._service().recall_context(
            RecallContextRequest(
                session=self._subscription_session(),
                scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                max_chars=1_000,
            )
        )
        self.assertLessEqual(len(json.dumps(result.payload, ensure_ascii=False)), 1_000)

    def test_subscription_context_refuses_budgets_that_cannot_fit_metadata(self) -> None:
        self._seed()
        for budget in (1, 10, 100):
            with self.subTest(budget=budget), self.assertRaises(GraphReadError) as raised:
                self._service().recall_context(
                    RecallContextRequest(
                        session=self._subscription_session(),
                        scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                        max_chars=budget,
                    )
                )
            self.assertEqual(raised.exception.category, GraphReadErrorCategory.INVALID_REQUEST)

    def test_malformed_prefix_is_invalid_request_not_corrupt_data(self) -> None:
        self._seed()
        with self.assertRaises(GraphReadError) as raised:
            self._service().search_memory(
                SearchMemoryRequest(
                    self._subscription_session(),
                    GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                    query="record**",
                )
            )
        self.assertEqual(GraphReadErrorCategory.INVALID_REQUEST, raised.exception.category)

    def test_search_rejects_an_undeclared_match_mode_before_reading(self) -> None:
        self._seed()
        with self.assertRaises(GraphReadError) as raised:
            self._service().search_memory(
                SearchMemoryRequest(
                    self._subscription_session(),
                    GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                    query="record_alpha",
                    match="fuzzy",
                )
            )
        self.assertEqual(GraphReadErrorCategory.INVALID_REQUEST, raised.exception.category)

    def test_search_uses_exact_subscription_owner_and_never_widens_scope(self) -> None:
        self._seed()
        service = self._service()
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        result = service.search_memory(
            SearchMemoryRequest(self._subscription_session(), scope, query="record_alpha")
        )
        self.assertEqual("search_memory", result.operation)
        self.assertEqual(1, result.payload["result_count"])
        for session in (
            GraphReadSession("agent_alpha", "task_alpha", None, "consumer_beta"),
            GraphReadSession("agent_beta", "task_alpha", None, "consumer_alpha"),
            GraphReadSession("agent_alpha", "task_beta", None, "consumer_alpha"),
        ):
            with self.subTest(session=session), self.assertRaises(GraphReadError):
                service.search_memory(SearchMemoryRequest(session, scope, query="record_alpha"))
        with self.assertRaises(GraphReadError) as raised:
            service.search_memory(
                SearchMemoryRequest(
                    self._global_session(),
                    GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    query="record_alpha",
                )
            )
        self.assertEqual(GraphReadErrorCategory.SCOPE_DENIED, raised.exception.category)

    def test_search_sanitizes_before_strict_character_budget_including_first_record(self) -> None:
        self._seed()

        def expanded(value: JsonObject) -> object:
            value["results"] = [{"record_id": "record_alpha", "summary": "X" * 500}]
            return value

        result = self._service(sanitizer=expanded).search_memory(
            SearchMemoryRequest(
                self._subscription_session(),
                GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                query="record_alpha",
                max_chars=100,
            )
        )
        self.assertEqual([], result.payload["results"])
        self.assertEqual(0, result.payload["result_count"])
        self.assertTrue(result.payload["truncated"])
        self.assertLessEqual(len(json.dumps(result.payload["results"], ensure_ascii=False)), 100)

    def test_owner_validation_and_context_share_a_snapshot(self) -> None:
        self._seed()
        service = self._service()
        request = RecallContextRequest(
            session=self._subscription_session(),
            scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
        )

        def write(writer: v4.KnowledgeGraphV4) -> None:
            writer.rebind_consumer(
                "consumer_alpha",
                task_id="task_alpha",
                expected_old_agent_id="agent_alpha",
                new_agent_id="agent_new",
            )
            applied(
                writer,
                [change("record_alpha", name="After revocation")],
                fingerprint="owner-after",
                expected_revisions={"record_alpha": 1},
            )

        original = v4.KnowledgeGraphV4.subscription_identity
        with scheduled_commit(self.store_path, write) as pause:

            def identity_then_pause(
                reader: v4.KnowledgeGraphV4, subscription_id: str
            ) -> dict[str, object] | None:
                identity = original(reader, subscription_id)
                pause()
                return identity

            with mock.patch.object(
                v4.KnowledgeGraphV4, "subscription_identity", new=identity_then_pause
            ):
                result = service.recall_context(request)

        self.assertEqual(result.payload["watermark"], 1)
        records = cast(list[dict[str, object]], result.payload["records"])
        self.assertEqual([record["revision"] for record in records], [1])
        with self.assertRaises(GraphReadError) as raised:
            service.recall_context(request)
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.AUTHORITY_DENIED)

    def test_composition_classifier_maps_backend_failures_to_safe_public_categories(self) -> None:
        """The private service receives only classified failures, never storage imports."""

        def service_that_fails(error: Exception) -> GraphReadService:
            def fail_config() -> object:
                raise error

            return GraphReadService(
                GraphReadDependencies(
                    load_config=fail_config,
                    resolve_store_path=lambda _config: self.store_path,
                    validate_store_path=lambda _config, _path: None,
                    detect_store=lambda _path: "sqlite_v4",
                    inspect_store=lambda _path: {},
                    load_plan_key=lambda _config, _path: None,
                    open_store=_SingleStoreOpener(_WriteFailingReadStore()),
                    sanitize=lambda value: value,
                    classify_error=lambda caught: classify_graph_backend_error(caught, v4),
                )
            )

        cases = (
            (
                v4.PlanTokenError("private token"),
                GraphReadErrorCategory.INVALID_REQUEST,
                "Malformed signed token",
            ),
            (
                v4.StoreFormatError("private path"),
                GraphReadErrorCategory.UNHEALTHY_GRAPH,
                "graph is unhealthy",
            ),
            (
                v4.ValidationError("private SQL"),
                GraphReadErrorCategory.UNHEALTHY_GRAPH,
                "graph is unhealthy",
            ),
            (
                v4.PathSafetyError("private path"),
                GraphReadErrorCategory.UNAVAILABLE_CAPABILITY,
                "graph is unavailable",
            ),
            (
                OSError("/tmp/private-graph.sqlite"),
                GraphReadErrorCategory.UNAVAILABLE_CAPABILITY,
                "graph is unavailable",
            ),
            (
                v4.ConcurrencyConflict("private revision"),
                GraphReadErrorCategory.CONFLICT,
                "graph state changed; retry request",
            ),
            (
                v4.SubscriptionError("private subscription"),
                GraphReadErrorCategory.SCOPE_DENIED,
                "subscription scope is unavailable",
            ),
            (
                ValueError("malformed cursor secret"),
                GraphReadErrorCategory.INVALID_REQUEST,
                "cursor is invalid",
            ),
        )
        request = BrainStatusRequest(
            session=self._global_session(), scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI)
        )
        for error, category, message in cases:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(GraphReadError) as raised:
                    service_that_fails(error).brain_status(request)
                self.assertEqual(category, raised.exception.category)
                self.assertEqual(message, str(raised.exception))

    def _seed(self) -> None:
        with self.create_store() as store:
            applied(store, [change("record_alpha")], fingerprint="application-service")
            store.register_consumer("consumer_alpha", agent_id="agent_alpha", task_id="task_alpha")
            store.register_consumer("consumer_beta", agent_id="agent_beta", task_id="task_beta")
            store.subscribe(
                "consumer_alpha",
                {"record_ids": ["record_alpha"]},
                subscription_id="subscription_alpha",
                start_watermark=0,
            )
            store.subscribe(
                "consumer_beta",
                {"all": True},
                subscription_id="subscription_beta",
                start_watermark=0,
            )

    def _service(
        self,
        opener: OpenReadStore | None = None,
        *,
        sanitizer: ResultSanitizer | None = None,
        authority_mode: ReadAuthorityMode = ReadAuthorityMode.EXACT_SESSION,
        classifier: ErrorClassifier | None = None,
    ) -> GraphReadService:
        read_opener = opener or _ReadOnlyOpener(self.store_path)

        def identity_sanitizer(value: JsonObject) -> object:
            return value

        return GraphReadService(
            GraphReadDependencies(
                load_config=lambda: {"store": self.store_path, "plan_key": PLAN_KEY},
                resolve_store_path=lambda config: cast(Path, config["store"]),
                validate_store_path=lambda _config, _path: None,
                detect_store=lambda _path: "sqlite_v4",
                inspect_store=lambda _path: {
                    "safe": True,
                    "coordination_scope": "same-machine-only",
                },
                load_plan_key=lambda config, _path: cast(bytes, config["plan_key"]),
                open_store=read_opener,
                sanitize=sanitizer or identity_sanitizer,
                authority_mode=authority_mode,
                classify_error=classifier,
            )
        )

    @staticmethod
    def _global_session() -> GraphReadSession:
        return GraphReadSession(agent_id="cli", task_id="task", run_id="run", consumer_id=None)

    def test_subscription_backend_failures_keep_safe_categories_and_release_snapshot(self) -> None:
        """A failed lookup is not evidence that a subscription is unknown."""

        self._seed()
        cases = (
            (
                sqlite3.OperationalError("private SQL"),
                GraphReadErrorCategory.INTERNAL_ERROR,
                "graph request could not be completed",
            ),
            (
                v4.PathSafetyError("private path"),
                GraphReadErrorCategory.UNAVAILABLE_CAPABILITY,
                "graph is unavailable",
            ),
            (
                v4.StoreFormatError("private state"),
                GraphReadErrorCategory.UNHEALTHY_GRAPH,
                "graph is unhealthy",
            ),
            (
                ValueError("private identifier"),
                GraphReadErrorCategory.INVALID_REQUEST,
                "subscription ID is invalid",
            ),
        )
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as reader:
            service = self._service(
                _SingleStoreOpener(cast(GraphReadStore, reader)),
                classifier=lambda error: classify_graph_backend_error(error, v4),
            )
            before = reader.state()
            for operation in ("context", "inbox"):
                for error, category, message in cases:
                    with self.subTest(operation=operation, error=type(error).__name__):
                        with (
                            mock.patch.object(
                                v4.KnowledgeGraphV4, "subscription_identity", side_effect=error
                            ),
                            self.assertRaises(GraphReadError) as raised,
                        ):
                            if operation == "context":
                                service.recall_context(
                                    RecallContextRequest(self._subscription_session(), scope)
                                )
                            else:
                                service.pull_changes(
                                    PullChangesRequest(self._subscription_session(), scope)
                                )
                        self.assertEqual(raised.exception.category, category)
                        self.assertEqual(str(raised.exception), message)
                        self.assertFalse(reader.connection.in_transaction)
            self.assertEqual(reader.state(), before)

    def test_service_failure_releases_snapshot_and_next_call_is_fresh(self) -> None:
        self._seed()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as reader:
            service = self._service(_SingleStoreOpener(cast(GraphReadStore, reader)))
            request = RecallContextRequest(
                session=self._subscription_session(),
                scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                max_items=0,
            )
            with self.assertRaises(GraphReadError):
                service.recall_context(request)
            self.assertFalse(reader.connection.in_transaction)
            with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as writer:
                applied(writer, [change("record_beta")], fingerprint="after-failure")
            result = service.recall_context(
                RecallContextRequest(
                    session=self._subscription_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                )
            )
            self.assertEqual(result.payload["watermark"], 2)
            self.assertFalse(reader.connection.in_transaction)

    @staticmethod
    def _subscription_session() -> GraphReadSession:
        return GraphReadSession(
            agent_id="agent_alpha",
            task_id="task_alpha",
            run_id="untrusted-run-id",
            consumer_id="consumer_alpha",
        )

    def test_global_read_payloads_preserve_query_and_feed_semantics(self) -> None:
        """This fails if the service reshapes search/feed payloads or hides provenance."""

        self._seed()
        service = self._service()
        global_scope = GraphReadScope(ReadScopeKind.GLOBAL_CLI)
        recall = service.recall_context(
            RecallContextRequest(
                session=self._global_session(),
                scope=global_scope,
                query="record_alpha",
                limit=10,
                max_chars=50_000,
            )
        )
        changes = service.pull_changes(
            PullChangesRequest(
                session=self._global_session(),
                scope=global_scope,
                since_watermark=0,
                limit=10,
            )
        )

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(
                recall.payload,
                store.search("record_alpha", limit=10, collections=None),
            )
            self.assertEqual(
                changes.payload,
                store.mutation_feed(
                    0,
                    limit=10,
                    selector={
                        "all": True,
                        "entity_ids": [],
                        "record_ids": [],
                        "collections": [],
                        "discovery_kinds": [],
                    },
                ),
            )
        self.assertIn("producer", cast(list[dict[str, object]], changes.payload["changes"])[0])

    def test_subscription_reads_require_exact_consumer_owner_not_run_id(self) -> None:
        """This fails if a run ID or a capped status list becomes subscription authority."""

        self._seed()
        service = self._service()
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        context = service.recall_context(
            RecallContextRequest(
                session=self._subscription_session(), scope=scope, max_items=10, max_chars=50_000
            )
        )
        inbox = service.pull_changes(
            PullChangesRequest(session=self._subscription_session(), scope=scope, limit=10)
        )

        self.assertEqual(context.operation, "recall_context")
        self.assertEqual(context.payload["subscription_id"], "subscription_alpha")
        self.assertEqual(inbox.payload["subscription_id"], "subscription_alpha")
        self.assertIn("ack_token", inbox.payload)

        mismatch = GraphReadSession(
            agent_id="agent_beta",
            task_id="task_beta",
            run_id="same-untrusted-run-id",
            consumer_id="consumer_alpha",
        )
        with self.assertRaises(GraphReadError) as raised:
            service.recall_context(RecallContextRequest(session=mismatch, scope=scope))
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.AUTHORITY_DENIED)
        self.assertNotIn("agent_beta", str(raised.exception))

    def test_subscription_context_can_request_the_existing_read_only_plan_token(self) -> None:
        """This fails if the explicit plan option disappears or changes context records."""

        self._seed()
        service = self._service()
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        # Exact signed-token parity requires both paths to share an issuance time.
        with mock.patch.object(v4, "utc_now", return_value="2026-09-07T12:00:00+00:00"):
            result = service.recall_context(
                RecallContextRequest(
                    session=self._subscription_session(),
                    scope=scope,
                    max_items=10,
                    max_chars=50_000,
                    plan_fingerprint="application-plan",
                )
            )
            with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
                direct_context = store.context_pack(
                    "subscription_alpha", max_items=10, max_chars=50_000
                )
                direct_records = cast(list[dict[str, object]], direct_context["records"])
                expected_token = store.create_plan_token(
                    [str(record["record_id"]) for record in direct_records],
                    fingerprint="application-plan",
                )
        self.assertEqual(result.payload["records"], direct_context["records"])
        self.assertEqual(result.payload["plan_token"], expected_token)

    def test_subscription_identity_is_an_exact_active_read_not_status_projection(self) -> None:
        """This fails if authority falls back to ``active_subscriptions`` pagination."""

        self._seed()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            identity = store.subscription_identity("subscription_alpha")
        self.assertEqual(
            identity,
            {
                "subscription_id": "subscription_alpha",
                "consumer_id": "consumer_alpha",
                "agent_id": "agent_alpha",
                "task_id": "task_alpha",
                "active": True,
            },
        )

    def test_subscription_context_rejects_cli_budget_overflows(self) -> None:
        """This fails if service requests silently bypass established CLI caps."""

        self._seed()
        service = self._service()
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        with self.assertRaises(GraphReadError) as items_error:
            service.recall_context(
                RecallContextRequest(
                    session=self._subscription_session(), scope=scope, max_items=501
                )
            )
        self.assertEqual(items_error.exception.category, GraphReadErrorCategory.INVALID_REQUEST)
        with self.assertRaises(GraphReadError) as chars_error:
            service.recall_context(
                RecallContextRequest(
                    session=self._subscription_session(), scope=scope, max_chars=100_001
                )
            )
        self.assertEqual(chars_error.exception.category, GraphReadErrorCategory.INVALID_REQUEST)

    def test_brain_status_rejects_subscription_scope_before_consumer_lookup(self) -> None:
        """This fails if a subscription caller can inspect another consumer's state."""

        self._seed()
        opener = _ReadOnlyOpener(self.store_path)
        service = self._service(opener)
        with self.assertRaises(GraphReadError) as raised:
            service.brain_status(
                BrainStatusRequest(
                    session=self._subscription_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                    consumer_id="consumer_beta",
                )
            )
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.SCOPE_DENIED)
        self.assertEqual(opener.calls, [])

    def test_invalid_runtime_scope_kinds_fail_before_any_global_read(self) -> None:
        """This fails if a string or object happens to fall through to global access."""

        self._seed()
        opener = _ReadOnlyOpener(self.store_path)
        service = self._service(opener)
        for invalid_kind in ("global_cli", object()):
            with self.subTest(invalid_kind=type(invalid_kind).__name__):
                with self.assertRaises(GraphReadError) as raised:
                    service.recall_context(
                        RecallContextRequest(
                            session=self._global_session(),
                            scope=GraphReadScope(cast(ReadScopeKind, invalid_kind)),
                            query="record_alpha",
                        )
                    )
                self.assertEqual(raised.exception.category, GraphReadErrorCategory.INVALID_REQUEST)
        self.assertEqual(opener.calls, [])

    def test_invalid_or_missing_subscription_scope_has_safe_deterministic_category(self) -> None:
        """This fails if identity lookup errors leak as internal failures."""

        self._seed()
        service = self._service()
        for subscription_id, category in (
            ("subscription_missing", GraphReadErrorCategory.SCOPE_DENIED),
            (" ", GraphReadErrorCategory.INVALID_REQUEST),
        ):
            with self.subTest(subscription_id=subscription_id):
                with self.assertRaises(GraphReadError) as raised:
                    service.pull_changes(
                        PullChangesRequest(
                            session=self._subscription_session(),
                            scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, subscription_id),
                        )
                    )
                self.assertEqual(raised.exception.category, category)
                if subscription_id.strip():
                    self.assertNotIn(subscription_id.strip(), str(raised.exception))

    def test_inactive_subscription_scope_is_denied_without_backend_detail(self) -> None:
        """This fails if a retired subscription is reported as an internal service fault."""

        self._seed()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.connection.execute(
                "UPDATE subscriptions SET active=0 WHERE subscription_id=?",
                ("subscription_alpha",),
            )
            store.connection.commit()
        with self.assertRaises(GraphReadError) as raised:
            self._service().pull_changes(
                PullChangesRequest(
                    session=self._subscription_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                )
            )
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.SCOPE_DENIED)
        self.assertNotIn("subscription_alpha", str(raised.exception))
        with self.assertRaises(GraphReadError) as raised:
            self._service().search_memory(
                SearchMemoryRequest(
                    self._subscription_session(),
                    GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                    query="record_alpha",
                )
            )
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.SCOPE_DENIED)

    def test_trusted_local_cli_mode_is_explicit_and_does_not_relax_default_session_mode(
        self,
    ) -> None:
        """This fails if local CLI compatibility silently weakens MCP/session authority."""

        self._seed()
        scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        local_session = GraphReadSession(agent_id=None, task_id=None, run_id=None, consumer_id=None)
        with self.assertRaises(GraphReadError) as strict_error:
            self._service().recall_context(RecallContextRequest(session=local_session, scope=scope))
        self.assertEqual(strict_error.exception.category, GraphReadErrorCategory.AUTHORITY_DENIED)
        trusted = self._service(authority_mode=ReadAuthorityMode.TRUSTED_LOCAL_CLI)
        result = trusted.pull_changes(
            PullChangesRequest(session=local_session, scope=scope, limit=10)
        )
        self.assertEqual(result.payload["subscription_id"], "subscription_alpha")

    def test_global_recall_rejects_context_budget_overflow(self) -> None:
        """This fails if global recall accepts a larger character budget than the CLI."""

        self._seed()
        with self.assertRaises(GraphReadError) as raised:
            self._service().recall_context(
                RecallContextRequest(
                    session=self._global_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    query="record_alpha",
                    max_chars=100_001,
                )
            )
        self.assertEqual(raised.exception.category, GraphReadErrorCategory.INVALID_REQUEST)

    def test_change_cursor_is_bounded_and_typed_before_store_access(self) -> None:
        """This fails if malformed cursors become an unredacted backend failure."""

        self._seed()
        opener = _ReadOnlyOpener(self.store_path)
        service = self._service(opener)
        global_scope = GraphReadScope(ReadScopeKind.GLOBAL_CLI)
        for cursor in (cast(str, object()), "x" * 16_385):
            with self.subTest(cursor_type=type(cursor).__name__):
                with self.assertRaises(GraphReadError) as raised:
                    service.pull_changes(
                        PullChangesRequest(
                            session=self._global_session(),
                            scope=global_scope,
                            cursor=cursor,
                        )
                    )
                self.assertEqual(raised.exception.category, GraphReadErrorCategory.INVALID_REQUEST)
        self.assertEqual(opener.calls, [])

    def test_scope_validation_and_read_only_opening_leave_graph_unchanged(self) -> None:
        """This fails if an unscoped read is accepted or a read path writes/acknowledges."""

        self._seed()
        opener = _ReadOnlyOpener(self.store_path)
        service = self._service(opener)
        before_bytes = self.store_path.read_bytes()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            before_state = store.state()
            before_consumer = store.consumer_status("consumer_alpha")

        with self.assertRaises(GraphReadError) as missing_scope:
            service.pull_changes(
                PullChangesRequest(
                    session=self._subscription_session(),
                    scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION),
                )
            )
        self.assertEqual(missing_scope.exception.category, GraphReadErrorCategory.INVALID_REQUEST)

        with self.assertRaises(GraphReadError) as coupled_global:
            service.brain_status(
                BrainStatusRequest(
                    session=self._subscription_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                )
            )
        self.assertEqual(coupled_global.exception.category, GraphReadErrorCategory.SCOPE_DENIED)

        result = service.pull_changes(
            PullChangesRequest(
                session=self._subscription_session(),
                scope=GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha"),
                limit=10,
            )
        )
        self.assertIn("ack_token", result.payload)
        self.assertEqual(self.store_path.read_bytes(), before_bytes)
        self.assertTrue(opener.calls)
        self.assertTrue(all(readonly for _, readonly, _ in opener.calls))
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(store.state(), before_state)
            self.assertEqual(store.consumer_status("consumer_alpha"), before_consumer)

    def test_every_service_operation_preserves_database_key_and_cursor_state(self) -> None:
        """This fails if any nominal read mutates durable graph or key material."""

        self._seed()
        config_path = self.root / "disposable-config.json"
        config_path.write_bytes(b'{"fixture":true}\n')

        def snapshot() -> tuple[object, ...]:
            key_path = v4._key_path(self.store_path)
            key_bytes = key_path.read_bytes() if key_path.exists() else None
            key_size = key_path.stat().st_size if key_path.exists() else None
            with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
                return (
                    self.store_path.read_bytes(),
                    self.store_path.stat().st_size,
                    key_bytes,
                    key_size,
                    config_path.read_bytes(),
                    config_path.stat().st_size,
                    store.state(),
                    store.consumer_status("consumer_alpha"),
                    store.subscription_identity("subscription_alpha"),
                    store.connection.execute("SELECT COUNT(*) FROM mutations").fetchone()[0],
                    store.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                    store.connection.execute(
                        "SELECT last_ack_watermark FROM subscriptions WHERE subscription_id=?",
                        ("subscription_alpha",),
                    ).fetchone()[0],
                )

        service = self._service()
        before = snapshot()
        global_scope = GraphReadScope(ReadScopeKind.GLOBAL_CLI)
        subscription_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        service.brain_status(BrainStatusRequest(session=self._global_session(), scope=global_scope))
        service.recall_context(
            RecallContextRequest(
                session=self._global_session(), scope=global_scope, query="record_alpha"
            )
        )
        service.pull_changes(
            PullChangesRequest(session=self._global_session(), scope=global_scope, limit=1)
        )
        service.recall_context(
            RecallContextRequest(session=self._subscription_session(), scope=subscription_scope)
        )
        service.pull_changes(
            PullChangesRequest(
                session=self._subscription_session(), scope=subscription_scope, limit=1
            )
        )
        service.search_memory(
            SearchMemoryRequest(
                self._subscription_session(), subscription_scope, query="record_alpha"
            )
        )
        self.assertEqual(snapshot(), before)
        self.assertEqual(os.stat(config_path).st_size, len(b'{"fixture":true}\n'))

    def test_all_operations_use_only_read_methods_on_a_write_failing_store(self) -> None:
        """This fails if a read route starts an acknowledgement or other durable write."""

        store = _WriteFailingReadStore()
        opener = _SingleStoreOpener(store)

        service = GraphReadService(
            GraphReadDependencies(
                load_config=lambda: object(),
                resolve_store_path=lambda _config: self.store_path,
                validate_store_path=lambda _config, _path: None,
                detect_store=lambda _path: "sqlite_v4",
                inspect_store=lambda _path: {
                    "safe": True,
                    "coordination_scope": "same-machine-only",
                },
                load_plan_key=lambda _config, _path: None,
                open_store=opener,
                sanitize=lambda value: value,
            )
        )
        global_scope = GraphReadScope(ReadScopeKind.GLOBAL_CLI)
        subscription_scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription_alpha")
        service.brain_status(BrainStatusRequest(session=self._global_session(), scope=global_scope))
        service.recall_context(
            RecallContextRequest(session=self._global_session(), scope=global_scope, query="query")
        )
        service.pull_changes(PullChangesRequest(session=self._global_session(), scope=global_scope))
        service.recall_context(
            RecallContextRequest(session=self._subscription_session(), scope=subscription_scope)
        )
        service.pull_changes(
            PullChangesRequest(session=self._subscription_session(), scope=subscription_scope)
        )
        self.assertEqual(
            store.read_calls,
            [
                "state",
                "search",
                "mutation_feed",
                "subscription_identity",
                "context_pack",
                "review_loops",
                "preferences",
                "subscription_identity",
                "inbox",
            ],
        )
        self.assertTrue(all(readonly for _, readonly, _ in opener.calls))

    def test_brain_status_excludes_records_and_sanitizes_complete_payload(self) -> None:
        """This fails if status leaks records or skips the injected disclosure boundary."""

        self._seed()
        service = self._service(sanitizer=lambda value: {"safe": value["graph"]["watermark"]})
        result = service.brain_status(
            BrainStatusRequest(
                session=self._global_session(),
                scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                consumer_id="consumer_alpha",
            )
        )
        self.assertEqual(result.payload, {"safe": 1})
        self.assertNotIn("records", result.payload)

    def test_brain_status_projects_store_health_without_diagnostics(self) -> None:
        """This fails if a public health result passes through store diagnostics."""

        for path in PATH_CASES:
            with self.subTest(path=path):
                assessment = {
                    "path": path,
                    "filesystem_type": "syntheticfs",
                    "safe": True,
                    "reasons": [f"diagnostic inspected {path}"],
                    "diagnostics": {"path": path, "nested": {"path": path}},
                    "unexpected": "do not expose",
                    "coordination_scope": "same-machine-only",
                }
                service = GraphReadService(
                    GraphReadDependencies(
                        load_config=lambda: object(),
                        resolve_store_path=lambda _config: self.store_path,
                        validate_store_path=lambda _config, _path: None,
                        detect_store=lambda _path: "sqlite_v4",
                        inspect_store=lambda _path, assessment=assessment: assessment,
                        load_plan_key=lambda _config, _path: None,
                        open_store=_SingleStoreOpener(_WriteFailingReadStore()),
                        sanitize=lambda value: value,
                    )
                )

                result = service.brain_status(
                    BrainStatusRequest(
                        session=self._global_session(),
                        scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                    )
                )

                self.assertEqual(
                    result.payload["store"],
                    {"safe": True, "coordination_scope": "same-machine-only"},
                )
                self.assertNotIn("Support/tedd", json.dumps(result.payload))
                self.assertNotIn("client-brain", json.dumps(result.payload))

    def test_brain_status_rejects_malformed_store_health(self) -> None:
        """This fails if missing or malformed health fields reach public output."""

        cases = (
            (
                "truthy safe string",
                {"safe": "true", "coordination_scope": "same-machine-only"},
            ),
            ("missing safe", {"coordination_scope": "same-machine-only"}),
            (
                "numeric safe",
                {"safe": 1, "coordination_scope": "same-machine-only"},
            ),
            ("missing coordination scope", {"safe": True}),
            (
                "incorrect coordination scope",
                {"safe": True, "coordination_scope": "multi-machine"},
            ),
        )

        for label, assessment in cases:
            with self.subTest(label=label):
                service = GraphReadService(
                    GraphReadDependencies(
                        load_config=lambda: object(),
                        resolve_store_path=lambda _config: self.store_path,
                        validate_store_path=lambda _config, _path: None,
                        detect_store=lambda _path: "sqlite_v4",
                        inspect_store=lambda _path, assessment=assessment: assessment,
                        load_plan_key=lambda _config, _path: None,
                        open_store=_SingleStoreOpener(_WriteFailingReadStore()),
                        sanitize=lambda value: value,
                    )
                )

                with self.assertRaises(GraphReadError) as raised:
                    service.brain_status(
                        BrainStatusRequest(
                            session=self._global_session(),
                            scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                        )
                    )

                self.assertEqual(GraphReadErrorCategory.INVALID_REQUEST, raised.exception.category)

    def test_brain_status_preserves_a_valid_unsafe_health_boolean(self) -> None:
        """This fails if the public projection changes a verified unsafe result."""

        service = GraphReadService(
            GraphReadDependencies(
                load_config=lambda: object(),
                resolve_store_path=lambda _config: self.store_path,
                validate_store_path=lambda _config, _path: None,
                detect_store=lambda _path: "sqlite_v4",
                inspect_store=lambda _path: {
                    "safe": False,
                    "coordination_scope": "same-machine-only",
                },
                load_plan_key=lambda _config, _path: None,
                open_store=_SingleStoreOpener(_WriteFailingReadStore()),
                sanitize=lambda value: value,
            )
        )

        result = service.brain_status(
            BrainStatusRequest(
                session=self._global_session(),
                scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
            )
        )

        self.assertEqual(
            {"safe": False, "coordination_scope": "same-machine-only"}, result.payload["store"]
        )

    def test_brain_status_hides_health_when_the_store_is_unavailable(self) -> None:
        """This fails if a non-v4 store exposes any diagnostic health assessment."""

        service = GraphReadService(
            GraphReadDependencies(
                load_config=lambda: object(),
                resolve_store_path=lambda _config: self.store_path,
                validate_store_path=lambda _config, _path: None,
                detect_store=lambda _path: "missing",
                inspect_store=lambda _path: self.fail("unavailable stores must not be inspected"),
                load_plan_key=lambda _config, _path: None,
                open_store=_SingleStoreOpener(_WriteFailingReadStore()),
                sanitize=lambda value: value,
            )
        )

        with self.assertRaises(GraphReadError) as raised:
            service.brain_status(
                BrainStatusRequest(
                    session=self._global_session(),
                    scope=GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                )
            )

        self.assertEqual(GraphReadErrorCategory.UNAVAILABLE_CAPABILITY, raised.exception.category)
        self.assertEqual("graph is unavailable", str(raised.exception))


if __name__ == "__main__":
    import unittest

    unittest.main()
