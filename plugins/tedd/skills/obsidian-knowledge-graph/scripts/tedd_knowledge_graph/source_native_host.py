"""Private pinned helper-file ownership shared by optional native image tools."""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Protocol, cast

from platform_support import guarded_input_descriptor, is_link_or_reparse_point

from .source_contracts import SourceArchiveError
from .source_files import check_entry
from .source_macos_host import require_worker_boundary
from .source_pdf_host import PosixResource

_IMAGE_MEMORY = 2 * 1024**3
_NATIVE_PARENT_MEMORY = 512 * 1024**2
_IMAGE_SECONDS = 30
_NATIVE_PARENT_SECONDS = 20
_IS_LINUX = sys.platform == "linux"
_MAX_HELPER_BYTES = 32 * 1024**2


class _CpuUsage(Protocol):
    ru_utime: float
    ru_stime: float


class _NativeResource(PosixResource, Protocol):
    RUSAGE_SELF: int
    RUSAGE_CHILDREN: int

    def getrlimit(self, resource: int) -> tuple[int, int]: ...
    def getrusage(self, who: int) -> _CpuUsage: ...
    def getpagesize(self) -> int: ...


class _LinuxFcntl(Protocol):
    def fcntl(self, fd: int, command: int, argument: int = 0) -> int: ...


def require_linux_exec_privacy() -> int:
    """An unreadable executable uses the kernel's suid_dumpable policy."""
    effective_uid = getattr(os, "geteuid", None)
    if not _IS_LINUX or not callable(effective_uid) or os.access not in os.supports_effective_ids:
        raise SourceArchiveError("capability_unavailable")
    uid = effective_uid()
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise SourceArchiveError("privacy_rejected")
    # RLIMIT_CORE alone does not stop a piped core collector; never change the
    # host policy or silently launch the ordinary readable executable instead.
    with Path("/proc/sys/fs/suid_dumpable").open("rb") as policy:
        if policy.read(3) != b"0\n":
            raise SourceArchiveError("privacy_rejected")
    return uid


@contextmanager
def sealed_linux_executable(source_fd: int, expected: str) -> Iterator[int]:
    """Copy only a pinned descriptor into bounded, sealed execute-only memory."""
    uid = require_linux_exec_privacy()
    create = getattr(os, "memfd_create", None)
    set_mode = getattr(os, "fchmod", None)
    if not callable(create) or not callable(set_mode):
        raise SourceArchiveError("capability_unavailable")
    fcntl = cast(_LinuxFcntl, importlib.import_module("fcntl"))
    # Linux UAPI constants, including seals absent from older Python bindings.
    fd = create("tedd-native", 0x0001 | 0x0002)  # CLOEXEC | ALLOW_SEALING
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise SourceArchiveError("capability_unavailable")
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while block := os.read(source_fd, 1024**2):
            total += len(block)
            if total > _MAX_HELPER_BYTES:
                raise SourceArchiveError("capability_unavailable")
            digest.update(block)
            pending = memoryview(block)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise SourceArchiveError("capability_unavailable")
                pending = pending[written:]
        if not total or digest.hexdigest() != expected:
            raise SourceArchiveError("capability_unavailable")
        set_mode(fd, 0o100)
        fcntl.fcntl(fd, 1033, 0x000F)  # ADD_SEALS: SEAL | SHRINK | GROW | WRITE
        info = os.fstat(fd)
        executable = f"/proc/self/fd/{fd}"
        if (
            fcntl.fcntl(fd, 1034) & 0x000F != 0x000F  # GET_SEALS
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o100
            or info.st_uid != uid
            or info.st_size != total
            or os.stat(executable) != info
            or os.access(executable, os.R_OK, effective_ids=True)
        ):
            # Effective-access checking also rejects read-bypass capabilities.
            raise SourceArchiveError("privacy_rejected")
        yield fd
    finally:
        os.close(fd)


def run_native_helper(
    arguments: list[str],
    payload: bytes,
    files: tuple[tuple[Path, str], ...],
    deadline: float | None,
) -> subprocess.CompletedProcess[bytes]:
    """Keep pins, limits, the sealed launch copy and child under one owner."""
    require_worker_boundary()
    if not files or not arguments or arguments[0] != str(files[0][0]):
        raise SourceArchiveError("capability_unavailable")
    with ExitStack() as held:
        descriptors = held.enter_context(pinned_helper_files(files))
        command = held.enter_context(
            native_child_arguments(
                arguments, deadline, reserved_memory=_MAX_HELPER_BYTES if _IS_LINUX else 0
            )
        )
        inherited: tuple[int, ...] = ()
        if _IS_LINUX:
            fd = held.enter_context(sealed_linux_executable(descriptors[0], files[0][1]))
            command = [*command[: -len(arguments)], f"/proc/self/fd/{fd}", *arguments[1:]]
            inherited = (fd,)
            require_linux_exec_privacy()
        result = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=files[0][0].parent,
            env={key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ},
            timeout=remaining_native_seconds(deadline),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            pass_fds=inherited,
        )
        remaining_native_seconds(deadline)
        return result


