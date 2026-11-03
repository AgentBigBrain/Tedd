# Own Async Resource Lifecycles

## Invariant

Every asynchronous resource must have one explicit owner that closes it on success,
failure, and cancellation. Prefer an async context manager when acquisition or
cleanup awaits.

## Applies when

- Opening network streams, async database connections, sessions, or locks.
- Combining multiple async resources in one bounded operation.
- Wrapping an API that exposes separate acquire and close calls.

## Does not apply when

- The resource is synchronous; use `with` or `try/finally` instead.
- The library already owns the resource for the entire application lifespan.
- A function merely borrows a resource and its caller remains the documented owner.

## Bad example

An exception from either awaited write skips both close operations.

```python
import asyncio


async def send_message(host: str, port: int, payload: bytes) -> None:
    _, writer = await asyncio.open_connection(host, port)
    writer.write(payload)
    await writer.drain()  # An exception skips both close operations.
    writer.close()
    await writer.wait_closed()
```

## Good example

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def open_writer(
    host: str,
    port: int,
) -> AsyncIterator[asyncio.StreamWriter]:
    _, writer = await asyncio.open_connection(host, port)
    try:
        yield writer
    finally:
        writer.close()
        await writer.wait_closed()


async def send_message(host: str, port: int, payload: bytes) -> None:
    async with open_writer(host, port) as writer:
        writer.write(payload)
        await writer.drain()
```

## Caveats and compatibility

- Whether `connect()` or `acquire()` is itself an async context manager is
  library-specific; inspect its documented protocol instead of guessing.
- Cleanup can also fail. Do not replace a useful primary exception with a vague
  cleanup error, but do surface cleanup failure when it invalidates the operation.
- Cancellation can arrive at any await point, including cleanup. Critical cleanup
  may require a narrowly designed shield and a separate timeout; shielding an
  entire operation makes shutdown unreliable.
- Parenthesized multiple context managers are supported by Python 3.10, but Tedd
  should introduce async code only for a measured need.
- The example uses only the standard library.

## Verification

- [ ] Success closes the resource exactly once.
- [ ] An exception in the body still closes it.
- [ ] A cancellation test proves cleanup runs and cancellation propagates.
- [ ] Acquisition failure does not attempt invalid cleanup.
- [ ] Ownership and lifespan are stated at the call boundary.

## Related guidance

- [Make failure observable](coding-error-no-silent-exceptions.md)
- [Testing](testing.md)
