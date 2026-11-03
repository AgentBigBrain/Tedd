"""Filesystem-independent path privacy for structured values and review text.

Free text cannot unambiguously distinguish every path from a route or determine
where an unquoted filename with spaces ends. Explicit web URLs are preserved;
route labels do not override configured filesystem path patterns. Other
multi-component absolute paths are treated as local;
their unquoted remainder of the line is withheld conservatively. Producers should
prefer structured path fields or quote paths when adjacent prose must survive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath, PureWindowsPath

PATH_MARKER = "[redacted local path]"
_TOKEN = re.compile(
    r"(?P<remote>\b(?!file://)[a-z][a-z0-9+.-]+://[^\s<>\"'`]+)"
    r"|(?P<root>(?<![\w./:\\])(?:file://|[a-z]:[\\/]|\\\\|//|~/|/))",
    re.I,
)
_ROUTE_CONTEXT = re.compile(r"\b(?:route|endpoint|url|href)\s*[:=]?\s*[\"'`]?\Z", re.I)
_FILESYSTEM_CONTEXT = re.compile(
    r"\b(?:path|directory|folder|file|symlink|cwd|root)\s*[:=]\s*[\"'`]?\Z", re.I
)
_PATH_FIELDS = frozenset(
    {
        "path",
        "paths",
        "cwd",
        "directory",
        "directory_path",
        "file_path",
        "store_path",
        "canonical_path",
        "vault_path",
        "backup_path",
        "config_path",
        "profile_path",
    }
)
_ROUTE_FIELDS = frozenset({"url", "href", "route", "endpoint"})


def _field_key(value: object) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def path_field(value: object) -> bool:
    """Known filesystem fields are private even when their contents are opaque."""
    return _field_key(value) in _PATH_FIELDS


def route_field_value(key: object, value: object, patterns: Iterable[re.Pattern[str]]) -> bool:
    """Preserve explicit routes only when no configured filesystem policy matches."""
    return (
        _field_key(key) in _ROUTE_FIELDS
        and isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not any(character.isspace() for character in value)
        and not any(pattern.search(value) for pattern in patterns)
    )


def _local_path(value: str, *, quoted: bool) -> bool:
    candidate = value.strip()
    if candidate.lower().startswith("file://") or candidate.startswith("~/"):
        return True
    if PureWindowsPath(candidate).is_absolute():
        return True
    # A lone /support or /projects/?page=2 remains an ambiguous route in prose.
    if not quoted:
        candidate = re.split(r"\s+(?=[a-z][a-z0-9+.-]+://)", candidate, maxsplit=1, flags=re.I)[0]
    if not quoted and re.fullmatch(r"/[^/\s?#]+/\?[^/\s]*", candidate):
        return False
    path = PurePosixPath(candidate)
    return path.is_absolute() and (quoted or len(path.parts) > 2)


def _apply_patterns(value: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[str, int]:
    count = 0
    for pattern in patterns:
        value, added = pattern.subn(PATH_MARKER, value)
        count += added
    return value, count


def redact_local_paths(value: str, patterns: Iterable[re.Pattern[str]]) -> tuple[str, int]:
    """Redact complete path spans; apply legacy hooks only outside URLs/routes."""
    policy = tuple(patterns)
    pieces: list[str] = []
    cursor = 0
    count = 0
    while match := _TOKEN.search(value, cursor):
        start = match.start()
        text, added = _apply_patterns(value[cursor:start], policy)
        pieces.append(text)
        count += added
        if match.group("remote"):
            pieces.append(match.group())
            cursor = match.end()
            continue
        quote = value[start - 1] if start and value[start - 1] in "\"'`" else ""
        line_end = value.find("\n", match.end())
        end = len(value) if line_end < 0 else line_end
        if quote:
            quote_end = value.find(quote, match.end(), end)
            if quote_end >= 0:
                end = quote_end
        candidate = value[start:end].rstrip("\r")
        if (
            match.group() == "/"
            and _ROUTE_CONTEXT.search(value[max(0, start - 32) : start])
            and not any(pattern.search(candidate) for pattern in policy)
        ):
            # A labeled route has URL token boundaries, not filesystem spaces.
            token = re.match(r"[^\s\"'`<>]+", candidate)
            end = start + (len(token.group()) if token else len(candidate))
            pieces.append(value[start:end])
        elif _local_path(candidate, quoted=bool(quote)) or _FILESYSTEM_CONTEXT.search(
            value[max(0, start - 32) : start]
        ):
            pieces.append(PATH_MARKER)
            count += 1
        else:
            # Do not split the slash away from a caller-supplied pattern. Stop
            # before the next URL/root so those spans retain their own policy.
            next_token = _TOKEN.search(value, match.end(), end)
            if next_token is not None:
                end = next_token.start()
            text, added = _apply_patterns(value[start:end], policy)
            pieces.append(text)
            count += added
        cursor = end
    text, added = _apply_patterns(value[cursor:], policy)
    pieces.append(text)
    return "".join(pieces), count + added
