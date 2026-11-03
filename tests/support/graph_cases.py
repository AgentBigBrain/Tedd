"""Shared synthetic graph builders and deterministic concurrency support."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any

import knowledge_graph as v3
import knowledge_graph_v4 as v4
from platform_support import set_private_path_mode

FIXED_TIME = "2026-01-01T12:00:00Z"
PLAN_KEY = bytes.fromhex("11" * 32)


def canonical_v3_bytes(graph: dict[str, Any]) -> bytes:
    return (json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def make_v3_graph() -> dict[str, Any]:
    graph = v3.blank_graph()
    packet: dict[str, Any] = {
        "source": {
            "kind": "test",
            "role": "fixture",
            "title": "Client Alpha import fixture",
            "observed_at": FIXED_TIME,
            "source_tier": "validated_structured_candidate",
        },
        "entities": [
            {
                "id": "entity_client_alpha_site",
                "ref": "client_alpha_site",
                "type": "site",
                "namespace": "client_alpha",
                "name": "Test Client Alpha",
                "identity_keys": [
                    {
                        "scheme": "client_slug",
                        "namespace": "clients",
                        "value": "client-alpha",
                    }
                ],
            }
        ],
        "claims": [
            {
                "surface_id": "surface_client_alpha_environment",
                "family": "deployment",
                "key": "environment",
                "value": "staging",
                "valid_from": FIXED_TIME,
                "source_tier": "validated_structured_candidate",
                "entity_refs": ["client_alpha_site"],
                "subject_refs": ["client_alpha_site"],
            }
        ],
        "open_loops": [
            {
                "id": "loop_client_alpha_review",
                "title": "Review Client Alpha library",
                "summary": "Wait for review feedback.",
                "created_at": FIXED_TIME,
                "status": "open",
                "priority": 2,
                "entity_refs": ["client_alpha_site"],
                "owner_refs": ["client_alpha_site"],
                "next_action": "Review feedback",
                "checkpoint_at": "2026-01-02T12:00:00Z",
                "terminal_condition": "Feedback is resolved.",
            }
        ],
    }
    result = v3.capture_packet(
        graph,
        packet,
        authority_ceiling="validated_structured_candidate",
        producer={"agent_id": "migration-test", "task_id": "v4-import"},
        based_on_watermark=0,
    )
    if result["status"] != "captured":
        raise AssertionError(result)
    validation = v3.validate_graph(graph)
    if not validation["valid"]:
        raise AssertionError(validation)
    return graph


def payload(
    record_id: str,
    *,
    namespace: str,
    name: str,
    collection: str = "entities",
    value: str | None = None,
) -> dict[str, Any]:
    if collection == "entities":
        return {
            "id": record_id,
            "namespace": namespace,
            "type": "site",
            "name": name,
            "aliases": [name],
            "identity_keys": [
                {
                    "scheme": "benchmark",
                    "namespace": namespace,
                    "value": record_id,
                }
            ],
            "lifecycle": "active",
            "status": "current",
            "sensitive": False,
        }
    if collection == "claim_surfaces":
        return {
            "id": record_id,
            "family": "test",
            "key": name,
            "subject_refs": [namespace],
            "cardinality": "single",
            "lifecycle": "active",
            "created_at": FIXED_TIME,
            "last_observed_at": FIXED_TIME,
            "evidence_refs": [],
            "source_tier": "validated_structured_candidate",
            "sensitive": False,
            "legacy_derived": False,
        }
    if collection == "claims":
        return {
            "id": record_id,
            "surface_ref": namespace,
            "family": "test",
            "key": name,
            "value": value or "current",
            "valid_from": FIXED_TIME,
            "valid_to": None,
            "asserted_at": FIXED_TIME,
            "ended_at": None,
            "source_tier": "validated_structured_candidate",
            "evidence_refs": [],
            "observation_refs": [],
            "entity_refs": [],
            "active": True,
            "lifecycle": "current",
            "superseded_by": None,
            "sensitive": False,
        }
    return {
        "id": record_id,
        "namespace": namespace,
        "title": name,
        "summary": value or name,
        "lifecycle": "active",
        "status": "recorded",
        "sensitive": False,
    }


def change(
    record_id: str,
    *,
    namespace: str = "client_alpha",
    name: str | None = None,
    collection: str = "entities",
    value: str | None = None,
    semantic_keys: list[dict[str, Any]] | None = None,
    refs: list[dict[str, Any]] | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "record_id": record_id,
        "collection": collection,
        "payload": payload(
            record_id,
            namespace=namespace,
            name=name or record_id,
            collection=collection,
            value=value,
        ),
        "change_kind": "upsert",
        "search_text": " ".join(
            part for part in (record_id, namespace, name or record_id, value or "") if part
        ),
    }
    if semantic_keys is not None:
        item["semantic_keys"] = semantic_keys
    if refs is not None:
        item["refs"] = refs
    if expected_revision is not None:
        item["expected_revision"] = expected_revision
    return item


def surface_key(family: str, key: str, subjects: list[str]) -> dict[str, Any]:
    return {
        "kind": "claim_surface",
        "key": f"{family}\0{key}\0{'|'.join(sorted(subjects))}",
    }


def current_claim_key(surface_id: str) -> dict[str, Any]:
    return {
        "kind": "current_claim",
        "key": surface_id,
    }


def applied(
    store: Any,
    changes: list[dict[str, Any]],
    *,
    fingerprint: str,
    expected_revisions: dict[str, int] | None = None,
    idempotency_key: str | None = None,
    plan_token: str | None = None,
    namespace: str = "test",
) -> dict[str, Any]:
    return store.apply_changes(
        changes,
        fingerprint=fingerprint,
        action="capture",
        discovery_kind="reusable_lesson",
        producer_agent_id=f"agent-{namespace}",
        task_id=f"task-{namespace}",
        parent_task_id="parent-test",
        run_id=f"run-{fingerprint}",
        based_on_watermark=store.state()["watermark"],
        expected_revisions=expected_revisions or {},
        idempotency_key=idempotency_key,
        plan_token=plan_token,
    )


class V4TestCase(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(prefix="kg-v4-test-", dir=private_parent)
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)
        self.store_path = self.root / "graph.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_store(self, path: Path | None = None) -> Any:
        return v4.create_store(path or self.store_path, plan_hmac_key=PLAN_KEY)


@contextmanager
def scheduled_commit(
    path: Path, write: Callable[[v4.KnowledgeGraphV4], None]
) -> Iterator[Callable[[], None]]:
    """Commit on an independent connection while the reader waits at a seam."""

    requested = Event()
    committed = Event()
    stopped = Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            if not requested.wait(10):
                raise AssertionError("reader never reached the scheduled boundary")
            if stopped.is_set():
                return
            with v4.open_store(path, plan_hmac_key=PLAN_KEY) as writer:
                write(writer)
        except BaseException as error:
            errors.append(error)
        finally:
            committed.set()

    def pause_reader() -> None:
        requested.set()
        if not committed.wait(10):
            raise AssertionError("WAL writer did not complete while reader was paused")
        if errors:
            raise errors[0]

    thread = Thread(target=worker, name="snapshot-test-writer", daemon=True)
    thread.start()
    try:
        yield pause_reader
    finally:
        stopped.set()
        requested.set()
        thread.join(10)
        if thread.is_alive():
            raise AssertionError("snapshot-test writer did not stop")
        if errors:
            raise errors[0]
