"""A small inert owner-only text viewer: anonymous pipes, no web server or files.

The parent starts the blank worker before taking archive locks, then makes one
bounded authorized handoff. The worker never writes evidence to stdout, logs,
clipboard, a vault or a cache. This is a same-user desktop boundary, not protection
against an administrator, screen capture or another unrestricted same-user process.
"""

from __future__ import annotations

import base64
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tedd_knowledge_graph.source_contracts import (
    SourceArchiveError,
    SourceExcerpt,
    SourceRef,
    digest,
    encode,
    object_fields,
    parse_json,
)
from tedd_knowledge_graph.source_review import ReviewRendering

MAX_PACKET = 12 * 1024**2
MAX_IMAGE = 8 * 1024**2
_CONFIRMATIONS = {
    "confirmation": ("Tedd — Google source access", "Continue to Google"),
    "export_confirmation": ("Tedd — preserve exports", "Preserve these exports"),
    "disconnect_confirmation": ("Tedd — disconnect Google", "Disconnect Google"),
    "reconnect_confirmation": ("Tedd — Google recovery", "Prepare a fresh sign-in"),
    "image_confirmation": ("Tedd — review before saving", "I reviewed this image"),
}
_IMAGE_KINDS = frozenset({"png", "image_confirmation"})
_IMAGE_DISPLAY_LIMITS = (
    "8-bit preview — not a calibrated color or HDR display. "
    "Animations appear as still frames; auxiliary images are not calibrated measurements."
)


def decode_view(payload: bytes) -> dict[str, str]:
    value = object_fields(parse_json(payload, maximum_bytes=MAX_PACKET), {"kind", "body", "label"})
    if (
        value["kind"] not in ("text", "png", *_CONFIRMATIONS)
        or not isinstance(value["body"], str)
        or not isinstance(value["label"], str)
        or (value["kind"] not in _IMAGE_KINDS and len(value["body"]) > 24000)
        or len(value["label"]) > 5000
    ):
        raise SourceArchiveError("scope_denied")
    if value["kind"] in _IMAGE_KINDS:
        try:
            png = base64.b64decode(value["body"], validate=True)
        except ValueError:
            raise SourceArchiveError("scope_denied") from None
        if (
            len(png) > MAX_IMAGE
            or not png.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            or len(png) < 33
        ):
            raise SourceArchiveError("scope_denied")
        width, height = struct.unpack("!II", png[16:24])
        if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 16_000_000:
            raise SourceArchiveError("limit_exceeded")
    return cast(dict[str, str], value)


def text_packet(excerpt: SourceExcerpt) -> bytes:
    if len(excerpt.units) != 1 or excerpt.integrity != "verified":
        raise SourceArchiveError("scope_denied")
    other_units = excerpt.previous_units + excerpt.remaining_units
    payload: bytes = encode(
        {
            "kind": "text",
            "body": excerpt.units[0].text,
            "label": f"Saved version: {excerpt.source_version} | Coverage: {excerpt.coverage} | Other units: {other_units}\nSelected evidence only — not the complete document.",
        }
    )
    decode_view(payload)
    return payload


def image_packet(_ref: SourceRef, png: bytes) -> bytes:
    if len(png) > MAX_IMAGE:
        raise SourceArchiveError("limit_exceeded")
    payload: bytes = encode(
        {
            "kind": "png",
            "body": base64.b64encode(png).decode("ascii"),
            "label": "Selected saved image or source page — rendered derivative, not the complete document. Office pagination and layout may differ from the original application. "
            + _IMAGE_DISPLAY_LIMITS,
        }
    )
    decode_view(payload)
    return payload


