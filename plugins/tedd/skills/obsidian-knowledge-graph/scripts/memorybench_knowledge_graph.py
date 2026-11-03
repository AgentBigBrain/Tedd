#!/usr/bin/env python3
"""Deterministic, read-only quality gates for Tedd's production recall path.

Version 1 measures the sibling CLI recall path. Version 2 can instead measure
actual MCP search or the delivered overdue/preference context sections, using
existing exact readers. Setup is separate: the runner never registers readers,
changes subscriptions, acknowledges changes, or writes canonical graph state.

Exit codes:
  0: all hard gates and configured aggregate thresholds passed
  1: a stable-snapshot quality or safety gate failed
  2: invalid suite, CLI/infrastructure error, or concurrent graph change
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from platform_support import (
    is_link_or_reparse_point,
    mode_has_any,
    owner_is_current_user,
    set_private_fd_mode,
    set_private_path_mode,
)

SCRIPT_DIR = Path(__file__).resolve().parent
KG_CLI = SCRIPT_DIR / "knowledge_graph.py"
DEFAULT_SUITE = (
    Path.home() / ".codex" / "knowledge-graph" / "memorybench" / "cases.json"
)

SCHEMA_VERSION = "tedd-memorybench-v1"
REPORT_SCHEMA_VERSION = "tedd-memorybench-report-v1"
SCHEMA_VERSION_V2 = "tedd-memorybench-v2"
REPORT_SCHEMA_VERSION_V2 = "tedd-memorybench-report-v2"
READ_PATHS = {"cli_recall", "mcp_search_memory", "mcp_recall_context"}
SESSION_KEYS = {"consumer_id", "agent_id", "task_id", "subscription_id"}
MAX_SUITE_BYTES = 1_000_000
MAX_CASES = 500
MAX_QUERY_CHARS = 2_000
MAX_RECALL_LIMIT = 100  # KnowledgeGraphV4.search clamps silently above 100.
MAX_CONTEXT_CHARS = 100_000
MAX_REPEAT = 20
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
SAFE_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SAFE_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
COLLECTIONS = {
    "entities",
    "relations",
    "observations",
    "claim_surfaces",
    "claims",
    "events",
    "decisions",
    "open_loops",
    "receipts",
    "sources",
}

TOP_LEVEL_KEYS = {
    "schema_version",
    "suite_id",
    "expected_store_id",
    "expected_generation",
    "defaults",
    "thresholds",
    "cases",
}
DEFAULT_KEYS = {"mode", "limit", "max_chars"}
THRESHOLD_KEYS = {
    "min_macro_precision_at_k",
    "min_macro_recall_at_k",
    "min_macro_mrr",
}
CASE_KEYS = {
    "id",
    "query",
    "limit",
    "max_chars",
    "subject_ids",
    "collections",
    "relevant_record_ids",
    "relevant_surface_ids",
    "required",
    "ranking_pairs",
    "forbidden_record_ids",
    "forbidden_reference_ids",
    "forbidden_namespaces",
    "forbidden_output_substrings",
    "max_result_count",
}
REQUIRED_KEYS = {"record_id", "surface_id", "max_rank", "collection", "payload_equals"}
RANKING_PAIR_KEYS = {"higher_record_id", "lower_record_id"}


class MemoryBenchError(ValueError):
    """Safe, user-facing benchmark configuration or execution error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 3)


