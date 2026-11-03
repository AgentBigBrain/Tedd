"""The installation check must notice missing, changed, and extra package bytes."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_plugin_install as verifier  # noqa: E402

from tests.support.paths import REPOSITORY_ROOT as ROOT


class VerifyPluginInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.source = self.root / "reviewed source"
        self.installed = self.root / "installed package"
        for root in (self.source, self.installed):
            (root / "skills").mkdir(parents=True)
            (root / "skills/guide.md").write_bytes(b"reviewed bytes\n")

    def test_equal_complete_trees_have_the_same_manifest(self) -> None:
        result = verifier.compare_installation(self.source, self.installed)
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["source_files"])
        self.assertEqual(result["source_manifest_sha256"], result["installed_manifest_sha256"])

    def test_reports_missing_changed_and_extra_files_separately(self) -> None:
        (self.source / "missing.md").write_bytes(b"required")
        (self.installed / "skills/guide.md").write_bytes(b"changed\n")
        (self.installed / "node_modules").mkdir()
        (self.installed / "node_modules/extra.js").write_bytes(b"unexpected dependency")
        before = (self.installed / "skills/guide.md").read_bytes()
        result = verifier.compare_installation(self.source, self.installed)
        self.assertFalse(result["ok"])
        self.assertEqual(["missing.md"], result["missing"])
        self.assertEqual(["skills/guide.md"], result["changed"])
        self.assertEqual(["node_modules/extra.js"], result["unexpected"])
        self.assertEqual(before, (self.installed / "skills/guide.md").read_bytes())

    def test_bytecode_is_reported_not_silently_ignored(self) -> None:
        (self.installed / "__pycache__").mkdir()
        (self.installed / "__pycache__/module.pyc").write_bytes(b"cache")
        self.assertFalse(verifier.compare_installation(self.source, self.installed)["ok"])

    def test_same_or_nested_roots_cannot_prove_installation_identity(self) -> None:
        for other in (self.source, self.source / "skills"):
            with self.subTest(other=other), self.assertRaises(ValueError):
                verifier.compare_installation(self.source, other)

    def test_symlink_is_rejected_without_reading_its_target(self) -> None:
        link = self.installed / "linked.md"
        try:
            link.symlink_to(self.source / "skills/guide.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable for this test account")
        with self.assertRaises(ValueError):
            verifier.compare_installation(self.source, self.installed)

    def test_reparse_point_is_rejected(self) -> None:
        metadata = mock.Mock(st_file_attributes=0x400)
        with (
            mock.patch.object(Path, "lstat", return_value=metadata),
            self.assertRaises(ValueError),
        ):
            verifier.compare_installation(self.source, self.installed)

    def test_cli_has_a_nonzero_exit_for_a_different_install(self) -> None:
        (self.installed / "extra.md").write_bytes(b"extra")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/verify_plugin_install.py"),
                "--source",
                str(self.source),
                "--installed",
                str(self.installed),
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(1, result.returncode)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_cli_filesystem_errors_do_not_dump_local_paths(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                sys, "argv", ["verify", "--source", "missing", "--installed", "absent"]
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(2, verifier.main())
        result = json.loads(output.getvalue())
        self.assertEqual("installation_comparison_unavailable", result["error"])


if __name__ == "__main__":
    unittest.main()
