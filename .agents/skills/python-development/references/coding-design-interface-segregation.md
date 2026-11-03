# Define Interfaces from Consumer Needs

## Invariant

Each consumer should depend only on the capability it uses. Split a broad Protocol
or abstract interface when real implementations or callers use distinct subsets,
not preemptively for every method.

## Applies when

- Implementations contain stubs or `NotImplementedError` for unrelated methods.
- Read-only code is forced to depend on mutation behavior.
- Tests must fake a large service to exercise one operation.

## Does not apply when

- Every consumer and implementation genuinely needs the cohesive full contract.
- Splitting would separate methods that must share one transaction or invariant.
- No real caller demonstrates the proposed capability boundary.

## Bad example

The read-only renderer is typed against capture, migration, and destructive
capabilities it must never use.

```python
from typing import Protocol


class GraphStore(Protocol):
    def recall(self, query: str) -> list[str]: ...
    def capture(self, packet: bytes) -> int: ...
    def migrate(self) -> None: ...
    def delete_all(self) -> None: ...


def render_context(store: GraphStore, query: str) -> str:
    return "\n".join(store.recall(query))
```

## Good example

```python
from typing import Protocol


class RecallReader(Protocol):
    def recall(self, query: str) -> list[str]: ...


class CaptureWriter(Protocol):
    def capture(self, packet: bytes) -> int: ...


def render_context(reader: RecallReader, query: str) -> str:
    return "\n".join(reader.recall(query))
```

## Caveats and compatibility

- Name protocols by capability when that clarifies their consumer role.
- Small does not mean one method by rule. Keep operations together when their shared
  invariant or transaction makes them one capability.
- Structural typing reduces inheritance ceremony but does not validate behavioral
  semantics.
- A concrete class may implement several protocols without declaring inheritance.
- `Protocol` is standard in Python 3.10.

## Verification

- [ ] Each protocol corresponds to at least one real consumer.
- [ ] Implementations do not contain unrelated stubs.
- [ ] Read-only consumers cannot reach mutation methods through their declared type.
- [ ] Contract tests cover each capability independently.
- [ ] The split preserves transactional boundaries.

## Related guidance

- [Typing with protocols](coding-typing-protocols.md)
- [Responsibility seams](coding-design-responsibility-seams.md)
- [Typing](typing.md)
