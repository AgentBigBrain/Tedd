"""Bounded, inert conversion of explicitly acquired workflow representations.

This module never acquires files, follows links, authenticates a provider, or
publishes an archive. The caller retains exact bytes separately from this reading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email import policy
from email.errors import MessageDefect, MessageError
from email.parser import BytesParser
from typing import NoReturn, cast

from .legacy_values import secret_hits
from .source_contracts import (
    CHAT_BYTES,
    MAX_UNITS,
    ArchiveLimits,
    SourceArchiveError,
    SourceLocation,
    SourceUnit,
    check_json_bounds,
    digest,
    encode_units,
    positive,
    sha256,
    text,
    unique_object,
)

UNIT_CHARS = 6000
PLAIN_KINDS = frozenset({"text", "markdown", "code", "tool_result", "web"})


@dataclass(frozen=True, repr=False)
class DocumentReading:
    original_sha256: str
    units: tuple[SourceUnit, ...]
    reader_id: str
    gaps: tuple[str, ...] = ()
    kind: str = "text"

    def __post_init__(self) -> None:
        sha256(self.original_sha256)
        ArchiveLimits().for_kind(self.kind)
        text(self.reader_id, maximum=256)
        if type(self.gaps) is not tuple or len(self.gaps) > 32:
            raise SourceArchiveError("limit_exceeded")
        for gap in self.gaps:
            text(gap, maximum=128)
        encode_units(self.units)


def _screen(value: str) -> str:
    text(value, maximum=CHAT_BYTES, empty=True)
    if any((ord(char) < 32 and char not in "\t\n\r") or ord(char) == 127 for char in value):
        raise SourceArchiveError("privacy_rejected")
    return value


def _lines(value: str, *, maximum: int = UNIT_CHARS) -> tuple[tuple[str, int, int], ...]:
    if not value.strip():
        raise SourceArchiveError("capture_incomplete")
    result: list[tuple[str, int, int]] = []
    first, end, size = 1, 0, 0
    chunk: list[str] = []
    for number, line in enumerate(value.splitlines(keepends=True), 1):
        if len(line) > maximum:
            raise SourceArchiveError("limit_exceeded")
        if chunk and size + len(line) > maximum:
            result.append(("".join(chunk), first, end))
            first, size, chunk = number, 0, []
        chunk.append(line)
        size += len(line)
        end = number
        if len(result) >= MAX_UNITS:
            raise SourceArchiveError("limit_exceeded")
    result.append(("".join(chunk), first, end))
    return tuple(result)


def _email(payload: bytes) -> tuple[SourceUnit, ...]:
    # Do not retain opaque MIME parts under a claim that their bytes were screened.
    # Multipart/HTML acquisition remains unavailable until that route is qualified.
    try:
        message = BytesParser(policy=policy.default.clone(raise_on_defect=True)).parsebytes(payload)
        if (
            message.is_multipart()
            or message.get_content_type() != "text/plain"
            or message.get_content_disposition() == "attachment"
            or message.get_filename() is not None
        ):
            raise SourceArchiveError("capability_unavailable")
        headers = list(message.items())
        if len(headers) > 64:
            raise SourceArchiveError("limit_exceeded")
        header_text = _screen("".join(f"{key}: {value}\n" for key, value in headers))
        if len(header_text) > 2000:
            raise SourceArchiveError("limit_exceeded")
        body = message.get_payload(decode=True)
        if not isinstance(body, bytes):
            raise SourceArchiveError()
        content = _screen(body.decode(message.get_content_charset() or "ascii", errors="strict"))
        source_id = str(message["Message-ID"]) if "Message-ID" in message else None
        speaker = str(message["From"]) if "From" in message else None
        reply = str(message["In-Reply-To"]) if "In-Reply-To" in message else None
        when = str(message["Date"]) if "Date" in message else None
    except (MessageError, MessageDefect, UnicodeError, LookupError, ValueError) as error:
        if isinstance(error, SourceArchiveError):
            raise
        raise SourceArchiveError() from None
    return tuple(
        SourceUnit(
            f"u{index}",
            header_text + "\n" + chunk,
            index,
            "message",
            source_id=source_id,
            speaker=speaker,
            source_time=when,
            reply_to=reply,
            location=SourceLocation("message", source_id, 1, 1),
        )
        for index, (chunk, _, _) in enumerate(
            _lines(content, maximum=UNIT_CHARS - len(header_text) - 1), 1
        )
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SourceArchiveError("capture_incomplete")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > MAX_UNITS:
        raise SourceArchiveError("capture_incomplete")
    return cast(list[object], value)


def _zero_index(value: object) -> int:
    if type(value) is not int or not 0 <= value < 1_000_000_000:
        raise SourceArchiveError()
    return value


class _ProviderNumber(str):
    """Distinguish a JSON numeric token from a string in a numeric provider field."""


def _number(raw: str) -> _ProviderNumber:
    # Preserve provider numeric tokens as text, never binary floats or evaluated
    # formulas. Native index fields still require bounded integers separately.
    if len(raw) > 128:
        raise SourceArchiveError("limit_exceeded")
    return _ProviderNumber(raw)


def _integer(raw: str) -> int:
    if len(raw.lstrip("-")) > 19:
        raise SourceArchiveError("limit_exceeded")
    return int(raw)


def _constant(_raw: str) -> NoReturn:
    raise SourceArchiveError()


def _provider_json(payload: bytes) -> dict[str, object]:
    check_json_bounds(payload, CHAT_BYTES)
    try:
        value = _object(
            json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=unique_object,
                parse_float=_number,
                parse_int=_integer,
                parse_constant=_constant,
            )
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, SourceArchiveError):
            raise
        raise SourceArchiveError() from None
    if secret_hits(value):
        raise SourceArchiveError("privacy_rejected")
    return value


def _append(units: list[SourceUnit], value: str, kind: str, location: SourceLocation) -> None:
    _screen(value)
    if not value.strip():
        return
    if len(value) > UNIT_CHARS or len(units) >= MAX_UNITS:
        raise SourceArchiveError("limit_exceeded")
    index = len(units) + 1
    units.append(SourceUnit(f"u{index}", value, index, kind, location=location))


def _doc_text(paragraph: dict[str, object], gaps: set[str]) -> str:
    parts: list[str] = []
    for value in _array(paragraph.get("elements", [])):
        element = _object(value)
        if "textRun" in element:
            run = _object(element["textRun"])
            parts.append(text(run.get("content"), maximum=UNIT_CHARS, empty=True))
        else:
            # An inline object or footnote marker cannot disappear silently.
            gaps.add("inline_context_requires_check")
    return "".join(parts)


def _has_suggestions(value: object) -> bool:
    # JSON byte/depth bounds were checked before this walk. Suggestions can live
    # on a run, paragraph, table, or document style—not only a paragraph element.
    if isinstance(value, dict):
        return any(
            (str(key).startswith("suggested") and bool(item)) or _has_suggestions(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_has_suggestions(item) for item in value)


def _doc_body(content: object, container: str, units: list[SourceUnit], gaps: set[str]) -> None:
    for value in _array(content):
        element = _object(value)
        if "sectionBreak" in element:
            continue
        if "paragraph" in element:
            paragraph = _object(element["paragraph"])
            value_text = _doc_text(paragraph, gaps)
            if value_text.strip():
                _append(
                    units,
                    value_text,
                    "section",
                    SourceLocation(
                        "utf16",
                        container,
                        _zero_index(element.get("startIndex", 0)) + 1,
                        positive(element.get("endIndex")),
                    ),
                )
        elif "table" in element:
            # Keep every cell, including nested tables, in order. Until paired
            # table/header ranges are qualified this reading is explicitly partial.
            gaps.add("table_layout_requires_check")
            for row_value in _array(_object(element["table"]).get("tableRows")):
                for cell in _array(_object(row_value).get("tableCells")):
                    _doc_body(_object(cell).get("content"), container, units, gaps)
        else:
            gaps.add("unsupported_document_element")


def _google_doc(value: dict[str, object]) -> tuple[tuple[SourceUnit, ...], tuple[str, ...]]:
    units: list[SourceUnit] = []
    gaps: set[str] = set()
    if _has_suggestions(value):
        gaps.add("unresolved_suggestions")
    seen: set[str] = set()
    pending = list(reversed(_array(value.get("tabs"))))
    while pending:
        tab_value = pending.pop()
        tab = _object(tab_value)
        native = "documentTab" in tab or "tabProperties" in tab
        tab_id = text(
            _object(tab.get("tabProperties")).get("tabId") if native else tab.get("tabId"),
            maximum=256,
        )
        if tab_id in seen or len(seen) >= MAX_UNITS:
            raise SourceArchiveError()
        seen.add(tab_id)
        pending.extend(reversed(_array(tab.get("childTabs", []))))
        content = _object(tab.get("documentTab")) if native else tab
        _doc_body(_object(content.get("body")).get("content"), tab_id, units, gaps)
        footnotes = content.get("footnotes")
        if footnotes is None:
            gaps.add("footnote_availability_unknown")
        else:
            for key, note in _object(footnotes).items():
                _doc_body(_object(note).get("content"), f"{tab_id}:{key}", units, gaps)
        if content.get("headers") or content.get("footers"):
            gaps.add("header_footer_context_requires_check")
    return tuple(units), tuple(sorted(gaps))


def _cell(value: object, gaps: set[str]) -> str:
    cell = _object(value)
    parts: list[str] = []
    for label, key in (("entered", "userEnteredValue"), ("effective", "effectiveValue")):
        if key in cell:
            data = _object(cell[key])
            if len(data) != 1:
                raise SourceArchiveError()
            kind, item = next(iter(data.items()))
            if kind not in (
                "stringValue",
                "numberValue",
                "boolValue",
                "formulaValue",
                "errorValue",
            ):
                raise SourceArchiveError()
            if kind in ("stringValue", "formulaValue"):
                text(item, maximum=UNIT_CHARS, empty=True)
            elif kind == "numberValue":
                if type(item) is not int and not isinstance(item, _ProviderNumber):
                    raise SourceArchiveError()
            elif kind == "boolValue":
                if type(item) is not bool:
                    raise SourceArchiveError()
            else:
                error = _object(item)
                text(error.get("type"), maximum=128)
                text(error.get("message", ""), maximum=UNIT_CHARS, empty=True)
            parts.append(f"{label} {kind}: {item}")
            if kind == "formulaValue":
                if "effectiveValue" not in cell:
                    gaps.add("formula_result_unavailable")
                # This is a bounded warning heuristic, not an exhaustive formula
                # classifier. The formula remains inert source text; known import
                # functions and explicit URLs keep external context visibly partial.
                formula = str(item).upper()
                if any(
                    marker in formula
                    for marker in (
                        "IMPORTRANGE(",
                        "IMPORTXML(",
                        "IMPORTDATA(",
                        "IMPORTFEED(",
                        "IMPORTHTML(",
                        "GOOGLEFINANCE(",
                        "IMAGE(",
                        "HTTP://",
                        "HTTPS://",
                    )
                ):
                    gaps.add("external_formula_dependency")
    if "formattedValue" in cell:
        parts.append("display: " + text(cell["formattedValue"], maximum=UNIT_CHARS, empty=True))
    if "effectiveFormat" in cell:
        number_format = _object(cell["effectiveFormat"]).get("numberFormat")
        if number_format:
            parts.append("number format: " + str(number_format))
    if cell.get("note"):
        parts.append("note: " + text(cell["note"], maximum=UNIT_CHARS))
    return "; ".join(parts) or "blank"


def _google_sheet(value: dict[str, object]) -> tuple[tuple[SourceUnit, ...], tuple[str, ...]]:
    units: list[SourceUnit] = []
    gaps: set[str] = set()
    workbook = _object(value.get("properties", {}))
    locale = text(workbook.get("locale", "unknown"), maximum=128)
    timezone = text(workbook.get("timeZone", "unknown"), maximum=128)
    seen: set[str] = set()
    for sheet_value in _array(value.get("sheets")):
        sheet = _object(sheet_value)
        properties = _object(sheet.get("properties"))
        sheet_id = str(_zero_index(properties.get("sheetId")))
        if sheet_id in seen:
            raise SourceArchiveError()
        seen.add(sheet_id)
        title = text(properties.get("title"), maximum=256)
        if sheet.get("basicFilter") or sheet.get("filterViews"):
            gaps.add("filter_view_state_requires_check")
        if sheet.get("charts") or sheet.get("merges") or sheet.get("conditionalFormats"):
            gaps.add("sheet_visual_context_requires_check")
        for block_value in _array(sheet.get("data")):
            block = _object(block_value)
            start = _zero_index(block.get("startRow", 0))
            column = _zero_index(block.get("startColumn", 0)) + 1
            headers = ""
            if start:
                gaps.add("header_context_unavailable")
            for offset, row_value in enumerate(_array(block.get("rowData"))):
                cells = _array(_object(row_value).get("values", []))
                if not cells:
                    continue
                row = start + offset + 1
                contents = "\n".join(
                    f"C{column + index}: {_cell(cell, gaps)}" for index, cell in enumerate(cells)
                )
                if not start and not offset:
                    headers = contents
                banner = (
                    f"Sheet: {title}; row {row}; hidden sheet: {properties.get('hidden', False)}; "
                    f"locale: {locale}; time zone: {timezone}\n"
                )
                metadata = block.get("rowMetadata", [])
                if isinstance(metadata, list) and offset < len(metadata):
                    flags = _object(metadata[offset])
                    if flags.get("hiddenByUser") or flags.get("hiddenByFilter"):
                        banner += "Row hidden in provider view.\n"
                context = "Headers:\n" + headers + "\n" if offset and headers else ""
                _append(
                    units,
                    banner + context + contents,
                    "row",
                    SourceLocation(
                        "cell",
                        sheet_id,
                        row,
                        row,
                        column,
                        column + len(cells) - 1,
                    ),
                )
    return tuple(units), tuple(sorted(gaps))


def _slide_text(elements: object, gaps: set[str]) -> str:
    parts: list[str] = []
    for value in _array(elements):
        element = _object(value)
        if "shape" in element:
            shape = _object(element["shape"])
            if shape.get("shapeType", "TEXT_BOX") != "TEXT_BOX" and not shape.get("placeholder"):
                gaps.add("visual_context_required")
            if "text" in shape:
                runs: list[str] = []
                for run_value in _array(_object(shape["text"]).get("textElements", [])):
                    run = _object(run_value)
                    if "textRun" in run:
                        runs.append(
                            text(
                                _object(run["textRun"]).get("content"),
                                maximum=UNIT_CHARS,
                                empty=True,
                            )
                        )
                parts.append("".join(runs))
            else:
                gaps.add("visual_context_required")
        elif "elementGroup" in element:
            parts.append(_slide_text(_object(element["elementGroup"]).get("children"), gaps))
        elif "sheetsChart" in element:
            gaps.add("linked_chart_unavailable")
        else:
            gaps.add("visual_context_required")
    return "\n".join(parts)


def _google_slide(value: dict[str, object]) -> tuple[tuple[SourceUnit, ...], tuple[str, ...]]:
    units: list[SourceUnit] = []
    gaps: set[str] = set()
    seen: set[str] = set()
    for number, slide_value in enumerate(_array(value.get("slides")), 1):
        slide = _object(slide_value)
        identity = text(slide.get("objectId"), maximum=256)
        if identity in seen:
            raise SourceArchiveError()
        seen.add(identity)
        content = _slide_text(slide.get("pageElements", []), gaps)
        properties = _object(slide.get("slideProperties", {}))
        if "notesPage" in properties:
            notes = _object(properties["notesPage"])
            speaker = _object(notes.get("notesProperties", {})).get("speakerNotesObjectId")
            selected = [
                element
                for element in _array(notes.get("pageElements", []))
                if _object(element).get("objectId") == speaker
            ]
            if selected:
                notes_text = _slide_text(selected, gaps)
                if notes_text.strip():
                    content += "\nSpeaker notes:\n" + notes_text
            elif speaker is not None:
                gaps.add("speaker_notes_unavailable")
        else:
            gaps.add("speaker_notes_availability_unknown")
        if not content.strip():
            content = "No readable text; visual source required."
            gaps.add("visual_context_required")
        _append(units, content, "slide", SourceLocation("slide", identity, number, number))
    return tuple(units), tuple(sorted(gaps))


def read_document(payload: bytes, kind: str, *, maximum_bytes: int = CHAT_BYTES) -> DocumentReading:
    positive(maximum_bytes, maximum=CHAT_BYTES)
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise SourceArchiveError("limit_exceeded")
    if kind == "email":
        units = _email(payload)
        reader = "tedd-email:1"
        gaps: tuple[str, ...] = ()
    elif kind in ("google_doc", "google_sheet", "google_slide"):
        value = _provider_json(payload)
        if kind == "google_doc":
            units, gaps = _google_doc(value)
        elif kind == "google_sheet":
            units, gaps = _google_sheet(value)
        else:
            units, gaps = _google_slide(value)
        reader = "tedd-" + kind.replace("_", "-") + ":2"
    elif kind in PLAIN_KINDS:
        try:
            content = _screen(payload.decode("utf-8", errors="strict"))
        except UnicodeError:
            raise SourceArchiveError() from None
        if re.search(
            r"(?m)^(?:<!-- codex-generated:v2:[0-9a-f]{64} -->|"
            r"# codex-generated:v2:[0-9a-f]{64}|x-codex-generated: v2:[0-9a-f]{64})\r?$",
            content,
        ):
            # A copied generated note is still a derivative, even outside its vault.
            raise SourceArchiveError("scope_denied")
        units = tuple(
            SourceUnit(
                f"u{index}",
                chunk,
                index,
                "receipt" if kind == "tool_result" else "line",
                location=SourceLocation("line", None, start, end),
            )
            for index, (chunk, start, end) in enumerate(_lines(content), 1)
        )
        reader = "tedd-text:1"
        gaps = ()
    else:
        raise SourceArchiveError("capability_unavailable")
    return DocumentReading(digest(payload), units, reader, gaps, kind=kind)
