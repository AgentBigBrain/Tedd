"""Disposable document/image worker using existing process limits, not a sandbox."""

from __future__ import annotations

import base64
import importlib
import io
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .source_documents import DocumentReading

# Cold Office startup can wait on the host without consuming its CPU allowance.
# The worker still independently enforces 30 CPU seconds and 2 GiB job memory.
DOCUMENT_WALL_SECONDS = 60
# In pinned LibreOffice 7.4.7, initial page 0 plus default magnification 0
# suppresses its generated /OpenAction so the unchanged PDF screen can decide.
_PDF_EXPORT_PROPERTIES = (
    '{"InitialPage":{"type":"long","value":"0"},"Magnification":{"type":"long","value":"0"}}'
)


def document_page(
    payload: bytes,
    kind: str,
    page: int,
    *,
    reader: Path,
    converter: Path,
    renderer: Path,
    work_root: Path,
    cancelled: threading.Event | None = None,
) -> dict[str, object]:
    """Parent-owned private staging and process-tree lifetime for one selected page."""
    from tedd_knowledge_graph import source_files
    from tedd_knowledge_graph.source_contracts import (
        READING_BYTES,
        SourceArchiveError,
        decoded_png,
        digest,
        parse_json,
        positive,
    )
    from tedd_knowledge_graph.source_macos_host import require_worker_boundary

    positive(page, maximum=256)
    if kind not in ("docx", "xlsx", "pptx") or len(payload) > 64 * 1024**2:
        raise SourceArchiveError("source_unavailable")
    if any(not path.is_absolute() or not path.is_file() for path in (reader, converter, renderer)):
        raise SourceArchiveError("capability_unavailable")
    if cancelled is not None and cancelled.is_set():
        raise SourceArchiveError("scope_denied")
    require_worker_boundary()
    root = source_files.private_root(work_root)
    operation: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="office-page-", dir=root) as raw:
            operation = Path(raw)
            source_files.set_private_path_mode(operation, 0o700)
            source_files.check_entry(operation, directory=True)
            env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
            env.update(TEMP=raw, TMP=raw, TMPDIR=raw, USERPROFILE=str(root), HOME=str(root))
            with subprocess.Popen(
                [
                    str(reader),
                    "-I",
                    "-B",
                    str(Path(__file__).resolve()),
                    kind,
                    "--render-page",
                    str(page),
                    "--converter",
                    str(converter),
                    "--renderer",
                    str(renderer),
                    "--private-root",
                    raw,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=operation,
                start_new_session=os.name != "nt",
                umask=0o077 if os.name != "nt" else -1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                deadline = time.monotonic() + DOCUMENT_WALL_SECONDS
                with ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        # Keep one communicate call writing through partial uploads.
                        # Retrying with None after a timeout can strand pending stdin.
                        transfer = executor.submit(
                            process.communicate, payload, timeout=DOCUMENT_WALL_SECONDS
                        )
                        while True:
                            if cancelled is not None and cancelled.is_set():
                                raise SourceArchiveError("scope_denied")
                            if time.monotonic() >= deadline:
                                raise SourceArchiveError("capability_unavailable")
                            try:
                                output, _ = transfer.result(timeout=0.1)
                                break
                            except FutureTimeoutError:
                                pass
                    finally:
                        # Kill pipe-holding descendants before joining the I/O worker.
                        # Windows worker Job closes with it; POSIX descendants share
                        # this explicitly new session, never the user's process group.
                        if sys.platform != "win32":
                            try:
                                os.killpg(process.pid, 9)
                            except ProcessLookupError:
                                pass
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
            if cancelled is not None and cancelled.is_set():
                raise SourceArchiveError("scope_denied")
            value = parse_json(output, maximum_bytes=READING_BYTES)
            if not isinstance(value, dict):
                raise SourceArchiveError()
            result = cast(dict[str, object], value)
            if process.returncode or result.get("status") != "text_screened":
                code = result.get("status")
                raise SourceArchiveError(
                    str(code)
                    if code
                    in {
                        "source_corrupt",
                        "privacy_rejected",
                        "limit_exceeded",
                        "source_unavailable",
                        "capture_incomplete",
                        "capability_unavailable",
                    }
                    else "capability_unavailable"
                )
            if (
                result.get("original_sha256") != digest(payload)
                or result.get("kind") != kind
                or type(result.get("page")) is not int
                or result["page"] != page
                or result.get("representation") != "libreoffice_rendering"
                or result.get("resources") != "bounded"
            ):
                raise SourceArchiveError()
            if page > positive(result.get("page_count"), maximum=256):
                raise SourceArchiveError()
            decoded_png(result)
            return result
    except (OSError, subprocess.TimeoutExpired):
        # A private leftover is not success. Never print source paths or stderr.
        raise SourceArchiveError(
            "cleanup_incomplete"
            if operation is not None and operation.exists()
            else "capability_unavailable"
        ) from None


def _image_envelope(payload: bytes, kind: str) -> None:
    """Refuse uninspected metadata/trailing data before the optional pixel decoder.

    This is a narrow container allowlist, not an image decoder or a claim that
    visible content is safe. The caller must still review the exact source.
    """
    from tedd_knowledge_graph.source_contracts import READING_BYTES, SourceArchiveError

    if not 1 <= len(payload) <= READING_BYTES:
        raise SourceArchiveError("limit_exceeded")
    if kind != "png":
        # These finite signatures/envelopes select a decoder, not a proof that
        # every encoded bit has visible meaning. Originals stay private; the
        # approved boundary uses bounded decoding and inspected metadata.
        signatures = {
            "jpeg": len(payload) > 4
            and payload.startswith(b"\xff\xd8\xff")
            # Office thumbnails can contain one NUL alignment byte after EOI.
            # Keep it in the original; arbitrary suffixes still fail closed.
            and payload.endswith((b"\xff\xd9", b"\xff\xd9\0")),
            "gif": payload.startswith((b"GIF87a", b"GIF89a")) and payload.endswith(b";"),
            "webp": len(payload) >= 20
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP"
            and int.from_bytes(payload[4:8], "little") == len(payload) - 8,
            "avif": len(payload) >= 24
            and payload[4:8] == b"ftyp"
            and payload[8:12] in (b"avif", b"avis"),
            "bmp": len(payload) >= 54
            and payload[:2] == b"BM"
            and int.from_bytes(payload[2:6], "little") == len(payload),
        }
        if kind not in signatures:
            raise SourceArchiveError("capability_unavailable")
        if not signatures[kind]:
            raise SourceArchiveError()
        return
    from tedd_knowledge_graph.source_image_host import inspect_png

    inspect_png(payload, maximum_dimension=8192, maximum_pixels=16_000_000)


def _render_scalar(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1e9
        and math.isfinite(value)
    )


def _image_metadata(info: dict[str, object], kind: str) -> None:
    """Allow rendering scalars and validated EXIF/ICC, not opaque private metadata.

    This inspects metadata exposed by the allowlisted decoder. It does not claim
    to detect steganography or all uninterpreted encoded data in the original.
    """
    from tedd_knowledge_graph.source_contracts import SourceArchiveError
    from tedd_knowledge_graph.source_office_metadata import inspect_icc

    if kind == "png":
        # All allowed PNG chunks were already validated by the stricter envelope.
        return
    scalar_keys = {
        "jfif",
        "jfif_unit",
        "adobe",
        "adobe_transform",
        "progressive",
        "progression",
        "transparency",
        "loop",
        "duration",
        "timestamp",
        "compression",
    }
    for key, value in info.items():
        if key == "exif":
            _exif_metadata(value)
        elif key == "icc_profile":
            inspect_icc(value)
        elif key in scalar_keys:
            if not _render_scalar(value):
                raise SourceArchiveError()
        elif key in ("jfif_version", "jfif_density", "dpi"):
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or any(not _render_scalar(item) for item in value)
            ):
                raise SourceArchiveError()
        elif key == "version" and value in (b"GIF87a", b"GIF89a"):
            continue
        elif key == "extension" and isinstance(value, tuple) and len(value) == 2:
            if value[0] != b"NETSCAPE2.0" or type(value[1]) is not int or value[1] < 0:
                raise SourceArchiveError("privacy_rejected")
        elif key == "background":
            channels = value if isinstance(value, tuple) else (value,)
            if len(channels) not in (1, 4) or any(
                type(channel) is not int or not 0 <= channel <= 255 for channel in channels
            ):
                raise SourceArchiveError()
        else:
            raise SourceArchiveError("privacy_rejected")


def _exif_metadata(value: object) -> None:
    """Use the existing Pillow reader for numeric resolution/dimension EXIF only."""
    from tedd_knowledge_graph.source_contracts import SourceArchiveError

    if not isinstance(value, bytes) or not 14 <= len(value) <= 65536:
        raise SourceArchiveError("limit_exceeded")
    if not value.startswith(b"Exif\0\0"):
        raise SourceArchiveError()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            exif = importlib.import_module("PIL.Image").Exif()
            exif.load(value)
            entries = dict(exif.items())
            if 0x8769 in entries:
                entries.pop(0x8769)
                child = exif.get_ifd(0x8769)
                if entries.keys() & child.keys():
                    raise SourceArchiveError()
                entries.update(child)
            allowed = {0x0112, 0x011A, 0x011B, 0x0128, 0xA001, 0xA002, 0xA003}
            if not entries or entries.keys() - allowed:
                raise SourceArchiveError("privacy_rejected")
            for tag, item in entries.items():
                if tag in (0x011A, 0x011B) and not isinstance(item, (int, float)):
                    numerator = getattr(item, "numerator", None)
                    denominator = getattr(item, "denominator", None)
                    if (
                        type(numerator) is not int
                        or type(denominator) is not int
                        or denominator <= 0
                    ):
                        raise SourceArchiveError()
                    item = numerator / denominator
                if not _render_scalar(item) or item <= 0:
                    raise SourceArchiveError()
                if (
                    tag == 0x0112
                    and item not in range(1, 9)
                    or tag == 0x0128
                    and item not in (1, 2, 3)
                ):
                    raise SourceArchiveError()
    except SourceArchiveError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        SyntaxError,
        OverflowError,
        struct.error,
        Warning,
    ) as error:
        raise SourceArchiveError() from error


