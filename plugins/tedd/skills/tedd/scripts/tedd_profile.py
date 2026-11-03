#!/usr/bin/env python3
"""Create and query Tedd's private, local identity profile.

The profile identifies the public Tedd agent, this installation, and known
operators.  Names, email aliases, and provider principals are identification
and contact attributes only; they never confer authority.  Authorization must
come from the active task or from independently verified provider context.

Importing this module is side-effect free.  In particular, installation never
creates a profile: only the explicit ``init`` command does that.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import errno
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from functools import lru_cache
from pathlib import Path
from typing import (
    Mapping,
    NamedTuple,
    NoReturn,
    Protocol,
    Sequence,
    TypeAlias,
    TypedDict,
    cast,
)

# The Tedd skill contract requires the sibling knowledge-graph skill.  Resolve
# its policy leaf exactly so both runtimes enforce one ACL decision without
# making the profile depend on graph storage or CLI modules.
_GRAPH_SCRIPT_DIRECTORY = (
    Path(__file__).resolve().parents[2] / "obsidian-knowledge-graph" / "scripts"
)
_graph_script_directory_added = str(_GRAPH_SCRIPT_DIRECTORY) not in sys.path
if _graph_script_directory_added:
    sys.path.insert(0, str(_GRAPH_SCRIPT_DIRECTORY))
try:
    import windows_acl_policy as _windows_acl_policy

    _expected_acl_policy = (_GRAPH_SCRIPT_DIRECTORY / "windows_acl_policy.py").resolve()
    _loaded_acl_policy = Path(getattr(_windows_acl_policy, "__file__", "")).resolve()
    if _loaded_acl_policy != _expected_acl_policy:
        raise ImportError(
            "refusing unexpected windows_acl_policy module: "
            f"{_loaded_acl_policy} (expected {_expected_acl_policy})"
        )
    _FILE_ALL_ACCESS = _windows_acl_policy.FILE_ALL_ACCESS
    _FILE_GENERIC_WRITE = _windows_acl_policy.FILE_GENERIC_WRITE
    _GENERIC_ALL = _windows_acl_policy.GENERIC_ALL
    _GENERIC_EXECUTE = _windows_acl_policy.GENERIC_EXECUTE
    _GENERIC_READ = _windows_acl_policy.GENERIC_READ
    _GENERIC_WRITE = _windows_acl_policy.GENERIC_WRITE
    _PrivateAclPrincipals = _windows_acl_policy.PrivateAclPrincipals
    _WINDOWS_MUTATING_ACCESS = _windows_acl_policy.WINDOWS_MUTATING_ACCESS
    _shared_expanded_file_mask = _windows_acl_policy.expanded_file_mask
    _evaluate_private_acl = _windows_acl_policy.private_acl_problem
finally:
    if _graph_script_directory_added:
        sys.path.remove(str(_GRAPH_SCRIPT_DIRECTORY))


_IS_WINDOWS = os.name == "nt"


class _FcntlOperations(Protocol):
    LOCK_EX: int
    LOCK_SH: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


if _IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    fcntl: _FcntlOperations | None = None

    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    import fcntl as _fcntl

    fcntl = cast(_FcntlOperations, _fcntl)


SCHEMA_VERSION = 1
AGENT_CLASS_ID = "tedd"
AGENT_DISPLAY_NAME = "Tedd"
IDENTITY_SCOPE = "identification_and_contact_only"
DEFAULT_PROFILE = Path("~/.codex/tedd/profile.json")

MAX_PROFILE_BYTES = 1024 * 1024
MAX_NAME_CHARS = 200
MAX_EMAIL_CHARS = 320
MAX_PROVIDER_CHARS = 64
MAX_PRINCIPAL_CHARS = 512
MAX_OPERATORS = 1_000
MAX_EMAIL_ALIASES = 100
MAX_PROVIDER_PRINCIPALS = 100
MAX_LOCK_TIMEOUT_SECONDS = 30.0
WINDOWS_LOCK_HARDENING_GRACE_SECONDS = 1.0

_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

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
_INHERIT_ONLY_ACE = 0x08
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_ACE_OBJECT_TYPE_PRESENT = 0x01
_ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x02
_ERROR_INSUFFICIENT_BUFFER = 122
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOTE = 4

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
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004

_FILE_GENERIC_READ = (
    _READ_CONTROL | _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _FILE_READ_EA | _SYNCHRONIZE
)
_FILE_GENERIC_EXECUTE = _READ_CONTROL | _FILE_READ_ATTRIBUTES | _FILE_EXECUTE | _SYNCHRONIZE
_FILE_READ_EXECUTE = _FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE

_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_OWNER_RIGHTS_SID = "S-1-3-4"
_TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"

GENERATED_ID_PATTERNS = {
    "installation_id": re.compile(r"installation_[0-9a-f]{32}"),
    "profile_id": re.compile(r"profile_[0-9a-f]{32}"),
    "agent_instance_id": re.compile(r"agent_instance_[0-9a-f]{32}"),
    "operator_id": re.compile(r"operator_[0-9a-f]{32}"),
}
PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "agent_class_id",
        "agent_display_name",
        "installation_id",
        "profile_id",
        "agent_instance_id",
        "identity_scope",
        "created_at",
        "updated_at",
        "operators",
    }
)
OPERATOR_FIELDS = frozenset(
    {
        "operator_id",
        "preferred_name",
        "display_name",
        "email_aliases",
        "provider_principals",
        "created_at",
        "updated_at",
    }
)
PRINCIPAL_FIELDS = frozenset({"provider", "principal"})


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class PrincipalBinding(TypedDict):
    provider: str
    principal: str


class OperatorProfile(TypedDict):
    operator_id: str
    preferred_name: str
    display_name: str
    email_aliases: list[str]
    provider_principals: list[PrincipalBinding]
    created_at: str
    updated_at: str


class ProfileDocument(TypedDict):
    schema_version: int
    agent_class_id: str
    agent_display_name: str
    installation_id: str
    profile_id: str
    agent_instance_id: str
    identity_scope: str
    created_at: str
    updated_at: str
    operators: list[OperatorProfile]


class OperatorSummary(TypedDict):
    operator_id: str
    preferred_name: str
    display_name: str


class UnconfiguredStatusResult(TypedDict):
    ok: bool
    configured: bool
    agent_class_id: str
    agent_display_name: str
    profile_path: str
    operator_count: int
    operator_selection_required: bool


class ConfiguredStatusResult(TypedDict):
    ok: bool
    configured: bool
    agent_class_id: str
    agent_display_name: str
    profile_id: str
    agent_instance_id: str
    identity_scope: str
    operator_count: int
    operator_selection_required: bool
    active_operator_id: None


class InitResult(TypedDict):
    ok: bool
    configured: bool
    profile_path: str
    profile_id: str
    installation_id: str
    agent_instance_id: str
    agent_class_id: str
    agent_display_name: str
    identity_scope: str
    operator: OperatorSummary
    operator_count: int


class AddOperatorResult(TypedDict):
    ok: bool
    profile_id: str
    operator: OperatorSummary
    operator_count: int
    operator_selection_required: bool


class BindIdentityResult(TypedDict):
    ok: bool
    updated: bool
    profile_id: str
    operator: OperatorSummary
    email_alias_count: int
    provider_principal_count: int


class ShowResult(TypedDict):
    ok: bool
    redacted: bool
    profile: ProfileDocument


class UnconfiguredValidationResult(TypedDict):
    ok: bool
    configured: bool
    valid: bool
    profile_path: str


class ConfiguredValidationResult(TypedDict):
    ok: bool
    configured: bool
    valid: bool
    schema_version: int
    profile_id: str
    operator_count: int


class ResolveResult(TypedDict):
    ok: bool
    found: bool
    matched_by: str
    identity_scope: str
    authority_asserted: bool
    operator: OperatorSummary | None


ProfileCommandResult: TypeAlias = (
    UnconfiguredStatusResult
    | ConfiguredStatusResult
    | InitResult
    | AddOperatorResult
    | BindIdentityResult
    | ShowResult
    | UnconfiguredValidationResult
    | ConfiguredValidationResult
    | ResolveResult
)


class _WindowsAce(NamedTuple):
    """One access-allowed ACE copied from a Windows DACL."""

    sid: str | None
    mask: int
    flags: int


class _WindowsSecuritySnapshot(NamedTuple):
    """Owner and DACL facts copied from a Windows security descriptor."""

    owner_sid: str | None
    dacl_present: bool
    dacl_protected: bool
    allowed_aces: tuple[_WindowsAce, ...]


if _IS_WINDOWS:

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
    _ADVAPI32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetSecurityDescriptorControl.restype = wintypes.BOOL
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
    _KERNEL32.ReOpenFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _KERNEL32.ReOpenFile.restype = wintypes.HANDLE
    _KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32.LocalFree.restype = ctypes.c_void_p
    _KERNEL32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetDriveTypeW.restype = wintypes.UINT


class ProfileError(ValueError):
    """A safe, caller-correctable profile error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JSONArgumentParser(argparse.ArgumentParser):
    """Turn argparse failures into the same JSON error envelope as commands."""

    def error(self, message: str) -> NoReturn:
        raise ProfileError("invalid_arguments", message)


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _next_timestamp(previous: str) -> str:
    """Return a UTC second strictly after ``previous`` despite coarse clocks."""

    current = _utc_now()
    if current > previous:
        return current
    prior = dt.datetime.strptime(previous, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return (prior + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")


def _generated_id(kind: str) -> str:
    prefix = {
        "installation_id": "installation_",
        "profile_id": "profile_",
        "agent_instance_id": "agent_instance_",
        "operator_id": "operator_",
    }[kind]
    return f"{prefix}{uuid.uuid4().hex}"


def _path(value: str | os.PathLike[str] | None) -> Path:
    candidate = Path(value) if value is not None else DEFAULT_PROFILE
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.name or candidate.name in {".", ".."}:
        raise ProfileError("invalid_path", "profile path must name a file")
    _reject_existing_symlink_chain(candidate)
    if _IS_WINDOWS:
        _check_windows_ancestor_chain(candidate)
    return candidate


def _reject_existing_symlink_chain(path: Path) -> None:
    """Reject every link-like component from the profile leaf through its ancestors.

    Missing ancestors are allowed for first-run initialization; each existing
    component is inspected lexically with ``lstat`` so a POSIX symlink or a
    Windows reparse point cannot be hidden by path resolution.
    """

    # Walk root-to-leaf so no descendant lookup traverses an already-known
    # symlink.  Missing components remain acceptable until explicit init.
    for component in (*reversed(path.parents), path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if _is_link_like(metadata):
            raise ProfileError(
                "unsafe_path",
                f"profile path contains a link-like component: {component}",
            )


def _lock_path(profile_path: Path) -> Path:
    return profile_path.with_name(f"{profile_path.name}.lock")


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProfileError("invalid_profile", f"{label} must be a string")
    if value != value.strip():
        raise ProfileError("invalid_profile", f"{label} must not have surrounding whitespace")
    if not value:
        raise ProfileError("invalid_profile", f"{label} is required")
    if len(value) > maximum:
        raise ProfileError("invalid_profile", f"{label} exceeds {maximum} characters")
    if _has_control_characters(value):
        raise ProfileError("invalid_profile", f"{label} contains control characters")
    return value


def _input_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProfileError("invalid_input", f"{label} must be a string")
    normalized = value.strip()
    try:
        return _bounded_text(normalized, label, maximum)
    except ProfileError as error:
        raise ProfileError("invalid_input", error.message) from error


def _email(value: object, *, input_value: bool = False) -> str:
    parser = _input_text if input_value else _bounded_text
    email = parser(value, "email alias", MAX_EMAIL_CHARS)
    if any(character.isspace() for character in email):
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email alias must not contain whitespace")
    if email.count("@") != 1:
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email alias must contain one @ separator")
    local, domain = email.rsplit("@", 1)
    if not local or not domain or local.startswith(".") or local.endswith("."):
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email alias is malformed")
    if ".." in local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email alias is malformed")
    if len(local) > 64 or len(domain) > 255:
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email alias is malformed")
    if not all(character.isalnum() or character in ".-" for character in domain):
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email domain contains unsupported characters")
    return email


def _normalized_email(value: str, *, input_value: bool = False) -> str:
    local, domain = value.rsplit("@", 1)
    try:
        normalized_domain = domain.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "email domain cannot be IDNA-normalized") from error
    # SMTP local parts are potentially case-sensitive.  Preserve those bytes
    # exactly; normalize only the DNS-owned domain portion.
    return f"{local}@{normalized_domain}"


def _provider(value: object, *, input_value: bool = False) -> str:
    parser = _input_text if input_value else _bounded_text
    provider = parser(value, "provider", MAX_PROVIDER_CHARS).casefold()
    if not PROVIDER_PATTERN.fullmatch(provider):
        code = "invalid_input" if input_value else "invalid_profile"
        raise ProfileError(code, "provider must be a short lowercase ASCII identifier")
    return provider


def _principal(value: object, *, input_value: bool = False) -> str:
    parser = _input_text if input_value else _bounded_text
    return parser(value, "provider principal", MAX_PRINCIPAL_CHARS)


def _timestamp(value: object, label: str) -> str:
    timestamp = _bounded_text(value, label, 20)
    if not TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise ProfileError("invalid_profile", f"{label} must be a UTC timestamp ending in Z")
    try:
        dt.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ProfileError("invalid_profile", f"{label} is not a valid timestamp") from error
    return timestamp


def _validate_generated_id(value: object, kind: str) -> str:
    identifier = _bounded_text(value, kind.replace("_", " "), 80)
    if not GENERATED_ID_PATTERNS[kind].fullmatch(identifier):
        raise ProfileError("invalid_profile", f"{kind} is not a valid generated identifier")
    return identifier


def _input_operator_id(value: object) -> str:
    identifier = _input_text(value, "operator ID", 80)
    if not GENERATED_ID_PATTERNS["operator_id"].fullmatch(identifier):
        raise ProfileError("invalid_input", "operator ID is not a valid generated identifier")
    return identifier


def _strict_fields(value: Mapping[object, object], expected: frozenset[str], label: str) -> None:
    actual: set[str] = set()
    for field in value:
        if not isinstance(field, str):
            raise ProfileError("invalid_profile", f"{label} field names must be strings")
        actual.add(field)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ProfileError("invalid_profile", f"{label} is missing fields: {missing}")
    if unknown:
        raise ProfileError("invalid_profile", f"{label} has unknown fields: {unknown}")


def validate_profile(profile: object) -> ProfileDocument:
    """Validate the complete v1 document and return it unchanged."""

    if not isinstance(profile, dict):
        raise ProfileError("invalid_profile", "profile must be a JSON object")
    _strict_fields(profile, PROFILE_FIELDS, "profile")
    profile_values = cast(dict[str, object], profile)
    if (
        type(profile_values["schema_version"]) is not int
        or profile_values["schema_version"] != SCHEMA_VERSION
    ):
        raise ProfileError("unsupported_schema", "profile schema_version must be 1")
    if profile_values["agent_class_id"] != AGENT_CLASS_ID:
        raise ProfileError("invalid_profile", f"agent_class_id must be {AGENT_CLASS_ID!r}")
    if profile_values["agent_display_name"] != AGENT_DISPLAY_NAME:
        raise ProfileError("invalid_profile", f"agent_display_name must be {AGENT_DISPLAY_NAME!r}")
    if profile_values["identity_scope"] != IDENTITY_SCOPE:
        raise ProfileError("invalid_profile", f"identity_scope must be {IDENTITY_SCOPE!r}")
    _validate_generated_id(profile_values["installation_id"], "installation_id")
    _validate_generated_id(profile_values["profile_id"], "profile_id")
    _validate_generated_id(profile_values["agent_instance_id"], "agent_instance_id")
    profile_created = _timestamp(profile_values["created_at"], "profile created_at")
    profile_updated = _timestamp(profile_values["updated_at"], "profile updated_at")
    if profile_updated < profile_created:
        raise ProfileError("invalid_profile", "profile updated_at precedes created_at")

    raw_operators = profile_values["operators"]
    if not isinstance(raw_operators, list) or not 1 <= len(raw_operators) <= MAX_OPERATORS:
        raise ProfileError(
            "invalid_profile", f"operators must contain between 1 and {MAX_OPERATORS} entries"
        )
    operators = cast(list[object], raw_operators)

    operator_ids: set[str] = set()
    owned_emails: dict[str, str] = {}
    owned_principals: dict[tuple[str, str], str] = {}
    for index, raw_operator in enumerate(operators):
        label = f"operators[{index}]"
        if not isinstance(raw_operator, dict):
            raise ProfileError("invalid_profile", f"{label} must be an object")
        _strict_fields(raw_operator, OPERATOR_FIELDS, label)
        operator = cast(dict[str, object], raw_operator)
        operator_id = _validate_generated_id(operator["operator_id"], "operator_id")
        if operator_id in operator_ids:
            raise ProfileError("invalid_profile", f"duplicate operator ID: {operator_id}")
        operator_ids.add(operator_id)
        _bounded_text(operator["preferred_name"], f"{label}.preferred_name", MAX_NAME_CHARS)
        _bounded_text(operator["display_name"], f"{label}.display_name", MAX_NAME_CHARS)
        operator_created = _timestamp(operator["created_at"], f"{label}.created_at")
        operator_updated = _timestamp(operator["updated_at"], f"{label}.updated_at")
        if operator_updated < operator_created:
            raise ProfileError("invalid_profile", f"{label}.updated_at precedes created_at")
        if operator_created < profile_created or operator_updated > profile_updated:
            raise ProfileError("invalid_profile", f"{label} timestamps exceed profile bounds")

        raw_aliases = operator["email_aliases"]
        if not isinstance(raw_aliases, list) or not 1 <= len(raw_aliases) <= MAX_EMAIL_ALIASES:
            raise ProfileError(
                "invalid_profile",
                f"{label}.email_aliases must contain between 1 and {MAX_EMAIL_ALIASES} entries",
            )
        aliases = cast(list[object], raw_aliases)
        local_emails: set[str] = set()
        for raw_email in aliases:
            email = _email(raw_email)
            normalized = _normalized_email(email)
            if normalized in local_emails:
                raise ProfileError("invalid_profile", f"duplicate email alias in {label}")
            local_emails.add(normalized)
            previous_owner = owned_emails.get(normalized)
            if previous_owner is not None and previous_owner != operator_id:
                raise ProfileError(
                    "duplicate_identity", "email alias belongs to multiple operators"
                )
            owned_emails[normalized] = operator_id

        raw_principals = operator["provider_principals"]
        if not isinstance(raw_principals, list) or len(raw_principals) > MAX_PROVIDER_PRINCIPALS:
            raise ProfileError(
                "invalid_profile",
                f"{label}.provider_principals must contain at most "
                f"{MAX_PROVIDER_PRINCIPALS} entries",
            )
        principals = cast(list[object], raw_principals)
        local_principals: set[tuple[str, str]] = set()
        for principal_index, raw_binding in enumerate(principals):
            binding_label = f"{label}.provider_principals[{principal_index}]"
            if not isinstance(raw_binding, dict):
                raise ProfileError("invalid_profile", f"{binding_label} must be an object")
            _strict_fields(raw_binding, PRINCIPAL_FIELDS, binding_label)
            binding = cast(dict[str, object], raw_binding)
            provider = _provider(binding["provider"])
            if provider != binding["provider"]:
                raise ProfileError(
                    "invalid_profile", f"{binding_label}.provider must be normalized"
                )
            principal = _principal(binding["principal"])
            key = (provider, principal)
            if key in local_principals:
                raise ProfileError("invalid_profile", f"duplicate provider principal in {label}")
            local_principals.add(key)
            previous_owner = owned_principals.get(key)
            if previous_owner is not None and previous_owner != operator_id:
                raise ProfileError(
                    "duplicate_identity", "provider principal belongs to multiple operators"
                )
            owned_principals[key] = operator_id
    return cast(ProfileDocument, profile_values)


def _input_email_aliases(emails: Sequence[str], *, required: bool) -> list[str]:
    if required and not emails:
        raise ProfileError("invalid_input", "at least one --email is required")
    if len(emails) > MAX_EMAIL_ALIASES:
        raise ProfileError(
            "invalid_input", f"at most {MAX_EMAIL_ALIASES} email aliases are allowed"
        )
    preserved_emails: list[str] = []
    seen_emails: set[str] = set()
    for value in emails:
        preserved = _email(value, input_value=True)
        normalized = _normalized_email(preserved, input_value=True)
        if normalized in seen_emails:
            raise ProfileError("invalid_input", "duplicate email aliases are not allowed")
        seen_emails.add(normalized)
        preserved_emails.append(preserved)
    return preserved_emails


def _input_principal_bindings(provider_principals: Sequence[str]) -> list[PrincipalBinding]:
    if len(provider_principals) > MAX_PROVIDER_PRINCIPALS:
        raise ProfileError(
            "invalid_input", f"at most {MAX_PROVIDER_PRINCIPALS} provider principals are allowed"
        )
    bindings: list[PrincipalBinding] = []
    seen_bindings: set[tuple[str, str]] = set()
    for raw_binding in provider_principals:
        if not isinstance(raw_binding, str) or "=" not in raw_binding:
            raise ProfileError("invalid_input", "--provider-principal must use PROVIDER=PRINCIPAL")
        raw_provider, raw_principal = raw_binding.split("=", 1)
        provider = _provider(raw_provider, input_value=True)
        principal = _principal(raw_principal, input_value=True)
        key = (provider, principal)
        if key in seen_bindings:
            raise ProfileError("invalid_input", "duplicate provider principals are not allowed")
        seen_bindings.add(key)
        bindings.append({"provider": provider, "principal": principal})
    return bindings


def _operator(
    preferred_name: str,
    display_name: str | None,
    emails: Sequence[str],
    provider_principals: Sequence[str],
    *,
    timestamp: str,
) -> OperatorProfile:
    preferred = _input_text(preferred_name, "preferred name", MAX_NAME_CHARS)
    display = _input_text(
        preferred if display_name is None else display_name,
        "display name",
        MAX_NAME_CHARS,
    )
    preserved_emails = _input_email_aliases(emails, required=True)
    bindings = _input_principal_bindings(provider_principals)
    return {
        "operator_id": _generated_id("operator_id"),
        "preferred_name": preferred,
        "display_name": display,
        "email_aliases": preserved_emails,
        "provider_principals": bindings,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _profile(operator: OperatorProfile, timestamp: str) -> ProfileDocument:
    profile: ProfileDocument = {
        "schema_version": SCHEMA_VERSION,
        "agent_class_id": AGENT_CLASS_ID,
        "agent_display_name": AGENT_DISPLAY_NAME,
        "installation_id": _generated_id("installation_id"),
        "profile_id": _generated_id("profile_id"),
        "agent_instance_id": _generated_id("agent_instance_id"),
        "identity_scope": IDENTITY_SCOPE,
        "created_at": timestamp,
        "updated_at": timestamp,
        "operators": [operator],
    }
    return validate_profile(profile)


def _is_link_like(metadata: os.stat_result) -> bool:
    """Return whether metadata describes a symlink or Windows reparse point."""

    if stat.S_ISLNK(metadata.st_mode):
        return True
    return _IS_WINDOWS and bool(getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)


def _reject_symlink(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _is_link_like(metadata):
        raise ProfileError("unsafe_path", f"{label} must not be a link-like path")


def _raise_windows_error(code: int, path: Path | None = None) -> None:
    """Raise an ``OSError`` for a Win32 API that returns a status code."""

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
    """Return the SID carried by the current process token."""

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
        value = _sid_to_string(token_user.User.Sid)
        if not value:
            raise OSError("the current process token has no user SID")
        return value
    finally:
        _KERNEL32.CloseHandle(token)


@lru_cache(maxsize=None)
def _lookup_account_sid(account_name: str) -> str | None:
    """Resolve an optional Windows account name to a SID string."""

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
    """Return the optional local SID Codex uses for sandbox access."""

    computer = os.environ.get("COMPUTERNAME", "").strip()
    candidates = [f"{computer}\\CodexSandboxUsers"] if computer else []
    candidates.append("CodexSandboxUsers")
    for account in candidates:
        value = _lookup_account_sid(account)
        if value:
            return value
    return None


def _trusted_private_sids() -> frozenset[str]:
    """Return principals permitted on Tedd's private Windows profile state."""

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
    """Locate the SID field in one supported access-allowed ACE layout."""

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
    """Copy access-allowed ACEs out of a live Windows ACL pointer."""

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
            sid = None
        copied.append(_WindowsAce(sid=sid, mask=mask, flags=header.AceFlags))
    return tuple(copied)


def _copy_windows_security_snapshot(descriptor: int, owner: int | None) -> _WindowsSecuritySnapshot:
    """Copy owner and DACL facts from a live Windows security descriptor."""

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
    allowed = (
        _allowed_aces(descriptor_dacl.value) if dacl_present.value and descriptor_dacl.value else ()
    )
    return _WindowsSecuritySnapshot(
        owner_sid=_sid_to_string(owner),
        dacl_present=bool(dacl_present.value and descriptor_dacl.value),
        dacl_protected=bool(control.value & _SE_DACL_PROTECTED),
        allowed_aces=allowed,
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


def _windows_descriptor_security_snapshot(fd: int) -> _WindowsSecuritySnapshot:
    """Read owner/DACL facts from an already-open Windows descriptor."""

    raw_handle = msvcrt.get_osfhandle(fd)
    if raw_handle == -1:
        raise OSError(errno.EBADF, "invalid Windows file descriptor")
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


def _expanded_file_mask(mask: int) -> int:
    """Expand generic Windows access bits into concrete file rights."""

    expanded: object = _shared_expanded_file_mask(mask)
    if not isinstance(expanded, int):
        raise TypeError("Windows ACL policy must return an integer mask")
    return expanded


def _windows_snapshot_acl_problem(
    snapshot: _WindowsSecuritySnapshot,
    *,
    is_directory: bool,
    require_protected: bool,
) -> str | None:
    """Return why copied Windows owner/DACL facts are not private."""

    current_user = _current_user_sid()
    problem: object = _evaluate_private_acl(
        snapshot,
        _PrivateAclPrincipals(
            current_user_sid=current_user,
            owner_rights_sid=_OWNER_RIGHTS_SID,
            trusted_sids=_trusted_private_sids(),
            sandbox_sid=_codex_sandbox_sid(),
        ),
        is_directory=is_directory,
        require_protected=require_protected,
    )
    if problem is not None and not isinstance(problem, str):
        raise TypeError("Windows ACL policy must return text or None")
    return problem


def _windows_acl_problem(
    path: Path, *, is_directory: bool, require_protected: bool = True
) -> str | None:
    """Return why a Windows path owner/DACL is not private, or ``None``."""

    return _windows_snapshot_acl_problem(
        _windows_security_snapshot(path),
        is_directory=is_directory,
        require_protected=require_protected,
    )


def _windows_ancestor_problem(path: Path) -> str | None:
    """Return why an existing Windows path ancestor permits unsafe mutation."""

    snapshot = _windows_security_snapshot(path)
    system_owners = {
        _SYSTEM_SID,
        _ADMINISTRATORS_SID,
        _TRUSTED_INSTALLER_SID,
    }
    if path == Path(path.anchor):
        drive_type = int(_KERNEL32.GetDriveTypeW(os.fspath(path)))
        if drive_type in {_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR, _DRIVE_REMOTE}:
            return "drive root must be on a local Windows volume"
        if snapshot.owner_sid not in system_owners or not snapshot.dacl_present:
            return "drive root must be system-owned with a non-null access-control list"
        # Windows drive roots intentionally allow bounded top-level creation.
        # Ignore only add-file/add-directory; deletion, ACL mutation, and
        # broader generic writes from untrusted principals remain unsafe.
        dangerous_root_access = _WINDOWS_MUTATING_ACCESS & ~(_FILE_WRITE_DATA | _FILE_APPEND_DATA)
        trusted = _trusted_private_sids()
        sandbox = _codex_sandbox_sid()
        for ace in snapshot.allowed_aces:
            if ace.flags & _INHERIT_ONLY_ACE:
                continue
            expanded = _expanded_file_mask(ace.mask)
            if ace.sid == sandbox and expanded & dangerous_root_access:
                return "drive root grants CodexSandboxUsers dangerous mutation access"
            if ace.sid not in trusted and expanded & dangerous_root_access:
                return "drive root grants untrusted principals dangerous mutation access"
        return None

    allowed_owners = {*system_owners, _current_user_sid()}
    if snapshot.owner_sid not in allowed_owners:
        return "must be owned by the current user or a trusted Windows system principal"
    if not snapshot.dacl_present:
        return "must have a non-null access-control list"

    trusted = _trusted_private_sids()
    sandbox = _codex_sandbox_sid()
    for ace in snapshot.allowed_aces:
        applies_here = not ace.flags & _INHERIT_ONLY_ACE
        can_reach_child = bool(ace.flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE))
        if not applies_here and not can_reach_child:
            continue
        expanded = _expanded_file_mask(ace.mask)
        if ace.sid == sandbox and expanded & _WINDOWS_MUTATING_ACCESS:
            return "must not grant CodexSandboxUsers mutation access"
        if ace.sid not in trusted and expanded & _WINDOWS_MUTATING_ACCESS:
            return "must not grant untrusted principals mutation access"
    return None


def _check_windows_ancestor_chain(path: Path) -> None:
    """Reject unsafe existing Windows ancestors without traversing redirects."""

    for component in reversed(path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if _is_link_like(metadata):
            raise ProfileError(
                "unsafe_path",
                f"profile path contains a link-like component: {component}",
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileError(
                "unsafe_path", f"profile path ancestor is not a directory: {component}"
            )
        problem = _windows_ancestor_problem(component)
        if problem is not None:
            raise ProfileError(
                "unsafe_permissions",
                f"profile path ancestor {problem}: {component}",
            )


def _check_private_windows_acl(
    path: Path,
    label: str,
    *,
    is_directory: bool,
    require_protected: bool = True,
) -> None:
    """Raise a stable profile error when a Windows owner or DACL is unsafe."""

    problem = _windows_acl_problem(
        path,
        is_directory=is_directory,
        require_protected=require_protected,
    )
    if problem is not None:
        raise ProfileError("unsafe_permissions", f"{label} {problem}")


def _check_private_windows_fd_acl(fd: int, label: str) -> None:
    """Validate owner/DACL facts on the exact already-open Windows file."""

    problem = _windows_snapshot_acl_problem(
        _windows_descriptor_security_snapshot(fd),
        is_directory=False,
        require_protected=True,
    )
    if problem is not None:
        raise ProfileError("unsafe_permissions", f"{label} {problem}")


def _private_windows_sddl(*, inherit: bool) -> str:
    """Build the protected private DACL for a profile directory or file."""

    flags = "OICI" if inherit else ""
    current_user = _current_user_sid()
    entries = [
        f"(A;{flags};FA;;;{current_user})",
        f"(A;{flags};FA;;;SY)",
        f"(A;{flags};FA;;;BA)",
    ]
    sandbox = _codex_sandbox_sid()
    if sandbox:
        entries.append(f"(A;{flags};0x{_FILE_READ_EXECUTE:08X};;;{sandbox})")
    return f"O:{current_user}D:P" + "".join(entries)


def _security_descriptor_parts_from_sddl(
    sddl: str,
) -> tuple[int, int | None, int]:
    """Allocate an SDDL descriptor and return descriptor, owner, and DACL pointers."""

    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        owner = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        if not _ADVAPI32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
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
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value or not dacl.value:
            raise OSError("private security descriptor did not contain a DACL")
        descriptor_pointer = descriptor.value
        dacl_pointer = dacl.value
        if descriptor_pointer is None or dacl_pointer is None:
            raise OSError("private security descriptor returned a null pointer")
        return descriptor_pointer, owner.value, dacl_pointer
    except BaseException:
        _KERNEL32.LocalFree(descriptor)
        raise


def _set_windows_dacl_from_sddl(path: Path, sddl: str, *, set_owner: bool = False) -> None:
    """Apply a protected DACL and optionally its SDDL owner to a Windows path."""

    descriptor, owner, dacl = _security_descriptor_parts_from_sddl(sddl)
    try:
        if set_owner and not owner:
            raise OSError("private security descriptor did not contain an owner")
        security_information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if set_owner:
            security_information |= _OWNER_SECURITY_INFORMATION
        result = _ADVAPI32.SetNamedSecurityInfoW(
            os.fspath(path),
            _SE_FILE_OBJECT,
            security_information,
            ctypes.c_void_p(owner) if set_owner else None,
            None,
            ctypes.c_void_p(dacl),
            None,
        )
        if result:
            _raise_windows_error(result, path)
    finally:
        _KERNEL32.LocalFree(ctypes.c_void_p(descriptor))


def _set_windows_private_fd(fd: int) -> None:
    """Apply the current owner and protected file DACL to an open descriptor."""

    security_descriptor, owner, dacl = _security_descriptor_parts_from_sddl(
        _private_windows_sddl(inherit=False)
    )
    try:
        if not owner:
            raise OSError("private security descriptor did not contain an owner")
        set_owner = _windows_descriptor_security_snapshot(fd).owner_sid != _current_user_sid()
        raw_handle = msvcrt.get_osfhandle(fd)
        if raw_handle == -1:
            raise OSError(errno.EBADF, "invalid Windows file descriptor")
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
                ctypes.c_void_p(owner) if set_owner else None,
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


def _set_private_path_mode(path: Path, mode: int) -> None:
    """Apply a private POSIX mode or protected Windows directory DACL."""

    if _IS_WINDOWS:
        set_owner = _windows_security_snapshot(path).owner_sid != _current_user_sid()
        _set_windows_dacl_from_sddl(path, _private_windows_sddl(inherit=True), set_owner=set_owner)
        _check_private_windows_acl(path, "profile directory", is_directory=True)
        return
    os.chmod(path, mode)


def _set_private_fd_mode(fd: int, mode: int) -> None:
    """Apply a private POSIX mode or protected Windows file DACL."""

    if _IS_WINDOWS:
        _set_windows_private_fd(fd)
        return
    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise NotImplementedError("descriptor chmod is unavailable")
    fchmod(fd, mode)


def _check_private_owner_and_mode(metadata: os.stat_result, label: str, expected_mode: int) -> None:
    """Enforce real POSIX ownership and modes without trusting Windows emulation."""

    if _IS_WINDOWS:
        return
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise NotImplementedError("POSIX ownership inspection is unavailable")
    current_uid: object = getuid()
    if not isinstance(current_uid, int):
        raise TypeError("os.getuid() must return an integer")
    if metadata.st_uid != current_uid:
        raise ProfileError("unsafe_permissions", f"{label} must be owned by this user")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise ProfileError("unsafe_permissions", f"{label} permissions must be {expected_mode:04o}")


def _check_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ProfileError("unsafe_path", "profile directory does not exist") from error
    if _is_link_like(metadata):
        raise ProfileError("unsafe_path", "profile directory must not be a link-like path")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProfileError("unsafe_path", "profile parent must be a directory")
    if _IS_WINDOWS:
        _check_private_windows_acl(path, "profile directory", is_directory=True)
    else:
        _check_private_owner_and_mode(metadata, "profile directory", 0o700)


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
            _set_private_path_mode(path, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise ProfileError("io_error", f"cannot create profile directory: {error}") from error
    else:
        if _is_link_like(metadata):
            raise ProfileError("unsafe_path", "profile directory must not be a link-like path")
        if _IS_WINDOWS:
            _check_private_windows_acl(
                path,
                "profile directory",
                is_directory=True,
                require_protected=False,
            )
            _set_private_path_mode(path, 0o700)
    _check_private_directory(path)


def _check_open_file(fd: int, label: str, expected_mode: int) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise ProfileError("unsafe_path", f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise ProfileError("unsafe_path", f"{label} must not have multiple hard links")
    if _IS_WINDOWS:
        _check_private_windows_fd_acl(fd, label)
    else:
        _check_private_owner_and_mode(metadata, label, expected_mode)
    return metadata


def _check_open_path(fd: int, path: Path, label: str, expected_mode: int) -> os.stat_result:
    """Validate an open file and prove its path still names that same file."""

    metadata = _check_open_file(fd, label, expected_mode)
    _check_open_path_identity(metadata, path, label)
    return metadata


def _check_open_path_identity(metadata: os.stat_result, path: Path, label: str) -> None:
    """Prove a validated open-file snapshot still matches its lexical path."""

    try:
        path_metadata = path.lstat()
    except FileNotFoundError as error:
        raise ProfileError("unsafe_path", f"{label} path changed while opening") from error
    if _is_link_like(path_metadata) or not os.path.samestat(metadata, path_metadata):
        raise ProfileError("unsafe_path", f"{label} must not be a link-like or replaced path")


def _prepare_lock_file(fd: int) -> None:
    """Ensure the one-byte region required by Windows locking exists."""

    if not _IS_WINDOWS:
        return
    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\0")
        os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)


def _try_lock_file(fd: int, *, exclusive: bool) -> bool:
    """Attempt one non-blocking advisory lock acquisition."""

    if _IS_WINDOWS:
        # The Windows CRT exposes exclusive byte-range locks.  Serializing
        # readers too is a safe, deliberately stronger boundary than flock's
        # shared-read behavior on POSIX.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    assert fcntl is not None
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_file(fd: int) -> None:
    """Release a lock acquired by ``_try_lock_file``."""

    if _IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    assert fcntl is not None
    fcntl.flock(fd, fcntl.LOCK_UN)


class _FileLock:
    def __init__(self, path: Path, *, exclusive: bool, timeout: float) -> None:
        if timeout < 0 or timeout > MAX_LOCK_TIMEOUT_SECONDS:
            raise ProfileError(
                "invalid_input",
                f"lock timeout must be between 0 and {MAX_LOCK_TIMEOUT_SECONDS:g} seconds",
            )
        self.path = path
        self.exclusive = exclusive
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        if not math.isfinite(self.timeout):
            raise ProfileError("invalid_input", "lock timeout must be a finite number")
        _check_private_directory(self.path.parent)
        _reject_symlink(self.path, "profile lock")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        started_at = time.monotonic()
        deadline = started_at + self.timeout
        hardening_deadline = min(deadline, started_at + WINDOWS_LOCK_HARDENING_GRACE_SECONDS)
        fd: int | None = None
        try:
            while True:
                try:
                    fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    fd = os.open(self.path, flags)
                    created = False
                if created:
                    _set_private_fd_mode(fd, 0o600)
                    break
                if not _IS_WINDOWS:
                    break

                # A peer can observe a newly created lock between its O_EXCL
                # publication and the creator's owner/DACL hardening pass.
                # Never mutate that foreign descriptor; close and reopen it
                # within the same deadline used for lock acquisition.
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    break
                _check_open_path_identity(metadata, self.path, "profile lock")
                owner_sid = _windows_descriptor_security_snapshot(fd).owner_sid
                if owner_sid == _current_user_sid():
                    break
                if owner_sid != _ADMINISTRATORS_SID:
                    break
                if time.monotonic() >= hardening_deadline:
                    break
                closing_fd = fd
                fd = None
                os.close(closing_fd)
                time.sleep(
                    min(
                        0.02,
                        max(0.001, hardening_deadline - time.monotonic()),
                    )
                )
        except OSError as error:
            if fd is not None:
                os.close(fd)
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise ProfileError("unsafe_path", "profile lock must not be a symlink") from error
            raise ProfileError("io_error", f"cannot open profile lock: {error}") from error
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        assert fd is not None
        try:
            _check_open_path(fd, self.path, "profile lock", 0o600)
            _prepare_lock_file(fd)
            if not _IS_WINDOWS:
                # Preserve the existing POSIX timeout boundary; Windows uses
                # the earlier deadline for both owner settling and locking.
                deadline = time.monotonic() + self.timeout
            while True:
                if _try_lock_file(fd, exclusive=self.exclusive):
                    break
                if time.monotonic() >= deadline:
                    raise ProfileError("lock_timeout", "timed out waiting for profile lock")
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            try:
                _unlock_file(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None


def _object_without_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("invalid_profile", f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_profile(path: Path) -> ProfileDocument:
    _check_private_directory(path.parent)
    _reject_symlink(path, "profile")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as error:
        raise ProfileError("unconfigured", "Tedd identity profile is not configured") from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            raise ProfileError("unsafe_path", "profile must not be a symlink") from error
        raise ProfileError("io_error", f"cannot open profile: {error}") from error
    try:
        metadata = _check_open_path(fd, path, "profile", 0o600)
        if metadata.st_size > MAX_PROFILE_BYTES:
            raise ProfileError("profile_too_large", "profile exceeds the maximum size")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(MAX_PROFILE_BYTES + 1)
        if len(payload) > MAX_PROFILE_BYTES:
            raise ProfileError("profile_too_large", "profile exceeds the maximum size")
    finally:
        os.close(fd)
    try:
        value: object = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
    except UnicodeDecodeError as error:
        raise ProfileError("invalid_profile", "profile is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise ProfileError("invalid_profile", f"profile is not valid JSON: {error.msg}") from error
    except RecursionError as error:
        raise ProfileError("invalid_profile", "profile JSON is nested too deeply") from error
    return validate_profile(value)


def _canonical_bytes(profile: Mapping[str, object]) -> bytes:
    payload = (json.dumps(profile, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(payload) > MAX_PROFILE_BYTES:
        raise ProfileError("profile_too_large", "profile exceeds the maximum size")
    return payload


def _fsync_directory(path: Path) -> None:
    if _IS_WINDOWS:
        # Windows does not expose directory descriptors through ``os.open``.
        # The file itself is flushed before the atomic rename below.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, profile: Mapping[str, object], *, replace: bool) -> None:
    payload = _canonical_bytes(profile)
    _check_private_directory(path.parent)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        _set_private_fd_mode(temp_fd, 0o600)
        with os.fdopen(temp_fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(temp_fd)
        temp_fd = -1
        if replace:
            _reject_symlink(path, "profile")
            try:
                existing_fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            except FileNotFoundError as error:
                raise ProfileError("unconfigured", "profile disappeared during update") from error
            try:
                _check_open_path(existing_fd, path, "profile", 0o600)
            finally:
                os.close(existing_fd)
            os.replace(temp_path, path)
        else:
            try:
                if _IS_WINDOWS:
                    # Unlike POSIX rename(), MoveFile refuses an existing
                    # destination, giving Windows an atomic no-replace create.
                    os.rename(temp_path, path)
                else:
                    os.link(temp_path, path, follow_symlinks=False)
            except FileExistsError as error:
                raise ProfileError("profile_exists", "identity profile already exists") from error
            if not _IS_WINDOWS:
                temp_path.unlink()
        if _IS_WINDOWS:
            _check_private_windows_acl(path, "profile", is_directory=False)
        _fsync_directory(path.parent)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _profile_exists(path: Path) -> bool:
    _reject_symlink(path, "profile")
    try:
        return path.exists()
    except OSError as error:
        raise ProfileError("io_error", f"cannot inspect profile: {error}") from error


def _redacted_profile(profile: ProfileDocument) -> ProfileDocument:
    redacted_value: object = copy.deepcopy(profile)
    if not isinstance(redacted_value, dict):
        raise TypeError("A validated profile must remain an object when copied")
    # ``deepcopy`` preserves the already-validated profile shape; the runtime
    # check above guards the only top-level operation this function requires.
    redacted = cast(ProfileDocument, redacted_value)
    for operator in redacted["operators"]:
        operator["email_aliases"] = ["***" for _ in operator["email_aliases"]]
        for binding in operator["provider_principals"]:
            binding["principal"] = "***"
    return redacted


def _operator_summary(operator: OperatorProfile) -> OperatorSummary:
    return {
        "operator_id": str(operator["operator_id"]),
        "preferred_name": str(operator["preferred_name"]),
        "display_name": str(operator["display_name"]),
    }


def _with_profile_lock(path: Path, *, exclusive: bool, timeout: float) -> _FileLock:
    return _FileLock(_lock_path(path), exclusive=exclusive, timeout=timeout)


def _status(path: Path, timeout: float) -> UnconfiguredStatusResult | ConfiguredStatusResult:
    if not _profile_exists(path):
        return {
            "ok": True,
            "configured": False,
            "agent_class_id": AGENT_CLASS_ID,
            "agent_display_name": AGENT_DISPLAY_NAME,
            "profile_path": str(path),
            "operator_count": 0,
            "operator_selection_required": False,
        }
    with _with_profile_lock(path, exclusive=False, timeout=timeout):
        profile = _load_profile(path)
    count = len(profile["operators"])
    return {
        "ok": True,
        "configured": True,
        "agent_class_id": profile["agent_class_id"],
        "agent_display_name": profile["agent_display_name"],
        "profile_id": profile["profile_id"],
        "agent_instance_id": profile["agent_instance_id"],
        "identity_scope": profile["identity_scope"],
        "operator_count": count,
        "operator_selection_required": count > 1,
        "active_operator_id": None,
    }


def _init(args: argparse.Namespace, path: Path, timeout: float) -> InitResult:
    if _profile_exists(path):
        raise ProfileError("profile_exists", "identity profile already exists")
    # Validate and assemble the complete document before creating any private
    # runtime paths.  A malformed first-run command therefore remains wholly
    # side-effect free.
    timestamp = _utc_now()
    operator = _operator(
        args.preferred_name,
        args.display_name,
        args.email,
        args.provider_principal,
        timestamp=timestamp,
    )
    profile = _profile(operator, timestamp)
    _ensure_private_directory(path.parent)
    with _with_profile_lock(path, exclusive=True, timeout=timeout):
        if _profile_exists(path):
            raise ProfileError("profile_exists", "identity profile already exists")
        _atomic_write(path, profile, replace=False)
    return {
        "ok": True,
        "configured": True,
        "profile_path": str(path),
        "profile_id": profile["profile_id"],
        "installation_id": profile["installation_id"],
        "agent_instance_id": profile["agent_instance_id"],
        "agent_class_id": AGENT_CLASS_ID,
        "agent_display_name": AGENT_DISPLAY_NAME,
        "identity_scope": IDENTITY_SCOPE,
        "operator": _operator_summary(operator),
        "operator_count": 1,
    }


def _add_operator(args: argparse.Namespace, path: Path, timeout: float) -> AddOperatorResult:
    if not _profile_exists(path):
        raise ProfileError("unconfigured", "Tedd identity profile is not configured")
    with _with_profile_lock(path, exclusive=True, timeout=timeout):
        profile = _load_profile(path)
        if len(profile["operators"]) >= MAX_OPERATORS:
            raise ProfileError("invalid_input", "profile has reached the operator limit")
        timestamp = _next_timestamp(profile["updated_at"])
        operator = _operator(
            args.preferred_name,
            args.display_name,
            args.email,
            args.provider_principal,
            timestamp=timestamp,
        )
        candidate = copy.deepcopy(profile)
        candidate["operators"].append(operator)
        candidate["updated_at"] = timestamp
        validate_profile(candidate)
        _atomic_write(path, candidate, replace=True)
    return {
        "ok": True,
        "profile_id": candidate["profile_id"],
        "operator": _operator_summary(operator),
        "operator_count": len(candidate["operators"]),
        "operator_selection_required": len(candidate["operators"]) > 1,
    }


def _bind_identity(args: argparse.Namespace, path: Path, timeout: float) -> BindIdentityResult:
    """Add confirmed identity keys to one existing operator.

    Bindings are monotonic: this command has no deletion, replacement, or
    reassignment path.  Repeating an already-present binding is a successful
    no-op and leaves both the document bytes and timestamps unchanged.
    """

    operator_id = _input_operator_id(args.operator_id)
    requested_emails = _input_email_aliases(args.email, required=False)
    requested_principals = _input_principal_bindings(args.provider_principal)
    if not requested_emails and not requested_principals:
        raise ProfileError(
            "invalid_input",
            "bind-identity requires at least one --email or --provider-principal",
        )
    if not _profile_exists(path):
        raise ProfileError("unconfigured", "Tedd identity profile is not configured")

    with _with_profile_lock(path, exclusive=True, timeout=timeout):
        profile = _load_profile(path)
        operator_index = next(
            (
                index
                for index, operator in enumerate(profile["operators"])
                if operator["operator_id"] == operator_id
            ),
            None,
        )
        if operator_index is None:
            raise ProfileError("unknown_operator", f"unknown operator ID: {operator_id}")
        operator = profile["operators"][operator_index]

        existing_emails = {_normalized_email(alias) for alias in operator["email_aliases"]}
        new_emails = [
            email
            for email in requested_emails
            if _normalized_email(email, input_value=True) not in existing_emails
        ]
        existing_principals = {
            (binding["provider"], binding["principal"])
            for binding in operator["provider_principals"]
        }
        new_principals = [
            binding
            for binding in requested_principals
            if (binding["provider"], binding["principal"]) not in existing_principals
        ]
        if not new_emails and not new_principals:
            return {
                "ok": True,
                "updated": False,
                "profile_id": profile["profile_id"],
                "operator": _operator_summary(operator),
                "email_alias_count": len(operator["email_aliases"]),
                "provider_principal_count": len(operator["provider_principals"]),
            }
        if len(operator["email_aliases"]) + len(new_emails) > MAX_EMAIL_ALIASES:
            raise ProfileError("invalid_input", "operator would exceed the email alias limit")
        if len(operator["provider_principals"]) + len(new_principals) > MAX_PROVIDER_PRINCIPALS:
            raise ProfileError(
                "invalid_input", "operator would exceed the provider principal limit"
            )

        candidate = copy.deepcopy(profile)
        candidate_operator = candidate["operators"][operator_index]
        candidate_operator["email_aliases"].extend(new_emails)
        candidate_operator["provider_principals"].extend(new_principals)
        timestamp = _next_timestamp(profile["updated_at"])
        candidate_operator["updated_at"] = timestamp
        candidate["updated_at"] = timestamp
        validate_profile(candidate)
        _atomic_write(path, candidate, replace=True)
    return {
        "ok": True,
        "updated": True,
        "profile_id": candidate["profile_id"],
        "operator": _operator_summary(candidate_operator),
        "email_alias_count": len(candidate_operator["email_aliases"]),
        "provider_principal_count": len(candidate_operator["provider_principals"]),
    }


def _show(path: Path, timeout: float, *, full: bool) -> ShowResult:
    if not _profile_exists(path):
        raise ProfileError("unconfigured", "Tedd identity profile is not configured")
    with _with_profile_lock(path, exclusive=False, timeout=timeout):
        profile = _load_profile(path)
    return {
        "ok": True,
        "redacted": not full,
        "profile": profile if full else _redacted_profile(profile),
    }


def _validate(
    path: Path, timeout: float
) -> UnconfiguredValidationResult | ConfiguredValidationResult:
    if not _profile_exists(path):
        return {
            "ok": True,
            "configured": False,
            "valid": False,
            "profile_path": str(path),
        }
    with _with_profile_lock(path, exclusive=False, timeout=timeout):
        profile = _load_profile(path)
    return {
        "ok": True,
        "configured": True,
        "valid": True,
        "schema_version": profile["schema_version"],
        "profile_id": profile["profile_id"],
        "operator_count": len(profile["operators"]),
    }


def _resolve(args: argparse.Namespace, path: Path, timeout: float) -> ResolveResult:
    if not _profile_exists(path):
        raise ProfileError("unconfigured", "Tedd identity profile is not configured")
    email_query = args.email
    provider_query = args.provider
    principal_query = args.principal
    using_email = email_query is not None
    using_provider = provider_query is not None or principal_query is not None
    if using_email == using_provider:
        raise ProfileError(
            "invalid_arguments",
            "resolve requires either --email or both --provider and --principal",
        )
    if using_provider and (provider_query is None or principal_query is None):
        raise ProfileError("invalid_arguments", "--provider and --principal must be used together")
    if using_email:
        normalized_email = _normalized_email(
            _email(email_query, input_value=True), input_value=True
        )
        match_kind = "email"
    else:
        provider = _provider(provider_query, input_value=True)
        principal = _principal(principal_query, input_value=True)
        match_kind = "provider_principal"

    with _with_profile_lock(path, exclusive=False, timeout=timeout):
        profile = _load_profile(path)
    matches: list[OperatorProfile] = []
    for operator in profile["operators"]:
        if using_email:
            matched = any(
                _normalized_email(alias) == normalized_email for alias in operator["email_aliases"]
            )
        else:
            matched = any(
                binding["provider"] == provider and binding["principal"] == principal
                for binding in operator["provider_principals"]
            )
        if matched:
            matches.append(operator)
    if len(matches) > 1:
        # Validation should make this unreachable, but fail closed if a future
        # schema or caller weakens that invariant.
        raise ProfileError("ambiguous_identity", "identity resolves to multiple operators")
    return {
        "ok": True,
        "found": bool(matches),
        "matched_by": match_kind,
        "identity_scope": IDENTITY_SCOPE,
        "authority_asserted": False,
        "operator": _operator_summary(matches[0]) if matches else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(description="Manage Tedd's private local identity profile")
    parser.add_argument("--profile", help="profile path (default: ~/.codex/tedd/profile.json)")
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=2.0,
        help=f"bounded lock wait in seconds (maximum {MAX_LOCK_TIMEOUT_SECONDS:g})",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=JSONArgumentParser
    )
    subparsers.add_parser("status", help="report configured state without creating a profile")

    init_parser = subparsers.add_parser("init", help="create a new private profile")
    init_parser.add_argument(
        "--preferred-name",
        "--name",
        dest="preferred_name",
        required=True,
        help="preferred form of address (--name is the first-run shorthand)",
    )
    init_parser.add_argument("--display-name")
    init_parser.add_argument("--email", action="append", required=True)
    init_parser.add_argument(
        "--provider-principal",
        action="append",
        default=[],
        metavar="PROVIDER=PRINCIPAL",
    )

    add_parser = subparsers.add_parser(
        "add-operator", help="append an operator without selecting one"
    )
    add_parser.add_argument(
        "--preferred-name",
        "--name",
        dest="preferred_name",
        required=True,
        help="preferred form of address",
    )
    add_parser.add_argument("--display-name")
    add_parser.add_argument("--email", action="append", required=True)
    add_parser.add_argument(
        "--provider-principal",
        action="append",
        default=[],
        metavar="PROVIDER=PRINCIPAL",
    )

    bind_parser = subparsers.add_parser(
        "bind-identity",
        help="add confirmed email or provider identities to an existing operator",
    )
    bind_parser.add_argument("--operator-id", required=True)
    bind_parser.add_argument("--email", action="append", default=[])
    bind_parser.add_argument(
        "--provider-principal",
        action="append",
        default=[],
        metavar="PROVIDER=PRINCIPAL",
    )

    show_parser = subparsers.add_parser(
        "show", help="show the profile with contact values redacted"
    )
    show_parser.add_argument(
        "--full", action="store_true", help="include email and principal values"
    )
    subparsers.add_parser("validate", help="validate schema, ownership, paths, and permissions")

    resolve_parser = subparsers.add_parser("resolve", help="resolve one exact identity key")
    resolve_parser.add_argument("--email")
    resolve_parser.add_argument("--provider")
    resolve_parser.add_argument("--principal")
    return parser


def _dispatch(args: argparse.Namespace) -> ProfileCommandResult:
    path = _path(args.profile)
    timeout = args.lock_timeout
    if not math.isfinite(timeout) or timeout < 0 or timeout > MAX_LOCK_TIMEOUT_SECONDS:
        raise ProfileError(
            "invalid_input",
            f"lock timeout must be a finite number between 0 and {MAX_LOCK_TIMEOUT_SECONDS:g}",
        )
    if args.command == "status":
        return _status(path, timeout)
    if args.command == "init":
        return _init(args, path, timeout)
    if args.command == "add-operator":
        return _add_operator(args, path, timeout)
    if args.command == "bind-identity":
        return _bind_identity(args, path, timeout)
    if args.command == "show":
        return _show(path, timeout, full=args.full)
    if args.command == "validate":
        return _validate(path, timeout)
    if args.command == "resolve":
        return _resolve(args, path, timeout)
    raise ProfileError("invalid_arguments", f"unsupported command: {args.command}")


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _configure_standard_streams() -> None:
    """Emit the JSON CLI contract as UTF-8 on every supported host."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_standard_streams()
    try:
        args = build_parser().parse_args(argv)
        result = _dispatch(args)
    except ProfileError as error:
        _emit({"ok": False, "error": {"code": error.code, "message": error.message}})
        return 2
    except OSError as error:
        _emit({"ok": False, "error": {"code": "io_error", "message": str(error)}})
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
