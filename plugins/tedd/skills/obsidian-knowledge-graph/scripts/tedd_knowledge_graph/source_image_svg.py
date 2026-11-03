"""Positive admission for a bounded static SVG subset, without rendering or I/O.

An admitted document still needs a qualified renderer, exact visual review and
retention authority. This does not sanitize or replace the original bytes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .legacy_values import secret_hits
from .source_contracts import SourceArchiveError, digest
from .source_image_metadata import metadata_text
from .source_images import ImageUnit

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_ID = r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}"
_NUMERIC = re.compile(_NUMBER)
_SEPARATOR = re.compile(r"(?:[ \t\r\n]+,?[ \t\r\n]*|,[ \t\r\n]*)")
_PATH_TOKEN = re.compile(_NUMBER + r"|[A-Za-z]")
_REFERENCE = re.compile(r"url\(\s*(['\"]?)#(" + _ID + r")\1\s*\)")
_SHAPES = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"})
_GRADIENTS = frozenset({"linearGradient", "radialGradient"})
_GRAPHICS = _SHAPES | {"g", "text"}
_COMMON = frozenset(
    {
        "id",
        "transform",
        "fill",
        "stroke",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "stroke-dashoffset",
        "fill-rule",
        "clip-rule",
        "clip-path",
        "marker-start",
        "marker-mid",
        "marker-end",
        "color",
    }
)
_TEXT = frozenset(
    {
        "x",
        "y",
        "dx",
        "dy",
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "text-anchor",
        "letter-spacing",
        "word-spacing",
    }
)
_ATTRIBUTES = {
    "svg": frozenset({"width", "height", "viewBox", "preserveAspectRatio", "version"}),
    "g": frozenset(),
    "defs": frozenset(),
    "title": frozenset(),
    "desc": frozenset(),
    "rect": frozenset({"x", "y", "width", "height", "rx", "ry"}),
    "circle": frozenset({"cx", "cy", "r"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry"}),
    "line": frozenset({"x1", "y1", "x2", "y2"}),
    "polyline": frozenset({"points"}),
    "polygon": frozenset({"points"}),
    "path": frozenset({"d", "pathLength"}),
    "text": _TEXT,
    "tspan": _TEXT,
    "linearGradient": frozenset(
        {"x1", "y1", "x2", "y2", "gradientUnits", "gradientTransform", "spreadMethod", "href"}
    ),
    "radialGradient": frozenset(
        {
            "cx",
            "cy",
            "r",
            "fx",
            "fy",
            "fr",
            "gradientUnits",
            "gradientTransform",
            "spreadMethod",
            "href",
        }
    ),
    "stop": frozenset({"offset", "stop-color", "stop-opacity"}),
    "clipPath": frozenset({"clipPathUnits"}),
    "marker": frozenset(
        {
            "markerWidth",
            "markerHeight",
            "refX",
            "refY",
            "orient",
            "markerUnits",
            "viewBox",
            "preserveAspectRatio",
        }
    ),
}
_ENUMS = {
    "stroke-linecap": {"butt", "round", "square"},
    "stroke-linejoin": {"miter", "round", "bevel"},
    "fill-rule": {"nonzero", "evenodd"},
    "clip-rule": {"nonzero", "evenodd"},
    "gradientUnits": {"userSpaceOnUse", "objectBoundingBox"},
    "clipPathUnits": {"userSpaceOnUse", "objectBoundingBox"},
    "spreadMethod": {"pad", "reflect", "repeat"},
    "markerUnits": {"strokeWidth", "userSpaceOnUse"},
    "font-style": {"normal", "italic", "oblique"},
    "font-weight": {
        "normal",
        "bold",
        "100",
        "200",
        "300",
        "400",
        "500",
        "600",
        "700",
        "800",
        "900",
    },
    "text-anchor": {"start", "middle", "end"},
    "version": {"1.0", "1.1", "2.0"},
}
_COLOURS = frozenset(
    {
        "none",
        "currentColor",
        "black",
        "white",
        "red",
        "green",
        "blue",
        "lime",
        "yellow",
        "cyan",
        "aqua",
        "magenta",
        "fuchsia",
        "gray",
        "grey",
        "silver",
        "maroon",
        "olive",
        "navy",
        "teal",
        "purple",
        "orange",
        "transparent",
    }
)


@dataclass(frozen=True, repr=False)
class SvgInventory:
    source_sha256: str
    size: tuple[int, int]
    font_families: tuple[str, ...]
    metadata_categories: tuple[str, ...]


class _BoundedTree(ET.TreeBuilder):
    """Stop excessive XML construction before allocating the complete tree."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.characters = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.depth += 1
        self.nodes += 1
        if self.depth > 32 or self.nodes > 4096 or len(attrs) > 48:
            raise SourceArchiveError("limit_exceeded")
        if any(len(key) > 256 or len(value) > 8192 for key, value in attrs.items()):
            raise SourceArchiveError("limit_exceeded")
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        self.depth -= 1
        return super().end(tag)

    def data(self, data: str) -> None:
        self.characters += len(data)
        if self.characters > 1_048_576:
            raise SourceArchiveError("limit_exceeded")
        super().data(data)

    def start_ns(self, prefix: str | None, uri: str) -> None:
        if len(prefix or "") > 32 or uri not in (SVG_NS, XLINK_NS):
            raise SourceArchiveError("privacy_rejected")