def inspect_image(payload: bytes, kind: str) -> int:
    """Keep Office's envelope, metadata policy, and narrower raster limits."""
    from tedd_knowledge_graph.source_image_host import inspect_raster

    _image_envelope(payload, kind)
    return len(inspect_raster(payload, kind, metadata=_image_metadata))


def render_image(payload: bytes, reading: DocumentReading, position: int) -> dict[str, object]:
    """Render one previously decoded frame; do not extract files or forward metadata."""
    from tedd_knowledge_graph.source_contracts import SourceArchiveError, digest, positive
    from tedd_knowledge_graph.source_pdf_host import MAX_OUTPUT

    positive(position, maximum=len(reading.units))
    selected = reading.units[position - 1]
    location = selected.location
    if selected.kind != "image_page" or location is None or location.container is None:
        raise SourceArchiveError("capability_unavailable")
    # read_office already validated every part and image in these immutable bytes.
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        raw = package.read(location.container)
    kind = location.container.rsplit(".", 1)[-1].upper()
    if kind == "JPG":
        kind = "JPEG"
    from tedd_knowledge_graph.source_image_host import render_raster

    png = render_raster(raw, kind, location.start - 1)
    if len(png) > MAX_OUTPUT * 3 // 4 - 4096:
        raise SourceArchiveError("limit_exceeded")
    return {
        "status": "text_screened",
        "resources": "bounded",
        "kind": reading.kind,
        "original_sha256": reading.original_sha256,
        "position": position,
        "unit": asdict(selected),
        "reader_id": reading.reader_id,
        "png_sha256": digest(png),
        "png_base64": base64.b64encode(png).decode("ascii"),
    }


