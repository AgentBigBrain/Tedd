"""Deterministic value normalization and review-safe redaction for legacy graphs.

This leaf owns no graph state or I/O beyond reading the supplied clock.  The flat
``knowledge_graph`` module retains compatibility wrappers and supplies its clock
at call time so existing monkeypatch and embedding contracts remain intact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .review_paths import PATH_MARKER, path_field, redact_local_paths, route_field_value

SAFE_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SENSITIVE_FIELD_KEYS = {
    "account_key",
    "access_token",
    "access_key",
    "access_key_id",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "pgpassword",
    "pwd",
    "refresh_token",
    "rediscli_auth",
    "secret",
    "secret_access_key",
    "secret_key",
    "session",
    "session_id",
    "set_cookie",
    "signature",
    "token",
}
SECRET_PATTERNS = [
    re.compile(
        r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?-----END \1-----",
        re.I | re.S,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:[A-Z0-9]+_)*(?:PASSWORD|PASSWD|PWD|PASSPHRASE|CREDENTIALS?|API_KEY|PRIVATE_KEY|SECRET_ACCESS_KEY|SECRET_KEY(?:_BASE)?|ACCESS_KEY(?:_ID)?|ACCOUNT_KEY|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|PGPASSWORD|REDISCLI_AUTH|TOKEN|SECRET)\s*=\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,;]+)",
        re.I,
    ),
    re.compile(
        r"\b(?:password|passwd|pwd|passphrase|credentials?|api[ _-]?key|private[ _-]?key|(?:aws[ _-]?)?secret[ _-]?access[ _-]?key|secret[ _-]?key|access[ _-]?key(?:[ _-]?id)?|account[ _-]?key|access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|authorization|auth|cookie|set[ _-]?cookie|session(?:[ _-]?id)?|token|secret)\b[\"']?\s*(?::|=|\bis\b)\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,;]+)",
        re.I,
    ),
    re.compile(r"\bauthorization\s*:\s*(?:bearer|basic)\s+[^\s]+", re.I),
    re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}\b", re.I),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.I),
    re.compile(
        r"([?&](?:access_token|api_key|apikey|auth|authorization|client_secret|key|password|secret|sig|signature|token|x-amz-credential|x-amz-security-token|x-amz-signature|x-goog-credential|x-goog-security-token|x-goog-signature)=)(?!%5Bredacted%5D|\[redacted(?:%20| )?(?:secret)?\])[^&#\s]+",
        re.I,
    ),
]
PATH_PATTERNS = [
    re.compile(r"\bfile://[^\r\n\s?#]*", re.I),
    re.compile(r"\b[A-Za-z]:[\\/][^\r\n\s]*", re.I),
    re.compile(r"(?<![A-Za-z0-9])\\\\[^\\\s]+\\[^\\\s]+(?:\\[^\r\n\s]+)*"),
    re.compile(r"(?<![:/])//[^/\s]+/[^/\s]+(?:/[^\r\n\s]+)*"),
    re.compile(r"/Users/[^/\s]+/[^\r\n\s]*", re.I),
    re.compile(r"/home/[^/\s]+/[^\r\n\s]*", re.I),
    re.compile(r"/root/[^\r\n\s]*", re.I),
    re.compile(r"/Volumes/[^/\s]+/[^\r\n\s]*", re.I),
    re.compile(
        r"/(?:Applications|Library|data|etc|mnt|opt|private|srv|tmp|usr|var|workspace)/[^\r\n\s]*",
        re.I,
    ),
    re.compile(r"/var/folders/[^\r\n\s]*"),
]
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "access_key",
    "access_key_id",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "key",
    "password",
    "secret",
    "secret_access_key",
    "secret_key",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
    "x-goog-credential",
    "x-goog-security-token",
    "x-goog-signature",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value: object) -> str:
    return unicodedata.normalize("NFC", normalize_space(value)).casefold()


def normalize_field_key(value: object) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def explicit_record_id(value: object) -> str:
    record_id = normalize_space(value)
    if record_id and not SAFE_RECORD_ID.fullmatch(record_id):
        raise ValueError(
            "Explicit record IDs must be 1-128 ASCII letters, digits, dots, "
            "underscores, colons, or hyphens"
        )
    return record_id


def sensitive_field(value: object) -> bool:
    field = normalize_field_key(value)
    if field in SENSITIVE_FIELD_KEYS:
        return True
    return any(
        field.endswith(f"_{suffix}")
        for suffix in (
            "access_key",
            "access_key_id",
            "account_key",
            "api_key",
            "authorization",
            "cookie",
            "credential",
            "credentials",
            "passphrase",
            "password",
            "private_key",
            "secret",
            "secret_access_key",
            "secret_key",
            "session",
            "session_id",
            "signature",
            "token",
        )
    )


def parse_time(
    value: object,
    fallback: str | None = None,
    *,
    clock: Callable[[], str] = utc_now,
) -> str:
    text = normalize_space(value)
    if not text:
        text = fallback or clock()
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"Invalid ISO 8601 timestamp: {text}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def time_value(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def latest_time(left: str | None, right: str) -> str:
    if not left:
        return right
    return right if time_value(right) > time_value(left) else left


def earliest_time(left: str | None, right: str) -> str:
    if not left:
        return right
    return right if time_value(right) < time_value(left) else left


def stable_hash(value: object, length: int = 20) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def unique_strings(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_space(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def redact_uri(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{hostname}{port}"
    query = [
        (key, "[redacted]" if key.lower() in SENSITIVE_QUERY_KEYS else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))


def redact_text(
    value: str,
    *,
    redact_paths: bool = True,
    max_length: int = 4000,
) -> tuple[str, int]:
    text = value
    redactions = 0
    for pattern in SECRET_PATTERNS:
        text, count = pattern.subn("[redacted secret]", text)
        redactions += count
    if redact_paths:
        text, count = redact_local_paths(text, PATH_PATTERNS)
        redactions += count
    redacted_uri = redact_uri(text)
    if redacted_uri != text:
        redactions += 1
        text = redacted_uri
    if len(text) > max_length:
        text = text[: max_length - 18].rstrip() + " … [truncated]"
    return text, redactions


def scrub_value(
    value: object,
    *,
    redact_paths: bool = True,
    depth: int = 0,
) -> tuple[object, int]:
    if depth > 20:
        raise ValueError("JSON nesting exceeds the 20-level safety limit")
    if isinstance(value, str):
        return redact_text(value, redact_paths=redact_paths)
    if isinstance(value, list):
        items = cast(Sequence[object], value)
        if len(items) > 500:
            raise ValueError("JSON list exceeds the 500-item safety limit")
        list_result: list[object] = []
        total = 0
        for item in items:
            cleaned, count = scrub_value(item, redact_paths=redact_paths, depth=depth + 1)
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
            cleaned_key, key_count = redact_text(
                str(key),
                redact_paths=redact_paths,
                max_length=100,
            )
            if sensitive_field(key) and item not in (None, "", "[redacted secret]"):
                cleaned, item_count = "[redacted secret]", 1
            elif redact_paths and path_field(key) and item not in (None, "", PATH_MARKER):
                cleaned, item_count = PATH_MARKER, 1
            else:
                cleaned, item_count = scrub_value(
                    item,
                    redact_paths=redact_paths and not route_field_value(key, item, PATH_PATTERNS),
                    depth=depth + 1,
                )
            dict_result[cleaned_key] = cleaned
            total += key_count + item_count
        return dict_result, total
    return value, 0


def secret_hits(value: object) -> list[str]:
    serialized = json.dumps(value, ensure_ascii=False)
    hits = [
        f"secret_pattern_{index}"
        for index, pattern in enumerate(SECRET_PATTERNS, start=1)
        if pattern.search(serialized)
    ]
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in cast(Mapping[object, object], item).items():
                normalized_field = normalize_field_key(key)
                if sensitive_field(key) and child not in (
                    None,
                    "",
                    "[redacted secret]",
                    "[redacted]",
                ):
                    hits.append(f"sensitive_field:{normalized_field}")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(cast(Sequence[object], item))
    return sorted(set(hits))
