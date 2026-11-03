---
name: python-development
description: Apply Tedd-native Python standards when changing or reviewing Python code, tests, or tooling in the Tedd repository. This repository-local skill is not shipped in the plugin.
---

# Python Development

Use this repository-local skill to improve Tedd's Python without weakening its privacy, durability, portability, or compatibility contracts. Treat the guidance as decision support: establish the relevant invariant, inspect current behavior, and choose the smallest evidence-backed change.

## Start safely

Preserving supported Windows, macOS, and Linux behavior is mandatory, not optional
style advice. Read the checkout's `CONTRIBUTING.md` before editing. For platform
changes, also read the applicable correctness, testing, and security references
below; their cross-platform requirements are mandatory.

1. Confirm the checkout, branch, dirty state, local instructions, supported Python versions, and existing verification path.
2. Preserve unrelated changes and runtime data. Never use a production knowledge graph as a test fixture.
3. Treat a checkout as untrusted until its executable configuration and relevant scripts have been inspected. Do not install dependencies or run tests, build backends, analyzers, plugins, hooks, or MCP servers from an untrusted checkout without explicit approval and isolation.
4. In an already inspected checkout and authorized isolated environment, run focused tests and in-scope repairs without asking again at each step. Recheck trust when executable setup, dependencies, or target environment materially changes. Prefer the repository's release wrapper for final verification.
5. Keep Tedd's runtime free of third-party dependencies unless the product decision explicitly changes.

## Select the relevant guidance

- Read [references/correctness.md](references/correctness.md) for storage, error handling, serialization, path handling, concurrency, or compatibility changes.
- Read [references/design.md](references/design.md) before refactoring modules, boundaries, state, dataclasses, or protocols.
- Read [references/typing.md](references/typing.md) when adding annotations, reducing `Any`, defining data shapes, or configuring a type checker.
- Read [references/testing.md](references/testing.md) before changing tests, fixtures, mocks, concurrency tests, or test execution.
- Read [references/tooling.md](references/tooling.md) for `pyproject.toml`, Ruff, type checking, coverage, dependency groups, or CI.
- Read [references/security.md](references/security.md) for filesystem trust boundaries, secrets, subprocesses, supply chain, or bounded untrusted-file parsing; [testing.md](references/testing.md#bounded-parser-controls) supplies synthetic mutation and deadline controls.
- Read [references/performance.md](references/performance.md) only for measured latency, memory, scale, or concurrency work.

Load only the focused cards needed for the current change. Cards preserve the relevant
invariants, edge cases and verification advice; examples appear where they clarify a
Tedd boundary. None overrides its compatibility, dependency, privacy or durability contracts.

### Coding cards

- For errors and asynchronous work, read [observable failures](references/coding-error-no-silent-exceptions.md), [resource lifecycles](references/coding-async-resource-lifecycle.md), [task ownership](references/coding-async-task-ownership.md), [concurrent results](references/coding-async-concurrency-results.md), or [bounded work](references/coding-async-bounded-work.md).
- For responsibilities and state, read [dependency injection](references/coding-design-dependency-injection.md), [control flow](references/coding-design-control-flow.md), [shared state](references/coding-design-shared-state.md), [pragmatic design](references/coding-design-pragmatism.md), [pure boundaries](references/coding-design-pure-boundaries.md), or [responsibility seams](references/coding-design-responsibility-seams.md).
- For public contracts and data models, read [API documentation](references/coding-documentation-api-contracts.md), [public typing](references/coding-typing-public-contracts.md), [boundary validation](references/coding-validation-boundary-data.md), [composition](references/coding-design-composition.md), [dataclasses](references/coding-data-dataclasses.md), [properties](references/coding-data-properties.md), or [protocols](references/coding-typing-protocols.md).
- For collections, serialization, and measured performance, read [mapping access](references/coding-collections-mapping-access.md), [materialization](references/coding-performance-materialization.md), [comprehensions](references/coding-collections-comprehensions.md), [membership structures](references/coding-collections-membership.md), or [text and structured serialization](references/coding-text-building-and-serialization.md).
- For interface behavior, read [interface segregation](references/coding-design-interface-segregation.md), [substitutability](references/coding-design-substitutability.md), or [extension points](references/coding-design-extension-points.md).

### Testing cards

- For synthetic setup, read [shared support](references/testing-fixture-shared-support.md), [factories](references/testing-fixture-factory.md), and [fixture lifetime](references/testing-fixture-scope.md).
- For doubles and global state, read [autospec](references/testing-mock-autospec.md), [mock boundaries](references/testing-mock-boundaries.md), and [scoped global changes](references/testing-mock-monkeypatch.md).
- For repeated cases, read [diagnostic IDs](references/testing-param-ids.md) and [`subTest` tables](references/testing-param-subtest-cases.md).
- For test structure, read [arrange-act-assert](references/testing-struct-aaa-pattern.md), [legitimate test logic](references/testing-struct-no-logic.md), [one behavioral reason](references/testing-struct-one-behavior.md), and [behavioral names](references/testing-struct-test-naming.md).

### Tooling cards

- Read [structural analysis](references/tooling-analysis-pyscn.md), [Ruff formatting](references/tooling-fmt-ruff.md), [Ruff linting](references/tooling-lint-ruff.md), [repository-only pyproject metadata](references/tooling-pkg-pyproject.md), [dependency-manager semantics](references/tooling-pkg-reviewed-dependencies.md), [`unittest` and branch coverage](references/tooling-test-unittest.md), or [gradual mypy](references/tooling-type-mypy.md) for the matching development-tool change.

Use the [rule authoring template](references/rule-authoring-template.md) for new or
materially revised cards. Consult the pinned [upstream coverage
manifest](references/UPSTREAM_COVERAGE.md) to verify whether an upstream idea was retained,
corrected, renamed, or intentionally rejected.

## Apply the engineering workflow

1. State the behavior and invariant being protected. Separate correctness, security, durability, maintainability, and performance claims.
2. Reproduce the failure safely and identify an unaffected control when fixing a defect.
3. Add a regression test that fails for the verified reason. Permit test logic when it expresses an invariant or concurrency schedule without copying production logic.
4. Implement the smallest cohesive fix. Preserve Python 3.10 compatibility unless the repository's declared floor changes.
5. Fail closed at trust and durability boundaries. Catch only exceptions whose recovery semantics are known; preserve causes when translating errors.
6. Run focused tests while iterating and the required quality and release gates on the stable candidate, in proportion to the change. After a later edit, rerun the affected checks rather than the whole matrix by default; retain every required acceptance gate.
7. Review the exact diff for private data, unsafe examples, compatibility regressions, and accidental plugin packaging.

## Use judgment, not slogans

- Prefer clarity and explicit invariants over artificial line, branch, or class-size rules.
- Use early returns, pure functions, composition, dependency injection, dataclasses, and protocols when they improve the local design; do not require them universally.
- Treat `frozen=True` as shallow assignment protection, not deep immutability, guaranteed hashability, or secret-safe representation.
- Optimize after measurement. Do not label ordinary micro-optimizations critical.
- Keep `unittest` when it fits Tedd. Coverage and isolation matter more than switching test frameworks.

This skill was adapted and corrected from reviewed source material. See
[references/upstream-provenance.md](references/upstream-provenance.md) for the pinned
provenance and MIT license terms. It is Tedd-maintained: do not auto-sync it with upstream.
