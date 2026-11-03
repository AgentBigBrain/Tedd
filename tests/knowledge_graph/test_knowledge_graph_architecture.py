"""Static architecture contracts for the private knowledge-graph services.

The checks in this module parse source files only.  They intentionally do not import
the implementation package because compatibility facades use lazy runtime bindings and
the architecture contract must remain safe to run during structural review.
"""

from __future__ import annotations

import ast
import unittest
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from tests.support.paths import GRAPH_SCRIPTS

_PACKAGE_NAME = "tedd_knowledge_graph"
_PACKAGE_DIR = GRAPH_SCRIPTS / _PACKAGE_NAME
_MCP_LAUNCHER = GRAPH_SCRIPTS / "tedd_graph_mcp.py"
_FLAT_COMPATIBILITY_FACADES = frozenset(
    {
        "knowledge_graph",
        "knowledge_graph_v4",
        "knowledge_graph_v4_capture",
        "knowledge_graph_v4_cli",
        "knowledge_graph_v4_integration",
    }
)
_REQUIRED_RESPONSIBILITY_MODULES = {
    "legacy store": frozenset({"legacy_store"}),
    "v4 store": frozenset({"v4_store"}),
    "legacy query": frozenset({"legacy_query"}),
    "v4 query": frozenset({"v4_queries"}),
    "legacy migration": frozenset({"legacy_migrations"}),
    "v4 migration": frozenset({"v4_migrations"}),
    "legacy projection": frozenset({"legacy_projection"}),
    "v4 projection": frozenset({"v4_projection"}),
    "v4 backup": frozenset({"v4_backup"}),
    "legacy coordination": frozenset({"legacy_coordination"}),
    "v4 coordination": frozenset({"v4_coordination"}),
    "legacy command families": frozenset(
        {
            "legacy_commands_mutation",
            "legacy_commands_projection",
            "legacy_commands_read",
        }
    ),
    "v4 command families": frozenset(
        {
            "v4_cli_capture",
            "v4_cli_coordination",
            "v4_cli_maintenance",
            "v4_cli_read",
        }
    ),
}
_ALLOWED_V4_CAPTURE_LEGACY_METHODS = frozenset(
    {
        "blank_graph",
        "capture_packet",
        "explicit_record_id",
        "index_by_id",
        "normalize_key",
        "normalize_space",
        "packet_records",
        "parse_time",
        "read_packet",
        "record_entity_refs",
        "record_text",
        "resolve_entity_ref",
        "scrub_value",
        "source_tier",
        "stable_id",
        "unique_strings",
        "upsert_entity",
        "utc_now",
        "validate_graph",
    }
)
_APPLICATION_MODULES = frozenset(
    {
        "v4_application_contracts",
        "v4_application_service",
    }
)
_MCP_TRANSPORT_MODULES = frozenset(
    {
        "v4_mcp_contracts",
        "v4_mcp_stdio",
    }
)
_APPLICATION_FORBIDDEN_IMPORT_PREFIXES = (
    "argparse",
    "legacy_",
    "mcp",
    "knowledge_graph",
    "knowledge_graph_v4",
    "tedd_graph_mcp",
    "v4_cli",
    "v4_mcp",
)


@dataclass(frozen=True)
class _ParsedModule:
    name: str
    path: Path
    tree: ast.Module
    is_package: bool


def _parse_private_modules() -> dict[str, _ParsedModule]:
    modules: dict[str, _ParsedModule] = {}
    for path in sorted(_PACKAGE_DIR.glob("*.py"), key=lambda candidate: candidate.name):
        is_package = path.name == "__init__.py"
        name = _PACKAGE_NAME if is_package else f"{_PACKAGE_NAME}.{path.stem}"
        source = path.read_text(encoding="utf-8")
        modules[name] = _ParsedModule(
            name=name,
            path=path,
            tree=ast.parse(source, filename=str(path)),
            is_package=is_package,
        )
    return modules


def _is_flat_facade(module_name: str) -> bool:
    return any(
        module_name == facade or module_name.startswith(f"{facade}.")
        for facade in _FLAT_COMPATIBILITY_FACADES
    )


def _flat_facade_imports(modules: dict[str, _ParsedModule]) -> list[str]:
    violations: list[str] = []
    for parsed in modules.values():
        for node in ast.walk(parsed.tree):
            imported_names: list[str] = []
            if isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_names.append(node.module)
            else:
                continue
            for imported_name in imported_names:
                if _is_flat_facade(imported_name):
                    violations.append(f"{parsed.path.name}:{node.lineno}: imports {imported_name}")
    return sorted(violations)


