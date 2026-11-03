# Tedd Memory Evaluation Instrument Revision 2

Status: current portable instrument on `main`

Protocol ID: `tedd-memory-eval-protocol-v2`

Report schema: `tedd-memory-eval-report-v2`

Scenario catalog: `tedd-memory-scenarios-v1`

Evaluator tokenizer: `tedd-memory-eval-tokenizer-v1`

## Purpose and inheritance

Revision 2 hardens the public synthetic evaluation instrument. It does not introduce a new
retrieval candidate, alter production retrieval, or replace the reviewed version 1 result.
It inherits the four version 1 providers, twelve frozen scenarios, two budgets, warm-up and
repetition counts, scoring rules, privacy boundary, and qualification gates from
[`PROTOCOL.md`](PROTOCOL.md).

The exact portable version 1 source remains reproducible from commit
`508206d654c0a8e716af22672eafa85df324e8f2` in the maintainers' private repository. The
reviewed historical baseline and its source remain preserved there by annotated tag
`research-archive/memory-eval-lab`. Version 1 and version 2
reports have different protocol, schema, and evaluator digests and must not be represented as
the same instrument revision.

## Versioned changes

Revision 2 makes only these instrument changes:

- bind each allocated temporary leaf to its physical filesystem identity while accepting a
  platform parent alias such as macOS `/var` to `/private/var`;
- reject symlink, junction, reparse-point, containment, and identity-substitution failures at
  the disposable path boundary;
- bind source reproducibility metadata to the graph runtime Python tree used by the lab;
- accept typed injected synthetic catalogs and a bounded protocol identifier for real-boundary
  characterization tests while keeping the public CLI fixed to this catalog and protocol;
- serialize review-safe payloads as canonical JSON with non-finite numbers rejected;
- fail closed on malformed, explicit-container, foreign, and dangling payload references; and
- report dependency-free evaluator-token counts alongside character counts.

Evaluator tokens are comparison units produced by the frozen regular-expression tokenizer.
They are not model tokens, billed tokens, or a cost estimate. Token efficiency is descriptive
and does not alter the inherited version 1 qualification gates.

## Isolation and public command

The public CLI remains path-free. It accepts only the timeout and pretty-print options, creates
one disposable synthetic graph, runs the inherited provider matrix, emits one sanitized JSON
report to standard output, and removes the graph on exit:

```text
python scripts/memory_eval_lab.py --pretty
```

Changing a scenario, provider, budget, scoring rule, tokenizer, report schema, or safety
classification requires another explicit protocol revision.
