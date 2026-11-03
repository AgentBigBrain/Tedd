"""Owner-bound Google credentials, separate from source evidence and graph data.

Windows uses current-user DPAPI; Linux uses the user's Secret Service; macOS
uses the user's Keychain. There is no plaintext fallback. This does not defend
against an unrestricted process running as the same OS user or an administrator.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from . import source_files
from .source_contracts import SourceArchiveError
from .source_macos_credentials import keychain as _macos_keychain

_MAXIMUM = 65536


def _secret_tool(operation: str, key: str, *, payload: bytes | None = None) -> bytes | None:
    """Use the system keyring client, never shell arguments for secret material."""
    executable = Path("/usr/bin/secret-tool")
    try:
        info = executable.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SourceArchiveError("privacy_rejected")
        command = [str(executable), operation]
        if operation == "store":
            command += ["--label=Tedd source connection"]
        command += ["application", "org.tedd.sources", "binding", key]
        result = subprocess.run(
            command,
            input=base64.b64encode(payload) if payload is not None else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceArchiveError("capability_unavailable") from error
    if result.returncode == 1 and not result.stdout and not result.stderr and operation != "store":
        return None
    if result.returncode != 0:
        raise SourceArchiveError("capability_unavailable")
    if operation != "lookup":
        return None
    try:
        if len(result.stdout) > (_MAXIMUM * 4 // 3) + 8:
            raise ValueError("oversized credential")
        value = base64.b64decode(result.stdout.rstrip(b"\n"), validate=True)
        if not 0 < len(value) <= _MAXIMUM:
            raise ValueError("invalid credential size")
        return value
    except ValueError as error:
        raise SourceArchiveError("privacy_rejected") from error


def _windows_protect(payload: bytes, key: str, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise SourceArchiveError("capability_unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source = ctypes.create_string_buffer(payload)
    entropy = ctypes.create_string_buffer(("tedd-google-credential-v1:" + key).encode("ascii"))
    source_blob = Blob(len(payload), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = Blob(len(entropy) - 1, ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(Blob),
    ]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; never machine-wide protection or a prompt.
        if not function(
            ctypes.byref(source_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            1,
            ctypes.byref(output),
        ):
            raise SourceArchiveError("privacy_rejected")
        if not 0 < output.size <= (_MAXIMUM if decrypt else _MAXIMUM + 4096):
            raise SourceArchiveError("privacy_rejected")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(source, 0, len(source))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


class CredentialStore:
    """Only opaque per-account/client keys; never include credentials in repr."""

    def __init__(self, root: Path) -> None:
        self._root = source_files.private_root(root)
        self._root = self.root

    @property
    def root(self) -> Path:
        path = source_files.private_root(self._root)
        if (path / "archive.json").exists():
            raise SourceArchiveError("privacy_rejected")
        return path

    def _path(self, key: str) -> Path:
        root = self.root
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise SourceArchiveError("privacy_rejected")
        if sys.platform not in {"win32", "linux", "darwin"}:
            raise SourceArchiveError("capability_unavailable")
        return root / (key + ".credential")

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if sys.platform == "linux":
            return _secret_tool("lookup", self.binding(key))
        if sys.platform == "darwin":
            return _macos_keychain("lookup", self.binding(key))
        try:
            protected = source_files.read_bytes(path, _MAXIMUM + 4096)
        except FileNotFoundError:
            return None
        return _windows_protect(protected, key, decrypt=True)

    def put(self, key: str, payload: bytes) -> None:
        path = self._path(key)
        if not 0 < len(payload) <= _MAXIMUM:
            raise SourceArchiveError("limit_exceeded")
        if sys.platform == "linux":
            _secret_tool("store", self.binding(key), payload=payload)
            return
        if sys.platform == "darwin":
            _macos_keychain("store", self.binding(key), payload=payload)
            return
        try:
            source_files.check_entry(path)
        except FileNotFoundError:
            pass
        protected = _windows_protect(payload, key, decrypt=False)
        source_files.write_bytes(path, protected, overwrite=True)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if sys.platform == "linux":
            _secret_tool("clear", self.binding(key))
            return
        if sys.platform == "darwin":
            _macos_keychain("clear", self.binding(key))
            return
        try:
            info = source_files.check_entry(path)
            protected = source_files.read_bytes(path, _MAXIMUM + 4096)
        except FileNotFoundError:
            return
        if not source_files.mutate_owned_preview(path, (info.st_dev, info.st_ino), protected, None):
            raise SourceArchiveError("source_unavailable")

    def binding(self, key: str) -> str:
        """Opaque root/account/client identity shared by keyring and lifecycle state."""
        self._path(key)
        # Separate installations/test roots without exposing paths to the service.
        return hashlib.sha256((str(self._root) + "\0" + key).encode("utf-8")).hexdigest()
