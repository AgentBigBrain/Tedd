#!/usr/bin/env python3
"""Characterization tests for the legacy CLI facade and private dispatcher."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NoReturn
from unittest import mock

import knowledge_graph as kg  # noqa: E402
from tedd_knowledge_graph import (  # noqa: E402
    legacy_commands_mutation,
    legacy_commands_projection,
    legacy_commands_read,
    legacy_configuration,
    legacy_dispatch,
    legacy_dispatch_contracts,
    source_contracts,
)

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

SCRIPT = SCRIPT_DIRECTORY / "knowledge_graph.py"


def v4_bindings(
    *,
    detected_store: str,
    should_handle: bool,
    result: legacy_dispatch_contracts.V4JsonObject | None = None,
) -> kg._V4Bindings:
    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("unexpected v4 operation")

    return kg._V4Bindings(
        detect_store=lambda path: detected_store,
        should_handle=lambda args, **kwargs: should_handle,
        handle_command=lambda args, **kwargs: result,
        initialize=unexpected,
        upgrade=unexpected,
        migration_recovery_source=unexpected,
        upgrade_transform_version="v3-to-v4.sqlite-wal.test",
        capture=unexpected,
        sanitize_review_safe=unexpected,
        project=unexpected,
        redact=unexpected,
        resolve_loop=unexpected,
        open_store=unexpected,
    )


class LegacyDispatchContractTests(unittest.TestCase):
    def configured_fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
        kg.set_private_path_mode(root, 0o700)
        vault = root / "vault"
        vault.mkdir(mode=0o700)
        kg.set_private_path_mode(vault, 0o700)
        config_path = root / "config.json"
        store_path = root / "graph.sqlite3"
        configured = kg.configure(
            argparse.Namespace(vault=str(vault), root_name=None, store=str(store_path)),
            config_path,
        )
        return config_path, vault, store_path, dict(configured)

    @staticmethod
    def preview_policy(
        config: dict[str, object], *, enabled: bool = True, exposure: str = "local_only"
    ) -> dict[str, object]:
        return {
            "format": 1,
            "scope": {
                "store_id": "store_test",
                "generation": "1",
                "subject_ids": ["entity_test"],
                "namespace": "Synthetic test scope",
            },
            "vault_path": config["vault_path"],
            "root_name": config["root_name"],
            "projection_root_id": config["projection_root_id"],
            "enabled": enabled,
            "audience": "owner",
            "sensitivities": ["ordinary"],
            "exposure": exposure,
            "exposure_approved": True,
        }

    @classmethod
    def owner_access_policy(cls, config: dict[str, object]) -> dict[str, object]:
        return {
            "format": 1,
            "scope": cls.preview_policy(config)["scope"],
            "audience": "owner",
            "sensitivities": ["ordinary"],
            "local_retention": True,
            "retained_bindings": ["a" * 64],
        }

    @staticmethod
    def write_config(path: Path, config: dict[str, object]) -> None:
        path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), *arguments]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            return_code = kg.main()
        return return_code, stdout.getvalue(), stderr.getvalue()

    def run_script(
        self,
        *arguments: str,
        working_directory: Path = SCRIPT_DIRECTORY,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=working_directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_semantic_timeout_error_remains_operational_not_argument_error(self) -> None:
        completed = self.run_script("--lock-timeout", "-1", "doctor")

        self.assertEqual(completed.returncode, 1, completed)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            json.loads(completed.stderr),
            {
                "status": "error",
                "error": "--lock-timeout must be between 0 and 300 seconds",
            },
        )
        self.assertNotIn("error_code", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_direct_script_help_keeps_argparse_exit_and_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = self.run_script(
                "--help",
                working_directory=Path(temporary),
            )

        self.assertEqual(completed.returncode, 0, completed)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(completed.stdout.startswith("usage:"), completed.stdout)
        self.assertIn("configure", completed.stdout)
        self.assertIn("upgrade-v4", completed.stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(completed.stdout)

    def test_configure_uses_call_time_facade_dependencies_and_bypasses_store_load(self) -> None:
        configured = mock.Mock(
            return_value={
                "vault_path": "Café Vault",
                "projection_manifest_key": "must-not-leak",
            }
        )
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(kg, "configure", configured),
            mock.patch.object(
                kg,
                "load_config",
                side_effect=AssertionError("configure must bypass config loading"),
            ),
            mock.patch.object(
                kg,
                "_load_cli_v4_bindings",
                side_effect=AssertionError("configure must bypass store detection"),
            ),
        ):
            return_code, stdout, stderr = self.run_main(
                "--config",
                "unused.json",
                "configure",
                "--vault",
                "unused-vault",
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "configured")
        self.assertEqual(payload["config"]["vault_path"], "Café Vault")
        self.assertNotIn("projection_manifest_key", payload["config"])
        self.assertIn("Café Vault", stdout)
        configured.assert_called_once()

    def test_unchanged_configuration_preserves_disabled_policies_and_unrelated_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            dir=str(Path.home()) if os.name == "nt" else None
        ) as temporary:
            config_path, vault, store_path, config = self.configured_fixture(Path(temporary))
            preview = self.preview_policy(config, enabled=False)
            owner_access = self.owner_access_policy(config)
            config.update(
                {
                    "source_preview_policy": preview,
                    "source_owner_access_policy": owner_access,
                    "unrelated_sentinel": {"keep": [1, "two"]},
                }
            )
            self.write_config(config_path, config)

            outcome = kg.configure(
                argparse.Namespace(
                    vault=str(vault), root_name=config["root_name"], store=str(store_path)
                ),
                config_path,
            )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(outcome["source_preview_policy"], preview)
        self.assertEqual(outcome["source_owner_access_policy"], owner_access)
        self.assertEqual(outcome["unrelated_sentinel"], {"keep": [1, "two"]})
        self.assertEqual(
            getattr(outcome, "owner_preview", None),
            legacy_configuration.OwnerPreviewTransition("unchanged", False),
        )
        self.assertNotIn("owner_preview", persisted)
        self.assertNotIn("owner_preview", json.loads(json.dumps(outcome)))

    def test_absent_preview_policy_keeps_ordinary_configuration_response_shape(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=str(Path.home()) if os.name == "nt" else None
        ) as temporary:
            root = Path(temporary)
            config_path, vault, store_path, config = self.configured_fixture(root)
            config["unrelated_sentinel"] = "keep"
            self.write_config(config_path, config)

            return_code, stdout, stderr = self.run_main(
                "--config",
                str(config_path),
                "configure",
                "--vault",
                str(vault),
                "--root-name",
                "Changed Root",
                "--store",
                str(store_path),
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(set(payload), {"status", "config"})
        self.assertEqual(payload["status"], "configured")
        self.assertEqual(payload["config"]["unrelated_sentinel"], "keep")

    def test_invalid_existing_preview_policy_fails_before_configuration_write(self) -> None:
        cases = {
            "malformed": lambda policy: policy.pop("format"),
            "wrong-old-target": lambda policy: policy.update({"vault_path": "C:/wrong-target"}),
            "unknown-exposure": lambda policy: policy.update({"exposure": "unknown"}),
        }
        for label, mutate in cases.items():
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory(
                    dir=str(Path.home()) if os.name == "nt" else None
                ) as temporary,
            ):
                config_path, vault, store_path, config = self.configured_fixture(Path(temporary))
                policy = self.preview_policy(config, enabled=False)
                mutate(policy)
                config["source_preview_policy"] = policy
                self.write_config(config_path, config)
                before = config_path.read_bytes()

                with self.assertRaises(source_contracts.SourceArchiveError):
                    kg.configure(
                        argparse.Namespace(
                            vault=str(vault), root_name="Changed Root", store=str(store_path)
                        ),
                        config_path,
                    )

                self.assertEqual(config_path.read_bytes(), before)

    def test_target_changes_invalidate_only_preview_approval_and_preserve_old_bytes(self) -> None:
        for changed_field in ("vault", "root"):
            with (
                self.subTest(changed_field=changed_field),
                tempfile.TemporaryDirectory(
                    dir=str(Path.home()) if os.name == "nt" else None
                ) as temporary,
            ):
                root = Path(temporary)
                config_path, vault, store_path, config = self.configured_fixture(root)
                preview = self.preview_policy(config)
                owner_access = self.owner_access_policy(config)
                config.update(
                    {
                        "source_preview_policy": preview,
                        "source_owner_access_policy": owner_access,
                        "unrelated_sentinel": "keep",
                    }
                )
                self.write_config(config_path, config)
                old_root = vault / str(config["root_name"])
                evidence = old_root / "20 Evidence"
                evidence.mkdir(parents=True)
                sentinel = evidence / "owner-edited.bin"
                sentinel.write_bytes(b"do-not-touch")
                next_vault = vault
                next_root = str(config["root_name"])
                if changed_field == "vault":
                    next_vault = root / "new-vault"
                    next_vault.mkdir(mode=0o700)
                    kg.set_private_path_mode(next_vault, 0o700)
                else:
                    next_root = "Changed Root"

                outcome = kg.configure(
                    argparse.Namespace(
                        vault=str(next_vault), root_name=next_root, store=str(store_path)
                    ),
                    config_path,
                )

                self.assertNotIn("source_preview_policy", outcome)
                self.assertEqual(outcome["source_owner_access_policy"], owner_access)
                self.assertEqual(outcome["unrelated_sentinel"], "keep")
                self.assertNotEqual(
                    outcome["projection_manifest_key"], config["projection_manifest_key"]
                )
                self.assertNotEqual(outcome["projection_root_id"], config["projection_root_id"])
                self.assertEqual(
                    getattr(outcome, "owner_preview", None),
                    legacy_configuration.OwnerPreviewTransition(
                        "invalidated",
                        True,
                        old_vault_path=str(vault),
                        old_root_name=str(config["root_name"]),
                    ),
                )
                self.assertEqual(sentinel.read_bytes(), b"do-not-touch")
                self.assertNotIn("owner_preview", json.loads(config_path.read_text("utf-8")))

    def test_store_change_invalidates_both_source_policies_without_opening_store(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=str(Path.home()) if os.name == "nt" else None
        ) as temporary:
            root = Path(temporary)
            config_path, vault, store_path, config = self.configured_fixture(root)
            config.update(
                {
                    "source_preview_policy": self.preview_policy(config),
                    "source_owner_access_policy": self.owner_access_policy(config),
                    "source_owner_view_access_policy": self.owner_access_policy(config),
                }
            )
            self.write_config(config_path, config)
            new_store = root / "new-graph.sqlite3"
            old_manifest_key = config["projection_manifest_key"]
            old_root_id = config["projection_root_id"]

            with mock.patch.object(
                kg,
                "load_graph_with_digest",
                side_effect=AssertionError("configure must not open a graph store"),
            ):
                outcome = kg.configure(
                    argparse.Namespace(
                        vault=str(vault), root_name=config["root_name"], store=str(new_store)
                    ),
                    config_path,
                )

        self.assertNotIn("source_preview_policy", outcome)
        self.assertNotIn("source_owner_access_policy", outcome)
        self.assertNotIn("source_owner_view_access_policy", outcome)
        self.assertEqual(outcome["projection_manifest_key"], old_manifest_key)
        self.assertEqual(outcome["projection_root_id"], old_root_id)
        self.assertEqual(
            getattr(outcome, "owner_preview", None),
            legacy_configuration.OwnerPreviewTransition(
                "invalidated",
                True,
                old_vault_path=str(vault),
                old_root_name=str(config["root_name"]),
            ),
        )

    def test_cli_reports_only_preview_transition_and_redacts_owner_policy(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=str(Path.home()) if os.name == "nt" else None
        ) as temporary:
            root = Path(temporary)
            config_path, vault, store_path, config = self.configured_fixture(root)
            config.update(
                {
                    "source_preview_policy": self.preview_policy(config),
                    "source_owner_access_policy": self.owner_access_policy(config),
                    "source_owner_view_access_policy": self.owner_access_policy(config),
                    "source_export_recovery_key": "a" * 64,
                }
            )
            self.write_config(config_path, config)
            new_vault = root / "new-vault"
            new_vault.mkdir(mode=0o700)
            kg.set_private_path_mode(new_vault, 0o700)

            return_code, stdout, stderr = self.run_main(
                "--config",
                str(config_path),
                "configure",
                "--vault",
                str(new_vault),
                "--store",
                str(store_path),
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "configured")
        self.assertIn("config", payload)
        self.assertNotIn("projection_manifest_key", payload["config"])
        self.assertNotIn("source_owner_access_policy", payload["config"])
        self.assertNotIn("source_owner_view_access_policy", payload["config"])
        self.assertNotIn("source_export_recovery_key", payload["config"])
        self.assertNotIn("a" * 64, stdout)
        self.assertEqual(
            payload["owner_preview"],
            {
                "policy": "invalidated",
                "old_target_copies_may_remain": True,
                "old_vault_path": str(vault),
                "old_root_name": config["root_name"],
            },
        )
        self.assertNotIn("a" * 64, stdout)

    def test_privacy_failure_precedes_v4_import_and_store_detection(self) -> None:
        loader = mock.Mock(side_effect=AssertionError("privacy failure must precede v4 loading"))
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(kg, "load_config", return_value={}),
            mock.patch.object(kg, "resolve_store", return_value=Path("graph.json")),
            mock.patch.object(
                kg,
                "state_privacy_warnings",
                return_value=["configured state is inside the vault"],
            ),
            mock.patch.object(kg, "_load_cli_v4_bindings", loader),
        ):
            return_code, stdout, stderr = self.run_main("validate")

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("must remain outside", json.loads(stderr)["error"])
        loader.assert_not_called()

    def test_sqlite_compatible_command_routes_to_v4_before_legacy_handler(self) -> None:
        bindings = v4_bindings(
            detected_store="sqlite_v4",
            should_handle=True,
            result={"valid": False, "marker": "native-v4"},
        )
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(kg, "load_config", return_value={}),
            mock.patch.object(
                kg,
                "resolve_store",
                return_value=Path("graph.sqlite3"),
            ),
            mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
            mock.patch.object(kg, "_load_cli_v4_bindings", return_value=bindings),
            mock.patch.object(
                kg,
                "load_graph_with_digest",
                side_effect=AssertionError("v4 validate must bypass the v3 loader"),
            ),
        ):
            return_code, stdout, stderr = self.run_main("validate")

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"valid": False, "marker": "native-v4"},
        )

    def test_legacy_migrate_refuses_v4_store_before_json_loading(self) -> None:
        bindings = v4_bindings(
            detected_store="sqlite_v4",
            should_handle=False,
        )
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(kg, "load_config", return_value={}),
            mock.patch.object(
                kg,
                "resolve_store",
                return_value=Path("graph.sqlite3"),
            ),
            mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
            mock.patch.object(kg, "_load_cli_v4_bindings", return_value=bindings),
            mock.patch.object(
                legacy_dispatch,
                "_migrate_response",
                side_effect=AssertionError("v4 store must not reach legacy JSON migration"),
            ),
        ):
            return_code, stdout, stderr = self.run_main("migrate", "--dry-run")

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr),
            {
                "status": "error",
                "error": (
                    "migrate supports legacy JSON stores only; the configured canonical "
                    "store is an activated v4 SQLite store"
                ),
            },
        )

    def test_v4_upgrade_apply_uses_only_manifest_validated_recovery_source(self) -> None:
        source_digest = "a" * 64
        plan_fingerprint = "b" * 64
        recovery = mock.Mock(return_value=Path("legacy-source.json"))
        handle = mock.Mock(return_value={"status": "recovered"})
        bindings = dataclasses.replace(
            v4_bindings(detected_store="sqlite_v4", should_handle=False),
            handle_command=handle,
            migration_recovery_source=recovery,
        )

        def secure_path(value: object, label: str) -> Path:
            if label == "Configuration":
                return Path("config.json")
            if label == "v4 migration manifest":
                self.assertEqual(value, "reviewed-manifest.json")
                return Path("reviewed-manifest.json")
            raise AssertionError(label)

        with (
            mock.patch.object(kg, "secure_state_path", side_effect=secure_path),
            mock.patch.object(
                kg,
                "load_config",
                return_value={"v4_migration_manifest": "reviewed-manifest.json"},
            ),
            mock.patch.object(
                kg,
                "resolve_store",
                return_value=Path("graph.sqlite3"),
            ),
            mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
            mock.patch.object(kg, "_load_cli_v4_bindings", return_value=bindings),
        ):
            return_code, stdout, stderr = self.run_main(
                "upgrade-v4",
                "--apply",
                "--expect-source-sha256",
                source_digest,
                "--expect-plan-fingerprint",
                plan_fingerprint,
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"status": "recovered"})
        recovery.assert_called_once_with(
            Path("reviewed-manifest.json"),
            config_path=Path("config.json"),
            target_path=Path("graph.sqlite3"),
            expected_source_sha256=source_digest,
            expected_plan_fingerprint=plan_fingerprint,
            transform_version=bindings.upgrade_transform_version,
        )
        self.assertEqual(handle.call_args.kwargs["db_path"], Path("graph.sqlite3"))
        self.assertEqual(
            handle.call_args.kwargs["v3_store_path"],
            Path("legacy-source.json"),
        )

    def test_v4_upgrade_dry_run_never_enters_recovery(self) -> None:
        recovery = mock.Mock(return_value=Path("legacy-source.json"))
        handle = mock.Mock(return_value={"status": "unexpected"})
        bindings = dataclasses.replace(
            v4_bindings(detected_store="sqlite_v4", should_handle=False),
            handle_command=handle,
            migration_recovery_source=recovery,
        )
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(
                kg,
                "load_config",
                return_value={"v4_migration_manifest": "reviewed-manifest.json"},
            ),
            mock.patch.object(
                kg,
                "resolve_store",
                return_value=Path("graph.sqlite3"),
            ),
            mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
            mock.patch.object(kg, "_load_cli_v4_bindings", return_value=bindings),
        ):
            return_code, stdout, stderr = self.run_main("upgrade-v4", "--dry-run")

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["error"],
            "upgrade-v4 recovery on an activated v4 store requires --apply",
        )
        recovery.assert_not_called()
        handle.assert_not_called()

    def test_public_contracts_and_private_handler_aliases_remain_compatible(self) -> None:
        self.assertIs(
            legacy_dispatch.CommandResponse,
            legacy_dispatch_contracts.CommandResponse,
        )
        self.assertIs(
            legacy_dispatch.LegacyDispatchDependencies,
            legacy_dispatch_contracts.LegacyDispatchDependencies,
        )
        self.assertIs(legacy_dispatch.V4Bindings, legacy_dispatch_contracts.V4Bindings)
        self.assertIs(kg._LegacyDispatchDependencies, legacy_dispatch.LegacyDispatchDependencies)
        self.assertIs(kg._V4Bindings, legacy_dispatch.V4Bindings)
        self.assertIs(legacy_dispatch._validate_response, legacy_commands_read.validate_response)
        self.assertIs(
            legacy_dispatch._migrate_response,
            legacy_commands_projection.migrate_response,
        )
        self.assertIs(
            legacy_dispatch._capture_response,
            legacy_commands_mutation.capture_response,
        )

    def test_legacy_validate_uses_read_family_with_call_time_dependencies(self) -> None:
        graph = {"schema_version": "v3"}
        validation = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "counts": {},
        }
        load_graph = mock.Mock(return_value=(graph, "canonical-digest", "v3"))
        validate = mock.Mock(return_value=validation)
        bindings = v4_bindings(
            detected_store="json_v3",
            should_handle=False,
        )
        with (
            mock.patch.object(kg, "secure_state_path", return_value=Path("config.json")),
            mock.patch.object(kg, "load_config", return_value={}),
            mock.patch.object(kg, "resolve_store", return_value=Path("graph.json")),
            mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
            mock.patch.object(kg, "_load_cli_v4_bindings", return_value=bindings),
            mock.patch.object(kg, "load_graph_with_digest", load_graph),
            mock.patch.object(kg, "validate_graph", validate),
        ):
            return_code, stdout, stderr = self.run_main("validate")

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                **validation,
                "stored_schema_version": "v3",
                "migration_required": False,
            },
        )
        load_graph.assert_called_once_with(Path("graph.json"))
        validate.assert_called_once_with(graph)


if __name__ == "__main__":
    unittest.main(verbosity=2)
