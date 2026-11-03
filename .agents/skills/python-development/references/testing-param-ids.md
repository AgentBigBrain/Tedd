# Diagnostic case IDs

Name non-trivial `subTest` rows by their condition/invariant. A short scalar may
already identify itself; do not add labels that hide the input or include private
values, filesystem paths, tokens or volatile data. Report a deterministic seed
separately when using synthetic randomized cases.

Verify a failing row is recognizable without counting rows, with stable unique
labels that survive case reordering. `subTest` labels diagnostics but does not
create independently loader-selectable tests; use a named method when independent
selection or setup/cleanup is required. This does not justify a framework change.
See [case tables](testing-param-subtest-cases.md) and [test names](testing-struct-test-naming.md).
