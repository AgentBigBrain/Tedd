"""Finite, inert readings of saved OOXML—not an Office renderer or formula engine.

Run through the bounded host worker for untrusted inputs. XML and qualified inert
image parts are screened; other opaque parts, macros, external data, and
noncanonical paths are refused. No package extraction reaches disk.
"""

from __future__ import annotations

import io
import posixpath
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Collection, Iterator
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from .source_contracts import (
    PDF_BYTES,
    READING_BYTES,
    SourceArchiveError,
    SourceLocation,
    SourceUnit,
    digest,
    positive,
    text,
)
from .source_documents import DocumentReading, _append, _screen
from .source_office_metadata import inspect_printer_settings

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
BIB = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"


def _container_spans(payload: bytes, members: list[zipfile.ZipInfo]) -> dict[str, bytes]:
    """Refuse ZIP data that the XML reader cannot inspect; never rewrite originals.

    Check the finite ZIP32 envelope around zipfile's validated members. Comments,
    extra fields, concatenated archives and unclaimed gaps are not source text.
    ZIP64 is unnecessary under the existing acquisition/expansion ceilings.
    """
    end = struct.unpack("<4s4H2IH", payload[-22:])
    directory_size = sum(
        46 + len(item.orig_filename.encode("utf-8" if item.flag_bits & 0x800 else "cp437"))
        for item in members
    )
    if end != (
        b"PK\x05\x06",
        0,
        0,
        len(members),
        len(members),
        directory_size,
        len(payload) - 22 - directory_size,
        0,
    ):
        raise SourceArchiveError("capability_unavailable")
    cursor = 0
    result: dict[str, bytes] = {}
    for item in sorted(members, key=lambda value: value.header_offset):
        _screen(item.orig_filename)
        if item.comment or item.extra or item.header_offset != cursor:
            raise SourceArchiveError("capability_unavailable")
        header = struct.unpack_from("<4s5H3I2H", payload, cursor)
        name = item.orig_filename.encode("utf-8" if item.flag_bits & 0x800 else "cp437")
        if (
            header[0] != b"PK\x03\x04"
            or header[2] != item.flag_bits
            or header[2] & ~0x80E
            or header[3] != item.compress_type
            or header[3] not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or header[-2:] != (len(name), 0)
            or payload[cursor + 30 : cursor + 30 + len(name)] != name
            or (
                not item.flag_bits & 8
                and header[6:9] != (item.CRC, item.compress_size, item.file_size)
            )
        ):
            raise SourceArchiveError("capability_unavailable")
        cursor += 30 + len(name)
        result[item.filename] = payload[cursor : cursor + item.compress_size]
        cursor += item.compress_size
        if item.flag_bits & 8:
            if header[6:9] not in ((0, 0, 0), (item.CRC, item.compress_size, item.file_size)):
                raise SourceArchiveError("capability_unavailable")
            if payload[cursor : cursor + 4] == b"PK\x07\x08":
                cursor += 4
            if struct.unpack_from("<3I", payload, cursor) != (
                item.CRC,
                item.compress_size,
                item.file_size,
            ):
                raise SourceArchiveError("capability_unavailable")
            cursor += 12
    if cursor != end[6]:
        raise SourceArchiveError("capability_unavailable")
    return result


