# Keep the test oracle independent

Test logic is valid when loops, reference models, branches or concurrency controls
express an independent invariant. A copy of the production algorithm can reproduce
the same bug; a branch that reaches no assertion proves nothing.

Prefer literal documented vectors or a simpler independently sourced oracle.
For each branch, identify its assertion, expected exception or explicit justified
skip. Platform conditions must report an unavailable integration rather than
silently omit evidence. Race tests need barriers/events or injected primitives,
not timing-only sleeps.

Ask whether a defect in production would make the oracle disagree. Keep per-case
`subTest` diagnostics where cases are independent. See [testing](testing.md) for
bounded parser mutation, migration and interruption controls.
