"""Closed Docker creation and inspection policy for the isolated reader."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_contracts import ReaderRequest
from tedd_knowledge_graph.source_reader_policy import (
    ReaderRuntime,
    create_arguments,
    validate_container,
)

OWNER_ID = "12345678-1234-4234-8234-123456789abc"
JOB_ID = "87654321-4321-4321-8321-cba987654321"
CONTAINER_ID = "b" * 64
IMAGE_ID = "sha256:" + "a" * 64
DAEMON_ID = "51fb0ef1-7c8d-4b79-b551-b607c2dbce27"
HOST_ROOT = Path(tempfile.gettempdir()).resolve() / "tedd-reader-policy"
DOCKER = HOST_ROOT / "bin" / "docker"
SOCKET = HOST_ROOT / "run" / "docker.sock"
STATE_ROOT = HOST_ROOT / "state"
GUEST = "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_reader_guest.py"
ENVIRONMENT = [
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "HOME=/work",
    "TMPDIR=/work",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "PYTHONDONTWRITEBYTECODE=1",
]


def runtime() -> ReaderRuntime:
    return ReaderRuntime(DOCKER, SOCKET, STATE_ROOT, OWNER_ID, DAEMON_ID, IMAGE_ID)


def inspected_container() -> list[dict[str, object]]:
    """Return a literal Docker inspect fixture, independent of the policy builder."""

    return [
        {
            "Id": CONTAINER_ID,
            "Name": "/tedd-reader-" + JOB_ID,
            "Image": IMAGE_ID,
            "Path": "/usr/local/bin/python3",
            "Args": ["-I", "-B", GUEST, "pdf-page", "2"],
            "Platform": "linux",
            "State": {
                "Status": "created",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
            },
            "Config": {
                "Hostname": CONTAINER_ID[:12],
                "Domainname": "",
                "Image": IMAGE_ID,
                "User": "65532:65532",
                "WorkingDir": "/work",
                "Entrypoint": ["/usr/local/bin/python3"],
                "Cmd": ["-I", "-B", GUEST, "pdf-page", "2"],
                "Env": ENVIRONMENT.copy(),
                "Labels": {
                    "org.tedd.reader.owner": OWNER_ID,
                    "org.tedd.reader.job": JOB_ID,
                },
                "Tty": False,
                "OpenStdin": True,
                "StdinOnce": True,
                "AttachStdin": True,
                "AttachStdout": True,
                "AttachStderr": False,
                "Healthcheck": {"Test": ["NONE"]},
                "Volumes": None,
                "ExposedPorts": None,
            },
            "HostConfig": {
                "ContainerIDFile": "",
                "AutoRemove": False,
                "PortBindings": {},
                "PublishAllPorts": False,
                "VolumeDriver": "",
                "Privileged": False,
                "ReadonlyRootfs": True,
                "NetworkMode": "none",
                "PidMode": "",
                "IpcMode": "private",
                "Cgroup": "",
                "CgroupnsMode": "private",
                "Runtime": "runc",
                "Isolation": "",
                "ShmSize": 67108864,
                "Dns": None,
                "DnsOptions": [],
                "DnsSearch": [],
                "ExtraHosts": None,
                "GroupAdd": None,
                "Links": None,
                "UTSMode": "",
                "UsernsMode": "",
                "OomScoreAdj": 0,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=true"],
                "PidsLimit": 64,
                "Memory": 2147483648,
                "MemorySwap": 2147483648,
                "NanoCpus": 2000000000,
                "CpuShares": 0,
                "CpuPeriod": 0,
                "CpuQuota": 0,
                "CpuRealtimePeriod": 0,
                "CpuRealtimeRuntime": 0,
                "CpusetCpus": "",
                "CpusetMems": "",
                "CgroupParent": "",
                "BlkioWeight": 0,
                "BlkioWeightDevice": [],
                "BlkioDeviceReadBps": [],
                "BlkioDeviceWriteBps": [],
                "BlkioDeviceReadIOps": [],
                "BlkioDeviceWriteIOps": [],
                "MemoryReservation": 0,
                "MemorySwappiness": None,
                "OomKillDisable": False,
                "CpuCount": 0,
                "CpuPercent": 0,
                "IOMaximumIOps": 0,
                "IOMaximumBandwidth": 0,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "LogConfig": {"Type": "none", "Config": {}},
                "Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0}],
                "Tmpfs": {
                    "/work": (
                        "rw,nosuid,nodev,noexec,size=268435456,mode=0700,uid=65532,gid=65532"
                    ),
                    "/tmp": ("rw,nosuid,nodev,noexec,size=67108864,mode=0700,uid=65532,gid=65532"),
                },
                "Binds": None,
                "Mounts": None,
                "VolumesFrom": None,
                "Devices": [],
                "DeviceCgroupRules": None,
                "DeviceRequests": None,
                "MaskedPaths": [
                    "/proc/acpi",
                    "/proc/asound",
                    "/proc/interrupts",
                    "/proc/kcore",
                    "/proc/keys",
                    "/proc/latency_stats",
                    "/proc/sched_debug",
                    "/proc/scsi",
                    "/proc/timer_list",
                    "/proc/timer_stats",
                    "/sys/devices/virtual/powercap",
                    "/sys/firmware",
                ],
                "ReadonlyPaths": [
                    "/proc/bus",
                    "/proc/fs",
                    "/proc/irq",
                    "/proc/sys",
                    "/proc/sysrq-trigger",
                ],
            },
            "Mounts": [],
            "NetworkSettings": {"Ports": {}, "Networks": {"none": {}}},
        }
    ]


def inspected_exited_container() -> list[dict[str, object]]:
    """Return the literal Engine 29 post-start normalization of the fixture."""

    document = inspected_container()
    document[0]["State"] = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "Dead": False,
    }
    host = cast(dict[str, object], document[0]["HostConfig"])
    host["OomKillDisable"] = None
    return document


class ReaderRuntimeTests(unittest.TestCase):
    def test_accepts_fixed_lexical_runtime_identity_without_touching_paths(self) -> None:
        current = runtime()

        self.assertEqual(current.socket, SOCKET)
        self.assertNotIn(OWNER_ID, repr(current))

    def test_rejects_relative_traversing_or_control_character_paths(self) -> None:
        with self.assertRaises(SourceArchiveError):
            replace(runtime(), docker=Path("docker"))
        with self.assertRaises(SourceArchiveError):
            replace(runtime(), socket=HOST_ROOT / "tmp" / ".." / "run" / "docker.sock")
        for path in (HOST_ROOT / "tedd\nreader", HOST_ROOT / "tedd\x00reader"):
            with self.subTest(path=path), self.assertRaises(SourceArchiveError):
                replace(runtime(), state_root=path)

    def test_rejects_non_path_values_and_malformed_identifiers(self) -> None:
        with self.assertRaises(SourceArchiveError):
            replace(runtime(), docker=cast(Path, "/opt/homebrew/bin/docker"))
        for value in ("12345678123442348234123456789abc", OWNER_ID.upper()):
            with self.subTest(owner_id=value), self.assertRaises(SourceArchiveError):
                replace(runtime(), owner_id=value)
        for value in ("daemon id", "daemon\nidentity"):
            with self.subTest(daemon_id=value), self.assertRaises(SourceArchiveError):
                replace(runtime(), daemon_id=value)
        for value in ("reader:latest", "sha256:" + "A" * 64):
            with self.subTest(image_id=value), self.assertRaises(SourceArchiveError):
                replace(runtime(), image_id=value)


class CreatePolicyTests(unittest.TestCase):
    def test_emits_the_literal_constrained_create_boundary(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DOCKER_HOST": "tcp://untrusted.example:2375",
                "DOCKER_CONTEXT": "untrusted-context",
            },
        ):
            arguments = create_arguments(runtime(), JOB_ID, ReaderRequest("pdf-page", 2))

        self.assertEqual(
            arguments,
            (
                str(DOCKER),
                "--host",
                "unix://" + str(SOCKET),
                "container",
                "create",
                "--pull",
                "never",
                "--platform",
                "linux/arm64",
                "--attach",
                "stdin",
                "--attach",
                "stdout",
                "--interactive",
                "--name",
                "tedd-reader-87654321-4321-4321-8321-cba987654321",
                "--label",
                "org.tedd.reader.owner=12345678-1234-4234-8234-123456789abc",
                "--label",
                "org.tedd.reader.job=87654321-4321-4321-8321-cba987654321",
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
                "PATH=/usr/local/bin:/usr/bin:/bin",
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
                "/usr/local/bin/python3",
                "--restart",
                "no",
                "--no-healthcheck",
                IMAGE_ID,
                "-I",
                "-B",
                GUEST,
                "pdf-page",
                "2",
            ),
        )
        for forbidden in ("--rm", "--init", "--tty", "-t", "--privileged", "--volume", "-v"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, arguments)

    def test_appends_only_the_six_canonical_worker_argument_forms(self) -> None:
        cases = (
            (ReaderRequest("image-inspect"), ("image-inspect",)),
            (ReaderRequest("image-page", 1), ("image-page", "1")),
            (ReaderRequest("pdf-text"), ("pdf-text",)),
            (ReaderRequest("pdf-page", 2), ("pdf-page", "2")),
            (ReaderRequest("docx-text"), ("docx-text",)),
            (ReaderRequest("docx-page", 256), ("docx-page", "256")),
        )
        for request, suffix in cases:
            with self.subTest(operation=request.operation):
                self.assertEqual(
                    create_arguments(runtime(), JOB_ID, request)[-len(suffix) :], suffix
                )

    def test_rejects_noncanonical_jobs_and_non_request_objects_content_free(self) -> None:
        marker = "DO_NOT_ECHO_POLICY_ee68"
        for job_id in (marker, JOB_ID.upper(), "", 7, None):
            with self.subTest(job_id=job_id), self.assertRaises(SourceArchiveError) as raised:
                create_arguments(
                    runtime(),
                    cast(str, job_id),
                    ReaderRequest("pdf-text"),
                )
            self.assertNotIn(marker, str(raised.exception))
        with self.assertRaises(SourceArchiveError):
            create_arguments(runtime(), JOB_ID, cast(ReaderRequest, object()))


class InspectPolicyTests(unittest.TestCase):
    def test_accepts_one_complete_created_container_and_returns_its_full_id(self) -> None:
        self.assertEqual(
            validate_container(
                runtime(),
                JOB_ID,
                inspected_container(),
                container_id=CONTAINER_ID,
            ),
            CONTAINER_ID,
        )
        reordered = inspected_container()
        config = cast(dict[str, object], reordered[0]["Config"])
        config["Env"] = list(reversed(ENVIRONMENT))
        del config["Volumes"]
        self.assertEqual(validate_container(runtime(), JOB_ID, reordered), CONTAINER_ID)

    def test_policy_validation_is_independent_of_coordinator_lifecycle_checks(self) -> None:
        states = (
            ("created", False, False, False, False),
            ("running", True, False, False, False),
            ("exited", False, False, False, False),
        )
        for status, running, paused, restarting, dead in states:
            with self.subTest(status=status):
                document = inspected_container()
                document[0]["State"] = {
                    "Status": status,
                    "Running": running,
                    "Paused": paused,
                    "Restarting": restarting,
                    "Dead": dead,
                }
                self.assertEqual(
                    validate_container(runtime(), JOB_ID, document, container_id=CONTAINER_ID),
                    CONTAINER_ID,
                )

    def test_accepts_only_boolean_false_or_post_start_null_oom_kill_disable(self) -> None:
        self.assertEqual(
            validate_container(
                runtime(),
                JOB_ID,
                inspected_exited_container(),
                container_id=CONTAINER_ID,
            ),
            CONTAINER_ID,
        )

        invalid_values: tuple[object, ...] = (True, 0, "false", [], {})
        for value in invalid_values:
            with self.subTest(value=value):
                document = inspected_exited_container()
                host = cast(dict[str, object], document[0]["HostConfig"])
                host["OomKillDisable"] = value
                with self.assertRaises(SourceArchiveError):
                    validate_container(runtime(), JOB_ID, document)

        missing = inspected_exited_container()
        cast(dict[str, object], missing[0]["HostConfig"]).pop("OomKillDisable")
        with self.assertRaises(SourceArchiveError):
            validate_container(runtime(), JOB_ID, missing)

    def test_accepts_empty_dns_after_daemon_restart(self) -> None:
        values: tuple[object, ...] = (None, [])
        for value in values:
            with self.subTest(dns=value):
                document = inspected_exited_container()
                cast(dict[str, object], document[0]["HostConfig"])["Dns"] = value
                self.assertEqual(
                    validate_container(runtime(), JOB_ID, document, container_id=CONTAINER_ID),
                    CONTAINER_ID,
                )

    def test_rejects_dns_overrides_and_malformed_values_after_daemon_restart(self) -> None:
        invalid_values: tuple[object, ...] = (["8.8.8.8"], [None], "", "[]", {}, False, 0, ())
        for value in invalid_values:
            with self.subTest(dns=value):
                document = inspected_exited_container()
                cast(dict[str, object], document[0]["HostConfig"])["Dns"] = value
                with self.assertRaises(SourceArchiveError):
                    validate_container(runtime(), JOB_ID, document, container_id=CONTAINER_ID)

    def test_rejects_oversized_page_selector_before_integer_conversion(self) -> None:
        marker = "9" * 10_000
        document = inspected_container()
        command = ["-I", "-B", GUEST, "pdf-page", marker]
        cast(dict[str, object], document[0]["Config"])["Cmd"] = command
        document[0]["Args"] = command.copy()

        with self.assertRaises(SourceArchiveError) as raised:
            validate_container(runtime(), JOB_ID, document)

        self.assertEqual(str(raised.exception), "source_corrupt")
        self.assertNotIn(marker, str(raised.exception))

    def test_rejects_identity_command_environment_and_state_mutations(self) -> None:
        mutations: tuple[tuple[tuple[object, ...], object], ...] = (
            ((0, "Id"), "b" * 63),
            ((0, "Name"), "/tedd-reader-wrong"),
            ((0, "Image"), "sha256:" + "c" * 64),
            ((0, "Path"), "/bin/sh"),
            ((0, "Args"), ["-I", "-B", GUEST, "pdf-text"]),
            ((0, "Platform"), "darwin"),
            ((0, "State", "Status"), "running"),
            ((0, "State", "Running"), True),
            ((0, "Config", "Image"), "reader:latest"),
            ((0, "Config", "Hostname"), "untrusted"),
            ((0, "Config", "Domainname"), "example.test"),
            ((0, "Config", "User"), "0:0"),
            ((0, "Config", "WorkingDir"), "/tmp"),
            ((0, "Config", "Entrypoint"), ["/bin/sh"]),
            ((0, "Config", "Cmd"), ["-I", "-B", GUEST, "pdf-page", "3"]),
            ((0, "Config", "Env"), [*ENVIRONMENT, "TOKEN=secret"]),
            ((0, "Config", "Labels", "org.tedd.reader.owner"), JOB_ID),
            ((0, "Config", "Labels", "org.tedd.reader.job"), OWNER_ID),
            ((0, "Config", "Tty"), True),
            ((0, "Config", "OpenStdin"), False),
            ((0, "Config", "StdinOnce"), False),
            ((0, "Config", "AttachStdin"), False),
            ((0, "Config", "AttachStdout"), False),
            ((0, "Config", "AttachStderr"), True),
            ((0, "Config", "Healthcheck"), None),
            ((0, "Config", "Volumes"), {"/data": {}}),
            ((0, "Config", "ExposedPorts"), {"80/tcp": {}}),
            ((0, "NetworkSettings", "Ports"), {"80/tcp": []}),
            ((0, "NetworkSettings", "Networks"), {"bridge": {}}),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                document = inspected_container()
                target: object = document
                for key in path[:-1]:
                    target = target[key]  # type: ignore[index]
                target[path[-1]] = value  # type: ignore[index]
                with self.assertRaises(SourceArchiveError):
                    validate_container(runtime(), JOB_ID, document)

    def test_rejects_isolation_logging_limit_and_mount_mutations(self) -> None:
        mutations: tuple[tuple[str, object], ...] = (
            ("AutoRemove", True),
            ("ContainerIDFile", "/tmp/id"),
            ("PortBindings", {"80/tcp": [{"HostPort": "80"}]}),
            ("PublishAllPorts", True),
            ("VolumeDriver", "local"),
            ("Init", True),
            ("Privileged", True),
            ("ReadonlyRootfs", False),
            ("NetworkMode", "host"),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("Cgroup", "untrusted"),
            ("CgroupnsMode", "host"),
            ("Runtime", "untrusted"),
            ("Isolation", "hyperv"),
            ("ShmSize", 67108865),
            ("Dns", ["8.8.8.8"]),
            ("DnsOptions", ["use-vc"]),
            ("DnsSearch", ["example.test"]),
            ("ExtraHosts", ["host:127.0.0.1"]),
            ("GroupAdd", ["0"]),
            ("Links", ["other:alias"]),
            ("UTSMode", "host"),
            ("UsernsMode", "host"),
            ("OomScoreAdj", 1),
            ("CapAdd", ["SYS_ADMIN"]),
            ("CapDrop", []),
            ("SecurityOpt", []),
            ("PidsLimit", 64.0),
            ("PidsLimit", 65),
            ("Memory", 2147483648.0),
            ("Memory", 2147483647),
            ("MemorySwap", 2147483648.0),
            ("MemorySwap", 4294967296),
            ("NanoCpus", 2000000000.0),
            ("NanoCpus", 1000000000),
            ("CpuShares", 2),
            ("CpuPeriod", 100000),
            ("CpuQuota", 200000),
            ("CpuRealtimePeriod", 1),
            ("CpuRealtimeRuntime", 1),
            ("CpusetCpus", "0"),
            ("CpusetMems", "0"),
            ("CgroupParent", "untrusted"),
            ("BlkioWeight", 10),
            ("BlkioWeightDevice", [{"Path": "/dev/null"}]),
            ("BlkioDeviceReadBps", [{"Path": "/dev/null", "Rate": 1}]),
            ("BlkioDeviceWriteBps", [{"Path": "/dev/null", "Rate": 1}]),
            ("BlkioDeviceReadIOps", [{"Path": "/dev/null", "Rate": 1}]),
            ("BlkioDeviceWriteIOps", [{"Path": "/dev/null", "Rate": 1}]),
            ("MemoryReservation", 1),
            ("MemorySwappiness", 0),
            ("OomKillDisable", True),
            ("CpuCount", 1),
            ("CpuPercent", 1),
            ("IOMaximumIOps", 1),
            ("IOMaximumBandwidth", 1),
            ("RestartPolicy", {"Name": "no", "MaximumRetryCount": 0.0}),
            ("RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
            ("LogConfig", {"Type": "json-file", "Config": {}}),
            ("Ulimits", [{"Name": "core", "Soft": 0.0, "Hard": 0}]),
            ("Ulimits", [{"Name": "core", "Soft": 0, "Hard": 1}]),
            ("Binds", ["/host:/work"]),
            ("Mounts", [{"Type": "volume", "Target": "/data"}]),
            ("VolumesFrom", ["other"]),
            ("Devices", [{"PathOnHost": "/dev/null"}]),
            ("DeviceCgroupRules", ["c 1:3 rwm"]),
            ("DeviceRequests", [{"Driver": ""}]),
            ("MaskedPaths", []),
            ("ReadonlyPaths", []),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                document = inspected_container()
                cast(dict[str, object], document[0]["HostConfig"])[field] = value
                with self.assertRaises(SourceArchiveError):
                    validate_container(runtime(), JOB_ID, document)

        for target, value in (
            ("/work", "rw,nosuid,nodev,noexec,size=1,mode=0700,uid=65532,gid=65532"),
            ("/tmp", "rw,nosuid,nodev,noexec,size=67108864,mode=0777,uid=65532,gid=65532"),
            ("/extra", "rw,nosuid,nodev,noexec,size=1,mode=0700,uid=65532,gid=65532"),
        ):
            with self.subTest(tmpfs=target):
                document = inspected_container()
                tmpfs = cast(
                    dict[str, object],
                    cast(dict[str, object], document[0]["HostConfig"])["Tmpfs"],
                )
                tmpfs[target] = value
                with self.assertRaises(SourceArchiveError):
                    validate_container(runtime(), JOB_ID, document)

        document = inspected_container()
        document[0]["Mounts"] = [{"Type": "volume", "Name": "anonymous", "Destination": "/x"}]
        with self.assertRaises(SourceArchiveError):
            validate_container(runtime(), JOB_ID, document)

    def test_rejects_ambiguous_malformed_or_mismatched_inspection_content_free(self) -> None:
        marker = "DO_NOT_ECHO_INSPECT_76dc"
        documents: tuple[object, ...] = (
            {},
            [],
            inspected_container() * 2,
            [{"error": marker}],
        )
        for document in documents:
            with self.subTest(document=document), self.assertRaises(SourceArchiveError) as raised:
                validate_container(runtime(), JOB_ID, document)
            self.assertNotIn(marker, str(raised.exception))

        with self.assertRaises(SourceArchiveError):
            validate_container(runtime(), JOB_ID, inspected_container(), container_id="c" * 64)

        document = copy.deepcopy(inspected_container())
        cast(dict[str, object], document[0]["Config"])["Labels"] = {
            "org.tedd.reader.owner": OWNER_ID,
            "org.tedd.reader.job": JOB_ID,
            "untrusted": marker,
        }
        with self.assertRaises(SourceArchiveError) as raised:
            validate_container(runtime(), JOB_ID, document)
        self.assertNotIn(marker, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
