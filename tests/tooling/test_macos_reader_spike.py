"""Behavioral tests for the pure macOS reader launch-policy builder."""

from __future__ import annotations

import builtins
import os
import subprocess
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scripts import macos_reader_spike as spike

SOCKET_PATH = "/private/tmp/tedd-probe/docker.sock"
IMAGE_ID = "sha256:" + "a" * 64
JOB_ID = "12345678-1234-4234-8234-123456789abc"
OPERATIONS = ("image-overview", "pdf-text", "pdf-page", "office-page", "self-check")


class MacosReaderSpikeTests(unittest.TestCase):
    def test_explicit_socket_and_immutable_image(self) -> None:
        """Keep the private endpoint explicit and reject mutable images."""

        argv = spike.build_run_argv(
            "/private/tmp/tedd-probe/docker.sock",
            "sha256:" + "a" * 64,
            "12345678-1234-4234-8234-123456789abc",
            "pdf-text",
        )

        self.assertEqual(
            argv[:3],
            ("docker", "--host", "unix:///private/tmp/tedd-probe/docker.sock"),
        )
        self.assertEqual(argv[-2:], ("sha256:" + "a" * 64, "pdf-text"))
        with self.assertRaises(ValueError):
            spike.build_run_argv(
                "/private/tmp/tedd-probe/docker.sock",
                "reader:latest",
                "12345678-1234-4234-8234-123456789abc",
                "pdf-text",
            )

    def test_emits_literal_constrained_run_boundary(self) -> None:
        """Catch weakened isolation flags, limits, identity binding, or ordering."""

        argv = spike.build_run_argv(SOCKET_PATH, IMAGE_ID, JOB_ID, "pdf-page")

        self.assertEqual(
            argv,
            (
                "docker",
                "--host",
                "unix:///private/tmp/tedd-probe/docker.sock",
                "run",
                "--pull",
                "never",
                "--platform",
                "linux/arm64",
                "--rm",
                "--interactive",
                "--name",
                "tedd-reader-spike-12345678-1234-4234-8234-123456789abc",
                "--label",
                "org.tedd.reader-spike.job=12345678-1234-4234-8234-123456789abc",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "64",
                "--memory",
                "2147483648",
                "--memory-swap",
                "2147483648",
                "--cpus",
                "2",
                "--log-driver",
                "none",
                "--ulimit",
                "core=0:0",
                "--tmpfs",
                "/work:rw,nosuid,nodev,noexec,size=268435456,mode=0700,uid=65532,gid=65532",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=0700,uid=65532,gid=65532",
                "--workdir",
                "/work",
                "--env",
                "HOME=/work",
                "--env",
                "TMPDIR=/work",
                "--env",
                "LANG=C.UTF-8",
                "--env",
                "LC_ALL=C.UTF-8",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "/usr/local/bin/tedd-reader-probe",
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "pdf-page",
            ),
        )
        for forbidden in ("--mount", "--volume", "-v", "--privileged", "--tty", "-t"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, argv)

    def test_accepts_every_closed_operation(self) -> None:
        """Catch an allowed reader mode being omitted or rewritten."""

        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                self.assertEqual(
                    (IMAGE_ID, operation),
                    spike.build_run_argv(SOCKET_PATH, IMAGE_ID, JOB_ID, operation)[-2:],
                )

    def test_rejects_unknown_and_non_string_operations(self) -> None:
        """Catch arbitrary entrypoint arguments crossing the closed operation boundary."""

        for operation in ("extract-all", "PDF-TEXT", "pdf-text\n", "", 7, None):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                spike.build_run_argv(
                    SOCKET_PATH,
                    IMAGE_ID,
                    JOB_ID,
                    cast(str, operation),
                )

    def test_rejects_mutable_malformed_and_non_string_images(self) -> None:
        """Catch tags, uppercase or wrong-length digests, and runtime type bypasses."""

        for image_id in (
            "reader:latest",
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "A" * 64,
            "sha512:" + "a" * 64,
            "sha256:" + "g" * 64,
            "sha256:" + "a" * 63 + "\n",
            "",
            7,
            None,
        ):
            with self.subTest(image_id=image_id), self.assertRaises(ValueError):
                spike.build_run_argv(
                    SOCKET_PATH,
                    cast(str, image_id),
                    JOB_ID,
                    "pdf-text",
                )

    def test_rejects_noncanonical_and_non_socket_paths(self) -> None:
        """Catch default, ambiguous, traversing, or wrong-socket runtime endpoints."""

        for socket_path in (
            "",
            "private/tmp/tedd-probe/docker.sock",
            "./docker.sock",
            "/private/tmp/tedd-probe/./docker.sock",
            "/private/tmp/tedd-probe/../docker.sock",
            "/private//tmp/tedd-probe/docker.sock",
            "//private/tmp/tedd-probe/docker.sock",
            "/private/tmp/tedd-probe/docker.sock/",
            "/private/tmp/tedd-probe/podman.sock",
            "/private/tmp/tedd\nprobe/docker.sock",
            "/private/tmp/tedd\x00probe/docker.sock",
            "/private/tmp/tedd\x85probe/docker.sock",
            7,
            None,
        ):
            with self.subTest(socket_path=socket_path), self.assertRaises(ValueError):
                spike.build_run_argv(
                    cast(str, socket_path),
                    IMAGE_ID,
                    JOB_ID,
                    "pdf-text",
                )

    def test_accepts_canonical_socket_path_with_parent_spaces(self) -> None:
        """Catch accidental shell-style splitting or over-restricting valid POSIX names."""

        argv = spike.build_run_argv(
            "/private/tmp/tedd probe/docker.sock",
            IMAGE_ID,
            JOB_ID,
            "pdf-text",
        )

        self.assertEqual("unix:///private/tmp/tedd probe/docker.sock", argv[2])
        unicode_argv = spike.build_run_argv(
            "/private/tmp/tédd probe/docker.sock",
            IMAGE_ID,
            JOB_ID,
            "pdf-text",
        )
        self.assertEqual("unix:///private/tmp/tédd probe/docker.sock", unicode_argv[2])

    def test_rejects_noncanonical_and_non_string_job_ids(self) -> None:
        """Catch identifiers that can escape deterministic name and label binding."""

        for job_id in (
            "",
            "12345678-1234-4234-8234-123456789ab",
            "12345678123442348234123456789abc",
            "{12345678-1234-4234-8234-123456789abc}",
            "12345678-1234-4234-8234-123456789ABC",
            "12345678-1234-4234-8234-123456789abc\n",
            7,
            None,
        ):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                spike.build_run_argv(
                    SOCKET_PATH,
                    IMAGE_ID,
                    cast(str, job_id),
                    "pdf-text",
                )

    def test_errors_do_not_echo_unknown_input(self) -> None:
        """Catch untrusted values leaking through validation diagnostics."""

        marker = "DO_NOT_ECHO_5c06"
        cases = (
            (f"/{marker}/../docker.sock", IMAGE_ID, JOB_ID, "pdf-text"),
            (SOCKET_PATH, marker, JOB_ID, "pdf-text"),
            (SOCKET_PATH, IMAGE_ID, marker, "pdf-text"),
            (SOCKET_PATH, IMAGE_ID, JOB_ID, marker),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError) as raised:
                    spike.build_run_argv(*arguments)
                self.assertNotIn(marker, str(raised.exception))

    def test_building_arguments_does_not_execute_or_mutate_files(self) -> None:
        """Catch execution or filesystem writes being added to the pure builder."""

        blocked = AssertionError("side effect attempted")
        with (
            mock.patch.object(subprocess, "run", side_effect=blocked),
            mock.patch.object(subprocess, "Popen", side_effect=blocked),
            mock.patch.object(builtins, "open", side_effect=blocked),
            mock.patch.object(os, "mkdir", side_effect=blocked),
            mock.patch.object(os, "remove", side_effect=blocked),
            mock.patch.object(Path, "mkdir", side_effect=blocked),
            mock.patch.object(Path, "write_bytes", side_effect=blocked),
            mock.patch.object(Path, "write_text", side_effect=blocked),
            mock.patch.object(Path, "unlink", side_effect=blocked),
        ):
            argv = spike.build_run_argv(SOCKET_PATH, IMAGE_ID, JOB_ID, "self-check")

        self.assertEqual(
            ("docker", "--host", "unix:///private/tmp/tedd-probe/docker.sock", "run"),
            argv[:4],
        )
        self.assertEqual((IMAGE_ID, "self-check"), argv[-2:])


if __name__ == "__main__":
    unittest.main()
