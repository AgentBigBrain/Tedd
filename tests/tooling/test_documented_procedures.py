"""Run the examples a reader follows against private, disposable graph and source stores."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from string import Template
from typing import Any
from unittest.mock import patch

import knowledge_graph as graph
import knowledge_graph_v4 as v4
import tedd_sources
from platform_support import set_private_path_mode
from tedd_knowledge_graph.v4_application_service import (
    CURRENT_STATE_BUDGET_DIVISOR,
    CURRENT_STATE_CHARS,
)

from tests.knowledge_graph.outcome_support import OutcomeFixture
from tests.support.graph_cases import FIXED_TIME, V4TestCase, applied, change, make_v3_graph

ROOT = Path(__file__).resolve().parents[2]
REF = ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/references"
CAPTURE = REF / "capture-and-correction.md"
RECOVERY = REF / "setup-and-recovery.md"
PROTOCOL = REF / "multi-agent-protocol.md"
ARCHIVE = ROOT / "docs/sources/setup.md"
LOCAL = ROOT / "docs/sources/local-example.md"


def example(document: Path, marker: str, language: str = "bash") -> str:
    text = document.read_text(encoding="utf-8")
    label = f"<!-- procedure: {marker} -->"
    if text.count(label) != 1:
        raise AssertionError(f"Expected one {label} in {document.name}")
    fence = re.match(r"\s*```" + language + r"\n(.*?)^```$", text.split(label)[1], re.M | re.S)
    if fence is None:
        raise AssertionError(f"Missing {language} example after {label}")
    return fence[1]


class DocumentedProcedureTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        graph_dir = self.root / "graph"
        graph_dir.mkdir(mode=0o700)
        set_private_path_mode(graph_dir, 0o700)
        self.config = graph_dir / "config.json"
        self.store_path = graph_dir / "graph.sqlite3"
        self.values = {
            "graph_config": str(self.config),
            "consumer_id": "consumer_alpha",
            "agent_id": "agent_alpha",
            "task_id": "task_alpha",
            "run_id": "run_alpha",
            "subject_id": "entity_project",
            "subscription_id": "scope_alpha",
            "archive_root": str(self.root / "archive"),
            "packet": str(self.root / "packet.json"),
            "plan_file": str(self.root / "signed plan.json"),
        }

    def graph_cli(self, *args: str, expected: int = 0) -> dict[str, Any]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["knowledge_graph.py", "--config", str(self.config), *args]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            result = graph.main()
        self.assertEqual(result, expected, err.getvalue() or out.getvalue())
        return json.loads((err if expected else out).getvalue())

    def documented(
        self,
        block: str,
        verb: str,
        *,
        expected: int = 0,
        capture_args: tuple[str, ...] = (),
        default_config: bool = False,
    ) -> dict[str, Any]:
        # Run one documented Python invocation as argv. Shell assignments and substitutions
        # are supplied by the fixture, and no shell gets to interpret file paths.
        calls = (
            shlex.split(line)
            for line in block.replace("\\\n", "").splitlines()
            if line.startswith("python3 -B scripts/")
        )
        # Normal task commands use the host default. Rehearsals name their configuration.
        # Match the documented form before binding either route to the disposable store.
        prefix = [] if default_config else ["--config", "$graph_config"]
        start = 3 + len(prefix)
        (call,) = [words for words in calls if words[3 : start + 1] == [*prefix, verb]]
        args = []
        for word in call[start:]:
            if word == "${capture_args[@]}":
                args.extend(capture_args)
            else:
                args.append(Template(word).substitute(self.values))
        self.assertFalse(any(arg.partition("=")[0] in {"--config", "--store"} for arg in args))
        if call[2] == "scripts/knowledge_graph.py":
            return self.graph_cli(*args, expected=expected)
        self.assertEqual(call[2], "scripts/tedd_sources.py")
        with io.TextIOWrapper(io.BytesIO(), encoding="utf-8") as out, redirect_stdout(out):
            code = tedd_sources.main(["--config", str(self.config), *args])
            out.seek(0)
            result = json.load(out)
        self.assertEqual(code, expected, result)
        return result

    def configure_store(self, store: Path) -> None:
        vault = self.root / "vault"
        vault.mkdir(mode=0o700)
        set_private_path_mode(vault, 0o700)
        self.graph_cli("configure", "--vault", str(vault), "--store", str(store))
        configured = Path(json.loads(self.config.read_text(encoding="utf-8"))["store_path"])
        self.assertTrue(configured.is_relative_to(self.root))
        self.assertEqual(configured, store)

    def initialize(self, *, scoped: bool = True) -> None:
        self.configure_store(self.store_path)
        self.graph_cli("init-v4")
        with v4.open_store(self.store_path) as store:
            store.register_consumer("consumer_alpha", agent_id="agent_alpha", task_id="task_alpha")
            if scoped:
                applied(store, [change("entity_project")], fingerprint="subject")
                store.subscribe(
                    "consumer_alpha",
                    {"entity_ids": ["entity_project"]},
                    subscription_id="scope_alpha",
                )

    def capture_inputs(self) -> tuple[str, tuple[str, ...]]:
        block = example(CAPTURE, "preview-save")
        array = re.search(r"^capture_args=\(\n(.*?)^\)$", block, re.M | re.S)
        assert array is not None, "The preview must declare the shared capture arguments"
        args = tuple(Template(word).substitute(self.values) for word in shlex.split(array[1]))
        return block, args

    def test_reviewed_sample_packet_saves_and_reads_back_each_record(self) -> None:
        self.initialize(scoped=False)
        packet = example(REF / "capture-schema.md", "restore-packet", "json")
        specimen = json.loads(packet)
        graph.atomic_write(Path(self.values["packet"]), packet)
        self.values["watermark"] = str(self.graph_cli("doctor")["graph"]["watermark"])
        block, args = self.capture_inputs()
        preview = self.documented(block, "capture", capture_args=args)
        self.assertEqual(preview["status"], "would_capture")
        self.assertTrue(Path(self.values["plan_file"]).is_file())
        self.values["fingerprint"] = preview["fingerprint"]
        apply = example(CAPTURE, "apply-save")
        plan_file = self.values["plan_file"]
        self.values["plan_file"] = str(self.root / "unreviewed-plan.json")
        self.documented(apply, "capture", capture_args=args, expected=1)
        self.values["plan_file"] = plan_file
        saved = self.documented(apply, "capture", capture_args=args)
        self.assertEqual(saved["status"], "captured")
        self.assertTrue(self.documented(apply, "validate")["valid"])
        records = {}
        for entry in saved["changed"]:
            self.values["record_id"] = entry["record_id"]
            read = self.documented(example(CAPTURE, "read-saved-record"), "get")
            records[entry["collection"]] = read["payload"]
        self.assertEqual(set(records), {"entities", "observations", "sources"})
        self.assertEqual(records["sources"]["title"], specimen["source"]["title"])
        self.assertEqual(records["entities"]["name"], specimen["entities"][0]["name"])
        observation = records["observations"]
        self.assertEqual(observation["value"], specimen["observations"][0]["value"])
        self.assertEqual(observation["entity_refs"], [records["entities"]["id"]])
        self.assertEqual(observation["source_ref"], "[withheld in review_safe mode]")

    def test_inbox_follows_snapshot_cursor_and_leaves_acknowledgement_pending(self) -> None:
        self.initialize()
        with v4.open_store(self.store_path) as store:
            for revision in (1, 2, 3):
                applied(
                    store,
                    [
                        change(
                            "entity_project",
                            name=f"Revision {revision}",
                            expected_revision=revision,
                        )
                    ],
                    fingerprint=f"revision_{revision}",
                )
        self.values["page_size"] = "1"
        page = self.documented(example(PROTOCOL, "first-inbox-page"), "inbox", default_config=True)
        self.values["snapshot_watermark"] = str(page["snapshot_watermark"])
        first_watermark = page["since_watermark"]
        rows = []
        cursors: set[str] = set()
        while True:
            rows.extend(page["changes"])
            if page["complete"]:
                break
            cursor = page["next_cursor"]
            self.assertNotIn(cursor, cursors)
            cursors.add(cursor)
            self.assertLessEqual(len(cursors), 3)
            self.values["next_cursor"] = cursor
            page = self.documented(
                example(PROTOCOL, "next-inbox-page"), "inbox", default_config=True
            )
            self.assertEqual(str(page["snapshot_watermark"]), self.values["snapshot_watermark"])
        self.assertGreaterEqual(len(cursors), 2)
        self.assertEqual(
            [r["watermark"] for r in rows],
            list(range(first_watermark + 1, page["advance_to_watermark"] + 1)),
        )
        self.assertEqual(page["ack_token"], "[redacted secret]")
        first = self.documented(example(PROTOCOL, "first-inbox-page"), "inbox", default_config=True)
        self.assertEqual(first["changes"], rows[:1])

    def test_source_init_requires_an_unused_archive_path(self) -> None:
        self.initialize()
        block = example(ARCHIVE, "source-setup")
        preexisting = self.root / "existing"
        preexisting.mkdir(mode=0o700)
        set_private_path_mode(preexisting, 0o700)
        self.values["archive_root"] = str(preexisting)
        self.assertEqual(self.documented(block, "init", expected=1)["status"], "source_unavailable")
        self.assertEqual(list(preexisting.iterdir()), [])
        self.values["archive_root"] = str(self.root / "archive")
        self.assertEqual(self.documented(block, "init")["status"], "enabled")

    def test_repeating_reviewed_upgrade_recovers_an_interrupted_cutover(self) -> None:
        source = self.config.parent / "graph.json"
        self.configure_store(source)
        graph.atomic_write(source, json.dumps(make_v3_graph()))
        preview = self.documented(example(RECOVERY, "upgrade-preview"), "upgrade-v4")
        self.values.update(
            source_sha256=preview["source_sha256"],
            plan_fingerprint=preview["plan_fingerprint"],
            source_watermark=str(preview["plan"]["source_watermark"]),
        )
        apply = example(RECOVERY, "upgrade-apply")
        write = graph.atomic_write

        def interrupt(path: Path, content: str, mode: int = 0o600) -> None:
            write(path, content, mode)
            if (
                path == self.config
                and json.loads(content).get("canonical_store_format") == "sqlite_v4_wal"
            ):
                raise RuntimeError("simulated interruption after switching stores")

        with patch.object(graph, "atomic_write", interrupt):
            stopped = self.documented(apply, "upgrade-v4", expected=1)
        self.assertEqual(stopped["error"], "simulated interruption after switching stores")
        self.assertEqual(
            json.loads(self.config.read_text("utf-8"))["canonical_store_format"], "sqlite_v4_wal"
        )
        recovered = self.documented(apply, "upgrade-v4")
        self.assertEqual(recovered["status"], "upgraded_and_activated")
        self.assertTrue(recovered["cutover_recovered"])
        verify = example(RECOVERY, "verify-upgrade")
        self.assertEqual(self.documented(verify, "doctor")["graph"]["schema_version"], "v4")
        self.assertTrue(self.documented(verify, "validate")["valid"])
        migrated = self.graph_cli("get", "--id", "entity_client_alpha_site")
        self.assertEqual(migrated["payload"]["name"], "Test Client Alpha")

    def test_local_document_requires_scoped_policy_then_preserves_the_note(self) -> None:
        self.initialize()
        setup = example(ARCHIVE, "source-setup")
        self.documented(setup, "init")
        note = self.root / "support-note.txt"
        metadata = self.root / "capture-metadata.json"
        graph.atomic_write(note, example(LOCAL, "source-note", "text"))
        state = self.graph_cli("doctor")["graph"]
        substitutes = {
            "STORE_ID_FROM_DOCTOR": state["store_id"],
            "GENERATION_FROM_DOCTOR_AS_STRING": str(state["generation"]),
            "AUTHORIZED_SUBJECT_NAMESPACE": "client_alpha",
            "AUTHORIZED_CANONICAL_SUBJECT_ID": "entity_project",
            "ACTUAL_ACQUIRING_TOOL_ID": "local_file",
            "ACTUAL_TASK_ID": "task_alpha",
            "ACTUAL_RUN_ID": "run_alpha",
            "ACTUAL_ENCOUNTER_ID": "interaction_alpha",
            "ACTUAL_CAPTURE_TIME_ISO8601": FIXED_TIME,
            "SCOPED_LOCAL_PRINCIPAL": "principal_alpha",
        }
        specimen = example(LOCAL, "source-metadata", "json")
        for placeholder, value in substitutes.items():
            specimen = specimen.replace(json.dumps(placeholder), json.dumps(value))
        graph.atomic_write(metadata, specimen)
        self.values.update(
            source_input=str(note),
            metadata_file=str(metadata),
            acquired_sha256=hashlib.sha256(note.read_bytes()).hexdigest(),
        )
        intake = example(LOCAL, "source-intake")
        self.assertEqual(
            self.documented(intake, "intake-file", expected=1)["status"], "capability_unavailable"
        )
        self.documented(setup, "configure")
        saved = self.documented(intake, "intake-file")
        self.assertEqual(saved["status"], "preserved")
        self.assertEqual(saved["original_bytes"], note.stat().st_size)
        ref = saved["ref"]
        self.values["source_uri"] = (
            "tedd-source://{archive_id}/{scope_id}/{manifest_sha256}#{anchor_id}".format(**ref)
        )
        inspected = self.documented(example(LOCAL, "source-inspect"), "inspect")
        self.assertEqual(inspected["integrity"], "verified")
        self.assertEqual(inspected["source_version"], self.values["acquired_sha256"])
        self.assertEqual(inspected["units"][0]["text"], note.read_text(encoding="utf-8"))

    def test_fact_sheet_considers_and_discloses_short_facts_within_documented_limits(self) -> None:
        self.initialize()
        guide = (ROOT / "docs/MEMORY_WORKFLOW.md").read_text(encoding="utf-8")
        limits = {
            name: int(number.replace(",", ""))
            for name, number in re.findall(r"<!-- sheet: (\w+) -->\s*([\d,]+)", guide)
        }
        packet = OutcomeFixture.packet()
        packet["claims"] = [
            OutcomeFixture.claim(f"site_{i:02}", f"Provider {i}", family="deployment")
            for i in range(16)
        ]
        graph.atomic_write(Path(self.values["packet"]), json.dumps(packet))
        self.values["watermark"] = str(self.graph_cli("doctor")["graph"]["watermark"])
        block, args = self.capture_inputs()
        self.values["fingerprint"] = self.documented(block, "capture", capture_args=args)[
            "fingerprint"
        ]
        self.documented(example(CAPTURE, "apply-save"), "capture", capture_args=args)
        with v4.open_store(self.store_path, readonly=True) as store:
            candidates = store.current_state(self.values["subscription_id"])
        self.assertTrue(candidates["truncated"])
        self.assertEqual(len(candidates["items"]), limits["candidates"])
        self.assertEqual(limits["characters"], CURRENT_STATE_CHARS)
        self.assertEqual(limits["percent"] * CURRENT_STATE_BUDGET_DIVISOR, 100)

        result = self.documented(example(PROTOCOL, "task-context"), "context", default_config=True)
        sheet = result["current_state"]
        self.assertTrue(sheet["truncated"])
        self.assertTrue(sheet["items"])
        self.assertLess(len(sheet["items"]), len(candidates["items"]))
        self.assertLessEqual(len(json.dumps(sheet, ensure_ascii=False)), limits["characters"])

    def test_missing_store_distinguishes_restore_from_first_installation(self) -> None:
        self.configure_store(self.store_path)
        graph.atomic_write(
            Path(self.values["packet"]),
            example(REF / "capture-schema.md", "restore-packet", "json"),
        )
        before = set(self.root.rglob("*"))
        status = self.documented(example(RECOVERY, "diagnose-store"), "doctor", default_config=True)
        self.assertEqual(status["store_state"], "missing")
        self.values["watermark"] = "0"
        block, args = self.capture_inputs()
        result = self.documented(block, "capture", capture_args=args, expected=1)
        self.assertEqual(result["error_code"], "store_missing")
        self.assertEqual(result["store_path"], str(self.store_path))
        self.assertEqual(result["config_path"], str(self.config))
        self.assertIn("restore or sync", result["error"])
        self.assertIn("For a first installation, run init-v4", result["error"])
        self.assertEqual(set(self.root.rglob("*")), before)
