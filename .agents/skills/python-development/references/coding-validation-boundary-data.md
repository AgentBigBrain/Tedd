# Validate Boundary Data Without Runtime Frameworks

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Bound and validate external data once at the trust boundary, produce a typed
standard-library value, and reject ambiguous coercion. Tedd's runtime remains free
of third-party validation dependencies.

## Applies when

- Parsing JSON, CLI values, environment variables, capture files, or SQLite rows.
- Data crosses from another process, repository, user-controlled path, or version.
- A stable internal dataclass can represent the accepted shape.

## Does not apply when

- Data was already validated by the same trusted boundary and its invariant is
  preserved.
- Repeating validation at every internal layer would create divergent policy.
- Permissive coercion is being proposed merely to make malformed input pass.

## Bad example

The parser has no byte bound, accepts arbitrary shapes, and coerces values such as
booleans or numeric strings without an explicit contract.

```python
import json


def load_request(content: bytes) -> CaptureRequest:
    payload = json.loads(content)
    return CaptureRequest(
        source=payload["source"],
        expected_watermark=int(payload["expected_watermark"]),
    )
```

## Good example

```python
import json
from dataclasses import dataclass
from typing import NoReturn


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CHARS = 512
MAX_JSON_INTEGER_DIGITS = 19
MAX_WATERMARK = (1 << 63) - 1


class JsonBoundaryError(ValueError):
    """JSON input exceeds the accepted scalar or object contract."""


class DuplicateJsonKeyError(JsonBoundaryError):
    """A JSON object repeated one key."""


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError("capture input contains a duplicate key")
        result[key] = value
    return result


def bounded_json_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise JsonBoundaryError("capture integer exceeds the digit limit")
    return int(raw)


def reject_json_float(raw: str) -> NoReturn:
    raise JsonBoundaryError("capture input does not accept floating-point values")


def reject_json_constant(raw: str) -> NoReturn:
    raise JsonBoundaryError("capture input does not accept non-finite constants")


@dataclass(frozen=True)
class CaptureRequest:
    source: str
    expected_watermark: int


def load_request(content: bytes) -> CaptureRequest:
    if len(content) > MAX_CAPTURE_BYTES:
        raise ValueError("capture input exceeds the byte limit")

    try:
        payload = json.loads(
            content,
            object_pairs_hook=unique_object,
            parse_int=bounded_json_int,
            parse_float=reject_json_float,
            parse_constant=reject_json_constant,
        )
    except JsonBoundaryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("capture input is not valid JSON") from exc

    return decode_request(payload)


def decode_request(payload: object) -> CaptureRequest:
    if not isinstance(payload, dict):
        raise ValueError("capture input must be an object")
    if set(payload) != {"source", "expected_watermark"}:
        raise ValueError("capture input has missing or unknown fields")

    source = payload["source"]
    watermark = payload["expected_watermark"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if len(source) > MAX_SOURCE_CHARS:
        raise ValueError("source exceeds the character limit")
    if type(watermark) is not int or not 0 <= watermark <= MAX_WATERMARK:
        raise ValueError("expected_watermark must be a signed 64-bit non-negative integer")

    return CaptureRequest(source=source.strip(), expected_watermark=watermark)
```

## CLI and MCP adapters

Keep `argparse.Namespace` inside the CLI adapter. Both transports call the same
typed decoder and transport-neutral service; do not make MCP simulate CLI flags.
This runnable extension to the example above only previews synthetic input. It is
not Tedd's complete capture contract or authority to write a graph.

```python
import argparse


def cli_request(argv: list[str]) -> CaptureRequest:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-watermark", required=True, type=bounded_json_int)
    args = parser.parse_args(argv)
    return decode_request(
        {"source": args.source, "expected_watermark": args.expected_watermark}
    )


def preview_capture(request: CaptureRequest) -> dict[str, object]:
    return {"source": request.source, "based_on": request.expected_watermark}


def cli_preview(argv: list[str]) -> dict[str, object]:
    return preview_capture(cli_request(argv))


def mcp_preview(arguments: object) -> dict[str, object]:
    return preview_capture(decode_request(arguments))
```

Prefer existing request and decoder contracts over a parallel model. Graph
`v4_cli_capture` already builds the production `CaptureRequest`; source
`tedd_sources.capture` still receives a namespace. Those are different current
implementations, not proof this example has refactored either. Convert at the
owning boundary, which need not literally be `main`. Small internal functions
need no new dataclass when their existing precise parameters suffice. Preserve
transaction, source authority, scope, provenance and reviewed-plan gates in a
real mutation service.

## Caveats and compatibility

- `bool` is a subclass of `int`; use an exact type check when JSON
  booleans must not satisfy an integer field.
- Bound bytes before parsing and every accepted field afterward. Catch parser depth
  failure as a stable rejection. If a format intentionally accepts nested data, use
  a parser or incremental strategy with an enforced depth limit; a post-parse walk
  alone cannot prevent parser recursion.
- Choose and test a duplicate-key policy. The example rejects duplicates instead of
  silently accepting the last occurrence.
- Apply numeric limits before conversion. Early Python 3.10 patch releases lack the
  interpreter digit limit, and later limits can vary by interpreter configuration;
  a byte limit alone can still permit an expensive huge integer.
- Normalization such as trimming text must be an explicit contract, not silent
  coercion of unrelated types.
- Pydantic can be appropriate in an application that intentionally adopts it, but
  it is not appropriate for Tedd's current standard-library runtime contract.
- Never hand-build JSON, CSV, XML, SQL, or URI escaping as "validation."

## Verification

- [ ] Tests cover malformed encoding, malformed JSON, wrong top-level type, unknown
  fields, duplicate keys, excessive nesting, overlong fields, booleans-as-integers,
  huge integer tokens, the signed 64-bit upper bound, negative values, floats,
  non-finite constants, and the byte limit.
- [ ] A valid adjacent input remains accepted.
- [ ] Parsed values are typed before entering domain code.
- [ ] CLI and MCP produce equal requests/results for equivalent input, and reject
  booleans, unknown fields and invalid ranges before the service is called.
- [ ] Error messages do not echo secrets or unbounded source content.
- [ ] No third-party runtime package is introduced.

## Related guidance

- [Public typing contracts](coding-typing-public-contracts.md)
- [Correctness and durability](correctness.md)
- [Security](security.md)
