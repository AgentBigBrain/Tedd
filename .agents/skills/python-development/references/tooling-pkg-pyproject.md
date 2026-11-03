# Keep Tedd's Pyproject Tool-Only and Reproducible

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Use `pyproject.toml` for PEP 735 development dependency groups and tool
configuration without declaring Tedd as a distributable package. Keep the reviewed
development closure exactly pinned and keep runtime code standard-library-only.

## Applies when

- Updating Ruff, mypy, coverage, PyYAML, or their reviewed transitive closure.
- Configuring a development tool shared by local checks and CI.
- Evaluating whether package metadata or a build backend is actually required.

## Does not apply when

- Adding `[project]` or `[build-system]` merely because a pyproject exists.
- Publishing internal developer tools as consumer-facing optional extras.
- Adding a runtime dependency without an explicit product decision.

## Bad example

```toml
[project]
name = "tedd"
dependencies = []

[project.optional-dependencies]
dev = ["ruff>=0.16", "mypy>=1.20"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

This invents distribution and build behavior, publishes development extras, and
allows tool drift.

## Corrected Tedd example

```toml
[dependency-groups]
dev = [
    "coverage[toml]==7.15.3",
    "librt==0.13.0; platform_python_implementation != 'PyPy'",
    "mypy==1.20.2",
    "mypy-extensions==1.1.0",
    "pathspec==1.1.1",
    "PyYAML==6.0.3",
    "ruff==0.16.1",
    "tomli==2.4.1; python_version < '3.11'",
    "typing-extensions==4.16.0",
]
```

`[dependency-groups]` describes internal development environments. A future
`[project.optional-dependencies]` table would describe published consumer features
and requires an intentional packaging decision; the two mechanisms are not
interchangeable.

## Caveats and edge cases

- Exact direct and transitive pins improve repeatability but do not replace source,
  release, artifact, and vulnerability review. A reviewed hash lock can strengthen
  the boundary later.
- Exercise every environment-marker branch and every supported operating system
  in CI. A full Cartesian matrix is required when a marker depends on both Python
  and OS; otherwise a documented boundary/OS smoke matrix is sufficient.
- Do not use editable installation to analyze Tedd; it invokes packaging machinery
  Tedd intentionally does not declare.

## Verification

- Parse the file with `tomllib` on Python 3.11+ and the checked compatibility path
  on Python 3.10.
- Confirm `[project]` and `[build-system]` remain absent.
- Confirm the CI dependency-smoke and quality jobs collectively resolve the
  Python 3.10 marker branch and the other branch across Windows, macOS, and Linux.
- In an approved isolated environment, install the group and run the repository
  quality wrapper; review every changed pin and resolved artifact before commit.
