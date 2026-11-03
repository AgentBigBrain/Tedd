import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts"))
sys.path.insert(0, str(ROOT / "plugins/tedd/skills/tedd/scripts"))
import knowledge_graph as graph  # noqa: E402
import knowledge_graph_v4 as storage  # noqa: E402
import knowledge_graph_v4_capture as capture  # noqa: E402
import tedd_setup as setup  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest, ProducerEnvelope  # noqa: E402
from tedd_setup import (  # noqa: E402
    SetupRequest,
    apply_setup,
    binding_for_project,
    preview_setup,
    qualify_python,
    session_binding,
)

from tests.support.graph_cases import applied, change  # noqa: E402


class SetupBehaviorTests(unittest.TestCase):
    def test_cli_sqlite_failures_are_safe_structured_refusals(self) -> None:
        class SQLiteFailure(sqlite3.OperationalError):
            sqlite_errorcode: int

        cases = (
            (None, "unavailable"),
            (5, "unavailable"),
            (6, "unavailable"),
            (10, "unavailable"),
            (266, "unavailable"),
            (11, "corrupt"),
            (26, "corrupt"),
            (267, "corrupt"),
            (3, "denied"),
            (8, "denied"),
            (23, "denied"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            arguments = [
                "--host",
                "claude",
                "--graph-config",
                str(root / "graph.json"),
                "--project",
                str(root),
                "--profile",
                str(root / "profile.json"),
            ]
            for operation in ("preview_setup", "session_binding"):
                for code, category in cases:
                    with self.subTest(operation=operation, code=code):
                        failure = SQLiteFailure("synthetic-private-database-detail")
                        if code is not None:
                            failure.sqlite_errorcode = code
                        command = arguments[:]
                        if operation == "session_binding":
                            command += [
                                "--session-id",
                                "fixture-session",
                                "--run-id",
                                "fixture-run",
                            ]
                        output = io.StringIO()
                        with (
                            contextlib.redirect_stdout(output),
                            mock.patch.object(setup, operation, side_effect=failure),
                        ):
                            try:
                                result = setup.main(command)
                            except sqlite3.Error:
                                self.fail("SQLite failure escaped the structured CLI boundary")
                        self.assertEqual(1, result)
                        payload = json.loads(output.getvalue())
                        self.assertEqual("refused", payload["status"])
                        self.assertEqual(category, payload["category"])
                        self.assertIs(False, payload["connected"])
                        self.assertNotIn("synthetic-private-database-detail", output.getvalue())
                        self.assertNotIn("Traceback", output.getvalue())
                        self.assertEqual([], list(root.iterdir()))

    def test_cli_does_not_swallow_cancellation(self) -> None:
        with mock.patch.object(setup, "preview_setup", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                setup.main(
                    [
                        "--host",
                        "claude",
                        "--graph-config",
                        "fixture.json",
                        "--project",
                        ".",
                        "--profile",
                        "fixture-profile.json",
                    ]
                )

    def test_python_probe_does_not_inherit_unrelated_environment(self) -> None:
        run = setup.run_command

        def inspect(command: list[str], *, environment: dict[str, str] | None = None) -> bytes:
            assert environment is not None
            self.assertNotIn("TEDD_SYNTHETIC_SECRET", environment)
            return run(command, environment=environment)

        with (
            mock.patch.dict(os.environ, {"TEDD_SYNTHETIC_SECRET": "not-a-real-secret"}),
            mock.patch.object(setup, "run_command", side_effect=inspect),
        ):
            qualify_python(Path(sys.executable))

    def test_store_alias_and_malformed_bindings_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            qualify_python(Path("WindowsApps/python.exe").absolute())
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            entry: dict[str, object] = {
                "directory": str(project),
                "include_descendants": False,
                "store_id": "synthetic-store",
                "generation": 1,
                "record_ids": ["record"],
                "entity_ids": [],
                "operator_id": "operator_fixture",
                "confirmed_at": "2026-01-01T00:00:00Z",
            }
            for key, value in (
                ("generation", True),
                ("generation", 1 << 70),
                ("include_descendants", 1),
                ("store_id", "x" * 1024),
                ("confirmed_at", "not an approval time"),
            ):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        setup.decode_bindings({"version": 1, "bindings": [{**entry, key: value}]})

    def test_private_input_refuses_duplicates_empty_json_and_excessive_depth(self) -> None:
        for content in (b"", b'{"version":1,"version":1}', b"[" * 30 + b"]" * 30):
            with self.subTest(content=content):
                with self.assertRaises(setup.SetupStateError):
                    setup.object_document(content)

    def test_cli_missing_configuration_is_unavailable_not_success_or_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "plugins/tedd/skills/tedd/scripts/tedd_setup.py"),
                    "--host",
                    "claude",
                    "--graph-config",
                    str(root / "missing.json"),
                    "--project",
                    str(root),
                    "--profile",
                    str(root / "profile.json"),
                    "--record-id",
                    "entity_alpha",
                ],
                capture_output=True,
                timeout=20,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual("unavailable", json.loads(completed.stdout)["category"])
            self.assertEqual([], list(root.iterdir()))

    def test_missing_configuration_never_initializes_or_creates_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            set_private_path_mode(root, 0o700)
            request = SetupRequest(
                "codex",
                root / "missing.json",
                root,
                root / "profile.json",
                root / "host",
                Path(sys.executable),
                record_ids=("entity_alpha",),
            )
            before = list(root.iterdir())
            with self.assertRaises(FileNotFoundError):
                preview_setup(request)
            self.assertEqual(before, list(root.iterdir()))

    def test_real_interpreter_has_supported_python_and_fts5(self) -> None:
        result = qualify_python(Path(sys.executable))
        self.assertGreaterEqual(tuple(cast(list[int], result["version"])[:2]), (3, 10))
        self.assertTrue(result["fts5"])

    def test_component_matching_descendants_and_store_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            bindings = {
                "version": 1,
                "bindings": [
                    {
                        "directory": str(project),
                        "include_descendants": False,
                        "store_id": "synthetic-store",
                        "generation": 1,
                        "record_ids": ["entity_alpha"],
                        "entity_ids": ["entity_alpha"],
                        "operator_id": "operator_fixture",
                        "confirmed_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
            self.assertEqual(
                ["entity_alpha"],
                binding_for_project(bindings, project, "synthetic-store", 1)["record_ids"],
            )
            for target in (project / "child", project.with_name("project-other")):
                with self.assertRaises(ValueError):
                    binding_for_project(bindings, target, "synthetic-store", 1)
            cast(list[dict[str, object]], bindings["bindings"])[0]["include_descendants"] = True
            self.assertEqual(
                ["entity_alpha"],
                binding_for_project(bindings, project / "child", "synthetic-store", 1)[
                    "record_ids"
                ],
            )
            with self.assertRaises(ValueError):
                binding_for_project(bindings, project, "synthetic-store", 2)

    def test_conflicting_bindings_never_pick_a_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = str(Path(temporary).resolve())
            entry = {
                "directory": directory,
                "include_descendants": True,
                "store_id": "synthetic-store",
                "generation": 1,
                "record_ids": ["entity_alpha"],
                "entity_ids": ["entity_alpha"],
                "operator_id": "operator_fixture",
                "confirmed_at": "2026-01-01T00:00:00Z",
            }
            with self.assertRaises(ValueError):
                binding_for_project(
                    {"version": 1, "bindings": [entry, entry]},
                    Path(directory),
                    "synthetic-store",
                    1,
                )


class ExistingBrainSetupTests(unittest.TestCase):
    def test_global_preferences_require_reviewed_capture_before_setup_apply(self) -> None:
        request = self.request()
        self.assertIn(
            "include_preferences", request.__dataclass_fields__, "D3 preference setup missing"
        )
        request = replace(request, include_preferences=True)
        before = self.store_path.read_bytes()
        preview = preview_setup(request)
        proposal = cast(dict[str, object], preview["preference_scope"])
        self.assertEqual("capture_required", proposal["status"])
        self.assertIn(
            proposal["entity_id"],
            cast(list[str], cast(dict[str, object], preview["binding"])["entity_ids"]),
        )
        self.assertEqual(before, self.store_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "capture"):
            apply_setup(request, preview["fingerprint"])
        self.assertFalse((self.profile.parent / "project-bindings.json").exists())
        self.assertEqual(before, self.store_path.read_bytes())

    def test_reviewed_scope_can_be_explicitly_bound_without_graph_side_effects(self) -> None:
        request = replace(self.request(), include_preferences=True)
        preview = preview_setup(request)
        proposal = cast(dict[str, object], preview["preference_scope"])
        packet = {
            "source": {
                "kind": "test",
                "role": "verified",
                "title": "Owner approved preference scope",
                "observed_at": "2026-01-01T00:00:00Z",
                "source_tier": "explicit_user_statement",
            },
            "entities": [proposal["entity"]],
        }
        capture_request = CaptureRequest(
            input_path="-",
            input_bytes=json.dumps(packet).encode(),
            dry_run=True,
            consumer_id="setup_review",
            envelope=ProducerEnvelope(
                "agent_setup", "task_setup", None, "run_setup", "identity_assertion", 1
            ),
            authority_ceiling="explicit_user_statement",
            allow_replace=False,
            allow_redirect=False,
            expected_fingerprint=None,
            expected_global_watermark=None,
            plan_token=None,
            expected_revisions={},
            expected_absent=(),
        )
        with storage.open_store(self.store_path) as store:
            store.register_consumer("setup_review", agent_id="agent_setup", task_id="task_setup")
            dry = capture.execute_capture(store, capture_request)
            capture.execute_capture(
                store,
                replace(
                    capture_request,
                    dry_run=False,
                    expected_fingerprint=str(dry["fingerprint"]),
                    plan_token=str(dry["plan_token"]),
                ),
            )
            before = store.state()
        # The missing-scope preview is stale after capture; approving it cannot apply.
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_setup(request, preview["fingerprint"])
        ready = preview_setup(request)
        self.assertEqual("ready", cast(dict[str, object], ready["preference_scope"])["status"])
        apply_setup(request, ready["fingerprint"])
        session = session_binding(request, "pref-session", "pref-run")
        self.assertIn(
            proposal["entity_id"],
            cast(list[str], cast(dict[str, object], session["binding"])["entity_ids"]),
        )
        with storage.open_store(self.store_path, readonly=True) as store:
            self.assertEqual(before, store.state())

    def test_unknown_selector_cli_is_invalid_request_without_traceback(self) -> None:
        for argument in ("--record-id", "--entity-id"):
            with self.subTest(argument=argument):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(ROOT / "plugins/tedd/skills/tedd/scripts/tedd_setup.py"),
                        "--host",
                        "claude",
                        "--graph-config",
                        str(self.config),
                        "--project",
                        str(self.project),
                        "--profile",
                        str(self.profile),
                        argument,
                        "entity_missing",
                    ],
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(1, completed.returncode)
                self.assertEqual("invalid_request", json.loads(completed.stdout)["category"])
                self.assertEqual(b"", completed.stderr)
                self.assertFalse((self.profile.parent / "project-bindings.json").exists())

    def test_apply_rejects_owner_change_between_preview_and_baseline_read(self) -> None:
        real_preview = setup.preview_setup
        for component in ("bindings", "host"):
            with self.subTest(component=component):
                if component == "host" and shutil.which("codex") is None:
                    continue
                request = self.request("codex" if component == "host" else "claude")
                path = (
                    self.home / "config.toml"
                    if component == "host"
                    else self.profile.parent / "project-bindings.json"
                )
                self.assertFalse(path.exists())
                preview = preview_setup(request)
                updated = (
                    'model = "owner-new-choice"\n'
                    if component == "host"
                    else '{"version":1,"bindings":[]}\n'
                )

                def change_after_preview(
                    candidate: SetupRequest, changed_path: Path = path, content: str = updated
                ) -> dict[str, object]:
                    result = real_preview(candidate)
                    graph.atomic_write(changed_path, content)
                    return result

                with mock.patch.object(setup, "preview_setup", side_effect=change_after_preview):
                    with self.assertRaises(ValueError):
                        apply_setup(request, preview["fingerprint"])
                self.assertEqual(updated.encode(), path.read_bytes())

    def test_registered_session_without_active_scope_cannot_switch_approved_clients(self) -> None:
        request = self.request()
        apply_setup(request, preview_setup(request)["fingerprint"])
        other = self.root / "other-approved-project"
        graph.ensure_private_directory(other)
        other_request = replace(request, project=other)
        apply_setup(other_request, preview_setup(other_request)["fingerprint"])
        session = session_binding(request, "registered-session", "run")
        consumer_id, agent_id, task_id = (
            session[key] for key in ("consumer_id", "agent_id", "task_id")
        )
        assert (
            isinstance(consumer_id, str) and isinstance(agent_id, str) and isinstance(task_id, str)
        )
        with storage.open_store(self.store_path) as store:
            store.register_consumer(consumer_id, agent_id=agent_id, task_id=task_id)
        before = self.store_path.read_bytes()
        for candidate in (request, other_request):
            with self.subTest(project=candidate.project):
                with self.assertRaises(ValueError):
                    session_binding(candidate, "registered-session", "run")
        self.assertEqual(before, self.store_path.read_bytes())

    def test_existing_session_with_wrong_owner_cannot_use_approved_binding(self) -> None:
        request = self.request()
        apply_setup(request, preview_setup(request)["fingerprint"])
        with storage.open_store(self.store_path) as store:
            store.register_consumer("claude:session", agent_id="foreign-agent", task_id="session")
        with self.assertRaises(ValueError):
            session_binding(request, "session", "run")

    def test_duplicate_configured_graph_endpoint_is_refused_without_writing(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Native Codex CLI is unavailable")
        config = self.home / "config.toml"
        original = '[mcp_servers.other]\ncommand = "python"\nargs = ["private/tedd_graph_mcp.py"]\n'
        graph.atomic_write(config, original)
        with self.assertRaises(ValueError):
            preview_setup(self.request("codex"))
        self.assertEqual(original.encode(), config.read_bytes())

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("LOCALAPPDATA"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # Elevated Windows runners can give new directories an Administrators owner.
        set_private_path_mode(self.root, 0o700)
        graph.ensure_private_directory(self.root)
        self.config = self.root / "graph-config.json"
        self.store_path = self.root / "graph.sqlite3"
        with storage.create_store(self.store_path, plan_hmac_key=bytes.fromhex("12" * 32)) as store:
            applied(
                store, [change("entity_alpha", name="Alpha project")], fingerprint="setup-fixture"
            )
            self.assertTrue(store.validate(full=True)["valid"])
            self.state = store.state()
        graph.atomic_write(
            self.config,
            json.dumps(
                {
                    "schema_version": "v2",
                    "canonical_store_format": "sqlite_v4_wal",
                    "store_path": str(self.store_path),
                }
            ),
        )
        self.profile = self.root / "identity/profile.json"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "plugins/tedd/skills/tedd/scripts/tedd_profile.py"),
                "--profile",
                str(self.profile),
                "init",
                "--preferred-name",
                "Example Owner",
                "--display-name",
                "Example Owner",
                "--email",
                "owner@example.invalid",
            ],
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.home = self.root / "host"
        graph.ensure_private_directory(self.home)
        self.project = self.root / "project space é"
        graph.ensure_private_directory(self.project)

    def request(self, host: str = "claude") -> SetupRequest:
        found = shutil.which("codex")
        return SetupRequest(
            host,
            self.config,
            self.project,
            self.profile,
            self.home,
            Path(sys.executable),
            record_ids=("entity_alpha",),
            entity_ids=("entity_alpha",),
            host_executable=Path(found) if found else None,
        )

    def test_preview_changes_nothing_and_apply_is_fingerprint_bound_and_idempotent(self) -> None:
        request = self.request()
        before = self.config.read_bytes(), self.profile.read_bytes(), self.store_path.read_bytes()
        preview = preview_setup(request)
        self.assertFalse(preview["connected"])
        self.assertFalse((self.profile.parent / "project-bindings.json").exists())
        self.assertEqual(
            before,
            (self.config.read_bytes(), self.profile.read_bytes(), self.store_path.read_bytes()),
        )
        with self.assertRaises(ValueError):
            apply_setup(request, "0" * 64)
        applied_result = apply_setup(request, preview["fingerprint"])
        self.assertFalse(applied_result["connected"])
        saved = (self.profile.parent / "project-bindings.json").read_bytes()
        repeat = preview_setup(request)
        repeated_result = apply_setup(request, repeat["fingerprint"])
        self.assertEqual([], repeated_result["changed_components"])
        self.assertEqual(saved, (self.profile.parent / "project-bindings.json").read_bytes())
        self.assertEqual(
            before,
            (self.config.read_bytes(), self.profile.read_bytes(), self.store_path.read_bytes()),
        )

    def test_preserves_pinned_source_endpoint(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Native Codex CLI is unavailable for its configuration-writer contract")
        original = """model = "example-model"
[mcp_servers.tedd_sources]
command = "synthetic-python"
args = ["private/source.py", "--google-client", "private/client.json", "--credentials-file", "private/credentials.json", "--reader", "private/reader", "--renderer", "private/renderer", "--office-converter", "private/office", "--svg-helper", "private/svg", "--av1-helper", "private/av1"]
[hooks]
sentinel = "keep"
[plugins."tedd@tedd"]
enabled = false
"""
        config = self.home / "config.toml"
        graph.atomic_write(config, original)
        if sys.version_info >= (3, 11):
            import tomllib as toml
        else:
            import tomli as toml
        owner = toml.loads(original)
        request = self.request("codex")
        preview = preview_setup(request)
        self.assertEqual(original.encode(), config.read_bytes())
        apply_setup(request, preview["fingerprint"])
        parsed = toml.loads(config.read_text())
        self.assertEqual(
            owner["mcp_servers"]["tedd_sources"], parsed["mcp_servers"]["tedd_sources"]
        )
        for key in ("model", "hooks", "plugins"):
            self.assertEqual(owner[key], parsed[key])
        self.assertEqual(
            str(Path(sys.executable).absolute()), parsed["mcp_servers"]["tedd-graph"]["command"]
        )
        installed = config.read_bytes()
        apply_setup(request, preview_setup(request)["fingerprint"])
        self.assertEqual(installed, config.read_bytes())

    def test_config_drift_is_refused_before_apply(self) -> None:
        request = self.request()
        preview = preview_setup(request)
        graph.atomic_write(
            self.config,
            json.dumps(
                {
                    "schema_version": "v2",
                    "canonical_store_format": "sqlite_v4_wal",
                    "store_path": str(self.store_path),
                    "changed": True,
                }
            ),
        )
        with self.assertRaises(ValueError):
            apply_setup(request, preview["fingerprint"])
        self.assertFalse((self.profile.parent / "project-bindings.json").exists())

    def test_partial_apply_reports_attempted_components_and_keeps_backup(self) -> None:
        if shutil.which("codex") is None:
            self.skipTest("Native Codex CLI is unavailable")
        request = self.request("codex")
        preview = preview_setup(request)
        write = graph.atomic_write

        def fail_binding(path: Path, content: str, mode: int = 0o600) -> None:
            if path.name == "project-bindings.json":
                raise PermissionError("synthetic refusal")
            write(path, content, mode)

        with mock.patch.object(graph, "atomic_write", side_effect=fail_binding):
            with self.assertRaises(setup.SetupApplyError) as failure:
                apply_setup(request, preview["fingerprint"])
        self.assertEqual(
            ["host_configuration", "project_bindings"], failure.exception.changed_components
        )
        self.assertTrue((failure.exception.backup / "before.json").is_file())
        self.assertTrue((self.home / "config.toml").is_file())
        self.assertFalse((self.profile.parent / "project-bindings.json").exists())

    def test_session_reads_approval_and_requires_exact_owned_consumer_scope(self) -> None:
        request = self.request()
        apply_setup(request, preview_setup(request)["fingerprint"])
        before = self.store_path.read_bytes()
        session = session_binding(request, "synthetic-session", "synthetic-run")
        self.assertEqual("claude:synthetic-session", session["consumer_id"])
        self.assertEqual("not_registered", session["coordination"])
        self.assertEqual(before, self.store_path.read_bytes())
        consumer_id, agent_id, task_id = (
            session[key] for key in ("consumer_id", "agent_id", "task_id")
        )
        assert (
            isinstance(consumer_id, str) and isinstance(agent_id, str) and isinstance(task_id, str)
        )
        with storage.open_store(self.store_path) as store:
            store.register_consumer(consumer_id, agent_id=agent_id, task_id=task_id)
            store.subscribe(
                consumer_id,
                {"record_ids": ["entity_alpha"], "entity_ids": ["entity_alpha"]},
                subscription_id="approved-scope",
            )
        before = self.store_path.read_bytes()
        resume = session_binding(request, "synthetic-session", "second-run")
        self.assertEqual(["approved-scope"], resume["subscription_ids"])
        self.assertEqual(
            "not_registered", session_binding(request, "fork-session", "fork-run")["coordination"]
        )
        with self.assertRaises(ValueError):
            session_binding(request, "${CLAUDE_SESSION_ID}", "run")
        self.assertEqual(before, self.store_path.read_bytes())
        with storage.open_store(self.store_path) as store:
            store.subscribe(
                consumer_id,
                {"record_ids": ["entity_alpha"]},
                subscription_id="duplicate-scope",
            )
        with self.assertRaises(ValueError):
            session_binding(request, "synthetic-session", "run")

    def test_changed_directory_does_not_adopt_an_existing_session(self) -> None:
        request = self.request()
        apply_setup(request, preview_setup(request)["fingerprint"])
        other = self.root / "other-project"
        graph.ensure_private_directory(other)
        with self.assertRaises(ValueError):
            session_binding(replace(request, project=other), "synthetic-session", "run")


if __name__ == "__main__":
    unittest.main()
