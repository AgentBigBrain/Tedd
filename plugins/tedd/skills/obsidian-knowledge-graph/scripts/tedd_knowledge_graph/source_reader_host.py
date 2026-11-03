"""Fail-closed coordinator for one bounded isolated reader job."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event
from typing import cast

from .source_contracts import SourceArchiveError, parse_json
from .source_reader_client import _CONTROL_BYTES, _RuntimeClient
from .source_reader_cold import MacColdEntitlement, MacColdSession, ProfileOwners, profile_locked
from .source_reader_contracts import (
    ReaderRequest,
    validate_reader_input,
    validate_reader_response,
)
from .source_reader_jobs import ReaderJobs
from .source_reader_policy import ReaderRuntime, create_arguments, validate_container
from .source_reader_process import (
    ProcessResult,
    _ManagedProcess,
    _ProcessStopped,
)
from .source_reader_runtime import MacRuntimeBinding

_REQUEST_SECONDS = 30.0
_CLEANUP_SECONDS = 10.0
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_GUEST = "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_reader_guest.py"


def _docker_create(client: _RuntimeClient, job_id: str, request: ReaderRequest) -> tuple[str, ...]:
    base = create_arguments(client.runtime, job_id, request)
    # create_arguments owns all policy flags; the client owns fixed endpoint/config.
    if base[:3] != (
        str(client.runtime.docker),
        "--host",
        "unix://" + str(client.runtime.socket),
    ):
        raise SourceArchiveError("source_unavailable")
    return base[3:]


def _document(result: ProcessResult) -> object:
    if result.returncode != 0:
        raise SourceArchiveError("source_unavailable")
    return parse_json(result.stdout, maximum_bytes=_CONTROL_BYTES)


def _inspect(
    client: _RuntimeClient,
    target: str,
    deadline: float,
    cancelled: Event | None = None,
) -> object:
    return _document(client.control(("container", "inspect", target), deadline, cancelled))


def _created_for_request(document: object, request: ReaderRequest) -> None:
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise SourceArchiveError()
    item = cast(dict[str, object], document[0])
    config = item.get("Config")
    state = item.get("State")
    expected = ["-I", "-B", _GUEST, *request.worker_arguments()]
    if (
        not isinstance(config, dict)
        or cast(dict[object, object], config).get("Cmd") != expected
        or not isinstance(state, dict)
        or cast(dict[object, object], state).get("Status") != "created"
    ):
        raise SourceArchiveError()


def _terminal(document: object, returncode: int) -> None:
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise SourceArchiveError()
    state = cast(dict[object, object], document[0]).get("State")
    if (
        not isinstance(state, dict)
        or state.get("Status") != "exited"
        or state.get("Running") is not False
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or state.get("Dead") is not False
        or type(state.get("ExitCode")) is not int
        or state.get("ExitCode") != returncode
    ):
        raise SourceArchiveError("source_unavailable")


def _query_ids(client: _RuntimeClient, name: str, deadline: float) -> tuple[str, ...]:
    result = client.control(
        (
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "name=^/" + name + "$",
        ),
        deadline,
    )
    if result.returncode != 0:
        raise SourceArchiveError("cleanup_incomplete")
    try:
        text = result.stdout.decode("ascii")
    except UnicodeError:
        raise SourceArchiveError("cleanup_incomplete") from None
    values = tuple(line for line in text.splitlines() if line)
    if any(_CONTAINER_ID.fullmatch(value) is None for value in values) or len(set(values)) != len(
        values
    ):
        raise SourceArchiveError("cleanup_incomplete")
    return values


def _recorded_id_exists(client: _RuntimeClient, container_id: str, deadline: float) -> bool:
    result = client.control(
        (
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "id=" + container_id,
        ),
        deadline,
    )
    if result.returncode != 0:
        raise SourceArchiveError("cleanup_incomplete")
    try:
        text = result.stdout.decode("ascii")
    except UnicodeError:
        raise SourceArchiveError("cleanup_incomplete") from None
    values = tuple(line for line in text.splitlines() if line)
    if values not in ((), (container_id,)):
        raise SourceArchiveError("cleanup_incomplete")
    return bool(values)


def _cleanup_receipt(
    client: _RuntimeClient,
    jobs: ReaderJobs,
    receipt: dict[str, object],
    deadline: float,
) -> None:
    try:
        client.daemon(deadline)
        job_id = receipt.get("job_id")
        name = receipt.get("name")
        if not isinstance(job_id, str) or not isinstance(name, str):
            raise SourceArchiveError()
        found = _query_ids(client, name, deadline)
        if not found:
            if receipt.get("phase") == "pending":
                jobs.clear(job_id)
                return
            recorded = receipt.get("container_id")
            if (
                receipt.get("phase") != "bound"
                or not isinstance(recorded, str)
                or _CONTAINER_ID.fullmatch(recorded) is None
                or _recorded_id_exists(client, recorded, deadline)
            ):
                raise SourceArchiveError()
            client.daemon(deadline)
            jobs.clear(job_id)
            return
        if len(found) != 1:
            raise SourceArchiveError()
        container_id = found[0]
        recorded = receipt.get("container_id")
        if receipt.get("phase") == "bound" and recorded != container_id:
            raise SourceArchiveError()
        document = _inspect(client, container_id, deadline)
        validate_container(
            client.runtime,
            job_id,
            document,
            container_id=container_id,
        )
        client.control(("container", "rm", "--force", container_id), deadline)
        if _query_ids(client, name, deadline) or _recorded_id_exists(
            client, container_id, deadline
        ):
            raise SourceArchiveError()
        client.daemon(deadline)
        jobs.clear(job_id)
    except SourceArchiveError:
        raise SourceArchiveError("cleanup_incomplete") from None


def _recover_locked(client: _RuntimeClient, jobs: ReaderJobs, deadline: float) -> None:
    receipts = jobs.receipts()
    if len(receipts) > 1:
        raise SourceArchiveError("cleanup_incomplete")
    if receipts:
        _cleanup_receipt(client, jobs, receipts[0], deadline)


def _stopped(deadline: float, cancelled: Event | None) -> bool:
    return time.monotonic() >= deadline or (cancelled is not None and bool(cancelled.is_set()))


class ReaderCoordinator:
    """Coordinate one reader transaction against a reviewed runtime."""

    def __init__(
        self,
        runtime: ReaderRuntime,
        *,
        binding: MacRuntimeBinding | None = None,
        cold: MacColdEntitlement | None = None,
        profile_owners: ProfileOwners | None = None,
    ) -> None:
        if not isinstance(runtime, ReaderRuntime):
            raise SourceArchiveError()
        if binding is not None and cold is not None:
            raise SourceArchiveError("capability_unavailable")
        if binding is not None and binding.runtime != runtime:
            raise SourceArchiveError("capability_unavailable")
        if cold is not None and (
            cold.runtime != runtime
            or cold.profile != "colima-tedd-reader-spike"
            or profile_owners is None
            or cold.profile_authority() != profile_owners
        ):
            raise SourceArchiveError("capability_unavailable")
        if (
            getattr(binding, "profile", None) == "colima-tedd-reader-spike"
            and profile_owners is None
        ):
            raise SourceArchiveError("capability_unavailable")
        if profile_owners is not None and (
            profile_owners.runtime != runtime
            or profile_owners.profile != "colima-tedd-reader-spike"
            or (cold is None and getattr(binding, "profile", None) != profile_owners.profile)
        ):
            raise SourceArchiveError("capability_unavailable")
        self.runtime = runtime
        self.binding = binding
        self.cold = cold
        self.profile_owners = profile_owners

    def _binding(self) -> MacRuntimeBinding:
        if self.binding is None:
            raise SourceArchiveError("capability_unavailable")
        return self.binding

    @contextmanager
    def _reserved(self, deadline: float, cancelled: Event | None) -> Iterator[ReaderJobs | None]:
        authority = self.profile_owners
        if authority is None:
            yield None
            return
        with profile_locked(authority.profile_root, authority.profile, deadline, cancelled):
            receipt = authority.read_receipt(deadline, cancelled)
            if self.cold is not None:
                # Even an idle receipt must reauthenticate the loaded entitlement.
                self.cold.reconcile_start(deadline, cancelled)
                if receipt["phase"] == "starting":
                    receipt = authority.read_receipt(deadline, cancelled)
            if receipt["phase"] != "idle":
                raise SourceArchiveError("source_unavailable")
            jobs = authority.preflight_jobs(deadline, cancelled)
            yield jobs

    def _running_binding(
        self, deadline: float, cancelled: Event | None
    ) -> MacRuntimeBinding | MacColdSession:
        cold = self.cold
        if cold is None:
            return self._binding()
        # A recovery may inspect Running, but must never resume Stopped.
        return MacColdSession(cold, cold._snapshot(deadline, cancelled))

    def recover(self, cancelled: Event | None = None) -> None:
        deadline = time.monotonic() + _CLEANUP_SECONDS
        if _stopped(deadline, cancelled):
            raise SourceArchiveError("source_unavailable")
        with self._reserved(deadline, cancelled) as reserved:
            self._running_binding(deadline, cancelled).qualify(deadline, cancelled)
            client = _RuntimeClient(self.runtime)
            jobs = reserved if reserved is not None else ReaderJobs(self.runtime)
            client.daemon(deadline, cancelled)
            with jobs.locked(deadline, cancelled):
                _recover_locked(client, jobs, deadline)
                if _stopped(deadline, cancelled):
                    raise SourceArchiveError("source_unavailable")

    def read(
        self,
        request: ReaderRequest,
        payload: bytes,
        cancelled: Event | None = None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + _REQUEST_SECONDS
        if _stopped(deadline, cancelled):
            raise SourceArchiveError("source_unavailable")
        with self._reserved(deadline, cancelled) as reserved:
            return self._read_reserved(request, payload, deadline, cancelled, reserved)

    def _read_reserved(
        self,
        request: ReaderRequest,
        payload: bytes,
        deadline: float,
        cancelled: Event | None,
        reserved: ReaderJobs | None,
    ) -> dict[str, object]:
        validate_reader_input(request, payload)
        if self.cold is not None:
            jobs = reserved if reserved is not None else ReaderJobs(self.runtime)
            with jobs.locked(deadline, cancelled):
                if jobs.receipts():
                    self._running_binding(deadline, cancelled).qualify(deadline, cancelled)
                    recovery_client = _RuntimeClient(self.runtime)
                    recovery_client.daemon(deadline, cancelled)
                    _recover_locked(
                        recovery_client, jobs, min(deadline, time.monotonic() + _CLEANUP_SECONDS)
                    )
            binding: MacRuntimeBinding | MacColdSession = self.cold.ensure_ready(
                deadline, cancelled
            )
        else:
            binding = self._binding()
            jobs = reserved if reserved is not None else ReaderJobs(self.runtime)
        binding.qualify(deadline, cancelled)
        if _stopped(deadline, cancelled):
            raise SourceArchiveError("source_unavailable")
        client = _RuntimeClient(self.runtime)
        client.preflight(deadline, cancelled)
        active: _ManagedProcess | None = None
        output: bytes | None = None
        worker_exit_code: int | None = None
        failure: SourceArchiveError | None = None
        created_job = False
        job_id = str(uuid.uuid4())
        with jobs.locked(deadline, cancelled):
            recovery_deadline = min(deadline, time.monotonic() + _CLEANUP_SECONDS)
            _recover_locked(client, jobs, recovery_deadline)
            if _stopped(deadline, cancelled):
                raise SourceArchiveError("source_unavailable")
            jobs.pending(job_id)
            try:
                created = client.control(
                    _docker_create(client, job_id, request), deadline, cancelled
                )
                if created.returncode != 0:
                    raise SourceArchiveError("source_unavailable")
                try:
                    container_id = created.stdout.decode("ascii").strip()
                except UnicodeError:
                    raise SourceArchiveError() from None
                if _CONTAINER_ID.fullmatch(container_id) is None:
                    raise SourceArchiveError()
                document = _inspect(client, container_id, deadline, cancelled)
                validate_container(self.runtime, job_id, document, container_id=container_id)
                _created_for_request(document, request)
                created_job = True
                jobs.bind(job_id, container_id)
                binding.qualify(deadline, cancelled)
                if _stopped(deadline, cancelled):
                    raise SourceArchiveError("source_unavailable")
                active = client.start(
                    ("container", "start", "--attach", "--interactive", container_id),
                    payload,
                    deadline,
                    cancelled,
                )
                try:
                    result = active.wait(deadline, cancelled, leave_running_on_stop=True)
                except _ProcessStopped:
                    raise SourceArchiveError("source_unavailable") from None
                output = result.stdout
                worker_exit_code = result.returncode
                terminal = _inspect(client, container_id, deadline, cancelled)
                validate_container(self.runtime, job_id, terminal, container_id=container_id)
                _terminal(terminal, result.returncode)
            except SourceArchiveError as error:
                failure = error

            cleanup_deadline = time.monotonic() + _CLEANUP_SECONDS if created_job else deadline
            cleanup_failed = False
            try:
                receipts = jobs.receipts()
                if len(receipts) != 1:
                    raise SourceArchiveError()
                _cleanup_receipt(client, jobs, receipts[0], cleanup_deadline)
            except SourceArchiveError:
                cleanup_failed = True
            finally:
                if active is not None:
                    try:
                        active.terminate_and_reap(cleanup_deadline)
                    except Exception:
                        cleanup_failed = True
            if cleanup_failed:
                raise SourceArchiveError("cleanup_incomplete") from None
            if _stopped(deadline, cancelled):
                raise SourceArchiveError("source_unavailable")
            if failure is not None:
                raise failure
            if output is None:
                raise SourceArchiveError("source_unavailable")
            response = validate_reader_response(request, payload, output)
            if worker_exit_code != 0:
                raise SourceArchiveError("source_unavailable")
            binding.qualify(deadline, cancelled)
            if _stopped(deadline, cancelled):
                raise SourceArchiveError("source_unavailable")
            return response
