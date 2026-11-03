"""Bounded inert Office metadata; never a renderer, executable or access grant.

ICC matrix/TRC profiles use the ICC.1 tag layout. Unknown tags/types remain
unsupported. Text is screened before retaining the private original; numerical
fields are not proof against covert data. No metadata is copied into previews.
"""

from __future__ import annotations

import math
import struct
from xml.etree import ElementTree as ET

from .source_contracts import SourceArchiveError
from .source_documents import _screen


def _u32(raw: bytes, offset: int) -> int:
    return int(struct.unpack_from(">I", raw, offset)[0])


def _metadata_text(raw: bytes, encoding: str = "ascii") -> None:
    # NUL padding is permitted only after a string, not between secret characters.
    value = raw.decode(encoding, errors="strict").rstrip("\0")
    if len(value) > 8192:
        raise SourceArchiveError("limit_exceeded")
    _screen(value)


def _icc_text(raw: bytes) -> None:
    kind = raw[:4]
    if kind == b"text":
        if not raw.endswith(b"\0"):
            raise SourceArchiveError()
        _metadata_text(raw[8:])
    elif kind == b"desc":
        # ICC v2 textDescription: ASCII, optional Unicode, fixed ScriptCode field.
        count = _u32(raw, 8)
        cursor = 12 + count
        if not 1 <= count <= 8192 or cursor + 78 > len(raw) or raw[cursor - 1] != 0:
            raise SourceArchiveError()
        _metadata_text(raw[12:cursor])
        unicode_count = _u32(raw, cursor + 4)
        end = cursor + 8 + unicode_count * 2
        if not end + 70 <= len(raw) <= end + 73 or any(raw[end + 70 :]):
            raise SourceArchiveError()
        _metadata_text(raw[cursor + 8 : end], "utf-16-be")
        script, length = struct.unpack_from(">HB", raw, end)
        if script != 0 or length > 67 or any(raw[end + 3 + length :]):
            raise SourceArchiveError("privacy_rejected")
        _metadata_text(raw[end + 3 : end + 3 + length], "mac_roman")
    elif kind == b"mluc":
        count, size = struct.unpack_from(">II", raw, 8)
        if not 1 <= count <= 64 or size != 12 or 16 + count * size > len(raw):
            raise SourceArchiveError("limit_exceeded")
        claimed = bytearray(len(raw))
        claimed[: 16 + count * size] = b"\1" * (16 + count * size)
        for index in range(count):
            entry = 16 + index * size
            language = raw[entry : entry + 4]
            length, offset = struct.unpack_from(">II", raw, entry + 4)
            if (
                not language.isalpha()
                or length % 2
                or offset < 16 + count * size
                or offset + length > len(raw)
            ):
                raise SourceArchiveError()
            _metadata_text(raw[offset : offset + length], "utf-16-be")
            claimed[offset : offset + length] = b"\1" * length
        if any(value for value, used in zip(raw, claimed, strict=True) if not used):
            raise SourceArchiveError("privacy_rejected")
    else:
        raise SourceArchiveError("capability_unavailable")


def _icc_tag(signature: bytes, raw: bytes) -> None:
    if len(raw) < 8 or any(raw[4:8]):
        raise SourceArchiveError()
    kind = raw[:4]
    if signature in (b"desc", b"dscm", b"cprt", b"dmnd", b"dmdd"):
        _icc_text(raw)
    elif signature in (b"wtpt", b"bkpt", b"rXYZ", b"gXYZ", b"bXYZ", b"lumi"):
        if kind != b"XYZ " or len(raw) != 20:
            raise SourceArchiveError()
    elif signature == b"chad":
        if kind != b"sf32" or len(raw) != 44:
            raise SourceArchiveError()
    elif signature in (b"rTRC", b"gTRC", b"bTRC", b"kTRC"):
        if kind == b"curv":
            count = _u32(raw, 8)
            if count > 4096 or len(raw) != 12 + 2 * count:
                raise SourceArchiveError("limit_exceeded")
        elif kind == b"para":
            function, reserved = struct.unpack_from(">HH", raw, 8)
            sizes = (1, 3, 4, 5, 7)
            if function >= len(sizes) or reserved or len(raw) != 12 + sizes[function] * 4:
                raise SourceArchiveError()
        else:
            raise SourceArchiveError("capability_unavailable")
    elif signature == b"chrm":
        if (
            kind != b"chrm"
            or len(raw) < 12
            or len(raw) != 12 + 8 * int.from_bytes(raw[8:10], "big")
        ):
            raise SourceArchiveError()
    else:
        raise SourceArchiveError("capability_unavailable")


