"""Copy Windows ownership and DACL information for profile and graph permission checks."""

from __future__ import annotations

import errno
import os
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple


class WindowsAce(NamedTuple):
    sid: str | None
    mask: int
    flags: int


class WindowsSecuritySnapshot(NamedTuple):
    owner_sid: str | None
    dacl_present: bool
    allowed_aces: tuple[WindowsAce, ...]
    dacl_protected: bool = False


_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_DACL_PROTECTED = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPES = frozenset({0x00, 0x04, 0x05, 0x09, 0x0B})
_ACE_OBJECT_TYPE_PRESENT = 0x01
_ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x02
_ERROR_INSUFFICIENT_BUFFER = 122
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
    _KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32.LocalFree.restype = ctypes.c_void_p


def raise_windows_error(code: int, path: Path | None = None) -> None:
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
def current_user_sid() -> str:
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
def codex_sandbox_sid() -> str | None:
    """Return the optional local SID Codex uses for sandbox read access."""

    computer = os.environ.get("COMPUTERNAME", "").strip()
    candidates = [f"{computer}\\CodexSandboxUsers"] if computer else []
    candidates.append("CodexSandboxUsers")
    for account in candidates:
        sid = _lookup_account_sid(account)
        if sid:
            return sid
    return None


def trusted_private_sids() -> frozenset[str]:
    """Return principals allowed to access a Codex-private Windows object."""

    trusted = {
        current_user_sid(),
        SYSTEM_SID,
        ADMINISTRATORS_SID,
        OWNER_RIGHTS_SID,
        TRUSTED_INSTALLER_SID,
    }
    sandbox = codex_sandbox_sid()
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


def _allowed_aces(dacl: int) -> tuple[WindowsAce, ...]:
    """Copy access-allowed ACEs out of a live ACL pointer."""

    information = _ACL_SIZE_INFORMATION()
    if not _ADVAPI32.GetAclInformation(
        ctypes.c_void_p(dacl),
        ctypes.byref(information),
        ctypes.sizeof(information),
        _ACL_SIZE_INFORMATION_CLASS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    copied: list[WindowsAce] = []
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
        copied.append(WindowsAce(sid=sid, mask=mask, flags=header.AceFlags))
    return tuple(copied)


def _copy_windows_security_snapshot(descriptor: int, owner: int | None) -> WindowsSecuritySnapshot:
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
    return WindowsSecuritySnapshot(
        owner_sid=owner_sid,
        dacl_present=bool(dacl_present.value and descriptor_dacl.value),
        allowed_aces=allowed,
        dacl_protected=bool(control.value & _SE_DACL_PROTECTED),
    )


def path_security_snapshot(path: Path) -> WindowsSecuritySnapshot:
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
        raise_windows_error(result, path)
    try:
        descriptor_pointer = descriptor.value
        if descriptor_pointer is None:
            raise OSError("Windows security query returned a null descriptor")
        return _copy_windows_security_snapshot(descriptor_pointer, owner.value)
    finally:
        if descriptor.value:
            _KERNEL32.LocalFree(descriptor)


def descriptor_security_snapshot(
    descriptor: int,
) -> WindowsSecuritySnapshot:
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
        raise_windows_error(result)
    try:
        descriptor_pointer = security_descriptor.value
        if descriptor_pointer is None:
            raise OSError("Windows security query returned a null descriptor")
        return _copy_windows_security_snapshot(descriptor_pointer, owner.value)
    finally:
        if security_descriptor.value:
            _KERNEL32.LocalFree(security_descriptor)
