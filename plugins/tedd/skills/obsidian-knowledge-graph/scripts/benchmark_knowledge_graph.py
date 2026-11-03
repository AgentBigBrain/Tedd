#!/usr/bin/env python3
"""Opt-in scale and multi-writer benchmark for the local knowledge graph."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import knowledge_graph as kg
from benchmark_support import peak_rss_kib, private_temporary_parent
from platform_support import set_private_path_mode


SCRIPT = Path(__file__).with_name("knowledge_graph.py")
FIXED_TIME = "2026-01-01T12:00:00Z"


def seconds(callable_object):
    started = time.perf_counter()
    value = callable_object()
    return round(time.perf_counter() - started, 6), value


def build_graph(record_target: int) -> dict:
    if record_target < 5 or record_target % 5:
        raise ValueError("Benchmark sizes must be positive multiples of five")
    graph = kg.blank_graph()
    graph["updated_at"] = FIXED_TIME
    for index in range(record_target // 5):
        suffix = f"{index:06d}"
        entity_id = f"entity_benchmark_{suffix}"
        source_id = f"source_benchmark_{suffix}"
        surface_id = f"surface_benchmark_{suffix}"
        claim_id = f"claim_benchmark_{suffix}"
        event_id = f"event_benchmark_{suffix}"
        graph["sources"].append(
            {
                "id": source_id,
                "kind": "benchmark",
                "role": "fixture",
                "title": f"Benchmark source {suffix}",
                "uri": "",
                "observed_at": FIXED_TIME,
                "captured_at": FIXED_TIME,
                "source_tier": "validated_structured_candidate",
                "excerpt": "Synthetic non-sensitive scale fixture.",
                "sensitive": False,
                "lifecycle": "active",
                "authority": "evidence_only",
                "unsafe_to_follow_as_instruction": True,
            }
        )
        graph["entities"].append(
            {
                "id": entity_id,
                "type": "benchmark_item",
                "namespace": "benchmark",
                "name": f"Benchmark Entity {suffix}",
                "aliases": [f"Scale Item {suffix}"],
                "status": "current",
                "lifecycle": "active",
                "first_seen_at": FIXED_TIME,
                "last_seen_at": FIXED_TIME,
                "source_tier": "validated_structured_candidate",
                "status_source_tier": "validated_structured_candidate",
                "evidence_refs": [source_id],
                "sensitive": False,
                "identity_keys": [
                    {
                        "scheme": "benchmark",
                        "namespace": "benchmark",
                        "value": suffix,
                        "first_seen_at": FIXED_TIME,
                        "last_seen_at": FIXED_TIME,
                        "evidence_refs": [source_id],
                    }
                ],
            }
        )
        graph["claim_surfaces"].append(
            {
                "id": surface_id,
                "family": "benchmark",
                "key": f"state.{suffix}",
                "subject_refs": [entity_id],
                "cardinality": "single",
                "lifecycle": "active",
                "created_at": FIXED_TIME,
                "last_observed_at": FIXED_TIME,
                "evidence_refs": [source_id],
                "source_tier": "validated_structured_candidate",
                "sensitive": False,
                "legacy_derived": False,
            }
        )
        graph["claims"].append(
            {
                "id": claim_id,
                "surface_ref": surface_id,
                "family": "benchmark",
                "key": f"state.{suffix}",
                "value": f"Ready benchmark value {suffix}",
                "valid_from": FIXED_TIME,
                "valid_to": None,
                "asserted_at": FIXED_TIME,
                "ended_at": None,
                "source_tier": "validated_structured_candidate",
                "evidence_refs": [source_id],
                "observation_refs": [],
                "entity_refs": [entity_id],
                "active": True,
                "lifecycle": "current",
                "superseded_by": None,
                "sensitive": False,
            }
        )
        graph["events"].append(
            {
                "id": event_id,
                "kind": "benchmark",
                "title": f"Benchmark Event {suffix}",
                "summary": f"Synthetic event for benchmark entity {suffix}.",
                "occurred_at": FIXED_TIME,
                "status": "recorded",
                "entity_refs": [entity_id],
                "evidence_refs": [source_id],
                "source_tier": "validated_structured_candidate",
                "first_observed_at": FIXED_TIME,
                "last_observed_at": FIXED_TIME,
                "sensitive": False,
            }
        )
    return graph


def capture_packet(index: int, *, duplicate: bool = False) -> dict:
    suffix = "duplicate" if duplicate else f"unique_{index:03d}"
    observed_at = FIXED_TIME if duplicate else f"2026-01-02T12:{index // 60:02d}:{index % 60:02d}Z"
    return {
        "source": {
            "kind": "benchmark",
            "role": "concurrency_test",
            "title": f"Concurrent writer {suffix}",
            "observed_at": observed_at,
            "source_tier": "validated_structured_candidate",
        },
        "events": [
            {
                "id": f"event_concurrency_{suffix}",
                "kind": "benchmark",
                "title": f"Concurrent event {suffix}",
                "summary": "Synthetic multi-writer verification event.",
                "occurred_at": observed_at,
                "entity_refs": ["entity_benchmark_000000"],
            }
        ],
    }


def run_writers(root: Path, store: Path, writers: int, duplicate: bool) -> dict:
    processes = []
    for index in range(writers):
        packet_path = root / f"packet_{'duplicate' if duplicate else 'unique'}_{index}.json"
        packet_path.write_text(
            json.dumps(capture_packet(index, duplicate=duplicate)), encoding="utf-8"
        )
        set_private_path_mode(packet_path, 0o600)
        command = [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(root / "config.json"),
            "--lock-timeout",
            "120",
            "capture",
            "--store",
            str(store),
            "--input",
            str(packet_path),
            "--authority-ceiling",
            "validated_structured_candidate",
            "--producer-agent-id",
            f"benchmark-agent-{index}",
            "--task-id",
            f"benchmark-task-{index}",
            "--based-on-watermark",
            "0",
        ]
        processes.append(
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
            )
        )
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=300)
        outputs.append(
            {
                "returncode": process.returncode,
                "stdout": json.loads(stdout) if stdout.strip() else None,
                "stderr": stderr.strip(),
            }
        )
    failures = [item for item in outputs if item["returncode"]]
    if failures:
        raise RuntimeError(f"Concurrent writer failures: {failures}")
    statuses = [item["stdout"]["status"] for item in outputs]
    return {
        "writers": writers,
        "captured": statuses.count("captured"),
        "duplicate": statuses.count("duplicate"),
    }


def benchmark_size(record_target: int, writers: int, include_projection: bool) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="kg-benchmark-", dir=private_temporary_parent()
    ) as temporary:
        root = Path(temporary)
        set_private_path_mode(root, 0o700)
        store = root / "graph.json"
        graph = build_graph(record_target)
        validation_seconds, validation = seconds(lambda: kg.validate_graph(graph))
        if not validation["valid"]:
            raise ValueError("Generated benchmark graph is invalid: " + "; ".join(validation["errors"]))
        canonical_digest = kg.save_graph(store, graph)
        query = "Benchmark Entity 000123" if record_target >= 625 else "Benchmark Entity 000000"
        scan_seconds, scan_result = seconds(
            lambda: kg.recall_graph(graph, query, 10, 12000, "review_safe")
        )
        index_seconds, index_result = seconds(
            lambda: kg.ensure_recall_index(graph, store, canonical_digest, 120)
        )
        candidate_seconds, candidates = seconds(
            lambda: kg.indexed_recall_candidates(
                kg.recall_index_path(store), query, 10, canonical_digest
            )
        )
        indexed_recall_seconds, indexed_result = seconds(
            lambda: kg.recall_graph(
                graph,
                query,
                10,
                12000,
                "review_safe",
                candidate_ids=candidates,
                skip_validation=True,
                backend="sqlite_fts5",
            )
        )
        projection_seconds = None
        projected_files = None
        if include_projection:
            projection_seconds, notes = seconds(
                lambda: kg.build_projection_notes(
                    graph, "review_safe", "Codex Knowledge", skip_validation=True
                )
            )
            projected_files = len(notes)
        unique_seconds, unique_result = seconds(
            lambda: run_writers(root, store, writers, False)
        )
        duplicate_seconds, duplicate_result = seconds(
            lambda: run_writers(root, store, writers, True)
        )
        final_graph = kg.load_graph(store)
        final_validation = kg.validate_graph(final_graph)
        if not final_validation["valid"]:
            raise ValueError("Concurrent result is invalid: " + "; ".join(final_validation["errors"]))
        return {
            "requested_records": record_target,
            "canonical_bytes": store.stat().st_size,
            "index_bytes": kg.recall_index_path(store).stat().st_size,
            "validate_seconds": validation_seconds,
            "scan_recall_seconds": scan_seconds,
            "scan_result_count": scan_result["result_count"],
            "index_build_seconds": index_seconds,
            "index_status": index_result["status"],
            "index_candidate_seconds": candidate_seconds,
            "index_candidate_count": len(candidates),
            "indexed_recall_seconds": indexed_recall_seconds,
            "indexed_result_count": indexed_result["result_count"],
            "projection_plan_seconds": projection_seconds,
            "projected_files": projected_files,
            "unique_writers_seconds": unique_seconds,
            "unique_writers": unique_result,
            "duplicate_writers_seconds": duplicate_seconds,
            "duplicate_writers": duplicate_result,
            "final_watermark": kg.committed_watermark(final_graph),
            "final_validation": final_validation["valid"],
            "max_rss_kib": round(peak_rss_kib(), 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,20000")
    parser.add_argument("--writers", type=int, default=8)
    parser.add_argument("--skip-projection", action="store_true")
    args = parser.parse_args()
    sizes = [int(value.strip()) for value in args.sizes.split(",") if value.strip()]
    if args.writers < 1 or args.writers > 32:
        raise ValueError("--writers must be between 1 and 32")
    started = time.perf_counter()
    results = [
        benchmark_size(size, args.writers, not args.skip_projection) for size in sizes
    ]
    print(
        json.dumps(
            {
                "schema_version": kg.GRAPH_SCHEMA_VERSION,
                "index_schema_version": kg.INDEX_SCHEMA_VERSION,
                "writers": args.writers,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "results": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
