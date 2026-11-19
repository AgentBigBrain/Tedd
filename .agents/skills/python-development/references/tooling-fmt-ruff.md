# Roll Out Ruff Formatting Deliberately

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Use the repository-pinned `ruff==0.16.1` formatter only on the reviewed scope.
Formatting must preserve Python 3.10 syntax, avoid unrelated plugin churn, and be
checked separately from any write or lint fix.

## Applies when

- Editing Python files already included by Tedd's Ruff configuration.
- Expanding formatting scope in a dedicated, reviewed change.
- Verifying that CI and local formatting use the same pinned version.

## Does not apply when

- The checkout or analyzer package has not been inspected and approved for execution.
- Formatting generated, vendored, migration, or compatibility code would obscure
  meaningful history without a specific decision.
- A behavior fix is being mixed with a repository-wide reformat.

## Bad example

```text
# Non-executable anti-patterns:
<package-manager> install --upgrade <formatter>
<formatter> write <entire-repository>
<linter> fix <entire-repository> <unsafe-mode>
```

This selects a mutable version, writes every Python file, and combines formatting
with potentially semantic lint fixes.

## Corrected Tedd example

Use the formatter scope declared in the repository's `pyproject.toml`.
To check a single included file, run the pinned Ruff from the repository root.

```bash
python -m ruff format --check plugins/tedd/skills/tedd/scripts/tedd_profile.py
```

Tedd's current production scope has no formatter exclusions. In an inspected
checkout and approved isolated development environment, run the repository
quality wrapper for the authoritative check. Use `ruff format --check` before a
write; perform any write separately and inspect the exact diff.

## Caveats and edge cases

- Ruff formatting and Ruff import sorting are separate operations; import sorting
  is an `I` lint rule and fix.
- Ruff normally considers explicitly passed paths even when they match an exclude.
  Tedd sets `force-exclude = true`; keep that guard or require wrapper-only
  invocation whenever excluded plugin paths remain.
- Ruff aims for Black-compatible style but is not a promise of byte-for-byte output
  across all versions. The exact pin is part of the review baseline.
- Exclusions are rollout debt, not proof excluded code is correctly formatted.

## Verification

- Confirm `ruff --version` reports `0.16.1` in the approved environment.
- Run `python scripts/verify_release.py --quality` and inspect any proposed write diff.
- Compile and test reformatted modules on Python 3.10 and the current stable version.
