"""Installed-tree contracts for Tedd's optional local graph MCP server."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock

import check_repository_boundary as boundary  # noqa: E402
import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402

from tests.support.graph_cases import applied, change  # noqa: E402
from tests.support.paths import PLUGIN_ROOT as PLUGIN
from tests.support.paths import REPOSITORY_ROOT as ROOT


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.iterdir(), key=lambda item: item.name):
        if candidate.name.endswith("-shm"):
            continue
        digest.update(candidate.name.encode("utf-8"))
        digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _directory_names(path: Path) -> list[str]:
    return sorted(candidate.name for candidate in path.iterdir())


def _shm_shape(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_size, metadata.st_mode & 0o777


def _mcp_request(identifier: int, method: str, params: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")


def _copied_python_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Run copied Python children without source-checkout or user-site imports."""

    environment = dict(inherited)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONSAFEPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _checkout_source_paths(
    import_paths: list[str | Path],
    checkout_root: Path,
    *,
    reviewed_package_roots: set[Path],
    empty_path: Path | None = None,
) -> list[Path]:
    """Return checkout import roots other than the reviewed interpreter's packages."""

    checkout = checkout_root.resolve()
    allowed = {path.resolve() for path in reviewed_package_roots}
    resolved_paths = [Path(path or empty_path or Path.cwd()).resolve() for path in import_paths]
    return [
        path for path in resolved_paths if path.is_relative_to(checkout) and path not in allowed
    ]


