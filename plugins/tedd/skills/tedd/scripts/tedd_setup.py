#!/usr/bin/env python3
"""Preview an existing Tedd brain connection; apply only the reviewed changes."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

GRAPH_SCRIPTS = Path(__file__).resolve().parents[2] / "obsidian-knowledge-graph/scripts"
sys.path.insert(0, str(GRAPH_SCRIPTS))

import knowledge_graph as graph  # noqa: E402
import knowledge_graph_v4 as storage  # noqa: E402
import tedd_profile  # noqa: E402
from platform_support import (  # noqa: E402
    guarded_input_descriptor,
    mode_has_any,
    owner_is_current_user,
)
from tedd_knowledge_graph.preferences import (  # noqa: E402
    preference_scope_id,
    validate_preference_record,
)
from tedd_knowledge_graph.source_contracts import SourceArchiveError, parse_json  # noqa: E402

if Path(graph.__file__).resolve() != (GRAPH_SCRIPTS / "knowledge_graph.py").resolve():
    raise ImportError("Refusing an unexpected graph module")

MAX_PRIVATE_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


@dataclass(frozen=True)
class SetupRequest:
    host: str
    graph_config: Path
    project: Path
    profile: Path
    host_home: Path
    python: Path
    record_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    operator_id: str | None = None
    include_descendants: bool = False
    host_executable: Path | None = None
    include_preferences: bool = False


class SetupApplyError(ValueError):
    """An apply failed; already replaced components need explicit recovery."""

    def __init__(self, changed_components: list[str], backup: Path) -> None:
        super().__init__("Setup apply failed; inspect the private backup before retrying")
        self.changed_components = changed_components
        self.backup = backup


class SetupStateError(ValueError):
    """Saved setup state is malformed, rather than a caller's request."""


def session_binding(request: SetupRequest, session_id: str, run_id: str) -> dict[str, object]:
    """Read approval and task ownership; never register, subscribe or acknowledge."""
    if request.host not in {"codex", "claude"}:
        raise ValueError("Select Codex or Claude Code")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", session_id) is None:
        raise ValueError("Use the freshly resolved native session ID, not inherited skill text")
    identifier_list([run_id])
    project = real_directory(request.project)
    config = private_object(request.graph_config)
    store_path = existing_store_path(request.graph_config, config)
    profile = tedd_profile.validate_profile(private_object(request.profile))
    with storage.open_store(store_path, readonly=True) as store:
        with store.read_snapshot():
            state = store.state()
            binding = binding_for_project(
                private_object(request.profile.parent / "project-bindings.json"),
                project,
                state["store_id"],
                state["generation"],
            )
            if binding["operator_id"] not in [item["operator_id"] for item in profile["operators"]]:
                raise ValueError(
                    "The binding operator is not in this profile; reviewed rebind required"
                )
            consumer_id = request.host + ":" + session_id
            agent_id = profile["agent_instance_id"]
            status = store.consumer_status(consumer_id)
            subscriptions: list[str] = []
            if status["found"]:
                owner = status["owner"]
                if (
                    not isinstance(owner, dict)
                    or owner.get("agent_id") != agent_id
                    or owner.get("task_id") != session_id
                    or status["active_subscriptions_truncated"]
                ):
                    raise ValueError(
                        "This session does not own the existing consumer; explicit rebind required"
                    )
                active = cast(list[dict[str, object]], status["active_subscriptions"])
                if len(active) != 1:
                    raise ValueError(
                        "No unique prior session scope; explicit review or a new session required"
                    )
                expected = {
                    "all": False,
                    "record_ids": sorted(cast(list[str], binding["record_ids"])),
                    "entity_ids": sorted(cast(list[str], binding["entity_ids"])),
                    "collections": [],
                    "discovery_kinds": [],
                }
                for entry in active:
                    subscription_id = cast(str, entry["subscription_id"])
                    pack = store.context_pack(subscription_id, max_items=1, max_chars=1000)
                    if pack["selector"] != expected:
                        raise ValueError(
                            "This session scope differs from the approved project; start a new session or rebind"
                        )
                    subscriptions.append(subscription_id)
            return {
                "host": request.host,
                "consumer_id": consumer_id,
                "agent_id": agent_id,
                "task_id": session_id,
                "run_id": run_id,
                "binding": binding,
                "subscription_ids": subscriptions,
                "watermark": state["watermark"],
                "coordination": "ready" if status["found"] else "not_registered",
                "source_access": "not_enabled_by_this_route",
            }


