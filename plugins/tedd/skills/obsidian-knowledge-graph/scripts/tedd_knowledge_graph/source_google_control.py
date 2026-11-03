"""Confirmed Google disconnect/recovery; no source or graph mutations."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event

from .source_contracts import SourceArchiveError
from .source_google import google_object, google_request
from .source_google_connection import DisconnectOutcome, GoogleConnection
from .v4_application_contracts import JsonObject


def revoke_saved_grant(payload: bytes) -> str:
    """Only an HTTP success confirms revocation; there is no automatic retry."""
    saved = google_object(payload)
    token = saved.get("refresh_token")
    if not isinstance(token, str) or not token or len(token) > 8192:
        return "proven_not_sent"
    try:
        google_request("https://oauth2.googleapis.com/revoke", maximum=65536, form={"token": token})
    except SourceArchiveError:
        # This transport does not prove whether an error preceded transmission.
        return "uncertain"
    return "confirmed"


def _description(account: str, client_id: str) -> str:
    if any(
        not value or len(value) > 1024 or any(ord(c) < 32 for c in value)
        for value in (account, client_id)
    ):
        raise SourceArchiveError("scope_denied")
    return f"Google account: {account}\nOAuth client: {client_id}\n\n"


def _report(connection: GoogleConnection, outcome: DisconnectOutcome) -> JsonObject:
    stamp, _ = connection.history()
    return {
        "status": "google_disconnect_recorded"
        if stamp.enabled
        else "google_disconnected"
        if outcome.credentials_removed
        else "google_disconnect_incomplete",
        "operation_id": outcome.intent.operation_id,
        "local_access_disabled": not stamp.enabled,
        "credentials_removed": outcome.credentials_removed,
        "provider_revocation": outcome.revocation,
        "sender_retired": outcome.sender_retired,
        "replacement_generation_active": stamp.enabled
        and stamp.generation > outcome.disabled_generation,
    }


def disconnect_google(
    connection: GoogleConnection,
    account: str,
    client_id: str,
    operation_id: str,
    cancelled: Event,
    confirm: Callable[[str, str], bool],
    *,
    sender: Callable[[bytes], str] = revoke_saved_grant,
) -> JsonObject:
    stamp, history = connection.history()
    previous = next((item for item in history if item.intent.operation_id == operation_id), None)
    if previous is None and not stamp.enabled:
        previous = next(
            (item for item in history if item.intent.operation_id == stamp.operation_id), None
        )
    if previous is not None:
        return _report(connection, previous)
    intent = connection.intent(operation_id)
    description = _description(account, client_id) + (
        "Disconnect this Google connection? Local source access will be disabled and saved "
        "sign-in removed. Google revocation may affect other grants for this same user across "
        "the entire Google Cloud project that owns this client—not just Tedd or this task. "
        "Saved evidence is not deleted. Content already delivered cannot be recalled."
    )
    # Neither confirmation nor provider I/O holds the credential/publication lock.
    if cancelled.is_set() or not confirm(description, "disconnect") or cancelled.is_set():
        return {"status": "google_disconnect_cancelled"}
    try:
        prepared = connection.disable(intent)
    except (OSError, SourceArchiveError):
        _, history = connection.history()
        previous = next((item for item in history if item.intent == intent), None)
        if previous is None:
            raise
        # A durable local disable can precede a failed secret removal. Report
        # that partial state, not successful deletion or successful revocation.
        return _report(connection, previous)
    if not prepared.owns_dispatch:
        return _report(connection, prepared.outcome)
    if prepared.payload is None:
        outcome = connection.retire(intent)
    else:
        outcome = connection.dispatch(
            prepared, lambda payload: "proven_not_sent" if cancelled.is_set() else sender(payload)
        )
    return _report(connection, outcome)


def reconnect_google(
    connection: GoogleConnection,
    account: str,
    client_id: str,
    cancelled: Event,
    confirm: Callable[[str, str], bool],
) -> JsonObject:
    stamp, history = connection.history()
    if stamp.enabled:
        return {"status": "google_recovery_not_required"}
    # Acquiring each operation's separate sender lock proves that any local
    # sender stopped; retirement also prevents queued old dispatches from running.
    for outcome in history:
        if not outcome.sender_retired:
            connection.retire(outcome.intent)
    current, history = connection.history()
    if current != stamp:
        raise SourceArchiveError("scope_denied")
    uncertain = any(item.revocation == "uncertain" for item in history)
    description = _description(account, client_id) + (
        "Prepare a fresh Google sign-in? Earlier local senders have stopped. "
        "An unresolved earlier revocation may still invalidate a new grant at Google. "
        "Fresh consent or manual removal does not prove that earlier revocation completed. "
        "Default is to remain disconnected. Continuing permits a new sign-in only: "
        "you must still complete Google's consent for a linked source before access resumes."
    )
    if cancelled.is_set() or not confirm(description, "reconnect") or cancelled.is_set():
        return {"status": "google_recovery_cancelled"}
    connection.reopen(stamp)
    return {"status": "google_recovery_awaiting_consent", "earlier_revocation_uncertain": uncertain}
