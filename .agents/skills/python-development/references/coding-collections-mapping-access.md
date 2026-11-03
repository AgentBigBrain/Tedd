# Mapping access follows missing-value semantics

Index required keys; use `get` or a sentinel when absence is permitted. Distinguish
missing, present `None` and invalid data rather than assigning plausible defaults.
Validate the external shape before indexing nested values.

Both `get(key, build())` and `setdefault(key, build())` evaluate `build()` even on
a hit; `setdefault` also mutates the mapping. For an expensive lazy default, catch
`KeyError` only around the lookup, then build/publish separately. A builder's own
`KeyError` must propagate. Concurrent publication requires a winner policy.

A sentinel preserves an optional field's meaning:

```python
from collections.abc import Mapping

_MISSING = object()


def optional_timeout(config: Mapping[str, object]) -> int | None:
    value = config.get("timeout", _MISSING)
    if value is _MISSING:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("timeout must be a non-negative integer")
    return value
```

Test absent, `None`, booleans, invalid types and valid adjacent values. Required
fields must fail rather than silently default. See [boundary validation](coding-validation-boundary-data.md)
and [correctness](correctness.md) for shared storage and concurrency contracts.
