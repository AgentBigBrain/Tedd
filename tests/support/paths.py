"""Canonical repository paths and scoped import setup for tests."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_SCRIPTS = REPOSITORY_ROOT / "scripts"
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins/tedd"
GRAPH_SCRIPTS = PLUGIN_ROOT / "skills/obsidian-knowledge-graph/scripts"
PROFILE_SCRIPTS = PLUGIN_ROOT / "skills/tedd/scripts"


def prepend_import_paths(*paths: Path) -> None:
    """Make explicit checkout paths importable without changing child environments."""

    for path in reversed(paths):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
