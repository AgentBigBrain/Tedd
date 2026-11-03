"""Bounded, content-free subprocess transport for the isolated reader."""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .source_contracts import SourceArchiveError

_REAP_RESERVE_SECONDS = 0.25
_WINDOWS_REQUIRED_ENVIRONMENT = ("SystemRoot", "WINDIR")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes


class _ProcessStopped(Exception):
    pass


def _child_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """Return the closed caller environment plus required Windows process state."""

    result = {} if environment is None else dict(environment)
    if os.name != "nt":
        return result
    for required in _WINDOWS_REQUIRED_ENVIRONMENT:
        value = os.environ.get(required)
        if value is None:
            continue
        for supplied in tuple(result):
            if supplied.casefold() == required.casefold():
                del result[supplied]
        result[required] = value
    return result


def _valid_deadline(deadline: float) -> float:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise SourceArchiveError("source_unavailable")
    return float(deadline)


class _ManagedProcess:
    """Keep both pipes moving independently until the child is reaped."""

    def __init__(
        self,
        arguments: tuple[str, ...],
        payload: bytes,
        output_limit: int,
        environment: Mapping[str, str] | None,
        umask: int = -1,
    ) -> None:
        if (
            not isinstance(arguments, tuple)
            or not arguments
            or any(not isinstance(argument, str) for argument in arguments)
            or not Path(arguments[0]).is_absolute()
            or not isinstance(payload, bytes)
            or type(output_limit) is not int
            or output_limit < 0
            or type(umask) is not int
            or not -1 <= umask <= 0o777
            or os.name == "nt"
            and umask != -1
            or environment is not None
            and any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in environment.items()
            )
        ):
            raise SourceArchiveError("source_unavailable")
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_child_environment(environment),
                bufsize=0,
                close_fds=True,
                umask=umask,
            )
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        self._payload = payload
        self._output_limit = output_limit
        self._output = bytearray()
        self._overflow = False
        self._discard = False
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._writer = threading.Thread(target=self._write_stdin, daemon=True)
        self._input_stopped = threading.Event()
        self._reader_started = False
        self._writer_started = False

    def start(self, deadline: float | None, cancelled: Event | None) -> None:
        if deadline is not None and self._stopped(deadline, cancelled):
            self.terminate_and_reap(deadline)
            raise SourceArchiveError("source_unavailable")
        self._reader.start()
        self._reader_started = True
        if deadline is not None and self._stopped(deadline, cancelled):
            self.terminate_and_reap(deadline)
            raise SourceArchiveError("source_unavailable")
        # Attached readers are Unix-socket scoped. Their input pipe must be
        # nonblocking so cancellation can stop writes while cleanup keeps the
        # child alive. Ordinary Windows calls still terminate the child on stop.
        if os.name != "nt":
            stream = self._process.stdin
            set_blocking = getattr(os, "set_blocking", None)
            if stream is None or not callable(set_blocking):
                self.terminate_and_reap(deadline if deadline is not None else time.monotonic() + 1)
                raise SourceArchiveError("source_unavailable")
            try:
                set_blocking(stream.fileno(), False)
            except OSError:
                self.terminate_and_reap(deadline if deadline is not None else time.monotonic() + 1)
                raise SourceArchiveError("source_unavailable") from None
        self._writer.start()
        self._writer_started = True

    def _write_stdin(self) -> None:
        stream = self._process.stdin
        if stream is None:
            return
        try:
            view = memoryview(self._payload)
            while view and not self._input_stopped.is_set():
                try:
                    if os.name == "nt":
                        written = stream.write(view[: 64 * 1024])
                    else:
                        written = os.write(stream.fileno(), view[: 64 * 1024])
                except BlockingIOError:
                    self._input_stopped.wait(0.01)
                    continue
                if written <= 0:
                    raise OSError()
                view = view[written:]
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                with self._lock:
                    if self._discard:
                        continue
                    if len(self._output) + len(chunk) > self._output_limit:
                        self._output.clear()
                        self._overflow = True
                        self._discard = True
                    else:
                        self._output.extend(chunk)
        except (OSError, ValueError):
            with self._lock:
                self._output.clear()
                self._overflow = True
                self._discard = True
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _cancelled(cancelled: Event | None) -> bool:
        if cancelled is None:
            return False
        check = getattr(cancelled, "is_set", None)
        if not callable(check):
            raise SourceArchiveError("source_unavailable")
        return bool(check())

    @classmethod
    def _stopped(cls, deadline: float, cancelled: Event | None) -> bool:
        return time.monotonic() >= deadline or cls._cancelled(cancelled)

    def _discard_output(self) -> None:
        with self._lock:
            self._output.clear()
            self._discard = True

    def stop_input(self) -> None:
        """Irreversibly stop submitting new payload bytes to the child."""

        self._input_stopped.set()

    def wait(
        self,
        deadline: float,
        cancelled: Event | None = None,
        *,
        leave_running_on_stop: bool = False,
    ) -> ProcessResult:
        deadline = _valid_deadline(deadline)
        remaining = max(0.0, deadline - time.monotonic())
        reap_reserve = min(_REAP_RESERVE_SECONDS, remaining / 2)
        work_deadline = deadline - reap_reserve
        while self._process.poll() is None:
            if self._cancelled(cancelled) or time.monotonic() >= work_deadline:
                self.stop_input()
                self._discard_output()
                if leave_running_on_stop:
                    raise _ProcessStopped()
                self.terminate_and_reap(deadline)
                raise SourceArchiveError("source_unavailable")
            time.sleep(min(0.01, max(0.0, work_deadline - time.monotonic())))
        self._join_pumps(deadline)
        with self._lock:
            if self._overflow:
                raise SourceArchiveError("limit_exceeded")
            output = bytes(self._output)
        return ProcessResult(int(self._process.returncode), output)

    def _join_pumps(self, deadline: float) -> None:
        threads = (
            *((self._writer,) if self._writer_started else ()),
            *((self._reader,) if self._reader_started else ()),
        )
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._discard_output()
                self.terminate_and_reap(deadline)
                raise SourceArchiveError("source_unavailable")

    def terminate_and_reap(self, deadline: float) -> None:
        deadline = _valid_deadline(deadline)
        self.stop_input()
        self._discard_output()
        if not self._writer_started:
            stdin = self._process.stdin
            if stdin is not None and not stdin.closed:
                try:
                    stdin.close()
                except OSError:
                    pass
        if not self._reader_started:
            stdout = self._process.stdout
            if stdout is not None and not stdout.closed:
                try:
                    stdout.close()
                except OSError:
                    pass
        if self._process.poll() is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
        remaining = max(0.0, deadline - time.monotonic())
        kill_at = time.monotonic() + min(0.1, remaining / 2)
        try:
            self._process.wait(timeout=max(0.0, kill_at - time.monotonic()))
        except subprocess.TimeoutExpired:
            if self._process.poll() is None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            try:
                self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise SourceArchiveError("source_unavailable") from None
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        if self._writer_started:
            self._writer.join(max(0.0, deadline - time.monotonic()))
        if self._reader_started:
            self._reader.join(max(0.0, deadline - time.monotonic()))


