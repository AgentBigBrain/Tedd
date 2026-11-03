#!/usr/bin/env python3
"""Pure Windows private-ACL policy shared by Tedd runtime components.

Win32 descriptor acquisition remains in the platform adapters.  This module owns
the security decision so the profile and knowledge graph cannot silently diverge
about owners, inheritance, sandbox mutation, or untrusted principals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

INHERIT_ONLY_ACE = 0x08
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02

FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_EXECUTE = 0x00000020
FILE_DELETE_CHILD = 0x00000040
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
SYNCHRONIZE = 0x00100000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000

FILE_GENERIC_READ = (
    READ_CONTROL | FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_READ_EA | SYNCHRONIZE
)
FILE_GENERIC_WRITE = (
    READ_CONTROL
    | FILE_WRITE_DATA
    | FILE_WRITE_ATTRIBUTES
    | FILE_WRITE_EA
    | FILE_APPEND_DATA
    | SYNCHRONIZE
)
FILE_GENERIC_EXECUTE = READ_CONTROL | FILE_READ_ATTRIBUTES | FILE_EXECUTE | SYNCHRONIZE
FILE_ALL_ACCESS = 0x001F01FF
WINDOWS_RELEVANT_ACCESS = (
    FILE_GENERIC_READ
    | FILE_GENERIC_WRITE
    | FILE_GENERIC_EXECUTE
    | FILE_DELETE_CHILD
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
)
WINDOWS_MUTATING_ACCESS = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_DELETE_CHILD
    | FILE_WRITE_ATTRIBUTES
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | GENERIC_WRITE
    | GENERIC_ALL
)


class WindowsAce(Protocol):
    """Structural view of one copied access-allowed ACE."""

    @property
    def sid(self) -> str | None: ...

    @property
    def mask(self) -> int: ...

    @property
    def flags(self) -> int: ...


class WindowsSecuritySnapshot(Protocol):
    """Structural owner/DACL facts required by the policy evaluator."""

    @property
    def owner_sid(self) -> str | None: ...

    @property
    def dacl_present(self) -> bool: ...

    @property
    def dacl_protected(self) -> bool: ...

    @property
    def allowed_aces(self) -> tuple[WindowsAce, ...]: ...


@dataclass(frozen=True)
class PrivateAclPrincipals:
    """Canonical principals participating in one private-ACL decision."""

    current_user_sid: str
    owner_rights_sid: str
    trusted_sids: frozenset[str]
    sandbox_sid: str | None


def expanded_file_mask(mask: int) -> int:
    """Expand generic Windows access bits into concrete file rights."""

    expanded = mask
    if mask & GENERIC_ALL:
        expanded |= FILE_ALL_ACCESS
    if mask & GENERIC_READ:
        expanded |= FILE_GENERIC_READ
    if mask & GENERIC_WRITE:
        expanded |= FILE_GENERIC_WRITE
    if mask & GENERIC_EXECUTE:
        expanded |= FILE_GENERIC_EXECUTE
    return expanded


def private_acl_problem(
    snapshot: WindowsSecuritySnapshot,
    principals: PrivateAclPrincipals,
    *,
    is_directory: bool,
    require_protected: bool,
) -> str | None:
    """Return why copied Windows owner/DACL facts are not private."""

    if snapshot.owner_sid != principals.current_user_sid:
        return "must be owned by the current Windows user"
    if not snapshot.dacl_present:
        return "must have a non-null protected access-control list"
    if require_protected and not snapshot.dacl_protected:
        return "must have inheritance disabled on its access-control list"

    trusted_sids = {
        *principals.trusted_sids,
        principals.current_user_sid,
        principals.owner_rights_sid,
    }
    if principals.sandbox_sid is not None:
        trusted_sids.add(principals.sandbox_sid)

    current_access = 0
    for ace in snapshot.allowed_aces:
        applies_here = not ace.flags & INHERIT_ONLY_ACE
        can_reach_child = is_directory and bool(
            ace.flags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
        )
        if not applies_here and not can_reach_child:
            continue
        expanded = expanded_file_mask(ace.mask)
        if ace.sid in {principals.current_user_sid, principals.owner_rights_sid} and applies_here:
            current_access |= expanded
        if (
            principals.sandbox_sid is not None
            and ace.sid == principals.sandbox_sid
            and expanded & WINDOWS_MUTATING_ACCESS
        ):
            return "must grant CodexSandboxUsers read/execute access only"
        if ace.sid not in trusted_sids and expanded & WINDOWS_RELEVANT_ACCESS:
            return "must not grant access to untrusted Windows principals"
    if current_access & FILE_ALL_ACCESS != FILE_ALL_ACCESS:
        return "must grant the current Windows user full control"
    return None
