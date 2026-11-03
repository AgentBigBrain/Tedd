# Keep unittest and Enforce Measured Branch Coverage

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Keep Tedd's standard-library `unittest` suites unless another framework solves a
specific, approved problem. Measure production branch coverage with
`coverage[toml]==7.15.3`, preserve synthetic isolation, and set the failure floor
from a verified baseline rather than a slogan.

## Applies when

- Changing test discovery, suite grouping, coverage scope, or warning policy.
- Adding focused, integration, platform, or release-gate tests.
- Evaluating a proposal for pytest or a test plugin.

## Does not apply when

- Framework popularity is the only reason to migrate.
- A plugin would be executed before its exact version and behavior are reviewed.
- Raising coverage would force meaningless assertions or hide deliberate omissions.

## Bad example

```toml
[project.optional-dependencies]
test = ["pytest>=8", "pytest-cov"]

[tool.pytest.ini_options]
filterwarnings = [
    "error",
    "ignore::DeprecationWarning",
]
```

This adds unpinned third-party execution and globally hides deprecations.

## Corrected Tedd example

```toml
# The complete dev group pins coverage[toml]==7.15.3 and its reviewed closure.
[tool.coverage.run]
branch = true
relative_files = true
source = [
    "scripts",
    "plugins/tedd/skills/tedd/scripts",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts",
]

[tool.coverage.report]
fail_under = 55
show_missing = true
```

Run the checked-in release wrapper; its quality mode combines the standard-library
suites, branch coverage, repository checks, Ruff, and mypy in the approved environment.
Every test subprocess sets `DeprecationWarning` and `PendingDeprecationWarning` to
errors. Because Tedd has no third-party runtime dependencies, that policy covers
owned runtime code without a broad ignore list.

## Caveats and edge cases

- Read the exact candidate's report for measured coverage. The 55 percent floor
  is the established cross-platform regression gate; change it only after a
  verified baseline is stable on every blocking operating system. Neither number
  is a universal target or proof of comprehensive testing.
- Test discovery imports repository-controlled Python. Inspect an unfamiliar
  checkout before running unittest, coverage, pytest, or plugins.
- Treat owned-code deprecations as failures. If a future test-only dependency
  produces an unavoidable warning, narrow its exception by category, module, and
  message, record the removal condition, and never ignore all deprecations.
- Omitting entry points, abstract methods, or type-checking branches can hide real
  gaps and requires an explicit rationale.

## Verification

- Run focused `unittest` modules, then `python scripts/verify_release.py --quality`.
- Confirm the report measures branches over production sources and enforces the
  checked-in floor.
- Inspect skips, warnings, and omitted files; a green percentage alone is insufficient.
