#!/usr/bin/env python3
"""Cross-platform tests for the pure Windows private-ACL policy."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

import windows_acl_policy as policy  # noqa: E402


@dataclass(frozen=True)
class Ace:
    sid: str | None
    mask: int
    flags: int = 0


@dataclass(frozen=True)
class Snapshot:
    owner_sid: str | None
    dacl_present: bool
    dacl_protected: bool
    allowed_aces: tuple[Ace, ...]


class PrivateAclPolicyTests(unittest.TestCase):
    USER = "S-user"
    OWNER_RIGHTS = "S-owner-rights"
    SANDBOX = "S-sandbox"
    SYSTEM = "S-system"
    PRINCIPALS = policy.PrivateAclPrincipals(
        current_user_sid=USER,
        owner_rights_sid=OWNER_RIGHTS,
        trusted_sids=frozenset({USER, OWNER_RIGHTS, SANDBOX, SYSTEM}),
        sandbox_sid=SANDBOX,
    )

    def snapshot(self, *aces: Ace, protected: bool = True) -> Snapshot:
        return Snapshot(self.USER, True, protected, tuple(aces))

    def full_user_ace(self) -> Ace:
        return Ace(self.USER, policy.GENERIC_ALL)

    def problem(self, snapshot: Snapshot, *, directory: bool = False) -> str | None:
        return policy.private_acl_problem(
            snapshot,
            self.PRINCIPALS,
            is_directory=directory,
            require_protected=True,
        )

    def test_private_acl_accepts_sandbox_read_execute_only(self) -> None:
        snapshot = self.snapshot(
            self.full_user_ace(),
            Ace(self.SANDBOX, policy.GENERIC_READ | policy.GENERIC_EXECUTE),
        )

        self.assertIsNone(self.problem(snapshot))

    def test_owner_null_dacl_and_inheritance_fail_closed(self) -> None:
        cases = (
            Snapshot("S-other", True, True, (self.full_user_ace(),)),
            Snapshot(self.USER, False, True, ()),
            self.snapshot(self.full_user_ace(), protected=False),
        )

        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                self.assertIsNotNone(self.problem(snapshot))

    def test_inherited_acl_can_be_accepted_when_caller_allows_it(self) -> None:
        snapshot = self.snapshot(self.full_user_ace(), protected=False)

        self.assertIsNone(
            policy.private_acl_problem(
                snapshot,
                self.PRINCIPALS,
                is_directory=False,
                require_protected=False,
            )
        )

    def test_sandbox_mutation_is_rejected(self) -> None:
        snapshot = self.snapshot(
            self.full_user_ace(),
            Ace(self.SANDBOX, policy.GENERIC_ALL),
        )

        self.assertEqual(
            self.problem(snapshot),
            "must grant CodexSandboxUsers read/execute access only",
        )

    def test_untrusted_principal_access_is_rejected(self) -> None:
        snapshot = self.snapshot(
            self.full_user_ace(),
            Ace("S-untrusted", policy.GENERIC_READ),
        )

        self.assertEqual(
            self.problem(snapshot),
            "must not grant access to untrusted Windows principals",
        )

    def test_inherit_only_mutation_is_rejected_for_directory_children(self) -> None:
        inherited = Ace(
            "S-untrusted",
            policy.GENERIC_WRITE,
            policy.INHERIT_ONLY_ACE | policy.OBJECT_INHERIT_ACE,
        )
        snapshot = self.snapshot(self.full_user_ace(), inherited)

        self.assertIsNone(self.problem(snapshot, directory=False))
        self.assertEqual(
            self.problem(snapshot, directory=True),
            "must not grant access to untrusted Windows principals",
        )

    def test_sandbox_inherit_only_mutation_is_rejected_for_directory_children(self) -> None:
        inherited = Ace(
            self.SANDBOX,
            policy.GENERIC_WRITE,
            policy.INHERIT_ONLY_ACE | policy.CONTAINER_INHERIT_ACE,
        )
        snapshot = self.snapshot(self.full_user_ace(), inherited)

        self.assertIsNone(self.problem(snapshot, directory=False))
        self.assertEqual(
            self.problem(snapshot, directory=True),
            "must grant CodexSandboxUsers read/execute access only",
        )

    def test_current_user_requires_full_control(self) -> None:
        snapshot = self.snapshot(Ace(self.USER, policy.GENERIC_READ))

        self.assertEqual(
            self.problem(snapshot),
            "must grant the current Windows user full control",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
