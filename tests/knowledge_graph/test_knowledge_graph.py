#!/usr/bin/env python3
"""Focused regression tests for the local knowledge-graph CLI."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable, Protocol, cast
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS

SCRIPT = GRAPH_SCRIPTS / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


class _OwnerCheck(Protocol):
    def __call__(
        self,
        subject: os.stat_result | str | os.PathLike[str] | int,
        *,
        allow_root: bool = False,
    ) -> bool: ...


def private_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    temporary = tempfile.TemporaryDirectory(dir=private_parent)
    try:
        kg.set_private_path_mode(Path(temporary.name), 0o700)
    except Exception:
        temporary.cleanup()
        raise
    return temporary


def make_private_directory(path: Path, *, parents: bool = False) -> None:
    path.mkdir(parents=parents, mode=0o700)
    kg.set_private_path_mode(path, 0o700)


def packet(
    title: str,
    observed_at: str,
    value: str,
    *,
    entity: str = "Test Client Alpha",
    tier: str = "validated_structured_candidate",
    policy: str = "replace",
) -> dict[str, object]:
    return {
        "source": {
            "kind": "http_check",
            "role": "tool",
            "title": title,
            "observed_at": observed_at,
            "source_tier": tier,
            "excerpt": f"Verified plugin version {value}",
        },
        "entities": [
            {
                "ref": "site",
                "type": "site",
                "namespace": entity,
                "name": entity,
                "aliases": ["Client Alpha"],
            }
        ],
        "claims": [
            {
                "family": "deployment",
                "key": "plugin.version",
                "value": value,
                "entity_refs": ["site"],
                "valid_from": observed_at,
                "source_tier": tier,
                "conflict_policy": policy,
            }
        ],
    }


def substantive_analysis_packet() -> dict[str, object]:
    """Return a complete synthetic proposal-review coverage fixture."""

    observed_at = "2026-08-12T18:30:06Z"
    return {
        "source": {
            "kind": "document",
            "role": "assistant",
            "title": "Synthetic proposal response analysis",
            "observed_at": observed_at,
            "source_tier": "validated_structured_candidate",
        },
        "entities": [{"ref": "opportunity", "type": "project", "name": "Synthetic Opportunity"}],
        "observations": [
            {
                "family": "proposal_architecture",
                "key": "synthetic.architecture",
                "value": "A verified headless architecture is proposed.",
                "observed_at": observed_at,
                "entity_refs": ["opportunity"],
            },
            {
                "family": "proposal_risk",
                "key": "synthetic.integration_gap",
                "value": "The integration contract is not yet defined.",
                "observed_at": observed_at,
                "entity_refs": ["opportunity"],
            },
        ],
        "decisions": [
            {
                "id": "decision_synthetic_technical_addendum",
                "title": "Require a technical addendum",
                "summary": "Treat the addendum as an assistant recommendation pending approval.",
                "decided_at": observed_at,
                "status": "accepted",
                "source_tier": "assistant_inference",
                "entity_refs": ["opportunity"],
            }
        ],
        "events": [
            {
                "kind": "proposal_response_review",
                "title": "Synthetic proposal response reviewed",
                "occurred_at": observed_at,
                "entity_refs": ["opportunity"],
            }
        ],
        "receipts": [
            {
                "kind": "artifact_verification",
                "title": "Synthetic proposal review artifact",
                "verified_at": observed_at,
                "verified": True,
                "entity_refs": ["opportunity"],
            }
        ],
        "coverage": {
            "version": "v1",
            "items": [
                {
                    "category": "identities_relationships_status",
                    "summary": "The opportunity identity is durable.",
                    "disposition": "captured",
                    "path": "entities[0]",
                },
                {
                    "category": "verified_current_state",
                    "summary": "The proposed architecture is verified from the response.",
                    "disposition": "captured",
                    "path": "observations[0]",
                },
                {
                    "category": "conclusions_risks_unknowns",
                    "summary": "The integration contract gap is material.",
                    "disposition": "captured",
                    "path": "observations[1]",
                },
                {
                    "category": "recommendations_decisions",
                    "summary": "A technical addendum is recommended but not accepted.",
                    "disposition": "captured",
                    "path": "decisions[0]",
                },
                {
                    "category": "obligations_next_actions",
                    "summary": "No owned implementation obligation exists yet.",
                    "disposition": "not_applicable",
                    "reason": "no_material_outcome",
                    "explanation": "The recommendation has no accepted owner or checkpoint.",
                },
                {
                    "category": "artifacts_events_receipts",
                    "summary": "The review artifact was verified.",
                    "disposition": "captured",
                    "path": "receipts[0]",
                },
            ],
        },
    }


def substantive_coverage_items(packet_value: dict[str, object]) -> list[dict[str, object]]:
    coverage = packet_value.get("coverage")
    if not isinstance(coverage, dict):
        raise AssertionError("substantive fixture coverage must be an object")
    items = coverage.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise AssertionError("substantive fixture coverage items must be objects")
    return cast(list[dict[str, object]], items)


class KnowledgeGraphTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows atomic replace sharing")
    def test_atomic_write_retries_while_destination_reader_is_open(self) -> None:
        with private_temporary_directory() as temporary:
            destination = Path(temporary) / "canonical.json"
            destination.write_text("old\n", encoding="utf-8")
            kg.set_private_path_mode(destination, 0o600)
            reader = destination.open("rb")
            first_sharing_failure = threading.Event()
            writer_errors: list[BaseException] = []
            real_replace = os.replace

            def observed_replace(source: Path, target: Path) -> None:
                try:
                    real_replace(source, target)
                except OSError as error:
                    if (
                        getattr(error, "winerror", None)
                        in kg.WINDOWS_ATOMIC_REPLACE_RETRY_WINERRORS
                    ):
                        first_sharing_failure.set()
                    raise

            def write_replacement() -> None:
                try:
                    kg.atomic_write(destination, "new\n")
                except BaseException as error:
                    writer_errors.append(error)

            writer = threading.Thread(target=write_replacement)
            try:
                with mock.patch.object(kg.os, "replace", observed_replace):
                    writer.start()
                    self.assertTrue(first_sharing_failure.wait(2))
                    reader.close()
                    writer.join(5)
            finally:
                reader.close()
                if writer.ident is not None:
                    writer.join(5)

            self.assertFalse(writer.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertEqual(destination.read_text(encoding="utf-8"), "new\n")

    @unittest.skipUnless(os.name == "nt", "Windows atomic replace sharing")
    def test_atomic_write_bounds_persistent_windows_replace_denial(self) -> None:
        from tests.knowledge_graph.test_knowledge_graph_file_io import ReplaceRetryTests

        with private_temporary_directory() as temporary:
            destination = Path(temporary) / "blocked.json"
            destination.write_text("old\n", encoding="utf-8")
            kg.set_private_path_mode(destination, 0o600)
            denial = PermissionError(13, "Access is denied", str(destination), 5)
            # Reuse the leaf tests' clock; scheduler delay is not a retry failure.
            clock = ReplaceRetryTests.Clock()
            with (
                mock.patch.object(kg, "time", clock),
                mock.patch.object(kg.os, "replace", side_effect=denial) as replace,
                mock.patch.object(kg, "WINDOWS_ATOMIC_REPLACE_TIMEOUT_SECONDS", 0.02),
                mock.patch.object(kg, "WINDOWS_ATOMIC_REPLACE_POLL_SECONDS", 0.001),
                self.assertRaises(PermissionError) as raised,
            ):
                kg.atomic_write(destination, "new\n")
            self.assertIs(raised.exception, denial)
            self.assertAlmostEqual(clock.now, 0.02)
            self.assertGreater(replace.call_count, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows atomic replace sharing")
    def test_atomic_write_does_not_retry_other_windows_replace_errors(self) -> None:
        with private_temporary_directory() as temporary:
            destination = Path(temporary) / "invalid-replace.json"
            destination.write_text("old\n", encoding="utf-8")
            kg.set_private_path_mode(destination, 0o600)
            invalid_parameter = OSError(22, "The parameter is incorrect", str(destination), 87)
            with (
                mock.patch.object(kg.os, "replace", side_effect=invalid_parameter) as replace,
                self.assertRaises(OSError) as raised,
            ):
                kg.atomic_write(destination, "new\n")
            self.assertIs(raised.exception, invalid_parameter)
            replace.assert_called_once()
            self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows lock initialization race")
    def test_existing_lock_waits_for_creator_owner_normalization(self) -> None:
        with private_temporary_directory() as temporary:
            lock_path = Path(temporary) / "initializing.lock"
            creator_in_hardening = threading.Event()
            waiter_observed_initial_owner = threading.Event()
            release_creator = threading.Event()
            initialized = threading.Event()
            call_lock = threading.Lock()
            harden_calls = 0
            reopen_calls = 0
            descriptors: list[int] = []
            errors: list[BaseException] = []
            real_harden = kg.set_private_fd_mode
            real_reopen = cast(Callable[[Path, str, int], int], kg._open_existing_lock_descriptor)

            def harden(descriptor: int, mode: int) -> None:
                nonlocal harden_calls
                with call_lock:
                    harden_calls += 1
                    call_number = harden_calls
                if call_number == 1:
                    creator_in_hardening.set()
                    if not release_creator.wait(5):
                        raise TimeoutError("creator hardening was not released")
                real_harden(descriptor, mode)
                if call_number == 1:
                    initialized.set()

            def reopen(path: Path, label: str, flags: int) -> int:
                nonlocal reopen_calls
                with call_lock:
                    reopen_calls += 1
                return real_reopen(path, label, flags)

            def owner_check(subject: object, **kwargs: object) -> bool:
                if initialized.is_set():
                    return True
                if bool(kwargs.get("allow_root")):
                    waiter_observed_initial_owner.set()
                    return True
                return False

            def open_lock() -> None:
                try:
                    descriptors.append(
                        kg._open_private_lock_descriptor(lock_path, "Test", kg.time.monotonic() + 5)
                    )
                except BaseException as error:
                    errors.append(error)

            creator = threading.Thread(target=open_lock)
            waiter = threading.Thread(target=open_lock)
            try:
                with (
                    mock.patch.object(kg, "set_private_fd_mode", harden),
                    mock.patch.object(kg, "owner_is_current_user", owner_check),
                    mock.patch.object(kg, "_open_existing_lock_descriptor", reopen),
                ):
                    creator.start()
                    self.assertTrue(creator_in_hardening.wait(2))
                    waiter.start()
                    self.assertTrue(waiter_observed_initial_owner.wait(2))
                    release_creator.set()
                    creator.join(5)
                    waiter.join(5)
            finally:
                release_creator.set()
                if creator.ident is not None:
                    creator.join(5)
                if waiter.ident is not None:
                    waiter.join(5)
                for descriptor in descriptors:
                    os.close(descriptor)

            self.assertFalse(creator.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(descriptors), 2)
            self.assertGreaterEqual(reopen_calls, 2)

    def test_lock_hardening_preserves_existing_owner_validation_order(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            make_private_directory(vault)
            real_harden = kg.set_private_fd_mode
            real_owner_check = cast(_OwnerCheck, kg.owner_is_current_user)
            real_validate = kg._validate_open_lock_descriptor

            for lock_context in (
                lambda: kg.store_lock(root / "graph.json"),
                lambda: kg.projection_lock(vault, "Codex Knowledge"),
            ):
                for existing in (False, True):
                    calls: list[str] = []

                    def harden(
                        descriptor: int, mode: int, observed_calls: list[str] = calls
                    ) -> None:
                        observed_calls.append("harden")
                        real_harden(descriptor, mode)

                    def owner_check(
                        subject: os.stat_result | str | os.PathLike[str] | int,
                        observed_calls: list[str] = calls,
                        *,
                        allow_root: bool = False,
                    ) -> bool:
                        observed_calls.append("owner")
                        return real_owner_check(subject, allow_root=allow_root)

                    def validate(
                        descriptor: int,
                        lock_path: Path,
                        label: str,
                        observed_calls: list[str] = calls,
                    ) -> None:
                        observed_calls.append("validate")
                        real_validate(descriptor, lock_path, label)

                    with (
                        mock.patch.object(kg, "set_private_fd_mode", harden),
                        mock.patch.object(kg, "owner_is_current_user", owner_check),
                        mock.patch.object(kg, "_validate_open_lock_descriptor", validate),
                        lock_context(),
                    ):
                        pass

                    if existing:
                        self.assertLess(calls.index("owner"), calls.index("harden"))
                    else:
                        self.assertLess(calls.index("harden"), calls.index("owner"))
                    self.assertLess(
                        max(index for index, call in enumerate(calls) if call == "harden"),
                        max(index for index, call in enumerate(calls) if call == "owner"),
                    )
                    self.assertLess(
                        max(index for index, call in enumerate(calls) if call == "owner"),
                        max(index for index, call in enumerate(calls) if call == "validate"),
                    )

    def test_lock_descriptor_cleanup_handles_base_exceptions(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)

            existing_path = root / "existing-interrupted.lock"
            existing_path.write_text("", encoding="utf-8")
            kg.set_private_path_mode(existing_path, 0o600)
            real_open = os.open
            opened_descriptors: list[int] = []

            def tracked_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                opened_descriptors.append(descriptor)
                return descriptor

            with (
                mock.patch.object(kg.os, "open", tracked_open),
                mock.patch.object(
                    kg,
                    "_validate_open_lock_descriptor",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                kg._open_existing_lock_descriptor(existing_path, "Test", os.O_RDWR)
            self.assertEqual(len(opened_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_descriptors[0])

            waiting_path = root / "waiting-interrupted.lock"
            waiting_path.write_text("", encoding="utf-8")
            waiting_descriptor = os.open(waiting_path, os.O_RDWR)
            with (
                mock.patch.object(kg, "uses_posix_permissions", return_value=False),
                mock.patch.object(kg, "owner_is_current_user", side_effect=SystemExit),
                self.assertRaises(SystemExit),
            ):
                kg._wait_for_windows_lock_initialization(
                    waiting_descriptor,
                    waiting_path,
                    "Test",
                    os.O_RDWR,
                    kg.time.monotonic() + 5,
                )
            with self.assertRaises(OSError):
                os.fstat(waiting_descriptor)

            private_path = root / "private-interrupted.lock"
            private_descriptors: list[int] = []

            def interrupt_hardening(descriptor: int, _mode: int) -> None:
                private_descriptors.append(descriptor)
                raise KeyboardInterrupt

            with (
                mock.patch.object(kg, "set_private_fd_mode", interrupt_hardening),
                self.assertRaises(KeyboardInterrupt),
            ):
                kg._open_private_lock_descriptor(private_path, "Test", kg.time.monotonic() + 5)
            self.assertEqual(len(private_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(private_descriptors[0])

    def test_existing_foreign_lock_is_rejected_without_mutation(self) -> None:
        with private_temporary_directory() as temporary:
            lock_path = Path(temporary) / "existing.lock"
            lock_path.write_text("", encoding="utf-8")
            kg.set_private_path_mode(lock_path, 0o600)
            with (
                mock.patch.object(kg, "owner_is_current_user", return_value=False),
                mock.patch.object(kg, "set_private_fd_mode") as harden,
                self.assertRaisesRegex(ValueError, "not owned"),
            ):
                kg._open_private_lock_descriptor(lock_path, "Test", kg.time.monotonic() + 5)
            harden.assert_not_called()

    def test_disappearing_existing_lock_retries_exclusive_creation(self) -> None:
        with private_temporary_directory() as temporary:
            lock_path = Path(temporary) / "disappearing.lock"
            lock_path.write_text("", encoding="utf-8")
            kg.set_private_path_mode(lock_path, 0o600)
            real_open_existing = cast(
                Callable[[Path, str, int], int], kg._open_existing_lock_descriptor
            )
            open_attempts = 0

            def disappear_before_open(path: Path, label: str, flags: int) -> int:
                nonlocal open_attempts
                open_attempts += 1
                if open_attempts == 1:
                    path.unlink()
                    raise FileNotFoundError(path)
                return real_open_existing(path, label, flags)

            descriptor = -1
            try:
                with mock.patch.object(
                    kg,
                    "_open_existing_lock_descriptor",
                    disappear_before_open,
                ):
                    descriptor = kg._open_private_lock_descriptor(
                        lock_path, "Test", kg.time.monotonic() + 5
                    )
                self.assertTrue(lock_path.is_file())
                self.assertTrue(kg.owner_is_current_user(descriptor))
            finally:
                if descriptor != -1:
                    os.close(descriptor)

    @unittest.skipUnless(os.name == "nt", "Windows lock initialization race")
    def test_incomplete_lock_initialization_times_out_without_mutation(self) -> None:
        with private_temporary_directory() as temporary:
            store_path = Path(temporary) / "incomplete.json"
            lock_path = store_path.with_suffix(".json.lock")
            lock_path.write_text("", encoding="utf-8")
            kg.set_private_path_mode(lock_path, 0o600)

            def owner_check(subject: object, **kwargs: object) -> bool:
                return bool(kwargs.get("allow_root"))

            started = kg.time.monotonic()
            with (
                mock.patch.object(kg, "owner_is_current_user", owner_check),
                mock.patch.object(kg, "set_private_fd_mode") as harden,
                mock.patch.object(kg, "WINDOWS_LOCK_INITIALIZATION_POLL_SECONDS", 0.001),
                self.assertRaisesRegex(ValueError, "not owned"),
            ):
                with kg.store_lock(store_path, timeout_seconds=0.02):
                    self.fail("incomplete lock initialization was accepted")
            self.assertLess(kg.time.monotonic() - started, 0.5)
            harden.assert_not_called()

    def test_temporal_claim_can_return_to_an_older_value(self) -> None:
        graph = kg.blank_graph()
        for title, moment, value in (
            ("Initial", "2026-07-01T12:00:00-04:00", "1.0"),
            ("Upgrade", "2026-07-02T12:00:00-04:00", "2.0"),
            ("Rollback", "2026-07-03T12:00:00-04:00", "1.0"),
        ):
            result = kg.capture_packet(
                graph,
                packet(title, moment, value),
                authority_ceiling="validated_structured_candidate",
                allow_replace=True,
            )
            self.assertEqual(result["status"], "captured")

        self.assertTrue(kg.validate_graph(graph)["valid"])
        self.assertEqual(len(graph["claims"]), 3)
        current = [claim for claim in graph["claims"] if claim["active"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["value"], "1.0")
        self.assertEqual(current[0]["valid_from"], "2026-07-03T16:00:00Z")
        historical = [claim for claim in graph["claims"] if not claim["active"]]
        self.assertEqual({claim["lifecycle"] for claim in historical}, {"superseded"})
        self.assertTrue(all(claim["superseded_by"] for claim in historical))

    def test_claim_surfaces_are_scoped_by_entity(self) -> None:
        graph = kg.blank_graph()
        two_sites = {
            "source": {
                "kind": "document",
                "role": "operator",
                "title": "Two site versions",
                "observed_at": "2026-07-01T12:00:00-04:00",
                "source_tier": "validated_structured_candidate",
            },
            "entities": [
                {"ref": "one", "type": "site", "namespace": "client-one", "name": "Site One"},
                {"ref": "two", "type": "site", "namespace": "client-two", "name": "Site Two"},
            ],
            "claims": [
                {
                    "family": "deployment",
                    "key": "plugin.version",
                    "value": "1.0",
                    "entity_refs": ["one"],
                },
                {
                    "family": "deployment",
                    "key": "plugin.version",
                    "value": "1.0",
                    "entity_refs": ["two"],
                },
            ],
        }
        kg.capture_packet(
            graph,
            two_sites,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(len(graph["claims"]), 2)
        self.assertNotEqual(graph["claims"][0]["id"], graph["claims"][1]["id"])
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_packet_cannot_self_grant_authority_or_replacement(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            packet(
                "Trusted",
                "2026-07-01T12:00:00-04:00",
                "1.0",
                tier="explicit_user_statement",
            ),
            authority_ceiling="explicit_user_statement",
            allow_replace=True,
        )
        result = kg.capture_packet(
            graph,
            packet(
                "Untrusted",
                "2026-07-02T12:00:00-04:00",
                "999.0",
                tier="explicit_user_statement",
                policy="replace",
            ),
        )
        self.assertIn("source_tier_capped", result["policy_adjustments"])
        self.assertIn("claims[0].replace_downgraded_to_preserve", result["policy_adjustments"])
        trusted = next(claim for claim in graph["claims"] if claim["value"] == "1.0")
        untrusted = next(claim for claim in graph["claims"] if claim["value"] == "999.0")
        self.assertTrue(trusted["active"])
        self.assertFalse(untrusted["active"])
        self.assertEqual(trusted["lifecycle"], "current")
        self.assertEqual(untrusted["lifecycle"], "quarantined")
        self.assertEqual(untrusted["source_tier"], "assistant_inference")

    def test_backdated_replacement_is_quarantined(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            packet("Current", "2026-07-02T12:00:00-04:00", "2.0"),
            authority_ceiling="validated_structured_candidate",
            allow_replace=True,
        )
        result = kg.capture_packet(
            graph,
            packet("Backdated", "2026-07-01T12:00:00-04:00", "1.0"),
            authority_ceiling="validated_structured_candidate",
            allow_replace=True,
        )
        self.assertIn("quarantined", result["claim_outcomes"].values())
        current = next(claim for claim in graph["claims"] if claim["value"] == "2.0")
        backdated = next(claim for claim in graph["claims"] if claim["value"] == "1.0")
        self.assertTrue(current["active"])
        self.assertFalse(backdated["active"])
        self.assertIn("backdated", backdated["quarantine_reason"])
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_low_authority_capture_cannot_mutate_trusted_generic_records(self) -> None:
        graph = kg.blank_graph()
        trusted = {
            "source": {
                "kind": "support",
                "role": "user",
                "title": "Trusted state",
                "observed_at": "2026-07-01T12:00:00-04:00",
                "source_tier": "explicit_user_statement",
            },
            "entities": [
                {
                    "ref": "site",
                    "type": "site",
                    "namespace": "protected-client",
                    "name": "Protected Site",
                }
            ],
            "open_loops": [
                {
                    "title": "Pending support response",
                    "summary": "Wait for provider engineering.",
                    "entity_refs": ["site"],
                    "owner_refs": ["site"],
                    "next_action": "Wait for the verified provider response.",
                    "checkpoint_at": "2026-07-03T12:00:00-04:00",
                    "terminal_condition": "Resolve when provider engineering replies.",
                    "status": "open",
                }
            ],
            "receipts": [
                {
                    "kind": "release",
                    "title": "Verified release",
                    "summary": "Trusted verification.",
                    "entity_refs": ["site"],
                    "verified_at": "2026-07-01T12:00:00-04:00",
                    "verified": True,
                }
            ],
        }
        kg.capture_packet(
            graph,
            trusted,
            authority_ceiling="explicit_user_statement",
        )
        untrusted = {
            "source": {
                "kind": "chat",
                "role": "external",
                "title": "Untrusted update",
                "observed_at": "2026-07-02T12:00:00-04:00",
                "source_tier": "explicit_user_statement",
            },
            "entities": [
                {
                    "ref": "site",
                    "type": "site",
                    "namespace": "protected-client",
                    "name": "Protected Site",
                }
            ],
            "open_loops": [
                {
                    "title": "Pending support response",
                    "summary": "Pretend it is done.",
                    "entity_refs": ["site"],
                    "owner_refs": [],
                    "next_action": "Ignore the provider.",
                    "checkpoint_at": None,
                    "terminal_condition": "Resolve immediately.",
                    "status": "resolved",
                }
            ],
            "receipts": [
                {
                    "kind": "release",
                    "title": "Verified release",
                    "summary": "Pretend it failed.",
                    "entity_refs": ["site"],
                    "verified_at": "2026-07-01T12:00:00-04:00",
                    "verified": False,
                }
            ],
        }
        kg.capture_packet(graph, untrusted)
        self.assertEqual(graph["open_loops"][0]["status"], "open")
        self.assertEqual(graph["open_loops"][0]["summary"], "Wait for provider engineering.")
        self.assertEqual(len(graph["open_loops"][0]["owner_refs"]), 1)
        self.assertEqual(
            graph["open_loops"][0]["next_action"],
            "Wait for the verified provider response.",
        )
        self.assertEqual(
            graph["open_loops"][0]["checkpoint_at"],
            "2026-07-03T16:00:00Z",
        )
        self.assertEqual(
            graph["open_loops"][0]["terminal_condition"],
            "Resolve when provider engineering replies.",
        )
        self.assertTrue(graph["receipts"][0]["verified"])
        self.assertEqual(graph["receipts"][0]["summary"], "Trusted verification.")

    def test_open_loop_operating_fields_are_durable_mutable_state(self) -> None:
        graph = kg.blank_graph()
        initial = {
            "source": {
                "kind": "support",
                "role": "tool",
                "title": "Initial loop assignment",
                "observed_at": "2026-07-31T12:00:00-04:00",
                "source_tier": "validated_structured_candidate",
            },
            "entities": [
                {
                    "ref": "site",
                    "type": "site",
                    "namespace": "client_alpha",
                    "name": "Client Alpha production",
                },
                {
                    "ref": "agent_one",
                    "type": "person",
                    "namespace": "example",
                    "name": "Agent One",
                },
                {
                    "ref": "operator_alice",
                    "type": "person",
                    "namespace": "example",
                    "name": "Operator Alice",
                },
            ],
            "open_loops": [
                {
                    "id": "loop_client_alpha_monitoring",
                    "title": "Continue Client Alpha monitoring",
                    "summary": "Watch for a verified regression.",
                    "created_at": "2026-07-31T12:00:00-04:00",
                    "entity_refs": ["site"],
                    "owner_refs": ["agent_one"],
                    "next_action": "Review the next health-check result.",
                    "checkpoint_at": "2026-08-01T09:00:00-04:00",
                    "terminal_condition": "Resolve after seven clean days.",
                    "status": "open",
                    "priority": 2,
                }
            ],
        }
        result = kg.capture_packet(
            graph,
            initial,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(result["status"], "captured")
        loop = graph["open_loops"][0]
        agent_one_id = next(
            entity["id"] for entity in graph["entities"] if entity["name"] == "Agent One"
        )
        operator_alice_id = next(
            entity["id"] for entity in graph["entities"] if entity["name"] == "Operator Alice"
        )
        self.assertEqual(loop["owner_refs"], [agent_one_id])
        self.assertEqual(loop["next_action"], "Review the next health-check result.")
        self.assertEqual(loop["checkpoint_at"], "2026-08-01T13:00:00Z")
        self.assertEqual(loop["terminal_condition"], "Resolve after seven clean days.")

        equivalent = json.loads(json.dumps(initial))
        equivalent["open_loops"][0]["next_action"] = "  Review   the next health-check result.  "
        equivalent["open_loops"][0]["checkpoint_at"] = "2026-08-01T13:00:00Z"
        duplicate = kg.capture_packet(
            graph,
            equivalent,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaisesRegex(ValueError, "based_on_watermark"):
            kg.capture_packet(
                graph,
                equivalent,
                authority_ceiling="validated_structured_candidate",
                based_on_watermark=kg.committed_watermark(graph) + 1,
            )

        omitted_same_source = json.loads(json.dumps(initial))
        for field in (
            "owner_refs",
            "next_action",
            "checkpoint_at",
            "terminal_condition",
        ):
            omitted_same_source["open_loops"][0].pop(field)
        omission = kg.capture_packet(
            graph,
            omitted_same_source,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(omission["status"], "captured")
        self.assertEqual(loop["owner_refs"], [agent_one_id])
        self.assertEqual(loop["next_action"], "Review the next health-check result.")

        updated = json.loads(json.dumps(initial))
        updated["source"].update(
            {
                "title": "Reassigned loop",
                "observed_at": "2026-08-01T12:00:00-04:00",
            }
        )
        updated_loop = updated["open_loops"][0]
        updated_loop.update(
            {
                "owner_refs": ["operator_alice"],
                "next_action": "Review the weekly incident summary.",
                "checkpoint_at": None,
                "terminal_condition": "Resolve when Operator Alice accepts the summary.",
            }
        )
        kg.capture_packet(
            graph,
            updated,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(loop["owner_refs"], [operator_alice_id])
        self.assertEqual(loop["next_action"], "Review the weekly incident summary.")
        self.assertIsNone(loop["checkpoint_at"])
        self.assertEqual(
            loop["terminal_condition"],
            "Resolve when Operator Alice accepts the summary.",
        )

        omitted = json.loads(json.dumps(updated))
        omitted["source"].update(
            {
                "title": "Summary-only loop update",
                "observed_at": "2026-08-02T12:00:00-04:00",
            }
        )
        for field in (
            "owner_refs",
            "next_action",
            "checkpoint_at",
            "terminal_condition",
        ):
            omitted["open_loops"][0].pop(field)
        omitted["open_loops"][0]["summary"] = "Continue monitoring."
        kg.capture_packet(
            graph,
            omitted,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(loop["owner_refs"], [operator_alice_id])
        self.assertEqual(loop["next_action"], "Review the weekly incident summary.")
        self.assertIsNone(loop["checkpoint_at"])
        self.assertEqual(
            loop["terminal_condition"],
            "Resolve when Operator Alice accepts the summary.",
        )
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_open_loop_owner_reference_validation_is_backward_compatible(self) -> None:
        graph = kg.blank_graph()
        legacy = {
            "source": {
                "kind": "support",
                "role": "tool",
                "title": "Legacy loop",
                "observed_at": "2026-07-31T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "entities": [
                {
                    "ref": "site",
                    "type": "site",
                    "namespace": "client_alpha",
                    "name": "Client Alpha production",
                }
            ],
            "open_loops": [
                {
                    "id": "loop_legacy",
                    "title": "Legacy pending work",
                    "entity_refs": ["site"],
                    "status": "open",
                }
            ],
        }
        kg.capture_packet(
            graph,
            legacy,
            authority_ceiling="validated_structured_candidate",
        )
        loop = graph["open_loops"][0]
        for field in (
            "owner_refs",
            "next_action",
            "checkpoint_at",
            "terminal_condition",
        ):
            loop.pop(field, None)
        self.assertTrue(kg.validate_graph(graph)["valid"])

        loop["owner_refs"] = ["entity_missing"]
        validation = kg.validate_graph(graph)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("missing owner entity" in error for error in validation["errors"]))
        loop["owner_refs"] = [{"not": "an id"}]
        malformed = kg.validate_graph(graph)
        self.assertFalse(malformed["valid"])
        self.assertTrue(
            any("invalid owner entity reference" in error for error in malformed["errors"])
        )

        invalid_packet = json.loads(json.dumps(legacy))
        invalid_packet["source"]["title"] = "Invalid owner"
        invalid_packet["open_loops"][0]["owner_refs"] = ["unknown-owner"]
        with self.assertRaisesRegex(ValueError, "Unknown entity reference"):
            kg.capture_packet(
                kg.blank_graph(),
                invalid_packet,
                authority_ceiling="validated_structured_candidate",
            )

    def test_equal_authority_recapture_updates_event_status_and_receipt_uri(self) -> None:
        graph = kg.blank_graph()
        first = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Initial record",
                "observed_at": "2026-07-01T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "events": [
                {
                    "kind": "incident",
                    "title": "Service interruption",
                    "occurred_at": "2026-07-01T11:00:00Z",
                    "status": "active",
                }
            ],
            "receipts": [
                {
                    "kind": "release",
                    "title": "Release verification",
                    "verified_at": "2026-07-01T11:30:00Z",
                    "uri": "https://example.test/old",
                    "verified": True,
                }
            ],
        }
        kg.capture_packet(
            graph,
            first,
            authority_ceiling="validated_structured_candidate",
        )
        corrected = json.loads(json.dumps(first))
        corrected["source"]["title"] = "Corrected record"
        corrected["source"]["observed_at"] = "2026-07-02T12:00:00Z"
        corrected["events"][0]["status"] = "resolved"
        corrected["receipts"][0]["uri"] = "https://example.test/current"
        kg.capture_packet(
            graph,
            corrected,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(len(graph["events"]), 1)
        self.assertEqual(graph["events"][0]["status"], "resolved")
        self.assertEqual(len(graph["receipts"]), 1)
        self.assertEqual(graph["receipts"][0]["uri"], "https://example.test/current")
        backfill = json.loads(json.dumps(first))
        backfill["source"]["title"] = "Backdated record"
        backfill["source"]["observed_at"] = "2026-06-30T12:00:00Z"
        backfill["events"][0]["status"] = "active"
        backfill["receipts"][0]["uri"] = "https://example.test/backdated"
        kg.capture_packet(
            graph,
            backfill,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(graph["events"][0]["status"], "resolved")
        self.assertEqual(graph["receipts"][0]["uri"], "https://example.test/current")

    def test_redact_cascades_to_lineage(self) -> None:
        graph = kg.blank_graph()
        evidence = {
            "source": {
                "kind": "log",
                "role": "tool",
                "title": "Status evidence",
                "observed_at": "2026-07-01T12:00:00-04:00",
                "excerpt": "The private status value is ULTRA_FORGET_ME.",
            },
            "entities": [
                {"ref": "site", "type": "site", "namespace": "forget-client", "name": "Forget Site"}
            ],
            "observations": [
                {
                    "family": "status",
                    "key": "private.state",
                    "value": "ULTRA_FORGET_ME",
                    "entity_refs": ["site"],
                }
            ],
            "claims": [
                {
                    "family": "status",
                    "key": "private.state",
                    "value": "ULTRA_FORGET_ME",
                    "entity_refs": ["site"],
                }
            ],
        }
        kg.capture_packet(graph, evidence)
        claim_id = graph["claims"][0]["id"]
        result = kg.redact_record(graph, claim_id)
        self.assertGreaterEqual(len(result["cascaded_ids"]), 3)
        self.assertNotIn("ULTRA_FORGET_ME", json.dumps(graph))
        self.assertEqual(graph["claims"][0]["lifecycle"], "redacted")
        self.assertEqual(graph["claims"][0]["family"], "[redacted]")
        self.assertEqual(graph["claims"][0]["key"], "[redacted]")
        self.assertEqual(graph["observations"][0]["lifecycle"], "redacted")
        self.assertEqual(graph["sources"][0]["lifecycle"], "redacted")
        self.assertEqual(kg.validate_graph(graph)["errors"], [])

        entity_id = graph["entities"][0]["id"]
        kg.redact_record(graph, entity_id)
        self.assertEqual(graph["entities"][0]["type"], "[redacted]")
        self.assertEqual(graph["entities"][0]["name"], "[redacted]")
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Attempted recapture",
                    "observed_at": "2026-07-02T12:00:00Z",
                },
                "entities": [{"type": "site", "namespace": "forget-client", "name": "Forget Site"}],
            },
        )
        self.assertEqual(graph["entities"][0]["name"], "[redacted]")
        self.assertEqual(graph["entities"][0]["aliases"], [])

    def test_secrets_are_scrubbed_and_safe_recall_hides_paths(self) -> None:
        graph = kg.blank_graph()
        secret_packet = {
            "source": {
                "kind": "log",
                "role": "tool",
                "title": "Private check",
                "observed_at": "2026-07-01T12:00:00-04:00",
                "excerpt": (
                    "password: ULTRA_UNIQUE_PASSWORD and privateKey: ULTRA_UNIQUE_KEY "
                    "and glpat-ULTRA_UNIQUE_PROVIDER_TOKEN at /Users/alice/private/notes.txt"
                ),
                "privateKey": "ULTRA_UNIQUE_FIELD_SECRET",
            },
            "entities": [
                {
                    "ref": "site",
                    "type": "site",
                    "namespace": "private-client",
                    "name": "Private Site",
                }
            ],
            "claims": [
                {
                    "family": "configuration",
                    "key": "private.path",
                    "value": "/Users/alice/private/notes.txt",
                    "entity_refs": ["site"],
                    "sensitive": True,
                },
                {
                    "family": "configuration",
                    "key": "api_key",
                    "value": "ULTRA_SEMANTIC_CREDENTIAL_12345",
                    "entity_refs": ["site"],
                },
            ],
        }
        result = kg.capture_packet(graph, secret_packet)
        serialized = json.dumps(graph)
        for secret in (
            "ULTRA_UNIQUE_PASSWORD",
            "ULTRA_UNIQUE_KEY",
            "ULTRA_UNIQUE_FIELD_SECRET",
            "glpat-ULTRA_UNIQUE_PROVIDER_TOKEN",
            "ULTRA_SEMANTIC_CREDENTIAL_12345",
        ):
            self.assertNotIn(secret, serialized)
        self.assertGreaterEqual(result["redactions"], 2)
        self.assertIn("claims[1].semantic_secret_value_redacted", result["policy_adjustments"])
        self.assertEqual(kg.secret_hits(graph), [])

        safe = kg.recall_graph(graph, "private.path", 10, 12000, "review_safe")
        full = kg.recall_graph(graph, "private.path", 10, 12000, "operator_full")
        self.assertNotIn("/Users/alice/private/notes.txt", json.dumps(safe))
        self.assertIn("/Users/alice/private/notes.txt", json.dumps(full))

    def test_multiword_credentials_are_fully_scrubbed(self) -> None:
        graph = kg.blank_graph()
        secret_packet = {
            "source": {
                "kind": "log",
                "role": "tool",
                "title": "Credential check",
                "observed_at": "2026-07-01T12:00:00Z",
                "excerpt": (
                    "password is correct horse battery staple\n"
                    "passphrase: 'violet river winter moon'\n"
                    'credentials = "shared staging access phrase"\n'
                    "AWS_SECRET_ACCESS_KEY=aws-secret-material\n"
                    "SECRET_KEY=framework-secret-material\n"
                    "token=short-free-form-token\n"
                    "DATABASE_PASSWORD=database-password-material\n"
                    "GITHUB_TOKEN=github-token-material\n"
                    "DJANGO_SECRET_KEY=django-secret-material\n"
                    "SECRET_KEY_BASE=secret-key-base-material\n"
                    "NEXTAUTH_SECRET=nextauth-secret-material\n"
                    "MY_CLIENT_SECRET=client-secret-material\n"
                    "FOO_API_KEY=api-key-material\n"
                    "PGPASSWORD=postgres-password-material\n"
                    "REDISCLI_AUTH=redis-cli-auth-material\n"
                    "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=azure-account-key-material;EndpointSuffix=core.windows.net\n"
                    'DOCKER_AUTH_CONFIG={"auths":{"registry.example":{"auth":"docker-auth-material"}}}\n'
                    "DATABASE_URL=postgres://test:ULTRA_UNIQUE_DATABASE_PASSWORD@db.invalid/prod\n"
                    "REDIS_URL=redis://test:ULTRA_UNIQUE_REDIS_PASSWORD@cache.invalid/0\n"
                    "https://acct.blob.core.windows.net/c/b?sv=1&sig=AZURETOPSECRET&se=2\n"
                    "https://storage.googleapis.test/object?X-Goog-Signature=GOOGTOPSECRET"
                ),
            }
        }
        kg.capture_packet(graph, secret_packet)
        serialized = json.dumps(graph)
        for fragment in (
            "correct horse battery staple",
            "violet river winter moon",
            "shared staging access phrase",
            "aws-secret-material",
            "framework-secret-material",
            "short-free-form-token",
            "database-password-material",
            "github-token-material",
            "django-secret-material",
            "secret-key-base-material",
            "nextauth-secret-material",
            "client-secret-material",
            "api-key-material",
            "postgres-password-material",
            "redis-cli-auth-material",
            "azure-account-key-material",
            "docker-auth-material",
            "ULTRA_UNIQUE_DATABASE_PASSWORD",
            "ULTRA_UNIQUE_REDIS_PASSWORD",
            "AZURETOPSECRET",
            "GOOGTOPSECRET",
        ):
            self.assertNotIn(fragment, serialized)
        self.assertEqual(kg.secret_hits(graph), [])
        semantic_secrets = {
            "accountKey": "semantic-azure-secret",
            "PGPASSWORD": "semantic-postgres-secret",
            "REDISCLI_AUTH": "semantic-redis-secret",
        }
        self.assertTrue(kg.secret_hits(semantic_secrets))
        cleaned_fields, _ = kg.scrub_value(semantic_secrets)
        self.assertEqual(
            set(cleaned_fields.values()),
            {"[redacted secret]"},
        )
        cleaned_paths, _ = kg.redact_text(
            "paths: /srv/app/private.txt /workspace/project/.env "
            "file:///workspace/secret.txt file://nas.internal/share/client/private.txt "
            "/var/www/client/wp-config.php "
            "/var/lib/postgresql/data /mnt/customer/private.txt "
            "/data/clients/acme/config.yml D:\\Projects\\Acme\\secret.txt "
            "\\\\server\\share\\private.txt //server/share/private.txt"
        )
        for local_path in (
            "/srv/app/private.txt",
            "/workspace/project/.env",
            "file:///workspace/secret.txt",
            "file://nas.internal/share/client/private.txt",
            "/var/www/client/wp-config.php",
            "/var/lib/postgresql/data",
            "/mnt/customer/private.txt",
            "/data/clients/acme/config.yml",
            "D:\\Projects\\Acme\\secret.txt",
            "\\\\server\\share\\private.txt",
            "//server/share/private.txt",
        ):
            self.assertNotIn(local_path, cleaned_paths)
        public_urls, _ = kg.redact_text(
            "https://example.com/projects/client-case/ and /projects/?page=2"
        )
        self.assertIn("https://example.com/projects/client-case/", public_urls)
        self.assertIn("/projects/?page=2", public_urls)

    def test_low_authority_claims_are_quarantined_and_cannot_reconcile(self) -> None:
        graph = kg.blank_graph()
        low = packet(
            "Assistant guess",
            "2026-07-01T12:00:00Z",
            "guess",
            tier="assistant_inference",
        )
        kg.capture_packet(graph, low)
        claim = graph["claims"][0]
        self.assertFalse(claim["active"])
        self.assertEqual(claim["lifecycle"], "quarantined")
        recalled = kg.recall_graph(graph, "plugin.version", 10, 12000)
        self.assertNotIn("current_claims", recalled["recall_order"])

        trusted_graph = kg.blank_graph()
        for title, value in (("Strict", "strict"), ("Permissive", "permissive")):
            kg.capture_packet(
                trusted_graph,
                packet(title, "2026-07-01T12:00:00Z", value, policy="preserve"),
                authority_ceiling="validated_structured_candidate",
            )
        strict = next(item for item in trusted_graph["claims"] if item["value"] == "strict")
        permissive = next(item for item in trusted_graph["claims"] if item["value"] == "permissive")
        self.assertEqual(strict["lifecycle"], "conflicting")
        self.assertEqual(permissive["lifecycle"], "conflicting")
        low_repeat = packet(
            "Low repeat",
            "2026-07-02T12:00:00Z",
            "strict",
            tier="assistant_inference",
            policy="replace",
        )
        kg.capture_packet(
            trusted_graph,
            low_repeat,
            authority_ceiling="validated_structured_candidate",
            allow_replace=True,
        )
        self.assertTrue(strict["active"])
        self.assertTrue(permissive["active"])
        self.assertEqual(strict["lifecycle"], "conflicting")
        self.assertEqual(permissive["lifecycle"], "conflicting")

    def test_visible_conflicts_are_scoped_to_recalled_results(self) -> None:
        graph = kg.blank_graph()
        for title, value in (("Alpha one", "one"), ("Alpha two", "two")):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "document",
                        "role": "tool",
                        "title": title,
                        "observed_at": "2026-07-01T12:00:00Z",
                        "source_tier": "validated_structured_candidate",
                    },
                    "entities": [{"ref": "alpha", "type": "client", "name": "Alpha"}],
                    "claims": [
                        {
                            "family": "private-topic",
                            "key": "secret-state",
                            "value": value,
                            "entity_refs": ["alpha"],
                            "conflict_policy": "preserve",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
            )
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Beta evidence",
                    "observed_at": "2026-07-02T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "entities": [{"ref": "beta", "type": "client", "name": "Beta"}],
                "claims": [
                    {
                        "family": "deployment",
                        "key": "state",
                        "value": "healthy",
                        "entity_refs": ["beta"],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        recalled = kg.recall_graph(graph, "Beta", 20, 20000)
        self.assertEqual(recalled["visible_conflicts"], [])
        self.assertNotIn("secret-state", json.dumps(recalled))

    def test_record_authority_cannot_exceed_its_source(self) -> None:
        graph = kg.blank_graph()
        item = packet(
            "Assistant source",
            "2026-07-01T12:00:00Z",
            "guess",
            tier="assistant_inference",
        )
        item_claims = cast(list[dict[str, object]], item["claims"])
        item_claims[0]["source_tier"] = "explicit_user_statement"
        result = kg.capture_packet(
            graph,
            item,
            authority_ceiling="explicit_user_statement",
        )
        self.assertEqual(graph["claims"][0]["source_tier"], "assistant_inference")
        self.assertIn("claims[0].source_tier_capped", result["policy_adjustments"])

    def test_sensitivity_escalation_is_monotonic(self) -> None:
        graph = kg.blank_graph()
        initial = packet(
            "Initial classification",
            "2026-07-01T12:00:00Z",
            "diagnosis alpha",
        )
        kg.capture_packet(
            graph,
            initial,
            authority_ceiling="validated_structured_candidate",
        )
        correction = packet(
            "Sensitivity correction",
            "2026-07-02T12:00:00Z",
            "diagnosis alpha",
            tier="explicit_user_statement",
        )
        correction_claims = cast(list[dict[str, object]], correction["claims"])
        correction_entities = cast(list[dict[str, object]], correction["entities"])
        correction_claims[0]["sensitive"] = True
        correction_entities[0]["sensitive"] = True
        kg.capture_packet(
            graph,
            correction,
            authority_ceiling="explicit_user_statement",
        )
        self.assertTrue(graph["claims"][0]["sensitive"])
        self.assertTrue(graph["entities"][0]["sensitive"])
        safe = kg.recall_graph(graph, "plugin.version", 10, 12000, "review_safe")
        claim_result = next(
            item for item in safe["results"] if item["id"] == graph["claims"][0]["id"]
        )
        self.assertEqual(claim_result["record"]["value"], "[redacted in review_safe mode]")

    def test_collection_limits_reject_instead_of_truncating(self) -> None:
        graph = kg.blank_graph()
        many_events = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Bulk import",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "events": [
                {
                    "kind": "test",
                    "title": f"Event {index}",
                    "occurred_at": f"2026-07-01T12:{index // 60:02d}:{index % 60:02d}Z",
                }
                for index in range(201)
            ],
        }
        kg.capture_packet(graph, many_events)
        self.assertEqual(len(graph["events"]), 201)

        oversized = dict(many_events)
        oversized["events"] = [{"title": f"Too many {index}"} for index in range(501)]
        with self.assertRaisesRegex(ValueError, "500-item|500-record"):
            kg.capture_packet(kg.blank_graph(), oversized)

        nested: object = "leaf"
        for _ in range(22):
            nested = {"next": nested}
        with self.assertRaisesRegex(ValueError, "nesting"):
            kg.scrub_value(nested)

    def test_scoped_entity_identity_and_explicit_id_validation(self) -> None:
        graph = kg.blank_graph()
        scoped = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Environments",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "entities": [
                {
                    "ref": "one",
                    "type": "environment",
                    "namespace": "client-one",
                    "name": "Production",
                },
                {
                    "ref": "two",
                    "type": "environment",
                    "namespace": "client-two",
                    "name": "Production",
                },
            ],
        }
        kg.capture_packet(graph, scoped)
        self.assertEqual(len(graph["entities"]), 2)
        self.assertNotEqual(graph["entities"][0]["id"], graph["entities"][1]["id"])

        missing_namespace = json.loads(json.dumps(scoped))
        missing_namespace["entities"] = [{"type": "environment", "name": "Production"}]
        with self.assertRaisesRegex(ValueError, "requires a stable namespace"):
            kg.capture_packet(kg.blank_graph(), missing_namespace)

        explicit_graph = kg.blank_graph()
        explicit = json.loads(json.dumps(scoped))
        explicit["entities"] = [
            {
                "id": "entity_operator_supplied",
                "type": "environment",
                "namespace": "client-one",
                "name": "Production",
            }
        ]
        kg.capture_packet(explicit_graph, explicit)
        mismatch = json.loads(json.dumps(explicit))
        mismatch["source"]["title"] = "Mismatched identity"
        mismatch["entities"][0]["namespace"] = "client-two"
        with self.assertRaisesRegex(ValueError, "does not match"):
            kg.capture_packet(explicit_graph, mismatch)

        semantic_graph = kg.blank_graph()
        first_identity = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Explicit identity",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "entities": [{"id": "custom-alpha", "type": "client", "name": "Alpha"}],
        }
        kg.capture_packet(semantic_graph, first_identity)
        implicit_repeat = json.loads(json.dumps(first_identity))
        implicit_repeat["source"]["title"] = "Implicit repeat"
        implicit_repeat["entities"][0].pop("id")
        kg.capture_packet(semantic_graph, implicit_repeat)
        self.assertEqual(len(semantic_graph["entities"]), 1)
        conflicting_id = json.loads(json.dumps(first_identity))
        conflicting_id["source"]["title"] = "Conflicting explicit ID"
        conflicting_id["entities"][0]["id"] = "other-alpha"
        with self.assertRaisesRegex(ValueError, "already belongs"):
            kg.capture_packet(semantic_graph, conflicting_id)

        unsafe_id = json.loads(json.dumps(first_identity))
        unsafe_id["source"]["title"] = "Unsafe explicit ID"
        unsafe_id["entities"][0]["id"] = "/Users/alice/Private/entity"
        with self.assertRaisesRegex(ValueError, "Explicit record IDs"):
            kg.capture_packet(kg.blank_graph(), unsafe_id)

    def test_identity_normalization_is_unicode_stable(self) -> None:
        self.assertEqual(kg.normalize_key("Caf\u00e9"), kg.normalize_key("Cafe\u0301"))
        self.assertEqual(kg.normalize_key("Stra\u00dfe"), kg.normalize_key("STRASSE"))

        graph = kg.blank_graph()
        first = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "NFC identity",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "entities": [{"type": "client", "name": "Caf\u00e9"}],
        }
        kg.capture_packet(graph, first)
        second = json.loads(json.dumps(first))
        second["source"]["title"] = "NFD identity"
        second["entities"][0]["name"] = "Cafe\u0301"
        kg.capture_packet(graph, second)
        self.assertEqual(len(graph["entities"]), 1)

    def test_generic_explicit_id_cannot_duplicate_semantic_identity(self) -> None:
        graph = kg.blank_graph()
        item = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Event identity",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "events": [
                {
                    "kind": "incident",
                    "title": "Same event",
                    "occurred_at": "2026-07-01T12:00:00Z",
                }
            ],
        }
        kg.capture_packet(graph, item)
        duplicate = json.loads(json.dumps(item))
        duplicate["source"]["title"] = "Duplicate identity"
        duplicate["events"][0]["id"] = "custom-event-id"
        with self.assertRaisesRegex(ValueError, "already belongs"):
            kg.capture_packet(graph, duplicate)

        unsafe = json.loads(json.dumps(item))
        unsafe["source"]["title"] = "Unsafe event ID"
        unsafe["events"][0]["id"] = "D:\\Private\\event"
        with self.assertRaisesRegex(ValueError, "Explicit record IDs"):
            kg.capture_packet(kg.blank_graph(), unsafe)

    def test_low_authority_decisions_and_loops_require_promotion(self) -> None:
        graph = kg.blank_graph()
        candidate_packet = {
            "source": {
                "kind": "chat",
                "role": "assistant",
                "title": "Assistant proposal",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "decisions": [
                {
                    "id": "decision_monitoring",
                    "title": "Change monitoring",
                    "decided_at": "2026-07-01T12:00:00Z",
                    "status": "accepted",
                }
            ],
            "open_loops": [
                {
                    "id": "loop_monitoring",
                    "title": "Change monitoring",
                    "created_at": "2026-07-01T12:00:00Z",
                    "status": "open",
                }
            ],
        }
        kg.capture_packet(graph, candidate_packet)
        decision = graph["decisions"][0]
        loop = graph["open_loops"][0]
        self.assertEqual(
            (decision["authority_state"], decision["status"]), ("candidate", "proposed")
        )
        self.assertEqual((loop["authority_state"], loop["status"]), ("candidate", "candidate"))
        recalled = kg.recall_graph(graph, "Change monitoring", 10, 12000)
        self.assertIn("candidate_records", recalled["recall_order"])
        self.assertNotIn("active_open_loops", recalled["recall_order"])

        promoted = json.loads(json.dumps(candidate_packet))
        promoted["source"].update(
            {
                "role": "tool",
                "title": "Validated proposal",
                "source_tier": "validated_structured_candidate",
            }
        )
        kg.capture_packet(
            graph,
            promoted,
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(
            (decision["authority_state"], decision["status"]), ("accepted", "accepted")
        )
        self.assertEqual((loop["authority_state"], loop["status"]), ("accepted", "open"))
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_proposal_review_requires_semantic_coverage_even_as_milestone(self) -> None:
        graph = kg.blank_graph()
        before = json.loads(json.dumps(graph))
        sparse = substantive_analysis_packet()
        del sparse["coverage"]
        observations = sparse.get("observations")
        if not isinstance(observations, list):
            self.fail("substantive fixture observations must be a list")
        sparse["observations"] = observations[:1]
        sparse.pop("decisions")

        with self.assertRaisesRegex(ValueError, "coverage ledger is required"):
            kg.capture_packet(graph, sparse, discovery_kind="milestone")

        self.assertEqual(graph, before)

        for event_kind, event_status in (
            ("review_completed", None),
            ("proposal-review", None),
            ("proposal review", None),
            ("proposal_response_reviewed", None),
            ("assessment_assessed", None),
            ("audit_audited", None),
            ("analysis_analyzed", None),
            ("analysis", "completed"),
            ("report", "completed"),
            ("review", "complete"),
            ("audit", "produced"),
            ("assessment", "finalized"),
            ("codeReviewReport", None),
            ("designReviewCompleted", None),
            ("proposalReview", None),
        ):
            with self.subTest(event_kind=event_kind, event_status=event_status):
                generic_review = json.loads(json.dumps(sparse))
                generic_events = generic_review.get("events")
                if (
                    not isinstance(generic_events, list)
                    or not generic_events
                    or not isinstance(generic_events[0], dict)
                ):
                    self.fail("substantive fixture event must be an object")
                generic_events[0]["kind"] = event_kind
                if event_status is None:
                    generic_events[0].pop("status", None)
                else:
                    generic_events[0]["status"] = event_status
                with self.assertRaisesRegex(ValueError, "coverage ledger is required"):
                    kg.capture_packet(kg.blank_graph(), generic_review, discovery_kind="milestone")

        for event_kind, event_status in (
            ("review", None),
            ("analysis", None),
            ("report", None),
            ("audit_scheduled", None),
            ("assessment_started", None),
            ("analysis_job_failed", None),
            ("review_requested", None),
            ("proposal_review_requested", None),
            ("proposal_response_reviewed_failed", None),
            ("assessment_assessed_pending", None),
            ("audit_audited_cancelled", None),
            ("analysis_analyzed_requested", None),
            ("analysis", "pending"),
            ("report", "requested"),
            ("review", "started"),
            ("audit", "failed"),
            ("assessment", "cancelled"),
            ("assessment", "canceled"),
            ("analysis", "scheduled"),
            ("analysisPending", "completed"),
            ("codeReviewReport", "pending"),
            ("codeReviewReport", "in_progress"),
            ("designReviewCompleted", "cancelled"),
            ("designReviewCompleted", "inProgress"),
            ("proposalReview", "requested"),
            ("deployment", "completed"),
            ("milestone", "finalized"),
        ):
            with self.subTest(routine_event_kind=event_kind, event_status=event_status):
                narrow_review = json.loads(json.dumps(sparse))
                narrow_events = narrow_review.get("events")
                if (
                    not isinstance(narrow_events, list)
                    or not narrow_events
                    or not isinstance(narrow_events[0], dict)
                ):
                    self.fail("substantive fixture event must be an object")
                narrow_events[0]["kind"] = event_kind
                if event_status is None:
                    narrow_events[0].pop("status", None)
                else:
                    narrow_events[0]["status"] = event_status
                narrow_result = kg.capture_packet(
                    kg.blank_graph(), narrow_review, discovery_kind="milestone"
                )
                self.assertEqual(narrow_result["status"], "captured")

    def test_substantial_analysis_coverage_resolves_and_keeps_recommendation_candidate(
        self,
    ) -> None:
        graph = kg.blank_graph()
        packet_value = substantive_analysis_packet()
        substantive_coverage_items(packet_value)[0]["summary"] = (
            "coverage-narrative-must-not-persist"
        )
        result = kg.capture_packet(
            graph,
            packet_value,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )

        coverage = result["coverage"]
        self.assertNotIn("coverage-narrative-must-not-persist", json.dumps(coverage))
        self.assertNotIn("coverage-narrative-must-not-persist", json.dumps(graph["journal"]))
        self.assertRegex(coverage["coverage_digest"], r"^[0-9a-f]{32}$")
        self.assertTrue(
            all(
                item.get("record_ids")
                for item in coverage["items"]
                if item["disposition"] in {"captured", "existing"}
            )
        )
        decision = next(
            item
            for item in graph["decisions"]
            if item["id"] == "decision_synthetic_technical_addendum"
        )
        self.assertEqual(decision["source_tier"], "assistant_inference")
        self.assertEqual(
            (decision["authority_state"], decision["status"]), ("candidate", "proposed")
        )
        self.assertEqual(graph["journal"][-1]["coverage"], coverage)
        duplicate = kg.capture_packet(
            graph,
            packet_value,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["coverage"]["coverage_digest"], coverage["coverage_digest"])

    def test_coverage_changes_fingerprint_and_receipt_cannot_satisfy_risk(self) -> None:
        first_packet = substantive_analysis_packet()
        second_packet = substantive_analysis_packet()
        substantive_coverage_items(second_packet)[2]["summary"] = (
            "The integration contract gap remains material."
        )
        first = kg.capture_packet(
            kg.blank_graph(),
            first_packet,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        second = kg.capture_packet(
            kg.blank_graph(),
            second_packet,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

        invalid = substantive_analysis_packet()
        substantive_coverage_items(invalid)[2]["path"] = "receipts[0]"
        graph = kg.blank_graph()
        before = json.dumps(graph, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "cannot be satisfied by receipts"):
            kg.capture_packet(
                graph,
                invalid,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )
        self.assertEqual(json.dumps(graph, sort_keys=True), before)

    def test_coverage_existing_id_is_validated_before_mutation(self) -> None:
        graph = kg.blank_graph()
        first = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Existing opportunity",
                "observed_at": "2026-08-12T18:00:00Z",
            },
            "entities": [
                {
                    "id": "entity_synthetic_existing_opportunity",
                    "type": "project",
                    "name": "Existing Synthetic Opportunity",
                }
            ],
        }
        kg.capture_packet(graph, first)
        before = json.loads(json.dumps(graph))
        packet_with_existing = substantive_analysis_packet()
        coverage_items = substantive_coverage_items(packet_with_existing)
        coverage_items[0] = {
            "category": "identities_relationships_status",
            "summary": "The existing opportunity identity is reused.",
            "disposition": "existing",
            "record_id": "entity_missing_opportunity",
        }
        with self.assertRaisesRegex(ValueError, "semantically eligible"):
            kg.capture_packet(
                graph,
                packet_with_existing,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )
        self.assertEqual(graph, before)

        kg.redact_record(graph, "entity_synthetic_existing_opportunity")
        redacted_before = json.loads(json.dumps(graph))
        coverage_items[0]["record_id"] = "entity_synthetic_existing_opportunity"
        with self.assertRaisesRegex(ValueError, "semantically eligible"):
            kg.capture_packet(
                graph,
                packet_with_existing,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )
        self.assertEqual(graph, redacted_before)

    def test_coverage_duplicate_survives_later_redaction_of_existing_record(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Existing opportunity identity",
                    "observed_at": "2026-08-12T18:00:00Z",
                },
                "entities": [
                    {
                        "id": "entity_duplicate_retry_opportunity",
                        "type": "project",
                        "name": "Duplicate Retry Opportunity",
                    }
                ],
            },
        )
        covered = substantive_analysis_packet()
        substantive_coverage_items(covered)[0] = {
            "category": "identities_relationships_status",
            "summary": "Reuse the existing opportunity identity.",
            "disposition": "existing",
            "record_id": "entity_duplicate_retry_opportunity",
        }
        first = kg.capture_packet(
            graph,
            covered,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        digest = first["coverage"]["coverage_digest"]
        kg.redact_record(graph, "entity_duplicate_retry_opportunity")

        retry = kg.capture_packet(
            graph,
            covered,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertEqual(retry["status"], "duplicate")
        self.assertEqual(retry["coverage"]["coverage_digest"], digest)

    def test_unknown_capture_packet_fields_are_rejected(self) -> None:
        ordinary = packet("Ordinary", "2026-07-01T12:00:00Z", "1.0")
        ordinary["unreviewed_metadata"] = {"ignored_before": True}
        with self.assertRaisesRegex(ValueError, "Unknown capture packet fields"):
            kg.capture_packet(kg.blank_graph(), ordinary)
        ordinary.pop("unreviewed_metadata")
        ordinary["_capture_fingerprint_only"] = "cannot-forge-private-graph-marker"
        with self.assertRaisesRegex(ValueError, "Unknown capture packet fields"):
            kg.capture_packet(kg.blank_graph(), ordinary)

    def test_fingerprint_only_normalizes_coverage_without_graph_state_or_mutation(self) -> None:
        graph = kg.blank_graph()
        packet_value = substantive_analysis_packet()
        substantive_coverage_items(packet_value)[0] = {
            "category": "identities_relationships_status",
            "summary": "Reuse an exact identity without consulting graph state.",
            "disposition": "existing",
            "record_id": "entity_fingerprint_only_missing",
        }
        graph["_capture_fingerprint_only"] = kg._CAPTURE_FINGERPRINT_ONLY
        before_keys = set(graph)
        result = kg.capture_packet(
            graph,
            packet_value,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertEqual(result["status"], "fingerprint")
        self.assertRegex(result["coverage"]["coverage_digest"], r"^[0-9a-f]{32}$")
        self.assertEqual(set(graph), before_keys)
        self.assertIs(graph["_capture_fingerprint_only"], kg._CAPTURE_FINGERPRINT_ONLY)

        malformed = kg.blank_graph()
        malformed["_capture_fingerprint_only"] = "forged"
        with self.assertRaisesRegex(ValueError, "Invalid private fingerprint-only"):
            kg.capture_packet(malformed, packet_value)

        nonblank = kg.blank_graph()
        nonblank["_capture_fingerprint_only"] = kg._CAPTURE_FINGERPRINT_ONLY
        entities = nonblank.get("entities")
        if not isinstance(entities, list):
            self.fail("blank graph entities must be a list")
        entities.append({"id": "entity_marker_must_remain_blank"})
        with self.assertRaisesRegex(ValueError, "structurally blank"):
            kg.capture_packet(nonblank, packet_value)

    def test_coverage_requires_explicit_decision_tier_and_caps_assistant_role(self) -> None:
        missing = substantive_analysis_packet()
        decision = missing.get("decisions")
        if not isinstance(decision, list) or not decision or not isinstance(decision[0], dict):
            self.fail("substantive fixture decision must be an object")
        decision[0].pop("source_tier")
        with self.assertRaisesRegex(ValueError, "must explicitly declare source_tier"):
            kg.capture_packet(
                kg.blank_graph(),
                missing,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        elevated = substantive_analysis_packet()
        elevated_decisions = elevated.get("decisions")
        if (
            not isinstance(elevated_decisions, list)
            or not elevated_decisions
            or not isinstance(elevated_decisions[0], dict)
        ):
            self.fail("substantive fixture decision must be an object")
        elevated_decisions[0]["source_tier"] = "validated_structured_candidate"
        graph = kg.blank_graph()
        result = kg.capture_packet(
            graph,
            elevated,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertIn("decisions[0].assistant_role_tier_capped", result["policy_adjustments"])
        stored = next(
            item
            for item in graph["decisions"]
            if item["id"] == "decision_synthetic_technical_addendum"
        )
        self.assertEqual(stored["source_tier"], "assistant_inference")
        self.assertEqual(stored["authority_state"], "candidate")

    def test_coverage_rejects_ineligible_existing_and_captured_records(self) -> None:
        graph = kg.blank_graph()
        first = substantive_analysis_packet()
        kg.capture_packet(
            graph,
            first,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        decision = next(
            item
            for item in graph["decisions"]
            if item["id"] == "decision_synthetic_technical_addendum"
        )
        decision["lifecycle"] = "superseded"
        existing = substantive_analysis_packet()
        substantive_coverage_items(existing)[3] = {
            "category": "recommendations_decisions",
            "summary": "Reuse the prior candidate recommendation.",
            "disposition": "existing",
            "record_id": "decision_synthetic_technical_addendum",
        }
        with self.assertRaisesRegex(ValueError, "semantically eligible"):
            kg.capture_packet(
                graph,
                existing,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        captured = substantive_analysis_packet()
        captured_decisions = captured.get("decisions")
        if (
            not isinstance(captured_decisions, list)
            or not captured_decisions
            or not isinstance(captured_decisions[0], dict)
        ):
            self.fail("substantive fixture decision must be an object")
        captured_decisions[0]["status"] = "accepted"
        captured_decisions[0]["source_tier"] = "assistant_inference"
        clean = kg.blank_graph()
        result = kg.capture_packet(
            clean,
            captured,
            authority_ceiling="validated_structured_candidate",
            discovery_kind="substantial_analysis",
        )
        self.assertEqual(result["status"], "captured")
        self.assertEqual(clean["decisions"][0]["authority_state"], "candidate")

    def test_current_state_coverage_requires_validated_current_evidence_atomically(self) -> None:
        captured = substantive_analysis_packet()
        observations = captured.get("observations")
        if (
            not isinstance(observations, list)
            or not observations
            or not isinstance(observations[0], dict)
        ):
            self.fail("substantive fixture observation must be an object")
        observations[0]["source_tier"] = "assistant_inference"
        graph = kg.blank_graph()
        before = json.loads(json.dumps(graph))
        with self.assertRaisesRegex(ValueError, "semantically ineligible"):
            kg.capture_packet(
                graph,
                captured,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )
        self.assertEqual(graph, before)

        existing_graph = kg.blank_graph()
        kg.capture_packet(
            existing_graph,
            {
                "source": {
                    "kind": "document",
                    "role": "assistant",
                    "title": "Low-authority state note",
                    "observed_at": "2026-08-12T18:00:00Z",
                    "source_tier": "assistant_inference",
                },
                "observations": [
                    {
                        "family": "proposal_architecture",
                        "key": "synthetic.low_authority_state",
                        "value": "An unverified architecture note.",
                        "observed_at": "2026-08-12T18:00:00Z",
                    }
                ],
            },
        )
        low_observation_id = existing_graph["observations"][0]["id"]
        existing = substantive_analysis_packet()
        substantive_coverage_items(existing)[1] = {
            "category": "verified_current_state",
            "summary": "Reuse the prior state note.",
            "disposition": "existing",
            "record_id": low_observation_id,
        }
        existing_before = json.loads(json.dumps(existing_graph))
        with self.assertRaisesRegex(ValueError, "semantically eligible"):
            kg.capture_packet(
                existing_graph,
                existing,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )
        self.assertEqual(existing_graph, existing_before)

        claim_graph = kg.blank_graph()
        kg.capture_packet(
            claim_graph,
            packet("Validated state", "2026-08-12T18:00:00Z", "1.0"),
            authority_ceiling="validated_structured_candidate",
        )
        claim = claim_graph["claims"][0]
        claim["active"] = False
        claim["lifecycle"] = "current"
        inactive = substantive_analysis_packet()
        substantive_coverage_items(inactive)[1] = {
            "category": "verified_current_state",
            "summary": "Reuse the inactive claim.",
            "disposition": "existing",
            "record_id": claim["id"],
        }
        with self.assertRaisesRegex(ValueError, "semantically eligible"):
            kg.capture_packet(
                claim_graph,
                inactive,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

    def test_coverage_rejects_not_applicable_bypasses(self) -> None:
        packet_value = substantive_analysis_packet()
        categories = (
            "identities_relationships_status",
            "verified_current_state",
            "conclusions_risks_unknowns",
            "recommendations_decisions",
            "obligations_next_actions",
            "artifacts_events_receipts",
        )
        for category, item in zip(
            categories, substantive_coverage_items(packet_value), strict=True
        ):
            item.clear()
            item.update(
                {
                    "category": category,
                    "summary": "No material outcome.",
                    "disposition": "not_applicable",
                    "reason": "no_material_outcome",
                    "explanation": "Synthetic bypass attempt.",
                }
            )
        with self.assertRaisesRegex(ValueError, "cannot mark all categories not_applicable"):
            kg.capture_packet(
                kg.blank_graph(),
                packet_value,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        exact_type = substantive_analysis_packet()
        substantive_coverage_items(exact_type)[3] = {
            "category": "recommendations_decisions",
            "summary": "No recommendation exists.",
            "disposition": "not_applicable",
            "reason": "no_material_outcome",
            "explanation": "Synthetic exact-type bypass attempt.",
        }
        with self.assertRaisesRegex(ValueError, "requires captured or existing coverage"):
            kg.capture_packet(
                kg.blank_graph(),
                exact_type,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        contradictory = substantive_analysis_packet()
        substantive_coverage_items(contradictory).append(
            {
                "category": "identities_relationships_status",
                "summary": "Identity is also claimed to be inapplicable.",
                "disposition": "not_applicable",
                "reason": "no_material_outcome",
                "explanation": "Synthetic contradictory identity disposition.",
            }
        )
        with self.assertRaisesRegex(ValueError, "cannot combine not_applicable"):
            kg.capture_packet(
                kg.blank_graph(),
                contradictory,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        mixed = substantive_analysis_packet()
        substantive_coverage_items(mixed)[3] = {
            "category": "recommendations_decisions",
            "summary": "Recommendation intentionally omitted.",
            "disposition": "omitted",
            "reason": "insufficient_authority",
            "explanation": "Synthetic mixed-disposition bypass attempt.",
        }
        with self.assertRaisesRegex(ValueError, "requires captured or existing coverage"):
            kg.capture_packet(
                kg.blank_graph(),
                mixed,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="substantial_analysis",
            )

        proposal = substantive_analysis_packet()
        substantive_coverage_items(proposal)[2] = {
            "category": "conclusions_risks_unknowns",
            "summary": "No material risk exists.",
            "disposition": "not_applicable",
            "reason": "no_material_outcome",
            "explanation": "Synthetic proposal-review bypass attempt.",
        }
        with self.assertRaisesRegex(ValueError, "Substantive coverage requires"):
            kg.capture_packet(
                kg.blank_graph(),
                proposal,
                authority_ceiling="validated_structured_candidate",
                discovery_kind="milestone",
            )

        receipt_only = {
            "source": {
                "kind": "document",
                "role": "assistant",
                "title": "Synthetic analysis receipt only",
                "observed_at": "2026-08-12T18:00:00Z",
            },
            "receipts": [
                {
                    "kind": "artifact_verification",
                    "title": "Synthetic analysis receipt",
                    "verified_at": "2026-08-12T18:00:00Z",
                }
            ],
            "coverage": {
                "version": "v1",
                "items": [
                    *[
                        {
                            "category": category,
                            "summary": "No material outcome.",
                            "disposition": "not_applicable",
                            "reason": "no_material_outcome",
                            "explanation": "Synthetic core-category bypass attempt.",
                        }
                        for category in categories[:-1]
                    ],
                    {
                        "category": "artifacts_events_receipts",
                        "summary": "The artifact receipt is captured.",
                        "disposition": "captured",
                        "path": "receipts[0]",
                    },
                ],
            },
        }
        with self.assertRaisesRegex(ValueError, "Substantive coverage requires"):
            kg.capture_packet(
                kg.blank_graph(),
                receipt_only,
                discovery_kind="substantial_analysis",
            )

    def test_event_and_receipt_record_tiers_cannot_override_the_packet(self) -> None:
        def generic_packet(record_tier: str) -> dict[str, object]:
            return {
                "source": {
                    "kind": "report",
                    "role": "assistant",
                    "title": "Assistant-authored generic evidence",
                    "observed_at": "2026-08-12T18:00:00Z",
                    "source_tier": "assistant_inference",
                },
                "events": [
                    {
                        "kind": "milestone",
                        "title": "Synthetic event authority ceiling",
                        "occurred_at": "2026-08-12T18:00:00Z",
                        "source_tier": record_tier,
                    }
                ],
                "receipts": [
                    {
                        "kind": "artifact_verification",
                        "title": "Synthetic receipt authority ceiling",
                        "verified_at": "2026-08-12T18:00:00Z",
                        "source_tier": record_tier,
                    }
                ],
            }

        graph = kg.blank_graph()
        high = kg.capture_packet(graph, generic_packet("explicit_user_statement"))
        self.assertEqual(graph["events"][0]["source_tier"], "assistant_inference")
        self.assertEqual(graph["receipts"][0]["source_tier"], "assistant_inference")
        low = kg.capture_packet(graph, generic_packet("assistant_inference"))
        self.assertEqual(low["status"], "duplicate")
        self.assertEqual(low["fingerprint"], high["fingerprint"])

    def test_one_hop_recall_admits_neighbor_and_relation(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Topology",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [
                    {"ref": "alpha", "type": "client", "name": "Alpha"},
                    {"ref": "beta", "type": "provider", "name": "Beta"},
                ],
                "relations": [{"source": "alpha", "target": "beta", "type": "hosted_by"}],
            },
        )
        result = kg.recall_graph(graph, "Alpha", 20, 20000)
        result_ids = {item["id"] for item in result["results"]}
        self.assertIn(graph["entities"][1]["id"], result_ids)
        self.assertIn(graph["relations"][0]["id"], result_ids)
        beta_result = next(
            item for item in result["results"] if item["id"] == graph["entities"][1]["id"]
        )
        self.assertIn("one_hop_entity", beta_result["reasons"])

    def test_qualified_or_id_recall_does_not_mix_namespaces(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Two production environments",
                    "observed_at": "2026-07-01T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "entities": [
                    {
                        "ref": "one",
                        "type": "environment",
                        "namespace": "client-one",
                        "name": "Production",
                    },
                    {
                        "ref": "two",
                        "type": "environment",
                        "namespace": "client-two",
                        "name": "Production",
                    },
                ],
                "claims": [
                    {
                        "family": "deployment",
                        "key": "marker",
                        "value": "one-only",
                        "entity_refs": ["one"],
                    },
                    {
                        "family": "deployment",
                        "key": "marker",
                        "value": "two-only",
                        "entity_refs": ["two"],
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        client_one = next(
            entity for entity in graph["entities"] if entity["namespace"] == "client-one"
        )
        for query in ("client-one Production", client_one["id"]):
            recalled = kg.recall_graph(graph, query, 20, 20000)
            serialized = json.dumps(recalled)
            self.assertIn("one-only", serialized)
            self.assertNotIn("two-only", serialized)
            self.assertEqual(recalled["seed_entities"], [client_one["id"]])

    def test_review_safe_omits_sensitive_relation_edges(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Private relationship",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [
                    {"ref": "person", "type": "client", "name": "Alice"},
                    {
                        "ref": "condition",
                        "type": "condition",
                        "name": "Condition X",
                        "sensitive": True,
                    },
                ],
                "relations": [
                    {
                        "source": "person",
                        "target": "condition",
                        "type": "has diagnosis",
                    }
                ],
            },
        )
        relation_id = graph["relations"][0]["id"]
        safe = kg.recall_graph(graph, "Alice", 20, 20000, mode="review_safe")
        full = kg.recall_graph(graph, "Alice", 20, 20000, mode="operator_full")
        self.assertNotIn(relation_id, {item["id"] for item in safe["results"]})
        self.assertEqual(safe["one_hop_entities"], [])
        self.assertIn(relation_id, {item["id"] for item in full["results"]})

        notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        alice_path = next(
            path
            for path, body in notes.items()
            if path.startswith("10 Entities/") and "# Alice" in body
        )
        self.assertNotIn("has diagnosis", notes[alice_path])
        self.assertNotIn("Condition X", notes[alice_path])

    def test_projection_bases_use_current_obsidian_filter_schema(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            packet("Current release", "2026-07-01T12:00:00Z", "1.0"),
            authority_ceiling="validated_structured_candidate",
        )

        notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        claims_base = notes["30 Bases/claims.base"]
        entities_base = notes["30 Bases/entities.base"]

        self.assertIn("filters:", claims_base)
        self.assertIn(
            '    - file.inFolder("Codex Knowledge/11 Claims")',
            claims_base,
        )
        self.assertIn('note.kg_type == "claim"', claims_base)
        self.assertNotIn("\nsource:", claims_base)
        self.assertNotIn("\nname:", claims_base)
        self.assertIn("      - formula.readable_note", claims_base)
        self.assertIn("      - about", claims_base)
        self.assertIn("      - formula.readable_state", claims_base)
        self.assertIn("      - formula.readable_source", claims_base)
        self.assertNotIn("      - claim_key", claims_base)
        self.assertNotIn("      - note.claim_key", claims_base)
        self.assertIn("    displayName: Statement", claims_base)
        self.assertIn("    name: Claims", claims_base)
        for identity_field in (
            "entity_type",
            "display_context",
            "formula.readable_state",
        ):
            self.assertIn(f"      - {identity_field}", entities_base)
        self.assertNotIn("      - kg_id", entities_base)
        self.assertNotIn("      - redirect_to", entities_base)

        for relative in (
            "30 Bases/entities.base",
            "30 Bases/claims.base",
            "30 Bases/events.base",
            "30 Bases/open_loops.base",
            "30 Bases/decisions.base",
        ):
            base = notes[relative]
            self.assertIn("    - file.inFolder(", base)
            self.assertNotIn('    displayName: "', base)
            self.assertNotIn('    name: "', base)
            order_section = "\n".join(
                line for line in base.splitlines() if line.startswith("      - ")
            )
            self.assertNotIn("      - note.", order_section)

    def test_revised_event_projection_uses_latest_observation_as_updated_at(self) -> None:
        graph = kg.blank_graph()
        event = {
            "id": "event_revised_incident",
            "kind": "incident",
            "title": "Revised incident",
            "summary": "The incident remains under investigation.",
            "occurred_at": "2026-07-01T12:00:00Z",
            "status": "open",
        }
        for observed_at, status in (
            ("2026-07-01T12:00:00Z", "open"),
            ("2026-07-02T12:00:00Z", "resolved"),
        ):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "document",
                        "role": "tool",
                        "title": f"Incident {status}",
                        "observed_at": observed_at,
                        "source_tier": "validated_structured_candidate",
                    },
                    "events": [{**event, "status": status}],
                },
                authority_ceiling="validated_structured_candidate",
            )

        notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        event_note = next(
            body for body in notes.values() if 'kg_id: "event_revised_incident"' in body
        )

        self.assertIn('occurred_at: "2026-07-01T12:00:00Z"', event_note)
        self.assertIn('updated_at: "2026-07-02T12:00:00Z"', event_note)
        self.assertIn('status: "resolved"', event_note)

    def test_projection_separates_active_candidate_and_resolved_loops(self) -> None:
        graph = kg.blank_graph()
        for title, observed_at, loop_id, loop_title, tier in (
            (
                "Active source",
                "2026-07-01T12:00:00Z",
                "loop_active",
                "Active work",
                "validated_structured_candidate",
            ),
            (
                "Candidate source",
                "2026-07-02T12:00:00Z",
                "loop_candidate",
                "Candidate work",
                "assistant_inference",
            ),
            (
                "Resolved source",
                "2026-07-03T12:00:00Z",
                "loop_resolved",
                "Resolved work",
                "validated_structured_candidate",
            ),
        ):
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "document",
                        "role": "tool",
                        "title": title,
                        "observed_at": observed_at,
                        "source_tier": tier,
                    },
                    "open_loops": [
                        {
                            "id": loop_id,
                            "title": loop_title,
                            "summary": f"Track {loop_title.lower()}.",
                            "status": "open",
                            "priority": 2,
                        }
                    ],
                },
                authority_ceiling=tier,
            )
        kg.resolve_loop(graph, "loop_resolved", "Completed and verified in production.")

        notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        dashboard = notes["00 Dashboard.md"]
        loops_base = notes["30 Bases/open_loops.base"]
        resolved_note = next(body for body in notes.values() if 'kg_id: "loop_resolved"' in body)

        self.assertNotIn("closed_loops", graph)
        self.assertIn("- Loop records: 3", dashboard)
        self.assertIn("- Active open loops: 1", dashboard)
        self.assertIn("- Candidate loops: 1", dashboard)
        self.assertIn("- Resolved loops: 1", dashboard)
        self.assertIn("## Recently Resolved Loops", dashboard)
        self.assertIn("[[30 Bases/open_loops.base|Completed work table]]", dashboard)
        for view_name, status in (
            ("Active", "open"),
            ("Candidates", "candidate"),
            ("Resolved", "resolved"),
        ):
            self.assertIn(f"name: {view_name}", loops_base)
            self.assertIn(f'note.status == "{status}"', loops_base)
        self.assertIn('kg_type: "open_loop"', resolved_note)
        self.assertIn('status: "resolved"', resolved_note)
        self.assertRegex(resolved_note, r'resolved_at: "[^"]+"')
        self.assertIn('resolution: "Completed and verified in production."', resolved_note)

    def test_projection_exposes_open_loop_operating_fields_review_safely(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Assigned work",
                    "observed_at": "2026-07-31T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "entities": [
                    {
                        "ref": "agent_one",
                        "type": "person",
                        "namespace": "example",
                        "name": "Agent One",
                    }
                ],
                "open_loops": [
                    {
                        "id": "loop_assigned_work",
                        "title": "Assigned work",
                        "owner_refs": ["agent_one"],
                        "next_action": "Inspect the next provider response.",
                        "checkpoint_at": "2026-08-01T09:00:00-04:00",
                        "terminal_condition": "Resolve after a verified provider reply.",
                        "status": "open",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )

        owner_id = graph["entities"][0]["id"]
        notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        loop_note = next(body for body in notes.values() if 'kg_id: "loop_assigned_work"' in body)
        loops_base = notes["30 Bases/open_loops.base"]

        self.assertIn("owners:", loop_note)
        self.assertIn("|Agent One]]", loop_note)
        self.assertNotIn(owner_id, loop_note)
        self.assertIn('next_action: "Inspect the next provider response."', loop_note)
        self.assertIn('checkpoint_at: "2026-08-01T13:00:00Z"', loop_note)
        self.assertIn(
            'terminal_condition: "Resolve after a verified provider reply."',
            loop_note,
        )
        self.assertIn("## Owners", loop_note)
        self.assertIn("Next action: Inspect the next provider response.", loop_note)
        for column in (
            "      - owners",
            "      - next_action",
            "      - checkpoint_at",
        ):
            self.assertIn(column, loops_base)

        graph["open_loops"][0]["sensitive"] = True
        sensitive_notes = kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")
        sensitive_loop_note = next(
            body for body in sensitive_notes.values() if 'kg_id: "loop_assigned_work"' in body
        )
        self.assertNotIn("Inspect the next provider response.", sensitive_loop_note)
        self.assertNotIn("Resolve after a verified provider reply.", sensitive_loop_note)
        self.assertNotIn("|Agent One]]", sensitive_loop_note)
        self.assertIn("Redacted in review_safe mode", sensitive_loop_note)

    def test_generic_identity_normalizes_ref_order_and_timestamp(self) -> None:
        graph = kg.blank_graph()
        base = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "First evidence",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "entities": [
                {"ref": "one", "type": "client", "name": "One"},
                {"ref": "two", "type": "provider", "name": "Two"},
            ],
            "events": [
                {
                    "kind": "incident",
                    "title": "Equivalent event",
                    "occurred_at": "2026-07-01T12:00:00Z",
                    "entity_refs": ["one", "two"],
                }
            ],
        }
        kg.capture_packet(graph, base)
        equivalent = json.loads(json.dumps(base))
        equivalent["source"]["title"] = "Second evidence"
        equivalent["source"]["observed_at"] = "2026-07-01T12:00:00+00:00"
        equivalent["events"][0]["occurred_at"] = "2026-07-01T12:00:00+00:00"
        equivalent["events"][0]["entity_refs"] = ["two", "one"]
        kg.capture_packet(graph, equivalent)
        self.assertEqual(len(graph["events"]), 1)
        self.assertEqual(len(graph["events"][0]["evidence_refs"]), 2)

    def test_equivalent_timestamp_spellings_are_duplicate_packets(self) -> None:
        graph = kg.blank_graph()
        item = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Equivalent timestamp packet",
                "observed_at": "2026-07-31T12:00:00Z",
            },
            "events": [
                {
                    "kind": "incident",
                    "title": "Equivalent timestamp event",
                    "occurred_at": "2026-07-31T12:00:00Z",
                }
            ],
        }
        first = kg.capture_packet(graph, item)
        equivalent = json.loads(json.dumps(item))
        equivalent["source"]["observed_at"] = "2026-07-31T08:00:00-04:00"
        equivalent["events"][0]["occurred_at"] = "2026-07-31T08:00:00-04:00"
        second = kg.capture_packet(graph, equivalent)
        self.assertEqual(first["status"], "captured")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(graph["sources"]), 1)
        self.assertEqual(len(graph["journal"]), 1)

    def test_fingerprint_normalizes_set_fields_and_stored_defaults(self) -> None:
        graph = kg.blank_graph()
        item = {
            "source": {
                "title": "Semantic fingerprint",
                "observed_at": "2026-07-31T12:00:00Z",
            },
            "entities": [
                {"ref": "one", "type": "client", "name": "One"},
                {"ref": "two", "type": "provider", "name": "Two"},
            ],
            "events": [
                {
                    "title": "Same event",
                    "occurred_at": "2026-07-31T12:00:00Z",
                    "entity_refs": ["one", "two"],
                }
            ],
        }
        first = kg.capture_packet(graph, item)
        explicit = json.loads(json.dumps(item))
        explicit["source"].update(
            {
                "kind": "other",
                "role": "unknown",
                "uri": "",
                "excerpt": "",
                "source_tier": "assistant_inference",
                "sensitive": False,
            }
        )
        for entity in explicit["entities"]:
            entity.update(
                {
                    "namespace": "global",
                    "aliases": [],
                    "status": "current",
                    "sensitive": False,
                }
            )
        explicit["events"][0].update(
            {
                "kind": "event",
                "summary": "",
                "status": "recorded",
                "sensitive": False,
            }
        )
        explicit["events"][0]["entity_refs"] = ["two", "one"]
        second = kg.capture_packet(graph, explicit)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(graph["sources"]), 1)
        self.assertEqual(len(graph["journal"]), 1)

    def test_entity_and_relation_state_respects_observation_time(self) -> None:
        graph = kg.blank_graph()

        def topology(title: str, observed_at: str, status: str) -> dict[str, object]:
            return {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": title,
                    "observed_at": observed_at,
                    "source_tier": "validated_structured_candidate",
                },
                "entities": [
                    {
                        "ref": "client",
                        "type": "client",
                        "name": "Client",
                        "status": status,
                    },
                    {"ref": "provider", "type": "provider", "name": "Provider"},
                ],
                "relations": [
                    {
                        "source": "client",
                        "target": "provider",
                        "type": "hosted_by",
                        "status": status,
                    }
                ],
            }

        kg.capture_packet(
            graph,
            topology("Current topology", "2026-07-02T12:00:00Z", "current"),
            authority_ceiling="validated_structured_candidate",
        )
        kg.capture_packet(
            graph,
            topology("Backfill topology", "2026-07-01T12:00:00Z", "retired"),
            authority_ceiling="validated_structured_candidate",
        )
        client = next(entity for entity in graph["entities"] if entity["name"] == "Client")
        relation = graph["relations"][0]
        self.assertEqual(client["status"], "current")
        self.assertEqual(client["first_seen_at"], "2026-07-01T12:00:00Z")
        self.assertEqual(client["last_seen_at"], "2026-07-02T12:00:00Z")
        self.assertEqual(relation["status"], "current")
        self.assertEqual(relation["first_observed_at"], "2026-07-01T12:00:00Z")
        self.assertEqual(relation["last_observed_at"], "2026-07-02T12:00:00Z")

        kg.capture_packet(
            graph,
            topology("Retired topology", "2026-07-03T12:00:00Z", "retired"),
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(client["status"], "retired")
        self.assertEqual(client["last_seen_at"], "2026-07-03T12:00:00Z")
        self.assertEqual(relation["status"], "retired")
        self.assertEqual(relation["last_observed_at"], "2026-07-03T12:00:00Z")

    def test_fractional_event_times_remain_distinct(self) -> None:
        graph = kg.blank_graph()
        item = {
            "source": {
                "kind": "log",
                "role": "tool",
                "title": "Subsecond events",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "events": [
                {
                    "kind": "request",
                    "title": "Request completed",
                    "occurred_at": "2026-07-01T12:00:00.100000Z",
                },
                {
                    "kind": "request",
                    "title": "Request completed",
                    "occurred_at": "2026-07-01T12:00:00.200000Z",
                },
            ],
        }
        kg.capture_packet(graph, item)
        self.assertEqual(len(graph["events"]), 2)
        self.assertNotEqual(graph["events"][0]["id"], graph["events"][1]["id"])

    def test_duplicate_packet_local_refs_are_rejected(self) -> None:
        graph = kg.blank_graph()
        item = {
            "source": {
                "kind": "document",
                "role": "tool",
                "title": "Ambiguous refs",
                "observed_at": "2026-07-01T12:00:00Z",
            },
            "entities": [
                {"ref": "target", "type": "client", "name": "One"},
                {"ref": "target", "type": "client", "name": "Two"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "points to more than one entity"):
            kg.capture_packet(graph, item)

    def test_entity_recall_includes_its_evidence_source(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Alpha source evidence",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [{"type": "client", "name": "Alpha Evidence Client"}],
            },
        )
        recalled = kg.recall_graph(graph, "Alpha Evidence Client", 10, 12000)
        source_id = graph["sources"][0]["id"]
        source_result = next(item for item in recalled["results"] if item["id"] == source_id)
        self.assertIn("seed_entity_evidence", source_result["reasons"])

    def test_claim_observations_do_not_cross_temporal_episodes(self) -> None:
        graph = kg.blank_graph()
        for index, value in enumerate(("A", "B", "A"), start=1):
            moment = f"2026-07-0{index}T12:00:00Z"
            item = packet(f"Episode {index}", moment, value)
            item["observations"] = [
                {
                    "family": "deployment",
                    "key": "plugin.version",
                    "value": value,
                    "entity_refs": ["site"],
                    "observed_at": moment,
                }
            ]
            kg.capture_packet(
                graph,
                item,
                authority_ceiling="validated_structured_candidate",
                allow_replace=True,
            )
        latest = next(claim for claim in graph["claims"] if claim["active"])
        self.assertEqual(latest["value"], "A")
        self.assertEqual(len(latest["observation_refs"]), 1)
        observation = kg.index_by_id(graph["observations"])[latest["observation_refs"][0]]
        self.assertEqual(observation["source_ref"], latest["evidence_refs"][0])

    def test_observation_identity_includes_entity_scope(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "log",
                    "role": "tool",
                    "title": "Scoped observations",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [
                    {"ref": "alpha", "type": "client", "name": "Alpha"},
                    {"ref": "beta", "type": "client", "name": "Beta"},
                ],
                "observations": [
                    {"family": "status", "key": "health", "value": "ok", "entity_refs": ["alpha"]},
                    {"family": "status", "key": "health", "value": "ok", "entity_refs": ["beta"]},
                ],
            },
        )
        self.assertEqual(len(graph["observations"]), 2)
        self.assertNotEqual(graph["observations"][0]["id"], graph["observations"][1]["id"])

    def test_resolved_open_loop_recurrence_creates_new_episode(self) -> None:
        graph = kg.blank_graph()
        first = {
            "source": {
                "kind": "support",
                "role": "tool",
                "title": "First occurrence",
                "observed_at": "2026-07-01T12:00:00Z",
                "source_tier": "validated_structured_candidate",
            },
            "open_loops": [{"title": "Provider follow-up", "status": "open"}],
        }
        kg.capture_packet(graph, first, authority_ceiling="validated_structured_candidate")
        original_id = graph["open_loops"][0]["id"]
        kg.resolve_loop(graph, original_id, "Provider replied")
        second = json.loads(json.dumps(first))
        second["source"].update(
            {"title": "Second occurrence", "observed_at": "2026-07-02T12:00:00Z"}
        )
        kg.capture_packet(graph, second, authority_ceiling="validated_structured_candidate")
        self.assertEqual(len(graph["open_loops"]), 2)
        self.assertEqual(graph["open_loops"][0]["status"], "resolved")
        self.assertEqual(graph["open_loops"][1]["status"], "open")

    def test_mutation_retries_are_idempotent(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "support",
                    "role": "tool",
                    "title": "Loop",
                    "observed_at": "2026-07-01T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "open_loops": [{"title": "Wait", "status": "open"}],
            },
            authority_ceiling="validated_structured_candidate",
        )
        loop_id = graph["open_loops"][0]["id"]
        first = kg.resolve_loop(graph, loop_id, "Done")
        resolved_at = graph["open_loops"][0]["resolved_at"]
        second = kg.resolve_loop(graph, loop_id, "Done")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(graph["open_loops"][0]["resolved_at"], resolved_at)

        entity_id = graph["sources"][0]["id"]
        self.assertTrue(kg.redact_record(graph, entity_id)["changed"])
        self.assertFalse(kg.redact_record(graph, entity_id)["changed"])

    def test_projection_paths_are_casefold_unique_and_root_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "collision"):
            kg.assert_unique_projection_paths(["10 Entities/Foo.md", "10 entities/foo.md"])
        first = kg.note_path("10 Entities", {"id": "entity_one"}, "Same")
        second = kg.note_path("10 Entities", {"id": "entity_two"}, "Same")
        self.assertNotEqual(first, second)

        graph = kg.blank_graph()
        kg.capture_packet(graph, packet("Initial", "2026-07-01T12:00:00Z", "1.0"))
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            vault_one = root / "vault-one"
            vault_two = root / "vault-two"
            make_private_directory(vault_one)
            make_private_directory(vault_two)
            key = "e" * 64
            kg.project_graph(graph, vault_one, "Codex Knowledge", "review_safe", key)
            shutil.copytree(vault_one / "Codex Knowledge", vault_two / "Codex Knowledge")
            kg.set_private_path_mode(vault_two / "Codex Knowledge", 0o700)
            with self.assertRaisesRegex(ValueError, "different configured root"):
                kg.project_graph(graph, vault_two, "Codex Knowledge", "review_safe", key)

    def test_base_ownership_marker_survives_obsidian_yaml_reordering(self) -> None:
        relative = "30 Bases/claims.base"
        manifest_key = "b" * 64
        root_id = "projection_root_test"
        marker = kg.projection_file_marker(manifest_key, root_id, relative)
        self.assertTrue(marker.startswith("x-codex-generated: "))

        with private_temporary_directory() as temporary:
            path = Path(temporary) / "claims.base"
            path.write_text(
                kg.marked_projection_content(
                    manifest_key,
                    root_id,
                    relative,
                    'filters:\n  and:\n    - note.kg_type == "claim"\nviews: []\n',
                ),
                encoding="utf-8",
            )
            self.assertTrue(kg.projection_file_is_owned(path, manifest_key, root_id, relative))

            marker_value = marker.split(": ", 1)[1]
            path.write_text(
                'filters:\n  and:\n    - note.kg_type == "claim"\nviews: []\n'
                + f"# codex-generated:{marker_value}\n",
                encoding="utf-8",
            )
            self.assertTrue(kg.projection_file_is_owned(path, manifest_key, root_id, relative))

            path.write_text(
                marker + '\nfilters:\n  and:\n    - note.kg_type == "claim"\nviews: []\n',
                encoding="utf-8",
            )
            self.assertTrue(kg.projection_file_is_owned(path, manifest_key, root_id, relative))

    def test_marked_base_matches_obsidian_stable_serialization(self) -> None:
        relative = "30 Bases/claims.base"
        manifest_key = "c" * 64
        root_id = "projection_root_test"
        content = kg.base_file(
            "Claims",
            "Codex Knowledge/11 Claims",
            "claim",
            ["family", "claim_key"],
        )
        marked = kg.marked_projection_content(manifest_key, root_id, relative, content)

        self.assertEqual(
            marked,
            kg.projection_file_marker(manifest_key, root_id, relative)
            + "\n"
            + "filters:\n"
            + "  and:\n"
            + '    - file.inFolder("Codex Knowledge/11 Claims")\n'
            + '    - note.kg_type == "claim"\n'
            + "formulas:\n"
            + "  readable_note: link(file.path, if(note.display_title, "
            + "note.display_title, file.name))\n"
            + "properties:\n"
            + "  formula.readable_note:\n"
            + "    displayName: Statement\n"
            + "  note.family:\n"
            + "    displayName: Family\n"
            + "  note.claim_key:\n"
            + "    displayName: Claim key\n"
            + "views:\n"
            + "  - type: table\n"
            + "    name: Claims\n"
            + "    order:\n"
            + "      - formula.readable_note\n"
            + "      - family\n"
            + "      - claim_key\n",
        )
        with private_temporary_directory() as temporary:
            path = Path(temporary) / "claims.base"
            path.write_text(marked, encoding="utf-8")
            self.assertTrue(kg.projection_file_is_owned(path, manifest_key, root_id, relative))

    @unittest.skipIf(os.name == "nt", "POSIX mode-bit policy is not a Windows ACL test")
    def test_projection_rejects_shared_writable_vault_or_ancestor(self) -> None:
        graph = kg.blank_graph()
        manifest_key = "a" * 64
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            writable_vault = root / "writable-vault"
            writable_vault.mkdir(mode=0o700)
            writable_vault.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "group/world writable"):
                    kg.project_graph(
                        graph,
                        writable_vault,
                        "Codex Knowledge",
                        "review_safe",
                        manifest_key,
                        "writable-vault-root",
                    )
            finally:
                writable_vault.chmod(0o700)

            writable_parent = root / "writable-parent"
            writable_parent.mkdir(mode=0o700)
            private_vault = writable_parent / "private-vault"
            private_vault.mkdir(mode=0o700)
            writable_parent.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "group/world writable"):
                    kg.project_graph(
                        graph,
                        private_vault,
                        "Codex Knowledge",
                        "review_safe",
                        manifest_key,
                        "writable-parent-root",
                    )
            finally:
                writable_parent.chmod(0o700)

    @unittest.skipIf(os.name == "nt", "POSIX mode-bit policy is not a Windows ACL test")
    def test_atomic_write_does_not_repermission_existing_parent(self) -> None:
        with private_temporary_directory() as temporary:
            shared_parent = Path(temporary) / "shared-parent"
            shared_parent.mkdir(mode=0o755)
            shared_parent.chmod(0o755)
            before = shared_parent.stat().st_mode & 0o777
            kg.atomic_write(shared_parent / "graph.json", "{}\n")
            after = shared_parent.stat().st_mode & 0o777
            self.assertEqual(before, 0o755)
            self.assertEqual(after, before)
            self.assertEqual((shared_parent / "graph.json").stat().st_mode & 0o777, 0o600)

    def test_projection_preserves_review_actions_and_rejects_tampered_manifest(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            packet("Initial", "2026-07-01T12:00:00-04:00", "1.0"),
            authority_ceiling="validated_structured_candidate",
        )
        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            make_private_directory(vault)
            key = "a" * 64
            kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            if os.name != "nt":
                self.assertEqual((vault / "Codex Knowledge").stat().st_mode & 0o777, 0o700)
            review_note = vault / "Codex Knowledge" / "40 Review Actions" / "human.md"
            review_note.write_text("keep me", encoding="utf-8")
            kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            self.assertEqual(review_note.read_text(encoding="utf-8"), "keep me")

            manifest_path = vault / "Codex Knowledge" / kg.PROJECTION_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["generated_files"].append("40 Review Actions/human.md")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            self.assertTrue(review_note.exists())

    def test_initial_projection_recovers_after_interrupted_write(self) -> None:
        graph = kg.blank_graph()
        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            make_private_directory(vault)
            key = "9" * 64
            original_atomic_write = cast(Callable[[Path, str, int], None], kg.atomic_write)
            calls = 0

            def interrupted_write(path: Path, content: str, mode: int = 0o600) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected projection interruption")
                original_atomic_write(path, content, mode)

            with mock.patch.object(kg, "atomic_write", interrupted_write):
                with self.assertRaisesRegex(OSError, "injected"):
                    kg.project_graph(
                        graph,
                        vault,
                        "Codex Knowledge",
                        "review_safe",
                        key,
                        "recoverable-root",
                    )
            projection_root = vault / "Codex Knowledge"
            self.assertTrue((projection_root / "00 Dashboard.md").is_file())
            self.assertFalse((projection_root / kg.PROJECTION_MANIFEST).exists())
            kg.project_graph(
                graph,
                vault,
                "Codex Knowledge",
                "review_safe",
                key,
                "recoverable-root",
            )
            self.assertTrue((projection_root / kg.PROJECTION_MANIFEST).is_file())

    def test_validation_rejects_dangling_entity_and_journal_lineage(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "Entity lineage",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [{"type": "client", "name": "Lineage Client"}],
            },
        )
        graph["sources"].clear()
        validation = kg.validate_graph(graph)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("references missing source" in error for error in validation["errors"]))
        self.assertTrue(any("missing changed record" in error for error in validation["errors"]))

    def test_replayed_manifest_cannot_leave_signed_stale_notes(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "document",
                    "role": "tool",
                    "title": "ULTRA_OPERATOR_ONLY_SOURCE",
                    "observed_at": "2026-07-01T12:00:00Z",
                    "sensitive": True,
                },
                "entities": [
                    {
                        "type": "client",
                        "name": "ULTRA_OPERATOR_ONLY_CLIENT",
                        "sensitive": True,
                    }
                ],
            },
        )
        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            make_private_directory(vault)
            key = "f" * 64
            kg.project_graph(graph, vault, "Codex Knowledge", "operator_full", key)
            root = vault / "Codex Knowledge"
            manifest_path = root / kg.PROJECTION_MANIFEST
            replayed_manifest = manifest_path.read_text(encoding="utf-8")
            kg.redact_record(graph, graph["entities"][0]["id"])
            kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            manifest_path.write_text(replayed_manifest, encoding="utf-8")
            kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            projected = "\n".join(
                path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()
            )
            self.assertNotIn("ULTRA_OPERATOR_ONLY_CLIENT", projected)
            self.assertNotIn("ULTRA_OPERATOR_ONLY_SOURCE", projected)

    def test_review_safe_projection_hides_sensitive_names_and_rejects_symlinks(self) -> None:
        graph = kg.blank_graph()
        sensitive_packet = {
            "source": {
                "kind": "document",
                "role": "operator",
                "title": "ULTRA_PRIVATE_SOURCE_TITLE",
                "observed_at": "2026-07-01T12:00:00-04:00",
                "sensitive": True,
            },
            "entities": [
                {
                    "ref": "private",
                    "type": "client",
                    "name": "ULTRA_PRIVATE_CLIENT_NAME",
                    "aliases": ["ULTRA_PRIVATE_ALIAS"],
                    "sensitive": True,
                }
            ],
            "claims": [
                {
                    "family": "ULTRA_PRIVATE_FAMILY",
                    "key": "ULTRA_PRIVATE_KEY",
                    "value": "ULTRA_PRIVATE_VALUE",
                    "entity_refs": ["private"],
                    "sensitive": True,
                }
            ],
        }
        kg.capture_packet(graph, sensitive_packet)
        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            make_private_directory(vault)
            key = "b" * 64
            kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            root = vault / "Codex Knowledge"
            projected = "\n".join(
                f"{path.relative_to(root)}\n{path.read_text(encoding='utf-8')}"
                for path in root.rglob("*")
                if path.is_file()
            )
            for private_text in (
                "ULTRA_PRIVATE_SOURCE_TITLE",
                "ULTRA_PRIVATE_CLIENT_NAME",
                "ULTRA_PRIVATE_ALIAS",
                "ULTRA_PRIVATE_FAMILY",
                "ULTRA_PRIVATE_KEY",
                "ULTRA_PRIVATE_VALUE",
            ):
                self.assertNotIn(private_text, projected)

        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            root = vault / "Codex Knowledge"
            review_actions = root / "40 Review Actions"
            make_private_directory(vault)
            make_private_directory(root)
            make_private_directory(review_actions)
            (root / "10 Entities").symlink_to(review_actions, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "nonempty|symlink"):
                kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", "c" * 64)

        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            root = vault / "Codex Knowledge"
            make_private_directory(vault)
            make_private_directory(root)
            human_dashboard = root / "00 Dashboard.md"
            human_dashboard.write_text("human content", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonempty"):
                kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", "d" * 64)
            self.assertEqual(human_dashboard.read_text(encoding="utf-8"), "human content")

    def test_review_safe_projection_sanitizes_all_user_metadata(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": {
                    "kind": "/Users/alice/Private/source.log",
                    "role": "/var/www/operator",
                    "title": "Metadata sanitization",
                    "observed_at": "2026-07-01T12:00:00Z",
                },
                "entities": [
                    {
                        "ref": "site",
                        "type": "site",
                        "namespace": "/Users/alice/Private/Acme",
                        "name": "Metadata Site",
                        "status": "/var/www/private",
                    }
                ],
                "events": [
                    {
                        "kind": "/mnt/customer/private-kind",
                        "title": "Metadata event",
                        "status": "D:\\Projects\\Acme\\private-status",
                        "entity_refs": ["site"],
                    }
                ],
            },
        )
        safe = "\n".join(
            kg.build_projection_notes(graph, "review_safe", "Codex Knowledge").values()
        ).casefold()
        full = "\n".join(
            kg.build_projection_notes(graph, "operator_full", "Codex Knowledge").values()
        ).casefold()
        full_unescaped = full.replace("\\\\", "\\")
        for private_value in (
            "/users/alice/private/source.log",
            "/var/www/operator",
            "/users/alice/private/acme",
            "/var/www/private",
            "/mnt/customer/private-kind",
            "d:\\projects\\acme\\private-status",
        ):
            self.assertNotIn(private_value, safe)
            self.assertIn(private_value, full_unescaped)
        self.assertNotIn("/users/alice", safe)

    def test_recall_and_projection_reject_preexisting_unsafe_ids(self) -> None:
        graph = kg.blank_graph()
        graph["entities"].append(
            {
                "id": "/Users/alice/Private/entity",
                "namespace": "global",
                "type": "client",
                "name": "Legacy unsafe entity",
                "aliases": [],
                "status": "current",
                "first_seen_at": "2026-07-01T12:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
                "evidence_refs": [],
                "source_tier": "assistant_inference",
                "sensitive": False,
            }
        )
        with self.assertRaisesRegex(ValueError, "invalid graph"):
            kg.recall_graph(graph, "Legacy unsafe entity", 10, 12000)
        with self.assertRaisesRegex(ValueError, "invalid graph"):
            kg.build_projection_notes(graph, "review_safe", "Codex Knowledge")

    def test_cli_end_to_end_and_duplicate_projection_retry(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            make_private_directory(vault)
            config = root / "config.json"
            store = root / "graph.json"
            capture_file = root / "capture.json"
            capture_file.write_text(
                json.dumps(packet("Initial", "2026-07-01T12:00:00-04:00", "1.0")),
                encoding="utf-8",
            )

            configured = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(store),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertNotIn("projection_manifest_key", configured.stdout)

            command = [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(config),
                "capture",
                "--input",
                str(capture_file),
                "--project",
                "--authority-ceiling",
                "validated_structured_candidate",
            ]
            dry_run = json.loads(
                subprocess.run(
                    [*command, "--dry-run"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(dry_run["status"], "would_capture")
            mismatch = subprocess.run(
                [
                    *command,
                    "--expect-fingerprint",
                    "not-the-reviewed-fingerprint",
                    "--expect-watermark",
                    str(dry_run["watermark"]),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertFalse(store.exists())
            command.extend(
                [
                    "--expect-fingerprint",
                    dry_run["fingerprint"],
                    "--expect-watermark",
                    str(dry_run["watermark"]),
                ]
            )
            first = json.loads(
                subprocess.run(command, check=True, capture_output=True, text=True).stdout
            )
            second = json.loads(
                subprocess.run(command, check=True, capture_output=True, text=True).stdout
            )
            self.assertEqual(first["status"], "captured")
            self.assertEqual(second["status"], "duplicate")
            self.assertIsNotNone(second["projection"])

            recalled = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--config",
                        str(config),
                        "recall",
                        "Test Client Alpha plugin",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertGreater(recalled["result_count"], 0)
            self.assertIn("current_claims", recalled["recall_order"])

            other_vault = root / "other-vault"
            make_private_directory(other_vault)
            redirected = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "project",
                    "--vault",
                    str(other_vault),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(redirected.returncode, 0)
            self.assertFalse((other_vault / kg.DEFAULT_ROOT_NAME).exists())

            alternate_store = root / "alternate-graph.json"
            kg.save_graph(alternate_store, kg.blank_graph())
            dashboard = vault / kg.DEFAULT_ROOT_NAME / "00 Dashboard.md"
            dashboard_before = dashboard.read_text(encoding="utf-8")
            alternate_projection = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "project",
                    "--store",
                    str(alternate_store),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(alternate_projection.returncode, 0)
            self.assertIn("other than the one bound", alternate_projection.stderr)
            self.assertEqual(dashboard.read_text(encoding="utf-8"), dashboard_before)

    def test_v3_coverage_apply_requires_reviewed_fingerprint_and_watermark(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            store = root / "graph.json"
            capture_file = root / "coverage.json"
            graph = kg.blank_graph()
            kg.capture_packet(
                graph,
                {
                    "source": {
                        "kind": "document",
                        "role": "tool",
                        "title": "Existing coverage identity",
                        "observed_at": "2026-08-12T18:00:00Z",
                    },
                    "entities": [
                        {
                            "id": "entity_v3_coverage_existing",
                            "type": "project",
                            "name": "V3 Coverage Existing",
                        }
                    ],
                },
            )
            kg.save_graph(store, graph)
            covered = substantive_analysis_packet()
            substantive_coverage_items(covered)[0] = {
                "category": "identities_relationships_status",
                "summary": "Reuse the existing v3 identity.",
                "disposition": "existing",
                "record_id": "entity_v3_coverage_existing",
            }
            capture_file.write_text(json.dumps(covered), encoding="utf-8")
            command = [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(config),
                "capture",
                "--store",
                str(store),
                "--input",
                str(capture_file),
                "--authority-ceiling",
                "validated_structured_candidate",
                "--discovery-kind",
                "substantial_analysis",
            ]
            dry = json.loads(
                subprocess.run(
                    [*command, "--dry-run"], check=True, capture_output=True, text=True
                ).stdout
            )
            missing_gate = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(missing_gate.returncode, 0)
            self.assertIn("--expect-fingerprint and --expect-watermark", missing_gate.stderr)

            changed = kg.load_graph(store)
            kg.capture_packet(
                changed,
                {
                    "source": {
                        "kind": "document",
                        "role": "tool",
                        "title": "Existing identity updated after review",
                        "observed_at": "2026-08-12T19:00:00Z",
                        "source_tier": "validated_structured_candidate",
                    },
                    "entities": [
                        {
                            "id": "entity_v3_coverage_existing",
                            "type": "project",
                            "name": "V3 Coverage Existing Updated",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
            )
            kg.save_graph(store, changed)
            stale = subprocess.run(
                [
                    *command,
                    "--expect-fingerprint",
                    dry["fingerprint"],
                    "--expect-watermark",
                    str(dry["watermark"]),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("Graph watermark changed", stale.stderr)

    def test_cli_refuses_empty_projection_and_vault_backed_state(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            make_private_directory(vault)
            config = root / "config.json"
            missing_store = root / "missing-graph.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(missing_store),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            refused = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(config), "project"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse((vault / kg.DEFAULT_ROOT_NAME).exists())

            inside_config = vault / "private-config.json"
            inside_store = vault / "private-graph.json"
            inside = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(inside_config),
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(inside_store),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(inside.returncode, 0)
            self.assertFalse(inside_config.exists())

    def test_cli_rejects_state_symlink_redirection(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            private = root / "private"
            shared = root / "shared"
            make_private_directory(vault)
            make_private_directory(private)
            make_private_directory(shared)
            (shared / "kg").symlink_to(private, target_is_directory=True)
            redirected_config = shared / "kg" / "settings.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(redirected_config),
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(private / "graph.json"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((private / "settings.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
