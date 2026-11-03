"""Finite HEIF/AVIF item-container admission before optional pixel decoding.

This guard accounts for all item bytes, properties and relationships. It does
not prove codec validity, authorize preservation, or replace complete decoding.
Unsupported properties and sequence profiles remain explicit capability refusals.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from .source_contracts import SourceArchiveError
from .source_image_metadata import inspect_exif, metadata_text
from .source_office_metadata import inspect_icc


class _Fields:
    def __init__(self, data: bytes) -> None:
        self.data, self.cursor = data, 0

    def take(self, count: int) -> bytes:
        end = self.cursor + count
        if count < 0 or end > len(self.data):
            raise SourceArchiveError()
        value, self.cursor = self.data[self.cursor : end], end
        return value

    def number(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def string(self) -> bytes:
        end = self.data.find(b"\0", self.cursor, self.cursor + 8193)
        if end < 0:
            raise SourceArchiveError()
        value = self.take(end - self.cursor)
        self.take(1)
        return value

    def finish(self) -> None:
        if self.cursor != len(self.data):
            raise SourceArchiveError()


@dataclass(frozen=True, repr=False)
class _Box:
    kind: bytes
    data: bytes
    offset: int


@dataclass(frozen=True, repr=False)
class BmffItem:
    item_id: int
    kind: bytes
    hidden: bool
    data: bytes
    properties: tuple[tuple[bytes, bytes], ...]
    references: tuple[tuple[bytes, tuple[int, ...]], ...]

    @property
    def coded_size(self) -> tuple[int, int]:
        extent = dict(self.properties).get(b"ispe", b"")
        if len(extent) != 12:
            raise SourceArchiveError()
        return int.from_bytes(extent[4:8], "big"), int.from_bytes(extent[8:], "big")

    @property
    def aperture(self) -> tuple[int, int, int, int]:
        width, height = self.coded_size
        clean = dict(self.properties).get(b"clap")
        if clean is None:
            return 0, 0, width, height
        values = tuple(
            int.from_bytes(clean[index : index + 4], "big", signed=index in (16, 24))
            for index in range(0, 32, 4)
        )
        if not all(values[index] > 0 for index in (0, 1, 2, 3, 5, 7)):
            raise SourceArchiveError()
        clean_width, clean_height = Fraction(values[0], values[1]), Fraction(values[2], values[3])
        left = (width - clean_width) / 2 + Fraction(values[4], values[5])
        top = (height - clean_height) / 2 + Fraction(values[6], values[7])
        rectangle = left, top, left + clean_width, top + clean_height
        if any(value.denominator != 1 for value in rectangle):
            raise SourceArchiveError("capability_unavailable")
        if (
            not 0 <= left < left + clean_width <= width
            or not 0 <= top < top + clean_height <= height
        ):
            raise SourceArchiveError()
        return int(rectangle[0]), int(rectangle[1]), int(rectangle[2]), int(rectangle[3])

    @property
    def orientation(self) -> int:
        # MIAF orders clean aperture, counterclockwise rotation, then mirroring.
        transforms = [name for name, _ in self.properties if name in (b"clap", b"irot", b"imir")]
        if transforms != sorted(transforms, key=(b"clap", b"irot", b"imir").index):
            raise SourceArchiveError("capability_unavailable")
        orientation = 1
        values = dict(self.properties)
        for _ in range(values.get(b"irot", b"\0")[0]):
            orientation = (0, 8, 7, 6, 5, 2, 1, 4, 3)[orientation]
        if b"imir" in values:
            table = (
                (0, 4, 3, 2, 1, 8, 7, 6, 5)
                if values[b"imir"] == b"\0"
                else (0, 2, 1, 4, 3, 6, 5, 8, 7)
            )
            orientation = table[orientation]
        return orientation

    @property
    def display_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.aperture
        size = right - left, bottom - top
        return size[::-1] if self.orientation >= 5 else size


@dataclass(frozen=True, repr=False)
class AvifTrack:
    track_id: int
    timescale: int
    durations: tuple[int, ...]
    sync: tuple[bool, ...]
    samples: tuple[BmffItem, ...]
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True, repr=False)
class BmffInventory:
    format: str
    primary_id: int
    items: tuple[BmffItem, ...]
    metadata_categories: tuple[str, ...]
    tracks: tuple[AvifTrack, ...] = ()

    @property
    def top_ids(self) -> tuple[int, ...]:
        children = {
            target
            for item in self.items
            for kind, targets in item.references
            if kind == b"dimg"
            for target in targets
        }
        return tuple(
            item.item_id
            for item in self.items
            if item.kind != b"Exif"
            and not item.hidden
            and item.item_id not in children
            and not any(kind in (b"auxl", b"thmb") for kind, _ in item.references)
        )

    @property
    def coded_pixels(self) -> int:
        return sum(
            item.coded_size[0] * item.coded_size[1] for item in self.items if item.kind != b"Exif"
        ) + sum(
            sample.coded_size[0] * sample.coded_size[1]
            for track in self.tracks
            for sample in track.samples
        )

    def auxiliary_ids(self, parent_id: int) -> tuple[int, ...]:
        return tuple(
            item.item_id
            for item in self.items
            if any(
                kind in (b"auxl", b"thmb") and parent_id in targets
                for kind, targets in item.references
            )
        )


def validate_relationships(items: tuple[BmffItem, ...], primary_id: int) -> None:
    """Reject hidden or contradictory relationships before opening a decoder."""
    indexed = {item.item_id: item for item in items}
    if not items or len(indexed) != len(items) or primary_id not in indexed:
        raise SourceArchiveError()
    image_count = sum(item.kind != b"Exif" for item in items)
    if not 1 <= image_count <= 256:
        raise SourceArchiveError("limit_exceeded")
    inventory = BmffInventory("heif", primary_id, items, ())
    if inventory.coded_pixels > 256_000_000:
        raise SourceArchiveError("limit_exceeded")
    if primary_id not in inventory.top_ids:
        raise SourceArchiveError()
    dependencies: dict[int, set[int]] = {item.item_id: set() for item in items}
    for item in items:
        refs = dict(item.references)
        if len(refs) != len(item.references) or any(
            target not in indexed or target == item.item_id
            for targets in refs.values()
            for target in targets
        ):
            raise SourceArchiveError()
        if item.kind == b"Exif":
            if (
                set(refs) != {b"cdsc"}
                or not refs[b"cdsc"]
                or item.properties
                or any(indexed[target].kind == b"Exif" for target in refs[b"cdsc"])
            ):
                raise SourceArchiveError()
        else:
            _ = item.display_size  # Validate aperture and transform order, not just dimensions.
            if b"cdsc" in refs or any(
                indexed[target].kind == b"Exif" for targets in refs.values() for target in targets
            ):
                raise SourceArchiveError()
            names = {kind for kind, _ in item.properties}
            if (b"auxC" in names) != (b"auxl" in refs) or b"auxl" in refs and b"thmb" in refs:
                raise SourceArchiveError()
            if item.kind in (b"grid", b"iden"):
                targets = refs.get(b"dimg", ())
                if not targets:
                    raise SourceArchiveError()
                if item.kind == b"iden":
                    if item.data or len(targets) != 1:
                        raise SourceArchiveError()
                else:
                    reader = _Fields(item.data)
                    version, flags, rows, columns = (
                        reader.number(1),
                        reader.number(1),
                        reader.number(1) + 1,
                        reader.number(1) + 1,
                    )
                    if version or flags not in (0, 1) or rows * columns != len(targets):
                        raise SourceArchiveError()
                    size = reader.number(4 if flags else 2), reader.number(4 if flags else 2)
                    reader.finish()
                    if size != item.coded_size:
                        raise SourceArchiveError()
                    dimensions = {indexed[target].display_size for target in targets}
                    if len(dimensions) != 1:
                        raise SourceArchiveError()
                    width, height = next(iter(dimensions))
                    if (
                        not width * (columns - 1) < size[0] <= width * columns
                        or not height * (rows - 1) < size[1] <= height * rows
                    ):
                        raise SourceArchiveError()
            elif b"dimg" in refs:
                raise SourceArchiveError()
        for kind, targets in item.references:
            if kind == b"dimg":
                dependencies[item.item_id].update(targets)
            else:
                for target in targets:
                    dependencies[target].add(item.item_id)

    visited: set[int] = set()
    active: set[int] = set()

    def visit(item_id: int) -> None:
        if item_id in active:
            raise SourceArchiveError()
        if item_id in visited:
            return
        active.add(item_id)
        for child in dependencies[item_id]:
            visit(child)
        active.remove(item_id)
        visited.add(item_id)

    for item_id in inventory.top_ids:
        visit(item_id)
    if visited != set(indexed):
        raise SourceArchiveError("capture_incomplete")


def _property(kind: bytes, data: bytes) -> None:
    reader = _Fields(data)
    if kind == b"ispe":
        if reader.number(4):
            raise SourceArchiveError()
        width, height = reader.number(4), reader.number(4)
        if not 0 < width <= 131072 or not 0 < height <= 131072 or width * height > 128_000_000:
            raise SourceArchiveError("limit_exceeded")
    elif kind == b"pixi":
        if reader.number(4):
            raise SourceArchiveError()
        count = reader.number(1)
        if not 1 <= count <= 4 or any(value not in (8, 10, 12, 16) for value in reader.take(count)):
            raise SourceArchiveError()
    elif kind == b"av1C":
        header = reader.take(4)
        if header[0] != 0x81 or header[3] & 0xE0:
            raise SourceArchiveError()
        # Additional configuration OBUs need their own complete admission.
    elif kind == b"hvcC":
        header = reader.take(23)
        if header[0] != 1 or header[21] & 3 == 2:
            raise SourceArchiveError()
        for _ in range(header[22]):
            nal_type = reader.number(1) & 63
            if nal_type not in (32, 33, 34):
                raise SourceArchiveError("privacy_rejected")
            count = reader.number(2)
            if not 1 <= count <= 256:
                raise SourceArchiveError("limit_exceeded")
            for _ in range(count):
                nal = reader.take(reader.number(2))
                if (
                    len(nal) < 3
                    or nal[0] & 0x81
                    or not nal[1] & 7
                    or nal[1] & 0xF8
                    or (nal[0] >> 1) & 63 != nal_type
                ):
                    raise SourceArchiveError()
    elif kind == b"colr":
        color = reader.take(4)
        if color == b"nclx":
            primaries, transfer, matrix = reader.number(2), reader.number(2), reader.number(2)
            if (
                primaries not in (*range(1, 13), 22)
                or not 1 <= transfer <= 18
                or not 0 <= matrix <= 14
                or reader.number(1) & 127
            ):
                raise SourceArchiveError("capability_unavailable")
        elif color in (b"rICC", b"prof"):
            inspect_icc(reader.take(len(data) - 4))
        else:
            raise SourceArchiveError("capability_unavailable")
    elif kind == b"clap":
        values = tuple(reader.number(4) for _ in range(8))
        if not all(values[index] for index in (0, 1, 2, 3, 5, 7)):
            raise SourceArchiveError()
    elif kind in (b"irot", b"imir"):
        if reader.number(1) > (3 if kind == b"irot" else 1):
            raise SourceArchiveError()
    elif kind == b"pasp":
        horizontal, vertical = reader.number(4), reader.number(4)
        if not horizontal or horizontal != vertical:
            raise SourceArchiveError("capability_unavailable")
    elif kind == b"auxC":
        if reader.number(4):
            raise SourceArchiveError()
        auxiliary = reader.string()
        if auxiliary not in (
            b"urn:mpeg:hevc:2015:auxid:1",
            b"urn:mpeg:hevc:2015:auxid:2",
            b"urn:mpeg:mpegB:cicp:systems:auxiliary:alpha",
            b"urn:mpeg:mpegB:cicp:systems:auxiliary:depth",
            b"urn:com:apple:photo:2020:aux:hdrgainmap",
        ):
            raise SourceArchiveError("capability_unavailable")
    else:
        raise SourceArchiveError("privacy_rejected")
    reader.finish()


def _coded_image(item: BmffItem) -> tuple[bytes, ...]:
    """Account for compressed-unit envelopes, refusing uninterpreted metadata.

    Pixel codecs still validate syntax and reconstruct every image. This guard
    prevents user-data SEI or metadata/padding OBUs disappearing from a decoder's
    metadata summary while their original bytes would otherwise be retained.
    """
    reader = _Fields(item.data)
    count = 0
    first_slice_seen = False
    sequence_headers: list[bytes] = []
    while reader.cursor < len(item.data):
        count += 1
        if count > 4096:
            raise SourceArchiveError("limit_exceeded")
        if item.kind == b"hvc1":
            config = dict(item.properties)[b"hvcC"]
            nal = reader.take(reader.number((config[21] & 3) + 1))
            if len(nal) < 3 or nal[0] & 0x81 or nal[1] & 0xF8 or not nal[1] & 7:
                raise SourceArchiveError()
            if (nal[0] >> 1) & 63 not in (*range(0, 10), *range(16, 22)):
                raise SourceArchiveError("privacy_rejected")
            # The first RBSP bit follows the two-byte NAL header, before an
            # emulation-prevention byte can occur. A still item has one picture,
            # possibly multiple slices—not an ignored second first-picture flag.
            first_slice = bool(nal[2] & 128)
            if first_slice == first_slice_seen:
                raise SourceArchiveError()
            first_slice_seen = True
        elif item.kind == b"av01":
            header = reader.number(1)
            if header & 0x81 or not header & 2:
                raise SourceArchiveError()
            kind = header >> 3 & 15
            if header & 4 and reader.number(1):
                raise SourceArchiveError("capability_unavailable")
            length = 0
            for shift in range(0, 56, 7):
                value = reader.number(1)
                length |= (value & 127) << shift
                if not value & 128:
                    break
            else:
                raise SourceArchiveError()
            data = reader.take(length)
            if kind == 2:
                if data:
                    raise SourceArchiveError()
            elif kind not in (1, 3, 4, 6, 7) or not data:
                raise SourceArchiveError("privacy_rejected")
            if kind == 1:
                sequence_headers.append(data)
    return tuple(sequence_headers)


def _media_coverage(payload: bytes, media: tuple[_Box, ...], spans: set[tuple[int, int]]) -> None:
    """All retained media belongs to exact declared extents, allowing alignment only."""
    previous_end = 0
    for start, end in sorted(spans):
        if start < previous_end or not any(
            block.offset <= start < end <= block.offset + len(block.data) for block in media
        ):
            raise SourceArchiveError()
        previous_end = end
    for block in media:
        cursor = block.offset
        for start, end in sorted(
            span for span in spans if block.offset <= span[0] < block.offset + len(block.data)
        ):
            if start - cursor > 3 or any(payload[cursor:start]):
                raise SourceArchiveError("privacy_rejected")
            cursor = end
        end = block.offset + len(block.data)
        if end - cursor > 3 or any(payload[cursor:end]):
            raise SourceArchiveError("privacy_rejected")


Boxes = Callable[[bytes, int], tuple[_Box, ...]]
_MATRIX = struct.pack("!9I", 65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30)


def _children(
    box: _Box, boxes: Boxes, required: set[bytes], optional: set[bytes] | None = None
) -> dict[bytes, _Box]:
    children = boxes(box.data, box.offset)
    found = {child.kind: child for child in children}
    if len(found) != len(children) or not required <= found.keys():
        raise SourceArchiveError()
    if found.keys() - required - (optional or set()):
        raise SourceArchiveError("capability_unavailable")
    return found


def _versioned(data: bytes, flags: tuple[int, ...] = (0,)) -> tuple[_Fields, int]:
    reader = _Fields(data)
    version = reader.number(1)
    if version not in (0, 1) or reader.number(3) not in flags:
        raise SourceArchiveError("capability_unavailable")
    return reader, 8 if version else 4


def _count(reader: _Fields) -> int:
    count = reader.number(4)
    if not 1 <= count <= 256:
        raise SourceArchiveError("limit_exceeded")
    return count


def _table(data: bytes) -> _Fields:
    reader = _Fields(data)
    if reader.number(4):
        raise SourceArchiveError("capability_unavailable")
    return reader


def _timestamps(reader: _Fields, width: int, categories: set[str]) -> None:
    creation, modification = reader.number(width), reader.number(width)
    # Match PNG tIME and EXIF dates: nonzero times are descriptive metadata,
    # never authenticated authorship. Zero/unspecified values add no category.
    if creation or modification:
        categories.add("description")


def _clock(data: bytes, categories: set[str]) -> tuple[_Fields, int, int, int]:
    reader, width = _versioned(data)
    _timestamps(reader, width, categories)
    scale, duration = reader.number(4), reader.number(width)
    if not scale or not duration:
        raise SourceArchiveError()
    return reader, scale, duration, (1 << (width * 8)) - 1


def _movie_clock(data: bytes, categories: set[str]) -> tuple[int, int, int]:
    reader, scale, duration, indefinite = _clock(data, categories)
    if (
        reader.number(4) != 65536
        or reader.number(2) != 256
        or any(reader.take(10))
        or reader.take(36) != _MATRIX
        or any(reader.take(24))
        or not reader.number(4)
    ):
        raise SourceArchiveError("capability_unavailable")
    reader.finish()
    return scale, duration, indefinite


def _track_header(data: bytes, categories: set[str]) -> tuple[int, int, tuple[int, int], int]:
    reader, width = _versioned(data, (1, 3, 7))
    _timestamps(reader, width, categories)
    identifier = reader.number(4)
    if not identifier or reader.number(4):
        raise SourceArchiveError()
    duration = reader.number(width)
    if any(reader.take(16)) or reader.take(36) != _MATRIX:
        raise SourceArchiveError("capability_unavailable")
    width_fixed, height_fixed = reader.number(4), reader.number(4)
    if width_fixed & 65535 or height_fixed & 65535:
        raise SourceArchiveError("capability_unavailable")
    size = width_fixed >> 16, height_fixed >> 16
    reader.finish()
    return identifier, duration, size, (1 << (width * 8)) - 1


def _handler(data: bytes, categories: set[str]) -> None:
    reader = _Fields(data)
    if reader.number(8) or reader.take(4) != b"pict" or any(reader.take(12)):
        raise SourceArchiveError("capability_unavailable")
    name = reader.string()
    if name:
        metadata_text(name)
        categories.add("description")
    reader.finish()


def _description(
    box: _Box, boxes: Boxes, size: tuple[int, int], categories: set[str]
) -> tuple[tuple[bytes, bytes], ...]:
    table = _table(box.data)
    if table.number(4) != 1:
        raise SourceArchiveError("capability_unavailable")
    entries = boxes(box.data[8:], box.offset + 8)
    if len(entries) != 1 or entries[0].kind != b"av01":
        raise SourceArchiveError("capability_unavailable")
    entry = entries[0]
    reader = _Fields(entry.data)
    if any(reader.take(6)) or reader.number(2) != 1 or any(reader.take(16)):
        raise SourceArchiveError("privacy_rejected")
    dimensions = reader.number(2), reader.number(2)
    if dimensions != size:
        raise SourceArchiveError("capability_unavailable")
    if (
        reader.number(4) != 72 << 16
        or reader.number(4) != 72 << 16
        or reader.number(4)
        or reader.number(2) != 1
    ):
        raise SourceArchiveError("capability_unavailable")
    name = reader.take(32)
    if name[0] > 31 or any(name[name[0] + 1 :]):
        raise SourceArchiveError("privacy_rejected")
    if name[0]:
        metadata_text(name[1 : name[0] + 1])
        categories.add("description")
    if reader.number(2) != 24 or reader.number(2) != 65535:
        raise SourceArchiveError("capability_unavailable")
    properties = _children(
        _Box(b"av01", entry.data[78:], entry.offset + 78),
        boxes,
        {b"av1C", b"colr"},
        {b"ccst", b"pasp"},
    )
    values = [(b"ispe", bytes(4) + struct.pack("!2I", *size))]
    for kind, prop in properties.items():
        if kind == b"ccst":
            # Complete known unrestricted libavif constraint profile. Other
            # constraint promises need comparison with decoded headers first.
            if prop.data != bytes.fromhex("000000007c000000"):
                raise SourceArchiveError("capability_unavailable")
        else:
            _property(kind, prop.data)
            values.append((kind, prop.data))
    _property(*values[0])
    return tuple(values)


def _samples(
    table: dict[bytes, _Box], properties: tuple[tuple[bytes, bytes], ...], payload: bytes
) -> tuple[tuple[BmffItem, ...], tuple[tuple[int, int], ...], tuple[int, ...], tuple[bool, ...]]:
    timing = _table(table[b"stts"].data)
    durations: list[int] = []
    for _ in range(_count(timing)):
        count, duration = _count(timing), timing.number(4)
        if not duration or len(durations) + count > 256:
            raise SourceArchiveError("limit_exceeded")
        durations.extend([duration] * count)
    timing.finish()
    sizes_reader = _table(table[b"stsz"].data)
    uniform, count = sizes_reader.number(4), _count(sizes_reader)
    sizes = tuple(uniform or sizes_reader.number(4) for _ in range(count))
    sizes_reader.finish()
    if count != len(durations) or any(not 0 < size <= 64 * 1024**2 for size in sizes):
        raise SourceArchiveError()
    sync = set(range(1, count + 1))
    if b"stss" in table:
        sync_reader = _table(table[b"stss"].data)
        entries = tuple(sync_reader.number(4) for _ in range(_count(sync_reader)))
        sync_reader.finish()
        if (
            entries != tuple(sorted(set(entries)))
            or not entries
            or entries[0] != 1
            or entries[-1] > count
        ):
            raise SourceArchiveError()
        sync = set(entries)
    offset_kinds = tuple(kind for kind in (b"stco", b"co64") if kind in table)
    if len(offset_kinds) != 1:
        raise SourceArchiveError()
    offsets_reader = _table(table[offset_kinds[0]].data)
    offsets = tuple(
        offsets_reader.number(8 if offset_kinds[0] == b"co64" else 4)
        for _ in range(_count(offsets_reader))
    )
    offsets_reader.finish()
    chunks_reader = _table(table[b"stsc"].data)
    chunks = tuple(
        (chunks_reader.number(4), _count(chunks_reader), chunks_reader.number(4))
        for _ in range(_count(chunks_reader))
    )
    chunks_reader.finish()
    starts = tuple(row[0] for row in chunks)
    if (
        starts != tuple(sorted(set(starts)))
        or starts[0] != 1
        or starts[-1] > len(offsets)
        or any(row[2] != 1 for row in chunks)
    ):
        raise SourceArchiveError()
    spans: list[tuple[int, int]] = []
    row_index = 0
    for chunk_index, offset in enumerate(offsets, 1):
        if row_index + 1 < len(chunks) and chunk_index == chunks[row_index + 1][0]:
            row_index += 1
        for _ in range(chunks[row_index][1]):
            if len(spans) >= count:
                raise SourceArchiveError()
            end = offset + sizes[len(spans)]
            if not 0 < offset < end <= len(payload):
                raise SourceArchiveError()
            spans.append((offset, end))
            offset = end
    if len(spans) != count:
        raise SourceArchiveError("capture_incomplete")
    samples: list[BmffItem] = []
    sequence_header: bytes | None = None
    for index, (start, end) in enumerate(spans):
        sample = BmffItem(index + 1, b"av01", False, payload[start:end], properties, ())
        headers = _coded_image(sample)
        if len(headers) > 1 or index == 0 and not headers:
            raise SourceArchiveError()
        if headers:
            if sequence_header is not None and headers[0] != sequence_header:
                raise SourceArchiveError()
            sequence_header = headers[0]
        samples.append(sample)
    return (
        tuple(samples),
        tuple(spans),
        tuple(durations),
        tuple(index in sync for index in range(1, count + 1)),
    )


def inspect_tracks(
    movie: _Box, boxes: Boxes, payload: bytes
) -> tuple[tuple[AvifTrack, ...], set[str]]:
    """Admit all boxes and every sample before any decoder sees the original."""
    categories: set[str] = set()
    content = _children(movie, boxes, {b"mvhd", b"trak"})
    movie_scale, movie_duration, movie_indefinite = _movie_clock(content[b"mvhd"].data, categories)
    track = _children(content[b"trak"], boxes, {b"tkhd", b"mdia"}, {b"edts"})
    identifier, track_duration, size, track_indefinite = _track_header(
        track[b"tkhd"].data, categories
    )
    media = _children(track[b"mdia"], boxes, {b"mdhd", b"hdlr", b"minf"})
    reader, scale, duration, indefinite = _clock(media[b"mdhd"].data, categories)
    language = reader.number(2)
    if language and (
        language & 32768 or any(not 1 <= (language >> shift & 31) <= 26 for shift in (0, 5, 10))
    ):
        raise SourceArchiveError()
    if reader.number(2) or duration == indefinite:
        raise SourceArchiveError()
    reader.finish()
    _handler(media[b"hdlr"].data, categories)
    info = _children(media[b"minf"], boxes, {b"vmhd", b"dinf", b"stbl"})
    if info[b"vmhd"].data != b"\0\0\0\1" + bytes(8):
        raise SourceArchiveError("capability_unavailable")
    references = _children(info[b"dinf"], boxes, {b"dref"})[b"dref"]
    if references.data[:8] != bytes(4) + b"\0\0\0\1":
        raise SourceArchiveError("privacy_rejected")
    urls = boxes(references.data[8:], references.offset + 8)
    if len(urls) != 1 or urls[0].kind != b"url " or urls[0].data != b"\0\0\0\1":
        raise SourceArchiveError("privacy_rejected")
    tables = _children(
        info[b"stbl"], boxes, {b"stsd", b"stts", b"stsc", b"stsz"}, {b"stco", b"co64", b"stss"}
    )
    properties = _description(tables[b"stsd"], boxes, size, categories)
    samples, spans, durations, sync = _samples(tables, properties, payload)
    if size[0] * size[1] * len(samples) > 256_000_000:
        raise SourceArchiveError("limit_exceeded")
    if sum(durations) != duration:
        raise SourceArchiveError()
    timeline = Fraction(duration, scale)
    if (
        track_duration != track_indefinite
        and Fraction(track_duration, movie_scale) != timeline
        or movie_duration != movie_indefinite
        and Fraction(movie_duration, movie_scale) != timeline
    ):
        raise SourceArchiveError("capability_unavailable")
    if b"edts" in track:
        edits = _children(track[b"edts"], boxes, {b"elst"})[b"elst"]
        reader, width = _versioned(edits.data, (0, 1))
        if (
            reader.number(4) != 1
            or Fraction(reader.number(width), movie_scale) != timeline
            or reader.number(width)
            or reader.number(4) != 65536
        ):
            raise SourceArchiveError("capability_unavailable")
        reader.finish()
    return (AvifTrack(identifier, scale, durations, sync, samples, spans),), categories


def inspect_bmff(payload: bytes) -> BmffInventory:
    """Screen exact item envelopes; callers must independently decode all units."""
    if not isinstance(payload, bytes) or not 16 <= len(payload) <= 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    box_count = 0

    def boxes(data: bytes, base: int = 0) -> tuple[_Box, ...]:
        nonlocal box_count
        reader, found = _Fields(data), []
        while reader.cursor < len(data):
            start = reader.cursor
            size, kind = reader.number(4), reader.take(4)
            header = 8
            if size == 1:
                size, header = reader.number(8), 16
            elif size == 0:
                size = len(data) - start
            if size < header:
                raise SourceArchiveError()
            box_count += 1
            if box_count > 4096:
                raise SourceArchiveError("limit_exceeded")
            found.append(_Box(kind, reader.take(size - header), base + start + header))
        return tuple(found)

    def singleton(values: tuple[_Box, ...], kind: bytes) -> _Box:
        found = tuple(value for value in values if value.kind == kind)
        if len(found) != 1:
            raise SourceArchiveError()
        return found[0]

    top = boxes(payload)
    if (
        not top
        or top[0].kind != b"ftyp"
        or any(value.kind not in (b"ftyp", b"meta", b"mdat", b"moov") for value in top)
    ):
        raise SourceArchiveError("capability_unavailable")
    brand = singleton(top, b"ftyp").data
    if len(brand) < 12 or len(brand) % 4:
        raise SourceArchiveError()
    brands = (brand[:4], *(brand[index : index + 4] for index in range(8, len(brand), 4)))
    if any(
        value
        not in (
            b"avif",
            b"avis",
            b"msf1",
            b"iso8",
            b"mif1",
            b"miaf",
            b"MA1A",
            b"MA1B",
            b"heic",
            b"heix",
        )
        for value in brands
    ):
        raise SourceArchiveError("capability_unavailable")
    kind = "avif" if b"avif" in brands or b"avis" in brands else "heif"
    media = tuple(value for value in top if value.kind == b"mdat")
    tracks: tuple[AvifTrack, ...] = ()
    categories: set[str] = set()
    movies = tuple(value for value in top if value.kind == b"moov")
    if movies or b"avis" in brands:
        if kind != "avif" or b"avis" not in brands or len(movies) != 1:
            raise SourceArchiveError("capability_unavailable")
        tracks, track_categories = inspect_tracks(movies[0], boxes, payload)
        categories.update(track_categories)
    spans = {span for track in tracks for span in track.spans}
    if tracks and not any(value.kind == b"meta" for value in top):
        if b"avif" in brands or b"mif1" in brands:
            raise SourceArchiveError()
        _media_coverage(payload, media, spans)
        return BmffInventory(kind, 0, (), tuple(sorted(categories)), tracks)
    meta = singleton(top, b"meta")
    if meta.data[:4] != bytes(4):
        raise SourceArchiveError()
    children = boxes(meta.data[4:], meta.offset + 4)
    if any(
        value.kind not in (b"hdlr", b"pitm", b"iinf", b"iloc", b"iprp", b"iref", b"idat")
        for value in children
    ):
        raise SourceArchiveError("capability_unavailable")
    handler = _Fields(singleton(children, b"hdlr").data)
    if handler.number(8) or handler.take(4) != b"pict" or any(handler.take(12)):
        raise SourceArchiveError()
    name = handler.string()
    if name:
        metadata_text(name)
        categories.add("description")
    handler.finish()
    primary = _Fields(singleton(children, b"pitm").data)
    version = primary.number(1)
    if version not in (0, 1) or primary.number(3):
        raise SourceArchiveError()
    primary_id = primary.number(2 if version == 0 else 4)
    primary.finish()

    info = singleton(children, b"iinf")
    fields = _Fields(info.data)
    version = fields.number(1)
    if version not in (0, 1) or fields.number(3):
        raise SourceArchiveError()
    count = fields.number(2 if version == 0 else 4)
    if not 1 <= count <= 4096:
        raise SourceArchiveError("limit_exceeded")
    entries = boxes(info.data[fields.cursor :], info.offset + fields.cursor)
    if len(entries) != count:
        raise SourceArchiveError()
    descriptions: dict[int, tuple[bytes, bool]] = {}
    for entry in entries:
        if entry.kind != b"infe":
            raise SourceArchiveError()
        reader = _Fields(entry.data)
        version, flags = reader.number(1), reader.number(3)
        if version not in (2, 3) or flags not in (0, 1):
            raise SourceArchiveError()
        item_id = reader.number(2 if version == 2 else 4)
        if not item_id or item_id in descriptions or reader.number(2):
            raise SourceArchiveError()
        item_kind, name = reader.take(4), reader.string()
        if item_kind not in (b"av01", b"hvc1", b"grid", b"iden", b"Exif"):
            raise SourceArchiveError("privacy_rejected")
        if name:
            metadata_text(name)
            categories.add("description")
        reader.finish()
        descriptions[item_id] = (item_kind, bool(flags))
    if primary_id not in descriptions or descriptions[primary_id][0] == b"Exif":
        raise SourceArchiveError()

    internal = tuple(value for value in children if value.kind == b"idat")
    if len(internal) > 1:
        raise SourceArchiveError()
    locations: dict[int, bytes] = {}
    reader = _Fields(singleton(children, b"iloc").data)
    version = reader.number(1)
    if version not in (0, 1, 2) or reader.number(3):
        raise SourceArchiveError()
    sizes = reader.number(1), reader.number(1)
    offset_size, length_size, base_size, index_size = (
        sizes[0] >> 4,
        sizes[0] & 15,
        sizes[1] >> 4,
        sizes[1] & 15,
    )
    if (
        offset_size not in (0, 4, 8)
        or length_size not in (4, 8)
        or base_size not in (0, 4, 8)
        or index_size
    ):
        raise SourceArchiveError("capability_unavailable")
    count = reader.number(2 if version < 2 else 4)
    if count != len(descriptions):
        raise SourceArchiveError()
    extent_total = 0
    for _ in range(count):
        item_id = reader.number(2 if version < 2 else 4)
        construction = reader.number(2) if version else 0
        if (
            item_id not in descriptions
            or item_id in locations
            or construction not in (0, 1)
            or reader.number(2)
        ):
            raise SourceArchiveError()
        base, extent_count = reader.number(base_size), reader.number(2)
        extent_total += extent_count
        if extent_total > 4096:
            raise SourceArchiveError("limit_exceeded")
        pieces: list[bytes] = []
        for _ in range(extent_count):
            start, length = base + reader.number(offset_size), reader.number(length_size)
            if construction:
                if not internal:
                    raise SourceArchiveError()
                start += internal[0].offset
            end = start + length
            if not length or not any(
                value.offset <= start < end <= value.offset + len(value.data)
                for value in (internal if construction else media)
            ):
                raise SourceArchiveError()
            span = start, end
            if any(
                span != previous and max(start, previous[0]) < min(end, previous[1])
                for previous in spans
            ):
                raise SourceArchiveError()
            spans.add(span)
            pieces.append(payload[start:end])
        locations[item_id] = b"".join(pieces)
    reader.finish()
    _media_coverage(payload, (*media, *internal), spans)

    properties = singleton(children, b"iprp")
    groups = boxes(properties.data, properties.offset)
    if any(value.kind not in (b"ipco", b"ipma") for value in groups):
        raise SourceArchiveError()
    property_container = singleton(groups, b"ipco")
    values = boxes(property_container.data, property_container.offset)
    for value in values:
        _property(value.kind, value.data)
    associations: dict[int, tuple[tuple[bytes, bytes], ...]] = {}
    reader = _Fields(singleton(groups, b"ipma").data)
    version, flags = reader.number(1), reader.number(3)
    if version not in (0, 1) or flags not in (0, 1):
        raise SourceArchiveError()
    count = reader.number(4)
    if count > len(descriptions):
        raise SourceArchiveError()
    for _ in range(count):
        item_id = reader.number(2 if version == 0 else 4)
        if item_id not in descriptions or item_id in associations:
            raise SourceArchiveError()
        selected: list[tuple[bytes, bytes]] = []
        for _ in range(reader.number(1)):
            index = reader.number(2 if flags else 1) & (0x7FFF if flags else 0x7F)
            if not 1 <= index <= len(values):
                raise SourceArchiveError()
            value = values[index - 1]
            if any(kind == value.kind for kind, _ in selected):
                raise SourceArchiveError()
            selected.append((value.kind, value.data))
        associations[item_id] = tuple(selected)
    reader.finish()

    references: dict[int, list[tuple[bytes, tuple[int, ...]]]] = {}
    reference_groups = tuple(value for value in children if value.kind == b"iref")
    if len(reference_groups) > 1:
        raise SourceArchiveError()
    for group in reference_groups:
        reader = _Fields(group.data)
        version = reader.number(1)
        if version not in (0, 1) or reader.number(3):
            raise SourceArchiveError()
        for reference in boxes(group.data[4:], group.offset + 4):
            if reference.kind not in (b"auxl", b"thmb", b"dimg", b"cdsc"):
                raise SourceArchiveError("capability_unavailable")
            reader = _Fields(reference.data)
            item_id = reader.number(2 if version == 0 else 4)
            count = reader.number(2)
            if item_id not in descriptions or not 1 <= count <= 4096:
                raise SourceArchiveError()
            targets = tuple(reader.number(2 if version == 0 else 4) for _ in range(count))
            if len(set(targets)) != count or any(
                target not in descriptions or target == item_id for target in targets
            ):
                raise SourceArchiveError()
            reader.finish()
            references.setdefault(item_id, []).append((reference.kind, targets))

    items: list[BmffItem] = []
    for item_id, (item_kind, hidden) in descriptions.items():
        data, item_properties = locations[item_id], associations.get(item_id, ())
        names = {name for name, _ in item_properties}
        if item_kind == b"Exif":
            if len(data) < 4:
                raise SourceArchiveError()
            skip = int.from_bytes(data[:4], "big")
            if data[4 : 4 + skip] not in (b"", b"Exif\0\0"):
                raise SourceArchiveError()
            categories.update(inspect_exif(data[4 + skip :]))
        else:
            if b"ispe" not in names or not data and item_kind != b"iden":
                raise SourceArchiveError()
            if (
                item_kind == b"av01"
                and b"av1C" not in names
                or item_kind == b"hvc1"
                and b"hvcC" not in names
            ):
                raise SourceArchiveError()
        item = BmffItem(
            item_id,
            item_kind,
            hidden,
            data,
            item_properties,
            tuple(references.get(item_id, ())),
        )
        if item_kind in (b"hvc1", b"av01"):
            _coded_image(item)
        items.append(item)
    if tracks and (len(items) != 1 or items[0].kind != b"av01" or items[0].references):
        raise SourceArchiveError("capability_unavailable")
    result = BmffInventory(kind, primary_id, tuple(items), tuple(sorted(categories)), tracks)
    if tracks and (
        len(items) + sum(len(track.samples) for track in tracks) > 256
        or result.coded_pixels > 256_000_000
    ):
        raise SourceArchiveError("limit_exceeded")
    validate_relationships(result.items, result.primary_id)
    return result


def avif_item_bytes(
    inventory: BmffInventory, item_id: int, *, reconstruction: bool = False
) -> bytes:
    """Build a decoder-only view of an already admitted item and its dependencies.

    Original retention and source identity never use these bytes. Every original
    item must be admitted first and decoded separately; selecting a root here is
    not proof of full-file coverage or compressed-payload validity. Item names
    and EXIF are omitted from this ephemeral display derivative, never stripped
    to make an inadmissible original acceptable.
    """
    if inventory.format != "avif" or type(item_id) is not int or type(reconstruction) is not bool:
        raise SourceArchiveError("capability_unavailable")
    validate_relationships(inventory.items, inventory.primary_id)
    indexed = {item.item_id: item for item in inventory.items}
    if item_id not in indexed or indexed[item_id].kind == b"Exif":
        raise SourceArchiveError()
    included: set[int] = set()
    pending = [item_id]
    while pending:
        current = pending.pop()
        if current in included:
            continue
        included.add(current)
        for kind, targets in indexed[current].references:
            if kind == b"dimg":
                pending.extend(targets)
        pending.extend(
            item.item_id
            for item in inventory.items
            if any(kind == b"auxl" and current in targets for kind, targets in item.references)
        )
    selected = tuple(item for item in inventory.items if item.item_id in included)
    if any(item.kind not in (b"av01", b"grid", b"iden") for item in selected):
        raise SourceArchiveError("capability_unavailable")

    def box(kind: bytes, value: bytes) -> bytes:
        return struct.pack("!I", len(value) + 8) + kind + value

    properties: list[tuple[bytes, bytes]] = []
    associations, entries, relations = [], [], []
    for item in selected:
        keys: list[int] = []
        for kind, value in item.properties:
            if item.item_id == item_id and (
                kind == b"auxC" or reconstruction and kind in (b"clap", b"irot", b"imir")
            ):
                continue
            entry = kind, value
            if entry not in properties:
                properties.append(entry)
            keys.append(properties.index(entry) + 1)
        if len(keys) > 255 or len(properties) > 32767:
            raise SourceArchiveError("limit_exceeded")
        associations.append(
            struct.pack("!IB", item.item_id, len(keys))
            + b"".join(struct.pack("!H", key | 0x8000) for key in keys)
        )
        entries.append(
            box(
                b"infe",
                struct.pack("!IIH", 0x03000000 | int(item.item_id != item_id), item.item_id, 0)
                + item.kind
                + b"\0",
            )
        )
        for kind, targets in item.references:
            retained = tuple(target for target in targets if target in included)
            if (
                kind not in (b"dimg", b"auxl")
                or not retained
                or item.item_id == item_id
                and kind == b"auxl"
            ):
                continue
            if kind == b"dimg" and retained != targets:
                raise SourceArchiveError()
            relations.append(
                box(
                    kind,
                    struct.pack("!IH", item.item_id, len(retained))
                    + b"".join(struct.pack("!I", target) for target in retained),
                )
            )
    file_type = box(b"ftyp", b"avif\0\0\0\0avifmif1miaf")
    handler = box(b"hdlr", bytes(8) + b"pict" + bytes(13))
    primary = box(b"pitm", b"\1\0\0\0" + struct.pack("!I", item_id))
    info = box(b"iinf", b"\1\0\0\0" + struct.pack("!I", len(entries)) + b"".join(entries))
    reference = box(b"iref", b"\1\0\0\0" + b"".join(relations)) if relations else b""
    association = box(
        b"ipma", b"\1\0\0\1" + struct.pack("!I", len(associations)) + b"".join(associations)
    )
    property_box = box(
        b"iprp",
        box(b"ipco", b"".join(box(kind, value) for kind, value in properties)) + association,
    )

    def meta(offset: int) -> bytes:
        locations: list[bytes] = []
        for item in selected:
            locations.append(struct.pack("!IHHH", item.item_id, 0, 0, int(bool(item.data))))
            if item.data:
                locations.append(struct.pack("!II", offset, len(item.data)))
            offset += len(item.data)
        locations_box = box(
            b"iloc", b"\2\0\0\0\x44\0" + struct.pack("!I", len(selected)) + b"".join(locations)
        )
        return box(
            b"meta", bytes(4) + handler + primary + info + locations_box + property_box + reference
        )

    media_size = sum(len(item.data) for item in selected)
    initial_meta = meta(0)
    if len(file_type) + len(initial_meta) + 8 + media_size > 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    return (
        file_type
        + meta(len(file_type) + len(initial_meta) + 8)
        + box(b"mdat", b"".join(item.data for item in selected))
    )
