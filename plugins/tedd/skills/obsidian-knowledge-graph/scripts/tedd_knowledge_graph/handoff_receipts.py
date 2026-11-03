"""Read-only handoff proof from committed capture journals, never agent counts.

File publication and host hooks are separate boundaries. Reconstruction retains
the original mutation/run even when a caller retries capture under a new run.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .v4_contracts import canonical_json

MAX_HANDOFF_MUTATIONS = 64
MAX_HANDOFF_CHANGES = 4_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


class HandoffError(ValueError):
    """Handoff input, ownership or bounded reconstruction is unavailable."""


def _receipt_integer(value: str) -> int:
    if len(value) > 19 or not value.isascii() or not value.isdigit():
        raise HandoffError("handoff numbers must be bounded nonnegative integers")
    number = int(value)
    if number > (1 << 63) - 1:
        raise HandoffError("handoff number exceeds the journal range")
    return number


def _reject_number(value: str) -> None:
    raise HandoffError("handoff numbers must be integers")


def _receipt_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("handoff receipt contains duplicate fields")
        result[key] = value
    return result


def decode_receipt(content: bytes) -> object:
    """Decode bounded control-file bytes without permissive numeric or key coercion."""
    if len(content) > 2 * 1024 * 1024:
        raise HandoffError("handoff receipt exceeds its byte limit")
    try:
        result: object = json.loads(
            content,
            object_pairs_hook=_receipt_object,
            parse_int=_receipt_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HandoffError("handoff receipt is not valid bounded JSON") from error


def validate_prompt_id(value: object) -> str:
    """Validate the host prompt token without treating it as a filesystem path."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise HandoffError("handoff prompt must be a bounded opaque identifier")
    return value


@dataclass(frozen=True)
class HandoffIdentity:
    consumer_id: str
    agent_id: str
    task_id: str
    prompt_id: str

    def __post_init__(self) -> None:
        for value in (self.consumer_id, self.agent_id, self.task_id, self.prompt_id):
            validate_prompt_id(value)


class ReceiptStore(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    def state(self) -> Mapping[str, object]: ...

    def read_snapshot(self) -> AbstractContextManager[None]: ...


def handoff_directory() -> Path:
    return Path.home() / ".codex" / "tedd" / "handoffs"


def receipt_path(state: Mapping[str, object], identity: HandoffIdentity) -> Path:
    """Bind filenames to graph generation, consumer and prompt without path interpolation."""
    key = canonical_json(
        [
            state["store_id"],
            state["generation"],
            identity.consumer_id,
            identity.agent_id,
            identity.task_id,
            identity.prompt_id,
        ]
    )
    name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
    return handoff_directory() / name


def reconstruct_receipt(store: ReceiptStore, identity: HandoffIdentity) -> dict[str, object]:
    """Recover one prompt's committed captures, or report an unverified pending handoff.

    This reads a single WAL snapshot and never changes graph or coordination state.
    Missing history, including an anchored-away mutation, cannot establish a save.
    """
    with store.read_snapshot():
        consumer = store.connection.execute(
            "SELECT agent_id,task_id FROM consumers WHERE consumer_id=?", (identity.consumer_id,)
        ).fetchone()
        if consumer is None or (consumer[0], consumer[1]) != (identity.agent_id, identity.task_id):
            raise HandoffError("handoff identity does not own the registered consumer")
        state = store.state()
        rows = store.connection.execute(
            "SELECT mutation_id,watermark,run_id FROM mutations "
            "WHERE task_id=? AND producer_agent_id=? AND action='capture' "
            "AND json_extract(details_json,'$.consumer_id')=? "
            "AND json_extract(details_json,'$.handoff_prompt_id')=? "
            "ORDER BY watermark LIMIT ?",
            (
                identity.task_id,
                identity.agent_id,
                identity.consumer_id,
                identity.prompt_id,
                MAX_HANDOFF_MUTATIONS + 1,
            ),
        ).fetchall()
        if len(rows) > MAX_HANDOFF_MUTATIONS:
            raise HandoffError("handoff mutation limit exceeded")
        mutations: list[dict[str, object]] = []
        remaining = MAX_HANDOFF_CHANGES
        for row in rows:
            changes = store.connection.execute(
                "SELECT record_id,revision FROM mutation_changes WHERE watermark=? "
                "ORDER BY ordinal LIMIT ?",
                (row[1], remaining + 1),
            ).fetchall()
            if not changes or len(changes) > remaining:
                raise HandoffError("handoff changes unavailable or over the limit")
            remaining -= len(changes)
            mutations.append(
                {
                    "mutation_id": row[0],
                    "watermark": row[1],
                    "run_id": row[2],
                    "changed_ids": [change[0] for change in changes],
                    "revisions": {change[0]: change[1] for change in changes},
                }
            )
        return {
            "version": 1,
            "status": "verified" if mutations else "pending",
            "store_id": state["store_id"],
            "generation": state["generation"],
            "consumer_id": identity.consumer_id,
            "agent_id": identity.agent_id,
            "task_id": identity.task_id,
            "prompt_id": identity.prompt_id,
            "mutations": mutations,
        }


def verify_receipt(store: ReceiptStore, identity: HandoffIdentity, receipt: object) -> bool:
    """Verify a bounded decoded receipt against the current prompt's complete journal.

    Canonical JSON equality also rejects bool-as-int substitutions and unexpected
    fields. A stale partial receipt must be reconstructed, not reported as saved.
    """
    expected = reconstruct_receipt(store, identity)
    if expected["status"] != "verified":
        return False
    try:
        return canonical_json(receipt) == canonical_json(expected)
    except (TypeError, ValueError, RecursionError):
        return False


def nothing_receipt(
    store: ReceiptStore, identity: HandoffIdentity, reason: object
) -> dict[str, object]:
    """An auditable agent assertion, never journal proof of a saved outcome."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise HandoffError("nothing-to-save reason must contain 1 to 500 characters")
    if any(ord(char) < 32 for char in reason):
        raise HandoffError("nothing-to-save reason must be one line")
    receipt = reconstruct_receipt(store, identity)
    if receipt["status"] == "verified":
        raise HandoffError("this prompt has committed captures; reconstruct their proof instead")
    return {**receipt, "status": "agent_asserted", "reason": reason.strip()}


def accepts_nothing(store: ReceiptStore, identity: HandoffIdentity, receipt: object) -> bool:
    """Accept a no-save assertion only for an owned prompt with no committed capture."""
    if not isinstance(receipt, dict):
        return False
    try:
        expected = nothing_receipt(store, identity, receipt.get("reason"))
        return canonical_json(receipt) == canonical_json(expected)
    except (HandoffError, TypeError, ValueError, RecursionError):
        return False
