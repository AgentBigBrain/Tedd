# Design and maintainability

## Choose seams from responsibilities

- Split modules around stable responsibilities and data ownership, not arbitrary line counts.
- Keep orchestration thin: parse and validate boundary input, call domain functions, serialize the result, and map known failures.
- Move cohesive storage, validation, projection, migration, platform, or command families behind explicit module interfaces when tests prove behavior can remain stable.
- Prevent new code from deepening a legacy dependency. During staged migration, use a narrow compatibility adapter and track its removal rather than duplicating behavior.
- Treat cross-module underscore imports as a seam to review. If a helper has real
  shared consumers, expose a cohesive public contract with behavior tests; otherwise
  retain a documented narrow compatibility adapter. Stdlib, dunder and necessary
  platform imports need their own compatibility evidence, not a blanket ban.

## Refactor large modules incrementally

1. Characterize public commands, imports, constants, and monkeypatch surfaces used by tests.
2. Extract a low-coupling leaf with deterministic tests.
3. Preserve one compatibility import path while callers migrate.
4. Move tests with the behavior and add contract tests at the adapter boundary.
5. Remove the compatibility path only after imports and consumers are verified.

Prefer slices such as platform security, SQLite URI construction, atomic publication, error serialization, or validation helpers before attempting a wholesale rewrite of the graph module.

## Use state and abstraction deliberately

- Prefer explicit parameters and return values over mutable module globals.
- Inject clocks, randomness, filesystem primitives, and subprocess runners when deterministic tests or platform behavior require a seam. Do not introduce a container or framework solely to claim dependency injection.
- Use pure functions for normalization, classification, fingerprints, and validation when practical. Keep side effects close to named boundaries.
- Use `Protocol` for structural interfaces with multiple implementations or test doubles; avoid an interface that merely mirrors one concrete class.
- Prefer composition when components have independent lifecycles. Use inheritance when substitutability is real and tested.

## Model data honestly

- Use dataclasses for value-oriented records when generated initialization and comparison help.
- Treat frozen dataclasses as shallow assignment protection. Mutable members remain mutable; generated hashing depends on field hashability; generated `repr` can expose sensitive values.
- Exclude secrets from representations explicitly and avoid storing secrets in general-purpose data models.
- Keep invariants in constructors or named factories when invalid instances must never exist. A structure may still expose derived properties or small invariant-preserving methods.

## Keep comments and documentation useful

- Document why an invariant, platform exception, or non-obvious tradeoff exists.
- Avoid comments that merely restate syntax or claim a guarantee the code does not enforce.
- Update command examples, compatibility claims, and architecture docs with the code that changes them.
