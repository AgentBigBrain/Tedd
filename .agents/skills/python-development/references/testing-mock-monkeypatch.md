# Restore Environment and Global State Reliably

Category policy: [Testing](testing.md).

## Tedd rule and invariant

Scope changes to environment variables, module attributes, dictionaries, clocks,
and the working directory so they are restored even when setup or assertions fail.
Prefer dependency injection when process-global mutation can be avoided.

## Applies when

- Testing configuration read from `os.environ`.
- Replacing a module attribute or platform capability for one test.
- Temporarily changing process state in a strictly serial test.

## Does not apply when

- Tests can run concurrently in the same process.
- An explicit function parameter or injected collaborator is available.
- The value contains a real credential or private path.

## Bad example

```python
os.environ["TEDD_MODE"] = "test"
settings.PLATFORM = "windows"
self.assertEqual(load_mode(), "test")
# Restoration is skipped if the assertion fails.
```

## Corrected Tedd example

```python
def test_mode_is_read_from_environment(self) -> None:
    with patch.dict(os.environ, {"TEDD_MODE": "synthetic"}, clear=False):
        self.assertEqual(load_mode(), "synthetic")


def test_platform_branch(self) -> None:
    with patch.object(settings, "PLATFORM", "windows"):
        self.assertTrue(platform_policy().requires_private_acl)
```

Use `TemporaryDirectory` and pass the resulting path explicitly instead of changing
the working directory. If `chdir` is unavoidable, save the original path and
register restoration with `addCleanup` before changing it.

## Caveats and edge cases

- Context-manager cleanup restores the prior value; it does not make a global
  mutation thread-safe.
- Module import caches may retain configuration read before the patch. Test the
  supported reload boundary rather than deleting arbitrary `sys.modules` entries.
- pytest's `monkeypatch` offers similar cleanup, but Tedd does not need pytest for
  this capability.

## Verification

- Assert the original state before and after the patch scope.
- Force an assertion failure in a focused test and confirm registered cleanup runs.
- Run global-state tests serially and prove unrelated tests see their original state.
