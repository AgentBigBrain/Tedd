# Name tests by condition and outcome

Name the observable contract, not only the called function. Let the test class
carry stable subject context: `test_existing_destination_is_preserved` explains
more than `test_backup_2`. Keep private values, paths and volatile identifiers out
of test names and diagnostics.

Preserve discovery prefixes. Rename only where diagnosis improves; broad naming
churn is not a correctness fix. After an internal refactor, confirm the name
still states the condition and expected behavior.

See [diagnostic case IDs](testing-param-ids.md) and [testing](testing.md).
