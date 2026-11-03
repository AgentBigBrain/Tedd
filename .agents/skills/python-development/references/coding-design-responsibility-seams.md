# Split Responsibilities at Stable Seams

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Parsing, boundary validation, domain decisions, storage mutation, serialization, and
CLI error mapping should be separately testable responsibilities. Orchestration
should state their order without reimplementing them.

## Applies when

- One function changes for unrelated parser, storage, output, and policy reasons.
- A large command handler is difficult to test without invoking every side effect.
- A low-coupling responsibility can be extracted behind a stable contract.

## Does not apply when

- Splitting creates pass-through functions with no independent invariant.
- The proposed seam crosses a transaction and makes rollback harder to reason about.
- A wholesale rewrite would break public imports or monkeypatch surfaces.

## Bad example

One command owns unbounded path reading, loose parsing, validation, transaction
control, and user output, so none of those policies has an isolated contract.

```python
def capture_command(path: Path, store: Store) -> int:
    raw = path.read_bytes()
    packet = json.loads(raw)
    if "records" not in packet:
        print('{"status":"error"}')
        return 1
    store.begin()
    store.insert_many(packet["records"])
    store.commit()
    print(json.dumps({"status": "ok", "count": len(packet["records"])}))
    return 0
```

## Good example

```python
from pathlib import Path


def load_capture(path: Path) -> bytes:
    return read_bounded_regular_file(path)


def parse_capture(content: bytes) -> CapturePacket:
    return validate_capture_json(content)


def apply_capture(store: Store, packet: CapturePacket) -> CaptureResult:
    return store.capture(packet)


def public_capture_error(error: CaptureError) -> tuple[str, str]:
    if isinstance(error, CaptureInputError):
        return "invalid_capture", "capture input is invalid"
    if isinstance(error, CaptureConflictError):
        return "capture_conflict", "capture state changed; retry from a fresh plan"
    return "capture_failed", "capture could not be completed"


def capture_command(path: Path, store: Store) -> int:
    try:
        packet = parse_capture(load_capture(path))
        result = apply_capture(store, packet)
    except CaptureError as exc:
        code, message = public_capture_error(exc)
        emit_cli_error(code, message)
        return 1
    emit_cli_result(result)
    return 0
```

## Caveats and compatibility

- A responsibility is a reason to change, not a line-count threshold.
- Use complexity findings to inspect changed high-risk branches, especially input
  parsing and mutation orchestration. Look for independently testable responsibilities
  and preserve their error/transaction semantics. Moving branches into one-use
  wrappers solely to beat a metric can hide the same risk. Do not impose global
  complexity, cast-count or private-import caps without a separate reviewed decision.
- Keep transactional steps together when separating them would permit partial state.
- Preserve compatibility imports while callers migrate, then remove the adapter only
  after repository consumers are verified.
- Avoid duplicating validation at every layer; validate at the boundary and assert
  internal invariants where needed.
- Map known domain failures to stable public codes and bounded, non-sensitive messages.
  Raw exception text belongs only in an explicitly approved private diagnostic path.
- Examples use abstract Tedd collaborators and require no runtime framework.

## Verification

- [ ] Each extracted unit has one named behavior and focused tests.
- [ ] The orchestrator remains the sole owner of ordering and error translation.
- [ ] Transaction and rollback behavior are unchanged.
- [ ] Public import paths and command output remain compatible or are versioned.
- [ ] Error tests prove code specificity and prevent private paths, payloads, and
  tracebacks from entering public output.
- [ ] The extraction does not deepen a dependency on the legacy monolith.

## Related guidance

- [Control flow](coding-design-control-flow.md)
- [Pragmatic design](coding-design-pragmatism.md)
- [Design and maintainability](design.md)
