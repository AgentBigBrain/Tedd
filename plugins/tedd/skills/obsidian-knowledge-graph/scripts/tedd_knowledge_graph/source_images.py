"""Closed standalone-image inventory shared by the core and optional worker.

An inventory is a validated description, not proof that a file was fully decoded.
The worker must reconstruct it from the immutable bytes on each rendering call.
No optional imaging package is imported by this standard-library boundary.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass

from .source_contracts import (
    SourceArchiveError,
    SourceLocation,
    SourceUnit,
    decoded_png,
    digest,
    encode,
    object_fields,
    positive,
    sha256,
    text,
)
from .source_documents import DocumentReading
from .source_review import ReviewBook, ReviewRendering, overview_size

_ROLES = {
    "png": frozenset({"still"}),
    "apng": frozenset({"poster", "frame"}),
    "jpeg": frozenset({"still"}),
    "webp": frozenset({"still", "frame"}),
    "avif": frozenset({"still", "frame", "item", "auxiliary"}),
    "gif": frozenset({"frame"}),
    "bmp": frozenset({"still"}),
    "tiff": frozenset({"page"}),
    "heif": frozenset({"item", "auxiliary"}),
    "ico": frozenset({"variant"}),
    "svg": frozenset({"still"}),
}

_METADATA_ACCESS = {
    "ordinary": frozenset(),
    "confidential": frozenset({"description", "identifying"}),
    "medical": frozenset({"description"}),
    "restricted": frozenset({"description", "identifying", "location"}),
}


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise SourceArchiveError()
    return value


@dataclass(frozen=True, repr=False)
class ImageUnit:
    """One constituent, including auxiliary images and same-size icon variants.

    Native indices are zero-based decoder/directory ordinals for ordinary units;
    an AVIF sequence lists its still item first, then its track's sample ordinals.
    auxiliary indices are the parent's codec-supplied image identifiers. Parent
    identifies that top-level unit's native index. Neither dimension nor a name
    substitutes for a variant's directory index.
    """

    role: str
    native_index: int
    size: tuple[int, int]
    orientation: int = 1
    parent: int | None = None
    reconstruction_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in {
            "still",
            "frame",
            "poster",
            "page",
            "item",
            "variant",
            "auxiliary",
        }:
            raise SourceArchiveError()
        _index(self.native_index)
        if type(self.size) is not tuple or len(self.size) != 2:
            raise SourceArchiveError()
        for dimension in self.size:
            positive(dimension, maximum=131072)
        if self.size[0] * self.size[1] > 128_000_000:
            raise SourceArchiveError("limit_exceeded")
        positive(self.orientation, maximum=8)
        if (self.role == "auxiliary") != (self.parent is not None):
            raise SourceArchiveError()
        if self.parent is not None:
            _index(self.parent)
        if self.reconstruction_size is not None:
            overview_size(self.reconstruction_size)

    @property
    def selector(self) -> str:
        parent = "root" if self.parent is None else str(self.parent)
        return f"{self.role}:{parent}:{self.native_index}"

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["size"] = list(self.size)
        if self.reconstruction_size is None:
            del value["reconstruction_size"]
        else:
            value["reconstruction_size"] = list(self.reconstruction_size)
        return value


@dataclass(frozen=True, repr=False)
class ImageInventory:
    original_sha256: str
    format: str
    units: tuple[ImageUnit, ...]
    metadata_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        sha256(self.original_sha256)
        if not isinstance(self.format, str) or self.format not in _ROLES:
            raise SourceArchiveError("capability_unavailable")
        if (
            type(self.metadata_categories) is not tuple
            or any(
                type(category) is not str or category not in _METADATA_ACCESS["restricted"]
                for category in self.metadata_categories
            )
            or self.metadata_categories != tuple(sorted(set(self.metadata_categories)))
        ):
            raise SourceArchiveError()
        if (
            type(self.units) is not tuple
            or not 1 <= len(self.units) <= 256
            or any(not isinstance(unit, ImageUnit) for unit in self.units)
        ):
            raise SourceArchiveError()
        if any(unit.role not in _ROLES[self.format] for unit in self.units):
            raise SourceArchiveError()
        if self.format not in ("avif", "heif") and any(
            unit.reconstruction_size is not None for unit in self.units
        ):
            raise SourceArchiveError()
        if len({unit.selector for unit in self.units}) != len(self.units):
            raise SourceArchiveError()
        top = tuple(unit for unit in self.units if unit.parent is None)
        if not top or tuple(unit.native_index for unit in top) != tuple(range(len(top))):
            raise SourceArchiveError("capture_incomplete")
        if any(unit.parent is not None and unit.parent >= len(top) for unit in self.units):
            raise SourceArchiveError()
        if any(unit.role == "still" for unit in self.units) and len(top) != 1:
            raise SourceArchiveError()
        if any(unit.role == "poster" for unit in self.units[1:]):
            raise SourceArchiveError()
        if self.pixel_count > 256_000_000:
            raise SourceArchiveError("limit_exceeded")

    @property
    def gaps(self) -> tuple[str, ...]:
        """Display limits, not a claim that the original contains HDR or metadata.

        Originals retain their admitted profiles and timing. The shared viewer
        delivers 8-bit RGBA frames without calibrated color/HDR playback or
        quantitative interpretation of auxiliary planes.
        """
        gaps: tuple[str, ...] = (
            "image_pixels_require_viewing",
            "image_preview_8bit_color_not_calibrated",
            "image_preview_hdr_not_reproduced",
        )
        if any(unit.role == "frame" for unit in self.units):
            gaps += ("animation_timing_not_reproduced",)
        if any(unit.role == "auxiliary" for unit in self.units):
            gaps += ("auxiliary_pixels_not_calibrated_measurements",)
        if self.format == "svg":
            gaps += ("svg_fixed_source_sans_pro_font",)
        if self.format in ("jpeg", "heif"):
            gaps += ("encoded_bytes_not_exhaustively_inspected",)
        return gaps

    @property
    def pixel_count(self) -> int:
        return sum(
            max(
                unit.size[0] * unit.size[1],
                0
                if unit.reconstruction_size is None
                else unit.reconstruction_size[0] * unit.reconstruction_size[1],
            )
            for unit in self.units
        )

    def source_units(self) -> tuple[SourceUnit, ...]:
        return tuple(
            SourceUnit(
                f"u{position}",
                "",
                position,
                kind="image_page",
                location=SourceLocation("page", f"image:1:{self.format}:{unit.selector}", 1, 1),
            )
            for position, unit in enumerate(self.units, start=1)
        )

    def require_metadata_permission(self, classification: str) -> None:
        """Profile 1 is a closed mapping, not an ordering of sensitivity labels.

        This describes metadata presence only. Actual source access and audience
        must already be authorized; neither a category nor a classification is
        an authority grant, and unknown never permits retention.
        """
        if (
            classification not in _METADATA_ACCESS
            or not set(self.metadata_categories) <= _METADATA_ACCESS[classification]
        ):
            raise SourceArchiveError("privacy_rejected")

    def as_dict(self) -> dict[str, object]:
        return {
            "original_sha256": self.original_sha256,
            "format": self.format,
            "units": [unit.as_dict() for unit in self.units],
            "metadata_profile": 1,
            "metadata_categories": list(self.metadata_categories),
        }

    @classmethod
    def decode(cls, value: object) -> ImageInventory:
        obj = object_fields(
            value, {"original_sha256", "format", "units", "metadata_profile", "metadata_categories"}
        )
        if type(obj["metadata_profile"]) is not int or obj["metadata_profile"] != 1:
            raise SourceArchiveError()
        categories = obj["metadata_categories"]
        if not isinstance(categories, list) or len(categories) > 3:
            raise SourceArchiveError()
        raw_units = obj["units"]
        if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= 256:
            raise SourceArchiveError()
        units: list[ImageUnit] = []
        for raw in raw_units:
            fields = {"role", "native_index", "size", "orientation", "parent"}
            if isinstance(raw, dict) and "reconstruction_size" in raw:
                fields.add("reconstruction_size")
            item = object_fields(raw, fields)
            size = item["size"]
            if not isinstance(size, list) or len(size) != 2:
                raise SourceArchiveError()
            reconstructed = item.get("reconstruction_size")
            if reconstructed is not None and (
                not isinstance(reconstructed, list) or len(reconstructed) != 2
            ):
                raise SourceArchiveError()
            units.append(
                ImageUnit(
                    text(item["role"]),
                    _index(item["native_index"]),
                    (positive(size[0]), positive(size[1])),
                    positive(item["orientation"]),
                    None if item["parent"] is None else _index(item["parent"]),
                    None
                    if reconstructed is None
                    else (positive(reconstructed[0]), positive(reconstructed[1])),
                )
            )
        return cls(
            sha256(obj["original_sha256"]),
            text(obj["format"]),
            tuple(units),
            tuple(text(category) for category in categories),
        )


@dataclass(frozen=True, repr=False)
class ImageReading:
    """Validated worker output; decoding still supplies no review/retention authority."""

    inventory: ImageInventory
    position: int | None = None
    png: bytes | None = None
    rectangle: tuple[int, int, int, int] | None = None
    view: str = "presentation"

    def __post_init__(self) -> None:
        if not isinstance(self.inventory, ImageInventory):
            raise SourceArchiveError()
        if self.view not in ("presentation", "reconstruction"):
            raise SourceArchiveError()
        if self.position is None:
            if self.png is not None or self.rectangle is not None or self.view != "presentation":
                raise SourceArchiveError()
            return
        positive(self.position, maximum=len(self.inventory.units))
        if (
            not isinstance(self.png, bytes)
            or not 33 <= len(self.png) <= 8 * 1024**2
            or not self.png.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        ):
            raise SourceArchiveError()
        self.review("0" * 64, "0" * 64, "model")  # Validate geometry; no receipt is offered.

    @property
    def document(self) -> DocumentReading:
        return DocumentReading(
            self.inventory.original_sha256,
            self.inventory.source_units(),
            "tedd-image:1",
            self.inventory.gaps,
            kind="image",
        )

    def review(self, scope_key: str, source_key: str, audience: str) -> ReviewRendering:
        if self.position is None or self.png is None:
            raise SourceArchiveError("capture_incomplete")
        unit = self.inventory.units[self.position - 1]
        size = unit.reconstruction_size if self.view == "reconstruction" else unit.size
        if size is None:
            raise SourceArchiveError("capability_unavailable")
        width, height = struct.unpack("!2I", self.png[16:24])
        return ReviewRendering(
            scope_key,
            source_key,
            self.inventory.original_sha256,
            f"u{self.position}",
            audience,
            digest(self.png),
            size,
            (width, height),
            unit.orientation if self.view == "presentation" else 1,
            self.rectangle,
            self.view,
        )

    def require_reviews(
        self, book: ReviewBook, scope_key: str, source_key: str, audience: str
    ) -> None:
        for position, unit in enumerate(self.inventory.units, 1):
            views = [("presentation", unit.size, unit.orientation)]
            if unit.reconstruction_size is not None:
                views.append(("reconstruction", unit.reconstruction_size, 1))
            for view, size, orientation in views:
                self._require_review(
                    book, scope_key, source_key, audience, position, view, size, orientation
                )

    def _require_review(
        self,
        book: ReviewBook,
        scope_key: str,
        source_key: str,
        audience: str,
        position: int,
        view: str,
        size: tuple[int, int],
        orientation: int,
    ) -> None:
        oriented = size[::-1] if orientation >= 5 else size
        # A lookup probe, never an offered/delivered receipt. Actual approvals
        # come only from this turn's book and its exact source/unit binding.
        probe = ReviewRendering(
            scope_key,
            source_key,
            self.inventory.original_sha256,
            f"u{position}",
            audience,
            "0" * 64,
            size,
            overview_size(oriented),
            orientation,
            view=view,
        )
        if not any(
            reviewed.rectangle is None or reviewed.rectangle == (0, 0, *oriented)
            for reviewed in book.acknowledged(probe)
        ):
            raise SourceArchiveError("privacy_rejected")

    @classmethod
    def decode(cls, payload: bytes, value: object) -> ImageReading:
        if not isinstance(value, dict):
            raise SourceArchiveError()
        rendered = "png_base64" in value
        fields = {
            "status",
            "resources",
            "kind",
            "original_sha256",
            "reader_id",
            "units",
            "gaps",
            "inventory",
        }
        if "view" in value:
            fields.add("view")
        if rendered:
            fields |= {
                "position",
                "original_size",
                "output_size",
                "orientation",
                "rectangle",
                "png_sha256",
                "png_base64",
            }
        obj = object_fields(value, fields)
        inventory = ImageInventory.decode(obj["inventory"])
        if (
            obj["status"] != "text_screened"
            or obj["resources"] != "bounded"
            or obj["kind"] != "image"
            or obj["reader_id"] != "tedd-image:1"
            or obj["original_sha256"] != digest(payload)
            or inventory.original_sha256 != digest(payload)
            or encode(obj["units"]) != encode([asdict(unit) for unit in inventory.source_units()])
            or obj["gaps"] != list(inventory.gaps)
        ):
            raise SourceArchiveError()
        if not rendered:
            return cls(inventory, view=text(obj.get("view", "presentation")))
        rectangle = obj["rectangle"]
        if rectangle is not None and (not isinstance(rectangle, list) or len(rectangle) != 4):
            raise SourceArchiveError()
        reading = cls(
            inventory,
            positive(obj["position"], maximum=len(inventory.units)),
            decoded_png(obj),
            None
            if rectangle is None
            else (
                _index(rectangle[0]),
                _index(rectangle[1]),
                _index(rectangle[2]),
                _index(rectangle[3]),
            ),
            view=text(obj.get("view", "presentation")),
        )
        preview = reading.review("0" * 64, "0" * 64, "model")
        if (
            encode(obj["original_size"]) != encode(list(preview.original_size))
            or encode(obj["output_size"]) != encode(list(preview.output_size))
            or type(obj["orientation"]) is not int
            or obj["orientation"] != preview.orientation
        ):
            raise SourceArchiveError()
        return reading