def _numbers(value: str) -> tuple[float, ...]:
    # Advance monotonically. A repeated-number regex with an optional separator
    # can backtrack exponentially over a short malformed digit string.
    value = value.strip(" \t\r\n")
    result: list[float] = []
    cursor = 0
    while cursor < len(value):
        match = _NUMERIC.match(value, cursor)
        if match is None:
            raise SourceArchiveError()
        number = float(match.group())
        if not math.isfinite(number) or abs(number) > 1e9:
            raise SourceArchiveError()
        result.append(number)
        cursor = match.end()
        if cursor == len(value):
            break
        separator = _SEPARATOR.match(value, cursor)
        if separator is None:
            raise SourceArchiveError()
        cursor = separator.end()
        if cursor == len(value):
            raise SourceArchiveError()
    if not result:
        raise SourceArchiveError()
    return tuple(result)


def _scalar(value: str) -> float:
    values = _numbers(value)
    if len(values) != 1:
        raise SourceArchiveError()
    return values[0]


def _length(value: str) -> float:
    return _scalar(value.removesuffix("px"))


def _path(value: str) -> None:
    tokens: list[str] = []
    cursor = 0
    for match in _PATH_TOKEN.finditer(value):
        separator = value[cursor : match.start()]
        numeric = _NUMERIC.fullmatch(match.group()) is not None
        between_numbers = numeric and bool(tokens) and _NUMERIC.fullmatch(tokens[-1]) is not None
        if not re.fullmatch(
            r"[ \t\r\n]*,?[ \t\r\n]*" if between_numbers else r"[ \t\r\n]*", separator
        ):
            raise SourceArchiveError()
        tokens.append(match.group())
        cursor = match.end()
    if value[cursor:].strip() or not tokens or tokens[0] not in ("M", "m"):
        raise SourceArchiveError()
    arities = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    index = 0
    while index < len(tokens):
        command = tokens[index].upper()
        index += 1
        if command == "Z":
            continue
        if command not in arities:
            raise SourceArchiveError()
        begin = index
        while index < len(tokens) and _NUMERIC.fullmatch(tokens[index]):
            index += 1
        count = index - begin
        if not count or count % arities[command]:
            raise SourceArchiveError()
        numbers = _numbers(" ".join(tokens[begin:index]))
        if command == "A":
            for offset in range(0, count, 7):
                if (
                    numbers[offset] < 0
                    or numbers[offset + 1] < 0
                    or any(tokens[begin + offset + flag] not in ("0", "1") for flag in (3, 4))
                ):
                    raise SourceArchiveError()


def _transform(value: str) -> None:
    cursor = 0
    arities = {
        "matrix": (6,),
        "translate": (1, 2),
        "scale": (1, 2),
        "rotate": (1, 3),
        "skewX": (1,),
        "skewY": (1,),
    }
    matches = list(re.finditer(r"([A-Za-z]+)\s*\(([^()]*)\)", value))
    if not matches or len(matches) > 64:
        raise SourceArchiveError("limit_exceeded")
    for match in matches:
        if not re.fullmatch(
            r"[ \t\r\n]*,?[ \t\r\n]*" if cursor else r"[ \t\r\n]*", value[cursor : match.start()]
        ):
            raise SourceArchiveError()
        if len(_numbers(match[2])) not in arities.get(match[1], ()):
            raise SourceArchiveError()
        cursor = match.end()
    if value[cursor:].strip():
        raise SourceArchiveError()