def _application_import_violations(modules: dict[str, _ParsedModule]) -> list[str]:
    """Keep the application layer transport-neutral and inward-facing."""

    violations: list[str] = []
    for stem in sorted(_APPLICATION_MODULES):
        parsed = modules.get(f"{_PACKAGE_NAME}.{stem}")
        if parsed is None:
            violations.append(f"missing {stem}.py")
            continue
        for node in ast.walk(parsed.tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = _absolute_import_from_base(parsed, node)
                if base is not None:
                    names.append(base)
            for name in names:
                leaf = name.rpartition(".")[2]
                if name.startswith(_APPLICATION_FORBIDDEN_IMPORT_PREFIXES) or leaf.startswith(
                    _APPLICATION_FORBIDDEN_IMPORT_PREFIXES
                ):
                    violations.append(
                        f"{parsed.path.name}:{getattr(node, 'lineno', 0)}: imports {name}"
                    )
    return sorted(violations)


def _absolute_import_from_base(parsed: _ParsedModule, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    current_package = parsed.name if parsed.is_package else parsed.name.rpartition(".")[0]
    package_parts = current_package.split(".")
    parent_count = node.level - 1
    if parent_count >= len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - parent_count]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _internal_imports(
    parsed: _ParsedModule,
    known_modules: frozenset[str],
) -> frozenset[str]:
    dependencies: set[str] = set()
    for node in ast.walk(parsed.tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names if alias.name in known_modules)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _absolute_import_from_base(parsed, node)
        if base is None:
            continue
        if base in known_modules and base != _PACKAGE_NAME:
            dependencies.add(base)
        if base == _PACKAGE_NAME:
            dependencies.update(
                candidate
                for alias in node.names
                if alias.name != "*"
                for candidate in (f"{base}.{alias.name}",)
                if candidate in known_modules
            )
    return frozenset(dependencies)


def _dependency_graph(modules: dict[str, _ParsedModule]) -> dict[str, frozenset[str]]:
    known_modules = frozenset(modules)
    return {
        name: _internal_imports(parsed, known_modules) for name, parsed in sorted(modules.items())
    }


def _strongly_connected_components(
    graph: dict[str, frozenset[str]],
) -> list[tuple[str, ...]]:
    next_index = 0
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module_name: str) -> None:
        nonlocal next_index
        indices[module_name] = next_index
        low_links[module_name] = next_index
        next_index += 1
        stack.append(module_name)
        on_stack.add(module_name)

        for dependency in sorted(graph[module_name]):
            if dependency not in indices:
                visit(dependency)
                low_links[module_name] = min(low_links[module_name], low_links[dependency])
            elif dependency in on_stack:
                low_links[module_name] = min(low_links[module_name], indices[dependency])

        if low_links[module_name] != indices[module_name]:
            return
        component: list[str] = []
        while stack:
            dependency = stack.pop()
            on_stack.remove(dependency)
            component.append(dependency)
            if dependency == module_name:
                break
        components.append(tuple(sorted(component)))

    for module_name in sorted(graph):
        if module_name not in indices:
            visit(module_name)
    return sorted(components)


def _path_within_component(
    graph: dict[str, frozenset[str]],
    start: str,
    goal: str,
    component: frozenset[str],
) -> tuple[str, ...] | None:
    queue = deque([start])
    predecessor: dict[str, str | None] = {start: None}
    while queue:
        module_name = queue.popleft()
        if module_name == goal:
            path: list[str] = []
            current: str | None = module_name
            while current is not None:
                path.append(current)
                current = predecessor[current]
            return tuple(reversed(path))
        for dependency in sorted(graph[module_name] & component):
            if dependency not in predecessor:
                predecessor[dependency] = module_name
                queue.append(dependency)
    return None


def _cycle_witness(
    graph: dict[str, frozenset[str]], component_modules: tuple[str, ...]
) -> tuple[str, ...]:
    start = component_modules[0]
    if start in graph[start]:
        return (start, start)
    component = frozenset(component_modules)
    for dependency in sorted(graph[start] & component):
        return_path = _path_within_component(graph, dependency, start, component)
        if return_path is not None:
            return (start, *return_path)
    raise AssertionError(f"unable to construct cycle witness for {component_modules!r}")


def _cycle_witnesses(graph: dict[str, frozenset[str]]) -> list[str]:
    cyclic_components = [
        component
        for component in _strongly_connected_components(graph)
        if len(component) > 1 or component[0] in graph[component[0]]
    ]
    return [" -> ".join(_cycle_witness(graph, component)) for component in cyclic_components]


