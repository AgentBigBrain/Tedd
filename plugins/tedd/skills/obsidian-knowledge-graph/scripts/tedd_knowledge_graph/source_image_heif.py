"""Optional, pinned libheif binding for the isolated image worker.

This module does not initialize plugin discovery or import pillow-heif's Python
extension. It decodes an already admitted container, compares native item IDs,
and closes each native allocation before delivering its bounded pixel copy.
Windows and Linux development artifacts are pinned separately; unknown builds refuse.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Protocol, cast

from .source_contracts import SourceArchiveError
from .source_image_bmff import BmffInventory, validate_relationships
from .source_native_host import pinned_helper_files

_WINDOWS_LIBRARIES = {
    "libheif-ec36329aadf3858458df7046db2a4326.dll": "7570b01e84170671d280057cb805573b19d2b1c635d399c20cd4b4f054e8cbb8",
    "libde265-0-1a0524458ee4f5129b4644aef4bc4507.dll": "0685d11bcdeacaa2807c93d1b2a0abc3d992a3e3e6024488380a42fd8d84c689",
    "libgcc_s_seh-1-c267b3647edabbd3abe0c9ca261100b7.dll": "396f56c3f23452f3852a51dc344d2c47bedd3686c901a97e557d5a625536cf07",
    "libstdc++-6-2a4fbed512b26ce85768ccc7704933bb.dll": "29ab9af4e91259e2e7ebc217fc87276dac4ebca6df72ec42d87d9cd593a722f8",
    "libwinpthread-1-2e8294a7e7519d618785cdce203fba17.dll": "2e8294a7e7519d618785cdce203fba17e29fb8ce311839b3e5a50a7bd408bd49",
    "libx265-217-13a95e5efe4a61e45dff5a8a4b76d3fe.dll": "7d7bb5acdaa8468f0b66d0f12f04013f96773904fe7d42ddf53ce3d8400c4473",
}
_LINUX_LIBRARIES = {
    "libheif-5ea5cb37.so.1.23.3": "bdd8332d00096c6d5fc8f90d5047de5c8b07a6ebb54119ceebcedc939994769a",
    "libde265-24373b0b.so.0.2.2": "ff82ee6f1db4c2b9eac95af276eb416adae1dfcaddf165beafa2b02ce50f8d13",
    "libx265-5e99f1c8.so.216": "78ee7642d77f2e31fb25d63786c507c78e4849bcec3b4911d908fa1795767edf",
}


class _Error(ctypes.Structure):
    _fields_ = [("code", ctypes.c_int), ("subcode", ctypes.c_int), ("message", ctypes.c_void_p)]


class _DecodePrefix(ctypes.Structure):
    # Public ABI prefix through version 3. The allocation belongs to libheif;
    # never allocate a truncated options struct or expose its message pointers.
    _fields_ = [
        ("version", ctypes.c_uint8),
        ("ignore_transformations", ctypes.c_uint8),
        ("start_progress", ctypes.c_void_p),
        ("on_progress", ctypes.c_void_p),
        ("end_progress", ctypes.c_void_p),
        ("progress_user_data", ctypes.c_void_p),
        ("convert_hdr_to_8bit", ctypes.c_uint8),
        ("strict_decoding", ctypes.c_uint8),
    ]


class _Function(Protocol):
    restype: object
    argtypes: list[object]

    def __call__(self, *args: object) -> object: ...


@contextmanager
def _library() -> Iterator[ctypes.CDLL]:
    libraries = {"win32": _WINDOWS_LIBRARIES, "linux": _LINUX_LIBRARIES}.get(sys.platform)
    if libraries is None or ctypes.sizeof(ctypes.c_void_p) != 8 or "_pillow_heif" in sys.modules:
        raise SourceArchiveError("capability_unavailable")
    try:
        package = distribution("pillow-heif")
        if package.version != "1.7.0":
            raise SourceArchiveError("capability_unavailable")
        root = Path(str(package.locate_file("")))
        if sys.platform == "linux":
            root /= "pillow_heif.libs"
        with pinned_helper_files(
            tuple((root / name, expected) for name, expected in libraries.items())
        ):
            # Windows loads only this DLL directory + System32. The inspected
            # Linux artifact uses $ORIGIN for its two pinned codec dependencies.
            yield ctypes.CDLL(str(root / next(iter(libraries))), winmode=0x1100)
    except (PackageNotFoundError, OSError) as error:
        raise SourceArchiveError("capability_unavailable") from error


def decode_heif(
    payload: bytes,
    inventory: BmffInventory,
    consume: Callable[[int, tuple[int, int], bytes], None],
    *,
    reconstruction: bool = False,
) -> None:
    """Decode every admitted image item, including hidden tiles and auxiliaries.

    The caller owns the worker's shared 30-second/2-GiB limit and must not retain
    all pixel copies. This is an 8-bit RGBA derivative, not calibrated color or
    HDR fidelity; the binding does not apply an ICC display-profile transform.
    Structural admission and visual acknowledgment remain separate obligations.
    """
    if type(reconstruction) is not bool:
        raise SourceArchiveError()
    validate_relationships(inventory.items, inventory.primary_id)
    if inventory.format != "heif" or not 1 <= len(payload) <= 64 * 1024**2:
        raise SourceArchiveError("capability_unavailable")
    pointer, integer, item_id_type = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
    pointer_out, ids_pointer = ctypes.POINTER(pointer), ctypes.POINTER(item_id_type)
    signatures: dict[str, tuple[object, list[object]]] = {
        "heif_get_version_number": (item_id_type, []),
        "heif_context_alloc": (pointer, []),
        "heif_context_free": (None, [pointer]),
        "heif_context_set_max_decoding_threads": (None, [pointer, integer]),
        "heif_context_read_from_memory_without_copy": (
            _Error,
            [pointer, pointer, ctypes.c_size_t, pointer],
        ),
        "heif_context_get_number_of_top_level_images": (integer, [pointer]),
        "heif_context_get_list_of_top_level_image_IDs": (integer, [pointer, ids_pointer, integer]),
        "heif_context_get_primary_image_ID": (_Error, [pointer, ids_pointer]),
        "heif_context_get_image_handle": (_Error, [pointer, item_id_type, pointer_out]),
        "heif_image_handle_release": (None, [pointer]),
        "heif_image_handle_get_item_id": (item_id_type, [pointer]),
        "heif_image_handle_get_number_of_auxiliary_images": (integer, [pointer, integer]),
        "heif_image_handle_get_list_of_auxiliary_image_IDs": (
            integer,
            [pointer, integer, ids_pointer, integer],
        ),
        "heif_image_handle_get_number_of_thumbnails": (integer, [pointer]),
        "heif_image_handle_get_list_of_thumbnail_IDs": (integer, [pointer, ids_pointer, integer]),
        "heif_decoding_options_alloc": (pointer, []),
        "heif_decoding_options_free": (None, [pointer]),
        "heif_decode_image": (_Error, [pointer, pointer_out, integer, integer, pointer]),
        "heif_image_get_width": (integer, [pointer, integer]),
        "heif_image_get_height": (integer, [pointer, integer]),
        "heif_image_get_plane_readonly2": (
            pointer,
            [pointer, integer, ctypes.POINTER(ctypes.c_size_t)],
        ),
        "heif_image_get_decoding_warnings": (integer, [pointer, integer, pointer, integer]),
        "heif_image_release": (None, [pointer]),
    }
    with _library() as library, ExitStack() as owned:
        functions: dict[str, _Function] = {}
        try:
            for name, (result, arguments) in signatures.items():
                function = cast(_Function, getattr(library, name))
                function.restype, function.argtypes = result, arguments
                functions[name] = function
        except AttributeError as error:
            raise SourceArchiveError("capability_unavailable") from error

        def number(name: str, *args: object) -> int:
            value = functions[name](*args)
            if type(value) is not int:
                raise SourceArchiveError()
            return value

        def checked(name: str, *args: object) -> None:
            result = functions[name](*args)
            if not isinstance(result, _Error) or result.code:
                raise SourceArchiveError()

        if number("heif_get_version_number") != 0x01170300:
            raise SourceArchiveError("capability_unavailable")
        context = number("heif_context_alloc")
        if not context:
            raise SourceArchiveError("limit_exceeded")
        owned.callback(functions["heif_context_free"], context)
        functions["heif_context_set_max_decoding_threads"](context, 0)
        buffer = ctypes.create_string_buffer(payload)
        checked("heif_context_read_from_memory_without_copy", context, buffer, len(payload), None)
        count = number("heif_context_get_number_of_top_level_images", context)
        if not 1 <= count <= 256:
            raise SourceArchiveError()
        ids = (item_id_type * count)()
        if (
            number("heif_context_get_list_of_top_level_image_IDs", context, ids, count) != count
            or tuple(ids) != inventory.top_ids
        ):
            raise SourceArchiveError("capture_incomplete")
        primary = item_id_type()
        checked("heif_context_get_primary_image_ID", context, ctypes.byref(primary))
        if primary.value != inventory.primary_id:
            raise SourceArchiveError()
        options = number("heif_decoding_options_alloc")
        if not options:
            raise SourceArchiveError("limit_exceeded")
        owned.callback(functions["heif_decoding_options_free"], options)
        prefix = ctypes.cast(options, ctypes.POINTER(_DecodePrefix)).contents
        if prefix.version != 10:
            raise SourceArchiveError("capability_unavailable")
        prefix.strict_decoding = 1
        prefix.convert_hdr_to_8bit = 1
        prefix.ignore_transformations = int(reconstruction)
        for item in inventory.items:
            if item.kind == b"Exif":
                continue
            handle, image = pointer(), pointer()
            try:
                checked(
                    "heif_context_get_image_handle", context, item.item_id, ctypes.byref(handle)
                )
                if not handle or number("heif_image_handle_get_item_id", handle) != item.item_id:
                    raise SourceArchiveError()
                auxiliary_ids: list[int] = []
                for category, args in (("auxiliary_image", (handle, 0)), ("thumbnail", (handle,))):
                    count_name = "heif_image_handle_get_number_of_" + (
                        "auxiliary_images" if category == "auxiliary_image" else "thumbnails"
                    )
                    count = number(count_name, *args)
                    if not 0 <= count <= 256:
                        raise SourceArchiveError("limit_exceeded")
                    ids = (item_id_type * count)()
                    if (
                        number(
                            "heif_image_handle_get_list_of_" + category + "_IDs", *args, ids, count
                        )
                        != count
                    ):
                        raise SourceArchiveError()
                    auxiliary_ids.extend(ids)
                if set(auxiliary_ids) != set(inventory.auxiliary_ids(item.item_id)) or len(
                    auxiliary_ids
                ) != len(set(auxiliary_ids)):
                    raise SourceArchiveError("capture_incomplete")
                checked("heif_decode_image", handle, ctypes.byref(image), 1, 11, options)
                if not image or number("heif_image_get_decoding_warnings", image, 0, None, 0):
                    raise SourceArchiveError()
                size = (
                    number("heif_image_get_width", image, 10),
                    number("heif_image_get_height", image, 10),
                )
                if size != (item.coded_size if reconstruction else item.display_size):
                    raise SourceArchiveError()
                stride = ctypes.c_size_t()
                address = number("heif_image_get_plane_readonly2", image, 10, ctypes.byref(stride))
                if not address or not size[0] * 4 <= stride.value <= size[0] * 4 + 4096:
                    raise SourceArchiveError()
                rgba = b"".join(
                    ctypes.string_at(address + row * stride.value, size[0] * 4)
                    for row in range(size[1])
                )
            finally:
                if image:
                    functions["heif_image_release"](image)
                if handle:
                    functions["heif_image_handle_release"](handle)
            consume(item.item_id, size, rgba)
