"""Neutral standalone-image contract and helper tests; no user evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
import struct
import sys
import unittest
import zlib
from contextlib import closing
from dataclasses import replace
from typing import cast
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError

from tests.knowledge_graph.test_source_office import office_worker, png_chunk, png_image


def baseline_jpeg_cases() -> tuple[tuple[str, str, tuple[tuple[int, int, int], ...]], ...]:
    """Hand-authored T.81/JFIF streams; no optional encoder round trip.

    DQT=1; 12 DC symbols use four-bit canonical codes; AC contains only EOB.
    Constant 8x8 blocks encode DC=8*(sample-128), per-component predictors,
    MSB entropy, one-bit padding, and FF byte stuffing.
    """
    prefix = "ffd8ffe000104a46494600010100000100010000ffdb004300" + "01" * 64
    tables = "ffc40031000000000c000000000000000000000000000102030405060708090a0b100100000000000000000000000000000000"
    return (
        (
            prefix
            + "ffc0000b080010001001011100"
            + tables
            + "ffda0008010100003f00a7fd70014ffab007ffd9",
            "a579d2136c2d1c748b48fc5a30ac5642836b4bc6e039a079b60c541a7d37bb38",
            ((64, 64, 64), (192, 192, 192), (128, 128, 128), (224, 224, 224)),
        ),
        (
            prefix
            + "ffc00011080010001003011100021100031100"
            + tables
            + "ffda000c03010002000300003f0092fa54eafe1528256eb15ea0dd7a615582f8828074543fffd9",
            "f969c46a47cacdb586906e1f810fa83684d9c1bbbfbfd013df35985202212a45",
            ((254, 0, 0), (0, 255, 1), (0, 0, 254), (255, 255, 255)),
        ),
    )


def long_condition_png() -> bytes:
    """Hand-drawn 'DO NOT STOP' near a long image's bottom; no font/encoder dependency."""
    glyphs = {
        "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
        "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
        "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    }
    pixels = bytearray(b"\xff" * (640 * 18000 * 3))
    for letter, character in enumerate("DO NOT STOP"):
        if character == " ":
            continue
        for row, pattern in enumerate(glyphs[character]):
            for column, mark in enumerate(pattern):
                if mark == "1":
                    for dy in range(6):
                        start = ((17900 + row * 6 + dy) * 640 + 32 + letter * 36 + column * 6) * 3
                        pixels[start : start + 18] = bytes(18)
    return png_image(width=640, height=18000, pixels=bytes(pixels))


def animation_png(*, poster: bool = True) -> bytes:
    """Hand-built PNG3 stream, independent of optional image encoders.

    Red poster; blue full first frame; opaque yellow right-hand overlay which
    restores the previous frame; green left-hand overlay. Every oracle below
    follows those operations rather than asking the decoder to create fixtures.
    """

    def pixels(rgb: bytes) -> bytes:
        return zlib.compress(b"\0" + rgb)

    blue = bytes((0, 0, 255))
    header = struct.pack("!2I5B", 2, 1, 8, 2, 0, 0, 0)
    parts = [
        b"\x89PNG\r\n\x1a\n",
        png_chunk(b"IHDR", header),
        png_chunk(b"acTL", struct.pack("!2I", 3, 0)),
    ]
    if poster:
        parts.append(png_chunk(b"IDAT", pixels(bytes((255, 0, 0)) * 2)))
    sequence = 0
    for index, (width, x, rgb, dispose) in enumerate(
        (
            (2, 0, blue * 2, 0),
            (1, 1, bytes((255, 255, 0)), 2),
            (1, 0, bytes((0, 255, 0)), 0),
        )
    ):
        control = struct.pack("!5I2H2B", sequence, width, 1, x, 0, 1 + index, 10, dispose, 1)
        parts.append(png_chunk(b"fcTL", control))
        sequence += 1
        if index == 0 and not poster:
            parts.append(png_chunk(b"IDAT", pixels(rgb)))
        else:
            parts.append(png_chunk(b"fdAT", struct.pack("!I", sequence) + pixels(rgb)))
            sequence += 1
    parts.append(png_chunk(b"IEND", b""))
    return b"".join(parts)


def exif_fields(fields: tuple[tuple[int, int, int, bytes], ...]) -> bytes:
    """Little-endian EXIF IFD with explicit types and values; no image encoder."""
    entries = bytearray()
    values = bytearray()
    offset = 8 + 2 + len(fields) * 12 + 4
    for tag, kind, count, raw in fields:
        if len(raw) <= 4:
            value = raw.ljust(4, b"\0")
        else:
            value = struct.pack("<I", offset + len(values))
            values.extend(raw)
            if len(values) % 2:
                values.append(0)
        entries.extend(struct.pack("<HHI", tag, kind, count) + value)
    return (
        b"Exif\0\0II*\0\x08\0\0\0" + struct.pack("<H", len(fields)) + entries + b"\0" * 4 + values
    )


def exif_location() -> bytes:
    """Explicit neutral GPS 0N/0E; offsets refer to the embedded TIFF header."""
    parent = struct.pack("<H", 1) + struct.pack("<HHII", 0x8825, 4, 1, 26) + bytes(4)
    gps = (
        struct.pack("<H", 4)
        + struct.pack("<HHI", 1, 2, 2)
        + b"N\0\0\0"
        + struct.pack("<HHII", 2, 5, 3, 80)
        + struct.pack("<HHI", 3, 2, 2)
        + b"E\0\0\0"
        + struct.pack("<HHII", 4, 5, 3, 104)
        + bytes(4)
        + struct.pack("<6I", 0, 1, 0, 1, 0, 1) * 2
    )
    return b"Exif\0\0II*\0\x08\0\0\0" + parent + gps


def animation_gif() -> bytes:
    """Hand-built GIF89a with literal indices, disposal and partial frames."""
    palette = bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0, 255, 255, 255)) + bytes(
        6
    )
    result = b"GIF89a" + struct.pack("<HH3B", 4, 2, 0x82, 0, 0) + palette
    for x, width, height, disposal, transparent, pixels in (
        (0, 4, 2, 1, False, (1, 1, 1, 1, 2, 2, 2, 2)),
        (0, 2, 1, 3, True, (3, 0)),
        (2, 2, 1, 2, False, (4, 4)),
        (0, 2, 1, 1, True, (0, 5)),
    ):
        # Minimum code size3: clear8 before each index keeps every code four bits.
        codes = [code for pixel in pixels for code in (8, pixel)] + [9]
        encoded = bytes(
            codes[index] | ((codes[index + 1] if index + 1 < len(codes) else 0) << 4)
            for index in range(0, len(codes), 2)
        )
        result += b"!\xf9\4" + struct.pack("<BHB", disposal * 4 + int(transparent), 10, 0) + b"\0"
        result += b"," + struct.pack("<4HB", x, 0, width, height, 0)
        result += b"\3" + bytes((len(encoded),)) + encoded + b"\0"
    return result + b";"


def bitmap_image(*, top_down: bool = False) -> bytes:
    """Exact uncompressed24-bit BGR rows: red/blue, then green/white."""
    rows = [bytes((0, 0, 255, 255, 0, 0, 0, 0)), bytes((0, 255, 0, 255, 255, 255, 0, 0))]
    pixels = b"".join(rows if top_down else reversed(rows))
    return (
        b"BM"
        + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
        + struct.pack(
            "<IiiHHIIiiII", 40, 2, -2 if top_down else 2, 1, 24, 0, len(pixels), 0, 0, 0, 0
        )
        + pixels
    )


def riff_chunk(tag: bytes, value: bytes) -> bytes:
    return tag + struct.pack("<I", len(value)) + value + bytes(len(value) % 2)


def tiff_pages(*, order: str = "<", compression: int = 1, orientation: int = 1) -> bytes:
    """Two independently assembled RGB pages with disjoint IFD/strip spans."""
    output = bytearray((b"II*\0" if order == "<" else b"MM\0*") + struct.pack(order + "I", 8))
    previous_link: int | None = None
    for width, height, pixels in (
        (2, 1, bytes((255, 0, 0, 0, 0, 255))),
        (1, 2, bytes((0, 255, 0, 255, 255, 255))),
    ):
        if len(output) % 2:
            output.append(0)
        offset = len(output)
        if previous_link is not None:
            struct.pack_into(order + "I", output, previous_link, offset)
        fields = (
            (256, 4, width),
            (257, 4, height),
            (258, 3, (8, 8, 8)),
            (259, 3, compression),
            (262, 3, 2),
            (273, 4, 0),
            (274, 3, orientation),
            (277, 3, 3),
            (278, 4, height),
            (279, 4, 0),
            (284, 3, 1),
        )
        start = offset + 2 + len(fields) * 12 + 4
        encoded = pixels
        if compression == 32773:
            stride = width * 3
            encoded = b"".join(
                bytes((stride - 1,)) + pixels[row : row + stride]
                for row in range(0, len(pixels), stride)
            )
        elif compression == 8:
            encoded = zlib.compress(pixels)
        elif compression == 5:
            # Explicit9-bit clear/literal pairs; no LZW encoder dependency.
            bits = (
                "".join(f"{code:09b}" for pixel in pixels for code in (256, pixel)) + f"{257:09b}"
            )
            encoded = int(bits.ljust((len(bits) + 7) // 8 * 8, "0"), 2).to_bytes(
                (len(bits) + 7) // 8, "big"
            )
        output.extend(struct.pack(order + "H", len(fields)))
        for tag, kind, value in fields:
            if tag == 258:
                encoded_value, count = struct.pack(order + "I", start), 3
            else:
                assert isinstance(value, int)
                value = start + 6 if tag == 273 else len(encoded) if tag == 279 else value
                encoded_value, count = (
                    struct.pack(order + ("H" if kind == 3 else "I"), value).ljust(4, b"\0"),
                    1,
                )
            output.extend(struct.pack(order + "HHI", tag, kind, count) + encoded_value)
        previous_link = len(output)
        output.extend(bytes(4) + struct.pack(order + "3H", 8, 8, 8) + encoded)
    return bytes(output)


def webp_literals(width: int, height: int, pixels: tuple[tuple[int, int, int, int], ...]) -> bytes:
    """Tiny literal VP8L: no transforms, cache, LZ77 or optional image encoder.

    Per Google's lossless specification, four simple two-symbol prefix trees
    encode channel0/255; the unused distance tree has only symbol0. Literal
    channels are emitted G,R,B,A, least-significant bit first.
    """
    assert len(pixels) == width * height
    bits = [0, 0, 0] + ([1, 1, 0, 0] + [1] * 8) * 4 + [1, 0, 0, 0]
    for pixel in pixels:
        assert all(value in (0, 255) for value in pixel)
        bits.extend(int(pixel[channel] == 255) for channel in (1, 0, 2, 3))
    encoded = bytes(
        sum(bit << offset for offset, bit in enumerate(bits[index : index + 8]))
        for index in range(0, len(bits), 8)
    )
    return b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14) | (1 << 28)) + encoded


