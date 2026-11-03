# Adopt Mypy Gradually With Real Diagnostics

Category policy: [Tooling](tooling.md).

## Contents

- [Invariant](#tedd-rule-and-invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-edge-cases)
- [Verification](#verification)

## Tedd rule and invariant

Use exactly `mypy==1.20.2` as one enforced static checker. Start from a passing,
explicit module scope; tighten stable boundaries incrementally; and suppress only
the diagnostic actually emitted, with a reason and removal path.

## Applies when

- Adding annotations or reducing `Any` in a checked module.
- Expanding mypy's file scope or enabling another flag.
- Introducing a local stub, cast, override, or `type: ignore`.

## Does not apply when

- Labeling the legacy repository strict while broad overrides remain.
- Using a cast or annotation instead of runtime validation for untrusted input.
- Auto-installing latest stubs or disabling errors merely to pass CI.

## Bad example

```toml
[tool.mypy]
strict = true
ignore_errors = true
```

```text
# Non-executable anti-pattern:
<type-checker> <auto-install-latest-stubs>
```

The configuration contradicts itself, and automatic stub installation selects
unreviewed mutable packages.

## Corrected Tedd example

```toml
[tool.mypy]
python_version = "3.10"
platform = "win32"
files = [
    "scripts",
    "plugins/tedd/skills/tedd/scripts/tedd_profile.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_file_io.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v3_projection.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v3_validation.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v4.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v4_cli.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/platform_support.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v4_capture.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v4_integration.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph_v4_validation.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/windows_acl_policy.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/tedd_knowledge_graph",
]
check_untyped_defs = true
disallow_any_generics = true
disallow_incomplete_defs = true
disallow_untyped_defs = true
follow_imports = "skip"
ignore_missing_imports = true
no_implicit_optional = true
pretty = true
show_error_codes = true
strict_equality = true
warn_redundant_casts = true
warn_return_any = true
warn_unreachable = true
warn_unused_ignores = true
```

Tedd's current configuration checks every production module with no disabled
diagnostic codes or explicit `Any`. It fixes mypy's target to `win32` on every host
so platform-specific Windows security APIs receive deterministic typeshed coverage;
the release matrix separately exercises runtime behavior on Windows, macOS, and
Linux. Dynamic legacy documents remain `object` or recursive JSON values until a
runtime-validated boundary narrows them; production modules then use `TypedDict`
and `Protocol` contracts for exact internal shapes. The release verifier discovers
production modules and fails if Ruff or mypy scope falls behind. Its AST gate
independently rejects explicit `Any` in every production module.

```python
def load_users() -> list[User]:
    # A separate adapter contract test proves this generated API returns list[User];
    # remove the ignore when its reviewed stub is added.
    return legacy_api.get_users()  # type: ignore[no-any-return]
```

Place `mypy==1.20.2` and any reviewed stub package at an exact pin in the existing
PEP 735 development group. Verify the diagnostic code with that pinned version.

## Caveats and edge cases

- `no-any-return` applies to a typed function returning `Any`; it is not the code
  for an arbitrary assignment. Use the code mypy actually reports.
- `cast` changes only the checker's view. Use it after runtime evidence establishes
  the type, not as validation.
- Per-module overrides inherit flags not explicitly changed. Describe a baseline as
  partial and give each disabled code a removal path.
- Script layouts and skipped imports can make module patterns surprising; verify the
  files mypy actually checks.

## Verification

- Run mypy through the repository quality wrapper and confirm version `1.20.2`.
- Enable `warn_unused_ignores` and remove suppressions that no longer match.
- Add a deliberate local type error when validating new scope, confirm the gate
  catches it, then revert the probe and run the full quality gate.
