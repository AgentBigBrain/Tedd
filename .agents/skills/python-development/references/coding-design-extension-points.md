# Add Extension Points Only for Expected Variation

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Keep stable orchestration closed to incidental variant logic when multiple variants
are expected now. Extend through a small injected strategy or callable, but retain a
simple branch when the variant set is finite and unlikely to grow.

## Applies when

- Two or more encoders, publishers, or platform policies already share orchestration.
- New variants are a documented product requirement.
- A growing conditional repeatedly changes a security-sensitive stable path.

## Does not apply when

- There is one implementation and no verified second variant.
- A small explicit `if` or `match` is easier to audit.
- Extension would permit untrusted dynamic imports or plug-in execution.

## Bad example

Every format changes stable publication code, and two branches use undefined,
unsafe text conversion instead of a format-aware serializer.

```python
def export(destination: Path, payload: dict[str, object], format_name: str) -> None:
    if format_name == "json":
        content = json.dumps(payload).encode("utf-8")
    elif format_name == "text":
        content = str(payload).encode("utf-8")
    elif format_name == "xml":
        # Unsafe: interpolation does not apply XML escaping.
        content = f"<payload>{payload}</payload>".encode("utf-8")
    else:
        raise ValueError("unsupported format")
    publish_atomic(destination, content)
```

## Good example

```python
import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Receipt:
    record_id: str
    count: int


Encoder = Callable[[Receipt], bytes]


def encode_json(receipt: Receipt) -> bytes:
    payload = {"record_id": receipt.record_id, "count": receipt.count}
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def encode_csv(receipt: Receipt) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("record_id", "count"))
    writer.writerow((receipt.record_id, receipt.count))
    return output.getvalue().encode("utf-8")


def export(
    destination: Path,
    receipt: Receipt,
    *,
    encode: Encoder,
) -> None:
    publish_atomic(destination, encode(receipt))


def export_json(destination: Path, receipt: Receipt) -> None:
    export(destination, receipt, encode=encode_json)


def export_csv(destination: Path, receipt: Receipt) -> None:
    export(destination, receipt, encode=encode_csv)
```

## Caveats and compatibility

- Open/closed is not a mandate to replace every conditional with classes.
- Keep extension selection in the trusted composition root. Do not load a module or
  execute a plug-in named by untrusted input.
- Every new encoder must use a format-aware serializer and retain output bounds.
- Keep each encoder's accepted domain shape explicit; do not claim an arbitrary
  mapping is valid JSON, CSV, or XML data.
- An extension point must not allow callers to bypass private permissions, atomic
  publication, validation, or durability.
- Callable aliases and keyword-only arguments shown here work on Python 3.10.

## Verification

- [ ] At least two current or approved variants justify the seam.
- [ ] Stable orchestration and safety controls are shared.
- [ ] Each variant has contract and round-trip tests.
- [ ] JSON and CSV variants escape adversarial text through their format-aware APIs.
- [ ] Untrusted data cannot select arbitrary executable code.
- [ ] A simpler reviewed branch was considered and rejected for a concrete reason.

## Related guidance

- [Pragmatic design](coding-design-pragmatism.md)
- [Dependency injection](coding-design-dependency-injection.md)
- [Security](security.md)