def existing_store_path(config_path: Path, config: dict[str, object]) -> Path:
    if config.get("canonical_store_format") != "sqlite_v4_wal":
        raise ValueError("Setup requires an existing v4 brain; migration is separate")
    value = config.get("store_path")
    if not isinstance(value, str) or not value:
        raise ValueError("The graph configuration has no explicit store")
    store_path = Path(graph.secure_state_path(value, "Canonical store"))
    if graph.state_privacy_warnings(config_path, store_path, config):
        raise ValueError("The configured brain is not private local state")
    if not store_path.is_file():
        raise FileNotFoundError("The configured brain is missing; initialize it separately")
    return store_path


def apply_setup(request: SetupRequest, fingerprint: object) -> dict[str, object]:
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("Apply requires the exact fingerprint from a setup preview")
    binding_path = graph.secure_state_path(
        request.profile.parent / "project-bindings.json", "Project bindings"
    )
    host_config = graph.secure_state_path(request.host_home / "config.toml", "Host configuration")
    # Serialize setup writers. Other host writers do not take this lock, so
    # recheck each owner file immediately before its replacement as well.
    with graph.store_lock(binding_path):
        preview = preview_setup(request)
        if preview["fingerprint"] != fingerprint:
            raise ValueError("Setup changed since preview; review a fresh preview")
        preference_scope = preview.get("preference_scope")
        if isinstance(preference_scope, dict) and preference_scope["status"] == "capture_required":
            raise ValueError(
                "Create the proposed preference scope through reviewed capture, then preview setup again"
            )
        snapshot = cast(dict[str, object], preview["_snapshot"])
        previous_host = (
            private_bytes(host_config, required=False) if request.host == "codex" else None
        )
        previous_bindings = private_bytes(binding_path, required=False)
        if (
            hashlib.sha256(previous_host or b"").hexdigest() != snapshot["host_config"]
            or (previous_host is not None) != snapshot["host_config_exists"]
            or hashlib.sha256(previous_bindings or b"").hexdigest() != snapshot["bindings"]
            or (previous_bindings is not None) != snapshot["bindings_exist"]
        ):
            raise ValueError("Private setup state changed after preview; review a fresh preview")
        bindings = cast(list[dict[str, object]], preview["_bindings"])
        if not preview["_binding_unchanged"]:
            bindings[-1] = {**bindings[-1], "confirmed_at": graph.utc_now()}
        desired_bindings = json.dumps({"version": 1, "bindings": bindings}, indent=2) + "\n"
        desired_host = cast(bytes | None, preview["_host_bytes"])
        replacements: list[tuple[str, Path, bytes | None, str]] = []
        if request.host == "codex" and desired_host != previous_host:
            assert desired_host is not None
            replacements.append(
                ("host_configuration", host_config, previous_host, desired_host.decode("utf-8"))
            )
        if not preview["_binding_unchanged"]:
            replacements.append(
                ("project_bindings", binding_path, previous_bindings, desired_bindings)
            )
        result = {key: value for key, value in preview.items() if not key.startswith("_")}
        result.update(
            {
                "configuration": "configured" if request.host == "codex" else "binding_saved",
                "changed_components": [],
                "backup": None,
            }
        )
        if not replacements:
            return result
        backup = Path(
            tempfile.mkdtemp(prefix=".tedd-setup-backup-", dir=request.profile.parent)
        ).resolve()
        graph.ensure_private_directory(backup)
        for name, _path, previous, _desired in replacements:
            if previous is not None:
                graph.atomic_write(backup / (name + ".txt"), previous.decode("utf-8"))
        graph.atomic_write(backup / "before.json", json.dumps(snapshot, indent=2) + "\n")
        changed: list[str] = []
        try:
            for name, path, previous, desired in replacements:
                if (
                    private_bytes(path, required=False) != previous
                    or hashlib.sha256(private_bytes(request.graph_config) or b"").hexdigest()
                    != snapshot["graph_config"]
                    or hashlib.sha256(private_bytes(request.profile) or b"").hexdigest()
                    != snapshot["profile"]
                ):
                    raise ValueError("Private setup state changed during apply")
                # Record attempted replacements too: a directory-sync failure can
                # occur after atomic publication, so absence of return is not rollback.
                changed.append(name)
                graph.atomic_write(path, desired)
        except (OSError, ValueError) as error:
            raise SetupApplyError(changed, backup) from error
        result.update(
            {"changed_components": changed, "backup": str(backup), "binding": bindings[-1]}
        )
        return result


