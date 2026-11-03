# Handle Concurrent Async Results Explicitly

## Invariant

Run operations concurrently only when they are independent, and define what happens
to every result, exception, and cancellation. Partial success must be an explicit
domain decision rather than an accidental effect of `return_exceptions=True`.

## Applies when

- A small, already-bounded set of independent I/O operations can overlap.
- The caller owns every operation and can state all-or-nothing or partial-success
  semantics.
- Result ordering and failure aggregation are part of the contract.

## Does not apply when

- One operation depends on another's result.
- Input can be very large or attacker-controlled; use bounded workers instead.
- Concurrency offers no measured benefit over simple sequential code.

## Bad example

Returning exceptions in the result list is unsafe when the consumer treats every
element as successful data.

```python
import asyncio


async def read_all() -> list[dict[str, object]]:
    results = await asyncio.gather(
        read_profile(),
        read_graph(),
        return_exceptions=True,
    )
    return [dict(result) for result in results]  # Exceptions become "data."
```

## Good example

```python
import asyncio
from collections.abc import Awaitable, Sequence


class ConcurrentReadError(RuntimeError):
    """An independent read failed."""


async def read_all(
    operations: Sequence[Awaitable[dict[str, object]]],
) -> list[dict[str, object]]:
    results = await asyncio.gather(*operations, return_exceptions=True)
    values: list[dict[str, object]] = []
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            raise ConcurrentReadError("an independent read failed") from result
        values.append(result)
    return values
```

## Caveats and compatibility

- With default gather behavior, the first propagated exception does not
  automatically cancel every sibling. Design cleanup and ownership accordingly.
- `return_exceptions=True` is valid for deliberate partial results only when each
  returned exception is inspected and mapped to a stable outcome.
- `asyncio.CancelledError` derives from `BaseException` on supported Python
  versions, so check it before ordinary exceptions.
- TaskGroup and `asyncio.timeout` require Python 3.11. A Tedd rule cannot use
  them unguarded while Python 3.10 remains supported.
- Gather materializes its awaitables; it is not a large-input backpressure tool.

## Verification

- [ ] Tests prove operations are independent.
- [ ] Success preserves the documented result order.
- [ ] Every exception result is inspected.
- [ ] Cancellation propagates and owned siblings are cleaned up.
- [ ] Large or untrusted inputs take the bounded-worker path.

## Related guidance

- [Own every async task](coding-async-task-ownership.md)
- [Async bounded work](coding-async-bounded-work.md)