def inspect_icc(value: object) -> None:
    """Validate a small v2/v4 RGB/gray matrix profile, including every text tag."""
    if not isinstance(value, bytes) or not 132 <= len(value) <= 262144:
        raise SourceArchiveError("limit_exceeded")
    try:
        if (
            _u32(value, 0) != len(value)
            or value[8] not in (2, 4)
            or value[12:16] not in (b"mntr", b"scnr", b"prtr", b"spac")
            or value[16:20] not in (b"RGB ", b"GRAY")
            or value[20:24] != b"XYZ "
            or value[36:40] != b"acsp"
            or any(value[100:128])
        ):
            raise SourceArchiveError()
        count = _u32(value, 128)
        if not 1 <= count <= 64 or 132 + count * 12 > len(value):
            raise SourceArchiveError("limit_exceeded")
        signatures: set[bytes] = set()
        spans: set[tuple[int, int]] = set()
        for index in range(count):
            entry = 132 + index * 12
            signature = value[entry : entry + 4]
            offset, size = struct.unpack_from(">II", value, entry + 4)
            if (
                signature in signatures
                or offset % 4
                or offset < 132 + count * 12
                or size < 8
                or offset + size > len(value)
            ):
                raise SourceArchiveError()
            signatures.add(signature)
            spans.add((offset, offset + size))
            _icc_tag(signature, value[offset : offset + size])
        required = {b"desc", b"cprt", b"wtpt"}
        required.update(
            {b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC"}
            if value[16:20] == b"RGB "
            else {b"kTRC"}
        )
        if not required <= signatures:
            raise SourceArchiveError("capability_unavailable")
        cursor = 132 + count * 12
        for start, end in sorted(spans):
            if start < cursor or start - cursor > 3 or any(value[cursor:start]):
                raise SourceArchiveError()
            cursor = end
        if len(value) - cursor > 3 or any(value[cursor:]):
            raise SourceArchiveError()
    except (struct.error, UnicodeError) as error:
        raise SourceArchiveError() from error


def inspect_printer_settings(raw: bytes) -> None:
    """Screen the finite Mac Office print-ticket envelope, never load a driver.

    Windows DEVMODE/private-driver bytes and other binary variants are still
    unsupported. Only two XML print tickets with inert scalar values are admitted.
    """
    if not 20 <= len(raw) <= 131072:
        raise SourceArchiveError("limit_exceeded")
    if raw[:4] != b"\2\0\0\0":
        raise SourceArchiveError("capability_unavailable")
    allowed = {
        "PMPPDPaperCodeName",
        "PMPPDTranslationStringPaperName",
        "PMTiogaPaperName",
        *(
            "com.apple.print." + key
            for key in (
                "PageFormat.PMHorizontalRes",
                "PageFormat.PMVerticalRes",
                "PageFormat.PMOrientation",
                "PageFormat.PMScaling",
                "PageFormat.PMVerticalScaling",
                "PageFormat.PMAdjustedPageRect",
                "PageFormat.PMAdjustedPaperRect",
                "subTicket.paper_info_ticket",
                "PaperInfo.PMPaperName",
                "PaperInfo.PMUnadjustedPageRect",
                "PaperInfo.PMUnadjustedPaperRect",
                "PaperInfo.ppd.PMPaperName",
                "ticket.creator",
                "ticket.itemArray",
                "ticket.stateFlag",
                "ticket.APIVersion",
                "ticket.type",
                "DocumentTicket.PMSpoolFormat",
                "PrintSettings.PMCopies",
                "PrintSettings.PMCopyCollate",
                "PrintSettings.PMFirstPage",
                "PrintSettings.PMLastPage",
                "PrintSettings.PMPageRange",
            )
        ),
    }
    declaration = '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    cursor = 4
    try:
        for expected_kind in (1, 2):
            kind, size = struct.unpack_from("<II", raw, cursor)
            cursor += 8
            if kind != expected_kind or not 1 <= size <= 65536 or cursor + size > len(raw):
                raise SourceArchiveError()
            content = raw[cursor : cursor + size].decode("utf-8", errors="strict")
            cursor += size
            # Expat/ElementTree never fetch the canonical external DTD. Refuse
            # every other declaration/entity before parsing this in-memory copy.
            content = content.replace(declaration, "", 1)
            if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
                raise SourceArchiveError("privacy_rejected")
            _screen(content)
            root = ET.fromstring(content)
            if (
                root.tag != "plist"
                or root.attrib != {"version": "1.0"}
                or len(root) != 1
                or root[0].tag != "dict"
                or (root.text or "").strip()
            ):
                raise SourceArchiveError()
            pending = [(root[0], 1)]
            count = 0
            while pending:
                node, depth = pending.pop()
                count += 1
                if count > 1024 or depth > 16:
                    raise SourceArchiveError("limit_exceeded")
                if node.attrib or node.tag not in {
                    "dict",
                    "array",
                    "key",
                    "string",
                    "integer",
                    "real",
                    "true",
                    "false",
                }:
                    raise SourceArchiveError("capability_unavailable")
                value = node.text or ""
                _screen(value)
                if (node.tail or "").strip():
                    raise SourceArchiveError()
                if node.tag in ("dict", "array"):
                    if value.strip():
                        raise SourceArchiveError()
                elif len(node):
                    raise SourceArchiveError()
                if node.tag == "dict":
                    keys = [child.text for child in node[::2]]
                    if (
                        len(node) % 2
                        or any(child.tag != "key" for child in node[::2])
                        or any(child.tag == "key" for child in node[1::2])
                        or len(keys) != len(set(keys))
                    ):
                        raise SourceArchiveError()
                elif node.tag == "key" and value not in allowed:
                    raise SourceArchiveError("capability_unavailable")
                elif node.tag in ("integer", "real"):
                    number = int(value) if node.tag == "integer" else float(value)
                    if not math.isfinite(number) or abs(number) > 2**32:
                        raise SourceArchiveError()
                elif node.tag in ("true", "false") and value.strip():
                    raise SourceArchiveError()
                pending.extend((child, depth + 1) for child in node)
        if cursor != len(raw):
            raise SourceArchiveError()
    except SourceArchiveError:
        raise
    except (struct.error, UnicodeError, ET.ParseError, ValueError, OverflowError) as error:
        raise SourceArchiveError() from error
