# Use Protocols for Real Structural Interfaces

## Invariant

Use a `Protocol` when multiple implementations or test doubles should satisfy
a small structural contract without inheriting shared code. Use an abstract base
class when inherited implementation and enforced overrides are intentional.

## Applies when

- Platform-specific implementations expose one capability.
- External or test code should implement an interface without inheritance.
- A consumer needs only a small subset of a larger object's behavior.

## Does not apply when

- Only one implementation exists and a callable or concrete type is simpler.
- Subclasses must share state or template-method implementation.
- Runtime validation of signatures is required.

## Bad example

Inheritance is required even though no implementation is shared, and the example
constructs JSON without an encoder.

```python
from abc import ABC, abstractmethod


class Serializer(ABC):
    @abstractmethod
    def to_json(self, value: dict[str, object]) -> str: ...


class Receipt(Serializer):
    def to_json(self, value: dict[str, object]) -> str:
        return f'{{"id": "{value["id"]}"}}'  # Unsafe hand-built JSON.
```

## Good example

```python
import json
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Receipt:
    record_id: str
    count: int


class Encoder(Protocol):
    def encode(self, value: Receipt) -> bytes: ...


class JsonEncoder:
    def encode(self, value: Receipt) -> bytes:
        payload = {"record_id": value.record_id, "count": value.count}
        return json.dumps(payload, sort_keys=True).encode("utf-8")


def encode_receipt(encoder: Encoder, receipt: Receipt) -> bytes:
    return encoder.encode(receipt)
```

## Caveats and compatibility

- Structural type checking verifies shape, not behavioral semantics.
- `@runtime_checkable` checks only runtime attribute presence and does not
  validate parameter or return types.
- Protocols may declare properties and attributes, but overly broad protocols create
  the same coupling as broad ABCs.
- Use an ABC when subclasses intentionally inherit concrete implementation or a
  template method.
- `Protocol` is in the Python 3.10 standard library.
- Prefer a validated domain value or explicit JSON type to `Mapping[str, object]`;
  protocol conformance cannot make unsupported serialization values valid.

## Verification

- [ ] At least two real implementations or a meaningful test double justify the seam.
- [ ] The protocol contains only behavior the consumer actually uses.
- [ ] Static type checking accepts each intended implementation.
- [ ] Contract tests verify semantics beyond method presence.
- [ ] Serialization examples use standard format-aware encoders.
- [ ] Invalid boundary shapes are rejected before reaching an encoder.

## Related guidance

- [Interface segregation](coding-design-interface-segregation.md)
- [Dependency injection](coding-design-dependency-injection.md)
- [Typing](typing.md)
