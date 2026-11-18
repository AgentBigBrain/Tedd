#!/usr/bin/env python3
"""Cross-platform filesystem primitives for Tedd's local graph runtime.

POSIX ownership and mode bits remain authoritative on macOS and Linux.  On
Windows, the equivalent checks use the object's owner SID and DACL.  Windows
callers therefore need to pass a path (rather than an ``os.stat_result``) to
the ownership and access helpers so the security descriptor can be inspected.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Protocol, cast

import windows_acl_inspection as _acl
import windows_acl_policy as _windows_acl_policy

for name, module in (("windows_acl_policy", _windows_acl_policy), ("windows_acl_inspection", _acl)):
    expected = (Path(__file__).resolve().parent / (name + ".py")).resolve()
    loaded = Path(getattr(module, "__file__", "")).resolve()
    if loaded != expected:
        raise ImportError(f"refusing unexpected {name} module: {loaded} (expected {expected})")
_ADMINISTRATORS_SID = _acl.ADMINISTRATORS_SID
_OWNER_RIGHTS_SID = _acl.OWNER_RIGHTS_SID
_SYSTEM_SID = _acl.SYSTEM_SID
_TRUSTED_INSTALLER_SID = _acl.TRUSTED_INSTALLER_SID
_WindowsAce = _acl.WindowsAce
_WindowsSecuritySnapshot = _acl.WindowsSecuritySnapshot
_codex_sandbox_sid = _acl.codex_sandbox_sid
_current_user_sid = _acl.current_user_sid
_raise_windows_error = _acl.raise_windows_error
_trusted_private_sids = _acl.trusted_private_sids
_windows_descriptor_security_snapshot = _acl.descriptor_security_snapshot
_windows_security_snapshot = _acl.path_security_snapshot
_CONTAINER_INHERIT_ACE = _windows_acl_policy.CONTAINER_INHERIT_ACE
_INHERIT_ONLY_ACE = _windows_acl_policy.INHERIT_ONLY_ACE
_OBJECT_INHERIT_ACE = _windows_acl_policy.OBJECT_INHERIT_ACE
PrivateAclPrincipals = _windows_acl_policy.PrivateAclPrincipals
WINDOWS_MUTATING_ACCESS = _windows_acl_policy.WINDOWS_MUTATING_ACCESS
expanded_file_mask = _windows_acl_policy.expanded_file_mask
_evaluate_private_acl = _windows_acl_policy.private_acl_problem


IS_WINDOWS = os.name == "nt"
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class _FcntlOperations(Protocol):
    LOCK_SH: int
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class LockableFile(Protocol):
    def fileno(self) -> int: ...


if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    fcntl: _FcntlOperations | None = None

    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    import fcntl as _fcntl

    fcntl = cast(_FcntlOperations, _fcntl)


# Windows security and filesystem constants.  Keeping them here avoids a
# dependency on pywin32 while still using the operating system's real access
# control boundary.
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_LOCKED = 158
_DRIVE_REMOTE = 4
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_APPEND_DATA = 0x00000004
_FILE_READ_EA = 0x00000008
_FILE_WRITE_EA = 0x00000010
_FILE_EXECUTE = 0x00000020
_FILE_DELETE_CHILD = 0x00000040
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_ALL = 0x10000000
_GENERIC_EXECUTE = 0x20000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80

_WINDOWS_READ_RIGHTS = (
    _FILE_READ_DATA | _FILE_READ_EA | _FILE_READ_ATTRIBUTES | _GENERIC_READ | _GENERIC_ALL
)
_WINDOWS_WRITE_RIGHTS = (
    _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_WRITE_EA
    | _FILE_DELETE_CHILD
    | _FILE_WRITE_ATTRIBUTES
    | _DELETE
    | _WRITE_DAC
    | _WRITE_OWNER
    | _GENERIC_WRITE
    | _GENERIC_ALL
)
_WINDOWS_EXECUTE_RIGHTS = _FILE_EXECUTE | _GENERIC_EXECUTE | _GENERIC_ALL


if IS_WINDOWS:

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _ADVAPI32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _ADVAPI32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _ADVAPI32.SetSecurityInfo.restype = wintypes.DWORD
    _ADVAPI32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _ADVAPI32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _ADVAPI32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _ADVAPI32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.ReOpenFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _KERNEL32.ReOpenFile.restype = wintypes.HANDLE
    _KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32.LocalFree.restype = ctypes.c_void_p
    _KERNEL32.GetVolumePathNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _KERNEL32.GetVolumePathNameW.restype = wintypes.BOOL
    _KERNEL32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetDriveTypeW.restype = wintypes.UINT
    _KERNEL32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _KERNEL32.GetVolumeInformationW.restype = wintypes.BOOL
    _KERNEL32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _KERNEL32.LockFileEx.restype = wintypes.BOOL
    _KERNEL32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _KERNEL32.UnlockFileEx.restype = wintypes.BOOL


def uses_posix_permissions() -> bool:
    """Return whether owner and mode-bit checks describe real access control."""

    return not IS_WINDOWS


def configure_utf8_standard_streams() -> None:
    """Make CLI JSON streams UTF-8 even under a legacy Windows code page."""

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            # Embedded/captured streams may expose reconfigure while refusing
            # encoding changes.  Their host owns encoding in that case.
            continue


def is_link_or_reparse_point(path: Path) -> bool:
    """Reject POSIX symlinks and every Windows reparse-point redirect."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(IS_WINDOWS and attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _path_subject(subject: object) -> Path | None:
    """Return a path for path-like subjects without accepting stat metadata."""

    if isinstance(subject, (str, os.PathLike)):
        return Path(subject)
    return None


