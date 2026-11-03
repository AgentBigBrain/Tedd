"""Shared optional raster decoding/rendering for disposable source workers.

Container and metadata policies belong to the calling source format. These
helpers do not acquire files, authorize retention, or establish visual review.
Pillow is loaded only when a qualified worker actually needs pixel operations.
"""

from __future__ import annotations

import base64
import importlib
import io
import struct
import warnings
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from .source_contracts import SourceArchiveError, digest, positive
from .source_image_containers import AnimationFrame
from .source_images import ImageInventory, ImageUnit
from .source_review import overview_size

if TYPE_CHECKING:
    from .source_av1_host import Av1Validator
    from .source_image_bmff import BmffInventory
    from .source_svg_host import SvgRenderer


class RasterImage(Protocol):
    format: str | None
    mode: str
    size: tuple[int, int]
    info: dict[str, object]

    def seek(self, frame: int) -> None: ...
    def verify(self) -> None: ...
    def load(self) -> object: ...
    def getexif(self) -> Mapping[int, object]: ...
    def convert(self, mode: str) -> RasterImage: ...
    def point(self, lut: list[int], mode: str) -> RasterImage: ...
    def putalpha(self, alpha: RasterImage) -> None: ...
    def crop(self, rectangle: tuple[int, int, int, int]) -> RasterImage: ...
    def resize(self, size: tuple[int, int], resample: int) -> RasterImage: ...
    def tobytes(self) -> bytes: ...
    def save(self, stream: io.BytesIO, *, format: str) -> None: ...
    def close(self) -> None: ...
    def copy(self) -> RasterImage: ...
    def getchannel(self, channel: str) -> RasterImage: ...
    def getextrema(self) -> object: ...
    def transpose(self, method: int) -> RasterImage: ...
    def paste(
        self, value: RasterImage | tuple[int, int, int, int], box: tuple[int, ...] | None = None
    ) -> None: ...
    def alpha_composite(self, image: RasterImage, dest: tuple[int, int]) -> None: ...


class RasterOpen(Protocol):
    def __call__(
        self, stream: io.BytesIO, *, formats: list[str]
    ) -> AbstractContextManager[RasterImage]: ...


class RasterFromBytes(Protocol):
    def __call__(self, mode: str, size: tuple[int, int], data: bytes) -> RasterImage: ...


class RasterNew(Protocol):
    def __call__(
        self, mode: str, size: tuple[int, int], color: tuple[int, int, int, int]
    ) -> RasterImage: ...


class RasterSettings(Protocol):
    MAX_IMAGE_PIXELS: int | None


@contextmanager
def _decoder_limits(module: ModuleType, maximum_pixels: int) -> Iterator[None]:
    """One explicit pixel policy for decode and render; restore worker globals."""
    positive(maximum_pixels, maximum=128_000_000)
    settings = cast(RasterSettings, module)
    old_limit = settings.MAX_IMAGE_PIXELS
    bomb_error = cast(type[Exception], module.DecompressionBombError)
    bomb_warning = cast(type[Warning], module.DecompressionBombWarning)
    settings.MAX_IMAGE_PIXELS = maximum_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            yield
    except (bomb_error, bomb_warning) as error:
        raise SourceArchiveError("limit_exceeded") from error
    finally:
        settings.MAX_IMAGE_PIXELS = old_limit


@dataclass(frozen=True, repr=False)
class RasterFrame:
    size: tuple[int, int]
    orientation: int


@contextmanager
def _owned_image(module: ModuleType, payload: bytes, kind: str) -> Iterator[RasterImage]:
    """Close both the input stream and decoded pixels, including failed reads."""
    opener = cast(RasterOpen, module.open)
    # Pillow ImageFile.__exit__ closes file handles but not its pixel core.
    # Explicit pixel ownership also releases buffers retained by a traceback.
    with (
        io.BytesIO(payload) as stream,
        opener(stream, formats=[kind]) as image,
        closing(image),
    ):
        yield image


def _png_chunks(payload: bytes) -> Iterator[tuple[bytes, bytes]]:
    """One bounded CRC-checked envelope walk for Office PNG and standalone APNG."""
    if not 1 <= len(payload) <= 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourceArchiveError()
    cursor, count = 8, 0
    while cursor + 12 <= len(payload):
        count += 1
        size = int.from_bytes(payload[cursor : cursor + 4], "big")
        end = cursor + 12 + size
        if count > 4096 or end > len(payload):
            raise SourceArchiveError("limit_exceeded")
        tag = payload[cursor + 4 : cursor + 8]
        if zlib.crc32(payload[cursor + 4 : end - 4]) != int.from_bytes(
            payload[end - 4 : end], "big"
        ):
            raise SourceArchiveError()
        yield tag, payload[cursor + 8 : end - 4]
        cursor = end
        if tag == b"IEND":
            if cursor != len(payload) or size:
                raise SourceArchiveError()
            return
    raise SourceArchiveError()


