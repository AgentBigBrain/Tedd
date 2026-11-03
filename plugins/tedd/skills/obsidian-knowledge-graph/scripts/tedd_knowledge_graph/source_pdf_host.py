"""Optional PDF host worker. Resource limits are not a security sandbox.

Run only as a disposable child with an already approved Python installation.
The parent also imposes a wall-clock timeout. Third-party PDF packages are optional
host capabilities, never imported by the standard-library graph runtime.
"""

from __future__ import annotations

import base64
import importlib
import io
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, cast

MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
MAX_MEMORY = 512 * 1024 * 1024
MAX_SECONDS = 30
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"


class PosixResource(Protocol):
    RLIMIT_AS: int
    RLIMIT_CPU: int
    RLIMIT_CORE: int

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...


def limit_worker(memory_bytes: int, seconds: int) -> int:
    """Limit this disposable process before importing a parser or reading input.

    On Windows, keep the returned job handle until process exit. Closing the last
    handle deliberately terminates the job, including the worker itself.
    """
    if not 32 * 1024**2 <= memory_bytes <= 2 * 1024**3 or not 1 <= seconds <= 30:
        raise ValueError("invalid worker limits")
    from tedd_knowledge_graph.source_macos_host import require_worker_boundary

    require_worker_boundary()
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit),
                ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = int(kernel.CreateJobObjectW(None, None) or 0)
        if not handle:
            raise OSError("worker limit unavailable")
        limits = ExtendedLimit()
        # Job-wide committed memory, job CPU time, and kill-on-last-handle-close.
        limits.basic.flags = 0x200 | 0x4 | 0x2000
        limits.basic.job_time = seconds * 10_000_000
        limits.job_memory = memory_bytes
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel.CloseHandle(handle)
            raise OSError("worker limit unavailable")
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            kernel.CloseHandle(handle)
            raise OSError("worker limit unavailable")
        return handle
    resource = cast(PosixResource, importlib.import_module("resource"))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if IS_LINUX:
        # Piped Linux core collectors ignore RLIMIT_CORE. Disable dumpability
        # in this worker before reading source bytes, without system changes.
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        try:
            prctl = library.prctl
        except AttributeError as error:
            raise OSError("worker crash privacy unavailable") from error
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE, disabled.
            raise OSError("worker crash privacy unavailable")
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    return 0


class PdfPage(Protocol):
    def extract_text(self) -> str | None: ...
    def extract_tables(self) -> list[list[list[str | None]]]: ...


class PdfDocument(Protocol):
    pages: Sequence[PdfPage]


class PdfOpen(Protocol):
    def __call__(self, stream: io.BytesIO) -> AbstractContextManager[PdfDocument]: ...


class PdfReader(Protocol):
    is_encrypted: bool
    trailer: Mapping[str, object]
    metadata: object
    pages: Sequence[Mapping[str, object]]


class ReaderFactory(Protocol):
    def __call__(self, stream: io.BytesIO, *, strict: bool) -> PdfReader: ...


def _resolved_mapping(value: object) -> Mapping[str, object]:
    resolver = getattr(value, "get_object", None)
    if callable(resolver):
        value = cast(Callable[[], object], resolver)()
    if not isinstance(value, Mapping):
        raise ValueError("PDF object unavailable")
    return cast(Mapping[str, object], value)