def private_bytes(path: Path, *, required: bool = True) -> bytes | None:
    path = graph.secure_state_path(path, "Setup state")
    try:
        with guarded_input_descriptor(path) as descriptor:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or not owner_is_current_user(descriptor)
                or mode_has_any(descriptor, 0o077)
                or info.st_size > MAX_PRIVATE_BYTES
            ):
                raise ValueError("Setup state must be a bounded owner-private file")
            chunks: list[bytes] = []
            size = 0
            while size <= MAX_PRIVATE_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_PRIVATE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_PRIVATE_BYTES:
                raise ValueError("Setup state exceeds its byte limit")
            return data
    except FileNotFoundError:
        if required:
            raise FileNotFoundError(
                "Required setup state is missing; initialize it separately"
            ) from None
        return None


def private_object(path: Path) -> dict[str, object]:
    data = private_bytes(path)
    assert data is not None
    return object_document(data)


def object_document(data: bytes) -> dict[str, object]:
    try:
        value = parse_json(data, maximum_bytes=MAX_PRIVATE_BYTES)
    except SourceArchiveError as error:
        raise SetupStateError("Saved setup state is not valid bounded JSON") from error
    if not isinstance(value, dict):
        raise SetupStateError("Setup state must contain a JSON object")
    return cast(dict[str, object], value)


def real_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if ".." in path.parts or not path.is_dir():
        raise ValueError("Select an existing absolute project directory")
    for component in (path, *path.parents):
        if graph.is_link_or_reparse_point(component):
            raise ValueError("Project aliases and reparse paths require an explicit real directory")
    return path.resolve(strict=True)


