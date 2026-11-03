"""Finite classic TIFF page admission, sharing typed IFD and metadata checks.

Every next-IFD page and referenced strip/tile is accounted for before decoding.
Unknown tags, private blobs, SubIFD trees, and unsupported compression remain
explicit refusals. This is not a general TIFF metadata library or a decoder.
"""

from __future__ import annotations

import struct
import zlib

from .source_contracts import SourceArchiveError
from .source_image_metadata import TiffField, inspect_exif_field, metadata_text, read_tiff_directory
from .source_images import ImageUnit
from .source_office_metadata import inspect_icc


def _lzw_size(payload: bytes, expected: int) -> None:
    """Validate TIFF LZW code/end boundaries without allocating decoded strings."""
    cursor, width, previous, count = 0, 9, 0, 0
    lengths: list[int] = []
    while cursor + width <= len(payload) * 8:
        start = cursor // 8
        word = payload[start : (cursor + width + 7) // 8]
        code = (int.from_bytes(word, "big") >> (len(word) * 8 - cursor % 8 - width)) & (
            (1 << width) - 1
        )
        cursor += width
        if code == 256:
            lengths, width, previous = [1] * 256 + [0, 0], 9, 0
            continue
        if not lengths:
            raise SourceArchiveError()
        if code == 257:
            if count != expected or (cursor + 7) // 8 != len(payload):
                raise SourceArchiveError()
            return
        if code < len(lengths):
            length = lengths[code]
        elif code == len(lengths) and previous:
            length = previous + 1
        else:
            raise SourceArchiveError()
        count += length
        if count > expected:
            raise SourceArchiveError()
        if previous:
            if len(lengths) >= 4096:
                raise SourceArchiveError()
            lengths.append(previous + 1)
            if len(lengths) == (1 << width) - 1 and width < 12:
                width += 1
        previous = length
    raise SourceArchiveError()


def _strip(payload: bytes, compression: int, row_bytes: int, rows: int) -> None:
    expected = row_bytes * rows
    if compression == 1:
        if len(payload) != expected:
            raise SourceArchiveError()
    elif compression == 5:
        _lzw_size(payload, expected)
    elif compression in (8, 32946):
        decoder = zlib.decompressobj()
        try:
            decoded = decoder.decompress(payload, expected + 1)
        except zlib.error as error:
            raise SourceArchiveError() from error
        if (
            len(decoded) != expected
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise SourceArchiveError()
    elif compression == 32773:
        cursor, count = 0, 0
        while cursor < len(payload):
            control = payload[cursor]
            cursor += 1
            if control == 128:
                continue
            size = control + 1 if control < 128 else 257 - control
            cursor += size if control < 128 else 1
            if size > row_bytes - count % row_bytes:
                raise SourceArchiveError()
            count += size
            if cursor > len(payload) or count > expected:
                raise SourceArchiveError()
        if count != expected:
            raise SourceArchiveError()
    else:
        raise SourceArchiveError("capability_unavailable")


def inspect_tiff(payload: bytes) -> tuple[tuple[ImageUnit, ...], frozenset[str]]:
    """Admit bounded gray/RGB/palette pages without trusting decoder tag omission."""
    if not 14 <= len(payload) <= 64 * 1024**2 or payload[:4] not in (b"II*\0", b"MM\0*"):
        raise SourceArchiveError()
    order = "<" if payload[:2] == b"II" else ">"
    used = bytearray(len(payload))
    used[:8] = b"\1" * 8
    spans: set[tuple[int, int]] = set()
    visited: set[int] = set()
    units: list[ImageUnit] = []
    categories: set[str] = set()
    metadata_bytes, decoded_pixels = 0, 0

    def claim(start: int, size: int) -> bytes:
        end = start + size
        if start < 8 or size < 1 or end > len(payload):
            raise SourceArchiveError()
        if (start, end) not in spans:
            if any(used[start:end]):
                raise SourceArchiveError()
            spans.add((start, end))
            used[start:end] = b"\1" * size
        return payload[start:end]

    offset = int.from_bytes(payload[4:8], "little" if order == "<" else "big")
    try:
        while offset:
            if offset in visited or len(units) >= 256:
                raise SourceArchiveError("limit_exceeded")
            visited.add(offset)
            fields, offset = read_tiff_directory(payload, offset, order, claim)
            table = {field.tag: field for field in fields}

            def integers(
                tag: int,
                kinds: tuple[int, ...],
                default: tuple[int, ...] | None = None,
                *,
                _table: dict[int, TiffField] = table,
            ) -> tuple[int, ...]:
                field = _table.get(tag)
                if field is None:
                    if default is None:
                        raise SourceArchiveError()
                    return default
                if field.kind not in kinds:
                    raise SourceArchiveError()
                return tuple(
                    int(value[0])
                    for value in struct.iter_unpack(
                        order + ("H" if field.kind == 3 else "I"), field.value
                    )
                )

            def number(tag: int, kinds: tuple[int, ...], default: int | None = None) -> int:
                values = integers(tag, kinds, None if default is None else (default,))
                if len(values) != 1:
                    raise SourceArchiveError()
                return values[0]

            width, height = number(256, (3, 4)), number(257, (3, 4))
            orientation = number(274, (3,), 1)
            unit = ImageUnit("page", len(units), (width, height), orientation)
            units.append(unit)
            compression = number(259, (3,), 1)
            photo = number(262, (3,))
            samples = number(277, (3,), 1)
            bits = integers(258, (3,), (1,) * samples)
            extras = integers(338, (3,), ())
            if (
                photo not in (0, 1, 2, 3)
                or samples not in (1, 3, 4)
                or len(bits) != samples
                or len(set(bits)) != 1
                or bits[0] not in (1, 2, 4, 8, 16)
                or photo == 2
                and (samples not in (3, 4) or bits[0] not in (8, 16))
                or photo != 2
                and samples != 1
                or samples == 4
                and extras not in ((1,), (2,))
                or samples != 4
                and extras
                or number(284, (3,), 1) != 1
                or number(266, (3,), 1) != 1
                or integers(339, (3,), (1,) * samples) != (1,) * samples
                or number(254, (4,), 0) not in (0, 1, 2, 3)
                or number(317, (3,), 1) not in ((1, 2) if compression in (5, 8, 32946) else (1,))
            ):
                raise SourceArchiveError("capability_unavailable")
            if photo == 3:
                if len(integers(320, (3,))) != 3 * 2 ** bits[0]:
                    raise SourceArchiveError()
            elif 320 in table:
                raise SourceArchiveError()

            tiled = any(tag in table for tag in (322, 323, 324, 325))
            if tiled:
                if any(tag in table for tag in (273, 278, 279)):
                    raise SourceArchiveError()
                tile_width, tile_height = number(322, (3, 4)), number(323, (3, 4))
                if (
                    not 0 < tile_width <= 131072
                    or not 0 < tile_height <= 131072
                    or tile_width % 16
                    or tile_height % 16
                ):
                    raise SourceArchiveError()
                count = ((width + tile_width - 1) // tile_width) * (
                    (height + tile_height - 1) // tile_height
                )
                if count > 8192:
                    raise SourceArchiveError("limit_exceeded")
                row_bytes = (tile_width * sum(bits) + 7) // 8
                row_counts = (tile_height,) * count
                offsets, sizes = integers(324, (4,)), integers(325, (3, 4))
                decoded_pixels += tile_width * tile_height * count
            else:
                rows = number(278, (3, 4), height)
                if not rows:
                    raise SourceArchiveError()
                count = (height + rows - 1) // rows
                if count > 8192:
                    raise SourceArchiveError("limit_exceeded")
                row_bytes = (width * sum(bits) + 7) // 8
                row_counts = tuple(min(rows, height - start) for start in range(0, height, rows))
                offsets, sizes = integers(273, (3, 4)), integers(279, (3, 4))
                decoded_pixels += width * height
            if count > 8192 or decoded_pixels > 256_000_000:
                raise SourceArchiveError("limit_exceeded")
            if len(offsets) != count or len(sizes) != count:
                raise SourceArchiveError()
            for start, size, rows in zip(offsets, sizes, row_counts, strict=True):
                _strip(claim(start, size), compression, row_bytes, rows)

            pixel_tags = {
                254,
                256,
                257,
                258,
                259,
                262,
                266,
                273,
                274,
                277,
                278,
                279,
                284,
                317,
                320,
                322,
                323,
                324,
                325,
                338,
                339,
            }
            pending: list[tuple[int, str]] = []
            for field in fields:
                if field.tag in pixel_tags:
                    continue
                metadata_bytes += len(field.value)
                if field.tag in (0x8769, 0x8825):
                    pending.append(
                        (number(field.tag, (4,)), "exif" if field.tag == 0x8769 else "gps")
                    )
                elif field.tag == 34675:
                    if field.kind != 7:
                        raise SourceArchiveError()
                    inspect_icc(field.value)
                elif field.tag in (269, 285):
                    if field.kind != 2 or field.value[-1:] != b"\0" or b"\0" in field.value[:-1]:
                        raise SourceArchiveError()
                    metadata_text(field.value[:-1], encoding="ascii")
                    categories.add("description")
                elif field.tag == 297:
                    values = integers(297, (3,))
                    if len(values) != 2 or values[1] and values[0] >= values[1]:
                        raise SourceArchiveError()
                else:
                    categories.update(
                        inspect_exif_field(
                            "main", field.tag, field.kind, field.count, field.value, order
                        )
                    )
            for pointer, group in pending:
                if pointer in visited:
                    raise SourceArchiveError()
                visited.add(pointer)
                metadata, successor = read_tiff_directory(payload, pointer, order, claim)
                if successor:
                    raise SourceArchiveError("capability_unavailable")
                for field in metadata:
                    metadata_bytes += len(field.value)
                    categories.update(
                        inspect_exif_field(
                            group, field.tag, field.kind, field.count, field.value, order
                        )
                    )
            if metadata_bytes > 262144:
                raise SourceArchiveError("limit_exceeded")
        if not units:
            raise SourceArchiveError("capture_incomplete")
        cursor = 8
        for start, end in sorted(spans):
            if start - cursor > 3 or any(payload[cursor:start]):
                raise SourceArchiveError("privacy_rejected")
            cursor = end
        if len(payload) - cursor > 3 or any(payload[cursor:]):
            raise SourceArchiveError("privacy_rejected")
    except (struct.error, UnicodeError, ValueError, OverflowError) as error:
        if isinstance(error, SourceArchiveError):
            raise
        raise SourceArchiveError() from error
    return tuple(units), frozenset(categories)
