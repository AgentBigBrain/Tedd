"""Runtime narrowing helpers for dynamic v4 CLI and backend values."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import cast

from .v4_cli_contracts import V4CLIError


def argument(args: argparse.Namespace, name: str) -> object:
    """Return one argparse value as unknown boundary data."""

    values = cast(Mapping[str, object], vars(args))
    return values.get(name)


def optional_text(value: object) -> str | None:
    """Normalize an optional CLI token without accepting another data shape."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def trimmed_if_truthy(value: object) -> str | None:
    """Preserve the legacy ``str(value).strip() if value else None`` contract."""

    if not value:
        return None
    return str(value).strip()


def sequence_value(value: object, label: str) -> tuple[object, ...]:
    """Narrow an argparse append/nargs value to a stable sequence."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise V4CLIError(f"{label} must be a sequence")
    return tuple(cast(Sequence[object], value))


def string_sequence(value: object, label: str) -> tuple[str, ...]:
    return tuple(str(item) for item in sequence_value(value, label))


def object_mapping(
    value: object,
    label: str,
    *,
    none_as_empty: bool = False,
) -> dict[str, object]:
    """Narrow a backend mapping before command logic reads or mutates it."""

    if value is None and none_as_empty:
        return {}
    if not isinstance(value, Mapping):
        raise V4CLIError(f"v4 storage backend returned an invalid {label}")
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise V4CLIError(f"v4 storage backend returned an invalid {label}")
    return {cast(str, key): item for key, item in mapping.items()}


def object_sequence(value: object, label: str) -> list[object]:
    """Narrow a backend collection while excluding scalar text values."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise V4CLIError(f"v4 storage backend returned an invalid {label}")
    return list(cast(Sequence[object], value))


def integer_value(value: object, label: str) -> int:
    """Validate Python's runtime integer conversion at a dynamic boundary."""

    try:
        # The cast only satisfies the static overload; int() remains the runtime check.
        return int(cast(str, value))
    except (TypeError, ValueError) as error:
        raise V4CLIError(f"{label} must be an integer") from error