def _colour(value: str) -> None:
    if value in _COLOURS or re.fullmatch(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})", value):
        return
    match = re.fullmatch(r"rgb\(([^()]*)\)", value)
    if match:
        values = _numbers(match[1])
        if len(values) == 3 and all(0 <= number <= 255 for number in values):
            return
    raise SourceArchiveError("capability_unavailable")


def _attribute(tag: str, name: str, value: str) -> tuple[str, frozenset[str]] | None:
    if name == "id":
        if not re.fullmatch(_ID, value):
            raise SourceArchiveError()
    elif name == "href":
        if tag not in _GRADIENTS or not re.fullmatch("#" + _ID, value):
            raise SourceArchiveError("privacy_rejected")
        return value[1:], _GRADIENTS
    elif name in ("fill", "stroke", "clip-path", "marker-start", "marker-mid", "marker-end"):
        target = _REFERENCE.fullmatch(value)
        if target:
            kinds = (
                _GRADIENTS
                if name in ("fill", "stroke")
                else frozenset({"clipPath" if name == "clip-path" else "marker"})
            )
            return target[2], kinds
        if name in ("fill", "stroke"):
            _colour(value)
        elif value != "none":
            raise SourceArchiveError("privacy_rejected")
    elif name in ("color", "stop-color"):
        _colour(value)
    elif (
        name == "font-style"
        and value != "normal"
        or name == "font-weight"
        and value not in ("normal", "400")
    ):
        # The initial qualified font set has one regular face. Matching by
        # family alone must not silently replace requested bold/italic metrics.
        raise SourceArchiveError("capability_unavailable")
    elif name in _ENUMS:
        if value not in _ENUMS[name]:
            raise SourceArchiveError()
    elif name in ("transform", "gradientTransform"):
        _transform(value)
    elif name == "d":
        _path(value)
    elif name == "viewBox":
        numbers = _numbers(value)
        if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
            raise SourceArchiveError()
    elif name == "preserveAspectRatio":
        if not re.fullmatch(
            r"(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?: (?:meet|slice))?)", value
        ):
            raise SourceArchiveError()
    elif name == "font-family":
        if not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,63}", value):
            raise SourceArchiveError("capability_unavailable")
    elif name == "orient" and value in ("auto", "auto-start-reverse"):
        pass
    elif name == "stroke-dasharray" and value == "none":
        pass
    elif name == "stroke-miterlimit":
        if _scalar(value) < 1:
            raise SourceArchiveError()
    elif name in ("pathLength", "orient"):
        number = _scalar(value)
        if name == "pathLength" and number < 0:
            raise SourceArchiveError()
    elif name == "points":
        values = _numbers(value)
        if len(values) % 2 or len(values) < (6 if tag == "polygon" else 4):
            raise SourceArchiveError()
    elif name in ("opacity", "fill-opacity", "stroke-opacity", "stop-opacity", "offset"):
        number = _scalar(value[:-1]) / 100 if value.endswith("%") else _scalar(value)
        if not 0 <= number <= 1:
            raise SourceArchiveError()
    else:
        numbers = (
            _numbers(value[:-1]) if value.endswith("%") else _numbers(value.removesuffix("px"))
        )
        is_list = (
            name == "stroke-dasharray"
            or tag in ("text", "tspan")
            and name in ("x", "y", "dx", "dy")
        )
        if len(numbers) != 1 and not is_list:
            raise SourceArchiveError()
        if name in (
            "width",
            "height",
            "r",
            "rx",
            "ry",
            "fr",
            "stroke-width",
            "pathLength",
            "font-size",
            "markerWidth",
            "markerHeight",
            "stroke-dasharray",
        ) and any(number < 0 for number in numbers):
            raise SourceArchiveError()
    return None


