"""Source scopes are derived from real disposable graph subscriptions."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import knowledge_graph_v4 as v4
from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceDetails,
    SourceMetadata,
    SourceScope,
)
from tedd_knowledge_graph.v4_application_contracts import (
    GraphReadScope,
    GraphReadSession,
    ReadScopeKind,
)


class SourceAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = SourceScope("store", "generation", ("subject",), "personal_task")
        self.metadata = SourceMetadata(
            self.scope,
            "resource",
            "snapshot",
            "text",
            "local_file",
            "fixture",
            "task",
            "run",
            "encounter",
            "2026-09-06T12:00:00Z",
            details=SourceDetails(
                "local", "owner", "representation", "original", "reader", sensitivity="ordinary"
            ),
        )

    def test_local_retention_needs_separate_audience_and_scope_authority(self) -> None:
        from tedd_knowledge_graph.source_authority import SourceAccess

        access = SourceAccess(self.scope, "model", ("ordinary",), local_retention=True)
        access.authorize(self.metadata, "a" * 64, "inspect")
        for denied in (
            replace(access, local_retention=False),
            replace(access, sensitivities=("medical",)),
            replace(access, scope=replace(self.scope, generation="other")),
        ):
            with self.subTest(access=denied), self.assertRaises(SourceArchiveError):
                denied.authorize(self.metadata, "a" * 64, "inspect")
        with self.assertRaises(SourceArchiveError):
            access.authorize(self.metadata, "a" * 64, "owner_preview")

    def test_cloud_metadata_cannot_grant_retention_or_current_access(self) -> None:
        from tedd_knowledge_graph.source_authority import SourceAccess, access_binding

        assert self.metadata.details is not None
        metadata = replace(self.metadata, details=replace(self.metadata.details, provider="google"))
        access = SourceAccess(self.scope, "model", ("ordinary",), local_retention=True)
        with self.assertRaises(SourceArchiveError):
            access.authorize(metadata, "a" * 64, "inspect")
        retained = replace(access, retained_bindings=(access_binding(metadata, "a" * 64),))
        retained.authorize(metadata, "a" * 64, "inspect")
        with self.assertRaises(SourceArchiveError):
            retained.authorize(metadata, "a" * 64, "capture")
        with self.assertRaises(SourceArchiveError):
            retained.authorize(metadata, "b" * 64, "inspect")

    def test_access_dependent_checks_bind_every_operation_and_audience(self) -> None:
        from tedd_knowledge_graph.source_authority import (
            AccessDecision,
            SourceAccess,
            access_binding,
        )

        assert self.metadata.details is not None
        metadata = replace(
            self.metadata,
            details=replace(self.metadata.details, provider="google", retention="access_dependent"),
        )
        binding = access_binding(metadata, "a" * 64)
        access = SourceAccess(self.scope, "model", ("ordinary",))
        decision = AccessDecision(binding, "inspect", "model", "allowed")
        for result in (
            decision,
            replace(decision, status="denied"),
            replace(decision, status="unknown"),
            replace(decision, binding="b" * 64),
            replace(decision, operation="render"),
            replace(decision, audience="owner"),
        ):

            def provide(
                _metadata: SourceMetadata,
                _payload: str,
                _operation: str,
                _audience: str,
                response: AccessDecision = result,
            ) -> AccessDecision:
                return response

            checked = replace(access, provider_check=provide)
            if result == decision:
                checked.authorize(metadata, "a" * 64, "inspect")
            else:
                with self.subTest(decision=result), self.assertRaises(SourceArchiveError):
                    checked.authorize(metadata, "a" * 64, "inspect")
        assert metadata.details is not None
        for change in (
            replace(metadata, source_id="other"),
            replace(metadata, source_version="other"),
            replace(metadata, details=replace(metadata.details, principal="other")),
        ):
            with self.subTest(metadata=change), self.assertRaises(SourceArchiveError):
                replace(
                    access, provider_check=lambda _m, _hash, _op, _audience: decision
                ).authorize(change, "a" * 64, "inspect")


class SourceAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="tedd-source-scope-", dir=private_temporary_parent()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        self.path = self.root / "graph.sqlite3"
        with v4.create_store(self.path) as store:
            store.apply_changes(
                [
                    v4.RecordChange(
                        "entity_task",
                        "entities",
                        {
                            "id": "entity_task",
                            "namespace": "personal_task",
                            "type": "task",
                            "name": "Neutral support task",
                            "aliases": [],
                            "identity_keys": [],
                            "lifecycle": "active",
                            "status": "current",
                            "sensitive": False,
                        },
                    )
                ],
                fingerprint="scope-fixture",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=0,
            )
            store.register_consumer("consumer", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer", {"entity_ids": ["entity_task"]}, subscription_id="subscription"
            )
            store.subscribe("consumer", {"all": True}, subscription_id="global")
        self.session = GraphReadSession("agent", "task", "run", "consumer")
        self.scope = GraphReadScope(ReadScopeKind.SUBSCRIPTION, "subscription")

    def test_exact_personal_task_scope_needs_no_invented_client(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path, readonly=True) as store:
            state = store.state()
            scope = authorized_scope(store, self.session, self.scope, ("entity_task",))
            self.assertEqual(scope.store_id, state["store_id"])
            self.assertEqual(scope.generation, str(state["generation"]))
            self.assertEqual(scope.subject_ids, ("entity_task",))
            self.assertEqual(store.state(), state)

    def test_wrong_task_consumer_agent_and_subject_never_gain_source_access(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path, readonly=True) as store:
            for session in (
                replace(self.session, task_id="other"),
                replace(self.session, agent_id="other"),
                replace(self.session, consumer_id="other"),
                replace(self.session, run_id=None),
            ):
                with self.subTest(session=session), self.assertRaises(SourceArchiveError):
                    authorized_scope(store, session, self.scope, ("entity_task",))
            with self.assertRaises(SourceArchiveError):
                authorized_scope(store, self.session, self.scope, ("other",))

    def test_global_scope_cannot_be_repurposed_for_source_browsing(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path, readonly=True) as store:
            for scope in (
                GraphReadScope(ReadScopeKind.GLOBAL_CLI),
                GraphReadScope(ReadScopeKind.SUBSCRIPTION, "global"),
            ):
                with self.subTest(scope=scope), self.assertRaises(SourceArchiveError):
                    authorized_scope(store, self.session, scope, ("entity_task",))

    def test_handoff_must_use_the_current_owner_without_registering_or_acknowledging(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path) as writable:
            writable.rebind_consumer(
                "consumer", task_id="task", expected_old_agent_id="agent", new_agent_id="new-agent"
            )
        with v4.open_store(self.path, readonly=True) as store:
            before = store.consumer_status("consumer")
            with self.assertRaises(SourceArchiveError):
                authorized_scope(store, self.session, self.scope, ("entity_task",))
            result = authorized_scope(
                store, replace(self.session, agent_id="new-agent"), self.scope, ("entity_task",)
            )
            self.assertEqual(result.subject_ids, ("entity_task",))
            self.assertEqual(store.consumer_status("consumer"), before)

    def test_requires_readonly_store_and_active_exact_subscription(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path) as store:
            with self.assertRaises(SourceArchiveError):
                authorized_scope(store, self.session, self.scope, ("entity_task",))
            store.connection.execute(
                "UPDATE subscriptions SET active=0 WHERE subscription_id='subscription'"
            )
            store.connection.commit()
        with v4.open_store(self.path, readonly=True) as store:
            with self.assertRaises(SourceArchiveError):
                authorized_scope(store, self.session, self.scope, ("entity_task",))

    def test_namespace_revision_does_not_reuse_the_previous_source_scope(self) -> None:
        from tedd_knowledge_graph.source_authority import authorized_scope

        with v4.open_store(self.path, readonly=True) as store:
            before = authorized_scope(store, self.session, self.scope, ("entity_task",))
        with v4.open_store(self.path) as store:
            current = store.get_record("entity_task")
            payload = current["payload"]
            assert isinstance(payload, dict)
            store.apply_changes(
                [
                    v4.RecordChange(
                        "entity_task",
                        "entities",
                        {**payload, "namespace": "other_task"},
                        expected_revision=current["revision"],
                    )
                ],
                fingerprint="namespace-revision",
                producer_agent_id="agent",
                task_id="task",
                run_id="run",
                based_on_watermark=store.watermark,
            )
        with v4.open_store(self.path, readonly=True) as store:
            after = authorized_scope(store, self.session, self.scope, ("entity_task",))
        self.assertNotEqual(before.key, after.key)


if __name__ == "__main__":
    unittest.main()
