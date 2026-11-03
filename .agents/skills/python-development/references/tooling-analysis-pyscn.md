# Audit Structure Without Executing an Unreviewed Analyzer

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Review dead code, clone drift, dependency cycles, coupling, and complexity after
structural changes. No structural-analysis score is authoritative, and no analyzer
may be downloaded, installed, run through `uvx`, or exposed as an MCP server until
its exact version, package contents, configuration, and execution boundary are
reviewed and approved.

## Applies when

- Extracting code from a large module or changing dependency direction.
- Suspecting copied platform/security logic or obsolete compatibility paths.
- Proposing a new structural analyzer or CI threshold.

## Does not apply when

- A numeric complexity limit is being used instead of design review.
- A one-line change has no structural effect.
- The tool version or configuration semantics have not been verified.

## Bad example

```text
# Non-executable anti-patterns:
<ephemeral-runner> <unpinned-analyzer> <repository>
<global-installer> <unpinned-analyzer>
<mcp-register> <unpinned-server>
```

Each pattern obtains or exposes unpinned executable code.

## Corrected Tedd example

```powershell
# Read-only inventory using an already trusted repository tool.
rg --files -g '*.py'
rg -n '^(from|import) ' plugins/tedd scripts tests
rg -n '^(def|class) ' plugins/tedd scripts
```

Use the inventory to document candidate dead code, duplicated responsibility,
cycles, and public imports. Confirm each candidate with call sites and tests before
removal. If pyscn is later evaluated, add an exact reviewed pin and validated
configuration in a separate change before running it.

## Caveats and edge cases

- Static tools can miss reflective imports, CLI dispatch, monkeypatch surfaces, and
  platform-only code.
- Cyclomatic complexity is a signal, not a universal defect threshold. Compare a
  measured baseline and the responsibility of the function.
- The upstream pyscn examples use inconsistent configuration shapes; verify the
  adopted version's official schema instead of copying them.

## Verification

- Trace every removal through imports, CLI commands, manifests, and tests.
- Run characterization tests before and after an extraction.
- For any proposed analyzer, record exact version, source, digest or lock evidence,
  configuration validation, false-positive baseline, and CI failure policy.
