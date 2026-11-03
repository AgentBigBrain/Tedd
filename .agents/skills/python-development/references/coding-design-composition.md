# Prefer Composition for Independent Behaviors

## Invariant

Compose components when they have independent responsibilities or lifecycles.
Use inheritance only when every subtype is behaviorally substitutable for the base
and shared implementation is a real part of that contract.

## Applies when

- Serialization and publication vary independently.
- Platform behavior is selected behind a stable capability.
- Tests need to combine a real domain component with a fake effect boundary.

## Does not apply when

- A plain function parameter is sufficient.
- A true subtype shares both contract and implementation.
- Creating multiple tiny objects would obscure one cohesive invariant.

## Bad example

Inheritance couples storage and export lifecycles, while `str(records)` is not a
defined interchange format.

```python
class JsonExportingStore(SqliteStore):
    def save_and_export(self, records: list[dict[str, object]]) -> None:
        self.save(records)
        self.destination.write_text(str(records), encoding="utf-8")
```

## Good example

```python
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Receipt:
    record_id: str
    count: int


class Encoder(Protocol):
    def encode(self, value: Receipt) -> bytes: ...


class Publisher(Protocol):
    def publish(self, destination: Path, content: bytes) -> None: ...


class JsonEncoder:
    def encode(self, value: Receipt) -> bytes:
        payload = {"record_id": value.record_id, "count": value.count}
        return json.dumps(payload, sort_keys=True).encode("utf-8")


class ExportService:
    def __init__(self, encoder: Encoder, publisher: Publisher) -> None:
        self._encoder = encoder
        self._publisher = publisher

    def export(self, destination: Path, value: Receipt) -> None:
        self._publisher.publish(destination, self._encoder.encode(value))
```

## Caveats and compatibility

- "Prefer composition" is contextual guidance, not a ban on inheritance.
- Do not add a Protocol for one stable implementation unless it creates a useful
  test or platform seam.
- Composed components still need compatible failure, ownership, and transaction
  semantics; swapping an encoder must not weaken publication guarantees.
- Format-aware serializers remain mandatory regardless of class structure.
- Give an encoder a validated domain type or JSON value contract. An arbitrary
  `Mapping[str, object]` is neither guaranteed to be accepted by `json.dumps` nor
  guaranteed to contain JSON-compatible values.
- The example uses Python 3.10 standard-library APIs only.

## Verification

- [ ] Each component owns one independent behavior.
- [ ] Replacing one component does not require subclass overrides elsewhere.
- [ ] Contract tests cover every production implementation.
- [ ] Unsupported value shapes fail at validation rather than inside publication.
- [ ] Composition does not split an atomic transaction into partial operations.
- [ ] A simpler function-based design was considered.

## Related guidance

- [Dependency injection](coding-design-dependency-injection.md)
- [Substitutability](coding-design-substitutability.md)
- [Design and maintainability](design.md)
