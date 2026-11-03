"""Dependency-free contracts shared by the legacy and v4 graph adapters."""

from __future__ import annotations

import json

NETWORK_FS_TYPES = frozenset(
    {
        "9p",
        "afpfs",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "fuseblk",
        "gcsfuse",
        "nfs",
        "nfs4",
        "s3fs",
        "smbfs",
        "sshfs",
        "webdav",
        "windows-remote",
    }
)
CLOUD_PATH_MARKERS = (
    "/library/mobile documents/",
    "/library/cloudstorage/",
    "/icloud drive/",
    "/dropbox/",
    "/google drive/",
    "/onedrive/",
    "/box sync/",
    "/syncthing/",
)


def canonical_json(value: object) -> str:
    """The existing stable graph serialization, shared without a facade import."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


DISCOVERY_KINDS: tuple[str, ...] = (
    "diagnosis",
    "decision",
    "verification",
    "release",
    "support_outcome",
    "stakeholder_outcome",
    "identity_assertion",
    "identity_resolution",
    "open_loop",
    "loop_resolution",
    "correction",
    "reusable_lesson",
    "milestone",
    "substantial_analysis",
    "migration",
    "redaction",
)


def validate_discovery_kind(value: object) -> str:
    """Return a controlled discovery kind or retain the legacy error contract."""

    if not isinstance(value, str) or value not in DISCOVERY_KINDS:
        raise ValueError(f"Unsupported discovery kind: {value}")
    return value