def _package(
    payload: bytes, inspect_image: Callable[[bytes, str], int] | None, gaps: set[str]
) -> tuple[dict[str, ET.Element], list[tuple[str, int]]]:
    result: dict[str, ET.Element] = {}
    seen: set[str] = set()
    images: set[str] = set()
    printer_settings: set[str] = set()
    image_frames: list[tuple[str, int]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        members = package.infolist()
        if (
            not 1 <= len(members) <= 4096
            or sum(item.file_size for item in members) > PDF_BYTES
            or any(item.file_size > READING_BYTES for item in members)
        ):
            raise SourceArchiveError("limit_exceeded")
        spans = _container_spans(payload, members)
        for item in members:
            name = item.filename
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or str(path) != name
                or name in seen
                or item.is_dir()
                or item.flag_bits & 1
            ):
                raise SourceArchiveError()
            seen.add(name)
            image_kind = path.suffix.lower().removeprefix(".")
            image_part = image_kind in ("png", "jpg", "jpeg", "webp", "avif", "gif", "bmp") and (
                len(path.parts) == 3
                and path.parts[0] in ("word", "xl", "ppt")
                and path.parts[1] == "media"
                or name in ("docProps/thumbnail.jpeg", "docProps/thumbnail.png")
            )
            printer_part = (
                len(path.parts) == 3
                and path.parts[0] in ("word", "xl", "ppt")
                and path.parts[1] == "printerSettings"
                and re.fullmatch(r"printerSettings[1-9][0-9]*\.bin", path.name) is not None
            )
            if (
                not name.endswith((".xml", ".rels")) and not image_part and not printer_part
            ) or any(
                part.casefold() in ("embeddings", "activex", "externallinks")
                or (part.casefold() == "customxml" and path.parts[0] != "customXml")
                for part in path.parts
            ):
                raise SourceArchiveError("capability_unavailable")
            with package.open(item) as stream:
                raw = stream.read(READING_BYTES + 1)
            if len(raw) != item.file_size or len(raw) > READING_BYTES:
                raise SourceArchiveError("limit_exceeded")
            if item.compress_type == zipfile.ZIP_STORED and spans[name] != raw:
                raise SourceArchiveError("capability_unavailable")
            if item.compress_type == zipfile.ZIP_DEFLATED:
                inflater = zlib.decompressobj(-15)
                checked = inflater.decompress(spans[name], len(raw) + 1)
                if (
                    checked != raw
                    or not inflater.eof
                    or inflater.unused_data
                    or inflater.unconsumed_tail
                ):
                    raise SourceArchiveError("capability_unavailable")
            if printer_part:
                inspect_printer_settings(raw)
                printer_settings.add(name)
                gaps.add("print_metadata_not_document_evidence")
                continue
            if image_part:
                if inspect_image is None:
                    raise SourceArchiveError("capability_unavailable")
                frames = positive(
                    inspect_image(raw, "jpeg" if image_kind == "jpg" else image_kind), maximum=256
                )
                image_frames.append((name, frames))
                if sum(count for _, count in image_frames) > 256:
                    raise SourceArchiveError("limit_exceeded")
                if frames > 1:
                    gaps.add("animation_timing_requires_check")
                images.add(name)
                gaps.add("embedded_images_require_visual_review")
                continue
            # UTF-8-only prevents alternate-encoding entity declarations bypassing
            # the preparse check. Limits also apply to expanded XML structure.
            content = raw.decode("utf-8-sig", errors="strict")
            if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
                raise SourceArchiveError("privacy_rejected")
            _screen(content)
            element = ET.fromstring(content)
            pending = [(element, 1)]
            count = 0
            while pending:
                node, depth = pending.pop()
                count += 1
                if depth > 64 or count > 100_000:
                    raise SourceArchiveError("limit_exceeded")
                if node.text:
                    _screen(node.text)
                if node.tail:
                    _screen(node.tail)
                for attribute in node.attrib.values():
                    _screen(attribute)
                pending.extend((child, depth + 1) for child in node)
                if node.tag == f"{{{REL}}}Relationship" and node.get("TargetMode") == "External":
                    if node.get("Type") != R + "/hyperlink":
                        raise SourceArchiveError("capability_unavailable")
            # Screen adjacent runs as well as individual XML nodes.
            _screen("".join(element.itertext()))
            result[name] = element
    for name, element in result.items():
        if not name.endswith(".rels"):
            continue
        relation_path = PurePosixPath(name)
        source = str(relation_path.parent.parent / relation_path.name.removesuffix(".rels"))
        for _, relation in _relationship_nodes(element):
            if relation.get("Type") == R + "/image":
                if relation.get("TargetMode", "Internal") != "Internal":
                    raise SourceArchiveError("capability_unavailable")
                _relationship_target(source, text(relation.get("Target"), maximum=512), images)
            elif relation.get("Type") == R + "/printerSettings":
                if relation.get("TargetMode", "Internal") != "Internal":
                    raise SourceArchiveError("capability_unavailable")
                _relationship_target(
                    source, text(relation.get("Target"), maximum=512), printer_settings
                )
    _empty_bibliographies(result)
    return result, image_frames


