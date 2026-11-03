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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

import windows_acl_policy as _windows_acl_policy

_expected_acl_policy = Path(__file__).with_name("windows_acl_policy.py").resolve()
_loaded_acl_policy = Path(getattr(_windows_acl_policy, "__file__", "")).resolve()
if _loaded_acl_policy != _expected_acl_policy:
    raise ImportError(
        "refusing unexpected windows_acl_policy module: "
        f"{_loaded_acl_policy} (expected {_expected_acl_policy})"
    )
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
_SE_DACL_PROTECTED = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPES = frozenset({0x00, 0x04, 0x05, 0x09, 0x0B})
_ACE_OBJECT_TYPE_PRESENT = 0x01
_ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x02
_ERROR_INSUFFICIENT_BUFFER = 122
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

_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_OWNER_RIGHTS_SID = "S-1-3-4"
_TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


@dataclass(frozen=True)
class _WindowsAce:
    """One access-allowed ACE extracted from a Windows DACL."""

    sid: str | None
    mask: int
    flags: int


@dataclass(frozen=True)
class _WindowsSecuritySnapshot:
    """Owner and DACL facts copied out of a Windows security descriptor."""

    owner_sid: str | None
    dacl_present: bool
    allowed_aces: tuple[_WindowsAce, ...]
    dacl_protected: bool = False


if IS_WINDOWS:

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER_BUFFER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _ADVAPI32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
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
    _ADVAPI32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.GetSecurityInfo.restype = wintypes.DWORD
    _ADVAPI32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _ADVAPI32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _ADVAPI32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetSecurityDescriptorControl.restype = wintypes.BOOL
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
    _ADVAPI32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _ADVAPI32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.LookupAccountNameW.restype = wintypes.BOOL
    _ADVAPI32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _ADVAPI32.GetAclInformation.restype = wintypes.BOOL
    _ADVAPI32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ADVAPI32.GetAce.restype = wintypes.BOOL
    _ADVAPI32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
    _ADVAPI32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
    _KERNEL32.GetCurrentProcess.argtypes = []
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
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


def _raise_windows_error(code: int, path: Path | None = None) -> None:
    """Raise an ``OSError`` for a Win32 status-returning API."""

    message = ctypes.FormatError(code).strip() if code else "Windows API failure"
    if path is None:
        raise OSError(code, message)
    raise OSError(code, message, str(path))


def _sid_to_string(sid: int | None) -> str | None:
    """Copy a binary Windows SID into stable string form."""

    if not sid:
        return None
    sid_text = wintypes.LPWSTR()
    if not _ADVAPI32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sid_text.value
    finally:
        _KERNEL32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    """Return the SID carried by the process token's user identity."""

    token = wintypes.HANDLE()
    if not _ADVAPI32.OpenProcessToken(
        _KERNEL32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        _ADVAPI32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        error = ctypes.get_last_error()
        if error != _ERROR_INSUFFICIENT_BUFFER or required.value == 0:
            raise ctypes.WinError(error)
        buffer = ctypes.create_string_buffer(required.value)
        if not _ADVAPI32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_BUFFER)).contents
        sid = _sid_to_string(token_user.User.Sid)
        if not sid:
            raise OSError("The current process token has no user SID")
        return sid
    finally:
        _KERNEL32.CloseHandle(token)


