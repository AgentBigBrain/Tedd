#!/usr/bin/env python3
"""Contract tests for the legacy v3 validation compatibility adapter."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

SCRIPT = SCRIPT_DIRECTORY / "knowledge_graph.py"
SPEC = importlib.util.spec_from_file_location("knowledge_graph_validation_target", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load knowledge_graph.py")
kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kg)


class KnowledgeGraphV3ValidationAdapterTests(unittest.TestCase):
    def test_public_adapter_preserves_graph_identity_and_result(self) -> None:
        graph = kg.blank_graph()
        expected = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "counts": {},
        }
        dependencies = kg._validation_dependencies()

        with (
            mock.patch.object(
                kg,
                "_validation_dependencies",
                autospec=True,
                return_value=dependencies,
            ),
            mock.patch.object(
                kg,
                "_validate_v3_graph",
                autospec=True,
                return_value=expected,
            ) as validate,
        ):
            result = kg.validate_graph(graph)

        self.assertIs(result, expected)
        validate.assert_called_once_with(graph, dependencies)

    def test_validation_is_observational_and_does_not_mutate_graph(self) -> None:
        graph = kg.blank_graph()
        before = copy.deepcopy(graph)

        result = kg.validate_graph(graph)

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(graph, before)


if __name__ == "__main__":
    unittest.main()