class KnowledgeGraphArchitectureTests(unittest.TestCase):
    def test_mcp_contracts_are_a_dependency_free_leaf_and_stdio_points_inward(self) -> None:
        modules = _parse_private_modules()
        contracts = modules[f"{_PACKAGE_NAME}.v4_mcp_contracts"]
        stdio = modules[f"{_PACKAGE_NAME}.v4_mcp_stdio"]

        self.assertEqual(set(), set(_internal_imports(contracts, frozenset(modules))))
        self.assertEqual(
            {
                f"{_PACKAGE_NAME}.v4_application_contracts",
                f"{_PACKAGE_NAME}.v4_mcp_contracts",
            },
            set(_internal_imports(stdio, frozenset(modules))),
        )

    def test_mcp_launcher_is_the_privacy_checked_composition_root(self) -> None:
        source = _MCP_LAUNCHER.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_MCP_LAUNCHER))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )

        self.assertTrue(
            {
                "knowledge_graph",
                "knowledge_graph_v4",
                "knowledge_graph_v4_integration",
                "tedd_knowledge_graph.v4_application_service",
                "tedd_knowledge_graph.v4_backend_error_mapping",
                "tedd_knowledge_graph.v4_mcp_stdio",
            }
            <= imported_modules
        )
        self.assertIn("state_privacy_warnings", source)
        self.assertIn("readonly=readonly", source)
        self.assertIn("classify_graph_backend_error", source)
        self.assertNotIn("immutable=True", source)

        launcher_forbidden = {"v4_cli_read", "v4_cli_coordination", "v4_store"}
        self.assertEqual(
            set(),
            {
                module
                for module in imported_modules
                if module.rpartition(".")[2] in launcher_forbidden
            },
            "The launcher is a composition root, not a CLI or storage implementation layer.",
        )

    def test_mcp_transport_stays_outside_mutation_and_cli_layers(self) -> None:
        """The MCP adapter may translate requests, but cannot own graph state transitions."""

        modules = _parse_private_modules()
        forbidden_imports = {
            "v4_application_service",
            "v4_cli_capture",
            "v4_cli_coordination",
            "v4_cli_maintenance",
            "v4_cli_read",
            "v4_lifecycle",
        }
        forbidden_method_names = {
            "ack",
            "apply_changes",
            "capture_packet",
            "create_store",
            "project",
            "register_consumer",
            "subscribe",
        }
        violations: list[str] = []
        for stem in sorted(_MCP_TRANSPORT_MODULES):
            parsed = modules[f"{_PACKAGE_NAME}.{stem}"]
            imports = _internal_imports(parsed, frozenset(modules))
            for imported in sorted(imports):
                if imported.rpartition(".")[2] in forbidden_imports:
                    violations.append(f"{parsed.path.name}: imports {imported}")
            for node in ast.walk(parsed.tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden_method_names:
                    violations.append(f"{parsed.path.name}:{node.lineno}: uses {node.attr}")

        self.assertEqual(
            [],
            sorted(violations),
            f"Read-only MCP transport imports or invokes mutation/CLI state: {violations}",
        )

    def test_graph_read_application_modules_are_transport_neutral(self) -> None:
        violations = _application_import_violations(_parse_private_modules())

        self.assertEqual(
            [],
            violations,
            "Graph read contracts/service must not import a flat facade, CLI, MCP, "
            f"or legacy module: {violations}",
        )

    def test_private_services_do_not_statically_import_flat_facades(self) -> None:
        violations = _flat_facade_imports(_parse_private_modules())

        self.assertEqual(
            [],
            violations,
            "Private service modules must depend inward, not on flat compatibility "
            f"facades. Static violations: {violations}",
        )

    def test_private_service_dependency_graph_is_acyclic(self) -> None:
        graph = _dependency_graph(_parse_private_modules())
        cycles = _cycle_witnesses(graph)

        self.assertEqual(
            [],
            cycles,
            f"Private service dependency cycles: {cycles}",
        )

    def test_v4_private_services_do_not_add_static_legacy_dependencies(self) -> None:
        graph = _dependency_graph(_parse_private_modules())
        violations = sorted(
            f"{module_name} imports {dependency}"
            for module_name, dependencies in graph.items()
            if module_name.rpartition(".")[2].startswith("v4_")
            for dependency in dependencies
            if dependency.rpartition(".")[2].startswith("legacy_")
        )

        self.assertEqual(
            [],
            violations,
            "v4 private services may receive compatibility policy through reviewed "
            f"adapters, but must not add static legacy imports: {violations}",
        )

    def test_v4_capture_legacy_policy_surface_cannot_grow_silently(self) -> None:
        parsed = _parse_private_modules()[f"{_PACKAGE_NAME}.v4_capture"]
        policy = next(
            (
                node
                for node in parsed.tree.body
                if isinstance(node, ast.ClassDef) and node.name == "LegacyCapturePolicy"
            ),
            None,
        )
        self.assertIsNotNone(policy)
        assert policy is not None
        methods = frozenset(
            node.name
            for node in policy.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

        self.assertEqual(
            _ALLOWED_V4_CAPTURE_LEGACY_METHODS,
            methods,
            "The temporary v4-to-v3 semantic-capture adapter is frozen. Remove methods "
            "as shared policy is extracted; do not add another legacy dependency without "
            "an explicit architecture review.",
        )

    def test_required_responsibility_modules_exist(self) -> None:
        present_stems = frozenset(
            path.stem for path in _PACKAGE_DIR.glob("*.py") if path.name != "__init__.py"
        )
        missing = [
            f"{responsibility}: {', '.join(sorted(required - present_stems))}"
            for responsibility, required in sorted(_REQUIRED_RESPONSIBILITY_MODULES.items())
            if not required <= present_stems
        ]

        self.assertEqual(
            [],
            missing,
            f"Missing responsibility modules: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
