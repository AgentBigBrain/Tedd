"""Noninteractive file-based macOS Keychain, with no plaintext fallback.

An ordinary Python CLI has no provisioned data-protection access group. The
SecKeychain APIs work in that environment and do not synchronize to iCloud.
They are deprecated, but remain supported (Apple TN3137). We retain the user's
default item ACL; never unlock/create a keychain or weaken access controls.

The interaction setting is process-wide: serialize this module's calls and
restore it before releasing the lock. Unrelated native callers in the same
process are outside this guard, as are unrestricted same-user processes.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading

from .source_contracts import SourceArchiveError

_MAXIMUM = 65536
_SERVICE = b"org.tedd.sources.google.v1"
_LOCK = threading.Lock()
_NOT_FOUND = -25300


def _check(status: int) -> None:
    if status == 0:
        return
    # Unsupported API, unavailable/missing keychain/default/storage/service, entitlement.
    if status in {-4, -25291, -25294, -25307, -25312, -34018, -67585}:
        raise SourceArchiveError("capability_unavailable")
    # Denied authentication/interaction, malformed keychain/data/ACL, restricted API.
    if status in {-25293, -25308, -25315, -25295, -25243, -26275, -34020, -128}:
        raise SourceArchiveError("privacy_rejected")
    raise SourceArchiveError("source_unavailable")


def _libraries() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """Load only OS frameworks by absolute path, with explicit native signatures."""
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint32
        security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        security.SecKeychainCopyDomainDefault.argtypes = [ctypes.c_int, ctypes.POINTER(pointer)]
        security.SecKeychainFindGenericPassword.argtypes = [
            pointer,
            uint,
            ctypes.c_char_p,
            uint,
            ctypes.c_char_p,
            ctypes.POINTER(uint),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]
        security.SecKeychainAddGenericPassword.argtypes = [
            pointer,
            uint,
            ctypes.c_char_p,
            uint,
            ctypes.c_char_p,
            uint,
            pointer,
            ctypes.POINTER(pointer),
        ]
        security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint, pointer]
        security.SecKeychainItemDelete.argtypes = [pointer]
        security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        for name in (
            "SecKeychainGetUserInteractionAllowed",
            "SecKeychainSetUserInteractionAllowed",
            "SecKeychainCopyDomainDefault",
            "SecKeychainFindGenericPassword",
            "SecKeychainAddGenericPassword",
            "SecKeychainItemModifyAttributesAndData",
            "SecKeychainItemDelete",
            "SecKeychainItemFreeContent",
        ):
            getattr(security, name).restype = ctypes.c_int32
        core.CFRelease.argtypes = [pointer]
        core.CFRelease.restype = None
        return security, core
    except (OSError, AttributeError) as error:
        raise SourceArchiveError("capability_unavailable") from error


def _operate(
    security: ctypes.CDLL,
    core: ctypes.CDLL,
    operation: str,
    account: bytes,
    payload: bytes | None,
) -> bytes | None:
    keychain_ref = ctypes.c_void_p()
    item = ctypes.c_void_p()
    data = ctypes.c_void_p()
    length = ctypes.c_uint32()
    source = ctypes.create_string_buffer(payload) if payload is not None else None
    try:
        # User-domain default only: never search all keychains or the system domain.
        _check(security.SecKeychainCopyDomainDefault(0, ctypes.byref(keychain_ref)))
        if not keychain_ref:
            raise SourceArchiveError("capability_unavailable")
        status = security.SecKeychainFindGenericPassword(
            keychain_ref,
            len(_SERVICE),
            _SERVICE,
            len(account),
            account,
            ctypes.byref(length) if operation == "lookup" else None,
            ctypes.byref(data) if operation == "lookup" else None,
            ctypes.byref(item),
        )
        if status == _NOT_FOUND:
            if operation == "store":
                assert payload is not None
                _check(
                    security.SecKeychainAddGenericPassword(
                        keychain_ref,
                        len(_SERVICE),
                        _SERVICE,
                        len(account),
                        account,
                        len(payload),
                        source,
                        None,
                    )
                )
            return None
        _check(status)
        if not item:
            raise SourceArchiveError("privacy_rejected")
        if operation == "lookup":
            if not data or not 0 < length.value <= _MAXIMUM:
                raise SourceArchiveError("privacy_rejected")
            return ctypes.string_at(data, length.value)
        if operation == "store":
            assert payload is not None
            _check(
                security.SecKeychainItemModifyAttributesAndData(item, None, len(payload), source)
            )
        else:
            status = security.SecKeychainItemDelete(item)
            if status != _NOT_FOUND:
                _check(status)
        return None
    finally:
        if source is not None:
            ctypes.memset(source, 0, len(source))
        try:
            if data:
                # Never copy or perform unbounded work on a malformed native length.
                ctypes.memset(data, 0, min(length.value, _MAXIMUM))
                _check(security.SecKeychainItemFreeContent(None, data))
        finally:
            if item:
                core.CFRelease(item)
            if keychain_ref:
                core.CFRelease(keychain_ref)


def keychain(operation: str, key: str, *, payload: bytes | None = None) -> bytes | None:
    """Get/upsert/delete one binding; lookup returns None only for a missing item.

    Locked/denied/corrupt items fail privacy checks, missing platform facilities
    report capability_unavailable, and other native failures are source_unavailable.
    No prompts, arbitrary queries, secret logging, subprocesses, or fallback files.
    """
    if operation not in {"lookup", "store", "clear"} or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise SourceArchiveError("privacy_rejected")
    if operation == "store" and (payload is None or not 0 < len(payload) <= _MAXIMUM):
        raise SourceArchiveError("limit_exceeded")
    if operation != "store" and payload is not None:
        raise SourceArchiveError("privacy_rejected")
    if sys.platform not in {"darwin"}:
        raise SourceArchiveError("capability_unavailable")
    security, core = _libraries()
    with _LOCK:
        interaction = ctypes.c_ubyte()
        _check(security.SecKeychainGetUserInteractionAllowed(ctypes.byref(interaction)))
        _check(security.SecKeychainSetUserInteractionAllowed(False))
        try:
            return _operate(security, core, operation, key.encode("ascii"), payload)
        finally:
            _check(security.SecKeychainSetUserInteractionAllowed(interaction.value))
