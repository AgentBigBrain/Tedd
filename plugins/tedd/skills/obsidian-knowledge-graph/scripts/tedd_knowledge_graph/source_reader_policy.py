"""Immutable Docker policy for the optional isolated source reader."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .source_contracts import SourceArchiveError
from .source_reader_contracts import ReaderRequest

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_DAEMON_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_GUEST = "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_reader_guest.py"
_PYTHON = "/usr/local/bin/python3"
_ENVIRONMENT = (
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "HOME=/work",
    "TMPDIR=/work",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "PYTHONDONTWRITEBYTECODE=1",
)
_TMPFS = {
    "/work": "rw,nosuid,nodev,noexec,size=268435456,mode=0700,uid=65532,gid=65532",
    "/tmp": "rw,nosuid,nodev,noexec,size=67108864,mode=0700,uid=65532,gid=65532",
}
_MASKED_PATHS = [
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
]
_READONLY_PATHS = [
    "/proc/bus",
    "/proc/fs",
    "/proc/irq",
    "/proc/sys",
    "/proc/sysrq-trigger",
]


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise SourceArchiveError()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise SourceArchiveError() from None
    if str(parsed) != value:
        raise SourceArchiveError()
    return value


def _path(value: object) -> Path:
    if not isinstance(value, Path):
        raise SourceArchiveError()
    text = str(value)
    if (
        not value.is_absolute()
        or ".." in value.parts
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in text)
    ):
        raise SourceArchiveError()
    return value


def _container_id(value: object) -> str:
    if not isinstance(value, str) or _CONTAINER_ID.fullmatch(value) is None:
        raise SourceArchiveError()
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceArchiveError()
    return cast(dict[str, object], value)


def _integer(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _environment(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(_ENVIRONMENT)
        and all(isinstance(item, str) for item in value)
        and set(value) == set(_ENVIRONMENT)
    )


def _core_limit(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    limit = value[0]
    return (
        isinstance(limit, dict)
        and set(limit) == {"Name", "Soft", "Hard"}
        and limit.get("Name") == "core"
        and _integer(limit.get("Soft"), 0)
        and _integer(limit.get("Hard"), 0)
    )


def _allowed_state(state: dict[str, object]) -> bool:
    status = state.get("Status")
    flags = tuple(state.get(field) for field in ("Running", "Paused", "Restarting", "Dead"))
    if not isinstance(status, str) or any(type(value) is not bool for value in flags):
        return False
    allowed = {
        "created": (False, False, False, False),
        "running": (True, False, False, False),
        "paused": (True, True, False, False),
        "restarting": (True, False, True, False),
        "exited": (False, False, False, False),
        "dead": (False, False, False, True),
    }
    return allowed.get(status) == flags


def _command(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SourceArchiveError()
    arguments = tuple(value)
    if arguments[:3] != ("-I", "-B", _GUEST):
        raise SourceArchiveError()
    worker = arguments[3:]
    if len(worker) == 1:
        request = ReaderRequest(worker[0])
    elif len(worker) == 2:
        position = worker[1]
        if not 1 <= len(position) <= 3 or not position.isascii() or not position.isdecimal():
            raise SourceArchiveError()
        page = int(position)
        if not 1 <= page <= 256 or str(page) != position:
            raise SourceArchiveError()
        request = ReaderRequest(worker[0], page)
    else:
        raise SourceArchiveError()
    if request.worker_arguments() != worker:
        raise SourceArchiveError()
    return arguments


@dataclass(frozen=True, repr=False)
class ReaderRuntime:
    """Runtime addressing and ownership identity, not private-storage qualification."""

    docker: Path
    socket: Path
    state_root: Path
    owner_id: str
    daemon_id: str
    image_id: str

    def __post_init__(self) -> None:
        _path(self.docker)
        _path(self.socket)
        _path(self.state_root)
        _uuid(self.owner_id)
        if not isinstance(self.daemon_id, str) or _DAEMON_ID.fullmatch(self.daemon_id) is None:
            raise SourceArchiveError()
        if not isinstance(self.image_id, str) or _IMAGE_ID.fullmatch(self.image_id) is None:
            raise SourceArchiveError()


def create_arguments(
    runtime: ReaderRuntime, job_id: str, request: ReaderRequest
) -> tuple[str, ...]:
    """Build one fixed ``docker container create`` invocation without executing it."""

    if not isinstance(runtime, ReaderRuntime) or not isinstance(request, ReaderRequest):
        raise SourceArchiveError()
    _uuid(job_id)
    fixed = (
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
        "tedd-reader-" + job_id,
        "--label",
        "org.tedd.reader.owner=" + runtime.owner_id,
        "--label",
        "org.tedd.reader.job=" + job_id,
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
        "/work:" + _TMPFS["/work"],
        "--tmpfs",
        "/tmp:" + _TMPFS["/tmp"],
        "--workdir",
        "/work",
        *(item for value in _ENVIRONMENT for item in ("--env", value)),
        "--entrypoint",
        _PYTHON,
        "--restart",
        "no",
        "--no-healthcheck",
    )
    return (
        str(runtime.docker),
        "--host",
        "unix://" + str(runtime.socket),
        "container",
        "create",
        *fixed,
        runtime.image_id,
        "-I",
        "-B",
        _GUEST,
        *request.worker_arguments(),
    )


def validate_container(
    runtime: ReaderRuntime,
    job_id: str,
    document: object,
    *,
    container_id: str | None = None,
) -> str:
    """Validate immutable ownership and policy independent of lifecycle admission."""

    if not isinstance(runtime, ReaderRuntime):
        raise SourceArchiveError()
    _uuid(job_id)
    expected_container = None if container_id is None else _container_id(container_id)
    if not isinstance(document, list) or len(document) != 1:
        raise SourceArchiveError()
    item = _mapping(document[0])
    actual_id = _container_id(item.get("Id"))
    if expected_container is not None and actual_id != expected_container:
        raise SourceArchiveError()

    config = _mapping(item.get("Config"))
    host = _mapping(item.get("HostConfig"))
    state = _mapping(item.get("State"))
    command = _command(config.get("Cmd"))
    actual_args = item.get("Args")
    labels = _mapping(config.get("Labels"))
    restart = _mapping(host.get("RestartPolicy"))
    logging = _mapping(host.get("LogConfig"))
    network = _mapping(item.get("NetworkSettings"))
    networks = _mapping(network.get("Networks"))

    checks = (
        item.get("Name") == "/tedd-reader-" + job_id,
        item.get("Image") == runtime.image_id,
        item.get("Path") == _PYTHON,
        actual_args == [*command],
        item.get("Platform") == "linux",
        _allowed_state(state),
        config.get("Hostname") == actual_id[:12],
        config.get("Domainname") == "",
        config.get("Image") == runtime.image_id,
        config.get("User") == "65532:65532",
        config.get("WorkingDir") == "/work",
        config.get("Entrypoint") == [_PYTHON],
        _environment(config.get("Env")),
        labels
        == {
            "org.tedd.reader.owner": runtime.owner_id,
            "org.tedd.reader.job": job_id,
        },
        config.get("Tty") is False,
        config.get("OpenStdin") is True,
        config.get("StdinOnce") is True,
        config.get("AttachStdin") is True,
        config.get("AttachStdout") is True,
        config.get("AttachStderr") is False,
        config.get("Healthcheck") == {"Test": ["NONE"]},
        config.get("Volumes") in (None, {}),
        config.get("ExposedPorts") in (None, {}),
        network.get("Ports") == {},
        set(networks) == {"none"},
        host.get("ContainerIDFile") == "",
        host.get("AutoRemove") is False,
        host.get("PortBindings") == {},
        host.get("PublishAllPorts") is False,
        host.get("VolumeDriver") == "",
        host.get("Init") in (None, False),
        host.get("Privileged") is False,
        host.get("ReadonlyRootfs") is True,
        host.get("NetworkMode") == "none",
        host.get("PidMode") == "",
        host.get("IpcMode") == "private",
        host.get("Cgroup") == "",
        host.get("CgroupnsMode") == "private",
        host.get("Runtime") == "runc",
        host.get("Isolation") == "",
        _integer(host.get("ShmSize"), 67108864),
        host.get("Dns") in (None, []),
        host.get("DnsOptions") == [],
        host.get("DnsSearch") == [],
        host.get("ExtraHosts") is None,
        host.get("GroupAdd") is None,
        host.get("Links") is None,
        host.get("UTSMode") == "",
        host.get("UsernsMode") == "",
        _integer(host.get("OomScoreAdj"), 0),
        host.get("CapAdd") in (None, []),
        host.get("CapDrop") == ["ALL"],
        host.get("SecurityOpt") == ["no-new-privileges=true"],
        _integer(host.get("PidsLimit"), 64),
        _integer(host.get("Memory"), 2147483648),
        _integer(host.get("MemorySwap"), 2147483648),
        _integer(host.get("NanoCpus"), 2000000000),
        _integer(host.get("CpuShares"), 0),
        _integer(host.get("CpuPeriod"), 0),
        _integer(host.get("CpuQuota"), 0),
        _integer(host.get("CpuRealtimePeriod"), 0),
        _integer(host.get("CpuRealtimeRuntime"), 0),
        host.get("CpusetCpus") == "",
        host.get("CpusetMems") == "",
        host.get("CgroupParent") == "",
        _integer(host.get("BlkioWeight"), 0),
        host.get("BlkioWeightDevice") == [],
        host.get("BlkioDeviceReadBps") == [],
        host.get("BlkioDeviceWriteBps") == [],
        host.get("BlkioDeviceReadIOps") == [],
        host.get("BlkioDeviceWriteIOps") == [],
        _integer(host.get("MemoryReservation"), 0),
        host.get("MemorySwappiness") is None,
        "OomKillDisable" in host
        and (host["OomKillDisable"] is False or host["OomKillDisable"] is None),
        _integer(host.get("CpuCount"), 0),
        _integer(host.get("CpuPercent"), 0),
        _integer(host.get("IOMaximumIOps"), 0),
        _integer(host.get("IOMaximumBandwidth"), 0),
        set(restart) == {"Name", "MaximumRetryCount"},
        restart.get("Name") == "no",
        _integer(restart.get("MaximumRetryCount"), 0),
        logging == {"Type": "none", "Config": {}},
        _core_limit(host.get("Ulimits")),
        host.get("Tmpfs") == _TMPFS,
        host.get("Binds") in (None, []),
        host.get("Mounts") in (None, []),
        host.get("VolumesFrom") in (None, []),
        host.get("Devices") in (None, []),
        host.get("DeviceCgroupRules") is None,
        host.get("DeviceRequests") in (None, []),
        host.get("MaskedPaths") == _MASKED_PATHS,
        host.get("ReadonlyPaths") == _READONLY_PATHS,
        item.get("Mounts") == [],
    )
    if not all(checks):
        raise SourceArchiveError()
    return actual_id
