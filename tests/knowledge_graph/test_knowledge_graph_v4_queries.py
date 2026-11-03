#!/usr/bin/env python3
"""Compatibility contracts for the extracted v4 schema and query kernels."""

from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph import v4_queries, v4_schema  # noqa: E402

from tests.support.graph_cases import (  # noqa: E402
    PLAN_KEY as FIXTURE_PLAN_KEY,
)
from tests.support.graph_cases import (  # noqa: E402
    V4TestCase,
    applied,
    change,
    current_claim_key,
    scheduled_commit,  # noqa: E402
    surface_key,
)

PLAN_KEY = bytes.fromhex("77" * 32)

EXPECTED_PUBLIC_EXPORTS = [
    "SCHEMA_VERSION",
    "SCHEMA_USER_VERSION",
    "COLLECTIONS",
    "RecordChange",
    "KnowledgeGraphV4",
    "V4Error",
    "PathSafetyError",
    "StoreFormatError",
    "ConcurrencyConflict",
    "RevisionConflictError",
    "IdentityConflict",
    "PlanTokenError",
    "SubscriptionError",
    "ValidationError",
    "detect_store",
    "inspect_store_path",
    "create_store",
    "open_store",
    "import_v3_graph",
    "sign_plan_token",
    "verify_plan_token",
]

EXPECTED_QUERY_SIGNATURES = {
    "state": "(self) -> '_v4_queries.GraphState'",
    "current_revisions": (
        "(self, record_ids: 'Iterable[str]') -> 'dict[str, _v4_queries.CurrentRevision | None]'"
    ),
    "get_record": (
        "(self, record_id: 'str', revision: 'int | None' = None) -> '_v4_queries.RecordSnapshot'"
    ),
    "get_records": ("(self, record_ids: 'Iterable[str]') -> 'list[_v4_queries.RecordSnapshot]'"),
    "history": (
        "(self, record_id: 'str', *, limit: 'int' = 100, "
        "before_revision: 'int | None' = None) -> '_v4_queries.HistoryResult'"
    ),
    "find_entities": (
        "(self, *, identity_keys: 'Sequence[Mapping[str, object]]' = (), "
        "namespace: 'str | None' = None, entity_type: 'str | None' = None, "
        "names: 'Sequence[str]' = (), limit: 'int' = 100) -> "
        "'list[_v4_queries.RecordSnapshot]'"
    ),
    "records_referencing": (
        "(self, target_ids: 'Iterable[str]', *, ref_kinds: "
        "'Iterable[str] | None' = None, limit: 'int' = 500) -> "
        "'list[_v4_queries.RecordSnapshot]'"
    ),
    "find_claim_surfaces": (
        "(self, *, surface_id: 'str | None' = None, family: 'str | None' = None, "
        "key: 'str | None' = None, subject_signature: 'str | None' = None, "
        "subject_ids: 'Sequence[str] | None' = None, limit: 'int' = 100) -> "
        "'list[_v4_queries.RecordSnapshot]'"
    ),
    "current_claims": (
        "(self, surface_ids: 'Iterable[str]', *, limit: 'int' = 500) -> "
        "'list[_v4_queries.RecordSnapshot]'"
    ),
    "semantic_key_owner": (
        "(self, collection: 'str', key_kind: 'str', key: 'str') -> 'str | None'"
    ),
    "search": (
        "(self, query: 'str', *, limit: 'int' = 20, collections: "
        "'Iterable[str] | None' = None) -> 'dict[str, object]'"
    ),
}