def run_image_worker(
    arguments: list[str], payload: bytes, *, timeout: float = 30
) -> subprocess.CompletedProcess[bytes]:
    """Own the worker session; a timeout must not leave native children alive."""
    require_worker_boundary()
    with subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ},
        start_new_session=sys.platform != "win32",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        try:
            output, _ = process.communicate(payload, timeout=timeout)
            return subprocess.CompletedProcess(arguments, process.returncode, output)
        finally:
            # Only the explicitly created session, never the caller's group.
            # Windows owns descendants through the worker's kill-on-close Job.
            if sys.platform != "win32":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@contextmanager
def native_child_arguments(
    arguments: list[str], deadline: float | None, *, reserved_memory: int = 0
) -> Iterator[list[str]]:
    """Allocate one serial child, then restore the worker's memory allowance.

    Linux rlimits are per process, not a Job. During native execution the parent
    gets 512 MiB and the child at most 1536 MiB; the parent regains 2 GiB only after the
    child is reaped. Hard CPU limits allocate 20 seconds to the parent and ten
    cumulatively to native children, rounding prior child usage up. No soft-limit
    Python signal handler must interrupt a native extension to enforce the total.
    These are trusted-helper resource limits, not a security sandbox.
    """
    require_worker_boundary()
    if not _IS_LINUX or deadline is None:
        yield arguments
        return
    if not 0 <= reserved_memory <= _MAX_HELPER_BYTES:
        raise SourceArchiveError("capability_unavailable")
    resource = cast(_NativeResource, importlib.import_module("resource"))

    remaining_native_seconds(deadline)
    memory = resource.getrlimit(resource.RLIMIT_AS)
    cpu = resource.getrlimit(resource.RLIMIT_CPU)
    if memory != (_IMAGE_MEMORY, _IMAGE_MEMORY) or cpu not in (
        (_IMAGE_SECONDS, _IMAGE_SECONDS),
        (_NATIVE_PARENT_SECONDS, _NATIVE_PARENT_SECONDS),
    ):
        raise SourceArchiveError("capability_unavailable")
    # Lowering RLIMIT_AS does not release existing mappings. Refuse an already
    # over-allocation parent before claiming that the two limits add up.
    with Path("/proc/self/statm").open("rb") as status:
        mapped = int(status.read(256).split()[0]) * resource.getpagesize()
    if mapped > _NATIVE_PARENT_MEMORY:
        raise SourceArchiveError("limit_exceeded")

    used = resource.getrusage(resource.RUSAGE_SELF)
    prior = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = _IMAGE_SECONDS - _NATIVE_PARENT_SECONDS - math.ceil(prior.ru_utime + prior.ru_stime)
    if child_cpu < 1 or used.ru_utime + used.ru_stime >= _NATIVE_PARENT_SECONDS:
        raise SourceArchiveError("limit_exceeded")
    resource.setrlimit(resource.RLIMIT_CPU, (_NATIVE_PARENT_SECONDS, _NATIVE_PARENT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (_NATIVE_PARENT_MEMORY, memory[1]))
    try:
        # A separate interpreter sets child limits before exec: no preexec_fn in
        # a threaded host and no shell, environment discovery or source code.
        bootstrap = (
            "import os,resource,sys; "
            "memory=int(sys.argv[1]); cpu=int(sys.argv[2]); "
            "resource.setrlimit(resource.RLIMIT_AS,(memory,memory)); "
            "resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu)); "
            "os.execv(sys.argv[3],sys.argv[3:])"
        )
        yield [
            sys.executable,
            "-I",
            "-B",
            "-c",
            bootstrap,
            # The memfd's kernel-backed pages are outside either process's AS
            # limit. Reserve their full bound, including during the copy phase.
            str(_IMAGE_MEMORY - _NATIVE_PARENT_MEMORY - reserved_memory),
            str(child_cpu),
            *arguments,
        ]
    finally:
        resource.setrlimit(resource.RLIMIT_AS, memory)
    remaining_native_seconds(deadline)


def remaining_native_seconds(deadline: object) -> float:
    """Share the worker deadline; a native child cannot start a fresh allowance."""
    if deadline is None:
        # Direct helper qualification has no enclosing image worker.
        return 25.0
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not 0 < deadline < float("inf")
    ):
        raise SourceArchiveError("capability_unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceArchiveError("limit_exceeded")
    return min(25.0, remaining)


@contextmanager
def pinned_helper_files(files: tuple[tuple[Path, str], ...]) -> Iterator[tuple[int, ...]]:
    """Hold each verified private single-link file until native execution ends."""
    with ExitStack() as held:
        descriptors: list[int] = []
        for path, expected in files:
            if not path.is_absolute() or any(
                is_link_or_reparse_point(parent) for parent in (path, *path.parents)
            ):
                raise SourceArchiveError("privacy_rejected")
            check_entry(path.parent, directory=True)
            before = check_entry(path)
            if before.st_size > _MAX_HELPER_BYTES:
                raise SourceArchiveError("capability_unavailable")
            fd = held.enter_context(guarded_input_descriptor(path))
            with os.fdopen(fd, "rb", closefd=False) as stream:
                actual = hashlib.sha256()
                total = 0
                while block := stream.read(1024**2):
                    total += len(block)
                    if total > _MAX_HELPER_BYTES:
                        raise SourceArchiveError("capability_unavailable")
                    actual.update(block)
            if actual.hexdigest() != expected:
                raise SourceArchiveError("capability_unavailable")
            descriptors.append(fd)
        yield tuple(descriptors)