def mean(values: Iterable[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 6)


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryBenchError(f"{label} must be a JSON object")
    return dict(value)


def reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise MemoryBenchError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryBenchError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise MemoryBenchError(f"{label} exceeds {maximum} characters")
    return text


def bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryBenchError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise MemoryBenchError(f"{label} must be between {minimum} and {maximum}")
    return int(value)


def bounded_ratio(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryBenchError(f"{label} must be a number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise MemoryBenchError(f"{label} must be between 0 and 1")
    return number


def identifier(value: Any, label: str, *, record: bool = True) -> str:
    text = bounded_text(value, label, 200)
    pattern = SAFE_RECORD_ID if record else SAFE_OPAQUE_ID
    if not pattern.fullmatch(text):
        raise MemoryBenchError(f"{label} must be an opaque ASCII identifier")
    return text


def unique_identifiers(
    value: Any, label: str, *, record: bool = True, maximum: int = 500
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MemoryBenchError(f"{label} must be a JSON array")
    if len(value) > maximum:
        raise MemoryBenchError(f"{label} exceeds {maximum} items")
    result = [identifier(item, f"{label} item", record=record) for item in value]
    if len(set(result)) != len(result):
        raise MemoryBenchError(f"{label} contains duplicate identifiers")
    return result


def safe_private_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    expanded = path.expanduser()
    if is_link_or_reparse_point(expanded):
        raise MemoryBenchError(f"{label} must be a regular non-symlink file: {expanded}")
    resolved = expanded.resolve()
    if not resolved.is_file():
        raise MemoryBenchError(f"{label} must be a regular non-symlink file: {resolved}")
    info = resolved.stat()
    if not owner_is_current_user(resolved):
        raise MemoryBenchError(f"{label} must be owned by the current user")
    if mode_has_any(resolved, stat.S_IRWXG | stat.S_IRWXO):
        raise MemoryBenchError(f"{label} must not be accessible by group or other users")
    if info.st_size > MAX_SUITE_BYTES:
        raise MemoryBenchError(f"{label} exceeds {MAX_SUITE_BYTES} bytes")
    payload = resolved.read_bytes()
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryBenchError(f"{label} is not valid UTF-8 JSON") from error
    return require_mapping(parsed, label), payload


def normalize_required(value: Any, case_id: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 500:
        raise MemoryBenchError(f"case {case_id} required must be an array of at most 500 items")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = require_mapping(raw, f"case {case_id} required[{index}]")
        reject_unknown(item, REQUIRED_KEYS, f"case {case_id} required[{index}]")
        has_record = item.get("record_id") is not None
        has_surface = item.get("surface_id") is not None
        if has_record == has_surface:
            raise MemoryBenchError(
                f"case {case_id} required[{index}] needs exactly one of record_id or surface_id"
            )
        normalized: dict[str, Any] = {}
        if has_record:
            normalized["record_id"] = identifier(
                item["record_id"], f"case {case_id} required record_id"
            )
        else:
            normalized["surface_id"] = identifier(
                item["surface_id"], f"case {case_id} required surface_id"
            )
        normalized["max_rank"] = bounded_int(
            item.get("max_rank", MAX_RECALL_LIMIT),
            f"case {case_id} required max_rank",
            1,
            MAX_RECALL_LIMIT,
        )
        if item.get("collection") is not None:
            collection = bounded_text(item["collection"], "required collection", 50)
            if collection not in COLLECTIONS:
                raise MemoryBenchError(f"case {case_id} required collection is unknown")
            normalized["collection"] = collection
        if item.get("payload_equals") is not None:
            assertions = require_mapping(
                item["payload_equals"], f"case {case_id} required payload_equals"
            )
            if len(assertions) > 30:
                raise MemoryBenchError(f"case {case_id} has too many payload assertions")
            for key, expected in assertions.items():
                bounded_text(key, f"case {case_id} payload assertion field", 100)
                if isinstance(expected, (dict, list)):
                    raise MemoryBenchError(
                        f"case {case_id} payload assertions must use scalar JSON values"
                    )
            normalized["payload_equals"] = assertions
        result.append(normalized)
    return result


def normalize_case(
    raw: Any, defaults: Mapping[str, Any], index: int, *, read_path: str = "cli_recall"
) -> dict[str, Any]:
    item = require_mapping(raw, f"case[{index}]")
    extra_keys = {"session", "context_section", "max_items"} if read_path != "cli_recall" else set()
    reject_unknown(item, CASE_KEYS | extra_keys, f"case[{index}]")
    case_id = identifier(item.get("id"), f"case[{index}] id", record=False)
    context = read_path == "mcp_recall_context"
    section = item.get("context_section")
    if context:
        if not isinstance(section, str) or section not in {"overdue", "preferences"}:
            raise MemoryBenchError("context_section must be overdue or preferences")
        if any(key in item for key in ("query", "limit")):
            raise MemoryBenchError("context cases use max_items, not query or limit")
    elif "context_section" in item or "max_items" in item:
        raise MemoryBenchError("context_section and max_items require mcp_recall_context")
    query = (
        "" if context else bounded_text(item.get("query"), f"case {case_id} query", MAX_QUERY_CHARS)
    )
    limit = bounded_int(
        (5 if section == "overdue" else 12) if context else item.get("limit", defaults["limit"]),
        f"case {case_id} limit",
        1,
        MAX_RECALL_LIMIT,
    )
    max_chars = bounded_int(
        item.get("max_chars", defaults["max_chars"]),
        f"case {case_id} max_chars",
        1,
        MAX_CONTEXT_CHARS,
    )
    collections = item.get("collections") or []
    if not isinstance(collections, list) or len(collections) > len(COLLECTIONS):
        raise MemoryBenchError(f"case {case_id} collections must be a bounded array")
    normalized_collections: list[str] = []
    for value in collections:
        collection = bounded_text(value, f"case {case_id} collection", 50)
        if collection not in COLLECTIONS:
            raise MemoryBenchError(f"case {case_id} collection is unknown: {collection}")
        normalized_collections.append(collection)
    if len(set(normalized_collections)) != len(normalized_collections):
        raise MemoryBenchError(f"case {case_id} contains duplicate collections")
    session = None
    if read_path != "cli_recall":
        if item.get("subject_ids") or normalized_collections:
            raise MemoryBenchError("MCP scope comes from session, not subject_ids or collections")
        raw_session = require_mapping(item.get("session"), f"case {case_id} session")
        if set(raw_session) != SESSION_KEYS:
            raise MemoryBenchError(
                "session requires exactly consumer_id, agent_id, task_id and subscription_id"
            )
        session = {
            key: identifier(raw_session[key], f"session {key}", record=False)
            for key in SESSION_KEYS
        }
        if max_chars < 2:
            raise MemoryBenchError("MCP max_chars must be at least 2")

    ranking_pairs: list[dict[str, str]] = []
    raw_pairs = item.get("ranking_pairs") or []
    if not isinstance(raw_pairs, list) or len(raw_pairs) > 100:
        raise MemoryBenchError(f"case {case_id} ranking_pairs must be a bounded array")
    for pair_index, raw_pair in enumerate(raw_pairs):
        pair = require_mapping(raw_pair, f"case {case_id} ranking_pairs[{pair_index}]")
        reject_unknown(pair, RANKING_PAIR_KEYS, f"case {case_id} ranking_pairs[{pair_index}]")
        ranking_pairs.append(
            {
                "higher_record_id": identifier(
                    pair.get("higher_record_id"), f"case {case_id} higher_record_id"
                ),
                "lower_record_id": identifier(
                    pair.get("lower_record_id"), f"case {case_id} lower_record_id"
                ),
            }
        )

    forbidden_namespaces = item.get("forbidden_namespaces") or []
    if not isinstance(forbidden_namespaces, list) or len(forbidden_namespaces) > 100:
        raise MemoryBenchError(f"case {case_id} forbidden_namespaces must be a bounded array")
    normalized_namespaces = [
        bounded_text(value, f"case {case_id} forbidden namespace", 200)
        for value in forbidden_namespaces
    ]
    forbidden_substrings = item.get("forbidden_output_substrings") or []
    if not isinstance(forbidden_substrings, list) or len(forbidden_substrings) > 100:
        raise MemoryBenchError(
            f"case {case_id} forbidden_output_substrings must be a bounded array"
        )
    normalized_substrings = [
        bounded_text(value, f"case {case_id} forbidden output substring", 500)
        for value in forbidden_substrings
    ]

    max_result_count = bounded_int(
        item.get("max_result_count", limit),
        f"case {case_id} max_result_count",
        0,
        limit,
    )
    required = normalize_required(item.get("required"), case_id)
    relevant_record_ids = unique_identifiers(
        item.get("relevant_record_ids"), f"case {case_id} relevant_record_ids"
    )
    relevant_surface_ids = unique_identifiers(
        item.get("relevant_surface_ids"), f"case {case_id} relevant_surface_ids"
    )
    # Required targets are independently asserted judgments, so they are always
    # relevance targets too. This prevents required-result gates and aggregate
    # recall/MRR from describing different notions of success.
    for requirement in required:
        record_id = requirement.get("record_id")
        surface_id = requirement.get("surface_id")
        if record_id and record_id not in relevant_record_ids:
            relevant_record_ids.append(record_id)
        if surface_id and surface_id not in relevant_surface_ids:
            relevant_surface_ids.append(surface_id)

    required_record_ids = {
        str(requirement["record_id"]) for requirement in required if requirement.get("record_id")
    }
    for pair in ranking_pairs:
        higher = pair["higher_record_id"]
        lower = pair["lower_record_id"]
        if higher == lower:
            raise MemoryBenchError(f"case {case_id} ranking pair must use two different record IDs")
        if higher not in required_record_ids:
            raise MemoryBenchError(
                f"case {case_id} ranking higher_record_id {higher} must also be a required record target"
            )

    normalized = {
        "id": case_id,
        "query": query,
        "limit": limit,
        "max_chars": max_chars,
        "subject_ids": unique_identifiers(
            item.get("subject_ids"), f"case {case_id} subject_ids", record=False
        ),
        "collections": normalized_collections,
        "relevant_record_ids": relevant_record_ids,
        "relevant_surface_ids": relevant_surface_ids,
        "required": required,
        "ranking_pairs": ranking_pairs,
        "forbidden_record_ids": unique_identifiers(
            item.get("forbidden_record_ids"), f"case {case_id} forbidden_record_ids"
        ),
        "forbidden_reference_ids": unique_identifiers(
            item.get("forbidden_reference_ids"),
            f"case {case_id} forbidden_reference_ids",
            record=False,
        ),
        "forbidden_namespaces": normalized_namespaces,
        "forbidden_output_substrings": normalized_substrings,
        "max_result_count": max_result_count,
    }
    meaningful = (
        any(
            normalized[key]
            for key in (
                "relevant_record_ids",
                "relevant_surface_ids",
                "required",
                "ranking_pairs",
                "forbidden_record_ids",
                "forbidden_reference_ids",
                "forbidden_namespaces",
                "forbidden_output_substrings",
            )
        )
        or max_result_count < limit
    )
    if not meaningful:
        raise MemoryBenchError(
            f"case {case_id} must contain at least one relevance, ranking, scope, privacy, or result-count assertion"
        )
    if session is not None:
        normalized["session"] = session
    if context:
        normalized["context_section"] = section
        normalized["max_items"] = bounded_int(item.get("max_items", 12), "max_items", 1, 500)
    return normalized


def normalize_suite(raw: Mapping[str, Any], payload: bytes) -> dict[str, Any]:
    version = raw.get("schema_version")
    if not isinstance(version, str) or version not in {SCHEMA_VERSION, SCHEMA_VERSION_V2}:
        raise MemoryBenchError("unsupported MemoryBench suite schema_version")
    reject_unknown(
        raw, TOP_LEVEL_KEYS | ({"read_path"} if version == SCHEMA_VERSION_V2 else set()), "suite"
    )
    read_path = raw.get("read_path", "cli_recall")
    if not isinstance(read_path, str) or read_path not in READ_PATHS:
        raise MemoryBenchError("unsupported MemoryBench read_path")
    suite_id = identifier(raw.get("suite_id"), "suite_id", record=False)
    expected_store_id = bounded_text(raw.get("expected_store_id"), "expected_store_id", 100)
    expected_generation = (
        None
        if raw.get("expected_generation") is None
        else bounded_int(raw["expected_generation"], "expected_generation", 1, 2**31 - 1)
    )
    defaults = require_mapping(raw.get("defaults", {}), "suite defaults")
    reject_unknown(defaults, DEFAULT_KEYS, "suite defaults")
    if read_path == "mcp_recall_context" and "limit" in defaults:
        raise MemoryBenchError("context suites use per-case max_items, not defaults.limit")
    mode = defaults.get("mode", "review_safe")
    if mode != "review_safe":
        raise MemoryBenchError("MemoryBench permits review_safe mode only")
    normalized_defaults = {
        "mode": "review_safe",
        "limit": bounded_int(defaults.get("limit", 5), "default limit", 1, MAX_RECALL_LIMIT),
        "max_chars": bounded_int(
            defaults.get("max_chars", 12_000),
            "default max_chars",
            1,
            MAX_CONTEXT_CHARS,
        ),
    }
    thresholds = require_mapping(raw.get("thresholds", {}), "suite thresholds")
    reject_unknown(thresholds, THRESHOLD_KEYS, "suite thresholds")
    normalized_thresholds = {
        key: bounded_ratio(thresholds.get(key, 0.0), key) for key in THRESHOLD_KEYS
    }
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise MemoryBenchError("suite cases must be a non-empty JSON array")
    if len(cases) > MAX_CASES:
        raise MemoryBenchError(f"suite exceeds {MAX_CASES} cases")
    normalized_cases = [
        normalize_case(case, normalized_defaults, index, read_path=read_path)
        for index, case in enumerate(cases)
    ]
    case_ids = [case["id"] for case in normalized_cases]
    if len(set(case_ids)) != len(case_ids):
        raise MemoryBenchError("suite contains duplicate case IDs")
    return {
        "schema_version": version,
        **({"read_path": read_path} if version == SCHEMA_VERSION_V2 else {}),
        "suite_id": suite_id,
        "suite_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_store_id": expected_store_id,
        "expected_generation": expected_generation,
        "defaults": normalized_defaults,
        "thresholds": normalized_thresholds,
        "cases": normalized_cases,
    }


def run_cli(
    config: Path, arguments: Sequence[str], timeout: float
) -> tuple[dict[str, Any], str, float]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(KG_CLI), "--config", str(config), *arguments],
            cwd=SCRIPT_DIR,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise MemoryBenchError("production knowledge-graph CLI timed out") from error
    latency_ms = (time.perf_counter() - started) * 1000
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise MemoryBenchError("production knowledge-graph CLI output exceeded the safety limit")
    if completed.returncode != 0:
        raise MemoryBenchError(
            f"production knowledge-graph CLI failed for {arguments[0]} (exit {completed.returncode})"
        )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MemoryBenchError("production knowledge-graph CLI returned non-JSON output") from error
    result = require_mapping(parsed, "production knowledge-graph CLI result")
    if result.get("status") == "error":
        raise MemoryBenchError(
            f"production knowledge-graph CLI reported an error for {arguments[0]}"
        )
    return result, completed.stdout, latency_ms


def compact_state(status: Mapping[str, Any]) -> dict[str, Any]:
    if status.get("command") != "status" or status.get("status") != "ok":
        raise MemoryBenchError("status command returned an unexpected result shape")
    graph = require_mapping(status.get("graph"), "status graph")
    return {
        "store_id": bounded_text(graph.get("store_id"), "status store_id", 100),
        "generation": bounded_int(graph.get("generation"), "status generation", 1, 2**31 - 1),
        "watermark": bounded_int(graph.get("watermark"), "status watermark", 0, 2**63 - 1),
    }


def implementation_digest() -> str:
    """Bind a v2 result to the benchmark and graph Python bytes it executed."""
    digest = hashlib.sha256()
    for path in sorted(SCRIPT_DIR.rglob("*.py")):
        name = path.relative_to(SCRIPT_DIR).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def run_mcp(
    config: Path, name: str, arguments: Mapping[str, object], timeout: float
) -> tuple[dict[str, object], str, float]:
    """Read an existing exact scope through the actual binary stdio endpoint."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "tedd-memorybench", "version": "2"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        },
    ]
    environment = {
        key: os.environ[key]
        for key in (
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LOCALAPPDATA",
            "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_ARCHITEW6432",
        )
        if key in os.environ
    }
    environment.update(
        HOME=str(config.parent), USERPROFILE=str(config.parent), PYTHONDONTWRITEBYTECODE="1"
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIR / "tedd_graph_mcp.py"), "--config", str(config)],
            cwd=SCRIPT_DIR,
            env=environment,
            input="".join(json.dumps(request) + "\n" for request in requests).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise MemoryBenchError("MCP read timed out") from error
    latency_ms = (time.perf_counter() - started) * 1000
    if completed.returncode or completed.stderr or len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise MemoryBenchError("MCP transport failed or exceeded its output limit")
    try:
        responses = [
            require_mapping(json.loads(line), "MCP response")
            for line in completed.stdout.splitlines()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryBenchError("MCP returned invalid JSON") from error
    if [response.get("id") for response in responses] != [1, 2, 3] or any(
        "error" in response for response in responses
    ):
        raise MemoryBenchError("MCP handshake or response IDs were invalid")
    if any(
        type(response.get("id")) is not int or response.get("jsonrpc") != "2.0"
        for response in responses
    ):
        raise MemoryBenchError("MCP response envelope was invalid")
    initialized = require_mapping(responses[0].get("result"), "MCP initialization")
    if initialized.get("protocolVersion") != "2025-11-25":
        raise MemoryBenchError("MCP initialization did not negotiate the requested protocol")
    tools = require_mapping(responses[1].get("result"), "MCP tools").get("tools")
    if not isinstance(tools, list) or not any(
        isinstance(tool, dict) and tool.get("name") == name for tool in tools
    ):
        raise MemoryBenchError("MCP endpoint did not advertise the requested tool")
    result = require_mapping(responses[2].get("result"), "MCP tool result")
    body = require_mapping(result.get("structuredContent"), "MCP structured result")
    if result.get("isError") or body.get("status") == "error" or "category" in body:
        raise MemoryBenchError("MCP tool refused the exact reader request")
    if body.get("subscription_id") != arguments.get("subscription_id"):
        raise MemoryBenchError("MCP returned a different subscription")
    return body, json.dumps(body, ensure_ascii=False), latency_ms


def context_records(body: Mapping[str, object], section: str) -> list[dict[str, object]]:
    """Score delivered compact fields, without rereading full canonical payloads."""
    value = require_mapping(body.get(section), f"MCP {section} section")
    items = value.get("items")
    if not isinstance(items, list):
        raise MemoryBenchError("MCP context section items must be an array")
    records: list[dict[str, object]] = []
    for item in items:
        payload = require_mapping(item, "MCP context item")
        record_id = identifier(payload.get("id"), "MCP context record id")
        records.append(
            {
                "record_id": record_id,
                "collection": "claims" if section == "preferences" else "open_loops",
                "payload": payload,
            }
        )
    return records


def read_mcp_case(
    config: Path, read_path: str, case: Mapping[str, object], timeout: float
) -> tuple[dict[str, object], str, float]:
    arguments = require_mapping(case.get("session"), "MCP session")
    if read_path == "mcp_search_memory":
        arguments.update(query=case["query"], limit=case["limit"], max_chars=case["max_chars"])
        body, raw, latency = run_mcp(config, "search_memory", arguments, timeout)
        output = {"command": "recall", "status": "ok", **body, "read_path": read_path}
    else:
        arguments.update(max_items=case["max_items"], max_chars=case["max_chars"])
        body, raw, latency = run_mcp(config, "recall_context", arguments, timeout)
        section = str(case["context_section"])
        results = context_records(body, section)
        records = body.get("records")
        if not isinstance(records, list):
            raise MemoryBenchError("MCP context records must be an array")
        disclosure = [
            *records,
            *context_records(body, "overdue"),
            *context_records(body, "preferences"),
        ]
        output = {
            "command": "recall",
            "status": "ok",
            "results": results,
            "result_count": len(results),
            "read_path": read_path,
            "context_section": section,
            "disclosed_records": disclosure,
            "response_chars": len(raw),
            "omitted": require_mapping(body[section], "MCP section").get("omitted", False),
            "truncated": require_mapping(body[section], "MCP section").get("truncated"),
        }
    return output, raw, latency


def reader_state_digest(suite: Mapping[str, object], config: Path, timeout: float) -> str:
    cases = suite.get("cases")
    if not isinstance(cases, list):
        raise MemoryBenchError("MCP cases must be an array")
    consumers: set[str] = set()
    for case in cases:
        session = require_mapping(require_mapping(case, "case").get("session"), "session")
        consumers.add(str(session["consumer_id"]))
    states = [
        run_cli(config, ["consumer-status", "--consumer-id", consumer], timeout)[0]
        for consumer in sorted(consumers)
    ]
    if any(state.get("active_subscriptions_truncated") for state in states):
        raise MemoryBenchError("reader subscription status is truncated; measurement is invalid")
    return hashlib.sha256(json.dumps(states, sort_keys=True).encode("utf-8")).hexdigest()


def recursive_contains(value: Any, needle: str) -> bool:
    if isinstance(value, Mapping):
        return any(recursive_contains(child, needle) for child in value.values())
    if isinstance(value, list):
        return any(recursive_contains(child, needle) for child in value)
    return isinstance(value, str) and value == needle


def surface_match(result: Mapping[str, Any], surface_id: str) -> bool:
    if result.get("record_id") == surface_id:
        return True
    payload = result.get("payload")
    return isinstance(payload, Mapping) and payload.get("surface_ref") == surface_id


def target_rank(
    results: Sequence[Mapping[str, Any]],
    *,
    record_id: str | None = None,
    surface_id: str | None = None,
) -> int | None:
    for rank, result in enumerate(results, start=1):
        if record_id is not None and result.get("record_id") == record_id:
            return rank
        if surface_id is not None and surface_match(result, surface_id):
            return rank
    return None


def evaluate_case(
    case: Mapping[str, Any], output: Mapping[str, Any], raw_stdout: str, latency_ms: float
) -> dict[str, Any]:
    case_id = str(case["id"])
    gates: list[str] = []
    if output.get("command") != "recall" or output.get("status") != "ok":
        gates.append("unexpected recall result shape")
    raw_results = output.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
        gates.append("recall results were not an array")
    results: list[Mapping[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            gates.append("recall returned a non-object result")
            continue
        results.append(item)
    ordered_ids = [str(item.get("record_id") or "") for item in results]
    if any(not SAFE_RECORD_ID.fullmatch(record_id) for record_id in ordered_ids):
        gates.append("recall returned an invalid record ID")
    if len(set(ordered_ids)) != len(ordered_ids):
        gates.append("recall returned duplicate record IDs")
    if len(results) > int(case["limit"]):
        gates.append("recall exceeded requested result limit")
    if len(results) > int(case["max_result_count"]):
        gates.append("recall exceeded case max_result_count")
    if output.get("result_count") != len(results):
        gates.append("reported result_count did not match returned results")
    encoded_chars = len(json.dumps(raw_results, ensure_ascii=False))
    if encoded_chars > int(case["max_chars"]):
        gates.append("review-safe results exceeded requested character budget")

    relevant_records = set(case["relevant_record_ids"])
    relevant_surfaces = set(case["relevant_surface_ids"])
    relevant_result_count = 0
    hit_records: set[str] = set()
    hit_surfaces: set[str] = set()
    first_relevant_rank: int | None = None
    for rank, result in enumerate(results, start=1):
        record_id = str(result.get("record_id") or "")
        record_hit = record_id in relevant_records
        surface_hits = {surface for surface in relevant_surfaces if surface_match(result, surface)}
        if record_hit:
            hit_records.add(record_id)
        hit_surfaces.update(surface_hits)
        if record_hit or surface_hits:
            relevant_result_count += 1
            if first_relevant_rank is None:
                first_relevant_rank = rank

    target_total = len(relevant_records) + len(relevant_surfaces)
    target_hits = len(hit_records) + len(hit_surfaces)
    precision = relevant_result_count / int(case["limit"])
    recall = target_hits / target_total if target_total else None
    mrr = (
        (1.0 / first_relevant_rank)
        if first_relevant_rank is not None
        else (None if target_total == 0 else 0.0)
    )

    for requirement in case["required"]:
        label = requirement.get("record_id") or f"surface:{requirement.get('surface_id')}"
        matches: list[tuple[int, Mapping[str, Any]]] = []
        for rank, result in enumerate(results, start=1):
            if requirement.get("record_id") == result.get("record_id"):
                matches.append((rank, result))
            elif requirement.get("surface_id") and surface_match(
                result, str(requirement["surface_id"])
            ):
                matches.append((rank, result))
        if not matches:
            gates.append(f"missing required target {label}")
            continue
        eligible = [
            (rank, result) for rank, result in matches if rank <= int(requirement["max_rank"])
        ]
        if not eligible:
            gates.append(
                f"required target {label} ranked {matches[0][0]} after {requirement['max_rank']}"
            )
            continue
        expected_collection = requirement.get("collection")
        assertions = requirement.get("payload_equals") or {}

        def satisfies(
            result: Mapping[str, Any],
            expected_collection=expected_collection,
            assertions=assertions,
        ) -> bool:
            if expected_collection and result.get("collection") != expected_collection:
                return False
            payload = result.get("payload") if isinstance(result.get("payload"), Mapping) else {}
            return all(payload.get(field) == expected for field, expected in assertions.items())

        if any(satisfies(result) for _, result in eligible):
            continue
        gates.append(
            f"required target {label} had no eligible result satisfying all collection and payload assertions"
        )
        if expected_collection and not any(
            result.get("collection") == expected_collection for _, result in eligible
        ):
            gates.append(f"required target {label} had wrong collection")
        for field, expected in assertions.items():
            if not any(
                (not expected_collection or result.get("collection") == expected_collection)
                and isinstance(result.get("payload"), Mapping)
                and result["payload"].get(field) == expected
                for _, result in eligible
            ):
                gates.append(f"required target {label} failed payload assertion {field}")

    positions = {record_id: rank for rank, record_id in enumerate(ordered_ids, start=1)}
    for pair in case["ranking_pairs"]:
        higher = pair["higher_record_id"]
        lower = pair["lower_record_id"]
        if lower in positions:
            if higher not in positions:
                gates.append(
                    f"ranking order indeterminate: {higher} is missing while {lower} is present"
                )
            elif positions[higher] >= positions[lower]:
                gates.append(f"ranking order violated: {higher} must precede {lower}")

    for record_id in case["forbidden_record_ids"]:
        if record_id in positions:
            gates.append(f"forbidden record returned: {record_id}")
    disclosed = output.get("disclosed_records", results)
    if not isinstance(disclosed, list):
        raise MemoryBenchError("disclosed records must be an array")
    for result in disclosed:
        if not isinstance(result, Mapping):
            raise MemoryBenchError("disclosed record must be an object")
        payload = result.get("payload") if isinstance(result.get("payload"), Mapping) else {}
        if result.get("record_id") in case["forbidden_record_ids"]:
            gates.append(f"forbidden record returned: {result['record_id']}")
        for reference_id in case["forbidden_reference_ids"]:
            if recursive_contains(payload, reference_id):
                gates.append(f"forbidden reference returned: {reference_id}")
        namespace = payload.get("namespace")
        if namespace in case["forbidden_namespaces"]:
            gates.append(f"forbidden namespace returned: {namespace}")
    for substring in case["forbidden_output_substrings"]:
        if substring in raw_stdout:
            gates.append("review-safe output contained a forbidden private sentinel")
    if output.get("context_section") is not None:
        if output["response_chars"] > int(case["max_chars"]):
            gates.append("complete MCP context exceeded requested character budget")
        if output.get("omitted"):
            gates.append("requested context section was omitted, not an empty retrieval")

    return {
        "case_id": case_id,
        "status": "pass" if not gates else "fail",
        "ordered_record_ids": ordered_ids,
        "result_count": len(results),
        "unjudged_at_k": len(results) - relevant_result_count,
        "precision_at_k": round(precision, 6),
        "recall_at_k": None if recall is None else round(recall, 6),
        "mrr": None if mrr is None else round(mrr, 6),
        "latency_ms": round(latency_ms, 3),
        "gates": sorted(set(gates)),
        **(
            {"read_path": output["read_path"], "precision_k": int(case["limit"])}
            if "read_path" in output
            else {}
        ),
        **(
            {
                "context_section": output["context_section"],
                "omitted": output["omitted"],
                "truncated": output["truncated"],
            }
            if "context_section" in output
            else {}
        ),
    }


def run_benchmark(
    suite: Mapping[str, Any], config: Path, *, repeat: int = 1, timeout: float = 30.0
) -> tuple[dict[str, Any], int]:
    repeat = bounded_int(repeat, "repeat", 1, MAX_REPEAT)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        raise MemoryBenchError("timeout must be greater than 0 and at most 300 seconds")
    status_output, _, status_latency = run_cli(config, ["status"], timeout)
    start = compact_state(status_output)
    if start["store_id"] != suite["expected_store_id"]:
        raise MemoryBenchError("configured graph store_id does not match the suite")
    if (
        suite.get("expected_generation") is not None
        and start["generation"] != suite["expected_generation"]
    ):
        raise MemoryBenchError("configured graph generation does not match the suite")
    read_path = str(suite.get("read_path", "cli_recall"))
    version2 = suite["schema_version"] == SCHEMA_VERSION_V2
    implementation = implementation_digest() if version2 else None
    readers = reader_state_digest(suite, config, timeout) if read_path != "cli_recall" else None

    case_reports: list[dict[str, Any]] = []
    latencies = [status_latency]
    execution_error: MemoryBenchError | None = None
    for case in suite["cases"]:
        repeat_reports: list[dict[str, Any]] = []
        try:
            for _ in range(repeat):
                arguments = [
                    "recall",
                    case["query"],
                    "--limit",
                    str(case["limit"]),
                    "--max-chars",
                    str(case["max_chars"]),
                    "--mode",
                    "review_safe",
                    "--at-watermark",
                    str(start["watermark"]),
                ]
                for subject_id in case["subject_ids"]:
                    arguments.extend(["--subject-id", subject_id])
                for collection in case["collections"]:
                    arguments.extend(["--section", collection])
                if read_path == "cli_recall":
                    output, raw_stdout, latency = run_cli(config, arguments, timeout)
                    if version2:
                        output["read_path"] = read_path
                else:
                    output, raw_stdout, latency = read_mcp_case(config, read_path, case, timeout)
                latencies.append(latency)
                repeat_reports.append(evaluate_case(case, output, raw_stdout, latency))
        except MemoryBenchError as error:
            execution_error = error
            break
        first = repeat_reports[0]
        if any(
            report["ordered_record_ids"] != first["ordered_record_ids"]
            for report in repeat_reports[1:]
        ):
            first["gates"].append("ordered record IDs changed across repeats")
        first["latency_ms"] = round(
            mean(report["latency_ms"] for report in repeat_reports) or 0.0, 3
        )
        first["gates"] = sorted(
            set(first["gates"]).union(*(set(report["gates"]) for report in repeat_reports[1:]))
        )
        first["status"] = "pass" if not first["gates"] else "fail"
        case_reports.append(first)

    end_error: MemoryBenchError | None = None
    try:
        end_output, _, end_latency = run_cli(config, ["status"], timeout)
        latencies.append(end_latency)
        end = compact_state(end_output)
    except MemoryBenchError as error:
        end_error = error
        end = None

    changed = end is not None and end != start
    operational_change = False
    if version2:
        try:
            operational_change = implementation != implementation_digest() or (
                readers is not None and readers != reader_state_digest(suite, config, timeout)
            )
        except MemoryBenchError as error:
            end_error = error
    aggregate = {
        "case_count": len(suite["cases"]),
        "executed_case_count": len(case_reports),
        "pass_count": sum(report["status"] == "pass" for report in case_reports),
        "fail_count": sum(report["status"] == "fail" for report in case_reports),
        "macro_precision_at_k": mean(report["precision_at_k"] for report in case_reports),
        "macro_recall_at_k": mean(report["recall_at_k"] for report in case_reports),
        "macro_mrr": mean(report["mrr"] for report in case_reports),
        "latency_ms_p50": percentile(latencies, 0.50),
        "latency_ms_p95": percentile(latencies, 0.95),
    }
    threshold_failures: list[str] = []
    metric_map = {
        "min_macro_precision_at_k": "macro_precision_at_k",
        "min_macro_recall_at_k": "macro_recall_at_k",
        "min_macro_mrr": "macro_mrr",
    }
    for threshold_name, metric_name in metric_map.items():
        threshold = float(suite["thresholds"][threshold_name])
        actual = aggregate[metric_name]
        if threshold > 0.0 and actual is None:
            threshold_failures.append(
                f"{metric_name} is unavailable but configured threshold is {threshold:.6f}"
            )
        elif actual is not None and actual < threshold:
            threshold_failures.append(
                f"{metric_name} {actual:.6f} is below configured threshold {threshold:.6f}"
            )

    status = "pass"
    exit_code = 0
    reason: str | None = None
    if changed or operational_change:
        status = "inconclusive"
        exit_code = 2
        reason = (
            "canonical state, reader state or implementation changed during the run"
            if version2
            else "canonical store identity, generation, or watermark changed during the run"
        )
    elif end_error is not None:
        status = "invalid"
        exit_code = 2
        reason = str(end_error)
    elif execution_error is not None:
        status = "invalid"
        exit_code = 2
        reason = str(execution_error)
    elif len(case_reports) != len(suite["cases"]):
        status = "invalid"
        exit_code = 2
        reason = "not all cases executed"
    elif aggregate["fail_count"] or threshold_failures:
        status = "fail"
        exit_code = 1

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION_V2 if version2 else REPORT_SCHEMA_VERSION,
        "status": status,
        "generated_at": utc_now(),
        "suite_id": suite["suite_id"],
        "suite_sha256": suite["suite_sha256"],
        "store": start,
        "repeat": repeat,
        "summary": aggregate,
        "threshold_failures": threshold_failures,
        "cases": case_reports,
        **(
            {
                "read_path": read_path,
                "implementation_sha256": implementation,
                "reader_state_sha256": readers,
            }
            if version2
            else {}
        ),
    }
    if reason:
        report["reason"] = reason
    if end is not None and changed:
        report["end_store"] = end
    return report, exit_code


def atomic_private_json(path: Path, value: Mapping[str, Any], *, force: bool) -> None:
    expanded = path.expanduser()
    if is_link_or_reparse_point(expanded):
        raise MemoryBenchError("report output must be a regular non-symlink file")
    target = expanded.resolve()
    if target.exists():
        if not target.is_file():
            raise MemoryBenchError("report output must be a regular non-symlink file")
        target.stat()
        if not owner_is_current_user(target):
            raise MemoryBenchError("report output must be owned by the current user")
        if not force:
            raise MemoryBenchError("report output already exists; pass --force to replace it")
        if target.stat().st_size > MAX_OUTPUT_BYTES:
            raise MemoryBenchError("existing report output exceeds the safety limit")
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MemoryBenchError(
                "--force may replace only an existing MemoryBench report"
            ) from error
        if not isinstance(existing, Mapping) or existing.get("schema_version") not in (
            REPORT_SCHEMA_VERSION,
            REPORT_SCHEMA_VERSION_V2,
        ):
            raise MemoryBenchError("--force may replace only an existing MemoryBench report")
    try:
        target.parent.mkdir(parents=True, mode=0o700)
    except FileExistsError as error:
        if not target.parent.is_dir():
            raise MemoryBenchError("report output parent must be a directory") from error
    else:
        set_private_path_mode(target.parent, 0o700)
    target.parent.stat()
    if not owner_is_current_user(target.parent):
        raise MemoryBenchError("report output parent must be owned by the current user")
    if mode_has_any(target.parent, stat.S_IRWXG | stat.S_IRWXO):
        raise MemoryBenchError(
            "report output parent must not be accessible by group or other users"
        )
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temporary)
    try:
        set_private_fd_mode(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        set_private_path_mode(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def safe_report_output(path: Path, suite_path: Path, config_path: Path) -> Path:
    """Bind reports to the suite's private reports directory and avoid inputs."""

    expanded = path.expanduser()
    if is_link_or_reparse_point(expanded):
        raise MemoryBenchError("report output must be a regular non-symlink file")
    target = expanded.resolve()
    suite = suite_path.expanduser().resolve()
    config = config_path.expanduser().resolve()
    report_root = suite.parent / "reports"
    if target == report_root or not target.is_relative_to(report_root):
        raise MemoryBenchError(
            "report output must be inside the suite's private reports directory"
        )
    protected = {suite, config, KG_CLI.resolve()}
    try:
        config_value = json.loads(config.read_text(encoding="utf-8"))
        if isinstance(config_value, Mapping) and config_value.get("store_path"):
            protected.add(Path(str(config_value["store_path"])).expanduser().resolve())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # The production CLI will return a bounded invalid result for a bad config.
        pass
    if target in protected:
        raise MemoryBenchError("report output collides with a protected benchmark or graph input")
    return target


def error_report(suite_path: Path, message: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "invalid",
        "generated_at": utc_now(),
        "suite_path_sha256": hashlib.sha256(str(suite_path).encode("utf-8")).hexdigest(),
        "reason": message,
    }


def console_report(report: Mapping[str, Any], *, detailed: bool) -> dict[str, Any]:
    """Keep record and case identifiers out of logs when a private report is saved."""

    if detailed:
        return dict(report)
    allowed = (
        "schema_version",
        "status",
        "generated_at",
        "suite_id",
        "suite_sha256",
        "repeat",
        "summary",
        "threshold_failures",
        "reason",
        "read_path",
        "implementation_sha256",
        "reader_state_sha256",
    )
    return {key: report[key] for key in allowed if key in report}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run deterministic read-only gates against Tedd's production recall path"
    )
    result.add_argument("--suite", default=str(DEFAULT_SUITE), help="Private MemoryBench suite JSON")
    result.add_argument("--config", required=True, help="Knowledge-graph config JSON")
    result.add_argument("--repeat", type=int, default=1, help="Repeat each query to test ordering stability")
    result.add_argument("--timeout", type=float, default=30.0, help="Per-CLI-call or MCP-process timeout in seconds")
    result.add_argument("--output", help="Optional private JSON report path")
    result.add_argument("--force", action="store_true", help="Replace an existing report output")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    suite_path = Path(args.suite)
    output_path = Path(args.output).expanduser() if args.output else None
    safe_output_path: Path | None = None
    try:
        raw_suite, payload = safe_private_json(suite_path, "MemoryBench suite")
        suite = normalize_suite(raw_suite, payload)
        if output_path is not None:
            safe_output_path = safe_report_output(
                output_path, suite_path, Path(args.config)
            )
        report, exit_code = run_benchmark(
            suite,
            Path(args.config).expanduser().resolve(),
            repeat=args.repeat,
            timeout=args.timeout,
        )
    except (MemoryBenchError, OSError) as error:
        report = error_report(suite_path, str(error))
        exit_code = 2
    if safe_output_path is not None:
        try:
            atomic_private_json(safe_output_path, report, force=bool(args.force))
        except (MemoryBenchError, OSError) as error:
            report = error_report(suite_path, str(error))
            exit_code = 2
    print(
        json.dumps(
            console_report(report, detailed=output_path is None),
            indent=2,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
