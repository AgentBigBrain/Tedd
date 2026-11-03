"""Neutral chat parsing and shallow exact-byte message recipes, without I/O."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass

from .source_contracts import (
    CHAT_BYTES,
    MAX_UNITS,
    SourceArchiveError,
    SourceMetadata,
    SourceUnit,
    decode_link,
    digest,
    encode,
    object_fields,
    optional_text,
    parse_json,
    sha256,
    text,
)

# At most CHAT_BYTES outside message objects encoded in base64, plus one
# 64-character digest and delimiters per unit and one extra span. This is a
# storage recipe bound, not the independently configurable source manifest cap.
RECIPE_BYTES = 4 * ((CHAT_BYTES + 2) // 3) + 80 * (MAX_UNITS + 1) + 128


def decode_chat(
    payload: bytes, metadata: SourceMetadata, *, maximum_bytes: int = CHAT_BYTES
) -> tuple[SourceUnit, ...]:
    obj = object_fields(
        parse_json(payload, maximum_bytes=maximum_bytes),
        {"format", "source_id", "source_version", "coverage", "gaps", "delivery", "messages"},
    )
    if type(obj["format"]) is not int or obj["format"] != 1:
        raise SourceArchiveError()
    if obj["source_id"] != metadata.source_id or obj["source_version"] != metadata.source_version:
        raise SourceArchiveError()
    gaps, messages = obj["gaps"], obj["messages"]
    if not isinstance(gaps, list) or not isinstance(messages, list):
        raise SourceArchiveError()
    if (
        obj["coverage"] != metadata.coverage
        or tuple(text(gap) for gap in gaps) != metadata.gaps
        or obj["delivery"] != "sent"
    ):
        raise SourceArchiveError("capture_incomplete")
    if not 1 <= len(messages) <= MAX_UNITS:
        raise SourceArchiveError("limit_exceeded")
    units: list[SourceUnit] = []
    known_ids: set[str] = set()
    for position, value in enumerate(messages, start=1):
        item = object_fields(value, {"id", "speaker", "text", "time", "reply_to", "links"})
        links = item["links"]
        if not isinstance(links, list):
            raise SourceArchiveError()
        source_id = optional_text(item["id"])
        if source_id is not None:
            if source_id in known_ids:
                raise SourceArchiveError("capture_incomplete")
            known_ids.add(source_id)
        units.append(
            SourceUnit(
                f"u{position}",
                text(item["text"], maximum=128 * 1024, empty=True),
                position,
                source_id=source_id,
                speaker=optional_text(item["speaker"]),
                source_time=optional_text(item["time"]),
                reply_to=optional_text(item["reply_to"]),
                links=tuple(decode_link(link) for link in links),
            )
        )
    return tuple(units)


def _message_spans(payload: bytes) -> tuple[tuple[int, int], ...]:
    """Locate already-validated JSON values, not substrings that resemble keys.

    Decode and re-encode only to map character offsets to byte offsets. Preserved
    fragments always slice the acquired bytes, including any Unicode BOM.
    """
    encoding = json.detect_encoding(payload)
    decoded = payload.decode(encoding)
    if encoding == "utf-8-sig":
        encoding = "utf-8"
    elif encoding in ("utf-16", "utf-32"):
        encoding += "-le" if payload.startswith((b"\xff\xfe", b"\xff\xfe\x00\x00")) else "-be"
    byte_offset = len(payload) - len(decoded.encode(encoding))
    decoder = json.JSONDecoder()

    def skip(index: int) -> int:
        while index < len(decoded) and decoded[index] in " \t\r\n":
            index += 1
        return index

    index = skip(0) + 1  # The closed contract already proved a top-level object.
    spans: list[tuple[int, int]] = []
    while True:
        index = skip(index)
        key, index = decoder.raw_decode(decoded, index)
        index = skip(skip(index) + 1)  # colon
        if key == "messages":
            index = skip(index + 1)
            previous = 0
            while decoded[index] != "]":
                start = index
                _, end = decoder.raw_decode(decoded, start)
                byte_offset += len(decoded[previous:start].encode(encoding))
                size = len(decoded[start:end].encode(encoding))
                spans.append((byte_offset, byte_offset + size))
                byte_offset += size
                previous = end
                index = skip(end)
                if decoded[index] == ",":
                    index = skip(index + 1)
            return tuple(spans)
        _, index = decoder.raw_decode(decoded, index)
        index = skip(index) + 1  # comma; the messages field is known to exist.


@dataclass(frozen=True, repr=False)
class ChatRecipe:
    gaps: tuple[bytes, ...]
    messages: tuple[str, ...]

    def encoded(self) -> bytes:
        payload = encode(
            {
                "format": 1,
                "gaps": [base64.b64encode(gap).decode("ascii") for gap in self.gaps],
                "messages": self.messages,
            }
        )
        if len(payload) > RECIPE_BYTES:
            raise SourceArchiveError("limit_exceeded")
        return payload

    def original(self, read_message: Callable[[str], bytes], expected_bytes: int) -> bytes:
        fragments: list[bytes] = []
        used = sum(map(len, self.gaps))
        if used > expected_bytes:
            raise SourceArchiveError()
        for gap, fingerprint in zip(self.gaps, self.messages, strict=False):
            message = read_message(fingerprint)
            used += len(message)
            if used > expected_bytes or digest(message) != fingerprint:
                raise SourceArchiveError()
            fragments.extend((gap, message))
        fragments.append(self.gaps[-1])
        if used != expected_bytes:
            raise SourceArchiveError()
        return b"".join(fragments)

    @classmethod
    def decode(cls, payload: bytes) -> ChatRecipe:
        obj = object_fields(
            parse_json(payload, maximum_bytes=RECIPE_BYTES), {"format", "gaps", "messages"}
        )
        gaps, messages = obj["gaps"], obj["messages"]
        if (
            type(obj["format"]) is not int
            or obj["format"] != 1
            or not isinstance(gaps, list)
            or not isinstance(messages, list)
            or not 1 <= len(messages) <= MAX_UNITS
            or len(gaps) != len(messages) + 1
        ):
            raise SourceArchiveError()
        decoded: list[bytes] = []
        try:
            for gap in gaps:
                if not isinstance(gap, str):
                    raise SourceArchiveError()
                decoded.append(base64.b64decode(gap, validate=True))
        except (ValueError, binascii.Error) as error:
            raise SourceArchiveError() from error
        if sum(map(len, decoded)) > CHAT_BYTES:
            raise SourceArchiveError("limit_exceeded")
        return cls(tuple(decoded), tuple(sha256(message) for message in messages))


def message_recipe(
    payload: bytes, metadata: SourceMetadata, units: tuple[SourceUnit, ...]
) -> tuple[bytes, dict[str, bytes]] | None:
    if decode_chat(payload, metadata) != units:
        raise SourceArchiveError()
    if any(unit.source_id is None for unit in units):
        return None
    gaps: list[bytes] = []
    messages: list[str] = []
    objects: dict[str, bytes] = {}
    previous = 0
    for start, end in _message_spans(payload):
        gaps.append(payload[previous:start])
        message = payload[start:end]
        fingerprint = digest(message)
        messages.append(fingerprint)
        objects[fingerprint] = message
        previous = end
    gaps.append(payload[previous:])
    if len(messages) != len(units):
        raise SourceArchiveError()
    return ChatRecipe(tuple(gaps), tuple(messages)).encoded(), objects
