# Use Dataclasses for Honest Value Models

## Invariant

Use a dataclass when generated initialization, comparison, and representation match
a value-oriented model. Configure defaults, representation, mutation, and derived
state explicitly so generated behavior does not overstate the model's guarantees.

## Applies when

- A record primarily stores validated values.
- Generated equality or initialization removes meaningful boilerplate.
- A request, plan, result, or internal row shape has named fields.

## Does not apply when

- The class primarily coordinates services or owns a complex lifecycle.
- Identity-based equality is required.
- A plain tuple, TypedDict, or function parameter is clearer.

## Bad example

Freezing the outer instance neither freezes the list nor prevents the token from
appearing in generated representations.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    paths: list[str]
    token: str  # Included in repr; paths remain mutable despite frozen=True.
```

## Good example

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Plan:
    paths: tuple[Path, ...]
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.paths:
            raise ValueError("a plan requires at least one path")


@dataclass(slots=True)
class CaptureBatch:
    record_ids: list[str] = field(default_factory=list)
```

## Caveats and compatibility

- `frozen=True` prevents normal field reassignment; it does not recursively
  freeze contained objects or prevent every low-level mutation.
- Frozen equality may cause dataclasses to generate a hash, but actual hashability
  still depends on configuration and every participating field.
- Use `default_factory` for mutable defaults.
- `repr=False` is only defense in depth against an accidental generated
  representation. Attribute access, `dataclasses.asdict`, custom serialization,
  pickling, process memory, and debugging tools can still expose the value. Prefer a
  dedicated secret handle or keep secrets out of general-purpose value models.
- A value computed once in `__post_init__` can become stale if its inputs remain
  mutable; calculate it as a property or make the inputs immutable.
- `slots=True` is available in Python 3.10. Confirm reflection, weak-reference,
  inheritance, serialization, and monkeypatch needs before enabling it.

## Verification

- [ ] Mutable defaults use a factory.
- [ ] Secret-bearing fields are avoided where possible; generated representations,
  serialization, logs, and diagnostics are explicitly redacted when they remain.
- [ ] Frozen models contain immutable field types where deep stability matters.
- [ ] Equality and hashing match the domain identity contract.
- [ ] Tests cover constructor invariants and serialization behavior.

## Related guidance

- [Properties](coding-data-properties.md)
- [Pure boundaries](coding-design-pure-boundaries.md)
- [Typing](typing.md)
