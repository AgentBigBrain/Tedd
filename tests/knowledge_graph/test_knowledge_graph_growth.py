#!/usr/bin/env python3
"""Growth and multi-writer regression tests for the v3 knowledge graph."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tests.support.paths import GRAPH_SCRIPTS

SCRIPT = GRAPH_SCRIPTS / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_growth_target", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


def private_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    temporary = tempfile.TemporaryDirectory(dir=private_parent)
    try:
        kg.set_private_path_mode(Path(temporary.name), 0o700)
    except Exception:
        temporary.cleanup()
        raise
    return temporary


def source(
    title: str,
    observed_at: str,
    *,
    tier: str = "validated_structured_candidate",
) -> dict[str, object]:
    return {
        "kind": "test_evidence",
        "role": "tool",
        "title": title,
        "observed_at": observed_at,
        "source_tier": tier,
    }


def simple_claim_packet(
    title: str,
    observed_at: str,
    value: str,
    *,
    surface_id: str | None = None,
) -> dict[str, object]:
    claim: dict[str, object] = {
        "family": "deployment",
        "key": "plugin.version",
        "value": value,
        "entity_refs": ["site"],
        "valid_from": observed_at,
        "source_tier": "validated_structured_candidate",
        "conflict_policy": "preserve",
    }
    if surface_id is not None:
        claim["surface_id"] = surface_id
        claim["subject_refs"] = ["site"]
    return {
        "source": source(title, observed_at),
        "entities": [
            {
                "ref": "site",
                "type": "site",
                "namespace": "growth-tests",
                "name": "Growth Test Site",
            }
        ],
        "claims": [claim],
    }


def run_parallel(commands: list[list[str]]) -> list[dict[str, object]]:
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for command in commands
    ]
    results: list[dict[str, object]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=45)
        if process.returncode != 0:
            raise AssertionError(
                f"Concurrent CLI exited {process.returncode}: {stderr}\nstdout: {stdout}"
            )
        results.append(cast(dict[str, object], json.loads(stdout)))
    return results


class KnowledgeGraphGrowthTests(unittest.TestCase):
    def test_v2_migration_is_deterministic_and_preserves_legacy_fingerprints(self) -> None:
        graph = kg.blank_graph()
        result = kg.capture_packet(
            graph,
            simple_claim_packet("Legacy capture", "2026-07-01T12:00:00Z", "1.37.57"),
            authority_ceiling="validated_structured_candidate",
        )
        legacy_fingerprint = result["fingerprint"]
        legacy_claim_id = graph["claims"][0]["id"]
        legacy_entity_id = graph["entities"][0]["id"]

        raw_v2 = copy.deepcopy(graph)
        raw_v2["schema_version"] = "v2"
        raw_v2.pop("claim_surfaces")
        raw_v2.pop("journal_anchor")
        for entity in raw_v2["entities"]:
            entity.pop("identity_keys", None)
            entity.pop("lifecycle", None)
        for claim in raw_v2["claims"]:
            claim.pop("surface_ref", None)
        for entry in raw_v2["journal"]:
            entry.pop("action", None)
            entry.pop("fingerprint_version", None)
            entry.pop("previous_entry_hash", None)
            entry.pop("entry_hash", None)

        first = kg.migrate_graph_v2_to_v3(raw_v2)
        second = kg.migrate_graph_v2_to_v3(raw_v2)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "v3")
        self.assertEqual(first["journal"][0]["fingerprint"], legacy_fingerprint)
        self.assertEqual(first["sources"][0]["fingerprint"], legacy_fingerprint)
        self.assertEqual(first["claims"][0]["id"], legacy_claim_id)
        self.assertEqual(first["entities"][0]["id"], legacy_entity_id)
        self.assertEqual(len(first["claim_surfaces"]), 1)
        self.assertEqual(first["claims"][0]["surface_ref"], first["claim_surfaces"][0]["id"])
        self.assertTrue(kg.validate_graph(first)["valid"])

    def test_cli_migration_requires_reviewed_digest_and_transform_fingerprint(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            simple_claim_packet("Migration gate fixture", "2026-07-01T12:00:00Z", "1.37.58"),
            authority_ceiling="validated_structured_candidate",
        )
        raw_v2 = copy.deepcopy(graph)
        raw_v2["schema_version"] = "v2"
        raw_v2.pop("claim_surfaces")
        raw_v2.pop("journal_anchor")
        for entity in raw_v2["entities"]:
            entity.pop("identity_keys", None)
            entity.pop("lifecycle", None)
        for claim in raw_v2["claims"]:
            claim.pop("surface_ref", None)
        for entry in raw_v2["journal"]:
            for field in (
                "action",
                "fingerprint_version",
                "previous_entry_hash",
                "entry_hash",
            ):
                entry.pop(field, None)

        with private_temporary_directory() as temporary:
            root = Path(temporary)
            store = root / "graph.json"
            config = root / "config.json"
            store.write_text(json.dumps(raw_v2), encoding="utf-8")
            kg.set_private_path_mode(store, 0o600)
            base = [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(config),
                "migrate",
                "--store",
                str(store),
            ]
            dry = json.loads(
                subprocess.run(
                    [*base, "--dry-run"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(dry["status"], "would_migrate")
            missing_gate = subprocess.run(
                [*base, "--expect-sha256", dry["canonical_sha256"]],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing_gate.returncode, 0)
            self.assertEqual(json.loads(store.read_text())["schema_version"], "v2")
            applied = json.loads(
                subprocess.run(
                    [
                        *base,
                        "--expect-sha256",
                        dry["canonical_sha256"],
                        "--expect-migration-fingerprint",
                        dry["migration_fingerprint"],
                        "--producer-agent-id",
                        "migration-test-agent",
                        "--task-id",
                        "migration-gate-test",
                        "--based-on-watermark",
                        str(dry["source_watermark"]),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(applied["status"], "migrated")
            self.assertTrue(Path(applied["backup"]).is_file())
            migrated = kg.load_graph(store)
            self.assertTrue(kg.validate_graph(migrated)["valid"])
            self.assertEqual(
                migrated["journal"][-1]["producer"]["agent_id"],
                "migration-test-agent",
            )

    def test_v3_loader_does_not_repair_missing_required_collections(self) -> None:
        with private_temporary_directory() as temporary:
            store = Path(temporary) / "graph.json"
            raw = kg.blank_graph()
            raw.pop("claims")
            store.write_text(json.dumps(raw), encoding="utf-8")
            kg.set_private_path_mode(store, 0o600)
            loaded, _, stored_version = kg.load_graph_with_digest(store)
            self.assertEqual(stored_version, "v3")
            self.assertNotIn("claims", loaded)
            validation = kg.validate_graph(loaded)
            self.assertFalse(validation["valid"])
            self.assertIn("missing required top-level collection: claims", validation["errors"])

    def test_identity_keys_dedupe_and_trusted_explicit_id_rename_preserves_alias(self) -> None:
        graph = kg.blank_graph()
        first_packet = {
            "source": source("Original task identity", "2026-07-01T12:00:00Z"),
            "entities": [
                {
                    "ref": "task",
                    "type": "task",
                    "namespace": "clickup-tedd",
                    "name": "Client Alpha library build",
                    "identity_keys": [
                        {
                            "scheme": "clickup_task_id",
                            "namespace": "clickup-tedd",
                            "value": "task-test-001",
                        }
                    ],
                }
            ],
        }
        kg.capture_packet(
            graph,
            first_packet,
            authority_ceiling="validated_structured_candidate",
        )
        entity_id = graph["entities"][0]["id"]

        rename_packet = {
            "source": source("Trusted task rename", "2026-07-02T12:00:00Z"),
            "entities": [
                {
                    "id": entity_id,
                    "ref": "task",
                    "type": "task",
                    "namespace": "clickup-tedd",
                    "name": "Client Alpha library staging review",
                    "identity_keys": [
                        {
                            "scheme": "clickup_task_id",
                            "namespace": "clickup-tedd",
                            "value": "task-test-001",
                        }
                    ],
                }
            ],
        }
        kg.capture_packet(
            graph,
            rename_packet,
            authority_ceiling="validated_structured_candidate",
        )

        self.assertEqual(len(graph["entities"]), 1)
        entity = graph["entities"][0]
        self.assertEqual(entity["id"], entity_id)
        self.assertEqual(entity["name"], "Client Alpha library staging review")
        self.assertIn("Client Alpha library build", entity["aliases"])
        self.assertIn("Client Alpha library staging review", entity["aliases"])
        self.assertEqual(len(entity["identity_keys"]), 1)
        self.assertEqual(entity["identity_keys"][0]["value"], "task-test-001")
        conflicting_type = {
            "source": source("Conflicting task type", "2026-07-03T12:00:00Z"),
            "entities": [
                {
                    "type": "repository",
                    "namespace": "clickup-tedd",
                    "name": "Not the same kind of entity",
                    "identity_keys": [
                        {
                            "scheme": "clickup_task_id",
                            "namespace": "clickup-tedd",
                            "value": "task-test-001",
                        }
                    ],
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "immutable namespace and type"):
            kg.capture_packet(
                copy.deepcopy(graph),
                conflicting_type,
                authority_ceiling="validated_structured_candidate",
            )
        self.assertTrue(kg.validate_graph(graph)["valid"])

        case_graph = kg.blank_graph()
        kg.capture_packet(
            case_graph,
            {
                "source": source("Case-sensitive provider IDs", "2026-07-04T12:00:00Z"),
                "entities": [
                    {
                        "type": "thing",
                        "namespace": "provider",
                        "name": "Uppercase opaque ID",
                        "identity_keys": [{"scheme": "provider_id", "value": "AbC123"}],
                    },
                    {
                        "type": "thing",
                        "namespace": "provider",
                        "name": "Lowercase opaque ID",
                        "identity_keys": [{"scheme": "provider_id", "value": "abc123"}],
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(len(case_graph["entities"]), 2)
        self.assertTrue(kg.validate_graph(case_graph)["valid"])

    def test_redirect_requires_gate_canonicalizes_future_refs_and_rejects_cycle(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Two discovered identities", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "duplicate",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Test Stakeholder duplicate",
                    },
                    {
                        "ref": "canonical",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Test Stakeholder",
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        duplicate_id, canonical_id = [item["id"] for item in graph["entities"]]
        redirect_packet = {
            "source": source("Identity reconciliation", "2026-07-02T12:00:00Z"),
            "entity_redirects": [
                {
                    "from": duplicate_id,
                    "to": canonical_id,
                    "reason": "Validated as the same stakeholder.",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "allow-redirect"):
            kg.capture_packet(
                copy.deepcopy(graph),
                redirect_packet,
                authority_ceiling="validated_structured_candidate",
            )
        kg.capture_packet(
            graph,
            redirect_packet,
            authority_ceiling="validated_structured_candidate",
            allow_redirect=True,
        )
        self.assertEqual(kg.canonical_entity_id(graph, duplicate_id), canonical_id)
        duplicate = kg.index_by_id(graph["entities"])[duplicate_id]
        self.assertEqual(duplicate["lifecycle"], "redirected")

        kg.capture_packet(
            graph,
            {
                "source": source("Post-redirect event", "2026-07-03T12:00:00Z"),
                "events": [
                    {
                        "kind": "review",
                        "title": "Test Stakeholder reviewed staging",
                        "entity_refs": [duplicate_id],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(graph["events"][-1]["entity_refs"], [canonical_id])

        cycle_packet = {
            "source": source("Invalid reverse redirect", "2026-07-04T12:00:00Z"),
            "entity_redirects": [
                {
                    "from": canonical_id,
                    "to": duplicate_id,
                    "reason": "Would create a cycle.",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "target itself"):
            kg.capture_packet(
                copy.deepcopy(graph),
                cycle_packet,
                authority_ceiling="validated_structured_candidate",
                allow_redirect=True,
            )
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_explicit_claim_surface_is_shared_but_immutable(self) -> None:
        graph = kg.blank_graph()
        surface_id = "surface_client_alpha_library_plugin_version"
        kg.capture_packet(
            graph,
            simple_claim_packet(
                "Initial plugin version",
                "2026-07-01T12:00:00Z",
                "1.37.57",
                surface_id=surface_id,
            ),
            authority_ceiling="validated_structured_candidate",
        )
        site_id = graph["entities"][0]["id"]

        kg.capture_packet(
            graph,
            {
                "source": source("Staging plugin version", "2026-07-02T12:00:00Z"),
                "entities": [
                    {
                        "ref": "context",
                        "type": "environment",
                        "namespace": "growth-tests",
                        "name": "Staging",
                    }
                ],
                "claims": [
                    {
                        "family": "deployment",
                        "key": "plugin.version",
                        "surface_id": surface_id,
                        "value": "1.37.58",
                        "entity_refs": ["context"],
                        "source_tier": "validated_structured_candidate",
                        "conflict_policy": "preserve",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )

        self.assertEqual(len(graph["claim_surfaces"]), 1)
        self.assertEqual(graph["claim_surfaces"][0]["subject_refs"], [site_id])
        self.assertEqual({claim["surface_ref"] for claim in graph["claims"]}, {surface_id})
        self.assertEqual(len(graph["claims"]), 2)

        immutable_change = {
            "source": source("Invalid surface mutation", "2026-07-03T12:00:00Z"),
            "claims": [
                {
                    "family": "deployment",
                    "key": "theme.version",
                    "surface_id": surface_id,
                    "subject_refs": [site_id],
                    "value": "9.9.9",
                    "entity_refs": [site_id],
                    "source_tier": "validated_structured_candidate",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "immutable"):
            kg.capture_packet(
                copy.deepcopy(graph),
                immutable_change,
                authority_ceiling="validated_structured_candidate",
            )
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_journal_hash_chain_detects_tampering(self) -> None:
        graph = kg.blank_graph()
        for index in range(2):
            kg.capture_packet(
                graph,
                {
                    "source": source(
                        f"Journal evidence {index}", f"2026-07-0{index + 1}T12:00:00Z"
                    ),
                    "events": [
                        {
                            "kind": "test",
                            "title": f"Journal event {index}",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
            )
        self.assertTrue(kg.validate_graph(graph)["valid"])

        tampered = copy.deepcopy(graph)
        tampered["journal"][0]["action"] = "tampered_capture"
        validation = kg.validate_graph(tampered)
        self.assertFalse(validation["valid"])
        self.assertTrue(
            any("invalid journal entry hash" in error for error in validation["errors"])
        )

    def test_sqlite_fts_exact_lookup_and_stale_digest_guard(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            store = root / "graph.json"
            graph = kg.blank_graph()
            kg.capture_packet(
                graph,
                simple_claim_packet(
                    "Client Alpha library searchable evidence",
                    "2026-07-01T12:00:00Z",
                    "1.37.58",
                ),
                authority_ceiling="validated_structured_candidate",
            )
            first_digest = kg.save_graph(store, graph)
            index_result = kg.ensure_recall_index(graph, store, first_digest)
            self.assertEqual(index_result["status"], "rebuilt")
            index_path = kg.recall_index_path(store)
            claim_id = graph["claims"][0]["id"]

            fts_candidates = kg.indexed_recall_candidates(
                index_path, "plugin version", 10, first_digest
            )
            self.assertIn(claim_id, fts_candidates)
            exact_candidates = kg.indexed_recall_candidates(index_path, claim_id, 10, first_digest)
            self.assertIn(claim_id, exact_candidates)
            exact = kg.get_indexed_graph_record(
                graph,
                index_path,
                claim_id,
                "review_safe",
                first_digest,
            )
            self.assertEqual(exact["id"], claim_id)
            self.assertEqual(exact["collection"], "claims")
            self.assertEqual(exact["backend"], "sqlite_exact")

            connection = sqlite3.connect(index_path)
            try:
                connection.execute(
                    "UPDATE records SET search_text = ?, content_hash = ? WHERE record_id = ?",
                    ("tampered read-model text", "0" * 64, claim_id),
                )
                connection.commit()
            finally:
                connection.close()
            canonical_exact = kg.get_indexed_graph_record(
                graph,
                index_path,
                claim_id,
                "operator_full",
                first_digest,
            )
            self.assertEqual(canonical_exact["record"]["value"], graph["claims"][0]["value"])
            self.assertNotIn("tampered", json.dumps(canonical_exact))

            kg.capture_packet(
                graph,
                {
                    "source": source("New searchable event", "2026-07-02T12:00:00Z"),
                    "events": [
                        {
                            "kind": "verification",
                            "title": "Fresh Watermark Result",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
            )
            second_digest = kg.save_graph(store, graph)
            self.assertNotEqual(first_digest, second_digest)
            with self.assertRaisesRegex(ValueError, "different canonical snapshot"):
                kg.indexed_recall_candidates(
                    index_path, "Fresh Watermark Result", 10, second_digest
                )

            rebuilt = kg.ensure_recall_index(graph, store, second_digest)
            self.assertEqual(rebuilt["status"], "rebuilt")
            fresh_candidates = kg.indexed_recall_candidates(
                index_path, "Fresh Watermark Result", 10, second_digest
            )
            self.assertIn(graph["events"][-1]["id"], fresh_candidates)

    def test_projection_second_run_is_noop_and_older_watermark_cannot_regress(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            simple_claim_packet("Projection first snapshot", "2026-07-01T12:00:00Z", "1.37.57"),
            authority_ceiling="validated_structured_candidate",
        )
        old_graph = copy.deepcopy(graph)
        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            kg.set_private_path_mode(vault, 0o700)
            key = "7" * 64
            first = kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            second = kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            self.assertGreater(first["written_files"], 0)
            self.assertEqual(second["written_files"], 0)
            self.assertEqual(second["unchanged_files"], second["generated_files"])

            kg.capture_packet(
                graph,
                {
                    "source": source("Projection second snapshot", "2026-07-02T12:00:00Z"),
                    "events": [{"kind": "verification", "title": "Newer projection event"}],
                },
                authority_ceiling="validated_structured_candidate",
            )
            newer = kg.project_graph(graph, vault, "Codex Knowledge", "review_safe", key)
            stale = kg.project_graph(old_graph, vault, "Codex Knowledge", "review_safe", key)
            self.assertEqual(newer["status"], "projected")
            self.assertEqual(stale["status"], "stale_projection_skipped")
            self.assertEqual(stale["projection_digest"], kg.stable_hash(old_graph, 64))
            self.assertEqual(stale["projected_watermark"], kg.committed_watermark(graph))
            manifest = json.loads(
                (vault / "Codex Knowledge" / kg.PROJECTION_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["graph_watermark"], kg.committed_watermark(graph))

    def test_cli_concurrent_unique_and_duplicate_writers(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            unique_store = root / "unique-graph.json"
            unique_commands: list[list[str]] = []
            for index in range(4):
                packet_path = root / f"unique-{index}.json"
                packet_path.write_text(
                    json.dumps(
                        {
                            "source": source(
                                f"Concurrent source {index}",
                                f"2026-07-0{index + 1}T12:00:00Z",
                            ),
                            "events": [
                                {
                                    "kind": "concurrency_test",
                                    "title": f"Concurrent event {index}",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                packet_path.chmod(0o600)
                unique_commands.append(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--config",
                        str(config),
                        "capture",
                        "--store",
                        str(unique_store),
                        "--input",
                        str(packet_path),
                        "--authority-ceiling",
                        "validated_structured_candidate",
                        "--producer-agent-id",
                        f"growth-agent-{index}",
                    ]
                )
            unique_results = run_parallel(unique_commands)
            self.assertEqual([result["status"] for result in unique_results].count("captured"), 4)
            unique_graph = kg.load_graph(unique_store)
            self.assertEqual(len(unique_graph["events"]), 4)
            self.assertEqual(len(unique_graph["journal"]), 4)
            self.assertEqual(
                {entry.get("producer", {}).get("agent_id") for entry in unique_graph["journal"]},
                {f"growth-agent-{index}" for index in range(4)},
            )
            self.assertTrue(kg.validate_graph(unique_graph)["valid"])

            duplicate_store = root / "duplicate-graph.json"
            duplicate_packet = root / "duplicate.json"
            duplicate_packet.write_text(
                json.dumps(
                    {
                        "source": source("Shared discovery", "2026-07-10T12:00:00Z"),
                        "events": [
                            {
                                "kind": "concurrency_test",
                                "title": "One shared event",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            duplicate_packet.chmod(0o600)
            duplicate_commands = [
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "capture",
                    "--store",
                    str(duplicate_store),
                    "--input",
                    str(duplicate_packet),
                    "--authority-ceiling",
                    "validated_structured_candidate",
                    "--producer-agent-id",
                    f"duplicate-agent-{index}",
                ]
                for index in range(4)
            ]
            duplicate_results = run_parallel(duplicate_commands)
            statuses = [result["status"] for result in duplicate_results]
            self.assertEqual(statuses.count("captured"), 1)
            self.assertEqual(statuses.count("duplicate"), 3)
            duplicate_graph = kg.load_graph(duplicate_store)
            self.assertEqual(len(duplicate_graph["events"]), 1)
            self.assertEqual(len(duplicate_graph["journal"]), 1)
            self.assertTrue(kg.validate_graph(duplicate_graph)["valid"])

    def test_validator_reports_malformed_nested_fields_without_throwing(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Validator fixture", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "site",
                        "type": "site",
                        "namespace": "growth-tests",
                        "name": "Validator Site",
                        "identity_keys": [
                            {
                                "scheme": "site_id",
                                "namespace": "growth-tests",
                                "value": "validator-site",
                            }
                        ],
                    }
                ],
                "events": [
                    {
                        "kind": "verification",
                        "title": "Validator event",
                        "entity_refs": ["site"],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        self.assertTrue(kg.validate_graph(graph)["valid"])

        malformed_cases = {
            "lifecycle": lambda item: item["entities"][0].__setitem__(
                "lifecycle", {"not": "a string"}
            ),
            "source_tier": lambda item: item["entities"][0].__setitem__(
                "source_tier", ["validated_structured_candidate"]
            ),
            "entity_refs": lambda item: item["events"][0].__setitem__(
                "entity_refs", {"not": "a list"}
            ),
            "aliases": lambda item: item["entities"][0].__setitem__("aliases", {"not": "a list"}),
            "nested_identity_evidence": lambda item: item["entities"][0]["identity_keys"][
                0
            ].__setitem__("evidence_refs", {"not": "a list"}),
        }
        for label, mutate in malformed_cases.items():
            with self.subTest(label=label):
                malformed = copy.deepcopy(graph)
                mutate(malformed)
                validation = kg.validate_graph(malformed)
                self.assertFalse(validation["valid"])
                self.assertTrue(validation["errors"])

    def test_corrupt_current_index_is_rebuilt_and_force_reindex_remains_compatible(self) -> None:
        with private_temporary_directory() as temporary:
            root = Path(temporary)
            store = root / "graph.json"
            config = root / "config.json"
            graph = kg.blank_graph()
            kg.capture_packet(
                graph,
                simple_claim_packet(
                    "Index hardening fixture",
                    "2026-07-01T12:00:00Z",
                    "1.37.58",
                ),
                authority_ceiling="validated_structured_candidate",
            )
            digest = kg.save_graph(store, graph)
            first = kg.ensure_recall_index(graph, store, digest)
            self.assertEqual(first["status"], "rebuilt")
            index_path = kg.recall_index_path(store)
            self.assertTrue(kg.recall_index_healthy(index_path))

            connection = sqlite3.connect(index_path)
            try:
                connection.execute("DROP TABLE records_fts")
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(kg.recall_index_healthy(index_path))

            repaired = kg.ensure_recall_index(graph, store, digest)
            self.assertEqual(repaired["status"], "rebuilt")
            self.assertTrue(kg.recall_index_healthy(index_path))

            # The appended force argument remains keyword- and CLI-compatible.
            forced = kg.ensure_recall_index(graph, store, digest, force=True)
            self.assertEqual(forced["status"], "rebuilt")
            cli_forced = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--config",
                        str(config),
                        "reindex",
                        "--store",
                        str(store),
                        "--force",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            self.assertEqual(cli_forced["status"], "rebuilt")
            self.assertTrue(kg.recall_index_healthy(index_path))

    def test_old_redirect_alias_reaches_canonical_current_record_in_both_backends(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Redirect identities", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "old",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Test Stakeholder Legacy Record",
                        "aliases": ["Old Stakeholder Alias"],
                    },
                    {
                        "ref": "current",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Test Stakeholder",
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        old_id, canonical_id = [item["id"] for item in graph["entities"]]
        kg.capture_packet(
            graph,
            {
                "source": source("Redirect validation", "2026-07-02T12:00:00Z"),
                "entity_redirects": [
                    {
                        "from": old_id,
                        "to": canonical_id,
                        "reason": "Validated duplicate stakeholder identity.",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
            allow_redirect=True,
        )
        kg.capture_packet(
            graph,
            {
                "source": source("Canonical current status", "2026-07-03T12:00:00Z"),
                "claims": [
                    {
                        "family": "review",
                        "key": "staging.status",
                        "value": "approved",
                        "entity_refs": [canonical_id],
                        "source_tier": "validated_structured_candidate",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        claim_id = graph["claims"][0]["id"]

        scanned = kg.recall_graph(graph, "Old Stakeholder Alias", 20, 20000, backend="scan")
        self.assertIn(claim_id, {item["id"] for item in scanned["results"]})
        self.assertIn(old_id, scanned["seed_entities"])
        self.assertIn(canonical_id, scanned["seed_entities"])

        with private_temporary_directory() as temporary:
            store = Path(temporary) / "graph.json"
            digest = kg.save_graph(store, graph)
            kg.ensure_recall_index(graph, store, digest)
            candidates = kg.indexed_recall_candidates(
                kg.recall_index_path(store),
                "Old Stakeholder Alias",
                20,
                digest,
            )
            indexed = kg.recall_graph(
                graph,
                "Old Stakeholder Alias",
                20,
                20000,
                candidate_ids=candidates,
                skip_validation=True,
                backend="sqlite_fts5",
            )
            self.assertIn(claim_id, {item["id"] for item in indexed["results"]})
            self.assertIn(canonical_id, candidates)

    def test_redirect_preflight_rejects_distinct_claim_surface_collapse(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Two scoped entities", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "one",
                        "type": "site",
                        "namespace": "same-client",
                        "name": "Duplicate Site One",
                    },
                    {
                        "ref": "two",
                        "type": "site",
                        "namespace": "same-client",
                        "name": "Duplicate Site Two",
                    },
                ],
                "claims": [
                    {
                        "family": "deployment",
                        "key": "plugin.version",
                        "value": "1.0",
                        "entity_refs": ["one"],
                        "source_tier": "validated_structured_candidate",
                    },
                    {
                        "family": "deployment",
                        "key": "plugin.version",
                        "value": "1.0",
                        "entity_refs": ["two"],
                        "source_tier": "validated_structured_candidate",
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        first_id, second_id = [item["id"] for item in graph["entities"]]
        with self.assertRaisesRegex(ValueError, "collapse distinct claim surfaces"):
            kg.capture_packet(
                copy.deepcopy(graph),
                {
                    "source": source("Unsafe identity collapse", "2026-07-02T12:00:00Z"),
                    "entity_redirects": [
                        {
                            "from": first_id,
                            "to": second_id,
                            "reason": "Would collide two independently tracked surfaces.",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
                allow_redirect=True,
            )
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_redirected_subject_reuses_existing_canonical_claim_surface(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Surface redirect identities", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "old",
                        "type": "site",
                        "namespace": "growth-tests",
                        "name": "Old Site Identity",
                    },
                    {
                        "ref": "canonical",
                        "type": "site",
                        "namespace": "growth-tests",
                        "name": "Canonical Site Identity",
                    },
                ],
                "claims": [
                    {
                        "family": "release",
                        "key": "status",
                        "value": "reviewing",
                        "entity_refs": ["old"],
                        "source_tier": "validated_structured_candidate",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        old_id, canonical_id = [entity["id"] for entity in graph["entities"]]
        original_surface_id = graph["claim_surfaces"][0]["id"]
        kg.capture_packet(
            graph,
            {
                "source": source("Surface redirect evidence", "2026-07-02T12:00:00Z"),
                "entity_redirects": [
                    {
                        "from": old_id,
                        "to": canonical_id,
                        "reason": "Validated duplicate site identity.",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
            allow_redirect=True,
        )
        kg.capture_packet(
            graph,
            {
                "source": source("Post-redirect claim", "2026-07-03T12:00:00Z"),
                "claims": [
                    {
                        "family": "release",
                        "key": "status",
                        "value": "approved",
                        "entity_refs": [old_id],
                        "source_tier": "validated_structured_candidate",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        self.assertEqual(len(graph["claim_surfaces"]), 1)
        self.assertTrue(
            all(claim["surface_ref"] == original_surface_id for claim in graph["claims"])
        )
        self.assertTrue(kg.validate_graph(graph)["valid"])

    def test_paginated_change_feed_is_stable_lossless_and_query_filter_does_not_advance(
        self,
    ) -> None:
        graph = kg.blank_graph()

        def capture_event(index: int, label: str) -> None:
            kg.capture_packet(
                graph,
                {
                    "source": source(f"{label} source {index}", f"2026-07-0{index}T12:00:00Z"),
                    "events": [
                        {
                            "kind": "change_feed_test",
                            "title": f"{label} event {index}",
                        }
                    ],
                },
                authority_ceiling="validated_structured_candidate",
                producer={
                    "agent_id": f"agent-{index}",
                    "task_id": f"task-{index}",
                    "parent_task_id": "parent-growth-test",
                },
                based_on_watermark=kg.committed_watermark(graph),
            )

        capture_event(1, "Alpha")
        capture_event(2, "Beta")
        capture_event(3, "Alpha")
        first_page = kg.change_feed_since(graph, 0, limit=2)
        snapshot = first_page["snapshot_watermark"]
        self.assertEqual(snapshot, 3)
        self.assertFalse(first_page["complete"])
        self.assertIsNone(first_page["advance_to_watermark"])
        self.assertEqual(first_page["changes"][0]["producer"]["agent_id"], "agent-1")
        self.assertEqual(first_page["changes"][0]["based_on_watermark"], 0)

        # A later commit must not leak into pages pinned to the first snapshot.
        capture_event(4, "Gamma")
        expected_snapshot_ids = [
            changed_id
            for entry in graph["journal"]
            if entry["watermark"] <= snapshot
            for changed_id in entry["changed_ids"]
        ]
        observed_ids = [change["record_id"] for change in first_page["changes"]]
        cursor = first_page["next_cursor"]
        final_page = first_page
        while cursor is not None:
            page = kg.change_feed_since(
                graph,
                0,
                through_watermark=snapshot,
                limit=2,
                cursor=cursor,
            )
            self.assertEqual(page["snapshot_watermark"], snapshot)
            observed_ids.extend(change["record_id"] for change in page["changes"])
            final_page = page
            cursor = page["next_cursor"]
        self.assertEqual(observed_ids, expected_snapshot_ids)
        self.assertTrue(final_page["complete"])
        self.assertEqual(final_page["advance_to_watermark"], snapshot)
        with self.assertRaisesRegex(ValueError, "continuation cursor"):
            kg.change_feed_since(
                graph,
                0,
                through_watermark=snapshot,
                offset=10_000,
                limit=2,
            )

        catch_up = kg.change_feed_since(graph, snapshot, limit=500)
        expected_catch_up_ids = [
            changed_id
            for entry in graph["journal"]
            if entry["watermark"] > snapshot
            for changed_id in entry["changed_ids"]
        ]
        self.assertEqual(
            [change["record_id"] for change in catch_up["changes"]],
            expected_catch_up_ids,
        )
        self.assertEqual(catch_up["advance_to_watermark"], kg.committed_watermark(graph))

        alpha_candidates = kg.scan_query_candidate_ids(graph, "Alpha")
        filtered = kg.change_feed_since(
            graph,
            0,
            limit=500,
            candidate_ids=alpha_candidates,
        )
        self.assertTrue(filtered["complete"])
        self.assertTrue(filtered["filtered_by_query"])
        self.assertIsNone(filtered["advance_to_watermark"])
        self.assertTrue(filtered["changes"])
        self.assertTrue(
            all(change["record_id"] in alpha_candidates for change in filtered["changes"])
        )
        beta_ids = {
            changed_id
            for entry in graph["journal"]
            if entry["watermark"] == 2
            for changed_id in entry["changed_ids"]
        }
        self.assertFalse(beta_ids & {change["record_id"] for change in filtered["changes"]})

        filtered_page = kg.change_feed_since(
            graph,
            0,
            limit=1,
            candidate_ids=alpha_candidates,
        )
        self.assertIsNotNone(filtered_page["next_cursor"])
        capture_event(5, "Alpha")
        changed_alpha_candidates = kg.scan_query_candidate_ids(graph, "Alpha")
        with self.assertRaisesRegex(ValueError, "restart the filtered inspection"):
            kg.change_feed_since(
                graph,
                0,
                through_watermark=filtered_page["snapshot_watermark"],
                limit=1,
                candidate_ids=changed_alpha_candidates,
                cursor=filtered_page["next_cursor"],
            )

    def test_filtered_change_feed_neighbor_expansion_matches_scan_and_index(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Neighbor feed fixture", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "alpha",
                        "type": "service",
                        "namespace": "growth-tests",
                        "name": "Alpha Service",
                    },
                    {
                        "ref": "beta",
                        "type": "service",
                        "namespace": "growth-tests",
                        "name": "Beta Service",
                    },
                ],
                "relations": [
                    {
                        "source": "alpha",
                        "target": "beta",
                        "type": "depends_on",
                    }
                ],
                "events": [
                    {
                        "kind": "verification",
                        "title": "Beta-only neighbor event",
                        "entity_refs": ["beta"],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        event_id = graph["events"][0]["id"]
        scan_candidates = kg.scan_query_candidate_ids(graph, "Alpha Service")
        self.assertIn(event_id, scan_candidates)
        with private_temporary_directory() as temporary:
            store = Path(temporary) / "graph.json"
            digest = kg.save_graph(store, graph)
            kg.ensure_recall_index(graph, store, digest)
            indexed_candidates = kg.indexed_recall_candidates(
                kg.recall_index_path(store),
                "Alpha Service",
                20,
                digest,
                exhaustive=True,
            )
        self.assertIn(event_id, indexed_candidates)

    def test_projection_lower_watermark_recovery_requires_exact_current_digest(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            simple_claim_packet("Projection rollback base", "2026-07-01T12:00:00Z", "1.37.57"),
            authority_ceiling="validated_structured_candidate",
        )
        restored_graph = copy.deepcopy(graph)
        restored_digest = kg.stable_hash(restored_graph, 64)
        kg.capture_packet(
            graph,
            {
                "source": source("Newer projected change", "2026-07-02T12:00:00Z"),
                "events": [{"kind": "verification", "title": "Newer projected event"}],
            },
            authority_ceiling="validated_structured_candidate",
        )
        newer_digest = kg.stable_hash(graph, 64)

        with private_temporary_directory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            kg.set_private_path_mode(vault, 0o700)
            key = "8" * 64
            kg.project_graph(
                graph,
                vault,
                "Codex Knowledge",
                "review_safe",
                key,
                graph_sha256=newer_digest,
            )
            skipped = kg.project_graph(
                restored_graph,
                vault,
                "Codex Knowledge",
                "review_safe",
                key,
                graph_sha256=restored_digest,
            )
            self.assertEqual(skipped["status"], "stale_projection_skipped")
            self.assertEqual(skipped["projection_digest"], restored_digest)
            with self.assertRaisesRegex(ValueError, "does not match"):
                kg.project_graph(
                    restored_graph,
                    vault,
                    "Codex Knowledge",
                    "review_safe",
                    key,
                    graph_sha256=restored_digest,
                    force_current_digest="0" * 64,
                )

            recovered = kg.project_graph(
                restored_graph,
                vault,
                "Codex Knowledge",
                "review_safe",
                key,
                graph_sha256=restored_digest,
                force_current_digest=restored_digest,
            )
            self.assertEqual(recovered["status"], "projected")
            manifest = json.loads(
                (vault / "Codex Knowledge" / kg.PROJECTION_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["graph_watermark"], kg.committed_watermark(restored_graph))
            self.assertEqual(manifest["graph_sha256"], restored_digest)

    def test_review_safe_hides_nested_identity_and_redirect_lineage(self) -> None:
        graph = kg.blank_graph()
        kg.capture_packet(
            graph,
            {
                "source": source("Identity lineage source", "2026-07-01T12:00:00Z"),
                "entities": [
                    {
                        "ref": "old",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Old identity",
                        "identity_keys": [
                            {
                                "scheme": "crm_contact_id",
                                "namespace": "stakeholders",
                                "value": "contact-old",
                            }
                        ],
                    },
                    {
                        "ref": "current",
                        "type": "person",
                        "namespace": "stakeholders",
                        "name": "Current identity",
                        "identity_keys": [
                            {
                                "scheme": "crm_contact_id",
                                "namespace": "stakeholders",
                                "value": "contact-current",
                            }
                        ],
                    },
                ],
            },
            authority_ceiling="validated_structured_candidate",
        )
        old_id, current_id = [item["id"] for item in graph["entities"]]
        kg.capture_packet(
            graph,
            {
                "source": source("Redirect lineage source", "2026-07-02T12:00:00Z"),
                "entity_redirects": [
                    {
                        "from": old_id,
                        "to": current_id,
                        "reason": "Validated identity correction.",
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
            allow_redirect=True,
        )
        redirected = kg.index_by_id(graph["entities"])[old_id]
        identity_source = redirected["identity_keys"][0]["evidence_refs"][0]
        redirect_source = redirected["redirect_source_ref"]

        safe = kg.recall_record(redirected, "review_safe")
        full = kg.recall_record(redirected, "operator_full")
        self.assertEqual(
            safe["identity_keys"][0]["evidence_refs"],
            ["[withheld in review_safe mode]"],
        )
        self.assertEqual(safe["redirect_source_ref"], "[withheld in review_safe mode]")
        safe_json = json.dumps(safe, sort_keys=True)
        self.assertNotIn(identity_source, safe_json)
        self.assertNotIn(redirect_source, safe_json)
        self.assertEqual(full["identity_keys"][0]["evidence_refs"][0], identity_source)
        self.assertEqual(full["redirect_source_ref"], redirect_source)


if __name__ == "__main__":
    unittest.main()
