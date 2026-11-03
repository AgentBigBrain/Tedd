#!/usr/bin/env python3
"""Focused native-platform tests for graph filesystem primitives."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import platform_support as platform  # noqa: E402

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY


class GuardedMutationTests(unittest.TestCase):
    def test_guard_pins_identity_and_excludes_owner_writes_or_refuses_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owned.txt"
            other = Path(temporary) / "owner-new.txt"
            path.write_bytes(b"original")
            other.write_bytes(b"owner replacement")
            identity = path.stat().st_ino
            if os.name != "nt":
                with self.assertRaises(platform.MutationGuardUnavailable):
                    with platform.guarded_file_mutation(path):
                        self.fail("unsupported guard yielded")
                self.assertEqual(path.read_bytes(), b"original")
                self.assertEqual(path.stat().st_ino, identity)
                return
            with platform.guarded_file_mutation(path) as fd:
                self.assertEqual(os.fstat(fd).st_ino, identity)
                self.assertEqual(os.read(fd, 8), b"original")
                with self.assertRaises(OSError):
                    path.write_bytes(b"owner edit")
                with self.assertRaises(OSError):
                    os.replace(other, path)
                self.assertEqual(os.fstat(fd).st_ino, identity)
                platform.delete_guarded_file(fd)
            self.assertFalse(path.exists())
            self.assertEqual(other.read_bytes(), b"owner replacement")


class StandardStreamTests(unittest.TestCase):
    def test_acl_policy_import_rejects_a_preloaded_module_from_another_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_directory = Path(temporary)
            (fake_directory / "windows_acl_policy.py").write_text(
                "MARKER = 'unexpected'\n",
                encoding="utf-8",
            )
            code = "\n".join(
                (
                    "import sys",
                    f"sys.path.insert(0, {str(fake_directory)!r})",
                    "import windows_acl_policy",
                    f"sys.path.insert(0, {str(SCRIPT_DIRECTORY)!r})",
                    "try:",
                    "    import platform_support",
                    "except ImportError as error:",
                    "    print(error)",
                    "else:",
                    "    raise SystemExit('unexpected ACL policy was accepted')",
                )
            )

            result = subprocess.run(
                [sys.executable, "-c", code],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refusing unexpected windows_acl_policy module", result.stdout)

    def test_graph_cli_reconfigures_cp1252_stream_to_utf8(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        with tempfile.TemporaryDirectory(dir=private_parent) as temporary:
            root = Path(temporary)
            platform.set_private_path_mode(root, 0o700)
            unicode_suffix = chr(0x2192) + "-" + chr(0x1F9E0)
            vault = root / f"vault-{unicode_suffix}"
            vault.mkdir()
            platform.set_private_path_mode(vault, 0o700)
            config = root / "config.json"
            store = root / "graph.json"
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "cp1252"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIRECTORY / "knowledge_graph.py"),
                    "--config",
                    str(config),
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(store),
                ],
                capture_output=True,
                env=environment,
            )
            stdout = result.stdout.decode("utf-8")
            stderr = result.stderr.decode("utf-8")
            self.assertEqual(result.returncode, 0, stderr)
            self.assertIn(unicode_suffix, stdout)


@unittest.skipUnless(os.name == "nt", "native Windows security tests")
class WindowsPlatformSupportTests(unittest.TestCase):
    def test_exclusive_file_is_private_before_post_creation_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.lock"
            descriptor = platform.create_private_exclusive_file(path)
            try:
                self.assertFalse(os.get_inheritable(descriptor))
                self.assertTrue(platform.owner_is_current_user(path))
                self.assertFalse(platform.mode_has_any(path, 0o077))
                self.assertIsNone(platform.private_windows_acl_problem(path))
                with self.assertRaises(FileExistsError):
                    platform.create_private_exclusive_file(path)
            finally:
                os.close(descriptor)

    def set_windows_administrators_owner(self, path: Path, *, directory: bool) -> None:
        if platform._windows_security_snapshot(path).owner_sid == platform._ADMINISTRATORS_SID:
            return
        inheritance = "OICI" if directory else ""
        try:
            platform._set_windows_dacl_from_sddl(
                path,
                "O:BAD:P"
                f"(A;{inheritance};FA;;;{platform._current_user_sid()})"
                f"(A;{inheritance};FA;;;SY)"
                f"(A;{inheritance};FA;;;BA)",
                set_owner=True,
            )
        except OSError as error:
            self.skipTest(f"cannot assign the Administrators owner: {error}")
        self.assertEqual(
            platform._windows_security_snapshot(path).owner_sid,
            platform._ADMINISTRATORS_SID,
        )

    def test_private_path_acl_claims_localappdata_temp_directory_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA")) as temporary:
            path = Path(temporary)

            platform.set_private_path_mode(path, 0o700)

            snapshot = platform._windows_security_snapshot(path)
            self.assertEqual(snapshot.owner_sid, platform._current_user_sid())
            self.assertFalse(platform.mode_has_any(path, 0o077))

    def test_guarded_generated_output_claims_administrators_owner(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA")) as temporary:
            root = Path(temporary)
            platform.set_private_path_mode(root, 0o700)
            output = root / "source.pdf"
            output.write_bytes(b"%PDF-synthetic")
            self.set_windows_administrators_owner(output, directory=False)
            self.assertFalse(platform.owner_is_current_user(output))
            self.assertFalse(platform.mode_has_any(output, 0o077))

            source_files.claim_private_generated_file(output)

            self.assertTrue(platform.owner_is_current_user(output))
            self.assertIsNone(platform.private_windows_acl_problem(output))
            self.assertEqual(output.read_bytes(), b"%PDF-synthetic")

    def test_private_path_acl_replaces_administrators_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA")) as temporary:
            path = Path(temporary)
            self.set_windows_administrators_owner(path, directory=True)

            platform.set_private_path_mode(path, 0o700)

            snapshot = platform._windows_security_snapshot(path)
            self.assertEqual(snapshot.owner_sid, platform._current_user_sid())
            self.assertFalse(platform.mode_has_any(path, 0o077))

    def test_private_path_acl_does_not_reapply_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner-restricted.json"
            path.write_text("{}", encoding="utf-8")
            platform.set_private_path_mode(path, 0o600)
            current_user = platform._current_user_sid()
            platform._set_windows_dacl_from_sddl(
                path,
                f"D:P(A;;FR;;;{current_user})(A;;FA;;;SY)(A;;FA;;;BA)",
            )
            restricted = platform._windows_security_snapshot(path)
            user_ace = next(ace for ace in restricted.allowed_aces if ace.sid == current_user)
            self.assertFalse(user_ace.mask & platform._WRITE_OWNER)

            platform.set_private_path_mode(path, 0o600)

            self.assertTrue(platform.owner_is_current_user(path))
            self.assertFalse(platform.mode_has_any(path, 0o077))

    def test_private_path_acl_rejects_broad_principals_and_preserves_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private.json"
            path.write_text("{}", encoding="utf-8")
            platform.set_private_path_mode(path, 0o600)
            platform._set_windows_dacl_from_sddl(
                path,
                (
                    f"O:{platform._SYSTEM_SID}D:P"
                    f"(A;;FA;;;{platform._current_user_sid()})"
                    "(A;;FR;;;WD)"
                    "(A;;FW;;;AU)"
                ),
            )

            adversarial = platform._windows_security_snapshot(path)
            self.assertEqual(adversarial.owner_sid, platform._current_user_sid())
            self.assertTrue(platform.mode_has_any(path, 0o044))
            self.assertTrue(platform.mode_has_any(path, 0o022))
            self.assertFalse(platform.owner_is_current_user(path.stat()))
            self.assertTrue(platform.mode_has_any(path.stat(), 0o077))

            platform.set_private_path_mode(path, 0o600)

            snapshot = platform._windows_security_snapshot(path)
            broad_sids = {"S-1-1-0", "S-1-5-11", "S-1-5-32-545"}
            self.assertTrue(snapshot.dacl_present)
            self.assertTrue(platform.owner_is_current_user(path))
            self.assertFalse(platform.mode_has_any(path, 0o077))
            self.assertTrue(all(ace.sid not in broad_sids for ace in snapshot.allowed_aces))
            sandbox_sid = platform._codex_sandbox_sid()
            if sandbox_sid:
                sandbox_ace = next(ace for ace in snapshot.allowed_aces if ace.sid == sandbox_sid)
                self.assertFalse(sandbox_ace.mask & platform._WINDOWS_WRITE_RIGHTS)
                self.assertTrue(sandbox_ace.mask & platform._WINDOWS_READ_RIGHTS)

    def test_codex_sandbox_mutation_is_not_treated_as_private(self) -> None:
        sandbox_sid = platform._codex_sandbox_sid()
        if sandbox_sid is None:
            self.skipTest("CodexSandboxUsers is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sandbox-writable.json"
            path.write_text("{}", encoding="utf-8")
            platform.set_private_path_mode(path, 0o600)
            try:
                platform._set_windows_dacl_from_sddl(
                    path,
                    "D:P"
                    f"(A;;FA;;;{platform._current_user_sid()})"
                    "(A;;FA;;;SY)"
                    "(A;;FA;;;BA)"
                    f"(A;;FA;;;{sandbox_sid})",
                )

                self.assertTrue(platform.mode_has_any(path, 0o022))
                self.assertEqual(
                    platform.private_windows_acl_problem(path),
                    "must grant CodexSandboxUsers read/execute access only",
                )
            finally:
                platform.set_private_path_mode(path, 0o600)

    def test_child_only_directory_mutation_counts_as_broad_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            snapshot = platform._WindowsSecuritySnapshot(
                owner_sid=platform._current_user_sid(),
                dacl_present=True,
                allowed_aces=(
                    platform._WindowsAce(
                        sid="S-1-1-0",
                        mask=platform._GENERIC_WRITE,
                        flags=(platform._INHERIT_ONLY_ACE | platform._OBJECT_INHERIT_ACE),
                    ),
                ),
                dacl_protected=True,
            )
            with mock.patch.object(
                platform,
                "_windows_security_snapshot",
                return_value=snapshot,
            ):
                self.assertTrue(platform.mode_has_any(directory, 0o022))

    def test_private_descriptor_acl_is_applied_to_the_open_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "descriptor.json"
            path.write_text("{}", encoding="utf-8")
            platform._set_windows_dacl_from_sddl(
                path,
                (f"D:P(A;;FA;;;{platform._current_user_sid()})(A;;FA;;;WD)"),
            )
            descriptor = os.open(path, os.O_RDWR)
            try:
                platform.set_private_fd_mode(descriptor, 0o600)
            finally:
                os.close(descriptor)

            self.assertTrue(platform.owner_is_current_user(path))
            self.assertFalse(platform.mode_has_any(path, 0o077))

    def test_private_descriptor_acl_replaces_administrators_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "administrators-owned.json"
            path.write_text("{}", encoding="utf-8")
            descriptor = os.open(path, os.O_RDWR)
            try:
                self.set_windows_administrators_owner(path, directory=False)

                platform.set_private_fd_mode(descriptor, 0o600)

                snapshot = platform._windows_descriptor_security_snapshot(descriptor)
                self.assertEqual(snapshot.owner_sid, platform._current_user_sid())
                self.assertFalse(platform.mode_has_any(descriptor, 0o077))
            finally:
                os.close(descriptor)

    def test_private_descriptor_acl_does_not_reapply_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner-restricted-descriptor.json"
            path.write_text("{}", encoding="utf-8")
            platform.set_private_path_mode(path, 0o600)
            descriptor = os.open(path, os.O_RDWR)
            try:
                current_user = platform._current_user_sid()
                platform._set_windows_dacl_from_sddl(
                    path,
                    f"D:P(A;;FR;;;{current_user})(A;;FA;;;SY)(A;;FA;;;BA)",
                )
                restricted = platform._windows_descriptor_security_snapshot(descriptor)
                user_ace = next(ace for ace in restricted.allowed_aces if ace.sid == current_user)
                self.assertFalse(user_ace.mask & platform._WRITE_OWNER)

                platform.set_private_fd_mode(descriptor, 0o600)

                self.assertTrue(platform.owner_is_current_user(descriptor))
                self.assertFalse(platform.mode_has_any(descriptor, 0o077))
            finally:
                os.close(descriptor)

    def test_private_directory_acl_protects_new_children_by_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private_directory = Path(temporary) / "graph-state"
            private_directory.mkdir()
            platform.set_private_path_mode(private_directory, 0o700)

            child_file = private_directory / "graph.sqlite3-wal"
            child_file.write_bytes(b"wal")
            child_directory = private_directory / "projection"
            child_directory.mkdir()

            for child in (child_file, child_directory):
                snapshot = platform._windows_security_snapshot(child)
                self.assertIn(
                    snapshot.owner_sid,
                    {
                        platform._current_user_sid(),
                        platform._ADMINISTRATORS_SID,
                    },
                )
                self.assertFalse(platform.mode_has_any(child, 0o077))
                self.assertTrue(any(ace.flags & 0x10 for ace in snapshot.allowed_aces))

                sandbox_sid = platform._codex_sandbox_sid()
                if sandbox_sid:
                    sandbox_ace = next(
                        ace for ace in snapshot.allowed_aces if ace.sid == sandbox_sid
                    )
                    self.assertFalse(sandbox_ace.mask & platform._WINDOWS_WRITE_RIGHTS)
                    self.assertTrue(sandbox_ace.mask & platform._WINDOWS_READ_RIGHTS)

            for child, mode in ((child_file, 0o600), (child_directory, 0o700)):
                inherited = platform._windows_security_snapshot(child).allowed_aces

                platform.set_private_path_mode(child, mode)

                hardened = platform._windows_security_snapshot(child)
                self.assertTrue(any(ace.flags & 0x10 for ace in inherited))
                self.assertEqual(hardened.owner_sid, platform._current_user_sid())
                self.assertFalse(platform.mode_has_any(child, 0o077))
                self.assertTrue(all(not ace.flags & 0x10 for ace in hardened.allowed_aces))

    def test_lockfileex_keeps_empty_files_unchanged_and_supports_shared_locks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graph.lock"
            path.touch()
            with path.open("r+b") as first, path.open("r+b") as second:
                platform.try_file_lock(first, shared=True)
                platform.try_file_lock(second, shared=True)
                platform.unlock_file(second)
                platform.unlock_file(first)

            with path.open("r+b") as first, path.open("r+b") as second:
                platform.try_file_lock(first)
                with self.assertRaises(BlockingIOError):
                    platform.try_file_lock(second, shared=True)
                platform.unlock_file(first)

            self.assertEqual(path.read_bytes(), b"")

    def test_directory_junction_is_rejected_as_a_reparse_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            junction = root / "junction"
            target.mkdir()
            result = subprocess.run(
                [
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(junction),
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"could not create a junction: {result.stderr.strip()}")
            try:
                self.assertTrue(platform.is_link_or_reparse_point(junction))
            finally:
                os.rmdir(junction)

    def test_volume_detection_distinguishes_local_and_remote_roots(self) -> None:
        local_path = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        local_type = platform.windows_volume_type(local_path)
        self.assertIsNotNone(local_type)
        self.assertNotEqual(local_type, "windows-remote")

        with (
            mock.patch.object(platform, "_windows_volume_root", return_value="Z:\\"),
            mock.patch.object(
                platform,
                "_windows_drive_type",
                return_value=platform._DRIVE_REMOTE,
            ),
            mock.patch.object(platform, "_windows_filesystem_name") as filesystem,
        ):
            self.assertEqual(
                platform.windows_volume_type(Path(r"Z:\graph")),
                "windows-remote",
            )
            filesystem.assert_not_called()

    def test_only_local_volume_root_gets_narrow_system_ancestor_exception(
        self,
    ) -> None:
        root = Path(Path(os.environ.get("SystemRoot", r"C:\Windows")).anchor)
        add_child_only = platform._WindowsSecuritySnapshot(
            owner_sid=platform._SYSTEM_SID,
            dacl_present=True,
            allowed_aces=(
                platform._WindowsAce(
                    sid="S-1-5-11",
                    mask=platform._FILE_APPEND_DATA,
                    flags=0,
                ),
            ),
        )
        broad_mutation = platform._WindowsSecuritySnapshot(
            owner_sid=platform._SYSTEM_SID,
            dacl_present=True,
            allowed_aces=(
                platform._WindowsAce(
                    sid="S-1-5-11",
                    mask=platform._WRITE_DAC,
                    flags=0,
                ),
            ),
        )
        sandbox_mutation = platform._WindowsSecuritySnapshot(
            owner_sid=platform._SYSTEM_SID,
            dacl_present=True,
            allowed_aces=(
                platform._WindowsAce(
                    sid="S-sandbox",
                    mask=platform._GENERIC_ALL,
                    flags=0,
                ),
            ),
        )
        with (
            mock.patch.object(platform, "_windows_volume_root", return_value=str(root)),
            mock.patch.object(platform, "_windows_drive_type", return_value=3),
            mock.patch.object(
                platform,
                "_windows_security_snapshot",
                return_value=add_child_only,
            ),
        ):
            self.assertTrue(platform.is_trusted_windows_volume_root(root))
            self.assertFalse(platform.is_trusted_windows_volume_root(root / "Users"))

        with (
            mock.patch.object(platform, "_windows_volume_root", return_value=str(root)),
            mock.patch.object(platform, "_windows_drive_type", return_value=3),
            mock.patch.object(
                platform,
                "_windows_security_snapshot",
                return_value=broad_mutation,
            ),
        ):
            self.assertFalse(platform.is_trusted_windows_volume_root(root))

        with (
            mock.patch.object(platform, "_windows_volume_root", return_value=str(root)),
            mock.patch.object(platform, "_windows_drive_type", return_value=3),
            mock.patch.object(
                platform,
                "_windows_security_snapshot",
                return_value=sandbox_mutation,
            ),
            mock.patch.object(platform, "_codex_sandbox_sid", return_value="S-sandbox"),
            mock.patch.object(
                platform,
                "_trusted_private_sids",
                return_value=frozenset({"S-sandbox"}),
            ),
        ):
            self.assertFalse(platform.is_trusted_windows_volume_root(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