def owner_is_current_user(
    subject: os.stat_result | str | os.PathLike[str] | int,
    *,
    allow_root: bool = False,
) -> bool:
    """Check path ownership on Windows and UID ownership on POSIX.

    A Windows ``os.stat_result`` does not carry an owner SID.  Passing one is
    therefore treated as unsafe rather than silently skipping the check.
    """

    path = _path_subject(subject)
    if IS_WINDOWS:
        if isinstance(subject, int):
            snapshot = _windows_descriptor_security_snapshot(subject)
        elif path is not None:
            snapshot = _windows_security_snapshot(path)
        else:
            return False
        if not snapshot.dacl_present or not snapshot.owner_sid:
            return False
        allowed = {_current_user_sid()}
        if allow_root:
            allowed.update(
                {
                    _SYSTEM_SID,
                    _ADMINISTRATORS_SID,
                    _TRUSTED_INSTALLER_SID,
                }
            )
        return snapshot.owner_sid in allowed

    if isinstance(subject, int):
        metadata = os.fstat(subject)
    elif path is not None:
        metadata = path.stat()
    elif isinstance(subject, os.stat_result):
        metadata = subject
    else:
        raise TypeError("Ownership checks require a path, descriptor, or stat result")
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return True
    current_uid: object = getuid()
    if not isinstance(current_uid, int):
        raise TypeError("os.getuid() must return an integer")
    allowed_uids = {current_uid}
    if allow_root:
        allowed_uids.add(0)
    return metadata.st_uid in allowed_uids


def _requested_windows_rights(mask: int) -> int:
    """Translate requested POSIX access classes to Windows file rights."""

    requested = 0
    if mask & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        requested |= _WINDOWS_READ_RIGHTS
    if mask & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        requested |= _WINDOWS_WRITE_RIGHTS
    if mask & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        requested |= _WINDOWS_EXECUTE_RIGHTS
    return requested


def private_windows_acl_problem(
    subject: os.stat_result | str | os.PathLike[str] | int,
    *,
    is_directory: bool | None = None,
    require_protected: bool = True,
) -> str | None:
    """Return why a Windows path or descriptor ACL is not private.

    POSIX callers receive ``None`` because ownership and mode checks remain the
    authoritative policy there.  A Windows ``os.stat_result`` cannot expose an
    owner SID or DACL and therefore fails closed.
    """

    if not IS_WINDOWS:
        return None
    path = _path_subject(subject)
    if isinstance(subject, int):
        snapshot = _windows_descriptor_security_snapshot(subject)
        metadata = os.fstat(subject) if is_directory is None else None
    elif path is not None:
        snapshot = _windows_security_snapshot(path)
        metadata = path.stat() if is_directory is None else None
    else:
        return "requires a path or open descriptor for Windows ACL inspection"
    directory = (
        stat.S_ISDIR(metadata.st_mode)
        if is_directory is None and metadata is not None
        else bool(is_directory)
    )
    problem: object = _evaluate_private_acl(
        snapshot,
        PrivateAclPrincipals(
            current_user_sid=_current_user_sid(),
            owner_rights_sid=_OWNER_RIGHTS_SID,
            trusted_sids=_trusted_private_sids(),
            sandbox_sid=_codex_sandbox_sid(),
        ),
        is_directory=directory,
        require_protected=require_protected,
    )
    if problem is not None and not isinstance(problem, str):
        raise TypeError("Windows ACL policy must return text or None")
    return problem


