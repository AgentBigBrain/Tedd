"""Public command contracts when the configured graph is absent or disappears."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import knowledge_graph as kg
from platform_support import set_private_path_mode

from tests.support.graph_cases import FIXED_TIME, V4TestCase, make_v3_graph
from tests.support.paths import GRAPH_SCRIPTS

# Real parser inputs for each refusing command. New commands need an explicit policy.
SCOPE = "--consumer-id fixture --subscription-id fixture"
REFUSALS = {
    "backup": "--output {output}",
    "capture": "--input {packet}",
    "changes": "--since-watermark 0",
    "checkpoint": "",
    "consumer-rebind": "--consumer-id fixture --existing-task-id task --expected-old-agent-id agent",
    "consumer-register": "--consumer-id fixture",
    "consumer-status": "--consumer-id fixture",
    "context": "--subscription-id fixture",
    "export": "--output {output}",
    "fold-candidates": SCOPE,
    "get": "--id event_fixture",
    "handoff": "--check --task-id task --prompt prompt --consumer-id fixture --producer-agent-id agent",
    "history": "--id event_fixture",
    "inbox": "--subscription-id fixture",
    "inbox-ack": "--subscription-id fixture --through-watermark 1 --ack-token token",
    "index-status": "",
    "loops-review": SCOPE,
    "migrate": "",
    "project": "",
    "recall": "fixture",
    "redact": "--id event_fixture",
    "reindex": "",
    "resolve-loop": "--id loop_fixture --resolution complete",
    "review-answers": SCOPE + " --input {packet} --packet-output {output}",
    "review-queue": SCOPE,
    "source-evidence": SCOPE
    + " --id event_fixture --subject-id subject --producer-agent-id agent --task-id task --producer-run-id run",
    "status": "",
    "subscribe": "--consumer-id fixture",
    "upgrade-v4": "--dry-run",
    "validate": "",
}

PACKET = {
    "source": {
        "kind": "test",
        "role": "fixture",
        "title": "Disposable fixture",
        "observed_at": FIXED_TIME,
        "source_tier": "validated_structured_candidate",
    },
    "events": [{"kind": "test", "title": "Saved while the graph exists"}],
}


def snapshot(folder: Path) -> dict[Path, bytes | None]:
    return {
        p.relative_to(folder): p.read_bytes() if p.is_file() else None for p in folder.rglob("*")
    }


class MissingStoreTests(V4TestCase):
    def fixture(self, name: str, filename: str = "graph.sqlite3") -> tuple[Path, Path, Path]:
        folder = self.root / name
        folder.mkdir()
        set_private_path_mode(folder, 0o700)
        config, store, packet = folder / "config.json", folder / filename, folder / "packet.json"
        kg.atomic_write(config, json.dumps({"store_path": str(store)}))
        kg.atomic_write(packet, json.dumps(PACKET))
        return config, store, packet

    def cli(self, config: Path, *args: str, expected: int = 0) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["knowledge_graph.py", "--config", str(config), *args]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = kg.main()
        self.assertEqual(code, expected, err.getvalue() or out.getvalue())
        self.assertEqual((out if expected else err).getvalue(), "")
        return json.loads((err if expected else out).getvalue())

    def assert_missing(self, config: Path, store: Path, *args: str) -> None:
        error = self.cli(config, *args, expected=1)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error.get("error_code"), "store_missing", error)
        self.assertEqual(error["store_path"], str(store))
        self.assertEqual(error["config_path"], str(config))

    def test_command_matrix(self) -> None:
        subcommands = next(
            action
            for action in kg.build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(set(subcommands.choices), {*REFUSALS, "doctor", "init-v4", "configure"})
        for command, flags in REFUSALS.items():
            with self.subTest(command=command):
                config, store, packet = self.fixture(command)
                before = snapshot(config.parent)
                args = [
                    s.format(packet=packet, output=config.parent / "output") for s in flags.split()
                ]
                self.assert_missing(config, store, command, *args)
                self.assertEqual(snapshot(config.parent), before)

    def test_doctor_and_full_validation(self) -> None:
        config, store, _ = self.fixture("diagnostics")
        before = snapshot(config.parent)
        report = self.cli(config, "doctor")
        self.assertIs(report["store_exists"], False)
        self.assertEqual(report["store_state"], "missing")
        self.assertEqual(report["store"], str(store))
        self.assertEqual(report["config"], str(config))
        for key in (
            "stored_schema_version",
            "graph_schema_version",
            "validation",
            "graph_watermark",
        ):
            self.assertNotIn(key, report)
        self.assert_missing(config, store, "validate", "--full")
        self.assertEqual(snapshot(config.parent), before)

    def test_capture_requires_existing_store_regardless_of_filename(self) -> None:
        for index, filename in enumerate(("f.json", "graph.sqlite3", "no-extension")):
            config, store, packet = self.fixture(str(index), filename)
            before = snapshot(config.parent)
            for extra in (["--dry-run", "--plan-output", str(config.parent / "plan")], []):
                with self.subTest(filename=filename, extra=extra):
                    self.assert_missing(config, store, "capture", "--input", str(packet), *extra)
                    self.assertEqual(snapshot(config.parent), before)

    def test_store_removed_before_locked_capture_is_not_recreated(self) -> None:
        config, store, packet = self.fixture("removed")
        kg.save_graph(store, make_v3_graph())
        real_lock = kg.store_lock

        @contextmanager
        def remove_while_locked(path, timeout):
            with real_lock(path, timeout):
                path.unlink()
                yield

        with patch.object(kg, "store_lock", remove_while_locked):
            self.assert_missing(config, store, "capture", "--input", str(packet))
        self.assertFalse(store.exists())
        self.assertFalse(kg.recall_index_path(store).exists())

    def test_explicit_graphs_accept_capture(self) -> None:
        for version in ("v3", "v4"):
            with self.subTest(version=version):
                config, store, packet = self.fixture(
                    version, "graph.json" if version == "v3" else "graph.sqlite3"
                )
                capture = ["capture", "--input", str(packet)]
                if version == "v3":
                    kg.save_graph(store, make_v3_graph())
                else:
                    self.cli(config, "init-v4")
                    producer = [
                        "--consumer-id",
                        "fixture",
                        "--producer-agent-id",
                        "agent-fixture",
                        "--task-id",
                        "fixture",
                        "--producer-run-id",
                        "run-fixture",
                        "--based-on-watermark",
                        "0",
                    ]
                    self.cli(config, "consumer-register", *producer)
                    capture += [*producer, "--discovery-kind", "verification"]
                    plan = str(config.parent / "plan")
                    review = self.cli(config, *capture, "--dry-run", "--plan-output", plan)
                    capture += [
                        "--expect-plan-file",
                        plan,
                        "--expect-fingerprint",
                        review["fingerprint"],
                    ]
                saved = self.cli(config, *capture)
                self.assertEqual(saved["status"], "captured")
                event = next(
                    record for record in saved["changed_ids"] if record.startswith("event_")
                )
                found = self.cli(config, "get", "--id", event)
                self.assertIn("Saved while the graph exists", json.dumps(found))
                if version == "v3":
                    self.cli(config, "upgrade-v4", "--dry-run")
                else:
                    self.assertTrue(self.cli(config, "validate", "--full")["valid"])

    def test_configuration_can_precede_graph_creation(self) -> None:
        config, store, _ = self.fixture("setup")
        vault = config.parent / "vault"
        kg.ensure_private_directory(vault)
        configured = self.cli(config, "configure", "--vault", str(vault))
        self.assertEqual(configured["status"], "configured")
        self.assertFalse(store.exists())

    def test_graph_endpoint_and_source_lookup_refuse_without_writes(self) -> None:
        config, _, _ = self.fixture("hosts")
        before = snapshot(config.parent)
        messages = [
            {
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "clientInfo": {}},
            },
            {"id": 2, "method": "tools/call", "params": {"name": "brain_status", "arguments": {}}},
        ]
        graph = subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "tedd_graph_mcp.py"),
                "--config",
                str(config),
            ],
            input="".join(
                json.dumps({"jsonrpc": "2.0", **msg}) + "\n" for msg in messages
            ).encode(),
            capture_output=True,
            timeout=30,
            check=True,
        )
        refused = json.loads(graph.stdout.splitlines()[-1])
        self.assertTrue(refused["result"]["isError"])
        self.assertEqual(
            refused["result"]["structuredContent"]["category"], "unavailable_capability"
        )
        self.assertEqual(snapshot(config.parent), before)
        source = subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "tedd_sources.py"),
                "--config",
                str(config),
                "lookup",
                "--consumer-id",
                "fixture",
                "--producer-agent-id",
                "agent",
                "--task-id",
                "task",
                "--producer-run-id",
                "run",
                "--subscription-id",
                "fixture",
                "--subject-id",
                "subject",
                "--query",
                "fixture",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(source.returncode, 1, source.stderr)
        self.assertEqual(json.loads(source.stdout)["status"], "source_unavailable")
        self.assertEqual(snapshot(config.parent), before)