def image_review_packet(rendering: ReviewRendering, png: bytes, *, saved: bool = False) -> bytes:
    """Exact owner-only pre-publication rendering, not a fabricated saved reference."""
    if (
        rendering.audience != "owner"
        or digest(png) != rendering.png_sha256
        or len(png) < 24
        or struct.unpack("!II", png[16:24]) != rendering.output_size
    ):
        raise SourceArchiveError("scope_denied")
    coverage = (
        f"overview {rendering.output_size} of oriented image {rendering.oriented_size}"
        if rendering.rectangle is None
        else f"native detail {rendering.rectangle} — zero-based, right/bottom excluded"
    )
    payload = encode(
        {
            "kind": "png" if saved else "image_confirmation",
            "body": base64.b64encode(png).decode("ascii"),
            "label": f"Image {rendering.unit_id}: {coverage}. "
            + (
                "Saved exact version — rendered derivative. An overview does not establish readable small text."
                if saved
                else "Original not yet preserved. Accept only the area you have reviewed; an overview does not establish readable small text. Cancel if the source needs another detail check or should not be kept."
            )
            + " "
            + _IMAGE_DISPLAY_LIMITS,
        }
    )
    decode_view(payload)
    return payload


class PrivateViewer:
    """One explicitly requested display; constructing it is not host qualification."""

    def __init__(self, *, confirmation: bool = False) -> None:
        # In particular, do not inherit PYTHON*, TCL*, TK*, credentials or a shell.
        env = {
            key: os.environ[key]
            for key in (
                "SystemRoot",
                "WINDIR",
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
            )
            if key in os.environ
        }
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=Path(sys.executable).parent,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._messages: queue.Queue[bytes] = queue.Queue()
        self._displayed = False
        self._confirmation = confirmation

        def receive() -> None:
            assert self._process.stdout is not None
            for _ in range(4 if confirmation else 3):
                line = self._process.stdout.readline(32)
                self._messages.put(line)
                if not line:
                    break

        threading.Thread(target=receive, daemon=True).start()
        try:
            self._expect(b"ready\n", 5)
        except BaseException:
            self.close()
            raise

    def _expect(self, expected: bytes, timeout: float = 2) -> None:
        try:
            result = self._messages.get(timeout=timeout)
        except queue.Empty:
            raise SourceArchiveError("capability_unavailable") from None
        if result != expected:
            raise SourceArchiveError("capability_unavailable")

    def display(self, excerpt: SourceExcerpt) -> None:
        self._display(text_packet(excerpt))

    def display_image(self, ref: SourceRef, png: bytes) -> None:
        self._display(image_packet(ref, png))

    def display_rendering(self, rendering: ReviewRendering, png: bytes) -> None:
        self._display(image_review_packet(rendering, png, saved=True))

    def review_image(
        self,
        rendering: ReviewRendering,
        png: bytes,
        cancelled: threading.Event,
        *,
        handoff: AbstractContextManager[None] | None = None,
    ) -> bool:
        return self._confirm(image_review_packet(rendering, png), cancelled, handoff=handoff)

    def confirm(
        self,
        description: str,
        cancelled: threading.Event,
        *,
        exports: bool = False,
        action: str | None = None,
    ) -> bool:
        """Require an explicit human click; closing, timeout or cancellation refuses."""
        if not self._confirmation or cancelled.is_set():
            raise SourceArchiveError("scope_denied")
        if action not in (None, "disconnect", "reconnect") or (exports and action is not None):
            raise SourceArchiveError("scope_denied")
        kind = (
            action + "_confirmation"
            if action
            else "export_confirmation"
            if exports
            else "confirmation"
        )
        return self._confirm(
            encode(
                {
                    "kind": kind,
                    "body": description,
                    "label": _CONFIRMATIONS[kind][0] + " — owner review"
                    if action
                    else "Retain ordinary exports — owner review"
                    if exports
                    else "Google source access — permission review",
                }
            ),
            cancelled,
        )

    def _confirm(
        self,
        payload: bytes,
        cancelled: threading.Event,
        *,
        handoff: AbstractContextManager[None] | None = None,
    ) -> bool:
        if not self._confirmation or cancelled.is_set():
            raise SourceArchiveError("scope_denied")
        try:
            with handoff if handoff is not None else nullcontext():
                if cancelled.is_set():
                    raise SourceArchiveError("scope_denied")
                self._display(payload)
            deadline = time.monotonic() + 300
            while not cancelled.is_set() and time.monotonic() < deadline:
                try:
                    return (
                        self._messages.get(timeout=0.25) == b"accepted\n" and not cancelled.is_set()
                    )
                except queue.Empty:
                    continue
            return False
        finally:
            self._displayed = False
            self.close()

    def _display(self, payload: bytes) -> None:
        if self._displayed:
            raise SourceArchiveError("scope_denied")
        assert self._process.stdin is not None

        def send() -> None:
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(struct.pack("!I", len(payload)) + payload)
                self._process.stdin.flush()
            except (OSError, ValueError):
                self._messages.put(b"failed\n")

        threading.Thread(target=send, daemon=True).start()
        try:
            self._expect(b"staged\n")
            self._process.stdin.write(b"1")
            self._process.stdin.flush()
            self._expect(b"displayed\n")
        except BaseException:
            self.close()
            raise
        self._displayed = True

    def close(self) -> None:
        if not self._displayed and self._process.poll() is None:
            self._process.terminate()
            self._process.wait(timeout=5)
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                stream.close()
        if self._displayed:
            # The human controls window lifetime. No wait for input under locks.
            threading.Thread(target=self._process.wait, daemon=True).start()


