"""Stopped-profile entitlement tests; no Colima, Lima, or Docker process runs."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import socket
import threading
import time
import unittest
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tedd_knowledge_graph import source_reader_cold as cold
from tedd_knowledge_graph.source_contracts import SourceArchiveError, encode
from tedd_knowledge_graph.source_reader_cold import load_mac_cold_entitlement, validate_ssh_configs
from tedd_knowledge_graph.source_reader_jobs import ReaderJobs
from tedd_knowledge_graph.source_reader_policy import ReaderRuntime
from tedd_knowledge_graph.source_reader_process import ProcessResult

from tests.knowledge_graph.test_source_reader_cidata import (
    META,
    NETWORK_CONFIG,
    USER_DATA,
    synthetic_iso,
)
from tests.knowledge_graph.test_source_reader_runtime import MacRuntimeFixture, _identity


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hold_profile(root: Path, profile: str, entered: ProcessEvent, release: ProcessEvent) -> None:
    with cold.profile_locked(root, profile, time.monotonic() + 5, None):
        entered.set()
        release.wait(5)


def _hold_job(runtime: ReaderRuntime, entered: ProcessEvent, release: ProcessEvent) -> None:
    with ReaderJobs(runtime).locked(time.monotonic() + 5):
        entered.set()
        release.wait(5)


class ColdFixture(MacRuntimeFixture):
    def __init__(self) -> None:
        super().__init__()
        self.warm_manifest = self.manifest()
        for listener in (self.listener, self.containerd_listener, *self.usernet_listeners):
            listener.close()
        for path in (self.socket, self.containerd_socket, *self.usernet_sockets):
            path.unlink()
        self.store_root = self.colima_home / "_store"
        self.store_root.mkdir(mode=0o700)
        self.store = self.store_root / f"{self.instance.name}.json"
        self.store.write_bytes(_json({"disk_formatted": True, "disk_runtime": "docker"}))
        self.store.chmod(0o600)
        self.docker_config = self.root / "docker-config"
        self.docker_config.mkdir(mode=0o700)
        (self.instance / "cidata.iso").write_bytes(synthetic_iso())
        self.colima = self.root / "bin" / "colima"
        self.lima = self.root / "bin" / "lima"
        self.guest_agent = self.root / "bin" / "guestagent"
        for path in (self.colima, self.lima, self.guest_agent):
            path.write_bytes(b"#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
        self.templates = self.root / "share" / "lima" / "templates"
        for path in (self.root / "share", self.templates.parent, self.templates):
            path.mkdir(mode=0o700)
        self.default_template = self.templates / "default.yaml"
        self.default_template.write_bytes(b"images: []\nmounts: []\n")
        self.default_template.chmod(0o600)
        self.roster = self.profile_root / "profile-owners.json"
        self.roster.write_bytes(_json(self.roster_document()))
        self.roster.chmod(0o600)
        self.cold_manifest = self.profile_root / "reader-cold.json"
        self.write_cold_manifest()

    def cold_document(self) -> dict[str, object]:
        warm = self.warm_manifest
        generated = {
            self.instance / "cidata.iso",
            self.instance / "ssh.config",
            self.colima_home / "ssh_config",
        }
        sockets = {self.socket, self.containerd_socket, *self.usernet_sockets}
        files = [
            item
            for item in cast(list[dict[str, object]], warm["files"])
            if Path(cast(str, item["path"])) not in generated | sockets
        ]
        files.extend(
            _identity(path, hashed=path.is_file())
            for path in (
                self.root / "bin",
                self.store_root,
                self.store,
                self.docker_config,
                self.docker,
                self.lima,
                self.roster,
                self.root / "share",
                self.templates.parent,
                self.templates,
                self.default_template,
            )
        )
        for item in files:
            item["size"] = (
                Path(cast(str, item["path"])).stat().st_size if item["type"] == "regular" else None
            )
        return {
            "format": "tedd-mac-reader-cold",
            "version": 1,
            "private_runtime_root": str(self.root),
            "lima_home": str(self.lima_home),
            "profile": self.instance.name,
            "colima": {
                "path": str(self.colima),
                "version": "0.10.3",
                "identity": _identity(self.colima, hashed=True),
            },
            "limactl": warm["limactl"],
            "guest_agent": {
                "path": str(self.guest_agent),
                "version": "2.2.0",
                "identity": _identity(self.guest_agent, hashed=True),
            },
            "runtime": warm["runtime"],
            "effective_config_sha256": warm["effective_config_sha256"],
            "persistent_files": files,
            "store_semantic_sha256": _sha(encode(json.loads(self.store.read_bytes()))),
            "docker_config_state": {
                "directory": str(self.docker_config),
                "present": False,
                "sha256": None,
                "current_context": None,
            },
            "profile_owners_sha256": _sha(self.roster.read_bytes()),
            "cidata_entries": {
                "etc/guestagent": _sha(b"trusted guest agent"),
                "user-data": _sha(USER_DATA),
                "network-config": _sha(NETWORK_CONFIG),
            },
            "meta_data_normalized_sha256": _sha(b"instance-id: <iid>\nlocal-hostname: synthetic\n"),
            "lima_env_normalized_sha256": _sha(
                b"LIMA_CIDATA_IID=<iid>\nLIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\nLIMA_CIDATA_TCP_DNS_LOCAL_PORT=0\n"
            ),
            "ssh_config_normalized_sha256": _sha(
                b"Host lima-colima-tedd-reader-spike\n"
                b"  HostName 127.0.0.1\n  Port <port>\n  User synthetic\n"
            ),
        }

    def roster_document(self) -> dict[str, object]:
        return {
            "profile": self.instance.name,
            "version": 1,
            "cutover_generation": "11111111-1111-4111-8111-111111111111",
            "owners": [self.warm_manifest["runtime"]],
        }

    def write_cold_manifest(self, document: dict[str, object] | None = None) -> None:
        self.cold_manifest.write_bytes(
            _json(self.cold_document() if document is None else document)
        )
        self.cold_manifest.chmod(0o600)


@unittest.skipIf(os.name == "nt", "Mac stopped-profile entitlements use POSIX identities")
class ColdEntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ColdFixture()
        self.addCleanup(self.fixture.close)

    def load(self) -> None:
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(entitlement.runtime, self.fixture.runtime)
        entitlement.check_persistent(time.monotonic() + 5, None)

    def test_stopped_profile_loads_without_sockets(self) -> None:
        self.assertFalse((self.fixture.instance / "user-data").exists())
        self.assertFalse((self.fixture.instance / "network-config").exists())
        self.load()

    def test_recreated_private_network_directories_load_without_reenrollment(self) -> None:
        networks = self.fixture.lima_home / "_networks"
        original = self.fixture.cold_manifest.read_bytes()
        old_inode = networks.stat().st_ino
        networks.rename(self.fixture.root / "previous-networks")
        networks.mkdir(mode=0o700)
        (networks / "user-v2").mkdir(mode=0o700)

        self.load()

        self.assertNotEqual(networks.stat().st_ino, old_inode)
        self.assertEqual(self.fixture.cold_manifest.read_bytes(), original)

    def test_recreated_network_directory_rejects_broad_mode_and_symlink(self) -> None:
        networks = self.fixture.lima_home / "_networks"
        displaced = self.fixture.root / "previous-networks"
        networks.rename(displaced)
        networks.mkdir(mode=0o700)
        (networks / "user-v2").mkdir(mode=0o700)
        networks.chmod(0o755)
        with self.assertRaises(SourceArchiveError) as raised:
            load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(raised.exception.code, "privacy_rejected")
        networks.chmod(0o700)
        (networks / "user-v2").rmdir()
        networks.rmdir()
        networks.symlink_to(displaced, target_is_directory=True)
        with self.assertRaises(SourceArchiveError) as raised:
            load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(raised.exception.code, "privacy_rejected")

    def test_stopped_rewritten_cloud_config_requires_exact_content_not_old_inode(self) -> None:
        config = self.fixture.instance / "cloud-config.yaml"
        raw = config.read_bytes()
        config.rename(config.with_suffix(".previous"))
        config.write_bytes(raw)
        config.chmod(0o600)
        self.load()
        config.write_bytes(b"unreviewed cloud config")
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_required_descriptor_flags_refuse_before_open_if_unavailable(self) -> None:
        for flag in ("O_NONBLOCK", "O_CLOEXEC"):
            with (
                self.subTest(flag=flag),
                patch.object(cold.os, flag, None),
                patch.object(cold.os, "open", autospec=True) as opened,
            ):
                with self.assertRaises(SourceArchiveError) as raised:
                    cold._repair_private_generated(
                        self.fixture.lima_home / "_networks", self.fixture.root, directory=True
                    )
                self.assertEqual(raised.exception.code, "capability_unavailable")
                opened.assert_not_called()

    def test_missing_template_inventory_refuses_enrollment(self) -> None:
        original = self.fixture.cold_document()
        inventory = cast(list[dict[str, object]], original["persistent_files"])
        for relative in (
            "share",
            "share/lima",
            "share/lima/templates",
            "share/lima/templates/default.yaml",
        ):
            with self.subTest(path=relative):
                path = self.fixture.root / relative
                files = [item for item in inventory if item["path"] != str(path)]
                self.fixture.write_cold_manifest({**original, "persistent_files": files})
                with self.assertRaises(SourceArchiveError):
                    self.load()

    def test_cold_manifest_requires_both_iso_cloud_init_hashes(self) -> None:
        for name in ("user-data", "network-config"):
            for value in (None, "not-a-hash"):
                with self.subTest(name=name, value=value):
                    document = self.fixture.cold_document()
                    entries = cast(dict[str, str], document["cidata_entries"])
                    if value is None:
                        del entries[name]
                    else:
                        entries[name] = value
                    self.fixture.write_cold_manifest(document)
                    with self.assertRaises(SourceArchiveError):
                        load_mac_cold_entitlement(self.fixture.cold_manifest)

    def test_persistent_inventory_still_rejects_missing_static_or_extra_sibling(self) -> None:
        original = self.fixture.cold_document()
        files = cast(list[dict[str, object]], original["persistent_files"])
        sibling = self.fixture.instance / "user-data"
        sibling.write_bytes(USER_DATA)
        sibling.chmod(0o600)
        extra = {**_identity(sibling, hashed=True), "size": len(USER_DATA)}
        missing = [
            item
            for item in files
            if item["path"] != str(self.fixture.instance / "cloud-config.yaml")
        ]
        for name, inventory in (("missing static", missing), ("extra sibling", [*files, extra])):
            with self.subTest(name=name):
                self.fixture.write_cold_manifest({**original, "persistent_files": inventory})
                with self.assertRaises(SourceArchiveError):
                    load_mac_cold_entitlement(self.fixture.cold_manifest)

    def test_missing_or_replaced_storage_and_store_refuse(self) -> None:
        for path in (
            self.fixture.instance / "disk",
            self.fixture.disk_root / "datadisk",
            self.fixture.instance / "vz-efi",
            self.fixture.image_source,
            self.fixture.store,
        ):
            with self.subTest(path=path.name):
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaises(SourceArchiveError):
                    self.load()
                path.write_bytes(saved)
                path.chmod(0o600)
                self.fixture.write_cold_manifest()

    def test_in_place_disk_resize_refuses(self) -> None:
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        disk = self.fixture.instance / "disk"
        disk.write_bytes(disk.read_bytes() + b"additional bytes")
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, None)

    def test_zero_length_disk_cannot_be_enrolled(self) -> None:
        (self.fixture.instance / "disk").write_bytes(b"")
        self.fixture.write_cold_manifest()
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_fresh_loader_requires_valid_matching_semantic_pin(self) -> None:
        original = json.loads(self.fixture.cold_manifest.read_bytes())
        missing = {key: value for key, value in original.items() if key != "store_semantic_sha256"}
        invalid = (
            None,
            True,
            1,
            "",
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            "0" * 64,
        )
        for document in (missing, *({**original, "store_semantic_sha256": pin} for pin in invalid)):
            with self.subTest(pin=document.get("store_semantic_sha256", "missing")):
                manifest = _json(document)
                self.fixture.cold_manifest.write_bytes(manifest)
                with patch(
                    "tedd_knowledge_graph.source_reader_cold.run_bounded",
                    side_effect=AssertionError("fresh loader reached a process"),
                ):
                    with self.assertRaises(SourceArchiveError):
                        load_mac_cold_entitlement(self.fixture.cold_manifest)
                self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)

    def test_fresh_loader_authenticates_complete_store_mapping_without_learning(self) -> None:
        baseline = {"disk_formatted": True, "disk_runtime": "docker", "reviewed": {"revision": 1}}
        self.fixture.store.write_bytes(_json(baseline))
        self.fixture.write_cold_manifest()
        manifest = self.fixture.cold_manifest.read_bytes()
        for mapping in (
            {**baseline, "extra": 1},
            {**baseline, "reviewed": {"revision": 2}},
            {"disk_formatted": True, "disk_runtime": "docker"},
            {**baseline, "disk_formatted": False},
            {**baseline, "disk_formatted": 1},
            {**baseline, "disk_runtime": "containerd"},
            {"disk_runtime": "docker", "reviewed": {"revision": 1}},
            {"disk_formatted": True, "reviewed": {"revision": 1}},
        ):
            with self.subTest(mapping=mapping):
                self.fixture.store.write_bytes(_json(mapping))
                with patch(
                    "tedd_knowledge_graph.source_reader_cold.run_bounded",
                    side_effect=AssertionError("fresh loader reached a process"),
                ):
                    with self.assertRaises(SourceArchiveError):
                        load_mac_cold_entitlement(self.fixture.cold_manifest)
                self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
        self.fixture.store.write_bytes(_json(baseline))
        fresh = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(fresh.runtime, self.fixture.runtime)
        self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)

    def test_fresh_loader_rejects_nonprivate_or_symlinked_store(self) -> None:
        manifest = self.fixture.cold_manifest.read_bytes()
        self.fixture.store.chmod(0o644)
        with self.assertRaises(SourceArchiveError):
            load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.fixture.store.chmod(0o600)
        saved = self.fixture.store.with_suffix(".saved")
        self.fixture.store.rename(saved)
        self.fixture.store.symlink_to(saved)
        with self.assertRaises(SourceArchiveError):
            load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)

    def test_fresh_loader_rejects_changed_config_bytes(self) -> None:
        manifest = self.fixture.cold_manifest.read_bytes()
        for config in (
            self.fixture.instance / "lima.yaml",
            self.fixture.instance / "colima.yaml",
            self.fixture.instance / "cloud-config.yaml",
            self.fixture.profile_root / "colima.yaml",
        ):
            with self.subTest(config=config):
                original = config.read_bytes()
                replacement = config.with_suffix(".same")
                replacement.write_bytes(b"X" + original[1:])
                replacement.chmod(0o600)
                replacement.replace(config)
                with self.assertRaises(SourceArchiveError):
                    load_mac_cold_entitlement(self.fixture.cold_manifest)
                self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
                config.write_bytes(original)

    def test_fresh_loader_rejects_replaced_immutable_tools_and_backing(self) -> None:
        manifest = self.fixture.cold_manifest.read_bytes()
        for path in (
            self.fixture.colima,
            self.fixture.limactl,
            self.fixture.guest_agent,
            self.fixture.docker,
            self.fixture.instance / "disk",
            self.fixture.disk_root / "datadisk",
        ):
            with self.subTest(path=path.name):
                saved = path.with_suffix(".saved")
                path.rename(saved)
                path.write_bytes(saved.read_bytes())
                path.chmod(saved.stat().st_mode & 0o777)
                try:
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded",
                        side_effect=AssertionError("fresh loader reached a process"),
                    ):
                        with self.assertRaises(SourceArchiveError):
                            load_mac_cold_entitlement(self.fixture.cold_manifest)
                    self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_bad_store_and_docker_config_state_refuse(self) -> None:
        for payload in (
            b"{}",
            _json({"disk_formatted": False, "disk_runtime": "docker"}),
            _json({"disk_formatted": True, "disk_runtime": "containerd"}),
        ):
            with self.subTest(payload=payload):
                self.fixture.store.write_bytes(payload)
                self.fixture.write_cold_manifest()
                with self.assertRaises(SourceArchiveError):
                    self.load()
        self.fixture.store.write_bytes(_json({"disk_formatted": True, "disk_runtime": "docker"}))
        self.fixture.write_cold_manifest()
        config = self.fixture.docker_config / "config.json"
        config.write_bytes(b"{}")
        config.chmod(0o600)
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_explicit_present_docker_config_is_pinned(self) -> None:
        config = self.fixture.docker_config / "config.json"
        config.write_bytes(_json({"currentContext": "private"}))
        config.chmod(0o600)
        document = self.fixture.cold_document()
        document["docker_config_state"] = {
            "directory": str(self.fixture.docker_config),
            "present": True,
            "sha256": _sha(config.read_bytes()),
            "current_context": "private",
        }
        self.fixture.write_cold_manifest(document)
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        config.write_bytes(_json({"currentContext": "other"}))
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, None)

    def test_postwrite_store_allows_only_same_mapping(self) -> None:
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        replacement = self.fixture.store.with_suffix(".new")
        replacement.write_bytes(b'{ "disk_runtime": "docker", "disk_formatted": true }')
        replacement.chmod(0o600)
        replacement.replace(self.fixture.store)
        entitlement.check_persistent(time.monotonic() + 5, None, post_start=True)
        self.fixture.store.write_bytes(
            _json({"disk_formatted": True, "disk_runtime": "docker", "extra": 1})
        )
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, None, post_start=True)

    def test_poststart_replaced_config_requires_exact_bytes(self) -> None:
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        config = self.fixture.instance / "lima.yaml"
        replacement = config.with_suffix(".new")
        replacement.write_bytes(config.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(config)
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, None)
        entitlement.check_persistent(time.monotonic() + 5, None, post_start=True)
        config.write_bytes(b"changed config")
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, None, post_start=True)

    def test_unknown_field_and_tool_change_refuse(self) -> None:
        document = self.fixture.cold_document()
        document["unexpected"] = True
        self.fixture.write_cold_manifest(document)
        with self.assertRaises(SourceArchiveError):
            self.load()
        self.fixture.write_cold_manifest()
        self.fixture.colima.write_bytes(b"changed")
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_nonexecutable_tool_refuses_even_when_enrolled(self) -> None:
        self.fixture.colima.chmod(0o600)
        self.fixture.write_cold_manifest()
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_symlink_permission_and_cancel_refuse(self) -> None:
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(SourceArchiveError):
            entitlement.check_persistent(time.monotonic() + 5, cancelled)
        disk = self.fixture.instance / "disk"
        disk.chmod(0o644)
        with self.assertRaises(SourceArchiveError):
            self.load()
        disk.chmod(0o600)
        disk.unlink()
        disk.symlink_to(self.fixture.image_source)
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_roster_is_closed_finite_and_pins_selected_runtime(self) -> None:
        original = self.fixture.roster_document()
        owner = cast(list[dict[str, object]], original["owners"])[0]
        for altered in (
            {**original, "unexpected": True},
            {**original, "owners": []},
            {**original, "owners": [owner] * 2},
            {**original, "owners": [{**owner, "image_id": "sha256:" + "b" * 64}]},
        ):
            with self.subTest(altered=altered):
                self.fixture.roster.write_bytes(_json(altered))
                self.fixture.write_cold_manifest()
                with self.assertRaises(SourceArchiveError):
                    self.load()

    def test_two_owner_roster_is_data_not_fixed_count(self) -> None:
        other_root = self.fixture.root / "reader-state-2"
        other_root.mkdir(mode=0o700)
        roster = self.fixture.roster_document()
        owner = cast(list[dict[str, object]], roster["owners"])[0]
        other = {
            **owner,
            "state_root": str(other_root),
            "owner_id": "22222222-2222-4222-8222-222222222222",
        }
        roster["owners"] = [owner, other]
        self.fixture.roster.write_bytes(_json(roster))
        self.fixture.write_cold_manifest()
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.assertEqual(len(entitlement.profile_owners), 2)
        roster["owners"] = [other, owner]
        self.fixture.roster.write_bytes(_json(roster))
        self.fixture.write_cold_manifest()
        with self.assertRaises(SourceArchiveError):
            self.load()

    def test_generated_ssh_comments_case_and_aggregate_newline(self) -> None:
        # Lima 2.2.0 writeSSHConfigFile; all values here are synthetic.
        template = (
            b"# Synthetic SSH config.\n# Created by Lima.\n# Rewritten at boot.\n"
            b"Host lima-colima-tedd-reader-spike\n"
            b'  IdentityFile "/synthetic/lima/_config/user"\n'
            b"  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n"
            b"  NoHostAuthenticationForLocalhost yes\n"
            b"  PreferredAuthentications publickey\n  Compression no\n"
            b"  BatchMode yes\n  IdentitiesOnly yes\n  GSSAPIAuthentication no\n"
            b"  Ciphers ^aes128-gcm@openssh.com,aes256-gcm@openssh.com\n"
            b"  User synthetic\n  ControlMaster auto\n"
            b'  ControlPath "/synthetic/lima/instance/ssh.sock"\n'
            b"  ControlPersist yes\n  Hostname 127.0.0.1\n  Port 4321\n"
        )
        lima = self.fixture.instance / "ssh.config"
        aggregate = self.fixture.colima_home / "ssh_config"
        baseline = _sha(template.replace(b"Port 4321", b"Port <port>"))

        def write(content: bytes) -> None:
            lima.write_bytes(content)
            # Colima 0.10.3 replaceSSHConfig preserves comments; generateSSHConfig
            # uses Fprintln on its already newline-terminated output.
            aggregate.write_bytes(
                content.replace(
                    b"Host lima-colima-tedd-reader-spike\n",
                    b"Host colima-tedd-reader-spike\n",
                    1,
                )
                + b"\n"
            )
            for path in (lima, aggregate):
                path.chmod(0o600)

        for port in (4321, 5432):
            write(template.replace(b"Port 4321", f"Port {port}".encode()))
            validate_ssh_configs(
                lima,
                aggregate,
                "colima-tedd-reader-spike",
                port,
                baseline,
                time.monotonic() + 5,
                None,
            )
        for label, content, enrolled in (
            ("comment", template.replace(b"Synthetic", b"Changed"), False),
            ("key path", template.replace(b"/_config/user", b"/_config/other"), False),
            ("hostname", template.replace(b"127.0.0.1", b"0.0.0.0"), True),
            ("port mismatch", template.replace(b"Port 4321", b"Port 4322"), True),
            ("second port", template + b"  Port 4321\n", True),
            ("second host", template + b"Host other\n", True),
            ("wrong host", template.replace(b"Host lima-colima", b"Host other-colima"), True),
            ("proxy", template + b"  ProxyCommand unexpected\n", True),
            ("include equals", template + b"  Include=/synthetic/other\n", True),
            ("match", template + b"Match all\n", True),
        ):
            with self.subTest(label=label):
                write(content)
                pinned = (
                    _sha(content.replace(b"Port 4321", b"Port <port>")) if enrolled else baseline
                )
                with self.assertRaises(SourceArchiveError):
                    validate_ssh_configs(
                        lima,
                        aggregate,
                        "colima-tedd-reader-spike",
                        4321,
                        pinned,
                        time.monotonic() + 5,
                        None,
                    )
        write(template)
        for bad in (
            aggregate.read_bytes().removesuffix(b"\n"),
            aggregate.read_bytes() + b"\n",
            aggregate.read_bytes() + b"Host other\n",
            aggregate.read_bytes().replace(b"# Synthetic", b"# Changed"),
        ):
            aggregate.write_bytes(bad)
            with self.assertRaises(SourceArchiveError):
                validate_ssh_configs(
                    lima,
                    aggregate,
                    "colima-tedd-reader-spike",
                    4321,
                    baseline,
                    time.monotonic() + 5,
                    None,
                )


@unittest.skipIf(os.name == "nt", "Mac cold start uses POSIX sockets")
class ColdStarterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ColdFixture()
        self.addCleanup(self.fixture.close)
        self.entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.receipt = self.fixture.profile_root / "profile-start.json"
        self.receipt.write_bytes(_json(self.idle()))
        self.receipt.chmod(0o600)
        ReaderJobs(self.fixture.runtime)
        self.profile_lock = self.fixture.profile_root / "profile-start.lock"
        self.profile_lock.write_bytes(b"")
        self.profile_lock.chmod(0o600)
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.sockets: list[socket.socket] = []
        self.addCleanup(self.close_sockets)

    def close_sockets(self) -> None:
        for listener in self.sockets:
            listener.close()

    def idle(self) -> dict[str, object]:
        runtime = self.fixture.runtime
        return {
            "version": 1,
            "profile": self.fixture.instance.name,
            "roster_sha256": _sha(self.fixture.roster.read_bytes()),
            "cold_entitlement_sha256": _sha(self.fixture.cold_manifest.read_bytes()),
            "owner_id": runtime.owner_id,
            "state_root": str(runtime.state_root),
            "daemon_id": runtime.daemon_id,
            "image_id": runtime.image_id,
            "phase": "idle",
            "attempt": None,
            "cutover_generation": self.fixture.roster_document()["cutover_generation"],
        }

    def generated(self) -> None:
        fixture = self.fixture
        iso = fixture.instance / "cidata.iso"
        iso.write_bytes(synthetic_iso())
        iso.chmod(0o600)
        ssh = fixture.instance / "ssh.config"
        ssh.write_bytes(
            b"Host lima-colima-tedd-reader-spike\n"
            b"  HostName 127.0.0.1\n  Port 4321\n  User synthetic\n"
        )
        ssh.chmod(0o600)
        aggregate = fixture.colima_home / "ssh_config"
        aggregate.write_bytes(
            ssh.read_bytes().replace(
                b"Host lima-colima-tedd-reader-spike", b"Host colima-tedd-reader-spike", 1
            )
            + b"\n"
        )
        aggregate.chmod(0o600)
        for path in (fixture.socket, fixture.containerd_socket, *fixture.usernet_sockets):
            listener = socket.socket(socket.AddressFamily["AF_UNIX"])
            listener.bind(str(path))
            path.chmod(0o600)
            self.sockets.append(listener)
        name = fixture.instance.name
        context_id = _sha(name.encode())
        meta = fixture.docker_config / "contexts" / "meta" / context_id
        meta.mkdir(parents=True, mode=0o700)
        for directory in (meta.parent.parent, meta.parent, meta):
            directory.chmod(0o755)
        metadata = {
            "Name": name,
            "Metadata": {"Description": "colima [profile=tedd-reader-spike]"},
            "Endpoints": {
                "docker": {"Host": "unix://" + str(fixture.socket), "SkipTLSVerify": False}
            },
        }
        (meta / "meta.json").write_bytes(_json(metadata))
        (meta / "meta.json").chmod(0o644)

    def fake_process(
        self, arguments: tuple[str, ...], payload: bytes, **kwargs: object
    ) -> ProcessResult:
        self.calls.append((arguments, cast(dict[str, str], kwargs["environment"])))
        if arguments[1:4] == ("list", "--json", self.fixture.instance.name):
            return ProcessResult(0, _json(self.fixture.record))
        if arguments[0] == str(self.fixture.colima):
            self.assertEqual(self.receipt_json()["phase"], "starting")
            self.generated()
            self.fixture.record["status"] = "Running"
            self.fixture.record["sshLocalPort"] = 4321
            return ProcessResult(0, b"")
        self.fail(f"unexpected process: {arguments}")

    def receipt_json(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.receipt.read_bytes()))

    def test_only_cold_start_requests_private_child_creation_mask(self) -> None:
        self.fixture.record["status"] = "Stopped"
        with (
            patch(
                "tedd_knowledge_graph.source_reader_cold.run_bounded",
                autospec=True,
                side_effect=self.fake_process,
            ) as process,
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            self.entitlement.ensure_ready(time.monotonic() + 5, None)
        starts = [
            call for call in process.call_args_list if call.args[0][0] == str(self.fixture.colima)
        ]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].kwargs.get("umask"), 0o077)
        self.assertTrue(
            all("umask" not in call.kwargs for call in process.call_args_list if call not in starts)
        )
        self.assertEqual(self.receipt_json(), self.idle())

    def test_session_rejects_network_parent_replacement_with_same_socket_inodes(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        original_sockets = tuple(path.stat().st_ino for path in self.fixture.usernet_sockets)
        with (
            patch(
                "tedd_knowledge_graph.source_reader_cold.run_bounded",
                autospec=True,
                side_effect=self.fake_process,
            ),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            networks = self.fixture.lima_home / "_networks"
            displaced = self.fixture.root / "previous-networks"
            networks.rename(displaced)
            networks.mkdir(mode=0o700)
            (displaced / "user-v2").rename(networks / "user-v2")
            self.assertEqual(
                tuple(path.stat().st_ino for path in self.fixture.usernet_sockets), original_sockets
            )
            with self.assertRaises(SourceArchiveError):
                session.qualify(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json(), self.idle())

    def test_cold_start_rebinds_recreated_networks_and_exact_cloud_config(self) -> None:
        self.fixture.record["status"] = "Stopped"
        manifest = self.fixture.cold_manifest.read_bytes()
        networks = self.fixture.lima_home / "_networks"
        original_inode = networks.stat().st_ino
        config = self.fixture.instance / "cloud-config.yaml"

        def process(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            if arguments[0] == str(self.fixture.colima):
                networks.rename(self.fixture.root / "previous-networks")
                networks.mkdir(mode=0o700)
                (networks / "user-v2").mkdir(mode=0o700)
                saved = config.with_suffix(".previous")
                config.rename(saved)
                config.write_bytes(saved.read_bytes())
                config.chmod(0o600)
            return self.fake_process(arguments, payload, **kwargs)

        with (
            patch(
                "tedd_knowledge_graph.source_reader_cold.run_bounded",
                autospec=True,
                side_effect=process,
            ),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            session.qualify(time.monotonic() + 5, None)
        self.assertNotEqual(networks.stat().st_ino, original_inode)
        self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
        self.assertEqual(self.receipt_json(), self.idle())

    def test_profile_lock_serializes_processes_and_honors_deadline_and_cancel(self) -> None:
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        process = context.Process(  # type: ignore[attr-defined]  # typeshed omits BaseContext factories
            target=_hold_profile,
            args=(self.fixture.profile_root, self.fixture.instance.name, entered, release),
        )
        process.start()
        try:
            self.assertTrue(entered.wait(3), "lock holder did not enter")
            with self.assertRaises(SourceArchiveError):
                with cold.profile_locked(
                    self.fixture.profile_root,
                    self.fixture.instance.name,
                    time.monotonic() + 0.05,
                    None,
                ):
                    self.fail("second process acquired the held profile lock")
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(SourceArchiveError):
                with cold.profile_locked(
                    self.fixture.profile_root,
                    self.fixture.instance.name,
                    time.monotonic() + 1,
                    cancelled,
                ):
                    self.fail("cancelled holder acquired the profile lock")
        finally:
            release.set()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(3)
        self.assertEqual(process.exitcode, 0)
        with cold.profile_locked(
            self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 1, None
        ):
            self.assertTrue(lock.exists())

    def test_profile_lock_releases_local_guard_after_unlock_failure(self) -> None:
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        with patch.object(cold, "unlock_file", side_effect=OSError("synthetic unlock failure")):
            with self.assertRaises(SourceArchiveError):
                with cold.profile_locked(
                    self.fixture.profile_root,
                    self.fixture.instance.name,
                    time.monotonic() + 1,
                    None,
                ):
                    pass
        try:
            with cold.profile_locked(
                self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 0.2, None
            ):
                self.assertTrue(lock.exists())
        except SourceArchiveError:
            self.fail("failed unlock retained the local profile guard")

    def test_profile_admission_refuses_missing_job_pair_without_initializing_it(self) -> None:
        authority = self.entitlement.profile_authority()
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        job_lock = self.fixture.state_root / "reader-job.lock"
        journal = self.fixture.state_root / "reader-job.json"
        job_lock.unlink()
        journal.unlink()
        with cold.profile_locked(
            self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 2, None
        ):
            with self.assertRaises(SourceArchiveError):
                authority.preflight_jobs(time.monotonic() + 1, None)
        self.assertFalse(job_lock.exists())
        self.assertFalse(journal.exists())

    def test_legacy_held_job_lock_blocks_profile_admission(self) -> None:
        context = multiprocessing.get_context("fork")
        entered = context.Event()
        release = context.Event()
        process = context.Process(  # type: ignore[attr-defined]  # typeshed omits BaseContext factories
            target=_hold_job, args=(self.fixture.runtime, entered, release)
        )
        process.start()
        try:
            self.assertTrue(entered.wait(3), "legacy job holder did not enter")
            with cold.profile_locked(
                self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 1, None
            ):
                with self.assertRaises(SourceArchiveError):
                    self.entitlement.profile_authority().preflight_jobs(
                        time.monotonic() + 0.05, None
                    )
        finally:
            release.set()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(3)
        self.assertEqual(process.exitcode, 0)

    def test_other_owner_pending_job_blocks_profile_admission(self) -> None:
        other_root = self.fixture.root / "reader-state-2"
        other_root.mkdir(mode=0o700)
        owner = cast(dict[str, object], self.fixture.warm_manifest["runtime"])
        other = {
            **owner,
            "state_root": str(other_root),
            "owner_id": "22222222-2222-4222-8222-222222222222",
        }
        roster = self.fixture.roster_document()
        roster["owners"] = [owner, other]
        self.fixture.roster.write_bytes(_json(roster))
        self.fixture.write_cold_manifest()
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.receipt.write_bytes(_json(self.idle()))
        other_runtime = ReaderRuntime(
            self.fixture.docker,
            self.fixture.socket,
            other_root,
            cast(str, other["owner_id"]),
            self.fixture.runtime.daemon_id,
            self.fixture.runtime.image_id,
        )
        ReaderJobs(self.fixture.runtime)
        other_jobs = ReaderJobs(other_runtime)
        with other_jobs.locked(time.monotonic() + 2):
            other_jobs.pending("33333333-3333-4333-8333-333333333333")
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        with cold.profile_locked(
            self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 2, None
        ):
            self.assertEqual(
                entitlement.profile_authority().read_receipt(time.monotonic() + 1, None)["phase"],
                "idle",
            )
            with self.assertRaises(SourceArchiveError):
                entitlement.profile_authority().preflight_jobs(time.monotonic() + 1, None)
        with other_jobs.locked(time.monotonic() + 2):
            self.assertEqual(other_jobs.receipts()[0]["phase"], "pending")
        starting = {
            **self.idle(),
            "phase": "starting",
            "attempt": "44444444-4444-4444-8444-444444444444",
        }
        self.receipt.write_bytes(_json(starting))
        with cold.profile_locked(
            self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 2, None
        ):
            with self.assertRaises(SourceArchiveError):
                entitlement.reconcile_start(time.monotonic() + 1, None)
        self.assertEqual(self.receipt_json(), starting)

    def test_new_holder_reconciles_running_start_without_second_launch(self) -> None:
        ReaderJobs(self.fixture.runtime)
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        self.generated()
        self.fixture.record["status"] = "Running"
        self.fixture.record["sshLocalPort"] = 4321
        attempt = "44444444-4444-4444-8444-444444444444"
        self.receipt.write_bytes(_json({**self.idle(), "phase": "starting", "attempt": attempt}))
        fresh = load_mac_cold_entitlement(self.fixture.cold_manifest)
        with (
            cold.profile_locked(
                self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 5, None
            ),
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            fresh.reconcile_start(time.monotonic() + 4, None)
        self.assertEqual(self.receipt_json(), self.idle())
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_stopped_after_interrupted_start_retains_obligation_without_relaunch(self) -> None:
        ReaderJobs(self.fixture.runtime)
        lock = self.fixture.profile_root / "profile-start.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        self.fixture.record["status"] = "Stopped"
        starting = {
            **self.idle(),
            "phase": "starting",
            "attempt": "55555555-5555-4555-8555-555555555555",
        }
        self.receipt.write_bytes(_json(starting))
        fresh = load_mac_cold_entitlement(self.fixture.cold_manifest)
        with (
            cold.profile_locked(
                self.fixture.profile_root, self.fixture.instance.name, time.monotonic() + 5, None
            ),
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            self.assertRaises(SourceArchiveError),
        ):
            fresh.reconcile_start(time.monotonic() + 4, None)
        self.assertEqual(self.receipt_json(), starting)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_cold_start_refuses_other_owner_pending_job_before_dispatch(self) -> None:
        other_root = self.fixture.root / "reader-state-2"
        other_root.mkdir(mode=0o700)
        owner = cast(dict[str, object], self.fixture.warm_manifest["runtime"])
        other = {
            **owner,
            "state_root": str(other_root),
            "owner_id": "22222222-2222-4222-8222-222222222222",
        }
        roster = self.fixture.roster_document()
        roster["owners"] = [owner, other]
        self.fixture.roster.write_bytes(_json(roster))
        self.fixture.write_cold_manifest()
        self.receipt.write_bytes(_json(self.idle()))
        other_runtime = ReaderRuntime(
            self.fixture.docker,
            self.fixture.socket,
            other_root,
            cast(str, other["owner_id"]),
            self.fixture.runtime.daemon_id,
            self.fixture.runtime.image_id,
        )
        other_jobs = ReaderJobs(other_runtime)
        with other_jobs.locked(time.monotonic() + 2):
            other_jobs.pending("33333333-3333-4333-8333-333333333333")
        entitlement = load_mac_cold_entitlement(self.fixture.cold_manifest)
        self.fixture.record["status"] = "Stopped"
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
            self.assertRaises(SourceArchiveError),
        ):
            entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json(), self.idle())
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_stopped_profile_dispatches_exact_resume_after_durable_starting_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"
        with (
            patch(
                "tedd_knowledge_graph.source_reader_cold.run_bounded",
                self.fake_process,
                create=True,
            ),
            patch(
                "tedd_knowledge_graph.source_reader_cold._preflight", return_value=None, create=True
            ),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json(), self.idle())
        self.assertEqual([call[0][0] for call in self.calls].count(str(self.fixture.colima)), 1)
        start = next(call for call in self.calls if call[0][0] == str(self.fixture.colima))
        self.assertEqual(
            start[0],
            (
                str(self.fixture.colima),
                "--profile",
                "tedd-reader-spike",
                "start",
                "--save-config=false",
                "--template=false",
                "--activate=false",
                "--ssh-config=false",
                "--ssh-agent=false",
                "--mount",
                "none",
                "--binfmt=false",
                "--vz-rosetta=false",
                "--port-forwarder",
                "none",
            ),
        )
        self.assertEqual(
            start[1],
            {
                "PATH": str(self.fixture.root / "bin") + ":/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "HOME": str(self.fixture.root),
                "TMPDIR": str(self.fixture.root),
                "COLIMA_HOME": str(self.fixture.colima_home),
                "COLIMA_CACHE_HOME": str(self.fixture.root / "cache"),
                "LIMA_HOME": str(self.fixture.lima_home),
                "DOCKER_CONFIG": str(self.fixture.docker_config),
            },
        )
        self.assertIsNotNone(session)
        meta = next(self.fixture.docker_config.glob("contexts/meta/*/meta.json"))
        self.assertEqual(meta.stat().st_mode & 0o777, 0o600)
        self.assertEqual(meta.parent.stat().st_mode & 0o777, 0o700)

    def test_missing_lima_refuses_before_starting_receipt_or_dispatch(self) -> None:
        (self.fixture.root / "bin" / "lima").unlink(missing_ok=True)
        self.assert_lima_refuses_before_start()

    def test_missing_default_template_refuses_before_start(self) -> None:
        self.fixture.default_template.unlink()
        self.assert_lima_refuses_before_start()

    def test_changed_default_template_refuses_before_start(self) -> None:
        original = self.fixture.default_template.read_bytes()
        self.fixture.default_template.write_bytes(original.replace(b"images", b"IMAGES"))
        self.assert_lima_refuses_before_start()

    def test_symlink_default_template_refuses_before_start(self) -> None:
        saved = self.fixture.templates / "saved.yaml"
        self.fixture.default_template.rename(saved)
        self.fixture.default_template.symlink_to(saved)
        self.assert_lima_refuses_before_start()

    def test_hardlinked_default_template_refuses_before_start(self) -> None:
        os.link(self.fixture.default_template, self.fixture.templates / "linked.yaml")
        self.assert_lima_refuses_before_start()

    def test_nonprivate_template_paths_refuse_before_start(self) -> None:
        for path in (self.fixture.templates, self.fixture.default_template):
            with self.subTest(path=path.name):
                mode = path.stat().st_mode & 0o777
                path.chmod(mode | 0o040)
                try:
                    self.assert_lima_refuses_before_start()
                finally:
                    path.chmod(mode)

    def test_replaced_template_directory_refuses_before_start(self) -> None:
        saved = self.fixture.templates.with_name("saved-templates")
        self.fixture.templates.rename(saved)
        self.fixture.templates.mkdir(mode=0o700)
        (saved / "default.yaml").rename(self.fixture.default_template)
        self.assert_lima_refuses_before_start()

    def test_template_requires_hashed_regular_inventory_entry(self) -> None:
        original = self.fixture.cold_document()
        inventory = cast(list[dict[str, object]], original["persistent_files"])
        receipt = self.receipt.read_bytes()
        inode = self.receipt.stat().st_ino
        for change in ({"sha256": None}, {"type": "directory", "size": None, "sha256": None}):
            with self.subTest(change=change):
                files = [
                    {**item, **change}
                    if item["path"] == str(self.fixture.default_template)
                    else item
                    for item in inventory
                ]
                self.fixture.write_cold_manifest({**original, "persistent_files": files})
                with self.assertRaises(SourceArchiveError):
                    load_mac_cold_entitlement(self.fixture.cold_manifest)
                self.assertEqual(self.receipt.read_bytes(), receipt)
                self.assertEqual(self.receipt.stat().st_ino, inode)

    def test_local_template_or_config_override_refuses_enrollment_and_start(self) -> None:
        for relative in ("_templates", "_config/default.yaml", "_config/override.yaml"):
            for kind in ("regular", "directory", "dangling_symlink"):
                with self.subTest(path=relative, kind=kind):
                    path = self.fixture.lima_home / relative
                    if kind == "regular":
                        path.write_bytes(b"{}\n")
                        path.chmod(0o600)
                    elif kind == "directory":
                        path.mkdir(mode=0o700)
                    else:
                        path.symlink_to(self.fixture.root / "missing-target")
                    try:
                        with self.assertRaises(SourceArchiveError):
                            load_mac_cold_entitlement(self.fixture.cold_manifest)
                        self.assert_lima_refuses_before_start()
                        self.assertEqual(self.calls, [])
                    finally:
                        if kind == "directory":
                            path.rmdir()
                        else:
                            path.unlink()

    def test_unreadable_override_metadata_refuses_before_start(self) -> None:
        original = Path.lstat
        target = self.fixture.lima_home / "_config" / "override.yaml"

        def lstat(path: Path) -> os.stat_result:
            if path == target:
                raise PermissionError("synthetic inaccessible override")
            return original(path)

        with patch.object(Path, "lstat", lstat):
            self.assert_lima_refuses_before_start()
        self.assertEqual(self.calls, [])

    def test_changed_lima_bytes_refuse_before_start(self) -> None:
        self.fixture.lima.write_bytes(b"#!/bin/sh\nexit 1\n")
        self.assert_lima_refuses_before_start()

    def test_nonexecutable_lima_refuses_before_start(self) -> None:
        self.fixture.lima.chmod(0o600)
        self.assert_lima_refuses_before_start()

    def test_nonprivate_lima_refuses_before_start(self) -> None:
        self.fixture.lima.chmod(0o750)
        self.assert_lima_refuses_before_start()

    def test_symlink_lima_refuses_before_start(self) -> None:
        saved = self.fixture.lima.with_name("saved-lima")
        self.fixture.lima.rename(saved)
        self.fixture.lima.symlink_to(saved)
        self.assert_lima_refuses_before_start()

    def test_hardlinked_lima_refuses_before_start(self) -> None:
        os.link(self.fixture.lima, self.fixture.lima.with_name("linked-lima"))
        self.assert_lima_refuses_before_start()

    def test_replaced_lima_identity_refuses_before_start(self) -> None:
        saved = self.fixture.lima.with_name("saved-lima")
        self.fixture.lima.rename(saved)
        self.fixture.lima.write_bytes(saved.read_bytes())
        self.fixture.lima.chmod(0o700)
        self.assert_lima_refuses_before_start()

    def test_directory_lima_refuses_before_start(self) -> None:
        self.fixture.lima.unlink()
        self.fixture.lima.mkdir(mode=0o700)
        self.assert_lima_refuses_before_start()

    def test_missing_or_invalid_lima_pin_refuses_without_receipt_change(self) -> None:
        original = self.fixture.cold_document()
        inventory = cast(list[dict[str, object]], original["persistent_files"])
        receipt = self.receipt.read_bytes()
        inode = self.receipt.stat().st_ino
        for field, value in (
            ("omitted", None),
            ("sha256", None),
            ("size", self.fixture.lima.stat().st_size + 1),
            ("owner", self.fixture.lima.stat().st_uid + 1),
        ):
            with self.subTest(field=field):
                files = []
                for item in inventory:
                    if item["path"] == str(self.fixture.lima):
                        if field == "omitted":
                            continue
                        item = {**item, field: value}
                    files.append(item)
                self.fixture.write_cold_manifest({**original, "persistent_files": files})
                with patch(
                    "tedd_knowledge_graph.source_reader_cold.run_bounded",
                    side_effect=AssertionError("invalid dependency reached runtime"),
                ):
                    with self.assertRaises(SourceArchiveError):
                        load_mac_cold_entitlement(self.fixture.cold_manifest)
                self.assertEqual(self.receipt.read_bytes(), receipt)
                self.assertEqual(self.receipt.stat().st_ino, inode)

    def assert_lima_refuses_before_start(self) -> None:
        self.fixture.record["status"] = "Stopped"
        receipt = self.receipt.read_bytes()
        inode = self.receipt.stat().st_ino
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt.read_bytes(), receipt)
        self.assertEqual(self.receipt.stat().st_ino, inode)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_missing_store_never_dispatches(self) -> None:
        self.fixture.record["status"] = "Stopped"
        self.fixture.store.unlink()
        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.calls, [])

    def test_conflicting_stopped_context_never_dispatches(self) -> None:
        self.fixture.record["status"] = "Stopped"
        self.generated()
        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_empty_context_parent_after_prior_stop_allows_exact_resume(self) -> None:
        self.fixture.record["status"] = "Stopped"
        meta = self.fixture.docker_config / "contexts" / "meta"
        meta.mkdir(parents=True, mode=0o700)
        meta.parent.chmod(0o700)
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json(), self.idle())

    def test_failed_start_retains_starting_receipt_without_retry(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def failed(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            if arguments[0] == str(self.fixture.colima):
                self.assertEqual(self.receipt_json()["phase"], "starting")
                return ProcessResult(1, b"")
            return self.fake_process(arguments, payload, **kwargs)

        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", failed, create=True):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")
        with patch(
            "tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process, create=True
        ):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_running_profile_validates_without_dispatch_or_new_receipt(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        original = self.receipt.stat().st_ino
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            session.qualify(time.monotonic() + 5, None)
        self.assertEqual(self.receipt.stat().st_ino, original)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_session_rejects_replaced_socket_and_context_metadata(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            metadata = next(self.fixture.docker_config.glob("contexts/meta/*/meta.json"))
            replacement = metadata.with_suffix(".new")
            replacement.write_bytes(metadata.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(metadata)
            with self.assertRaises(SourceArchiveError):
                session.qualify(time.monotonic() + 5, None)

    def test_running_rewritten_store_and_configs_validate_without_start_or_receipt_write(
        self,
    ) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        manifest = self.fixture.cold_manifest.read_bytes()
        receipt = self.receipt.read_bytes()
        receipt_inode = self.receipt.stat().st_ino
        for path in (
            self.fixture.store,
            self.fixture.instance / "lima.yaml",
            self.fixture.instance / "colima.yaml",
            self.fixture.instance / "cloud-config.yaml",
            self.fixture.profile_root / "colima.yaml",
        ):
            original_inode = path.stat().st_ino
            replacement = path.with_suffix(".same")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
            self.assertNotEqual(path.stat().st_ino, original_inode)

        docker_calls: list[tuple[str, ...]] = []

        def docker(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            docker_calls.append(arguments)
            self.assertEqual(arguments[0], str(self.fixture.docker))
            if arguments[-4:] == ("system", "info", "--format", "{{json .ID}}"):
                return ProcessResult(0, _json(self.fixture.runtime.daemon_id))
            if arguments[-3:] == ("image", "inspect", self.fixture.runtime.image_id):
                return ProcessResult(
                    0,
                    _json(
                        [
                            {
                                "Id": self.fixture.runtime.image_id,
                                "Os": "linux",
                                "Architecture": "arm64",
                            }
                        ]
                    ),
                )
            self.fail(f"unexpected Docker request: {arguments}")

        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_client.run_bounded", docker),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            session.qualify(time.monotonic() + 5, None)
        self.assertEqual(self.receipt.read_bytes(), receipt)
        self.assertEqual(self.receipt.stat().st_ino, receipt_inode)
        self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
        self.assertEqual(len(docker_calls), 6)
        self.assertTrue(self.calls)
        self.assertTrue(
            all(
                call[0] == (str(self.fixture.limactl), "list", "--json", self.fixture.instance.name)
                for call in self.calls
            )
        )

    def test_stopped_rewritten_store_or_config_refuses_before_dispatch(self) -> None:
        self.fixture.record["status"] = "Stopped"
        for path in (self.fixture.store, self.fixture.instance / "lima.yaml"):
            with self.subTest(path=path.name):
                saved = path.with_suffix(".saved")
                path.rename(saved)
                path.write_bytes(saved.read_bytes())
                path.chmod(0o600)
                self.calls.clear()
                try:
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process
                    ):
                        with self.assertRaises(SourceArchiveError):
                            self.entitlement.ensure_ready(time.monotonic() + 5, None)
                    self.assertEqual(self.receipt_json(), self.idle())
                    self.assertFalse(
                        any(call[0][0] == str(self.fixture.colima) for call in self.calls)
                    )
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_fresh_running_load_accepts_reviewed_store_and_config_rewrites(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        manifest = self.fixture.cold_manifest.read_bytes()
        receipt = self.receipt.read_bytes()
        receipt_inode = self.receipt.stat().st_ino
        for path in (
            self.fixture.instance / "lima.yaml",
            self.fixture.instance / "colima.yaml",
            self.fixture.instance / "cloud-config.yaml",
            self.fixture.profile_root / "colima.yaml",
        ):
            replacement = path.with_suffix(".same")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)

        docker_calls: list[tuple[str, ...]] = []

        def docker(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            docker_calls.append(arguments)
            self.assertEqual(arguments[0], str(self.fixture.docker))
            if arguments[-4:] == ("system", "info", "--format", "{{json .ID}}"):
                return ProcessResult(0, _json(self.fixture.runtime.daemon_id))
            if arguments[-3:] == ("image", "inspect", self.fixture.runtime.image_id):
                return ProcessResult(
                    0,
                    _json(
                        [
                            {
                                "Id": self.fixture.runtime.image_id,
                                "Os": "linux",
                                "Architecture": "arm64",
                            }
                        ]
                    ),
                )
            self.fail(f"unexpected Docker request: {arguments}")

        for store_bytes in (
            self.fixture.store.read_bytes(),
            b'{ "disk_runtime": "docker", "disk_formatted": true }\n',
        ):
            with self.subTest(store_bytes=store_bytes):
                original_inode = self.fixture.store.stat().st_ino
                replacement = self.fixture.store.with_suffix(".same")
                replacement.write_bytes(store_bytes)
                replacement.chmod(0o600)
                replacement.replace(self.fixture.store)
                self.assertNotEqual(self.fixture.store.stat().st_ino, original_inode)
                with patch(
                    "tedd_knowledge_graph.source_reader_cold.run_bounded",
                    side_effect=AssertionError("fresh loader reached a process"),
                ):
                    fresh = load_mac_cold_entitlement(self.fixture.cold_manifest)
                with (
                    patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
                    patch("tedd_knowledge_graph.source_reader_client.run_bounded", docker),
                ):
                    session = fresh.ensure_ready(time.monotonic() + 5, None)
                    session.qualify(time.monotonic() + 5, None)
                self.assertEqual(self.receipt.read_bytes(), receipt)
                self.assertEqual(self.receipt.stat().st_ino, receipt_inode)
                self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
        self.assertEqual(len(docker_calls), 12)
        self.assertTrue(self.calls)
        self.assertTrue(
            all(
                call[0] == (str(self.fixture.limactl), "list", "--json", self.fixture.instance.name)
                for call in self.calls
            )
        )

    def test_running_changed_store_mapping_or_config_bytes_refuse_before_query(self) -> None:
        for path, payload in (
            (
                self.fixture.store,
                _json({"disk_formatted": True, "disk_runtime": "docker", "extra": 1}),
            ),
            (
                self.fixture.instance / "lima.yaml",
                b"X" + (self.fixture.instance / "lima.yaml").read_bytes()[1:],
            ),
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(payload)
                try:
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded",
                        side_effect=AssertionError("unvalidated state reached a process"),
                    ):
                        with self.assertRaises(SourceArchiveError):
                            self.entitlement.ensure_ready(time.monotonic() + 5, None)
                    self.assertEqual(self.receipt_json(), self.idle())
                finally:
                    path.write_bytes(original)

    def test_fresh_loaded_stopped_profile_keeps_strict_raw_dispatch_pins(self) -> None:
        self.fixture.record["status"] = "Stopped"
        manifest = self.fixture.cold_manifest.read_bytes()
        receipt = self.receipt.read_bytes()
        receipt_inode = self.receipt.stat().st_ino
        for path, payload in (
            (self.fixture.store, self.fixture.store.read_bytes()),
            (self.fixture.store, b'{ "disk_runtime": "docker", "disk_formatted": true }'),
            (
                self.fixture.instance / "lima.yaml",
                (self.fixture.instance / "lima.yaml").read_bytes(),
            ),
        ):
            with self.subTest(path=path.name, payload=payload):
                saved = path.with_suffix(".saved")
                path.rename(saved)
                path.write_bytes(payload)
                path.chmod(0o600)
                self.calls.clear()
                try:
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded",
                        side_effect=AssertionError("fresh loader reached a process"),
                    ):
                        fresh = load_mac_cold_entitlement(self.fixture.cold_manifest)
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process
                    ):
                        with self.assertRaises(SourceArchiveError):
                            fresh.ensure_ready(time.monotonic() + 5, None)
                    self.assertEqual(self.receipt.read_bytes(), receipt)
                    self.assertEqual(self.receipt.stat().st_ino, receipt_inode)
                    self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
                    self.assertEqual(
                        [call[0] for call in self.calls],
                        [(str(self.fixture.limactl), "list", "--json", self.fixture.instance.name)],
                    )
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_running_replaced_tool_or_backing_identity_refuses_before_query(self) -> None:
        for path in (
            self.fixture.colima,
            self.fixture.limactl,
            self.fixture.guest_agent,
            self.fixture.docker,
            self.fixture.instance / "disk",
            self.fixture.disk_root / "datadisk",
        ):
            with self.subTest(path=path.name):
                saved = path.with_suffix(".saved")
                path.rename(saved)
                path.write_bytes(saved.read_bytes())
                path.chmod(saved.stat().st_mode & 0o777)
                try:
                    with patch(
                        "tedd_knowledge_graph.source_reader_cold.run_bounded",
                        side_effect=AssertionError("unvalidated identity reached a process"),
                    ):
                        with self.assertRaises(SourceArchiveError):
                            self.entitlement.ensure_ready(time.monotonic() + 5, None)
                    self.assertEqual(self.receipt_json(), self.idle())
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_running_nonprivate_root_refuses_before_query(self) -> None:
        self.fixture.root.chmod(0o755)
        try:
            with patch(
                "tedd_knowledge_graph.source_reader_cold.run_bounded",
                side_effect=AssertionError("nonprivate root reached a process"),
            ):
                with self.assertRaises(SourceArchiveError):
                    self.entitlement.ensure_ready(time.monotonic() + 5, None)
        finally:
            self.fixture.root.chmod(0o700)
        self.assertEqual(self.receipt_json(), self.idle())

    def test_running_missing_context_refuses_without_start_or_receipt_write(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        metadata = next(self.fixture.docker_config.glob("contexts/meta/*/meta.json"))
        metadata.unlink()
        receipt = self.receipt.read_bytes()
        receipt_inode = self.receipt.stat().st_ino
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch(
                "tedd_knowledge_graph.source_reader_client.run_bounded",
                side_effect=AssertionError("missing context reached Docker"),
            ),
        ):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt.read_bytes(), receipt)
        self.assertEqual(self.receipt.stat().st_ino, receipt_inode)
        self.assertEqual(
            [call[0] for call in self.calls],
            [(str(self.fixture.limactl), "list", "--json", self.fixture.instance.name)],
        )

    def test_broad_generated_config_is_repaired_but_changed_content_refuses(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        config = self.fixture.instance / "cloud-config.yaml"
        config.chmod(0o644)
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            self.entitlement.ensure_ready(time.monotonic() + 5, None)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            config.write_bytes(b"changed")
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)

    def test_running_iso_requires_exact_cloud_init_entries_on_each_qualification(self) -> None:
        self.generated()
        self.fixture.record["sshLocalPort"] = 4321
        iso = self.fixture.instance / "cidata.iso"
        original = iso.read_bytes()
        manifest = self.fixture.cold_manifest.read_bytes()
        receipt = self.receipt.read_bytes()
        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_cold._preflight", return_value=None),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
            for name, image in (
                ("missing user-data", synthetic_iso(user_data=None)),
                ("missing network-config", synthetic_iso(network_config=None)),
                ("altered user-data", synthetic_iso(user_data=b"unreviewed user data")),
                ("altered network-config", synthetic_iso(network_config=b"unreviewed network")),
                ("extra entry", synthetic_iso(extra=True)),
            ):
                with self.subTest(name=name):
                    iso.write_bytes(image)
                    with self.assertRaises(SourceArchiveError):
                        self.entitlement.ensure_ready(time.monotonic() + 5, None)
                    with self.assertRaises(SourceArchiveError):
                        session.qualify(time.monotonic() + 5, None)
                    self.assertEqual(self.fixture.cold_manifest.read_bytes(), manifest)
                    self.assertEqual(self.receipt.read_bytes(), receipt)
            iso.write_bytes(original)
            session.qualify(time.monotonic() + 5, None)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_mismatched_receipt_and_profile_refuse_before_start(self) -> None:
        self.fixture.record["status"] = "Stopped"
        self.receipt.write_bytes(_json({**self.idle(), "image_id": "sha256:" + "b" * 64}))
        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.calls, [])
        self.receipt.write_bytes(_json(self.idle()))
        self.fixture.record["name"] = "colima-other"
        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))

    def test_changed_entitlement_after_load_refuses_before_start(self) -> None:
        self.fixture.record["status"] = "Stopped"
        self.fixture.cold_manifest.write_bytes(self.fixture.cold_manifest.read_bytes() + b" ")
        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.calls, [])

    def test_store_changed_after_list_refuses_before_start(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def changed(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            result = self.fake_process(arguments, payload, **kwargs)
            self.fixture.store.write_bytes(
                _json({"disk_formatted": False, "disk_runtime": "docker"})
            )
            return result

        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", changed):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertFalse(any(call[0][0] == str(self.fixture.colima) for call in self.calls))
        self.assertEqual(self.receipt_json(), self.idle())

    def test_cancelled_or_failed_cli_retains_uncertain_start(self) -> None:
        for action in ("cancel", "timeout"):
            with self.subTest(action=action):
                self.fixture.record["status"] = "Stopped"
                self.receipt.write_bytes(_json(self.idle()))
                cancelled = threading.Event()

                def interrupted(
                    arguments: tuple[str, ...],
                    payload: bytes,
                    *,
                    selected: str = action,
                    cancel_event: threading.Event = cancelled,
                    **kwargs: object,
                ) -> ProcessResult:
                    if arguments[0] == str(self.fixture.colima):
                        self.assertEqual(self.receipt_json()["phase"], "starting")
                        if selected == "cancel":
                            cancel_event.set()
                            return ProcessResult(0, b"")
                        raise SourceArchiveError("source_unavailable")
                    return self.fake_process(arguments, payload, **kwargs)

                with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", interrupted):
                    with self.assertRaises(SourceArchiveError):
                        self.entitlement.ensure_ready(time.monotonic() + 5, cancelled)
                self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_changed_poststart_context_refuses_and_retains_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def changed(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            result = self.fake_process(arguments, payload, **kwargs)
            if arguments[0] == str(self.fixture.colima):
                metadata = next(self.fixture.docker_config.glob("contexts/meta/*/meta.json"))
                record = json.loads(metadata.read_bytes())
                record["Endpoints"]["docker"]["Host"] = "unix:///tmp/other.sock"
                metadata.write_bytes(_json(record))
            return result

        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", changed):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_changed_poststart_iso_refuses_and_retains_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def changed(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            result = self.fake_process(arguments, payload, **kwargs)
            if arguments[0] == str(self.fixture.colima):
                (self.fixture.instance / "cidata.iso").write_bytes(
                    synthetic_iso(meta=META.replace(b"synthetic", b"alternate"))
                )
            return result

        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", changed):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_replaced_backing_disk_after_start_refuses_and_retains_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def changed(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            result = self.fake_process(arguments, payload, **kwargs)
            if arguments[0] == str(self.fixture.colima):
                disk = self.fixture.instance / "disk"
                saved = disk.read_bytes()
                disk.unlink()
                disk.write_bytes(saved)
                disk.chmod(0o600)
            return result

        with patch("tedd_knowledge_graph.source_reader_cold.run_bounded", changed):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_real_docker_preflight_rejects_wrong_daemon_without_clearing_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def docker(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            self.assertEqual(arguments[0], str(self.fixture.docker))
            self.assertEqual(
                kwargs["environment"], {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
            )
            if arguments[-4:] == ("system", "info", "--format", "{{json .ID}}"):
                return ProcessResult(0, _json("wrong-daemon"))
            self.fail(f"unexpected Docker request: {arguments}")

        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_client.run_bounded", docker),
        ):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_real_docker_preflight_rejects_wrong_image_without_clearing_receipt(self) -> None:
        self.fixture.record["status"] = "Stopped"

        def docker(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            self.assertEqual(arguments[0], str(self.fixture.docker))
            if arguments[-4:] == ("system", "info", "--format", "{{json .ID}}"):
                return ProcessResult(0, _json(self.fixture.runtime.daemon_id))
            if arguments[-3:] == ("image", "inspect", self.fixture.runtime.image_id):
                return ProcessResult(
                    0,
                    _json([{"Id": "sha256:" + "b" * 64, "Os": "linux", "Architecture": "arm64"}]),
                )
            self.fail(f"unexpected Docker request: {arguments}")

        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_client.run_bounded", docker),
        ):
            with self.assertRaises(SourceArchiveError):
                self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertEqual(self.receipt_json()["phase"], "starting")

    def test_real_docker_preflight_accepts_pinned_daemon_and_arm64_image(self) -> None:
        self.fixture.record["status"] = "Stopped"
        docker_calls: list[tuple[str, ...]] = []

        def docker(arguments: tuple[str, ...], payload: bytes, **kwargs: object) -> ProcessResult:
            docker_calls.append(arguments)
            if arguments[-4:] == ("system", "info", "--format", "{{json .ID}}"):
                return ProcessResult(0, _json(self.fixture.runtime.daemon_id))
            if arguments[-3:] == ("image", "inspect", self.fixture.runtime.image_id):
                return ProcessResult(
                    0,
                    _json(
                        [
                            {
                                "Id": self.fixture.runtime.image_id,
                                "Os": "linux",
                                "Architecture": "arm64",
                            }
                        ]
                    ),
                )
            self.fail(f"unexpected Docker request: {arguments}")

        with (
            patch("tedd_knowledge_graph.source_reader_cold.run_bounded", self.fake_process),
            patch("tedd_knowledge_graph.source_reader_client.run_bounded", docker),
        ):
            session = self.entitlement.ensure_ready(time.monotonic() + 5, None)
        self.assertIsNotNone(session)
        self.assertEqual(len(docker_calls), 4)
        self.assertEqual(self.receipt_json(), self.idle())

    def test_ssh_permits_only_matching_loopback_port_and_derived_aggregate(self) -> None:
        lima = self.fixture.instance / "ssh.config"
        aggregate = self.fixture.colima_home / "ssh_config"
        lima.write_bytes(
            b"Host lima-colima-tedd-reader-spike\n  HostName 127.0.0.1\n  Port 4321\n  User synthetic\n"
        )
        aggregate.write_bytes(
            lima.read_bytes().replace(
                b"Host lima-colima-tedd-reader-spike", b"Host colima-tedd-reader-spike", 1
            )
            + b"\n"  # Colima's aggregate writer adds the stanza separator.
        )
        for path in (lima, aggregate):
            path.chmod(0o600)
        baseline = _sha(lima.read_bytes().replace(b"4321", b"<port>"))
        validate_ssh_configs(
            lima, aggregate, "colima-tedd-reader-spike", 4321, baseline, time.monotonic() + 5, None
        )
        unsafe = lima.read_bytes() + b"  ProxyCommand unexpected\n"
        lima.write_bytes(unsafe)
        aggregate.write_bytes(
            unsafe.replace(
                b"Host lima-colima-tedd-reader-spike", b"Host colima-tedd-reader-spike", 1
            )
            + b"\n"
        )
        with self.assertRaises(SourceArchiveError):
            validate_ssh_configs(
                lima,
                aggregate,
                "colima-tedd-reader-spike",
                4321,
                _sha(unsafe.replace(b"4321", b"<port>")),
                time.monotonic() + 5,
                None,
            )
        lima.write_bytes(unsafe.removesuffix(b"  ProxyCommand unexpected\n"))
        aggregate.write_bytes(
            lima.read_bytes().replace(
                b"Host lima-colima-tedd-reader-spike", b"Host colima-tedd-reader-spike", 1
            )
            + b"\n"
        )
        for bad in (
            lima.read_bytes().replace(b"127.0.0.1", b"0.0.0.0"),
            lima.read_bytes().replace(b"Port 4321", b"Port 4322"),
            lima.read_bytes() + b"  ProxyCommand bad\n",
        ):
            lima.write_bytes(bad)
            with self.assertRaises(SourceArchiveError):
                validate_ssh_configs(
                    lima,
                    aggregate,
                    "colima-tedd-reader-spike",
                    4321,
                    baseline,
                    time.monotonic() + 5,
                    None,
                )


if __name__ == "__main__":
    unittest.main()
