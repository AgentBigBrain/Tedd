# Upstream rule coverage

This manifest accounts for every substantive rule reviewed from
[`ludo-technologies/python-best-practices`](https://github.com/ludo-technologies/python-best-practices/tree/8f2d989c1e921ef9ed2a8284f2be8870dbca32d2/skills)
at commit `8f2d989c1e921ef9ed2a8284f2be8870dbca32d2`. It is a historical review record,
not a synchronization list. Tedd owns the local cards and may diverge further as its
contracts change.

Local filenames describe Tedd's policy rather than the upstream framework/tool.
Generic cards may be concise; each mapped idea and its disposition remain retained.

The upstream `_sections.md` and `_template.md` helper files are structural rather than
substantive rules. Tedd replaces them with explicit routing in [SKILL.md](../SKILL.md)
and a local [authoring template](rule-authoring-template.md). The 45 substantive
upstream cards are mapped below: 26 coding, 12 testing, and 7 tooling.

“Corrected” means the useful engineering idea was retained but a factual claim, unsafe
example, framework mandate, compatibility assumption, or missing edge case was changed.
“Renamed” means the upstream title would misrepresent Tedd's policy. None of these cards
authorizes an unreviewed dependency, installer, plugin, analyzer, or code execution.

## Coding standards: 26 of 26

| Reviewed upstream rule | Tedd-native card | Disposition |
| --- | --- | --- |
| `skills/coding-standards/rules/error-no-silent-exceptions.md` | [Error handling](coding-error-no-silent-exceptions.md) | Retained and strengthened for durability failures, narrow recovery, and exception causes. |
| `skills/coding-standards/rules/async-context-manager.md` | [Async resource lifecycle](coding-async-resource-lifecycle.md) | Renamed; retains lifecycle cleanup without adding an HTTP dependency. |
| `skills/coding-standards/rules/async-create-task.md` | [Async task ownership](coding-async-task-ownership.md) | Renamed and corrected for strong references, result retrieval, cancellation, and shutdown. |
| `skills/coding-standards/rules/async-gather.md` | [Async concurrency results](coding-async-concurrency-results.md) | Renamed and corrected for dependency ordering, exception inspection, and Python-version behavior. |
| `skills/coding-standards/rules/async-semaphore.md` | [Bounded async work](coding-async-bounded-work.md) | Renamed and corrected: a semaphore limits active bodies, not materialized tasks or payloads. |
| `skills/coding-standards/rules/design-dependency-injection.md` | [Dependency injection](coding-design-dependency-injection.md) | Retained contextually; no service-container or mocking-framework mandate. |
| `skills/coding-standards/rules/design-early-return.md` | [Control flow](coding-design-control-flow.md) | Renamed; guard clauses remain useful, but branch order follows semantics and safety rather than folklore. |
| `skills/coding-standards/rules/design-no-global-singleton.md` | [Shared state](coding-design-shared-state.md) | Renamed; distinguishes mutable hidden state from valid constants, loggers, and supervised state. |
| `skills/coding-standards/rules/design-philosophy.md` | [Pragmatic design](coding-design-pragmatism.md) | Corrected to treat DRY, YAGNI, and KISS as judgment aids rather than numeric rules. |
| `skills/coding-standards/rules/design-pure-functions.md` | [Pure boundaries](coding-design-pure-boundaries.md) | Renamed and corrected: purity aids reasoning but does not imply deep immutability, safety, or cacheability. |
| `skills/coding-standards/rules/design-single-responsibility.md` | [Responsibility seams](coding-design-responsibility-seams.md) | Renamed; focuses on ownership and change seams rather than word or import-count heuristics. |
| `skills/coding-standards/rules/doc-docstring.md` | [API contracts](coding-documentation-api-contracts.md) | Renamed; documents public behavior, invariants, side effects, and failures without a universal style mandate. |
| `skills/coding-standards/rules/doc-type-hints.md` | [Public typing contracts](coding-typing-public-contracts.md) | Renamed and expanded; annotations complement rather than replace runtime validation. |
| `skills/coding-standards/rules/validation-pydantic.md` | [Boundary data validation](coding-validation-boundary-data.md) | Renamed and corrected to preserve Tedd's standard-library runtime; Pydantic is not a default dependency. |
| `skills/coding-standards/rules/oop-composition-over-inheritance.md` | [Composition and inheritance](coding-design-composition.md) | Renamed; composition is preferred only when lifecycles and substitutability justify it. |
| `skills/coding-standards/rules/oop-dataclass.md` | [Dataclasses](coding-data-dataclasses.md) | Corrected for mutable defaults, shallow freezing, hashability, representations, and derived fields. |
| `skills/coding-standards/rules/oop-property.md` | [Properties](coding-data-properties.md) | Retained contextually with validation, caching, synchronization, and stale-value caveats. |
| `skills/coding-standards/rules/oop-protocol.md` | [Protocols](coding-typing-protocols.md) | Renamed and corrected: runtime protocol checks prove shape, not behavioral contracts. |
| `skills/coding-standards/rules/perf-dict-get.md` | [Mapping access](coding-collections-mapping-access.md) | Renamed and corrected for missing-versus-`None`, eager defaults, sentinels, and mutation. |
| `skills/coding-standards/rules/perf-generator-expression.md` | [Materialization](coding-performance-materialization.md) | Renamed and corrected: generators are not universally faster or lower-memory end to end. |
| `skills/coding-standards/rules/perf-list-comprehension.md` | [Comprehensions](coding-collections-comprehensions.md) | Renamed; prioritizes readable transformations and measured behavior over blanket speed claims. |
| `skills/coding-standards/rules/perf-set-lookup.md` | [Membership structures](coding-collections-membership.md) | Renamed and corrected for construction cost, average complexity, memory, order, and hashability. |
| `skills/coding-standards/rules/perf-str-join.md` | [Text building and serialization](coding-text-building-and-serialization.md) | Renamed and expanded to require format-aware JSON, CSV, XML, URL, and SQL handling. |
| `skills/coding-standards/rules/solid-isp.md` | [Interface segregation](coding-design-interface-segregation.md) | Renamed; consumer-specific capabilities are introduced only when real consumers differ. |
| `skills/coding-standards/rules/solid-lsp.md` | [Substitutability](coding-design-substitutability.md) | Renamed and expanded for preconditions, postconditions, failure behavior, and contract tests. |
| `skills/coding-standards/rules/solid-ocp.md` | [Extension points](coding-design-extension-points.md) | Renamed; extension seams are justified by expected variants, not created for every branch. |

## Testing: 12 of 12

| Reviewed upstream rule | Tedd-native card | Disposition |
| --- | --- | --- |
| `skills/testing/rules/fixture-conftest.md` | [Shared test support](testing-fixture-shared-support.md) | Corrected for `unittest`; promote helpers only after a real second consumer. |
| `skills/testing/rules/fixture-factory.md` | [Synthetic factories](testing-fixture-factory.md) | Retained with keyword-only overrides, deterministic identity, fresh state, and cleanup. |
| `skills/testing/rules/fixture-scope.md` | [Fixture lifetime](testing-fixture-scope.md) | Corrected for mutable isolation, setup failures, temporary paths, and process-global state. |
| `skills/testing/rules/mock-autospec.md` | [Autospec](testing-mock-autospec.md) | Corrected: `spec` and `spec_set` restrict attributes but do not enforce call signatures like autospec. |
| `skills/testing/rules/mock-boundaries.md` | [Mock boundaries](testing-mock-boundaries.md) | Corrected for lookup-site patching, observable outcomes, real temporary resources, and small fakes. |
| `skills/testing/rules/mock-monkeypatch.md` | [Scoped global changes](testing-mock-monkeypatch.md) | Rewritten with standard-library patching and explicit parallelism limitations. |
| `skills/testing/rules/param-ids.md` | [Case identifiers](testing-param-ids.md) | Rewritten with stable, non-secret `subTest` identifiers. |
| `skills/testing/rules/param-parametrize.md` | [Case tables](testing-param-subtest-cases.md) | Rewritten for `unittest.subTest`, coherent assertion shapes, and bounded combinations. |
| `skills/testing/rules/struct-aaa-pattern.md` | [Arrange, act, assert](testing-struct-aaa-pattern.md) | Corrected: a readability heuristic, not a mandatory one-action layout for every integration test. |
| `skills/testing/rules/struct-no-logic.md` | [Test logic](testing-struct-no-logic.md) | Corrected substantially: invariant, race, numerical, migration, and reference-model logic can be legitimate. |
| `skills/testing/rules/struct-one-behavior.md` | [One behavioral reason](testing-struct-one-behavior.md) | Corrected to allow multiple assertions that prove one coherent outcome. |
| `skills/testing/rules/struct-test-naming.md` | [Test naming](testing-struct-test-naming.md) | Retained contextually without enforcing one verbose naming grammar. |

## Tooling: 7 of 7

| Reviewed upstream rule | Tedd-native card | Disposition |
| --- | --- | --- |
| `skills/tooling/rules/analysis-pyscn.md` | [Structural analysis](tooling-analysis-pyscn.md) | Corrected to a tool-neutral, evidence-based checklist; no unpinned analyzer, MCP, or `uvx` execution. |
| `skills/tooling/rules/fmt-ruff.md` | [Ruff formatting](tooling-fmt-ruff.md) | Retained with exact pins, check-before-write workflow, and scoped adoption. |
| `skills/tooling/rules/lint-ruff.md` | [Ruff linting](tooling-lint-ruff.md) | Corrected for target-version accuracy, reviewed rule sets, safe fixes, and temporary scoped exceptions. |
| `skills/tooling/rules/pkg-pyproject.md` | [Repository metadata](tooling-pkg-pyproject.md) | Corrected for a tool-only `pyproject.toml`, PEP 735 dependency groups, and non-distribution repositories. |
| `skills/tooling/rules/pkg-uv.md` | [Dependency-manager semantics](tooling-pkg-reviewed-dependencies.md) | Corrected: no unsafe installer or `uvx`; sync, groups, extras, locking, and pip commands are not interchangeable. |
| `skills/tooling/rules/test-pytest.md` | [Unittest and coverage](tooling-test-unittest.md) | Renamed in substance for Tedd's `unittest` suite, measured branch coverage, and narrow warning policy. |
| `skills/tooling/rules/type-mypy.md` | [Gradual mypy](tooling-type-mypy.md) | Corrected for pinned gradual adoption, real diagnostic codes, runtime evidence, and no automatic type installation. |

## Deliberately excluded upstream machinery

Tedd does not carry the upstream metadata generator, update workflow, automatic rule
discovery code, severity labels, path-trigger syntax, or synchronization scripts. Those
mechanisms are not needed for a repository-local skill and would make provenance review
and divergence harder. Rule cards are selected through the local index and category
routers, while repository verification checks that every mapped card remains present.
