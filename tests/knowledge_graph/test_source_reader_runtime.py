"""Private Mac runtime binding tests with a synthetic Lima boundary."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_policy import ReaderRuntime
from tedd_knowledge_graph.source_reader_runtime import (
    MacRuntimeBinding,
    load_mac_runtime_binding,
)

from tests.knowledge_graph.test_source_reader_policy import DAEMON_ID, IMAGE_ID, OWNER_ID
from tests.support.sockets import require_unix_socket

_PROFILE = "colima-tedd-reader-spike"
_SHORT_PROFILE = "tedd-reader-spike"
_AF_UNIX = cast(int, socket.__dict__.get("AF_UNIX", -1))


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _identity(path: Path, *, hashed: bool) -> dict[str, object]:
    info = path.lstat()
    return {
        "path": str(path),
        "type": (
            "directory"
            if stat.S_ISDIR(info.st_mode)
            else "socket"
            if stat.S_ISSOCK(info.st_mode)
            else "regular"
        ),
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner": info.st_uid,
        "links": info.st_nlink,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if hashed else None,
    }


class MacRuntimeFixture:
    """Synthetic unit double for a measured Lima 2.2.0/VZ runtime."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".tedd-runtime-", dir=Path.home())
        self.base = Path(self.temporary.name)
        self.base.chmod(0o700)
        try:
            require_unix_socket(self.base / "probe")
        except BaseException:
            self.temporary.cleanup()
            raise
        self.root = self.base / "runtime"
        self.lima_home = self.root / "lima"
        self.instance = self.lima_home / _PROFILE
        self.disk_root = self.lima_home / "_disks" / _PROFILE
        self.config_root = self.lima_home / "_config"
        self.usernet_root = self.lima_home / "_networks" / "user-v2"
        self.colima_home = self.root / "colima"
        self.profile_root = self.colima_home / _SHORT_PROFILE
        self.state_root = self.root / "reader-state"
        for directory in (
            self.root,
            self.lima_home,
            self.instance,
            self.lima_home / "_disks",
            self.disk_root,
            self.config_root,
            self.lima_home / "_networks",
            self.usernet_root,
            self.colima_home,
            self.profile_root,
            self.state_root,
            self.root / "bin",
        ):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)

        self.socket = self.profile_root / "docker.sock"
        self.listener = socket.socket(_AF_UNIX)
        self.listener.bind(str(self.socket))
        self.socket.chmod(0o600)
        self.containerd_socket = self.profile_root / "containerd.sock"
        self.containerd_listener = socket.socket(_AF_UNIX)
        self.containerd_listener.bind(str(self.containerd_socket))
        self.containerd_socket.chmod(0o600)
        self.usernet_listeners: list[socket.socket] = []
        self.usernet_sockets: list[Path] = []
        for name in ("user-v2_ep.sock", "user-v2_fd.sock", "user-v2_qemu.sock"):
            path = self.usernet_root / name
            listener = socket.socket(_AF_UNIX)
            listener.bind(str(path))
            path.chmod(0o600)
            self.usernet_sockets.append(path)
            self.usernet_listeners.append(listener)
        self.docker = self.root / "bin" / "docker"
        self.docker.write_text("#!/bin/sh\nexit 0\n")
        self.docker.chmod(0o700)
        self.limactl = self.root / "bin" / "limactl"
        cache = self.root / "cache" / "caches"
        cache.mkdir(parents=True, mode=0o700)
        cache.parent.chmod(0o700)
        cache.chmod(0o700)
        self.image_source = cache / ("b" * 64)
        self.image_source.write_bytes(b"inactive synthetic boot template")
        self.image_source.chmod(0o600)

        self.config = {
            "vmType": "vz",
            "arch": "aarch64",
            "cpus": 2,
            "memory": "3GiB",
            "disk": "10GiB",
            "additionalDisks": [{"name": _PROFILE, "format": False, "fsType": "ext4"}],
            "firmware": {"legacyBIOS": False},
            "ssh": {
                "loadDotSSHPubKeys": False,
                "forwardAgent": False,
                "forwardX11": False,
                "forwardX11Trusted": False,
            },
            "nestedVirtualization": False,
            "mounts": [],
            "networks": [
                {
                    "lima": "user-v2",
                    "macAddress": "52:55:55:8d:47:e3",
                    "interface": "lima0",
                    "metric": 100,
                }
            ],
            "images": [{"location": str(self.image_source), "arch": "aarch64"}],
            "copyToHost": [],
            "portForwards": [
                {
                    "guestIPMustBeZero": False,
                    "guestIP": "127.0.0.1",
                    "guestPortRange": [1, 65535],
                    "guestSocket": "/var/run/docker.sock",
                    "hostIP": "127.0.0.1",
                    "hostPortRange": [0, 0],
                    "hostSocket": str(self.socket),
                    "proto": "tcp",
                },
                {
                    "guestIPMustBeZero": False,
                    "guestIP": "127.0.0.1",
                    "guestPortRange": [1, 65535],
                    "guestSocket": "/var/run/containerd/containerd.sock",
                    "hostIP": "127.0.0.1",
                    "hostPortRange": [0, 0],
                    "hostSocket": str(self.profile_root / "containerd.sock"),
                    "proto": "tcp",
                },
                {
                    "guestIPMustBeZero": True,
                    "guestIP": "0.0.0.0",
                    "guestPortRange": [1, 65535],
                    "hostIP": "127.0.0.1",
                    "hostPortRange": [1, 65535],
                    "proto": "any",
                    "ignore": True,
                },
            ],
        }
        self.record: dict[str, object] = {
            "name": _PROFILE,
            "status": "Running",
            "dir": str(self.instance),
            "vmType": "vz",
            "arch": "aarch64",
            "cpus": 2,
            "memory": 3221225472,
            "disk": 10737418240,
            "additionalDisks": [{"name": _PROFILE, "format": False, "fsType": "ext4"}],
            "limaVersion": "2.2.0",
            "network": self.config["networks"],
            "config": self.config,
        }
        self._write_limactl()

        self.static_paths = (
            self.profile_root / "colima.yaml",
            self.instance / "lima.yaml",
            self.instance / "colima.yaml",
            self.instance / "cloud-config.yaml",
            self.instance / "cidata.iso",
            self.instance / "vz-identifier",
            self.instance / "ssh.config",
            self.config_root / "user",
            self.config_root / "user.pub",
            self.config_root / "networks.yaml",
            self.colima_home / "ssh_config",
        )
        self.mutable_paths = (
            self.instance / "disk",
            self.disk_root / "datadisk",
            self.instance / "vz-efi",
            self.socket,
            self.containerd_socket,
            *self.usernet_sockets,
        )
        regular_mutable = tuple(path for path in self.mutable_paths if path.suffix != ".sock")
        for index, path in enumerate((*self.static_paths, *regular_mutable)):
            path.write_bytes(f"synthetic-{index}".encode())
            path.chmod(0o600)

        self.runtime = ReaderRuntime(
            self.docker,
            self.socket,
            self.state_root,
            OWNER_ID,
            DAEMON_ID,
            IMAGE_ID,
        )
        self.manifest_path = self.profile_root / "reader-runtime.json"
        self.write_manifest()

    def _write_limactl(self) -> None:
        record_path = self.root / "limactl-record.json"
        record_path.write_bytes(_canonical(self.record))
        record_path.chmod(0o600)
        self.limactl.write_text(
            "#!" + sys.executable + "\n"
            "import os,pathlib,sys\n"
            f"root={str(self.lima_home)!r}\n"
            f"home={str(Path.home())!r}\n"
            f"profile={_PROFILE!r}\n"
            f"record=pathlib.Path({str(record_path)!r})\n"
            "if os.environ.get('LIMA_HOME') != root:\n raise SystemExit(91)\n"
            "if os.environ.get('HOME') != home:\n raise SystemExit(93)\n"
            "if any(key in os.environ for key in "
            "('SSH_AUTH_SOCK','DOCKER_CONTEXT','LIMA_CONFIG','LIMA_SSH_PORT')):\n"
            " raise SystemExit(94)\n"
            "if sys.argv[1:] != ['list','--json',profile]:\n raise SystemExit(92)\n"
            "sys.stdout.buffer.write(record.read_bytes()+b'\\n')\n"
        )
        self.limactl.chmod(0o700)

    def write_record(self) -> None:
        (self.root / "limactl-record.json").write_bytes(_canonical(self.record))

    def manifest(self) -> dict[str, object]:
        directories = {
            self.root,
            self.lima_home,
            self.instance,
            self.lima_home / "_disks",
            self.disk_root,
            self.config_root,
            self.lima_home / "_networks",
            self.usernet_root,
            self.colima_home,
            self.profile_root,
            self.state_root,
            self.root / "cache",
            self.root / "cache" / "caches",
        }
        files = [
            *(_identity(path, hashed=False) for path in sorted(directories)),
            *(_identity(path, hashed=True) for path in self.static_paths),
            *(_identity(path, hashed=False) for path in self.mutable_paths),
            _identity(self.image_source, hashed=False),
        ]
        return {
            "format": "tedd-mac-reader-runtime",
            "version": 1,
            "private_runtime_root": str(self.root),
            "lima_home": str(self.lima_home),
            "profile": _PROFILE,
            "limactl": {
                "path": str(self.limactl),
                "version": "2.2.0",
                "identity": _identity(self.limactl, hashed=True),
            },
            "runtime": {
                "docker": str(self.runtime.docker),
                "socket": str(self.runtime.socket),
                "state_root": str(self.runtime.state_root),
                "owner_id": self.runtime.owner_id,
                "daemon_id": self.runtime.daemon_id,
                "image_id": self.runtime.image_id,
            },
            "effective_config_sha256": hashlib.sha256(_canonical(self.config)).hexdigest(),
            "files": files,
        }

    def write_manifest(self, document: dict[str, object] | None = None) -> None:
        self.manifest_path.write_bytes(
            _canonical(self.manifest() if document is None else document)
        )
        self.manifest_path.chmod(0o600)

    def close(self) -> None:
        self.listener.close()
        self.containerd_listener.close()
        for listener in self.usernet_listeners:
            listener.close()
        self.temporary.cleanup()