def mode_has_any(subject: os.stat_result | str | os.PathLike[str] | int, mask: int) -> bool:
    """Test POSIX mode bits or equivalent broad Windows DACL grants.

    On Windows, an access-allowed ACE for any principal outside the private
    principal set counts as group/other access.  CodexSandboxUsers may retain
    read/execute access, but any requested mutation right counts as broad access.
    Child-reaching inherit-only ACEs are included for directories.  A missing or
    null DACL is public access, and stat-only Windows calls fail closed whenever
    the supplied mask asks about read, write, or execute access.
    """

    path = _path_subject(subject)
    if not IS_WINDOWS:
        if isinstance(subject, int):
            metadata = os.fstat(subject)
        elif path is not None:
            metadata = path.stat()
        elif isinstance(subject, os.stat_result):
            metadata = subject
        else:
            raise TypeError("Mode checks require a path, descriptor, or stat result")
        return bool(metadata.st_mode & mask)

    requested = _requested_windows_rights(mask)
    if not requested:
        return False
    if isinstance(subject, int):
        snapshot = _windows_descriptor_security_snapshot(subject)
        metadata = os.fstat(subject)
    elif path is not None:
        snapshot = _windows_security_snapshot(path)
        metadata = path.stat()
    else:
        return True
    if not snapshot.dacl_present:
        return True
    trusted = _trusted_private_sids()
    sandbox = _codex_sandbox_sid()
    is_directory = stat.S_ISDIR(metadata.st_mode)
    for ace in snapshot.allowed_aces:
        applies_here = not ace.flags & _INHERIT_ONLY_ACE
        can_reach_child = is_directory and bool(
            ace.flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
        )
        if not applies_here and not can_reach_child:
            continue
        expanded = expanded_file_mask(ace.mask)
        if ace.sid == sandbox and sandbox is not None:
            if requested & _WINDOWS_WRITE_RIGHTS and expanded & WINDOWS_MUTATING_ACCESS:
                return True
            continue
        if (ace.sid is None or ace.sid not in trusted) and expanded & requested:
            return True
    return False


def _private_windows_sddl(*, directory: bool = False) -> str:
    """Build the owner-aware protected private DACL used for graph state.

    Directory ACEs inherit to both files and subdirectories so SQLite-created
    WAL/SHM files cannot fall back to a broader token default DACL.  File ACEs
    intentionally remain non-inheritable.
    """

    inheritance = "OICI" if directory else ""
    current_user = _current_user_sid()
    entries = [
        f"(A;{inheritance};FA;;;{current_user})",
        f"(A;{inheritance};FA;;;SY)",
        f"(A;{inheritance};FA;;;BA)",
    ]
    sandbox = _codex_sandbox_sid()
    if sandbox:
        # Read/execute plus synchronize lets Codex sandbox processes traverse
        # graph directories without granting mutation rights.
        entries.append(f"(A;{inheritance};0x001200A9;;;{sandbox})")
    return f"O:{current_user}D:P" + "".join(entries)


def _dacl_from_sddl(sddl: str) -> tuple[int, int]:
    """Allocate a security descriptor and return its descriptor/DACL pointers."""

    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    dacl = ctypes.c_void_p()
    if not _ADVAPI32.GetSecurityDescriptorDacl(
        descriptor,
        ctypes.byref(dacl_present),
        ctypes.byref(dacl),
        ctypes.byref(dacl_defaulted),
    ):
        _KERNEL32.LocalFree(descriptor)
        raise ctypes.WinError(ctypes.get_last_error())
    if not dacl_present.value or not dacl.value:
        _KERNEL32.LocalFree(descriptor)
        raise OSError("Private security descriptor did not contain a DACL")
    descriptor_pointer = descriptor.value
    dacl_pointer = dacl.value
    if descriptor_pointer is None or dacl_pointer is None:
        _KERNEL32.LocalFree(descriptor)
        raise OSError("Private security descriptor returned a null pointer")
    return descriptor_pointer, dacl_pointer