def _part(parts: dict[str, ET.Element], name: str) -> ET.Element:
    try:
        return parts[name]
    except KeyError:
        raise SourceArchiveError("capture_incomplete") from None


def _contains(root: ET.Element, namespace: str, *names: str) -> bool:
    # Empty XML elements are falsey on older Python versions. Test presence,
    # never element truthiness, for filters, revision markers, or visual context.
    return any(next(root.iter(f"{{{namespace}}}{name}"), None) is not None for name in names)


def _relationship_target(source: str, target: str, available: Collection[str]) -> str:
    if "\\" in target or ":" in target or "?" in target or "#" in target or "%" in target:
        raise SourceArchiveError()
    resolved = posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent), target))
    if target.startswith("/") and not target.startswith("//"):
        resolved = resolved.removeprefix("/")
    if resolved.startswith(("/", "../")) or resolved not in available:
        raise SourceArchiveError("capture_incomplete")
    return resolved


def _relationship_nodes(element: ET.Element) -> Iterator[tuple[str, ET.Element]]:
    seen: set[str] = set()
    for relation in element:
        identity = text(relation.get("Id"), maximum=256)
        if identity in seen:
            raise SourceArchiveError()
        seen.add(identity)
        yield identity, relation


def _relationships(parts: dict[str, ET.Element], source: str, kind: str) -> dict[str, str]:
    path = PurePosixPath(source)
    relations = parts.get(str(path.parent / "_rels" / (path.name + ".rels")))
    result: dict[str, str] = {}
    if relations is None:
        return result
    for identity, relation in _relationship_nodes(relations):
        if (
            relation.get("Type") != R + "/" + kind
            or relation.get("TargetMode", "Internal") != "Internal"
        ):
            continue
        target = text(relation.get("Target"), maximum=512)
        result[identity] = _relationship_target(source, target, parts)
    return result


def _empty_bibliographies(parts: dict[str, ET.Element]) -> None:
    """Allow screened empty Word template stores, not arbitrary custom XML data.

    The bibliography and its properties remain in the exact original. They add
    no reading units because an empty store contains no bibliographic evidence.
    Populated stores need a separate context-preserving reader, not silent omission.
    """
    custom = {name for name in parts if name.startswith("customXml/")}
    checked: set[str] = set()
    for name in sorted(custom):
        root = parts[name]
        if root.tag != f"{{{BIB}}}Sources":
            continue
        if (
            len(PurePosixPath(name).parts) != 2
            or len(root)
            or (root.text or "").strip()
            or root.attrib.keys() - {"SelectedStyle", "StyleName", "Version"}
        ):
            raise SourceArchiveError("capability_unavailable")
        relation_name = str(
            PurePosixPath(name).parent / "_rels" / (PurePosixPath(name).name + ".rels")
        )
        relations = _part(parts, relation_name)
        properties = _relationships(parts, name, "customXmlProps")
        if (
            relations.tag != f"{{{REL}}}Relationships"
            or relations.attrib
            or len(relations) != 1
            or len(properties) != 1
            or any((value or "").strip() for value in relations.itertext())
            or len(relations[0])
            or relations[0].attrib.keys() - {"Id", "Type", "Target", "TargetMode"}
        ):
            raise SourceArchiveError("capability_unavailable")
        target = next(iter(properties.values()))
        props = _part(parts, target)
        nodes = list(props.iter())
        if (
            target not in custom
            or target == name
            or len(nodes) != 3
            or [node.tag for node in nodes]
            != [f"{{{CUSTOM}}}{tag}" for tag in ("datastoreItem", "schemaRefs", "schemaRef")]
            or len(props) != 1
            or len(nodes[1]) != 1
            or props.attrib.keys() != {f"{{{CUSTOM}}}itemID"}
            or re.fullmatch(
                r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}",
                props.get(f"{{{CUSTOM}}}itemID", ""),
            )
            is None
            or nodes[1].attrib
            or nodes[2].attrib != {f"{{{CUSTOM}}}uri": BIB}
            or any((value or "").strip() for value in props.itertext())
        ):
            raise SourceArchiveError("capability_unavailable")
        checked.update((name, relation_name, target))
    if custom != checked:
        raise SourceArchiveError("capability_unavailable")


