"""Coordinator tests using only a disposable synthetic Docker executable."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from unittest import mock

from platform_support import owner_is_current_user
from tedd_knowledge_graph import source_reader_client as reader_client
from tedd_knowledge_graph import source_reader_host as reader_host
from tedd_knowledge_graph.source_contracts import SourceArchiveError, encode
from tedd_knowledge_graph.source_reader_cold import (
    MacColdEntitlement,
    ProfileOwners,
    profile_locked,
)
from tedd_knowledge_graph.source_reader_contracts import ReaderRequest
from tedd_knowledge_graph.source_reader_host import ReaderCoordinator
from tedd_knowledge_graph.source_reader_jobs import ReaderJobs
from tedd_knowledge_graph.source_reader_policy import ReaderRuntime, create_arguments
from tedd_knowledge_graph.source_reader_runtime import MacRuntimeBinding

from tests.knowledge_graph.test_source_reader_policy import (
    CONTAINER_ID,
    DAEMON_ID,
    IMAGE_ID,
    JOB_ID,
    OWNER_ID,
    inspected_container,
    inspected_exited_container,
)

_PDF = b"%PDF-1.4\nsynthetic reader fixture"
_AF_UNIX = cast(int, socket.__dict__.get("AF_UNIX", -1))
_MAX_MACOS_UNIX_SOCKET_PATH_BYTES = 103
_FAKE_DOCKER = r"""#!__PYTHON__
import json, os, pathlib, sys, time

STATE = pathlib.Path(__STATE__)

def load():
    return json.loads(STATE.read_text())

def save(state):
    temporary = STATE.with_suffix(".new")
    temporary.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    os.replace(temporary, STATE)

state = load()
args = sys.argv[1:]
state["commands"].append(args)
state["environments"].append(sorted(os.environ))
save(state)
if args[:2] != ["--config", state["config"]] or args[2:4] != ["--host", state["host"]]:
    raise SystemExit(91)
command = args[4:]
phase = " ".join(command[:2])
if phase == "container start" and state.get("mutate_binary_on_start"):
    state["mutating_pid"] = os.getpid()
    save(state)
    os.utime(sys.argv[0], None)
    time.sleep(30)
delay = state.get("delays", {}).get(phase, 0)
if delay:
    time.sleep(delay)
if command[:2] == ["system", "info"]:
    if state.get("daemon_failure"):
        raise SystemExit(1)
    print(json.dumps(state.get("reported_daemon", state["daemon_id"])))
elif command[:2] == ["image", "inspect"]:
    if state.get("image_failure"):
        raise SystemExit(1)
    print(json.dumps([{"Id": state["image_id"], "Os": "linux", "Architecture": "arm64"}]))
elif command[:2] == ["container", "create"]:
    if state.get("create_failure"):
        raise SystemExit(1)
    state["exists"] = True
    state["document"][0]["State"] = {
        "Status": "created", "Running": False, "Paused": False,
        "Restarting": False, "Dead": False,
    }
    save(state)
    if state.get("oversized_create"):
        sys.stdout.write("c" * (2 * 1024 * 1024))
    else:
        print(state["container_id"])
elif command[:2] == ["container", "ls"]:
    if state.get("query_failure"):
        raise SystemExit(1)
    filter_value = command[command.index("--filter") + 1]
    if filter_value.startswith("id="):
        if state.get("id_query_failure"):
            raise SystemExit(1)
        if state.get("exists"):
            print(state["container_id"])
    elif state.get("ambiguous_query"):
        print(state["container_id"])
        print("d" * 64)
    elif state.get("exists") and not state.get("renamed"):
        print(state.get("query_id", state["container_id"]))
elif command[:2] == ["container", "inspect"]:
    if not state.get("exists") or state.get("inspect_failure"):
        raise SystemExit(1)
    print(json.dumps(state["document"], separators=(",", ":"), sort_keys=True))
elif command[:2] == ["container", "start"]:
    state["start_pid"] = os.getpid()
    state["document"][0]["State"] = {
        "Status": "running", "Running": True, "Paused": False,
        "Restarting": False, "Dead": False,
    }
    save(state)
    upload_path = state.get("upload_path")
    if upload_path:
        upload_marker = pathlib.Path(upload_path)
        upload_temporary = upload_marker.with_name(upload_marker.name + ".new")
        payload = bytearray()
        while True:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                break
            payload.extend(chunk)
            upload_temporary.write_text(str(len(payload)))
            os.replace(upload_temporary, upload_marker)
            time.sleep(state.get("upload_delay", 0))
        payload = bytes(payload)
    else:
        payload = sys.stdin.buffer.read()
    state = load()
    if state.get("start_crash"):
        raise SystemExit(17)
    if state.get("oversized_output"):
        sys.stdout.buffer.write(b"x" * (33 * 1024 * 1024))
        sys.stdout.buffer.flush()
        state["drained"] = True
    else:
        sys.stdout.buffer.write(state["response"].encode())
        sys.stdout.buffer.flush()
    if state.get("after_output_delay"):
        state["output_written"] = True
        save(state)
        time.sleep(state["after_output_delay"])
        state = load()
    state["document"][0]["State"] = {
        "Status": "exited", "Running": False, "Paused": False,
        "Restarting": False, "Dead": False,
        "ExitCode": state.get("worker_exit_code", 0),
    }
    state["received_sha256"] = __import__("hashlib").sha256(payload).hexdigest()
    save(state)
    raise SystemExit(state.get("worker_exit_code", 0))
elif command[:2] == ["container", "rm"]:
    if state.get("remove_failure"):
        raise SystemExit(1)
    state["exists"] = False
    save(state)
    if state.get("remove_response_failure"):
        raise SystemExit(1)
else:
    raise SystemExit(92)
