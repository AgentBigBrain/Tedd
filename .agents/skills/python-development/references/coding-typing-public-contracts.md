# Type Stable Public Boundaries Precisely

## Invariant

Public parameters, return values, callbacks, and persistent record shapes must use
the most precise maintainable type that matches runtime behavior. Untrusted values
remain `object` until runtime validation narrows them.

## Applies when

- Defining a public function, CLI adapter, storage record, or callback.
- A dictionary has a stable set of keys.
- A consumer needs only an abstract input capability such as `Iterable` or
  `Mapping`.

## Does not apply when

- A value crosses a dynamic compatibility boundary without a known shape; accept
  `object` or the existing JSON union and validate before narrowing. This changes
  the starting type, not Tedd's ban on explicit production `Any`.
- A domain alias adds a new name without clarifying units, format, or identity.
- Static typing would be used as a substitute for parsing untrusted data.

## Bad example

`Any` permits unchecked indexing at both levels and leaves callers without a
return contract.

```python
from typing import Any


def receipt_id(payload: Any):
    return payload["receipt"]["id"]
```

## Good example

```python
from typing import TypedDict


class Receipt(TypedDict):
    record_id: str
    verified_at: str


def parse_receipt(value: object) -> Receipt:
    if not isinstance(value, dict):
        raise ValueError("receipt must be an object")

    record_id = value.get("record_id")
    verified_at = value.get("verified_at")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("record_id must be a non-empty string")
    if not isinstance(verified_at, str) or not verified_at:
        raise ValueError("verified_at must be a non-empty string")

    return {"record_id": record_id, "verified_at": verified_at}


def receipt_id(receipt: Receipt) -> str:
    return receipt["record_id"]
```

## Caveats and compatibility

- Type hints do not enforce runtime input. JSON, SQLite rows, CLI strings, and
  filesystem metadata still require explicit checks.
- Prefer `object` over `Any` when arbitrary operations should be forbidden.
- A `cast` records an already-proven fact for the checker; it performs no check.
- Prefer an existing typed decoder, such as the source `object_fields` or
  `positive` helper where its shape, limits and failure semantics fit. Otherwise
  runtime-narrow `object` in a small boundary decoder and return the accepted
  type. Repeating casts at downstream callers obscures where evidence was checked.
  A localized cast for an already-validated dynamic adapter remains appropriate;
  document the fact the checker cannot infer. Cast counts do not measure safety.
- Keep CLI namespaces in adapters. Use the [shared request example](coding-validation-boundary-data.md#cli-and-mcp-adapters)
  when both CLI and MCP call the same service; retain existing request contracts.
- Use abstract collection inputs only when the function truly needs that limited
  interface. A stable snapshot may correctly require a concrete tuple or list.
- All syntax in this card is supported by Python 3.10.

## Verification

- [ ] Public parameters and return values, including side-effect-only `None`,
  are annotated.
- [ ] Stable mapping shapes use `TypedDict` or an explicit dataclass.
- [ ] Untrusted values are validated before a cast or typed return.
- [ ] New code does not widen existing types to `Any`.
- [ ] The configured gradual type-checker scope passes.

## Related guidance

- [Boundary validation](coding-validation-boundary-data.md)
- [Typing with protocols](coding-typing-protocols.md)
- [Typing](typing.md)
