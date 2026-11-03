"""Disposable graph sandbox and public-CLI boundary for memory evaluation."""

from __future__ import annotations

import importlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from .catalog import NAMESPACE, scenarios, subject_ids
from .contracts import PROTOCOL_ID, RecordFixture, Scenario, StateSnapshot

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GRAPH_SCRIPTS = (REPOSITORY_ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts").resolve()
GRAPH_CLI = GRAPH_SCRIPTS / "knowledge_graph.py"
PLAN_KEY = b"tedd-memory-eval-lab-only-plan-key-v1"
WINDOWS_REPARSE_POINT = 0x400
READ_COMMAND_FLAGS = {
    "context": frozenset({"--subscription-id", "--max-items", "--max-chars", "--mode"}),
    "recall": frozenset({"--subject-id", "--limit", "--max-chars", "--mode"}),
    "get": frozenset({"--id", "--mode"}),
}
READ_POSITIONAL_COUNTS = {"context": 0, "recall": 1, "get": 0}
SYNTHETIC_ID_FLAGS = frozenset({"--id", "--subscription-id", "--subject-id"})


class GraphStore(Protocol):
    """Narrow setup/read surface used from the dynamically loaded graph facade."""

    def state(self) -> Mapping[str, object]: ...

    def validate(self, *, full: bool = False) -> Mapping[str, object]: ...

    def apply_changes(
        self,
        changes: Sequence[object],
        *,
        fingerprint: str,
        action: str = "capture",
        discovery_kind: str = "durable_discovery",
        producer_agent_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        based_on_watermark: int | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]: ...

    def register_consumer(
        self,
        consumer_id: str,
        *,
        agent_id: str,
        task_id: str,
        metadata: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def subscribe(
        self,
        consumer_id: str,
        selector: Mapping[str, object],
        *,
        subscription_id: str | None = None,
        start_watermark: int | None = None,
    ) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class CreateStore(Protocol):
    def __call__(
        self, path: str | Path, *, plan_hmac_key: bytes | str | None = None
    ) -> GraphStore: ...


class OpenStore(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        plan_hmac_key: bytes | str | None = None,
    ) -> GraphStore: ...


class RecordChangeFactory(Protocol):
    def __call__(
        self,
        record_id: str,
        collection: str,
        payload: Mapping[str, object],
        *,
        refs: Sequence[Mapping[str, object]] = (),
        semantic_keys: Sequence[object] = (),
        search_text: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class GraphApi:
    """Typed graph constructors loaded without coupling the lab to plugin packaging."""

    create_store: CreateStore
    open_store: OpenStore
    record_change: RecordChangeFactory


def _load_module(name: str) -> ModuleType:
    graph_scripts = str(GRAPH_SCRIPTS)
    if graph_scripts not in sys.path:
        sys.path.insert(0, graph_scripts)
    module = importlib.import_module(name)
    expected = (GRAPH_SCRIPTS / f"{name}.py").resolve(strict=True)
    loaded_file = getattr(module, "__file__", None)
    if not isinstance(loaded_file, str) or Path(loaded_file).resolve(strict=True) != expected:
        raise RuntimeError(f"Refusing shadowed memory evaluation dependency: {name}")
    return module


def _graph_api() -> GraphApi:
    module = _load_module("knowledge_graph_v4")
    return GraphApi(
        create_store=cast(CreateStore, module.create_store),
        open_store=cast(OpenStore, module.open_store),
        record_change=cast(RecordChangeFactory, module.RecordChange),
    )


def _private_temporary_parent() -> str | None:
    support = _load_module("benchmark_support")
    helper = cast(Callable[[], str | None], support.private_temporary_parent)
    return helper()


def _set_private_path_mode(path: Path, mode: int) -> None:
    support = _load_module("platform_support")
    helper = cast(Callable[[Path, int], None], support.set_private_path_mode)
    helper(path, mode)


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(int(attributes) & WINDOWS_REPARSE_POINT)


def _physical_temporary_root(raw_root: Path) -> Path:
    """Bind an allocated leaf to its physical path without trusting parent aliases."""

    before = raw_root.lstat()
    before_attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or bool(int(before_attributes) & WINDOWS_REPARSE_POINT)
    ):
        raise RuntimeError("Memory evaluation root cannot be a link or reparse point")
    resolved = raw_root.resolve(strict=True)
    after = resolved.lstat()
    after_attributes = getattr(after, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or bool(int(after_attributes) & WINDOWS_REPARSE_POINT)
    ):
        raise RuntimeError("Memory evaluation root cannot be a link or reparse point")
    rebound = raw_root.lstat()
    before_identity = (before.st_dev, before.st_ino)
    if before_identity != (after.st_dev, after.st_ino) or before_identity != (
        rebound.st_dev,
        rebound.st_ino,
    ):
        raise RuntimeError("Memory evaluation root changed during physical path binding")
    return resolved


def _assert_owned_plain_path(root: Path, path: Path) -> None:
    lexical_root = Path(os.path.abspath(root))
    root_metadata = lexical_root.lstat()
    root_attributes = getattr(root_metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(root_metadata.st_mode) or bool(int(root_attributes) & WINDOWS_REPARSE_POINT):
        raise RuntimeError("Memory evaluation root cannot be a link or reparse point")
    resolved_root = lexical_root.resolve(strict=True)
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as error:
        raise RuntimeError("Memory evaluation path escaped its owned temporary root") from error
    current = lexical_root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(int(attributes) & WINDOWS_REPARSE_POINT):
            raise RuntimeError("Memory evaluation paths cannot contain links or reparse points")
    try:
        lexical_path.resolve(strict=False).relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError("Memory evaluation path escaped its owned temporary root") from error


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a JSON object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise RuntimeError(f"{label} keys must be strings")
    return {str(key): item for key, item in raw.items()}


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{label} must be an integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be text")
    return value


class MemoryEvalSandbox:
    """Own a private synthetic graph for exactly one evaluation run."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        catalog: Sequence[Scenario] | None = None,
        protocol_id: str = PROTOCOL_ID,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be finite and between 0 and 300")
        if (
            not isinstance(protocol_id, str)
            or not protocol_id
            or len(protocol_id) > 256
            or protocol_id.strip() != protocol_id
        ):
            raise ValueError("protocol_id must be non-empty bounded text without outer whitespace")
        selected_catalog = scenarios() if catalog is None else tuple(catalog)
        if not selected_catalog or not all(
            isinstance(scenario, Scenario) for scenario in selected_catalog
        ):
            raise ValueError("catalog must contain typed memory evaluation scenarios")
        self.timeout_seconds = timeout_seconds
        self._protocol_id = protocol_id
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.config_path: Path | None = None
        self.store_path: Path | None = None
        self.vault_path: Path | None = None
        self._initial_state: StateSnapshot | None = None
        self._catalog = selected_catalog

    @property
    def catalog(self) -> tuple[Scenario, ...]:
        return self._catalog

    @property
    def initial_state(self) -> StateSnapshot:
        if self._initial_state is None:
            raise RuntimeError("Memory evaluation sandbox is not prepared")
        return self._initial_state

    def __enter__(self) -> "MemoryEvalSandbox":
        self._temporary = tempfile.TemporaryDirectory(
            prefix="tedd-memory-eval-", dir=_private_temporary_parent()
        )
        try:
            raw_root = Path(self._temporary.name)
            self.root = _physical_temporary_root(raw_root)
            _set_private_path_mode(self.root, 0o700)
            self.config_path = self.root / "config.json"
            self.store_path = self.root / "graph.sqlite3"
            self.vault_path = self.root / "vault"
            self.vault_path.mkdir(mode=0o700)
            _set_private_path_mode(self.vault_path, 0o700)
            for path in (self.config_path, self.store_path, self.vault_path):
                _assert_owned_plain_path(self.root, path)
            self._configure()
            self._seed()
            return self
        except BaseException:
            self._cleanup()
            raise

    def __exit__(self, *_: object) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
        self._temporary = None
        self.root = None
        self.config_path = None
        self.store_path = None
        self.vault_path = None
        self._initial_state = None

    def _required_path(self, value: Path | None, label: str) -> Path:
        if value is None or self.root is None:
            raise RuntimeError(f"Memory evaluation {label} is unavailable")
        _assert_owned_plain_path(self.root, value)
        return value

    def _environment(self) -> dict[str, str]:
        root = self._required_path(self.root, "root")
        environment = dict(os.environ)
        environment.update(
            {
                "APPDATA": str(root),
                "HOME": str(root),
                "LOCALAPPDATA": str(root),
                "TEMP": str(root),
                "TMP": str(root),
                "USERPROFILE": str(root),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.pop("PYTHONPATH", None)
        return environment

    def _command(self, arguments: Sequence[str]) -> list[str]:
        config = self._required_path(self.config_path, "configuration")
        return [
            sys.executable,
            str(GRAPH_CLI),
            "--config",
            str(config),
            *arguments,
        ]

    def _invoke(self, arguments: Sequence[str]) -> dict[str, object]:
        command = self._command(arguments)
        try:
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                env=self._environment(),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Synthetic graph command exceeded its time limit") from error
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Synthetic graph command failed: {message[:500]}")
        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Synthetic graph command returned invalid JSON") from error
        return _object(decoded, "Synthetic graph response")

    @staticmethod
    def _validate_read_arguments(arguments: Sequence[str]) -> None:
        if not arguments or arguments[0] not in READ_COMMAND_FLAGS:
            raise RuntimeError("Memory evaluation permits only frozen read commands")
        command = arguments[0]
        allowed_flags = READ_COMMAND_FLAGS[command]
        seen_flags: set[str] = set()
        positionals = 0
        index = 1
        while index < len(arguments):
            item = arguments[index]
            if item.startswith("--"):
                if item not in allowed_flags or item in seen_flags or index + 1 >= len(arguments):
                    raise RuntimeError("Memory evaluation read arguments violate the frozen schema")
                value = arguments[index + 1]
                if value.startswith("--"):
                    raise RuntimeError("Memory evaluation read options require explicit values")
                if item in SYNTHETIC_ID_FLAGS and not value.startswith("lab_"):
                    raise RuntimeError("Memory evaluation reads require synthetic lab identifiers")
                if item == "--mode" and value != "review_safe":
                    raise RuntimeError("Memory evaluation reads require review-safe mode")
                seen_flags.add(item)
                index += 2
                continue
            positionals += 1
            index += 1
        if seen_flags != allowed_flags or positionals != READ_POSITIONAL_COUNTS[command]:
            raise RuntimeError("Memory evaluation read arguments are incomplete or ambiguous")

    def call(self, arguments: Sequence[str]) -> dict[str, object]:
        """Invoke one allowlisted read against only the owned synthetic config."""

        self._validate_read_arguments(arguments)
        return self._invoke(arguments)

    def _configure(self) -> None:
        vault = self._required_path(self.vault_path, "vault")
        store = self._required_path(self.store_path, "store")
        self._invoke(
            (
                "configure",
                "--vault",
                str(vault),
                "--root-name",
                "Tedd Memory Eval Lab",
                "--store",
                str(store),
            )
        )

    @staticmethod
    def _entity_payload(subject_id: str) -> dict[str, object]:
        return {
            "id": subject_id,
            "namespace": NAMESPACE,
            "type": "synthetic_eval_subject",
            "name": subject_id.removeprefix("lab_subject_").replace("_", " "),
            "aliases": [],
            "identity_keys": [
                {
                    "scheme": "memory_eval",
                    "namespace": NAMESPACE,
                    "value": subject_id,
                }
            ],
            "lifecycle": "active",
            "status": "current",
            "sensitive": False,
        }

    def _apply(
        self,
        store: GraphStore,
        changes: Sequence[object],
        *,
        fingerprint: str,
    ) -> None:
        state = store.state()
        store.apply_changes(
            changes,
            fingerprint=fingerprint,
            discovery_kind="reusable_lesson",
            producer_agent_id="lab_agent_memory_eval",
            task_id="lab_task_memory_eval",
            run_id="lab_run_memory_eval",
            based_on_watermark=_integer(state.get("watermark"), "Graph watermark"),
            idempotency_key=f"lab:{fingerprint}",
        )

    def _fixture_change(self, api: GraphApi, fixture: RecordFixture) -> object:
        refs = (
            {"target_id": fixture.subject_id, "kind": "subject"},
            *({"target_id": target_id, "kind": kind} for target_id, kind in fixture.refs),
        )
        return api.record_change(
            fixture.record_id,
            fixture.collection,
            fixture.payload,
            refs=refs,
            search_text=fixture.search_text,
        )

    def _seed(self) -> None:
        store_path = self._required_path(self.store_path, "store")
        api = _graph_api()
        store = api.create_store(store_path, plan_hmac_key=PLAN_KEY)
        try:
            fixture_subject_ids = subject_ids(self._catalog)
            known_subject_ids = set(fixture_subject_ids)
            missing_scenario_subject_ids: list[str] = []
            for scenario in self._catalog:
                if scenario.subject_id not in known_subject_ids:
                    known_subject_ids.add(scenario.subject_id)
                    missing_scenario_subject_ids.append(scenario.subject_id)
            seeded_subject_ids = (*fixture_subject_ids, *missing_scenario_subject_ids)
            entity_changes = [
                api.record_change(
                    subject_id,
                    "entities",
                    self._entity_payload(subject_id),
                    search_text=f"synthetic memory evaluation subject {subject_id}",
                )
                for subject_id in seeded_subject_ids
            ]
            self._apply(store, entity_changes, fingerprint="seed-subjects-v1")
            mutation = 0
            for scenario in self._catalog:
                for fixture in scenario.records:
                    mutation += 1
                    self._apply(
                        store,
                        [self._fixture_change(api, fixture)],
                        fingerprint=f"seed-{mutation:04d}-{fixture.record_id}",
                    )
            for scenario in self._catalog:
                consumer_id = consumer_id_for(scenario)
                subscription_id = subscription_id_for(scenario)
                store.register_consumer(
                    consumer_id,
                    agent_id="lab_agent_memory_eval",
                    task_id=f"lab_task_{scenario.case_id}",
                    metadata={"protocol": self._protocol_id},
                )
                store.subscribe(
                    consumer_id,
                    {
                        "all": False,
                        "record_ids": [],
                        "entity_ids": [scenario.subject_id],
                        "collections": [],
                        "discovery_kinds": [],
                    },
                    subscription_id=subscription_id,
                    start_watermark=0,
                )
            validation = _object(store.validate(full=True), "Graph validation")
            if validation.get("valid") is not True:
                raise RuntimeError("Synthetic graph failed full validation")
            self._initial_state = _snapshot(store, validation)
        finally:
            store.close()

    def final_state(self) -> StateSnapshot:
        """Read canonical state after all providers have completed."""

        store_path = self._required_path(self.store_path, "store")
        store = _graph_api().open_store(store_path, readonly=True, plan_hmac_key=PLAN_KEY)
        try:
            validation = _object(store.validate(full=True), "Graph validation")
            if validation.get("valid") is not True:
                raise RuntimeError("Synthetic graph became invalid during evaluation")
            return _snapshot(store, validation)
        finally:
            store.close()


def _snapshot(store: GraphStore, validation: Mapping[str, object]) -> StateSnapshot:
    state = _object(store.state(), "Graph state")
    counts = _object(validation.get("counts"), "Graph validation counts")
    return StateSnapshot(
        generation=_integer(state.get("generation"), "Graph generation"),
        watermark=_integer(state.get("watermark"), "Graph watermark"),
        journal_head_hash=_text(state.get("journal_head_hash"), "Graph journal head"),
        record_count=_integer(counts.get("records"), "Graph record count"),
    )


def consumer_id_for(scenario: Scenario) -> str:
    return f"lab_consumer_{scenario.case_id}"


def subscription_id_for(scenario: Scenario) -> str:
    return f"lab_subscription_{scenario.case_id}"


__all__ = [
    "MemoryEvalSandbox",
    "consumer_id_for",
    "subscription_id_for",
]