class V4SchemaAndQueryFacadeTests(unittest.TestCase):
    def test_flat_module_preserves_schema_and_public_exports(self) -> None:
        self.assertIs(v4.DDL, v4_schema.DDL)
        self.assertEqual(v4.SCHEMA_VERSION, v4_schema.SCHEMA_VERSION)
        self.assertEqual(v4.SCHEMA_USER_VERSION, v4_schema.SCHEMA_USER_VERSION)
        self.assertEqual(
            hashlib.sha256(v4.DDL.encode("utf-8")).hexdigest(),
            "e406393123dd255b25d424abef85fe919cd63c2283f2b510555672a4d181072b",
        )
        self.assertEqual(v4.__all__, EXPECTED_PUBLIC_EXPORTS)

    def test_query_method_signatures_remain_exact(self) -> None:
        observed = {
            name: str(inspect.signature(getattr(v4.KnowledgeGraphV4, name)))
            for name in EXPECTED_QUERY_SIGNATURES
        }
        self.assertEqual(observed, EXPECTED_QUERY_SIGNATURES)

    def test_facade_delegates_with_call_time_policy(self) -> None:
        expected: v4_queries.RecordSnapshot = {
            "record_id": "entity_delegated",
            "collection": "entities",
            "revision": 1,
            "watermark": 1,
            "payload_hash": "hash",
            "payload_available": True,
            "payload": {"id": "entity_delegated"},
        }
        observed_dependencies: list[v4_queries.QueryDependencies] = []

        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        with tempfile.TemporaryDirectory(
            prefix="kg-v4-query-test-", dir=private_parent
        ) as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            store_path = root / "graph.sqlite3"
            with v4.create_store(store_path, plan_hmac_key=PLAN_KEY) as store:
                real_validator = v4._valid_record_id

                def delegated(
                    connection: object,
                    record_id: str,
                    revision: int | None = None,
                    *,
                    dependencies: v4_queries.QueryDependencies,
                ) -> v4_queries.RecordSnapshot:
                    self.assertIs(connection, store.connection)
                    self.assertEqual(record_id, "entity_delegated")
                    self.assertIsNone(revision)
                    observed_dependencies.append(dependencies)
                    return expected

                with (
                    mock.patch.object(
                        v4,
                        "_valid_record_id",
                        autospec=True,
                        side_effect=real_validator,
                    ) as validator,
                    mock.patch.object(
                        v4_queries,
                        "get_record",
                        autospec=True,
                        side_effect=delegated,
                    ) as query_kernel,
                ):
                    result = store.get_record("entity_delegated")

        self.assertIs(result, expected)
        query_kernel.assert_called_once()
        self.assertEqual(len(observed_dependencies), 1)
        self.assertIs(observed_dependencies[0].valid_record_id, validator)


