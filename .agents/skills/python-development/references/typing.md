# Typing

## Treat types as an executable design check

- Annotate public functions, module boundaries, persistent record shapes, and callbacks first.
- Keep runtime validation for untrusted JSON, filesystem data, CLI values, and database rows. Type hints do not validate runtime input.
- Prefer precise unions, `Literal`, `TypedDict`, dataclasses, and small protocols over unbounded `Any` at stable boundaries.
- In production, receive unknown values as `object` or an existing validated JSON union, then narrow through runtime checks. The release gate rejects explicit `Any` in every production module, including serialization and compatibility adapters.
- Prefer `object` when a value is unknown but arbitrary operations should not be permitted.

## Preserve the production baseline

Production modules are already covered by the quality gate. Include new modules
in that scope and preserve the no-explicit-`Any` rule. Use concrete types,
`TypedDict`, recursive JSON unions, or protocols as the boundary requires.

Gradual improvement applies to remaining implicit typing and documented test-only
exceptions. Narrow those exceptions with evidence; they do not authorize a new
production override. Describe the actual enforced flags rather than labeling a
partial checker configuration `strict`.

Use `cast` only when runtime evidence already establishes the type and the checker cannot infer it. A cast performs no validation.
Prefer returning precise values from reusable boundary decoders to casting again
at every caller. Reuse a decoder only when its validation and error semantics fit
the new boundary; a stored-source decoder that reports corruption may be wrong
for malformed caller input. See [public typing](coding-typing-public-contracts.md)
and [boundary validation](coding-validation-boundary-data.md).

## Preserve compatibility

- Use syntax accepted by the declared minimum Python version.
- Prefer postponed annotations where circular imports or runtime evaluation would otherwise create compatibility problems.
- Keep optional imports behind clear feature or type-checking boundaries.
- Test import and CLI startup on every supported interpreter in CI; a successful static check on one version does not prove runtime compatibility.

## Configure checkers accurately

- Start with one primary enforced checker. Add another only when its distinct signal justifies the maintenance cost.
- Pin tool versions through the development dependency group or lock workflow.
- Use real diagnostic codes from the selected checker and verify examples against the pinned version.
- Do not globally silence broad categories such as all assignment, import, or `Any` errors to make the gate green.
