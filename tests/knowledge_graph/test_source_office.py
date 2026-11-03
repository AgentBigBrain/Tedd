"""Neutral OOXML fixtures exercise saved bytes, not installed Office applications."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import io
import json
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceLocation,
    decode_units,
    digest,
    encode,
)

from tests.support.paths import GRAPH_SCRIPTS

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
BIB = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"


def package(members: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def word_document() -> bytes:
    return package(
        {
            "word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Not approved.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Condition</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Review first</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>Only then.</w:t></w:r></w:p></w:body></w:document>'.encode()
        }
    )


def two_page_word_document(
    *,
    first: str = "Not approved unless reviewed.",
    second: str = "The second page does not remove the review requirement.",
) -> bytes:
    """Generated neutral original shared by layout lifecycle and private-view checks."""
    document = __import__("docx", fromlist=["Document"]).Document()
    document.add_paragraph(first)
    document.add_page_break()
    document.add_paragraph(second)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def noted_word_document(note_kind: str, *, include_note: bool = True) -> bytes:
    """A table qualification with a nonadjacent, exact-ID footnote or endnote."""
    members = {
        "word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Release condition</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>The reviewed change may ship.</w:t><w:{note_kind}Reference w:id="7"/></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'.encode(),
    }
    if include_note:
        members[f"word/{note_kind}s.xml"] = (
            f'<w:{note_kind}s xmlns:w="{W}"><w:{note_kind} w:id="3"><w:p><w:r><w:t>A different condition, not the cited note.</w:t></w:r></w:p></w:{note_kind}><w:{note_kind} w:id="7"><w:p><w:r><w:t>Do not ship if backup verification failed.</w:t></w:r></w:p></w:{note_kind}></w:{note_kind}s>'.encode()
        )
        members["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{REL}"><Relationship Id="n1" Type="{R}/{note_kind}s" Target="{note_kind}s.xml"/></Relationships>'.encode()
        )
    return package(members)


def literal_workbook(*, formula: bool = False, extra_sheet: bool = False) -> bytes:
    """A copied sheet; optional dependency/empty-sheet cases must refuse excerpt intake."""
    extra = '<sheet name="Other" sheetId="9" r:id="r2"/>' if extra_sheet else ""
    relation = (
        f'<Relationship Id="r2" Type="{R}/worksheet" Target="worksheets/other.xml"/>'
        if extra_sheet
        else ""
    )
    cell = (
        '<c r="A2"><f>Other!A1</f><v>1</v></c>'
        if formula
        else '<c r="A2" t="inlineStr"><is><t>Do not stop if jobs remain.</t></is></c>'
    )
    members = {
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Selected" sheetId="3" r:id="r1"/>{extra}</sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="worksheets/selected.xml"/>{relation}</Relationships>'.encode(),
        "xl/worksheets/selected.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Condition</t></is></c></row><row r="2">{cell}</row></sheetData></worksheet>'.encode(),
    }
    if extra_sheet:
        members["xl/worksheets/other.xml"] = (
            f'<worksheet xmlns="{S}"><sheetData/></worksheet>'.encode()
        )
    return package(members)


def conditional_workbook() -> bytes:
    """A summary whose saved value depends on an authorized, hidden second sheet."""
    return package(
        {
            "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Summary" sheetId="1" r:id="r1"/><sheet name="Conditions" sheetId="7" r:id="r2" state="hidden"/></sheets></workbook>'.encode(),
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Type="{R}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>'.encode(),
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>See conditions</t></is></c></row><row r="2"><c r="A2"><f>Conditions!A2</f><v>3.5000</v></c></row></sheetData></worksheet>'.encode(),
            "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Amount USD</t></is></c></row><row r="2" hidden="1"><c r="A2"><f>7/2</f><v>3.5000</v></c></row></sheetData><autoFilter ref="A1:A2"/></worksheet>'.encode(),
        }
    )


def bibliography_document() -> bytes:
    """The empty, standard bibliography store shipped in ordinary Word templates."""
    with zipfile.ZipFile(io.BytesIO(word_document())) as original:
        members = {name: original.read(name) for name in original.namelist()}
    return package(
        {
            **members,
            "word/_rels/document.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="b1" Type="{R}/customXml" Target="../customXml/item1.xml"/></Relationships>'.encode(),
            "customXml/item1.xml": f'<b:Sources xmlns:b="{BIB}" SelectedStyle="/APA.XSL" StyleName="APA"/>'.encode(),
            "customXml/itemProps1.xml": f'<ds:datastoreItem xmlns:ds="{CUSTOM}" ds:itemID="{{12345678-1234-1234-1234-123456789abc}}"><ds:schemaRefs><ds:schemaRef ds:uri="{BIB}"/></ds:schemaRefs></ds:datastoreItem>'.encode(),
            "customXml/_rels/item1.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="p1" Type="{R}/customXmlProps" Target="itemProps1.xml"/></Relationships>'.encode(),
        }
    )


def png_chunk(kind: bytes, value: bytes) -> bytes:
    return (
        struct.pack("!I", len(value)) + kind + value + struct.pack("!I", zlib.crc32(kind + value))
    )


def png_image(*, width: int = 1, height: int = 1, pixels: bytes | None = None) -> bytes:
    """Synthetic RGB pixels; no metadata, paths, or external image fixtures."""

    rgb = pixels if pixels is not None else b"\xff\xff\xff" * width * height
    assert len(rgb) == width * height * 3
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + png_chunk(
            b"IDAT",
            zlib.compress(
                b"".join(
                    b"\x00" + rgb[row : row + width * 3] for row in range(0, len(rgb), width * 3)
                )
            ),
        )
        + png_chunk(b"IEND", b"")
    )


def image_document(
    image: bytes, name: str = "word/media/image.png", document: bytes | None = None
) -> bytes:
    with zipfile.ZipFile(io.BytesIO(word_document() if document is None else document)) as original:
        members = {item.filename: original.read(item) for item in original.infolist()}
    return package({**members, name: image})


def office_worker(payload: bytes, kind: str, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    worker = GRAPH_SCRIPTS / "tedd_knowledge_graph" / "source_office_host.py"
    return subprocess.run(
        [sys.executable, "-I", "-B", str(worker), kind, *arguments],
        input=payload,
        capture_output=True,
        timeout=30,
        check=False,
    )


class OfficeLayoutTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        from tedd_knowledge_graph import source_office_host

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        set_private_path_mode(self.root, 0o700)
        self.work = self.root / "work"
        self.work.mkdir()
        set_private_path_mode(self.work, 0o700)
        self.worker = self.root / "worker.py"
        # Replace only the external Office worker, retaining real pipes/session cleanup.
        stack.enter_context(mock.patch.object(source_office_host, "__file__", str(self.worker)))
        stack.enter_context(
            mock.patch("tedd_knowledge_graph.source_macos_host.require_worker_boundary")
        )
        stack.enter_context(mock.patch.object(source_office_host, "DOCUMENT_WALL_SECONDS", 3))
        self.host = source_office_host
        self.threads = set(threading.enumerate())

    def worker_body(self, body: str) -> None:
        self.worker.write_text(
            "import hashlib,json,os,pathlib,sys,time\n"
            f"root = pathlib.Path({str(self.root)!r})\n"
            "(root / 'pid').write_text(str(os.getpid()))\n" + body,
            encoding="utf-8",
        )

    def read(self, payload: bytes, cancelled: threading.Event | None = None) -> dict[str, object]:
        return self.host.document_page(
            payload,
            "docx",
            1,
            reader=Path(sys.executable),
            converter=Path(sys.executable),
            renderer=Path(sys.executable),
            work_root=self.work,
            cancelled=cancelled,
        )

    def assert_cleaned(self) -> None:
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertLessEqual(set(threading.enumerate()), self.threads)
        if sys.platform != "win32":
            with self.assertRaises(ChildProcessError):
                os.waitpid(int((self.root / "pid").read_text()), os.WNOHANG)

    def test_upload_resumes_after_first_poll_and_delivers_exact_bytes_and_eof(self) -> None:
        png = png_image()
        response = {
            "status": "text_screened",
            "kind": "docx",
            "page": 1,
            "page_count": 1,
            "representation": "libreoffice_rendering",
            "resources": "bounded",
            "png_base64": base64.b64encode(png).decode(),
            "png_sha256": digest(png),
        }
        self.worker_body(
            "first = sys.stdin.buffer.read(1)\n"
            "(root / 'ready').touch()\n"
            "deadline = time.monotonic() + 2\n"
            "while not (root / 'release').exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.001)\n"
            "assert (root / 'release').exists()\n"
            # Require output drainage while the remaining upload is still blocked.
            "sys.stdout.buffer.write(b' ' * (256 * 1024))\n"
            "sys.stdout.buffer.flush()\n"
            "data = first + sys.stdin.buffer.read()\n"
            "(root / 'received').write_text(str(len(data)))\n"
            f"response = {response!r}\n"
            "response['original_sha256'] = hashlib.sha256(data).hexdigest()\n"
            "print(json.dumps(response), flush=True)\n"
        )
        for payload in (b"small control", b"p" * (2 * 1024**2)):
            with self.subTest(size=len(payload)):
                for marker in ("ready", "release", "received"):
                    (self.root / marker).unlink(missing_ok=True)
                checks = 0

                def release_after_poll() -> bool:
                    nonlocal checks
                    checks += 1
                    # One pre-start check and one initial poll precede release.
                    if checks >= 3 and (self.root / "ready").exists():
                        (self.root / "release").touch()
                    return False

                cancelled = threading.Event()
                with mock.patch.object(cancelled, "is_set", side_effect=release_after_poll):
                    result = self.read(payload, cancelled)
                self.assertEqual(result["original_sha256"], digest(payload))
                self.assertEqual(int((self.root / "received").read_text()), len(payload))
                self.assert_cleaned()

    def test_cancellation_during_partial_upload_reaps_worker(self) -> None:
        self.worker_body("sys.stdin.buffer.read(1)\n(root / 'ready').touch()\ntime.sleep(20)\n")
        cancelled = threading.Event()
        with (
            mock.patch.object(
                cancelled, "is_set", side_effect=lambda: (self.root / "ready").exists()
            ),
            self.assertRaisesRegex(SourceArchiveError, "scope_denied"),
        ):
            self.read(b"p" * (2 * 1024**2), cancelled)
        self.assert_cleaned()

    def test_early_stdin_close_preserves_refusal_and_reaps_worker(self) -> None:
        self.worker_body(
            "os.read(0, 1)\n"
            "os.close(0)\n"
            "print(json.dumps({'status': 'source_unavailable'}), flush=True)\n"
        )
        with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
            self.read(b"p" * (2 * 1024**2))
        self.assert_cleaned()

    def test_deadline_during_partial_upload_reaps_worker(self) -> None:
        self.worker_body("sys.stdin.buffer.read(1)\ntime.sleep(20)\n")
        started = time.monotonic()
        with (
            mock.patch.object(self.host, "DOCUMENT_WALL_SECONDS", 0.3),
            self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"),
        ):
            self.read(b"p" * (2 * 1024**2))
        self.assertLess(time.monotonic() - started, 3)
        self.assert_cleaned()

    @unittest.skipIf(os.name == "nt", "POSIX session descendants; Windows uses worker Jobs")
    def test_cancellation_kills_descendant_holding_pipes(self) -> None:
        self.worker_body(
            "descendant = os.fork()\n"
            "if descendant == 0:\n"
            "    time.sleep(20)\n"
            "    os._exit(0)\n"
            "(root / 'descendant').write_text(str(descendant))\n"
            "time.sleep(20)\n"
        )
        cancelled = threading.Event()
        with (
            mock.patch.object(
                cancelled, "is_set", side_effect=lambda: (self.root / "descendant").exists()
            ),
            self.assertRaisesRegex(SourceArchiveError, "scope_denied"),
        ):
            self.read(b"p" * (2 * 1024**2), cancelled)
        descendant = (self.root / "descendant").read_text()
        state = subprocess.run(
            ["/bin/ps", "-p", descendant, "-o", "stat="],
            capture_output=True,
            check=False,
            timeout=3,
        ).stdout.strip()
        # The OS may still be reaping the orphan; it must no longer be running.
        self.assertTrue(not state or state.startswith(b"Z"), state)
        self.assert_cleaned()


class OfficeTests(unittest.TestCase):
    def test_workbook_refuses_ambiguous_sheet_names_before_reading_or_rendering(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        original = conditional_workbook()
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        valid = read_office(original, "xlsx")
        self.assertIn("formula: =Conditions!A2; saved n: 3.5000", valid.units[1].text)
        self.assertEqual(len(valid.units), 4)

        # The IDs and package parts remain distinct. Formula references use names,
        # so duplicate names must not produce two plausible target worksheets.
        for name in (b"Conditions", b"cONDITIONS"):
            payload = package(
                {
                    **members,
                    "xl/workbook.xml": members["xl/workbook.xml"].replace(
                        b'name="Summary"', b'name="' + name + b'"'
                    ),
                }
            )
            for rendering in (False, True):
                with (
                    self.subTest(name=name, rendering=rendering),
                    self.assertRaisesRegex(SourceArchiveError, "source_corrupt"),
                ):
                    read_office(payload, "xlsx", for_rendering=rendering)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_OFFICE_CONVERTER")
        and os.environ.get("TEDD_TEST_PDF_RENDERER")
        and all(
            importlib.util.find_spec(name) for name in ("openpyxl", "pptx", "pdfplumber", "pypdf")
        ),
        "explicit isolated spreadsheet/presentation layout helper check",
    )
    def test_real_sheet_and_slide_layouts_keep_exact_original_and_clean_staging(self) -> None:
        from tedd_knowledge_graph.source_office_host import document_page

        workbook = __import__("openpyxl", fromlist=["Workbook"]).Workbook()
        sheet = workbook.active
        sheet.append(["Not approved unless reviewed.", 2])
        sheet.append(["Review remains required.", "=SUM(B1:B1)"])
        presentation = __import__("pptx", fromlist=["Presentation"]).Presentation()
        for wording in ("Not approved unless reviewed.", "The condition still applies."):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = wording
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            set_private_path_mode(root, 0o700)
            for kind, original, page, count in (
                ("xlsx", workbook, 1, 1),
                ("pptx", presentation, 2, 2),
            ):
                with self.subTest(kind=kind):
                    stream = io.BytesIO()
                    original.save(stream)
                    payload = stream.getvalue()
                    result = document_page(
                        payload,
                        kind,
                        page,
                        reader=Path(sys.executable),
                        converter=Path(os.environ["TEDD_TEST_OFFICE_CONVERTER"]),
                        renderer=Path(os.environ["TEDD_TEST_PDF_RENDERER"]),
                        work_root=root,
                    )
                    self.assertEqual(result["original_sha256"], digest(payload))
                    self.assertEqual(result["page_count"], count)
                    self.assertEqual(result["representation"], "libreoffice_rendering")
                    self.assertEqual(list(root.iterdir()), [])

    def test_layout_cleanup_failure_reports_private_leftover(self) -> None:
        from tedd_knowledge_graph.source_office_host import document_page

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            allocated = tempfile.TemporaryDirectory(prefix="office-page-", dir=root)
            try:
                if os.name != "nt":
                    os.chmod(allocated.name, 0o755)
                with (
                    # Exercise cleanup after a synthetic host admits the job;
                    # Popen is also mocked. This does not qualify a Mac worker.
                    mock.patch("tedd_knowledge_graph.source_macos_host.require_worker_boundary"),
                    mock.patch("tempfile.TemporaryDirectory", return_value=allocated),
                    mock.patch.object(allocated, "cleanup", side_effect=PermissionError),
                    mock.patch("subprocess.Popen", side_effect=OSError) as start,
                    self.assertRaisesRegex(SourceArchiveError, "cleanup_incomplete"),
                ):
                    document_page(
                        word_document(),
                        "docx",
                        1,
                        reader=Path(sys.executable),
                        converter=Path(sys.executable),
                        renderer=Path(sys.executable),
                        work_root=root,
                    )
                self.assertTrue(
                    any("--render-page" in call.args[0] for call in start.call_args_list)
                )
                self.assertTrue(Path(allocated.name).exists())
            finally:
                allocated.cleanup()

    def test_rendering_refuses_external_formula_functions_but_accepts_basic_calculation(
        self,
    ) -> None:
        from xml.etree import ElementTree as ET

        from tedd_knowledge_graph.source_office import _check_renderable

        for formula in (
            'WEBSERVICE("https://example.invalid/")',
            'DDE("cmd","topic","item")',
            '_xlfn.WEBSERVICE("x")',
            "[other.xlsx]Sheet1!A1",
        ):
            element = ET.Element(f"{{{S}}}f")
            element.text = formula
            with self.subTest(formula=formula), self.assertRaises(SourceArchiveError):
                _check_renderable({"xl/worksheets/sheet1.xml": element})
        for formula in ("SUM(A1:A2)", 'IF(A1>0,"Only if reviewed","Not approved")', "A1*2"):
            element = ET.Element(f"{{{S}}}f")
            element.text = formula
            _check_renderable({"xl/worksheets/sheet1.xml": element})

    def test_layout_refuses_executable_fields_before_conversion(self) -> None:
        from tedd_knowledge_graph.source_office_host import render_document

        raw = package(
            {
                "word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:instrText>DDEAUTO service topic</w:instrText><w:t>Cached value</w:t></w:r></w:p></w:body></w:document>'.encode()
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"):
                render_document(raw, "docx", 1, Path(sys.executable), Path(sys.executable), root)
            self.assertEqual(list(root.iterdir()), [])

    def test_generated_pdf_is_private_before_ownership_is_claimed(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            with (
                mock.patch.object(
                    source_files,
                    "mode_has_any",
                    side_effect=lambda subject, mask: isinstance(subject, int),
                ),
                mock.patch.object(source_files, "set_private_fd_mode") as claim,
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                source_files.claim_private_generated_file(pdf)
            claim.assert_not_called()

    def test_private_generated_pdf_can_be_claimed_and_rechecked(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            set_private_path_mode(pdf, 0o600)
            claimed = False
            original_owner = source_files.owner_is_current_user
            original_claim = source_files.set_private_fd_mode

            def owner(subject: Path | int, *, allow_root: bool = False) -> bool:
                if isinstance(subject, int):
                    return allow_root or claimed
                if subject == pdf:
                    return claimed
                return original_owner(subject, allow_root=allow_root)

            def claim(descriptor: int, mode: int) -> None:
                nonlocal claimed
                self.assertEqual(mode, 0o600)
                original_claim(descriptor, mode)
                claimed = True

            with (
                mock.patch.object(source_files, "owner_is_current_user", side_effect=owner),
                mock.patch.object(source_files, "set_private_fd_mode", side_effect=claim),
            ):
                source_files.claim_private_generated_file(pdf)
            self.assertTrue(claimed)

    def test_untrusted_generated_pdf_owner_is_not_claimed(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            set_private_path_mode(pdf, 0o600)
            with (
                mock.patch.object(
                    source_files,
                    "owner_is_current_user",
                    side_effect=lambda subject, allow_root=False: not isinstance(subject, int),
                ),
                mock.patch.object(source_files, "set_private_fd_mode") as claim,
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                source_files.claim_private_generated_file(pdf)
            claim.assert_not_called()

    def test_hardlinked_generated_pdf_is_not_claimed(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            os.link(pdf, root / "other.pdf")
            with (
                mock.patch.object(source_files, "set_private_fd_mode") as claim,
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                source_files.claim_private_generated_file(pdf)
            claim.assert_not_called()

    def test_generated_pdf_claim_failure_remains_a_privacy_refusal(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            set_private_path_mode(pdf, 0o600)
            with (
                mock.patch.object(source_files, "set_private_fd_mode", side_effect=PermissionError),
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                source_files.claim_private_generated_file(pdf)

    @unittest.skipUnless(os.name == "nt", "native Windows guarded output check")
    def test_generated_pdf_cannot_be_replaced_during_claim(self) -> None:
        from tedd_knowledge_graph import source_files

        with tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA")) as raw:
            root = Path(raw)
            set_private_path_mode(root, 0o700)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-synthetic")
            set_private_path_mode(pdf, 0o600)
            replacement = root / "replacement.pdf"
            replacement.write_bytes(b"other")
            original_claim = source_files.set_private_fd_mode

            def claim(descriptor: int, mode: int) -> None:
                with self.assertRaises(OSError):
                    pdf.write_bytes(b"changed")
                with self.assertRaises(OSError):
                    os.replace(replacement, pdf)
                original_claim(descriptor, mode)

            with mock.patch.object(source_files, "set_private_fd_mode", side_effect=claim):
                source_files.claim_private_generated_file(pdf)
            self.assertEqual(pdf.read_bytes(), b"%PDF-synthetic")
            self.assertEqual(replacement.read_bytes(), b"other")

    def test_cancelled_layout_does_not_start_a_worker_or_create_staging(self) -> None:
        from tedd_knowledge_graph.source_office_host import document_page

        cancelled = threading.Event()
        cancelled.set()
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                document_page(
                    word_document(),
                    "docx",
                    1,
                    reader=Path(sys.executable),
                    converter=Path(sys.executable),
                    renderer=Path(sys.executable),
                    work_root=Path(raw),
                    cancelled=cancelled,
                )
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_layout_requires_screened_office_before_creating_conversion_files(self) -> None:
        from tedd_knowledge_graph import source_office_host

        render = source_office_host.render_document
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(SourceArchiveError):
                render(b"not Office", "docx", 1, Path(sys.executable), Path(sys.executable), root)
            self.assertEqual(list(root.iterdir()), [])

    def test_layout_isolated_conversion_preserves_original_and_binds_derived_page(self) -> None:
        from tedd_knowledge_graph import source_files, source_office_host, source_pdf_host

        render = source_office_host.render_document
        payload = word_document()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)

            def convert(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                # A real neutral Google Word export completed in 45.64 s. The
                # approved 60 s wall budget must allow it to reach PDF checks.
                timeout = kwargs["timeout"]
                assert isinstance(timeout, (int, float))
                if timeout < 46:
                    raise subprocess.TimeoutExpired(argv, timeout)
                self.assertLessEqual(timeout, 60)
                self.assertEqual(Path(argv[-1]).read_bytes(), payload)
                self.assertIn("--headless", argv)
                conversion = argv[argv.index("--convert-to") + 1]
                components = conversion.split(":", 2)
                self.assertEqual(3, len(components))
                media, export_filter, raw_properties = components
                self.assertEqual(("pdf", "writer_pdf_Export"), (media, export_filter))
                self.assertEqual(
                    '{"InitialPage":{"type":"long","value":"0"},'
                    '"Magnification":{"type":"long","value":"0"}}',
                    raw_properties,
                )
                self.assertEqual(
                    {
                        "InitialPage": {"type": "long", "value": "0"},
                        "Magnification": {"type": "long", "value": "0"},
                    },
                    json.loads(raw_properties),
                )
                environment = kwargs["env"]
                assert isinstance(environment, dict)
                self.assertNotIn("SECRET_FIXTURE", environment)
                self.assertNotIn("HTTP_PROXY", environment)
                profile = (root / "profile" / "user" / "registrymodifications.xcu").read_text()
                self.assertIn('oor:name="DisableMacrosExecution"', profile)
                self.assertIn('oor:name="DisableActiveContent"', profile)
                source_files.write_bytes(root / "output" / "source.pdf", b"%PDF-derived-fixture")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with (
                # The converter and both PDF stages are synthetic below. Keep
                # staging-contract coverage separate from actual host admission.
                mock.patch("tedd_knowledge_graph.source_macos_host.require_worker_boundary"),
                mock.patch.dict(os.environ, {"SECRET_FIXTURE": "do not inherit"}),
                mock.patch.object(source_office_host.subprocess, "run", side_effect=convert),
                mock.patch.object(
                    source_pdf_host,
                    "extract",
                    return_value={
                        "status": "text_screened",
                        "page_count": 2,
                        "original_sha256": digest(b"%PDF-derived-fixture"),
                    },
                ),
                mock.patch.object(
                    source_pdf_host,
                    "render",
                    return_value={
                        "status": "rendered",
                        "page": 2,
                        "original_sha256": digest(b"%PDF-derived-fixture"),
                        "png_sha256": digest(png_image()),
                        "png_base64": base64.b64encode(png_image()).decode(),
                    },
                ),
            ):
                result = render(
                    payload, "docx", 2, Path(sys.executable), Path(sys.executable), root
                )
            self.assertEqual(result["original_sha256"], digest(payload))
            self.assertEqual(result["page"], 2)
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["representation"], "libreoffice_rendering")
            self.assertEqual((root / "source.docx").read_bytes(), payload)

    def test_layout_keeps_pdf_screen_refusal_before_rendering(self) -> None:
        from tedd_knowledge_graph import source_files, source_office_host, source_pdf_host

        payload = word_document()
        derived = b"%PDF-derived-with-open-action"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            set_private_path_mode(root, 0o700)

            def convert(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                source_files.write_bytes(root / "output" / "source.pdf", derived)
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with (
                mock.patch("tedd_knowledge_graph.source_macos_host.require_worker_boundary"),
                mock.patch.object(source_office_host.subprocess, "run", side_effect=convert),
                mock.patch.object(
                    source_pdf_host, "extract", return_value={"status": "privacy_rejected"}
                ) as screen,
                mock.patch.object(source_pdf_host, "render") as render_pdf,
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                source_office_host.render_document(
                    payload, "docx", 1, Path(sys.executable), Path(sys.executable), root
                )

            screen.assert_called_once_with(derived)
            render_pdf.assert_not_called()

    def test_print_ticket_envelope_refuses_secrets_unknown_data_and_malformed_parts(self) -> None:
        from tedd_knowledge_graph.source_office_metadata import inspect_printer_settings

        def ticket(value: dict[str, object]) -> bytes:
            encoded = plistlib.dumps(value)
            return b"\2\0\0\0" + b"".join(
                struct.pack("<II", kind, len(encoded)) + encoded for kind in (1, 2)
            )

        valid = ticket({"com.apple.print.PrintSettings.PMCopies": 1})
        inspect_printer_settings(valid)
        with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
            inspect_printer_settings(ticket({"PMPPDPaperCodeName": "password=SYNTHETIC-ONLY"}))
        for case, raw in (
            ("credential", ticket({"PMPPDPaperCodeName": "password=SYNTHETIC-ONLY"})),
            ("opaque binary", ticket({"PMPPDPaperCodeName": b"hidden"})),
            ("unknown key", ticket({"unapproved": 1})),
            ("unknown envelope", b"\3" + valid[1:]),
            ("suffix", valid + b"x"),
            ("truncated", valid[:-1]),
            ("invalid number", ticket({"com.apple.print.PrintSettings.PMCopies": float("inf")})),
            ("entity", valid.replace(b"<plist", b'<!ENTITY x "x"><plist')),
        ):
            with self.subTest(case=case), self.assertRaises(SourceArchiveError):
                inspect_printer_settings(raw)

    def test_empty_word_bibliography_preserves_original_and_readable_condition(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = bibliography_document()
        reading = read_office(raw, "docx")
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual(reading.units[0].text, "Not approved.")
        self.assertEqual(reading.reader_id, "tedd-ooxml:5")
        self.assertIn("office_layout_not_rendered", reading.gaps)

    def test_bibliography_allowance_cannot_admit_arbitrary_custom_data(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        with zipfile.ZipFile(io.BytesIO(bibliography_document())) as original:
            members = {name: original.read(name) for name in original.namelist()}
        for case, name, raw in (
            ("arbitrary data", "customXml/item1.xml", b"<private>unreviewed</private>"),
            (
                "bibliographic record",
                "customXml/item1.xml",
                f'<b:Sources xmlns:b="{BIB}"><b:Source/></b:Sources>'.encode(),
            ),
            (
                "text body",
                "customXml/item1.xml",
                f'<b:Sources xmlns:b="{BIB}">unreviewed</b:Sources>'.encode(),
            ),
            (
                "unknown property",
                "customXml/item1.xml",
                f'<b:Sources xmlns:b="{BIB}" other="unreviewed"/>'.encode(),
            ),
            (
                "credential",
                "customXml/item1.xml",
                f'<b:Sources xmlns:b="{BIB}" StyleName="password=synthetic-only"/>'.encode(),
            ),
            (
                "wrong schema",
                "customXml/itemProps1.xml",
                members["customXml/itemProps1.xml"].replace(BIB.encode(), b"urn:unreviewed"),
            ),
            (
                "missing target",
                "customXml/_rels/item1.xml.rels",
                members["customXml/_rels/item1.xml.rels"].replace(
                    b"itemProps1.xml", b"missing.xml"
                ),
            ),
            (
                "external properties",
                "customXml/_rels/item1.xml.rels",
                members["customXml/_rels/item1.xml.rels"].replace(
                    b" Target=", b' TargetMode="External" Target='
                ),
            ),
            (
                "other relationship",
                "customXml/_rels/item1.xml.rels",
                members["customXml/_rels/item1.xml.rels"].replace(
                    b"/customXmlProps", b"/hyperlink"
                ),
            ),
        ):
            with self.subTest(case=case), self.assertRaises(SourceArchiveError):
                read_office(package({**members, name: raw}), "docx")

    def test_metadata_limits_refuse_nonfinite_and_non_numeric_values(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_metadata

        for value in (True, "100", -1, float("inf"), float("nan"), 10**1000):
            for key in ("duration", "dpi"):
                with (
                    self.subTest(key=key, value_type=type(value)),
                    self.assertRaises(SourceArchiveError),
                ):
                    _image_metadata({key: (value, 72) if key == "dpi" else value}, "jpeg")

    def test_image_frame_budget_is_package_wide_and_requires_positive_counts(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = image_document(png_image())
        for count in (0, -1, True, 257):
            with self.subTest(count=count), self.assertRaises(SourceArchiveError):
                read_office(raw, "docx", inspect_image=mock.Mock(return_value=count))
        two = image_document(png_image(), "word/media/second.png", raw)
        with self.assertRaises(SourceArchiveError):
            read_office(two, "docx", inspect_image=mock.Mock(return_value=129))

    def test_png_requires_exact_scanlines_and_complete_compressed_input(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_envelope

        image = png_image()
        for case, compressed in (
            ("hidden suffix", zlib.compress(b"\x00\xff\xff\xff") + b"PRIVATE-SYNTHETIC"),
            ("second stream", zlib.compress(b"\x00\xff\xff\xff") + zlib.compress(b"x")),
            ("too many pixels", zlib.compress(b"\x00\xff\xff\xffextra")),
            ("too few pixels", zlib.compress(b"\x00\xff")),
            ("invalid filter", zlib.compress(b"\x05\xff\xff\xff")),
        ):
            with self.subTest(case=case), self.assertRaises(SourceArchiveError):
                _image_envelope(image[:33] + png_chunk(b"IDAT", compressed) + image[-12:], "png")

    def test_png_ancillary_chunks_cannot_hide_extra_bodies(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_envelope

        image = png_image()
        for tag in (b"sRGB", b"gAMA", b"pHYs", b"cHRM", b"tRNS", b"PLTE"):
            with self.subTest(tag=tag), self.assertRaises(SourceArchiveError):
                _image_envelope(
                    image[:33] + png_chunk(tag, b"SYNTHETIC-UNINSPECTED!") + image[33:], "png"
                )

    def test_truncated_jpeg_is_not_a_valid_image_envelope(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_envelope

        # The approved decoder policy still requires an actual image envelope.
        with self.assertRaises(SourceArchiveError) as caught:
            _image_envelope(b"\xff\xd8\xff\xd9", "jpeg")
        self.assertEqual(caught.exception.code, "source_corrupt")

    def test_truecolor_png_cannot_retain_an_unused_palette(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_envelope

        image = png_image()
        payload = image[:33] + png_chunk(b"PLTE", b"PRIVATE" * 3) + image[33:]
        with self.assertRaises(SourceArchiveError):
            _image_envelope(payload, "png")

    def test_image_envelope_refuses_metadata_trailing_bytes_and_invalid_crc(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_envelope

        image = png_image()
        _image_envelope(image, "png")
        broken_crc = bytearray(image)
        broken_crc[-1] ^= 1
        # Even harmless text metadata is outside the inspected raster subset.
        data = b"note\x00private wording"
        chunk = (
            struct.pack("!I", len(data))
            + b"tEXt"
            + data
            + struct.pack("!I", zlib.crc32(b"tEXt" + data))
        )
        for case, payload in (
            ("trailing", image + b"not image data"),
            ("crc", bytes(broken_crc)),
            ("metadata", image[:-12] + chunk + image[-12:]),
            ("truncated", image[:-1]),
        ):
            with self.subTest(case=case), self.assertRaises(SourceArchiveError):
                _image_envelope(payload, "png")

    def test_image_parts_require_a_qualified_decoder_and_keep_original_binding(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        image = png_image()
        raw = image_document(image)
        with self.assertRaises(SourceArchiveError):
            read_office(raw, "docx")
        inspector = mock.Mock(return_value=1)
        reading = read_office(raw, "docx", inspect_image=inspector)
        inspector.assert_called_once_with(image, "png")
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual(reading.units[0].text, "Not approved.")
        self.assertEqual(reading.reader_id, "tedd-ooxml:6")
        self.assertIn("embedded_images_require_visual_review", reading.gaps)
        self.assertIn("office_layout_not_rendered", reading.gaps)

    def test_image_inspection_failure_cannot_be_stripped_from_original(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        inspector = mock.Mock(side_effect=SourceArchiveError("privacy_rejected"))
        with self.assertRaises(SourceArchiveError) as caught:
            read_office(image_document(png_image()), "docx", inspect_image=inspector)
        self.assertEqual(caught.exception.code, "privacy_rejected")

    def test_image_relationship_must_resolve_inside_the_same_package(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        with zipfile.ZipFile(io.BytesIO(image_document(png_image()))) as original:
            members = {item.filename: original.read(item) for item in original.infolist()}
        for target, mode, valid in (
            ("media/image.png", "Internal", True),
            ("/word/media/image.png", "Internal", True),
            ("media/missing.png", "Internal", False),
            ("document.xml", "Internal", False),
            ("../../../escape.png", "Internal", False),
            ("https://example.invalid/image.png", "External", False),
        ):
            raw = package(
                {
                    **members,
                    "word/_rels/document.xml.rels": (
                        f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/image" '
                        f'Target="{target}" TargetMode="{mode}"/></Relationships>'
                    ).encode(),
                }
            )
            with self.subTest(target=target):
                if valid:
                    self.assertEqual(
                        read_office(
                            raw, "docx", inspect_image=mock.Mock(return_value=1)
                        ).original_sha256,
                        digest(raw),
                    )
                else:
                    with self.assertRaises(SourceArchiveError):
                        read_office(raw, "docx", inspect_image=mock.Mock(return_value=1))

    def test_missing_optional_image_decoder_refuses_without_installation(self) -> None:
        from tedd_knowledge_graph.source_office_host import inspect_image

        with (
            mock.patch("importlib.import_module", side_effect=ModuleNotFoundError),
            self.assertRaises(SourceArchiveError) as caught,
        ):
            inspect_image(png_image(), "png")
        self.assertEqual(caught.exception.code, "capability_unavailable")

    def test_image_relationship_ids_are_present_and_unique(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        with zipfile.ZipFile(io.BytesIO(image_document(png_image()))) as original:
            members = {item.filename: original.read(item) for item in original.infolist()}
        image_relation = f'<Relationship Id="r1" Type="{R}/image" Target="media/image.png"/>'
        for body in (
            image_relation * 2,
            image_relation.replace('Id="r1" ', ""),
            image_relation
            + f'<Relationship Id="r1" Type="{R}/hyperlink" Target="https://example.invalid" TargetMode="External"/>',
        ):
            raw = package(
                {
                    **members,
                    "word/_rels/document.xml.rels": f'<Relationships xmlns="{REL}">{body}</Relationships>'.encode(),
                }
            )
            with self.subTest(body=body), self.assertRaises(SourceArchiveError):
                read_office(raw, "docx", inspect_image=mock.Mock(return_value=1))

    def test_streamed_member_descriptor_is_checked_without_changing_its_reading(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = word_document()
        directory = struct.unpack_from("<I", raw, len(raw) - 6)[0]
        crc, compressed, expanded = struct.unpack_from("<3I", raw, 14)
        for signature in (b"", b"PK\x07\x08"):
            with self.subTest(signature=signature):
                descriptor = signature + struct.pack("<3I", crc, compressed, expanded)
                payload = bytearray(raw[:directory] + descriptor + raw[directory:])
                struct.pack_into("<H", payload, 6, 8)
                struct.pack_into("<3I", payload, 14, 0, 0, 0)
                struct.pack_into("<H", payload, directory + len(descriptor) + 8, 8)
                struct.pack_into("<I", payload, len(payload) - 6, directory + len(descriptor))
                self.assertEqual(read_office(bytes(payload), "docx").units[0].text, "Not approved.")
                payload[directory + len(signature)] ^= 1
                with self.assertRaises(SourceArchiveError):
                    read_office(bytes(payload), "docx")

    def test_member_bytes_ignored_by_zipfile_are_not_preserved(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                stream = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(word_document())) as source:
                    raw_xml = source.read("word/document.xml")
                with zipfile.ZipFile(stream, "w", compression=compression) as archive:
                    archive.writestr("word/document.xml", raw_xml)
                payload = bytearray(stream.getvalue())
                directory = struct.unpack_from("<I", payload, len(payload) - 6)[0]
                size = struct.unpack_from("<I", payload, 18)[0]
                marker = b"password=neutral-test-secret"
                payload[directory:directory] = marker
                directory += len(marker)
                struct.pack_into("<I", payload, 18, size + len(marker))
                struct.pack_into("<I", payload, directory + 20, size + len(marker))
                struct.pack_into("<I", payload, len(payload) - 6, directory)
                # The standard reader ignores these bytes; Tedd must refuse them.
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertEqual(archive.read("word/document.xml"), raw_xml)
                with self.assertRaises(SourceArchiveError):
                    read_office(bytes(payload), "docx")

    def test_unscreened_zip_container_data_is_refused(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        marker = b"password=neutral-test-secret"
        for field in ("archive_comment", "member_comment", "extra", "name", "prefix", "suffix"):
            stream = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(word_document())) as source:
                raw_xml = source.read("word/document.xml")
            member = zipfile.ZipInfo("word/document.xml")
            if field == "member_comment":
                member.comment = marker
            elif field == "extra":
                member.extra = b"\xfe\xca" + len(marker).to_bytes(2, "little") + marker
            with zipfile.ZipFile(stream, "w") as package_file:
                package_file.writestr(member, raw_xml)
                if field == "archive_comment":
                    package_file.comment = marker
                if field == "name":
                    package_file.writestr("word/password=neutral-test-secret.xml", b"<x/>")
            payload = stream.getvalue()
            if field == "prefix":
                payload = marker + payload
            elif field == "suffix":
                payload += marker
            with self.subTest(field=field), self.assertRaises(SourceArchiveError):
                read_office(payload, "docx")

    def test_cli_preparation_uses_the_real_disposable_worker(self) -> None:
        import tedd_sources

        raw = word_document()
        if sys.platform == "darwin":
            # This is a supported refusal, not Mac reader qualification. Keep
            # the real child path: do not mock the resource gate into success.
            with self.assertRaises(SourceArchiveError) as refused:
                tedd_sources.office_reading(
                    raw, "docx", argparse.Namespace(reader_python=sys.executable)
                )
            self.assertEqual(refused.exception.code, "capability_unavailable")
            return
        reading = tedd_sources.office_reading(
            raw, "docx", argparse.Namespace(reader_python=sys.executable)
        )
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual(reading.kind, "docx")
        self.assertEqual(reading.units[0].text, "Not approved.")

    def test_worker_is_bounded_and_returns_bound_derivatives_not_paths(self) -> None:
        raw = word_document()
        worker = GRAPH_SCRIPTS / "tedd_knowledge_graph" / "source_office_host.py"
        result = office_worker(raw, "docx")
        if sys.platform == "darwin":
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "status": "capability_unavailable",
                    "reason": "macos_worker_boundary_unqualified",
                },
            )
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "text_screened")
        self.assertEqual(output["original_sha256"], digest(raw))
        self.assertEqual(output["kind"], "docx")
        self.assertEqual(output["resources"], "bounded")
        self.assertIn("office_layout_not_rendered", output["gaps"])
        self.assertNotIn(str(worker).encode(), result.stdout)

    def test_docx_keeps_paragraph_table_order_and_native_locations(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = word_document()
        reading = read_office(raw, "docx")
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual(reading.units[0].text, "Not approved.")
        self.assertIn("Condition", reading.units[1].text)
        self.assertIn("Review first", reading.units[1].text)
        self.assertEqual(reading.units[2].text, "Only then.")
        self.assertEqual(
            reading.units[0].location, SourceLocation("paragraph", "word/document.xml", 1, 1)
        )
        self.assertEqual(reading.units[1].location, SourceLocation("cell", "table:1", 1, 1, 1, 2))

    def test_word_tracked_moves_are_not_presented_as_accepted_wording(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = package(
            {
                "word/document.xml": (
                    f'<w:document xmlns:w="{W}"><w:body><w:p>'
                    '<w:moveFrom w:id="1"><w:r><w:delText>Do not ship.</w:delText></w:r>'
                    '</w:moveFrom></w:p><w:p><w:moveTo w:id="2"><w:r>'
                    "<w:t>Do not ship.</w:t></w:r></w:moveTo></w:p></w:body></w:document>"
                ).encode()
            }
        )
        reading = read_office(raw, "docx")
        self.assertEqual(
            [unit.text for unit in reading.units],
            ["[moveFrom: Do not ship.]", "[moveTo: Do not ship.]"],
        )
        self.assertIn("unresolved_suggestions", reading.gaps)
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual(reading.reader_id, "tedd-ooxml:8")

    def test_word_note_callouts_preserve_identity_and_report_missing_context(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        for kind in ("footnote", "endnote"):
            for available in (True, False):
                with self.subTest(kind=kind, available=available):
                    raw = noted_word_document(kind, include_note=available)
                    reading = read_office(raw, "docx")
                    self.assertEqual(reading.original_sha256, digest(raw))
                    self.assertIn(f"[{kind}:7]", reading.units[1].text)
                    self.assertEqual(reading.reader_id, "tedd-ooxml:7")
                    self.assertEqual("note_context_unavailable" in reading.gaps, not available)
                    if available:
                        self.assertEqual(
                            reading.units[-1].location,
                            SourceLocation("paragraph", f"word/{kind}s.xml:7", 1, 1),
                        )
                        self.assertEqual(
                            reading.units[-1].text,
                            "Do not ship if backup verification failed.",
                        )

    def test_word_note_callout_without_identity_is_not_silently_dropped(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = package(
            {
                "word/document.xml": f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>May ship.</w:t><w:footnoteReference/></w:r></w:p></w:body></w:document>'.encode()
            }
        )
        with self.assertRaises(SourceArchiveError):
            read_office(raw, "docx")

    def test_xlsx_keeps_second_sheet_formula_and_saved_result_without_recalculation(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = conditional_workbook()
        reading = read_office(raw, "xlsx")
        self.assertIn("=Conditions!A2", reading.units[1].text)
        row = reading.units[3]
        self.assertEqual(row.location, SourceLocation("cell", "7", 2, 2, 1, 1))
        for value in ("Amount USD", "=7/2", "3.5000", "hidden"):
            self.assertIn(value, row.text)
        self.assertIn("displayed_value_unavailable", reading.gaps)
        self.assertIn("filter_view_state_requires_check", reading.gaps)

    def test_pptx_uses_presentation_order_not_zip_member_order(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        def slide(value: str) -> bytes:
            return f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{value}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'.encode()

        raw = package(
            {
                "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="257" r:id="r2"/><p:sldId id="256" r:id="r1"/></p:sldIdLst></p:presentation>'.encode(),
                "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/slide" Target="slides/slide1.xml"/><Relationship Id="r2" Type="{R}/slide" Target="slides/slide2.xml"/></Relationships>'.encode(),
                "ppt/slides/slide1.xml": slide("Only after review."),
                "ppt/slides/slide2.xml": slide("Not approved."),
            }
        )
        reading = read_office(raw, "pptx")
        self.assertEqual(
            [unit.text for unit in reading.units], ["Not approved.", "Only after review."]
        )
        self.assertEqual(reading.units[0].location, SourceLocation("slide", "257", 1, 1))
        self.assertIn("speaker_notes_availability_unknown", reading.gaps)

    def test_xlsx_missing_shared_formula_is_unknown_not_an_empty_expression(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = package(
            {
                "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Neutral" sheetId="1" r:id="r1"/></sheets></workbook>'.encode(),
                "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
                "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1"><f t="shared" si="0" ref="A1:A2">7/2</f><v>3.5</v></c></row><row r="2"><c r="A2"><f t="shared" si="0"/><v>0</v></c></row></sheetData></worksheet>'.encode(),
            }
        )
        reading = read_office(raw, "xlsx")
        self.assertEqual(reading.reader_id, "tedd-ooxml:2")
        self.assertIn("A1: formula: =7/2; saved n: 3.5", reading.units[0].text)
        self.assertIn("A2: formula: unavailable; saved n: 0", reading.units[1].text)
        self.assertNotIn("A2: formula: =", reading.units[1].text)
        self.assertIn("formula_expression_unavailable", reading.gaps)
        self.assertIn("shared_or_array_formula_requires_check", reading.gaps)
        self.assertIn("formula_cache_freshness_unknown", reading.gaps)

    def test_package_root_relationship_is_not_treated_as_a_filesystem_path(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        raw = package(
            {
                "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Neutral" sheetId="1" r:id="r1"/></sheets></workbook>'.encode(),
                "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="/xl/worksheets/sheet1.xml"/></Relationships>'.encode(),
                "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Not approved.</t></is></c></row></sheetData></worksheet>'.encode(),
            }
        )
        reading = read_office(raw, "xlsx")
        self.assertIn("Not approved.", reading.units[0].text)

    def test_uninspectable_active_or_unsafe_packages_are_refused(self) -> None:
        from tedd_knowledge_graph.source_office import read_office

        for name, content in (
            ("word/vbaProject.bin", b"macro"),
            ("word/media/image.png", b"opaque"),
            ("../escape.xml", b"<root/>"),
            ("word/document.xml", b'<!DOCTYPE x [<!ENTITY y "secret">]><x>&y;</x>'),
            ("word/document.xml", b"<root>password=not-a-real-secret</root>"),
            (
                "word/_rels/document.xml.rels",
                f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/image" Target="https://example.invalid/private" TargetMode="External"/></Relationships>'.encode(),
            ),
        ):
            with self.subTest(member=name), self.assertRaises(SourceArchiveError):
                read_office(package({name: content}), "docx")
        with self.assertRaises(SourceArchiveError):
            read_office(word_document(), "unknown")
        with self.assertRaises(SourceArchiveError):
            read_office(word_document(), "docx", maximum_bytes=1)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Optional Pillow worker is not installed")
class OfficeImageWorkerTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    @unittest.skipUnless(
        importlib.util.find_spec("pptx"), "Optional PowerPoint fixture generator unavailable"
    )
    def test_unmodified_powerpoint_print_metadata_preserves_conditions_and_notes(self) -> None:
        module = __import__("pptx", fromlist=["Presentation"])
        document = module.Presentation()
        first = document.slides.add_slide(document.slide_layouts[1])
        first.shapes.title.text = "Not approved."
        first.notes_slide.notes_text_frame.text = "Only after independent review."
        second = document.slides.add_slide(document.slide_layouts[1])
        second.shapes.title.text = "The condition still applies."
        stream = io.BytesIO()
        document.save(stream)
        raw = stream.getvalue()
        result = office_worker(raw, "pptx")
        self.assertEqual(result.returncode, 0, result.stdout)
        reading = json.loads(result.stdout)
        self.assertEqual(reading["original_sha256"], digest(raw))
        texts = "\n".join(unit.text for unit in decode_units(encode(reading["units"])))
        for expected in (
            "Not approved.",
            "Only after independent review.",
            "The condition still applies.",
        ):
            self.assertIn(expected, texts)

    def test_jpeg_accepts_one_null_padding_byte_but_not_arbitrary_suffixes(self) -> None:
        from tedd_knowledge_graph.source_office_host import inspect_image

        module = __import__("PIL.Image", fromlist=["Image"])
        stream = io.BytesIO()
        module.new("RGB", (2, 2), "white").save(stream, format="JPEG")
        raw = stream.getvalue()
        self.assertEqual(inspect_image(raw + b"\0", "jpeg"), 1)
        for tail in (b"\0\0", b"x", b"\0x", b"private-body"):
            with self.subTest(tail=tail), self.assertRaises(SourceArchiveError):
                inspect_image(raw + tail, "jpeg")

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    @unittest.skipUnless(
        importlib.util.find_spec("docx"), "Optional Word fixture generator unavailable"
    )
    def test_unmodified_word_metadata_preserves_original_and_condition(self) -> None:
        module = __import__("docx", fromlist=["Document"])
        document = module.Document()
        document.add_paragraph("Not approved unless reviewed.")
        document.add_picture(io.BytesIO(png_image()))
        stream = io.BytesIO()
        document.save(stream)
        original = stream.getvalue()
        response = office_worker(original, "docx")
        self.assertEqual(response.returncode, 0, response.stdout)
        result = json.loads(response.stdout)
        self.assertEqual(result["original_sha256"], digest(original))
        self.assertEqual(result["reader_id"], "tedd-ooxml:6")
        self.assertEqual(
            decode_units(encode(result["units"]))[0].text, "Not approved unless reviewed."
        )
        self.assertIn("office_layout_not_rendered", result["gaps"])
        self.assertIn("embedded_images_require_visual_review", result["gaps"])

    def test_numeric_exif_is_accepted_but_private_or_unknown_fields_are_not(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_metadata

        image_module = __import__("PIL.Image", fromlist=["Image"])
        exif = image_module.Exif()
        exif[0x011A] = 72
        exif[0x011B] = 72
        exif[0x0128] = 2
        _image_metadata({"exif": exif.tobytes()}, "jpeg")
        for tag, value in (
            (0x010E, "password=SYNTHETIC-ONLY"),
            (0x927C, b"opaque"),
            (0x8825, {}),
            (0x011A, 0),
        ):
            with self.subTest(tag=tag), self.assertRaises(SourceArchiveError):
                invalid = image_module.Exif()
                invalid[tag] = value
                _image_metadata({"exif": invalid.tobytes()}, "jpeg")
        for raw in (b"Exif\0\0broken", b"X" * 65537):
            with self.subTest(size=len(raw)), self.assertRaises(SourceArchiveError):
                _image_metadata({"exif": raw}, "jpeg")

    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("PIL.ImageCms"),
        "Optional color reader unavailable",
    )
    def test_bounded_color_profile_is_validated(self) -> None:
        from tedd_knowledge_graph.source_office_host import _image_metadata

        cms = __import__("PIL.ImageCms", fromlist=["ImageCmsProfile"])
        profile = cms.ImageCmsProfile(cms.createProfile("sRGB")).tobytes()
        _image_metadata({"icc_profile": profile}, "jpeg")
        unknown = bytearray(profile)
        unknown[132:136] = b"priv"
        overlapping = bytearray(profile)
        overlapping[136:140] = (128).to_bytes(4, "big")
        for raw in (
            profile[:-1],
            profile + b"secret",
            b"X" * 262145,
            bytes(unknown),
            bytes(overlapping),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(SourceArchiveError):
                _image_metadata({"icc_profile": raw}, "jpeg")

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("PIL.ImageCms"),
        "Optional color reader unavailable",
    )
    def test_bounded_color_profile_is_never_copied_to_preview(self) -> None:
        cms = __import__("PIL.ImageCms", fromlist=["ImageCmsProfile"])
        image_module = __import__("PIL.Image", fromlist=["Image"])
        profile = cms.ImageCmsProfile(cms.createProfile("sRGB")).tobytes()
        stream = io.BytesIO()
        image_module.new("RGB", (2, 2), "white").save(stream, format="JPEG", icc_profile=profile)
        result = office_worker(
            image_document(stream.getvalue(), "word/media/image.jpeg"), "docx", "--render-unit", "4"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        png = base64.b64decode(json.loads(result.stdout)["png_base64"], validate=True)
        with image_module.open(io.BytesIO(png)) as preview:
            self.assertEqual(preview.info, {})

    def test_profile_text_cannot_hide_credentials_in_an_accepted_text_type(self) -> None:
        from tedd_knowledge_graph.source_office_metadata import _icc_tag

        text = b"password=SYNTHETIC-ONLY"
        for kind, raw in (
            ("ascii", b"text\0\0\0\0" + text + b"\0"),
            (
                "unicode",
                b"mluc\0\0\0\0"
                + struct.pack(">II", 1, 12)
                + b"enUS"
                + struct.pack(">II", len(text) * 2, 28)
                + text.decode().encode("utf-16-be"),
            ),
        ):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"),
            ):
                _icc_tag(b"cprt", raw)

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_accepted_exif_orientation_is_applied_to_the_pixel_only_preview(self) -> None:
        image_module = __import__("PIL.Image", fromlist=["Image"])
        exif = image_module.Exif()
        exif[0x0112] = 6
        output = io.BytesIO()
        image_module.new("RGB", (2, 3), "white").save(output, format="JPEG", exif=exif.tobytes())
        result = office_worker(
            image_document(output.getvalue(), "word/media/image.jpeg"), "docx", "--render-unit", "4"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        png = base64.b64decode(json.loads(result.stdout)["png_base64"], validate=True)
        with image_module.open(io.BytesIO(png)) as preview:
            self.assertEqual(preview.size, (3, 2))
            self.assertEqual(preview.info, {})

    def test_missing_optional_decoder_reports_capability_unavailable(self) -> None:
        from tedd_knowledge_graph.source_office_host import inspect_image

        image_module = __import__("PIL.Image", fromlist=["Image"])
        self.assertEqual(inspect_image(png_image(), "png"), 1)
        image_module.init()
        with (
            mock.patch.dict(image_module.OPEN, {}, clear=True),
            mock.patch.object(image_module, "init", return_value=None),
            self.assertRaisesRegex(SourceArchiveError, "capability_unavailable"),
        ):
            inspect_image(png_image(), "png")

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_worker_renders_the_selected_animation_frame_without_metadata(self) -> None:
        image_module = __import__("PIL.Image", fromlist=["Image"])
        stream = io.BytesIO()
        frames = [image_module.new("RGB", (2, 2), color) for color in ("red", "blue")]
        frames[0].save(stream, format="GIF", save_all=True, append_images=frames[1:], duration=100)
        raw = image_document(stream.getvalue(), "word/media/image.gif")
        # The fixture has three text units; u4/u5 identify its two image frames.
        for position, color in ((4, (255, 0, 0, 255)), (5, (0, 0, 255, 255))):
            with self.subTest(position=position):
                result = office_worker(raw, "docx", "--render-unit", str(position))
                self.assertEqual(result.returncode, 0, result.stdout)
                output = json.loads(result.stdout)
                self.assertEqual(output["original_sha256"], digest(raw))
                self.assertEqual(output["position"], position)
                png = base64.b64decode(output["png_base64"], validate=True)
                self.assertEqual(output["png_sha256"], digest(png))
                with image_module.open(io.BytesIO(png)) as preview:
                    self.assertEqual(preview.getpixel((0, 0)), color)
                    self.assertEqual(preview.info, {})
        for position in (0, 1, 6):
            with self.subTest(invalid_position=position):
                result = office_worker(raw, "docx", "--render-unit", str(position))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("png_base64", json.loads(result.stdout))

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_worker_reads_common_still_images_without_changing_original_bytes(self) -> None:
        image_module = __import__("PIL.Image", fromlist=["Image"])
        for kind in ("JPEG", "WEBP", "AVIF", "GIF", "BMP"):
            with self.subTest(kind=kind):
                stream = io.BytesIO()
                image_module.init()
                if kind not in image_module.SAVE:
                    self.skipTest(f"Optional {kind} encoder is not installed")
                image_module.new("RGB", (2, 2), "white").save(stream, format=kind)
                raw = image_document(stream.getvalue(), f"word/media/image.{kind.lower()}")
                result = office_worker(raw, "docx")
                self.assertEqual(result.returncode, 0, result.stdout)
                reading = json.loads(result.stdout)
                self.assertEqual(reading["original_sha256"], digest(raw))
                self.assertEqual(decode_units(encode(reading["units"]))[0].text, "Not approved.")
                self.assertIn("embedded_images_require_visual_review", reading["gaps"])
                self.assertIn("office_layout_not_rendered", reading["gaps"])

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_worker_retains_a_distinct_reference_for_every_animation_frame(self) -> None:
        image_module = __import__("PIL.Image", fromlist=["Image"])
        for kind in ("GIF", "WEBP", "AVIF"):
            with self.subTest(kind=kind):
                stream = io.BytesIO()
                frames = [image_module.new("RGB", (2, 2), color) for color in ("red", "blue")]
                image_module.init()
                if kind not in image_module.SAVE_ALL:
                    self.skipTest(f"Optional {kind} animation encoder is not installed")
                frames[0].save(
                    stream,
                    format=kind,
                    save_all=True,
                    append_images=frames[1:],
                    duration=[100, 200],
                    loop=0,
                )
                raw = image_document(stream.getvalue(), f"word/media/image.{kind.lower()}")
                result = office_worker(raw, "docx")
                self.assertEqual(result.returncode, 0, result.stdout)
                reading = json.loads(result.stdout)
                visual = [
                    unit.location
                    for unit in decode_units(encode(reading["units"]))
                    if unit.kind == "image_page"
                ]
                self.assertTrue(all(location is not None for location in visual))
                self.assertEqual([location.start for location in visual if location], [1, 2])
                self.assertEqual(
                    {location.container for location in visual if location},
                    {f"word/media/image.{kind.lower()}"},
                )
                self.assertIn("animation_timing_requires_check", reading["gaps"])

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_worker_refuses_image_metadata_instead_of_forwarding_it(self) -> None:
        image_module = __import__("PIL.Image", fromlist=["Image"])
        for kind, metadata in (
            ("JPEG", {"comment": b"password=SYNTHETIC-DO-NOT-RETAIN"}),
            ("WEBP", {"xmp": b"SYNTHETIC-UNINSPECTED-METADATA"}),
            ("AVIF", {"xmp": b"SYNTHETIC-UNINSPECTED-METADATA"}),
            ("GIF", {"comment": b"SYNTHETIC-UNINSPECTED-METADATA"}),
        ):
            with self.subTest(kind=kind):
                stream = io.BytesIO()
                image_module.init()
                if kind not in image_module.SAVE:
                    self.skipTest(f"Optional {kind} encoder is not installed")
                image_module.new("RGB", (2, 2), "white").save(
                    stream, format=kind, **({} if kind == "JPEG" else metadata)
                )
                payload = stream.getvalue()
                if kind == "JPEG":
                    # Older Pillow ignores the JPEG comment save argument.
                    comment = metadata["comment"]
                    segment = b"\xff\xfe" + struct.pack("!H", len(comment) + 2) + comment
                    payload = payload[:2] + segment + payload[2:]
                result = office_worker(
                    image_document(payload, f"word/media/image.{kind.lower()}"), "docx"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"SYNTHETIC", result.stdout)
                self.assertNotIn("units", json.loads(result.stdout))

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_real_worker_decodes_png_and_preserves_condition_in_each_office_kind(self) -> None:
        # These are package-reader fixtures, not a claim that Office opened them.
        documents = {
            "docx": word_document(),
            "xlsx": package(
                {
                    "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Summary" sheetId="1" r:id="r1"/><sheet name="Conditions" sheetId="2" r:id="r2"/></sheets></workbook>'.encode(),
                    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Type="{R}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>'.encode(),
                    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1"><f>7/2</f><v>3.5</v></c></row></sheetData></worksheet>'.encode(),
                    "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Not approved.</t></is></c></row></sheetData></worksheet>'.encode(),
                }
            ),
            "pptx": package(
                {
                    "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="r1"/><p:sldId id="257" r:id="r2"/></p:sldIdLst></p:presentation>'.encode(),
                    "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="r1" Type="{R}/slide" Target="slides/slide1.xml"/><Relationship Id="r2" Type="{R}/slide" Target="slides/slide2.xml"/></Relationships>'.encode(),
                    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>Review first.</a:t></a:r></a:p></p:sld>'.encode(),
                    "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>Not approved.</a:t></a:r></a:p></p:sld>'.encode(),
                    "ppt/slides/_rels/slide2.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="notes" Type="{R}/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>'.encode(),
                    "ppt/notesSlides/notesSlide1.xml": f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Only after sign-off.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>'.encode(),
                }
            ),
        }
        for kind, document in documents.items():
            root = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[kind]
            raw = image_document(png_image(), f"{root}/media/image.png", document)
            with self.subTest(kind=kind):
                result = office_worker(raw, kind)
                self.assertEqual(result.returncode, 0, result.stdout)
                output = json.loads(result.stdout)
                self.assertEqual(output["original_sha256"], digest(raw))
                self.assertEqual(output["reader_id"], "tedd-ooxml:6")
                self.assertIn("embedded_images_require_visual_review", output["gaps"])
                self.assertIn("Not approved.", result.stdout.decode())
                if kind == "xlsx":
                    self.assertIn("3.5", result.stdout.decode())
                if kind == "pptx":
                    self.assertIn("Only after sign-off.", result.stdout.decode())

    @unittest.skipIf(sys.platform == "darwin", "macOS native worker boundary is unqualified")
    def test_real_worker_refuses_hidden_png_bytes_but_keeps_approved_jpeg_originals(self) -> None:
        image = png_image()
        hidden = (
            image[:33]
            + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff") + b"PRIVATE-SYNTHETIC")
            + image[-12:]
        )
        # This optional fixture builder stays out of the dependency-free runtime.
        image_module = __import__("PIL.Image", fromlist=["Image"])
        stream = io.BytesIO()
        image_module.new("RGB", (1, 1), "white").save(stream, format="JPEG")
        jpeg = stream.getvalue()
        for name, payload, accepted in (
            ("image.png", hidden, False),
            ("image.jpeg", jpeg, True),
            ("image.jpeg", jpeg[:-2] + b"PRIVATE-SYNTHETIC" + jpeg[-2:], True),
            ("image.jpeg", jpeg + b"PRIVATE-SYNTHETIC", False),
        ):
            with self.subTest(name=name, size=len(payload)):
                raw = image_document(payload, "word/media/" + name)
                result = office_worker(raw, "docx")
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["original_sha256"], digest(raw))
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("units", json.loads(result.stdout))
                self.assertNotIn(b"PRIVATE-SYNTHETIC", result.stdout)


if __name__ == "__main__":
    unittest.main()
