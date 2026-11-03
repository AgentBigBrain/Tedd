"""Synthetic credential boundaries; native Keychain tests require explicit opt-in."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph import source_credentials as credentials
from tedd_knowledge_graph import source_macos_credentials as macos
from tedd_knowledge_graph.source_contracts import SourceArchiveError

if TYPE_CHECKING:
    from _ctypes import _CArgObject


class MacCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=private_temporary_parent())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        self.store = credentials.CredentialStore(self.root)

    def test_mac_store_roundtrip_is_bound_to_root_and_key_without_credential_files(self) -> None:
        saved: dict[str, bytes] = {}

        def keychain(operation: str, key: str, *, payload: bytes | None = None) -> bytes | None:
            if operation == "store":
                assert payload is not None
                saved[key] = payload
            elif operation == "clear":
                saved.pop(key, None)
            return saved.get(key) if operation == "lookup" else None

        other_root = self.root / "other"
        other_root.mkdir(mode=0o700)
        set_private_path_mode(other_root, 0o700)
        other = credentials.CredentialStore(other_root)
        with (
            mock.patch.object(credentials.sys, "platform", "darwin"),
            mock.patch.object(credentials, "_macos_keychain", side_effect=keychain),
        ):
            self.store.put("a" * 64, b"synthetic-first")
            self.assertEqual(self.store.get("a" * 64), b"synthetic-first")
            self.assertIsNone(self.store.get("b" * 64))
            self.assertIsNone(other.get("a" * 64))
            self.store.put("a" * 64, b"synthetic-replacement")
            self.assertEqual(self.store.get("a" * 64), b"synthetic-replacement")
            self.store.delete("a" * 64)
            self.store.delete("a" * 64)
            self.assertIsNone(self.store.get("a" * 64))
        self.assertEqual(list(self.root.iterdir()), [other_root])
        self.assertEqual(list(other_root.iterdir()), [])

    def test_mac_invalid_payload_or_key_never_reaches_keychain(self) -> None:
        with (
            mock.patch.object(credentials.sys, "platform", "darwin"),
            mock.patch.object(credentials, "_macos_keychain") as keychain,
        ):
            for payload in (b"", b"x" * 65537):
                with (
                    self.subTest(size=len(payload)),
                    self.assertRaises(SourceArchiveError) as raised,
                ):
                    self.store.put("a" * 64, payload)
                self.assertEqual(str(raised.exception), "limit_exceeded")
            with self.assertRaises(SourceArchiveError) as raised:
                self.store.get("invalid")
            self.assertEqual(str(raised.exception), "privacy_rejected")
            keychain.assert_not_called()

    @unittest.skipUnless(
        sys.platform == "darwin" and os.environ.get("TEDD_TEST_MACOS_KEYCHAIN") == "1",
        "explicit synthetic native Keychain opt-in required",
    )
    def test_native_mac_roundtrip_reopen_isolation_and_exact_cleanup(self) -> None:
        # The random private temporary root makes these account bindings unique.
        # No listing, consent, or access to an existing account/keychain item.
        key = "c" * 64
        other_key = "d" * 64
        other_root = self.root / "other"
        other_root.mkdir(mode=0o700)
        self.assertIsNone(self.store.get(key))
        try:
            self.store.put(key, b"synthetic-mac-first\0binary")
            reopened = credentials.CredentialStore(self.root)
            self.assertEqual(reopened.get(key), b"synthetic-mac-first\0binary")
            self.assertIsNone(reopened.get(other_key))
            self.assertIsNone(credentials.CredentialStore(other_root).get(key))
            self.store.put(key, b"synthetic-mac-replacement")
            self.assertEqual(reopened.get(key), b"synthetic-mac-replacement")
            child = subprocess.run(
                [sys.executable, "-", str(self.root)],
                input=(
                    "from tests import knowledge_graph\n"
                    "from pathlib import Path\n"
                    "import sys\n"
                    "from tedd_knowledge_graph.source_credentials import CredentialStore\n"
                    "assert CredentialStore(Path(sys.argv[1])).get('c' * 64) "
                    "== b'synthetic-mac-replacement'\n"
                ),
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(child.returncode, 0, "synthetic cross-process Keychain reopen failed")
            self.assertEqual(child.stdout, "")
            self.assertEqual(child.stderr, "")
            self.assertEqual(list(self.root.iterdir()), [other_root])
            self.assertEqual(list(other_root.iterdir()), [])
        finally:
            self.store.delete(key)
        self.assertIsNone(self.store.get(key))
        self.store.delete(key)


class MacKeychainBoundaryTests(unittest.TestCase):
    """Replace only Security.framework calls, never the credential state machine."""

    def setUp(self) -> None:
        self.security = mock.Mock()
        self.core = mock.Mock()
        self.interaction = True
        self.saved: bytes | None = None
        self.find_status = 0
        self.buffers: list[ctypes.Array[ctypes.c_char]] = []
        self.security.SecKeychainGetUserInteractionAllowed.side_effect = self._get_interaction
        self.security.SecKeychainSetUserInteractionAllowed.side_effect = self._set_interaction
        self.security.SecKeychainCopyDomainDefault.side_effect = self._default
        self.security.SecKeychainFindGenericPassword.side_effect = self._find
        self.security.SecKeychainAddGenericPassword.side_effect = self._add
        self.security.SecKeychainItemModifyAttributesAndData.side_effect = self._update
        self.security.SecKeychainItemDelete.side_effect = self._delete
        self.security.SecKeychainItemFreeContent.return_value = 0
        patcher = mock.patch.object(macos, "_libraries", return_value=(self.security, self.core))
        patcher.start()
        self.addCleanup(patcher.stop)
        platform = mock.patch.object(sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)

    def _get_interaction(self, state: _CArgObject) -> int:
        ctypes.cast(state, ctypes.POINTER(ctypes.c_ubyte))[0] = self.interaction
        return 0

    def _set_interaction(self, state: bool) -> int:
        self.interaction = bool(state)
        return 0

    def _default(self, domain: int, result: _CArgObject) -> int:
        self.assertFalse(self.interaction)
        self.assertEqual(domain, 0)  # User domain; never system/search-list defaults.
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = 11
        return 0

    def _find(
        self,
        keychain: ctypes.c_void_p,
        service_length: int,
        service: bytes,
        account_length: int,
        account: bytes,
        length: _CArgObject | None,
        data: _CArgObject | None,
        item: _CArgObject,
    ) -> int:
        self.assertFalse(self.interaction)
        self.assertEqual(keychain.value, 11)
        self.assertEqual(service[:service_length], b"org.tedd.sources.google.v1")
        self.assertEqual(account[:account_length], b"a" * 64)
        if self.find_status:
            return self.find_status
        if self.saved is None:
            return -25300
        ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 22
        if data is not None:
            assert length is not None
            buffer = ctypes.create_string_buffer(self.saved)
            self.buffers.append(buffer)
            ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(self.saved)
        return 0

    def _add(
        self,
        keychain: ctypes.c_void_p,
        service_length: int,
        service: bytes,
        account_length: int,
        account: bytes,
        size: int,
        data: ctypes.Array[ctypes.c_char],
        item: object,
    ) -> int:
        self.assertFalse(self.interaction)
        self.assertEqual(keychain.value, 11)
        self.assertEqual(service[:service_length], b"org.tedd.sources.google.v1")
        self.assertEqual(account[:account_length], b"a" * 64)
        self.saved = ctypes.string_at(data, size)
        return 0

    def _update(
        self,
        item: ctypes.c_void_p,
        attributes: object,
        size: int,
        data: ctypes.Array[ctypes.c_char],
    ) -> int:
        self.assertFalse(self.interaction)
        self.assertEqual(item.value, 22)
        self.assertIsNone(attributes)
        self.saved = ctypes.string_at(data, size)
        return 0

    def _delete(self, item: ctypes.c_void_p) -> int:
        self.assertFalse(self.interaction)
        self.assertEqual(item.value, 22)
        self.saved = None
        return 0

    def test_exact_item_roundtrip_and_noninteractive_state_restoration(self) -> None:
        try:
            self.assertIsNone(macos.keychain("lookup", "a" * 64))
            macos.keychain("store", "a" * 64, payload=b"synthetic\0first")
            self.assertEqual(macos.keychain("lookup", "a" * 64), b"synthetic\0first")
            macos.keychain("store", "a" * 64, payload=b"synthetic-replaced")
            self.assertEqual(macos.keychain("lookup", "a" * 64), b"synthetic-replaced")
            macos.keychain("clear", "a" * 64)
            macos.keychain("clear", "a" * 64)
            self.assertIsNone(macos.keychain("lookup", "a" * 64))
        except SourceArchiveError as error:
            self.fail(f"synthetic Keychain roundtrip refused: {error}")
        self.assertTrue(self.interaction)
        self.assertTrue(all(buffer.raw == b"\0" * len(buffer) for buffer in self.buffers))
        self.assertEqual(self.security.SecKeychainItemFreeContent.call_count, 2)

    def test_native_errors_are_not_misreported_as_absent_credentials(self) -> None:
        for status, expected in (
            (-25291, "capability_unavailable"),
            (-25294, "capability_unavailable"),
            (-25307, "capability_unavailable"),
            (-25312, "capability_unavailable"),
            (-67585, "capability_unavailable"),
            (-4, "capability_unavailable"),
            (-25293, "privacy_rejected"),
            (-25308, "privacy_rejected"),
            (-25315, "privacy_rejected"),
            (-25295, "privacy_rejected"),
            (-25243, "privacy_rejected"),
            (-34020, "privacy_rejected"),
            (-26275, "privacy_rejected"),
            (-36, "source_unavailable"),
        ):
            with self.subTest(status=status):
                self.find_status = status
                with self.assertRaises(SourceArchiveError) as raised:
                    macos.keychain("lookup", "a" * 64)
                self.assertEqual(str(raised.exception), expected)
                self.assertTrue(self.interaction)

    def test_rejects_empty_and_oversized_native_values_and_releases_buffers(self) -> None:
        for payload in (b"", b"x" * 65537):
            with self.subTest(size=len(payload)):
                self.saved = payload
                with self.assertRaises(SourceArchiveError) as raised:
                    macos.keychain("lookup", "a" * 64)
                self.assertEqual(str(raised.exception), "privacy_rejected")
                self.assertTrue(self.interaction)
        self.assertEqual(self.security.SecKeychainItemFreeContent.call_count, 2)

    def test_refuses_invalid_operation_or_binding_before_loading_frameworks(self) -> None:
        for operation, key in (("list", "a" * 64), ("clear", ""), ("lookup", "a" * 63)):
            with self.subTest(operation=operation, key=key):
                with self.assertRaises(SourceArchiveError) as raised:
                    macos.keychain(operation, key)
                self.assertEqual(str(raised.exception), "privacy_rejected")
        self.security.SecKeychainCopyDomainDefault.assert_not_called()

    def test_preserves_already_disabled_interaction(self) -> None:
        self.interaction = False
        try:
            self.assertIsNone(macos.keychain("lookup", "a" * 64))
        except SourceArchiveError as error:
            self.fail(f"synthetic missing item refused: {error}")
        self.assertFalse(self.interaction)

    def test_failed_mutations_preserve_items_and_restore_interaction(self) -> None:
        cases = (
            ("store", None, "SecKeychainAddGenericPassword"),
            ("store", b"synthetic-original", "SecKeychainItemModifyAttributesAndData"),
            ("clear", b"synthetic-original", "SecKeychainItemDelete"),
        )
        for operation, original, function in cases:
            with self.subTest(operation=operation, function=function):
                self.saved = original
                with (
                    mock.patch.object(self.security, function, return_value=-25293),
                    self.assertRaises(SourceArchiveError) as raised,
                ):
                    macos.keychain(
                        operation,
                        "a" * 64,
                        payload=b"synthetic-new" if operation == "store" else None,
                    )
                self.assertEqual(str(raised.exception), "privacy_rejected")
                self.assertEqual(self.saved, original)
                self.assertTrue(self.interaction)

    def test_no_interaction_guard_failure_prevents_keychain_access(self) -> None:
        with (
            mock.patch.object(
                self.security, "SecKeychainSetUserInteractionAllowed", return_value=-36
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            macos.keychain("lookup", "a" * 64)
        self.assertEqual(str(raised.exception), "source_unavailable")
        self.security.SecKeychainCopyDomainDefault.assert_not_called()

    def test_rejects_invalid_native_payloads_before_touching_keychain(self) -> None:
        for payload in (None, b"", b"x" * 65537):
            with self.subTest(size=None if payload is None else len(payload)):
                with self.assertRaises(SourceArchiveError) as raised:
                    macos.keychain("store", "a" * 64, payload=payload)
                self.assertEqual(str(raised.exception), "limit_exceeded")
        with self.assertRaises(SourceArchiveError) as raised:
            macos.keychain("clear", "a" * 64, payload=b"synthetic-unexpected")
        self.assertEqual(str(raised.exception), "privacy_rejected")
        self.security.SecKeychainCopyDomainDefault.assert_not_called()

    def test_maximum_sized_item_remains_usable(self) -> None:
        macos.keychain("store", "a" * 64, payload=b"x" * 65536)
        self.assertEqual(macos.keychain("lookup", "a" * 64), b"x" * 65536)
        self.assertTrue(self.interaction)


class MacKeychainAvailabilityTests(unittest.TestCase):
    def test_missing_framework_fails_closed_without_alternate_storage(self) -> None:
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(macos.ctypes, "CDLL", side_effect=OSError("synthetic unavailable")),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            macos.keychain("lookup", "a" * 64)
        self.assertEqual(str(raised.exception), "capability_unavailable")

    def test_other_platform_never_loads_native_framework(self) -> None:
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(macos.ctypes, "CDLL") as load,
            self.assertRaises(SourceArchiveError) as raised,
        ):
            macos.keychain("lookup", "a" * 64)
        self.assertEqual(str(raised.exception), "capability_unavailable")
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
