#!/usr/bin/env python3
"""Tests for cross-platform benchmark helpers."""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmark_support import peak_rss_kib, private_temporary_parent


class BenchmarkSupportTests(unittest.TestCase):
    def test_explicit_temporary_parent_is_used_without_changing_platform_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"TEDD_PRIVATE_TEMP_PARENT": folder}):
                self.assertEqual(private_temporary_parent(), folder)

    def test_explicit_temporary_parent_rejects_relative_or_missing_paths(self) -> None:
        for value in ("relative", str(Path.home() / "tedd-missing-neutral-parent")):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"TEDD_PRIVATE_TEMP_PARENT": value}):
                    with self.assertRaises(RuntimeError):
                        private_temporary_parent()

    def test_peak_rss_is_positive_and_finite(self) -> None:
        measured = peak_rss_kib()
        self.assertGreater(measured, 0)
        self.assertTrue(math.isfinite(measured))

    def test_private_temporary_parent_matches_platform(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("TEDD_PRIVATE_TEMP_PARENT", None)
            parent = private_temporary_parent()
        if os.name == "nt":
            self.assertIsNotNone(parent)
            self.assertTrue(Path(parent).is_dir())
        else:
            self.assertIsNone(parent)


if __name__ == "__main__":
    unittest.main()