"""


def _pdf_response(payload: bytes) -> dict[str, object]:
    return {
        "status": "text_screened",
        "original_sha256": hashlib.sha256(payload).hexdigest(),
        "reader_id": "synthetic-reader:1",
        "page_count": 1,
        "units": [
            {
                "anchor_id": "u1",
                "text": "synthetic text",
                "position": 1,
                "kind": "page",
                "source_id": None,
                "speaker": None,
                "source_time": None,
                "reply_to": None,
                "links": [],
            }
        ],
        "resources": "bounded",
        "visual_review_required": True,
    }


def _is_private_fixture_parent(path: Path) -> bool:
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISDIR(info.st_mode)
            or not owner_is_current_user(parent, allow_root=True)
            or info.st_mode & 0o022
        ):
            return False
    return True


def _short_private_fixture_root() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    default_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    home = Path.home().resolve(strict=True)
    visited: set[Path] = set()
    for start in (default_parent, home):
        for parent in (start, *start.parents):
            if parent in visited:
                continue
            visited.add(parent)
            if parent == Path(parent.anchor) or not _is_private_fixture_parent(parent):
                continue
            try:
                temporary = tempfile.TemporaryDirectory(prefix=".tr-", dir=parent)
            except OSError:
                continue
            root = Path(temporary.name).resolve(strict=True)
            socket_path = root / "s"
            if len(os.fsencode(socket_path)) <= _MAX_MACOS_UNIX_SOCKET_PATH_BYTES:
                return temporary, root
            temporary.cleanup()
    raise RuntimeError("No private temporary parent supports a bounded AF_UNIX path")


class FakeDocker:
    def __init__(self) -> None:
        listener = socket.socket(_AF_UNIX)
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary, root = _short_private_fixture_root()
            os.chmod(root, 0o700)
            self.state_root = root / "state"
            self.state_root.mkdir(mode=0o700)
            self.socket = root / "s"
            listener.bind(str(self.socket))
            os.chmod(self.socket, 0o600)
            binary_root = root / "bin"
            binary_root.mkdir(mode=0o700)
            self.docker = binary_root / "docker"
            self.state_path = root / "fake-state.json"
            script = _FAKE_DOCKER.replace("__PYTHON__", sys.executable).replace(
                "__STATE__", repr(str(self.state_path))
            )
            self.docker.write_text(script)
            self.docker.chmod(0o700)
            self.runtime = ReaderRuntime(
                self.docker,
                self.socket,
                self.state_root,
                OWNER_ID,
                DAEMON_ID,
                IMAGE_ID,
            )
            document = copy.deepcopy(inspected_container())
            document[0]["Args"] = [
                "-I",
                "-B",
                "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_reader_guest.py",
                "pdf-text",
            ]
            cast(dict[str, object], document[0]["Config"])["Cmd"] = document[0]["Args"]
            self.write(
                {
                    "commands": [],
                    "environments": [],
                    "config": str(self.state_root / "docker-config"),
                    "host": "unix://" + str(self.socket),
                    "daemon_id": DAEMON_ID,
                    "image_id": IMAGE_ID,
                    "container_id": CONTAINER_ID,
                    "exists": False,
                    "document": document,
                    "response": json.dumps(_pdf_response(_PDF), separators=(",", ":")),
                    "delays": {},
                }
            )
        except BaseException:
            listener.close()
            if temporary is not None:
                temporary.cleanup()
            raise
        self.listener = listener
        self.temporary = temporary

    def close(self) -> None:
        self.listener.close()
        self.temporary.cleanup()

    def read(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.state_path.read_text()))

    def write(self, state: dict[str, object]) -> None:
        self.state_path.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))

    def update(self, **changes: object) -> None:
        state = self.read()
        state.update(changes)
        self.write(state)


class UnitMacRuntimeBinding:
    """Unit double that records qualification without claiming live Lima evidence."""

    def __init__(self, runtime: ReaderRuntime, *, fail_call: int | None = None) -> None:
        self.runtime = runtime
        self.fail_call = fail_call
        self.profile: str | None = None
        self.calls: list[tuple[float, threading.Event | None]] = []

    def qualify(self, deadline: float, cancelled: threading.Event | None = None) -> None:
        self.calls.append((deadline, cancelled))
        if len(self.calls) == self.fail_call:
            raise SourceArchiveError("source_unavailable")
        if time.monotonic() >= deadline or cancelled is not None and cancelled.is_set():
            raise SourceArchiveError("source_unavailable")


class UnitColdEntitlement:
    """Host-boundary double; the cold module owns its own artifact validation tests."""

    def __init__(self, authority: ProfileOwners) -> None:
        self.runtime = authority.runtime
        self.profile = authority.profile
        self.authority = authority
        self.session = UnitMacRuntimeBinding(authority.runtime)
        self.ready_deadlines: list[float] = []
        self.reconcile_deadlines: list[float] = []
        self.snapshots = 0
        self.running = True
        self.on_ready: Callable[[], None] | None = None
        self.on_reconcile: Callable[[], None] | None = None
        self.snapshot = object()

    def profile_authority(self) -> ProfileOwners:
        return self.authority

    def ensure_ready(
        self, deadline: float, cancelled: threading.Event | None
    ) -> UnitMacRuntimeBinding:
        self.ready_deadlines.append(deadline)
        self.authority.preflight_jobs(deadline, cancelled, require_all_idle=True)
        if self.on_ready is not None:
            self.on_ready()
        return self.session

    def reconcile_start(self, deadline: float, cancelled: threading.Event | None) -> None:
        self.reconcile_deadlines.append(deadline)
        if self.on_reconcile is not None:
            self.on_reconcile()

    def _snapshot(self, deadline: float, cancelled: threading.Event | None) -> object:
        self.snapshots += 1
        if not self.running or time.monotonic() >= deadline or cancelled and cancelled.is_set():
            raise SourceArchiveError("source_unavailable")
        return self.snapshot


@unittest.skipIf(os.name == "nt", "Unix-socket reader transport is macOS-specific")
class FakeDockerFixtureTests(unittest.TestCase):
    def test_long_private_tmpdir_still_uses_a_bounded_absolute_socket_path(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            long_tmpdir = Path(outer) / ("nested-" + "x" * 96)
            long_tmpdir.mkdir(mode=0o700)
            with mock.patch.object(tempfile, "tempdir", str(long_tmpdir)):
                fake = FakeDocker()
            try:
                self.assertTrue(fake.socket.is_absolute())
                self.assertLessEqual(
                    len(os.fsencode(fake.socket)), _MAX_MACOS_UNIX_SOCKET_PATH_BYTES
                )
                self.assertTrue(fake.socket.is_socket())
            finally:
                fake.close()

    def test_initialization_failure_closes_listener_and_removes_fixture_root(self) -> None:
        roots: list[Path] = []
        listeners: list[socket.socket] = []
        original_root = _short_private_fixture_root
        original_socket = socket.socket

        def tracked_root() -> tuple[tempfile.TemporaryDirectory[str], Path]:
            temporary, root = original_root()
            roots.append(root)
            return temporary, root

        def tracked_socket(family: int) -> socket.socket:
            listener = original_socket(family)
            listeners.append(listener)
            return listener

        with (
            mock.patch(f"{__name__}._short_private_fixture_root", side_effect=tracked_root),
            mock.patch.object(socket, "socket", side_effect=tracked_socket),
            mock.patch.object(Path, "write_text", side_effect=OSError("synthetic failure")),
            self.assertRaisesRegex(OSError, "synthetic failure"),
        ):
            FakeDocker()

        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        self.assertEqual(len(listeners), 1)
        self.assertEqual(listeners[0].fileno(), -1)


@unittest.skipIf(os.name == "nt", "Unix-socket reader transport is macOS-specific")
class ReaderCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDocker()
        self.addCleanup(self.fake.close)

    def qualified(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch(
                "tedd_knowledge_graph.source_reader_host.uuid.uuid4",
                return_value=__import__("uuid").UUID(JOB_ID),
            )
        )
        return stack

    def coordinator(self, fake: FakeDocker | None = None) -> ReaderCoordinator:
        selected = self.fake if fake is None else fake
        binding = UnitMacRuntimeBinding(selected.runtime)
        return ReaderCoordinator(
            selected.runtime,
            binding=cast(MacRuntimeBinding, binding),
        )

    def profile_authority(self) -> ProfileOwners:
        runtime = self.fake.runtime
        root = self.fake.state_root.parent
        profile = "colima-tedd-reader-spike"
        profile_root = root / "tedd-reader-spike"
        profile_root.mkdir(mode=0o700)
        generation = "11111111-1111-4111-8111-111111111111"
        roster = encode(
            {
                "profile": profile,
                "version": 1,
                "cutover_generation": generation,
                "owners": [
                    {
                        "docker": str(runtime.docker),
                        "socket": str(runtime.socket),
                        "state_root": str(runtime.state_root),
                        "owner_id": runtime.owner_id,
                        "daemon_id": runtime.daemon_id,
                        "image_id": runtime.image_id,
                    }
                ],
            }
        )
        (profile_root / "profile-owners.json").write_bytes(roster)
        (profile_root / "profile-owners.json").chmod(0o600)
        (profile_root / "profile-start.lock").write_bytes(b"")
        (profile_root / "profile-start.lock").chmod(0o600)
        ReaderJobs(runtime)
        return ProfileOwners(
            profile_root,
            root,
            profile,
            runtime,
            hashlib.sha256(roster).hexdigest(),
            "a" * 64,
            generation,
            (runtime,),
        )

    def profile_receipt(self, authority: ProfileOwners, *, phase: str = "idle") -> None:
        runtime = self.fake.runtime
        receipt = {
            "version": 1,
            "profile": authority.profile,
            "roster_sha256": authority.roster_sha256,
            "cold_entitlement_sha256": authority.cold_entitlement_sha256,
            "owner_id": runtime.owner_id,
            "state_root": str(runtime.state_root),
            "daemon_id": runtime.daemon_id,
            "image_id": runtime.image_id,
            "phase": phase,
            "attempt": None if phase == "idle" else "22222222-2222-4222-8222-222222222222",
            "cutover_generation": authority.cutover_generation,
        }
        path = authority.profile_root / "profile-start.json"
        path.write_bytes(encode(receipt))
        path.chmod(0o600)

    def cold_coordinator(self) -> tuple[ReaderCoordinator, UnitColdEntitlement]:
        authority = self.profile_authority()
        self.profile_receipt(authority)
        cold = UnitColdEntitlement(authority)
        coordinator = ReaderCoordinator(
            self.fake.runtime,
            cold=cast(MacColdEntitlement, cold),
            profile_owners=authority,
        )
        return coordinator, cold

    def test_cold_read_uses_reviewed_owner_and_one_deadline(self) -> None:
        coordinator, cold = self.cold_coordinator()

        with self.qualified():
            result = coordinator.read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertEqual(len(cold.ready_deadlines), 1)
        self.assertEqual(cold.reconcile_deadlines, cold.ready_deadlines)
        self.assertGreaterEqual(len(cold.session.calls), 2)
        self.assertEqual(
            {deadline for deadline, _ in cold.session.calls}, set(cold.ready_deadlines)
        )
        self.assertEqual(
            json.loads((self.fake.state_root / "reader-job.json").read_text())["phase"], "idle"
        )

    def test_cold_binding_requires_matching_authority_and_excludes_warm_binding(self) -> None:
        authority = self.profile_authority()
        cold = UnitColdEntitlement(authority)
        with self.assertRaises(SourceArchiveError):
            ReaderCoordinator(self.fake.runtime, cold=cast(MacColdEntitlement, cold))
        with self.assertRaises(SourceArchiveError):
            ReaderCoordinator(
                self.fake.runtime,
                cold=cast(MacColdEntitlement, cold),
                binding=cast(MacRuntimeBinding, UnitMacRuntimeBinding(self.fake.runtime)),
                profile_owners=authority,
            )
        self.assertEqual(self.fake.read()["commands"], [])

    def test_cold_reconciles_starting_receipt_before_docker_access(self) -> None:
        coordinator, cold = self.cold_coordinator()
        self.profile_receipt(cold.authority, phase="starting")
        with self.assertRaises(SourceArchiveError):
            coordinator.read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(self.fake.read()["commands"], [])
        cold.on_reconcile = lambda: self.profile_receipt(cold.authority)

        with self.qualified():
            result = coordinator.read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertEqual(len(cold.reconcile_deadlines), 2)
        self.assertEqual(cold.reconcile_deadlines[-1], cold.ready_deadlines[-1])

    def test_cold_start_consumes_the_original_request_budget(self) -> None:
        coordinator, cold = self.cold_coordinator()

        # Charge reservation and readiness explicitly, independently of preflight I/O.
        with mock.patch.object(time, "monotonic", autospec=True, return_value=100.0) as clock:

            def during_reservation() -> None:
                clock.return_value = 100.02

            def during_start() -> None:
                clock.return_value = 100.06

            cold.on_reconcile = during_reservation
            cold.on_ready = during_start
            with (
                mock.patch.object(reader_host, "_REQUEST_SECONDS", 0.05),
                self.assertRaises(SourceArchiveError) as raised,
            ):
                coordinator.read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertEqual(len(cold.ready_deadlines), 1)
        self.assertEqual(cold.reconcile_deadlines, [100.05])
        self.assertEqual(cold.ready_deadlines, cold.reconcile_deadlines)
        self.assertEqual(cold.session.calls, [(100.05, None)])
        self.assertEqual(self.fake.read()["commands"], [])

    def test_cold_start_holds_the_profile_reservation(self) -> None:
        coordinator, cold = self.cold_coordinator()
        contender: list[str] = []

        def inspect_reservation() -> None:
            try:
                with profile_locked(
                    cold.authority.profile_root,
                    cold.authority.profile,
                    time.monotonic() + 0.1,
                    None,
                ):
                    contender.append("acquired")
            except SourceArchiveError:
                contender.append("blocked")

        def during_start() -> None:
            thread = threading.Thread(target=inspect_reservation)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())

        cold.on_ready = during_start
        with self.qualified():
            self.assertEqual(coordinator.read(ReaderRequest("pdf-text"), _PDF), _pdf_response(_PDF))
        self.assertEqual(contender, ["blocked"])

    def test_cancellation_during_cold_start_never_submits_payload(self) -> None:
        coordinator, cold = self.cold_coordinator()
        cancelled = threading.Event()
        cold.on_ready = cancelled.set

        with self.assertRaises(SourceArchiveError) as raised:
            coordinator.read(ReaderRequest("pdf-text"), _PDF, cancelled)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertEqual(self.fake.read()["commands"], [])

    def test_cold_recover_never_starts_a_stopped_profile(self) -> None:
        coordinator, cold = self.cold_coordinator()
        cold.running = False

        with self.assertRaises(SourceArchiveError):
            coordinator.recover()

        self.assertEqual(cold.ready_deadlines, [])
        self.assertEqual(self.fake.read()["commands"], [])

    def test_cold_read_recovers_selected_pending_job_before_start(self) -> None:
        coordinator, cold = self.cold_coordinator()
        jobs = ReaderJobs(self.fake.runtime)
        with jobs.locked(time.monotonic() + 2):
            jobs.pending(JOB_ID)

        with self.qualified():
            result = coordinator.read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertGreater(cold.snapshots, 0)
        self.assertEqual(len(cold.ready_deadlines), 1)
        self.assertEqual(
            json.loads((self.fake.state_root / "reader-job.json").read_text())["phase"], "idle"
        )

    def test_failed_create_gets_no_extra_cleanup_budget(self) -> None:
        self.fake.update(create_failure=True)
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        cleanup_deadlines: list[float] = []
        original = reader_host._cleanup_receipt

        def record_cleanup(
            client: reader_client._RuntimeClient,
            jobs: ReaderJobs,
            receipt: dict[str, object],
            deadline: float,
        ) -> None:
            cleanup_deadlines.append(deadline)
            original(client, jobs, receipt, deadline)

        with (
            self.qualified(),
            mock.patch.object(reader_host, "_CLEANUP_SECONDS", 60.0),
            mock.patch.object(reader_host, "_cleanup_receipt", new=record_cleanup),
            self.assertRaises(SourceArchiveError),
        ):
            ReaderCoordinator(self.fake.runtime, binding=cast(MacRuntimeBinding, binding)).read(
                ReaderRequest("pdf-text"), _PDF
            )

        self.assertEqual(len(cleanup_deadlines), 1)
        self.assertLessEqual(cleanup_deadlines[0], binding.calls[0][0])
        self.assertEqual(
            json.loads((self.fake.state_root / "reader-job.json").read_text())["phase"], "idle"
        )

    def test_exact_created_job_gets_extra_cleanup_budget(self) -> None:
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        cleanup_deadlines: list[float] = []
        original = reader_host._cleanup_receipt

        def record_cleanup(
            client: reader_client._RuntimeClient,
            jobs: ReaderJobs,
            receipt: dict[str, object],
            deadline: float,
        ) -> None:
            cleanup_deadlines.append(deadline)
            original(client, jobs, receipt, deadline)

        with (
            self.qualified(),
            mock.patch.object(reader_host, "_REQUEST_SECONDS", 3.0),
            mock.patch.object(reader_host, "_cleanup_receipt", new=record_cleanup),
        ):
            result = ReaderCoordinator(
                self.fake.runtime, binding=cast(MacRuntimeBinding, binding)
            ).read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertEqual(len(cleanup_deadlines), 1)
        self.assertGreater(cleanup_deadlines[0], binding.calls[0][0])

    def test_dedicated_warm_binding_requires_reviewed_profile_authority(self) -> None:
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        binding.profile = "colima-tedd-reader-spike"
        with self.assertRaises(SourceArchiveError):
            ReaderCoordinator(self.fake.runtime, binding=cast(MacRuntimeBinding, binding))
        self.assertEqual(binding.calls, [])
        self.assertEqual(self.fake.read()["commands"], [])

    def test_profile_authority_cannot_be_attached_to_another_binding(self) -> None:
        authority = self.profile_authority()
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        binding.profile = "colima-unrelated"
        with self.assertRaises(SourceArchiveError):
            ReaderCoordinator(
                self.fake.runtime,
                binding=cast(MacRuntimeBinding, binding),
                profile_owners=authority,
            )
        self.assertEqual(binding.calls, [])

    def test_warm_read_checks_start_receipt_before_binding_or_docker(self) -> None:
        authority = self.profile_authority()
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        binding.profile = "colima-tedd-reader-spike"
        coordinator = ReaderCoordinator(
            self.fake.runtime, binding=cast(MacRuntimeBinding, binding), profile_owners=authority
        )
        with self.assertRaises(SourceArchiveError):
            coordinator.read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(binding.calls, [])
        self.assertEqual(self.fake.read()["commands"], [])
        self.profile_receipt(authority, phase="starting")
        with self.assertRaises(SourceArchiveError):
            coordinator.recover()
        self.assertEqual(binding.calls, [])
        self.assertEqual(self.fake.read()["commands"], [])
        self.profile_receipt(authority)
        with self.qualified():
            result = coordinator.read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(result, _pdf_response(_PDF))

    def test_warm_read_refuses_corrupt_or_mismatched_start_receipt(self) -> None:
        authority = self.profile_authority()
        binding = UnitMacRuntimeBinding(self.fake.runtime)
        binding.profile = authority.profile
        coordinator = ReaderCoordinator(
            self.fake.runtime, binding=cast(MacRuntimeBinding, binding), profile_owners=authority
        )
        self.profile_receipt(authority)
        path = authority.profile_root / "profile-start.json"
        original = cast(dict[str, object], json.loads(path.read_bytes()))
        for payload in (
            b"not json",
            encode({**original, "cold_entitlement_sha256": "b" * 64}),
            encode({**original, "image_id": "sha256:" + "b" * 64}),
            encode({**original, "cutover_generation": "22222222-2222-4222-8222-222222222222"}),
        ):
            with self.subTest(payload=payload[:30]):
                path.write_bytes(payload)
                with self.assertRaises(SourceArchiveError):
                    coordinator.read(ReaderRequest("pdf-text"), _PDF)
                self.assertEqual(binding.calls, [])
                self.assertEqual(self.fake.read()["commands"], [])

    def test_normal_construction_is_inert_and_read_is_unavailable_before_runtime_use(self) -> None:
        missing = self.fake.state_root / "missing"
        runtime = ReaderRuntime(
            missing / "docker",
            missing / "socket",
            missing / "state",
            OWNER_ID,
            DAEMON_ID,
            IMAGE_ID,
        )
        coordinator = ReaderCoordinator(runtime)

        with self.assertRaises(SourceArchiveError) as raised:
            coordinator.read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "capability_unavailable")
        self.assertFalse(missing.exists())

    def test_bound_runtime_uses_one_shared_deadline_and_rechecks_before_return(self) -> None:
        binding = UnitMacRuntimeBinding(self.fake.runtime)

        with self.qualified():
            result = ReaderCoordinator(
                self.fake.runtime, binding=cast(MacRuntimeBinding, binding)
            ).read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertGreaterEqual(len(binding.calls), 3)
        self.assertEqual(len({deadline for deadline, _ in binding.calls}), 1)

    def test_requalification_failure_after_bind_cleans_without_sending_input(self) -> None:
        binding = UnitMacRuntimeBinding(self.fake.runtime, fail_call=2)

        with (
            self.qualified(),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            ReaderCoordinator(self.fake.runtime, binding=cast(MacRuntimeBinding, binding)).read(
                ReaderRequest("pdf-text"), _PDF
            )

        self.assertEqual(raised.exception.code, "source_unavailable")
        state = self.fake.read()
        self.assertFalse(state["exists"])
        self.assertNotIn("received_sha256", state)
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")

    def test_success_uses_exact_endpoint_sanitized_environment_and_clears_receipt(self) -> None:
        ambient = "TEDD_FAKE_AMBIENT_SECRET"
        os.environ[ambient] = "must-not-cross"
        try:
            with self.qualified():
                result = self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        finally:
            os.environ.pop(ambient, None)

        self.assertEqual(result, _pdf_response(_PDF))
        state = self.fake.read()
        self.assertEqual(state["received_sha256"], hashlib.sha256(_PDF).hexdigest())
        self.assertFalse(state["exists"])
        environments = cast(list[list[str]], state["environments"])
        self.assertTrue(all(ambient not in environment for environment in environments))
        for command in cast(list[list[str]], state["commands"]):
            self.assertEqual(command[:4], ["--config", state["config"], "--host", state["host"]])
        commands = cast(list[list[str]], state["commands"])
        expected_create = list(
            create_arguments(self.fake.runtime, JOB_ID, ReaderRequest("pdf-text"))
        )
        self.assertIn(
            ["--config", state["config"], *expected_create[1:]],
            commands,
        )
        self.assertIn(
            [
                "--config",
                state["config"],
                "--host",
                state["host"],
                "container",
                "start",
                "--attach",
                "--interactive",
                CONTAINER_ID,
            ],
            commands,
        )
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")

    def test_oversized_worker_output_is_drained_then_cleaned_without_content(self) -> None:
        self.fake.update(oversized_output=True)
        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "limit_exceeded")
        self.assertTrue(self.fake.read()["drained"])
        self.assertFalse(self.fake.read()["exists"])
        self.assertNotIn("xxx", str(raised.exception))

    def test_cancellation_during_attached_start_cleans_and_publishes_nothing(self) -> None:
        self.fake.update(delays={"container start": 30})
        cancelled = threading.Event()
        timer = threading.Timer(0.3, cancelled.set)
        timer.start()
        try:
            with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
                self.coordinator().read(ReaderRequest("pdf-text"), _PDF, cancelled)
        finally:
            timer.cancel()

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertFalse(self.fake.read()["exists"])

    def test_cancellation_stops_partial_upload_before_delayed_cleanup_finishes(self) -> None:
        upload = self.fake.state_root.parent / "uploaded-bytes"
        self.fake.update(upload_path=str(upload), upload_delay=0.01)
        cancelled = threading.Event()
        cleanup_started = threading.Event()
        finish_cleanup = threading.Event()
        observed: list[int] = []
        original_cleanup = reader_host._cleanup_receipt

        def delayed_cleanup(
            client: reader_client._RuntimeClient,
            jobs: ReaderJobs,
            receipt: dict[str, object],
            deadline: float,
        ) -> None:
            cleanup_started.set()
            if not finish_cleanup.wait(6):
                raise AssertionError("cleanup was not released")
            original_cleanup(client, jobs, receipt, deadline)

        def cancel_after_partial_upload() -> None:
            limit = time.monotonic() + 5
            try:
                while time.monotonic() < limit:
                    if upload.exists() and int(upload.read_text()) > 0:
                        cancelled.set()
                        break
                    time.sleep(0.01)
                if not cleanup_started.wait(max(0, limit - time.monotonic())):
                    return
                while time.monotonic() < limit:
                    if "received_sha256" in self.fake.read():
                        observed.append(int(upload.read_text()))
                        return
                    time.sleep(0.01)
            finally:
                cancelled.set()
                finish_cleanup.set()

        watcher = threading.Thread(target=cancel_after_partial_upload)
        watcher.start()
        payload = _PDF + b"p" * (8 * 1024**2 - len(_PDF))
        try:
            with (
                self.qualified(),
                mock.patch.object(reader_host, "_cleanup_receipt", new=delayed_cleanup),
                self.assertRaises(SourceArchiveError) as raised,
            ):
                self.coordinator().read(ReaderRequest("pdf-text"), payload, cancelled)
        finally:
            watcher.join(5)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertTrue(cleanup_started.is_set())
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], len(payload))
        self.assertEqual(int(upload.read_text()), observed[0])
        self.assertFalse(self.fake.read()["exists"])

    def test_cleanup_failure_supersedes_worker_success_and_preserves_receipt(self) -> None:
        self.fake.update(remove_failure=True)
        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        self.assertNotIn("synthetic text", str(raised.exception))
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "bound")

    def test_recover_clears_matching_pending_receipt_only_after_successful_absence_query(
        self,
    ) -> None:
        jobs = ReaderJobs(self.fake.runtime)
        with jobs.locked(time.monotonic() + 2):
            jobs.pending(JOB_ID)

        self.coordinator().recover()

        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertTrue(any(command[4:6] == ["container", "ls"] for command in commands))

    def test_recover_reconciles_bound_container_with_empty_dns_after_daemon_restart(self) -> None:
        document = inspected_exited_container()
        cast(dict[str, object], document[0]["HostConfig"])["Dns"] = []
        self.fake.update(exists=True, document=document)
        jobs = ReaderJobs(self.fake.runtime)
        with jobs.locked(time.monotonic() + 2):
            jobs.pending(JOB_ID)
            jobs.bind(JOB_ID, CONTAINER_ID)

        self.coordinator().recover()

        self.assertFalse(self.fake.read()["exists"])
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        removals = [command[4:] for command in commands if command[4:6] == ["container", "rm"]]
        self.assertEqual(removals, [["container", "rm", "--force", CONTAINER_ID]])

    def test_recover_refuses_ambiguous_or_wrong_identity_and_preserves_receipt(self) -> None:
        self.fake.update(remove_failure=True)
        with self.qualified(), self.assertRaises(SourceArchiveError):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.fake.update(remove_failure=False, ambiguous_query=True)

        with self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().recover()

        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "bound")

    def test_cleanup_reconciles_a_lost_remove_response_by_immutable_id_absence(self) -> None:
        self.fake.update(remove_response_failure=True)

        with self.qualified():
            result = self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(result, _pdf_response(_PDF))
        self.assertFalse(self.fake.read()["exists"])
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertTrue(any("id=" + CONTAINER_ID in command for command in commands))

    def test_recover_reconciles_interruption_after_remove_before_journal_clear(self) -> None:
        original_clear = ReaderJobs.clear
        interrupted = False

        def interrupt_first_clear(jobs: ReaderJobs, job_id: str) -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise SourceArchiveError("source_unavailable")
            original_clear(jobs, job_id)

        with (
            self.qualified(),
            mock.patch.object(ReaderJobs, "clear", new=interrupt_first_clear),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        self.assertFalse(self.fake.read()["exists"])

        self.coordinator().recover()

        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")

    def test_bound_absence_reconciliation_refuses_renames_query_failures_and_daemon_changes(
        self,
    ) -> None:
        cases = (
            {"exists": True, "renamed": True},
            {"exists": False, "id_query_failure": True},
            {"exists": False, "reported_daemon": "changed-daemon"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                fake = FakeDocker()
                self.addCleanup(fake.close)
                jobs = ReaderJobs(fake.runtime)
                with jobs.locked(time.monotonic() + 2):
                    jobs.pending(JOB_ID)
                    jobs.bind(JOB_ID, CONTAINER_ID)
                fake.update(**changes)

                with self.assertRaises(SourceArchiveError) as raised:
                    self.coordinator(fake).recover()

                self.assertEqual(raised.exception.code, "cleanup_incomplete")
                journal = json.loads((fake.state_root / "reader-job.json").read_text())
                self.assertEqual(journal["phase"], "bound")

    def test_cold_socket_and_daemon_mismatch_refuse_without_create(self) -> None:
        self.fake.listener.close()
        self.fake.socket.unlink()
        with self.qualified(), self.assertRaises(SourceArchiveError):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(self.fake.read()["commands"], [])

        replacement = socket.socket(_AF_UNIX)
        replacement.bind(str(self.fake.socket))
        os.chmod(self.fake.socket, 0o600)
        self.addCleanup(replacement.close)
        self.fake.update(reported_daemon="wrong-daemon")
        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertFalse(any(command[4:6] == ["container", "create"] for command in commands))

    def test_recovery_refuses_macos_ignore_ownership_mount_before_runtime_query(self) -> None:
        with (
            mock.patch("tedd_knowledge_graph.source_reader_client.sys.platform", "darwin"),
            mock.patch(
                "tedd_knowledge_graph.source_reader_client._darwin_mount_flags",
                return_value=0x00200000,
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().recover()

        self.assertEqual(raised.exception.code, "privacy_rejected")
        self.assertEqual(self.fake.read()["commands"], [])

    def test_use_time_recheck_refuses_mount_that_switches_to_ignore_ownership(self) -> None:
        flags = iter((0, 0, 0, 0x00200000))
        with (
            self.qualified(),
            mock.patch("tedd_knowledge_graph.source_reader_client.sys.platform", "darwin"),
            mock.patch(
                "tedd_knowledge_graph.source_reader_client._darwin_mount_flags",
                side_effect=lambda _path: next(flags),
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "privacy_rejected")
        self.assertEqual(self.fake.read()["commands"], [])

    def test_recover_does_not_require_the_image_after_a_job_is_gone(self) -> None:
        self.fake.update(image_failure=True)

        self.coordinator().recover()

        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertFalse(any(command[4:6] == ["image", "inspect"] for command in commands))

    def test_client_loss_is_content_free_and_cleanup_completes(self) -> None:
        self.fake.update(start_crash=True)
        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertFalse(self.fake.read()["exists"])
        self.assertEqual(str(raised.exception), "source_unavailable")

    def test_worker_crash_output_is_rejected_after_cleanup(self) -> None:
        self.fake.update(response="", worker_exit_code=17)

        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_corrupt")
        self.assertFalse(self.fake.read()["exists"])
        self.assertEqual(str(raised.exception), "source_corrupt")

    def test_daemon_unavailable_refuses_before_create(self) -> None:
        self.fake.update(daemon_failure=True)

        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertFalse(any(command[4:6] == ["container", "create"] for command in commands))

    def test_recovery_finishes_owned_cleanup_then_preserves_cancellation_precedence(self) -> None:
        self.fake.update(remove_failure=True)
        with self.qualified(), self.assertRaises(SourceArchiveError):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.fake.update(remove_failure=False)
        cancelled = threading.Event()
        recover_locked = reader_host._recover_locked

        def cancel_during_recovery(
            client: reader_host._RuntimeClient, jobs: ReaderJobs, deadline: float
        ) -> None:
            # Cancel after preflight admits cleanup, not during fake-daemon startup.
            cancelled.set()
            recover_locked(client, jobs, deadline)

        with mock.patch.object(
            reader_host, "_recover_locked", autospec=True, side_effect=cancel_during_recovery
        ):
            with self.assertRaises(SourceArchiveError) as raised:
                self.coordinator().recover(cancelled)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.fake.read()["exists"])
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")

    def test_exact_requested_command_is_checked_before_start(self) -> None:
        state = self.fake.read()
        document = cast(list[dict[str, object]], state["document"])
        wrong = [
            "-I",
            "-B",
            "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_reader_guest.py",
            "pdf-page",
            "2",
        ]
        document[0]["Args"] = wrong
        cast(dict[str, object], document[0]["Config"])["Cmd"] = wrong
        self.fake.write(state)

        with self.qualified(), self.assertRaises(SourceArchiveError):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertFalse(any(command[4:6] == ["container", "start"] for command in commands))
        self.assertFalse(self.fake.read()["exists"])

    def test_wrong_recovery_id_and_unrelated_labels_preserve_bound_receipt(self) -> None:
        self.fake.update(remove_failure=True)
        with self.qualified(), self.assertRaises(SourceArchiveError):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)
        self.fake.update(remove_failure=False, query_id="d" * 64)
        with self.assertRaises(SourceArchiveError) as wrong_id:
            self.coordinator().recover()
        self.assertEqual(wrong_id.exception.code, "cleanup_incomplete")

        self.fake.update(query_id=CONTAINER_ID)
        state = self.fake.read()
        document = cast(list[dict[str, object]], state["document"])
        labels = cast(dict[str, object], cast(dict[str, object], document[0]["Config"])["Labels"])
        labels["org.tedd.reader.owner"] = "00000000-0000-4000-8000-000000000000"
        self.fake.write(state)
        with self.assertRaises(SourceArchiveError) as unrelated:
            self.coordinator().recover()
        self.assertEqual(unrelated.exception.code, "cleanup_incomplete")
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "bound")

    def test_receipt_write_failure_prevents_create(self) -> None:
        with (
            self.qualified(),
            mock.patch(
                "tedd_knowledge_graph.source_reader_host.ReaderJobs.pending",
                side_effect=SourceArchiveError("source_unavailable"),
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        commands = cast(list[list[str]], self.fake.read()["commands"])
        self.assertFalse(any(command[4:6] == ["container", "create"] for command in commands))

    def test_archive_root_is_rejected_before_any_docker_process(self) -> None:
        archive = self.fake.state_root / "archive.json"
        archive.write_text("{}")
        archive.chmod(0o600)

        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "privacy_rejected")
        self.assertEqual(self.fake.read()["commands"], [])

    def test_oversized_create_response_is_capped_and_pending_receipt_is_recovered(self) -> None:
        self.fake.update(oversized_create=True)
        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "limit_exceeded")
        self.assertFalse(self.fake.read()["exists"])
        journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
        self.assertEqual(journal["phase"], "idle")

    def test_cancellation_during_preflight_create_and_inspect_never_leaves_a_job(self) -> None:
        for phase in ("system info", "image inspect", "container create", "container inspect"):
            with self.subTest(phase=phase):
                fake = FakeDocker()
                self.addCleanup(fake.close)
                fake.update(delays={phase: 0.6})
                cancelled = threading.Event()
                timer = threading.Timer(0.2, cancelled.set)
                timer.start()
                try:
                    with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
                        self.coordinator(fake).read(ReaderRequest("pdf-text"), _PDF, cancelled)
                finally:
                    timer.cancel()
                self.assertEqual(raised.exception.code, "source_unavailable")
                self.assertFalse(fake.read()["exists"])
                journal = fake.state_root / "reader-job.json"
                if journal.exists():
                    self.assertEqual(json.loads(journal.read_text())["phase"], "idle")

    def test_cancellation_after_valid_output_still_cleans_and_returns_no_content(self) -> None:
        self.fake.update(after_output_delay=1)
        cancelled = threading.Event()

        def cancel_after_output() -> None:
            limit = time.monotonic() + 5
            while time.monotonic() < limit:
                if self.fake.read().get("output_written") is True:
                    cancelled.set()
                    return
                time.sleep(0.01)

        watcher = threading.Thread(target=cancel_after_output)
        watcher.start()
        try:
            with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
                self.coordinator().read(ReaderRequest("pdf-text"), _PDF, cancelled)
        finally:
            watcher.join(5)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertTrue(self.fake.read()["output_written"])
        self.assertFalse(self.fake.read()["exists"])
        self.assertNotIn("synthetic text", str(raised.exception))

    def test_structured_worker_error_survives_nonzero_exit_after_cleanup(self) -> None:
        self.fake.update(response='{"status":"source_corrupt"}', worker_exit_code=1)

        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_corrupt")
        self.assertFalse(self.fake.read()["exists"])

    def test_success_envelope_from_nonzero_worker_exit_is_rejected_after_cleanup(self) -> None:
        self.fake.update(worker_exit_code=17)

        with self.qualified(), self.assertRaises(SourceArchiveError) as raised:
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertFalse(self.fake.read()["exists"])

    def test_cancellation_during_response_validation_prevents_late_return(self) -> None:
        cancelled = threading.Event()

        def validate(*_arguments: object) -> dict[str, object]:
            cancelled.set()
            return _pdf_response(_PDF)

        with (
            self.qualified(),
            mock.patch(
                "tedd_knowledge_graph.source_reader_host.validate_reader_response",
                side_effect=validate,
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF, cancelled)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertFalse(self.fake.read()["exists"])

    def test_post_launch_identity_failure_reaps_the_attached_cli(self) -> None:
        self.fake.update(mutate_binary_on_start=True)

        original_start = reader_client._start_bounded

        def synchronized_start(
            arguments: tuple[str, ...],
            payload: bytes,
            *,
            output_limit: int,
            environment: Mapping[str, str] | None = None,
            deadline: float | None = None,
            cancelled: threading.Event | None = None,
        ) -> reader_host._ManagedProcess:
            process = original_start(
                arguments,
                payload,
                output_limit=output_limit,
                environment=environment,
                deadline=deadline,
                cancelled=cancelled,
            )
            limit = time.monotonic() + 5
            while "mutating_pid" not in self.fake.read() and time.monotonic() < limit:
                time.sleep(0.01)
            return process

        with (
            self.qualified(),
            mock.patch.object(reader_client, "_start_bounded", new=synchronized_start),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        pid = cast(int, self.fake.read()["mutating_pid"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_journal_read_failure_finalizes_active_cli_and_reports_cleanup_incomplete(
        self,
    ) -> None:
        self.fake.update(after_output_delay=30)
        cancelled = threading.Event()
        original_receipts = ReaderJobs.receipts
        calls = 0

        def fail_final_receipt_read(jobs: ReaderJobs) -> tuple[dict[str, object], ...]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SourceArchiveError("source_unavailable")
            return original_receipts(jobs)

        def cancel_after_output() -> None:
            limit = time.monotonic() + 5
            while time.monotonic() < limit:
                if self.fake.read().get("output_written") is True:
                    cancelled.set()
                    return
                time.sleep(0.01)

        watcher = threading.Thread(target=cancel_after_output)
        watcher.start()
        pid = 0
        try:
            with (
                self.qualified(),
                mock.patch.object(
                    ReaderJobs,
                    "receipts",
                    new=fail_final_receipt_read,
                ),
                self.assertRaises(SourceArchiveError) as raised,
            ):
                self.coordinator().read(ReaderRequest("pdf-text"), _PDF, cancelled)
            self.assertEqual(raised.exception.code, "cleanup_incomplete")
            pid = cast(int, self.fake.read()["start_pid"])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertTrue(self.fake.read()["exists"])
            journal = json.loads((self.fake.state_root / "reader-job.json").read_text())
            self.assertEqual(journal["phase"], "bound")
        finally:
            watcher.join(5)
            if pid:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass

    def test_cancellation_during_bind_refuses_before_start_or_payload_writer(self) -> None:
        cancelled = threading.Event()
        original_bind = ReaderJobs.bind

        def cancel_after_bind(jobs: ReaderJobs, job_id: str, container_id: str) -> None:
            original_bind(jobs, job_id, container_id)
            cancelled.set()

        with (
            self.qualified(),
            mock.patch.object(ReaderJobs, "bind", new=cancel_after_bind),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF, cancelled)

        self.assertEqual(raised.exception.code, "source_unavailable")
        state = self.fake.read()
        commands = cast(list[list[str]], state["commands"])
        self.assertFalse(any(command[4:6] == ["container", "start"] for command in commands))
        self.assertNotIn("received_sha256", state)
        self.assertFalse(state["exists"])

    def test_deadline_during_bind_refuses_before_start_or_payload_writer(self) -> None:
        original_bind = ReaderJobs.bind

        def expire_during_bind(jobs: ReaderJobs, job_id: str, container_id: str) -> None:
            original_bind(jobs, job_id, container_id)
            time.sleep(1.1)

        with (
            self.qualified(),
            mock.patch("tedd_knowledge_graph.source_reader_host._REQUEST_SECONDS", 1.0),
            mock.patch.object(ReaderJobs, "bind", new=expire_during_bind),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "source_unavailable")
        state = self.fake.read()
        commands = cast(list[list[str]], state["commands"])
        self.assertFalse(any(command[4:6] == ["container", "start"] for command in commands))
        self.assertNotIn("received_sha256", state)
        self.assertFalse(state["exists"])

    def test_reap_failure_cannot_mask_cleanup_incomplete_classification(self) -> None:
        original_reap = reader_host._ManagedProcess.terminate_and_reap

        def fail_after_reap(process: reader_host._ManagedProcess, deadline: float) -> None:
            original_reap(process, deadline)
            raise RuntimeError("synthetic reap failure")

        with (
            self.qualified(),
            mock.patch.object(
                reader_host._ManagedProcess,
                "terminate_and_reap",
                new=fail_after_reap,
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            self.coordinator().read(ReaderRequest("pdf-text"), _PDF)

        self.assertEqual(raised.exception.code, "cleanup_incomplete")
        self.assertNotIn("synthetic", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
