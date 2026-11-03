# One primary behavioral reason to fail

Several assertions may jointly prove one result, such as a structured non-success
status, specific code and private-data-free output. Split unrelated behavior when
the first failure masks independent defects and the split improves diagnosis.

Preserve migration, recovery and state-machine sequences whose intermediate
assertions establish the same contract. Characterization tests may initially cover
a broad legacy surface; split them only when behavior remains faithful and setup
stays independent. Avoid multiplying expensive fixtures for a style rule.

State the outcome in one sentence and check that the test name and evidence match
it. A separated test must run without hidden ordering. See [testing](testing.md).
