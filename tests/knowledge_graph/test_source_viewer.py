"""The private viewer receives inert, selected evidence; never model output."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import unittest
import zlib
from dataclasses import replace

from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceExcerpt,
    SourceLocation,
    SourceRef,
    SourceUnit,
    encode,
)
from tedd_knowledge_graph.source_viewer import decode_view, image_packet, text_packet

from tests.support.paths import GRAPH_SCRIPTS


class SourceViewerTests(unittest.TestCase):
    def test_owner_image_review_binds_exact_rendering_without_claiming_saved_evidence(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_review import ReviewRendering
        from tedd_knowledge_graph.source_viewer import image_review_packet

        png = self.pixel()
        rendering = ReviewRendering(
            "a" * 64, "b" * 64, "c" * 64, "u1", "owner", digest(png), (1, 1), (1, 1)
        )
        packet = image_review_packet(rendering, png)
        value = decode_view(packet)
        self.assertEqual(value["kind"], "image_confirmation")
        self.assertIn("not yet preserved", value["label"])
        self.assertIn("overview", value["label"])
        saved = decode_view(image_review_packet(rendering, png, saved=True))
        self.assertEqual(saved["kind"], "png")
        self.assertIn("Saved exact version", saved["label"])
        self.assertNotIn("not yet preserved", saved["label"])
        for shown in (value, saved):
            self.assertIn("8-bit", shown["label"])
            self.assertIn("HDR", shown["label"])
            self.assertIn("still frames", shown["label"])
        for invalid in (
            replace(rendering, audience="model"),
            replace(rendering, png_sha256="d" * 64),
            replace(rendering, original_size=(2, 2), output_size=(2, 2)),
        ):
            with self.assertRaises(SourceArchiveError):
                image_review_packet(invalid, png)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_pre_save_review_requires_own_acceptance_button_and_visible_pixels(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_review import ReviewRendering
        from tedd_knowledge_graph.source_viewer import image_review_packet

        png = self.pixel()
        rendering = ReviewRendering(
            "a" * 64, "b" * 64, "c" * 64, "u1", "owner", digest(png), (1, 1), (1, 1)
        )
        for accept in (False, True):
            assertion = """canvas = next(child for child in self.winfo_children() if isinstance(child, tk.Canvas))
            image = canvas.itemcget(canvas.find_all()[0], 'image')
            buttons = [child for child in self.winfo_children() if isinstance(child, tk.Button)]
            verified.append(self.tk.call(image, 'get', 0, 0) == (255, 0, 0) and 'I reviewed this image' in [button.cget('text') for button in buttons] and self.winfo_viewable())"""
            if accept:
                assertion += "\n            next(button for button in buttons if button.cget('text') == 'I reviewed this image').invoke()"
            with self.subTest(accept=accept):
                self._native_worker(
                    image_review_packet(rendering, png),
                    assertion,
                    close=not accept,
                    expected_reply=b"accepted\n" if accept else b"declined\n",
                )

    def test_google_disconnect_and_recovery_have_distinct_confirmation_packets(self) -> None:
        for kind in ("disconnect_confirmation", "reconnect_confirmation"):
            with self.subTest(kind=kind):
                value = decode_view(
                    encode({"kind": kind, "body": "Synthetic warning", "label": "Owner review"})
                )
                self.assertEqual(value["kind"], kind)

    def setUp(self) -> None:
        self.ref = SourceRef("a" * 32, "b" * 64, "c" * 64, "u1")
        self.excerpt = SourceExcerpt(
            self.ref,
            (
                SourceUnit(
                    "u1",
                    "Not approved — review first.",
                    1,
                    "line",
                    location=SourceLocation("line", None, 1, 1),
                ),
            ),
            1,
            (),
            0,
            "partial",
            "screened",
            "d" * 64,
            title="Sensitive document name",
            source_version="version-7",
        )

    def test_only_selected_text_and_honest_coverage_reach_the_viewer(self) -> None:
        raw = text_packet(self.excerpt)
        value = decode_view(raw)
        self.assertEqual(value["body"], "Not approved — review first.")
        self.assertIn("partial", value["label"])
        self.assertNotIn(b"Sensitive document name", raw)
        self.assertNotIn(self.ref.as_uri().encode(), raw)

    def test_multiple_units_or_unverified_evidence_are_not_silently_displayed(self) -> None:
        for excerpt in (
            replace(self.excerpt, units=()),
            replace(self.excerpt, units=self.excerpt.units * 2),
            replace(self.excerpt, integrity="unknown"),
        ):
            with self.assertRaises(SourceArchiveError):
                text_packet(excerpt)

    def test_last_unit_discloses_preceding_context_not_shown(self) -> None:
        value = decode_view(text_packet(replace(self.excerpt, previous_units=3, remaining_units=0)))
        self.assertIn("Other units: 3", value["label"])
        self.assertEqual(value["body"], "Not approved — review first.")

    def test_markup_is_inert_text_not_browser_navigation(self) -> None:
        hostile = "<script>fetch('https://example.com')</script> ![](https://example.com/pixel)"
        value = decode_view(
            text_packet(
                replace(self.excerpt, units=(replace(self.excerpt.units[0], text=hostile),))
            )
        )
        self.assertEqual(value["kind"], "text")
        self.assertEqual(value["body"], hostile)

    def test_unknown_fields_malformed_or_oversize_payloads_are_refused(self) -> None:
        for payload in (
            b"not JSON",
            b"x" * 131073,
            json.dumps({"kind": "html", "body": "hello", "label": "test"}).encode(),
            b'{"kind":"text","kind":"text","body":"hello","label":"test"}',
        ):
            with self.subTest(length=len(payload)), self.assertRaises(SourceArchiveError):
                decode_view(payload)

    @staticmethod
    def pixel() -> bytes:
        def chunk(kind: bytes, body: bytes) -> bytes:
            return (
                struct.pack("!I", len(body))
                + kind
                + body
                + struct.pack("!I", zlib.crc32(kind + body))
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
            + chunk(b"IEND", b"")
        )

    def test_image_handoff_refuses_non_png_and_excessive_dimensions(self) -> None:
        raw = self.pixel()
        packet = decode_view(image_packet(self.ref, raw))
        self.assertEqual(packet["kind"], "png")
        self.assertIn("Selected saved image", packet["label"])
        self.assertIn("not the complete document", packet["label"])
        for invalid in (b"<svg></svg>", raw[:16] + struct.pack("!II", 9000, 9000) + raw[24:]):
            with self.assertRaises(SourceArchiveError):
                image_packet(self.ref, invalid)

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_worker_displays_literal_selected_text_and_closes(self) -> None:
        self._native_worker(
            text_packet(self.excerpt),
            "body = [child for child in self.winfo_children() if isinstance(child, tk.Text)][0]\n            verified.append(body.get('1.0', 'end-1c') == 'Not approved — review first.' and body.cget('state') == 'disabled' and self.winfo_viewable())",
        )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_worker_displays_selected_png_without_file_cache(self) -> None:
        self._native_worker(
            image_packet(self.ref, self.pixel()),
            "canvas = [child for child in self.winfo_children() if isinstance(child, tk.Canvas)][0]\n            image = canvas.itemcget(canvas.find_all()[0], 'image')\n            verified.append(self.tk.call(image, 'get', 0, 0) == (255, 0, 0) and self.winfo_viewable())",
        )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_long_export_review_has_a_working_visible_scrollbar(self) -> None:
        self._native_worker(
            encode(
                {
                    "kind": "export_confirmation",
                    "body": "An ordinary export to review.\n" * 80
                    + "Last export: review this too.",
                    "label": "Retain ordinary exports",
                }
            ),
            """body = next(child for child in self.winfo_children() if isinstance(child, tk.Text))
            self.update_idletasks()
            bars = [child for child in self.winfo_children() if isinstance(child, tk.Scrollbar) and child.winfo_viewable()]
            if bars:
                self.tk.call(bars[0].cget('command'), 'moveto', 0.0)
                self.update_idletasks()
            before = body.yview()
            if bars:
                self.tk.call(bars[0].cget('command'), 'moveto', 1.0)
                self.update_idletasks()
            after = body.yview()
            verified.append(bool(bars) and before[1] < 1.0 and after[0] > before[0] and after[1] == 1.0 and body.cget('state') == 'disabled' and body.get('end-2l', 'end-1c').endswith('Last export: review this too.'))""",
            expected_reply=b"declined\n",
        )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_text_copy_never_reaches_the_system_clipboard(self) -> None:
        self._native_worker(
            text_packet(self.excerpt),
            """body = [child for child in self.winfo_children() if isinstance(child, tk.Text)][0]
            calls = []
            self.tk.call('rename', 'clipboard', 'original_clipboard')
            self.tk.createcommand('clipboard', lambda *args: calls.append(args) or '')
            body.tag_add('sel', '1.0', 'end')
            body.event_generate('<<Copy>>')
            verified.append(calls == [])""",
        )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_permission_review_requires_continue_and_closing_refuses(self) -> None:
        from tedd_knowledge_graph.source_google_oauth import CONSENT_DESCRIPTION

        payload = encode(
            {"kind": "confirmation", "body": CONSENT_DESCRIPTION, "label": "Permission review"}
        )
        for accept in (False, True):
            assertion = """body = [child for child in self.winfo_children() if isinstance(child, tk.Text)][0]
            verified.append('permanently delete' in body.get('1.0', 'end') and self.winfo_viewable())"""
            if accept:
                assertion += "\n            next(child for child in self.winfo_children() if isinstance(child, tk.Button) and child.cget('text') == 'Continue to Google').invoke()"
            with self.subTest(accept=accept):
                self._native_worker(
                    payload,
                    assertion,
                    close=not accept,
                    expected_reply=b"accepted\n" if accept else b"declined\n",
                )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_export_confirmation_is_distinct_from_google_consent(self) -> None:
        payload = encode(
            {
                "kind": "export_confirmation",
                "body": "Owner-confirmed, not provider-verified.",
                "label": "Retain ordinary exports",
            }
        )
        for accept in (False, True):
            assertion = """buttons = [child for child in self.winfo_children() if isinstance(child, tk.Button)]
            verified.append('Preserve these exports' in [button.cget('text') for button in buttons] and 'Continue to Google' not in [button.cget('text') for button in buttons] and self.winfo_viewable())"""
            if accept:
                assertion += "\n            next(button for button in buttons if button.cget('text') == 'Preserve these exports').invoke()"
            with self.subTest(accept=accept):
                self._native_worker(
                    payload,
                    assertion,
                    close=not accept,
                    expected_reply=b"accepted\n" if accept else b"declined\n",
                )

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_permission_review_cancellation_closes_its_exact_window(self) -> None:
        from tedd_knowledge_graph.source_google_oauth import CONSENT_DESCRIPTION
        from tedd_knowledge_graph.source_viewer import PrivateViewer

        viewer = PrivateViewer(confirmation=True)
        cancelled = threading.Event()
        timer = threading.Timer(0.5, cancelled.set)
        timer.start()
        try:
            self.assertFalse(viewer.confirm(CONSENT_DESCRIPTION, cancelled))
            self.assertIsNotNone(viewer._process.poll())
        finally:
            timer.cancel()
            timer.join(3)
            viewer.close()

    @unittest.skipUnless(
        os.environ.get("TEDD_TEST_NATIVE_VIEWER") == "1", "explicit native display check"
    )
    def test_native_google_controls_display_exact_actions_and_closing_is_not_consent(self) -> None:
        for kind, label in (
            ("disconnect_confirmation", "Disconnect Google"),
            ("reconnect_confirmation", "Prepare a fresh sign-in"),
        ):
            for accept in (False, True):
                with self.subTest(kind=kind, accept=accept):
                    assertion = f"""buttons = [child for child in self.winfo_children() if isinstance(child, tk.Button)]
            verified.append({label!r} in [button.cget('text') for button in buttons] and 'Continue to Google' not in [button.cget('text') for button in buttons] and self.winfo_viewable())"""
                    if accept:
                        assertion += f"\n            next(button for button in buttons if button.cget('text') == {label!r}).invoke()"
                    self._native_worker(
                        encode(
                            {
                                "kind": kind,
                                "body": "Synthetic Google control warning.",
                                "label": "Owner review",
                            }
                        ),
                        assertion,
                        close=not accept,
                        expected_reply=b"accepted\n" if accept else b"declined\n",
                    )

    def _native_worker(
        self, payload: bytes, assertion: str, *, close: bool = True, expected_reply: bytes = b""
    ) -> None:
        # Instrument only the neutral fixture's window. Rendering, process limits,
        # packet decoding and the two-phase handoff are the actual worker code.
        code = """
import sys, tkinter as tk
sys.path.insert(0, sys.argv[1])
from tedd_knowledge_graph.source_viewer import worker
original = tk.Tk
verified = []
class CheckedTk(original):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        def check():
            ASSERTION
            CLOSE
        self.after(250, check)
tk.Tk = CheckedTk
status = worker()
if status != 0 or verified != [True]: raise SystemExit(2)
"""
        code = code.replace("ASSERTION", assertion).replace(
            "CLOSE", "self.destroy()" if close else "pass"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(GRAPH_SCRIPTS)],
            input=struct.pack("!I", len(payload)) + payload + b"1",
            capture_output=True,
            timeout=10,
            env={
                key: os.environ[key]
                for key in ("SystemRoot", "WINDIR", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
                if key in os.environ
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"ready\nstaged\ndisplayed\n" + expected_reply)
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
