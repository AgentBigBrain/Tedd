# Make Failure Observable

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Cause and remediation](#classify-by-cause-and-remediation)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Tedd must not report success after an operation fails. Catch an exception only when
the current layer can recover, translate it into a stable domain error, add useful
context, or convert it into an explicit non-success result at a process boundary.

## Applies when

- Reading or writing persistent state, configuration, backups, or exports.
- Translating filesystem, SQLite, parsing, or subprocess errors.
- Supervising background work or implementing a CLI entry point.
- Providing a documented fallback for one precisely identified condition.

## Does not apply when

- A context manager is performing best-effort cleanup while the primary exception
  is already being preserved.
- An optional resource is absent and absence is explicitly part of the contract.
- A probe intentionally tests for a capability and distinguishes unsupported
  behavior from permission, corruption, or I/O failure.

## Bad example

This blanket catch turns an unknown durability failure into an apparent success.

```python
def sync_parent(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        pass  # A full disk or permission failure now looks like success.
```

## Good example

```python
import errno
import os
from pathlib import Path


class DurabilityError(RuntimeError):
    """A required durability operation did not complete."""


_UNSUPPORTED_DIRECTORY_FSYNC = {
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def sync_parent(path: Path) -> None:
    if os.name == "nt":
        # Native Windows directory handles need a different durability policy.
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)  # Opening failures always propagate.
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC:
                return
            raise DurabilityError("parent directory synchronization failed") from exc
    finally:
        os.close(directory_fd)
```

## Caveats and compatibility

- Unsupported-operation handling must be platform-specific and proven by an error
  code or documented capability after the directory was opened successfully;
  `except OSError: pass` is never equivalent. A bad descriptor is a defect, not a
  capability result.
- Do not catch `BaseException` in ordinary code. Cancellation, `KeyboardInterrupt`,
  and `SystemExit` normally must propagate.
- Logging is not recovery. If a CLI catches a domain error, it must also return a
  nonzero status and a stable, non-sensitive error payload.
- Cleanup errors require judgment: preserve the primary exception, but do not
  silently claim a durable commit if final synchronization or publication failed.
- This rule is fully compatible with Python 3.10 and adds no runtime dependency.

## Classify by cause and remediation

Choose a category from the cause at the owning boundary and who can remedy it.
Preserve each transport's current public vocabulary; do not replace every error
with a universal five-code scheme.

| Cause | Remediation | Current public examples |
| --- | --- | --- |
| Malformed caller input | Caller supplies an accepted shape/range | Graph `invalid_request` |
| Missing configuration, unsupported platform or unavailable service | Restore setup/capability; retry only when evidence changes | Graph `unavailable_capability`; source `source_unavailable` |
| Access, authority, scope or privacy refusal | Obtain applicable authority or narrow scope; do not route around the refusal | `authority_denied`, `scope_denied`, `privacy_rejected` |
| Genuinely malformed stored bytes/state | Verify evidence and use the reviewed repair/recovery path | Source `source_corrupt`; graph `unhealthy_graph` |
| Conflicting revision or stale snapshot | Refresh bounded evidence and review a new guarded attempt | Graph `conflict`, `stale_state` |
| Unexpected internal defect | Preserve private diagnostic cause; fix the implementation | Graph `internal_error` |

`SourceArchiveError` currently defaults to `source_corrupt`; that is an existing
compatibility constraint, not a recommended label for a new input/configuration
boundary. `GraphReadError` already requires an explicit typed category. New base
errors should require a deliberate category or typed subtype. Do not classify
from message substrings, or treat every `ValueError`, missing file or `OSError`
as corruption: the operation and trusted cause determine the meaning.

This synthetic read-boundary example uses the existing graph categories. Its
local exception types stand for already-classified causes; it changes no Tedd
runtime code. The final catch is a process/service error boundary: it raises a
non-success error with a safe fixed message and retained cause.

```python
from collections.abc import Callable

from tedd_knowledge_graph.v4_application_contracts import (
    GraphReadError,
    GraphReadErrorCategory as Category,
)


class InvalidInput(ValueError):
    pass


class UnavailableConfiguration(RuntimeError):
    pass


class DeniedScope(PermissionError):
    pass


class MalformedStore(ValueError):
    pass


class StaleSnapshot(RuntimeError):
    pass


CAUSES: tuple[tuple[type[Exception], Category, str], ...] = (
    (InvalidInput, Category.INVALID_REQUEST, "input is invalid"),
    (UnavailableConfiguration, Category.UNAVAILABLE_CAPABILITY, "configuration unavailable"),
    (DeniedScope, Category.SCOPE_DENIED, "scope denied"),
    (MalformedStore, Category.UNHEALTHY_GRAPH, "stored data is malformed"),
    (StaleSnapshot, Category.STALE_STATE, "snapshot changed; refresh before retry"),
)


def guarded_read(read: Callable[[], None]) -> None:
    try:
        read()
    except Exception as error:
        for error_type, category, message in CAUSES:
            if isinstance(error, error_type):
                raise GraphReadError(category, message) from error
        raise GraphReadError(Category.INTERNAL_ERROR, "read failed internally") from error
```

For a direct CLI boundary, serialize only its documented safe fields and return
a nonzero status. Preserve `KeyboardInterrupt`, `SystemExit` and cancellation;
the example catches `Exception`, not `BaseException`. The caller must not convert
the translated failure into an empty success result. Real unavailable-service
and conflict adapters need their own typed evidence, not a broad default.

### Classification controls

Append these tests to the example in one disposable file; expose the checked-out
graph scripts on its Python import path. They execute no store or native helper.
Test the real boundary separately before claiming implemented classification.

```python
import unittest


class ClassificationTests(unittest.TestCase):
    def test_each_cause_keeps_its_category_and_private_cause(self) -> None:
        cases = (
            (InvalidInput, Category.INVALID_REQUEST),
            (UnavailableConfiguration, Category.UNAVAILABLE_CAPABILITY),
            (DeniedScope, Category.SCOPE_DENIED),
            (MalformedStore, Category.UNHEALTHY_GRAPH),
            (StaleSnapshot, Category.STALE_STATE),
            (KeyError, Category.INTERNAL_ERROR),
        )
        for error_type, category in cases:
            with self.subTest(cause=error_type.__name__):
                cause = error_type("synthetic-private-detail")

                def fail() -> None:
                    raise cause

                with self.assertRaises(GraphReadError) as caught:
                    guarded_read(fail)
                self.assertEqual(caught.exception.category, category)
                self.assertIs(caught.exception.__cause__, cause)
                self.assertNotIn("synthetic-private-detail", str(caught.exception))

    def test_success_and_stop_are_distinct(self) -> None:
        performed: list[str] = []
        guarded_read(lambda: performed.append("read"))
        self.assertEqual(performed, ["read"])

        def stop() -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            guarded_read(stop)


if __name__ == "__main__":
    unittest.main()
```

## Verification

- [ ] A regression test injects the expected recoverable condition.
- [ ] A separate test injects a permission or I/O error and proves it propagates.
- [ ] Opening failure and `EBADF` controls propagate rather than being classified as
  unsupported.
- [ ] Translated errors retain their cause with `raise ... from exc`.
- [ ] CLI failures have a nonzero exit status and contain no traceback by default.
- [ ] No plausible default is returned for an unexpected failure.
- [ ] Input, setup/service availability, refusal, stored-data corruption,
  concurrency and internal-defect controls produce their documented non-success
  categories at the actual boundary. Public codes remain compatible.

## Related guidance

- [Correctness and durability](correctness.md)
- [Security](security.md)
- [Testing](testing.md)
