# Inject Volatile Dependencies

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Make volatile or replaceable dependencies explicit at the boundary where they are
used. Inject clocks, randomness, filesystem publication, subprocess execution, and
storage adapters when doing so exposes an invariant or enables a deterministic test.

## Applies when

- Code otherwise reaches into time, environment, filesystem, or process globals.
- Multiple platform implementations or a test fake must honor one contract.
- A security-sensitive primitive needs fault injection for failure-path tests.

## Does not apply when

- A pure standard-library function is already deterministic and trivial to call.
- An interface would mirror one concrete implementation with no testing benefit.
- A dependency-injection container would add runtime machinery solely for style.

## Bad example

The service hides its clock and publication policy, preventing deterministic time
and failure injection while using a non-durable direct write.

```python
import json
from datetime import datetime, timezone
from pathlib import Path


class ReceiptService:
    def write(self, path: Path, record_id: str) -> None:
        payload = {
            "record_id": record_id,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
```

## Good example

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class Publisher(Protocol):
    def publish(self, destination: Path, content: bytes) -> None: ...


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()


class ReceiptService:
    def __init__(self, clock: Clock, publisher: Publisher) -> None:
        self._clock = clock
        self._publisher = publisher

    def write(self, path: Path, record_id: str) -> None:
        payload = {
            "record_id": record_id,
            "verified_at": canonical_utc(self._clock.now()),
        }
        content = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._publisher.publish(path, content)
```

## Caveats and compatibility

- Constructor and function-parameter injection are usually enough for Tedd. A
  service locator or global `get_*()` accessor merely hides the dependency.
- Use a `Protocol` when multiple implementations or structural test doubles
  exist; otherwise a callable parameter may be simpler.
- `Mock(spec=...)` restricts attributes but does not guarantee call-signature
  enforcement. Use `create_autospec(..., instance=True)` or a small fake when
  signature accuracy matters.
- Keep concrete dependency construction in one composition root.
- Define clock timezone semantics. Reject naive datetimes and normalize aware values
  to UTC before persistence or comparison.
- Protocol and dataclass features used by this pattern are available in Python 3.10.

## Verification

- [ ] The dependency and its lifecycle are visible in the constructor or signature.
- [ ] Tests inject deterministic time and exact filesystem failures.
- [ ] Tests reject a naive datetime and normalize a non-UTC aware datetime to UTC.
- [ ] The production composition root constructs the real implementation once.
- [ ] No hidden global fallback bypasses the injected dependency.
- [ ] The abstraction has a behavioral contract, not just matching method names.

## Related guidance

- [Typing with protocols](coding-typing-protocols.md)
- [Shared state](coding-design-shared-state.md)
- [Testing](testing.md)