def preview_setup(request: SetupRequest) -> dict[str, object]:
    if request.host not in {"codex", "claude"}:
        raise ValueError("Select Codex or Claude Code")
    python = qualify_python(request.python)
    project = real_directory(request.project)
    config_path = graph.secure_state_path(request.graph_config, "Graph configuration")
    config_bytes = private_bytes(config_path)
    assert config_bytes is not None
    config = object_document(config_bytes)
    store_path = existing_store_path(config_path, config)
    profile_bytes = private_bytes(request.profile)
    assert profile_bytes is not None
    try:
        profile = tedd_profile.validate_profile(object_document(profile_bytes))
    except ValueError as error:
        raise SetupStateError("The existing profile is invalid; repair it separately") from error
    operators = [item["operator_id"] for item in profile["operators"]]
    operator = request.operator_id or (operators[0] if len(operators) == 1 else None)
    if operator not in operators:
        raise ValueError("Select one confirmed operator from the existing profile")
    records = identifier_list(list(request.record_ids))
    entities = identifier_list(list(request.entity_ids))
    if not records and not entities:
        raise ValueError("Setup requires exact record or subject selectors")
    preference_scope: dict[str, object] | None = None
    with storage.open_store(store_path, readonly=True) as store:
        with store.read_snapshot():
            if not store.validate(full=True)["valid"]:
                raise SetupStateError("The existing brain is unhealthy; repair it separately")
            state = store.state()
            for record_id in {*records, *entities}:
                try:
                    record = store.get_record(record_id)
                except KeyError as error:
                    raise ValueError("A selector must name an existing record or entity") from error
                if record_id in entities and record["collection"] != "entities":
                    raise ValueError("A subject selector must name an existing entity")
            if request.include_preferences:
                assert isinstance(operator, str)
                scope_id = preference_scope_id(operator)
                entity = {
                    "id": scope_id,
                    "type": "scope",
                    "namespace": operator,
                    "name": "Operator preferences",
                }
                preference_scope = {
                    "entity_id": scope_id,
                    "status": "capture_required",
                    "entity": entity,
                }
                try:
                    record = store.get_record(scope_id)
                except KeyError:
                    pass
                else:
                    payload = record["payload"]
                    if (
                        record["collection"] != "entities"
                        or not isinstance(payload, dict)
                        or any(
                            payload.get(key) != value
                            for key, value in entity.items()
                            if key != "name"
                        )
                        or payload.get("sensitive")
                        or payload.get("lifecycle") != "active"
                        or payload.get("redirect_to")
                    ):
                        raise ValueError(
                            "The preference scope identity is unavailable; review it separately"
                        )
                    related = store.records_referencing([scope_id], limit=2000)
                    if len(related) >= 2000:
                        raise ValueError("The preference scope needs a bounded integrity review")
                    for item in related:
                        related_payload = item["payload"]
                        if not isinstance(related_payload, dict):
                            raise ValueError("The preference scope has an unavailable reference")
                        validate_preference_record(str(item["collection"]), related_payload)
                    preference_scope["status"] = "ready"
                entities = sorted(set(entities) | {scope_id})
    binding_path = request.profile.parent / "project-bindings.json"
    old_bindings = private_bytes(binding_path, required=False)
    document = (
        object_document(old_bindings)
        if old_bindings is not None
        else {"version": 1, "bindings": []}
    )
    try:
        entries = decode_bindings(document)
    except ValueError as error:
        raise SetupStateError(
            "Saved project bindings are invalid; review them separately"
        ) from error
    binding: dict[str, object] = {
        "directory": str(project),
        "include_descendants": request.include_descendants,
        "store_id": state["store_id"],
        "generation": state["generation"],
        "record_ids": records,
        "entity_ids": entities,
        "operator_id": operator,
        "confirmed_at": "pending explicit apply",
    }
    existing = [entry for entry in entries if Path(cast(str, entry["directory"])) == project]
    if existing and (
        existing[0]["store_id"] != state["store_id"]
        or existing[0]["generation"] != state["generation"]
    ):
        raise ValueError("The binding brain identity changed; reviewed rebind required")
    unchanged = bool(
        existing
        and all(
            existing[0][key] == value for key, value in binding.items() if key != "confirmed_at"
        )
    )
    if unchanged:
        binding["confirmed_at"] = existing[0]["confirmed_at"]
    proposed = [entry for entry in entries if Path(cast(str, entry["directory"])) != project]
    proposed.append(binding)
    if len(proposed) > 100:
        raise ValueError("Project bindings exceed their limit; review unused entries separately")
    # Overlapping approvals cannot choose a different client by directory depth.
    for entry in proposed[:-1]:
        other = Path(cast(str, entry["directory"]))
        if (entry["include_descendants"] and other in project.parents) or (
            request.include_descendants and project in other.parents
        ):
            raise ValueError("Overlapping project bindings require a reviewed rebind")
    host_config = graph.secure_state_path(request.host_home / "config.toml", "Host configuration")
    original = private_bytes(host_config, required=False) if request.host == "codex" else None
    command = [
        cast(str, python["executable"]),
        "-B",
        str(Path(graph.__file__).with_name("tedd_graph_mcp.py")),
        "--config",
        str(config_path),
    ]
    updated = original
    if request.host == "codex":
        executable = request.host_executable or (
            Path(found) if (found := shutil.which("codex")) else None
        )
        if executable is None or not executable.is_file():
            raise ValueError("Codex is unavailable; select its installed executable")
        # Codex parses and writes its own TOML in an isolated copy. Do not add a
        # runtime TOML dependency or execute a writer against live state in preview.
        with tempfile.TemporaryDirectory(
            prefix=".tedd-setup-preview-", dir=request.profile.parent
        ) as temp:
            stage = Path(temp).resolve()
            graph.ensure_private_directory(stage)
            graph.atomic_write(stage / "config.toml", (original or b"").decode("utf-8"))
            environment = subprocess_environment()
            environment.update(
                {"CODEX_HOME": str(stage), "HOME": str(stage), "USERPROFILE": str(stage)}
            )
            configured = parse_json(
                run_command([str(executable), "mcp", "list", "--json"], environment=environment),
                maximum_bytes=MAX_PRIVATE_BYTES,
            )
            if not isinstance(configured, list):
                raise ValueError("Codex returned invalid MCP configuration")
            for server in configured:
                if not isinstance(server, dict):
                    raise ValueError("Codex returned invalid MCP configuration")
                transport = server.get("transport")
                if (
                    server.get("name") != "tedd-graph"
                    and server.get("enabled") is not False
                    and isinstance(transport, dict)
                    and isinstance(transport.get("args"), list)
                    and any(
                        isinstance(arg, str) and arg.endswith("tedd_graph_mcp.py")
                        for arg in transport["args"]
                    )
                ):
                    raise ValueError("Another configured graph endpoint would duplicate Tedd")
            run_command(
                [str(executable), "mcp", "add", "tedd-graph", "--", *command],
                environment=environment,
            )
            updated = private_bytes(stage / "config.toml")
    snapshot = {
        "host_config": hashlib.sha256(original or b"").hexdigest(),
        "host_config_exists": original is not None,
        "bindings": hashlib.sha256(old_bindings or b"").hexdigest(),
        "bindings_exist": old_bindings is not None,
        "graph_config": hashlib.sha256(config_bytes).hexdigest(),
        "profile": hashlib.sha256(profile_bytes).hexdigest(),
    }
    public: dict[str, object] = {
        "host": request.host,
        "python": python,
        "graph_config": str(config_path),
        "binding_path": str(binding_path),
        "host_config": str(host_config) if request.host == "codex" else None,
        "binding": binding,
        "graph_watermark": state["watermark"],
        "graph_command": command,
        "root_name": config.get("root_name", "Codex Knowledge")
        if config.get("vault_path")
        else None,
        "connected": False,
        "host_discovery": "not_run",
        "direct_protocol": "not_run",
        "source_access": "unchanged",
        "configuration": "preview",
    }
    if preference_scope is not None:
        public["preference_scope"] = preference_scope
    fingerprint_input = {
        **public,
        "snapshot": snapshot,
        "proposed_host_config": hashlib.sha256(updated or b"").hexdigest(),
    }
    # Unrelated knowledge changes do not invalidate this configuration preview.
    fingerprint_input.pop("graph_watermark")
    fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True).encode()).hexdigest()
    return {
        **public,
        "fingerprint": fingerprint,
        "_snapshot": snapshot,
        "_host_bytes": updated,
        "_bindings": proposed,
        "_binding_unchanged": unchanged,
    }