def _word_text(element: ET.Element) -> str:
    if element.tag in (f"{{{W}}}t", f"{{{W}}}delText"):
        return element.text or ""
    if element.tag == f"{{{W}}}tab":
        return "\t"
    if element.tag in (f"{{{W}}}br", f"{{{W}}}cr"):
        return "\n"
    if element.tag in (f"{{{W}}}footnoteReference", f"{{{W}}}endnoteReference"):
        kind = element.tag.rsplit("}", 1)[1].removesuffix("Reference")
        identity = text(element.get(f"{{{W}}}id"), maximum=128)
        return f"[{kind}:{identity}]"
    value = "".join(_word_text(child) for child in element)
    if element.tag in (f"{{{W}}}ins", f"{{{W}}}del", f"{{{W}}}moveFrom", f"{{{W}}}moveTo"):
        return f"[{element.tag.rsplit('}', 1)[1]}: {value}]"
    return value


def _docx(parts: dict[str, ET.Element], gaps: set[str]) -> tuple[SourceUnit, ...]:
    root = _part(parts, "word/document.xml")
    body = root.find(f"{{{W}}}body")
    if body is None:
        raise SourceArchiveError()
    units: list[SourceUnit] = []
    paragraph = table = 0
    for item in body:
        if item.tag == f"{{{W}}}p":
            paragraph += 1
            _append(
                units,
                _word_text(item),
                "section",
                SourceLocation("paragraph", "word/document.xml", paragraph, paragraph),
            )
        elif item.tag == f"{{{W}}}tbl":
            table += 1
            header = ""
            for row, node in enumerate(item.findall(f"{{{W}}}tr"), 1):
                cells = node.findall(f"{{{W}}}tc")
                if not cells:
                    raise SourceArchiveError()
                content = "\n".join(
                    f"C{index}: " + "\n".join(_word_text(p) for p in cell.findall(f"{{{W}}}p"))
                    for index, cell in enumerate(cells, 1)
                )
                if row == 1:
                    header = content
                context = f"First row context:\n{header}\n" if row > 1 else ""
                _append(
                    units,
                    context + content,
                    "row",
                    SourceLocation("cell", f"table:{table}", row, row, 1, len(cells)),
                )
            gaps.add("table_layout_requires_check")
        elif item.tag != f"{{{W}}}sectPr":
            gaps.add("unsupported_document_element")
    if _contains(root, W, "ins", "del"):
        gaps.add("unresolved_suggestions")
    if any(_contains(part, W, "moveFrom", "moveTo") for part in parts.values()):
        gaps.add("unresolved_suggestions")
    for name, element in parts.items():
        if name in ("word/footnotes.xml", "word/endnotes.xml"):
            for note in element:
                identity = note.get(f"{{{W}}}id")
                if identity is None or identity.startswith("-"):
                    continue
                for number, paragraph_node in enumerate(note.findall(f"{{{W}}}p"), 1):
                    _append(
                        units,
                        _word_text(paragraph_node),
                        "section",
                        SourceLocation("paragraph", f"{name}:{identity}", number, number),
                    )
        elif name.startswith("word/") and any(
            label in name for label in ("comments", "header", "footer")
        ):
            gaps.add("annotation_context_requires_check")
    available = {unit.location.container for unit in units if unit.location is not None}
    for kind in ("footnote", "endnote"):
        references = {node.get(f"{{{W}}}id") for node in root.iter(f"{{{W}}}{kind}Reference")}
        if any(f"word/{kind}s.xml:{identity}" not in available for identity in references):
            gaps.add("note_context_unavailable")
    if _contains(root, W, "drawing", "pict"):
        gaps.add("visual_context_required")
    if _contains(root, W, "fldChar", "instrText"):
        gaps.add("field_result_unverified")
    return tuple(units)