@unittest.skipIf(os.name == "nt", "Mac runtime bindings use Unix sockets")
class MacRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = MacRuntimeFixture()
        self.addCleanup(self.fixture.close)

    def test_socket_capability_refusal_removes_the_fixture_root(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix=".tedd-runtime-", dir=Path.home())
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with (
            mock.patch.object(tempfile, "TemporaryDirectory", return_value=temporary),
            mock.patch.object(socket, "socket", side_effect=PermissionError(1, "socket denied")),
            self.assertRaisesRegex(unittest.SkipTest, "socket denied"),
        ):
            MacRuntimeFixture()

        self.assertFalse(root.exists())

    def test_valid_manifest_loads_and_qualifies_the_exact_profile(self) -> None:
        binding = load_mac_runtime_binding(self.fixture.manifest_path)

        self.assertIsInstance(binding, MacRuntimeBinding)
        self.assertEqual(binding.runtime, self.fixture.runtime)
        with mock.patch.dict(
            os.environ,
            {
                "SSH_AUTH_SOCK": "/ambient/agent.sock",
                "DOCKER_CONTEXT": "ambient-context",
                "LIMA_CONFIG": "/ambient/lima.yaml",
                "LIMA_SSH_PORT": "2222",
            },
        ):
            binding.qualify(time.monotonic() + 5)

    def test_missing_or_invalid_home_is_a_typed_refusal(self) -> None:
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        for home in ("", str(self.fixture.root / "missing-home")):
            with (
                self.subTest(home=home),
                mock.patch.dict(os.environ, {"HOME": home}),
                self.assertRaises(SourceArchiveError),
            ):
                binding.qualify(time.monotonic() + 5)

    def test_missing_manifest_is_a_content_free_typed_failure(self) -> None:
        missing = self.fixture.root / "missing" / "reader-runtime.json"
        with self.assertRaises(SourceArchiveError) as raised:
            load_mac_runtime_binding(missing)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertNotIn(str(missing), str(raised.exception))

    def test_manifest_schema_is_closed_and_size_bounded(self) -> None:
        document = self.fixture.manifest()
        document["unexpected"] = True
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        self.fixture.manifest_path.write_bytes(b" " * (64 * 1024 + 1))
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_manifest_rejects_bool_version_and_malformed_identity_types(self) -> None:
        document = self.fixture.manifest()
        document["version"] = True
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        document = self.fixture.manifest()
        identity = cast(list[dict[str, object]], document["files"])[0]
        identity["sha256"] = "not-a-digest"
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        document = self.fixture.manifest()
        identity = cast(list[dict[str, object]], document["files"])[0]
        identity["path"] = str(self.fixture.base)
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        document = self.fixture.manifest()
        identity = cast(list[dict[str, object]], document["files"])[0]
        identity["type"] = []
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_manifest_json_rejects_duplicate_fields_and_nonfinite_numbers(self) -> None:
        self.fixture.manifest_path.write_bytes(b'{"format":"a","format":"b"}')
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        self.fixture.manifest_path.write_bytes(b'{"version":NaN}')
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_limactl_requires_hashed_executable_with_trusted_ancestry(self) -> None:
        document = self.fixture.manifest()
        limactl = cast(dict[str, object], document["limactl"])
        identity = cast(dict[str, object], limactl["identity"])
        identity["sha256"] = None
        self.fixture.write_manifest(document)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        self.fixture.write_manifest()
        self.fixture.limactl.chmod(0o600)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

        self.fixture.limactl.chmod(0o700)
        self.fixture.limactl.parent.chmod(0o777)
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_omitted_backing_disk_cannot_be_enrolled(self) -> None:
        document = self.fixture.manifest()
        files = cast(list[dict[str, object]], document["files"])
        document["files"] = [
            item for item in files if item["path"] != str(self.fixture.disk_root / "datadisk")
        ]
        self.fixture.write_manifest(document)

        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_changed_static_file_requires_reenrollment_but_mutable_size_does_not(self) -> None:
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        (self.fixture.instance / "lima.yaml").write_bytes(b"changed")
        with self.assertRaises(SourceArchiveError):
            binding.qualify(time.monotonic() + 5)

        self.fixture.write_manifest()
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        with (self.fixture.instance / "disk").open("ab") as handle:
            handle.write(b"mutable-growth")
        binding.qualify(time.monotonic() + 5)

    def test_changed_filesystem_identity_and_private_permissions_are_rejected(self) -> None:
        for mutation in ("symlink", "hardlink", "replaced", "world_writable"):
            with self.subTest(mutation=mutation):
                fixture = MacRuntimeFixture()
                self.addCleanup(fixture.close)
                binding = load_mac_runtime_binding(fixture.manifest_path)
                target = fixture.instance / "lima.yaml"
                if mutation == "symlink":
                    target.unlink()
                    target.symlink_to(fixture.instance / "colima.yaml")
                elif mutation == "hardlink":
                    os.link(target, fixture.root / "unexpected-hardlink")
                elif mutation == "replaced":
                    replacement = fixture.instance / "replacement.yaml"
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    self.assertNotEqual(target.stat().st_ino, replacement.stat().st_ino)
                    replacement.replace(target)
                else:
                    fixture.config_root.chmod(0o777)
                with self.assertRaises(SourceArchiveError):
                    binding.qualify(time.monotonic() + 5)

    @unittest.skipUnless(sys.platform == "darwin", "noowners is a Darwin mount flag")
    def test_noowners_mount_is_rejected(self) -> None:
        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_runtime._darwin_mount_flags",
                return_value=0x00200000,
            ),
            self.assertRaises(SourceArchiveError),
        ):
            load_mac_runtime_binding(self.fixture.manifest_path)

    def test_qualification_rejects_stopped_wrong_instance_and_changed_config(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("status", "Stopped"),
            ("dir", str(self.fixture.root / "other")),
        )
        for key, value in cases:
            with self.subTest(key=key):
                self.fixture.record[key] = value
                self.fixture.write_record()
                binding = load_mac_runtime_binding(self.fixture.manifest_path)
                with self.assertRaises(SourceArchiveError):
                    binding.qualify(time.monotonic() + 5)
                self.fixture.record[key] = (
                    "Running" if key == "status" else str(self.fixture.instance)
                )

        self.fixture.config["mounts"] = [{"location": str(Path.home())}]
        self.fixture.write_record()
        self.fixture.write_manifest()
        with self.assertRaises(SourceArchiveError):
            load_mac_runtime_binding(self.fixture.manifest_path).qualify(time.monotonic() + 5)

    def test_unhashable_config_values_fail_closed_with_matching_digest(self) -> None:
        invalid_values: tuple[object, ...] = ([], {})
        for memory in invalid_values:
            with self.subTest(memory=memory):
                fixture = MacRuntimeFixture()
                self.addCleanup(fixture.close)
                fixture.config["memory"] = memory
                fixture.write_record()
                fixture.write_manifest()
                with self.assertRaises(SourceArchiveError):
                    load_mac_runtime_binding(fixture.manifest_path).qualify(time.monotonic() + 5)

    def test_cancellation_and_deadline_stop_before_limactl(self) -> None:
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(SourceArchiveError):
            binding.qualify(time.monotonic() + 5, cancelled)
        with self.assertRaises(SourceArchiveError):
            binding.qualify(time.monotonic() - 1)

    def test_cancellation_and_deadline_interrupt_static_hash_loops(self) -> None:
        target = self.fixture.instance / "lima.yaml"
        target.write_bytes(b"x" * (256 * 1024))
        self.fixture.write_manifest()
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        real_read = os.read

        cancelled = threading.Event()
        cancel_reads = 0

        def cancel_after_read(fd: int, size: int) -> bytes:
            nonlocal cancel_reads
            chunk = real_read(fd, size)
            if chunk:
                cancel_reads += 1
                cancelled.set()
            return chunk

        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_runtime.os.read",
                side_effect=cancel_after_read,
            ),
            self.assertRaises(SourceArchiveError),
        ):
            binding.qualify(time.monotonic() + 5, cancelled)
        self.assertEqual(cancel_reads, 1)

        delayed_reads = 0

        def delayed_read(fd: int, size: int) -> bytes:
            nonlocal delayed_reads
            chunk = real_read(fd, size)
            if chunk:
                delayed_reads += 1
                time.sleep(0.02)
            return chunk

        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_runtime.os.read",
                side_effect=delayed_read,
            ),
            self.assertRaises(SourceArchiveError),
        ):
            binding.qualify(time.monotonic() + 0.01)
        self.assertEqual(delayed_reads, 1)

    def test_filesystem_failures_at_requalification_are_normalized(self) -> None:
        binding = load_mac_runtime_binding(self.fixture.manifest_path)
        with (
            mock.patch.object(Path, "lstat", side_effect=OSError("synthetic failure")),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            binding.qualify(time.monotonic() + 5)

        self.assertEqual(raised.exception.code, "source_unavailable")


if __name__ == "__main__":
    unittest.main()