def subprocess_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR") if key in os.environ}


def run_command(command: list[str], *, environment: dict[str, str]) -> bytes:
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise ValueError("The selected executable could not complete the check")
    if len(result.stdout) > MAX_PRIVATE_BYTES:
        raise ValueError("Executable output exceeds the setup limit")
    return result.stdout


def qualify_python(executable: Path) -> dict[str, object]:
    executable = executable.expanduser().absolute()
    if any(part.casefold() == "windowsapps" for part in executable.parts):
        raise ValueError("Select a real Python executable, not a Windows Store alias")
    if not executable.is_file():
        raise ValueError("The selected Python executable is unavailable")
    probe = """import json, sqlite3, sys
connection = sqlite3.connect(':memory:')
try:
    connection.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
    fts5 = True
except sqlite3.Error:
    fts5 = False
print(json.dumps({'version': list(sys.version_info[:3]), 'fts5': fts5}))
"""
    result = parse_json(
        run_command(
            [str(executable), "-I", "-B", "-c", probe], environment=subprocess_environment()
        ),
        maximum_bytes=MAX_PRIVATE_BYTES,
    )
    if not isinstance(result, dict):
        raise ValueError("Python qualification returned invalid output")
    version = result.get("version")
    if (
        not isinstance(version, list)
        or len(version) != 3
        or any(type(part) is not int for part in version)
        or not (3, 10) <= tuple(version[:2]) <= (3, 14)
        or result.get("fts5") is not True
    ):
        raise ValueError("Setup requires Python 3.10–3.14 with SQLite FTS5")
    return {"executable": str(executable), "version": version, "fts5": True}


