# Bound Async Work at the Producer

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Bound both active operations and materialized work. A semaphore around thousands of
already-created coroutines limits simultaneous bodies but does not bound queued
tasks, payload memory, or producer pressure.

## Applies when

- Input size can be large, external, or attacker-influenced.
- Each item performs async I/O and a fixed worker count is appropriate.
- Connection, descriptor, memory, or rate limits define a concurrency ceiling.

## Does not apply when

- The input is a tiny fixed tuple of independent operations.
- Work is CPU-bound or uses blocking filesystem APIs; use the appropriate
  synchronous or process boundary.
- The called client already supplies a proven bounded pool and producer backpressure.

## Bad example

The semaphore limits active calls, but `gather` still receives one materialized
coroutine for every input path.

```python
import asyncio


async def inspect_all(paths: list[str]) -> list[dict[str, object]]:
    semaphore = asyncio.Semaphore(8)

    async def inspect_one(path: str) -> dict[str, object]:
        async with semaphore:
            return await inspect_path(path)

    return await asyncio.gather(*(inspect_one(path) for path in paths))
    # Every coroutine is materialized before the limit takes effect.
```

## Good example

```python
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path


async def inspect_all(
    paths: Iterable[Path],
    *,
    emit: Callable[[dict[str, object]], Awaitable[None]],
    worker_count: int = 8,
    max_items: int = 10_000,
    max_result_bytes: int = 64 * 1024,
) -> int:
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    if max_items < 1 or max_result_bytes < 1:
        raise ValueError("item and result limits must be positive")

    queue: asyncio.Queue[Path | None] = asyncio.Queue(maxsize=worker_count * 2)
    processed = 0

    async def produce() -> None:
        for index, path in enumerate(paths):
            if index >= max_items:
                raise ValueError("input exceeds the item limit")
            await queue.put(path)
        for _ in range(worker_count):
            await queue.put(None)

    async def consume() -> None:
        nonlocal processed
        while True:
            path = await queue.get()
            if path is None:
                return
            result = await inspect_path(path, max_bytes=max_result_bytes)
            await emit(result)
            processed += 1

    tasks = [
        asyncio.create_task(produce()),
        *(asyncio.create_task(consume()) for _ in range(worker_count)),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return processed
```

## Caveats and compatibility

- The sink observes completion order. Add bounded indexed reordering only if the
  contract requires input order, and define the maximum reorder window.
- `inspect_path` and `emit` must enforce their own item-size and destination bounds.
  Route a non-concurrency-safe sink through one additional bounded writer queue.
- The broad `BaseException` catch is limited to cleanup and immediately
  re-raises; ordinary domain code should not suppress base exceptions.
- A bounded queue applies backpressure only if the producer awaits `put`.
- Choose limits from measured resource constraints, not a universal default.
- Avoid caching semaphores by event loop in a `WeakKeyDictionary` whose values
  can themselves retain the loop. Prefer one explicitly owned client per loop.
- The worker-queue pattern uses only Python 3.10 standard-library APIs.

## Verification

- [ ] A test records that no more than `worker_count` operations run at once.
- [ ] The queue has a finite maximum size.
- [ ] Input count and per-result bytes have explicit, exercised limits.
- [ ] Results flow to a bounded or streaming sink instead of accumulating in a list.
- [ ] A worker failure cancels and awaits the remaining tasks.
- [ ] Result ordering matches the documented contract.
- [ ] Cancellation and empty-input paths terminate without leaked tasks.

## Related guidance

- [Own every async task](coding-async-task-ownership.md)
- [Performance and scale](performance.md)
- [Testing](testing.md)