@lru_cache(maxsize=None)
def _lookup_account_sid(account_name: str) -> str | None:
    """Resolve an optional local account name to a SID string."""

    sid_size = wintypes.DWORD()
    domain_size = wintypes.DWORD()
    sid_use = wintypes.DWORD()
    _ADVAPI32.LookupAccountNameW(
        None,
        account_name,
        None,
        ctypes.byref(sid_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(sid_use),
    )
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or sid_size.value == 0:
        return None
    sid_buffer = ctypes.create_string_buffer(sid_size.value)
    domain_buffer = ctypes.create_unicode_buffer(max(1, domain_size.value))
    if not _ADVAPI32.LookupAccountNameW(
        None,
        account_name,
        sid_buffer,
        ctypes.byref(sid_size),
        domain_buffer,
        ctypes.byref(domain_size),
        ctypes.byref(sid_use),
    ):
        return None
    return _sid_to_string(ctypes.addressof(sid_buffer))


@lru_cache(maxsize=1)
def _codex_sandbox_sid() -> str | None:
    """Return the optional local SID Codex uses for sandbox read access."""

    computer = os.environ.get("COMPUTERNAME", "").strip()
    candidates = [f"{computer}\\CodexSandboxUsers"] if computer else []
    candidates.append("CodexSandboxUsers")
    for account in candidates:
        sid = _lookup_account_sid(account)
        if sid:
            return sid
    return None


def _trusted_private_sids() -> frozenset[str]:
    """Return principals allowed to access a Codex-private Windows object."""

    trusted = {
        _current_user_sid(),
        _SYSTEM_SID,
        _ADMINISTRATORS_SID,
        _OWNER_RIGHTS_SID,
        _TRUSTED_INSTALLER_SID,
    }
    sandbox = _codex_sandbox_sid()
    if sandbox:
        trusted.add(sandbox)
    return frozenset(trusted)


def _ace_sid_address(ace_address: int, ace_type: int, ace_size: int) -> int | None:
    """Locate the SID field in a supported access-allowed ACE layout."""

    offset = 8
    if ace_type == 0x04:
        offset = 12
    elif ace_type in {0x05, 0x0B}:
        if ace_size < 12:
            return None
        object_flags = ctypes.c_uint32.from_address(ace_address + 8).value
        offset = 12
        if object_flags & _ACE_OBJECT_TYPE_PRESENT:
            offset += 16
        if object_flags & _ACE_INHERITED_OBJECT_TYPE_PRESENT:
            offset += 16
    if offset >= ace_size:
        return None
    return ace_address + offset


def _allowed_aces(dacl: int) -> tuple[_WindowsAce, ...]:
    """Copy access-allowed ACEs out of a live ACL pointer."""

    information = _ACL_SIZE_INFORMATION()
    if not _ADVAPI32.GetAclInformation(
        ctypes.c_void_p(dacl),
        ctypes.byref(information),
        ctypes.sizeof(information),
        _ACL_SIZE_INFORMATION_CLASS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    copied: list[_WindowsAce] = []
    for index in range(information.AceCount):
        ace_pointer = ctypes.c_void_p()
        if not _ADVAPI32.GetAce(ctypes.c_void_p(dacl), index, ctypes.byref(ace_pointer)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not ace_pointer.value:
            continue
        header = ctypes.cast(ace_pointer, ctypes.POINTER(_ACE_HEADER)).contents
        if header.AceType not in _ACCESS_ALLOWED_ACE_TYPES or header.AceSize < 8:
            continue
        mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
        sid_address = _ace_sid_address(ace_pointer.value, header.AceType, header.AceSize)
        try:
            sid = _sid_to_string(sid_address)
        except OSError:
            # An unfamiliar allow-ACE layout is conservatively untrusted.
            sid = None
        copied.append(_WindowsAce(sid=sid, mask=mask, flags=header.AceFlags))
    return tuple(copied)


def _copy_windows_security_snapshot(descriptor: int, owner: int | None) -> _WindowsSecuritySnapshot:
    """Copy owner and DACL facts from a live security descriptor."""

    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    descriptor_dacl = ctypes.c_void_p()
    if not _ADVAPI32.GetSecurityDescriptorDacl(
        ctypes.c_void_p(descriptor),
        ctypes.byref(dacl_present),
        ctypes.byref(descriptor_dacl),
        ctypes.byref(dacl_defaulted),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not _ADVAPI32.GetSecurityDescriptorControl(
        ctypes.c_void_p(descriptor), ctypes.byref(control), ctypes.byref(revision)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    owner_sid = _sid_to_string(owner)
    allowed = (
        _allowed_aces(descriptor_dacl.value) if dacl_present.value and descriptor_dacl.value else ()
    )
    return _WindowsSecuritySnapshot(
        owner_sid=owner_sid,
        dacl_present=bool(dacl_present.value and descriptor_dacl.value),
        allowed_aces=allowed,
        dacl_protected=bool(control.value & _SE_DACL_PROTECTED),
    )


def _windows_security_snapshot(path: Path) -> _WindowsSecuritySnapshot:
    """Read and copy a Windows path's owner SID and DACL."""

    descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = _ADVAPI32.GetNamedSecurityInfoW(
        os.fspath(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        _raise_windows_error(result, path)
    try:
        descriptor_pointer = descriptor.value
        if descriptor_pointer is None:
            raise OSError("Windows security query returned a null descriptor")
        return _copy_windows_security_snapshot(descriptor_pointer, owner.value)
    finally:
        if descriptor.value:
            _KERNEL32.LocalFree(descriptor)


def _windows_descriptor_security_snapshot(
    descriptor: int,
) -> _WindowsSecuritySnapshot:
    """Read and copy owner/DACL facts for an already-open descriptor."""

    raw_handle = msvcrt.get_osfhandle(descriptor)
    if raw_handle == -1:
        raise OSError(errno.EBADF, "Invalid Windows file descriptor")
    security_descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = _ADVAPI32.GetSecurityInfo(
        wintypes.HANDLE(raw_handle),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result:
        _raise_windows_error(result)
    try:
        descriptor_pointer = security_descriptor.value
        if descriptor_pointer is None:
            raise OSError("Windows security query returned a null descriptor")
        return _copy_windows_security_snapshot(descriptor_pointer, owner.value)
    finally:
        if security_descriptor.value:
            _KERNEL32.LocalFree(security_descriptor)


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
