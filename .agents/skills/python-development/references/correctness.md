# Correctness and durability

## Preserve explicit contracts

- Define the valid inputs, observable outputs, state transition, failure result, and compatibility range before editing code.
- Keep Python 3.10 as the syntax and standard-library floor while Tedd advertises 3.10+.
- Keep OS-specific APIs and dependencies behind existing platform boundaries; changes to one platform must preserve the shared contract on the others.
- Distinguish host filesystem paths from guest/protocol paths. Use native `Path` objects for host access and explicit protocol semantics, such as `PurePosixPath`, for POSIX guest paths. `Path("/opt/helper")` is not a drive-qualified absolute path on Windows; do not weaken absolute-path validation to accommodate it.
- Validate at trust boundaries and keep internal invariants explicit. Avoid repeating the same validation at every layer.
- Use format-aware encoders for JSON, CSV, XML, URLs, and SQL. Never assemble structured formats by concatenating unescaped user-controlled strings.

## Handle errors by semantics

- Catch the narrowest exception set whose recovery behavior is understood.
- Suppress an `OSError` only when its `errno` or platform error code proves the operation is unsupported and the documented fallback remains correct.
- Propagate permission, I/O, corruption, full-disk, and durability failures. Reporting success after such a failure violates the storage contract.
- Preserve exception causes with `raise ... from error` when translating an implementation error into a stable domain or CLI error.
- Keep cleanup in `finally` or context managers; do not hide the primary failure with best-effort cleanup.

## Make durable writes honest

For a durable atomic replacement:

1. Create a temporary file in the destination directory.
2. Write all bytes, flush the Python buffer, and `fsync` the file.
3. Apply and verify private permissions before publication when required.
4. Atomically publish the file.
5. `fsync` the parent directory where supported.
6. Report success only after every required step succeeds.

Treat unsupported directory synchronization separately from real storage errors. Add tests for an unsupported-platform error and for a permission or I/O error that must propagate.

## Remove pathname races

- Do not validate a security-sensitive path and later reopen it by name. Open once with no-follow semantics where supported, validate the opened object's metadata, and consume the same descriptor or already-bounded bytes.
- Reject symlinks and Windows reparse points at the actual use boundary, not only during an earlier check.
- Keep bounded input size enforcement while reading; a prior `stat` is not sufficient when another process can replace or extend a file.
- For create-without-overwrite, use an atomic primitive such as exclusive creation, a hard-link publication protocol, or a platform abstraction with equivalent semantics. A separate existence check followed by `replace` is not no-clobber.
- Design concurrent tests with explicit barriers or events. Avoid timing-only sleeps as the proof of a race fix.

## Use SQLite paths correctly

- Prefer ordinary path arguments when URI features are unnecessary.
- When using SQLite URI parameters such as `mode=ro`, percent-encode the filesystem path as a URI path before appending query parameters. Test spaces, `#`, `?`, `%`, non-ASCII characters, and Windows drive or UNC forms where supported.
- Keep SQL values parameterized. Identifiers require explicit allowlists or safe quoting; placeholders do not bind identifiers.
- Keep transactions short and choose `BEGIN` behavior deliberately for concurrent writers.

## Bound work and state

- Limit bytes, records, nesting, path length, retry duration, lock wait, and output size at the boundary closest to the untrusted source.
- Distinguish the number of active operations from the number of materialized tasks or payloads. A semaphore alone does not bound queued coroutine objects.
- Make retries idempotent or guarded by explicit compare-and-swap, fingerprint, revision, or uniqueness conditions.