def worker() -> int:
    """Tk receives literal strings, never HTML, Tcl scripts or resource URLs."""
    import tkinter as tk

    from tedd_knowledge_graph.source_pdf_host import limit_worker

    job = limit_worker(512 * 1024**2, 30)
    root = tk.Tk(className="TeddEvidence")
    root.withdraw()
    root.title("Tedd — saved evidence")
    sys.stdout.buffer.write(b"ready\n")
    sys.stdout.buffer.flush()
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        root.destroy()
        return 1
    length = struct.unpack("!I", header)[0]
    if not 0 < length <= MAX_PACKET:
        root.destroy()
        return 1
    value = decode_view(sys.stdin.buffer.read(length))
    confirmation = value["kind"] in _CONFIRMATIONS
    accepted = False
    if confirmation:
        root.title(_CONFIRMATIONS[value["kind"]][0])
    tk.Label(root, text=value["label"], justify="left", wraplength=760, padx=16, pady=12).pack(
        fill="x"
    )
    if value["kind"] not in _IMAGE_KINDS:
        body = tk.Text(
            root, wrap="word", width=90, height=24, padx=16, pady=12, exportselection=False
        )
        body.insert("1.0", value["body"])
        body.configure(state="disabled")
        for event in ("<<Copy>>", "<<Cut>>", "<<Paste>>"):
            body.bind(event, lambda _event: "break")
        vertical = tk.Scrollbar(root, orient="vertical", command=body.yview)
        body.configure(yscrollcommand=vertical.set)
        vertical.pack(side="right", fill="y")
        body.pack(fill="both", expand=True)
    else:
        photo = tk.PhotoImage(data=value["body"], format="png")
        canvas = tk.Canvas(root, width=min(photo.width(), 960), height=min(photo.height(), 640))
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.configure(scrollregion=(0, 0, photo.width(), photo.height()))
        vertical = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
        horizontal = tk.Scrollbar(root, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        canvas.pack(fill="both", expand=True)
    if confirmation:

        def accept() -> None:
            nonlocal accepted
            accepted = True
            root.destroy()

        tk.Button(root, text=_CONFIRMATIONS[value["kind"]][1], command=accept).pack(pady=8)
    tk.Button(root, text="Cancel" if confirmation else "Close evidence", command=root.destroy).pack(
        pady=12
    )
    root.bind("<Escape>", lambda _event: root.destroy())
    sys.stdout.buffer.write(b"staged\n")
    sys.stdout.buffer.flush()
    if sys.stdin.buffer.read(1) != b"1":
        root.destroy()
        return 1
    root.deiconify()
    root.update()
    sys.stdout.buffer.write(b"displayed\n")
    sys.stdout.buffer.flush()
    root.mainloop()
    if confirmation:
        sys.stdout.buffer.write(b"accepted\n" if accepted else b"declined\n")
        sys.stdout.buffer.flush()
    del job
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(worker() if sys.argv[1:] == ["--worker"] else 1)
    except Exception:
        # Never serialize a Tk exception containing an input value.
        raise SystemExit(1) from None
