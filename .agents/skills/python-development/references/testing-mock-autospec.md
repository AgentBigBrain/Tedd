# Enforce Real Call Signatures in Test Doubles

Category policy: [Testing](testing.md).

## Tedd rule and invariant

Use `autospec=True` or `create_autospec` when a test double stands in for a
callable whose signature is part of the contract. A test must fail when production
code calls that dependency with invalid arguments.

## Applies when

- Patching a function, method, class, or callable collaborator.
- Refactoring a boundary where signature drift would otherwise pass silently.
- Interaction arguments are themselves observable behavior.

## Does not apply when

- A small fake exercises richer state more faithfully.
- The dependency is a dynamic object whose attributes are intentionally open.
- The test only needs a sentinel value and does not replace a callable contract.

## Bad example

```python
with patch("capture.plan_store.publish") as publish:
    save_plan("plan-1")
    publish.assert_called_once_with("plan-1")

# A plain Mock accepts calls the real publish(*, plan_id, digest) rejects.
```

## Corrected Tedd example

```python
with patch("capture.plan_store.publish", autospec=True) as publish:
    save_plan("plan-1")
    publish.assert_called_once_with(plan_id="plan-1", digest=ANY)
```

For a standalone double, use `create_autospec(PlanStore, instance=True,
spec_set=True)`. Keep return values and raised exceptions realistic; autospec does
not validate their types or semantics.

## Caveats and edge cases

- `spec=` and `spec_set=` constrain attributes but are not substitutes for
  autospec's call-signature enforcement.
- Autospeccing descriptors or objects with dynamic attribute access can execute
  introspection behavior or expose an inaccurate surface. Scope it narrowly.
- Modern `Mock` blocks common misspelled `assert*` attributes by default, but that
  protection does not make a plain mock signature-safe.

## Verification

- Add a focused control that calls the double with an invalid argument and confirm
  it raises `TypeError`.
- Patch the name where the code under test looks it up.
- Run the test after a harmless implementation refactor to confirm it asserts the
  contract rather than private wiring.
