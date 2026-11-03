# Require Behavioral Substitutability

## Invariant

Every implementation of an abstraction must honor the same accepted inputs,
observable outputs, mutation, durability, privacy, and failure contract. Matching a
method name or static signature is not sufficient.

## Applies when

- Multiple filesystem, store, serializer, or platform implementations share a type.
- Replacing a real collaborator with a fake in tests.
- Subclassing or implementing a Protocol used by existing callers.

## Does not apply when

- Two objects deliberately have different contracts and should have different types.
- Shared implementation is being considered without a common behavioral role.
- A runtime shape check is being mistaken for a contract test.

## Bad example

The implementation weakens the missing-record contract by returning a plausible
empty payload.

```python
from typing import Protocol


class RecordReader(Protocol):
    def read(self, record_id: str) -> bytes:
        """Return the record, raising KeyError when it does not exist."""
        ...


class LenientReader:
    def read(self, record_id: str) -> bytes:
        return self._records.get(record_id, b"")
        # Missing and present-but-empty are no longer distinguishable.
```

## Good example

```python
from typing import Protocol


class RecordReader(Protocol):
    def read(self, record_id: str) -> bytes:
        """Return the record, raising KeyError when it does not exist."""
        ...


class MemoryRecordReader:
    def __init__(self, records: dict[str, bytes]) -> None:
        self._records = dict(records)

    def read(self, record_id: str) -> bytes:
        return self._records[record_id]


def assert_reader_contract(reader: RecordReader) -> None:
    try:
        reader.read("missing")
    except KeyError:
        return
    raise AssertionError("reader did not honor the missing-record contract")
```

## Caveats and compatibility

- An implementation must not strengthen preconditions or weaken postconditions.
- Additional implementation-specific exceptions are acceptable only when they fit
  the abstraction's documented failure hierarchy and caller handling.
- Fakes must preserve failures and transaction semantics, not merely happy-path
  return types.
- `@runtime_checkable` verifies attribute presence, not this behavior.
- Liskov's principle applies to Protocol implementations as well as subclasses.

## Verification

- [ ] One contract-test suite runs against every production implementation and fake.
- [ ] Accepted inputs and boundary values are identical.
- [ ] Missing, conflict, permission, and corruption outcomes remain distinguishable.
- [ ] Durability and no-overwrite guarantees are not weakened by an implementation.
- [ ] Callers require no implementation-type checks.

## Related guidance

- [Composition](coding-design-composition.md)
- [Typing with protocols](coding-typing-protocols.md)
- [Correctness and durability](correctness.md)
