"""Fixed dispatcher for one isolated reader worker."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if __package__:
    from .source_reader_guest import parse_arguments
else:
    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from tedd_knowledge_graph.source_reader_guest import parse_arguments

_MAX_INPUT = 64 * 1024**2
_MAX_OUTPUT = 32 * 1024**2
_OFFICE_MEMORY = 2 * 1024**3
_SECONDS = 30
_WORK_ROOT = Path("/work")
_ERROR_CODES = frozenset(
    {
        "capability_unavailable",
        "capture_incomplete",
        "cleanup_incomplete",
        "limit_exceeded",
        "privacy_rejected",
        "scope_denied",
        "source_corrupt",
        "source_unavailable",
    }
)


def host_argv(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    """Map a validated operation to a trusted sibling host command."""

    operation, page = parse_arguments(arguments)
    package = Path(__file__).resolve().parent
    prefix = (sys.executable, "-I", "-B")
    if operation == "image-inspect":
        return (*prefix, str(package / "source_office_host.py"), "image")
    if operation == "image-page":
        return (
            *prefix,
            str(package / "source_office_host.py"),
            "image",
            "--render-unit",
            str(page),
        )
    if operation == "pdf-text":
        return (*prefix, str(package / "source_pdf_host.py"))
    if operation == "pdf-page":
        return (
            *prefix,
            str(package / "source_pdf_host.py"),
            "--render-page",
            str(page),
            "--renderer",
            "/usr/bin/pdftoppm",
        )
    if operation == "docx-text":
        return (*prefix, str(package / "source_office_host.py"), "docx")
    return None


def _respond(result: dict[str, object]) -> int:
    try:
        payload = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        payload = b'{"status":"capability_unavailable"}'
    if len(payload) > _MAX_OUTPUT:
        payload = b'{"status":"limit_exceeded"}'
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    status = json.loads(payload).get("status")
    return 0 if status == "text_screened" else 1


def _docx_page(page: int) -> int:
    from tedd_knowledge_graph.source_contracts import SourceArchiveError
    from tedd_knowledge_graph.source_office_host import document_page
    from tedd_knowledge_graph.source_pdf_host import limit_worker

    try:
        _job = limit_worker(_OFFICE_MEMORY, _SECONDS)
        payload = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if not payload:
            raise SourceArchiveError("source_corrupt")
        if len(payload) > _MAX_INPUT:
            raise SourceArchiveError("limit_exceeded")
        with tempfile.TemporaryDirectory(prefix="tedd-docx-", dir=_WORK_ROOT) as raw:
            result = document_page(
                payload,
                "docx",
                page,
                reader=Path(sys.executable),
                converter=Path("/usr/bin/libreoffice"),
                renderer=Path("/usr/bin/pdftoppm"),
                work_root=Path(raw),
            )
        if result.get("status") != "text_screened":
            raise SourceArchiveError("capability_unavailable")
    except SourceArchiveError as error:
        code = error.code if error.code in _ERROR_CODES else "capability_unavailable"
        return _respond({"status": code})
    except MemoryError:
        return _respond({"status": "limit_exceeded"})
    except Exception:
        return _respond({"status": "capability_unavailable"})
    return _respond(result)


def run(arguments: tuple[str, ...]) -> int:
    """Dispatch one validated operation without accepting an executable from the caller."""

    try:
        operation, page = parse_arguments(arguments)
        command = host_argv(arguments)
    except ValueError:
        return 127
    if operation == "docx-page":
        assert page is not None
        return _docx_page(page)
    assert command is not None
    try:
        os.execv(command[0], command)
    except OSError:
        return 127


if __name__ == "__main__":
    raise SystemExit(run(tuple(sys.argv[1:])))