def extract(payload: bytes) -> dict[str, object]:
    """Produce bounded derivative pages, never a truth or visual-screening verdict."""
    from tedd_knowledge_graph.legacy_values import secret_hits
    from tedd_knowledge_graph.source_contracts import (
        SourceArchiveError,
        SourceUnit,
        digest,
        encode,
        encode_units,
        parse_json,
    )

    if not payload.startswith(b"%PDF-"):
        return {"status": "source_corrupt"}
    if len(payload) > MAX_INPUT:
        return {"status": "limit_exceeded"}
    try:
        plumbing = importlib.import_module("pdfplumber")
        parser = importlib.import_module("pypdf")
    except ModuleNotFoundError:
        return {"status": "capability_unavailable"}
    try:
        reader = cast(ReaderFactory, parser.PdfReader)(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            return {"status": "privacy_rejected"}
        root = _resolved_mapping(reader.trailer["/Root"])
        # V1 refuses credential-prone/active or hidden source classes rather than
        # constructing a bespoke PDF sanitizer. Visual review is still required.
        if any(key in root for key in ("/AcroForm", "/OpenAction", "/AA", "/Names")):
            return {"status": "privacy_rejected"}
        if secret_hits(str(reader.metadata)):
            return {"status": "privacy_rejected"}
        if not 1 <= len(reader.pages) <= 256:
            return {"status": "limit_exceeded"}
        if any(page.get("/Annots") or page.get("/AA") for page in reader.pages):
            return {"status": "privacy_rejected"}
        opener = cast(PdfOpen, plumbing.open)
        units: list[SourceUnit] = []
        with opener(io.BytesIO(payload)) as document:
            if len(document.pages) != len(reader.pages):
                return {"status": "capture_incomplete"}
            for position, page in enumerate(document.pages, start=1):
                content = page.extract_text() or ""
                tables = page.extract_tables()
                if tables:
                    content += "\n\n[Table extraction — check page layout]\n" + encode(
                        tables
                    ).decode("utf-8")
                if secret_hits(content):
                    return {"status": "privacy_rejected"}
                units.append(
                    SourceUnit(
                        f"u{position}", content, position, kind="page" if content else "image_page"
                    )
                )
        reading = encode_units(tuple(units))
        result: dict[str, object] = {
            "status": "text_screened",
            "original_sha256": digest(payload),
            "reader_id": f"pdfplumber:{plumbing.__version__};pypdf:{parser.__version__}",
            "page_count": len(units),
            "units": parse_json(reading, maximum_bytes=MAX_OUTPUT),
            "resources": "bounded",
            "visual_review_required": True,
        }
        if len(encode(result)) > MAX_OUTPUT:
            return {"status": "limit_exceeded"}
        return result
    except MemoryError:
        return {"status": "limit_exceeded"}
    except SourceArchiveError as error:
        return {"status": error.code}
    except parser.errors.PdfReadError:
        # Known strict-parser rejection is a document problem, not a missing host.
        # Its message can contain source bytes; return only the closed error code.
        return {"status": "source_corrupt"}
    except (ValueError, TypeError, KeyError, IndexError, OSError, RuntimeError, RecursionError):
        # Parser errors may contain file content. Only closed codes leave this process.
        return {"status": "source_corrupt"}
    except Exception:
        # Last-resort process boundary for third-party parser exception classes.
        # Do not blame the document when the failure cannot be classified safely.
        return {"status": "capability_unavailable"}


def render(payload: bytes, page: int, renderer: Path) -> dict[str, object]:
    """Render one bounded page with the host's explicitly selected Poppler binary."""
    from tedd_knowledge_graph.source_contracts import digest, encode
    from tedd_knowledge_graph.source_macos_host import (
        MacOSWorkerUnavailable,
        require_worker_boundary,
    )

    if type(page) is not int or not 1 <= page <= 256 or len(payload) > MAX_INPUT:
        return {"status": "limit_exceeded"}
    if not renderer.is_absolute() or not renderer.is_file():
        return {"status": "capability_unavailable"}
    if not payload.startswith(b"%PDF-"):
        return {"status": "source_corrupt"}
    try:
        require_worker_boundary()
        result = subprocess.run(
            [
                str(renderer),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-scale-to",
                "1800",
                "-png",
                "-",
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            return {"status": "source_corrupt"}
        response: dict[str, object] = {
            "status": "rendered",
            "page": page,
            "original_sha256": digest(payload),
            "png_sha256": digest(result.stdout),
            "png_base64": base64.b64encode(result.stdout).decode("ascii"),
            "resources": "bounded",
        }
        if len(encode(response)) > MAX_OUTPUT:
            return {"status": "limit_exceeded"}
        return response
    except MacOSWorkerUnavailable as error:
        return error.response()
    except MemoryError:
        return {"status": "limit_exceeded"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "capability_unavailable"}


def main() -> int:
    import json

    # -I excludes user imports. Load only this script's own trusted sibling modules.
    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable

    result: dict[str, object]
    try:
        _job = limit_worker(MAX_MEMORY, MAX_SECONDS)
    except MacOSWorkerUnavailable as error:
        result = error.response()
    except (OSError, ValueError):
        result = {"status": "capability_unavailable"}
    else:
        try:
            payload = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(sys.argv) == 1:
                result = extract(payload)
            elif (
                len(sys.argv) == 5
                and sys.argv[1] == "--render-page"
                and sys.argv[3] == "--renderer"
            ):
                try:
                    page = int(sys.argv[2])
                except ValueError:
                    result = {"status": "source_corrupt"}
                else:
                    result = render(payload, page, Path(sys.argv[4]))
            else:
                result = {"status": "source_corrupt"}
        except MemoryError:
            result = {"status": "limit_exceeded"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0 if result["status"] in ("text_screened", "rendered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
