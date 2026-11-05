"""Preflight only the Unix-socket capability required by disposable fixtures."""

from __future__ import annotations

import errno
import socket
import unittest
from pathlib import Path
from typing import cast


def require_unix_socket(path: Path) -> None:
    """Create, bind and remove a probe at a fresh path inside a private fixture."""
    family = cast(int | None, socket.__dict__.get("AF_UNIX"))
    if family is None:
        raise unittest.SkipTest("Unix-domain sockets are unavailable on this host")
    listener: socket.socket | None = None
    bound = False
    try:
        listener = socket.socket(family)
        listener.bind(str(path))
        bound = True
    except OSError as error:
        if error.errno in (
            errno.EPERM,
            errno.EACCES,
            errno.EAFNOSUPPORT,
            errno.EPROTONOSUPPORT,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
        ):
            raise unittest.SkipTest(
                f"Unix-domain socket creation/binding unavailable: {error}"
            ) from error
        raise
    finally:
        if listener is not None:
            listener.close()
        if bound:
            path.unlink()
