#!/usr/bin/env python3
"""Characterize v4 compatibility facades and injected service boundaries."""

from __future__ import annotations

import inspect
import types
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph as legacy
import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture
import knowledge_graph_v4_integration as integration
from tedd_knowledge_graph import v4_capture as capture_service
from tedd_knowledge_graph import v4_store as store_service
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest


def _parameter_contract(
    callable_value: types.FunctionType,
) -> tuple[tuple[object, ...], ...]:
    signature = inspect.signature(callable_value)
    return tuple(
        (
            parameter.name,
            parameter.kind,
            "required" if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    )


class FacadeSignatureTests(unittest.TestCase):
    def test_public_facade_call_shapes_remain_stable(self) -> None:
        positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
        keyword = inspect.Parameter.KEYWORD_ONLY
        required = "required"
        expected = {
            v4.create_store: (
                ("path", positional, required),
                ("plan_hmac_key", keyword, None),
            ),
            v4.open_store: (
                ("path", positional, required),
                ("readonly", keyword, False),
                ("plan_hmac_key", keyword, None),
            ),
            capture.execute_capture: (
                ("store", positional, required),
                ("request", positional, required),
                ("source_host", keyword, None),
            ),
            integration.initialize_v4: (
                ("request", positional, required),
                ("config_path", keyword, required),
                ("lock_timeout", keyword, required),
            ),
            integration.project_v4: (
                ("store_path", positional, required),
                ("vault", keyword, required),
                ("root_name", keyword, required),
                ("mode", keyword, required),
                ("manifest_key", keyword, required),
                ("root_id", keyword, required),
                ("timeout_seconds", keyword, required),
                ("force_current_digest", keyword, None),
            ),
            integration.resolve_loop_v4: (
                ("store_path", positional, required),
                ("args", positional, required),
            ),
            integration.redact_v4: (
                ("store_path", positional, required),
                ("args", positional, required),
            ),
        }
        for facade, contract in expected.items():
            with self.subTest(facade=facade.__name__):
                self.assertEqual(
                    _parameter_contract(cast(types.FunctionType, facade)),
                    contract,
                )

    def test_capture_protocol_callable_shapes_match_live_facades(self) -> None:
        pairs = (
            (capture_service.LegacyCapturePolicy, legacy),
            (capture_service.StorageCapturePolicy, v4),
        )
        for protocol, module in pairs:
            annotations = getattr(protocol, "__annotations__", {})
            for attribute in annotations:
                with self.subTest(protocol=protocol.__name__, attribute=attribute):
                    self.assertTrue(hasattr(module, attribute))
            for name, protocol_member in protocol.__dict__.items():
                if name.startswith("_") or not callable(protocol_member):
                    continue
                actual = getattr(module, name)
                with self.subTest(protocol=protocol.__name__, member=name):
                    self.assertEqual(
                        _parameter_contract(protocol_member),
                        _parameter_contract(actual),
                    )

    def test_capture_protocol_critical_return_annotations_are_truthful(self) -> None:
        expected = {
            "scrub_value": "tuple[object, int]",
            "record_text": "str",
            "capture_packet": "dict[str, object]",
        }
        for name, annotation in expected.items():
            protocol_member = getattr(capture_service.LegacyCapturePolicy, name)
            actual = getattr(legacy, name)
            with self.subTest(member=name):
                self.assertEqual(
                    inspect.signature(protocol_member).return_annotation,
                    annotation,
                )
                self.assertEqual(inspect.signature(actual).return_annotation, annotation)
        for name in ("stable_hash", "canonical_json"):
            with self.subTest(member=name):
                self.assertEqual(
                    inspect.signature(
                        getattr(capture_service.StorageCapturePolicy, name)
                    ).return_annotation,
                    "str",
                )
                self.assertEqual(
                    inspect.signature(getattr(v4, name)).return_annotation,
                    "str",
                )


class CallTimeBindingTests(unittest.TestCase):
    def test_store_path_policy_is_resolved_per_call(self) -> None:
        assessment: store_service.StorePathAssessment = {
            "path": "unused.sqlite",
            "filesystem_type": None,
            "safe": True,
            "reasons": [],
            "coordination_scope": "same-machine-only",
        }
        with (
            mock.patch.object(v4, "_mount_type") as mount_type,
            mock.patch.object(v4, "_nearest_existing_parent") as nearest_parent,
            mock.patch.object(v4, "is_link_or_reparse_point") as link_check,
            mock.patch.object(
                store_service, "inspect_store_path", return_value=assessment
            ) as kernel,
        ):
            self.assertEqual(v4.inspect_store_path("unused.sqlite"), assessment)

        dependencies = kernel.call_args.kwargs["dependencies"]
        self.assertIs(dependencies.mount_type, mount_type)
        self.assertIs(dependencies.nearest_existing_parent, nearest_parent)
        self.assertIs(dependencies.is_link_or_reparse_point, link_check)

    def test_store_publication_callbacks_are_resolved_per_call(self) -> None:
        destination = Path("unused.sqlite")
        with (
            mock.patch.object(v4, "_stage_bytes") as stage,
            mock.patch.object(v4, "_publish_staged_file") as publish,
            mock.patch.object(v4, "_fsync_parent") as fsync_parent,
            mock.patch.object(store_service, "atomic_bytes") as kernel,
        ):
            v4._atomic_bytes(destination, b"payload", overwrite=False)

        dependencies = kernel.call_args.kwargs["dependencies"]
        self.assertIs(dependencies.stage_bytes, stage)
        self.assertIs(dependencies.publish_staged_file, publish)
        self.assertIs(dependencies.fsync_parent, fsync_parent)
        self.assertFalse(kernel.call_args.kwargs["overwrite"])

    def test_store_retry_policy_and_callbacks_are_resolved_per_call(self) -> None:
        key_path = Path("unused.plan.key")
        with (
            mock.patch.object(v4, "PLAN_KEY_LINK_RETRY_ATTEMPTS", 7),
            mock.patch.object(v4, "PLAN_KEY_LINK_RETRY_DELAY_SECONDS", 0.125),
            mock.patch.object(v4, "_read_private_plan_key") as read_key,
            mock.patch.object(v4, "_validate_plan_key_stat") as validate_stat,
            mock.patch.object(v4.time, "sleep") as sleep,
            mock.patch.object(
                store_service,
                "read_private_plan_key_after_publication",
                return_value=b"key",
            ) as kernel,
        ):
            self.assertEqual(v4._read_private_plan_key_after_publication(key_path), b"key")

        dependencies = kernel.call_args.kwargs["dependencies"]
        self.assertEqual(dependencies.retry_attempts, 7)
        self.assertEqual(dependencies.retry_delay_seconds, 0.125)
        self.assertIs(dependencies.read_private_plan_key, read_key)
        self.assertIs(dependencies.validate_plan_key_stat, validate_stat)
        self.assertIs(dependencies.sleep, sleep)

    def test_create_store_policy_is_resolved_per_call(self) -> None:
        sentinel = object()
        with (
            mock.patch.object(v4, "_assert_safe_path") as assert_safe,
            mock.patch.object(v4, "detect_store") as detect,
            mock.patch.object(v4, "_connection_pragmas") as pragmas,
            mock.patch.object(v4, "_publish_staged_file") as publish,
            mock.patch.object(v4, "_load_or_create_plan_key") as load_key,
            mock.patch.object(v4, "open_store") as open_store,
            mock.patch.object(v4, "utc_now") as clock,
            mock.patch.object(v4.uuid, "uuid4") as uuid4,
            mock.patch.object(store_service, "create_store", return_value=sentinel) as kernel,
        ):
            self.assertIs(v4.create_store(Path("unused.sqlite")), sentinel)

        dependencies = kernel.call_args.kwargs["dependencies"]
        self.assertIs(dependencies.assert_safe_path, assert_safe)
        self.assertIs(dependencies.detect_store, detect)
        self.assertIs(dependencies.configure_connection, pragmas)
        self.assertIs(dependencies.publish_staged_file, publish)
        self.assertIs(dependencies.load_or_create_plan_key, load_key)
        self.assertIs(dependencies.open_store, open_store)
        self.assertIs(dependencies.utc_now, clock)
        self.assertIs(dependencies.uuid4, uuid4)

    def test_projection_policy_is_resolved_per_call(self) -> None:
        replacement = mock.Mock(return_value={"valid": True})
        with (
            mock.patch.object(legacy, "validate_graph", replacement),
            mock.patch.object(v4, "SCHEMA_VERSION", "v4-test"),
        ):
            dependencies = integration._projection_dependencies()
        self.assertIs(dependencies.validate_graph, replacement)
        self.assertEqual(dependencies.canonical_schema_version, "v4-test")

    def test_capture_facade_injects_current_modules(self) -> None:
        legacy_policy = mock.Mock()
        storage_policy = mock.Mock()
        storage_policy.KnowledgeGraphV4 = object
        expected = {"status": "delegated"}
        request = cast(CaptureRequest, object())
        with (
            mock.patch.object(capture, "_legacy_module", return_value=legacy_policy),
            mock.patch.object(capture, "_storage_module", return_value=storage_policy),
            mock.patch.object(capture_service, "execute_capture", return_value=expected) as kernel,
        ):
            result = capture.execute_capture(object(), request)

        self.assertEqual(result, expected)
        dependencies = kernel.call_args.kwargs["dependencies"]
        self.assertIs(dependencies.legacy, legacy_policy)
        self.assertIs(dependencies.storage, storage_policy)
        self.assertIs(dependencies.store_type, object)

    def test_capture_dependency_binding_rolls_back_after_failure(self) -> None:
        dependencies = capture_service.CaptureDependencies(
            legacy=cast(capture_service.LegacyCapturePolicy, mock.Mock()),
            storage=cast(capture_service.StorageCapturePolicy, mock.Mock()),
            store_type=object,
        )
        with mock.patch.object(
            capture_service, "_execute_capture", side_effect=RuntimeError("boom")
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                capture_service.execute_capture(
                    object(), cast(CaptureRequest, object()), dependencies=dependencies
                )
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            capture_service._dependencies()


if __name__ == "__main__":
    unittest.main()