class TeddMcpPackagingTests(unittest.TestCase):
    """Exercise the host-facing package surface rather than source-text markers."""

    def test_claude_manifest_overrides_only_graph_launch_with_per_user_files(self) -> None:
        manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual("./plugins/tedd", marketplace["plugins"][0]["source"])
        self.assertEqual(
            "Local project memory and delegated-work workflows.", marketplace.get("description")
        )
        self.assertEqual("tedd", manifest["name"])
        self.assertEqual(
            json.loads((PLUGIN / "plugin.json").read_text())["version"], manifest["version"]
        )
        for key in ("python", "graph_config"):
            self.assertEqual("file", manifest["userConfig"][key]["type"])
            self.assertTrue(manifest["userConfig"][key]["required"])
        self.assertEqual({"tedd-graph"}, set(manifest["mcpServers"]))
        server = manifest["mcpServers"]["tedd-graph"]
        self.assertEqual("${user_config.python}", server["command"])
        self.assertEqual(
            [
                "-B",
                "${CLAUDE_PLUGIN_ROOT}/skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py",
                "--config",
                "${user_config.graph_config}",
            ],
            server["args"],
        )
        self.assertNotIn("cwd", server)
        self.assertEqual({"SessionStart", "UserPromptSubmit", "Stop"}, set(manifest["hooks"]))
        for event in manifest["hooks"].values():
            hook = event[0]["hooks"][0]
            self.assertEqual("${user_config.python}", hook["command"])
            self.assertIn("${user_config.graph_config}", hook["args"])
            self.assertEqual(10, hook["timeout"])
        codex_hooks = json.loads((PLUGIN / "hooks/codex.json").read_text())
        self.assertEqual({"SessionStart", "UserPromptSubmit", "Stop"}, set(codex_hooks["hooks"]))
        # Documented same-name override, not evidence of actual host loading.
        codex = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual({"tedd-graph"}, set({**codex, **manifest["mcpServers"]}))

    def test_checkout_import_guard_allows_only_reviewed_interpreter_package_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-import-guard-") as temporary:
            checkout = Path(temporary) / "checkout"
            reviewed_packages = checkout / ".venv/lib/python3.12/site-packages"
            lookalike_packages = checkout / "shadow/site-packages"

            self.assertEqual(
                [checkout.resolve(), lookalike_packages.resolve()],
                _checkout_source_paths(
                    [checkout, reviewed_packages, lookalike_packages],
                    checkout,
                    reviewed_package_roots={reviewed_packages},
                ),
            )

    def test_shipped_plugin_contains_no_repository_tests(self) -> None:
        test_files = sorted(
            path.relative_to(PLUGIN).as_posix() for path in PLUGIN.rglob("test_*.py")
        )
        self.assertEqual([], test_files)

    def test_codex_manifest_references_one_closed_local_stdio_server(self) -> None:
        """Catch a config that adds a network, secret, or host-global launch path."""

        manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        portable_manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
        mcp_config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))

        self.assertEqual("./.mcp.json", manifest["mcpServers"])
        self.assertNotIn("mcpServers", portable_manifest)
        self.assertEqual(
            {
                "mcpServers": {
                    "tedd-graph": {
                        "command": "python3",
                        "args": ["./skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"],
                        "cwd": ".",
                    }
                }
            },
            mcp_config,
        )
        self.assertEqual([], boundary.mcp_packaging_errors())

    def test_copied_plugin_tree_runs_all_read_tools_without_state_mutation(self) -> None:
        """Catch a copied install whose configured launcher cannot read all tools safely."""

        unavailable = boundary.mcp_launcher_availability()
        if unavailable is not None:
            self.skipTest(unavailable)
        self._assert_copied_read_tools("python3")

    def test_explicit_interpreter_runs_all_copied_read_tools_without_state_mutation(self) -> None:
        """Qualify copied subprocess tools when the packaged python3 is unavailable."""

        self._assert_copied_read_tools(sys.executable)

    def test_resolved_claude_launch_runs_copied_tools_without_host_or_checkout_cwd(self) -> None:
        """Exercise resolved launch bytes, not Claude's substitution or native discovery."""
        self._assert_copied_read_tools(sys.executable, claude=True)

    def _assert_copied_read_tools(self, interpreter: str, *, claude: bool = False) -> None:
        # Some Windows AppData ancestors grant application-capability writes.
        # Use a new owned leaf under the home directory; never relax parent ACLs.
        parent = Path.home() if os.name == "nt" else None
        with tempfile.TemporaryDirectory(prefix=".tedd-mcp-test-", dir=parent) as temporary:
            temporary_root = Path(temporary).resolve()
            set_private_path_mode(temporary_root, 0o700)
            installed = temporary_root / "installed package" / "tedd"
            shutil.copytree(PLUGIN, installed)
            state_root = temporary_root / "state"
            state_root.mkdir(mode=0o700)
            set_private_path_mode(state_root, 0o700)
            store_path = state_root / "graph.sqlite3"
            with v4.create_store(store_path) as store:
                applied(store, [change("record-alpha")], fingerprint="packaging-read-only")
                store.register_consumer(
                    "consumer-alpha", agent_id="agent-alpha", task_id="task-alpha"
                )
                store.subscribe(
                    "consumer-alpha",
                    {"all": True},
                    subscription_id="subscription-alpha",
                    start_watermark=0,
                )
                config_path = state_root / "config.json"
                config_path.write_text(
                    json.dumps({"store_path": str(store_path)}), encoding="utf-8"
                )
                set_private_path_mode(config_path, 0o600)
                before = _directory_digest(state_root)
                before_names = _directory_names(state_root)
                shm_path = store_path.with_name(f"{store_path.name}-shm")
                self.assertTrue(shm_path.exists())
                before_shm_shape = _shm_shape(shm_path)
                before_state = store.state()
                before_consumer = store.consumer_status("consumer-alpha")
                before_subscription = store.subscription_identity("subscription-alpha")
                configured = json.loads((installed / ".mcp.json").read_text(encoding="utf-8"))
                server = configured["mcpServers"]["tedd-graph"]
                inherited_environment = os.environ.copy()
                inherited_environment["PYTHONPATH"] = str(ROOT)
                child_environment = _copied_python_environment(inherited_environment)
                self.assertNotIn("PYTHONPATH", child_environment)
                self.assertNotIn("PYTHONSAFEPATH", child_environment)
                self.assertEqual("1", child_environment["PYTHONNOUSERSITE"])
                command = [
                    interpreter,
                    *server["args"],
                    "--config",
                    str(config_path),
                ]
                working_directory = installed / server["cwd"]
                if claude:
                    declared = json.loads((installed / ".claude-plugin/plugin.json").read_text())
                    server = declared["mcpServers"]["tedd-graph"]
                    substitutions = {
                        "${user_config.python}": interpreter,
                        "${user_config.graph_config}": str(config_path),
                        "${CLAUDE_PLUGIN_ROOT}": str(installed),
                    }
                    command = [server["command"], *server["args"]]
                    for placeholder, value in substitutions.items():
                        command = [argument.replace(placeholder, value) for argument in command]
                    working_directory = temporary_root
                import_probe = subprocess.run(
                    [
                        interpreter,
                        "-c",
                        (
                            "import json, sys, sysconfig; "
                            "print(json.dumps({'path': sys.path, 'package_roots': "
                            "[sysconfig.get_path('purelib'), sysconfig.get_path('platlib')]}))"
                        ),
                    ],
                    cwd=working_directory,
                    env=child_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    0,
                    import_probe.returncode,
                    import_probe.stderr.decode("utf-8"),
                )
                checkout_root = ROOT.resolve()
                import_layout = json.loads(import_probe.stdout)
                child_paths = import_layout["path"]
                reviewed_package_roots = {
                    Path(entry) for entry in import_layout["package_roots"] if entry
                }
                checkout_paths = _checkout_source_paths(
                    child_paths,
                    checkout_root,
                    reviewed_package_roots=reviewed_package_roots,
                    empty_path=installed,
                )
                self.assertEqual([], checkout_paths)
                request_lines = (
                    b"\n".join(
                        (
                            _mcp_request(
                                1,
                                "initialize",
                                {
                                    "protocolVersion": "2025-11-25",
                                    "clientInfo": {"name": "packaging-test"},
                                },
                            ),
                            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
                            _mcp_request(2, "tools/list", {"_meta": {"progressToken": 0}}),
                            _mcp_request(
                                3,
                                "tools/call",
                                {
                                    "name": "brain_status",
                                    "arguments": {},
                                    "_meta": {"progressToken": 1},
                                },
                            ),
                            _mcp_request(
                                4,
                                "tools/call",
                                {
                                    "name": "recall_context",
                                    "_meta": {"progressToken": 2},
                                    "arguments": {
                                        "subscription_id": "subscription-alpha",
                                        "consumer_id": "consumer-alpha",
                                        "agent_id": "agent-alpha",
                                        "task_id": "task-alpha",
                                    },
                                },
                            ),
                            _mcp_request(
                                5,
                                "tools/call",
                                {
                                    "name": "pull_changes",
                                    "_meta": {"progressToken": 3},
                                    "arguments": {
                                        "subscription_id": "subscription-alpha",
                                        "consumer_id": "consumer-alpha",
                                        "agent_id": "agent-alpha",
                                        "task_id": "task-alpha",
                                    },
                                },
                            ),
                            _mcp_request(
                                6,
                                "tools/call",
                                {
                                    "name": "search_memory",
                                    "arguments": {
                                        "subscription_id": "subscription-alpha",
                                        "consumer_id": "consumer-alpha",
                                        "agent_id": "agent-alpha",
                                        "task_id": "task-alpha",
                                        "query": "record-alpha",
                                    },
                                },
                            ),
                        )
                    )
                    + b"\n"
                )
                completed = subprocess.run(
                    command,
                    cwd=working_directory,
                    input=request_lines,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=child_environment,
                    # Six reads include native privacy checks; allow bounded
                    # startup/ACL overhead on loaded Windows hosts.
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8"))
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual(6, len(responses))
                for response in responses:
                    self.assertNotIn("error", response)
                manifest = json.loads(
                    (installed / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest["version"], responses[0]["result"]["serverInfo"]["version"]
                )
                self.assertEqual(
                    ["brain_status", "recall_context", "search_memory", "pull_changes"],
                    [tool["name"] for tool in responses[1]["result"]["tools"]],
                )
                health = responses[2]["result"]
                self.assertEqual(
                    {"safe": True, "coordination_scope": "same-machine-only"},
                    health["structuredContent"]["store"],
                )
                self.assertNotIn(str(store_path), completed.stdout.decode("utf-8"))
                for response in responses[2:]:
                    result = response["result"]
                    self.assertFalse(result.get("isError", False))
                    self.assertEqual(
                        result["structuredContent"],
                        json.loads(result["content"][0]["text"]),
                    )
                self.assertEqual(
                    ["record-alpha"],
                    [
                        record["record_id"]
                        for record in responses[3]["result"]["structuredContent"]["records"]
                    ],
                )
                self.assertEqual(
                    ["record-alpha"],
                    [
                        change["record_id"]
                        for change in responses[4]["result"]["structuredContent"]["changes"]
                    ],
                )
                self.assertEqual(before, _directory_digest(state_root))
                search = responses[5]["result"]["structuredContent"]
                self.assertEqual(["record-alpha"], [r["record_id"] for r in search["results"]])
                self.assertEqual(before_state["watermark"], search["watermark"])
                self.assertFalse(search["truncated"])
                self.assertEqual(before_names, _directory_names(state_root))
                self.assertEqual(before_shm_shape, _shm_shape(shm_path))
                self.assertEqual(before_state, store.state())
                self.assertEqual(before_consumer, store.consumer_status("consumer-alpha"))
                self.assertEqual(
                    before_subscription, store.subscription_identity("subscription-alpha")
                )

    def test_cli_remains_usable_when_the_optional_mcp_config_is_removed(self) -> None:
        """Catch a CLI path that accidentally requires the optional MCP declaration."""

        parent = Path.home() if os.name == "nt" else None
        with tempfile.TemporaryDirectory(prefix=".tedd-mcp-test-", dir=parent) as temporary:
            temporary_root = Path(temporary).resolve()
            set_private_path_mode(temporary_root, 0o700)
            installed = temporary_root / "tedd"
            shutil.copytree(PLUGIN, installed)
            (installed / ".mcp.json").rename(installed / ".mcp.json.disabled")
            state_root = temporary_root / "state"
            state_root.mkdir(mode=0o700)
            set_private_path_mode(state_root, 0o700)
            store_path = state_root / "graph.sqlite3"
            with v4.create_store(store_path):
                config_path = state_root / "config.json"
                config_path.write_text(
                    json.dumps({"store_path": str(store_path)}), encoding="utf-8"
                )
                set_private_path_mode(config_path, 0o600)
                cli = installed / "skills/obsidian-knowledge-graph/scripts/knowledge_graph.py"
                for command in (("doctor",), ("recall", "nothing-recorded")):
                    with self.subTest(command=command):
                        completed = subprocess.run(
                            [sys.executable, str(cli), "--config", str(config_path), *command],
                            cwd=installed,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=10,
                            check=False,
                        )
                        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8"))

    def test_missing_python3_reports_optional_mcp_unavailable_but_copied_cli_works(self) -> None:
        """Catch an absent optional launcher that incorrectly invalidates a copied package."""

        parent = Path.home() if os.name == "nt" else None
        with tempfile.TemporaryDirectory(prefix=".tedd-mcp-test-", dir=parent) as temporary:
            temporary_root = Path(temporary).resolve()
            set_private_path_mode(temporary_root, 0o700)
            installed = temporary_root / "tedd"
            shutil.copytree(PLUGIN, installed)
            state_root = temporary_root / "state"
            state_root.mkdir(mode=0o700)
            set_private_path_mode(state_root, 0o700)
            store_path = state_root / "graph.sqlite3"
            with v4.create_store(store_path):
                config_path = state_root / "config.json"
                config_path.write_text(
                    json.dumps({"store_path": str(store_path)}), encoding="utf-8"
                )
                set_private_path_mode(config_path, 0o600)
                isolated_path = temporary_root / "no-python3"
                isolated_path.mkdir()
                isolated_environment = os.environ.copy()
                isolated_environment["PATH"] = str(isolated_path)
                self.assertIsNone(shutil.which("python3", path=isolated_environment["PATH"]))

                copied_manifest = json.loads(
                    (installed / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
                )
                with (
                    mock.patch.object(boundary, "PLUGIN_ROOT", installed),
                    mock.patch.object(boundary, "MCP_CONFIG", installed / ".mcp.json"),
                    mock.patch.dict(
                        os.environ, {"PATH": isolated_environment["PATH"]}, clear=False
                    ),
                ):
                    self.assertEqual([], boundary.mcp_packaging_errors())
                    self.assertEqual(
                        "Optional MCP unavailable: python3 launcher not found",
                        boundary.mcp_launcher_availability(),
                    )
                    self.assertEqual(
                        [],
                        boundary.portable_plugin_errors(
                            installed,
                            copied_manifest,
                            expected_skills=boundary.PACKAGED_SKILLS,
                        ),
                    )
                    output = io.StringIO()
                    with (
                        mock.patch.object(boundary, "repository_files", return_value=[]),
                        mock.patch.object(boundary, "manifest_errors", return_value=[]),
                        contextlib.redirect_stdout(output),
                    ):
                        self.assertEqual(0, boundary.main())
                    self.assertIn(
                        "Optional MCP unavailable: python3 launcher not found", output.getvalue()
                    )

                cli = installed / "skills/obsidian-knowledge-graph/scripts/knowledge_graph.py"
                completed = subprocess.run(
                    [sys.executable, str(cli), "--config", str(config_path), "doctor"],
                    cwd=installed,
                    env=isolated_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
