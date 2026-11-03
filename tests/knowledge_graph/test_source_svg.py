"""Restricted SVG admission tests; rendering and retention are separate gates."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError, digest
from tedd_knowledge_graph.source_image_svg import inspect_svg

from tests.knowledge_graph.test_source_office import office_worker

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="180" viewBox="0 0 480 180">
<defs>
 <linearGradient id="g"><stop offset="0" stop-color="red"/><stop offset="1" stop-color="blue"/></linearGradient>
 <clipPath id="clip"><rect x="20" y="20" width="150" height="50"/></clipPath>
 <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8Z" fill="black"/></marker>
</defs>
<rect width="480" height="180" fill="white"/>
<g clip-path="url(#clip)"><rect x="20" y="20" width="200" height="50" fill="url(#g)"/></g>
<circle cx="210" cy="45" r="25" fill="lime"/>
<path d="M260 45 L360 45" stroke="black" stroke-width="3" marker-end="url(#arrow)"/>
<text x="20" y="132" font-family="Source Sans Pro" font-size="34">DO NOT STOP</text>
</svg>"""


def document(body: bytes, attributes: bytes = b'width="20" height="10"') -> bytes:
    return b'<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + b">" + body + b"</svg>"


class SvgAdmissionTests(unittest.TestCase):
    def test_static_profile_retains_identity_and_accounts_for_internal_references(self) -> None:
        original = digest(SVG)
        result = inspect_svg(SVG)
        self.assertEqual(result.size, (480, 180))
        self.assertEqual(result.source_sha256, original)
        self.assertEqual(result.font_families, ("Source Sans Pro",))
        self.assertEqual(result.metadata_categories, ())
        self.assertEqual(digest(SVG), original)
        self.assertEqual(
            inspect_svg(document(b'<rect width="1" height="1"/>', b'viewBox="0 0 20 10"')).size,
            (20, 10),
        )

    def test_active_external_unknown_and_namespace_confused_constructs_refuse(self) -> None:
        cases = (
            b"<script/>",
            b"<foreignObject/>",
            b'<image href="data:image/png;base64,AA=="/>',
            b"<style/>",
            b"<animate/>",
            b"<filter/>",
            b'<use href="#x"/>',
            b'<rect onclick="run()"/>',
            b'<rect style="fill:red"/>',
            b'<rect xmlns="urn:other"/>',
            b'<rect xmlns:x="urn:other" x:fill="red"/>',
            b'<g xml:base="https://example.invalid/"/>',
            b'<rect fill="url(https://example.invalid/a)"/>',
            b'<rect fill="url(&#x66;ile:///private)"/>',
            b'<linearGradient href="https://example.invalid/a"/>',
            b'<rect fill="u\\72l(#x)"/>',
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(SourceArchiveError):
                inspect_svg(document(body))

    def test_dtd_entities_processing_instructions_and_unreviewed_comments_refuse(self) -> None:
        cases = (
            b'<!DOCTYPE svg [<!ENTITY value "hidden">]>' + document(b"<text>&value;</text>"),
            b'<?xml-stylesheet href="file:///private"?>' + SVG,
            document(b"<!-- unreviewed hidden text -->"),
            document(b"<text><![CDATA[hidden]]></text>"),
            SVG.decode().encode("utf-16"),
            b"<svg",
        )
        for payload in cases:
            with self.subTest(prefix=payload[:40]), self.assertRaises(SourceArchiveError):
                inspect_svg(payload)
        self.assertEqual(
            inspect_svg(b'<?xml version="1.0" encoding="UTF-8"?>' + SVG).size, (480, 180)
        )

    def test_reference_cycles_missing_wrong_kind_and_duplicate_ids_refuse(self) -> None:
        cases = (
            b'<rect fill="url(#missing)"/>',
            b'<rect id="x"/><rect id="x"/>',
            b'<rect id="x"/><rect fill="url(#x)"/>',
            b'<defs><linearGradient id="a" href="#b"/><linearGradient id="b" href="#a"/></defs>',
            b'<defs><clipPath id="clip"><rect width="1" height="1" clip-path="url(#clip)"/></clipPath></defs>',
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(SourceArchiveError):
                inspect_svg(document(body))

    def test_namespace_aliases_and_bounded_acyclic_reference_expansion(self) -> None:
        valid = document(
            b'<defs><linearGradient id="a"/><linearGradient id="b" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#a"/></defs>'
            b'<rect fill="url(#b)"/>'
        )
        self.assertEqual(inspect_svg(valid).size, (20, 10))
        duplicate = valid.replace(b'xlink:href="#a"', b'href="#a" xlink:href="#a"')
        with self.assertRaises(SourceArchiveError):
            inspect_svg(duplicate)
        self.assertEqual(
            inspect_svg(
                b'<s:svg xmlns:s="http://www.w3.org/2000/svg" width="20" height="10"/>'
            ).size,
            (20, 10),
        )
        # Few XML nodes can still describe excessive expanded drawing work.
        # Every edge is forward-only; this is not merely another cycle test.
        chain = b'<clipPath id="c18"><rect width="1" height="1"/></clipPath>'
        for index in range(18):
            member = f'<rect width="1" height="1" clip-path="url(#c{index + 1})"/>'.encode()
            chain += f'<clipPath id="c{index}">'.encode() + member * 2 + b"</clipPath>"
        with self.assertRaises(SourceArchiveError) as failure:
            inspect_svg(document(b"<defs>" + chain + b"</defs>"))
        self.assertEqual(failure.exception.code, "limit_exceeded")

    def test_invalid_geometry_path_and_bounded_work_refuse(self) -> None:
        cases = (
            document(b"", b'width="NaN" height="10"'),
            document(b"", b'width="131073" height="1"'),
            document(b"", b'width="128000" height="1001"'),
            document(b"", b'viewBox="0 0 -1 10"'),
            document(b'<path d="M0 0 L"/>'),
            document(b'<path d="M0 0 A1 1 0 2 0 4 5"/>'),
            document(b'<rect width="-1" height="2"/>'),
            document(b'<g transform="matrix(1 0 0)"/>'),
            document(b'<rect x="' + b"0" * 4096 + b'x"/>'),
            document(b"<g>" * 40 + b"</g>" * 40),
            document(b"<rect/>" * 4097),
        )
        for payload in cases:
            with self.subTest(prefix=payload[:90]), self.assertRaises(SourceArchiveError):
                inspect_svg(payload)

    def test_description_is_classified_but_never_returned(self) -> None:
        result = inspect_svg(document(b"<title>Neutral diagram</title><desc>Owner caption</desc>"))
        self.assertEqual(result.metadata_categories, ("description",))
        self.assertNotIn("Owner caption", repr(result))

    def test_decoded_attribute_secrets_refuse_but_benign_character_references_work(self) -> None:
        marker = "ghp_" + "A" * 16
        encoded = "".join(f"&#x{ord(character):x};" for character in marker)
        for value in (marker, encoded):
            for body in (f'<rect id="{value}"/>', f"<text>{value}</text>"):
                with self.subTest(encoded=value == encoded, text=body.startswith("<text>")):
                    with self.assertRaises(SourceArchiveError) as failure:
                        inspect_svg(document(body.encode()))
                    self.assertEqual(failure.exception.code, "privacy_rejected")
        self.assertEqual(inspect_svg(document(b'<rect id="&#x73;hape"/>')).size, (20, 10))

    def test_unsafe_svg_never_reaches_helper_files_or_process_creation(self) -> None:
        from tedd_knowledge_graph.source_svg_host import SvgRenderer

        renderer = SvgRenderer(Path("unused-renderer"), Path("unused-font"))
        with (
            mock.patch("tedd_knowledge_graph.source_native_host.check_entry") as files,
            mock.patch("tedd_knowledge_graph.source_svg_host.subprocess.run") as process,
        ):
            for payload in (
                document(b"<script/>"),
                document(b'<rect fill="url(file:///private)"/>'),
            ):
                with self.assertRaises(SourceArchiveError):
                    renderer.render(payload)
            files.assert_not_called()
            process.assert_not_called()

    def test_malformed_separators_scalar_lengths_and_unresolved_ids_refuse(self) -> None:
        cases = (
            b'<path d="M,0 0 L1 1"/>',
            b'<path d="M0,,0 L1 1"/>',
            b'<path d="M0 0, L1 1"/>',
            b'<path d="M0 0 L1 1,"/>',
            b'<g transform="translate(0),,scale(1)"/>',
            b'<g transform=",translate(0)"/>',
            b'<rect x="1 2" width="2" height="2"/>',
            b'<path stroke-miterlimit="0" d="M0 0L1 1"/>',
            b'<rect id=" x "/>',
            b'<rect opacity="1px"/>',
            b'<rect opacity="100px%"/>',
            b'<path d="M0 0L1 1" pathLength="20%"/>',
            b'<path d="M0 0L1 1" pathLength="20px"/>',
            b'<defs><marker id="m" orient="90%"/></defs>',
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(SourceArchiveError):
                inspect_svg(document(body))
        valid = (
            b'<path d="M0 0L1-1.5 2.5.5Z"/>',
            b'<g transform="translate(0), scale(1)"/>',
            b'<text x="1 2" y="3">Label</text>',
        )
        for body in valid:
            with self.subTest(body=body):
                self.assertEqual(inspect_svg(document(body)).size, (20, 10))


@unittest.skipUnless(
    os.environ.get("TEDD_TEST_SVG_RENDERER"), "optional SVG qualification not configured"
)
class SvgHostQualificationTests(unittest.TestCase):
    """Dedicated real-helper run sets both paths; missing/wrong artifacts fail."""

    def arguments(self) -> tuple[str, ...]:
        return (
            "--svg-renderer",
            os.environ["TEDD_TEST_SVG_RENDERER"],
            "--svg-font",
            os.environ["TEDD_TEST_SVG_FONT"],
        )

    def test_real_worker_preserves_svg_identity_and_native_detail_pixels(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_images import ImageReading

        for attributes in (None, b'viewBox="0 0 20 10"', b'width="20.2" height="10.2"'):
            payload = (
                SVG
                if attributes is None
                else document(b'<rect width="20" height="10" fill="red"/>', attributes)
            )
            result = office_worker(payload, "image", "--render-unit", "1", *self.arguments())
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stderr, b"")
            reading = ImageReading.decode(payload, json.loads(result.stdout))
            self.assertEqual(reading.inventory.format, "svg")
            self.assertEqual(reading.inventory.original_sha256, digest(payload))
            self.assertIn("svg_fixed_source_sans_pro_font", reading.document.gaps)
        overview = office_worker(SVG, "image", "--render-unit", "1", *self.arguments())
        detail = office_worker(
            SVG, "image", "--render-unit", "1", "--rectangle", "190,20,230,60", *self.arguments()
        )
        self.assertEqual(detail.returncode, 0, detail.stdout)
        base = ImageReading.decode(SVG, json.loads(overview.stdout))
        crop = ImageReading.decode(SVG, json.loads(detail.stdout))
        assert base.png is not None and crop.png is not None
        with io.BytesIO(base.png) as raw, Image.open(raw) as full, closing(full):
            self.assertEqual(full.getpixel((210, 45)), (0, 255, 0, 255))
            self.assertEqual(full.getpixel((180, 45)), (255, 255, 255, 255))
            with (
                io.BytesIO(crop.png) as raw_crop,
                Image.open(raw_crop) as selected,
                closing(selected),
            ):
                with closing(full.crop((190, 20, 230, 60))) as expected:
                    self.assertEqual(selected.tobytes(), expected.tobytes())
        self.assertEqual(crop.rectangle, (190, 20, 230, 60))

    def test_real_worker_refuses_missing_helper_font_substitution_and_missing_glyphs(self) -> None:
        cases = (
            (SVG, ()),
            (SVG.replace(b"Source Sans Pro", b"Unavailable Named Font"), self.arguments()),
            (SVG.replace(b"DO NOT STOP", "\U0001f9ea".encode()), self.arguments()),
            (
                SVG.replace(b'font-size="34"', b'font-size="34" font-weight="bold"'),
                self.arguments(),
            ),
            (
                SVG.replace(b'font-size="34"', b'font-size="34" font-style="italic"'),
                self.arguments(),
            ),
        )
        for payload, arguments in cases:
            with self.subTest(original=digest(payload), configured=bool(arguments)):
                result = office_worker(payload, "image", "--render-unit", "1", *arguments)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout), {"status": "capability_unavailable"})
                self.assertEqual(result.stderr, b"")

    def test_native_failure_keeps_artifacts_guarded_and_closes_owned_descriptors(self) -> None:
        from tedd_knowledge_graph import source_native_host, source_svg_host

        renderer = source_svg_host.SvgRenderer(
            Path(os.environ["TEDD_TEST_SVG_RENDERER"]), Path(os.environ["TEDD_TEST_SVG_FONT"])
        )
        guard = source_native_host.guarded_input_descriptor
        descriptors: list[int] = []

        @contextmanager
        def observed_guard(path: Path) -> Iterator[int]:
            with guard(path) as fd:
                descriptors.append(fd)
                yield fd

        def failed_native(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(len(descriptors), 2)
            for path, descriptor in zip(
                (renderer.executable, renderer.font), descriptors, strict=True
            ):
                info = os.fstat(descriptor)
                current = path.stat()
                self.assertEqual((info.st_dev, info.st_ino), (current.st_dev, current.st_ino))
                if os.name == "nt":
                    # Windows adds mandatory sharing exclusion. POSIX anchors
                    # no-follow descriptors; it cannot promise to stop the same
                    # owner from opening a separate writable descriptor.
                    with self.assertRaises(OSError):
                        fd = os.open(path, os.O_RDWR)
                        os.close(fd)  # An unexpected successful open must not leak.
            return subprocess.CompletedProcess([], 0, b"", b"neutral missing glyph warning")

        with (
            mock.patch.object(source_native_host, "guarded_input_descriptor", observed_guard),
            mock.patch.object(
                source_svg_host.subprocess, "run", side_effect=failed_native
            ) as native,
        ):
            with self.assertRaises(SourceArchiveError) as failure:
                renderer.render(SVG)
            self.assertEqual(failure.exception.code, "capability_unavailable")
            native.assert_called_once()
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_LARGE_IMAGE") == "1", "explicit large-image helper check"
    )
    def test_large_svg_overview_and_bottom_detail_share_existing_worker_budget(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_images import ImageReading

        for width, height, overview in (
            (8000, 6000, (2048, 1536)),
            (12000, 9000, (2048, 1536)),
            (16000, 8000, (2048, 1024)),
        ):
            payload = document(
                (
                    f'<rect width="{width}" height="{height}" fill="white"/>'
                    f'<rect x="{width - 150}" y="{height - 150}" width="100" height="100" fill="lime"/>'
                ).encode(),
                f'width="{width}" height="{height}"'.encode(),
            )
            for rectangle in (None, (width - 200, height - 200, width, height)):
                with self.subTest(size=(width, height), detail=rectangle is not None):
                    arguments = (
                        () if rectangle is None else ("--rectangle", ",".join(map(str, rectangle)))
                    )
                    completed = office_worker(
                        payload, "image", "--render-unit", "1", *self.arguments(), *arguments
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout)
                    self.assertEqual(completed.stderr, b"")
                    reading = ImageReading.decode(payload, json.loads(completed.stdout))
                    self.assertEqual(reading.inventory.units[0].size, (width, height))
                    self.assertEqual(reading.rectangle, rectangle)
                    assert reading.png is not None
                    with (
                        io.BytesIO(reading.png) as stream,
                        Image.open(stream) as image,
                        closing(image),
                    ):
                        self.assertEqual(image.size, overview if rectangle is None else (200, 200))
                        if rectangle is not None:
                            self.assertEqual(image.getpixel((10, 10)), (255, 255, 255, 255))
                            self.assertEqual(image.getpixel((100, 100)), (0, 255, 0, 255))


if __name__ == "__main__":
    unittest.main()
