# Enforce a Reviewed Ruff Lint Baseline

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Use `ruff==0.16.1` with `target-version = "py310"` and a checked-in, passing rule
subset. Expand scope or rules only when findings are reviewed, compatibility is
preserved, and temporary ignores have an owner and removal path.

## Applies when

- Adding or changing Python included by the current Ruff gate.
- Introducing a high-signal rule family in a bounded change.
- Replacing a blanket exception or import-order baseline with verified fixes.

## Does not apply when

- Selecting every rule and auto-fixing the repository without semantic review.
- Treating a clean lint result as proof of correctness, security, or durability.
- Silencing a real defect merely to keep the gate green.

## Bad example

```text
# Non-executable anti-pattern:
<linter> check <entire-repository> <all-rules> <write-unsafe-fixes>
```

## Corrected Tedd example

The repository's `pyproject.toml` lists the included files and enabled rules.
From the repository root, check one included file with the pinned Ruff in your
development environment.

```bash
python -m ruff check plugins/tedd/skills/tedd/scripts/tedd_profile.py
```

Tedd's current production scope has no import-order exemptions. New production
modules must enter the explicit scope in the same change; the release verifier
checks that discovery and configuration remain aligned. During a future rollout,
prefer a narrow ignore with an owner, rationale, and removal condition over a
global ignore, and do not describe that transitional state as full compliance.
Run checks before applying fixes, and never apply unsafe fixes by default.

## Reviewing function growth

The quality verifier checks two measures across the existing Ruff include list.
From the repository root, reproduce the trial with Ruff 0.16.1:

```bash
python -m ruff check . --no-fix --ignore-noqa --select C901,PLR0915 --config "lint.mccabe.max-complexity=25" --config "lint.pylint.max-statements=100"
```

On commit `1153d72`, 25 functions exceeded complexity 25, 13 exceeded 100
statements, and nine crossed both thresholds. The 29 distinct functions include
the source MCP dispatcher, which mixes tool responsibilities, and nine workflow
functions worth reviewing when their behavior changes. Twelve format-reader
functions follow format rules in sequence. Two option registries, two repository
checks, and three tests also crossed a threshold. Splitting these solely for a
lower count can make the sequence harder to review.

Run `python scripts/check_function_growth.py` to compare the counts with the
reviewed entries in `scripts/function_growth_baseline.json`.
The checker uses the repository's Ruff file selection and measures those files
independently of inline ignores, per-file ignores, and lint exclusions.
It identifies a function by repository path and qualified name, so moving a
definition up or down the file preserves its limits. New functions and previously
unlisted metrics use the trial thresholds. For a reported increase, review the
design first. If the increase is warranted, update the measured value, reason,
maintainer role, and review condition together. Lower or retire entries when
the responsibility shrinks or moves.

## Caveats and edge cases

- `UP`, `PTH`, `SIM`, security, and broad-exception rules can be valuable but may
  change compatibility, evaluation order, platform behavior, or established style.
- `S110` and `S112` detect specific silent-handler shapes; they do not prove every
  caught exception has correct recovery semantics.
- A rule's fix classification and behavior can change between Ruff versions.
- For a future `UP` or `SIM` trial, run the pinned Ruff on a named bounded
  scope without fixes. Record the command, configuration, findings and useful
  versus noisy signal; inspect Python 3.10 and platform semantics and test candidate
  fixes before proposing policy. Counts from another scope are not a baseline.
  This guidance does not enable those rules or create permanent exemptions.

## Verification

- Confirm the exact Ruff version and configuration loaded by the quality wrapper.
- Review lint findings by rule code and classify defects, intentional patterns, and
  temporary baseline debt.
- After any fix, run focused tests, the full quality gate, and `git diff --check`.