def inspect_png(
    payload: bytes, *, maximum_dimension: int = 131072, maximum_pixels: int = 128_000_000
) -> None:
    """Inspect the restricted PNG envelope without broadening Office's policy."""
    positive(maximum_dimension, maximum=131072)
    positive(maximum_pixels, maximum=128_000_000)
    fixed: dict[bytes, bytes] = {}
    compressed: list[bytes] = []
    sizes = {b"IHDR": 13, b"IEND": 0, b"sRGB": 1, b"gAMA": 4, b"pHYs": 9, b"cHRM": 32}
    for chunks, (tag, value) in enumerate(_png_chunks(payload), 1):
        size = len(value)
        if tag not in sizes and tag not in (b"PLTE", b"tRNS", b"IDAT"):
            raise SourceArchiveError("capability_unavailable")
        if (
            chunks == 1
            and tag != b"IHDR"
            or tag in fixed
            or tag in sizes
            and size != sizes[tag]
            or compressed
            and tag not in (b"IDAT", b"IEND")
        ):
            raise SourceArchiveError()
        if tag == b"IDAT":
            compressed.append(value)
        else:
            fixed[tag] = value
    if b"IEND" not in fixed or not compressed:
        raise SourceArchiveError()
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        "!2I5B", fixed[b"IHDR"]
    )
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
        raise SourceArchiveError()
    if (
        not 0 < width <= maximum_dimension
        or not 0 < height <= maximum_dimension
        or width * height > maximum_pixels
    ):
        raise SourceArchiveError("limit_exceeded")
    palette = fixed.get(b"PLTE", b"")
    if (b"PLTE" in fixed and (color != 3 or not 0 < len(palette) <= 768 or len(palette) % 3)) or (
        color == 3 and not 0 < len(palette) // 3 <= 2**depth
    ):
        raise SourceArchiveError()
    if b"tRNS" in fixed:
        transparency = fixed[b"tRNS"]
        if not (
            color == 0
            and len(transparency) == 2
            or color == 2
            and len(transparency) == 6
            or color == 3
            and 0 < len(transparency) <= len(palette) // 3
        ):
            raise SourceArchiveError()
    if (b"sRGB" in fixed and fixed[b"sRGB"][0] > 3) or (b"pHYs" in fixed and fixed[b"pHYs"][8] > 1):
        raise SourceArchiveError()
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = (
        (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
        if interlace
        else ((0, 0, 1, 1),)
    )
    rows: list[tuple[int, int]] = []
    for x, y, dx, dy in passes:
        columns = max(0, (width - x + dx - 1) // dx)
        count = max(0, (height - y + dy - 1) // dy)
        if columns and count:
            rows.append((1 + (columns * channels * depth + 7) // 8, count))
    expected = sum(size * count for size, count in rows)
    inflater = zlib.decompressobj()
    try:
        pixels = inflater.decompress(b"".join(compressed), expected + 1)
    except zlib.error as error:
        raise SourceArchiveError() from error
    if (
        len(pixels) != expected
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise SourceArchiveError()
    cursor = 0
    for size, count in rows:
        if any(pixels[start] > 4 for start in range(cursor, cursor + size * count, size)):
            raise SourceArchiveError()
        cursor += size * count


def _png_sequence(
    payload: bytes, frames: list[AnimationFrame] | None = None, categories: set[str] | None = None
) -> tuple[str, ...]:
    """Validate every PNG/APNG stream before bounded original-frame composition.

    Small reconstructed PNG envelopes are used only to validate each compressed
    stream with the existing parser. They are never retained or rendered as the
    original. Composition uses only the admitted original frame data and controls.
    """
    header = b""
    prefix: list[tuple[bytes, bytes]] = []
    data: list[bytes] = []
    dimensions = (0, 0)
    canvas = (0, 0)
    roles: list[str] = []
    sequence, declared, controls = 0, 0, 0
    offset, disposal, blend = (0, 0), 0, 0
    idat_seen, frame_active, idat_closed = False, False, False
    metadata_bytes = 0
    metadata_seen: set[bytes] = set()

    def finish() -> None:
        if not data:
            raise SourceArchiveError("capture_incomplete")

        def chunk(tag: bytes, value: bytes) -> bytes:
            return (
                struct.pack("!I", len(value))
                + tag
                + value
                + struct.pack("!I", zlib.crc32(tag + value))
            )

        frame = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I", *dimensions) + header[8:])
        frame += b"".join(chunk(tag, value) for tag, value in prefix)
        frame += b"".join(chunk(b"IDAT", value) for value in data) + chunk(b"IEND", b"")
        inspect_png(frame)
        if frames is not None:
            frames.append(AnimationFrame(frame, roles[-1], offset, disposal, blend))
        data.clear()

    for number, (tag, value) in enumerate(_png_chunks(payload)):
        if number == 0:
            if tag != b"IHDR" or len(value) != 13:
                raise SourceArchiveError()
            header = value
            canvas = struct.unpack("!2I", header[:8])
            dimensions = canvas
            continue
        if tag == b"acTL":
            if declared or idat_seen or len(value) != 8:
                raise SourceArchiveError()
            declared = positive(int.from_bytes(value[:4], "big"), maximum=256)
            if canvas[0] * canvas[1] * declared > 256_000_000:
                raise SourceArchiveError("limit_exceeded")
        elif tag == b"fcTL":
            if not declared or len(value) != 26:
                raise SourceArchiveError()
            serial, width, height, x, y, _, _, next_disposal, next_blend = struct.unpack(
                "!5I2H2B", value
            )
            if (
                serial != sequence
                or next_disposal > 2
                or next_blend > 1
                or not (0 < width <= canvas[0] - x and 0 < height <= canvas[1] - y)
            ):
                raise SourceArchiveError()
            if frame_active or idat_seen:
                finish()
            if not idat_seen and (width, height, x, y) != (*canvas, 0, 0):
                raise SourceArchiveError()
            controls += 1
            if controls > declared:
                raise SourceArchiveError("capture_incomplete")
            roles.append("frame")
            dimensions = (width, height)
            offset, disposal, blend = (x, y), next_disposal, next_blend
            sequence += 1
            frame_active = True
            idat_closed = idat_seen
        elif tag == b"IDAT":
            if idat_closed:
                raise SourceArchiveError()
            if not idat_seen and not frame_active:
                roles.append("poster" if declared else "still")
            idat_seen = True
            data.append(value)
        elif tag == b"fdAT":
            if (
                not idat_seen
                or not frame_active
                or not idat_closed
                or len(value) < 4
                or int.from_bytes(value[:4], "big") != sequence
            ):
                raise SourceArchiveError()
            sequence += 1
            data.append(value[4:])
        elif tag == b"IEND":
            finish()
        elif tag in {b"PLTE", b"tRNS", b"sRGB", b"gAMA", b"pHYs", b"cHRM"}:
            if idat_seen or any(previous == tag for previous, _ in prefix):
                raise SourceArchiveError()
            prefix.append((tag, value))
        elif tag in {b"eXIf", b"iCCP", b"tEXt", b"zTXt", b"iTXt", b"tIME"}:
            from .source_image_metadata import inspect_png_metadata

            metadata_bytes += len(value)
            if metadata_bytes > 262144:
                raise SourceArchiveError("limit_exceeded")
            if tag in {b"eXIf", b"iCCP", b"tIME"} and tag in metadata_seen:
                raise SourceArchiveError()
            metadata_seen.add(tag)
            admitted = inspect_png_metadata(tag, value)
            if categories is not None:
                categories.update(admitted)
            idat_closed = idat_closed or idat_seen
        else:
            raise SourceArchiveError("capability_unavailable")
    if controls != declared or not idat_seen or not roles:
        raise SourceArchiveError("capture_incomplete")
    if len(roles) > 256 or canvas[0] * canvas[1] * len(roles) > 256_000_000:
        raise SourceArchiveError("limit_exceeded")
    return tuple(roles)


def inspect_raster(
    payload: bytes,
    kind: str,
    *,
    metadata: Callable[[dict[str, object], str], None],
    maximum_dimension: int = 8192,
    maximum_frame_pixels: int = 16_000_000,
    maximum_pixels: int = 64_000_000,
    inspect_pixels: Callable[[RasterImage], None] | None = None,
) -> tuple[RasterFrame, ...]:
    """Force every frame with explicit aggregate limits and caller metadata policy.

    Defaults preserve the narrower Office limits. The standalone-image caller
    must opt into its separately qualified ceiling; this is not admission alone.
    """
    positive(maximum_dimension, maximum=131072)
    positive(maximum_frame_pixels, maximum=128_000_000)
    positive(maximum_pixels, maximum=256_000_000)
    try:
        module = importlib.import_module("PIL.Image")
    except ModuleNotFoundError as error:
        raise SourceArchiveError("capability_unavailable") from error
    expected = kind.upper()
    module.init()
    if expected not in module.OPEN:
        raise SourceArchiveError("capability_unavailable")

    def bounded_size(image: RasterImage) -> tuple[int, int]:
        width, height = image.size
        if (
            not 0 < width <= maximum_dimension
            or not 0 < height <= maximum_dimension
            or width * height > maximum_frame_pixels
        ):
            raise SourceArchiveError("limit_exceeded")
        return width, height

    try:
        with _decoder_limits(module, maximum_frame_pixels):
            with _owned_image(module, payload, expected) as image:
                if image.format != expected:
                    raise SourceArchiveError()
                bounded_size(image)
                image.verify()
            with _owned_image(module, payload, expected) as image:
                frames = getattr(image, "n_frames", 1)
                if type(frames) is not int or not 1 <= frames <= 256:
                    raise SourceArchiveError("limit_exceeded")
                pixels = 0
                result: list[RasterFrame] = []
                for frame in range(frames):
                    image.seek(frame)
                    size = bounded_size(image)
                    pixels += size[0] * size[1]
                    if pixels > maximum_pixels:
                        raise SourceArchiveError("limit_exceeded")
                    metadata(image.info, kind)
                    image.load()
                    if inspect_pixels is not None:
                        inspect_pixels(image)
                    metadata(image.info, kind)
                    orientation = positive(image.getexif().get(0x0112, 1), maximum=8)
                    result.append(RasterFrame(size, orientation))
                return tuple(result)
    except SourceArchiveError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Warning) as error:
        raise SourceArchiveError() from error


@contextmanager
def _rgba(image: RasterImage, *, normalize_gray16: bool) -> Iterator[RasterImage]:
    """Explicit 8-bit derivative mapping; never clip 16-bit midtones to white."""
    if normalize_gray16 and image.mode in ("I;16", "I;16L", "I;16B", "I;16N"):
        with ExitStack() as output:
            with (
                closing(image.convert("I")) as integers,
                closing(integers.point([value >> 8 for value in range(65536)], "L")) as gray,
            ):
                # A PNG16 transparency key applies before quantization; nearby
                # values sharing an8-bit shade must remain opaque.
                transparency = image.info.get("transparency")
                gray.info.pop("transparency", None)
                rgba = output.enter_context(closing(gray.convert("RGBA")))
                if transparency is not None:
                    if type(transparency) is not int or not 0 <= transparency <= 65535:
                        raise SourceArchiveError()
                    with closing(
                        integers.point(
                            [0 if value == transparency else 255 for value in range(65536)], "L"
                        )
                    ) as alpha:
                        rgba.putalpha(alpha)
            # Release full-resolution integer/gray planes before caller resize.
            yield rgba
        return
    with closing(image.convert("RGBA")) as rgba:
        yield rgba


def _render_pixels(
    image: RasterImage,
    module: ModuleType,
    *,
    size: tuple[int, int] | None,
    rectangle: tuple[int, int, int, int] | None,
    normalize_gray16: bool = False,
) -> bytes:
    """One metadata-free pixel/output path after orientation or composition."""
    if size is not None and rectangle is not None:
        raise SourceArchiveError()
    frombytes = cast(RasterFromBytes, module.frombytes)
    with io.BytesIO() as stream, ExitStack() as frames:
        width, height = image.size
        selected = image
        if rectangle is not None:
            if (
                type(rectangle) is not tuple
                or len(rectangle) != 4
                or any(type(value) is not int for value in rectangle)
            ):
                raise SourceArchiveError()
            left, top, right, bottom = rectangle
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise SourceArchiveError()
            if right - left > 2048 or bottom - top > 2048:
                raise SourceArchiveError("limit_exceeded")
            selected = frames.enter_context(closing(image.crop(rectangle)))
        elif size is not None:
            if type(size) is not tuple or len(size) != 2:
                raise SourceArchiveError()
            out_width, out_height = (positive(value, maximum=2048) for value in size)
            expected = overview_size(image.size, max(size))
            if out_width > width or out_height > height or size != expected:
                raise SourceArchiveError()
            resizable = image
            if (
                normalize_gray16
                and "transparency" in image.info
                and image.mode in ("I;16", "I;16L", "I;16B", "I;16N")
            ):
                # Keyed transparency is a property of exact native samples,
                # never of intermediate gray values invented by a filter.
                resizable = frames.enter_context(_rgba(image, normalize_gray16=True))
            selected = frames.enter_context(closing(resizable.resize(size, 1)))
        with _rgba(selected, normalize_gray16=normalize_gray16) as rgba:
            with closing(frombytes("RGBA", rgba.size, rgba.tobytes())) as clean:
                clean.save(stream, format="PNG")
        return stream.getvalue()


@contextmanager
def _oriented_pixels(image: RasterImage, orientation: int) -> Iterator[RasterImage]:
    """Apply one validated EXIF-equivalent transform to untransformed pixels."""
    positive(orientation, maximum=8)
    if orientation == 1:
        yield image
    else:
        transform = {2: 0, 3: 3, 4: 1, 5: 5, 6: 4, 7: 6, 8: 2}[orientation]
        with closing(image.transpose(transform)) as oriented:
            yield oriented


def render_avif_item(
    payload: bytes,
    item_id: int,
    *,
    reconstruction: bool = False,
    size: tuple[int, int] | None = None,
    rectangle: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Render one decoder derivative; not full-source or retention admission.

    The shared image path runs the strict native gate before calling this pixel
    primitive. Calling it alone is not admission. Pillow exposes AVIF orientation
    metadata but returns untransformed samples and does not apply clean aperture. Decode
    without root transforms, then crop and orient once in the shared pixel path.
    Reconstruction deliberately retains the full decoded plane instead.
    """
    from .source_image_bmff import avif_item_bytes, inspect_bmff

    inventory = inspect_bmff(payload)
    wrapper = avif_item_bytes(inventory, item_id, reconstruction=True)
    item = next(part for part in inventory.items if part.item_id == item_id)
    if type(reconstruction) is not bool:
        raise SourceArchiveError()
    try:
        module = importlib.import_module("PIL.Image")
    except ModuleNotFoundError as error:
        raise SourceArchiveError("capability_unavailable") from error
    try:
        with (
            _decoder_limits(module, 128_000_000),
            _owned_image(module, wrapper, "AVIF") as image,
            ExitStack() as frames,
        ):
            if (
                image.size != item.coded_size
                or getattr(image, "n_frames", 1) != 1
                or image.getexif().get(274, 1) != 1
            ):
                raise SourceArchiveError()
            image.load()
            selected = image
            if not reconstruction and item.aperture != (0, 0, *item.coded_size):
                selected = frames.enter_context(closing(image.crop(item.aperture)))
            with _oriented_pixels(selected, 1 if reconstruction else item.orientation) as oriented:
                return _render_pixels(oriented, module, size=size, rectangle=rectangle)
    except SourceArchiveError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Warning) as error:
        raise SourceArchiveError() from error


def render_heif_item(
    payload: bytes,
    item_id: int,
    *,
    reconstruction: bool = False,
    size: tuple[int, int] | None = None,
    rectangle: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Render native HEIF planes without reapplying container transformations.

    Every item is decoded before returning the selected derivative. Structure,
    declared metadata and native warnings are checked, but successful decoding
    does not establish consumption of every encoded byte in the exact original.
    """
    from .source_image_bmff import inspect_bmff
    from .source_image_heif import decode_heif

    positive(item_id, maximum=2**32 - 1)
    inventory = inspect_bmff(payload)
    if inventory.format != "heif" or not any(
        item.item_id == item_id and item.kind != b"Exif" for item in inventory.items
    ):
        raise SourceArchiveError()
    if type(reconstruction) is not bool:
        raise SourceArchiveError()
    try:
        module = importlib.import_module("PIL.Image")
    except ModuleNotFoundError as error:
        raise SourceArchiveError("capability_unavailable") from error
    frombytes = cast(RasterFromBytes, module.frombytes)
    rendered: bytes | None = None

    def consume(identifier: int, dimensions: tuple[int, int], rgba: bytes) -> None:
        nonlocal rendered
        if identifier == item_id:
            with closing(frombytes("RGBA", dimensions, rgba)) as image:
                rendered = _render_pixels(image, module, size=size, rectangle=rectangle)

    try:
        with _decoder_limits(module, 128_000_000):
            decode_heif(payload, inventory, consume, reconstruction=reconstruction)
    except SourceArchiveError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Warning) as error:
        raise SourceArchiveError() from error
    if rendered is None:
        raise SourceArchiveError("capture_incomplete")
    return rendered


def _render_animation(
    payload: bytes,
    kind: str,
    position: int,
    *,
    size: tuple[int, int] | None,
    rectangle: tuple[int, int, int, int] | None,
    orientation: int,
) -> bytes:
    """Compose on the format-defined canvas, including partial initial frames.

    Pillow 12.3's APNG seek path copies the default poster into its OVER canvas.
    Use public pixel primitives on fully admitted original frame streams instead
    of patching Pillow or relying on private decoder state. The original remains
    unchanged and every constituent was admitted before this selection.
    """
    parts: list[AnimationFrame] = []
    if kind == "png":
        _png_sequence(payload, parts)
        canvas_size = struct.unpack("!2I", payload[16:24])
    elif kind == "gif":
        from .source_image_containers import inspect_gif

        inspect_gif(payload, parts)
        canvas_size = struct.unpack_from("<2H", payload, 6)
    else:
        raise SourceArchiveError()
    positive(position, maximum=len(parts))
    module = importlib.import_module("PIL.Image")
    create = cast(RasterNew, module.new)
    try:
        with (
            _decoder_limits(module, 128_000_000),
            closing(create("RGBA", canvas_size, parts[0].background)) as canvas,
        ):
            for number, part in enumerate(parts, 1):
                if part.role == "poster" and number != position:
                    continue
                with (
                    _owned_image(module, part.payload, kind.upper()) as decoded,
                    _rgba(decoded, normalize_gray16=True) as frame,
                    ExitStack() as temporary,
                ):
                    previous = (
                        temporary.enter_context(closing(canvas.copy()))
                        if part.disposal == 2 and number < position
                        else None
                    )
                    if part.blend:
                        canvas.alpha_composite(frame, dest=part.offset)
                    else:
                        canvas.paste(frame, part.offset)
                    if number < position and part.disposal == 1:
                        left, top = part.offset
                        width, height = frame.size
                        canvas.paste(part.background, (left, top, left + width, top + height))
                    elif previous is not None:
                        canvas.paste(previous)
                if number == position:
                    # Release decoded/converted frame planes before allocating
                    # the final preview. At the admitted pixel boundary those
                    # otherwise-overlapping buffers exceed the shared job cap.
                    with _oriented_pixels(canvas, orientation) as oriented:
                        return _render_pixels(oriented, module, size=size, rectangle=rectangle)
    except SourceArchiveError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Warning) as error:
        raise SourceArchiveError() from error
    raise SourceArchiveError("capture_incomplete")


def render_raster(
    payload: bytes,
    kind: str,
    frame: int,
    *,
    size: tuple[int, int] | None = None,
    rectangle: tuple[int, int, int, int] | None = None,
    maximum_frame_pixels: int = 16_000_000,
    normalize_gray16: bool = False,
) -> bytes:
    """Render previously admitted bytes to metadata-free pixels.

    No selection means the existing Office native-size behavior. An explicit
    overview preserves aspect; an oriented native-detail rectangle never scales.
    The caller enforces its own output-byte and transport limits.
    """
    if type(frame) is not int or not 0 <= frame < 256 or size is not None and rectangle is not None:
        raise SourceArchiveError()
    try:
        module = importlib.import_module("PIL.Image")
        transpose = cast(
            Callable[[RasterImage], RasterImage],
            importlib.import_module("PIL.ImageOps").exif_transpose,
        )
    except ModuleNotFoundError as error:
        raise SourceArchiveError("capability_unavailable") from error
    try:
        with _decoder_limits(module, maximum_frame_pixels):
            with _owned_image(module, payload, kind.upper()) as image:
                image.seek(frame)
                with closing(transpose(image)) as oriented:
                    return _render_pixels(
                        oriented,
                        module,
                        size=size,
                        rectangle=rectangle,
                        normalize_gray16=normalize_gray16,
                    )
    except SourceArchiveError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Warning) as error:
        raise SourceArchiveError() from error


def _standalone_metadata(info: dict[str, object], kind: str) -> None:
    # Container admission already screened these exact fields. Decoder summaries
    # are only a second guard; they are not proof of complete metadata coverage.
    allowed = {
        "gamma",
        "chromaticity",
        "srgb",
        "dpi",
        "transparency",
        "loop",
        "default_image",
        "duration",
        "disposal",
        "blend",
        "bbox",
        "exif",
        "icc_profile",
        "Title",
        "Description",
        "Copyright",
        "Author",
        "Software",
        "Creation Time",
    }
    if kind == "jpeg":
        allowed = {
            "jfif",
            "jfif_unit",
            "jfif_density",
            "jfif_version",
            "dpi",
            "exif",
            "comment",
            "icc_profile",
            "adobe",
            "adobe_transform",
            "progressive",
            "progression",
        }
    elif kind == "gif":
        allowed = {
            "version",
            "background",
            "transparency",
            "duration",
            "loop",
            "extension",
            "comment",
        }
    elif kind == "bmp":
        allowed = {"dpi", "compression"}
    elif kind == "webp":
        allowed = {"loop", "background", "duration", "timestamp", "exif", "icc_profile"}
    elif kind == "ico":
        allowed = {"sizes"}
    elif kind == "tiff":
        allowed = {"compression", "dpi", "resolution", "icc_profile"}
    if info.keys() - allowed:
        raise SourceArchiveError("privacy_rejected")


def _svg_image(payload: bytes, renderer: SvgRenderer | None) -> tuple[ImageInventory, bytes]:
    if renderer is None:
        raise SourceArchiveError("capability_unavailable")
    svg, png = renderer.render(payload)
    frames = inspect_raster(
        png,
        "png",
        metadata=_standalone_metadata,
        maximum_dimension=131072,
        maximum_frame_pixels=128_000_000,
        maximum_pixels=256_000_000,
    )
    if len(frames) != 1 or frames[0].size != svg.size:
        raise SourceArchiveError()
    return ImageInventory(
        svg.source_sha256, "svg", (ImageUnit("still", 0, svg.size),), svg.metadata_categories
    ), png


def _svg_candidate(payload: bytes) -> bool:
    return payload.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")


def _bmff_units(inventory: BmffInventory) -> tuple[tuple[ImageUnit, int], ...]:
    """One stable slot per retained image, including clipped tiles and auxiliaries.

    Item ordinals follow the original directory, not the primary-image choice.
    A shared auxiliary has one display slot under its first source-listed owner;
    the immutable container retains all relationships. Hidden coded items are
    still presented for review, never silently omitted from retained originals.
    """
    items = tuple(item for item in inventory.items if item.kind != b"Exif")
    parents = {
        item.item_id: tuple(
            target
            for kind, targets in item.references
            if kind in (b"auxl", b"thmb")
            for target in targets
        )
        for item in items
    }
    roots = {
        item.item_id: index
        for index, item in enumerate(item for item in items if not parents[item.item_id])
    }
    result: list[tuple[ImageUnit, int]] = []
    # Directory ordinals for ordinary items must be contiguous; auxiliary item
    # identifiers remain exact native identifiers rather than ordinal aliases.
    for item in (
        *[item for item in items if not parents[item.item_id]],
        *[item for item in items if parents[item.item_id]],
    ):
        owner = item.item_id
        for _ in range(len(items)):
            if owner in roots:
                break
            owner = parents[owner][0]
        else:
            raise SourceArchiveError()
        auxiliary = bool(parents[item.item_id])
        left, top, right, bottom = item.aperture
        result.append(
            (
                ImageUnit(
                    "auxiliary" if auxiliary else "item",
                    item.item_id if auxiliary else roots[item.item_id],
                    (right - left, bottom - top),
                    item.orientation,
                    roots[owner] if auxiliary else None,
                    item.coded_size if item.aperture != (0, 0, *item.coded_size) else None,
                ),
                item.item_id,
            )
        )
    return tuple(result)


def _bmff_image(
    payload: bytes, validator: Av1Validator | None
) -> tuple[ImageInventory, tuple[int, ...]]:
    from .source_image_bmff import inspect_bmff
    from .source_image_heif import decode_heif

    bmff = inspect_bmff(payload)
    if bmff.format == "heif":
        # The pinned binding decodes every item and refuses native warnings.
        # No pixels or metadata values leave inventory-only admission.
        decode_heif(payload, bmff, lambda _identifier, _size, _rgba: None)
    else:
        if validator is None:
            raise SourceArchiveError("capability_unavailable")
        bmff = validator.inspect(payload)
    units = _bmff_units(bmff)
    # The admitted finite sequence profile has one color track and at most one
    # independent still item. Combined ordinals keep both declared views; even
    # a poster sharing the first frame's coded bytes is not silently omitted.
    frames = tuple(
        ImageUnit("frame", len(units) + index, sample.coded_size)
        for track in bmff.tracks
        for index, sample in enumerate(track.samples)
    )
    return ImageInventory(
        digest(payload),
        bmff.format,
        (*tuple(unit for unit, _ in units), *frames),
        bmff.metadata_categories,
    ), tuple(identifier for _, identifier in units)


def read_image(
    payload: bytes, *, svg: SvgRenderer | None = None, av1: Av1Validator | None = None
) -> ImageInventory:
    """Identify exact image bytes and verify every retained presentation unit."""
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    if _svg_candidate(payload):
        return _svg_image(payload, svg)[0]
    if payload[4:8] == b"ftyp":
        return _bmff_image(payload, av1)[0]
    categories: frozenset[str] = frozenset()
    decoded_payload = payload
    require_opaque = False
    if payload[:4] in (b"II*\0", b"MM\0*"):
        from .source_image_tiff import inspect_tiff

        tiff_units, categories = inspect_tiff(payload)
        frames = inspect_raster(
            payload,
            "tiff",
            metadata=_standalone_metadata,
            maximum_dimension=131072,
            maximum_frame_pixels=128_000_000,
            maximum_pixels=256_000_000,
        )
        # The pinned TIFF reader orients on load and removes the orientation tag.
        # Keep native inventory geometry from the exact IFD, then verify output.
        expected = tuple(
            unit.size[::-1] if unit.orientation >= 5 else unit.size for unit in tiff_units
        )
        if tuple(frame.size for frame in frames) != expected:
            raise SourceArchiveError()
        return ImageInventory(digest(payload), "tiff", tiff_units, tuple(sorted(categories)))
    if payload[:4] == b"\0\0\1\0":
        from .source_image_containers import ico_members

        units: list[ImageUnit] = []
        for index, (member, kind, dimensions) in enumerate(ico_members(payload)):
            if kind == "png":
                child = read_image(member)
                if child.format != "png" or len(child.units) != 1:
                    raise SourceArchiveError("capability_unavailable")
                frame = RasterFrame(child.units[0].size, child.units[0].orientation)
                categories |= frozenset(child.metadata_categories)
            else:
                frames = inspect_raster(member, "ico", metadata=_standalone_metadata)
                if len(frames) != 1:
                    raise SourceArchiveError("capture_incomplete")
                frame = frames[0]
            if frame.size != dimensions:
                raise SourceArchiveError()
            units.append(ImageUnit("variant", index, frame.size, frame.orientation))
        return ImageInventory(digest(payload), "ico", tuple(units), tuple(sorted(categories)))
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        png_categories: set[str] = set()
        roles = _png_sequence(payload, categories=png_categories)
        categories = frozenset(png_categories)
        kind = "png"
    elif payload.startswith(b"\xff\xd8\xff"):
        from .source_image_containers import inspect_jpeg

        categories = inspect_jpeg(payload)
        roles, kind = ("still",), "jpeg"
    elif payload.startswith((b"GIF87a", b"GIF89a")):
        from .source_image_containers import inspect_gif

        count, categories = inspect_gif(payload)
        roles, kind = ("frame",) * count, "gif"
    elif payload.startswith(b"BM"):
        from .source_image_containers import inspect_bmp

        inspect_bmp(payload)
        roles, kind = ("still",), "bmp"
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        from .source_image_containers import inspect_webp, webp_decode_payload

        roles, categories = inspect_webp(payload)
        decoded_payload, require_opaque = webp_decode_payload(payload)
        kind = "webp"
    else:
        raise SourceArchiveError("capability_unavailable")

    def opaque(image: RasterImage) -> None:
        with closing(image.getchannel("A")) as alpha:
            if alpha.getextrema() != (255, 255):
                raise SourceArchiveError()

    if require_opaque:
        from .source_image_containers import webp_pixel_members

        # VP8X describes intrinsic frame alpha, not the animation's background.
        # A nonopaque ANIM background is valid even when that flag is unset.
        for member in webp_pixel_members(decoded_payload):
            inspect_raster(
                member,
                "webp",
                metadata=_standalone_metadata,
                maximum_dimension=131072,
                maximum_frame_pixels=128_000_000,
                maximum_pixels=256_000_000,
                inspect_pixels=opaque,
            )
    frames = inspect_raster(
        decoded_payload,
        kind,
        metadata=_standalone_metadata,
        maximum_dimension=131072,
        maximum_frame_pixels=128_000_000,
        maximum_pixels=256_000_000,
    )
    if len(frames) != len(roles):
        raise SourceArchiveError("capture_incomplete")
    return ImageInventory(
        digest(payload),
        "apng" if kind == "png" and roles != ("still",) else kind,
        tuple(
            ImageUnit(role, index, frame.size, frame.orientation)
            for index, (role, frame) in enumerate(zip(roles, frames, strict=True))
        ),
        tuple(sorted(categories)),
    )


def image_response(
    payload: bytes,
    position: int | None = None,
    *,
    rectangle: tuple[int, int, int, int] | None = None,
    view: str = "presentation",
    svg: SvgRenderer | None = None,
    av1: Av1Validator | None = None,
) -> dict[str, object]:
    """Reconstruct inventory from exact bytes before every inspection/rendering."""
    if (
        view not in ("presentation", "reconstruction")
        or position is None
        and view != "presentation"
    ):
        raise SourceArchiveError()
    svg_png = None
    item_ids: tuple[int, ...] = ()
    if _svg_candidate(payload):
        inventory, svg_png = _svg_image(payload, svg)
    elif payload[4:8] == b"ftyp":
        inventory, item_ids = _bmff_image(payload, av1)
    else:
        inventory = read_image(payload)
    result: dict[str, object] = {
        "status": "text_screened",
        "resources": "bounded",
        "kind": "image",
        "original_sha256": inventory.original_sha256,
        "reader_id": "tedd-image:1",
        "units": [asdict(unit) for unit in inventory.source_units()],
        "gaps": list(inventory.gaps),
        "inventory": inventory.as_dict(),
    }
    if position is None:
        if rectangle is not None:
            raise SourceArchiveError()
        return result
    unit = inventory.units[positive(position, maximum=len(inventory.units)) - 1]
    if view != "presentation" and (not item_ids or unit.reconstruction_size is None):
        raise SourceArchiveError("capability_unavailable")
    original_size, orientation = (
        (unit.reconstruction_size, 1) if view == "reconstruction" else (unit.size, unit.orientation)
    )
    if original_size is None:
        raise SourceArchiveError()
    oriented = original_size[::-1] if orientation >= 5 else original_size
    size = overview_size(oriented)
    while True:
        if item_ids and position <= len(item_ids):
            render_item = render_heif_item if inventory.format == "heif" else render_avif_item
            png = render_item(
                payload,
                item_ids[position - 1],
                reconstruction=view == "reconstruction",
                size=size if rectangle is None else None,
                rectangle=rectangle,
            )
        elif inventory.format in ("apng", "gif"):
            png = _render_animation(
                payload,
                "png" if inventory.format == "apng" else "gif",
                position,
                size=size if rectangle is None else None,
                rectangle=rectangle,
                orientation=unit.orientation,
            )
        else:
            rendered_payload, rendered_kind, rendered_index = (
                payload,
                inventory.format,
                unit.native_index,
            )
            if inventory.format == "ico":
                from .source_image_containers import ico_members

                rendered_payload, rendered_kind, _ = ico_members(payload)[unit.native_index]
                rendered_index = 0
            elif inventory.format == "avif":
                rendered_index -= len(item_ids)
            elif inventory.format == "svg":
                if svg_png is None:
                    raise SourceArchiveError()
                rendered_payload, rendered_kind = svg_png, "png"
            elif inventory.format == "webp":
                from .source_image_containers import webp_decode_payload

                rendered_payload, _ = webp_decode_payload(payload)
            png = render_raster(
                rendered_payload,
                rendered_kind,
                rendered_index,
                size=size if rectangle is None else None,
                rectangle=rectangle,
                maximum_frame_pixels=128_000_000,
                normalize_gray16=True,
            )
        # Raw PNG and encoded transport limits are separate. Overview reduction
        # is disclosed; native detail never silently shrinks to fit a packet.
        if len(png) <= 8 * 1024**2:
            break
        if rectangle is not None or max(size) == 1:
            raise SourceArchiveError("limit_exceeded")
        size = overview_size(oriented, max(1, max(size) * 3 // 4))
    output_size = struct.unpack("!2I", png[16:24])
    result.update(
        position=position,
        original_size=list(original_size),
        orientation=orientation,
        output_size=list(output_size),
        rectangle=list(rectangle) if rectangle is not None else None,
        png_sha256=digest(png),
        png_base64=base64.b64encode(png).decode("ascii"),
    )
    if view != "presentation":
        result["view"] = view
    return result
