"""Construct arguments for an unqualified macOS reader-container spike."""

from __future__ import annotations

import re
import uuid

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_OPERATIONS = frozenset({"image-overview", "pdf-text", "pdf-page", "office-page", "self-check"})


def _validate_socket_path(socket_path: object) -> None:
    if not isinstance(socket_path, str):
        raise ValueError("invalid socket path")
    parts = socket_path.split("/")
    if (
        not socket_path.startswith("/")
        or socket_path.startswith("//")
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in socket_path)
        or any(part in {"", ".", ".."} for part in parts[1:])
        or parts[-1] != "docker.sock"
    ):
        raise ValueError("invalid socket path")


def _validate_job_id(job_id: object) -> None:
    if not isinstance(job_id, str):
        raise ValueError("invalid job identifier")
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise ValueError("invalid job identifier") from None
    if str(parsed) != job_id:
        raise ValueError("invalid job identifier")


def build_run_argv(
    socket_path: str,
    image_id: str,
    job_id: str,
    operation: str,
) -> tuple[str, ...]:
    """Return arguments for a private, immutable reader-spike image contract."""

    _validate_socket_path(socket_path)
    if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError("invalid image identifier")
    _validate_job_id(job_id)
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError("invalid operation")

    fixed = (
        "--pull",
        "never",
        "--platform",
        "linux/arm64",
        "--rm",
        "--interactive",
        "--name",
        "tedd-reader-spike-" + job_id,
        "--label",
        "org.tedd.reader-spike.job=" + job_id,
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
    )
    return (
        "docker",
        "--host",
        "unix://" + socket_path,
        "run",
        *fixed,
        image_id,
        operation,
    )
