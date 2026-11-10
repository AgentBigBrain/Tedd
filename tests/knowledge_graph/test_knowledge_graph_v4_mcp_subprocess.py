"""Binary-level stdio purity regression for the graph MCP launcher."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

LAUNCHER = SCRIPT_DIRECTORY / "tedd_graph_mcp.py"

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph.v4_mcp_contracts import MAX_REQUEST_BYTES  # noqa: E402

from tests.support.graph_cases import applied, change  # noqa: E402


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.iterdir(), key=lambda item: item.name):
        if candidate.name.endswith("-shm"):
            continue
        digest.update(candidate.name.encode("utf-8"))
        digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _directory_entries(path: Path) -> list[tuple[str, str]]:
    return [
        (candidate.name, hashlib.sha256(candidate.read_bytes()).hexdigest())
        for candidate in sorted(path.iterdir(), key=lambda item: item.name)
        if not candidate.name.endswith("-shm")
    ]


def _directory_names(path: Path) -> list[str]:
    return sorted(candidate.name for candidate in path.iterdir())


def _shm_metadata(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mode & 0o777


class McpSubprocessTests(unittest.TestCase):
    def test_partial_search_and_strict_opt_out_use_the_real_read_only_endpoint(self) -> None:
        question = "Who approved the Harborstudio logo?"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            store_path = root / "graph.sqlite3"
            with v4.create_store(store_path) as store:
                sensitive = change("sensitive", name=question)
                sensitive["payload"]["sensitive"] = True
                applied(
                    store,
                    [
                        change("approval", name="Harborstudio logo approved teal"),
                        change("proposal", name="Harborstudio logo proposal cobalt"),
                        change("foreign", name=question),
                        sensitive,
                    ],
                    fingerprint="partial-search-subprocess",
                )
                store.register_consumer("reader", agent_id="agent", task_id="task")
                store.subscribe(
                    "reader",
                    {"record_ids": ["approval", "proposal", "sensitive"]},
                    subscription_id="scope",
                )
                config = root / "config.json"
                config.write_text(json.dumps({"store_path": str(store_path)}), encoding="utf-8")
                set_private_path_mode(config, 0o600)
                before = _directory_digest(root)
                state = store.state()
                consumer = store.consumer_status("reader")
                subscription = store.subscription_identity("scope")
                cases: dict[str, dict[str, str | int]] = {
                    "default_partial": {"query": question},
                    "strict_opt_out": {"query": question, "match": "all"},
                    "strict_match": {"query": "logo approved"},
                    "bounded_partial": {"query": question, "limit": 1},
                }
                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"},
                    },
                ]
                for number, arguments in enumerate(cases.values(), 2):
                    requests.append(
                        {
                            "jsonrpc": "2.0",
                            "id": number,
                            "method": "tools/call",
                            "params": {
                                "name": "search_memory",
                                "arguments": {
                                    "subscription_id": "scope",
                                    "consumer_id": "reader",
                                    "agent_id": "agent",
                                    "task_id": "task",
                                    **arguments,
                                },
                            },
                        }
                    )
                completed = subprocess.run(
                    [sys.executable, str(LAUNCHER), "--config", str(config)],
                    input=("\n".join(json.dumps(request) for request in requests) + "\n").encode(),
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode())
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(len(requests), len(responses))
                results = {
                    name: response["result"]["structuredContent"]
                    for name, response in zip(cases, responses[1:], strict=True)
                }
                partial = results["default_partial"]
                self.assertEqual(
                    ["approval", "proposal"], [r["record_id"] for r in partial["results"]]
                )
                self.assertEqual("any_terms", partial["match_mode"])
                self.assertEqual(["approved", "harborstudio", "logo"], partial["considered_terms"])
                self.assertEqual(
                    [["approved", "harborstudio", "logo"], ["harborstudio", "logo"]],
                    [r["matched_terms"] for r in partial["results"]],
                )
                self.assertFalse(partial["truncated"])
                self.assertEqual([], results["strict_opt_out"]["results"])
                self.assertEqual("all_terms", results["strict_opt_out"]["match_mode"])
                strict = results["strict_match"]
                self.assertEqual(["approval"], [r["record_id"] for r in strict["results"]])
                self.assertEqual("all_terms", strict["match_mode"])
                self.assertNotIn("matched_terms", strict["results"][0])
                bounded = results["bounded_partial"]
                self.assertEqual(["approval"], [r["record_id"] for r in bounded["results"]])
                self.assertTrue(bounded["truncated"])
                self.assertEqual(before, _directory_digest(root))
                self.assertEqual(state, store.state())
                self.assertEqual(consumer, store.consumer_status("reader"))
                self.assertEqual(subscription, store.subscription_identity("scope"))

    def test_prefix_search_and_refusals_use_the_real_read_only_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            store_path = root / "graph.sqlite3"
            with v4.create_store(store_path) as store:
                applied(
                    store,
                    [
                        change("approval", name="Harborstudio logo approval"),
                        change("approved", name="Harborside logo approved"),
                        change("foreign", name="Harborstudio logo approved"),
                    ],
                    fingerprint="prefix-subprocess",
                )
                store.register_consumer("reader", agent_id="agent", task_id="task")
                store.subscribe(
                    "reader",
                    {"record_ids": ["approval", "approved"]},
                    subscription_id="scope",
                )
                config = root / "config.json"
                config.write_text(json.dumps({"store_path": str(store_path)}), encoding="utf-8")
                set_private_path_mode(config, 0o600)
                before = _directory_digest(root)
                consumer = store.consumer_status("reader")
                subscription = store.subscription_identity("scope")
                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"},
                    },
                ]
                for number, query in enumerate(
                    ("harbor* logo approv*", "logo app*", "logo approved*"), 2
                ):
                    requests.append(
                        {
                            "jsonrpc": "2.0",
                            "id": number,
                            "method": "tools/call",
                            "params": {
                                "name": "search_memory",
                                "arguments": {
                                    "subscription_id": "scope",
                                    "consumer_id": "reader",
                                    "agent_id": "agent",
                                    "task_id": "task",
                                    "query": query,
                                },
                            },
                        }
                    )
                completed = subprocess.run(
                    [sys.executable, str(LAUNCHER), "--config", str(config)],
                    input=("\n".join(json.dumps(r) for r in requests) + "\n").encode(),
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode())
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(4, len(responses))
                matches = responses[1]["result"]["structuredContent"]
                self.assertEqual(
                    ["approval", "approved"], [r["record_id"] for r in matches["results"]]
                )
                self.assertEqual(
                    "invalid_request", responses[2]["result"]["structuredContent"]["category"]
                )
                self.assertEqual(
                    ["approved"],
                    [
                        r["record_id"]
                        for r in responses[3]["result"]["structuredContent"]["results"]
                    ],
                )
                self.assertEqual(before, _directory_digest(root))
                self.assertEqual(consumer, store.consumer_status("reader"))
                self.assertEqual(subscription, store.subscription_identity("scope"))
                self.assertNotIn(str(root), completed.stdout.decode())

    def test_launcher_returns_only_one_compact_protocol_line_for_malformed_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"store_path": str(root / "missing.sqlite3")}), encoding="utf-8"
            )
            set_private_path_mode(config_path, 0o600)
            completed = subprocess.run(
                [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                input=b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            lines = completed.stdout.splitlines()
            self.assertEqual(1, len(lines))
            response = json.loads(lines[0].decode("utf-8"))
            self.assertEqual("2.0", response["jsonrpc"])
            self.assertIn("error", response)
            self.assertNotIn(str(SCRIPT_DIRECTORY), completed.stderr.decode("utf-8"))

    def test_launcher_returns_a_bounded_protocol_error_for_an_oversize_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"store_path": str(root / "missing.sqlite3")}), encoding="utf-8"
            )
            set_private_path_mode(config_path, 0o600)
            completed = subprocess.run(
                [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                input=b"x" * (MAX_REQUEST_BYTES + 1) + b"\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            lines = completed.stdout.splitlines()
            self.assertEqual(1, len(lines))
            response = json.loads(lines[0].decode("utf-8"))
            self.assertEqual(-32001, response["error"]["code"])

    def test_launcher_lazily_reports_a_configured_store_inside_the_vault_without_leaking_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            vault = root / "vault"
            vault.mkdir(mode=0o700)
            set_private_path_mode(vault, 0o700)
            store_path = vault / "graph.sqlite3"
            with v4.create_store(store_path):
                pass
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"store_path": str(store_path), "vault_path": str(vault)}),
                encoding="utf-8",
            )
            set_private_path_mode(config_path, 0o600)
            completed = subprocess.run(
                [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                input=(
                    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
                    b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"brain_status","arguments":{}}}\n'
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(2, len(responses))
            self.assertTrue(responses[1]["result"]["isError"])
            self.assertEqual(
                "invalid_request", responses[1]["result"]["structuredContent"]["category"]
            )
            self.assertEqual("", completed.stderr.decode("utf-8"))
            self.assertNotIn(str(vault), completed.stderr.decode("utf-8"))
            self.assertNotIn(str(vault), completed.stdout.decode("utf-8"))

    def test_launcher_keeps_protocol_setup_available_without_a_default_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            bootstrap = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(SCRIPT_DIRECTORY)!r}); "
                "import knowledge_graph, tedd_graph_mcp; "
                "knowledge_graph.DEFAULT_CONFIG_PATH = Path(sys.argv[1]) / 'missing-config.json'; "
                "knowledge_graph.DEFAULT_STORE_PATH = Path(sys.argv[1]) / 'missing-legacy.json'; "
                "knowledge_graph.DEFAULT_V4_STORE_PATH = Path(sys.argv[1]) / 'missing-graph.sqlite3'; "
                "raise SystemExit(tedd_graph_mcp.main([]))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", bootstrap, str(root)],
                input=(
                    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
                    b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
                    b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"brain_status","arguments":{}}}\n'
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(3, len(responses))
            self.assertIn("tools", responses[1]["result"])
            self.assertTrue(responses[2]["result"]["isError"])
            self.assertEqual(
                "unavailable_capability",
                responses[2]["result"]["structuredContent"]["category"],
            )
            self.assertEqual("", completed.stderr.decode("utf-8"))

    def test_launcher_lazily_converts_malformed_explicit_config_to_a_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            config_path = root / "malformed-config.json"
            config_path.write_text("{malformed", encoding="utf-8")
            set_private_path_mode(config_path, 0o600)
            completed = subprocess.run(
                [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                input=(
                    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
                    b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
                    b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"brain_status","arguments":{}}}\n'
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(3, len(responses))
            self.assertIn("tools", responses[1]["result"])
            self.assertTrue(responses[2]["result"]["isError"])
            self.assertEqual(
                "invalid_request", responses[2]["result"]["structuredContent"]["category"]
            )
            self.assertNotIn(str(config_path), completed.stdout.decode("utf-8"))
            self.assertEqual("", completed.stderr.decode("utf-8"))

    def test_idless_subprocess_notifications_never_write_stdout_or_open_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            config_path = root / "config.json"
            config_path.write_text("{malformed", encoding="utf-8")
            set_private_path_mode(config_path, 0o600)
            completed = subprocess.run(
                [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                input=(
                    b'{"jsonrpc":"2.0","method":"tools/call","params":["invalid"]}\n'
                    b'{"jsonrpc":"2.0","method":"method/unsupported","params":{}}\n'
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, completed.returncode)
            self.assertEqual(b"", completed.stdout)
            self.assertEqual("", completed.stderr.decode("utf-8"))

    def test_launcher_uses_only_the_explicit_disposable_config_and_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            set_private_path_mode(root, 0o700)
            store_path = root / "graph.sqlite3"
            with v4.create_store(store_path) as store:
                # Keep the live writer's WAL and shared-memory state open across
                # the child read. This models the supported live-graph case and
                # proves MCP adds no bytes beyond that existing runtime state.
                applied(
                    store,
                    [change("record_alpha"), change("record_beta")],
                    fingerprint="mcp-read-only",
                )
                store.register_consumer(
                    "consumer-alpha", agent_id="agent-alpha", task_id="task-alpha"
                )
                store.subscribe(
                    "consumer-alpha",
                    {"all": True},
                    subscription_id="subscription-alpha",
                    start_watermark=0,
                )
                config_path = root / "config.json"
                config_path.write_text(
                    json.dumps({"store_path": str(store_path)}), encoding="utf-8"
                )
                set_private_path_mode(config_path, 0o600)
                before = _directory_digest(root)
                before_entries = _directory_entries(root)
                before_names = _directory_names(root)
                shm_path = store_path.with_name(f"{store_path.name}-shm")
                self.assertTrue(shm_path.exists())
                before_shm = _shm_metadata(shm_path)
                before_state = store.state()
                before_consumer = store.consumer_status("consumer-alpha")
                before_subscription = store.subscription_identity("subscription-alpha")
                request_lines = (
                    b"\n".join(
                        (
                            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","clientInfo":{"name":"test"}}}',
                            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"brain_status","arguments":{}}}',
                            b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"recall_context","arguments":{"subscription_id":"subscription-alpha","consumer_id":"consumer-alpha","agent_id":"agent-alpha","task_id":"task-alpha"}}}',
                            b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"pull_changes","arguments":{"subscription_id":"subscription-alpha","consumer_id":"consumer-alpha","agent_id":"agent-alpha","task_id":"task-alpha","limit":1}}}',
                            b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"pull_changes","arguments":{"subscription_id":"subscription-alpha","consumer_id":"consumer-alpha","agent_id":"agent-alpha","task_id":"task-alpha","since_watermark":0}}}',
                            b'{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"pull_changes","arguments":{"subscription_id":"missing","consumer_id":"consumer-alpha","agent_id":"agent-alpha","task_id":"task-alpha"}}}',
                        )
                    )
                    + b"\n"
                )
                completed = subprocess.run(
                    [sys.executable, str(LAUNCHER), "--config", str(config_path)],
                    input=request_lines,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8"))
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(6, len(responses))
                self.assertEqual("2.0", responses[0]["jsonrpc"])
                self.assertEqual(
                    responses[1]["result"]["structuredContent"],
                    json.loads(responses[1]["result"]["content"][0]["text"]),
                )
                self.assertEqual(
                    {"safe": True, "coordination_scope": "same-machine-only"},
                    responses[1]["result"]["structuredContent"]["store"],
                )
                self.assertNotIn(str(store_path), completed.stdout.decode("utf-8"))
                self.assertIn("structuredContent", responses[2]["result"])
                self.assertFalse(responses[3]["result"]["structuredContent"]["complete"])
                self.assertTrue(responses[4]["result"]["isError"])
                self.assertEqual(
                    "invalid_request", responses[4]["result"]["structuredContent"]["category"]
                )
                self.assertTrue(responses[5]["result"]["isError"])
                self.assertEqual(before_entries, _directory_entries(root))
                self.assertEqual(before, _directory_digest(root))
                self.assertEqual(before_names, _directory_names(root))
                self.assertEqual(before_shm, _shm_metadata(shm_path))
                self.assertEqual(before_state, store.state())
                self.assertEqual(before_consumer, store.consumer_status("consumer-alpha"))
                self.assertEqual(
                    before_subscription, store.subscription_identity("subscription-alpha")
                )


if __name__ == "__main__":
    unittest.main()
