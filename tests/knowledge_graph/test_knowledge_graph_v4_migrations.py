#!/usr/bin/env python3
"""Characterization tests for the v3-to-v4 migration service facade."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
import knowledge_graph_v4_integration as integration  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph import v4_migrations as migrations  # noqa: E402
from tedd_knowledge_graph.v4_cli_contracts import (  # noqa: E402
    ProducerEnvelope,
    UpgradeRequest,
)


class SimulatedCutoverCrash(RuntimeError):
    """Raised after one durable publication to model process interruption."""


class MigrationFacadeTests(unittest.TestCase):
    def test_facade_binds_dependencies_at_call_time(self) -> None:
        request = object()
        config_path = Path("config.json")
        result = {"status": "delegated"}
        atomic_bytes = mock.Mock()
        validate_graph = mock.Mock()

        with (
            mock.patch.object(integration, "_atomic_bytes", atomic_bytes),
            mock.patch.object(integration.legacy, "validate_graph", validate_graph),
            mock.patch.object(
                integration._v4_migrations,
                "upgrade_v3_to_v4",
                return_value=result,
            ) as delegate,
        ):
            actual = integration.upgrade_v3_to_v4(
                request,
                config_path=config_path,
                lock_timeout=3.5,
            )

        self.assertIs(actual, result)
        delegate.assert_called_once()
        call = delegate.call_args
        self.assertIs(call.args[0], request)
        self.assertEqual(call.kwargs["config_path"], config_path)
        self.assertEqual(call.kwargs["lock_timeout"], 3.5)
        dependencies = call.kwargs["dependencies"]
        self.assertIs(dependencies.atomic_bytes, atomic_bytes)
        self.assertIs(dependencies.validate_legacy, validate_graph)


class MigrationCutoverRecoveryTests(unittest.TestCase):
    def _fixture(
        self, root: Path
    ) -> tuple[Path, Path, Path, migrations.MigrationDependencies, UpgradeRequest]:
        source = (root / "graph.json").resolve()
        target = (root / "graph.sqlite3").resolve()
        config = (root / "config.json").resolve()
        dependencies = integration._migration_dependencies()
        legacy = integration._legacy_module()
        set_private_path_mode(root, 0o700)
        graph = legacy.blank_graph()
        dependencies.atomic_write(
            source,
            json.dumps(graph, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
        dependencies.atomic_write(
            config,
            json.dumps(
                {
                    "schema_version": "v2",
                    "store_path": str(source),
                    "canonical_store_format": "json_v3",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            mode=0o600,
        )
        dry_request = UpgradeRequest(
            source_path=source,
            target_path=target,
            dry_run=True,
            expected_source_sha256=None,
            expected_plan_fingerprint=None,
            backup_output=None,
            envelope=None,
        )
        dry = migrations.upgrade_v3_to_v4(
            dry_request,
            config_path=config,
            lock_timeout=3.0,
            dependencies=dependencies,
        )
        source_sha256 = dry["source_sha256"]
        plan_fingerprint = dry["plan_fingerprint"]
        plan = dry["plan"]
        self.assertIsInstance(source_sha256, str)
        self.assertIsInstance(plan_fingerprint, str)
        self.assertIsInstance(plan, dict)
        if (
            not isinstance(source_sha256, str)
            or not isinstance(plan_fingerprint, str)
            or not isinstance(plan, dict)
        ):
            raise AssertionError("upgrade result bindings must have stable types")
        source_watermark = plan["source_watermark"]
        self.assertIsInstance(source_watermark, int)
        request = UpgradeRequest(
            source_path=source,
            target_path=target,
            dry_run=False,
            expected_source_sha256=source_sha256,
            expected_plan_fingerprint=plan_fingerprint,
            backup_output=None,
            envelope=ProducerEnvelope(
                producer_agent_id="migration-agent",
                task_id="migration-task",
                parent_task_id=None,
                producer_run_id="migration-run",
                discovery_kind="maintenance",
                based_on_watermark=source_watermark,
            ),
        )
        return source, target, config, dependencies, request

    def _crashing_dependencies(
        self,
        dependencies: migrations.MigrationDependencies,
        *,
        boundary: str,
        source: Path,
        target: Path,
        config: Path,
    ) -> migrations.MigrationDependencies:
        target_key = target.with_name(f"{target.name}.plan.key")

        def publish_then_crash(
            staged: Path,
            destination: Path,
            *,
            overwrite: bool,
        ) -> tuple[int, int]:
            identity = dependencies.publish_staged_file(
                staged,
                destination,
                overwrite=overwrite,
            )
            if (boundary == "database" and destination == target) or (
                boundary == "key" and destination == target_key
            ):
                raise SimulatedCutoverCrash(boundary)
            return identity

        def write_then_crash(path: Path, content: str, mode: int = 0o600) -> None:
            dependencies.atomic_write(path, content, mode)
            if (boundary == "config" and path == config) or (
                boundary == "tombstone" and path == source
            ):
                raise SimulatedCutoverCrash(boundary)

        return replace(
            dependencies,
            publish_staged_file=cast(
                migrations.PublishStagedFile,
                publish_then_crash,
            ),
            atomic_write=write_then_crash,
        )

    def _racing_dependencies(
        self,
        dependencies: migrations.MigrationDependencies,
        *,
        matches: Callable[[Path], bool],
        competing_bytes: bytes,
    ) -> tuple[migrations.MigrationDependencies, threading.Thread]:
        barrier = threading.Barrier(2)
        raced_destination: list[Path] = []
        competitor_failures: list[BaseException] = []

        def competitor() -> None:
            try:
                destination = raced_destination[0]
                barrier.wait(timeout=10.0)
                dependencies.atomic_bytes(destination, competing_bytes)
                barrier.wait(timeout=10.0)
            except BaseException as error:
                competitor_failures.append(error)
                barrier.abort()

        thread = threading.Thread(
            target=competitor,
            name="tedd-migration-publication-competitor",
        )

        def publish_with_race(
            staged: Path,
            destination: Path,
            *,
            overwrite: bool,
        ) -> tuple[int, int]:
            if matches(destination):
                if raced_destination:
                    raise AssertionError("publication race matched more than once")
                raced_destination.append(destination)
                thread.start()
                try:
                    barrier.wait(timeout=10.0)
                    barrier.wait(timeout=10.0)
                except BaseException as error:
                    barrier.abort()
                    thread.join(timeout=10.0)
                    if thread.is_alive():
                        raise AssertionError(
                            "publication competitor did not stop after owner failure"
                        ) from error
                    if competitor_failures:
                        raise competitor_failures[0] from error
                    raise
                thread.join(timeout=10.0)
                if thread.is_alive():
                    raise AssertionError("publication competitor did not stop")
                if competitor_failures:
                    raise competitor_failures[0]
            return dependencies.publish_staged_file(
                staged,
                destination,
                overwrite=overwrite,
            )

        return (
            replace(
                dependencies,
                publish_staged_file=cast(
                    migrations.PublishStagedFile,
                    publish_with_race,
                ),
            ),
            thread,
        )

    def _link_then_crash_dependencies(
        self,
        dependencies: migrations.MigrationDependencies,
        *,
        matches: Callable[[Path], bool],
    ) -> migrations.MigrationDependencies:
        def link_then_crash(
            staged: Path,
            destination: Path,
            *,
            overwrite: bool,
        ) -> tuple[int, int]:
            if not matches(destination):
                return dependencies.publish_staged_file(
                    staged,
                    destination,
                    overwrite=overwrite,
                )
            self.assertFalse(overwrite)
            os.link(staged, destination)
            raise SimulatedCutoverCrash(destination.name)

        return replace(
            dependencies,
            publish_staged_file=cast(migrations.PublishStagedFile, link_then_crash),
        )

    def _assert_recovers_after(self, boundary: str) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-recovery-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            interrupted_dependencies = self._crashing_dependencies(
                dependencies,
                boundary=boundary,
                source=source,
                target=target,
                config=config,
            )

            with self.assertRaisesRegex(SimulatedCutoverCrash, boundary):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=interrupted_dependencies,
                )

            recovered = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )
            self.assertEqual(recovered["status"], "upgraded_and_activated")
            self.assertIs(recovered["cutover_recovered"], True)
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["store_path"], str(target)
            )
            self.assertEqual(
                json.loads(source.read_text(encoding="utf-8"))["schema_version"],
                "v4_redirect",
            )
            manifest = Path(str(recovered["migration_manifest"]))
            target_key = target.with_name(f"{target.name}.plan.key")
            self.assertIsNotNone(request.expected_source_sha256)
            self.assertIsNotNone(request.expected_plan_fingerprint)
            recovery_source = migrations.migration_recovery_source(
                manifest,
                config_path=config,
                target_path=target,
                expected_source_sha256=str(request.expected_source_sha256),
                expected_plan_fingerprint=str(request.expected_plan_fingerprint),
                transform_version=dependencies.transform_version,
                read_verified_published_bytes=dependencies.read_verified_published_bytes,
                finalize_publication_link=dependencies.finalize_publication_link,
            )
            self.assertEqual(recovery_source, source)
            paths = (source, target, target_key, config, manifest)
            exact_state = tuple(path.read_bytes() for path in paths)

            replayed = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )
            self.assertEqual(replayed["status"], "upgraded_and_activated")
            self.assertIs(replayed["cutover_recovered"], True)
            self.assertEqual(exact_state, tuple(path.read_bytes() for path in paths))

    def test_crash_after_database_publication_rolls_forward(self) -> None:
        self._assert_recovers_after("database")

    def test_crash_after_key_publication_rolls_forward(self) -> None:
        self._assert_recovers_after("key")

    def test_crash_after_config_publication_rolls_forward(self) -> None:
        self._assert_recovers_after("config")

    def test_crash_after_tombstone_publication_replays_as_noop(self) -> None:
        self._assert_recovers_after("tombstone")

    def test_changed_published_database_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-ambiguity-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            interrupted_dependencies = self._crashing_dependencies(
                dependencies,
                boundary="database",
                source=source,
                target=target,
                config=config,
            )
            with self.assertRaises(SimulatedCutoverCrash):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=interrupted_dependencies,
                )
            target.write_bytes(b"ambiguous replacement")

            with self.assertRaisesRegex(ValueError, "differs from cutover intent"):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=dependencies,
                )
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["store_path"], str(source)
            )
            self.assertEqual(json.loads(source.read_text(encoding="utf-8"))["schema_version"], "v3")

    def test_recovery_source_rejects_an_older_transform_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-transform-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            self.assertEqual(dependencies.transform_version, "v3-to-v4.sqlite-wal.3")
            completed = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )
            manifest = Path(str(completed["migration_manifest"]))

            with self.assertRaisesRegex(ValueError, "arguments differ"):
                migrations.migration_recovery_source(
                    manifest,
                    config_path=config,
                    target_path=target,
                    expected_source_sha256=str(request.expected_source_sha256),
                    expected_plan_fingerprint=str(request.expected_plan_fingerprint),
                    transform_version="v3-to-v4.sqlite-wal.2",
                    read_verified_published_bytes=dependencies.read_verified_published_bytes,
                    finalize_publication_link=dependencies.finalize_publication_link,
                )

    def test_publication_competitor_starts_only_at_matching_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-race-start-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            staged = root / "staged"
            destination = root / "destination"
            dependencies.atomic_bytes(staged, b"reviewed")
            raced_dependencies, competitor = self._racing_dependencies(
                dependencies,
                matches=lambda path: path == destination,
                competing_bytes=b"competitor",
            )

            try:
                self.assertFalse(competitor.is_alive())
            finally:
                with self.assertRaises(FileExistsError):
                    raced_dependencies.publish_staged_file(
                        staged,
                        destination,
                        overwrite=False,
                    )
                competitor.join(timeout=10.0)

            self.assertFalse(competitor.is_alive())
            self.assertEqual(destination.read_bytes(), b"competitor")

    def test_publication_competitor_failure_propagates_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-race-failure-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            staged = root / "staged"
            destination = root / "destination"
            dependencies.atomic_bytes(staged, b"reviewed")
            competitor_finished = threading.Event()

            def fail_competing_write(path: Path, payload: bytes) -> None:
                del path, payload
                try:
                    raise SimulatedCutoverCrash("competitor")
                finally:
                    competitor_finished.set()

            raced_dependencies, competitor = self._racing_dependencies(
                replace(dependencies, atomic_bytes=fail_competing_write),
                matches=lambda path: path == destination,
                competing_bytes=b"competitor",
            )
            self.addCleanup(competitor.join, timeout=10.0)

            with (
                mock.patch.object(competitor, "join", wraps=competitor.join) as join_competitor,
                self.assertRaisesRegex(SimulatedCutoverCrash, "competitor"),
            ):
                raced_dependencies.publish_staged_file(
                    staged,
                    destination,
                    overwrite=False,
                )

            join_competitor.assert_called_once()
            self.assertTrue(competitor_finished.is_set())
            self.assertFalse(competitor.is_alive())
            self.assertFalse(destination.exists())

    def test_database_and_key_publication_races_never_clobber(self) -> None:
        for boundary in ("database", "key"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory(prefix=f"tedd-migration-{boundary}-race-") as temporary,
            ):
                root = Path(temporary).resolve()
                source, target, config, dependencies, request = self._fixture(root)
                target_key = target.with_name(f"{target.name}.plan.key")
                raced_path = target if boundary == "database" else target_key
                competing_bytes = f"competing-{boundary}".encode()

                def matches_raced_path(path: Path, *, expected: Path = raced_path) -> bool:
                    return path == expected

                raced_dependencies, competitor = self._racing_dependencies(
                    dependencies,
                    matches=matches_raced_path,
                    competing_bytes=competing_bytes,
                )

                with self.assertRaises(FileExistsError):
                    migrations.upgrade_v3_to_v4(
                        request,
                        config_path=config,
                        lock_timeout=3.0,
                        dependencies=raced_dependencies,
                    )
                competitor.join(timeout=10.0)
                self.assertFalse(competitor.is_alive())
                self.assertEqual(raced_path.read_bytes(), competing_bytes)
                self.assertEqual(
                    json.loads(config.read_text(encoding="utf-8"))["store_path"],
                    str(source),
                )
                self.assertEqual(
                    json.loads(source.read_text(encoding="utf-8"))["schema_version"],
                    "v3",
                )

    def test_backup_and_rollback_publication_races_never_clobber(self) -> None:
        cases = (
            ("backup", lambda path: path.name.startswith("graph-v3-wm")),
            ("rollback", lambda path: path.name.endswith(".rollback-v3.json")),
        )
        for label, matches in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory(prefix=f"tedd-migration-{label}-race-") as temporary,
            ):
                root = Path(temporary).resolve()
                source, target, config, dependencies, request = self._fixture(root)
                competing_bytes = f"competing-{label}".encode()
                raced_dependencies, competitor = self._racing_dependencies(
                    dependencies,
                    matches=matches,
                    competing_bytes=competing_bytes,
                )

                with self.assertRaisesRegex(ValueError, f"{label}.*differs"):
                    migrations.upgrade_v3_to_v4(
                        request,
                        config_path=config,
                        lock_timeout=3.0,
                        dependencies=raced_dependencies,
                    )
                competitor.join(timeout=10.0)
                self.assertFalse(competitor.is_alive())
                evidence = [path for path in (root / "backups").iterdir() if matches(path)]
                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0].read_bytes(), competing_bytes)
                self.assertFalse(target.exists())

    def test_pre_manifest_orphans_are_inert_and_retry_uses_new_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-pre-manifest-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            payload = source.read_bytes()
            graph, plan, fingerprint = migrations.upgrade_plan(
                source,
                target,
                payload,
                dependencies,
            )
            envelope = request.envelope
            self.assertIsNotNone(envelope)
            if envelope is None:
                raise AssertionError("fixture must include migration provenance")
            orphan_dependencies = replace(dependencies, uuid_hex=lambda: "a" * 32)
            orphan_paths = migrations._migration_paths(
                request,
                source,
                target,
                plan,
                fingerprint,
                orphan_dependencies,
            )
            migrations._ensure_source_backup(orphan_paths.backup, payload, orphan_dependencies)
            migrations._stage_and_verify(
                graph,
                plan,
                envelope,
                orphan_paths,
                orphan_dependencies,
            )
            migrations._inspect_staged_target(orphan_paths, orphan_dependencies)
            orphan_evidence = (
                orphan_paths.staging,
                orphan_paths.staging_key,
                orphan_paths.verification_backup,
                orphan_paths.verification_manifest,
                orphan_paths.rollback_export,
            )
            orphan_bytes = tuple(path.read_bytes() for path in orphan_evidence)

            completed = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )

            self.assertEqual(completed["status"], "upgraded_and_activated")
            self.assertNotEqual(
                Path(str(completed["rollback_export"])), orphan_paths.rollback_export
            )
            self.assertEqual(orphan_bytes, tuple(path.read_bytes() for path in orphan_evidence))

    def test_exact_byte_pre_link_crash_reuses_deterministic_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-pre-link-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            destination = root / "backup[1].json"
            payload = b"reviewed-backup"
            staged = migrations._exact_publication_stage(
                destination,
                dependencies.sha256_bytes(payload),
            )
            dependencies.atomic_bytes(staged, payload)

            migrations._publish_exact_bytes(
                destination,
                payload,
                label="migration source backup",
                dependencies=dependencies,
            )

            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(staged.exists())

    def test_exact_byte_partial_link_ignores_unrelated_orphan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-partial-link-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            destination = root / "manifest.json"
            payload = b"reviewed-manifest"
            payload_digest = dependencies.sha256_bytes(payload)
            staged = migrations._exact_publication_stage(destination, payload_digest)
            orphan = migrations._exact_publication_stage(destination, "f" * 64)
            dependencies.atomic_bytes(staged, payload)
            dependencies.atomic_bytes(orphan, b"old-orphan")
            os.link(staged, destination)

            migrations._publish_exact_bytes(
                destination,
                payload,
                label="migration cutover manifest",
                dependencies=dependencies,
            )

            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(staged.exists())
            self.assertEqual(orphan.read_bytes(), b"old-orphan")

    def test_link_before_unlink_crashes_recover_for_all_publications(self) -> None:
        cases = (
            ("backup", lambda path: path.name.startswith("graph-v3-wm")),
            ("manifest", lambda path: path.name.startswith("graph-v4-migration-")),
            ("database", lambda path: path.name == "graph.sqlite3"),
            ("key", lambda path: path.name == "graph.sqlite3.plan.key"),
        )
        for label, matches in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory(
                    prefix=f"tedd-migration-link-crash-{label}-"
                ) as temporary,
            ):
                root = Path(temporary).resolve()
                source, target, config, dependencies, request = self._fixture(root)
                interrupted = self._link_then_crash_dependencies(
                    dependencies,
                    matches=matches,
                )
                with self.assertRaisesRegex(SimulatedCutoverCrash, ""):
                    migrations.upgrade_v3_to_v4(
                        request,
                        config_path=config,
                        lock_timeout=3.0,
                        dependencies=interrupted,
                    )

                completed = migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=dependencies,
                )

                self.assertEqual(completed["status"], "upgraded_and_activated")
                self.assertEqual(target.stat().st_nlink, 1)
                self.assertEqual(
                    target.with_name(f"{target.name}.plan.key").stat().st_nlink,
                    1,
                )
                self.assertEqual(
                    Path(str(completed["migration_manifest"])).stat().st_nlink,
                    1,
                )
                self.assertEqual(Path(str(completed["source_backup"])).stat().st_nlink, 1)

    def test_service_rejects_primary_and_backup_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-alias-") as temporary:
            root = Path(temporary).resolve()
            source, target, _config, dependencies, request = self._fixture(root)
            source_before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "v3 source and configuration"):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=source,
                    lock_timeout=3.0,
                    dependencies=dependencies,
                )

            for backup in (source, target.with_name(f"{target.name}.plan.key")):
                with (
                    self.subTest(backup=backup),
                    self.assertRaisesRegex(
                        ValueError,
                        "paths must be distinct",
                    ),
                ):
                    migrations.upgrade_v3_to_v4(
                        replace(request, backup_output=backup),
                        config_path=(root / "config.json").resolve(),
                        lock_timeout=3.0,
                        dependencies=dependencies,
                    )
            self.assertEqual(source.read_bytes(), source_before)

    def test_published_database_verification_streams_without_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-streaming-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            staged = root / "staged.sqlite3"
            destination = root / "published.sqlite3"
            payload = b"database-bytes"
            dependencies.atomic_bytes(staged, payload)
            bounded_read = mock.Mock(side_effect=AssertionError("bounded read used"))

            migrations._publish_staged_component(
                staged,
                destination,
                dependencies.sha256_bytes(payload),
                label="database",
                dependencies=replace(
                    dependencies,
                    read_verified_published_bytes=bounded_read,
                ),
            )

            self.assertEqual(destination.read_bytes(), payload)
            bounded_read.assert_not_called()

    def test_manifest_replacement_between_lookup_and_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-manifest-swap-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            completed = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )
            manifest = Path(str(completed["migration_manifest"]))
            original_read = dependencies.read_verified_published_bytes

            def replace_before_open(
                path: Path,
                expected_identity: tuple[int, int],
                *,
                maximum_bytes: int,
            ) -> bytes:
                replacement = path.with_name(f".{path.name}.replacement")
                dependencies.atomic_bytes(replacement, b"{}\n")
                os.replace(replacement, path)
                return original_read(
                    path,
                    expected_identity,
                    maximum_bytes=maximum_bytes,
                )

            with self.assertRaises(v4.ConcurrencyConflict):
                migrations.migration_recovery_source(
                    manifest,
                    config_path=config,
                    target_path=target,
                    expected_source_sha256=str(request.expected_source_sha256),
                    expected_plan_fingerprint=str(request.expected_plan_fingerprint),
                    transform_version=dependencies.transform_version,
                    read_verified_published_bytes=replace_before_open,
                    finalize_publication_link=dependencies.finalize_publication_link,
                )

    def test_recovery_rejects_replaced_staged_database_symlink_before_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-staged-symlink-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)

            def stop_before_database(
                staged: Path,
                destination: Path,
                *,
                overwrite: bool,
            ) -> tuple[int, int]:
                if destination == target:
                    raise SimulatedCutoverCrash("before-database-publication")
                return dependencies.publish_staged_file(
                    staged,
                    destination,
                    overwrite=overwrite,
                )

            with self.assertRaisesRegex(SimulatedCutoverCrash, "before-database"):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=replace(
                        dependencies,
                        publish_staged_file=cast(
                            migrations.PublishStagedFile,
                            stop_before_database,
                        ),
                    ),
                )
            self.assertFalse(target.exists())
            manifests = list((root / "backups").glob("graph-v4-migration-*.manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            staged_database = Path(str(manifest["staging_path"]))
            replacement = root / "replacement.sqlite3"
            replacement.write_bytes(staged_database.read_bytes())
            set_private_path_mode(replacement, 0o600)
            staged_database.unlink()
            try:
                os.symlink(replacement, staged_database)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")

            with self.assertRaises((OSError, v4.ConcurrencyConflict, v4.PathSafetyError)):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=dependencies,
                )
            self.assertFalse(target.exists())

    def test_manifest_parser_rejects_noncanonical_and_duplicate_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-manifest-json-") as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            dependencies = integration._migration_dependencies()
            manifest = root / "manifest.json"
            payloads = (
                b'{"format":"knowledge-graph-v4-migration-manifest",'
                b'"cutover_protocol":"verified-roll-forward-v1"}\n',
                b'{"format":"knowledge-graph-v4-migration-manifest",'
                b'"format":"knowledge-graph-v4-migration-manifest",'
                b'"cutover_protocol":"verified-roll-forward-v1"}\n',
            )
            messages = ("not canonical", "duplicate key")
            for payload, message in zip(payloads, messages, strict=True):
                with self.subTest(message=message):
                    if manifest.exists():
                        manifest.unlink()
                    dependencies.atomic_bytes(manifest, payload)
                    with self.assertRaisesRegex(ValueError, message):
                        migrations._read_cutover_manifest(
                            manifest,
                            read_verified_published_bytes=(
                                dependencies.read_verified_published_bytes
                            ),
                            finalize_publication_link=(dependencies.finalize_publication_link),
                        )

    def test_recovery_rejects_changed_run_and_discovery_provenance(self) -> None:
        for field, value in (
            ("producer_run_id", "different-run"),
            ("discovery_kind", "different-kind"),
        ):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory(
                    prefix=f"tedd-migration-provenance-{field}-"
                ) as temporary,
            ):
                root = Path(temporary).resolve()
                source, target, config, dependencies, request = self._fixture(root)
                interrupted = self._crashing_dependencies(
                    dependencies,
                    boundary="database",
                    source=source,
                    target=target,
                    config=config,
                )
                with self.assertRaises(SimulatedCutoverCrash):
                    migrations.upgrade_v3_to_v4(
                        request,
                        config_path=config,
                        lock_timeout=3.0,
                        dependencies=interrupted,
                    )
                envelope = request.envelope
                self.assertIsNotNone(envelope)
                if envelope is None:
                    raise AssertionError("fixture must include migration provenance")
                if field == "producer_run_id":
                    changed_envelope = replace(envelope, producer_run_id=value)
                else:
                    changed_envelope = replace(envelope, discovery_kind=value)
                with self.assertRaisesRegex(ValueError, "provenance differs"):
                    migrations.upgrade_v3_to_v4(
                        replace(request, envelope=changed_envelope),
                        config_path=config,
                        lock_timeout=3.0,
                        dependencies=dependencies,
                    )

    def test_recovery_rejects_hardlinked_manifest_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-hardlink-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            completed = migrations.upgrade_v3_to_v4(
                request,
                config_path=config,
                lock_timeout=3.0,
                dependencies=dependencies,
            )
            manifest = Path(str(completed["migration_manifest"]))
            extra_link = manifest.with_name(f"{manifest.name}.extra-link")
            os.link(manifest, extra_link)
            try:
                with self.assertRaises(v4.PathSafetyError):
                    migrations.migration_recovery_source(
                        manifest,
                        config_path=config,
                        target_path=target,
                        expected_source_sha256=str(request.expected_source_sha256),
                        expected_plan_fingerprint=str(request.expected_plan_fingerprint),
                        transform_version=dependencies.transform_version,
                        read_verified_published_bytes=(dependencies.read_verified_published_bytes),
                        finalize_publication_link=dependencies.finalize_publication_link,
                    )
            finally:
                extra_link.unlink()

    def test_full_recovery_rejects_incompatible_transform(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-old-transform-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            interrupted = self._crashing_dependencies(
                dependencies,
                boundary="database",
                source=source,
                target=target,
                config=config,
            )
            with self.assertRaises(SimulatedCutoverCrash):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=interrupted,
                )
            old_dependencies = replace(
                dependencies,
                transform_version="v3-to-v4.sqlite-wal.2",
            )
            with self.assertRaisesRegex(ValueError, "different source paths or code"):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=old_dependencies,
                )

    def test_manifest_publication_does_not_clobber_a_competing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-migration-manifest-") as temporary:
            root = Path(temporary).resolve()
            source, target, config, dependencies, request = self._fixture(root)
            competing_bytes = b'{"format":"unrelated"}\n'
            original_link = os.link

            def competing_link(staged: Path, destination: Path) -> None:
                if destination.name.startswith("graph-v4-migration-"):
                    destination.write_bytes(competing_bytes)
                    set_private_path_mode(destination, 0o600)
                    raise FileExistsError(destination)
                original_link(staged, destination)

            with (
                mock.patch.object(os, "link", side_effect=competing_link),
                self.assertRaisesRegex(ValueError, "cutover manifest differs"),
            ):
                migrations.upgrade_v3_to_v4(
                    request,
                    config_path=config,
                    lock_timeout=3.0,
                    dependencies=dependencies,
                )

            manifests = list((root / "backups").glob("graph-v4-migration-*.manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(manifests[0].read_bytes(), competing_bytes)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
