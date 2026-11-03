# Keep Test Support Near Its Consumers

Category policy: [Testing](testing.md).

## Tedd rule and invariant

Place shared setup at the narrowest module or test package that owns it. A test
must remain understandable without searching a suite-wide grab bag, and changing
one helper must not silently alter unrelated test families.

## Applies when

- Two or more tests share synthetic object construction, cleanup, or assertions.
- A helper has a clear domain owner such as profile, capture, migration, or CLI tests.
- Moving setup removes real duplication without hiding the behavior under test.

## Does not apply when

- Only one test module uses the setup; keep it in that module.
- The shared resource is production data, a live graph, a real key, or a user profile.
- A global helper would couple otherwise independent suites.

## Bad example

```python
# tests/support.py grows into a suite-wide registry with unclear consumers.
def graph_store() -> Path: ...
def webhook_payload() -> dict[str, object]: ...
def windows_acl() -> object: ...
```

## Corrected Tedd example

```python
# test_capture_support.py, imported only by capture test modules.
def write_capture(directory: Path, payload: bytes) -> Path:
    path = directory / "capture.json"
    path.write_bytes(payload)
    return path
```

Keep single-module setup beside its tests. Promote it only after another module
needs the same contract. If Tedd later adopts pytest, use the nearest applicable
`conftest.py`; do not introduce pytest merely to obtain fixture discovery.

## Caveats and edge cases

- Shared helpers may contain setup logic, but assertions should normally remain
  near the behavior they explain.
- A compatibility helper used across versioned graph suites may be intentionally
  central; document its supported consumers and stable surface.
- Importing support code still executes Python. Inspect unfamiliar test support
  before discovery or collection.

## Verification

- Search for every helper consumer and confirm its location matches that scope.
- Run the affected modules both alone and in the full release suite.
- Reverse test order where practical and confirm no hidden shared state appears.
