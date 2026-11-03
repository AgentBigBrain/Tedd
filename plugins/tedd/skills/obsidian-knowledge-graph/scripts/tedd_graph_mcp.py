#!/usr/bin/env python3
"""Launch the bounded, read-only Tedd graph MCP server over stdio."""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import knowledge_graph
import knowledge_graph_v4 as storage
import knowledge_graph_v4_integration
from tedd_knowledge_graph.v4_application_contracts import JsonObject
from tedd_knowledge_graph.v4_application_service import (
    GraphReadDependencies,
    GraphReadLimits,
    GraphReadService,
    GraphReadStore,
)
from tedd_knowledge_graph.v4_backend_error_mapping import classify_graph_backend_error
from tedd_knowledge_graph.v4_mcp_stdio import McpServer, serve, stdio_streams


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(knowledge_graph.DEFAULT_CONFIG_PATH),
        help="secure graph configuration path (defaults to the graph CLI configuration)",
    )
    return parser.parse_args(argv)


@dataclass(frozen=True)
class _ConfiguredGraph:
    config_path: Path
    config_data: object
    store_path: Path


def _configured_service(config_path: Path) -> GraphReadService:
    """Compose lazily so protocol setup is available without graph state.

    Configuration, privacy validation, and store resolution occur once per
    actual graph read, immediately before a readonly store can be opened.
    """

    def load_config() -> _ConfiguredGraph:
        canonical_config_path = knowledge_graph.secure_state_path(config_path, "Configuration")
        config_data = knowledge_graph.load_config(canonical_config_path)
        store_path = knowledge_graph.resolve_store(argparse.Namespace(store=None), config_data)
        if knowledge_graph.state_privacy_warnings(canonical_config_path, store_path, config_data):
            raise ValueError("configured graph state is not private")
        return _ConfiguredGraph(canonical_config_path, config_data, store_path)

    def resolve_store_path(config: object) -> Path:
        if not isinstance(config, _ConfiguredGraph):
            raise ValueError("graph configuration is invalid")
        return config.store_path

    def validate_store_path(config: object, path: Path) -> None:
        if not isinstance(config, _ConfiguredGraph) or path != config.store_path:
            raise ValueError("graph service path does not match the configured store")

    def open_store(
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> contextlib.AbstractContextManager[GraphReadStore]:
        return cast(
            contextlib.AbstractContextManager[GraphReadStore],
            storage.open_store(
                path,
                readonly=readonly,
                plan_hmac_key=plan_hmac_key,
            ),
        )

    def sanitize(value: JsonObject) -> object:
        return knowledge_graph_v4_integration.sanitize_review_safe_value(value)

    return GraphReadService(
        GraphReadDependencies(
            load_config=load_config,
            resolve_store_path=resolve_store_path,
            validate_store_path=validate_store_path,
            detect_store=storage.detect_store,
            inspect_store=lambda path: cast(dict[str, object], storage.inspect_store_path(path)),
            load_plan_key=lambda _config, _path: None,
            open_store=open_store,
            sanitize=sanitize,
            limits=GraphReadLimits(),
            classify_error=lambda error: classify_graph_backend_error(error, storage),
        )
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(sys.argv[1:] if argv is None else argv)
    input_stream, output_stream, error_stream = stdio_streams()
    return serve(
        McpServer(_configured_service(Path(arguments.config))),
        input_stream,
        output_stream,
        error_stream,
    )


if __name__ == "__main__":
    raise SystemExit(main())
