# Materialize Data Deliberately

## Invariant

Use a generator or streaming iterator when data is single-pass and incremental
processing preserves the contract. Materialize a list or tuple when callers need a
stable snapshot, repeated traversal, indexing, or deterministic failure timing.

## Applies when

- A transform feeds directly into `sum`, `any`, `all`, or one loop.
- Input may be large and holding every transformed value provides no benefit.
- A producer can enforce byte or record bounds while yielding.

## Does not apply when

- The result must be traversed more than once.
- The operation needs a stable snapshot before state can change.
- Deferring an exception until iteration would violate an API contract.

## Bad example

The intermediate size list is retained even though `sum` consumes it once.

```python
def total_payload_bytes(records: list[bytes]) -> int:
    sizes = [len(record) for record in records]
    return sum(sizes)
```

## Good example

```python
from collections.abc import Iterable, Iterator


def iter_payload_sizes(records: Iterable[bytes]) -> Iterator[int]:
    for record in records:
        yield len(record)


def total_payload_bytes(records: Iterable[bytes]) -> int:
    return sum(iter_payload_sizes(records))
```

## Caveats and compatibility

- A generator has bounded incremental overhead only if its source and consumer do
  not already retain all data. Calling `list(generator)` materializes it again.
- Generators are stateful, single-use iterators and can hold files or transactions
  open until exhausted or closed.
- A list may be safer when validation must complete before any mutation begins.
- "O(1) memory" is not a blanket claim: yielded objects, buffers, source state, and
  downstream accumulation still count.
- Generator functions and expressions are fully supported on Python 3.10.

## Verification

- [ ] The consumer is genuinely single-pass.
- [ ] Resource ownership remains clear if iteration stops early.
- [ ] Validation occurs before irreversible effects when required.
- [ ] Representative memory use is measured rather than inferred from syntax.
- [ ] Tests cover empty input, partial iteration, and producer failure.

## Related guidance

- [Comprehensions](coding-collections-comprehensions.md)
- [Performance and scale](performance.md)
