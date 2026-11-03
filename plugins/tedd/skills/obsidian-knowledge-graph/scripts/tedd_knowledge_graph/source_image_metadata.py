"""Standalone image metadata profile 1: bounded, inert, and content-free output.

This does not change Office metadata admission. Categories describe which
already-authorized classification is required; they never confer authority.
Exact originals stay unchanged. Unknown fields, maker blobs, thumbnails not
inventoried as images, and unreferenced non-padding bytes are refused.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .source_contracts import SourceArchiveError, text
from .source_documents import _screen
from .source_office_metadata import inspect_icc


def metadata_text(raw: bytes, *, encoding: str = "utf-8") -> None:
    if not 1 <= len(raw) <= 8192:
        raise SourceArchiveError("limit_exceeded")
    try:
        _screen(text(raw.decode(encoding, errors="strict"), maximum=8192, empty=True))
    except UnicodeError as error:
        raise SourceArchiveError() from error


def _inflate_metadata(payload: bytes, maximum: int) -> bytes:
    decoder = zlib.decompressobj()
    try:
        result = decoder.decompress(payload, maximum + 1)
    except zlib.error as error:
        raise SourceArchiveError() from error
    if len(result) > maximum or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise SourceArchiveError("limit_exceeded")
    return result


def inspect_png_metadata(tag: bytes, value: bytes) -> frozenset[str]:
    """Validate standalone PNG metadata; never add its values to a preview."""
    if len(value) > 262144:
        raise SourceArchiveError("limit_exceeded")
    if tag == b"eXIf":
        return inspect_exif(value)
    if tag == b"tIME":
        if len(value) != 7:
            raise SourceArchiveError()
        try:
            year, month, day, hour, minute, second = struct.unpack("!H5B", value)
            datetime(year, month, day, hour, minute, min(second, 59))
            if second > 60:
                raise SourceArchiveError()
        except ValueError as error:
            raise SourceArchiveError() from error
        return frozenset({"description"})
    keyword, separator, content = value.partition(b"\0")
    if not separator or not 1 <= len(keyword) <= 79:
        raise SourceArchiveError()
    metadata_text(keyword, encoding="latin-1")
    if tag == b"iCCP":
        if not content or content[0] != 0:
            raise SourceArchiveError()
        inspect_icc(_inflate_metadata(content[1:], 262144))
        return frozenset()
    if keyword not in (
        b"Title",
        b"Description",
        b"Copyright",
        b"Author",
        b"Software",
        b"Creation Time",
    ):
        raise SourceArchiveError("privacy_rejected")
    encoding = "latin-1"
    if tag == b"zTXt":
        if not content or content[0] != 0:
            raise SourceArchiveError()
        content = _inflate_metadata(content[1:], 8192)
    elif tag == b"iTXt":
        if len(content) < 4 or content[0] not in (0, 1) or content[1] != 0:
            raise SourceArchiveError()
        compressed = content[0] == 1
        language, separator, remaining = content[2:].partition(b"\0")
        translated, translated_separator, content = remaining.partition(b"\0")
        if (
            not separator
            or not translated_separator
            or not re.fullmatch(rb"[A-Za-z0-9-]{0,63}", language)
        ):
            raise SourceArchiveError()
        if translated:
            metadata_text(translated)
        if compressed:
            content = _inflate_metadata(content, 8192)
        encoding = "utf-8"
    elif tag != b"tEXt":
        raise SourceArchiveError("capability_unavailable")
    metadata_text(content, encoding=encoding)
    return frozenset({"identifying" if keyword in (b"Author", b"Software") else "description"})


def inspect_exif_field(
    group: str, tag: int, kind: int, count: int, raw: bytes, order: str
) -> set[str]:
    if group == "gps":
        if tag == 0 and kind == 1 and count == 4 and raw in (b"\2\2\0\0", b"\2\3\0\0"):
            return {"location"}
        if tag in (1, 3, 9, 10, 12, 14, 16, 23, 25) and kind == 2:
            allowed = {
                1: b"NS",
                3: b"EW",
                9: b"AV",
                10: b"23",
                12: b"KMN",
                14: b"TM",
                16: b"TM",
                23: b"TM",
                25: b"KMN",
            }
            if count != 2 or raw[1:] != b"\0" or raw[0] not in allowed[tag]:
                raise SourceArchiveError()
        elif tag in (2, 4, 7, 20, 22) and kind == 5 and count == 3:
            values = _rationals(raw, order, signed=False)
            maximum = 24 if tag == 7 else (90 if tag in (2, 20) else 180)
            if not (0 <= values[0] <= maximum and all(0 <= value < 60 for value in values[1:])):
                raise SourceArchiveError()
            if values[0] == maximum and (tag == 7 or any(values[1:])):
                raise SourceArchiveError()
        elif tag == 5 and kind == 1 and count == 1 and raw in (b"\0", b"\1"):
            pass
        elif tag in (6, 11, 13, 15, 17, 24, 26, 31) and kind == 5 and count == 1:
            number = _rationals(raw, order, signed=False)[0]
            if not 0 <= number <= 1e9 or tag in (15, 17, 24) and number >= 360:
                raise SourceArchiveError()
        elif tag == 29 and kind == 2 and count == 11:
            datetime.strptime(raw[:-1].decode("ascii"), "%Y:%m:%d")
            if raw[-1:] != b"\0":
                raise SourceArchiveError()
        else:
            raise SourceArchiveError("privacy_rejected")
        return {"location"}

    main = {
        0x0100,
        0x0101,
        0x010E,
        0x010F,
        0x0110,
        0x0112,
        0x011A,
        0x011B,
        0x0128,
        0x0131,
        0x0132,
        0x013B,
        0x013C,
        0x8298,
    }
    if group not in ("main", "exif") or (group == "main") != (tag in main):
        raise SourceArchiveError("privacy_rejected")
    descriptive = {0x010E, 0x8298}
    identifying = {
        0x010F,
        0x0110,
        0x0131,
        0x013B,
        0x013C,
        0xA420,
        0xA430,
        0xA431,
        0xA433,
        0xA434,
        0xA435,
    }
    dates = {0x0132, 0x9003, 0x9004}
    if tag == 0x8298:
        # Exif Copyright permits photographer\0editor\0, including a single
        # space for an omitted photographer. Other ASCII fields stay singular.
        if kind != 2 or count < 2 or raw[-1:] != b"\0":
            raise SourceArchiveError()
        components = raw[:-1].split(b"\0")
        if not 1 <= len(components) <= 2 or any(not value for value in components):
            raise SourceArchiveError()
        for value in components:
            metadata_text(value, encoding="ascii")
        return {"description"}
    if tag in descriptive | identifying | dates:
        if kind != 2 or count < 2 or raw[-1:] != b"\0" or b"\0" in raw[:-1]:
            raise SourceArchiveError()
        metadata_text(raw[:-1], encoding="ascii")
        if tag in dates:
            if count != 20 or not re.fullmatch(rb"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}\0", raw):
                raise SourceArchiveError()
            datetime.strptime(raw[:-1].decode("ascii"), "%Y:%m:%d %H:%M:%S")
        if tag == 0xA420 and not re.fullmatch(rb"[A-Fa-f0-9]{32}\0", raw):
            raise SourceArchiveError()
        return {"description" if tag in descriptive | dates else "identifying"}
    if tag == 0x9286:
        if kind != 7 or not raw.startswith(b"ASCII\0\0\0"):
            raise SourceArchiveError("privacy_rejected")
        metadata_text(raw[8:].rstrip(b"\0"), encoding="ascii")
        return {"description"}
    if tag in (0x9000, 0xA000):
        versions = (
            (b"0100", b"0101")
            if tag == 0xA000
            else (b"0100", b"0200", b"0210", b"0220", b"0230", b"0231", b"0232", b"0300")
        )
        if kind != 7 or count != 4 or raw not in versions:
            raise SourceArchiveError()
        return set()
    if tag == 0x9101:
        if kind != 7 or count != 4 or raw not in (b"\1\2\3\0", b"\4\5\6\0"):
            raise SourceArchiveError()
        return set()
    if tag in (0xA300, 0xA301):
        if kind != 7 or raw != (b"\3" if tag == 0xA300 else b"\1"):
            raise SourceArchiveError()
        return set()
    # TIFF/Exif types and counts are field contracts—not interchangeable ways of
    # storing numbers. A malformed numeric field must not hide text-shaped bytes.
    numeric: dict[int, tuple[tuple[int, ...], int]] = {
        **{field: ((3, 4), 1) for field in (0x0100, 0x0101, 0xA002, 0xA003)},
        **{
            field: ((3,), 1)
            for field in (
                0x0112,
                0x0128,
                0x8822,
                0x8827,
                0x8830,
                0x9207,
                0x9208,
                0x9209,
                0xA001,
                0xA210,
                0xA217,
                0xA401,
                0xA402,
                0xA403,
                0xA405,
                0xA406,
                0xA407,
                0xA408,
                0xA409,
                0xA40A,
                0xA40C,
            )
        },
        **{field: ((4,), 1) for field in (0x8831, 0x8832, 0x8833)},
        **{
            field: ((5,), 1)
            for field in (
                0x011A,
                0x011B,
                0x829A,
                0x829D,
                0x9102,
                0x9202,
                0x9205,
                0x9206,
                0x920A,
                0xA20E,
                0xA20F,
                0xA404,
            )
        },
        **{field: ((10,), 1) for field in (0x9201, 0x9203, 0x9204)},
        0xA432: ((5,), 4),
    }
    contract = numeric.get(tag)
    if contract is None:
        raise SourceArchiveError("privacy_rejected")
    if kind not in contract[0] or count != contract[1]:
        raise SourceArchiveError()
    if kind in (5, 10):
        values = _rationals(raw, order, signed=kind == 10)
    else:
        code = "H" if kind == 3 else "I"
        values = tuple(float(value[0]) for value in struct.iter_unpack(order + code, raw))
    if any(abs(value) > 1e12 for value in values):
        raise SourceArchiveError()
    if tag == 0x0112 and (kind != 3 or count != 1 or values[0] not in range(1, 9)):
        raise SourceArchiveError()
    if tag == 0x0128 and (count != 1 or values[0] not in (1, 2, 3)):
        raise SourceArchiveError()
    if tag in (0x0100, 0x0101, 0xA002, 0xA003) and not 0 < values[0] <= 131072:
        raise SourceArchiveError()
    if tag in (0x011A, 0x011B, 0xA20E, 0xA20F) and values[0] <= 0:
        raise SourceArchiveError()
    enumerated = {
        0x8822: tuple(range(9)),
        0x8830: tuple(range(8)),
        0x9207: (*range(7), 255),
        0x9208: (0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23, 24, 255),
        0xA001: (1, 2, 65535),
        0xA210: (2, 3),
        0xA217: (1, 2, 3, 4, 5, 7, 8),
        0xA401: (0, 1),
        0xA402: (0, 1, 2),
        0xA403: (0, 1),
        0xA406: (0, 1, 2, 3),
        0xA407: (0, 1, 2, 3, 4),
        0xA408: (0, 1, 2),
        0xA409: (0, 1, 2),
        0xA40A: (0, 1, 2),
        0xA40C: (0, 1, 2, 3),
    }
    if tag in enumerated and values[0] not in enumerated[tag] or tag == 0x9209 and values[0] > 127:
        raise SourceArchiveError()
    return set()


def _rationals(raw: bytes, order: str, *, signed: bool) -> tuple[float, ...]:
    values: list[float] = []
    for numerator, denominator in struct.iter_unpack(order + ("ii" if signed else "II"), raw):
        if denominator <= 0:
            raise SourceArchiveError()
        values.append(numerator / denominator)
    return tuple(values)


@dataclass(frozen=True, repr=False)
class TiffField:
    tag: int
    kind: int
    count: int
    value: bytes


def read_tiff_directory(
    raw: bytes, offset: int, order: str, claim: Callable[[int, int], bytes]
) -> tuple[tuple[TiffField, ...], int]:
    """One typed bounded IFD reader shared by EXIF metadata and TIFF page admission.

    The caller owns span accounting and whether a next-directory pointer is
    legal. This parser does not infer image or metadata semantics from numbers.
    """
    if order not in ("<", ">") or offset < 8 or offset + 2 > len(raw):
        raise SourceArchiveError()
    count = int(struct.unpack_from(order + "H", raw, offset)[0])
    if count > 128:
        raise SourceArchiveError("limit_exceeded")
    table = claim(offset, 2 + count * 12 + 4)
    fields: list[TiffField] = []
    seen: set[int] = set()
    for index in range(count):
        entry = table[2 + index * 12 : 14 + index * 12]
        tag, kind, length = struct.unpack_from(order + "HHI", entry)
        if tag in seen or kind not in (1, 2, 3, 4, 5, 7, 9, 10) or not 1 <= length <= 8192:
            raise SourceArchiveError()
        seen.add(tag)
        size = length * {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}[kind]
        if size <= 4:
            if any(entry[8 + size :]):
                raise SourceArchiveError()
            value = entry[8 : 8 + size]
        else:
            start = int(struct.unpack_from(order + "I", entry, 8)[0])
            value = claim(start, size)
        fields.append(TiffField(tag, kind, length, value))
    return tuple(fields), int.from_bytes(table[-4:], "little" if order == "<" else "big")


def inspect_exif(payload: bytes) -> frozenset[str]:
    """Inspect a finite classic TIFF EXIF tree without trusting a lossy tag summary.

    All entries are typed and allowlisted. IFD links are bounded and disjoint;
    unknown payload is not hidden by a decoder dropping an unfamiliar tag.
    """
    if not isinstance(payload, bytes) or not 14 <= len(payload) <= 65536:
        raise SourceArchiveError("limit_exceeded")
    raw = payload[6:] if payload.startswith(b"Exif\0\0") else payload
    if raw[:4] not in (b"II*\0", b"MM\0*"):
        raise SourceArchiveError()
    order = "<" if raw[:2] == b"II" else ">"
    used = bytearray(len(raw))
    used[:8] = b"\1" * 8
    spans: set[tuple[int, int]] = set()
    categories: set[str] = set()
    pending = [(int.from_bytes(raw[4:8], "little" if order == "<" else "big"), "main", 0)]
    visited: set[int] = set()
    entries = 0

    def claim(start: int, size: int) -> bytes:
        end = start + size
        if start < 8 or size < 1 or end > len(raw) or any(used[start:end]):
            raise SourceArchiveError()
        used[start:end] = b"\1" * size
        spans.add((start, end))
        return raw[start:end]

    try:
        while pending:
            offset, group, depth = pending.pop()
            if offset in visited or depth > 2 or offset < 8 or offset + 2 > len(raw):
                raise SourceArchiveError()
            visited.add(offset)
            fields, successor = read_tiff_directory(raw, offset, order, claim)
            entries += len(fields)
            if entries > 128:
                raise SourceArchiveError("limit_exceeded")
            if successor:
                raise SourceArchiveError("capability_unavailable")
            for field in fields:
                tag, kind, length, value = field.tag, field.kind, field.count, field.value
                if tag in (0x8769, 0x8825):
                    if group != "main" or kind != 4 or length != 1:
                        raise SourceArchiveError()
                    pending.append(
                        (
                            int(struct.unpack(order + "I", value)[0]),
                            "exif" if tag == 0x8769 else "gps",
                            depth + 1,
                        )
                    )
                else:
                    categories.update(inspect_exif_field(group, tag, kind, length, value, order))
        cursor = 8
        for start, end in sorted(spans):
            if start - cursor > 3 or any(raw[cursor:start]):
                raise SourceArchiveError("privacy_rejected")
            cursor = end
        if len(raw) - cursor > 3 or any(raw[cursor:]):
            raise SourceArchiveError("privacy_rejected")
    except (struct.error, UnicodeError, ValueError, OverflowError) as error:
        if isinstance(error, SourceArchiveError):
            raise
        raise SourceArchiveError() from error
    return frozenset(categories)
