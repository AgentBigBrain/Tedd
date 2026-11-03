"""Neutral source conversion cases; no provider access or live user material."""

from __future__ import annotations

import unittest

from tedd_knowledge_graph.source_contracts import SourceArchiveError, SourceLocation, encode


class DocumentTests(unittest.TestCase):
    def test_google_docs_rest_tabs_keep_nested_native_locations_and_exact_bytes(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_documents import read_document

        paragraph = {
            "startIndex": 1,
            "endIndex": 15,
            "paragraph": {"elements": [{"textRun": {"content": "Not approved.\n"}}]},
        }
        child = {
            "tabProperties": {"tabId": "child"},
            "documentTab": {"body": {"content": [paragraph]}, "footnotes": {}},
        }
        raw = encode(
            {
                "documentId": "selected",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "parent"},
                        "documentTab": {"body": {"content": []}, "footnotes": {}},
                        "childTabs": [child],
                    }
                ],
            }
        )
        reading = read_document(raw, "google_doc")
        self.assertEqual(reading.original_sha256, digest(raw))
        self.assertEqual([unit.text for unit in reading.units], ["Not approved.\n"])
        location = reading.units[0].location
        assert location is not None
        self.assertEqual(location.container, "child")

    def test_slides_distinguish_recovered_empty_and_dangling_notes(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        def shape(content: str) -> dict[str, object]:
            return {"shape": {"text": {"textElements": [{"textRun": {"content": content}}]}}}

        for case, elements, expected_text, expected_gaps in (
            (
                "recovered",
                [{"objectId": "notes", **shape("Only after review.")}],
                "Proposal.\nSpeaker notes:\nOnly after review.",
                (),
            ),
            ("empty", [{"objectId": "notes", **shape("")}], "Proposal.", ()),
            (
                "dangling",
                [{"objectId": "other", **shape("Unrelated.")}],
                "Proposal.",
                ("speaker_notes_unavailable",),
            ),
        ):
            with self.subTest(case=case):
                reading = read_document(
                    encode(
                        {
                            "slides": [
                                {
                                    "objectId": "slide",
                                    "pageElements": [shape("Proposal.")],
                                    "slideProperties": {
                                        "notesPage": {
                                            "notesProperties": {"speakerNotesObjectId": "notes"},
                                            "pageElements": elements,
                                        }
                                    },
                                }
                            ]
                        }
                    ),
                    "google_slide",
                )
                self.assertEqual(reading.units[0].text, expected_text)
                self.assertEqual(reading.gaps, expected_gaps)
                self.assertEqual(reading.reader_id, "tedd-google-slide:2")

    def test_text_families_keep_conditions_unicode_and_native_lines(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        raw = "Proposal — café.\r\nDo not deploy unless reviewed.\r\n".encode()
        for kind in ("text", "markdown", "code", "tool_result", "web"):
            with self.subTest(kind=kind):
                reading = read_document(raw, kind)
                self.assertEqual("".join(unit.text for unit in reading.units), raw.decode())
                self.assertEqual(reading.gaps, ())
                self.assertEqual(reading.units[0].location, SourceLocation("line", None, 1, 2))

    def test_large_text_units_fit_evidence_without_losing_lines(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        lines = [f"Line {number}: still not approved.\n" for number in range(1, 401)]
        reading = read_document("".join(lines).encode(), "code")
        self.assertGreater(len(reading.units), 1)
        self.assertTrue(all(len(unit.text) <= 6000 for unit in reading.units))
        self.assertEqual("".join(unit.text for unit in reading.units), "".join(lines))
        last = reading.units[-1].location
        assert last is not None
        self.assertEqual(last.end, 400)

    def test_copied_managed_note_is_not_a_new_primary_source(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        copied = b"Neutral copied note.\n<!-- codex-generated:v2:" + b"a" * 64 + b" -->\n"
        with self.assertRaises(SourceArchiveError):
            read_document(copied, "markdown")

    def test_unsafe_malformed_or_empty_text_does_not_become_evidence(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        for raw in (
            b"",
            b" \r\n",
            b"\xff",
            b"a\x00b",
            b"\x1b[31mhidden",
            b"password=very-secret-value",
        ):
            with self.subTest(raw=raw), self.assertRaises(SourceArchiveError):
                read_document(raw, "text")
        with self.assertRaises(SourceArchiveError):
            read_document(b"Neutral", "unknown")
        with self.assertRaises(SourceArchiveError):
            read_document(b"Neutral", "text", maximum_bytes=2)

    def test_saved_email_retains_attribution_reply_and_exact_body(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        raw = (
            b"From: Support <support@example.invalid>\r\n"
            b"To: Owner <owner@example.invalid>\r\n"
            b"Subject: Conditional approval\r\n"
            b"Message-ID: <reply-2@example.invalid>\r\n"
            b"In-Reply-To: <question-1@example.invalid>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Not approved until the review finishes.\r\n"
        )
        reading = read_document(raw, "email")
        self.assertEqual(reading.gaps, ())
        unit = reading.units[0]
        self.assertIn("Subject: Conditional approval", unit.text)
        self.assertIn("Not approved until the review finishes.\r\n", unit.text)
        self.assertEqual(unit.speaker, "Support <support@example.invalid>")
        self.assertEqual(unit.reply_to, "<question-1@example.invalid>")
        self.assertEqual(unit.source_id, "<reply-2@example.invalid>")
        self.assertEqual(
            unit.location, SourceLocation("message", "<reply-2@example.invalid>", 1, 1)
        )

    def test_email_does_not_silently_drop_or_execute_attachments(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        for case, raw, code in (
            (
                "malformed-header",
                b"Subject: Missing body\r\nBroken header\r\n\r\nNeutral",
                "source_corrupt",
            ),
            (
                "html",
                b"Content-Type: text/html\r\n\r\n<script>not safe</script>",
                "capability_unavailable",
            ),
            (
                "malformed-transfer-encoding",
                b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!",
                "source_corrupt",
            ),
            (
                "attachment-disposition",
                b"Content-Type: text/plain\r\nContent-Disposition: attachment; filename=note.txt\r\n\r\nNot a message body.",
                "capability_unavailable",
            ),
            (
                "inline-filename",
                b"Content-Type: text/plain\r\nContent-Disposition: inline; filename=note.txt\r\n\r\nInline attachment.",
                "capability_unavailable",
            ),
            (
                "content-type-name",
                b"Content-Type: text/plain; name=note.txt\r\n\r\nNamed attachment.",
                "capability_unavailable",
            ),
            (
                "multipart",
                b"Content-Type: multipart/mixed; boundary=test\r\n\r\n--test\r\nContent-Type: application/octet-stream\r\n\r\nopaque\r\n--test--",
                "capability_unavailable",
            ),
        ):
            with self.subTest(case=case):
                with self.assertRaises(SourceArchiveError) as caught:
                    read_document(raw, "email")
                self.assertEqual(caught.exception.code, code)

    def test_google_doc_uses_later_tab_and_marks_unresolved_suggestions(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        def paragraph(value: str, *, suggested: bool = False) -> dict[str, object]:
            element: dict[str, object] = {"textRun": {"content": value}}
            if suggested:
                element["suggestedInsertionIds"] = ["proposal"]
            return {
                "startIndex": 1,
                "endIndex": len(value) + 1,
                "paragraph": {"elements": [element]},
            }

        raw = encode(
            {
                "tabs": [
                    {
                        "tabId": "first",
                        "body": {"content": [paragraph("Still proposed.\n")]},
                        "footnotes": {},
                    },
                    {
                        "tabId": "later",
                        "parentTabId": "first",
                        "body": {"content": [paragraph("Only after review.\n", suggested=True)]},
                        "footnotes": {},
                    },
                ]
            }
        )
        reading = read_document(raw, "google_doc")
        self.assertEqual(
            [unit.text for unit in reading.units], ["Still proposed.\n", "Only after review.\n"]
        )
        self.assertEqual(reading.units[1].location, SourceLocation("utf16", "later", 2, 20))
        self.assertIn("unresolved_suggestions", reading.gaps)

    def test_google_sheet_preserves_native_formula_result_display_and_header(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        raw = b'{"sheets":[{"properties":{"sheetId":2,"title":"Costs","hidden":false},"data":[{"startRow":0,"startColumn":0,"rowData":[{"values":[{"formattedValue":"Item"},{"formattedValue":"Cost USD"}]},{"values":[{"userEnteredValue":{"stringValue":"Blue"},"formattedValue":"Blue"},{"userEnteredValue":{"formulaValue":"=7/2"},"effectiveValue":{"numberValue":3.5},"formattedValue":"$3.50","effectiveFormat":{"numberFormat":{"type":"CURRENCY","pattern":"$0.00"}}}]}]}]}]}'
        reading = read_document(raw, "google_sheet")
        row = reading.units[1]
        self.assertEqual(row.location, SourceLocation("cell", "2", 2, 2, 1, 2))
        for expected in ("Cost USD", "=7/2", "3.5", "$3.50", "CURRENCY"):
            self.assertIn(expected, row.text)
        self.assertEqual(reading.gaps, ())

    def test_nested_doc_tabs_and_run_suggestions_cannot_disappear(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        child = {
            "tabId": "child",
            "body": {
                "content": [
                    {
                        "startIndex": 0,
                        "endIndex": 4,
                        "paragraph": {
                            "elements": [
                                {
                                    "textRun": {
                                        "content": "No.\n",
                                        "suggestedTextStyleChanges": {"pending": {}},
                                    }
                                }
                            ]
                        },
                    }
                ]
            },
            "footnotes": {},
        }
        parent = {"tabId": "parent", "body": {"content": []}, "footnotes": {}, "childTabs": [child]}
        reading = read_document(encode({"tabs": [parent]}), "google_doc")
        self.assertEqual([unit.text for unit in reading.units], ["No.\n"])
        self.assertEqual(reading.units[0].location, SourceLocation("utf16", "child", 1, 4))
        self.assertIn("unresolved_suggestions", reading.gaps)

    def test_second_sheet_dates_filters_and_external_formula_keep_context(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        raw = encode(
            {
                "properties": {"locale": "en_GB", "timeZone": "Europe/London"},
                "sheets": [
                    {
                        "properties": {"sheetId": 0, "title": "Summary"},
                        "data": [{"rowData": [{"values": [{"formattedValue": "See conditions"}]}]}],
                    },
                    {
                        "properties": {"sheetId": 7, "title": "Conditions"},
                        "basicFilter": {"range": {}},
                        "data": [
                            {
                                "rowData": [
                                    {
                                        "values": [
                                            {"formattedValue": "Date"},
                                            {"formattedValue": "Amount GBP"},
                                        ]
                                    },
                                    {
                                        "values": [
                                            {
                                                "effectiveValue": {"numberValue": 45000},
                                                "formattedValue": "15/03/2023",
                                                "effectiveFormat": {
                                                    "numberFormat": {
                                                        "type": "DATE",
                                                        "pattern": "dd/mm/yyyy",
                                                    }
                                                },
                                            },
                                            {
                                                "userEnteredValue": {
                                                    "formulaValue": '=IMPORTRANGE("unavailable", "A1")'
                                                }
                                            },
                                        ]
                                    },
                                ]
                            }
                        ],
                    },
                ],
            }
        )
        reading = read_document(raw, "google_sheet")
        row = reading.units[2]
        self.assertEqual(row.location, SourceLocation("cell", "7", 2, 2, 1, 2))
        for context in (
            "Amount GBP",
            "en_GB",
            "Europe/London",
            "45000",
            "15/03/2023",
            "dd/mm/yyyy",
        ):
            self.assertIn(context, row.text)
        self.assertTrue(
            {
                "filter_view_state_requires_check",
                "formula_result_unavailable",
                "external_formula_dependency",
            }
            <= set(reading.gaps)
        )

    def test_external_sheet_formula_families_remain_explicit_gaps(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        for case, formula, external in (
            ("import-feed-cell-reference", "=IMPORTFEED(A1)", True),
            ("image-cell-reference", "=IMAGE(A1)", True),
            ("unknown-function-url", '=CUSTOMFETCH("https://example.invalid/data")', True),
            ("ordinary-formula", "=A1+B1", False),
        ):
            raw = encode(
                {
                    "sheets": [
                        {
                            "properties": {"sheetId": 0, "title": "Conditions"},
                            "data": [
                                {
                                    "rowData": [
                                        {
                                            "values": [
                                                {
                                                    "userEnteredValue": {"formulaValue": formula},
                                                    "formattedValue": "Unavailable",
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            )

            with self.subTest(case=case):
                reading = read_document(raw, "google_sheet")
                if external:
                    self.assertIn("external_formula_dependency", reading.gaps)
                else:
                    self.assertNotIn("external_formula_dependency", reading.gaps)
                self.assertEqual(reading.reader_id, "tedd-google-sheet:2")

    def test_diagram_reading_does_not_claim_visual_relationships_are_complete(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        shape = {
            "shape": {
                "shapeType": "RECTANGLE",
                "text": {"textElements": [{"textRun": {"content": "Only after approval"}}]},
            }
        }
        reading = read_document(
            encode(
                {
                    "slides": [
                        {
                            "objectId": "diagram",
                            "pageElements": [shape],
                            "slideProperties": {"notesPage": {}},
                        }
                    ]
                }
            ),
            "google_slide",
        )
        self.assertIn("visual_context_required", reading.gaps)

    def test_google_slides_preserve_native_order_notes_and_unavailable_chart_gap(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        def shape(value: str) -> dict[str, object]:
            return {"shape": {"text": {"textElements": [{"textRun": {"content": value}}]}}}

        raw = encode(
            {
                "slides": [
                    {
                        "objectId": "later-created-first",
                        "pageElements": [
                            shape("Not yet approved."),
                            {"sheetsChart": {"spreadsheetId": "not-fetched"}},
                        ],
                    },
                    {
                        "objectId": "earlier-created-second",
                        "pageElements": [shape("Conditional next step.")],
                        "slideProperties": {
                            "notesPage": {
                                "pageElements": [
                                    {"objectId": "notes", **shape("Only after review.")}
                                ],
                                "notesProperties": {"speakerNotesObjectId": "notes"},
                            }
                        },
                    },
                ]
            }
        )
        reading = read_document(raw, "google_slide")
        self.assertEqual(
            [unit.location for unit in reading.units],
            [
                SourceLocation("slide", "later-created-first", 1, 1),
                SourceLocation("slide", "earlier-created-second", 2, 2),
            ],
        )
        self.assertIn("Only after review.", reading.units[1].text)
        self.assertIn("linked_chart_unavailable", reading.gaps)

    def test_structured_readings_reject_duplicate_keys_and_hidden_secrets(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        valid = b'{"sheets":[{"properties":{"sheetId":0,"title":"Neutral"},"data":[{"rowData":[{"values":[{"formattedValue":"Not approved."}]}]}]}]}'
        self.assertIn("Not approved.", read_document(valid, "google_sheet").units[0].text)
        for case, raw, code in (
            (
                "duplicate-key",
                valid.replace(b'{"sheets":', b'{"sheets":[],"sheets":', 1),
                "source_corrupt",
            ),
            (
                "hidden-secret",
                valid[:-1] + b',"metadata":{"api_session":"hidden-secret"}}',
                "privacy_rejected",
            ),
            ("non-finite-number", valid[:-1] + b',"number":NaN}', "source_corrupt"),
        ):
            with self.subTest(case=case):
                with self.assertRaises(SourceArchiveError) as caught:
                    read_document(raw, "google_sheet")
                self.assertEqual(caught.exception.code, code)

    def test_format_specific_secret_locations_are_screened_before_reading(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        paragraph = {
            "startIndex": 0,
            "endIndex": 13,
            "paragraph": {"elements": [{"textRun": {"content": "Not approved."}}]},
        }
        cases = (
            (
                "document-metadata",
                "google_doc",
                encode(
                    {
                        "tabs": [
                            {
                                "tabId": "tab",
                                "body": {"content": [paragraph]},
                                "footnotes": {},
                            }
                        ],
                        "metadata": {"api_key": "not-a-real-secret"},
                    }
                ),
            ),
            (
                "sheet-formula",
                "google_sheet",
                encode(
                    {
                        "sheets": [
                            {
                                "properties": {"sheetId": 0, "title": "Conditions"},
                                "data": [
                                    {
                                        "rowData": [
                                            {
                                                "values": [
                                                    {
                                                        "userEnteredValue": {
                                                            "formulaValue": '=CONCAT("api_key='
                                                            'not-a-real-secret")'
                                                        },
                                                        "formattedValue": "Unavailable",
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ),
            ),
            (
                "slide-notes",
                "google_slide",
                encode(
                    {
                        "slides": [
                            {
                                "objectId": "slide",
                                "pageElements": [],
                                "slideProperties": {
                                    "notesPage": {
                                        "notesProperties": {"speakerNotesObjectId": "notes"},
                                        "pageElements": [
                                            {
                                                "objectId": "notes",
                                                "shape": {
                                                    "text": {
                                                        "textElements": [
                                                            {
                                                                "textRun": {
                                                                    "content": (
                                                                        "password=not-a-real-secret"
                                                                    )
                                                                }
                                                            }
                                                        ]
                                                    }
                                                },
                                            }
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                ),
            ),
            (
                "email-header",
                "email",
                b"Content-Type: text/plain\r\n"
                b"X-Context: api_key=not-a-real-secret\r\n\r\nNot approved.\r\n",
            ),
        )
        for case, kind, raw in cases:
            with self.subTest(case=case):
                with self.assertRaises(SourceArchiveError) as caught:
                    read_document(raw, kind)
                self.assertEqual(caught.exception.code, "privacy_rejected")

    def test_structured_and_packaged_resource_bounds_fail_closed(self) -> None:
        import io
        import zipfile

        from tedd_knowledge_graph.source_documents import read_document
        from tedd_knowledge_graph.source_office import read_office

        deep_json = b'{"tabs":' + b"[" * 21 + b"0" + b"]" * 21 + b"}"
        with self.assertRaises(SourceArchiveError) as caught:
            read_document(deep_json, "google_doc")
        self.assertEqual(caught.exception.code, "limit_exceeded")

        def package(name: str, payload: bytes) -> bytes:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(name, payload)
            return stream.getvalue()

        nested_xml = ("<x>" * 65 + "Not approved." + "</x>" * 65).encode()
        expanded = bytearray(package("word/document.xml", b"<x/>"))
        directory = expanded.index(b"PK\x01\x02")
        expanded[directory + 24 : directory + 28] = (32 * 1024 * 1024 + 1).to_bytes(4, "little")
        cases = (
            ("deep-xml", package("word/document.xml", nested_xml), "limit_exceeded"),
            (
                "alternate-encoding",
                package("word/document.xml", "<x>Not approved.</x>".encode("utf-16")),
                "source_corrupt",
            ),
            ("active-content", package("word/vbaProject.bin", b"macro"), "capability_unavailable"),
            ("expanded-member", bytes(expanded), "limit_exceeded"),
        )
        for case, raw, code in cases:
            with self.subTest(case=case):
                with self.assertRaises(SourceArchiveError) as caught:
                    read_office(raw, "docx")
                self.assertEqual(caught.exception.code, code)

    def test_code_and_markup_are_read_as_inert_source_text(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        for kind, raw in (
            ("code", b'raise RuntimeError("must remain source text")\n'),
            ("markdown", b'<script src="https://example.invalid/inert.js"></script>\n'),
        ):
            with self.subTest(kind=kind):
                reading = read_document(raw, kind)
                self.assertEqual(reading.units[0].text, raw.decode())

    def test_provider_cell_types_are_not_invented_from_malformed_values(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        for entry in (
            {"numberValue": "not a number"},
            {"boolValue": "false"},
            {"stringValue": {"unexpected": "object"}},
        ):
            raw = encode(
                {
                    "sheets": [
                        {
                            "properties": {"sheetId": 0, "title": "Neutral"},
                            "data": [{"rowData": [{"values": [{"userEnteredValue": entry}]}]}],
                        }
                    ]
                }
            )
            with self.subTest(entry=entry), self.assertRaises(SourceArchiveError):
                read_document(raw, "google_sheet")

    def test_slide_text_runs_are_contiguous_and_duplicate_ids_are_rejected(self) -> None:
        from tedd_knowledge_graph.source_documents import read_document

        slide = {
            "objectId": "slide-one",
            "pageElements": [
                {
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "non"}},
                                {"textRun": {"content": "compliant"}},
                            ]
                        }
                    }
                }
            ],
        }
        reading = read_document(encode({"slides": [slide]}), "google_slide")
        self.assertEqual(reading.units[0].text, "noncompliant")
        with self.assertRaises(SourceArchiveError):
            read_document(encode({"slides": [slide, slide]}), "google_slide")


if __name__ == "__main__":
    unittest.main()