def _cell_position(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", value)
    if match is None:
        raise SourceArchiveError()
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise SourceArchiveError("limit_exceeded")
    return row, column


def _saved_cell(cell: ET.Element, strings: tuple[str, ...], gaps: set[str]) -> str:
    value = cell.findtext(f"{{{S}}}v")
    kind = cell.get("t", "n")
    if kind == "s":
        if (
            value is None
            or not value.isascii()
            or not value.isdigit()
            or int(value) >= len(strings)
        ):
            raise SourceArchiveError()
        saved = strings[int(value)]
    elif kind == "inlineStr":
        saved = "".join(node.text or "" for node in cell.iter(f"{{{S}}}t"))
    elif kind in ("n", "str", "b", "e", "d"):
        saved = value or ""
        if (
            kind == "n"
            and saved
            and not re.fullmatch(
                r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]{1,3})?", saved
            )
        ):
            raise SourceArchiveError()
        if kind == "b" and saved not in ("0", "1", ""):
            raise SourceArchiveError()
        if kind == "e":
            gaps.add("formula_error")
    else:
        raise SourceArchiveError()
    result = f"saved {kind}: {saved}"
    formula = cell.find(f"{{{S}}}f")
    if formula is not None:
        expression = formula.text
        if expression is None or not expression.strip():
            result = "formula: unavailable; " + result
            gaps.add("formula_expression_unavailable")
        else:
            result = f"formula: ={expression}; " + result
        gaps.add("formula_cache_freshness_unknown")
        if value is None:
            gaps.add("formula_result_unavailable")
        if formula.get("t", "normal") != "normal":
            gaps.add("shared_or_array_formula_requires_check")
        if "[" in (formula.text or ""):
            gaps.add("external_formula_dependency")
    if kind in ("n", "d") or cell.get("s") is not None:
        gaps.add("displayed_value_unavailable")
        result += "; style: " + cell.get("s", "default")
    return result