def identifier_list(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("A binding requires a bounded list of exact IDs")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or IDENTIFIER.fullmatch(item) is None:
            raise ValueError("Invalid binding selector ID")
        if item not in result:
            result.append(item)
    return result


def decode_bindings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {"version", "bindings"}:
        raise ValueError("Invalid project binding document")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported project binding version")
    items = value["bindings"]
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("Project bindings exceed their limit")
    result: list[dict[str, object]] = []
    directories: set[str] = set()
    fields = {
        "directory",
        "include_descendants",
        "store_id",
        "generation",
        "record_ids",
        "entity_ids",
        "operator_id",
        "confirmed_at",
    }
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("Invalid project binding entry")
        directory = item["directory"]
        if (
            not isinstance(directory, str)
            or not Path(directory).is_absolute()
            or ".." in Path(directory).parts
            or len(directory) > 4096
        ):
            raise ValueError("A binding directory must be an absolute real path")
        directory_key = os.path.normcase(str(Path(directory)))
        if directory_key in directories:
            raise ValueError("Conflicting project bindings")
        directories.add(directory_key)
        if (
            type(item["include_descendants"]) is not bool
            or type(item["generation"]) is not int
            or not 1 <= item["generation"] <= (1 << 63) - 1
            or not isinstance(item["store_id"], str)
            or not item["store_id"]
            or not isinstance(item["operator_id"], str)
            or not item["operator_id"]
            or not isinstance(item["confirmed_at"], str)
            or not item["confirmed_at"]
        ):
            raise ValueError("Binding identity and approval are required")
        for field in ("store_id", "operator_id"):
            identifier_list([item[field]])
        timestamp = item["confirmed_at"]
        if len(timestamp) > 64:
            raise ValueError("Binding confirmation must be a UTC timestamp")
        try:
            confirmation = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Binding confirmation must be a UTC timestamp") from error
        if confirmation.utcoffset() != dt.timedelta(0):
            raise ValueError("Binding confirmation must be a UTC timestamp")
        records = identifier_list(item["record_ids"])
        entities = identifier_list(item["entity_ids"])
        if not records and not entities:
            raise ValueError("A project binding must have explicit selectors")
        result.append({**item, "record_ids": records, "entity_ids": entities})
    return result


def binding_for_project(
    bindings: object, project: Path, store_id: str, generation: int
) -> dict[str, object]:
    matches = []
    for binding in decode_bindings(bindings):
        directory = Path(cast(str, binding["directory"]))
        if project == directory or (
            binding["include_descendants"] and directory in project.parents
        ):
            matches.append(binding)
    if len(matches) != 1:
        raise ValueError("No unique approved binding covers this project")
    binding = matches[0]
    if binding["store_id"] != store_id or binding["generation"] != generation:
        raise ValueError("The configured brain identity changed; reviewed rebind required")
    return binding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "claude"), required=True)
    parser.add_argument("--graph-config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=tedd_profile.DEFAULT_PROFILE)
    parser.add_argument("--host-home", type=Path, default=Path("~/.codex"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--host-executable", type=Path)
    parser.add_argument("--record-id", action="append", default=[])
    parser.add_argument("--entity-id", action="append", default=[])
    parser.add_argument("--operator-id")
    parser.add_argument("--include-descendants", action="store_true")
    parser.add_argument(
        "--include-preferences",
        action="store_true",
        help="Preview the selected operator's global preference scope as an extra subject",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--session-id", help="Read only: freshly resolved native session ID")
    parser.add_argument("--run-id", help="Read only: current invocation provenance ID")
    args = parser.parse_args(argv)
    request = SetupRequest(
        host=args.host,
        graph_config=args.graph_config.expanduser().absolute(),
        project=args.project.expanduser().absolute(),
        profile=args.profile.expanduser().absolute(),
        host_home=args.host_home.expanduser().absolute(),
        python=args.python,
        record_ids=tuple(args.record_id),
        entity_ids=tuple(args.entity_id),
        operator_id=args.operator_id,
        include_descendants=args.include_descendants,
        host_executable=args.host_executable,
        include_preferences=args.include_preferences,
    )
    try:
        if bool(args.apply) != bool(args.fingerprint):
            raise ValueError("Use --apply with the exact preview --fingerprint")
        if args.session_id is not None:
            if (
                args.apply
                or args.run_id is None
                or args.record_id
                or args.entity_id
                or args.include_preferences
            ):
                raise ValueError(
                    "Session checks require --run-id and read the saved selectors, without apply"
                )
            result = session_binding(request, args.session_id, args.run_id)
        elif args.run_id is not None:
            raise ValueError("--run-id requires --session-id")
        elif args.apply:
            result = apply_setup(request, args.fingerprint)
        else:
            result = {
                key: value
                for key, value in preview_setup(request).items()
                if not key.startswith("_")
            }
    except SetupApplyError as error:
        print(
            json.dumps(
                {
                    "status": "incomplete",
                    "category": "unavailable",
                    "message": str(error),
                    "possibly_changed_components": error.changed_components,
                    "backup": str(error.backup),
                    "connected": False,
                }
            )
        )
        return 1
    except (PermissionError, storage.PathSafetyError) as error:
        category = "denied"
        message = str(error)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, TimeoutError):
        category = "unavailable"
        message = "The selected state or executable is unavailable; check paths, permissions and prerequisites"
    except sqlite3.Error as error:
        # Stable SQLite primary codes also work without Python 3.11's named constants.
        code = getattr(error, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        if primary in (11, 26):  # SQLITE_CORRUPT, SQLITE_NOTADB
            category = "corrupt"
            message = "The graph database failed validation; repair it separately"
        elif primary in (3, 8, 23):  # SQLITE_PERM, SQLITE_READONLY, SQLITE_AUTH
            category = "denied"
            message = "SQLite refused the selected database access"
        else:
            category = "unavailable"
            message = "The graph database is unavailable; check storage and active operations"
    except (SetupStateError, storage.StoreFormatError, storage.ValidationError):
        category = "corrupt"
        message = "Stored setup or graph data failed validation; repair it separately"
    except (ValueError, storage.SubscriptionError) as error:
        category = "invalid_request"
        message = str(error)
    else:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(
        json.dumps(
            {"status": "refused", "category": category, "message": message, "connected": False}
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
