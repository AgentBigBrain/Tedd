# <Rule title>

Use this structure when it helps explain a rule. Keep the invariant, applicability,
examples, caveats and verification evidence clear; headings and grouping may vary
with the subject. YAML frontmatter and related guidance are optional. The package
checker validates discoverability and provenance, not a fixed prose outline.

## Invariant

State the observable correctness, security, durability, compatibility, maintainability,
or measured-performance property this rule protects. Describe the failure it prevents;
do not assign a universal severity.

## Applies when

- List concrete conditions that make the technique appropriate.
- Name the boundary, lifecycle, scale, or repeated variation that justifies it.

## Does not apply when

- List common cases where simpler code or another technique is safer.
- Identify assumptions that must not be inferred from the rule.

## Bad example

Explain exactly why this example violates the stated invariant in this context.

```python
# Small, synthetic example of the risky behavior.
```

## Good example

Show the smallest cohesive correction. Keep it compatible with Python 3.10 and avoid
adding a runtime dependency unless the card explicitly describes an already-authorized
optional integration.

```python
# Small, synthetic example that enforces the invariant.
```

## Caveats and compatibility

- **Python versions:** Record relevant floor, platform, or newer-API constraints.
- **Dependencies:** State whether the example is standard-library only; identify any
  optional or development-only package explicitly.
- **Tradeoffs:** Describe readability, lifecycle, memory, performance, or maintenance
  costs and the evidence needed to accept them.
- **Trust boundaries:** Note any filesystem, subprocess, secret, concurrency, or
  durability concerns; remove this bullet only when genuinely irrelevant.

## Verification

- Name the focused success or regression test.
- Exercise the important failure or edge condition.
- Include an unaffected control when it materially strengthens the proof.
- Name the appropriate repository gate; do not treat a Markdown example as execution
  evidence.

## Related guidance (optional)

- Category router: `<category>.md`
- Related local rule cards or repository contracts: `<paths>`
