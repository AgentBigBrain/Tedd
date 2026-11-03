"""Temporary hosted-runner diagnosis; synthetic bytes, no graph or launcher."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.verify_release import isolated_test_environment  # noqa: E402


def communicate(
    process: subprocess.Popen[bytes], payload: bytes, outcomes: list[tuple[bytes, bytes]]
) -> None:
    try:
        outcomes.append(process.communicate(payload))
    except Exception as error:
        outcomes.append((b"", type(error).__name__.encode()))


def main() -> None:
    adapter = (ROOT / "plugins/tedd/hooks/codex_handoff.ps1").read_text(encoding="utf-8")
    body = adapter[adapter.index("$teddInput =") : adapter.index("    $teddScript =")]
    markers = (
        ("$teddInput =", "Mark 'before-open'\n$teddInput ="),
        ("$teddBuffer =", "Mark 'after-open'\n$teddBuffer ="),
        ("$teddCount = 0", "Mark 'buffer-ready'\n$teddCount = 0"),
        ("        $teddRead =", "        Mark ('before-read:' + $teddCount)\n        $teddRead ="),
        (
            "        if ($teddRead",
            "        Mark ('after-read:' + $teddRead)\n        if ($teddRead",
        ),
        ("        exit 0", "        Mark 'limit-exit'\n        exit 0"),
    )
    for old, new in markers:
        assert body.count(old) == 1
        body = body.replace(old, new)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        'function Mark($value) { [IO.File]::AppendAllText($env:TEDD_PROBE_LOG, $value + "`n") }\n'
        "Mark 'entry'\n" + body + "\nMark 'eof'\n} finally { $teddInput.Dispose(); Mark 'exit' }\n"
    )
    keys = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "SystemDrive", "ProgramData", "USERPROFILE")
    inherited = {key: os.environ[key] for key in keys if key in os.environ}
    with isolated_test_environment(inherited) as environment:
        with tempfile.TemporaryDirectory(dir=environment["TEMP"]) as temporary:
            root = Path(temporary).resolve()
            probe = root / "probe.ps1"
            probe.write_text(script, encoding="utf-8")
            shell = str(
                Path(environment["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            cases = [
                ("winps-command-small", shell, False, b"{}"),
                ("winps-command-limit", shell, False, b"x" * (2 * 1024 * 1024 + 1)),
                ("winps-file-small", shell, True, b"{}"),
            ]
            if pwsh := shutil.which("pwsh"):
                cases.append(("pwsh-command-small", pwsh, False, b"{}"))
            for label, executable, as_file, payload in cases:
                log = root / (label + ".txt")
                env = dict(
                    environment, HOME=str(root), USERPROFILE=str(root), TEDD_PROBE_LOG=str(log)
                )
                invocation = (
                    ["-File", str(probe)] if as_file else ["-Command", '& "' + str(probe) + '"']
                )
                process = subprocess.Popen(
                    [executable, "-NoLogo", "-NoProfile", "-NonInteractive", *invocation],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=root,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                outcomes: list[tuple[bytes, bytes]] = []
                worker = threading.Thread(
                    target=communicate, args=(process, payload, outcomes), daemon=True
                )
                worker.start()
                worker.join(20)
                timed_out = worker.is_alive()
                code_at_deadline = process.poll()
                if timed_out and code_at_deadline is None:
                    process.kill()
                worker.join(3)
                print(
                    json.dumps(
                        {
                            "probe": label,
                            "timed_out": timed_out,
                            "returncode_at_deadline": code_at_deadline,
                            "collector_finished": not worker.is_alive(),
                            "markers": log.read_text(encoding="utf-8")[-16000:]
                            if log.exists()
                            else "",
                            "stderr": outcomes[0][1].decode("utf-8", errors="replace")[-2000:]
                            if outcomes
                            else "",
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