def _xlsx(parts: dict[str, ET.Element], gaps: set[str]) -> tuple[SourceUnit, ...]:
    root = _part(parts, "xl/workbook.xml")
    relations = _relationships(parts, "xl/workbook.xml", "worksheet")
    shared = parts.get("xl/sharedStrings.xml")
    strings = (
        tuple("".join(node.text or "" for node in item.iter(f"{{{S}}}t")) for item in shared)
        if shared is not None
        else ()
    )
    units: list[SourceUnit] = []
    seen: set[str] = set()
    names: set[str] = set()
    for sheet in root.iter(f"{{{S}}}sheet"):
        identity = text(sheet.get("sheetId"), maximum=128)
        name = text(sheet.get("name"), maximum=256)
        # Formulas address worksheet names, not just the sheetId used by anchors.
        # Refuse ambiguous targets rather than choosing one or letting a renderer
        # silently repair the workbook into a different interpretation.
        if identity in seen or name.casefold() in names:
            raise SourceArchiveError()
        seen.add(identity)
        names.add(name.casefold())
        relation = text(sheet.get(f"{{{R}}}id"), maximum=256)
        worksheet = _part(parts, relations.get(relation, ""))
        if _contains(worksheet, S, "autoFilter"):
            gaps.add("filter_view_state_requires_check")
        if _contains(worksheet, S, "drawing", "mergeCells", "conditionalFormatting"):
            gaps.add("sheet_visual_context_requires_check")
        header = ""
        previous = 0
        for row in worksheet.iter(f"{{{S}}}row"):
            number = int(text(row.get("r"), maximum=7))
            if number <= previous:
                raise SourceArchiveError()
            previous = number
            contents: list[str] = []
            columns: list[int] = []
            for cell in row.findall(f"{{{S}}}c"):
                address = text(cell.get("r"), maximum=10)
                r, column = _cell_position(address)
                if r != number or (columns and column <= columns[-1]):
                    raise SourceArchiveError()
                columns.append(column)
                contents.append(address + ": " + _saved_cell(cell, strings, gaps))
            if not columns:
                continue
            content = "\n".join(contents)
            if number == 1:
                header = content
            elif not header:
                gaps.add("header_context_unavailable")
            banner = f"Sheet: {name}; state: {sheet.get('state', 'visible')}; hidden row: {row.get('hidden', '0')}\n"
            context = f"Headers:\n{header}\n" if number > 1 and header else ""
            _append(
                units,
                banner + context + content,
                "row",
                SourceLocation("cell", identity, number, number, columns[0], columns[-1]),
            )
    gaps.add("workbook_locale_timezone_unknown")
    return tuple(units)


def _pptx(parts: dict[str, ET.Element], gaps: set[str]) -> tuple[SourceUnit, ...]:
    root = _part(parts, "ppt/presentation.xml")
    relations = _relationships(parts, "ppt/presentation.xml", "slide")
    units: list[SourceUnit] = []
    seen: set[str] = set()
    for number, identity_node in enumerate(root.iter(f"{{{P}}}sldId"), 1):
        identity = text(identity_node.get("id"), maximum=128)
        if identity in seen:
            raise SourceArchiveError()
        seen.add(identity)
        relation = text(identity_node.get(f"{{{R}}}id"), maximum=256)
        part_name = relations.get(relation, "")
        slide = _part(parts, part_name)
        content = "\n".join(
            "".join(node.text or "" for node in paragraph.iter(f"{{{A}}}t"))
            for paragraph in slide.iter(f"{{{A}}}p")
        )
        if _contains(slide, P, "graphicFrame", "cxnSp", "pic"):
            gaps.add("visual_context_required")
        if _contains(slide, P, "timing", "transition"):
            gaps.add("animation_context_unavailable")
        notes = _relationships(parts, part_name, "notesSlide")
        if not notes:
            gaps.add("speaker_notes_availability_unknown")
        for note_name in notes.values():
            note = _part(parts, note_name)
            bodies = [
                shape
                for shape in note.iter(f"{{{P}}}sp")
                if any(ph.get("type") == "body" for ph in shape.iter(f"{{{P}}}ph"))
            ]
            for body in bodies:
                content += "\nSpeaker notes:\n" + "\n".join(
                    "".join(node.text or "" for node in paragraph.iter(f"{{{A}}}t"))
                    for paragraph in body.iter(f"{{{A}}}p")
                )
        if not content.strip():
            content = "No readable text; visual source required."
            gaps.add("visual_context_required")
        _append(units, content, "slide", SourceLocation("slide", identity, number, number))
    return tuple(units)