def _owner_from_security_descriptor(descriptor: int) -> int:
    """Return the non-null owner SID pointer stored in a live descriptor."""

    owner = ctypes.c_void_p()
    owner_defaulted = wintypes.BOOL()
    if not _ADVAPI32.GetSecurityDescriptorOwner(
        ctypes.c_void_p(descriptor),
        ctypes.byref(owner),
        ctypes.byref(owner_defaulted),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not owner.value:
        raise OSError("Private security descriptor did not contain an owner")
    return owner.value


def _set_windows_dacl_from_sddl(path: Path, sddl: str, *, set_owner: bool = False) -> None:
    """Apply a protected DACL and, when requested, its owner to a path.

    The default remains DACL-only so focused adversarial-ACL setup cannot
    silently transfer ownership merely because its SDDL contains an owner.
    """

    descriptor, dacl = _dacl_from_sddl(sddl)
    try:
        owner = _owner_from_security_descriptor(descriptor) if set_owner else None
        security_information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if set_owner:
            security_information |= _OWNER_SECURITY_INFORMATION
        result = _ADVAPI32.SetNamedSecurityInfoW(
            os.fspath(path),
            _SE_FILE_OBJECT,
            security_information,
            ctypes.c_void_p(owner) if owner else None,
            None,
            ctypes.c_void_p(dacl),
            None,
        )
        if result:
            _raise_windows_error(result, path)
    finally:
        _KERNEL32.LocalFree(ctypes.c_void_p(descriptor))


class MutationGuardUnavailable(OSError):
    """This platform has no supported uncooperative-writer exclusion primitive."""


def _windows_read_descriptor(path: Path, access: int, flags: int) -> int:
    """Open an existing object while denying writes/renames; transfer handle ownership."""
    if not IS_WINDOWS:
        raise MutationGuardUnavailable(errno.ENOTSUP, "Windows descriptor unavailable")
    handle = _KERNEL32.CreateFileW(str(path), access, _FILE_SHARE_READ, None, 3, flags, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _KERNEL32.CloseHandle(handle)
        raise


def create_private_exclusive_file(path: Path) -> int:
    """Create a private file exclusively, with no unprotected Windows interval."""

    if not IS_WINDOWS:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    descriptor, _ = _dacl_from_sddl(_private_windows_sddl())
    try:
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        handle = _KERNEL32.CreateFileW(
            str(path),
            _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            ctypes.byref(security),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), path)
            raise ctypes.WinError(error)
        try:
            file_descriptor = msvcrt.open_osfhandle(
                handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
            )
        except BaseException:
            _KERNEL32.CloseHandle(handle)
            raise
        try:
            if not owner_is_current_user(file_descriptor) or private_windows_acl_problem(
                file_descriptor, is_directory=False
            ):
                raise PermissionError("New Windows file is not private")
        except BaseException:
            os.close(file_descriptor)
            raise
        return file_descriptor
    finally:
        _KERNEL32.LocalFree(ctypes.c_void_p(descriptor))


@contextmanager
def guarded_input_descriptor(path: Path) -> Iterator[int]:
    """Anchor every path component before consuming a selected original.

    POSIX walks relative to no-follow directory descriptors. Windows pins each
    ancestor without write/delete sharing before opening its child. Shared input
    ACLs are not changed. Busy or unsupported paths fail; there is no reopen fallback.
    """
    if not path.is_absolute() or ".." in path.parts or len(path.parents) > 128:
        raise OSError(errno.EINVAL, "invalid input path")
    if not IS_WINDOWS and (
        os.open not in os.supports_dir_fd
        or not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
    ):
        raise MutationGuardUnavailable(errno.ENOTSUP, "anchored input unavailable")
    with ExitStack() as stack:
        parent_fd: int | None = None
        for parent in reversed(path.parents):
            if IS_WINDOWS:
                fd = _windows_read_descriptor(parent, _FILE_READ_ATTRIBUTES, 0x02200000)
            else:
                flags = (
                    os.O_RDONLY
                    | int(getattr(os, "O_DIRECTORY", 0))
                    | int(getattr(os, "O_NOFOLLOW", 0))
                )
                fd = os.open(
                    str(parent) if parent_fd is None else parent.name, flags, dir_fd=parent_fd
                )
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OSError(errno.ELOOP, "input ancestor is not a plain directory")
            parent_fd = fd
        if IS_WINDOWS:
            fd = _windows_read_descriptor(path, _GENERIC_READ, 0x00200000)
        else:
            flags = (
                os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_NONBLOCK", 0))
            )
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(errno.ELOOP, "input is not a plain file")
        yield fd


@contextmanager
def guarded_file_mutation(path: Path) -> Iterator[int]:
    """Pin one Windows file and exclude concurrent writes, renames, and deletion.

    The caller must verify descriptor identity, privacy and bytes before marking
    deletion. This is not a POSIX advisory lock: unsupported platforms refuse.
    Existing readers or writers with incompatible sharing cause a real conflict.
    """
    if not IS_WINDOWS:
        raise MutationGuardUnavailable(errno.ENOTSUP, "guarded mutation unavailable")
    descriptor = _windows_read_descriptor(path, _GENERIC_READ | _DELETE, 0x00200000)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def delete_guarded_file(descriptor: int) -> None:
    """Mark the already-verified guarded object, never re-resolve its pathname."""
    if not IS_WINDOWS:
        raise MutationGuardUnavailable(errno.ENOTSUP, "guarded mutation unavailable")
    disposition = wintypes.BOOLEAN(1)
    if not _KERNEL32.SetFileInformationByHandle(
        wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),  # FileDispositionInfo
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _set_windows_private_handle(descriptor: int) -> None:
    """Apply the private owner and protected DACL to an open descriptor."""

    security_descriptor, dacl = _dacl_from_sddl(_private_windows_sddl())
    try:
        set_owner = (
            _windows_descriptor_security_snapshot(descriptor).owner_sid != _current_user_sid()
        )
        owner = _owner_from_security_descriptor(security_descriptor) if set_owner else None
        raw_handle = msvcrt.get_osfhandle(descriptor)
        if raw_handle == -1:
            raise OSError(errno.EBADF, "Invalid Windows file descriptor")
        desired_access = _READ_CONTROL | _WRITE_DAC
        if set_owner:
            desired_access |= _WRITE_OWNER
        security_handle = _KERNEL32.ReOpenFile(
            wintypes.HANDLE(raw_handle),
            desired_access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            0,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if not security_handle or security_handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            security_information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
            if set_owner:
                security_information |= _OWNER_SECURITY_INFORMATION
            result = _ADVAPI32.SetSecurityInfo(
                security_handle,
                _SE_FILE_OBJECT,
                security_information,
                ctypes.c_void_p(owner) if owner else None,
                None,
                ctypes.c_void_p(dacl),
                None,
            )
            if result:
                _raise_windows_error(result)
        finally:
            _KERNEL32.CloseHandle(security_handle)
    finally:
        _KERNEL32.LocalFree(ctypes.c_void_p(security_descriptor))


def set_private_path_mode(path: Path, mode: int) -> None:
    """Apply a private POSIX mode or protected Windows DACL."""

    if not IS_WINDOWS:
        os.chmod(path, mode)
        return
    if is_link_or_reparse_point(path):
        raise ValueError(f"Cannot apply a private ACL through a reparse point: {path}")
    directory = path.is_dir()
    set_owner = _windows_security_snapshot(path).owner_sid != _current_user_sid()
    _set_windows_dacl_from_sddl(
        path,
        _private_windows_sddl(directory=directory),
        set_owner=set_owner,
    )
    problem = private_windows_acl_problem(path, is_directory=directory)
    if problem is not None:
        raise PermissionError(f"Could not enforce a private Windows ACL: {path}")


def set_private_fd_mode(descriptor: int, mode: int) -> None:
    """Apply a private POSIX descriptor mode or protected Windows DACL."""

    if not IS_WINDOWS:
        fchmod = getattr(os, "fchmod", None)
        if not callable(fchmod):
            raise NotImplementedError("Descriptor chmod is unavailable")
        fchmod(descriptor, mode)
        return
    _set_windows_private_handle(descriptor)
    problem = private_windows_acl_problem(descriptor, is_directory=False)
    if problem is not None:
        raise PermissionError("Could not enforce a private Windows descriptor ACL")


def _windows_file_handle(handle: LockableFile) -> int:
    """Return the native Windows handle for a Python file object."""

    raw_handle = msvcrt.get_osfhandle(handle.fileno())
    if raw_handle == -1:
        raise OSError(errno.EBADF, "Invalid Windows file handle")
    return int(raw_handle)


def try_file_lock(handle: LockableFile, *, shared: bool = False) -> None:
    """Attempt one non-blocking process lock on the supplied open file.

    POSIX retains ``flock`` shared/exclusive behavior.  Windows uses
    ``LockFileEx`` over a one-byte range beyond-or-within EOF; unlike
    ``msvcrt.locking``, this supports shared locks and never writes a sentinel
    byte into an empty lock file.
    """

    if not IS_WINDOWS:
        assert fcntl is not None
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
        return

    overlapped = _OVERLAPPED()
    flags = _LOCKFILE_FAIL_IMMEDIATELY
    if not shared:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    if _KERNEL32.LockFileEx(
        wintypes.HANDLE(_windows_file_handle(handle)),
        flags,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        return
    code = ctypes.get_last_error()
    error = ctypes.WinError(code)
    if code in {_ERROR_LOCK_VIOLATION, _ERROR_NOT_LOCKED}:
        raise BlockingIOError(errno.EAGAIN, str(error)) from error
    raise error


def unlock_file(handle: LockableFile) -> None:
    """Release a lock acquired by :func:`try_file_lock`."""

    if not IS_WINDOWS:
        assert fcntl is not None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    overlapped = _OVERLAPPED()
    if not _KERNEL32.UnlockFileEx(
        wintypes.HANDLE(_windows_file_handle(handle)),
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _nearest_existing_windows_path(path: Path) -> Path:
    """Return the closest existing path without resolving reparse points."""

    candidate = Path(os.path.abspath(os.fspath(path)))
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _windows_volume_root(path: Path) -> str | None:
    """Return the Win32 volume root containing a local or UNC path."""

    candidate = _nearest_existing_windows_path(path)
    buffer = ctypes.create_unicode_buffer(32768)
    if _KERNEL32.GetVolumePathNameW(os.fspath(candidate), buffer, len(buffer)) and buffer.value:
        return buffer.value
    anchor = candidate.anchor
    if anchor:
        return anchor if anchor.endswith("\\") else f"{anchor}\\"
    return None


def _windows_drive_type(root: str) -> int:
    """Return the Win32 drive category for a volume root."""

    return int(_KERNEL32.GetDriveTypeW(root))


def _windows_filesystem_name(root: str) -> str | None:
    """Return the filesystem label reported for a Windows volume."""

    filesystem = ctypes.create_unicode_buffer(64)
    if not _KERNEL32.GetVolumeInformationW(
        root,
        None,
        0,
        None,
        None,
        None,
        filesystem,
        len(filesystem),
    ):
        return None
    return filesystem.value.casefold() or None


def windows_volume_type(path: Path) -> str | None:
    """Return a local Windows filesystem label or remote-volume sentinel."""

    if not IS_WINDOWS:
        return None
    try:
        root = _windows_volume_root(path)
        if not root:
            return None
        if _windows_drive_type(root) == _DRIVE_REMOTE:
            return "windows-remote"
        return _windows_filesystem_name(root)
    except (AttributeError, OSError, ValueError):
        return None


def is_trusted_windows_volume_root(path: Path) -> bool:
    """Allow only a local, system-owned Windows volume root as an ancestor.

    A normal Windows drive root grants Authenticated Users permission to add a
    top-level child while denying deletion or ACL mutation of existing child
    trees.  That is not equivalent to a POSIX world-writable ancestor.  This
    narrow exception ignores only add-file/add-directory rights on the root;
    any broader untrusted write grant still fails.
    """

    if not IS_WINDOWS:
        return False
    candidate = Path(os.path.abspath(os.fspath(path)))
    if not candidate.anchor or candidate != Path(candidate.anchor):
        return False
    try:
        root = _windows_volume_root(candidate)
        if not root or _windows_drive_type(root) == _DRIVE_REMOTE:
            return False
        snapshot = _windows_security_snapshot(candidate)
        allowed_owners = {
            _current_user_sid(),
            _SYSTEM_SID,
            _ADMINISTRATORS_SID,
            _TRUSTED_INSTALLER_SID,
        }
        if not snapshot.dacl_present or snapshot.owner_sid not in allowed_owners:
            return False
        dangerous_root_write = _WINDOWS_WRITE_RIGHTS & ~(_FILE_WRITE_DATA | _FILE_APPEND_DATA)
        trusted = _trusted_private_sids()
        sandbox = _codex_sandbox_sid()
        for ace in snapshot.allowed_aces:
            if ace.flags & _INHERIT_ONLY_ACE:
                continue
            expanded = expanded_file_mask(ace.mask)
            if sandbox is not None and ace.sid == sandbox and expanded & dangerous_root_write:
                return False
            if (ace.sid is None or ace.sid not in trusted) and (expanded & dangerous_root_write):
                return False
        return True
    except OSError:
        return False
