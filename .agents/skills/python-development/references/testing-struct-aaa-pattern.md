# Make setup, action and evidence visible

Arrange-Act-Assert is a readability aid. Keep the starting state, exercised
behavior and outcome obvious; do not add decorative phase comments or split a
state-machine/migration scenario that needs intermediate assertions.

Several assertions may prove one coherent outcome. A second action is valid
when the transition sequence is the contract. Shared setup should expose the
inputs relevant to the failure rather than hide them in a fixture registry.
Read the test without implementation context and confirm it explains what broke.

See [testing](testing.md) and [one behavioral reason](testing-struct-one-behavior.md).
