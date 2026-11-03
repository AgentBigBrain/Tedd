#!/usr/bin/env python3
"""Facade and leaf-service tests for v4 identity/plan registries."""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph import v4_registry  # noqa: E402

PLAN_KEY = bytes.fromhex("77" * 32)

EXPECTED_SIGNATURES = {
    "_constraint_owner": ("(self, constraint: 'Mapping[str, object]') -> 'str | None'"),
    "create_plan_token": (
        "(self, record_ids: 'Iterable[str]', fingerprint: 'str', *, "
        "constraint_expectations: 'Sequence[Mapping[str, object]]' = ()) -> 'str'"
    ),
    "_verify_capture_plan": (
        "(self, token: 'str', fingerprint: 'str', state: "
        "'Mapping[str, object]') -> 'dict[str, object]'"
    ),
    "register_identity_scheme": (
        "(self, scheme: 'str', *, normalization: 'str' = 'binary', governed: "
        "'bool' = False) -> '_v4_registry.IdentitySchemeResult'"
    ),
    "_current_payload": ("(self, record_id: 'str') -> 'dict[str, object] | None'"),
    "_apply_entity_registry": (
        "(self, record_id: 'str', payload: 'Mapping[str, object]', watermark: "
        "'int', pending_payloads: 'Mapping[str, Mapping[str, object]] | None' = "
        "None) -> 'None'"
    ),
    "_apply_surface_registry": (
        "(self, record_id: 'str', payload: 'Mapping[str, object]', watermark: 'int') -> 'None'"
    ),
    "_apply_semantic_registry": (
        "(self, record_id: 'str', collection: 'str', semantic_keys: "
        "'Sequence[object]', watermark: 'int') -> 'None'"
    ),
}


class V4RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kg-v4-registry-test-", dir=private_parent
        )
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)
        self.store_path = self.root / "graph.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_store(self) -> v4.KnowledgeGraphV4:
        return v4.create_store(self.store_path, plan_hmac_key=PLAN_KEY)


class V4RegistryFacadeTests(V4RegistryTestCase):
    def test_facade_signatures_preserve_defaults_with_typed_boundaries(self) -> None:
        observed = {
            name: str(inspect.signature(getattr(v4.KnowledgeGraphV4, name)))
            for name in EXPECTED_SIGNATURES
        }
        self.assertEqual(observed, EXPECTED_SIGNATURES)

    def test_plan_facade_resolves_live_policy_at_call_time(self) -> None:
        with self.create_store() as store:
            constraint = {"kind": "semantic_owner"}
            with mock.patch.object(
                v4_registry, "constraint_owner", return_value="entity-1"
            ) as delegated:
                self.assertEqual(store._constraint_owner(constraint), "entity-1")
            args, kwargs = delegated.call_args
            self.assertIs(args[0], store.connection)
            self.assertEqual(args[1], constraint)
            constraint_dependencies = kwargs["dependencies"]
            self.assertIs(constraint_dependencies.normalize_key, v4.normalize_key)
            self.assertIs(
                constraint_dependencies.semantic_key_owner.__self__,
                store,
            )

            expectations = ({"kind": "identity_owner", "scheme": "email"},)
            with mock.patch.object(
                v4_registry, "create_plan_token", return_value="signed-plan"
            ) as delegated:
                token = store.create_plan_token(
                    ["entity-1"],
                    "fingerprint",
                    constraint_expectations=expectations,
                )
            self.assertEqual(token, "signed-plan")
            _, kwargs = delegated.call_args
            self.assertEqual(kwargs["constraint_expectations"], expectations)
            plan_dependencies = kwargs["dependencies"]
            self.assertIs(plan_dependencies.normalize_space, v4.normalize_space)
            self.assertIs(plan_dependencies.compare_digest, v4.hmac.compare_digest)
            self.assertIs(plan_dependencies.constraint_owner.__self__, store)
            self.assertIs(plan_dependencies.require_token_key.__self__, store)

            state: dict[str, object] = {"store_id": "store", "generation": 1}
            with mock.patch.object(
                v4_registry,
                "verify_capture_plan",
                return_value={"type": "capture_plan"},
            ) as delegated:
                verified = store._verify_capture_plan("token", "fingerprint", state)
            self.assertEqual(verified, {"type": "capture_plan"})
            self.assertIs(delegated.call_args.kwargs["dependencies"].state_loader.__self__, store)

    def test_registry_facade_delegates_without_owning_transactions(self) -> None:
        with self.create_store() as store:
            scheme_result: v4_registry.IdentitySchemeResult = {
                "scheme": "email",
                "normalization": "casefold",
                "governed": True,
            }
            with mock.patch.object(
                v4_registry,
                "register_identity_scheme",
                return_value=scheme_result,
            ) as delegated:
                self.assertEqual(
                    store.register_identity_scheme(
                        "email", normalization="casefold", governed=True
                    ),
                    scheme_result,
                )
            self.assertIs(delegated.call_args.args[0], store.connection)

            with mock.patch.object(
                v4_registry,
                "current_payload",
                return_value={"id": "entity-1"},
            ) as delegated:
                self.assertEqual(store._current_payload("entity-1"), {"id": "entity-1"})
            self.assertIs(delegated.call_args.args[0], store.connection)

            payload: dict[str, object] = {"id": "entity-1"}
            pending = {"entity-2": {"id": "entity-2"}}
            with (
                mock.patch.object(v4_registry, "apply_entity_registry") as entity,
                mock.patch.object(v4_registry, "apply_surface_registry") as surface,
                mock.patch.object(v4_registry, "apply_semantic_registry") as semantic,
            ):
                store._apply_entity_registry("entity-1", payload, 7, pending)
                store._apply_surface_registry("surface-1", payload, 7)
                store._apply_semantic_registry(
                    "event-1", "events", [{"kind": "episode", "key": "one"}], 7
                )
            self.assertIs(entity.call_args.args[0], store.connection)
            self.assertEqual(entity.call_args.args[4], pending)
            self.assertIs(surface.call_args.args[0], store.connection)
            self.assertIs(semantic.call_args.args[0], store.connection)
            for delegated in (entity, surface, semantic):
                dependencies = delegated.call_args.kwargs["dependencies"]
                self.assertIs(dependencies.current_payload_loader.__self__, store)
                self.assertIs(dependencies.identity_conflict_type, v4.IdentityConflict)


class V4RegistryLeafTests(V4RegistryTestCase):
    def test_service_is_facade_independent_and_explicit_any_free(self) -> None:
        source = Path(v4_registry.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import knowledge_graph_v4", source)
        self.assertNotIn("from knowledge_graph_v4", source)
        self.assertNotIn("Any", source)


if __name__ == "__main__":
    unittest.main()