def _check_renderable(parts: dict[str, ET.Element]) -> None:
    """A renderer interprets fields/formulas that the text reader only quotes.

    Keep a finite calculation subset; unknown functions/fields are still readable
    as inert evidence but cannot be passed to an executing Office application.
    """
    functions = {
        "SUM",
        "AVERAGE",
        "MIN",
        "MAX",
        "COUNT",
        "COUNTA",
        "IF",
        "AND",
        "OR",
        "NOT",
        "ROUND",
        "ABS",
    }
    for root in parts.values():
        for node in root.iter():
            if node.tag in (f"{{{W}}}instrText", f"{{{W}}}fldSimple"):
                field = node.get(f"{{{W}}}instr", node.text or "").strip()
                if field not in ("PAGE", "NUMPAGES"):
                    raise SourceArchiveError("capability_unavailable")
            if node.tag in (f"{{{S}}}f", f"{{{S}}}definedName"):
                expression = node.text or ""
                # No remote resources, dynamic names, extension/add-in calls or
                # macro commands. Quoted literal text is inert in this subset.
                plain = re.sub(r'"(?:[^"\r\n]|"")*"', '""', expression)
                calls = re.findall(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", plain)
                if any(call.upper() not in functions for call in calls) or any(
                    marker in plain for marker in ("[", "]", "|", "\\", ";", "{", "}", "@")
                ):
                    raise SourceArchiveError("capability_unavailable")


def read_office(
    payload: bytes,
    kind: str,
    *,
    maximum_bytes: int = PDF_BYTES,
    inspect_image: Callable[[bytes, str], int] | None = None,
    for_rendering: bool = False,
    sheet_excerpt: bool = False,
) -> DocumentReading:
    positive(maximum_bytes, maximum=PDF_BYTES)
    if kind not in ("docx", "xlsx", "pptx"):
        raise SourceArchiveError("capability_unavailable")
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise SourceArchiveError("limit_exceeded")
    try:
        gaps: set[str] = set()
        parts, images = _package(payload, inspect_image, gaps)
        if sheet_excerpt:
            # The only qualified excerpt shape is a directly copied literal
            # sheet. Do not infer safe dependency closure from cached values.
            if kind != "xlsx" or (
                len(tuple(_part(parts, "xl/workbook.xml").iter(f"{{{S}}}sheet"))) != 1
                or any(_contains(part, S, "f", "definedName") for part in parts.values())
            ):
                raise SourceArchiveError("capture_incomplete")
        if for_rendering:
            _check_renderable(parts)
        if kind == "docx":
            units = _docx(parts, gaps)
        elif kind == "xlsx":
            units = _xlsx(parts, gaps)
        else:
            units = _pptx(parts, gaps)
        visual_units: list[SourceUnit] = []
        for name, frames in images:
            for frame in range(1, frames + 1):
                position = len(units) + len(visual_units) + 1
                visual_units.append(
                    SourceUnit(
                        f"u{position}",
                        f"Embedded image, frame {frame} of {frames}. Visual evidence—not document page layout.",
                        position,
                        "image_page",
                        location=SourceLocation("page", name, frame, frame),
                    )
                )
        units += tuple(visual_units)
        gaps.add("office_layout_not_rendered")
    except SourceArchiveError:
        raise
    except (
        zipfile.BadZipFile,
        ET.ParseError,
        UnicodeError,
        ValueError,
        OverflowError,
        RecursionError,
        struct.error,
        zlib.error,
    ):
        raise SourceArchiveError() from None
    reader = (
        "tedd-ooxml:8"
        if kind == "docx"
        and any(_contains(part, W, "moveFrom", "moveTo") for part in parts.values())
        else "tedd-ooxml:7"
        if kind == "docx"
        and any(
            _contains(part, W, "footnoteReference", "endnoteReference") for part in parts.values()
        )
        else "tedd-ooxml:6"
        if images or "print_metadata_not_document_evidence" in gaps
        else "tedd-ooxml:5"
        if any(name.startswith("customXml/") for name in parts)
        else "tedd-ooxml:2"
    )
    return DocumentReading(digest(payload), units, reader, tuple(sorted(gaps)), kind=kind)