def render_document(
    payload: bytes, kind: str, page: int, converter: Path, renderer: Path, root: Path
) -> dict[str, object]:
    """Render exact screened Office bytes in a caller-owned disposable private root.

    This is a LibreOffice interpretation, not saved Microsoft Office pagination.
    The parent owns process-tree termination and cleanup even on worker timeout.
    """
    from tedd_knowledge_graph import source_files, source_pdf_host
    from tedd_knowledge_graph.source_contracts import SourceArchiveError, digest, positive
    from tedd_knowledge_graph.source_macos_host import require_worker_boundary
    from tedd_knowledge_graph.source_office import read_office

    require_worker_boundary()
    positive(page, maximum=256)
    reading = read_office(payload, kind, inspect_image=inspect_image, for_rendering=True)
    if any(not path.is_absolute() or not path.is_file() for path in (converter, renderer)):
        raise SourceArchiveError("capability_unavailable")
    source_files.private_root(root)
    if any(root.iterdir()):
        raise SourceArchiveError("privacy_rejected")
    for relative in ("profile", "profile/user", "output", "tmp"):
        source_files.directory(root / relative)
    # Closed, fresh configuration. Never inherit the owner's Office profile.
    settings = (
        ("Common/Security/Scripting", "DisableMacrosExecution", "true"),
        ("Common/Security/Scripting", "DisableActiveContent", "true"),
        ("Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
        ("Common/Security/Scripting", "MacroSecurityLevel", "3"),
        ("Writer/Content/Update", "Link", "0"),
        ("Calc/Content/Update", "Link", "1"),
        ("Common/Misc", "UseOpenCL", "false"),
    )
    configuration = (
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        + "".join(
            f'<item oor:path="/org.openoffice.Office.{path}"><prop oor:name="{name}" '
            f'oor:op="fuse"><value>{value}</value></prop></item>'
            for path, name, value in settings
        )
        + "</oor:items>"
    )
    source_files.write_bytes(
        root / "profile/user/registrymodifications.xcu", configuration.encode()
    )
    source = root / f"source.{kind}"
    source_files.write_bytes(source, payload)
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    environment.update(
        HOME=str(root),
        USERPROFILE=str(root),
        LOCALAPPDATA=str(root),
        XDG_CONFIG_HOME=str(root),
        XDG_CACHE_HOME=str(root),
        TEMP=str(root / "tmp"),
        TMP=str(root / "tmp"),
        TMPDIR=str(root / "tmp"),
        SAL_USE_VCLPLUGIN="svp",
        SAL_DISABLE_OPENCL="1",
    )
    filter_name = {"docx": "writer", "xlsx": "calc", "pptx": "impress"}[kind]
    result = subprocess.run(
        [
            str(converter),
            "-env:UserInstallation=" + (root / "profile").as_uri(),
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            f"pdf:{filter_name}_pdf_Export:{_PDF_EXPORT_PROPERTIES}",
            "--outdir",
            str(root / "output"),
            str(source),
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # The parent enforces the same deadline across conversion, PDF reading
        # and rendering. A shorter nested cutoff rejected valid cold starts.
        timeout=DOCUMENT_WALL_SECONDS,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise SourceArchiveError("capability_unavailable")
    pdf_path = root / "output/source.pdf"
    if os.name == "nt":
        source_files.claim_private_generated_file(pdf_path)
    pdf = source_files.read_bytes(pdf_path, source_pdf_host.MAX_INPUT)
    screened = source_pdf_host.extract(pdf)
    if screened.get("status") != "text_screened":
        raise SourceArchiveError(str(screened.get("status", "capability_unavailable")))
    count = positive(screened.get("page_count"), maximum=256)
    if page > count:
        raise SourceArchiveError("source_unavailable")
    rendered = source_pdf_host.render(pdf, page, renderer)
    if rendered.get("status") != "rendered" or rendered.get("original_sha256") != digest(pdf):
        raise SourceArchiveError("capture_incomplete")
    return {
        **rendered,
        "status": "text_screened",
        "original_sha256": reading.original_sha256,
        "derived_pdf_sha256": digest(pdf),
        "page_count": count,
        "kind": kind,
        "reader_id": reading.reader_id,
        "representation": "libreoffice_rendering",
        "coverage": "partial",
        "gaps": list(reading.gaps),
    }


def main() -> int:
    # -I omits user imports; load only the trusted checkout's sibling modules.
    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from tedd_knowledge_graph.source_contracts import (
        SourceArchiveError,
        encode,
        encode_units,
        parse_json,
    )
    from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable
    from tedd_knowledge_graph.source_native_host import remaining_native_seconds
    from tedd_knowledge_graph.source_office import read_office
    from tedd_knowledge_graph.source_pdf_host import (
        MAX_INPUT,
        MAX_MEMORY,
        MAX_OUTPUT,
        MAX_SECONDS,
        limit_worker,
    )

    try:
        layout = len(sys.argv) == 10 and sys.argv[2::2] == [
            "--render-page",
            "--converter",
            "--renderer",
            "--private-root",
        ]
        standalone_image = len(sys.argv) >= 2 and sys.argv[1] == "image"
        deadline = time.monotonic() + MAX_SECONDS if standalone_image else None
        _job = limit_worker(2 * 1024**3 if layout or standalone_image else MAX_MEMORY, MAX_SECONDS)
        sheet_excerpt = len(sys.argv) == 3 and sys.argv[2] == "--sheet-excerpt"
        if (
            not layout
            and not standalone_image
            and not sheet_excerpt
            and len(sys.argv) != 2
            and not (len(sys.argv) == 4 and sys.argv[2] == "--render-unit")
        ):
            raise SourceArchiveError()
        payload = sys.stdin.buffer.read(MAX_INPUT + 1)
        if standalone_image:
            from tedd_knowledge_graph.source_av1_host import Av1Validator
            from tedd_knowledge_graph.source_image_host import image_response
            from tedd_knowledge_graph.source_svg_host import SvgRenderer

            rectangle = None
            view = "presentation"
            if len(sys.argv) % 2 or len(sys.argv) > 14:
                raise SourceArchiveError()
            options: dict[str, str] = {}
            for flag, value in zip(sys.argv[2::2], sys.argv[3::2], strict=True):
                if flag in options or flag not in (
                    "--rectangle",
                    "--view",
                    "--render-unit",
                    "--svg-renderer",
                    "--svg-font",
                    "--av1-validator",
                ):
                    raise SourceArchiveError()
                options[flag] = value
                if flag == "--view":
                    view = value
                elif flag == "--rectangle":
                    coordinates = tuple(int(coordinate) for coordinate in value.split(","))
                    if len(coordinates) != 4:
                        raise SourceArchiveError()
                    rectangle = (coordinates[0], coordinates[1], coordinates[2], coordinates[3])
            if ("--svg-renderer" in options) != ("--svg-font" in options):
                raise SourceArchiveError("capability_unavailable")
            svg = (
                SvgRenderer(Path(options["--svg-renderer"]), Path(options["--svg-font"]), deadline)
                if "--svg-renderer" in options
                else None
            )
            result: dict[str, object] = image_response(
                payload,
                int(options["--render-unit"]) if "--render-unit" in options else None,
                rectangle=rectangle,
                view=view,
                svg=svg,
                av1=Av1Validator(Path(options["--av1-validator"]), deadline)
                if "--av1-validator" in options
                else None,
            )
        elif layout:
            result = render_document(
                payload,
                sys.argv[1],
                int(sys.argv[3]),
                Path(sys.argv[5]),
                Path(sys.argv[7]),
                Path(sys.argv[9]),
            )
        else:
            reading = read_office(
                payload, sys.argv[1], inspect_image=inspect_image, sheet_excerpt=sheet_excerpt
            )
            result = {
                "status": "text_screened",
                "resources": "bounded",
                "kind": reading.kind,
                "original_sha256": reading.original_sha256,
                "reader_id": reading.reader_id,
                "units": parse_json(encode_units(reading.units), maximum_bytes=MAX_OUTPUT),
                "gaps": list(reading.gaps),
            }
            if len(sys.argv) == 4:
                result = render_image(payload, reading, int(sys.argv[3]))
        if len(encode(result)) > MAX_OUTPUT:
            raise SourceArchiveError("limit_exceeded")
        if standalone_image:
            remaining_native_seconds(deadline)
    except MacOSWorkerUnavailable as error:
        result = error.response()
    except SourceArchiveError as error:
        result = {"status": error.code}
    except MemoryError:
        result = {"status": "limit_exceeded"}
    except (OSError, ValueError, NotImplementedError, RuntimeError, subprocess.TimeoutExpired):
        result = {"status": "capability_unavailable"}
    sys.stdout.buffer.write(encode(result))
    sys.stdout.buffer.flush()
    return 0 if result["status"] == "text_screened" else 1


if __name__ == "__main__":
    raise SystemExit(main())
