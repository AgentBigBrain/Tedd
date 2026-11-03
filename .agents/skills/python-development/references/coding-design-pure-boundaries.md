# Keep Deterministic Logic Inside Explicit Effect Boundaries

## Invariant

Normalization, validation, classification, and fingerprint logic should be pure
when practical. Pass time and other nondeterministic inputs explicitly, and keep
filesystem, database, environment, and output effects in named orchestration code.

## Applies when

- The same transformation must be deterministic across tests and migrations.
- Code mixes time, randomness, environment access, or mutation with calculation.
- A complex storage workflow can separate planning from applying.

## Does not apply when

- A function's purpose is explicitly to perform I/O or mutate a transaction.
- Copying a very large structure merely to appear pure would violate resource bounds.
- The effect boundary would become hidden behind a misleading "pure" wrapper.

## Bad example

The function reads hidden time, mutates its input, performs I/O, and therefore
cannot be replayed as a deterministic transformation.

```python
from datetime import datetime, timezone


def verify_receipt(receipt: dict[str, object]) -> dict[str, object]:
    receipt["verified_at"] = datetime.now(timezone.utc).isoformat()
    write_receipt(receipt)
    return receipt
```

## Good example

```python
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class VerifiedReceipt:
    record_id: str
    verified_at: str


def build_verified_receipt(
    record_id: str,
    verified_at: datetime,
) -> VerifiedReceipt:
    normalized_id = record_id.strip()
    if not normalized_id:
        raise ValueError("record_id is required")
    if verified_at.tzinfo is None or verified_at.utcoffset() is None:
        raise ValueError("verified_at must be timezone-aware")
    return VerifiedReceipt(
        record_id=normalized_id,
        verified_at=verified_at.astimezone(timezone.utc).isoformat(),
    )


def verify_and_publish(record_id: str, clock: Clock, publisher: Publisher) -> None:
    receipt = build_verified_receipt(record_id, clock.now())
    publisher.publish(receipt)
```

## Caveats and compatibility

- A frozen dataclass prevents attribute reassignment; it does not deeply freeze
  mutable members or make secret-bearing representations safe.
- Purity does not make a function cheap, bounded, safe to cache, or safe to
  parallelize. Caches can retain private values and change memory behavior.
- Passing an entire application context to every pure helper hides dependencies
  again; pass only the values the calculation needs.
- Plan/apply separation must bind the applied operation to the verified plan.
- Treat timezone awareness and UTC normalization as part of a timestamp's contract;
  injection alone does not make a naive datetime safe to persist.
- The example is compatible with Python 3.10 and the standard library.

## Verification

- [ ] Repeating the pure function with equal inputs yields an equal result.
- [ ] Naive datetimes are rejected and aware offsets normalize to UTC.
- [ ] Unit tests need no clock, environment, filesystem, or database monkeypatch.
- [ ] Mutation and I/O occur only at named boundaries.
- [ ] Plan tokens or revisions bind any later effect to the checked state.
- [ ] No secret is introduced into a dataclass representation or cache.

## Related guidance

- [Dependency injection](coding-design-dependency-injection.md)
- [Dataclasses](coding-data-dataclasses.md)
- [Correctness and durability](correctness.md)