def _start_bounded(
    arguments: tuple[str, ...],
    payload: bytes,
    *,
    output_limit: int,
    environment: Mapping[str, str] | None = None,
    deadline: float | None = None,
    cancelled: Event | None = None,
    umask: int = -1,
) -> _ManagedProcess:
    if deadline is not None:
        deadline = _valid_deadline(deadline)
        if _ManagedProcess._stopped(deadline, cancelled):
            raise SourceArchiveError("source_unavailable")
    process = _ManagedProcess(arguments, payload, output_limit, environment, umask)
    try:
        process.start(deadline, cancelled)
    except RuntimeError as error:
        cleanup_deadline = deadline if deadline is not None else time.monotonic() + 1
        try:
            process.terminate_and_reap(cleanup_deadline)
        except Exception as cleanup_error:
            raise SourceArchiveError("source_unavailable") from cleanup_error
        raise SourceArchiveError("source_unavailable") from error
    return process


def run_bounded(
    arguments: tuple[str, ...],
    payload: bytes,
    *,
    deadline: float,
    output_limit: int,
    cancelled: Event | None = None,
    environment: Mapping[str, str] | None = None,
    umask: int = -1,
) -> ProcessResult:
    """Run one bounded child; an optional POSIX umask never changes the parent."""

    deadline = _valid_deadline(deadline)
    if time.monotonic() >= deadline or _ManagedProcess._cancelled(cancelled):
        raise SourceArchiveError("source_unavailable")
    process = _start_bounded(
        arguments,
        payload,
        output_limit=output_limit,
        environment=environment,
        deadline=deadline,
        cancelled=cancelled,
        umask=umask,
    )
    return process.wait(deadline, cancelled)
