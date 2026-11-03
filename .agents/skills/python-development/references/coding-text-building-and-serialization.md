# Separate Text Assembly from Structured Serialization

## Invariant

Use `str.join` or `bytes.join` for a bounded collection of trusted text
fragments. Use the standard format-aware serializer for JSON, CSV, XML, URLs, SQL,
and paths; joining strings is not escaping.

## Applies when

- Combining already-encoded lines, labels, or byte fragments.
- A bounded result must exist as one string or byte sequence.
- Repeated concatenation obscures the intended delimiter.

## Does not apply when

- Producing structured data with quoting or escaping rules.
- Output can be unbounded and should stream.
- Two or three local values are clearer as an f-string.

## Bad example

Both functions confuse delimiter joining with the escaping required by their
structured formats.

```python
def receipt_json(record_id: str, note: str) -> str:
    return '{"record_id":"' + record_id + '","note":"' + note + '"}'


def csv_row(values: list[str]) -> str:
    return ",".join(values)
```

## Good example

```python
import csv
import io
import json


def receipt_json(record_id: str, note: str) -> str:
    return json.dumps(
        {"record_id": record_id, "note": note},
        ensure_ascii=False,
        sort_keys=True,
    )


def csv_row(values: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="")
    writer.writerow(values)
    return output.getvalue()


def render_lines(lines: list[str]) -> str:
    return "\n".join(lines)
```

## Caveats and compatibility

- Repeated `+=` can be costly, but CPython optimizes some local concatenation;
  do not promise universal O(n-squared) behavior or label it critical without data.
- `join` still materializes the final result and may retain all fragments.
- XML requires an XML API, URLs require URI quoting, SQL values require parameters,
  and filesystem paths require `pathlib.Path` semantics.
- Canonical JSON may need explicit separators, Unicode, number, and ordering policy.
- The example uses only Python 3.10 standard-library modules.

## Verification

- [ ] Adversarial quotes, delimiters, newlines, Unicode, and backslashes round-trip.
- [ ] Structured output is parsed by the corresponding standard-library parser.
- [ ] Output size is bounded or streamed.
- [ ] Canonical-output options are explicit and tested.
- [ ] No user-controlled fragment is inserted by manual escaping.

## Related guidance

- [Boundary validation](coding-validation-boundary-data.md)
- [Correctness and durability](correctness.md)
- [Security](security.md)
