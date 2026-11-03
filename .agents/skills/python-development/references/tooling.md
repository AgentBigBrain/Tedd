# Tooling and CI

## Keep project metadata non-distributable

Use `pyproject.toml` as the shared configuration and development-dependency surface without adding a `[build-system]` or `[project]` table unless Tedd intentionally becomes a distributable Python package.

Use the standardized PEP 735 form for development dependencies:

```toml
[dependency-groups]
dev = [
  "coverage[toml]==<reviewed-version>",
  "mypy==<reviewed-version>",
  "ruff==<reviewed-version>",
]
```

Keep development dependencies separate from published optional dependencies. Document the reviewed installation/sync command for the chosen environment manager and verify its behavior against the pinned version.

## Enforce a pragmatic quality stack

- Use Ruff for formatting and a reviewed lint subset compatible with supported Python versions.
- Introduce type checking gradually with explicit module scope and a checked-in baseline configuration.
- Measure branch coverage over production modules. Set an initial threshold from the verified baseline and raise it deliberately; never select an aspirational number that forces meaningless tests.
- Keep the standard-library release wrapper able to run without development packages. Add explicit development gates that fail clearly when requested tools are unavailable.
- Run tools through the selected interpreter or environment so CI and local results use the same versions.

## Keep dependency execution safe

- Prefer a trusted package manager or a downloaded artifact whose version and digest can be inspected. Do not teach agents to use `curl | sh` or `irm | iex` as the default.
- Pin direct development tools or use a committed lock with reviewed transitive resolution.
- Review packages and build backends before installation. Do not install an unfamiliar project in editable mode merely to analyze it.
- Treat analyzers, formatters, test plugins, hooks, and MCP servers as executable code with the checkout's privileges.

## Make CI prove the support claim

- Test every supported Python minor across a matrix that covers Linux, Windows, and macOS without multiplying identical jobs unnecessarily.
- Keep the minimum version, current stable version, and all operating systems blocking.
- Use an allowed-to-fail prerelease job only as an early warning, never as proof of support.
- Pin third-party GitHub Actions to full commit SHAs and retain a version comment for maintainability.
- Use least-privilege workflow permissions, disable persisted checkout credentials when unused, and bound job time.
- Validate that wrappers use the interpreter selected by the matrix.

Do not globally ignore every `DeprecationWarning`; narrow unavoidable external warnings. Do not assume `uv sync`, `uv pip compile`, extras, and dependency groups have interchangeable semantics.
