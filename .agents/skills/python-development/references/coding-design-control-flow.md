# Keep Control Flow Shallow and Semantic

## Invariant

Arrange control flow so preconditions and exceptional cases are explicit while the
main state transition remains easy to follow. Early returns are a tool for clarity,
not a universal style requirement.

## Applies when

- Several independent preconditions otherwise create deep nesting.
- A loop can reject or skip an invalid item before its main work.
- Known terminal states can be returned without sharing cleanup.

## Does not apply when

- Several branches must share one cleanup, transaction, or rollback boundary.
- Multiple early exits would obscure a state machine or partial mutation.
- A single structured conditional is clearer than repeated guards.

## Bad example

Nested type and shape checks bury the successful state and make error precedence
harder to review.

```python
def capture_status(packet: object) -> str:
    if isinstance(packet, dict):
        if "records" in packet:
            records = packet["records"]
            if isinstance(records, list):
                if records:
                    return "ready"
                return "empty"
            return "invalid_records"
        return "missing_records"
    return "invalid_packet"
```

## Good example

```python
def capture_status(packet: object) -> str:
    if not isinstance(packet, dict):
        return "invalid_packet"
    if "records" not in packet:
        return "missing_records"

    records = packet["records"]
    if not isinstance(records, list):
        return "invalid_records"
    if not records:
        return "empty"

    return "ready"
```

## Caveats and compatibility

- Order guards by contract: validate type before shape, ownership before use, and
  cheap safe checks before expensive parsing. Do not reorder them for speculative
  branch-frequency gains.
- Avoid returning after a mutation unless the function's partial-state behavior is
  deliberate and tested.
- Context managers are preferable to duplicated cleanup before every return.
- Extract a named predicate when a guard condition itself is hard to understand.
- This is a readability rule with no Python-version or dependency requirement.

## Verification

- [ ] Each exit maps to a named contract outcome.
- [ ] The happy path is visible without following deep indentation.
- [ ] Cleanup and rollback occur for every exit.
- [ ] Tests cover each guard and at least one unaffected valid input.
- [ ] Refactoring did not alter error precedence or externally visible messages.

## Related guidance

- [Responsibility seams](coding-design-responsibility-seams.md)
- [Correctness and durability](correctness.md)
