"""Durable local Google generation fencing, separate from provider transport.

All public reservations are short and local. Never perform network work or ask
for human confirmation while holding one. Secret bytes remain in the existing
OS-protected credential store and short-lived, repr-hidden snapshots.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from knowledge_graph_file_io import fsync_directory
from platform_support import create_private_exclusive_file

from . import source_files
from .source_contracts import SourceArchiveError, encode, object_fields, parse_json, sha256
from .source_credentials import CredentialStore

_SCHEMA = "tedd-google-connection-v1"
_STATE_FIELDS = {"schema", "binding", "generation", "revision", "enabled", "operation_id"}
_INTENT_FIELDS = {"binding", "generation", "operation_id"}
_OUTCOME_FIELDS = {
    "intent",
    "disabled_generation",
    "credentials_removed",
    "revocation",
    "sender_retired",
}
_REVOCATIONS = {"not_started", "sending", "confirmed", "proven_not_sent", "uncertain"}
_MAX_OPERATIONS = 256
_MAX_STATE_BYTES = 256 * 1024


def _empty_lock(path: Path) -> None:
    """Create the zero-byte lock exclusively; there is no staged/link cleanup gap."""
    source_files.private_root(path.parent)
    try:
        descriptor = create_private_exclusive_file(path)
    except FileExistsError:
        if source_files.read_bytes(path, 1) != b"":
            raise SourceArchiveError("privacy_rejected") from None
    else:
        with os.fdopen(descriptor, "wb") as handle:
            os.fsync(handle.fileno())
        source_files.check_entry(path)
    fsync_directory(path.parent)


def _counter(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise SourceArchiveError()
    return value


@dataclass(frozen=True, repr=False)
class ConnectionStamp:
    binding: str
    generation: int
    revision: int
    enabled: bool
    operation_id: str | None = None

    def __post_init__(self) -> None:
        sha256(self.binding)
        _counter(self.generation)
        _counter(self.revision)
        if type(self.enabled) is not bool:
            raise SourceArchiveError()
        if self.operation_id is not None:
            sha256(self.operation_id)


@dataclass(frozen=True, repr=False)
class CredentialSnapshot:
    stamp: ConnectionStamp
    payload: bytes | None


@dataclass(frozen=True, repr=False)
class DisconnectIntent:
    """Host-created identity captured before showing confirmation; not approval."""

    binding: str
    generation: int
    operation_id: str

    def __post_init__(self) -> None:
        sha256(self.binding)
        _counter(self.generation)
        sha256(self.operation_id)


@dataclass(frozen=True, repr=False)
class DisconnectOutcome:
    intent: DisconnectIntent
    disabled_generation: int
    credentials_removed: bool
    revocation: str
    sender_retired: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.intent, DisconnectIntent)
            or _counter(self.disabled_generation) != self.intent.generation + 1
            or type(self.credentials_removed) is not bool
            or type(self.sender_retired) is not bool
            or not isinstance(self.revocation, str)
            or self.revocation not in _REVOCATIONS
            or (self.sender_retired != (self.revocation not in {"not_started", "sending"}))
        ):
            raise SourceArchiveError()


@dataclass(frozen=True, repr=False)
class PreparedDisconnect:
    outcome: DisconnectOutcome
    payload: bytes | None
    owns_dispatch: bool


@dataclass(frozen=True, repr=False)
class _State:
    stamp: ConnectionStamp
    operations: tuple[DisconnectOutcome, ...] = ()

    def operation(self, intent: DisconnectIntent) -> DisconnectOutcome | None:
        for operation in self.operations:
            if operation.intent.operation_id == intent.operation_id:
                if operation.intent != intent:
                    raise SourceArchiveError("scope_denied")
                return operation
        return None

    def update(self, outcome: DisconnectOutcome) -> _State:
        return replace(
            self,
            operations=tuple(
                outcome if item.intent.operation_id == outcome.intent.operation_id else item
                for item in self.operations
            ),
        )


class GoogleConnection:
    """One root/account/client's durable generation and refresh CAS boundary."""

    def __init__(self, credentials: CredentialStore, key: str) -> None:
        self._credentials = credentials
        self._key = sha256(key)
        self._binding = credentials.binding(key)
        parent = credentials.root
        _empty_lock(parent / "archive.lock")
        # Serialize first initialization across processes and different bindings.
        # An existing partial/corrupt directory is never silently reinitialized.
        with source_files.locked(parent, shared=False):
            self.root = parent / (key + ".connection")
            if self.root.exists():
                source_files.private_root(self.root)
                self._read()
            else:
                source_files.private_root(self.root, create=True)
                _empty_lock(self.root / "archive.lock")
                self._write(_State(ConnectionStamp(self._binding, 0, 0, True)), overwrite=False)

    def _read(self) -> _State:
        try:
            raw = source_files.read_bytes(self.root / "state.json", _MAX_STATE_BYTES)
        except FileNotFoundError:
            raise SourceArchiveError() from None
        value = object_fields(
            parse_json(raw, maximum_bytes=_MAX_STATE_BYTES), _STATE_FIELDS | {"operations"}
        )
        if value["schema"] != _SCHEMA or value["binding"] != self._binding:
            raise SourceArchiveError()
        enabled = value["enabled"]
        if type(enabled) is not bool:
            raise SourceArchiveError()
        stamp = ConnectionStamp(
            self._binding,
            _counter(value["generation"]),
            _counter(value["revision"]),
            enabled,
            None if value["operation_id"] is None else sha256(value["operation_id"]),
        )
        entries = value["operations"]
        if not isinstance(entries, list) or len(entries) > _MAX_OPERATIONS:
            raise SourceArchiveError()
        operations = []
        seen: set[str] = set()
        for entry in entries:
            fields = object_fields(entry, _OUTCOME_FIELDS)
            intent_fields = object_fields(fields["intent"], _INTENT_FIELDS)
            intent = DisconnectIntent(
                sha256(intent_fields["binding"]),
                _counter(intent_fields["generation"]),
                sha256(intent_fields["operation_id"]),
            )
            removed, retired, revocation = (
                fields["credentials_removed"],
                fields["sender_retired"],
                fields["revocation"],
            )
            if (
                type(removed) is not bool
                or type(retired) is not bool
                or not isinstance(revocation, str)
                or intent.binding != self._binding
                or intent.operation_id in seen
            ):
                raise SourceArchiveError()
            outcome = DisconnectOutcome(
                intent, _counter(fields["disabled_generation"]), removed, revocation, retired
            )
            if outcome.disabled_generation > stamp.generation:
                raise SourceArchiveError()
            operations.append(outcome)
            seen.add(intent.operation_id)
        if stamp.operation_id is not None and stamp.operation_id not in seen:
            raise SourceArchiveError()
        return _State(stamp, tuple(operations))

    def _write(self, state: _State, *, overwrite: bool = True) -> None:
        source_files.write_bytes(
            self.root / "state.json",
            encode(
                {
                    "schema": _SCHEMA,
                    **asdict(state.stamp),
                    "operations": [asdict(item) for item in state.operations],
                }
            ),
            overwrite=overwrite,
        )

    def snapshot(self) -> CredentialSnapshot:
        with source_files.locked(self.root, shared=True):
            return CredentialSnapshot(self._read().stamp, self._credentials.get(self._key))

    def history(self) -> tuple[ConnectionStamp, tuple[DisconnectOutcome, ...]]:
        """Content-free status; never read credentials for a disconnect receipt."""
        with source_files.locked(self.root, shared=True):
            state = self._read()
            return state.stamp, state.operations

    def _compare(self, expected: CredentialSnapshot) -> _State:
        current = self._read()
        if current.stamp != expected.stamp or not current.stamp.enabled:
            raise SourceArchiveError("reauthentication_required")
        payload = self._credentials.get(self._key)
        if (payload is None) != (expected.payload is None) or (
            payload is not None
            and expected.payload is not None
            and not secrets.compare_digest(payload, expected.payload)
        ):
            raise SourceArchiveError("reauthentication_required")
        return current

    def replace(
        self,
        expected: CredentialSnapshot,
        payload: bytes,
        *,
        new_grant: bool = False,
    ) -> CredentialSnapshot:
        if (
            type(payload) is not bytes
            or not 0 < len(payload) <= 65536
            or type(new_grant) is not bool
        ):
            raise SourceArchiveError()
        with source_files.locked(self.root, shared=False):
            current = self._compare(expected)
            updated = replace(
                current.stamp,
                generation=_counter(current.stamp.generation + int(new_grant)),
                revision=_counter(current.stamp.revision + 1),
            )
            # Fence before changing secret storage. A crash or write failure
            # leaves local access disabled, never an apparently usable old grant.
            self._write(replace(current, stamp=replace(updated, enabled=False)))
            self._credentials.put(self._key, payload)
            self._write(replace(current, stamp=updated))
            return CredentialSnapshot(updated, payload)

    def reject(self, expected: CredentialSnapshot) -> ConnectionStamp:
        """Invalidate only the exact failed refresh, never replacement credentials."""
        with source_files.locked(self.root, shared=False):
            current = self._compare(expected)
            disabled = replace(
                current.stamp,
                generation=_counter(current.stamp.generation + 1),
                revision=_counter(current.stamp.revision + 1),
                enabled=False,
            )
            self._write(replace(current, stamp=disabled))
            self._credentials.delete(self._key)
            return disabled

    @contextmanager
    def hold(self, expected: ConnectionStamp) -> Iterator[None]:
        """Fence a short publication/delivery against acknowledged local disable."""
        with source_files.locked(self.root, shared=True):
            current = self._read().stamp
            if (
                not current.enabled
                or not expected.enabled
                or current.binding != expected.binding
                or current.generation != expected.generation
            ):
                raise SourceArchiveError("reauthentication_required")
            yield

    def intent(self, operation_id: str) -> DisconnectIntent:
        """Freeze the target generation before a host asks for confirmation."""
        sha256(operation_id)
        with source_files.locked(self.root, shared=True):
            state = self._read()
            for item in state.operations:
                if item.intent.operation_id == operation_id:
                    return item.intent
            if not state.stamp.enabled:
                raise SourceArchiveError("reauthentication_required")
            return DisconnectIntent(self._binding, state.stamp.generation, operation_id)

    def _sender_root(self, intent: DisconnectIntent) -> Path:
        if intent.binding != self._binding:
            raise SourceArchiveError("scope_denied")
        return self.root / (intent.operation_id + ".sender")

    def disable(self, intent: DisconnectIntent) -> PreparedDisconnect:
        """Apply a confirmed intent; duplicate delivery never gets another token."""
        with source_files.locked(self.root, shared=False):
            state = self._read()
            existing = state.operation(intent)
            if existing is not None:
                return PreparedDisconnect(existing, None, False)
            if (
                intent.binding != self._binding
                or intent.generation != state.stamp.generation
                or not state.stamp.enabled
            ):
                raise SourceArchiveError("scope_denied")
            if len(state.operations) >= _MAX_OPERATIONS:
                # Preserve unresolved and confirmed history rather than evicting it.
                raise SourceArchiveError("limit_exceeded")
            try:
                sender_root = source_files.private_root(self._sender_root(intent), create=True)
            except FileExistsError:
                # Only our exact private, empty-lock preparation can be resumed.
                # Unknown contents/reparse points are not crash-recovery evidence.
                sender_root = source_files.private_root(self._sender_root(intent))
                for child in sender_root.iterdir():
                    if child.name != "archive.lock" or source_files.read_bytes(child, 1) != b"":
                        raise SourceArchiveError("privacy_rejected") from None
            _empty_lock(sender_root / "archive.lock")
            disabled = replace(
                state.stamp,
                generation=_counter(state.stamp.generation + 1),
                revision=_counter(state.stamp.revision + 1),
                enabled=False,
                operation_id=intent.operation_id,
            )
            outcome = DisconnectOutcome(intent, disabled.generation, False, "not_started", False)
            state = _State(disabled, state.operations + (outcome,))
            # Receipt and disable are one atomic durable publication before deletion.
            self._write(state)
            # Even unreadable credentials must not leave cached grants active.
            payload = self._credentials.get(self._key)
            self._credentials.delete(self._key)
            outcome = replace(outcome, credentials_removed=True)
            self._write(state.update(outcome))
            return PreparedDisconnect(outcome, payload, True)

    def dispatch(
        self, prepared: PreparedDisconnect, sender: Callable[[bytes], str]
    ) -> DisconnectOutcome:
        """One bounded sender. Its token is never persisted or queued for retry."""
        intent = prepared.outcome.intent
        if not prepared.owns_dispatch:
            with source_files.locked(self.root, shared=True):
                existing = self._read().operation(intent)
                if existing is None:
                    raise SourceArchiveError()
                return existing
        # A separate sender lock spans network work, never the credential lock.
        # Recovery must acquire this lock before claiming the old sender stopped.
        with source_files.locked(self._sender_root(intent), shared=False):
            with source_files.locked(self.root, shared=False):
                state = self._read()
                outcome = state.operation(intent)
                if outcome is None:
                    raise SourceArchiveError()
                if outcome.sender_retired:
                    return outcome
                if outcome.revocation != "not_started" or prepared.payload is None:
                    return self._retire(state, outcome)
                outcome = replace(outcome, revocation="sending")
                self._write(state.update(outcome))
            result = "uncertain"
            try:
                returned = sender(prepared.payload)
                if returned not in {"confirmed", "proven_not_sent", "uncertain"}:
                    raise SourceArchiveError()
                result = returned
            finally:
                # A crash before this publication remains 'sending' (uncertain),
                # never presumed confirmed. No old callback touches the new stamp.
                with source_files.locked(self.root, shared=False):
                    state = self._read()
                    outcome = replace(outcome, revocation=result, sender_retired=True)
                    self._write(state.update(outcome))
            return outcome

    def _retire(self, state: _State, outcome: DisconnectOutcome) -> DisconnectOutcome:
        if not outcome.sender_retired:
            outcome = replace(
                outcome,
                sender_retired=True,
                revocation=(
                    "proven_not_sent" if outcome.revocation == "not_started" else "uncertain"
                ),
            )
            self._write(state.update(outcome))
        return outcome

    def retire(self, intent: DisconnectIntent) -> DisconnectOutcome:
        """Reconcile a stopped sender, without retrying an uncertain request."""
        with source_files.locked(self._sender_root(intent), shared=False):
            with source_files.locked(self.root, shared=False):
                state = self._read()
                outcome = state.operation(intent)
                if outcome is None:
                    raise SourceArchiveError()
                return self._retire(state, outcome)

    def reopen(self, expected: ConnectionStamp) -> CredentialSnapshot:
        """Host-confirmed recovery only; fresh OAuth consent is still required."""
        with source_files.locked(self.root, shared=False):
            state = self._read()
            if (
                state.stamp != expected
                or expected.enabled
                or any(not item.sender_retired for item in state.operations)
            ):
                raise SourceArchiveError("scope_denied")
            if len(state.operations) >= _MAX_OPERATIONS:
                # Never enable a grant without room for its eventual disconnect.
                # Keep all outcomes; an exhausted connection stays disconnected
                # until separately reviewed maintenance, not silent history loss.
                raise SourceArchiveError("limit_exceeded")
            # Delete only under exact generation CAS; a failed old deletion cannot
            # be retried against replacement credentials.
            self._credentials.delete(self._key)
            state = replace(
                state,
                operations=tuple(
                    replace(item, credentials_removed=True)
                    if item.intent.operation_id == state.stamp.operation_id
                    else item
                    for item in state.operations
                ),
            )
            stamp = replace(
                expected,
                generation=_counter(expected.generation + 1),
                revision=_counter(expected.revision + 1),
                enabled=True,
                operation_id=None,
            )
            self._write(replace(state, stamp=stamp))
            return CredentialSnapshot(stamp, None)
