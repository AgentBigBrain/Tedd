"""Typed privacy and redaction policy for legacy graph values.

The flat ``knowledge_graph`` compatibility facade supplies policy patterns and
callbacks at call time.  This leaf retains no facade dependency or mutable
policy state, so facade monkeypatch hooks remain observable after extraction.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from .review_paths import PATH_MARKER, path_field, redact_local_paths, route_field_value


class RedactText(Protocol):
    def __call__(
        self,
        value: str,
        *,
        redact_paths: bool = True,
        max_length: int = 4000,
    ) -> tuple[str, int]: ...


@dataclass(frozen=True)
class PrivacyDependencies:
    """Call-time privacy policy supplied by the compatibility facade."""

    secret_patterns: Iterable[re.Pattern[str]]
    path_patterns: Iterable[re.Pattern[str]]
    redact_uri: Callable[[str], str]
    sensitive_field: Callable[[object], bool]
    redact_text_override: RedactText | None = None


def redact_text(
    value: str,
    dependencies: PrivacyDependencies,
    *,
    redact_paths: bool = True,
    max_length: int = 4000,
) -> tuple[str, int]:
    text = value
    redactions = 0
    for pattern in dependencies.secret_patterns:
        text, count = pattern.subn("[redacted secret]", text)
        redactions += count
    if redact_paths:
        text, count = redact_local_paths(text, dependencies.path_patterns)
        redactions += count
    redacted_uri = dependencies.redact_uri(text)
    if redacted_uri != text:
        redactions += 1
        text = redacted_uri
    if len(text) > max_length:
        text = text[: max_length - 18].rstrip() + " … [truncated]"
    return text, redactions


def scrub_value(
    value: object,
    dependencies: PrivacyDependencies,
    *,
    redact_paths: bool = True,
    depth: int = 0,
) -> tuple[object, int]:
    if depth > 20:
        raise ValueError("JSON nesting exceeds the 20-level safety limit")
    if isinstance(value, str):
        if dependencies.redact_text_override is not None:
            return dependencies.redact_text_override(value, redact_paths=redact_paths)
        return redact_text(value, dependencies, redact_paths=redact_paths)
    if isinstance(value, list):
        items = cast(Sequence[object], value)
        if len(items) > 500:
            raise ValueError("JSON list exceeds the 500-item safety limit")
        list_result: list[object] = []
        total = 0
        for item in items:
            cleaned, count = scrub_value(
                item,
                dependencies,
                redact_paths=redact_paths,
                depth=depth + 1,
            )
            list_result.append(cleaned)
            total += count
        return list_result, total
    if isinstance(value, dict):
        values = cast(Mapping[object, object], value)
        if len(values) > 300:
            raise ValueError("JSON object exceeds the 300-field safety limit")
        dict_result: dict[str, object] = {}
        total = 0
        for key, item in values.items():
            if dependencies.redact_text_override is not None:
                cleaned_key, key_count = dependencies.redact_text_override(
                    str(key),
                    redact_paths=redact_paths,
                    max_length=100,
                )
            else:
                cleaned_key, key_count = redact_text(
                    str(key),
                    dependencies,
                    redact_paths=redact_paths,
                    max_length=100,
                )
            if dependencies.sensitive_field(key) and item not in (
                None,
                "",
                "[redacted secret]",
            ):
                cleaned, item_count = "[redacted secret]", 1
            elif redact_paths and path_field(key) and item not in (None, "", PATH_MARKER):
                cleaned, item_count = PATH_MARKER, 1
            else:
                cleaned, item_count = scrub_value(
                    item,
                    dependencies,
                    redact_paths=redact_paths
                    and not route_field_value(key, item, dependencies.path_patterns),
                    depth=depth + 1,
                )
            dict_result[cleaned_key] = cleaned
            total += key_count + item_count
        return dict_result, total
    return value, 0


__all__ = ["PrivacyDependencies", "redact_text", "scrub_value"]
