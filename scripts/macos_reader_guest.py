#!/usr/local/bin/python3 -I
"""Fixed entrypoint for the isolated macOS reader guest."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

_TRUSTED_SCRIPTS = "/opt/tedd-reader/scripts"
_TRUSTED_PACKAGE = "/opt/tedd-reader/scripts/tedd_knowledge_graph"
_MAX_INPUT = 64 * 1024**2
_MAX_OUTPUT = 32 * 1024**2
_SELF_CHECK_MEMORY = 512 * 1024**2
_OFFICE_MEMORY = 2 * 1024**3
_SECONDS = 30
_WORK_ROOT = Path("/work")
_ERROR_CODES = frozenset(
    {
        "capability_unavailable",
        "capture_incomplete",
        "cleanup_incomplete",
        "limit_exceeded",
        "privacy_rejected",
        "scope_denied",
        "source_corrupt",
        "source_unavailable",
    }
)
_CGROUP_FILES = (
    ("memory.max", Path("/sys/fs/cgroup/memory.max")),
    ("memory.swap.max", Path("/sys/fs/cgroup/memory.swap.max")),
    ("pids.max", Path("/sys/fs/cgroup/pids.max")),
    ("cpu.max", Path("/sys/fs/cgroup/cpu.max")),
)
_WORKERS = {
    "image-overview": (
        f"{_TRUSTED_PACKAGE}/source_office_host.py",
        "image",
        "--render-unit",
        "1",
    ),
    "pdf-text": (f"{_TRUSTED_PACKAGE}/source_pdf_host.py",),
    "pdf-page": (
        f"{_TRUSTED_PACKAGE}/source_pdf_host.py",
        "--render-page",
        "1",
        "--renderer",
        "/usr/bin/pdftoppm",
    ),
}
_SPECIAL = frozenset({"office-page", "self-check"})


def worker_argv(operation: str, python: str) -> tuple[str, ...] | None:
    """Return the worker command for an operation."""

    if not isinstance(operation, str):
        raise ValueError("invalid operation")
    if operation in _SPECIAL:
        return None
    try:
        worker = _WORKERS[operation]
    except KeyError:
        raise ValueError("invalid operation") from None
    return (python, "-I", "-B", *worker)


def _json_bytes(result: dict[str, object]) -> bytes:
    try:
        payload = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        payload = b'{"status":"capability_unavailable"}'
    if len(payload) > _MAX_OUTPUT:
        return b'{"status":"limit_exceeded"}'
    return payload


def _respond(result: dict[str, object]) -> int:
    payload = _json_bytes(result)
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    status = json.loads(payload).get("status")
    return 0 if status in ("self_check", "text_screened") else 1


def _cgroup_value(path: Path, *, pairs: bool) -> str:
    with path.open("rb") as stream:
        raw = stream.read(129)
    if not raw.endswith(b"\n") or len(raw) > 128:
        raise ValueError("unavailable cgroup value")
    try:
        value = raw[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("unavailable cgroup value") from None
    pattern = r"(?:max|[0-9]+) [0-9]+" if pairs else r"(?:max|[0-9]+)"
    if re.fullmatch(pattern, value) is None:
        raise ValueError("unavailable cgroup value")
    return value


def _self_check() -> dict[str, object]:
    architecture = platform.machine()
    python_version = platform.python_version()
    if not architecture or not python_version:
        raise ValueError("unavailable runtime diagnostic")
    getuid = cast(Callable[[], int], vars(os)["getuid"])
    result: dict[str, object] = {
        "status": "self_check",
        "uid": getuid(),
        "architecture": architecture,
        "python_version": python_version,
    }
    for name, path in _CGROUP_FILES:
        result[name] = _cgroup_value(path, pairs=name == "cpu.max")
    return result


def _special(operation: str) -> int:
    if _TRUSTED_SCRIPTS not in sys.path:
        sys.path.insert(0, _TRUSTED_SCRIPTS)
    try:
        from tedd_knowledge_graph.source_contracts import SourceArchiveError
        from tedd_knowledge_graph.source_office_host import document_page
        from tedd_knowledge_graph.source_pdf_host import limit_worker
    except Exception:
        return _respond({"status": "capability_unavailable"})

    try:
        memory = _OFFICE_MEMORY if operation == "office-page" else _SELF_CHECK_MEMORY
        _job = limit_worker(memory, _SECONDS)
        if operation == "self-check":
            result = _self_check()
        else:
            payload = sys.stdin.buffer.read(_MAX_INPUT + 1)
            if not payload:
                raise SourceArchiveError("source_corrupt")
            if len(payload) > _MAX_INPUT:
                raise SourceArchiveError("limit_exceeded")
            with tempfile.TemporaryDirectory(prefix="tedd-office-", dir=_WORK_ROOT) as raw:
                result = document_page(
                    payload,
                    "docx",
                    1,
                    reader=Path(sys.executable),
                    converter=Path("/usr/bin/libreoffice"),
                    renderer=Path("/usr/bin/pdftoppm"),
                    work_root=Path(raw),
                )
            if result.get("status") != "text_screened":
                raise SourceArchiveError("capability_unavailable")
    except SourceArchiveError as error:
        code = error.code if error.code in _ERROR_CODES else "capability_unavailable"
        return _respond({"status": code})
    except Exception:
        return _respond({"status": "capability_unavailable"})
    return _respond(result)


def main() -> int:
    """Dispatch exactly one closed guest operation."""

    if len(sys.argv) != 2:
        return _respond({"status": "capability_unavailable"})
    operation = sys.argv[1]
    try:
        argv = worker_argv(operation, sys.executable)
    except ValueError:
        return _respond({"status": "capability_unavailable"})
    if argv is None:
        return _special(operation)
    try:
        execute = cast(Callable[[str, tuple[str, ...]], None], os.execv)
        execute(argv[0], argv)
    except OSError:
        return _respond({"status": "capability_unavailable"})
    return _respond({"status": "capability_unavailable"})


if __name__ == "__main__":
    raise SystemExit(main())
