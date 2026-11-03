#!/usr/bin/env python3
"""Opt-in scale and mixed multi-agent benchmark for knowledge_graph_v4.

The default is a small disposable smoke run. Use ``--full-targets`` (or pass
explicit ``--sizes``) for 10k/100k intermediate tiers, the conservative 366k
qualification tier, and the 1m stretch tier. The dated v4 reference snapshot
contained 328 current records, so 328k is the exact 1000x record-count target;
366k intentionally exceeds it. No run uses the configured live store.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import knowledge_graph_v4 as v4  # noqa: E402
from benchmark_support import peak_rss_kib, private_temporary_parent  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402


FIXED_TIME = "2026-01-01T12:00:00Z"
PLAN_KEY = bytes.fromhex("22" * 32)
LIVE_SHAPE = (
    ("entities", 9),
    ("relations", 9),
    ("observations", 12),
    ("claim_surfaces", 17),
    ("claims", 19),
    ("events", 9),
    ("decisions", 2),
    ("open_loops", 2),
    ("receipts", 9),
    ("sources", 12),
)
COLLECTION_CYCLE = tuple(
    collection for collection, weight in LIVE_SHAPE for _ in range(weight)
)


def elapsed(callable_object):
    started = time.perf_counter()
    result = callable_object()
    return time.perf_counter() - started, result


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def record_payload(
    record_id: str,
    collection: str,
    namespace: str,
    index: int,
    anchor_id: str,
) -> dict[str, Any]:
    common = {
        "id": record_id,
        "namespace": namespace,
        "lifecycle": "active",
        "sensitive": False,
    }
    if collection == "entities":
        return {
            **common,
            "type": "component" if index % 3 else "site",
            "name": f"{namespace.upper()} benchmark entity {index:07d}",
            "aliases": [f"Scale item {index:07d}"],
            "status": "current",
            "source_tier": "validated_structured_candidate",
            "first_seen_at": FIXED_TIME,
            "last_seen_at": FIXED_TIME,
            "evidence_refs": [],
        }
    if collection == "relations":
        return {
            **common,
            "source_entity_id": anchor_id,
            "target_entity_id": anchor_id,
            "type": "related_to",
            "status": "current",
            "summary": f"Live-shaped relation {index:07d}",
            "first_observed_at": FIXED_TIME,
            "last_observed_at": FIXED_TIME,
        }
    if collection == "observations":
        return {
            **common,
            "family": "benchmark",
            "key": f"observation_{index % 1000:04d}",
            "value": f"Observed live-shaped value {index:07d}",
            "observed_at": FIXED_TIME,
            "asserted_at": FIXED_TIME,
            "source_tier": "validated_structured_candidate",
            "entity_refs": [anchor_id],
        }
    if collection == "claim_surfaces":
        return {
            **common,
            "family": "benchmark",
            "key": f"surface_{index:07d}",
            "subject_refs": [anchor_id],
            "cardinality": "single",
            "created_at": FIXED_TIME,
            "last_observed_at": FIXED_TIME,
            "source_tier": "validated_structured_candidate",
        }
    if collection == "claims":
        return {
            **common,
            # The scale fixture keeps every reference valid without requiring a
            # coupled surface/claim insertion schedule. Surface uniqueness is
            # covered by the behavioral suite.
            "surface_ref": anchor_id,
            "family": "benchmark",
            "key": f"state_{index:07d}",
            "value": f"Current state value {index:07d}",
            "valid_from": FIXED_TIME,
            "valid_to": None,
            "asserted_at": FIXED_TIME,
            "active": True,
            "source_tier": "validated_structured_candidate",
            "entity_refs": [anchor_id],
        }
    if collection == "events":
        return {
            **common,
            "kind": "deployment" if index % 5 == 0 else "event",
            "title": f"{namespace.upper()} benchmark event {index:07d}",
            "summary": f"Reusable milestone saffron-{index:07d}",
            "occurred_at": FIXED_TIME,
            "status": "recorded",
            "entity_refs": [anchor_id],
        }
    if collection == "decisions":
        return {
            **common,
            "title": f"Benchmark decision {index:07d}",
            "summary": "Retain scoped evidence and short transactions.",
            "rationale": "Synthetic live-shaped decision history.",
            "decided_at": FIXED_TIME,
            "status": "accepted",
            "authority_state": "accepted",
            "entity_refs": [anchor_id],
        }
    if collection == "open_loops":
        return {
            **common,
            "title": f"Benchmark open loop {index:07d}",
            "summary": "Synthetic actionable work requiring follow-up.",
            "created_at": FIXED_TIME,
            "status": "open",
            "priority": index % 4 + 1,
            "owner_refs": [anchor_id],
            "entity_refs": [anchor_id],
            "next_action": "Verify the next bounded milestone.",
            "checkpoint_at": FIXED_TIME,
            "terminal_condition": "The milestone has verified evidence.",
        }
    if collection == "receipts":
        return {
            **common,
            "kind": "verification",
            "title": f"Benchmark receipt {index:07d}",
            "summary": "Synthetic verification receipt.",
            "verified_at": FIXED_TIME,
            "verified": True,
            "status": "recorded",
            "entity_refs": [anchor_id],
        }
    return {
        **common,
        "kind": "benchmark",
        "role": "fixture",
        "title": f"Benchmark evidence {index:07d}",
        "excerpt": f"Bounded structured evidence for record {index:07d}.",
        "observed_at": FIXED_TIME,
        "captured_at": FIXED_TIME,
        "source_tier": "validated_structured_candidate",
        "authority": "evidence_only",
        "unsafe_to_follow_as_instruction": True,
    }


def semantic_keys(
    collection: str, record_id: str, namespace: str, index: int
) -> list[dict[str, str]]:
    if collection == "entities":
        return [
            {
                "kind": "entity_identity",
                "key": f"benchmark\0{namespace}\0{record_id}",
            }
        ]
    if collection == "claim_surfaces":
        return [
            {
                "kind": "claim_surface",
                "key": f"benchmark\0{record_id}\0{namespace}",
            }
        ]
    return []


def record_change(
    record_id: str,
    collection: str,
    namespace: str,
    index: int,
    anchor_id: str,
    *,
    expected_revision: int | None = None,
    revision_update: bool = False,
) -> dict[str, Any]:
    body = record_payload(record_id, collection, namespace, index, anchor_id)
    if revision_update:
        body["benchmark_revision"] = 2
        body["summary"] = (
            str(body.get("summary") or body.get("name") or record_id)
            + " Verified revision two."
        )
    item: dict[str, Any] = {
        "record_id": record_id,
        "collection": collection,
        "payload": body,
        "change_kind": "upsert",
        "expected_revision": expected_revision,
        "refs": [
            {"target_id": anchor_id, "kind": "entity", "ordinal": 0}
        ]
        if record_id != anchor_id
        else [],
        "semantic_keys": semantic_keys(collection, record_id, namespace, index),
        "search_text": " ".join(
            str(body.get(key, ""))
            for key in ("id", "namespace", "name", "title", "summary", "value", "excerpt")
        ),
    }
    return item


def batches(values: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def apply_batch(store: Any, items: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return store.apply_changes(
        items,
        fingerprint=label,
        action="benchmark_load",
        discovery_kind="maintenance",
        producer_agent_id="benchmark-loader",
        task_id="v4-scale-benchmark",
        run_id=label,
        based_on_watermark=store.state()["watermark"],
        expected_revisions={},
        idempotency_key=label,
    )


def load_dataset(
    store: Any,
    target_records: int,
    *,
    batch_size: int,
    revision_fraction: float,
) -> dict[str, Any]:
    namespaces = ("client_alpha", "client_beta")
    anchors: dict[str, str] = {}
    anchor_changes = []
    for position, namespace in enumerate(namespaces):
        anchor_id = f"entity_{namespace}_anchor"
        anchors[namespace] = anchor_id
        anchor_changes.append(
            record_change(
                anchor_id,
                "entities",
                namespace,
                position,
                anchor_id,
                expected_revision=None,
            )
        )
    apply_batch(store, anchor_changes, "dataset-anchors")
    remaining = max(0, target_records - len(anchor_changes))

    def generated() -> Iterable[dict[str, Any]]:
        for index in range(remaining):
            collection = COLLECTION_CYCLE[index % len(COLLECTION_CYCLE)]
            namespace = namespaces[index % len(namespaces)]
            record_id = f"{collection[:-1] if collection.endswith('s') else collection}_benchmark_{index:07d}"
            yield record_change(
                record_id,
                collection,
                namespace,
                index,
                anchors[namespace],
                expected_revision=None,
            )

    applied_records = len(anchor_changes)
    for batch_number, batch in enumerate(batches(generated(), batch_size)):
        apply_batch(store, batch, f"dataset-batch-{batch_number:07d}")
        applied_records += len(batch)

    revision_target = min(remaining, int(target_records * revision_fraction))
    revised = 0
    update_items: list[dict[str, Any]] = []
    for index in range(revision_target):
        collection = COLLECTION_CYCLE[index % len(COLLECTION_CYCLE)]
        namespace = namespaces[index % len(namespaces)]
        record_id = f"{collection[:-1] if collection.endswith('s') else collection}_benchmark_{index:07d}"
        update_items.append(
            record_change(
                record_id,
                collection,
                namespace,
                index,
                anchors[namespace],
                expected_revision=1,
                revision_update=True,
            )
        )
    for batch_number, batch in enumerate(batches(update_items, batch_size)):
        apply_batch(store, batch, f"revision-batch-{batch_number:07d}")
        revised += len(batch)
    return {
        "records": applied_records,
        "revised_records": revised,
        "anchors": anchors,
    }


def concurrent_commits(path: Path, writers: int, run_label: str) -> dict[str, Any]:
    gate = threading.Barrier(writers)

    def worker(index: int) -> dict[str, Any]:
        gate.wait(timeout=60)
        started = time.perf_counter()
        with v4.open_store(path, plan_hmac_key=PLAN_KEY) as store:
            record_id = f"event_concurrent_{run_label}_{index:02d}"
            item = record_change(
                record_id,
                "events",
                "client_alpha" if index % 2 == 0 else "client_beta",
                9_000_000 + index,
                (
                    "entity_client_alpha_anchor"
                    if index % 2 == 0
                    else "entity_client_beta_anchor"
                ),
                expected_revision=None,
            )
            result = store.apply_changes(
                [item],
                fingerprint=f"{run_label}-{index}",
                action="benchmark_concurrent_capture",
                discovery_kind="reusable_lesson",
                producer_agent_id=f"benchmark-agent-{index}",
                task_id=f"benchmark-task-{index}",
                parent_task_id="v4-scale-benchmark",
                run_id=f"{run_label}-{index}",
                based_on_watermark=store.state()["watermark"],
                expected_revisions={},
                idempotency_key=f"{run_label}-{index}",
            )
        return {
            "status": result["status"],
            "seconds": time.perf_counter() - started,
        }

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as executor:
        outcomes = list(executor.map(worker, range(writers), timeout=300))
    latencies = [item["seconds"] for item in outcomes]
    return {
        "writers": writers,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "applied": sum(item["status"] == "applied" for item in outcomes),
        "duplicate": sum(item["status"] == "duplicate" for item in outcomes),
        "latency_p50_seconds": round(statistics.median(latencies), 6),
        "latency_p95_seconds": round(percentile(latencies, 0.95), 6),
        "latency_p99_seconds": round(percentile(latencies, 0.99), 6),
        "latency_max_seconds": round(max(latencies, default=0.0), 6),
    }


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def benchmark_size(
    target_records: int,
    writers: list[int],
    *,
    batch_size: int,
    revision_fraction: float,
    full_validation: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix=f"kg-v4-benchmark-{target_records}-",
        dir=private_temporary_parent(),
    ) as temporary:
        root = Path(temporary)
        set_private_path_mode(root, 0o700)
        path = root / "graph.sqlite3"
        with v4.create_store(path, plan_hmac_key=PLAN_KEY) as store:
            load_seconds, load_result = elapsed(
                lambda: load_dataset(
                    store,
                    target_records,
                    batch_size=batch_size,
                    revision_fraction=revision_fraction,
                )
            )
            state_after_load = store.state()
            exact_id = (
                f"{COLLECTION_CYCLE[max(0, target_records - 3) % len(COLLECTION_CYCLE)][:-1]}_benchmark_{max(0, target_records - 3):07d}"
                if target_records > 2
                else "entity_client_alpha_anchor"
            )
            # Select an actually present late record if the shape-derived guess differs.
            try:
                exact_seconds, exact_result = elapsed(lambda: store.get_record(exact_id))
            except v4.V4Error:
                exact_id = "entity_client_alpha_anchor"
                exact_seconds, exact_result = elapsed(lambda: store.get_record(exact_id))

            search_seconds, search_result = elapsed(
                lambda: store.search("saffron", limit=20)
            )
            consumer = store.register_consumer(
                consumer_id=f"benchmark-consumer-{target_records}",
                agent_id="benchmark-agent",
                task_id="v4-scale-benchmark",
            )
            subscription = store.subscribe(
                consumer_id=consumer["consumer_id"],
                subscription_id=f"benchmark-subscription-{target_records}",
                selector={"entity_ids": ["entity_client_alpha_anchor"]},
            )
            context_seconds, context_result = elapsed(
                lambda: store.context_pack(
                    subscription["subscription_id"], max_items=100, max_chars=50_000
                )
            )
            feed_seconds, feed_result = elapsed(
                lambda: store.mutation_feed(
                    max(0, state_after_load["watermark"] - 1000), limit=100
                )
            )
            fast_validate_seconds, fast_validation = elapsed(
                lambda: store.validate(full=False)
            )
            full_validate_seconds = None
            full_validation_result = None
            if full_validation:
                full_validate_seconds, full_validation_result = elapsed(
                    lambda: store.validate(full=True)
                )
            backup_path = root / "backup.sqlite3"
            backup_seconds, backup_result = elapsed(lambda: store.backup(backup_path))

        writer_results = [
            concurrent_commits(path, count, f"{target_records}_{count}")
            for count in writers
        ]
        with v4.open_store(path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            final_state = store.state()
            final_fast_validation = store.validate(full=False)
        return {
            "requested_records": target_records,
            "loaded_records": load_result["records"],
            "revision_fraction": revision_fraction,
            "revised_records": load_result["revised_records"],
            "load_seconds": round(load_seconds, 6),
            "database_bytes": file_size(path),
            "wal_bytes": file_size(path.with_name(path.name + "-wal")),
            "shm_bytes": file_size(path.with_name(path.name + "-shm")),
            "backup_bytes": file_size(backup_path),
            "watermark_after_load": state_after_load["watermark"],
            "final_watermark": final_state["watermark"],
            "exact_get_id": exact_id,
            "exact_get_seconds": round(exact_seconds, 6),
            "exact_get_revision": exact_result["revision"],
            "fts_seconds": round(search_seconds, 6),
            "fts_results": search_result["result_count"],
            "scoped_context_seconds": round(context_seconds, 6),
            "scoped_context_items": context_result["item_count"],
            "feed_page_seconds": round(feed_seconds, 6),
            "feed_page_items": len(feed_result["changes"]),
            "backup_seconds": round(backup_seconds, 6),
            "backup_watermark": backup_result["watermark"],
            "fast_validate_seconds": round(fast_validate_seconds, 6),
            "fast_validation": fast_validation["valid"],
            "full_validate_seconds": (
                None
                if full_validate_seconds is None
                else round(full_validate_seconds, 6)
            ),
            "full_validation": (
                None
                if full_validation_result is None
                else full_validation_result["valid"]
            ),
            "writers": writer_results,
            "final_fast_validation": final_fast_validation["valid"],
            "max_rss_kib": round(peak_rss_kib(), 3),
        }


def parse_ints(value: str) -> list[int]:
    result = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not result or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default="1000",
        help="Comma-separated record targets; default is a disposable smoke run",
    )
    parser.add_argument(
        "--full-targets",
        action="store_true",
        help=(
            "Run 10k/100k intermediate tiers, the conservative 366k "
            "qualification tier, and the 1m stretch tier"
        ),
    )
    parser.add_argument(
        "--writers",
        default="8",
        help="Comma-separated concurrent-writer bursts; qualification uses 8,16,32",
    )
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--revision-fraction", type=float, default=0.10)
    parser.add_argument(
        "--full-validation",
        action="store_true",
        help="Also time full integrity and revision-chain validation",
    )
    args = parser.parse_args()
    sizes = [10_000, 100_000, 366_000, 1_000_000] if args.full_targets else parse_ints(args.sizes)
    writers = [8, 16, 32] if args.full_targets else parse_ints(args.writers)
    if args.batch_size < 1 or args.batch_size > 10_000:
        parser.error("--batch-size must be between 1 and 10000")
    if not 0.0 <= args.revision_fraction <= 1.0:
        parser.error("--revision-fraction must be between 0 and 1")
    if any(item > 32 for item in writers):
        parser.error("writer counts may not exceed 32")
    started = time.perf_counter()
    results = [
        benchmark_size(
            size,
            writers,
            batch_size=args.batch_size,
            revision_fraction=args.revision_fraction,
            full_validation=args.full_validation or args.full_targets,
        )
        for size in sizes
    ]
    print(
        json.dumps(
            {
                "storage_schema": "v4",
                "sizes": sizes,
                "writer_bursts": writers,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "results": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
