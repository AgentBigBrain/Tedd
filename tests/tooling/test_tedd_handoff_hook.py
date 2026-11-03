"""Synthetic host events exercise reminders without transcripts or production state."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from unittest import mock

import knowledge_graph_v4 as v4
import knowledge_graph_v4_cli as cli

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.paths import GRAPH_SCRIPTS


class HandoffHookTests(HandoffFixture):
    # Reuse only synthetic capture builders, not the inherited test methods.
    def hook(self):
        path = GRAPH_SCRIPTS.parents[1] / "tedd/scripts/tedd_handoff_hook.py"
        self.assertTrue(path.is_file(), "host memory hooks are missing")
        sys.path.insert(0, str(path.parent))
        try:
            spec = importlib.util.spec_from_file_location("tedd_handoff_hook", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(path.parent))

    def event(self, name, prompt_id="prompt_alpha", **extra):
        return {
            "session_id": "task_alpha",
            "prompt_id": prompt_id,
            "hook_event_name": name,
            "cwd": str(self.root),
            **extra,
        }

    def bound(self):
        return {
            "consumer_id": "consumer_alpha",
            "agent_id": "agent_alpha",
            "task_id": "task_alpha",
            "coordination": "ready",
        }

    def test_hook_rejects_malformed_native_flags_before_local_access(self):
        hook = self.hook()
        with mock.patch.object(hook, "session_context") as context:
            with self.assertRaises(ValueError):
                hook.handle_event("claude", self.event("Stop", stop_hook_active="false"))
            context.assert_not_called()

    def test_native_resume_ignores_numeric_telemetry_without_receipt_coercion(self):
        hook = self.hook()
        payload = json.dumps(
            self.event("SessionStart", source="resume", estimated_cache_write_usd=0.0123)
        ).encode()
        output = io.StringIO()
        with (
            mock.patch.object(hook.sys, "stdin", mock.Mock(buffer=io.BytesIO(payload))),
            mock.patch.object(hook.sys, "stdout", output),
            mock.patch.object(hook, "session_context") as context,
        ):
            self.assertEqual(0, hook.main(["--host", "claude"]))
        self.assertEqual("", output.getvalue())
        context.assert_not_called()
        with self.assertRaises(ValueError):
            self.receipts.decode_receipt(b'{"generation": 1.5}')

    def test_hook_wire_refuses_invalid_used_fields_before_local_access(self):
        hook = self.hook()
        for changes in (
            {"source": 1.5},
            {"prompt_id": 1.5},
            {"stop_hook_active": 0},
            {"hook_event_name": []},
        ):
            with self.subTest(changes=changes):
                payload = json.dumps(self.event("UserPromptSubmit", **changes)).encode()
                output = io.StringIO()
                with (
                    mock.patch.object(hook.sys, "stdin", mock.Mock(buffer=io.BytesIO(payload))),
                    mock.patch.object(hook.sys, "stdout", output),
                    mock.patch.object(hook, "session_context") as context,
                ):
                    self.assertEqual(0, hook.main(["--host", "claude"]))
                self.assertIn("systemMessage", json.loads(output.getvalue()))
                context.assert_not_called()

    def test_native_json_encoding_cannot_bypass_the_depth_bound(self):
        hook = self.hook()
        prefix = json.dumps(self.event("SessionStart", source="resume", ignored='"'))[:-1]
        document = prefix + ',"nested":' + "[" * 1100 + "null" + "]" * 1100 + "}"
        for encoding in ("utf-8", "utf-16-le", "utf-32-le"):
            with self.subTest(encoding=encoding):
                output = io.StringIO()
                with (
                    mock.patch.object(
                        hook.sys, "stdin", mock.Mock(buffer=io.BytesIO(document.encode(encoding)))
                    ),
                    mock.patch.object(hook.sys, "stdout", output),
                    mock.patch.object(hook, "session_context") as context,
                ):
                    self.assertEqual(0, hook.main(["--host", "claude"]))
                self.assertIn("systemMessage", json.loads(output.getvalue()))
                context.assert_not_called()

    def test_existing_nonprivate_receipt_root_is_refused_not_repaired(self):
        hook = self.hook()
        root = self.root / "handoffs"
        root.mkdir(mode=0o700)
        with (
            mock.patch.object(
                hook, "session_context", return_value=(self.store_path, self.bound())
            ),
            mock.patch.object(self.receipts, "handoff_directory", return_value=root),
            mock.patch.object(hook, "mode_has_any", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "Handoff directory must be private"):
                hook.handle_event("claude", self.event("UserPromptSubmit"))
        self.assertEqual([], list(root.iterdir()))

    def test_packaged_windows_command_resolves_plugin_root_without_writes(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell hook launcher; POSIX uses its own command")
        plugin = GRAPH_SCRIPTS.parents[2]
        command = json.loads((plugin / "hooks/codex.json").read_text())["hooks"]["Stop"][0][
            "hooks"
        ][0]["commandWindows"]
        environment = {
            k: os.environ[k] for k in ("SystemRoot", "WINDIR", "PATH") if k in os.environ
        }
        environment.update(
            {
                "CLAUDE_PLUGIN_ROOT": str(plugin),
                "TEDD_NO_LOCAL_WRITES": "1",
                "USERPROFILE": str(self.root),
                "HOME": str(self.root),
            }
        )
        shell = os.path.join(
            environment["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        result = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            input=b"{}",
            env=environment,
            cwd=self.root,
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"", result.stdout)
        self.assertFalse((self.root / ".codex").exists())

    def test_packaged_windows_hook_receives_json_through_console_free_powershell(self):
        if os.name != "nt":
            self.skipTest("Native Windows PowerShell hook transport")
        plugin = GRAPH_SCRIPTS.parents[2]
        command = json.loads((plugin / "hooks/codex.json").read_text())["hooks"][
            "UserPromptSubmit"
        ][0]["hooks"][0]["commandWindows"]
        environment = {
            key: os.environ[key]
            for key in (
                "SystemRoot",
                "WINDIR",
                "PATH",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "SystemDrive",
                "ProgramData",
                "PATHEXT",
            )
            if key in os.environ
        }
        environment.update(
            HOME=str(self.root), USERPROFILE=str(self.root), CLAUDE_PLUGIN_ROOT=str(plugin)
        )
        shells = [
            os.path.join(
                environment["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
        ]
        if pwsh := shutil.which("pwsh"):
            shells.append(pwsh)
        # CreateProcess searches cwd before PATH for a bare executable name.
        # A harmless system executable must never replace the qualified launcher.
        shutil.copyfile(
            os.path.join(environment["SystemRoot"], "System32", "cmd.exe"),
            self.root / "py.exe",
        )
        cases = (
            (b'{"hook_event_name":[],"private":"never-echo-this"}', "systemMessage"),
            (
                json.dumps(
                    self.event("SessionStart", source="resume", ignored="é 中文", cost_usd=0.0123),
                    ensure_ascii=False,
                ).encode("utf-8"),
                None,
            ),
            (b"x" * (2 * 1024 * 1024 + 1), "exceeds its limit"),
        )
        for shell, (payload, expected) in ((shell, case) for shell in shells for case in cases):
            with self.subTest(shell=shell, expected=expected):
                result = subprocess.run(
                    [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                    input=payload,
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                if expected is None:
                    self.assertEqual(b"", result.stdout)
                else:
                    self.assertIn(expected, json.dumps(json.loads(result.stdout)))
                self.assertNotIn(b"never-echo-this", result.stdout + result.stderr)
                self.assertEqual(b"", result.stderr)
                self.assertFalse((self.root / ".codex").exists())

    def test_host_stop_blocks_once_and_accepts_a_journal_verified_handoff(self):
        hook = self.hook()
        with (
            mock.patch.object(
                hook, "session_context", return_value=(self.store_path, self.bound())
            ),
            mock.patch.object(
                self.receipts, "handoff_directory", return_value=self.root / "handoffs"
            ),
        ):
            self.assertEqual({}, hook.handle_event("claude", self.event("UserPromptSubmit")))
            self.assertEqual("block", hook.handle_event("claude", self.event("Stop"))["decision"])
            self.assertEqual({}, hook.handle_event("claude", self.event("Stop")))
            self.assertEqual(
                {}, hook.handle_event("claude", self.event("Stop", stop_hook_active=True))
            )
            with v4.open_store(self.store_path, plan_hmac_key=bytes.fromhex("11" * 32)) as store:
                self.apply(store, self.request("event_hook_saved"))
                receipt = self.receipts.reconstruct_receipt(store, self.identity)
                cli._write_handoff_file(self.receipts.receipt_path(receipt, self.identity), receipt)
            self.assertEqual({}, hook.handle_event("claude", self.event("Stop")))

    def test_reminder_is_once_and_clear_fork_reset_only_their_identity(self):
        hook = self.hook()
        with (
            mock.patch.object(
                hook, "session_context", return_value=(self.store_path, self.bound())
            ),
            mock.patch.object(
                self.receipts, "handoff_directory", return_value=self.root / "handoffs"
            ),
        ):
            for host in ("claude", "codex"):
                with self.subTest(host=host):
                    hook.handle_event(host, self.event("UserPromptSubmit", turn_id="prompt_alpha"))
                    next_event = self.event(
                        "UserPromptSubmit",
                        "prompt_beta",
                        turn_id="prompt_beta",
                        prompt="Don't save; handle the new request",
                    )
                    result = hook.handle_event(host, next_event)
                    self.assertIn("pending", result["hookSpecificOutput"]["additionalContext"])
                    self.assertEqual({}, hook.handle_event(host, next_event))
                    for source in ("resume", "compact"):
                        self.assertEqual(
                            {}, hook.handle_event(host, self.event("SessionStart", source=source))
                        )
                    for source in ("clear", "fork"):
                        hook.handle_event(host, self.event("SessionStart", source=source))
                        self.assertEqual(
                            {},
                            hook.handle_event(
                                host,
                                self.event(
                                    "UserPromptSubmit", "prompt_next", turn_id="prompt_next"
                                ),
                            ),
                        )

    def test_strict_mode_never_reads_or_writes_and_unused_session_is_silent(self):
        hook = self.hook()
        with (
            mock.patch.dict("os.environ", {"TEDD_NO_LOCAL_WRITES": "1"}),
            mock.patch.object(hook, "session_context") as context,
        ):
            for name in ("UserPromptSubmit", "Stop", "SessionStart"):
                self.assertEqual({}, hook.handle_event("claude", self.event(name)))
            context.assert_not_called()
        with (
            mock.patch.object(
                hook,
                "session_context",
                return_value=(self.store_path, {**self.bound(), "coordination": "not_registered"}),
            ),
            mock.patch.object(
                self.receipts, "handoff_directory", return_value=self.root / "handoffs"
            ),
        ):
            self.assertEqual({}, hook.handle_event("claude", self.event("Stop")))

    def test_codex_stop_warns_without_creating_a_continuation_prompt(self):
        hook = self.hook()
        with (
            mock.patch.object(
                hook, "session_context", return_value=(self.store_path, self.bound())
            ),
            mock.patch.object(
                self.receipts, "handoff_directory", return_value=self.root / "handoffs"
            ),
        ):
            hook.handle_event("codex", self.event("UserPromptSubmit", turn_id="prompt_alpha"))
            result = hook.handle_event("codex", self.event("Stop", turn_id="prompt_alpha"))
            self.assertNotIn("decision", result)
            self.assertIn("pending", result["systemMessage"])
            self.assertEqual(
                {}, hook.handle_event("codex", self.event("Stop", turn_id="prompt_alpha"))
            )

    def test_older_codex_uses_one_valid_utc_prompt_token_for_stop(self):
        hook = self.hook()
        with (
            mock.patch.object(
                hook, "session_context", return_value=(self.store_path, self.bound())
            ),
            mock.patch.object(
                self.receipts, "handoff_directory", return_value=self.root / "handoffs"
            ),
        ):
            self.assertEqual({}, hook.handle_event("codex", self.event("UserPromptSubmit")))
            state = cli._read_handoff_file(hook.session_path("codex", "task_alpha"))
            token = state["prompt_id"]
            self.assertTrue(token.endswith("Z"))
            self.assertEqual(token, self.receipts.validate_prompt_id(token))
            self.assertIn(
                "pending", hook.handle_event("codex", self.event("Stop"))["systemMessage"]
            )
            self.assertEqual(
                token, cli._read_handoff_file(hook.session_path("codex", "task_alpha"))["prompt_id"]
            )

    def test_unmocked_hook_entry_uses_private_binding_and_accepts_nothing_receipt(self):
        hook = self.hook()
        environment = {
            k: os.environ[k]
            for k in ("SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA")
            if k in os.environ
        }
        environment.update({"HOME": str(self.root), "USERPROFILE": str(self.root)})
        profile_script = GRAPH_SCRIPTS.parents[1] / "tedd/scripts/tedd_profile.py"
        init = subprocess.run(
            [
                sys.executable,
                "-B",
                str(profile_script),
                "init",
                "--preferred-name",
                "Example Owner",
                "--display-name",
                "Example Owner",
                "--email",
                "owner@example.invalid",
            ],
            env=environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, init.returncode, init.stderr)
        profile = json.loads((self.root / ".codex/tedd/profile.json").read_bytes())
        with v4.open_store(self.store_path) as store:
            before = store.state()
            store.register_consumer(
                "claude:task_alpha", agent_id=profile["agent_instance_id"], task_id="task_alpha"
            )
            store.subscribe(
                "claude:task_alpha",
                {"entity_ids": ["entity_project"]},
                subscription_id="scope_hook",
            )
        config = self.root / "graph-config.json"
        hook.setup.graph.atomic_write(
            config,
            json.dumps(
                {
                    "schema_version": "v2",
                    "canonical_store_format": "sqlite_v4_wal",
                    "store_path": str(self.store_path),
                }
            ),
        )
        binding = {
            "directory": str(self.root),
            "include_descendants": False,
            "store_id": before["store_id"],
            "generation": before["generation"],
            "record_ids": [],
            "entity_ids": ["entity_project"],
            "operator_id": profile["operators"][0]["operator_id"],
            "confirmed_at": "2026-01-01T12:00:00Z",
        }
        hook.setup.graph.atomic_write(
            self.root / ".codex/tedd/project-bindings.json",
            json.dumps({"version": 1, "bindings": [binding]}),
        )
        command = [
            sys.executable,
            "-B",
            str(profile_script.with_name("tedd_handoff_hook.py")),
            "--host",
            "claude",
            "--config",
            str(config),
        ]

        def run_event(name):
            result = subprocess.run(
                command,
                input=json.dumps(
                    self.event(
                        name,
                        prompt="private synthetic prompt",
                        transcript_path="never-open-this-file",
                    )
                ).encode(),
                env=environment,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)
            return json.loads(result.stdout) if result.stdout.strip() else {}

        self.assertEqual({}, run_event("UserPromptSubmit"))
        self.assertEqual("block", run_event("Stop")["decision"])
        nothing = subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "knowledge_graph.py"),
                "--config",
                str(config),
                "handoff",
                "--nothing",
                "--reason",
                "Nothing durable to save",
                "--session",
                "task_alpha",
                "--prompt",
                "prompt_alpha",
                "--consumer-id",
                "claude:task_alpha",
                "--producer-agent-id",
                profile["agent_instance_id"],
            ],
            env=environment,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(0, nothing.returncode, nothing.stderr)
        self.assertIn(b"agent_asserted", nothing.stdout)
        self.assertEqual({}, run_event("Stop"))
        for path in (self.root / ".codex/tedd/handoffs").rglob("*.json"):
            self.assertNotIn("private synthetic prompt", path.read_text())
            self.assertNotIn("never-open-this-file", path.read_text())
        with v4.open_store(self.store_path, readonly=True) as store:
            self.assertEqual(before, store.state())
            self.assertTrue(store.validate(full=True)["valid"])
