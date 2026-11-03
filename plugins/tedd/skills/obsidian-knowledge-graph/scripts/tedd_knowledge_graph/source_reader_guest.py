"""PID-1 watchdog for one fixed isolated reader operation.

This module deliberately imports only the Python standard library. It owns the
guest lifetime but never reads, buffers, or forwards the source stream.
"""

from __future__ import annotations

import ctypes
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Callable, NoReturn, Protocol, cast


class _AlarmSignal(Protocol):
    SIGALRM: int
    ITIMER_REAL: int

    def signal(
        self, signal_number: int, handler: Callable[[int, FrameType | None], object]
    ) -> object: ...

    def setitimer(self, which: int, seconds: float) -> tuple[float, float]: ...


_ALARM = cast(_AlarmSignal, signal)
_IS_LINUX = sys.platform == "linux"
_OPERATIONS_WITHOUT_PAGE = frozenset({"image-inspect", "pdf-text", "docx-text"})
_OPERATIONS_WITH_PAGE = frozenset({"image-page", "pdf-page", "docx-page"})


def parse_arguments(arguments: tuple[str, ...]) -> tuple[str, int | None]:
    """Parse the closed six-operation wire vocabulary without external parsers."""

    if len(arguments) == 1 and arguments[0] in _OPERATIONS_WITHOUT_PAGE:
        return arguments[0], None
    if len(arguments) == 2 and arguments[0] in _OPERATIONS_WITH_PAGE:
        raw_page = arguments[1]
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            pass
        else:
            if 1 <= page <= 256 and raw_page == str(page):
                return arguments[0], page
    raise ValueError("invalid reader operation") from None


def worker_argv(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Return the sole trusted worker entrypoint for validated operation arguments."""

    parse_arguments(arguments)
    worker = Path(__file__).resolve().with_name("source_reader_worker.py")
    return (sys.executable, "-I", "-B", str(worker), *arguments)


def supervise(argv: tuple[str, ...], *, seconds: float = 30.0) -> int:
    """Run and reap one trusted direct child within an absolute deadline."""

    if (
        not isinstance(argv, tuple)
        or not argv
        or any(type(argument) is not str or not argument for argument in argv)
        or isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or not 0 < seconds <= 30
    ):
        raise ValueError("invalid supervisor arguments") from None
    deadline = time.monotonic() + seconds
    try:
        process = subprocess.Popen(
            argv,
            stdin=None,
            stdout=None,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        return 127
    try:
        return process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return 124


def hard_timeout(_signum: int, _frame: FrameType | None) -> NoReturn:
    """End PID 1 immediately; container teardown owns any remaining descendants."""

    os._exit(124)


def arm_deadline() -> None:
    """Arm the independent PID-1 deadline before any possibly blocking launch work."""

    _ALARM.signal(_ALARM.SIGALRM, hard_timeout)
    _ALARM.setitimer(_ALARM.ITIMER_REAL, 30.0)


def disable_dumpability() -> None:
    """Prevent same-UID tracing from disarming the watchdog after admission."""

    library = ctypes.CDLL(None, use_errno=True)
    try:
        prctl = library.prctl
    except AttributeError as error:
        raise OSError("supervisor privacy unavailable") from error
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE, disabled.
        raise OSError("supervisor privacy unavailable")


def entrypoint(arguments: tuple[str, ...]) -> int:
    """Admit one fixed Linux PID-1 operation and return its terminal exit status."""

    if not _IS_LINUX or os.getpid() != 1:
        return 126
    try:
        command = worker_argv(arguments)
    except ValueError:
        return 126
    try:
        arm_deadline()
        disable_dumpability()
    except (OSError, ValueError):
        return 126
    return supervise(command, seconds=30.0)


def main() -> NoReturn:
    """Run the fixed guest entrypoint and exit without cooperative flushing."""

    try:
        status = entrypoint(tuple(sys.argv[1:]))
    except BaseException:
        status = 127
    os._exit(status)


if __name__ == "__main__":
    main()