def inspect_svg(payload: bytes) -> SvgInventory:
    """Validate the retained XML without following links or invoking a renderer."""
    if not 1 <= len(payload) <= 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    try:
        content = payload.decode("utf-8-sig", errors="strict")
        # Unknown XML declarations, comments and CDATA are not this profile.
        declaration = re.match(
            r"^<\?xml version=[\"\']1\.0[\"\'](?: encoding=[\"\']UTF-8[\"\'])?\s*\?>", content
        )
        body = content[declaration.end() :] if declaration else content
        if "<!" in content or "<?" in body or "\0" in content or secret_hits(content):
            raise SourceArchiveError("privacy_rejected")
        root = ET.fromstring(content, parser=ET.XMLParser(target=_BoundedTree()))
    except (UnicodeError, ET.ParseError) as error:
        raise SourceArchiveError() from error
    if root.tag != "{" + SVG_NS + "}svg":
        raise SourceArchiveError()
    nodes = list(root.iter())
    kinds: dict[ET.Element, str] = {}
    ids: dict[str, ET.Element] = {}
    edges: dict[ET.Element, list[ET.Element]] = {}
    references: list[tuple[ET.Element, str, frozenset[str]]] = []
    fonts: set[str] = set()
    categories: set[str] = set()
    for node in nodes:
        if not node.tag.startswith("{" + SVG_NS + "}"):
            raise SourceArchiveError("privacy_rejected")
        tag = node.tag[len(SVG_NS) + 2 :]
        if tag not in _ATTRIBUTES or tag == "svg" and node is not root:
            raise SourceArchiveError("capability_unavailable")
        kinds[node] = tag
        edges[node] = list(node)
        names: set[str] = set()
        for raw_name, value in node.attrib.items():
            # XML character references are decoded here. Screening only the
            # original XML would miss credential-bearing normalized values.
            metadata_text(value.encode("utf-8"))
            name = "href" if raw_name == "{" + XLINK_NS + "}href" else raw_name
            if name in names or name not in _COMMON | _ATTRIBUTES[tag]:
                raise SourceArchiveError("privacy_rejected")
            names.add(name)
            if name == "id" and value != value.strip():
                raise SourceArchiveError()
            reference = _attribute(tag, name, value.strip())
            if reference:
                references.append((node, *reference))
            if name == "id":
                if value in ids:
                    raise SourceArchiveError()
                ids[value] = node
            if name == "font-family":
                fonts.add(value.strip())
        if node.text and node.text.strip():
            if tag not in ("text", "tspan", "title", "desc"):
                raise SourceArchiveError("privacy_rejected")
            metadata_text(node.text.encode("utf-8"))
            if tag in ("title", "desc"):
                categories.add("description")
    for node in nodes:
        tag = kinds[node]
        allowed = (
            _GRAPHICS | {"defs", "title", "desc"}
            if tag in ("svg", "g")
            else (
                _GRADIENTS | {"clipPath", "marker"}
                if tag == "defs"
                else {"stop"}
                if tag in _GRADIENTS
                else _GRAPHICS
                if tag in ("clipPath", "marker")
                else {"tspan"}
                if tag in ("text", "tspan")
                else {"title", "desc"}
                if tag in _SHAPES
                else set()
            )
        )
        for child in node:
            if (
                kinds[child] not in allowed
                or child.tail
                and child.tail.strip()
                and tag not in ("text", "tspan")
            ):
                raise SourceArchiveError()
            if child.tail and child.tail.strip():
                metadata_text(child.tail.encode("utf-8"))
    for node, name, allowed in references:
        target = ids.get(name)
        if target is None or kinds[target] not in allowed:
            raise SourceArchiveError()
        edges[node].append(target)
    visiting: set[ET.Element] = set()
    sizes: dict[ET.Element, int] = {}

    def expanded(node: ET.Element) -> int:
        if node in visiting or len(visiting) >= 64:
            raise SourceArchiveError("limit_exceeded")
        if node not in sizes:
            visiting.add(node)
            total = 1
            for child in edges[node]:
                total += expanded(child)
                if total > 65536:
                    raise SourceArchiveError("limit_exceeded")
            visiting.remove(node)
            sizes[node] = total
        return sizes[node]

    expanded(root)
    box = _numbers(root.attrib["viewBox"]) if "viewBox" in root.attrib else ()
    dimensions = tuple(
        _length(root.attrib[key]) if key in root.attrib else box[index + 2] if box else 0
        for index, key in enumerate(("width", "height"))
    )
    if any(number <= 0 for number in dimensions):
        raise SourceArchiveError()
    size = math.ceil(dimensions[0]), math.ceil(dimensions[1])
    ImageUnit("still", 0, size)
    return SvgInventory(digest(payload), size, tuple(sorted(fonts)), tuple(sorted(categories)))
