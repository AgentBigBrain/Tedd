"""Finite standalone raster envelopes before optional decoding.

These checks inspect non-pixel container fields a decoder can silently omit.
They do not detect steganography, authorize capture, or replace whole decoding.
Office keeps its existing narrower admission path.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from .source_contracts import SourceArchiveError
from .source_image_metadata import inspect_exif, metadata_text
from .source_office_metadata import inspect_icc


@dataclass(frozen=True, repr=False)
class AnimationFrame:
    """Admitted native pixels and common composition controls, never an original."""

    payload: bytes
    role: str
    offset: tuple[int, int]
    disposal: int  # 0: keep, 1: background, 2: previous.
    blend: int  # 0: replace, 1: source-over.
    background: tuple[int, int, int, int] = (0, 0, 0, 0)


def _image_size(width: int, height: int, count: int = 1) -> None:
    if (
        not 0 < width <= 131072
        or not 0 < height <= 131072
        or width * height > 128_000_000
        or not 1 <= count <= 256
        or width * height * count > 256_000_000
    ):
        raise SourceArchiveError("limit_exceeded")


def inspect_bmp(payload: bytes) -> None:
    """Bounded BITMAPINFOHEADER BI_RGB; reject profile/driver/private extensions."""
    if len(payload) < 54 or payload[:2] != b"BM":
        raise SourceArchiveError()
    length, reserved_a, reserved_b, offset = struct.unpack_from("<IHHI", payload, 2)
    header, width, height, planes, depth, compression, image_size, _, _, colors, important = (
        struct.unpack_from("<IiiHHIIiiII", payload, 14)
    )
    if length != len(payload) or reserved_a or reserved_b:
        raise SourceArchiveError()
    if header != 40 or compression != 0 or planes != 1 or depth not in (1, 4, 8, 16, 24, 32):
        raise SourceArchiveError("capability_unavailable")
    _image_size(width, abs(height))
    palette = (colors or 2**depth) if depth <= 8 else 0
    if (
        (depth > 8 and colors)
        or palette > 256
        or (depth <= 8 and palette > 2**depth)
        or important > palette
    ):
        raise SourceArchiveError()
    stride = ((width * depth + 31) // 32) * 4
    if (
        offset != 54 + 4 * palette
        or offset + stride * abs(height) != length
        or image_size not in (0, stride * abs(height))
    ):
        raise SourceArchiveError()
    if any(payload[index] for index in range(57, offset, 4)):
        raise SourceArchiveError("privacy_rejected")


def _gif_blocks(payload: bytes, cursor: int, maximum: int) -> tuple[bytes, int]:
    blocks: list[bytes] = []
    size = 0
    while cursor < len(payload):
        count = payload[cursor]
        cursor += 1
        if not count:
            return b"".join(blocks), cursor
        size += count
        if size > maximum or cursor + count > len(payload):
            raise SourceArchiveError("limit_exceeded")
        blocks.append(payload[cursor : cursor + count])
        cursor += count
    raise SourceArchiveError()


def inspect_gif(
    payload: bytes, parts: list[AnimationFrame] | None = None
) -> tuple[int, frozenset[str]]:
    """Count actual image descriptors; refuse unrendered plain text/unknown apps."""
    if len(payload) < 14 or payload[:6] not in (b"GIF87a", b"GIF89a"):
        raise SourceArchiveError()
    width, height, flags, background, _ = struct.unpack_from("<HH3B", payload, 6)
    _image_size(width, height)
    palette = 2 ** ((flags & 7) + 1) if flags & 0x80 else 0
    cursor = 13 + 3 * palette
    if cursor >= len(payload) or palette and background >= palette:
        raise SourceArchiveError()
    frames, blocks, metadata_size = 0, 0, 0
    pending_control = False
    transparency: int | None = None
    disposal = 0
    loop_seen = False
    categories: set[str] = set()
    while cursor < len(payload):
        blocks += 1
        if blocks > 4096:
            raise SourceArchiveError("limit_exceeded")
        marker = payload[cursor]
        cursor += 1
        if marker == 0x3B:
            if cursor != len(payload) or not frames or pending_control:
                raise SourceArchiveError()
            return frames, frozenset(categories)
        if marker == 0x21:
            if cursor >= len(payload):
                raise SourceArchiveError()
            extension = payload[cursor]
            cursor += 1
            if extension == 0xF9:
                if (
                    cursor + 6 > len(payload)
                    or payload[cursor] != 4
                    or payload[cursor + 5] != 0
                    or pending_control
                ):
                    raise SourceArchiveError()
                control = payload[cursor + 1]
                if control & 0xE2 or (control >> 2) & 7 > 3:
                    raise SourceArchiveError("capability_unavailable")
                transparency = payload[cursor + 4] if control & 1 else None
                disposal = (control >> 2) & 7
                pending_control = True
                cursor += 6
            elif extension == 0xFF:
                if (
                    loop_seen
                    or cursor + 12 > len(payload)
                    or payload[cursor : cursor + 12] != b"\x0bNETSCAPE2.0"
                ):
                    raise SourceArchiveError("privacy_rejected")
                value, cursor = _gif_blocks(payload, cursor + 12, 3)
                if len(value) != 3 or value[0] != 1:
                    raise SourceArchiveError()
                loop_seen = True
            elif extension == 0xFE:
                value, cursor = _gif_blocks(payload, cursor, 8192)
                metadata_size += len(value)
                if metadata_size > 65536:
                    raise SourceArchiveError("limit_exceeded")
                metadata_text(value, encoding="ascii")
                categories.add("description")
            else:
                raise SourceArchiveError("capability_unavailable")
        elif marker == 0x2C:
            if cursor + 9 > len(payload):
                raise SourceArchiveError()
            left, top, frame_width, frame_height, field = struct.unpack_from(
                "<4HB", payload, cursor
            )
            descriptor_start = cursor
            cursor += 9
            if field & 0x18 or not (
                0 < frame_width <= width - left and 0 < frame_height <= height - top
            ):
                raise SourceArchiveError()
            count = 2 ** ((field & 7) + 1) if field & 0x80 else palette
            if not count or transparency is not None and transparency >= count:
                raise SourceArchiveError()
            if field & 0x80:
                cursor += 3 * count
            if cursor >= len(payload) or not 2 <= payload[cursor] <= 8:
                raise SourceArchiveError()
            value, cursor = _gif_blocks(payload, cursor + 1, 64 * 1024**2)
            if not value:
                raise SourceArchiveError()
            frames += 1
            _image_size(width, height, frames)
            if parts is not None:
                # Give the public decoder one native-sized frame, not its
                # incorrectly initialized/composited full logical screen.
                header = (
                    payload[:6]
                    + struct.pack("<HH", frame_width, frame_height)
                    + payload[10 : 13 + 3 * palette]
                )
                control_bytes = (
                    b"!\xf9\4\1\0\0" + bytes((transparency,)) + b"\0"
                    if transparency is not None
                    else b""
                )
                frame_bytes = (
                    header
                    + control_bytes
                    + b","
                    + bytes(4)
                    + payload[descriptor_start + 4 : cursor]
                    + b";"
                )
                color = (0, 0, 0, 0)
                if palette and transparency != background:
                    red, green, blue = payload[13 + 3 * background : 16 + 3 * background]
                    color = (red, green, blue, 255)
                parts.append(
                    AnimationFrame(
                        frame_bytes,
                        "frame",
                        (left, top),
                        {0: 0, 1: 0, 2: 1, 3: 2}[disposal],
                        1,
                        color,
                    )
                )
            pending_control, transparency = False, None
            disposal = 0
        else:
            raise SourceArchiveError()
    raise SourceArchiveError()


def _riff_chunks(payload: bytes) -> Iterator[tuple[bytes, bytes]]:
    cursor, count = 0, 0
    while cursor + 8 <= len(payload):
        count += 1
        size = int.from_bytes(payload[cursor + 4 : cursor + 8], "little")
        end = cursor + 8 + size
        if count > 4096 or end + size % 2 > len(payload):
            raise SourceArchiveError("limit_exceeded")
        if size % 2 and payload[end] != 0:
            raise SourceArchiveError()
        yield payload[cursor : cursor + 4], payload[cursor + 8 : end]
        cursor = end + size % 2
    if cursor != len(payload):
        raise SourceArchiveError()


def _webp_pixels(chunks: tuple[tuple[bytes, bytes], ...]) -> tuple[int, int]:
    alpha = b""
    if not chunks:
        raise SourceArchiveError()
    if chunks[0][0] == b"ALPH":
        alpha = chunks[0][1]
        if not alpha or alpha[0] & 0xC0 or alpha[0] & 3 > 1 or (alpha[0] >> 4) & 3 > 1:
            raise SourceArchiveError()
        if len(chunks) != 2 or chunks[1][0] != b"VP8 ":
            raise SourceArchiveError()
        chunks = chunks[1:]
    if len(chunks) != 1:
        raise SourceArchiveError("privacy_rejected")
    tag, value = chunks[0]
    if tag == b"VP8L":
        if len(value) < 5 or value[0] != 0x2F or value[4] & 0xE0:
            raise SourceArchiveError()
        shape = int.from_bytes(value[1:5], "little")
        result = (1 + (shape & 0x3FFF), 1 + ((shape >> 14) & 0x3FFF))
    elif tag == b"VP8 ":
        if len(value) < 10 or value[0] & 1 or value[3:6] != b"\x9d\x01\x2a":
            raise SourceArchiveError()
        result = (
            int.from_bytes(value[6:8], "little") & 0x3FFF,
            int.from_bytes(value[8:10], "little") & 0x3FFF,
        )
    else:
        raise SourceArchiveError("privacy_rejected")
    _image_size(*result)
    if alpha and alpha[0] & 3 == 0 and len(alpha) != 1 + result[0] * result[1]:
        raise SourceArchiveError()
    return result


def webp_decode_payload(payload: bytes) -> tuple[bytes, bool]:
    """Ask the pinned decoder for RGBA without trusting a lossless alpha hint.

    Only ephemeral decoding flags change—not compressed samples, metadata, or
    the retained original. A cleared extended alpha declaration must additionally
    be verified against every decoded alpha channel before any delivery.
    """
    amended = bytearray(payload)
    require_opaque = False

    def mark(start: int, end: int) -> None:
        nonlocal require_opaque
        cursor = start
        while cursor < end:
            tag = payload[cursor : cursor + 4]
            size = int.from_bytes(payload[cursor + 4 : cursor + 8], "little")
            if tag == b"VP8X":
                require_opaque = not bool(payload[cursor + 8] & 0x10)
                amended[cursor + 8] |= 0x10
            elif tag == b"VP8L":
                amended[cursor + 12] |= 0x10
            elif tag == b"ANMF":
                mark(cursor + 24, cursor + 8 + size)
            cursor += 8 + size + size % 2

    mark(12, len(payload))  # Called only after exact bounded container admission.
    return bytes(amended), require_opaque


def webp_pixel_members(payload: bytes) -> Iterator[bytes]:
    """Decode admitted intrinsic frame alpha separately from animation background.

    The input has already passed container admission and decoder-hint correction.
    These minimal standalone envelopes retain the exact compressed frame samples;
    they are not source originals or user-visible derivatives.
    """
    animated = False
    for tag, value in _riff_chunks(payload[12:]):
        if tag != b"ANMF":
            continue
        animated = True
        # Native width/height-minus-one fields are already in VP8X byte order.
        header = b"\x10\0\0\0" + value[6:12]
        body = b"WEBPVP8X\x0a\0\0\0" + header + value[16:]
        yield b"RIFF" + struct.pack("<I", len(body)) + body
    if not animated:
        yield payload


def inspect_webp(payload: bytes) -> tuple[tuple[str, ...], frozenset[str]]:
    """Validate every RIFF field, including unknown chunks inside ANMF frames."""
    if (
        len(payload) < 20
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") != len(payload) - 8
    ):
        raise SourceArchiveError()
    chunks = tuple(_riff_chunks(payload[12:]))
    if not chunks:
        raise SourceArchiveError()
    if chunks[0][0] != b"VP8X":
        _webp_pixels(chunks)
        return ("still",), frozenset()
    header = chunks[0][1]
    if len(header) != 10 or header[0] & 0xC1 or any(header[1:4]):
        raise SourceArchiveError()
    canvas = (1 + int.from_bytes(header[4:7], "little"), 1 + int.from_bytes(header[7:10], "little"))
    _image_size(*canvas)
    animated = bool(header[0] & 2)
    seen: set[bytes] = set()
    categories: set[str] = set()
    still: list[tuple[bytes, bytes]] = []
    frames, metadata_bytes = 0, 0
    for tag, value in chunks[1:]:
        if tag != b"ANMF" and tag in seen:
            raise SourceArchiveError()
        seen.add(tag)
        if tag == b"ANIM":
            if not animated or frames or len(value) != 6:
                raise SourceArchiveError()
        elif tag == b"ANMF":
            if b"ANIM" not in seen or not animated or len(value) < 24 or value[15] & 0xFC:
                raise SourceArchiveError()
            left, top = (
                2 * int.from_bytes(value[:3], "little"),
                2 * int.from_bytes(value[3:6], "little"),
            )
            width, height = (
                1 + int.from_bytes(value[6:9], "little"),
                1 + int.from_bytes(value[9:12], "little"),
            )
            if width > canvas[0] - left or height > canvas[1] - top:
                raise SourceArchiveError()
            if _webp_pixels(tuple(_riff_chunks(value[16:]))) != (width, height):
                raise SourceArchiveError()
            frames += 1
            _image_size(*canvas, frames)
        elif tag in (b"ALPH", b"VP8 ", b"VP8L"):
            if animated:
                raise SourceArchiveError()
            still.append((tag, value))
        elif tag in (b"EXIF", b"ICCP"):
            metadata_bytes += len(value)
            if metadata_bytes > 262144:
                raise SourceArchiveError("limit_exceeded")
            if tag == b"ICCP":
                if frames or still or b"ANIM" in seen:
                    raise SourceArchiveError()
                inspect_icc(value)
            else:
                categories.update(inspect_exif(value))
        else:
            raise SourceArchiveError("privacy_rejected")
    if (
        bool(header[0] & 0x20) != (b"ICCP" in seen)
        or bool(header[0] & 0x08) != (b"EXIF" in seen)
        or header[0] & 0x04
    ):
        raise SourceArchiveError()
    if animated:
        if not frames:
            raise SourceArchiveError("capture_incomplete")
    else:
        if _webp_pixels(tuple(still)) != canvas:
            raise SourceArchiveError()
        frames = 1
    return (("frame",) * frames if animated else ("still",)), frozenset(categories)


def ico_members(payload: bytes) -> tuple[tuple[bytes, str, tuple[int, int]], ...]:
    """Return bounded decoding envelopes in exact directory order, not by size.

    PNG bytes are supplied unchanged. DIB data receives a one-entry ICO envelope
    for the existing decoder; that derivative is never retained as the original.
    Shared member spans remain separate variants. Partial overlaps and hidden
    suffixes are refused.
    """
    if not 6 <= len(payload) <= 64 * 1024**2 or payload[:4] != b"\0\0\1\0":
        raise SourceArchiveError()
    count = int.from_bytes(payload[4:6], "little")
    if not 1 <= count <= 256 or 6 + count * 16 > len(payload):
        raise SourceArchiveError("limit_exceeded")
    members: list[tuple[bytes, str, tuple[int, int]]] = []
    spans: set[tuple[int, int]] = set()
    for index in range(count):
        entry = payload[6 + index * 16 : 22 + index * 16]
        width, height, colors, reserved, planes, depth, size, offset = struct.unpack(
            "<4B2H2I", entry
        )
        width, height = width or 256, height or 256
        if reserved or planes not in (0, 1) or depth not in (0, 1, 4, 8, 16, 24, 32):
            raise SourceArchiveError()
        if size < 8 or offset < 6 + 16 * count or offset + size > len(payload):
            raise SourceArchiveError()
        span = (offset, offset + size)
        if any(
            span != previous and max(span[0], previous[0]) < min(span[1], previous[1])
            for previous in spans
        ):
            raise SourceArchiveError()
        spans.add(span)
        member = payload[offset : offset + size]
        kind = "png"
        if not member.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(member) < 40:
                raise SourceArchiveError()
            (
                header,
                dib_width,
                dib_height,
                dib_planes,
                dib_depth,
                compression,
                image_size,
                xppm,
                yppm,
                used,
                important,
            ) = struct.unpack_from("<IiiHHIIiiII", member)
            if (
                header != 40
                or compression
                or dib_planes != 1
                or dib_depth not in (1, 4, 8, 16, 24, 32)
            ):
                raise SourceArchiveError("capability_unavailable")
            if (
                (dib_width, dib_height) != (width, height * 2)
                or depth not in (0, dib_depth)
                or xppm
                or yppm
                or used
                or important
            ):
                raise SourceArchiveError()
            palette = 2**dib_depth if dib_depth <= 8 else 0
            if 40 + palette * 4 > len(member):
                raise SourceArchiveError()
            if colors not in (0, palette) or any(
                member[index] for index in range(43, 40 + palette * 4, 4)
            ):
                raise SourceArchiveError()
            start = 40 + 4 * palette
            xor_size = ((width * dib_depth + 31) // 32) * 4 * height
            mask_size = ((width + 31) // 32) * 4 * height
            if start + xor_size + mask_size != len(member) or image_size not in (
                0,
                xor_size,
                xor_size + mask_size,
            ):
                raise SourceArchiveError()
            if dib_depth == 32 and (
                not any(member[start + 3 : start + xor_size : 4]) or any(member[start + xor_size :])
            ):
                # Mixed legacy-mask/all-zero-alpha interpretation is not a
                # faithful RGBA preview in the pinned reader. Never hide it.
                raise SourceArchiveError("capability_unavailable")
            if dib_depth < 32:
                # AND=1/XOR!=black means destination-dependent inversion, not
                # transparent RGBA. The private preview has no such destination.
                xor_stride = xor_size // height
                mask_stride = mask_size // height
                for row in range(height):
                    for column in range(width):
                        masked = member[start + xor_size + row * mask_stride + column // 8] & (
                            0x80 >> (column % 8)
                        )
                        if not masked:
                            continue
                        pixel = start + row * xor_stride + column * dib_depth // 8
                        if dib_depth <= 8:
                            shift = 8 - dib_depth - (column * dib_depth % 8)
                            palette_index = (member[pixel] >> shift) & ((1 << dib_depth) - 1)
                            color = member[40 + palette_index * 4 : 43 + palette_index * 4]
                        else:
                            color = member[pixel : pixel + dib_depth // 8]
                        if any(color):
                            raise SourceArchiveError("capability_unavailable")
            rebuilt = entry[:6] + struct.pack("<HII", dib_depth, len(member), 22)
            member = b"\0\0\1\0\1\0" + rebuilt + member
            kind = "ico"
        members.append((member, kind, (width, height)))
    cursor = 6 + count * 16
    for start, end in sorted(spans):
        if start - cursor > 3 or any(payload[cursor:start]):
            raise SourceArchiveError("privacy_rejected")
        cursor = end
    if len(payload) - cursor > 3 or any(payload[cursor:]):
        raise SourceArchiveError("privacy_rejected")
    return tuple(members)


def inspect_jpeg(payload: bytes) -> frozenset[str]:
    """Screen every marker, including APP fields omitted from Pillow Image.info."""
    if not 4 <= len(payload) <= 64 * 1024**2 or not payload.startswith(b"\xff\xd8"):
        raise SourceArchiveError()
    cursor, segments, scans, frames, metadata_bytes = 2, 0, 0, 0, 0
    in_scan = False
    categories: set[str] = set()
    seen: set[int] = set()
    icc_parts: dict[int, bytes] = {}
    icc_count = 0
    while cursor < len(payload):
        if in_scan:
            cursor = payload.find(b"\xff", cursor)
            if cursor < 0:
                raise SourceArchiveError()
        if payload[cursor] != 0xFF:
            raise SourceArchiveError()
        while cursor < len(payload) and payload[cursor] == 0xFF:
            cursor += 1
        if cursor == len(payload):
            raise SourceArchiveError()
        marker = payload[cursor]
        cursor += 1
        if in_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        in_scan = False
        segments += 1
        if segments > 4096:
            raise SourceArchiveError("limit_exceeded")
        if marker == 0xD9:
            if cursor != len(payload) or not scans or frames != 1:
                raise SourceArchiveError()
            if icc_count:
                if set(icc_parts) != set(range(1, icc_count + 1)):
                    raise SourceArchiveError()
                inspect_icc(b"".join(icc_parts[index] for index in range(1, icc_count + 1)))
            return frozenset(categories)
        if cursor + 2 > len(payload):
            raise SourceArchiveError()
        size = int.from_bytes(payload[cursor : cursor + 2], "big")
        if size < 2 or cursor + size > len(payload):
            raise SourceArchiveError()
        value = payload[cursor + 2 : cursor + size]
        cursor += size
        if marker in (0xC0, 0xC2):
            frames += 1
            if len(value) < 6 or value[0] != 8 or len(value) != 6 + 3 * value[5]:
                raise SourceArchiveError("capability_unavailable")
        elif marker in (0xC4, 0xDB, 0xDD):
            # Huffman/quantization/restart parameters are validated by decoding.
            if not value or marker == 0xDD and len(value) != 2:
                raise SourceArchiveError()
        elif marker == 0xDA:
            if not frames or not value or len(value) != 4 + 2 * value[0]:
                raise SourceArchiveError()
            scans += 1
            in_scan = True
        elif 0xE0 <= marker <= 0xEF or marker == 0xFE:
            metadata_bytes += len(value)
            if metadata_bytes > 262144:
                raise SourceArchiveError("limit_exceeded")
            if marker != 0xE2 and marker in seen:
                raise SourceArchiveError("privacy_rejected")
            seen.add(marker)
            if marker == 0xE0:
                if (
                    len(value) != 14
                    or not value.startswith(b"JFIF\0")
                    or any(value[12:14])
                    or value[7] > 2
                ):
                    raise SourceArchiveError("capability_unavailable")
            elif marker == 0xE1:
                if not value.startswith(b"Exif\0\0"):
                    raise SourceArchiveError("privacy_rejected")
                categories.update(inspect_exif(value))
            elif marker == 0xE2:
                if not value.startswith(b"ICC_PROFILE\0") or len(value) < 15:
                    raise SourceArchiveError("privacy_rejected")
                index, count = value[12:14]
                if (
                    not 1 <= index <= count <= 16
                    or index in icc_parts
                    or icc_count not in (0, count)
                ):
                    raise SourceArchiveError()
                icc_count = count
                icc_parts[index] = value[14:]
            elif marker == 0xEE:
                if len(value) != 12 or not value.startswith(b"Adobe") or value[-1] not in (0, 1, 2):
                    raise SourceArchiveError()
            elif marker == 0xFE:
                metadata_text(value, encoding="latin-1")
                categories.add("description")
            else:
                raise SourceArchiveError("privacy_rejected")
        else:
            raise SourceArchiveError("capability_unavailable")
    raise SourceArchiveError()
