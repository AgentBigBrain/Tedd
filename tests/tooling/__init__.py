"""Repository tooling and packaging tests."""

from tests.support.paths import GRAPH_SCRIPTS, REPOSITORY_SCRIPTS, prepend_import_paths

prepend_import_paths(GRAPH_SCRIPTS, REPOSITORY_SCRIPTS)
