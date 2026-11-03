# Own Every Async Task

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Every created task must have an owner that retains it, retrieves its terminal
exception, and defines shutdown behavior. If a caller needs the result, await the
operation directly instead of treating it as background work.

## Applies when

- Work must intentionally outlive the function that starts it.
- A process-level supervisor owns several background operations.
- A bounded service loop needs coordinated cancellation and shutdown.

## Does not apply when

- The result or failure is part of the current operation; await it directly.
- A simple sequential await is adequate.
- Task creation would only disguise blocking filesystem or CPU work as async.

## Bad example

The caller neither retains the task nor observes its terminal failure.

```python
import asyncio


async def schedule_projection() -> None:
    asyncio.create_task(rebuild_projection())  # No owner and no observed result.
```

## Good example

```python
import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol


class FailureReporter(Protocol):
    def report(self, code: str) -> None: ...


class TaskSupervisor:
    def __init__(self, reporter: FailureReporter) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._reporter = reporter
        self._failed = False

    def spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            self._failed = True
            self._reporter.report("background_task_failed")

    @property
    def failed(self) -> bool:
        return self._failed

    async def cancel_all(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

## Caveats and compatibility

- The event loop keeps weak references to tasks. Retaining a strong reference is
  necessary, but it is not sufficient: terminal failures must also be observed.
- A done callback makes failure observable to the supervisor; it cannot return a
  failed result to the original caller.
- Preserve a supervised failure state and emit a stable, non-sensitive event. Do not
  write raw exception text or tracebacks to ordinary logs; an approved private
  diagnostic path must apply explicit redaction and retention policy.
- The supervisor itself needs an explicit owner and shutdown point. Do not replace
  one hidden global task with an immortal module-global task registry.
- Let `asyncio.CancelledError` propagate from task bodies after cleanup.
- `asyncio.TaskGroup` is Python 3.11+. Tedd's Python 3.10 floor requires the
  explicit ownership pattern unless compatibility policy changes.

## Verification

- [ ] Every `create_task` call stores or immediately awaits the task.
- [ ] A failing task produces an observable supervised failure.
- [ ] Failure events and logs exclude private paths, payloads, and tracebacks.
- [ ] Shutdown cancels and awaits all owned tasks.
- [ ] Cancellation is not logged as an ordinary application defect.
- [ ] Tests leave no pending tasks or "exception was never retrieved" warnings.

## Related guidance

- [Async concurrency results](coding-async-concurrency-results.md)
- [Async bounded work](coding-async-bounded-work.md)
- [Make failure observable](coding-error-no-silent-exceptions.md)