class V4QueryConsistencyTests(V4TestCase):
    def test_subscription_prefix_search_requires_all_terms_and_preserves_literal_queries(
        self,
    ) -> None:
        with self.create_store() as store:
            applied(
                store,
                [
                    change("approval", name="Harborstudio café owner's logo approval"),
                    change("approved", name="Harborside café owners' logo approved"),
                    change("other", name="Harborstudio palette proposed"),
                ],
                fingerprint="prefix-terms",
            )
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe("reader", {"all": True}, subscription_id="scope")
            before = store.state()
            cases: tuple[tuple[str, list[str]], ...] = (
                ("harbor* logo approv*", ["approval", "approved"]),
                ('"café" logo approv*', ["approval", "approved"]),
                ("HARBOR* logo approved", ["approved"]),
                ("owner's logo approv*", ["approval"]),
                ("owners' logo approv*", ["approved"]),
                ("harborſide* logo approv*", ["approved"]),
                ("harbor* palette approv*", []),
                ("harbor* logo approved*", ["approved"]),
                ("harbor logo", []),
                ("logo OR approval", []),
            )
            for query, expected in cases:
                with self.subTest(query=query):
                    result = store.search_subscription("scope", query)
                    self.assertEqual(expected, [r["record_id"] for r in result["results"]])
            self.assertEqual(before, store.state())

    def test_subscription_prefix_search_rejects_malformed_wildcards(self) -> None:
        with self.create_store() as store:
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe("reader", {"all": True}, subscription_id="scope")
            for query in (
                "*",
                "abc*",
                "*harbor",
                "har*bor",
                "harbor**",
                "harbor*tail",
                '"harbor*"',
                "'harbor*'",
                '"logo harbor* approval"',
                '"logo harbor*',
                "'owner's logo harbor*",
                "'logo harbor* approval'",
                "harbor.*",
                "harbor-*",
                "harbor:*",
                "café*",
                "harbor* OR *",
                "harbor*;",
                "(harbor*)",
                'harbor*"',
                " ".join(f"term{i}*" for i in range(33)),
            ):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    store.search_subscription("scope", query)
            self.assertEqual([], store.search_subscription("scope", "abcd*")["results"])

    def test_subscription_prefix_search_filters_hidden_and_foreign_matches_before_limit(
        self,
    ) -> None:
        with self.create_store() as store:
            applied(store, [change("visible", name="Logo approval")], fingerprint="visible-prefix")
            hidden = [change(f"hidden_{i}", name="Logo approved") for i in range(30)]
            for item in hidden:
                item["payload"]["sensitive"] = True
            erased = change("erased", name="Logo approved")
            erased["payload"]["lifecycle"] = "tombstoned"
            applied(store, [*hidden, erased], fingerprint="hidden-prefix")
            applied(
                store,
                [change(f"foreign_{i}", name="Logo approved") for i in range(30)],
                fingerprint="foreign-prefix",
            )
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe(
                "reader",
                {"record_ids": ["visible", "erased", *[f"hidden_{i}" for i in range(30)]]},
                subscription_id="scope",
            )
            result = store.search_subscription("scope", "logo approv*", limit=1)
            self.assertEqual(["visible"], [r["record_id"] for r in result["results"]])
            self.assertFalse(result["truncated"])

    def test_subscription_search_uses_existing_selector_union_semantics(self) -> None:
        with self.create_store() as store:
            applied(
                store,
                [
                    change("subject", name="Logo project"),
                    change("linked", name="Logo approval", refs=[{"target_id": "subject"}]),
                    change("unlinked", name="Logo task"),
                ],
                fingerprint="selector-fixture",
            )
            store.register_consumer("reader", agent_id="agent", task_id="task")
            cases: tuple[tuple[dict[str, object], list[str]], ...] = (
                ({"record_ids": ["subject"]}, ["subject"]),
                ({"entity_ids": ["subject"]}, ["linked", "subject"]),
                ({"collections": ["entities"]}, ["linked", "subject", "unlinked"]),
                ({"discovery_kinds": ["reusable_lesson"]}, ["linked", "subject", "unlinked"]),
                ({"discovery_kinds": ["no-matching-kind"]}, []),
                ({"all": True}, ["linked", "subject", "unlinked"]),
                ({"record_ids": ["subject"], "entity_ids": ["subject"]}, ["linked", "subject"]),
            )
            for i, (selector, expected) in enumerate(cases):
                with self.subTest(selector=selector):
                    scope = f"scope_{i}"
                    store.subscribe("reader", selector, subscription_id=scope)
                    result = store.search_subscription(scope, "logo")
                    self.assertEqual(expected, [r["record_id"] for r in result["results"]])

    def test_subscription_search_literal_terms_unicode_caps_and_stable_order(self) -> None:
        with self.create_store() as store:
            applied(store, [change("first", name="Café logo approved")], fingerprint="first")
            applied(store, [change("second", name="Café logo proposed")], fingerprint="second")
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe("reader", {"all": True}, subscription_id="scope")
            self.assertEqual(
                ["first"],
                [
                    r["record_id"]
                    for r in store.search_subscription("scope", '"café" approved')["results"]
                ],
            )
            self.assertEqual([], store.search_subscription("scope", "logo OR approved")["results"])
            result = store.search_subscription("scope", "logo", limit=1)
            self.assertEqual(["second"], [r["record_id"] for r in result["results"]])
            self.assertTrue(result["truncated"])
            applied(
                store, [change("foreign", name="first logo approved")], fingerprint="exact-priority"
            )
            result = store.search_subscription("scope", "first", limit=1)
            self.assertEqual(["first"], [r["record_id"] for r in result["results"]])
            for query in (
                "",
                " " * 20,
                "!@#$",
                "x" * 2001,
                " ".join(f"term{i}" for i in range(33)),
            ):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    store.search_subscription("scope", query)
            for limit in (0, 101, True):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    store.search_subscription("scope", "logo", limit=limit)

    def test_subscription_search_finds_old_approval_without_recent_context_replacement(
        self,
    ) -> None:
        with self.create_store() as store:
            approved = change("approval_old", name="Logo approved")
            approved["payload"]["authority_state"] = "accepted"
            applied(store, [approved], fingerprint="old-approval")
            applied(
                store,
                [change(f"activity_{i}", name="Meeting notes") for i in range(20)],
                fingerprint="recent-activity",
            )
            applied(
                store,
                [change(f"other_{i}", name="Logo approved") for i in range(150)],
                fingerprint="foreign-matches",
            )
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe(
                "reader",
                {"record_ids": ["approval_old", *[f"activity_{i}" for i in range(20)]]},
                subscription_id="scope",
                start_watermark=0,
            )
            before = store.state()
            context = store.context_pack("scope", max_items=3)
            result = store.search_subscription("scope", "logo approved", limit=1)
            self.assertNotIn("approval_old", str(context["records"]))
            self.assertEqual(["approval_old"], [r["record_id"] for r in result["results"]])
            self.assertEqual(
                "accepted",
                cast(dict[str, object], result["results"][0]["payload"])["authority_state"],
            )
            self.assertEqual(context, store.context_pack("scope", max_items=3))
            self.assertEqual(before, store.state())

    def test_subscription_search_enforces_scope_and_privacy_before_exact_ids_and_limits(
        self,
    ) -> None:
        with self.create_store() as store:
            hidden = change("hidden", name="Logo approved")
            hidden["payload"]["sensitive"] = True
            unavailable = change("erased", name="Logo approved")
            unavailable["payload"]["lifecycle"] = "redacted"
            applied(store, [change("visible", name="Logo approved")], fingerprint="visible")
            applied(store, [hidden, unavailable], fingerprint="hidden")
            applied(store, [change("foreign", name="Logo approved")], fingerprint="foreign")
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe(
                "reader",
                {"record_ids": ["visible", "hidden", "erased"]},
                subscription_id="scope",
                start_watermark=0,
            )
            result = store.search_subscription("scope", "logo approved", limit=1)
            self.assertEqual(["visible"], [r["record_id"] for r in result["results"]])
            for query in ("hidden", "erased", "foreign", "no-such-memory"):
                with self.subTest(query=query):
                    result = store.search_subscription("scope", query)
                    self.assertEqual([], result["results"])
                    self.assertEqual(0, result["result_count"])
                    self.assertFalse(result["truncated"])

    def test_subscription_search_preserves_snapshot_across_concurrent_revision(self) -> None:
        with self.create_store() as store:
            applied(store, [change("record", name="Before")], fingerprint="before")
            store.register_consumer("reader", agent_id="agent", task_id="task")
            store.subscribe("reader", {"all": True}, subscription_id="scope")

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(
                writer,
                [change("record", name="After")],
                fingerprint="after",
                expected_revisions={"record": 1},
            )

        with v4.open_store(
            self.store_path, readonly=True, plan_hmac_key=FIXTURE_PLAN_KEY
        ) as reader:
            original = reader.get_records
            with scheduled_commit(self.store_path, write) as pause:

                def get_then_pause(ids: Iterable[str]) -> list[v4_queries.RecordSnapshot]:
                    records = original(ids)
                    pause()
                    return records

                with mock.patch.object(reader, "get_records", side_effect=get_then_pause):
                    result = reader.search_subscription("scope", "Before")
            self.assertEqual(1, result["watermark"])
            self.assertEqual(1, result["results"][0]["revision"])
            self.assertFalse(reader.connection.in_transaction)
            self.assertEqual([], reader.search_subscription("scope", "Before")["results"])

    def test_candidate_selection_and_record_loading_share_a_snapshot(self) -> None:
        for method in ("find_entities", "records_referencing", "find_claim_surfaces"):
            with self.subTest(method=method):
                path = self.root / f"{method}.sqlite3"
                with self.create_store(path) as store:
                    applied(store, [change("entity_subject")], fingerprint="subject")
                    candidate = (
                        change(
                            "surface_one",
                            collection="claim_surfaces",
                            namespace="entity_subject",
                            name="state",
                            semantic_keys=[surface_key("test", "state", ["entity_subject"])],
                        )
                        if method == "find_claim_surfaces"
                        else change(
                            "entity_one", name="Before", refs=[{"target_id": "entity_subject"}]
                        )
                    )
                    applied(store, [candidate], fingerprint="candidate-before")

                def write(
                    writer: v4.KnowledgeGraphV4, candidate: dict[str, object] = candidate
                ) -> None:
                    applied(
                        writer,
                        [candidate],
                        fingerprint="candidate-after",
                        expected_revisions={str(candidate["record_id"]): 1},
                    )

                with v4.open_store(path, readonly=True, plan_hmac_key=FIXTURE_PLAN_KEY) as reader:
                    original = reader.get_records
                    with scheduled_commit(path, write) as pause:

                        def pause_then_get(
                            ids: Iterable[str],
                            original: Callable[
                                [Iterable[str]], list[v4_queries.RecordSnapshot]
                            ] = original,
                        ) -> list[v4_queries.RecordSnapshot]:
                            pause()
                            return original(ids)

                        with mock.patch.object(reader, "get_records", side_effect=pause_then_get):
                            if method == "find_entities":
                                records = reader.find_entities(names=["Before"])
                            elif method == "records_referencing":
                                records = reader.records_referencing(["entity_subject"])
                            else:
                                records = reader.find_claim_surfaces(surface_id="surface_one")
                    self.assertEqual(
                        {record["record_id"]: record["revision"] for record in records}[
                            str(candidate["record_id"])
                        ],
                        1,
                    )
                    self.assertFalse(reader.connection.in_transaction)

    def test_plan_token_read_set_and_basis_share_a_snapshot(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one")], fingerprint="plan-before")

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(
                writer,
                [change("entity_one")],
                fingerprint="plan-after",
                expected_revisions={"entity_one": 1},
            )

        with v4.open_store(
            self.store_path, readonly=True, plan_hmac_key=FIXTURE_PLAN_KEY
        ) as reader:
            original = reader.current_revisions
            with scheduled_commit(self.store_path, write) as pause:

                def revisions_then_pause(
                    ids: Iterable[str],
                ) -> dict[str, v4_queries.CurrentRevision | None]:
                    revisions = original(ids)
                    pause()
                    return revisions

                with mock.patch.object(
                    reader, "current_revisions", side_effect=revisions_then_pause
                ):
                    token = reader.create_plan_token(["entity_one"], fingerprint="snapshot")
            decoded = v4.verify_plan_token(token, FIXTURE_PLAN_KEY)
            self.assertEqual(decoded["basis_watermark"], 1)
            self.assertEqual(cast(list[dict[str, object]], decoded["read_set"])[0]["revision"], 1)

    def test_validation_uses_one_logical_snapshot(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one")], fingerprint="validation-before")

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(writer, [change("entity_two")], fingerprint="validation-after")

        with v4.open_store(
            self.store_path, readonly=True, plan_hmac_key=FIXTURE_PLAN_KEY
        ) as reader:
            original = reader.state
            with scheduled_commit(self.store_path, write) as pause:

                def state_then_pause() -> v4_queries.GraphState:
                    state = original()
                    pause()
                    return state

                with mock.patch.object(reader, "state", side_effect=state_then_pause):
                    result = reader.validate(full=True)
            self.assertTrue(result["valid"], result["errors"])
            self.assertEqual(cast(dict[str, object], result["state"])["watermark"], 1)

    def test_search_records_and_watermark_share_a_snapshot(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one", name="Before")], fingerprint="search-before")

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(
                writer,
                [change("entity_one", name="After")],
                fingerprint="search-after",
                expected_revisions={"entity_one": 1},
            )

        with v4.open_store(
            self.store_path, readonly=True, plan_hmac_key=FIXTURE_PLAN_KEY
        ) as reader:
            self.assertEqual(reader.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            original = reader.get_records
            with scheduled_commit(self.store_path, write) as pause:

                def get_then_pause(ids: Iterable[str]) -> list[v4_queries.RecordSnapshot]:
                    records = original(ids)
                    pause()
                    return records

                with mock.patch.object(reader, "get_records", side_effect=get_then_pause):
                    result = reader.search("entity_one")
            self.assertEqual(result["watermark"], 1)
            records = cast(list[v4_queries.RecordSnapshot], result["results"])
            self.assertEqual([record["revision"] for record in records], [1])
            self.assertFalse(reader.connection.in_transaction)
            fresh = reader.search("entity_one")
            self.assertEqual(fresh["watermark"], 2)
            self.assertEqual(
                [
                    record["revision"]
                    for record in cast(list[v4_queries.RecordSnapshot], fresh["results"])
                ],
                [2],
            )

    def test_get_records_remains_on_one_snapshot_during_a_concurrent_commit(self) -> None:
        with self.create_store() as reader:
            applied(
                reader,
                [
                    change("entity_one", name="One before"),
                    change("entity_two", name="Two before"),
                ],
                fingerprint="query-snapshot-before",
            )
            with v4.open_store(self.store_path, plan_hmac_key=FIXTURE_PLAN_KEY) as writer:
                original_load_json = v4_queries._load_json
                writer_committed = False

                def load_after_concurrent_commit(value: str) -> v4_queries.JsonValue:
                    nonlocal writer_committed
                    if not writer_committed:
                        writer_committed = True
                        applied(
                            writer,
                            [
                                change("entity_one", name="One after"),
                                change("entity_two", name="Two after"),
                            ],
                            fingerprint="query-snapshot-after",
                            expected_revisions={"entity_one": 1, "entity_two": 1},
                        )
                    return original_load_json(value)

                with mock.patch.object(
                    v4_queries,
                    "_load_json",
                    side_effect=load_after_concurrent_commit,
                ):
                    records = reader.get_records(["entity_one", "entity_two"])

            self.assertTrue(writer_committed)
            self.assertEqual([item["revision"] for item in records], [1, 1])
            payloads = [cast(dict[str, v4_queries.JsonValue], item["payload"]) for item in records]
            self.assertEqual([item["name"] for item in payloads], ["One before", "Two before"])
            self.assertEqual(
                [item["revision"] for item in reader.get_records(["entity_one", "entity_two"])],
                [2, 2],
            )

    def test_current_claims_uses_the_authoritative_registry(self) -> None:
        with self.create_store() as store:
            self._create_current_claim(store)
            self.assertEqual(
                [item["record_id"] for item in store.current_claims(["surface_one"])],
                ["claim_one"],
            )
            store.connection.execute(
                "DELETE FROM claim_current_registry WHERE surface_id=?",
                ("surface_one",),
            )
            store.connection.commit()

            self.assertEqual(store.current_claims(["surface_one"]), [])

    def test_current_claims_requires_the_current_record_lifecycle(self) -> None:
        with self.create_store() as store:
            self._create_current_claim(store)
            store.connection.execute(
                "UPDATE records SET lifecycle=? WHERE record_id=?",
                ("superseded", "claim_one"),
            )
            store.connection.commit()

            self.assertEqual(store.current_claims(["surface_one"]), [])

    def _create_current_claim(self, store: v4.KnowledgeGraphV4) -> None:
        applied(
            store,
            [change("entity_subject", namespace="client_alpha", name="Subject")],
            fingerprint="query-claim-subject",
        )
        applied(
            store,
            [
                change(
                    "surface_one",
                    collection="claim_surfaces",
                    namespace="entity_subject",
                    name="state",
                    semantic_keys=[surface_key("test", "state", ["entity_subject"])],
                )
            ],
            fingerprint="query-claim-surface",
        )
        applied(
            store,
            [
                change(
                    "claim_one",
                    collection="claims",
                    namespace="surface_one",
                    name="state",
                    value="one",
                    semantic_keys=[current_claim_key("surface_one")],
                )
            ],
            fingerprint="query-claim-current",
        )


if __name__ == "__main__":
    unittest.main()
