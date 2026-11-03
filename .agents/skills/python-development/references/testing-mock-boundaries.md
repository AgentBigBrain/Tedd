# Replace Boundaries Without Replacing Owned Behavior

Category policy: [Testing](testing.md).

## Tedd rule and invariant

Exercise Tedd-owned normalization, validation, and state transitions directly.
Replace a boundary only when isolation, deterministic failure injection, or an
external side effect requires it. Patch the lookup site actually used by the code.

## Applies when

- Injecting a clock, random source, filesystem primitive, subprocess runner, or
  external service response.
- Proving a permission, durability, cleanup, timeout, or publication failure.
- A small fake models a stateful collaborator more clearly than call assertions.

## Does not apply when

- The dependency is pure Tedd logic that can run safely and cheaply.
- A temporary directory or synthetic SQLite database provides a more faithful test.
- The mock would merely restate internal call order after a harmless refactor.

## Bad example

```python
@patch("knowledge_graph.normalize_capture", return_value={"title": "ok"})
@patch("knowledge_graph.validate_capture", return_value=None)
def test_capture(mock_validate, mock_normalize) -> None:
    self.assertEqual(capture({"title": "bad"})["title"], "ok")
```

## Corrected Tedd example

```python
def test_directory_sync_permission_error_is_not_hidden(self) -> None:
    error = OSError(errno.EACCES, "denied")
    with patch("knowledge_graph.os.fsync", autospec=True, side_effect=error):
        with self.assertRaises(PermissionError):
            publish_atomically(self.destination, b"synthetic")
```

The corrected test runs owned publication logic and replaces the exact operating-
system primitive needed to force the otherwise difficult branch.

## Caveats and edge cases

- "External" is not synonymous with "always mock." Real temporary files and
  databases often catch integration errors that mocks miss.
- Patching a name in the module under test is correct when that is where lookup
  occurs; patching the original definition may have no effect.
- Interaction assertions are appropriate when the interaction is the contract,
  such as `fsync` before publication or no shell invocation.

## Verification

- Prove the double was reached and the intended branch failed for that reason.
- Include an unaffected control using the real safe implementation.
- Remove or alter a private helper call and ensure the behavior-focused test does
  not fail unless the public contract changed.
