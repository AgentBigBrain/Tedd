#!/usr/bin/env python3
"""Isolated tests for Tedd's private identity profile CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.support.paths import PROFILE_SCRIPTS as SCRIPT_DIR

SCRIPT = SCRIPT_DIR / "tedd_profile.py"
IS_WINDOWS = os.name == "nt"

import tedd_profile as profile_module  # noqa: E402


class TeddProfileCLITests(unittest.TestCase):
    def setUp(self) -> None:
        if IS_WINDOWS:
            # %TEMP% is intentionally shared with Codex sandbox identities and
            # is therefore an unsafe profile ancestor.  Build each fixture
            # directly under the user's LocalAppData tree and protect it.
            self.temporary = tempfile.TemporaryDirectory(
                prefix="tedd-profile-test-", dir=os.environ["LOCALAPPDATA"]
            )
        else:
            self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS exposes /var as a compatibility symlink.  Resolve the test
        # sandbox itself so ordinary fixtures have a symlink-free ancestor
        # chain; individual tests add deliberate symlink components.
        self.root = Path(self.temporary.name).resolve()
        if IS_WINDOWS:
            profile_module._set_private_path_mode(self.root, 0o700)
        self.profile = self.root / "private" / "profile.json"

    def run_cli(
        self,
        *arguments: str,
        expected_code: int = 0,
        profile: Path | None = None,
    ) -> dict[str, Any]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--profile",
            str(profile or self.profile),
            *arguments,
        ]
        result = subprocess.run(command, check=False, capture_output=True, timeout=10)
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(
            result.returncode,
            expected_code,
            msg=f"stdout={stdout!r}\nstderr={stderr!r}",
        )
        self.assertEqual(stderr, "")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as error:
            self.fail(f"CLI did not return JSON: {stdout!r}: {error}")

    def init_profile(
        self,
        *,
        preferred_name: str = "Operator One",
        display_name: str = "Operator One",
        email: str = "Person.One@Example.com",
        principal: str = "google-chat=users/123",
    ) -> dict[str, Any]:
        return self.run_cli(
            "init",
            "--preferred-name",
            preferred_name,
            "--display-name",
            display_name,
            "--email",
            email,
            "--provider-principal",
            principal,
        )

    def read_profile(self) -> dict[str, Any]:
        return json.loads(self.profile.read_text(encoding="utf-8"))

    def write_profile(self, value: dict[str, Any]) -> None:
        self.profile.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.profile, 0o600)

    def make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if IS_WINDOWS and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink creation privilege is unavailable")
            raise

    def set_broad_windows_acl(
        self, path: Path, *, inherit: bool = False, principal: str = "WD"
    ) -> None:
        flags = "OICI" if inherit else ""
        profile_module._set_windows_dacl_from_sddl(
            path,
            "D:P"
            f"(A;{flags};FA;;;{profile_module._current_user_sid()})"
            f"(A;{flags};FA;;;SY)"
            f"(A;{flags};FA;;;BA)"
            f"(A;{flags};FA;;;{principal})",
        )

    def set_windows_acl_without_write_owner(self, path: Path, *, inherit: bool) -> None:
        flags = "OICI" if inherit else ""
        current_user = profile_module._current_user_sid()
        access_without_write_owner = profile_module._FILE_ALL_ACCESS & ~profile_module._WRITE_OWNER
        profile_module._set_windows_dacl_from_sddl(
            path,
            f"D:P(A;{flags};0x{access_without_write_owner:08X};;;{current_user})",
        )
        snapshot = profile_module._windows_security_snapshot(path)
        self.assertEqual(snapshot.owner_sid, current_user)
        self.assertFalse(
            any(
                ace.sid == current_user
                and profile_module._expanded_file_mask(ace.mask) & profile_module._WRITE_OWNER
                for ace in snapshot.allowed_aces
            )
        )

    def test_first_status_is_unconfigured_and_side_effect_free(self) -> None:
        result = self.run_cli("status")
        self.assertTrue(result["ok"])
        self.assertFalse(result["configured"])
        self.assertEqual(result["agent_class_id"], "tedd")
        self.assertEqual(result["agent_display_name"], "Tedd")
        self.assertFalse(self.profile.parent.exists())

    @unittest.skipUnless(IS_WINDOWS, "Windows owner/DACL hardening behavior")
    def test_windows_private_path_hardening_sets_current_user_owner(self) -> None:
        # GitHub-hosted runners may create this child with Administrators as
        # owner, while ordinary workstations usually start with the user SID.
        directory = self.root / "raw-directory"
        directory.mkdir()

        profile_module._set_private_path_mode(directory, 0o700)

        snapshot = profile_module._windows_security_snapshot(directory)
        self.assertEqual(snapshot.owner_sid, profile_module._current_user_sid())
        self.assertIsNone(profile_module._windows_acl_problem(directory, is_directory=True))

    @unittest.skipUnless(IS_WINDOWS, "Windows owner/DACL hardening behavior")
    def test_windows_private_fd_hardening_sets_current_user_owner(self) -> None:
        path = self.root / "raw-file.json"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            profile_module._set_private_fd_mode(fd, 0o600)

            snapshot = profile_module._windows_descriptor_security_snapshot(fd)
            self.assertEqual(snapshot.owner_sid, profile_module._current_user_sid())
            self.assertIsNone(
                profile_module._windows_snapshot_acl_problem(
                    snapshot,
                    is_directory=False,
                    require_protected=True,
                )
            )
        finally:
            os.close(fd)

    @unittest.skipUnless(IS_WINDOWS, "Windows owner/DACL hardening behavior")
    def test_windows_private_path_hardening_does_not_require_write_owner(self) -> None:
        directory = self.root / "current-owner-directory"
        directory.mkdir()
        profile_module._set_private_path_mode(directory, 0o700)
        self.set_windows_acl_without_write_owner(directory, inherit=True)

        profile_module._set_private_path_mode(directory, 0o700)

        self.assertIsNone(profile_module._windows_acl_problem(directory, is_directory=True))

    @unittest.skipUnless(IS_WINDOWS, "Windows owner/DACL hardening behavior")
    def test_windows_private_fd_hardening_does_not_require_write_owner(self) -> None:
        path = self.root / "current-owner-file.json"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            profile_module._set_private_fd_mode(fd, 0o600)
            self.set_windows_acl_without_write_owner(path, inherit=False)

            profile_module._set_private_fd_mode(fd, 0o600)

            self.assertIsNone(
                profile_module._windows_snapshot_acl_problem(
                    profile_module._windows_descriptor_security_snapshot(fd),
                    is_directory=False,
                    require_protected=True,
                )
            )
        finally:
            os.close(fd)

    @unittest.skipUnless(IS_WINDOWS, "Windows owner/DACL helper behavior")
    def test_windows_generic_dacl_helper_preserves_owner_by_default(self) -> None:
        current_user = profile_module._current_user_sid()
        profile_module._set_windows_dacl_from_sddl(
            self.root,
            f"O:BAD:P(A;OICI;FA;;;{current_user})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
        )

        self.assertEqual(
            profile_module._windows_security_snapshot(self.root).owner_sid,
            current_user,
        )

    def test_init_creates_v1_profile_with_private_modes_and_random_ids(self) -> None:
        result = self.init_profile()
        value = self.read_profile()
        lock = self.profile.with_name("profile.json.lock")

        self.assertTrue(result["configured"])
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["agent_class_id"], "tedd")
        self.assertEqual(value["agent_display_name"], "Tedd")
        self.assertEqual(value["identity_scope"], "identification_and_contact_only")
        self.assertRegex(value["installation_id"], r"^installation_[0-9a-f]{32}$")
        self.assertRegex(value["profile_id"], r"^profile_[0-9a-f]{32}$")
        self.assertRegex(value["agent_instance_id"], r"^agent_instance_[0-9a-f]{32}$")
        self.assertRegex(value["operators"][0]["operator_id"], r"^operator_[0-9a-f]{32}$")
        self.assertEqual(value["operators"][0]["email_aliases"], ["Person.One@Example.com"])
        if not IS_WINDOWS:
            self.assertEqual(stat.S_IMODE(self.profile.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        else:
            self.assertTrue(self.profile.is_file())
            self.assertTrue(lock.is_file())
            self.assertEqual(lock.stat().st_size, 1)
            for path, is_directory in (
                (self.profile.parent, True),
                (self.profile, False),
                (lock, False),
            ):
                self.assertIsNone(
                    profile_module._windows_acl_problem(path, is_directory=is_directory)
                )

            # A raw child created without the profile helper must still inherit
            # only the trusted OI/CI directory ACL before explicit hardening.
            probe = self.profile.parent / "inherited-acl-probe.tmp"
            probe.write_bytes(b"")
            try:
                snapshot = profile_module._windows_security_snapshot(probe)
                # A raw child can retain the runner's default owner until its
                # own hardening pass.  Validate only the inherited access
                # semantics here; hardened profile files are owner-checked
                # separately above and by the production validator.
                self.assertIsNone(
                    profile_module._windows_snapshot_acl_problem(
                        snapshot._replace(owner_sid=profile_module._current_user_sid()),
                        is_directory=False,
                        require_protected=False,
                    )
                )
                self.assertTrue(any(ace.flags & 0x10 for ace in snapshot.allowed_aces))
            finally:
                probe.unlink()
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_first_run_name_shorthand_matches_documented_command(self) -> None:
        result = self.run_cli(
            "init",
            "--name",
            "Operator One",
            "--email",
            "person.one@example.com",
        )

        self.assertTrue(result["configured"])
        self.assertEqual(result["operator"]["preferred_name"], "Operator One")
        self.assertEqual(self.read_profile()["operators"][0]["preferred_name"], "Operator One")

    def test_init_refuses_existing_profile_without_changing_it(self) -> None:
        self.init_profile()
        original = self.profile.read_bytes()
        result = self.run_cli(
            "init",
            "--preferred-name",
            "Replacement",
            "--email",
            "replacement@example.com",
            expected_code=2,
        )
        self.assertEqual(result["error"]["code"], "profile_exists")
        self.assertEqual(self.profile.read_bytes(), original)

    def test_atomic_create_refuses_an_existing_destination(self) -> None:
        self.init_profile()
        original = self.profile.read_bytes()

        with self.assertRaises(profile_module.ProfileError) as raised:
            profile_module._atomic_write(
                self.profile, {"sentinel": "must not replace"}, replace=False
            )

        self.assertEqual(raised.exception.code, "profile_exists")
        self.assertEqual(self.profile.read_bytes(), original)

    def test_show_redacts_contact_values_unless_full_is_explicit(self) -> None:
        self.init_profile()
        redacted = self.run_cli("show")
        operator = redacted["profile"]["operators"][0]
        self.assertTrue(redacted["redacted"])
        self.assertEqual(operator["email_aliases"], ["***"])
        self.assertEqual(operator["provider_principals"][0]["principal"], "***")
        self.assertEqual(operator["provider_principals"][0]["provider"], "google-chat")
        self.assertNotIn("Person.One@Example.com", json.dumps(redacted))
        self.assertNotIn("users/123", json.dumps(redacted))

        full = self.run_cli("show", "--full")
        full_operator = full["profile"]["operators"][0]
        self.assertFalse(full["redacted"])
        self.assertEqual(full_operator["email_aliases"], ["Person.One@Example.com"])
        self.assertEqual(full_operator["provider_principals"][0]["principal"], "users/123")

    def test_multiple_operators_never_create_an_implicit_selection(self) -> None:
        self.init_profile()
        added = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Operator Two",
            "--email",
            "two@example.com",
        )
        status = self.run_cli("status")
        self.assertEqual(added["operator_count"], 2)
        self.assertTrue(added["operator_selection_required"])
        self.assertEqual(status["operator_count"], 2)
        self.assertTrue(status["operator_selection_required"])
        self.assertIsNone(status["active_operator_id"])
        self.assertNotIn("active_operator_id", self.run_cli("show")["profile"])

    def test_resolve_requires_and_uses_an_exact_identity_key(self) -> None:
        first = self.init_profile()
        second = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Operator Two",
            "--email",
            "two@example.com",
            "--provider-principal",
            "slack=U456",
        )
        by_email = self.run_cli("resolve", "--email", "Person.One@example.COM")
        by_principal = self.run_cli("resolve", "--provider", "SLACK", "--principal", "U456")
        wrong_principal_case = self.run_cli("resolve", "--provider", "slack", "--principal", "u456")
        absent = self.run_cli("resolve", "--email", "absent@example.com")

        self.assertTrue(by_email["found"])
        self.assertEqual(by_email["operator"]["operator_id"], first["operator"]["operator_id"])
        self.assertTrue(by_principal["found"])
        self.assertEqual(by_principal["operator"]["operator_id"], second["operator"]["operator_id"])
        self.assertFalse(by_principal["authority_asserted"])
        self.assertFalse(wrong_principal_case["found"])
        self.assertFalse(absent["found"])
        self.assertIsNone(absent["operator"])

        missing_selector = self.run_cli("resolve", expected_code=2)
        self.assertEqual(missing_selector["error"]["code"], "invalid_arguments")
        partial_selector = self.run_cli("resolve", "--provider", "slack", expected_code=2)
        self.assertEqual(partial_selector["error"]["code"], "invalid_arguments")

    def test_duplicate_email_ownership_normalizes_only_the_domain(self) -> None:
        self.init_profile(email="Owner@Example.com")
        result = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Duplicate",
            "--email",
            "Owner@example.COM",
            expected_code=2,
        )
        self.assertEqual(result["error"]["code"], "duplicate_identity")
        self.assertEqual(len(self.read_profile()["operators"]), 1)

    def test_email_local_part_case_variants_can_belong_to_distinct_operators(self) -> None:
        first = self.init_profile(email="Owner@MÜNICH.example")
        second = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Lowercase Local",
            "--email",
            "owner@xn--mnich-kva.EXAMPLE",
        )

        upper = self.run_cli("resolve", "--email", "Owner@xn--mnich-kva.example")
        lower = self.run_cli("resolve", "--email", "owner@MÜNICH.EXAMPLE")
        self.assertEqual(upper["operator"]["operator_id"], first["operator"]["operator_id"])
        self.assertEqual(lower["operator"]["operator_id"], second["operator"]["operator_id"])

    def test_bind_identity_adds_email_and_provider_without_changing_ids(self) -> None:
        initialized = self.init_profile()
        before = self.read_profile()
        operator_id = initialized["operator"]["operator_id"]
        result = self.run_cli(
            "bind-identity",
            "--operator-id",
            operator_id,
            "--email",
            "Alias@Example.net",
            "--provider-principal",
            "slack=U999",
        )
        after = self.read_profile()

        self.assertTrue(result["updated"])
        for key in ("profile_id", "installation_id", "agent_instance_id", "created_at"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["operators"][0]["operator_id"], operator_id)
        self.assertEqual(after["operators"][0]["created_at"], before["operators"][0]["created_at"])
        self.assertGreater(after["updated_at"], before["updated_at"])
        self.assertGreater(
            after["operators"][0]["updated_at"],
            before["operators"][0]["updated_at"],
        )
        by_email = self.run_cli("resolve", "--email", "Alias@example.NET")
        by_principal = self.run_cli("resolve", "--provider", "SLACK", "--principal", "U999")
        self.assertEqual(by_email["operator"]["operator_id"], operator_id)
        self.assertEqual(by_principal["operator"]["operator_id"], operator_id)

    def test_bind_identity_is_idempotent_and_does_not_rewrite(self) -> None:
        initialized = self.init_profile()
        operator_id = initialized["operator"]["operator_id"]
        first = self.run_cli(
            "bind-identity",
            "--operator-id",
            operator_id,
            "--email",
            "new@example.com",
            "--provider-principal",
            "slack=U123",
        )
        self.assertTrue(first["updated"])
        before = self.profile.read_bytes()
        repeated = self.run_cli(
            "bind-identity",
            "--operator-id",
            operator_id,
            "--email",
            "new@EXAMPLE.COM",
            "--provider-principal",
            "SLACK=U123",
        )
        self.assertFalse(repeated["updated"])
        self.assertEqual(self.profile.read_bytes(), before)

    def test_bind_identity_rejects_duplicate_ownership_without_mutation(self) -> None:
        first = self.init_profile()
        second = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Operator Two",
            "--email",
            "two@example.com",
            "--provider-principal",
            "slack=U222",
        )
        before = self.profile.read_bytes()
        duplicate_email = self.run_cli(
            "bind-identity",
            "--operator-id",
            first["operator"]["operator_id"],
            "--email",
            "two@EXAMPLE.COM",
            expected_code=2,
        )
        self.assertEqual(duplicate_email["error"]["code"], "duplicate_identity")
        self.assertEqual(self.profile.read_bytes(), before)
        duplicate_principal = self.run_cli(
            "bind-identity",
            "--operator-id",
            first["operator"]["operator_id"],
            "--provider-principal",
            "SLACK=U222",
            expected_code=2,
        )
        self.assertEqual(duplicate_principal["error"]["code"], "duplicate_identity")
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertNotEqual(first["operator"]["operator_id"], second["operator"]["operator_id"])

    def test_bind_identity_rejects_unknown_operator_and_empty_binding(self) -> None:
        self.init_profile()
        before = self.profile.read_bytes()
        unknown = self.run_cli(
            "bind-identity",
            "--operator-id",
            "operator_" + "f" * 32,
            "--email",
            "unknown@example.com",
            expected_code=2,
        )
        self.assertEqual(unknown["error"]["code"], "unknown_operator")
        empty = self.run_cli(
            "bind-identity",
            "--operator-id",
            self.read_profile()["operators"][0]["operator_id"],
            expected_code=2,
        )
        self.assertEqual(empty["error"]["code"], "invalid_input")
        self.assertEqual(self.profile.read_bytes(), before)

    def test_duplicate_provider_principal_ownership_is_rejected(self) -> None:
        self.init_profile(principal="google-chat=users/123")
        result = self.run_cli(
            "add-operator",
            "--preferred-name",
            "Duplicate",
            "--email",
            "different@example.com",
            "--provider-principal",
            "GOOGLE-CHAT=users/123",
            expected_code=2,
        )
        self.assertEqual(result["error"]["code"], "duplicate_identity")

    def test_malformed_and_oversized_input_are_rejected(self) -> None:
        malformed = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "not-an-email",
            expected_code=2,
        )
        self.assertEqual(malformed["error"]["code"], "invalid_input")
        self.assertFalse(self.profile.exists())

        oversized = self.run_cli(
            "init",
            "--preferred-name",
            "x" * 201,
            "--email",
            "person@example.com",
            expected_code=2,
        )
        self.assertEqual(oversized["error"]["code"], "invalid_input")

        empty_display = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--display-name",
            "",
            "--email",
            "person@example.com",
            expected_code=2,
        )
        self.assertEqual(empty_display["error"]["code"], "invalid_input")

    def test_unknown_fields_and_invalid_ids_are_rejected(self) -> None:
        self.init_profile()
        unknown = self.read_profile()
        unknown["unexpected"] = True
        self.write_profile(unknown)
        result = self.run_cli("validate", expected_code=2)
        self.assertEqual(result["error"]["code"], "invalid_profile")

        del unknown["unexpected"]
        unknown["profile_id"] = "profile_not-a-uuid"
        self.write_profile(unknown)
        result = self.run_cli("validate", expected_code=2)
        self.assertEqual(result["error"]["code"], "invalid_profile")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        self.init_profile()
        original = self.profile.read_text(encoding="utf-8")
        duplicate = original.replace(
            '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,', 1
        )
        self.profile.write_text(duplicate, encoding="utf-8")
        os.chmod(self.profile, 0o600)
        result = self.run_cli("validate", expected_code=2)
        self.assertEqual(result["error"]["code"], "invalid_profile")

    def test_profile_and_lock_permission_failures_are_rejected(self) -> None:
        self.init_profile()
        if IS_WINDOWS:
            self.set_broad_windows_acl(self.profile)
        else:
            os.chmod(self.profile, 0o644)
        result = self.run_cli("status", expected_code=2)
        self.assertEqual(result["error"]["code"], "unsafe_permissions")

        if IS_WINDOWS:
            profile_module._set_windows_dacl_from_sddl(
                self.profile,
                profile_module._private_windows_sddl(inherit=False),
            )
        else:
            os.chmod(self.profile, 0o600)
        lock = self.profile.with_name("profile.json.lock")
        if IS_WINDOWS:
            self.set_broad_windows_acl(lock)
        else:
            os.chmod(lock, 0o644)
        result = self.run_cli("status", expected_code=2)
        self.assertEqual(result["error"]["code"], "unsafe_permissions")

    def test_unsafe_parent_permissions_are_rejected(self) -> None:
        self.profile.parent.mkdir(parents=True)
        if IS_WINDOWS:
            self.set_broad_windows_acl(self.profile.parent, inherit=True)
        else:
            os.chmod(self.profile.parent, 0o755)
        result = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "person@example.com",
            expected_code=2,
        )
        self.assertEqual(result["error"]["code"], "unsafe_permissions")
        self.assertFalse(self.profile.exists())

    @unittest.skipUnless(IS_WINDOWS, "Windows DACL ancestry behavior")
    def test_windows_mutable_ancestor_is_rejected(self) -> None:
        broad_ancestor = self.root / "broad-ancestor"
        broad_ancestor.mkdir()
        self.set_broad_windows_acl(broad_ancestor, inherit=True)
        nested_profile = broad_ancestor / "nested" / "profile.json"

        result = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "person@example.com",
            expected_code=2,
            profile=nested_profile,
        )

        self.assertEqual(result["error"]["code"], "unsafe_permissions")
        self.assertFalse(nested_profile.exists())

    @unittest.skipUnless(IS_WINDOWS, "Windows DACL principal behavior")
    def test_windows_broad_well_known_groups_are_rejected(self) -> None:
        self.init_profile()

        for principal in ("WD", "BU", "AU"):
            with self.subTest(principal=principal):
                self.set_broad_windows_acl(self.profile, principal=principal)
                result = self.run_cli("validate", expected_code=2)
                self.assertEqual(result["error"]["code"], "unsafe_permissions")

    @unittest.skipUnless(IS_WINDOWS, "Windows Codex sandbox ACL behavior")
    def test_windows_codex_sandbox_write_access_is_rejected(self) -> None:
        sandbox = profile_module._codex_sandbox_sid()
        if sandbox is None:
            self.skipTest("CodexSandboxUsers is not installed")
        self.init_profile()
        profile_module._set_windows_dacl_from_sddl(
            self.profile,
            "D:P"
            f"(A;;FA;;;{profile_module._current_user_sid()})"
            f"(A;;FA;;;SY)"
            f"(A;;FA;;;BA)"
            f"(A;;FA;;;{sandbox})",
        )

        result = self.run_cli("validate", expected_code=2)

        self.assertEqual(result["error"]["code"], "unsafe_permissions")

    def test_symlinked_profile_directory_is_rejected(self) -> None:
        real_directory = self.root / "real-private"
        real_directory.mkdir(mode=0o700)
        self.make_symlink(self.profile.parent, real_directory, directory=True)
        result = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "person@example.com",
            expected_code=2,
        )
        self.assertEqual(result["error"]["code"], "unsafe_path")
        self.assertFalse((real_directory / "profile.json").exists())

    def test_symlink_above_immediate_parent_is_rejected(self) -> None:
        real_ancestor = self.root / "real-ancestor"
        real_ancestor.mkdir(mode=0o700)
        alias = self.root / "alias-ancestor"
        self.make_symlink(alias, real_ancestor, directory=True)
        nested_profile = alias / "nested" / "profile.json"
        result = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "person@example.com",
            expected_code=2,
            profile=nested_profile,
        )
        self.assertEqual(result["error"]["code"], "unsafe_path")
        self.assertFalse((real_ancestor / "nested" / "profile.json").exists())

    def test_profile_and_lock_symlinks_are_rejected(self) -> None:
        self.profile.parent.mkdir(mode=0o700, parents=True)
        if IS_WINDOWS:
            profile_module._set_private_path_mode(self.profile.parent, 0o700)
        target = self.profile.parent / "target.json"
        target.write_text("{}\n", encoding="utf-8")
        os.chmod(target, 0o600)
        self.make_symlink(self.profile, target)
        result = self.run_cli("status", expected_code=2)
        self.assertEqual(result["error"]["code"], "unsafe_path")

        self.profile.unlink()
        self.init_profile()
        lock = self.profile.with_name("profile.json.lock")
        lock.unlink()
        self.make_symlink(lock, target)
        result = self.run_cli("status", expected_code=2)
        self.assertEqual(result["error"]["code"], "unsafe_path")

    @unittest.skipUnless(IS_WINDOWS, "Windows junction behavior")
    def test_windows_junction_in_profile_path_is_rejected(self) -> None:
        real_directory = self.root / "real-junction-target"
        real_directory.mkdir()
        junction = self.root / "junction-private"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real_directory)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(created.returncode, 0, created.stderr or created.stdout)

        result = self.run_cli(
            "init",
            "--preferred-name",
            "Operator",
            "--email",
            "person@example.com",
            expected_code=2,
            profile=junction / "profile.json",
        )

        self.assertEqual(result["error"]["code"], "unsafe_path")
        self.assertFalse((real_directory / "profile.json").exists())

    def test_lock_wait_is_bounded(self) -> None:
        self.init_profile()
        lock_path = self.profile.with_name("profile.json.lock")
        lock_fd = os.open(lock_path, os.O_RDWR)
        self.addCleanup(os.close, lock_fd)
        self.assertTrue(profile_module._try_lock_file(lock_fd, exclusive=True))
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--profile",
                str(self.profile),
                "--lock-timeout",
                "0.05",
                "status",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "lock_timeout")

    @unittest.skipUnless(IS_WINDOWS, "Windows lock publication hardening race")
    def test_windows_lock_reopens_until_new_peer_owner_is_hardened(self) -> None:
        lock_path = self.root / "publication-race.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            profile_module._set_private_fd_mode(descriptor, 0o600)
        finally:
            os.close(descriptor)

        real_snapshot = profile_module._windows_descriptor_security_snapshot
        observed_foreign_owner = False

        def publication_snapshot(fd: int) -> object:
            nonlocal observed_foreign_owner
            snapshot = real_snapshot(fd)
            if not observed_foreign_owner:
                observed_foreign_owner = True
                return snapshot._replace(owner_sid=profile_module._ADMINISTRATORS_SID)
            return snapshot

        with (
            mock.patch.object(
                profile_module,
                "_windows_descriptor_security_snapshot",
                side_effect=publication_snapshot,
            ) as snapshot,
            mock.patch.object(profile_module.time, "sleep") as sleep,
            mock.patch.object(profile_module.os, "close", wraps=os.close) as close,
            mock.patch.object(profile_module, "_set_private_fd_mode") as harden,
            profile_module._FileLock(lock_path, exclusive=True, timeout=1.0),
        ):
            pass

        self.assertTrue(observed_foreign_owner)
        self.assertGreaterEqual(snapshot.call_count, 3)
        sleep.assert_called_once()
        self.assertGreaterEqual(close.call_count, 2)
        harden.assert_not_called()

    @unittest.skipUnless(IS_WINDOWS, "Windows foreign lock ownership behavior")
    def test_windows_non_administrators_lock_is_rejected_without_retry(self) -> None:
        lock_path = self.root / "foreign.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            profile_module._set_private_fd_mode(descriptor, 0o600)
        finally:
            os.close(descriptor)

        real_snapshot = profile_module._windows_descriptor_security_snapshot

        def foreign_snapshot(fd: int) -> object:
            return real_snapshot(fd)._replace(owner_sid=profile_module._SYSTEM_SID)

        with (
            mock.patch.object(
                profile_module,
                "_windows_descriptor_security_snapshot",
                side_effect=foreign_snapshot,
            ),
            mock.patch.object(profile_module.time, "sleep") as sleep,
            mock.patch.object(profile_module, "_set_private_fd_mode") as harden,
            self.assertRaises(profile_module.ProfileError) as raised,
        ):
            with profile_module._FileLock(lock_path, exclusive=True, timeout=30.0):
                pass

        self.assertEqual(raised.exception.code, "unsafe_permissions")
        sleep.assert_not_called()
        harden.assert_not_called()

    @unittest.skipUnless(IS_WINDOWS, "Windows lock hardening grace behavior")
    def test_windows_administrators_lock_hardening_grace_is_capped(self) -> None:
        lock_path = self.root / "administrators.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            profile_module._set_private_fd_mode(descriptor, 0o600)
        finally:
            os.close(descriptor)

        real_snapshot = profile_module._windows_descriptor_security_snapshot

        def administrators_snapshot(fd: int) -> object:
            return real_snapshot(fd)._replace(owner_sid=profile_module._ADMINISTRATORS_SID)

        with (
            mock.patch.object(
                profile_module,
                "_windows_descriptor_security_snapshot",
                side_effect=administrators_snapshot,
            ),
            mock.patch.object(
                profile_module.time, "monotonic", side_effect=[100.0, 101.0]
            ) as monotonic,
            mock.patch.object(profile_module.time, "sleep") as sleep,
            mock.patch.object(profile_module, "_set_private_fd_mode") as harden,
            self.assertRaises(profile_module.ProfileError) as raised,
        ):
            with profile_module._FileLock(lock_path, exclusive=True, timeout=30.0):
                pass

        self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.assertEqual(monotonic.call_count, 2)
        sleep.assert_not_called()
        harden.assert_not_called()

    def test_concurrent_operator_additions_are_serialized_without_loss(self) -> None:
        self.init_profile()
        processes: list[subprocess.Popen[str]] = []
        for index in range(4):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--profile",
                        str(self.profile),
                        "--lock-timeout",
                        "5",
                        "add-operator",
                        "--preferred-name",
                        f"Concurrent {index}",
                        "--email",
                        f"concurrent-{index}@example.com",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, f"stdout={stdout!r} stderr={stderr!r}")
            self.assertTrue(json.loads(stdout)["ok"])
        value = self.read_profile()
        self.assertEqual(len(value["operators"]), 5)
        self.assertTrue(self.run_cli("validate")["valid"])

    def test_explicit_default_profile_init_is_private_and_utf8(self) -> None:
        isolated_home = self.root / "explicit-default-home"
        isolated_home.mkdir()
        environment = os.environ.copy()
        environment["HOME"] = str(isolated_home)
        environment["USERPROFILE"] = str(isolated_home)
        environment["PYTHONUTF8"] = "0"
        environment.pop("PYTHONIOENCODING", None)
        preferred_name = "Operator → 🧠"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "init",
                "--preferred-name",
                preferred_name,
                "--email",
                "person@example.com",
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=10,
        )
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        self.assertEqual(result.returncode, 0, f"stdout={stdout!r} stderr={stderr!r}")
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["operator"]["preferred_name"], preferred_name)

        default_profile = isolated_home / ".codex" / "tedd" / "profile.json"
        self.assertEqual(
            json.loads(default_profile.read_text(encoding="utf-8"))["operators"][0][
                "preferred_name"
            ],
            preferred_name,
        )
        if IS_WINDOWS:
            self.assertIsNone(
                profile_module._windows_acl_problem(default_profile.parent, is_directory=True)
            )
            self.assertIsNone(
                profile_module._windows_acl_problem(default_profile, is_directory=False)
            )
        else:
            self.assertEqual(stat.S_IMODE(default_profile.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(default_profile.stat().st_mode), 0o600)

    def test_import_does_not_initialize_the_default_profile(self) -> None:
        isolated_home = self.root / "isolated-home"
        isolated_home.mkdir()
        environment = os.environ.copy()
        environment["HOME"] = str(isolated_home)
        environment["USERPROFILE"] = str(isolated_home)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib.util; "
                    "s=importlib.util.spec_from_file_location("
                    f"'isolated_tedd_profile', {str(SCRIPT)!r}); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((isolated_home / ".codex" / "tedd" / "profile.json").exists())
        self.assertEqual(profile_module.DEFAULT_PROFILE.as_posix(), "~/.codex/tedd/profile.json")
        self.assertFalse(self.profile.exists())


if __name__ == "__main__":
    unittest.main()
