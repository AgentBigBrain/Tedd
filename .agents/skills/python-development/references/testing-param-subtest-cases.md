# Use Tables for Repeated Behavior, Not Different Stories

Category policy: [Testing](testing.md).

## Tedd rule and invariant

Use a case table when inputs and expected outcomes vary but the behavior and
assertion shape remain the same. Preserve independent diagnostics with
`unittest.subTest`; keep genuinely different behaviors in separate named tests.

## Applies when

- Boundary values share one public contract.
- Platform error classifications use the same action and outcome shape.
- Adding a case should require only one explicit row.

## Does not apply when

- Cases require different setup, actions, cleanup, or explanations.
- The table would hide a stateful end-to-end scenario.
- Expected values are produced by copying the production algorithm.

## Bad example

```python
def test_all_inputs(self) -> None:
    for value in values:
        result = normalize(value)
        if value is None:
            self.assertEqual(result, "")
        elif value.startswith(" "):
            self.assertEqual(result, value.strip())
        # Some paths can execute without any assertion.
```

## Corrected Tedd example

```python
def test_normalize_whitespace_cases(self) -> None:
    cases = [
        ("empty", "", ""),
        ("leading", "  note", "note"),
        ("trailing", "note  ", "note"),
        ("unicode-space", "\u2003note", "note"),
    ]

    for case, raw, expected in cases:
        with self.subTest(case=case):
            self.assertEqual(normalize(raw), expected)
```

## Caveats and edge cases

- A loop in a test is not inherently wrong. The invariant and independent
  per-case diagnostic matter.
- `subTest` iterations share the same `TestCase` instance, `setUp`, local scope,
  and mutable state. They separate diagnostics, not execution state. Keep rows
  pure, register explicit cleanup, or use separate test methods when isolation
  matters.
- Use `assertRaises` in a dedicated table only when all rows prove the same failure
  contract; otherwise separate exception behaviors.
- Cartesian products grow quickly. Select meaningful combinations instead of
  multiplying every dimension automatically.

## Verification

- Temporarily break one row and confirm later subtests still execute.
- Review whether each expected value is a literal, an independent oracle, or a
  documented invariant rather than duplicated production logic.
- Confirm every row has exactly one clear behavioral reason to exist.
