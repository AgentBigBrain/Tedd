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

```toml
[tool.ruff]
target-version = "py310"
line-length = 100
force-exclude = true
include = [
    "scripts/**/*.py",
    "plugins/tedd/skills/tedd/scripts/tedd_profile.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/knowledge_graph*.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/platform_support.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/windows_acl_policy.py",
    "plugins/tedd/skills/obsidian-knowledge-graph/scripts/tedd_knowledge_graph/**/*.py",
]

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "B"]
```

Tedd's current production scope has no import-order exemptions. New production
modules must enter the explicit scope in the same change; the release verifier
checks that discovery and configuration remain aligned. During a future rollout,
prefer a narrow ignore with an owner, rationale, and removal condition over a
global ignore, and do not describe that transitional state as full compliance.
Run checks before applying fixes, and never apply unsafe fixes by default.

## Caveats and edge cases

- `UP`, `PTH`, `SIM`, security, and broad-exception rules can be valuable but may
  change compatibility, evaluation order, platform behavior, or established style.
- `S110` and `S112` detect specific silent-handler shapes; they do not prove every
  caught exception has correct recovery semantics.
- A rule's fix classification and behavior can change between Ruff versions.
- For a future `C901`, `UP` or `SIM` trial, run the pinned Ruff on a named bounded
  scope without fixes. Record the command, configuration, findings and useful
  versus noisy signal; inspect Python 3.10 and platform semantics and test candidate
  fixes before proposing policy. Counts from another scope are not a baseline.
  This guidance does not enable those rules or create permanent exemptions.

## Verification

- Confirm the exact Ruff version and configuration loaded by the quality wrapper.
- Review lint findings by rule code and classify defects, intentional patterns, and
  temporary baseline debt.
- After any fix, run focused tests, the full quality gate, and `git diff --check`.
