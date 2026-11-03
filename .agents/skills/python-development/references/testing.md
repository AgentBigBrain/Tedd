# Testing

## Test behavior and invariants

- Keep `unittest` unless a migration solves a concrete problem. Test framework choice is secondary to determinism, coverage, and failure quality.
- Name tests after the behavior and condition they prove.
- Prefer one primary failure reason per test while allowing several assertions that establish one coherent outcome.
- Use arrange-act-assert as a readability aid, not a mandatory visual ritual.

Test code may contain loops, conditions, exception handling, derived expectations, reference models, and concurrency coordination when they express an invariant without duplicating the production algorithm. A blanket ban would weaken property, numerical, migration, and race tests.

For skills, static checks establish packaging, usable references, and structured
interfaces, not agent judgment. Do not freeze prose, headings, or section order
as a substitute for behavioral verification. Check instruction changes with
bounded realistic scenarios, including when to continue and when to stop;
report whether those were live exercises or review-only checks.

## Use doubles accurately

- Mock at the boundary where the code under test looks up the dependency.
- Use `create_autospec` or `autospec=True` when call signatures must be enforced. `spec=` and `spec_set=` constrain attributes but do not provide the same call-signature guarantee.
- Prefer a small fake for stateful collaboration or complex call sequences.
- Assert the observable result first; assert calls only when the interaction is itself part of the contract.
- Use scoped patching and always restore global state.

## Build safe fixtures

- Use synthetic data and temporary directories. Never copy a production graph, profile, key, backup, or projection into the repository.
- Build host-path fixtures from native temporary directories, not hard-coded Unix roots. Keep POSIX guest-path fixtures separate from host filesystem operations; do not mock away the validation the test is meant to exercise.
- Default to the narrowest fixture lifetime that keeps tests clear. Broader scope is valid for expensive immutable setup when isolation is preserved.
- Centralize truly shared fixture construction, but keep domain-specific setup near the tests that explain it.
- Give parameters stable human-readable IDs when they materially improve failure diagnosis.

## Prove failure paths

- Test success, adjacent behavior, malformed input, size limits, permission failures, cleanup, and unaffected controls.
- For atomic and durable writes, inject or mock the exact filesystem primitive and verify unsupported errors are classified separately from real I/O failures.
- For races, use multiprocessing or separate handles with barriers/events so the contested interleaving is deterministic.
- For subprocess output tests, establish readiness with a bounded handshake rather than assuming startup completes in a short fixed interval. Test deadline expiry separately; do not lengthen production deadlines merely to hide slow test startup.
- For path handling, test symlinks or reparse points where supported and special URI characters on each relevant OS.
- For migrations and persisted state, verify equivalence, rollback boundaries, idempotency, and corruption refusal.

## Bounded parser controls

Use synthetic fixtures, literal expected results and valid adjacent inputs. Along
with format-specific rejection tests, a small seeded truncation/byte-mutation
corpus can expose uncaught exceptions and lost progress. For the
[synthetic parser](security.md#bound-untrusted-file-parsing), save this as
`test_bounded_parser.py` beside `bounded_parser.py` in a disposable directory.
Only the documented refusal is an expected parse outcome; never catch every
`Exception` and call it a pass.

```python
import random
import unittest

from bounded_parser import MAX_BYTES, MAX_ITEMS, ParserRefusal, parse_items


class ParserTests(unittest.TestCase):
    def test_valid_adjacent_controls(self) -> None:
        self.assertEqual(parse_items(b"TR\x00\x01A\x00\x00"), [b"A", b""])
        self.assertEqual(parse_items(b"TR"), [])
        self.assertEqual(parse_items(b"TR" + b"\x00\x00" * MAX_ITEMS), [b""] * MAX_ITEMS)
        self.assertEqual(parse_items(b"TR\x00\x80" + b"A" * 128), [b"A" * 128])

    def test_known_budget_and_truncation_refusals(self) -> None:
        for raw in (
            b"TR" + b"\x00\x00" * (MAX_ITEMS + 1),
            b"TR\x00\x81" + b"A" * 129,
            b"TR\x00\x01",
            b"TR" + b"A" * MAX_BYTES,
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ParserRefusal):
                parse_items(raw)

    def test_small_seeded_mutation_corpus(self) -> None:
        sample = b"TR\x00\x02AB\x00\x01C"
        corpus = [sample[:end] for end in range(len(sample) + 1)]
        rng = random.Random(42)
        for _ in range(32):
            changed = bytearray(sample)
            changed[rng.randrange(len(changed))] ^= rng.randrange(1, 256)
            corpus.append(bytes(changed))
        for index, raw in enumerate(corpus):
            with self.subTest(case=index):
                try:
                    items = parse_items(raw)
                except ParserRefusal:
                    continue
                self.assertLessEqual(len(items), MAX_ITEMS)
                self.assertLessEqual(sum(map(len, items)), len(raw))


if __name__ == "__main__":
    unittest.main()
```

Run the saved test from that directory in a child process when hangs/crashes are
in scope. A timeout, abnormal exit or unexpected exception is a failure; it must
not become a known refusal. Choose a bounded deadline that permits host startup,
and independently exercise timeout handling with a deliberately stalled child.

```python
import os
import subprocess
import sys

environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
subprocess.run(
    [sys.executable, "-B", "-S", "test_bounded_parser.py"],
    env=environment,
    check=True,
    timeout=20,
)
```

This deterministic smoke test covers only its declared fixtures and mutations.
It is not a fuzzer, proof of complete format coverage, a parser security audit or
native-decoder qualification. Keep real formats' depth/pixel/decompression checks
and their neighboring valid controls in focused tests.

## Preserve test execution trust

Shared-contract tests must run across supported platforms. Skip only tests that
exercise a genuinely unavailable platform integration, with an explicit reason;
a Mac-specific feature name does not justify skipping portable host contracts.
Use the repository verification wrappers and CI gates defined in `CONTRIBUTING.md`;
record the exact candidate, tested platforms, skips, and unavailable checks.
Mocked coverage is not native-helper qualification, and an earlier green commit
does not qualify a later change.

Test discovery can import repository-controlled modules, configuration, plugins, and fixtures; setup display can execute fixtures and test bodies. Before running `unittest`, pytest, coverage, or a test plugin in an unfamiliar checkout, inspect the entry points and use an isolated environment with explicit approval.

Do not globally ignore all deprecation warnings. Treat owned-code warnings as errors and narrowly match unavoidable third-party warnings by module and message.
