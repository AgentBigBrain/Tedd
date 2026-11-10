"""Shared synthetic fixture for outcome, review-queue and fact-sheet tests.

Every record is invented for the test and reaches the store through the same
reviewed dry-run and apply path that production capture uses.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any
from unittest import mock

import knowledge_graph as graph
import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture
import knowledge_graph_v4_cli as cli
from knowledge_graph_v4_integration import sanitize_review_safe_value
from platform_support import set_private_path_mode

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.graph_cases import PLAN_KEY

EARLY = "2026-01-03T12:00:00Z"
LATER = "2026-01-20T09:30:00Z"
NOW = "2026-02-10T12:00:00Z"
VALIDATED = "validated_structured_candidate"
USER = "explicit_user_statement"
INFERENCE = "assistant_inference"
SCOPE = "scope_alpha"

Packet = dict[str, Any]


class OutcomeFixture(HandoffFixture):
    """A project entity, one registered consumer, and helpers to capture and read."""

    @staticmethod
    def packet(*, observed_at: str = EARLY, tier: str = VALIDATED) -> Packet:
        """Start a packet whose source carries the given time and authority."""

        spoken = tier == USER
        return {
            "source": {
                "kind": "chat" if spoken else "test",
                "role": "user" if spoken else "verified",
                "title": f"Synthetic source at {observed_at}",
                "observed_at": observed_at,
                "source_tier": tier,
            }
        }

    def _request(self, packet: Packet, ceiling: str) -> Any:
        return dataclasses.replace(
            self.request("unused", prompt=None),
            input_bytes=json.dumps(packet).encode(),
            authority_ceiling=ceiling,
            allow_replace=True,
        )

    def save(self, packet: Packet, *, ceiling: str = VALIDATED) -> dict[str, Any]:
        """Dry-run and apply one packet, returning the applied summary."""

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            return self.apply(store, self._request(packet, ceiling))

    def preview(self, packet: Packet, *, ceiling: str = VALIDATED) -> dict[str, Any]:
        """Dry-run one packet without changing the store."""

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            return capture.execute_capture(store, self._request(packet, ceiling))

    def subscribe(
        self, *entity_ids: str, subscription_id: str = SCOPE, consumer_id: str = "consumer_alpha"
    ) -> None:
        """Give a consumer one subscription to the named entities."""

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.subscribe(
                consumer_id,
                {"entity_ids": list(entity_ids or ("entity_project",))},
                subscription_id=subscription_id,
            )

    def read(self, method: str, *args: Any, now: str = NOW, **kwargs: Any) -> Any:
        """Call one read method on a read-only store at a fixed current time."""

        with (
            mock.patch.object(v4, "utc_now", return_value=now),
            v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store,
        ):
            return getattr(store, method)(*args, **kwargs)

    def payload(self, record_id: str) -> dict[str, Any]:
        return self.read("get_record", record_id)["payload"]

    def references(self, record_id: str, kind: str) -> list[str]:
        """Targets of one record's current references of the given kind."""

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            rows = store.connection.execute(
                "SELECT rr.target_record_id FROM revision_refs rr JOIN records r "
                "ON r.record_id=rr.record_id AND r.current_revision=rr.revision "
                "WHERE rr.record_id=? AND rr.ref_kind=? ORDER BY rr.ordinal",
                (record_id, kind),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def run_cli(self, *argv: str, now: str = NOW) -> dict[str, Any]:
        """Run one v4 CLI command against the fixture store at a fixed current time."""

        args = graph.build_parser().parse_args(list(argv))
        with mock.patch.object(v4, "utc_now", return_value=now):
            result = cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
                review_sanitizer=sanitize_review_safe_value,
            )
        assert result is not None
        return result

    def context(self, *, max_chars: int = 50_000, max_items: int = 100) -> dict[str, Any]:
        """Read the subscription context through the CLI in review-safe mode."""

        result = self.run_cli(
            "context",
            "--subscription-id",
            SCOPE,
            "--max-chars",
            str(max_chars),
            "--max-items",
            str(max_items),
            "--mode",
            "review_safe",
        )
        return {key: value for key, value in result.items() if key not in {"command", "status"}}

    @staticmethod
    def loop(identifier: str, **fields: Any) -> Packet:
        return {
            "id": identifier,
            "title": identifier,
            "summary": "Synthetic follow-up",
            "created_at": EARLY,
            "status": "open",
            "entity_refs": ["entity_project"],
            **fields,
        }

    @staticmethod
    def decision(identifier: str, **fields: Any) -> Packet:
        return {
            "id": identifier,
            "title": identifier,
            "summary": "Synthetic decision",
            "decided_at": EARLY,
            "status": "accepted",
            "entity_refs": ["entity_project"],
            **fields,
        }

    @staticmethod
    def note(key: str, value: str, *, family: str = "release", **fields: Any) -> Packet:
        return {
            "family": family,
            "key": key,
            "value": value,
            "entity_refs": ["entity_project"],
            **fields,
        }

    @staticmethod
    def claim(key: str, value: str, *, family: str = "release", **fields: Any) -> Packet:
        return {
            "surface_id": f"surface_project_{family}_{key}",
            "family": family,
            "key": key,
            "value": value,
            "subject_refs": ["entity_project"],
            "valid_from": EARLY,
            **fields,
        }

    def private_json(self, name: str, document: object) -> Path:
        """Write one owner-only JSON file inside the fixture's private directory."""

        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        set_private_path_mode(path, 0o600)
        return path

    def note_ids(self, saved: dict[str, Any]) -> list[str]:
        """Observation IDs created by one applied capture, in stored order."""

        return [item for item in saved["changed_ids"] if item.startswith("observation_")]