def animation_webp(*, replace_last: bool = False) -> bytes:
    clear, red, green, blue, yellow, white = (
        (0, 0, 0, 0),
        (255, 0, 0, 255),
        (0, 255, 0, 255),
        (0, 0, 255, 255),
        (255, 255, 0, 255),
        (255, 255, 255, 255),
    )
    parts = [riff_chunk(b"VP8X", b"\x12\0\0\0\3\0\0\1\0\0"), riff_chunk(b"ANIM", bytes(6))]
    for x, width, height, flags, pixels in (
        (0, 4, 2, 2, (red,) * 4 + (green,) * 4),
        (0, 2, 1, 0, (blue, clear)),
        (2, 2, 1, 1, (yellow, yellow)),
        (0, 2, 1, 2 if replace_last else 0, (clear, white)),
    ):
        header = b"".join(
            value.to_bytes(3, "little") for value in (x // 2, 0, width - 1, height - 1, 100)
        ) + bytes((flags,))
        parts.append(
            riff_chunk(b"ANMF", header + riff_chunk(b"VP8L", webp_literals(width, height, pixels)))
        )
    body = b"WEBP" + b"".join(parts)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class ImageInventoryTests(unittest.TestCase):
    def test_preview_limits_are_fixed_capabilities_not_source_metadata(self) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageReading, ImageUnit

        common = {
            "image_pixels_require_viewing",
            "image_preview_8bit_color_not_calibrated",
            "image_preview_hdr_not_reproduced",
        }
        for format_name, units, extra in (
            ("png", (ImageUnit("still", 0, (2, 2)),), set()),
            (
                "jpeg",
                (ImageUnit("still", 0, (2, 2)),),
                {"encoded_bytes_not_exhaustively_inspected"},
            ),
            (
                "heif",
                (ImageUnit("item", 0, (2, 2)),),
                {"encoded_bytes_not_exhaustively_inspected"},
            ),
            ("gif", (ImageUnit("frame", 0, (2, 2)),), {"animation_timing_not_reproduced"}),
            ("svg", (ImageUnit("still", 0, (2, 2)),), {"svg_fixed_source_sans_pro_font"}),
            (
                "avif",
                (ImageUnit("item", 0, (2, 2)), ImageUnit("auxiliary", 1, (2, 2), parent=0)),
                {"auxiliary_pixels_not_calibrated_measurements"},
            ),
        ):
            with self.subTest(format=format_name):
                inventory = ImageInventory("a" * 64, format_name, units)
                self.assertEqual(set(inventory.gaps), common | extra)
                self.assertEqual(ImageReading(inventory).document.gaps, inventory.gaps)
                self.assertEqual(
                    replace(inventory, metadata_categories=("location",)).gaps, inventory.gaps
                )
                self.assertEqual(ImageInventory.decode(inventory.as_dict()), inventory)

    def test_cropped_source_requires_distinct_reconstruction_review_for_same_unit(self) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageReading, ImageUnit
        from tedd_knowledge_graph.source_review import ReviewBook, ReviewRendering

        unit = ImageUnit("item", 0, (8, 8), reconstruction_size=(64, 64))
        inventory = ImageInventory("a" * 64, "heif", (unit,))
        self.assertEqual(len(inventory.source_units()), 1)
        self.assertEqual(inventory.pixel_count, 4096)
        self.assertEqual(ImageInventory.decode(inventory.as_dict()), inventory)
        reading = ImageReading(inventory)
        book = ReviewBook()
        display = ReviewRendering(
            "b" * 64, "c" * 64, "a" * 64, "u1", "model", "d" * 64, (8, 8), (8, 8)
        )
        token = book.offer(display)
        book.delivered(token)
        book.acknowledge(token, audience="model")
        with self.assertRaises(SourceArchiveError):
            reading.require_reviews(book, "b" * 64, "c" * 64, "model")
        # Even an equal-sized view cannot impersonate a different view's receipt.
        self.assertFalse(book.acknowledged(replace(display, view="reconstruction")))
        reconstructed = replace(
            display, original_size=(64, 64), output_size=(64, 64), view="reconstruction"
        )
        self.assertFalse(book.acknowledged(reconstructed))
        token = book.offer(reconstructed)
        book.delivered(token)
        book.acknowledge(token, audience="model")
        reading.require_reviews(book, "b" * 64, "c" * 64, "model")
        with self.assertRaises(SourceArchiveError):
            reading.require_reviews(book, "b" * 64, "c" * 64, "owner")
        book.close()

    def test_exif_copyright_accepts_two_screened_components_not_arbitrary_nul_text(self) -> None:
        from tedd_knowledge_graph.source_image_metadata import inspect_exif

        for content in (b"A\0", b"A\0B\0", b" \0B\0"):
            self.assertEqual(
                inspect_exif(exif_fields(((0x8298, 2, len(content), content),))),
                frozenset({"description"}),
            )
        for content in (b"A\0password=SECRET!\0", b"A\0B\0C\0", b"A\0B", b"\0B\0"):
            with self.assertRaises(SourceArchiveError):
                inspect_exif(exif_fields(((0x8298, 2, len(content), content),)))
        with self.assertRaises(SourceArchiveError):
            inspect_exif(exif_fields(((0x010E, 2, 4, b"A\0B\0"),)))

    def test_tiff_lzw_counts_code_width_changes_and_refuses_hidden_suffixes(self) -> None:
        from tedd_knowledge_graph.source_image_tiff import _strip

        # TIFF6 switches from9 to10 after decoder entry510:254 literal
        # samples follow CLEAR before that transition. This is not an encoder.
        bits = (
            f"{256:09b}"
            + "".join(f"{value:09b}" for value in range(254))
            + f"{254:010b}{255:010b}{257:010b}"
        )
        raw = int(bits.ljust((len(bits) + 7) // 8 * 8, "0"), 2).to_bytes(
            (len(bits) + 7) // 8, "big"
        )
        _strip(raw, 5, 256, 1)
        for content, width in ((raw + b"suffix", 256), (raw[:-1], 256), (raw, 255), (raw, 257)):
            with self.assertRaises(SourceArchiveError):
                _strip(content, 5, width, 1)

    def test_exif_numeric_tag_types_counts_and_gps_direction_are_not_generic_numbers(self) -> None:
        from tedd_knowledge_graph.source_image_metadata import inspect_exif

        valid_resolution = exif_fields(((0x011A, 5, 1, struct.pack("<II", 72, 1)),))
        self.assertEqual(inspect_exif(valid_resolution), frozenset())
        for field in (
            (0x011A, 4, 4, b"password=SECRET!"),
            (0x011A, 3, 1, struct.pack("<H", 72)),
            (0x0112, 3, 2, struct.pack("<HH", 1, 1)),
            (0x829A, 5, 1, struct.pack("<II", 1, 60)),  # Exif-only tag in IFD0.
        ):
            with self.subTest(tag=field[0]), self.assertRaises(SourceArchiveError):
                inspect_exif(exif_fields((field,)))
        prefix = b"Exif\0\0II*\0\x08\0\0\0" + struct.pack("<HHHII", 1, 0x8825, 4, 1, 26) + bytes(4)
        gps = struct.pack("<H", 3) + struct.pack("<HHI", 0, 1, 4) + b"\2\3\0\0"
        gps += struct.pack("<HHI", 16, 2, 2) + b"T\0\0\0"
        gps += struct.pack("<HHII", 17, 5, 1, 68) + bytes(4)
        self.assertEqual(
            inspect_exif(prefix + gps + struct.pack("<II", 359, 1)), frozenset({"location"})
        )
        for direction in (360, 361):
            with self.assertRaises(SourceArchiveError):
                inspect_exif(prefix + gps + struct.pack("<II", direction, 1))

    def test_metadata_profile_requires_explicit_classification_without_exposing_values(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageUnit

        base = ImageInventory("a" * 64, "jpeg", (ImageUnit("still", 0, (2, 2)),))
        for categories, allowed in (
            ((), {"ordinary", "confidential", "medical", "restricted"}),
            (("description",), {"confidential", "medical", "restricted"}),
            (("identifying",), {"confidential", "restricted"}),
            (("location",), {"restricted"}),
        ):
            inventory = replace(base, metadata_categories=categories)
            self.assertEqual(ImageInventory.decode(inventory.as_dict()), inventory)
            for classification in ("ordinary", "confidential", "medical", "restricted", "unknown"):
                with self.subTest(categories=categories, classification=classification):
                    if classification in allowed:
                        inventory.require_metadata_permission(classification)
                    else:
                        with self.assertRaises(SourceArchiveError):
                            inventory.require_metadata_permission(classification)
        for malformed in (("location", "location"), ("secret",), ("location", "description")):
            with self.assertRaises(SourceArchiveError):
                replace(base, metadata_categories=malformed)

    def test_exif_metadata_profile_screens_all_fields_and_unreferenced_bytes(self) -> None:
        from tedd_knowledge_graph.source_image_metadata import inspect_exif

        oriented = exif_fields(((0x0112, 3, 1, struct.pack("<H", 6)),))
        described = exif_fields(((0x010E, 2, 17, b"Synthetic sample\0"),))
        identified = exif_fields(((0x013B, 2, 16, b"Synthetic owner\0"),))
        for raw, categories in (
            (oriented, ()),
            (described, ("description",)),
            (identified, ("identifying",)),
            (exif_location(), ("location",)),
        ):
            self.assertEqual(inspect_exif(raw), frozenset(categories))
        for raw in (
            exif_fields(((0x927C, 7, 4, b"data"),)),
            exif_fields(((0x9999, 2, 7, b"secret\0"),)),
            exif_fields(((0x010E, 2, 30, b"password=synthetic-fixture-123\0"),)),
            exif_fields(((0x0112, 3, 1, struct.pack("<H", 9)),)),
            described + b"unreferenced private bytes",
            described[:-2],
        ):
            with self.subTest(size=len(raw)), self.assertRaises(SourceArchiveError):
                inspect_exif(raw)

    def test_image_manifests_require_format_four_without_rewriting_legacy_identities(self) -> None:
        from tedd_knowledge_graph.source_contracts import (
            SourceDetails,
            SourceManifest,
            decode_manifest,
            encode_manifest,
        )

        from tests.knowledge_graph.test_source_archive import metadata

        source = replace(
            metadata(),
            kind="image",
            capture_method="local_file",
            details=SourceDetails(
                "local", "owner", "original", "original", "tedd-image:1", identity_version=2
            ),
        )
        manifest = SourceManifest(source, "a" * 64, 10, "b" * 64, 1, None, format_version=4)
        self.assertEqual(decode_manifest(encode_manifest(manifest)), manifest)
        assert source.details is not None
        with self.assertRaises(SourceArchiveError):
            replace(
                manifest,
                format_version=3,
                metadata=replace(source, details=replace(source.details, identity_version=1)),
            )

    def test_image_reading_requires_exact_delivered_review_not_a_model_checksum_claim(self) -> None:
        import base64
        from dataclasses import asdict

        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_images import ImageInventory, ImageReading, ImageUnit
        from tedd_knowledge_graph.source_review import ReviewBook

        raw = png_image(width=2, height=2)
        inventory = ImageInventory(digest(raw), "png", (ImageUnit("still", 0, (2, 2)),))
        value: dict[str, object] = {
            "status": "text_screened",
            "resources": "bounded",
            "kind": "image",
            "original_sha256": digest(raw),
            "reader_id": "tedd-image:1",
            "units": [asdict(unit) for unit in inventory.source_units()],
            "gaps": list(inventory.gaps),
            "inventory": inventory.as_dict(),
            "position": 1,
            "original_size": [2, 2],
            "output_size": [2, 2],
            "orientation": 1,
            "rectangle": None,
            "png_sha256": digest(raw),
            "png_base64": base64.b64encode(raw).decode("ascii"),
        }
        reading = ImageReading.decode(raw, value)
        self.assertEqual(reading.document.kind, "image")
        self.assertEqual(reading.document.original_sha256, digest(raw))
        book = ReviewBook()
        rendering = reading.review("a" * 64, "b" * 64, "model")
        token = book.offer(rendering)
        for acknowledged in (False, True):
            if acknowledged:
                book.delivered(token)
                book.acknowledge(token, audience="model")
                reading.require_reviews(book, "a" * 64, "b" * 64, "model")
            else:
                with self.assertRaises(SourceArchiveError):
                    reading.require_reviews(book, "a" * 64, "b" * 64, "model")
        for scope, source, audience in (
            ("c" * 64, "b" * 64, "model"),
            ("a" * 64, "c" * 64, "model"),
            ("a" * 64, "b" * 64, "owner"),
        ):
            with self.assertRaises(SourceArchiveError):
                reading.require_reviews(book, scope, source, audience)
        for changed in (
            {**value, "original_sha256": "d" * 64},
            {**value, "original_size": [4, 4]},
            {**value, "units": []},
            {**value, "reviewed": True},
            {**value, "rectangle": [0, 0, 1, 1]},
        ):
            with self.assertRaises(SourceArchiveError):
                ImageReading.decode(raw, changed)

    def test_native_selectors_keep_posters_frames_pages_items_and_variants_distinct(self) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageUnit

        for format_name, roles in (
            ("apng", ("poster", "frame", "frame")),
            ("tiff", ("page", "page", "page")),
            ("heif", ("item", "item", "auxiliary")),
            ("ico", ("variant", "variant", "variant")),
        ):
            with self.subTest(format=format_name):
                units = tuple(
                    ImageUnit(role, index, (64, 64), parent=0 if role == "auxiliary" else None)
                    for index, role in enumerate(roles)
                )
                inventory = ImageInventory("a" * 64, format_name, units)
                reading = inventory.source_units()
                self.assertEqual([unit.anchor_id for unit in reading], ["u1", "u2", "u3"])
                self.assertTrue(
                    all(unit.kind == "image_page" and unit.text == "" for unit in reading)
                )
                self.assertEqual(len({unit.location for unit in reading}), 3)
                self.assertEqual(inventory.pixel_count, 12288)
                self.assertEqual(ImageInventory.decode(inventory.as_dict()), inventory)

    def test_inventory_rejects_partial_duplicate_unknown_and_over_budget_members(self) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageUnit

        first = ImageUnit("frame", 0, (8000, 6000))
        for label, create in (
            ("empty", lambda: ImageInventory("a" * 64, "gif", ())),
            ("duplicate", lambda: ImageInventory("a" * 64, "gif", (first, first))),
            (
                "aggregate",
                lambda: ImageInventory(
                    "a" * 64, "gif", tuple(replace(first, native_index=n) for n in range(6))
                ),
            ),
            (
                "too_many",
                lambda: ImageInventory(
                    "a" * 64, "gif", tuple(ImageUnit("frame", n, (1, 1)) for n in range(257))
                ),
            ),
            ("wrong_role", lambda: ImageInventory("a" * 64, "jpeg", (first,))),
            (
                "mutable",
                lambda: ImageInventory("a" * 64, "gif", cast(tuple[ImageUnit, ...], [first])),
            ),
            ("unknown", lambda: ImageInventory("a" * 64, "unknown", (first,))),
            ("large_dimension", lambda: ImageUnit("still", 0, (131073, 1))),
            ("large_pixels", lambda: ImageUnit("still", 0, (16000, 9000))),
            ("boolean_index", lambda: ImageUnit("still", True, (1, 1))),
            ("missing_parent", lambda: ImageUnit("auxiliary", 1, (1, 1))),
            ("malformed_role", lambda: ImageUnit(cast(str, []), 0, (1, 1))),
        ):
            with self.subTest(case=label), self.assertRaises(SourceArchiveError):
                create()

    def test_serialized_inventory_does_not_accept_unknown_content_or_coerced_numbers(self) -> None:
        from tedd_knowledge_graph.source_images import ImageInventory, ImageUnit

        inventory = ImageInventory("a" * 64, "png", (ImageUnit("still", 0, (640, 480)),))
        value = inventory.as_dict()
        value["caption"] = "untrusted source text"
        with self.assertRaises(SourceArchiveError):
            ImageInventory.decode(value)
        value = inventory.as_dict()
        units = value["units"]
        assert isinstance(units, list)
        first = units[0]
        assert isinstance(first, dict)
        first["native_index"] = "0"
        with self.assertRaises(SourceArchiveError):
            ImageInventory.decode(value)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "optional image helper is unavailable")
class RasterHelperTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_long_screenshot_bottom_negation_survives_exact_native_detail(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_contracts import decoded_png, digest

        raw = long_condition_png()
        original_hash = digest(raw)
        overview = office_worker(raw, "image", "--render-unit", "1")
        self.assertEqual(overview.returncode, 0, overview.stdout)
        summary = json.loads(overview.stdout)
        self.assertEqual(summary["output_size"], [73, 2048])
        self.assertEqual(summary["original_size"], [640, 18000])
        detail = office_worker(
            raw, "image", "--render-unit", "1", "--rectangle", "0,17800,640,18000"
        )
        self.assertEqual(detail.returncode, 0, detail.stdout)
        value = json.loads(detail.stdout)
        self.assertEqual(value["original_sha256"], original_hash)
        self.assertEqual(value["rectangle"], [0, 17800, 640, 18000])
        self.assertEqual(value["output_size"], [640, 200])
        with (
            closing(Image.open(io.BytesIO(raw), formats=["PNG"])) as original,
            closing(Image.open(io.BytesIO(decoded_png(value)), formats=["PNG"])) as viewed,
            closing(original.crop((0, 17800, 640, 18000))) as expected,
            closing(viewed.convert("RGB")) as actual,
        ):
            self.assertEqual(digest(actual.tobytes()), digest(expected.tobytes()))
            # Fixed locations in the source's six-pixel glyphs preserve the
            # N/O/T strokes and their whitespace; overview alone proves no words.
            self.assertEqual(actual.getpixel((140, 100)), (0, 0, 0))
            self.assertEqual(actual.getpixel((146, 100)), (255, 255, 255))
            self.assertEqual(actual.getpixel((188, 100)), (0, 0, 0))
            self.assertEqual(actual.getpixel((224, 100)), (0, 0, 0))
        self.assertEqual(digest(raw), original_hash)

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_high_entropy_png_reduces_overview_but_never_shrinks_native_detail(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_contracts import decoded_png, digest

        # SHAKE supplies a fixed, incompressible synthetic pixel plane. The
        # original uses the existing hand-built PNG helper, not a Pillow encoder.
        pixels = hashlib.shake_256(b"tedd-high-entropy-image-v1").digest(2048 * 2048 * 3)
        raw = png_image(width=2048, height=2048, pixels=pixels)
        original_hash = digest(raw)
        self.assertGreater(len(raw), 8 * 1024**2)

        overview = office_worker(raw, "image", "--render-unit", "1")
        self.assertEqual(overview.returncode, 0, overview.stdout)
        value = json.loads(overview.stdout)
        self.assertEqual(value["original_sha256"], original_hash)
        self.assertEqual(value["original_size"], [2048, 2048])
        self.assertIsNone(value["rectangle"])
        self.assertEqual(value["output_size"][0], value["output_size"][1])
        self.assertLess(value["output_size"][0], 2048)
        png = decoded_png(value)
        self.assertLessEqual(len(png), 8 * 1024**2)
        with Image.open(io.BytesIO(png), formats=["PNG"]) as image:
            self.assertEqual(list(image.size), value["output_size"])
            self.assertEqual(image.info, {})

        too_large = office_worker(
            raw, "image", "--render-unit", "1", "--rectangle", "0,0,2048,2048"
        )
        self.assertNotEqual(too_large.returncode, 0)
        self.assertEqual(json.loads(too_large.stdout), {"status": "limit_exceeded"})
        self.assertEqual(too_large.stderr, b"")

        detail = office_worker(
            raw, "image", "--render-unit", "1", "--rectangle", "1536,1536,2048,2048"
        )
        self.assertEqual(detail.returncode, 0, detail.stdout)
        value = json.loads(detail.stdout)
        self.assertEqual(value["original_sha256"], original_hash)
        self.assertEqual(value["rectangle"], [1536, 1536, 2048, 2048])
        self.assertEqual(value["output_size"], [512, 512])
        expected = b"".join(
            pixels[(row * 2048 + 1536) * 3 : (row * 2048 + 2048) * 3] for row in range(1536, 2048)
        )
        with Image.open(io.BytesIO(decoded_png(value)), formats=["PNG"]) as image:
            with image.convert("RGB") as rgb:
                self.assertEqual(digest(rgb.tobytes()), digest(expected))
        self.assertEqual(digest(raw), original_hash)

    def test_png_output_buffer_closes_on_success_and_encoding_failure(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph import source_image_host

        constructor = io.BytesIO
        streams: list[io.BytesIO] = []

        def owned_buffer(initial: bytes = b"") -> io.BytesIO:
            stream = constructor(initial)
            streams.append(stream)
            return stream

        Image.init()
        with Image.new("RGBA", (2, 1), (1, 2, 3, 255)) as input_image:
            for failure in (False, True):
                streams.clear()
                with self.subTest(failure=failure), mock.patch.object(io, "BytesIO", owned_buffer):
                    if failure:
                        with (
                            mock.patch.object(
                                Image.Image,
                                "save",
                                side_effect=OSError("synthetic encoder failure"),
                            ),
                            self.assertRaises(OSError),
                        ):
                            source_image_host._render_pixels(
                                input_image, Image, size=None, rectangle=None
                            )
                    else:
                        png = source_image_host._render_pixels(
                            input_image, Image, size=None, rectangle=None
                        )
                        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertTrue(streams)
                self.assertTrue(all(stream.closed for stream in streams))

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_reconstruction_option_cannot_relabel_ordinary_pixels(self) -> None:
        raw = png_image()
        normal = office_worker(raw, "image", "--render-unit", "1")
        explicit = office_worker(raw, "image", "--render-unit", "1", "--view", "presentation")
        self.assertEqual(normal.returncode, 0, normal.stdout)
        self.assertEqual((explicit.returncode, explicit.stdout), (normal.returncode, normal.stdout))
        for options in (
            ("--view", "reconstruction"),
            ("--view", "unknown"),
            ("--view", "presentation", "--view", "presentation"),
        ):
            with self.subTest(options=options):
                refused = office_worker(raw, "image", "--render-unit", "1", *options)
                self.assertNotEqual(refused.returncode, 0)
                self.assertEqual(set(json.loads(refused.stdout)), {"status"})

    def test_gray16_transparency_is_applied_before_overview_resampling(self) -> None:
        from tedd_knowledge_graph.source_image_host import image_response

        def png(depth: int, color: int, row: bytes, transparency: bool) -> bytes:
            return (
                b"\x89PNG\r\n\x1a\n"
                + png_chunk(b"IHDR", struct.pack("!2I5B", 4096, 1, depth, color, 0, 0, 0))
                + (png_chunk(b"tRNS", b"\0\0") if transparency else b"")
                + png_chunk(b"IDAT", zlib.compress(b"\0" + row))
                + png_chunk(b"IEND", b"")
            )

        gray = png(16, 0, b"\0\0\xff\xff" * 2048, True)
        rgba = png(8, 6, b"\0\0\0\0\xff\xff\xff\xff" * 2048, False)
        self.assertEqual(
            image_response(gray, 1, rectangle=(0, 0, 2, 1))["png_sha256"],
            image_response(rgba, 1, rectangle=(0, 0, 2, 1))["png_sha256"],
        )
        self.assertEqual(
            image_response(gray, 1)["png_sha256"], image_response(rgba, 1)["png_sha256"]
        )

    def test_tiff_gray_palette_and_straight_alpha_have_literal_pixel_oracles(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        for photo, depth, samples, pixels, extra, colors in (
            (1, 8, 1, b"\x40\xc0", (), ((64, 64, 64, 255), (192, 192, 192, 255))),
            (0, 8, 1, b"\x40\xc0", (), ((191, 191, 191, 255), (63, 63, 63, 255))),
            (1, 1, 1, b"\x40", (), ((0, 0, 0, 255), (255, 255, 255, 255))),
            (
                2,
                8,
                4,
                b"\xff\0\0\x40\0\0\xff\xff",
                ((338, 3, 1, b"\2\0"),),
                ((255, 0, 0, 64), (0, 0, 255, 255)),
            ),
            (
                3,
                1,
                1,
                b"\x40",
                ((320, 3, 6, struct.pack("<6H", 65535, 0, 0, 0, 0, 65535)),),
                ((255, 0, 0, 255), (0, 0, 255, 255)),
            ),
        ):
            with self.subTest(photo=photo, depth=depth, samples=samples):
                fields = (
                    tuple(
                        (tag, kind, 1, struct.pack("<H" if kind == 3 else "<I", value))
                        for tag, kind, value in (
                            (256, 4, 2),
                            (257, 4, 1),
                            (259, 3, 1),
                            (262, 3, photo),
                            (273, 4, 0),
                            (277, 3, samples),
                            (278, 4, 1),
                            (279, 4, len(pixels)),
                        )
                    )
                    + ((258, 3, samples, struct.pack("<" + "H" * samples, *([depth] * samples))),)
                    + extra
                )
                fields = tuple(sorted(fields))
                start = len(exif_fields(fields)) - 6
                fields = tuple(
                    (tag, kind, count, struct.pack("<I", start) if tag == 273 else value)
                    for tag, kind, count, value in fields
                )
                raw = exif_fields(fields)[6:] + pixels
                with module.open(io.BytesIO(decoded_png(image_response(raw, 1)))) as image:
                    self.assertEqual(tuple(image.getpixel((x, 0)) for x in range(2)), colors)

    def test_tiff_tiled_rgb_checks_every_tile_and_oriented_detail(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        fields = tuple(
            (tag, kind, 1, struct.pack("<H" if kind == 3 else "<I", value))
            for tag, kind, value in (
                (256, 4, 17),
                (257, 4, 1),
                (259, 3, 1),
                (262, 3, 2),
                (274, 3, 6),
                (277, 3, 3),
                (284, 3, 1),
                (322, 4, 16),
                (323, 4, 16),
            )
        ) + (
            (258, 3, 3, struct.pack("<3H", 8, 8, 8)),
            (324, 4, 2, bytes(8)),
            (325, 4, 2, struct.pack("<2I", 768, 768)),
        )
        fields = tuple(sorted(fields))
        start = len(exif_fields(fields)) - 6
        fields = tuple(
            (tag, kind, count, struct.pack("<2I", start, start + 768) if tag == 324 else value)
            for tag, kind, count, value in fields
        )
        raw = exif_fields(fields)[6:] + b"\xff\0\0" * 256 + b"\0\0\xff" * 256
        result = image_response(raw, 1, rectangle=(0, 15, 1, 17))
        self.assertEqual(result["original_size"], [17, 1])
        self.assertEqual(result["orientation"], 6)
        module = __import__("PIL.Image", fromlist=["Image"])
        with module.open(io.BytesIO(decoded_png(result))) as image:
            self.assertEqual(image.size, (1, 2))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((0, 1)), (0, 0, 255, 255))
        for malformed in (raw[:-1], raw + b"uninspected"):
            with self.assertRaises(SourceArchiveError):
                image_response(malformed, 1)

    def test_gif_local_palette_and_background_disposal_keep_distinct_colors(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        palette = bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255)) + bytes(12)
        header = b"GIF89a" + struct.pack("<HH3B", 4, 1, 0x82, 2, 0) + palette
        # Blue patch restores global green; local palette then draws yellow.
        first = b"!\xf9\4\x08\x0a\0\0\0," + struct.pack("<4HB", 1, 0, 1, 1, 0) + b"\3\2\x38\x09\0"
        local = palette[:9] + b"\xff\xff\0" + palette[12:]
        second = b"," + struct.pack("<4HB", 3, 0, 1, 1, 0x82) + local + b"\3\2\x38\x09\0;"
        raw = header + first + second
        green, blue, yellow = (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255)
        for position, expected in (
            (2, (green, green, green, yellow)),
            (1, (green, blue, green, green)),
        ):
            with module.open(io.BytesIO(decoded_png(image_response(raw, position)))) as image:
                self.assertEqual(tuple(image.getpixel((x, 0)) for x in range(4)), expected)

    def test_sixteen_bit_transparency_key_does_not_hide_neighboring_gray_values(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        raw = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack("!2I5B", 2, 1, 16, 0, 0, 0, 0))
            + png_chunk(b"tRNS", struct.pack("!H", 16384))
            + png_chunk(b"IDAT", zlib.compress(b"\0" + struct.pack("!2H", 16384, 16385)))
            + png_chunk(b"IEND", b"")
        )
        module = __import__("PIL.Image", fromlist=["Image"])
        with module.open(io.BytesIO(decoded_png(image_response(raw, 1)))) as image:
            self.assertEqual(image.getpixel((0, 0)), (64, 64, 64, 0))
            self.assertEqual(image.getpixel((1, 0)), (64, 64, 64, 255))

    def test_standalone_sixteen_bit_grayscale_preserves_midtone_contrast(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        samples = (0, 16384, 32768, 65535)
        tiff = exif_fields(
            tuple(
                (tag, kind, 1, struct.pack("<H" if kind == 3 else "<I", value))
                for tag, kind, value in (
                    (256, 4, 4),
                    (257, 4, 1),
                    (258, 3, 16),
                    (259, 3, 1),
                    (262, 3, 1),
                    (273, 4, 122),
                    (277, 3, 1),
                    (278, 4, 1),
                    (279, 4, 8),
                )
            )
        )[6:] + struct.pack("<4H", *samples)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack("!2I5B", 4, 1, 16, 0, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(b"\0" + struct.pack("!4H", *samples)))
            + png_chunk(b"IEND", b"")
        )
        module = __import__("PIL.Image", fromlist=["Image"])
        for raw in (tiff, png):
            response = image_response(raw, 1)
            gaps = response["gaps"]
            assert isinstance(gaps, list)
            self.assertIn("image_preview_8bit_color_not_calibrated", gaps)
            self.assertIn("image_preview_hdr_not_reproduced", gaps)
            with module.open(io.BytesIO(decoded_png(response))) as image:
                self.assertEqual(
                    tuple(image.getpixel((x, 0)) for x in range(4)),
                    tuple((level, level, level, 255) for level in (0, 64, 128, 255)),
                )

    def test_independent_baseline_jpegs_have_expected_grayscale_and_color_quadrants(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png, digest
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        for hex_bytes, sha256, quadrants in baseline_jpeg_cases():
            raw = bytes.fromhex(hex_bytes)
            self.assertEqual(digest(raw), sha256)
            result = image_response(raw, 1)
            self.assertEqual(result["original_sha256"], sha256)
            with module.open(io.BytesIO(decoded_png(result))) as image:
                self.assertEqual(image.size, (16, 16))
                self.assertEqual(image.info, {})
                for y in range(16):
                    for x in range(16):
                        expected = quadrants[(y // 8) * 2 + x // 8]
                        actual = image.getpixel((x, y))
                        self.assertEqual(actual[3], 255)
                        self.assertTrue(
                            all(abs(a - b) <= 1 for a, b in zip(actual[:3], expected, strict=True))
                        )
            for refused in (raw[:-2], raw[:2] + b"\xff\xe5\0\x06test" + raw[2:]):
                with self.assertRaises(SourceArchiveError):
                    image_response(refused, 1)

    def test_jpeg_ignored_entropy_is_not_claimed_as_inspected(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png, digest
        from tedd_knowledge_graph.source_image_host import image_response

        raw = bytes.fromhex(baseline_jpeg_cases()[0][0])
        baseline = image_response(raw, 1)
        altered = raw[:-2] + b"synthetic-hidden" + raw[-2:]
        self.assertEqual(len(altered), 189)
        self.assertEqual(
            digest(altered), "e2211fc109bf62faaacc6907a8fb10e818547dad70a9971186579c2bc7158f57"
        )
        # September 13 owner-approved boundary: exact originals may retain
        # decoder-ignored bytes. Pixel review must not claim they were inspected.
        result = image_response(altered, 1)
        self.assertEqual(result["original_sha256"], digest(altered))
        self.assertNotEqual(result["original_sha256"], baseline["original_sha256"])
        self.assertEqual(decoded_png(result), decoded_png(baseline))
        gaps = result["gaps"]
        assert isinstance(gaps, list)
        self.assertIn("encoded_bytes_not_exhaustively_inspected", gaps)
        self.assertNotIn(b"synthetic-hidden", decoded_png(result))
        for refused in (altered[:-2], altered + b"outer-tail"):
            with self.assertRaises(SourceArchiveError):
                image_response(refused, 1)

    @unittest.skipUnless(
        importlib.util.find_spec("pillow_heif"), "optional HEIF helper is unavailable"
    )
    def test_hevc_ignored_slice_bytes_do_not_change_constituent_review(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png, digest
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        from tests.knowledge_graph.test_source_bmff import HEVC_COLLECTION, hevc_ignored_tail

        altered = hevc_ignored_tail()
        inventory = ImageInventory.decode(image_response(HEVC_COLLECTION)["inventory"])
        for ordinal in range(1, len(inventory.units) + 1):
            with self.subTest(unit=ordinal):
                baseline = image_response(HEVC_COLLECTION, ordinal)
                result = image_response(altered, ordinal)
                self.assertEqual(result["original_sha256"], digest(altered))
                self.assertEqual(decoded_png(result), decoded_png(baseline))
                gaps = result["gaps"]
                assert isinstance(gaps, list)
                self.assertIn("encoded_bytes_not_exhaustively_inspected", gaps)
                self.assertNotIn(b"synthetic-hidden", decoded_png(result))

    def test_tiff_screens_later_page_metadata_references_and_compressed_suffixes(self) -> None:
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        raw = tiff_pages()
        next_page = int.from_bytes(raw[142:146], "little")
        # PlanarConfiguration defaults to1; replace its optional field with a
        # bounded description without changing the image or array offsets.
        entry = 10 + 10 * 12
        described = raw[:entry] + struct.pack("<HHI", 285, 2, 2) + b"x\0\0\0" + raw[entry + 12 :]
        self.assertEqual(
            ImageInventory.decode(image_response(described)["inventory"]).metadata_categories,
            ("description",),
        )
        copyright_pair = (
            raw[:entry] + struct.pack("<HHI", 0x8298, 2, 4) + b"A\0B\0" + raw[entry + 12 :]
        )
        self.assertEqual(
            ImageInventory.decode(image_response(copyright_pair)["inventory"]).metadata_categories,
            ("description",),
        )
        unknown = bytearray(raw)
        struct.pack_into("<H", unknown, next_page + 2 + 10 * 12, 65000)
        cycle = bytearray(raw)
        struct.pack_into("<I", cycle, 142, 8)
        overlap = bytearray(raw)
        struct.pack_into("<I", overlap, 10 + 5 * 12 + 8, 8)
        compressed = bytearray(tiff_pages(compression=8))
        second = int.from_bytes(compressed[142:146], "little")
        count_offset = second + 2 + 9 * 12 + 8
        size = int.from_bytes(compressed[count_offset : count_offset + 4], "little")
        struct.pack_into("<I", compressed, count_offset, size + 6)
        compressed.extend(b"suffix")
        for malformed in (
            bytes(unknown),
            bytes(cycle),
            bytes(overlap),
            bytes(compressed),
            raw + b"private suffix",
            raw[:-1],
        ):
            with (
                self.subTest(size=len(malformed)),
                self.assertRaises(SourceArchiveError),
                mock.patch("tedd_knowledge_graph.source_image_host.inspect_raster") as decoder,
            ):
                try:
                    image_response(malformed, 1)
                finally:
                    decoder.assert_not_called()

    def test_tiff_preserves_every_page_and_applies_orientation_once(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        for order in ("<", ">"):
            for compression in (1, 32773, 8, 5):
                for orientation in (1, 6):
                    with self.subTest(
                        order=order, compression=compression, orientation=orientation
                    ):
                        raw = tiff_pages(
                            order=order, compression=compression, orientation=orientation
                        )
                        inventory = ImageInventory.decode(image_response(raw)["inventory"])
                        self.assertEqual(
                            (inventory.format, [unit.size for unit in inventory.units]),
                            ("tiff", [(2, 1), (1, 2)]),
                        )
                        self.assertEqual(
                            [unit.orientation for unit in inventory.units], [orientation] * 2
                        )
                        for position, colors in (
                            (2, ((0, 255, 0, 255), (255, 255, 255, 255))),
                            (1, ((255, 0, 0, 255), (0, 0, 255, 255))),
                        ):
                            with module.open(
                                io.BytesIO(decoded_png(image_response(raw, position)))
                            ) as image:
                                expected = (
                                    colors[::-1] if orientation == 6 and position == 2 else colors
                                )
                                self.assertEqual(
                                    tuple(
                                        image.getpixel((x, y))
                                        for y in range(image.height)
                                        for x in range(image.width)
                                    ),
                                    expected,
                                )
                                self.assertEqual(image.info, {})

    def test_webp_opaque_partial_frames_allow_a_transparent_animation_background(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        red, blue, clear = (255, 0, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0)
        parts = [riff_chunk(b"VP8X", b"\x02\0\0\0\3\0\0\0\0\0"), riff_chunk(b"ANIM", bytes(6))]
        for left, color in ((0, red), (2, blue)):
            frame = bytearray(webp_literals(2, 1, (color,) * 2))
            frame[4] &= ~0x10
            header = (
                b"".join(value.to_bytes(3, "little") for value in (left // 2, 0, 1, 0, 100))
                + b"\x02"
            )
            parts.append(riff_chunk(b"ANMF", header + riff_chunk(b"VP8L", bytes(frame))))
        body = b"WEBP" + b"".join(parts)
        raw = b"RIFF" + struct.pack("<I", len(body)) + body
        for position, expected in ((1, (red, red, clear, clear)), (2, (red, red, blue, blue))):
            with module.open(io.BytesIO(decoded_png(image_response(raw, position)))) as image:
                self.assertEqual(tuple(image.getpixel((x, 0)) for x in range(4)), expected)

    def test_gif_partial_first_frame_uses_the_declared_global_background(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        palette = bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255)) + bytes(12)
        raw = b"GIF89a" + struct.pack("<HH3B", 4, 2, 0x82, 2, 0) + palette
        raw += (
            b"!\xf9\4\4\x0a\0\0\0" + b"," + struct.pack("<4HB", 1, 0, 1, 1, 0) + b"\3\2\x38\x09\0;"
        )
        green, blue = (0, 255, 0, 255), (0, 0, 255, 255)
        with module.open(io.BytesIO(decoded_png(image_response(raw, 1)))) as image:
            self.assertEqual(
                tuple(image.getpixel((x, y)) for y in range(2) for x in range(4)),
                (green, blue, green, green) + (green,) * 4,
            )

    def test_webp_refuses_raw_alpha_suffix_and_contradictory_alpha_flags(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_containers import _riff_chunks
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        output = io.BytesIO()
        with closing(module.new("RGB", (2, 1), "white")) as image:
            image.save(output, format="WEBP", lossless=False)
        vp8 = next(value for tag, value in _riff_chunks(output.getvalue()[12:]) if tag == b"VP8 ")

        def assembled(alpha: bytes) -> bytes:
            body = (
                b"WEBP"
                + riff_chunk(b"VP8X", b"\x10\0\0\0\1\0\0\0\0\0")
                + riff_chunk(b"ALPH", alpha)
                + riff_chunk(b"VP8 ", vp8)
            )
            return b"RIFF" + struct.pack("<I", len(body)) + body

        valid = assembled(b"\0\0\xff")
        with module.open(io.BytesIO(decoded_png(image_response(valid, 1)))) as image:
            self.assertEqual([image.getpixel((x, 0))[3] for x in range(2)], [0, 255])
        animation = animation_webp(replace_last=True)
        for raw in (
            assembled(b"\0\0"),
            assembled(b"\0\0\xffpassword=SECRET!"),
            animation[:20] + b"\x02" + animation[21:],
        ):
            with self.subTest(size=len(raw)), self.assertRaises(SourceArchiveError):
                image_response(raw, 1)

    def test_ico_dib_masks_preserve_real_transparency_but_refuse_destination_inversion(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])

        def icon(depth: int, pixels: bytes, mask: bytes) -> bytes:
            dib = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, depth, 0, 0, 0, 0, 0, 0) + pixels + mask
            return (
                struct.pack("<HHH", 0, 1, 1)
                + struct.pack("<4B2H2I", 2, 1, 0, 0, 1, depth, len(dib), 22)
                + dib
            )

        rgb = icon(24, bytes((0, 0, 255, 0, 0, 0, 0, 0)), b"\x40\0\0\0")
        rgba = icon(32, bytes((0, 0, 255, 255, 255, 0, 0, 255)), bytes(4))
        for raw, expected in (
            (rgb, [(255, 0, 0, 255), (0, 0, 0, 0)]),
            (rgba, [(255, 0, 0, 255), (0, 0, 255, 255)]),
        ):
            with module.open(io.BytesIO(decoded_png(image_response(raw, 1)))) as image:
                self.assertEqual([image.getpixel((x, 0)) for x in range(2)], expected)
        for raw in (
            icon(24, bytes((0, 0, 255, 255, 0, 0, 0, 0)), b"\x40\0\0\0"),
            icon(32, bytes((0, 0, 255, 0, 255, 0, 0, 0)), bytes(4)),
            icon(8, bytes(8), bytes(4)),
        ):
            with self.assertRaises(SourceArchiveError):
                image_response(raw, 1)

    def test_webp_lossless_alpha_hint_does_not_change_real_pixels(self) -> None:
        from tedd_knowledge_graph.source_image_host import image_response

        pixels = ((255, 0, 0, 255), (0, 0, 0, 0))
        frame = webp_literals(2, 1, pixels)

        def still(content: bytes) -> bytes:
            body = b"WEBP" + riff_chunk(b"VP8L", content)
            return b"RIFF" + struct.pack("<I", len(body)) + body

        hinted = still(frame)
        unhinted = still(frame[:4] + bytes((frame[4] & ~0x10,)) + frame[5:])
        self.assertEqual(
            image_response(hinted, 1)["png_sha256"], image_response(unhinted, 1)["png_sha256"]
        )

        animation = animation_webp(replace_last=True)
        body = bytearray(animation)
        cursor = 0
        while (cursor := animation.find(b"VP8L", cursor)) >= 0:
            body[cursor + 12] &= ~0x10
            cursor += 13
        self.assertEqual(
            image_response(animation, 4)["png_sha256"], image_response(bytes(body), 4)["png_sha256"]
        )
        body[20] &= ~0x10
        with self.assertRaises(SourceArchiveError):
            image_response(bytes(body), 1)

    def test_ico_keeps_same_size_variants_in_original_directory_order(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        red, blue = png_image(pixels=b"\xff\0\0"), png_image(pixels=b"\0\0\xff")
        directory = struct.pack("<HHH", 0, 1, 2)
        directory += struct.pack("<4B2H2I", 1, 1, 0, 0, 1, 24, len(red), 38)
        directory += struct.pack("<4B2H2I", 1, 1, 0, 0, 1, 24, len(blue), 38 + len(red))
        raw = directory + red + blue
        inventory = ImageInventory.decode(image_response(raw)["inventory"])
        self.assertEqual(inventory.format, "ico")
        self.assertEqual(
            [unit.selector for unit in inventory.units], ["variant:root:0", "variant:root:1"]
        )
        for position, rgb in ((2, (0, 0, 255)), (1, (255, 0, 0))):
            with module.open(io.BytesIO(decoded_png(image_response(raw, position)))) as image:
                self.assertEqual(image.getpixel((0, 0)), (*rgb, 255))
                self.assertEqual(image.info, {})
        for changed in (
            raw[:-8],
            raw + b"unreferenced data",
            raw[:18] + struct.pack("<I", 6) + raw[22:],
        ):
            with self.assertRaises(SourceArchiveError):
                image_response(changed, 1)

    def test_webp_literal_frames_keep_blending_background_disposal_and_no_blend_distinct(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        colors = {
            "R": (255, 0, 0, 255),
            "G": (0, 255, 0, 255),
            "B": (0, 0, 255, 255),
            "Y": (255, 255, 0, 255),
            "W": (255, 255, 255, 255),
            ".": (0, 0, 0, 0),
        }
        for replacement in (False, True):
            raw = animation_webp(replace_last=replacement)
            inventory = ImageInventory.decode(image_response(raw)["inventory"])
            self.assertEqual((inventory.format, len(inventory.units)), ("webp", 4))
            for position, row in (
                (4, ".W.." if replacement else "BW.."),
                (1, "RRRR"),
                (3, "BRYY"),
                (2, "BRRR"),
            ):
                with self.subTest(replace=replacement, position=position):
                    with module.open(
                        io.BytesIO(decoded_png(image_response(raw, position)))
                    ) as image:
                        self.assertEqual(
                            [image.getpixel((x, y)) for y in range(2) for x in range(4)],
                            [colors[char] for char in row + "GGGG"],
                        )
                        self.assertEqual(image.info, {})

    def test_webp_refuses_unknown_top_level_and_frame_chunks_before_decoding(self) -> None:
        from tedd_knowledge_graph.source_image_host import image_response

        raw = animation_webp()
        unknown = riff_chunk(b"JUNK", b"unknown application bytes")
        top = raw[:4] + struct.pack("<I", len(raw) - 8 + len(unknown)) + raw[8:] + unknown
        # Preserve every original chunk except the final ANMF payload length.
        last = raw.rfind(b"ANMF")
        size = int.from_bytes(raw[last + 4 : last + 8], "little")
        frame = (
            raw[:4]
            + struct.pack("<I", len(raw) - 8 + len(unknown))
            + raw[8 : last + 4]
            + struct.pack("<I", size + len(unknown))
            + raw[last + 8 :]
            + unknown
        )
        for payload in (top, frame, raw + b"suffix", raw[:-4]):
            with self.assertRaises(SourceArchiveError):
                image_response(payload, 1)

    def test_gif_and_bmp_decode_independent_originals_with_exact_pixel_and_disposal_oracles(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        colors = {
            "R": (255, 0, 0),
            "G": (0, 255, 0),
            "B": (0, 0, 255),
            "Y": (255, 255, 0),
            "W": (255, 255, 255),
            ".": (0, 0, 0),
        }
        raw = animation_gif()
        inventory = ImageInventory.decode(image_response(raw)["inventory"])
        self.assertEqual(
            (inventory.format, [unit.role for unit in inventory.units]), ("gif", ["frame"] * 4)
        )
        for position, expected in (
            (4, "RW..GGGG"),
            (1, "RRRRGGGG"),
            (3, "RRYYGGGG"),
            (2, "BRRRGGGG"),
        ):
            with (
                self.subTest(position=position),
                module.open(io.BytesIO(decoded_png(image_response(raw, position)))) as image,
            ):
                self.assertEqual(image.size, (4, 2))
                self.assertEqual(
                    [image.getpixel((x, y))[:3] for y in range(2) for x in range(4)],
                    [colors[char] for char in expected],
                )
                self.assertTrue(
                    all(image.getpixel((x, y))[3] == 255 for y in range(2) for x in range(4))
                )
        for top_down in (True, False):
            with module.open(
                io.BytesIO(decoded_png(image_response(bitmap_image(top_down=top_down), 1)))
            ) as image:
                self.assertEqual(
                    [image.getpixel((x, y))[:3] for y in range(2) for x in range(2)],
                    [colors[char] for char in "RBGW"],
                )
                self.assertEqual(image.info, {})

    def test_gif_and_bmp_refuse_unknown_extensions_and_uninspected_suffixes(self) -> None:
        from tedd_knowledge_graph.source_image_host import image_response

        gif, bmp = animation_gif(), bitmap_image()
        for raw in (
            gif[:-1] + b"!\xff\x0bUNKNOWNTEST\x01\x01x\0;",
            gif[:-1] + b"!\xfe\x1cpassword=synthetic-fixture-12\0;",
            gif[:-1] + b"!\x01\x0c" + bytes(12) + b"\0;",
            gif + b"suffix",
            bmp[:2] + struct.pack("<I", len(bmp) + 6) + bmp[6:] + b"suffix",
            bmp[:6] + b"\1\0" + bmp[8:],
        ):
            with self.subTest(size=len(raw)), self.assertRaises(SourceArchiveError):
                image_response(raw, 1)

    def test_animation_releases_decoded_frame_before_allocating_preview_pixels(self) -> None:
        from collections.abc import Iterator
        from contextlib import contextmanager
        from types import ModuleType

        from tedd_knowledge_graph import source_image_host
        from tedd_knowledge_graph.source_image_host import RasterImage

        original_owned, original_render = (
            source_image_host._owned_image,
            source_image_host._render_pixels,
        )
        active_decoders = 0

        @contextmanager
        def owned(module: ModuleType, payload: bytes, kind: str) -> Iterator[RasterImage]:
            nonlocal active_decoders
            with original_owned(module, payload, kind) as image:
                active_decoders += 1
                try:
                    yield image
                finally:
                    active_decoders -= 1

        def render(
            image: RasterImage,
            module: ModuleType,
            *,
            size: tuple[int, int] | None,
            rectangle: tuple[int, int, int, int] | None,
        ) -> bytes:
            self.assertEqual(active_decoders, 0, "selected frame still owns full-resolution pixels")
            return original_render(image, module, size=size, rectangle=rectangle)

        for kind, raw, position in (("png", animation_png(), 4), ("gif", animation_gif(), 2)):
            with self.subTest(format=kind):
                with (
                    mock.patch.object(source_image_host, "_owned_image", owned),
                    mock.patch.object(source_image_host, "_render_pixels", render),
                ):
                    source_image_host._render_animation(
                        raw, kind, position, size=(2, 1), rectangle=None, orientation=1
                    )
                self.assertEqual(active_decoders, 0)

    def test_selected_apng_previous_disposal_does_not_copy_an_unused_full_canvas(self) -> None:
        from tedd_knowledge_graph.source_image_host import RasterImage, image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        original_copy = module.Image.copy
        snapshots: list[tuple[int, int]] = []

        def copy(image: RasterImage) -> RasterImage:
            caller = inspect.currentframe()
            assert caller is not None
            try:
                if (
                    caller.f_back is not None
                    and caller.f_back.f_code.co_name == "_render_animation"
                ):
                    snapshots.append(image.size)
                return cast(RasterImage, original_copy(image))
            finally:
                del caller

        raw = animation_png(poster=False)
        with mock.patch.object(module.Image, "copy", copy):
            selected = image_response(raw, 2)
            self.assertEqual(snapshots, [])
            image_response(raw, 3)
            self.assertEqual(snapshots, [(2, 1)])
        self.assertEqual(selected["png_sha256"], image_response(raw, 2)["png_sha256"])

    def test_png_metadata_is_preserved_only_in_original_and_orientation_still_precedes_crop(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        red, green, blue, white, black, yellow = (
            bytes((255, 0, 0)),
            bytes((0, 255, 0)),
            bytes((0, 0, 255)),
            bytes((255, 255, 255)),
            bytes(3),
            bytes((255, 255, 0)),
        )
        raw = png_image(width=3, height=2, pixels=red + green + blue + white + black + yellow)
        oriented = exif_fields(((0x0112, 3, 1, struct.pack("<H", 6)),))
        description = png_chunk(b"tEXt", b"Description\0Synthetic sample")
        original = raw[:33] + png_chunk(b"eXIf", oriented[6:]) + description + raw[33:]
        result = image_response(original, 1, rectangle=(0, 1, 2, 3))
        self.assertEqual(
            ImageInventory.decode(result["inventory"]).metadata_categories, ("description",)
        )
        self.assertEqual(result["orientation"], 6)
        with module.open(io.BytesIO(decoded_png(result))) as image:
            self.assertEqual(image.size, (2, 2))
            self.assertEqual(
                image.tobytes(), b"".join(pixel + b"\xff" for pixel in (black, green, yellow, blue))
            )
            self.assertEqual(image.info, {})
        self.assertNotIn("Synthetic sample", json.dumps(result))
        located = raw[:33] + png_chunk(b"eXIf", exif_location()[6:]) + raw[33:]
        inventory = ImageInventory.decode(image_response(located)["inventory"])
        self.assertEqual(inventory.metadata_categories, ("location",))
        with self.assertRaises(SourceArchiveError):
            inventory.require_metadata_permission("ordinary")
        inventory.require_metadata_permission("restricted")

    def test_jpeg_admission_checks_hidden_app_segments_and_binds_metadata_categories(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        output = io.BytesIO()
        with closing(module.new("RGB", (2, 2), (255, 255, 255))) as image:
            image.save(output, format="JPEG")
        raw = output.getvalue()

        def with_marker(marker: int, content: bytes) -> bytes:
            return (
                raw[:2]
                + bytes((255, marker))
                + struct.pack("!H", len(content) + 2)
                + content
                + raw[2:]
            )

        described = with_marker(0xE1, exif_fields(((0x010E, 2, 17, b"Synthetic sample\0"),)))
        for payload, categories in ((raw, ()), (described, ("description",))):
            value = image_response(payload, 1)
            inventory = ImageInventory.decode(value["inventory"])
            self.assertEqual(
                (inventory.format, inventory.metadata_categories), ("jpeg", categories)
            )
            self.assertNotIn("Synthetic sample", json.dumps(value))
            with module.open(io.BytesIO(decoded_png(value))) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255, 255))
                self.assertEqual(image.info, {})
        for payload in (
            with_marker(0xE5, b"unknown omitted application data"),
            with_marker(0xFE, b"password=synthetic-fixture-123"),
            raw + b"hidden suffix",
        ):
            with self.subTest(size=len(payload)), self.assertRaises(SourceArchiveError):
                image_response(payload, 1)

    def test_apng_separate_poster_never_bleeds_into_transparent_animation(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response

        module = __import__("PIL.Image", fromlist=["Image"])
        clear, red, blue, magenta = (
            bytes(4),
            bytes((255, 0, 0, 255)),
            bytes((0, 0, 255, 255)),
            bytes((255, 0, 255, 255)),
        )

        def compressed(pixels: bytes, width: int) -> bytes:
            return zlib.compress(
                b"".join(
                    b"\0" + pixels[start : start + width * 4]
                    for start in range(0, len(pixels), width * 4)
                )
            )

        raw = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack("!2I5B", 4, 2, 8, 6, 0, 0, 0))
            + png_chunk(b"acTL", struct.pack("!2I", 2, 0))
            + png_chunk(b"IDAT", compressed(magenta * 8, 4))
            + png_chunk(b"fcTL", struct.pack("!5I2H2B", 0, 4, 2, 0, 0, 1, 10, 0, 1))
            + png_chunk(b"fdAT", struct.pack("!I", 1) + compressed(red + clear * 7, 4))
            + png_chunk(b"fcTL", struct.pack("!5I2H2B", 2, 2, 1, 2, 0, 1, 10, 0, 1))
            + png_chunk(b"fdAT", struct.pack("!I", 3) + compressed(blue + clear, 2))
            + png_chunk(b"IEND", b"")
        )
        expected = {1: magenta * 8, 2: red + clear * 7, 3: red + clear + blue + clear * 5}
        for position in (3, 1, 2):
            with self.subTest(position=position):
                with module.open(io.BytesIO(decoded_png(image_response(raw, position)))) as image:
                    self.assertEqual(image.tobytes(), expected[position])

    def test_apng_empty_data_chunk_is_valid_but_empty_frame_is_not(self) -> None:
        from tedd_knowledge_graph.source_image_host import _png_chunks, image_response

        raw = animation_png()
        parts = [raw[:8]]
        inserted = False
        for tag, value in _png_chunks(raw):
            if tag == b"fdAT" and not inserted:
                parts.append(png_chunk(tag, value[:4]))
                inserted = True
            if inserted and tag in (b"fdAT", b"fcTL"):
                value = struct.pack("!I", int.from_bytes(value[:4], "big") + 1) + value[4:]
            parts.append(png_chunk(tag, value))
        expanded = b"".join(parts)
        for position in range(1, 5):
            self.assertEqual(
                image_response(raw, position)["png_sha256"],
                image_response(expanded, position)["png_sha256"],
            )
        no_pixels = raw[:8] + b"".join(
            png_chunk(tag, value[:4] if tag == b"fdAT" else value)
            for tag, value in _png_chunks(raw)
        )
        with self.assertRaises(SourceArchiveError):
            image_response(no_pixels)

    def test_apng_retains_poster_and_every_composited_frame_with_stable_pixel_oracles(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_image_host import image_response
        from tedd_knowledge_graph.source_images import ImageInventory

        module = __import__("PIL.Image", fromlist=["Image"])
        red, green, blue, yellow = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
        for poster in (False, True):
            raw = animation_png(poster=poster)
            expected = ([(red, red)] if poster else []) + [
                (blue, blue),
                (blue, yellow),
                (green, blue),
            ]
            inventory = ImageInventory.decode(image_response(raw)["inventory"])
            self.assertEqual(inventory.format, "apng")
            self.assertEqual(
                [unit.role for unit in inventory.units],
                (["poster"] if poster else []) + ["frame"] * 3,
            )
            for position, row in enumerate(expected, 1):
                with self.subTest(poster=poster, position=position):
                    rendered = image_response(raw, position)
                    self.assertEqual(rendered["inventory"], inventory.as_dict())
                    gaps = rendered["gaps"]
                    assert isinstance(gaps, list)
                    self.assertIn("animation_timing_not_reproduced", gaps)
                    with module.open(io.BytesIO(decoded_png(rendered))) as image:
                        self.assertEqual([image.getpixel((x, 0))[:3] for x in range(2)], list(row))
                        self.assertEqual(image.info, {})

    def test_apng_refuses_missing_frames_broken_sequence_metadata_and_unknown_extensions(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_image_host import image_response

        raw = animation_png()
        control = png_chunk(b"acTL", struct.pack("!2I", 3, 0))
        broken_sequence = png_chunk(
            b"fdAT", struct.pack("!I", 99) + zlib.compress(b"\0\0\0\xff" * 2)
        )
        for payload in (
            raw.replace(control, png_chunk(b"acTL", struct.pack("!2I", 4, 0))),
            raw.replace(control, png_chunk(b"acTL", struct.pack("!2I", 257, 0))),
            raw[:-12] + broken_sequence + raw[-12:],
            raw[:-12] + png_chunk(b"tEXt", b"Unrecognized\0data") + raw[-12:],
            raw[:-12] + png_chunk(b"zzZZ", b"opaque data") + raw[-12:],
            raw + b"trailing data",
        ):
            with self.subTest(size=len(payload)), self.assertRaises(SourceArchiveError):
                image_response(payload)

    def test_office_jpeg_pixel_limit_keeps_its_original_limit_error(self) -> None:
        from tedd_knowledge_graph.source_office_host import inspect_image

        module = __import__("PIL.Image", fromlist=["Image"])
        output = io.BytesIO()
        with closing(module.new("RGB", (5000, 5000), "white")) as image:
            image.save(output, format="JPEG")
        with self.assertRaises(SourceArchiveError) as refused:
            inspect_image(output.getvalue(), "jpeg")
        self.assertEqual(refused.exception.code, "limit_exceeded")

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_LARGE_IMAGE") == "1", "explicit large-image helper check"
    )
    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_108_megapixel_png_admission_overview_and_native_detail_share_the_bound(self) -> None:
        raw = png_image(width=12000, height=9000)
        for args, size in (
            (("--render-unit", "1"), [2048, 1536]),
            (("--render-unit", "1", "--rectangle", "10000,7000,12000,9000"), [2000, 2000]),
        ):
            with self.subTest(args=args):
                result = office_worker(raw, "image", *args)
                self.assertEqual(result.returncode, 0, result.stdout)
                value = json.loads(result.stdout)
                self.assertEqual(value["original_size"], [12000, 9000])
                self.assertEqual(value["output_size"], size)
        oversized = (
            raw[:8]
            + png_chunk(b"IHDR", struct.pack("!2I5B", 16000, 9000, 8, 2, 0, 0, 0))
            + raw[33:]
        )
        refused = office_worker(oversized, "image")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(json.loads(refused.stdout), {"status": "limit_exceeded"})

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_LARGE_IMAGE") == "1", "explicit large-image helper check"
    )
    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_aggregate_pixel_boundary_animation_renders_within_unchanged_worker_limit(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_images import ImageReading

        still = png_image(width=16000, height=8000)
        length = int.from_bytes(still[33:37], "big")
        self.assertEqual(still[37:41], b"IDAT")
        compressed = still[41 : 41 + length]

        def frame(index: int) -> bytes:
            sequence = 0 if index == 0 else index * 2 - 1
            control = png_chunk(
                b"fcTL", struct.pack("!5I2H2B", sequence, 16000, 8000, 0, 0, 1, 10, 0, 0)
            )
            data = (
                png_chunk(b"IDAT", compressed)
                if index == 0
                else png_chunk(b"fdAT", struct.pack("!I", index * 2) + compressed)
            )
            return control + data

        originals = tuple(
            still[:33]
            + png_chunk(b"acTL", struct.pack("!2I", count, 0))
            + b"".join(frame(index) for index in range(count))
            + png_chunk(b"IEND", b"")
            for count in (2, 3)
        )
        raw, excessive = originals
        # The pinned Windows/Linux zlib builds encode the same literal fixture
        # differently. Keep both exact observed originals, not a runtime-derived
        # expected digest. Pixel/aggregate assertions below are unchanged.
        self.assertIn(
            digest(raw),
            (
                "eb82a5a361a64f61f1eba19f8dc6262da55ed4dfb57cb1522c2bb93b99f062bf",
                "75610cc5e8615c654d7408be5e1ee15260fcd4512d419ac4503aa55c483be39c",
            ),
        )
        result = office_worker(raw, "image", "--render-unit", "2")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stderr, b"")
        value = json.loads(result.stdout)
        reading = ImageReading.decode(raw, value)
        self.assertEqual(len(reading.inventory.units), 2)
        self.assertEqual(reading.inventory.pixel_count, 256_000_000)
        self.assertEqual(value["output_size"], [2048, 1024])
        refused = office_worker(excessive, "image", "--render-unit", "1")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout), {"status": "limit_exceeded"})

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_frame_count_boundary_reviews_last_distinct_frame_and_refuses_one_more(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png
        from tedd_knowledge_graph.source_images import ImageReading

        cycle = animation_gif()
        self.assertEqual(cycle[37:40], b"!\xf9\4")
        self.assertEqual(cycle[67:70], b"!\xf9\4")
        raw = cycle[:37] + cycle[37:-1] * 64 + b";"
        result = office_worker(raw, "image", "--render-unit", "256")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stderr, b"")
        value = json.loads(result.stdout)
        reading = ImageReading.decode(raw, value)
        self.assertEqual(len(reading.inventory.units), 256)
        self.assertEqual(reading.inventory.units[-1].selector, "frame:root:255")
        baseline = office_worker(cycle, "image", "--render-unit", "4")
        self.assertEqual(baseline.returncode, 0, baseline.stdout)
        self.assertEqual(reading.png, decoded_png(json.loads(baseline.stdout)))
        too_many = raw[:-1] + cycle[37:67] + b";"
        refused = office_worker(too_many, "image", "--render-unit", "1")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout), {"status": "limit_exceeded"})

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_longest_axis_boundary_preserves_last_native_pixels_and_refuses_overflow(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png

        for dimensions, rectangle, output_size in (
            ((1, 131072), "0,130560,1,131072", (1, 512)),
            ((131072, 1), "130560,0,131072,1", (512, 1)),
        ):
            with self.subTest(dimensions=dimensions):
                raw = png_image(
                    width=dimensions[0], height=dimensions[1], pixels=bytes((30, 90, 150)) * 131072
                )
                result = office_worker(raw, "image", "--render-unit", "1", "--rectangle", rectangle)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stderr, b"")
                value = json.loads(result.stdout)
                self.assertEqual(value["original_size"], list(dimensions))
                png = decoded_png(value)
                module = __import__("PIL.Image", fromlist=["Image"])
                with io.BytesIO(png) as stream, module.open(stream) as image, closing(image):
                    self.assertEqual(image.size, output_size)
                    self.assertEqual(image.info, {})
                    self.assertEqual(image.tobytes(), bytes((30, 90, 150, 255)) * 512)
                oversized = tuple(131073 if value == 131072 else value for value in dimensions)
                changed = (
                    raw[:8]
                    + png_chunk(b"IHDR", struct.pack("!2I5B", *oversized, 8, 2, 0, 0, 0))
                    + raw[33:]
                )
                refused = office_worker(changed, "image")
                self.assertEqual(refused.returncode, 1)
                self.assertEqual(json.loads(refused.stdout), {"status": "limit_exceeded"})

    def test_orientation_precedes_native_crop_with_asymmetric_pixel_oracle(self) -> None:
        from tedd_knowledge_graph.source_image_host import render_raster

        module = __import__("PIL.Image", fromlist=["Image"])
        red, green, blue, white, black, yellow = (
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 255),
            (0, 0, 0),
            (255, 255, 0),
        )
        original = (red, green, blue, white, black, yellow)
        output = io.BytesIO()
        with closing(module.new("RGB", (3, 2))) as image:
            image.putdata(original)
            exif = module.Exif()
            exif[0x0112] = 6
            image.save(output, format="PNG", exif=exif.tobytes())
        # This tests shared rendering mechanics, not admission of PNG EXIF (the
        # first standalone profile still refuses it). Clockwise rotation yields
        # rows (white, red), (black, green), (yellow, blue).
        png = render_raster(output.getvalue(), "png", 0, rectangle=(0, 1, 2, 3))
        with module.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (2, 2))
            self.assertEqual(
                [image.getpixel((x, y))[:3] for y in range(2) for x in range(2)],
                [black, green, yellow, blue],
            )
            self.assertEqual(image.info, {})

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_png_worker_inspects_exact_bytes_and_returns_separate_overview_and_detail(self) -> None:
        from tedd_knowledge_graph.source_contracts import decoded_png, digest
        from tedd_knowledge_graph.source_images import ImageInventory

        raw = png_image(width=4096, height=4)
        original = raw
        inspected = office_worker(raw, "image")
        self.assertEqual(inspected.returncode, 0, inspected.stdout)
        reading = json.loads(inspected.stdout)
        inventory = ImageInventory.decode(reading["inventory"])
        self.assertEqual(inventory.original_sha256, digest(raw))
        self.assertEqual(inventory.format, "png")
        self.assertEqual(inventory.units[0].size, (4096, 4))
        for args, expected, rect in (
            (("--render-unit", "1"), (2048, 2), None),
            (("--render-unit", "1", "--rectangle", "2048,0,4096,4"), (2048, 4), [2048, 0, 4096, 4]),
        ):
            with self.subTest(args=args):
                rendered = office_worker(raw, "image", *args)
                self.assertEqual(rendered.returncode, 0, rendered.stdout)
                value = json.loads(rendered.stdout)
                png = decoded_png(value)
                self.assertEqual(struct.unpack("!2I", png[16:24]), expected)
                self.assertEqual(value["inventory"], reading["inventory"])
                self.assertEqual(value["rectangle"], rect)
                self.assertEqual(value["original_size"], [4096, 4])
                self.assertEqual(value["output_size"], list(expected))
                self.assertNotIn("review_id", value)  # Decoding is not acknowledged review.
        self.assertEqual(raw, original)

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_png_worker_refuses_unknown_metadata_trailing_bytes_and_non_image_input(self) -> None:
        raw = png_image()
        for payload in (
            raw + b"uninspected",
            raw[:-12] + png_chunk(b"tEXt", b"Comment\0synthetic private field") + raw[-12:],
            b"a file named image.png is not image evidence",
        ):
            with self.subTest(bytes=len(payload)):
                result = office_worker(payload, "image")
                self.assertNotEqual(result.returncode, 0)
                value = json.loads(result.stdout)
                self.assertEqual(set(value), {"status"})
                self.assertNotIn(b"synthetic private field", result.stdout)

    def test_shared_decoder_keeps_office_limits_and_accepts_explicit_standalone_limits(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_image_host import inspect_raster
        from tedd_knowledge_graph.source_office_host import _image_metadata, inspect_image

        raw = png_image(width=9000)
        with self.assertRaises(SourceArchiveError):
            inspect_image(raw, "png")
        frames = inspect_raster(
            raw,
            "png",
            metadata=_image_metadata,
            maximum_dimension=131072,
            maximum_frame_pixels=128_000_000,
            maximum_pixels=256_000_000,
        )
        self.assertEqual([(item.size, item.orientation) for item in frames], [((9000, 1), 1)])

    def test_shared_renderer_preserves_native_crop_and_honest_overview_geometry(self) -> None:
        from tedd_knowledge_graph.source_image_host import render_raster

        raw = png_image(width=6, height=3)
        overview = render_raster(raw, "png", 0, size=(4, 2))
        detail = render_raster(raw, "png", 0, rectangle=(2, 1, 6, 3))
        self.assertEqual(struct.unpack("!2I", overview[16:24]), (4, 2))
        self.assertEqual(struct.unpack("!2I", detail[16:24]), (4, 2))
        for options in (
            {"rectangle": (2, 1, 7, 3)},
            {"rectangle": (0, 0, 2, 2), "size": (1, 1)},
            {"size": (3, 3)},
            {"size": (12, 6)},
        ):
            with self.subTest(options=options), self.assertRaises(SourceArchiveError):
                render_raster(raw, "png", 0, **options)


if __name__ == "__main__":
    unittest.main()
